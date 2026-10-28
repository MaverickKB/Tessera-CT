"""Detached inspector: a Unix-socket attach bound by the resident daemon.

The resident process stays the only writer of its home. Inspector connections
never take ``sessions/.manager.lock`` or a session ``.lock``; reads copy committed
JSONL lines without locking, and the few write methods (note, tighten, companion
pause, revert) are performed by the daemon itself as the lock holder, each after
an inverse record in ``run/inspector-journal.jsonl``. There is no TCP, no port
and no HTTP. Socket handler failures stay in their own thread.
"""

from dataclasses import asdict, fields as dataclass_fields
import hmac
import json
import os
from pathlib import Path
import socket
import stat
import struct
import sys
import threading
import time
import uuid

from .rpc import RpcError, _parse

PROTOCOL_VERSION = 1
OBSERVER_METHODS = ('inspect/status', 'inspect/sessions', 'inspect/log', 'inspect/events', 'inspect/view',
                    'inspect/zoom', 'inspect/config', 'inspect/code', 'inspect/approvals')
EDITOR_METHODS = ('inspect/note', 'inspect/tighten', 'inspect/companion-pause', 'inspect/revert',
                  'inspect/ember-transition')
METHODS = OBSERVER_METHODS + EDITOR_METHODS
NOTICE = 'Inspector socket unavailable; the resident daemon continues without it.'


def resolve_socket(home, socket_path):
    path = Path(socket_path).expanduser()
    return path if path.is_absolute() else Path(home) / path


def committed_lines(path, after=0, limit=None):
    """Return parsed complete lines, skipping a partial tail. Opens read-only, no lock."""
    rows = []
    if not path.is_file():
        return rows
    with path.open('rb') as stream:
        data = stream.read()
    end = data.rfind(b'\n')
    if end < 0:
        return rows
    for index, line in enumerate(data[:end + 1].splitlines()):
        if index < after:
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            break
        if limit is not None and len(rows) >= limit:
            break
    return rows


def chat_files(session_root):
    """Dated ChatLog files only; events.jsonl and turns.jsonl are excluded."""
    log = session_root / 'log'
    names = sorted(path for path in log.glob('*.jsonl')
                   if path.name not in ('events.jsonl', 'turns.jsonl')) if log.is_dir() else []
    return names


def read_log(session_root, after, limit):
    messages = []
    for path in chat_files(session_root):
        for row in committed_lines(path):
            if isinstance(row, dict) and type(row.get('i')) is int and row['i'] > after:
                messages.append(row)
                if len(messages) >= limit:
                    return messages
    return messages


def session_rows(home):
    rows = []
    root = Path(home) / 'sessions'
    if not root.is_dir():
        return rows
    for path in sorted(root.iterdir()):
        meta = path / 'meta.json'
        if path.is_dir() and not path.is_symlink() and meta.is_file():
            try:
                value = json.loads(meta.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                continue
            rows.append({'slug': value.get('slug'), 'title': value.get('title'), 'archived': bool(value.get('archived'))})
    return rows


def redacted_config(config):
    """Section names, enabled flags and whether endpoint lists are empty. No URLs, no secrets."""
    def endpoints(values):
        return {'empty': not values, 'count': len(values)}
    atlas, ember, memory = config.companions.atlas, config.companions.ember, config.aethermind
    return {
        'sections': sorted(item.name for item in dataclass_fields(config)
                           if item.name not in ('home', 'cwd', 'user_instructions', 'inspector_error',
                                                'config_missing')),
        'backends': {name: {'configured': True} for name in config.backends},
        'roles': sorted(config.roles),
        'tools': {'approval': config.tools.approval},
        'aethermind': {
            'required': True,
            'retired_enabled': (getattr(config, 'retired_settings', {}) or {}).get('aethermind.enabled', None),
            'endpoints': endpoints(memory.endpoints or ([memory.url] if memory.url else [])),
        },
        'jev': {'enabled': config.jev.enabled},
        'companions': {
            'atlas': {'enabled': atlas.enabled, 'endpoints': endpoints(atlas.endpoints),
                      'allow_index_refresh': atlas.allow_index_refresh, 'read_prefixes': len(atlas.read_uri_prefixes)},
            'ember': {'enabled': ember.enabled, 'helmet_endpoints': endpoints(ember.helmet_endpoints),
                      'companion_endpoints': endpoints(ember.companion_endpoints), 'allow_transition': ember.allow_transition},
        },
        'inspector': {'enabled': config.inspector.enabled, 'source_root_set': bool(config.inspector.source_root),
                      'editor_available': bool(config.inspector.operator_approval_env)},
        'identity_file_set': bool(config.identity_file),
    }


def offline_status(home, config=None):
    """Socket down: read meta.json, view.json and redacted config only. No locks, no app-server."""
    home = Path(home)
    sessions = []
    for row in session_rows(home):
        view = home / 'sessions' / str(row['slug']) / 'view.json'
        sessions.append({**row, 'view_bytes': view.stat().st_size if view.is_file() else None})
    return {'attached': False, 'sessions': sessions, 'config': redacted_config(config) if config else None}


def safe_source(root, relative):
    if not root:
        raise PermissionError('source_root is not configured')
    base = Path(root).expanduser().resolve()
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute() or '..' in Path(relative).parts:
        raise ValueError('Code path must be relative to source_root')
    target = base / relative
    resolved = target.resolve()
    if not resolved.is_relative_to(base):
        raise ValueError('Code path escapes source_root')
    return base, resolved


def peer_identity(connection):
    """Return (uid, gid) of the connected peer using kernel credentials."""
    if hasattr(socket, 'SO_PEERCRED'):
        raw = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i'))
        _, uid, gid = struct.unpack('3i', raw)
        return uid, gid
    # macOS/BSD: LOCAL_PEERCRED returns struct xucred {u_int version; uid_t uid; short ngroups; gid_t groups[16]}.
    local_peercred = getattr(socket, 'LOCAL_PEERCRED', 0x001)
    raw = connection.getsockopt(0, local_peercred, struct.calcsize('IIh16I') + 8)
    version, uid, ngroups = struct.unpack_from('IIh', raw)
    groups = struct.unpack_from('16I', raw, struct.calcsize('IIh') + 2)
    return uid, groups[0] if ngroups > 0 else -1


class InspectorServer:
    def __init__(self, daemon):
        self.daemon = daemon
        self.config = daemon.config
        self.settings = daemon.config.inspector
        self.path = resolve_socket(self.config.home, self.settings.socket_path)
        self.listener = None
        self.thread = None
        self.error = None
        self.stopping = threading.Event()
        self.slots = threading.BoundedSemaphore(self.settings.max_pending_requests)
        self.journal_lock = threading.Lock()

    @property
    def bound(self):
        return self.listener is not None

    def start(self):
        if not self.settings.enabled:
            return False
        try:
            if self.config.inspector_error:
                raise ValueError('Invalid inspector configuration')
            directory = self.path.parent
            directory.mkdir(parents=True, exist_ok=True)
            os.chmod(directory, 0o700)
            if self.path.exists() or self.path.is_symlink():
                if not stat.S_ISSOCK(self.path.lstat().st_mode):
                    raise FileExistsError('Inspector socket path is not a socket')
                probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                try:
                    probe.connect(str(self.path))
                except OSError:
                    self.path.unlink()  # Stale socket from a previous daemon.
                else:
                    probe.close()
                    raise FileExistsError('Another daemon owns the inspector socket')
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            previous = os.umask(0o177)
            try:
                listener.bind(str(self.path))
            finally:
                os.umask(previous)
            os.chmod(self.path, 0o600)
            listener.listen(8)
            listener.settimeout(0.2)
        except Exception as error:
            self.error = type(error).__name__
            print(NOTICE, file=sys.stderr)
            return False
        self.listener = listener
        self.thread = threading.Thread(target=self._accept, name='tessera-inspector', daemon=True)
        self.thread.start()
        return True

    def stop(self):
        self.stopping.set()
        if self.listener is not None:
            try:
                self.listener.close()
            finally:
                self.listener = None
                try:
                    if stat.S_ISSOCK(self.path.lstat().st_mode):
                        self.path.unlink()
                except OSError:
                    pass
        if self.thread is not None:
            self.thread.join(timeout=2)

    def status(self):
        return {'enabled': self.settings.enabled, 'bound': self.bound, 'error': self.error or (self.config.inspector_error or None)}

    def _accept(self):
        while not self.stopping.is_set():
            try:
                connection, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self._connection, args=(connection,), name='tessera-inspector-client', daemon=True).start()

    def _allowed_peer(self, connection):
        try:
            uid, gid = peer_identity(connection)
        except OSError:
            return False
        if uid == os.getuid():
            return True
        return bool(self.settings.allow_groups) and gid in self.settings.allow_groups

    def _connection(self, connection):
        try:
            with connection:
                if not self._allowed_peer(connection):
                    return
                connection.settimeout(None)
                stream = connection.makefile('rwb')
                role = None
                limit = self.settings.max_frame_bytes
                while not self.stopping.is_set():
                    line = stream.readline(limit + 1)
                    if not line:
                        return
                    request_id = None
                    try:
                        if len(line) > limit:
                            while line and not line.endswith(b'\n'):
                                line = stream.readline(limit + 1)
                            raise RpcError(-32005, 'Request exceeds configured frame limit')
                        if not line.endswith(b'\n'):
                            raise RpcError(-32700, 'Incomplete JSON frame')
                        request = _parse(line)
                        request_id = request.get('id')
                        if 'id' not in request:
                            continue
                        method, params = request.get('method'), request.get('params', {})
                        if not isinstance(method, str) or not isinstance(params, dict):
                            raise RpcError(-32602, 'Method and object parameters required')
                        if method == 'initialize':
                            if role is not None:
                                raise RpcError(-32600, 'Already initialized')
                            role = self._initialize(params)
                            result = {'serverInfo': {'name': 'tessera-inspector', 'protocolVersion': PROTOCOL_VERSION},
                                      'role': role, 'methods': list(METHODS if role == 'editor' else OBSERVER_METHODS)}
                        elif role is None:
                            raise RpcError(-32002, 'Initialize the attachment first')
                        else:
                            result = self._bounded(role, method, params)
                        self._send(stream, {'id': request_id, 'result': result})
                    except RpcError as error:
                        self._send(stream, {'id': request_id, 'error': {'code': error.code, 'message': error.message}})
                    except KeyError:
                        self._send(stream, {'id': request_id, 'error': {'code': -32004, 'message': 'Session or resource not found'}})
                    except PermissionError:
                        self._send(stream, {'id': request_id, 'error': {'code': -32008, 'message': 'Operator authority required'}})
                    except (ValueError, TypeError, IndexError):
                        self._send(stream, {'id': request_id, 'error': {'code': -32602, 'message': 'Invalid parameters or unavailable configuration'}})
                    except Exception as error:
                        print('Inspector request failed (' + type(error).__name__ + ')', file=sys.stderr)
                        self._send(stream, {'id': request_id, 'error': {'code': -32603, 'message': 'Inspector request failed'}})
        except Exception:
            # Connection failures stay in this thread; the resident turn loop is unaffected.
            return

    def _send(self, stream, message):
        raw = json.dumps({'jsonrpc': '2.0', **message}, ensure_ascii=False, allow_nan=False).encode() + b'\n'
        if len(raw) > self.settings.max_frame_bytes:
            raw = json.dumps({'jsonrpc': '2.0', 'id': message.get('id'),
                              'error': {'code': -32005, 'message': 'Response exceeds configured frame limit'}}).encode() + b'\n'
        stream.write(raw)
        stream.flush()

    def _initialize(self, params):
        allowed = {'clientInfo', 'role', 'operatorApproval', 'clientHome'}
        if set(params) - allowed:
            raise RpcError(-32602, 'Invalid initialize parameters')
        role = params.get('role', 'observer')
        if role not in ('observer', 'editor'):
            raise RpcError(-32602, 'Role must be observer or editor')
        if role == 'editor':
            name = self.settings.operator_approval_env
            expected = os.environ.get(name, '') if name else ''
            supplied = params.get('operatorApproval')
            if not expected or not isinstance(supplied, str) or not hmac.compare_digest(supplied.encode(), expected.encode()):
                raise RpcError(-32008, 'Editor role requires operator approval')
            home = params.get('clientHome')
            if not isinstance(home, str) or Path(home).expanduser().resolve() == Path(self.config.home).resolve():
                # The detached session needs its own home; the resident home cannot edit itself.
                raise RpcError(-32008, 'Editor role requires a separate inspector home')
        return role

    def _bounded(self, role, method, params):
        if method not in METHODS:
            # Includes any approval decision: the inspector may never approve a resident tool.
            raise RpcError(-32601, 'Method not found')
        if method in EDITOR_METHODS and role != 'editor':
            raise RpcError(-32008, 'Editor role required')
        if not self.slots.acquire(blocking=False):
            # Saturation drops the inspector request, never the turn.
            raise RpcError(-32006, 'Inspector request queue is full')
        try:
            return self.dispatch(method, params)
        finally:
            self.slots.release()

    # Methods -----------------------------------------------------------------

    def _session_root(self, slug):
        root = Path(self.config.home) / 'sessions'
        if not isinstance(slug, str) or not slug or '/' in slug or slug.startswith('.'):
            raise ValueError('Invalid session slug')
        path = root / slug
        if path.is_symlink() or not (path / 'meta.json').is_file() or path.resolve().parent != root.resolve():
            raise KeyError(slug)
        return path

    def _journal(self, action, inverse):
        record = {'id': uuid.uuid4().hex, 'time': time.time(), 'action': action, 'inverse': inverse}
        path = Path(self.config.home) / 'run' / 'inspector-journal.jsonl'
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.journal_lock:
            with path.open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + '\n')
                stream.flush()
                os.fsync(stream.fileno())
        return record['id']

    @staticmethod
    def _fields(params, allowed, required=()):
        if set(params) - set(allowed) or not set(required) <= set(params):
            raise RpcError(-32602, 'Invalid method parameters')

    @staticmethod
    def _integer(params, name, default, minimum=0, maximum=None):
        value = params.get(name, default)
        if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
            raise RpcError(-32602, 'Invalid ' + name)
        return value

    def dispatch(self, method, params):
        daemon = self.daemon
        page = self.config.daemon.history_page_size
        if method == 'inspect/status':
            self._fields(params, ())
            return daemon.inspect_status()
        if method == 'inspect/sessions':
            self._fields(params, ())
            return {'data': session_rows(self.config.home)}
        if method == 'inspect/log':
            self._fields(params, ('threadId', 'after', 'limit'), ('threadId',))
            root = self._session_root(params['threadId'])
            after = self._integer(params, 'after', -1, minimum=-1)
            limit = self._integer(params, 'limit', page, minimum=1, maximum=page)
            return {'messages': read_log(root, after, limit)}
        if method == 'inspect/events':
            self._fields(params, ('threadId', 'afterEventId', 'limit'), ('threadId',))
            root = self._session_root(params['threadId'])
            after = self._integer(params, 'afterEventId', 0)
            limit = self._integer(params, 'limit', page, minimum=1, maximum=page)
            return {'events': committed_lines(root / 'events.jsonl', after, limit)}
        if method in ('inspect/view', 'inspect/zoom'):
            self._session_root(params.get('threadId'))
            return daemon._view('view/read' if method == 'inspect/view' else 'view/zoom', params)
        if method == 'inspect/config':
            self._fields(params, ())
            return redacted_config(self.config)
        if method == 'inspect/code':
            self._fields(params, ('path',), ('path',))
            _, target = safe_source(self.settings.source_root, params['path'])
            home = Path(self.config.home).resolve()
            if target.is_relative_to(home) or not target.is_file():
                raise KeyError(params['path'])
            size = target.stat().st_size
            if size > self.settings.max_frame_bytes // 2:
                raise RpcError(-32005, 'File exceeds configured frame limit')
            data = target.read_bytes()
            import hashlib
            return {'path': params['path'], 'text': data.decode('utf-8', errors='replace'),
                    'sha256': hashlib.sha256(data).hexdigest(), 'bytes': size}
        if method == 'inspect/approvals':
            self._fields(params, ())
            return {'data': daemon.pending_approval_list()}
        if method == 'inspect/note':
            self._fields(params, ('threadId', 'text'), ('threadId', 'text'))
            text = params['text']
            if not isinstance(text, str) or not text.strip() or len(text.encode()) > self.settings.max_note_bytes:
                raise RpcError(-32602, 'Note text must be 1..max_note_bytes')
            slug = params['threadId']
            self._session_root(slug)
            journal = self._journal({'method': 'inspect/note', 'threadId': slug},
                                    {'method': 'retract-note', 'threadId': slug})
            index = daemon.append_inspector_note(slug, text, journal)
            return {'appended': True, 'i': index, 'journalId': journal}
        if method == 'inspect/tighten':
            self._fields(params, ('approval',), ('approval',))
            current = self.config.tools.approval
            if params['approval'] != 'deny_risky' or current != 'ask':
                raise RpcError(-32602, 'Tighten only changes approval from ask to deny_risky')
            journal = self._journal({'method': 'inspect/tighten', 'approval': 'deny_risky'},
                                    {'method': 'set-approval', 'approval': current})
            daemon.set_approval('deny_risky')
            return {'approval': 'deny_risky', 'previous': current, 'journalId': journal}
        if method == 'inspect/companion-pause':
            self._fields(params, ())
            marker = Path(self.config.home) / 'run' / 'companions-paused.json'
            journal = self._journal({'method': 'inspect/companion-pause'}, {'method': 'companion-resume'})
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(json.dumps({'journalId': journal}) + '\n', encoding='utf-8')
            return {'paused': True, 'effective': 'next session start', 'journalId': journal}
        if method == 'inspect/revert':
            self._fields(params, ('journalId', 'operatorApproval'), ('journalId',))
            return self._revert(params)
        if method == 'inspect/ember-transition':
            self._fields(params, ('threadId', 'arguments'), ('threadId', 'arguments'))
            return daemon.ember_transition(params['threadId'], params['arguments'])
        raise RpcError(-32601, 'Method not found')

    def _revert(self, params):
        path = Path(self.config.home) / 'run' / 'inspector-journal.jsonl'
        match = None
        for record in committed_lines(path):
            if record.get('id') == params['journalId']:
                match = record
        if match is None:
            raise KeyError(params['journalId'])
        inverse = match['inverse']
        if inverse['method'] == 'retract-note':
            # Log lines are never deleted; a later note retracts the earlier one.
            journal = self._journal({'method': 'inspect/revert', 'of': match['id']}, {'method': 'none'})
            index = self.daemon.append_inspector_note(inverse['threadId'], 'Retracts inspector note ' + match['id'] + '.', journal)
            return {'reverted': True, 'i': index}
        if inverse['method'] == 'set-approval':
            # Restoring a looser policy is a loosen: the operator env must be present.
            name = self.settings.operator_approval_env
            expected = os.environ.get(name, '') if name else ''
            supplied = params.get('operatorApproval')
            if not expected or not isinstance(supplied, str) or not hmac.compare_digest(supplied.encode(), expected.encode()):
                raise PermissionError('Loosening approval requires the operator')
            self._journal({'method': 'inspect/revert', 'of': match['id']}, {'method': 'none'})
            self.daemon.set_approval(inverse['approval'])
            return {'reverted': True, 'approval': inverse['approval']}
        if inverse['method'] == 'companion-resume':
            self._journal({'method': 'inspect/revert', 'of': match['id']}, {'method': 'none'})
            (Path(self.config.home) / 'run' / 'companions-paused.json').unlink(missing_ok=True)
            return {'reverted': True, 'paused': False}
        raise RpcError(-32602, 'Journal entry has no reversible effect')
