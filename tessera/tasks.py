"""Opt-in append-only task board with locked, leased and fenced claims."""

from contextlib import contextmanager
from copy import deepcopy
from . import locking as fcntl
from .durability import sync_directory
import json
import math
import os
from pathlib import Path
import threading
import time
import uuid

from .provider_status import ERROR_CATEGORIES
from .rate_limits import day_key
from .gates import scopes_overlap


class TaskError(RuntimeError):
    pass


def nonnegative_numbers(values):
    if not isinstance(values, dict):
        raise ValueError("Task budget must be a numeric table")
    for value in values.values():
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ValueError("Task budget must contain finite nonnegative numbers")


class TaskStore:
    def __init__(self, path, *, enabled=False, clock=time.time):
        self.path = Path(path)
        self.enabled = enabled
        self.clock = clock
        self._lock = threading.RLock()

    @contextmanager
    def _locked(self):
        if not self.enabled:
            raise TaskError("Task board is disabled")
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.with_name(self.path.name + ".lock").open("a+b") as stream:
                fcntl.flock(stream, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(stream, fcntl.LOCK_UN)

    def _sync_directory(self):
        sync_directory(self.path.parent)

    def _load(self):
        tasks = {}
        sequence = 0
        if not self.path.exists():
            return tasks, sequence
        with self.path.open("r+b") as stream:
            while True:
                offset = stream.tell()
                line = stream.readline()
                if not line:
                    break
                if not line.endswith(b"\n"):
                    evidence = self.path.with_name(self.path.name + ".incomplete-" + uuid.uuid4().hex)
                    with evidence.open("xb") as backup:
                        backup.write(line)
                        backup.flush()
                        os.fsync(backup.fileno())
                    stream.truncate(offset)
                    stream.flush()
                    os.fsync(stream.fileno())
                    self._sync_directory()
                    break
                try:
                    record = json.loads(line)
                    sequence += 1
                    if record["sequence"] != sequence or not isinstance(record["task"], dict):
                        raise ValueError("Invalid task record")
                    task = record["task"]
                    tasks[task["id"]] = task
                except (ValueError, KeyError, TypeError):
                    raise TaskError("Invalid task journal record") from None
        return tasks, sequence

    def _append(self, task, sequence):
        existed = self.path.exists()
        with self.path.open("ab") as stream:
            record = {"sequence": sequence + 1, "task": task}
            stream.write((json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        if not existed:
            self._sync_directory()
        return deepcopy(task)

    def create(self, objective, scope, budget, account, *, task_id=None):
        if not self.enabled:
            raise TaskError("Task board is disabled")
        if not isinstance(objective, str) or not objective.strip() or not isinstance(account, str) or not account:
            raise ValueError("Task objective and account are required")
        if not isinstance(scope, list) or not scope or any(not isinstance(item, str) or not item for item in scope):
            raise ValueError("Task scope must name owned files or resources")
        nonnegative_numbers(budget)
        task_id = task_id or uuid.uuid4().hex
        if not isinstance(task_id, str) or not task_id:
            raise ValueError("Task id must be nonempty text")
        with self._locked():
            tasks, sequence = self._load()
            if task_id in tasks:
                raise TaskError("Task id is already recorded")
            stamp = self.clock()
            task = {"id": task_id, "objective": objective, "scope": list(scope), "budget": dict(budget),
                    "account": account, "attempt": 0, "status": "queued", "owner": None,
                    "lease_until": None, "not_before": 0, "result_ref": "", "error_category": None,
                    "created_at": stamp, "updated_at": stamp}
            return self._append(task, sequence)

    def list(self, *, status=None, account=None):
        if not self.enabled:
            return []
        with self._locked():
            tasks, _ = self._load()
            result = []
            for task in tasks.values():
                if status is not None and task["status"] != status:
                    continue
                if account is not None and task["account"] != account:
                    continue
                result.append(deepcopy(task))
            return result

    def get(self, task_id):
        with self._locked():
            tasks, _ = self._load()
            return deepcopy(tasks[task_id])

    def claim(self, task_id, owner, *, lease_seconds, account_limit=None, admission=None, reserve_budget=False, scope_exclusive=False):
        if not isinstance(owner, str) or not owner or not math.isfinite(lease_seconds) or lease_seconds <= 0:
            raise ValueError("Task claim requires an owner and positive lease")
        with self._locked():
            tasks, sequence = self._load()
            task = tasks[task_id]
            stamp = self.clock()
            if task["status"] != "queued" or task["not_before"] > stamp:
                return None
            if account_limit is not None:
                active = 0
                for other in tasks.values():
                    if other["account"] == task["account"] and other["status"] == "running" and other["lease_until"] > stamp:
                        active += 1
                if active >= account_limit:
                    return None
            if scope_exclusive:
                for other in tasks.values():
                    if other['status'] == 'running' and other['lease_until'] > stamp and scopes_overlap(task['scope'], other['scope']):
                        return None
            if admission is not None and not admission(deepcopy(task), deepcopy(list(tasks.values()))):
                return None
            if reserve_budget:
                task['reservation'] = {}
                for unit in ('tokens', 'money', 'seconds'):
                    if unit in task['budget']:
                        task['reservation'][unit] = max(0, task['budget'][unit] - task.get('usage', {}).get(unit, 0))
            task.update(status="running", owner=owner, attempt=task["attempt"] + 1,
                        lease_until=stamp + lease_seconds, updated_at=stamp)
            return self._append(task, sequence)

    def finish(self, task_id, owner, attempt, status, *, result_ref="", error_category=None, not_before=0, usage=None):
        if status not in ("succeeded", "failed", "cancelled", "queued") or not isinstance(result_ref, str):
            raise ValueError("Invalid task completion")
        if not math.isfinite(not_before) or not_before < 0:
            raise ValueError("Invalid task retry time")
        if error_category is not None and error_category not in ERROR_CATEGORIES:
            raise ValueError("Invalid task error category")
        if usage is not None:
            nonnegative_numbers(usage)
        with self._locked():
            tasks, sequence = self._load()
            task = tasks[task_id]
            stamp = self.clock()
            if task["status"] != "running" or task["owner"] != owner or task["attempt"] != attempt or stamp >= task["lease_until"]:
                raise TaskError("Rejected stale task claim")
            self._charge(task, usage or {}, stamp)
            task.update(status=status, result_ref=result_ref, error_category=error_category,
                        not_before=not_before, owner=None, lease_until=None, updated_at=stamp)
            return self._append(task, sequence)

    @staticmethod
    def _charge(task, usage, stamp):
        total = task.setdefault('usage', {})
        daily = task.setdefault('daily_usage', {}).setdefault(day_key(stamp), {})
        for unit in ('tokens', 'money', 'seconds'):
            if unit in usage:
                total[unit] = total.get(unit, 0) + usage[unit]
                daily[unit] = daily.get(unit, 0) + usage[unit]
        task.pop('reservation', None)

    def recover_expired(self, *, max_attempts=None):
        if max_attempts is not None and (type(max_attempts) is not int or max_attempts <= 0):
            raise ValueError("Task attempt cap must be a positive integer")
        with self._locked():
            tasks, sequence = self._load()
            count = 0
            for task in tasks.values():
                expired = task["status"] == "running" and self.clock() >= task["lease_until"]
                exhausted = max_attempts is not None and task["attempt"] >= max_attempts
                if expired or (task["status"] == "queued" and exhausted):
                    budget_exhausted = False
                    if expired:
                        self._charge(task, task.get("reservation", {}), task["updated_at"])
                        for unit, amount in task.get('usage', {}).items():
                            if unit in task['budget'] and amount >= task['budget'][unit]:
                                budget_exhausted = True
                    task.update(status="failed" if exhausted or budget_exhausted else "queued", owner=None, lease_until=None,
                                error_category="quota" if budget_exhausted else "timeout" if expired else task["error_category"] or "provider",
                                updated_at=self.clock())
                    self._append(task, sequence)
                    sequence += 1
                    count += 1
            return count
