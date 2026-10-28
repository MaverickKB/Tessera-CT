"""Detached inspector over the resident daemon's Unix socket. Offline fixtures only."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading

import pytest

from fake_backend import FakeBackend
from tessera.config import Config, InspectorConfig, load_config
from tessera.daemon import Daemon
from tessera.inspector_client import InspectorClient, InspectorError, apply, propose, rollback
from tessera.sessions import SessionManager

ROOT = Path(__file__).resolve().parents[1]
APPROVAL = 'TESSERA_TEST_OPERATOR_APPROVAL'


@pytest.fixture
def short(tmp_path):
    # AF_UNIX paths are limited (104 bytes on macOS); keep the socket path short.
    path = Path(tempfile.mkdtemp(prefix='tsi', dir='/tmp'))
    yield path
    shutil.rmtree(path, ignore_errors=True)


def resident_config(home, sock, source_root=''):
    config = Config(home=home, cwd=home)
    config.tools.approval = 'ask'
    config.inspector = InspectorConfig(enabled=True, socket_path=str(sock), source_root=str(source_root),
                                       operator_approval_env=APPROVAL)
    return config


@contextmanager
def resident(tmp_path, short, replies=('Resident reply',), source_root=''):
    home = tmp_path / 'resident'
    home.mkdir()
    sock = short / 'run' / 'inspector.sock'
    daemon = Daemon(resident_config(home, sock, source_root),
                    backend_factory=lambda *args: FakeBackend(list(replies)))
    try:
        yield daemon, home, sock
    finally:
        daemon.close()


def inspector_home(tmp_path):
    home = tmp_path / 'inspector'
    home.mkdir(exist_ok=True)
    config = Config(home=home, cwd=home)
    config.inspector = InspectorConfig(operator_approval_env=APPROVAL)
    return config


def seeded(daemon):
    manager = daemon._manager()
    slug = manager.create('Move-in thread')['slug']
    manager.submit(slug, 'hello resident')
    manager.wait(slug)
    return slug


def test_socket_is_owner_only_and_cli_reads_status_sessions_log_view_zoom_config(tmp_path, short):
    with resident(tmp_path, short) as (daemon, home, sock):
        assert stat.S_IMODE(sock.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(sock.stat().st_mode) == 0o600
        slug = seeded(daemon)
        env = {k: v for k, v in os.environ.items() if not k.startswith('TESSERA_')}
        def cli(*args):
            result = subprocess.run([sys.executable, '-m', 'tessera', '--home', str(tmp_path / 'inspector'), 'inspect',
                                     '--attach', str(sock), *args], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
            assert result.returncode == 0, result.stderr
            return json.loads(result.stdout)
        status = cli('status')
        assert status['pid'] == os.getpid() and status['lockHolderPid'] == os.getpid() and status['inFlight'] == []
        assert status['inspector'] == {'enabled': True, 'bound': True, 'error': None}
        assert status['companions']['atlas']['enabled'] is False
        assert cli('sessions')['data'] == [{'slug': slug, 'title': 'Move-in thread', 'archived': False}]
        log = cli('log', slug)['messages']
        assert [m['kind'] for m in log][:2] == ['user', 'agent'] and log[0]['text'] == 'hello resident'
        assert cli('view', slug)['lines']
        assert 'hello resident' in cli('zoom', slug, '--start', '0')['text']
        config = cli('config')
        assert config['companions']['atlas']['endpoints'] == {'empty': True, 'count': 0}
        assert 'http' not in json.dumps(config) and APPROVAL not in json.dumps(config)


def test_inspector_never_takes_resident_locks_or_blocks_session_manager(tmp_path, short):
    with resident(tmp_path, short) as (daemon, home, sock):
        # An attached inspector process holds its connection open before the resident's manager starts.
        attach = subprocess.Popen([sys.executable, '-c', (
            'import sys, time; sys.path.insert(0, ".");'
            'from tessera.inspector_client import InspectorClient;'
            'c = InspectorClient(sys.argv[1]); c.call("inspect/sessions"); print("ready", flush=True); time.sleep(30)'), str(sock)],
            cwd=ROOT, stdout=subprocess.PIPE, text=True)
        try:
            assert attach.stdout.readline().strip() == 'ready'
            manager = daemon._manager()  # Must not raise "locked by another process".
            slug = seeded(daemon)
            with InspectorClient(sock) as client:
                client.call('inspect/log', {'threadId': slug})
                client.call('inspect/events', {'threadId': slug})
            with pytest.raises(RuntimeError, match='locked by another process'):
                SessionManager(daemon.config)  # The resident still holds its lock.
            assert manager._owner is not None
        finally:
            attach.kill()
            attach.wait()


def test_approval_decisions_are_not_inspector_methods_and_observers_cannot_write(tmp_path, short):
    with resident(tmp_path, short) as (daemon, home, sock):
        daemon.pending_approvals['fake'] = {'id': 'fake', 'threadId': 'move-in-thread', 'turnId': 't1', 'name': 'shell'}
        with InspectorClient(sock) as client:
            assert client.call('inspect/approvals')['data'] == [{'id': 'fake', 'threadId': 'move-in-thread', 'turnId': 't1', 'name': 'shell'}]
            for method in ('inspect/approve', 'inspect/approval', 'item/tool/requestApproval', 'turn/start', 'shutdown'):
                with pytest.raises(InspectorError) as error:
                    client.call(method, {'id': 'fake', 'decision': 'accept'})
                assert error.value.code == -32601
            with pytest.raises(InspectorError) as error:
                client.call('inspect/note', {'threadId': 'x', 'text': 'y'})
            assert error.value.code == -32008
        assert 'fake' in daemon.pending_approvals


def test_editor_requires_operator_env_and_a_separate_home(tmp_path, short, monkeypatch):
    with resident(tmp_path, short) as (daemon, home, sock):
        with pytest.raises(InspectorError):
            InspectorClient(sock, role='editor', approval='guess', home=tmp_path / 'inspector')
        monkeypatch.setenv(APPROVAL, 'operator-fixture')
        with pytest.raises(InspectorError):
            InspectorClient(sock, role='editor', approval='wrong', home=tmp_path / 'inspector')
        with pytest.raises(InspectorError):
            InspectorClient(sock, role='editor', approval='operator-fixture', home=home)
        with InspectorClient(sock, role='editor', approval='operator-fixture', home=tmp_path / 'inspector') as client:
            assert client.info['role'] == 'editor'


def test_note_is_appended_by_the_daemon_visible_on_resume_and_revertible_by_retraction(tmp_path, short, monkeypatch):
    monkeypatch.setenv(APPROVAL, 'operator-fixture')
    with resident(tmp_path, short) as (daemon, home, sock):
        slug = seeded(daemon)
        with InspectorClient(sock, role='editor', approval='operator-fixture', home=tmp_path / 'inspector') as client:
            reply = client.call('inspect/note', {'threadId': slug, 'text': 'Check the compactor budget.'})
            assert reply['appended']
            client.call('inspect/revert', {'journalId': reply['journalId']})
            with pytest.raises(InspectorError):
                client.call('inspect/note', {'threadId': slug, 'text': 'x' * 100000})
        journal = (home / 'run' / 'inspector-journal.jsonl').read_text().splitlines()
        assert json.loads(journal[0])['inverse'] == {'method': 'retract-note', 'threadId': slug}
        system_prompt = daemon.manager._runtimes[slug].session.system_prompt
        assert 'compactor budget' not in system_prompt
    manager = SessionManager(Config(home=home, cwd=home), backend_factory=lambda *a: FakeBackend([]))
    try:
        session = manager.get(slug)
        notes = [m.text for m in session.log.iter() if m.kind == 'note' and m.meta.get('source') == 'inspector']
        assert notes[0] == 'Inspector note: Check the compactor budget.' and notes[1].startswith('Inspector note: Retracts inspector note ' + reply['journalId'])
        assert len(session.view.lines()) >= 1 and session.view.zoom(notes_index(session), 1).endswith('Check the compactor budget.')
    finally:
        manager.close()


def notes_index(session):
    return [m.i for m in session.log.iter() if m.kind == 'note' and m.meta.get('source') == 'inspector'][0]


def test_note_on_an_unloaded_session_keeps_view_consistent(tmp_path, short, monkeypatch):
    monkeypatch.setenv(APPROVAL, 'operator-fixture')
    with resident(tmp_path, short) as (daemon, home, sock):
        slug = daemon._manager().create('Cold thread')['slug']
        with InspectorClient(sock, role='editor', approval='operator-fixture', home=tmp_path / 'inspector') as client:
            client.call('inspect/note', {'threadId': slug, 'text': 'cold note'})
            assert client.call('inspect/log', {'threadId': slug})['messages'][0]['text'] == 'Inspector note: cold note'
    manager = SessionManager(Config(home=home, cwd=home), backend_factory=lambda *a: FakeBackend([]))
    try:
        assert manager.get(slug).log.count() == 1
    finally:
        manager.close()


def test_tighten_only_moves_ask_to_deny_risky_and_loosening_needs_the_operator(tmp_path, short, monkeypatch):
    monkeypatch.setenv(APPROVAL, 'operator-fixture')
    with resident(tmp_path, short) as (daemon, home, sock):
        slug = seeded(daemon)
        session = daemon.manager._runtimes[slug].session
        with InspectorClient(sock, role='editor', approval='operator-fixture', home=tmp_path / 'inspector') as client:
            with pytest.raises(InspectorError):
                client.call('inspect/tighten', {'approval': 'ask'})
            reply = client.call('inspect/tighten', {'approval': 'deny_risky'})
            assert session.tools.approval == 'deny_risky' and daemon.config.tools.approval == 'deny_risky'
            with pytest.raises(InspectorError) as error:
                client.call('inspect/tighten', {'approval': 'ask'})  # deny_risky -> ask is refused
            assert error.value.code == -32602
            with pytest.raises(InspectorError) as error:
                client.call('inspect/revert', {'journalId': reply['journalId']})
            assert error.value.code == -32008
            client.call('inspect/revert', {'journalId': reply['journalId'], 'operatorApproval': 'operator-fixture'})
            assert session.tools.approval == 'ask'
            paused = client.call('inspect/companion-pause')
            assert (home / 'run' / 'companions-paused.json').exists()
            client.call('inspect/revert', {'journalId': paused['journalId']})
            assert not (home / 'run' / 'companions-paused.json').exists()


def test_code_reads_stay_inside_source_root(tmp_path, short):
    source = tmp_path / 'checkout'
    (source / 'pkg').mkdir(parents=True)
    (source / 'pkg' / 'mod.py').write_text('VALUE = 1\n')
    (tmp_path / 'outside.txt').write_text('secret')
    (source / 'escape').symlink_to(tmp_path / 'outside.txt')
    with resident(tmp_path, short, source_root=source) as (daemon, home, sock):
        with InspectorClient(sock) as client:
            assert client.call('inspect/code', {'path': 'pkg/mod.py'})['text'] == 'VALUE = 1\n'
            for bad in ('/etc/hosts', '../outside.txt', 'escape', 'pkg/../../outside.txt'):
                with pytest.raises(InspectorError):
                    client.call('inspect/code', {'path': bad})


def test_queue_saturation_returns_32006_without_touching_turns(tmp_path, short):
    with resident(tmp_path, short) as (daemon, home, sock):
        server = daemon.inspector
        held = [server.slots.acquire(blocking=False) for _ in range(server.settings.max_pending_requests)]
        assert all(held)
        try:
            with InspectorClient(sock) as client:
                with pytest.raises(InspectorError) as error:
                    client.call('inspect/status')
                assert error.value.code == -32006
        finally:
            for _ in held:
                server.slots.release()
        slug = seeded(daemon)
        assert daemon.manager.turn(slug, daemon.manager._runtimes[slug].turns and list(daemon.manager._runtimes[slug].turns)[-1])['state'] == 'done'


def test_unbindable_or_invalid_inspector_config_never_refuses_daemon_start(tmp_path, short, capsys):
    blocker = short / 'file'
    blocker.write_text('not a directory')
    home = tmp_path / 'r'
    home.mkdir()
    daemon = Daemon(resident_config(home, blocker / 'inspector.sock'))
    try:
        assert daemon.inspector.status() == {'enabled': True, 'bound': False, 'error': 'FileExistsError'} or \
            daemon.inspector.status()['bound'] is False
        assert 'Inspector socket unavailable' in capsys.readouterr().err
        assert daemon.dispatch('status', {})['inspector']['bound'] is False
    finally:
        daemon.close()
    (home / 'config.toml').write_text('[inspector]\nenabled = true\nmax_frame_bytes = "huge"\n')
    config = load_config(home)
    assert config.inspector.enabled is False and config.inspector_error == 'ValueError'


def test_socket_down_status_reads_files_without_locks_or_app_server(tmp_path, short):
    home = tmp_path / 'resident'
    manager = SessionManager(Config(home=home, cwd=home))
    try:
        manager.create('Offline thread')
        env = {k: v for k, v in os.environ.items() if not k.startswith('TESSERA_')}
        result = subprocess.run([sys.executable, '-m', 'tessera', '--home', str(tmp_path / 'inspector'), 'inspect',
                                 '--attach', str(short / 'missing.sock'), '--resident-home', str(home), 'status'],
                                cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, result.stderr
        report = json.loads(result.stdout)
        assert report['attached'] is False and report['sessions'][0]['title'] == 'Offline thread'
    finally:
        manager.close()  # Still ours: the inspector never contended for it.
    result = subprocess.run([sys.executable, '-m', 'tessera', '--home', str(home), 'inspect', '--attach',
                             str(home / 'run' / 'inspector.sock'), 'note', 'offline-thread', 'x'],
                            cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 1 and 'must differ from the resident home' in result.stderr


def checkout(tmp_path):
    source = tmp_path / 'checkout'
    (source / 'pkg').mkdir(parents=True)
    (source / 'pkg' / 'mod.py').write_text('VALUE = 1\nNAME = "x"\n')
    return source


def test_propose_writes_only_the_inspector_home(tmp_path, short):
    source = checkout(tmp_path)
    before = (source / 'pkg' / 'mod.py').read_bytes()
    config = inspector_home(tmp_path)
    with resident(tmp_path, short, source_root=source) as (daemon, home, sock):
        with InspectorClient(sock, home=config.home) as client:
            result = propose(config, client, [{'path': 'pkg/mod.py', 'old': 'VALUE = 1', 'new': 'VALUE = 2'}], branch='inspect/value')
            for bad in ([{'path': '../x.py', 'old': 'a', 'new': 'b'}], [{'path': 'pkg/mod.py', 'old': 'missing', 'new': 'b'}],
                        [{'path': 'pkg/.lock', 'old': 'a', 'new': 'b'}]):
                with pytest.raises((ValueError, InspectorError)):
                    propose(config, client, bad)
    record = json.loads(Path(result['path']).read_text())
    assert Path(result['path']).parent == config.home / 'proposals'
    assert record['sha256']['pkg/mod.py'] and '-VALUE = 1\n+VALUE = 2' in record['diff'] and record['branch'] == 'inspect/value'
    assert (source / 'pkg' / 'mod.py').read_bytes() == before


def test_apply_refuses_live_package_during_a_turn_and_snapshots_first_on_a_checkout(tmp_path, short, monkeypatch):
    source = checkout(tmp_path)
    config = inspector_home(tmp_path)
    monkeypatch.setenv(APPROVAL, 'operator-fixture')
    with resident(tmp_path, short, source_root=source) as (daemon, home, sock):
        with InspectorClient(sock, home=config.home) as client:
            good = propose(config, client, [{'path': 'pkg/mod.py', 'old': 'VALUE = 1', 'new': 'VALUE = 2'}])['id']
            stale = propose(config, client, [{'path': 'pkg/mod.py', 'old': 'NAME = "x"', 'new': 'NAME = "y"'}])['id']
        with InspectorClient(sock) as observer:
            with pytest.raises(PermissionError):
                apply(config, observer, good)
        class LiveClient:
            info = {'role': 'editor'}
            def call(self, method, params=None):
                return {'inFlight': [{'threadId': 't', 'turnId': 'x'}], 'livePackage': str(source / 'pkg')}
        with pytest.raises(PermissionError):
            apply(config, LiveClient(), good)
        assert (source / 'pkg' / 'mod.py').read_text() == 'VALUE = 1\nNAME = "x"\n'
        class BusyCheckout(LiveClient):
            def call(self, method, params=None):
                return {'inFlight': [{'threadId': 't', 'turnId': 'x'}], 'livePackage': str(ROOT / 'tessera')}
        result = apply(config, BusyCheckout(), good)  # Separate checkout: write allowed, no restart.
        assert result['restarted'] is False
        snapshot = json.loads(Path(result['snapshot']).read_text())
        assert snapshot['files']['pkg/mod.py'] == 'VALUE = 1\nNAME = "x"\n'
        assert (source / 'pkg' / 'mod.py').read_text() == 'VALUE = 2\nNAME = "x"\n'
        # The second proposal's recorded hash does not match the file, so nothing changes.
        current = (source / 'pkg' / 'mod.py').read_bytes()
        with InspectorClient(sock, role='editor', approval='operator-fixture', home=config.home) as editor:
            with pytest.raises(ValueError):
                apply(config, editor, stale)
        assert (source / 'pkg' / 'mod.py').read_bytes() == current
        assert not (config.home / 'proposals' / (stale + '.snapshot.json')).exists()


def test_failed_anchor_leaves_the_tree_unchanged(tmp_path, short, monkeypatch):
    source = checkout(tmp_path)
    config = inspector_home(tmp_path)
    monkeypatch.setenv(APPROVAL, 'operator-fixture')
    with resident(tmp_path, short, source_root=source) as (daemon, home, sock):
        with InspectorClient(sock, home=config.home) as client:
            pid = propose(config, client, [{'path': 'pkg/mod.py', 'old': 'VALUE = 1', 'new': 'VALUE = 2'}])['id']
        path = config.home / 'proposals' / (pid + '.json')
        record = json.loads(path.read_text())
        record['edits'][0]['old'] = 'NOT PRESENT'
        path.write_text(json.dumps(record))
        with InspectorClient(sock, role='editor', approval='operator-fixture', home=config.home) as editor:
            with pytest.raises(ValueError):
                apply(config, editor, pid)
    assert (source / 'pkg' / 'mod.py').read_text() == 'VALUE = 1\nNAME = "x"\n'


def test_rollback_restores_the_snapshot_and_refuses_a_tampered_one(tmp_path, short, monkeypatch):
    source = checkout(tmp_path)
    config = inspector_home(tmp_path)
    monkeypatch.setenv(APPROVAL, 'operator-fixture')
    with resident(tmp_path, short, source_root=source) as (daemon, home, sock):
        with InspectorClient(sock, home=config.home) as client:
            pid = propose(config, client, [{'path': 'pkg/mod.py', 'old': 'VALUE = 1', 'new': 'VALUE = 2'}])['id']
        with InspectorClient(sock, role='editor', approval='operator-fixture', home=config.home) as editor:
            snapshot = Path(apply(config, editor, pid)['snapshot'])
    original = snapshot.read_text()
    snapshot.write_text(json.dumps({'files': {'pkg/mod.py': 'VALUE = 666\n'}}))
    with pytest.raises(ValueError):
        rollback(config, pid)
    assert (source / 'pkg' / 'mod.py').read_text() == 'VALUE = 2\nNAME = "x"\n'
    snapshot.write_text(original)
    assert rollback(config, pid)['restarted'] is False
    assert (source / 'pkg' / 'mod.py').read_text() == 'VALUE = 1\nNAME = "x"\n'


def test_inspect_mcp_serves_reads_on_stdio_and_binds_nothing(tmp_path, short):
    with resident(tmp_path, short) as (daemon, home, sock):
        seeded(daemon)
        env = {k: v for k, v in os.environ.items() if not k.startswith('TESSERA_')}
        frames = [{'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {}},
                  {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
                  {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'inspect_sessions', 'arguments': {}}},
                  {'jsonrpc': '2.0', 'id': 4, 'method': 'tools/call', 'params': {'name': 'inspect_note', 'arguments': {}}}]
        result = subprocess.run([sys.executable, '-m', 'tessera', '--home', str(tmp_path / 'inspector'), 'inspect-mcp', '--attach', str(sock)],
                                input=''.join(json.dumps(f) + '\n' for f in frames), cwd=ROOT, env=env,
                                capture_output=True, text=True, timeout=60)
        replies = [json.loads(line) for line in result.stdout.splitlines()]
        assert {t['name'] for t in replies[1]['result']['tools']} >= {'inspect_status', 'inspect_log', 'inspect_view'}
        assert 'Move-in thread' in replies[2]['result']['content'][0]['text']
        assert replies[3]['error']['code'] == -32601


def test_peer_credentials_report_this_uid(short):
    from tessera.inspector import peer_identity
    left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    with left, right:
        assert peer_identity(left)[0] == os.getuid()
