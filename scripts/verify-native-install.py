"""Accept a built release through the public installer and native start command.

Run on disposable native CI runners. Installs into the current user's ordinary
application location, but all core state and work stay in a temporary home.
No model, account, or external service is contacted by the test conversation.
"""

import argparse
import json
import os
from pathlib import Path
import queue
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request


def request(port, path, body=None):
    data = None if body is None else json.dumps(body).encode()
    call = urllib.request.Request(
        f'http://127.0.0.1:{port}{path}', data=data,
        headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(call, timeout=2) as response:
        return json.load(response)


def unused_port():
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        return listener.getsockname()[1]


def stop(process, ready, port):
    if os.name == 'nt':
        for pid in {ready.get('shell_pid'), ready.get('core_pid')} - {None}:
            subprocess.run(['taskkill', '/PID', str(pid), '/T', '/F'],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if process.poll() is None:
            process.terminate()
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            request(port, '/api/status?shape=connection')
        except (OSError, ValueError):
            return
        time.sleep(0.1)
    raise AssertionError('Closing the native app left its owned core running')


def exercise(command, home, work, browser_marker, env, port):
    records = queue.Queue()
    output = []
    ready = {}
    process = subprocess.Popen(command, cwd=work, env=env,
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True,
                               start_new_session=os.name != 'nt')

    def read():
        for line in process.stdout:
            records.put(line)
        records.put(None)

    threading.Thread(target=read, daemon=True).start()
    try:
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            try:
                line = records.get(timeout=0.5)
            except queue.Empty:
                continue
            if line is None:
                raise AssertionError('Public command exited without a native window: '
                                     + ''.join(output)[-12000:])
            output.append(line)
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict) and record.get('event') == 'tessera-desktop.window-ready':
                ready = record
                break
        if not ready:
            raise AssertionError('Native window did not become ready: ' + ''.join(output)[-12000:])
        status = request(port, '/api/status?shape=connection')
        assert status.get('generation'), 'The owned core did not identify its generation'
        session = request(port, '/api/sessions', {'title': 'Native install acceptance', 'backend': 'smoke'})
        assert (home / 'sessions' / session['slug']).is_dir(), 'The app used a different Tessera home'
        assert not browser_marker.exists(), 'The default command opened a browser'
        print(json.dumps({'command': command[command.index('tessera') + 3],
                          'window_ready': True, 'core_answered': True,
                          'selected_home': True, 'browser_opened': False}), flush=True)
    finally:
        stop(process, ready, port)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--asset', type=Path, required=True)
    parser.add_argument('--sha256-file', type=Path, required=True)
    args = parser.parse_args()
    asset = args.asset.resolve()
    checksum = args.sha256_file.read_text().split()[0]
    with tempfile.TemporaryDirectory(prefix='tessera-native-acceptance-') as temporary:
        root = Path(temporary)
        home = root / 'selected-home'
        home.mkdir()
        work = root / 'outside-checkout'
        work.mkdir()
        browser_marker = root / 'browser-opened'
        sentinel = root / 'browser-sentinel.py'
        sentinel.write_text('from pathlib import Path\nPath(' + repr(str(browser_marker)) + ').touch()\n')
        (home / 'config.toml').write_text(
            'desktop_install_timeout = 120\ndesktop_startup_timeout = 120\n'
            '[backends.smoke]\nbase_url = "http://127.0.0.1:9/v1"\nmodel = "unused"\n'
            '[roles]\nmain = "smoke"\n')
        env = {key: value for key, value in os.environ.items()
               if not key.startswith('TESSERA_') and key != 'PYTHONPATH'}
        env['BROWSER'] = f'"{sys.executable}" "{sentinel}" %s'
        env['APPIMAGE_EXTRACT_AND_RUN'] = '1'
        # The installed interpreter must find the installed package from a clean
        # directory. The release job installs this checkout in its venv first.
        base = [sys.executable, '-u', '-m', 'tessera', '--home', str(home)]
        for command in [base + ['install-desktop', '--release-url', asset.as_uri(), '--sha256', checksum],
                        base + ['start']]:
            port = unused_port()
            env['TESSERA_DESKTOP_CORE_PORT'] = str(port)
            if command[-1] == 'start':
                command += ['--port', str(port)]
            exercise(command, home, work, browser_marker, env, port)
    print('Native package install and subsequent native start passed.', flush=True)


if __name__ == '__main__':
    main()
