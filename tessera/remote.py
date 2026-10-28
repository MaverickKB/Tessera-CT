"""Remote machine tools over the system OpenSSH client.

Each configured [remotes.<name>] table names a host, port, user and key. Commands run
through `ssh` with batch mode, a pinned known_hosts file and no local shell. The tools
are registered on the session's ToolRegistry, so the approval policy (ask, deny_risky,
yolo) applies to remote commands and writes exactly as it does to local ones.
"""

from contextlib import nullcontext
import os
from pathlib import Path
import shlex
import signal
import subprocess
import tempfile

from .tools import clip_output, object_schema


def ssh_argv(remote, home):
    def local(path):
        path = Path(path).expanduser()
        return str(path if path.is_absolute() else Path(home) / path)
    argv = list(remote.ssh_command) + ['-p', str(remote.port), '-o', 'BatchMode=yes',
                                       '-o', f'ConnectTimeout={remote.connect_timeout}',
                                       '-o', f'StrictHostKeyChecking={remote.strict_host_key_checking}']
    if remote.identity_file:
        argv += ['-i', local(remote.identity_file), '-o', 'IdentitiesOnly=yes']
    if remote.known_hosts_file:
        argv += ['-o', 'UserKnownHostsFile=' + local(remote.known_hosts_file)]
    return argv + [f'{remote.user}@{remote.host}', '--']


def run_remote(remote, home, command, timeout, clip_chars, stdin_text=None, registry=None):
    argv = ssh_argv(remote, home) + [command]
    with tempfile.TemporaryFile() as output:
        from .procutil import kill_group, popen
        process = popen(argv, stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
                        stdout=output, stderr=subprocess.STDOUT)
        def cancel():
            try:
                kill_group(process.pid)
            except ProcessLookupError:
                pass

        timed_out = False
        try:
            with registry.cancellation(cancel) if registry is not None else nullcontext():
                process.communicate(stdin_text.encode('utf-8') if stdin_text is not None else None, timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
        except BaseException:
            cancel()
            process.wait()
            raise
        if timed_out:
            try:
                kill_group(process.pid)
            except ProcessLookupError:
                pass
            process.wait()
        output.seek(0)
        text = output.read().decode('utf-8', errors='replace')
    return {'exit_code': process.returncode, 'timed_out': timed_out, 'output': clip_output(text, clip_chars)}


def register_remote_tools(registry, remotes, home, clip_chars=30000):
    enabled = {name: remote for name, remote in remotes.items() if remote.enabled}
    if not enabled:
        return
    names = sorted(enabled)
    listing = '; '.join(f'{name} ({enabled[name].description or enabled[name].host})' for name in names)
    host = {'type': 'string', 'enum': names}
    string = {'type': 'string'}

    def pick(name):
        if name not in enabled:
            raise ValueError(f'Unknown remote: {name}')
        return enabled[name]

    def remote_shell(remote, command, timeout=None):
        settings = pick(remote)
        limit = settings.timeout if timeout is None else timeout
        if limit <= 0:
            raise ValueError('timeout must be positive')
        return run_remote(settings, home, command, limit, clip_chars, registry=registry)

    def remote_read_file(remote, path, offset=0, limit=clip_chars):
        settings = pick(remote)
        result = run_remote(settings, home, 'cat -- ' + shlex.quote(path), settings.timeout, clip_chars * 4, registry=registry)
        if result['exit_code'] != 0 or result['timed_out']:
            raise RuntimeError('remote read failed: ' + result['output'][-500:])
        return clip_output(result['output'][offset:], limit)

    def remote_write_file(remote, path, text):
        settings = pick(remote)
        quoted = shlex.quote(path)
        command = f'mkdir -p -- "$(dirname -- {quoted})" && cat > {quoted}'
        result = run_remote(settings, home, command, settings.timeout, clip_chars, stdin_text=text, registry=registry)
        if result['exit_code'] != 0 or result['timed_out']:
            raise RuntimeError('remote write failed: ' + result['output'][-500:])
        return f'Wrote {len(text.encode("utf-8"))} bytes to {remote}:{path}'

    registry.register('remote_shell', 'Run a shell command on a configured remote machine over SSH. '
                      'Remotes: ' + listing + '. Requires approval under ask.',
                      object_schema({'remote': host, 'command': string, 'timeout': {'type': 'number', 'minimum': 0.001}},
                                    ['remote', 'command']), remote_shell, risky=True)
    registry.register('remote_read_file', 'Read a UTF-8 text file on a configured remote machine over SSH. Remotes: ' + listing + '.',
                      object_schema({'remote': host, 'path': string, 'offset': {'type': 'integer', 'minimum': 0},
                                     'limit': {'type': 'integer', 'minimum': 1}}, ['remote', 'path']), remote_read_file)
    registry.register('remote_write_file', 'Write UTF-8 text to a file on a configured remote machine over SSH, creating parents. '
                      'Remotes: ' + listing + '. Requires approval under ask.',
                      object_schema({'remote': host, 'path': string, 'text': string}, ['remote', 'path', 'text']),
                      remote_write_file, risky=True)
