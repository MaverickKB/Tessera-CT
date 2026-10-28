"""Explicit per-account queues with optional engine pressure admission observations."""

from collections import deque
import math
import random as random_module
from pathlib import Path
import threading
import time

from .provider_status import ERROR_CATEGORIES
from .tasks import TaskError, TaskStore
from .swarm_policy import AccountPolicy
from .jev import JudgeKit


class SchedulerHooks(AccountPolicy):
    """Offline no-ops by default; from_config enables measured account policy."""


BACKEND_ACCOUNT = 'backend:'


def swarm_accounts(config):
    """Configured accounts plus one implicit pool per backend that has no account.

    Pool and swarm concurrency numbers are not admission caps. A model endpoint is.
    """
    from .config import AccountConfig
    accounts = {}
    for name, account in config.accounts.items():
        accounts[name] = account
    for name, backend in config.backends.items():
        if backend.account:
            continue
        key = BACKEND_ACCOUNT + name
        if key in accounts:
            continue
        accounts[key] = AccountConfig(name=key, provider_kind=backend.provider_kind or 'openai_chat')
    return accounts


class Scheduler:
    def __init__(self, store, accounts, *, enabled=False, hooks=None,
                 max_attempts=3, lease_seconds=600, clock=time.time, judgekit=None, human=None):
        if type(max_attempts) is not int or max_attempts <= 0:
            raise ValueError("Scheduler attempts must be a positive integer")
        if not math.isfinite(lease_seconds) or lease_seconds <= 0:
            raise ValueError("Scheduler lease must be positive and finite")
        self.store = store
        self.enabled = enabled
        self.hooks = hooks or SchedulerHooks()
        self.max_attempts = max_attempts
        self.lease_seconds = lease_seconds
        self.clock = clock
        self.judgekit = judgekit if enabled else None
        self.human = human
        self.pressure_config = None
        self._lock = threading.RLock()
        self._queues = {}
        for account in accounts:
            self._queues[account] = deque()
        self._queued = set()
        if enabled:
            if not store.enabled:
                raise ValueError("Enabled scheduler requires an enabled task board")
            store.recover_expired(max_attempts=self.max_attempts)
            for task in store.list(status="queued"):
                self.enqueue(task["id"])

    @classmethod
    def from_config(cls, config, *, hooks=None, clock=time.time, random=random_module.random, human=None):
        settings = config.swarm
        path = Path(settings.tasks_file).expanduser()
        if not path.is_absolute():
            path = config.home / path
        store = TaskStore(path, enabled=settings.enabled, clock=clock)
        if hooks is None:
            hooks = SchedulerHooks(swarm_accounts(config), settings, enabled=settings.enabled, clock=clock, random=random)
        kit = None
        if settings.enabled and settings.gates_enabled:
            kit = JudgeKit.from_config(config, decision_log=path.with_name(path.stem + '.decisions.jsonl'))
        scheduler = cls(store, swarm_accounts(config), enabled=settings.enabled, hooks=hooks,
                   max_attempts=settings.max_attempts,
                   lease_seconds=settings.lease_seconds, clock=clock, judgekit=kit, human=human)
        scheduler.pressure_config = config
        return scheduler

    def pressure_allows(self, account, backend=None):
        """Inventory model identity opts an HTTP backend into engine admission.

        CLI pools never probe. Callers selecting from shared pools pass the exact
        backend; account-only legacy callers can infer it only for unique pools.
        Unknown engines hold until a later pass observes valid capacity.
        """
        config = self.pressure_config
        if config is None or not config.swarm.pressure_hold_enabled:
            return True
        # pressure_hold_enabled defaults off. On means the operator turned it on,
        # so the hold applies in Safe mode and in Unleashed. Only a system default drops there.
        if account in config.accounts and config.accounts[account].provider_kind == 'cli_worker':
            return True
        if backend is None:
            if account.startswith(BACKEND_ACCOUNT):
                backend = account[len(BACKEND_ACCOUNT):]
            else:
                names = [name for name, item in config.backends.items() if item.account == account]
                # Shared pools need the caller to identify the actual backend.
                if len(names) != 1:
                    return not names
                backend = names[0]
        settings = config.backends[backend]
        pool = config.accounts.get(settings.account)
        kind = pool.provider_kind if pool else settings.provider_kind
        if kind == "cli_worker":
            return True
        engines = [engine for engine in config.infrastructure.pressure_engines
                   if engine["model"] == settings.model]
        if not engines:
            return True
        from .engine_pressure import sample_engine
        for engine in engines:
            reading = sample_engine(engine)
            if not reading["reachable"]:
                return False
            occupied = reading["running"] + reading["waiting"] + int(reading["warming"])
            if occupied >= reading["slots_total"] * config.swarm.pressure_soft_threshold:
                return False
        return True

    def enqueue(self, task_id):
        if not self.enabled:
            return False
        with self._lock:
            task = self.store.get(task_id)
            if task["account"] not in self._queues:
                raise ValueError("Task account is not configured")
            if task["status"] != "queued" or task_id in self._queued:
                return False
            self._queues[task["account"]].append(task_id)
            self._queued.add(task_id)
            return True

    def claim_next(self, account, owner, *, task_ids=None, backend=None):
        if not self.enabled:
            return None
        with self._lock:
            if account not in self._queues:
                raise ValueError("Scheduler account is not configured")
            if not self.hooks.circuit_allows(account) or not self.hooks.rate_ready(account, self.clock()):
                return None
            # Swarm and account concurrency numbers are not admission caps. The model endpoint is.
            queue = self._queues[account]
            for _ in range(len(queue)):
                task_id = queue.popleft()
                self._queued.remove(task_id)
                if task_ids is not None and task_id not in task_ids:
                    self.enqueue(task_id)
                    continue
                task = self.store.get(task_id)
                if task["status"] != "queued":
                    continue
                if task["attempt"] >= self.max_attempts:
                    # Recovery never grants another attempt beyond the configured cap.
                    self.store.recover_expired(max_attempts=self.max_attempts)
                    continue
                if not self.hooks.budget_allows(task):
                    self.enqueue(task_id)
                    continue
                if not self.pressure_allows(account, backend):
                    self.enqueue(task_id)
                    continue
                if self.judgekit is not None:
                    running = self.store.list(status='running')
                    running = [other for other in running if other['lease_until'] > self.clock()]
                    gate = self.judgekit.swarm_gates(task['objective'], tasks=[task] + running,
                                                    phase='admission', human=self.human)
                    if not gate['allowed']:
                        self.enqueue(task_id)
                        continue
                claim = self.store.claim(task_id, owner, lease_seconds=self.lease_seconds, account_limit=None,
                                         admission=self.hooks.admit, reserve_budget=self.hooks.enabled,
                                         scope_exclusive=self.judgekit is not None)
                if claim is not None:
                    self.hooks.claimed(claim)
                    return claim
                self.enqueue(task_id)
            return None

    def complete(self, claim, *, result_ref="", usage=None, rate_limits=None):
        if not self.enabled:
            raise TaskError("Scheduler is disabled")
        with self._lock:
            charge = self.hooks.charge(claim, usage)
            overrun = False
            for unit, amount in (charge or {}).items():
                if unit in claim['budget'] and amount + claim.get('usage', {}).get(unit, 0) > claim['budget'][unit]:
                    overrun = True
            task = self.store.finish(claim["id"], claim["owner"], claim["attempt"], 'failed' if overrun else 'succeeded',
                                     result_ref=result_ref, error_category='quota' if overrun else None, usage=charge)
            self.hooks.observe(claim["account"], True, usage=usage, rate_limits=rate_limits)
            return task

    def fail(self, claim, category, *, usage=None, rate_limits=None):
        if not self.enabled:
            raise TaskError("Scheduler is disabled")
        with self._lock:
            if not isinstance(category, str) or category not in ERROR_CATEGORIES:
                category = "provider"
            delay = self.hooks.retry_delay(claim, category)
            retry = delay is not None and claim["attempt"] < self.max_attempts
            if delay is not None and (type(delay) not in (int, float) or not math.isfinite(delay) or delay < 0):
                raise ValueError("Retry policy must return a finite nonnegative delay or None")
            charge = self.hooks.charge(claim, usage)
            observation_category = category
            for unit, amount in (charge or {}).items():
                spent = amount + claim.get('usage', {}).get(unit, 0)
                if unit in claim['budget'] and spent >= claim['budget'][unit]:
                    retry = False
                    # A conservative full-reservation charge (unreported usage) is not an
                    # observed overrun; keep the real failure category. Only exceeding is quota.
                    if spent > claim['budget'][unit]:
                        category = 'quota'
            task = self.store.finish(claim["id"], claim["owner"], claim["attempt"], "queued" if retry else "failed",
                                     error_category=category, not_before=self.clock() + delay if retry else 0, usage=charge)
            if retry:
                self.enqueue(task["id"])
            self.hooks.observe(claim["account"], False, observation_category, usage=usage, rate_limits=rate_limits)
            return task

    def run_next(self, account, execute, *, owner="scheduler"):
        claim = self.claim_next(account, owner)
        if claim is None:
            return None
        try:
            result = execute(claim)
        except Exception as error:
            task = self.fail(claim, getattr(error, "category", "provider"),
                             usage=getattr(error, "usage", None), rate_limits=getattr(error, "rate_limits", None))
            return {"task": task, "error_category": task["error_category"]}
        reference = result.get("artifact_ref", "") if isinstance(result, dict) else ""
        usage = result.get("usage") if isinstance(result, dict) else None
        limits = result.get("rate_limits") if isinstance(result, dict) else None
        gate = None
        if self.judgekit is not None:
            details = result if isinstance(result, dict) else {}
            gate = self.judgekit.swarm_gates(claim['objective'], artifacts=details.get('artifacts', []),
                                            checks=details.get('checks'), steps=details.get('steps', []), human=self.human)
            if not gate['allowed']:
                task = self.store.finish(claim['id'], claim['owner'], claim['attempt'], 'failed',
                                         error_category='configuration', usage=self.hooks.charge(claim, usage))
                self.hooks.observe(account, True, usage=usage, rate_limits=limits)
                return {'task': task, 'gate': gate}
        task = self.complete(claim, result_ref=reference, usage=usage, rate_limits=limits)
        output = {"task": task, "result": result}
        if gate is not None:
            output['gate'] = gate
        return output
