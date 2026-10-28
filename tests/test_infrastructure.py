"""Explicit collectors and passive inventory use only offline fixtures."""
import json
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from tessera.config import Config, load_config


def setup_config(tmp_path):
    from tessera.config import HostConfig
    config = Config(home=tmp_path, cwd=tmp_path)
    config.infrastructure.enabled = True
    config.hosts['fixture-host'] = HostConfig(name='fixture-host', enabled=True, kind='local', disk_path=str(tmp_path))
    return config


def test_infrastructure_defaults_are_inert_and_configuration_is_typed(tmp_path, monkeypatch):
    config = load_config(tmp_path)
    assert not config.infrastructure.enabled and config.hosts == {} and not config.grafana.enabled
    (tmp_path / 'config.toml').write_text('''
[infrastructure]
enabled = true
[hosts.example]
enabled = true
kind = "ssh"
address = "host.example.com"
ssh_alias = "example-alias"
ssh_command = ["ssh"]
probe_command = ["probe-placeholder", "--json"]
[grafana]
enabled = true
base_url = "https://metrics.example.com/grafana"
token_env = "EXAMPLE_METRICS_TOKEN"
''')
    monkeypatch.setenv('TESSERA_HOSTS__EXAMPLE__TIMEOUT', '4.0')
    assert load_config(tmp_path).hosts['example'].timeout == 4
    for fragment in ('[hosts.example]\nenabled="yes"', '[hosts.example]\nkind="other"',
                     '[hosts.example]\nprobe_command="shell text"', '[infrastructure]\nstale_after=0',
                     '[grafana]\nbase_url="https://name:password@example.com"'):
        (tmp_path / 'config.toml').write_text(fragment)
        with pytest.raises(ValueError):
            load_config(tmp_path)


def test_disabled_inventory_has_no_collection_storage_or_clock(tmp_path):
    from tessera.infrastructure import Infrastructure
    config = Config(home=tmp_path)
    collector = Mock()
    clock = Mock(side_effect=AssertionError('disabled clock'))
    service = Infrastructure(config, collector=collector, clock=clock)
    assert service.status()['hosts'] == []
    with pytest.raises(ValueError, match='disabled'):
        service.collect('anything')
    collector.collect.assert_not_called()
    clock.assert_not_called()
    assert list(tmp_path.iterdir()) == []


def test_samples_reopen_stale_without_probes_and_config_changes_invalidate(tmp_path):
    from tessera.infrastructure import Infrastructure
    config = setup_config(tmp_path)
    stamp = [100.0]
    fake = Mock()
    fake.collect.return_value = {'metrics': {'uptime_seconds': 50, 'cpu_percent': 12, 'private': 'never'},
                                'gpu': [{'utilization_percent': 7, 'name': 'private-gpu'}],
                                'services': {'private-service': 'running'}, 'raw_error': 'private exception'}
    config.hosts['fixture-host'].services = {'private-service': []}
    service = Infrastructure(config, collector=fake, clock=lambda: stamp[0])
    assert service.status()['hosts'][0]['health'] == 'unknown'
    report = service.collect('fixture-host')
    assert report['hosts'][0]['health'] == 'healthy'
    row = report['hosts'][0]
    assert row['metrics'] == {'uptime_seconds': 50, 'cpu_percent': 12}
    assert row['services'] == [{'ordinal': 1, 'state': 'running'}]
    assert row['gpu'] == [{'ordinal': 1, 'utilization_percent': 7}]
    raw = service.path.read_text()
    assert 'private' not in raw and 'fixture-host' not in raw and str(tmp_path) not in raw
    second = Infrastructure(config, collector=Mock(side_effect=AssertionError('read cannot probe')), clock=lambda: stamp[0])
    before = service.path.read_bytes(), service.path.stat().st_mtime_ns
    assert second.status() == report
    stamp[0] += config.infrastructure.stale_after + 1
    assert second.status()['hosts'][0]['health'] == 'stale'
    assert (service.path.read_bytes(), service.path.stat().st_mtime_ns) == before
    config.hosts['fixture-host'].address = 'changed.example.com'
    assert second.status()['hosts'][0]['health'] == 'unknown'
    assert second.status()['revision'] != report['revision']
    fake.collect.assert_called_once()


def test_local_and_ssh_commands_are_bounded_quoted_and_errors_are_finite(tmp_path):
    from tessera.infrastructure import HostCollector
    import subprocess
    config = setup_config(tmp_path)
    host = config.hosts['fixture-host']
    host.probe_command = ['fixture-probe', 'argument with spaces', '$(not-a-shell)']
    def run(argv, **kwargs):
        assert argv == host.probe_command and kwargs['timeout'] == host.timeout
        assert kwargs['stdin'] == subprocess.DEVNULL and kwargs['shell'] is False
        return subprocess.CompletedProcess(argv, 0, '{"metrics":{"cpu_percent":8}}', '')
    collector = HostCollector(run=run)
    assert collector.collect(host)['metrics']['cpu_percent'] == 8
    host.kind = 'ssh'
    host.ssh_alias = 'example-alias'
    host.ssh_command = ['ssh-placeholder']
    calls = []
    def ssh(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, '{"metrics":{"uptime_seconds":10}}', '')
    assert HostCollector(run=ssh).collect(host)['metrics']['uptime_seconds'] == 10
    import shlex
    assert calls[0][-2] == host.ssh_alias and shlex.split(calls[0][-1]) == host.probe_command
    assert 'BatchMode=yes' in calls[0] and 'NumberOfPasswordPrompts=0' in calls[0]
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired('private-command', 3, output='private-path')
    with pytest.raises(Exception) as error:
        HostCollector(run=timeout).collect(host)
    assert error.value.category == 'timeout' and 'private' not in str(error.value)


def test_builtin_local_metrics_and_explicit_service_probes(tmp_path, monkeypatch):
    from tessera.infrastructure import HostCollector
    import subprocess
    host = setup_config(tmp_path).hosts['fixture-host']
    host.services = {'private-service': ['check-placeholder']}
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        from tessera.host_metrics import collect_metrics
        return subprocess.CompletedProcess(argv, 0, json.dumps({'metrics': collect_metrics(str(tmp_path))}), '')
    data = HostCollector(run=run).collect(host)
    assert data['metrics']['cpu_count'] > 0 and data['metrics']['disk_total_bytes'] > 0
    assert data['services'] == {'private-service': 'running'} and calls[-1] == ['check-placeholder'] and len(calls) == 2


def test_bad_or_failed_sample_never_leaks_diagnostics_or_keeps_old_health(tmp_path):
    from tessera.infrastructure import Infrastructure, CollectionError
    config = setup_config(tmp_path)
    fake = Mock()
    fake.collect.return_value = {'metrics': {'cpu_percent': 20}}
    service = Infrastructure(config, collector=fake)
    service.collect('fixture-host')
    fake.collect.side_effect = CollectionError('timeout')
    row = service.collect('fixture-host')['hosts'][0]
    assert row['health'] == 'unreachable' and row['error_category'] == 'timeout' and 'metrics' not in row
    fake.collect.side_effect = RuntimeError('private-host private-token')
    row = service.collect('fixture-host')['hosts'][0]
    assert row['error_category'] == 'unavailable' and 'private' not in json.dumps(row)
    fake.collect.side_effect = None
    fake.collect.return_value = {'metrics': {'cpu_percent': float('nan')}}
    row = service.collect('fixture-host')['hosts'][0]
    assert row['error_category'] == 'invalid_response'
