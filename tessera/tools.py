"""Tool schemas, allowlists, approval checks, and the built-in local tools.

This registry controls approvals. It is not a process or filesystem sandbox.
"""

from contextlib import contextmanager
from .http_cancel import CancellableHTTP
from dataclasses import dataclass
from datetime import datetime, timezone
import base64
import fnmatch
import html
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess

from .procutil import kill_group, popen, terminate_group
import tempfile
import threading
import urllib.error
import urllib.request
import uuid


def clip_output(text, limit=30000):
    """Clip with a complete notice; tiny limits allow one character plus notice."""
    if len(text) <= limit:
        return text
    marker = '\n[clipped]\n'
    remaining = max(1, limit - len(marker))
    head = (remaining + 1) // 2
    tail = remaining - head
    return text[:head] + marker + (text[-tail:] if tail else '')


class WorkingDirectory:
    """The session checkout. Tools read ``path`` when they run, so it can change mid-session."""

    def __init__(self, path):
        self.set(path)

    def set(self, path):
        if not isinstance(path, (str, Path)):
            raise ValueError('workspace must be a path')
        text = str(path)
        if not text or '\x00' in text:
            raise ValueError('workspace must be a path')
        self.path = Path(text).expanduser().resolve()
        return self.path


_INACTIVE_WORD = re.compile(r'(?i)\b(?:inactive|superseded|thread-replaced)\b')
_PROSE_WORD = re.compile(r'[A-Za-z]{3,}')
_INACTIVE_STOP = {'inactive', 'superseded', 'thread', 'replaced', 'the', 'and', 'for', 'id', 'ids'}
_TOKEN = re.compile(r'[A-Za-z0-9_.:/-]+')
# Whole tokens only. Letter runs inside a hex id are not prose, or a real chain is kept.
_ID_TOKEN = re.compile(r'(?i)^(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{6,}|\d{3,8})$')
_LABELED_HEAD = re.compile(r'(?i)\b([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{6,}|\d{3,8})\s*\(([^)]{1,80})\)')


def _inactive_chain(line):
    """An id chain marked inactive, superseded, or thread-replaced, not a sentence."""
    if not _INACTIVE_WORD.search(line):
        return False
    prose = []
    for token in _TOKEN.findall(line):
        if _ID_TOKEN.match(token) or token.lower() in _INACTIVE_STOP or _INACTIVE_WORD.fullmatch(token):
            continue
        prose.extend(word for word in _PROSE_WORD.findall(token) if word.lower() not in _INACTIVE_STOP)
    return len(prose) <= 1


def _id_chain_line(line):
    tokens = _TOKEN.findall(line)
    if len(tokens) < 3:
        return False
    ids = [token for token in tokens if _ID_TOKEN.match(token)]
    return len(ids) >= 3 and len(ids) * 2 >= len(tokens)


def present_id_text(text):
    """Drop inactive id chains. Bare active-head lists become a count or short labels."""
    if not isinstance(text, str) or not text:
        return '' if not isinstance(text, str) else text
    kept = []
    for line in text.split('\n'):
        if _inactive_chain(line):
            continue
        if not _id_chain_line(line):
            kept.append(line)
            continue
        labeled = []
        for match in _LABELED_HEAD.finditer(line):
            label = ' '.join(match.group(2).split())
            if label and label.lower() not in _INACTIVE_STOP:
                labeled.append(f'{match.group(1)} / {label}')
        if labeled:
            shown = labeled[:8]
            extra = len(labeled) - len(shown)
            block = 'Active heads:\n' + '\n'.join(shown)
            if extra:
                block += f'\nmore: {extra}, page with memory_recall'
            kept.append(block)
        else:
            count = sum(1 for token in _TOKEN.findall(line) if _ID_TOKEN.match(token))
            kept.append(f'Active heads: {count}')
    return '\n'.join(kept)


def _envelope(value):
    if not isinstance(value, dict):
        return None
    inner = value.get('envelope')
    if isinstance(inner, dict) and ('currentness' in inner or 'native' in inner):
        return inner
    native = value.get('native')
    if 'currentness' in value or (isinstance(native, dict) and 'kept' in native):
        return value
    return None


def _record_block(record):
    if not isinstance(record, dict):
        return ''
    ident = str(record.get('id') or '').strip()
    kind = str(record.get('type') or 'record').strip()
    author = str(record.get('author') or '').strip()
    head = ' / '.join(part for part in (ident, kind, author) if part)
    parts = [head] if head else []
    ctx = record.get('ctx')
    if isinstance(ctx, str) and ctx.strip() and not _is_only_id(ctx) and not _id_chain_line(ctx):
        parts.append(ctx.strip().split('\n', 1)[0][:160])
    body = record.get('body')
    if isinstance(body, str) and body.strip():
        parts.append(present_id_text(body.strip()))
    return '\n'.join(part for part in parts if part)


def _is_only_id(text):
    return bool(_ID_TOKEN.match(text.strip()))


def _inactive_count(value):
    if isinstance(value, dict):
        return len(value)
    if isinstance(value, list):
        return len(value)
    return 0


def present_inheritance(value):
    """Project inheritance for the model: labeled records, no inactive or bare id chains."""
    if isinstance(value, str):
        stripped = value.strip()
        if stripped[:1] in '{[':
            try:
                value = json.loads(stripped)
            except ValueError:
                return present_id_text(value)
        else:
            return present_id_text(value)
    envelope = _envelope(value) if isinstance(value, dict) else None
    if envelope is None:
        if isinstance(value, dict):
            return present_id_text(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str))
        return present_id_text('' if value is None else str(value))
    blocks = []
    native = envelope.get('native') if isinstance(envelope.get('native'), dict) else {}
    for key in ('kept', 'must_know'):
        rows = native.get(key)
        if isinstance(rows, list):
            for record in rows:
                block = _record_block(record)
                if block:
                    blocks.append(block)
    current = envelope.get('currentness') if isinstance(envelope.get('currentness'), dict) else {}
    active = current.get('active_ids')
    if isinstance(active, list) and active:
        blocks.append(f'Active heads: {len(active)}')
    omitted = _inactive_count(current.get('inactive'))
    if omitted:
        blocks.append(f'Inactive records omitted: {omitted}')
    return '\n\n'.join(blocks)


def drop_inactive_chains(text):
    """Drop injected lines that are only inactive or superseded ids."""
    if not isinstance(text, str) or not text:
        return '' if text is None else text
    return '\n'.join(line for line in text.split('\n') if not _inactive_chain(line))


def _records(text):
    if '\n\n' in text:
        return [record for record in text.split('\n\n') if record.strip()], '\n\n'
    return [record for record in text.split('\n') if record.strip()], '\n'


def clip_records(text, limit, page_with, offset=0):
    """Keep whole records that fit. The paging line is extra, so it never steals a record."""
    text = drop_inactive_chains(text if isinstance(text, str) else '' if text is None else str(text))
    limit = max(1, int(limit))
    offset = max(0, int(offset))
    records, joiner = _records(text)
    pending = records[offset:]
    if not pending:
        return ''
    kept = []
    used = 0
    for record in pending:
        extra = len(joiner) if kept else 0
        if used + extra + len(record) <= limit:
            kept.append(record)
            used += extra + len(record)
            continue
        break
    omitted = len(pending) - len(kept)
    if kept:
        body = joiner.join(kept)
        if omitted:
            body += f'\nmore: {omitted}, page with {page_with}'
        return body
    # One record is longer than the page. Keep whole words only.
    cut = pending[0][:limit]
    if len(pending[0]) > limit:
        if ' ' not in cut:
            cut = ''
        else:
            cut = cut.rsplit(' ', 1)[0]
    omitted = len(pending) - (1 if cut and cut == pending[0] else 0)
    if cut and cut != pending[0]:
        omitted = len(pending)
    marker = f'more: {omitted}, page with {page_with}'
    if not omitted:
        return cut
    return (cut + '\n' + marker) if cut else marker


# yolo is the operator-facing switch: like never, every tool runs without approval.
APPROVAL_POLICIES = ('ask', 'never', 'deny_risky', 'yolo')
AUTO_APPROVE = ('never', 'yolo')


class ApprovalRefused(PermissionError):
    """A risky tool was refused by the registry's approval policy.

    It subclasses PermissionError because every existing caller and test treats a
    refusal as one. The distinct type exists so the turn loop can tell a refusal
    apart from a tool that merely failed: a refusal means the side effect never
    happened, which is what `blocked` reports. Swallowing both into the generic
    `except Exception` arm made a prevented deletion read as a completed one.
    """

    def __init__(self, name, policy):
        self.tool = name
        self.policy = policy
        super().__init__(f'Approval required for {name} (policy: {policy})')


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    handler: object
    risky: bool = False
    # builtin, mcp, or session. The shelf page reads this; it does not decide whether a call runs.
    origin: str = 'builtin'


def approval_rule(name, classes, rules):
    """allow or gate for a tool name; a rule naming the tool wins over its class rule."""
    if name in rules:
        return rules[name]
    for cls, patterns in classes.items():
        if cls in rules and any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns):
            return rules[cls]
    return 'gate'


class _Policy:
    def __init__(self, approval):
        self.approval = approval


class ToolRegistry:
    # policy is any object whose .approval attribute is the one live mode (a session passes its
    # ToolsConfig), so every registry and restricted child enforces whatever the switch says now.
    def __init__(self, approval='ask', approve=None, policy=None):
        # policy_of, when set, is the live owner: () -> current ToolsConfig.
        # Descendants keep this callable instead of a copied ToolsConfig, so a
        # later off switch or Safe/YOLO change is what the next call enforces.
        self.policy_of = None
        self.policy = policy if policy is not None else _Policy(approval)
        if self.policy.approval not in APPROVAL_POLICIES:
            raise ValueError('Invalid approval policy')
        self.approve = approve
        # Optional defer(name, arguments, run): turns a refused risky call into an operator request.
        self.defer = None
        # Optional allowed(name) -> bool: tools that skip approval under ask and deny_risky.
        self.allowed = None
        self._tools = {}
        # Optional () -> names the operator turned off. Read on each offer and each call,
        # so a config reload reaches the next turn without rebuilding the registry.
        self.disabled_of = None
        self.cancelled = threading.Event()
        self._cancel_lock = threading.RLock()
        self._cancel_callbacks = set()
        self._cancel_generation = [0]

    @contextmanager
    def cancellation(self, callback):
        with self._cancel_lock:
            self.check_cancelled()
            self._cancel_callbacks.add(callback)
        try:
            yield
        finally:
            with self._cancel_lock:
                self._cancel_callbacks.discard(callback)

    def check_cancelled(self):
        if self.cancelled.is_set():
            raise RuntimeError('Tool cancelled by operator')

    def reset_cancel(self):
        with self._cancel_lock:
            self.cancelled.clear()

    def cancel(self):
        with self._cancel_lock:
            self.cancelled.set()
            self._cancel_generation[0] += 1
            callbacks = list(self._cancel_callbacks)
        for callback in callbacks:
            try:
                callback()
            except (OSError, ProcessLookupError):
                pass

    @property
    def policy(self):
        provider = self.policy_of
        if provider is not None:
            live = provider()
            if live is not None:
                return live
        return self._policy

    @policy.setter
    def policy(self, value):
        self._policy = value

    @property
    def approval(self):
        return self.policy.approval

    @approval.setter
    def approval(self, value):
        if value not in APPROVAL_POLICIES:
            raise ValueError('Invalid approval policy')
        self.policy.approval = value

    def register(self, name, description, parameters, handler, risky=False, origin='builtin'):
        if name in self._tools:
            raise ValueError(f'Tool already registered: {name}')
        self._tools[name] = Tool(name, description, parameters, handler, risky, origin)
        return handler

    def _disabled_names(self):
        # The operator's off list applies in every approval mode, including yolo.
        # YOLO skips the approval prompt for a tool that is still on.
        provider = self.disabled_of
        raw = provider() if provider is not None else getattr(self.policy, 'disabled', ())
        if not isinstance(raw, (list, tuple, set)):
            return set()
        return {item for item in raw if isinstance(item, str) and item}

    def schemas(self):
        disabled = self._disabled_names()
        schemas = []
        for tool in self._tools.values():
            if tool.name in disabled:
                continue
            schemas.append({'type': 'function', 'function': {'name': tool.name, 'description': tool.description, 'parameters': tool.parameters}})
        return schemas

    def names(self):
        # The set of tools this registry actually holds. A child inherits this as its
        # ceiling: delegation must not widen capability, only narrow it.
        return list(self._tools)

    def restricted(self, names):
        child = ToolRegistry(approve=self.approve, policy=self._policy)
        child.policy_of = self.policy_of
        child.defer = self.defer
        child.allowed = self.allowed
        child.disabled_of = self.disabled_of
        child.cancelled = self.cancelled
        child._cancel_lock = self._cancel_lock
        child._cancel_callbacks = self._cancel_callbacks
        child._cancel_generation = self._cancel_generation
        for name in names:
            if name not in self._tools:
                raise ValueError(f'Unknown tool: {name}')
            child._tools[name] = self._tools[name]
        return child

    def execute(self, name, arguments):
        self.check_cancelled()
        if name not in self._tools:
            raise ValueError(f'Unknown tool: {name}')
        if name in self._disabled_names():
            raise ValueError(f'Tool disabled: {name}')
        tool = self._tools[name]
        self._validate(arguments, tool.parameters, 'arguments')
        if tool.risky and self.approval not in AUTO_APPROVE and not (self.allowed and self.allowed(name)):
            accepted = self.approval == 'ask' and self.approve and self.approve(name, arguments)
            if not accepted and self.defer is not None:
                generation = self._cancel_generation[0]
                def approved_run():
                    self.check_cancelled()
                    if generation != self._cancel_generation[0]:
                        raise RuntimeError('Tool cancelled by operator')
                    return tool.handler(**arguments)
                return self.defer(name, arguments, approved_run)
            if not accepted:
                raise ApprovalRefused(name, self.approval)
        self.check_cancelled()
        return tool.handler(**arguments)

    @classmethod
    def _validate(cls, value, schema, label):
        kind = schema.get('type')
        types = {'string': str, 'object': dict, 'array': list, 'number': (int, float), 'integer': int, 'boolean': bool}
        if kind in types:
            if not isinstance(value, types[kind]) or (kind in ('integer', 'number') and isinstance(value, bool)):
                raise ValueError(f'Invalid {label}: expected {kind}')
        if 'enum' in schema and value not in schema['enum']:
            raise ValueError(f'Invalid {label}: unsupported value')
        if kind in ('number', 'integer') and 'minimum' in schema and value < schema['minimum']:
            raise ValueError(f'Invalid {label}: below minimum')
        if kind == 'object':
            properties = schema.get('properties', {})
            for name in schema.get('required', []):
                if name not in value:
                    raise ValueError(f'Missing argument: {name}')
            for name, item in value.items():
                if name in properties:
                    cls._validate(item, properties[name], f'argument {name}')
                elif schema.get('additionalProperties') is False:
                    raise ValueError(f'Unknown argument: {name}')
        if kind == 'array' and 'items' in schema:
            for item in value:
                cls._validate(item, schema['items'], label)


def object_schema(properties, required=()):
    return {'type': 'object', 'properties': properties, 'required': list(required), 'additionalProperties': False}


def _popen_is_real():
    popen = subprocess.Popen
    return getattr(popen, '__module__', '') == 'subprocess' and getattr(popen, '__name__', '') == 'Popen'


def _git_dir(cwd):
    path = Path(cwd) / '.git'
    try:
        if path.is_dir():
            return path
        if path.is_file():
            text = path.read_text(encoding='utf-8', errors='replace').strip()
            if text.startswith('gitdir:'):
                raw = text.split(':', 1)[1].strip()
                located = Path(raw)
                if not located.is_absolute():
                    located = (path.parent / located).resolve()
                if located.is_dir():
                    return located
    except OSError:
        return None
    return None


def _read_git_head(git_dir):
    try:
        head = (git_dir / 'HEAD').read_text(encoding='utf-8', errors='replace').strip()
    except OSError:
        return 'unavailable'
    if not head.startswith('ref:'):
        return head or 'unavailable'
    ref_name = head.split(':', 1)[1].strip()
    bases = [git_dir]
    common = git_dir / 'commondir'
    try:
        if common.is_file():
            raw = common.read_text(encoding='utf-8', errors='replace').strip()
            located = Path(raw)
            if not located.is_absolute():
                located = (git_dir / located).resolve()
            bases.append(located)
    except OSError:
        pass
    for base in bases:
        try:
            ref = base / ref_name
            if ref.is_file():
                value = ref.read_text(encoding='utf-8', errors='replace').strip()
                if value:
                    return value
        except OSError:
            pass
    return 'unavailable'


_SKIP_DIRS = {'.git', 'node_modules', '__pycache__', '.venv'}


def _apply_hunks(original, hunk_lines):
    lines = original.split('\n')
    trailing = original.endswith('\n')
    if trailing and lines and lines[-1] == '':
        lines = lines[:-1]
    groups = []
    current = []
    for line in hunk_lines:
        if line.startswith('@@'):
            if current:
                groups.append(current)
            current = []
            continue
        current.append(line)
    if current:
        groups.append(current)
    for group in groups:
        old, new = [], []
        for line in group:
            if line.startswith('+'):
                new.append(line[1:])
            elif line.startswith('-'):
                old.append(line[1:])
            else:
                context = line[1:] if line.startswith(' ') else line
                old.append(context)
                new.append(context)
        if not old and not new:
            continue
        found = None
        for index in range(len(lines) - len(old) + 1):
            if lines[index:index + len(old)] == old:
                found = index
                break
        if found is None:
            raise ValueError('hunk did not match')
        lines[found:found + len(old)] = new
    text = '\n'.join(lines)
    if trailing:
        text += '\n'
    return text


def _patch_files(text):
    """Parse a *** Begin Patch block or a unified diff into file operations."""
    body = text.replace('\r\n', '\n').strip('\n')
    if body.startswith('*** Begin Patch'):
        lines = body.split('\n')
        if lines[0].strip() != '*** Begin Patch' or lines[-1].strip() != '*** End Patch':
            raise ValueError('patch must start with *** Begin Patch and end with *** End Patch')
        files = []
        index = 1
        while index < len(lines) - 1:
            header = lines[index].strip()
            index += 1
            if header.startswith('*** Add File:'):
                path = header.split(':', 1)[1].strip()
                added = []
                while index < len(lines) - 1 and not lines[index].startswith('*** '):
                    row = lines[index]
                    added.append(row[1:] if row.startswith('+') else row)
                    index += 1
                files.append(('add', path, '\n'.join(added) + ('\n' if added else '')))
            elif header.startswith('*** Delete File:'):
                files.append(('delete', header.split(':', 1)[1].strip(), None))
            elif header.startswith('*** Update File:'):
                path = header.split(':', 1)[1].strip()
                hunk = []
                while index < len(lines) - 1 and not lines[index].startswith('*** '):
                    hunk.append(lines[index])
                    index += 1
                files.append(('update', path, hunk))
            else:
                raise ValueError('unsupported patch header')
        return files
    if body.startswith('--- ') or body.startswith('diff '):
        files = []
        current = None
        hunk = []
        for line in body.split('\n'):
            if line.startswith('+++ '):
                path = line[4:].strip()
                if path.startswith('b/'):
                    path = path[2:]
                if current:
                    files.append(current)
                current = ('update', path, [])
                hunk = current[2]
            elif current and (line.startswith('@@') or line[:1] in (' ', '+', '-', '\\')):
                if not line.startswith('\\'):
                    hunk.append(line)
        if current:
            files.append(current)
        if not files:
            raise ValueError('unified diff has no files')
        return files
    raise ValueError('patch must be a *** Begin Patch block or a unified diff')


def _extract_text(body, content_type):
    if 'html' not in content_type.lower():
        return body
    stripped = re.sub(r'(?is)<(script|style)\b.*?>.*?</\1>', ' ', body)
    stripped = re.sub(r'(?s)<[^>]+>', ' ', stripped)
    return re.sub(r'\s+', ' ', html.unescape(stripped)).strip()


def _image_kind(data):
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    if data.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    if data.startswith(b'GIF87a') or data.startswith(b'GIF89a'):
        return 'image/gif'
    if data.startswith(b'RIFF') and data[8:12] == b'WEBP':
        return 'image/webp'
    return ''


class _Processes:
    def __init__(self, spill):
        self.spill = spill
        self._items = {}
        self._lock = threading.Lock()

    def cancel(self):
        with self._lock:
            items = list(self._items.values())
        for item in items:
            process = item['process']
            if process.poll() is None and _popen_is_real():
                try:
                    kill_group(process.pid)
                except ProcessLookupError:
                    pass

    def close(self):
        with self._lock:
            items = list(self._items.values())
        for item in items:
            self._stop(item)

    def _stop(self, item):
        process = item['process']
        if process.poll() is None and _popen_is_real():
            try:
                terminate_group(process.pid)
                process.wait(timeout=1)
            except (ProcessLookupError, subprocess.TimeoutExpired, OSError):
                try:
                    kill_group(process.pid)
                except (ProcessLookupError, OSError):
                    pass
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    pass
        log = item.get('log')
        if log and not log.closed:
            log.close()

    def start(self, command, cwd, env, spill_dir):
        proc_id = uuid.uuid4().hex[:8]
        path = spill_dir / f'process-{proc_id}.log'
        log = path.open('w+b')
        process = popen(command, shell=True, cwd=cwd, env=env, stdin=subprocess.PIPE,
                        stdout=log, stderr=subprocess.STDOUT)
        with self._lock:
            self._items[proc_id] = {'process': process, 'log': log, 'path': path, 'command': command}
        return {'id': proc_id, 'pid': process.pid, 'output_path': str(path)}

    def get(self, proc_id):
        with self._lock:
            item = self._items.get(proc_id)
        if item is None:
            raise ValueError('unknown process')
        return item

    def poll(self, proc_id, tail_chars):
        item = self.get(proc_id)
        process = item['process']
        code = process.poll()
        log = item['log']
        log.flush()
        log.seek(0, os.SEEK_END)
        size = log.tell()
        log.seek(max(0, size - tail_chars))
        output = log.read().decode('utf-8', errors='replace')
        return {'id': proc_id, 'running': code is None, 'exit_code': code, 'output': output,
                'output_path': str(item['path'])}

    def kill(self, proc_id):
        item = self.get(proc_id)
        self._stop(item)
        return {'id': proc_id, 'killed': True, 'exit_code': item['process'].poll()}

    def write(self, proc_id, text):
        item = self.get(proc_id)
        process = item['process']
        if process.poll() is not None or process.stdin is None:
            return 'process is not accepting stdin'
        process.stdin.write(text.encode('utf-8'))
        process.stdin.flush()
        return f'wrote {len(text)} characters to {proc_id}'


def build_registry(cwd=None, view=None, spawn=None, approval='ask', approve=None, clip_chars=30000, timeout=30, parent_view=None,
                   policy=None, agent_wait=None, file_reader=None):
    if not isinstance(cwd, WorkingDirectory):
        cwd = WorkingDirectory(cwd or Path.cwd())
    root = cwd
    list_limit = getattr(policy, 'list_limit', 200) if policy is not None else 200
    grep_limit = getattr(policy, 'grep_limit', 40) if policy is not None else 40
    grep_scan = getattr(policy, 'grep_scan', 2000) if policy is not None else 2000
    git_log_limit = getattr(policy, 'git_log_limit', 20) if policy is not None else 20
    spill_name = getattr(policy, 'spill_dir', 'tool-output') if policy is not None else 'tool-output'
    tail_chars = getattr(policy, 'output_tail_chars', 4000) if policy is not None else 4000
    read_line_limit = getattr(policy, 'read_line_limit', 400) if policy is not None else 400
    glob_limit = getattr(policy, 'glob_limit', 200) if policy is not None else 200
    read_bytes_limit = getattr(policy, 'read_bytes_limit', 1000000) if policy is not None else 1000000
    image_bytes_limit = getattr(policy, 'image_bytes_limit', 5000000) if policy is not None else 5000000
    fetch_max_bytes = getattr(policy, 'fetch_max_bytes', 1000000) if policy is not None else 1000000
    fetch_timeout = getattr(policy, 'fetch_timeout', 20.0) if policy is not None else 20.0
    shell_max_timeout = getattr(policy, 'shell_max_timeout', 600.0) if policy is not None else 600.0
    registry = ToolRegistry(approval, approve, policy)
    processes = _Processes(spill_name)
    registry._cancel_callbacks.add(processes.cancel)

    def resolve(path):
        path = Path(path).expanduser()
        if not path.is_absolute():
            path = root.path / path
        return path

    def spill_dir():
        path = Path(spill_name).expanduser()
        if not path.is_absolute():
            path = root.path / path
        path.mkdir(parents=True, exist_ok=True)
        return path

    def environment(env):
        if env is None:
            return None
        if not isinstance(env, dict) or any(not isinstance(key, str) or not isinstance(value, str) for key, value in env.items()):
            raise ValueError('env must be a map of strings')
        merged = os.environ.copy()
        merged.update(env)
        return merged

    def shell(command, cwd=None, timeout=timeout, env=None, stdin=None):
        if timeout <= 0:
            raise ValueError('timeout must be positive')
        timeout = min(float(timeout), float(shell_max_timeout))
        working_dir = resolve(cwd) if cwd else root.path
        with tempfile.TemporaryFile() as output:
            def cancel_process():
                try:
                    kill_group(process.pid)
                except ProcessLookupError:
                    pass

            with registry._cancel_lock:
                registry.check_cancelled()
                process = popen(command, shell=True, cwd=working_dir, env=environment(env),
                                stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                                stdout=output, stderr=subprocess.STDOUT)
                registry._cancel_callbacks.add(cancel_process)
            timed_out = False
            try:
                if stdin is not None:
                    process.stdin.write(stdin.encode('utf-8'))
                    process.stdin.close()
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                try:
                    kill_group(process.pid)
                except ProcessLookupError:
                    pass
                process.wait()
            except BaseException:
                # A shell starts in its own process group, so Ctrl-C in the
                # caller must explicitly terminate the entire command tree.
                try:
                    kill_group(process.pid)
                except ProcessLookupError:
                    pass
                process.wait()
                raise
            finally:
                with registry._cancel_lock:
                    registry._cancel_callbacks.discard(cancel_process)
            output.seek(0)
            text = output.read().decode('utf-8', errors='replace')
        result = {'exit_code': process.returncode, 'timed_out': timed_out, 'output': text}
        if len(text) > clip_chars:
            path = spill_dir() / f'shell-{uuid.uuid4().hex[:8]}.log'
            path.write_text(text, encoding='utf-8')
            shown = clip_output(text, clip_chars)
            notice = f'\nfull output: {path} ({len(text.encode("utf-8"))} bytes)'
            if len(shown) + len(notice) <= clip_chars:
                shown += notice
            result['output'] = shown
            result['output_path'] = str(path)
        return result

    def file_bytes(path):
        target = resolve(path)
        if file_reader is not None:
            return file_reader(target)
        return target.read_bytes()

    def read_file(path, offset=0, limit=clip_chars, start_line=None, end_line=None):
        data = file_bytes(path)
        try:
            text = data.decode('utf-8')
        except UnicodeDecodeError:
            return 'unavailable: file is not UTF-8 text; use read_bytes'
        if start_line is not None:
            rows = text.splitlines()
            start = int(start_line)
            if start < 1:
                raise ValueError('start_line must be >= 1')
            end = int(end_line) if end_line is not None else min(len(rows), start + read_line_limit - 1)
            if end < start:
                raise ValueError('end_line must be >= start_line')
            chosen = rows[start - 1:end]
            body = '\n'.join(f'{start + index}|{line}' for index, line in enumerate(chosen))
            if end < len(rows):
                body += f'\nmore lines: {len(rows) - end}, page with read_file'
            return body
        return clip_output(text[offset:], limit)

    def read_bytes(path, limit=None):
        data = file_bytes(path)
        cap = read_bytes_limit if limit is None else min(int(limit), read_bytes_limit)
        shown = data[:cap]
        text = base64.b64encode(shown).decode('ascii')
        if len(data) > cap:
            text += f'\ntruncated: {len(data)} bytes, showing {cap}'
        return text

    def read_image(path):
        data = file_bytes(path)
        if len(data) > image_bytes_limit:
            return f'unavailable: image exceeds {image_bytes_limit} bytes'
        kind = _image_kind(data)
        if not kind:
            return 'unavailable: not a png, jpeg, gif, or webp image'
        encoded = base64.b64encode(data).decode('ascii')
        return {'bytes': len(data), 'media_type': kind,
                'image': {'caption': str(path), 'data_url': f'data:{kind};base64,{encoded}'}}

    def write_file(path, text):
        target = resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding='utf-8')
        return f'Wrote {len(text.encode("utf-8"))} bytes to {path}'

    def edit_file(path, old, new):
        target = resolve(path)
        text = target.read_text(encoding='utf-8')
        if not old or text.count(old) != 1:
            raise ValueError('old text must occur exactly once')
        target.write_text(text.replace(old, new, 1), encoding='utf-8')
        return f'Edited {path}'

    def apply_patch(patch):
        summary = []
        for kind, path, payload in _patch_files(patch):
            target = resolve(path)
            if kind == 'add':
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(payload, encoding='utf-8')
                summary.append(f'added {path}')
            elif kind == 'delete':
                if target.is_dir():
                    raise ValueError('patch delete expects a file')
                target.unlink()
                summary.append(f'deleted {path}')
            else:
                updated = _apply_hunks(target.read_text(encoding='utf-8'), payload)
                target.write_text(updated, encoding='utf-8')
                summary.append(f'Updated {path}')
        if not summary:
            raise ValueError('patch is empty')
        return '; '.join(summary)

    def glob_paths(pattern, path='.', limit=glob_limit):
        if not isinstance(pattern, str) or not pattern or '\x00' in pattern:
            raise ValueError('pattern required')
        base = resolve(path)
        if not base.is_dir():
            raise ValueError('not a directory')
        limit = max(1, min(int(limit), 2000))
        found = []
        overflow = 0
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(name for name in dirnames if name not in _SKIP_DIRS)
            for name in sorted(filenames):
                candidate = Path(dirpath) / name
                relative = str(candidate.relative_to(base))
                if not fnmatch.fnmatch(relative, pattern) and not fnmatch.fnmatch(name, pattern):
                    continue
                if len(found) >= limit:
                    overflow += 1
                    continue
                found.append(relative)
        text = '\n'.join(found)
        if overflow:
            text += f'\nmore: {overflow}, page with glob'
        return text

    def move_path(src, dest):
        source = resolve(src)
        target = resolve(dest)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))
        return f'Moved {src} to {dest}'

    def copy_path(src, dest):
        source = resolve(src)
        target = resolve(dest)
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target, dirs_exist_ok=True)
        else:
            shutil.copy2(source, target)
        return f'Copied {src} to {dest}'

    def delete_path(path, recursive=False):
        target = resolve(path)
        if not target.exists():
            raise ValueError('path does not exist')
        if target.is_dir():
            if recursive:
                shutil.rmtree(target)
            else:
                target.rmdir()
        else:
            target.unlink()
        return f'Deleted {path}'

    def mkdir(path):
        resolve(path).mkdir(parents=True, exist_ok=True)
        return f'Created {path}'

    def process(action, command=None, id=None, stdin=None, cwd=None, env=None):
        if action == 'start':
            if not command:
                raise ValueError('command required')
            with registry._cancel_lock:
                registry.check_cancelled()
                return processes.start(command, resolve(cwd) if cwd else root.path, environment(env), spill_dir())
        if not id:
            raise ValueError('id required')
        if action == 'poll':
            return processes.poll(id, min(tail_chars, clip_chars))
        if action == 'kill':
            return processes.kill(id)
        if action == 'write':
            if stdin is None:
                raise ValueError('stdin required')
            return processes.write(id, stdin)
        raise ValueError('unsupported process action')

    def web_fetch(url, headers=None):
        if not isinstance(url, str) or not url.startswith(('http://', 'https://')) or len(url) > 8000:
            raise ValueError('url must be an http or https URL')
        extra = {}
        if headers:
            if not isinstance(headers, dict):
                raise ValueError('headers must be a map of strings')
            for key, value in headers.items():
                if not isinstance(key, str) or not isinstance(value, str) or '\n' in key or '\r' in key or '\n' in value or '\r' in value:
                    raise ValueError('invalid header')
                extra[key] = value
        request = urllib.request.Request(url, headers=extra, method='GET')
        transport = CancellableHTTP()
        transport.cancel_event = registry.cancelled
        try:
            with registry.cancellation(transport.cancel), transport.opener.open(request, timeout=fetch_timeout) as response:
                transport.response = response
                transport.check_cancelled()
                raw = response.read(fetch_max_bytes + 1)
                status = response.status
                content_type = response.headers.get('Content-Type', '')
        except urllib.error.HTTPError as error:
            raw = error.read(fetch_max_bytes + 1)
            status = error.code
            content_type = error.headers.get('Content-Type', '') if error.headers else ''
        finally:
            transport.finish()
        truncated = len(raw) > fetch_max_bytes
        raw = raw[:fetch_max_bytes]
        text = raw.decode('utf-8', errors='replace')
        return {'status': status, 'content_type': content_type, 'truncated': truncated,
                'text': _extract_text(text, content_type)}

    def select_view(source):
        selected = parent_view if source == 'parent' else view
        if selected is None:
            raise RuntimeError(f'No {source} view attached')
        return selected

    def zoom(start, count=1, offset=0, limit=None, source='local'):
        selected = select_view(source)
        page = selected.zoom(start, count, offset=offset, limit=limit)
        if count == 1 and limit is not None:
            total = len(selected.tree.log.get(start).text.encode('utf-8'))
            next_offset = offset + len(page.encode('utf-8'))
            if next_offset < total:
                source_arg = ", source='parent'" if source == 'parent' else ''
                page += (f'\n[Message {start}: bytes {offset}..{next_offset} of {total}. '
                         f'Continue with zoom({start}, 1, offset={next_offset}{source_arg}).]')
        return page

    def date(i=None, source='local'):
        if i is None:
            return datetime.now(timezone.utc).isoformat()
        return select_view(source).date(i)

    def spawn_agent(name, task, tools=None, backend=None, capability='routine', repo=None):
        if spawn is None:
            raise RuntimeError('Subagents unavailable')
        if repo:
            return spawn(name, task, tools, backend=backend, capability=capability, repo=repo)
        if backend is None and capability == 'routine':
            return spawn(name, task, tools)
        return spawn(name, task, tools, backend=backend, capability=capability)

    def wait_agents(timeout=None):
        if agent_wait is None:
            raise RuntimeError('Subagents unavailable')
        return agent_wait(timeout)

    def list_dir(path='.', limit=list_limit):
        limit = max(1, min(int(limit), 2000))
        target = resolve(path)
        if not target.is_dir():
            raise ValueError('not a directory')
        children = sorted(target.iterdir(), key=lambda item: item.name)
        rows = [child.name + ('/' if child.is_dir() else '') for child in children[:limit]]
        omitted = len(children) - len(rows)
        text = '\n'.join(rows)
        if omitted:
            text += f'\nmore: {omitted}, page with list_dir'
        return text

    def grep(pattern, path='.', limit=grep_limit):
        if not isinstance(pattern, str) or not pattern or len(pattern) > 200:
            raise ValueError('pattern must be 1..200 characters')
        try:
            compiled = re.compile(pattern)
        except re.error as error:
            raise ValueError('invalid pattern') from error
        limit = max(1, min(int(limit), 500))
        start = resolve(path)
        skip = {'.git', 'node_modules', '__pycache__', '.venv'}
        files = []
        if start.is_file():
            files = [start]
        elif start.is_dir():
            for dirpath, dirnames, filenames in os.walk(start):
                dirnames[:] = sorted(name for name in dirnames if name not in skip)
                for name in sorted(filenames):
                    files.append(Path(dirpath) / name)
                    if len(files) >= grep_scan:
                        break
                if len(files) >= grep_scan:
                    break
        matches = []
        extra = 0
        for file in files:
            try:
                if file.stat().st_size > 1_000_000:
                    continue
                data = file.read_bytes()
            except OSError:
                continue
            if b'\x00' in data[:8192]:
                continue
            for number, line in enumerate(data.decode('utf-8', errors='replace').splitlines(), 1):
                if not compiled.search(line):
                    continue
                if len(matches) >= limit:
                    extra += 1
                    continue
                try:
                    label = file.name if file == start else str(file.relative_to(start))
                except ValueError:
                    label = file.name
                matches.append(f'{label}:{number}: {line[:200]}')
        text = '\n'.join(matches)
        if extra:
            text += f'\nmore: {extra}, page with grep'
        return text

    def git_read(action, limit=None):
        """status, log, diff, or HEAD. Fixed git arguments only; this does not run a shell."""
        checkout = root.path
        git_dir = _git_dir(checkout)
        if git_dir is None:
            return 'not a git checkout'
        if action == 'head':
            return _read_git_head(git_dir)
        if not _popen_is_real():
            return 'git unavailable'
        if action == 'status':
            args = ['status', '--porcelain=v1', '-b']
        elif action == 'log':
            count = git_log_limit if limit is None else limit
            count = max(1, min(int(count), 100))
            args = ['log', '-n', str(count), '--format=%h %s']
        elif action == 'diff':
            args = ['diff', '--no-ext-diff', 'HEAD']
        else:
            raise ValueError('unsupported git action')
        try:
            result = subprocess.run(['git', '-C', str(checkout), '-c', 'color.ui=false', *args],
                                    capture_output=True, text=True, timeout=timeout, check=False)
        except (OSError, subprocess.SubprocessError) as error:
            return f'git unavailable ({type(error).__name__})'
        text = (result.stdout or '') + (('\n' + result.stderr.strip()) if result.returncode and result.stderr.strip() else '')
        return clip_output(text.strip() or 'clean', clip_chars)

    string = {'type': 'string'}
    integer = {'type': 'integer', 'minimum': 0}
    line_no = {'type': 'integer', 'minimum': 1}
    source = {'type': 'string', 'enum': ['local', 'parent']}
    env_schema = {'type': 'object', 'additionalProperties': {'type': 'string'}}
    registry.release = processes.close
    registry.register('shell', 'Run a shell command in the session workspace. cwd defaults to that workspace. env is merged onto the current environment. Long output is saved and the tail is returned. stdin is written once. Requires approval under ask.', object_schema({'command': string, 'cwd': string, 'timeout': {'type': 'number', 'minimum': 0.001}, 'env': env_schema, 'stdin': string}, ['command']), shell, risky=True)
    registry.register('read_file', 'Read UTF-8 text. start_line and end_line select a 1-based line range; otherwise offset and limit clip characters. Relative paths start at the session workspace. Absolute paths are allowed.', object_schema({'path': string, 'offset': integer, 'limit': {'type': 'integer', 'minimum': 1}, 'start_line': line_no, 'end_line': line_no}, ['path']), read_file)
    registry.register('read_bytes', 'Read a file as base64, including binary files. Relative paths start at the session workspace.', object_schema({'path': string, 'limit': {'type': 'integer', 'minimum': 1}}, ['path']), read_bytes)
    registry.register('read_image', 'Read a png, jpeg, gif, or webp file and return the image.', object_schema({'path': string}, ['path']), read_image)
    registry.register('list_dir', 'List names in a directory of the session workspace. Relative paths start there. Absolute paths are allowed.', object_schema({'path': string, 'limit': {'type': 'integer', 'minimum': 1}}), list_dir)
    registry.register('glob', 'Find files by a glob pattern under a directory. Skips version-control and dependency directories.', object_schema({'pattern': string, 'path': string, 'limit': {'type': 'integer', 'minimum': 1}}, ['pattern']), glob_paths)
    registry.register('grep', 'Search UTF-8 text under the session workspace. Skips version-control and dependency directories.', object_schema({'pattern': string, 'path': string, 'limit': {'type': 'integer', 'minimum': 1}}, ['pattern']), grep)
    registry.register('git_read', 'Read git status, log, diff, or HEAD for the session workspace. Does not change the checkout.', object_schema({'action': {'type': 'string', 'enum': ['status', 'log', 'diff', 'head']}, 'limit': {'type': 'integer', 'minimum': 1}}, ['action']), git_read)
    registry.register('write_file', 'Write UTF-8 text to a file, creating parents. Requires approval under ask.', object_schema({'path': string, 'text': string}, ['path', 'text']), write_file, risky=True)
    registry.register('edit_file', 'Replace exactly one occurrence of old text with new text. Requires approval under ask.', object_schema({'path': string, 'old': string, 'new': string}, ['path', 'old', 'new']), edit_file, risky=True)
    registry.register('apply_patch', 'Apply a multi-hunk patch. Accepts a *** Begin Patch block (Add, Update, Delete File) or a unified diff. Requires approval under ask.', object_schema({'patch': string}, ['patch']), apply_patch, risky=True)
    registry.register('move_path', 'Move or rename a file or directory. Requires approval under ask.', object_schema({'src': string, 'dest': string}, ['src', 'dest']), move_path, risky=True)
    registry.register('copy_path', 'Copy a file or directory. Requires approval under ask.', object_schema({'src': string, 'dest': string}, ['src', 'dest']), copy_path, risky=True)
    registry.register('delete_path', 'Delete a file or an empty directory. recursive deletes a directory tree. Requires approval under ask.', object_schema({'path': string, 'recursive': {'type': 'boolean'}}, ['path']), delete_path, risky=True)
    registry.register('mkdir', 'Create a directory, including parents. Requires approval under ask.', object_schema({'path': string}, ['path']), mkdir, risky=True)
    registry.register('process', 'Start, poll, kill, or write stdin to a background process in the session workspace. start returns an id. Requires approval under ask.', object_schema({'action': {'type': 'string', 'enum': ['start', 'poll', 'kill', 'write']}, 'command': string, 'id': string, 'stdin': string, 'cwd': string, 'env': env_schema}, ['action']), process, risky=True)
    registry.register('web_fetch', 'HTTP GET a URL and return extracted text, capped by the configured size. No search API.', object_schema({'url': string, 'headers': env_schema}, ['url']), web_fetch)
    registry.register('zoom', 'Expand a summary range; count is a power of two, start is aligned. Use source=parent for inherited IDs. offset/limit bound UTF-8 page data; continuation notices are outside that bound.', object_schema({'start': integer, 'count': {'type': 'integer', 'minimum': 1}, 'offset': integer, 'limit': {'type': 'integer', 'minimum': 1}, 'source': source}, ['start']), zoom)
    registry.register('date', 'Get a logged message date, or current UTC time if i is omitted.', object_schema({'i': integer, 'source': source}), date)
    registry.register('spawn_agent', "Start independent background work. Declare capability: routine for simple inspection/edits, standard for implementation/debugging, complex for difficult architecture/reasoning. The engine selects the cheapest configured capable backend; backend explicitly overrides it. Read agent_backends for choices. Tools can narrow access. For a change to a git repository, pass repo (a path inside that checkout): a worker with write tools gets its own worktree and branch, can write and commit there, and its report names the branch to merge. Without repo, a session workspace that is a git checkout is used the same way. Its report returns to this chat.", object_schema({'name': string, 'task': string, 'tools': {'type': 'array', 'items': string}, 'backend': string, 'capability': {'type': 'string', 'enum': ['routine', 'standard', 'complex']}, 'repo': string}, ['name', 'task']), spawn_agent)
    registry.register('wait_agents', 'Wait for background agents and return their reports.', object_schema({'timeout': {'type': 'number', 'minimum': 0}}), wait_agents)
    return registry
