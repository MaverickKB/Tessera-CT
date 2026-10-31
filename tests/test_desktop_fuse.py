"""Linux AppImage launch says when FUSE 2 is missing, and does not extract unless asked."""
import os
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from tessera import cli, desktop_install as desktop, terminal
from tessera.config import load_config


def _release(distro, version='', like=''):
    lines = [f'ID={distro}']
    if version:
        lines.append(f'VERSION_ID={version}')
    if like:
        lines.append(f'ID_LIKE={like}')
    return '\n'.join(lines) + '\n'


@pytest.mark.parametrize('text, command', [
    (_release('ubuntu', '24.04'), 'sudo apt install fuse'),
    (_release('ubuntu', '22.04'), 'sudo apt install libfuse2'),
    (_release('debian', '13'), 'sudo apt install fuse'),
    (_release('debian', '12'), 'sudo apt install libfuse2'),
    (_release('linuxmint', '22'), 'sudo apt install fuse'),
    (_release('linuxmint', '21.3'), 'sudo apt install libfuse2'),
    (_release('pop', '24.04', 'ubuntu debian'), 'sudo apt install fuse'),
    (_release('fedora', '40'), 'sudo dnf install fuse fuse-libs'),
    (_release('arch'), 'sudo pacman -S fuse2'),
])
def test_fuse2_install_command_is_one_line_for_the_distro(text, command):
    assert desktop.fuse2_install_command(text) == command


def test_unknown_distro_lists_the_known_fixes():
    text = desktop.fuse2_install_command(_release('slackware', '15.0'))
    assert 'sudo apt install fuse' in text
    assert 'sudo apt install libfuse2' in text
    assert 'sudo dnf install fuse fuse-libs' in text
    assert 'sudo pacman -S fuse2' in text


def test_fuse2_present_rejects_a_missing_binary():
    assert desktop.fuse2_present(lookup=lambda _name: None) is False


def test_fuse2_present_rejects_a_fusermount3_symlink(tmp_path):
    target = tmp_path / 'fusermount3'
    target.write_text('#!/bin/sh\necho fusermount3 version: 3.14.0\n')
    target.chmod(0o755)
    link = tmp_path / 'fusermount'
    link.symlink_to(target)
    assert desktop.fuse2_present(lookup=lambda name: str(link) if name == 'fusermount' else None) is False


def test_fuse2_present_accepts_fusermount_2(tmp_path):
    binary = tmp_path / 'fusermount'
    binary.write_text('#!/bin/sh\necho "fusermount version: 2.9.9"\n')
    binary.chmod(0o755)
    assert desktop.fuse2_present(lookup=lambda name: str(binary) if name == 'fusermount' else None) is True


@pytest.mark.parametrize('text', [
    'No suitable fusermount binary found on the $PATH\nCannot mount AppImage, please check your FUSE setup.\n',
    'Error: $FUSERMOUNT_PROG not set\n',
    'fuse: failed to exec fusermount: No such file or directory\n',
    'libfuse.so.2: cannot open shared object file\n',
])
def test_appimage_fuse_failure_recognizes_the_runtime_errors(text):
    assert desktop.appimage_fuse_failure(text) is True


def test_appimage_fuse_failure_ignores_other_launch_errors():
    assert desktop.appimage_fuse_failure('The native desktop window did not become ready') is False
    assert desktop.appimage_fuse_failure('') is False


def test_missing_fuse2_is_reported_and_does_not_launch(tmp_path, monkeypatch, capsys):
    binary = tmp_path / 'tessera-desktop'
    binary.write_bytes(b'AppImage')
    monkeypatch.setattr(desktop, 'find_binary', lambda: binary)
    monkeypatch.setattr(desktop, 'bootstrap_path', lambda: tmp_path / 'desktop-bootstrap.json')
    monkeypatch.setattr(desktop, 'fuse2_present', lambda: False)
    monkeypatch.setattr(desktop, 'fuse2_install_command', lambda _text=None: 'sudo apt install fuse')
    launch = Mock()
    monkeypatch.setattr(desktop, '_launch_native', launch)
    with pytest.raises(desktop.NativeShellNotFound, match='FUSE 2 is missing') as caught:
        desktop.launch(tmp_path, extract_appimage=False, startup_timeout=1)
    launch.assert_not_called()
    assert caught.value.fuse is True
    message = str(caught.value)
    assert 'sudo apt install fuse' in message
    assert 'will not run sudo' in message
    assert 'Extract-and-run is off' in message
    assert 'desktop_appimage_extract = true' in message
    assert capsys.readouterr().err == ''


def test_extract_setting_is_announced_and_passed_to_the_appimage(tmp_path, monkeypatch, capsys):
    binary = tmp_path / 'tessera-desktop'
    binary.write_bytes(b'AppImage')
    monkeypatch.setattr(desktop, 'find_binary', lambda: binary)
    monkeypatch.setattr(desktop, 'bootstrap_path', lambda: tmp_path / 'desktop-bootstrap.json')
    monkeypatch.setattr(desktop, 'fuse2_present', lambda: False)
    monkeypatch.setattr(desktop, 'fuse2_install_command', lambda _text=None: 'sudo apt install fuse')
    monkeypatch.setenv('APPIMAGE_EXTRACT_AND_RUN', '1')
    calls = []

    def native(_binary, env, _timeout):
        calls.append(dict(env))

    monkeypatch.setattr(desktop, '_launch_native', native)
    desktop.launch(tmp_path, extract_appimage=True, startup_timeout=1)
    assert calls[0]['APPIMAGE_EXTRACT_AND_RUN'] == '1'
    err = capsys.readouterr().err
    assert 'desktop_appimage_extract is on' in err
    assert 'desktop_appimage_extract = false' in err
    assert 'sudo apt install fuse' in err
    assert 'will not run sudo' in err


def test_extract_off_strips_a_stray_appimage_environment_variable(tmp_path, monkeypatch):
    binary = tmp_path / 'tessera-desktop'
    binary.write_bytes(b'not-an-appimage')
    monkeypatch.setattr(desktop, 'find_binary', lambda: binary)
    monkeypatch.setattr(desktop, 'bootstrap_path', lambda: tmp_path / 'desktop-bootstrap.json')
    monkeypatch.setattr(desktop, 'fuse2_present', lambda: True)
    monkeypatch.setenv('APPIMAGE_EXTRACT_AND_RUN', '1')
    calls = []
    monkeypatch.setattr(desktop, '_launch_native', lambda _binary, env, _timeout: calls.append(dict(env)))
    desktop.launch(tmp_path, extract_appimage=False, startup_timeout=1)
    assert 'APPIMAGE_EXTRACT_AND_RUN' not in calls[0]


def test_runtime_fuse_error_is_explained_without_a_second_launch(tmp_path, monkeypatch, capsys):
    binary = tmp_path / 'tessera-desktop'
    binary.write_text('#!/bin/sh\necho "Cannot mount AppImage, please check your FUSE setup." >&2\nexit 1\n')
    binary.chmod(0o755)
    monkeypatch.setattr(desktop, 'find_binary', lambda: binary)
    monkeypatch.setattr(desktop, 'bootstrap_path', lambda: tmp_path / 'desktop-bootstrap.json')
    monkeypatch.setattr(desktop, 'fuse2_present', lambda: True)
    monkeypatch.setattr(desktop, '_looks_like_appimage', lambda _path: False)
    monkeypatch.setattr(desktop, 'fuse2_install_command', lambda _text=None: 'sudo dnf install fuse fuse-libs')
    with pytest.raises(desktop.NativeShellNotFound, match='FUSE 2 is missing') as caught:
        desktop.launch(tmp_path, extract_appimage=False, startup_timeout=2)
    assert caught.value.fuse is True
    assert 'sudo dnf install fuse fuse-libs' in str(caught.value)
    assert 'Cannot mount AppImage' in capsys.readouterr().err


def test_runtime_fuse_error_retries_extract_once_when_enabled(tmp_path, monkeypatch, capsys):
    binary = tmp_path / 'tessera-desktop'
    binary.write_bytes(b'native')
    monkeypatch.setattr(desktop, 'find_binary', lambda: binary)
    monkeypatch.setattr(desktop, 'bootstrap_path', lambda: tmp_path / 'desktop-bootstrap.json')
    monkeypatch.setattr(desktop, 'fuse2_present', lambda: True)
    monkeypatch.setattr(desktop, '_looks_like_appimage', lambda _path: False)
    monkeypatch.setattr(desktop, 'fuse2_install_command', lambda _text=None: 'sudo pacman -S fuse2')
    calls = []

    def native(_binary, env, _timeout):
        calls.append(dict(env))
        if len(calls) == 1:
            raise desktop.NativeShellNotFound('The native desktop window did not become ready', fuse=True)

    monkeypatch.setattr(desktop, '_launch_native', native)
    desktop.launch(tmp_path, extract_appimage=True, startup_timeout=1)
    assert 'APPIMAGE_EXTRACT_AND_RUN' not in calls[0]
    assert calls[1]['APPIMAGE_EXTRACT_AND_RUN'] == '1'
    assert 'sudo pacman -S fuse2' in capsys.readouterr().err


def _quiet_start(monkeypatch):
    monkeypatch.setattr(cli, '_placeholder_memory_projects', lambda _: [])
    monkeypatch.setattr('tessera.bundled_engine.ensure_local_engine_bundle', lambda _: None)
    monkeypatch.setattr('tessera.seeds.ensure', lambda _: {})
    monkeypatch.setattr('tessera.updates.install_report', lambda: None)
    monkeypatch.setattr('tessera.server._release_embedded_engine', lambda _: None)


def test_start_names_the_fuse_fix_before_the_browser_fallback(tmp_path, monkeypatch, capsys):
    (tmp_path / 'config.toml').write_text('')
    _quiet_start(monkeypatch)
    monkeypatch.setattr(desktop, 'find_binary', lambda: tmp_path / 'tessera-desktop')
    monkeypatch.setattr(desktop, '_looks_like_appimage', lambda _path: True)
    monkeypatch.setattr(desktop, 'fuse2_present', lambda: False)
    monkeypatch.setattr(desktop, 'fuse2_install_command', lambda _text=None: 'sudo apt install fuse')
    server = Mock(url='http://127.0.0.1:9234', server_address=('127.0.0.1', 9234))
    server.serve_forever.side_effect = KeyboardInterrupt
    create = Mock(return_value=server)
    monkeypatch.setattr('tessera.server.create_server', create)
    monkeypatch.setattr('threading.Thread', Mock())
    assert terminal.main(['--home', str(tmp_path), 'start']) == 0
    err = capsys.readouterr().err
    assert 'FUSE 2 is missing' in err
    assert 'sudo apt install fuse' in err
    assert 'will not run sudo' in err
    assert 'Extract-and-run is off' in err
    assert 'opening the web UI instead' in err
    assert 'not a successful native launch' in err
    create.assert_called_once()


def test_start_with_extract_enabled_does_not_open_a_browser(tmp_path, monkeypatch, capsys):
    (tmp_path / 'config.toml').write_text('desktop_appimage_extract = true\n')
    assert load_config(tmp_path).desktop_appimage_extract is True
    _quiet_start(monkeypatch)
    monkeypatch.setattr(desktop, 'find_binary', lambda: tmp_path / 'tessera-desktop')
    monkeypatch.setattr(desktop, '_looks_like_appimage', lambda _path: True)
    monkeypatch.setattr(desktop, 'fuse2_present', lambda: False)
    monkeypatch.setattr(desktop, 'fuse2_install_command', lambda _text=None: 'sudo apt install fuse')
    calls = []
    monkeypatch.setattr(desktop, '_launch_native', lambda _binary, env, _timeout: calls.append(dict(env)))
    create = Mock()
    monkeypatch.setattr('tessera.server.create_server', create)
    assert terminal.main(['--home', str(tmp_path), 'start']) == 0
    assert calls[0]['APPIMAGE_EXTRACT_AND_RUN'] == '1'
    err = capsys.readouterr().err
    assert 'desktop_appimage_extract is on' in err
    assert 'opening the web UI instead' not in err
    create.assert_not_called()


def test_desktop_appimage_extract_defaults_off_and_loads_from_the_environment(tmp_path, monkeypatch):
    (tmp_path / 'config.toml').write_text('')
    assert load_config(tmp_path).desktop_appimage_extract is False
    monkeypatch.setenv('TESSERA_DESKTOP_APPIMAGE_EXTRACT', 'true')
    assert load_config(tmp_path).desktop_appimage_extract is True
    monkeypatch.setenv('TESSERA_DESKTOP_APPIMAGE_EXTRACT', 'false')
    assert load_config(tmp_path).desktop_appimage_extract is False
