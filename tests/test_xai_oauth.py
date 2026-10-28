import json

import pytest

from tessera.account_oauth import save_grant
from tessera.backend import Backend, BackendError
from tessera.config import load_config
from tessera.credentials import CredentialReference, CredentialResolver
from tessera.loop import Session
from tessera.xai_oauth import resolve_access_token

SENTINEL = 'must-not-be-sent'


def test_xai_oauth_does_not_require_another_program():
    CredentialReference(oauth='xai').validate()


def test_xai_oauth_reads_this_harness_grant_and_not_an_api_key(tmp_path, monkeypatch):
    monkeypatch.setenv('XAI_API_KEY', 'must-not-be-used')
    save_grant(tmp_path, 'xai', {
        'access_token': 'oauth-grant',
        'refresh_token': 'refresh-grant',
        'expires_at': 9_999_999_999,
        'token_endpoint': 'https://auth.x.ai/oauth2/token',
    })
    token = resolve_access_token(tmp_path)
    assert token == 'oauth-grant'
    resolved = CredentialResolver(
        {'xai-oauth': {'oauth': 'xai'}},
        oauth_home=str(tmp_path),
    ).resolve('xai-oauth')
    assert resolved.source == 'oauth'
    assert resolved.value == 'oauth-grant'
    assert 'XAI_API_KEY' not in resolved.value


def test_oauth_chat_sends_api_tools_not_a_cli_prompt(tmp_path, monkeypatch):
    monkeypatch.setenv('XAI_API_KEY', 'must-not-be-used')
    save_grant(tmp_path, 'xai', {
        'access_token': 'oauth-grant',
        'refresh_token': 'refresh-grant',
        'expires_at': 9_999_999_999,
        'token_endpoint': 'https://auth.x.ai/oauth2/token',
    })
    seen = {}

    class Response:
        def __init__(self, body):
            self.body = body
        def read(self):
            return self.body
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def getheader(self, name, default=None):
            return default

    def opener_open(req, timeout=None):
        seen['url'] = req.full_url
        seen['auth'] = req.get_header('Authorization')
        seen['body'] = json.loads(req.data.decode())
        payload = {'choices': [{'message': {'content': 'ok', 'tool_calls': []}, 'finish_reason': 'stop'}], 'usage': {}}
        return Response(json.dumps(payload).encode())

    backend = Backend('grok', 'https://api.x.ai/v1', 'grok-4.7', extra={
        'credential_ref': 'xai-oauth',
        'credential_sources': {'xai-oauth': {'oauth': 'xai'}},
        'oauth_home': str(tmp_path),
        'timeout': 5,
        'retries': 0,
        'stream': False,
    })
    backend._opener.open = opener_open
    tools = [{'type': 'function', 'function': {'name': 'list_lessons', 'parameters': {'type': 'object'}}}]
    result = backend.chat([{'role': 'user', 'content': 'hi'}], tools)
    assert result['content'] == 'ok'
    assert backend.last_auth == 'oauth'
    assert seen['url'] == 'https://api.x.ai/v1/chat/completions'
    assert seen['auth'] == 'Bearer oauth-grant'
    assert seen['body']['tools'] == tools
    assert 'tool_calls' not in json.dumps(seen['body']['messages'])
    assert 'disallowed-tools' not in json.dumps(seen['body'])


def _grok_home(tmp_path, *, credential_env='', backend_env=''):
    """Documented Grok chat: Tessera xAI sign-in. An API key is not a fallback."""
    credential = 'oauth = "xai"\n'
    if credential_env:
        credential += f'env = "{credential_env}"\n'
    backend_key = f'api_key_env = "{backend_env}"\n' if backend_env else ''
    (tmp_path / 'config.toml').write_text(
        '[credentials.xai-oauth]\n'
        + credential
        + '[backends.grok]\n'
        'base_url = "https://api.x.ai/v1"\n'
        'model = "grok-4.7"\n'
        'credential_ref = "xai-oauth"\n'
        + backend_key
        + 'timeout = 5\n'
        'retries = 0\n'
        '[backends.paid]\n'
        'base_url = "https://api.x.ai/v1"\n'
        'model = "grok-4.7"\n'
        'api_key_env = "XAI_API_KEY"\n'
        'timeout = 5\n'
        'retries = 0\n'
        '[roles]\n'
        'main = "grok"\n'
        'failover = "paid"\n'
    )
    return load_config(tmp_path)


def _grok_backend(config):
    settings = config.backend_for('main')
    assert settings.credential_ref == 'xai-oauth'
    return Backend(settings.name, settings.base_url, settings.model, settings.api_key_env, settings.extra)


def _refuse_transport(monkeypatch):
    """Chat and token calls are recorded. A real socket is a test failure."""
    chat = []
    refresh = []

    def opener_open(req, timeout=None):
        header = req.get_header('Authorization') or ''
        body = req.data.decode() if req.data else ''
        chat.append(req.full_url)
        if SENTINEL in header or SENTINEL in body or SENTINEL in req.full_url:
            raise AssertionError('chat transport carried the API key')
        raise AssertionError('chat transport was called')

    def poster(url, body, headers):
        payload = body.decode() if isinstance(body, bytes) else str(body)
        refresh.append(url)
        if SENTINEL in payload or SENTINEL in url or any(SENTINEL in str(value) for value in headers.values()):
            raise AssertionError('token refresh carried the API key')
        return 401, {'error': 'invalid_grant'}

    def urlopen(*args, **kwargs):
        raise AssertionError('urllib must not open a socket')

    monkeypatch.setattr('urllib.request.urlopen', urlopen)
    monkeypatch.setattr('tessera.account_oauth._urllib_post', poster)
    return chat, refresh, opener_open


@pytest.mark.parametrize('state,credential_env,backend_env', [
    ('missing', '', ''),
    ('expired', '', ''),
    ('missing', 'XAI_API_KEY', ''),
    ('missing', '', 'XAI_API_KEY'),
    ('expired', '', 'XAI_API_KEY'),
])
def test_grok_http_chat_fails_closed_when_oauth_cannot_authorize(tmp_path, monkeypatch, state, credential_env, backend_env):
    """Ambient XAI_API_KEY must not authorize a Grok chat when the xAI grant is missing or expired.

    The request is the real config path: load_config, backend_for, Backend.chat. Naming
    XAI_API_KEY on the OAuth credential, or on the backend beside that credential, is not
    permission to send it when the grant cannot authorize.
    """
    monkeypatch.setenv('XAI_API_KEY', SENTINEL)
    config = _grok_home(tmp_path, credential_env=credential_env, backend_env=backend_env)
    if state == 'expired':
        save_grant(config.home, 'xai', {
            'access_token': 'expired-oauth-grant',
            'refresh_token': 'refresh-grant',
            'expires_at': 1,
            'token_endpoint': 'https://auth.x.ai/oauth2/token',
        })
    chat, refresh, opener_open = _refuse_transport(monkeypatch)
    backend = _grok_backend(config)
    backend._opener.open = opener_open
    with pytest.raises(BackendError) as error:
        backend.chat([{'role': 'user', 'content': 'hi'}])
    assert error.value.category == 'configuration'
    assert error.value.credential == 'missing'
    assert SENTINEL not in str(error.value)
    if state == 'missing':
        assert 'Not signed in' in str(error.value)
        assert refresh == []
    else:
        assert 'expired' in str(error.value)
        assert refresh == ['https://auth.x.ai/oauth2/token']
    assert chat == []
    assert backend.last_auth == ''


def test_current_oauth_grant_is_sent_instead_of_xai_api_key(tmp_path, monkeypatch):
    """A live xAI grant wins over XAI_API_KEY even when the backend names that variable."""
    monkeypatch.setenv('XAI_API_KEY', SENTINEL)
    config = _grok_home(tmp_path, backend_env='XAI_API_KEY')
    save_grant(config.home, 'xai', {
        'access_token': 'oauth-grant',
        'refresh_token': 'refresh-grant',
        'expires_at': 9_999_999_999,
        'token_endpoint': 'https://auth.x.ai/oauth2/token',
    })
    seen = {}

    class Response:
        def __init__(self, body):
            self.body = body
        def read(self):
            return self.body
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def getheader(self, name, default=None):
            return default

    def opener_open(req, timeout=None):
        seen['auth'] = req.get_header('Authorization')
        seen['body'] = req.data.decode()
        payload = {'choices': [{'message': {'content': 'ok'}, 'finish_reason': 'stop'}], 'usage': {}}
        return Response(json.dumps(payload).encode())

    def urlopen(*args, **kwargs):
        raise AssertionError('urllib must not open a socket')

    monkeypatch.setattr('urllib.request.urlopen', urlopen)
    backend = _grok_backend(config)
    backend._opener.open = opener_open
    result = backend.chat([{'role': 'user', 'content': 'hi'}])
    assert result['content'] == 'ok'
    assert backend.last_auth == 'oauth'
    assert seen['auth'] == 'Bearer oauth-grant'
    assert SENTINEL not in seen['auth']
    assert SENTINEL not in seen['body']


def test_session_chat_does_not_fail_over_onto_xai_api_key(tmp_path, monkeypatch):
    """A missing xAI grant is a configuration error, so the turn does not switch to the paid backend."""
    monkeypatch.setenv('XAI_API_KEY', SENTINEL)
    config = _grok_home(tmp_path)
    chat, refresh, opener_open = _refuse_transport(monkeypatch)
    backend = _grok_backend(config)
    backend._opener.open = opener_open
    built = []

    def _build(name):
        built.append(name)
        raise AssertionError('failover must not build a backend')

    with Session(config, backend=backend) as session:
        session._build = _build
        with pytest.raises(BackendError) as error:
            session._chat(session.backend, [{'role': 'user', 'content': 'hi'}], None)
    assert error.value.category == 'configuration'
    assert 'Not signed in' in str(error.value)
    assert SENTINEL not in str(error.value)
    assert built == []
    assert chat == []
    assert refresh == []
