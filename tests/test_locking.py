"""Local lock ownership survives contention and process exit."""
import subprocess
import sys

import pytest

from tessera import locking


def test_nonblocking_contention_and_release(tmp_path):
    path = tmp_path / 'owner.lock'
    with path.open('a+b') as first, path.open('a+b') as second:
        locking.flock(first, locking.LOCK_EX | locking.LOCK_NB)
        with pytest.raises(BlockingIOError):
            locking.flock(second, locking.LOCK_EX | locking.LOCK_NB)
        locking.flock(first, locking.LOCK_UN)
        locking.flock(second, locking.LOCK_EX | locking.LOCK_NB)
        locking.flock(second, locking.LOCK_UN)


def test_process_exit_releases_ownership(tmp_path):
    path = tmp_path / 'owner.lock'
    script = '''
import sys
from tessera import locking
with open(sys.argv[1], 'a+b') as stream:
    locking.flock(stream, locking.LOCK_EX | locking.LOCK_NB)
    print('locked', flush=True)
    sys.stdin.readline()
'''
    child = subprocess.Popen([sys.executable, '-c', script, str(path)],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == 'locked'
        with path.open('a+b') as stream:
            with pytest.raises(BlockingIOError):
                locking.flock(stream, locking.LOCK_EX | locking.LOCK_NB)
        child.communicate(chr(10), timeout=5)
        assert child.returncode == 0
        with path.open('a+b') as stream:
            locking.flock(stream, locking.LOCK_EX | locking.LOCK_NB)
            locking.flock(stream, locking.LOCK_UN)
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=5)
