"""Session-scoped orientation glue and out-of-transcript credential entry."""

from pathlib import Path
import re
import threading
import time
import uuid

from .assets import asset_path
from .tools import object_schema

PURPOSE = 'orientation'
SKILL = 'getting-started'


def documentation(path='', offset=0):
    """Read current shipped docs, never a copied knowledge summary or arbitrary file."""
    if not path:
        root = asset_path('docs')
        return {'files': ['README.md'] + (sorted('docs/' + str(p.relative_to(root))
                for p in root.rglob('*.md') if p.is_file()) if root.is_dir() else [])}
    relative = Path(path)
    if (relative.is_absolute() or '..' in relative.parts or relative.suffix != '.md'
            or (path != 'README.md' and relative.parts[0] != 'docs')):
        raise ValueError('Choose README.md or a markdown file under docs')
    target = asset_path(path)
    root = asset_path('docs').resolve() if path != 'README.md' else target.parent.resolve()
    if not target.resolve().is_relative_to(root):
        raise ValueError('Documentation path leaves its documentation root')
    if type(offset) is not int or offset < 0:
        raise ValueError('Offset must be nonnegative')
    text = target.read_text(encoding='utf-8')
    end = offset + 12000
    return {'path': path, 'text': text[offset:end], 'next_offset': end if end < len(text) else None}


class CredentialPrompts:
    """Pending metadata only. Secret values never enter this store or the chat APIs."""
    def __init__(self, manager, clock=time.monotonic):
        self.manager = manager
        self.clock = clock
        self.lock = threading.RLock()
        self.requests = {}

    def _session(self, slug):
        meta = self.manager._metadata.get(slug, {})
        if meta.get('purpose') != PURPOSE or meta.get('archived'):
            raise ValueError('Protected entry requires an active orientation session')
        return meta

    def _prune(self):
        now = self.clock()
        self.requests = {key: item for key, item in self.requests.items() if now - item['created'] < 1800}

    @staticmethod
    def binding(reference):
        return None if reference is None else tuple(getattr(reference, key, '') for key in
            ('env', 'keychain_service', 'keychain_account', 'config_ref', 'oauth'))

    @staticmethod
    def public(item):
        return {key: item[key] for key in ('id', 'name', 'purpose', 'state', 'replacing')}

    def request(self, slug, name, purpose):
        self._session(slug)
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', name):
            raise ValueError('Choose a valid connection reference name')
        if not isinstance(purpose, str) or not purpose.strip() or len(purpose) > 300:
            raise ValueError('Explain the credential purpose in 1-300 characters')
        existing = self.manager.config.credentials.get(name)
        if existing and existing.oauth:
            raise ValueError('Use browser sign-in for an OAuth connection')
        with self.lock:
            self._prune()
            if self.manager.runtime(slug).operator_stopped:
                raise ValueError('Session was stopped; continue the conversation before requesting a credential')
            pending = next((item for item in self.requests.values()
                            if item['slug'] == slug and item['state'] == 'pending'), None)
            if pending:
                if pending['name'] != name:
                    raise ValueError('Finish or cancel the pending protected entry first')
                return self.public(pending)
            if len(self.requests) >= 128:
                raise ValueError('Too many protected requests; finish an existing request first')
            item = {'id': uuid.uuid4().hex, 'slug': slug, 'name': name, 'purpose': purpose.strip(),
                    'state': 'pending', 'created': self.clock(), 'replacing': existing is not None,
                    'binding': self.binding(existing)}
            self.requests[item['id']] = item
        self.manager.runtime(slug).events.append('credential_request', self.public(item))
        return {**self.public(item), 'note': 'Use the protected form, never chat. Save and continue sends a value-free continuation; end this turn while waiting.'}

    def pending(self, slug):
        with self.lock:
            self._prune()
            meta = self.manager._metadata.get(slug, {})
            if meta.get('purpose') != PURPOSE or meta.get('archived'):
                return []
            return [self.public(item) for item in self.requests.values()
                    if item['slug'] == slug and item['state'] == 'pending']

    def cancel(self, slug):
        changed = []
        with self.lock:
            for item in self.requests.values():
                if item['slug'] == slug and item['state'] == 'pending':
                    item['state'] = 'cancelled'
                    changed.append(self.public(item))
        runtime = self.manager._runtimes.get(slug)
        if runtime is not None:
            for item in changed:
                runtime.events.append('credential_request', item)

    def resolve(self, slug, request_id, value=None, cancel=False):
        self._session(slug)
        if not isinstance(request_id, str) or not re.fullmatch(r'[a-f0-9]{32}', request_id):
            raise ValueError('Invalid protected request identity')
        runtime = self.manager.runtime(slug)
        stop_generation = runtime.operator_stop_generation
        with self.lock:
            self._prune()
            item = self.requests.get(request_id)
            if not item or item['slug'] != slug:
                raise ValueError('Protected request is unavailable or expired')
            if item['state'] == 'saved':
                return self.public(item)
            if item['state'] != 'pending':
                raise ValueError('Protected request is no longer pending')
            if cancel:
                item['state'] = 'cancelled'
                return self.public(item)
            if not isinstance(value, str) or not value or '\x00' in value or len(value) > 4096:
                raise ValueError('Enter a credential value of 1-4096 characters')
            reference = self.manager.config.credentials.get(item['name'])
            if self.binding(reference) != item['binding']:
                raise ValueError('This connection changed; request a new protected form')
            if reference and reference.oauth:
                raise ValueError('This connection now uses browser sign-in; request a new entry')
            record = {'secret': value, 'env': '',
                      'keychain_service': (reference.keychain_service if reference else '') or 'tessera',
                      'keychain_account': (reference.keychain_account if reference else '') or uuid.uuid4().hex}
            if reference is None:
                record['name'] = item['name']
            try:
                self.manager.apply_settings({'id': 'credentials', 'action': 'edit' if reference else 'add',
                                             **({'name': item['name']} if reference else {}), 'record': record})
            except Exception:
                # A storage/validation error may contain provider details. Never echo
                # submitted data or arbitrary exception messages through this channel.
                raise ValueError('Could not save securely. Check the OS credential store and try again.') from None
            item['state'] = 'saved'
            # Only references and outcome reach the ordinary turn queue, log, hooks,
            # model, compactor and memory capture. The value is never submitted.
            try:
                self.manager.submit(slug, 'Protected credential entry completed for connection '
                                    + item['name'] + '. The value was stored securely and is not available in chat. '
                                    'Continue the setup using its reference, and verify the configuration.',
                                    expected_stop_generation=stop_generation)
            except Exception:
                return {**self.public(item), 'note': 'Saved securely. Send a message to continue setup.'}
            return self.public(item)


def register_tools(runtime, registry):
    manager = runtime.manager
    registry.register('orientation_docs', 'List or read the current installed Tessera README/docs. Re-read relevant docs before configuration; use next_offset to page.',
                      object_schema({'path': {'type': 'string'}, 'offset': {'type': 'integer', 'minimum': 0}}), documentation)
    registry.register('orientation_settings', 'Read the existing Settings projection. Values of stored credentials are never returned.',
                      object_schema({}), manager.settings_view)

    def apply(body):
        if body.get('id') == 'credentials':
            raise ValueError('Request protected credential entry instead of putting credentials in tool arguments')
        return manager.apply_settings(body)
    registry.register('orientation_configure', 'Apply an existing non-secret Settings edit, with ordinary approval policy. Read orientation_settings first. Credential values must use request_credential.',
                      object_schema({'body': {'type': 'object'}}, ['body']), apply, risky=True)
    registry.register('request_credential', 'Ask the user for an API key/token in a protected form outside the transcript. name is a connection reference, purpose explains the use. Returns pending metadata, never the value. End this turn and wait for the value-free continuation.',
                      object_schema({'name': {'type': 'string'}, 'purpose': {'type': 'string'}}, ['name', 'purpose']),
                      lambda name, purpose: manager.credential_prompts.request(runtime.slug, name, purpose))

    def sign_in(provider, start=False, wait_seconds=0):
        desk = getattr(manager, 'oauth_login', None)
        if desk is None:
            raise ValueError('Sign-in is available when the UI core is running')
        if type(wait_seconds) is not int or not 0 <= wait_seconds <= 30:
            raise ValueError('Wait seconds must be between 0 and 30')
        result = desk.start(provider, manager.config) if start else desk.status(provider)
        deadline = time.monotonic() + wait_seconds
        while result.get('status') in ('starting', 'waiting') and time.monotonic() < deadline:
            registry.check_cancelled()
            time.sleep(min(0.25, max(0, deadline - time.monotonic())))
            result = desk.status(provider)
        # Public handoff fields only, even if the underlying implementation later
        # grows token fields. Never pass raw provider errors into the transcript.
        state = result.get('status', 'idle')
        return {'status': state, 'verification_url': result.get('verification_url', ''),
                'user_code': result.get('user_code', '')}
    registry.register('orientation_sign_in', 'Start the existing provider browser sign-in flow. Read status for its verification link/code. Never request OAuth tokens in chat.',
                      object_schema({'provider': {'type': 'string', 'enum': ['xai', 'openai-codex']}}, ['provider']),
                      lambda provider: sign_in(provider, True), risky=True)
    registry.register('orientation_sign_in_status', 'Read existing browser handoff status. Optional wait_seconds (0-30) waits for completion without busy polling model turns. Share only its public verification URL/code. A pending handoff is not a successful connection.',
                      object_schema({'provider': {'type': 'string', 'enum': ['xai', 'openai-codex']},
                                     'wait_seconds': {'type': 'integer', 'minimum': 0, 'maximum': 30}}, ['provider']), sign_in)
