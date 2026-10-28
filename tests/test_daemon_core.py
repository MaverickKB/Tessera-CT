"""Owned daemon regressions: real pipes and offline core adapters."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading

import pytest


ROOT = Path(__file__).resolve().parents[1]
CHILD = '''
import sys
from pathlib import Path
from tessera.config import Config
from tessera.daemon import Daemon
from tessera.rpc import serve
sys.path.insert(0, str(Path.cwd() / 'tests'))
from fake_backend import FakeBackend
from local_memory import test_local_memory
config = Config(home=Path(sys.argv[1]), cwd=Path(sys.argv[1]))
reply = {'tool_calls': [{'id': 'write-fixture', 'name': 'write_file',
         'arguments': {'path': 'approved.txt', 'text': 'approved fixture'}}]}
factory = lambda *args: FakeBackend([reply, 'Completed fixture', 'Resumed fixture'])
with test_local_memory():
    serve(Daemon(config, backend_factory=factory))
'''


@contextmanager
def child(home, *, fake=True):
    environment = {}
    for name, value in os.environ.items():
        if not name.startswith('TESSERA_'):
            environment[name] = value
    command = [sys.executable, '-u', '-c', CHILD, str(home)] if fake else [
        sys.executable, '-m', 'tessera.app_server', '--home', str(home)]
    process = subprocess.Popen(command, cwd=ROOT, env=environment, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    packets = queue.Queue()
    def read():
        for line in process.stdout:
            packets.put(json.loads(line))
        packets.put(None)
    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    class Client:
        def __init__(self):
            self.next_id = 0
            self.events = []
        def send(self, message):
            process.stdin.write(json.dumps(message).encode() + b'\n')
            process.stdin.flush()
        def receive(self):
            packet = packets.get(timeout=15)
            assert packet is not None, 'Daemon exited without a response'
            assert packet['jsonrpc'] == '2.0'
            return packet
        def call(self, method, params=None):
            self.next_id += 1
            number = self.next_id
            self.send({'jsonrpc': '2.0', 'id': number, 'method': method, 'params': params or {}})
            while True:
                packet = self.receive()
                if packet.get('id') == number and 'method' not in packet:
                    assert 'error' not in packet, packet
                    return packet['result']
                self.events.append(packet)
        def initialize(self):
            result = self.call('initialize', {'clientInfo': {'name': 'fixture'}})
            self.send({'jsonrpc': '2.0', 'method': 'initialized'})
            return result
        def next_event(self):
            return self.events.pop(0) if self.events else self.receive()
    try:
        yield Client(), process
    finally:
        if process.stdin and not process.stdin.closed:
            process.stdin.close()
        process.stdin = None
        try:
            _, errors = process.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            pytest.fail('Daemon did not stop on attachment EOF')
        reader.join(timeout=5)
        assert process.returncode == 0, errors.decode()


def test_real_pipe_approval_turn_history_view_and_resume(tmp_path):
    with child(tmp_path) as (client, _):
        assert client.initialize()['capabilities']['transport'] == 'stdio'
        thread = client.call('thread/start', {'title': 'Pipe fixture'})['thread']
        slug = thread['id']
        turn = client.call('turn/start', {'threadId': slug, 'input': [{'type': 'text', 'text': 'Use write_file to save the fixture'}]})['turn']
        approved = False
        while True:
            packet = client.next_event()
            if packet.get('method') == 'item/tool/requestApproval':
                assert packet['params']['name'] == 'write_file'
                assert packet['params']['threadId'] == slug
                assert packet['params']['turnId'] == turn['id']
                assert not (tmp_path / 'approved.txt').exists()
                client.send({'jsonrpc': '2.0', 'id': packet['id'], 'result': {'decision': 'accept'}})
                approved = True
            if packet.get('method') == 'turn/completed':
                assert packet['params']['turn']['status'] == 'completed'
                break
        assert approved and (tmp_path / 'approved.txt').read_text() == 'approved fixture'
        assert client.call('view/zoom', {'threadId': slug, 'start': 0})['text'] == 'Use write_file to save the fixture'
        assert client.call('view/read', {'threadId': slug})['lines']
        history = client.call('thread/read', {'threadId': slug})['messages']
        assert history[-1]['text'] == 'Completed fixture'
    with child(tmp_path) as (client, _):
        client.initialize()
        resumed = client.call('thread/resume', {'threadId': slug})
        assert resumed['messages'] == history


def test_empty_daemon_needs_no_provider_or_listener(tmp_path):
    with child(tmp_path, fake=False) as (client, _):
        client.initialize()
        assert client.call('thread/list') == {'data': []}
        assert client.call('status')['session_count'] == 0
        thread = client.call('thread/start', {'title': 'Offline'})['thread']
        assert client.call('thread/resume', {'threadId': thread['id']})['messages'] == []
        assert client.call('view/read', {'threadId': thread['id']})['lines'] == []
