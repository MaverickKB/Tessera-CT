"""Command line adapter for host-local AetherMind stores.

The CLI is the portable transport target: run it on the machine that owns the
project filesystem, whether directly or through any remote shell on that machine.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Iterable, Optional

from .core import AetherMind, error_payload, initialize_store, runtime_capabilities


def _layer_to_dict(layer: Any) -> dict[str, Any]:
    return {
        "id": getattr(layer, "id", None),
        "timestamp": getattr(layer, "ts", None),
        "ts": getattr(layer, "ts", None),
        "author": getattr(layer, "author", None),
        "type": getattr(layer, "type", None),
        "body": getattr(layer, "body", None),
        "ctx": getattr(layer, "ctx", None),
        "conf": getattr(layer, "conf", None),
        "markers": list(getattr(layer, "markers", []) or []),
        "primitive": getattr(layer, "primitive", "layer"),
        "thread_key": getattr(layer, "thread_key", None),
        "supersedes": list(getattr(layer, "supersedes", []) or []),
        "rollback_of": getattr(layer, "rollback_of", []),
        "corrects": list(getattr(layer, "corrects", []) or []),
        "prev_hash": getattr(layer, "prev_hash", None),
        "receipt_hash": getattr(layer, "receipt_hash", None),
        "evidence": getattr(layer, "evidence", []),
        "recurrence_of": getattr(layer, "recurrence_of", []),
        "verification": getattr(layer, "verification", []),
        "artifact": getattr(layer, "artifact", None),
        "artifact_ref": getattr(layer, "artifact_ref", None),
        "anchor": getattr(layer, "anchor", None),
        "ref": getattr(layer, "ref", None),
        "kind": getattr(layer, "kind", None),
        "label": getattr(layer, "label", None),
        "host": getattr(layer, "host", None),
        "repo_root": getattr(layer, "repo_root", None),
        "content_id": getattr(layer, "content_id", None),
        "selector": getattr(layer, "selector", None),
        "span_hint": getattr(layer, "span_hint", None),
        "domain": getattr(layer, "domain", None),
        "symptom": getattr(layer, "symptom", None),
        "next_verification": getattr(layer, "next_verification", None),
        "suspected_mechanism": getattr(layer, "suspected_mechanism", None),
        "scope": getattr(layer, "scope", None),
        "severity": getattr(layer, "severity", None),
        "owner_hint": getattr(layer, "owner_hint", None),
        "repair": getattr(layer, "repair", None),
        "reason": getattr(layer, "reason", None),
        "replacement": getattr(layer, "replacement", None),
        "restored_to": getattr(layer, "restored_to", None),
        "inline": getattr(layer, "inline", None),
    }


def _read_payload(value: Optional[str]) -> dict[str, Any]:
    if not value:
        return {}
    if value == "-":
        return json.load(sys.stdin)
    return json.loads(value)


def _markers(values: Optional[Iterable[str]], payload: dict[str, Any]) -> list[str]:
    if "markers" in payload:
        raw = payload["markers"]
        if isinstance(raw, str):
            return [part.strip() for part in raw.split(",") if part.strip()]
        if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
            raise ValueError("markers must be an array of strings")
        return raw
    return [item for value in values or [] for item in value.split(",") if item]


def _string_list(values: Optional[Iterable[str]], payload: dict[str, Any], key: str) -> list[str]:
    if key in payload:
        raw = payload[key]
        if isinstance(raw, str):
            return [part.strip() for part in raw.split(",") if part.strip()]
        if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
            raise ValueError(f"{key} must be an array of strings")
        return raw
    return [item for value in values or [] for item in value.split(",") if item]


def _optional_bool(payload: dict[str, Any], args: argparse.Namespace, key: str) -> Optional[bool]:
    if key in payload:
        raw = payload[key]
    else:
        raw = getattr(args, key, None)
    if raw is None:
        return None
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        normalized = raw.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
    raise ValueError(f"{key} must be a bool")


def _project_root(args: argparse.Namespace, payload: dict[str, Any]) -> str:
    root = payload.get("project_root") or args.project_root
    if root:
        return str(root)
    return os.getcwd()


def _required(payload: dict[str, Any], args: argparse.Namespace, key: str, attr: Optional[str] = None) -> str:
    value = payload.get(key)
    if value is None and attr:
        value = getattr(args, attr)
    elif value is None:
        value = getattr(args, key, None)
    if value is None or value == "":
        raise ValueError(f"{key} is required")
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    return value


def _confidence(payload: dict[str, Any], args: argparse.Namespace) -> float:
    value = payload.get("conf", args.conf)
    if isinstance(value, bool) or not isinstance(value, float):
        raise ValueError("conf must be a float")
    return value


def _create_requested(payload: dict[str, Any], args: argparse.Namespace) -> bool:
    value = payload.get("create", getattr(args, "create", False))
    if not isinstance(value, bool):
        raise ValueError("create must be a bool")
    return value


def _print(payload: dict[str, Any]) -> int:
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if payload.get("success", True) else 1


def cmd_write_layer(args: argparse.Namespace) -> int:
    payload = _read_payload(args.payload)
    aether = AetherMind(
        _project_root(args, payload), create=_create_requested(payload, args)
    )
    layer = aether.write_layer(
        type=_required(payload, args, "type", "layer_type"),
        body=_required(payload, args, "body"),
        ctx=_required(payload, args, "ctx"),
        conf=_confidence(payload, args),
        markers=_markers(args.marker, payload),
        author=payload.get("author") or args.author,
        primitive=payload.get("primitive") or args.primitive or "layer",
        thread_key=(
            payload["thread_key"] if "thread_key" in payload else args.thread_key
        ),
        supersedes=_string_list(args.supersedes, payload, "supersedes"),
        rollback_of=_string_list(args.rollback_of, payload, "rollback_of"),
        corrects=_string_list(args.corrects, payload, "corrects"),
        evidence=_string_list(args.evidence, payload, "evidence"),
        recurrence_of=_string_list(args.recurrence_of, payload, "recurrence_of"),
        verification=_string_list(args.verification, payload, "verification"),
        artifact=payload.get("artifact") or args.artifact,
        artifact_ref=payload.get("artifact_ref") or args.artifact_ref,
        anchor=payload.get("anchor") or args.anchor,
        ref=payload.get("ref") or args.ref,
        kind=payload.get("kind") or args.kind,
        label=payload.get("label") or args.label,
        host=payload.get("host") or args.host,
        repo_root=payload.get("repo_root") or args.repo_root,
        content_id=payload.get("content_id") or args.content_id,
        selector=payload.get("selector") or args.selector,
        span_hint=payload.get("span_hint") or args.span_hint,
        domain=payload.get("domain") or args.domain,
        symptom=payload.get("symptom") or args.symptom,
        next_verification=payload.get("next_verification") or args.next_verification,
        suspected_mechanism=payload.get("suspected_mechanism") or args.suspected_mechanism,
        scope=payload.get("scope") or args.scope,
        severity=payload.get("severity") or args.severity,
        owner_hint=payload.get("owner_hint") or args.owner_hint,
        repair=payload.get("repair") or args.repair,
        reason=payload.get("reason") or args.reason,
        replacement=payload.get("replacement") or args.replacement,
        restored_to=payload.get("restored_to") or args.restored_to,
        inline=_optional_bool(payload, args, "inline"),
    )
    layer_dict = _layer_to_dict(layer)
    receipt = getattr(layer, "receipt_hash", None)
    if receipt:
        layer_dict["receipt_hash"] = receipt
    return _print({"success": True, "layer": layer_dict})


def cmd_write_texture(args: argparse.Namespace) -> int:
    payload = _read_payload(args.payload)
    aether = AetherMind(
        _project_root(args, payload), create=_create_requested(payload, args)
    )
    entry = aether.write_texture(
        body=_required(payload, args, "body"),
        ctx=_required(payload, args, "ctx"),
        marker=payload.get("marker") or args.marker,
        author=payload.get("author") or args.author,
    )
    return _print({"success": True, "entry": entry})


def cmd_read_layers(args: argparse.Namespace) -> int:
    aether = AetherMind(args.project_root or os.getcwd(), create=False)
    aether._require_store()
    layers = aether.read_layers(
        ctx=args.ctx,
        type=args.layer_type,
        author=args.author,
        text=args.text,
    )
    payload = {
        "success": True,
        "store": str(aether.project_root),
        "layers": [_layer_to_dict(layer) for layer in layers],
        "count": len(layers),
    }
    return _print(payload)


def cmd_read_texture(args: argparse.Namespace) -> int:
    aether = AetherMind(args.project_root or os.getcwd(), create=False)
    texture = aether.read_texture()
    return _print({"success": True, "texture": texture, "count": len(texture)})


def cmd_status(args: argparse.Namespace) -> int:
    aether = AetherMind(args.project_root or os.getcwd(), create=False)
    return _print({"success": True, **aether.status_summary()})


def cmd_init(args: argparse.Namespace) -> int:
    root = args.project_root or os.getcwd()
    result = initialize_store(root, args.project_key)
    if args.seed:
        from .seed import write_seed
        result["seed"] = write_seed(root)
    return _print({
        "success": True,
        **result,
    })


def cmd_capabilities(args: argparse.Namespace) -> int:
    root = args.project_root or os.getcwd()
    return _print({"success": True, "runtime": runtime_capabilities(root)})


def cmd_brief(args: argparse.Namespace) -> int:
    aether = AetherMind(args.project_root or os.getcwd(), create=False)
    result = aether.brief(as_of=args.as_of)
    return _print({
        "success": True,
        **result,
        "digests": [_layer_to_dict(layer) for layer in result["digests"]],
        "deltas": [_layer_to_dict(layer) for layer in result["deltas"]],
    })


def cmd_brief_anchor(args: argparse.Namespace) -> int:
    aether = AetherMind(args.project_root or os.getcwd(), create=False)
    brief = aether.brief_anchor(
        anchor=args.anchor, budget=args.budget, as_of=args.as_of
    )
    return _print({
        "success": True,
        "anchor": brief["anchor"],
        "must_know": brief["must_know"],
        "unabsorbed_debt": brief["unabsorbed_debt"],
        "kept": [_layer_to_dict(layer) for layer in brief["kept"]],
        "tombstones": brief["tombstones"],
        "budget": brief["budget"],
        "budget_used": brief["budget_used"],
        "layer_count": brief["layer_count"],
        "determinism_key": brief["determinism_key"],
    })


def cmd_audit(args: argparse.Namespace) -> int:
    aether = AetherMind(args.project_root or os.getcwd(), create=args.create)
    return _print({"success": True, **aether.audit()})


def cmd_write_event(args: argparse.Namespace) -> int:
    payload = _read_payload(args.payload)
    aether = AetherMind(args.project_root or os.getcwd(), create=False)
    return _print({
        "success": True,
        **aether.write_event(
            type=_required(payload, args, "type", "type"),
            body=_required(payload, args, "body"),
            ctx=_required(payload, args, "ctx"),
            author=payload.get("author") or args.author,
        ),
    })


def cmd_read_events(args: argparse.Namespace) -> int:
    aether = AetherMind(args.project_root or os.getcwd(), create=False)
    return _print({"success": True, **aether.read_events()})


def cmd_archive(args: argparse.Namespace) -> int:
    payload = _read_payload(args.payload)
    layer_ids = (
        args.layer_ids
        if args.layer_ids
        else [str(v) for v in (payload.get("layer_ids") or [])]
    )
    if not layer_ids:
        raise ValueError("archive requires at least one layer id (--layer-id or payload.layer_ids)")
    aether = AetherMind(args.project_root or os.getcwd(), create=False)
    return _print({
        "success": True,
        **aether.archive(layer_ids, reason=payload.get("reason") or args.reason or ""),
    })


def cmd_gate_check(args: argparse.Namespace) -> int:
    aether = AetherMind(args.project_root or os.getcwd(), create=False)
    plan = {
        "anchor": args.anchor,
        "planned_type": args.planned_type,
        "planned_ctx": args.planned_ctx,
        "enforce_gate": args.enforce_gate,
    }
    return _print({"success": True, **aether.gate_check(plan, anchor=args.anchor)})


def cmd_currentness(args: argparse.Namespace) -> int:
    aether = AetherMind(args.project_root or os.getcwd(), create=False)
    result = aether.currentness(as_of=args.as_of)
    return _print({
        "success": True,
        **result,
        "history": [_layer_to_dict(layer) for layer in result["history"]],
        "active_heads": [
            _layer_to_dict(layer) for layer in result["active_heads"]
        ],
        "unresolved_currentness": [
            _layer_to_dict(layer)
            for layer in result["unresolved_currentness"]
        ],
    })


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read and write host-local AetherMind stores.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    write_layer = subparsers.add_parser("write-layer", help="Append one layer.")
    write_layer.add_argument("--payload", help="JSON payload or '-' for stdin.")
    write_layer.add_argument("--project-root")
    write_layer.add_argument("--type", dest="layer_type")
    write_layer.add_argument("--body")
    write_layer.add_argument("--ctx")
    write_layer.add_argument("--conf", type=float, default=1.0)
    write_layer.add_argument("--marker", action="append")
    write_layer.add_argument("--author")
    write_layer.add_argument("--create", action="store_true")
    write_layer.add_argument("--primitive")
    write_layer.add_argument("--thread-key")
    write_layer.add_argument("--supersedes", action="append")
    write_layer.add_argument("--rollback-of", dest="rollback_of", action="append")
    write_layer.add_argument("--corrects", action="append")
    write_layer.add_argument("--evidence", action="append")
    write_layer.add_argument("--recurrence-of", dest="recurrence_of", action="append")
    write_layer.add_argument("--verification", action="append")
    write_layer.add_argument("--artifact")
    write_layer.add_argument("--artifact-ref")
    write_layer.add_argument("--anchor")
    write_layer.add_argument("--ref")
    write_layer.add_argument("--kind")
    write_layer.add_argument("--label")
    write_layer.add_argument("--host")
    write_layer.add_argument("--repo-root")
    write_layer.add_argument("--content-id")
    write_layer.add_argument("--selector")
    write_layer.add_argument("--span-hint")
    write_layer.add_argument("--domain")
    write_layer.add_argument("--symptom")
    write_layer.add_argument("--next-verification")
    write_layer.add_argument("--suspected-mechanism")
    write_layer.add_argument("--scope")
    write_layer.add_argument("--severity")
    write_layer.add_argument("--owner-hint")
    write_layer.add_argument("--repair")
    write_layer.add_argument("--reason")
    write_layer.add_argument("--replacement")
    write_layer.add_argument("--restored-to")
    write_layer.add_argument("--inline", action="store_const", const=True, default=None)
    write_layer.set_defaults(func=cmd_write_layer)

    write_texture = subparsers.add_parser("write-texture", help="Append one texture entry.")
    write_texture.add_argument("--payload", help="JSON payload or '-' for stdin.")
    write_texture.add_argument("--project-root")
    write_texture.add_argument("--body")
    write_texture.add_argument("--ctx")
    write_texture.add_argument("--marker")
    write_texture.add_argument("--author")
    write_texture.add_argument("--create", action="store_true")
    write_texture.set_defaults(func=cmd_write_texture)

    read_layers = subparsers.add_parser("read-layers", help="Read layers.")
    read_layers.add_argument("--project-root")
    read_layers.add_argument("--ctx")
    read_layers.add_argument("--type", dest="layer_type")
    read_layers.add_argument("--author")
    read_layers.add_argument("--text", help="Case-insensitive substring of body, context, or marker.")
    read_layers.set_defaults(func=cmd_read_layers)

    read_texture = subparsers.add_parser("read-texture", help="Read texture.")
    read_texture.add_argument("--project-root")
    read_texture.set_defaults(func=cmd_read_texture)

    status = subparsers.add_parser("status", help="Return store status.")
    status.add_argument("--project-root")
    status.set_defaults(func=cmd_status)

    init = subparsers.add_parser("init", help="Explicitly initialize a store.")
    init.add_argument("--project-root")
    init.add_argument("--project-key")
    init.add_argument(
        "--seed",
        action="store_true",
        help="Append the generic explanation layers. Refuses a store that already has layers.",
    )
    init.set_defaults(func=cmd_init)

    capabilities = subparsers.add_parser(
        "capabilities", help="Return runtime identity and capabilities."
    )
    capabilities.add_argument("--project-root")
    capabilities.set_defaults(func=cmd_capabilities)

    brief = subparsers.add_parser("brief", help="Return active scoped briefing state.")
    brief.add_argument("--project-root")
    brief.add_argument("--as-of")
    brief.set_defaults(func=cmd_brief)

    brief_anchor = subparsers.add_parser(
        "brief-anchor",
        help="Return the anchor-scoped deterministic brief.",
    )
    brief_anchor.add_argument("--project-root")
    brief_anchor.add_argument("--anchor")
    brief_anchor.add_argument("--budget", type=int, default=4000)
    brief_anchor.add_argument("--as-of")
    brief_anchor.set_defaults(func=cmd_brief_anchor)

    audit = subparsers.add_parser(
        "audit", help="Scan the ledger hash chain and report integrity."
    )
    audit.add_argument("--project-root")
    audit.add_argument("--create", action="store_true")
    audit.set_defaults(func=cmd_audit)

    gate_check = subparsers.add_parser(
        "gate-check", help="Check a proposed mutation against must-know corrections."
    )
    gate_check.add_argument("--project-root")
    gate_check.add_argument("--anchor")
    gate_check.add_argument("--planned-type", default="layer")
    gate_check.add_argument("--planned-ctx")
    gate_check.add_argument("--enforce-gate", action="store_true")
    gate_check.set_defaults(func=cmd_gate_check)

    write_event = subparsers.add_parser(
        "write-event", help="Append a cheap routine event to events.aem."
    )
    write_event.add_argument("--project-root")
    write_event.add_argument("--type")
    write_event.add_argument("--body", required=True)
    write_event.add_argument("--ctx", required=True)
    write_event.add_argument("--author")
    write_event.add_argument("--payload")
    write_event.set_defaults(func=cmd_write_event)

    read_events = subparsers.add_parser(
        "read-events", help="Read events from events.aem (split ledger)."
    )
    read_events.add_argument("--project-root")
    read_events.set_defaults(func=cmd_read_events)

    archive = subparsers.add_parser(
        "archive", help="Archive layers to archive.aem (tombstone + continuation)."
    )
    archive.add_argument("--project-root")
    archive.add_argument("--layer-id", dest="layer_ids", action="append")
    archive.add_argument("--reason")
    archive.add_argument("--payload")
    archive.set_defaults(func=cmd_archive)

    currentness = subparsers.add_parser(
        "currentness", help="Return active heads and inactive lineage."
    )
    currentness.add_argument("--project-root")
    currentness.add_argument("--as-of")
    currentness.set_defaults(func=cmd_currentness)

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except Exception as exc:
        return _print(error_payload(exc))


if __name__ == "__main__":
    raise SystemExit(main())
