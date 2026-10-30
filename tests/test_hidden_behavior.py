"""File-only behavior, landing receipts, dropped failures, and the trial margin.

Every behavior below is visible on the page: the one new setting (the trial
margin) is stored and actually decides. A retired local_serving_slots key is
not a live value. Prompt text is shown, not edited here.
"""

import io
import json
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from fake_backend import FakeBackend
from tessera import bundled_engine
from tessera.action_judge import DependencyResolver
from tessera.bundled_engine import (NO_ENGINE_LOG_SENTENCE, engine_log_path, engine_log_sighting,
                                    engine_log_text, ensure_bundled_engine, stop_bundled_engine)
from tessera.config import Config, load_config
from tessera.config_editor import ConfigEditor, _change_row, _redact_tree
from tessera.daemon import Daemon
from tessera.router_dependencies import REMOTE_READER
from tessera.server import create_server
from tessera.sessions import SessionManager
from tessera.settings_model import file_only, identities_field
from tessera.terminal import Terminal
from tessera.trial import RATE_MARGIN
from test_bundled_engine import packaged_assets
from test_identity_records import manager
from test_infrastructure import setup_config
from test_server import call

ROOT = Path(__file__).resolve().parents[1]
APPROVAL = {'yolo': False, 'safe_mode': 'deny_risky', 'configured': 'deny_risky'}


def test_file_only_behavior_values_are_live_and_leave_out_a_retired_key():
    config = Config()
    config.server.auth_token = 'hidden-token'
    config.agents.max_depth = 3
    config.agents.token_budget = 12
    report = file_only(config, APPROVAL)
    tables = {row['name']: row for row in report['rows']}
    for name in ('agents', 'prompt', 'compactor', 'board', 'server'):
        assert tables[name]['values'], name
    # `tools` is reached in part now: the Safe policy is the approval control, and
    # the rest of the table is listed under uncovered rather than as a dead table.
    tools = {item['key']: item for item in next(row['values'] for row in report['uncovered']
                                                if row['name'] == 'tools')}
    assert tools, 'the tool limits are still shown'
    assert 'approval' not in tools, 'the approval key has its own control'
    swarm = {item['key']: item for item in next(row['values'] for row in report['uncovered'] if row['name'] == 'swarm')}
    assert swarm['max_attempts'] == {'key': 'max_attempts', 'secret': False, 'value': 3}
    for owned in ('pressure_hold_enabled', 'pressure_soft_threshold', 'max_job_tasks', 'distinct_workers'):
        assert owned not in swarm
    agents = {item['key']: item for item in tables['agents']['values']}
    assert agents['max_depth'] == {'key': 'max_depth', 'secret': False, 'value': 3}
    assert agents['token_budget'] == {'key': 'token_budget', 'secret': False, 'value': 12}
    assert 'local_serving_slots' not in agents
    prompt = {item['key']: item for item in tables['prompt']['values']}
    assert prompt['turn_reminder']['secret'] is False
    assert 'memory_recall' in prompt['turn_reminder']['value']
    token = {item['key']: item for item in tables['server']['values']}['auth_token']
    assert token == {'key': 'auth_token', 'secret': True, 'set': True}
    assert 'hidden-token' not in json.dumps(report)


def test_the_journal_records_saves_and_outside_writes_without_secrets(tmp_path):
    path = tmp_path / 'config.toml'
    path.write_text('agent_name = "Tessera"\nidentity_max_bytes = 20000\n')
    editor = ConfigEditor(path)
    assert editor.notice_drift() == 'baseline'
    assert editor.changes() == []
    saved = editor.apply([
        {'path': ['agent_name'], 'value': 'Tessera'},
        {'path': ['server', 'auth_token'], 'value': 'hidden-token'},
        {'path': ['agents', 'token_budget'], 'value': 12},
    ])
    assert saved['changed'] and saved['backup']
    recorded = editor.changes()
    assert recorded[-1]['source'] == 'config_editor'
    assert recorded[-1]['backup'] == saved['backup']
    by_path = {item['path']: item['value'] for item in recorded[-1]['changes']}
    assert by_path['agent_name'] == 'Tessera'
    assert by_path['agents.token_budget'] == 12
    assert by_path['server.auth_token'] == 'changed (not shown)'
    assert 'hidden-token' not in json.dumps(recorded)
    path.write_text(path.read_text().replace('Tessera', 'Robin').replace('hidden-token', 'another-secret'))
    assert editor.notice_drift() == 'outside_config_editor'
    outside = editor.changes()[-1]
    assert outside['source'] == 'outside_config_editor'
    outside_values = {item['path']: item['value'] for item in outside['changes']}
    assert outside_values['agent_name'] == 'Robin'
    assert outside_values['server.auth_token'] == 'changed (not shown)'
    assert 'another-secret' not in json.dumps(editor.changes())
    integration = tmp_path / 'config.before-integration-20261027T010203.toml'
    integration.write_text(path.read_text())
    assert integration.name in {row['name'] for row in editor.backups()}


def test_prefixed_credential_names_stay_hidden_in_the_journal_and_the_observed_tree(tmp_path):
    """A credential named in capitals is still a credential.

    BRAVE_API_KEY, OPENAI_API_KEY, CLIENT_SECRET and DB_PASSWORD end in a
    credential word, so the change log and the observed snapshot keep the name
    and hide the value. token_budget and client_secret_path name a count and a
    path rather than a value, so they stay readable.
    """
    secrets = {'BRAVE_API_KEY': 'brave-value', 'OPENAI_API_KEY': 'openai-value',
               'CLIENT_SECRET': 'client-value', 'DB_PASSWORD': 'db-value'}
    readable = {'token_budget': '12', 'token_env': 'TOKEN_VARIABLE',
                'api_key_env': 'KEY_VARIABLE', 'secret_ttl_seconds': '30',
                'client_secret_path': 'vault/credential-file'}
    path = tmp_path / 'config.toml'
    path.write_text('agent_name = "Tessera"\n', encoding='utf-8')
    editor = ConfigEditor(path)
    table = {'url': 'https://plugin.example.invalid/mcp', 'env': dict(secrets, **readable)}
    saved = editor.apply([{'path': ['mcp_servers', 'desk'], 'value': table}])
    assert saved['changed'], saved
    journal = path.with_name('config-changes.jsonl').read_text(encoding='utf-8')
    observed = path.with_name('config-observed.json').read_text(encoding='utf-8')
    for name, value in secrets.items():
        assert name in journal, name
        assert name in observed, name
        assert value not in journal, name
        assert value not in observed, name
    for name, value in readable.items():
        assert name in journal and value in journal, name
        assert name in observed and value in observed, name
    assert 'changed (not shown)' in journal
    assert '"redacted": true' in observed
    # A policy switch whose name ends in api_key is not a credential.
    assert _change_row(['backends', 'alpha', 'allow_api_key'], True)['value'] is True
    assert _redact_tree(False, 'allow_api_key') is False


def test_a_table_edit_redacts_nested_secrets_before_the_journal(tmp_path):
    path = tmp_path / 'config.toml'
    path.write_text('agent_name = "Tessera"\n')
    editor = ConfigEditor(path)
    saved = editor.apply([
        {'path': ['server'], 'value': {'auth_token': 'private-value', 'port': 8740}},
    ])
    assert saved['changed']
    table = {item['path']: item['value'] for item in editor.changes()[-1]['changes']}['server']
    assert table['port'] == 8740
    assert table['auth_token'] == 'changed (not shown)'
    nested = _change_row(['mcp_servers', 'desk'], {
        'url': 'https://example.invalid/mcp',
        'env': [{'name': 'desk', 'api_key': 'list-secret', 'note': 'keep'}],
    })
    assert nested['value']['url'] == 'https://example.invalid/mcp'
    assert nested['value']['env'][0]['name'] == 'desk'
    assert nested['value']['env'][0]['note'] == 'keep'
    assert nested['value']['env'][0]['api_key'] == 'changed (not shown)'
    recorded = json.dumps(editor.changes()) + json.dumps(nested)
    assert 'private-value' not in recorded and 'list-secret' not in recorded
    assert 'private-value' not in path.with_name('config-changes.jsonl').read_text(encoding='utf-8')


def test_the_change_log_says_how_many_it_shows_and_stops_growing(tmp_path):
    path = tmp_path / 'config.toml'
    path.write_text('agent_name = "Tessera"\n[ui]\nconfig_journal_entries = 2\n', encoding='utf-8')
    editor = ConfigEditor(path)
    journal = path.with_name('config-changes.jsonl')
    rows = []
    for index in range(5):
        rows.append(json.dumps({
            'at': str(index), 'source': 'config_editor', 'summary': str(index),
            'changes': [{'path': 'agent_name', 'value': str(index)}],
        }))
    journal.write_text('\n'.join(rows) + '\n', encoding='utf-8')
    report = editor.change_report()
    assert report['keep'] == 2
    assert report['shown'] == 2
    assert report['total'] == 5
    assert [row['summary'] for row in report['entries']] == ['3', '4']
    assert len(journal.read_text(encoding='utf-8').splitlines()) == 2
    again = editor.change_report()
    assert again['shown'] == 2 and again['total'] == 2
    for name in ('One', 'Two', 'Three'):
        editor.apply([{'path': ['agent_name'], 'value': name}])
    assert len(journal.read_text(encoding='utf-8').splitlines()) == 2
    kept = [item['value'] for row in editor.changes() for item in row['changes']]
    assert 'Three' in kept and 'One' not in kept


def test_a_long_change_says_when_it_is_truncated():
    shown = _change_row(['agent_name'], 'n' * 500)
    assert isinstance(shown['value'], str)
    assert shown['value'].endswith('… (truncated)')
    assert 'n' * 500 not in shown['value']
    assert _change_row(['agent_name'], 'Tessera')['value'] == 'Tessera'
    assert _change_row(['server', 'auth_token'], 'hidden-token')['value'] == 'changed (not shown)'


def test_a_removed_key_is_recorded_as_removed(tmp_path):
    path = tmp_path / 'config.toml'
    path.write_text('agent_name = "Tessera"\nextra = "keep"\n', encoding='utf-8')
    editor = ConfigEditor(path)
    editor.notice_drift()
    saved = editor.apply([{'path': ['extra'], 'value': None}])
    assert saved['changed']
    values = {item['path']: item['value'] for item in editor.changes()[-1]['changes']}
    assert values['extra'] == 'removed'
    assert 'extra' not in path.read_text(encoding='utf-8')


def test_a_vanished_backup_does_not_blank_the_settings_list(tmp_path, monkeypatch):
    path = tmp_path / 'config.toml'
    path.write_text('agent_name = "Tessera"\n', encoding='utf-8')
    editor = ConfigEditor(path)
    kept = editor.apply([{'path': ['agent_name'], 'value': 'Tessera'}])['backup']
    dropped = tmp_path / 'config.before-settings-20260101T000000.toml'
    dropped.write_text(path.read_text(encoding='utf-8'), encoding='utf-8')
    real_stat = Path.stat

    def stat(self, *args, **kwargs):
        if getattr(stat, 'armed', False) and self.name == dropped.name:
            raise FileNotFoundError(2, 'gone')
        return real_stat(self, *args, **kwargs)

    real_is_backup = editor._is_backup

    def is_backup(candidate):
        ok = real_is_backup(candidate)
        if ok and candidate.name == dropped.name:
            stat.armed = True
        return ok

    monkeypatch.setattr(Path, 'stat', stat)
    monkeypatch.setattr(editor, '_is_backup', is_backup)
    names = [row['name'] for row in editor.backups()]
    assert kept in names
    assert dropped.name not in names


def test_a_backup_that_cannot_be_read_is_named_not_dropped(tmp_path, monkeypatch):
    path = tmp_path / 'config.toml'
    path.write_text('agent_name = "Tessera"\n', encoding='utf-8')
    editor = ConfigEditor(path)
    editor.apply([{'path': ['agent_name'], 'value': 'Tessera'}])
    real_stat = Path.stat

    def stat(self, *args, **kwargs):
        if self.name.startswith('config.before-'):
            raise PermissionError(13, 'Permission denied')
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, 'stat', stat)
    with pytest.raises(PermissionError):
        editor.backups()


def test_the_settings_view_names_a_config_directory_it_cannot_read(tmp_path, monkeypatch):
    (tmp_path / 'config.toml').write_text('agent_name = "Tessera"\n', encoding='utf-8')
    with manager(tmp_path) as sessions:
        monkeypatch.setattr(sessions.config_editor, 'backups',
                            Mock(side_effect=PermissionError(13, 'Permission denied')))
        body = sessions.settings_view()
        assert body['config_backups'] == []
        assert 'Permission denied' in body['config_backups_error']


def test_a_change_report_names_the_read_it_could_not_finish(tmp_path, monkeypatch):
    """A size or cut that cannot be read is reported, not replaced by a default."""
    path = tmp_path / 'config.toml'
    path.write_text('agent_name = "Tessera"\n', encoding='utf-8')
    editor = ConfigEditor(path)
    editor.apply([{'path': ['agent_name'], 'value': 'Tessera'}])
    real_read_text = Path.read_text

    def read_text(self, *args, **kwargs):
        if self == path:
            raise PermissionError(13, 'Permission denied')
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, 'read_text', read_text)
    report = editor.change_report()
    assert 'the change log size could not be read' in report['error']
    assert 'Permission denied' in report['error']
    monkeypatch.setattr(Path, 'read_text', real_read_text)
    monkeypatch.setattr(editor, '_trim_journal', Mock(side_effect=PermissionError(13, 'Permission denied')))
    report = editor.change_report(1)
    assert report['shown'] == 1
    assert 'the change log could not be cut to 1 records' in report['error']
    assert 'Permission denied' in report['error']


def test_a_save_that_landed_reports_a_journal_it_could_not_cut(tmp_path, monkeypatch):
    path = tmp_path / 'config.toml'
    path.write_text('agent_name = "Tessera"\n', encoding='utf-8')
    editor = ConfigEditor(path)
    first = editor.apply([{'path': ['agent_name'], 'value': 'Tessera'}])
    assert first['changed'] and 'journal_error' not in first
    monkeypatch.setattr(editor, '_trim_journal', Mock(side_effect=PermissionError(13, 'Permission denied')))
    second = editor.apply([{'path': ['agent_name'], 'value': 'Third'}])
    assert second['changed'] is True
    assert 'the change log could not be cut to size' in second['journal_error']
    assert load_config(tmp_path).agent_name == 'Third'


def test_restore_puts_the_backup_back_and_rejects_a_symlink(tmp_path):
    path = tmp_path / 'config.toml'
    path.write_text('identity_max_bytes = 20000\nagent_name = "Tessera"\n')
    editor = ConfigEditor(path)
    saved = editor.apply([{'path': ['identity_max_bytes'], 'value': 30000}])
    restored = editor.restore(saved['backup'])
    assert restored['restored'] == saved['backup']
    assert load_config(tmp_path).identity_max_bytes == 20000
    assert load_config(tmp_path).agent_name == 'Tessera'
    assert (tmp_path / restored['backup']).is_file()
    for name in ('../config.toml', 'config.before-nope.toml', 'not-a-backup.toml'):
        with pytest.raises(ValueError, match='Unknown backup'):
            editor.restore(name)
    link = tmp_path / 'config.before-settings-20260101T000000.toml'
    link.symlink_to(path)
    with pytest.raises(ValueError, match='Unknown backup'):
        editor.restore(link.name)
    assert link.name not in {row['name'] for row in editor.backups()}


def test_status_does_not_write_the_journal_and_settings_shows_a_later_edit(tmp_path):
    (tmp_path / 'config.toml').write_text('agent_name = "Tessera"\nidentity_max_bytes = 20000\n')
    config = load_config(tmp_path)
    config.server.port = 0
    with SessionManager(config) as sessions:
        before = {path: path.read_bytes() for path in tmp_path.rglob('*') if path.is_file()}
        sessions.status()
        assert {path: path.read_bytes() for path in tmp_path.rglob('*') if path.is_file()} == before
        (tmp_path / 'config.toml').write_text('agent_name = "Robin"\nidentity_max_bytes = 20000\n\n'
                                              '[server]\nauth_token = "hidden-token"\n')
        body = sessions.settings.view()
        sources = [row['source'] for row in body['config_changes']]
        assert 'outside_config_editor' in sources
        outside = next(row for row in body['config_changes'] if row['source'] == 'outside_config_editor')
        values = {item['path']: item['value'] for item in outside['changes']}
        assert values['agent_name'] == 'Robin'
        assert values['server.auth_token'] == 'changed (not shown)'
        assert 'hidden-token' not in json.dumps(body['config_changes'])
        agents = next(row for row in body['file_only']['rows'] if row['name'] == 'agents')
        assert any(item['key'] == 'max_depth' for item in agents['values'])
        assert all(item['key'] != 'local_serving_slots' for item in agents['values'])


def test_http_restore_round_trip(tmp_path):
    (tmp_path / 'config.toml').write_text('identity_max_bytes = 20000\n')
    config = load_config(tmp_path)
    config.server.port = 0
    server = create_server(config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        code, settings = call(server, 'GET', '/api/settings')
        assert code == 200
        agents = next(row for row in settings['file_only']['rows'] if row['name'] == 'agents')
        assert any(item['key'] == 'max_depth' for item in agents['values'])
        assert all(item['key'] != 'local_serving_slots' for item in agents['values'])
        code, body = call(server, 'POST', '/api/config',
                          {'edits': [{'path': ['identity_max_bytes'], 'value': 25000}]})
        assert code == 200 and body['backup']
        code, restored = call(server, 'POST', '/api/config/restore', {'name': body['backup']})
        assert code == 200 and restored['restored'] == body['backup']
        assert 'file_only' not in restored and 'sections' not in restored
        agents = next(row for row in restored['settings']['file_only']['rows'] if row['name'] == 'agents')
        assert any(item['key'] == 'max_depth' for item in agents['values'])
        assert load_config(tmp_path).identity_max_bytes == 20000
        assert call(server, 'POST', '/api/config/restore', {'name': '../config.toml'})[0] == 400
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def _agent_config(tmp_path):
    from tessera.config import AccountConfig, BackendConfig
    config = Config(home=tmp_path, cwd=tmp_path)
    config.accounts['fixture-pool'] = AccountConfig('fixture-pool', provider_kind='cli_worker',
                                                     limits={'max_concurrent': 2})
    config.backends['fixture-worker'] = BackendConfig('fixture-worker', account='fixture-pool')
    config.roles = {'main': 'fixture-worker'}
    config.swarm.enabled = True
    config.swarm.max_attempts = 1
    config.swarm.lease_seconds = 30
    config.agents.enabled = True
    config.agents.max_depth = 1
    config.agents.token_budget = 200
    config.agents.judge_tokens = 0
    config.agents.wall_seconds = 10.0
    config.agents.view_bytes = 256
    config.agents.worktrees_enabled = True
    config.agents.repo_root = str(tmp_path)
    config.agents.allowed_scopes = ['alpha', 'beta']
    config.channels.enabled = True
    config.channels.auto_deliver = False
    config.jev.enabled = False
    # bundled_path stays empty. The pytest stand-in is the memory client, and
    # an empty path does not select a receipt-less directory that would leave
    # the run with no service.
    return config


def test_a_failed_land_stores_the_exception_and_the_session_receipt(tmp_path):
    reason = 'Integration conflict; no merge was committed'
    config = _agent_config(tmp_path)
    daemon = Daemon(config, backend_factory=lambda slug, backend: FakeBackend(
        [{'content': 'Synthetic fixture evidence', 'usage': {'total_tokens': 5}}]))
    daemon.peer = SimpleNamespace(notify=lambda *args: None)
    child = tmp_path / 'child'
    child.mkdir()

    def ensure(run, save):
        if 'workspace' not in run:
            run['workspace'] = {'repo': 'fixture', 'parent': {'path': str(tmp_path)}}
            for worker in run['workers']:
                worker['workspace'] = {'path': str(child)}
            save()

    daemon.agents.worktrees.ensure = ensure
    daemon.agents.worktrees.finish = lambda run, worker: {'files': ['alpha']}
    def land(run, worker, judge):
        raise ValueError(reason)

    daemon.agents.worktrees.land = land
    try:
        parent = daemon.dispatch('thread/start', {'title': 'Landing parent'})['thread']['id']
        workers = [{'objective': 'Give the ALPHA fixture result', 'scope': ['alpha'], 'budget': {'tokens': 40}},
                   {'objective': 'Give the BETA fixture result', 'scope': ['beta'], 'budget': {'tokens': 40}}]
        run = daemon.dispatch('agent/spawn', {'threadId': parent, 'objective': 'Combine results',
                                              'workers': workers})['run']
        run = daemon.dispatch('agent/run', {'runId': run['id']})['run']
        assert run['status'] == 'ready'
        merged = daemon.dispatch('agent/merge', {'runId': run['id']})['run']
        assert merged['status'] == 'blocked'
        assert len(merged['workers']) == 2
        for worker in merged['workers']:
            assert worker['landing']['status'] == 'blocked'
            assert worker['landing']['reason'] == reason
            assert 'validation_scope_or_git' not in json.dumps(worker['landing'])
        rows = daemon.dispatch('thread/read', {'threadId': parent})['messages']
        receipts = [row for row in rows if row['meta'].get('landing_receipt') is True]
        assert len(receipts) == 2
        expected = Terminal.landing_lines(merged['workers'][0])
        assert receipts[0]['meta']['landing_lines'] == expected
        assert receipts[0]['text'] == '\n'.join(expected)
        assert expected[0] == 'Git landing receipt: blocked'
        assert 'Reason: ' + reason in expected
    finally:
        daemon.close()


def test_identity_record_errors_reach_status_and_the_people_field(tmp_path, monkeypatch):
    def refuse_operator(self, name, display_name=None):
        raise ValueError('operator name is not a handle')

    def refuse_companion(self, name, display_name=None):
        raise RuntimeError('companion journal full')

    monkeypatch.setattr('tessera.identities.IdentityRegistry.ensure_operator', refuse_operator)
    monkeypatch.setattr('tessera.identities.IdentityRegistry.ensure_companion', refuse_companion)
    home = tmp_path / 'on'
    home.mkdir()
    with manager(home, enabled=True, record_operator=True, record_companion=True) as sessions:
        errors = sessions.status()['identity_record_errors']
        assert errors == [
            {'what': 'operator', 'error': 'ValueError: operator name is not a handle'},
            {'what': 'companion', 'error': 'RuntimeError: companion journal full'},
        ]
        assert sessions.identities_view()['record_errors'] == errors
        field = identities_field(sessions.config, sessions)
        assert field['record_errors'] == errors
    off = tmp_path / 'off'
    off.mkdir()
    with manager(off) as sessions:
        assert 'identity_record_errors' not in sessions.status()
        assert identities_field(sessions.config, sessions) is None


def test_infrastructure_names_a_failed_host_and_a_crashed_collector(tmp_path):
    from tessera.infrastructure import Infrastructure
    config = setup_config(tmp_path)
    service = Infrastructure(config, collector=Mock())
    service.collect = Mock(side_effect=ValueError('probe refused'))
    report = service.collect_all()
    assert report['collection_errors'] == [{'host': 'fixture-host', 'error': 'ValueError: probe refused'}]
    assert 'collector_error' not in report
    quiet = Infrastructure(config, collector=Mock())
    assert 'collection_errors' not in quiet.status()
    config.infrastructure.collect_interval = 0.05
    crashed = Infrastructure(config, collector=Mock())

    def boom():
        raise RuntimeError('collector crashed')

    crashed.collect_all = boom
    crashed.start()
    try:
        deadline = time.time() + 2
        while time.time() < deadline and not crashed._collector_error:
            time.sleep(0.02)
    finally:
        crashed.stop()
    assert crashed.status()['collector_error'] == 'RuntimeError: collector crashed'


def test_bundled_engine_stdio_is_appended_to_a_log_the_page_can_read(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    bundle = home / 'engine'
    (bundle / 'mcp').mkdir(parents=True)
    (bundle / 'mcp' / 'aethermind_mcp.py').write_text('# test-owned entry\n', encoding='utf-8')
    (bundle / 'build-receipt.json').write_text('{}\n', encoding='utf-8')
    # The directory is this test's. It has the entry and receipt the launcher
    # needs, and the file does not store a memory switch.
    (home / 'config.toml').write_text('[aethermind]\nbundled_path = "engine"\n', encoding='utf-8')
    config = load_config(home)
    opened = {}
    payload = b'listening on 127.0.0.1:9\n'

    class Proc:
        def __init__(self):
            self.stdout = io.BytesIO(payload)

        def poll(self):
            return None

        def terminate(self):
            return None

    def popen(argv, **kwargs):
        opened['stdout'] = kwargs.get('stdout')
        opened['stderr'] = kwargs.get('stderr')
        return Proc()

    monkeypatch.setattr('tessera.bundled_engine.subprocess.Popen', popen)
    def provision(root, runtime=None):
        if runtime is not None:
            runtime.mkdir(parents=True, exist_ok=True)
        return sys.executable

    monkeypatch.setattr('tessera.bundled_engine._provision', provision)
    monkeypatch.setattr('tessera.bundled_engine._wait_for_engine', lambda *args, **kwargs: True)
    monkeypatch.setattr('tessera.bundled_engine._pick_port', lambda: 9)
    try:
        url = ensure_bundled_engine(config)
        assert url.startswith('http://127.0.0.1:')
        assert opened['stderr'] is subprocess.STDOUT
        assert opened['stdout'] is subprocess.PIPE
        log = engine_log_path(config)
        assert log is not None and log.is_relative_to(config.home)
        stop_bundled_engine(config)
        text = engine_log_text(config)
        assert text['available'] is True
        assert 'listening on 127.0.0.1:9' in text['text']
    finally:
        stop_bundled_engine(config)


def test_the_engine_log_redacts_secrets_and_keeps_the_rest(tmp_path, monkeypatch):
    packaged_assets(tmp_path, monkeypatch)
    home = tmp_path / 'home'
    home.mkdir()
    config = Config(home=home)
    config.server.auth_token = 'q7'
    monkeypatch.setenv('API_KEY', 'hidden-token-value')
    path = engine_log_path(config)
    path.parent.mkdir(parents=True)
    path.write_text(
        '--- start 2026-10-27T00:00:00Z ---\n'
        'listening on 127.0.0.1:9\n'
        'Authorization: Bearer fixture-token\n'
        'Authorization: Bearer q7\n'
        'auth_token=hidden-token-value\n'
        'child saw hidden-token-value in the header\n'
        'saw q7 in the log\n'
        '"api_key": "inline-secret-value"\n'
        'token_count=12\n'
        'engine failed: address already in use\n'
        'path runtime/aethermind/abc/engine.log\n',
        encoding='utf-8')
    text = engine_log_text(config)['text']
    assert '--- start 2026-10-27T00:00:00Z ---' in text
    assert 'listening on 127.0.0.1:9' in text
    assert 'token_count=12' in text
    assert 'address already in use' in text
    assert 'runtime/aethermind/abc/engine.log' in text
    assert 'Bearer [redacted]' in text
    assert 'auth_token=[redacted]' in text
    assert '"api_key": "[redacted]"' in text
    assert 'child saw [redacted] in the header' in text
    for secret in ('fixture-token', 'hidden-token-value', 'inline-secret-value', 'q7'):
        assert secret not in text
    config.aethermind.engine_log_max_bytes = 24
    path.write_bytes(b'x' * 80 + b'END')
    capped = engine_log_text(config)
    assert capped['text'].startswith('…\n')
    tail = capped['text'].removeprefix('…\n')
    assert tail.endswith('END')
    assert len(tail.encode('utf-8')) <= config.aethermind.engine_log_max_bytes
    assert 'x' * 80 not in capped['text']
    assert capped['cap'] == 24


def test_prefixed_credential_names_stay_hidden_in_the_engine_log(tmp_path, monkeypatch):
    """The engine log redacts a credential by name, whatever its case.

    BRAVE_API_KEY, OPENAI_API_KEY, CLIENT_SECRET and DB_PASSWORD are named
    credentials, so their values become [redacted]. token_budget, token_env,
    api_key_env, secret_ttl_seconds and client_secret_path name a count, a
    variable or a path, so they keep their text.
    """
    packaged_assets(tmp_path, monkeypatch)
    home = tmp_path / 'home'
    home.mkdir()
    config = Config(home=home)
    path = engine_log_path(config)
    path.parent.mkdir(parents=True)
    path.write_text(
        'BRAVE_API_KEY=brave-value\n'
        'OPENAI_API_KEY: openai-value\n'
        'CLIENT_SECRET=client-value\n'
        'DB_PASSWORD=db-value\n'
        'token_budget=12\n'
        'token_env=TOKEN_VARIABLE\n'
        'api_key_env=KEY_VARIABLE\n'
        'secret_ttl_seconds=30\n'
        'client_secret_path=vault/credential-file\n'
        'allow_api_key=false\n',
        encoding='utf-8')
    text = engine_log_text(config)['text']
    assert 'BRAVE_API_KEY=[redacted]' in text
    assert 'OPENAI_API_KEY: [redacted]' in text
    assert 'CLIENT_SECRET=[redacted]' in text
    assert 'DB_PASSWORD=[redacted]' in text
    for value in ('brave-value', 'openai-value', 'client-value', 'db-value'):
        assert value not in text
    for line in ('token_budget=12', 'token_env=TOKEN_VARIABLE', 'api_key_env=KEY_VARIABLE',
                 'secret_ttl_seconds=30', 'client_secret_path=vault/credential-file',
                 'allow_api_key=false'):
        assert line in text


def test_a_directory_at_the_log_path_is_named_as_a_directory(tmp_path, monkeypatch):
    packaged_assets(tmp_path, monkeypatch)
    home = tmp_path / 'home'
    home.mkdir()
    config = Config(home=home)
    path = engine_log_path(config)
    path.mkdir(parents=True)
    report = engine_log_text(config)
    assert report['available'] is False
    assert report['path'] == str(path)
    assert report['error'].endswith('the log path is a directory')
    assert 'missing' not in report


def test_an_install_with_no_bundled_engine_says_so(tmp_path, monkeypatch):
    """No engine is a fact about the install, not a log that went missing."""
    from tessera import assets
    empty = tmp_path / 'site' / 'tessera' / '_assets' / 'aethermind-engine'
    empty.mkdir(parents=True)
    monkeypatch.setattr(assets, 'asset_path', lambda name: empty)
    home = tmp_path / 'home'
    home.mkdir()
    config = load_config(home)
    report = engine_log_text(config)
    assert report['available'] is False
    assert report['reason'] == NO_ENGINE_LOG_SENTENCE
    assert 'missing' not in report
    assert report['path'] == ''


def test_a_log_path_that_cannot_be_located_is_reported_not_dropped(tmp_path, monkeypatch):
    packaged_assets(tmp_path, monkeypatch)
    home = tmp_path / 'home'
    home.mkdir()
    config = Config(home=home)

    def root(the_config):
        raise OSError(5, 'Input/output error')

    monkeypatch.setattr(bundled_engine, '_engine_root', root)
    assert engine_log_path(config) is None
    path, error = engine_log_sighting(config)
    assert path is None
    assert 'could not locate the engine log' in error
    assert 'Input/output error' in error
    assert 'could not locate the engine log' in engine_log_text(config)['error']


def test_status_carries_the_engine_log_error(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    config = load_config(home)
    with manager(home) as sessions:
        monkeypatch.setattr(bundled_engine, 'engine_log_sighting',
                            lambda the_config: (None, 'could not locate the engine log: OSError: boom'))
        status = sessions.status()['aethermind']
        assert status['engine_log'] is False
        assert status['engine_log_error'] == 'could not locate the engine log: OSError: boom'


def test_a_missing_engine_log_names_its_path(tmp_path):
    home = tmp_path / 'home'
    bundle = home / 'engine'
    (bundle / 'mcp').mkdir(parents=True)
    (bundle / 'mcp' / 'aethermind_mcp.py').write_text('# entry\n', encoding='utf-8')
    (bundle / 'build-receipt.json').write_text('{}\n', encoding='utf-8')
    (home / 'config.toml').write_text('[aethermind]\nbundled_path = "engine"\n', encoding='utf-8')
    config = load_config(home)
    report = engine_log_text(config)
    assert report['available'] is False
    assert report['missing'] is True
    assert not report.get('error')
    assert report['path'].endswith('engine.log')
    assert report['cap'] == config.aethermind.engine_log_max_bytes


def test_a_missing_build_receipt_is_the_engine_log_error(tmp_path):
    home = tmp_path / 'home'
    bundle = home / 'engine'
    bundle.mkdir(parents=True)
    (home / 'config.toml').write_text('[aethermind]\nbundled_path = "engine"\n', encoding='utf-8')
    config = load_config(home)
    report = engine_log_text(config)
    assert report['available'] is False
    assert report['error'].startswith('could not read engine log at ')
    assert 'build-receipt.json' in report['error']
    assert engine_log_path(config) is None


def test_an_unreadable_engine_log_names_the_path(tmp_path, monkeypatch):
    import pathlib
    packaged_assets(tmp_path, monkeypatch)
    home = tmp_path / 'home'
    home.mkdir()
    config = Config(home=home)
    path = engine_log_path(config)
    path.parent.mkdir(parents=True)
    path.write_text('engine said hello\n', encoding='utf-8')
    real = pathlib.Path.read_bytes

    def read_bytes(self):
        if self == path:
            raise PermissionError(13, 'Permission denied')
        return real(self)

    monkeypatch.setattr(pathlib.Path, 'read_bytes', read_bytes)
    report = engine_log_text(config)
    assert report['available'] is False
    assert report['error'].startswith(f'could not read engine log at {path}:')
    assert 'Permission denied' in report['error']
    assert 'engine said hello' not in json.dumps(report)


def test_engine_log_route_reads_the_manager_config(tmp_path):
    (tmp_path / 'config.toml').write_text('identity_max_bytes = 20000\n', encoding='utf-8')
    config = load_config(tmp_path)
    config.server.port = 0
    server = create_server(config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        live = load_config(tmp_path)
        live.aethermind.engine_log_max_bytes = 32
        server.manager.config = live
        code, body = call(server, 'GET', '/api/aethermind/engine-log')
        assert code == 200
        assert body['cap'] == 32
        assert body['available'] is False
        assert server.config.aethermind.engine_log_max_bytes == 32
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def _tailscale_slice():
    source = REMOTE_READER
    start = source.index('tailscale_by_host = {}')
    end = source.index('def identifiers')
    return source[start:end]


def test_a_failed_tailscale_status_is_named_on_the_remote_payload():
    completed = SimpleNamespace(returncode=1, stderr=b'no tailscale socket\n', stdout=b'')

    def run(argv, **kwargs):
        assert argv[:3] == ['tailscale', 'status', '--json']
        assert kwargs['stderr'] is subprocess.PIPE
        return completed

    namespace = {'subprocess': SimpleNamespace(run=run, PIPE=subprocess.PIPE), 'json': json}
    exec(_tailscale_slice(), namespace)
    assert namespace['tailscale_error'] == 'tailscale status exited 1: no tailscale socket'
    payload = {}
    if namespace['tailscale_error']:
        payload['tailscale_error'] = namespace['tailscale_error']
    assert payload['tailscale_error'].endswith('no tailscale socket')

    def missing(*args, **kwargs):
        raise FileNotFoundError('tailscale')

    namespace = {'subprocess': SimpleNamespace(run=missing, PIPE=subprocess.PIPE), 'json': json}
    exec(_tailscale_slice(), namespace)
    assert namespace['tailscale_error'].startswith('FileNotFoundError: tailscale')


def test_an_unresolved_route_still_carries_the_tailscale_error():
    config = Config()
    endpoint = 'http://router.example/v1'
    config.jev.router_probes[endpoint] = ['python3', '-']
    resolver = DependencyResolver(config.jev)
    note = 'tailscale status exited 1: no tailscale socket'
    resolver._probe = lambda command, registry: {'routes': {}, 'tailscale_error': note}
    backend = SimpleNamespace(name='main', base_url=endpoint, model='route-a')
    result = resolver.resolve(backend, SimpleNamespace(check_cancelled=lambda: None))
    assert result['resolution'] == 'unknown'
    assert result['tailscale_error'] == note


def test_a_session_trial_stores_the_margin_and_says_so_before_it_runs(tmp_path):
    home = tmp_path / 'trials'
    home.mkdir()
    config = Config(home=home, cwd=home)
    config.server.port = 0
    config.jev.enabled = False
    # The bundle path is a directory with no engine receipt, so this cannot
    # launch the vendored copy. Nothing here turns memory off.
    bundle = tmp_path / 'no-engine'
    bundle.mkdir()
    config.aethermind.bundled_path = str(bundle)
    server = create_server(config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        first = server.manager.create('Margin')['slug']
        second = server.manager.create('Default margin')['slug']
        body = {'lever': 'reasoning', 'candidate': 'high', 'turns': 2,
                'instrument': 'repeat_failures', 'why': 'counts differ'}
        code, started = call(server, 'POST', '/api/trials', {**body, 'slug': first, 'margin': 0.8})
        assert code == 200
        assert started['trial']['margin'] == RATE_MARGIN == 0.8
        assert 'at most 80% of the baseline' in started['view']['trials'][0]['line']
        code, defaulted = call(server, 'POST', '/api/trials', {**body, 'slug': second})
        assert code == 200 and defaulted['trial']['margin'] == 0.8
        assert call(server, 'POST', '/api/trials', {**body, 'slug': first, 'margin': 2})[0] == 409
        assert call(server, 'POST', '/api/trials', {**body, 'slug': first, 'margin': True})[0] == 409
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def _node(script):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is optional for the browser-free JavaScript smoke')
    result = subprocess.run([node, '-e', script], cwd=ROOT, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_settings_page_shows_live_values_backups_and_outside_writes():
    _node(r'''
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');

class Element {
  constructor(tag) {
    this.tag = tag; this.children = []; this.attrs = {}; this.dataset = {};
    this.listeners = {}; this.value = ''; this.checked = false; this.hidden = false;
    this.disabled = false; this.className = ''; this._text = ''; this.parent = null; this.type = '';
    this.classList = { add() {}, remove() {}, contains: () => false };
  }
  set textContent(text) { this._text = String(text); this.children = []; }
  get textContent() { return this._text + this.children.map(node => node.textContent).join(''); }
  append(...nodes) { for (const node of nodes) this.insertBefore(node, null); }
  replaceChildren(...nodes) { this.children = []; this._text = ''; this.append(...nodes); }
  insertBefore(node, cursor) {
    if (node.parent) node.parent.children.splice(node.parent.children.indexOf(node), 1);
    const index = cursor ? this.children.indexOf(cursor) : this.children.length;
    this.children.splice(index, 0, node); node.parent = this;
  }
  setAttribute(key, value) { this.attrs[key] = String(value); }
  getAttribute(key) { return this.attrs[key] || null; }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  async fire(type) { for (const fn of this.listeners[type] || []) await fn({ preventDefault() {} }); }
  focus() {}
  querySelector(sel) {
    const cls = sel && sel.match(/\.([A-Za-z0-9_-]+)/);
    const walk = (node) => {
      for (const child of node.children) {
        if (cls && String(child.className || '').split(/\s+/).includes(cls[1])) return child;
        const found = walk(child);
        if (found) return found;
      }
      return null;
    };
    return walk(this);
  }
  querySelectorAll(sel) {
    const cls = sel && sel.match(/^\.([A-Za-z0-9_-]+)$/);
    const out = [];
    const walk = (node) => { for (const child of node.children) { if (!cls || child.className.split(' ').includes(cls[1])) out.push(child); walk(child); } };
    walk(this);
    return out;
  }
}

const settingsData = {
  supervised: false,
  sections: [{id: 'identity', label: 'Identity', fields: [{
    id: 'identities', type: 'list-of-records', label: 'People', records: [], actions: [],
    record_errors: [{what: 'operator', error: 'ValueError: operator name is not a handle'}],
  }]}],
  file_only: {count: 2, reachable: 1, rows: [
    {name: 'agents', reason: 'Worker concurrency, depth, budget and the worker backend policy.', values: [
      {key: 'max_depth', secret: false, value: 2},
    ]},
    {name: 'server', reason: 'Listener host, port, auth and restart timing.', values: [
      {key: 'auth_token', secret: true, set: true},
      {key: 'port', secret: false, value: 0},
    ]},
  ],
  uncovered: [{name: 'swarm', reason: 'Parallel evidence jobs.', values: [
    {key: 'max_attempts', secret: false, value: 3},
  ]}]},
  config_backups: [{name: 'config.before-settings-20261027T010203.toml', bytes: 20, modified: '2026-10-27T01:02:03+00:00'}],
  config_backups_error: 'PermissionError: [Errno 13] Permission denied',
  config_changes: [{at: '2026-10-27T01:03:00+00:00', source: 'outside_config_editor',
    summary: 'config.toml changed outside ConfigEditor', changes: [
      {path: 'agent_name', value: 'Robin'},
      {path: 'extra', value: 'removed'}]}],
  config_changes_shown: 1,
  config_changes_total: 4,
};
const fresh = JSON.parse(JSON.stringify(settingsData));
fresh.file_only.rows[0].values[0].value = 9;
fresh.config_backups = [{name: 'config.before-restore-20261027T010304.toml', bytes: 20, modified: '2026-10-27T01:03:04+00:00'}];
fresh.sections = [{id: 'ui', label: 'Appearance', fields: [{id: 'ui.theme', type: 'enum', label: 'Theme', value: 'night', options: [{value: 'night', label: 'Night'}]}]}];
const calls = [];
const appearances = [];
const api = async (path, method, body) => {
  calls.push({path, method: method || 'GET', body});
  if (path === '/api/config/restore') {
    return {changed: true, restart_required: true, restored: body.name,
      detail: 'Restored ' + body.name, file_only: settingsData.file_only, settings: fresh};
  }
  return settingsData;
};
const doc = { createElement: tag => new Element(tag),
  createTextNode: text => { const node = new Element('#text'); node.textContent = String(text); return node; },
  getElementById: () => null, body: new Element('body'), addEventListener() {} };
const root = new Element('div');
const status = new Element('p');
const context = vm.createContext({ document: doc, navigator: {}, console,
  setTimeout: () => 0, clearTimeout: () => {},
  getComputedStyle: () => ({ color: 'rgb(0,0,0)' }), window: {} });
vm.runInContext(fs.readFileSync('web/settings.js', 'utf8'), context);
const view = context.window.TesseraSettings.mount({
  api, element: (tag, cls, text) => { const node = new Element(tag); if (cls) node.className = cls;
    if (text !== undefined) node.textContent = String(text); return node; },
  root, status, restartButton: new Element('button'), onRestarted() {},
  onAppearance(patch) { appearances.push(patch.theme); }, onApproval() {}, onChanged() {} });

(async () => {
  await view.load();
  const text = root.textContent;
  assert(text.includes('agents.max_depth'), text);
  assert(text.includes('swarm.max_attempts'), text);
  assert(!text.includes('pressure_hold_enabled'), text);
  assert(text.includes('agents live values'), text);
  assert(text.includes('Set (not shown)'), text);
  assert(!text.includes('hidden-token'), text);
  assert(!text.includes('local_serving_slots'), text);
  assert(text.includes('lists only the keys that control does not cover'), text);
  assert(text.includes('config.before-settings-20261027T010203.toml'), text);
  assert(text.includes('Config backups: PermissionError'), text);
  assert(text.includes('outside_config_editor'), text);
  assert(text.includes('agent_name = Robin'), text);
  assert(text.includes('Showing 1 of 4.'), text);
  assert(text.includes('extra removed'), text);
  assert(!text.includes('extra = null'), text);
  assert(!text.includes('extra = removed'), text);
  assert(text.includes('operator: ValueError: operator name is not a handle'), text);
  const walk = (node, out = []) => { out.push(node); for (const child of node.children) walk(child, out); return out; };
  const restore = walk(root).find(node => node.tag === 'button' && node.textContent === 'Restore');
  assert(restore, 'a backup has a Restore control');
  const before = calls.length;
  await restore.fire('click');
  assert.equal(calls.length, before, 'the first press confirms instead of writing');
  assert(status.textContent.includes('Choose Restore again'), status.textContent);
  const confirm = walk(root).find(node => node.tag === 'button' && node.textContent === 'Replace config.toml');
  await confirm.fire('click');
  assert.equal(String(calls.at(-1).path), '/api/config/restore');
  assert.equal(String(calls.at(-1).method), 'POST');
  assert.equal(String(calls.at(-1).body.name), 'config.before-settings-20261027T010203.toml');
  assert(root.textContent.includes('agents.max_depth9'), root.textContent);
  assert(!root.textContent.includes('agents.max_depth2'), root.textContent);
  assert(root.textContent.includes('config.before-restore-20261027T010304.toml'), root.textContent);
  assert(status.textContent.includes('Restored config.before-settings-20261027T010203.toml'), status.textContent);
  assert.equal(appearances.at(-1), 'night');
  console.log('Settings shows live values, backups and outside writes.');
})().catch(error => { console.error(error); process.exit(1); });
''')


def test_the_session_page_shows_a_landing_receipt_and_dropped_failures():
    _node(r'''
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync('web/app.js', 'utf8');
function grab(name) {
  let at = source.indexOf('async function ' + name + '(');
  if (at < 0) at = source.indexOf('function ' + name + '(');
  assert(at >= 0, name);
  let depth = 0, i = source.indexOf('{', at);
  for (; i < source.length; i += 1) {
    const ch = source[i];
    if (ch === '{') depth += 1;
    else if (ch === '}') { depth -= 1; if (depth === 0) break; }
  }
  return source.slice(at, i + 1);
}
const liveIds = {};
class El {
  constructor(tag) { this.tag = tag; this.children = []; this._id = ''; this.className = ''; this._text = ''; this.dataset = {}; this.listeners = {}; this.value = ''; this.checked = false; this.hidden = false; this.parent = null; }
  set id(value) { this._id = String(value); if (value) liveIds[this._id] = this; }
  get id() { return this._id; }
  set textContent(text) { this._text = String(text); this.children = []; }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(''); }
  append(...nodes) { for (const node of nodes) this.insertBefore(node, null); }
  insertBefore(node, cursor) {
    if (node.parent) node.parent.children.splice(node.parent.children.indexOf(node), 1);
    const index = cursor ? this.children.indexOf(cursor) : this.children.length;
    this.children.splice(Math.max(index, 0), 0, node);
    node.parent = this;
    node.parentElement = this;
  }
  replaceChildren(...nodes) { this.children = []; this._text = ''; this.append(...nodes); }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  async fire(type) { for (const fn of this.listeners[type] || []) await fn({}); }
}
const ids = {};
const element = (tag, cls, text) => { const node = new El(tag); if (cls) node.className = cls; if (text !== undefined) node.textContent = text; return node; };
const $ = id => (ids[id] ||= element('div'));
const byId = {};
const calls = [];
const context = vm.createContext({
  console, document: {
    getElementById: id => liveIds[id] || byId[id] || null,
    createDocumentFragment: () => element('fragment'),
  },
  element, state: { sessions: [], slug: '', selecting: false, selected: new Set(), status: {} },
  $ , api: async path => {
    calls.push(path);
    if (calls.length === 1) return {available: true, text: 'engine said hello', cap: 32};
    if (calls.length === 2) return {available: false, missing: true, path: '/tmp/tessera-engine.log', cap: 32};
    if (calls.length === 3) return {available: false, path: '/tmp/tessera-engine.log', cap: 32,
      error: 'could not read engine log at /tmp/tessera-engine.log: Permission denied'};
    if (calls.length === 4) return {available: false, path: '/tmp/tessera-engine.log', cap: 32,
      error: 'could not read engine log at /tmp/tessera-engine.log: the log path is a directory'};
    return {available: false, path: '', cap: 32,
      reason: 'This install has no bundled engine, so it has no engine log.'};
  },
  statusMark: () => element('span'),
  providerRecords: rows => rows,
  fleetSummary: () => '1 host',
  infrastructureHostNode: () => element('li', '', 'host'),
  paintInfrastructure: () => {},
  setIntegrationBadge: (id, label) => { context.badges[id] = label; },
  watchLongPress: () => {}, renderSelectBar: () => {}, openSessionMenu: () => {},
  toggleSelected: () => {}, selectSession: async () => {}, showError: () => {},
  badges: {},
});
for (const name of ['messageNode', 'renderSessions', 'renderInfrastructureStatus', 'memoryStatus', 'memoryAvailability', 'showMemoryError', 'memoryErrorLabel', 'memoryErrorValue', 'renderMemoryTreeStatus', 'engineLogMessage', 'engineLogCap', 'openEngineLog']) {
  vm.runInContext(grab(name) + '\nthis.' + name + ' = ' + name + ';', context);
}
const receipt = context.messageNode({
  i: 4, kind: 'work', date: '2026-10-27T01:00:00Z', text: 'ignored body',
  meta: {landing_receipt: true, landing_lines: ['Git landing receipt: blocked', 'Reason: Integration conflict; no merge was committed']},
});
assert(receipt.className.includes('landing-receipt'));
assert(receipt.textContent.includes('Git landing receipt: blocked'));
assert(receipt.textContent.includes('Reason: Integration conflict; no merge was committed'));
assert(!receipt.textContent.includes('ignored body'));
assert(!receipt.textContent.includes('validation_scope_or_git'));

context.state.status = {sessions_unreadable: [], identity_record_errors: [
  {what: 'operator', error: 'ValueError: operator name is not a handle'}]};
context.renderSessions();
assert(ids['session-list-unreadable'].textContent.includes('operator: ValueError: operator name is not a handle'));
assert.equal(ids['session-list-unreadable'].hidden, false);

context.state.status = {infrastructure_status: {enabled: true, configured: true, hosts: [{health: 'healthy'}],
  collection_errors: [{host: 'fixture-host', error: 'ValueError: probe refused'}], collector_error: 'RuntimeError: collector crashed'}};
context.renderInfrastructureStatus();
assert.equal(context.badges['infrastructure-state'], 'Collection failed');
assert(ids['infrastructure-summary'].textContent.includes('fixture-host: ValueError: probe refused'));
assert(ids['infrastructure-summary'].textContent.includes('RuntimeError: collector crashed'));

const metadata = ids['memory-tree-metadata'] || (ids['memory-tree-metadata'] = element('div'));
const parent = element('section');
parent.append(metadata);
context.state.status = {aethermind: {configured: false, engine: 'present', engine_log: false}};
context.renderMemoryTreeStatus();
const button = metadata.children.flatMap(node => [node, ...node.children]).find(node => node.tag === 'button' && node.textContent === 'Open engine log');
assert(button, metadata.textContent);
(async () => {
  await button.fire('click');
  assert.equal(calls.at(-1), '/api/aethermind/engine-log');
  assert(parent.textContent.includes('engine said hello'), parent.textContent);
  assert(parent.textContent.includes('Engine log cap: 32 bytes.'), parent.textContent);
  assert(!parent.textContent.includes('The engine log is empty.'), parent.textContent);
  await button.fire('click');
  assert(parent.textContent.includes('The engine log is missing: /tmp/tessera-engine.log.'), parent.textContent);
  assert(!parent.textContent.includes('The engine log is empty.'), parent.textContent);
  await button.fire('click');
  assert(parent.textContent.includes('could not read engine log at /tmp/tessera-engine.log: Permission denied'), parent.textContent);
  await button.fire('click');
  assert(parent.textContent.includes('the log path is a directory'), parent.textContent);
  await button.fire('click');
  assert(parent.textContent.includes('This install has no bundled engine, so it has no engine log.'), parent.textContent);
  assert(!parent.textContent.includes('The engine log is missing.'), parent.textContent);
  console.log('The session page shows the landing receipt and the dropped failures.');
})().catch(error => { console.error(error); process.exit(1); });
''')
