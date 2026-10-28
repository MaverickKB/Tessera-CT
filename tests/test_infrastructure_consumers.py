"""Independent offline consumers of configured hosts and passive observations."""

import json
import shlex
import subprocess
from unittest.mock import Mock

import pytest

from tessera.backend import Backend
from tessera.config import Config, load_config
from tessera.credentials import CredentialResolver
from tessera.jev import JudgeKit
from test_server import call
from test_status_projection_consumers import disk_snapshot, forbid_active_work, status_server


@pytest.fixture(autouse=True)
def forbid_external_capabilities(monkeypatch):
    tripwires = []
    for owner, name in ((CredentialResolver, "resolve"), (Backend, "__init__"),
                        (JudgeKit, "_evaluate_batch")):
        tripwire = Mock(side_effect=AssertionError("Infra consumers must remain offline"))
        monkeypatch.setattr(owner, name, tripwire)
        tripwires.append(tripwire)
    for target in ("subprocess.Popen", "urllib.request.urlopen", "urllib.request.OpenerDirector.open"):
        tripwire = Mock(side_effect=AssertionError("Only injected fake collectors may run"))
        monkeypatch.setattr(target, tripwire)
        tripwires.append(tripwire)
    yield
    for tripwire in tripwires:
        tripwire.assert_not_called()


@pytest.fixture
def forbid_infra_work(monkeypatch):
    tripwires = []

    def install():
        from tessera.grafana import GrafanaClient
        from tessera.infrastructure import HostCollector, Infrastructure

        for owner, name in ((Infrastructure, "collect"), (HostCollector, "collect"), (GrafanaClient, "_get")):
            tripwire = Mock(side_effect=AssertionError("Status cannot invoke active infra operations"))
            monkeypatch.setattr(owner, name, tripwire)
            tripwires.append(tripwire)

    yield install
    for tripwire in tripwires:
        tripwire.assert_not_called()


def inventory_config(tmp_path):
    from tessera.config import HostConfig

    config = Config(home=tmp_path, cwd=tmp_path)
    config.infrastructure.enabled = True
    config.hosts["private-host-placeholder"] = HostConfig(
        name="private-host-placeholder", enabled=True, kind="local", disk_path=str(tmp_path),
        address="host.example.com", probe_command=["private-probe-placeholder"],
        services={"private-service-placeholder": []})
    return config


def assert_private_values_absent(value, tmp_path):
    encoded = json.dumps(value)
    for private in ("private-host-placeholder", "host.example.com", "private-probe-placeholder",
                    "private-service-placeholder", "private-stdout-placeholder", "private-stderr-placeholder",
                    "private-alias-placeholder", "agent.example.com", "EXAMPLE_INFRA_KEY",
                    "infra-secret-placeholder", str(tmp_path)):
        assert private not in encoded


@pytest.mark.parametrize("enabled", [False, True])
def test_empty_inventory_is_inert_across_direct_and_http_consumers(tmp_path, enabled, forbid_infra_work):
    from tessera.infrastructure import Infrastructure

    config = Config(home=tmp_path, cwd=tmp_path)
    config.infrastructure.enabled = enabled
    collector = Mock()
    before = disk_snapshot(tmp_path)
    service = Infrastructure(config, collector=collector)
    assert service.status()["hosts"] == []
    assert service.status()["configured"] is False
    with pytest.raises(ValueError):
        service.collect("private-host-placeholder")
    assert disk_snapshot(tmp_path) == before
    forbid_infra_work()
    with status_server(config) as server:
        before = disk_snapshot(tmp_path)
        for path in ("/api/infrastructure", "/api/status"):
            code, report = call(server, "GET", path)
            assert code == 200
            snapshot = report if path == "/api/infrastructure" else report["infrastructure_status"]
            assert snapshot["enabled"] is enabled
            assert snapshot["hosts"] == [] and snapshot["configured"] is False
        assert disk_snapshot(tmp_path) == before
    collector.collect.assert_not_called()


def test_configured_but_disabled_host_never_collects_or_persists(tmp_path, monkeypatch):
    from tessera.infrastructure import Infrastructure

    (tmp_path / "config.toml").write_text('''
[infrastructure]
enabled = true
[hosts.private_host_placeholder]
enabled = true
kind = "ssh"
address = "host.example.com"
ssh_alias = "private-alias-placeholder"
ssh_command = ["ssh-placeholder"]
probe_command = ["private-probe-placeholder"]
''')
    monkeypatch.setenv("TESSERA_HOSTS__PRIVATE_HOST_PLACEHOLDER__ENABLED", "false")
    config = load_config(tmp_path)
    assert config.hosts["private_host_placeholder"].enabled is False
    collector = Mock()
    before = disk_snapshot(tmp_path)
    service = Infrastructure(config, collector=collector)
    row = service.status()["hosts"][0]
    assert row["enabled"] is False and row["health"] == "disabled"
    assert "metrics" not in row
    with pytest.raises(ValueError):
        service.collect("private_host_placeholder")
    assert disk_snapshot(tmp_path) == before
    collector.collect.assert_not_called()
    assert_private_values_absent(row, tmp_path)
    assert "private_host_placeholder" not in json.dumps(row)


@pytest.mark.parametrize("kind", ["local", "ssh"])
@pytest.mark.parametrize("failure,category", [("timeout", "timeout"), ("exit", "process"),
                                                ("json", "invalid_response")])
def test_fake_probe_failures_replace_old_success_and_persist_only_finite_errors(tmp_path, kind, failure, category):
    from tessera.infrastructure import HostCollector, Infrastructure

    config = inventory_config(tmp_path)
    host = config.hosts["private-host-placeholder"]
    host.kind = kind
    host.ssh_alias = "private-alias-placeholder"
    host.ssh_command = ["ssh-placeholder"]
    host.probe_command += ["argument with spaces", "operator;literal"]
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        if kind == "local":
            assert argv == host.probe_command
        else:
            assert argv[-2] == host.ssh_alias
            assert shlex.split(argv[-1]) == host.probe_command
            assert "BatchMode=yes" in argv and "NumberOfPasswordPrompts=0" in argv
        assert kwargs["timeout"] == host.timeout and kwargs["shell"] is False
        assert kwargs["stdin"] == subprocess.DEVNULL
        if len(calls) == 1:
            return subprocess.CompletedProcess(argv, 0, '{"metrics":{"cpu_percent":14}}', "")
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, host.timeout, output="private-stdout-placeholder",
                                            stderr="private-stderr-placeholder")
        if failure == "exit":
            return subprocess.CompletedProcess(argv, 7, "private-stdout-placeholder", "private-stderr-placeholder")
        return subprocess.CompletedProcess(argv, 0, "private-stdout-placeholder", "private-stderr-placeholder")

    service = Infrastructure(config, collector=HostCollector(run=run), clock=lambda: 100.0)
    assert service.collect(host.name)["hosts"][0]["metrics"] == {"cpu_percent": 14}
    report = service.collect(host.name)
    row = report["hosts"][0]
    assert row["health"] == "unreachable" and row["error_category"] == category
    assert "metrics" not in row and "gpu" not in row and "services" not in row
    assert len(calls) == 2
    assert_private_values_absent(report, tmp_path)
    assert_private_values_absent(service.path.read_text(), tmp_path)
    before = disk_snapshot(tmp_path)
    denied = Mock()
    reopened = Infrastructure(config, collector=denied, clock=lambda: 100.0)
    assert reopened.status() == report
    assert reopened.status() == report
    denied.collect.assert_not_called()
    assert disk_snapshot(tmp_path) == before


def test_snapshot_revision_survives_elapsed_time_so_explicit_collection_can_use_it(tmp_path):
    config = inventory_config(tmp_path)
    collector = Mock()
    collector.collect.return_value = {"metrics": {"cpu_percent": 14}}
    stamp = [100.0]
    with status_server(config) as server:
        service = server.manager.infrastructure
        service.collector = collector
        service.clock = lambda: stamp[0]
        first = service.collect("private-host-placeholder")
        stamp[0] += 1
        before = disk_snapshot(tmp_path)
        code, later = call(server, "GET", "/api/infrastructure")
        assert code == 200
        assert later["hosts"][0]["health"] == "healthy"
        assert later["hosts"][0]["age_seconds"] == 1
        assert later["revision"] == first["revision"]
        for body, expected in (({"ordinal": 1, "revision": "stale-placeholder"}, 409),
                               ({"ordinal": True, "revision": later["revision"]}, 400),
                               ({"ordinal": 1, "revision": None}, 409),
                               ({"host": "private-host-placeholder", "revision": later["revision"]}, 400)):
            code, error = call(server, "POST", "/api/infrastructure/collect", body)
            assert code == expected
            assert_private_values_absent(error, tmp_path)
        assert disk_snapshot(tmp_path) == before
        collector.collect.assert_called_once()
        collector.collect.return_value = {"metrics": {"cpu_percent": 19}}
        code, collected = call(server, "POST", "/api/infrastructure/collect",
                               {"ordinal": 1, "revision": later["revision"]})
        assert code == 200 and collected["hosts"][0]["metrics"] == {"cpu_percent": 19}
        assert collector.collect.call_count == 2
        assert collected["revision"] != later["revision"]
        assert_private_values_absent(collected, tmp_path)


def test_http_status_projects_only_finite_observations_without_active_capabilities(
        tmp_path, monkeypatch, forbid_active_work, forbid_infra_work):
    from tessera.infrastructure import Infrastructure

    monkeypatch.setenv("EXAMPLE_INFRA_KEY", "infra-secret-placeholder")
    config = inventory_config(tmp_path)
    config.grafana.enabled = True
    config.grafana.base_url = "https://metrics.example.com/private-grafana-placeholder"
    config.grafana.token_env = "EXAMPLE_INFRA_KEY"
    fake = Mock()
    fake.collect.return_value = {
        "metrics": {"cpu_percent": 18, "disk_total_bytes": 1024, "address": "host.example.com"},
        "gpu": [{"utilization_percent": 11, "name": "private-device-placeholder"}],
        "services": {"private-service-placeholder": "stopped", "unlisted-service-placeholder": "running"},
        "raw_error": "infra-secret-placeholder private-stdout-placeholder"}
    service = Infrastructure(config, collector=fake)
    service.collect("private-host-placeholder")
    fake.collect.assert_called_once()
    forbid_infra_work()
    with status_server(config) as server:
        forbid_active_work()
        before = disk_snapshot(tmp_path)
        for _ in range(2):
            for path in ("/api/infrastructure", "/api/status"):
                code, report = call(server, "GET", path)
                assert code == 200
                snapshot = report if path == "/api/infrastructure" else report["infrastructure_status"]
                assert snapshot["enabled"] is True and snapshot["configured"] is True
                assert len(snapshot["hosts"]) == 1
                row = snapshot["hosts"][0]
                assert set(row) == {"ordinal", "enabled", "configured", "kind", "health", "age_seconds",
                                    "metrics", "gpu", "services"}
                assert row["ordinal"] == 1 and row["kind"] == "local" and row["health"] == "degraded"
                assert row["enabled"] is True and row["configured"] is True
                assert row["age_seconds"] >= 0
                assert row["metrics"] == {"cpu_percent": 18, "disk_total_bytes": 1024}
                assert row["gpu"] == [{"ordinal": 1, "utilization_percent": 11}]
                assert row["services"] == [{"ordinal": 1, "state": "stopped"}]
                assert_private_values_absent(report, tmp_path)
                for private in ("private-device-placeholder", "unlisted-service-placeholder",
                                "metrics.example.com", "private-grafana-placeholder"):
                    assert private not in json.dumps(report)
                if path == "/api/status":
                    assert report["grafana"] == {"enabled": True, "configured": True, "health": "unknown"}
        assert disk_snapshot(tmp_path) == before


@pytest.mark.parametrize("content", [b'{"observations":\n', b'{"observations":'])
def test_unreadable_observations_never_repair_or_reuse_success(tmp_path, content):
    from tessera.infrastructure import Infrastructure

    config = inventory_config(tmp_path)
    fake = Mock()
    fake.collect.return_value = {"metrics": {"cpu_percent": 18}}
    service = Infrastructure(config, collector=fake, clock=lambda: 100.0)
    service.collect("private-host-placeholder")
    service.path.write_bytes(content)
    before = disk_snapshot(tmp_path)
    denied = Mock()
    reopened = Infrastructure(config, collector=denied, clock=lambda: 100.0)
    for _ in range(2):
        row = reopened.status()["hosts"][0]
        assert row["health"] == "unknown"
        assert "metrics" not in row and "gpu" not in row and "services" not in row
    denied.collect.assert_not_called()
    assert disk_snapshot(tmp_path) == before


def test_global_disable_suppresses_fully_configured_inventory_without_clock_or_storage(tmp_path):
    from tessera.infrastructure import Infrastructure

    config = inventory_config(tmp_path)
    config.infrastructure.enabled = False
    collector = Mock()
    clock = Mock(side_effect=AssertionError("Disabled infrastructure must not inspect time"))
    before = disk_snapshot(tmp_path)
    service = Infrastructure(config, collector=collector, clock=clock)
    assert service.status() == {"enabled": False, "configured": True, "hosts": []}
    with pytest.raises(ValueError, match="disabled"):
        service.collect("private-host-placeholder")
    collector.collect.assert_not_called()
    clock.assert_not_called()
    assert disk_snapshot(tmp_path) == before


def test_unconfigured_ssh_does_not_invent_command_or_connection(tmp_path):
    from tessera.infrastructure import Infrastructure

    config = inventory_config(tmp_path)
    host = config.hosts["private-host-placeholder"]
    host.kind = "ssh"
    host.ssh_command = []
    denied = Mock()
    service = Infrastructure(config, collector=denied)
    before = disk_snapshot(tmp_path)
    row = service.status()["hosts"][0]
    assert row["configured"] is False and row["health"] == "unconfigured"
    with pytest.raises(ValueError, match="unconfigured"):
        service.collect(host.name)
    denied.collect.assert_not_called()
    assert disk_snapshot(tmp_path) == before


def test_agent_address_is_inventory_only_and_never_opens_http(tmp_path):
    from tessera.infrastructure import HostCollector, Infrastructure

    config = inventory_config(tmp_path)
    host = config.hosts["private-host-placeholder"]
    host.kind = "agent"
    host.agent_url = "https://agent.example.com/private-agent-placeholder"
    run = Mock(side_effect=AssertionError("Agent inventory cannot spawn a probe"))
    service = Infrastructure(config, collector=HostCollector(run=run), clock=lambda: 100.0)
    row = service.status()["hosts"][0]
    assert row["configured"] is True and row["health"] == "unknown"
    report = service.collect(host.name)
    assert report["hosts"][0]["health"] == "unreachable"
    assert report["hosts"][0]["error_category"] == "unsupported"
    run.assert_not_called()
    assert_private_values_absent(report, tmp_path)
    assert_private_values_absent(service.path.read_text(), tmp_path)
