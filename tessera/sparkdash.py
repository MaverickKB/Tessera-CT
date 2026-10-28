"""Read SparkDash through an operator-configured command; credentials stay at source."""
import json
from pathlib import Path
import subprocess
import threading
import time

RANGES = {'1h', '6h', '24h', '7d', '30d'}
_lock = threading.Lock()
_cache = {}


def snapshot(settings, range_name='24h'):
    if range_name not in RANGES:
        raise ValueError('Unknown SparkDash history range')
    command = settings.sparkdash_command
    if not command:
        return {'configured': False}
    key = (tuple(command), range_name)
    with _lock:
        cached = _cache.get(key)
        if cached and time.monotonic() - cached[0] < 30:
            return cached[1]
        script = Path(__file__).with_name('sparkdash_remote.py').read_text()
        try:
            result = subprocess.run([*command, range_name], input=script, text=True,
                                    capture_output=True, timeout=35, check=True)
            if len(result.stdout) > 8 * 1024 * 1024:
                raise ValueError('Snapshot too large')
            data = json.loads(result.stdout)
            if not isinstance(data.get('sparks'), list):
                raise ValueError('Invalid snapshot')
        except (OSError, subprocess.SubprocessError, ValueError):
            # Command stderr can contain deployment secrets, so keep errors bounded.
            return {'configured': True, 'error': 'SparkDash read unavailable'}
        data['configured'] = True
        data['source_url'] = settings.sparkdash_url
        _cache[key] = (time.monotonic(), data)
        return data
