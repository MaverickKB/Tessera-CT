"""Real daemon/CLI subprocess pipes with synthetic local vendor executables."""

import json
import os
from pathlib import Path
import sys

from integration_helpers import FakeMcpServer
from tessera.rpc_client import RpcClient


def test_two_session_agents_messages_merge_and_killed_attachment_recovery(tmp_path, monkeypatch):
    for key in list(os.environ):
        if key.startswith('TESSERA_'):
            monkeypatch.delenv(key)
    command = tmp_path / 'claude'
    command.write_text('#!' + sys.executable + '''
import json,sys
prompt=sys.stdin.read()
text='ALPHA' if 'Reply ALPHA' in prompt else 'BETA'
print(json.dumps({'type':'result','subtype':'success','is_error':False,
                  'result':text,'usage':{'input_tokens':2,'output_tokens':1}}))
''')
    command.chmod(0o700)
    (tmp_path / 'config.toml').write_text('''
[accounts.fixture]
provider_kind = "cli_worker"
[accounts.fixture.limits]
max_concurrent = 2
[backends.worker]
account = "fixture"
[backends.worker.cli]
vendor = "claude"
command = ''' + json.dumps(str(command)) + '''
cwd = ''' + json.dumps(str(tmp_path)) + '''
timeout = 5
[roles]
main = "worker"
[swarm]
enabled = true
max_concurrent = 2
initial_concurrency = 2
job_workers = 2
lease_seconds = 30
[agents]
enabled = true
max_concurrent = 2
token_budget = 100
wall_seconds = 10.0
[channels]
enabled = true
[aethermind]
endpoints = ["%s"]
probe_timeout = 0.5
probe_interval = 0
''')
    argv = [sys.executable, '-m', 'tessera.app_server', '--home', str(tmp_path)]
    def client():
        peer = RpcClient(argv, timeout=15)
        peer.call('initialize')
        peer.notification('initialized')
        return peer
    with FakeMcpServer(dialect='path') as memory:
        text = (tmp_path / 'config.toml').read_text()
        (tmp_path / 'config.toml').write_text(text % memory.url)
        peer = client()
        try:
            parent = peer.call('thread/start', {'title': 'Pipe fixture'})['thread']['id']
            run = peer.call('agent/spawn', {'threadId': parent, 'objective': 'Independent ALPHA and BETA', 'workers': [
                {'objective': 'Reply ALPHA', 'scope': ['alpha']}, {'objective': 'Reply BETA', 'scope': ['beta']} ]})['run']
            children = [w['threadId'] for w in run['workers']]
            peer.call('agent/send', {'fromThreadId': children[0], 'toThreadId': children[1], 'text': 'Scoped observation'})
            result = peer.call('agent/run', {'runId': run['id']})['run']
            assert result['status'] == 'succeeded' and result['usage']['tokens'] == 6
            assert [a['content'] for a in result['merged']['artifacts']] == ['ALPHA', 'BETA']
            sources = [p for slug in [parent, *children] for p in (tmp_path / 'sessions' / slug).rglob('*')
                       if p.is_file() and (p.parent.name == 'log' or p.name == 'view.json')]
            before = {p: p.read_bytes() for p in sources}
            peer.process.kill()
            peer.process.wait(timeout=5)
        finally:
            peer.close()
        with client() as resumed:
            for slug in [parent, *children]:
                assert resumed.call('thread/resume', {'threadId': slug})['messages']
            assert resumed.call('agent/show', {'runId': run['id']})['run'] == result
            assert before == {p: p.read_bytes() for p in sources}
            assert resumed.call('channel/events')['data']
            assert resumed.call('channel/deliver') == {'delivered': 0}
