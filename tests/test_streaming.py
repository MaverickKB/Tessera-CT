"""Streamed thought/reply text: assembled faithfully, reported live, never persisted as events."""
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import threading

from tessera.backend import Backend
from tessera.providers import ChatCompletionsAdapter

CHUNKS = [
    {'choices': [{'delta': {'reasoning_content': 'Weigh '}}]},
    {'choices': [{'delta': {'reasoning_content': 'options.'}}]},
    {'choices': [{'delta': {'content': 'Hel'}}]},
    {'choices': [{'delta': {'content': 'lo.'}, 'finish_reason': 'stop'}]},
]


def sse(chunks):
    return [f'data: {json.dumps(c)}\n'.encode() for c in chunks] + [b'data: [DONE]\n']


def test_assemble_folds_a_stream_into_one_reply_and_reports_deltas():
    seen = []
    data = ChatCompletionsAdapter.assemble(sse(CHUNKS + [{'choices': [{'delta': {'tool_calls': [
        {'index': 0, 'id': 'c1', 'function': {'name': 'wip_board', 'arguments': '{}'}}]}}]}]), lambda k, t: seen.append((k, t)))
    result = ChatCompletionsAdapter().decode(data)
    assert result['content'] == 'Hello.' and result['reasoning'] == 'Weigh options.'
    assert result['tool_calls'] == [{'id': 'c1', 'name': 'wip_board', 'arguments': {}}]
    assert seen[:2] == [('thought', 'Weigh '), ('thought', 'options.')] and ('tool', 'wip_board') in seen


def test_backend_streams_from_a_real_http_endpoint():
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            assert body['stream'] is True
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            for line in sse(CHUNKS):
                self.wfile.write(line + b'\n')
                self.wfile.flush()

        def log_message(self, *args):
            pass
    server = HTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        backend = Backend('stub', f'http://127.0.0.1:{server.server_port}/v1', 'm', extra={'retries': 0})
        seen = []
        backend.on_delta = lambda k, t: seen.append(k)
        result = backend.chat([{'role': 'user', 'content': 'hi'}])
        assert result['content'] == 'Hello.' and result['reasoning'] == 'Weigh options.'
        assert seen.count('thought') == 2 and seen.count('text') == 2
        backend.on_delta = None
    finally:
        server.shutdown()


import io
import pytest

from tessera.backend import _StreamRedactor


@pytest.mark.parametrize('parts', [
    ['synthetic-secret'],
    ['synthetic-', 'secret'],
    ['syn', 'thetic-', 'secret'],
    list('synthetic-secret'),
])
def test_backend_redacts_whole_and_split_stream_credentials(monkeypatch, parts):
    secret = 'synthetic-secret'
    monkeypatch.setenv('STREAM_TEST_KEY', secret)
    chunks = []
    for part in parts:
        chunks.append({'choices': [{'delta': {'reasoning_content': part, 'content': part}}]})
    response = io.BytesIO(b''.join(sse(chunks)))
    response.headers = {'Content-Type': 'text/event-stream'}
    backend = Backend('stub', 'http://localhost/v1', 'm', 'STREAM_TEST_KEY', {'retries': 0})
    monkeypatch.setattr(backend._opener, 'open', lambda *a, **kw: response)
    seen = []
    backend.on_delta = lambda k, t: seen.append((k, t))
    result = backend.chat([])
    for kind in ('thought', 'text'):
        output = ''.join(t for k, t in seen if k == kind)
        assert output == '[REDACTED]'
        assert secret not in output
        assert not any(secret[:n] in output for n in range(1, len(secret)))
    assert result['content'] == '[REDACTED]'
    assert result['reasoning'] == '[REDACTED]'


def test_backend_without_credential_preserves_stream_chunks(monkeypatch):
    response = io.BytesIO(b''.join(sse(CHUNKS)))
    response.headers = {'Content-Type': 'text/event-stream'}
    backend = Backend('stub', 'http://localhost/v1', 'm', extra={'retries': 0})
    monkeypatch.setattr(backend._opener, 'open', lambda *a, **kw: response)
    seen = []
    backend.on_delta = lambda k, t: seen.append((k, t))
    result = backend.chat([])
    assert [(k, t) for k, t in seen if k in ('thought', 'text')] == [
        ('thought', 'Weigh '), ('thought', 'options.'), ('text', 'Hel'), ('text', 'lo.')]
    assert result['content'] == 'Hello.'


def test_stream_redactor_bounds_tails_and_keeps_kinds_separate():
    seen = []
    redactor = _StreamRedactor(lambda k, t: seen.append((k, t)), 'abcdef')
    redactor('thought', 'abc')
    redactor('text', 'def')
    redactor('tool', 'x' * 10000)
    assert all(len(t) <= 5 for t in redactor.pending.values())
    redactor.flush()
    assert ''.join(t for k, t in seen if k == 'thought') == 'abc'
    assert ''.join(t for k, t in seen if k == 'text') == 'def'
    assert ''.join(t for k, t in seen if k == 'tool') == 'x' * 10000
    assert redactor.pending == {}


@pytest.mark.parametrize('secret', ['a', 'aaa', 'abab', 'abcdef'])
def test_stream_redactor_matches_final_redaction_at_every_split(secret):
    from tessera.credentials import redact_secret
    text = 'before ' + secret * 2 + ' after'
    for split in range(len(text) + 1):
        seen = []
        redactor = _StreamRedactor(lambda k, t: seen.append(t), secret)
        redactor('text', text[:split])
        redactor('text', text[split:])
        redactor.flush()
        assert ''.join(seen) == redact_secret(text, secret)
