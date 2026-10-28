"""Status reads saved records without constructing execution or repairing storage."""
import json
import threading
from unittest.mock import patch

from tessera.config import Config
from tessera.server import create_server
from tessera.sessions import SessionManager
from tessera.tasks import TaskStore
from test_server import call


def forbidden(*args, **kwargs):
    raise AssertionError('Passive status cannot execute, resolve, probe or repair')


def test_task_and_judge_status_include_saved_unloaded_sessions_without_leaks(tmp_path, monkeypatch):
    config = Config(home=tmp_path)
    config.swarm.enabled = True
    config.jev.credential_ref = 'private-reference'
    config.credentials = {'private-reference': {'keychain_service': 'private-service', 'keychain_account': 'private-account'}}
    with SessionManager(config) as manager:
        manager.create('Private subject')
    board = TaskStore(tmp_path / config.swarm.tasks_file, enabled=True)
    board.create('private-objective', ['private-scope'], {}, 'private-account')
    log = tmp_path / 'sessions' / 'private-subject' / 'decisions.jsonl'
    log.write_text(json.dumps({'point': 'verification_batch', 'model_id': 'private-model', 'prompt': 'private-prompt'}) + '\n')
    before = {path: path.read_bytes() for path in tmp_path.rglob('*') if path.is_file()}
    with SessionManager(config) as manager:
        monkeypatch.setattr(manager, 'runtime', forbidden)
        monkeypatch.setattr('tessera.tasks.TaskStore._load', forbidden)
        monkeypatch.setattr('tessera.credentials.CredentialResolver.resolve', forbidden)
        monkeypatch.setattr('urllib.request.OpenerDirector.open', forbidden)
        monkeypatch.setattr('subprocess.Popen', forbidden)
        result = manager.status()
        assert result['task_status'] == {'enabled': True, 'configured': True, 'tasks': [{'status': 'queued', 'attempt': 0}]}
        assert result['jev'] == {'enabled': False, 'configured': True, 'decision_log_present': True,
                                 'decision_count': 1, 'last_decision_category': 'verification'}
        assert manager._runtimes == {}
    encoded = json.dumps([result['task_status'], result['jev']])
    for private in ('private-', str(tmp_path)):
        assert private not in encoded
    assert all(path.read_bytes() == data for path, data in before.items())


def test_missing_disabled_and_incomplete_files_are_honest_and_unmodified(tmp_path):
    config = Config(home=tmp_path)
    with SessionManager(config) as manager:
        result = manager.status()
        assert result['task_status'] == {'enabled': False, 'configured': True}
        assert result['jev']['decision_log_present'] is False and result['jev']['decision_count'] == 0
        assert not (tmp_path / 'tasks.jsonl').exists()
        config.swarm.enabled = True
        board = TaskStore(tmp_path / 'tasks.jsonl', enabled=True)
        board.create('Read', ['A'], {}, 'one')
        with board.path.open('ab') as stream:
            stream.write(b'{incomplete')
        before = board.path.read_bytes()
        assert manager.status()['task_status']['tasks'] == [{'status': 'queued', 'attempt': 0}]
        assert board.path.read_bytes() == before
        assert not list(tmp_path.glob('*.incomplete-*'))


def test_bounded_or_corrupt_records_omit_unknown_counts_and_task_arrays(tmp_path):
    config = Config(home=tmp_path)
    config.swarm.enabled = True
    config.status.max_file_bytes = 100
    with SessionManager(config) as manager:
        subject = manager.create('Saved')
        log = manager.path(subject['slug']) / 'decisions.jsonl'
        log.write_text('x' * 101)
        (tmp_path / 'tasks.jsonl').write_text('invalid complete record\n')
        status = manager.status()
        assert status['jev']['decision_log_present'] is True
        assert 'decision_count' not in status['jev'] and 'last_decision_category' not in status['jev']
        assert 'tasks' not in status['task_status']


def test_http_status_exposes_contract_without_starting_session_or_scheduler(tmp_path, monkeypatch):
    config = Config(home=tmp_path)
    config.server.port = 0
    config.swarm.enabled = True
    server = create_server(config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setattr('tessera.scheduler.Scheduler.from_config', forbidden)
        monkeypatch.setattr('tessera.credentials.CredentialResolver.resolve', forbidden)
        code, status = call(server, 'GET', '/api/status')
        assert code == 200 and status['task_status']['tasks'] == []
        assert status['jev']['decision_count'] == 0
        assert server.manager._runtimes == {}
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_bounded_log_scope_does_not_report_unproven_absence(tmp_path):
    config = Config(home=tmp_path)
    config.status.max_logs = 1
    with SessionManager(config) as manager:
        manager.create('one')
        manager.create('two')
        (manager.path('two') / 'decisions.jsonl').write_text('{"point":"memory"}\n')
        judge = manager.status()['jev']
        assert 'decision_count' not in judge
        assert judge.get('decision_log_present') is not False


def test_task_rows_are_bounded_and_attempt_values_are_finite_safe_integers(tmp_path):
    config = Config(home=tmp_path)
    config.swarm.enabled = True
    config.status.task_limit = 1
    board = TaskStore(tmp_path / 'tasks.jsonl', enabled=True)
    board.create('First', ['A'], {}, 'one')
    board.create('Second', ['B'], {}, 'one')
    with SessionManager(config) as manager:
        report = manager.status()['task_status']
        assert report['tasks'] == [{'status': 'queued', 'attempt': 0}]
        # task_limit is a bound, not a filter: one row was dropped and the reader
        # has to be able to tell that the visible list is not the whole store.
        assert report['tasks_omitted'] == 1
        assert report['task_limit'] == 1


def test_total_log_read_budget_omits_incomplete_scope_counts(tmp_path):
    config = Config(home=tmp_path)
    config.status.max_total_bytes = 1000
    with SessionManager(config) as manager:
        for name in ('one', 'two'):
            manager.create(name)
            (manager.path(name) / 'decisions.jsonl').write_text(json.dumps({'point': 'memory', 'padding': 'x' * 600}) + '\n')
        judge = manager.status()['jev']
        assert judge['decision_log_present'] is True
        assert 'decision_count' not in judge and 'last_decision_category' not in judge


def test_symlinked_decision_file_is_neither_followed_nor_claimed_present(tmp_path):
    config = Config(home=tmp_path)
    private = tmp_path / 'unrelated-private-file'
    private.write_text('{"point":"memory"}\n')
    with SessionManager(config) as manager:
        manager.create('one')
        (manager.path('one') / 'decisions.jsonl').symlink_to(private)
        judge = manager.status()['jev']
        assert judge.get('decision_log_present') is not True
        assert 'decision_count' not in judge
