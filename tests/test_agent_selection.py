"""Worker selection reaches real dispatch, cancellation and durable swarm paths."""

import threading

import pytest

from fake_backend import FakeBackend
from test_loop import session_config
from tessera.agent_selection import backend_catalog, select_backend
from tessera.backend import Backend
from tessera.config import BackendConfig
from tessera.loop import Session
from tessera.sessions import SessionManager


def configured(tmp_path):
    config = session_config(tmp_path)
    for name, level, rank in [('small', 'routine', 1), ('medium', 'standard', 2), ('large', 'complex', 3)]:
        config.backends[name] = BackendConfig(name=name, model=name, base_url='http://127.0.0.1:1')
        config.agents.backend_policy[name] = {'capability': level, 'cost_rank': rank}
    config.roles['main'] = 'large'
    return config


def test_cheapest_capable_selection_skips_unavailable_and_honors_explicit_choice(tmp_path):
    config = configured(tmp_path)
    assert select_backend(config) == 'small'
    assert select_backend(config, capability='standard') == 'medium'
    assert select_backend(config, capability='complex') == 'large'
    assert select_backend(config, backend='large') == 'large'
    config.backends['small'].enabled = False
    assert select_backend(config) == 'medium'
    config.backends['medium'].enabled = False
    assert select_backend(config) == 'large'
    config.backends['large'].enabled = False
    with pytest.raises(ValueError, match='No enabled'):
        select_backend(config)


@pytest.mark.parametrize('policy', [
    {'capability': 'unknown', 'cost_rank': 1},
    {'capability': 'routine', 'cost_rank': -1},
    {'capability': 'routine', 'cost_rank': True},
])
def test_invalid_cost_policy_is_rejected_before_dispatch(tmp_path, policy):
    config = configured(tmp_path)
    config.agents.backend_policy['small'] = policy
    with pytest.raises(ValueError):
        config.validate()


def test_workers_use_selected_independent_backends_and_the_current_workspace(tmp_path, monkeypatch):
    config = configured(tmp_path)
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    seen = []
    barrier = threading.Barrier(2)

    def chat(backend, messages, tools=None, **kwargs):
        seen.append((backend.name, id(backend)))
        barrier.wait(timeout=5)
        return {'content': backend.name + ' completed', 'tool_calls': []}

    monkeypatch.setattr(Backend, 'chat', chat)
    with Session(config) as parent:
        parent.set_workspace(workspace)
        parent.tools.execute('spawn_agent', {'name': 'one', 'task': 'Inspect the file', 'capability': 'routine'})
        parent.tools.execute('spawn_agent', {'name': 'two', 'task': 'Inspect another file', 'capability': 'standard'})
        assert parent.agents.wait(5)
        assert {name for name, _ in seen} == {'small', 'medium'}
        assert len({identity for _, identity in seen}) == 2
        reports = [m for m in parent.log.iter() if m.kind == 'work']
        assert {m.meta['backend'] for m in reports} == {'small', 'medium'}
        assert all(m.meta['success'] for m in reports)
        assert parent.tools.execute('agent_backends', {}) == backend_catalog(config)


def test_stop_reaches_delegated_worker_after_parent_turn_has_returned(tmp_path):
    config = session_config(tmp_path)
    entered = threading.Event()
    cancelled = threading.Event()

    class Blocking:
        def chat(self, messages, tools=None, **kwargs):
            entered.set()
            assert cancelled.wait(5)
            raise RuntimeError('cancelled')

        def cancel(self):
            cancelled.set()
            return True

    with Session(config, backend=FakeBackend([]), agent_backend_factory=Blocking) as parent:
        parent.run_turn('Delegate an independent task to inspect the note')
        assert entered.wait(5)
        assert not parent._in_turn.is_set()
        assert parent.interrupt()
        assert cancelled.wait(1)
        assert parent.agents.wait(5)


def test_cost_policy_lets_the_parent_declare_difficulty_before_delegating(tmp_path):
    config = configured(tmp_path)
    backend = FakeBackend([
        {'tool_calls': [{'id': 'delegate', 'name': 'spawn_agent', 'arguments': {
            'name': 'architect', 'task': 'Analyze the architecture', 'capability': 'complex'}}]},
        'Architecture work delegated.',
    ])
    with Session(config, backend=backend,
                 agent_backend_factory=lambda: FakeBackend(['Architecture findings.'])) as parent:
        parent.run_turn('Delegate an independent task to analyze the architecture')
        assert parent.agents.wait(5)
        assert backend.calls, 'Routing must not force an unclassified task onto the cheapest tier'
        reports = [m for m in parent.log.iter() if m.kind == 'work']
        assert reports[0].meta['backend'] == 'large'


def test_live_owner_swarm_tool_selects_workers_without_a_second_manager(tmp_path):
    config = configured(tmp_path)
    config.swarm.enabled = True
    config.swarm.distinct_workers = False
    with SessionManager(config, backend_factory=lambda *args: FakeBackend([]),
                        swarm_backend_factory=lambda selected: FakeBackend([
                            {'content': selected.name + ' finding', 'usage': {'total_tokens': 3}}
                        ])) as manager:
        slug = manager.create('swarm-owner')['slug']
        session = manager.runtime(slug).session
        job = session.tools.execute('swarm_job', {'action': 'create', 'objective': 'Inspect independent facts',
            'workers': [{'objective': 'Inspect A', 'scope': ['a']},
                        {'objective': 'Inspect B', 'scope': ['b'], 'capability': 'standard'}]})
        assert [w['backend'] for w in job['workers']] == ['small', 'medium']
        result = session.tools.execute('swarm_job', {'action': 'run', 'job_id': job['id']})
        assert result['status'] == 'succeeded'
        artifact = session.tools.execute('swarm_job', {'action': 'artifact', 'job_id': job['id']})
        assert [a['content'] for a in artifact['artifacts']] == ['small finding', 'medium finding']


def test_agent_capacity_is_shared_across_saved_sessions(tmp_path, monkeypatch):
    config = configured(tmp_path)
    config.agents.max_concurrent = 1
    entered = threading.Event()
    release = threading.Event()

    def chat(backend, messages, tools=None, **kwargs):
        entered.set()
        assert release.wait(5)
        return {'content': 'Done', 'tool_calls': []}

    monkeypatch.setattr(Backend, 'chat', chat)
    with SessionManager(config) as manager:
        first = manager.runtime(manager.create('first')['slug']).session
        second = manager.runtime(manager.create('second')['slug']).session
        first.agents.spawn('worker', 'Inspect a fact')
        try:
            assert entered.wait(5)
            assert second.agents.active() == 0
            assert second.interrupt() is False
            with pytest.raises(RuntimeError, match='concurrency'):
                second.agents.spawn('other', 'Inspect another fact')
        finally:
            release.set()
            assert first.agents.wait(5)


@pytest.mark.parametrize('custom_args', [False, True])
@pytest.mark.parametrize('mode,tools,expected', [
    ('yolo', None, 'workspace-write'),
    ('never', ['write_file'], 'workspace-write'),
    ('ask', None, 'read-only'),
    ('deny_risky', None, 'read-only'),
    ('yolo', ['read_file'], 'read-only'),
])
def test_spawned_codex_argv_inherits_live_authority(tmp_path, monkeypatch, mode, tools, expected, custom_args):
    config = configured(tmp_path)
    config.backends['large'].provider_kind = 'cli_worker'
    args = ['exec', '--json', '--sandbox', 'workspace-write', '-'] if custom_args else []
    config.backends['large'].extra = {'cli': {'vendor': 'codex', 'command': 'codex',
        'cwd': str(tmp_path), 'args': args}}
    # Start from a different policy to prove the live registry is consulted.
    config.tools.approval = 'ask'
    seen = []
    monkeypatch.setattr('tessera.cli_worker.shutil.which', lambda command: command)

    def chat(backend, messages, tools=None, **kwargs):
        seen.append(backend.worker._argv())
        return {'content': 'Done', 'tool_calls': []}

    monkeypatch.setattr(Backend, 'chat', chat)
    with Session(config, backend=FakeBackend([])) as parent:
        parent.tools.approval = mode
        parent.agents.spawn('worker', 'Inspect or implement', tools=tools, backend='large')
        assert parent.agents.wait(5)
        assert len(seen) == 1
        argv = seen[0]
        assert argv[argv.index('--sandbox') + 1] == expected
        assert argv.count('--sandbox') == 1
        assert argv[1] == 'exec'
        assert '--json' in argv and argv[-1] == '-'
        assert not {'resume', 'fork', '--last'}.intersection(argv)
        assert all(m.meta['success'] for m in parent.log.iter() if m.kind == 'work')


def test_cli_and_local_capacity_are_independent_and_shared_across_sessions(tmp_path, monkeypatch):
    config = configured(tmp_path)
    config.agents.concurrency_by_kind = {'cli_worker': 6}
    # session_config sets approval 'never', which is YOLO. The Safe default of 1
    # does not apply. A hand-set 4 is what caps this local lane.
    config.backends['small'].max_concurrent_requests = 4
    # Resolve CLI kind through the account, as real OAuth model variants do.
    from tessera.config import AccountConfig
    config.accounts['oauth'] = AccountConfig(name='oauth', provider_kind='cli_worker')
    for name in ('medium', 'large'):
        config.backends[name].account = 'oauth'
        config.backends[name].extra = {'cli': {'vendor': 'codex', 'command': 'codex', 'cwd': str(tmp_path)}}
    release = threading.Event()

    def chat(backend, messages, tools=None, **kwargs):
        assert release.wait(15)
        return {'content': 'Done', 'tool_calls': []}

    monkeypatch.setattr(Backend, 'chat', chat)
    with SessionManager(config) as manager:
        first = manager.runtime(manager.create('first')['slug']).session
        second = manager.runtime(manager.create('second')['slug']).session
        try:
            for index in range(6):
                first.agents.spawn(f'cli-{index}', 'Wait', backend='medium')
            assert first.agents.pool.running_by_kind['cli_worker'] == 6
            with pytest.raises(RuntimeError, match='concurrency'):
                second.agents.spawn('cli-overflow', 'Wait', backend='large')
            for index in range(4):
                second.agents.spawn(f'local-{index}', 'Wait', backend='small')
            assert first.agents.pool.running == 10
            with pytest.raises(RuntimeError, match='concurrency'):
                first.agents.spawn('local-overflow', 'Wait', backend='small')
        finally:
            release.set()
            assert first.agents.wait(15)
            assert second.agents.wait(15)
        assert first.agents.pool.running == 0
        assert all(count == 0 for count in first.agents.pool.running_by_kind.values())
        reports = list(first.log.iter()) + list(second.log.iter())
        assert all(m.meta['success'] for m in reports if m.kind == 'work')
        # Completion releases the reservation for another model variant.
        second.agents.spawn('after-release', 'Done', backend='large')
        assert second.agents.wait(5)


@pytest.mark.parametrize('caps', [{'cli_worker': -1}, {'cli_worker': True},
                                 {'cli_worker': 1.5}, {'unknown': 4}, []])
def test_invalid_agent_kind_caps_are_rejected(tmp_path, caps):
    config = configured(tmp_path)
    config.agents.concurrency_by_kind = caps
    with pytest.raises(ValueError, match='concurrency'):
        config.validate()


def test_unset_safe_local_lane_starts_subagents(tmp_path):
    """An omitted max_concurrent_requests must not refuse every local subagent.

    Safe mode still allows one request at a time on the model. That slot is not
    a lane the chat fills so that no child can start. Main allowed three.
    """
    release = threading.Event()
    config = configured(tmp_path)
    config.tools.approval = 'ask'
    config.agents.max_concurrent = 0
    config.agents.concurrency_by_kind = {}
    config.backends['small'].api_key_env = ''
    config.backends['small'].max_concurrent_requests = None
    config.roles['main'] = 'small'

    def chat(backend, messages, tools=None, **kwargs):
        assert release.wait(15)
        return {'content': 'Done', 'tool_calls': []}

    with Session(config, backend=FakeBackend([]),
                 agent_backend_factory=lambda: FakeBackend([chat] * 8)) as session:
        try:
            for index in range(3):
                session.agents.spawn(f'local-{index}', 'Wait', backend='small')
            assert session.agents.pool.running == 3
        finally:
            release.set()
            assert session.agents.wait(15)
    from tessera.settings_model import CONCURRENCY_HINT
    assert 'max_concurrent_requests on this model is the cap, and the chat holds one slot' in CONCURRENCY_HINT


def test_hand_set_local_limit_error_names_max_concurrent_requests(tmp_path):
    config = configured(tmp_path)
    config.tools.approval = 'ask'
    config.agents.max_concurrent = 0
    config.agents.concurrency_by_kind = {}
    config.backends['small'].api_key_env = ''
    config.backends['small'].max_concurrent_requests = 1
    config.roles['main'] = 'small'
    with Session(config, backend=FakeBackend([]),
                 agent_backend_factory=lambda: FakeBackend([])) as session:
        with pytest.raises(RuntimeError, match=r'max_concurrent_requests on this model is 1, and the chat holds one slot'):
            session.agents.spawn('local-0', 'Wait', backend='small')


def test_agent_kind_caps_load_and_preserve_conservative_default(tmp_path):
    from tessera.config import AgentsConfig, load_config
    defaults = AgentsConfig()
    # The ruling: there is no global worker cap, so the blunt
    # scalar defaults to 0 (unbounded) and deliberately advertises no per-kind
    # lane. A local model's width is its max_concurrent_requests.
    assert defaults.max_concurrent == 0
    assert defaults.concurrency_by_kind == {}
    (tmp_path / 'config.toml').write_text('[agents]\nconcurrency_by_kind = { cli_worker = 8, openai_chat = 2 }\n')
    loaded = load_config(tmp_path)
    assert loaded.agents.concurrency_by_kind == {'cli_worker': 8, 'openai_chat': 2}


def test_cloud_workers_are_not_capped_by_the_local_serving_width(tmp_path):
    """Cloud workers are not capped by the local serving width.

    Every backend here reports provider_kind 'openai_chat', so a lane keyed on the
    kind alone puts a model that names a credential and a model with none in ONE
    bucket and the local model's four serving slots then refuse the fifth remote
    worker.

    The two halves are asserted together, because fixing only one leaves the other:
    (1) a cloud backend is unbounded, and (2) local and cloud work do not share a
    counter, so running cloud workers cannot starve the local lane of its slots.
    """
    release = threading.Event()
    config = configured(tmp_path)
    config.agents.max_concurrent = 0
    config.agents.concurrency_by_kind = {}
    config.backends['small'].api_key_env = ''          # the local serving model
    config.backends['small'].max_concurrent_requests = 4
    config.backends['small'].base_url = 'http://127.0.0.1:8680/v1'
    config.backends['medium'].api_key_env = 'FIXTURE_CLOUD_KEY'
    config.backends['large'].api_key_env = 'FIXTURE_CLOUD_KEY'
    # The root session runs on the local model, so it holds one of its four
    # slots; that is the accounting the lane must preserve.
    config.roles['main'] = 'small'

    def chat(backend, messages, tools=None, **kwargs):
        assert release.wait(15)
        return {'content': 'Done', 'tool_calls': []}

    with Session(config, backend=FakeBackend([]),
                 agent_backend_factory=lambda: FakeBackend([chat] * 40)) as session:
        try:
            # Cloud first, past the local width. Eight is well beyond 4.
            for index in range(8):
                session.agents.spawn(f'cloud-{index}', 'Wait', backend='medium')
            # Then a DIFFERENT cloud backend: its own unbounded lane, not the shared local one.
            for index in range(8):
                session.agents.spawn(f'cloud2-{index}', 'Wait', backend='large')
            # The local lane is still empty, so it admits its own three workers
            # (the root session holds the fourth slot).
            for index in range(3):
                session.agents.spawn(f'local-{index}', 'Wait', backend='small')
            with pytest.raises(RuntimeError, match='concurrency limit'):
                session.agents.spawn('local-overflow', 'Wait', backend='small')
            assert session.agents.pool.running == 19
        finally:
            release.set()
            assert session.agents.wait(20)
        assert session.agents.pool.running == 0
        assert all(count == 0 for count in session.agents.pool.running_by_kind.values())


def test_local_aliases_on_one_endpoint_share_a_single_lane(tmp_path):
    """Three aliases for one Spark process must not invent twelve slots.

    router-auto, router-struct and router-auto-lite all point at :8680, one
    process serving four concurrent requests. Keying the local lane by backend
    NAME would hand each alias its own four and over-subscribe the machine.
    """
    release = threading.Event()
    config = configured(tmp_path)
    config.agents.max_concurrent = 0
    config.agents.concurrency_by_kind = {}
    for name in ('small', 'medium', 'large'):
        config.backends[name].api_key_env = ''
        config.backends[name].base_url = 'http://127.0.0.1:8680/v1'
        config.backends[name].max_concurrent_requests = 4
    config.roles['main'] = 'small'

    def chat(backend, messages, tools=None, **kwargs):
        assert release.wait(15)
        return {'content': 'Done', 'tool_calls': []}

    with Session(config, backend=FakeBackend([]),
                 agent_backend_factory=lambda: FakeBackend([chat] * 20)) as session:
        try:
            session.agents.spawn('a-0', 'Wait', backend='small')
            session.agents.spawn('b-0', 'Wait', backend='medium')
            session.agents.spawn('c-0', 'Wait', backend='large')
            # Four slots, root holds one: the third worker fills the lane.
            with pytest.raises(RuntimeError, match='concurrency limit'):
                session.agents.spawn('a-1', 'Wait', backend='small')
            assert session.agents.pool.running == 3
            assert len(session.agents.pool.running_by_kind) == 1
        finally:
            release.set()
            assert session.agents.wait(20)


def test_a_writer_is_refused_an_unrooted_workspace_but_a_reader_is_not(tmp_path):
    """A spawn with no repo= must not silently root in an ancestor checkout.

    repository_root() walks up to the enclosing checkout, so a session working in
    a subdirectory of a bigger repo would branch its writer from that bigger repo
    and report a base that does not exist in the intended project.

    The refusal must be scoped to writers. A read-only worker never creates a
    branch, so the enclosing checkout is not a guess it can get wrong. Refusing
    it as well would break every read-only spawn from an unrooted workspace,
    which is why both halves are asserted here.
    """
    import subprocess
    config = configured(tmp_path)
    outer = tmp_path / 'outer'
    inner = outer / 'nested' / 'workspace'
    inner.mkdir(parents=True)
    run = lambda *a: subprocess.run(a, cwd=outer, check=True, capture_output=True)
    run('git', 'init', '-q')
    run('git', 'config', 'user.email', 'probe@example.com')
    run('git', 'config', 'user.name', 'probe')
    (outer / 'seed.txt').write_text('seed')
    run('git', 'add', 'seed.txt')
    run('git', 'commit', '-q', '-m', 'seed')

    config.cwd = inner
    config.workspace = str(inner)
    config.agents.worktrees_dir = str(tmp_path / 'worktrees')

    with Session(config, backend=FakeBackend([]),
                 agent_backend_factory=lambda: FakeBackend(['Done', 'Done'])) as session:
        # A writer is refused: it would otherwise branch from `outer`.
        with pytest.raises(ValueError, match='pass repo= explicitly'):
            session.agents.spawn('writer', 'Write', ['write_file'])
        # A reader is allowed: it makes no branch, so there is nothing to misroot.
        session.agents.spawn('reader', 'Read', ['read_file'])
        assert session.agents.wait(timeout=10)
        reports = [m for m in session.log.iter() if m.kind == 'work']
        assert len(reports) == 1
        assert not any('worktree' in str(m.meta) for m in reports)
