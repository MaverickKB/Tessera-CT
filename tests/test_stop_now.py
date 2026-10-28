"""Stop must break blocked I/O and prevent the next side effect, not just set a flag."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import shlex
import sys
import threading
import time

import pytest

from fake_backend import FakeBackend
from test_loop import session_config
from tessera.backend import Backend, BackendError
from tessera.loop import Session
from tessera.sessions import SessionManager
from tessera.tools import build_registry


@contextmanager
def stalled_provider(headers):
    entered = threading.Event()
    release = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            if headers:
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
                self.wfile.write(b': connected\n\n')
                self.wfile.flush()
            entered.set()
            release.wait(5)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/v1', entered
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(2)


@pytest.mark.parametrize('headers', [False, True])
def test_stop_breaks_wait_for_headers_and_silent_sse_without_retry(headers):
    with stalled_provider(headers) as (url, entered):
        backend = Backend('local', url, 'test', extra={'timeout': 60})
        backend.on_delta = lambda *args: None
        failures = []

        def run():
            try:
                backend.chat([{'role': 'user', 'content': 'wait'}])
            except BackendError as error:
                failures.append(str(error))

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        assert entered.wait(2)
        started = time.monotonic()
        backend.cancel()
        thread.join(1)
        assert not thread.is_alive(), 'Stop must wake the blocked network reader'
        assert time.monotonic() - started < 1
        assert failures and 'cancelled' in failures[0]
        assert not backend._transport._connections


def wait_file(path):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.01)
    raise AssertionError('fixture process did not start')


def test_stop_kills_shell_and_prevents_later_tool_in_same_batch(tmp_path):
    marker = tmp_path / 'started'
    forbidden = tmp_path / 'must-not-run'
    script = f'from pathlib import Path; import time; Path({str(marker)!r}).touch(); time.sleep(30); Path({str(forbidden)!r}).touch()'
    command = shlex.quote(sys.executable) + ' -c ' + shlex.quote(script)
    backend = FakeBackend([{'tool_calls': [
        {'id': 'first', 'name': 'shell', 'arguments': {'command': command}},
        {'id': 'second', 'name': 'write_file', 'arguments': {'path': 'second-forbidden', 'text': 'bad'}},
    ]}])
    with Session(session_config(tmp_path), backend=backend) as session:
        thread = threading.Thread(target=lambda: session.run_turn('Run these tools'), daemon=True)
        thread.start()
        wait_file(marker)
        started = time.monotonic()
        assert session.interrupt()
        thread.join(1)
        assert not thread.is_alive()
        assert time.monotonic() - started < 1
        assert session.last_turn_interrupted
        assert not forbidden.exists()
        assert not (tmp_path / 'second-forbidden').exists()
        assert len(backend.calls) == 1


def test_stop_cancels_queue_and_next_human_prompt_resumes(tmp_path):
    entered = threading.Event()
    release = threading.Event()

    class Waiting:
        def chat(self, *args, **kwargs):
            entered.set()
            release.wait(5)
            return {'content': 'finished', 'tool_calls': []}

        def cancel(self):
            release.set()

    manager = SessionManager(session_config(tmp_path), backend_factory=lambda *args: Waiting())
    try:
        slug = manager.create(title='Stop fixture')['slug']
        manager.submit(slug, 'first')
        assert entered.wait(2)
        manager.submit(slug, 'queued')
        result = manager.interrupt(slug)
        assert result['cancelled_queued'] == 1
        runtime = manager.runtime(slug)
        assert runtime.wait(2)
        assert runtime.operator_stopped
        assert not manager.queued_turns(slug)
        manager.submit(slug, 'resume explicitly')
        assert runtime.wait(2)
        assert not runtime.operator_stopped
    finally:
        release.set()
        manager.close()


def test_stop_blocks_approved_but_deferred_action(tmp_path):
    registry = build_registry(cwd=tmp_path, approval='ask')
    pending = []
    registry.defer = lambda name, arguments, run: pending.append(run)
    registry.execute('write_file', {'path': 'forbidden', 'text': 'bad'})
    registry.cancel()
    registry.reset_cancel()  # A later turn must not revive an old approval.
    with pytest.raises(RuntimeError, match='cancelled'):
        pending[0]()
    assert not (tmp_path / 'forbidden').exists()


def test_stop_during_turn_admission_prevents_first_model_request(tmp_path):
    entered = threading.Event()
    release = threading.Event()
    backend = FakeBackend(['must not be requested'])

    class PausedAdmission(Session):
        def run_turn(self, *args, **kwargs):
            entered.set()
            release.wait(3)
            return super().run_turn(*args, **kwargs)

    manager = SessionManager(session_config(tmp_path), session_factory=PausedAdmission,
                             backend_factory=lambda *args: backend)
    try:
        slug = manager.create(title='Admission fixture')['slug']
        manager.submit(slug, 'must not execute')
        assert entered.wait(2)
        assert manager.interrupt(slug)['interrupted']
        release.set()
        assert manager.runtime(slug).wait(2)
        assert not backend.calls
        assert manager.runtime(slug).operator_stopped
    finally:
        release.set()
        manager.close()


def test_mcp_stop_breaks_call_and_notifies_remote_request_id():
    from tessera.mcp_client import McpClient, McpError
    entered = threading.Event()
    notified = threading.Event()
    release = threading.Event()
    request_ids = []
    cancelled_ids = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if payload['method'] == 'notifications/cancelled':
                cancelled_ids.append(payload['params']['requestId'])
                self.send_response(202)
                self.end_headers()
                notified.set()
                return
            request_ids.append(payload['id'])
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            self.wfile.write(b': tool running\n\n')
            self.wfile.flush()
            entered.set()
            release.wait(3)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    client = McpClient(f'http://127.0.0.1:{server.server_port}/mcp', token_env='', timeout=30)
    client._initialized = True
    registry = build_registry(approval='never')
    failures = []

    def run():
        try:
            client.call_tool_cancellable('waiting', {}, registry)
        except (McpError, RuntimeError) as error:
            failures.append(str(error))

    thread = threading.Thread(target=run, daemon=True)
    try:
        thread.start()
        assert entered.wait(2)
        registry.cancel()
        thread.join(1)
        assert not thread.is_alive()
        assert failures
        assert notified.wait(1)
        assert cancelled_ids == request_ids
    finally:
        release.set()
        server.shutdown()
        server.server_close()


def test_stdio_tool_stop_terminates_owned_server(tmp_path):
    from tessera.mcp_stdio import StdioMcpClient
    marker = tmp_path / 'mcp-started'
    script = tmp_path / 'fixture.py'
    script.write_text('import sys,time\nfrom pathlib import Path\nPath('+repr(str(marker))+').touch()\nfor line in sys.stdin: time.sleep(30)\n')
    client = StdioMcpClient([sys.executable, str(script)], timeout=30)
    client._start()
    client._initialized = True
    registry = build_registry(cwd=tmp_path, approval='never')
    failures = []

    def run():
        try:
            client.call_tool_cancellable('waiting', {}, registry)
        except Exception as error:
            failures.append(str(error))

    thread = threading.Thread(target=run, daemon=True)
    try:
        thread.start()
        wait_file(marker)
        registry.cancel()
        thread.join(1)
        assert not thread.is_alive()
        assert failures
        assert client._process is None or client._process.poll() is not None
    finally:
        client.close()


def test_parent_stop_cancels_delegated_http_turn(tmp_path):
    with stalled_provider(True) as (url, entered):
        child_backend = Backend('local', url, 'test', extra={'timeout': 60})
        with Session(session_config(tmp_path), backend=FakeBackend([]),
                     agent_backend_factory=lambda *args: child_backend) as session:
            session.agents.spawn('waiting-child', 'Wait for the model')
            assert entered.wait(2)
            assert session.interrupt()
            assert session.agents.wait(timeout=1), 'Parent Stop must not wait for a delegated model response'
            assert not session.agents.active()
