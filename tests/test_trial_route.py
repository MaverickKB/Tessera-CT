"""The trial entry point the 'auto' policy waits on.

The 'auto' policy starts a trial through a route. These tests pin that
route, and pin that it refuses the shapes the engine refuses.
"""
import json
import threading

import pytest

from fake_backend import FakeBackend
from tessera.config import BackendConfig, Config
from tessera.server import create_server
from test_server import call


def config_for(root):
    config = Config(home=root, cwd=root)
    config.server.port = 0
    config.jev.enabled = False
    config.tools.approval = 'never'
    config.autonomy.enabled = False
    config.autonomy.oversight_policy = 'auto'
    config.autonomy.trial_poll_seconds = 0.0
    config.backends['candidate'] = BackendConfig(name='candidate', model='candidate',
                                                 base_url='http://127.0.0.1:1')
    config.backends['base'] = BackendConfig(name='base', model='base',
                                            base_url='http://127.0.0.1:1')
    return config


@pytest.fixture
def served(tmp_path):
    config = config_for(tmp_path)
    server = create_server(config, backend_factory=lambda slug, name: FakeBackend([]))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, server.manager
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def make_session(manager, name='Trial route subject'):
    slug = manager.create(name)['slug']
    manager.update(slug, reasoning='low')
    return slug


def test_the_route_starts_the_trial_the_auto_policy_asks_for(served):
    server, manager = served
    slug = make_session(manager)
    status, body = call(server, 'POST', '/api/trials', {
        'slug': slug,
        'lever': 'reasoning',
        'candidate': 'high',
        'turns': 3,
        'instrument': 'repeat_failures',
        'why': 'a repeated failing tool is a structural fact, not a judge score',
    })
    assert status == 200, body
    trial = body['trial']
    assert trial['phase'] == 'baseline'
    assert trial['from'] == 'low' and trial['to'] == 'high'
    assert trial['turns'] == 3
    # On disk, not only in the response: a measurement nobody can read later
    # is not a measurement.
    status, view = call(server, 'GET', '/api/trials')
    assert status == 200
    assert [t['id'] for t in view['trials']] == [trial['id']]
    assert view['open'] == [trial['id']]
    assert view['levers'] == ['backend', 'reasoning']


def test_the_auto_report_names_the_started_trial(served):
    server, manager = served
    slug = make_session(manager)
    call(server, 'POST', '/api/trials', {
        'slug': slug, 'lever': 'reasoning', 'candidate': 'high', 'turns': 2,
        'instrument': 'turn_seconds_median',
        'why': 'turn span is recorded on every message, so both arms carry it',
    })
    status, report = call(server, 'GET', '/api/oversight')
    assert status == 200
    assert report['policy'] == 'auto'
    joined = ' '.join(report['findings'])
    assert '1 trial(s) are measuring a change' in joined
    assert 'nothing is applied' not in joined.lower()
    assert len(report['trials']) == 1
    assert report['trials'][0]['phase'] == 'baseline'


def test_a_completion_score_instrument_is_refused_over_the_wire(served):
    server, manager = served
    slug = make_session(manager)
    status, body = call(server, 'POST', '/api/trials', {
        'slug': slug, 'lever': 'reasoning', 'candidate': 'high', 'turns': 2,
        'instrument': 'completion', 'why': 'it would read the same in both arms',
    })
    assert status == 409
    assert 'completion score cannot separate two arms' in json.dumps(body)


def test_a_lever_outside_the_bound_is_refused_over_the_wire(served):
    server, manager = served
    slug = make_session(manager)
    status, body = call(server, 'POST', '/api/trials', {
        'slug': slug, 'lever': 'model', 'candidate': 'glm', 'turns': 2,
        'instrument': 'repeat_failures', 'why': 'not a lever this dial may change',
    })
    assert status == 409
    assert 'lever must be one of' in json.dumps(body)


def test_an_unknown_session_is_a_404_not_a_traceback(served):
    server, manager = served
    status, body = call(server, 'POST', '/api/trials', {
        'slug': 'no-such-session', 'lever': 'reasoning', 'candidate': 'high',
        'turns': 2, 'instrument': 'repeat_failures', 'why': 'measured nowhere',
    })
    assert status == 404
    assert 'no such session' in json.dumps(body)


def test_the_body_must_carry_every_field_the_engine_needs(served):
    server, manager = served
    slug = make_session(manager)
    status, body = call(server, 'POST', '/api/trials', {
        'slug': slug, 'lever': 'reasoning', 'candidate': 'high', 'turns': 2,
        'instrument': 'repeat_failures',
    })
    assert status == 400
    assert 'Invalid request fields' in json.dumps(body)


def test_a_second_open_trial_on_one_session_is_refused(served):
    """One change at a time, enforced at the wire and not only in the engine."""
    server, manager = served
    slug = make_session(manager)
    first = {'slug': slug, 'lever': 'reasoning', 'candidate': 'high', 'turns': 2,
             'instrument': 'repeat_failures', 'why': 'one change at a time'}
    status, _ = call(server, 'POST', '/api/trials', first)
    assert status == 200
    status, body = call(server, 'POST', '/api/trials', dict(first, candidate='medium'))
    assert status == 409
    assert 'already has an open trial' in json.dumps(body)
