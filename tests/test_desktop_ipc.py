"""The desktop shell owns core startup, loopback API access and child shutdown."""

from contextlib import contextmanager
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import urlsplit

import pytest

from tessera.config import Config
from tessera import desktop as desktop_module
from tessera import server as server_module
from integration_helpers import FakeMcpServer
from test_server import read_event


ROOT = Path(__file__).resolve().parents[1]


def _ready_line(child, timeout):
    """The first stdout line.

    ``select`` on macOS only watches descriptors below ``FD_SETSIZE``. The
    pytest process can already be past that, so the ready bytes sit in the
    pipe while ``select`` times out. ``readline`` does not have that cap.
    """
    box = []

    def read_line():
        box.append(child.stdout.readline())

    reader = threading.Thread(target=read_line, daemon=True)
    reader.start()
    reader.join(timeout)
    if reader.is_alive() or not box or not box[0]:
        return None
    line = box[0]
    return line.encode() if isinstance(line, str) else line


def _finish_desktop_child(child, timeout):
    """Wait until the child exits. stderr is a file, so it cannot fill a pipe."""
    try:
        stdout, _stderr = child.communicate(timeout=timeout)
        timed_out = False
    except subprocess.TimeoutExpired:
        child.kill()
        stdout, _stderr = child.communicate()
        timed_out = True
    stdout = getattr(child, '_stdout_rest', b'') + (stdout or b'')
    handle = getattr(child, '_stderr_file', None)
    if handle is not None:
        handle.seek(0)
        stderr = handle.read()
    else:
        stderr = b''
    return stdout, stderr, timed_out


@contextmanager
def desktop_child(home):
    env = {}
    for key, value in os.environ.items():
        if key.startswith(('TESSERA_', 'OPENAI_', 'ANTHROPIC_', 'XAI_')):
            continue
        if key.upper() in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY'):
            continue
        env[key] = value
    env['TESSERA_SERVER_TOKEN'] = 'unused-desktop-token-canary'
    env['DESKTOP_VOICE_TEST_KEY'] = 'provider-key-canary'
    stderr_file = tempfile.TemporaryFile()
    child = subprocess.Popen([sys.executable, '-m', 'tessera', '--home', str(home),
                              'desktop', '--host', '127.0.0.1', '--port', '0'],
                             cwd=ROOT, env=env, stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=stderr_file)
    child._stderr_file = stderr_file
    child._stdout_rest = b''
    announced = False
    try:
        line = _ready_line(child, 20)
        if not line:
            stderr_file.seek(0)
            raise AssertionError('Desktop child did not announce readiness: '
                                 + stderr_file.read().decode('utf-8', 'replace')[-400:])
        ready = json.loads(line)
        assert ready['type'] == 'tessera.ready' and ready['version'] == 1
        assert ready['transport'] == 'loopback-http' and ready['pid'] == child.pid
        parsed = urlsplit(ready['url'])
        assert parsed.scheme == 'http' and ipaddress.ip_address(parsed.hostname).is_loopback
        assert parsed.port > 0 and not parsed.username and not parsed.password
        assert set(ready) == {'type', 'version', 'transport', 'url', 'pid'}
        announced = True
        yield child, ready
    finally:
        if child.stdin is not None:
            try:
                child.stdin.close()
            except BrokenPipeError:
                pass
            child.stdin = None
        timed_out = False
        if child.poll() is None:
            _stdout, _stderr, timed_out = _finish_desktop_child(child, 15)
        stderr_file.close()
        if timed_out and announced:
            pytest.fail('Desktop child did not exit after its owner closed stdin')


def request(origin, method, path, body=None):
    address = urlsplit(origin)
    connection = http.client.HTTPConnection(address.hostname, address.port, timeout=10)
    try:
        # Shaped like the desktop window: the window loads the core origin itself.
        headers = {'Content-Type': 'application/json', 'Origin': f'{address.scheme}://{address.netloc}'}
        connection.request(method, path, body=None if body is None else json.dumps(body), headers=headers)
        response = connection.getresponse()
        raw = response.read()
        assert b'provider-key-canary' not in raw and b'unused-desktop-token-canary' not in raw
        return response.status, json.loads(raw)
    finally:
        connection.close()


def test_bind_does_not_reverse_resolve_the_loopback_address(tmp_path, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError('socket.getfqdn')

    monkeypatch.setattr(socket, 'getfqdn', refuse)
    config = Config(home=tmp_path)
    config.jev.enabled = False
    config.aethermind.bundled_path = str(tmp_path / 'no-engine')
    with server_module.create_server(config) as server:
        host, port = server.server_address[:2]
        assert server.server_name == host and server.server_port == port
        assert ipaddress.ip_address(host).is_loopback and port > 0


def test_desktop_defaults_use_an_assigned_loopback_port(tmp_path):
    settings = Config(home=tmp_path).server
    assert ipaddress.ip_address(settings.host).is_loopback
    assert settings.port == 0


def test_legacy_token_cannot_enable_a_nonloopback_listener(tmp_path, monkeypatch):
    config = Config(home=tmp_path)
    config.server.host = '0.0.0.0'
    monkeypatch.setenv('TESSERA_SERVER_TOKEN', 'legacy-token-canary')
    manager = Mock(side_effect=AssertionError('Reject before loading sessions'))
    listener = Mock(side_effect=AssertionError('Reject before opening a listener'))
    monkeypatch.setattr(server_module, 'SessionManager', manager)
    monkeypatch.setattr(server_module, 'TesseraServer', listener)
    with pytest.raises(ValueError, match='loopback'):
        server_module.create_server(config)
    manager.assert_not_called()
    listener.assert_not_called()


def test_desktop_child_owns_crud_sse_and_python_voice_mint(tmp_path):
    received = []

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            received.append((self.path, self.headers.get('Authorization'), body))
            if self.path == '/mint-fixture':
                value = {'value': 'ephemeral-desktop-fixture', 'expires_at': time.time() + 30}
            else:
                value = {'choices': [{'message': {'content': 'Desktop reply'}, 'finish_reason': 'stop'}],
                         'usage': {'total_tokens': 3}}
            raw = json.dumps(value).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    with FakeMcpServer(dialect='path') as memory, ThreadingHTTPServer(('127.0.0.1', 0), Provider) as upstream:
        worker = threading.Thread(target=upstream.serve_forever, daemon=True)
        worker.start()
        try:
            base_url = 'http://' + upstream.server_address[0] + ':' + str(upstream.server_address[1])
            home = tmp_path / 'desktop-home'
            home.mkdir()
            (home / 'config.toml').write_text(f'''[backends.fixture]
base_url={json.dumps(base_url)}
model="model-placeholder"
[roles]
main="fixture"
fast="fixture"
[aethermind]
endpoints = [{json.dumps(memory.url)}]
probe_timeout = 0.5
probe_interval = 0
[voice]
enabled=true
websocket_url="wss://voice.example.test/realtime"
model="voice-placeholder"
api_base_url={json.dumps(base_url)}
client_secret_path="/mint-fixture"
client_protocol_prefix="ephemeral-fixture."
api_key_env="DESKTOP_VOICE_TEST_KEY"
''')
            with desktop_child(home) as (child, ready):
                url = ready['url']
                assert request(url, 'GET', '/api/status')[0] == 200 and received == []
                status, metadata = request(url, 'POST', '/api/sessions', {'title': 'Desktop fixture'})
                assert status == 201 and received == []
                slug = metadata['slug']
                assert request(url, 'POST', f'/api/sessions/{slug}/messages', {'text': 'Reply briefly'})[0] == 202
                address = urlsplit(url)
                events = http.client.HTTPConnection(address.hostname, address.port, timeout=10)
                try:
                    events.request('GET', f'/api/sessions/{slug}/events')
                    stream = events.getresponse()
                    assert stream.status == 200
                    deadline = time.monotonic() + 15
                    finished = False
                    while time.monotonic() < deadline:
                        if read_event(stream).get('event') == 'turn_end':
                            finished = True
                            break
                    assert finished
                finally:
                    events.close()
                messages = request(url, 'GET', f'/api/sessions/{slug}/messages')[1]
                assert messages[-1]['text'] == 'Desktop reply'
                assert request(url, 'GET', f'/api/sessions/{slug}/view')[0] == 200
                status, lease = request(url, 'POST', f'/api/sessions/{slug}/voice/start', {})
                assert status == 200
                assert lease['protocols'] == ['ephemeral-fixture.ephemeral-desktop-fixture']
                mint = [row for row in received if row[0] == '/mint-fixture']
                assert len(mint) == 1 and mint[0][1] == 'Bearer provider-key-canary'
                assert mint[0][2] == {'expires_after': {'seconds': 60}}
                assert request(url, 'POST', f'/api/sessions/{slug}/voice/stop', {'voice_id': lease['voice_id']}) == (200, {'stopped': True})
                child.stdin.close()
                child.stdin = None
                stdout, stderr, timed_out = _finish_desktop_child(child, 15)
                assert not timed_out
                assert child.returncode == 0 and stdout == b'' and stderr == b''
        finally:
            upstream.shutdown()
            worker.join(timeout=5)


def test_desktop_child_handles_parent_termination(tmp_path):
    with desktop_child(tmp_path / 'home') as (child, ready):
        assert request(ready['url'], 'GET', '/api/status')[0] == 200
        child.terminate()
        child.stdin = None
        stdout, stderr, timed_out = _finish_desktop_child(child, 15)
        assert not timed_out
        assert child.returncode == 0 and stdout == b'' and stderr == b''


def test_eintr_retries_the_stdin_read(monkeypatch):
    """An interrupted poll or read is not EOF. The core keeps reading."""
    calls = {'select': 0, 'read': 0}

    def fake_select(reads, writes, excepts, timeout):
        calls['select'] += 1
        if calls['select'] == 1:
            raise InterruptedError
        return [0], [], []

    def fake_read(fd, size):
        calls['read'] += 1
        if calls['read'] == 1:
            raise InterruptedError
        return b''

    monkeypatch.setattr(desktop_module.os, 'name', 'posix')
    monkeypatch.setattr(desktop_module.select, 'select', fake_select)
    monkeypatch.setattr(desktop_module.os, 'read', fake_read)
    monkeypatch.setattr(desktop_module.sys.stdin, 'fileno', lambda: 0)
    desktop_module._wait_for_stdin_eof()
    assert calls['select'] >= 2
    assert calls['read'] >= 2


def test_a_zero_mode_pipe_matches_the_leaked_stdin_writer(monkeypatch):
    """macOS fstat on a pipe zeroes st_mode and keeps the shared inode."""
    closed = []

    def fstat(fd):
        if fd == 0:
            return SimpleNamespace(st_mode=0, st_ino=99, st_dev=7)
        if fd == 8:
            return SimpleNamespace(st_mode=0, st_ino=99, st_dev=7)
        if fd == 4:
            return SimpleNamespace(st_mode=stat.S_IFIFO, st_ino=3, st_dev=7)
        raise OSError(9, 'bad fd')

    monkeypatch.setattr(os, 'fstat', fstat)
    monkeypatch.setattr(os, 'listdir', lambda path: ['0', '1', '2', '4', '8'])
    monkeypatch.setattr(os, 'close', lambda fd: closed.append(fd))
    desktop_module._drop_leaked_stdin_writer()
    assert closed == [8]


def test_darwin_pipe_peer_is_the_leaked_stdin_writer(monkeypatch):
    """The write end is the fd whose handle is stdin's peer, not a shared inode."""
    closed = []

    def ends(fd):
        if fd == 0:
            return (10, 20)
        if fd == 6:
            return (20, 10)
        if fd == 7:
            return (30, 40)
        return None

    monkeypatch.setattr(os, 'fstat', lambda fd: SimpleNamespace(st_mode=stat.S_IFREG, st_ino=1, st_dev=1))
    monkeypatch.setattr(os, 'listdir', lambda path: ['6', '7'])
    monkeypatch.setattr(os, 'close', lambda fd: closed.append(fd))
    monkeypatch.setattr(desktop_module, '_darwin_pipe_ends', ends)
    desktop_module._drop_leaked_stdin_writer()
    assert closed == [6]


def test_a_regular_stdin_is_not_scanned_for_a_leaked_writer(monkeypatch):
    def fstat(fd):
        if fd == 0:
            return SimpleNamespace(st_mode=stat.S_IFREG, st_ino=5, st_dev=1)
        raise AssertionError('scanned fd %s' % fd)

    monkeypatch.setattr(os, 'fstat', fstat)
    monkeypatch.setattr(os, 'listdir', lambda path: ['4'])
    desktop_module._drop_leaked_stdin_writer()


@pytest.mark.skipif(os.name == 'nt', reason='POSIX pass_fds is the inherited write end of the stdin pipe')
def test_desktop_child_exits_when_it_inherited_the_stdin_write_end(tmp_path):
    """The child holds its own copy of the pipe's write end, as macOS posix_spawn can.

    Closing the parent's end is EOF only after the child drops that copy.
    """
    read_fd, write_fd = os.pipe()
    os.set_inheritable(read_fd, True)
    os.set_inheritable(write_fd, True)
    home = tmp_path / 'home'
    home.mkdir()
    env = {}
    for key, value in os.environ.items():
        if key.startswith(('TESSERA_', 'OPENAI_', 'ANTHROPIC_', 'XAI_')):
            continue
        env[key] = value
    child = subprocess.Popen(
        [sys.executable, '-m', 'tessera', '--home', str(home), 'desktop',
         '--host', '127.0.0.1', '--port', '0'],
        cwd=ROOT, env=env, stdin=read_fd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        pass_fds=(write_fd,))
    os.close(read_fd)
    try:
        line = child.stdout.readline()
        assert json.loads(line)['type'] == 'tessera.ready'
        os.close(write_fd)
        write_fd = -1
        try:
            _stdout, stderr = child.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.communicate()
            raise
        assert child.returncode == 0, stderr
    finally:
        if write_fd >= 0:
            os.close(write_fd)
        if child.poll() is None:
            child.kill()
            child.communicate()
