"""Changing stores must initialize them even while the embedded service is alive."""

import json
import sys
from types import SimpleNamespace

import pytest

from tessera import bundled_engine, seeds
from tessera.config import Config


@pytest.mark.aethermind_unavailable
@pytest.mark.parametrize('setting', ['project', 'personal_project', 'shelf'])
@pytest.mark.parametrize('seed_fails', [False, True])
def test_cached_engine_initializes_changed_store_or_shelf(tmp_path, monkeypatch, setting, seed_fails):
    config = Config(home=tmp_path, cwd=tmp_path)
    config.aethermind.project = str(tmp_path / 'original')
    seeds.ensure(config)
    root = tmp_path / 'bundle'
    (root / 'mcp').mkdir(parents=True)
    (root / 'mcp' / 'aethermind_mcp.py').touch()
    (root / 'build-receipt.json').write_text(json.dumps({'engine': 'test'}))
    monkeypatch.setattr(bundled_engine, '_engine_root', lambda checked: root)
    monkeypatch.setattr(bundled_engine, '_provision',
                        lambda *args: pytest.fail('a running engine must be reused'))
    key = str(bundled_engine._runtime_root(config, root))
    url = 'http://127.0.0.1:59997/mcp'
    proc = SimpleNamespace(poll=lambda: None)
    bundled_engine._REGISTRY[key] = {'url': url, 'proc': proc}
    assert bundled_engine.choose_memory(config)['url'] == url

    changed = tmp_path / ('new-' + setting)
    if setting == 'shelf':
        config.skills.shelf = str(changed)
        expected = changed / 'first-contact' / 'SKILL.md'
    else:
        setattr(config.aethermind, setting, str(changed))
        expected = changed / '.aethermind' / (
            'layers.aem' if setting == 'project' else 'project.json')
    assert not expected.exists()
    if seed_fails:
        def refuse(checked):
            raise seeds.SeedUnavailable('the new destination is read-only')
        monkeypatch.setattr(seeds, 'ensure', refuse)

    choice = bundled_engine.choose_memory(config)

    if seed_fails:
        assert not choice['url'], 'an uninitialized destination must not reuse the cached URL'
        assert 'the new destination is read-only' in choice['reason']
        assert not expected.exists()
    else:
        assert choice['url'] == url
        assert expected.is_file(), 'the cached engine bypassed initialization of the changed setting'
    assert bundled_engine._REGISTRY[key]['proc'] is proc


@pytest.mark.aethermind_unavailable
def test_live_engine_reads_new_project_without_restarting(tmp_path, monkeypatch):
    pytest.importorskip('mcp.server.fastmcp')
    from tessera.aethermind import AetherMindClient

    config = Config(home=tmp_path, cwd=tmp_path)
    config.aethermind.project = str(tmp_path / 'original')
    root = bundled_engine.ensure_local_engine_bundle(config)
    assert root is not None
    config.aethermind.bundled_path = str(root)
    # Exercise the real engine and HTTP client using an already installed MCP
    # runtime; this offline regression must not download dependencies.
    monkeypatch.setattr(bundled_engine, '_provision', lambda *args: sys.executable)
    try:
        first = bundled_engine.choose_memory(config)
        assert first['url'], first
        key = str(bundled_engine._runtime_root(config, root))
        proc = bundled_engine._REGISTRY[key]['proc']
        config.aethermind.project = str(tmp_path / 'new-project')
        second = bundled_engine.choose_memory(config)
        assert second['url'] == first['url']
        client = AetherMindClient(second['url'], timeout=15)
        result = client.read_layers(config.aethermind.project)
        expected, _ = seeds.normalize(seeds.load_pack())
        assert {layer['body'] for layer in expected} <= {
            layer['body'] for layer in result.get('layers', [])}, result
        assert bundled_engine._REGISTRY[key]['proc'] is proc
        assert proc.poll() is None
    finally:
        bundled_engine.stop_bundled_engine(config)
