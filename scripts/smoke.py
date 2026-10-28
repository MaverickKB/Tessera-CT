#!/usr/bin/env python3
"""Headless smoke: start the core, check health, run one mocked turn.

Exits 0 only when GET /api/status answers, one session turn completes
against a local fake model, and that turn's capture is acknowledged by the
embedded AetherMind. Any other outcome exits non-zero and names the step
that failed. Ports are chosen by the operating system. The model name comes
from demo/fake_config.toml. The written config omits aethermind's enabled
key. It points bundled_path at a copy of the vendored engine plus the same
build receipt a wheel build writes, and the turn provisions that engine the
way a clean install does. A failure to start the engine fails this smoke
with that error.
"""

import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))

from fake_backend import FakeBackend  # noqa: E402
from tessera.aethermind import EMBEDDED_LOCK_REFUSAL  # noqa: E402
from tessera.procutil import kill_group, popen  # noqa: E402


def fail(message):
    print(f'smoke: {message}', file=sys.stderr)
    raise SystemExit(1)


def readline_within(stream, seconds):
    box = []

    def read():
        box.append(stream.readline())

    thread = threading.Thread(target=read, daemon=True)
    thread.start()
    thread.join(seconds)
    if thread.is_alive() or not box:
        return ''
    return box[0]


class Fixture:
    def __init__(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        backend = FakeBackend([self._reply] * 20)

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get('Content-Length') or 0)
                body = json.loads(self.rfile.read(length) or b'{}')
                reply = backend.chat(body.get('messages') or [], tools=body.get('tools'))
                message = {'role': 'assistant', 'content': reply.get('content') or ''}
                raw = json.dumps({'choices': [{'message': message}], 'usage': {}}).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @staticmethod
    def _reply(messages, tools):
        last = messages[-1] if messages else {}
        text = last.get('content') if isinstance(last.get('content'), str) else ''
        return {'content': 'Local fake reply: ' + text}

    @property
    def url(self):
        return f'http://127.0.0.1:{self.server.server_port}/v1'

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def request(base, method, path, body=None, timeout=20):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, method=method)
    req.add_header('Content-Type', 'application/json')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as error:
        raw = error.read()
        try:
            payload = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            payload = None
        return error.code, payload


def stop_core(process):
    """Stop the core and the engine child it started.

    On Windows ``Process.kill`` leaves that child holding its venv interpreter,
    and removing the smoke home then fails with an access error that hides the
    turn's own failure. ``taskkill /T`` stops the tree.
    """
    if process.poll() is not None:
        return process.returncode
    stopped = None
    if os.name == 'nt':
        try:
            kill_group(process.pid)
        except ProcessLookupError:
            pass
        except OSError as error:
            stopped = error
    else:
        process.send_signal(signal.SIGINT)
    try:
        code = process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        if stopped is not None:
            raise stopped
        process.kill()
        try:
            code = process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            fail('the core did not exit after it was asked to stop')
    if stopped is not None and process.poll() is None:
        raise stopped
    return code


def remove_home(path):
    """Remove the smoke home. A just-killed Windows interpreter can stay locked briefly."""
    last = None
    for _ in range(10):
        try:
            shutil.rmtree(path)
            return
        except PermissionError as error:
            last = error
            time.sleep(0.2)
    if last is not None:
        raise last


def _toml_basic(value):
    escaped = str(value).replace('\\', '\\\\').replace('"', '\\"')
    return f'"{escaped}"'


def prepare_bundled_engine(home):
    """Copy the vendored engine and write the wheel build's receipt beside it.

    A checkout has the engine bytes and no receipt. A wheel build writes the
    receipt next to those bytes. This copy is that layout, so discovery and
    provisioning follow the clean-install path.
    """
    from build_support import write_build_receipt
    from tessera.engine_manifest import vendored_dir

    dest = Path(home) / 'aethermind-engine'
    shutil.copytree(vendored_dir(ROOT), dest)
    write_build_receipt(dest, ROOT)
    return dest


def write_config(home):
    """Write the smoke config. It has no aethermind enabled key.

    ``bundled_path`` is the vendored engine plus a build receipt. The project
    store does not exist yet, so the first capture is allowed to create it.
    """
    home = Path(home)
    bundle = prepare_bundled_engine(home)
    store = home / 'memory'
    written = []
    in_memory = False
    for line in (ROOT / 'demo' / 'fake_config.toml').read_text(encoding='utf-8').splitlines():
        stripped = line.strip()
        if stripped.startswith('[') and stripped.endswith(']'):
            in_memory = stripped == '[aethermind]'
            written.append(line)
            if in_memory:
                written.append('bundled_path = ' + _toml_basic(bundle))
                written.append('create_project = true')
                written.append('project = ' + _toml_basic(store))
            continue
        if not in_memory:
            written.append(line)
            continue
        if stripped == 'enabled' or stripped.startswith('enabled ') or stripped.startswith('enabled='):
            continue
        if stripped.startswith('project ') or stripped.startswith('project='):
            continue
        written.append(line)
    (home / 'config.toml').write_text('\n'.join(written) + '\n', encoding='utf-8')
    return bundle


def memory_journals(home):
    texts = []
    for journal in sorted(Path(home).glob('sessions/*/outbox/outbox.jsonl')):
        texts.append(journal.read_text(encoding='utf-8'))
    return '\n'.join(texts)


def _journal_records(home):
    records = []
    for line in memory_journals(home).splitlines():
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def memory_write_acknowledged(home):
    return any(record.get('event') == 'ack' for record in _journal_records(home))


def memory_write_failure(home):
    failures = [record for record in _journal_records(home) if record.get('event') == 'failure']
    return failures[-1] if failures else None


def report_turn():
    print('smoke: turn ok')


def report_memory():
    print('smoke: memory write ok')


def runtime_notes(home):
    """Pip output and the engine log. The core's stderr is a file so it cannot fill a pipe."""
    parts = []
    stderr_path = Path(home) / 'core-stderr.log'
    if stderr_path.is_file():
        text = stderr_path.read_text(encoding='utf-8', errors='replace')[-2000:].strip()
        if text:
            parts.append(text)
    for log in sorted(Path(home).glob('runtime/aethermind/*/engine.log')):
        text = log.read_text(encoding='utf-8', errors='replace')[-2000:].strip()
        if text:
            parts.append(text)
    return '\n'.join(parts)


def fail_with_notes(home, message):
    notes = runtime_notes(home)
    fail(message if not notes else f'{message}\n{notes}')


def main():
    fixture = Fixture()
    home_parent = tempfile.TemporaryDirectory(prefix='tessera-smoke-', ignore_cleanup_errors=True)
    try:
        home = Path(home_parent.name)
        write_config(home)
        env = {key: value for key, value in os.environ.items() if not key.startswith('TESSERA_')}
        env['TESSERA_HOME'] = str(home)
        env['TESSERA_CWD'] = str(home)
        env['TESSERA_BACKENDS__FAKE__BASE_URL'] = fixture.url
        env['PYTHONUNBUFFERED'] = '1'
        stderr_path = home / 'core-stderr.log'
        stderr_handle = stderr_path.open('w', encoding='utf-8', errors='replace')
        process = popen([sys.executable, '-m', 'tessera', 'serve'], cwd=ROOT, env=env,
                        stdout=subprocess.PIPE, stderr=stderr_handle, text=True)
        try:
            line = readline_within(process.stdout, 20)
            if not line.startswith('Serving Tessera at '):
                fail_with_notes(home, 'the core did not announce an address')
            base = line.removeprefix('Serving Tessera at ').strip()
            status, payload = request(base, 'GET', '/api/status')
            if status != 200 or not isinstance(payload, dict) or 'session_count' not in payload:
                fail(f'health check failed (HTTP {status})')
            host = payload.get('host') if isinstance(payload.get('host'), dict) else {}
            if not host.get('platform') or not host.get('limits'):
                fail('health check did not name the host platform and its limits')
            print(f'smoke: health ok ({host["platform"]})')
            created, session = request(base, 'POST', '/api/sessions', {'title': 'smoke'})
            if created != 201 or not isinstance(session, dict) or not session.get('slug'):
                fail(f'session create failed (HTTP {created})')
            slug = session['slug']
            try:
                queued, turn = request(base, 'POST', f'/api/sessions/{slug}/messages',
                                       {'text': 'Say hello from the smoke test'}, timeout=180)
            except (TimeoutError, urllib.error.URLError) as error:
                fail_with_notes(home, f'turn submit failed ({type(error).__name__}: {error})')
            if queued not in (200, 202) or not isinstance(turn, dict):
                fail_with_notes(home, f'turn submit failed (HTTP {queued})')
            deadline = time.monotonic() + 180
            reply = ''
            while time.monotonic() < deadline:
                code, messages = request(base, 'GET', f'/api/sessions/{slug}/messages')
                if code == 200 and isinstance(messages, list):
                    for message in messages:
                        text = message.get('text') or ''
                        if message.get('kind') == 'agent' and text.startswith('Turn failed'):
                            fail_with_notes(home, text)
                        if message.get('kind') == 'agent' and text == EMBEDDED_LOCK_REFUSAL:
                            fail(text)
                        if message.get('kind') == 'agent' and 'Local fake reply:' in text:
                            reply = text
                if reply:
                    failure = memory_write_failure(home)
                    if failure and not memory_write_acknowledged(home):
                        detail = failure.get('detail') or failure.get('error') or 'write failed'
                        fail(f'AetherMind write failed: {detail}')
                    if memory_write_acknowledged(home):
                        break
                if process.poll() is not None:
                    fail('the core exited during the turn')
                time.sleep(0.05)
            if not reply:
                fail_with_notes(home, 'the mocked turn did not complete')
            if not memory_write_acknowledged(home):
                journal = memory_journals(home).strip()
                notes = '\n'.join(part for part in (journal[-2000:] if journal else '', runtime_notes(home)) if part)
                fail('the turn completed without an AetherMind write' + (f'\n{notes}' if notes else ''))
            report_turn()
            report_memory()
        finally:
            pending = sys.exc_info()[0]
            try:
                code = stop_core(process)
            except OSError as error:
                code = process.poll()
                if pending is None:
                    raise
                print(f'smoke: {error}', file=sys.stderr)
            finally:
                stderr_handle.close()
            if pending is None and os.name != 'nt' and code not in (0, 130, -2):
                fail(f'the core exited with status {code}')
    finally:
        fixture.close()
        pending = sys.exc_info()[0]
        try:
            remove_home(home_parent.name)
        except PermissionError as error:
            if pending is None:
                fail(f'could not remove the smoke home: {error}')
            print(f'smoke: could not remove the smoke home: {error}', file=sys.stderr)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
