"""Endpoint pace is per model. A hand-set number binds YOLO. Empty stays unlimited.

No sockets. The clock is the test's.
"""

from contextlib import contextmanager
import threading

from tessera.config import load_config
from tessera.endpoint_throttle import endpoint_pace, throttle_limits
from tessera.settings_model import project
from test_endpoint_concurrency import _backend, _start, _swarm, _workers
from fake_backend import FakeBackend


def _clock():
    box = {'t': 0.0}
    waits = []

    def now():
        return box['t']

    def wait(seconds):
        waits.append(seconds)
        box['t'] += seconds

    return box, waits, now, wait


def test_unset_pace_is_unlimited_in_safe_and_yolo(tmp_path):
    config = _swarm(tmp_path)
    config.backends['bench'] = _backend('bench', local=True)
    assert throttle_limits(config, 'bench') == (None, None)
    _box, waits, now, wait = _clock()
    for _ in range(5):
        with endpoint_pace(config.home, 'bench', None, None, clock=now, wait=wait) as charge:
            charge(10_000)
    assert waits == []
    config.tools.approval = 'yolo'
    assert throttle_limits(config, 'bench') == (None, None)


def test_hand_set_request_pace_spaces_calls_and_yolo_keeps_it(tmp_path):
    config = _swarm(tmp_path)
    config.tools.approval = 'yolo'
    config.backends['bench'] = _backend('bench', local=True)
    config.backends['bench'].throttle_requests_per_minute = 2
    assert throttle_limits(config, 'bench') == (2, None)
    _box, waits, now, wait = _clock()
    starts = []
    for _ in range(3):
        with endpoint_pace(config.home, 'bench', 2, None, clock=now, wait=wait, on_start=starts.append):
            pass
    assert starts == [0.0, 0.0, 60.0]
    assert waits == [60.0]


def test_hand_set_token_pace_uses_reported_usage(tmp_path):
    config = _swarm(tmp_path)
    config.tools.approval = 'yolo'
    config.backends['bench'] = _backend('bench', local=True)
    config.backends['bench'].throttle_tokens_per_minute = 10
    _box, waits, now, wait = _clock()
    starts = []
    for amount in (6, 6, 6):
        with endpoint_pace(config.home, 'bench', None, 10, clock=now, wait=wait, on_start=starts.append) as charge:
            charge({'total_tokens': amount})
    assert starts == [0.0, 0.0, 60.0]
    assert waits == [60.0]


def test_one_endpoint_does_not_pace_another():
    _box, waits, now, wait = _clock()
    with endpoint_pace('home', 'local', 1, None, clock=now, wait=wait):
        with endpoint_pace('home', 'other', 1, None, clock=now, wait=wait):
            pass
    assert waits == []


def test_zero_is_not_a_pace(tmp_path):
    config = _swarm(tmp_path)
    config.backends['bench'] = _backend('bench', local=True)
    config.backends['bench'].throttle_requests_per_minute = 0
    try:
        throttle_limits(config, 'bench')
    except ValueError as error:
        assert 'positive integer' in str(error)
    else:
        raise AssertionError('zero must not be treated as a pace')


def test_env_sets_the_pace_without_a_default(tmp_path, monkeypatch):
    (tmp_path / 'config.toml').write_text(
        '[backends.bench]\nbase_url = "http://127.0.0.1:9/v1"\nmodel = "m"\n')
    monkeypatch.setenv('TESSERA_BACKENDS__BENCH__THROTTLE_REQUESTS_PER_MINUTE', '12')
    monkeypatch.setenv('TESSERA_BACKENDS__BENCH__THROTTLE_TOKENS_PER_MINUTE', '34')
    loaded = load_config(tmp_path)
    assert loaded.backends['bench'].throttle_requests_per_minute == 12
    assert loaded.backends['bench'].throttle_tokens_per_minute == 34
    assert throttle_limits(loaded, 'bench') == (12, 34)
    assert 'throttle_requests_per_minute' not in (tmp_path / 'config.toml').read_text()


def test_swarm_pace_is_per_endpoint_and_not_a_swarm_cap(tmp_path):
    """Swarm numbers are pinned to 1. YOLO is on. Only the hand-set pace spaces work."""
    config = _swarm(tmp_path)
    config.tools.approval = 'yolo'
    config.backends['bench'] = _backend('bench', local=True)
    config.backends['other'] = _backend('other', local=True)
    config.backends['bench'].throttle_requests_per_minute = 1
    box = {'t': 0.0}
    waits = []
    starts = []

    def now():
        return box['t']

    def wait(seconds):
        waits.append((seconds, box['t']))
        box['t'] += seconds

    @contextmanager
    def paced(home, name, requests_per_minute, tokens_per_minute, on_wait=None):
        with endpoint_pace(home, name, requests_per_minute, tokens_per_minute,
                           clock=now, wait=wait, on_start=lambda stamp: starts.append((name, stamp))) as charge:
            yield charge

    from tessera import jobs as jobs_module
    original = jobs_module.endpoint_pace
    jobs_module.endpoint_pace = paced
    try:
        def factory(settings):
            def reply(messages, tools):
                return {'content': settings.name + ' finding'}
            return FakeBackend([reply])

        workers = _workers('bench', 2) + _workers('other', 1)
        _jobs, _job, runner, holders = _start(config, factory, workers)
        runner.join(5)
    finally:
        jobs_module.endpoint_pace = original
    assert not runner.is_alive()
    assert holders[0]['status'] == 'succeeded'
    bench = [stamp for name, stamp in starts if name == 'bench']
    other = [stamp for name, stamp in starts if name == 'other']
    assert bench == [0.0, 60.0]
    assert len(other) == 1
    assert waits == [(60.0, 0.0)]


def test_settings_throttle_is_editable_and_honest(tmp_path):
    config = _swarm(tmp_path)
    config.backends['bench'] = _backend('bench', local=True)
    config.backends['bench'].throttle_requests_per_minute = 9
    config.tools.approval = 'yolo'
    body = project(config, {'yolo': True})
    providers = next(field for section in body['sections'] for field in section['fields'] if field.get('id') == 'providers')
    record = next(item for item in providers['records'] if item['name'] == 'bench')
    requests = next(field for field in record['fields'] if field['key'] == 'throttle_requests_per_minute')
    tokens = next(field for field in record['fields'] if field['key'] == 'throttle_tokens_per_minute')
    assert requests['label'] == 'Requests per minute'
    assert requests['value'] == '9'
    assert 'by hand' in requests['status'].lower() and 'unleashed' in requests['status'].lower()
    assert '9' in requests['status']
    assert 'swarm' in requests['status'].lower()
    assert tokens['value'] == ''
    assert 'unlimited' in tokens['status'].lower() and 'unleashed' in tokens['status'].lower()
    assert 'by hand' in tokens['hint'].lower() and 'swarm' in tokens['hint'].lower()
    blank = next(field for field in providers['blank'] if field['key'] == 'throttle_tokens_per_minute')
    assert 'unlimited' in blank['status'].lower() and 'by hand' in blank['status'].lower()


def test_settings_save_sets_and_clears_a_pace(tmp_path):
    from test_server import call
    from test_settings_model import serve, stop

    server, thread = serve(tmp_path)
    try:
        saved = call(server, 'POST', '/api/settings', {
            'id': 'providers', 'action': 'edit', 'name': 'fake',
            'record': {'throttle_requests_per_minute': '5', 'throttle_tokens_per_minute': '80'}})
        assert saved[0] == 200, saved
        loaded = load_config(tmp_path)
        assert loaded.backends['fake'].throttle_requests_per_minute == 5
        assert loaded.backends['fake'].throttle_tokens_per_minute == 80
        assert throttle_limits(loaded, 'fake') == (5, 80)
        loaded.tools.approval = 'yolo'
        assert throttle_limits(loaded, 'fake') == (5, 80)

        cleared = call(server, 'POST', '/api/settings', {
            'id': 'providers', 'action': 'edit', 'name': 'fake',
            'record': {'throttle_requests_per_minute': '', 'throttle_tokens_per_minute': ''}})
        assert cleared[0] == 200, cleared
        cleared_config = load_config(tmp_path)
        assert throttle_limits(cleared_config, 'fake') == (None, None)
        text = (tmp_path / 'config.toml').read_text()
        assert 'throttle_requests_per_minute' not in text
        assert 'throttle_tokens_per_minute' not in text

        refused = call(server, 'POST', '/api/settings', {
            'id': 'providers', 'action': 'edit', 'name': 'fake',
            'record': {'throttle_requests_per_minute': '0'}})
        assert refused[0] == 400
        message = refused[1].get('error') or ''
        assert 'positive integer' in message.lower()
        assert 'unleashed' in message.lower()
        assert throttle_limits(load_config(tmp_path), 'fake') == (None, None)
    finally:
        stop(server, thread)
