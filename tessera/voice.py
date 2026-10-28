"""Explicit browser voice leases, ephemeral minting and recorded event bridging."""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
import math
import os
import re
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import uuid

from .credentials import CredentialError, CredentialResolver, redact_secret
from .grafana import http_url, safe_path
from .log import ChatLog
from .prompts import SYSTEM_PROMPT_TEMPLATE
from .tree import SummaryTree
from .view import View

TOKEN = r"[A-Za-z0-9!#$%&'*+\-.^_\x60|~]+"
# The provider is the voice agent, not a transcriber. It hears the speaker,
# calls Tessera's tools through this server, and speaks the answer itself, which
# is what makes voice feel immediate. Its output transcript is
# therefore the agent's real spoken reply, not an echo. A parrot reply comes from
# stripping tools and instructions and telling the model "do not answer": the
# provider then has nothing to do but repeat the speaker. The broker keeps the reply.
TRANSCRIPTS = {'conversation.item.input_audio_transcription.completed': 'user',
               'response.output_audio_transcript.done': 'agent'}
STOP_WORDS = {'stop', 'cancel', 'quiet'}
# Transcription events are not emitted unless input transcription is configured.
# xAI docs: `conversation.item.input_audio_transcription.updated` is "only emitted
# when audio.input.transcription.model is set to grok-transcribe". With no
# transcription config the provider sends speech_started/speech_stopped/
# response.created and never a transcript, so the lease carries no spoken text into
# the session and the app is just the provider talking to itself. This is the key
# that makes the bridge real.
TRANSCRIBE_MODEL = 'grok-transcribe'


SESSION_TASK = {
    'type': 'function', 'name': 'session_task',
    'description': 'Dispatch work to this same session with its full tools, permissions and history, without blocking voice; or read its task status.',
    'parameters': {'type': 'object', 'properties': {
        'action': {'type': 'string', 'enum': ['submit', 'status']},
        'text': {'type': 'string', 'description': 'Complete task and relevant spoken context for the session.'},
        'turn_id': {'type': 'string'}}, 'required': ['action'], 'additionalProperties': False}}
VOICE_CONVERSATION_STYLE = """
Speak in one or two concise sentences by default. Acknowledge a tool request
before the lookup, then give its useful result briefly. Offer details rather
than reading long reports aloud.
"""
HANDOFF_INSTRUCTIONS = """
You are the spoken side of this selected session, sharing its identity and conversation.
For work requiring tools, use session_task submit with the user's request and relevant
conversation context. The selected session performs it with its full tools and permissions.
Acknowledge accepted work briefly and keep talking naturally while it runs. Do not claim
it is complete from an acceptance receipt. Completed task status arrives automatically;
explain the actual result naturally once, without starting the task again. Use status when
asked about progress. Do not submit ordinary conversation as a second task.
"""


def _words(text):
    return re.findall(r"[\w']+", text.lower())


class VoiceError(ValueError):
    def __init__(self, category):
        self.category = category
        super().__init__('Voice operation unavailable (' + category + ')')


def configured(settings):
    return bool(settings.websocket_url and settings.model and settings.api_base_url and settings.client_secret_path
                and settings.client_protocol_prefix and (settings.api_key_env or settings.credential_ref))


def validate_voice(settings, credentials):
    if settings.websocket_url:
        try:
            parsed = urlsplit(settings.websocket_url)
            if (parsed.scheme not in ('ws', 'wss') or not parsed.hostname or parsed.username is not None
                    or parsed.fragment or any(c.isspace() for c in settings.websocket_url)):
                raise ValueError()
            parsed.port
            for key, _ in parse_qsl(parsed.query):
                if key.lower() in ('token', 'api_key', 'key', 'authorization', 'client_secret', 'access_token'):
                    raise ValueError()
        except (ValueError, TypeError):
            raise ValueError('Voice WebSocket must be a credential-free WS(S) endpoint') from None
    if settings.websocket_url:
        websocket_origin(settings)
    if not isinstance(settings.audio_format, str) or not settings.audio_format:
        raise ValueError('Voice audio format must be configured text')
    if settings.api_base_url:
        http_url(settings.api_base_url)
    if settings.client_secret_path:
        if settings.client_secret_path.startswith('//'):
            raise ValueError('Voice mint path must stay under the configured API base')
        safe_path(settings.client_secret_path, absolute=True)
    for name in ('max_session_seconds', 'secret_ttl_seconds', 'max_response_bytes', 'max_event_bytes',
                 'max_text_bytes', 'max_events', 'max_sessions', 'audio_sample_rate'):
        if type(getattr(settings, name)) is not int or getattr(settings, name) <= 0:
            raise ValueError('Voice durations and bounds must be positive integers')
    if settings.timeout <= 0 or settings.clock_skew_seconds < 0:
        raise ValueError('Invalid voice timeout or clock skew')
    if settings.credential_ref and settings.credential_ref not in credentials:
        raise ValueError('Voice credential_ref is not configured')
    if settings.client_protocol_prefix and not re.fullmatch(TOKEN, settings.client_protocol_prefix):
        raise ValueError('Invalid voice subprotocol prefix')
    if not settings.model_query_param or not re.fullmatch(r'[A-Za-z0-9_-]+', settings.model_query_param):
        raise ValueError('Invalid voice model query selector')
    if settings.tools_mode not in ('configured', 'session', 'handoff'):
        raise ValueError('Voice tools_mode must be configured, session or handoff')
    # The dedicated voice session's own identity. A slug is a directory name, so
    # it is validated here rather than discovered as a bad path at boot.
    if not isinstance(settings.dedicated_slug, str) or not settings.dedicated_slug:
        raise ValueError('Voice session slug must be configured text')
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', settings.dedicated_slug):
        raise ValueError('Voice session slug must be lowercase letters, digits and hyphens')
    if not isinstance(settings.dedicated_title, str) or not settings.dedicated_title.strip():
        raise ValueError('Voice session name must be configured text')
    if settings.dedicated_reasoning not in ('off', 'low', 'medium', 'high'):
        raise ValueError('Voice reasoning must be off, low, medium or high')
    if type(settings.dedicated_thinking_budget) is not int or settings.dedicated_thinking_budget < 0:
        raise ValueError('Voice thinking budget must be a non-negative integer')
    if not isinstance(settings.tools, list) or any(not isinstance(name, str) or not name for name in settings.tools):
        raise ValueError('Voice tools must be an explicit list of names')
    if len(set(settings.tools)) != len(settings.tools):
        raise ValueError('Voice tool names must be unique')
    if not isinstance(settings.turn_detection, dict):
        raise ValueError('Voice turn detection must be a settings table')
    if not isinstance(settings.session_options, dict) or set(settings.session_options) & {'instructions', 'tools', 'model'}:
        raise ValueError('Voice context and tool schemas are server-owned')
    audio = settings.session_options.get('audio', {})
    if not isinstance(audio, dict):
        raise ValueError('Voice audio options must be a table')
    for direction in ('input', 'output'):
        stream = audio.get(direction, {})
        if not isinstance(stream, dict):
            raise ValueError('Voice audio stream options must be tables')
        if 'format' in stream:
            format = stream['format']
            if (not isinstance(format, dict) or format.get('type') != settings.audio_format
                    or type(format.get('rate')) is not int or format['rate'] != settings.audio_sample_rate):
                raise ValueError('Voice stream format must match the configured browser codec and rate')
    try:
        raw = json.dumps([settings.session_options, settings.turn_detection], allow_nan=False).encode()
    except (ValueError, TypeError):
        raise ValueError('Voice session options must be finite JSON') from None
    if len(raw) > settings.max_event_bytes:
        raise ValueError('Voice session options exceed configured bound')


def websocket_origin(settings):
    parsed = urlsplit(settings.websocket_url)
    host = (parsed.hostname or '').encode('idna').decode('ascii')
    if not re.fullmatch(r'[A-Za-z0-9._:-]+', host):
        raise ValueError('Invalid voice WebSocket host')
    if ':' in host:
        host = '[' + host + ']'
    if parsed.port is not None:
        host += ':' + str(parsed.port)
    return parsed.scheme + '://' + host


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return None


class VoiceClient:
    def __init__(self, settings, *, credentials=None, opener=None, clock=time.time):
        self.settings = settings
        self.credentials = credentials if credentials is not None else CredentialResolver()
        self.opener = opener
        self.clock = clock
        self._secret = ''

    def mint(self):
        settings = self.settings
        if not settings.enabled:
            raise VoiceError('disabled')
        if not configured(settings):
            raise VoiceError('configuration')
        try:
            self._secret = self.credentials.resolve(settings.credential_ref, env=settings.api_key_env).value
            if not self._secret:
                raise CredentialError('Missing voice credential')
            request = urllib.request.Request(http_url(settings.api_base_url) + settings.client_secret_path,
                data=json.dumps({'expires_after': {'seconds': settings.secret_ttl_seconds}}).encode(),
                headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + self._secret}, method='POST')
            send = self.opener or urllib.request.build_opener(_NoRedirect()).open
            with send(request, timeout=settings.timeout) as response:
                raw = response.read(settings.max_response_bytes + 1)
            if len(raw) > settings.max_response_bytes:
                raise VoiceError('invalid_response')
            data = json.loads(raw)
            token = data.get('value')
            expires = data.get('expires_at')
            now = self.clock()
            if (not isinstance(token, str) or not re.fullmatch(TOKEN, token) or self._secret in token
                    or type(expires) not in (int, float) or not math.isfinite(expires)
                    or not now < expires <= now + settings.secret_ttl_seconds + settings.clock_skew_seconds):
                raise VoiceError('invalid_response')
            return {'value': token, 'expires_at': expires}
        except VoiceError:
            raise
        except CredentialError:
            raise VoiceError('credential') from None
        except urllib.error.HTTPError as error:
            error.close()
            raise VoiceError('upstream') from None
        except TimeoutError:
            raise VoiceError('timeout') from None
        except urllib.error.URLError as error:
            raise VoiceError('timeout' if isinstance(error.reason, TimeoutError) else 'upstream') from None
        except (ValueError, TypeError, AttributeError, OverflowError):
            raise VoiceError('invalid_response') from None
        except Exception:
            raise VoiceError('upstream') from None


class VoiceSessions:
    def __init__(self, manager, *, client=None, clock=time.time):
        self.manager = manager
        self.config = manager.config
        self.settings = self.config.voice
        self.clock = clock
        self.client = client or VoiceClient(self.settings, credentials=CredentialResolver(self.config.credentials))
        self._leases = {}
        self._lock = threading.RLock()
        self._start_lock = threading.RLock()

    def _fingerprint(self):
        return hashlib.sha256(json.dumps(asdict(self.settings), sort_keys=True).encode()).hexdigest()

    def status(self):
        enabled = self.settings.enabled
        count = 0
        with self._lock:
            leases = list(self._leases.values())
        if enabled and leases:
            now = self.clock()
            for lease in leases:
                if (not lease['stopped'] and now < lease['deadline'] and not self._idle(lease, now)
                        and lease['fingerprint'] == self._fingerprint()):
                    count += 1
        options = self.settings.session_options if isinstance(self.settings.session_options, dict) else {}
        voices = [str(v) for v in self.settings.voices if isinstance(v, str)]
        return {'enabled': enabled, 'configured': configured(self.settings), 'active_count': count,
                'voices': voices, 'voice': str(options.get('voice', ''))}

    def _idle(self, lease, now):
        """True only for a lease that declared keepalive and then went silent.

        A server-side "no events for N seconds" reap would kill every healthy
        quiet call: the provider socket is opened client-side, so this server
        never observes speech and a silent client is indistinguishable from a
        dead one. Client keepalive is therefore the only honest liveness signal,
        and it is opt-in per lease. The iOS app does not beat, so it is never
        reaped here and keeps the deadline as its backstop.
        """
        if not lease.get('keepalive'):
            return False
        if self.settings.idle_reap_seconds <= 0:
            # A non-positive window disables reaping rather than reaping instantly.
            return False
        last = lease.get('last_event')
        if not isinstance(last, (int, float)):
            return False
        return now - last >= self.settings.idle_reap_seconds

    def _require(self, slug):
        if not self.settings.enabled:
            raise VoiceError('disabled')
        if not configured(self.settings):
            raise VoiceError('configuration')
        with self.manager._lock:
            self.manager.path(slug)
            meta = self.manager._metadata.get(slug)
            if meta is None or meta['archived'] or not self.manager._accepting:
                raise VoiceError('session')

    def _secrets(self):
        values = self.manager.messaging._secret_values()
        selected = os.environ.get(self.settings.api_key_env, '') if self.settings.api_key_env else ''
        if selected:
            values.append(selected)
        return values

    @staticmethod
    def _scrub(value, secrets):
        for secret in secrets:
            value = redact_secret(value, secret)
        return value

    @contextmanager
    def _surface(self, slug, secrets):
        with self.manager._lock:
            runtime = self.manager._runtimes.get(slug)
            if runtime is None:
                root = self.manager.path(slug)
                log = ChatLog(root / 'log', secrets)
                try:
                    self.manager.observe_log(slug, log)
                    tree = SummaryTree(root, log, node_bytes=self.config.view.node_bytes)
                    view = View(root, tree, high_bytes=self.config.view.high_bytes, low_bytes=self.config.view.low_bytes)
                    log.subscribe(view.on_message)
                    yield log, view
                    self.manager._save_work_count(slug, log.count())
                finally:
                    log.close()
                return
        runtime.session.log.add_secrets(secrets)
        yield runtime.session.log, runtime.session.view

    def start(self, slug, voice=None, keepalive=False):
        if voice is not None and (not isinstance(voice, str) or voice not in self.settings.voices):
            raise ValueError('Voice is not in voice.voices')
        if not isinstance(keepalive, bool):
            raise ValueError('keepalive must be a boolean')
        self._require(slug)
        with self._start_lock:
            self._require(slug)
            fingerprint = self._fingerprint()
            now = self.clock()
            with self._lock:
                self._leases = {key: lease for key, lease in self._leases.items()
                                if not lease['stopped'] and now < lease['deadline']
                                and not self._idle(lease, now)}
                if len(self._leases) >= self.settings.max_sessions:
                    raise VoiceError('limit')
            if slug == self.settings.dedicated_slug:
                self.manager.ensure_voice()
            runtime = self.manager.runtime(slug) if self.settings.tools_mode in ('session', 'handoff') or self.settings.tools else None
            # The broker needs the real schemas: the provider calls these tools and
            # this server executes them on the live session, then the provider speaks
            # the result. An empty list here is what turned voice into a parrot.
            tools = []
            if runtime is not None:
                registry = runtime.session.tools
                if self.settings.tools_mode == 'configured':
                    registry = registry.restricted(self.settings.tools)
                if self.settings.tools_mode != 'handoff':
                    for schema in registry.schemas():
                        tools.append({'type': 'function', **schema['function']})
                if self.settings.tools_mode == 'handoff':
                    tools.append(SESSION_TASK)
            with self._surface(slug, self._secrets()) as (log, view):
                if runtime is None:
                    instructions = SYSTEM_PROMPT_TEMPLATE.format(agent_name=self.config.agent_name,
                        operator=self.config.operator_label(),
                        user_instructions=self.config.user_instructions)
                else:
                    session = runtime.session
                    parts = [session.system_prompt, runtime.inheritance]
                    if session.inherited_view:
                        parts.append(session.inherited_view)
                    parts.extend(session._session_context)
                    instructions = '\n'.join(parts)
                instructions += '\n' + view.render() + VOICE_CONVERSATION_STYLE
                if self.settings.tools_mode == 'handoff':
                    instructions += HANDOFF_INSTRUCTIONS
            minted = self.client.mint()
            self._require(slug)
            if self._fingerprint() != fingerprint:
                raise VoiceError('configuration')
            secrets = [self.client._secret, minted['value'], *self._secrets()]
            voice_id = uuid.uuid4().hex
            now = self.clock()
            deadline = now + self.settings.max_session_seconds
            lease = {'slug': slug, 'deadline': deadline, 'stopped': False, 'secrets': secrets,
                     'fingerprint': self._fingerprint(), 'tools': {tool['name'] for tool in tools}, 'runtime': runtime, 'goal': '', 'utterance': None, 'utterances': {}, 'stop_generation': None, 'tasks': {},
                     'calls': {}, 'responses': {}, 'pending': {}, 'events': 0, 'lock': threading.RLock(),
                     'keepalive': keepalive, 'last_event': now}
            if runtime is not None:
                runtime.session.log.add_secrets(secrets)
            with self._lock:
                self._leases[voice_id] = lease
            parsed = urlsplit(self.settings.websocket_url)
            query = dict(parse_qsl(parsed.query))
            query[self.settings.model_query_param] = self.settings.model
            endpoint = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ''))
            session = deepcopy(self.settings.session_options)
            session.setdefault('turn_detection', deepcopy(self.settings.turn_detection) or None)
            audio = session.setdefault('audio', {})
            for direction in ('input', 'output'):
                audio.setdefault(direction, {}).setdefault('format',
                    {'type': self.settings.audio_format, 'rate': self.settings.audio_sample_rate})
            # The speaker's transcript is the only path from speech into the
            # session, and the provider emits it only when transcription is
            # configured. Without this the lease is silent: no transcript event,
            # no session turn, and the provider answers on its own instead.
            audio['input'].setdefault('transcription', {'model': TRANSCRIBE_MODEL})
            if voice is not None:
                session['voice'] = voice
            session.update(instructions=instructions, tools=tools)
            detection = session.get('turn_detection')
            if not isinstance(detection, dict):
                detection = {}
            detection = dict(detection)
            detection['type'] = 'server_vad'
            # turn_detection carries only `type` and `idle_timeout_ms`. An unknown key
            # is silently ignored, so it must not read as a guarantee.
            detection.pop('create_response', None)
            session['turn_detection'] = detection
            session = self._scrub(session, secrets)
            if self._scrub(endpoint, secrets) != endpoint:
                with self._lock:
                    self._leases.pop(voice_id, None)
                raise VoiceError('configuration')
            return {'voice_id': voice_id, 'websocket_url': endpoint,
                    'protocols': [self.settings.client_protocol_prefix + minted['value']],
                    'expires_at': minted['expires_at'], 'session_expires_at': deadline,
                    'max_session_seconds': self.settings.max_session_seconds,
                    'audio_sample_rate': self.settings.audio_sample_rate,
                    'audio_format': self.settings.audio_format,
                    'session_update': {'type': 'session.update', 'session': session}}

    def _lease(self, slug, voice_id):
        self._require(slug)
        with self._lock:
            lease = self._leases.get(voice_id) if isinstance(voice_id, str) else None
        if lease is None or lease['slug'] != slug or lease['stopped'] or self.clock() >= lease['deadline']:
            raise VoiceError('expired')
        if lease['fingerprint'] != self._fingerprint():
            raise VoiceError('configuration')
        return lease

    def _audit(self, lease, phase, **fields):
        """Metadata-only broker receipts, never audio, credentials or tool output."""
        runtime = lease['runtime']
        if runtime is not None:
            runtime.events.append('voice', {'phase': phase, **fields})

    def _key(self, voice_id, *parts):
        return hashlib.sha256(json.dumps([voice_id, *parts], sort_keys=True).encode()).hexdigest()

    def _validate_events(self, events, lease):
        if not isinstance(events, list) or len(events) + lease['events'] > self.settings.max_events:
            raise VoiceError('limit')
        for event in events:
            try:
                if not isinstance(event, dict) or len(json.dumps(event, allow_nan=False).encode()) > self.settings.max_event_bytes:
                    raise ValueError()
                kind = event.get('type')
                if not isinstance(kind, str):
                    raise ValueError()
                if kind == 'input_audio_buffer.committed':
                    if not isinstance(event.get('item_id'), str) or not event['item_id']:
                        raise ValueError()
                elif kind in TRANSCRIPTS:
                    if not isinstance(event.get('item_id'), str) or not event['item_id']:
                        raise ValueError()
                    if not isinstance(event.get('transcript'), str) or len(event['transcript'].encode()) > self.settings.max_text_bytes:
                        raise ValueError()
                    if type(event.get('content_index', 0)) is not int or event.get('content_index', 0) < 0:
                        raise ValueError()
                elif kind == 'response.function_call_arguments.done':
                    if event.get('name') not in lease['tools']:
                        raise VoiceError('permission')
                    for field in ('call_id', 'response_id'):
                        if not isinstance(event.get(field), str) or not event[field]:
                            raise ValueError()
                    arguments = json.loads(event['arguments']) if isinstance(event.get('arguments'), str) else event.get('arguments')
                    if not isinstance(arguments, dict):
                        raise ValueError()
                    json.dumps(arguments, allow_nan=False)
                elif kind == 'response.done':
                    if not isinstance(event.get('response'), dict) or not isinstance(event['response'].get('id'), str):
                        raise ValueError()
            except VoiceError:
                raise
            except (ValueError, TypeError, KeyError, RecursionError):
                raise VoiceError('event') from None

    def events(self, slug, voice_id, events):
        lease = self._lease(slug, voice_id)
        with lease['lock']:
            self._lease(slug, voice_id)
            # Every accepted batch is a liveness signal for a keepalive lease.
            lease['last_event'] = self.clock()
            for secret in self._secrets():
                if secret and secret not in lease['secrets']:
                    lease['secrets'].append(secret)
            if lease['runtime'] is not None:
                lease['runtime'].session.log.add_secrets(lease['secrets'])
            self._validate_events(events, lease)
            messages = []
            send = []
            response_ids = []
            session_turn = None
            for event in events:
                self._lease(slug, voice_id)
                kind = event['type']
                if kind == 'input_audio_buffer.committed':
                    self._begin_utterance(voice_id, lease, event['item_id'])
                elif kind in TRANSCRIPTS:
                    key = self._key(voice_id, TRANSCRIPTS[kind], event['item_id'], event.get('content_index', 0))
                    text = self._scrub(event['transcript'], lease['secrets'])
                    if not text.strip():
                        continue
                    with self._surface(slug, lease['secrets']) as (log, view):
                        rows = list(log.iter())
                        previous_version = None
                        already_recorded = False
                        for row in rows:
                            if row.meta.get('voice_event_key') != key:
                                continue
                            previous_version = row
                            if row.text == text:
                                already_recorded = True
                        if already_recorded:
                            continue
                        meta = {'voice_id': voice_id, 'voice_event_key': key, 'voice_transcript': True,
                                'provenance': 'client_reported'}
                        previous = next((row for row in reversed(rows) if row.meta.get('voice_id') == voice_id), None)
                        # A completed event may revise the same item repeatedly.
                        # Retain corrections, while identical/old replays stay inert.
                        if previous_version is not None:
                            meta['supersedes'] = previous_version.i
                        # Paused speech can also resume under a new item identity.
                        elif TRANSCRIPTS[kind] == 'user' and previous is not None and previous.kind == 'user' \
                                and _words(text)[:len(_words(previous.text))] == _words(previous.text) and _words(previous.text):
                            meta['supersedes'] = previous.i
                        message = log.append(TRANSCRIPTS[kind], text, meta)
                        messages.append(asdict(message))
                        self._audit(lease, 'transcript', role=TRANSCRIPTS[kind],
                                    characters=len(text), revised=previous_version is not None)
                    # The provider answers on its own here (it is the voice agent), so a
                    # spoken line must not also be steered into the session: that would
                    # answer twice. Only an explicit stop word interrupts the session.
                    if TRANSCRIPTS[kind] == 'user':
                        lease['goal'] = text
                        self._begin_utterance(voice_id, lease, event['item_id'])
                        session_turn = self._hand_spoken(slug, text)
                        if session_turn is not None:
                            lease['utterance'] = None
                elif kind == 'response.function_call_arguments.done':
                    send.append(self._tool(voice_id, lease, event))
                    if event['response_id'] not in response_ids:
                        response_ids.append(event['response_id'])
                elif kind == 'response.done':
                    response = event['response']
                    response_id = response['id']
                    self._audit(lease, 'response_done', response_id=response_id,
                                status=str(response.get('status', ''))[:24])
                    expected = set()
                    output = response.get('output', [])
                    if isinstance(output, list):
                        for item in output:
                            if isinstance(item, dict) and item.get('type') == 'function_call' and isinstance(item.get('call_id'), str):
                                expected.add(item['call_id'])
                    lease['pending'][response_id] = {'status': response.get('status'), 'expected': expected}
                    if response_id not in response_ids:
                        response_ids.append(response_id)
            for response_id in response_ids:
                continuation = self._continuation(voice_id, lease, response_id)
                if continuation is not None:
                    send.append(continuation)
                    self._audit(lease, 'continuation_returned', response_id=response_id)
            # Polls request completion only when the client is idle. Ordinary
            # transcript/tool batches never interrupt a live spoken response.
            if not events:
                send.extend(self._task_completions(voice_id, lease))
            poll_after_ms = None
            for task in lease['tasks'].values():
                if not task['delivered']:
                    poll_after_ms = 1000
            lease['events'] += len(events)
            return {'accepted': len(events), 'messages': messages, 'send': self._scrub(send, lease['secrets']),
                    'session_turn': session_turn, 'poll_after_ms': poll_after_ms}

    def _begin_utterance(self, voice_id, lease, item_id):
        # Audio commitment precedes asynchronous transcription and tool calls.
        # Replayed transcripts must retain the original Stop generation.
        if item_id in lease['utterances']:
            return
        runtime = lease['runtime']
        if runtime is not None:
            with runtime.condition:
                lease['stop_generation'] = runtime.operator_stop_generation
        lease['utterance'] = self._key(voice_id, 'user', item_id)
        lease['utterances'][item_id] = lease['utterance']

    def _hand_spoken(self, slug, text):
        """A spoken stop word interrupts the session. Everything else is answered by
        the provider itself, which is the voice agent; steering it into the session
        as well would answer the same utterance twice."""
        words = _words(text)
        spoken_stop = False
        if words and len(words) <= 3:
            for word in words:
                if word in STOP_WORDS:
                    spoken_stop = True
                    break
        if not spoken_stop:
            return None
        try:
            stopped = self.manager.interrupt(slug)
        except Exception:
            return None
        return {'mode': 'interrupt', 'turn_id': stopped.get('turn_id') if isinstance(stopped, dict) else None}

    def _continuation(self, voice_id, lease, response_id):
        pending = lease['pending'].get(response_id)
        if pending is None or pending['status'] != 'completed':
            return None
        completed = set()
        for key, call in lease['calls'].items():
            if call['response_id'] == response_id:
                completed.add(key)
        if not completed or not pending['expected'] <= completed:
            return None
        if response_id not in lease['responses']:
            lease['responses'][response_id] = {'type': 'response.create',
                'event_id': self._key(voice_id, response_id, 'continue')}
        return deepcopy(lease['responses'][response_id])

    def _tool(self, voice_id, lease, event):
        key = event['call_id']
        arguments = json.loads(event['arguments']) if isinstance(event['arguments'], str) else event['arguments']
        request = {'name': event['name'], 'arguments': self._scrub(arguments, lease['secrets'])}
        previous = lease['calls'].get(key)
        if previous:
            if previous['request'] != request or previous['response_id'] != event['response_id']:
                raise VoiceError('event')
            return deepcopy(previous['send'])
        item = {'type': 'conversation.item.create', 'event_id': self._key(voice_id, key, 'result'),
                'item': {'type': 'function_call_output', 'call_id': key,
                         'output': json.dumps({'error': 'Voice tool result unavailable; not retried'})}}
        if event['name'] == 'session_task':
            lease['calls'][key] = {'request': request, 'response_id': event['response_id'], 'send': item}
            try:
                output = self._session_task(lease, arguments)
            except (ValueError, RuntimeError, KeyError) as exc:
                output = {'error': str(exc)}
            item['item']['output'] = self._scrub(json.dumps(output), lease['secrets'])
            return deepcopy(item)
        runtime = lease['runtime']
        if not runtime.session._turn_lock.acquire(blocking=False):
            raise VoiceError('busy')
        previous_goal = getattr(runtime.session, '_action_goal', '')
        try:
            self._lease(lease['slug'], voice_id)
            if lease['goal']:
                runtime.session._action_goal = lease['goal']
            lease['calls'][key] = {'request': request, 'response_id': event['response_id'], 'send': item}
            try:
                tool_started = time.monotonic()
                self._audit(lease, 'tool_started', name=request['name'], response_id=event['response_id'])
                output, record, contexts = runtime.session._call_tool(
                    {'id': 'voice-' + self._key(voice_id, key), **request})
                if not record['success']:
                    output = json.dumps({'error': 'Voice tool was denied or failed'})
                item['item']['output'] = self._scrub(output, lease['secrets'])
                self._audit(lease, 'tool_returned', name=request['name'], success=bool(record['success']),
                            milliseconds=round((time.monotonic() - tool_started) * 1000),
                            characters=len(item['item']['output']), response_id=event['response_id'])
            except Exception:
                pass  # Reserved calls with an uncertain result are not retried.
        finally:
            runtime.session._action_goal = previous_goal
            runtime.session._turn_lock.release()
        return deepcopy(item)

    def _session_task(self, lease, arguments):
        action = arguments.get('action')
        if set(arguments) - {'action', 'text', 'turn_id'}:
            raise ValueError('Unknown session_task argument')
        if action == 'submit':
            text = arguments.get('text')
            if not isinstance(text, str) or not text.strip() or not lease['utterance']:
                raise ValueError('A current spoken request and task text are required')
            turn_id = str(uuid.uuid5(uuid.NAMESPACE_URL, lease['utterance'] + text))
            turn_id = self.manager.submit(lease['slug'], text, source='voice', client_request_id=turn_id,
                                         expected_stop_generation=lease['stop_generation'])
            if turn_id not in lease['tasks']:
                lease['tasks'][turn_id] = {'delivered': False}
            return {'turn_id': turn_id, 'state': self.manager.turn(lease['slug'], turn_id)['state'],
                    'message': 'Accepted by the selected session. Continue the conversation; completion will arrive.'}
        if action == 'status':
            turn_id = arguments.get('turn_id')
            if turn_id not in lease['tasks']:
                raise ValueError('Task does not belong to this voice call')
            return self._task_result(lease, turn_id)
        raise ValueError('session_task action must be submit or status')

    def _task_result(self, lease, turn_id):
        turn = self.manager.turn(lease['slug'], turn_id)
        result = {'turn_id': turn_id, 'state': turn['state'], 'error': turn.get('error')}
        replies = []
        for row in lease['runtime'].session.log.iter():
            if row.kind == 'agent' and row.meta.get('request_id') == turn_id:
                replies.append(row.text)
        result['replies'] = replies
        return result

    def _task_completions(self, voice_id, lease):
        frames = []
        for turn_id, task in lease['tasks'].items():
            if task['delivered']:
                continue
            result = self._task_result(lease, turn_id)
            if result['state'] not in ('done', 'error', 'cancelled'):
                continue
            call_id = 'task-' + self._key(voice_id, turn_id)
            # Seed a completed status call using the provider's documented history
            # format. Do not impersonate a new human utterance or replay work.
            frames.append({'type': 'conversation.item.create', 'event_id': call_id + '-call',
                           'item': {'type': 'function_call', 'call_id': call_id, 'name': 'session_task',
                                    'arguments': json.dumps({'action': 'status', 'turn_id': turn_id})}})
            frames.append({'type': 'conversation.item.create', 'event_id': call_id + '-output',
                           'item': {'type': 'function_call_output', 'call_id': call_id,
                                    'output': json.dumps(result)}})
            task['delivered'] = True
        if frames:
            frames.append({'type': 'response.create', 'event_id': self._key(voice_id, frames, 'completion')})
        return frames

    def stop(self, slug, voice_id):
        with self._lock:
            lease = self._leases.get(voice_id) if isinstance(voice_id, str) else None
        if lease is None or lease['slug'] != slug:
            raise VoiceError('expired')
        with lease['lock']:
            lease['stopped'] = True
            lease['secrets'] = []
        return {'stopped': True}

    def heartbeat(self, slug, voice_id):
        """A keepalive lease's cheap liveness beat.

        Deliberately separate from events(): an empty events() call also drains
        pending task completions, so reusing it as a beat would mark a spoken
        result delivered and drop the reply. This returns no send frames.
        """
        lease = self._lease(slug, voice_id)
        if not lease.get('keepalive'):
            # A lease that did not opt in has nothing to renew. Answer plainly
            # rather than silently pretending the beat landed.
            return {'keepalive': False, 'reaped': False}
        with lease['lock']:
            self._lease(slug, voice_id)
            lease['last_event'] = self.clock()
        return {'keepalive': True, 'reaped': False}

    def close(self):
        with self._lock:
            leases = list(self._leases.values())
        for lease in leases:
            with lease['lock']:
                lease['stopped'] = True
                lease['secrets'] = []
