"""Native shell locator, mandatory-checksum install, and launch handoff."""

import hashlib
import os
import stat
import sys
from pathlib import Path
from unittest import mock

import pytest

from tessera import desktop_install as native
from tessera.desktop_install import NativeShellNotFound, find_binary, install, launch


@pytest.fixture(autouse=True)
def _linux_platform_and_clean_env(monkeypatch):
    # The locate/install tests below assert the per-user Linux install layout
    # (~/.local/bin AppImage). Pin the platform so they run identically on the
    # macOS/Windows CI runners rather than exercising the .app/.msi branch with
    # a Linux-shaped fixture.
    monkeypatch.setattr(native.sys, 'platform', 'linux')
    monkeypatch.delenv('TESSERA_DESKTOP_BIN', raising=False)
    monkeypatch.delenv('TESSERA_HOME', raising=False)
    monkeypatch.delenv('TESSERA_DESKTOP_RELEASE_URL', raising=False)
    monkeypatch.delenv('TESSERA_DESKTOP_RELEASE_SHA256', raising=False)


def _fake_config(timeout=600.0):
    class C:
        desktop_install_timeout = timeout
    return C()


def test_find_binary_returns_none_when_nothing_is_installed(monkeypatch, tmp_path):
    monkeypatch.setenv('PATH', str(tmp_path))
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


def test_launch_execs_binary_with_home_on_posix(monkeypatch, tmp_path):
    if sys.platform.startswith('win') or sys.platform == 'darwin':
        pytest.skip('POSIX execve path')
    fake = tmp_path / 'tessera-desktop'
    fake.write_bytes(b'x')
    fake.chmod(0o755)
    monkeypatch.setenv('TESSERA_DESKTOP_BIN', str(fake))
    monkeypatch.setattr(native.sys, 'platform', 'linux')
    calls = {}

    def fake_execve(path, args, env):
        calls['path'] = path
        calls['args'] = args
        calls['home_env'] = env.get('TESSERA_HOME')
        raise SystemExit(0)

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
        install('https://example.test/tessera.AppImage', digest, timeout=600.0)
    written = tmp_path / '.local' / 'bin' / 'tessera-desktop'
    assert written.read_bytes() == payload
    assert written.stat().st_mode & stat.S_IXUSR
    fetch.assert_called_once_with('https://example.test/tessera.AppImage', timeout=600.0)


def test_install_rejects_bad_checksum(tmp_path, monkeypatch):
    monkeypatch.setattr(native.Path, 'home', lambda: tmp_path)
    payload = b'bundle-bytes'
    with mock.patch.object(native, '_fetch', return_value=payload):
        with pytest.raises(NativeShellNotFound, match='checksum mismatch'):
            install('https://example.test/tessera.AppImage', 'a' * 64, timeout=600.0)
    assert not (tmp_path / '.local' / 'bin' / 'tessera-desktop').exists()


def test_install_refuses_missing_or_malformed_checksum(tmp_path, monkeypatch):
    monkeypatch.setattr(native.Path, 'home', lambda: tmp_path)
    with mock.patch.object(native, '_fetch', return_value=b'payload') as fetch:
        for bad in (None, '', 'deadbeef', 'z' * 64, 'a' * 63, 'a' * 65):
            with pytest.raises(NativeShellNotFound, match='sha256'):
                install('https://example.test/t.AppImage', bad, timeout=600.0)
    # No file was ever written, and no download was even attempted for a bad digest.
    assert fetch.call_count == 0
    assert not (tmp_path / '.local' / 'bin' / 'tessera-desktop').exists()


def test_fetch_checksum_reads_sidecar(monkeypatch):
    def fake(url, timeout):
        assert url.endswith('.sha256')
        return ('deadbeef' * 8).encode()
    monkeypatch.setattr(native, '_fetch', fake)
    assert native._fetch_checksum('https://example.test/t.AppImage', timeout=30.0) == 'deadbeef' * 8


def test_fetch_checksum_refuses_absent_or_malformed(monkeypatch):
    def missing(url, timeout):
        raise NativeShellNotFound(f'could not download {url}: 404')
    monkeypatch.setattr(native, '_fetch', missing)
    with pytest.raises(NativeShellNotFound, match='could not download'):
        native._fetch_checksum('https://example.test/t.AppImage', timeout=30.0)
    # A sidecar that exists but is not a valid digest is still refused.
    monkeypatch.setattr(native, '_fetch', lambda url, timeout: b'not-a-hash\n')
    with pytest.raises(NativeShellNotFound, match='sha256'):
        native._fetch_checksum('https://example.test/t.AppImage', timeout=30.0)


def test_download_failure_is_normalized(tmp_path, monkeypatch):
    monkeypatch.setattr(native.Path, 'home', lambda: tmp_path)
    import urllib.error
    with mock.patch.object(native.urllib.request, 'urlopen',
                           side_effect=urllib.error.URLError('boom')):
        with pytest.raises(NativeShellNotFound, match='could not download'):
            install('https://example.test/t.AppImage', 'a' * 64, timeout=600.0)
    assert not (tmp_path / '.local' / 'bin' / 'tessera-desktop').exists()


def test_ensure_installed_returns_existing_binary(monkeypatch, tmp_path):
    fake = tmp_path / 'tessera-desktop'
    fake.write_bytes(b'x')
    fake.chmod(0o755)
    monkeypatch.setenv('TESSERA_DESKTOP_BIN', str(fake))
    assert native.ensure_installed(_fake_config()) == fake


def test_ensure_installed_requires_a_checksum(monkeypatch, tmp_path):
    monkeypatch.setattr(native.Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(native, 'find_binary', lambda: None)
    monkeypatch.setenv('TESSERA_DESKTOP_RELEASE_URL', 'https://example.test/t.AppImage')
    # No sidecar checksum, and none in the environment: fail closed, no install.
    monkeypatch.setattr(native, '_fetch_checksum',
                        mock.Mock(side_effect=NativeShellNotFound('sidecar missing')))
    with pytest.raises(NativeShellNotFound):
        native.ensure_installed(_fake_config())
    assert not (tmp_path / '.local' / 'bin' / 'tessera-desktop').exists()


def test_ensure_installed_installs_when_checksum_valid(monkeypatch, tmp_path):
    payload = b'actual-payload'
    digest = hashlib.sha256(payload).hexdigest()
    monkeypatch.setattr(native.Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(native, 'find_binary', lambda: None)
    monkeypatch.setenv('TESSERA_DESKTOP_RELEASE_URL', 'https://example.test/t.AppImage')
    monkeypatch.setattr(native, '_fetch_checksum', lambda url, timeout: digest)
    with mock.patch.object(native, '_fetch', return_value=payload):
        installed = native.ensure_installed(_fake_config())
    assert installed == tmp_path / '.local' / 'bin' / 'tessera-desktop'


def test_ensure_installed_prefers_env_checksum(monkeypatch, tmp_path):
    payload = b'payload'
    digest = hashlib.sha256(payload).hexdigest()
    monkeypatch.setattr(native.Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(native, 'find_binary', lambda: None)
    monkeypatch.setenv('TESSERA_DESKTOP_RELEASE_URL', 'https://example.test/t.AppImage')
    monkeypatch.setenv('TESSERA_DESKTOP_RELEASE_SHA256', digest)
    # sidecar must not even be fetched when the env supplies a valid digest.
    sidecar = mock.Mock()
    monkeypatch.setattr(native, '_fetch_checksum', sidecar)
    with mock.patch.object(native, '_fetch', return_value=payload):
        native.ensure_installed(_fake_config())
    sidecar.assert_not_called()


def test_release_asset_explicit_url_wins(monkeypatch):
    url, checksum = native._release_asset('https://x/foo.AppImage', 'abc')
    assert url == 'https://x/foo.AppImage'
    assert checksum == 'abc'


def test_release_asset_derives_from_provenance(monkeypatch):
    from tessera import updates
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


def test_timeout_comes_from_config_not_a_literal(monkeypatch):
    assert native._timeout(_fake_config(timeout=90.0), None) == 90.0
    assert native._timeout(_fake_config(), 5.0) == 5.0