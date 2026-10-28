import json

import pytest

from integration_helpers import FakeMcpServer
from tessera.aethermind import AetherMindClient
from tessera.capture import Capture
from tessera.hooks import HookBus
from tessera.mcp_client import McpError


def test_path_dialect_uses_runtime_types_required_markers_ref_and_create():
    with FakeMcpServer(dialect="path") as server:
        client = AetherMindClient(endpoints=[server.url], timeout=1, create_project=True,
                                  read_only_projects=["/personal-placeholder"])
        for category, expected in (("fact", "discovery"), ("preference", "discovery"),
                                   ("failure", "friction"), ("decision", "decision"), ("correction", "correction")):
            layer = {"type": category, "body": "A useful fact", "ctx": "test/turn", "author": "tester"}
            result = client.write_layer("/project-placeholder", category, layer)
            assert result["success"] is True
            written = server.storage["layers"][-1]
            assert written["layer_type"] == expected
            assert written["markers"] == [] and written["ref"] == category and written["create"] is True
        assert "layers" in client.prepare_inheritance("/project-placeholder", None, 500)
        assert client.tree_view("/personal-placeholder")["view"] == "personal history"
        with pytest.raises(ValueError, match="read-only"):
            client.write_layer("/personal-placeholder", "blocked", layer)
        assert server.native_write_calls == 5


def test_native_lost_ack_fails_over_and_reconciles_exact_ref_once():
    storage = {"layers": []}
    with FakeMcpServer(dialect="path", storage=storage) as first, FakeMcpServer(dialect="path", storage=storage) as second:
        first.lose_next_ack = True
        client = AetherMindClient(endpoints=[first.url, second.url], timeout=.5, probe_timeout=.2)
        layer = {"type": "fact", "body": "Keep once", "ctx": "test/turn", "author": "tester"}
        result = client.write_layer("/project-placeholder", "stable-capture", layer)
        assert result["deduplicated"] is True
        assert len(storage["layers"]) == 1
        assert first.native_write_calls == 1 and second.native_write_calls == 0
        assert client.last_endpoint == second.url
        assert client.status()["endpoints"][0]["healthy"] is False
        restored = AetherMindClient(endpoints=[second.url], timeout=.5)
        assert restored.write_layer("/project-placeholder", "stable-capture", layer)["deduplicated"] is True
        assert len(storage["layers"]) == 1


@pytest.mark.parametrize("dialect", ["id", "path"])
def test_call_failure_and_preferred_recovery_for_each_dialect(dialect):
    now = [0.0]
    with FakeMcpServer(dialect=dialect) as first, FakeMcpServer(dialect=dialect) as second:
        first.available = False
        client = AetherMindClient(endpoints=[first.url, second.url], timeout=.5,
                                  probe_timeout=.2, probe_interval=5, clock=lambda: now[0])
        assert client.tree_view("work")["view"] == "personal history"
        assert client.last_endpoint == second.url
        failed_probes = len(first.requests)
        assert failed_probes == 1
        first.available = True
        now[0] = 4
        assert client.tree_view("work")["view"] == "personal history"
        assert client.last_endpoint == second.url
        assert len(first.requests) == failed_probes
        now[0] = 5
        assert client.tree_view("work")["view"] == "personal history"
        assert client.last_endpoint == first.url
        first.available = False
        assert client.tree_view("work")["view"] == "personal history"
        assert client.last_endpoint == second.url
        assert [call["endpoint"] for call in client.status()["calls"]] == [
            second.url, second.url, first.url, second.url]


@pytest.mark.parametrize("dialect", ["id", "path"])
def test_healthy_preferred_endpoint_does_not_probe_fallback(dialect):
    with FakeMcpServer(dialect=dialect) as first, FakeMcpServer(dialect=dialect) as second:
        client = AetherMindClient(endpoints=[first.url, second.url], timeout=1)
        assert first.requests == second.requests == []
        assert client.tree_view("work")["view"] == "personal history"
        assert client.last_endpoint == first.url
        assert second.requests == []
        assert client.status()["endpoints"][1]["healthy"] is None


def test_id_lost_ack_failover_and_restart_preserve_exact_request():
    storage = {"layers": []}
    with FakeMcpServer(storage=storage) as first, FakeMcpServer(storage=storage) as second:
        first.lose_next_ack = True
        layer = {"type": "discovery", "body": "Keep café and 日本語 exactly.",
                 "ctx": "test/capture", "author": "tester", "markers": ["?"]}
        client = AetherMindClient(endpoints=[first.url, second.url], timeout=1)
        assert client.write_layer("work", "stable-id-placeholder", layer)["deduplicated"] is True
        assert client.last_endpoint == second.url
        restored = AetherMindClient(endpoints=[second.url], timeout=1)
        assert restored.write_layer("work", "stable-id-placeholder", layer)["deduplicated"] is True
        assert first.native_write_calls == 1 and second.native_write_calls == 0
        assert len(storage["layers"]) == 1
        written = storage["layers"][0]
        assert written["ref"] == "stable-id-placeholder"
        assert written["project_root"] == "work"
        assert written["body"] == layer["body"]
        assert written["markers"] == layer["markers"]


def test_native_reference_collision_retains_original_body_without_second_write():
    with FakeMcpServer(dialect="path") as server:
        client = AetherMindClient(endpoints=[server.url], timeout=1)
        layer = {"type": "fact", "body": "Original body", "ctx": "test", "author": "tester"}
        assert client.write_layer("/project-placeholder", "stable-ref-placeholder", layer)["success"] is True
        with pytest.raises(McpError, match="different content"):
            client.write_layer("/project-placeholder", "stable-ref-placeholder", {**layer, "body": "Changed body"})
        assert server.native_write_calls == 1
        assert len(server.storage["layers"]) == 1
        assert server.storage["layers"][0]["body"] == "Original body"


@pytest.mark.parametrize("dialect", ["id", "path"])
def test_personal_write_guard_runs_before_any_probe_and_reads_remain_allowed(dialect):
    with FakeMcpServer(dialect=dialect) as server:
        client = AetherMindClient(endpoints=[server.url], timeout=1,
                                  read_only_projects=["/personal-placeholder"])
        layer = {"type": "discovery", "body": "Blocked", "ctx": "test", "author": "tester"}
        with pytest.raises(ValueError, match="read-only"):
            client.write_layer("/personal-placeholder", "blocked-ref-placeholder", layer)
        assert server.requests == []
        assert client.tree_view("/personal-placeholder")["view"] == "personal history"
        assert server.writes == {} and server.native_write_calls == 0


def test_capture_drains_native_debt_after_restart(tmp_path):
    capture = Capture(None, tmp_path / "outbox", "/project-placeholder", "", "tester")
    hooks = HookBus()
    capture.install(hooks)
    hooks.emit("session_start", {"session_id": "offline"})
    hooks.emit("turn_start", {"message": "Remember this decision."})
    hooks.emit("turn_end", {"reply": "The decision was recorded."})
    assert len(capture.debt()) == 1
    request_id = capture.debt()[0]["request_id"]
    with FakeMcpServer(dialect="path") as server:
        client = AetherMindClient(endpoints=[server.url], timeout=.5)
        restored = Capture(client, tmp_path / "outbox", "/project-placeholder", "", "tester")
        restored.retry_outbox()
        assert restored.debt() == []
        assert server.storage["layers"][0]["ref"] == request_id
        assert server.native_write_calls == 1


def test_mcp_endpoint_without_factory_fails_loudly():
    import pytest
    client_cls = AetherMindClient
    with pytest.raises(ValueError, match='endpoint factory'):
        client_cls(endpoints=['mcp:aethermind-bundled'])


def test_mcp_endpoint_resolves_through_the_shared_catalog_factory():
    calls = []

    def factory(endpoint, token_env, timeout):
        calls.append((endpoint, token_env, timeout))
        return object()

    client = AetherMindClient(endpoints=['mcp:aethermind-bundled'], endpoint_factory=factory)
    assert 'mcp:aethermind-bundled' in client._clients
    assert calls == [('mcp:aethermind-bundled', '', 15)]
