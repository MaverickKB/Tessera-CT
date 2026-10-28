"""Terminal consumers of the daemon's swarm/A2A contract over actual pipes."""

import io
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from tessera.rpc_client import RpcClient, RpcError
from tessera.terminal import Terminal


ROOT = Path(__file__).resolve().parents[1]
ENGINE = r'''
import json
from pathlib import Path
import sys

requests = Path(sys.argv[1])
mode = sys.argv[2]
events = [
    {'sequence': 1, 'op': 'message', 'message': {
        'id': 'message-one', 'kind': 'direct', 'sender': 'parent',
        'target': 'child', 'text': 'Synthetic task update.\x1b]52;PRIVATE_OSC\x07', 'secret': 'PRIVATE_FIELD'}},
    {'sequence': 2, 'op': 'ack', 'message_id': 'message-one',
     'session': 'child', 'direction': 'incoming'},
    {'sequence': 3, 'op': 'message', 'message': {
        'id': 'message-two', 'kind': 'handoff', 'sender': 'child',
        'target': 'parent', 'text': 'Synthetic handoff.'}},
    {'sequence': 4, 'op': 'message', 'message': {
        'id': 'message-three', 'kind': 'channel', 'sender': 'child',
        'channel': 'fixture-channel', 'text': 'Synthetic channel note.'}},
]
if mode == 'mixed':
    events[0]['message'].update(sender='cli-child', target='grok-child', provider_kind='grok_cli')
    events[1]['session'] = 'grok-child'
    events[2]['message'].update(sender='grok-child', target='cli-child')

for line in sys.stdin:
    row = json.loads(line)
    with requests.open('a') as stream:
        stream.write(json.dumps(row) + '\n')
    method = row.get('method')
    if 'id' not in row:
        continue
    params = row.get('params', {})
    result = {}
    if method == 'initialize':
        result = {'serverInfo': {'protocolVersion': 1}}
    elif method == 'agent/list':
        if mode == 'old':
            print(json.dumps({'jsonrpc': '2.0', 'id': row['id'],
                              'error': {'code': -32601, 'message': 'Unavailable'}}), flush=True)
            continue
        result = {'data': [{'id': 'run-placeholder', 'parentThreadId': 'parent',
                           'mode': 'parallel', 'status': 'succeeded',
                           'workers': [{'threadId': 'child', 'endpoint': 'PRIVATE_FIELD'}]}]}
        if mode == 'empty':
            result = {'data': []}
        elif mode == 'bad-list':
            result = {'data': [{'id': 'parent'}, {'id': None}]}
        elif mode == 'mixed':
            result = {'data': [{'id': 'run-placeholder', 'parentThreadId': 'parent',
                               'mode': 'parallel', 'status': 'running', 'workers': [
                                   {'threadId': 'cli-child', 'provider_kind': 'cli_worker', 'backend': 'PRIVATE_FIELD'},
                                   {'threadId': 'grok-child', 'provider_kind': 'grok_cli', 'credential_ref': 'PRIVATE_FIELD'}]}]}
    elif method == 'thread/list':
        result = {'data': [{'id': 'parent', 'status': 'idle'}, {'id': 'child', 'status': 'completed'}]}
        if mode == 'old':
            result = {'data': [{'id': 'parent'}]}
        elif mode == 'empty':
            result = {'data': []}
        elif mode == 'mixed':
            result = {'data': [{'id': 'parent'}, {'id': 'cli-child', 'backend': 'PRIVATE_FIELD'},
                               {'id': 'grok-child', 'auth_path': 'PRIVATE_FIELD'}]}
    elif method == 'thread/resume':
        result = {'thread': {'id': params['threadId']},
                  'messages': [{'i': 0, 'kind': 'agent', 'text': 'Child saved result.'}]}
    elif method == 'agent/send':
        result = {'message': {'message_id': params['messageId'], 'secret': 'PRIVATE_FIELD'}}
        if mode == 'no-receipt':
            result = {'accepted': True}
    elif method == 'channel/events':
        after = params['after']
        data = [event for event in events if event['sequence'] > after][:params['limit']]
        result = {'data': data, 'nextAfter': data[-1]['sequence'] if data else after, 'enabled': True}
        if mode == 'disabled':
            result = {'data': [], 'nextAfter': after, 'enabled': False}
        elif mode == 'no-events':
            result = {'data': [], 'nextAfter': after, 'enabled': True}
        elif mode == 'bad-cursor':
            result = {'data': data, 'nextAfter': after}
        elif mode == 'bad-rows':
            result = {'data': [{'sequence': True, 'op': 'ack'}], 'nextAfter': 1}
        elif mode == 'unavailable':
            print(json.dumps({'jsonrpc': '2.0', 'id': row['id'],
                              'error': {'code': -32601, 'message': 'Unavailable'}}), flush=True)
            continue
    else:
        raise AssertionError('Unexpected method')
    print(json.dumps({'jsonrpc': '2.0', 'id': row['id'], 'result': result}), flush=True)
'''


def command(tmp_path, mode='normal'):
    script = tmp_path / 'coordination_fixture.py'
    script.write_text(ENGINE)
    return [sys.executable, '-u', str(script), str(tmp_path / 'requests.jsonl'), mode]


def client(tmp_path, text, mode='normal', *options):
    environment = dict(os.environ, TESSERA_ENGINE_COMMAND=json.dumps(command(tmp_path, mode)), NO_COLOR='1')
    return subprocess.run([sys.executable, '-m', 'tessera', *options], input=text,
                          cwd=ROOT, env=environment, text=True, capture_output=True, timeout=15)


def requests(tmp_path, method):
    matches = []
    for line in (tmp_path / 'requests.jsonl').read_text().splitlines():
        row = json.loads(line)
        if row.get('method') == method:
            matches.append(row['params'])
    return matches


def test_list_resume_child_send_and_page_coordination_over_stdio(tmp_path):
    result = client(tmp_path, '/agents\n/resume child\n/send parent "Ready for merge"\n'
                    '/events --limit 2\n/events --limit 2\n/events\n/quit\n')
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'parent  parent session  [idle]' in result.stdout
    assert 'child  parent=parent  [completed]' in result.stdout
    assert 'Resumed: child' in result.stdout
    assert 'agent: Child saved result.' in result.stdout
    assert 'Recorded message: ' in result.stdout
    assert 'does not confirm delivery, acknowledgement or a reply' in result.stdout
    assert '[1] direct' in result.stdout and '[2] ack' in result.stdout
    assert '[3] handoff' in result.stdout and '[4] channel' in result.stdout
    assert 'No coordination events after cursor 4.' in result.stdout
    assert 'PRIVATE_FIELD' not in result.stdout + result.stderr
    assert 'PRIVATE_OSC' not in result.stdout + result.stderr
    assert '\x1b' not in result.stdout
    sends = requests(tmp_path, 'agent/send')
    assert len(sends) == 1
    sent = sends[0]
    assert sent['fromThreadId'] == 'child'
    assert sent['toThreadId'] == 'parent'
    assert sent['text'] == 'Ready for merge'
    assert len(sent['messageId']) == 32
    assert 'Recorded message: ' + sent['messageId'] in result.stdout
    assert requests(tmp_path, 'channel/events') == [
        {'after': 0, 'limit': 2}, {'after': 2, 'limit': 2}, {'after': 4, 'limit': 100}]
    assert not requests(tmp_path, 'turn/start')
    assert not requests(tmp_path, 'swarm/create')


@pytest.mark.parametrize('mode,expected', [
    ('empty', 'No saved agents or sessions.'),
    ('old', 'parent unreported'),
    ('bad-list', 'invalid agent topology'),
])
def test_listing_empty_older_and_invalid_daemons(tmp_path, mode, expected):
    result = client(tmp_path, '/agents\n/quit\n', mode)
    assert result.returncode == 0
    assert expected in result.stdout
    assert not requests(tmp_path, 'thread/resume')


def test_send_requires_sender_and_receipt_and_never_retries(tmp_path):
    result = client(tmp_path, '/send child hello\n/resume parent\n/send child hello\n/quit\n', 'no-receipt')
    assert result.returncode == 0
    assert 'resume a sender session' in result.stdout
    assert 'did not confirm a durable message receipt' in result.stdout
    assert 'Recorded message:' not in result.stdout
    assert len(requests(tmp_path, 'agent/send')) == 1


@pytest.mark.parametrize('mode', ['bad-cursor', 'bad-rows'])
def test_invalid_coordination_page_never_advances_or_prints_partial_rows(tmp_path, mode):
    output = io.StringIO()
    with RpcClient(command(tmp_path, mode), timeout=5) as peer:
        terminal = Terminal(peer, output=output, color='never')
        terminal.initialize()
        with pytest.raises(RpcError, match='coordination'):
            terminal.events([])
        assert terminal.event_cursor == 0
    assert 'Synthetic task update' not in output.getvalue()


@pytest.mark.parametrize('mode,expected', [
    ('disabled', 'Coordination channels are disabled.'),
    ('unavailable', 'This engine method is unavailable.'),
])
def test_disabled_or_missing_coordination_stays_honest(tmp_path, mode, expected):
    result = client(tmp_path, '/events --watch\n/quit\n', mode)
    assert result.returncode == 0
    assert expected in result.stdout
    assert len(requests(tmp_path, 'channel/events')) == 1


def test_watch_resumes_cursor_and_ctrl_c_returns_without_stopping_work(tmp_path, monkeypatch):
    waits = []

    def pause(seconds):
        waits.append(seconds)
        if len(waits) == 2:
            raise KeyboardInterrupt

    monkeypatch.setattr('tessera.terminal.time', SimpleNamespace(sleep=pause))
    output = io.StringIO()
    with RpcClient(command(tmp_path), timeout=5) as peer:
        terminal = Terminal(peer, output=output, color='never', event_limit=2, event_poll_seconds=0.25)
        terminal.initialize()
        terminal.events(['--watch'])
        assert terminal.event_cursor == 4
        terminal.agents()
    assert waits == [0.25, 0.25]
    assert 'Stopped watching at cursor 4.' in output.getvalue()
    assert requests(tmp_path, 'channel/events') == [{'after': 0, 'limit': 2}, {'after': 2, 'limit': 2}]
    assert not requests(tmp_path, 'turn/interrupt')
    assert not requests(tmp_path, 'shutdown')


def test_real_terminal_watch_interrupt_returns_to_commands(tmp_path):
    import pty

    environment = dict(os.environ, TESSERA_ENGINE_COMMAND=json.dumps(command(tmp_path)), NO_COLOR='1')
    master, slave = pty.openpty()
    process = subprocess.Popen([sys.executable, '-m', 'tessera', '--event-poll-seconds', '0.25'],
                               cwd=ROOT, env=environment, stdin=slave, stdout=slave, stderr=slave)
    os.close(slave)
    transcript = ''
    deadline = time.monotonic() + 15

    def until(marker):
        nonlocal transcript
        while marker not in transcript:
            remaining = deadline - time.monotonic()
            assert remaining > 0, transcript
            assert select.select([master], [], [], remaining)[0], transcript
            transcript += os.read(master, 65536).decode()

    try:
        until('> ')
        os.write(master, b'/events --watch\n')
        until('Coordination cursor: 4')
        process.send_signal(signal.SIGINT)
        until('Stopped watching at cursor 4.')
        os.write(master, b'/agents\n')
        until('Agents / sessions')
        os.write(master, b'/quit\n')
        assert process.wait(timeout=5) == 0, transcript
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
        os.close(master)
    assert not requests(tmp_path, 'turn/interrupt')
    assert len(requests(tmp_path, 'agent/list')) == 1


def test_watch_prints_an_unchanged_empty_page_once(tmp_path, monkeypatch):
    waits = []

    def pause(seconds):
        waits.append(seconds)
        if len(waits) == 3:
            raise KeyboardInterrupt

    monkeypatch.setattr('tessera.terminal.time', SimpleNamespace(sleep=pause))
    output = io.StringIO()
    with RpcClient(command(tmp_path, 'no-events'), timeout=5) as peer:
        terminal = Terminal(peer, output=output, color='never')
        terminal.initialize()
        terminal.events(['--watch'])
    assert output.getvalue().count('No coordination events after cursor 0.') == 1
    assert output.getvalue().count('Coordination cursor: 0') == 1
    assert len(requests(tmp_path, 'channel/events')) == 3


@pytest.mark.parametrize('text', [
    '/events --after -1', '/events --limit 0', '/events --limit',
    '/events --watch --watch', '/events --unknown', '/agents extra', '/send child',
])
def test_invalid_command_never_reaches_daemon(tmp_path, text):
    result = client(tmp_path, text + '\n/quit\n')
    assert result.returncode == 0
    assert not requests(tmp_path, 'agent/send')
    assert not requests(tmp_path, 'agent/list')
    assert not requests(tmp_path, 'channel/events')


@pytest.mark.parametrize('option,value', [
    ('--event-limit', '0'), ('--event-poll-seconds', 'nan'),
    ('--event-poll-seconds', 'inf'), ('--event-poll-seconds', '-1'),
])
def test_invalid_event_settings_fail_before_spawn(tmp_path, option, value):
    result = client(tmp_path, '/quit\n', 'normal', option, value)
    assert result.returncode == 1
    assert 'must be positive' in result.stderr
    assert not (tmp_path / 'requests.jsonl').exists()


REAL_COORDINATION_DAEMON = r'''
from pathlib import Path
import socket
import subprocess
import sys
from types import SimpleNamespace
from urllib import request

sys.path.insert(0, str(Path.cwd() / 'tests'))
from fake_backend import FakeBackend
from local_memory import test_local_memory
from tessera.config import AccountConfig, BackendConfig, Config
from tessera.credentials import CredentialResolver
from tessera.daemon import Daemon
from tessera.rpc import serve

def forbidden(*args, **kwargs):
    raise AssertionError('This terminal acceptance is offline')

socket.socket.connect = socket.socket.connect_ex = socket.socket.bind = forbidden
request.OpenerDirector.open = forbidden
CredentialResolver.resolve = forbidden
subprocess.Popen = forbidden
home = Path(sys.argv[1])
config = Config(home=home, cwd=home)
config.accounts['fixture-pool'] = AccountConfig(name='fixture-pool', provider_kind='cli_worker',
                                               limits={'max_concurrent': 2})
config.backends['fixture-worker'] = BackendConfig(name='fixture-worker', account='fixture-pool')
config.roles = {'main': 'fixture-worker'}
config.swarm.enabled = config.agents.enabled = config.channels.enabled = True
config.agents.token_budget = 100
config.agents.wall_seconds = 10
config.channels.auto_reply = False
mode = sys.argv[2] if len(sys.argv) > 2 else ''
if mode == 'planning':
    config.agents.planner_tokens = 10

def backend(slug, name):
    if mode == 'planning' and slug == 'parent':
        import json
        proposal = {'mode': 'parallel', 'workers': [
            {'objective': 'Reply ALPHA', 'scope': ['alpha']},
            {'objective': 'Reply BETA', 'scope': ['beta']}]}
        return FakeBackend([{'content': json.dumps(proposal), 'usage': {'input_tokens': 2, 'output_tokens': 1}}])
    word = 'ALPHA' if slug.endswith('-0') else 'BETA'
    return FakeBackend([{'content': word, 'usage': {'input_tokens': 2, 'output_tokens': 1}}])

with test_local_memory():
    daemon = Daemon(config, backend_factory=backend)
    daemon.peer = SimpleNamespace(notify=lambda *args: None)
    if not mode and not daemon.dispatch('thread/list', {})['data']:
        parent = daemon.dispatch('thread/start', {'title': 'Parent'})['thread']['id']
        plan = daemon.dispatch('agent/spawn', {
            'threadId': parent, 'objective': 'Combine independent ALPHA and BETA outcomes',
            'workers': [{'objective': 'Reply ALPHA', 'scope': ['alpha']},
                        {'objective': 'Reply BETA', 'scope': ['beta']}],
            'requestId': 'c' * 32})['run']
        assert plan['mode'] == 'parallel'
        finished = daemon.dispatch('agent/run', {'runId': plan['id']})['run']
        assert finished['status'] == 'succeeded'
    serve(daemon)
assert 'tessera.server' not in sys.modules
'''


def test_terminal_actual_daemon_lists_children_sends_watches_and_reattaches(tmp_path):
    home = tmp_path / 'daemon-home'
    argv = [sys.executable, '-u', '-c', REAL_COORDINATION_DAEMON, str(home)]
    environment = dict(os.environ, TESSERA_ENGINE_COMMAND=json.dumps(argv), NO_COLOR='1')
    child = 'agent-' + 'c' * 32 + '-0'
    text = '/agents\n/resume ' + child + '\n/send parent "Synthetic terminal report"\n'
    text += '/events --limit 2\n/events\n/quit\n'
    first = subprocess.run([sys.executable, '-m', 'tessera'], input=text, cwd=ROOT,
                           env=environment, text=True, capture_output=True, timeout=30)
    assert first.returncode == 0, first.stdout + first.stderr
    assert child + '  parent=parent' in first.stdout
    assert 'mode=parallel  [succeeded]  workers=2' in first.stdout
    assert 'agent: ALPHA' in first.stdout
    assert 'Recorded message: ' in first.stdout
    assert '[1] direct' in first.stdout and '[2] ack' in first.stdout
    assert '[3] ack' in first.stdout
    assert 'Synthetic terminal report' in first.stdout
    second = subprocess.run([sys.executable, '-m', 'tessera', 'resume', child],
                            input='/agents\n/events\n/quit\n', cwd=ROOT, env=environment,
                            text=True, capture_output=True, timeout=30)
    assert second.returncode == 0, second.stdout + second.stderr
    assert 'Resumed: ' + child in second.stdout
    assert 'agent: ALPHA' in second.stdout
    assert 'Synthetic terminal report' in second.stdout
    assert 'mode=parallel  [succeeded]  workers=2' in second.stdout
    assert 'direction=incoming' in second.stdout


def test_mixed_cli_and_grok_children_share_list_send_events_and_resume(tmp_path):
    result = client(tmp_path, '/agents\n/resume cli-child\n/send grok-child "Synthetic cross-worker note"\n'
                    '/events\n/resume grok-child\n/quit\n', 'mixed')
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'cli-child  parent=parent' in result.stdout
    assert 'grok-child  parent=parent' in result.stdout
    assert 'Resumed: cli-child' in result.stdout and 'Resumed: grok-child' in result.stdout
    sent = requests(tmp_path, 'agent/send')
    assert len(sent) == 1
    assert sent[0]['fromThreadId'] == 'cli-child' and sent[0]['toThreadId'] == 'grok-child'
    assert 'Recorded message: ' + sent[0]['messageId'] in result.stdout
    assert 'target=grok-child' in result.stdout and 'session=grok-child' in result.stdout
    assert 'PRIVATE_FIELD' not in result.stdout + result.stderr
