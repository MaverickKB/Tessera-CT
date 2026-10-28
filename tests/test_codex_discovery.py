"""Codex models list from the stored sign-in, before any model row exists."""

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from tessera.config import load_config
from tessera.model_discovery import DiscoveryError, discover_account


def _token():
    claims = {'https://api.openai.com/auth': {'chatgpt_account_id': 'acct-test'}}
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=')
    return 'header.' + payload + '.sig'


def test_codex_models_list_right_after_sign_in_with_no_model_rows(tmp_path):
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append({
                'path': self.path,
                'authorization': self.headers.get('Authorization'),
                'account': self.headers.get('ChatGPT-Account-ID'),
            })
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'models': [
                {'slug': 'later', 'priority': 5, 'supported_in_api': False},
                {'slug': 'hidden-one', 'visibility': 'hide', 'priority': 1},
                {'slug': 'first', 'priority': 1, 'supported_in_api': True},
            ]}).encode())

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        grant = tmp_path / 'oauth' / 'openai-codex.json'
        grant.parent.mkdir()
        grant.write_text(json.dumps({
            'access_token': _token(),
            'expires_at': 9_999_999_999,
        }))
        (tmp_path / 'config.toml').write_text(
            '[codex]\nmodels_url = "http://127.0.0.1:%d/models"\nclient_version = "99.0.0"\n'
            % server.server_port)
        config = load_config(tmp_path)
        assert config.backends == {}
        assert config.credentials == {}
        names = discover_account(config, 'openai-codex')
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    assert names == ['first', 'later']
    assert received[0]['path'] == '/models?client_version=99.0.0'
    assert received[0]['authorization'] == 'Bearer ' + _token()
    assert received[0]['account'] == 'acct-test'


def test_codex_discovery_without_a_sign_in_names_that_and_not_a_missing_credential_row(tmp_path):
    (tmp_path / 'config.toml').write_text(
        '[codex]\nmodels_url = "http://127.0.0.1:9/models"\nclient_version = "99.0.0"\n')
    config = load_config(tmp_path)
    with pytest.raises(DiscoveryError) as caught:
        discover_account(config, 'openai-codex')
    assert 'Not signed in' in str(caught.value)
    assert 'Credential config reference is not configured' not in str(caught.value)


def test_codex_catalog_settings_fail_at_config_load(tmp_path):
    (tmp_path / 'config.toml').write_text('[codex]\nmodels_url = "ftp://chatgpt.com/models"\n')
    with pytest.raises(ValueError, match='codex.models_url'):
        load_config(tmp_path)
    (tmp_path / 'config.toml').write_text(
        '[codex]\nmodels_url = "https://chatgpt.com/backend-api/codex/models"\nclient_version = "99 0"\n')
    with pytest.raises(ValueError, match='client_version'):
        load_config(tmp_path)
    (tmp_path / 'config.toml').write_text('')
    loaded = load_config(tmp_path)
    assert loaded.codex.models_url == 'https://chatgpt.com/backend-api/codex/models'
    assert loaded.codex.client_version == '99.0.0'


def test_backend_api_key_env_is_rejected_at_config_load(tmp_path):
    (tmp_path / 'config.toml').write_text(
        '[backends.bad]\nbase_url = "http://127.0.0.1:9/v1"\nmodel = "m"\napi_key_env = "sk-live-secret"\n')
    with pytest.raises(ValueError, match='api_key_env'):
        load_config(tmp_path)
