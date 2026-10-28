"""Read-only occupancy observations from configured engine health and metrics."""

import json
import math
import re
from queue import Empty, Queue
from threading import Thread
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener

TIMEOUT = 2.5
MAX_BYTES = 1024 * 1024


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def validate_engines(engines):
    if not isinstance(engines, list):
        raise ValueError('infrastructure.pressure_engines must be a list')
    for engine in engines:
        if not isinstance(engine, dict) or set(engine) != {'host', 'port', 'model', 'slots'}:
            raise ValueError('Pressure engines require host, port, model and slots')
        host = engine['host']
        if (not isinstance(host, str) or not host
                or re.fullmatch(r'[A-Za-z0-9._-]+', host) is None):
            raise ValueError('Pressure engine host must be a bare hostname or IPv4 address')
        if type(engine['port']) is not int or not 1 <= engine['port'] <= 65535:
            raise ValueError('Pressure engine port must be between 1 and 65535')
        if not isinstance(engine['model'], str) or not engine['model'].strip():
            raise ValueError('Pressure engine model must be text')
        if type(engine['slots']) is not int or engine['slots'] <= 0:
            raise ValueError('Pressure engine slots must be a positive integer')


def _read_http(url, timeout):
    # Talk directly to the configured source, without environment proxies or redirects.
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    with opener.open(url, timeout=timeout) as response:
        body = response.read(MAX_BYTES + 1)
    if len(body) > MAX_BYTES:
        raise ValueError('Engine response exceeds size limit')
    return body.decode('utf-8')


def _get(url, timeout):
    # Socket timeouts alone do not bound DNS or a response that trickles bytes.
    # A daemon owns only this read; the caller stops waiting at the deadline.
    result = Queue(maxsize=1)

    def read():
        try:
            result.put((_read_http(url, timeout), None))
        except Exception as error:
            result.put((None, error))

    Thread(target=read, daemon=True).start()
    try:
        body, error = result.get(timeout=timeout)
    except Empty:
        raise TimeoutError('Engine GET exceeded read deadline') from None
    if error is not None:
        raise error
    return body


def _occupancy(metrics, name):
    for line in metrics.splitlines():
        parts = line.split()
        if parts and parts[0] == name:
            value = float(parts[1])
            if not math.isfinite(value) or value < 0 or not value.is_integer():
                raise ValueError('Invalid engine occupancy: ' + name)
            return int(value)
    raise ValueError('Missing engine metric: ' + name)


def sample_engine(engine, transport=None):
    """Return unknown occupancy on any failure; never infer idle from silence.

    Each of the two GETs has a 2.5 second wall-clock wait bound. No observation is saved.
    The configured slots are a fallback; a healthy source reports its own ceiling.
    """
    reading = {
        'host': engine['host'], 'port': engine['port'], 'model': engine['model'],
        'slots_total': engine['slots'], 'running': None, 'waiting': None,
        'warming': None, 'free': None, 'reachable': False, 'error': None,
    }
    if transport is None:
        transport = _get
    try:
        base = f"http://{engine['host']}:{engine['port']}"
        health = json.loads(transport(base + '/health', TIMEOUT))
        metrics = transport(base + '/metrics', TIMEOUT)
        slots = health['max_batch_size']
        warming = health['warming']
        if type(slots) is not int or slots <= 0 or type(warming) is not bool:
            raise ValueError('Invalid engine health capacity or warming flag')
        running = _occupancy(metrics, 'tensorfold:requests_running')
        waiting = _occupancy(metrics, 'tensorfold:requests_waiting')
        reading.update(slots_total=slots, running=running, waiting=waiting,
                       warming=warming, free=max(0, slots - running - int(warming)),
                       reachable=True)
    except Exception as error:
        reading['error'] = str(error) or type(error).__name__
    return reading


def snapshot(settings, transport=None):
    readings = []
    for engine in settings.pressure_engines:
        readings.append(sample_engine(engine, transport))
    return {'engines': readings}
