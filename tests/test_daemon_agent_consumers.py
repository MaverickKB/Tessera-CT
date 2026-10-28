"""Independent negative-path consumers of the documented daemon agent RPCs."""

from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from fake_backend import FakeBackend
from tessera.backend import BackendError
from tessera.config import AccountConfig, BackendConfig, Config
from tessera.daemon import Daemon
from tessera.rpc import RpcError
from test_jev_backend import typed_answers


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    guards = []
    for target in ('urllib.request.urlopen', 'urllib.request.OpenerDirector.open',
                   'tessera.credentials.CredentialResolver.resolve', 'subprocess.Popen'):
        guard = Mock(side_effect=AssertionError('Agent contracts may not start external work'))
        monkeypatch.setattr(target, guard)
        guards.append(guard)
    yield
    for guard in guards:
        guard.assert_not_called()


def config_for(tmp_path, *, semantic=False):
    config = Config(home=tmp_path, cwd=tmp_path)
    config.accounts['fixture-pool'] = AccountConfig('fixture-pool', provider_kind='cli_worker',
                                                   limits={'max_concurrent': 2})
    config.backends['fixture-worker'] = BackendConfig('fixture-worker', account='fixture-pool')
    config.roles = {'main': 'fixture-worker'}
    config.swarm.enabled = True
    config.swarm.max_attempts = 1
    config.swarm.lease_seconds = 30
    config.agents.enabled = True
    config.agents.max_depth = 1
    config.agents.token_budget = 200
    config.agents.judge_tokens = 10 if semantic else 0
    config.agents.wall_seconds = 10.0
    config.agents.view_bytes = 256
    config.channels.enabled = True
    config.channels.auto_deliver = False
    config.jev.enabled = semantic
    if semantic:
        config.roles['jev'] = 'fixture-worker'
    return config


def candidates():
    return [{'objective': 'Give the ALPHA fixture result', 'scope': ['alpha'], 'budget': {'tokens': 40}},
            {'objective': 'Give the BETA fixture result', 'scope': ['beta'], 'budget': {'tokens': 40}}]


def attach(config, factory):
    daemon = Daemon(config, backend_factory=factory)
    daemon.peer = SimpleNamespace(notify=lambda *args: None)
    return daemon


def spawn(daemon, parent, workers=None, request_id=None):
    params = {'threadId': parent, 'objective': 'Combine independent fixture results',
              'workers': candidates() if workers is None else workers}
    if request_id:
        params['requestId'] = request_id
    return daemon.dispatch('agent/spawn', params)['run']


def source_snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*')
            if p.is_file() and p.suffix in ('.json', '.jsonl')}


def assert_no_merge_publication(daemon, parent, run_id):
    rows = daemon.dispatch('thread/read', {'threadId': parent})['messages']
    assert not [row for row in rows if row['meta'].get('agent_event_id') == 'merge-' + run_id]
    journal = daemon.manager.path(parent) / 'events.jsonl'
    events = [json.loads(line) for line in journal.read_text().splitlines()]
    assert not [row for row in events if row['event'] == 'agent/merge'
                and row['data'].get('agent_run_id') == run_id]


def fake_semantics(monkeypatch, *, deny=None, unavailable=False):
    calls = []
    def chat(backend, messages, tools=None, **options):
        assert tools is None
        packet = json.loads(messages[-1]['content'])
        calls.append(packet)
        if unavailable:
            raise BackendError('Synthetic unavailable judge', 'connection')
        questions = packet['questions']
        answers = typed_answers(questions)
        for name in questions:
            if name.startswith('independent_'):
                answers[name]['noul'] = 0 if deny == 'admission' else 1
            if name == 'goal_met' and deny == 'merge' and packet['state'].get('artifacts'):
                answers[name] = {'type': 'score', 'score': 0, 'confidence': 1,
                                 'probabilities': {'0': 1, '1': 0, '2': 0}}
        return {'content': json.dumps({'answers': answers}), 'tool_calls': [],
                'finish_reason': 'stop', 'usage': {'total_tokens': 1}}
    monkeypatch.setattr('tessera.backend.Backend.chat', chat)
    return calls


@pytest.mark.parametrize('case', ['one-worker', 'dependency', 'scope-overlap'])
def test_single_agent_policy_control_never_creates_children_or_loads_provider(tmp_path, case):
    factory = Mock(side_effect=AssertionError('A single-agent decision cannot create a worker'))
    daemon = attach(config_for(tmp_path), factory)
    try:
        parent = daemon.dispatch('thread/start', {'title': 'Control'})['thread']['id']
        plan = candidates()
        if case == 'one-worker':
            plan = plan[:1]
        elif case == 'dependency':
            plan[1]['dependsOn'] = [0]
        else:
            plan[1]['scope'] = ['alpha/nested']
        run = spawn(daemon, parent, plan)
        assert run['mode'] == 'single' and run['workers'] == []
        assert len(daemon.dispatch('thread/list', {})['data']) == 1
        assert daemon.dispatch('agent/list', {'threadId': parent})['data'] == [run]
        factory.assert_not_called()
    finally:
        daemon.close()


def test_queued_plan_reads_replays_and_conflicting_request_are_passive(tmp_path):
    factory = Mock(side_effect=AssertionError('Plan inspection must not load a provider'))
    config = config_for(tmp_path)
    daemon = attach(config, factory)
    try:
        parent = daemon.dispatch('thread/start', {'title': 'Passive parent'})['thread']['id']
        run = spawn(daemon, parent, request_id='b' * 32)
        assert run['mode'] == 'parallel' and run['status'] == 'queued'
        ids = [row['threadId'] for row in run['workers']]
        assert len(set(ids)) == 2
        before = source_snapshot(tmp_path)
        assert spawn(daemon, parent, request_id='b' * 32) == run
        assert daemon.dispatch('agent/show', {'runId': run['id']})['run'] == run
        assert daemon.dispatch('agent/list', {'threadId': parent})['data'] == [run]
        different = candidates()
        different[0]['objective'] = 'A different fixture request'
        with pytest.raises(ValueError):
            spawn(daemon, parent, different, request_id='b' * 32)
        assert source_snapshot(tmp_path) == before
        factory.assert_not_called()
    finally:
        daemon.close()
    reopened = attach(config, factory)
    try:
        assert reopened.dispatch('agent/show', {'runId': run['id']})['run'] == run
        assert source_snapshot(tmp_path) == before
        assert len(reopened.dispatch('thread/list', {})['data']) == 3
        factory.assert_not_called()
    finally:
        reopened.close()


@pytest.mark.parametrize('unavailable', [False, True])
def test_semantic_admission_deny_or_unavailable_cannot_authorize_fanout(tmp_path, monkeypatch, unavailable):
    semantic_calls = fake_semantics(monkeypatch, deny='admission', unavailable=unavailable)
    factory = Mock(side_effect=AssertionError('Rejected semantic admission must not load children'))
    daemon = attach(config_for(tmp_path, semantic=True), factory)
    try:
        parent = daemon.dispatch('thread/start', {'title': 'Denied fixture'})['thread']['id']
        run = spawn(daemon, parent)
        assert run['mode'] == 'single' and not run['workers']
        assert not run['admission_gate']['allowed']
        assert run['admission_gate']['source'] == ('unavailable' if unavailable else 'jev')
        assert len(semantic_calls) == 1
        assert len(daemon.dispatch('thread/list', {})['data']) == 1
        factory.assert_not_called()
    finally:
        daemon.close()


def test_child_turn_and_automatic_message_reply_cannot_bypass_claims(tmp_path):
    config = config_for(tmp_path)
    config.channels.auto_reply = config.channels.auto_deliver = True
    factory = Mock(side_effect=AssertionError('Queued child cannot execute outside the coordinator'))
    daemon = attach(config, factory)
    try:
        parent = daemon.dispatch('thread/start', {'title': 'Claim parent'})['thread']['id']
        run = spawn(daemon, parent)
        child = run['workers'][0]['threadId']
        before = daemon.dispatch('thread/read', {'threadId': child})['messages']
        with pytest.raises((ValueError, RpcError)):
            daemon.dispatch('turn/start', {'threadId': child,
                'input': [{'type': 'text', 'text': 'Unclaimed fixture task'}]})
        assert daemon.dispatch('thread/read', {'threadId': child})['messages'] == before
        message = daemon.dispatch('agent/send', {'fromThreadId': parent, 'toThreadId': child,
            'text': 'Can you confirm the fixture?', 'messageId': 'fixture-claim-message'})['message']
        assert message['message_id'] == 'fixture-claim-message'
        assert daemon.manager.channels.pending() == []
        assert daemon.dispatch('agent/show', {'runId': run['id']})['run']['status'] == 'queued'
        factory.assert_not_called()
    finally:
        daemon.close()


def test_semantic_merge_rejection_never_publishes_a_successful_merge(tmp_path, monkeypatch):
    fake_semantics(monkeypatch, deny='merge')
    executed = []
    def factory(slug, backend):
        executed.append(slug)
        return FakeBackend([{'content': 'Synthetic fixture evidence', 'usage': {'total_tokens': 5}}])
    config = config_for(tmp_path, semantic=True)
    daemon = attach(config, factory)
    try:
        parent = daemon.dispatch('thread/start', {'title': 'Merge parent'})['thread']['id']
        run = spawn(daemon, parent)
        assert run['mode'] == 'parallel'
        result = daemon.dispatch('agent/run', {'runId': run['id']})['run']
        assert result['status'] != 'succeeded'
        assert result['merge_gate']['allowed'] is False
        assert not result.get('merged')
        assert len(executed) == 2
        assert_no_merge_publication(daemon, parent, run['id'])
    finally:
        daemon.close()
    reopened = attach(config, Mock(side_effect=AssertionError('Denied merge readback cannot execute')))
    try:
        assert reopened.dispatch('agent/show', {'runId': run['id']})['run'] == result
        assert_no_merge_publication(reopened, parent, run['id'])
    finally:
        reopened.close()


def test_worker_token_overrun_cannot_be_merged_or_automatically_retried(tmp_path):
    executed = []
    def factory(slug, backend):
        executed.append(slug)
        return FakeBackend([{'content': 'Synthetic over-budget reply', 'usage': {'total_tokens': 1000}}])
    config = config_for(tmp_path)
    daemon = attach(config, factory)
    try:
        parent = daemon.dispatch('thread/start', {'title': 'Budget parent'})['thread']['id']
        run = spawn(daemon, parent)
        result = daemon.dispatch('agent/run', {'runId': run['id']})['run']
        assert result['status'] == 'failed' and not result.get('merged')
        attempts = len(executed)
        assert attempts and attempts <= 2
        assert daemon.dispatch('agent/run', {'runId': run['id']})['run'] == result
        assert len(executed) == attempts
        assert_no_merge_publication(daemon, parent, run['id'])
    finally:
        daemon.close()
    reopened = attach(config, Mock(side_effect=AssertionError('Failed run readback cannot execute')))
    try:
        assert reopened.dispatch('agent/show', {'runId': run['id']})['run'] == result
        assert_no_merge_publication(reopened, parent, run['id'])
    finally:
        reopened.close()


def test_child_depth_and_plan_token_caps_reject_without_new_sessions(tmp_path):
    factory = Mock(side_effect=AssertionError('Rejected plans must not load providers'))
    config = config_for(tmp_path)
    daemon = attach(config, factory)
    try:
        parent = daemon.dispatch('thread/start', {'title': 'Depth parent'})['thread']['id']
        run = spawn(daemon, parent)
        child = run['workers'][0]['threadId']
        before = len(daemon.dispatch('thread/list', {})['data'])
        with pytest.raises(ValueError):
            spawn(daemon, child)
        expensive = deepcopy(candidates())
        expensive[0]['budget']['tokens'] = config.agents.token_budget
        with pytest.raises(ValueError):
            spawn(daemon, parent, expensive)
        assert len(daemon.dispatch('thread/list', {})['data']) == before
        factory.assert_not_called()
    finally:
        daemon.close()


def test_saved_provider_binding_change_rejects_execution_without_repair(tmp_path):
    config = config_for(tmp_path)
    factory = Mock(side_effect=AssertionError('Changed provider binding must not execute'))
    daemon = attach(config, factory)
    try:
        parent = daemon.dispatch('thread/start', {'title': 'Binding parent'})['thread']['id']
        run = spawn(daemon, parent)
    finally:
        daemon.close()
    config.backends['fixture-worker'].extra['cli'] = {'args': ['fixture-changed-selector']}
    before = source_snapshot(tmp_path)
    reopened = attach(config, factory)
    try:
        with pytest.raises(ValueError, match='binding'):
            reopened.dispatch('agent/run', {'runId': run['id']})
        assert reopened.dispatch('agent/show', {'runId': run['id']})['run'] == run
        assert source_snapshot(tmp_path) == before
        factory.assert_not_called()
    finally:
        reopened.close()


def test_completed_artifact_tamper_fails_readback_without_provider_or_repair(tmp_path):
    config = config_for(tmp_path)
    executed = []
    def factory(slug, backend):
        executed.append(slug)
        return FakeBackend([{'content': 'Immutable fixture result', 'usage': {'total_tokens': 5}}])
    daemon = attach(config, factory)
    try:
        parent = daemon.dispatch('thread/start', {'title': 'Artifact parent'})['thread']['id']
        run = spawn(daemon, parent)
        completed = daemon.dispatch('agent/run', {'runId': run['id']})['run']
        assert completed['status'] == 'succeeded' and len(executed) == 2
    finally:
        daemon.close()
    artifact = completed['workers'][0]['artifact']
    path = tmp_path / config.agents.runs_dir / run['id'] / artifact['file']
    path.write_bytes(b'{}')
    before = source_snapshot(tmp_path)
    forbidden = Mock(side_effect=AssertionError('Tampered artifact cannot execute a provider'))
    reopened = attach(config, forbidden)
    try:
        with pytest.raises(ValueError, match='integrity'):
            reopened.dispatch('agent/run', {'runId': run['id']})
        assert source_snapshot(tmp_path) == before
        forbidden.assert_not_called()
    finally:
        reopened.close()


def test_public_a2a_receipt_and_ack_pages_survive_partial_delivery_restart(tmp_path):
    config = config_for(tmp_path)
    forbidden = Mock(side_effect=AssertionError('A2A notifications cannot execute queued children'))
    daemon = attach(config, forbidden)
    try:
        parent = daemon.dispatch('thread/start', {'title': 'A2A parent'})['thread']['id']
        run = spawn(daemon, parent)
        left, right = [row['threadId'] for row in run['workers']]
        params = {'fromThreadId': left, 'toThreadId': right,
                  'text': 'Synthetic A2A check: alpha = 42.', 'messageId': 'fixture-api-message'}
        receipt = daemon.dispatch('agent/send', params)['message']
        assert receipt['message_id'] == 'fixture-api-message' and receipt['recipients'] == [right]
        first = daemon.dispatch('channel/events', {'after': 0, 'limit': 1})
        assert first['enabled'] is True and len(first['data']) == 1
        assert first['data'][0]['op'] == 'message'
        assert first['nextAfter'] == first['data'][0]['sequence']
        assert daemon.dispatch('channel/deliver', {'limit': 1}) == {'delivered': 1}
        assert [(r['session'], r['direction']) for r in daemon.manager.channels.pending()] == [(right, 'incoming')]
    finally:
        daemon.close()
    reopened = attach(config, forbidden)
    try:
        assert reopened.dispatch('channel/deliver', {'limit': 1}) == {'delivered': 1}
        assert reopened.dispatch('channel/deliver', {}) == {'delivered': 0}
        before = source_snapshot(tmp_path)
        cursor, journal = 0, []
        for _ in range(4):
            page = reopened.dispatch('channel/events', {'after': cursor, 'limit': 1})
            if not page['data']:
                assert page['nextAfter'] == cursor
                break
            assert len(page['data']) == 1 and page['nextAfter'] > cursor
            cursor = page['nextAfter']
            journal.extend(page['data'])
        assert [r['op'] for r in journal] == ['message', 'ack', 'ack']
        assert [r['direction'] for r in journal if r['op'] == 'ack'] == ['outgoing', 'incoming']
        assert reopened.dispatch('agent/send', params)['message'] == receipt
        assert source_snapshot(tmp_path) == before
        for slug in (left, right):
            rows = reopened.dispatch('thread/read', {'threadId': slug})['messages']
            work = [r for r in rows if r['meta'].get('work_id') == 'fixture-api-message']
            assert len(work) == 1 and params['text'] in work[0]['text']
        forbidden.assert_not_called()
    finally:
        reopened.close()


@pytest.mark.aethermind_unavailable
def test_parent_plan_refuses_before_the_model_when_aethermind_is_unavailable(tmp_path):
    from tessera.bundled_engine import AetherMindUnavailable

    config = config_for(tmp_path)
    config.agents.planner_tokens = 50
    config.agents.wall_seconds = 20
    provider = FakeBackend(['should not run'])
    factory = Mock(return_value=provider)
    daemon = attach(config, factory)
    try:
        parent = daemon.dispatch('thread/start', {'title': 'No memory'})['thread']['id']
        request = {'threadId': parent, 'objective': 'Implement independent pieces',
                   'backlog': 'Synthetic unfinished module work', 'requestId': 'e' * 32}
        with pytest.raises(AetherMindUnavailable) as refused:
            daemon.dispatch('agent/plan', request)
        message = str(refused.value)
        assert 'AetherMind is not available' in message
        assert 'Fix:' in message
        assert 'bytes present, not started' in message
        assert provider.calls == []
        store = daemon.agents._scheduler_instance().store
        task = store.get('agent-' + request['requestId'] + '-plan')
        assert task['status'] == 'failed'
        assert task['error_category'] == 'aethermind'
        assert 'reservation' not in task
        assert task.get('usage', {}).get('tokens', 0) == 0
        assert task.get('usage', {}).get('seconds', 0) == 0
        for day in (task.get('daily_usage') or {}).values():
            assert day.get('tokens', 0) == 0
            assert day.get('seconds', 0) == 0
        recorded = store.path.read_text(encoding='utf-8')
        assert '"error_category": "aethermind"' in recorded
        assert '"error_category": "provider"' not in recorded
    finally:
        daemon.close()


@pytest.mark.parametrize('finish_reason,tool_calls', [
    ('length', []), ('content_filter', []), ('tool_calls', []),
    ('stop', [{'id': 'fixture-unexecuted-call', 'name': 'fixture'}]),
])
def test_incomplete_parent_proposal_is_charged_without_fanout_or_retry(tmp_path, finish_reason, tool_calls):
    config = config_for(tmp_path)
    config.agents.planner_tokens = 20
    proposal = {'mode': 'parallel', 'workers': candidates()}
    provider = FakeBackend([{'content': json.dumps(proposal), 'finish_reason': finish_reason,
                             'tool_calls': tool_calls, 'usage': {'total_tokens': 2}}])
    factory = Mock(return_value=provider)
    daemon = attach(config, factory)
    try:
        parent = daemon.dispatch('thread/start', {'title': 'Incomplete parent'})['thread']['id']
        request = {'threadId': parent, 'objective': 'Implement independent pieces',
                   'backlog': 'Synthetic unfinished module work', 'requestId': 'f' * 32}
        with pytest.raises(ValueError, match='incomplete'):
            daemon.dispatch('agent/plan', request)
        saved = source_snapshot(tmp_path)
        with pytest.raises(ValueError, match='interrupted or failed'):
            daemon.dispatch('agent/plan', request)
        assert source_snapshot(tmp_path) == saved
        assert len(daemon.dispatch('thread/list', {})['data']) == 1
        task = daemon.agents._scheduler_instance().store.get('agent-' + request['requestId'] + '-plan')
        assert task['status'] == 'failed' and task['usage']['tokens'] == 2
        assert len(provider.calls) == 1
        factory.assert_called_once_with(parent, 'fixture-worker')
    finally:
        daemon.close()
    forbidden = Mock(side_effect=AssertionError('Incomplete parent plans cannot replay after restart'))
    reopened = attach(config, forbidden)
    try:
        before = source_snapshot(tmp_path)
        with pytest.raises(ValueError, match='interrupted or failed'):
            reopened.dispatch('agent/plan', request)
        assert source_snapshot(tmp_path) == before
        assert len(reopened.dispatch('thread/list', {})['data']) == 1
        forbidden.assert_not_called()
    finally:
        reopened.close()
