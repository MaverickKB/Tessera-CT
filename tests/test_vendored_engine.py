"""The vendored AetherMind tree is the public commit, byte for byte."""

from pathlib import Path
import shutil

import pytest

from tessera import assets
from tessera.bundled_engine import _engine_root, ensure_local_engine_bundle, mcp_requirements_lock
from tessera.cli import _missing_memory_engine
from tessera.config import Config
from tessera.engine_manifest import (
    SOURCE_REPO, EngineBundleError, engine_packaging, engine_source, load_manifest,
    parse_hashed_lock, vendored_dir, vendored_mcp_requirement, verify_engine_tree,
    verify_mcp_requirements_lock)

ROOT_COMMIT = 'ec1011eebe00e8197e29acbbb3ae710090f53899'


def test_vendored_tree_matches_the_manifest_and_names_its_commit():
    manifest = load_manifest()
    assert manifest['source_repo'] == SOURCE_REPO
    assert manifest['commit'] == ROOT_COMMIT
    assert manifest['license'] == 'AetherMind Noncommercial Public License'
    assert manifest['license_file'] == 'LICENSE'
    verify_engine_tree(vendored_dir(), manifest, override=False)
    license_text = (vendored_dir() / 'LICENSE').read_text(encoding='utf-8')
    assert license_text.startswith('AetherMind Noncommercial Public License\n')
    assert 'non-commercial' in license_text


def test_a_tampered_copy_names_the_file_and_the_override(tmp_path):
    copy = tmp_path / 'engine'
    shutil.copytree(vendored_dir(), copy)
    target = copy / 'lib' / 'aethermind' / 'core.py'
    target.write_bytes(target.read_bytes() + b'\n')
    with pytest.raises(EngineBundleError) as caught:
        verify_engine_tree(copy, load_manifest(), override=True)
    message = str(caught.value)
    assert 'TESSERA_BUNDLE_SOURCE' in message
    assert 'docs/packaging/aethermind-engine-manifest.json' in message
    assert 'hash mismatch: lib/aethermind/core.py' in message


def test_a_synthetic_tree_is_not_accepted_in_place_of_the_engine(tmp_path):
    fake = tmp_path / 'synthetic'
    (fake / 'mcp').mkdir(parents=True)
    (fake / 'mcp' / 'aethermind_mcp.py').write_text('# synthetic fixture, not the engine\n', encoding='utf-8')
    with pytest.raises(EngineBundleError) as caught:
        verify_engine_tree(fake, load_manifest(), override=True)
    message = str(caught.value)
    assert 'TESSERA_BUNDLE_SOURCE' in message
    assert 'missing:' in message
    assert 'LICENSE' in message


def test_an_empty_override_selects_the_vendored_tree(monkeypatch):
    monkeypatch.setenv('TESSERA_BUNDLE_SOURCE', '   ')
    root = Path(__file__).resolve().parents[1]
    source, override = engine_source(root)
    assert override is False
    assert source == vendored_dir(root)


def test_mcp_lock_lives_outside_the_vendored_tree_and_pins_the_extra():
    lock = verify_mcp_requirements_lock()
    assert lock == mcp_requirements_lock()
    assert lock.name == 'aethermind-mcp-requirements.lock'
    assert 'aethermind-engine' not in lock.parts
    assert not (vendored_dir() / 'mcp' / 'requirements-mcp.lock').exists()
    items = parse_hashed_lock(lock.read_text(encoding='utf-8'))
    names = [item[0].split(';', 1)[0].strip() for item in items]
    assert vendored_mcp_requirement() in names
    assert vendored_mcp_requirement() == 'mcp==1.27.1'
    assert any(name.startswith('pydantic==') for name in names)
    assert not any(name.split('==', 1)[0] in {'pytest', 'typer', 'rich', 'tomli'} for name in names)


def test_an_unhashed_mcp_pin_is_refused():
    with pytest.raises(EngineBundleError, match='no sha256 hash'):
        parse_hashed_lock('mcp==1.27.1\n')


def test_doctor_does_not_call_a_matching_vendored_tree_missing(tmp_path):
    config = Config(home=tmp_path, cwd=tmp_path)
    assert _missing_memory_engine(config) is None
    assert engine_packaging(config) == 'present'


def test_a_verified_source_tree_materializes_a_receipt_backed_bundle(tmp_path, monkeypatch):
    copy = tmp_path / 'engine'
    shutil.copytree(vendored_dir(), copy)
    monkeypatch.setattr(assets, 'asset_path',
                        lambda name: copy if name == 'aethermind-engine' else tmp_path / 'missing')
    config = Config(home=tmp_path / 'home', cwd=tmp_path)
    bundle = ensure_local_engine_bundle(config)
    assert bundle is not None
    assert (bundle / 'build-receipt.json').is_file()
    assert _engine_root(config) == bundle
    assert engine_packaging(config) == 'bundled'


def test_a_tampered_source_tree_does_not_materialize(tmp_path, monkeypatch):
    copy = tmp_path / 'engine'
    shutil.copytree(vendored_dir(), copy)
    target = copy / 'lib' / 'aethermind' / 'core.py'
    target.write_bytes(target.read_bytes() + b'\n')
    monkeypatch.setattr(assets, 'asset_path',
                        lambda name: copy if name == 'aethermind-engine' else tmp_path / 'missing')
    config = Config(home=tmp_path / 'home', cwd=tmp_path)
    assert ensure_local_engine_bundle(config) is None
    assert _engine_root(config) is None


def test_an_explicit_bundle_is_not_replaced(tmp_path):
    config = Config(home=tmp_path / 'home', cwd=tmp_path)
    config.aethermind.bundled_path = str(tmp_path / 'explicit')
    assert ensure_local_engine_bundle(config) is None


def _fake_engine_home(monkeypatch, tmp_path, source, name):
    copy = tmp_path / name
    shutil.copytree(source, copy)
    from tessera import assets
    monkeypatch.setattr(assets, 'asset_path',
                        lambda wanted: copy if wanted == 'aethermind-engine' else tmp_path / 'missing')
    return Config(home=tmp_path / ('home-' + name), cwd=tmp_path)


def _fake_launch(monkeypatch):
    import io
    import sys

    class Proc:
        def __init__(self):
            self.stdout = io.BytesIO(b'listening on 127.0.0.1:9\n')

        def poll(self):
            return None

        def terminate(self):
            return None

    monkeypatch.setattr('tessera.bundled_engine.subprocess.Popen', lambda argv, **kwargs: Proc())
    monkeypatch.setattr('tessera.bundled_engine._provision', lambda root, runtime=None: sys.executable)
    monkeypatch.setattr('tessera.bundled_engine._wait_for_engine', lambda *args, **kwargs: True)
    monkeypatch.setattr('tessera.bundled_engine._pick_port', lambda: 9)


def test_a_cold_session_materializes_the_bundle_before_launch(tmp_path, monkeypatch):
    """A checkout session that skipped init still starts the engine.

    init and serve materialize the home-local bundle up front. choose_memory
    reaches ensure_bundled_engine directly, so the launch itself must not
    report a missing engine while a verified vendored tree sits in the
    checkout.
    """
    from tessera.bundled_engine import ensure_bundled_engine, stop_bundled_engine
    config = _fake_engine_home(monkeypatch, tmp_path, vendored_dir(), 'cold')
    _fake_launch(monkeypatch)
    try:
        url = ensure_bundled_engine(config, force=True)
        assert url is not None and url.startswith('http://127.0.0.1:')
        bundle = ensure_local_engine_bundle(config)
        assert bundle is not None
        assert (bundle / 'build-receipt.json').is_file()
        assert _engine_root(config) == bundle
    finally:
        stop_bundled_engine(config)


@pytest.mark.aethermind_unavailable
def test_a_cold_session_with_a_tampered_tree_fails_closed(tmp_path, monkeypatch):
    """The launch does not turn a tampered tree into a running engine."""
    from tessera.bundled_engine import ensure_bundled_engine
    copy = tmp_path / 'engine'
    shutil.copytree(vendored_dir(), copy)
    target = copy / 'lib' / 'aethermind' / 'core.py'
    target.write_bytes(target.read_bytes() + b'\n')
    from tessera import assets
    monkeypatch.setattr(assets, 'asset_path',
                        lambda wanted: copy if wanted == 'aethermind-engine' else tmp_path / 'missing')
    config = Config(home=tmp_path / 'home', cwd=tmp_path)
    _fake_launch(monkeypatch)
    assert ensure_bundled_engine(config, force=True) is None
    assert ensure_local_engine_bundle(config) is None
    assert not (config.home / 'runtime').exists()
