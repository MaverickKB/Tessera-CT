"""Offline consumers of scheduling policies; no provider or credential access."""

import pytest

from tessera.credentials import CredentialResolver
from tessera.backend import BackendError
from tessera.config import AccountConfig, Config
from tessera.jev import JudgeKit
from tessera.scheduler import Scheduler, SchedulerHooks
from tessera.swarm_policy import AccountPolicy
from tessera.tasks import TaskStore


@pytest.fixture(autouse=True)
def forbid_external_work(monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("Policy consumers must stay offline")

    monkeypatch.setattr(CredentialResolver, "resolve", unexpected)
    monkeypatch.setattr("urllib.request.OpenerDirector.open", unexpected)
    monkeypatch.setattr("subprocess.Popen", unexpected)


def test_disabled_scheduler_never_consults_policies_or_recovers_existing_debt(tmp_path):
    clock = [10.0]
    store = TaskStore(tmp_path / "tasks.jsonl", enabled=True, clock=lambda: clock[0])
    task = store.create("Inspect synthetic source", ["owned-placeholder"], {"tokens": 20}, "one")
    store.claim(task["id"], "expired-worker", lease_seconds=1)
    clock[0] = 12.0
    before = store.path.read_bytes()

    class UnusedHooks:
        def __getattr__(self, name):
            raise AssertionError("Disabled swarm must not consult " + name)

    def unused_executor(task):
        raise AssertionError("Disabled swarm must not execute")

    scheduler = Scheduler(store, ["one"], enabled=False, hooks=UnusedHooks(), clock=lambda: clock[0])
    assert scheduler.enqueue(task["id"]) is False
    assert scheduler.claim_next("unconfigured", "worker") is None
    assert scheduler.run_next("unconfigured", unused_executor) is None
    assert store.path.read_bytes() == before
    assert store.get(task["id"])["status"] == "running"


@pytest.mark.parametrize("blocking_gate", ["circuit", "rate", "budget"])
def test_denied_admission_spends_no_attempt_and_can_resume_offline(tmp_path, blocking_gate):
    class ConsumerHooks(SchedulerHooks):
        blocked = True

        def circuit_allows(self, account):
            return not (self.blocked and blocking_gate == "circuit")

        def rate_ready(self, account, now):
            assert now == 100.0
            return not (self.blocked and blocking_gate == "rate")

        def budget_allows(self, task):
            assert task["budget"] == {"tokens": 20}
            return not (self.blocked and blocking_gate == "budget")

        def concurrency_limit(self, account, configured):
            return 0 if self.blocked and blocking_gate == "capacity" else configured

        def observe(self, account, success, category=None, usage=None, rate_limits=None):
            observed.append((account, success, category, usage, rate_limits))

    hooks = ConsumerHooks()
    observed = []
    executions = []
    store = TaskStore(tmp_path / "tasks.jsonl", enabled=True, clock=lambda: 100.0)
    task = store.create("Inspect synthetic source", ["owned-placeholder"], {"tokens": 20}, "one")
    scheduler = Scheduler(store, ["one"], enabled=True, hooks=hooks, clock=lambda: 100.0)
    before = store.path.read_bytes()

    def execute(claim):
        executions.append((claim["id"], claim["attempt"], claim["scope"]))
        return {"artifact_ref": "artifact-placeholder", "usage": {"total_tokens": 7},
                "rate_limits": {"remaining_requests": 2}}

    for _ in range(3):
        assert scheduler.run_next("one", execute) is None
    assert executions == observed == []
    assert store.path.read_bytes() == before
    assert store.get(task["id"])["attempt"] == 0
    hooks.blocked = False
    result = scheduler.run_next("one", execute)
    assert result["task"]["status"] == "succeeded"
    assert executions == [(task["id"], 1, ["owned-placeholder"])]
    assert observed == [("one", True, None, {"total_tokens": 7}, {"remaining_requests": 2})]
    assert scheduler.run_next("one", execute) is None


def test_policy_concurrency_does_not_cap_admission(tmp_path):
    class LargerCapacity(SchedulerHooks):
        def concurrency_limit(self, account, configured):
            return 0

    store = TaskStore(tmp_path / "tasks.jsonl", enabled=True)
    first = store.create("Inspect A", ["scope-a"], {}, "one")
    second = store.create("Inspect B", ["scope-b"], {}, "one")
    scheduler = Scheduler(store, ["one"], enabled=True, hooks=LargerCapacity())
    claim = scheduler.claim_next("one", "first-worker")
    other = scheduler.claim_next("one", "second-worker")
    assert claim["id"] == first["id"]
    assert other["id"] == second["id"]
    assert store.get(second["id"])["attempt"] == 1


@pytest.mark.parametrize("invalid_capacity", [True, -1, 1.5, None])
def test_invalid_policy_capacity_is_not_an_admission_gate(tmp_path, invalid_capacity):
    class InvalidCapacity(SchedulerHooks):
        def concurrency_limit(self, account, configured):
            return invalid_capacity

    store = TaskStore(tmp_path / "tasks.jsonl", enabled=True)
    task = store.create("Inspect source", ["scope-placeholder"], {}, "one")
    scheduler = Scheduler(store, ["one"], enabled=True, hooks=InvalidCapacity())

    def execute(claim):
        return {"artifact_ref": "artifact-placeholder"}

    result = scheduler.run_next("one", execute)
    assert result["task"]["id"] == task["id"]
    assert result["task"]["status"] == "succeeded"


def test_disabled_account_policy_does_not_read_settings_clock_or_random():
    def unused(*args, **kwargs):
        raise AssertionError("Disabled policies must not inspect external state")

    class UnusedSettings:
        def __getattr__(self, name):
            unused()

    policy = AccountPolicy(settings=UnusedSettings(), enabled=False, clock=unused, random=unused)
    assert policy.rate_ready("unconfigured", 0) is True
    assert policy.circuit_allows("unconfigured") is True
    assert policy.budget_allows({}) is True
    assert policy.admit({}, []) is True
    assert policy.retry_delay({}, "rate_limit") is None
    assert policy.charge({}) is None
    assert policy.observe("unconfigured", False, "rate_limit") is None


def test_disabled_swarm_does_not_construct_configured_semantic_gates(tmp_path, monkeypatch):
    config = Config(home=tmp_path)
    config.swarm.enabled = False
    config.swarm.gates_enabled = True
    config.jev.enabled = True
    config.jev.url = "https://judge.example.com/evaluate"
    config.jev.model = "judge-placeholder"
    config.jev.credential_ref = "credential-placeholder"
    config.credentials = {"credential-placeholder": {"env": "EXAMPLE_CONSUMER_JUDGE_KEY"}}

    def unused(*args, **kwargs):
        raise AssertionError("Disabled swarm must not construct judgments or execute")

    monkeypatch.setattr(JudgeKit, "from_config", unused)
    scheduler = Scheduler.from_config(config, clock=unused, random=unused)
    assert scheduler.run_next("unconfigured", unused) is None
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("unit", ["tokens", "money", "seconds"])
def test_durable_daily_budget_is_account_local_and_admits_exact_remaining_limit(tmp_path, unit):
    config = Config(home=tmp_path)
    config.swarm.enabled = True
    for name in ("one", "two"):
        config.accounts[name] = AccountConfig(name=name, budget={unit + "_per_day": 10})
    scheduler = Scheduler.from_config(config, clock=lambda: 100.0)
    first = scheduler.store.create("Inspect A", ["scope-a"], {unit: 4}, "one")
    scheduler.enqueue(first["id"])
    claim = scheduler.claim_next("one", "first-worker")
    scheduler.complete(claim, usage={unit: 4})
    assert scheduler.store.get(first["id"])["usage"][unit] == 4

    reopened = Scheduler.from_config(config, clock=lambda: 100.0)
    tasks = []
    for objective, account, amount in (("over-budget", "one", 7), ("other-account", "two", 7),
                                       ("exact-budget", "one", 6)):
        task = reopened.store.create(objective, ["scope-placeholder"], {unit: amount}, account)
        reopened.enqueue(task["id"])
        tasks.append(task)
    other = reopened.claim_next("two", "other-worker")
    exact = reopened.claim_next("one", "exact-worker")
    assert other["id"] == tasks[1]["id"]
    assert exact["id"] == tasks[2]["id"]
    assert reopened.store.get(tasks[0]["id"])["attempt"] == 0
    assert reopened.claim_next("one", "over-worker") is None


def test_bounded_account_requires_reservation_without_spending_attempt(tmp_path):
    config = Config(home=tmp_path)
    config.swarm.enabled = True
    config.accounts["one"] = AccountConfig(name="one", budget={"money_per_day": 10})
    scheduler = Scheduler.from_config(config, clock=lambda: 100.0)
    task = scheduler.store.create("Inspect source", ["scope-placeholder"], {"tokens": 4}, "one")
    scheduler.enqueue(task["id"])
    before = scheduler.store.path.read_bytes()
    assert scheduler.claim_next("one", "worker") is None
    assert scheduler.store.path.read_bytes() == before
    assert scheduler.store.get(task["id"])["attempt"] == 0


def test_rate_failure_metadata_blocks_only_its_account_until_probe_can_run(tmp_path):
    clock = [100.0]
    config = Config(home=tmp_path)
    config.swarm.enabled = True
    config.swarm.circuit_failures = 1
    config.swarm.circuit_cooldown = 5
    config.swarm.retry_base = config.swarm.retry_cap = 8
    for name in ("one", "two"):
        config.accounts[name] = AccountConfig(name=name)
    scheduler = Scheduler.from_config(config, clock=lambda: clock[0], random=lambda: .5)
    tasks = []
    for label, account in (("A", "one"), ("B", "one"), ("C", "two")):
        task = scheduler.store.create("Inspect " + label, [label], {}, account)
        scheduler.enqueue(task["id"])
        tasks.append(task)

    def rate_limited(claim):
        raise BackendError("synthetic-private-failure", "rate_limit", rate_limits={"retry_at": 112})

    result = scheduler.run_next("one", rate_limited)
    assert result["task"]["id"] == tasks[0]["id"]
    assert result["task"]["status"] == "queued"
    assert result["task"]["not_before"] == 104
    assert "synthetic-private-failure" not in str(result) + scheduler.store.path.read_text()
    before = scheduler.store.path.read_bytes()
    assert scheduler.claim_next("one", "circuit-blocked") is None
    clock[0] = 105.0
    assert scheduler.claim_next("one", "rate-blocked") is None
    assert scheduler.store.path.read_bytes() == before
    other = scheduler.claim_next("two", "other-worker")
    assert other["id"] == tasks[2]["id"]
    clock[0] = 112.0
    probe = scheduler.claim_next("one", "probe-worker")
    assert probe["id"] == tasks[1]["id"]
    # The half-open circuit no longer leaves a one-claim semaphore. The other
    # queued task is admitted too. The model endpoint is the concurrency limit.
    extra = scheduler.claim_next("one", "extra-probe")
    assert extra["id"] == tasks[0]["id"] and extra["attempt"] == 2
    scheduler.complete(probe)
    scheduler.complete(extra)
