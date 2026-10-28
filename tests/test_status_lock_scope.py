"""A status poll must not hold the manager-wide lock while it assembles its
projection.

Every sidebar poll goes through SessionManager.status(). Holding self._lock
for the whole assembly queues a poll behind every running turn's message
append, and concurrent polls serialize behind each other. That is the
multi-second spike on /api/status, /api/sessions and /api/board/view. The
manager-owned mutable state in that projection is just the two dicts of
runtimes and metadata; the poll copies them under the lock and assembles the
rest outside it.
"""
import threading
import time

from fake_backend import FakeBackend
from tessera.config import Config
from tessera.sessions import SessionManager


def config_for(root):
    config = Config(home=root, cwd=root)
    config.jev.enabled = False
    return config


class SlowCapture:
    client = None
    _inheritance = None


def slow_runtime(delay):
    """A runtime whose per-session status read takes `delay` seconds."""

    class SlowAgents:
        def active_details(self):
            time.sleep(delay)
            return []

    class SlowSession:
        agents = SlowAgents()
        backend = None
        fast_backend = None
        capture = SlowCapture()

    class SlowRuntime:
        session = SlowSession()
        pending = []
        failure = None
        inheritance = None
        active = False
        operator_stopped = False

    return SlowRuntime()


SLOW_META = {'slug': 'slow', 'title': 'slow', 'created': '2026-01-01T00:00:00Z',
             'updated': '2026-01-01T00:00:00Z', 'messages': 0, 'archived': False,
             'backend': 'fake', 'debt': 0}


def _max_lock_starvation(manager, work):
    """Run `work` while a reader pulses the manager lock; report its worst wait.

    If work holds the lock through a slow assembly, the reader starves for the
    whole assembly. If the lock only covers a dict copy, the reader never waits
    measurably at all.
    """
    waits = []
    stop = threading.Event()

    def chatter():
        while not stop.is_set():
            started = time.monotonic()
            with manager._lock:
                waits.append(time.monotonic() - started)
            time.sleep(0.01)

    chatter_thread = threading.Thread(target=chatter)
    chatter_thread.start()
    time.sleep(0.05)
    try:
        work()
    finally:
        stop.set()
        chatter_thread.join(5)
    return max(waits) if waits else 0.0


def test_status_does_not_hold_the_manager_lock_during_assembly(tmp_path):
    manager = SessionManager(config_for(tmp_path), backend_factory=lambda slug, name: FakeBackend(['Answer']))
    with manager._lock:
        manager._runtimes['slow'] = slow_runtime(0.4)
        manager._metadata['slow'] = dict(SLOW_META)
    try:
        # Before the fix, the 0.4s per-session read ran inside the manager
        # lock and the reader starved for the whole of it (~0.4s measured at
        # base). After the fix the lock covers only the dict copy.
        starvation = _max_lock_starvation(manager, manager.status)
        assert starvation < 0.15, f'a lock read starved {starvation:.2f}s behind status assembly'
    finally:
        with manager._lock:
            manager._runtimes.pop('slow', None)
            manager._metadata.pop('slow', None)
        manager.close()


def test_a_turn_can_append_while_a_status_poll_is_assembling(tmp_path):
    """The other side of the same fault: a poll must not delay turn work."""
    manager = SessionManager(config_for(tmp_path), backend_factory=lambda slug, name: FakeBackend(['Answer']))
    with manager._lock:
        manager._runtimes['slow'] = slow_runtime(0.4)
        manager._metadata['slow'] = dict(SLOW_META)
    try:
        poll = threading.Thread(target=manager.status)
        poll.start()
        time.sleep(0.1)  # the poll is inside its per-session assembly
        started = time.monotonic()
        with manager._lock:  # what a turn's message append takes
            pass
        waited = time.monotonic() - started
        poll.join(5)
        # At base this append waited out the poll's whole 0.4s assembly.
        assert waited < 0.15, f'a turn append waited {waited:.2f}s behind a status poll'
    finally:
        with manager._lock:
            manager._runtimes.pop('slow', None)
            manager._metadata.pop('slow', None)
        manager.close()
