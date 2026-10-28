"""Configured provider transport using urllib and reference-only credentials."""

import json
import http.client
import math
import time
import threading
from urllib import error, request
from urllib.parse import urlsplit

from .http_cancel import CancellableHTTP, CancelledIO
from .credentials import CredentialError, CredentialResolver, redact_secret
from .codex_headers import codex_headers, official_codex_url
from .providers import create_adapter
from .provider_status import initial_status
from .cli_worker import CliWorker, WorkerError
from .rate_limits import parse_rate_limits


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None




class BackendError(RuntimeError):
    def __init__(self, message, category='provider', credential=None, rate_limits=None,
                 *, kind=None, http_status=None, detail=''):
        super().__init__(message)
        self.category = category
        self.credential = credential
        self.rate_limits = rate_limits or {}
        self.kind = category if kind is None else kind
        self.http_status = http_status
        # Bounded operator-facing evidence (e.g. the CLI worker's stderr tail).
        # Persisted redacted; never part of model-visible text.
        self.detail = detail

    def diagnostics(self):
        """Persist bounded failure metadata, never provider text or selectors."""
        categories = ('provider', 'configuration', 'authentication', 'permission',
                      'rate_limit', 'timeout', 'connection')
        kinds = categories + ('http', 'parse', 'incomplete_response')
        category = self.category if isinstance(self.category, str) and self.category in categories else 'provider'
        kind = self.kind if isinstance(self.kind, str) and self.kind in kinds else 'provider'
        result = {'error_category': category, 'error_kind': kind}
        if kind == 'http' and type(self.http_status) is int and 100 <= self.http_status <= 599:
            result['http_status'] = self.http_status
        if isinstance(self.detail, str) and self.detail.strip():
            result['error_detail'] = self.detail[:1024]
        return result


class _StreamRedactor:
    def __init__(self, callback, secret):
        self.callback = callback
        self.secret = secret
        self.pending = {}

    def __call__(self, kind, text):
        text = self.pending.pop(kind, '') + text
        # Consume whole matches before cutting the tail, so a cut cannot
        # expose the beginning of a secret. Each kind retains at most n - 1.
        limit = max(0, len(text) - len(self.secret) + 1)
        start = 0
        parts = []
        while start < limit:
            match = text.find(self.secret, start)
            if match < 0:
                parts.append(text[start:limit])
                start = limit
                break
            parts.extend((text[start:match], '[REDACTED]'))
            start = match + len(self.secret)
        self.pending[kind] = text[start:]
        output = ''.join(parts)
        if output:
            self.callback(kind, output)

    def flush(self):
        pending, self.pending = self.pending, {}
        for kind, text in pending.items():
            if text:
                self.callback(kind, text)


class Backend:
    def __init__(self, name, base_url, model, api_key_env='', extra=None):
        self.name = name
        self.base_url = base_url.rstrip('/')
        self.model = model
        self.api_key_env = api_key_env
        self.extra = dict(extra or {})
        self.provider_kind = self.extra.pop('provider_kind', 'openai_chat')
        self.credential_ref = self.extra.pop('credential_ref', '')
        self.account = self.extra.pop('account', name)
        self.account_settings = self.extra.pop('account_settings', {})
        sources = self.extra.pop('credential_sources', {})
        oauth_home = self.extra.pop('oauth_home', '')
        # Set by Config.backend_for. Absent means this Backend was built by a test
        # or a caller that does not participate in the model endpoint gate.
        # endpoint_resolve re-reads the live config so a YOLO switch or a saved
        # number applies to a backend that is already constructed.
        self._endpoint_resolve = None
        self._endpoint_throttle = None
        self._endpoint_gate = 'endpoint_limit' in self.extra
        self.endpoint_limit = self.extra.pop('endpoint_limit', None)
        self.endpoint_home = self.extra.pop('endpoint_home', '')
        self.endpoint_requests_per_minute = self.extra.pop('endpoint_requests_per_minute', None)
        self.endpoint_tokens_per_minute = self.extra.pop('endpoint_tokens_per_minute', None)
        self.last_auth = ''
        self.worker = None
        if self.provider_kind == 'cli_worker':
            if self.credential_ref or self.api_key_env:
                raise ValueError('CLI workers use their own login, not API credentials')
            stripped = []
            for source in sources.values():
                env = source.get('env') if isinstance(source, dict) else source.env
                if env:
                    stripped.append(env)
            cli = dict(self.extra.get('cli', {}))
            if cli.get('vendor') == 'codex' and self.model:
                cli['model'] = self.model
            self.worker = CliWorker(cli, strip_env=stripped, credential_sources=sources)
            self._credentials = None
            self.adapter = None
        else:
            self._credentials = CredentialResolver(sources, oauth_home=oauth_home)
            self.adapter = create_adapter(self.provider_kind, self.extra.get('replay_cache_size', 64))
        self._transport = CancellableHTTP(_NoRedirect())
        self.cancel_event = None
        self._opener = self._transport.opener
        self._status_lock = threading.RLock()
        self._status = initial_status(bool(self.credential_ref or self.api_key_env or self.worker))
        self._observed_at = 0
        # Set per call by the session: receives ('thought'|'text'|'tool', text) as a reply streams.
        self.on_delta = None

    def status_snapshot(self):
        with self._status_lock:
            return self._observed_at, dict(self._status)

    def check_cancelled(self):
        self._transport.cancel_event = self.cancel_event
        try:
            self._transport.check_cancelled()
        except CancelledIO:
            raise BackendError('Model request cancelled by operator', 'connection') from None

    def cancel(self):
        self._transport.cancel()
        from .endpoint_limit import notify_waiters
        home = getattr(self, 'endpoint_home', '')
        if home:
            notify_waiters(home, self.name)
        worker = getattr(self, 'worker', None)
        if worker is not None:
            worker.cancel()
        return True

    def bind_endpoint(self, config, name):
        """Re-read this model's limits from the current config on each chat.

        ``config`` may be the config object or a zero-arg callable that returns
        the one the session holds now. A settings save replaces that object;
        a closure over the object from before the save would keep enforcing
        the old numbers. The snapshots in extra are what a job binding can
        hash, and those omit a Safe-mode default.
        """
        from .endpoint_limit import request_limit
        from .endpoint_throttle import throttle_limits
        owner = config if callable(config) else (lambda bound=config: bound)
        self._endpoint_owner = owner
        self._endpoint_gate = True
        self._endpoint_resolve = lambda: request_limit(owner(), name)
        self._endpoint_throttle = lambda: throttle_limits(owner(), name)
        self.endpoint_home = str(owner().home)

    def chat(self, messages, tools=None, **kw):
        if not self._endpoint_gate and self._endpoint_throttle is None:
            return self._chat_guarded(messages, tools, **kw)
        owner = getattr(self, '_endpoint_owner', None)
        if owner is not None:
            self.endpoint_home = str(owner().home)
        limit = None
        if self._endpoint_gate:
            limit = self._endpoint_resolve() if self._endpoint_resolve is not None else self.endpoint_limit
        if self._endpoint_throttle is not None:
            requests_per_minute, tokens_per_minute = self._endpoint_throttle()
        else:
            requests_per_minute = self.endpoint_requests_per_minute
            tokens_per_minute = self.endpoint_tokens_per_minute
        from .endpoint_limit import EndpointWaitCancelled, model_slot_value
        from .endpoint_throttle import endpoint_pace
        announced = False

        def note_wait(text):
            nonlocal announced
            if not isinstance(text, str) or not text.startswith('waiting for '):
                return
            announced = True
            if self.on_delta is not None:
                self.on_delta('status', text)

        try:
            with model_slot_value(self.endpoint_home, self.name, limit, note_wait, cancel=self.cancel_event):
                with endpoint_pace(self.endpoint_home, self.name, requests_per_minute, tokens_per_minute,
                                   on_wait=note_wait, cancel=self.cancel_event) as charge:
                    if announced and self.on_delta is not None:
                        self.on_delta('status', 'Running')
                    result = self._chat_guarded(messages, tools, **kw)
                    charge(result.get('usage') if isinstance(result, dict) else None)
                    return result
        except EndpointWaitCancelled:
            # Not a connection failure: that category fails over to another model.
            raise BackendError('Model request cancelled by operator', 'cancelled') from None

    def _chat_guarded(self, messages, tools=None, **kw):
        self._transport.reset()
        self.check_cancelled()
        try:
            result = self._chat(messages, tools, **kw)
        except CancelledIO:
            raise BackendError('Model request cancelled by operator', 'connection') from None
        except BackendError as exc:
            with self._status_lock:
                self._status.update(health='error', error_category=exc.category)
                if exc.credential:
                    self._status['credential'] = exc.credential
                self._observed_at = time.monotonic()
            raise
        else:
            with self._status_lock:
                self._status['health'] = 'healthy'
                if self.worker:
                    self._status['credential'] = 'available'
                self._status.pop('error_category', None)
                self._observed_at = time.monotonic()
            return result
        finally:
            self._transport.finish()

    def _chat(self, messages, tools=None, **kw):
        self.last_auth = ''
        if self.worker:
            try:
                # Compaction keeps the shared schema for HTTP prompt caching,
                # but explicitly disables calls. CLI workers need no API schema.
                if kw.get('tool_choice') == 'none':
                    tools = None
                self.worker.on_delta = self.on_delta
                try:
                    result = self.worker.chat(messages, tools, **kw)
                finally:
                    self.worker.on_delta = None
                self.last_auth = getattr(self.worker, 'auth_path', '') or 'cli_login'
                return result
            except WorkerError as exc:
                # The CLI worker's own stderr is the only place a provider-side
                # rejection (context limit, auth, quota) is reported. Dropping it
                # left operators with a bare "Turn failed (BackendError)".
                failure = getattr(getattr(self, 'worker', None), 'last_failure', None) or {}
                tail = failure.get('stderr_tail') or ''
                if not tail:
                    tail = failure.get('stdout_tail') or ''
                raise BackendError(str(exc), exc.category, detail=tail) from None
        try:
            parsed = urlsplit(self.base_url)
            if parsed.scheme not in ('http', 'https') or not parsed.netloc or parsed.username is not None or not self.model:
                raise ValueError('Provider endpoint and model must be configured')
            timeout = float(self.extra.get('timeout', 60))
            retries = int(self.extra.get('retries', 2))
            delay = float(self.extra.get('retry_delay', 0.5))
            if not math.isfinite(timeout) or timeout <= 0 or retries < 0 or not math.isfinite(delay) or delay < 0:
                raise ValueError('Invalid transport limits')
            options = dict(self.extra.get('parameters', {}))
            for key, value in self.extra.items():
                if key not in ('timeout', 'retries', 'retry_delay', 'parameters', 'path', 'api_version', 'replay_cache_size'):
                    options[key] = value
            options.update(kw)
            # A separate thinking budget must not consume the configured answer allowance.
            thinking_budget = kw.get('thinking_budget', 0)
            if type(thinking_budget) is int and thinking_budget > 0:
                for key in ('max_tokens', 'max_completion_tokens'):
                    allowance = options.get(key)
                    if type(allowance) is int and allowance > 0:
                        options[key] = allowance + thinking_budget
            headers = {'Content-Type': 'application/json'}
            payload = self.adapter.encode(self.model, messages, tools, options)
            streaming = bool(self.on_delta) and hasattr(self.adapter, 'assemble') and self.extra.get('stream', True) is not False
            if streaming:
                payload['stream'] = True
            # An OAuth grant is this call's credential. The API-key variable is not a fallback.
            env_name = self.api_key_env
            reference = self._credentials.references.get(self.credential_ref) if self.credential_ref else None
            if reference is not None and reference.oauth:
                env_name = ''
            credential = self._credentials.resolve(self.credential_ref, env=env_name)
            if credential.source == "oauth":
                self.last_auth = "oauth"
            elif credential.value:
                self.last_auth = "api_key"
            else:
                self.last_auth = "none"
            with self._status_lock:
                self._status['credential'] = 'available' if credential.value else 'not_required'
            headers.update(self.adapter.headers(credential.value, self.extra))
            if official_codex_url(self.base_url):
                headers.update(codex_headers(credential.value))
            encoded = json.dumps(payload, allow_nan=False).encode('utf-8')
            path = self.extra.get('path', self.adapter.path)
            if not isinstance(path, str) or not path.startswith('/') or path.startswith('//'):
                raise ValueError('Invalid provider request path')
            url = self.base_url + path
        except CredentialError as exc:
            raise BackendError(str(exc), 'configuration', 'missing') from None
        except (ValueError, TypeError, KeyError):
            raise BackendError('Invalid provider request configuration; check endpoint, model, api_version, max_tokens and messages', 'configuration') from None
        for attempt in range(retries + 1):
            self.check_cancelled()
            req = request.Request(url, data=encoded, headers=headers, method='POST')
            try:
                if streaming:
                    self.on_delta('status', 'Requesting model response' if attempt == 0 else 'Retrying model connection')
                with self._opener.open(req, timeout=timeout) as response:
                    self._transport.response = response
                    self.check_cancelled()
                    rates = parse_rate_limits(getattr(response, 'headers', {}))
                    kind = (getattr(response, 'headers', {}) or {}).get('Content-Type', '') if streaming else ''
                    if streaming and 'text/event-stream' in kind:
                        self.on_delta('status', 'Connected, waiting for model output')
                        on_delta = self.on_delta
                        redactor = _StreamRedactor(on_delta, credential.value) if credential.value else None
                        if redactor is None:
                            data = self.adapter.assemble(response, on_delta)
                        else:
                            try:
                                data = self.adapter.assemble(response, redactor)
                            finally:
                                redactor.flush()
                    else:
                        data = json.load(response)
                self.check_cancelled()
                result = self.adapter.decode(redact_secret(data, credential.value))
                if rates:
                    result['rate_limits'] = rates
                return result
            except error.HTTPError as exc:
                status = exc.code
                rates = parse_rate_limits(exc.headers)
                retry_after = exc.headers.get('Retry-After', '')
                exc.close()
                if status != 429 and not 500 <= status <= 599:
                    category = {401: 'authentication', 403: 'permission'}.get(status, 'provider')
                    raise BackendError(f'Backend {self.name}: HTTP {status}', category,
                                       kind='http', http_status=status) from None
                if attempt == retries:
                    category = 'rate_limit' if status == 429 else 'provider'
                    raise BackendError(f'Backend {self.name}: HTTP {status} after {attempt + 1} attempts', category,
                                       rate_limits=rates, kind='http', http_status=status) from None
                pause = delay * (2 ** attempt)
                try:
                    pause = max(pause, float(retry_after))
                except ValueError:
                    pass
                if self._transport._cancelled.wait(min(pause, 30)):
                    self.check_cancelled()
            except (error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
                self.check_cancelled()
                if attempt == retries:
                    cause = exc.reason if isinstance(exc, error.URLError) else exc
                    category = 'timeout' if isinstance(cause, TimeoutError) else 'connection'
                    raise BackendError(f'Backend {self.name}: transport failed ({type(exc).__name__})', category) from None
                if self._transport._cancelled.wait(min(delay * (2 ** attempt), 30)):
                    self.check_cancelled()
            except (ValueError, KeyError, IndexError, TypeError):
                self.check_cancelled()
                raise BackendError(f'Backend {self.name}: invalid provider response', kind='parse') from None
        raise BackendError('Backend exhausted retries')
