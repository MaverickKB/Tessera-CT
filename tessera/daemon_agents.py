"""Explicit policy-gated daemon sessions, fenced attempts and durable work links."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import re
import threading
import time
import uuid

from .agent_budget import AgentBackend
from .agent_selection import select_backend
from .agent_edits import apply_edits, edit_prompt
from .agent_planning import AgentPlanning
from .agent_worktrees import AgentWorktrees
from .backend import BackendError
from .gates import scopes_overlap
from .jev import JudgeKit
from .jobs import atomic_write, encoded
from .judge_backend import BackendJudge
from .loop import Session
from .passive_status import configured_path
from .scheduler import Scheduler, SchedulerHooks
from .rate_limits import day_key
from .sessions import EventJournal
from .tasks import TaskError
from .tree import utf8_prefix


class SessionDelegation:
    """The daemon's single-task tool cannot bypass its multi-task admission gate."""
    def __init__(self, session):
        self.session = session

    def spawn(self, name, task, tools=None, backend=None, capability='routine'):
        raise ValueError('Daemon delegation requires a gated agent/spawn candidate plan')

    def wait(self, timeout=None):
        return True

    def wait_for_reports(self, timeout=None):
        return 'No worker reports.'

    def active(self):
        return 0


class AgentPolicy(SchedulerHooks):
    def __init__(self, config):
        super().__init__(config.accounts, config.swarm, enabled=True)

    def admit(self, task, tasks):
        active = [other for other in tasks if other['status'] == 'running'
                  and other['lease_until'] > self.clock()]
        if any(scopes_overlap(task['scope'], other['scope']) for other in active):
            return False
        return super().admit(task, tasks)


class DaemonAgents:
    def __init__(self, daemon):
        self.daemon = daemon
        self.config = daemon.config
        self.settings = self.config.agents
        self.root = configured_path(self.config.home, self.settings.runs_dir)
        self._lock = threading.RLock()
        self._runs = {}
        self._claims = {}
        self._scheduler = None
        self.owner = 'agent-' + uuid.uuid4().hex
        self._running = set()
        self.worktrees = AgentWorktrees(self.config)
        self.planning = AgentPlanning(self)

    @property
    def enabled(self):
        return (self.settings.enabled and self.config.swarm.enabled
                and self.settings.token_budget > 0 and self.settings.wall_seconds > 0)

    def require_enabled(self):
        if not self.enabled:
            raise ValueError('Daemon session agents are disabled or lack execution budgets')

    def _path(self, run_id):
        if not isinstance(run_id, str) or not re.fullmatch('[a-f0-9]{32}', run_id):
            raise ValueError('Invalid agent run id')
        path = self.root / run_id
        if self.root.is_symlink() or path.is_symlink():
            raise ValueError('Agent run paths cannot be symbolic links')
        return path

    def _save(self, run):
        data = encoded(run)
        if len(data) > self.config.swarm.max_artifact_bytes:
            raise ValueError('Agent run exceeds configured artifact bound')
        path = self._path(run['id'])
        path.mkdir(parents=True, exist_ok=True)
        atomic_write(path / 'run.json', data)
        self._runs[run['id']] = run

    def show(self, run_id):
        self.require_enabled()
        with self._lock:
            data = (self._path(run_id) / 'run.json').read_bytes()
            if len(data) > self.config.swarm.max_artifact_bytes:
                raise ValueError('Agent run exceeds configured artifact bound')
            return json.loads(data)

    def list(self, parent=None):
        if not self.enabled:
            return []
        result = []
        for path in sorted(self.root.glob('*/run.json')):
            run = self.show(path.parent.name)
            if parent is None or run['parentThreadId'] == parent:
                result.append(run)
        return result

    def _worker(self, slug):
        for run in self.list():
            for worker in run['workers']:
                if worker['threadId'] == slug:
                    return run, worker
        return None, None

    def is_child(self, slug):
        manager = self.daemon.manager
        if manager is not None and manager._metadata.get(slug, {}).get('agent'):
            return True
        return False

    def allow_reply(self, slug):
        return not self.is_child(slug)

    def session(self, config, backend=None, **options):
        slug = Path(options['root']).name
        run, worker = self._worker(slug) if self.is_child(slug) else (None, None)
        options['agent_factory'] = SessionDelegation
        if worker is not None:
            task_id = worker['taskId']
            selected = replace(config, roles={'main': worker['backend']}).backend_for()
            extra = deepcopy(selected.extra)
            if extra.get('provider_kind') == 'cli_worker':
                cli = extra.setdefault('cli', {})
                # The CLI speaks the engine tool protocol. Without this it rejects the tool list.
                cli['engine_tools'] = True
            if 'workspace' in worker:
                worktree = Path(worker['workspace']['path'])
                config = replace(config, cwd=worktree, workspace=str(worktree))
                if extra.get('provider_kind') == 'cli_worker':
                    extra.setdefault('cli', {})['cwd'] = str(config.cwd)
            selected = replace(selected, extra=extra)
            backend = AgentBackend(selected, lambda: self._context(task_id),
                                   lambda usage, reported: self._observe(run['id'], task_id, usage, reported),
                                   delegate=backend)
            backend.bind_endpoint(lambda: self.config, worker['backend'])
            # Judge stays off so a child does not spend an unreserved judge call.
            # Tools stay on. An empty allowlist was the text-only stop.
            if run is None:
                raise RuntimeError('Worker run is missing')
            config = replace(config, jev=replace(config.jev, enabled=False), roles={'main': worker['backend']})
            options.update(depth=run['depth'], inherited_view=run['parent_view'])
        return Session(config, backend=backend, **options)

    def _scheduler_instance(self):
        with self._lock:
            if self._scheduler is None:
                config = replace(self.config, swarm=replace(self.config.swarm, gates_enabled=False, max_attempts=1))
                self._scheduler = Scheduler.from_config(config, hooks=AgentPolicy(config))
            return self._scheduler

    def _context(self, task_id):
        with self._lock:
            claim = self._claims.get(task_id)
            if claim is None:
                raise BackendError('Agent requires an active coordinator claim', 'configuration')
            task = self._scheduler.store.get(task_id)
            if (task['status'] != 'running' or task['owner'] != self.owner
                    or task['attempt'] != claim['task']['attempt'] or task['lease_until'] <= time.time()):
                raise BackendError('Agent claim is no longer active', 'configuration')
            return {'tokens': claim['task']['budget']['tokens'] - claim['usage'].get('tokens', 0),
                    'seconds': claim['deadline'] - time.time()}

    def _observe(self, run_id, task_id, usage, reported):
        with self._lock:
            entry = self._claims[task_id]
            for unit in ('tokens', 'seconds'):
                entry['usage'][unit] = entry['usage'].get(unit, 0) + usage.get(unit, 0)
            run = self._runs[run_id]
            run['charges'][task_id] = {'usage': dict(entry['usage']), 'tokens_reported': bool(reported)}
            run['usage'] = {unit: sum(c['usage'].get(unit, 0) for c in run['charges'].values())
                            for unit in ('tokens', 'seconds')}
            self._save(run)

    def _activate(self, run, task):
        self._claims[task['id']] = {'task': task, 'deadline': min(run['deadline'], time.time() + task['budget']['seconds']),
                                    'usage': {}}

    def _event(self, slug, event, identity, data):
        manager = self.daemon._manager()
        with manager._lock:
            runtime = manager._runtimes.get(slug)
            journal = runtime.events if runtime else EventJournal(manager.path(slug) / 'events.jsonl',
                observer=lambda record: self.daemon._observe_event(slug, record))
            if not any(r['event'] == event and r['data'].get('agent_event_id') == identity for r in journal.records):
                journal.append(event, {'agent_event_id': identity, **data})
            if runtime is None:
                journal.close()

    def _log_landing(self, run, worker):
        """Write the same landing receipt the terminal prints into the owning session."""
        from .terminal import Terminal
        lines = Terminal.landing_lines(worker)
        self._log(run['parentThreadId'], '\n'.join(lines), {
            'agent_run_id': run['id'],
            'child_thread_id': worker.get('threadId'),
            'landing_receipt': True,
            'landing_lines': lines,
        }, 'landing-' + str(worker.get('threadId') or ''))

    def _log(self, slug, text, meta, identity):
        manager = self.daemon._manager()
        with manager._swarm_log(slug) as log:
            if not any(m.meta.get('agent_event_id') == identity for m in log.iter()):
                log.append('work', text, {**meta, 'agent_event_id': identity})

    def _binding(self, name):
        selected = replace(self.config, roles={'main': name}).backend_for()
        return hashlib.sha256(encoded(asdict(selected))).hexdigest()

    def _normalize(self, workers):
        if not isinstance(workers, list):
            raise ValueError('Candidate workers must be a list')
        gate_count = len(workers) + 1 if self.settings.worktrees_enabled else 2
        gate_reserve = gate_count * self.settings.judge_tokens if self.config.jev.enabled else 0
        available = self.settings.token_budget - gate_reserve - self.settings.planner_tokens
        if available <= 0:
            raise ValueError('Agent token budget does not cover judge reservations')
        result = []
        for index, worker in enumerate(workers):
            if not isinstance(worker, dict) or set(worker) - {'objective', 'scope', 'backend', 'budget', 'dependsOn', 'capability'}:
                raise ValueError('Invalid agent candidate fields')
            objective, scope = worker.get('objective'), worker.get('scope')
            if not isinstance(objective, str) or not objective.strip() or not isinstance(scope, list) or not scope:
                raise ValueError('Agent candidates require objective and scope')
            if any(not isinstance(s, str) or not s.strip() for s in scope):
                raise ValueError('Agent scopes must be nonempty text')
            if self.settings.worktrees_enabled:
                for item_scope in scope:
                    if (Path(item_scope).is_absolute() or '..' in Path(item_scope).parts
                            or not any(item_scope == a.rstrip('/') or item_scope.startswith(a.rstrip('/') + '/')
                                       for a in self.settings.allowed_scopes)):
                        raise ValueError('Agent scope is outside the configured worktree allowlist')
                scope = worker['scope']
            name = select_backend(self.config, worker.get('backend'), worker.get('capability', 'routine'))
            if not isinstance(name, str) or name not in self.config.backends:
                raise ValueError('Agent backend is not configured')
            account = self.config.backends[name].account
            if not account or account not in self.config.accounts:
                raise ValueError('Agent workers require a named account pool')
            budget = dict(worker.get('budget', {}))
            if set(budget) - {'tokens', 'seconds'}:
                raise ValueError('Agent budgets support tokens and seconds')
            budget.setdefault('tokens', available // max(1, len(workers)))
            budget.setdefault('seconds', self.settings.wall_seconds)
            if (type(budget['tokens']) is not int or budget['tokens'] <= 0
                    or type(budget['seconds']) not in (int, float) or not 0 < budget['seconds'] <= self.settings.wall_seconds):
                raise ValueError('Agent budgets must be positive and within configured bounds')
            dependencies = worker.get('dependsOn', [])
            if not isinstance(dependencies, list) or any(type(i) is not int or not 0 <= i < len(workers) for i in dependencies):
                raise ValueError('Invalid agent dependency indices')
            result.append({'objective': objective, 'scope': list(scope), 'backend': name, 'account': account,
                           'budget': budget, 'dependsOn': dependencies, 'binding': self._binding(name)})
        if sum(w['budget']['tokens'] for w in result) > available:
            raise ValueError('Worker reservations exceed configured agent token budget')
        return result

    def _preflight(self, workers):
        planned = [{'account': w['account'], 'budget': w['budget']} for w in workers]
        if self.config.jev.enabled:
            judge = self.config.backend_for('jev')
            if 'jev' not in self.config.roles or not judge.account or self.settings.judge_tokens <= 0:
                raise ValueError('Semantic agent gates require a budgeted configured Jev account')
            planned += [{'account': judge.account, 'budget': {'tokens': self.settings.judge_tokens,
                         'seconds': self.settings.wall_seconds}}] * (len(workers) + 1 if self.settings.worktrees_enabled else 2)
        if not any(self.config.accounts[p['account']].budget for p in planned):
            return
        ledger = self._scheduler_instance().store.list()
        today = day_key(time.time())
        for account_name in {p['account'] for p in planned}:
            for option, limit in self.config.accounts[account_name].budget.items():
                daily = option.endswith('_per_day')
                unit = option.removesuffix('_per_day')
                requests = [p['budget'].get(unit) for p in planned if p['account'] == account_name]
                if any(amount is None for amount in requests):
                    raise ValueError('Agent plan lacks a unit required by its account budget')
                spent = 0
                for task in ledger:
                    if task['account'] != account_name:
                        continue
                    usage = task.get('daily_usage', {}).get(today, {}) if daily else task.get('usage', {})
                    spent += usage.get(unit, 0)
                    if task['status'] == 'running':
                        spent += task.get('reservation', {}).get(unit, 0)
                if spent + sum(requests) > limit:
                    raise ValueError('Agent plan reservations exceed its account budget')

    def _gate(self, run, phase, *, artifacts=None, checks=None, identity=None, objective=None, tasks=None):
        tasks = run['creation']['workers'] if tasks is None else tasks
        objective = run['objective'] if objective is None else objective
        identity = identity or phase
        path = self._path(run['id'])
        preliminary = JudgeKit.from_config(self.config, enabled=False, evaluator=None,
                                           decision_log=path / 'decisions.jsonl')
        kwargs = {'tasks': tasks, 'artifacts': artifacts or [], 'checks': checks, 'phase': phase}
        deterministic = preliminary.swarm_gates(objective, **kwargs)
        if deterministic['source'] == 'deterministic':
            return deterministic
        if phase == 'admission':
            self._preflight(tasks)
        if not self.config.jev.enabled:
            return deterministic
        if 'jev' not in self.config.roles or self.settings.judge_tokens <= 0:
            raise ValueError('Semantic agent gates require a budgeted configured Jev backend role')
        selected = self.config.backend_for('jev')
        if not selected.account:
            raise ValueError('Agent judge requires a named account pool')
        scheduler = self._scheduler_instance()
        task_id = 'agent-' + run['id'] + '-gate-' + identity
        try:
            task = scheduler.store.get(task_id)
        except KeyError:
            task = scheduler.store.create('Agent ' + phase + ' gate', ['agent-gate-' + run['id']],
                                          {'tokens': self.settings.judge_tokens, 'seconds': self.settings.wall_seconds},
                                          selected.account, task_id=task_id)
        if task['status'] != 'queued':
            # A lost semantic receipt never grants permission or repeats a billable call.
            if task['status'] == 'running':
                task = scheduler.store.finish(task_id, task['owner'], task['attempt'], 'failed',
                                              error_category='timeout', usage=task.get('reservation', {}))
            self._record_charge(run, task)
            return {**deterministic, 'allowed': False, 'source': 'unavailable'}
        scheduler.enqueue(task_id)
        claim = scheduler.claim_next(selected.account, self.owner, task_ids={task_id}, backend=selected.name)
        if claim is None:
            return {**deterministic, 'allowed': False, 'source': 'unavailable'}
        with self._lock:
            self._activate(run, claim)
        try:
            evaluator = BackendJudge(selected)
            evaluator.backend = AgentBackend(selected, lambda: self._context(task_id),
                                            lambda usage, reported: self._observe(run['id'], task_id, usage, reported))
            kit = JudgeKit.from_config(self.config, evaluator=evaluator, decision_log=path / 'decisions.jsonl')
            gate = kit.swarm_gates(objective, human=lambda d: self.daemon._human(run['parentThreadId'], None, d), **kwargs)
            usage = self._claims[task_id]['usage']
            finished = scheduler.complete(claim, usage=usage)
            self._record_charge(run, finished)
            if gate['source'] != 'jev' or finished['status'] != 'succeeded':
                gate.update(allowed=False, source='unavailable')
            run[identity + '_gate'] = gate
            with self._lock:
                self._save(run)
            return gate
        except Exception:
            scheduler.fail(claim, 'provider', usage=self._claims[task_id]['usage'] or None)
            raise
        finally:
            with self._lock:
                self._claims.pop(task_id, None)

    def spawn(self, parent, objective, workers, request_id=None, *, deadline=None):
        self.require_enabled()
        self.daemon._thread(parent)
        if not isinstance(objective, str) or not objective.strip():
            raise ValueError('Agent objective must be nonempty text')
        manager = self.daemon._manager()
        objective = manager.messaging._redact(objective)
        normalized = self._normalize(workers)
        for worker in normalized:
            worker['objective'] = manager.messaging._redact(worker['objective'])
            worker['scope'] = [manager.messaging._redact(s) for s in worker['scope']]
        parent_meta = manager._metadata[parent]
        depth = parent_meta.get('agent', {}).get('depth', 0) + 1
        if depth > self.settings.max_depth:
            raise ValueError('Agent depth limit reached')
        run_id = uuid.uuid4().hex if request_id is None else request_id
        path = self._path(run_id)
        creation = {'parentThreadId': parent, 'objective': objective, 'workers': normalized}
        if (path / 'run.json').exists():
            saved = self.show(run_id)
            if saved['creation'] != creation:
                raise ValueError('Agent request identity belongs to another plan')
            return saved
        run = {'id': run_id, 'parentThreadId': parent, 'objective': objective, 'creation': creation,
               'depth': depth, 'mode': 'single', 'status': 'single', 'workers': [],
               'admission_gate': None, 'merge_gate': None, 'merged': None, 'usage': {}, 'charges': {},
               'deadline': min(time.time() + self.settings.wall_seconds, deadline if deadline is not None else float('inf')), 'parent_view': '',
               'judge_binding': self._binding(self.config.roles['jev']) if self.config.jev.enabled and 'jev' in self.config.roles else None}
        # A durable plan precedes any provider call or child creation.
        with self._lock:
            self._save(run)
        single = len(normalized) < 2 or any(w['dependsOn'] for w in normalized)
        if single:
            gate = {'allowed': False, 'source': 'deterministic', 'reason': 'single_or_dependent'}
        else:
            gate = self._gate(run, 'admission')
        run['admission_gate'] = gate
        if not single and gate['allowed']:
            with self.daemon._surface(parent) as (log, view):
                run['parent_view'] = utf8_prefix(view.render(), self.settings.view_bytes)
            run.update(mode='parallel', status='queued')
            for index, worker in enumerate(normalized):
                run['workers'].append({**worker, 'threadId': 'agent-' + run_id + '-' + str(index),
                                       'taskId': 'agent-' + run_id + '-' + str(index), 'turnId': None,
                                       'artifact': None})
        with self._lock:
            self._save(run)
        if run['mode'] == 'parallel':
            self._ensure_children(run)
        self._event(parent, 'agent/admission', run_id, {'run_id': run_id, 'mode': run['mode'], 'gate': gate})
        return deepcopy(run)

    def _ensure_children(self, run):
        self.worktrees.ensure(run, lambda: self._save(run))
        manager = self.daemon._manager()
        for worker in run['workers']:
            slug = worker['threadId']
            identity = {'runId': run['id'], 'parentThreadId': run['parentThreadId'], 'depth': run['depth']}
            with manager._lock:
                if slug not in manager._metadata:
                    row = manager.create(slug, worker['backend'], agent=identity)
                    if row['slug'] != slug:
                        raise ValueError('Agent session identity collision')
                elif manager._metadata[slug].get('agent') != identity:
                    raise ValueError('Agent session belongs to another plan')
            metadata = {'agent_run_id': run['id'], 'parent_thread_id': run['parentThreadId'],
                        'child_thread_id': slug, 'depth': run['depth']}
            for target in (run['parentThreadId'], slug):
                self._log(target, 'Agent assignment: ' + worker['objective'], metadata, 'link-' + slug)
                self._event(target, 'agent/link', 'link-' + slug, metadata)

    def _artifact(self, run, worker, content, attempt):
        artifact = {'threadId': worker['threadId'], 'taskId': worker['taskId'], 'attempt': attempt,
                    'turnId': worker['turnId'], 'content': content}
        data = encoded(artifact)
        if len(data) > self.config.swarm.max_artifact_bytes:
            raise ValueError('Agent result exceeds artifact bound')
        name = worker['taskId'] + '-' + str(attempt) + '.json'
        path = self._path(run['id']) / name
        if path.is_symlink() or (path.exists() and path.read_bytes() != data):
            raise ValueError('Agent artifact identity mismatch')
        if not path.exists():
            atomic_write(path, data)
        return {**artifact, 'file': name, 'sha256': hashlib.sha256(data).hexdigest()}

    def _read_artifact(self, run, artifact, worker=None):
        if worker is not None:
            task = self._scheduler_instance().store.get(worker['taskId'])
            if (task['status'] != 'succeeded' or task['result_ref'] != artifact['sha256']
                    or task['attempt'] != artifact['attempt']
                    or any(worker[k] != artifact[k] for k in ('taskId', 'threadId', 'turnId'))):
                raise ValueError('Agent artifact does not match its successful task claim')
        filename = artifact['file']
        if Path(filename).name != filename:
            raise ValueError('Invalid agent artifact file')
        path = self._path(run['id']) / filename
        if path.is_symlink():
            raise ValueError('Agent artifact cannot be a symbolic link')
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != artifact['sha256']:
            raise ValueError('Agent artifact integrity failed')
        body = json.loads(data)
        if any(body[k] != artifact[k] for k in ('threadId', 'taskId', 'attempt', 'turnId', 'content')):
            raise ValueError('Agent artifact evidence mismatch')
        return body

    def _execute(self, run, worker, claim):
        scheduler = self._scheduler_instance()
        task_id = worker['taskId']
        manager = self.daemon._manager()
        with self._lock:
            self._activate(run, claim)
        try:
            runtime = manager.runtime(worker['threadId'])
            self.daemon._watch(runtime)
            # A restored queued turn is never submitted a second time.
            turn_id = worker['turnId']
            if turn_id is None:
                turn_id = uuid.uuid4().hex
                with self._lock:
                    worker['turnId'] = turn_id
                    self._save(run)
            if self.settings.worktrees_enabled and self.settings.worktree_result_format == 'edits_json':
                text = edit_prompt(self.config, worker)
                digest = hashlib.sha256(text.encode()).hexdigest()
                if worker.get('execution_input_sha256', digest) != digest:
                    raise ValueError('Structured worker input changed')
                # The durable turn journal owns the full input, not run metadata.
                worker['execution_input_sha256'] = digest
                self._save(run)
            else:
                text = worker['objective']
            runtime.enqueue(text, turn_id=turn_id)
            remaining = max(0, run['deadline'] - time.time())
            completed = manager.wait(worker['threadId'], timeout=remaining)
            if not completed:
                runtime.wait()  # Provider timeout is already clipped to the deadline.
                raise BackendError('Agent wall budget exhausted', 'timeout')
            if manager.turn(worker['threadId'], turn_id)['state'] != 'done':
                errors = [r['data'] for r in runtime.events.records
                          if r['event'] == 'error' and r['data'].get('turn_id') == turn_id]
                category = errors[-1].get('error_category', 'provider') if errors else 'provider'
                if self._claims[task_id]['usage'].get('tokens', 0) > claim['budget']['tokens']:
                    category = 'quota'
                raise BackendError('Agent turn failed', category)
            auth_path = getattr(runtime.session.backend.worker, 'auth_path', '')
            if auth_path in ('cli_login', 'api_key_fallback'):
                worker['auth_path'] = auth_path
                self._save(run)
            users = [m for m in runtime.session.log.iter() if m.kind == 'user' and m.meta.get('request_id') == turn_id]
            if len(users) != 1:
                raise BackendError('Agent turn provenance is incomplete', 'provider')
            rows = [m for m in runtime.session.log.iter(users[0].i + 1)
                    if m.kind == 'agent' and m.meta.get('turn') == users[0].meta['turn']]
            if not rows or not rows[-1].text.strip() or rows[-1].meta.get('tool_calls'):
                raise BackendError('Agent result is incomplete', 'provider')
            if self.settings.worktrees_enabled and self.settings.worktree_result_format == 'edits_json':
                worker['model_edits'] = apply_edits(self.config, worker, rows[-1].text)
            implementation = self.worktrees.finish(run, worker) if self.settings.worktrees_enabled else None
            artifact = self._artifact(run, worker, rows[-1].text, claim['attempt'])
            usage = self._claims[task_id]['usage']
            task = scheduler.complete(claim, result_ref=artifact['sha256'], usage=usage)
            if task['status'] == 'succeeded':
                if self.settings.worktrees_enabled:
                    worker['implementation'] = implementation
                with self._lock:
                    worker['artifact'] = artifact
                    self._save(run)
            return task
        except Exception as error:
            usage = self._claims[task_id]['usage']
            self._record_worker_failure(run, worker, error)
            try:
                return scheduler.fail(claim, error.category if isinstance(error, BackendError) else 'provider', usage=usage or None)
            except TaskError:
                return scheduler.store.get(task_id)
        finally:
            with self._lock:
                self._claims.pop(task_id, None)

    def _record_worker_failure(self, run, worker, error):
        """Operator evidence for a failed child: real category, exit code, redacted stderr tail."""
        try:
            runtime = self.daemon._manager()._runtimes.get(worker['threadId'])
            cli = getattr(getattr(getattr(runtime, 'session', None), 'backend', None), 'worker', None)
            failure = dict(getattr(cli, 'last_failure', None) or {})
            redact = self.daemon._manager().messaging._redact
            record = {'error': redact(str(error))[:500],
                      'category': getattr(error, 'category', None) or failure.get('category')}
            for key in ('reason', 'exit_code'):
                if key in failure:
                    record[key] = failure[key]
            for key in ('stderr_tail', 'stdout_tail'):
                if failure.get(key):
                    record[key] = redact(failure[key])[-1024:]
            with self._lock:
                worker['failure'] = record
                self._save(run)
        except Exception:
            pass  # Evidence capture must never mask the original failure.

    def run(self, run_id):
        self.require_enabled()
        with self._lock:
            if run_id in self._running:
                raise ValueError('Agent run is already executing')
            self._running.add(run_id)
        try:
            return self._run(run_id)
        finally:
            with self._lock:
                self._running.remove(run_id)

    def _run(self, run_id):
        run = self.show(run_id)
        if run['mode'] == 'single':
            return run
        if run['status'] == 'succeeded':
            for worker in run['workers']:
                self._read_artifact(run, worker['artifact'], worker)
            self._publish(run)
            return run
        if run['status'] == 'failed':
            return run
        if run.get('judge_binding') is not None and (not self.config.jev.enabled
                or 'jev' not in self.config.roles or self._binding(self.config.roles['jev']) != run['judge_binding']):
            raise ValueError('Agent judge binding changed')
        for worker in run['workers']:
            if self._binding(worker['backend']) != worker['binding']:
                raise ValueError('Agent provider binding changed')
        with self._lock:
            self._runs[run_id] = run
        self._ensure_children(run)
        scheduler = self._scheduler_instance()
        scheduler.store.recover_expired(max_attempts=1)
        run['status'] = 'running'
        with self._lock:
            self._save(run)
        pending = []
        capacity = len(run['workers'])
        for worker in run['workers']:
            task_id = worker['taskId']
            try:
                task = scheduler.store.get(task_id)
            except KeyError:
                task = scheduler.store.create(worker['objective'], worker['scope'], worker['budget'],
                                               worker['account'], task_id=task_id)
            if task['status'] == 'running':
                # Explicit recovery fences an uncertain previous process attempt.
                scheduler.store.finish(task_id, task['owner'], task['attempt'], 'failed', error_category='timeout',
                                       usage=task.get('reservation', {}))
                continue
            if task['status'] == 'queued' and len(pending) < capacity and time.time() < run['deadline']:
                scheduler.enqueue(task_id)
                claim = scheduler.claim_next(worker['account'], self.owner, task_ids={task_id}, backend=worker['backend'])
                if claim:
                    pending.append((worker, claim))
        with ThreadPoolExecutor(max_workers=max(1, len(pending))) as pool:
            futures = [pool.submit(self._execute, run, worker, claim) for worker, claim in pending]
            for future in futures:
                future.result()
        tasks = [scheduler.store.get(w['taskId']) for w in run['workers']]
        for worker, task in zip(run['workers'], tasks):
            worker.update(status=task['status'], attempt=task['attempt'], error_category=task['error_category'])
            self._record_charge(run, task)
            if task['status'] == 'succeeded' and worker['artifact'] is None:
                name = worker['taskId'] + '-' + str(task['attempt']) + '.json'
                path = self._path(run_id) / name
                if path.is_symlink():
                    raise ValueError('Agent artifact cannot be a symbolic link')
                body = json.loads(path.read_bytes())
                worker['artifact'] = {**body, 'file': name, 'sha256': task['result_ref']}
        if all(t['status'] == 'succeeded' for t in tasks) and self.settings.worktrees_enabled:
            run['status'] = 'ready'
            self._save(run)
            return deepcopy(run)
        if all(t['status'] == 'succeeded' for t in tasks):
            artifacts = [self._read_artifact(run, w['artifact'], w) for w in run['workers']]
            checks = {'all_workers_succeeded': True, 'artifact_hashes_valid': True,
                      'all_artifacts_present': len(artifacts) == len(run['workers'])}
            gate = run.get('merge_gate') or self._gate(run, 'merge', artifacts=artifacts, checks=checks)
            run['merge_gate'] = gate
            if gate['allowed']:
                run['merged'] = {'artifacts': artifacts, 'gate': gate}
                run['status'] = 'succeeded'
                with self._lock:
                    self._save(run)
                self._publish(run)
            else:
                run['status'] = 'blocked'
        else:
            run['status'] = 'failed' if any(t['status'] in ('failed', 'cancelled') for t in tasks) or time.time() >= run['deadline'] else 'blocked'
        with self._lock:
            self._save(run)
        return deepcopy(run)

    def merge(self, run_id):
        self.require_enabled()
        if not self.settings.worktrees_enabled:
            raise ValueError('Agent worktree merges are disabled')
        with self._lock:
            if run_id in self._running:
                raise ValueError('Agent run is already executing')
            self._running.add(run_id)
        try:
            run = self.show(run_id)
            if run['mode'] != 'parallel' or 'workspace' not in run:
                raise ValueError('No admitted worktree plan to merge')
            if run.get('judge_binding') is not None and (not self.config.jev.enabled
                    or 'jev' not in self.config.roles or self._binding(self.config.roles['jev']) != run['judge_binding']):
                raise ValueError('Agent judge binding changed')
            self.worktrees.ensure(run, lambda: self._save(run))
            self._runs[run_id] = run
            accepted = []
            for index, worker in enumerate(run['workers']):
                if self._binding(worker['backend']) != worker['binding']:
                    raise ValueError('Agent provider binding changed')
                task = self._scheduler_instance().store.get(worker['taskId'])
                if task['status'] != 'succeeded' or worker['artifact'] is None:
                    continue
                artifact = self._read_artifact(run, worker['artifact'], worker)
                def judge(proof, diff, checks):
                    evidence = {'content': encoded({'implementation': proof, 'diff': diff,
                                                   'worker_result': artifact['content']}).decode()}
                    return self._gate(run, 'merge', identity='merge-' + str(index),
                                      objective=worker['objective'], tasks=[worker], artifacts=[evidence], checks=checks)
                try:
                    landing = self.worktrees.land(run, worker, judge)
                except ValueError as error:
                    landing = {'status': 'blocked', 'reason': str(error),
                               'gate': {'allowed': False, 'source': 'deterministic'}}
                worker['landing'] = landing
                self._save(run)
                self._log_landing(run, worker)
                if landing['status'] == 'merged':
                    accepted.append(artifact)
                    meta = {'agent_run_id': run_id, 'child_thread_id': worker['threadId'],
                            'commit': landing['commit'], 'child_commit': landing['child_commit'], 'gate': landing['gate']}
                    identity = 'piece-' + worker['threadId']
                    self._log(run['parentThreadId'], 'Validated child merge: ' + worker['objective'], meta, identity)
                    self._event(run['parentThreadId'], 'agent/merge', identity, meta)
            complete = len(accepted) == len(run['workers'])
            run['status'] = 'succeeded' if complete else 'partial' if accepted else 'blocked'
            run['merge_gate'] = {'allowed': complete, 'source': 'per_piece'}
            run['merged'] = {'artifacts': accepted, 'gate': run['merge_gate']} if accepted else None
            self._save(run)
            if complete:
                self._publish(run)
            return deepcopy(run)
        finally:
            with self._lock:
                self._running.remove(run_id)

    def _record_charge(self, run, task):
        if task['status'] not in ('succeeded', 'failed', 'cancelled') or task['id'] in run['charges']:
            return
        with self._lock:
            run['charges'][task['id']] = {'usage': task['usage'], 'tokens_reported': False}
            run['usage'] = {unit: sum(c['usage'].get(unit, 0) for c in run['charges'].values())
                            for unit in ('tokens', 'seconds')}

    def _publish(self, run):
        artifacts = run['merged']['artifacts']
        text = '\n'.join(artifact['content'] for artifact in artifacts)
        meta = {'agent_run_id': run['id'], 'child_threads': [w['threadId'] for w in run['workers']],
                'gate': run['merge_gate']}
        self._log(run['parentThreadId'], text, meta, 'merge-' + run['id'])
        self._event(run['parentThreadId'], 'agent/merge', 'merge-' + run['id'], meta)

    def message_events(self, result, source, targets):
        for slug in [source, *targets]:
            self._event(slug, 'agent/message', result['message_id'], {'message_id': result['message_id'],
                        'from_thread_id': source, 'recipients': targets, 'hop': result['hop']})
        return result
