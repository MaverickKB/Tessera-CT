"""Real archive installation and platform installer contracts, without a global platform override."""

import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tarfile
import subprocess

import pytest

from tessera import desktop_install as desktop


def app_archive(entries=None):
    buffer = io.BytesIO()
    entries = entries if entries is not None else {'Tessera.app/Contents/MacOS/tessera-desktop': b'native-shell'}
    with tarfile.open(fileobj=buffer, mode='w:gz') as archive:
        for name, value in entries.items():
            member = tarfile.TarInfo(name)
            if isinstance(value, tuple):
                member.type = tarfile.SYMTYPE
                member.linkname = value[0]
            else:
                member.size = len(value)
                member.mode = 0o755
            archive.addfile(member, io.BytesIO(value) if isinstance(value, bytes) else None)
    return buffer.getvalue()


def install_fixture(monkeypatch, tmp_path, platform, data):
    monkeypatch.setattr(desktop.sys, 'platform', platform)
    monkeypatch.setattr(desktop.Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(desktop, '_fetch', lambda url, timeout: data)
    return desktop.install('https://example.test/bundle', hashlib.sha256(data).hexdigest(), timeout=17)


def test_real_macos_archive_installs_exact_app_and_returns_executable(tmp_path, monkeypatch):
    executable = install_fixture(monkeypatch, tmp_path, 'darwin', app_archive())
    assert executable == tmp_path / 'Applications/Tessera.app/Contents/MacOS/tessera-desktop'
    assert executable.read_bytes() == b'native-shell'
    assert executable.stat().st_mode & 0o111
    assert not (tmp_path / 'Applications/Tessera.app.app').exists()


@pytest.mark.parametrize('data', [
    b'not a gzip archive', app_archive({}), app_archive({'Wrong.app/file': b'x'}),
    app_archive({'Tessera.app/Contents/Info.plist': b'no executable'}),
    app_archive({'../outside': b'escape'}),
    app_archive({'Tessera.app/Contents/MacOS/tessera-desktop': ('../../../../outside',)}),
])
def test_invalid_mac_bundle_preserves_installed_app(tmp_path, monkeypatch, data):
    old = tmp_path / 'Applications/Tessera.app/Contents/MacOS/tessera-desktop'
    old.parent.mkdir(parents=True)
    old.write_bytes(b'previous shell')
    with pytest.raises(desktop.NativeShellNotFound):
        install_fixture(monkeypatch, tmp_path, 'darwin', data)
    assert old.read_bytes() == b'previous shell'
    assert not (tmp_path / 'outside').exists()
    assert sorted(p.name for p in (tmp_path / 'Applications').iterdir()) == ['Tessera.app']


def test_mac_replacement_rolls_back_when_rename_fails(tmp_path, monkeypatch):
    previous = tmp_path / 'Applications/Tessera.app/Contents/MacOS/tessera-desktop'
    previous.parent.mkdir(parents=True)
    previous.write_bytes(b'old')
    replace = desktop.os.replace
    def fail_new(source, target):
        if Path(source).name == 'Tessera.app' and Path(source).parent.name.startswith('.tessera-install-'):
            raise OSError('simulated final rename failure')
        return replace(source, target)
    monkeypatch.setattr(desktop.os, 'replace', fail_new)
    with pytest.raises(desktop.NativeShellNotFound):
        install_fixture(monkeypatch, tmp_path, 'darwin', app_archive())
    assert previous.read_bytes() == b'old'


def test_appimage_replacement_failure_preserves_working_binary(tmp_path, monkeypatch):
    previous = tmp_path / '.local/bin/tessera-desktop'
    previous.parent.mkdir(parents=True)
    previous.write_bytes(b'old')
    monkeypatch.setattr(desktop.os, 'replace', lambda *_: (_ for _ in ()).throw(OSError('rename refused')))
    with pytest.raises(desktop.NativeShellNotFound):
        install_fixture(monkeypatch, tmp_path, 'linux', b'new verified executable')
    assert previous.read_bytes() == b'old'
    assert list(previous.parent.iterdir()) == [previous]


def test_windows_installer_is_current_user_and_verifies_explicit_destination(tmp_path, monkeypatch):
    local = tmp_path / 'redirected local appdata with spaces'
    monkeypatch.setenv('LOCALAPPDATA', str(local))
    expected = local / 'Programs/Tessera/tessera-desktop.exe'
    calls = []
    def run(command, **kwargs):
        calls.append((command, kwargs))
        installer = Path(command.split(' /S /D=', 1)[0].strip(chr(34)))
        assert installer.suffix == '.exe'
        assert installer.read_bytes() == b'verified NSIS installer'
        assert command == subprocess.list2cmdline([str(installer), '/S']) + ' /D=' + str(expected.parent)
        assert kwargs['stdin'] == subprocess.DEVNULL
        assert kwargs['timeout'] == 17
        expected.parent.mkdir(parents=True)
        expected.write_bytes(b'installed native shell')
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(desktop.subprocess, 'run', run)
    binary = install_fixture(monkeypatch, tmp_path, 'win32', b'verified NSIS installer')
    assert binary == expected
    assert not Path(calls[0][0].split(' /S /D=', 1)[0].strip(chr(34))).exists(), 'downloaded installer must be removed'
    config = json.loads((Path(__file__).parents[1] / 'desktop/tauri.conf.json').read_text())
    assert config['bundle']['windows']['nsis']['installMode'] == 'currentUser'


@pytest.mark.parametrize('outcome', ['failure', 'no-binary', 'timeout', 'spawn-error'])
def test_windows_install_failures_are_recoverable_and_remove_installer(tmp_path, monkeypatch, outcome):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'local'))
    launched = []
    def run(command, **kwargs):
        launched.append(Path(command.split(' /S /D=', 1)[0].strip(chr(34))))
        if outcome == 'timeout':
            raise subprocess.TimeoutExpired(command, kwargs['timeout'])
        if outcome == 'spawn-error':
            raise OSError('cannot launch installer')
        return SimpleNamespace(returncode=9 if outcome == 'failure' else 0)
    monkeypatch.setattr(desktop.subprocess, 'run', run)
    with pytest.raises(desktop.NativeShellNotFound):
        install_fixture(monkeypatch, tmp_path, 'win32', b'verified NSIS installer')
    assert launched and not launched[0].exists()


def test_non_utf8_sidecar_is_recoverable(monkeypatch):
    monkeypatch.setattr(desktop, '_fetch', lambda *_args, **_kwargs: b'\xff')
    with pytest.raises(desktop.NativeShellNotFound, match='checksum sidecar'):
        desktop._fetch_checksum('https://example.test/bundle', timeout=2)
