"""Lossless transport of AetherMind's native inheritance contract.

This is an adapter, not another selector, writer, or definition of currentness.
Receipts describe externally supplied evidence; they cannot discharge native
correction debt or establish a model's comprehension.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import re
from pathlib import Path
from uuid import UUID

from . import core

SCHEMA = "aethermind-inheritance/v1"
RECEIPT_SCHEMA = "aethermind-use-receipt/v1"
DISCOVERY_SCHEMA = "aethermind-inheritance-discovery/v1"


def project_identity_path(project_root):
    """Optional project identity file. Absent is a valid store."""
    return Path(project_root) / ".aethermind" / "project.identity.json"


def _json(value):
    if dataclasses.is_dataclass(value):
        return {key: _json(item) for key, item in dataclasses.asdict(value).items()}
    if isinstance(value, dict):
        return {key: _json(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json(item) for item in value]
    return value


def _encode(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _sha(content):
    return hashlib.sha256(content).hexdigest()


def _read(path):
    """Read one append-only file under the same lock used by native writers."""
    try:
        with path.open("rb") as stream:
            if core.fcntl is not None:
                core.fcntl.flock(stream.fileno(), core.fcntl.LOCK_SH)
            try:
                return stream.read()
            finally:
                if core.fcntl is not None:
                    core.fcntl.flock(stream.fileno(), core.fcntl.LOCK_UN)
    except FileNotFoundError:
        return None


class _SnapshotMind(core.AetherMind):
    """Run the unchanged core selector against the exact bytes fingerprinted."""

    def __init__(self, layers):
        self._layers = layers

    def currentness(self, as_of=None):
        return core.derive_currentness(self._layers, as_of=as_of)


def _identity(path, content):
    result = {"path": str(path), "sha256": _sha(content) if content is not None else None,
              "status": "absent", "project_id": None}
    if content is None:
        return result
    try:
        native = json.loads(content)
        project_id = str(UUID(native["project_id"]))
        if str(native.get("schema_version")) != "1":
            raise ValueError("unsupported project identity schema")
    except (ValueError, TypeError, KeyError, UnicodeError) as exc:
        result.update(status="invalid", issue=str(exc))
        return result
    result.update(status="local_identity_validated", project_id=project_id,
                  native=native, authority_verified=False)
    return result


def _texture(path, content, anchor):
    """Keep whole canonical texture entries separate from decision authority.

    Recognize both native dated entries and the legacy Context/Marker form.
    Unsegmented prose remains available by exact source reference. Do not
    convert texture into fact/decision records or manufacture missing fields.
    """
    content = content or b""
    text = content.decode("utf-8")
    records = []
    pattern = re.compile(r"(?m)^\d{4}-\d{2}-\d{2} \[[^\r\n|]+ \| ([^\r\n]+)\]\r?\n.*?^---[ \t]*(?:\r?\n|$)", re.S)
    for match in pattern.finditer(text):
        records.append({"start": match.start(), "end": match.end(), "ctx": match.group(1),
                        "text": match.group(0), "form": "dated"})
    legacy = re.compile(r"(?m)^Context: ([^\r\n]+)\r?\nMarker: [^\r\n]*(?:\r?\n|$)")
    for match in legacy.finditer(text):
        if any(item["start"] <= match.start() < item["end"] for item in records):
            continue
        # Prior complete entry boundaries preserve multi-paragraph legacy
        # bodies. Do not split their content at arbitrary blank lines.
        start = max((item["end"] for item in records if item["end"] <= match.start()), default=0)
        records.append({"start": start, "end": match.end(), "ctx": match.group(1),
                        "text": text[start:match.end()], "form": "legacy-context-marker"})
    records.sort(key=lambda item: item["start"])
    entries = [item for item in records if anchor is None or item["ctx"] == anchor or item["ctx"].startswith(anchor + "/")]
    # One complete, native attention pointer. Oversize entries are referenced,
    # never cut in half. This independent limit does not thin native layers.
    selected = entries[-1:] if entries and len(entries[-1]["text"]) <= 1200 else []
    return {"path": str(path), "sha256": _sha(content), "bytes": len(content),
            "relation": "felt-sense and attention; not factual or decision authority",
            "selection": "latest complete dated or legacy Context/Marker entry in requested context",
            "entries": [item["text"] for item in selected],
            "entry_forms": [item["form"] for item in selected], "matching_entries": len(entries),
            "omitted_content": bool(text and "".join(item["text"] for item in selected) != text),
            "limit_chars": 1200,
            "status": "selected" if selected else ("reference_only" if text else "empty")}


def prepare_inheritance(project_root, anchor=None, budget=3000, surface=None, session_id=None):
    """Return a JSON-safe envelope without writing or initializing a store.

    ``budget`` retains core's character units and unconditional overflow rule.
    The revision includes native bytes (not merely record IDs), core/adapter
    source, identity, texture, root and selection scope. Files are individual
    snapshots; no cross-file atomicity or remote authority is asserted.
    """
    aether = core.AetherMind(project_root, create=False)
    root = aether.project_root
    layer_bytes = _read(aether.layers_path)
    texture_bytes = _read(aether.texture_path)
    identity_path = project_identity_path(root)
    identity_bytes = _read(identity_path)
    layers = aether.layer_store._parse_bytes(layer_bytes or b"") if aether.layer_store else []
    snapshot = _SnapshotMind(layers)
    native = _json(snapshot.brief_anchor(anchor=anchor, budget=budget))
    projection = snapshot.currentness()
    identity = _identity(identity_path, identity_bytes)
    provenance = {
        "project_root": str(root), "layers_path": str(aether.layers_path),
        "archive_path": str(aether.archive_path),
        "store_exists": aether.aethermind_dir.is_dir(),
        "layers_sha256": _sha(layer_bytes) if layer_bytes is not None else None,
        "texture_sha256": _sha(texture_bytes) if texture_bytes is not None else None,
        "identity_sha256": identity["sha256"],
        "core_version": core.RUNTIME_VERSION, "format_version": core.FORMAT_VERSION,
        "core_sha256": _sha(Path(core.__file__).read_bytes()),
        "adapter_sha256": _sha(Path(__file__).read_bytes()),
        "schema": SCHEMA, "anchor": anchor, "budget_chars": native["budget"],
        "texture_selection_version": 1,
        "snapshot": "per-file read; no cross-file atomicity or remote freshness claim",
        "history_count": len(projection["history"]),
    }
    revision = _sha(_encode(provenance).encode("utf-8"))
    return {
        "schema": SCHEMA, "revision": revision,
        "recipient": {"surface": surface, "session_id": session_id},
        "identity": identity, "provenance": provenance, "native": native,
        "currentness": {
            "scope": "complete native layer history; selection is scoped separately by anchor",
            "active_ids": [layer.id for layer in projection["active_heads"]],
            "inactive": projection["inactive"],
            "unresolved_currentness_ids": [layer.id for layer in projection["unresolved_currentness"]],
            "as_of": projection["as_of"],
        },
        "texture": _texture(aether.texture_path, texture_bytes, anchor),
    }


def prepare_discovery(project_root, surface=None, session_id=None):
    """Expose scope references without disguising a directory as inheritance.

    An unscoped consumer can discover available contexts and then explicitly
    request a native envelope. There are no selected record bodies here.
    """
    envelope = prepare_inheritance(project_root, budget=0, surface=surface, session_id=session_id)
    native = envelope["native"]
    records = {item["id"]: item for item in native["kept"] + native["tombstones"]}
    contexts = {}
    for item in records.values():
        contexts[item["ctx"]] = contexts.get(item["ctx"], 0) + 1
    return {
        "schema": DISCOVERY_SCHEMA,
        "revision": _sha(_encode({"schema": DISCOVERY_SCHEMA, "native_revision": envelope["revision"]}).encode("utf-8")),
        "native_revision": envelope["revision"],
        "recipient": envelope["recipient"], "identity": envelope["identity"],
        "provenance": envelope["provenance"],
        "status": "selection_required; full native inheritance not delivered",
        "contexts": [{"anchor": anchor, "record_count": count} for anchor, count in sorted(contexts.items())],
        "must_know_refs": [{"id": item, "ctx": records[item]["ctx"]} for item in native["must_know"]],
        "record_count": native["layer_count"],
    }


def render_discovery(manifest, max_contexts=12):
    """Render a bounded directory preview, never selected or clipped layers."""
    if manifest.get("schema") != DISCOVERY_SCHEMA:
        raise ValueError("unsupported discovery schema")
    contexts = manifest["contexts"]
    visible = contexts[:max(0, int(max_contexts))]
    lines = [
        f"AetherMind scope discovery ({DISCOVERY_SCHEMA}) revision={manifest['revision']}",
        "Selection required: full native inheritance has not been delivered. This is only a context directory.",
        f"Artifact root: {manifest['provenance']['project_root']}",
        f"{manifest['record_count']} native records; {len(manifest['must_know_refs'])} must-know correction references.",
        f"Available contexts (alphabetical preview {len(visible)}/{len(contexts)}):",
    ]
    lines.extend(f"- {item['anchor']} ({item['record_count']} records)" for item in visible)
    lines.append("Choose the task's explicit context/anchor and call prepare_inheritance(root, anchor=...). Use anchor=None only when full native inheritance is intended.")
    lines.append("Read the complete directory with prepare_discovery(root); layers: " + manifest["provenance"]["layers_path"])
    return "\n".join(lines)


def render_inheritance(envelope):
    """Render whole native records with explicit history and omission signals."""
    if envelope.get("schema") != SCHEMA:
        raise ValueError("unsupported inheritance schema")
    native = envelope["native"]
    lines = [
        f"AetherMind inheritance ({SCHEMA}) revision={envelope['revision']}",
        f"Artifact root: {envelope['provenance']['project_root']}; anchor={native['anchor']!r}",
        f"Project UUID: {envelope['identity']['project_id']}; identity={envelope['identity']['status']} (remote authority not established by this read).",
        "Inherit the work's decisions, pressure, uncertainty, corrections and texture with their relations intact.",
        "A corrects edge does not retire its target. Historical records retain rationale but are not active posture.",
        "Receiving or acknowledging this packet does not absorb correction debt; use evidence remains separately assessable.",
        f"Native budget: {native['budget_used']}/{native['budget']} characters (whole load-bearing records may overflow).",
    ]
    for layer in native["kept"]:
        inactive = envelope["currentness"]["inactive"].get(layer["id"])
        state = "historical/inactive " + _encode(inactive) if inactive else "active"
        # Omit unauthored null/empty optional fields only in the prose view;
        # the machine envelope above retains the exact complete core result.
        authored = {key: value for key, value in layer.items() if value is not None and value != []}
        lines.append(f"\n[{layer['id']} {state}]\n" + _encode(authored))
    for key in ("must_know", "unabsorbed_debt", "tombstones"):
        lines.append(f"\n{key}: " + _encode(native[key]))
    in_scope = {item["id"] for item in native["kept"] + native["tombstones"]}
    unresolved = envelope["currentness"]["unresolved_currentness_ids"]
    lines.append("unresolved_currentness_ids (requested scope): " + _encode([item for item in unresolved if item in in_scope]))
    outside_count = sum(item not in in_scope for item in unresolved)
    if outside_count:
        lines.append(f"Outside this scope: {outside_count} unresolved currentness references; full projection is in the machine envelope.")
    texture = envelope["texture"]
    lines.append(f"\nTexture ({texture['relation']}):")
    lines.extend(texture["entries"])
    lines.append(f"Texture source: {texture['path']} sha256={texture['sha256']}; status={texture['status']}; omitted_content={texture['omitted_content']}")
    lines.append("Native layer source: " + envelope["provenance"]["layers_path"])
    lines.append("Archive source (native archive references remain in the records): " + envelope["provenance"]["archive_path"])
    return "\n".join(lines)


def verify_use(envelope, receipt):
    """Validate an evidence claim's bindings, not its truth or comprehension.

    A delivery claim needs a harness transport reference; recognition needs a
    model response reference; applied use needs action and observed outcome
    references. References are deliberately not fetched or trusted here.
    Persist receipts only as transient harness evidence, never as layers.
    """
    errors = []
    if not isinstance(envelope, dict) or not isinstance(receipt, dict):
        return {"valid": False, "kind": None, "errors": ["envelope and receipt must be objects"],
                "evidence_verified": False, "comprehension_proven": False, "debt_changed": False}
    if envelope.get("schema") != SCHEMA:
        errors.append("unsupported inheritance schema")
    if receipt.get("schema") != RECEIPT_SCHEMA:
        errors.append("unsupported receipt schema")
    if receipt.get("revision") != envelope.get("revision"):
        errors.append("receipt revision does not match delivered inheritance")
    for field in ("surface", "session_id"):
        if not receipt.get(field) or receipt.get(field) != envelope.get("recipient", {}).get(field):
            errors.append(f"receipt {field} does not match recipient")
    kind = receipt.get("kind")
    required = {"delivered": ("transport_ref",), "recognized": ("response_ref",),
                "applied": ("action_ref", "outcome_ref")}
    if not isinstance(kind, str) or kind not in required:
        errors.append("kind must be delivered, recognized, or applied")
    else:
        evidence = receipt.get("evidence", {})
        if not isinstance(evidence, dict):
            evidence = {}
        for field in required[kind]:
            if not isinstance(evidence.get(field), str) or not evidence[field].strip():
                errors.append(f"{kind} requires {field}")
    ids = receipt.get("record_ids", [])
    known = {layer["id"] for layer in envelope.get("native", {}).get("kept", [])}
    if not isinstance(ids, list) or any(not isinstance(item, str) or item not in known for item in ids):
        errors.append("record_ids must reference whole records present in this packet")
    if kind in ("recognized", "applied") and not ids:
        errors.append(f"{kind} requires record_ids")
    return {"valid": not errors, "kind": kind, "errors": errors,
            "evidence_verified": False, "comprehension_proven": False,
            "debt_changed": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--anchor")
    parser.add_argument("--budget", type=int, default=3000)
    parser.add_argument("--surface")
    parser.add_argument("--session-id")
    parser.add_argument("--render", action="store_true")
    parser.add_argument("--discover", action="store_true", help="context directory only; no record bodies delivered")
    args = parser.parse_args(argv)
    if args.discover:
        if args.anchor is not None:
            parser.error("--discover is unscoped; use --anchor without --discover for inheritance")
        manifest = prepare_discovery(args.project_root, args.surface, args.session_id)
        print(render_discovery(manifest) if args.render else _encode(manifest))
        return
    envelope = prepare_inheritance(args.project_root, args.anchor, args.budget,
                                   args.surface, args.session_id)
    print(render_inheritance(envelope) if args.render else _encode(envelope))


if __name__ == "__main__":
    main()
