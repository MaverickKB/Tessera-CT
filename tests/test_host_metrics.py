"""Built-in metrics run behind the same bounded subprocess boundary."""
import json
import subprocess
import sys
from unittest.mock import Mock

import pytest

from test_infrastructure import setup_config


def test_builtin_collection_uses_bounded_runtime_worker_before_service_commands(tmp_path):
    from tessera.infrastructure import HostCollector
    host=setup_config(tmp_path).hosts['fixture-host']
    host.services={'private-service':['service-placeholder']}
    calls=[]
    def run(argv,**kwargs):
        calls.append((argv,kwargs))
        return subprocess.CompletedProcess(argv,0,json.dumps({'metrics':{'cpu_count':2,'disk_total_bytes':2048}}),'')
    data=HostCollector(run=run).collect(host)
    assert calls[0][0][0] == sys.executable
    assert calls[0][0][-1] == host.disk_path
    assert calls[0][1]['timeout'] <= host.timeout
    assert calls[1][0] == ['service-placeholder'] and calls[1][1]['timeout'] <= host.timeout
    assert data['metrics']['cpu_count'] == 2 and data['services'] == {'private-service':'running'}


def test_runtime_worker_gathers_actual_local_os_metrics_only_when_called(tmp_path):
    from tessera.host_metrics import collect_metrics
    metrics=collect_metrics(str(tmp_path))
    assert metrics['cpu_count'] > 0 and metrics['disk_total_bytes'] >= metrics['disk_free_bytes']


VM_STAT = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                                     4152.
Pages active:                                 213712.
Pages inactive:                               207362.
Pages speculative:                              5498.
Pages throttled:                                   0.
Pages wired down:                             225164.
Pages purgeable:                                 538.
"""


def _stub_vm_stat(monkeypatch, stdout, returncode=0):
    import tessera.host_metrics as host_metrics
    calls = []
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, returncode, stdout, '')
    monkeypatch.setattr(host_metrics.subprocess, 'run', run)
    return calls


def test_darwin_available_memory_reads_vm_stat_pages(monkeypatch):
    """Available memory on Darwin is the reclaimable pages, not a fabricated zero."""
    from tessera.host_metrics import _darwin_available_memory
    calls = _stub_vm_stat(monkeypatch, VM_STAT)
    expected = (4152 + 207362 + 5498 + 538) * 16384
    assert _darwin_available_memory() == expected
    assert calls[0][0] == ['vm_stat'] and calls[0][1]['timeout'] <= 10


def test_darwin_available_memory_uses_the_reported_page_size(monkeypatch):
    """The page size comes from the header, never assumed to be 4096."""
    from tessera.host_metrics import _darwin_available_memory
    _stub_vm_stat(monkeypatch, VM_STAT.replace('16384', '4096'))
    assert _darwin_available_memory() == (4152 + 207362 + 5498 + 538) * 4096


@pytest.mark.parametrize('stdout,returncode', [
    ('', 0),
    ('vm_stat: command not found', 1),
    ('Mach Virtual Memory Statistics: (page size of 16384 bytes)\n', 0),
    ('no header here\nPages free: 10.\n', 0),
    ('Mach Virtual Memory Statistics: (page size of abc bytes)\nPages free: 10.\n', 0),
])
def test_darwin_available_memory_returns_none_when_unusable(monkeypatch, stdout, returncode):
    """Absence stays absence: a broken vm_stat must not read as zero memory."""
    from tessera.host_metrics import _darwin_available_memory
    _stub_vm_stat(monkeypatch, stdout, returncode)
    assert _darwin_available_memory() is None


def test_darwin_available_memory_survives_a_missing_binary(monkeypatch):
    import tessera.host_metrics as host_metrics
    def run(argv, **kwargs):
        raise FileNotFoundError(argv[0])
    monkeypatch.setattr(host_metrics.subprocess, 'run', run)
    assert host_metrics._darwin_available_memory() is None


def test_darwin_available_memory_survives_a_timeout(monkeypatch):
    import tessera.host_metrics as host_metrics
    def run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs.get('timeout'))
    monkeypatch.setattr(host_metrics.subprocess, 'run', run)
    assert host_metrics._darwin_available_memory() is None


def test_collect_metrics_prefers_sysconf_when_it_reports_available(monkeypatch):
    """Linux keeps the sysconf path; the Darwin fallback must not double-report."""
    import tessera.host_metrics as host_metrics
    monkeypatch.setattr(host_metrics.os, 'sysconf_names', {'SC_PHYS_PAGES', 'SC_AVPHYS_PAGES', 'SC_PAGE_SIZE'})
    monkeypatch.setattr(host_metrics.os, 'sysconf', lambda name: {'SC_PHYS_PAGES': 1000, 'SC_AVPHYS_PAGES': 400, 'SC_PAGE_SIZE': 4096}[name])
    monkeypatch.setattr(host_metrics.sys, 'platform', 'linux')
    def run(argv, **kwargs):
        raise AssertionError('vm_stat must not run when sysconf reports available memory')
    monkeypatch.setattr(host_metrics.subprocess, 'run', run)
    metrics = host_metrics.collect_metrics()
    assert metrics['memory_total_bytes'] == 1000 * 4096
    assert metrics['memory_available_bytes'] == 400 * 4096


def test_collect_metrics_falls_back_on_darwin_without_sysconf_available(monkeypatch):
    import tessera.host_metrics as host_metrics
    monkeypatch.setattr(host_metrics.os, 'sysconf_names', {'SC_PHYS_PAGES', 'SC_PAGE_SIZE'})
    # Total must exceed the vm_stat figure: 2_000_000 * 4096 = 8.19 GB > 6.84 GB.
    monkeypatch.setattr(host_metrics.os, 'sysconf', lambda name: {'SC_PHYS_PAGES': 2_000_000, 'SC_PAGE_SIZE': 4096}[name])
    monkeypatch.setattr(host_metrics.sys, 'platform', 'darwin')
    _stub_vm_stat(monkeypatch, VM_STAT)
    metrics = host_metrics.collect_metrics()
    assert metrics['memory_total_bytes'] == 2_000_000 * 4096
    assert metrics['memory_available_bytes'] == (4152 + 207362 + 5498 + 538) * 16384


def test_collect_metrics_rejects_available_larger_than_total(monkeypatch):
    """A vm_stat figure above physical memory is unusable, so it is dropped."""
    import tessera.host_metrics as host_metrics
    monkeypatch.setattr(host_metrics.os, 'sysconf_names', {'SC_PHYS_PAGES', 'SC_PAGE_SIZE'})
    monkeypatch.setattr(host_metrics.os, 'sysconf', lambda name: {'SC_PHYS_PAGES': 1000, 'SC_PAGE_SIZE': 4096}[name])
    monkeypatch.setattr(host_metrics.sys, 'platform', 'darwin')
    _stub_vm_stat(monkeypatch, VM_STAT)
    metrics = host_metrics.collect_metrics()
    assert metrics['memory_total_bytes'] == 1000 * 4096
    assert 'memory_available_bytes' not in metrics


def test_collect_metrics_omits_available_when_the_fallback_cannot_read(monkeypatch):
    import tessera.host_metrics as host_metrics
    monkeypatch.setattr(host_metrics.os, 'sysconf_names', {'SC_PHYS_PAGES', 'SC_PAGE_SIZE'})
    monkeypatch.setattr(host_metrics.os, 'sysconf', lambda name: {'SC_PHYS_PAGES': 1000, 'SC_PAGE_SIZE': 4096}[name])
    monkeypatch.setattr(host_metrics.sys, 'platform', 'darwin')
    _stub_vm_stat(monkeypatch, '', 1)
    metrics = host_metrics.collect_metrics()
    assert 'memory_available_bytes' not in metrics
