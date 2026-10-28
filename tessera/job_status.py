"""Bounded saved-job counts only. Never open execution stores or providers."""

import os
import re

from .passive_status import MAX_SAFE_INTEGER, configured_path, read_snapshot

STATES = {'queued', 'running', 'blocked', 'succeeded', 'failed', 'cancelled'}
DISPATCH = {'waiting', 'running', 'queued', 'succeeded', 'failed', 'cancelled'}
_WAIT = re.compile(r'^waiting for (.+) (slot|throttle)$')


def _shown_dispatch(status, backend, names):
    """A slot or throttle wait names the provider ordinal, not the backend name."""
    if isinstance(status, str):
        match = _WAIT.fullmatch(status)
        if match:
            kind = match.group(2)
            if isinstance(backend, str) and backend in names:
                return f'waiting for provider {names.index(backend) + 1} {kind}'
            return f'waiting for endpoint {kind}'
    if status in DISPATCH:
        return status
    return None


def _project_workers(workers, job_state):
    """Ordinals and dispatch only. Backend names stay in the job file."""
    if not isinstance(workers, list):
        return None
    names = []
    for worker in workers:
        if not isinstance(worker, dict):
            return None
        backend = worker.get('backend')
        if isinstance(backend, str) and backend not in names:
            names.append(backend)
    rows = []
    for index, worker in enumerate(workers):
        status = _shown_dispatch(worker.get('dispatch'), worker.get('backend'), names)
        if status is None:
            # A saved job from before dispatch was recorded has no per-worker phase.
            # A succeeded job only merges when every worker succeeded. Anything else
            # stays queued rather than inventing a failure.
            status = 'succeeded' if job_state == 'succeeded' else 'queued'
        row = {'ordinal': index + 1, 'status': status}
        attempt = worker.get('attempt')
        if type(attempt) is int and not isinstance(attempt, bool) and 0 <= attempt <= MAX_SAFE_INTEGER:
            row['attempt'] = attempt
        backend = worker.get('backend')
        if isinstance(backend, str):
            row['provider_ordinal'] = names.index(backend) + 1
        rows.append(row)
    return rows


def _usable_workers(config):
    """The (backend, account) pairs SwarmJobs._workers would actually admit.

    A job needs at least two workers. With distinct_workers on, both the backend
    name and the account must differ between workers; off, one backend may fan
    out many workers held by that backend's cap. Counting only *named* accounts
    said a lane was unconfigured while seven backends could run a job right now.
    """
    pairs = set()
    for name, backend in config.backends.items():
        if not backend.enabled:
            continue
        if backend.account and backend.account not in config.accounts:
            continue
        pairs.add((name, backend.account or 'backend:' + name))
    return pairs


def job_status(config):
    pairs = _usable_workers(config)
    from .swarm_bounds import job_task_limit, require_distinct_workers
    if require_distinct_workers(config):
        configured = len({name for name, _ in pairs}) >= 2 and len({account for _, account in pairs}) >= 2
    else:
        # One model is enough when a job may fan out on it.
        configured = len(pairs) >= 1
    result = {'enabled': config.swarm.enabled, 'configured': configured}
    if not result['enabled'] or not result['configured']:
        return result
    root = configured_path(config.home, config.swarm.jobs_dir)
    if root.is_symlink():
        return result
    paths = []
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                if re.fullmatch('[a-f0-9]{32}', entry.name) and entry.is_dir(follow_symlinks=False):
                    paths.append(root / entry.name / 'job.json')
                    if len(paths) > config.status.max_logs:
                        return result
    except FileNotFoundError:
        result['jobs'] = []
        return result
    except OSError:
        return result
    rows = []
    remaining = config.status.max_total_bytes
    for path in sorted(paths):
        present, records, _, size = read_snapshot(path, config.status, byte_limit=remaining)
        remaining -= size
        if not present or records is None or len(records) != 1:
            return result
        job = records[0]
        if not isinstance(job.get('status'), str) or job['status'] not in STATES or not isinstance(job.get('workers'), list):
            return result
        count = len(job['workers'])
        artifacts = job.get('artifacts')
        if count < 2 or not isinstance(artifacts, dict) or len(artifacts) > count:
            return result
        limit = job_task_limit(config)
        over_limit = limit is not None and count > limit
        row = {'ordinal': len(rows) + 1, 'status': job['status'], 'worker_count': count, 'artifact_count': len(artifacts)}
        if over_limit:
            row['over_limit'] = True
            row['limit_reason'] = (
                f'This job lists {count} workers and the current mode allows {limit}. '
                'swarm.max_job_tasks is the bound. The job stays listed.'
            )
        projected = _project_workers(job.get('workers'), job['status'])
        if projected is None:
            return result
        row['workers'] = projected
        for name in ('completed_count', 'failed_count'):
            value = job.get(name)
            if type(value) is not int or not 0 <= value <= min(count, MAX_SAFE_INTEGER):
                return result
            row[name] = value
        if row['completed_count'] + row['failed_count'] > count:
            return result
        row['merge_status'] = 'pending'
        if job.get('merged') is not None and job['status'] == 'succeeded':
            row['merge_status'] = 'succeeded'
        elif job['status'] in ('blocked', 'failed'):
            row['merge_status'] = job['status']
        row['gate_status'] = 'not_checked'
        gate = job.get('gate')
        if isinstance(gate, dict) and type(gate.get('allowed')) is bool:
            row['gate_status'] = 'allowed' if gate['allowed'] else 'blocked'
        rows.append(row)
    # Same bound as the task projection: the newest N jobs are not the whole store
    # unless nothing was dropped, and a reader that cannot see the cut counts the
    # visible rows as the total.
    selected = rows[-config.status.task_limit:]
    result['jobs'] = selected
    omitted = len(rows) - len(selected)
    if omitted > 0:
        result['jobs_omitted'] = omitted
        result['job_limit'] = config.status.task_limit
    return result
