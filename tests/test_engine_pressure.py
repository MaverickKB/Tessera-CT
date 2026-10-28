"""Pressure readings use source occupancy, never GPU load or inferred idleness."""

import json
import threading
import time
from types import SimpleNamespace
from urllib.error import URLError

import pytest

from tessera.config import InfrastructureConfig, load_config
from tessera import engine_pressure
from tessera.server import Handler

ENGINE = {'host': 'engine.example', 'port': 8888, 'model': 'configured-alias', 'slots': 4}


def transport_for(running=1, waiting=0, warming=False, slots=4):
    def get(url, timeout):
        assert timeout == 2.5
        assert url.startswith('http://engine.example:8888/')
        if url.endswith('/health'):
            return json.dumps({'max_batch_size': slots, 'warming': warming})
        assert url.endswith('/metrics')
        return (f'# HELP tensorfold:requests_running Requests in prefill or decode.\n'
                f'tensorfold:requests_running {running}\n'
                f'tensorfold:requests_waiting {waiting}\n')
    return get


@pytest.mark.parametrize('running,waiting,warming,slots,free', [
    (1, 0, False, 4, 3), (1, 2, True, 4, 2), (4, 3, True, 4, 0),
    (0, 0, True, 4, 3), (1, 0, False, 6, 5),
])
def test_source_capacity_and_warming_arithmetic(running, waiting, warming, slots, free):
    reading = engine_pressure.sample_engine(ENGINE, transport_for(running, waiting, warming, slots))
    assert reading == {
        'host': ENGINE['host'], 'port': 8888, 'model': 'configured-alias',
        'slots_total': slots, 'running': running, 'waiting': waiting,
        'warming': warming, 'free': free, 'reachable': True, 'error': None,
    }


@pytest.mark.parametrize('error', [ConnectionRefusedError('Connection refused'), TimeoutError('timed out')])
def test_unreachable_is_unknown_not_idle(error):
    def get(url, timeout):
        assert timeout == 2.5
        raise error
    reading = engine_pressure.sample_engine(ENGINE, get)
    assert reading['reachable'] is False
    assert reading['error'] == str(error)
    for key in ('running', 'waiting', 'warming', 'free'):
        assert reading[key] is None


@pytest.mark.parametrize('metrics', ['', 'tensorfold:requests_running NaN\n',
                                     'tensorfold:requests_running -1\n',
                                     'tensorfold:requests_running 1\n'])
def test_incomplete_or_invalid_metrics_are_not_idle(metrics):
    def get(url, timeout):
        if url.endswith('/metrics'):
            return metrics
        return json.dumps({'max_batch_size': 4, 'warming': False})
    reading = engine_pressure.sample_engine(ENGINE, get)
    assert reading['reachable'] is False
    assert reading['free'] is None
    assert reading['error']


def test_get_route_returns_failure_shape_without_network_or_session_actions(monkeypatch):
    calls = []
    def get(url, timeout):
        calls.append((url, timeout))
        raise URLError('Connection refused')
    monkeypatch.setattr(engine_pressure, '_get', get)
    handler = Handler.__new__(Handler)
    handler.server = SimpleNamespace(manager=object(), config=SimpleNamespace(
        infrastructure=InfrastructureConfig(pressure_engines=[ENGINE])))
    responses = []
    handler._json = lambda status, payload: responses.append((status, payload))
    handler._api('GET', '/api/pressure/engines', {})
    status, payload = responses[0]
    assert status == 200
    assert set(payload) == {'engines'}
    reading = payload['engines'][0]
    assert set(reading) == {'host', 'port', 'model', 'slots_total', 'running',
                            'waiting', 'warming', 'free', 'reachable', 'error'}
    assert reading['reachable'] is False
    assert reading['running'] is None
    assert reading['free'] is None
    assert 'Connection refused' in reading['error']
    assert calls == [('http://engine.example:8888/health', 2.5)]


def test_configured_list_loads_and_can_be_empty(tmp_path):
    (tmp_path / 'config.toml').write_text('''
[[infrastructure.pressure_engines]]
host = "engine.example"
port = 8888
model = "configured-alias"
slots = 4
''')
    config = load_config(tmp_path)
    assert config.infrastructure.pressure_engines == [ENGINE]
    (tmp_path / 'config.toml').write_text('[infrastructure]\npressure_engines = []\n')
    assert engine_pressure.snapshot(load_config(tmp_path).infrastructure) == {'engines': []}


@pytest.mark.parametrize('field,value', [('port', 0), ('slots', -1), ('host', 'http://engine.example'),
                                        ('model', ''), ('slots', True)])
def test_config_rejects_invalid_engine(field, value):
    engine = dict(ENGINE)
    engine[field] = value
    with pytest.raises(ValueError):
        engine_pressure.validate_engines([engine])


def test_http_wait_is_bounded_even_if_transport_stalls(monkeypatch):
    release = threading.Event()
    def stalled_read(url, timeout):
        release.wait(5)
        return 'late response'
    monkeypatch.setattr(engine_pressure, '_read_http', stalled_read)
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError, match='deadline'):
            engine_pressure._get('http://engine.example/health', 0.03)
        assert time.monotonic() - started < 1
    finally:
        release.set()


def test_metrics_failure_discards_partial_health():
    def get(url, timeout):
        if url.endswith('/health'):
            return json.dumps({'max_batch_size': 4, 'warming': False})
        raise TimeoutError('metrics timed out')
    reading = engine_pressure.sample_engine(ENGINE, get)
    assert reading['reachable'] is False
    assert reading['running'] is None
    assert reading['free'] is None
    assert reading['error'] == 'metrics timed out'
