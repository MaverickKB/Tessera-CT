"""Session adapters for explicit local work delivery. No polling or provider setup."""

import os
import threading

from .channels import ChannelError, ChannelStore
from .jev import JudgeKit
from .log import ChatLog
from .tree import SummaryTree
from .view import View
from .passive_status import configured_path, read_snapshot


def may_resume_work(config, item):
    return not item.get('work_id') or (config.channels.enabled and config.channels.auto_reply)


class SessionMessaging:
    def __init__(self, manager):
        self.manager = manager
        self.config = manager.config
        settings = self.config.channels
        self.store = ChannelStore(configured_path(self.config.home, settings.journal_file), enabled=settings.enabled,
                                  max_hops=settings.max_hops, rate_limit=settings.rate_limit,
                                  rate_window=settings.rate_window, rate_overrides=settings.rate_overrides,
                                  max_text_bytes=settings.max_text_bytes, block_cycles=settings.block_cycles)
        self._lock = threading.RLock()

    def _require_enabled(self):
        if not self.config.channels.enabled:
            raise ChannelError('Session channels are disabled')
        self.store._require_enabled()

    def wait_idle(self):
        with self._lock:
            pass

    def _validate_sessions(self, source, targets):
        self._require_enabled()
        with self.manager._lock:
            for slug in [source] + list(targets):
                self.manager.path(slug)
                if slug not in self.manager._metadata:
                    raise ChannelError('Work session does not exist')
                if self.manager._metadata[slug]['archived']:
                    raise ChannelError('Work session is archived')
            if not self.manager._accepting:
                runtime = self.manager._runtimes.get(source)
                if runtime is None or runtime.active is None:
                    raise ChannelError('Session manager is stopping')

    def _parent(self, source, supplied):
        runtime = self.manager._runtimes.get(source)
        active = runtime.active if runtime else None
        inherited = active.get('work_id') if active else None
        if inherited:
            if supplied is not None and supplied != inherited:
                raise ChannelError('Active work cannot replace its causal parent')
            return inherited
        return supplied

    def _secret_values(self):
        names = [self.config.jev.api_key_env, self.config.aethermind.token_env, 'TESSERA_SERVER_TOKEN']  # legacy variable stays redacted; it grants nothing
        for backend in self.config.backends.values():
            names.append(backend.api_key_env)
        for reference in self.config.credentials.values():
            names.append(reference.env)
        values = []
        for name in names:
            value = os.environ.get(name, '')
            if value:
                values.append(value)
        return values

    def _redact(self, text):
        if not isinstance(text, str):
            raise ChannelError('Work content must be text')
        for value in self._secret_values():
            text = text.replace(value, '[REDACTED]')
        return text

    def redact_value(self, value):
        """Redact a JSON-shaped value (results, notifications) without mutating it.

        Call this before persisting or returning structured data; _redact stays text-only for content."""
        secrets = self._secret_values()
        def clean(item):
            if isinstance(item, str):
                for secret in secrets:
                    item = item.replace(secret, '[REDACTED]')
                return item
            if item is None or isinstance(item, (bool, int, float)):
                return item
            if isinstance(item, dict):
                return {clean(k) if isinstance(k, str) else k: clean(v) for k, v in item.items()}
            if isinstance(item, (list, tuple)):
                return [clean(v) for v in item]
            raise ChannelError('Redacted values must be JSON-shaped')
        return clean(value)

    def create_channel(self, name):
        with self._lock:
            self._require_enabled()
            if not self.manager._accepting:
                raise ChannelError('Session manager is stopping')
            return self.store.create(name)

    def subscribe_channel(self, name, slug, subscribed=True):
        with self._lock:
            self._validate_sessions(slug, [])
            return self.store.subscribe(name, slug, subscribed)

    def _result(self, message):
        if self.config.channels.auto_deliver:
            self.deliver_work(message_id=message['id'], internal=True)
        result = {'message_id': message['id'], 'recipients': message['recipients'], 'hop': message['hop']}
        if message['target']:
            turn = self.store.delivery_result(message['id'], message['target'])
            if turn:
                result['turn_id'] = turn
                result['slug'] = message['target']
        return result

    def publish_channel(self, name, source, text, *, parent_id=None, message_id=None):
        with self._lock:
            self._validate_sessions(source, [])
            members = None
            for channel in self.store.list_channels():
                if channel['name'] == name:
                    members = channel['members']
            if members is None:
                raise ChannelError('Channel does not exist')
            self._validate_sessions(source, members)
            message = self.store.publish(name, source, self._redact(text),
                                         parent_id=self._parent(source, parent_id), message_id=message_id)
            return self._result(message)

    def direct_work(self, source, target, text, *, parent_id=None, message_id=None):
        with self._lock:
            self._validate_sessions(source, [target])
            message = self.store.direct(source, target, self._redact(text),
                                        parent_id=self._parent(source, parent_id), message_id=message_id)
            return self._result(message)

    def handoff(self, source, target, brief, *, view=None, task_ref='', parent_id=None, message_id=None):
        with self._lock:
            self._validate_sessions(source, [target])
            if view is None:
                runtime = self.manager._runtimes.get(source)
                if runtime is None:
                    raise ChannelError('Handoff needs a loaded sender view or an explicit snapshot')
                view = runtime.session.view.prompt_render(runtime.session.config.view.prompt_bytes)
            message = self.store.handoff(source, target, self._redact(brief), self._redact(view),
                                         task_ref=self._redact(task_ref), parent_id=self._parent(source, parent_id),
                                         message_id=message_id)
            return self._result(message)

    def _append_to_log(self, log, message, direction):
        for row in log.iter():
            if row.kind == 'work' and row.meta.get('work_id') == message['id'] and row.meta.get('direction') == direction:
                return
        if direction == 'incoming':
            text = '[' + message['sender'] + '] ' + message['text']
        else:
            target = message['channel'] or message['target']
            text = '[to ' + target + '] ' + message['text']
        log.append('work', text, {'work_id': message['id'], 'direction': direction, 'work_kind': message['kind'],
                                 'from_slug': message['sender'], 'hop': message['hop'], 'channel': message['channel'],
                                 'task_ref': message['task_ref']})

    def _log_work(self, slug, message, direction):
        with self.manager._lock:
            if slug not in self.manager._metadata:
                raise ChannelError('Work recipient session no longer exists')
            runtime = self.manager._runtimes.get(slug)
            if runtime is None:
                # Keep the normal log/view invariant without constructing a Session,
                # provider, compactor, memory integration or background worker.
                root = self.manager.path(slug)
                log = ChatLog(root / 'log', self._secret_values())
                try:
                    self.manager.observe_log(slug, log)
                    tree = SummaryTree(root, log, node_bytes=self.config.view.node_bytes)
                    view = View(root, tree, high_bytes=self.config.view.high_bytes, low_bytes=self.config.view.low_bytes)
                    log.subscribe(view.on_message)
                    self._append_to_log(log, message, direction)
                    self.manager._save_work_count(slug, log.count())
                finally:
                    log.close()
                return
        # A loaded log has subscribers that acquire the manager lock. Never hold
        # that lock across append, or two active sessions could deadlock.
        self._append_to_log(runtime.session.log, message, direction)

    def _previous_reply(self, slug, work_id):
        _, records, _, _ = read_snapshot(self.manager.path(slug) / 'turns.jsonl', self.config.status)
        if records is None:
            raise ChannelError('Cannot verify saved work reply within configured read bounds')
        previous = None
        state = None
        for record in records:
            if record.get('op') == 'enqueue':
                turn = record.get('turn')
                if not isinstance(turn, dict):
                    raise ChannelError('Invalid saved work reply record')
                if turn.get('work_id') == work_id:
                    if previous is not None:
                        raise ChannelError('Duplicate saved work reply identity')
                    previous, state = turn['id'], turn['state']
            elif record.get('op') == 'state' and record.get('id') == previous:
                state = record.get('state')
        return (previous, state) if previous is not None else None

    def _reply(self, slug, message):
        allowed = self.manager.work_reply_allowed
        if allowed is not None and not allowed(slug):
            return None
        previous = self._previous_reply(slug, message['id'])
        if previous is not None:
            turn_id, state = previous
            if state in ('queued', 'running'):
                self.manager.runtime(slug, internal=True)
            return turn_id
        runtime = self.manager._runtimes.get(slug)
        kit = runtime.session.router if runtime else JudgeKit.from_config(
            self.config, decision_log=self.manager.path(slug) / 'decisions.jsonl')
        gate = kit.message_gate(message['text'], hop=message['hop'], max_hops=self.config.channels.max_hops)
        if gate['needs_reply'] < self.config.channels.needs_reply_threshold or gate['looping'] >= self.config.jev.gate_threshold:
            return None
        runtime = self.manager.runtime(slug, internal=True)
        return runtime.enqueue(message['text'], sender=message['sender'], hop=message['hop'], work_id=message['id'])

    def deliver_work(self, *, limit=None, message_id=None, internal=False):
        with self._lock:
            self._require_enabled()
            if self.manager._closed or (not self.manager._accepting and not internal):
                raise ChannelError('Session manager is stopping')
            bound = self.config.channels.delivery_limit if limit is None else limit
            if type(bound) is not int or bound <= 0 or bound > self.config.channels.delivery_limit:
                raise ChannelError('Invalid work delivery bound')
            delivered = 0
            for item in self.store.pending(limit=bound, message_id=message_id):
                slug, message, direction = item['session'], item['message'], item['direction']
                self._log_work(slug, message, direction)
                turn = None
                if direction == 'incoming' and self.config.channels.auto_reply and not self.manager._metadata[slug]['archived']:
                    turn = self._reply(slug, message)
                self.store.acknowledge(message['id'], slug, direction, turn_id=turn)
                delivered += 1
            return {'delivered': delivered}
