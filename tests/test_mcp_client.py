from concurrent.futures import ThreadPoolExecutor
import time
import urllib.error

import pytest

from integration_helpers import FakeMcpServer
from tessera.aethermind import AetherMindClient
from tessera.mcp_client import McpClient, McpError


@pytest.mark.parametrize("mode", ["json", "sse", "sse_multiline"])
def test_initialize_notification_session_headers_and_paginated_tools(mode, monkeypatch):
    monkeypatch.setenv("TEST_MEMORY_TOKEN", "secret-test-token")
    with FakeMcpServer(mode) as server:
        client = McpClient(server.url, token_env="TEST_MEMORY_TOKEN", timeout=1)
        assert [tool["name"] for tool in client.list_tools()] == ["prepare_inheritance", "write_layer", "read_layers", "status", "capabilities", "view", "zoom"]
        assert client.call_tool("structured", {})["structuredContent"] == {"answer": 42}
        methods = [item[0]["method"] for item in server.requests]
        assert methods == ["initialize", "notifications/initialized", "tools/list", "tools/list", "tools/call"]
        for request, headers in server.requests:
            assert headers["Authorization"] == "Bearer secret-test-token"
            assert "application/json" in headers["Accept"]
            assert "text/event-stream" in headers["Accept"]
            if request["method"] != "initialize":
                assert headers["Mcp-Session-Id"] == "fake-session"
                assert headers["Mcp-Protocol-Version"] == "2025-03-26"


def test_rpc_and_tool_errors_do_not_look_successful():
    with FakeMcpServer() as server:
        client = McpClient(server.url, timeout=1)
        with pytest.raises(McpError, match="-32601"):
            client.request("rpc_error", {})
        with pytest.raises(McpError, match="tool"):
            client.call_tool("fail", {})


def test_aethermind_wrappers_pass_project_ids_and_unwrap_content():
    with FakeMcpServer("sse") as server:
        client = AetherMindClient(server.url, timeout=1)
        assert client.prepare_inheritance("work", "scope", 600) == {"inheritance": "project history", "layers": []}
        assert client.tree_view("personal", 500) == {"view": "personal history"}
        assert client.tree_zoom("work", 2, 3)["args"] == {"project_root": "work", "level": 2, "index": 3}
        assert client.read_layers("work", query="prior decision", last_n=4)["layers"] == []
        layer = {"type": "discovery", "body": "fact", "ctx": "test", "author": "tester"}
        assert client.write_layer("work", "stable", layer)["success"] is True
        assert client.list_projects("work")["success"] is True
        calls = [r[0]["params"] for r in server.requests if r[0]["method"] == "tools/call"]
        assert calls[0] == {"name": "capabilities", "arguments": {}}
        assert calls[1]["arguments"] == {"project_root": "work", "anchor": "scope", "budget": 600}
        assert server.storage["layers"][0]["body"] == layer["body"]
        assert server.storage["layers"][0]["ref"] == "stable"


def test_parallel_calls_initialize_once_and_use_unique_ids():
    with FakeMcpServer() as server:
        client = McpClient(server.url, timeout=1)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda i: client.call_tool("echo", {"i": i}), range(8)))
        assert len(results) == 8
        ids = []
        for request, _ in server.requests:
            if "id" in request:
                ids.append(request["id"])
        assert len(ids) == len(set(ids))
        assert sum(r[0]["method"] == "initialize" for r in server.requests) == 1


def test_http_failure_is_bounded_and_does_not_leak_key(monkeypatch):
    monkeypatch.setenv("TEST_MEMORY_TOKEN", "private-value")
    with FakeMcpServer() as server:
        server.available = False
        client = McpClient(server.url, token_env="TEST_MEMORY_TOKEN", timeout=.2)
        with pytest.raises(McpError) as error:
            client.list_tools()
        assert "private-value" not in str(error.value)
        assert len(server.requests) == 1


def test_transport_errors_omit_userinfo_and_query(monkeypatch):
    secret_url = 'http://user:pass@127.0.0.1:9/mcp?token=secret'
    client = McpClient('http://127.0.0.1:9/mcp', timeout=1)
    client.url = secret_url
    seen = []

    def open_timeout(request, timeout=None):
        seen.append(request.full_url)
        raise urllib.error.URLError(TimeoutError('timed out ' + request.full_url))

    def open_refused(request, timeout=None):
        seen.append(request.full_url)
        raise urllib.error.URLError(ConnectionRefusedError(111, 'Connection refused ' + request.full_url))

    def hidden(text):
        assert 'secret' not in text
        assert 'user:pass' not in text
        assert 'token=' not in text
        assert 'http://127.0.0.1:9/mcp' in text

    monkeypatch.setattr(client._opener, 'open', open_timeout)
    with pytest.raises(McpError) as timed:
        client.list_tools()
    timed_text = str(timed.value)
    hidden(timed_text)
    assert 'timeout after 1s' in timed_text
    assert 'timed out' in timed_text

    monkeypatch.setattr(client._opener, 'open', open_refused)
    with pytest.raises(McpError) as refused:
        client.list_tools()
    refused_text = str(refused.value)
    hidden(refused_text)
    assert 'Connection refused' in refused_text
    assert seen == [secret_url, secret_url]


def test_a_short_query_in_prose_survives_url_redaction(monkeypatch):
    secret_url = 'http://u:pw12345@127.0.0.1:9/mcp?a=1&token=supersecret'
    client = McpClient('http://127.0.0.1:9/mcp', timeout=1)
    client.url = secret_url
    prose = 'the note said a=1 and then continued'
    seen = []

    def open_refused(request, timeout=None):
        seen.append(request.full_url)
        raise urllib.error.URLError(ConnectionRefusedError(111, prose + ' ' + request.full_url))

    monkeypatch.setattr(client._opener, 'open', open_refused)
    with pytest.raises(McpError) as caught:
        client.list_tools()
    text = str(caught.value)
    assert seen == [secret_url]
    assert prose in text
    assert 'pw12345' not in text
    assert 'supersecret' not in text
    assert 'u:pw12345@' not in text
    assert '[REDACTED]' in text
    assert 'http://[REDACTED]@127.0.0.1:9/mcp?[REDACTED]' in text


def test_invalid_layer_is_rejected_before_network():
    client = AetherMindClient("http://127.0.0.1:1/mcp", timeout=.1)
    with pytest.raises(ValueError, match="body"):
        client.write_layer("work", "r1", {"type": "discovery", "ctx": "test", "author": "tester"})


def test_sse_keepalives_do_not_extend_request_deadline_forever():
    with FakeMcpServer("slow_sse") as server:
        client = McpClient(server.url, timeout=.1)
        started = time.monotonic()
        with pytest.raises(McpError):
            client.list_tools()
        assert time.monotonic() - started < .5


def test_write_acknowledgement_must_name_same_request_when_present():
    with FakeMcpServer(tool_results={"write_layer": {"success": True, "request_id": "different"}}) as server:
        client = AetherMindClient(server.url, timeout=1)
        layer = {"type": "discovery", "body": "fact", "ctx": "test", "author": "tester"}
        with pytest.raises(McpError, match="request"):
            client.write_layer("work", "expected", layer)


def test_path_toolset_schema_mapping_and_missing_capability():
    client = AetherMindClient("http://127.0.0.1:1/mcp")
    schemas = {
        "prepare_inheritance": ["project_root", "anchor", "budget"],
        "view": ["project_root", "budget_chars"],
        "zoom": ["project_root", "level", "index"],
        "read_layers": ["project_root", "ctx", "last_n"],
        "status": ["project_root"],
    }
    tools = []
    for name, fields in schemas.items():
        tools.append({"name": name, "inputSchema": {"type": "object", "properties": {field: {} for field in fields}, "required": ["project_root"]}})
    client.mcp.list_tools = lambda: tools
    calls = []
    def call(name, args):
        calls.append((name, args))
        return {"ok": True}
    client.call = call
    with pytest.raises(McpError, match="schema mismatch"):
        client.detect_tools()
    assert calls == []
    assert client._toolsets == client._layer_types == {}
