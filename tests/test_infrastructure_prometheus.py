from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time
from urllib.parse import parse_qs, urlsplit

import pytest

from tessera.config import Config, HostConfig, load_config
from tessera.infrastructure import Infrastructure, PrometheusReader, prometheus_sample, CollectionError

SERIES = {
    'up{instance="box-a"}': [({}, '1')],
    'up{instance="box-down"}': [({}, '0')],
    'load{instance="box-a"}': [({}, '0.5')],
    'mem{instance="box-a"}': [({}, '1073741824.0')],
    'cores{instance="box-a"}': [({}, '20')],
    'gpu_util{instance="box-a"}': [({'gpu': '1'}, '30'), ({'gpu': '0'}, '12')],
    'gpu_mem{instance="box-a"}': [({'gpu': '0'}, '-1048576')],
}


@contextmanager
def prometheus():
    seen = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlsplit(self.path)
            query = parse_qs(parsed.query)['query'][0]
            seen.append((parsed.path, query))
            result = [{'metric': m, 'value': [time.time(), v]} for m, v in SERIES.get(query, [])]
            body = json.dumps({'status': 'success', 'data': {'resultType': 'vector', 'result': result}}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_address[1]}', seen
    finally:
        server.shutdown()
        server.server_close()


def host(instance='box-a', **extra):
    values = dict(name=instance, enabled=True, kind='prometheus', label='Box A', prometheus_instance=instance,
                  prometheus_up_query='up{instance="{instance}"}',
                  prometheus_queries={'cpu_load1': 'load{instance="{instance}"}', 'memory_total_bytes': 'mem{instance="{instance}"}',
                                      'cpu_count': 'cores{instance="{instance}"}', 'disk_total_bytes': 'missing{instance="{instance}"}'},
                  prometheus_gpu_queries={'utilization_percent': 'gpu_util{instance="{instance}"}', 'memory_used_bytes': 'gpu_mem{instance="{instance}"}'})
    values.update(extra)
    return HostConfig(**values)


def test_prometheus_sample_maps_queries_and_drops_sentinels():
    with prometheus() as (url, seen):
        data = prometheus_sample(host(), PrometheusReader(url))
    assert data['metrics'] == {'cpu_load1': 0.5, 'memory_total_bytes': 1073741824, 'cpu_count': 20}
    assert data['gpu'] == [{'utilization_percent': 12.0}, {'utilization_percent': 30.0}]
    assert seen[0] == ('/api/v1/query', 'up{instance="box-a"}')


def test_down_target_is_unreachable_and_board_reports_labels(tmp_path):
    config = Config(home=tmp_path, cwd=tmp_path)
    with prometheus() as (url, _):
        config.infrastructure.enabled = True
        config.infrastructure.prometheus_url = url
        config.hosts = {'a': host(), 'down': host('box-down', name='down', label='Down box')}
        config.hosts['a'].name = 'a'
        config.validate()
        board = Infrastructure(config)
        report = board.collect_all()
    a, down = report['hosts']
    assert a['label'] == 'Box A' and a['health'] == 'healthy' and a['kind'] == 'prometheus'
    assert a['metrics']['cpu_count'] == 20 and a['gpu'][0] == {'ordinal': 1, 'utilization_percent': 12.0}
    assert down['label'] == 'Down box' and down['health'] == 'unreachable' and down['error_category'] == 'unavailable'


def test_unreachable_prometheus_is_reported_not_raised(tmp_path):
    config = Config(home=tmp_path, cwd=tmp_path)
    config.infrastructure.enabled = True
    config.infrastructure.prometheus_url = 'http://127.0.0.1:9'
    config.hosts = {'a': host(name='a')}
    config.validate()
    row = Infrastructure(config).collect_all()['hosts'][0]
    assert row['health'] == 'unreachable'


def test_background_collection_runs_on_interval(tmp_path):
    with prometheus() as (url, seen):
        config = Config(home=tmp_path, cwd=tmp_path)
        config.infrastructure.enabled = True
        config.infrastructure.prometheus_url = url
        config.infrastructure.collect_interval = 0.2
        config.hosts = {'a': host(name='a')}
        config.validate()
        board = Infrastructure(config)
        board.start()
        try:
            deadline = time.time() + 5
            while time.time() < deadline and board.status()['hosts'][0]['health'] != 'healthy':
                time.sleep(0.05)
        finally:
            board.stop()
    assert board.status()['hosts'][0]['health'] == 'healthy'


def test_prometheus_config_loads_and_validates(tmp_path):
    base = ('[infrastructure]\nenabled = true\nprometheus_url = "http://prometheus.example:9090"\ncollect_interval = 30\n'
            '[hosts.box]\nkind = "prometheus"\nenabled = true\nlabel = "Box"\nprometheus_instance = "box"\n'
            'prometheus_queries = {cpu_load1 = \'load{instance="{instance}"}\'}\n')
    (tmp_path / 'config.toml').write_text(base)
    config = load_config(tmp_path)
    assert config.hosts['box'].prometheus_queries == {'cpu_load1': 'load{instance="{instance}"}'}
    for bad in (base.replace('cpu_load1 =', 'bogus_metric ='), base.replace('prometheus_url = "http://prometheus.example:9090"\n', ''),
                base.replace('collect_interval = 30', 'collect_interval = -1')):
        (tmp_path / 'config.toml').write_text(bad)
        with pytest.raises(ValueError):
            load_config(tmp_path)
