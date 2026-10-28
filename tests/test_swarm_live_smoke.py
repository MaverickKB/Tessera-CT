"""Private smoke safety and acceptance contracts, using no live providers."""

import json
from pathlib import Path
import sys
from unittest.mock import Mock

import pytest

from demo import swarm_live_smoke as smoke
from fake_backend import FakeBackend
from test_status_projection_consumers import disk_snapshot
from test_swarm_job_consumers import offline_capabilities


@pytest.fixture
def private_home(tmp_path, monkeypatch):
    for name in list(smoke.os.environ):
        if name.startswith("TESSERA_"):
            monkeypatch.delenv(name)
    owner = tmp_path / "owner-placeholder"
    root = owner / ".tessera-live"
    root.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: owner)
    monkeypatch.setenv("TESSERA_LIVE_SWARM_SMOKE", "1")
    tables = ['[swarm]', 'enabled = true', 'job_workers = 2', 'max_attempts = 1',
              '[board]', 'enabled = true']
    for lane in ("alpha", "beta"):
        tables += ['[accounts.' + lane + '-account-placeholder]', 'provider_kind = "openai_chat"',
                   '[backends.' + lane + '-backend-placeholder]', 'account = "' + lane + '-account-placeholder"',
                   'base_url = "https://models.example.com/' + lane + '"',
                   'model = "' + lane + '-model-placeholder"']
    (root / "config.toml").write_text("\n".join(tables) + "\n")
    workers = []
    for lane in ("alpha", "beta"):
        workers.append({"backend": lane + "-backend-placeholder", "budget": {"tokens": 100}})
    (root / "swarm-smoke.json").write_text(json.dumps({"workers": workers}))
    return root


def echo_factory(selected):
    def factory(settings):
        selected.append((settings.name, settings.account, settings.model, settings.extra["retries"]))

        def answer(messages, tools):
            assert tools == []
            token = messages[-1]["content"].split(":", 1)[1].strip()
            return {"content": token, "usage": {"total_tokens": 2}}

        return FakeBackend([answer])

    return factory


@pytest.mark.parametrize("value", [None, "0", "true"])
def test_no_exact_opt_in_means_no_private_read_or_provider(value, monkeypatch, capsys):
    monkeypatch.delenv("TESSERA_LIVE_SWARM_SMOKE", raising=False)
    if value is not None:
        monkeypatch.setenv("TESSERA_LIVE_SWARM_SMOKE", value)
    home = Mock(side_effect=AssertionError("No private input before opt-in"))
    monkeypatch.setattr(Path, "home", home)
    factory = Mock(side_effect=AssertionError("No provider before opt-in"))
    assert smoke.main(backend_factory=factory) == 2
    assert json.loads(capsys.readouterr().out)["reason"] == "opt_in_required"
    home.assert_not_called()
    factory.assert_not_called()


def test_private_config_only_and_redacted_real_job_evidence(private_home, tmp_path, monkeypatch, capsys):
    outside = tmp_path / "outside-placeholder"
    outside.mkdir()
    (outside / "config.toml").write_text("invalid configuration must never be loaded")
    monkeypatch.setenv("TESSERA_HOME", str(outside))
    before = disk_snapshot(outside)
    selected = []
    assert smoke.main(backend_factory=echo_factory(selected)) == 0
    report = json.loads(capsys.readouterr().out)
    assert report == {
        "status": "passed", "reason": "verified", "execution": "offline_fake",
        "backend_count": 2, "account_count": 2, "configured_model_count": 2,
        "cli_worker_count": 0, "completed_count": 2, "artifact_count": 3,
        "challenge_verified": True, "board_verified": True,
    }
    assert set(selected) == {
        ("alpha-backend-placeholder", "alpha-account-placeholder", "alpha-model-placeholder", 0),
        ("beta-backend-placeholder", "beta-account-placeholder", "beta-model-placeholder", 0),
    }
    assert json.loads((private_home / "swarm-smoke-report.json").read_text()) == report
    assert (private_home / "swarm-smoke-report.json").stat().st_mode & 0o777 == 0o600
    serialized = json.dumps(report)
    for private in ("alpha", "beta", "example.com", str(private_home), "smoke-token"):
        assert private not in serialized
    assert disk_snapshot(outside) == before
    tasks = list((private_home / "swarm-smoke-runs").glob("*/tasks.jsonl"))
    assert len(tasks) == 1
    assert tasks[0].parent.stat().st_mode & 0o777 == 0o700
    assert not (private_home / "tasks.jsonl").exists()


@pytest.mark.parametrize("change", ["empty", "one_backend", "one_account", "selected_same_account"])
def test_insufficient_lanes_fail_clearly_before_storage_or_provider(private_home, change, capsys):
    path = private_home / "config.toml"
    text = path.read_text()
    if change == "empty":
        text = '[swarm]\nenabled = true\n[board]\nenabled = true\n'
    elif change == "one_backend":
        text = text[:text.index('[backends.beta-backend-placeholder]')]
    else:
        text = text.replace('account = "beta-account-placeholder"', 'account = "alpha-account-placeholder"')
        if change == "one_account":
            text = text.replace('[accounts.beta-account-placeholder]\nprovider_kind = "openai_chat"\n', '')
    path.write_text(text)
    before = disk_snapshot(private_home)
    factory = Mock(side_effect=AssertionError("No provider for insufficient lanes"))
    assert smoke.main(backend_factory=factory) == 2
    assert json.loads(capsys.readouterr().out)["reason"] == "at_least_two_backends_and_accounts_required"
    factory.assert_not_called()
    assert disk_snapshot(private_home) == before


@pytest.mark.parametrize("name", ["TESSERA_LIVE_SWARM_SPEC", "TESSERA_LIVE_SWARM_REPORT", "TESSERA_LIVE_SWARM_RUNS"])
def test_external_paths_reject_before_any_execution(private_home, tmp_path, name, monkeypatch, capsys):
    outside = tmp_path / "external-private-placeholder"
    outside.write_text("External fixture must remain untouched")
    monkeypatch.setenv(name, str(outside))
    before = disk_snapshot(private_home)
    factory = Mock(side_effect=AssertionError("No provider for an outside path"))
    assert smoke.main(backend_factory=factory) == 2
    assert json.loads(capsys.readouterr().out)["reason"] == "private_path_required"
    assert outside.read_text() == "External fixture must remain untouched"
    assert disk_snapshot(private_home) == before
    factory.assert_not_called()


def test_private_config_symlink_is_not_followed(private_home, tmp_path, capsys):
    outside = tmp_path / "external-config-placeholder"
    outside.write_text((private_home / "config.toml").read_text())
    (private_home / "config.toml").unlink()
    (private_home / "config.toml").symlink_to(outside)
    factory = Mock(side_effect=AssertionError("No provider for a redirected config"))
    assert smoke.main(backend_factory=factory) == 2
    assert json.loads(capsys.readouterr().out)["reason"] == "private_path_required"
    factory.assert_not_called()


@pytest.mark.parametrize("answer", ["wrong_nonce", "incomplete", "error"])
def test_provider_failure_or_wrong_challenge_cannot_be_live_acceptance(private_home, answer, capsys):
    def factory(settings):
        if answer == "error":
            raise RuntimeError("private-provider-error-placeholder https://secret.example.com/path")
        if answer == "incomplete":
            return FakeBackend([{"content": "partial-private-placeholder", "finish_reason": "length"}])
        return FakeBackend(["wrong-private-response-placeholder"])

    assert smoke.main(backend_factory=factory) == 2
    serialized = capsys.readouterr().out
    report = json.loads(serialized)
    assert report["status"] == "failed" and report["execution"] == "offline_fake"
    assert report["reason"] == ("challenge_mismatch" if answer == "wrong_nonce" else "job_incomplete")
    for private in ("private-provider", "secret.example.com", "partial-private", "wrong-private", str(private_home)):
        assert private not in serialized
    assert json.loads((private_home / "swarm-smoke-report.json").read_text()) == report


@pytest.mark.parametrize("target", ["config.toml", "swarm-smoke.json", "."])
def test_output_cannot_overwrite_config_spec_or_private_root(private_home, target, monkeypatch, capsys):
    monkeypatch.setenv("TESSERA_LIVE_SWARM_REPORT", target)
    before = disk_snapshot(private_home)
    factory = Mock(side_effect=AssertionError("No provider for colliding output"))
    assert smoke.main(backend_factory=factory) == 2
    assert json.loads(capsys.readouterr().out)["reason"] == "input_output_overlap"
    assert disk_snapshot(private_home) == before
    factory.assert_not_called()


def test_configured_storage_cannot_escape_private_home(private_home, tmp_path, monkeypatch, capsys):
    outside = tmp_path / "external-tasks-placeholder"
    path = private_home / "config.toml"
    path.write_text(path.read_text().replace('[swarm]', '[swarm]\ntasks_file = ' + json.dumps(str(outside))))
    factory = Mock(side_effect=AssertionError("No provider for external task storage"))
    before = disk_snapshot(private_home)
    assert smoke.main(backend_factory=factory) == 2
    assert json.loads(capsys.readouterr().out)["reason"] == "private_path_required"
    assert not outside.exists() and disk_snapshot(private_home) == before
    factory.assert_not_called()


def test_api_smoke_never_searches_a_keychain_when_env_is_missing(private_home, capsys):
    path = private_home / "config.toml"
    text = path.read_text().replace('[accounts.alpha-account-placeholder]',
        '[accounts.alpha-account-placeholder]\ncredential_ref = "login-placeholder"')
    text += ('\n[credentials.login-placeholder]\nkeychain_service = "service-placeholder"\n'
             'keychain_account = "account-placeholder"\n')
    path.write_text(text)
    before = disk_snapshot(private_home)
    factory = Mock(side_effect=AssertionError("No provider without an environment credential"))
    assert smoke.main(backend_factory=factory) == 2
    assert json.loads(capsys.readouterr().out)["reason"] == "environment_credential_required"
    assert disk_snapshot(private_home) == before
    factory.assert_not_called()


@pytest.mark.parametrize("inactive", ["swarm", "board", "budget", "oversized"])
def test_smoke_preflight_is_inert_for_inactive_or_unbounded_work(private_home, inactive, monkeypatch, capsys):
    path = private_home / "config.toml"
    text = path.read_text()
    reason = "swarm_and_board_must_be_enabled"
    if inactive == "swarm":
        text = text.replace('[swarm]\nenabled = true', '[swarm]\nenabled = false')
    elif inactive == "board":
        text = text.replace('[board]\nenabled = true', '[board]\nenabled = false')
    elif inactive == "budget":
        spec = private_home / "swarm-smoke.json"
        plan = json.loads(spec.read_text())
        plan["workers"][0]["budget"]["tokens"] = 0
        spec.write_text(json.dumps(plan))
        reason = "positive_worker_budget_required"
    else:
        monkeypatch.setenv("TESSERA_LIVE_SWARM_INPUT_BYTES", "1")
        reason = "private_input_too_large"
    path.write_text(text)
    before = disk_snapshot(private_home)
    factory = Mock(side_effect=AssertionError("No provider for inactive or unbounded smoke"))
    assert smoke.main(backend_factory=factory) == 2
    assert json.loads(capsys.readouterr().out)["reason"] == reason
    assert disk_snapshot(private_home) == before
    factory.assert_not_called()


def test_relative_private_cli_is_normalized_and_proves_the_same_challenge(private_home, offline_capabilities, capsys):
    from tessera.backend import Backend

    command = private_home / "bin" / "claude"
    command.parent.mkdir()
    source = '''import json
import sys
messages = json.loads(sys.stdin.read().split("\\n", 1)[1])
token = messages[-1]["content"].split(":", 1)[1].strip()
print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                  "result": token, "usage": {"output_tokens": 2}}))
'''
    command.write_text("#!" + sys.executable + "\n" + source)
    command.chmod(0o700)
    offline_capabilities.add(str(command))
    path = private_home / "config.toml"
    text = path.read_text().replace('[accounts.beta-account-placeholder]\nprovider_kind = "openai_chat"',
        '[accounts.beta-account-placeholder]\nprovider_kind = "cli_worker"')
    text += '\n[backends.beta-backend-placeholder.extra.cli]\nvendor = "claude"\ncommand = "bin/claude"\ncwd = "."\n'
    path.write_text(text)
    selected = []

    def factory(settings):
        if settings.provider_kind == "cli_worker":
            assert settings.extra["cli"]["command"] == str(command)
            assert settings.extra["cli"]["cwd"] == str(private_home)
            return Backend(settings.name, settings.base_url, settings.model, settings.api_key_env, settings.extra)
        return echo_factory(selected)(settings)

    assert smoke.main(backend_factory=factory) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "passed" and report["execution"] == "offline_fake"
    assert report["cli_worker_count"] == 1 and report["completed_count"] == 2
    assert report["artifact_count"] == 3 and report["challenge_verified"] is True


@pytest.mark.parametrize("field", ["checks", "gate"])
def test_truthy_unverified_gate_metadata_cannot_pass_acceptance(private_home, field, monkeypatch, capsys):
    original = smoke.SwarmJobs.artifact

    def malformed(jobs, job_id):
        artifact = original(jobs, job_id)
        if field == "checks":
            artifact["checks"]["artifact_hashes_valid"] = "false"
        else:
            artifact["gate"]["allowed"] = "false"
        return artifact

    monkeypatch.setattr(smoke.SwarmJobs, "artifact", malformed)
    assert smoke.main(backend_factory=echo_factory([])) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "failed" and report["reason"] == "evidence_incomplete"


def test_board_counts_cannot_substitute_for_the_exact_merged_artifact(private_home, monkeypatch, capsys):
    from tessera.board import BoardStore

    original = BoardStore.get

    def unrelated_reference(store, card_id):
        card = original(store, card_id)
        if card["status"] == "succeeded" and len(card["artifacts"]) == 3:
            card["artifacts"][-1] = "opaque-unrelated-artifact-placeholder"
        return card

    monkeypatch.setattr(BoardStore, "get", unrelated_reference)
    assert smoke.main(backend_factory=echo_factory([])) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["reason"] == "evidence_incomplete" and report["status"] == "failed"


@pytest.mark.parametrize("wrapper, accepted", [
    ("complete", True), ("nested", True), ("sequential", True),
    ("thinking_only", False), ("unfinished", False), ("unmatched_close", False),
])
def test_live_observed_reasoning_wrapper_verifies_only_final_nonce_and_preserves_raw_artifacts(
        private_home, wrapper, accepted, capsys):
    """Mirror the sanitized integrations report; this execution remains fake."""
    raw = {}

    def factory(settings):
        def reply(messages, tools):
            token = messages[-1]["content"].split(":", 1)[1].strip()
            if wrapper == "complete":
                text = "<think>private-reasoning-placeholder</think>\n" + token
            elif wrapper == "nested":
                text = "<think>outer<think>private-reasoning-placeholder</think>end</think>\n" + token
            elif wrapper == "sequential":
                text = "<think>first</think><think>private-reasoning-placeholder</think>\n" + token
            elif wrapper == "thinking_only":
                text = "<think>" + token + "</think>wrong-visible-answer-placeholder"
            elif wrapper == "unfinished":
                text = "<think>" + token
            else:
                text = "</think>" + token
            raw[settings.name] = text
            return {"content": text, "finish_reason": "stop", "usage": {"total_tokens": 2}}

        return FakeBackend([reply])

    assert smoke.main(backend_factory=factory) == (0 if accepted else 2)
    serialized = capsys.readouterr().out
    report = json.loads(serialized)
    assert report["execution"] == "offline_fake"
    assert report["reason"] == ("verified" if accepted else "challenge_mismatch")
    assert "private-reasoning-placeholder" not in serialized
    merged_paths = list((private_home / "swarm-smoke-runs").glob("*/jobs/*/merged-*.json"))
    assert len(merged_paths) == 1
    merged = json.loads(merged_paths[0].read_text())
    assert [artifact["content"] for artifact in merged["artifacts"]] == [
        raw["alpha-backend-placeholder"], raw["beta-backend-placeholder"]]
