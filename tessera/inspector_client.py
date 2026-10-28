"""Clients of the inspector socket: the CLI, proposals/apply/rollback and stdio MCP.

These run in the detached session's own process and home. They dial the
resident daemon's Unix socket; they never take the resident's locks, never spawn
app-server and never signal the resident.
"""

import difflib
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
import time
import uuid

from .inspector import offline_status, safe_source

WRITER_VERBS = {'note', 'tighten', 'pause-companions', 'revert', 'propose', 'apply', 'rollback'}


class InspectorError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class InspectorClient:
    def __init__(self, path, *, role='observer', approval=None, home=None, timeout=30, max_frame_bytes=8388608):
        self.path = str(path)
        self.max_frame_bytes = max_frame_bytes
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        self.sock.connect(self.path)
        self.stream = self.sock.makefile('rwb')
        self.next_id = 0
        params = {'clientInfo': {'name': 'tessera-inspect'}, 'role': role}
        if home is not None:
            params['clientHome'] = str(Path(home).expanduser().resolve())
        if approval is not None:
            params['operatorApproval'] = approval
        self.info = self.call('initialize', params)

    def call(self, method, params=None):
        self.next_id += 1
        frame = json.dumps({'jsonrpc': '2.0', 'id': self.next_id, 'method': method, 'params': params or {}}).encode() + b'\n'
        self.stream.write(frame)
        self.stream.flush()
        line = self.stream.readline(self.max_frame_bytes + 1)
        if not line or not line.endswith(b'\n'):
            raise InspectorError(-32000, 'Inspector connection closed')
        reply = json.loads(line)
        if 'error' in reply:
            raise InspectorError(reply['error']['code'], reply['error']['message'])
        return reply['result']

    def close(self):
        try:
            self.stream.close()
        finally:
            self.sock.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def _approval(config):
    name = config.inspector.operator_approval_env
    value = os.environ.get(name, '') if name else ''
    return value or None


# Proposals ----------------------------------------------------------------------

def _proposals(config):
    path = Path(config.home) / 'proposals'
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def _git_revision(root):
    head = Path(root) / '.git' / 'HEAD'
    try:
        value = head.read_text().strip()
        if value.startswith('ref: '):
            ref = Path(root) / '.git' / value[5:]
            return ref.read_text().strip() if ref.is_file() else None
        return value
    except OSError:
        return None


def _check_edit_path(config, source_root, resident_home, relative):
    base, target = safe_source(source_root, relative)
    name = Path(relative).name
    if name.endswith('.lock') or name == '.lock':
        raise ValueError('Proposals may not touch lock files')
    if resident_home is not None and target.is_relative_to(Path(resident_home).resolve()):
        raise ValueError('Proposals may not touch the resident home')
    if target.is_relative_to(Path(config.home).resolve()):
        raise ValueError('Proposals may not touch the inspector home')
    return base, target


def _secret_values(config):
    names = [config.aethermind.token_env, config.jev.api_key_env, config.companions.atlas.token_env,
             config.companions.ember.token_env, config.companions.ember.transition_authority_env,
             config.inspector.operator_approval_env, 'TESSERA_SERVER_TOKEN']
    for backend in config.backends.values():
        names.append(backend.api_key_env)
    return [os.environ[n] for n in names if n and os.environ.get(n)]


def propose(config, client, edits, *, scope=None, branch=None, resident_home=None):
    """Write a proposal record in the inspector home. Source is read, never written."""
    if not isinstance(edits, list) or not edits:
        raise ValueError('A proposal requires a nonempty edits list')
    remote_status = client.call('inspect/config')
    if not remote_status['inspector']['source_root_set']:
        raise ValueError('The resident has no source_root; proposals are unavailable')
    files, diff, seen = {}, [], set()
    secrets = _secret_values(config)
    for edit in edits:
        if not isinstance(edit, dict) or set(edit) != {'path', 'old', 'new'} or any(not isinstance(v, str) for v in edit.values()):
            raise ValueError('Each edit requires path, old and new text')
        name = edit['path']
        if name in seen or Path(name).is_absolute() or '..' in Path(name).parts or Path(name).name.endswith('.lock'):
            raise ValueError('Invalid or repeated proposal path')
        seen.add(name)
        if any(secret in edit['new'] or secret in edit['old'] for secret in secrets):
            raise ValueError('Proposal diff contains a credential value')
        current = client.call('inspect/code', {'path': name})
        source = current['text']
        if not edit['old'] or source.count(edit['old']) != 1:
            raise ValueError('Proposal anchor does not match the current source exactly once')
        updated = source.replace(edit['old'], edit['new'], 1)
        files[name] = current['sha256']
        diff.extend(difflib.unified_diff(source.splitlines(True), updated.splitlines(True), 'a/' + name, 'b/' + name))
    record = {'id': time.strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex[:8], 'created': time.time(),
              'source_root': client.call('inspect/status')['sourceRoot'], 'base_revision': None,
              'scope': list(scope or sorted(files)), 'diff': ''.join(diff), 'sha256': files, 'edits': edits,
              'branch': branch, 'state': 'proposed'}
    if record['source_root']:
        record['base_revision'] = _git_revision(record['source_root'])
    for name in files:
        _check_edit_path(config, record['source_root'], resident_home, name)
    path = _proposals(config) / (record['id'] + '.json')
    with path.open('x', encoding='utf-8') as stream:
        json.dump(record, stream, ensure_ascii=False, indent=2)
    return {'id': record['id'], 'path': str(path), 'files': sorted(files), 'diff_bytes': len(record['diff'].encode())}


def _load(config, proposal_id):
    if not isinstance(proposal_id, str) or '/' in proposal_id or proposal_id.startswith('.'):
        raise ValueError('Invalid proposal id')
    path = _proposals(config) / (proposal_id + '.json')
    return path, json.loads(path.read_text(encoding='utf-8'))


def _atomic_write(path, data):
    temporary = path.with_name('.' + path.name + '.' + uuid.uuid4().hex + '.tmp')
    temporary.write_bytes(data)
    os.replace(temporary, path)


def apply(config, client, proposal_id, *, resident_home=None):
    """Operator apply: snapshot first, exact anchors, never restart, never push."""
    if client.info.get('role') != 'editor':
        raise PermissionError('Apply requires the editor role with operator approval')
    path, record = _load(config, proposal_id)
    status = client.call('inspect/status')
    root = Path(record['source_root']).resolve()
    live = Path(status['livePackage']).resolve()
    if status['inFlight'] and (live.is_relative_to(root) or root.is_relative_to(live)):
        raise PermissionError('A resident turn is running and source_root is the live package image')
    prepared = {}
    for edit in record['edits']:
        _, target = _check_edit_path(config, str(root), resident_home, edit['path'])
        data = target.read_bytes()
        if hashlib.sha256(data).hexdigest() != record['sha256'][edit['path']]:
            raise ValueError('Source changed since the proposal; nothing was applied')
        text = data.decode('utf-8')
        if not edit['old'] or text.count(edit['old']) != 1:
            raise ValueError('Proposal anchor no longer matches exactly once; nothing was applied')
        prepared[edit['path']] = (target, data, text.replace(edit['old'], edit['new'], 1).encode('utf-8'))
    snapshot = path.with_suffix('.snapshot.json')
    with snapshot.open('x', encoding='utf-8') as stream:
        json.dump({'files': {name: old.decode('utf-8') for name, (_, old, _) in prepared.items()}}, stream)
        stream.flush()
        os.fsync(stream.fileno())
    for name, (target, _, new) in prepared.items():
        _atomic_write(target, new)
    record['state'] = 'applied'
    record['snapshot'] = snapshot.name
    _atomic_write(path, json.dumps(record, ensure_ascii=False, indent=2).encode())
    return {'applied': True, 'files': sorted(prepared), 'snapshot': str(snapshot), 'restarted': False}


def rollback(config, proposal_id, *, resident_home=None):
    path, record = _load(config, proposal_id)
    snapshot = path.with_suffix('.snapshot.json')
    files = json.loads(snapshot.read_text(encoding='utf-8'))['files']
    restored = {}
    for name, text in files.items():
        data = text.encode('utf-8')
        if hashlib.sha256(data).hexdigest() != record['sha256'].get(name):
            raise ValueError('Snapshot does not match the recorded sha256; nothing was restored')
        _, target = _check_edit_path(config, record['source_root'], resident_home, name)
        restored[name] = (target, data)
    for target, data in restored.values():
        _atomic_write(target, data)
    record['state'] = 'rolled_back'
    _atomic_write(path, json.dumps(record, ensure_ascii=False, indent=2).encode())
    return {'rolled_back': True, 'files': sorted(restored), 'restarted': False}


# CLI ------------------------------------------------------------------------------

def _resident_home(socket_path, explicit):
    if explicit:
        return Path(explicit).expanduser().resolve()
    path = Path(socket_path).expanduser().resolve()
    return path.parent.parent if path.parent.name == 'run' else None


def run_cli(config, args):
    resident = _resident_home(args.attach, args.resident_home)
    home = Path(config.home).resolve()
    if args.verb in WRITER_VERBS and resident is not None and home == resident:
        raise ValueError('The inspector home must differ from the resident home for writer verbs')
    if args.verb == 'rollback':
        return rollback(config, args.target, resident_home=resident)
    editor = args.verb in ('note', 'tighten', 'pause-companions', 'revert', 'apply')
    try:
        client = InspectorClient(args.attach, role='editor' if editor else 'observer',
                                 approval=_approval(config) if editor else None, home=home,
                                 max_frame_bytes=config.inspector.max_frame_bytes)
    except (FileNotFoundError, ConnectionRefusedError):
        if args.verb == 'status' and resident is not None:
            from .config import load_config
            try:
                remote = load_config(resident)
            except (OSError, ValueError):
                remote = None
            return offline_status(resident, remote)
        raise
    with client:
        verb, target = args.verb, args.target
        if verb == 'status':
            return client.call('inspect/status')
        if verb == 'sessions':
            return client.call('inspect/sessions')
        if verb == 'log':
            params = {'threadId': target, 'after': args.after, 'limit': args.limit} if args.limit else {'threadId': target, 'after': args.after}
            return client.call('inspect/log', params)
        if verb == 'events':
            return client.call('inspect/events', {'threadId': target, 'afterEventId': max(args.after, 0)})
        if verb == 'view':
            return client.call('inspect/view', {'threadId': target})
        if verb == 'zoom':
            return client.call('inspect/zoom', {'threadId': target, 'start': args.start, 'count': args.count})
        if verb == 'config':
            return client.call('inspect/config')
        if verb == 'code':
            return client.call('inspect/code', {'path': target})
        if verb == 'approvals':
            return client.call('inspect/approvals')
        if verb == 'note':
            return client.call('inspect/note', {'threadId': target, 'text': args.text})
        if verb == 'tighten':
            return client.call('inspect/tighten', {'approval': 'deny_risky'})
        if verb == 'pause-companions':
            return client.call('inspect/companion-pause')
        if verb == 'revert':
            params = {'journalId': target}
            if _approval(config):
                params['operatorApproval'] = _approval(config)
            return client.call('inspect/revert', params)
        if verb == 'propose':
            edits = json.loads(Path(args.edits).read_text(encoding='utf-8'))
            return propose(config, client, edits.get('edits') if isinstance(edits, dict) else edits,
                           branch=args.branch, resident_home=resident)
        if verb == 'apply':
            return apply(config, client, target, resident_home=resident)
    raise ValueError('Unknown inspect verb')


def add_parsers(commands):
    inspect = commands.add_parser('inspect', help='Attach to a resident daemon inspector socket (detached session)')
    inspect.add_argument('--attach', required=True, help='Resident inspector Unix socket path')
    inspect.add_argument('--resident-home', help='Resident home for offline status (default: inferred from run/<socket>)')
    inspect.add_argument('verb', choices=['status', 'sessions', 'log', 'events', 'view', 'zoom', 'config', 'code', 'approvals',
                                          'note', 'tighten', 'pause-companions', 'revert', 'propose', 'apply', 'rollback'])
    inspect.add_argument('target', nargs='?', help='Session slug, code path, journal id or proposal id')
    inspect.add_argument('text', nargs='?', help='Note text')
    inspect.add_argument('--after', type=int, default=-1)
    inspect.add_argument('--limit', type=int)
    inspect.add_argument('--start', type=int, default=0)
    inspect.add_argument('--count', type=int, default=1)
    inspect.add_argument('--edits', help='JSON file with an edits list of {path, old, new}')
    inspect.add_argument('--branch', help='Optional local branch name recorded in the proposal')
    mcp = commands.add_parser('inspect-mcp', help='Serve inspector tools as MCP on this process stdio (no listener)')
    mcp.add_argument('--attach', required=True)


# MCP on stdio -------------------------------------------------------------------

MCP_TOOLS = {
    'inspect_status': ('inspect/status', {}),
    'inspect_sessions': ('inspect/sessions', {}),
    'inspect_log': ('inspect/log', {'threadId': 'string', 'after': 'integer', 'limit': 'integer'}),
    'inspect_events': ('inspect/events', {'threadId': 'string', 'afterEventId': 'integer', 'limit': 'integer'}),
    'inspect_view': ('inspect/view', {'threadId': 'string'}),
    'inspect_zoom': ('inspect/zoom', {'threadId': 'string', 'start': 'integer', 'count': 'integer'}),
    'inspect_config': ('inspect/config', {}),
    'inspect_code': ('inspect/code', {'path': 'string'}),
    'inspect_approvals': ('inspect/approvals', {}),
}


def serve_mcp(config, attach, source=None, output=None):
    """MCP JSON-RPC on this process's own stdio; it dials the socket and binds nothing."""
    source = source or sys.stdin
    output = output or sys.stdout
    client = InspectorClient(attach, home=config.home, max_frame_bytes=config.inspector.max_frame_bytes)

    def send(message):
        output.write(json.dumps({'jsonrpc': '2.0', **message}) + '\n')
        output.flush()
    with client:
        for line in source:
            try:
                request = json.loads(line)
            except ValueError:
                send({'id': None, 'error': {'code': -32700, 'message': 'Invalid JSON'}})
                continue
            if 'id' not in request:
                continue
            method, params = request.get('method'), request.get('params') or {}
            if method == 'initialize':
                send({'id': request['id'], 'result': {'protocolVersion': params.get('protocolVersion', '2025-03-26'),
                      'capabilities': {'tools': {}}, 'serverInfo': {'name': 'tessera-inspect', 'version': '1'}}})
            elif method == 'tools/list':
                tools = []
                for name, (_, props) in MCP_TOOLS.items():
                    tools.append({'name': name, 'description': 'Read-only resident inspection (' + name[8:] + ').',
                                  'inputSchema': {'type': 'object', 'properties': {k: {'type': v} for k, v in props.items()}}})
                send({'id': request['id'], 'result': {'tools': tools}})
            elif method == 'tools/call' and params.get('name') in MCP_TOOLS:
                try:
                    result = client.call(MCP_TOOLS[params['name']][0], params.get('arguments') or {})
                    send({'id': request['id'], 'result': {'content': [{'type': 'text', 'text': json.dumps(result)}]}})
                except InspectorError as error:
                    send({'id': request['id'], 'result': {'isError': True, 'content': [{'type': 'text', 'text': str(error)}]}})
            else:
                send({'id': request['id'], 'error': {'code': -32601, 'message': 'Method not found'}})
    return 0
