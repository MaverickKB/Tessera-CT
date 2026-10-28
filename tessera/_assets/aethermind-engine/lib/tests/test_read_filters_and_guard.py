"""Tests for the 2026-07-02 MCP server upgrade:
- read_layers filters: since_ts, last_n, ctx_prefix, markers_any
- brief()
- project-key aliasing (register_project_key)
- auto-init guard (create flag)
"""
from __future__ import annotations

import json

import pytest

from aethermind import core
from aethermind.core import AetherMind, AutoInitBlocked


def _seed(aether: AetherMind, ctx: str, ts: str, markers=None, **kwargs):
    return aether.layer_store.append(
        author="filters-test",
        type=kwargs.pop("type", "discovery"),
        body=kwargs.pop("body", f"layer for {ctx} @ {ts}"),
        ctx=ctx,
        conf=1.0,
        markers=markers or [],
        ts=ts,
        **kwargs,
    )


# --- read_layers filters -----------------------------------------------

def test_ctx_prefix_matches_segments_not_substrings(tmp_path):
    aether = AetherMind(tmp_path, create=True)
    _seed(aether, "book4/ch2/topic", "2026-07-01T00:00:00Z")
    _seed(aether, "book4/ch2", "2026-07-01T00:00:01Z")
    _seed(aether, "book4/ch2x/topic", "2026-07-01T00:00:02Z")  # sibling segment, must not match
    _seed(aether, "book4x/ch2/topic", "2026-07-01T00:00:03Z")  # sibling segment, must not match

    matched = aether.read_layers(ctx_prefix="book4/ch2")

    assert {l.ctx for l in matched} == {"book4/ch2/topic", "book4/ch2"}


def test_since_ts_is_exclusive(tmp_path):
    aether = AetherMind(tmp_path, create=True)
    _seed(aether, "t/since", "2026-07-01T00:00:00Z")
    second = _seed(aether, "t/since", "2026-07-01T00:00:01Z")
    third = _seed(aether, "t/since", "2026-07-01T00:00:02Z")

    after = aether.read_layers(since_ts="2026-07-01T00:00:00Z")

    assert [l.id for l in after] == [second.id, third.id]


def test_last_n_returns_newest_first_without_disturbing_default_order(tmp_path):
    aether = AetherMind(tmp_path, create=True)
    first = _seed(aether, "t/lastn", "2026-07-01T00:00:00Z")
    second = _seed(aether, "t/lastn", "2026-07-01T00:00:01Z")
    third = _seed(aether, "t/lastn", "2026-07-01T00:00:02Z")

    default_order = aether.read_layers(ctx="t/lastn")
    newest_two = aether.read_layers(ctx="t/lastn", last_n=2)

    assert [l.id for l in default_order] == [first.id, second.id, third.id]
    assert [l.id for l in newest_two] == [third.id, second.id]


def test_markers_any_matches_intersection(tmp_path):
    aether = AetherMind(tmp_path, create=True)
    _seed(aether, "t/markers", "2026-07-01T00:00:00Z", markers=["RULE"])
    _seed(aether, "t/markers", "2026-07-01T00:00:01Z", markers=["RES"])
    _seed(aether, "t/markers", "2026-07-01T00:00:02Z", markers=[])

    matched = aether.read_layers(ctx="t/markers", markers_any=["RULE", "RES"])

    assert len(matched) == 2


def test_filters_are_composable_with_existing_filters(tmp_path):
    aether = AetherMind(tmp_path, create=True)
    _seed(aether, "book4/ch3/topic", "2026-07-01T00:00:00Z", markers=["RULE"], type="correction")
    _seed(aether, "book4/ch3/topic", "2026-07-01T00:00:01Z", markers=["RULE"], type="discovery")
    _seed(aether, "book4/ch3/other", "2026-07-01T00:00:02Z", markers=["RULE"], type="correction")

    matched = aether.read_layers(
        ctx_prefix="book4/ch3",
        type="correction",
        markers_any=["RULE"],
    )

    assert len(matched) == 2
    assert all(l.type == "correction" for l in matched)


def test_text_matches_body_context_or_marker(tmp_path):
    aether = AetherMind(tmp_path, create=True)
    body_hit = _seed(
        aether,
        "notes/plain",
        "2026-07-01T00:00:00Z",
        body="Victor Taelin wrote the spec",
    )
    _seed(aether, "notes/other", "2026-07-01T00:00:01Z", body="unrelated note")
    marker_hit = _seed(
        aether,
        "notes/marked",
        "2026-07-01T00:00:02Z",
        body="no name in the body",
        markers=["victor-taelin"],
    )
    context_hit = _seed(
        aether,
        "notes/taelin-context",
        "2026-07-01T00:00:03Z",
        body="no name here either",
    )

    matched = aether.read_layers(text="taelin")

    matched_ids = [layer.id for layer in matched]
    assert body_hit.id in matched_ids
    assert marker_hit.id in matched_ids
    assert context_hit.id in matched_ids
    assert len(matched_ids) == 3
    assert aether.read_layers(text="zzzz-not-present") == []


def test_old_read_layers_signature_and_order_unchanged(tmp_path):
    aether = AetherMind(tmp_path, create=True)
    aether.write_layer(type="discovery", body="first", ctx="t/oldsig", conf=1.0, markers=[])
    aether.write_layer(type="discovery", body="second", ctx="t/oldsig", conf=1.0, markers=[])

    # positional ctx/type/author still work exactly as before
    readback = aether.read_layers("t/oldsig", None, None)
    assert [l.body for l in readback] == ["first", "second"]


# --- brief() -------------------------------------------------------------

def test_brief_returns_newest_digest_per_ctx_and_post_digest_deltas(tmp_path):
    aether = AetherMind(tmp_path, create=True)
    _seed(aether, "book4/ch1/digest", "2026-07-01T00:00:00Z", type="load-bearing")
    ch1_digest_2 = _seed(aether, "book4/ch1/digest", "2026-07-01T00:00:05Z", type="load-bearing")
    ch2_digest = _seed(aether, "book4/ch2/digest", "2026-07-01T00:00:10Z", type="load-bearing")
    delta1 = _seed(aether, "book4/ch2/scene1", "2026-07-01T00:00:11Z")
    delta2 = _seed(aether, "book4/ch2/scene2", "2026-07-01T00:00:12Z")

    result = aether.brief()

    digest_ids = {l.id for l in result["digests"]}
    assert digest_ids == {ch1_digest_2.id, ch2_digest.id}  # newest per ctx only
    assert result["newest_digest_ts"] == ch2_digest.ts
    assert [l.id for l in result["deltas"]] == [delta1.id, delta2.id]
    assert result["layer_count"] == 5


def test_brief_with_no_digests_returns_full_store_as_deltas(tmp_path):
    aether = AetherMind(tmp_path, create=True)
    aether.write_layer(type="discovery", body="no digest yet", ctx="book4/ch1/scene1", conf=1.0, markers=[])

    result = aether.brief()

    assert result["digests"] == []
    assert result["newest_digest_ts"] is None
    assert len(result["deltas"]) == 1


def test_brief_on_empty_store_is_empty_not_an_error(tmp_path):
    aether = AetherMind(tmp_path, create=True)
    result = aether.brief()
    assert result == {
        "digests": [],
        "deltas": [],
        "newest_digest_ts": None,
        "layer_count": 0,
        "type_breakdown": {},
    }


# --- project-key aliasing -------------------------------------------------

def test_register_project_key_allows_addressing_by_key(tmp_path, monkeypatch):
    registry_path = tmp_path / "registry.json"
    monkeypatch.setattr(core, "_REGISTRY_PATH", registry_path)

    project_dir = tmp_path / "sample-project"
    result = core.register_project_key(project_dir, "sample-project")
    assert result["key"] == "sample-project"
    assert result["project_root"] == str(project_dir)

    # Addressed by key, not filesystem path.
    aether = AetherMind("sample-project", create=False)
    written = aether.write_layer(type="discovery", body="via key", ctx="t/key", conf=1.0, markers=[])

    # Also visible when addressed by the literal filesystem path.
    aether_by_path = AetherMind(str(project_dir), create=False)
    readback = aether_by_path.read_layers(ctx="t/key")

    assert len(readback) == 1
    assert readback[0].id == written.id

    # project.json metadata was declared on the store itself.
    meta_path = project_dir / ".aethermind" / "project.json"
    assert meta_path.exists()
    metadata = json.loads(meta_path.read_text(encoding="utf-8"))
    assert metadata["project_key"] == "sample-project"
    assert metadata["project_id"] == result["project_id"]


def test_register_project_key_does_not_auto_merge_conflicting_roots(tmp_path, monkeypatch):
    registry_path = tmp_path / "registry.json"
    monkeypatch.setattr(core, "_REGISTRY_PATH", registry_path)

    root_a = tmp_path / "store-a"
    root_b = tmp_path / "store-b"
    core.register_project_key(root_a, "shared-key")

    with pytest.raises(ValueError, match="already registered"):
        core.register_project_key(root_b, "shared-key")

    # Explicit overwrite repoints the key rather than merging.
    result = core.register_project_key(root_b, "shared-key", overwrite=True)
    assert result["project_root"] == str(root_b)


def test_unregistered_string_is_treated_as_literal_path(tmp_path, monkeypatch):
    registry_path = tmp_path / "registry.json"
    monkeypatch.setattr(core, "_REGISTRY_PATH", registry_path)

    # No registry file exists at all; a normal filesystem path must behave
    # exactly as before (fully backward compatible).
    aether = AetherMind(tmp_path, create=True)
    written = aether.write_layer(type="discovery", body="literal path", ctx="t/literal", conf=1.0, markers=[])
    assert written.id == "0001"


# --- auto-init guard -------------------------------------------------------

def test_write_without_create_on_new_root_is_blocked(tmp_path):
    fresh_root = tmp_path / "never-touched"
    aether = AetherMind(fresh_root, create=False)

    assert aether.layer_store is None
    assert not (fresh_root / ".aethermind").exists()

    with pytest.raises(AutoInitBlocked):
        aether.write_layer(type="discovery", body="blocked", ctx="t/guard", conf=1.0, markers=[])

    with pytest.raises(AutoInitBlocked):
        aether.write_texture(body="blocked", ctx="t/guard")

    # Blocked writes must never touch disk.
    assert not (fresh_root / ".aethermind").exists()


def test_reads_on_new_root_are_empty_not_errors(tmp_path):
    fresh_root = tmp_path / "never-touched-2"
    aether = AetherMind(fresh_root, create=False)

    assert aether.read_layers() == []
    assert aether.read_texture() == ""
    assert aether.status_summary()["layer_count"] == 0
    assert aether.brief()["layer_count"] == 0
    assert not (fresh_root / ".aethermind").exists()


def test_create_true_still_initializes_a_new_store(tmp_path):
    fresh_root = tmp_path / "explicit-create"
    aether = AetherMind(fresh_root, create=True)
    written = aether.write_layer(type="discovery", body="explicit create", ctx="t/guard", conf=1.0, markers=[])
    assert written.id == "0001"
    assert (fresh_root / ".aethermind").exists()


def test_default_create_is_safe_for_library_callers(tmp_path):
    fresh_root = tmp_path / "default-call"
    aether = AetherMind(fresh_root)
    with pytest.raises(AutoInitBlocked):
        aether.write_layer(
            type="discovery",
            body="default call",
            ctx="t/guard",
            conf=1.0,
            markers=[],
        )
    assert not (fresh_root / ".aethermind").exists()


def test_existing_store_is_always_writable_regardless_of_create_flag(tmp_path):
    root = tmp_path / "existing"
    bootstrap = AetherMind(root, create=True)
    bootstrap.write_layer(type="discovery", body="seed", ctx="t/guard", conf=1.0, markers=[])

    # Now re-open with create=False: the store already exists, so writes
    # must still succeed without needing the flag again.
    reopened = AetherMind(root, create=False)
    written = reopened.write_layer(type="discovery", body="second", ctx="t/guard", conf=1.0, markers=[])
    assert written.id == "0002"
