#!/usr/bin/env python3
"""Run the Tessera core in the foreground, or write a platform supervisor file.

foreground is the path that works on every platform this script knows: it
replaces this process with ``python -m tessera serve``.

install writes a supervisor definition and does not start it unless --start
is given.

- Linux: a systemd user unit. An existing unit is left in place unless
  --force is passed. --start runs systemctl --user when systemctl exists.
  If it does not, install still writes the unit and exits 2.
- macOS: a LaunchAgent plist under ~/Library/LaunchAgents. --start runs
  launchctl bootstrap. This script does not claim that bootstrap succeeded
  unless launchctl's exit status says so.
- Windows: refused. This build does not install a Windows service.

Paths come from --home, --cwd, and the process environment. Nothing here is
a personal machine path.
"""

import argparse
import os
from pathlib import Path
import plistlib
import shlex
import shutil
import subprocess
import sys


LABEL = os.environ.get('TESSERA_CORE_LABEL') or 'app.tessera.core'


def serve_command(python, host, port):
    command = [python, '-m', 'tessera', 'serve']
    if host:
        command.extend(['--host', host])
    if port is not None:
        command.extend(['--port', str(port)])
    return command


def _systemd_environment(name, value):
    """One Environment= assignment systemd will not split or expand.

    The assignment is one double-quoted word. systemd then applies C-string
    escapes and specifier expansion, so a space stays in the value and a
    literal backslash, quote, or percent sign survives.
    """
    escaped = []
    for char in str(value):
        if char == '\\':
            escaped.append('\\\\')
        elif char == '"':
            escaped.append('\\"')
        elif char == '%':
            escaped.append('%%')
        elif char == '\n':
            escaped.append('\\n')
        elif char == '\r':
            escaped.append('\\r')
        elif char == '\t':
            escaped.append('\\t')
        else:
            escaped.append(char)
    return f'Environment="{name}={"".join(escaped)}"'


def linux_unit(python, home, cwd, host, port):
    command = ' '.join(shlex.quote(part) for part in serve_command(python, host, port))
    return (
        '[Unit]\n'
        'Description=Tessera core\n'
        '\n'
        '[Service]\n'
        'Type=simple\n'
        f'ExecStart={command}\n'
        f'WorkingDirectory={cwd}\n'
        f'{_systemd_environment("TESSERA_HOME", home)}\n'
        'Restart=on-failure\n'
        '\n'
        '[Install]\n'
        'WantedBy=default.target\n'
    )


def linux_unit_directory(environ):
    config_home = environ.get('XDG_CONFIG_HOME')
    if config_home:
        base = Path(config_home)
    else:
        home = environ.get('HOME')
        if not home:
            raise SystemExit('service: HOME is unset, so there is no systemd user directory to write.')
        base = Path(home) / '.config'
    return base / 'systemd' / 'user'


def launchd_plist(python, home, cwd, host, port, label):
    return {
        'Label': label,
        'ProgramArguments': serve_command(python, host, port),
        'WorkingDirectory': str(cwd),
        'EnvironmentVariables': {'TESSERA_HOME': str(home)},
        'RunAtLoad': True,
        'KeepAlive': True,
    }


def require_home(args):
    raw = args.home or os.environ.get('TESSERA_HOME')
    if not raw:
        raise SystemExit('service: set --home or TESSERA_HOME. Refusing to choose a directory on my own.')
    return Path(raw).expanduser()


def foreground(args):
    home = require_home(args)
    command = serve_command(args.python, args.host, args.port)
    env = os.environ.copy()
    env['TESSERA_HOME'] = str(home)
    os.execvpe(command[0], command, env)


def install(args):
    system = sys.platform
    if system.startswith('win'):
        print('service: this build does not install a Windows service.', file=sys.stderr)
        print('service: LaunchAgent start and stop is macOS-only and is not available on Windows.', file=sys.stderr)
        print('service: run the core in the foreground:', file=sys.stderr)
        print('  python -m tessera serve', file=sys.stderr)
        return 2
    home = require_home(args)
    cwd = Path(args.cwd).expanduser() if args.cwd else Path.cwd()
    python = args.python
    if system.startswith('linux'):
        directory = linux_unit_directory(os.environ)
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / 'tessera-core.service'
        if target.exists() and not args.force:
            print(f'service: {target} already exists. Pass --force to replace it.', file=sys.stderr)
            return 2
        target.write_text(linux_unit(python, home, cwd, args.host, args.port), encoding='utf-8')
        print(f'service: wrote {target}')
        if not args.start:
            print('service: the unit is not started. Pass --start, or run: python -m tessera serve')
            return 0
        systemctl = shutil.which('systemctl')
        if not systemctl:
            print('service: systemctl is not available, so the unit was not started.', file=sys.stderr)
            print('service: run the core in the foreground: python -m tessera serve', file=sys.stderr)
            return 2
        reload = subprocess.run([systemctl, '--user', 'daemon-reload'], check=False)
        if reload.returncode != 0:
            print('service: systemctl --user daemon-reload failed, so the unit was not started.', file=sys.stderr)
            return reload.returncode or 1
        started = subprocess.run([systemctl, '--user', 'enable', '--now', 'tessera-core.service'], check=False)
        if started.returncode != 0:
            print('service: systemctl --user enable --now tessera-core.service failed.', file=sys.stderr)
            print('service: the unit file is on disk. The core was not started by this command.', file=sys.stderr)
            return started.returncode or 1
        print('service: systemd user service tessera-core.service is enabled and started.')
        return 0
    if system == 'darwin':
        directory = Path.home() / 'Library' / 'LaunchAgents'
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f'{args.label}.plist'
        if target.exists() and not args.force:
            print(f'service: {target} already exists. Pass --force to replace it.', file=sys.stderr)
            return 2
        with target.open('wb') as stream:
            plistlib.dump(launchd_plist(python, home, cwd, args.host, args.port, args.label), stream)
        print(f'service: wrote {target}')
        if not args.start:
            print('service: the LaunchAgent is not loaded. Pass --start, or run: python -m tessera serve')
            return 0
        uid = os.getuid()
        domain = f'gui/{uid}'
        boot = subprocess.run(['launchctl', 'bootstrap', domain, str(target)], check=False)
        if boot.returncode != 0:
            print('service: launchctl bootstrap failed, so the LaunchAgent was not loaded.', file=sys.stderr)
            return boot.returncode or 1
        print(f'service: launchctl bootstrap {domain} loaded {target.name}.')
        return 0
    print(f'service: no supervisor install is implemented for {system}.', file=sys.stderr)
    print('service: run the core in the foreground: python -m tessera serve', file=sys.stderr)
    return 2


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', help='Tessera home. Defaults to TESSERA_HOME. Required for install and foreground.')
    parser.add_argument('--cwd', help='Working directory for a supervisor. Defaults to the current directory.')
    parser.add_argument('--python', default=sys.executable, help='Python used to start the core.')
    parser.add_argument('--host', help='Pass --host through to tessera serve. Omit to use the core config.')
    parser.add_argument('--port', type=int, help='Pass --port through to tessera serve. Omit to use the core config.')
    parser.add_argument('--label', default=LABEL, help='LaunchAgent label. macOS install only.')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('foreground', help='Replace this process with tessera serve.')
    install_parser = sub.add_parser('install', help='Write the supervisor file for this operating system.')
    install_parser.add_argument('--start', action='store_true', help='Start the supervisor after writing it.')
    install_parser.add_argument('--force', action='store_true', help='Replace an existing supervisor file.')
    args = parser.parse_args(argv)
    if args.command == 'foreground':
        foreground(args)
        return 0
    return install(args)


if __name__ == '__main__':
    raise SystemExit(main())
