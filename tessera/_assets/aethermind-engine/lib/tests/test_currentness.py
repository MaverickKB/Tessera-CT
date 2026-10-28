"""Pass 3 tests for structural currentness and scoped digest briefings."""

from __future__ import annotations

from aethermind.core import AetherMind


def _seed(aether: AetherMind, *, ts: str, ctx: str, body: str, layer_type: str = "discovery", **kwargs):
    assert aether.layer_store is not None
    return aether.layer_store.append(
        author="currentness-test",
        type=layer_type,
        body=body,
        ctx=ctx,
        conf=1.0,
        markers=[],
        ts=ts,
        **kwargs,
    )


def test_unrelated_newer_digest_cannot_hide_valid_delta(tmp_path):
    aether = AetherMind(tmp_path, create=True)
    _seed(aether, ts="2026-07-01T00:00:00Z", ctx="project/a/digest", body="A digest", layer_type="load-bearing")
    delta = _seed(aether, ts="2026-07-01T00:00:01Z", ctx="project/a/work", body="A changed")
    _seed(aether, ts="2026-07-01T00:00:02Z", ctx="project/b/digest", body="B digest", layer_type="load-bearing")

    result = aether.brief()

    assert [layer.id for layer in result["deltas"]] == [delta.id]


def test_same_scope_newer_digest_summarizes_prior_delta(tmp_path):
    aether = AetherMind(tmp_path, create=True)
    _seed(aether, ts="2026-07-01T00:00:00Z", ctx="project/a/digest", body="old digest", layer_type="load-bearing")
    summarized = _seed(aether, ts="2026-07-01T00:00:01Z", ctx="project/a/work", body="summarized change")
    newest = _seed(aether, ts="2026-07-01T00:00:02Z", ctx="project/a/digest", body="new digest", layer_type="load-bearing")

    result = aether.brief()

    assert [layer.id for layer in result["digests"]] == [newest.id]
    assert summarized.id not in {layer.id for layer in result["deltas"]}


def test_most_specific_digest_scope_controls_nested_delta(tmp_path):
    aether = AetherMind(tmp_path, create=True)
    _seed(aether, ts="2026-07-01T00:00:00Z", ctx="project/digest", body="project digest", layer_type="load-bearing")
    nested_digest = _seed(aether, ts="2026-07-01T00:00:02Z", ctx="project/ch1/digest", body="chapter digest", layer_type="load-bearing")
    before_nested = _seed(aether, ts="2026-07-01T00:00:01Z", ctx="project/ch1/work", body="already summarized")
    after_nested = _seed(aether, ts="2026-07-01T00:00:03Z", ctx="project/ch1/work", body="live nested delta")

    result = aether.brief()

    assert nested_digest.id in {layer.id for layer in result["digests"]}
    assert before_nested.id not in {layer.id for layer in result["deltas"]}
    assert after_nested.id in {layer.id for layer in result["deltas"]}


def test_thread_key_and_supersedes_produce_one_active_head(tmp_path):
    aether = AetherMind(tmp_path, create=True)
    wrong = _seed(
        aether,
        ts="2026-07-01T00:00:00Z",
        ctx="config/notes",
        body="Use the old notes directory",
        thread_key="notes.active-folder",
    )
    current = _seed(
        aether,
        ts="2026-07-01T00:00:01Z",
        ctx="config/notes",
        body="Use the current notes folder",
        layer_type="correction",
        thread_key="notes.active-folder",
        supersedes=[wrong.id],
    )

    projection = aether.currentness()
    brief = aether.brief()

    assert [layer.id for layer in projection["active_heads"]] == [current.id]
    assert projection["inactive"][wrong.id]["reason"] == "superseded"
    assert wrong.id not in {layer.id for layer in brief["deltas"]}
    assert current.id in {layer.id for layer in brief["deltas"]}


def test_rollback_event_removes_rolled_back_head_without_reactivating_stale_prose(tmp_path):
    aether = AetherMind(tmp_path, create=True)
    original = _seed(
        aether,
        ts="2026-07-01T00:00:00Z",
        ctx="claim/path",
        body="old path",
        thread_key="path.claim",
    )
    replacement = _seed(
        aether,
        ts="2026-07-01T00:00:01Z",
        ctx="claim/path",
        body="replacement path",
        layer_type="correction",
        thread_key="path.claim",
        supersedes=[original.id],
    )
    rollback = _seed(
        aether,
        ts="2026-07-01T00:00:02Z",
        ctx="claim/path",
        body="replacement rolled back; verify before reuse",
        layer_type="correction",
        primitive="rollback",
        thread_key="path.claim",
        rollback_of=[replacement.id],
        reason="replacement failed verification",
        restored_to=original.id,
    )

    projection = aether.currentness()

    assert [layer.id for layer in projection["active_heads"]] == [rollback.id]
    assert projection["inactive"][original.id]["reason"] in {"superseded", "thread-replaced"}
    assert projection["inactive"][replacement.id]["reason"] == "rolled-back"


def test_unlinked_correction_is_explicit_currentness_attention(tmp_path):
    aether = AetherMind(tmp_path, create=True)
    correction = _seed(
        aether,
        ts="2026-07-01T00:00:00Z",
        ctx="claim/unlinked",
        body="new claim with unknown target",
        layer_type="correction",
    )

    projection = aether.currentness()

    assert projection["unresolved_currentness"] == [correction]


def test_as_of_projection_is_reproducible(tmp_path):
    aether = AetherMind(tmp_path, create=True)
    first = _seed(
        aether,
        ts="2026-07-01T00:00:00Z",
        ctx="claim/as-of",
        body="first",
        thread_key="as-of.claim",
    )
    _seed(
        aether,
        ts="2026-07-01T00:00:01Z",
        ctx="claim/as-of",
        body="second",
        thread_key="as-of.claim",
        supersedes=[first.id],
    )

    projection = aether.currentness(as_of="2026-07-01T00:00:00Z")

    assert [layer.id for layer in projection["active_heads"]] == [first.id]


def test_thread_key_round_trips_and_is_strict(tmp_path):
    aether = AetherMind(tmp_path, create=True)
    written = aether.write_layer(
        type="discovery",
        body="threaded claim",
        ctx="claim/thread",
        conf=1.0,
        markers=[],
        thread_key="claim.thread",
    )
    assert aether.read_layers()[0].thread_key == "claim.thread"

    try:
        aether.write_layer(
            type="discovery",
            body="bad thread",
            ctx="claim/thread",
            conf=1.0,
            markers=[],
            thread_key=7,  # type: ignore[arg-type]
        )
    except ValueError as exc:
        assert "thread_key must be a string" in str(exc)
    else:
        raise AssertionError("non-string thread_key was accepted")
