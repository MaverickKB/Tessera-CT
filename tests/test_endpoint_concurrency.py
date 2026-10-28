"""Swarm size is not a concurrency limit. Each model endpoint is."""

import hashlib
import json
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, replace

import pytest

from tessera.backend import Backend, BackendError
from tessera.config import BackendConfig, Config
from tessera.endpoint_limit import LOCAL_MODEL_CONCURRENCY, request_limit
from tessera.job_status import job_status
from tessera.jobs import SwarmJobs, encoded
from tessera.log import ChatLog
from tessera.loop import Session, run_turn
from tessera.settings_model import project
from fake_backend import FakeBackend
from test_loop import session_config


def _swarm(tmp_path):
    """Every former swarm-wide cap is pinned to 1. It must not limit model calls."""
    config = Config(home=tmp_path, cwd=tmp_path)
    config.swarm.enabled = True
    config.swarm.distinct_workers = False
    config.swarm.max_job_tasks = 32
    config.agents.max_concurrent = 1
    return config


def _backend(name, *, local=True, limit=None):
    backend = BackendConfig(
        name=name,
        base_url='https://models.example.com/' + name,
        model=name + '-model',
        api_key_env='' if local else 'FIXTURE_MODEL_KEY',
    )
    backend.max_concurrent_requests = limit
    return backend


def _workers(backend, count):
    return [
        {'backend': backend, 'objective': f'part {i}', 'scope': [f'{backend}/{i}']}
        for i in range(count)
    ]


def _start(config, factory, workers):
    jobs = SwarmJobs(config, backend_factory=factory)
    log = ChatLog(config.home / 'log')
    log.__enter__()
    job = jobs.create('fan out', workers, session_slug='subject', log=log)
    holder = []

    def run_job():
        try:
            holder.append(jobs.run(job['id'], log=log))
        finally:
            log.close()

    runner = threading.Thread(target=run_job)
    runner.start()
    return jobs, job, runner, holder


def test_one_local_model_never_exceeds_its_limit_and_queues_the_rest(tmp_path):
    config = _swarm(tmp_path)
    config.backends['bench'] = _backend('bench', limit=2)
    assert request_limit(config, 'bench') == 2
    limit = 2
    count = 6
    lock = threading.Lock()
    state = {'now': 0, 'peak': 0}
    release = threading.Event()
    entered = threading.Event()

    def factory(settings):
        def reply(messages, tools):
            with lock:
                state['now'] += 1
                state['peak'] = max(state['peak'], state['now'])
                if state['now'] >= limit:
                    entered.set()
            assert release.wait(5)
            with lock:
                state['now'] -= 1
            return {'content': settings.name + ' finding'}

        return FakeBackend([reply] * count)

    jobs, job, runner, holders = _start(config, factory, _workers('bench', count))
    try:
        assert entered.wait(5), 'local model did not reach its own limit'
        deadline = time.time() + 3
        snapshot = None
        while time.time() < deadline:
            snapshot = json.loads((jobs.root / job['id'] / 'job.json').read_text())
            states = [worker.get('dispatch') for worker in snapshot['workers']]
            waiting = [state for state in states if isinstance(state, str) and state.startswith('waiting for ') and state.endswith(' slot')]
            if states.count('running') == limit and len(waiting) == count - limit:
                break
            time.sleep(0.02)
        assert snapshot is not None
        states = [worker.get('dispatch') for worker in snapshot['workers']]
        waiting = [state for state in states if isinstance(state, str) and state.startswith('waiting for ') and state.endswith(' slot')]
        assert states.count('running') == limit
        assert len(waiting) == count - limit
        assert all(state == 'waiting for bench slot' for state in waiting)
        assert state['peak'] == limit
    finally:
        release.set()
    runner.join(5)
    assert not runner.is_alive()
    assert holders[0]['status'] == 'succeeded'
    assert state['peak'] == limit
    report = job_status(config)
    reported = [worker['status'] for worker in report['jobs'][0]['workers']]
    assert reported == ['succeeded'] * count
    assert 'bench' not in json.dumps(report)


def test_different_models_run_together_and_a_cloud_model_ignores_swarm_caps(tmp_path):
    config = _swarm(tmp_path)
    config.backends['bench'] = _backend('bench', local=True)  # unset → conservative default
    config.backends['remote'] = _backend('remote', local=False)  # unset cloud → unlimited
    # A name that says "local" is still cloud when the configured type is oauth.
    config.backends['local-named-cloud'] = _backend('local-named-cloud', local=False)
    assert request_limit(config, 'bench') == LOCAL_MODEL_CONCURRENCY == 1
    assert request_limit(config, 'remote') is None
    assert request_limit(config, 'local-named-cloud') is None
    workers = _workers('bench', 1) + _workers('remote', 4) + _workers('local-named-cloud', 4)
    lock = threading.Lock()
    state = {'now': {}, 'peak': {}}
    # One local request plus eight cloud requests. A swarm cap of 1 cannot pass this barrier.
    together = threading.Barrier(len(workers))

    def factory(settings):
        def reply(messages, tools):
            with lock:
                state['now'][settings.name] = state['now'].get(settings.name, 0) + 1
                state['peak'][settings.name] = max(state['peak'].get(settings.name, 0), state['now'][settings.name])
            together.wait(timeout=5)
            with lock:
                state['now'][settings.name] -= 1
            return {'content': settings.name + ' finding'}

        return FakeBackend([reply])

    _jobs, _job, runner, holders = _start(config, factory, workers)
    runner.join(8)
    assert not runner.is_alive()
    assert holders[0]['status'] == 'succeeded'
    assert state['peak']['bench'] == 1
    assert state['peak']['remote'] == 4
    assert state['peak']['local-named-cloud'] == 4


def test_yolo_leaves_an_unset_local_model_unlimited(tmp_path):
    config = _swarm(tmp_path)
    config.tools.approval = 'yolo'
    config.backends['bench'] = _backend('bench', local=True)
    assert request_limit(config, 'bench') is None
    workers = _workers('bench', 4)
    lock = threading.Lock()
    state = {'now': 0, 'peak': 0}
    together = threading.Barrier(len(workers))

    def factory(settings):
        def reply(messages, tools):
            with lock:
                state['now'] += 1
                state['peak'] = max(state['peak'], state['now'])
            together.wait(timeout=5)
            with lock:
                state['now'] -= 1
            return {'content': 'finding'}

        return FakeBackend([reply])

    _jobs, _job, runner, holders = _start(config, factory, workers)
    runner.join(8)
    assert not runner.is_alive()
    assert holders[0]['status'] == 'succeeded'
    assert state['peak'] == 4


def test_a_constructed_backend_rereads_yolo_and_a_saved_number(tmp_path):
    """The chat client is built once. Safe mode's default must not stay stuck on it."""
    config = _swarm(tmp_path)
    config.tools.approval = 'ask'
    config.backends['bench'] = _backend('bench', local=True)
    settings = config.backend_for()
    backend = Backend(settings.name, settings.base_url, settings.model, settings.api_key_env, settings.extra)
    backend.bind_endpoint(config, 'bench')
    lock = threading.Lock()
    state = {'now': 0, 'peak': 0}
    release = threading.Event()

    def guarded(messages, tools=None, **kwargs):
        with lock:
            state['now'] += 1
            state['peak'] = max(state['peak'], state['now'])
        assert release.wait(5)
        with lock:
            state['now'] -= 1
        return {'content': 'ok'}

    backend._chat_guarded = guarded

    def wave(count):
        state['now'] = 0
        state['peak'] = 0
        release.clear()
        threads = [threading.Thread(target=backend.chat, args=([{'role': 'user', 'content': 'go'}],))
                   for _ in range(count)]
        for thread in threads:
            thread.start()
        return threads

    first = wave(3)
    deadline = time.time() + 3
    while time.time() < deadline and state['peak'] < 1:
        time.sleep(0.01)
    assert state['peak'] == 1
    release.set()
    for thread in first:
        thread.join(5)
        assert not thread.is_alive()

    config.tools.approval = 'yolo'
    second = wave(3)
    deadline = time.time() + 3
    while time.time() < deadline and state['peak'] < 3:
        time.sleep(0.01)
    assert state['peak'] == 3
    release.set()
    for thread in second:
        thread.join(5)
        assert not thread.is_alive()

    config.backends['bench'].max_concurrent_requests = 2
    third = wave(3)
    deadline = time.time() + 3
    while time.time() < deadline and state['peak'] < 2:
        time.sleep(0.01)
    assert state['peak'] == 2
    release.set()
    for thread in third:
        thread.join(5)
        assert not thread.is_alive()


def test_yolo_enforces_a_number_the_user_set(tmp_path):
    config = _swarm(tmp_path)
    config.tools.approval = 'yolo'
    config.backends['bench'] = _backend('bench', limit=2)
    assert request_limit(config, 'bench') == 2
    count = 4
    lock = threading.Lock()
    state = {'now': 0, 'peak': 0}
    release = threading.Event()
    entered = threading.Event()

    def factory(settings):
        def reply(messages, tools):
            with lock:
                state['now'] += 1
                state['peak'] = max(state['peak'], state['now'])
                if state['now'] >= 2:
                    entered.set()
            assert release.wait(5)
            with lock:
                state['now'] -= 1
            return {'content': 'finding'}

        return FakeBackend([reply] * count)

    _jobs, _job, runner, holders = _start(config, factory, _workers('bench', count))
    try:
        assert entered.wait(5)
        assert state['peak'] == 2
    finally:
        release.set()
    runner.join(5)
    assert not runner.is_alive()
    assert holders[0]['status'] == 'succeeded'
    assert state['peak'] == 2


def test_settings_shows_an_editable_per_model_limit_with_an_honest_local_note(tmp_path):
    config = _swarm(tmp_path)
    config.backends['bench'] = _backend('bench', local=True)
    config.backends['remote'] = _backend('remote', local=False)
    body = project(config, {'yolo': False, 'safe_mode': 'ask'})
    providers = next(field for section in body['sections'] for field in section['fields'] if field.get('id') == 'providers')
    by_name = {record['name']: record for record in providers['records']}
    local = next(field for field in by_name['bench']['fields'] if field['key'] == 'max_concurrent_requests')
    cloud = next(field for field in by_name['remote']['fields'] if field['key'] == 'max_concurrent_requests')
    assert local['label'] == 'Max concurrent requests'
    assert local['value'] == ''
    assert '1' in local['status'] and 'safe' in local['status'].lower()
    assert 'hardware' in local['status'].lower()
    assert 'unleashed' in local['status'].lower()
    assert cloud['value'] == ''
    assert 'no cap' in cloud['status'].lower()
    assert 'swarm' in local['hint'].lower() and 'unleashed' in local['hint'].lower()
    blank = next(field for field in providers['blank'] if field['key'] == 'max_concurrent_requests')
    assert 'local' in blank['hint'].lower() and 'cloud' in blank['hint'].lower()
    config.tools.approval = 'yolo'
    yolo_body = project(config, {'yolo': True, 'safe_mode': 'ask'})
    yolo_providers = next(field for section in yolo_body['sections'] for field in section['fields'] if field.get('id') == 'providers')
    yolo_local = next(field for field in next(record for record in yolo_providers['records'] if record['name'] == 'bench')['fields']
                      if field['key'] == 'max_concurrent_requests')
    assert yolo_local['value'] == ''
    assert 'no cap' in yolo_local['status'].lower()
    assert '1 at a time' not in yolo_local['status'].lower()
    config.backends['bench'].max_concurrent_requests = 3
    hand = project(config, {'yolo': True, 'safe_mode': 'ask'})
    hand_providers = next(field for section in hand['sections'] for field in section['fields'] if field.get('id') == 'providers')
    hand_local = next(field for field in next(record for record in hand_providers['records'] if record['name'] == 'bench')['fields']
                      if field['key'] == 'max_concurrent_requests')
    assert hand_local['value'] == '3'
    assert '3' in hand_local['status']
    assert 'by hand' in hand_local['status'].lower() and 'unleashed' in hand_local['status'].lower()
    assert 'no cap' not in hand_local['status'].lower()


def _provider_field(body, name):
    for section in body['settings']['sections']:
        for field in section['fields']:
            if field.get('id') != 'providers':
                continue
            for record in field['records']:
                if record['name'] == name:
                    return next(item for item in record['fields'] if item['key'] == 'max_concurrent_requests')
    raise AssertionError(name)


def test_settings_save_sets_and_clears_a_model_limit(tmp_path):
    """The field is a real edit. Empty restores the type default. Zero is refused."""
    from tessera.config import load_config
    from test_server import call
    from test_settings_model import serve, stop

    server, thread = serve(tmp_path)
    try:
        saved = call(server, 'POST', '/api/settings', {
            'id': 'providers', 'action': 'edit', 'name': 'fake',
            'record': {'max_concurrent_requests': '2'}})
        assert saved[0] == 200, saved
        assert load_config(tmp_path).backends['fake'].max_concurrent_requests == 2
        assert request_limit(load_config(tmp_path), 'fake') == 2
        shown = _provider_field(saved[1], 'fake')
        assert shown['value'] == '2'
        assert 'hardware' in shown['status'].lower() and '2' in shown['status']
        assert 'by hand' in shown['status'].lower() and 'unleashed' in shown['status'].lower()

        cleared = call(server, 'POST', '/api/settings', {
            'id': 'providers', 'action': 'edit', 'name': 'fake',
            'record': {'max_concurrent_requests': ''}})
        assert cleared[0] == 200, cleared
        assert load_config(tmp_path).backends['fake'].max_concurrent_requests is None
        assert request_limit(load_config(tmp_path), 'fake') == LOCAL_MODEL_CONCURRENCY
        assert 'max_concurrent_requests' not in (tmp_path / 'config.toml').read_text()

        refused = call(server, 'POST', '/api/settings', {
            'id': 'providers', 'action': 'edit', 'name': 'fake',
            'record': {'max_concurrent_requests': '0'}})
        assert refused[0] == 400
        message = refused[1].get('error') or refused[1].get('detail') or json.dumps(refused[1])
        assert 'positive integer' in message.lower()
        assert 'swarm' not in message.lower()
        assert load_config(tmp_path).backends['fake'].max_concurrent_requests is None

        added = call(server, 'POST', '/api/settings', {
            'id': 'providers', 'action': 'add',
            'record': {
                'name': 'cloud', 'account': 'other', 'kind': 'openai_chat',
                'base_url': 'https://models.example.com/v1', 'model': 'cloud-model',
                'credential': 'voice', 'chat': False, 'max_concurrent_requests': ''}})
        assert added[0] == 200, added
        assert request_limit(load_config(tmp_path), 'cloud') is None
        capped = call(server, 'POST', '/api/settings', {
            'id': 'providers', 'action': 'edit', 'name': 'cloud',
            'record': {'max_concurrent_requests': '4'}})
        assert capped[0] == 200, capped
        assert request_limit(load_config(tmp_path), 'cloud') == 4
        cloud = _provider_field(capped[1], 'cloud')
        assert cloud['value'] == '4'
        assert 'hardware' not in cloud['status'].lower()
        assert 'by hand' in cloud['status'].lower() and 'unleashed' in cloud['status'].lower()
    finally:
        stop(server, thread)


def _open_local_session(tmp_path):
    """A real session backend, so Stop and a settings save hit the live gate."""
    config = session_config(tmp_path)
    config.tools.approval = 'ask'
    local = BackendConfig(
        name='local', base_url='http://127.0.0.1:9/v1', model='local-model')
    config.backends['local'] = local
    config.roles['main'] = 'local'
    return config


def _binding(config, name):
    selected = replace(config, roles={'main': name}).backend_for()
    return hashlib.sha256(encoded(asdict(selected))).hexdigest()


def test_stop_releases_a_turn_waiting_on_a_slot(tmp_path):
    config = _open_local_session(tmp_path)
    config.backends['local'].max_concurrent_requests = 1
    release = threading.Event()
    holding = threading.Event()
    with Session(config) as session:
        backend = session.backend
        backend._chat_guarded = lambda *args, **kwargs: {'content': 'ok', 'finish_reason': 'stop'}

        def hold():
            from tessera.endpoint_limit import model_slot_value
            with model_slot_value(backend.endpoint_home, backend.name, 1):
                holding.set()
                release.wait(10)

        holder = threading.Thread(target=hold)
        holder.start()
        try:
            assert holding.wait(5)
            started = threading.Event()
            real = backend.chat

            def wrapped(messages, tools=None, **kwargs):
                started.set()
                return real(messages, tools, **kwargs)

            backend.chat = wrapped
            result = {}
            worker = threading.Thread(target=lambda: result.setdefault('reply', run_turn(session, 'Wait on the slot')))
            worker.start()
            assert started.wait(5), 'the turn never reached the model call'
            deadline = time.time() + 1
            while time.time() < deadline and 'reply' not in result:
                time.sleep(0.02)
            assert 'reply' not in result, 'the turn finished without waiting for the slot'
            began = time.monotonic()
            assert session.interrupt() is True
            worker.join(3)
            assert not worker.is_alive()
            assert time.monotonic() - began < 3
            assert result['reply'] == 'Interrupted by the user.'
            assert not release.is_set()
            assert not any('unavailable' in message.text for message in session.log.iter())
        finally:
            release.set()
            holder.join(3)


def test_stop_releases_a_turn_waiting_on_pace(tmp_path):
    config = _open_local_session(tmp_path)
    config.backends['local'].max_concurrent_requests = 4
    config.backends['local'].throttle_requests_per_minute = 1
    with Session(config) as session:
        backend = session.backend
        backend._chat_guarded = lambda *args, **kwargs: {'content': 'ok', 'finish_reason': 'stop'}
        backend.chat([{'role': 'user', 'content': 'prime'}])
        started = threading.Event()
        real = backend.chat

        def wrapped(messages, tools=None, **kwargs):
            started.set()
            return real(messages, tools, **kwargs)

        backend.chat = wrapped
        result = {}
        worker = threading.Thread(target=lambda: result.setdefault('reply', run_turn(session, 'Wait on the pace')))
        worker.start()
        assert started.wait(5), 'the turn never reached the model call'
        deadline = time.time() + 1
        while time.time() < deadline and 'reply' not in result:
            time.sleep(0.02)
        assert 'reply' not in result, 'the turn finished without waiting for the pace window'
        began = time.monotonic()
        assert session.interrupt() is True
        worker.join(3)
        assert not worker.is_alive()
        assert time.monotonic() - began < 3
        assert result['reply'] == 'Interrupted by the user.'


def test_a_cancelled_wait_does_not_fail_over(tmp_path):
    """Stop while queued is operator cancellation, not an unreachable model."""
    config = _open_local_session(tmp_path)
    config.backends['local'].max_concurrent_requests = 1
    with Session(config) as session:
        backend = session.backend
        cancel = threading.Event()
        cancel.set()
        backend.cancel_event = cancel
        with pytest.raises(BackendError) as caught:
            backend.chat([{'role': 'user', 'content': 'go'}])
        assert caught.value.category == 'cancelled'
        assert Session._unavailable(caught.value) is False


def test_settings_save_reaches_an_open_sessions_limit(tmp_path):
    """A save replaces the config object. The backend built before the save follows it."""
    from tessera.config import load_config
    from tessera.sessions import SessionManager

    # The bundle directory is this test's. It is not a launchable engine, so
    # discovery can leave memory on without starting the packaged one, and the
    # file does not store an off switch.
    (tmp_path / 'engine').mkdir()
    (tmp_path / 'config.toml').write_text(
        '[backends.local]\n'
        'base_url = "http://127.0.0.1:9/v1"\n'
        'model = "local-model"\n'
        'chat = true\n'
        'enabled = true\n'
        '\n'
        '[roles]\n'
        'main = "local"\n'
        '\n'
        '[tools]\n'
        'approval = "ask"\n'
        '\n'
        '[aethermind]\n'
        'project = "endpoint-limit"\n'
        'bundled_path = "engine"\n'
    )
    manager = SessionManager(load_config(tmp_path))
    try:
        slug = manager.create('Open')['slug']
        runtime = manager.runtime(slug)
        backend = runtime.session.backend
        assert request_limit(runtime.session.config, 'local') == 1
        assert backend._endpoint_resolve() == 1
        state = {'now': 0, 'peak': 0}
        lock = threading.Lock()
        release = threading.Event()

        def guarded(messages, tools=None, **kwargs):
            with lock:
                state['now'] += 1
                state['peak'] = max(state['peak'], state['now'])
            assert release.wait(5)
            with lock:
                state['now'] -= 1
            return {'content': 'ok', 'finish_reason': 'stop'}

        backend._chat_guarded = guarded

        def wave(count):
            state['now'] = 0
            state['peak'] = 0
            release.clear()
            threads = [threading.Thread(target=backend.chat, args=([{'role': 'user', 'content': 'go'}],))
                       for _ in range(count)]
            for thread in threads:
                thread.start()
            return threads

        first = wave(2)
        deadline = time.time() + 3
        while time.time() < deadline and state['peak'] < 1:
            time.sleep(0.01)
        time.sleep(0.15)
        assert state['peak'] == 1
        release.set()
        for thread in first:
            thread.join(5)
            assert not thread.is_alive()

        saved = manager.apply_settings({
            'id': 'providers', 'action': 'edit', 'name': 'local',
            'record': {'max_concurrent_requests': '4'}})
        assert saved['applied'] == 'live'
        assert runtime.session.backend is backend
        assert backend._endpoint_resolve() == 4
        assert request_limit(load_config(tmp_path), 'local') == 4

        second = wave(4)
        deadline = time.time() + 3
        while time.time() < deadline and state['peak'] < 4:
            time.sleep(0.01)
        assert state['peak'] == 4
        release.set()
        for thread in second:
            thread.join(5)
            assert not thread.is_alive()
    finally:
        manager.close()


def test_a_pace_longer_than_the_lease_still_finishes(tmp_path, monkeypatch):
    config = _swarm(tmp_path)
    config.tools.approval = 'yolo'
    config.swarm.lease_seconds = 1
    config.backends['bench'] = _backend('bench', limit=1)
    config.backends['bench'].throttle_requests_per_minute = 1
    box = {'t': 1000.0}
    tick = threading.Lock()

    def clock():
        return box['t']

    from tessera.scheduler import Scheduler
    original = Scheduler.from_config.__func__

    def from_config(cls, cfg, *args, **kwargs):
        kwargs['clock'] = clock
        return original(cls, cfg, *args, **kwargs)

    monkeypatch.setattr(Scheduler, 'from_config', classmethod(from_config))

    @contextmanager
    def paced(home, name, requests_per_minute, tokens_per_minute, **kwargs):
        with tick:
            box['t'] += config.swarm.lease_seconds + 5
        yield lambda tokens: None

    monkeypatch.setattr('tessera.jobs.endpoint_pace', paced)

    def factory(settings):
        return FakeBackend([settings.name + ' finding'])

    _jobs, _job, runner, holders = _start(config, factory, _workers('bench', 2))
    runner.join(5)
    assert not runner.is_alive()
    assert holders[0]['status'] == 'succeeded'


def test_mode_switch_leaves_a_queued_job_runnable(tmp_path):
    """The Safe default of one local request is not part of the job binding."""
    config = _swarm(tmp_path)
    config.tools.approval = 'ask'
    config.backends['bench'] = _backend('bench', local=True)
    config.backends['other'] = _backend('other', local=False)
    assert request_limit(config, 'bench') == 1
    assert replace(config, roles={'main': 'bench'}).backend_for().extra['endpoint_limit'] is None
    before = _binding(config, 'bench')
    config.tools.approval = 'yolo'
    assert request_limit(config, 'bench') is None
    assert _binding(config, 'bench') == before
    config.tools.approval = 'ask'
    workers = [
        {'backend': 'bench', 'objective': 'one', 'scope': ['bench/0']},
        {'backend': 'other', 'objective': 'two', 'scope': ['other/0']},
    ]

    def factory(settings):
        return FakeBackend([settings.name + ' finding'])

    jobs = SwarmJobs(config, backend_factory=factory)
    log = ChatLog(config.home / 'log')
    log.__enter__()
    try:
        job = jobs.create('fan out', workers, session_slug='subject', log=log)
        config.tools.approval = 'yolo'
        assert jobs.run(job['id'], log=log)['status'] == 'succeeded'
        config.tools.approval = 'ask'
        config.backends['bench'].max_concurrent_requests = 2
        assert replace(config, roles={'main': 'bench'}).backend_for().extra['endpoint_limit'] == 2
        hand = _binding(config, 'bench')
        config.tools.approval = 'yolo'
        assert _binding(config, 'bench') == hand
        held = jobs.create('held', workers, session_slug='subject', log=log)
        config.backends['bench'].max_concurrent_requests = 3
        with pytest.raises(ValueError, match='configuration changed'):
            jobs.run(held['id'], log=log)
    finally:
        log.close()


def test_a_throttle_wait_is_not_marked_running(tmp_path, monkeypatch):
    config = _swarm(tmp_path)
    config.backends['bench'] = _backend('bench', limit=4)
    config.backends['bench'].throttle_requests_per_minute = 1
    clock = {'now': 1000.0}
    release = threading.Event()
    saw_wait = threading.Event()

    def mono():
        return clock['now']

    def sleep(delay):
        saw_wait.set()
        assert release.wait(5)
        clock['now'] += delay

    monkeypatch.setattr('tessera.endpoint_throttle.time.monotonic', mono)
    monkeypatch.setattr('tessera.endpoint_throttle.time.sleep', sleep)

    def factory(settings):
        def reply(messages, tools):
            assert release.wait(5)
            return {'content': settings.name + ' finding'}

        return FakeBackend([reply, reply])

    jobs, job, runner, holders = _start(config, factory, _workers('bench', 2))
    try:
        assert saw_wait.wait(5)
        snapshot = json.loads((jobs.root / job['id'] / 'job.json').read_text())
        states = [worker.get('dispatch') for worker in snapshot['workers']]
        assert 'waiting for bench throttle' in states
        assert states.count('running') == 1
        report = job_status(config)
        shown = [worker['status'] for worker in report['jobs'][0]['workers']]
        assert 'waiting for provider 1 throttle' in shown
        assert 'bench' not in json.dumps(report)
    finally:
        release.set()
    runner.join(5)
    assert not runner.is_alive()
    assert holders[0]['status'] == 'succeeded'


def test_chat_turn_reports_a_slot_wait_and_clears_it_when_work_starts(tmp_path):
    config = _open_local_session(tmp_path)
    config.backends['local'].max_concurrent_requests = 1
    release = threading.Event()
    holding = threading.Event()
    with Session(config) as session:
        backend = session.backend
        notes = []
        backend.on_delta = lambda kind, text: notes.append((kind, text))

        def hold():
            from tessera.endpoint_limit import model_slot_value
            with model_slot_value(backend.endpoint_home, backend.name, 1):
                holding.set()
                release.wait(5)

        holder = threading.Thread(target=hold)
        holder.start()
        try:
            assert holding.wait(5)
            started = threading.Event()

            def guarded(messages, tools=None, **kwargs):
                started.set()
                return {'content': 'ok', 'finish_reason': 'stop'}

            backend._chat_guarded = guarded
            worker = threading.Thread(target=lambda: backend.chat([{'role': 'user', 'content': 'wait'}]))
            worker.start()
            deadline = time.time() + 3
            while time.time() < deadline and ('status', 'waiting for local slot') not in notes:
                time.sleep(0.02)
            assert ('status', 'waiting for local slot') in notes
            assert not started.is_set()
            release.set()
            worker.join(3)
            assert not worker.is_alive()
            assert started.is_set()
            slot_at = notes.index(('status', 'waiting for local slot'))
            assert ('status', 'Running') in notes[slot_at + 1:]
        finally:
            release.set()
            holder.join(3)


def test_chat_turn_reports_a_throttle_wait_and_clears_it_when_work_starts(tmp_path, monkeypatch):
    config = _open_local_session(tmp_path)
    config.backends['local'].max_concurrent_requests = 4
    config.backends['local'].throttle_requests_per_minute = 1
    clock = {'now': 1000.0}
    release = threading.Event()

    def mono():
        return clock['now']

    def sleep(delay):
        release.wait(5)
        clock['now'] += delay

    monkeypatch.setattr('tessera.endpoint_throttle.time.monotonic', mono)
    monkeypatch.setattr('tessera.endpoint_throttle.time.sleep', sleep)
    with Session(config) as session:
        backend = session.backend
        notes = []
        backend.on_delta = lambda kind, text: notes.append((kind, text))
        backend._chat_guarded = lambda *args, **kwargs: {'content': 'ok', 'finish_reason': 'stop'}
        assert backend.chat([{'role': 'user', 'content': 'prime'}])['content'] == 'ok'
        notes.clear()
        result = {}
        worker = threading.Thread(
            target=lambda: result.setdefault('reply', backend.chat([{'role': 'user', 'content': 'paced'}])))
        worker.start()
        try:
            deadline = time.time() + 3
            while time.time() < deadline and ('status', 'waiting for local throttle') not in notes:
                time.sleep(0.02)
            assert ('status', 'waiting for local throttle') in notes
            assert 'reply' not in result
        finally:
            release.set()
        worker.join(3)
        assert not worker.is_alive()
        assert result['reply']['content'] == 'ok'
        wait_at = notes.index(('status', 'waiting for local throttle'))
        assert ('status', 'Running') in notes[wait_at + 1:]
