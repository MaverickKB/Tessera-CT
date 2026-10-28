import pytest

from fake_backend import fake_http_server
from tessera.backend import Backend, BackendError


@pytest.mark.parametrize("finish_reason", ["length", "content_filter", "stop"])
def test_http_backend_preserves_finish_reason_for_the_turn_loop(finish_reason):
    response = {"choices": [{"message": {"content": "A plausible answer."},
                             "finish_reason": finish_reason}]}
    with fake_http_server([(200, response)]) as (url, received):
        backend = Backend("offline-fixture", url, "model-placeholder", extra={"retries": 0})
        reply = backend.chat([{"role": "user", "content": "Synthetic request"}])
    assert reply["finish_reason"] == finish_reason
    assert reply["content"] == "A plausible answer."
    assert len(received) == 1 and received[0]["authorization"] is None


def test_http_retry_tool_decoding_and_usage(monkeypatch):
    response = {'choices': [{'message': {'content': None, 'tool_calls': [{'id': 'call-1', 'type': 'function', 'function': {'name': 'date', 'arguments': '{"i":0}'}}]}}], 'usage': {'total_tokens': 12}}
    monkeypatch.setenv('TEST_BACKEND_KEY', 'test-secret')
    with fake_http_server([(429, {}), (503, {}), (200, response)]) as (url, received):
        backend = Backend('test', url, 'model', 'TEST_BACKEND_KEY', {'retry_delay': 0})
        reply = backend.chat([{'role': 'user', 'content': 'hello'}])
    assert reply['tool_calls'] == [{'id': 'call-1', 'name': 'date', 'arguments': {'i': 0}}]
    assert reply['usage']['total_tokens'] == 12
    assert len(received) == 3
    assert received[0]['path'] == '/v1/chat/completions'
    assert received[0]['authorization'] == 'Bearer test-secret'
    assert received[0]['body']['model'] == 'model'


def test_http_auth_error_does_not_retry_or_expose_response(monkeypatch):
    monkeypatch.setenv('TEST_BACKEND_KEY', 'test-secret')
    with fake_http_server([(401, {'error': 'test-secret'})]) as (url, received):
        with pytest.raises(BackendError, match='401') as error:
            Backend('test', url, 'model', 'TEST_BACKEND_KEY', {}).chat([])
    assert 'test-secret' not in str(error.value)
    assert len(received) == 1


def test_backend_refuses_cross_origin_redirect_with_credentials(monkeypatch):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading
    leaked = []
    class Redirect(BaseHTTPRequestHandler):
        def do_POST(self):
            self.send_response(302)
            self.send_header('Location', target)
            self.end_headers()
        def log_message(self, *args):
            pass
    class Sink(BaseHTTPRequestHandler):
        def do_GET(self):
            leaked.append(self.headers.get('Authorization'))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"choices":[{"message":{"content":"redirected"}}]}')
        def log_message(self, *args):
            pass
    servers = [ThreadingHTTPServer(('127.0.0.1', 0), cls) for cls in (Redirect, Sink)]
    target = f'http://127.0.0.1:{servers[1].server_port}/steal'
    threads = []
    for server in servers:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        threads.append(thread)
    monkeypatch.setenv('REDIRECT_TEST_KEY', 'local-dummy-key')
    try:
        backend = Backend('test', f'http://127.0.0.1:{servers[0].server_port}', 'test', 'REDIRECT_TEST_KEY', {})
        with pytest.raises(BackendError, match='302'):
            backend.chat([])
        assert leaked == []
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join()
