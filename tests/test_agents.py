import pytest

from fake_backend import FakeBackend
from tessera.backend import BackendError
from tessera.config import Config
from tessera.loop import Session


def _spawn_failure_report(tmp_path, error):
    config = Config(home=tmp_path, cwd=tmp_path)
    config.jev.enabled = False
    with Session(config, backend=FakeBackend([]),
                 agent_backend_factory=lambda: FakeBackend([error])) as session:
        session.agents.spawn('failing-worker', 'Do the work', ['read_file'])
        assert session.agents.wait(timeout=5)
        reports = [m for m in session.log.iter() if m.kind == 'work']
        assert len(reports) == 1
        return reports[0]


def test_spawn_failure_preserves_backend_diagnostics(tmp_path):
    report = _spawn_failure_report(tmp_path, BackendError(
        'upstream rejected request', category='rate_limit', kind='http',
        http_status=429, detail='429 Too Many Requests'))
    assert report.meta['error_category'] == 'rate_limit'
    assert report.meta['error_kind'] == 'http'
    assert report.meta['http_status'] == 429
    assert '429' in report.meta['error_detail']
    assert report.text.count('rate_limit') == 1
    assert 'http_status=429' in report.text
    assert '429 Too Many Requests' in report.text


def test_spawn_failure_explains_missing_backend_diagnostics(tmp_path):
    report = _spawn_failure_report(tmp_path, BackendError(''))
    assert 'no diagnostic detail' in report.text
    assert 'provider, provider' not in report.text


def test_subagent_reports_to_main_log_and_cannot_expand_allowlist(tmp_path):
    config = Config(home=tmp_path, cwd=tmp_path)
    config.jev.enabled = False
    config.tools.approval = 'never'
    child = FakeBackend([
        {'tool_calls': [{'id': 'x', 'name': 'write_file', 'arguments': {'path': 'forbidden', 'text': 'bad'}}]},
        'Inspected the task with read-only tools.',
    ])
    with Session(config, backend=FakeBackend([]), agent_backend_factory=lambda: child) as session:
        session.agents.spawn('helper', 'Read the file only', ['read_file'])
        assert session.agents.wait(timeout=5)
        reports = [m for m in session.log.iter() if m.kind == 'work']
        assert len(reports) == 1
        assert reports[0].text == '[helper] Inspected the task with read-only tools.'
        assert not (tmp_path / 'forbidden').exists()
        assert (session.root / 'agents' / 'helper' / 'log').is_dir()
        assert '<chat>' in child.calls[0]['messages'][1]['content']
        assert {t['function']['name'] for t in child.calls[0]['tools']} == {'read_file'}
        with pytest.raises(ValueError):
            session.agents.spawn('../escape', 'task')


def test_default_spawn_gets_the_parent_tools_and_can_write(tmp_path):
    config = Config(home=tmp_path, cwd=tmp_path)
    config.jev.enabled = False
    config.tools.approval = 'never'
    child = FakeBackend([
        {'tool_calls': [{'id': 'w', 'name': 'write_file', 'arguments': {'path': 'from-the-worker.txt', 'text': 'wrote it'}}]},
        'Wrote the file.',
    ])
    with Session(config, backend=FakeBackend([]), agent_backend_factory=lambda: child) as session:
        session.agents.spawn('writer', 'Write the note')
        assert session.agents.wait(timeout=5)
        names = {item['function']['name'] for item in child.calls[0]['tools']}
        assert 'write_file' in names
        assert 'shell' in names
        assert (tmp_path / 'from-the-worker.txt').read_text() == 'wrote it'
        reports = session.agents.wait_for_reports(timeout=1)
        assert '[writer] Wrote the file.' in reports


def test_agent_depth_limit(tmp_path):
    config = Config(home=tmp_path, cwd=tmp_path)
    config.jev.enabled = False
    config.agents.max_depth = 0
    with Session(config, backend=FakeBackend([])) as session:
        with pytest.raises(RuntimeError, match='depth'):
            session.agents.spawn('child', 'task')


def test_inherited_view_has_explicit_parent_zoom_namespace(tmp_path):
    config = Config(home=tmp_path, cwd=tmp_path)
    config.jev.enabled = False
    parent_backend = FakeBackend(['I will retain that fact.'])
    child = FakeBackend([
        {'tool_calls': [{'id': 'z', 'name': 'zoom', 'arguments': {'start': 0, 'count': 1, 'source': 'parent'}}]},
        'Inherited fact recovered.',
    ])
    with Session(config, backend=parent_backend, agent_backend_factory=lambda: child) as session:
        session.run_turn('Remember inherited fact alpha-123')
        session.agents.spawn('reader', 'Use tools to recover the inherited first message.', ['zoom'])
        assert session.agents.wait(5)
        assert child.calls[1]['messages'][-1]['content'] == 'Remember inherited fact alpha-123'


def test_automatic_delegation_delivers_actual_result_to_root(tmp_path):
    config = Config(home=tmp_path, cwd=tmp_path)
    config.jev.enabled = False
    backend = FakeBackend(['Actual analysis result: alpha-123'])
    with Session(config, backend=backend) as session:
        session.run_turn('Delegate an independent task to inspect the note')
        assert session.agents.wait(5)
        reports = [message.text for message in session.log.iter() if message.kind == 'work']
        assert reports == ['[turn-1] Actual analysis result: alpha-123']


def test_default_spawn_survives_a_parent_tool_the_child_cannot_hold(tmp_path):
    # A depth-0 session carries tools a child never registers (MCP servers are registered
    # only at depth 0). The default "this session's tools" must not be built from those
    # names, or restricted() rejects it and every spawn fails with Unknown tool.
    config = Config(home=tmp_path, cwd=tmp_path)
    config.jev.enabled = False
    config.tools.approval = 'never'
    child = FakeBackend(['Done.'])
    with Session(config, backend=FakeBackend([]), agent_backend_factory=lambda: child) as session:
        session.tools.register('resident_only_probe', 'A tool only the resident session holds.',
                               {'type': 'object', 'properties': {}}, lambda: 'ok')
        session.agents.spawn('worker', 'Say done')
        assert session.agents.wait(timeout=5)
        names = {item['function']['name'] for item in child.calls[0]['tools']}
        assert 'resident_only_probe' not in names
        assert 'read_file' in names
        reports = session.agents.wait_for_reports(timeout=1)
        assert '[worker] Done.' in reports


def test_grandchild_inherits_the_parents_tool_ceiling(tmp_path):
    """Delegation narrows capability. A child restricted to spawn_agent must not be able to
    hand a grandchild the tools it was denied by omitting its own tools argument."""
    config = Config(home=tmp_path, cwd=tmp_path)
    config.jev.enabled = False
    config.tools.approval = 'never'
    grandchild = FakeBackend([
        {'tool_calls': [{'id': 'g', 'name': 'write_file', 'arguments': {'path': 'escaped.txt', 'text': 'escaped'}}]},
        'Wrote it.',
    ])
    child = FakeBackend([
        {'tool_calls': [{'id': 'c', 'name': 'spawn_agent', 'arguments': {'name': 'grand', 'task': 'Write the file'}}]},
        'Delegated.',
    ])
    backends = [child, grandchild]

    def factory():
        return backends.pop(0) if backends else FakeBackend([])

    with Session(config, backend=FakeBackend([]), agent_backend_factory=factory) as session:
        session.agents.spawn('middle', 'Delegate the write', ['spawn_agent'])
        assert session.agents.wait(timeout=10)
    assert {t['function']['name'] for t in child.calls[0]['tools']} == {'spawn_agent'}
    assert {t['function']['name'] for t in grandchild.calls[0]['tools']} == {'spawn_agent'}
    assert not (tmp_path / 'escaped.txt').exists()


def test_active_details_follow_the_real_worker_lifetime(tmp_path):
    import threading
    entered = threading.Event()
    release = threading.Event()

    def work(messages, tools):
        entered.set()
        assert release.wait(5)
        return 'Done'

    config = Config(home=tmp_path, cwd=tmp_path)
    config.jev.enabled = False
    child = FakeBackend([work])
    with Session(config, backend=FakeBackend([]), agent_backend_factory=lambda: child) as session:
        try:
            session.agents.spawn('visible-worker', 'Work until released')
            assert entered.wait(5)
            rows = session.agents.active_details()
            assert [row['name'] for row in rows] == ['visible-worker']
        finally:
            release.set()
            assert session.agents.wait(timeout=5)
        assert session.agents.active_details() == []


@pytest.mark.parametrize('kind,count,primary_kind', [
    ('cli_worker', 12, 'cli_worker'),
    ('openai_chat', 12, 'openai_chat'),
    ('openai_responses', 12, 'openai_responses'),
    ('anthropic_messages', 12, 'anthropic_messages'),
    ('anthropic_messages', 4, 'cli_worker'),
    ('anthropic_messages', 3, 'anthropic_messages'),
])
def test_advertised_local_slots_include_primary_cloud_is_unbounded(tmp_path, monkeypatch, kind, count, primary_kind):
    import threading
    release = threading.Event()
    config = Config(home=tmp_path, cwd=tmp_path)
    config.jev.enabled = False
    config.agents.concurrency_by_kind = {'anthropic_messages': 4} if count < 12 else {}
    parent = FakeBackend([])
    parent.provider_kind = primary_kind
    def work(messages, tools):
        assert release.wait(10)
        return 'Done'
    with Session(config, backend=parent, agent_backend_factory=lambda: FakeBackend([work])) as session:
        # Injected backend selection uses the parent's kind. Change the primary
        # after selection so this also covers a cloud root dispatching local work.
        original = session.agents._reserve_and_start
        def reserve(*args):
            parent.provider_kind = primary_kind
            return original(*args)
        monkeypatch.setattr(session.agents, '_reserve_and_start', reserve)
        try:
            for i in range(count):
                parent.provider_kind = kind
                session.agents.spawn(f'worker-{i}', 'Work', ['read_file'])
            assert session.agents.pool.running == count
            if count < 12:
                parent.provider_kind = kind
                with pytest.raises(RuntimeError, match='concurrency limit'):
                    session.agents.spawn('overflow', 'Work', ['read_file'])
        finally:
            release.set()
            assert session.agents.wait(10)


def test_cloud_primary_does_not_occupy_local_slots_using_same_protocol(tmp_path):
    from tessera.config import BackendConfig
    import threading
    release = threading.Event()
    config = Config(home=tmp_path, cwd=tmp_path)
    config.jev.enabled = False
    config.backends = {
        'cloud': BackendConfig(name='cloud', base_url='https://example.com/v1', model='fixture', api_key_env='FIXTURE_CLOUD_KEY'),
        'local': BackendConfig(name='local', base_url='http://localhost:9000/v1', model='fixture'),
    }
    config.roles = {'main': 'cloud'}
    config.agents.concurrency_by_kind = {'openai_chat': 4}
    def work(messages, tools):
        assert release.wait(10)
        return 'Done'
    with Session(config, backend=FakeBackend([]), agent_backend_factory=lambda: FakeBackend([work])) as session:
        try:
            for i in range(4):
                session.agents.spawn(f'local-{i}', 'Work', ['read_file'], backend='local')
            with pytest.raises(RuntimeError, match='concurrency limit'):
                session.agents.spawn('overflow', 'Work', ['read_file'], backend='local')
        finally:
            release.set()
            assert session.agents.wait(10)


@pytest.mark.parametrize('lane,roles,count,classification_error', [
    ('local', {'main': 'lane'}, 3, False),
    ('local', {}, 3, False),
    ('cloud', {'main': 'lane'}, 12, False),
    ('local', {'main': 'lane'}, 4, True),
])
def test_credential_root_slot_accounting(tmp_path, monkeypatch, lane, roles, count, classification_error):
    from tessera.config import BackendConfig
    from tessera.settings_ladder import credential_source, no_credential
    import threading

    release = threading.Event()
    config = Config(home=tmp_path, cwd=tmp_path)
    config.jev.enabled = False
    config.backends = {
        'lane': BackendConfig(name='lane', base_url='http://localhost:9000/v1',
                              model='fixture', api_key_env='FIXTURE_LANE_KEY' if lane == 'cloud' else ''),
    }
    config.roles = roles
    config.agents.max_concurrent = 0
    config.agents.concurrency_by_kind = {'openai_chat': 4} if lane == 'local' else {}
    assert no_credential(config, 'lane') is (lane == 'local')
    assert credential_source(config, 'lane')['source'] == ('none' if lane == 'local' else 'unavailable')

    def work(messages, tools):
        assert release.wait(10)
        return 'Done'

    with Session(config, backend=FakeBackend([]), agent_backend_factory=lambda: FakeBackend([work])) as session:
        if classification_error:
            def unavailable(*args, **kwargs):
                raise RuntimeError('credential classification unavailable')
            real = credential_source
            # Child selection uses a replaced config; only root classification fails.
            monkeypatch.setattr(config, 'backend_for', unavailable)
            def resolve(candidate, name, environ=None):
                if candidate is config:
                    raise RuntimeError('credential classification unavailable')
                return real(candidate, name, environ)
            monkeypatch.setattr('tessera.settings_ladder.credential_source', resolve)
        try:
            for i in range(count):
                session.agents.spawn(f'worker-{i}', 'Work', ['read_file'], backend='lane')
            assert session.agents.pool.running == count
            if lane == 'local':
                assert count + (0 if classification_error else 1) == 4
                with pytest.raises(RuntimeError, match='concurrency limit'):
                    session.agents.spawn('overflow', 'Work', ['read_file'], backend='lane')
        finally:
            release.set()
            assert session.agents.wait(10)
