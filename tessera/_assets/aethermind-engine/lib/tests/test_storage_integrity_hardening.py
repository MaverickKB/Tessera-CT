"""Pass 1 regression tests for AetherMind storage integrity."""

from __future__ import annotations

from dataclasses import asdict

import pytest

from aethermind import core
from aethermind.core import AetherLayer, AetherMind, AetherMindParseError, LayerStore


def _layer(**overrides):
    values = {
        "id": "0001",
        "ts": "2026-07-10T12:00:00Z",
        "author": "integrity-test",
        "type": "discovery",
        "body": "valid layer",
        "ctx": "integrity/test",
        "conf": 1.0,
        "markers": [],
    }
    values.update(overrides)
    return AetherLayer(**values)


def _record(layer_id: str, body: str = "valid") -> bytes:
    return (
        "[[layer]]\n"
        f'id = "{layer_id}"\n'
        'ts = "2026-07-10T12:00:00Z"\n'
        'author = "integrity-test"\n'
        'type = "discovery"\n'
        f'body = "{body}"\n'
        'ctx = "integrity/test"\n'
        "conf = 1.0\n"
        "markers = []\n"
        'primitive = "layer"\n\n'
    ).encode("utf-8")


def test_bool_or_non_float_confidence_is_rejected_before_write(tmp_path):
    aether = AetherMind(tmp_path, create=True)

    with pytest.raises(ValueError, match="conf must be a float"):
        aether.write_layer("discovery", "bad bool", "integrity/conf", conf=True)
    with pytest.raises(ValueError, match="conf must be a float"):
        aether.write_layer("discovery", "bad int", "integrity/conf", conf=1)

    assert not aether.layers_path.exists()


def test_scalar_and_list_field_types_are_strict():
    with pytest.raises(ValueError, match="author must be a non-empty string"):
        _layer(author=7)
    with pytest.raises(ValueError, match="markers must be an array of strings"):
        _layer(markers="not-an-array")
    with pytest.raises(ValueError, match="markers must contain only strings"):
        _layer(markers=["ok", 7])
    with pytest.raises(ValueError, match="host must be a string"):
        _layer(host=7)


def test_timezone_timestamp_is_required_and_legacy_minute_precision_reads():
    with pytest.raises(ValueError, match="timestamp with timezone"):
        _layer(ts="2026-07-10 12:00:00")
    with pytest.raises(ValueError, match="timestamp with timezone"):
        _layer(ts="2026-07-10T12:00:00")

    assert _layer(ts="2026-05-06T00:00Z").ts == "2026-05-06T00:00Z"


def test_serialized_record_is_round_tripped_before_file_is_created(tmp_path, monkeypatch):
    path = tmp_path / ".aethermind" / "layers.aem"
    store = LayerStore(path)
    monkeypatch.setattr(core, "_format_layer", lambda _layer: "[[layer]]\nconf = True\n")

    with pytest.raises(ValueError, match="isolated TOML round-trip"):
        store.write(_layer())

    assert not path.exists()


def test_acknowledged_write_matches_exact_readback(tmp_path):
    store = LayerStore(tmp_path / ".aethermind" / "layers.aem")
    expected = _layer(body='quotes " slash \\ newline\nkept', markers=[">", "RULE"])

    store.write(expected)

    assert asdict(store.read()[0]) == asdict(expected)


def test_schema_invalid_record_is_explicit_and_reserves_its_textual_id(tmp_path):
    path = tmp_path / ".aethermind" / "layers.aem"
    path.parent.mkdir()
    path.write_bytes(
        _record("0001")
        + _record("0002").replace(b"conf = 1.0", b'conf = "not-float"')
    )
    store = LayerStore(path)
    before = path.read_bytes()

    report = store.read_report()
    assert [layer.id for layer in report.layers] == ["0001"]
    assert report.reserved_ids == ["0001", "0002"]
    assert len(report.issues) == 1
    assert report.issues[0].layer_id == "0002"
    assert store.next_id(report.layers, report.reserved_ids) == "0003"

    with pytest.raises(AetherMindParseError) as exc_info:
        store.read()
    strict_report = exc_info.value.report
    assert strict_report is not None
    assert strict_report.issues == report.issues

    with pytest.raises(AetherMindParseError):
        store.append(
            author="integrity-test",
            type="discovery",
            body="must not append past corruption",
            ctx="integrity/test",
            conf=1.0,
            markers=[],
        )
    assert path.read_bytes() == before


def test_partial_final_record_salvages_prior_layers_and_blocks_append(tmp_path):
    path = tmp_path / ".aethermind" / "layers.aem"
    path.parent.mkdir()
    partial = (
        b"[[layer]]\nid = '0003'\nts = \"2026-07-10T12:00:02Z\"\n"
        b'author = "integrity-test"\ntype = "discovery"\nbody = "partial\n'
    )
    path.write_bytes(_record("0001", "first") + _record("0002", "second") + partial)
    store = LayerStore(path)
    before = path.read_bytes()

    report = store.read_report()
    assert [layer.id for layer in report.layers] == ["0001", "0002"]
    assert report.reserved_ids == ["0001", "0002", "0003"]
    assert len(report.issues) == 1
    assert report.issues[0].layer_id == "0003"

    with pytest.raises(AetherMindParseError) as exc_info:
        store.read()
    strict_report = exc_info.value.report
    assert strict_report is not None
    assert [layer.id for layer in strict_report.layers] == ["0001", "0002"]

    with pytest.raises(AetherMindParseError):
        store.append(
            author="integrity-test",
            type="discovery",
            body="blocked",
            ctx="integrity/test",
            conf=1.0,
            markers=[],
        )
    assert path.read_bytes() == before


def test_equal_timestamp_order_uses_append_id_as_tie_breaker(tmp_path):
    aether = AetherMind(tmp_path, create=True)
    assert aether.layer_store is not None
    common_ts = "2026-07-10T12:00:00Z"
    first = aether.layer_store.append(
        author="first",
        type="discovery",
        body="first",
        ctx="integrity/order",
        conf=1.0,
        markers=[],
        ts=common_ts,
    )
    second = aether.layer_store.append(
        author="second",
        type="discovery",
        body="second",
        ctx="integrity/order",
        conf=1.0,
        markers=[],
        ts=common_ts,
    )

    assert (first.id, second.id) == ("0001", "0002")
    assert [layer.id for layer in aether.read_layers(last_n=2)] == ["0002", "0001"]
    assert aether.status_summary()["last_author"] == "second"


def test_semantic_type_tokens_are_extensible_but_syntax_checked(tmp_path):
    aether = AetherMind(tmp_path, create=True)
    written = aether.write_layer(
        type="domain-decision",
        body="!A domain-specific semantic type remains visible.",
        ctx="types/extension",
        conf=0.9,
        markers=["!"],
        author="test-agent",
    )
    assert aether.read_layers()[0].id == written.id
    assert aether.read_layers()[0].type == "domain-decision"

    with pytest.raises(ValueError, match="lowercase semantic token"):
        aether.write_layer(
            type="Bad Type!",
            body="!Invalid token.",
            ctx="types/extension",
            conf=0.9,
            markers=["!"],
            author="test-agent",
        )
