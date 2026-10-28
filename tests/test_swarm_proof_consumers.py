"""Offline acceptance contracts for a gateway and a generated CLI worker."""

import hashlib
import io
import json
import sys
import threading
from unittest.mock import Mock

import pytest

from fake_backend import FakeBackend
from cli_fixtures import synchronize_cli_start
from tessera.backend import Backend
from tessera.config import BackendConfig
from tessera.credentials import ResolvedCredential
from tessera.jev import JudgeKit
from tessera.jobs import SwarmJobs
from tessera.log import ChatLog
from test_status_projection_consumers import disk_snapshot
from test_swarm_job_consumers import (
    enabled_config, offline_capabilities, synthetic_cli, worker_specs,
)


@pytest.fixture
def mixed_providers(tmp_path, monkeypatch, offline_capabilities):
    config = enabled_config(tmp_path)
    command = synthetic_cli(tmp_path, offline_capabilities)
    synchronize_cli_start(monkeypatch, command)
    config.accounts["beta-account-placeholder"].provider_kind = "cli_worker"
    config.backends["beta-backend-placeholder"] = BackendConfig(
        name="beta-backend-placeholder", account="beta-account-placeholder",
        extra={"cli": {"vendor": "claude", "command": str(command),
                       "cwd": str(tmp_path), "timeout": 3}})
    config.backends["alpha-backend-placeholder"].api_key_env = "EXAMPLE_SWARM_GATEWAY_KEY"
    observed = {"selected": [], "http": [], "gates": [], "finish": "stop", "deny_phase": None,
                "command": command}
    credential = Mock()
    credential.resolve.return_value = ResolvedCredential("gateway-placeholder", "env")
    observed["credential"] = credential
    barrier = threading.Barrier(2)
    real_gate = JudgeKit.swarm_gates

    def gate(kit, objective, **options):
        if options["phase"] == observed["deny_phase"]:
            result = {"allowed": False, "source": "fixture"}
        else:
            result = real_gate(kit, objective, **options)
        observed["gates"].append((options["phase"], result))
        return result

    monkeypatch.setattr(JudgeKit, "swarm_gates", gate)

    def http(request, timeout):
        observed["http"].append(request)
        assert request.full_url == "https://models.example.com/alpha/chat/completions"
        assert request.get_header("Authorization") == "Bearer gateway-placeholder"
        payload = json.loads(request.data)
        assert payload["model"] == "alpha-model-placeholder"
        assert [row["role"] for row in payload["messages"]] == ["system", "user"]
        assert not payload.get("tools") and timeout > 0
        if observed["finish"] == "timeout":
            raise TimeoutError("Synthetic gateway timeout")
        return io.BytesIO(json.dumps({"choices": [{"message": {"role": "assistant",
            "content": "Alpha verified fixture"}, "finish_reason": observed["finish"]}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}}).encode())

    def factory(settings):
        observed["selected"].append((settings.provider_kind, settings.account))
        assert settings.extra["retries"] == 0
        transport = Backend(settings.name, settings.base_url, settings.model, settings.api_key_env, settings.extra)
        if settings.provider_kind == "openai_chat":
            transport._credentials = credential
            transport._opener = Mock(open=http)

        def reply(messages, tools):
            barrier.wait(timeout=3)
            return transport.chat(messages, tools=tools)

        return FakeBackend([reply])

    return config, factory, observed


def test_mixed_provider_proof_recomputes_artifact_hashes_and_reopens_offline(tmp_path, mixed_providers):
    config, factory, observed = mixed_providers
    jobs = SwarmJobs(config, backend_factory=factory)
    with ChatLog(tmp_path / "proof-log") as log:
        job = jobs.create("Join synthetic protocol findings", worker_specs(), session_slug="proof-placeholder", log=log)
        result = jobs.run(job["id"], log=log)
        assert result["status"] == "succeeded"
        assert set(observed["selected"]) == {("openai_chat", "alpha-account-placeholder"),
                                             ("cli_worker", "beta-account-placeholder")}
        assert len(observed["http"]) == 1
        observed["credential"].resolve.assert_called_once_with("", env="EXAMPLE_SWARM_GATEWAY_KEY")
        assert "Inspect beta fixture" in json.loads(observed["command"].with_suffix(".request.json").read_text())["prompt"]
        assert observed["gates"][0][0] == "admission"
        assert [phase for phase, _ in observed["gates"]].count("merge") == 1
        assert all(gate["allowed"] is True for _, gate in observed["gates"])
        references = list(result["artifacts"].values()) + [result["merged"]]
        for reference in references:
            raw = (jobs.root / job["id"] / reference["file"]).read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            assert digest == reference["sha256"]
            assert reference["ref"] == "swarm:" + job["id"] + ":" + reference["file"] + ":" + digest
        merged = jobs.artifact(job["id"])
        assert [row["content"] for row in merged["artifacts"]] == [
            "Alpha verified fixture", "Beta fixture inspected: beta-evidence-placeholder"]
        assert merged["checks"]["artifact_hashes_valid"] is True and all(merged["checks"].values())
        assert merged["gate"]["allowed"] is True
        tasks = jobs.tasks.list()
        assert len(tasks) == 2 and all(task["status"] == "succeeded" and task["attempt"] == 1 for task in tasks)
        card = jobs.board.get(job["id"])
        assert card["status"] == "succeeded" and set(card["artifacts"]) == {ref["ref"] for ref in references}
        before = disk_snapshot(tmp_path)
        forbidden = Mock(side_effect=AssertionError("A completed proof cannot rerun either provider"))
        reopened = SwarmJobs(config, backend_factory=forbidden)
        assert reopened.run(job["id"], log=log) == result
        assert reopened.artifact(job["id"]) == merged
        assert disk_snapshot(tmp_path) == before
        forbidden.assert_not_called()


@pytest.mark.parametrize("target", ["worker", "merged"])
def test_tampered_mixed_provider_proof_refuses_reads_and_resume_without_repair(tmp_path, mixed_providers, target):
    config, factory, observed = mixed_providers
    jobs = SwarmJobs(config, backend_factory=factory)
    with ChatLog(tmp_path / "proof-log") as log:
        job = jobs.create("Verify mixed evidence integrity", worker_specs(), session_slug="proof-placeholder", log=log)
        result = jobs.run(job["id"], log=log)
        reference = result["artifacts"]["1"] if target == "worker" else result["merged"]
        path = jobs.root / job["id"] / reference["file"]
        path.write_bytes(path.read_bytes() + b" ")  # Still valid JSON, different immutable bytes.
        before = disk_snapshot(tmp_path)
        forbidden = Mock(side_effect=AssertionError("Integrity failure cannot restart providers"))
        reopened = SwarmJobs(config, backend_factory=forbidden)
        with pytest.raises(ValueError, match="integrity"):
            reopened.artifact(job["id"])
        with pytest.raises(ValueError, match="integrity"):
            reopened.run(job["id"], log=log)
        assert disk_snapshot(tmp_path) == before
        forbidden.assert_not_called()
        assert len(observed["http"]) == 1


@pytest.mark.parametrize("phase", ["admission", "merge"])
def test_mixed_provider_gate_rejection_never_publishes_a_merged_proof(tmp_path, mixed_providers, phase):
    config, factory, observed = mixed_providers
    observed["deny_phase"] = phase
    jobs = SwarmJobs(config, backend_factory=factory)
    with ChatLog(tmp_path / "proof-log") as log:
        job = jobs.create("Gate mixed protocol findings", worker_specs(), session_slug="proof-placeholder", log=log)
        result = jobs.run(job["id"], log=log)
        assert result["status"] == "blocked" and result["merged"] is None
        card = jobs.board.get(job["id"])
        assert card["status"] == "blocked"
        expected = 0 if phase == "admission" else 2
        assert len(observed["selected"]) == expected and len(card["artifacts"]) == expected
        assert all(task["attempt"] == (0 if phase == "admission" else 1) for task in jobs.tasks.list())
        if phase == "admission":
            assert observed["http"] == [] and not observed["command"].with_suffix(".request.json").exists()
            observed["credential"].resolve.assert_not_called()
        with pytest.raises(ValueError, match="no merged artifact"):
            jobs.artifact(job["id"])


@pytest.mark.parametrize("boundary", ["reservation", "prior_usage", "usage"])
def test_mixed_provider_budget_rejection_cannot_claim_success(tmp_path, mixed_providers, boundary):
    config, factory, observed = mixed_providers
    specs = worker_specs()
    if boundary in {"reservation", "prior_usage"}:
        for account in config.accounts.values():
            account.budget = {"tokens": 19 if boundary == "reservation" else 24}
    else:
        for worker in specs:
            worker["budget"] = {"tokens": 2}
    jobs = SwarmJobs(config, backend_factory=factory)
    previous = []
    if boundary == "prior_usage":
        for account in config.accounts:
            task = jobs.tasks.create("Earlier synthetic spend", ["earlier-" + account], {"tokens": 5}, account)
            claim = jobs.tasks.claim(task["id"], "prior-owner-placeholder", lease_seconds=60)
            saved = jobs.tasks.finish(task["id"], claim["owner"], claim["attempt"], "succeeded", usage={"tokens": 5})
            previous.append(saved)
    with ChatLog(tmp_path / "proof-log") as log:
        job = jobs.create("Bound mixed protocol spend", specs, session_slug="proof-placeholder", log=log)
        result = jobs.run(job["id"], log=log)
        assert result["merged"] is None and result["artifacts"] == {}
        tasks = [jobs.tasks.get(worker["task_id"]) for worker in result["workers"]]
        assert len(tasks) == 2
        if boundary in {"reservation", "prior_usage"}:
            assert result["status"] == "blocked" and observed["selected"] == [] and observed["http"] == []
            assert all(task["status"] == "queued" and task["attempt"] == 0 for task in tasks)
            assert not observed["command"].with_suffix(".request.json").exists()
            observed["credential"].resolve.assert_not_called()
        else:
            assert result["status"] == "failed" and len(observed["selected"]) == 2
            assert all(task["status"] == "failed" and task["error_category"] == "quota" for task in tasks)
            assert all(task["usage"]["tokens"] > task["budget"]["tokens"] for task in tasks)
        card = jobs.board.get(job["id"])
        assert card["status"] == result["status"] and card["artifacts"] == []
        assert [jobs.tasks.get(task["id"]) for task in previous] == previous


@pytest.mark.parametrize("failure", ["length", "content_filter", "timeout", "cli_exit"])
def test_incomplete_mixed_provider_proof_preserves_failure_and_verified_child_only(tmp_path, mixed_providers, failure):
    config, factory, observed = mixed_providers
    if failure == "cli_exit":
        observed["command"].write_text("#!" + sys.executable + "\nraise SystemExit(7)\n")
    else:
        observed["finish"] = failure
    jobs = SwarmJobs(config, backend_factory=factory)
    with ChatLog(tmp_path / "proof-log") as log:
        job = jobs.create("Reject incomplete protocol findings", worker_specs(), session_slug="proof-placeholder", log=log)
        result = jobs.run(job["id"], log=log)
        assert result["status"] == "failed" and result["merged"] is None
        assert len(observed["selected"]) == 2 and len(observed["http"]) == 1
        tasks = jobs.tasks.list()
        assert sorted(task["status"] for task in tasks) == ["failed", "succeeded"]
        assert all(task["attempt"] == 1 for task in tasks)
        assert len(result["artifacts"]) == 1
        card = jobs.board.get(job["id"])
        assert card["status"] == "failed" and len(card["artifacts"]) == 1
        assert all(phase != "merge" for phase, _ in observed["gates"])
        with pytest.raises(ValueError, match="no merged artifact"):
            jobs.artifact(job["id"])
