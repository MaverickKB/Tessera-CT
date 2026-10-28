"""Opt-in proxy names for a tailnet-only HTTPS front (e.g. tailscale serve). Loopback bind unchanged."""

from contextlib import contextmanager
import http.client
import json
import threading
from unittest.mock import Mock

import pytest

from tessera import server as server_module
from tessera.config import Config, load_config

PROXY = 'phone-proxy.placeholder.example'


@contextmanager
def core(tmp_path, allowed=()):
    config = Config(home=tmp_path / 'home', cwd=tmp_path)
    config.server.host, config.server.port = '127.0.0.1', 0
    config.server.allowed_hosts = list(allowed)
    app = tmp_path / 'app'
    app.mkdir(exist_ok=True)
    (app / 'index.html').write_text('<title>fixture</title>')
    backend = Mock(side_effect=AssertionError('no model backend'))
    with server_module.create_server(config, web_root=app, backend_factory=backend) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            thread.join()


def call(server, method, path, headers, body=None):
    connection = http.client.HTTPConnection(*server.server_address, timeout=5)
    try:
        connection.request(method, path, body=None if body is None else json.dumps(body),
                           headers={'Content-Type': 'application/json', **headers})
        response = connection.getresponse()
        raw = response.read()
        return response.status, raw
    finally:
        connection.close()


def test_off_by_default_and_env_configurable(tmp_path, monkeypatch):
    assert Config().server.allowed_hosts == []
    assert load_config(tmp_path).server.allowed_hosts == []
    monkeypatch.setenv('TESSERA_SERVER_ALLOWED_HOSTS', json.dumps([PROXY]))
    assert load_config(tmp_path).server.allowed_hosts == [PROXY]
    for bad in (['https://' + PROXY], [PROXY + ':443'], ['*.example'], [PROXY + '/x'], 'not-a-list'):
        monkeypatch.setenv('TESSERA_SERVER_ALLOWED_HOSTS', json.dumps(bad))
        with pytest.raises(ValueError):
            load_config(tmp_path)


def test_proxy_host_is_refused_unless_listed(tmp_path):
    with core(tmp_path) as server:
        assert call(server, 'GET', '/', {'Host': PROXY})[0] == 403
        assert call(server, 'GET', '/api/sessions', {'Host': PROXY, 'Origin': 'https://' + PROXY})[0] == 403


def test_listed_proxy_host_passes_for_ui_api_and_same_https_origin(tmp_path):
    with core(tmp_path, [PROXY + '.']) as server:  # trailing-dot form normalizes
        status, body = call(server, 'GET', '/', {'Host': PROXY})
        assert status == 200 and b'fixture' in body
        assert call(server, 'GET', '/api/sessions', {'Host': PROXY.upper()})[0] == 200
        status, _ = call(server, 'POST', '/api/sessions', {'Host': PROXY, 'Origin': 'https://' + PROXY}, {'title': 'phone'})
        assert status == 201
        assert call(server, 'GET', '/', {'Host': PROXY + ':8443'})[0] == 200  # non-default serve port
        # Loopback access keeps working alongside.
        port = server.server_address[1]
        assert call(server, 'GET', '/api/sessions', {'Host': f'127.0.0.1:{port}'})[0] == 200


@pytest.mark.parametrize('host, origin', [
    ('evil.placeholder.example', None),
    (PROXY, 'http://' + PROXY),                       # proxied site must be https
    (PROXY, 'https://evil.placeholder.example'),      # foreign origin
    (PROXY, 'https://' + PROXY + '.evil.example'),    # suffix trick
    (PROXY, 'null'),
    ('127.0.0.1:1', None),                            # wrong loopback port still refused
])
def test_everything_else_still_403(tmp_path, host, origin):
    with core(tmp_path, [PROXY]) as server:
        headers = {'Host': host}
        if origin is not None:
            headers['Origin'] = origin
        assert call(server, 'POST', '/api/sessions', headers, {'title': 'x'})[0] == 403
        port = server.server_address[1]
        assert call(server, 'GET', '/api/sessions', {'Host': f'127.0.0.1:{port}', 'Origin': 'https://' + PROXY})[0] == 403
