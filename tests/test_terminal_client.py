"""Terminal client behavior through real pipes, without an in-process engine."""

import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from tessera.rpc_client import RpcClient, RpcError
from tessera.terminal import Terminal, plain


ROOT = Path(__file__).resolve().parents[1]

FAKE_ENGINE = r'''
import json
import sys
from pathlib import Path

state = Path(sys.argv[1])
rows = json.loads(state.read_text()) if state.exists() else []
initialized = False
approval = None

def send(value):
    print(json.dumps({'jsonrpc': '2.0', **value}), flush=True)

def event(method, params):
    send({'method': method, 'params': params})

for line in sys.stdin:
    request = json.loads(line)
    method = request.get('method')
    if method is None:
        assert request['id'] == 'approval-placeholder'
        assert request['result']['decision'] in ('accept', 'decline')
        event('item/completed', {'threadId': 'subject', 'item': {'id': 'answer', 'type': 'agentMessage', 'text': 'Tool ' + request['result']['decision']}})
        event('turn/completed', {'threadId': 'subject', 'turn': {'id': 'turn-approval', 'status': 'completed'}})
        continue
    if method == 'initialize':
        assert request['params']['clientInfo']['name'] == 'tessera-terminal'
        send({'id': request['id'], 'result': {'serverInfo': {'name': 'tessera', 'protocolVersion': 1}, 'capabilities': {}}})
        continue
    if method == 'initialized':
        initialized = True
        continue
    assert initialized
    params = request['params']
    result = {}
    if method == 'thread/list':
        result = {'data': [{'id': 'subject', 'title': 'Subject', 'updated': '2026-01-01'}]} if rows else {'data': []}
    elif method == 'thread/start':
        result = {'thread': {'id': 'subject'}}
    elif method == 'thread/resume':
        assert params['threadId'] == 'subject'
        result = {'thread': {'id': 'subject'}, 'messages': rows}
    elif method == 'turn/start':
        assert params['threadId'] == 'subject'
        text = params['input'][0]['text']
        rows.append({'kind': 'user', 'text': text})
        state.write_text(json.dumps(rows))
        if text == 'approve':
            send({'id': request['id'], 'result': {'turn': {'id': 'turn-approval'}}})
            send({'id': 'approval-placeholder', 'method': 'item/tool/requestApproval', 'params': {'threadId': 'subject', 'turnId': 'turn-approval', 'name': 'fixture_tool', 'arguments': {'value': 'fixture'}}})
            continue
        # Exercise notifications that arrive before the matching start response.
        event('item/agentMessage/delta', {'threadId': 'other', 'itemId': 'bad', 'delta': 'WRONG_THREAD'})
        event('item/agentMessage/delta', {'threadId': 'subject', 'itemId': 'answer', 'delta': 'Hello '})
        event('item/agentMessage/delta', {'threadId': 'subject', 'itemId': 'answer', 'delta': 'from the engine.'})
        event('item/completed', {'threadId': 'subject', 'item': {'id': 'answer', 'type': 'agentMessage', 'text': 'Hello from the engine.'}})
        event('turn/completed', {'threadId': 'subject', 'turn': {'id': 'turn-one', 'status': 'completed'}})
        result = {'turn': {'id': 'turn-one'}}
    elif method == 'view/read':
        result = {'lines': [{'start': 0, 'count': 1, 'text': 'Saved working-view text.'}]}
    elif method == 'view/zoom':
        assert params['start'] == 0 and params['count'] == 1
        original = 'Original source text.'
        offset = params.get('offset', 0)
        limit = params.get('limit', len(original))
        end = offset + limit
        result = {'text': original[offset:end], 'nextOffset': end if end < len(original) else None}
    elif method == 'status':
        result = {'sessions': 1, 'configured': False}
    elif method == 'swarm/show':
        assert params['jobId'] == 'job-placeholder'
        result = {'status': 'queued', 'workers': []}
    else:
        send({'id': request['id'], 'error': {'code': -32601, 'message': 'Not implemented'}})
        continue
    send({'id': request['id'], 'result': result})
'''


@pytest.fixture
def engine_command(tmp_path):
    script = tmp_path / 'fake_engine.py'
    script.write_text(FAKE_ENGINE)
    return [sys.executable, '-u', str(script), str(tmp_path / 'saved.json')]


def run_client(command, text, *args):
    env = dict(os.environ, TESSERA_ENGINE_COMMAND=json.dumps(command), NO_COLOR='1')
    return subprocess.run([sys.executable, '-m', 'tessera', *args], input=text,
                          cwd=ROOT, env=env, text=True, capture_output=True, timeout=15)


def test_default_chat_streams_once_and_resumes_saved_history(engine_command):
    first = run_client(engine_command, 'First question\n/view\n/zoom 0+1\n/status\n/swarm show job-placeholder\n/quit\n')
    assert first.returncode == 0, first.stderr
    assert first.stdout.count('Hello from the engine.') == 1
    assert 'WRONG_THREAD' not in first.stdout
    assert '[0+1] Saved working-view text.' in first.stdout
    assert 'Original source text.' in first.stdout
    assert '"status": "queued"' in first.stdout
    second = run_client(engine_command, '/quit\n', 'resume', '--last')
    assert second.returncode == 0, second.stderr
    assert 'Resumed: subject' in second.stdout
    assert 'user: First question' in second.stdout


def test_chat_session_alias_and_noninteractive_tool_approval_denies(engine_command):
    result = run_client(engine_command, 'approve\n/quit\n', 'chat', '--session', 'Subject')
    assert result.returncode == 0, result.stderr
    assert 'Approval: fixture_tool' in result.stdout
    assert 'Tool decline' in result.stdout
    assert 'Declined: interactive approval' in result.stdout


def test_tty_approval_is_one_explicit_decision(engine_command):
    class TtyInput(io.StringIO):
        def isatty(self):
            return True
    output = io.StringIO()
    with RpcClient(engine_command, timeout=5) as peer:
        terminal = Terminal(peer, source=TtyInput('yes\n'), output=output, color='never')
        terminal.initialize()
        terminal.new()
        terminal.turn('approve')
    assert 'Allow this tool once? [y/N]' in output.getvalue()
    assert 'Tool accept' in output.getvalue()


def test_resume_picker_cancellation_and_bad_slash_input(engine_command):
    run_client(engine_command, 'Save this\n/quit\n')
    result = run_client(engine_command, '/quit\n', 'resume')
    assert result.returncode == 1
    assert 'resume <id>' in result.stderr
    result = run_client(engine_command, '/zoom 0\n/not-a-command\n/quit\n')
    assert result.returncode == 0
    assert 'Start or resume a session' in result.stdout
    assert 'Unknown command' in result.stdout


def test_ansi_and_control_sequences_are_data_not_terminal_instructions():
    assert plain('\x1b[31mhello\x1b[0m\x1b]52;clipboard\x07\x9b\x00world') == 'helloworld'


@pytest.mark.parametrize('value', ['nan', 'inf', '-1'])
def test_invalid_timeout_fails_before_spawn(value):
    result = subprocess.run([sys.executable, '-m', 'tessera', '--request-timeout', value],
                            cwd=ROOT, text=True, capture_output=True, timeout=10)
    assert result.returncode != 0
    assert 'finite and nonnegative' in result.stderr


@pytest.mark.parametrize('payload', ['not-json\n', '{"jsonrpc":"2.0","id":true,"result":{}}\n', '{"jsonrpc":"2.0","id":1,"result":NaN}\n'])
def test_bad_frames_and_ids_fail_without_echoing_engine_data(tmp_path, payload):
    script = tmp_path / 'bad_engine.py'
    script.write_text('import sys\nsys.stdin.readline()\nsys.stdout.write(' + repr(payload) + ')\nsys.stdout.flush()\n')
    with RpcClient([sys.executable, str(script)], timeout=2) as peer:
        with pytest.raises(RpcError):
            peer.call('initialize')
    assert peer.process.poll() is not None


def test_frame_limit_and_shutdown_bound_are_enforced(tmp_path):
    script = tmp_path / 'oversized.py'
    script.write_text('import sys,time\nsys.stdin.readline()\nprint("x" * 4096,flush=True)\ntime.sleep(30)\n')
    with RpcClient([sys.executable, str(script)], timeout=1, max_frame_bytes=1024, shutdown_timeout=0.1) as peer:
        with pytest.raises(RpcError, match='oversized'):
            peer.call('initialize')
    assert peer.process.poll() is not None
    assert peer.process.returncode < 0

REAL_DAEMON = r'''
import json
import socket
import sys
from pathlib import Path
from tessera.config import Config
from tessera.daemon import Daemon
from tessera.rpc import serve
sys.path.insert(0, str(Path.cwd() / 'tests'))
from fake_backend import FakeBackend
from local_memory import test_local_memory

def forbidden(*args, **kwargs):
    raise AssertionError('No network or listener belongs to this fixture')
socket.socket.connect = forbidden
socket.socket.connect_ex = forbidden
socket.socket.bind = forbidden
config = Config(home=Path(sys.argv[1]), cwd=Path(sys.argv[1]))
config.daemon.history_page_size = 1

def answer(messages, tools):
    text = json.dumps(messages)
    if 'Second durable question' in text:
        assert 'First durable question' in text
        return {'content': 'History survived the daemon restart.'}
    return {'content': 'First durable answer.'}

with test_local_memory():
    serve(Daemon(config, backend_factory=lambda *args: FakeBackend([answer])))
assert 'tessera.server' not in sys.modules
'''


def test_terminal_actual_daemon_restarts_resumes_history_and_uses_existing_view(tmp_path):
    command = [sys.executable, '-u', '-c', REAL_DAEMON, str(tmp_path)]
    first = run_client(command, 'First durable question\n/view\n/zoom 0+1\n/quit\n', 'chat', '--session', 'durable')
    assert first.returncode == 0, first.stdout + first.stderr
    assert 'First durable answer.' in first.stdout
    assert 'First durable question' in first.stdout
    second = run_client(command, 'Second durable question\n/view\n/quit\n', 'resume', 'durable')
    assert second.returncode == 0, second.stdout + second.stderr
    assert second.stdout.count('user: First durable question') == 1
    assert second.stdout.count('agent: First durable answer.') == 1
    assert 'History survived the daemon restart.' in second.stdout
    bounded = run_client(command, '/quit\n', '--history-limit', '1', 'resume', 'durable')
    assert bounded.returncode == 0, bounded.stdout + bounded.stderr
    assert 'Showing 1 of 4 saved messages' in bounded.stdout
    assert 'agent: History survived the daemon restart.' in bounded.stdout
    assert 'user: First durable question' not in bounded.stdout
    logs = list((tmp_path / 'sessions' / 'durable' / 'log').glob('*.jsonl'))
    stored = []
    for log in logs:
        stored.extend(json.loads(line) for line in log.read_text().splitlines())
    assert [row['text'] for row in stored] == [
        'First durable question', 'First durable answer.',
        'Second durable question', 'History survived the daemon restart.',
    ]


def test_actual_terminal_tty_tool_prompt_controls_the_daemon_write(tmp_path):
    import pty
    import select
    from fake_backend import fake_http_server

    tool = {'choices': [{'message': {'content': None, 'tool_calls': [
        {'id': 'call-placeholder', 'type': 'function', 'function': {
            'name': 'write_file', 'arguments': json.dumps({'path': 'approved.txt', 'text': 'approved fixture'}),
        }},
    ]}}]}
    done = {'choices': [{'message': {'content': 'The approved fixture is saved.'}}]}
    environment = {}
    for name, value in os.environ.items():
        if not name.startswith('TESSERA_'):
            environment[name] = value
    environment['NO_COLOR'] = '1'
    from integration_helpers import FakeMcpServer
    with FakeMcpServer(dialect='path') as memory, fake_http_server([(200, tool), (200, done)]) as (url, received):
        config = 'cwd=' + json.dumps(str(tmp_path)) + '\n'
        config += f'[backends.fixture]\nbase_url="{url}"\nmodel="fixture-placeholder"\n[roles]\nmain="fixture"\n'
        config += ('[aethermind]\nendpoints = [' + json.dumps(memory.url) + ']\n'
                   'probe_timeout = 0.5\nprobe_interval = 0\n')
        (tmp_path / 'config.toml').write_text(config)
        master, slave = pty.openpty()
        process = subprocess.Popen([sys.executable, '-m', 'tessera', '--home', str(tmp_path), 'chat', '--session', 'approval-fixture'],
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
                chunk = os.read(master, 65536)
                assert chunk, transcript
                transcript += chunk.decode()
        try:
            until('> ')
            os.write(master, b'Write a file with write_file to save the approved fixture\n')
            until('Allow this tool once? [y/N]')
            assert not (tmp_path / 'approved.txt').exists()
            os.write(master, b'yes\n')
            until('The approved fixture is saved.')
            os.write(master, b'/quit\n')
            assert process.wait(timeout=5) == 0
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)
            os.close(master)
        assert (tmp_path / 'approved.txt').exists(), transcript
        assert (tmp_path / 'approved.txt').read_text() == 'approved fixture'
        assert len(received) == 2


def test_zoom_prints_reported_continuation_and_sends_paging_options(engine_command):
    result = run_client(engine_command, 'Question\n/zoom 0+1 --limit 4\n/zoom 0+1 --offset 4 --limit 100\n/quit\n')
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'Orig\nMore source remains. Continue with /zoom 0+1 --offset 4 --limit 4' in result.stdout
    assert 'inal source text.' in result.stdout
    assert result.stdout.count('More source remains.') == 1
