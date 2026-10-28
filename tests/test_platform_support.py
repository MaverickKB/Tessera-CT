import errno
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

from fake_backend import FakeBackend
from tessera.bundled_engine import ensure_bundled_engine
from tessera.config import Config
from tessera.credentials import CredentialError, CredentialResolver
from tessera.durability import open_regular
from tessera.host_cua import UnavailableCua, select_backend
from tessera.mcp_servers import computer_backend
from tessera.platform_support import host_report
from tessera.sessions import SessionManager


ROOT = Path(__file__).resolve().parents[1]
USER_HOME = '/'.join(['', 'Users', ''])


def _service():
    spec = importlib.util.spec_from_file_location('tessera_service', ROOT / 'scripts' / 'service.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_service_label_honors_tessera_core_label(monkeypatch):
    monkeypatch.setenv('TESSERA_CORE_LABEL', 'tessera.custom.label')
    assert _service().LABEL == 'tessera.custom.label'


def test_service_label_defaults_when_unset(monkeypatch):
    monkeypatch.delenv('TESSERA_CORE_LABEL', raising=False)
    assert _service().LABEL == 'app.tessera.core'


def test_this_process_names_its_own_limits():
    report = host_report()
    text = ' '.join(report['limits'])
    if sys.platform.startswith('linux'):
        assert report['platform'] == 'linux'
        assert 'not available on Linux' in text
        assert 'secret-tool' in text
        assert 'Windows Credential Manager' not in text
        assert 'under WSL' not in text
        assert 'embedded memory' not in text
    elif sys.platform == 'darwin':
        assert report['platform'] == 'macos'
        assert 'macOS keychain' in text
        assert 'LaunchAgent plist' in text
        assert 'under WSL' not in text
    elif sys.platform.startswith('win'):
        assert report['platform'] == 'windows'
        assert 'not available on Windows' in text
        assert 'does not install a Windows service' in text
        assert 'Windows Credential Manager is not used' in text
        assert 'not supported on Windows' in text
        assert 'under WSL' in text
        assert 'not yet supported for embedded memory' in text
    else:
        assert report['platform'] == sys.platform
        assert 'this operating system' in text


def test_other_platform_copy_names_what_is_absent(monkeypatch):
    monkeypatch.setattr(sys, 'platform', 'win32')
    windows = ' '.join(host_report()['limits'])
    assert 'not available on Windows' in windows
    assert 'does not install a Windows service' in windows
    assert 'Windows Credential Manager is not used' in windows
    assert 'not supported on Windows' in windows
    assert 'under WSL' in windows
    assert 'not yet supported for embedded memory' in windows
    monkeypatch.setattr(sys, 'platform', 'darwin')
    macos = ' '.join(host_report()['limits'])
    assert 'macOS keychain' in macos
    assert 'LaunchAgent plist' in macos
    assert 'under WSL' not in macos
    monkeypatch.setattr(sys, 'platform', 'linux')
    linux = ' '.join(host_report()['limits'])
    assert 'not available on Linux' in linux
    assert 'under WSL' not in linux


def test_status_surfaces_a_delivery_detail_and_not_the_journal(tmp_path):
    config = Config(home=tmp_path, cwd=tmp_path)
    config.jev.enabled = False
    bundle = tmp_path / 'no-engine'
    bundle.mkdir()
    config.aethermind.bundled_path = str(bundle)
    detail = 'AetherMind write did not acknowledge success: remote store body'
    with SessionManager(config, backend_factory=lambda slug, name: FakeBackend(['ok'])) as manager:
        slug = manager.create('Delivery')['slug']
        session = manager.get(slug)
        session.capture.service_error = 'McpError'
        session.capture._delivery_errors['write'] = 'McpError: ' + detail
        report = manager.status()['aethermind']
        assert report['last_error'] == 'McpError'
        assert 'remote store body' not in str(report)
        assert detail in session.capture._delivery_errors['write']
        journal = session.root / 'outbox' / 'outbox.jsonl'
        if journal.is_file():
            assert 'remote store body' not in journal.read_text(encoding='utf-8')


def test_status_keeps_host_on_the_connection_poll(tmp_path):
    config = Config(home=tmp_path, cwd=tmp_path)
    config.jev.enabled = False
    bundle = tmp_path / 'no-engine'
    bundle.mkdir()
    config.aethermind.bundled_path = str(bundle)
    assert ensure_bundled_engine(config) is None
    with SessionManager(config) as manager:
        full = manager.status()
        narrow = manager.status(connection=True)
    assert full['host'] == host_report()
    assert narrow['host'] == full['host']
    assert narrow['host']['limits']


def test_windows_computer_use_reports_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, 'platform', 'win32')
    config = Config(home=tmp_path, cwd=tmp_path)
    assert computer_backend(config) == 'unsupported'
    backend = select_backend(config)
    assert isinstance(backend, UnavailableCua)
    assert 'not supported on this operating system' in backend.screenshot()


def test_open_regular_refuses_a_symlink(tmp_path, monkeypatch):
    target = tmp_path / 'regular'
    target.write_text('kept', encoding='utf-8')
    link = tmp_path / 'link'
    link.symlink_to(target)
    with pytest.raises(OSError) as caught:
        open_regular(link)
    assert caught.value.errno == errno.ELOOP
    monkeypatch.setattr(os, 'name', 'nt')
    with pytest.raises(OSError) as caught:
        open_regular(link)
    assert caught.value.errno == errno.ELOOP


def test_windows_keychain_says_unsupported_and_does_not_query(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError('no credential helper should run')
    resolver = CredentialResolver({'work': {
        'keychain_service': 'example-service',
        'keychain_account': 'example-account',
    }}, environ={}, platform='win32', which=lambda name: 'secret-tool', run=refuse)
    with pytest.raises(CredentialError, match='unsupported on this OS'):
        resolver.resolve('work')


def test_linux_unit_uses_the_given_home():
    service = _service()
    text = service.linux_unit('/usr/bin/python3', '/var/tmp/tessera-home', '/var/tmp/tessera-cwd', None, None)
    assert 'ExecStart=/usr/bin/python3 -m tessera serve' in text
    assert 'TESSERA_HOME=/var/tmp/tessera-home' in text
    assert 'WorkingDirectory=/var/tmp/tessera-cwd' in text
    assert USER_HOME not in text


def test_linux_unit_records_a_given_users_executable():
    service = _service()
    executable = '/Users/example/venv/bin/python3'
    text = service.linux_unit(executable, '/var/tmp/tessera-home', '/var/tmp/tessera-cwd', None, None)
    assert f'ExecStart={executable} -m tessera serve' in text
    assert 'TESSERA_HOME=/var/tmp/tessera-home' in text
    assert 'WorkingDirectory=/var/tmp/tessera-cwd' in text
    assert 'TESSERA_HOME=' + USER_HOME not in text
    assert 'WorkingDirectory=' + USER_HOME not in text


def _systemd_environment_value(line):
    word = line.removeprefix('Environment=')
    assert word.startswith('"') and word.endswith('"'), line
    inner = word[1:-1]
    unquoted = []
    index = 0
    escapes = {'\\': '\\', '"': '"', 'n': '\n', 'r': '\r', 't': '\t'}
    while index < len(inner):
        char = inner[index]
        if char == '\\':
            unquoted.append(escapes[inner[index + 1]])
            index += 2
            continue
        unquoted.append(char)
        index += 1
    specified = []
    text = ''.join(unquoted)
    index = 0
    while index < len(text):
        if text[index] == '%':
            assert text[index + 1] == '%', text
            specified.append('%')
            index += 2
            continue
        specified.append(text[index])
        index += 1
    name, value = ''.join(specified).split('=', 1)
    assert name == 'TESSERA_HOME'
    return value


def test_linux_unit_quotes_a_home_path_that_contains_spaces(tmp_path):
    service = _service()
    home = '/var/tmp/tessera home "odd"\\share%h'
    text = service.linux_unit(sys.executable, home, '/var/tmp/tessera-cwd', None, None)
    line = next(item for item in text.splitlines() if item.startswith('Environment='))
    assert _systemd_environment_value(line) == home
    assert f'Environment=TESSERA_HOME={home}' not in text
    analyzer = shutil.which('systemd-analyze')
    if analyzer is not None:
        unit = tmp_path / 'tessera-core.service'
        unit.write_text(text, encoding='utf-8')
        checked = subprocess.run([analyzer, 'verify', str(unit)], capture_output=True, text=True)
        assert 'Invalid environment assignment' not in checked.stderr
        assert 'Invalid syntax' not in checked.stderr


def test_linux_install_refuses_to_replace_a_unit_unless_forced(tmp_path, monkeypatch, capsys):
    service = _service()
    monkeypatch.setattr(service.sys, 'platform', 'linux')
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'config'))

    def refuse(*args, **kwargs):
        raise AssertionError(args)

    monkeypatch.setattr(service.subprocess, 'run', refuse)
    home = tmp_path / 'home'
    home.mkdir()
    target = tmp_path / 'config' / 'systemd' / 'user' / 'tessera-core.service'
    target.parent.mkdir(parents=True)
    target.write_text('original-unit\n', encoding='utf-8')
    args = SimpleNamespace(home=str(home), cwd=str(home), python='/usr/bin/python3',
                           host=None, port=None, start=False, force=False, label='unused')
    assert service.install(args) == 2
    assert target.read_text(encoding='utf-8') == 'original-unit\n'
    err = capsys.readouterr().err
    assert 'already exists' in err
    assert '--force' in err
    args.force = True
    assert service.install(args) == 0
    written = target.read_text(encoding='utf-8')
    assert written != 'original-unit\n'
    assert 'ExecStart=/usr/bin/python3 -m tessera serve' in written
    assert 'service: wrote' in capsys.readouterr().out


def test_windows_service_install_refuses(monkeypatch, capsys):
    service = _service()
    monkeypatch.setattr(service.sys, 'platform', 'win32')
    code = service.install(object())
    assert code == 2
    err = capsys.readouterr().err
    assert 'does not install a Windows service' in err
    assert 'python -m tessera serve' in err


def test_launchagent_scripts_refuse_without_a_live_home_label_or_port(tmp_path):
    """Source the scripts under shell functions, not the real service commands.

    A fake ``uname`` earlier on ``PATH`` is not enough on Windows: Git Bash
    resolves ``uname.exe`` and never runs the fake. Functions defined in the
    same shell hide ``uname``, ``launchctl``, ``lsof``, and ``id`` before the
    script body runs.
    """
    home = tmp_path / 'home'
    home.mkdir()
    shell = shutil.which('sh') or shutil.which('bash')
    assert shell, 'a POSIX shell is required to run the maintenance scripts'
    env = os.environ.copy()
    env['HOME'] = home.as_posix()
    env['TESSERA_HOME'] = home.as_posix()
    env['TESSERA_PORT'] = '0'
    env['TESSERA_CORE_LABEL'] = 'tessera.refusal.isolated'
    env['TESSERA_RESTART_TIMEOUT'] = '1'
    env.pop('TESSERA_KEY', None)
    assert env['TESSERA_PORT'] != '8740'
    assert env['TESSERA_CORE_LABEL'] != 'app.tessera.core'
    log = tmp_path / 'restart-core.sh.log'
    log.write_text('', encoding='utf-8')
    env['TESSERA_TEST_COMMAND_LOG'] = log.as_posix()
    script = (ROOT / 'scripts' / 'restart-core.sh').as_posix()
    command = (
        'record() {\n'
        '  printf "%s\\n" "$1 $2" >> "$TESSERA_TEST_COMMAND_LOG"\n'
        '}\n'
        'uname() {\n'
        '  record uname "$*"\n'
        '  printf "%s\\n" Linux\n'
        '}\n'
        'launchctl() {\n'
        '  record launchctl "$*"\n'
        '  exit 99\n'
        '}\n'
        'lsof() {\n'
        '  record lsof "$*"\n'
        '  exit 99\n'
        '}\n'
        'id() {\n'
        '  record id "$*"\n'
        '  exit 99\n'
        '}\n'
        f'. "{script}"\n'
    )
    result = subprocess.run(
        [shell, '-c', command],
        capture_output=True, text=True, env=env, cwd=home)
    assert result.returncode == 2, result.stderr
    assert 'LaunchAgent' in result.stderr
    assert 'python -m tessera serve' in result.stderr
    assert 'app.tessera.core' not in result.stderr
    assert '8740' not in result.stderr
    recorded = log.read_text(encoding='utf-8')
    invoked = [line.split()[0] for line in recorded.splitlines() if line.strip()]
    assert invoked == ['uname'], recorded
    script = (ROOT / 'scripts' / 'restart-core.sh').read_text(encoding='utf-8')
    assert 'used to' not in script
    assert 'not available' in script
    assert 'foreground' in script
