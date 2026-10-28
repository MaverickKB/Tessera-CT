"""AetherMind on the turn: brief, honest errors, and visible note routing.

No sockets. The client is a fake object. Inheritance refresh already existed
(tests/test_capture.py); these tests cover the paths that were still missing.
"""

import json
import threading
import time

import pytest

from fake_backend import FakeBackend
from tessera.aethermind import AetherMindClient
from tessera.capture import Capture
from tessera.config import Config
from tessera.loop import Session
from tessera.mcp_client import McpError
from tessera.sessions import SessionManager


@pytest.fixture(autouse=True)
def packaged_engine_absent(monkeypatch):
    """Keep this file on the injected client. Absence is a missing engine root, not an off switch."""
    monkeypatch.setattr('tessera.bundled_engine._engine_root', lambda config: None)


def _config(tmp_path):
    config = Config(home=tmp_path, cwd=tmp_path)
    config.tools.approval = 'never'
    config.jev.enabled = False
    return config


class _Quiet:
    def prepare_inheritance(self, *args, **kwargs):
        return 'project layer'

    def write_layer(self, project, request_id, layer):
        return {'success': True, 'layer_id': '1'}


def test_brief_is_absent_when_there_is_no_client(tmp_path):
    from tessera.bundled_engine import AetherMindUnavailable

    capture = Capture(None, tmp_path / 'outbox', 'work')
    assert capture.turn_brief() == ''
    assert capture.service_error is None
    backend = FakeBackend(['ok', 'later'])
    with Session(_config(tmp_path), backend=backend, capture=capture) as session:
        with pytest.raises(AetherMindUnavailable, match='no AetherMind client'):
            session.run_turn('Hello')
    assert backend.calls == []


def test_a_ready_brief_is_in_the_turn_and_a_slow_one_does_not_block_it(tmp_path):
    release = threading.Event()
    started = threading.Event()

    class Client(_Quiet):
        def brief(self, project_id, anchor=None, budget=4000):
            started.set()
            assert release.wait(5)
            raise McpError('No configured memory endpoint is reachable')

    capture = Capture(Client(), tmp_path / 'outbox', 'work')
    backend = FakeBackend(['ok', 'later'])
    with Session(_config(tmp_path), backend=backend, capture=capture) as session:
        started_at = time.monotonic()
        session.run_turn('Hello')
        elapsed = time.monotonic() - started_at
        assert elapsed < 0.75
        assert started.wait(1)
        first = backend.calls[0]['messages'][3]['content']
        assert 'AetherMind brief: refresh in progress.' in first
        assert 'not reachable' not in first
        release.set()
        assert capture.settle_brief()
        session.run_turn('Again')
    second = backend.calls[1]['messages'][3]['content']
    assert 'AetherMind brief error: McpError: No configured memory endpoint is reachable' in second
    assert 'AetherMind brief:\n' not in second


def test_a_successful_brief_is_shown_and_a_rejected_brief_is_not_success(tmp_path):
    class Client(_Quiet):
        def brief(self, project_id, anchor=None, budget=4000):
            assert project_id == 'work'
            return 'kept digest'

    capture = Capture(Client(), tmp_path / 'outbox', 'work')
    capture.turn_brief()
    assert capture.settle_brief()
    backend = FakeBackend(['ok'])
    with Session(_config(tmp_path), backend=backend, capture=capture) as session:
        session.run_turn('Hello')
    assert 'AetherMind brief:\nkept digest' in backend.calls[0]['messages'][3]['content']

    class Rejecting(_Quiet):
        def brief(self, project_id, anchor=None, budget=4000):
            return {'success': False, 'error': 'layer type rejected'}

    rejected = Capture(Rejecting(), tmp_path / 'rejected', 'work')
    rejected.turn_brief()
    assert rejected.settle_brief()
    text = rejected._brief_line()
    assert 'AetherMind brief error: McpError: layer type rejected' in text
    assert 'AetherMind brief:\n' not in text
    assert rejected.service_error == 'McpError'


def test_recall_and_inheritance_keep_the_real_error_label(tmp_path):
    class Client:
        def prepare_inheritance(self, *args, **kwargs):
            raise McpError('MCP HTTP 401')

        def read_layers(self, *args, **kwargs):
            raise McpError('MCP HTTP 401')

    capture = Capture(Client(), tmp_path / 'outbox', 'work')
    block = capture.inheritance_block()
    assert 'Project inheritance unavailable (McpError: MCP HTTP 401).' in block
    assert capture._inheritance is None
    recalled = capture.memory_recall(query='history')
    assert recalled == {'error': 'McpError: MCP HTTP 401'}
    assert capture.service_error == 'McpError'
    assert capture.service_error_category == 'authentication'
    assert capture.memory_recall(query='history')['error'] != 'memory service unavailable'


def test_note_routing_is_visible_for_delivery_failure_and_off(tmp_path):
    sent = []

    class Client:
        def write_layer(self, project, request_id, layer):
            sent.append(project)
            if project == 'notes':
                raise McpError('MCP HTTP 403')
            return {'success': True, 'layer_id': '1'}

    capture = Capture(Client(), tmp_path / 'outbox', 'work', note_project='notes')
    ordinary = capture.memory_note('a plain note')
    assert ordinary['project'] == 'work' and ordinary['delivered'] is True and 'error' not in ordinary
    assert sent == ['work']
    decision = capture.memory_note('The operator decided X', type='decision')
    assert decision['project'] == 'notes'
    assert decision['delivered'] is False and decision['queued'] is True
    assert decision['error'].startswith('McpError') and '403' in decision['error']
    assert capture.service_error == 'McpError'
    assert capture.service_error_category == 'authentication'

    offline = Capture(None, tmp_path / 'off', 'work', note_project='notes')
    queued = offline.memory_note('hold this', type='lock')
    assert queued['project'] == 'notes' and queued['delivered'] is False
    assert queued['error'] == 'memory service unavailable'
    assert offline.service_error is None


def test_a_tool_error_is_labeled_in_the_capture_and_the_note_route_is_stored(tmp_path):
    capture = Capture(None, tmp_path / 'outbox', 'work')
    bus = _bus()
    capture.install(bus)
    bus.emit('session_start', {'session_id': 's'})
    bus.emit('turn_start', {'message': 'Write the note'})
    bus.emit('pre_tool', {'name': 'shell', 'id': 's1', 'arguments': {}})
    bus.emit('post_tool', {'name': 'shell', 'id': 's1', 'success': False,
                           'error': 'Tool error (ValueError): boom',
                           'result': 'Tool error (ValueError): boom'})
    bus.emit('pre_tool', {'name': 'memory_note', 'id': 'n1', 'arguments': {'text': 'decided', 'type': 'decision'}})
    bus.emit('post_tool', {'name': 'memory_note', 'id': 'n1', 'success': False,
                           'result': json.dumps({'error': 'McpError: MCP HTTP 401', 'project': 'notes',
                                                 'delivered': False, 'queued': True})})
    bus.emit('turn_end', {'reply': 'I could not store it.', 'success': False})
    body = capture.debt()[-1]['layer']['body']
    assert 'shell: failed (ValueError)' in body
    assert 'memory_note: failed (McpError), routed to notes' in body
    assert 'memory_note: ok' not in body


def test_the_loop_reports_a_failed_note_as_a_failure(tmp_path):
    class Client(_Quiet):
        def write_layer(self, project, request_id, layer):
            raise McpError('MCP HTTP 401')

    capture = Capture(Client(), tmp_path / 'outbox', 'work', note_project='notes')
    with Session(_config(tmp_path), backend=FakeBackend([]), capture=capture) as session:
        output, record, _ = session._call_tool({
            'id': 'n1', 'name': 'memory_note', 'arguments': {'text': 'decided', 'type': 'decision'}})
    body = json.loads(output)
    assert record['success'] is False
    assert body['project'] == 'notes' and body['delivered'] is False
    assert body['error'].startswith('McpError') and '401' in body['error']


def test_status_and_settings_say_unreachable_and_name_the_error(tmp_path):
    class Client:
        def prepare_inheritance(self, *args, **kwargs):
            raise McpError('No configured memory endpoint is reachable')

    capture = Capture(Client(), tmp_path / 'outbox', 'work')
    config = _config(tmp_path)
    config.aethermind.url = 'https://memory.example.invalid/mcp'
    config.aethermind.project = 'work'

    def factory(session_config, **kwargs):
        return Session(session_config, capture=capture, **kwargs)

    with SessionManager(config, session_factory=factory,
                        backend_factory=lambda *args, **kwargs: FakeBackend([])) as manager:
        manager.create('one')
        manager.runtime('one')
        report = manager.status()['aethermind']
        assert 'enabled' not in report and report['configured'] is True
        assert report['reachable'] is False
        assert report['last_error'] == 'McpError'
        assert report['error_category'] == 'unreachable'
        view = manager.settings_view()
        field = next(field for section in view['sections'] if section['id'] == 'integrations'
                     for field in section['fields'] if field['id'] == 'aethermind.url')
        assert 'Last error: McpError' in field['status']
        assert 'unreachable' in field['status']
        assert 'https://memory.example.invalid/mcp' in field['status']
        assert 'Off' not in field['status']

    off = _config(tmp_path / 'off')
    with SessionManager(off) as manager:
        manager.create('one')
        field = next(field for section in manager.settings_view()['sections'] if section['id'] == 'integrations'
                     for field in section['fields'] if field['id'] == 'aethermind.url')
        report = manager.status()['aethermind']
        assert field['status'].startswith('bytes present, not started')
        assert 'build receipt' in field['status']
        assert 'last_error' not in report
        assert report.get('active') != 'embedded'
        assert 'process' not in report
        assert report['engine'] == 'present'


def test_cached_tools_without_brief_fail_before_any_call():
    client = AetherMindClient('https://memory.example.invalid/mcp')
    client._tools = {'prepare_inheritance': {}, 'read_layers': {}}
    called = []
    client._mapped_call = lambda *args, **kwargs: called.append(args)
    with pytest.raises(McpError, match='does not expose brief'):
        client.brief('work')
    assert called == []


def _bus():
    class Bus:
        def __init__(self):
            self.hooks = {}

        def register(self, event, fn):
            self.hooks.setdefault(event, []).append(fn)

        def emit(self, event, payload):
            return [hook(payload) for hook in self.hooks.get(event, [])]

    return Bus()


@pytest.mark.parametrize('auto', [True, False])
def test_auto_recall_switch_controls_only_the_pre_turn_query(tmp_path, auto):
    config = _config(tmp_path)
    config.aethermind.auto_recall = auto
    capture = Capture(None, tmp_path / 'outbox', 'p', 'pp', 'a')
    calls = []
    capture.client = object()
    capture.memory_recall = lambda **kw: calls.append(kw) or {'layers': []}
    session = Session(config, backend=FakeBackend(['ok']), capture=capture)
    session.hooks.register('turn_start', lambda payload: payload.update(needs_memory=1.0))
    session.run_turn('What did we decide about the project history yesterday?')
    assert bool(calls) is auto
