"""HTTP consumers of passive task and judge projections over synthetic durable files."""

from contextlib import contextmanager
import json
import os
import threading
from unittest.mock import Mock

import pytest

from tessera.backend import Backend
from tessera.config import AccountConfig, BackendConfig, Config
from tessera.credentials import CredentialResolver
from tessera.jev import JudgeKit
from tessera.scheduler import Scheduler
from tessera.server import create_server
from tessera.sessions import SessionManager
from tessera.tasks import TaskStore
from test_server import call


@pytest.fixture
def forbid_active_work(monkeypatch):
    tripwires = []

    def install():
        targets = [
            (CredentialResolver, "resolve"), (Backend, "__init__"), (JudgeKit, "_evaluate_batch"),
            (Scheduler, "__init__"), (SessionManager, "runtime"),
            (TaskStore, "create"), (TaskStore, "claim"), (TaskStore, "finish"),
            (TaskStore, "recover_expired"), (TaskStore, "_append"),
        ]
        for owner, name in targets:
            tripwire = Mock(side_effect=AssertionError("Status must remain passive"))
            monkeypatch.setattr(owner, name, tripwire)
            tripwires.append(tripwire)
        for target in ("urllib.request.urlopen", "urllib.request.OpenerDirector.open", "subprocess.Popen"):
            tripwire = Mock(side_effect=AssertionError("Status must not contact or spawn integrations"))
            monkeypatch.setattr(target, tripwire)
            tripwires.append(tripwire)

    yield install
    for tripwire in tripwires:
        tripwire.assert_not_called()


def disk_snapshot(root):
    """Files and directories under root, keyed by relative path.

    atomic_json writes a temporary file and renames it into place. A snapshot taken
    while that happens can see the temp file vanish between listing and reading, so
    a path that disappears mid-walk is skipped rather than raised.
    """
    snapshot = {}
    for path in root.rglob("*"):
        # atomic_json's temporary file is a write in progress, not persisted state.
        if path.name.endswith(".tmp"):
            continue
        try:
            if path.is_file():
                snapshot[str(path.relative_to(root))] = (path.read_bytes(), path.stat().st_mtime_ns)
            else:
                snapshot[str(path.relative_to(root))] = None
        except FileNotFoundError:
            continue
    return snapshot


@contextmanager
def status_server(config):
    config.server.port = 0
    server = create_server(config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def configured_status(tmp_path, monkeypatch):
    monkeypatch.setenv("EXAMPLE_STATUS_PROJECTION_KEY", "synthetic-status-key")
    config = Config(home=tmp_path, cwd=tmp_path)
    config.credentials = {"reference-placeholder": {"env": "EXAMPLE_STATUS_PROJECTION_KEY",
        "keychain_service": "private-service-placeholder", "keychain_account": "private-keychain-placeholder"}}
    config.accounts["private-account-placeholder"] = AccountConfig(
        name="private-account-placeholder", credential_ref="reference-placeholder")
    config.backends["backend-placeholder"] = BackendConfig(name="backend-placeholder",
        account="private-account-placeholder", base_url="https://models.example.com/v1", model="private-model-placeholder")
    config.jev.enabled = True
    config.jev.url = "https://judge.example.com/evaluate"
    config.jev.model = "private-judge-placeholder"
    config.jev.credential_ref = "reference-placeholder"
    config.aethermind.endpoints = ["https://memory.example.com/mcp"]
    config.aethermind.project = "/project-placeholder"
    config.swarm.enabled = True
    config.swarm.gates_enabled = True
    config.swarm.tasks_file = "board/tasks.jsonl"
    return config


def seed_task_states(config):
    store = TaskStore(config.home / config.swarm.tasks_file, enabled=True, clock=lambda: 100.0)
    for status in ("queued", "running", "succeeded", "failed"):
        task = store.create("private-objective-placeholder", ["private-scope-placeholder"], {"tokens": 10},
                            "private-account-placeholder", task_id="private-task-" + status)
        if status == "queued":
            continue
        claim = store.claim(task["id"], "private-worker-placeholder", lease_seconds=1)
        if status != "running":
            store.finish(claim["id"], claim["owner"], claim["attempt"], status,
                         result_ref="private-artifact-placeholder")
    return store.path


def seed_judge_records(manager):
    slug = manager.create("status fixture")["slug"]
    log = manager.path(slug) / "decisions.jsonl"
    records = []
    for index, point in enumerate(("intake", "memory")):
        records.append({"batch_id": "private-batch-placeholder", "question_id": "question-" + str(index),
                        "point": point, "kind": point, "source": "heuristic", "noul": .8,
                        "at": f"2024-01-01T00:00:0{index}+00:00", "model_id": "private-log-model-placeholder",
                        "session_id": "private-session-placeholder", "request": "private-prompt-placeholder",
                        "error": "private-diagnostic-placeholder"})
    log.write_text("".join(json.dumps(record) + "\n" for record in records))
    return log


def assert_private_values_absent(report):
    encoded = json.dumps(report)
    for private in ("reference-placeholder", "EXAMPLE_STATUS_PROJECTION_KEY", "synthetic-status-key",
                    "private-service-placeholder", "private-keychain-placeholder", "private-account-placeholder",
                    "private-model-placeholder", "private-judge-placeholder", "models.example.com",
                    "judge.example.com", "memory.example.com", "/project-placeholder",
                    "private-objective-placeholder", "private-scope-placeholder", "private-task-",
                    "private-worker-placeholder", "private-artifact-placeholder", "private-batch-placeholder",
                    "private-log-model-placeholder", "private-session-placeholder", "private-prompt-placeholder",
                    "private-diagnostic-placeholder"):
        assert private not in encoded


def test_http_projection_reports_saved_state_without_recovery_or_external_work(
        tmp_path, monkeypatch, forbid_active_work):
    config = configured_status(tmp_path, monkeypatch)
    seed_task_states(config)
    with status_server(config) as server:
        seed_judge_records(server.manager)
        forbid_active_work()
        before = disk_snapshot(tmp_path)
        reports = []
        for _ in range(3):
            code, report = call(server, "GET", "/api/status")
            assert code == 200
            tasks = report["task_status"]
            assert tasks["enabled"] is True and tasks["configured"] is True
            assert sorted((row["status"], row["attempt"]) for row in tasks["tasks"]) == [
                ("failed", 1), ("queued", 0), ("running", 1), ("succeeded", 1)]
            for row in tasks["tasks"]:
                assert set(row) <= {"status", "attempt", "account_ordinal"}
            judge = report["jev"]
            assert judge["enabled"] is True and judge["decision_log_present"] is True
            assert judge["decision_count"] == 2 and judge["last_decision_category"] == "memory"
            assert_private_values_absent(report)
            reports.append((tasks, judge))
        assert reports[0] == reports[1] == reports[2]
        assert disk_snapshot(tmp_path) == before


def test_empty_disabled_status_does_not_create_task_or_judge_storage(tmp_path, forbid_active_work):
    config = Config(home=tmp_path, cwd=tmp_path)
    with status_server(config) as server:
        forbid_active_work()
        before = disk_snapshot(tmp_path)
        code, report = call(server, "GET", "/api/status")
        assert code == 200
        assert report["task_status"]["enabled"] is False
        assert "tasks" not in report["task_status"]
        assert report["jev"]["decision_log_present"] is False
        assert report["jev"]["decision_count"] == 0
        assert report["jev"].get("last_decision_category") is None
        assert disk_snapshot(tmp_path) == before


@pytest.mark.parametrize("last_point", ["private-category-placeholder", {"private-category-placeholder": "value"}])
def test_torn_tails_and_unknown_judge_points_stay_unmodified_and_unreported(
        tmp_path, monkeypatch, forbid_active_work, last_point):
    config = configured_status(tmp_path, monkeypatch)
    board = seed_task_states(config)
    with board.open("ab") as stream:
        stream.write(b'{"sequence":')
    with status_server(config) as server:
        log = seed_judge_records(server.manager)
        with log.open("a") as stream:
            stream.write(json.dumps({"point": last_point, "prompt": "private-prompt-placeholder"}) + "\n")
            stream.write('{"point":"private-uncommitted')
        forbid_active_work()
        before = disk_snapshot(tmp_path)
        code, report = call(server, "GET", "/api/status")
        assert code == 200
        assert sorted(row["status"] for row in report["task_status"]["tasks"]) == [
            "failed", "queued", "running", "succeeded"]
        assert report["jev"]["decision_count"] == 3
        assert "last_decision_category" not in report["jev"]
        assert_private_values_absent(report)
        assert "private-category-placeholder" not in json.dumps(report)
        assert "private-uncommitted" not in json.dumps(report)
        assert disk_snapshot(tmp_path) == before


@pytest.mark.parametrize("storage", ["oversized_judge", "corrupt_tasks"])
def test_unreadable_metadata_remains_unknown_without_errors_or_repairs(
        tmp_path, monkeypatch, forbid_active_work, storage):
    config = configured_status(tmp_path, monkeypatch)
    board = seed_task_states(config)
    with status_server(config) as server:
        log = seed_judge_records(server.manager)
        if storage == "oversized_judge":
            config.status.max_file_bytes = log.stat().st_size - 1
        else:
            with board.open("a") as stream:
                stream.write("private-corrupt-task-record\n")
        forbid_active_work()
        before = disk_snapshot(tmp_path)
        code, report = call(server, "GET", "/api/status")
        assert code == 200
        if storage == "oversized_judge":
            assert report["jev"]["decision_log_present"] is True
            assert "decision_count" not in report["jev"]
            assert "last_decision_category" not in report["jev"]
        else:
            assert "tasks" not in report["task_status"]
        assert_private_values_absent(report)
        assert "private-corrupt-task-record" not in json.dumps(report)
        assert disk_snapshot(tmp_path) == before


def test_status_does_not_drain_pending_channels_even_when_automatic_replies_are_enabled(
        tmp_path, monkeypatch, forbid_active_work):
    config = configured_status(tmp_path, monkeypatch)
    config.channels.enabled = True
    config.channels.auto_deliver = False
    config.channels.auto_reply = True
    seed_task_states(config)
    with status_server(config) as server:
        server.manager.create("one")
        server.manager.create("two")
        seed_judge_records(server.manager)
        server.manager.direct_work("one", "two", "Can you inspect the synthetic artifact?",
                                   message_id="pending-placeholder")
        pending = server.manager.channels.pending()
        assert len(pending) == 2
        deliver = Mock(side_effect=AssertionError("Status must not deliver or reply to work"))
        monkeypatch.setattr(server.manager, "deliver_work", deliver)
        forbid_active_work()
        before = disk_snapshot(tmp_path)
        code, report = call(server, "GET", "/api/status")
        assert code == 200 and len(report["task_status"]["tasks"]) == 4
        assert report["jev"]["decision_count"] == 2
        deliver.assert_not_called()
        assert server.manager.channels.pending() == pending
        assert disk_snapshot(tmp_path) == before
        assert_private_values_absent(report)


def test_saved_archived_and_board_judge_logs_share_one_passive_snapshot_scope(
        tmp_path, monkeypatch, forbid_active_work):
    config = configured_status(tmp_path, monkeypatch)
    seed_task_states(config)
    with status_server(config) as server:
        log = seed_judge_records(server.manager)
        server.manager.update(log.parent.name, archived=True)
        board_log = (tmp_path / config.swarm.tasks_file).with_name("tasks.decisions.jsonl")
        board_log.write_text(json.dumps({"point": "message_gate", "model_id": "private-log-model-placeholder"}) + "\n")
        os.utime(log, ns=(2_000_000_000, 2_000_000_000))
        os.utime(board_log, ns=(3_000_000_000, 3_000_000_000))
        forbid_active_work()
        before = disk_snapshot(tmp_path)
        code, report = call(server, "GET", "/api/status")
        assert code == 200
        assert report["jev"]["decision_log_present"] is True
        assert report["jev"]["decision_count"] == 3
        assert report["jev"]["last_decision_category"] == "judge"
        assert_private_values_absent(report)
        assert disk_snapshot(tmp_path) == before
