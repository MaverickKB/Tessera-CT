"""Session-backed swarm consumers; providers and semantic gates stay offline."""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from cli_fixtures import synthetic_codex_auth
from fake_backend import FakeBackend
from tessera.config import Config, AccountConfig, BackendConfig
from tessera.daemon import Daemon


def configured(tmp_path):
    cfg = Config(home=tmp_path, cwd=tmp_path)
    cfg.accounts['pool'] = AccountConfig(name='pool', provider_kind='cli_worker', limits={'max_concurrent': 2})
    cfg.backends['worker'] = BackendConfig(name='worker', account='pool')
    cfg.roles = {'main': 'worker'}
    cfg.swarm.enabled = True
    cfg.swarm.max_attempts = 1
    cfg.swarm.lease_seconds = 60
    cfg.agents.enabled = True
    cfg.agents.token_budget = 100
    cfg.agents.judge_tokens = 0
    cfg.agents.wall_seconds = 10.0
    cfg.agents.view_bytes = 512
    cfg.channels.enabled = True
    return cfg


def workers():
    return [{'objective': 'Reply ALPHA', 'scope': ['alpha']},
            {'objective': 'Reply BETA', 'scope': ['beta']}]


def make_daemon(cfg, factory):
    daemon = Daemon(cfg, backend_factory=factory)
    daemon.peer = SimpleNamespace(notify=lambda *args: None)
    return daemon


def test_disabled_and_single_plans_never_construct_a_child_provider(tmp_path):
    calls = []
    daemon = make_daemon(Config(home=tmp_path, cwd=tmp_path), lambda *a: calls.append(a))
    try:
        assert daemon.dispatch('agent/list', {}) == {'data': []}
        slug = daemon.dispatch('thread/start', {'title': 'Parent'})['thread']['id']
        with pytest.raises(ValueError):
            daemon.dispatch('agent/spawn', {'threadId': slug, 'objective': 'Simple answer', 'workers': workers()})
        assert calls == [] and not (tmp_path / 'agent-runs').exists()
    finally:
        daemon.close()
    cfg = configured(tmp_path)
    daemon = make_daemon(cfg, lambda *a: calls.append(a))
    try:
        result = daemon.dispatch('agent/spawn', {'threadId': slug, 'objective': 'Single answer', 'workers': workers()[:1]})['run']
        assert result['mode'] == 'single' and result['workers'] == []
        assert calls == []
        assert len(daemon.dispatch('thread/list', {})['data']) == 1
    finally:
        daemon.close()


def test_parallel_children_have_logs_views_links_messages_ordered_merge_and_restart(tmp_path):
    cfg = configured(tmp_path)
    barrier = threading.Barrier(2, timeout=5)
    started = []
    def factory(slug, name):
        def reply(messages, tools):
            started.append(slug)
            barrier.wait()
            word = 'ALPHA' if messages[-1]['content'] == 'Reply ALPHA' else 'BETA'
            return {'content': word, 'usage': {'input_tokens': 3, 'output_tokens': 2}}
        return FakeBackend([reply])
    daemon = make_daemon(cfg, factory)
    saved = {}
    try:
        parent = daemon.dispatch('thread/start', {'title': 'Parent'})['thread']['id']
        run = daemon.dispatch('agent/spawn', {'threadId': parent, 'objective': 'Independent ALPHA and BETA together',
                                             'workers': workers(), 'requestId': 'a' * 32})['run']
        assert run['mode'] == 'parallel' and run['status'] == 'queued'
        children = [w['threadId'] for w in run['workers']]
        assert len(set(children)) == 2 and not started
        assert daemon.dispatch('agent/spawn', {'threadId': parent, 'objective': 'Independent ALPHA and BETA together',
                                               'workers': workers(), 'requestId': 'a' * 32})['run']['id'] == run['id']
        sent = daemon.dispatch('agent/send', {'fromThreadId': children[0], 'toThreadId': children[1],
                                              'text': 'A scoped fixture observation', 'messageId': 'fixture-message'})['message']
        assert sent['hop'] == 1
        daemon.dispatch('agent/send', {'fromThreadId': children[0], 'toThreadId': children[1],
                                       'text': 'A scoped fixture observation', 'messageId': 'fixture-message'})
        result = daemon.dispatch('agent/run', {'runId': run['id']})['run']
        assert result['status'] == 'succeeded' and result['merge_gate']['allowed'] is True
        assert len(started) == 2
        assert [a['content'] for a in result['merged']['artifacts']] == ['ALPHA', 'BETA']
        assert result['usage']['tokens'] == 10
        for child in children:
            rows = daemon.dispatch('thread/read', {'threadId': child})['messages']
            assert any(row['meta'].get('parent_thread_id') == parent for row in rows)
            work = [r for r in rows if r['meta'].get('work_id') == 'fixture-message']
            assert len(work) == 1
            assert daemon.dispatch('view/read', {'threadId': child})['lines']
            assert daemon.manager._metadata[child]['agent']['parentThreadId'] == parent
        assert daemon.manager.channels.pending() == []
        for slug in [parent, *children]:
            saved[slug] = daemon.dispatch('thread/read', {'threadId': slug})['messages']
            root = daemon.manager.path(slug)
            assert (root / 'view.json').exists()
            saved[slug + '-bytes'] = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*')
                                     if p.is_file() and (p.parent.name == 'log' or p.name == 'view.json')}
    finally:
        daemon.close()
    restarted = make_daemon(cfg, lambda *args: FakeBackend([]))
    try:
        assert restarted.dispatch('agent/show', {'runId': run['id']})['run']['status'] == 'succeeded'
        for slug in [parent, *children]:
            assert restarted.dispatch('thread/resume', {'threadId': slug})['messages'] == saved[slug]
            root = restarted.manager.path(slug)
            assert {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*')
                    if p.is_file() and (p.parent.name == 'log' or p.name == 'view.json')} == saved[slug + '-bytes']
    finally:
        restarted.close()


def test_dependent_conflicting_and_over_budget_plans_never_fan_out(tmp_path):
    cfg = configured(tmp_path)
    daemon = make_daemon(cfg, lambda *args: pytest.fail('Admission cannot execute workers'))
    try:
        parent = daemon.dispatch('thread/start', {'title': 'Parent'})['thread']['id']
        dependent = workers()
        dependent[1]['dependsOn'] = [0]
        for plan in [dependent, [dict(w, scope=['shared']) for w in workers()]]:
            run = daemon.dispatch('agent/spawn', {'threadId': parent, 'objective': 'Sequential fixture', 'workers': plan})['run']
            assert run['mode'] == 'single' and not run['workers']
            assert run['admission_gate']['source'] == 'deterministic'
        excessive = [dict(w, budget={'tokens': 99}) for w in workers()]
        with pytest.raises(ValueError):
            daemon.dispatch('agent/spawn', {'threadId': parent, 'objective': 'Too expensive', 'workers': excessive})
        assert len(daemon.dispatch('thread/list', {})['data']) == 1
    finally:
        daemon.close()


def test_missing_or_partial_usage_is_conservatively_charged(tmp_path):
    cfg = configured(tmp_path)
    daemon = make_daemon(cfg, lambda *a: FakeBackend([{'content': 'Evidence', 'usage': {'input_tokens': 2}}]))
    try:
        parent = daemon.dispatch('thread/start', {'title': 'Budget evidence'})['thread']['id']
        run = daemon.dispatch('agent/spawn', {'threadId': parent, 'objective': 'Independent evidence', 'workers': workers()})['run']
        result = daemon.dispatch('agent/run', {'runId': run['id']})['run']
        assert result['status'] == 'succeeded'
        assert result['usage']['tokens'] == cfg.agents.token_budget
        assert all(not c['tokens_reported'] for c in result['charges'].values())
    finally:
        daemon.close()


def test_restart_fences_uncertain_attempt_instead_of_reexecuting(tmp_path):
    cfg = configured(tmp_path)
    daemon = make_daemon(cfg, lambda *a: pytest.fail('Recovery cannot execute a provider'))
    parent = daemon.dispatch('thread/start', {'title': 'Recovery'})['thread']['id']
    run = daemon.dispatch('agent/spawn', {'threadId': parent, 'objective': 'Independent evidence', 'workers': workers()})['run']
    scheduler = daemon.agents._scheduler_instance()
    for w in run['workers']:
        task = scheduler.store.create(w['objective'], w['scope'], w['budget'], w['account'], task_id=w['taskId'])
        scheduler.enqueue(task['id'])
        assert scheduler.claim_next(w['account'], 'old-attachment', task_ids={task['id']})
    daemon.close()
    reopened = make_daemon(cfg, lambda *a: pytest.fail('Uncertain claims must not replay'))
    try:
        result = reopened.dispatch('agent/run', {'runId': run['id']})['run']
        assert result['status'] == 'failed' and result['merged'] is None
        assert result['usage']['tokens'] == cfg.agents.token_budget
        assert all(w['attempt'] == 1 and w['error_category'] == 'timeout' for w in result['workers'])
    finally:
        reopened.close()


def test_merge_republication_recovers_without_repeating_workers(tmp_path, monkeypatch):
    cfg = configured(tmp_path)
    daemon = make_daemon(cfg, lambda *a: FakeBackend([{'content': 'Evidence', 'usage': {'total_tokens': 1}}]))
    try:
        parent = daemon.dispatch('thread/start', {'title': 'Publish recovery'})['thread']['id']
        run = daemon.dispatch('agent/spawn', {'threadId': parent, 'objective': 'Independent evidence', 'workers': workers()})['run']
        original = daemon.agents._log
        def fail_merge(slug, text, meta, identity):
            if identity.startswith('merge-'):
                raise OSError('Synthetic lost merge publication')
            return original(slug, text, meta, identity)
        monkeypatch.setattr(daemon.agents, '_log', fail_merge)
        with pytest.raises(OSError):
            daemon.dispatch('agent/run', {'runId': run['id']})
        assert daemon.dispatch('agent/show', {'runId': run['id']})['run']['status'] == 'succeeded'
        monkeypatch.setattr(daemon.agents, '_log', original)
        daemon.dispatch('agent/run', {'runId': run['id']})
        daemon.dispatch('agent/run', {'runId': run['id']})
        rows = daemon.dispatch('thread/read', {'threadId': parent})['messages']
        assert len([r for r in rows if r['meta'].get('agent_event_id') == 'merge-' + run['id']]) == 1
    finally:
        daemon.close()


def test_completed_task_recovers_artifact_receipt_but_rejects_tampering(tmp_path):
    cfg = configured(tmp_path)
    daemon = make_daemon(cfg, lambda *a: FakeBackend([{'content': 'Evidence', 'usage': {'total_tokens': 1}}]))
    try:
        parent = daemon.dispatch('thread/start', {'title': 'Artifact recovery'})['thread']['id']
        run = daemon.dispatch('agent/spawn', {'threadId': parent, 'objective': 'Independent evidence', 'workers': workers()})['run']
        result = daemon.dispatch('agent/run', {'runId': run['id']})['run']
        lost = json.loads(json.dumps(result))
        lost.update(status='running', merged=None, merge_gate=None)
        lost['workers'][0]['artifact'] = None
        daemon.agents._save(lost)
        recovered = daemon.dispatch('agent/run', {'runId': run['id']})['run']
        assert recovered['status'] == 'succeeded'
        artifact = recovered['workers'][0]['artifact']
        (daemon.agents._path(run['id']) / artifact['file']).write_text('{}')
        with pytest.raises(ValueError, match='integrity'):
            daemon.dispatch('agent/run', {'runId': run['id']})
    finally:
        daemon.close()


def test_run_plan_and_messages_redact_configured_environment_credentials(tmp_path, monkeypatch):
    from tessera.credentials import CredentialReference
    cfg = configured(tmp_path)
    cfg.credentials['fixture'] = CredentialReference(env='FIXTURE_AGENT_KEY')
    monkeypatch.setenv('FIXTURE_AGENT_KEY', 'fixture-sensitive-value')
    daemon = make_daemon(cfg, lambda *a: FakeBackend([]))
    try:
        parent = daemon.dispatch('thread/start', {'title': 'Redaction'})['thread']['id']
        plan = workers()
        plan[0]['objective'] += ' fixture-sensitive-value'
        run = daemon.dispatch('agent/spawn', {'threadId': parent, 'objective': 'Independent fixture-sensitive-value', 'workers': plan})['run']
        assert 'fixture-sensitive-value' not in json.dumps(run)
        assert '[REDACTED]' in run['objective']
        child = run['workers'][0]['threadId']
        daemon.dispatch('agent/send', {'fromThreadId': parent, 'toThreadId': child, 'text': 'fixture-sensitive-value'})
        page = daemon.dispatch('channel/events', {})
        assert page['data'] and 'fixture-sensitive-value' not in json.dumps(page)
        assert daemon.dispatch('channel/events', {'after': page['nextAfter']})['data'] == []
    finally:
        daemon.close()


def test_synthetic_cli_is_bounded_by_agent_wall_budget(tmp_path):
    import sys
    import time
    cfg = configured(tmp_path)
    executable = tmp_path / 'codex'
    executable.write_text('#!' + sys.executable + '\nimport sys,time\nsys.stdin.read()\ntime.sleep(30)\n')
    executable.chmod(0o700)
    cfg.backends['worker'].extra = {'cli': {'vendor': 'codex', 'command': str(executable),
                                           'cwd': str(tmp_path), 'timeout': 20,
                                           'auth_path': synthetic_codex_auth(tmp_path)}}
    cfg.agents.wall_seconds = 3.0
    daemon = Daemon(cfg)
    daemon.peer = SimpleNamespace(notify=lambda *args: None)
    try:
        parent = daemon.dispatch('thread/start', {'title': 'Timeout'})['thread']['id']
        run = daemon.dispatch('agent/spawn', {'threadId': parent, 'objective': 'Independent timeouts', 'workers': workers()})['run']
        start = time.monotonic()
        result = daemon.dispatch('agent/run', {'runId': run['id']})['run']
        assert time.monotonic() - start < 8
        assert result['status'] == 'failed' and result['usage']['tokens'] == cfg.agents.token_budget
        for w in result['workers']:
            runtime = daemon.manager.runtime(w['threadId'])
            errors = [r for r in runtime.events.records if r['event'] == 'error']
            assert errors[-1]['data']['error_category'] == 'timeout'
    finally:
        daemon.close()


def test_whole_plan_account_budget_rejects_before_any_judge_or_worker(tmp_path, monkeypatch):
    from unittest.mock import Mock
    cfg = configured(tmp_path)
    cfg.accounts['pool'].budget = {'tokens': 80}
    cfg.jev.enabled = True
    cfg.roles['jev'] = 'worker'
    cfg.agents.judge_tokens = 10
    factory = Mock(side_effect=AssertionError('Unaffordable plans cannot execute'))
    judge = Mock(side_effect=AssertionError('Unaffordable plans cannot judge'))
    monkeypatch.setattr('tessera.backend.Backend.chat', judge)
    daemon = make_daemon(cfg, factory)
    try:
        parent = daemon.dispatch('thread/start', {'title': 'Account budget'})['thread']['id']
        with pytest.raises(ValueError, match='account budget'):
            daemon.dispatch('agent/spawn', {'threadId': parent, 'objective': 'Independent work', 'workers': workers()})
        factory.assert_not_called()
        judge.assert_not_called()
        assert len(daemon.dispatch('thread/list', {})['data']) == 1
    finally:
        daemon.close()


def test_twelve_cli_workers_have_no_child_count_bound(tmp_path):
    cfg = configured(tmp_path)
    cfg.agents.token_budget = 1200
    cfg.accounts['pool'].limits = {'max_concurrent': 0}
    cfg.jev.batch_items = 128
    barrier = threading.Barrier(12, timeout=10)
    started = []
    def factory(*args):
        def reply(messages, tools):
            started.append(True)
            barrier.wait()
            return 'Done'
        return FakeBackend([reply])
    daemon = make_daemon(cfg, factory)
    try:
        candidates = [{'objective': f'Worker {i}', 'scope': [f'file-{i}']} for i in range(12)]
        assert len(daemon.agents._normalize(candidates)) == 12
        parent = daemon.dispatch('thread/start', {'title': 'Parent'})['thread']['id']
        run = daemon.dispatch('agent/spawn', {'threadId': parent, 'objective': 'Independent work',
                                             'workers': candidates})['run']
        assert len(run['workers']) == 12
        result = daemon.dispatch('agent/run', {'runId': run['id']})['run']
        assert result['status'] == 'succeeded'
        assert len(started) == 12
    finally:
        daemon.close()
