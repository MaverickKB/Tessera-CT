from cli_fixtures import synthetic_codex_auth
from fake_backend import FakeBackend
from tessera.loop import Session, run_turn
from test_loop import session_config


def test_steer_during_model_call_continues_turn_with_guidance(tmp_path):
    holder = {}

    def first(messages, tools):
        assert holder['session'].steer('Actually answer in French') is True
        return 'Hello'

    backend = FakeBackend([first, 'Bonjour'])
    with Session(session_config(tmp_path), backend=backend) as session:
        holder['session'] = session
        assert run_turn(session, 'Say hello') == 'Bonjour'
        second = backend.calls[1]['messages']
        assert second[-1]['content'].endswith('Actually answer in French')
        assert second[-2] == {'role': 'assistant', 'content': 'Hello'}
        steers = [m for m in session.log.iter() if m.kind == 'user' and m.meta.get('steer')]
        assert [m.text for m in steers] == ['Actually answer in French']
        assert session.steer('late') is False


def test_interrupt_stops_turn_before_tools_run(tmp_path):
    holder = {}

    def call(messages, tools):
        assert holder['session'].interrupt() is True
        return {'tool_calls': [{'id': 'a', 'name': 'write_file', 'arguments': {'path': 'x.txt', 'text': 'no'}}]}

    config = session_config(tmp_path)
    backend = FakeBackend([call, 'Next turn works'])
    with Session(config, backend=backend) as session:
        holder['session'] = session
        assert run_turn(session, 'Write the file using write_file') == 'Interrupted by the user.'
        assert not (tmp_path / 'x.txt').exists()
        assert session.last_turn_success is False
        assert session.interrupt() is False
        assert run_turn(session, 'Hi') == 'Next turn works'


def test_steer_after_last_safe_point_is_queued_as_next_turn(tmp_path):
    from tessera.backend import BackendError
    from tessera.config import Config
    from tessera.sessions import SessionManager
    holder = {}

    def failing(messages, tools):
        assert holder['manager'].steer('s', 'Use this instead')['mode'] == 'steer'
        raise BackendError('cut off', kind='incomplete_response')

    backend = FakeBackend([failing, 'Applied late steer'])
    config = session_config(tmp_path)
    manager = SessionManager(config, backend_factory=lambda *args: backend)
    holder['manager'] = manager
    try:
        slug = manager.create(title='s')['slug']
        assert slug == 's'
        manager.submit(slug, 'First')
        runtime = manager.runtime(slug)
        import time
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            texts = [m.text for m in runtime.session.log.iter() if m.kind == 'agent']
            if 'Applied late steer' in texts:
                break
            time.sleep(.02)
        users = [m.text for m in runtime.session.log.iter() if m.kind == 'user']
        assert users == ['First', 'Use this instead']
        assert 'Applied late steer' in texts
    finally:
        manager.close()


def test_interrupt_cancels_in_flight_backend_call(tmp_path):
    import threading
    started, cancelled = threading.Event(), threading.Event()

    class Slow:
        def chat(self, messages, tools=None, **kw):
            started.set()
            assert cancelled.wait(10)
            raise RuntimeError('killed')

        def cancel(self):
            cancelled.set()
            return True

    with Session(session_config(tmp_path), backend=Slow()) as session:
        result = {}
        worker = threading.Thread(target=lambda: result.setdefault('reply', run_turn(session, 'Long')))
        worker.start()
        assert started.wait(10)
        assert session.interrupt() is True
        worker.join(10)
        assert result['reply'] == 'Interrupted by the user.'
        assert session.last_turn_interrupted is True


def test_cli_worker_cancel_kills_running_process(tmp_path):
    import sys, threading, time
    from tessera.cli_worker import CliWorker, WorkerError
    script = tmp_path / 'codex'
    script.write_text('#!' + sys.executable + '\nimport time\ntime.sleep(30)\n')
    script.chmod(0o755)
    worker = CliWorker({'vendor': 'codex', 'command': str(script), 'cwd': str(tmp_path), 'timeout': 60,
                        'args': ['exec', '--json', '--ephemeral', '-'],
                        'auth_path': synthetic_codex_auth(tmp_path)})
    errors = []
    thread = threading.Thread(target=lambda: errors.append(pytest_raises(worker)))
    def pytest_raises(w):
        try:
            w.chat([{'role': 'user', 'content': 'x'}])
        except WorkerError as e:
            return e
    thread = threading.Thread(target=lambda: errors.append(pytest_raises(worker)))
    start = time.monotonic()
    thread.start()
    while getattr(worker, '_current', None) is None and time.monotonic() - start < 10:
        time.sleep(.02)
    assert worker.cancel() is True
    thread.join(10)
    assert time.monotonic() - start < 10
    assert isinstance(errors[0], WorkerError)


def test_direct_route_promotes_to_tools_when_the_model_reaches_for_an_unoffered_tool(tmp_path):
    (tmp_path / 'a.txt').write_text('alpha')
    config = session_config(tmp_path)
    config.tools.direct_route_tools = ['memory_recall', 'memory_tree']
    leaked = 'Pulling it now.<tool_call>read_file<arg_key>path</arg_key><arg_value>a.txt</arg_value></tool_call>'
    backend = FakeBackend([{'content': leaked},
                           {'tool_calls': [{'id': 'r', 'name': 'read_file', 'arguments': {'path': 'a.txt'}}]},
                           'It says alpha.'])
    deltas = []
    with Session(config, backend=backend) as session:
        session.delta_sink = lambda kind, text: deltas.append(kind)
        assert run_turn(session, 'Hello') == 'It says alpha.'
        offered = [sorted(t['function']['name'] for t in call['tools'] or []) for call in backend.calls]
        assert 'memory_recall' in offered[0] and 'memory_tree' in offered[0]
        for name in ('skill_search', 'skill_load', 'skill_read', 'skill_write', 'zoom', 'date', 'session_briefs'):
            assert name in offered[0]
        assert 'read_file' in offered[0] and 'shell' in offered[0]
        assert 'shell' in offered[1] and 'read_file' in offered[1] and 'read_file' in offered[2]
        agents = [m for m in session.log.iter() if m.kind == 'agent']
        assert not any('<tool_call>' in m.text for m in agents)
        assert agents[-1].meta['route']['route'] == 'tools' and 'override' in agents[-1].meta['route']
        assert [m.text for m in session.log.iter() if m.kind == 'echo'] == ['alpha']
        assert session.last_turn_success is True


def test_direct_route_promotes_on_markup_naming_a_tool_it_never_saw(tmp_path):
    config = session_config(tmp_path)
    backend = FakeBackend([{'content': '<tool_call>mesh_status</tool_call><tool_call>mesh_machines</tool_call>'},
                           'node-a is healthy.'])
    with Session(config, backend=backend) as session:
        assert run_turn(session, 'Hello') == 'node-a is healthy.'
        first = {t['function']['name'] for t in backend.calls[0]['tools']}
        assert 'zoom' in first and 'skill_search' in first and 'mesh_status' not in first
        assert 'read_file' in first and 'shell' in first
        assert 'shell' in {t['function']['name'] for t in backend.calls[1]['tools']}
        config.tools.promote_direct_route = False
        backend.script.append({'content': 'Sure.<tool_call>x</tool_call>'})
        assert run_turn(session, 'Hello') == 'Sure.<tool_call>x</tool_call>'


def test_steer_during_a_tool_call_keeps_its_output_and_continues_the_turn(tmp_path):
    holder = {}

    def slow():
        assert holder['session'].steer('Use the desk profile') is True
        return 'slow-out'

    def after(messages, tools):
        assert messages[-2] == {'role': 'tool', 'tool_call_id': 's', 'content': 'slow-out'}
        assert messages[-1]['role'] == 'user' and messages[-1]['content'].endswith('Use the desk profile')
        return 'Adjusted for the desk profile.'

    backend = FakeBackend([{'tool_calls': [{'id': 's', 'name': 'slow', 'arguments': {}}]}, after])
    with Session(session_config(tmp_path), backend=backend) as session:
        holder['session'] = session
        from tessera.tools import object_schema
        session.tools.register('slow', 'Slow tool.', object_schema({}), slow)
        assert run_turn(session, 'Use slow to check the sample profile') == 'Adjusted for the desk profile.'
        kinds = [(m.kind, m.text) for m in session.log.iter() if m.kind in ('tool', 'echo', 'user', 'agent')]
        assert kinds[-3:] == [('echo', 'slow-out'), ('user', 'Use the desk profile'), ('agent', 'Adjusted for the desk profile.')]
        assert session.last_turn_success is True


def test_steer_while_thinking_streams_lands_after_the_tool_results(tmp_path):
    holder = {}

    class Streaming(FakeBackend):
        on_delta = None

        def chat(self, messages, tools=None, **kw):
            if not self.calls:
                self.on_delta('thought', 'Planning')
                assert holder['session'].steer('Also say hi') is True
                self.on_delta('thought', ' more')
            return super().chat(messages, tools, **kw)

    def after(messages, tools):
        assert messages[-2]['role'] == 'tool' and messages[-2]['content'] == 'alpha'
        assert messages[-1]['content'].endswith('Also say hi')
        return 'Hi. It says alpha.'

    (tmp_path / 'a.txt').write_text('alpha')
    backend = Streaming([{'tool_calls': [{'id': 'r', 'name': 'read_file', 'arguments': {'path': 'a.txt'}}]}, after])
    deltas = []
    with Session(session_config(tmp_path), backend=backend) as session:
        holder['session'] = session
        session.delta_sink = lambda kind, text: deltas.append((kind, text))
        assert run_turn(session, 'Use read_file on a.txt') == 'Hi. It says alpha.'
        assert ('thought', 'Planning') in deltas and ('thought', ' more') in deltas
        assert [m.text for m in session.log.iter() if m.kind == 'echo'] == ['alpha']
