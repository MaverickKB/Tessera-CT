"""Native shell locator, install, and launch handoff."""

import hashlib
import os
import stat
import sys
from pathlib import Path
from unittest import mock

import pytest

from tessera import native
from tessera.native import NativeShellNotFound, find_binary, install, launch


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv('TESSERA_DESKTOP_BIN', raising=False)
    monkeypatch.delenv('TESSERA_HOME', raising=False)


def test_find_binary_returns_none_when_nothing_is_installed(monkeypatch, tmp_path):
    monkeypatch.setenv('PATH', str(tmp_path))
    # .local/bin does not exist under the fake HOME either.
    monkeypatch.setattr(native.Path, 'home', lambda: tmp_path / 'home')
    assert find_binary() is None


def test_find_binary_honours_explicit_env_override(monkeypatch, tmp_path):
    fake = tmp_path / 'tessera-desktop'
    fake.write_bytes(b'x')
    fake.chmod(0o755)
    monkeypatch.setenv('TESSERA_DESKTOP_BIN', str(fake))
    assert find_binary() == fake


def test_find_binary_finds_local_bin_install(monkeypatch, tmp_path):
    fake = tmp_path / 'home' / '.local' / 'bin' / 'tessera-desktop'
    fake.parent.mkdir(parents=True)
    fake.write_bytes(b'x')
    fake.chmod(0o755)
    monkeypatch.setenv('PATH', str(tmp_path))
    monkeypatch.setattr(native.Path, 'home', lambda: tmp_path / 'home')
    assert find_binary() == fake


def test_find_binary_prefers_explicit_over_path(monkeypatch, tmp_path):
    explicit = tmp_path / 'explicit-desktop'
    explicit.write_bytes(b'x')
    on_path = tmp_path / 'tessera-desktop'
    on_path.write_bytes(b'x')
    monkeypatch.setenv('TESSERA_DESKTOP_BIN', str(explicit))
    monkeypatch.setenv('PATH', str(tmp_path))
    assert find_binary() == explicit


def test_launch_raises_when_no_binary(monkeypatch, tmp_path):
    monkeypatch.setenv('PATH', str(tmp_path))
    monkeypatch.setattr(native.Path, 'home', lambda: tmp_path / 'home')
    with pytest.raises(NativeShellNotFound):
        launch(tmp_path / 'home')


def test_launch_execs_binary_with_home(monkeypatch, tmp_path):
    fake = tmp_path / 'tessera-desktop'
    fake.write_bytes(b'x')
    fake.chmod(0o755)
    monkeypatch.setenv('TESSERA_DESKTOP_BIN', str(fake))
    calls = {}

    def fake_execve(path, args, env):
        calls['path'] = path
        calls['args'] = args
        calls['home_env'] = env.get('TESSERA_HOME')
        raise SystemExit(0)  # a real exec replaces the image and never returns

    monkeypatch.setattr(native.os, 'execve', fake_execve)
    with pytest.raises(SystemExit):
        launch(tmp_path / 'the-home')
    assert calls['path'] == str(fake)
    assert calls['args'] == [str(fake)]
    assert calls['home_env'] == str(tmp_path / 'the-home')


def test_install_verifies_checksum(tmp_path, monkeypatch):
    monkeypatch.setattr(native.Path, 'home', lambda: tmp_path)
    payload = b'bundle-bytes'
    digest = hashlib.sha256(payload).hexdigest()
    with mock.patch.object(native, '_fetch', return_value=payload) as fetch:
        install('https://example.test/tessera.AppImage', checksum=digest)
    written = tmp_path / '.local' / 'bin' / 'tessera-desktop'
    assert written.read_bytes() == payload
    assert written.stat().st_mode & stat.S_IXUSR
    fetch.assert_called_once_with('https://example.test/tessera.AppImage', timeout=600)


def test_install_rejects_bad_checksum(tmp_path, monkeypatch):
    monkeypatch.setattr(native.Path, 'home', lambda: tmp_path)
    with mock.patch.object(native, '_fetch', return_value=b'bundle-bytes'):
        with pytest.raises(NativeShellNotFound, match='checksum mismatch'):
            install('https://example.test/tessera.AppImage', checksum='0' * 64)
    assert not (tmp_path / '.local' / 'bin' / 'tessera-desktop').exists()


def test_install_skips_checksum_when_none(tmp_path, monkeypatch):
    monkeypatch.setattr(native.Path, 'home', lambda: tmp_path)
    with mock.patch.object(native, '_fetch', return_value=b'bytes'):
        install('https://example.test/tessera.AppImage')
    assert (tmp_path / '.local' / 'bin' / 'tessera-desktop').read_bytes() == b'bytes'


def test_platform_asset_suffix_names_this_host():
    suffix = native._platform_asset_suffix()
    if sys.platform.startswith('win'):
        assert suffix == '.exe'
    elif sys.platform == 'darwin':
        assert suffix == '.dmg'
    else:
        assert suffix == '.AppImage'


def test_release_asset_explicit_url_wins(monkeypatch):
    monkeypatch.setattr(native, '_package_version', lambda: '9.9.9')
    url, checksum = native._release_asset('https://x/foo.AppImage', 'abc')
    assert url == 'https://x/foo.AppImage'
    assert checksum == 'abc'


def test_release_asset_derives_from_provenance(monkeypatch):
    from tessera import updates
    # Derivation is the Linux AppImage path; pin the platform so the test runs
    # identically on macOS/Windows CI.
    monkeypatch.setattr(native.sys, 'platform', 'linux')
    monkeypatch.setattr(updates, 'install_report',
                        lambda: {'url': 'https://github.com/MaverickKB/Tessera-CT', 'kind': 'git-install'})
    monkeypatch.setattr(native, '_package_version', lambda: '0.1.1')
    monkeypatch.setattr(native, '_machine_arch', lambda: 'x86_64')
    monkeypatch.setattr(native, '_platform_tag', lambda: 'linux')
    monkeypatch.setattr(native, '_platform_asset_suffix', lambda: '.AppImage')
    url, checksum = native._release_asset(None, None)
    assert url == 'https://github.com/MaverickKB/Tessera-CT/releases/download/v0.1.1/tessera-linux-x86_64.AppImage'
    assert checksum is None


def test_release_asset_requires_origin(monkeypatch):
    from tessera import updates
    monkeypatch.setattr(native.sys, 'platform', 'linux')
    monkeypatch.setattr(updates, 'install_report', lambda: None)
    with pytest.raises(NativeShellNotFound, match='no install origin'):
        native._release_asset(None, None)


def test_release_asset_derivation_is_linux_only(monkeypatch):
    # Auto-derivation on a non-Linux host names a self-contained installer this
    # module cannot drop on PATH, so it stops before naming a download.
    monkeypatch.setattr(native.sys, 'platform', 'darwin')
    with pytest.raises(NativeShellNotFound, match='Linux AppImage'):
        native._release_asset(None, None)


def test_release_asset_explicit_url_bypasses_platform_gate(monkeypatch):
    # An explicit URL is honored before the Linux gate, so a manual bootstrap on
    # any platform still works.
    monkeypatch.setattr(native.sys, 'platform', 'darwin')
    url, checksum = native._release_asset('https://example.test/t.AppImage', None)
    assert url == 'https://example.test/t.AppImage'
    assert checksum is None


def test_ensure_installed_returns_existing_binary(monkeypatch, tmp_path):
    fake = tmp_path / 'tessera-desktop'
    fake.write_bytes(b'x')
    fake.chmod(0o755)
    monkeypatch.setenv('TESSERA_DESKTOP_BIN', str(fake))
    assert native.ensure_installed() == fake


def test_ensure_installed_downloads_when_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(native.Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(native, 'find_binary', lambda: None)
    monkeypatch.setenv('TESSERA_DESKTOP_RELEASE_URL', 'https://example.test/tessera.AppImage')
    monkeypatch.setattr(native, '_fetch_checksum', lambda url: None)
    with mock.patch.object(native, '_fetch', return_value=b'payload'):
        installed = native.ensure_installed()
    assert installed == tmp_path / '.local' / 'bin' / 'tessera-desktop'


def test_ensure_installed_verifies_sidecar_checksum(monkeypatch, tmp_path):
    payload = b'actual-payload'
    digest = hashlib.sha256(payload).hexdigest()
    monkeypatch.setattr(native.Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(native, 'find_binary', lambda: None)
    monkeypatch.setenv('TESSERA_DESKTOP_RELEASE_URL', 'https://example.test/t.AppImage')
    monkeypatch.setattr(native, '_fetch_checksum', lambda url: digest)
    with mock.patch.object(native, '_fetch', return_value=payload):
        installed = native.ensure_installed()
    assert installed == tmp_path / '.local' / 'bin' / 'tessera-desktop'


def test_fetch_checksum_reads_sidecar(monkeypatch):
    def fake(url, timeout):
        assert url.endswith('.sha256')
        return b'deadbeefcafe0123  tessera-linux-x86_64.AppImage\n'
    monkeypatch.setattr(native, '_fetch', fake)
    assert native._fetch_checksum('https://example.test/t.AppImage') == 'deadbeefcafe0123'


def test_fetch_checksum_returns_none_when_absent(monkeypatch):
    def fake(url, timeout):
        raise OSError('missing')
    monkeypatch.setattr(native, '_fetch', fake)
    assert native._fetch_checksum('https://example.test/t.AppImage') is None