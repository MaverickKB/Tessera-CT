"""Tests for the converged read contract: brief_anchor determinism,
correction links, append receipts, hash-chain integrity, and the gate contract.

These lock the load-bearing invariants from the foundry finalists:
- same ledger + same budget = byte-identical brief (determinism_key)
- correction targeting an unknown layer is a hard error
- every write returns a receipt hash; prev_hash chains the ledger
- audit() is a scan that detects tampering
- gate_check is advisory by default, enforceable per-project
"""

import json

import pytest

from aethermind import AetherMind


@pytest.fixture
def store(tmp_path):
    aether = AetherMind(str(tmp_path), create=True)
    aether.write_layer(
        "load-bearing",
        "!The north star: continuity over memory.",
        "project/origin",
        conf=1.0,
        markers=["!"],
        author="test",
    )
    aether.write_layer(
        "decision",
        "Split open substrate from closed intelligence.",
        "design/split",
        conf=0.95,
        markers=["!"],
        author="test",
    )
    aether.write_layer(
        "discovery",
        "Texture stays felt-sense, not source truth.",
        "design/texture",
        conf=0.9,
        markers=["~"],
        author="test",
    )
    aether.write_layer(
        "friction",
        "Reading the whole ledger is too expensive.",
        "design/read",
        conf=0.85,
        markers=[">"],
        author="test",
    )
    return aether


def layer_ids(brief):
    return [layer.id for layer in brief["kept"]]


def test_brief_anchor_determinism_lock(store):
    first = store.brief_anchor(anchor="design", budget=4000)
    second = store.brief_anchor(anchor="design", budget=4000)
    assert first["determinism_key"] == second["determinism_key"]
    # The determinism key must cover content, not just ids.
    third = store.brief_anchor(anchor="design", budget=100)
    assert first["determinism_key"] != third["determinism_key"]


def test_brief_anchor_scopes_to_anchor(store):
    design = store.brief_anchor(anchor="design", budget=4000)
    origin = store.brief_anchor(anchor="project/origin", budget=4000)
    assert any("split" in layer.ctx for layer in design["kept"])
    assert not any("split" in layer.ctx for layer in origin["kept"])


def test_brief_budget_thins_informational_but_keeps_decisions(store):
    tiny = store.brief_anchor(anchor="design", budget=1)
    kept_types = {layer.type for layer in tiny["kept"]}
    # Decision survives even at budget 1 (unconditional). The fixture's
    # load-bearing layer lives under project/origin, so it is not relevant to
    # the design anchor; the unconditional guarantee is checked over the whole
    # ledger below.
    assert "decision" in kept_types
    whole = store.brief_anchor(anchor=None, budget=1)
    assert "load-bearing" in {layer.type for layer in whole["kept"]}
    # Informational types may or may not survive, but the budget exhaustion is
    # self-described via tombstones unless nothing was dropped.
    if tiny["budget_used"] > tiny["budget"]:
        # unconditional-only spill is allowed; informational must have dropped
        assert any(t["dropped"] == "budget" for t in tiny["tombstones"])


def test_brief_full_ledger_tombstones_are_deterministic(store):
    one = store.brief_anchor(anchor=None, budget=200)
    two = store.brief_anchor(anchor=None, budget=200)
    assert one["determinism_key"] == two["determinism_key"]
    assert [t["id"] for t in one["tombstones"]] == [t["id"] for t in two["tombstones"]]


def test_correction_to_unknown_layer_is_hard_error(store):
    with pytest.raises(ValueError, match="does not exist"):
        store.write_layer(
            "correction",
            "Corrects a layer that was never written.",
            "design/read",
            corrects=["9999"],
            author="test",
        )


def test_correction_link_valid_and_receipt(store):
    target = store.write_layer(
        "discovery", "Old claim that needs correcting.", "design/claims"
    )
    correction = store.write_layer(
        "correction",
        "New truth replaces the old claim.",
        "design/claims",
        corrects=[target.id],
        author="test",
    )
    # Receipt hash is attached to the returned layer.
    assert getattr(correction, "receipt_hash", None)
    assert getattr(target, "receipt_hash", None)
    parsed = store.read_layers()
    corrected = [layer for layer in parsed if layer.id == correction.id][0]
    assert corrected.corrects == [target.id]


def test_append_receipt_hash_matches_canonical_serialization(store):
    from aethermind.core import _format_layer, _layer_hash

    written = store.write_layer(
        "discovery", "Receipt must match canonical bytes.", "design/receipt"
    )
    assert getattr(written, "receipt_hash") == _layer_hash(written)
    assert _layer_hash(written) != _layer_hash(
        store.write_layer("discovery", "Different body.", "design/receipt")
    )


def test_hash_chain_prev_hash_carry(store):
    from aethermind.core import _layer_hash

    store.write_layer("discovery", "chain record one", "design/chain")
    second = store.write_layer("discovery", "chain record two", "design/chain")
    assert second.prev_hash
    parsed = store.read_layers()
    last_two = parsed[-2:]
    # The last record's prev_hash must equal the canonical hash of the previous
    # record in the file (the chain is computed at write time over the stored
    # record, including its own prev_hash field).
    assert last_two[-1].prev_hash is not None
    assert last_two[-1].prev_hash == _layer_hash(last_two[-2])


def test_audit_healthy_after_writes(store):
    store.write_layer("discovery", "audit target", "design/audit")
    result = store.audit()
    assert result["healthy"] is True
    assert result["record_count"] >= 5
    # The first record is the chain root, not a legacy gap.
    assert result["legacy_prefix"] == 0
    assert result["chain_start"] is not None


def test_audit_detects_tampering(tmp_path):
    aether = AetherMind(str(tmp_path), create=True)
    aether.write_layer("load-bearing", "!original body", "project/tamper")
    aether.write_layer("discovery", "second record", "project/tamper")
    layers_path = tmp_path / ".aethermind" / "layers.aem"
    original = layers_path.read_text(encoding="utf-8")
    tampered = original.replace("original body", "tampered body")
    assert tampered != original
    layers_path.write_text(tampered, encoding="utf-8")
    result = aether.audit()
    assert result["healthy"] is False
    assert result["chain_breaks"]


def test_gate_check_advisory_by_default(store):
    plan = {"anchor": "design", "planned_type": "correction", "planned_ctx": "design/read"}
    result = store.gate_check(plan)
    assert result["verdict"] in ("clear", "advisory", "blocked")
    # No correction exists yet, so nothing blocks.
    assert result["verdict"] == "clear"


def test_gate_check_enforced_with_unabsorbed_correction(store):
    target = store.write_layer(
        "decision", "Decision that will be corrected.", "design/claims", conf=0.9
    )
    store.write_layer(
        "correction",
        "This corrects the decision; debt is unabsorbed until superseded.",
        "design/claims",
        corrects=[target.id],
        author="test",
    )
    plan = {
        "anchor": "design/claims",
        "planned_type": "discovery",
        "planned_ctx": "design/claims",
        "enforce_gate": True,
    }
    result = store.gate_check(plan)
    assert result["verdict"] == "blocked"
    assert result["blockers"]
    # Advisory default never blocks.
    plan["enforce_gate"] = False
    result = store.gate_check(plan)
    assert result["verdict"] in ("advisory", "clear")


def test_unabsorbed_debt_surfaces_correction_blast_radius(store):
    target = store.write_layer(
        "decision", "Decision at risk.", "design/claims", conf=0.9
    )
    dependent = store.write_layer(
        "discovery",
        "Built on the decision; should be re-verified when corrected.",
        "design/claims",
    )
    correction = store.write_layer(
        "correction",
        "Corrects the decision.",
        "design/claims",
        corrects=[target.id],
        author="test",
    )
    brief = store.brief_anchor(anchor="design/claims", budget=4000)
    assert any(c["correction"] == correction.id for c in brief["unabsorbed_debt"])
    debt_item = [c for c in brief["unabsorbed_debt"] if c["correction"] == correction.id][0]
    assert target.id in debt_item["targets"]
    # Absorbing the correction (superseding it) clears the debt flag.
    store.write_layer(
        "correction",
        "Absorbed: re-verified dependents.",
        "design/claims",
        supersedes=[correction.id],
        author="test",
    )
    brief_after = store.brief_anchor(anchor="design/claims", budget=4000)
    assert all(c["correction"] != correction.id for c in brief_after["unabsorbed_debt"])
