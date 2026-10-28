"""Explicit scheduling exercises policy hooks; construction starts no workers."""

import pytest

from tessera.config import load_config
from tessera.scheduler import Scheduler, SchedulerHooks
from tessera.tasks import TaskStore


def test_disabled_config_has_no_journal_or_executor_side_effects(tmp_path):
    scheduler = Scheduler.from_config(load_config(tmp_path))
    def unexpected(task):
        pytest.fail("Disabled scheduler cannot execute")
    assert scheduler.run_next("unconfigured", unexpected) is None
    assert not (tmp_path / "tasks.jsonl").exists()


def test_account_queues_restore_and_attempts_are_not_claimed_twice(tmp_path):
    store = TaskStore(tmp_path / "tasks.jsonl", enabled=True)
    first = store.create("Read A", ["scope-a"], {}, "one")
    second = store.create("Read B", ["scope-b"], {}, "two")
    scheduler = Scheduler(store, ["one", "two"], enabled=True)
    claim = scheduler.claim_next("one", "worker-one")
    assert claim["id"] == first["id"]
    assert scheduler.claim_next("one", "duplicate") is None
    assert scheduler.claim_next("two", "worker-two")["id"] == second["id"]
    scheduler.complete(claim, result_ref="artifact-a")
    assert store.get(first["id"])["status"] == "succeeded"


def test_rate_aimd_budget_retry_and_circuit_hooks_control_admission(tmp_path):
    clock = [100.0]
    class Hooks(SchedulerHooks):
        ready = False
        circuit = True
        budget = True
        observed = []
        def rate_ready(self, account, now): return self.ready
        def circuit_allows(self, account): return self.circuit
        def budget_allows(self, task): return self.budget
        def concurrency_limit(self, account, configured): return 1
        def retry_delay(self, task, category): return 5 if category == "rate_limit" else None
        def observe(self, account, success, category=None, usage=None, rate_limits=None):
            self.observed.append((account, success, category))
    hooks = Hooks()
    store = TaskStore(tmp_path / "tasks.jsonl", enabled=True, clock=lambda: clock[0])
    task = store.create("Read source", ["scope-placeholder"], {"tokens": 20}, "one")
    scheduler = Scheduler(store, ["one"], enabled=True, hooks=hooks, clock=lambda: clock[0], max_attempts=2)
    assert scheduler.claim_next("one", "worker") is None
    hooks.ready = True
    hooks.budget = False
    assert scheduler.claim_next("one", "worker") is None
    hooks.budget = True
    claim = scheduler.claim_next("one", "worker")
    assert claim["attempt"] == 1
    scheduler.fail(claim, "rate_limit")
    assert store.get(task["id"])["status"] == "queued"
    assert scheduler.claim_next("one", "early") is None
    clock[0] = 106
    hooks.circuit = False
    assert scheduler.claim_next("one", "blocked") is None
    hooks.circuit = True
    retry = scheduler.claim_next("one", "retry")
    assert retry["attempt"] == 2
    scheduler.fail(retry, "rate_limit")
    assert store.get(task["id"])["status"] == "failed"
    assert hooks.observed == [("one", False, "rate_limit"), ("one", False, "rate_limit")]


def test_explicit_run_executes_once_and_stores_only_artifact_reference(tmp_path):
    store = TaskStore(tmp_path / "tasks.jsonl", enabled=True)
    task = store.create("Read source", ["scope-placeholder"], {}, "one")
    scheduler = Scheduler(store, ["one"], enabled=True)
    calls = []
    def execute(claim):
        calls.append(claim["id"])
        return {"artifact_ref": "artifact-placeholder", "content": "transient result"}
    result = scheduler.run_next("one", execute)
    assert calls == [task["id"]] and result["task"]["status"] == "succeeded"
    assert "transient result" not in store.path.read_text()
    assert scheduler.run_next("one", execute) is None


def test_scheduler_does_not_share_a_swarm_concurrency_cap(tmp_path):
    path = tmp_path / "tasks.jsonl"
    store = TaskStore(path, enabled=True)
    first = store.create("Read A", ["scope-a"], {}, "one")
    second = store.create("Read B", ["scope-b"], {}, "one")
    left = Scheduler(store, ["one"], enabled=True)
    right = Scheduler(TaskStore(path, enabled=True), ["one"], enabled=True)
    claim = left.claim_next("one", "left")
    other = right.claim_next("one", "right")
    assert claim["id"] == first["id"]
    assert other["id"] == second["id"]
    left.complete(claim)
    right.complete(other)


def test_crash_on_last_attempt_becomes_terminal_during_recovery(tmp_path):
    clock = [100.0]
    store = TaskStore(tmp_path / "tasks.jsonl", enabled=True, clock=lambda: clock[0])
    task = store.create("Read source", ["scope-placeholder"], {}, "one")
    store.claim(task["id"], "crashed", lease_seconds=1)
    clock[0] = 102
    scheduler = Scheduler(store, ["one"], enabled=True, max_attempts=1, clock=lambda: clock[0])
    assert scheduler.claim_next("one", "retry") is None
    assert store.get(task["id"])["status"] == "failed"


def test_opt_in_config_assembles_board_without_starting_execution(tmp_path, monkeypatch):
    (tmp_path / "config.toml").write_text('''
[accounts.one]
[swarm]
enabled = true
tasks_file = "board/tasks.jsonl"
max_attempts = 2
lease_seconds = 20
''')
    monkeypatch.setenv("TESSERA_SWARM_MAX_CONCURRENT", "2")
    loaded = load_config(tmp_path)
    assert any('swarm.max_concurrent' in note and 'retired' in note for note in loaded.migration_notes)
    scheduler = Scheduler.from_config(loaded)
    task = scheduler.store.create("Read source", ["scope-placeholder"], {}, "one")
    scheduler.enqueue(task["id"])
    assert scheduler.lease_seconds == 20
    assert scheduler.claim_next("one", "configured")["id"] == task["id"]


def test_configured_task_can_explicitly_run_a_fake_official_cli_worker(tmp_path):
    import json
    from tessera.backend import Backend
    from test_cli_worker import fake_cli
    result = {"type": "result", "subtype": "success", "is_error": False, "result": "Worker artifact ready"}
    command = fake_cli(tmp_path, "claude", "import sys\nsys.stdin.read()\nprint(" + repr(json.dumps(result)) + ")")
    (tmp_path / "config.toml").write_text(f'''
[accounts.subscription]
provider_kind = "cli_worker"
[backends.worker]
account = "subscription"
[backends.worker.cli]
vendor = "claude"
command = {json.dumps(command)}
cwd = {json.dumps(str(tmp_path))}
[swarm]
enabled = true
''')
    config = load_config(tmp_path)
    scheduler = Scheduler.from_config(config)
    task = scheduler.store.create("Read the scoped artifact", ["artifact-placeholder"], {"seconds": 10}, "subscription")
    scheduler.enqueue(task["id"])
    settings = config.backend_for()
    worker = Backend(settings.name, settings.base_url, settings.model, settings.api_key_env, settings.extra)
    output = scheduler.run_next("subscription", lambda claim: worker.chat([{"role": "user", "content": claim["objective"]}]))
    assert output["task"]["status"] == "succeeded"
    assert output["result"]["content"] == "Worker artifact ready"
    assert "Worker artifact ready" not in scheduler.store.path.read_text()


def test_executor_failure_records_category_without_private_exception_text(tmp_path):
    from tessera.backend import BackendError
    store = TaskStore(tmp_path / "tasks.jsonl", enabled=True)
    task = store.create("Read source", ["scope-placeholder"], {}, "one")
    scheduler = Scheduler(store, ["one"], enabled=True)
    def execute(claim):
        raise BackendError("synthetic-private-provider-error", "rate_limit")
    result = scheduler.run_next("one", execute)
    assert result["task"]["status"] == "failed" and result["error_category"] == "rate_limit"
    assert result["task"]["attempt"] == 1
    assert "synthetic-private-provider-error" not in str(result) + store.path.read_text()
