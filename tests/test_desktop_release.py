"""Release assembly uses real target layouts and emits installable artifacts."""
import hashlib
import importlib.util
from pathlib import Path
import tarfile

import pytest

_SPEC = importlib.util.spec_from_file_location('desktop_release', Path(__file__).resolve().parents[1] / 'scripts' / 'desktop_release.py')
release = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(release)


def built_bundle(root, row):
    directory = root / 'desktop' / 'target' / row['target'] / 'release' / 'bundle'
    if row['platform'] == 'macos':
        executable = directory / 'macos' / 'Tessera.app' / 'Contents' / 'MacOS' / 'tessera-desktop'
        executable.parent.mkdir(parents=True)
        executable.write_bytes(b'native-executable')
        executable.chmod(0o755)
        return executable
    formats = [('appimage', 'Tessera.AppImage')] if row['platform'] == 'linux' else [('nsis', 'Tessera-setup.exe'), ('msi', 'Tessera.msi')]
    for child, name in formats:
        path = directory / child / name
        path.parent.mkdir(parents=True)
        path.write_bytes((row['target'] + '/' + name).encode())
    return directory


@pytest.mark.parametrize('row', release.TARGETS, ids=lambda row: row['target'])
def test_assembly_reads_actual_target_triple_and_preserves_bundle(tmp_path, row):
    source = tmp_path / 'checkout'
    output = tmp_path / 'out'
    built_bundle(source, row)
    names = release.assemble(source, output, row['target'])
    for name in names:
        path = output / name
        assert path.is_file()
        expected = hashlib.sha256(path.read_bytes()).hexdigest() + '  ' + name + '\n'
        assert (output / (name + '.sha256')).read_text() == expected
    if row['platform'] == 'macos':
        with tarfile.open(output / names[0]) as archive:
            entry = archive.getmember('Tessera.app/Contents/MacOS/tessera-desktop')
            assert entry.isfile() and entry.mode & 0o111
            assert archive.extractfile(entry).read() == b'native-executable'
            assert 'Contents' not in archive.getnames(), 'must archive the .app, not its child directory'


def test_verify_requires_all_platform_assets_and_rejects_tampered_bytes(tmp_path):
    output = tmp_path / 'out'
    for row in release.TARGETS:
        built_bundle(tmp_path, row)
        release.assemble(tmp_path, output, row['target'])
    release.verify(output)
    image = output / 'tessera-linux-aarch64.AppImage'
    image.write_bytes(b'corrupt')
    with pytest.raises(ValueError, match='Checksum mismatch'):
        release.verify(output)
    image.unlink()
    with pytest.raises(ValueError, match='missing='):
        release.verify(output)


def test_assembly_rejects_ambiguous_output_instead_of_guessing(tmp_path):
    row = release.TARGETS[0]
    bundle = built_bundle(tmp_path, row)
    (bundle / 'appimage' / 'Other.AppImage').write_bytes(b'other')
    with pytest.raises(ValueError, match='exactly one'):
        release.assemble(tmp_path, tmp_path / 'out', row['target'])


def test_version_tag_must_match_every_downloaded_component(tmp_path):
    (tmp_path / 'desktop').mkdir()
    (tmp_path / 'pyproject.toml').write_text('[project]\nversion="1.2.3"\n')
    (tmp_path / 'desktop' / 'Cargo.toml').write_text('[package]\nversion="1.2.3"\n')
    (tmp_path / 'desktop' / 'tauri.conf.json').write_text('{"version":"1.2.3"}')
    release.check_version(tmp_path, 'v1.2.3')
    with pytest.raises(ValueError, match='must match'):
        release.check_version(tmp_path, 'v9.9.9')
    (tmp_path / 'desktop' / 'tauri.conf.json').write_text('{"version":"1.2.4"}')
    with pytest.raises(ValueError, match='must match'):
        release.check_version(tmp_path, 'v1.2.3')
