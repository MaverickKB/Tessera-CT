"""Operator swarm controls; offline success does not claim parent self-building."""

import io
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from tessera.rpc_client import RpcError
from tessera.terminal import Terminal
from test_terminal_agents import REAL_COORDINATION_DAEMON


ROOT = Path(__file__).resolve().parents[1]
RUN = 'd' * 32


def run_record():
    return {'id': RUN, 'parentThreadId': 'parent', 'mode': 'parallel', 'status': 'queued',
            'workers': [{'threadId': 'child', 'backend': 'PRIVATE_FIELD', 'worktree': 'PRIVATE_FIELD'}],
            'admission_gate': {'allowed': True, 'source': 'jev', 'decision': 'PRIVATE_FIELD'},
            'merge_gate': None, 'usage': {'tokens': 7}, 'parent_view': 'PRIVATE_FIELD'}


class Peer:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def call(self, method, params=None, timeout=None):
        self.calls.append((method, params, timeout))
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


def spec_file(tmp_path, **changes):
    spec = {'objective': 'Combine independent ALPHA and BETA outcomes',
            'workers': [{'objective': 'Reply ALPHA', 'scope': ['alpha']},
                        {'objective': 'Reply BETA', 'scope': ['beta']}],
            'requestId': RUN}
    spec.update(changes)
    path = tmp_path / 'operator plan.json'
    path.write_text(json.dumps(spec))
    return path


def test_operator_plan_routes_to_gated_daemon_without_dumping_private_fields(tmp_path):
    path = spec_file(tmp_path)
    peer = Peer({'run': run_record()})
    output = io.StringIO()
    terminal = Terminal(peer, output=output, color='never', turn_timeout=23)
    terminal.thread_id = 'parent'
    terminal.command('/agents spawn ' + json.dumps(str(path)))
    method, params, timeout = peer.calls[0]
    assert method == 'agent/spawn'
    assert params == {'threadId': 'parent', **json.loads(path.read_text())}
    assert timeout == 23
    assert 'Admission: allowed (source=jev)' in output.getvalue()
    assert 'Result merge gate: unreported' in output.getvalue()
    assert 'Git landing receipt: unreported' in output.getvalue()
    assert 'State: queued' in output.getvalue()
    assert 'PRIVATE_FIELD' not in output.getvalue()
    assert len(peer.calls) == 1  # Submitting a plan does not execute a wave.


@pytest.mark.parametrize('action,timeout', [('run', 23), ('show', None), ('merge', 23)])
def test_run_and_inspect_use_run_id_and_distinct_waits(action, timeout):
    peer = Peer({'run': run_record()})
    terminal = Terminal(peer, output=io.StringIO(), turn_timeout=23)
    terminal.command('/agents ' + action + ' ' + RUN)
    assert peer.calls == [('agent/' + action, {'runId': RUN}, timeout)]


@pytest.mark.parametrize('error', [RpcError('Synthetic wait expired'), KeyboardInterrupt()])
def test_ambiguous_execution_never_retries_or_claims_success(error):
    peer = Peer(error)
    output = io.StringIO()
    terminal = Terminal(peer, output=output)
    with pytest.raises(type(error)):
        terminal.command('/agents run ' + RUN)
    assert len(peer.calls) == 1
    assert 'No retry performed.' in output.getvalue()
    assert 'does not confirm cancellation' in output.getvalue()
    assert 'Agent run:' not in output.getvalue()


def test_mismatched_plan_receipt_fails_without_rendering_a_run(tmp_path):
    run = run_record()
    run['id'] = 'e' * 32
    output = io.StringIO()
    peer = Peer({'run': run})
    terminal = Terminal(peer, output=output)
    terminal.thread_id = 'parent'
    with pytest.raises(RpcError, match='matching agent plan'):
        terminal.agent_action(['spawn', str(spec_file(tmp_path))])
    assert 'Agent run:' not in output.getvalue()
    assert len(peer.calls) == 1


def test_parent_planning_passes_backlog_to_daemon_without_selecting_workers(tmp_path):
    backlog = 'Read-only daemon status events; offline connector interfaces.'
    path = tmp_path / 'backlog.json'
    path.write_text(json.dumps({'objective': 'Implement bounded backlog pieces',
                                'backlog': backlog, 'requestId': RUN}))
    peer = Peer({'run': run_record()})
    output = io.StringIO()
    terminal = Terminal(peer, output=output, turn_timeout=23)
    terminal.thread_id = 'parent'
    terminal.agent_action(['plan', str(path)])
    assert peer.calls == [('agent/plan', {'threadId': 'parent', 'objective': 'Implement bounded backlog pieces',
                                         'backlog': backlog, 'requestId': RUN}, 23)]
    assert 'Parent plan request:' in output.getvalue()
    assert 'workers' not in peer.calls[0][1]


@pytest.mark.parametrize('changes', [{'requestId': 'invalid'}, {'extra': 'PRIVATE_FIELD'}])
def test_invalid_plan_does_not_reach_daemon(tmp_path, changes):
    peer = Peer({})
    terminal = Terminal(peer, output=io.StringIO())
    terminal.thread_id = 'parent'
    with pytest.raises(ValueError):
        terminal.agent_action(['spawn', str(spec_file(tmp_path, **changes))])
    assert not peer.calls


def test_coordination_notices_keep_session_fence_and_gate_enums():
    output = io.StringIO()
    terminal = Terminal(Peer({}), output=output)
    terminal.thread_id = 'parent'
    data = {'gate': {'allowed': False, 'source': 'jev', 'reason': 'PRIVATE_FIELD'},
            'worktree': 'PRIVATE_FIELD'}
    terminal.notification('thread/event', {'threadId': 'other', 'event': 'agent/admission', 'data': data})
    assert not output.getvalue()
    terminal.notification('thread/event', {'threadId': 'parent', 'event': 'agent/admission', 'data': data})
    assert 'Gate: denied (source=jev)' in output.getvalue()
    assert 'PRIVATE_FIELD' not in output.getvalue()


@pytest.mark.parametrize('case,expected', [('valid', 'merged'), ('missing', 'incomplete'),
                                         ('failed-check', 'incomplete'), ('denied', 'incomplete')])
def test_landing_receipt_needs_both_checks_and_real_jev_gate(case, expected):
    run = run_record()
    receipt = {'status': 'merged', 'child_commit': 'a' * 40, 'commit': 'b' * 40,
               'gate': {'allowed': True, 'source': 'jev', 'raw': 'PRIVATE_FIELD'},
               'child_checks': [{'command_ordinal': 1, 'exit_code': 0}],
               'integration_checks': [{'command_ordinal': 1, 'exit_code': 0}]}
    if case == 'missing':
        receipt.pop('integration_checks')
    elif case == 'failed-check':
        receipt['child_checks'][0]['exit_code'] = 1
    elif case == 'denied':
        receipt['gate']['allowed'] = False
    run['workers'][0]['landing'] = receipt
    output = io.StringIO()
    terminal = Terminal(Peer({}), output=output)
    terminal.agent_run_summary({'run': run})
    assert 'Git landing receipt: ' + expected in output.getvalue()
    assert ('Integration commit:' in output.getvalue()) == (expected == 'merged')
    assert 'PRIVATE_FIELD' not in output.getvalue()


@pytest.mark.parametrize('state', ['ready', 'partial'])
def test_worktree_states_and_per_piece_gate_source_stay_explicit(state):
    run = run_record()
    run.update(status=state, merge_gate={'allowed': False, 'source': 'per_piece'})
    output = io.StringIO()
    Terminal(Peer({}), output=output).agent_run_summary({'run': run})
    assert 'State: ' + state in output.getvalue()
    assert 'Result merge gate: denied (source=per_piece)' in output.getvalue()
    assert 'Git landing receipt: unreported' in output.getvalue()


def test_actual_terminal_can_submit_execute_inspect_and_resume_offline_daemon_run(tmp_path):
    home = tmp_path / 'private-home'
    argv = [sys.executable, '-u', '-c', REAL_COORDINATION_DAEMON, str(home), 'unseeded']
    environment = dict(os.environ, TESSERA_ENGINE_COMMAND=json.dumps(argv), NO_COLOR='1')
    path = spec_file(tmp_path)
    child = 'agent-' + RUN + '-0'
    script = '/new Parent\n/agents spawn ' + json.dumps(str(path)) + '\n'
    script += '/agents run ' + RUN + '\n/agents show ' + RUN + '\n'
    script += '/agents\n/resume ' + child + '\n/quit\n'
    result = subprocess.run([sys.executable, '-m', 'tessera'], input=script, cwd=ROOT,
                            env=environment, text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'Operator plan request: ' + RUN in result.stdout
    assert 'State: queued' in result.stdout
    assert result.stdout.count('State: succeeded') == 2
    assert 'Admission: allowed' in result.stdout and 'Result merge gate: allowed' in result.stdout
    assert 'Git landing receipt: unreported' in result.stdout
    assert 'Resumed: ' + child in result.stdout
    assert 'agent: ALPHA' in result.stdout
    assert 'Parent session: parent' in result.stdout


def test_actual_terminal_parent_planning_uses_recorded_backend_plan(tmp_path):
    home = tmp_path / 'private-home'
    argv = [sys.executable, '-u', '-c', REAL_COORDINATION_DAEMON, str(home), 'planning']
    environment = dict(os.environ, TESSERA_ENGINE_COMMAND=json.dumps(argv), NO_COLOR='1')
    path = tmp_path / 'parent-backlog.json'
    path.write_text(json.dumps({'objective': 'Combine independent fixture answers',
                                'backlog': 'Produce independent ALPHA and BETA fixtures.', 'requestId': RUN}))
    script = '/new Parent\n/agents plan ' + json.dumps(str(path)) + '\n'
    script += '/agents run ' + RUN + '\n/agents merge ' + RUN + '\n/agents show ' + RUN + '\n/quit\n'
    result = subprocess.run([sys.executable, '-m', 'tessera'], input=script, cwd=ROOT,
                            env=environment, text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'Parent plan request: ' + RUN in result.stdout
    assert 'Parent planning: backend record present' in result.stdout
    assert 'Workers: 2' in result.stdout
    assert 'State: succeeded' in result.stdout
    assert 'Git landing receipt: unreported' in result.stdout
    assert 'No retry performed. Inspect /agents show ' + RUN in result.stdout
    records = []
    for log in (home / 'sessions' / 'parent' / 'log').glob('*.jsonl'):
        records.extend(json.loads(line) for line in log.read_text().splitlines())
    assert any(row['meta'].get('agent_plan_id') == RUN and row['kind'] == 'agent' for row in records)
