"""Persistent subjects, durable turn queues, replayable events, and session mail."""

from collections import deque
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import datetime, timezone
from .settings_ladder import credential_source
from .config import offered_for_chat
import shutil
from . import locking as fcntl
from .durability import sync_directory
from .platform_support import host_report
import json
import os
from pathlib import Path
import re
import threading
import time
import unicodedata
import uuid

from .capture import Capture, service_error_name
from .identities import handle_for
from .aethermind import AetherMindClient
from .backend import Backend, BackendError
from .agents import AgentPool
from .agent_selection import backend_catalog
from .loop import INTERRUPT_REPLIES, Session
from .provider_status import build_provider_status
from .passive_status import task_status, judge_status, configured_path
from .oversight import oversight_report
from .trial import TrialStore, advance, session_rows
from .messaging import SessionMessaging, may_resume_work
from .tools import object_schema, AUTO_APPROVE
from .voice import VoiceSessions
from .infrastructure import Infrastructure
from .grafana import GrafanaClient
from .credentials import CredentialResolver
from .board import _MISSING
from .board_tools import BoardTools
from .autonomy import Autonomy
from .cua import register_cua_tools
from .board_status import (board_brief, board_claim, board_file, board_link, board_move as move_board_card,
                           board_answer, board_create, board_note, board_project_create, board_project_update,
                           board_remove, board_revise, board_start as start_board_card, board_status,
                           board_step_check, board_steps, board_view as read_board_view,
                           accept_board_card, triage_intake_card, accept_intake_card, ignore_intake_card)
from .jobs import SwarmJobs
from .job_status import job_status
from .feedback import Feedback
from .attachments import (
    MAX_PER_TURN,
    describe as describe_attachments,
    resolve as resolve_attachment,
    store as store_attachment,
    reference as reference_attachment,
    read as read_attachment,
    location as attachment_location,
)
from .log import ChatLog
from .tree import SummaryTree
from .view import View


def now():
    return datetime.now(timezone.utc).isoformat()


class UnreadableSession(ValueError):
    """One session's meta.json cannot be rendered, but the rest of the list can.

    Raised instead of KeyError/TypeError so the failure names the record and the
    field.
    """

    def __init__(self, slug, field, detail):
        self.slug = slug
        self.field = field
        self.detail = detail
        super().__init__(f'session {slug!r} field {field!r} {detail}')

    def report(self):
        return {'slug': self.slug, 'field': self.field, 'reason': self.detail}


def atomic_json(path, data):
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(data, stream, ensure_ascii=False, allow_nan=False)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    sync_directory(path.parent)


def append_record(path, record):
    existed = path.exists()
    with path.open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + '\n')
        stream.flush()
        os.fsync(stream.fileno())
    if not existed:
        sync_directory(path.parent)


def read_records(path):
    records = []
    if not path.exists():
        return records
    with path.open('r+b') as stream:
        while True:
            offset = stream.tell()
            line = stream.readline()
            if not line:
                break
            if not line.endswith(b'\n'):
                recovery = path.with_name(path.name + '.incomplete-' + uuid.uuid4().hex)
                with recovery.open('xb') as evidence:
                    evidence.write(line)
                    evidence.flush()
                    os.fsync(evidence.fileno())
                sync_directory(path.parent)
                stream.truncate(offset)
                stream.flush()
                os.fsync(stream.fileno())
                break
            try:
                records.append(json.loads(line))
            except ValueError:
                raise ValueError(f'Invalid {path.name} record at byte {offset}') from None
    return records



def _wake_flag(item):
    # A wake runs unseen; its failure is recorded but must not raise an error banner in the chat.
    return {'wake': True} if item.get('wake') else {}


def _choice_error_text(choice):
    """The external failure a person can read. The full text wins over the class name."""
    if not isinstance(choice, dict):
        return ''
    text = choice.get('external_error')
    if isinstance(text, str) and text.strip():
        return text.strip()
    name = choice.get('external_error_name')
    if isinstance(name, str) and name.strip():
        return name.strip()
    return ''

class EventJournal:
    def __init__(self, path, observer=None):
        self.path = path
        self.observer = observer
        # Called on every durable append so an idle restart can tell a working
        # turn from a wedged one without reading the journal.
        self.on_progress = None
        self.condition = threading.Condition(threading.RLock())
        self.closed = False
        # Live-only records (streamed thought and reply text): never persisted, never replayed.
        self.live = []
        self.live_seq = 0
        self.records = read_records(path)
        for index, record in enumerate(self.records, 1):
            if record['id'] != index:
                raise ValueError('Noncontiguous event IDs')

    def append(self, event, data):
        if event == 'error':
            # Lets a client tell a live failure from one replayed with history.
            data = {**data, 'at': now()}
        if self.on_progress is not None:
            self.on_progress()
        with self.condition:
            record = {'id': len(self.records) + 1, 'event': event, 'data': data}
            append_record(self.path, record)
            self.records.append(record)
            self.condition.notify_all()
        if self.observer is not None:
            self.observer(record)
        return record

    def push_live(self, event, data, keep=512):
        with self.condition:
            self.live_seq += 1
            self.live.append((self.live_seq, event, data))
            del self.live[:-keep]
            self.condition.notify_all()

    def live_after(self, seq):
        with self.condition:
            return [item for item in self.live if item[0] > seq]

    def turn_snapshot(self, turn_id):
        """The in-flight turn folded into one record, for a client that just attached.

        Streamed thought and reply text are live-only and never replayed, so a
        page that opens a session mid-turn sees history and then silence: the
        working turn looks stalled. Fold the retained deltas for this turn into
        the text it has accumulated so far, so the reader sees the work.
        """
        if not turn_id:
            return None
        with self.condition:
            thought = ''
            text = ''
            tool = ''
            seen = False
            status = ''
            updated_at = None
            for _, event, data in self.live:
                if event != 'delta' or not isinstance(data, dict):
                    continue
                if data.get('turn_id') != turn_id:
                    continue
                kind = data.get('kind')
                value = data.get('text') or ''
                if kind == 'status':
                    if value != status:
                        updated_at = data.get('at')
                    status = value
                elif kind == 'heartbeat':
                    continue
                elif kind == 'thought':
                    updated_at = data.get('at')
                    status = 'Thinking'
                    thought += value
                elif kind == 'text':
                    updated_at = data.get('at')
                    status = 'Writing'
                    text += value
                elif kind == 'tool':
                    updated_at = data.get('at')
                    status = 'Calling ' + value + '...'
                    tool = value
                elif kind == 'reset':
                    updated_at = data.get('at')
                    status = 'Waiting for model output'
                    text = ''
                    tool = ''
                else:
                    continue
                seen = True
            if not seen:
                return None
            return {'turn_id': turn_id, 'thought': thought, 'text': text, 'tool': tool, 'status': status, 'at': updated_at}

    def after(self, event_id=0, timeout=0):
        with self.condition:
            if event_id > len(self.records):
                raise ValueError('Event ID is ahead of this session')
            if event_id == len(self.records) and timeout and not self.closed:
                self.condition.wait(timeout)
            return list(self.records[event_id:])

    def replay_start(self, limit):
        """First event id a fresh client should be sent.

        A page that opens with no checkpoint wants the newest events, not the
        whole journal. The transcript itself comes from the messages endpoint;
        replaying every event since the session began costs megabytes on a long
        session and rebuilds messages the page already has.
        """
        if type(limit) is not int or limit < 1:
            raise ValueError('replay limit must be a positive integer')
        with self.condition:
            return max(0, len(self.records) - limit)

    def close(self):
        with self.condition:
            self.closed = True
            self.condition.notify_all()


class _Runtime:
    def __init__(self, manager, meta):
        self.manager = manager
        self.slug = meta['slug']
        self.root = manager.path(self.slug)
        self.condition = threading.Condition(threading.RLock())
        self.intake_lock = threading.RLock()
        self.pending = deque()
        self.turns = {}
        self.active = None
        # Monotonic time of the last sign of life from the running turn: a
        # durable event, or a live streamed delta. An idle restart waits on this
        # rather than on wall-clock duration, because a long turn that keeps
        # emitting events is working, and a turn that has gone silent is not.
        self.progress_at = time.monotonic()
        self.intakes = 0
        self.failure = None
        self.last_route = None
        self.stopping = False
        self.draining = False
        self.operator_stopped = False
        self.operator_stop_generation = 0
        self.operator_cancelled = threading.Event()
        self.journal = self.root / 'turns.jsonl'
        observer = None
        if manager.event_observer is not None:
            observer = lambda record: manager.event_observer(self.slug, record)
        self.events = EventJournal(self.root / 'events.jsonl', observer)
        self.events.on_progress = self._mark_progress
        if observer is not None:
            for record in self.events.after():
                observer(record)
        config = manager.config
        roles = dict(config.roles)
        if not meta.get('backend_override', True):
            # Sessions that never picked a model follow the configured default; the stored
            # name is only what the default was when the session was created.
            meta = {**meta, 'backend': None}
        missing = meta['backend'] if meta['backend'] is not None and meta['backend'] not in config.backends else None
        if missing is not None:
            # A session pinned to a removed backend loads on the default chat model instead of failing.
            meta = {**meta, 'backend': None}
        if meta['backend'] is not None:
            roles['main'] = meta['backend']
            if meta.get('backend_override', True):
                roles['fast'] = meta['backend']
        config = replace(config, roles=roles)
        backend = manager.backend_factory(self.slug, meta['backend']) if manager.backend_factory else None
        factory = manager.session_factory or Session
        self.session = factory(config, backend=backend, root=self.root, session_id=meta['id'],
                               pool=manager.agent_pool,
                               approve=self._approve, configure_tools=self._register_tools,
                               judge_human=self._human if manager.human_request else None)
        self.session._parent_cancelled = self.operator_cancelled
        if hasattr(self.session, 'refresh_attached_memory'):
            self.session._refresh_peers = self.manager.refresh_open_memory
        chosen = meta.get('workspace') or ''
        if chosen:
            self.session.set_workspace(chosen)
        manager.observe_log(self.slug, self.session.log)
        if missing is not None:
            notice = f'Backend {missing} is no longer configured; using the default chat model.'
            self.session.log.append('note', notice, {'label': 'backend-fallback'})
        self.session._default_main = manager.config.roles.get('main')
        if manager.backend_factory:
            self.session.backend_builder = lambda name: manager.backend_factory(self.slug, name)
        self.session.delta_sink = self._on_delta
        self.session.reasoning = meta.get('reasoning') or 'low'
        self.session.thinking_budget = meta.get('thinking_budget') or 0
        self.session.log.subscribe(self._on_message)
        self.session.hooks.register('turn_start', self._on_turn_start)
        self.session.hooks.register('turn_end', self._on_turn_end)
        self.inheritance = self.session.log.redact(self.session.capture.inheritance_snapshot)
        self._restore_turns()
        self.manager._save_runtime(self)
        self.thread = threading.Thread(target=self._worker, name=f'tessera-session-{self.slug}', daemon=True)
        self.thread.start()

    def _approve(self, name, arguments):
        if self.manager.approval_request is not None:
            turn_id = self.active['id'] if self.active else None
            return self.manager.approval_request(self.slug, turn_id, name, arguments)
        return bool(self.manager.approve and self.manager.approve(name, arguments))

    def _human(self, decision):
        turn_id = self.active['id'] if self.active else None
        return self.manager.human_request(self.slug, turn_id, decision)

    def switch_backend(self, name):
        # Applies at the next turn; a running turn finishes on the model it started with.
        built = self.manager.backend_factory(self.slug, name) if self.manager.backend_factory else None
        self.session.queue_backend(name, built)

    def _register_tools(self, registry):
        self.manager.board_tools.register_tools(self, registry)
        if self.manager.config.swarm.enabled:
            def swarm_job(action, objective='', workers=None, job_id=''):
                jobs = self.manager.swarm_jobs
                if action == 'create':
                    return self.manager.create_swarm_job(self.slug, objective, workers)
                job = jobs.get(job_id)
                if action == 'run':
                    if job['session_slug'] != self.slug:
                        raise ValueError('Run swarm work from its owning session')
                    return self.manager.run_swarm_job(job_id)
                if action == 'status':
                    return job
                if action == 'artifact':
                    return jobs.artifact(job_id)
                raise ValueError('Unknown swarm action')
            registry.register('swarm_job',
                              'Create, run or inspect a bounded parallel response-only evidence job. '
                              'Workers need objective and non-overlapping scope; backend is optional, '
                              'capability selects the cheapest capable configured model. These jobs do not '
                              'execute tools or edit files; use spawn_agent for implementation. '
                              'Use this live-owner tool instead of launching another Tessera on this home.',
                              object_schema({'action': {'type': 'string', 'enum': ['create', 'run', 'status', 'artifact']},
                                             'objective': {'type': 'string'}, 'workers': {'type': 'array', 'items': {'type': 'object'}},
                                             'job_id': {'type': 'string'}}, ['action']), swarm_job)
        # Boards are windows the agent opens on demand, never context pushed into every turn.
        if self.manager.config.board.enabled:
            registry.register('wip_board', 'Read the shared WIP board: every card with its title, owner, status, note, '
                              'artifacts and pending questions, plus the cards nobody has claimed and the cards '
                              'nobody filed. Tessera files into granular subjects and planning projects. '
                              'Read filing_guidance, reuse subjects, and use board_read_evidence for attached context. '
                              'Claim an unclaimed card to take it.',
                              object_schema({}), lambda: self.manager.board_brief())
        registry.register('infra_status', 'Read the Infrastructure board: hosts, health and the latest observations.',
                          object_schema({}), lambda: self.manager.infrastructure.status())
        registry.register('session_quality',
                          'Read how sessions actually behaved, quality first: tool outcomes, whether the '
                          'judge answered or fell back, and claims that outran evidence. Grades structure, '
                          'not the judge\'s completion score, because the heuristic cannot discriminate at '
                          'the configured thresholds. Read-only; it names its own blind spots.',
                          object_schema({}), lambda: self.manager.oversight_report())
        self.manager.autonomy.register_tools(self, registry)
        register_cua_tools(registry, self.manager.config)
        if not self.manager.config.channels.enabled:
            return
        def send_to_session(slug, text):
            hop = self.active['hop'] if self.active else 0
            return self.manager.send_to_session(self.slug, slug, text, hop=hop)
        registry.register('send_to_session', 'Send a durable work message to an existing session. Configured reply and loop guards apply.',
                          object_schema({'slug': {'type': 'string'}, 'text': {'type': 'string'}}, ['slug', 'text']), send_to_session)
        def publish_channel(channel, text):
            return self.manager.publish_channel(channel, self.slug, text)
        registry.register('publish_channel', 'Publish durable work to an existing subscribed channel.',
                          object_schema({'channel': {'type': 'string'}, 'text': {'type': 'string'}}, ['channel', 'text']), publish_channel)
        def handoff(slug, brief, task_ref=''):
            return self.manager.handoff(self.slug, slug, brief, task_ref=task_ref)
        registry.register('handoff', 'Transfer a brief, task reference and current view snapshot to an existing session.',
                          object_schema({'slug': {'type': 'string'}, 'brief': {'type': 'string'},
                                         'task_ref': {'type': 'string'}}, ['slug', 'brief']), handoff)

    def _restore_turns(self):
        for record in read_records(self.journal):
            if record['op'] == 'enqueue':
                item = dict(record['turn'])
                self.turns[item['id']] = item
            elif record['op'] == 'state':
                self.turns[record['id']].update(record)
            elif record['op'] == 'operator_stop':
                self.operator_stopped = record['stopped']
                if self.operator_stopped:
                    self.operator_cancelled.set()
                else:
                    self.operator_cancelled.clear()
            elif record['op'] == 'revise':
                self.turns[record['id']]['text'] = record['text']
            else:
                raise ValueError('Unknown turn journal operation')
        for item in self.turns.values():
            if item['state'] == 'queued' and may_resume_work(self.manager.config, item):
                self.pending.append(item)
            elif item['state'] == 'running':
                self._set_state(item, 'error', 'Interrupted by a previous runtime; not replayed automatically')
                self.events.append('error', {'turn_id': item['id'], 'error': item['error'], **_wake_flag(item)})
                self.events.append('turn_end', {'turn_id': item['id'], 'success': False})

    def _set_state(self, item, state, error=None):
        record = {'op': 'state', 'id': item['id'], 'state': state, 'error': error}
        append_record(self.journal, record)
        item.update(record)

    def enqueue(self, text, sender=None, hop=0, work_id=None, *, turn_id=None, wake=None, source=None, attachments=None, project_id=None, selected=None):
        with self.intake_lock:
            if turn_id is not None and turn_id in self.turns:
                previous = self.turns[turn_id]
                if (previous['text'] != self.session.log.redact(text) or previous.get('source') != source
                        or previous.get('project_id') != project_id or previous.get('selected', []) != (selected or [])):
                    raise ValueError('Turn identity belongs to another input')
                return turn_id
            if work_id is not None:
                for previous in self.turns.values():
                    if previous.get('work_id') == work_id:
                        return previous['id']
            item = {'id': turn_id or uuid.uuid4().hex, 'text': self.session.log.redact(text), 'from_slug': sender,
                    'hop': hop, 'state': 'queued', 'created': now(), 'work_id': work_id}
            if project_id:
                item['project_id'] = project_id
                item['selected'] = selected or []
            if attachments:
                item['attachments'] = attachments
            if source is not None:
                item['source'] = source
            if wake is not None:
                item['wake'] = wake
            with self.condition:
                if self.stopping or self.failure:
                    raise RuntimeError(self.failure or 'Session is stopping')
                self.intakes += 1
            try:
                with self.condition:
                    append_record(self.journal, {'op': 'enqueue', 'turn': item})
                    self.turns[item['id']] = item
                if sender and work_id is None:
                    self.session.log.append('work', f'[{sender}] {item["text"]}',
                                            {'from_slug': sender, 'hop': hop, 'turn_id': item['id']})
                with self.condition:
                    self.pending.append(item)
                return item['id']
            finally:
                with self.condition:
                    self.intakes -= 1
                    self.condition.notify_all()

    def _on_delta(self, kind, text):
        turn_id = self.active['id'] if self.active else None
        # A streamed delta is life: a model that is emitting is not wedged.
        self._mark_progress()
        self.events.push_live('delta', {'turn_id': turn_id, 'kind': kind, 'text': text, 'at': now()})

    def _mark_progress(self):
        with self.condition:
            self.progress_at = time.monotonic()

    def _on_message(self, message):
        judgment = message.meta.get('action_judgment') or {}
        if message.kind == 'echo' and judgment.get('block'):
            reason = judgment.get('reason', '')
            floor = self.manager.config.autonomy.infinity_slug
            if (self.manager.config.autonomy.enabled and floor in self.manager._metadata and self.slug != floor
                    and getattr(self, '_last_action_attention', None) != reason):
                self._last_action_attention = reason
                self.manager.autonomy.post(floor, 'coordination',
                    f'Session {self.slug} needs coordination after a blocked action: {reason} '
                    'Read coordination_status and the session evidence, then steer or delegate the next safe step. '
                    'Preserve operator Stops.')
        route = message.meta.get('route') if message.kind == 'agent' else None
        if route and route != self.last_route:
            self.last_route = dict(route)
            turn_id = self.active['id'] if self.active else None
            self.events.append('route', {'turn_id': turn_id, **route})
        event = asdict(message)
        if self.active is not None and not message.meta.get('voice_transcript') and message.meta.get('provenance') != 'client_reported':
            event['turn_id'] = self.active['id']
        self.events.append('message', event)
        self.manager._save_runtime(self)

    def _on_turn_start(self, payload):
        if payload.get('routing'):
            self.last_route = dict(payload['routing'])
            self.events.append('route', {'turn_id': self.active['id'], **payload['routing']})
        message = payload.get('message') or payload.get('user') or ''
        text = self.manager.autonomy.learning.context_for(self.slug, message)
        context = [text] if text else []
        if self.active and self.active.get('project_id'):
            from .project_canvas import planning_context
            project = self.manager.board.get_project(self.active['project_id'])
            context.append(planning_context(project, self.active.get('selected', [])))
        if context:
            return {'context': '\n\n'.join(context)}

    def _on_turn_end(self, payload):
        self.inheritance = self.session.log.redact(self.session.capture.inheritance_block())
        debt = len(self.session.capture.debt())
        self.events.append('capture', {'turn_id': self.active['id'], 'debt': debt})
        self.manager._save_runtime(self)
        message = payload.get('message') or payload.get('user') or ''
        is_wake = str(message).lstrip().startswith('[wake]')
        self.manager.autonomy.learning.observe_turn(
            self.slug, message, payload.get('tools') or [], payload.get('judge_score'),
            bool(payload.get('success')), is_wake,
            pinned=payload.get('pinned_skills') or [])
        # A trial measures turns, so a turn ending is the moment to move it.
        # Throttled inside; a failure here must not fail the turn.
        try:
            self.manager.advance_trials(self.slug)
        except Exception as error:
            self.events.append('error', {'error': f'trial advance failed ({type(error).__name__})'})

    def _worker(self):
        try:
            self._process_turns()
        except Exception as failure:
            with self.condition:
                self.failure = f'Session storage failed ({type(failure).__name__}); reopen to recover queued work'
                self.active = None
                self.condition.notify_all()
            try:
                self.events.append('error', {'error': self.failure})
            except Exception:
                pass

    def _process_turns(self):
        while True:
            with self.condition:
                while (not self.pending or self.draining or self.operator_stopped) and not self.stopping:
                    self.condition.wait()
                # Queued turns stay queued through a drain; the next runtime resumes them.
                if not self.pending or self.draining:
                    return
                item = self.pending.popleft()
                if item.get('state') == 'cancelled':
                    continue
                self.active = item
                # The turn starts now: without this, an idle restart reads the
                # previous turn's silence as this turn's and interrupts it.
                self.progress_at = time.monotonic()
                self._set_state(item, 'running')
            self.events.append('turn_start', {'turn_id': item['id'], 'from_slug': item['from_slug'], 'hop': item['hop']})
            success = False
            error = None
            failure_details = {}
            try:
                # Restore a sender report if a crash happened between durable enqueue and log append.
                if item['from_slug'] and not item.get('work_id'):
                    present = False
                    for message in self.session.log.iter():
                        if message.kind == 'work' and message.meta.get('turn_id') == item['id']:
                            present = True
                            break
                    if not present:
                        self.session.log.append('work', f'[{item["from_slug"]}] {item["text"]}',
                                                {'from_slug': item['from_slug'], 'hop': item['hop'], 'turn_id': item['id']})
                awareness = None if item.get('wake') or item['from_slug'] else self.manager.autonomy.take_awareness(self.slug)
                self.session.run_turn(item['text'], turn_meta={'request_id': item['id'], 'from_slug': item['from_slug'], 'hop': item['hop'], 'work_id': item.get('work_id'),
                                                        **({'wake': item['wake']} if item.get('wake') else {}),
                                                        **({'awareness': awareness} if awareness else {}),
                                                        **({'attachments': item['attachments']} if item.get('attachments') else {}),
                                                        **({'project_id': item['project_id'], 'selected': item.get('selected', [])} if item.get('project_id') else {})})
                if item.get('wake'):
                    self.manager.autonomy.wake_record(self.slug, item['id'], not self.session.last_turn_silent)
                self.session.agents.wait()
                success = self.session.last_turn_success
                if not success:
                    if self.session.last_turn_interrupted:
                        error = INTERRUPT_REPLIES[self.session.interrupt_reason].rstrip('.')
                    else:
                        error = 'Turn did not pass the output gate'
            except Exception as failure:
                from .bundled_engine import AetherMindUnavailable
                if isinstance(failure, AetherMindUnavailable):
                    error = str(failure)
                else:
                    error = f'Turn failed ({type(failure).__name__})'
                if isinstance(failure, BackendError):
                    failure_details = failure.diagnostics()
                    # The CLI worker's stderr can carry credential fragments, so
                    # redact it through the session log before persisting.
                    if failure_details.get('error_detail'):
                        failure_details['error_detail'] = self.session.log.redact(
                            failure_details['error_detail'])
            finally:
                with self.condition:
                    self._set_state(item, 'done' if success else 'error', error)
                    if error:
                        self.events.append('error', {'turn_id': item['id'], 'error': error, **failure_details, **_wake_flag(item)})
                    self.events.append('turn_end', {'turn_id': item['id'], 'success': success,
                                                     'interrupted': self.session.last_turn_interrupted})
                    self.active = None
                    self.condition.notify_all()
                self.manager._save_runtime(self)
            # Steering that arrived after the turn's last safe point becomes the next turn.
            for guidance in self.session.take_unapplied_steers():
                if self.operator_stopped:
                    continue
                try:
                    self.enqueue(guidance['text'], project_id=guidance['metadata'].get('project_id'),
                                 selected=guidance['metadata'].get('selected'))
                except Exception:
                    self.events.append('error', {'turn_id': item['id'], 'error': 'Late steer could not be queued'})

    def wait(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + timeout
        with self.condition:
            while (self.pending and not self.draining) or self.active or self.intakes or self.failure:
                if self.failure:
                    raise RuntimeError(self.failure)
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return False
                self.condition.wait(remaining)
            return True

    def idle(self):
        with self.condition:
            return bool(self.failure) or not ((self.pending and not self.draining) or self.active or self.intakes)

    def hold(self):
        """Start no more queued turns; the running one may finish."""
        with self.condition:
            self.draining = True
            self.condition.notify_all()

    def wait_active(self, timeout):
        deadline = time.monotonic() + max(0.0, timeout)
        with self.condition:
            while self.active:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self.condition.wait(remaining)
            return True

    def close(self):
        with self.intake_lock, self.condition:
            self.stopping = True
            self.condition.notify_all()
        self.thread.join()
        self.session.close(end_session=False)
        self.events.close()


class SessionManager:
    def infrastructure_targets(self):
        """Prometheus scrape targets with health, from the configured Prometheus."""
        import json as _json
        import urllib.request
        base = self.config.infrastructure.prometheus_url.rstrip('/')
        if not base:
            return {'configured': False, 'targets': []}
        try:
            with urllib.request.urlopen(base + '/api/v1/targets?state=active', timeout=8) as response:
                data = _json.loads(response.read(8 * 1024 * 1024))['data']['activeTargets']
        except Exception as error:
            return {'configured': True, 'error': type(error).__name__, 'targets': []}
        targets = []
        for item in data:
            labels = item.get('labels') or {}
            targets.append({'job': str(labels.get('job', '')), 'instance': str(labels.get('instance', '')),
                            'health': str(item.get('health', 'unknown')),
                            'error': str(item.get('lastError') or '')[:160],
                            'last_scrape': str(item.get('lastScrape') or '')})
        targets.sort(key=lambda t: (t['health'] == 'up', t['job'], t['instance']))
        return {'configured': True, 'targets': targets,
                'up': sum(t['health'] == 'up' for t in targets), 'total': len(targets)}

    def __init__(self, config, backend_factory=None, approve=None, swarm_backend_factory=None,
                 *, approval_request=None, human_request=None, log_observer=None, event_observer=None,
                 session_factory=None, work_reply_allowed=None):
        self.config = config.validate()
        self.backend_factory = backend_factory
        self.approve = approve
        self.approval_request = approval_request
        self.human_request = human_request
        self.log_observer = log_observer
        self.event_observer = event_observer
        self.session_factory = session_factory
        self.work_reply_allowed = work_reply_allowed
        self.root = Path(config.home) / 'sessions'
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._runtime_ready = threading.Condition(self._lock)
        self._runtime_loads = set()
        self._runtimes = {}
        self.agent_pool = AgentPool()
        self.messaging = SessionMessaging(self)
        self.channels = self.messaging.store
        self.board_tools = BoardTools(self)
        self.board = self.board_tools.store
        self.swarm_jobs = SwarmJobs(config, backend_factory=swarm_backend_factory)
        self.infrastructure = Infrastructure(config)
        self.infrastructure.start()
        from .integrations import Integrations
        from .config_editor import ConfigEditor
        self.integrations = Integrations(self)
        self.config_editor = ConfigEditor(Path(self.config.home) / 'config.toml')
        # The first look at config.toml is a baseline, taken once at startup. A later
        # write that bypasses ConfigEditor is then a diff Settings can show. Status
        # stays a read: a poll must not create the journal.
        self.config_journal_error = ''
        try:
            self.config_editor.notice_drift()
        except OSError as error:
            self.config_journal_error = type(error).__name__ + ': ' + str(error)
        from .settings_model import SettingsService
        self.settings = SettingsService(self)
        self.grafana = GrafanaClient(config.grafana, credentials=CredentialResolver(config.credentials))
        # Bounded A/B trials, the measurement an 'auto' oversight policy waits for.
        self.trials = TrialStore(Path(self.config.home) / 'trials' / 'trials.json')
        self._trials_advanced_at = 0.0
        self._metadata = {}
        self._accepting = True
        self._closed = False
        self.voice = VoiceSessions(self)
        # Person records under the board's owner strings. Off unless configured, so an
        # existing install keeps its free-string ownership exactly as it is today.
        from .identities import IdentityRegistry, IdentityStore
        identity_settings = self.config.identity
        self.identities = IdentityRegistry(
            IdentityStore(configured_path(self.config.home, identity_settings.journal_file),
                          enabled=identity_settings.enabled),
            self)
        # The board's view and write functions all take (config, session_slugs) as their
        # shared context, and every write route ends in board_view. Attaching the registry
        # here rather than threading a parameter through fifteen functions means one
        # implementation covers reads and writes alike, with no route left unenriched.
        self.config.identity_registry = self.identities
        # The board validates an owner against the same registry, so a card cannot be
        # claimed by a name that resolves to nobody once identity records are on.
        self.board.identities = self.identities
        self.identity_record_errors = []
        if identity_settings.enabled and identity_settings.record_operator and self.config.operator_name.strip():
            # A configured operator becomes a person record once, so the board's owner
            # string resolves to an identity instead of an unknown name. Idempotent: an
            # existing record is returned, not duplicated. An unconfigured operator
            # creates nothing, so a fresh install never inherits someone's identity.
            try:
                self.identities.ensure_operator(self.config.operator_name)
            except Exception as error:
                # A bad name in config must not stop the core from starting. The board
                # shows the owner as unresolved, and the error is kept where that
                # status already looks.
                self.identity_record_errors.append({
                    'what': 'operator',
                    'error': type(error).__name__ + ': ' + str(error),
                })
        if identity_settings.enabled and identity_settings.record_companion and self.config.agent_name.strip():
            # The companion's own projects resolve to nobody otherwise: they show as
            # unknown owners the moment records are enabled, because only the operator
            # was recorded.
            try:
                self.identities.ensure_companion(self.config.agent_name)
            except Exception as error:
                self.identity_record_errors.append({
                    'what': 'companion',
                    'error': type(error).__name__ + ': ' + str(error),
                })
        self._owner = (self.root / '.manager.lock').open('a')
        try:
            fcntl.flock(self._owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._owner.close()
            raise RuntimeError('Session manager is locked by another process') from None
        self._init_approval()
        self.autonomy = Autonomy(self)
        try:
            for path in sorted(self.root.iterdir()):
                if not path.is_dir() or path.is_symlink():
                    continue
                self.path(path.name)
                if (path / 'meta.json').exists():
                    meta = json.loads((path / 'meta.json').read_text(encoding='utf-8'))
                    if meta['slug'] != path.name:
                        raise ValueError('Session metadata slug mismatch')
                    self._metadata[path.name] = meta
            for slug in list(self._metadata):
                states = {}
                for record in read_records(self.path(slug) / 'turns.jsonl'):
                    if record['op'] == 'enqueue':
                        states[record['turn']['id']] = dict(record['turn'])
                    elif record['op'] == 'state':
                        states[record['id']]['state'] = record['state']
                for item in states.values():
                    if (config.server.resume_turns and item['state'] in ('queued', 'running')
                            and may_resume_work(self.config, item)):
                        self.runtime(slug)
                        break
        except Exception:
            for runtime in self._runtimes.values():
                runtime.close()
            self._owner.close()
            raise
        self.autonomy.start()

    def path(self, slug):
        if not isinstance(slug, str) or not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?', slug):
            raise ValueError('Invalid session slug')
        path = self.root / slug
        if path.is_symlink() or path.resolve().parent != self.root.resolve():
            raise ValueError('Invalid session path')
        return path

    def observe_log(self, slug, log):
        if self.log_observer is not None:
            self.log_observer(slug, log)

    REQUIRED_META = ('slug', 'title', 'created', 'updated', 'messages', 'archived', 'backend')

    def _public(self, meta):
        result = {}
        for key in self.REQUIRED_META:
            if key not in meta:
                raise UnreadableSession(meta.get('slug') or '?', key, 'is missing')
            result[key] = meta[key]
        for key in ('created', 'updated'):
            # The list is sorted on these. Refuse a non-string here, where the
            # record is known, so the sort does not raise TypeError.
            if not isinstance(result[key], str):
                raise UnreadableSession(result['slug'], key,
                                        f'is {type(result[key]).__name__}, not an ISO timestamp')
        result['workspace'] = meta.get('workspace') or ''
        result['reasoning'] = meta.get('reasoning') or 'low'
        result['thinking_budget'] = meta.get('thinking_budget') or 0
        # The serialized identity, so a coordinator can address a session by a handle
        # that does not change meaning when a title changes. The slug is a label: it is
        # derived from the title, so two sessions can share a stem and the second one
        # gets a '-2' suffix that says nothing about who it is. The id is minted once at
        # creation and never re-derived, which is what makes it safe to key coordination
        # on. It is exposed here and never used as a storage path.
        if isinstance(meta.get('id'), str) and meta['id']:
            result['id'] = meta['id']
        if not meta.get('backend_override', True):
            # Not pinned: it follows the configured default, whatever that is now.
            result['backend'] = None
        if meta.get('infinity'):
            result['infinity'] = True
        if meta.get('voice'):
            result['voice'] = True
        return result

    def resolve_session(self, handle):
        """Return the slug for a session handle, accepting the slug or the serialized id.

        Sessions are addressed by slug everywhere: it is the directory name, the owner
        string on a card, the sender on a message. A slug is a label derived from the
        title, so it is not an identity: retitling does not change it, but two sessions
        with the same stem differ only by a '-2' suffix. The id is the stable handle.

        This resolves either one to the slug, so an identity-aware caller can address a
        session without changing what the storage keys on. Returns None for a handle
        that names no session, rather than raising: a caller that guessed wrong should
        get the same refusal it would get for an unknown slug.
        """
        if not isinstance(handle, str) or not handle:
            return None
        with self._lock:
            if handle in self._metadata:
                return handle
            for slug, meta in self._metadata.items():
                if meta.get('id') == handle:
                    return slug
        return None

    def _as_slug(self, handle):
        """Normalize a session handle to its slug, leaving a non-session identity alone.

        Owners and actors are not only sessions. The operator is an identity on the
        board too, and a project can name a manager who is a person. So this
        only rewrites a handle that resolves to a session; anything else is passed
        through exactly as given, which keeps every existing caller's behavior.
        """
        if handle is None:
            return None
        return self.resolve_session(handle) or handle

    def _workspace_value(self, workspace):
        if workspace is None:
            return None
        if not isinstance(workspace, str) or '\x00' in workspace or len(workspace) > 4096:
            raise ValueError('workspace must be a path')
        if not workspace.strip():
            return ''
        path = Path(workspace).expanduser()
        if not path.is_absolute():
            path = Path(self.config.cwd) / path
        return str(path.resolve())

    def create(self, title, backend=None, *, agent=None, workspace=None):
        if not isinstance(title, str) or not title.strip() or len(title) > 200:
            raise ValueError('Title must be 1..200 characters')
        if backend is not None and (not isinstance(backend, str) or backend not in self.config.backends):
            raise ValueError('Unknown backend')
        if backend is not None and agent is None and not offered_for_chat(backend, self.config.backends[backend]):
            raise ValueError('Backend is not offered as a chat brain')
        backend_override = backend is not None
        if backend is None:
            backend = self.config.roles.get('main')
            if backend is None and self.config.backends:
                backend = next(iter(self.config.backends))
        normalized = unicodedata.normalize('NFKD', title).encode('ascii', errors='ignore').decode()
        stem = re.sub('[^a-z0-9]+', '-', normalized.lower()).strip('-')[:64].rstrip('-') or 'session'
        with self._lock:
            if not self._accepting:
                raise RuntimeError('Session manager is stopping')
            slug = stem
            suffix = 2
            while (self.root / slug).exists():
                ending = '-' + str(suffix)
                slug = stem[:64 - len(ending)].rstrip('-') + ending
                suffix += 1
            path = self.path(slug)
            path.mkdir()
            timestamp = now()
            chosen = self._workspace_value(workspace)
            meta = {'id': uuid.uuid4().hex, 'slug': slug, 'title': title.strip(), 'created': timestamp,
                    'updated': timestamp, 'messages': 0, 'archived': False, 'backend': backend, 'backend_override': backend_override,
                    'debt': 0, 'workspace': chosen or ''}
            if agent is not None:
                meta['agent'] = dict(agent)
            atomic_json(path / 'meta.json', meta)
            self._metadata[slug] = meta
            return self._public(meta)

    def ensure(self, slug, title):
        """Open one durable session. The same slug and id are reused after a restart."""
        self.path(slug)
        if not isinstance(title, str) or not title.strip():
            raise ValueError('Title must be 1..200 characters')
        with self._lock:
            if not self._accepting:
                raise RuntimeError('Session manager is stopping')
            existing = self._metadata.get(slug)
            if existing is not None:
                if existing.get('archived'):
                    existing['archived'] = False
                    atomic_json(self.path(slug) / 'meta.json', existing)
                return self._public(existing)
            backend = self.config.roles.get('main')
            if backend is None and self.config.backends:
                backend = next(iter(self.config.backends))
            path = self.path(slug)
            path.mkdir()
            timestamp = now()
            meta = {'id': uuid.uuid4().hex, 'slug': slug, 'title': title.strip(), 'created': timestamp,
                    'updated': timestamp, 'messages': 0, 'archived': False, 'backend': backend,
                    'backend_override': False, 'debt': 0, 'workspace': '', 'infinity': True}
            atomic_json(path / 'meta.json', meta)
            self._metadata[slug] = meta
            return self._public(meta)

    def ensure_voice(self):
        """The dedicated voice session: a session of its own, not a chat you talk through.

        Settings changes supersede saved session choices. Track each last-applied
        value so an unchanged setting does not erase subsequent hand tuning.
        Legacy sessions without that record adopt Settings on their next call.
        """
        slug = self.config.voice.dedicated_slug
        title = self.config.voice.dedicated_title
        if not isinstance(slug, str) or not slug:
            raise ValueError('Voice session slug must be configured text')
        self.path(slug)
        if not isinstance(title, str):
            raise ValueError('Voice session title must be configured text')
        desired = {'title': title.strip(),
                   'reasoning': self.config.voice.dedicated_reasoning,
                   'thinking_budget': self.config.voice.dedicated_thinking_budget}
        with self._lock:
            if not self._accepting:
                raise RuntimeError('Session manager is stopping')
            existing = self._metadata.get(slug)
            if existing is not None:
                changed = False
                applied = existing.get('voice_applied')
                if not isinstance(applied, dict):
                    applied = {}
                applied = dict(applied)
                for field, value in desired.items():
                    if field == 'title' and not value:
                        continue
                    if field not in applied or applied[field] != value:
                        if existing.get(field) != value:
                            existing[field] = value
                            runtime = self._runtimes.get(slug)
                            if runtime is not None and field in ('reasoning', 'thinking_budget'):
                                setattr(runtime.session, field, value)
                        applied[field] = value
                        changed = True
                if changed:
                    existing['voice_applied'] = applied
                if existing.get('archived'):
                    existing['archived'] = False
                    changed = True
                if not existing.get('voice'):
                    existing['voice'] = True
                    changed = True
                if changed:
                    atomic_json(self.path(slug) / 'meta.json', existing)
                return self._public(existing)
            if not title.strip():
                raise ValueError('Voice session title must be configured text')
            backend = self.config.roles.get('main')
            if backend is None and self.config.backends:
                backend = next(iter(self.config.backends))
            path = self.path(slug)
            path.mkdir()
            timestamp = now()
            meta = {'id': uuid.uuid4().hex, 'slug': slug, 'title': title.strip(), 'created': timestamp,
                    'updated': timestamp, 'messages': 0, 'archived': False, 'backend': backend,
                    'backend_override': False, 'debt': 0, 'workspace': '',
                    'reasoning': self.config.voice.dedicated_reasoning,
                    'thinking_budget': self.config.voice.dedicated_thinking_budget,
                    'voice': True, 'voice_applied': desired}
            atomic_json(path / 'meta.json', meta)
            self._metadata[slug] = meta
            return self._public(meta)

    def coordination_status(self):
        """Read the cross-session picture without starting dormant sessions."""
        rows = []
        with self._lock:
            runtimes = dict(self._runtimes)
            metadata = list(self._metadata.values())
        for meta in metadata:
            if meta.get('archived'):
                continue
            row = self._public(meta)
            runtime = runtimes.get(meta['slug'])
            row.update(busy=False, operator_stopped=False, recent=[])
            if runtime is not None:
                with runtime.condition:
                    active = dict(runtime.active) if runtime.active else None
                    row.update(busy=bool(active), operator_stopped=runtime.operator_stopped,
                               queued=len(runtime.pending), error=runtime.failure)
                if active:
                    row['turn'] = active
                    snapshot = runtime.events.turn_snapshot(active['id']) or {}
                    row['activity'] = {key: snapshot.get(key) for key in ('status', 'tool', 'at')}
                log = runtime.session.log
                row['recent'] = [asdict(message) for message in log.iter(max(0, log.count() - 8))]
                row['agents'] = runtime.session.agents.active_details()
            rows.append(row)
        return rows

    def coordinate_steer(self, slug, text):
        """Guide active work; never turn coordinator advice into a new human prompt."""
        self._validate_text(text)
        with self._lock:
            runtime = self._runtimes.get(slug)
        if runtime is None:
            return {'applied': False, 'reason': 'idle'}
        with runtime.condition:
            if runtime.operator_stopped:
                return {'applied': False, 'reason': 'operator_stopped'}
            if not runtime.active:
                return {'applied': False, 'reason': 'idle'}
            applied = runtime.session.steer('[' + self.config.floor_label() + ' coordination] ' + text)
            if applied:
                runtime.events.append('steer', {'turn_id': runtime.active['id'], 'from_slug': self.config.autonomy.infinity_slug})
            return {'applied': bool(applied)}

    def list_sessions(self):
        with self._lock:
            rows = []
            broken = []
            for meta in self._metadata.values():
                try:
                    rows.append(self._public(meta))
                except UnreadableSession as problem:
                    broken.append(problem)
            # One unreadable session must not hide every other session. Name the
            # record and the field so whoever owns the file can find it.
            for problem in broken:
                print(f'Session list skipped a record: {problem}', flush=True)
            rows.sort(key=lambda row: (row.get('updated') or row.get('created') or '', row.get('created') or ''),
                      reverse=True)
            return rows

    def describe(self, slug):
        """Return one session's public metadata without loading its runtime."""
        self.path(slug)
        with self._lock:
            return self._public(self._metadata[slug])

    def _unreadable_reports(self):
        """Name every session record that cannot be rendered, for a reader.

        `list_sessions` already skips these records and prints the name to stdout,
        but a print is not a payload: a client reading the list sees a shorter
        list and no reason. That is the same shape as a board that is empty
        because it is unreadable. `UnreadableSession.report()` was written for
        this and called from nowhere, so the reason never reached a reader.

        Computed per call rather than cached, so the answer does not depend on
        whether `list_sessions` happened to run first. It reads the manager's
        in-memory metadata, the same records `list_sessions` renders, so the
        notice and the list always describe the same set. A file repaired on
        disk is not picked up until the manager reloads that record, which is
        the same rule the list already follows; it is not a fresh disk read.
        """
        reports = []
        for meta in self._metadata.values():
            try:
                self._public(meta)
            except UnreadableSession as problem:
                reports.append(problem.report())
        return reports

    def update(self, slug, title=None, archived=None, backend=None, workspace=None, reasoning=None, thinking_budget=None):
        self.path(slug)
        if backend is not None:
            # '' returns the session to the default chat model.
            chosen = self.config.backends.get(backend) if backend else None
            if not isinstance(backend, str) or (backend and (chosen is None or not offered_for_chat(backend, chosen))):
                raise ValueError('Unknown chat backend')
        if title is not None and (not isinstance(title, str) or not title.strip() or len(title) > 200):
            raise ValueError('Title must be 1..200 characters')
        if archived is not None and type(archived) is not bool:
            raise ValueError('archived must be a boolean')
        if workspace is not None:
            workspace = self._workspace_value(workspace)
        if reasoning is not None and reasoning not in ('off', 'low', 'medium', 'high'):
            raise ValueError('reasoning must be off, low, medium, or high')
        if thinking_budget is not None:
            if type(thinking_budget) is not int or not 0 <= thinking_budget <= 4294967295:
                raise ValueError('thinking_budget must be an integer from 0 to 4294967295 (0 uses the engine default)')
        with self._runtime_ready:
            while slug in self._runtime_loads:
                self._runtime_ready.wait()
            meta = self._metadata[slug]
            if title is not None:
                meta['title'] = title.strip()
            if archived is not None:
                meta['archived'] = archived
            if backend is not None:
                meta['backend'] = backend or None
                meta['backend_override'] = bool(backend)
                runtime = self._runtimes.get(slug)
                if runtime is not None:
                    runtime.switch_backend(backend or None)
            if workspace is not None:
                meta['workspace'] = workspace
                runtime = self._runtimes.get(slug)
                if runtime is not None:
                    runtime.session.set_workspace(workspace or self.config.workspace or self.config.cwd)
            if reasoning is not None:
                meta['reasoning'] = reasoning
                runtime = self._runtimes.get(slug)
                if runtime is not None:
                    runtime.session.reasoning = reasoning
            if thinking_budget is not None:
                meta['thinking_budget'] = thinking_budget
                runtime = self._runtimes.get(slug)
                if runtime is not None:
                    runtime.session.thinking_budget = thinking_budget
            meta['updated'] = now()
            atomic_json(self.path(slug) / 'meta.json', meta)
            return self._public(meta)

    def delete(self, slug):
        """Remove a session and its files for good. A session with work in flight is refused."""
        path = self.path(slug)
        with self._runtime_ready:
            while slug in self._runtime_loads:
                self._runtime_ready.wait()
            if slug not in self._metadata:
                raise KeyError(slug)
            runtime = self._runtimes.get(slug)
            if runtime is not None and not runtime.idle():
                raise ValueError('Session is busy; stop it before deleting')
            self._metadata.pop(slug)
            self._runtimes.pop(slug, None)
        if runtime is not None:
            runtime.close()
        shutil.rmtree(path)
        return {'deleted': slug}

    def runtime(self, slug, internal=False):
        self.path(slug)
        with self._runtime_ready:
            while slug in self._runtime_loads:
                self._runtime_ready.wait()
            if self._closed:
                raise RuntimeError('Session manager is closed')
            if slug not in self._metadata:
                raise KeyError(slug)
            if slug in self._runtimes:
                return self._runtimes[slug]
            if not self._accepting and not internal:
                raise RuntimeError('Session manager is stopping')
            self._runtime_loads.add(slug)
            meta = self._metadata[slug]
        # Construction discovers integrations and inherits memory. Those calls
        # may wait on another process; they must not lock every session and API.
        try:
            runtime = _Runtime(self, meta)
        except BaseException:
            with self._runtime_ready:
                self._runtime_loads.remove(slug)
                self._runtime_ready.notify_all()
            raise
        with self._runtime_ready:
            if not self._accepting and not internal:
                runtime.hold()
            self._runtimes[slug] = runtime
            self._runtime_loads.remove(slug)
            self._runtime_ready.notify_all()
            return runtime

    def get(self, slug):
        return self.runtime(slug).session

    def submit(self, slug, text, reply_to=None, source=None, *, client_request_id=None, attachments=None, project_id=None, selected=None, expected_stop_generation=None):
        if project_id is not None:
            self.board.get_project(project_id)
            if not isinstance(selected or [], list) or any(not isinstance(item, str) for item in selected or []):
                raise ValueError('Selected canvas pieces must be identifiers')
        attachments = self._attachments(attachments)
        if attachments and not (isinstance(text, str) and text.strip()):
            text = 'See the attached file.' if len(attachments) == 1 else 'See the attached files.'
        self._validate_text(text)
        if source is not None and (not isinstance(source, str) or source not in ('voice', 'text')):
            raise ValueError('Invalid message source')
        if client_request_id is not None:
            if not isinstance(client_request_id, str):
                raise ValueError('Client request identity must be a UUID')
            try:
                uuid.UUID(client_request_id)
            except ValueError:
                raise ValueError('Client request identity must be a UUID') from None
        if reply_to is not None:
            text = self._quote(slug, reply_to) + text
        with self._lock:
            if not self._accepting:
                raise RuntimeError('Session manager is stopping')
            if self._metadata[slug]['archived']:
                raise ValueError('Session is archived')
        runtime = self.runtime(slug)
        if source != 'voice':
            self.autonomy.activity(slug)
        # Voice handoffs must not resume work from an utterance predating Stop.
        # Keep intake and Stop ordered through the normal runtime locks.
        with runtime.intake_lock, runtime.condition:
            if expected_stop_generation is not None:
                if runtime.operator_stop_generation != expected_stop_generation:
                    raise ValueError('Request predates operator Stop')
            if runtime.operator_stopped:
                append_record(runtime.journal, {'op': 'operator_stop', 'stopped': False})
                runtime.operator_stopped = False
                runtime.operator_cancelled.clear()
                runtime.condition.notify_all()
            return runtime.enqueue(text, turn_id=client_request_id, source=source, attachments=attachments,
                                   project_id=project_id, selected=selected)

    def _quote(self, slug, index):
        original = self._message(slug, index)
        limit = self.config.server.reply_quote_chars
        excerpt = original.text if len(original.text) <= limit else original.text[:limit].rstrip() + '...'
        speaker = self.config.operator_label_sentence() if original.kind == 'user' else self.config.agent_name
        lines = [f'> Replying to {speaker} (message {index}):'] + ['> ' + line for line in excerpt.splitlines()]
        return '\n'.join(lines) + '\n\n'

    def _message(self, slug, index):
        if not isinstance(index, int) or isinstance(index, bool) or index < 0:
            raise ValueError('Message index must be a non-negative integer')
        for message in self.get(slug).log.iter(index):
            if message.i == index:
                return message
            break
        raise ValueError(f'No message {index} in this session')

    def feedback(self, slug):
        return Feedback(self.path(slug) / 'feedback.json').all()

    def mark(self, slug, index, **change):
        self._message(slug, index)
        return Feedback(self.path(slug) / 'feedback.json').mark(index, **change)

    def steer(self, slug, text, source=None, *, project_id=None, selected=None):
        """Steer the running turn, or queue a normal turn when the session is idle."""
        self._validate_text(text)
        if source is not None and (not isinstance(source, str) or source not in ('voice', 'text')):
            raise ValueError('Invalid message source')
        with self._lock:
            if self._metadata[slug]['archived']:
                raise ValueError('Session is archived')
            if not self._accepting and slug not in self._runtimes:
                raise RuntimeError('Session manager is stopping')
        runtime = self.runtime(slug)
        if source != 'voice':
            self.autonomy.activity(slug)
        with runtime.condition:
            active = runtime.active
        context = None
        if project_id is not None:
            from .project_canvas import planning_context
            project = self.board.get_project(project_id)
            if not isinstance(selected or [], list) or any(not isinstance(item, str) for item in selected or []):
                raise ValueError('Selected canvas pieces must be identifiers')
            context = planning_context(project, selected or [])
        if active:
            if context is not None:
                applied = runtime.session.steer(text, context=context, metadata={'project_id': project_id, 'selected': selected or []})
            else:
                applied = runtime.session.steer(text)
            if applied:
                runtime.events.append('steer', {'turn_id': active['id']})
                return {'mode': 'steer', 'turn_id': active['id']}
        with self._lock:
            if not self._accepting:
                raise RuntimeError('Session manager is stopping')
            return {'mode': 'queued', 'turn_id': runtime.enqueue(text, source=source, project_id=project_id, selected=selected)}

    def interrupt(self, slug):
        runtime = self.runtime(slug)
        with runtime.condition:
            active = runtime.active
            runtime.operator_stopped = True
            runtime.operator_stop_generation += 1
            runtime.operator_cancelled.set()
            append_record(runtime.journal, {'op': 'operator_stop', 'stopped': True})
            cancelled = list(runtime.pending)
            runtime.pending.clear()
            for item in cancelled:
                runtime._set_state(item, 'cancelled', 'Stopped by operator')
            runtime.events.append('interrupt', {'turn_id': active['id'] if active else None})
        # Announce before cancellation so every client acknowledges Stop even
        # while a transport is being torn down. No queued turn replaces it.
        interrupted = runtime.session.interrupt()
        interrupted = bool(active) or interrupted
        for item in cancelled:
            runtime.events.append('queue', {'turn_id': item['id'], 'action': 'removed'})
        return {'interrupted': interrupted, 'stopped': True,
                'turn_id': active['id'] if active else None, 'cancelled_queued': len(cancelled)}

    def add_attachment(self, slug, data, name, claimed_type=''):
        self.path(slug)
        try:
            return store_attachment(self.path(slug), data, name, claimed_type)
        except ValueError as exc:
            raise ValueError(str(exc)) from None

    def add_local_attachment(self, slug, path):
        return reference_attachment(self.path(slug), path)

    def attachment_content(self, slug, file_id):
        return read_attachment(self.path(slug), file_id)

    def attachment_file(self, slug, file_id):
        self.path(slug)
        return resolve_attachment(self.path(slug), file_id)

    @staticmethod
    def _attachments(value):
        if value is None:
            return None
        if not isinstance(value, list) or len(value) > MAX_PER_TURN:
            raise ValueError('Too many attachments')
        refs = []
        seen = set()
        for item in value:
            if not isinstance(item, dict):
                raise ValueError('Invalid attachment')
            file_id = item.get('id')
            name = item.get('name')
            media = item.get('media_type')
            size = item.get('bytes')
            if not isinstance(file_id, str) or not isinstance(name, str) or not isinstance(media, str):
                raise ValueError('Invalid attachment')
            if type(size) is not int or size < 1:
                raise ValueError('Invalid attachment')
            if file_id in seen:
                raise ValueError('Duplicate attachment')
            seen.add(file_id)
            refs.append({'id': file_id, 'name': name[:120], 'media_type': media[:80], 'bytes': size})
        return refs or None

    @staticmethod
    def _validate_text(text):
        if not isinstance(text, str) or not text.strip():
            raise ValueError('Message text cannot be empty')

    def send_to_session(self, source, target, text, hop=0):
        self.messaging._require_enabled()
        if type(hop) is not int or hop < 0 or hop >= min(self.config.server.max_hops, self.config.channels.max_hops):
            raise ValueError('Session message hop limit reached')
        return self.direct_work(source, target, text)

    def create_channel(self, name):
        return self.messaging.create_channel(name)

    def subscribe_channel(self, name, slug, subscribed=True):
        return self.messaging.subscribe_channel(name, slug, subscribed)

    def publish_channel(self, name, source, text, **options):
        return self.messaging.publish_channel(name, source, text, **options)

    def direct_work(self, source, target, text, **options):
        return self.messaging.direct_work(source, target, text, **options)

    def handoff(self, source, target, brief, **options):
        return self.messaging.handoff(source, target, brief, **options)

    def deliver_work(self, **options):
        return self.messaging.deliver_work(**options)

    def _save_work_count(self, slug, count):
        meta = self._metadata[slug]
        meta['messages'] = count
        meta['updated'] = now()
        atomic_json(self.path(slug) / 'meta.json', meta)

    def turn(self, slug, turn_id):
        runtime = self.runtime(slug)
        with runtime.condition:
            return dict(runtime.turns[turn_id])

    def queued_turns(self, slug):
        """Prompts waiting behind the running turn. Not steers, not the active turn."""
        runtime = self.runtime(slug)
        with runtime.condition:
            waiting = [item for item in runtime.pending if item.get('state') == 'queued' and not item.get('wake')]
        return [{'id': item['id'], 'text': item['text'], 'created': item.get('created')} for item in waiting]

    def revise_queued(self, slug, turn_id, text):
        """Replace the text of a prompt that has not started. A running turn is left alone."""
        if not isinstance(turn_id, str) or not turn_id:
            raise ValueError('Turn identity required')
        if not isinstance(text, str) or not text.strip():
            raise ValueError('Prompt text required')
        runtime = self.runtime(slug)
        revised = runtime.session.log.redact(text)
        with runtime.condition:
            item = runtime.turns.get(turn_id)
            if item is None or item.get('state') != 'queued' or item not in runtime.pending:
                raise ValueError('That prompt is not waiting')
            item['text'] = revised
            append_record(runtime.journal, {'op': 'revise', 'id': turn_id, 'text': revised})
        runtime.events.append('queue', {'turn_id': turn_id, 'action': 'revised'})
        return {'id': turn_id, 'text': revised}

    def cancel_queued(self, slug, turn_id):
        """Drop a prompt that has not started. A running turn is left alone."""
        if not isinstance(turn_id, str) or not turn_id:
            raise ValueError('Turn identity required')
        runtime = self.runtime(slug)
        with runtime.condition:
            item = runtime.turns.get(turn_id)
            if item is None or item.get('state') != 'queued':
                raise ValueError('That prompt is not waiting')
            if item not in runtime.pending:
                raise ValueError('That prompt is not waiting')
            runtime.pending.remove(item)
            runtime._set_state(item, 'cancelled', 'Removed before it started')
        runtime.events.append('queue', {'turn_id': turn_id, 'action': 'removed'})
        return {'removed': turn_id}

    def wait(self, slug, timeout=None):
        return self.runtime(slug).wait(timeout)

    def _save_runtime(self, runtime):
        with self._lock:
            meta = self._metadata[runtime.slug]
            meta['messages'] = runtime.session.log.count()
            meta['debt'] = len(runtime.session.capture.debt())
            meta['updated'] = now()
            atomic_json(runtime.root / 'meta.json', meta)

    @contextmanager
    def _swarm_log(self, slug):
        with self._lock:
            if not self._accepting or slug not in self._metadata or self._metadata[slug]['archived']:
                raise ValueError('Swarm job requires an active saved session')
            runtime = self._runtimes.get(slug)
            if runtime is not None:
                log = runtime.session.log
            else:
                log = ChatLog(self.path(slug) / 'log', self.messaging._secret_values())
                try:
                    self.observe_log(slug, log)
                    tree = SummaryTree(self.path(slug), log, node_bytes=self.config.view.node_bytes)
                    view = View(self.path(slug), tree, high_bytes=self.config.view.high_bytes,
                                low_bytes=self.config.view.low_bytes)
                    log.subscribe(view.on_message)
                except Exception:
                    log.close()
                    raise
        try:
            yield log
        finally:
            if runtime is None:
                log.close()
            with self._lock:
                self._metadata[slug]['messages'] = log.count()
                self._metadata[slug]['updated'] = now()
                atomic_json(self.path(slug) / 'meta.json', self._metadata[slug])

    def create_swarm_job(self, slug, objective, workers, *, job_id=None):
        self.swarm_jobs._enabled()
        self.swarm_jobs._workers(objective, workers)
        with self._swarm_log(slug) as log:
            return self.swarm_jobs.create(objective, workers, session_slug=slug, log=log, job_id=job_id)

    def run_swarm_job(self, job_id):
        job = self.swarm_jobs.get(job_id)
        with self._swarm_log(job['session_slug']) as log:
            return self.swarm_jobs.run(job_id, log=log)

    def board_status(self):
        with self._lock:
            slugs = [row['slug'] for row in self.list_sessions()]
            return board_status(self.config, slugs)

    def board_brief(self):
        with self._lock:
            slugs = [row['slug'] for row in self.list_sessions()]
            return board_brief(self.config, slugs)

    def oversight_report(self):
        """Quality-first reading of how sessions actually behaved. Read-only."""
        with self._lock:
            roots = [self.path(row['slug']) for row in self.list_sessions()]
            trials = list(self.trials.trials.values())
        return oversight_report(self.config, roots, trials=trials)

    # --- bounded A/B trials -------------------------------------------------
    # The 'auto' oversight policy says it waits for a measurement that shows a
    # change is better. These three methods are that measurement, and nothing
    # else: a trial is named by a caller, bounded in turns, graded on one
    # instrument for both arms, and adopted only on a win.

    def start_trial(self, slug, lever, candidate, turns, instrument, why, trial_id=None, margin=None):
        """Begin a trial on a session, holding it where it is for the baseline arm."""
        from .trial import start as begin, validate
        self.path(slug)
        with self._lock:
            if slug not in self._metadata:
                raise KeyError(slug)
            meta = self._metadata[slug]
            current = meta.get('reasoning') or 'low' if lever == 'reasoning' else meta.get('backend')
        validate(lever, candidate, turns, instrument, why, self.config)
        trial = begin(trial_id or uuid.uuid4().hex, slug, lever, candidate, current,
                      turns, instrument, why, margin=margin)
        return self.trials.add(trial)

    def advance_trials(self, slug=None):
        """Move open trials one step. The only place a trial changes a session.

        Called from two places on purpose. The autonomy tick advances every open
        trial, so a house with autonomy on keeps measuring. A turn ending
        advances only that session's trial, because a trial measures turns: with
        autonomy off, nothing else would ever move it.
        """
        # Each open trial costs a bounded read of its session's events, so this is
        # throttled: a trial measures turns, which take far longer than a tick.
        now = time.time()
        if now - self._trials_advanced_at < self.config.autonomy.trial_poll_seconds:
            return {'moved': [], 'open': len(self.trials.open_for_all())}
        self._trials_advanced_at = now
        moved = []
        for trial in list(self.trials.trials.values()):
            if trial['phase'] == 'decided':
                continue
            if slug is not None and trial['slug'] != slug:
                continue
            subject = trial['slug']
            if subject not in self._metadata:
                continue
            rows = session_rows(self.path(subject), self.config)
            updated, instruction = advance(trial, rows, self.config, subject)
            if updated != trial:
                self.trials.update(updated)
                moved.append(updated['id'])
            if instruction:
                self._apply_trial(instruction['apply'])
        return {'moved': moved, 'open': len(self.trials.open_for_all())}

    def trials_view(self):
        """Every trial on disk, described for a reader who did not start it.

        Read-only. A trial carries its own window, reading and verdict, so this
        reports what was measured rather than asking anyone to trust a summary.
        """
        from .trial import INSTRUMENTS, LEVERS, annotate_trial, describe

        with self._lock:
            trials = list(self.trials.trials.values())
        return {
            'enabled': True,
            'policy': getattr(self.config.autonomy, 'oversight_policy', 'approve'),
            'unreadable': self.trials.unreadable,
            'levers': list(LEVERS),
            'instruments': dict(INSTRUMENTS),
            'trials': [annotate_trial(dict(trial, line=describe(trial))) for trial in trials],
            'open': [trial['id'] for trial in trials if trial['phase'] != 'decided'],
        }

    def _apply_trial(self, instruction):
        """Apply one named change to one session. Refuses anything not in LEVERS."""
        from .trial import LEVERS
        lever = instruction.get('lever')
        if lever not in LEVERS:
            raise ValueError('a trial may only change a backend or a reasoning level')
        slug = instruction['slug']
        value = instruction['value']
        if lever == 'backend':
            self.update(slug, backend=value or None)
        else:
            self.update(slug, reasoning=value)
        return {'applied': True, 'slug': slug, 'lever': lever, 'value': value}

    def board_link(self, ordinal, revision):
        with self._lock:
            slugs = [row['slug'] for row in self.list_sessions()]
            return board_link(self.config, slugs, ordinal, revision)

    def board_view(self):
        with self._lock:
            slugs = [row['slug'] for row in self.list_sessions()]
            return read_board_view(self.config, slugs)

    def identities_view(self):
        """People and live sessions in one namespace, plus the owner strings that
        resolve to nobody.

        The unresolved list is the reason this route exists: it is the report that
        says which cards are owned by a handle nobody holds, which is the failure a
        free-string owner could never surface.
        """
        from .identities import unresolved_owners
        with self._lock:
            slugs = [row['slug'] for row in self.list_sessions()]
            view = read_board_view(self.config, slugs)
            registry = self.identities
            body = registry.status()
            body['unresolved_owners'] = unresolved_owners(registry, view)
            body['record_errors'] = list(self.identity_record_errors)
            return body

    def identity_create(self, name, display_name=None):
        """Record a person from a client, so a second human does not need config.toml."""
        with self._lock:
            return self.identities.store.create(name, display_name, 'person')

    def identity_rename(self, identity_id, name=None, display_name=None):
        with self._lock:
            return self.identities.store.rename(identity_id, name, display_name)

    def board_move(self, ordinal, revision, status, actor=None, card_id=None):
        with self._lock:
            slugs = [row['slug'] for row in self.list_sessions()]
            return move_board_card(self.board, self.config, slugs, ordinal, revision, status,
                                   self._as_slug(actor), card_id)

    def board_start(self, ordinal, revision, session, receipt='', card_id=None):
        with self._lock:
            slugs = [row['slug'] for row in self.list_sessions()]
            return start_board_card(self.board, self.config, slugs, ordinal, revision,
                                    self._as_slug(session), receipt, card_id)

    def board_claim(self, ordinal, revision, owner=None, actor=None, card_id=None):
        with self._lock:
            slugs = [row['slug'] for row in self.list_sessions()]
            return board_claim(self.board, self.config, slugs, ordinal, revision,
                               self._as_slug(owner) or '', actor=self._as_slug(actor), card_id=card_id)

    def board_note(self, ordinal, revision, note, actor=None, card_id=None):
        with self._lock:
            slugs = [row['slug'] for row in self.list_sessions()]
            return board_note(self.board, self.config, slugs, ordinal, revision, note,
                              actor=self._as_slug(actor), card_id=card_id)

    def board_create(self, title, *, note='', project='', owner='', evidence=None):
        """The operator writes a card. It starts unclaimed so any session may take it."""
        with self._lock:
            from .board import validate_evidence
            evidence = [] if evidence is None else evidence
            validate_evidence(evidence)
            cleaned = []
            for item in evidence:
                path, media = self.attachment_file(item['session_slug'], item['id'])
                if media != item['media_type'] or path.stat().st_size != item['size']:
                    raise ValueError('Evidence does not match its stored attachment')
                cleaned.append({**item, 'name': self.messaging._redact(item['name'])})
            slugs = [row['slug'] for row in self.list_sessions()]
            return board_create(self.board, self.config, slugs, title, note=note, project=project,
                                owner=self._as_slug(owner) or '', evidence=cleaned)

    def board_answer(self, ordinal, revision, question_id, answer, actor=None, card_id=None):
        """The operator answers a card question; the answer is what unblocks the card."""
        with self._lock:
            slugs = [row['slug'] for row in self.list_sessions()]
            return board_answer(self.board, self.config, slugs, ordinal, revision, question_id,
                                answer, actor=self._as_slug(actor), card_id=card_id)

    def board_accept(self, ordinal, revision, by, evidence='', note='', card_id=None):
        """Record an acceptance on a card, addressed the way every other board write is.

        The revision fence is the same one the other writes use, so an acceptance cannot
        land against a card that moved underneath the reader.
        """
        with self._lock:
            slugs = [row['slug'] for row in self.list_sessions()]
            return accept_board_card(self.board, self.config, slugs, ordinal, revision, by,
                                     evidence, note, card_id)

    def board_triage_intake(self, ordinal, revision, subject=None, compatibility=None, summary=None,
                            actor=None, card_id=None):
        """Record the intake reviewer's findings on a gathered card."""
        with self._lock:
            slugs = [row['slug'] for row in self.list_sessions()]
            return triage_intake_card(self.board, self.config, slugs, ordinal, revision, subject,
                                      compatibility, summary, actor=self._as_slug(actor), card_id=card_id)

    def board_accept_intake(self, ordinal, revision, by, note='', card_id=None):
        """Accept a triaged intake card as work, from the page."""
        with self._lock:
            slugs = [row['slug'] for row in self.list_sessions()]
            return accept_intake_card(self.board, self.config, slugs, ordinal, revision, by, note, card_id)

    def board_ignore_intake(self, ordinal, revision, by, reason, card_id=None):
        """Decline an intake card as work, keeping it as evidence with the reason."""
        with self._lock:
            slugs = [row['slug'] for row in self.list_sessions()]
            return ignore_intake_card(self.board, self.config, slugs, ordinal, revision, by, reason, card_id)

    def board_revise(self, ordinal, revision, title=None, note=None, actor=None, card_id=None, priority=_MISSING):
        with self._lock:
            slugs = [row['slug'] for row in self.list_sessions()]
            return board_revise(self.board, self.config, slugs, ordinal, revision, title=title,
                                note=note, actor=self._as_slug(actor), card_id=card_id, priority=priority)

    def board_file(self, ordinal, revision, project, actor=None, card_id=None):
        with self._lock:
            slugs = [row['slug'] for row in self.list_sessions()]
            return board_file(self.board, self.config, slugs, ordinal, revision, project,
                              actor=self._as_slug(actor), card_id=card_id)

    def board_remove(self, ordinal, revision, actor=None, card_id=None):
        with self._lock:
            slugs = [row['slug'] for row in self.list_sessions()]
            return board_remove(self.board, self.config, slugs, ordinal, revision,
                                actor=self._as_slug(actor), card_id=card_id)

    def board_project_create(self, title, *, description='', manager='', note='', actor=None, kind='planning'):
        with self._lock:
            slugs = [row['slug'] for row in self.list_sessions()]
            # A project needs a creator the board can name. The page sends no actor,
            # so the configured operator takes that place; without one, the companion.
            creator = actor
            if creator is None:
                creator = self.config.operator_name.strip() or self.config.agent_name.strip()
            return board_project_create(self.board, self.config, slugs, title, description=description,
                                        manager=self._as_slug(manager) or '', note=note,
                                        actor=self._as_slug(handle_for(creator)), kind=kind)

    def board_project_update(self, project_id, *, title=None, description=None, note=None, manager=None, status=None, kind=None):
        with self._lock:
            slugs = [row['slug'] for row in self.list_sessions()]
            return board_project_update(self.board, self.config, slugs, project_id, title=title,
                                        description=description, note=note, manager=manager, status=status, kind=kind)

    def board_steps(self, project_id, steps):
        with self._lock:
            slugs = [row['slug'] for row in self.list_sessions()]
            return board_steps(self.board, self.config, slugs, project_id, steps)

    def board_project_canvas(self, project_id, changes=None):
        from .project_canvas import empty_canvas
        with self._lock:
            if changes is not None:
                def validate_media(node):
                    # Placement consumes metadata; HTTP/model reads enforce the byte budget.
                    _, media = attachment_location(self.path(node['session_slug']), node['attachment_id'])
                    if node['kind'] == 'image' and not media.startswith('image/'):
                        raise ValueError('Image pieces require image attachments')
                self.board.update_canvas(project_id, self.messaging.redact_value(changes), validate_media=validate_media)
            project = self.board.get_project(project_id)
            cards = []
            referenced = set()
            for node in project.get('canvas', empty_canvas())['nodes']:
                if node.get('card_id'):
                    referenced.add(node['card_id'])
            for card in self.board.list():
                if card.get('project') == project_id or card['id'] in referenced:
                    cards.append({'id': card['id'], 'title': card['title'], 'status': card['status'],
                                  'owner': card.get('owner', ''), 'removed': card.get('removed', False)})
            return self.messaging.redact_value({'project_id': project_id, 'title': project['title'],
                    'canvas': project.get('canvas', empty_canvas()), 'cards': cards,
                    'steps': project['steps'], 'session_slug': project['session_slug'], 'manager': project['manager']})

    def board_project_append(self, project_id, text, *, step=False):
        with self._lock:
            if step:
                self.board.add_step(project_id, text)
            else:
                self.board.append_project_note(project_id, text)
            slugs = [row['slug'] for row in self.list_sessions()]
            return read_board_view(self.config, slugs)

    def board_step_check(self, project_id, step_id, done):
        with self._lock:
            slugs = [row['slug'] for row in self.list_sessions()]
            return board_step_check(self.board, self.config, slugs, project_id, step_id, done)

    def refresh_open_memory(self, origin=None):
        """Probe the process-wide memory choice once and apply it to every open session."""
        with self._lock:
            sessions = [runtime.session for runtime in self._runtimes.values()]
        sessions = [session for session in sessions
                    if isinstance(getattr(session, 'memory_choice', None), dict)
                    and getattr(session, '_apply_memory_choice', None) is not None]
        if not sessions:
            return None
        sample = origin if origin in sessions else sessions[0]
        from .bundled_engine import _copy_choice, refresh_shared_memory
        refreshed = refresh_shared_memory(sample.config, sample.memory_choice)
        for session in sessions:
            session._apply_memory_choice(_copy_choice(refreshed))
        return getattr(sample, 'memory_choice', refreshed)

    def status(self, *, connection=False):
        # Copy the runtime and metadata dicts under the lock, then assemble the
        # status outside it. Sessions may read one turn stale. The per-backend
        # and AetherMind readers carry their own locks.
        with self._lock:
            runtimes = dict(self._runtimes)
            metadata = dict(self._metadata)
            # Same shape as list_sessions: skip unreadable records here, they are
            # named below in sessions_unreadable instead.
            slugs = []
            for meta in metadata.values():
                try:
                    slugs.append(self._public(meta)['slug'])
                except UnreadableSession:
                    continue
            unreadable = self._unreadable_reports()

        def board_row():
            # board_status re-takes the manager lock; keeping that call out of
            # status() keeps this projection lock-free. Pass it the slugs the
            # lock already produced instead of re-deriving them.
            return board_status(self.config, slugs)
        observations = []
        seen = set()
        for runtime in runtimes.values():
            for backend in (runtime.session.backend, runtime.session.fast_backend):
                if not isinstance(backend, Backend) or id(backend) in seen:
                    continue
                seen.add(id(backend))
                stamp, snapshot = backend.status_snapshot()
                observations.append((stamp, backend.name, backend.account, snapshot))
        debt = 0
        for meta in metadata.values():
            debt += meta.get('debt', 0)
        reachable = None
        memory = self.config.aethermind
        from .bundled_engine import RECEIPT_ABSENT_SENTENCE
        from .engine_manifest import engine_packaging
        external_configured = bool(memory.url or memory.endpoints)
        packaging = engine_packaging(self.config)
        # active is set only after a choice names a process ensure_bundled_engine
        # returned, or an external service that validated. A checkout whose
        # bytes match and that has no receipt has not started an engine.
        if external_configured:
            pending_reason = 'External service configured; not checked yet'
        elif packaging == 'bundled':
            pending_reason = 'No external service configured'
        elif packaging == 'present':
            pending_reason = RECEIPT_ABSENT_SENTENCE
        else:
            pending_reason = 'Packaged engine is not in this install'
        from .bundled_engine import engine_log_sighting
        log_path, log_error = engine_log_sighting(self.config)
        memory_status = {'configured': external_configured or packaging == 'bundled',
                         'engine': packaging,
                         'engine_log': bool(log_path and log_path.is_file() and not log_path.is_symlink()),
                         'reachable': None, 'reason': pending_reason}
        if log_error:
            memory_status['engine_log_error'] = log_error
        from .engine_lock import EmbeddedEngineLockError, embedded_lock_backend
        embedded = None
        external = False
        for runtime in runtimes.values():
            session = runtime.session
            if getattr(session, 'uses_embedded_engine', False):
                embedded = session
            elif getattr(getattr(session, 'capture', None), 'client', None) is not None:
                external = True
        try:
            if embedded is not None:
                lock_backend = embedded_lock_backend(self.config, embedded)
            elif external:
                lock_backend = None
            else:
                lock_backend = embedded_lock_backend(self.config)
        except EmbeddedEngineLockError as error:
            if 'last_error' not in memory_status:
                memory_status['last_error'] = str(error)
            lock_backend = None
        if lock_backend:
            memory_status['lock_backend'] = lock_backend
            if lock_backend == 'unavailable':
                from .aethermind import EMBEDDED_LOCK_REFUSAL
                memory_status['lock_detail'] = EMBEDDED_LOCK_REFUSAL
        latest_tree = None
        latest_connection = None
        latest_error = None
        latest_error_at = -1
        latest_category = None
        configured_endpoints = list(dict.fromkeys(memory.endpoints or ([memory.url] if memory.url else [])))
        self.refresh_open_memory()
        for runtime in runtimes.values():
            capture = runtime.session.capture
            label = service_error_name(getattr(capture, 'service_error', None))
            if label is not None:
                stamp = getattr(capture, 'service_error_at', 0) or 0
                if stamp >= latest_error_at:
                    latest_error = label
                    latest_error_at = stamp
                    category = getattr(capture, 'service_error_category', None)
                    latest_category = category if category in ('authentication', 'permission', 'unreachable', 'timeout', 'tool') else None
            client = capture.client
            if client is not None and not isinstance(client, AetherMindClient):
                reachable = runtime.session.capture._inheritance is not None
            if isinstance(client, AetherMindClient):
                endpoints = client.status()['endpoints']
                choice = getattr(runtime.session, 'memory_choice', None) or {}
                expected = list(choice.get('endpoints') or [])
                if not expected and choice.get('url'):
                    expected = [choice['url']]
                if not expected:
                    expected = configured_endpoints
                if [row['endpoint'] for row in endpoints] != expected:
                    continue
                stamps = [row['last_probe'] for row in endpoints if row['last_probe'] is not None]
                if stamps and (latest_connection is None or max(stamps) > latest_connection):
                    latest_connection = max(stamps)
                    health = [row['healthy'] for row in endpoints]
                    if True in health:
                        reachable = True
                    elif all(value is False for value in health):
                        reachable = False
                    else:
                        reachable = None
                stamp, state = client.tree_status_snapshot(
                    memory.personal_project or memory.project,
                    endpoints=configured_endpoints)
                if stamp is not None and (latest_tree is None or stamp > latest_tree):
                    latest_tree = stamp
                    memory_status['tree_status'] = state
                if latest_error is None:
                    for row in endpoints:
                        error_text = row.get('error')
                        if row.get('healthy') is False and isinstance(error_text, str) and error_text.strip():
                            latest_error = error_text.replace('\n', ' ').strip()
                            latest_category = 'unreachable'
                            break
        memory_status['reachable'] = reachable
        choices = [getattr(runtime.session, 'memory_choice', None) for runtime in runtimes.values()]
        choices = [choice for choice in choices if isinstance(choice, dict)]
        failed = [choice for choice in choices if _choice_error_text(choice)]
        chosen = failed[-1] if failed else (choices[-1] if choices else None)
        if chosen:
            source = chosen.get('source') or ''
            returned = chosen.get('url') or ''
            if source == 'external':
                memory_status['active'] = 'external'
                memory_status['configured'] = True
                memory_status['reason'] = chosen.get('reason') or memory_status['reason']
            elif source == 'embedded' and returned:
                memory_status['active'] = 'embedded'
                memory_status['configured'] = True
                memory_status['process'] = returned
                memory_status['reason'] = chosen.get('reason') or memory_status['reason']
            else:
                memory_status.pop('active', None)
                memory_status.pop('process', None)
                memory_status['configured'] = False
                memory_status['reason'] = chosen.get('reason') or memory_status['reason']
            shown_error = _choice_error_text(chosen)
            if shown_error:
                memory_status['external_error'] = shown_error
                if latest_error is None:
                    latest_error = shown_error
                    latest_category = chosen.get('external_category')
        if memory_status.get('active') == 'external':
            stops = [choice.get('embedded_stop') for choice in choices
                     if isinstance(choice.get('embedded_stop'), str) and choice.get('embedded_stop').strip()]
            if stops:
                memory_status['embedded_stop'] = stops[-1].strip()
        if latest_error:
            memory_status['last_error'] = latest_error
            if latest_category:
                memory_status['error_category'] = latest_category
            target = next((item for item in configured_endpoints if isinstance(item, str) and item.strip()), '')
            if target:
                memory_status['external_target'] = target
        sessions = {}
        for slug, meta in metadata.items():
            runtime = runtimes.get(slug)
            # The inheritance digest is kilobytes per loaded session and the page
            # already reads it from /view on the same open. Carrying it here made
            # every 15-second status poll pay for text nobody read twice.
            agents = runtime.session.agents.active_details() if runtime else []
            row = {'slug': slug, 'debt': meta.get('debt', 0),
                   'inheritance_loaded': bool(runtime and runtime.inheritance),
                   'busy': bool(runtime and runtime.active),
                   'operator_stopped': bool(runtime and runtime.operator_stopped),
                   'active_agents': len(agents),
                   'agents': agents,
                   'queued': len(runtime.pending) if runtime else 0,
                   'error': runtime.failure if runtime else None}
            # The serialized handle, beside the slug. A coordinator that needs to
            # name a session without depending on its title reads this.
            if isinstance(meta.get('id'), str) and meta['id']:
                row['id'] = meta['id']
            sessions[slug] = row
        report = {'backends': list(self.config.backends),
                'agent_backends': (None if connection else backend_catalog(self.config)),
                'chat_backends': [name for name, item in self.config.backends.items() if offered_for_chat(name, item)],
                'backend_credentials': {name: credential_source(self.config, name, redact=True) for name in self.config.backends},
                'roles': {'main': self.config.roles.get('main', ''), 'failover': self.config.roles.get('failover', '')},
                'agent_name': self.config.agent_name,
                # The handle a board write from this page records as its owner. The
                # page cannot invent one: claiming needs a slug, and an empty owner
                # releases a card instead. Empty when no operator name is configured.
                'operator': handle_for(self.config.operator_name) if self.config.operator_name.strip() else '',
                'approval': self.approval_status(),
                'ui': {'yolo_banner': self.config.ui.yolo_banner,
                       'easter_egg_f_it': self.config.ui.easter_egg_f_it,
                       'error_banner_seconds': self.config.ui.error_banner_seconds, 'history_page': self.config.ui.history_page, 'item_body_chars': self.config.ui.item_body_chars, 'item_summary_chars': self.config.ui.item_summary_chars, 'long_press_ms': self.config.ui.long_press_ms, 'approval_poll_ms': self.config.ui.approval_poll_ms,
                       'view_page_lines': self.config.ui.view_page_lines,
                       'theme': self.config.ui.theme, 'mark': self.config.ui.mark, 'skin': self.config.ui.skin, 'accent_color': self.config.ui.accent_color,
                       'show_thoughts': self.config.ui.show_thoughts,
                       'send_shortcut': self.config.ui.send_shortcut},
                'provider_status': (None if connection else build_provider_status(self.config, observations)),
                'task_status': (None if connection else task_status(self.config)),
                'job_status': (None if connection else job_status(self.config)),
                'board_status': (None if connection else board_row()),
                'infrastructure_status': (None if connection else self.infrastructure.status()),
                'grafana': (None if connection else self.grafana.status()),
                'voice': self.voice.status(),
                'jev': ({'enabled': self.config.jev.enabled} if connection else judge_status(self.config, [self.path(slug) for slug in metadata])),
                'aethermind': memory_status,
                # A record that cannot be rendered is not an absent session. Carry
                # the reason here so the sidebar can say which file and which field,
                # instead of a shorter list that reads as "that is all there is".
                'sessions_unreadable': unreadable,
                'debt_total': debt, 'sessions': sessions, 'session_count': len(metadata),
                'host': host_report(),
                'migration_notes': list(getattr(self.config, 'migration_notes', []) or [])}
        if self.identity_record_errors:
            report['identity_record_errors'] = list(self.identity_record_errors)
        if connection:
            fields = ('backends', 'chat_backends', 'backend_credentials', 'roles', 'agent_name', 'operator',
                      'approval', 'ui', 'voice', 'jev', 'aethermind',
                      'sessions_unreadable', 'debt_total', 'sessions', 'session_count', 'host')
            return {key: report[key] for key in fields}
        return report

    # Approval switch: the configured policy is the safe mode; yolo auto-approves every tool.
    def _approval_state_path(self):
        path = Path(self.config.tools.approval_state_file).expanduser()
        return path if path.is_absolute() else Path(self.config.home) / path

    def _init_approval(self):
        configured = self.config.tools.approval
        self._configured_approval = configured
        self._safe_approval = 'deny_risky' if configured in AUTO_APPROVE else configured
        path = self._approval_state_path()
        if not self.config.tools.yolo_switch or not path.is_file():
            return
        try:
            state = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return
        mode = state.get('mode') if isinstance(state, dict) else None
        if mode == 'yolo':
            self.config.tools.approval = 'yolo'
        elif mode == 'safe':
            self.config.tools.approval = self._safe_approval

    def approval_status(self):
        current = self.config.tools.approval
        return {'mode': current, 'yolo': current in AUTO_APPROVE, 'configured': self._configured_approval,
                'safe_mode': self._safe_approval, 'switchable': bool(self.config.tools.yolo_switch)}

    def set_yolo(self, enabled):
        if not isinstance(enabled, bool):
            raise ValueError('yolo must be true or false')
        if not self.config.tools.yolo_switch:
            raise ValueError('The Unleashed switch is disabled in config (tools.yolo_switch)')
        value = 'yolo' if enabled else self._safe_approval
        path = self._approval_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + '.tmp')
        temporary.write_text(json.dumps({'mode': 'yolo' if enabled else 'safe', 'updated': now()}), encoding='utf-8')
        os.replace(temporary, path)
        with self._lock:
            # Every session registry reads this ToolsConfig, so this one write is what they all enforce.
            self.config.tools.approval = value
        return self.approval_status()

    def settings_view(self):
        return self.settings.view()

    def apply_settings(self, body):
        return self.settings.apply(body)

    def set_ui(self, yolo_banner):
        if not isinstance(yolo_banner, bool):
            raise ValueError('yolo_banner must be true or false')
        self.config_editor.apply([{'path': ['ui', 'yolo_banner'], 'value': yolo_banner}], label='yolo-banner')
        self.config.ui.yolo_banner = yolo_banner
        return {'yolo_banner': yolo_banner}

    def drain(self, timeout=None):
        """Before a restart: refuse new turns, let running turns finish, interrupt any still running at the deadline."""
        settings = self.config.server
        timeout = settings.drain_seconds if timeout is None else timeout
        runtimes = self.hold()
        deadline = time.monotonic() + timeout
        late = [runtime for runtime in runtimes if not runtime.wait_active(deadline - time.monotonic())]
        for runtime in late:
            runtime.session.interrupt('restart')
        stuck = [runtime.slug for runtime in late if not runtime.wait_active(settings.interrupt_grace_seconds)]
        return {'drained': not stuck, 'interrupted': [runtime.slug for runtime in late]}

    def hold(self):
        """Refuse new turns and start no queued ones; running turns may finish.

        Split out of drain so an idle restart can stop taking work *before* it
        waits. Waiting first left a gap: a queued turn could start while the
        wait was looking, and then the wait covered it and the deadline fell
        through to an interruption.
        """
        with self._lock:
            self._accepting = False
        self.autonomy.close()
        with self._lock:
            runtimes = list(self._runtimes.values())
        for runtime in runtimes:
            runtime.hold()
        return runtimes

    def busy_running(self, stall_seconds=None):
        """True when any session has a turn running that is still making progress.

        An idle restart waits on this instead of on the requesting turn: drain
        cannot finish while the turn that asked for the restart is still inside
        the request, so the restart has to wait for that turn to end rather than
        interrupt it.

        Queued turns are deliberately excluded. A drain already refuses new
        turns, keeps queued ones, and the next runtime resumes them (see
        test_drain_finishes_the_running_turn_keeps_queued_turns_and_they_resume),
        so waiting for a queue buys nothing: it just holds the reload open until
        the queue runs itself out, and if the queue is longer than
        restart_stall_seconds the deadline falls through to drain and interrupts
        a running turn anyway. That is the interruption this wait exists to
        avoid, so the wait must be able to end without it.

        A running turn counts as busy only while it is producing something:
        durable events or streamed deltas. Measured on this home, 110 of 663
        turns (16.6%) ran longer than 30 minutes and 26 ran longer than an hour,
        so any wait bounded by turn *duration* interrupts the very work it
        exists to protect. Silence is the signal that a turn is wedged: of those
        663 turns only 8 ever went quiet for more than 30 minutes, and each of
        those was a single model call that returned nothing for hours.
        """
        stall = self.config.server.restart_stall_seconds if stall_seconds is None else stall_seconds
        now_monotonic = time.monotonic()
        with self._lock:
            runtimes = list(self._runtimes.values())
        for runtime in runtimes:
            with runtime.condition:
                if runtime.active and now_monotonic - runtime.progress_at < stall:
                    return True
        return False

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._accepting = False
        self.autonomy.close()
        self.voice.close()
        self.infrastructure.stop()
        self.messaging.wait_idle()
        with self._runtime_ready:
            while self._runtime_loads:
                self._runtime_ready.wait()
        while True:
            with self._lock:
                runtimes = list(self._runtimes.values())
            for runtime in runtimes:
                try:
                    runtime.wait()
                except RuntimeError:
                    if runtime.failure is None:
                        raise
            with self._lock:
                current = list(self._runtimes.values())
            idle = current == runtimes and all(runtime.idle() for runtime in current)
            if idle:
                with self._runtime_ready:
                    if self._runtime_loads:
                        self._runtime_ready.wait()
                        continue
                    if list(self._runtimes.values()) != current:
                        continue
                    self._closed = True
                break
        failure = None
        try:
            for runtime in runtimes:
                try:
                    runtime.close()
                except Exception as error:
                    failure = error
        finally:
            fcntl.flock(self._owner, fcntl.LOCK_UN)
            self._owner.close()
        if failure is not None:
            raise RuntimeError(f'Session shutdown failed ({type(failure).__name__})') from None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
