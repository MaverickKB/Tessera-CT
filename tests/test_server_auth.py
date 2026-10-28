"""The shared-key gate, and what it must not break.

Lock a proxied core with a shared key without breaking the phone, the desktop,
or stranding the operator on the machine the core runs on.

These tests assert two things in equal measure:
  1. the gate refuses an unauthenticated proxied request, and
  2. the DEFAULT posture cannot lock out a local client. A wrong or missing key
     must never strand the operator on the machine the core runs on.
"""
from contextlib import contextmanager
import http.client
import json
import threading

import pytest

from fake_backend import FakeBackend
from tessera.config import Config
from tessera.server import create_server

KEY = "test-shared-key-0123456789"


@contextmanager
def running_server(tmp_path, auth_token='', auth_mode='loopback', allowed_hosts=None):
    config = Config(home=tmp_path, cwd=tmp_path)
    config.server.port = 0
    config.jev.enabled = False
    config.server.auth_token = auth_token
    config.server.auth_mode = auth_mode
    config.server.allowed_hosts = list(allowed_hosts or [])
    server = create_server(config, backend_factory=lambda slug, name: FakeBackend(['Hello']))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def call(server, method, path, body=None, headers=None, host=None):
    """Send one request. `host` overrides the Host header, which is how a proxied
    request is distinguished from a direct loopback one."""
    connection = http.client.HTTPConnection(*server.server_address, timeout=5)
    encoded = None if body is None else json.dumps(body)
    supplied = {'Content-Type': 'application/json'}
    if host:
        supplied['Host'] = host
    supplied.update(headers or {})
    connection.request(method, path, body=encoded, headers=supplied)
    response = connection.getresponse()
    data = response.read()
    status = response.status
    connection.close()
    # Static UI answers with HTML, not JSON; only decode when it looks like JSON.
    if data and data[:1] in (b'{', b'['):
        return status, json.loads(data)
    return status, data


PROXY_HOST = 'core.example.test'


def test_no_token_configured_means_no_auth_at_all(tmp_path):
    """The historical desktop posture must be untouched when no key is set."""
    with running_server(tmp_path) as server:
        status, _ = call(server, 'GET', '/api/status')
        assert status == 200


def test_direct_loopback_needs_no_key_in_the_default_mode(tmp_path):
    """THE ANTI-STRANDING PROPERTY. A local client with no key still works."""
    with running_server(tmp_path, auth_token=KEY) as server:
        status, body = call(server, 'GET', '/api/status')
        assert status == 200, body
        # And a local write still works, not only a read.
        status, body = call(server, 'POST', '/api/sessions', {'title': 'local'})
        assert status == 201, body


def test_a_wrong_key_on_loopback_is_also_not_a_lockout(tmp_path):
    """A stale key in localStorage must not brick the local page."""
    with running_server(tmp_path, auth_token=KEY) as server:
        status, body = call(server, 'GET', '/api/status', headers={'X-Tessera-Key': 'wrong'})
        assert status == 200, body


def test_a_proxied_request_without_the_key_is_refused(tmp_path):
    with running_server(tmp_path, auth_token=KEY, allowed_hosts=[PROXY_HOST]) as server:
        status, body = call(server, 'GET', '/api/status', host=PROXY_HOST)
        assert status == 401, body
        assert body['error'] == 'Authentication required'


def test_a_proxied_request_with_a_wrong_key_is_refused(tmp_path):
    with running_server(tmp_path, auth_token=KEY, allowed_hosts=[PROXY_HOST]) as server:
        status, body = call(server, 'GET', '/api/status', host=PROXY_HOST,
                            headers={'X-Tessera-Key': 'not-the-key'})
        assert status == 401, body


@pytest.mark.parametrize('style', ['bearer', 'header', 'query'])
def test_all_three_key_forms_are_accepted_through_a_proxy(tmp_path, style):
    """No single form covers every client: the iOS app sends Bearer, the web UI
    sends a header, and EventSource can only use the query string."""
    with running_server(tmp_path, auth_token=KEY, allowed_hosts=[PROXY_HOST]) as server:
        if style == 'bearer':
            status, body = call(server, 'GET', '/api/status', host=PROXY_HOST,
                                headers={'Authorization': 'Bearer ' + KEY})
        elif style == 'header':
            status, body = call(server, 'GET', '/api/status', host=PROXY_HOST,
                                headers={'X-Tessera-Key': KEY})
        else:
            status, body = call(server, 'GET', '/api/status?key=' + KEY, host=PROXY_HOST)
        assert status == 200, body


def test_the_event_stream_accepts_the_query_form(tmp_path):
    """The live transcript is an EventSource, which cannot set headers. If the
    query form did not work, turning auth on would look like a dead page."""
    with running_server(tmp_path, auth_token=KEY, allowed_hosts=[PROXY_HOST]) as server:
        connection = http.client.HTTPConnection(*server.server_address, timeout=5)
        connection.request('GET', '/api/sessions/x/events?key=' + KEY, headers={'Host': PROXY_HOST})
        response = connection.getresponse()
        assert response.status != 401
        connection.close()


def test_static_ui_is_served_without_the_key(tmp_path):
    """The page must load so a missing key shows an explanation, not a blank
    window. Static files are code, not data."""
    with running_server(tmp_path, auth_token=KEY, allowed_hosts=[PROXY_HOST]) as server:
        status, _ = call(server, 'GET', '/', host=PROXY_HOST)
        assert status == 200


def test_always_mode_requires_the_key_on_loopback_too(tmp_path):
    with running_server(tmp_path, auth_token=KEY, auth_mode='always') as server:
        status, body = call(server, 'GET', '/api/status')
        assert status == 401, body
        status, body = call(server, 'GET', '/api/status', headers={'X-Tessera-Key': KEY})
        assert status == 200, body


def test_always_mode_still_serves_the_static_page(tmp_path):
    with running_server(tmp_path, auth_token=KEY, auth_mode='always') as server:
        status, _ = call(server, 'GET', '/')
        assert status == 200


def test_always_mode_without_a_key_is_refused_at_config_time(tmp_path):
    """A posture that locks every client out must fail loudly, not silently."""
    config = Config(home=tmp_path, cwd=tmp_path)
    config.server.auth_mode = 'always'
    config.server.auth_token = ''
    with pytest.raises(ValueError, match="requires server.auth_token"):
        config.validate()


def test_a_key_with_whitespace_is_refused(tmp_path):
    config = Config(home=tmp_path, cwd=tmp_path)
    config.server.auth_token = 'has space'
    with pytest.raises(ValueError, match='no whitespace'):
        config.validate()


def test_an_unknown_auth_mode_is_refused(tmp_path):
    config = Config(home=tmp_path, cwd=tmp_path)
    config.server.auth_mode = 'sometimes'
    with pytest.raises(ValueError, match="'loopback' or 'always'"):
        config.validate()


def test_the_key_is_not_echoed_in_the_error(tmp_path):
    """A refusal must not repeat the credential, and must not say whether the
    presented key was close. One message for every failure."""
    with running_server(tmp_path, auth_token=KEY, allowed_hosts=[PROXY_HOST]) as server:
        status, body = call(server, 'GET', '/api/status', host=PROXY_HOST,
                            headers={'X-Tessera-Key': 'almost-' + KEY[:-1]})
        assert status == 401
        assert KEY not in json.dumps(body)
