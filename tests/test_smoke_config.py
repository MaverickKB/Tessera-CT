"""The cross-platform smoke turn starts the vendored AetherMind."""
import importlib.util
import json
import tomllib
from pathlib import Path

from tessera.config import load_config
from tessera.engine_manifest import load_manifest


ROOT = Path(__file__).resolve().parents[1]


def _smoke():
    spec = importlib.util.spec_from_file_location('tessera_smoke', ROOT / 'scripts' / 'smoke.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_smoke_config_uses_the_vendored_engine_and_a_build_receipt(tmp_path):
    smoke = _smoke()
    bundle = smoke.write_config(tmp_path)
    raw = tomllib.loads((tmp_path / 'config.toml').read_text(encoding='utf-8'))
    memory = raw['aethermind']
    assert 'enabled' not in memory
    assert memory['create_project'] is True
    assert Path(memory['bundled_path']) == bundle
    assert Path(memory['project']) == tmp_path / 'memory'
    assert (bundle / 'mcp' / 'aethermind_mcp.py').is_file()
    receipt = json.loads((bundle / 'build-receipt.json').read_text(encoding='utf-8'))
    manifest = load_manifest(ROOT)
    assert receipt['engine'] == 'bundled'
    assert receipt['source'] == 'vendored'
    assert receipt['commit'] == manifest['commit']
    assert receipt['files'] == len(manifest['files'])
    config = load_config(tmp_path)
    assert not hasattr(config.aethermind, 'enabled')
    assert config.aethermind.create_project is True
    for name in ('scripts/smoke.py', 'README.md', 'docs/SUPPORTED_SYSTEMS.md'):
        assert 'does not exercise AetherMind' not in (ROOT / name).read_text(encoding='utf-8')
