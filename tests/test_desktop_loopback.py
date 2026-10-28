"""Desktop-owned loopback requests require no browser or provider credentials."""

from contextlib import contextmanager
import http.client
import json
import socket
import threading
from unittest.mock import Mock

import pytest

from tessera import server as server_module
from tessera.config import Config
from tessera.credentials import CredentialResolver


@contextmanager
def desktop_server(tmp_path, monkeypatch, *, configured_token=True):
    config = Config(home=tmp_path / "home", cwd=tmp_path)
    config.server.host = "127.0.0.1"
    config.server.port = 0
    if configured_token:
        monkeypatch.setenv("TESSERA_SERVER_TOKEN", "synthetic-desktop-token-placeholder")
    else:
        monkeypatch.delenv("TESSERA_SERVER_TOKEN", raising=False)
    backend = Mock(side_effect=AssertionError("Desktop metadata cannot construct a model backend"))
    credentials = Mock(side_effect=AssertionError("Desktop metadata cannot resolve credentials"))
    monkeypatch.setattr(CredentialResolver, "resolve", credentials)
    app = tmp_path / "app"
    app.mkdir()
    (app / "index.html").write_text("<title>Desktop fixture</title>")
    with server_module.create_server(config, web_root=app, backend_factory=backend) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            thread.join(timeout=5)
            assert not thread.is_alive()
    backend.assert_not_called()
    credentials.assert_not_called()


def request(server, method, path, *, body=None, headers=None):
    connection = http.client.HTTPConnection(*server.server_address, timeout=5)
    supplied = {"Content-Type": "application/json"}
    supplied.update(headers or {})
    assert "Authorization" not in supplied
    payload = None if body is None else json.dumps(body)
    try:
        connection.request(method, path, body=payload, headers=supplied)
        response = connection.getresponse()
        raw = response.read()
        assert b"synthetic-desktop-token-placeholder" not in raw
        return response.status, json.loads(raw)
    finally:
        connection.close()


@pytest.mark.parametrize("configured_token", [False, True])
def test_loopback_desktop_status_needs_no_bearer_even_with_a_configured_key(tmp_path, monkeypatch, configured_token):
    with desktop_server(tmp_path, monkeypatch, configured_token=configured_token) as server:
        status, state = request(server, "GET", "/api/status")
        assert status == 200
        assert "sessions" in state


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost"])
@pytest.mark.parametrize("same_origin", [False, True])
def test_app_owned_loopback_requests_need_no_credentials(tmp_path, monkeypatch, host, same_origin):
    with desktop_server(tmp_path, monkeypatch) as server:
        port = server.server_address[1]
        headers = {"Host": f"{host}:{port}"}
        if same_origin:
            headers["Origin"] = f"http://{host}:{port}"
        status, created = request(server, "POST", "/api/sessions", body={"title": "Desktop fixture"}, headers=headers)
        assert status == 201
        status, sessions = request(server, "GET", "/api/sessions", headers=headers)
        assert status == 200 and any(item["slug"] == created["slug"] for item in sessions)


@pytest.mark.parametrize("host", ["desktop-placeholder.example.com", "rebound.example"])
@pytest.mark.parametrize("origin", [None, "null", "file://", "tessera://desktop", "https://attacker.example"])
def test_foreign_host_or_origin_cannot_drive_the_unauthenticated_core(tmp_path, monkeypatch, host, origin):
    with desktop_server(tmp_path, monkeypatch) as server:
        port = server.server_address[1]
        for headers in ({"Host": f"{host}:{port}"}, {"Host": f"127.0.0.1:{port}", "Origin": origin or f"http://{host}:{port}"}):
            if origin is not None and "Origin" not in headers:
                headers["Origin"] = origin
            status, body = request(server, "POST", "/api/sessions", body={"title": "rebound"}, headers=headers)
            assert status == 403 and "error" in body, headers
        status, sessions = request(server, "GET", "/api/sessions")
        assert status == 200 and sessions == []


@pytest.mark.parametrize("selector", ["", "REMOTE_SERVER_KEY_PLACEHOLDER"])
def test_remote_without_explicit_credentials_fails_before_binding_or_loading_a_manager(tmp_path, monkeypatch, selector):
    config = Config(home=tmp_path, cwd=tmp_path)
    config.server.host = "0.0.0.0"
    config.server.port = 0
    if selector:
        monkeypatch.setenv("TESSERA_SERVER_TOKEN", selector)
    else:
        monkeypatch.delenv("TESSERA_SERVER_TOKEN", raising=False)
    manager = Mock(side_effect=AssertionError("Rejected remote binds cannot load session state"))
    binding = Mock(side_effect=AssertionError("Rejected remote binds cannot open a listener"))
    monkeypatch.setattr(server_module, "SessionManager", manager)
    monkeypatch.setattr(server_module, "TesseraServer", binding)
    with pytest.raises(ValueError, match="non-loopback|loopback|token"):
        server_module.create_server(config)
    manager.assert_not_called()
    binding.assert_not_called()


def test_mixed_address_resolution_cannot_misclassify_a_remote_bind_as_loopback(tmp_path, monkeypatch):
    config = Config(home=tmp_path, cwd=tmp_path)
    config.server.host = "desktop-placeholder.example.com"
    config.server.port = 0
    addresses = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (host, 0))
                 for host in ("127.0.0.1", "0.0.0.0")]
    resolver = Mock(return_value=addresses)
    monkeypatch.setattr(server_module.socket, "getaddrinfo", resolver)
    manager = Mock(side_effect=AssertionError("Mixed-address binds cannot load session state"))
    binding = Mock(side_effect=AssertionError("Mixed-address binds cannot open a listener"))
    monkeypatch.setattr(server_module, "SessionManager", manager)
    monkeypatch.setattr(server_module, "TesseraServer", binding)
    with pytest.raises(ValueError, match="non-loopback|loopback|token"):
        server_module.create_server(config)
    resolver.assert_called_once()
    manager.assert_not_called()
    binding.assert_not_called()
