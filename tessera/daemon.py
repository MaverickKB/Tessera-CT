"""Transport-independent session engine and local client protocol methods."""

from contextlib import contextmanager
from dataclasses import asdict, replace
from pathlib import Path
import threading
import uuid

from .daemon_agents import DaemonAgents
from .log import ChatLog
from .mirror import AppendMirror
from .rpc import RpcError
from .sessions import SessionManager, read_records
from .tree import SummaryTree
from .view import View


METHODS = ['status', 'thread/start', 'thread/list', 'thread/resume', 'thread/read',
           'thread/update', 'turn/start', 'turn/steer', 'turn/interrupt', 'view/read', 'view/zoom',
           'memory/recall', 'memory/sync', 'swarm/create', 'swarm/run', 'swarm/show',
           'agent/plan', 'agent/merge', 'agent/spawn', 'agent/run', 'agent/show', 'agent/list', 'agent/send',
           'channel/create', 'channel/list', 'channel/subscribe', 'channel/send',
           'channel/deliver', 'channel/events', 'board/read', 'voice/start', 'voice/events', 'voice/stop', 'voice/local', 'shutdown']


def fields(params, allowed=(), required=()):
    if not isinstance(params, dict) or set(params) - set(allowed) or not set(required) <= set(params):
        raise RpcError(-32602, 'Invalid method parameters')


def integer(params, name, default, minimum=0, maximum=None):
    value = params.get(name, default)
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        raise RpcError(-32602, 'Invalid ' + name)
    return value


class Daemon:
    def __init__(self, config, *, backend_factory=None, swarm_backend_factory=None, mirror=None):
        # Attaching metadata is passive; only explicit resume/turn loads pending work.
        self.config = replace(config, server=replace(config.server, resume_turns=False),
                              aethermind=replace(config.aethermind, create_project=False)).validate()
        self.backend_factory = backend_factory
        self.swarm_backend_factory = swarm_backend_factory
        self.mirror = mirror or AppendMirror(self.config)
        self.manager = None
        self.agents = DaemonAgents(self)
        self.peer = None
        self.watches = {}
        self.watch_lock = threading.RLock()
        self.stopping = threading.Event()
        self.pending_approvals = {}
        self.approval_lock = threading.Lock()
        from .inspector import InspectorServer
        self.inspector = InspectorServer(self)
        # Bound only if [inspector] enabled; failure leaves the daemon running.
        self.inspector.start()

    def initialize(self, params):
        fields(params, ('clientInfo', 'capabilities'))
        for value in params.values():
            if not isinstance(value, dict):
                raise RpcError(-32602, 'Initialization metadata must be objects')
        return {'serverInfo': {'name': 'tessera', 'protocolVersion': 1},
                'capabilities': {'transport': 'stdio', 'methods': METHODS,
                                 'agentMessageDeltas': False, 'approvals': True,
                                 'interrupt': True, 'steer': True}}

    def _manager(self):
        if self.manager is None:
            self.manager = SessionManager(self.config, backend_factory=self.backend_factory,
                swarm_backend_factory=self.swarm_backend_factory, approval_request=self._approve,
                human_request=self._human, log_observer=self._observe_log,
                event_observer=self._observe_event, session_factory=self.agents.session,
                work_reply_allowed=self.agents.allow_reply)
            self.manager.swarm_jobs.human = lambda decision: self._human(None, None, decision)
        return self.manager

    def _observe_log(self, slug, log):
        self.mirror.attach(slug, self.manager._metadata[slug]['id'], log)

    def _observe_event(self, slug, record):
        self.mirror.record(slug, self.manager._metadata[slug]['id'], 'event', record)

    def _approve(self, slug, turn_id, name, arguments):
        runtime = self.manager._runtimes.get(slug)
        if runtime is not None:
            arguments = runtime.session.log.redact(arguments)
        item = uuid.uuid4().hex
        with self.approval_lock:
            self.pending_approvals[item] = {'id': item, 'threadId': slug, 'turnId': turn_id, 'name': name}
        try:
            return self.peer.approve('item/tool/requestApproval', {
                'threadId': slug, 'turnId': turn_id, 'itemId': item,
                'name': name, 'arguments': arguments})
        finally:
            with self.approval_lock:
                self.pending_approvals.pop(item, None)

    def pending_approval_list(self):
        with self.approval_lock:
            return [dict(row) for row in self.pending_approvals.values()]

    def companion_status(self):
        from .companions import Companions
        manager = self.manager
        if manager is not None:
            with manager._lock:
                for runtime in manager._runtimes.values():
                    companions = getattr(runtime.session, 'companions', None)
                    if companions is not None:
                        return companions.status()
        # No resident session yet: configuration only, no network.
        return Companions(replace(self.config, companions=self.config.companions), paused=True).status() | {
            'paused': (self.config.home / 'run' / 'companions-paused.json').exists()}

    def companion_notices(self):
        notices = []
        manager = self.manager
        if manager is not None:
            with manager._lock:
                for runtime in manager._runtimes.values():
                    companions = getattr(runtime.session, 'companions', None)
                    if companions is not None:
                        notices.extend(companions.notices())
        return list(dict.fromkeys(notices))

    def inspect_status(self):
        import os
        manager = self.manager
        in_flight = []
        if manager is not None:
            with manager._lock:
                for runtime in manager._runtimes.values():
                    if runtime.active:
                        in_flight.append({'threadId': runtime.slug, 'turnId': runtime.active['id']})
        mirror = self.mirror.status()
        return {'pid': os.getpid(), 'protocolVersion': 1, 'accepting': manager is None or manager._accepting,
                'inFlight': in_flight, 'lockHolderPid': os.getpid() if manager is not None else None,
                'companionNotices': self.companion_notices(), 'mirrorNotice': mirror.get('notice'),
                'companions': self.companion_status(), 'inspector': self.inspector.status(),
                'approval': self.config.tools.approval, 'livePackage': _live_package(),
                'sourceRoot': str(Path(self.config.inspector.source_root).expanduser().resolve()) if self.config.inspector.source_root else None}

    def append_inspector_note(self, slug, text, journal_id):
        """The daemon, as lock holder, appends a visible note. Never a prompt rewrite."""
        manager = self._manager()
        with manager._lock:
            loaded = slug in manager._runtimes
            with self._surface(slug) as (log, view):
                message = log.append('note', 'Inspector note: ' + text, {'source': 'inspector', 'journal_id': journal_id})
                if not loaded:
                    # A temporary surface's view is not subscribed; persist the new line for resume.
                    view.on_message(message)
                    view.save()
                return message.i

    def set_approval(self, value):
        # ToolsConfig is shared by the daemon and every resident session's config.
        self.config.tools.approval = value
        if self.manager is not None:
            self.manager.config.tools.approval = value

    def ember_transition(self, slug, arguments):
        runtime = self._manager()._runtimes.get(slug)
        companions = getattr(runtime.session, 'companions', None) if runtime else None
        member = companions.member('ember') if companions else None
        if member is None:
            raise PermissionError('Ember is not active for this session')
        return member.transition(arguments)

    def _human(self, slug, turn_id, decision):
        return self.peer.approve('decision/requestApproval', {
            'threadId': slug, 'turnId': turn_id, 'decision': decision})

    def _thread(self, slug):
        manager = self._manager()
        manager.path(slug)
        for row in manager.list_sessions():
            if row['slug'] == slug:
                return self._thread_row(row)
        raise KeyError(slug)

    def _thread_row(self, row):
        """One thread row on the wire, with `id` meaning the thread handle.

        The daemon's protocol says a thread is addressed by `id`, and callers pass that
        id straight back as `threadId`. Sessions now carry a serialized identity too, and
        spreading the row after the explicit id silently replaced the slug with the uuid,
        which broke every `threadId` a caller had been handed. The protocol's `id` stays
        the slug; the session's serialized handle travels beside it as `session_id`.
        """
        session_id = row.get('id')
        thread = {'id': row['slug'], **{key: value for key, value in row.items() if key != 'id'}}
        if isinstance(session_id, str) and session_id:
            thread['session_id'] = session_id
        return thread

    @contextmanager
    def _surface(self, slug):
        self._thread(slug)
        manager = self._manager()
        with manager._lock:
            runtime = manager._runtimes.get(slug)
            if runtime is not None:
                yield runtime.session.log, runtime.session.view
                return
            root = manager.path(slug)
            with ChatLog(root / 'log', manager.messaging._secret_values()) as log:
                tree = SummaryTree(root, log, node_bytes=self.config.view.node_bytes)
                view = View(root, tree, high_bytes=self.config.view.high_bytes, low_bytes=self.config.view.low_bytes)
                manager.observe_log(slug, log)
                for record in read_records(root / 'events.jsonl'):
                    self._observe_event(slug, record)
                yield log, view

    def _history(self, slug, params):
        after = integer(params, 'after', -1, minimum=-1)
        limit = integer(params, 'limit', self.config.daemon.history_page_size,
                        minimum=1, maximum=self.config.daemon.history_page_size)
        with self._surface(slug) as (log, view):
            messages = []
            for row in log.iter(after + 1):
                messages.append(asdict(row))
                if len(messages) >= limit:
                    break
            return messages

    def _watch(self, runtime, after=None):
        with self.watch_lock:
            if runtime.slug in self.watches:
                if after is not None:
                    raise RpcError(-32602, 'Session is already attached; event cursor cannot be replaced')
                return
            checkpoint = len(runtime.events.after()) if after is None else after
            runtime.events.after(checkpoint)
            worker = threading.Thread(target=self._events, args=(runtime, checkpoint),
                                      name='tessera-events-' + runtime.slug, daemon=True)
            self.watches[runtime.slug] = worker
            worker.start()

    def _events(self, runtime, checkpoint):
        while True:
            timeout = 0 if self.stopping.is_set() else self.config.daemon.event_poll_seconds
            records = runtime.events.after(checkpoint, timeout=timeout)
            for record in records:
                self._event(runtime.slug, record)
                checkpoint = record['id']
            if not records and (self.stopping.is_set() or runtime.events.closed):
                return

    def _event(self, slug, record):
        data = record['data']
        params = {'threadId': slug, 'eventId': record['id'], 'turnId': data.get('turn_id')}
        event = record['event']
        if event == 'turn_start':
            params['turn'] = {'id': data['turn_id'], 'status': 'inProgress'}
            self.peer.notify('turn/started', params)
        elif event == 'turn_end':
            params['turn'] = {'id': data['turn_id'], 'status': 'completed' if data['success'] else 'failed'}
            self.peer.notify('turn/completed', params)
        elif event == 'message':
            params['turnId'] = data['meta'].get('request_id') or data['meta'].get('turn_id')
            kinds = {'user': 'userMessage', 'agent': 'agentMessage', 'tool': 'tool', 'echo': 'toolResult'}
            item = {'id': str(data['i']), 'type': kinds.get(data['kind'], data['kind']),
                    'text': data['text'], 'kind': data['kind']}
            if data['kind'] in ('tool', 'echo'):
                item['output'] = data['text']
                item['name'] = data['meta'].get('name', 'tool')
            params['item'] = item
            self.peer.notify('item/started' if data['kind'] == 'tool' else 'item/completed', params)
        else:
            self.peer.notify('thread/event', {**params, 'event': event, 'data': data})

    def dispatch(self, method, params):
        if method not in METHODS:
            raise RpcError(-32601, 'Method not found')
        if method == 'shutdown':
            fields(params)
            self.close()
            return {'stopped': True}
        if self.stopping.is_set():
            raise RpcError(-32001, 'Daemon is shutting down')
        manager = self._manager()
        if method.startswith('agent/') or method.startswith('channel/'):
            return self._agents(method, params)
        if method == 'status':
            fields(params)
            return {**manager.status(), 'mirror': self.mirror.status(),
                    'companions': self.companion_status(), 'inspector': self.inspector.status()}
        if method == 'thread/start':
            fields(params, ('title', 'backend'), ('title',))
            row = manager.create(**params)
            return {'thread': self._thread_row(row)}
        if method == 'thread/list':
            fields(params)
            return {'data': [self._thread_row(row) for row in manager.list_sessions()]}
        if method == 'thread/resume':
            fields(params, ('threadId', 'afterEventId'), ('threadId',))
            slug = params['threadId']
            row = self._thread(slug)
            checkpoint = integer(params, 'afterEventId', 0) if 'afterEventId' in params else None
            if not row['archived'] and (self.config.backends or self.backend_factory is not None):
                self._watch(manager.runtime(slug), checkpoint)
            return {'thread': row, 'messages': self._history(slug, {})}
        if method == 'thread/read':
            fields(params, ('threadId', 'after', 'limit'), ('threadId',))
            return {'thread': self._thread(params['threadId']), 'messages': self._history(params['threadId'], params)}
        if method == 'thread/update':
            fields(params, ('threadId', 'title', 'archived'), ('threadId',))
            row = manager.update(params['threadId'], title=params.get('title'), archived=params.get('archived'))
            return {'thread': self._thread_row(row)}
        if method == 'turn/interrupt':
            fields(params, ('threadId', 'turnId'), ('threadId',))
            result = manager.interrupt(params['threadId'])
            return {'interrupted': result['interrupted'], 'turnId': result['turn_id']}
        if method == 'turn/steer':
            fields(params, ('threadId', 'text'), ('threadId', 'text'))
            result = manager.steer(params['threadId'], params['text'])
            return {'mode': result['mode'], 'turnId': result['turn_id']}
        if method == 'turn/start':
            fields(params, ('threadId', 'input'), ('threadId', 'input'))
            text = []
            if not isinstance(params['input'], list) or not params['input']:
                raise RpcError(-32602, 'Nonempty text input required')
            for item in params['input']:
                if not isinstance(item, dict) or set(item) != {'type', 'text'} or item['type'] != 'text' or not isinstance(item['text'], str):
                    raise RpcError(-32602, 'Only text inputs are supported')
                text.append(item['text'])
            slug = params['threadId']
            row = self._thread(slug)
            manager._validate_text('\n'.join(text))
            if row['archived']:
                raise RpcError(-32003, 'Session is archived')
            if self.agents.is_child(slug):
                raise ValueError('Child turns require an agent/run claim')
            runtime = manager.runtime(slug)
            self._watch(runtime)
            turn_id = manager.submit(slug, '\n'.join(text))
            return {'turn': {'id': turn_id, 'status': 'queued'}}
        if method in ('view/read', 'view/zoom'):
            return self._view(method, params)
        if method == 'memory/sync':
            fields(params)
            return self.mirror.sync()
        if method == 'memory/recall':
            fields(params, ('threadId', 'query', 'level', 'index'), ('threadId',))
            slug = params['threadId']
            self._thread(slug)
            return self.mirror.recall(slug, manager._metadata[slug]['id'],
                                     query=params.get('query'), level=params.get('level'), index=params.get('index'))
        if method == 'board/read':
            fields(params)
            return manager.board_status()
        if method.startswith('swarm/'):
            if method == 'swarm/create':
                fields(params, ('threadId', 'objective', 'workers'), ('threadId', 'objective', 'workers'))
                job = manager.create_swarm_job(params['threadId'], params['objective'], params['workers'])
            else:
                fields(params, ('jobId',), ('jobId',))
                job = manager.run_swarm_job(params['jobId']) if method == 'swarm/run' else manager.swarm_jobs.get(params['jobId'])
            return {'job': job}
        if method.startswith('voice/'):
            allowed = {'voice/start': ('threadId',), 'voice/events': ('threadId', 'voiceId', 'events'),
                       'voice/stop': ('threadId', 'voiceId'),
                       'voice/local': ('threadId', 'audio', 'rate')}[method]
            fields(params, allowed, allowed)
            if method == 'voice/local':
                from .local_voice import LocalVoice, LocalVoiceError
                try:
                    return LocalVoice(manager).utterance(params['threadId'], params['audio'], params['rate'])
                except LocalVoiceError as error:
                    codes = {'request': -32602, 'encoding': -32602, 'size': -32602, 'rate': -32602}
                    raise RpcError(codes.get(error.category, -32001), str(error)) from None
            if method == 'voice/start':
                return manager.voice.start(params['threadId'])
            if method == 'voice/events':
                return manager.voice.events(params['threadId'], params['voiceId'], params['events'])
            return manager.voice.stop(params['threadId'], params['voiceId'])

    def _agents(self, method, params):
        manager = self._manager()
        if method == 'agent/plan':
            fields(params, ('threadId', 'objective', 'backlog', 'requestId'), ('threadId', 'objective', 'backlog'))
            return {'run': self.agents.planning.plan(params['threadId'], params['objective'], params['backlog'], params.get('requestId'))}
        if method == 'agent/merge':
            fields(params, ('runId',), ('runId',))
            return {'run': self.agents.merge(params['runId'])}
        if method == 'agent/spawn':
            fields(params, ('threadId', 'objective', 'workers', 'requestId'), ('threadId', 'objective', 'workers'))
            return {'run': self.agents.spawn(params['threadId'], params['objective'], params['workers'], params.get('requestId'))}
        if method in ('agent/run', 'agent/show'):
            fields(params, ('runId',), ('runId',))
            return {'run': self.agents.run(params['runId']) if method == 'agent/run' else self.agents.show(params['runId'])}
        if method == 'agent/list':
            fields(params, ('threadId',))
            if 'threadId' in params:
                self._thread(params['threadId'])
            return {'data': self.agents.list(params.get('threadId'))}
        if method == 'agent/send':
            fields(params, ('fromThreadId', 'toThreadId', 'text', 'parentMessageId', 'messageId'),
                   ('fromThreadId', 'toThreadId', 'text'))
            source, target = params['fromThreadId'], params['toThreadId']
            result = manager.direct_work(source, target, params['text'],
                parent_id=params.get('parentMessageId'), message_id=params.get('messageId'))
            return {'message': self.agents.message_events(result, source, [target])}
        if method == 'channel/list':
            fields(params)
            return {'data': manager.channels.list_channels()}
        if method == 'channel/create':
            fields(params, ('name',), ('name',))
            return {'channel': manager.create_channel(params['name'])}
        if method == 'channel/subscribe':
            fields(params, ('name', 'threadId', 'subscribed'), ('name', 'threadId'))
            return {'subscription': manager.subscribe_channel(params['name'], params['threadId'], params.get('subscribed', True))}
        if method == 'channel/send':
            fields(params, ('name', 'threadId', 'text', 'parentMessageId', 'messageId'), ('name', 'threadId', 'text'))
            result = manager.publish_channel(params['name'], params['threadId'], params['text'],
                parent_id=params.get('parentMessageId'), message_id=params.get('messageId'))
            return {'message': self.agents.message_events(result, params['threadId'], result['recipients'])}
        if method == 'channel/deliver':
            fields(params, ('limit', 'messageId'))
            return manager.deliver_work(limit=params.get('limit'), message_id=params.get('messageId'))
        fields(params, ('after', 'limit'))
        after = integer(params, 'after', 0)
        limit = integer(params, 'limit', self.config.daemon.history_page_size, minimum=1,
                        maximum=self.config.daemon.history_page_size)
        from .passive_status import read_snapshot
        enabled = self.config.channels.enabled
        rows = []
        if enabled:
            _, records, _, _ = read_snapshot(manager.channels.path, self.config.status)
            if records is None:
                raise ValueError('Channel journal exceeds passive read bounds')
            rows = [r for r in records if r['sequence'] > after][:limit]
        return {'enabled': enabled, 'data': rows, 'nextAfter': rows[-1]['sequence'] if rows else after}

    def _view(self, method, params):
        fields(params, ('threadId',) if method == 'view/read' else ('threadId', 'start', 'count', 'offset', 'limit'), ('threadId',))
        with self._surface(params['threadId']) as (log, view):
            if method == 'view/read':
                return {'lines': view.lines(), 'bytes': len(view.render().encode()),
                        'high': view.high_bytes, 'low': view.low_bytes}
            start = integer(params, 'start', 0)
            count = integer(params, 'count', 1, minimum=1)
            offset = integer(params, 'offset', 0)
            limit = integer(params, 'limit', self.config.daemon.zoom_page_bytes, minimum=1,
                            maximum=self.config.daemon.max_frame_bytes // 2)
            text = view.zoom(start, count, offset=offset, limit=limit)
            result = {'text': text}
            if count == 1:
                total = len(log.get(start).text.encode('utf-8'))
                next_offset = offset + len(text.encode('utf-8'))
                result.update(offset=offset, totalBytes=total,
                              nextOffset=next_offset if next_offset < total else None)
            return result

    def close(self):
        if self.stopping.is_set():
            return
        self.inspector.stop()
        # Leave the event pumps alive until accepted turns and their final events drain.
        if self.manager is not None:
            self.manager.close()
        self.stopping.set()
        for worker in self.watches.values():
            worker.join()


def _live_package():
    return str(Path(__file__).resolve().parent)
