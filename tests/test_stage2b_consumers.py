"""Consumer-facing checks for passive status and offline command behavior."""

import json
import threading

import pytest

from cli_fixtures import synthetic_codex_auth
from tessera.backend import Backend
from tessera.cli import main
from tessera.config import load_config
from tessera.credentials import CredentialResolver
from tessera.mcp_client import McpClient
from tessera.server import create_server
from test_server import call


@pytest.fixture
def no_external_work(monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("Passive operations must not resolve, probe, construct backends, or spawn")

    monkeypatch.setattr(CredentialResolver, "resolve", unexpected)
    monkeypatch.setattr(Backend, "__init__", unexpected)
    monkeypatch.setattr(McpClient, "_post", unexpected)
    monkeypatch.setattr("urllib.request.OpenerDirector.open", unexpected)
    monkeypatch.setattr("subprocess.Popen", unexpected)


@pytest.mark.parametrize("credential_present", [False, True])
def test_http_status_is_passive_even_with_configured_memory_and_credential_sources(
        tmp_path, monkeypatch, no_external_work, credential_present):
    monkeypatch.delenv("EXAMPLE_PASSIVE_STATUS_KEY", raising=False)
    if credential_present:
        monkeypatch.setenv("EXAMPLE_PASSIVE_STATUS_KEY", "status-secret-placeholder")
    (tmp_path / "config.toml").write_text('''
[credentials.reference-placeholder]
env = "EXAMPLE_PASSIVE_STATUS_KEY"
keychain_service = "service-placeholder"
keychain_account = "keychain-account-placeholder"
[accounts.account-placeholder]
credential_ref = "reference-placeholder"
[backends.public-backend]
base_url = "https://models.example.com/v1"
model = "model-placeholder"
account = "account-placeholder"
[aethermind]
endpoints = ["https://memory.example.com/mcp"]
project = "/project-placeholder"
''')
    config = load_config(tmp_path)
    config.server.port = 0
    server = create_server(config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        reports = []
        for _ in range(3):
            status, report = call(server, "GET", "/api/status")
            assert status == 200 and report["sessions"] == {}
            assert "enabled" not in report["aethermind"]
            provider = report["provider_status"]
            assert len(provider["accounts"]) == len(provider["backends"]) == 1
            assert provider["backends"][0]["id"] == "public-backend"
            for item in provider["accounts"] + provider["backends"]:
                assert item["configured"] is True
                assert item["health"] == "not_checked" and item["credential"] == "not_reported"
            reports.append(provider)
            serialized = json.dumps(report)
            for private in ("reference-placeholder", "account-placeholder", "model-placeholder",
                            "models.example.com", "memory.example.com", "service-placeholder",
                            "EXAMPLE_PASSIVE_STATUS_KEY", "status-secret-placeholder", "/project-placeholder"):
                assert private not in serialized
        assert reports[0] == reports[1] == reports[2]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_empty_cli_status_is_inert_and_missing_backend_fails_without_external_work(
        tmp_path, no_external_work, capsys):
    assert main(["--home", str(tmp_path), "status"]) == 0
    output = capsys.readouterr()
    status = json.loads(output.out)
    assert status["backends"] == [] and status["messages"] == status["write_debt"] == 0
    assert output.err == ""
    assert main(["--home", str(tmp_path), "run", "Synthetic request"]) == 1
    output = capsys.readouterr()
    assert "backend" in output.err.lower() and output.out == ""


@pytest.mark.parametrize("options", [{}, {"vendor": "codex"}, {"vendor": "claude"},
                                     {"vendor": "codex", "command": "codex"}])
def test_incomplete_cli_configuration_never_searches_path_or_spawns(
        options, monkeypatch, no_external_work):
    from tessera.cli_worker import CliWorker, WorkerError

    def unexpected(*args, **kwargs):
        raise AssertionError("Unconfigured CLI workers must not search for executables")

    def no_grant(*args, **kwargs):
        raise AssertionError("Unconfigured CLI workers must not read a Codex auth file")

    monkeypatch.setattr("tessera.cli_worker.shutil.which", unexpected)
    monkeypatch.setattr("tessera.codex_grant.grant_state", no_grant)
    with pytest.raises(WorkerError, match="configured") as error:
        CliWorker(options).chat([{"role": "user", "content": "Synthetic request"}])
    assert error.value.category == "configuration"


def test_cli_worker_rejects_harness_tool_schema_before_discovery(tmp_path, monkeypatch, no_external_work):
    from tessera.cli_worker import CliWorker, WorkerError

    def unexpected(*args, **kwargs):
        raise AssertionError("Rejected tool schemas must not cause CLI discovery")

    def no_grant(*args, **kwargs):
        raise AssertionError("Rejected tool schemas must not read a Codex auth file")

    monkeypatch.setattr("tessera.cli_worker.shutil.which", unexpected)
    monkeypatch.setattr("tessera.codex_grant.grant_state", no_grant)
    worker = CliWorker({"vendor": "codex", "command": "codex", "cwd": str(tmp_path)})
    with pytest.raises(WorkerError, match="tool schema") as error:
        worker.chat([], tools=[{"type": "function"}])
    assert error.value.category == "configuration"


@pytest.mark.parametrize("vendor, result", [
    ("codex", {"type": "item.completed", "item": {"type": "agent_message", "text": "Partial private placeholder"}}),
    ("claude", {"type": "result", "subtype": "success", "result": "Partial private placeholder"}),
])
def test_cli_text_without_a_completed_protocol_result_cannot_succeed(tmp_path, monkeypatch, vendor, result):
    from tessera.backend import BackendError
    from test_cli_worker import fake_cli

    def unexpected(*args, **kwargs):
        raise AssertionError("Subscription CLI execution must not resolve API credentials")

    monkeypatch.setattr(CredentialResolver, "resolve", unexpected)
    command = fake_cli(tmp_path, vendor, "print(" + repr(json.dumps(result)) + ")\n")
    cli = {"vendor": vendor, "command": command, "cwd": str(tmp_path), "timeout": 2}
    if vendor == "codex":
        cli["auth_path"] = synthetic_codex_auth(tmp_path)
    backend = Backend("worker-placeholder", "", "", extra={"provider_kind": "cli_worker", "cli": cli})
    with pytest.raises(BackendError) as error:
        backend.chat([{"role": "user", "content": "Synthetic request"}])
    assert error.value.category == "provider"
    assert "Partial private placeholder" not in str(error.value)


def test_durable_task_restart_and_explicit_execution_need_no_external_runtime(
        tmp_path, monkeypatch, no_external_work):
    from tessera.scheduler import Scheduler
    from tessera.tasks import TaskStore

    def unexpected(*args, **kwargs):
        raise AssertionError("Task construction must not start background work")

    monkeypatch.setattr("threading.Thread", unexpected)
    path = tmp_path / "board" / "tasks.jsonl"
    scope = ["artifact-placeholder"]
    budget = {"tokens": 7}
    store = TaskStore(path, enabled=True, clock=lambda: 100)
    created = store.create("Inspect the synthetic artifact", scope, budget, "account-placeholder")
    scope.append("unauthorized-placeholder")
    budget["tokens"] = 999
    created["scope"].clear()
    created["budget"]["tokens"] = 999
    queued = TaskStore(path, enabled=True, clock=lambda: 100).get(created["id"])
    assert queued["scope"] == ["artifact-placeholder"] and queued["budget"] == {"tokens": 7}
    assert queued["status"] == "queued" and queued["attempt"] == 0
    before = path.read_bytes()
    disabled = Scheduler(store, ["account-placeholder"])
    assert disabled.run_next("account-placeholder", unexpected) is None
    assert path.read_bytes() == before

    restored = TaskStore(path, enabled=True, clock=lambda: 100)
    scheduler = Scheduler(restored, ["account-placeholder"], enabled=True, clock=lambda: 100)
    executed = []

    def execute(claim):
        executed.append(claim["id"])
        assert claim["objective"] == "Inspect the synthetic artifact"
        assert claim["scope"] == ["artifact-placeholder"] and claim["budget"] == {"tokens": 7}
        return {"artifact_ref": "result-placeholder", "content": "transient-response-placeholder"}

    outcome = scheduler.run_next("account-placeholder", execute, owner="worker-placeholder")
    assert outcome["task"]["status"] == "succeeded"
    assert scheduler.run_next("account-placeholder", execute) is None
    assert executed == [created["id"]]
    final = TaskStore(path, enabled=True).get(created["id"])
    assert final["result_ref"] == "result-placeholder" and final["attempt"] == 1
    assert final["scope"] == ["artifact-placeholder"] and final["budget"] == {"tokens": 7}
    assert "transient-response-placeholder" not in path.read_text()


def test_lease_boundary_rejects_completion_before_an_explicit_recovery(tmp_path, no_external_work):
    from tessera.tasks import TaskError, TaskStore

    clock = [100.0]
    store = TaskStore(tmp_path / "tasks.jsonl", enabled=True, clock=lambda: clock[0])
    task = store.create("Inspect synthetic evidence", ["artifact-placeholder"], {}, "account-placeholder")
    claim = store.claim(task["id"], "first-worker", lease_seconds=5)
    before = store.path.read_bytes()
    clock[0] = 105
    with pytest.raises(TaskError, match="stale"):
        store.finish(task["id"], "first-worker", claim["attempt"], "succeeded")
    assert store.path.read_bytes() == before
    restarted = TaskStore(store.path, enabled=True, clock=lambda: clock[0])
    assert restarted.recover_expired() == 1
    next_claim = restarted.claim(task["id"], "next-worker", lease_seconds=5)
    assert next_claim["attempt"] == 2
