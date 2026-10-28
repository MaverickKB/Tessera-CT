"""Loopback-only desktop JSON/SSE API and bundled static UI."""

import base64
import gzip
import hashlib
import hmac
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from socketserver import TCPServer
import ipaddress
import json
import logging
import mimetypes
import os
from pathlib import Path
import re
import socket
import subprocess
import signal
import threading
import time
import uuid
import zlib
from urllib.parse import parse_qs, unquote, urlsplit

from .attachments import MAX_BYTES as ATTACHMENT_MAX_BYTES
from .log import read_tail
from .oauth_login import LoginDesk, LoginError
from .sessions import SessionManager
from .settings_model import (add_skill_directory, adopt_file_result, file_write_result,
                             remove_skill_directory, set_shelf_enabled, set_skill_enabled,
                             set_tool_enabled, skills_view, tools_view)
from .cua import desks, mac_helper
from .assets import asset_path
from .board import BoardError, BoardSizeError, INTAKE_SUBJECTS, _MISSING
from .floor_definition import FloorDefinitionError
from .board_status import BoardRevisionError
from .infrastructure import InventoryRevisionError
from .voice import VoiceError, configured as voice_configured, selected_pipe as voice_selected_pipe, websocket_origin
from .session_speech import SessionSpeech, SpeechError

LOCAL_VOICE_STATUS = {'connection': 502, 'upstream': 502, 'timeout': 504, 'turn_timeout': 504,
                      'invalid_response': 502, 'audio_format': 502, 'transcript': 422,
                      'reply': 502, 'turn': 409}
LOCAL_VOICE_MESSAGE = {
    'connection': 'The local speech service could not be reached.',
    'upstream': 'The local speech service refused the request.',
    'timeout': 'The local speech service did not answer in time.',
    'turn_timeout': 'The session turn did not finish in time.',
    'invalid_response': 'The local speech service answered with something unexpected.',
    'audio_format': 'The text-to-speech service returned audio in an unsupported format.',
    'transcript': 'The speech-to-text service returned no text.',
    'reply': 'The session produced no reply to speak.',
    'turn': 'The session turn failed.',
}


class ApiError(Exception):
    def __init__(self, status, message):
        self.status = status
        self.message = message


def _binding(host, port):
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not addresses:
        raise ValueError('Server host did not resolve')
    for family, kind, proto, canonical, address in addresses:
        if not ipaddress.ip_address(address[0]).is_loopback:
            raise ValueError('The desktop core requires a loopback server address')
    family, kind, proto, canonical, address = addresses[0]
    return family, address


def ui_build_id(root):
    """Content hash of the assets that need a fresh document, not of every served file.

    A stylesheet is fetched fresh on the next navigation and applies without a reload, so
    hashing it meant a cosmetic edit reloaded every open page and threw away whatever a
    person was typing into a form. Only markup and scripts change the document.

    And only the markup and scripts the document actually loads. Hashing every `.js` under
    the root counted the browser test files in `web/tests/`, which no page ever fetches:
    a session committing its own tests reloaded the operator's open page mid-sentence.
    Appending a comment to `web/tests/mobile.test.js` changed the id. So the hash is now
    index.html plus the files index.html references.
    """
    digest = hashlib.sha256()
    root = Path(root).resolve()
    index = root / 'index.html'
    if not index.is_file():
        return ''
    document = index.read_bytes()
    digest.update(b'index.html\0' + document + b'\0')
    for name in document_assets(document.decode('utf-8', 'replace')):
        # A stylesheet is refetched on the next navigation and applies without a reload,
        # so only scripts change the document. That was the point of the original split.
        if not name.endswith('.js'):
            continue
        path = (root / name).resolve()
        # A referenced file that is missing or outside the root is the server's problem,
        # not the hash's; skip it rather than crash the status route.
        if not path.is_relative_to(root) or not path.is_file():
            continue
        digest.update(name.encode() + b'\0' + path.read_bytes() + b'\0')
    return digest.hexdigest()[:16]


def document_assets(html):
    """The script and stylesheet paths an index.html asks the browser to load."""
    names = []
    for match in re.finditer(r'<(?:script|link)\b[^>]*>', html):
        tag = match.group(0)
        for attribute in ('src', 'href'):
            found = re.search(attribute + r'\s*=\s*"([^"]+)"', tag)
            if not found:
                continue
            url = found.group(1)
            if not url.startswith('/static/'):
                continue
            names.append(url[len('/static/'):])
    return names


class TesseraServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    # socketserver's default listen backlog is 5. A browser opens several connections at once
    # for the page's assets, and through a proxy (tailscale serve) each one is a fresh upstream
    # connection; on macOS overflow is answered with a reset, which the proxy reports as 502.
    request_queue_size = 128

    def __init__(self, config, manager, web_root, family, address):
        self.address_family = family
        self.config = config
        self.manager = manager
        self.web_root = Path(web_root).resolve()
        self.oauth_login = LoginDesk(Path(config.home) / 'oauth-login')
        self.stopping = threading.Event()
        self._restart_lock = threading.Lock()
        self._restarting = False
        self._restart_drained = True
        self._ui_build = (None, None)
        self._ui_build_lock = threading.Lock()
        # A restart drains the old process and exits; the supervisor starts a new one.
        # This id is minted once per process, so a page that asked for a restart can
        # tell "the old core is still draining and answering" from "a new core is up".
        self.generation = uuid.uuid4().hex
        self.started_at = time.time()
        # A voice session of its own exists from boot, so the Voice page has a
        # target that is not an ordinary chat. Best-effort: a home with voice
        # disabled gets no session, and a failure here must not stop the core.
        if config.voice.enabled:
            try:
                manager.ensure_voice()
            except (ValueError, RuntimeError, OSError):
                pass
        super().__init__(address, Handler)

    def server_bind(self):
        TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port

    def ui_build(self):
        # Assets are read from disk per request, so a pull changes the UI without a restart.
        # Rehash only when a file's name, size or mtime changed.
        files = sorted(p for p in self.web_root.rglob('*') if p.is_file())
        signature = tuple((str(p), p.stat().st_size, p.stat().st_mtime_ns) for p in files)
        with self._ui_build_lock:
            if self._ui_build[0] != signature:
                self._ui_build = (signature, ui_build_id(self.web_root))
            return self._ui_build[1]

    def restart(self, when='now'):
        """Drain running turns while still serving, then exit; idempotent.

        when='idle' waits for every session to go idle before draining. A turn
        that asks for the restart is itself the turn drain would wait on, so
        draining immediately kills the asking turn at drain_seconds instead of
        letting it finish. Waiting first makes the request a request, not a
        suicide.
        """
        with self._restart_lock:
            if self._restarting:
                return
            self._restarting = True

        def run():
            if when == 'idle':
                # Stop taking work first: a queued turn that starts during the
                # wait is a running turn, and then the wait would cover it and
                # the deadline would fall through to an interruption.
                self.manager.hold()
                # Wait while running turns are still producing something. A turn
                # that goes silent for restart_stall_seconds is treated as wedged
                # and drained; a turn that keeps working is never interrupted for
                # merely taking a long time.
                while self.manager.busy_running():
                    time.sleep(0.1)
            result = self.manager.drain()
            self._restart_drained = result['drained']
            print(f'Drained for restart: {result}', flush=True)
            self.shutdown()
        threading.Thread(target=run, name='tessera-restart', daemon=True).start()

    def shutdown(self):
        self.stopping.set()
        super().shutdown()

    def server_close(self):
        self.stopping.set()
        super().server_close()
        self.manager.close()
        _release_embedded_engine(self.config)

    def handle_error(self, request, client_address):
        # Do not log request bodies, auth headers, or plugin exception contents.
        pass

    @property
    def url(self):
        host, port = self.server_address[:2]
        if ':' in host:
            host = '[' + host + ']'
        return f'http://{host}:{port}'


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):
        pass

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def end_headers(self):
        # One response per connection. The idle socket timeout above would otherwise close
        # kept-alive connections silently, and a reverse proxy (tailscale serve) that reuses
        # such a pooled connection answers 502 for whatever asset it was fetching.
        if not any(line.lower().startswith(b'connection:') for line in getattr(self, '_headers_buffer', [])):
            self.send_header('Connection', 'close')
        self.close_connection = True
        super().end_headers()

    def do_GET(self):
        self._handle('GET')

    def do_POST(self):
        self._handle('POST')

    def do_PUT(self):
        self._handle('PUT')

    def do_PATCH(self):
        self._handle('PATCH')

    def do_DELETE(self):
        self._handle('DELETE')

    def do_OPTIONS(self):
        self._json(405, {'error': 'Method not allowed'})

    def _compressed(self, raw):
        """Return the body to send, gzip or deflate when the client accepts it and it is worth it."""
        threshold = self.server.config.server.compress_min_bytes
        if not threshold or len(raw) < threshold:
            return raw
        accepted = {}
        for part in self.headers.get('Accept-Encoding', '').split(','):
            name, _, params = part.strip().partition(';')
            weight = 1.0
            if params.strip().lower().startswith('q='):
                try:
                    weight = float(params.strip()[2:])
                except ValueError:
                    weight = 0.0
            accepted[name.strip().lower()] = weight
        if accepted.get('gzip', 0) > 0:
            self.send_header('Content-Encoding', 'gzip')
            raw = gzip.compress(raw, compresslevel=6, mtime=0)
        elif accepted.get('deflate', 0) > 0:
            self.send_header('Content-Encoding', 'deflate')
            raw = zlib.compress(raw, 6)
        return raw

    def _json(self, status, value, extra_headers=None):
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Vary', 'Accept-Encoding')
        raw = self._compressed(raw)
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(raw)

    def _check_local_request(self):
        """Reject DNS-rebinding and cross-origin browser requests.

        Returns True when the request arrived through a configured proxy host.
        The caller uses that to decide whether the shared key is required.
        A proxied request is the only path a remote client has.
        """
        bound_host, bound_port = self.server.server_address[:2]
        try:
            parsed = urlsplit('http://' + self.headers.get('Host', ''))
            name, port = (parsed.hostname or ''), (parsed.port or 80)
        except ValueError:
            raise ApiError(403, 'Invalid Host') from None
        allowed = {'localhost', '127.0.0.1', '::1', str(bound_host).lower(), self.server.config.server.host.lower()}
        # Opt-in proxy names (server.allowed_hosts); empty by default. The proxy owns the port.
        proxied = {item.lower().rstrip('.') for item in self.server.config.server.allowed_hosts}
        name = name.lower().rstrip('.')
        via_proxy = name in proxied
        if not via_proxy and (name not in allowed or port != bound_port):
            raise ApiError(403, 'Unrecognized Host')
        origin = self.headers.get('Origin')
        if origin is not None:
            try:
                origin_url = urlsplit(origin)
                origin_name = (origin_url.hostname or '').lower().rstrip('.')
                if via_proxy:
                    # Same proxied site only, over HTTPS.
                    matches = origin_url.scheme == 'https' and origin_name == name
                else:
                    matches = (origin_url.scheme == 'http' and origin_name in allowed
                               and (origin_url.port or 80) == bound_port)
            except ValueError:
                matches = False
            if not matches:
                raise ApiError(403, 'Cross-origin request rejected')
        return via_proxy

    def _presented_key(self, query):
        """Return the key this request carries, or '' when it carries none.

        Three forms, because no single one covers every client:
          Authorization: Bearer <key>   the iOS app already sends this
          X-Tessera-Key: <key>          plain header for scripts and fetch()
          ?key=<key>                    EventSource cannot set headers, so the SSE
                                        stream needs this or auth would strand it
        """
        header = self.headers.get('Authorization') or ''
        if header[:7].lower() == 'bearer ':
            return header[7:].strip()
        explicit = self.headers.get('X-Tessera-Key') or ''
        if explicit:
            return explicit.strip()
        values = query.get('key') or []
        return values[0].strip() if values else ''

    def _require_key(self, via_proxy, query, path):
        """Enforce server.auth_token for /api/ requests. No-op when unset.

        The default mode requires the key only on proxied requests, so a local
        page, the desktop window and a curl script cannot be locked out by a
        wrong or missing key. 'always' extends it to every request.
        """
        settings = self.server.config.server
        expected = settings.auth_token
        if not expected:
            return
        # Static UI is code, not data. Serving it unauthenticated lets a client
        # load the page and show an auth error instead of a blank window.
        if not path.startswith('/api/'):
            return
        if settings.auth_mode != 'always' and not via_proxy:
            return
        presented = self._presented_key(query)
        if presented and hmac.compare_digest(presented, expected):
            return
        raise ApiError(401, 'Authentication required')

    def _local_voice(self, manager, slug, body):
        """One push-to-talk utterance: speech in, a session turn, speech out."""
        from .local_voice import LocalVoiceError, utterance
        audio_text = body.get('audio')
        rate = body.get('rate')
        if not isinstance(audio_text, str) or type(rate) is not int:
            raise ApiError(400, 'Invalid local voice request')
        if voice_selected_pipe(manager.config.voice) != 'local':
            raise ApiError(409, 'The local pipe is not selected. Choose Local speech in Settings.')
        try:
            audio = base64.b64decode(audio_text, validate=True)
        except (ValueError, TypeError):
            raise ApiError(400, 'Invalid audio encoding') from None
        if not audio or len(audio) > manager.config.voice.local_max_audio_bytes:
            raise ApiError(413, 'Utterance exceeds the configured bound')
        if rate < 8000 or rate > 48000 or len(audio) % 2:
            raise ApiError(400, 'Invalid audio rate or frame size')
        try:
            return utterance(manager, slug, audio, rate, manager.config.voice.local_reply_timeout)
        except LocalVoiceError as error:
            raise ApiError(LOCAL_VOICE_STATUS.get(error.category, 400),
                           LOCAL_VOICE_MESSAGE.get(error.category, str(error))) from None

    def _body(self, allowed, required=()):
        if self.headers.get('Transfer-Encoding'):
            raise ApiError(400, 'Transfer-Encoding is unsupported')
        if self.headers.get_content_type() != 'application/json':
            raise ApiError(415, 'Expected application/json')
        try:
            length = int(self.headers.get('Content-Length', '-1'))
        except ValueError:
            raise ApiError(400, 'Invalid Content-Length') from None
        if length < 0:
            raise ApiError(411, 'Content-Length required')
        if length > 1024 * 1024:
            raise ApiError(413, 'Request body exceeds 1 MiB')
        try:
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError('Incomplete JSON')
            value = json.loads(raw)
        except (ValueError, UnicodeError):
            raise ApiError(400, 'Invalid JSON body') from None
        if not isinstance(value, dict) or set(value) - set(allowed) or not set(required) <= set(value):
            raise ApiError(400, 'Invalid request fields')
        return value

    @staticmethod
    def _integer(query, key, default, minimum=0, maximum=None):
        values = query.get(key, [str(default)])
        try:
            if len(values) != 1:
                raise ValueError()
            value = int(values[0])
        except ValueError:
            raise ApiError(400, f'Invalid {key}') from None
        if value < minimum or (maximum is not None and value > maximum):
            raise ApiError(400, f'Invalid {key}')
        return value

    def _handle(self, method):
        try:
            via_proxy = self._check_local_request()
            parsed = urlsplit(self.path)
            path = unquote(parsed.path)
            query = parse_qs(parsed.query, keep_blank_values=True)
            self._require_key(via_proxy, query, path)
            if path.startswith('/api/'):
                self._api(method, path, query)
            elif method == 'GET':
                self._static(path)
            else:
                raise ApiError(404, 'Not found')
        except ApiError as error:
            self.close_connection = True
            self._json(error.status, {'error': error.message})
        except VoiceError as error:
            self.close_connection = True
            status = {'credential': 503, 'upstream': 502, 'timeout': 504, 'invalid_response': 502,
                      'permission': 403, 'expired': 409, 'disabled': 409, 'session': 409, 'limit': 429}.get(error.category, 400)
            self._json(status, {'error': str(error), 'error_category': error.category})
        except KeyError:
            self.close_connection = True
            self._json(404, {'error': 'Session or resource not found'})
        except (ValueError, IndexError) as error:
            self.close_connection = True
            message = str(error)
            # Input validation is specific; disk/model failures do not expose file paths.
            if '/ ' in message or '\n' in message or len(message) > 300:
                message = 'Invalid request'
            self._json(409 if 'archived' in message else 400, {'error': message})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True
        except Exception as error:
            self.close_connection = True
            self._json(500, {'error': f'Request failed ({type(error).__name__})'})

    def _api(self, method, path, query):
        manager = self.server.manager
        if path == '/api/pressure/engines' and method == 'GET':
            from .engine_pressure import snapshot
            self._json(200, snapshot(self.server.config.infrastructure))
            return
        if path == '/api/status' and method == 'GET':
            shape = query.get('shape', ['full'])
            if len(shape) != 1 or shape[0] not in ('full', 'connection'):
                raise ApiError(400, 'Invalid status shape')
            report = manager.status(connection=True) if shape[0] == 'connection' else manager.status()
            self._json(200, {**report, 'ui_build': self.server.ui_build(),
                             'generation': self.server.generation,
                             'started_at': self.server.started_at})
            return
        if path == '/api/autonomy' and method == 'GET':
            self._json(200, manager.autonomy.status())
            return
        if path == '/api/autonomy/floor-definition' and method == 'GET':
            self._json(200, manager.autonomy.floor_view())
            return
        if path == '/api/autonomy/floor-definition' and method == 'PUT':
            body = self._body(('definition', 'actor'), ('definition',))
            try:
                self._json(200, manager.autonomy.floor_save(body['definition'], body.get('actor') or 'operator'))
            except FloorDefinitionError as error:
                raise ApiError(400, str(error)) from None
            return
        if path == '/api/autonomy/floor-definition/preview' and method == 'POST':
            body = self._body(('definition',))
            try:
                self._json(200, manager.autonomy.floor_preview(body.get('definition')))
            except FloorDefinitionError as error:
                raise ApiError(400, str(error)) from None
            return
        if path == '/api/autonomy/floor-definition/dry-run' and method == 'POST':
            body = self._body(('rule_id', 'definition'), ('rule_id',))
            if not isinstance(body['rule_id'], str) or not body['rule_id']:
                raise ApiError(400, 'Invalid rule id')
            try:
                self._json(200, manager.autonomy.floor_dry_run(body['rule_id'], body.get('definition')))
            except FloorDefinitionError as error:
                raise ApiError(400, str(error)) from None
            return
        if path == '/api/autonomy/floor-definition/import' and method == 'POST':
            body = self._body(('text', 'actor'), ('text',))
            if not isinstance(body['text'], str):
                raise ApiError(400, 'Floor definition import must be text')
            try:
                self._json(200, manager.autonomy.floor_import(body['text'], body.get('actor') or 'operator'))
            except FloorDefinitionError as error:
                raise ApiError(400, str(error)) from None
            return
        if path == '/api/autonomy/floor-definition/export' and method == 'GET':
            fmt = query.get('format', ['json'])
            if len(fmt) != 1 or fmt[0] not in ('json', 'toml'):
                raise ApiError(400, 'Floor definition export format must be json or toml')
            try:
                text = manager.autonomy.floor_export(fmt[0])
            except FloorDefinitionError as error:
                raise ApiError(400, str(error)) from None
            self._json(200, {'format': fmt[0], 'text': text})
            return
        if path == '/api/identities':
            # The record layer existed but nothing could reach it: no route listed or
            # created a person, so a second human needed config.toml edited by hand.
            if method == 'GET':
                self._json(200, manager.identities_view())
                return
            if method == 'POST':
                body = self._body(('name', 'display_name'), ('name',))
                self._json(201, manager.identity_create(body['name'], body.get('display_name')))
                return
            raise ApiError(405, 'Method not allowed')
        if path == '/api/identities/rename':
            if method != 'POST':
                raise ApiError(405, 'Method not allowed')
            body = self._body(('id', 'name', 'display_name'), ('id',))
            if not isinstance(body['id'], str) or len(body['id']) != 32:
                raise ApiError(400, 'Invalid identity id')
            self._json(200, manager.identity_rename(body['id'], body.get('name'), body.get('display_name')))
            return
        if path.startswith('/api/timers/') and path.endswith('/cancel') and method == 'POST':
            self._body(())
            try:
                self._json(200, manager.autonomy.cancel_timer_for_operator(unquote(path.split('/')[3])))
            except KeyError:
                raise ApiError(404, 'No such timer') from None
            return
        if path.startswith('/api/approvals/') and method == 'POST':
            approval_id = unquote(path.split('/')[3])
            try:
                body = self._body(('approve', 'source'), ('approve',))
                result = manager.autonomy.resolve(approval_id, body['approve'], str(body.get('source', 'api')))
            except KeyError:
                raise ApiError(404, 'No such approval') from None
            except ValueError as error:
                raise ApiError(409, str(error)) from None
            self._json(200, result)
            return
        if path == '/api/desk' and method == 'GET':
            self._json(200, {'desks': sorted(desks(manager.config)), 'mac': manager.config.cua.mac_enabled})
            return
        if path.startswith('/api/desk/') and method == 'GET':
            parts = path.split('/')
            if len(parts) != 5 or parts[4] not in ('look', 'status'):
                raise ApiError(404, 'Not found')
            name = unquote(parts[3])
            try:
                if name == 'mac':
                    if not manager.config.cua.mac_enabled:
                        raise ApiError(404, 'Mac computer use is off ([cua] mac_enabled)')
                    helper = mac_helper(manager.config)
                    result = helper.look() if parts[4] == 'look' else helper.run('status')
                else:
                    available = desks(manager.config)
                    if name not in available:
                        raise ApiError(404, 'No such desk')
                    result = available[name].look() if parts[4] == 'look' else available[name].status()
            except (PermissionError, RuntimeError, OSError, subprocess.SubprocessError) as error:
                raise ApiError(502, str(error)) from None
            if 'image' in result:
                image = result.pop('image')
                result = {**result, 'data_url': image['data_url']}
            self._json(200, result)
            return
        if path == '/api/integrations' and method == 'GET':
            self._json(200, manager.integrations.list())
            return
        if path.startswith('/api/integrations/'):
            parts = path.split('/')
            if len(parts) != 5 or method != 'POST':
                raise ApiError(404, 'Not found')
            item = unquote(parts[3])
            if parts[4] == 'check':
                self._body(())
                self._json(200, manager.integrations.check(item))
            elif parts[4] == 'enabled':
                self._json(200, manager.integrations.set_enabled(item, self._body(('enabled',), ('enabled',))['enabled']))
            else:
                raise ApiError(404, 'Not found')
            return
        if path == '/api/oauth/open' and method == 'POST':
            from .account_oauth import OAuthError, open_sign_in_page
            url = self._body(('url',), ('url',)).get('url') or ''
            try:
                open_sign_in_page(url)
            except OAuthError as error:
                raise ApiError(400, str(error)) from None
            self._json(200, {'opened': True})
            return
        if path == '/api/oauth/refresh' and method == 'POST':
            from .account_oauth import OAuthError, grant_report, refresh
            provider = self._body(('provider',), ('provider',)).get('provider') or ''
            if provider not in ('xai', 'openai-codex'):
                raise ApiError(400, 'Unknown account')
            try:
                refresh(self.server.config.home, provider)
            except OAuthError as error:
                raise ApiError(400, str(error)) from None
            report = grant_report(self.server.config.home, provider)
            self._json(200, {'refreshed': True, 'status': report['status'], 'message': report['status']})
            return
        if path == '/api/oauth/login':
            if method == 'POST':
                provider = self._body(('provider',), ('provider',)).get('provider') or ''
            elif method == 'GET':
                values = query.get('provider', [''])
                provider = values[0] if values else ''
            else:
                raise ApiError(405, 'Method not allowed')
            try:
                if method == 'POST':
                    result = self.server.oauth_login.start(provider, self.server.config)
                else:
                    result = self.server.oauth_login.status(provider)
            except LoginError as error:
                raise ApiError(400, str(error)) from None
            self._json(200, result)
            return
        if path == '/api/settings':
            if method == 'GET':
                self._json(200, manager.settings_view())
            elif method == 'POST':
                self._json(200, manager.apply_settings(self._body(
                    ('id', 'value', 'action', 'record', 'name'), ('id',))))
            else:
                raise ApiError(405, 'Method not allowed')
            return
        if path == '/api/models/discover':
            # Two questions, one route: a saved backend (name) or an endpoint being
            # typed into the Add-model form (base_url + kind). Nothing is written.
            if method != 'POST':
                raise ApiError(405, 'Method not allowed')
            body = self._body(('name', 'account', 'base_url', 'kind', 'credential', 'api_key_env'))
            from .model_discovery import DiscoveryError, discover, discover_account, discover_unsaved
            try:
                if isinstance(body.get('name'), str) and body['name']:
                    names = discover(manager.config, body['name'])
                elif isinstance(body.get('account'), str) and body['account']:
                    names = discover_account(manager.config, body['account'])
                else:
                    base_url = body.get('base_url')
                    if not isinstance(base_url, str) or not base_url.strip():
                        raise ApiError(400, 'A backend name or a base URL is required')
                    kind = body.get('kind') if isinstance(body.get('kind'), str) else 'openai_chat'
                    names = discover_unsaved(
                        manager.config, base_url.strip(), kind,
                        body.get('credential') if isinstance(body.get('credential'), str) else '',
                        body.get('api_key_env') if isinstance(body.get('api_key_env'), str) else '')
            except DiscoveryError as error:
                status = {'authentication': 401, 'configuration': 400,
                          'connection': 502, 'provider': 502}.get(error.category, 502)
                self._json(status, {'error': str(error), 'error_category': error.category})
                return
            self._json(200, {'models': names})
            return
        if path == '/api/config/restore' and method == 'POST':
            body = self._body(('name',), ('name',))
            try:
                self._json(200, manager.settings.restore_backup(body['name']))
            except ValueError as error:
                raise ApiError(400, str(error)[:300]) from None
            return
        if path == '/api/aethermind/engine-log' and method == 'GET':
            from .bundled_engine import engine_log_text
            self._json(200, engine_log_text(manager.config))
            return
        if path == '/api/config':
            editor = manager.config_editor
            if method == 'GET':
                self._json(200, {'config': editor.read(), 'file': editor.config_path.name,
                                 'supervised': self.server.config.server.supervised})
            elif method == 'POST':
                body = self._body(('edits', 'adopt'))
                if 'edits' in body:
                    edits = body['edits']
                    self._json(200, file_write_result(manager, edits, editor.apply(edits)))
                elif body.get('adopt') is True:
                    self._json(200, adopt_file_result(manager))
                else:
                    raise ApiError(400, 'Expected edits or adopt: true')
            else:
                raise ApiError(405, 'Method not allowed')
            return
        if path == '/api/core/restart' and method == 'POST':
            body = self._body(('when',))
            when = body.get('when', 'now')
            if when not in ('now', 'idle'):
                raise ApiError(400, "when must be 'now' or 'idle'")
            if not self.server.config.server.supervised:
                raise ApiError(409, 'Core is not supervised; restart it where it was started')
            self._json(202, {'restarting': True, 'deferred': when == 'idle',
                             'drain_seconds': self.server.config.server.drain_seconds})
            # A supervisor (launchd KeepAlive) starts the core again after a clean exit.
            self.server.restart(when)
            return
        if path == '/api/infrastructure/targets' and method == 'GET':
            self._json(200, manager.infrastructure_targets())
            return
        if path == '/api/skills':
            cwd = manager.config.workspace or manager.config.cwd
            if method == 'GET':
                self._json(200, skills_view(manager.config, cwd))
                return
            if method != 'POST':
                raise ApiError(405, 'Method not allowed')
            body = self._body(('name', 'enabled', 'add_dir', 'remove_dir', 'shelf_enabled'))
            kinds = [key for key in ('shelf_enabled', 'add_dir', 'remove_dir', 'name') if key in body]
            if len(kinds) != 1:
                raise ApiError(400, 'Expected one of shelf_enabled, name and enabled, add_dir, or remove_dir')
            try:
                if 'shelf_enabled' in body:
                    result = set_shelf_enabled(manager, body['shelf_enabled'], cwd)
                elif 'add_dir' in body:
                    result = add_skill_directory(manager, body['add_dir'], cwd)
                elif 'remove_dir' in body:
                    result = remove_skill_directory(manager, body['remove_dir'], cwd)
                else:
                    if 'enabled' not in body:
                        raise ApiError(400, 'Expected one of shelf_enabled, name and enabled, add_dir, or remove_dir')
                    result = set_skill_enabled(manager, body['name'], body['enabled'], cwd)
            except ValueError as error:
                raise ApiError(400, str(error)) from None
            self._json(200, result)
            return
        if path == '/api/tools':
            if method == 'GET':
                self._json(200, tools_view(manager.config, manager.approval_status(), manager))
                return
            if method != 'POST':
                raise ApiError(405, 'Method not allowed')
            body = self._body(('name', 'enabled'), ('name', 'enabled'))
            try:
                result = set_tool_enabled(manager, body['name'], body['enabled'])
            except ValueError as error:
                raise ApiError(400, str(error)) from None
            self._json(200, result)
            return
        if path == '/api/ui' and method == 'POST':
            self._json(200, manager.set_ui(self._body(('yolo_banner',), ('yolo_banner',))['yolo_banner']))
            return
        if path == '/api/approval':
            if method == 'GET':
                self._json(200, manager.approval_status())
            elif method == 'POST':
                self._json(200, manager.set_yolo(self._body(('yolo',), ('yolo',))['yolo']))
            else:
                raise ApiError(405, 'Method not allowed')
            return
        if path == '/api/infrastructure/sparks' and method == 'GET':
            from .sparkdash import snapshot, RANGES
            range_name = parse_qs(urlsplit(self.path).query).get('range', ['24h'])[0]
            if range_name not in RANGES:
                raise ApiError(400, 'Unknown SparkDash history range')
            self._json(200, snapshot(self.server.config.infrastructure, range_name))
            return
        if path == '/api/infrastructure' and method == 'GET':
            self._json(200, manager.infrastructure.status())
            return
        if path == '/api/infrastructure/collect' and method == 'POST':
            body = self._body(('ordinal', 'revision'), ('ordinal', 'revision'))
            try:
                report = manager.infrastructure.collect_ordinal(body['ordinal'], body['revision'])
            except InventoryRevisionError as error:
                raise ApiError(409, str(error)) from None
            self._json(200, report)
            return
        if path == '/api/board/view' and method == 'GET':
            self._json(200, manager.board_view())
            return
        if path == '/api/board/cards' and method == 'POST':
            body = self._body(('title', 'note', 'project', 'owner', 'evidence'), ('title',))
            try:
                view, card_id = manager.board_create(body['title'], note=body.get('note') or '',
                                                     project=body.get('project') or '',
                                                     owner=body.get('owner') or '', evidence=body.get('evidence'))
            except (BoardError, ValueError) as error:
                raise ApiError(409, str(error)) from None
            self._json(200, {'card_id': card_id, 'view': view})
            return
        if path == '/api/board/pressure' and method == 'GET':
            self._json(200, manager.autonomy.pressure_report())
            return
        if path == '/api/oversight' and method == 'GET':
            # On demand, not in the 15-second status poll: this reads the newest
            # bytes of every session's events log, which is too much per poll.
            self._json(200, manager.oversight_report())
            return
        if path == '/api/trials' and method == 'GET':
            # The measurement 'auto' waits for, made visible. A trial nobody can
            # read back is not a measurement.
            self._json(200, manager.trials_view())
            return
        if path == '/api/trials' and method == 'POST':
            # Starting a trial is a person's act: the change, the sample size and
            # the instrument are all named by the caller. The lane never invents
            # a change, which is why there is no route that does.
            body = self._body(('slug', 'lever', 'candidate', 'turns', 'instrument', 'why', 'margin'),
                              ('slug', 'lever', 'candidate', 'turns', 'instrument', 'why'))
            try:
                trial = manager.start_trial(body['slug'], body['lever'], body['candidate'],
                                            body['turns'], body['instrument'], body['why'],
                                            margin=body.get('margin'))
            except KeyError as error:
                raise ApiError(404, f'no such session: {error.args[0]}') from None
            except ValueError as error:
                raise ApiError(409, str(error)) from None
            self._json(200, {'trial': trial, 'view': manager.trials_view()})
            return
        if path == '/api/board/projects':
            if method != 'POST':
                raise ApiError(405, 'Method not allowed')
            body = self._body(('title', 'description', 'manager', 'note', 'kind'), ('title',))
            try:
                view, project_id = manager.board_project_create(
                    body['title'], description=body.get('description') or '',
                    manager=body.get('manager') or '', note=body.get('note') or '', kind=body.get('kind', 'planning'))
            except ValueError as error:
                raise ApiError(409, str(error)) from None
            self._json(200, {'project_id': project_id, 'view': view})
            return
        if path.startswith('/api/board/projects/'):
            parts = path.split('/')
            if len(parts) != 6 or parts[5] not in ('update', 'steps', 'step', 'note', 'add-step', 'canvas'):
                raise ApiError(404, 'Not found')
            project_id = parts[4]
            if parts[5] == 'canvas':
                if method not in ('GET', 'POST'):
                    raise ApiError(405, 'Method not allowed')
                changes = None
                if method == 'POST':
                    changes = self._body(('nodes', 'links', 'remove_nodes', 'remove_links', 'expected_version'))
                try:
                    self._json(200, manager.board_project_canvas(project_id, changes))
                except (ValueError, KeyError) as error:
                    raise ApiError(409, str(error)) from None
                return
            if parts[5] in ('note', 'add-step'):
                if method != 'POST':
                    raise ApiError(405, 'Method not allowed')
                body = self._body(('text',), ('text',))
                try:
                    view = manager.board_project_append(project_id, body['text'], step=parts[5] == 'add-step')
                except ValueError as error:
                    raise ApiError(409, str(error)) from None
                self._json(200, view)
                return
            if parts[5] == 'update':
                if method != 'POST':
                    raise ApiError(405, 'Method not allowed')
                body = self._body(('title', 'description', 'note', 'manager', 'status', 'kind'))
                try:
                    view = manager.board_project_update(
                        project_id, title=body.get('title'), description=body.get('description'),
                        note=body.get('note'), manager=body.get('manager'), status=body.get('status'), kind=body.get('kind'))
                except ValueError as error:
                    raise ApiError(409, str(error)) from None
                self._json(200, view)
                return
            if parts[5] == 'steps':
                if method != 'POST':
                    raise ApiError(405, 'Method not allowed')
                body = self._body(('steps',), ('steps',))
                if not isinstance(body['steps'], list):
                    raise ApiError(400, 'Invalid project steps')
                try:
                    view = manager.board_steps(project_id, body['steps'])
                except ValueError as error:
                    raise ApiError(409, str(error)) from None
                self._json(200, view)
                return
            if method != 'POST':
                raise ApiError(405, 'Method not allowed')
            body = self._body(('step_id', 'done'), ('step_id', 'done'))
            if not isinstance(body['step_id'], str) or type(body['done']) is not bool:
                raise ApiError(400, 'Invalid project step')
            try:
                view = manager.board_step_check(project_id, body['step_id'], body['done'])
            except ValueError as error:
                raise ApiError(409, str(error)) from None
            self._json(200, view)
            return
        if path == '/api/board' and method == 'GET':
            snapshot = manager.board_status()
            revision = snapshot.get('revision')
            headers = {'ETag': '"' + revision + '"'} if revision else {}
            validators = []
            for value in self.headers.get('If-None-Match', '').split(','):
                validators.append(value.strip().removeprefix('W/'))
            if revision and (headers['ETag'] in validators or '*' in validators):
                self.send_response(304)
                self.send_header('ETag', headers['ETag'])
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Content-Length', '0')
                self.end_headers()
            else:
                self._json(200, snapshot, headers)
            return
        if path.startswith('/api/board/cards/'):
            parts = path.split('/')
            if len(parts) != 6 or parts[5] not in ('link', 'status', 'start', 'claim', 'note', 'file', 'answer',
                                                  'remove', 'accept', 'intake-triage', 'intake-accept',
                                                  'intake-ignore'):
                raise ApiError(404, 'Not found')
            try:
                ordinal = int(parts[4])
            except ValueError:
                raise ApiError(400, 'Invalid board ordinal') from None
            if parts[5] == 'answer':
                if method != 'POST':
                    raise ApiError(405, 'Method not allowed')
                body = self._body(('revision', 'question_id', 'answer', 'session', 'card_id'), ('revision', 'question_id', 'answer'))
                if not isinstance(body['question_id'], str) or not isinstance(body['answer'], str):
                    raise ApiError(400, 'Invalid board answer')
                try:
                    moved = manager.board_answer(ordinal, body['revision'], body['question_id'], body['answer'],
                                                 actor=body.get('session') or None, card_id=body.get('card_id'))
                except BoardRevisionError as error:
                    raise ApiError(409, str(error)) from None
                except (BoardError, ValueError) as error:
                    raise ApiError(409, str(error)) from None
                self._json(200, moved)
                return
            if parts[5] == 'status':
                if method != 'POST':
                    raise ApiError(405, 'Method not allowed')
                body = self._body(('revision', 'status', 'session', 'card_id'), ('revision', 'status'))
                try:
                    moved = manager.board_move(ordinal, body['revision'], body['status'],
                                               actor=body.get('session') or None, card_id=body.get('card_id'))
                except BoardRevisionError as error:
                    raise ApiError(409, str(error)) from None
                except BoardError as error:
                    # An unknown status is a bad request (400), which this route already
                    # answered that way before the acceptance gate existed. A gated card
                    # refusing the move into succeeded is a conflict with the board's
                    # policy (409). Tell them apart rather than collapsing both.
                    message = str(error)
                    raise ApiError(400 if 'Unknown board status' in message else 409, message) from None
                self._json(200, moved)
                return
            if parts[5] == 'accept':
                if method != 'POST':
                    raise ApiError(405, 'Method not allowed')
                body = self._body(('revision', 'by', 'evidence', 'note', 'card_id'), ('revision', 'by'))
                if not isinstance(body['by'], str) or not body['by']:
                    raise ApiError(400, 'Invalid accepting identity')
                try:
                    moved = manager.board_accept(ordinal, body['revision'], body['by'],
                                                 body.get('evidence') or '', body.get('note') or '',
                                                 card_id=body.get('card_id'))
                except BoardRevisionError as error:
                    raise ApiError(409, str(error)) from None
                except (BoardError, ValueError) as error:
                    raise ApiError(409, str(error)) from None
                self._json(200, moved)
                return
            if parts[5] in ('intake-triage', 'intake-accept', 'intake-ignore'):
                if method != 'POST':
                    raise ApiError(405, 'Method not allowed')
                if parts[5] == 'intake-triage':
                    body = self._body(('revision', 'subject', 'compatibility', 'summary', 'session', 'card_id'),
                                      ('revision',))
                    if 'subject' in body and body['subject'] not in INTAKE_SUBJECTS:
                        raise ApiError(400, 'Unknown intake subject')
                    try:
                        moved = manager.board_triage_intake(
                            ordinal, body['revision'], body.get('subject'), body.get('compatibility'),
                            body.get('summary'), actor=body.get('session') or None, card_id=body.get('card_id'))
                    except BoardRevisionError as error:
                        raise ApiError(409, str(error)) from None
                    except (BoardError, ValueError) as error:
                        raise ApiError(409, str(error)) from None
                    self._json(200, moved)
                    return
                if parts[5] == 'intake-accept':
                    body = self._body(('revision', 'by', 'note', 'session', 'card_id'), ('revision', 'by'))
                    if not isinstance(body['by'], str) or not body['by']:
                        raise ApiError(400, 'Invalid accepting identity')
                    try:
                        moved = manager.board_accept_intake(ordinal, body['revision'], body['by'],
                                                            body.get('note') or '', card_id=body.get('card_id'))
                    except BoardRevisionError as error:
                        raise ApiError(409, str(error)) from None
                    except (BoardError, ValueError) as error:
                        raise ApiError(409, str(error)) from None
                    self._json(200, moved)
                    return
                body = self._body(('revision', 'by', 'reason', 'session', 'card_id'), ('revision', 'by', 'reason'))
                if not isinstance(body['by'], str) or not body['by']:
                    raise ApiError(400, 'Invalid rejecting identity')
                try:
                    moved = manager.board_ignore_intake(ordinal, body['revision'], body['by'],
                                                        body['reason'], card_id=body.get('card_id'))
                except BoardRevisionError as error:
                    raise ApiError(409, str(error)) from None
                except (BoardError, ValueError) as error:
                    raise ApiError(409, str(error)) from None
                self._json(200, moved)
                return
            if parts[5] == 'start':
                if method != 'POST':
                    raise ApiError(405, 'Method not allowed')
                body = self._body(('revision', 'session', 'receipt', 'card_id'), ('revision', 'session'))
                if not isinstance(body['session'], str) or not body['session']:
                    raise ApiError(400, 'Invalid board session')
                try:
                    moved = manager.board_start(ordinal, body['revision'], body['session'],
                                                body.get('receipt') or '', card_id=body.get('card_id'))
                except BoardRevisionError as error:
                    raise ApiError(409, str(error)) from None
                except ValueError as error:
                    raise ApiError(409, str(error)) from None
                self._json(200, moved)
                return
            if parts[5] == 'claim':
                if method != 'POST':
                    raise ApiError(405, 'Method not allowed')
                body = self._body(('revision', 'owner', 'session', 'card_id'))
                if 'revision' not in body:
                    raise ApiError(400, 'Missing revision')
                owner = body.get('owner')
                if owner is not None and not isinstance(owner, str):
                    raise ApiError(400, 'Invalid board owner')
                try:
                    moved = manager.board_claim(ordinal, body['revision'], owner,
                                            actor=body.get('session') or None, card_id=body.get('card_id'))
                except BoardRevisionError as error:
                    raise ApiError(409, str(error)) from None
                except ValueError as error:
                    raise ApiError(409, str(error)) from None
                self._json(200, moved)
                return
            if parts[5] == 'note':
                if method != 'POST':
                    raise ApiError(405, 'Method not allowed')
                body = self._body(('revision', 'note', 'title', 'session', 'card_id', 'priority'), ('revision',))
                if 'revision' not in body or not (isinstance(body.get('note'), str) or isinstance(body.get('title'), str)
                                                  or 'priority' in body):
                    raise ApiError(400, 'Missing revision or text')
                if 'priority' in body and body['priority'] is not None and type(body['priority']) is not int:
                    raise ApiError(400, 'Invalid board priority')
                try:
                    if 'title' in body or 'priority' in body:
                        # The Edit button corrects the card; a note sent with it is a replacement.
                        # priority is omitted from the call unless the body named it, so a title
                        # edit does not clear a priority the person did not touch.
                        revise_priority = body['priority'] if 'priority' in body else _MISSING
                        moved = manager.board_revise(ordinal, body['revision'],
                                                     title=body.get('title'), note=body.get('note'),
                                                     actor=body.get('session') or None, card_id=body.get('card_id'),
                                                     priority=revise_priority)
                    else:
                        moved = manager.board_note(ordinal, body['revision'], body['note'],
                                                   actor=body.get('session') or None, card_id=body.get('card_id'))
                except BoardSizeError as error:
                    # 413, not 409: the card did not change, the text did not fit. A person
                    # told "this card changed" about an oversized note learns nothing.
                    raise ApiError(413, str(error)) from None
                except BoardRevisionError as error:
                    raise ApiError(409, str(error)) from None
                except ValueError as error:
                    raise ApiError(409, str(error)) from None
                self._json(200, moved)
                return
            if parts[5] == 'file':
                if method != 'POST':
                    raise ApiError(405, 'Method not allowed')
                body = self._body(('revision', 'project', 'session', 'card_id'), ('revision', 'project'))
                if not isinstance(body['project'], str):
                    raise ApiError(400, 'Invalid board project')
                try:
                    moved = manager.board_file(ordinal, body['revision'], body['project'],
                                               actor=body.get('session') or None, card_id=body.get('card_id'))
                except BoardRevisionError as error:
                    raise ApiError(409, str(error)) from None
                except ValueError as error:
                    raise ApiError(409, str(error)) from None
                self._json(200, moved)
                return
            if parts[5] == 'remove':
                if method != 'POST':
                    raise ApiError(405, 'Method not allowed')
                body = self._body(('revision', 'session', 'card_id'), ('revision',))
                if 'revision' not in body:
                    raise ApiError(400, 'Missing revision')
                try:
                    moved = manager.board_remove(ordinal, body['revision'],
                                                 actor=body.get('session') or None, card_id=body.get('card_id'))
                except BoardRevisionError as error:
                    raise ApiError(409, str(error)) from None
                except ValueError as error:
                    raise ApiError(409, str(error)) from None
                self._json(200, moved)
                return
            if method != 'GET':
                raise ApiError(405, 'Method not allowed')
            revisions = query.get('revision', [])
            if len(revisions) != 1:
                raise ApiError(400, 'Board link needs one snapshot revision')
            try:
                link = manager.board_link(ordinal, revisions[0])
            except BoardRevisionError as error:
                raise ApiError(409, str(error)) from None
            self._json(200, link)
            return
        if path == '/api/channels':
            if method == 'GET':
                self._json(200, manager.channels.list_channels())
            elif method == 'POST':
                self._json(201, manager.create_channel(**self._body(('name',), ('name',))))
            else:
                raise ApiError(405, 'Method not allowed')
            return
        if path == '/api/work/deliver' and method == 'POST':
            self._json(200, manager.deliver_work(**self._body(('limit',))))
            return
        channel_parts = path.split('/')
        if len(channel_parts) == 5 and channel_parts[:3] == ['', 'api', 'channels']:
            if method != 'POST':
                raise ApiError(405, 'Method not allowed')
            if channel_parts[4] == 'subscriptions':
                self._json(200, manager.subscribe_channel(channel_parts[3], **self._body(('slug', 'subscribed'), ('slug',))))
            elif channel_parts[4] == 'messages':
                self._json(202, manager.publish_channel(channel_parts[3], **self._body(
                    ('source', 'text', 'parent_id', 'message_id'), ('source', 'text'))))
            else:
                raise ApiError(404, 'Not found')
            return
        if path == '/api/sessions':
            if method == 'GET':
                self._json(200, manager.list_sessions())
            elif method == 'POST':
                body = self._body(('title', 'backend', 'workspace'), ('title',))
                self._json(201, manager.create(**body))
            else:
                raise ApiError(405, 'Method not allowed')
            return
        parts = path.split('/')
        if len(parts) < 4 or parts[:3] != ['', 'api', 'sessions']:
            raise ApiError(404, 'Not found')
        slug = parts[3]
        manager.path(slug)
        if len(parts) == 4:
            if method == 'GET':
                self._json(200, manager.describe(slug))
            elif method == 'PATCH':
                self._json(200, manager.update(slug, **self._body(('title', 'archived', 'backend', 'workspace', 'reasoning', 'thinking_budget'))))
            elif method == 'DELETE':
                try:
                    self._json(200, manager.delete(slug))
                except ValueError as error:
                    raise ApiError(409, str(error)) from None
            else:
                raise ApiError(405, 'Method not allowed')
            return
        if len(parts) == 6 and parts[4] == 'voice':
            if method != 'POST':
                raise ApiError(405, 'Method not allowed')
            if parts[5] == 'start':
                body = self._body(('voice', 'keepalive'))
                keepalive = body.get('keepalive', False)
                if not isinstance(keepalive, bool):
                    raise ApiError(400, 'Invalid request fields')
                self._json(200, manager.voice.start(slug, body.get('voice'), keepalive=keepalive))
            elif parts[5] == 'local':
                body = self._body(('audio', 'rate'), ('audio', 'rate'))
                self._json(200, self._local_voice(manager, slug, body))
            elif parts[5] == 'events':
                body = self._body(('voice_id', 'events'), ('voice_id', 'events'))
                self._json(200, manager.voice.events(slug, body['voice_id'], body['events']))
            elif parts[5] == 'stop':
                body = self._body(('voice_id',), ('voice_id',))
                self._json(200, manager.voice.stop(slug, body['voice_id']))
            elif parts[5] == 'heartbeat':
                body = self._body(('voice_id',), ('voice_id',))
                self._json(200, manager.voice.heartbeat(slug, body['voice_id']))
            else:
                raise ApiError(404, 'Not found')
            return
        if len(parts) != 5:
            raise ApiError(404, 'Not found')
        resource = parts[4]
        if resource == 'speech' and method == 'POST':
            body = self._body(('message_i', 'offset', 'limit', 'encoding'), ('message_i',))
            try:
                audio, next_offset = SessionSpeech(manager).render(slug, message_i=body['message_i'],
                    offset=body.get('offset', 0), limit=body.get('limit', 300))
            except SpeechError as error:
                raise ApiError(503, str(error)) from None
            if body.get('encoding') == 'json':
                self._json(200, {'audio_base64': base64.b64encode(audio).decode('ascii'),
                                 'sample_rate': SessionSpeech.sample_rate, 'next_offset': next_offset,
                                 'voice': 'Rex'})
                return
            self.send_response(200)
            self.send_header('Content-Type', 'audio/pcm')
            self.send_header('Content-Length', str(len(audio)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('X-Audio-Sample-Rate', '24000')
            self.send_header('X-Text-Next-Offset', str(next_offset))
            self.send_header('X-Speech-Voice', 'Rex')
            self.end_headers()
            self.wfile.write(audio)
            return
        if resource in ('work', 'handoff') and method == 'POST':
            if resource == 'work':
                body = self._body(('source', 'text', 'parent_id', 'message_id'), ('source', 'text'))
                result = manager.direct_work(target=slug, **body)
            else:
                body = self._body(('source', 'brief', 'view', 'task_ref', 'parent_id', 'message_id'), ('source', 'brief'))
                result = manager.handoff(target=slug, **body)
            self._json(202, result)
            return
        if resource == 'steer' and method == 'POST':
            body = self._body(('text', 'source', 'project_id', 'selected'), ('text',))
            try:
                result = manager.steer(slug, body['text'], source=body.get('source'),
                                       project_id=body.get('project_id'), selected=body.get('selected'))
            except RuntimeError as error:
                raise ApiError(409, str(error)) from None
            self._json(202, result)
            return
        if resource == 'interrupt' and method == 'POST':
            self._body(())
            self._json(202, manager.interrupt(slug))
            return
        if resource == 'queue' and method == 'GET':
            self._json(200, {'queued': manager.queued_turns(slug)})
            return
        if resource == 'queue' and method == 'PATCH':
            body = self._body(('turn_id', 'text'), ('turn_id', 'text'))
            try:
                self._json(200, manager.revise_queued(slug, body['turn_id'], body['text']))
            except ValueError as error:
                raise ApiError(409, str(error)) from None
            return
        if resource == 'queue' and method == 'DELETE':
            body = self._body(('turn_id',), ('turn_id',))
            try:
                self._json(200, manager.cancel_queued(slug, body['turn_id']))
            except ValueError as error:
                raise ApiError(409, str(error)) from None
            return
        if resource == 'presence' and method == 'POST':
            body = self._body(('state', 'source'), ('state',))
            self._json(200, manager.autonomy.presence(slug, body['state'], str(body.get('source', ''))[:40]))
            return
        if resource == 'attachment-references' and method == 'POST':
            body = self._body(('path',), ('path',))
            if not isinstance(body['path'], str) or not Path(body['path']).is_absolute():
                raise ApiError(400, 'Local attachment needs an absolute host path')
            try:
                ref = manager.add_local_attachment(slug, body['path'])
            except (OSError, ValueError) as exc:
                raise ApiError(400, str(exc)) from None
            self._json(201, ref)
            return
        if resource == 'attachments' and method == 'POST':
            self._json(201, self._save_attachment(manager, slug))
            return
        if resource == 'attachments' and method == 'GET':
            self._send_attachment(manager, slug, query)
            return
        if resource == 'messages' and method == 'POST':
            body = self._body(('text', 'reply_to', 'source', 'client_request_id', 'attachments', 'project_id', 'selected'), ('text',))
            self._json(202, {'turn_id': manager.submit(slug, body['text'], body.get('reply_to'),
                source=body.get('source'), client_request_id=body.get('client_request_id'),
                attachments=body.get('attachments'), project_id=body.get('project_id'), selected=body.get('selected'))})
            return
        if resource == 'feedback' and method == 'POST':
            body = self._body(('i', 'rating', 'resolved', 'note'), ('i',))
            self._json(200, manager.mark(slug, body.pop('i'), **body))
            return
        if resource == 'feedback' and method == 'GET':
            self._json(200, manager.feedback(slug))
            return
        if method != 'GET':
            raise ApiError(405, 'Method not allowed')
        if resource == 'messages':
            after = self._integer(query, 'after', -1, minimum=-1)
            limit = self._integer(query, 'limit', 200, minimum=1, maximum=1000)
            # tail=N returns the newest N; clip=K shortens tool, result and note bodies (meta.clipped keeps the full length).
            tail = self._integer(query, 'tail', 0, minimum=0, maximum=1000)
            clip = self._integer(query, 'clip', 0, minimum=0)
            if tail and slug not in manager._runtimes:
                rows = read_tail(manager.path(slug) / 'log', tail)
            else:
                rows = list(manager.get(slug).log.iter(after + 1))
                rows = rows[-tail:] if tail else rows[:limit]
            messages = []
            for message in rows:
                if isinstance(message, dict):
                    row = message
                else:
                    row = asdict(message)
                if clip and row['kind'] in ('tool', 'echo', 'note') and len(row['text']) > clip:
                    row['meta'] = {**row['meta'], 'clipped': len(row['text'])}
                    row['text'] = row['text'][:clip]
                messages.append(row)
            self._json(200, messages)
        elif resource == 'memory':
            runtime = manager.runtime(slug)
            session = runtime.session
            scope = query.get('scope', ['session'])[0]
            if scope not in ('session', 'personal'):
                raise ApiError(400, 'Memory scope must be session or personal')
            project = session.capture.personal_project if scope == 'personal' else session.capture.project_id
            if not project:
                raise ApiError(400, 'This memory scope is not configured')
            if ('level' in query) != ('index' in query):
                raise ApiError(400, 'Memory zoom needs both level and index')
            if 'level' in query:
                level = self._integer(query, 'level', 0, maximum=64)
                index = self._integer(query, 'index', 0)
                result = session.capture.memory_recall(project_id=project, level=level, index=index)
            else:
                budget = self._integer(query, 'budget', 4000, minimum=256, maximum=16000)
                result = session.capture.memory_tree(project_id=project, budget_chars=budget)
            self._json(200, session.log.redact({'scope': scope, 'source': 'live_aethermind_read', 'tree': result}))
        elif resource == 'view':
            runtime = manager.runtime(slug)
            session = runtime.session
            rows = session.view.lines()
            view_bytes = len('<chat>\n</chat>'.encode('utf-8'))
            for row in rows:
                view_bytes += row['bytes']
            # The panel shows the newest lines; older ones stay in the tree and zoom
            # still opens any of them. The whole-view byte count is kept because it is
            # the prompt budget the person is reading.
            tail = self._integer(query, 'tail', self.server.config.ui.view_page_lines) \
                if 'tail' in query else self.server.config.ui.view_page_lines
            omitted = 0
            if tail and len(rows) > tail:
                omitted = len(rows) - tail
                rows = rows[omitted:]
            self._json(200, {'lines': rows, 'bytes': view_bytes, 'omitted': omitted,
                             'high': session.view.high_bytes, 'low': session.view.low_bytes,
                             'inheritance': runtime.inheritance, 'debt': len(session.capture.debt())})
        elif resource == 'zoom':
            session = manager.get(slug)
            start = self._integer(query, 'start', 0)
            count = self._integer(query, 'count', 1, minimum=1)
            offset = self._integer(query, 'offset', 0)
            limit = self._integer(query, 'limit', 16000, minimum=1, maximum=64000) if 'limit' in query else None
            text = session.view.zoom(start, count, offset=offset, limit=limit)
            if count == 1:
                self._json(200, {'text': text})
            else:
                lines = []
                half = count // 2
                level = half.bit_length() - 1
                for position in (start, start + half):
                    node = session.tree.node(level, position // half)
                    value = node if node is not None else '(no summary yet; zoom in for the raw messages)'
                    lines.append({'start': position, 'count': half, 'level': level,
                                  'text': value, 'bytes': len(value.encode('utf-8'))})
                self._json(200, {'lines': lines})
        elif resource == 'events':
            self._events(manager.runtime(slug), query)
        else:
            raise ApiError(404, 'Not found')

    def _events(self, runtime, query=None):
        header = self.headers.get('Last-Event-ID')
        try:
            checkpoint = int(header if header is not None else '0')
        except ValueError:
            raise ApiError(400, 'Invalid Last-Event-ID') from None
        if checkpoint < 0:
            raise ApiError(400, 'Invalid Last-Event-ID')
        limit = self.server.config.server.event_replay_limit
        fresh = False
        # A page that fetched the transcript on this same open asks not to be sent
        # the message bodies a second time as events. The default stays as it was:
        # a plain SSE consumer still receives the replay window it always did.
        skip_messages = 'messages' in (query or {}) and (query or {})['messages'] == ['skip']
        if header is None and limit > 0:
            # A page opening a long session has no checkpoint. Replaying every event
            # since the session began cost megabytes and rebuilt messages the page
            # already fetches. Start at the newest events instead; a client that does
            # hold a checkpoint still resumes exactly where it left off.
            checkpoint = runtime.events.replay_start(limit)
            # Only a page that skipped message bodies needs the closing checkpoint:
            # it never saw those ids, so a reconnect would replay the window again.
            fresh = skip_messages
        runtime.events.after(checkpoint)
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('X-Accel-Buffering', 'no')
        self.send_header('Connection', 'close')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.close_connection = True
        live_cursor = runtime.events.live_seq
        resumed = False
        try:
            while not self.server.stopping.is_set() and not runtime.events.closed:
                records = runtime.events.after(checkpoint, timeout=1)
                live = runtime.events.live_after(live_cursor)
                for seq, event, data in live:
                    # No id: live records never move the client's Last-Event-ID.
                    self.wfile.write(f'event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n'.encode('utf-8'))
                    live_cursor = seq
                if not records and not live:
                    self.wfile.write(b': keepalive\n\n')
                for record in records:
                    if skip_messages and record['event'] == 'message':
                        checkpoint = record['id']
                        continue
                    data = json.dumps(record['data'], ensure_ascii=False, allow_nan=False)
                    self.wfile.write(f'id: {record["id"]}\nevent: {record["event"]}\ndata: {data}\n\n'.encode('utf-8'))
                    checkpoint = record['id']
                if fresh:
                    # A fresh page never sees the ids of the skipped message events,
                    # so hand it the newest id it should resume from next time.
                    fresh = False
                    self.wfile.write(f'id: {checkpoint}\nevent: checkpoint\ndata: {{}}\n\n'.encode('utf-8'))
                if not resumed:
                    # A page that attaches mid-turn gets history and then silence: the
                    # working turn looks stalled. Tell it the turn is running and hand it
                    # the text accumulated so far, which live-only deltas never replay.
                    resumed = True
                    active = runtime.active
                    turn_id = active['id'] if active else ''
                    snapshot = runtime.events.turn_snapshot(turn_id) if turn_id else None
                    state = {'busy': bool(active), 'turn_id': turn_id, 'live': snapshot,
                             'started_at': active.get('started_at') if active else None,
                             'operator_stopped': runtime.operator_stopped}
                    self.wfile.write(f'event: resume\ndata: {json.dumps(state, ensure_ascii=False)}\n\n'.encode('utf-8'))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass

    def _save_attachment(self, manager, slug):
        if self.headers.get('Transfer-Encoding'):
            raise ApiError(400, 'Transfer-Encoding is unsupported')
        try:
            length = int(self.headers.get('Content-Length', '-1'))
        except ValueError:
            raise ApiError(400, 'Invalid Content-Length') from None
        if length < 1:
            raise ApiError(400, 'Attachment is empty')
        if length > ATTACHMENT_MAX_BYTES:
            raise ApiError(413, f'Attachment exceeds {ATTACHMENT_MAX_BYTES} bytes')
        data = self.rfile.read(length)
        if len(data) != length:
            raise ApiError(400, 'Incomplete attachment')
        name = unquote(self.headers.get('X-File-Name', '') or 'file')[:120]
        claimed = (self.headers.get_content_type() or '').split(';', 1)[0].strip().lower()
        try:
            return manager.add_attachment(slug, data, name, claimed)
        except ValueError as exc:
            raise ApiError(400, str(exc)) from None
        except KeyError:
            raise ApiError(404, 'Session not found') from None

    def _send_attachment(self, manager, slug, query):
        ids = query.get('id', [])
        if len(ids) != 1:
            raise ApiError(400, 'Attachment needs one id')
        try:
            path, media, body = manager.attachment_content(slug, ids[0])
        except ValueError as exc:
            raise ApiError(404, str(exc)) from None
        except KeyError:
            raise ApiError(404, 'Session not found') from None
        self.send_response(200)
        self.send_header('Content-Type', media)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Disposition', 'inline')
        self.end_headers()
        self.wfile.write(body)

    def _static(self, path):
        if path == '/':
            relative = 'index.html'
        elif path in ('/favicon.ico', '/apple-touch-icon.png', '/apple-touch-icon-precomposed.png'):
            # WKWebView asks for these on every load. A 404 becomes the Cocoa banner
            # "The object can not be found here." The F-it setting alone decides the
            # gremlin favicon; the icon picker is a logo choice and does not gate it.
            # The apple-touch png routes stay the product icon: native home-screen and
            # dock icons are not alternate icons. Settings replace the manager's
            # config. The server object keeps the copy from boot, so the icon has to
            # read the live one.
            ui = self.server.manager.config.ui
            if path == '/favicon.ico' and ui.easter_egg_f_it is True:
                relative = 'brand/gremlin-logo.svg'
            else:
                relative = 'brand/icon-32.png'
        elif path.startswith('/static/'):
            relative = path[len('/static/'):]
        else:
            raise ApiError(404, 'Not found')
        if '\\' in relative or '\x00' in relative or '..' in Path(relative).parts:
            raise ApiError(404, 'Not found')
        target = (self.server.web_root / relative).resolve()
        if not target.is_relative_to(self.server.web_root) or not target.is_file():
            raise ApiError(404, 'Not found')
        body = target.read_bytes()
        media_type = mimetypes.guess_type(target.name)[0] or 'application/octet-stream'
        self.send_response(200)
        self.send_header('Content-Type', media_type + ('; charset=utf-8' if media_type.startswith('text/') or media_type == 'application/javascript' else ''))
        self.send_header('Vary', 'Accept-Encoding')
        self.send_header('Cache-Control', 'no-cache')
        if media_type.startswith('text/') or media_type in ('application/javascript', 'application/json', 'image/svg+xml'):
            body = self._compressed(body)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('X-Content-Type-Options', 'nosniff')
        # The document origin is the configured loopback core, so the core is
        # 'self' rather than a host or port. ipc: and http://ipc.localhost are
        # Tauri's IPC placeholders for the desktop server controls. A configured
        # voice WebSocket origin is appended only when voice is enabled.
        connect = "'self' ipc: http://ipc.localhost"
        voice = self.server.config.voice
        if voice.enabled and voice_configured(voice):
            connect += ' ' + websocket_origin(voice)
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; script-src-attr 'none'; style-src 'self'; img-src 'self' blob: data:; font-src 'self'; connect-src " + connect + "; media-src 'none'; object-src 'none'; frame-src 'none'; worker-src 'self'; manifest-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)


def create_server(config, manager=None, web_root=None, backend_factory=None):
    config.validate()
    family, address = _binding(config.server.host, config.server.port)
    manager = manager or SessionManager(config, backend_factory=backend_factory)
    root = web_root or asset_path('web')
    try:
        return TesseraServer(config, manager, root, family, address)
    except Exception:
        manager.close()
        raise


def serve(config):
    from .bundled_engine import ensure_local_engine_bundle
    ensure_local_engine_bundle(config)
    with create_server(config) as server:
        # A supervisor's SIGTERM drains like /api/core/restart instead of killing running turns.
        signal.signal(signal.SIGTERM, lambda *args: server.restart())
        print(f'Serving Tessera at {server.url}', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        if config.server.supervised:
            # The listener has stopped, so this process has to actually end or the
            # supervisor never sees it exit and never starts the new build. Whether
            # the drain succeeded or timed out, the turns it covered are already
            # persisted and fenced; waiting for them again in manager.close can
            # strand a closed listener forever. Release process ownership to the
            # supervisor either way; startup recovers the durable queue.
            #
            # Exiting only on the timeout path let a SUCCESSFUL drain print
            # 'Drained for restart' and then resume serving on the same pid.
            # /api/core/restart answered 200 and the core kept serving the old code
            # indefinitely, which is the worst shape: a restart that reports
            # success and does nothing.
            print('Restart drained; handing core back to supervisor.' if server._restart_drained
                  else 'Restart drain timed out; handing core back to supervisor.', flush=True)
            # os._exit skips server_close, which is the other place the engine stops.
            _release_embedded_engine(config)
            os._exit(0)


def _release_embedded_engine(config):
    """Stop the embedded engine. A missing bundle is reported, not raised."""
    from .bundled_engine import stop_bundled_engine
    try:
        result = stop_bundled_engine(config)
    except OSError as error:
        detail = str(error).replace('\n', ' ').strip()
        reason = 'Embedded engine stop failed: ' + type(error).__name__
        if detail and detail != type(error).__name__:
            reason += ': ' + detail
        result = {'stopped': False, 'reason': reason}
    reason = result.get('reason') if isinstance(result, dict) else ''
    stopped = isinstance(result, dict) and result.get('stopped') is True
    if not stopped:
        visible = reason or result
        from .bundled_engine import _IDLE_STOP_REASONS
        idle = isinstance(reason, str) and reason.startswith(_IDLE_STOP_REASONS)
        logger = logging.getLogger('tessera.server')
        if idle:
            logger.debug('%s', visible)
        else:
            logger.warning('%s', visible)
            print(visible, flush=True)
    return result
