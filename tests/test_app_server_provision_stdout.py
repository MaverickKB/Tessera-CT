"""First-session provisioning must not write pip noise onto app-server stdout.

The stdio daemon is an NDJSON stream. redirect_stdout does not cover child
processes, so a pip install that inherits stdout breaks every client. This
drives the real app-server and the real provisioner.
"""

import json
import os
from pathlib import Path
import queue
import shutil
import signal
import subprocess
import sys
import threading

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _toml(value):
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"') + '"'


def test_app_server_stdout_stays_json_while_the_engine_provisions(tmp_path):
    engine = tmp_path / 'engine'
    shutil.copytree(ROOT / 'tessera' / '_assets' / 'aethermind-engine', engine)
    (engine / 'build-receipt.json').write_text(
        '{"engine":"bundled","commit":"ec1011eebe00e8197e29acbbb3ae710090f53899"}\n',
        encoding='utf-8')
    home = tmp_path / 'home'
    project = home / 'memory-project'
    project.mkdir(parents=True)
    home.mkdir(exist_ok=True)
    (home / 'config.toml').write_text(
        '\n'.join([
            '[backends.fake]',
            'base_url = "http://127.0.0.1:9/v1"',
            'model = "scripted-local-fake"',
            'api_key_env = ""',
            'timeout = 5',
            'retries = 0',
            '[roles]',
            'main = "fake"',
            '[aethermind]',
            'url = ""',
            'bundled_path = ' + _toml(engine),
            'project = ' + _toml(project),
            'personal_project = ""',
            'author = "tessera"',
            'timeout = 60',
            'probe_timeout = 30',
            '[jev]',
            'enabled = false',
            '[tools]',
            'approval = "never"',
            '',
        ]),
        encoding='utf-8')
    environment = {key: value for key, value in os.environ.items() if not key.startswith('TESSERA_')}
    environment.pop('AETHERMIND_TOKEN', None)
    process = subprocess.Popen(
        [sys.executable, '-m', 'tessera.app_server', '--home', str(home)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=environment, start_new_session=True)
    raw_lines = []
    bad = []
    incoming = queue.Queue()
    stderr = []

    def read_stdout():
        for raw in iter(process.stdout.readline, b''):
            text = raw.decode('utf-8', errors='replace')
            raw_lines.append(text)
            try:
                incoming.put(json.loads(text))
            except json.JSONDecodeError:
                bad.append(text)

    def read_stderr():
        stderr.append(process.stderr.read().decode('utf-8', errors='replace'))

    threading.Thread(target=read_stdout, daemon=True).start()
    threading.Thread(target=read_stderr, daemon=True).start()
    next_id = 0

    def send(message):
        process.stdin.write(json.dumps(message).encode() + b'\n')
        process.stdin.flush()

    def call(method, params=None, timeout=180):
        nonlocal next_id
        next_id += 1
        number = next_id
        send({'jsonrpc': '2.0', 'id': number, 'method': method, 'params': params or {}})
        deadline = timeout
        while True:
            try:
                message = incoming.get(timeout=deadline)
            except queue.Empty:
                pytest.fail('no JSON reply for ' + method + '\nstdout:\n' + ''.join(raw_lines)
                            + '\nstderr:\n' + ''.join(stderr))
            if message.get('id') == number and 'method' not in message:
                assert 'error' not in message, message
                return message['result']

    try:
        call('initialize', {'clientInfo': {'name': 'stdout-check'}})
        send({'jsonrpc': '2.0', 'method': 'initialized'})
        thread = call('thread/start', {'title': 'Provision'})['thread']
        call('thread/resume', {'threadId': thread['id']})
        call('turn/start', {'threadId': thread['id'], 'input': [{'type': 'text', 'text': 'hello'}]})
        assert raw_lines, 'app-server wrote no stdout'
        assert not bad, 'non-JSON stdout during provisioning:\n' + ''.join(bad)
        for line in raw_lines:
            if line.strip():
                json.loads(line)
    finally:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except OSError:
                process.kill()
        process.wait(timeout=20)
    log = ''.join(stderr)
    assert 'Collecting ' in log or 'Successfully installed ' in log, log
    assert not bad
