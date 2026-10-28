"""Run the packaged AetherMind engine as a local HTTP MCP service on demand.

The bundled engine is a FastMCP streamable-http server (not a stdio child), so
it needs a port and a provisioned runtime. This module owns that lifecycle:

- first use builds a venv under the configured Tessera home and installs Tessera's
  pinned MCP requirements lock (provisioning happens once per install). That lock
  is not a file of the vendored engine;
- the engine starts on 127.0.0.1 with an OS-chosen port so two installs on one
  machine cannot collide;
- a process-wide registry reuses the single running engine for every session;
- the packaged engine is the memory service. An external URL or endpoint list
  replaces it only after that service validates (reachable, accepted auth).
  A configured service that does not validate leaves the packaged engine running.
"""

import hashlib
import json
import os
import pathlib
import re
import shutil
import socket
import stat
import subprocess
import sys
import threading
import time

from .secret_names import is_secret_name


_REGISTRY = {}
_CHOICES = {}
_FLIGHTS = {}
_LOCK = threading.Lock()
_CLASS_NAME = re.compile(r'^[A-Za-z][A-Za-z0-9]{0,40}$')


def _engine_root(config):
    raw = getattr(config.aethermind, 'bundled_path', '') or ''
    if not raw:
        from .assets import asset_path
        root = asset_path('aethermind-engine')
        entry = root / 'mcp' / 'aethermind_mcp.py'
        if not entry.is_file():
            return None
        if (root / 'build-receipt.json').is_file():
            return root
        # A source checkout has the engine bytes but no wheel receipt. A
        # verified home-local bundle is the receipt-backed copy that can start.
        return _local_bundle_root(config, root)
    root = pathlib.Path(raw).expanduser()
    if not root.is_absolute():
        home = getattr(config, 'home', None) or pathlib.Path('~').expanduser()
        root = home / root
    return root


def _local_bundle_root(config, source):
    from .engine_manifest import EngineBundleError, load_manifest, manifest_file, verify_engine_tree
    try:
        manifest = load_manifest()
        verify_engine_tree(source, manifest, override=False)
        manifest_bytes = manifest_file().read_bytes()
    except EngineBundleError:
        return None
    key = hashlib.sha256(manifest_bytes).hexdigest()[:20]
    home = pathlib.Path(config.home).expanduser().resolve()
    bundle = home / 'runtime' / 'aethermind' / 'bundles' / key
    if not (bundle / 'build-receipt.json').is_file():
        return None
    return bundle


def ensure_local_engine_bundle(config):
    """Materialize a verified source checkout as a receipt-backed bundle.

    A wheel ships build-receipt.json. A source checkout has the same engine
    bytes but no receipt, so the embedded engine would never start. Copy the
    manifest-verified vendored tree into this home once, write the receipt
    there, and let _engine_root use that copy. Returns the bundle root or None.
    """
    raw = getattr(config.aethermind, 'bundled_path', '') or ''
    if raw:
        return None
    from .assets import asset_path
    source = asset_path('aethermind-engine')
    entry = source / 'mcp' / 'aethermind_mcp.py'
    if not entry.is_file():
        return None
    if (source / 'build-receipt.json').is_file():
        return None
    from .engine_manifest import EngineBundleError, load_manifest, manifest_file, verify_engine_tree
    try:
        manifest = load_manifest()
        verify_engine_tree(source, manifest, override=False)
        manifest_bytes = manifest_file().read_bytes()
    except EngineBundleError:
        return None
    key = hashlib.sha256(manifest_bytes).hexdigest()[:20]
    home = pathlib.Path(config.home).expanduser().resolve()
    bundle = home / 'runtime' / 'aethermind' / 'bundles' / key
    if (bundle / 'build-receipt.json').is_file():
        return bundle
    parent = bundle.parent
    parent.mkdir(parents=True, exist_ok=True)
    temporary = parent / (key + '.tmp')
    if temporary.exists():
        shutil.rmtree(temporary)
    payload = {
        'source': 'verified source checkout',
        'manifest': manifest_file().name,
        'manifest_sha256': hashlib.sha256(manifest_bytes).hexdigest(),
        'commit': str(manifest.get('commit', '')),
        'files': len(manifest.get('files') or {}),
    }
    try:
        shutil.copytree(source, temporary)
        (temporary / 'build-receipt.json').write_text(
            json.dumps(payload, indent=2, sort_keys=True) + '\n', encoding='utf-8')
        if bundle.exists():
            shutil.rmtree(bundle)
        temporary.rename(bundle)
    except OSError:
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)
        return None
    return bundle


def _runtime_root(config, root):
    # Installed assets can be read-only; each Tessera home owns its runtime.
    identity = str(root.resolve()) + chr(10) + sys.executable
    identity += chr(10) + (root / 'build-receipt.json').read_text()
    key = hashlib.sha256(identity.encode()).hexdigest()[:20]
    return pathlib.Path(config.home).expanduser().resolve() / 'runtime' / 'aethermind' / key


def _pick_port():
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.bind(('127.0.0.1', 0))
        return listener.getsockname()[1]
    finally:
        listener.close()


def _venv_python(venv, platform=None):
    platform = os.name if platform is None else platform
    return venv / 'Scripts' / 'python.exe' if platform == 'nt' else venv / 'bin' / 'python'


def mcp_requirements_lock():
    """Where this install keeps Tessera's MCP lock.

    A wheel copies docs/packaging/aethermind-mcp-requirements.lock to
    tessera/_assets/aethermind-mcp-requirements.lock, beside the vendored tree
    rather than inside it. A source checkout reads the docs copy. An installed
    package that lacks the file does not walk out to a neighboring checkout.
    """
    from .assets import _installed_layout
    package = pathlib.Path(__file__).resolve().parent
    packaged = package / '_assets' / 'aethermind-mcp-requirements.lock'
    if packaged.is_file():
        return packaged
    if _installed_layout(package):
        return packaged
    source = package.parent / 'docs' / 'packaging' / 'aethermind-mcp-requirements.lock'
    if source.is_file():
        return source
    return packaged


def _child_text(args):
    """Run a provision child and keep its output off the parent's stdout.

    The app-server speaks JSON on stdout. redirect_stdout does not cover a
    child, so pip's progress must be captured and written to stderr. A failure
    raises with that same output, so the pip log is not discarded.
    """
    completed = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    output = completed.stdout or ''
    if output:
        sys.stderr.write(output if output.endswith('\n') else output + '\n')
    return completed.returncode, output


def _provision(root, runtime=None):
    """Venv plus Tessera's hashed MCP lock. Installs only when the sdk is absent."""
    runtime = root if runtime is None else runtime
    runtime.mkdir(parents=True, exist_ok=True)
    venv = runtime / '.venv'
    python = _venv_python(venv)
    if not python.is_file():
        code, output = _child_text([sys.executable, '-m', 'venv', str(venv)])
        if code != 0:
            error = RuntimeError('Could not create the embedded engine venv.\n' + output)
            _note_engine_log(runtime, str(error))
            raise error
    probe = subprocess.run([str(python), '-c', 'import mcp'],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if probe.returncode != 0:
        try:
            _install_locked_mcp(python)
        except RuntimeError as error:
            _note_engine_log(runtime, str(error))
            raise
    return str(python)


def _log_limit(config=None):
    """The configured cap, or the dataclass default when config does not name one."""
    from .config import AetherMindConfig
    fallback = AetherMindConfig.engine_log_max_bytes
    memory = getattr(config, 'aethermind', None) if config is not None else None
    limit = getattr(memory, 'engine_log_max_bytes', fallback)
    if type(limit) is not int or limit < 1:
        return fallback
    return limit


def _note_engine_log(runtime, message, limit=None):
    runtime = pathlib.Path(runtime)
    runtime.mkdir(parents=True, exist_ok=True)
    if limit is None:
        limit = _log_limit()
    _append_capped_log(runtime / 'engine.log', (message.rstrip() + '\n').encode('utf-8', errors='replace'), limit)


def _append_capped_log(path, data, limit):
    """Append bytes, then keep only the last ``limit`` bytes.

    The file is opened for update, not append: an append-mode write ignores
    the seek and would grow past the cap.
    """
    if not path.exists():
        path.write_bytes(b'')
    with path.open('r+b') as stream:
        stream.seek(0, os.SEEK_END)
        stream.write(data)
        stream.flush()
        size = stream.tell()
        if size <= limit:
            return
        stream.seek(size - limit)
        tail = stream.read()
        stream.seek(0)
        stream.write(tail)
        stream.truncate()


def _pump_engine_log(stream, path, limit):
    """Copy the engine's output into a file that stays within ``limit`` bytes."""
    try:
        with path.open('w+b') as handle:
            while True:
                chunk = stream.read(65536)
                if not chunk:
                    break
                handle.write(chunk)
                handle.flush()
                size = handle.tell()
                if size <= limit:
                    continue
                handle.seek(size - limit)
                tail = handle.read()
                handle.seek(0)
                handle.write(tail)
                handle.truncate()
                handle.flush()
    finally:
        stream.close()


def _finish_log(state):
    thread = state.get('log_thread')
    if thread is not None:
        thread.join(timeout=5)
    log = state.get('log')
    if log is not None:
        log.close()


def _install_locked_mcp(python):
    """Install Tessera's hashed MCP lock. The vendored tree does not ship one."""
    lock = mcp_requirements_lock()
    if not lock.is_file():
        raise RuntimeError(
            'Tessera is missing its pinned AetherMind MCP requirements lock '
            f'({lock}). The vendored engine does not ship mcp/requirements-mcp.lock. '
            'A release wheel carries docs/packaging/aethermind-mcp-requirements.lock '
            'as tessera/_assets/aethermind-mcp-requirements.lock. '
            'TESSERA_BUNDLE_SOURCE does not supply this lock.')
    code, output = _child_text(
        [str(python), '-m', 'pip', 'install', '--require-hashes', '-r', str(lock)])
    if code != 0:
        raise RuntimeError(
            'Installing the embedded AetherMind MCP requirements from '
            f'{lock} failed.\n' + output)


def _wait_for_engine(port, process, deadline, poll=0.25):
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return False
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=1):
                return True
        except OSError:
            time.sleep(poll)
    return False


# A matching vendored tree has no build receipt, so ensure_bundled_engine
# returns no process. Status uses this sentence instead of claiming one started.
RECEIPT_ABSENT_SENTENCE = (
    'bytes present, not started: This checkout has no build receipt, '
    'so the embedded engine is not started'
)


def unstarted_engine_reason(config):
    """Why no engine URL came back. 'present' is bytes on disk, not a process."""
    from .engine_manifest import engine_packaging
    if engine_packaging(config) == 'present':
        return RECEIPT_ABSENT_SENTENCE
    return 'Packaged engine is not in this install'


# How to get a working service. Every refusal names this, including YOLO.
MEMORY_UNAVAILABLE_FIX = (
    'Fix: install a release wheel, which writes build-receipt.json, '
    'or set aethermind.bundled_path to a copy that has the receipt, '
    'or configure an external AetherMind that validates.'
)


class AetherMindUnavailable(RuntimeError):
    """A turn or job cannot run because no working AetherMind is attached.

    The message names the cause and the fix. Approval mode, including YOLO,
    is not consulted.
    """


def unavailable_message(choice):
    """Labeled reason a choice produced no service, plus how to get one.

    External text is already redacted.
    """
    choice = choice if isinstance(choice, dict) else {}
    reason = choice.get('reason')
    if not isinstance(reason, str) or not reason.strip():
        reason = 'this session has no AetherMind client' if not choice else 'no memory service is available'
    text = 'AetherMind is not available: ' + reason.strip()
    external = choice.get('external_error')
    if isinstance(external, str) and external.strip() and external.strip() not in text:
        text += ' (' + external.strip() + ')'
    return text + ' ' + MEMORY_UNAVAILABLE_FIX


def require_memory_choice(config):
    """Return the chosen service, or raise when neither embedded nor external is up."""
    choice = choose_memory(config)
    if choice.get('url') or choice.get('endpoints'):
        return choice
    raise AetherMindUnavailable(unavailable_message(choice))


def _engine_log_place(config):
    """Return (path, error).

    path is the log when it can be named, or the build receipt when that file
    is why the log cannot be named. error is set when locating the log failed.
    A missing packaged engine is (None, None): there is no path to report.
    """
    root = _engine_root(config)
    home = getattr(config, 'home', None)
    if root is None or not home:
        return None, None
    try:
        return _runtime_root(config, root) / 'engine.log', None
    except OSError as error:
        return pathlib.Path(root) / 'build-receipt.json', error


def engine_log_sighting(config):
    """Where the engine log would be, and why it cannot be named when it cannot.

    Returns (path, error). path is None only when this install has no bundled
    engine, which is a fact about the install rather than a failure. error is a
    sentence for any other reason the path is unknown, so a caller that reports
    status or the log view can name the failure instead of dropping it.
    """
    try:
        return _engine_log_place(config)
    except OSError as error:
        return None, 'could not locate the engine log: ' + type(error).__name__ + ': ' + str(error)


def engine_log_path(config):
    """The log file a bundled engine writes, or None when it cannot be named.

    Status uses this as a boolean. engine_log_sighting carries the reason when
    the path is unknown; engine_log_text reports it on the log view.
    """
    path, error = engine_log_sighting(config)
    if error is not None:
        return None
    return path


# The marker stays in the line so a person can see that a value was removed
# and read everything else. The name rule lives in secret_names.
_ASSIGNMENT = re.compile(
    r'(?P<keyquote>["\']?)(?P<key>[A-Za-z0-9_.-]+)(?P=keyquote)'
    r'(?P<sep>\s*[:=]\s*)(?P<valuequote>["\']?)(?P<value>[^\s"\'&,}]+)(?P=valuequote)'
)
_BEARER = re.compile(r'(?i)\b((?:authorization\s*[:=]\s*)?bearer\s+)([^\s"\']+)')
_REDACTED = '[redacted]'


def _credential_name(key):
    return is_secret_name(key)


def _configured_secrets(config):
    """Secret strings the running config and the process environment already hold."""
    found = []

    def walk(value, key=''):
        if isinstance(value, str):
            if value and value != _REDACTED and _credential_name(key):
                found.append(value)
            return
        if isinstance(value, dict):
            for name, child in value.items():
                walk(child, str(name))
            return
        if isinstance(value, (list, tuple)):
            for item in value:
                walk(item, key)
            return
        fields = getattr(value, '__dataclass_fields__', None)
        if fields:
            for name in fields:
                walk(getattr(value, name), name)

    walk(config)
    for name, value in os.environ.items():
        if _credential_name(name) and isinstance(value, str) and value and value != _REDACTED:
            found.append(value)
    return sorted(set(found), key=len, reverse=True)


def redact_engine_log(text, config):
    """Hide secret and token values. The surrounding log stays readable.

    Each removal is the visible marker ``[redacted]``. Paths, counts and
    ordinary diagnostic lines are left as the engine wrote them.
    """
    def bearer(match):
        value = match.group(2)
        if not value or value == _REDACTED:
            return match.group(0)
        return match.group(1) + _REDACTED

    def assignment(match):
        if not _credential_name(match.group('key')):
            return match.group(0)
        value = match.group('value')
        if not value or value == _REDACTED or value.lower() in ('true', 'false'):
            # A switch named like a credential (allow_api_key) is not a value.
            return match.group(0)
        return ''.join((
            match.group('keyquote'), match.group('key'), match.group('keyquote'),
            match.group('sep'), match.group('valuequote'), _REDACTED, match.group('valuequote'),
        ))

    text = _BEARER.sub(bearer, text)
    text = _ASSIGNMENT.sub(assignment, text)
    for secret in _configured_secrets(config):
        if secret and secret != _REDACTED and secret in text:
            text = text.replace(secret, _REDACTED)
    return text


def _engine_log_error(path, error):
    where = '' if path is None else str(path)
    if where:
        return f'could not read engine log at {where}: {error}'
    return f'could not read engine log: {error}'


NO_ENGINE_LOG_SENTENCE = 'This install has no bundled engine, so it has no engine log.'


def _path_kind(mode):
    """What a non-regular file at the log path is, in words."""
    if stat.S_ISDIR(mode):
        return 'a directory'
    if stat.S_ISFIFO(mode):
        return 'a pipe'
    if stat.S_ISSOCK(mode):
        return 'a socket'
    if stat.S_ISCHR(mode) or stat.S_ISBLK(mode):
        return 'a device'
    return 'not a regular file'


def engine_log_text(config):
    """The tail of the bundled engine log. The path is chosen here, never by the caller.

    The tail is the engine log cap the writer already uses. Secret and token
    values are replaced with a visible ``[redacted]`` marker before the text
    is returned. A log that is not there names its path. An install with no
    bundled engine says so. Anything that is not a regular file is named for
    what it is. An unreadable log, including a missing build receipt under an
    explicit bundled path, names the path and the error instead of failing the
    request.
    """
    limit = _log_limit(config)
    body = {'cap': limit, 'available': False, 'text': ''}
    path, error = engine_log_sighting(config)
    if error is not None:
        body['path'] = '' if path is None else str(path)
        body['error'] = _engine_log_error(path, error)
        return body
    if path is None:
        body['reason'] = NO_ENGINE_LOG_SENTENCE
        body['path'] = ''
        return body
    body['path'] = str(path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        body['missing'] = True
        return body
    except OSError as error:
        body['error'] = _engine_log_error(path, error)
        return body
    if stat.S_ISLNK(info.st_mode):
        body['error'] = _engine_log_error(path, 'the log is a symlink')
        return body
    if not stat.S_ISREG(info.st_mode):
        body['error'] = _engine_log_error(path, 'the log path is ' + _path_kind(info.st_mode))
        return body
    try:
        data = path.read_bytes()
    except OSError as error:
        body['error'] = _engine_log_error(path, error)
        return body
    tail = data[-limit:]
    text = tail.decode('utf-8', 'replace')
    if len(data) > limit:
        text = '…\n' + text
    body['available'] = True
    body['missing'] = False
    body['text'] = redact_engine_log(text, config)
    body['bytes'] = len(data)
    return body


def ensure_bundled_engine(config, clock=time.monotonic, *, force=False):
    """Start (once) and return the engine URL, or None when the engine is absent.

    Without force, a configured external service skips the launch: the caller
    has not asked the packaged engine to run. There is no enabled flag. An
    empty bundled_path still names the packaged copy. Session startup uses
    force after it has decided the packaged engine is the one that should run.
    """
    memory = config.aethermind
    if not force:
        if memory.endpoints or memory.url:
            return None  # The caller has not asked the bundle to run.
    root = _engine_root(config)
    if root is None:
        return None
    entry = root / 'mcp' / 'aethermind_mcp.py'
    receipt = root / 'build-receipt.json'
    if not entry.is_file() or not receipt.is_file():
        return None
    runtime = _runtime_root(config, root)
    key = str(runtime)
    with _LOCK:
        state = _REGISTRY.get(key)
        if state is not None and state['proc'].poll() is None:
            return state['url']
        previous = _REGISTRY.pop(key, None)
        if previous is not None:
            _finish_log(previous)
        limit = _log_limit(config)
        python = _provision(root, runtime)
        port = _pick_port()
        env = dict(os.environ)
        env['MCP_HOST'] = '127.0.0.1'
        runtime.mkdir(parents=True, exist_ok=True)
        child_home = runtime / 'home'
        child_home.mkdir(exist_ok=True)
        env['HOME'] = str(child_home)
        env['USERPROFILE'] = str(child_home)
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        log_path = runtime / 'engine.log'
        log_path.write_bytes(b'')
        process = subprocess.Popen([python, str(entry), str(port)],
                                   cwd=str(runtime), env=env,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        log_thread = threading.Thread(
            target=_pump_engine_log, args=(process.stdout, log_path, limit),
            name='aethermind-engine-log', daemon=True)
        log_thread.start()
        url = 'http://127.0.0.1:' + str(port) + '/mcp'
        if not _wait_for_engine(port, process, clock() + 60):
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            log_thread.join(timeout=5)
            tail = log_path.read_text(encoding='utf-8', errors='replace')[-2000:].strip()
            detail = f': {tail}' if tail else ''
            raise RuntimeError(f'bundled engine did not answer on its port{detail}')
        _REGISTRY[key] = {'url': url, 'proc': process, 'log_thread': log_thread}
        return url


def _poll_failure(error):
    """A poll() failure, named so the reason says which check failed."""
    return 'Embedded engine poll failed: ' + _error_label(error, [])


def _error_label(error, secret_names):
    """The exception name and its whole message, with configured secrets removed.

    The message is not cut. Settings and the turn refusal show this text.
    """
    message = str(error).replace('\n', ' ').strip()
    for name in secret_names:
        if not isinstance(name, str) or not name:
            continue
        secret = os.environ.get(name, '')
        if secret:
            message = message.replace(secret, '[REDACTED]')
    name = type(error).__name__
    if not message or message == name:
        return name
    return name + ': ' + message


def probe_external(config):
    """Raise when the configured service is unreachable or rejects its auth.

    The endpoint and token variable come from config. This does not start the
    packaged engine.
    """
    from .aethermind import AetherMindClient
    memory = config.aethermind
    endpoint_factory = None
    if any(str(item or '').startswith('mcp:') for item in (memory.endpoints or [])):
        from .mcp_servers import resolve_endpoint_factory
        endpoint_factory = resolve_endpoint_factory(config)
    client = AetherMindClient(
        memory.url or '', token_env=memory.token_env, timeout=memory.probe_timeout,
        endpoints=memory.endpoints or None, endpoint_factory=endpoint_factory,
        probe_timeout=memory.probe_timeout, probe_interval=0)
    client.detect_tools(refresh=True)


def _wants_external(memory):
    """An explicit choice wins. An empty choice follows a saved external service."""
    source = getattr(memory, 'source', '') or ''
    if source == 'embedded':
        return False
    if source == 'external':
        return True
    return bool(memory.url or memory.endpoints)


def _choice_key(config):
    """Content of the choice, not id(config). A recycled object must not reuse a dead URL."""
    memory = config.aethermind
    home = getattr(config, 'home', '') or ''
    return (
        str(home),
        memory.url or '',
        tuple(memory.endpoints or []),
        memory.bundled_path or '',
        getattr(memory, 'source', '') or '',
        getattr(memory, 'token_env', '') or '',
    )


def clear_memory_choices():
    """Drop cached service choices so the next session build checks again."""
    with _LOCK:
        _CHOICES.clear()


def _copy_choice(choice):
    """A session-owned copy. Endpoints are not shared with the process cache."""
    if not isinstance(choice, dict):
        return choice
    copied = dict(choice)
    endpoints = copied.get('endpoints')
    if isinstance(endpoints, list):
        copied['endpoints'] = list(endpoints)
    return copied


def _probe_error_types():
    """Failures a service probe is allowed to raise. Anything else propagates."""
    from .endpoints import EndpointUnavailable
    from .mcp_client import McpError
    return (McpError, EndpointUnavailable, OSError, ValueError)


def _embedded_url_live(url):
    """Whether this process still has a running engine for url.

    Returns (state, cause). state is True while a process is running, False
    when every process for this URL has exited or could not be inspected, and
    None when this process never started the URL. cause names an OSError from
    poll() when no process was confirmed running.
    """
    if not url:
        return False, ''
    with _LOCK:
        procs = [state.get('proc') for state in _REGISTRY.values()
                 if state.get('url') == url and state.get('proc') is not None]
    if not procs:
        return None, ''
    causes = []
    for proc in procs:
        try:
            if proc.poll() is None:
                return True, ''
        except OSError as error:
            causes.append(_poll_failure(error))
    return False, '; '.join(causes)


def _launch(starter, config, clock):
    try:
        return starter(config, clock, force=True) or '', ''
    except Exception as error:
        return '', _error_label(error, [])


def _stopped_reason(config, detail):
    return 'Embedded engine stopped and did not restart: ' + (detail or unstarted_engine_reason(config))


def _revive_embedded(config, choice, clock, starter):
    """Relaunch an embedded engine this process started after it exits.

    A URL this process never registered is left alone. The same dead URL, or
    no URL, is not a working service.
    """
    url = choice.get('url') or ''
    live, inspect_error = _embedded_url_live(url)
    if live is not False:
        return choice
    fresh, detail = _launch(starter, config, clock)
    fresh_error = ''
    if fresh:
        fresh_live, fresh_error = _embedded_url_live(fresh)
        if fresh_live is not False:
            updated = dict(choice)
            updated['url'] = fresh
            return updated
    updated = dict(choice)
    updated['url'] = ''
    updated['reason'] = _stopped_reason(config, detail or fresh_error or inspect_error)
    return updated


_IDLE_STOP_REASONS = (
    'Embedded engine is not running',
    'Embedded engine has already exited',
    'bytes present, not started',
    'Packaged engine is not in this install',
    'Embedded engine has no build receipt',
)


def _stop_embedded_engine(config):
    """Stop the packaged engine. An OSError is a labeled result."""
    try:
        return stop_bundled_engine(config)
    except OSError as error:
        detail = str(error).replace('\n', ' ').strip()
        reason = 'Embedded engine stop failed: ' + type(error).__name__
        if detail and detail != type(error).__name__:
            reason += ': ' + detail
        return {'stopped': False, 'reason': reason}


def _switch_to_external(config):
    """Use the validated external service and stop the embedded engine."""
    choice = _validated_external(config)
    stopped = _stop_embedded_engine(config)
    reason = stopped.get('reason') if isinstance(stopped, dict) else ''
    failed = isinstance(stopped, dict) and stopped.get('stopped') is not True
    if isinstance(stopped, dict) and stopped.get('stopped') is True and isinstance(reason, str) and reason:
        choice['embedded_stop'] = reason
    elif failed and isinstance(reason, str) and reason and not reason.startswith(_IDLE_STOP_REASONS):
        if reason not in choice['reason']:
            choice['reason'] = choice['reason'] + ' (' + reason + ')'
    else:
        with _LOCK:
            cached = _CHOICES.get(_choice_key(config))
        prior = cached.get('embedded_stop') if isinstance(cached, dict) else None
        if isinstance(prior, str) and prior.strip():
            choice['embedded_stop'] = prior.strip()
    return choice


def _record_external_failure(choice, config, error):
    """Keep a live embedded engine and replace the external failure on it."""
    from .capture import _category_for
    memory = config.aethermind
    name = type(error).__name__
    label = _error_label(error, [getattr(memory, 'token_env', '') or '', 'AETHERMIND_TOKEN'])
    reason = 'External service did not validate'
    if label and label not in reason:
        reason = reason + ' (' + label + ')'
    updated = dict(choice)
    updated['external_error'] = label
    updated['external_error_name'] = name if _CLASS_NAME.fullmatch(name) else None
    updated['external_category'] = _category_for(error)
    updated['reason'] = reason
    return updated


def _recover_external(config, clock, starter, current=None):
    """Probe the configured external service. Success replaces an embedded fallback.

    A failed probe leaves an embedded URL that is still running, or that this
    process never started, in place. Only an engine this process saw exit is
    launched again.
    """
    try:
        probe_external(config)
    except _probe_error_types() as error:
        url = ''
        if isinstance(current, dict) and (current.get('source') or '') == 'embedded':
            url = current.get('url') or ''
        if url and _embedded_url_live(url)[0] is not False:
            return _record_external_failure(current, config, error)
        return _embedded_after_external_failure(config, error, clock, starter)
    return _switch_to_external(config)


def _running_embedded_url():
    """A packaged engine this process still has running, and any poll() failure.

    Returns (url, cause). url is empty when none is running. cause names each
    OSError from poll(), including when another process is still running.
    """
    with _LOCK:
        procs = [(state.get('url') or '', state.get('proc')) for state in _REGISTRY.values()]
    live = ''
    causes = []
    for url, proc in procs:
        if not url or proc is None:
            continue
        try:
            running = proc.poll() is None
        except OSError as error:
            causes.append(_poll_failure(error))
            continue
        if running and not live:
            live = url
    return live, '; '.join(causes)


def _note_inspect_error(choice, inspect_error):
    if not isinstance(choice, dict) or not inspect_error:
        return choice
    reason = choice.get('reason') or ''
    if inspect_error in reason:
        return choice
    updated = dict(choice)
    updated['reason'] = (reason + ' (' + inspect_error + ')').strip()
    return updated


def _fallback_from_external(config, error, clock, starter):
    """Leave a dead external service for the embedded engine, labeling the failure.

    An engine this process already has running is kept. One it saw exit, or
    none at all, is launched. No URL means the embedded engine did not start.
    A poll() failure is recorded either way.
    """
    live, inspect_error = _running_embedded_url()
    if live:
        choice = _record_external_failure(
            {'source': 'embedded', 'url': live, 'endpoints': [], 'reason': ''},
            config, error)
    else:
        choice = _embedded_after_external_failure(config, error, clock, starter)
    return _note_inspect_error(choice, inspect_error)


def refresh_memory_choice(config, choice, clock=None):
    """Recheck the service a live session is using.

    An external choice is probed again. A failed probe falls back to the
    embedded engine and keeps the labeled failure. When that engine cannot
    start, the choice has no service URL. An embedded fallback probes the
    configured external service again and, when that probe succeeds, switches
    to it and stops the embedded engine. A dead embedded engine this process
    owns is relaunched.
    """
    if not isinstance(choice, dict):
        return choice
    clock = time.monotonic if clock is None else clock
    source = choice.get('source') or ''
    url = choice.get('url') or ''
    endpoints = list(choice.get('endpoints') or [])
    if source == 'external' and (url or endpoints):
        try:
            probe_external(config)
        except _probe_error_types() as error:
            recovered = _fallback_from_external(config, error, clock, ensure_bundled_engine)
        else:
            recovered = _validated_external(config)
            prior = choice.get('embedded_stop')
            if isinstance(prior, str) and prior.strip():
                recovered['embedded_stop'] = prior.strip()
        if (recovered.get('source') or '') == 'embedded' and recovered.get('url'):
            recovered = _revive_embedded(config, recovered, clock, ensure_bundled_engine)
        _remember_choice(_choice_key(config), recovered)
        return recovered
    if source == 'embedded' and choice.get('external_error') and _wants_external(config.aethermind):
        recovered = _recover_external(config, clock, ensure_bundled_engine, current=choice)
        if (recovered.get('source') or '') == 'embedded' and recovered.get('url'):
            recovered = _revive_embedded(config, recovered, clock, ensure_bundled_engine)
        _remember_choice(_choice_key(config), recovered)
        return recovered
    if source != 'embedded' or not url:
        return dict(choice)
    return _revive_embedded(config, choice, clock, ensure_bundled_engine)


def refresh_shared_memory(config, choice, clock=None):
    """Probe this process-wide choice once. Concurrent callers share the result."""
    if not isinstance(choice, dict):
        return choice
    key = _choice_key(config)
    thread = threading.get_ident()
    with _LOCK:
        flight = _FLIGHTS.get(key)
        if flight is None:
            flight = {
                'event': threading.Event(),
                'result': None,
                'error': None,
                'thread': thread,
            }
            _FLIGHTS[key] = flight
            leader = True
        else:
            leader = False
    if not leader:
        if flight.get('thread') == thread:
            return _copy_choice(refresh_memory_choice(config, choice, clock))
        flight['event'].wait()
        error = flight.get('error')
        if error is not None:
            raise error
        return _copy_choice(flight.get('result'))
    try:
        flight['result'] = refresh_memory_choice(config, choice, clock)
        return _copy_choice(flight['result'])
    except BaseException as error:
        flight['error'] = error
        raise
    finally:
        flight['event'].set()
        with _LOCK:
            if _FLIGHTS.get(key) is flight:
                del _FLIGHTS[key]


def _embedded_after_external_failure(config, error, clock, starter):
    """Keep the embedded engine and name the external failure that lost."""
    from .capture import _category_for
    memory = config.aethermind
    url, start_error = _launch(starter, config, clock)
    name = type(error).__name__
    label = _error_label(error, [getattr(memory, 'token_env', '') or '', 'AETHERMIND_TOKEN'])
    if url:
        reason = 'External service did not validate'
    elif start_error:
        reason = 'External service did not validate; embedded engine did not start: ' + start_error
    else:
        reason = unstarted_engine_reason(config)
        if reason == 'Packaged engine is not in this install':
            reason = 'External service did not validate; packaged engine is not in this install'
    if label and label not in reason:
        reason = reason + ' (' + label + ')'
    return {
        'source': 'embedded', 'url': url, 'endpoints': [],
        'external_error': label,
        'external_error_name': name if _CLASS_NAME.fullmatch(name) else None,
        'external_category': _category_for(error),
        'reason': reason,
    }


def _validated_external(config):
    memory = config.aethermind
    return {
        'source': 'external',
        'url': '' if memory.endpoints else (memory.url or ''),
        'endpoints': list(memory.endpoints or []),
        'external_error': None, 'external_error_name': None,
        'external_category': None, 'reason': 'External service validated',
    }


def _revalidate_cached(config, cached, clock, starter):
    """Probe a configured external service again, then relaunch a dead embedded URL."""
    source = cached.get('source') or ''
    url = cached.get('url') or ''
    if source == 'external' and (url or cached.get('endpoints')):
        try:
            probe_external(config)
        except _probe_error_types() as error:
            return _embedded_after_external_failure(config, error, clock, starter)
        return dict(cached)
    if source == 'embedded' and cached.get('external_error') and _wants_external(config.aethermind):
        return _recover_external(config, clock, starter, current=cached)
    if source != 'embedded' or not url:
        return None
    return _revive_embedded(config, cached, clock, starter)


def _remember_choice(key, choice):
    stored = _copy_choice(choice)
    if stored.get('url') or stored.get('endpoints'):
        with _LOCK:
            _CHOICES[key] = stored
    else:
        with _LOCK:
            _CHOICES.pop(key, None)


def choose_memory(config, probe=None, launch=None, clock=None):
    """Pick the memory service. External wins only when its probe succeeds.

    A cached choice is rechecked on every call: an external service is probed
    again, and an embedded engine this process started is relaunched when it
    has exited. The cache key is the service content, not the config object's
    identity, which a later config can reuse.
    """
    memory = config.aethermind
    clock = time.monotonic if clock is None else clock
    key = _choice_key(config)
    starter = launch or ensure_bundled_engine
    use_cache = probe is None and launch is None
    if use_cache:
        with _LOCK:
            cached = _CHOICES.get(key)
        if cached is not None:
            refreshed = _revalidate_cached(config, cached, clock, starter)
            if refreshed is not None:
                if (refreshed.get('source') or '') == 'embedded' and refreshed.get('url'):
                    refreshed = _revive_embedded(config, refreshed, clock, starter)
                _remember_choice(key, refreshed)
                return dict(refreshed)
            with _LOCK:
                _CHOICES.pop(key, None)
    configured = [item for item in (memory.endpoints or []) if item]
    if not configured and memory.url:
        configured = [memory.url]
    checker = probe or probe_external
    if _wants_external(memory) and not configured:
        def missing_external(checked):
            raise ValueError('No external service is configured')

        checker = missing_external
    if _wants_external(memory):
        try:
            checker(config)
        except _probe_error_types() as error:
            choice = _embedded_after_external_failure(config, error, clock, starter)
        else:
            choice = _validated_external(config)
    else:
        url, start_error = _launch(starter, config, clock)
        source = getattr(memory, 'source', '') or ''
        if url and source == 'embedded':
            reason = 'Embedded engine selected'
        elif url:
            reason = 'No external service configured'
        elif start_error and source == 'embedded':
            reason = 'Embedded engine selected; embedded engine did not start: ' + start_error
        elif start_error:
            reason = 'Embedded engine did not start: ' + start_error
        elif source == 'embedded':
            reason = unstarted_engine_reason(config)
        else:
            reason = unstarted_engine_reason(config)
        choice = {
            'source': 'embedded', 'url': url, 'endpoints': [],
            'external_error': None, 'external_error_name': None, 'external_category': None,
            'reason': reason,
        }
    if choice.get('source') == 'embedded' and choice.get('url'):
        choice = _revive_embedded(config, choice, clock, starter)
    if use_cache:
        _remember_choice(key, choice)
    return choice


def stop_bundled_engine(config):
    """Stop the engine this process started. A missing bundle is a labeled result."""
    try:
        root = _engine_root(config)
    except OSError as error:
        return {'stopped': False, 'reason': 'Embedded engine path could not be read: ' + type(error).__name__}
    if root is None or not root.exists():
        return {'stopped': False, 'reason': unstarted_engine_reason(config)}
    try:
        runtime = str(_runtime_root(config, root))
    except FileNotFoundError:
        return {'stopped': False, 'reason': unstarted_engine_reason(config)}
    except OSError as error:
        return {'stopped': False, 'reason': 'Embedded engine path could not be read: ' + type(error).__name__}
    with _LOCK:
        state = _REGISTRY.pop(runtime, None)
    if state is None:
        return {'stopped': False, 'reason': 'Embedded engine is not running'}
    proc = state.get('proc')
    try:
        running = proc is not None and proc.poll() is None
    except Exception as error:
        _finish_log(state)
        memory = config.aethermind
        label = _error_label(error, [getattr(memory, 'token_env', '') or '', 'AETHERMIND_TOKEN'])
        return {'stopped': False, 'reason': 'Embedded engine could not be inspected: ' + label}
    if not running:
        _finish_log(state)
        return {'stopped': False, 'reason': 'Embedded engine has already exited'}
    proc.terminate()
    _finish_log(state)
    return {'stopped': True, 'reason': 'Embedded engine stopped'}
