"""Independent explicit swarm-job consumers with fake backends and synthetic CLI."""

import json
import socket
import subprocess
import sys
from unittest.mock import Mock

import pytest

from cli_fixtures import synthetic_codex_auth
from fake_backend import FakeBackend
from tessera.backend import Backend
from tessera.config import AccountConfig, BackendConfig, Config
from tessera.credentials import CredentialResolver
from tessera.jev import JudgeKit
from test_status_projection_consumers import disk_snapshot


@pytest.fixture(autouse=True)
def offline_capabilities(monkeypatch):
    allowed_executables = set()
    tripwires = []
    for owner, name in ((CredentialResolver, "resolve"), (JudgeKit, "_evaluate_batch"),
                        (socket.socket, "connect")):
        tripwire = Mock(side_effect=AssertionError("Swarm consumers cannot contact live capabilities"))
        monkeypatch.setattr(owner, name, tripwire)
        tripwires.append(tripwire)
    for target in ("socket.create_connection", "urllib.request.urlopen", "urllib.request.OpenerDirector.open"):
        tripwire = Mock(side_effect=AssertionError("Swarm consumers cannot use network transports"))
        monkeypatch.setattr(target, tripwire)
        tripwires.append(tripwire)
    original_popen = subprocess.Popen
    forbidden_process = Mock(side_effect=AssertionError("Only the generated synthetic CLI may spawn"))
    tripwires.append(forbidden_process)

    def popen(argv, *args, **kwargs):
        if not isinstance(argv, (list, tuple)) or not argv or argv[0] not in allowed_executables:
            return forbidden_process(argv, *args, **kwargs)
        assert kwargs.get("shell", False) is False
        return original_popen(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", popen)
    yield allowed_executables
    for tripwire in tripwires:
        tripwire.assert_not_called()


def two_lane_config(tmp_path):
    config = Config(home=tmp_path, cwd=tmp_path)
    for lane in ("alpha", "beta"):
        account = lane + "-account-placeholder"
        backend = lane + "-backend-placeholder"
        config.accounts[account] = AccountConfig(name=account, provider_kind="openai_chat")
        config.backends[backend] = BackendConfig(name=backend, account=account,
            base_url="https://models.example.com/" + lane, model=lane + "-model-placeholder")
    config.roles = {"main": "alpha-backend-placeholder"}
    return config


def synthetic_cli(tmp_path, offline_capabilities):
    path = tmp_path / "synthetic-cli-placeholder" / "claude"
    path.parent.mkdir()
    source = '''import json
from pathlib import Path
import sys
prompt = sys.stdin.read()
Path(__file__).with_suffix(".request.json").write_text(json.dumps({"prompt": prompt}))
print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                  "result": "Beta fixture inspected: beta-evidence-placeholder",
                  "usage": {"input_tokens": 2, "output_tokens": 3}}))
'''
    path.write_text("#!" + sys.executable + "\n" + source)
    path.chmod(0o700)
    offline_capabilities.add(str(path))
    return path


def enabled_config(tmp_path):
    config = two_lane_config(tmp_path)
    config.swarm.enabled = True
    config.swarm.max_attempts = 1
    config.board.enabled = True
    return config.validate()


def worker_specs():
    return [{"backend": lane + "-backend-placeholder", "objective": "Inspect " + lane + " fixture",
             "scope": [lane + "-scope-placeholder"], "budget": {"tokens": 20}}
            for lane in ("alpha", "beta")]


def test_two_configured_lanes_fan_out_and_merge_real_artifacts_with_durable_reopen(tmp_path):
    import threading
    from tessera.jobs import SwarmJobs
    from tessera.log import ChatLog

    config = enabled_config(tmp_path)
    barrier = threading.Barrier(2)
    selected = []
    backends = []

    def factory(settings):
        selected.append((settings.name, settings.account))

        def reply(messages, tools):
            assert tools == []
            barrier.wait(timeout=3)
            return {"content": settings.name + " finding", "usage": {"total_tokens": 3}}

        backend = FakeBackend([reply])
        backends.append(backend)
        return backend

    jobs = SwarmJobs(config, backend_factory=factory)
    with ChatLog(tmp_path / "consumer-log") as log:
        job = jobs.create("Join independent fixture findings", worker_specs(), session_slug="subject-placeholder", log=log)
        result = jobs.run(job["id"], log=log)
        assert result["status"] == "succeeded"
        assert set(selected) == {("alpha-backend-placeholder", "alpha-account-placeholder"),
                                 ("beta-backend-placeholder", "beta-account-placeholder")}
        assert len(backends) == 2 and all(len(backend.calls) == 1 for backend in backends)
        tasks = jobs.tasks.list()
        assert len(tasks) == 2 and all(row["status"] == "succeeded" and row["attempt"] == 1 for row in tasks)
        assert {row["account"] for row in tasks} == {"alpha-account-placeholder", "beta-account-placeholder"}
        merged = jobs.artifact(job["id"])
        assert [row["content"] for row in merged["artifacts"]] == [
            "alpha-backend-placeholder finding", "beta-backend-placeholder finding"]
        assert merged["checks"] and all(value is True for value in merged["checks"].values())
        assert merged["gate"]["allowed"] is True
        card = jobs.board.get(job["id"])
        assert card["status"] == "succeeded" and len(card["artifacts"]) == 3
        assert {row["result_ref"] for row in tasks} <= set(card["artifacts"])
        assert card["session_slug"] == "subject-placeholder" and card["log_end"] < log.count()
        before_count = log.count()
        denied = Mock(side_effect=AssertionError("Completed jobs cannot reconstruct providers"))
        reopened = SwarmJobs(config, backend_factory=denied)
        assert reopened.run(job["id"], log=log)["status"] == "succeeded"
        assert reopened.artifact(job["id"]) == merged
        assert reopened.tasks.list() == tasks and reopened.board.get(job["id"]) == card
        assert log.count() == before_count
        denied.assert_not_called()


@pytest.mark.parametrize("inactive", ["swarm", "backends", "accounts"])
def test_inactive_job_cannot_recover_existing_debt_create_artifacts_or_construct_providers(tmp_path, inactive):
    from pathlib import Path
    from tessera.jobs import SwarmJobs
    from tessera.tasks import TaskStore

    config = enabled_config(tmp_path)
    path = Path(config.swarm.tasks_file)
    if not path.is_absolute():
        path = config.home / path
    store = TaskStore(path, enabled=True, clock=lambda: 100.0)
    debt = store.create("Unrelated fixture debt", ["foreign-scope-placeholder"], {}, "foreign-account-placeholder")
    store.claim(debt["id"], "expired-owner-placeholder", lease_seconds=1)
    if inactive == "swarm":
        config.swarm.enabled = False
    elif inactive == "backends":
        config.backends.clear()
    else:
        config.accounts.clear()
    before = disk_snapshot(tmp_path)
    factory = Mock(side_effect=AssertionError("Inactive jobs cannot construct providers"))
    jobs = SwarmJobs(config, backend_factory=factory)
    with pytest.raises(ValueError):
        jobs.create("Join independent fixture findings", worker_specs(), session_slug="subject-placeholder", log=None)
    factory.assert_not_called()
    assert store.get(debt["id"])["status"] == "running"
    assert disk_snapshot(tmp_path) == before


def test_mixed_fake_backend_and_generated_cli_use_distinct_configured_accounts(tmp_path, offline_capabilities):
    from tessera.jobs import SwarmJobs
    from tessera.log import ChatLog

    config = enabled_config(tmp_path)
    command = synthetic_cli(tmp_path, offline_capabilities)
    account = config.accounts["beta-account-placeholder"]
    account.provider_kind = "cli_worker"
    config.backends["beta-backend-placeholder"] = BackendConfig(name="beta-backend-placeholder",
        account=account.name, extra={"cli": {"vendor": "claude",
        "command": str(command), "cwd": str(tmp_path), "timeout": 3}})
    fake = FakeBackend(["Alpha fixture inspected: alpha-evidence-placeholder"])
    selected = []

    def factory(settings):
        selected.append((settings.name, settings.account))
        if settings.provider_kind == "cli_worker":
            return Backend(settings.name, settings.base_url, settings.model, settings.api_key_env, settings.extra)
        return fake

    jobs = SwarmJobs(config, backend_factory=factory)
    with ChatLog(tmp_path / "consumer-log") as log:
        job = jobs.create("Join independent fixture findings", worker_specs(), session_slug="subject-placeholder", log=log)
        assert jobs.run(job["id"], log=log)["status"] == "succeeded"
        assert set(selected) == {("alpha-backend-placeholder", "alpha-account-placeholder"),
                                 ("beta-backend-placeholder", "beta-account-placeholder")}
        assert len(fake.calls) == 1
        merged = jobs.artifact(job["id"])
        assert [row["content"] for row in merged["artifacts"]] == [
            "Alpha fixture inspected: alpha-evidence-placeholder", "Beta fixture inspected: beta-evidence-placeholder"]
        assert merged["gate"]["allowed"] is True
        recorded = json.loads(command.with_suffix(".request.json").read_text())
        assert "Inspect beta fixture" in recorded["prompt"]
        assert jobs.board.get(job["id"])["status"] == "succeeded"


@pytest.mark.parametrize("finish_reason", ["length", "content_filter"])
def test_unsuccessful_worker_final_blocks_successful_merge_and_board_artifact_claim(tmp_path, finish_reason):
    from tessera.jobs import SwarmJobs
    from tessera.log import ChatLog

    config = enabled_config(tmp_path)
    selected = []

    def factory(settings):
        selected.append(settings.name)
        if settings.name == "beta-backend-placeholder":
            return FakeBackend([{"content": "Incomplete beta fixture", "finish_reason": finish_reason}])
        return FakeBackend(["Alpha fixture complete"])

    jobs = SwarmJobs(config, backend_factory=factory)
    with ChatLog(tmp_path / "consumer-log") as log:
        job = jobs.create("Join independent fixture findings", worker_specs(), session_slug="subject-placeholder", log=log)
        result = jobs.run(job["id"], log=log)
        assert result["status"] == "failed" and result["merged"] is None
        tasks = jobs.tasks.list()
        beta = [row for row in tasks if row["account"] == "beta-account-placeholder"]
        assert len(beta) == 1 and beta[0]["status"] == "failed" and beta[0]["result_ref"] == ""
        assert jobs.board.get(job["id"])["status"] == "failed"
        assert sorted(selected) == ["alpha-backend-placeholder", "beta-backend-placeholder"]


def test_session_entrypoints_bind_board_evidence_and_keep_unloaded_log_zoomable(tmp_path):
    from tessera.log import ChatLog
    from tessera.sessions import SessionManager
    from tessera.tree import SummaryTree
    from tessera.view import View

    config = enabled_config(tmp_path)
    chat_factory = Mock(side_effect=AssertionError("An explicit job cannot load a chat backend"))
    selected = []

    def factory(settings):
        selected.append(settings.name)
        return FakeBackend([settings.name + " finding"])

    with SessionManager(config, backend_factory=chat_factory, swarm_backend_factory=factory) as manager:
        slug = manager.create("job subject fixture")["slug"]
        job = manager.create_swarm_job(slug, "Join independent fixture findings", worker_specs())
        assert selected == [] and manager._runtimes == {}
        assert manager.run_swarm_job(job["id"])["status"] == "succeeded"
        assert sorted(selected) == ["alpha-backend-placeholder", "beta-backend-placeholder"]
        assert manager._runtimes == {}
        root = manager.path(slug)
        with ChatLog(root / "log") as log:
            rows = list(log.iter())
            assert [row.meta.get("status") for row in rows] == ["queued", "running", "succeeded"]
            assert all(row.kind == "work" and row.meta["swarm_job_id"] == job["id"] for row in rows)
            view = View(root, SummaryTree(root, log))
            assert view.zoom(0, 1) == "Swarm job queued"
            assert [row["start"] for row in view.lines()] == [0, 1, 2]
        assert manager.list_sessions()[0]["messages"] == 3
        snapshot = manager.board_status()
        assert snapshot["cards"][0]["status"] == "succeeded"
        assert manager.board_link(1, snapshot["revision"]) == {"session_slug": slug, "log_start": 0, "log_end": 2}
    chat_factory.assert_not_called()


def test_cli_create_show_and_run_use_real_session_job_paths_without_live_provider(tmp_path, monkeypatch, capsys):
    from tessera import cli, sessions

    config = enabled_config(tmp_path)
    tables = ['cwd = ' + json.dumps(str(tmp_path)), '[swarm]', 'enabled = true', 'job_workers = 2',
              'max_attempts = 1', '[board]', 'enabled = true']
    for name in config.accounts:
        tables += ['[accounts.' + json.dumps(name) + ']', 'provider_kind = "openai_chat"']
    for name, settings in config.backends.items():
        tables += ['[backends.' + json.dumps(name) + ']', 'account = ' + json.dumps(settings.account),
                   'base_url = ' + json.dumps(settings.base_url), 'model = ' + json.dumps(settings.model)]
    (tmp_path / "config.toml").write_text("\n".join(tables) + "\n")
    original_manager = sessions.SessionManager
    selected = []
    chat_factory = Mock(side_effect=AssertionError("Swarm CLI cannot start a chat provider"))

    def backend(settings):
        selected.append(settings.name)
        return FakeBackend([settings.name + " finding"])

    def manager(settings):
        return original_manager(settings, backend_factory=chat_factory, swarm_backend_factory=backend)

    with manager(config) as saved:
        slug = saved.create("CLI job fixture")["slug"]
        journal = saved.path(slug) / "turns.jsonl"
    journal.write_text(json.dumps({"op": "enqueue", "turn": {
        "id": "unrelated-turn-placeholder", "text": "Preserve unrelated fixture request", "state": "queued"}}) + "\n")
    saved_turns = journal.read_bytes()
    spec = tmp_path / "spec-placeholder.json"
    spec.write_text(json.dumps({"objective": "Join independent fixture findings", "workers": worker_specs()}))
    monkeypatch.setattr(sessions, "SessionManager", manager)
    prefix = ["--home", str(tmp_path)]
    assert cli.main(prefix + ["swarm-create", str(spec), "--session", slug]) == 0
    created = json.loads(capsys.readouterr().out)
    assert created["status"] == "queued" and created["worker_count"] == 2
    assert created["artifact_count"] == 0 and created["merged"] is False and selected == []
    job_id = created["job_id"]
    before = disk_snapshot(tmp_path)
    assert cli.main(prefix + ["swarm-show", job_id]) == 0
    assert json.loads(capsys.readouterr().out) == created
    assert selected == [] and disk_snapshot(tmp_path) == before
    assert cli.main(prefix + ["swarm-run", job_id]) == 0
    completed = json.loads(capsys.readouterr().out)
    assert completed == {"job_id": job_id, "status": "succeeded", "worker_count": 2, "artifact_count": 2, "merged": True}
    assert sorted(selected) == ["alpha-backend-placeholder", "beta-backend-placeholder"]
    before = disk_snapshot(tmp_path)
    assert cli.main(prefix + ["swarm-show", job_id]) == 0
    assert json.loads(capsys.readouterr().out) == completed
    assert disk_snapshot(tmp_path) == before
    assert journal.read_bytes() == saved_turns
    chat_factory.assert_not_called()


def test_merge_gate_veto_retains_child_evidence_without_publishing_success(tmp_path, monkeypatch):
    from tessera.jobs import SwarmJobs
    from tessera.log import ChatLog

    config = enabled_config(tmp_path)
    kit = Mock()
    kit.swarm_gates.side_effect = lambda objective, **options: {
        "allowed": options["phase"] != "merge", "source": "fixture"}
    monkeypatch.setattr(JudgeKit, "from_config", Mock(return_value=kit))
    jobs = SwarmJobs(config, backend_factory=lambda settings: FakeBackend([settings.name + " finding"]))
    with ChatLog(tmp_path / "consumer-log") as log:
        job = jobs.create("Join independent fixture findings", worker_specs(), session_slug="subject-placeholder", log=log)
        result = jobs.run(job["id"], log=log)
        assert result["status"] == "blocked" and result["merged"] is None
        assert len(jobs.tasks.list(status="succeeded")) == 2
        card = jobs.board.get(job["id"])
        assert card["status"] == "blocked" and len(card["artifacts"]) == 2
        merges = [entry.kwargs for entry in kit.swarm_gates.call_args_list if entry.kwargs["phase"] == "merge"]
        assert len(merges) == 1
        assert [row["content"] for row in merges[0]["artifacts"]] == [
            "alpha-backend-placeholder finding", "beta-backend-placeholder finding"]


@pytest.mark.parametrize("vendor", ["claude", "codex"])
def test_three_lanes_preserve_distinct_models_accounts_and_cli_protocol(tmp_path, offline_capabilities, vendor):
    import threading
    from tessera.jobs import SwarmJobs
    from tessera.log import ChatLog

    config = enabled_config(tmp_path)
    config.accounts["beta-account-placeholder"].provider_kind = "openai_responses"
    config.accounts["gamma-account-placeholder"] = AccountConfig(
        name="gamma-account-placeholder", provider_kind="cli_worker")
    command = tmp_path / "synthetic-protocol-placeholder" / vendor
    command.parent.mkdir()
    source = '''import json
from pathlib import Path
import sys
prompt = sys.stdin.read()
Path(__file__).with_suffix(".request.json").write_text(json.dumps({"prompt": prompt, "argv": sys.argv[1:]}))
if Path(__file__).name == "claude":
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                      "result": "Gamma CLI evidence placeholder", "usage": {"output_tokens": 2}}))
else:
    print(json.dumps({"type": "turn.started"}))
    print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "Gamma CLI evidence placeholder"}}))
    print(json.dumps({"type": "turn.completed", "usage": {"output_tokens": 2}}))
'''
    command.write_text("#!" + sys.executable + "\n" + source)
    command.chmod(0o700)
    offline_capabilities.add(str(command))
    arguments = ["--print", "--output-format", "json"]
    if vendor == "codex":
        arguments = ["exec", "--json", "--sandbox", "read-only", "-"]
    arguments += ["--model", "gamma-model-placeholder"]
    config.backends["gamma-backend-placeholder"] = BackendConfig(
        name="gamma-backend-placeholder", account="gamma-account-placeholder", model="gamma-model-placeholder",
        extra={"cli": {"vendor": vendor, "command": str(command), "cwd": str(tmp_path), "args": arguments,
                       **({"auth_path": synthetic_codex_auth(tmp_path)} if vendor == "codex" else {})}})
    config.validate()
    workers = worker_specs() + [{"backend": "gamma-backend-placeholder", "objective": "Inspect gamma fixture",
                                 "scope": ["gamma-scope-placeholder"], "budget": {"tokens": 20}}]
    barrier = threading.Barrier(3)
    selected = []
    instances = []

    def factory(settings):
        selected.append((settings.name, settings.account, settings.model, settings.provider_kind))
        assert settings.extra["retries"] == 0
        cli = None
        if settings.provider_kind == "cli_worker":
            cli = Backend(settings.name, settings.base_url, settings.model, settings.api_key_env, settings.extra)

        def reply(messages, tools):
            assert tools == []
            barrier.wait(timeout=3)
            if cli is not None:
                return cli.chat(messages, tools=tools)
            return {"content": settings.model + " evidence", "usage": {"total_tokens": 2}}

        backend = FakeBackend([reply])
        instances.append(backend)
        return backend

    jobs = SwarmJobs(config, backend_factory=factory)
    with ChatLog(tmp_path / "model-routing-log") as log:
        job = jobs.create("Join three independent protocol findings", workers, session_slug="model-proof-placeholder", log=log)
        assert jobs.run(job["id"], log=log)["status"] == "succeeded"
        assert set(selected) == {
            ("alpha-backend-placeholder", "alpha-account-placeholder", "alpha-model-placeholder", "openai_chat"),
            ("beta-backend-placeholder", "beta-account-placeholder", "beta-model-placeholder", "openai_responses"),
            ("gamma-backend-placeholder", "gamma-account-placeholder", "gamma-model-placeholder", "cli_worker"),
        }
        assert len(instances) == 3 and all(len(backend.calls) == 1 for backend in instances)
        merged = jobs.artifact(job["id"])
        assert [row["content"] for row in merged["artifacts"]] == [
            "alpha-model-placeholder evidence", "beta-model-placeholder evidence", "Gamma CLI evidence placeholder"]
        assert merged["gate"]["allowed"] is True and all(merged["checks"].values())
        tasks = jobs.tasks.list()
        assert len(tasks) == 3 and all(task["status"] == "succeeded" and task["attempt"] == 1 for task in tasks)
        card = jobs.board.get(job["id"])
        assert card["status"] == "succeeded" and len(card["artifacts"]) == 4
        assert {task["result_ref"] for task in tasks} <= set(card["artifacts"])
    recorded = json.loads(command.with_suffix(".request.json").read_text())
    assert "Inspect gamma fixture" in recorded["prompt"]
    assert recorded["argv"] == arguments
