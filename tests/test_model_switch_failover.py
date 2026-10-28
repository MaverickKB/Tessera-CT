"""Per-session model switching and failover when the default model's service is down."""
from tessera.backend import BackendError
from tessera.config import BackendConfig, Config
from tessera.sessions import SessionManager

from fake_backend import FakeBackend


class Down:
    """The default model's service is unreachable (simulated; nothing real is touched)."""
    def __init__(self, error):
        self.error = error
        self.calls = 0

    def chat(self, messages, tools=None, **kw):
        self.calls += 1
        raise self.error


def manager_with(tmp_path, primary, failover_on=True):
    config = Config(home=tmp_path, cwd=tmp_path)
    config.tools.approval = 'never'
    config.backends = {
        'local': BackendConfig(name='local', base_url='http://127.0.0.1:9/v1', model='auto'),
        'grok-chat': BackendConfig(name='grok-chat', base_url='https://api.example.com/v1', model='grok-test'),
    }
    config.roles = {'main': 'local', **({'failover': 'grok-chat'} if failover_on else {})}
    grok = FakeBackend(['Answered by the failover model.'] * 4)
    backends = {'local': primary, None: primary, 'grok-chat': grok}
    return SessionManager(config, backend_factory=lambda slug, name: backends[name]), grok


def notes(manager, slug):
    return [m.text for m in manager.get(slug).log.iter() if m.kind == 'note']


def test_unreachable_default_fails_over_with_a_notice(tmp_path):
    down = Down(BackendError('Backend local: transport failed (URLError)', 'connection'))
    manager, grok = manager_with(tmp_path, down)
    with manager:
        manager.create('one')
        manager.submit('one', 'Hello')
        assert manager.wait('one', 5)
        log = manager.get('one').log
        assert [m.text for m in log.iter() if m.kind == 'agent'][-1] == 'Answered by the failover model.'
        assert 'local unavailable; used grok-test.' in notes(manager, 'one')
        assert down.calls == 1 and len(grok.calls) == 1


def test_server_error_fails_over_but_a_model_error_does_not(tmp_path):
    manager, _ = manager_with(tmp_path, Down(BackendError('HTTP 503', 'provider', kind='http', http_status=503)))
    with manager:
        manager.create('one')
        manager.submit('one', 'Hello')
        assert manager.wait('one', 5)
        assert any('used grok-test' in n for n in notes(manager, 'one'))
    manager, grok = manager_with(tmp_path / 'b', Down(BackendError('HTTP 400', 'provider', kind='http', http_status=400)))
    with manager:
        manager.create('two')
        manager.submit('two', 'Hello')
        manager.wait('two', 5)
        assert not any('used grok-test' in n for n in notes(manager, 'two')) and not grok.calls


def test_failover_off_surfaces_the_outage(tmp_path):
    manager, grok = manager_with(tmp_path, Down(BackendError('down', 'timeout')), failover_on=False)
    with manager:
        manager.create('one')
        manager.submit('one', 'Hello')
        manager.wait('one', 5)
        assert not grok.calls


def test_length_capped_answer_fails_over_but_a_refusal_does_not(tmp_path):
    manager, grok = manager_with(tmp_path, FakeBackend([{'content': '', 'finish_reason': 'length'}] * 2))
    with manager:
        manager.create('one')
        manager.submit('one', 'Hello')
        assert manager.wait('one', 5)
        assert [m.text for m in manager.get('one').log.iter() if m.kind == 'agent'][-1] == 'Answered by the failover model.'
        assert any('used grok-test' in n for n in notes(manager, 'one'))
        assert len(grok.calls) == 1
    manager, grok = manager_with(tmp_path / 'b', FakeBackend([{'content': 'I will not.', 'finish_reason': 'content_filter'}]))
    with manager:
        manager.create('two')
        manager.submit('two', 'Hello')
        manager.wait('two', 5)
        assert not grok.calls


def test_switching_a_session_model_mid_session_persists_and_applies_next_turn(tmp_path):
    local = FakeBackend(['From local.'] * 4)
    manager, grok = manager_with(tmp_path, local)
    with manager:
        manager.create('one')
        manager.submit('one', 'First')
        assert manager.wait('one', 5)
        assert manager.update('one', backend='grok-chat')['backend'] == 'grok-chat'
        manager.submit('one', 'Second')
        assert manager.wait('one', 5)
        replies = [m.text for m in manager.get('one').log.iter() if m.kind == 'agent']
        assert replies[-2:] == ['From local.', 'Answered by the failover model.']
        assert manager.describe('one')['backend'] == 'grok-chat'
        manager.update('one', backend='')
        assert manager.describe('one')['backend'] is None
        manager.submit('one', 'Third')
        assert manager.wait('one', 5)
        assert [m.text for m in manager.get('one').log.iter() if m.kind == 'agent'][-1] == 'From local.'


def test_unpinned_sessions_follow_a_changed_default_and_pinned_ones_stay(tmp_path):
    manager, _ = manager_with(tmp_path, FakeBackend(['ok'] * 4))
    with manager:
        manager.create('follows')
        manager.create('pinned', 'local')
        meta = tmp_path / 'sessions' / 'follows' / 'meta.json'
        assert '"backend": "local"' in meta.read_text()  # the default at creation, as older metas store it
        manager.config.roles['main'] = 'grok-chat'  # The operator switches the default (applied on restart)
        assert manager.get('follows').config.roles['main'] == 'grok-chat'
        assert manager.get('pinned').config.roles['main'] == 'local'
        assert manager.describe('follows')['backend'] is None
        assert manager.describe('pinned')['backend'] == 'local'
        manager.update('follows', backend='local')
        assert manager.describe('follows')['backend'] == 'local'
