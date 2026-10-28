"""Configured memory reaches the session; a missing packaged engine keeps write-debt."""

import json

import pytest

from integration_helpers import FakeMcpServer
from tessera.aethermind import AetherMindClient
from tessera.capture import Capture
from tessera.config import load_config
from tessera.loop import Session
from fake_backend import FakeBackend


def test_ordered_endpoints_and_probe_settings_load_from_env(tmp_path, monkeypatch):
    (tmp_path / "config.toml").write_text('''
[aethermind]
url = "https://memory.example.com/alias"
endpoints = ["https://memory.example.com/first", "https://memory.example.com/second"]
probe_timeout = 0.5
probe_interval = 3
create_project = true
''')
    monkeypatch.setenv("TESSERA_AETHERMIND_PROBE_INTERVAL", "7")
    config = load_config(tmp_path)
    assert config.aethermind.endpoints == [
        "https://memory.example.com/first", "https://memory.example.com/second"]
    assert config.aethermind.probe_timeout == .5
    assert config.aethermind.probe_interval == 7
    assert config.aethermind.create_project is True


@pytest.mark.parametrize("setting", [
    'endpoints = "https://memory.example.com/mcp"',
    'endpoints = [1]',
    'endpoints = ["file:///project-placeholder"]',
    'endpoints = ["https://user:secret@memory.example.com/mcp"]',
    'probe_timeout = 0',
    'probe_interval = -1',
    'create_project = "true"',
])
def test_invalid_memory_settings_fail_before_network(tmp_path, setting):
    (tmp_path / "config.toml").write_text("[aethermind]\n" + setting)
    with pytest.raises(ValueError):
        load_config(tmp_path)


@pytest.mark.parametrize("use_alias", [False, True])
def test_session_wires_native_capture_and_protects_personal_project(tmp_path, use_alias):
    with FakeMcpServer(dialect="path") as server:
        endpoint_setting = "url" if use_alias else "endpoints"
        endpoint_value = server.url if use_alias else [server.url]
        (tmp_path / "config.toml").write_text(f'''
[aethermind]
{endpoint_setting} = {json.dumps(endpoint_value)}
project = "/project-placeholder"
personal_project = "/personal-placeholder"
create_project = true
probe_timeout = 0.2
probe_interval = 0
''')
        config = load_config(tmp_path)
        session = Session(config, backend=FakeBackend([{"content": "Recorded."}]))
        try:
            client = session.capture.client
            assert client is not None
            assert client.create_project is True
            assert client._pool.probe_timeout == .2
            assert client._pool.probe_interval == 0
            with pytest.raises(ValueError, match="read-only"):
                client.write_layer("/personal-placeholder", "blocked", {})
            session.run_turn("Remember the tested configuration.")
            assert session.capture.debt() == []
        finally:
            session.close()
        assert server.native_write_calls == 2
        assert all(layer["project_root"] == "/project-placeholder"
                   and layer["create"] is True for layer in server.storage["layers"])


@pytest.mark.aethermind_unavailable
def test_enabled_without_endpoints_reports_a_missing_packaged_engine(tmp_path):
    (tmp_path / "config.toml").write_text('[aethermind]\n')
    session = Session(load_config(tmp_path), backend=FakeBackend([]))
    try:
        assert session.capture.client is None
        assert session.memory_choice["source"] == "embedded"
        assert session.memory_choice["url"] == ""
        assert session.memory_choice["reason"].startswith("bytes present, not started")
        assert "build receipt" in session.memory_choice["reason"]
        assert "unavailable" in session.capture.inheritance_block().lower()
    finally:
        session.close()


@pytest.mark.aethermind_unavailable
@pytest.mark.parametrize("mode", ["defaults", "invalid_external", "receipt_less"])
def test_a_missing_packaged_engine_refuses_the_turn(tmp_path, monkeypatch, mode):
    from tessera.bundled_engine import AetherMindUnavailable
    from tessera.config import Config
    from tessera.mcp_client import McpError

    config = Config(home=tmp_path, cwd=tmp_path)
    config.tools.approval = 'yolo'
    if mode == "invalid_external":
        config.aethermind.endpoints = ["https://memory.example.com/mcp"]

        def reject(checked):
            raise McpError("MCP HTTP 401")

        # The probe stands in for the external service so this file never opens a socket.
        monkeypatch.setattr("tessera.bundled_engine.probe_external", reject)
    elif mode == "receipt_less":
        from local_memory import receipt_less_bundle
        config.aethermind.bundled_path = str(receipt_less_bundle(tmp_path))
        assert "aethermind.enabled" not in config.retired_settings

    def unexpected_client(*args, **kwargs):
        pytest.fail("A missing packaged engine must not construct a transport")

    monkeypatch.setattr("tessera.loop.AetherMindClient", unexpected_client)
    backend = FakeBackend(["A local reply."])
    with Session(config, backend=backend) as session:
        assert session.capture.client is None
        assert session.memory_choice["source"] == "embedded"
        if mode == "invalid_external":
            assert session.memory_choice["external_error_name"] == "McpError"
            assert "401" in session.memory_choice["external_error"]
            text = session._turn_state("main", [])
            assert "AetherMind external service was not used (McpError: MCP HTTP 401)." in text
            assert "bytes present, not started" in text
            assert "build receipt" in text
        else:
            assert session.memory_choice["external_error"] is None
            assert session.memory_choice["url"] == ""
            if mode == "receipt_less":
                assert "bytes present, not started" not in session.memory_choice["reason"]
                assert "not in this install" in session.memory_choice["reason"]
            else:
                assert session.memory_choice["reason"].startswith("bytes present, not started")
                assert "build receipt" in session.memory_choice["reason"]
        with pytest.raises(AetherMindUnavailable, match="AetherMind is not available: ") as refused:
            session.run_turn("Remember these synthetic words.")
        if mode == "receipt_less":
            assert "not in this install" in str(refused.value)
        else:
            assert "bytes present, not started" in str(refused.value)
        assert backend.calls == []
        assert session.capture.debt() == []


def test_changed_project_retains_old_debt_and_flushes_only_current_destination(tmp_path):
    original = Capture(None, tmp_path / "outbox", "/previous-placeholder")
    original.session_id = "old"
    original.turn_start({"message": "Keep the original destination."})
    original.turn_end({"reply": "Queued."})
    old_debt = original.debt()
    with FakeMcpServer(dialect="path") as server:
        client = AetherMindClient(endpoints=[server.url], timeout=1)
        current = Capture(client, tmp_path / "outbox", "/project-placeholder")
        current.retry_outbox()
        assert current.debt() == old_debt
        assert server.native_write_calls == 0
        assert "different project" in current.inheritance_block()
        current.session_id = "new"
        current.turn_start({"message": "Record the current project."})
        current.turn_end({"reply": "Recorded."})
        assert current.debt() == old_debt
        assert server.native_write_calls == 1
        assert server.storage["layers"][0]["project_root"] == "/project-placeholder"
