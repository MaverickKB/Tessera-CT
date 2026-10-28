"""AetherMind is required. Nothing in a shipped file can turn it off.

A stored `enabled` key is retired: load accepts it, Settings shows it, and a
turn still uses the embedded engine. A session, an infinity wake, a delegated
agent, and a swarm job all refuse when no engine is up and no external
service is configured. YOLO is not an exception.
"""

import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import threading
import time

import pytest

from fake_backend import FakeBackend
from tessera.bundled_engine import AetherMindUnavailable
from tessera.capture import Capture
from tessera.config import AccountConfig, BackendConfig, Config, load_config
from tessera.jobs import SwarmJobs
from tessera.log import ChatLog
from tessera.loop import Session
from tessera.sessions import SessionManager


ROOT = Path(__file__).resolve().parents[1]
SHIPPED = (
    ROOT / 'README.md',
    ROOT / 'config.example.toml',
    ROOT / 'demo',
    ROOT / 'desktop',
    ROOT / 'docs',
    ROOT / 'evals',
    ROOT / 'scripts',
    ROOT / 'seeds',
    ROOT / 'tessera',
    ROOT / 'web',
)
TEXT_SUFFIXES = {
    '.css', '.example', '.html', '.js', '.json', '.md', '.mjs', '.py', '.rs',
    '.sh', '.toml', '.txt', '.yaml', '.yml',
}
ASSIGNMENT = re.compile(
    r'(?:TESSERA_AETHERMIND_ENABLED\s*[=:]|\.aethermind\.enabled\s*=)'
)
# Object-literal memory off switch. A later section's enabled = false is not memory.
MEMORY_OFF = re.compile(
    r'aethermind\s*[:=]\s*\{[^}]{0,240}?enabled\s*[:=]\s*false',
    re.IGNORECASE,
)
SECTION_KEY = re.compile(r"""^\[([^\]]+)\]\s*(?:#.*)?$""")
ENABLED_KEY = re.compile(r"""^["']?enabled["']?\s*=""")


def _shipped_files():
    found = []
    for path in SHIPPED:
        if path.is_file():
            found.append(path)
            continue
        for child in path.rglob('*'):
            if not child.is_file():
                continue
            if 'aethermind-engine' in child.parts or '__pycache__' in child.parts:
                continue
            if child.suffix.lower() not in TEXT_SUFFIXES and child.name != 'README.md':
                continue
            found.append(child)
    return found


def _config_text(path):
    """File text, with source escapes expanded so a written config is visible."""
    try:
        text = path.read_text(encoding='utf-8')
    except (OSError, UnicodeDecodeError):
        return None
    return text.replace('\\n', '\n')


def _flag_assignments(path, *, dotted=True):
    """Lines that set the retired switch, not lines that merely name it."""
    text = _config_text(path)
    if text is None:
        return []
    hits = []
    section = ''
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith('#') or stripped.startswith('//'):
            continue
        header = SECTION_KEY.match(stripped)
        if header:
            section = header.group(1).strip().strip('"').strip("'")
            continue
        stored = section == 'aethermind' and ENABLED_KEY.match(stripped)
        if stored or (dotted and ASSIGNMENT.search(line)):
            hits.append(f'{path.relative_to(ROOT)}:{number}: {stripped}')
    return hits


def test_no_shipped_file_sets_the_aethermind_enabled_flag():
    """A shipped config, demo, doc, example, or script must not set the flag."""
    hits = []
    for path in _shipped_files():
        hits.extend(_flag_assignments(path))
    assert hits == []


def test_no_checked_in_fixture_sets_the_aethermind_enabled_flag():
    """Test fixtures do not store enabled = false under [aethermind].

    A fixture that must not start an engine points bundled_path at an empty
    receipt-less directory. A retired key is covered through the environment
    overlay, which load_config records and does not apply.
    """
    hits = []
    for child in (ROOT / 'tests').rglob('*'):
        if not child.is_file() or '__pycache__' in child.parts:
            continue
        if child.suffix.lower() not in TEXT_SUFFIXES:
            continue
        hits.extend(_flag_assignments(child, dotted=False))
    assert hits == []


def test_no_tracked_file_stores_a_memory_off_switch():
    """No tracked file stores enabled=false for AetherMind.

    Other sections may be off. Memory may not. A checkout that still has the
    switch in a config, demo, doc, script, or fixture fails here.
    """
    from git_checkout import require_git_checkout

    require_git_checkout()
    listed = subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT)
    hits = []
    for name in listed.decode().split('\0'):
        if not name or 'aethermind-engine' in name.split('/'):
            continue
        path = ROOT / name
        if not path.is_file():
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES and path.name != 'README.md':
            continue
        hits.extend(_flag_assignments(path))
        text = _config_text(path)
        if text is None:
            continue
        for match in MEMORY_OFF.finditer(text):
            line = text.count('\n', 0, match.start()) + 1
            hits.append(f'{name}:{line}: {match.group(0)[:160]}')
        if name.endswith('web/tests/fixtures/memory-status.json') and re.search(r'"enabled"\s*:\s*false', text):
            hits.append(name + ': memory status fixture stores enabled false')
    assert hits == []


def _refused(message):
    assert 'AetherMind is not available' in message
    assert 'Fix:' in message
    assert 'build-receipt.json' in message
    assert 'aethermind.bundled_path' in message
    assert 'external AetherMind' in message


class _EngineProc:
    def __init__(self):
        self.exited = None

    def poll(self):
        return self.exited

    def terminate(self):
        self.exited = 0


_CAUSES = {
    'no_receipt': 'bytes present, not started',
    'missing_bundle': 'Packaged engine is not in this install',
    'dead_engine': 'Embedded engine stopped',
}


@pytest.mark.aethermind_unavailable
@pytest.mark.parametrize('approval', ['ask', 'yolo'])
@pytest.mark.parametrize('mode', ['no_receipt', 'missing_bundle', 'dead_engine'])
def test_chat_infinity_and_child_refuse_without_a_working_engine(tmp_path, monkeypatch, approval, mode):
    """No receipt, a missing bundle, and a dead engine each refuse the turn.

    Safe (`ask`) and YOLO are the same gate. Chat, the infinity floor, a
    delegated child, and a daemon child all go through it.
    """
    from tessera import bundled_engine
    from tessera.daemon_agents import DaemonAgents
    from local_memory import QuietMemory

    config = Config(home=tmp_path, cwd=tmp_path)
    config.tools.approval = approval
    config.autonomy.enabled = True
    config.autonomy.tick_seconds = 3600
    config.jev.enabled = False
    proc = _EngineProc()
    dead_url = 'http://127.0.0.1:59998/mcp'
    if mode == 'missing_bundle':
        monkeypatch.setattr(bundled_engine, '_engine_root', lambda checked: None)
        monkeypatch.setattr('tessera.engine_manifest.vendored_status', lambda root=None: 'absent')
    elif mode == 'dead_engine':
        calls = []

        def launch(checked, clock=None, *, force=False):
            calls.append(force)
            return dead_url if len(calls) == 1 else None

        monkeypatch.setattr(bundled_engine, 'ensure_bundled_engine', launch)
        monkeypatch.setattr('tessera.loop.AetherMindClient', lambda *args, **kwargs: QuietMemory())
        bundled_engine._REGISTRY['contract-dead-engine'] = {'url': dead_url, 'proc': proc}
    backend = FakeBackend(['should not run'])
    try:
        with SessionManager(config, backend_factory=lambda slug, name: backend) as manager:
            manager.autonomy.close()
            floor = config.autonomy.infinity_slug
            assert any(row['slug'] == floor for row in manager.list_sessions())
            floor_runtime = manager.runtime(floor)
            if mode == 'dead_engine':
                assert floor_runtime.session.capture.client is not None
                proc.exited = 0
            chat = manager.create('Notes')['slug']
            floor_turn = manager.submit(floor, 'Coordinate the unfinished work')
            chat_turn = manager.submit(chat, 'Answer this')
            assert manager.wait(floor, timeout=10)
            assert manager.wait(chat, timeout=10)
            for slug, turn_id in ((floor, floor_turn), (chat, chat_turn)):
                row = manager.turn(slug, turn_id)
                assert row['state'] == 'error', row
                _refused(row['error'])
                if slug == floor:
                    assert _CAUSES[mode] in row['error']
                    if mode == 'missing_bundle':
                        assert 'bytes present, not started' not in row['error']
            wake = manager.runtime(floor).enqueue(
                '[wake] unclaimed cards are still open',
                wake={'events': ['pressure'], 'held_seconds': 0})
            assert manager.wait(floor, timeout=10)
            wake_row = manager.turn(floor, wake)
            assert wake_row['state'] == 'error', wake_row
            _refused(wake_row['error'])
            assert backend.calls == []

        parent = FakeBackend(['parent should not run'])
        child = FakeBackend(['child should not run'])
        with Session(config, backend=parent, agent_backend_factory=lambda: child) as session:
            session.agents.spawn('helper', 'Do the delegated work', ['read_file'])
            assert session.agents.wait(timeout=5)
            reports = [message for message in session.log.iter() if message.kind == 'work']
            assert len(reports) == 1
            _refused(reports[0].text)
            assert reports[0].text.startswith('[helper] ')
            assert reports[0].meta['success'] is False
            assert parent.calls == [] and child.calls == []

        class Host:
            def __init__(self, held):
                self.config = held
                self.manager = None

        root = tmp_path / 'daemon-child'
        root.mkdir()
        daemon_child = DaemonAgents(Host(config)).session(
            config, backend=FakeBackend(['daemon child should not run']), root=root)
        try:
            with pytest.raises(AetherMindUnavailable) as refused:
                daemon_child.run_turn('delegated child')
        finally:
            daemon_child.close()
        _refused(str(refused.value))
    finally:
        bundled_engine._REGISTRY.pop('contract-dead-engine', None)


@pytest.mark.aethermind_unavailable
def test_swarm_job_refuses_before_any_worker_when_aethermind_is_unavailable(tmp_path):
    config = Config(home=tmp_path, cwd=tmp_path)
    config.swarm.enabled = True
    config.board.enabled = True
    config.tools.approval = 'yolo'
    for name in ('alpha', 'beta'):
        config.accounts[name] = AccountConfig(name=name)
        config.backends[name] = BackendConfig(
            name=name, account=name, base_url='https://models.example.com/' + name,
            model=name + '-model-placeholder')
    calls = []

    def factory(settings):
        calls.append(settings.name)
        return FakeBackend(['should not run'])

    jobs = SwarmJobs(config, backend_factory=factory)
    workers = [
        {'backend': 'alpha', 'objective': 'Inspect alpha', 'scope': ['alpha'], 'budget': {'tokens': 20}},
        {'backend': 'beta', 'objective': 'Inspect beta', 'scope': ['beta'], 'budget': {'tokens': 20}},
    ]
    with ChatLog(tmp_path / 'log') as log:
        job = jobs.create('Inspect the fixture', workers, session_slug='subject', log=log)
        with pytest.raises(AetherMindUnavailable, match='AetherMind is not available') as refused:
            jobs.run(job['id'], log=log)
    _refused(str(refused.value))
    assert calls == []


def test_stored_enabled_false_still_runs_the_embedded_engine(tmp_path, monkeypatch):
    from local_memory import QuietMemory

    launches = []

    def launch(config, clock=None, *, force=False):
        launches.append(force)
        return 'http://127.0.0.1:59999/mcp'

    monkeypatch.setattr('tessera.bundled_engine.ensure_bundled_engine', launch)
    monkeypatch.setattr('tessera.loop.AetherMindClient', lambda *args, **kwargs: QuietMemory(*args, **kwargs))
    monkeypatch.setenv('TESSERA_AETHERMIND_ENABLED', 'false')
    (tmp_path / 'config.toml').write_text('[tools]\napproval = "yolo"\n')
    config = load_config(tmp_path)
    assert config.retired_settings['aethermind.enabled'] is False
    assert not hasattr(config.aethermind, 'enabled')
    assert config.tools.approval == 'yolo'
    backend = FakeBackend(['embedded-ran'])
    with Session(config, backend=backend) as session:
        assert launches == [True]
        assert session.memory_choice['source'] == 'embedded'
        assert session.memory_choice['url'] == 'http://127.0.0.1:59999/mcp'
        assert isinstance(session.capture.client, QuietMemory)
        assert session.run_turn('hello from a stored false flag') == 'embedded-ran'
    assert backend.calls


def test_cli_run_refuses_when_no_aethermind_is_configured(tmp_path):
    # A bare checkout cold-starts the engine now, so a config that names
    # an absent bundled_path is the case that still refuses: the explicit
    # path is not the packaged copy and it has no receipt.
    (tmp_path / 'config.toml').write_text(
        '[backends.test]\nbase_url="http://127.0.0.1:9/v1"\nmodel="test-model"\n'
        '[roles]\nmain="test"\n[tools]\napproval = "yolo"\n'
        '[aethermind]\nbundled_path = "no-such-bundle"\n')
    env = {key: value for key, value in os.environ.items() if key != 'TESSERA_AETHERMIND_ENABLED'}
    env['TESSERA_HOME'] = str(tmp_path)
    env['PYTHONPATH'] = str(ROOT) + os.pathsep + env.get('PYTHONPATH', '')
    result = subprocess.run(
        [sys.executable, '-m', 'tessera', 'run', 'Hello'], cwd=str(tmp_path), env=env,
        capture_output=True, text=True, timeout=20)
    assert result.returncode == 1, result.stderr
    assert result.stderr.startswith('tessera: AetherMind is not available')
    assert 'Packaged engine is not in this install' in result.stderr
    _refused(result.stderr)


@pytest.mark.parametrize('approval', ['ask', 'yolo'])
def test_an_injected_capture_without_a_client_refuses_the_turn(tmp_path, approval):
    config = Config(home=tmp_path, cwd=tmp_path)
    config.tools.approval = approval
    backend = FakeBackend(['should not run'])
    capture = Capture(None, tmp_path / 'outbox', 'work')
    with Session(config, backend=backend, capture=capture) as session:
        assert session.memory_choice is None
        assert session.capture.client is None
        with pytest.raises(AetherMindUnavailable) as refused:
            session.run_turn('hello')
    assert 'this session has no AetherMind client' in str(refused.value)
    _refused(str(refused.value))
    assert backend.calls == []


@pytest.mark.aethermind_unavailable
def test_mirror_names_the_failure_when_no_engine_is_up(tmp_path):
    from tessera.mirror import AppendMirror

    config = Config(home=tmp_path, cwd=tmp_path)
    mirror = AppendMirror(config)
    mirror.record('subject', 'session', 'chat', {'i': 0, 'text': 'offline'})
    status = mirror.status()
    assert status['local_failures'] == 1
    _refused(status['notice'])
    assert 'bytes present, not started' in status['notice']
    assert list(tmp_path.iterdir()) == []


def test_a_cached_choice_is_rechecked_and_not_keyed_by_identity(tmp_path, monkeypatch):
    from tessera import bundled_engine
    from tessera.mcp_client import McpError

    probes = []

    def probe(checked):
        probes.append(checked.aethermind.url)
        if len(probes) > 1:
            raise McpError('MCP HTTP 401')

    launches = []

    def launch(checked, clock=None, *, force=False):
        launches.append(force)
        return 'http://127.0.0.1:59997/mcp'

    monkeypatch.setattr(bundled_engine, 'probe_external', probe)
    monkeypatch.setattr(bundled_engine, 'ensure_bundled_engine', launch)
    config = Config(home=tmp_path, cwd=tmp_path)
    config.aethermind.url = 'https://memory.example.invalid/mcp'
    other = Config(home=tmp_path, cwd=tmp_path)
    other.aethermind.url = config.aethermind.url
    assert id(config) != id(other)
    assert bundled_engine._choice_key(config) == bundled_engine._choice_key(other)
    first = bundled_engine.choose_memory(config)
    assert first['source'] == 'external'
    assert launches == []
    second = bundled_engine.choose_memory(other)
    assert second['source'] == 'embedded'
    assert second['url'] == 'http://127.0.0.1:59997/mcp'
    assert len(probes) >= 2
    assert launches == [True]

    proc = _EngineProc()
    dead = 'http://127.0.0.1:59996/mcp'
    bundled_engine._REGISTRY['contract-cache'] = {'url': dead, 'proc': proc}
    calls = []

    def embedded(checked, clock=None, *, force=False):
        calls.append(1)
        return dead if len(calls) == 1 else None

    monkeypatch.setattr(bundled_engine, 'ensure_bundled_engine', embedded)
    plain = Config(home=tmp_path / 'plain', cwd=tmp_path)
    (tmp_path / 'plain').mkdir()
    try:
        built = bundled_engine.choose_memory(plain)
        assert built['url'] == dead
        proc.exited = 0
        again = bundled_engine.choose_memory(plain)
        assert again['url'] == ''
        assert 'Embedded engine stopped' in again['reason']
        assert calls[0] == 1 and len(calls) >= 2
    finally:
        bundled_engine._REGISTRY.pop('contract-cache', None)


def test_external_is_used_again_after_it_recovers(tmp_path, monkeypatch):
    from tessera import bundled_engine
    from tessera.mcp_client import McpError

    up = {'value': True}
    probes = []

    def probe(checked):
        probes.append(checked.aethermind.url)
        if not up['value']:
            raise McpError('external down')

    def launch(checked, clock=None, *, force=False):
        launches.append(force)
        return 'http://127.0.0.1:59993/mcp'

    launches = []
    monkeypatch.setattr(bundled_engine, 'probe_external', probe)
    monkeypatch.setattr(bundled_engine, 'ensure_bundled_engine', launch)
    config = Config(home=tmp_path, cwd=tmp_path)
    config.aethermind.url = 'https://memory.example.invalid/mcp'
    first = bundled_engine.choose_memory(config)
    assert first['source'] == 'external'
    assert first['external_error'] is None
    assert launches == []
    up['value'] = False
    second = bundled_engine.choose_memory(config)
    assert second['source'] == 'embedded'
    assert second['url'] == 'http://127.0.0.1:59993/mcp'
    assert second['external_error'] == 'McpError: external down'
    assert launches == [True]
    up['value'] = True
    third = bundled_engine.choose_memory(config)
    assert len(probes) == 3
    assert launches == [True]
    assert third['source'] == 'external'
    assert third['external_error'] is None
    assert third['reason'] == 'External service validated'
    assert 'external down' not in third['reason']


def test_an_engine_poll_failure_is_named(tmp_path, monkeypatch):
    from tessera import bundled_engine
    from tessera.settings_model import _memory_truth

    class Broken:
        def poll(self):
            raise OSError('engine pid could not be read')

    url = 'http://127.0.0.1:59995/mcp'
    calls = []

    def launch(checked, clock=None, *, force=False):
        calls.append(force)
        return url if len(calls) == 1 else None

    monkeypatch.setattr(bundled_engine, 'ensure_bundled_engine', launch)
    bundled_engine._REGISTRY['contract-poll'] = {'url': url, 'proc': Broken()}
    config = Config(home=tmp_path, cwd=tmp_path)
    try:
        choice = bundled_engine.choose_memory(config)
    finally:
        bundled_engine._REGISTRY.pop('contract-poll', None)
    assert choice['url'] == ''
    poll_failure = 'Embedded engine poll failed: OSError: engine pid could not be read'
    assert poll_failure in choice['reason']
    message = bundled_engine.unavailable_message(choice)
    assert poll_failure in message
    assert poll_failure in _memory_truth({'reason': choice['reason']})
    assert calls[0] is True and len(calls) >= 2


def test_a_cached_external_probe_failure_names_its_cause(tmp_path, monkeypatch):
    from tessera import bundled_engine
    from tessera.settings_model import _memory_truth

    probes = []

    def probe(checked):
        probes.append(checked.aethermind.url)
        if len(probes) > 1:
            raise OSError('cached probe refused the socket')

    def launch(checked, clock=None, *, force=False):
        return None

    monkeypatch.setattr(bundled_engine, 'probe_external', probe)
    monkeypatch.setattr(bundled_engine, 'ensure_bundled_engine', launch)
    config = Config(home=tmp_path, cwd=tmp_path)
    config.aethermind.url = 'https://memory.example.invalid/mcp'
    first = bundled_engine.choose_memory(config)
    assert first['source'] == 'external'
    second = bundled_engine.choose_memory(config)
    assert len(probes) == 2
    assert second['source'] == 'embedded'
    assert second['url'] == ''
    assert second['external_error'] == 'OSError: cached probe refused the socket'
    assert 'cached probe refused the socket' in second['reason']
    message = bundled_engine.unavailable_message(second)
    assert 'cached probe refused the socket' in message
    assert 'OSError' in message
    shown = _memory_truth({'reason': second['reason'], 'external_error': second['external_error']})
    assert 'cached probe refused the socket' in shown
    assert 'Last error: OSError: cached probe refused the socket' in shown


def test_a_long_error_keeps_its_ending_and_redacts_the_secret(tmp_path, monkeypatch):
    from tessera import bundled_engine
    from tessera.settings_model import _memory_truth

    secret = 'token-value-that-must-not-leak'
    monkeypatch.setenv('AETHERMIND_TOKEN', secret)
    tail = 'ENDING-STILL-VISIBLE'
    body = ('word ' * 80) + secret + ' ' + tail

    def probe(checked):
        raise OSError(body)

    def launch(checked, clock=None, *, force=False):
        return None

    monkeypatch.setattr(bundled_engine, 'probe_external', probe)
    monkeypatch.setattr(bundled_engine, 'ensure_bundled_engine', launch)
    config = Config(home=tmp_path, cwd=tmp_path)
    config.aethermind.url = 'https://memory.example.invalid/mcp'
    config.aethermind.token_env = 'AETHERMIND_TOKEN'
    choice = bundled_engine.choose_memory(config)
    assert len(choice['external_error']) > 200
    assert tail in choice['external_error']
    assert tail in choice['reason']
    assert secret not in choice['external_error']
    assert secret not in choice['reason']
    assert '[REDACTED]' in choice['external_error']
    shown = _memory_truth({'reason': choice['reason'], 'external_error': choice['external_error']})
    assert tail in shown
    assert secret not in shown
    message = bundled_engine.unavailable_message(choice)
    assert tail in message
    assert secret not in message


def test_stop_reports_the_inspect_error_message(tmp_path, monkeypatch):
    from tessera import bundled_engine

    secret = 'inspect-secret-token'
    monkeypatch.setenv('AETHERMIND_TOKEN', secret)
    tail = 'INSPECT-END'
    message = 'could not read pid ' + ('n' * 180) + ' ' + secret + ' ' + tail

    class Broken:
        def poll(self):
            raise OSError(message)

    bundle = tmp_path / 'engine'
    bundle.mkdir()
    (bundle / 'build-receipt.json').write_text('receipt\n')
    config = Config(home=tmp_path, cwd=tmp_path)
    config.aethermind.bundled_path = str(bundle)
    config.aethermind.token_env = 'AETHERMIND_TOKEN'
    root = bundled_engine._engine_root(config)
    runtime = str(bundled_engine._runtime_root(config, root))
    bundled_engine._REGISTRY[runtime] = {'url': 'http://127.0.0.1:59994/mcp', 'proc': Broken()}
    try:
        result = bundled_engine.stop_bundled_engine(config)
    finally:
        bundled_engine._REGISTRY.pop(runtime, None)
    assert result['stopped'] is False
    assert result['reason'].startswith('Embedded engine could not be inspected: OSError: ')
    assert tail in result['reason']
    assert secret not in result['reason']
    assert '[REDACTED]' in result['reason']


def test_stop_bundled_engine_labels_a_missing_receipt(tmp_path):
    from local_memory import receipt_less_bundle
    from tessera.bundled_engine import stop_bundled_engine

    config = Config(home=tmp_path, cwd=tmp_path)
    missing = stop_bundled_engine(config)
    assert missing['stopped'] is False
    assert 'bytes present, not started' in missing['reason']
    assert 'Packaged engine is not in this install' not in missing['reason']
    config.aethermind.bundled_path = str(tmp_path / 'does-not-exist')
    absent = stop_bundled_engine(config)
    assert absent['stopped'] is False
    assert absent['reason'] == 'Packaged engine is not in this install'
    assert 'receipt' not in absent['reason']
    config.aethermind.bundled_path = str(receipt_less_bundle(tmp_path))
    labeled = stop_bundled_engine(config)
    assert labeled['stopped'] is False
    assert labeled['reason'] == 'Packaged engine is not in this install'


@pytest.mark.aethermind_unavailable
def test_bundle_absence_uses_one_reason_for_choice_refusal_and_stop(tmp_path):
    from local_memory import receipt_less_bundle
    from tessera.bundled_engine import choose_memory, stop_bundled_engine, unavailable_message

    def surfaces(checked):
        choice = choose_memory(checked)
        stopped = stop_bundled_engine(checked)
        return choice['reason'], unavailable_message(choice), stopped['reason']

    bare_home = tmp_path / 'bare'
    bare_home.mkdir()
    choice_reason, refusal, stopped = surfaces(Config(home=bare_home, cwd=tmp_path))
    assert choice_reason == stopped
    assert choice_reason in refusal
    assert 'bytes present, not started' in choice_reason

    missing_home = tmp_path / 'missing'
    missing_home.mkdir()
    missing = Config(home=missing_home, cwd=tmp_path)
    missing.aethermind.bundled_path = str(tmp_path / 'does-not-exist')
    choice_reason, refusal, stopped = surfaces(missing)
    assert choice_reason == stopped == 'Packaged engine is not in this install'
    assert choice_reason in refusal

    empty_home = tmp_path / 'empty'
    empty_home.mkdir()
    empty = Config(home=empty_home, cwd=tmp_path)
    empty.aethermind.bundled_path = str(receipt_less_bundle(tmp_path))
    choice_reason, refusal, stopped = surfaces(empty)
    assert choice_reason == stopped == 'Packaged engine is not in this install'
    assert 'receipt' not in stopped
    assert choice_reason in refusal

    explicit_home = tmp_path / 'explicit'
    explicit_home.mkdir()
    explicit = Config(home=explicit_home, cwd=tmp_path)
    explicit.aethermind.source = 'embedded'
    explicit.aethermind.bundled_path = str(tmp_path / 'does-not-exist')
    choice_reason, refusal, stopped = surfaces(explicit)
    assert choice_reason == stopped == 'Packaged engine is not in this install'
    assert 'Embedded engine selected' not in choice_reason
    assert choice_reason in refusal
    backend = FakeBackend(['should not run'])
    with Session(empty, backend=backend) as session:
        with pytest.raises(AetherMindUnavailable) as refused:
            session.run_turn('hello')
    assert choice_reason in str(refused.value)
    assert backend.calls == []


def test_a_running_serve_returns_to_external_and_stops_the_embedded_engine(tmp_path, monkeypatch):
    from tessera import bundled_engine
    from tessera.mcp_client import McpError

    bundle = tmp_path / 'engine'
    (bundle / 'mcp').mkdir(parents=True)
    (bundle / 'build-receipt.json').write_text('receipt\n')
    proc = _EngineProc()
    embedded = 'http://127.0.0.1:59992/mcp'
    up = {'value': False}
    probes = []
    registered = []

    def probe(checked):
        probes.append(checked.aethermind.url)
        if not up['value']:
            raise McpError('No configured memory endpoint is reachable')

    def launch(checked, clock=None, *, force=False):
        root = bundled_engine._engine_root(checked)
        runtime = str(bundled_engine._runtime_root(checked, root))
        bundled_engine._REGISTRY[runtime] = {'url': embedded, 'proc': proc}
        registered.append(runtime)
        return embedded

    monkeypatch.setattr(bundled_engine, 'probe_external', probe)
    monkeypatch.setattr(bundled_engine, 'ensure_bundled_engine', launch)
    # The recovered service is the choice under test. Opening it would dial
    # the configured host; this client keeps that call off the network.
    from local_memory import QuietMemory
    import tessera.loop as loop
    monkeypatch.setattr(loop, 'AetherMindClient', QuietMemory)
    config = Config(home=tmp_path, cwd=tmp_path)
    config.aethermind.url = 'https://memory.example.invalid/mcp'
    config.aethermind.bundled_path = str(bundle)
    config.autonomy.enabled = False
    try:
        with SessionManager(config, backend_factory=lambda *args, **kwargs: FakeBackend([])) as manager:
            slug = manager.create('Notes')['slug']
            session = manager.runtime(slug).session
            assert session.memory_choice['source'] == 'embedded'
            assert session.memory_choice['url'] == embedded
            assert session.memory_choice['external_error'] == 'McpError: No configured memory endpoint is reachable'
            assert proc.exited is None
            up['value'] = True
            before = len(probes)
            report = manager.status()['aethermind']
            assert len(probes) > before
            assert session.memory_choice['source'] == 'external'
            assert session.memory_choice['external_error'] is None
            assert session.memory_choice['reason'] == 'External service validated'
            assert session.memory_choice['embedded_stop'] == 'Embedded engine stopped'
            assert report['embedded_stop'] == 'Embedded engine stopped'
            assert report['active'] == 'external'
            assert 'external_error' not in report
            assert 'No configured memory endpoint is reachable' not in report.get('reason', '')
            assert 'enabled' not in report
            assert proc.exited == 0
            other = manager.create('Later')['slug']
            follow = manager.runtime(other).session
            assert follow.memory_choice['source'] == 'external'
            assert follow.memory_choice['external_error'] is None
            assert follow.memory_choice['url'] == config.aethermind.url
    finally:
        for key in registered:
            bundled_engine._REGISTRY.pop(key, None)


def _external_then_down(tmp_path, monkeypatch, launch):
    """A serve whose first sessions validated an external service."""
    from tessera import bundled_engine
    from tessera.mcp_client import McpError

    up = {'value': True}
    probes = []

    def probe(checked):
        probes.append(checked.aethermind.url)
        if not up['value']:
            raise McpError('No configured memory endpoint is reachable')

    monkeypatch.setattr(bundled_engine, 'probe_external', probe)
    monkeypatch.setattr(bundled_engine, 'ensure_bundled_engine', launch)
    from local_memory import QuietMemory
    import tessera.loop as loop
    monkeypatch.setattr(loop, 'AetherMindClient', QuietMemory)
    config = Config(home=tmp_path, cwd=tmp_path)
    config.aethermind.url = 'https://memory.example.invalid/mcp'
    config.aethermind.bundled_path = str(tmp_path / 'engine')
    config.autonomy.enabled = False
    return up, probes, config


def test_a_live_session_falls_back_when_its_external_service_dies(tmp_path, monkeypatch):
    from tessera import bundled_engine

    bundle = tmp_path / 'engine'
    (bundle / 'mcp').mkdir(parents=True)
    (bundle / 'build-receipt.json').write_text('receipt\n')
    proc = _EngineProc()
    embedded = 'http://127.0.0.1:59991/mcp'
    launches = []
    registered = []

    def launch(checked, clock=None, *, force=False):
        launches.append(force)
        root = bundled_engine._engine_root(checked)
        runtime = str(bundled_engine._runtime_root(checked, root))
        bundled_engine._REGISTRY[runtime] = {'url': embedded, 'proc': proc}
        registered.append(runtime)
        return embedded

    up, probes, config = _external_then_down(tmp_path, monkeypatch, launch)
    holder = {}
    recorded = []
    backend = FakeBackend(['embedded answer', 'external answer'] + ['extra'] * 12)
    real_chat = backend.chat

    def chat(messages, tools=None, **kwargs):
        choice = getattr(holder.get('session'), 'memory_choice', None) or {}
        recorded.append((choice.get('source'), choice.get('url')))
        return real_chat(messages, tools, **kwargs)

    backend.chat = chat
    try:
        with SessionManager(config, backend_factory=lambda *args, **kwargs: backend) as manager:
            first = manager.create('Notes')['slug']
            second = manager.create('Other')['slug']
            session = manager.runtime(first).session
            other = manager.runtime(second).session
            holder['session'] = session
            assert session.memory_choice['source'] == 'external'
            assert other.memory_choice['source'] == 'external'
            assert launches == []
            up['value'] = False
            before = len(probes)
            turn = manager.submit(first, 'Use memory')
            assert manager.wait(first, timeout=10)
            row = manager.turn(first, turn)
            assert row['state'] == 'done', row
            assert len(probes) > before
            assert recorded, 'the turn finished without asking the model'
            assert recorded == [('embedded', embedded)] * len(recorded)
            assert session.memory_choice['source'] == 'embedded'
            assert session.memory_choice['url'] == embedded
            assert 'McpError' in session.memory_choice['external_error']
            assert other.memory_choice['source'] == 'embedded'
            assert other.memory_choice['url'] == embedded
            assert launches == [True]
            report = manager.status()['aethermind']
            assert report['active'] == 'embedded'
            assert report['process'] == embedded
            assert 'validated' not in report['reason']
            assert 'McpError' in report.get('external_error', '')
            assert not (report.get('reachable') is False and 'validated' in report.get('reason', ''))
            up['value'] = True
            back = manager.status()['aethermind']
            assert session.memory_choice['source'] == 'external'
            assert session.memory_choice['external_error'] is None
            assert other.memory_choice['source'] == 'external'
            assert other.memory_choice['url'] == config.aethermind.url
            assert back['active'] == 'external'
            assert back['reason'] == 'External service validated'
            assert 'external_error' not in back
            assert back['embedded_stop'] == 'Embedded engine stopped'
            assert proc.exited == 0
            mark = len(recorded)
            again = manager.submit(first, 'Back on the external service')
            assert manager.wait(first, timeout=10)
            assert manager.turn(first, again)['state'] == 'done'
            assert recorded[mark:]
            assert recorded[mark:] == [('external', config.aethermind.url)] * (len(recorded) - mark)
    finally:
        for key in registered:
            bundled_engine._REGISTRY.pop(key, None)


def test_a_live_session_refuses_when_the_external_dies_and_embedded_cannot_start(tmp_path, monkeypatch):
    def launch(checked, clock=None, *, force=False):
        return None

    up, _probes, config = _external_then_down(tmp_path, monkeypatch, launch)
    backend = FakeBackend(['should not run'])
    with SessionManager(config, backend_factory=lambda *args, **kwargs: backend) as manager:
        first = manager.create('Notes')['slug']
        second = manager.create('Other')['slug']
        session = manager.runtime(first).session
        other = manager.runtime(second).session
        assert session.memory_choice['source'] == 'external'
        assert other.memory_choice['source'] == 'external'
        up['value'] = False
        started = len(backend.calls)
        turn = manager.submit(first, 'Do not call the model')
        assert manager.wait(first, timeout=10)
        row = manager.turn(first, turn)
        assert row['state'] == 'error', row
        assert 'AetherMind is not available' in row['error']
        assert 'McpError' in row['error']
        assert 'No configured memory endpoint is reachable' in row['error']
        assert not session.memory_choice.get('url')
        assert not session.memory_choice.get('endpoints')
        assert not other.memory_choice.get('url')
        follow = manager.submit(second, 'Still no memory')
        assert manager.wait(second, timeout=10)
        follow_row = manager.turn(second, follow)
        assert follow_row['state'] == 'error', follow_row
        assert 'AetherMind is not available' in follow_row['error']
        assert len(backend.calls) == started
        report = manager.status()['aethermind']
        assert report.get('active') != 'external'
        assert 'validated' not in report.get('reason', '')


def test_a_failed_engine_stop_keeps_and_records_the_error(tmp_path, monkeypatch, capsys):
    from tessera.server import _release_embedded_engine

    def boom(checked):
        raise OSError('stop socket vanished END')

    monkeypatch.setattr('tessera.bundled_engine.stop_bundled_engine', boom)
    config = Config(home=tmp_path, cwd=tmp_path)
    result = _release_embedded_engine(config)
    assert result['stopped'] is False
    assert result['reason'] == 'Embedded engine stop failed: OSError: stop socket vanished END'
    assert 'stop socket vanished END' in capsys.readouterr().out

    def reported(checked):
        return {'stopped': False, 'reason': 'Embedded engine could not be inspected: OSError: disk gone END'}

    monkeypatch.setattr('tessera.bundled_engine.stop_bundled_engine', reported)
    shown = _release_embedded_engine(config)
    assert shown['reason'] == 'Embedded engine could not be inspected: OSError: disk gone END'
    assert 'disk gone END' in capsys.readouterr().out

    def unexpected(checked):
        raise RuntimeError('not an os error')

    monkeypatch.setattr('tessera.bundled_engine.stop_bundled_engine', unexpected)
    with pytest.raises(RuntimeError, match='not an os error'):
        _release_embedded_engine(config)


@pytest.mark.aethermind_unavailable
def test_a_refused_session_does_not_call_the_summarizer(tmp_path):
    config = Config(home=tmp_path, cwd=tmp_path)
    backend = FakeBackend(['should not run'])
    compactor = FakeBackend(['summary line'])
    with Session(config, backend=backend, compactor_backend=compactor) as session:
        assert not (session.memory_choice or {}).get('url')
        assert not (session.memory_choice or {}).get('endpoints')
        session.log.append('user', 'a line the summarizer would fold')
        with pytest.raises(AetherMindUnavailable) as refused:
            session.run_turn('hello')
        _refused(str(refused.value))
        time.sleep(0.6)
        assert backend.calls == []
        assert compactor.calls == []
        assert not any(thread.is_alive() for thread in session.compactor.threads)


def test_settings_surfaces_a_status_failure():
    from tessera.settings_model import _annotate_memory_status

    class Manager:
        def status(self):
            raise RuntimeError('status boom')

    sections = [{'id': 'integrations', 'fields': [
        {'id': 'aethermind.source', 'status': ''},
        {'id': 'aethermind.url', 'status': 'kept'},
        {'id': 'aethermind.project', 'status': 'untouched'},
    ]}]
    _annotate_memory_status(sections, Manager())
    assert sections[0]['fields'][0]['status'] == 'AetherMind status failed: RuntimeError: status boom'
    assert sections[0]['fields'][1]['status'] == 'kept · AetherMind status failed: RuntimeError: status boom'
    assert sections[0]['fields'][2]['status'] == 'untouched'


def test_supervised_shutdown_stops_the_embedded_engine(tmp_path, monkeypatch):
    import signal

    from tessera.server import TesseraServer, serve

    order = []
    monkeypatch.setattr(
        'tessera.bundled_engine.stop_bundled_engine',
        lambda checked: order.append('stop') or {'stopped': False, 'reason': 'Embedded engine is not running'})
    monkeypatch.setattr(TesseraServer, 'serve_forever', lambda self: None)

    def _exit(code):
        order.append('exit')
        raise SystemExit(code)

    monkeypatch.setattr('tessera.server.os._exit', _exit)
    config = Config(home=tmp_path, cwd=tmp_path)
    config.server.port = 0
    config.server.supervised = True
    config.autonomy.enabled = False
    previous = signal.getsignal(signal.SIGTERM)
    try:
        with pytest.raises(SystemExit):
            serve(config)
    finally:
        signal.signal(signal.SIGTERM, previous)
    assert order[:2] == ['stop', 'exit']


def _external_choice(url):
    return {
        'source': 'external', 'url': url, 'endpoints': [],
        'reason': 'External service validated',
    }


def test_a_closed_port_stays_unreachable_and_keeps_the_error(tmp_path, monkeypatch):
    from tessera import bundled_engine
    from tessera.capture import _category_for
    from tessera.mcp_client import McpError

    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    sock.close()
    url = f'http://127.0.0.1:{port}/mcp'
    monkeypatch.setattr(bundled_engine, 'ensure_bundled_engine', lambda *args, **kwargs: None)
    config = Config(home=tmp_path, cwd=tmp_path)
    config.aethermind.url = url
    config.aethermind.probe_timeout = 1
    with pytest.raises(McpError) as caught:
        bundled_engine.probe_external(config)
    text = str(caught.value)
    assert url in text
    assert 'Connection refused' in text
    assert 'No configured memory endpoint is reachable' not in text
    assert _category_for(caught.value) == 'unreachable'
    choice = bundled_engine.refresh_memory_choice(config, _external_choice(url))
    assert choice['external_category'] == 'unreachable'
    assert 'Connection refused' in choice['external_error']
    assert url in choice['external_error']
    assert 'timeout after' not in choice['external_error'].lower()


def test_a_poll_error_is_recorded_while_a_running_engine_is_reused(tmp_path, monkeypatch):
    from tessera import bundled_engine
    from tessera.mcp_client import McpError

    class Broken:
        def poll(self):
            raise PermissionError('engine pid could not be read')

    class Live:
        def poll(self):
            return None

    launches = []

    def probe(checked):
        raise McpError('external down')

    def launch(checked, clock=None, *, force=False):
        launches.append(force)
        return None

    monkeypatch.setattr(bundled_engine, 'probe_external', probe)
    monkeypatch.setattr(bundled_engine, 'ensure_bundled_engine', launch)
    config = Config(home=tmp_path, cwd=tmp_path)
    config.aethermind.url = 'https://memory.example.invalid/mcp'
    bundled_engine._REGISTRY['poll-broken'] = {
        'url': 'http://127.0.0.1:59990/mcp', 'proc': Broken()}
    try:
        missed = bundled_engine.refresh_memory_choice(config, _external_choice(config.aethermind.url))
    finally:
        bundled_engine._REGISTRY.pop('poll-broken', None)
    poll_failure = 'Embedded engine poll failed: PermissionError: engine pid could not be read'
    assert poll_failure in missed['reason']
    assert launches == [True]

    live = 'http://127.0.0.1:59989/mcp'
    bundled_engine._REGISTRY['poll-broken'] = {
        'url': 'http://127.0.0.1:59988/mcp', 'proc': Broken()}
    bundled_engine._REGISTRY['poll-live'] = {'url': live, 'proc': Live()}
    try:
        kept = bundled_engine.refresh_memory_choice(config, _external_choice(config.aethermind.url))
    finally:
        bundled_engine._REGISTRY.pop('poll-broken', None)
        bundled_engine._REGISTRY.pop('poll-live', None)
    assert kept['source'] == 'embedded'
    assert kept['url'] == live
    assert poll_failure in kept['reason']
    assert launches == [True]


class _HungExternal:
    """A loopback server that accepts and never answers."""

    def __init__(self):
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind(('127.0.0.1', 0))
        self.server.listen(16)
        self.server.settimeout(0.2)
        self.port = self.server.getsockname()[1]
        self.url = f'http://127.0.0.1:{self.port}/mcp'
        self.accepts = []
        self._held = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while not self._stop.is_set():
            try:
                conn, _addr = self.server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            self.accepts.append(time.monotonic())
            self._held.append(conn)

    def close(self):
        self._stop.set()
        try:
            self.server.close()
        except OSError:
            pass
        for conn in self._held:
            try:
                conn.close()
            except OSError:
                pass
        self._thread.join(timeout=2)


def test_open_sessions_share_one_hung_probe(tmp_path, monkeypatch):
    from tessera import bundled_engine

    hung = _HungExternal()
    probe_timeout = 0.6
    sessions_n = 4
    real_probe = bundled_engine.probe_external
    hang = {'on': False}
    probes = []

    def probe(checked):
        if not hang['on']:
            return None
        probes.append(checked.aethermind.url)
        return real_probe(checked)

    bundle = tmp_path / 'engine'
    (bundle / 'mcp').mkdir(parents=True)
    (bundle / 'build-receipt.json').write_text('receipt\n')
    proc = _EngineProc()
    embedded = 'http://127.0.0.1:59987/mcp'
    launches = []
    registered = []

    def launch(checked, clock=None, *, force=False):
        launches.append(force)
        root = bundled_engine._engine_root(checked)
        runtime = str(bundled_engine._runtime_root(checked, root))
        bundled_engine._REGISTRY[runtime] = {'url': embedded, 'proc': proc}
        registered.append(runtime)
        return embedded

    monkeypatch.setattr(bundled_engine, 'probe_external', probe)
    monkeypatch.setattr(bundled_engine, 'ensure_bundled_engine', launch)
    from local_memory import QuietMemory
    import tessera.loop as loop
    monkeypatch.setattr(loop, 'AetherMindClient', QuietMemory)
    config = Config(home=tmp_path, cwd=tmp_path)
    config.aethermind.url = hung.url
    config.aethermind.bundled_path = str(bundle)
    config.aethermind.probe_timeout = probe_timeout
    config.autonomy.enabled = False
    bound = probe_timeout * 2 + 0.45
    backend = FakeBackend(['embedded answer'] + ['extra'] * 12)
    try:
        with SessionManager(config, backend_factory=lambda *args, **kwargs: backend) as manager:
            slugs = [manager.create(f'Notes {index}')['slug'] for index in range(sessions_n)]
            opened = [manager.runtime(slug).session for slug in slugs]
            assert [session.memory_choice['source'] for session in opened] == ['external'] * sessions_n
            assert probes == []
            assert hung.accepts == []
            hang['on'] = True

            def one_refresh(action):
                before = len(probes)
                started = time.monotonic()
                result = action()
                elapsed = time.monotonic() - started
                assert len(probes) == before + 1, probes
                assert len(hung.accepts) == len(probes)
                assert elapsed < bound, elapsed
                assert elapsed >= probe_timeout * 0.75, elapsed
                return result

            def turn():
                handle = manager.submit(slugs[0], 'Use memory')
                assert manager.wait(slugs[0], timeout=10)
                row = manager.turn(slugs[0], handle)
                assert row['state'] == 'done', row

            one_refresh(turn)
            assert backend.calls
            for session in opened:
                assert session.memory_choice['source'] == 'embedded'
                assert session.memory_choice['url'] == embedded
                assert session.memory_choice['external_category'] == 'timeout'
                label = session.memory_choice['external_error']
                assert hung.url in label
                assert f'timeout after {format(probe_timeout, "g")}s' in label
                assert 'timed out' in label.lower()
                assert 'No configured memory endpoint is reachable' not in label
            assert launches == [True]
            assert len({id(session.memory_choice) for session in opened}) == sessions_n

            report = one_refresh(lambda: manager.status()['aethermind'])
            assert len(probes) == 2
            assert report['active'] == 'embedded'
            assert report['process'] == embedded
            assert 'validated' not in report.get('reason', '')
            assert hung.url in report.get('external_error', '')
            assert 'timeout after' in report.get('external_error', '')
            assert launches == [True]

            def overlap():
                barrier = threading.Barrier(2)
                errors = []

                def once():
                    try:
                        barrier.wait(timeout=5)
                        manager.refresh_open_memory()
                    except Exception as error:
                        errors.append(error)

                threads = [threading.Thread(target=once) for _ in range(2)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=10)
                assert not errors
                assert not any(thread.is_alive() for thread in threads)

            before = len(probes)
            started = time.monotonic()
            overlap()
            elapsed = time.monotonic() - started
            assert len(probes) == before + 1, probes
            assert len(hung.accepts) == len(probes)
            assert elapsed < bound, elapsed
            assert launches == [True]
            for session in opened:
                assert session.memory_choice['source'] == 'embedded'
                assert session.memory_choice['url'] == embedded
    finally:
        hung.close()
        for key in registered:
            bundled_engine._REGISTRY.pop(key, None)
