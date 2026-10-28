"""Parent-written plans and real local git work from offline child providers."""

import json
from pathlib import Path
import sys

import pytest

from fake_backend import FakeBackend
from test_agent_worktrees import git, setup
from test_daemon_agents import make_daemon
from tessera.config import AccountConfig, BackendConfig


def configured(tmp_path):
    cfg, repo, _ = setup(tmp_path)
    cfg.accounts['fixture'] = AccountConfig('fixture', provider_kind='cli_worker', limits={'max_concurrent': 2})
    cfg.backends['worker'] = BackendConfig('worker', account='fixture')
    cfg.roles = {'main': 'worker'}
    cfg.swarm.enabled = True
    cfg.swarm.lease_seconds = 60
    cfg.agents.enabled = True
    cfg.agents.max_concurrent = 2
    cfg.agents.planner_tokens = 20
    cfg.agents.token_budget = 500
    cfg.agents.wall_seconds = 30
    cfg.agents.worker_backend = 'worker'
    cfg.agents.allowed_scopes = ['piece_a.py', 'piece_b.py']
    cfg.channels.enabled = True
    return cfg, repo


@pytest.mark.parametrize("semantic", [False, True])
@pytest.mark.parametrize("structured", [False, True])
def test_parent_proposal_creates_worktree_children_and_checked_per_piece_merges(tmp_path, monkeypatch, semantic, structured):
    cfg, repo = configured(tmp_path)
    cfg.agents.worktree_result_format = 'edits_json' if structured else 'native'
    cfg.agents.commit_children = structured
    judge_calls = []
    if semantic:
        from test_jev_backend import typed_answers
        cfg.jev.enabled = True
        cfg.roles['jev'] = 'worker'
        cfg.agents.judge_tokens = 10
        def judge(backend, messages, tools=None, **options):
            packet = json.loads(messages[-1]['content'])
            judge_calls.append(packet)
            answers = typed_answers(packet['questions'])
            for key in answers:
                if key.startswith('independent_'):
                    answers[key]['noul'] = 1
            return {'content': json.dumps({'answers': answers}), 'usage': {'total_tokens': 1}}
        monkeypatch.setattr('tessera.backend.Backend.chat', judge)
    calls = []
    proposal = {'mode': 'parallel', 'workers': [
        {'objective': 'Implement piece A', 'scope': ['piece_a.py']},
        {'objective': 'Implement piece B', 'scope': ['piece_b.py']}]}
    def factory(slug, backend):
        def reply(messages, tools):
            calls.append(slug)
            if slug == 'parent':
                # The planner answers with JSON, so it is called without tools.
                assert not tools
                return {'content': json.dumps(proposal), 'usage': {'total_tokens': 1}}
            # A spawned worker gets this session's tools unless a shorter list is
            # named, and can write files (54188dc). Text-only children were the
            # empty-allowlist stop, not the design.
            names = {tool['function']['name'] for tool in tools}
            assert {'shell', 'read_file', 'write_file'} <= names
            index = int(slug.rsplit('-', 1)[1])
            run_id = slug.split('-')[1]
            root = cfg.home / 'agent-worktrees' / run_id / str(index)
            name = ['piece_a.py', 'piece_b.py'][index]
            if structured:
                return {'content': json.dumps({'edits': [{'path': name, 'old': '', 'new': 'VALUE = 1\n'}]}),
                        'usage': {'total_tokens': 1}}
            (root / name).write_text('VALUE = 1\n')
            git(root, 'add', name)
            git(root, 'commit', '-qm', 'Synthetic child implementation')
            return {'content': 'Committed and checked fixture implementation', 'usage': {'total_tokens': 1}}
        return FakeBackend([reply])
    daemon = make_daemon(cfg, factory)
    original_head = git(repo, 'rev-parse', 'HEAD')
    try:
        parent = daemon.dispatch('thread/start', {'title': 'Parent'})['thread']['id']
        request = {'threadId': parent, 'objective': 'Implement both pieces', 'backlog': 'Two independent module fixtures', 'requestId': 'd' * 32}
        run = daemon.dispatch('agent/plan', request)['run']
        assert calls == [parent] and run['planning']['source'] == 'backend'
        assert daemon.dispatch('agent/plan', request)['run'] == run
        children = [w['threadId'] for w in run['workers']]
        daemon.dispatch('agent/send', {'fromThreadId': children[0], 'toThreadId': children[1], 'text': 'Independent scopes confirmed'})
        executed = daemon.dispatch('agent/run', {'runId': run['id']})['run']
        assert executed['status'] == 'ready' and executed['merged'] is None
        result = daemon.dispatch('agent/merge', {'runId': run['id']})['run']
        assert result['status'] == 'succeeded'
        assert all(w['landing']['status'] == 'merged' and w['landing']['integration_checks'] for w in result['workers'])
        assert result['usage']['tokens'] == (6 if semantic else 3)
        assert len(judge_calls) == (3 if semantic else 0)
        if semantic:
            assert all(w['landing']['gate']['source'] == 'jev' for w in result['workers'])
            assert all('VALUE = 1' in q['state']['artifacts'][0]['content'] for q in judge_calls[1:])
        assert len(calls) == 3
        target = Path(result['workspace']['parent']['path'])
        assert (target / 'piece_a.py').exists() and (target / 'piece_b.py').exists()
        assert git(repo, 'rev-parse', 'HEAD') == original_head
        assert (repo / 'dirty.txt').exists() and not (repo / 'piece_a.py').exists()
        daemon.dispatch('agent/merge', {'runId': run['id']})
        assert len(calls) == 3
    finally:
        daemon.close()


def test_malformed_parent_plan_is_charged_but_never_repeated_or_executed(tmp_path):
    cfg, repo = configured(tmp_path)
    provider = FakeBackend([{'content': 'invalid JSON', 'usage': {'total_tokens': 2}}])
    daemon = make_daemon(cfg, lambda *a: provider)
    try:
        parent = daemon.dispatch('thread/start', {'title': 'Parent'})['thread']['id']
        request = {'threadId': parent, 'objective': 'Implement pieces', 'backlog': 'Fixture backlog', 'requestId': 'e' * 32}
        with pytest.raises(ValueError):
            daemon.dispatch('agent/plan', request)
        with pytest.raises(ValueError, match='interrupted or failed'):
            daemon.dispatch('agent/plan', request)
        assert len(provider.calls) == 1
        assert len(daemon.dispatch('thread/list', {})['data']) == 1
        task = daemon.agents._scheduler_instance().store.get('agent-' + 'e' * 32 + '-plan')
        assert task['status'] == 'failed' and task['usage']['tokens'] == 2
    finally:
        daemon.close()


def test_large_structured_context_does_not_expand_bounded_run_record(tmp_path):
    cfg, repo = configured(tmp_path)
    cfg.agents.worktree_result_format = 'edits_json'
    cfg.agents.commit_children = True
    cfg.agents.worktree_context_files = ['context.txt']
    (repo / 'context.txt').write_text('source context\n' * 5000)
    git(repo, 'add', 'context.txt')
    git(repo, 'commit', '-qm', 'Large read-only fixture context')
    proposal = {'mode': 'parallel', 'workers': [
        {'objective': 'Implement A', 'scope': ['piece_a.py']},
        {'objective': 'Implement B', 'scope': ['piece_b.py']}]}
    def factory(slug, name):
        if slug == 'parent':
            return FakeBackend([{'content': json.dumps(proposal), 'usage': {'total_tokens': 1}}])
        name = 'piece_' + ('a' if slug.endswith('-0') else 'b') + '.py'
        return FakeBackend([{'content': json.dumps({'edits': [{'path': name, 'old': '', 'new': 'VALUE = 1\n'}]}),
                            'usage': {'total_tokens': 1}}])
    daemon = make_daemon(cfg, factory)
    try:
        parent = daemon.dispatch('thread/start', {'title': 'Parent'})['thread']['id']
        run = daemon.dispatch('agent/plan', {'threadId': parent, 'objective': 'Build pieces', 'backlog': 'Independent pieces'})['run']
        run = daemon.dispatch('agent/run', {'runId': run['id']})['run']
        assert run['status'] == 'ready'
        assert all(len(w['execution_input_sha256']) == 64 and 'execution_input' not in w for w in run['workers'])
        assert len(json.dumps(run).encode()) < cfg.swarm.max_artifact_bytes
    finally:
        daemon.close()


def test_successful_cli_auth_path_survives_child_validation_failure(tmp_path, monkeypatch):
    from tessera.agent_budget import AgentBackend
    cfg, repo = configured(tmp_path)
    cfg.agents.worktree_result_format = 'edits_json'
    cfg.agents.commit_children = True
    cfg.agents.validation_commands = [[sys.executable, '-c', 'raise SystemExit(1)']]
    proposal = {'mode': 'parallel', 'workers': [
        {'objective': 'Implement A', 'scope': ['piece_a.py']},
        {'objective': 'Implement B', 'scope': ['piece_b.py']}]}
    def factory(slug, name):
        if slug == 'parent':
            return FakeBackend([{'content': json.dumps(proposal), 'usage': {'total_tokens': 1}}])
        name = 'piece_' + ('a' if slug.endswith('-0') else 'b') + '.py'
        return FakeBackend([{'content': json.dumps({'edits': [{'path': name, 'old': '', 'new': 'VALUE = 1\n'}]}),
                            'usage': {'total_tokens': 1}}])
    original = AgentBackend.chat
    def completed(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        self.worker.auth_path = 'cli_login'
        return result
    monkeypatch.setattr(AgentBackend, 'chat', completed)
    daemon = make_daemon(cfg, factory)
    try:
        parent = daemon.dispatch('thread/start', {'title': 'Parent'})['thread']['id']
        run = daemon.dispatch('agent/plan', {'threadId': parent, 'objective': 'Build pieces', 'backlog': 'Independent pieces'})['run']
        run = daemon.dispatch('agent/run', {'runId': run['id']})['run']
        assert all(w['status'] == 'failed' and w['auth_path'] == 'cli_login' for w in run['workers'])
        assert all(w['artifact'] is None for w in run['workers'])
    finally:
        daemon.close()
