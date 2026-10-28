"""Explicit bounded fan-out jobs over the shared scheduler and immutable artifacts."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict, replace
from . import locking as fcntl
from .durability import sync_directory
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import time
import uuid

from .backend import Backend, BackendError
from .agent_selection import select_backend
from .endpoint_limit import model_slot
from .swarm_bounds import job_task_limit, require_distinct_workers
from .endpoint_throttle import endpoint_pace, throttle_limits
from .scheduler import BACKEND_ACCOUNT
from .board import BoardStore
from .channels import valid_name
from .gates import conflicting_tasks
from .jev import JudgeKit
from .passive_status import configured_path
from .scheduler import Scheduler
from .tasks import TaskError, TaskStore, nonnegative_numbers


def encoded(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False) + '\n').encode('utf-8')


def atomic_write(path, data):
    temporary = path.with_name('.' + path.name + '.' + uuid.uuid4().hex)
    try:
        with temporary.open('xb') as stream:
            os.chmod(temporary, 0o600)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


class SwarmJobs:
    """Construction and reads never instantiate providers or start worker threads.

    run() makes at most one attempt per queued child. Retry deadlines, account
    pauses and uncertain expired claims require another explicit run().
    """

    def __init__(self, config, *, backend_factory=None, human=None):
        self.config = config
        self.settings = config.swarm
        self.root = configured_path(config.home, self.settings.jobs_dir)
        self.tasks = TaskStore(configured_path(config.home, self.settings.tasks_file), enabled=self.settings.enabled)
        board = config.board
        self.board = BoardStore(configured_path(config.home, board.journal_file), enabled=board.enabled,
                                max_text_bytes=board.max_text_bytes, max_artifacts=board.max_artifacts,
                                max_questions=board.max_questions,
                                max_journal_bytes=config.status.max_file_bytes)
        # The acceptance gate is the board's policy, and this lane is a second writer of card
        # status. Without copying it here, a swarm job card closed through _record bypasses a
        # gate the session lane honours: measured with [board] acceptance_required = true, this
        # store reported False and closed a job card as succeeded with no acceptance.
        self.board.acceptance_required = bool(board.acceptance_required)
        self.board.acceptance_by = tuple(board.acceptance_by or ())
        self.backend_factory = backend_factory
        self.human = human
        self._scheduler = None
        self._scheduler_lock = threading.Lock()
        self._snapshot = threading.Lock()

    def _enabled(self):
        if not self.settings.enabled:
            raise ValueError('Swarm jobs are disabled')

    def _path(self, job_id):
        if not isinstance(job_id, str) or not re.fullmatch('[a-f0-9]{32}', job_id):
            raise ValueError('Invalid swarm job id')
        path = self.root / job_id
        if self.root.is_symlink() or path.is_symlink():
            raise ValueError('Swarm job directories cannot be symbolic links')
        return path

    @contextmanager
    def _locked(self, job_id):
        path = self._path(job_id)
        with (path / '.lock').open('a') as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError('Swarm job is already executing') from None
            try:
                yield path
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    def get(self, job_id):
        self._enabled()
        return json.loads((self._path(job_id) / 'job.json').read_text(encoding='utf-8'))

    def _workers(self, objective, workers):
        if not isinstance(objective, str) or not objective.strip():
            raise ValueError('A swarm job needs an objective')
        if len(objective.encode('utf-8')) > self.settings.max_artifact_bytes:
            raise ValueError('Swarm objective exceeds configured bound')
        limit = job_task_limit(self.config)
        if not isinstance(workers, list) or len(workers) < 2 or (limit is not None and len(workers) > limit):
            raise ValueError('A swarm job needs a bounded set of at least two workers')
        normalized = []
        accounts = set()
        backends = set()
        for worker in workers:
            if not isinstance(worker, dict) or set(worker) - {'backend', 'objective', 'scope', 'budget', 'capability'}:
                raise ValueError('Invalid swarm worker fields')
            name = select_backend(self.config, worker.get('backend'), worker.get('capability', 'routine'))
            if not isinstance(name, str) or name not in self.config.backends:
                raise ValueError('Swarm worker backend is not configured')
            settings = self.config.backends[name]
            account = settings.account or BACKEND_ACCOUNT + name
            if settings.account and settings.account not in self.config.accounts:
                raise ValueError('Swarm worker account is not configured')
            if require_distinct_workers(self.config) and (name in backends or account in accounts):
                raise ValueError('Swarm workers require distinct backends and accounts')
            backends.add(name)
            accounts.add(account)
            text = worker.get('objective')
            scope = worker.get('scope')
            budget = deepcopy(worker.get('budget', {}))
            if not isinstance(text, str) or not text.strip() or len(text.encode('utf-8')) > self.settings.max_artifact_bytes:
                raise ValueError('Invalid bounded worker objective')
            if not isinstance(scope, list) or not scope or any(not isinstance(s, str) or not s.strip() for s in scope):
                raise ValueError('Swarm workers require explicit scopes')
            nonnegative_numbers(budget)
            selected = replace(self.config, roles={'main': name}).backend_for()
            binding = hashlib.sha256(encoded(asdict(selected))).hexdigest()
            normalized.append({'backend': name, 'account': account, 'binding': binding,
                               'objective': text, 'scope': scope, 'budget': budget})
        if conflicting_tasks(normalized):
            raise ValueError('Swarm worker scopes overlap')
        return normalized

    def create(self, objective, workers, *, session_slug, log, job_id=None):
        self._enabled()
        normalized = self._workers(objective, workers)
        valid_name(session_slug)
        if log is None:
            raise ValueError('Swarm jobs require a session ChatLog')
        if self.board.enabled and len(normalized) + 1 > self.config.board.max_artifacts:
            raise ValueError('Board artifact capacity is smaller than the job')
        job_id = uuid.uuid4().hex if job_id is None else job_id
        path = self._path(job_id)
        creation = log.redact({'objective': objective, 'workers': normalized, 'session_slug': session_slug})
        if self.board.enabled:
            self.board._text(creation['objective'])
        path.mkdir(parents=True, exist_ok=True)
        with self._locked(job_id):
            if (path / 'job.json').exists():
                job = self.get(job_id)
                if job['creation'] != creation:
                    raise ValueError('Swarm job id belongs to another request')
                return job
            job = {'id': job_id, **deepcopy(creation), 'creation': creation, 'status': 'queued', 'artifacts': {},
                   'merged': None, 'gate': None, 'created_at': time.time(), 'completed_count': 0, 'failed_count': 0}
            atomic_write(path / 'job.json', encoded(job))
            self._record(job, log, 'queued')
            atomic_write(path / 'job.json', encoded(job))
            return deepcopy(job)

    def _record(self, job, log, status):
        message = log.append('work', 'Swarm job ' + status, meta={'swarm_job_id': job['id'], 'status': status})
        if 'log_start' not in job:
            for earlier in log.iter():
                if earlier.meta.get('swarm_job_id') == job['id']:
                    job['log_start'] = earlier.i
                    break
        job['log_end'] = message.i
        if self.board.enabled:
            self.board.create(job['objective'], job['session_slug'], job['log_start'], job['log_start'], card_id=job['id'])
            attempt = 0
            for worker in job['workers']:
                task_id = worker.get('task_id')
                if task_id:
                    attempt = max(attempt, self.tasks.get(task_id)['attempt'])
            self.board.update_status(job['id'], status, attempt=attempt, actor=job['session_slug'],
                                     log_start=job['log_start'], log_end=job['log_end'])
            for reference in list(job['artifacts'].values()) + ([job['merged']] if job['merged'] else []):
                self.board.attach_artifact(job['id'], reference['ref'], actor=job['session_slug'])

    def _ensure_tasks(self, job):
        for index, worker in enumerate(job['workers']):
            task_id = job['id'] + '-' + str(index)
            worker['task_id'] = task_id
            try:
                task = self.tasks.get(task_id)
            except KeyError:
                self.tasks.create(worker['objective'], worker['scope'], worker['budget'], worker['account'], task_id=task_id)
                continue
            for field in ('objective', 'scope', 'budget', 'account'):
                if task[field] != worker[field]:
                    raise ValueError('Swarm task identity mismatch')

    def _save_artifact(self, job, filename, value):
        data = encoded(value)
        if len(data) > self.settings.max_artifact_bytes:
            raise BackendError('Swarm artifact exceeds configured bound', 'configuration')
        digest = hashlib.sha256(data).hexdigest()
        if filename is None:
            filename = 'merged-' + digest + '.json'
        path = self._path(job['id']) / filename
        if path.is_symlink():
            raise ValueError('Swarm artifact cannot be a symbolic link')
        if path.exists() and path.read_bytes() != data:
            raise ValueError('Swarm artifact already exists with different bytes')
        if not path.exists():
            atomic_write(path, data)
        return {'file': filename, 'sha256': digest, 'ref': 'swarm:' + job['id'] + ':' + filename + ':' + digest}

    def _read_artifact(self, job, reference):
        filename = reference['file']
        if not isinstance(filename, str) or Path(filename).name != filename:
            raise ValueError('Invalid swarm artifact name')
        path = self._path(job['id']) / filename
        if path.is_symlink():
            raise ValueError('Swarm artifact cannot be a symbolic link')
        try:
            data = path.read_bytes()
            if len(data) > self.settings.max_artifact_bytes or hashlib.sha256(data).hexdigest() != reference['sha256']:
                raise ValueError('Swarm artifact integrity check failed')
            return json.loads(data)
        except (OSError, json.JSONDecodeError):
            raise ValueError('Swarm artifact is missing or invalid') from None

    def artifact(self, job_id):
        job = self.get(job_id)
        for reference in job['artifacts'].values():
            self._read_artifact(job, reference)
        if not job['merged']:
            raise ValueError('Swarm job has no merged artifact')
        return self._read_artifact(job, job['merged'])

    def _publish_dispatch(self, path, job, index, state, attempt=None):
        with self._snapshot:
            worker = job['workers'][index]
            worker['dispatch'] = state
            if attempt is not None:
                worker['attempt'] = attempt
            atomic_write(path / 'job.json', encoded(job))

    def _launch(self, path, job, index, worker, log):
        task = self.tasks.get(worker['task_id'])
        if task['status'] != 'queued':
            return index, None, task

        def publish(state):
            self._publish_dispatch(path, job, index, state)

        requests_per_minute, tokens_per_minute = throttle_limits(self.config, worker['backend'])
        with model_slot(self.config, worker['backend'], publish):
            self._scheduler.enqueue(task['id'])
            # Pace before the lease. A wait longer than lease_seconds must not
            # make finish() reject the claim as stale. The pace is this model's,
            # not a swarm-wide cap. The slot and the pace each publish their own
            # wait. 'running' starts only after both have cleared.
            with endpoint_pace(self.config.home, worker['backend'], requests_per_minute, tokens_per_minute,
                               on_wait=publish) as charge:
                claim = self._scheduler.claim_next(worker['account'], 'job-' + job['id'], task_ids={task['id']},
                                                   backend=worker['backend'])
                if not claim:
                    publish('queued')
                    return index, None, self.tasks.get(task['id'])
                self._publish_dispatch(path, job, index, 'running', attempt=claim['attempt'])
                index, reference, finished = self._execute(job, index, worker, claim, log)
                if isinstance(finished, dict):
                    charge(finished.get('usage'))
        if finished is not None and finished['status'] in ('succeeded', 'failed', 'cancelled', 'queued'):
            self._publish_dispatch(path, job, index, finished['status'], attempt=finished.get('attempt'))
        return index, reference, finished

    def _execute(self, job, index, worker, claim, log):
        from .bundled_engine import require_memory_choice
        require_memory_choice(self.config)
        usage = None
        rates = None
        try:
            selected = replace(self.config, roles={'main': worker['backend']}).backend_for()
            # The account scheduler owns retries for this explicit job.
            selected.extra = {**selected.extra, 'retries': 0}
            factory = self.backend_factory
            backend = factory(selected) if factory else Backend(selected.name, selected.base_url, selected.model,
                                                               selected.api_key_env, selected.extra)
            if factory is None:
                # A substituted fake has no live config. The slot above still bounds it.
                bind = getattr(backend, 'bind_endpoint', None)
                if bind is not None:
                    bind(lambda: self.config, worker['backend'])
            response = backend.chat([{'role': 'system', 'content': 'Return evidence for your scoped task. Do not call tools.'},
                                     {'role': 'user', 'content': worker['objective']}], tools=[])
            usage = response.get('usage')
            rates = response.get('rate_limits')
            content = log.redact(response.get('content'))
            if (not isinstance(content, str) or not content.strip() or response.get('tool_calls')
                    or response.get('finish_reason') not in (None, 'stop')):
                raise BackendError('Swarm worker must return a nonempty artifact without tool calls', 'configuration')
            artifact = {'task_id': claim['id'], 'attempt': claim['attempt'], 'content': content}
            filename = 'worker-' + str(index) + '-' + str(claim['attempt']) + '.json'
            reference = self._save_artifact(job, filename, artifact)
            task = self._scheduler.complete(claim, result_ref=reference['ref'], usage=usage, rate_limits=rates)
            return index, reference, task
        except Exception as error:
            category = getattr(error, 'category', 'provider')
            try:
                task = self._scheduler.fail(claim, category, usage=usage, rate_limits=rates)
            except TaskError:
                # An expired or superseded lease cannot publish an artifact or be finished again.
                task = self.tasks.get(claim['id'])
            return index, None, task

    def run(self, job_id, *, log):
        self._enabled()
        from .bundled_engine import require_memory_choice
        require_memory_choice(self.config)
        with self._locked(job_id) as path:
            job = self.get(job_id)
            # Reject changed accounts instead of silently rerouting resumed work.
            request_workers = []
            for worker in job['creation']['workers']:
                request_workers.append({key: worker[key] for key in ('backend', 'objective', 'scope', 'budget')})
            current = self._workers(job['objective'], request_workers)
            for worker, selected in zip(job['workers'], current):
                for field in ('backend', 'objective', 'scope', 'budget', 'account', 'binding'):
                    if worker[field] != selected[field]:
                        raise ValueError('Swarm job request or account configuration changed')
            if job['status'] in ('succeeded', 'failed'):
                if job['status'] == 'succeeded':
                    self.artifact(job_id)
                return job
            self._ensure_tasks(job)
            with self._scheduler_lock:
                if self._scheduler is None:
                    self._scheduler = Scheduler.from_config(self.config, human=self.human)
            self.tasks.recover_expired(max_attempts=self.settings.max_attempts)
            kit = JudgeKit.from_config(self.config, enabled=self.settings.gates_enabled and self.config.jev.enabled,
                                       decision_log=path / 'decisions.jsonl')
            admission = kit.swarm_gates(job['objective'], tasks=job['workers'], phase='admission', human=self.human)
            if not admission['allowed']:
                job.update(status='blocked', gate=admission)
                self._record(job, log, 'blocked')
                atomic_write(path / 'job.json', encoded(job))
                return job
            job['status'] = 'running'
            self._record(job, log, 'running')
            atomic_write(path / 'job.json', encoded(job))
            # Every worker starts. A model endpoint, not the swarm, decides who runs.
            with ThreadPoolExecutor(max_workers=max(1, len(job['workers']))) as executor:
                futures = []
                for index, worker in enumerate(job['workers']):
                    task = self.tasks.get(worker['task_id'])
                    if task['status'] != 'queued':
                        continue
                    futures.append(executor.submit(self._launch, path, job, index, worker, log))
                for future in futures:
                    index, reference, task = future.result()
                    if reference is not None and task is not None and task['status'] == 'succeeded':
                        job['artifacts'][str(index)] = reference
                    atomic_write(path / 'job.json', encoded(job))
            artifacts = []
            all_succeeded = True
            failed = False
            job['completed_count'] = 0
            job['failed_count'] = 0
            for index, worker in enumerate(job['workers']):
                task = self.tasks.get(worker['task_id'])
                failed = failed or task['status'] in ('failed', 'cancelled')
                all_succeeded = all_succeeded and task['status'] == 'succeeded'
                job['completed_count'] += int(task['status'] == 'succeeded')
                job['failed_count'] += int(task['status'] in ('failed', 'cancelled'))
                reference = job['artifacts'].get(str(index))
                # Recover a committed child if the process died before saving its parent snapshot.
                if reference is None and task['status'] == 'succeeded':
                    filename = 'worker-' + str(index) + '-' + str(task['attempt']) + '.json'
                    saved = path / filename
                    digest = hashlib.sha256(saved.read_bytes()).hexdigest() if saved.exists() else ''
                    reference = {'file': filename, 'sha256': digest, 'ref': task['result_ref']}
                    if reference['ref'] != 'swarm:' + job_id + ':' + filename + ':' + digest:
                        raise ValueError('Swarm task artifact reference mismatch')
                    job['artifacts'][str(index)] = reference
                if reference:
                    artifact = self._read_artifact(job, reference)
                    if artifact['task_id'] != task['id'] or artifact['attempt'] != task['attempt']:
                        raise ValueError('Swarm artifact task identity mismatch')
                    artifacts.append(artifact)
            if all_succeeded and job['merged'] is not None:
                merged = self.artifact(job_id)
                if (merged.get('objective') != job['objective'] or merged.get('artifacts') != artifacts
                        or merged.get('gate', {}).get('allowed') is not True):
                    raise ValueError('Swarm merge checkpoint does not match its child evidence')
                job['gate'] = merged['gate']
                job['status'] = 'succeeded'
            elif all_succeeded:
                checks = {'all_workers_succeeded': True, 'artifact_hashes_valid': True,
                          'all_artifacts_present': len(artifacts) == len(job['workers'])}
                body = {'objective': job['objective'], 'artifacts': artifacts, 'checks': checks}
                checks['merge_body_within_bound'] = len(encoded(body)) <= self.settings.max_artifact_bytes
                gate = kit.swarm_gates(job['objective'], tasks=job['workers'], artifacts=artifacts,
                                       checks=checks, phase='merge', human=self.human)
                job['gate'] = gate
                if gate['allowed']:
                    merged = {'objective': job['objective'], 'artifacts': artifacts, 'checks': checks, 'gate': gate}
                    if len(encoded(merged)) > self.settings.max_artifact_bytes:
                        checks['merge_within_bound'] = False
                        job['gate'] = kit.swarm_gates(job['objective'], tasks=job['workers'], artifacts=artifacts,
                                                      checks=checks, phase='merge', human=self.human)
                        job['status'] = 'blocked'
                    else:
                        job['merged'] = self._save_artifact(job, None, merged)
                        job['status'] = 'succeeded'
                else:
                    job['status'] = 'blocked'
            else:
                job['status'] = 'failed' if failed else 'blocked'
            if job['merged'] is not None:
                # A durable accepted merge precedes cross-store board publication.
                # Recovery reuses this gate, even if a semantic judge would answer differently.
                checkpoint = deepcopy(job)
                checkpoint['status'] = 'running'
                atomic_write(path / 'job.json', encoded(checkpoint))
            self._record(job, log, job['status'])
            atomic_write(path / 'job.json', encoded(job))
            return deepcopy(job)
