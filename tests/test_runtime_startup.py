"""Integration startup is owned by one session, outside the manager lock."""
import concurrent.futures
import threading

from tessera.sessions import SessionManager
from test_sessions import config_for
from fake_backend import FakeBackend


def test_slow_startup_preserves_status_other_sessions_and_single_owner(tmp_path):
    entered = threading.Event()
    release = threading.Event()
    built = []

    def backend(slug, name):
        built.append(slug)
        if slug == 'slow':
            entered.set()
            assert release.wait(10)
        return FakeBackend(['Ready'])

    with SessionManager(config_for(tmp_path), backend_factory=backend) as manager:
        manager.create('slow')
        manager.create('fast')
        with concurrent.futures.ThreadPoolExecutor() as executor:
            first = executor.submit(manager.runtime, 'slow')
            assert entered.wait(2)
            same = executor.submit(manager.runtime, 'slow')
            try:
                status = executor.submit(manager.status).result(timeout=2)
                assert status['session_count'] == 2
                assert len(executor.submit(manager.list_sessions).result(timeout=2)) == 2
                executor.submit(manager.submit, 'fast', 'Hello').result(timeout=2)
                assert manager.wait('fast', 2)
                assert list(manager.get('fast').log.iter())[-1].text == 'Ready'
            finally:
                release.set()
            assert first.result(timeout=5) is same.result(timeout=5)
        assert built.count('slow') == 1


def test_shutdown_waits_for_startup_and_closes_its_runtime(tmp_path):
    entered = threading.Event()
    release = threading.Event()
    stopping = threading.Event()

    def backend(slug, name):
        entered.set()
        assert release.wait(10)
        return FakeBackend([])

    manager = SessionManager(config_for(tmp_path), backend_factory=backend)
    manager.create('slow')
    close_autonomy = manager.autonomy.close

    def announce_close():
        stopping.set()
        close_autonomy()

    manager.autonomy.close = announce_close
    with concurrent.futures.ThreadPoolExecutor() as executor:
        pending = executor.submit(manager.runtime, 'slow')
        assert entered.wait(2)
        closing = executor.submit(manager.close)
        try:
            assert stopping.wait(2)
            assert not closing.done()
        finally:
            release.set()
        runtime = pending.result(timeout=5)
        closing.result(timeout=5)
        assert runtime.session.log._closed


def test_cached_memory_status_does_not_wait_for_network_operation():
    from tessera.aethermind import AetherMindClient
    from tessera.endpoints import EndpointPool

    entered = threading.Event()
    release = threading.Event()

    def probe(endpoint, timeout):
        entered.set()
        assert release.wait(5)

    pool = EndpointPool(['memory'], probe)
    with concurrent.futures.ThreadPoolExecutor() as executor:
        checking = executor.submit(pool.select)
        assert entered.wait(2)
        try:
            snapshot = executor.submit(pool.status).result(timeout=1)
            assert snapshot[0]['healthy'] is None
        finally:
            release.set()
        assert checking.result(timeout=2) == 'memory'

    client = AetherMindClient('http://127.0.0.1:1/mcp')
    client._observe_tree('project', 'view_available', {'view': 'Cached tree'})
    entered.clear()
    release.clear()

    def memory_operation():
        with client._lock:
            entered.set()
            assert release.wait(5)

    with concurrent.futures.ThreadPoolExecutor() as executor:
        operation = executor.submit(memory_operation)
        assert entered.wait(2)
        try:
            _, state = executor.submit(client.tree_status_snapshot, 'project').result(timeout=1)
            assert state['view_available'] is True
        finally:
            release.set()
        operation.result(timeout=2)
