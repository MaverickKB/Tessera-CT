"""Durable explicit jobs execute named lanes and merge persisted evidence."""

import json
import threading

import pytest

from fake_backend import FakeBackend
from tessera.config import Config, AccountConfig, BackendConfig
from tessera.log import ChatLog
from tessera.jobs import SwarmJobs


def configured(tmp_path):
    config = Config(home=tmp_path, cwd=tmp_path)
    config.swarm.enabled = True
    config.board.enabled = True
    for name in ('alpha', 'beta'):
        config.accounts[name] = AccountConfig(name=name)
        config.backends[name] = BackendConfig(name=name, account=name, base_url="https://models.example.com/" + name, model=name + "-model-placeholder")
    return config


def workers():
    return [{'backend': 'alpha', 'objective': 'Inspect alpha fixture', 'scope': ['alpha'], 'budget': {'tokens': 20}},
            {'backend': 'beta', 'objective': 'Inspect beta fixture', 'scope': ['beta'], 'budget': {'tokens': 20}}]


def test_parallel_fanout_merges_actual_artifacts_and_survives_reopen(tmp_path):
    config = configured(tmp_path)
    barrier = threading.Barrier(2)
    calls = []
    def factory(settings):
        calls.append((settings.name, settings.account))
        def reply(messages, tools):
            barrier.wait(timeout=3)
            assert not tools
            return {'content': settings.name + ' fixture finding', 'usage': {'total_tokens': 3}}
        return FakeBackend([reply])
    jobs = SwarmJobs(config, backend_factory=factory)
    with ChatLog(tmp_path / 'log') as log:
        job = jobs.create('Join independent fixture findings', workers(), session_slug='subject', log=log)
        result = jobs.run(job['id'], log=log)
        assert result['status'] == 'succeeded'
        assert set(calls) == {('alpha', 'alpha'), ('beta', 'beta')}
        assert len(jobs.tasks.list(status='succeeded')) == 2
        merged = jobs.artifact(job['id'])
        assert [item['content'] for item in merged['artifacts']] == ['alpha fixture finding', 'beta fixture finding']
        assert merged['checks'] and all(merged['checks'].values())
        assert merged['gate']['allowed'] is True
        card = jobs.board.get(job['id'])
        assert card['status'] == 'succeeded' and len(card['artifacts']) == 3
        assert card['session_slug'] == 'subject' and card['log_end'] < log.count()
        reopened = SwarmJobs(config, backend_factory=lambda _: pytest.fail('Completed job cannot replay'))
        assert reopened.run(job['id'], log=log)['status'] == 'succeeded'
        assert reopened.artifact(job['id']) == merged


def test_disabled_job_construction_and_create_do_not_touch_disk_or_backends(tmp_path):
    config = Config(home=tmp_path, cwd=tmp_path)
    jobs = SwarmJobs(config, backend_factory=lambda _: pytest.fail('Disabled job'))
    with pytest.raises(ValueError, match='disabled'):
        jobs.create('fixture', workers(), session_slug='subject', log=None)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('change', ['same_account', 'same_backend', 'overlap', 'empty_providers'])
def test_invalid_fanout_rejects_before_writing_or_execution(tmp_path, change):
    config = configured(tmp_path)
    items = workers()
    if change == 'same_account':
        config.backends['beta'].account = 'alpha'
    elif change == 'same_backend':
        items[1]['backend'] = 'alpha'
    elif change == 'overlap':
        items[1]['scope'] = ['alpha/subpath']
    else:
        config.backends.clear()
    jobs = SwarmJobs(config, backend_factory=lambda _: pytest.fail('Invalid job cannot execute'))
    with pytest.raises(ValueError):
        jobs.create('fixture', items, session_slug='subject', log=None)
    assert list(tmp_path.iterdir()) == []


def test_only_job_tasks_execute_and_failure_is_durable_without_raw_error(tmp_path):
    config = configured(tmp_path)
    config.swarm.max_attempts = 1
    jobs = SwarmJobs(config, backend_factory=lambda s: FakeBackend([RuntimeError('private-exception-placeholder')]))
    unrelated = jobs.tasks.create('Unrelated', ['elsewhere'], {}, 'alpha')
    with ChatLog(tmp_path / 'log') as log:
        job = jobs.create('fixture', workers(), session_slug='subject', log=log)
        result = jobs.run(job['id'], log=log)
        assert result['status'] == 'failed'
        assert jobs.tasks.get(unrelated['id'])['attempt'] == 0
        assert jobs.tasks.get(unrelated['id'])['status'] == 'queued'
        assert jobs.board.get(job['id'])['status'] == 'failed'
    for path in tmp_path.rglob('*'):
        if path.is_file():
            assert b'private-exception-placeholder' not in path.read_bytes()


def test_tampered_child_artifact_blocks_merge_without_rerunning_worker(tmp_path):
    config = configured(tmp_path)
    jobs = SwarmJobs(config, backend_factory=lambda s: FakeBackend([s.name + ' finding']))
    with ChatLog(tmp_path / 'log') as log:
        job = jobs.create('fixture', workers(), session_slug='subject', log=log)
        jobs.run(job['id'], log=log)
        path = jobs.root / job['id'] / 'worker-0-1.json'
        path.write_text('{}')
        with pytest.raises(ValueError, match='artifact'):
            jobs.artifact(job['id'])


def test_completed_child_recovers_parent_crash_without_replaying_calls(tmp_path, monkeypatch):
    config = configured(tmp_path)
    calls = []
    def factory(settings):
        calls.append(settings.name)
        return FakeBackend([settings.name + ' evidence'])
    jobs = SwarmJobs(config, backend_factory=factory)
    with ChatLog(tmp_path / 'log') as log:
        job = jobs.create('fixture', workers(), session_slug='subject', log=log)
        original = jobs._record
        def crash_before_final_snapshot(job, log, status):
            if status == 'succeeded':
                raise OSError('synthetic process crash')
            return original(job, log, status)
        monkeypatch.setattr(jobs, '_record', crash_before_final_snapshot)
        with pytest.raises(OSError):
            jobs.run(job['id'], log=log)
        # Model calls and task finishes are durable even when the parent snapshot is incomplete.
        assert len(jobs.tasks.list(status='succeeded')) == 2
        restored = SwarmJobs(config, backend_factory=lambda _: pytest.fail('No child replay'))
        assert restored.run(job['id'], log=log)['status'] == 'succeeded'
        assert sorted(calls) == ['alpha', 'beta']


def test_explicit_retry_only_replays_failed_lane_and_respects_account_budget(tmp_path):
    config = configured(tmp_path)
    config.swarm.retry_base = 0
    config.swarm.retry_cap = 0
    backend = {'alpha': FakeBackend([RuntimeError('transient'), 'alpha evidence']),
               'beta': FakeBackend(['beta evidence'])}
    jobs = SwarmJobs(config, backend_factory=lambda s: backend[s.name])
    with ChatLog(tmp_path / 'log') as log:
        job = jobs.create('fixture', workers(), session_slug='subject', log=log)
        assert jobs.run(job['id'], log=log)['status'] == 'failed'
        assert [len(backend[k].calls) for k in ('alpha','beta')] == [1,1]
        result = jobs.run(job['id'], log=log)
        # Missing usage charges the declared budget conservatively, so a retry cannot spend it twice.
        assert result['status'] == 'failed'
        assert [len(backend[k].calls) for k in ('alpha','beta')] == [1,1]


def test_changed_provider_binding_and_merge_budget_are_fenced(tmp_path):
    config = configured(tmp_path)
    jobs = SwarmJobs(config, backend_factory=lambda s: FakeBackend([s.name + ' evidence']))
    with ChatLog(tmp_path / 'log') as log:
        job = jobs.create('fixture', workers(), session_slug='subject', log=log)
        config.backends['alpha'].model = 'changed-model-placeholder'
        with pytest.raises(ValueError, match='configuration changed'):
            jobs.run(job['id'], log=log)
        assert jobs.tasks.list() == []


def test_session_entrypoint_runs_fake_plus_synthetic_official_cli_without_parent_runtime(tmp_path):
    import sys
    from tessera.backend import Backend
    from tessera.sessions import SessionManager
    config = configured(tmp_path)
    command = tmp_path / 'claude'
    source = '''import json,sys
prompt=sys.stdin.read()
assert 'Inspect beta fixture' in prompt
print(json.dumps({'type':'result','subtype':'success','is_error':False,
                  'result':'beta CLI evidence','usage':{'input_tokens':2,'output_tokens':3}}))
'''
    command.write_text('#!' + sys.executable + '\n' + source)
    command.chmod(0o700)
    config.accounts['beta'].provider_kind = 'cli_worker'
    config.backends['beta'].extra = {'cli': {'vendor':'claude','command':str(command),'cwd':str(tmp_path)}}
    made = []
    def factory(settings):
        made.append((settings.name, settings.provider_kind, settings.account))
        if settings.name == 'alpha':
            return FakeBackend(['alpha fake evidence'])
        return Backend(settings.name, settings.base_url, settings.model, settings.api_key_env, settings.extra)
    with SessionManager(config, swarm_backend_factory=factory) as manager:
        slug = manager.create('Swarm subject')['slug']
        job = manager.create_swarm_job(slug,'Join fixture evidence',workers())
        assert not made and manager._runtimes == {}
        result = manager.run_swarm_job(job['id'])
        assert result['status'] == 'succeeded' and manager._runtimes == {}
        assert set(made) == {('alpha','openai_chat','alpha'),('beta','cli_worker','beta')}
        assert [a['content'] for a in manager.swarm_jobs.artifact(job['id'])['artifacts']] == ['alpha fake evidence','beta CLI evidence']
        assert manager.status()['task_status']['tasks']
    with ChatLog(tmp_path / 'sessions' / slug / 'log') as log:
        assert all(m.kind == 'work' for m in log.iter())
        assert any(m.meta.get('swarm_job_id') == job['id'] and m.meta.get('status') == 'succeeded' for m in log.iter())


def test_cli_create_run_and_passive_show_use_the_durable_job_entrypoint(tmp_path, monkeypatch, capsys):
    from tessera import cli
    from tessera.sessions import SessionManager
    config = configured(tmp_path)
    with SessionManager(config) as manager:
        slug = manager.create('CLI subject')['slug']
    monkeypatch.setattr(cli, 'load_config', lambda _: config)
    calls = []
    def backend(name, *args):
        calls.append(name)
        return FakeBackend([name + ' CLI contract evidence'])
    monkeypatch.setattr('tessera.jobs.Backend', backend)
    spec = tmp_path / 'spec.json'
    spec.write_text(json.dumps({'objective':'Join fixture findings','workers':workers()}))
    assert cli.main(['swarm-create',str(spec),'--session',slug]) == 0
    created = json.loads(capsys.readouterr().out)
    assert created['status'] == 'queued' and calls == []
    assert cli.main(['swarm-show',created['job_id']]) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'queued' and calls == []
    assert cli.main(['swarm-run',created['job_id']]) == 0
    completed = json.loads(capsys.readouterr().out)
    assert completed['status'] == 'succeeded' and completed['merged'] is True
    assert sorted(calls) == ['alpha','beta']


def test_merge_size_bound_blocks_completion_and_keeps_verified_children(tmp_path):
    config = configured(tmp_path)
    config.swarm.max_artifact_bytes = 500
    jobs = SwarmJobs(config, backend_factory=lambda s: FakeBackend([s.name + ' evidence ' + 'x' * 200]))
    with ChatLog(tmp_path / 'log') as log:
        job = jobs.create('fixture', workers(), session_slug='subject', log=log)
        result = jobs.run(job['id'], log=log)
        assert result['status'] == 'blocked' and result['merged'] is None
        assert len(jobs.tasks.list(status='succeeded')) == 2
        assert len(jobs.board.get(job['id'])['artifacts']) == 2
        assert result['gate']['source'] == 'deterministic' and result['gate']['allowed'] is False


def test_partial_retry_is_explicit_and_reuses_completed_lane(tmp_path):
    config = configured(tmp_path)
    config.swarm.retry_base = 0
    config.swarm.retry_cap = 0
    backends = {'alpha': FakeBackend([RuntimeError('transient'), 'alpha evidence']),
                'beta': FakeBackend(['beta evidence'])}
    items = workers()
    items[0]['budget'] = {}
    jobs = SwarmJobs(config, backend_factory=lambda s: backends[s.name])
    with ChatLog(tmp_path / 'log') as log:
        job = jobs.create('fixture', items, session_slug='subject', log=log)
        assert jobs.run(job['id'], log=log)['status'] == 'blocked'
        assert len(backends['alpha'].calls) == 1 and len(backends['beta'].calls) == 1
        assert jobs.run(job['id'], log=log)['status'] == 'succeeded'
        assert len(backends['alpha'].calls) == 2 and len(backends['beta'].calls) == 1


def test_job_snapshot_without_parent_artifact_refs_recovers_finished_children(tmp_path):
    config = configured(tmp_path)
    jobs = SwarmJobs(config, backend_factory=lambda s: FakeBackend([s.name + ' evidence']))
    with ChatLog(tmp_path / 'log') as log:
        job = jobs.create('fixture', workers(), session_slug='subject', log=log)
        jobs.run(job['id'], log=log)
        path = jobs.root / job['id'] / 'job.json'
        saved = json.loads(path.read_text())
        saved.update(status='running',artifacts={},merged=None)
        path.write_text(json.dumps(saved) + '\n')
        restored = SwarmJobs(config, backend_factory=lambda _: pytest.fail('Cannot replay committed child'))
        assert restored.run(job['id'],log=log)['status'] == 'succeeded'
        assert len(restored.artifact(job['id'])['artifacts']) == 2


def test_merge_gate_requires_goal_evidence_before_human_override():
    from tessera.jev import JudgeKit
    kit = JudgeKit(enabled=False)
    called = []
    result = kit.swarm_gates('Inspect fixture', artifacts=[{'content':'unverified fixture'}],
                             phase='merge', human=lambda gate: called.append(gate) or True)
    assert result['goal_met'] < kit.goal_threshold
    assert result['allowed'] is False and called == []


@pytest.mark.parametrize('approve',[None,True])
def test_configured_jev_merge_uses_saved_content_and_optional_human_gate(tmp_path, monkeypatch, approve):
    from tessera.jev import JudgeKit
    config = configured(tmp_path)
    config.swarm.gates_enabled = True
    config.jev.enabled = True
    config.jev.url = 'https://judge.example.com/evaluate'
    config.jev.model = 'judge-model-placeholder'
    config.jev.api_key_env = 'JUDGE_CREDENTIAL_PLACEHOLDER'
    monkeypatch.setenv('JUDGE_CREDENTIAL_PLACEHOLDER','synthetic-credential-placeholder')
    seen = []
    def evaluate(self,state,questions,key):
        assert key == 'synthetic-credential-placeholder'
        seen.append(state)
        answers = {}
        for name,question in questions.items():
            if question['type'] == 'score':
                answers[name]={'type':'score','score':2,'probabilities':{'0':0,'1':0,'2':1}}
            else:
                favorable = name.startswith('independent_')
                escalate = name == 'escalate_to_human' and bool(state['artifacts'])
                answers[name]={'type':'noul','noul':.95 if favorable or escalate else .05}
        return {'model':'judge-model-placeholder','answers':answers}
    monkeypatch.setattr(JudgeKit,'_evaluate_batch',evaluate)
    callbacks = []
    def human(gate):
        callbacks.append(gate)
        return approve
    jobs = SwarmJobs(config,backend_factory=lambda s:FakeBackend([s.name+' evidence']),
                     human=human if approve is not None else None)
    with ChatLog(tmp_path/'log') as log:
        job=jobs.create('fixture',workers(),session_slug='subject',log=log)
        result=jobs.run(job['id'],log=log)
        assert result['status'] == ('succeeded' if approve else 'blocked')
        assert result['gate']['source'] == 'jev'
        merges=[state for state in seen if state['artifacts']]
        assert len(merges) == 1
        assert [a['content'] for a in merges[0]['artifacts']] == ['alpha evidence','beta evidence']
        assert len(callbacks) == (1 if approve else 0)
    for path in tmp_path.rglob('decisions.jsonl'):
        assert 'alpha evidence' not in path.read_text() and 'synthetic-credential-placeholder' not in path.read_text()


def test_swarm_cli_never_resumes_unrelated_saved_chat_turns(tmp_path, monkeypatch, capsys):
    from tessera import cli
    from tessera.sessions import SessionManager
    config = configured(tmp_path)
    with SessionManager(config) as manager:
        slug = manager.create('Queued subject')['slug']
        journal = manager.path(slug) / 'turns.jsonl'
    journal.write_text(json.dumps({'op':'enqueue','turn':{
        'id':'pending-turn-placeholder','text':'Unrelated saved request','state':'queued'}}) + '\n')
    before = journal.read_bytes()
    monkeypatch.setattr(cli,'load_config',lambda _:config)
    forbidden = []
    def runtime(*args,**kwargs):
        forbidden.append(args)
        raise AssertionError('Swarm CLI cannot resume unrelated chat work')
    monkeypatch.setattr(SessionManager,'runtime',runtime)
    spec = tmp_path/'spec.json'
    spec.write_text(json.dumps({'objective':'fixture','workers':workers()}))
    assert cli.main(['swarm-create',str(spec),'--session',slug]) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'queued'
    assert forbidden == [] and journal.read_bytes() == before
    assert config.server.resume_turns is True


def test_merge_checkpoint_survives_crash_without_rejudging_or_duplicate_board_refs(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from tessera.jev import JudgeKit
    config = configured(tmp_path)
    merges = []
    def gate(objective,**options):
        if options['phase'] == 'merge':
            merges.append(options['artifacts'])
        return {'allowed':True,'source':'fixture','goal_met':.8 + len(merges)/100}
    monkeypatch.setattr(JudgeKit,'from_config',lambda *args,**kwargs:SimpleNamespace(swarm_gates=gate))
    jobs = SwarmJobs(config,backend_factory=lambda s:FakeBackend([s.name+' evidence']))
    with ChatLog(tmp_path/'log') as log:
        job = jobs.create('fixture',workers(),session_slug='subject',log=log)
        record=jobs._record
        def crash(job,log,status):
            if status == 'succeeded':raise OSError('synthetic board boundary crash')
            return record(job,log,status)
        monkeypatch.setattr(jobs,'_record',crash)
        with pytest.raises(OSError):jobs.run(job['id'],log=log)
        restored=SwarmJobs(config,backend_factory=lambda _:pytest.fail('No worker replay'))
        assert restored.run(job['id'],log=log)['status'] == 'succeeded'
        assert len(merges) == 1
        assert len(restored.board.get(job['id'])['artifacts']) == 3


def test_per_backend_caps_fan_out_cloud_wide_and_hold_local_to_its_limit(tmp_path):
    import time
    config = Config(home=tmp_path, cwd=tmp_path)
    config.swarm.enabled = True
    config.swarm.distinct_workers = False
    config.swarm.max_job_tasks = 32
    # A credential decides the default, not the backend's name.
    config.backends['cloud-grok'] = BackendConfig(
        name='cloud-grok', base_url='https://models.example.com/g', model='g', api_key_env='FIXTURE_CLOUD_KEY')
    config.backends['local'] = BackendConfig(
        name='local', base_url='https://models.example.com/l', model='auto',
        max_concurrent_requests=4)
    lock = threading.Lock()
    state = {'now': 0, 'peak': {}}

    def factory(settings):
        def reply(messages, tools):
            with lock:
                state['now'] += 1
                state['peak'][settings.name] = max(state['peak'].get(settings.name, 0), state['now'])
            time.sleep(0.3)
            with lock:
                state['now'] -= 1
            return {'content': settings.name + ' finding'}
        return FakeBackend([reply])

    jobs = SwarmJobs(config, backend_factory=factory)
    with ChatLog(tmp_path / 'log') as log:
        for backend, count in (('cloud-grok', 10), ('local', 7)):
            state['now'] = 0
            items = [{'backend': backend, 'objective': f'part {i}', 'scope': [f'{backend}/{i}']} for i in range(count)]
            job = jobs.create('fan out', items, session_slug='subject', log=log)
            jobs.run(job['id'], log=log)
            assert jobs.get(job['id'])['status'] == 'succeeded'
        assert state['peak']['cloud-grok'] == 10
        assert state['peak']['local'] == 4
