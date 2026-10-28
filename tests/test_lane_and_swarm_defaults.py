"""Local agent lanes follow the endpoint limit. Swarm defaults do not bind YOLO.

A hand-set number holds in Safe mode and in YOLO mode. A default this harness
adds applies only in Safe mode. YOLO mode drops it.
"""

import threading

import pytest

from fake_backend import FakeBackend
from tessera.config import BackendConfig, load_config
from tessera.loop import Session
from tessera.sessions import SessionManager

from test_agent_selection import configured


def _release_chat(release):
    def chat(backend, messages, tools=None, **kwargs):
        assert release.wait(15)
        return {'content': 'Done', 'tool_calls': []}
    return chat


def _local_lane(tmp_path, *, limit, yolo):
    """Cloud root, so the local lane's full width is the endpoint limit alone."""
    release = threading.Event()
    config = configured(tmp_path)
    config.tools.approval = 'yolo' if yolo else 'ask'
    config.agents.max_concurrent = 0
    config.agents.concurrency_by_kind = {}
    config.backends['cloud'] = BackendConfig(
        name='cloud', model='cloud', base_url='https://models.example.invalid/v1', api_key_env='FIXTURE_CLOUD_KEY')
    config.backends['local'] = BackendConfig(
        name='local', model='local', base_url='http://127.0.0.1:9/v1',
        max_concurrent_requests=limit)
    config.roles['main'] = 'cloud'
    return config, release


def _spawn_local(session, count):
    for index in range(count):
        session.agents.spawn(f'local-{index}', 'Wait', backend='local')


def test_hand_set_endpoint_limit_is_the_only_local_lane_cap(tmp_path):
    """2 is the cap in Safe and in YOLO. Swarm numbers pinned to 1 are not."""
    for yolo in (False, True):
        config, release = _local_lane(tmp_path / ('yolo' if yolo else 'safe'), limit=2, yolo=yolo)
        with Session(config, backend=FakeBackend([]),
                     agent_backend_factory=lambda: FakeBackend([_release_chat(release)] * 8)) as session:
            try:
                _spawn_local(session, 2)
                with pytest.raises(RuntimeError, match='concurrency limit'):
                    session.agents.spawn('local-overflow', 'Wait', backend='local')
                assert session.agents.pool.running == 2
            finally:
                release.set()
                assert session.agents.wait(20)


def test_omitted_endpoint_limit_does_not_refuse_local_subagents(tmp_path):
    """Safe mode's one-request default is the model slot, not a lane that refuses children.

    An omitted max_concurrent_requests leaves the lane open in Safe mode and in
    YOLO mode. A number set by hand is the cap, covered above.
    """
    for yolo in (False, True):
        config, release = _local_lane(tmp_path / ('yolo' if yolo else 'safe'), limit=None, yolo=yolo)
        with Session(config, backend=FakeBackend([]),
                     agent_backend_factory=lambda: FakeBackend([_release_chat(release)] * 8)) as session:
            try:
                _spawn_local(session, 4)
                assert session.agents.pool.running == 4
            finally:
                release.set()
                assert session.agents.wait(20)


def test_retired_local_serving_slots_is_noted_and_not_applied(tmp_path, monkeypatch):
    (tmp_path / 'config.toml').write_text(
        '[agents]\nlocal_serving_slots = 4\n'
        '[backends.local]\nbase_url = "http://127.0.0.1:9/v1"\nmodel = "local"\n'
        '[roles]\nmain = "local"\n'
    )
    loaded = load_config(tmp_path)
    assert not hasattr(loaded.agents, 'local_serving_slots')
    assert loaded.migration_notes
    note = loaded.migration_notes[0]
    assert 'agents.local_serving_slots' in note
    assert '4' in note
    assert 'max_concurrent_requests' in note
    # The saved 4 is not the lane width. Safe mode's omitted endpoint allows 1.
    from tessera.endpoint_limit import request_limit
    assert request_limit(loaded, 'local') == 1

    monkeypatch.setenv('TESSERA_AGENTS_LOCAL_SERVING_SLOTS', '9')
    env_home = tmp_path / 'env'
    env_home.mkdir()
    (env_home / 'config.toml').write_text('[agents]\nlocal_serving_slots = 4\n')
    noted = load_config(env_home)
    assert any('local_serving_slots' in item and '9' in item for item in noted.migration_notes)
    assert not hasattr(noted.agents, 'local_serving_slots')


def _jobs(tmp_path, *, yolo, tasks, distinct):
    from tessera.config import AccountConfig, Config
    from tessera.jobs import SwarmJobs
    config = Config(home=tmp_path, cwd=tmp_path)
    config.swarm.enabled = True
    config.tools.approval = 'yolo' if yolo else 'ask'
    config.swarm.max_job_tasks = tasks
    config.swarm.distinct_workers = distinct
    config.accounts['alpha'] = AccountConfig(name='alpha')
    config.backends['alpha'] = BackendConfig(
        name='alpha', account='alpha', base_url='https://models.example.invalid/a', model='alpha')
    return SwarmJobs(config, backend_factory=lambda _: pytest.fail('bounds are checked before execution'))


def _workers(count):
    return [{'backend': 'alpha', 'objective': f'Look at {index}', 'scope': [f'path-{index}'],
             'budget': {'tokens': 1}} for index in range(count)]


def test_max_job_tasks_default_is_safe_only_and_a_hand_set_binds_both(tmp_path):
    from tessera.log import ChatLog
    safe = _jobs(tmp_path / 'safe', yolo=False, tasks=None, distinct=False)
    with ChatLog(tmp_path / 'safe-log') as log:
        safe.create('eight', _workers(8), session_slug='subject', log=log)
        with pytest.raises(ValueError, match='bounded'):
            safe.create('nine', _workers(9), session_slug='subject', log=log)

    yolo = _jobs(tmp_path / 'yolo', yolo=True, tasks=None, distinct=False)
    with ChatLog(tmp_path / 'yolo-log') as log:
        yolo.create('twelve', _workers(12), session_slug='subject', log=log)

    for mode in (False, True):
        home = tmp_path / ('hand-yolo' if mode else 'hand-safe')
        jobs = _jobs(home, yolo=mode, tasks=3, distinct=False)
        with ChatLog(home / 'log') as log:
            jobs.create('three', _workers(3), session_slug='subject', log=log)
            with pytest.raises(ValueError, match='bounded'):
                jobs.create('four', _workers(4), session_slug='subject', log=log)


def test_distinct_workers_default_is_a_safe_guard_and_a_saved_value_holds(tmp_path):
    """Distinct backends and accounts are independent evidence, not a judge rule.

    Overlapping scopes stay refused in both modes.
    """
    from tessera.log import ChatLog
    safe = _jobs(tmp_path / 'safe', yolo=False, tasks=None, distinct=None)
    with pytest.raises(ValueError, match='distinct'):
        with ChatLog(tmp_path / 'safe-log') as log:
            safe.create('shared', _workers(2), session_slug='subject', log=log)

    yolo = _jobs(tmp_path / 'yolo', yolo=True, tasks=None, distinct=None)
    with ChatLog(tmp_path / 'yolo-log') as log:
        yolo.create('shared', _workers(2), session_slug='subject', log=log)

    saved_off = _jobs(tmp_path / 'off', yolo=False, tasks=None, distinct=False)
    with ChatLog(tmp_path / 'off-log') as log:
        saved_off.create('shared', _workers(2), session_slug='subject', log=log)

    saved_on = _jobs(tmp_path / 'on', yolo=True, tasks=None, distinct=True)
    with pytest.raises(ValueError, match='distinct'):
        with ChatLog(tmp_path / 'on-log') as log:
            saved_on.create('shared', _workers(2), session_slug='subject', log=log)

    overlap = _workers(2)
    overlap[1]['scope'] = ['path-0/child']
    loose = _jobs(tmp_path / 'overlap', yolo=True, tasks=None, distinct=False)
    with pytest.raises(ValueError, match='overlap'):
        with ChatLog(tmp_path / 'overlap-log') as log:
            loose.create('overlap', overlap, session_slug='subject', log=log)


def test_job_bound_settings_say_which_mode_the_default_binds(tmp_path):
    (tmp_path / 'config.toml').write_text('[tools]\napproval = "ask"\n')
    manager = SessionManager(load_config(tmp_path))
    try:
        models = next(section for section in manager.settings_view()['sections'] if section['id'] == 'models')
        tasks = next(field for field in models['fields'] if field['id'] == 'swarm.max_job_tasks')
        distinct = next(field for field in models['fields'] if field['id'] == 'swarm.distinct_workers')
        assert tasks['value'] == ''
        assert '8' in tasks['status'] and 'Safe mode' in tasks['status'] and 'Unleashed' in tasks['status']
        assert 'by hand' in tasks['status']
        assert distinct['value'] == ''
        assert 'Safe mode' in distinct['status'] and 'Unleashed' in distinct['status']
        assert 'distinct' in distinct['hint'] and 'judge' in distinct['hint']
        assert 'evidence' in distinct['hint']

        saved = manager.apply_settings({'id': 'swarm.max_job_tasks', 'value': '3'})
        assert saved['changed'] is True
        assert load_config(tmp_path).swarm.max_job_tasks == 3
        tasks = next(field for field in next(
            section for section in manager.settings_view()['sections'] if section['id'] == 'models'
        )['fields'] if field['id'] == 'swarm.max_job_tasks')
        assert tasks['value'] == '3'
        assert 'by hand' in tasks['status'] and 'Unleashed' in tasks['status']

        cleared = manager.apply_settings({'id': 'swarm.max_job_tasks', 'value': ''})
        assert cleared['changed'] is True
        assert load_config(tmp_path).swarm.max_job_tasks is None

        with pytest.raises(ValueError, match='integer'):
            manager.apply_settings({'id': 'swarm.max_job_tasks', 'value': '1'})
    finally:
        manager.close()


def test_saved_distinct_choice_round_trips_and_zero_job_size_is_refused(tmp_path):
    (tmp_path / 'config.toml').write_text('[tools]\napproval = "yolo"\n')
    manager = SessionManager(load_config(tmp_path))
    try:
        models = next(section for section in manager.settings_view()['sections'] if section['id'] == 'models')
        tasks = next(field for field in models['fields'] if field['id'] == 'swarm.max_job_tasks')
        assert 'no cap' in tasks['status'].lower() or 'does not add' in tasks['status']
        assert '8' not in tasks['status'] or 'does not add' in tasks['status']
        distinct = next(field for field in models['fields'] if field['id'] == 'swarm.distinct_workers')
        assert distinct['value'] == ''
        chosen = manager.apply_settings({'id': 'swarm.distinct_workers', 'value': 'false'})
        assert chosen['changed'] is True
        assert load_config(tmp_path).swarm.distinct_workers is False
        with pytest.raises(ValueError, match='integer'):
            manager.apply_settings({'id': 'swarm.max_job_tasks', 'value': '0'})
    finally:
        manager.close()


def test_retired_pool_numbers_are_refused_instead_of_dropped(tmp_path):
    from tessera.settings_model import accounts_field
    text = '''
[accounts.pool]
provider_kind = "cli_worker"

[accounts.pool.limits]
max_concurrent = 24
initial_concurrency = 24
'''
    (tmp_path / 'config.toml').write_text(text)
    config = load_config(tmp_path)
    assert 'max_concurrent' not in config.accounts['pool'].limits
    assert any('accounts.pool.limits.max_concurrent' in note for note in config.migration_notes)
    field = accounts_field(config)
    record = field['records'][0]
    keys = {spec['key'] for spec in record['fields']}
    assert 'max_concurrent' not in keys and 'initial_concurrency' not in keys
    manager = SessionManager(config)
    try:
        with pytest.raises(ValueError, match='max_concurrent_requests'):
            manager.apply_settings({
                'id': 'named_accounts', 'action': 'edit', 'name': 'pool',
                'record': {'max_concurrent': 16, 'initial_concurrency': 2,
                           'provider_kind': 'cli_worker'},
            })
    finally:
        manager.close()
    saved = load_config(tmp_path).accounts['pool']
    assert 'max_concurrent' not in saved.limits
    assert saved.provider_kind == 'cli_worker'
