"""OS-only metric worker; invoked explicitly inside the bounded probe process."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

# Darwin has no SC_AVPHYS_PAGES. Available memory comes from the Mach VM
# statistics instead. Counted as reclaimable without pressure: free, inactive,
# speculative and purgeable pages. Wired, active and compressor pages are in
# use, so they are deliberately excluded.
_VM_STAT_PAGES = ('Pages free', 'Pages inactive', 'Pages speculative', 'Pages purgeable')


def _darwin_available_memory(timeout=5):
    """Available bytes from vm_stat, or None when the tool is absent or garbled.

    Absence stays absence. A zero here would read as "the machine is out of
    memory" on a board that cannot tell the difference.
    """
    try:
        result = subprocess.run(['vm_stat'], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode:
        return None
    header = re.search(r'page size of (\d+) bytes', result.stdout)
    if not header:
        return None
    try:
        page_size = int(header.group(1))
    except ValueError:
        return None
    if page_size <= 0:
        return None
    total_pages = 0
    seen = 0
    for name in _VM_STAT_PAGES:
        found = re.search(r'^' + re.escape(name) + r':\s+(\d+)\.', result.stdout, re.M)
        if not found:
            continue
        total_pages += int(found.group(1))
        seen += 1
    if not seen:
        return None
    return total_pages * page_size


def collect_metrics(disk_path=''):
    metrics = {}
    count = os.cpu_count()
    if count is not None:
        metrics['cpu_count'] = count
    if hasattr(os, 'getloadavg'):
        try:
            metrics['cpu_load1'] = os.getloadavg()[0]
        except OSError:
            pass
    for clock_name in ('CLOCK_BOOTTIME', 'CLOCK_UPTIME_RAW'):
        if hasattr(time, clock_name):
            metrics['uptime_seconds'] = time.clock_gettime(getattr(time, clock_name))
            break
    if hasattr(os, 'sysconf'):
        for source, target in (('SC_PHYS_PAGES', 'memory_total_bytes'), ('SC_AVPHYS_PAGES', 'memory_available_bytes')):
            if source in os.sysconf_names:
                value = os.sysconf(source)
                if value >= 0:
                    metrics[target] = value * os.sysconf('SC_PAGE_SIZE')
    if 'memory_available_bytes' not in metrics and sys.platform == 'darwin':
        available = _darwin_available_memory()
        if available is not None and metrics.get('memory_total_bytes', 0) >= available:
            metrics['memory_available_bytes'] = available
    if disk_path:
        disk = shutil.disk_usage(Path(disk_path).expanduser())
        metrics.update(disk_total_bytes=disk.total, disk_used_bytes=disk.used, disk_free_bytes=disk.free)
    return metrics


if __name__ == '__main__':
    print(json.dumps({'metrics': collect_metrics(sys.argv[1] if len(sys.argv) > 1 else '')}, allow_nan=False))
