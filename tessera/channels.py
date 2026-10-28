"""Opt-in local channels and a durable, explicitly drained work-message outbox."""

from contextlib import contextmanager
from copy import deepcopy
from . import locking as fcntl
from .durability import sync_directory
import json
import math
import os
from pathlib import Path
import re
import threading
import time
import uuid


class ChannelError(ValueError):
    pass


def valid_name(name):
    if not isinstance(name, str) or not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?', name):
        raise ChannelError('Invalid channel or session name')
    return name


class ChannelStore:
    def __init__(self, path, *, enabled=False, max_hops=4, rate_limit=20, rate_window=60,
                 rate_overrides=None, max_text_bytes=65536, block_cycles=True, clock=time.time):
        if (type(max_hops) is not int or max_hops <= 0 or type(rate_limit) is not int or rate_limit < 0
                or type(max_text_bytes) is not int or max_text_bytes <= 0
                or not math.isfinite(rate_window) or rate_window <= 0):
            raise ValueError('Invalid channel limits')
        self.path = Path(path)
        self.enabled = enabled
        self.max_hops = max_hops
        self.rate_limit = rate_limit
        self.rate_window = rate_window
        self.rate_overrides = dict(rate_overrides or {})
        for name, value in self.rate_overrides.items():
            valid_name(name)
            if type(value) is not int or value < 0:
                raise ValueError('Channel rate overrides must be nonnegative integers')
        self.max_text_bytes = max_text_bytes
        self.block_cycles = block_cycles
        self.clock = clock
        self._lock = threading.RLock()

    def _require_enabled(self):
        if not self.enabled:
            raise ChannelError('Session channels are disabled')

    def _sync_directory(self):
        sync_directory(self.path.parent)

    @contextmanager
    def _locked(self):
        self._require_enabled()
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.with_name(self.path.name + '.lock').open('a+b') as stream:
                fcntl.flock(stream, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(stream, fcntl.LOCK_UN)

    def _read(self, repair=False):
        channels, messages, acknowledgments = {}, {}, {}
        sequence = 0
        if not self.path.exists():
            return channels, messages, acknowledgments, sequence
        with self.path.open('r+b' if repair else 'rb') as stream:
            while True:
                offset = stream.tell()
                line = stream.readline()
                if not line:
                    break
                if not line.endswith(b'\n'):
                    if repair:
                        backup = self.path.with_name(self.path.name + '.incomplete-' + uuid.uuid4().hex)
                        with backup.open('xb') as evidence:
                            evidence.write(line)
                            evidence.flush()
                            os.fsync(evidence.fileno())
                        stream.truncate(offset)
                        stream.flush()
                        os.fsync(stream.fileno())
                        self._sync_directory()
                    break
                try:
                    record = json.loads(line)
                    sequence += 1
                    if record['sequence'] != sequence:
                        raise ValueError()
                    op = record['op']
                    if op == 'channel':
                        channels[record['name']] = []
                    elif op == 'subscription':
                        members = channels[record['name']]
                        member = record['session']
                        if record['subscribed'] and member not in members:
                            members.append(member)
                        elif not record['subscribed'] and member in members:
                            members.remove(member)
                    elif op == 'message':
                        message = record['message']
                        if message['id'] in messages:
                            raise ValueError()
                        messages[message['id']] = message
                    elif op == 'ack':
                        key = (record['message_id'], record['session'], record['direction'])
                        acknowledgments[key] = record.get('turn_id')
                    else:
                        raise ValueError()
                except (ValueError, KeyError, TypeError):
                    raise ChannelError('Invalid channel journal record') from None
        return channels, messages, acknowledgments, sequence

    def _append(self, record, sequence):
        existed = self.path.exists()
        record = {**record, 'sequence': sequence + 1}
        with self.path.open('ab') as stream:
            stream.write((json.dumps(record, ensure_ascii=False, allow_nan=False) + '\n').encode())
            stream.flush()
            os.fsync(stream.fileno())
        if not existed:
            self._sync_directory()

    def create(self, name):
        valid_name(name)
        with self._locked():
            channels, _, _, sequence = self._read(repair=True)
            if name not in channels:
                self._append({'op': 'channel', 'name': name}, sequence)
            return {'name': name, 'members': list(channels.get(name, []))}

    def subscribe(self, name, session, subscribed=True):
        valid_name(name)
        valid_name(session)
        if type(subscribed) is not bool:
            raise ChannelError('Subscription flag must be boolean')
        with self._locked():
            channels, _, _, sequence = self._read(repair=True)
            if name not in channels:
                raise ChannelError('Channel does not exist')
            self._append({'op': 'subscription', 'name': name, 'session': session, 'subscribed': subscribed}, sequence)
        return {'name': name, 'session': session, 'subscribed': subscribed}

    def list_channels(self):
        if not self.enabled:
            return []
        channels, _, _, _ = self._read()
        rows = []
        for name, members in channels.items():
            rows.append({'name': name, 'members': list(members)})
        return rows

    def publish(self, channel, sender, text, *, parent_id=None, message_id=None):
        valid_name(channel)
        return self._send('channel', sender, text, channel=channel, parent_id=parent_id, message_id=message_id)

    def direct(self, sender, target, text, *, parent_id=None, message_id=None):
        valid_name(target)
        return self._send('direct', sender, text, target=target, parent_id=parent_id, message_id=message_id)

    def handoff(self, sender, target, brief, view, *, task_ref='', parent_id=None, message_id=None):
        valid_name(target)
        if not isinstance(view, str) or not view.strip() or not isinstance(task_ref, str):
            raise ChannelError('Handoff requires a sender view snapshot and text task reference')
        text = 'Work handoff\n\nBrief:\n' + brief + '\n\nSender view:\n' + view if isinstance(brief, str) else ''
        return self._send('handoff', sender, text, target=target, brief=brief, view=view,
                          task_ref=task_ref, parent_id=parent_id, message_id=message_id)

    def _send(self, kind, sender, text, *, target=None, channel=None, parent_id=None, message_id=None,
              brief='', view='', task_ref=''):
        self._require_enabled()
        valid_name(sender)
        if not isinstance(text, str) or not text.strip() or len(text.encode()) > self.max_text_bytes:
            raise ChannelError('Work message is empty or exceeds configured byte limit')
        if parent_id is not None and (not isinstance(parent_id, str) or not parent_id or len(parent_id) > 256):
            raise ChannelError('Invalid work parent id')
        message_id = uuid.uuid4().hex if message_id is None else message_id
        if not isinstance(message_id, str) or not message_id or len(message_id) > 256:
            raise ChannelError('Invalid work message id')
        request = {'kind': kind, 'sender': sender, 'target': target, 'channel': channel, 'text': text,
                   'brief': brief, 'view': view, 'task_ref': task_ref, 'parent_id': parent_id}
        with self._locked():
            channels, messages, _, sequence = self._read(repair=True)
            if message_id in messages:
                saved = messages[message_id]
                for key, value in request.items():
                    if saved[key] != value:
                        raise ChannelError('Work message id was used for a different request')
                return deepcopy(saved)
            if channel is not None:
                if channel not in channels or sender not in channels[channel]:
                    raise ChannelError('Channel sender must be a subscribed member')
                recipients = [member for member in channels[channel] if member != sender]
                rate_key = 'channel:' + channel
            else:
                if sender == target:
                    raise ChannelError('Work message cannot target its sender')
                recipients = [target]
                rate_key = 'direct:' + ':'.join(sorted([sender, target]))
            hop = 1
            trail = []
            if parent_id is not None:
                parent = messages.get(parent_id)
                if parent is None or sender not in parent['recipients']:
                    raise ChannelError('Reply sender must be a parent message recipient')
                hop = parent['hop'] + 1
                trail = list(parent['trail'])
            if hop > self.max_hops:
                raise ChannelError('Work message hop limit reached')
            if self.block_cycles and sender in trail:
                raise ChannelError('Work message sender cycle blocked')
            stamp = self.clock()
            count = 0
            for message in messages.values():
                if message['rate_key'] == rate_key and message['created_at'] > stamp - self.rate_window:
                    count += 1
            limit = self.rate_overrides.get(channel, self.rate_limit)
            if count >= limit:
                raise ChannelError('Work message channel rate limit reached')
            message = {**request, 'id': message_id, 'recipients': recipients, 'hop': hop,
                       'trail': trail + [sender], 'created_at': stamp, 'rate_key': rate_key}
            self._append({'op': 'message', 'message': message}, sequence)
            return deepcopy(message)

    def pending(self, limit=None, message_id=None):
        if not self.enabled:
            return []
        _, messages, acknowledgments, _ = self._read()
        result = []
        for message in messages.values():
            if message_id is not None and message['id'] != message_id:
                continue
            destinations = [(message['sender'], 'outgoing')]
            for recipient in message['recipients']:
                destinations.append((recipient, 'incoming'))
            for session, direction in destinations:
                if (message['id'], session, direction) not in acknowledgments:
                    result.append({'session': session, 'direction': direction, 'message': deepcopy(message)})
                    if limit is not None and len(result) >= limit:
                        return result
        return result

    def acknowledge(self, message_id, session, direction, *, turn_id=None):
        with self._locked():
            _, messages, acknowledged, sequence = self._read(repair=True)
            message = messages.get(message_id)
            if message is None or direction not in ('incoming', 'outgoing'):
                raise ChannelError('Unknown work delivery')
            expected = [message['sender']] if direction == 'outgoing' else message['recipients']
            if session not in expected:
                raise ChannelError('Session is not a work delivery recipient')
            key = (message_id, session, direction)
            if key not in acknowledged:
                self._append({'op': 'ack', 'message_id': message_id, 'session': session,
                              'direction': direction, 'turn_id': turn_id}, sequence)
            return acknowledged.get(key, turn_id)

    def delivery_result(self, message_id, session):
        if not self.enabled:
            return None
        _, _, acknowledgments, _ = self._read()
        return acknowledgments.get((message_id, session, 'incoming'))
