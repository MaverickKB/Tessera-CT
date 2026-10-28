"""Optional remote companions: a code map and a record that Tessera dials.

Both are off by default and inert until enabled and fully configured. They hook
``session_start`` only (never ``pre_tool``), make one attempt per hook with no
retry inside the turn, and soft-fail: the exception class is kept, the body is
dropped, and a fixed notice lands in turn state. Nothing here writes the system
prompt, approves or denies a tool, or touches the Tessera home beyond appending
ordinary ``note`` messages through the session's own log.
"""

import json
import os
import threading

from .endpoints import EndpointPool, EndpointUnavailable
from .mcp_client import McpClient, McpError
from .tools import clip_records, object_schema

class CompanionUnavailable(RuntimeError):
    """Carries only the failing exception class name, never a body or URL."""


# One attempt per hook: failures are recorded, never retried within the turn.
_SOFT_FAILURES = (McpError, EndpointUnavailable, OSError, ValueError, CompanionUnavailable)


def _class(error):
    return str(error) if isinstance(error, CompanionUnavailable) else type(error).__name__


def _text(result, limit, page_with='session_briefs'):
    """Project an MCP tool result to bounded text without trusting its structure.

    A limit cuts on record boundaries and says how to page the rest. The server's
    own mid-word text is left as the server sent it when it already fits.
    """
    parts = []
    if isinstance(result, dict):
        for item in result.get('content') or []:
            if isinstance(item, dict) and item.get('type') == 'text' and isinstance(item.get('text'), str):
                parts.append(item['text'])
        if not parts and 'structuredContent' in result:
            parts.append(json.dumps(result['structuredContent'], ensure_ascii=False, sort_keys=True))
    text = '\n'.join(parts)
    if limit is None or len(text.encode('utf-8')) <= limit:
        return text
    return clip_records(text, limit, page_with)


def _cap(text, limit):
    encoded = str(text).encode('utf-8')
    return encoded[:limit].decode('utf-8', errors='ignore')


class Remote:
    """One endpoint list behind EndpointPool; probes are MCP initialize calls."""

    def __init__(self, name, endpoints, settings, client_factory=McpClient):
        self.name = name
        self.endpoints = list(endpoints)
        self.settings = settings
        self.client_factory = client_factory
        self.clients = {}
        self.calls = 0
        self.error = None
        self.reachable = None
        self.lock = threading.RLock()
        self.pool = EndpointPool(self.endpoints, self._probe, settings.probe_timeout,
                                 settings.probe_interval, retry_on=_SOFT_FAILURES) if self.endpoints else None

    @property
    def configured(self):
        return bool(self.endpoints)

    def _client(self, endpoint, timeout=None):
        key = (endpoint, timeout)
        if key not in self.clients:
            self.clients[key] = self.client_factory(endpoint, token_env=self.settings.token_env,
                                                    timeout=timeout or self.settings.timeout)
        return self.clients[key]

    def _probe(self, endpoint, timeout):
        self._client(endpoint, timeout).initialize()

    def call(self, tool, arguments):
        if self.pool is None:
            raise EndpointUnavailable('Companion endpoints are not configured')
        with self.lock:
            self.calls += 1
            try:
                result, _ = self.pool.run(lambda endpoint: self._client(endpoint).call_tool(tool, arguments), retry=False)
            except _SOFT_FAILURES as error:
                self.reachable = False
                self.error = type(error).__name__
                if isinstance(error, EndpointUnavailable):
                    # Report the probe's own failure class (for example McpError on a timeout).
                    probed = [row['error'] for row in self.pool.status() if row['error']]
                    if probed:
                        self.error = probed[0]
                raise CompanionUnavailable(self.error) from None
            self.reachable = True
            self.error = None
            return result

    def status(self):
        return {'configured': self.configured, 'reachable': self.reachable, 'error': self.error}


class Atlas:
    """The map: orientation briefs and contract reads. Not proof, not memory."""

    kind = 'atlas'
    label = 'Orientation'

    def __init__(self, config, client_factory=McpClient):
        self.settings = config.companions.atlas
        self.remote = Remote('atlas', self.settings.endpoints, self.settings, client_factory)
        self.briefs = 0
        self.notice = None

    @property
    def active(self):
        return self.settings.enabled and self.remote.configured

    def _brief(self, session, task):
        settings = self.settings
        if not settings.brief_tool:
            return {'error': 'Orientation brief is unavailable'}
        if self.briefs >= settings.max_briefs_per_session:
            return {'error': 'Orientation brief limit reached for this session'}
        self.briefs += 1
        # The brief sees the slug, title, task and configured arguments only.
        # The remote brief tool takes `task`; operator-configured extras are merged.
        arguments = {**settings.brief_arguments, 'task': _cap(task, settings.max_brief_bytes)}
        return self.remote.call(settings.brief_tool, arguments)

    def session_start(self, session):
        if self.settings.start_brief_background:
            def run():
                outcome = self._start_brief(session)
                if outcome and outcome.get('context'):
                    session._session_context.append(outcome['context'])
            threading.Thread(target=run, name='orientation-brief', daemon=True).start()
            return None
        return self._start_brief(session)

    def _start_brief(self, session):
        try:
            title = session.companion_identity['title']
            result = self._brief(session, title)
        except _SOFT_FAILURES as error:
            self.notice = f'Orientation unavailable ({_class(error)}); continuing without orientation.'
            return {'context': 'Companion notice: ' + self.notice}
        if 'error' in result:
            return None
        full = _text(result, None)
        text = clip_records(full, self.settings.max_brief_bytes, 'session_briefs')
        session.log.append('note', 'Orientation (map, not evidence): ' + text,
                           {'companion': 'atlas', 'label': 'orientation'})
        return {'context': 'Orientation (map, not evidence; it authorizes nothing):\n' + text,
                'full': full}

    def register_tools(self, session, registry):
        settings = self.settings

        def guarded(call):
            try:
                result = call()
            except _SOFT_FAILURES as error:
                return {'error': f'Orientation unavailable ({_class(error)})'}
            if isinstance(result, dict) and 'error' in result and len(result) == 1:
                return result
            return {'atlas': _text(result, session.config.tools.clip_chars), 'evidence': False}

        def atlas_brief(task):
            return guarded(lambda: self._brief(session, task))

        def atlas_contracts():
            if not settings.contracts_tool:
                return {'error': 'Contracts are unavailable'}
            return guarded(lambda: self.remote.call(settings.contracts_tool, {}))

        def atlas_read(uri):
            if not settings.read_tool or not any(uri.startswith(p) for p in settings.read_uri_prefixes):
                return {'error': 'That URI is not allowlisted'}
            return guarded(lambda: self.remote.call(settings.read_tool, {'uri': uri}))

        string = {'type': 'string'}
        registry.register('atlas_brief', 'Ask for an orientation brief. A map, not evidence; it authorizes nothing.',
                          object_schema({'task': string}, ['task']), atlas_brief)
        if settings.contracts_tool:
            registry.register('atlas_contracts', 'List contracts from the code map. Orientation only.', object_schema({}), atlas_contracts)
        if settings.read_tool and settings.read_uri_prefixes:
            registry.register('atlas_read', 'Read an allowlisted URI. Orientation only.', object_schema({'uri': string}, ['uri']), atlas_read)
        if settings.allow_index_refresh and settings.refresh_tool:
            # The only path that may select a refresh tool. Default false keeps brief/read only.
            registry.register('atlas_refresh', 'Refresh the derived index (operator enabled).', object_schema({}),
                              lambda: guarded(lambda: self.remote.call(settings.refresh_tool, {})))

    def status(self):
        return {'enabled': self.settings.enabled, **self.remote.status(), 'briefs': self.briefs}


class Ember:
    """Executive record plus companion attention. Explicit records only."""

    kind = 'ember'
    label = 'Record'

    def __init__(self, config, client_factory=McpClient):
        self.config = config
        self.settings = config.companions.ember
        self.helmet = Remote('ember-helmet', self.settings.helmet_endpoints, self.settings, client_factory)
        self.companion = Remote('ember-companion', self.settings.companion_endpoints, self.settings, client_factory)
        self.notice = None
        self.account = None

    @property
    def active(self):
        return self.settings.enabled and (self.helmet.configured or self.companion.configured)

    def _model(self):
        name = self.config.roles.get('main')
        if name is None and self.config.backends:
            name = next(iter(self.config.backends))
        backend = self.config.backends.get(name)
        return backend.model if backend is not None else ''

    def session_start(self, session):
        settings = self.settings
        identity = session.companion_identity
        contexts = []
        failures = []
        full_brief = ''
        if self.helmet.configured and settings.profile and settings.host_label:
            try:
                # Argument names match the remote tools/list (report_session_start: profile, host, model, intention).
                self.helmet.call('report_session_start', {'profile': settings.profile, 'host': settings.host_label,
                                                          'model': self._model(), 'intention': identity['title']})
                briefing = _text(self.helmet.call('get_compact_cortex_briefing',
                                                  {'session_id': identity['slug'], 'profile': settings.profile}), None)
                if briefing:
                    shown = clip_records(briefing, session.config.tools.clip_chars, 'session_briefs')
                    session.log.append('note', 'Briefing (administrative, not proof): ' + shown,
                                       {'companion': 'ember', 'label': 'administrative'})
                    contexts.append('Briefing (administrative note; it does not change tool approval):\n' + shown)
                    full_brief = briefing
            except _SOFT_FAILURES as error:
                failures.append(_class(error))
        if self.companion.configured:
            try:
                if identity['slug'] and settings.instance_id and settings.surface:
                    self.companion.call('companion_encounter', {'session_id': identity['slug'], 'instance_id': settings.instance_id,
                                                                'surface': settings.surface})
                self.account = _text(self.companion.call('companion_account', {}), 4096)
            except _SOFT_FAILURES as error:
                failures.append(_class(error))
        if failures:
            self.notice = f'Record unavailable ({failures[0]}); continuing without companion records.'
            contexts.append('Companion notice: ' + self.notice)
        if not contexts and not full_brief:
            return None
        result = {'context': '\n'.join(contexts)} if contexts else {}
        if full_brief:
            result['full'] = full_brief
        return result or None

    def register_tools(self, session, registry):
        settings = self.settings
        string = {'type': 'string'}

        def ember_decision(text):
            if not settings.decision_tool or not self.helmet.configured:
                return {'error': 'Decision record is unavailable'}
            try:
                self.helmet.call(settings.decision_tool, {'type': 'decision', 'note': text, 'kwargs': json.dumps({
                    'session': session.companion_identity['slug'], 'profile': settings.profile})})
            except _SOFT_FAILURES as error:
                return {'error': f'Record unavailable ({_class(error)})'}
            return {'recorded': True}

        def ember_propose(title, noticed, rationale, wants_to):
            if not self.companion.configured:
                return {'error': 'Companion record is unavailable'}
            try:
                result = self.companion.call('companion_propose', {'title': title, 'noticed': noticed, 'rationale': rationale,
                                                                   'wants_to': wants_to, 'due': {}})
            except _SOFT_FAILURES as error:
                return {'error': f'Record unavailable ({_class(error)})'}
            # The return is a note. wants_to is never executed, shelled or turned into a turn.
            text = _text(result, session.config.tools.clip_chars)
            session.log.append('note', 'Proposal recorded: ' + title, {'companion': 'ember', 'label': 'proposal'})
            return {'note': text, 'executed': False}

        if settings.decision_tool and self.helmet.configured:
            registry.register('ember_decision', 'Record an explicit decision.',
                              object_schema({'text': string}, ['text']), ember_decision)
        if self.companion.configured:
            registry.register('ember_propose', 'Leave a companion proposal. It is recorded as a note and never executed.',
                              object_schema({'title': string, 'noticed': string, 'rationale': string, 'wants_to': string},
                                            ['title', 'noticed', 'rationale', 'wants_to']), ember_propose)

    def transition(self, arguments):
        """Inspector/operator only. Denied unless allowed and the authority env is present."""
        settings = self.settings
        if not (settings.enabled and settings.allow_transition and settings.transition_authority_env
                and os.environ.get(settings.transition_authority_env) and self.companion.configured):
            raise PermissionError('Transition requires operator authority')
        if not isinstance(arguments, dict):
            raise ValueError('Transition arguments must be an object')
        self.companion.call('companion_transition', dict(arguments))
        return {'transitioned': True}

    def status(self):
        return {'enabled': self.settings.enabled, 'helmet': self.helmet.status(), 'companion': self.companion.status(),
                'reachable': self.helmet.reachable if self.helmet.configured else self.companion.reachable,
                'configured': self.helmet.configured or self.companion.configured,
                'error': self.helmet.error or self.companion.error, 'account': self.account is not None}


class Companions:
    """Install enabled companions on one session. Disabled companions do no I/O."""

    def __init__(self, config, client_factory=None, paused=False):
        self.config = config
        if client_factory is None:
            from .mcp_servers import resolve_endpoint_factory
            client_factory = resolve_endpoint_factory(config)
        self.members = []
        if paused:
            self.paused = True
            return
        self.paused = False
        for cls in (Atlas, Ember):
            member = cls(config, client_factory)
            if member.active:
                self.members.append(member)

    def install(self, session):
        # A resumed session already carries its brief in the log; only a brand-new session is briefed.
        fresh = session.log.count() == 0
        for member in self.members:
            member.register_tools(session, session.tools)
            # session_start only: a remote timeout can never stall or fail-close a tool call.
            if fresh:
                session.hooks.register('session_start', lambda payload, member=member: member.session_start(session))

    def notices(self):
        return [member.notice for member in self.members if member.notice]

    def status(self):
        def summary(name, enabled):
            for member in self.members:
                if getattr(member, 'kind', '') == name:
                    return member.status()
            return {'enabled': enabled, 'configured': False, 'reachable': None, 'error': None}
        return {'paused': self.paused,
                'atlas': summary('atlas', self.config.companions.atlas.enabled),
                'ember': summary('ember', self.config.companions.ember.enabled)}

    def member(self, name):
        for member in self.members:
            if getattr(member, 'kind', '') == name:
                return member
        return None
