"""Actual local HTTP inventory stays passive; collection is an explicit action."""
import json
import threading
from unittest.mock import Mock

import pytest

from tessera.config import Config
from tessera.sessions import SessionManager
from tessera.server import create_server
from test_server import call
from test_infrastructure import setup_config


def test_manager_status_adds_inert_infrastructure_and_grafana_fields(tmp_path, monkeypatch):
    config = Config(home=tmp_path)
    with SessionManager(config) as manager:
        tripwires = []
        for target in ('subprocess.Popen', 'tessera.credentials.CredentialResolver.resolve',
                       'urllib.request.OpenerDirector.open', 'tessera.sessions.SessionManager.runtime'):
            tripwire = Mock(side_effect=AssertionError('Passive read cannot execute'))
            monkeypatch.setattr(target, tripwire)
            tripwires.append(tripwire)
        first = manager.status()
        assert first['infrastructure_status'] == {'enabled':False,'configured':False,'hosts':[]}
        assert first['grafana'] == {'enabled':False,'configured':False,'health':'unknown'}
        assert not manager.infrastructure.path.exists() and manager._runtimes == {}
        for tripwire in tripwires:
            tripwire.assert_not_called()


def test_http_fenced_collection_persists_only_safe_metrics_and_get_never_probes(tmp_path, monkeypatch):
    config = setup_config(tmp_path)
    config.server.port = 0
    server = create_server(config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        fake = Mock()
        fake.collect.return_value = {'metrics': {'cpu_percent': 15}, 'address':'private-address','token':'private-secret'}
        server.manager.infrastructure.collector = fake
        stamp = [100.0]
        server.manager.infrastructure.clock = lambda: stamp[0]
        code, snapshot = call(server,'GET','/api/infrastructure')
        assert code == 200 and snapshot['hosts'][0]['health'] == 'unknown'
        body = {'ordinal':1,'revision':snapshot['revision']}
        code, collected = call(server,'POST','/api/infrastructure/collect',body)
        assert code == 200 and collected['hosts'][0]['metrics'] == {'cpu_percent':15}
        fake.collect.assert_called_once()
        before = {p:(p.read_bytes(),p.stat().st_mtime_ns) for p in tmp_path.rglob('*') if p.is_file()}
        tripwires=[]
        for target in ('tessera.infrastructure.HostCollector.collect','tessera.credentials.CredentialResolver.resolve',
                       'urllib.request.OpenerDirector.open','subprocess.Popen'):
            tripwire=Mock(side_effect=AssertionError('Passive read'))
            monkeypatch.setattr(target,tripwire)
            tripwires.append(tripwire)
        stamp[0] += 2
        code, refreshed = call(server,'GET','/api/infrastructure')
        assert refreshed['revision'] == collected['revision']
        assert refreshed['hosts'][0]['age_seconds'] == 2
        status = call(server,'GET','/api/status')[1]
        assert status['infrastructure_status'] == refreshed and status['grafana']['health'] == 'unknown'
        assert 'private' not in json.dumps(status['infrastructure_status'])
        assert 'fixture-host' not in json.dumps(refreshed) and str(tmp_path) not in json.dumps(refreshed)
        assert before == {p:(p.read_bytes(),p.stat().st_mtime_ns) for p in tmp_path.rglob('*') if p.is_file()}
        fake.collect.assert_called_once()
        for tripwire in tripwires: tripwire.assert_not_called()
        config.hosts['fixture-host'].address = 'other.example.com'
        assert call(server,'POST','/api/infrastructure/collect',{'ordinal':1,'revision':refreshed['revision']})[0] == 409
        fake.collect.assert_called_once()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_corrupt_oversized_and_symlinked_snapshot_remains_unknown_without_repair(tmp_path):
    from tessera.infrastructure import Infrastructure
    config = setup_config(tmp_path)
    service = Infrastructure(config)
    for raw in (b'broken\n', b'{"observations":', b'{"observations": {"x": "private"}}\n'):
        service.path.write_bytes(raw)
        before=service.path.read_bytes(),service.path.stat().st_mtime_ns
        assert service.status()['hosts'][0]['health'] == 'unknown'
        assert (service.path.read_bytes(),service.path.stat().st_mtime_ns) == before
    service.path.write_bytes(b'x'*(config.status.max_file_bytes+1))
    assert service.status()['hosts'][0]['health'] == 'unknown'
    service.path.unlink()
    other=tmp_path/'external-fixture'
    other.write_text('{"observations":{}}\n')
    service.path.symlink_to(other)
    assert service.status()['hosts'][0]['health'] == 'unknown' and other.read_text() == '{"observations":{}}\n'


def test_numeric_metric_boundaries_do_not_report_impossible_samples(tmp_path):
    from tessera.infrastructure import Infrastructure
    config=setup_config(tmp_path)
    fake=Mock()
    service=Infrastructure(config,collector=fake)
    for metrics in ({'cpu_count':1.5}, {'cpu_count':True}, {'disk_total_bytes':1,'disk_used_bytes':2},
                    {'memory_total_bytes':1,'memory_available_bytes':2}, {'cpu_percent':101}):
        fake.collect.return_value={'metrics':metrics}
        assert service.collect('fixture-host')['hosts'][0]['error_category'] == 'invalid_response'


def test_inventory_changes_during_collection_do_not_retarget_saved_samples(tmp_path):
    from tessera.infrastructure import Infrastructure, InventoryRevisionError
    config=setup_config(tmp_path)
    class ChangingCollector:
        def collect(self,host):
            assert host.address == ''
            config.hosts['fixture-host'].address='other.example.com'
            return {'metrics':{'cpu_percent':12}}
    service=Infrastructure(config,collector=ChangingCollector())
    with pytest.raises(InventoryRevisionError):
        service.collect('fixture-host')
    assert not service.path.exists()


def test_unsafe_numeric_ranges_and_unmeasured_services_stay_unknown(tmp_path):
    from tessera.infrastructure import Infrastructure
    config=setup_config(tmp_path)
    fake=Mock()
    service=Infrastructure(config,collector=fake)
    fake.collect.return_value={'metrics':{'cpu_load1':1e100}}
    assert service.collect('fixture-host')['hosts'][0]['error_category'] == 'invalid_response'
    config.hosts['fixture-host'].services={'private-service':[]}
    fake.collect.return_value={'services':{}}
    row=service.collect('fixture-host')['hosts'][0]
    assert row['health'] == 'unknown' and row['services'] == [{'ordinal':1,'state':'unknown'}]
