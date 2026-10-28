"""Transport must preserve the native primitive and make evidence limits clear."""

import dataclasses
import json

import pytest

from aethermind import AetherMind
from aethermind.inheritance import (
    RECEIPT_SCHEMA, prepare_inheritance, render_inheritance, verify_use,
    prepare_discovery, render_discovery, project_identity_path,
)


@pytest.fixture
def store(tmp_path):
    aether = AetherMind(tmp_path, create=True)
    original = aether.write_layer("decision", "Original whole rationale.", "design/route")
    correction = aether.write_layer("correction", "The correction retains its whole explanation.",
                                    "design/route", corrects=[original.id])
    aether.write_layer("discovery", "Optional detail.", "design/route")
    aether.write_texture("Keep the pressure around the seam visible.", "design/route", marker="~")
    return aether, original, correction


def test_native_contract_and_overflow_are_preserved(store):
    aether, original, correction = store
    envelope = prepare_inheritance(aether.project_root, "design", budget=1)
    native = aether.brief_anchor("design", budget=1)
    native["kept"] = [dataclasses.asdict(layer) for layer in native["kept"]]
    assert envelope["native"] == native
    assert envelope["native"]["budget_used"] > 1
    assert correction.id in envelope["native"]["must_know"]
    assert envelope["native"]["unabsorbed_debt"][0]["targets"] == [original.id]
    assert original.id in envelope["currentness"]["active_ids"]
    assert correction.body in render_inheritance(envelope)
    assert envelope["native"]["tombstones"][0]["dropped"] == "budget"


def test_inactive_kept_records_are_explicitly_historical(store):
    aether, original, _ = store
    replacement = aether.write_layer("supersession", "New posture.", "design/route",
                                     supersedes=[original.id], reason="Observed change")
    envelope = prepare_inheritance(aether.project_root, "design", budget=1)
    assert original.id in {layer["id"] for layer in envelope["native"]["kept"]}
    assert original.id not in envelope["currentness"]["active_ids"]
    assert envelope["currentness"]["inactive"][original.id]["by"] == replacement.id
    assert f"[{original.id} historical/inactive" in render_inheritance(envelope)


def test_content_mutation_invalidates_even_when_native_id_key_does_not(store):
    aether, _, _ = store
    first = prepare_inheritance(aether.project_root, "design", budget=1)
    content = aether.layers_path.read_text()
    aether.layers_path.write_text(content.replace("Original whole rationale.", "Changed original rationale."))
    second = prepare_inheritance(aether.project_root, "design", budget=1)
    assert first["native"]["determinism_key"] == second["native"]["determinism_key"]
    assert first["revision"] != second["revision"]


def test_scope_budget_texture_and_identity_bind_revision(store):
    aether, _, _ = store
    first = prepare_inheritance(aether.project_root, "design", budget=1)
    assert first == prepare_inheritance(aether.project_root, "design", budget=1)
    assert first["revision"] != prepare_inheritance(aether.project_root, "design", budget=2)["revision"]
    assert first["revision"] != prepare_inheritance(aether.project_root, "other", budget=1)["revision"]
    aether.write_texture("A new whole attention pointer.", "design/route")
    second = prepare_inheritance(aether.project_root, "design", budget=1)
    assert first["revision"] != second["revision"]
    identity_file = project_identity_path(aether.project_root)
    identity_file.parent.mkdir(parents=True, exist_ok=True)
    identity_file.write_text(json.dumps({
        "schema_version": "1", "project_id": "4123de54-aeef-4130-9bcb-30081d5a1151"}))
    third = prepare_inheritance(aether.project_root, "design", budget=1)
    assert second["revision"] != third["revision"]
    assert third["identity"]["status"] == "local_identity_validated"
    assert third["identity"]["authority_verified"] is False


def test_texture_is_whole_separate_and_scoped(store):
    aether, _, _ = store
    aether.write_texture("Other project's attention.", "elsewhere")
    envelope = prepare_inheritance(aether.project_root, "design")
    selected = envelope["texture"]["entries"]
    assert len(selected) == 1
    assert "Keep the pressure" in selected[0]
    assert selected[0].endswith("---\n")
    assert "Other project's" not in selected[0]
    assert envelope["texture"]["omitted_content"] is True
    aether.write_texture("x" * 1300, "design/route")
    oversize = prepare_inheritance(aether.project_root, "design")
    assert oversize["texture"]["status"] == "reference_only"
    assert oversize["texture"]["entries"] == []


def test_whole_legacy_texture_survives_with_dated_entries(store):
    aether, _, _ = store
    body = "~The seam still has pressure.\n\nA second paragraph belongs to the same entry."
    legacy = body + "\nContext: design/route\nMarker: ~\n\n"
    aether.texture_store.write(legacy)
    first = prepare_inheritance(aether.project_root, "design")
    assert body in first["texture"]["entries"][0]
    assert "Context: design/route\nMarker: ~\n" in first["texture"]["entries"][0]
    assert first["texture"]["entry_forms"] == ["legacy-context-marker"]
    aether.write_texture("A later dated entry.", "design/route")
    second = prepare_inheritance(aether.project_root, "design")
    assert second["texture"]["entry_forms"] == ["dated"]
    assert second["texture"]["matching_entries"] == 3


def test_missing_store_is_not_initialized(tmp_path):
    envelope = prepare_inheritance(tmp_path)
    assert envelope["native"]["kept"] == []
    assert envelope["provenance"]["store_exists"] is False
    assert not (tmp_path / ".aethermind").exists()


def test_discovery_is_explicitly_a_directory_without_layer_bodies(store):
    aether, original, correction = store
    manifest = prepare_discovery(aether.project_root, surface="fixture", session_id="one")
    assert manifest["contexts"] == [{"anchor": "design/route", "record_count": 3}]
    assert manifest["must_know_refs"] == [{"id": correction.id, "ctx": "design/route"}]
    assert "native" not in manifest
    assert original.body not in json.dumps(manifest)
    rendered = render_discovery(manifest, max_contexts=0)
    assert "full native inheritance has not been delivered" in rendered
    assert "preview 0/1" in rendered


@pytest.mark.parametrize("receipt", [None, [], {"kind": []}, {"kind": "applied", "evidence": None}])
def test_receipt_malformed_claims_are_rejected(store, receipt):
    aether, _, _ = store
    result = verify_use(prepare_inheritance(aether.project_root), receipt)
    assert result["valid"] is False


@pytest.mark.parametrize("kind,evidence", [
    ("delivered", {"transport_ref": "session://fixture/packet-1"}),
    ("recognized", {"response_ref": "session://fixture/response-1"}),
    ("applied", {"action_ref": "session://fixture/action-1", "outcome_ref": "artifact://fixture/observed"}),
])
def test_receipt_validates_binding_without_claiming_truth_or_changing_debt(store, kind, evidence):
    aether, _, correction = store
    envelope = prepare_inheritance(aether.project_root, "design", surface="fixture", session_id="one")
    before = aether.layers_path.read_bytes(), aether.texture_path.read_bytes()
    receipt = {"schema": RECEIPT_SCHEMA, "revision": envelope["revision"], "surface": "fixture",
               "session_id": "one", "kind": kind, "record_ids": [correction.id], "evidence": evidence}
    verdict = verify_use(envelope, receipt)
    assert verdict["valid"] is True
    assert verdict["evidence_verified"] is verdict["comprehension_proven"] is verdict["debt_changed"] is False
    after = prepare_inheritance(aether.project_root, "design", surface="fixture", session_id="one")
    assert after == envelope
    assert before == (aether.layers_path.read_bytes(), aether.texture_path.read_bytes())
    assert after["native"]["unabsorbed_debt"][0]["unabsorbed"] is True


def test_receipt_cannot_promote_acknowledgement_to_applied_use(store):
    aether, _, correction = store
    envelope = prepare_inheritance(aether.project_root, "design", surface="fixture", session_id="one")
    receipt = {"schema": RECEIPT_SCHEMA, "revision": envelope["revision"], "surface": "fixture",
               "session_id": "one", "kind": "applied", "record_ids": [correction.id],
               "evidence": {"response_ref": "session://fixture/said-understood"}}
    verdict = verify_use(envelope, receipt)
    assert verdict["valid"] is False
    assert "applied requires action_ref" in verdict["errors"]
    assert "applied requires outcome_ref" in verdict["errors"]


def test_receipt_rejects_other_revision_session_or_missing_record(store):
    aether, _, _ = store
    envelope = prepare_inheritance(aether.project_root, "design", surface="fixture", session_id="one")
    receipt = {"schema": RECEIPT_SCHEMA, "revision": "stale", "surface": "fixture",
               "session_id": "other", "kind": "recognized", "record_ids": ["missing"],
               "evidence": {"response_ref": "session://fixture/said-understood"}}
    verdict = verify_use(envelope, receipt)
    assert verdict["valid"] is False
    assert len(verdict["errors"]) == 3
