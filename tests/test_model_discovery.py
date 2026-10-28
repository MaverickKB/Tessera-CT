"""Model discovery: ask the endpoint what it serves instead of guessing a name.

The Models section asks the endpoint what it serves and shows those answers
on the settings page. Every test here talks to a loopback fixture. Nothing
reaches a real provider.
"""

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from tessera.config import load_config
from tessera.model_discovery import DiscoveryError, discover, discover_unsaved
from tessera.server import create_server
from test_server import call


CONFIG = '''
[backends.local]
base_url = "http://127.0.0.1:{port}/v1"
model = "placeholder"
chat = true
enabled = true

[backends.grok]
provider_kind = "cli_worker"
chat = true
enabled = true

[backends.grok.cli]
vendor = "grok"
command = "grok"
cwd = "workers"

[credentials.local-key]
env = "LOCAL_KEY"
'''

# Same wiring plus a stored credential, for the tests that check it is used.
CREDENTIALED = CONFIG.replace(
    'model = "placeholder"',
    'model = "placeholder"\ncredential_ref = "local-key"')


@contextmanager
def model_list_server(payload, status=200, require_auth=None):
    """A loopback endpoint that answers GET /v1/models the way a real server does."""
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append({'path': self.path,
                             'authorization': self.headers.get('Authorization'),
                             'api_key': self.headers.get('x-api-key')})
            if require_auth and self.headers.get('Authorization') != require_auth:
                self.send_response(401)
                self.end_headers()
                self.wfile.write(b'{"error":"unauthorized"}')
                return
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield 'http://127.0.0.1:%d/v1' % server.server_port, received
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def config_at(tmp_path, port=9, text=CONFIG):
    (tmp_path / 'workers').mkdir(exist_ok=True)
    (tmp_path / 'config.toml').write_text(text.format(port=port))
    return load_config(tmp_path)


def test_a_saved_backend_reports_the_models_its_endpoint_names(tmp_path):
    with model_list_server({'object': 'list', 'data': [
            {'id': 'glm-5.3', 'object': 'model'},
            {'id': 'qwen3-coder', 'object': 'model'}]}) as (url, received):
        config = config_at(tmp_path, port=url.rsplit(':', 1)[1].split('/')[0])
        names = discover(config, 'local')
    assert names == ['glm-5.3', 'qwen3-coder']
    assert received[0]['path'] == '/v1/models'


def test_an_unsaved_endpoint_can_be_asked_before_it_is_named(tmp_path):
    """The case that matters: know what is there before typing a model name."""
    with model_list_server({'data': [{'id': 'local-a'}, {'id': 'local-b'}]}) as (url, _):
        config = config_at(tmp_path)
        names = discover_unsaved(config, url, 'openai_chat')
    assert names == ['local-a', 'local-b']


def test_the_stored_credential_is_used_and_sent_as_a_bearer(tmp_path, monkeypatch):
    monkeypatch.setenv('LOCAL_KEY', 'secret-value')
    with model_list_server({'data': [{'id': 'only'}]},
                           require_auth='Bearer secret-value') as (url, received):
        config = config_at(tmp_path, port=url.rsplit(':', 1)[1].split('/')[0],
                           text=CREDENTIALED)
        names = discover(config, 'local')
    assert names == ['only']
    assert received[0]['authorization'] == 'Bearer secret-value'


def test_a_refused_credential_says_so_instead_of_returning_nothing(tmp_path):
    with model_list_server({'data': []}, require_auth='Bearer right') as (url, _):
        config = config_at(tmp_path)
        with pytest.raises(DiscoveryError) as caught:
            discover_unsaved(config, url, 'openai_chat')
    assert caught.value.category == 'authentication'
    assert 'refused the credential' in str(caught.value)


def test_an_endpoint_without_a_list_says_that_rather_than_an_empty_answer(tmp_path):
    with model_list_server({}, status=404) as (url, _):
        config = config_at(tmp_path)
        with pytest.raises(DiscoveryError) as caught:
            discover_unsaved(config, url, 'openai_chat')
    assert 'does not publish a model list' in str(caught.value)


def test_a_cli_worker_is_not_probed_because_it_has_no_list(tmp_path):
    config = config_at(tmp_path)
    with pytest.raises(DiscoveryError) as caught:
        discover(config, 'grok')
    assert caught.value.category == 'configuration'
    assert 'CLI worker' in str(caught.value)


def test_anthropic_style_lists_are_read_from_the_same_shape(tmp_path):
    with model_list_server({'data': [{'id': 'claude-example'}]}) as (url, received):
        config = config_at(tmp_path)
        names = discover_unsaved(config, url, 'anthropic_messages')
    assert names == ['claude-example']
    assert received[0]['path'] == '/v1/models'


def test_duplicates_collapse_and_order_is_stable(tmp_path):
    with model_list_server({'data': [{'id': 'b'}, {'id': 'a'}, {'id': 'b'}]}) as (url, _):
        config = config_at(tmp_path)
        assert discover_unsaved(config, url, 'openai_chat') == ['a', 'b']


def test_the_route_answers_on_the_live_server_and_writes_nothing(tmp_path):
    with model_list_server({'data': [{'id': 'served-model'}]}) as (url, _):
        (tmp_path / 'workers').mkdir()
        (tmp_path / 'config.toml').write_text(CONFIG.format(
            port=url.rsplit(':', 1)[1].split('/')[0]))
        config = load_config(tmp_path)
        config.server.port = 0
        server = create_server(config)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        before = (tmp_path / 'config.toml').read_text()
        try:
            status, body = call(server, 'POST', '/api/models/discover',
                                {'name': 'local'})
            # The unsaved-endpoint path, with the same route.
            status2, body2 = call(server, 'POST', '/api/models/discover',
                                  {'base_url': url, 'kind': 'openai_chat'})
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
        after = (tmp_path / 'config.toml').read_text()
    assert status == 200 and body == {'models': ['served-model']}
    assert status2 == 200 and body2 == {'models': ['served-model']}
    assert before == after


def test_the_route_refuses_a_request_that_names_nothing(tmp_path):
    (tmp_path / 'workers').mkdir()
    (tmp_path / 'config.toml').write_text(CONFIG.format(port=9))
    config = load_config(tmp_path)
    config.server.port = 0
    server = create_server(config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, body = call(server, 'POST', '/api/models/discover', {})
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    assert status == 400
    assert 'base URL' in body['error']


def test_the_route_reports_a_refusal_as_authentication_not_as_success(tmp_path):
    with model_list_server({'data': []}, require_auth='Bearer right') as (url, _):
        (tmp_path / 'workers').mkdir()
        (tmp_path / 'config.toml').write_text(CONFIG.format(port=9))
        config = load_config(tmp_path)
        config.server.port = 0
        server = create_server(config)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            status, body = call(server, 'POST', '/api/models/discover',
                                {'base_url': url, 'kind': 'openai_chat'})
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
    assert status == 401
    assert body['error_category'] == 'authentication'


def test_a_sign_in_provider_is_asked_at_its_fixed_endpoint(tmp_path, monkeypatch):
    """An OAuth account hides URL and credential in the form, so discovery has to
    read the same fixed wiring the form does."""
    monkeypatch.setattr('tessera.model_discovery.OAUTH_MODELS', {
        'example': {'label': 'Example', 'kind': 'openai_chat',
                    'base_url': 'http://127.0.0.1:9/v1', 'credential': '',
                    'oauth': ''}})
    from tessera import model_discovery
    with model_list_server({'data': [{'id': 'oauth-served'}]}) as (url, received):
        model_discovery.OAUTH_MODELS['example']['base_url'] = url
        config = config_at(tmp_path)
        names = model_discovery.discover_account(config, 'example')
    assert names == ['oauth-served']
    assert received[0]['path'] == '/v1/models'


def test_an_unknown_sign_in_provider_is_refused_by_name(tmp_path):
    config = config_at(tmp_path)
    from tessera.model_discovery import discover_account
    with pytest.raises(DiscoveryError) as caught:
        discover_account(config, 'not-a-provider')
    assert caught.value.category == 'configuration'
