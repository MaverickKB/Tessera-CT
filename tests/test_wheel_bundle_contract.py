"""A clean export builds a wheel that carries the vendored engine.

The engine commit lives in the repository. A default `pip wheel` of a clean export
must include those bytes and a build receipt. A tree that does not match the
manifest must fail the build, and the error names TESSERA_BUNDLE_SOURCE.

The success path uses the vendored commit. The failure path uses a small
synthetic tree, not a second copy of the real engine.
"""

from pathlib import Path
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / 'docs' / 'packaging' / 'aethermind-engine-manifest.json'
COMMIT = 'ec1011eebe00e8197e29acbbb3ae710090f53899'
SOURCE_REPO = 'https://github.com/MaverickKB/aethermind-engine'


def _extract(blob, tree):
    tree.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(blob), mode='r:*') as archive:
        kwargs = {}
        if 'filter' in tarfile.TarFile.extractall.__code__.co_varnames:
            kwargs['filter'] = 'data'
        archive.extractall(tree, **kwargs)


@pytest.fixture(scope='module')
def archive_blob():
    """The committed tree. Uncommitted edits are outside what a release builds."""
    from git_checkout import require_git_checkout

    require_git_checkout()
    result = subprocess.run(['git', 'archive', 'HEAD'], cwd=ROOT, capture_output=True, check=True)
    return result.stdout


def _build(tmp_path, blob, *, extra_env=None, mutate=None):
    tree = tmp_path / 'tree'
    _extract(blob, tree)
    if mutate is not None:
        mutate(tree)
    dist = tmp_path / 'dist'
    dist.mkdir()
    env = {key: value for key, value in os.environ.items() if key != 'TESSERA_BUNDLE_SOURCE'}
    if extra_env:
        env.update(extra_env)
    result = subprocess.run(
        [sys.executable, '-m', 'pip', 'wheel', '.', '--no-deps', '--no-build-isolation',
         '-w', str(dist)],
        cwd=tree, env=env, capture_output=True, text=True, timeout=180)
    return result, dist


def _wheel(dist):
    wheels = list(dist.glob('*.whl'))
    assert len(wheels) == 1, wheels
    return wheels[0]


def test_default_wheel_from_a_clean_export_ships_the_vendored_engine(tmp_path, archive_blob):
    result, dist = _build(tmp_path, archive_blob)
    assert result.returncode == 0, result.stdout + result.stderr
    wheel = _wheel(dist)
    manifest = json.loads(MANIFEST_PATH.read_text(encoding='utf-8'))
    prefix = 'tessera/_assets/aethermind-engine/'
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        for rel, digest in manifest['files'].items():
            entry = prefix + rel
            assert entry in names, rel
            assert hashlib.sha256(archive.read(entry)).hexdigest() == digest, rel
        license_text = archive.read(prefix + 'LICENSE')
        assert license_text.startswith(b'AetherMind Noncommercial Public License')
        receipt = json.loads(archive.read(prefix + 'build-receipt.json'))
        assert receipt['engine'] == 'bundled'
        assert receipt['commit'] == COMMIT
        assert receipt['source_repo'] == SOURCE_REPO
        assert receipt['source'] == 'vendored'
        assert receipt['files'] == len(manifest['files'])
        assert receipt['manifest'] == 'docs/packaging/aethermind-engine-manifest.json'
        provenance = json.loads(archive.read(prefix + 'provenance.json'))
        assert provenance['commit'] == COMMIT
        assert provenance['files'] == manifest['files']
        lock_name = 'tessera/_assets/aethermind-mcp-requirements.lock'
        assert lock_name in names
        assert archive.read(lock_name) == (ROOT / 'docs' / 'packaging' / 'aethermind-mcp-requirements.lock').read_bytes()
        assert prefix + 'mcp/requirements-mcp.lock' not in names

    site = tmp_path / 'site'
    installed = subprocess.run(
        [sys.executable, '-m', 'pip', 'install', '--no-deps', '--target', str(site), str(wheel)],
        check=False, capture_output=True, text=True)
    assert installed.returncode == 0, installed.stdout + installed.stderr
    decoy = site / 'aethermind-engine'
    (decoy / 'mcp').mkdir(parents=True)
    (decoy / 'mcp' / 'aethermind_mcp.py').write_text('# decoy\n', encoding='utf-8')
    (decoy / 'build-receipt.json').write_text('{}\n', encoding='utf-8')
    home = tmp_path / 'home'
    home.mkdir()
    probe = subprocess.run(
        [sys.executable, '-c',
         'import json\n'
         'from pathlib import Path\n'
         'from tessera.assets import asset_path\n'
         'from tessera.bundled_engine import _engine_root, mcp_requirements_lock\n'
         'from tessera.config import Config\n'
         f'home = Path({str(home)!r})\n'
         'config = Config(home=home, cwd=home)\n'
         'root = _engine_root(config)\n'
         'found = asset_path("aethermind-engine")\n'
         'print(json.dumps({"root": str(root), "found": str(found), "lock": str(mcp_requirements_lock())}))'],
        cwd=str(tmp_path),
        env={**os.environ, 'PYTHONPATH': str(site), 'TESSERA_BUNDLE_SOURCE': ''},
        capture_output=True, text=True)
    # cwd is the temp dir, not the checkout, so the import is the installed wheel.
    assert probe.returncode == 0, probe.stdout + probe.stderr
    report = json.loads(probe.stdout.strip().splitlines()[-1])
    root = report['root'].replace('\\', '/')
    found = report['found'].replace('\\', '/')
    site_text = str(site).replace('\\', '/')
    assert root == found
    assert root.endswith('/tessera/_assets/aethermind-engine')
    assert site_text in root
    assert not root.endswith('/site/aethermind-engine')
    lock = report['lock'].replace('\\', '/')
    assert lock.endswith('/tessera/_assets/aethermind-mcp-requirements.lock')
    assert site_text in lock
    assert '/aethermind-engine/' not in lock


def _sdist(tree, dist, env):
    built = subprocess.run(
        [sys.executable, '-c',
         'from setuptools.build_meta import build_sdist\n'
         'import pathlib, sys\n'
         'print(build_sdist(sys.argv[1]))\n',
         str(dist)],
        cwd=tree, env=env, capture_output=True, text=True, timeout=180)
    assert built.returncode == 0, built.stdout + built.stderr
    name = built.stdout.strip().splitlines()[-1]
    return dist / name


def _assert_wheel_matches_vendored_engine(wheel):
    manifest = json.loads(MANIFEST_PATH.read_text(encoding='utf-8'))
    prefix = 'tessera/_assets/aethermind-engine/'
    lock_bytes = (ROOT / 'docs' / 'packaging' / 'aethermind-mcp-requirements.lock').read_bytes()
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        for rel, digest in manifest['files'].items():
            entry = prefix + rel
            assert entry in names, rel
            assert hashlib.sha256(archive.read(entry)).hexdigest() == digest, rel
        assert prefix + 'mcp/requirements-mcp.lock' not in names
        assert archive.read('tessera/_assets/aethermind-mcp-requirements.lock') == lock_bytes


def _sdist_root(members):
    """The single top-level directory of an sdist, independent of set order."""
    tops = sorted({name.split('/', 1)[0] for name in members if '/' in name})
    assert len(tops) == 1, tops
    return tops[0] + '/'


def _wheel_names(wheel):
    with zipfile.ZipFile(wheel) as archive:
        return set(archive.namelist())


def _pip_wheel(source, dest, env):
    dest.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [sys.executable, '-m', 'pip', 'wheel', str(source), '--no-deps', '--no-build-isolation',
         '-w', str(dest)],
        cwd=source if Path(source).is_dir() else None,
        env=env, capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stdout + result.stderr
    return _wheel(dest)


def test_sdist_wheel_matches_a_wheel_built_from_the_export(tmp_path, archive_blob):
    direct_tree = tmp_path / 'direct'
    sdist_tree = tmp_path / 'sdist-tree'
    _extract(archive_blob, direct_tree)
    _extract(archive_blob, sdist_tree)
    env = {key: value for key, value in os.environ.items() if key != 'TESSERA_BUNDLE_SOURCE'}
    direct = _pip_wheel(direct_tree, tmp_path / 'direct-dist', env)
    sdist = _sdist(sdist_tree, tmp_path / 'sdist', env)
    with tarfile.open(sdist) as archive:
        members = set(archive.getnames())
    root_prefix = _sdist_root(members)
    assert root_prefix + 'docs/packaging/aethermind-engine-manifest.json' in members
    assert root_prefix + 'docs/packaging/aethermind-mcp-requirements.lock' in members
    assert root_prefix + 'seeds/tessera-seed-layers.json' in members
    assert root_prefix + 'seeds/README.md' in members
    assert root_prefix + 'tessera/_assets/aethermind-engine/mcp/aethermind_mcp.py' in members
    assert root_prefix + 'tessera/_assets/aethermind-engine/.gitignore' in members
    assert root_prefix + 'tessera/_assets/aethermind-engine/LICENSE' in members
    packaged = _pip_wheel(sdist, tmp_path / 'from-sdist', env)
    direct_names = _wheel_names(direct)
    packaged_names = _wheel_names(packaged)
    assert packaged_names == direct_names
    for name in (
            'tessera/_assets/seeds/tessera-seed-layers.json',
            'tessera/_assets/seeds/README.md',
            'tessera/_assets/seeds/skills/board-hygiene/SKILL.md',
            'tessera/_assets/seeds/skills/first-contact/SKILL.md',
            'tessera/_assets/seeds/skills/prove-the-real-target/SKILL.md',
            'tessera/_assets/seeds/skills/search-before-building/SKILL.md',
            'tessera/_assets/seeds/skills/small-model-turn/SKILL.md'):
        assert name in packaged_names
    _assert_wheel_matches_vendored_engine(packaged)
    _assert_wheel_matches_vendored_engine(direct)


def test_a_clean_export_fails_when_the_mcp_lock_is_missing(tmp_path, archive_blob):
    def mutate(tree):
        (tree / 'docs' / 'packaging' / 'aethermind-mcp-requirements.lock').unlink()

    result, _dist = _build(tmp_path, archive_blob, mutate=mutate)
    assert result.returncode != 0
    text = result.stdout + result.stderr
    assert 'aethermind-mcp-requirements.lock' in text
    assert 'mcp/requirements-mcp.lock' in text
    assert 'TESSERA_BUNDLE_SOURCE' in text
    assert not list((tmp_path / 'dist').glob('*.whl'))


def test_a_clean_export_fails_when_the_vendored_bytes_change(tmp_path, archive_blob):
    def mutate(tree):
        path = tree / 'tessera' / '_assets' / 'aethermind-engine' / 'mcp' / 'aethermind_mcp.py'
        path.write_bytes(path.read_bytes() + b'\n# tampered\n')

    result, _dist = _build(tmp_path, archive_blob, mutate=mutate)
    assert result.returncode != 0
    text = result.stdout + result.stderr
    assert 'TESSERA_BUNDLE_SOURCE' in text
    assert 'aethermind-engine-manifest.json' in text
    assert 'hash mismatch' in text
    assert 'mcp/aethermind_mcp.py' in text
    assert not list((tmp_path / 'dist').glob('*.whl'))


def test_bundle_source_override_rejects_a_synthetic_tree(tmp_path, archive_blob):
    fake = tmp_path / 'synthetic-engine'
    (fake / 'mcp').mkdir(parents=True)
    (fake / 'mcp' / 'aethermind_mcp.py').write_text(
        '# synthetic fixture, not the engine\n', encoding='utf-8')
    result, _dist = _build(
        tmp_path, archive_blob, extra_env={'TESSERA_BUNDLE_SOURCE': str(fake)})
    assert result.returncode != 0
    text = result.stdout + result.stderr
    assert 'TESSERA_BUNDLE_SOURCE' in text
    assert 'aethermind-engine-manifest.json' in text
    assert 'missing:' in text
    assert not list((tmp_path / 'dist').glob('*.whl'))


def test_bundle_source_override_accepts_a_byte_copy_of_the_vendored_tree(tmp_path, archive_blob):
    import shutil
    copy = tmp_path / 'same-bytes'
    shutil.copytree(ROOT / 'tessera' / '_assets' / 'aethermind-engine', copy)
    result, dist = _build(
        tmp_path, archive_blob, extra_env={'TESSERA_BUNDLE_SOURCE': str(copy)})
    assert result.returncode == 0, result.stdout + result.stderr
    with zipfile.ZipFile(_wheel(dist)) as archive:
        receipt = json.loads(archive.read('tessera/_assets/aethermind-engine/build-receipt.json'))
    assert receipt['source'] == 'TESSERA_BUNDLE_SOURCE'
    assert receipt['commit'] == COMMIT
