#!/usr/bin/env python3
"""
AetherMind MCP Server

Exposes AetherMind read/write capabilities as MCP tools.

Implements a FastMCP HTTP server (streamable-http) listening on port 8452.
"""

import sys
import json
from typing import List, Optional
from pathlib import Path

# Add lib directory to path for aethermind package
lib_path = Path(__file__).parent.parent / "lib"
sys.path.insert(0, str(lib_path))
mcp_path = str(Path(__file__).parent)
while mcp_path in sys.path:
    sys.path.remove(mcp_path)

from aethermind.core import (
    AetherMind,
    error_payload as _error_payload,
    initialize_store as _initialize_store,
    register_project_key as _register_project_key,
    runtime_capabilities as _runtime_capabilities,
)
from aethermind.inheritance import (
    prepare_inheritance as _prepare_inheritance,
    prepare_discovery as _prepare_discovery,
    verify_use as _verify_use,
)

# FastMCP server setup
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import StrictFloat

mcp = FastMCP("aethermind")
_READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                             idempotentHint=True, openWorldHint=False)


def _error_json(exc: Exception) -> str:
    return json.dumps(_error_payload(exc))


def _posture_of(layer, inactive: dict) -> str:
    """Live unless currentness marks the id inactive.

    Archive leaves the record in layers.aem and appends a tombstone whose
    supersedes list names it. derive_currentness records that as
    inactive[id].reason == "superseded". There is no stored posture field
    and archived_to is not written on the archived record (append-only).
    Thread-replaced and rolled-back share that inactive set. An id absent
    from inactive is live.
    """
    layer_id = getattr(layer, "id", None)
    if layer_id and layer_id in inactive:
        return "archived"
    return "live"


def _layer_to_dict(layer, inactive: dict | None = None) -> dict:
    return {
        "id": getattr(layer, "id", None),
        "posture": _posture_of(layer, inactive or {}),
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

# -----------------------------------------------------------------------------
# MCP tool definitions – these are the names the task description expects.
# -----------------------------------------------------------------------------

@mcp.tool(annotations=_READ_ONLY)
def prepare_inheritance(
    project_root: str,
    anchor: Optional[str] = None,
    budget: int = 3000,
    surface: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """Read a native AetherMind inheritance envelope on this filesystem host.

    Retains whole records, currentness, correction debt, omissions and separate
    authored texture. Budget is native characters; load-bearing records may
    overflow. With no anchor this explicitly requests full native inheritance;
    use discover_inheritance first when task scope is unknown. No store is
    initialized or written. Preparing a packet grants no action authority and
    does not prove delivery, recognition, application or comprehension.
    """
    try:
        result = _prepare_inheritance(project_root, anchor, budget, surface, session_id)
        return json.dumps({"success": True, "envelope": result})
    except Exception as e:
        return _error_json(e)


@mcp.tool(annotations=_READ_ONLY)
def discover_inheritance(
    project_root: str,
    surface: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """Read available AetherMind context/anchor and must-know references.

    This directory contains no layer bodies: full native inheritance has not
    been delivered. Follow with prepare_inheritance for the required scope.
    Does not initialize a store, alter history, or grant action authority.
    """
    try:
        result = _prepare_discovery(project_root, surface, session_id)
        return json.dumps({"success": True, "manifest": result})
    except Exception as e:
        return _error_json(e)


@mcp.tool(annotations=_READ_ONLY)
def verify_inheritance_use(envelope: dict, receipt: dict) -> str:
    """Validate a use-evidence claim's shape and packet/session binding only.

    Distinguishes delivered, recognized and applied claims. Evidence references
    are not fetched or verified; a valid claim is not proof of comprehension,
    permission, or successful action. No receipt is persisted, no debt is
    cleared and no layer/currentness/authority state is changed.
    """
    try:
        return json.dumps({"success": True, **_verify_use(envelope, receipt),
                           "authority_granted": False})
    except Exception as e:
        return _error_json(e)


@mcp.tool()
def write_layer(
    project_root: str,
    layer_type: str,
    body: str,
    ctx: str,
    conf: StrictFloat,
    markers: List[str],
    author: Optional[str] = None,
    primitive: str = "layer",
    thread_key: Optional[str] = None,
    supersedes: Optional[List[str]] = None,
    rollback_of: Optional[List[str]] = None,
    corrects: Optional[List[str]] = None,
    evidence: Optional[List[str]] = None,
    recurrence_of: Optional[List[str]] = None,
    verification: Optional[List[str]] = None,
    artifact: Optional[str] = None,
    artifact_ref: Optional[str] = None,
    anchor: Optional[str] = None,
    ref: Optional[str] = None,
    kind: Optional[str] = None,
    label: Optional[str] = None,
    host: Optional[str] = None,
    repo_root: Optional[str] = None,
    content_id: Optional[str] = None,
    selector: Optional[str] = None,
    span_hint: Optional[str] = None,
    domain: Optional[str] = None,
    symptom: Optional[str] = None,
    next_verification: Optional[str] = None,
    suspected_mechanism: Optional[str] = None,
    scope: Optional[str] = None,
    severity: Optional[str] = None,
    owner_hint: Optional[str] = None,
    repair: Optional[str] = None,
    reason: Optional[str] = None,
    replacement: Optional[str] = None,
    restored_to: Optional[str] = None,
    inline: Optional[bool] = None,
    create: bool = False,
) -> str:
    """Write a layer to the AetherMind project.

    create: if a store does not already exist at project_root, this must
        be True (or project_root must have been registered first via
        register_project_key) to initialize a new one. Defaults to False
        so a mistyped or unintended path fails loudly instead of silently
        spawning an empty store. Existing stores are always writable
        regardless of this flag.

    Returns a JSON string with success flag and metadata.
    """
    try:
        aethermind = AetherMind(project_root, create=create)
        layer = aethermind.write_layer(
            layer_type,
            body,
            ctx,
            conf,
            markers,
            author=author,
            primitive=primitive,
            thread_key=thread_key,
            supersedes=supersedes,
            rollback_of=rollback_of,
            corrects=corrects,
            evidence=evidence,
            recurrence_of=recurrence_of,
            verification=verification,
            artifact=artifact,
            artifact_ref=artifact_ref,
            anchor=anchor,
            ref=ref,
            kind=kind,
            label=label,
            host=host,
            repo_root=repo_root,
            content_id=content_id,
            selector=selector,
            span_hint=span_hint,
            domain=domain,
            symptom=symptom,
            next_verification=next_verification,
            suspected_mechanism=suspected_mechanism,
            scope=scope,
            severity=severity,
            owner_hint=owner_hint,
            repair=repair,
            reason=reason,
            replacement=replacement,
            restored_to=restored_to,
            inline=inline,
        )
        return json.dumps({
            "success": True,
            "layer_id": getattr(layer, "id", None),
            "hash": getattr(layer, "receipt_hash", None),
            "timestamp": getattr(layer, "ts", None),
            "ts": getattr(layer, "ts", None),
            "author": getattr(layer, "author", None),
            "layer": _layer_to_dict(layer),
            "message": "Layer written successfully",
        })
    except Exception as e:
        return _error_json(e)

@mcp.tool()
def write_texture(
    project_root: str,
    body: str,
    ctx: str,
    marker: Optional[str] = None,
    author: Optional[str] = None,
    create: bool = False,
) -> str:
    """Write a texture entry to the AetherMind project.

    create: same guard as write_layer's create param — required to
        initialize a brand-new store, not required for an existing one.

    Returns a JSON string with success flag and entry details.
    """
    try:
        aethermind = AetherMind(project_root, create=create)
        entry = aethermind.write_texture(body, ctx, marker, author=author)
        return json.dumps({
            "success": True,
            "entry": entry,
            "message": "Texture entry written successfully",
        })
    except Exception as e:
        return _error_json(e)

@mcp.tool()
def read_layers(
    project_root: str,
    ctx: Optional[str] = None,
    type: Optional[str] = None,
    author: Optional[str] = None,
    since_ts: Optional[str] = None,
    last_n: Optional[int] = None,
    ctx_prefix: Optional[str] = None,
    markers_any: Optional[List[str]] = None,
    posture: Optional[str] = None,
    text: Optional[str] = None,
) -> str:
    """Return filtered layers as JSON.

    Parameters mirror the core read_layers API. All filters are composable
    (AND together):
    - ctx: substring match against ctx (original behavior).
    - type / author: exact match.
    - since_ts: ISO-8601 UTC timestamp; only layers strictly newer than
      this are returned.
    - last_n: keep only the most recent N results (after other filters),
      newest-first. Omit for the original oldest-first store order.
    - ctx_prefix: path-segment prefix match against ctx (e.g. "book4/ch2"
      matches "book4/ch2/topic" but not "book4/ch2x/topic").
    - markers_any: include a layer if it has at least one of these markers.
    - text: case-insensitive substring of the body, the context, or a marker.
    - posture: "live", "archived", or "all". Default None/"all", so existing
      callers still receive every layer they received before. "live" keeps
      layers currentness does not mark inactive. "archived" keeps layers
      whose id is in currentness()["inactive"] (an archive tombstone does
      this via supersedes; reason is "superseded"). Each returned layer
      carries posture "live" or "archived". Applied before last_n.

    Unfiltered reads on stores past ~40 layers are already unusable in
    agent context windows; use these filters (or the `brief` tool) instead.
    """
    try:
        if posture not in (None, "live", "archived", "all"):
            raise ValueError("posture must be live, archived, or all")
        aethermind = AetherMind(project_root, create=False)
        aethermind._require_store()
        layers = aethermind.read_layers(
            ctx=ctx,
            type=type,
            author=author,
            since_ts=since_ts,
            last_n=None,
            ctx_prefix=ctx_prefix,
            markers_any=markers_any,
            text=text,
        )
        inactive = aethermind.currentness().get("inactive") or {}
        selected = []
        for layer in layers:
            layer_posture = _posture_of(layer, inactive)
            if posture in ("live", "archived") and layer_posture != posture:
                continue
            selected.append(layer)
        if last_n is not None:
            selected = sorted(
                selected,
                key=lambda layer: (
                    getattr(layer, "ts", "") or "",
                    getattr(layer, "id", "") or "",
                ),
                reverse=True,
            )[: max(0, int(last_n))]
        return json.dumps({
            "success": True,
            "store": str(aethermind.project_root),
            "layers": [_layer_to_dict(l, inactive) for l in selected],
            "count": len(selected),
        })
    except Exception as e:
        return _error_json(e)

@mcp.tool()
def read_texture(project_root: str) -> str:
    """Return the full texture file content.
    """
    try:
        aethermind = AetherMind(project_root, create=False)
        texture = aethermind.read_texture()
        return json.dumps({"success": True, "texture": texture, "count": len(texture)})
    except Exception as e:
        return _error_json(e)

@mcp.tool()
def status(project_root: str) -> str:
    """Return layer count, last author, last timestamp, and type breakdown.
    """
    try:
        aethermind = AetherMind(project_root, create=False)
        summary = aethermind.status_summary()
        return json.dumps({"success": True, **summary})
    except Exception as e:
        return _error_json(e)

@mcp.tool()
def brief(project_root: str, as_of: Optional[str] = None, anchor: Optional[str] = None, budget: Optional[int] = None) -> str:
    """Return active digests and per-scope deltas at an optional timestamp.

    When ``anchor`` is provided, returns the anchor-scoped deterministic brief
    (read contract) instead: must-know corrections, unabsorbed debt,
    budget-thinned kept layers, tombstones, and a determinism key. The same
    ledger + same budget produce a byte-identical brief.
    """
    try:
        aethermind = AetherMind(project_root, create=False)
        if anchor is not None or budget is not None:
            result = aethermind.brief_anchor(
                anchor=anchor, budget=budget if budget is not None else 4000, as_of=as_of
            )
            return json.dumps({
                "success": True,
                "anchor": result["anchor"],
                "must_know": result["must_know"],
                "unabsorbed_debt": result["unabsorbed_debt"],
                "kept": [_layer_to_dict(l) for l in result["kept"]],
                "tombstones": result["tombstones"],
                "budget": result["budget"],
                "budget_used": result["budget_used"],
                "layer_count": result["layer_count"],
                "determinism_key": result["determinism_key"],
            })
        result = aethermind.brief(as_of=as_of)
        return json.dumps({
            "success": True,
            "digests": [_layer_to_dict(l) for l in result["digests"]],
            "deltas": [_layer_to_dict(l) for l in result["deltas"]],
            "newest_digest_ts": result["newest_digest_ts"],
            "layer_count": result["layer_count"],
            "type_breakdown": result["type_breakdown"],
        })
    except Exception as e:
        return _error_json(e)


@mcp.tool()
def audit(project_root: str) -> str:
    """Scan the ledger hash chain and report integrity (audit)."""
    try:
        aethermind = AetherMind(project_root, create=False)
        result = aethermind.audit()
        return json.dumps({"success": True, **result})
    except Exception as e:
        return _error_json(e)


@mcp.tool()
def gate_check(
    project_root: str,
    anchor: Optional[str] = None,
    planned_type: str = "layer",
    planned_ctx: Optional[str] = None,
    enforce_gate: bool = False,
) -> str:
    """Check a proposed mutation against must-know corrections (gate).

    Advisory by default; when enforce_gate is true, unabsorbed must-know
    corrections reject the plan with the layer IDs to account for first.
    """
    try:
        aethermind = AetherMind(project_root, create=False)
        plan = {
            "anchor": anchor,
            "planned_type": planned_type,
            "planned_ctx": planned_ctx,
            "enforce_gate": enforce_gate,
        }
        result = aethermind.gate_check(plan, anchor=anchor)
        return json.dumps({"success": True, **result})
    except Exception as e:
        return _error_json(e)


@mcp.tool()
def write_event(
    project_root: str,
    event_type: str,
    body: str,
    ctx: str,
    author: Optional[str] = None,
) -> str:
    """Append a cheap routine event to events.aem (split ledger).

    Events never enter currentness or the default brief; they keep the
    decision ledger dense. Returns {event_id, hash}.
    """
    try:
        aethermind = AetherMind(project_root, create=False)
        return json.dumps({"success": True, **aethermind.write_event(
            type=event_type, body=body, ctx=ctx, author=author
        )})
    except Exception as e:
        return _error_json(e)


@mcp.tool()
def read_events(project_root: str) -> str:
    """Read events from events.aem (split ledger); empty store is healthy."""
    try:
        aethermind = AetherMind(project_root, create=False)
        return json.dumps({"success": True, **aethermind.read_events()})
    except Exception as e:
        return _error_json(e)


@mcp.tool()
def archive(
    project_root: str,
    layer_ids: List[str],
    reason: Optional[str] = None,
) -> str:
    """Archive layers to archive.aem (tombstone + continuation header).

    The ledger is never rewritten in place; the tombstone supersedes the
    archived layers so they leave live posture while history is preserved.
    """
    try:
        aethermind = AetherMind(project_root, create=False)
        return json.dumps({"success": True, **aethermind.archive(
            layer_ids, reason=reason or ""
        )})
    except Exception as e:
        return _error_json(e)


@mcp.tool()
def currentness(project_root: str, as_of: Optional[str] = None) -> str:
    """Return immutable history, active heads, and explicit inactive reasons."""
    try:
        aethermind = AetherMind(project_root, create=False)
        result = aethermind.currentness(as_of=as_of)
        return json.dumps({
            "success": True,
            "history": [_layer_to_dict(l) for l in result["history"]],
            "active_heads": [_layer_to_dict(l) for l in result["active_heads"]],
            "inactive": result["inactive"],
            "unresolved_currentness": [
                _layer_to_dict(l) for l in result["unresolved_currentness"]
            ],
            "as_of": result["as_of"],
        })
    except Exception as e:
        return _error_json(e)

@mcp.tool()
def initialize_store(project_root: str, project_key: Optional[str] = None) -> str:
    """Explicitly create store metadata without writing a layer or texture."""
    try:
        return json.dumps({
            "success": True,
            **_initialize_store(project_root, project_key=project_key),
        })
    except Exception as e:
        return _error_json(e)


@mcp.tool()
def capabilities(project_root: Optional[str] = None) -> str:
    """Return the canonical runtime identity and capability contract."""
    try:
        return json.dumps({
            "success": True,
            "runtime": _runtime_capabilities(project_root),
        })
    except Exception as e:
        return _error_json(e)


@mcp.tool()
def register_project_key(project_root: str, key: str, overwrite: bool = False) -> str:
    """Declare a stable project key for the store at project_root (e.g.
    key="sample-project"). After registration, every
    AetherMind tool may take `key` in place of project_root.

    This is addressing only: it does NOT merge stores. If `key` is already
    registered to a different root, this fails unless overwrite=True.
    Registering is also the explicit "registration step" that authorizes
    creating a new store at project_root if one does not already exist
    there (equivalent to passing create=True once).
    """
    try:
        result = _register_project_key(project_root, key, overwrite=overwrite)
        return json.dumps({"success": True, **result})
    except Exception as e:
        return _error_json(e)

@mcp.tool(annotations=_READ_ONLY)
def view(project_root: str, budget_chars: int = 3600, recent_window: int = 64) -> str:
    """Render the hybrid brief: whole-store coarse cover + full-res must-knows.

    Fixed-budget read-only view over the project's layers and its derived
    summary tree (tree.aem). budget_chars=3600 is about 900 estimated tokens.
    Raises if the tree has not been built yet.
    """
    try:
        from aethermind.view import render_view, view_cost
        text = render_view(project_root, budget_chars=budget_chars,
                           recent_window=recent_window)
        return json.dumps({"success": True, "view": text, **view_cost(text)})
    except Exception as e:
        return _error_json(e)

@mcp.tool(annotations=_READ_ONLY)
def zoom(project_root: str, level: int, index: int) -> str:
    """Zoom into one summary-tree node: its line plus its children's lines.

    Level 0 nodes map one-to-one onto layers (chronological index). Each
    higher level halves the count; the root is (max_level, 0). Returns the
    node text, its record range, and its two children's texts so an agent
    can walk from the root to a raw record.
    """
    try:
        from aethermind.tree import TreeStore, default_tree_path
        from pathlib import Path
        tree_path = default_tree_path(Path(project_root))
        store = TreeStore(tree_path)
        nodes = store.index()
        node = nodes.get((level, index))
        if node is None:
            return json.dumps({"success": False,
                               "error": f"no node at (level={level}, index={index})"})
        child_level = level - 1
        left = nodes.get((child_level, index * 2))
        right = nodes.get((child_level, index * 2 + 1))
        return json.dumps({
            "success": True,
            "level": node.level,
            "index": node.index,
            "start": node.start,
            "end": node.end,
            "cut": node.cut,
            "text": node.text,
            "children": [
                {"level": c.level, "index": c.index, "text": c.text,
                 "cut": c.cut}
                for c in (left, right) if c is not None
            ],
        })
    except Exception as e:
        return _error_json(e)

# -----------------------------------------------------------------------------
# Server entry point – runs as a FastMCP streamable‑http server.
# -----------------------------------------------------------------------------

if __name__ == "__main__":
    import os
    # Default port 8452; allow override via first command-line arg.
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8452
    # Bind host: default 127.0.0.1 (safe/local-only). Set MCP_HOST=0.0.0.0 or LAN IP for network access.
    mcp.settings.host = os.environ.get("MCP_HOST", "127.0.0.1")
    allowed_hosts = [h.strip() for h in os.environ.get("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]
    if allowed_hosts:
        try:
            mcp.settings.transport_security.allowed_hosts = allowed_hosts
        except Exception:
            pass
    mcp.settings.port = port
    mcp.run(transport="streamable-http")
