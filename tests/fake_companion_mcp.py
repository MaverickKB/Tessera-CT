"""Loopback fake MCP server for companion tests. No live Atlas, Ember or model."""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time


class FakeCompanion:
    def __init__(self, tools=None, delay=0.0):
        self.tools = dict(tools or {})
        self.delay = delay
        self.calls = []
        self.methods = []


@contextmanager
def fake_companion(tools=None, delay=0.0):
    state = FakeCompanion(tools, delay)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *args):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            state.methods.append(request['method'])
            if request['method'] == 'notifications/initialized':
                self.send_response(202)
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            if state.delay:
                time.sleep(state.delay)
            if request['method'] == 'initialize':
                result = {'protocolVersion': '2025-03-26', 'capabilities': {'tools': {}}}
            elif request['method'] == 'tools/call':
                name = request['params']['name']
                state.calls.append((name, request['params']['arguments']))
                text = state.tools.get(name, 'ok:' + name)
                result = {'content': [{'type': 'text', 'text': text}]}
            else:
                result = {}
            body = json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}).encode()
            try:
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.url = 'http://127.0.0.1:%d/mcp' % server.server_address[1]
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
