from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest

from aethermind import AetherMind as PublicAetherMind
from aethermind import AetherMindParseError
from aethermind.core import AetherMind as CoreAetherMind


MCP_PATH = Path(__file__).resolve().parents[2] / "mcp" / "aethermind_mcp.py"


def _load_mcp_module():
    spec = importlib.util.spec_from_file_location("aethermind_mcp_under_test", MCP_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_public_import_uses_canonical_append_only_core(tmp_path):
    public_store = PublicAetherMind(str(tmp_path), create=True)
    core_store = CoreAetherMind(str(tmp_path), create=False)

    public_store.write_layer("discovery", "first", "readback/drift", author="public")
    core_store.write_layer("discovery", "second", "readback/drift", author="core")

    text = (tmp_path / ".aethermind" / "layers.aem").read_text(encoding="utf-8")
    layers = core_store.read_layers(ctx="readback/drift")

    assert PublicAetherMind is CoreAetherMind
    assert text.count("[[layer]]") == 2
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", layers[0].ts)
    assert [layer.body for layer in layers] == ["first", "second"]


def test_malformed_toml_is_explicit_parse_failure(tmp_path):
    aethermind_dir = tmp_path / ".aethermind"
    aethermind_dir.mkdir()
    (aethermind_dir / "layers.aem").write_text('[[layer]]\nbody = "unterminated\n', encoding="utf-8")

    with pytest.raises(AetherMindParseError, match="failed to parse"):
        CoreAetherMind(str(tmp_path)).read_layers()


def test_duplicate_layer_ids_are_explicit_parse_failure(tmp_path):
    aethermind_dir = tmp_path / ".aethermind"
    aethermind_dir.mkdir()
    (aethermind_dir / "layers.aem").write_text(
        """
[[layer]]
id = "0001"
ts = "2026-05-23T00:00:00Z"
author = "first"
type = "discovery"
body = "first"
ctx = "readback/duplicate"
conf = 1.0
markers = []

[[layer]]
id = "0001"
ts = "2026-05-23T00:00:01Z"
author = "second"
type = "correction"
body = "second"
ctx = "readback/duplicate"
conf = 1.0
markers = []
""",
        encoding="utf-8",
    )

    with pytest.raises(AetherMindParseError, match="duplicate layer id: 0001"):
        CoreAetherMind(str(tmp_path)).read_layers()


def test_mcp_read_and_status_report_parse_failure(tmp_path):
    mcp = _load_mcp_module()
    aethermind_dir = tmp_path / ".aethermind"
    aethermind_dir.mkdir()
    (aethermind_dir / "layers.aem").write_text('[[layer]]\nbody = "unterminated\n', encoding="utf-8")

    read_result = json.loads(mcp.read_layers(project_root=str(tmp_path)))
    status_result = json.loads(mcp.status(project_root=str(tmp_path)))

    assert read_result["success"] is False
    assert status_result["success"] is False
    assert "failed to parse" in read_result["error"]
    assert "failed to parse" in status_result["error"]
