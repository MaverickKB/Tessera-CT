import json
import sys

import pytest

from fake_backend import FakeBackend
from tessera.config import Config, RemoteConfig, load_config
from tessera.loop import Session
from tessera.remote import register_remote_tools, ssh_argv
from tessera.tools import ToolRegistry

FAKE_SSH = r'''
import json, os, subprocess, sys
args = sys.argv[1:]
with open(os.environ['FAKE_SSH_LOG'], 'a') as log:
    log.write(json.dumps(args) + '\n')
command = args[args.index('--') + 1:]
assert len(command) == 1, command
sys.exit(subprocess.call(['/bin/sh', '-c', command[0]]))
'''


@pytest.fixture
def fake_ssh(tmp_path, monkeypatch):
    script = tmp_path / 'fake_ssh.py'
    script.write_text(FAKE_SSH)
    log = tmp_path / 'ssh.log'
    monkeypatch.setenv('FAKE_SSH_LOG', str(log))
    return [sys.executable, str(script)], log


def remote(fake, **extra):
    values = dict(name='desk', host='192.0.2.10', port=2222, user='operator', identity_file='keys/id',
                  known_hosts_file='/pinned/known_hosts', ssh_command=fake, description='test desk')
    values.update(extra)
    return RemoteConfig(**values)


def test_ssh_argv_is_fully_configured_and_batch_only(tmp_path):
    argv = ssh_argv(remote(['ssh']), tmp_path)
    assert argv[:3] == ['ssh', '-p', '2222']
    assert 'BatchMode=yes' in argv and 'StrictHostKeyChecking=yes' in argv and 'IdentitiesOnly=yes' in argv
    assert argv[argv.index('-i') + 1] == str(tmp_path / 'keys' / 'id')
    assert 'UserKnownHostsFile=/pinned/known_hosts' in argv
    assert argv[-2:] == ['operator@192.0.2.10', '--']


def test_remote_tools_run_read_and_write_through_ssh(tmp_path, fake_ssh):
    fake, log = fake_ssh
    registry = ToolRegistry('yolo')
    register_remote_tools(registry, {'desk': remote(fake)}, tmp_path)
    result = registry.execute('remote_shell', {'remote': 'desk', 'command': 'echo hi; echo "$((2+3))"'})
    assert result == {'exit_code': 0, 'timed_out': False, 'output': 'hi\n5\n'}
    target = tmp_path / 'deep dir' / "it's.txt"
    assert 'Wrote 6 bytes' in registry.execute('remote_write_file', {'remote': 'desk', 'path': str(target), 'text': 'héllo'[:4] + '!'})
    assert target.read_text() == 'héll!'
    assert registry.execute('remote_read_file', {'remote': 'desk', 'path': str(target)}) == 'héll!'
    with pytest.raises(RuntimeError, match='remote read failed'):
        registry.execute('remote_read_file', {'remote': 'desk', 'path': str(tmp_path / 'missing')})
    assert registry.execute('remote_shell', {'remote': 'desk', 'command': 'sleep 5', 'timeout': 0.3})['timed_out'] is True
    with pytest.raises(ValueError):
        registry.execute('remote_shell', {'remote': 'other', 'command': 'true'})
    assert all('operator@192.0.2.10' in json.loads(line) for line in log.read_text().splitlines())


def test_remote_commands_follow_the_approval_policy(tmp_path, fake_ssh):
    fake, log = fake_ssh
    registry = ToolRegistry('deny_risky')
    register_remote_tools(registry, {'desk': remote(fake)}, tmp_path)
    with pytest.raises(PermissionError):
        registry.execute('remote_shell', {'remote': 'desk', 'command': 'touch x'})
    with pytest.raises(PermissionError):
        registry.execute('remote_write_file', {'remote': 'desk', 'path': str(tmp_path / 'x'), 'text': 'x'})
    assert not log.exists()
    asked = []
    registry = ToolRegistry('ask', approve=lambda name, args: asked.append(name) or True)
    register_remote_tools(registry, {'desk': remote(fake)}, tmp_path)
    assert registry.execute('remote_shell', {'remote': 'desk', 'command': 'echo ok'})['output'] == 'ok\n'
    assert asked == ['remote_shell']


def test_disabled_or_absent_remotes_register_nothing(tmp_path):
    registry = ToolRegistry()
    register_remote_tools(registry, {'desk': remote(['ssh'], enabled=False)}, tmp_path)
    assert registry.schemas() == []


def test_remotes_load_from_toml_validate_and_reach_the_session(tmp_path):
    (tmp_path / 'config.toml').write_text(
        '[jev]\n[aethermind]\n'
        '[remotes.desk]\nhost = "192.0.2.10"\nport = 2222\nuser = "operator"\nidentity_file = "~/.ssh/id_test"\n'
        'description = "Office desk"\n')
    config = load_config(tmp_path)
    assert config.remotes['desk'].port == 2222 and config.remotes['desk'].ssh_command == ['ssh']
    with Session(config, backend=FakeBackend([])) as session:
        names = [item['function']['name'] for item in session.tools.schemas()]
        assert {'remote_shell', 'remote_read_file', 'remote_write_file'} <= set(names)
    for bad in ('port = 0', 'user = "-oProxyCommand=x"', 'strict_host_key_checking = "no"', 'bogus = 1'):
        body = '[remotes.desk]\nhost = "192.0.2.10"\nuser = "operator"\n' + bad + '\n'
        if bad.startswith('user'):
            body = '[remotes.desk]\nhost = "192.0.2.10"\n' + bad + '\n'
        (tmp_path / 'config.toml').write_text(body)
        with pytest.raises(ValueError, match='remote'):
            load_config(tmp_path)
