"""Synthetic ID-dialect wire contracts; no socket or live memory is accessed."""

from copy import deepcopy
from email.message import Message
import io
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from tessera.aethermind import AetherMindClient
from tessera.capture import Capture
from tessera.credentials import CredentialResolver
from tessera.jev import JudgeKit
from tessera.tools import ToolRegistry
from test_status_projection_consumers import disk_snapshot


NO_TREE = "No summary tree for this project yet; use read_layers or prepare_inheritance."
PROJECTS = [
    {"project_id": "alpha-id-placeholder", "name": "Alpha display label"},
    {"project_id": "beta-id-placeholder", "name": "Beta display label"},
]
READS = {"capabilities", "status", "view", "zoom", "read_layers", "prepare_inheritance"}


def tool(name, properties, required=()):
    return {"name": name, "inputSchema": {"type": "object", "properties": properties,
            "required": list(required), "additionalProperties": False}}


class Response(io.BytesIO):
    def __init__(self, body, content_type="application/json", status=200):
        super().__init__(body)
        self.status = status
        self.headers = Message()
        self.headers["Content-Type"] = content_type
        self.headers["Mcp-Session-Id"] = "in-memory-session-placeholder"


class IdWire:
    """Use the real MCP encoder/decoder with a strict in-memory HTTP response."""

    def __init__(self, mode):
        self.mode = mode
        self.requests = []
        self.rejected = []
        self.no_tree = None
        self.endpoint = "https://memory.example.com/mcp"
        reference = Path(__file__).resolve().parents[1] / "docs/reference/aethermind_server_tools.json"
        self.tools = json.loads(reference.read_text())

    def result(self, name, args):
        if name not in READS:
            self.rejected.append(name)
            raise AssertionError("Only advertised ID read tools may be called")
        schema = next(item["inputSchema"] for item in self.tools if item["name"] == name)
        assert set(args) <= set(schema["properties"])
        assert set(schema.get("required", [])) <= set(args)
        assert "project_id" not in args
        if name == "capabilities":
            return {"success": True, "runtime": {"runtime_id": "aethermind-python",
                    "runtime_version": "0.3.0", "contract_version": "adapter-v1",
                    "layer_types": ["discovery"]}}
        project = args["project_root"]
        if name == "status":
            return {"success": True, "project_id": project, "layer_count": 1}
        assert project in {item["project_id"] for item in PROJECTS}
        if name in ("view", "zoom") and self.no_tree is not None:
            return deepcopy(self.no_tree)
        if name == "view":
            return {"success": True, "project_id": project, "view": "0+2|Synthetic ID tree summary"}
        if name == "zoom":
            return {"success": True, "project_id": project, "level": args["level"], "index": args["index"],
                    "text": "Synthetic source node: café fixture"}
        layers = [{"id": "layer-id-placeholder", "project_id": project,
                   "ctx": "topic-placeholder", "body": "Synthetic retained layer"}]
        if name == "read_layers":
            return {"success": True, "project_id": project, "layers": layers, "count": 1}
        return {"project_id": project, "inheritance": "Synthetic layered history", "layers": layers}

    def open(self, request, timeout):
        assert request.full_url == self.endpoint and timeout > 0
        payload = json.loads(request.data)
        self.requests.append(payload)
        method = payload["method"]
        params = payload["params"]
        if method == "initialize":
            result = {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}}}
        elif method == "notifications/initialized":
            return Response(b"", status=202)
        elif method == "tools/list":
            if params.get("cursor"):
                assert params == {"cursor": "read-page-placeholder"}
                result = {"tools": self.tools[2:]}
            else:
                result = {"tools": self.tools[:2], "nextCursor": "read-page-placeholder"}
        else:
            assert method == "tools/call"
            value = self.result(params["name"], params["arguments"])
            result = {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}]}
            if self.mode == "structured":
                result["structuredContent"] = {"result": json.dumps(value, ensure_ascii=False)}
                result["content"] = [{"type": "text", "text": "Ignored fallback placeholder"}]
        envelope = {"jsonrpc": "2.0", "id": payload["id"], "result": result}
        raw = json.dumps(envelope, ensure_ascii=False).encode()
        if self.mode == "sse":
            prefix = b': keepalive\n\ndata: {"jsonrpc":"2.0","method":"notice"}\n\n'
            return Response(prefix + b"data: " + raw + b"\n\n", "text/event-stream")
        return Response(raw)

    def calls(self):
        return [request["params"] for request in self.requests if request["method"] == "tools/call"]


@pytest.fixture(autouse=True)
def no_live_or_mutating_capabilities(monkeypatch):
    tripwires = []
    for target in ("socket.socket.connect", "socket.socket.connect_ex", "socket.socket.sendto",
                   "socket.getaddrinfo", "urllib.request.urlopen", "urllib.request.OpenerDirector.open",
                   "subprocess.Popen"):
        tripwire = Mock(side_effect=AssertionError("ID fixtures cannot access network or processes"))
        monkeypatch.setattr(target, tripwire)
        tripwires.append(tripwire)
    for owner, name in ((AetherMindClient, "write_layer"), (Capture, "_enqueue"),
                        (Capture, "retry_outbox"), (CredentialResolver, "resolve"), (JudgeKit, "_evaluate_batch")):
        tripwire = Mock(side_effect=AssertionError("Read-only fixtures cannot mutate or call providers"))
        monkeypatch.setattr(owner, name, tripwire)
        tripwires.append(tripwire)
    native_file = Mock(side_effect=AssertionError("Client must never inspect native memory files"))
    tripwires.append(native_file)
    original_open = Path.open

    def opened(path, *args, **kwargs):
        if path.name in ("tree.aem", "layers.aem", "texture.aem"):
            return native_file()
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", opened)
    yield
    for tripwire in tripwires:
        tripwire.assert_not_called()


def client_for(wire):
    client = AetherMindClient(endpoints=[wire.endpoint], token_env="", timeout=1,
                              read_only_projects=[project["project_id"] for project in PROJECTS])
    client.mcp._opener = wire
    return client


@pytest.mark.parametrize("mode", ["json", "sse", "structured"])
@pytest.mark.parametrize("ordinal", [0, 1])
def test_id_catalogue_selects_exact_target_and_all_read_successes_round_trip(mode, ordinal):
    wire = IdWire(mode)
    client = client_for(wire)
    assert wire.requests == []
    project = PROJECTS[ordinal]["project_id"]
    assert project != PROJECTS[ordinal]["name"]
    assert client.list_projects(project) == {"success": True, "project_id": project, "layer_count": 1}
    assert client.tree_view(project, budget_chars=120) == {
        "success": True, "project_id": project, "view": "0+2|Synthetic ID tree summary"}
    assert client.tree_zoom(project, 2, 1) == {
        "success": True, "project_id": project, "level": 2, "index": 1,
        "text": "Synthetic source node: café fixture"}
    layers = client.read_layers(project, query="topic-placeholder", ctx_prefix="topic", last_n=2)
    assert layers["layers"] == [{"id": "layer-id-placeholder", "project_id": project,
        "ctx": "topic-placeholder", "body": "Synthetic retained layer"}]
    assert wire.calls() == [
        {"name": "capabilities", "arguments": {}},
        {"name": "status", "arguments": {"project_root": project}},
        {"name": "view", "arguments": {"project_root": project, "budget_chars": 120}},
        {"name": "zoom", "arguments": {"project_root": project, "level": 2, "index": 1}},
        {"name": "read_layers", "arguments": {"project_root": project,
            "ctx_prefix": "topic", "last_n": client.recall_scan}},
    ]
    assert wire.rejected == []
    assert [request["method"] for request in wire.requests[:4]] == [
        "initialize", "notifications/initialized", "tools/list", "tools/list"]
    before = len(wire.requests)
    assert client.status()["endpoints"][0]["healthy"] is True
    assert len(wire.requests) == before


@pytest.mark.parametrize("mode", ["json", "sse", "structured"])
@pytest.mark.parametrize("shape", ["text", "message", "unsuccessful"])
def test_no_tree_is_normal_read_state_with_layers_and_inheritance_available(mode, shape, tmp_path):
    wire = IdWire(mode)
    wire.no_tree = NO_TREE
    if shape == "message":
        wire.no_tree = {"message": NO_TREE, "tree_available": False}
    elif shape == "unsuccessful":
        wire.no_tree = {"success": False, "error": NO_TREE}
    client = client_for(wire)
    project = PROJECTS[0]["project_id"]
    personal = PROJECTS[1]["project_id"]
    assert client.tree_view(project) == wire.no_tree
    assert client.tree_zoom(project, 0, 0) == wire.no_tree
    capture = Capture(client, tmp_path / "outbox", project, personal_project=personal)
    before = disk_snapshot(tmp_path)
    block = capture.inheritance_block()
    assert "Synthetic layered history" in block and NO_TREE in block
    assert "Personal memory unavailable" not in block and "Project inheritance unavailable" not in block
    assert "Memory service unavailable" not in block
    assert capture._inheritance == block and capture.debt() == []
    reads = len(wire.calls())
    assert capture.inheritance_block() == block and len(wire.calls()) == reads
    registry = ToolRegistry()
    capture.register_tools(registry)
    recall = registry.restricted(["memory_recall"])
    result = recall.execute("memory_recall", {"query": "topic-placeholder", "project_id": personal})
    assert result["success"] is True and result["layers"][0]["project_id"] == personal
    assert recall.execute("memory_recall", {"level": 0, "index": 0, "project_id": personal}) == wire.no_tree
    assert client.status()["endpoints"][0]["healthy"] is True
    assert all(call["name"] in READS for call in wire.calls()) and wire.rejected == []
    assert wire.calls()[-2:] == [
        {"name": "read_layers", "arguments": {"project_root": personal, "last_n": client.recall_scan}},
        {"name": "zoom", "arguments": {"project_root": personal, "level": 0, "index": 0}},
    ]
    assert disk_snapshot(tmp_path) == before
