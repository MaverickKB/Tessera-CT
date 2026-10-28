"""Configured inventory, explicit collectors and passive finite observations."""
from copy import deepcopy
from dataclasses import asdict
from . import locking as fcntl
from .durability import sync_directory
import hashlib
import json
import math
import os
from pathlib import Path
import shlex
import sys
import subprocess
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlencode
import uuid

from .passive_status import configured_path, read_snapshot
from .probe_process import ProbeLimitError, run_probe
from .grafana import _NoRedirect

METRICS = {'uptime_seconds', 'cpu_count', 'cpu_load1', 'cpu_percent',
           'memory_total_bytes', 'memory_available_bytes', 'memory_used_bytes',
           'disk_total_bytes', 'disk_used_bytes', 'disk_free_bytes'}
GPU_METRICS = {'utilization_percent', 'memory_total_bytes', 'memory_used_bytes', 'temperature_celsius'}
CATEGORIES = {'timeout', 'process', 'unavailable', 'invalid_response', 'unsupported'}
HEALTH = {'unknown', 'healthy', 'degraded', 'unreachable', 'stale', 'disabled', 'unconfigured'}
STATES = {'running', 'stopped', 'unknown'}


class InventoryRevisionError(ValueError):
    pass


class CollectionError(ValueError):
    def __init__(self, category):
        self.category = category if category in CATEGORIES else 'unavailable'
        super().__init__('Infrastructure collection failed: ' + self.category)


def argv_valid(value):
    return isinstance(value, list) and all(isinstance(v, str) and v and '\0' not in v for v in value)


def validate_inventory(config):
    settings = config.infrastructure
    if not argv_valid(settings.sparkdash_command):
        raise ValueError('SparkDash command must be an argv list')
    if settings.sparkdash_url:
        from .grafana import http_url
        http_url(settings.sparkdash_url)
    if (not settings.snapshot_file or settings.stale_after <= 0 or settings.max_hosts <= 0
            or settings.max_output_bytes <= 0 or not isinstance(config.hosts, dict)):
        raise ValueError('Invalid infrastructure storage or bounds')
    if len(config.hosts) > settings.max_hosts:
        raise ValueError('Host inventory exceeds configured bound')
    for name, host in config.hosts.items():
        if not isinstance(name, str) or not name or host.name != name:
            raise ValueError('Invalid host name')
        for field in ('name', 'address', 'kind', 'ssh_alias', 'agent_url', 'disk_path'):
            if not isinstance(getattr(host, field), str):
                raise ValueError('Host selectors must be text')
        if type(host.enabled) is not bool or host.kind not in ('', 'local', 'ssh', 'agent', 'prometheus'):
            raise ValueError('Invalid host enablement or kind')
        if type(host.timeout) not in (int, float) or not math.isfinite(host.timeout) or host.timeout <= 0:
            raise ValueError('Host timeout must be positive and finite')
        if not argv_valid(host.probe_command) or not argv_valid(host.ssh_command) or not isinstance(host.services, dict):
            raise ValueError('Host commands must be argv lists')
        for service, command in host.services.items():
            if not isinstance(service, str) or not service or not argv_valid(command):
                raise ValueError('Listed services require names and optional argv')
        for target in (host.address, host.ssh_alias):
            if target.startswith('-') or any(c.isspace() or c == '\0' for c in target):
                raise ValueError('Invalid host connection selector')
        if host.agent_url:
            from .grafana import http_url
            http_url(host.agent_url)
        if not isinstance(host.label, str) or len(host.label) > 80 or any(c in host.label for c in '\0\n\r'):
            raise ValueError('Host label must be short text')
        if host.kind == 'prometheus':
            if not settings.prometheus_url:
                raise ValueError('Prometheus hosts need infrastructure.prometheus_url')
            if not isinstance(host.prometheus_instance, str) or any(c in host.prometheus_instance for c in '"\\\n'):
                raise ValueError('Invalid prometheus_instance')
            for table, allowed in ((host.prometheus_queries, METRICS), (host.prometheus_gpu_queries, GPU_METRICS)):
                if not isinstance(table, dict) or set(table) - allowed:
                    raise ValueError('Prometheus queries must map known board metrics to PromQL')
                if any(not isinstance(q, str) or not q.strip() for q in table.values()):
                    raise ValueError('Prometheus queries must be PromQL text')
            if not isinstance(host.prometheus_up_query, str):
                raise ValueError('prometheus_up_query must be PromQL text')
    if settings.prometheus_url:
        from .grafana import http_url, safe_path
        http_url(settings.prometheus_url)
        safe_path(settings.prometheus_query_path, absolute=True)
    if type(settings.collect_interval) not in (int, float) or not math.isfinite(settings.collect_interval) or settings.collect_interval < 0:
        raise ValueError('infrastructure.collect_interval must be a nonnegative number')


def identity(host):
    return hashlib.sha256(json.dumps(asdict(host), sort_keys=True).encode()).hexdigest()


def configured(host):
    if host.kind == 'local':
        return True
    if host.kind == 'ssh':
        return bool((host.ssh_alias or host.address) and host.ssh_command and host.probe_command)
    if host.kind == 'prometheus':
        return bool(host.prometheus_queries or host.prometheus_gpu_queries)
    return bool(host.kind == 'agent' and host.agent_url)


def numbers(values, allowed):
    if not isinstance(values, dict):
        raise CollectionError('invalid_response')
    result = {}
    for name in allowed:
        if name not in values:
            continue
        value = values[name]
        if type(value) not in (int, float):
            raise CollectionError('invalid_response')
        try:
            if not math.isfinite(value):
                raise CollectionError('invalid_response')
        except OverflowError:
            raise CollectionError('invalid_response') from None
        if abs(value) > 2 ** 53 - 1:
            raise CollectionError('invalid_response')
        if name == 'cpu_count' and (type(value) is not int or not 0 < value <= 2 ** 53 - 1):
            raise CollectionError('invalid_response')
        if name != 'temperature_celsius' and value < 0:
            raise CollectionError('invalid_response')
        if (name.endswith('_percent') and value > 100) or (name.endswith('_bytes') and (type(value) is not int or value > 2 ** 53 - 1)):
            raise CollectionError('invalid_response')
        result[name] = value
    for prefix, fields in (('memory', ('available', 'used')), ('disk', ('free', 'used'))):
        total = result.get(prefix + '_total_bytes')
        for field in fields:
            part = result.get(prefix + '_' + field + '_bytes')
            if total is not None and part is not None and part > total:
                raise CollectionError('invalid_response')
    return result


def observation_health(metrics, gpu, services):
    health = 'healthy' if metrics else 'unknown'
    for device in gpu:
        if len(device) > 1:
            health = 'healthy'
    for service in services:
        if service['state'] == 'stopped':
            return 'degraded'
        if service['state'] == 'running':
            health = 'healthy'
    return health


def sample(data, host):
    if not isinstance(data, dict):
        raise CollectionError('invalid_response')
    metrics = numbers(data.get('metrics', {}), METRICS)
    gpu_data = data.get('gpu', [])
    services = data.get('services', {})
    if not isinstance(gpu_data, list) or not isinstance(services, dict):
        raise CollectionError('invalid_response')
    gpu = []
    for ordinal, row in enumerate(gpu_data, 1):
        gpu.append({'ordinal': ordinal, **numbers(row, GPU_METRICS)})
    states = []
    for ordinal, name in enumerate(host.services, 1):
        state = services.get(name, 'unknown')
        if not isinstance(state, str) or state not in STATES:
            state = 'unknown'
        states.append({'ordinal': ordinal, 'state': state})
    if not metrics and not any(len(g) > 1 for g in gpu) and not states:
        raise CollectionError('invalid_response')
    return {'health': observation_health(metrics, gpu, states), 'metrics': metrics, 'gpu': gpu, 'services': states}


INTEGER_METRICS = {'cpu_count'}


class PrometheusReader:
    """Instant queries against an existing Prometheus HTTP API (read-only GET)."""

    def __init__(self, base_url, query_path='/api/v1/query', *, timeout=5.0, max_bytes=1048576, opener=None):
        self.url = base_url.rstrip('/') + query_path
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.opener = opener or urllib.request.build_opener(_NoRedirect)

    def query(self, promql):
        request = urllib.request.Request(self.url + '?' + urlencode({'query': promql}), headers={'Accept': 'application/json'})
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read(self.max_bytes + 1)
        except TimeoutError:
            raise CollectionError('timeout') from None
        except (urllib.error.URLError, OSError, ValueError):
            raise CollectionError('unavailable') from None
        if len(raw) > self.max_bytes:
            raise CollectionError('invalid_response')
        try:
            data = json.loads(raw)
            if data.get('status') != 'success' or data['data']['resultType'] != 'vector':
                raise ValueError()
            rows = []
            for item in data['data']['result']:
                rows.append((item.get('metric', {}), float(item['value'][1])))
            return rows
        except (ValueError, TypeError, KeyError, IndexError):
            raise CollectionError('invalid_response') from None


def _usable(name, value):
    # Exporters report unknown readings as negative sentinels (for example -1048576); omit them.
    if not math.isfinite(value) or (value < 0 and name != 'temperature_celsius'):
        return None
    if name.endswith('_bytes') or name in INTEGER_METRICS:
        return int(round(value))
    if name.endswith('_percent'):
        return min(value, 100.0)
    return value


def prometheus_sample(host, reader):
    def fill(template):
        return template.replace('{instance}', host.prometheus_instance)
    if host.prometheus_up_query:
        rows = reader.query(fill(host.prometheus_up_query))
        if not rows or all(value < 1 for _, value in rows):
            raise CollectionError('unavailable')
    metrics = {}
    for name, template in host.prometheus_queries.items():
        rows = reader.query(fill(template))
        if rows:
            value = _usable(name, rows[0][1])
            if value is not None:
                metrics[name] = value
    gpus = {}
    for name, template in host.prometheus_gpu_queries.items():
        for labels, raw in reader.query(fill(template)):
            value = _usable(name, raw)
            if value is not None:
                gpus.setdefault(str(labels.get('gpu', labels.get('device', '0'))), {})[name] = value
    return {'metrics': metrics, 'gpu': [gpus[key] for key in sorted(gpus)]}


class HostCollector:
    def __init__(self, *, run=run_probe, max_output_bytes=65536, prometheus=None):
        self.run = run
        self.max_output_bytes = max_output_bytes
        self.prometheus = prometheus

    def _run(self, argv, timeout):
        try:
            result = self.run(argv, timeout=timeout, max_output_bytes=self.max_output_bytes,
                              stdin=subprocess.DEVNULL, shell=False, capture_output=True, text=True, check=False)
            if len(result.stdout.encode()) > self.max_output_bytes:
                raise CollectionError('invalid_response')
            return result
        except subprocess.TimeoutExpired:
            raise CollectionError('timeout') from None
        except (OSError, subprocess.SubprocessError):
            raise CollectionError('unavailable') from None
        except (ProbeLimitError, UnicodeError):
            raise CollectionError('invalid_response') from None

    def collect(self, host):
        if not host.enabled or not configured(host):
            raise CollectionError('unsupported')
        if host.kind == 'agent':
            raise CollectionError('unsupported')
        if host.kind == 'prometheus':
            if self.prometheus is None:
                raise CollectionError('unsupported')
            return prometheus_sample(host, self.prometheus)
        if host.kind == 'local' and not host.probe_command:
            return self._local(host)
        argv = host.probe_command
        if host.kind == 'ssh':
            argv = [host.ssh_command[0], '-o', 'BatchMode=yes', '-o', 'NumberOfPasswordPrompts=0',
                    '-T', *host.ssh_command[1:], '--', host.ssh_alias or host.address, shlex.join(host.probe_command)]
        result = self._run(argv, host.timeout)
        if result.returncode:
            raise CollectionError('process')
        try:
            value = json.loads(result.stdout)
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except (ValueError, TypeError):
            raise CollectionError('invalid_response') from None

    def _local(self, host):
        # Even a configured disk path can block (for example, an unavailable mount).
        # Run OS metric syscalls behind the same deadline/output/process boundary.
        deadline = time.monotonic() + host.timeout
        worker = Path(__file__).with_name('host_metrics.py')
        result = self._run([sys.executable, '-I', str(worker), host.disk_path], host.timeout)
        if result.returncode:
            raise CollectionError('process')
        try:
            data = json.loads(result.stdout)
            if not isinstance(data, dict) or not isinstance(data.get('metrics'), dict):
                raise ValueError()
        except (ValueError, TypeError):
            raise CollectionError('invalid_response') from None
        services = {}
        for name, argv in host.services.items():
            if not argv:
                services[name] = 'unknown'
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CollectionError('timeout')
            result = self._run(argv, remaining)
            services[name] = {0: 'running', 1: 'stopped'}.get(result.returncode, 'unknown')
        return {'metrics': data['metrics'], 'services': services}


class Infrastructure:
    def __init__(self, config, *, collector=None, clock=time.time):
        self.config = config
        self.path = configured_path(config.home, config.infrastructure.snapshot_file)
        self.collector = collector
        self.clock = clock
        self._lock = threading.RLock()
        self._collection_errors = []
        self._collector_error = ''

    def _read(self):
        present, records, _, size = read_snapshot(self.path, self.config.status)
        if records is None or len(records) > 1:
            return None
        if not records:
            return None if present and size else {}
        record = records[0]
        if not isinstance(record.get('observations'), dict):
            return None
        clean = {}
        for key, saved in record['observations'].items():
            if not isinstance(key, str) or len(key) != 64 or any(c not in '0123456789abcdef' for c in key):
                return None
            try:
                clean[key] = self._saved(saved)
            except (ValueError, TypeError, KeyError, OverflowError):
                return None
        return clean

    def status(self):
        settings = self.config.infrastructure
        report = {'enabled': settings.enabled, 'configured': bool(self.config.hosts), 'hosts': []}
        if not settings.enabled or not self.config.hosts:
            return report
        observations = self._read()
        now = None
        rows = []
        fingerprints = []
        for ordinal, host in enumerate(self.config.hosts.values(), 1):
            fingerprint = identity(host)
            fingerprints.append(fingerprint)
            row = {'ordinal': ordinal, 'enabled': host.enabled, 'configured': configured(host),
                   'kind': host.kind or 'unknown', 'health': 'unknown'}
            if host.label:
                row['label'] = host.label
            if host.label:
                row['label'] = host.label
            if not host.enabled:
                row['health'] = 'disabled'
            elif not row['configured']:
                row['health'] = 'unconfigured'
            elif observations is not None and fingerprint in observations:
                saved = observations[fingerprint]
                try:
                    safe = self._saved(saved)
                except (ValueError, TypeError, KeyError):
                    safe = None
                if safe is not None:
                    if now is None:
                        now = self.clock()
                    age = max(0, now - safe.pop('observed_at'))
                    row.update(safe, age_seconds=age)
                    if age > settings.stale_after:
                        row['health'] = 'stale'
            rows.append(row)
        report['hosts'] = rows
        # Hash sanitized observations and inventory fingerprints, never raw selectors.
        report['revision'] = hashlib.sha256(json.dumps({'inventory': fingerprints, 'observations': observations}, sort_keys=True).encode()).hexdigest() if observations is not None else None
        if self._collection_errors:
            report['collection_errors'] = list(self._collection_errors)
        if self._collector_error:
            report['collector_error'] = self._collector_error
        return report

    @staticmethod
    def _saved(saved):
        if not isinstance(saved, dict) or saved.get('health') not in ('unknown', 'healthy', 'degraded', 'unreachable'):
            raise ValueError()
        stamp = saved['observed_at']
        if type(stamp) not in (int, float) or not math.isfinite(stamp) or stamp < 0:
            raise ValueError()
        result = {'observed_at': stamp, 'health': saved['health']}
        if 'error_category' in saved:
            if saved['error_category'] not in CATEGORIES or saved['health'] != 'unreachable':
                raise ValueError()
            result['error_category'] = saved['error_category']
            return result
        result['metrics'] = numbers(saved.get('metrics', {}), METRICS)
        result['gpu'] = []
        for ordinal, gpu in enumerate(saved.get('gpu', []), 1):
            result['gpu'].append({'ordinal': ordinal, **numbers(gpu, GPU_METRICS)})
        result['services'] = []
        for ordinal, state in enumerate(saved.get('services', []), 1):
            if not isinstance(state, dict) or state.get('state') not in STATES:
                raise ValueError()
            result['services'].append({'ordinal': ordinal, 'state': state['state']})
        expected = observation_health(result['metrics'], result['gpu'], result['services'])
        if expected != result['health']:
            raise ValueError()
        return result

    def collect(self, host_name):
        if not self.config.infrastructure.enabled:
            raise ValueError('Infrastructure collection is disabled')
        host = self.config.hosts.get(host_name)
        if host is None or not host.enabled or not configured(host):
            raise ValueError('Host collection is disabled or unconfigured')
        host = deepcopy(host)
        try:
            collector = self.collector or self._default_collector()
            observation = sample(collector.collect(host), host)
        except CollectionError as error:
            observation = {'health': 'unreachable', 'error_category': error.category}
        except Exception:
            observation = {'health': 'unreachable', 'error_category': 'unavailable'}
        observation['observed_at'] = self.clock()
        with self._lock:
            current = self.config.hosts.get(host_name)
            if not self.config.infrastructure.enabled or current is None or identity(current) != identity(host):
                raise InventoryRevisionError('Infrastructure inventory changed; refresh before collecting')
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.with_name(self.path.name + '.lock').open('a+b') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                try:
                    observations = self._read()
                    if observations is None:
                        raise ValueError('Infrastructure observation storage is unavailable')
                    observations[identity(host)] = observation
                    known = {identity(h) for h in self.config.hosts.values()}
                    observations = {k: v for k, v in observations.items() if k in known}
                    self._save(observations)
                finally:
                    fcntl.flock(lock, fcntl.LOCK_UN)
        return self.status()

    def _default_collector(self):
        settings = self.config.infrastructure
        reader = None
        if settings.prometheus_url:
            reader = PrometheusReader(settings.prometheus_url, settings.prometheus_query_path,
                                      timeout=max(host.timeout for host in self.config.hosts.values()) if self.config.hosts else 5.0,
                                      max_bytes=settings.max_output_bytes)
        return HostCollector(max_output_bytes=settings.max_output_bytes, prometheus=reader)

    def collect_all(self):
        errors = []
        for name, host in list(self.config.hosts.items()):
            if host.enabled and configured(host):
                try:
                    self.collect(name)
                except (InventoryRevisionError, ValueError) as error:
                    errors.append({'host': name, 'error': type(error).__name__ + ': ' + str(error)})
        self._collection_errors = errors
        self._collector_error = ''
        return self.status()

    def start(self):
        """Background collection every infrastructure.collect_interval seconds (0 = off)."""
        interval = self.config.infrastructure.collect_interval
        if not self.config.infrastructure.enabled or interval <= 0 or getattr(self, '_thread', None):
            return
        self._stop = threading.Event()
        def loop():
            while not self._stop.is_set():
                try:
                    self.collect_all()
                except Exception as error:
                    self._collector_error = type(error).__name__ + ': ' + str(error)
                self._stop.wait(interval)
        self._thread = threading.Thread(target=loop, name='tessera-infrastructure', daemon=True)
        self._thread.start()

    def stop(self):
        thread = getattr(self, '_thread', None)
        if thread is not None:
            self._stop.set()
            thread.join(timeout=10)
            self._thread = None

    def collect_ordinal(self, ordinal, revision):
        with self._lock:
            snapshot = self.status()
            if not isinstance(revision, str) or snapshot.get('revision') != revision:
                raise InventoryRevisionError('Infrastructure snapshot changed or is unavailable; refresh before collecting')
            if type(ordinal) is not int or not 1 <= ordinal <= len(self.config.hosts):
                raise ValueError('Invalid infrastructure host ordinal')
            return self.collect(list(self.config.hosts)[ordinal - 1])

    def _save(self, observations):
        temporary = self.path.with_name(self.path.name + '.' + uuid.uuid4().hex + '.tmp')
        try:
            with temporary.open('x', encoding='utf-8') as stream:
                json.dump({'observations': observations}, stream, allow_nan=False)
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            sync_directory(self.path.parent)
        finally:
            temporary.unlink(missing_ok=True)
