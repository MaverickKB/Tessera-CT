"""Native MCP inheritance tools retain library semantics and existing tools."""

import asyncio
import importlib.util
import json
from pathlib import Path

import pytest

from aethermind import AetherMind
from aethermind.inheritance import RECEIPT_SCHEMA, prepare_inheritance, prepare_discovery


@pytest.fixture
def adapter():
    path = Path(__file__).resolve().parents[2] / "mcp" / "aethermind_mcp.py"
    spec = importlib.util.spec_from_file_location("aethermind_inheritance_mcp_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def three_records(tmp_path):
    aether = AetherMind(tmp_path, create=True)
    decision = aether.write_layer("decision", "The original rationale stays visible.", "fixture/continuity")
    correction = aether.write_layer("correction", "Acknowledge the seam without silently retiring its target.",
                                    "fixture/continuity", corrects=[decision.id])
    aether.write_layer("discovery", "Optional evidence remains discoverable if omitted.", "fixture/continuity")
    return aether, decision, correction


def test_new_tools_are_read_only_and_previous_fifteen_remain(adapter):
    previous = {"write_layer", "write_texture", "read_layers", "read_texture", "status", "brief",
                "audit", "gate_check", "write_event", "read_events", "archive", "currentness",
                "initialize_store", "capabilities", "register_project_key"}
    new = {"prepare_inheritance", "discover_inheritance", "verify_inheritance_use"}
    derived = {"view", "zoom"}
    listed = asyncio.run(adapter.mcp.list_tools())
    assert {tool.name for tool in listed} == previous | new | derived
    for tool in listed:
        if tool.name in new or tool.name in derived:
            assert tool.annotations.readOnlyHint is True
            assert tool.annotations.destructiveHint is False
            assert tool.annotations.openWorldHint is False


def test_prepare_and_discovery_match_canonical_module(adapter, three_records):
    aether, original, correction = three_records
    args = dict(project_root=str(aether.project_root), anchor="fixture", budget=1,
                surface="mcp-fixture", session_id="one")
    prepared = json.loads(adapter.prepare_inheritance(**args))
    assert prepared["success"] is True
    assert prepared["envelope"] == prepare_inheritance(**args)
    assert correction.id in prepared["envelope"]["native"]["must_know"]
    assert original.id in prepared["envelope"]["currentness"]["active_ids"]
    assert prepared["envelope"]["native"]["budget_used"] > 1
    discovered = json.loads(adapter.discover_inheritance(str(aether.project_root), "mcp-fixture", "one"))
    assert discovered["manifest"] == prepare_discovery(aether.project_root, "mcp-fixture", "one")
    assert "native" not in discovered["manifest"]


def test_mcp_receipt_does_not_promote_ack_or_mutate_state(adapter, three_records):
    aether, _, correction = three_records
    args = dict(project_root=str(aether.project_root), anchor="fixture", budget=1,
                surface="mcp-fixture", session_id="one")
    envelope = json.loads(adapter.prepare_inheritance(**args))["envelope"]
    before = {p.name: p.read_bytes() for p in aether.aethermind_dir.iterdir() if p.is_file()}
    receipt = {"schema": RECEIPT_SCHEMA, "revision": envelope["revision"], "surface": "mcp-fixture",
               "session_id": "one", "kind": "applied", "record_ids": [correction.id],
               "evidence": {"response_ref": "fixture://said-understood"}}
    rejected = json.loads(adapter.verify_inheritance_use(envelope, receipt))
    assert rejected["success"] is True
    assert rejected["valid"] is False
    receipt["kind"] = "recognized"
    claimed = json.loads(adapter.verify_inheritance_use(envelope, receipt))
    assert claimed["valid"] is True
    assert claimed["evidence_verified"] is claimed["comprehension_proven"] is False
    assert claimed["debt_changed"] is claimed["authority_granted"] is False
    assert before == {p.name: p.read_bytes() for p in aether.aethermind_dir.iterdir() if p.is_file()}
    assert json.loads(adapter.prepare_inheritance(**args))["envelope"] == envelope


def test_missing_store_stays_missing_and_corrupt_store_is_explicit(adapter, tmp_path):
    result = json.loads(adapter.prepare_inheritance(str(tmp_path)))
    assert result["success"] is True
    assert result["envelope"]["provenance"]["store_exists"] is False
    assert not (tmp_path / ".aethermind").exists()
    store = tmp_path / ".aethermind"
    store.mkdir()
    (store / "layers.aem").write_text("[[layer]]\nbroken")
    invalid = json.loads(adapter.prepare_inheritance(str(tmp_path)))
    assert invalid["success"] is False
    assert "error_code" in invalid
