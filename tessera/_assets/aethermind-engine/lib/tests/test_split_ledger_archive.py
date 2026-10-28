"""Tests for the split ledger (events.aem) and archive-as-migration."""

import pytest

from aethermind import AetherMind


@pytest.fixture
def store(tmp_path):
    aether = AetherMind(str(tmp_path), create=True)
    aether.write_layer(
        "load-bearing", "!Decision ledger stays dense.", "project/ledger", conf=1.0
    )
    aether.write_layer(
        "discovery", "A record that will later be archived.", "project/archive-me"
    )
    return aether


def test_events_excluded_from_currentness_and_brief(store):
    store.write_event("heartbeat", "routine tick", "ops/tick")
    store.write_event("heartbeat", "another routine tick", "ops/tick")
    events = store.read_events()
    assert len(events["events"]) == 2
    # currentness only sees layers, never events.
    projection = store.currentness()
    assert len(projection["history"]) == 2
    # default brief only sees layers.
    brief = store.brief_anchor(anchor=None, budget=4000)
    assert brief["layer_count"] == 2


def test_write_event_returns_receipt(store):
    receipt = store.write_event("heartbeat", "tick", "ops/tick")
    assert receipt["event_id"]
    assert len(receipt["hash"]) == 64
    event = [e for e in store.read_events()["events"] if e["id"] == receipt["event_id"]][0]
    assert event["ctx"] == "ops/tick"


def test_archive_appends_tombstone_and_never_rewrites(tmp_path):
    aether = AetherMind(str(tmp_path), create=True)
    first = aether.write_layer("load-bearing", "!keep me", "project/keep")
    doomed = aether.write_layer("discovery", "old note", "project/doomed")
    layers_path = tmp_path / ".aethermind" / "layers.aem"
    before = layers_path.read_text(encoding="utf-8")

    result = aether.archive([doomed.id], reason="cleanup")
    assert result["archived"] == [doomed.id]
    assert result["tombstone_id"]
    assert result["tombstone_hash"]

    # Append-only: the original file must be a prefix of the new file.
    after = layers_path.read_text(encoding="utf-8")
    assert after.startswith(before)
    # Tombstone supersedes the archived layer; archive file has continuation header.
    archive_text = (tmp_path / ".aethermind" / "archive.aem").read_text(encoding="utf-8")
    assert "continuation ledger" in archive_text
    assert "project/doomed" in archive_text

    # The archived layer is now inactive (superseded by tombstone).
    projection = aether.currentness()
    assert doomed.id not in {layer.id for layer in projection["active_heads"]}
    # The kept layer is still active.
    assert first.id in {layer.id for layer in projection["active_heads"]}


def test_archive_unknown_layer_is_hard_error(store):
    with pytest.raises(ValueError, match="unknown layers"):
        store.archive(["nope-9999"])
