"""Native launch retains operator selection and only accepts a real ready window."""
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import Mock

import pytest

from tessera import cli, desktop_install as desktop, terminal
from tessera.config import load_config

@pytest.fixture(autouse=True)
def _bootstrap_isolation(tmp_path, monkeypatch):
    monkeypatch.setattr(desktop, 'bootstrap_path', lambda: tmp_path / 'desktop-bootstrap.json')



@pytest.mark.parametrize('platform', ['linux', 'darwin'])
def test_posix_launch_preserves_selected_home_interpreter_and_port(tmp_path, monkeypatch, platform):
    executable = tmp_path / 'Tessera.app/Contents/MacOS/tessera-desktop'
    monkeypatch.setattr(desktop, 'find_binary', lambda: executable)
    monkeypatch.setattr(desktop.sys, 'platform', platform)
    monkeypatch.setenv('TESSERA_DESKTOP_HOME', str(tmp_path / 'stale-home'))
    monkeypatch.delenv('TESSERA_DESKTOP_PYTHON', raising=False)
    monkeypatch.delenv('TESSERA_DESKTOP_CORE_ROOT', raising=False)
    selected = tmp_path / 'private-home'
    workspace = tmp_path / 'caller-workspace'
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    calls = []
    def execute(binary, env, timeout):
        calls.append((binary, env, timeout))
    monkeypatch.setattr(desktop, '_launch_native', execute)
    desktop.launch(selected, port=9145, startup_timeout=5)
    binary, env, timeout = calls[0]
    assert binary == executable and timeout == 5
    assert env['TESSERA_HOME'] == str(selected)
    assert env['TESSERA_DESKTOP_HOME'] == str(selected)
    assert env['TESSERA_DESKTOP_PYTHON'] == sys.executable
    assert env['TESSERA_DESKTOP_CORE_ROOT'] == str(workspace)
    assert env['TESSERA_DESKTOP_CORE_PORT'] == '9145'


def test_windows_locator_uses_localappdata(tmp_path, monkeypatch):
    executable = tmp_path / 'relocated/Programs/Tessera/tessera-desktop.exe'
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b'fixture')
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'relocated'))
    monkeypatch.delenv('TESSERA_DESKTOP_BIN', raising=False)
    monkeypatch.setattr(desktop.shutil, 'which', lambda _: None)
    monkeypatch.setattr(desktop.sys, 'platform', 'win32')
    assert desktop.find_binary() == executable


def test_native_launch_waits_for_real_window_ready_and_forwards_proof(monkeypatch, capsys):
    processes = []
    original = subprocess.Popen
    event = {'event': 'tessera-desktop.window-ready', 'shell_pid': 21, 'core_pid': 22}
    script = 'import time; print(' + repr(json.dumps(event)) + ', flush=True); time.sleep(30)'
    def start(argv, **kwargs):
        if argv[0] == '/bin/ps':
            return original(argv, **kwargs)
        process = original([sys.executable, '-u', '-c', script], **kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr(desktop.subprocess, 'Popen', start)
    try:
        desktop._launch_native(Path(sys.executable), os.environ.copy(), 5)
        assert processes[0].poll() is None
        assert json.loads(capsys.readouterr().out) == {**event, 'native_owner_pid': processes[0].pid}
    finally:
        for process in processes:
            process.terminate()
            process.wait(timeout=5)


@pytest.mark.parametrize('script', ['raise SystemExit(2)', 'import time; time.sleep(30)'])
def test_native_launch_failure_and_timeout_leave_no_detached_shell(monkeypatch, script):
    processes = []
    original = subprocess.Popen
    def start(argv, **kwargs):
        if argv[0] == '/bin/ps':
            return original(argv, **kwargs)
        process = original([sys.executable, '-u', '-c', script], **kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr(desktop.subprocess, 'Popen', start)
    with pytest.raises(desktop.NativeShellNotFound, match='did not become ready'):
        desktop._launch_native(Path(sys.executable), os.environ.copy(), 0.2)
    assert processes[0].poll() is not None


@pytest.mark.parametrize('surface', ['native', 'browser', 'web', 'headless', 'download-failure', 'launch-failure'])
def test_public_start_surface_and_recovery_do_not_duplicate_a_core(tmp_path, monkeypatch, surface):
    (tmp_path / 'config.toml').write_text('')
    monkeypatch.setattr(cli, '_placeholder_memory_projects', lambda _: [])
    monkeypatch.setattr('tessera.bundled_engine.ensure_local_engine_bundle', lambda _: None)
    monkeypatch.setattr('tessera.seeds.ensure', lambda _: {})
    monkeypatch.setattr('tessera.updates.install_report', lambda: None)
    monkeypatch.setattr('tessera.server._release_embedded_engine', lambda _: None)
    find = Mock(return_value=None if surface == 'download-failure' else tmp_path / 'native')
    monkeypatch.setattr(desktop, 'find_binary', find)
    install = Mock(side_effect=desktop.NativeShellNotFound('offline'))
    monkeypatch.setattr(desktop, 'ensure_installed', install)
    launch = Mock(side_effect=desktop.NativeShellNotFound('bad executable') if surface == 'launch-failure' else None)
    monkeypatch.setattr(desktop, 'launch', launch)
    server = Mock(url='http://127.0.0.1:9234', server_address=('127.0.0.1', 9234))
    server.serve_forever.side_effect = KeyboardInterrupt
    create = Mock(return_value=server)
    monkeypatch.setattr('tessera.server.create_server', create)
    thread = Mock()
    monkeypatch.setattr('threading.Thread', thread)
    args = ['--home', str(tmp_path), 'start', '--port', '9234']
    args += {'browser': ['--browser'], 'web': ['--web'], 'headless': ['--no-browser']}.get(surface, [])
    assert terminal.main(args) == 0
    if surface == 'native':
        launch.assert_called_once_with(tmp_path, port=9234, startup_timeout=60.0)
        create.assert_not_called()
        thread.assert_not_called()
    else:
        create.assert_called_once()
        assert create.call_args.args[0].server.port == 9234
        assert thread.call_count == (0 if surface == 'headless' else 1)
        if surface in ('browser', 'web', 'headless'):
            find.assert_not_called()
            launch.assert_not_called()


def test_public_installer_dispatch_is_available(capsys):
    with pytest.raises(SystemExit) as exit:
        terminal.main(['install-desktop', '--help'])
    assert exit.value.code == 0
    assert '--release-url' in capsys.readouterr().out


def test_startup_timeout_loads_from_toml_and_environment(tmp_path, monkeypatch):
    (tmp_path / 'config.toml').write_text('desktop_startup_timeout = 91.0\n')
    assert load_config(tmp_path).desktop_startup_timeout == 91.0
    monkeypatch.setenv('TESSERA_DESKTOP_STARTUP_TIMEOUT', '12.5')
    assert load_config(tmp_path).desktop_startup_timeout == 12.5


def test_explicit_macos_app_resolves_its_actual_executable(tmp_path, monkeypatch):
    app = tmp_path / 'Chosen.app'
    binary = app / 'Contents/MacOS/tessera-desktop'
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b'fixture')
    monkeypatch.setenv('TESSERA_DESKTOP_BIN', str(app))
    assert desktop.find_binary() == binary


@pytest.mark.skipif(os.name == 'nt', reason='POSIX process-group ownership')
def test_failed_posix_shell_cannot_leave_its_core_child_running(tmp_path, monkeypatch):
    import time
    child_pid = tmp_path / 'child.pid'
    child_script = 'import time; time.sleep(30)'
    script = ('import subprocess, sys; from pathlib import Path; '
              'child = subprocess.Popen([sys.executable, "-c", ' + repr(child_script) + ']); '
              'Path(' + repr(str(child_pid)) + ').write_text(str(child.pid)); raise SystemExit(3)')
    original = subprocess.Popen
    def start(argv, **kwargs):
        if argv[0] == '/bin/ps':
            return original(argv, **kwargs)
        return original([sys.executable, '-u', '-c', script], **kwargs)
    monkeypatch.setattr(desktop.subprocess, 'Popen', start)
    with pytest.raises(desktop.NativeShellNotFound):
        desktop._launch_native(Path(sys.executable), os.environ.copy(), 0.5)
    monkeypatch.setattr(desktop.subprocess, 'Popen', original)
    pid = int(child_pid.read_text())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        status = subprocess.run(['ps', '-o', 'stat=', '-p', str(pid)], capture_output=True, text=True)
        if not status.stdout.strip() or status.stdout.strip().startswith('Z'):
            break
        time.sleep(0.05)
    else:
        pytest.fail('Failed native launch left its core child running')


@pytest.mark.skipif(os.name == 'nt', reason='POSIX executable fixture')
def test_public_start_recovers_after_real_native_process_exits(tmp_path, monkeypatch):
    executable = tmp_path / 'failed-native'
    executable.write_text('#!/bin/sh\nexit 3\n')
    executable.chmod(0o755)
    (tmp_path / 'config.toml').write_text('desktop_startup_timeout = 1\n')
    monkeypatch.setenv('TESSERA_DESKTOP_BIN', str(executable))
    monkeypatch.setattr(cli, '_placeholder_memory_projects', lambda _: [])
    monkeypatch.setattr('tessera.bundled_engine.ensure_local_engine_bundle', lambda _: None)
    monkeypatch.setattr('tessera.seeds.ensure', lambda _: {})
    monkeypatch.setattr('tessera.updates.install_report', lambda: None)
    monkeypatch.setattr('tessera.server._release_embedded_engine', lambda _: None)
    monkeypatch.setattr('urllib.request.urlopen', lambda *args, **kwargs: None)
    browser = Mock()
    monkeypatch.setattr('webbrowser.open', browser)
    server = Mock(url='http://127.0.0.1:9234', server_address=('127.0.0.1', 9234))
    server.serve_forever.side_effect = KeyboardInterrupt
    create = Mock(return_value=server)
    monkeypatch.setattr('tessera.server.create_server', create)
    assert terminal.main(['--home', str(tmp_path), 'start']) == 0
    create.assert_called_once()
