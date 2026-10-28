"""The combined status changes preserve the narrow payload and lock scope."""
from tessera.sessions import SessionManager
from test_status_lock_scope import config_for, slow_runtime, SLOW_META, _max_lock_starvation


def test_connection_skips_board_and_preserves_shared_fields(tmp_path, monkeypatch):
    manager = SessionManager(config_for(tmp_path))
    calls = []
    monkeypatch.setattr('tessera.sessions.board_status', lambda *args: calls.append(args) or {'sentinel': True})
    try:
        narrow = manager.status(connection=True)
        assert calls == []
        heavy = {'agent_backends', 'provider_status', 'task_status', 'job_status', 'board_status', 'infrastructure_status', 'grafana'}
        assert heavy.isdisjoint(narrow)
        full = manager.status()
        assert len(calls) == 1
        assert full['board_status'] == {'sentinel': True}
        assert heavy <= full.keys()
        for key in ('sessions', 'session_count', 'debt_total', 'backends', 'roles', 'aethermind'):
            assert narrow[key] == full[key]
    finally:
        manager.close()


def test_connection_assembly_releases_manager_lock(tmp_path):
    manager = SessionManager(config_for(tmp_path))
    with manager._lock:
        manager._runtimes['slow'] = slow_runtime(0.4)
        manager._metadata['slow'] = dict(SLOW_META)
    try:
        assert _max_lock_starvation(manager, lambda: manager.status(connection=True)) < 0.15
    finally:
        with manager._lock:
            manager._runtimes.pop('slow', None)
            manager._metadata.pop('slow', None)
        manager.close()
