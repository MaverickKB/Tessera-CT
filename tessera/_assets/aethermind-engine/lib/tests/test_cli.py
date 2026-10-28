from __future__ import annotations

import io
import json
import sys

from aethermind.cli import main


def _json_output(capsys):
    return json.loads(capsys.readouterr().out)


def test_cli_write_layer_and_read_layers(tmp_path, capsys):
    result = main([
        "write-layer",
        "--project-root",
        str(tmp_path),
        "--type",
        "discovery",
        "--body",
        "host-local CLI proof",
        "--ctx",
        "transport/cli",
        "--author",
        "test-agent",
        "--create",
        "--marker",
        ">",
    ])

    written = _json_output(capsys)

    assert result == 0
    assert written["success"] is True
    assert written["layer"]["author"] == "test-agent"
    assert written["layer"]["ctx"] == "transport/cli"

    result = main(["read-layers", "--project-root", str(tmp_path), "--ctx", "transport/cli"])
    readback = _json_output(capsys)

    assert result == 0
    assert readback["count"] == 1
    assert readback["layers"][0]["body"] == "host-local CLI proof"


def test_cli_write_layer_from_stdin_payload(tmp_path, capsys, monkeypatch):
    payload = {
        "project_root": str(tmp_path),
        "create": True,
        "type": "load-bearing",
        "body": "!remote executor should run this where files live",
        "ctx": "transport/stdin",
        "conf": 0.97,
        "markers": ["!"],
        "author": "example-writer",
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))

    result = main(["write-layer", "--payload", "-"])
    written = _json_output(capsys)

    assert result == 0
    assert written["layer"]["author"] == "example-writer"
    assert written["layer"]["type"] == "load-bearing"
    assert written["layer"]["markers"] == ["!"]


def test_cli_write_layer_preserves_supersedes_payload(tmp_path, capsys, monkeypatch):
    payload = {
        "project_root": str(tmp_path),
        "create": True,
        "type": "correction",
        "body": "newer relation claim",
        "ctx": "transport/supersession",
        "author": "example-writer",
        "supersedes": ["0001"],
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))

    result = main(["write-layer", "--payload", "-"])
    written = _json_output(capsys)

    assert result == 0
    assert written["layer"]["supersedes"] == ["0001"]

    result = main(["read-layers", "--project-root", str(tmp_path), "--ctx", "transport/supersession"])
    readback = _json_output(capsys)

    assert result == 0
    assert readback["layers"][0]["supersedes"] == ["0001"]


def test_cli_write_layer_preserves_rollback_of_payload(tmp_path, capsys, monkeypatch):
    payload = {
        "project_root": str(tmp_path),
        "create": True,
        "type": "correction",
        "body": "rollback relation claim",
        "ctx": "transport/rollback",
        "author": "example-writer",
        "rollback_of": ["0001"],
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))

    result = main(["write-layer", "--payload", "-"])
    written = _json_output(capsys)

    assert result == 0
    assert written["layer"]["rollback_of"] == ["0001"]

    result = main(["read-layers", "--project-root", str(tmp_path), "--ctx", "transport/rollback"])
    readback = _json_output(capsys)

    assert result == 0
    assert readback["layers"][0]["rollback_of"] == ["0001"]


def test_cli_write_layer_preserves_artifact_reference_payload(tmp_path, capsys, monkeypatch):
    payload = {
        "project_root": str(tmp_path),
        "create": True,
        "type": "discovery",
        "body": "artifact reference claim",
        "ctx": "transport/artifact-reference",
        "author": "example-writer",
        "primitive": "artifact-reference",
        "ref": "lib/aethermind/core.py",
        "kind": "file",
        "label": "core writer",
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))

    result = main(["write-layer", "--payload", "-"])
    written = _json_output(capsys)

    assert result == 0
    assert written["layer"]["primitive"] == "artifact-reference"
    assert written["layer"]["ref"] == "lib/aethermind/core.py"
    assert written["layer"]["kind"] == "file"

    result = main(["read-layers", "--project-root", str(tmp_path), "--ctx", "transport/artifact-reference"])
    readback = _json_output(capsys)

    assert result == 0
    assert readback["layers"][0]["ref"] == "lib/aethermind/core.py"


def test_cli_write_layer_preserves_anchor_payload(tmp_path, capsys, monkeypatch):
    payload = {
        "project_root": str(tmp_path),
        "create": True,
        "type": "discovery",
        "body": "anchor claim",
        "ctx": "transport/anchor",
        "author": "example-writer",
        "primitive": "anchor",
        "artifact": "lib/aethermind/core.py",
        "anchor": "aem-4f2c91ab",
        "selector": "AetherLayer",
        "span_hint": "dataclass fields",
        "inline": True,
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))

    result = main(["write-layer", "--payload", "-"])
    written = _json_output(capsys)

    assert result == 0
    assert written["layer"]["primitive"] == "anchor"
    assert written["layer"]["artifact"] == "lib/aethermind/core.py"
    assert written["layer"]["anchor"] == "aem-4f2c91ab"
    assert written["layer"]["inline"] is True

    result = main(["read-layers", "--project-root", str(tmp_path), "--ctx", "transport/anchor"])
    readback = _json_output(capsys)

    assert result == 0
    assert readback["layers"][0]["selector"] == "AetherLayer"
    assert readback["layers"][0]["span_hint"] == "dataclass fields"


def test_cli_write_layer_preserves_pressure_event_payload(tmp_path, capsys, monkeypatch):
    payload = {
        "project_root": str(tmp_path),
        "create": True,
        "type": "friction",
        "body": "pressure claim",
        "ctx": "transport/pressure-event",
        "author": "example-writer",
        "primitive": "pressure-event",
        "domain": "operations/profile-auth",
        "symptom": "profile fell back to wrong provider",
        "evidence": ["reports/auth.md", "auth list"],
        "next_verification": "run primary-provider smoke",
        "suspected_mechanism": "profile-local auth package incomplete",
        "scope": "profile",
        "severity": "high",
        "recurrence_of": ["pressure-0001"],
        "owner_hint": "profile config",
        "repair": "repaired auth package",
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))

    result = main(["write-layer", "--payload", "-"])
    written = _json_output(capsys)

    assert result == 0
    assert written["layer"]["primitive"] == "pressure-event"
    assert written["layer"]["domain"] == "operations/profile-auth"
    assert written["layer"]["evidence"] == ["reports/auth.md", "auth list"]
    assert written["layer"]["next_verification"] == "run primary-provider smoke"
    assert written["layer"]["recurrence_of"] == ["pressure-0001"]

    result = main(["read-layers", "--project-root", str(tmp_path), "--ctx", "transport/pressure-event"])
    readback = _json_output(capsys)

    assert result == 0
    assert readback["layers"][0]["symptom"] == "profile fell back to wrong provider"
    assert readback["layers"][0]["severity"] == "high"
    assert readback["layers"][0]["repair"] == "repaired auth package"


def test_cli_write_layer_preserves_supersession_primitive_payload(tmp_path, capsys, monkeypatch):
    payload = {
        "project_root": str(tmp_path),
        "create": True,
        "type": "correction",
        "body": "standalone supersession claim",
        "ctx": "transport/supersession-primitive",
        "author": "example-writer",
        "primitive": "supersession",
        "supersedes": ["0001"],
        "reason": "newer claim narrows the old one",
        "scope": "claim",
        "replacement": "0002",
        "artifact": "lib/aethermind/core.py",
        "anchor": "aem-4f2c91ab",
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))

    result = main(["write-layer", "--payload", "-"])
    written = _json_output(capsys)

    assert result == 0
    assert written["layer"]["primitive"] == "supersession"
    assert written["layer"]["supersedes"] == ["0001"]
    assert written["layer"]["reason"] == "newer claim narrows the old one"
    assert written["layer"]["scope"] == "claim"

    result = main(["read-layers", "--project-root", str(tmp_path), "--ctx", "transport/supersession-primitive"])
    readback = _json_output(capsys)

    assert result == 0
    assert readback["layers"][0]["replacement"] == "0002"
    assert readback["layers"][0]["artifact"] == "lib/aethermind/core.py"


def test_cli_write_layer_preserves_rollback_primitive_payload(tmp_path, capsys, monkeypatch):
    payload = {
        "project_root": str(tmp_path),
        "create": True,
        "type": "correction",
        "body": "standalone rollback claim",
        "ctx": "transport/rollback-primitive",
        "author": "example-writer",
        "primitive": "rollback",
        "rollback_of": ["0001", "commit:abc123"],
        "reason": "the replacement introduced a regression",
        "artifact": "lib/aethermind/core.py",
        "anchor": "aem-4f2c91ab",
        "restored_to": "commit:def456",
        "verification": ["pytest lib/tests/test_core_append_contract.py -q"],
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))

    result = main(["write-layer", "--payload", "-"])
    written = _json_output(capsys)

    assert result == 0
    assert written["layer"]["primitive"] == "rollback"
    assert written["layer"]["rollback_of"] == ["0001", "commit:abc123"]
    assert written["layer"]["reason"] == "the replacement introduced a regression"
    assert written["layer"]["verification"] == ["pytest lib/tests/test_core_append_contract.py -q"]

    result = main(["read-layers", "--project-root", str(tmp_path), "--ctx", "transport/rollback-primitive"])
    readback = _json_output(capsys)

    assert result == 0
    assert readback["layers"][0]["restored_to"] == "commit:def456"
    assert readback["layers"][0]["anchor"] == "aem-4f2c91ab"


def test_cli_rejects_missing_required_layer_fields(tmp_path, capsys):
    result = main(["write-layer", "--project-root", str(tmp_path), "--type", "discovery"])
    output = _json_output(capsys)

    assert result == 1
    assert output["success"] is False
    assert output["error"] == "body is required"


def test_cli_status_reports_counts(tmp_path, capsys):
    main([
        "write-layer",
        "--project-root",
        str(tmp_path),
        "--type",
        "correction",
        "--body",
        "status count proof",
        "--ctx",
        "transport/status",
        "--author",
        "test-agent",
        "--create",
    ])
    capsys.readouterr()

    result = main(["status", "--project-root", str(tmp_path)])
    status = _json_output(capsys)

    assert result == 0
    assert status["layer_count"] == 1
    assert status["last_author"] == "test-agent"
    assert status["type_breakdown"] == {"correction": 1}
