import json
import sys
import threading
import tomllib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from tessera.config import load_config
from tessera.integrations import Integrations, set_toml_bool
from test_remote_tools import fake_ssh  # noqa: F401  (fixture)
from test_server import running_server, call


def test_set_toml_bool_edits_only_the_target_line():
    text = '# keep\n[backends."router-auto"]\n"model" = "auto"\n"chat" = true # trailing\n\n[remotes."edge-node"]\n"host" = "h"\n'
    out = set_toml_bool(text, ('backends', 'router-auto'), 'chat', False)
    assert out == text.replace('"chat" = true # trailing', '"chat" = false')
    out = set_toml_bool(text, ('remotes', 'edge-node'), 'enabled', False)
    assert tomllib.loads(out)['remotes']['edge-node'] == {'host': 'h', 'enabled': False}
    assert out.startswith('# keep\n')
    out = set_toml_bool(text, ('voice',), 'enabled', True)
    assert tomllib.loads(out)['voice'] == {'enabled': True}
    bare = '[jev]\nenabled = true\nurl = "x"\n'
    assert tomllib.loads(set_toml_bool(bare, ('jev',), 'enabled', False))['jev'] == {'enabled': False, 'url': 'x'}


class _Prom(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith('/api/v1/status/buildinfo'):
            body = {'status': 'success', 'data': {'version': '3.1.0'}}
        elif self.path.startswith('/api/v1/targets'):
            body = {'status': 'success', 'data': {'activeTargets': [{'health': 'up'}, {'health': 'down'}]}}
        elif self.path == '/v1/models':
            body = {'data': [{'id': 'auto'}, {'id': 'other'}]}
        else:
            self.send_response(404); self.end_headers(); return
        raw = json.dumps(body).encode()
        self.send_response(200); self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw))); self.end_headers(); self.wfile.write(raw)

    def log_message(self, *args):
        pass


@pytest.fixture
def http_target():
    server = ThreadingHTTPServer(('127.0.0.1', 0), _Prom)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f'http://127.0.0.1:{server.server_port}'
    server.shutdown()


def write_home(tmp_path, base, fake):
    (tmp_path / 'config.toml').write_text(f'''
[roles]
"main" = "chat-model"

[backends."chat-model"]
"base_url" = "{base}/v1"
"model" = "auto"

[backends."struct-model"]
"base_url" = "{base}/v1"
"model" = "missing"
"chat" = false

[aethermind]

[jev]
"enabled" = false

[infrastructure]
"enabled" = true
"prometheus_url" = "{base}"

[remotes."desk"]
"host" = "192.0.2.10"
"port" = 2222
"user" = "operator"
"known_hosts_file" = "/pinned/known_hosts"
"ssh_command" = {json.dumps(fake)}
''')
    return load_config(tmp_path)


def test_inventory_checks_and_persisted_switches(tmp_path, http_target, fake_ssh):
    fake, log = fake_ssh
    config = write_home(tmp_path, http_target, fake)
    integrations = Integrations(SimpleNamespace(config=config))
    rows = {row['id']: row for row in integrations.list()['integrations']}
    assert {'backend:chat-model', 'backend:struct-model', 'mcp:aethermind', 'remote:desk', 'monitoring:prometheus', 'voice:xai'} <= set(rows)
    assert rows['backend:chat-model']['main'] is True and rows['backend:struct-model']['enabled'] is False
    assert rows['mcp:aethermind']['enabled'] is True and rows['mcp:aethermind']['switch'] == 'On'
    with pytest.raises(ValueError, match='no off state'):
        integrations.set_enabled('mcp:aethermind', False)
    assert rows['remote:desk']['target'] == 'operator@192.0.2.10' + ':2222'

    ok = integrations.check('backend:chat-model')['result']
    assert ok['ok'] is True and 'auto' in ok['tools']
    bad = integrations.check('backend:struct-model')['result']
    assert bad['ok'] is False and 'not in 2 served' in bad['detail']
    prom = integrations.check('monitoring:prometheus')['result']
    assert prom == {**prom, 'ok': True, 'detail': 'v3.1.0, 1/2 targets up'}
    desk = integrations.check('remote:desk')['result']
    assert desk['ok'] is True and desk['detail'] and 'remote_shell' in desk['tools']
    assert 'uname -sn' in log.read_text()

    with pytest.raises(ValueError, match='roles.main'):
        integrations.set_enabled('backend:chat-model', False)
    with pytest.raises(ValueError):
        integrations.set_enabled('remote:desk', 'no')
    reply = integrations.set_enabled('remote:desk', False)
    assert reply['pending_restart'] is True and (tmp_path / reply['backup']).is_file()
    assert load_config(tmp_path).remotes['desk'].enabled is False
    from tessera.config_editor import ConfigEditor
    journal = ConfigEditor(tmp_path / 'config.toml')
    assert journal.notice_drift() is None
    assert journal.changes()[-1]['source'] == 'config_editor'
    assert config.remotes['desk'].enabled is False
    assert integrations.list()['integrations'][[r['id'] for r in integrations.list()['integrations']].index('remote:desk')]['pending_restart'] is True
    again = integrations.set_enabled('remote:desk', True)
    assert again['backup'] != reply['backup'] and '"enabled" = false' in (tmp_path / again['backup']).read_text()
    integrations.set_enabled('backend:struct-model', True)
    assert load_config(tmp_path).backends['struct-model'].chat is True
    with pytest.raises(KeyError):
        integrations.check('nope:nothing')


def test_invalid_edit_is_rejected_and_file_untouched(tmp_path, http_target, fake_ssh, monkeypatch):
    config = write_home(tmp_path, http_target, fake_ssh[0])
    before = (tmp_path / 'config.toml').read_text()
    integrations = Integrations(SimpleNamespace(config=config))
    monkeypatch.setattr('tessera.config_editor.set_toml_value', lambda *a: before + '\n[roles]\n"main" = "ghost"\n')
    with pytest.raises(Exception):
        integrations.set_enabled('remote:desk', False)
    assert (tmp_path / 'config.toml').read_text() == before
    assert not list(tmp_path.glob('config.before-integration-*'))


def test_http_integrations_endpoints(tmp_path):
    with running_server(tmp_path) as server:
        status, body = call(server, 'GET', '/api/integrations')
        assert status == 200 and any(row['id'] == 'voice:xai' for row in body['integrations'])
        assert call(server, 'POST', '/api/integrations/nope%3Ax/check', {})[0] == 404
        assert call(server, 'POST', '/api/integrations/voice%3Axai/enabled', {'enabled': 'x'})[0] == 400
        assert call(server, 'POST', '/api/integrations/voice%3Axai/check', {}, {'Origin': 'https://evil.example'})[0] == 403


def test_rdp_and_desk_switches_leave_ssh_alone(tmp_path, http_target, fake_ssh):
    config = write_home(tmp_path, http_target, fake_ssh[0])
    text = (tmp_path / 'config.toml').read_text().replace('"user" = "operator"', '"user" = "operator"\n"rdp_port" = 3389\n"desk" = true')
    (tmp_path / 'config.toml').write_text(text)
    config = load_config(tmp_path)
    integrations = Integrations(SimpleNamespace(config=config))
    rows = {row['id']: row for row in integrations.list()['integrations']}
    assert integrations._row('rdp:desk')['key'] == 'rdp_enabled' and integrations._row('desk:desk')['key'] == 'desk'
    assert {'rdp:desk', 'desk:desk'} <= set(rows)
    integrations.set_enabled('rdp:desk', False)
    loaded = load_config(tmp_path).remotes['desk']
    assert loaded.rdp_enabled is False and loaded.enabled is True and loaded.desk is True
    integrations.set_enabled('desk:desk', False)
    loaded = load_config(tmp_path).remotes['desk']
    assert loaded.desk is False and loaded.enabled is True


class _Judge(BaseHTTPRequestHandler):
    """TypeSafe's shape: the inference route is POST-only, auth is checked before
    request validation, and a valid key with an empty body answers 422. GET answers
    405. Reproduced here so the probe can be tested without the network."""
    key = 'good-key'

    def _auth(self):
        supplied = (self.headers.get('Authorization') or '').removeprefix('Bearer ').strip()
        if not supplied:
            return 403, {'detail': {'error_type': 'authentication_error',
                                    'message': 'Must supply an API key! Check your request and try again.'}}
        if supplied != self.key:
            return 401, {'detail': {'error_type': 'authentication_error',
                                    'message': 'Cannot authenticate with the server. Please check your API key and try again.'}}
        return 422, {'detail': [{'type': 'missing', 'loc': ['body', 'model'], 'msg': 'Field required'}]}

    def do_GET(self):
        self.send_response(405); self.send_header('Content-Type', 'application/json')
        self.end_headers(); self.wfile.write(b'{"detail":"Method Not Allowed"}')

    def do_POST(self):
        length = int(self.headers.get('Content-Length') or 0)
        self.rfile.read(length)
        status, body = self._auth()
        raw = json.dumps(body).encode()
        self.send_response(status); self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw))); self.end_headers(); self.wfile.write(raw)

    def log_message(self, *args):
        pass


@pytest.fixture
def judge_target(monkeypatch):
    server = ThreadingHTTPServer(('127.0.0.1', 0), _Judge)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv('PROBE_JEV_KEY', _Judge.key)
    yield f'http://127.0.0.1:{server.server_port}/v1/systemone'
    server.shutdown()


def _judge_home(tmp_path, url, key_ref=''):
    (tmp_path / 'config.toml').write_text(f'''
[roles]
"main" = "chat-model"

[backends."chat-model"]
"base_url" = "http://127.0.0.1:9/v1"
"model" = "auto"

[aethermind]

[jev]
"enabled" = true
"url" = "{url}"
"model" = "jev-1.14.0"
"api_key_env" = "PROBE_JEV_KEY"
''')
    return load_config(tmp_path)


def test_a_healthy_judge_is_not_reported_failed_for_refusing_a_get(tmp_path, judge_target):
    """The page said Failed for a judge that was answering tool gates in 191ms.
    Cause: the probe GET the inference route, which is POST-only, and urllib raised
    405, which the page drew as Failed. A healthy judge must read ok."""
    config = _judge_home(tmp_path, judge_target)
    result = Integrations(SimpleNamespace(config=config)).check('judge:jev')['result']
    assert result['ok'] is True, result
    assert '405' not in result['detail']
    assert '422' in result['detail']


def test_a_rejected_credential_is_still_reported_failed(tmp_path, judge_target, monkeypatch):
    """The fix must not turn every answer into a pass. Auth runs before validation on
    this endpoint, so a bad key answers 401 and that is a real failure worth showing."""
    config = _judge_home(tmp_path, judge_target)
    monkeypatch.setenv('PROBE_JEV_KEY', 'wrong-key')
    result = Integrations(SimpleNamespace(config=config)).check('judge:jev')['result']
    assert result['ok'] is False, result
    assert '401' in result['detail'] and 'credential' in result['detail']


def test_a_missing_credential_is_reported_failed(tmp_path, judge_target, monkeypatch):
    config = _judge_home(tmp_path, judge_target)
    monkeypatch.delenv('PROBE_JEV_KEY', raising=False)
    result = Integrations(SimpleNamespace(config=config)).check('judge:jev')['result']
    assert result['ok'] is False, result
    assert '403' in result['detail']


class _Models(BaseHTTPRequestHandler):
    """A model-list route that demands a Bearer header.

    A configured token answers 200 and lists models; a missing header answers 401.
    Reproduced here so the backend probe can be tested without the network.
    """

    key = 'backend-token'

    def do_GET(self):
        supplied = (self.headers.get('Authorization') or '').removeprefix('Bearer ').strip()
        if supplied != self.key:
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b'{"error":"Unauthorized"}')
            return
        body = json.dumps({'data': [{'id': 'grok-4.7'}, {'id': 'grok-4.3'}]}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def models_target(monkeypatch):
    server = ThreadingHTTPServer(('127.0.0.1', 0), _Models)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f'http://127.0.0.1:{server.server_port}/v1'
    server.shutdown()


def _backend_home(tmp_path, base, ref, env=''):
    """A home whose only chat backend reaches its credential the way grok does:
    a named credential_ref, not an api_key_env on the backend itself."""
    source = f'[credentials."{ref}"]\n"env" = "{env}"\n\n' if env else f'[credentials."{ref}"]\n\n'
    (tmp_path / 'config.toml').write_text(f'''
[roles]
"main" = "chat-model"

{source}[backends."chat-model"]
"base_url" = "{base}"
"model" = "grok-4.7"
"credential_ref" = "{ref}"

[aethermind]
''')
    return load_config(tmp_path)


def test_a_backend_whose_credential_is_a_named_reference_reads_ok(tmp_path, models_target, monkeypatch):
    """A healthy backend must not read Failed because its credential is a reference.

    The probe has to send the credential a reference resolves, not only
    os.environ of an api_key_env. A backend configured with a credential reference
    and no api_key_env otherwise sends no Authorization header, and the endpoint's
    401 is drawn as Failed. A backend that reaches its credential through a
    reference must read ok.
    """
    monkeypatch.setenv('PROBE_BACKEND_TOKEN', _Models.key)
    config = _backend_home(tmp_path, models_target, 'probe-backend', env='PROBE_BACKEND_TOKEN')
    result = Integrations(SimpleNamespace(config=config)).check('backend:chat-model')['result']
    assert result['ok'] is True, result
    assert 'grok-4.7 listed' in result['detail']
    assert '401' not in result['detail']


def test_a_backend_credential_that_is_rejected_still_reads_failed(tmp_path, models_target, monkeypatch):
    """The discriminating half: a wrong credential must NOT read healthy.
    The endpoint answers 401 and that IS a failure, named as one."""
    monkeypatch.setenv('PROBE_BACKEND_TOKEN', 'not-the-right-token')
    config = _backend_home(tmp_path, models_target, 'probe-backend', env='PROBE_BACKEND_TOKEN')
    result = Integrations(SimpleNamespace(config=config)).check('backend:chat-model')['result']
    assert result['ok'] is False, result
    assert '401' in result['detail']
    assert 'rejected' in result['detail']


def test_a_backend_whose_credential_cannot_be_resolved_says_so(tmp_path, models_target, monkeypatch):
    """No silent empty header. When the reference cannot resolve, the detail must
    name that instead of reporting a bare endpoint 401."""
    monkeypatch.delenv('PROBE_MISSING_KEY', raising=False)
    config = _backend_home(tmp_path, models_target, 'probe-missing', env='PROBE_MISSING_KEY')
    result = Integrations(SimpleNamespace(config=config)).check('backend:chat-model')['result']
    assert result['ok'] is False, result
    assert 'credential unavailable' in result['detail']


def test_a_failed_backend_names_its_sign_in_and_offers_a_repair(tmp_path, models_target, monkeypatch):
    """A failed backend names its sign-in and offers a repair.

    Two halves:
    (1) a backend whose credential is a refreshable OAuth grant carries the
        provider in 'repair', so the page can offer a sign-in refresh;
    (2) when that credential is rejected, the detail says what the saved sign-in
        looks like rather than only echoing the endpoint's 401 -- and it never
        includes the token.
    An API-key backend carries no repair: its fix is editing the credential, not
    a button.
    """
    import json as _json
    (tmp_path / 'oauth').mkdir()
    (tmp_path / 'oauth' / 'xai.json').write_text(_json.dumps({
        'access_token': 'live-but-rejected-token-value', 'refresh_token': 'r', 'expires_at': 2**31}))
    (tmp_path / 'config.toml').write_text(f'''
[roles]
"main" = "oauth-model"

[credentials."probe-oauth"]
"oauth" = "xai"

[credentials."probe-key"]
"env" = "PROBE_KEY_TOKEN"

[backends."oauth-model"]
"base_url" = "{models_target}"
"model" = "grok-4.7"
"credential_ref" = "probe-oauth"

[backends."key-model"]
"base_url" = "{models_target}"
"model" = "grok-4.7"
"credential_ref" = "probe-key"

[aethermind]
''')
    monkeypatch.setenv('PROBE_KEY_TOKEN', 'wrong-key')
    config = load_config(tmp_path)
    integrations = Integrations(SimpleNamespace(config=config))

    rows = {row['id']: row for row in integrations.list()['integrations']}
    assert rows['backend:oauth-model']['repair'] == 'xai'
    assert rows['backend:key-model']['repair'] == ''

    result = integrations.check('backend:oauth-model')['result']
    assert result['ok'] is False, result
    assert 'rejected' in result['detail']
    assert 'saved sign-in' in result['detail']
    assert 'live-but-rejected-token-value' not in result['detail']

    keyed = integrations.check('backend:key-model')['result']
    assert keyed['ok'] is False, keyed
    assert 'saved sign-in' not in keyed['detail']


def test_a_grant_that_cannot_produce_a_token_says_sign_in_again(tmp_path, models_target, monkeypatch):
    """The other repair shape, observed while writing the first test: a saved grant
    with no token endpoint cannot mint an access token at all. The detail must say
    the credential is unavailable and what to do, not report a bare endpoint 401
    that sends the reader looking at the wrong machine."""
    import json as _json
    (tmp_path / 'oauth').mkdir()
    (tmp_path / 'oauth' / 'xai.json').write_text(_json.dumps({
        'access_token': '', 'refresh_token': 'r', 'expires_at': 1}))
    (tmp_path / 'config.toml').write_text(f'''
[roles]
"main" = "oauth-model"

[credentials."probe-oauth"]
"oauth" = "xai"

[backends."oauth-model"]
"base_url" = "{models_target}"
"model" = "grok-4.7"
"credential_ref" = "probe-oauth"

[aethermind]
''')
    config = load_config(tmp_path)
    result = Integrations(SimpleNamespace(config=config)).check('backend:oauth-model')['result']
    assert result['ok'] is False, result
    assert 'credential unavailable' in result['detail']
    # Either phrasing is honest; both name the repair instead of blaming the host.
    assert ('Sign in again' in result['detail']) or ('Sign in on the settings page' in result['detail'])
