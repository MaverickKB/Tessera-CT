"""Scripted model and a loopback HTTP fixture. No credentials or external calls."""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import copy
import threading


class FakeBackend:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self._lock = threading.Lock()

    def chat(self, messages, tools=None, **kw):
        with self._lock:
            self.calls.append({'messages': copy.deepcopy(messages), 'tools': copy.deepcopy(tools), **kw})
            if not self.script:
                raise AssertionError('FakeBackend script exhausted')
            response = self.script.pop(0)
        if isinstance(response, Exception):
            raise response
        if callable(response):
            response = response(messages, tools)
        if isinstance(response, str):
            response = {'content': response}
        result = {'content': '', 'tool_calls': [], 'usage': {}}
        result.update(response)
        return result


@contextmanager
def fake_http_server(script):
    pending = list(script)
    received = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers['Content-Length']))
            received.append({'path': self.path, 'body': json.loads(body), 'authorization': self.headers.get('Authorization')})
            status, response = pending.pop(0)
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Retry-After', '0')
            self.end_headers()
            self.wfile.write(json.dumps(response).encode())

        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/v1', received
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
