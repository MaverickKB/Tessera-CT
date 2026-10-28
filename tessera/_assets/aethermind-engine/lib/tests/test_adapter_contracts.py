"""Pass 2 conformance tests for library, CLI, and MCP adapters."""

from __future__ import annotations

import importlib.util
import json
import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from aethermind import core
from aethermind.cli import main
from aethermind.core import (
    AetherMind,
    AutoInitBlocked,
    LockUnavailableError,
    initialize_store,
    runtime_capabilities,
)


MCP_PATH = Path(__file__).resolve().parents[2] / "mcp" / "aethermind_mcp.py"


def _load_mcp_module():
    spec = importlib.util.spec_from_file_location("aethermind_mcp_contract_test", MCP_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _json_output(capsys):
    return json.loads(capsys.readouterr().out)


def test_library_defaults_to_no_auto_init_and_explicit_initializer_is_idempotent(tmp_path):
    fresh = tmp_path / "fresh"
    aether = AetherMind(fresh)
    with pytest.raises(AutoInitBlocked):
        aether.write_layer("discovery", "blocked", "adapter/init")
    assert not (fresh / ".aethermind").exists()

    first = initialize_store(fresh)
    second = initialize_store(fresh)
    assert first["created"] is True
    assert second["created"] is False
    assert first["project_id"] == second["project_id"]
    assert str(uuid.UUID(first["project_id"])) == first["project_id"]
    metadata = json.loads((fresh / ".aethermind" / "project.json").read_text())
    assert metadata["project_id"] == first["project_id"]

    written = AetherMind(fresh).write_layer("discovery", "allowed", "adapter/init")
    assert written.id == "0001"


def test_invalid_existing_project_id_fails_closed_on_explicit_initialization(tmp_path):
    store = tmp_path / ".aethermind"
    store.mkdir()
    (store / "project.json").write_text(
        json.dumps({"project_id": "copied-human-label"}), encoding="utf-8"
    )

    with pytest.raises(core.RegistryError, match="project_id"):
        initialize_store(tmp_path)


def test_cli_requires_explicit_initialization_and_uses_shared_error_envelope(tmp_path, capsys):
    fresh = tmp_path / "cli-fresh"
    result = main([
        "write-layer", "--project-root", str(fresh),
        "--type", "discovery", "--body", "blocked", "--ctx", "adapter/cli",
    ])
    blocked = _json_output(capsys)
    assert result == 1
    assert blocked["error_code"] == "AUTO_INIT_BLOCKED"
    assert not (fresh / ".aethermind").exists()

    assert main(["init", "--project-root", str(fresh)]) == 0
    initialized = _json_output(capsys)
    assert initialized["success"] is True
    assert initialized["created"] is True

    assert main([
        "write-layer", "--project-root", str(fresh),
        "--type", "discovery", "--body", "allowed", "--ctx", "adapter/cli",
    ]) == 0
    assert _json_output(capsys)["layer"]["id"] == "0001"


def test_bool_confidence_is_rejected_consistently_by_cli_and_mcp(tmp_path, capsys, monkeypatch):
    cli_root = tmp_path / "cli"
    payload = {
        "project_root": str(cli_root), "create": True,
        "type": "discovery", "body": "bad", "ctx": "adapter/conf", "conf": True,
    }
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(json.dumps(payload)))
    assert main(["write-layer", "--payload", "-"]) == 1
    cli_result = _json_output(capsys)
    assert cli_result["error_code"] == "INVALID_ARGUMENT"
    assert not (cli_root / ".aethermind" / "layers.aem").exists()

    mcp = _load_mcp_module()
    mcp_root = tmp_path / "mcp"
    mcp_result = json.loads(mcp.write_layer(
        str(mcp_root), "discovery", "bad", "adapter/conf", True, [], create=True
    ))
    assert mcp_result["success"] is False
    assert mcp_result["error_code"] == "INVALID_ARGUMENT"
    assert not (mcp_root / ".aethermind" / "layers.aem").exists()

    transport_tool = mcp.mcp._tool_manager.get_tool("write_layer")
    with pytest.raises(Exception, match="valid number"):
        transport_tool.fn_metadata.arg_model.model_validate(
            {
                "project_root": str(tmp_path / "transport"),
                "layer_type": "load-bearing",
                "body": "bad",
                "ctx": "adapters/validation",
                "conf": True,
                "markers": ["!"],
            }
        )


def test_empty_thread_key_is_rejected_consistently_by_cli_and_mcp(
    tmp_path, capsys, monkeypatch
):
    cli_root = tmp_path / "cli-thread"
    payload = {
        "project_root": str(cli_root),
        "create": True,
        "type": "discovery",
        "body": "bad thread",
        "ctx": "adapter/thread",
        "thread_key": "",
    }
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(json.dumps(payload)))
    assert main(["write-layer", "--payload", "-"]) == 1
    cli_result = _json_output(capsys)
    assert cli_result["error_code"] == "INVALID_ARGUMENT"
    assert not (cli_root / ".aethermind" / "layers.aem").exists()

    mcp = _load_mcp_module()
    mcp_root = tmp_path / "mcp-thread"
    mcp_result = json.loads(mcp.write_layer(
        str(mcp_root),
        "discovery",
        "bad thread",
        "adapter/thread",
        1.0,
        [],
        thread_key="",
        create=True,
    ))
    assert mcp_result["error_code"] == "INVALID_ARGUMENT"
    assert not (mcp_root / ".aethermind" / "layers.aem").exists()


def test_all_adapters_report_the_same_runtime_capability_identity(tmp_path, capsys):
    initialize_store(tmp_path)
    expected = runtime_capabilities(tmp_path)

    assert main(["capabilities", "--project-root", str(tmp_path)]) == 0
    cli_result = _json_output(capsys)
    mcp = _load_mcp_module()
    mcp_result = json.loads(mcp.capabilities(str(tmp_path)))

    assert cli_result["runtime"] == expected
    assert mcp_result["runtime"] == expected
    assert expected["safe_default_create"] is True
    assert expected["lock_backend"] == "fcntl"
    assert expected["implementation_sha256"]


def test_new_texture_writes_use_canonical_header_without_rewriting_legacy_content(tmp_path):
    initialize_store(tmp_path)
    path = tmp_path / ".aethermind" / "texture.aem"
    path.write_text("legacy body\nContext: old/context\nMarker: ~\n\n", encoding="utf-8")

    entry = AetherMind(tmp_path).write_texture(
        body="The safety boundary feels clear.",
        ctx="adapter/texture",
        marker="~",
        author="texture-test",
    )
    text = path.read_text(encoding="utf-8")

    assert text.startswith("legacy body\nContext: old/context\nMarker: ~\n\n")
    assert re.search(
        r"\d{4}-\d{2}-\d{2} \[texture-test \| adapter/texture\]\n"
        r"~The safety boundary feels clear\.\n---\n$",
        text,
    )
    assert entry["author"] == "texture-test"


def test_registry_updates_are_locked_atomic_and_project_metadata_is_verified(tmp_path, monkeypatch):
    registry = tmp_path / "registry.json"
    monkeypatch.setattr(core, "_REGISTRY_PATH", registry)

    def register(index: int):
        return core.register_project_key(tmp_path / f"project-{index}", f"key-{index}")

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(register, range(20)))

    payload = json.loads(registry.read_text(encoding="utf-8"))
    assert len(results) == 20
    assert len(payload) == 20
    for index in range(20):
        root = tmp_path / f"project-{index}"
        metadata = json.loads((root / ".aethermind" / "project.json").read_text())
        assert metadata["key"] == f"key-{index}"
        assert AetherMind(f"key-{index}").project_root == root.resolve()


def test_missing_lock_backend_fails_closed_before_creating_data_file(tmp_path, monkeypatch):
    initialize_store(tmp_path)
    monkeypatch.setattr(core, "fcntl", None)
    aether = AetherMind(tmp_path)

    with pytest.raises(LockUnavailableError, match="locking backend"):
        aether.write_layer("discovery", "blocked", "adapter/lock")
    assert not (tmp_path / ".aethermind" / "layers.aem").exists()


def test_cli_and_mcp_share_currentness_and_thread_key(tmp_path, capsys):
    mcp_module = _load_mcp_module()
    initialize_store(tmp_path)
    assert main([
        "write-layer",
        "--project-root",
        str(tmp_path),
        "--type",
        "discovery",
        "--body",
        "old claim",
        "--ctx",
        "claim/adapter",
        "--thread-key",
        "adapter.claim",
    ]) == 0
    first = _json_output(capsys)
    assert main([
        "write-layer",
        "--project-root",
        str(tmp_path),
        "--type",
        "correction",
        "--body",
        "new claim",
        "--ctx",
        "claim/adapter",
        "--thread-key",
        "adapter.claim",
        "--supersedes",
        first["layer"]["id"],
    ]) == 0
    second = _json_output(capsys)

    assert main(["currentness", "--project-root", str(tmp_path)]) == 0
    cli_projection = _json_output(capsys)
    mcp_projection = json.loads(mcp_module.currentness(str(tmp_path)))

    assert [item["id"] for item in cli_projection["active_heads"]] == [
        second["layer"]["id"]
    ]
    mcp_ids = [item["id"] for item in mcp_projection["active_heads"]]
    assert mcp_ids == [item["id"] for item in cli_projection["active_heads"]]
    assert mcp_projection["inactive"] == cli_projection["inactive"]
