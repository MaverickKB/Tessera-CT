"""Native OS entry points retain selected nonsecret context without rewriting settings."""
import json
import os
from pathlib import Path
import sys
from unittest.mock import Mock

import pytest

from tessera import desktop_install as desktop


@pytest.mark.parametrize('platform', ['linux', 'darwin', 'win32'])
def test_bootstrap_uses_platform_user_config_directory(tmp_path, monkeypatch, platform):
    monkeypatch.setattr(desktop.sys, 'platform', platform)
    environment = {'HOME': str(tmp_path / 'home'), 'USERPROFILE': str(tmp_path / 'profile'),
                   'APPDATA': str(tmp_path / 'roaming')}
    expected = (tmp_path / 'roaming' if platform == 'win32' else tmp_path / 'home' / '.config')
    assert desktop.bootstrap_path(environment) == expected / 'tessera' / 'desktop-bootstrap.json'


def test_bootstrap_preserves_exact_interpreter_and_operator_settings(tmp_path, monkeypatch):
    target = tmp_path / 'settings' / 'desktop-bootstrap.json'
    target.parent.mkdir()
    settings = target.parent / 'desktop.toml'
    original = 'keep_core = true\nattach_url = "http://127.0.0.1:8740"\nauth_token = "synthetic-private-key"\n'
    settings.write_text(original)
    monkeypatch.setattr(desktop, 'bootstrap_path', lambda: target)
    interpreter = str(tmp_path / 'venv' / 'bin' / 'python')
    home = tmp_path / 'chosen-home'
    workspace = tmp_path / 'workspace'
    desktop.remember_install(home, interpreter, str(workspace))
    record = json.loads(target.read_text())
    assert record == {'home': str(home.resolve()), 'python': interpreter, 'core_root': str(workspace)}
    assert settings.read_text() == original
    assert 'synthetic-private-key' not in target.read_text()
    if os.name != 'nt':
        assert target.stat().st_mode & 0o777 == 0o600


def test_failed_bootstrap_replacement_preserves_previous_selection(tmp_path, monkeypatch):
    target = tmp_path / 'desktop-bootstrap.json'
    target.write_text('{"prior": "selection"}')
    monkeypatch.setattr(desktop, 'bootstrap_path', lambda: target)
    monkeypatch.setattr(desktop.os, 'replace', Mock(side_effect=PermissionError('Synthetic write refusal')))
    with pytest.raises(desktop.NativeShellNotFound, match='remember'):
        desktop.remember_install(tmp_path / 'new-home', sys.executable, str(tmp_path))
    assert target.read_text() == '{"prior": "selection"}'
    assert list(tmp_path.glob('.desktop-bootstrap-*')) == []


def test_native_handoff_records_context_before_window_start(tmp_path, monkeypatch):
    home = tmp_path / 'chosen-home'
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    target = tmp_path / 'settings' / 'desktop-bootstrap.json'
    monkeypatch.chdir(workspace)
    monkeypatch.setattr(desktop, 'bootstrap_path', lambda: target)
    monkeypatch.setattr(desktop, 'find_binary', lambda: tmp_path / 'native-shell')
    monkeypatch.delenv('TESSERA_DESKTOP_PYTHON', raising=False)
    monkeypatch.delenv('TESSERA_DESKTOP_CORE_ROOT', raising=False)
    monkeypatch.setenv('TESSERA_DESKTOP_HOME', str(tmp_path / 'stale-home'))

    def start(binary, environment, timeout):
        record = json.loads(target.read_text())
        assert record['home'] == environment['TESSERA_HOME'] == environment['TESSERA_DESKTOP_HOME'] == str(home)
        assert record['python'] == environment['TESSERA_DESKTOP_PYTHON'] == sys.executable
        assert record['core_root'] == environment['TESSERA_DESKTOP_CORE_ROOT'] == str(workspace)
        assert environment['TESSERA_DESKTOP_CORE_PORT'] == '9234'
        assert timeout == 9

    monkeypatch.setattr(desktop, '_launch_native', start)
    desktop.launch(home, port=9234, startup_timeout=9)


def test_explicit_interpreter_and_root_remain_authoritative(tmp_path, monkeypatch):
    target = tmp_path / 'desktop-bootstrap.json'
    monkeypatch.setattr(desktop, 'bootstrap_path', lambda: target)
    monkeypatch.setattr(desktop, 'find_binary', lambda: tmp_path / 'native-shell')
    monkeypatch.setattr(desktop, '_launch_native', Mock())
    monkeypatch.setenv('TESSERA_DESKTOP_PYTHON', str(tmp_path / 'chosen-python'))
    monkeypatch.setenv('TESSERA_DESKTOP_CORE_ROOT', str(tmp_path / 'chosen-root'))
    desktop.launch(tmp_path / 'chosen-home', startup_timeout=9)
    record = json.loads(target.read_text())
    assert record['python'] == str(tmp_path / 'chosen-python')
    assert record['core_root'] == str(tmp_path / 'chosen-root')
