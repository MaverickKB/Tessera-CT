"""Hybrid brief renderer: whole-store coverage at a fixed budget.

Reads a project's layers plus its derived summary tree and renders ONE
brief that contains:

1. A coarse cover of the older region, built from the largest aligned
   tree nodes (guaranteed visibility of every record).
2. Full-resolution must-know layers, ranked by a deterministic RIV proxy
   (type weight, marker pressure, recency).
3. Newest recent-window records as their level-0 summary lines.
4. A zoom footer pointing at the zoom tool.

The recent window and the footer are reserved a share of the budget
BEFORE the coarse fill, so a long store cannot starve them.

Read-only: layers.aem is never written; tree.aem is only read.

Budget is chars (about 4 chars per estimated token). A 900-token brief
is therefore budget_chars=3600.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import List, Optional

from aethermind.core import AetherMind
from aethermind.tree import TreeStore, TreeNode, default_tree_path

# Type weights for the deterministic RIV proxy. Corrections carry the
# most re-inheritance value: they change what the next session does.
TYPE_WEIGHTS = {
    "correction": 1.0,
    "load-bearing": 0.9,
    "decision": 0.7,
    "fork": 0.7,
    "discovery": 0.5,
    "friction": 0.4,
    "uncertainty": 0.3,
    "observation": 0.2,
}
MARKER_BONUS = {"!": 0.2, ">": 0.1, "?": 0.05}
RECENCY_DECAY_PER_DAY = 0.03
MAX_MUSTKNOW_CHARS = 1200
# The zoom footer is short but load-bearing: it is how the reader gets
# from a coarse line back to full resolution. Reserve its bytes up front.
FOOTER_RESERVE = 320
# A must-know body smaller than this is not worth its own prefix line.
MIN_MUSTKNOW_BODY_BYTES = 80


def riv_score(layer, now_ts: Optional[float] = None,
              corrected_ids: Optional[set] = None) -> float:
    """Deterministic re-inheritance-value proxy in [0, ~1.5].

    corrected_ids: ids that some other layer's `corrects` field points at.
    A superseded layer scores 0 so it never takes a full-res must-know
    slot; it stays visible through its coarse-cover line.
    """
    if corrected_ids and layer.id in corrected_ids:
        return 0.0
    score = TYPE_WEIGHTS.get(layer.type, 0.3)
    for marker in layer.markers or []:
        score += MARKER_BONUS.get(marker, 0.0)
    if layer.corrects:
        score += 0.1
    if now_ts is None:
        now_ts = time.time()
    try:
        from datetime import datetime, timezone
        ts = layer.ts.replace("Z", "+00:00")
        age_days = max(0.0, (now_ts - datetime.fromisoformat(ts).timestamp()) / 86400)
    except (ValueError, TypeError, OSError):
        age_days = 30.0
    return score * max(0.5, 1.0 - RECENCY_DECAY_PER_DAY * min(age_days, 30.0))


def decomposition(nodes: dict, end: int) -> List[TreeNode]:
    """Largest aligned tree nodes covering records [0, end)."""
    result = []
    pos = 0
    level = end.bit_length()
    while pos < end and level >= 0:
        width = 1 << level
        key = (level, pos // width)
        aligned = pos % width == 0
        if aligned and key in nodes and pos + width <= end:
            result.append(nodes[key])
            pos += width
        else:
            level -= 1
    return result


def render_view(project_root, budget_chars: int = 3600,
                recent_window: int = 64) -> str:
    """Render the hybrid brief. Raises ValueError if the tree is missing."""
    root = Path(project_root)
    tree_path = default_tree_path(root)
    if not tree_path.exists():
        raise ValueError(f"no summary tree at {tree_path}; run build_tree first")

    mind = AetherMind(root)
    layers = mind.read_layers()
    layers.sort(key=lambda item: (item.ts, item.id))
    total = len(layers)
    if total == 0:
        raise ValueError("store has no layers")

    nodes = TreeStore(tree_path).index()
    if not nodes:
        raise ValueError("summary tree is empty")

    window_start = max(0, total - recent_window)
    # Pin the must-know age reference to the newest record instead of the
    # wall clock: the same store must render the same brief on every read.
    from datetime import datetime
    now_ts = datetime.fromisoformat(layers[-1].ts).timestamp()

    # Compartment reserves. The recent window and the zoom footer get
    # their bytes set aside BEFORE the coarse fill. The old fill order
    # let the coarse cover plus the must-knows spend the whole budget,
    # which ended the brief with no recent lines and no footer.
    footer_reserve = FOOTER_RESERVE
    # One effective budget: every section spends inside budget minus the
    # footer reserve, so the footer can never be crowded out.
    usable = budget_chars - footer_reserve
    recent_reserve = usable // 4

    # 1. Recent window as level-0 summary lines, newest first, inside its
    #    reserve. When the reserve is full the oldest recent entries drop
    #    first; they stay reachable by zooming into their region.
    recent_lines = []
    recent_used = 0
    for index in range(total - 1, window_start - 1, -1):
        node = nodes.get((0, index))
        if node is None:
            continue
        line = f"[{index} {layers[index].ts}] {node.text}"
        cost = len(line.encode("utf-8")) + 1
        if recent_used + cost > recent_reserve:
            break
        recent_lines.append(line)
        recent_used += cost

    # 2. Coarse cover of the older region. This is the whole-store
    #    visibility guarantee: every record [0, window_start) is inside
    #    one of these lines, for as long as the remaining budget allows.
    coarse_lines = []
    coarse_used = 0
    coarse_spendable = usable - recent_used
    older_end = window_start
    if older_end > 0:
        for node in decomposition(nodes, older_end):
            line = f"[x{node.start}-{node.end} L{node.level}] {node.text}"
            cost = len(line.encode("utf-8")) + 1
            if coarse_used + cost > coarse_spendable:
                continue
            coarse_lines.append(line)
            coarse_used += cost

    # 3. Must-knows: top-RIV layers rendered in full from whatever budget
    #    is left. Layers that were corrected by a later layer are excluded
    #    here so a stale claim cannot poison the full-res section; the
    #    correction itself ranks up via its `corrects` bonus. When the
    #    leftover is smaller than a full body, the top correction is
    #    truncated to fit rather than dropped entirely.
    corrected_ids = set()
    for item in layers:
        for target_id in item.corrects or []:
            corrected_ids.add(target_id)
    scored = sorted(layers, key=lambda item: riv_score(item, now_ts, corrected_ids),
                    reverse=True)
    mustknow_lines = []
    mustknow_used = 0
    mustknow_spendable = coarse_spendable - coarse_used
    for layer in scored:
        # Score 0 means superseded: never render at full resolution.
        if riv_score(layer, now_ts, corrected_ids) == 0.0:
            continue
        prefix = f"[must-know {layer.ts} {layer.type}/{layer.ctx}] "
        prefix_cost = len(prefix.encode("utf-8")) + 1
        body_budget = min(mustknow_spendable - mustknow_used - prefix_cost,
                          MAX_MUSTKNOW_CHARS)
        if body_budget < MIN_MUSTKNOW_BODY_BYTES + 3:
            continue
        body = layer.body
        if len(body.encode("utf-8")) > body_budget:
            body = body.encode("utf-8")[: body_budget - 3].decode(
                "utf-8", "ignore") + "..."
        line = prefix + body
        cost = len(line.encode("utf-8")) + 1
        mustknow_lines.append(line)
        mustknow_used += cost

    footer = (
        f"[view: {total} records, {len(nodes)} tree nodes; "
        f"coarse lines cover records [0, {window_start}); "
        f"the bracketed timestamp lines just above are the {len(recent_lines)} "
        f"newest layers verbatim, newest first; "
        f"zoom any region with the zoom tool: give level then index, and it "
        f"returns that node plus its two children so you can walk down to "
        f"raw records]"
    )

    ordered_lines = coarse_lines + mustknow_lines + recent_lines
    used = coarse_used + mustknow_used + recent_used
    remaining = budget_chars - used
    if remaining > len(footer) + 1:
        ordered_lines.append(footer)

    return "\n".join(ordered_lines)


def view_cost(text: str) -> dict:
    """Report the rendered cost in chars and estimated tokens."""
    chars = len(text.encode("utf-8"))
    return {"chars": chars, "est_tokens": chars // 4}
