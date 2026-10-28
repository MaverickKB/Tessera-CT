"""MCP read_layers posture field and filter. Fixture only; no canonical writes."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

MCP_PATH = Path(__file__).resolve().parents[2] / "mcp" / "aethermind_mcp.py"


def _load_mcp():
    spec = importlib.util.spec_from_file_location("aethermind_mcp_posture", MCP_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def store(tmp_path):
    from aethermind.core import AetherMind

    aether = AetherMind(str(tmp_path), create=True)
    live = aether.write_layer("note", "still live", "posture/live", author="test")
    archived = aether.write_layer("note", "will be archived", "posture/old", author="test")
    aether.archive([archived.id], reason="fixture")
    return tmp_path, live.id, archived.id


def test_layer_dict_carries_posture(store):
    mcp = _load_mcp()
    root, live_id, archived_id = store
    payload = json.loads(mcp.read_layers(project_root=str(root)))
    by_id = {layer["id"]: layer for layer in payload["layers"]}
    assert by_id[live_id]["posture"] == "live"
    assert by_id[archived_id]["posture"] == "archived"


def test_live_filter_excludes_archived(store):
    mcp = _load_mcp()
    root, live_id, archived_id = store
    payload = json.loads(mcp.read_layers(project_root=str(root), posture="live"))
    ids = {layer["id"] for layer in payload["layers"]}
    assert live_id in ids
    assert archived_id not in ids
    assert all(layer["posture"] == "live" for layer in payload["layers"])


def test_default_filter_count_matches_unfiltered(store):
    mcp = _load_mcp()
    root, _, _ = store
    default = json.loads(mcp.read_layers(project_root=str(root)))
    explicit_all = json.loads(mcp.read_layers(project_root=str(root), posture="all"))
    from aethermind.core import AetherMind

    core_count = len(AetherMind(str(root), create=False).read_layers())
    assert default["count"] == core_count
    assert explicit_all["count"] == core_count
    assert {layer["id"] for layer in default["layers"]} == {
        layer["id"] for layer in explicit_all["layers"]
    }
