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
