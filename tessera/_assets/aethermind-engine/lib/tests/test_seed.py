from aethermind.cli import main
from aethermind.core import RUNTIME_VERSION, AetherMind
from aethermind.seed import SEED_AUTHOR, write_seed
from pathlib import Path


def test_seed_writes_only_the_generic_baseline(tmp_path):
    result = write_seed(tmp_path)
    aether = AetherMind(tmp_path, create=False)
    layers = aether.read_layers()

    assert result["count"] == 3
    assert result["author"] == SEED_AUTHOR
    assert [layer.author for layer in layers] == [SEED_AUTHOR, SEED_AUTHOR, SEED_AUTHOR]
    assert [layer.ctx for layer in layers] == [
        "guide/what-this-is",
        "guide/rules",
        "guide/bootstrap",
    ]
    texture = aether.read_texture()
    assert "stack of notes" in texture


def test_seed_refuses_a_store_that_already_has_layers(tmp_path):
    aether = AetherMind(tmp_path, create=True)
    aether.write_layer(
        type="discovery",
        body="real project note",
        ctx="work/note",
        conf=1.0,
        markers=[],
        author="example-writer",
    )
    try:
        write_seed(tmp_path)
    except ValueError as exc:
        assert "already has layers" in str(exc)
    else:
        raise AssertionError("seed wrote into a non-empty store")
    layers = aether.read_layers()
    assert len(layers) == 1
    assert layers[0].body == "real project note"


def test_cli_init_seed_round_trip(tmp_path, capsys):
    result = main(["init", "--project-root", str(tmp_path), "--seed"])
    payload = capsys.readouterr().out
    assert result == 0
    assert '"success": true' in payload.lower() or '"success": true' in payload
    layers = AetherMind(tmp_path, create=False).read_layers()
    assert len(layers) == 3


def test_package_version_matches_runtime():
    root = Path(__file__).resolve().parents[2]
    text = (root / "pyproject.toml").read_text(encoding="utf-8")
    assert f'version = "{RUNTIME_VERSION}"' in text
