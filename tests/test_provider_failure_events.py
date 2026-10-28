"""Provider failures remain diagnosable after restart without raw error leakage."""

import io
import json
from urllib.error import HTTPError, URLError

import pytest

from tessera.backend import Backend, BackendError
from tessera.config import Config
from tessera.sessions import SessionManager, read_records


@pytest.mark.parametrize('failure,expected', [
    ('http-unavailable', {'error_category': 'provider', 'error_kind': 'http', 'http_status': 503}),
    ('http-auth', {'error_category': 'authentication', 'error_kind': 'http', 'http_status': 401}),
    ('invalid-json', {'error_category': 'provider', 'error_kind': 'parse'}),
    ('invalid-schema', {'error_category': 'provider', 'error_kind': 'parse'}),
    ('timeout', {'error_category': 'timeout', 'error_kind': 'timeout'}),
    ('connection', {'error_category': 'connection', 'error_kind': 'connection'}),
    ('incomplete', {'error_category': 'provider', 'error_kind': 'incomplete_response'}),
])
def test_provider_failure_kind_survives_session_close(tmp_path, monkeypatch, failure, expected):
    # Removing provider failure details at the session boundary must fail this
    # consumer, as must confusing HTTP, parse, timeout or incomplete responses.
    secret = 'synthetic-failure-credential'
    endpoint = 'https://provider.example.invalid/v1'
    monkeypatch.setenv('TEST_FAILURE_CREDENTIAL', secret)
    backend = Backend('private-selector-placeholder', endpoint, 'model-placeholder',
                      'TEST_FAILURE_CREDENTIAL', {'retries': 0})

    def transport(request, timeout):
        if failure.startswith('http-'):
            status = 401 if failure == 'http-auth' else 503
            raise HTTPError(request.full_url, status, secret, {}, io.BytesIO(secret.encode()))
        if failure == 'timeout':
            raise TimeoutError(secret + endpoint)
        if failure == 'connection':
            raise URLError(secret + endpoint)
        if failure == 'invalid-json':
            return io.BytesIO(b'not-json-' + secret.encode())
        if failure == 'invalid-schema':
            return io.BytesIO(json.dumps({'private': secret}).encode())
        return io.BytesIO(json.dumps({'choices': [{'message': {'content': secret},
                                                   'finish_reason': 'length'}]}).encode())

    monkeypatch.setattr(backend._opener, 'open', transport)
    manager = SessionManager(Config(home=tmp_path, cwd=tmp_path), backend_factory=lambda *_: backend)
    try:
        slug = manager.create('Provider failure fixture')['slug']
        turn = manager.submit(slug, 'Hello')
        manager.wait(slug, timeout=10)
        assert manager.turn(slug, turn)['state'] == 'error'
        root = manager.path(slug)
    finally:
        manager.close()

    # Read persisted records after the runtime is gone, not an in-memory status.
    events = read_records(root / 'events.jsonl')
    errors = [event['data'] for event in events if event['event'] == 'error']
    assert len(errors) == 1
    assert {key: errors[0][key] for key in expected} == expected
    assert set(errors[0]) == {'turn_id', 'error', 'at', *expected}
    assert errors[0]['turn_id'] == turn
    assert events[-1]['event'] == 'turn_end' and events[-1]['data']['success'] is False
    persisted = ''.join(p.read_text() for p in root.rglob('*.jsonl'))
    for private in (secret, endpoint, backend.name, backend.model):
        assert private not in persisted


def test_untrusted_backend_diagnostics_cannot_become_event_content(tmp_path):
    failure = BackendError('synthetic-private-message', category='synthetic-private-category')
    failure.kind = 'synthetic-private-kind'
    failure.http_status = 'synthetic-private-status'
    class BrokenProvider:
        def chat(self, *args, **kwargs):
            raise failure
    manager = SessionManager(Config(home=tmp_path, cwd=tmp_path), backend_factory=lambda *_: BrokenProvider())
    try:
        slug = manager.create('Redaction fixture')['slug']
        manager.submit(slug, 'Hello')
        manager.wait(slug, timeout=10)
        root = manager.path(slug)
    finally:
        manager.close()
    data = next(record['data'] for record in read_records(root / 'events.jsonl') if record['event'] == 'error')
    assert data['error_category'] == 'provider'
    assert data['error_kind'] == 'provider'
    assert 'http_status' not in data
    assert 'synthetic-private' not in json.dumps(data)
