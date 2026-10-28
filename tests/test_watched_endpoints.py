"""A watched endpoint is a kind, an interval, and a reader that actually reads.

`autonomy.watch_sources` was declared with strict per-entry validation -- name,
http(s) URL, positive integer interval -- and nothing ever fetched it. An install
could list a service, say how often to check it, and get silence, forever. The
setting looked like a feature and behaved like a comment.

The repair is a real consumer: `endpoint` is a system kind, a watched one is
polled at its own interval, and only a change of answer reaches a session. The
tests below pin the reader, not the intent.
"""

import pytest

from tessera.autonomy import Autonomy
from tessera.systems import (DEFAULT_ENDPOINT_POLL_SECONDS, due_endpoints,
                             endpoint_polls, validate_systems)


def endpoint(**options):
    return {'kind': 'endpoint', 'label': 'Turn API', 'role': 'Serves turns.',
            'address': 'http://127.0.0.1:9/health', 'watch': True, **options}


# --- the schedule is readable without a clock, a socket or an autonomy object ---

def test_a_watched_endpoint_is_pollable_and_carries_its_own_interval():
    records = validate_systems({'api': endpoint(signals={'poll_seconds': 30})})
    assert endpoint_polls(records) == {
        'api': {'address': 'http://127.0.0.1:9/health', 'role': 'Serves turns.',
                'label': 'Turn API', 'interval_seconds': 30}}


def test_an_endpoint_that_says_nothing_gets_the_documented_default():
    records = validate_systems({'api': endpoint()})
    assert endpoint_polls(records)['api']['interval_seconds'] == DEFAULT_ENDPOINT_POLL_SECONDS


def test_an_unwatched_endpoint_is_documentation_and_is_never_polled():
    records = validate_systems({'api': endpoint(watch=False)})
    assert endpoint_polls(records) == {}


def test_other_kinds_are_not_polled_even_when_watched():
    """`watch` already means something else for a host: it contributes thresholds."""
    records = validate_systems({'mac': {'kind': 'host', 'label': 'Mac',
                                        'role': 'Development surface.', 'watch': True,
                                        'address': 'ssh mac'}})
    assert endpoint_polls(records) == {}


def test_an_endpoint_is_due_immediately_then_not_until_its_interval_elapsed():
    records = validate_systems({'api': endpoint(signals={'poll_seconds': 30})})
    assert list(due_endpoints(records, {}, 1_000)) == ['api']
    assert due_endpoints(records, {'api': 990}, 1_000) == {}
    assert list(due_endpoints(records, {'api': 960}, 1_000)) == ['api']


def test_an_endpoint_with_no_previous_poll_is_due_rather_than_silent():
    """Never-polled is not the same as recently-polled. It must be read at once."""
    records = validate_systems({'api': endpoint(signals={'poll_seconds': 3600})})
    assert list(due_endpoints(records, {}, 5)) == ['api']


def test_a_bad_poll_interval_is_refused_at_load_not_ignored_at_poll_time():
    with pytest.raises(ValueError, match='^Invalid signal value for poll_seconds$'):
        validate_systems({'api': endpoint(signals={'poll_seconds': 0})})


def test_a_watched_endpoint_with_no_address_is_refused():
    """A record that claims to be polled must name something to poll."""
    with pytest.raises(ValueError,
                       match='^System api is a watched endpoint and needs an address to poll$'):
        validate_systems({'api': endpoint(address='')})


def test_an_unwatched_endpoint_may_still_be_declared_without_an_address():
    records = validate_systems({'api': endpoint(watch=False, address='')})
    assert records['api'].kind == 'endpoint'


# --- the consumer: a watched endpoint is actually polled, and only a change wakes ---

class _Settings:
    """The autonomy settings the poller touches, and nothing it does not."""
    enabled = True
    standing_floor = False
    self_prompt_enabled = False
    state_file = 'autonomy/state.json'
    tasks_dir = 'autonomy/tasks'
    lesson_file = 'learning/lessons.json'
    lesson_pass_score = 0.7
    max_open_lessons = 12
    lesson_fail_limit = 3
    infinity_slug = 'infinity'
    tick_seconds = 3600
    watch_sources = []


class _Config:
    def __init__(self, home, systems=None):
        self.home = home
        self.autonomy = _Settings()
        self.systems = validate_systems(systems or {})


class _Manager:
    """Enough manager for tick(): the poller and the floor it runs beside."""
    _metadata = {}

    def __init__(self, config):
        self.config = config

    def advance_trials(self):
        return None


def _autonomy(tmp_path, systems=None, watch_sources=(), clock=None):
    config = _Config(str(tmp_path), systems)
    config.autonomy.watch_sources = list(watch_sources)
    autonomy = Autonomy(_Manager(config), clock=clock or (lambda: 1_000.0))
    autonomy.subscribe('inbox', 'infrastructure')
    return autonomy


def _held(autonomy, slug='inbox'):
    return [e for e in autonomy.state['held'].get(slug, []) if e['kind'] == 'infra']


def _stub(autonomy, answers):
    """Answer per address, and record every address actually probed."""
    seen = []

    def probe(address, timeout=5.0):
        seen.append(address)
        return answers.get(address, 'down')

    autonomy.probe_endpoint = probe
    return seen


def test_a_watched_endpoint_is_actually_fetched(tmp_path):
    """The whole gap in one assertion: the URL configured is the URL fetched."""
    autonomy = _autonomy(tmp_path, {'api': endpoint()})
    seen = _stub(autonomy, {'http://127.0.0.1:9/health': 'down'})
    autonomy.tick()
    assert seen == ['http://127.0.0.1:9/health']


def test_the_first_answer_is_not_news_but_a_change_is(tmp_path):
    autonomy = _autonomy(tmp_path, {'api': endpoint()})
    _stub(autonomy, {'http://127.0.0.1:9/health': 'down'})
    autonomy.tick()
    # Learning it is down for the first time is a baseline, not an event.
    assert _held(autonomy) == []

    autonomy.probe_endpoint = lambda address, timeout=5.0: 'up'
    autonomy.state['endpoint_checked']['api'] = 0.0
    autonomy.tick()
    events = _held(autonomy)
    assert len(events) == 1
    assert 'Turn API down -> up' in events[0]['text']


def test_a_steady_answer_does_not_wake_anyone(tmp_path):
    autonomy = _autonomy(tmp_path, {'api': endpoint()})
    _stub(autonomy, {'http://127.0.0.1:9/health': 'up'})
    for _ in range(4):
        autonomy.state['endpoint_checked']['api'] = 0.0
        autonomy.tick()
    assert _held(autonomy) == []


def test_a_poll_interval_is_honoured_between_ticks(tmp_path):
    autonomy = _autonomy(tmp_path, {'api': endpoint(signals={'poll_seconds': 300})})
    seen = _stub(autonomy, {})
    autonomy.tick()
    autonomy.tick()          # same clock: not due
    assert seen == ['http://127.0.0.1:9/health']


def test_no_subscriber_means_no_traffic_to_a_watched_service(tmp_path):
    """Nothing reads the result, so nothing should be fetched."""
    autonomy = _autonomy(tmp_path, {'api': endpoint()})
    autonomy.state['subscriptions']['infrastructure'] = []
    seen = _stub(autonomy, {})
    autonomy.tick()
    assert seen == []


def test_a_declared_watch_source_is_read_now_that_it_has_a_reader(tmp_path):
    """The legacy list kept its own interval; it must not stay decorative."""
    autonomy = _autonomy(tmp_path, watch_sources=[
        {'name': 'status', 'url': 'http://127.0.0.1:9/status', 'interval_seconds': 60}])
    seen = _stub(autonomy, {'http://127.0.0.1:9/status': 'up'})
    autonomy.tick()
    assert seen == ['http://127.0.0.1:9/status']
    autonomy.probe_endpoint = lambda address, timeout=5.0: 'down'
    autonomy.state['endpoint_checked']['watch_source:status'] = 0.0
    autonomy.tick()
    assert 'status up -> down' in _held(autonomy)[0]['text']


def test_endpoint_state_survives_a_restart_so_it_is_not_re_announced(tmp_path):
    first = _autonomy(tmp_path, {'api': endpoint()})
    _stub(first, {'http://127.0.0.1:9/health': 'up'})
    first.tick()
    assert first.state['endpoint_health']['api'] == 'up'

    second = _autonomy(tmp_path, {'api': endpoint()})
    assert second.state['endpoint_health']['api'] == 'up'
    assert second.state['endpoint_checked']['api'] == 1_000.0
    # Its last poll is recent, so a restart does not re-read it and cannot
    # mistake the state it just loaded for a change.
    seen = _stub(second, {'http://127.0.0.1:9/health': 'down'})
    second.tick()
    assert seen == []
    assert _held(second) == []


# --- the probe itself, over a real socket, because a stub cannot prove it fetches ---

import http.server
import socket
import threading
import urllib.error


def _server(handler):
    server = http.server.HTTPServer(('127.0.0.1', 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _handler(status, hits):
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            self.send_response(status)
            self.end_headers()
            self.wfile.write(b'ok')

        def log_message(self, *args):
            pass

    return H


def test_the_probe_really_fetches_the_declared_address(tmp_path):
    """A stub proves the wiring; only a socket proves the fetch."""
    hits = []
    server = _server(_handler(200, hits))
    try:
        address = f'http://127.0.0.1:{server.server_address[1]}/health'
        autonomy = _autonomy(tmp_path, {'api': endpoint(address=address)})
        autonomy.tick()
        assert hits == ['/health']
        assert autonomy.state['endpoint_health']['api'] == 'up'
    finally:
        server.shutdown()


def test_an_error_status_still_counts_as_answered(tmp_path):
    """Reachability, not a health opinion: a 500 means the endpoint is there."""
    hits = []
    server = _server(_handler(500, hits))
    try:
        autonomy = _autonomy(tmp_path)
        assert autonomy.probe_endpoint(f'http://127.0.0.1:{server.server_address[1]}/x') == 'up'
    finally:
        server.shutdown()


def test_a_closed_port_is_down(tmp_path):
    autonomy = _autonomy(tmp_path)
    assert autonomy.probe_endpoint('http://127.0.0.1:9/health', timeout=1) == 'down'


def test_a_host_that_does_not_resolve_is_down_not_an_exception(tmp_path, monkeypatch):
    """A dead name is an answer about the endpoint, not a crash in the poller."""
    import urllib.request

    def refuse(*args, **kwargs):
        raise urllib.error.URLError(socket.gaierror(8, 'nodename nor servname provided'))

    monkeypatch.setattr(urllib.request, 'urlopen', refuse)
    autonomy = _autonomy(tmp_path)
    assert autonomy.probe_endpoint('http://no-such-endpoint.invalid/x', timeout=1) == 'down'
