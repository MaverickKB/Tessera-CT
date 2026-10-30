import re

import pytest

from fake_backend import FakeBackend
from tessera.config import Config
from tessera.hooks import HookBus
from tessera.loop import Session, run_turn
from tessera.backend import BackendError


def session_config(tmp_path):
    config = Config(home=tmp_path, cwd=tmp_path)
    config.tools.approval = 'never'
    config.jev.enabled = False
    return config


def test_tool_loop_logs_and_uses_constant_prompt_prefix(tmp_path):
    backend = FakeBackend([
        {'tool_calls': [{'id': 'a', 'name': 'write_file', 'arguments': {'path': 'answer.txt', 'text': 'forty-two'}}]},
        'Wrote the answer.',
        'You are welcome.',
    ])
    with Session(session_config(tmp_path), backend=backend) as session:
        assert run_turn(session, 'Write the answer') == 'Wrote the answer.'
        assert (tmp_path / 'answer.txt').read_text() == 'forty-two'
        assert run_turn(session, 'Thanks') == 'You are welcome.'
        assert [m.kind for m in session.log.iter()] == ['user', 'agent', 'tool', 'echo', 'agent', 'user', 'agent']
        assert backend.calls[0]['messages'][0] == backend.calls[2]['messages'][0]
        assert backend.calls[0]['messages'][0]['role'] == 'system'
        assert backend.calls[0]['messages'][1]['content'].startswith('<chat>')
        assert backend.calls[0]['messages'][2]['content'].startswith('<tools>')
        assert '<turn-state>' in backend.calls[0]['messages'][3]['content']
        assert backend.calls[0]['messages'][4] == {'role': 'user', 'content': 'Write the answer'}
        assert backend.calls[1]['messages'][-1]['role'] == 'tool'
        assert session.capture.debt()


def test_the_turn_prompt_is_capped_at_the_settle_budget(tmp_path):
    """A session that outgrows its view still sends a bounded prompt.

    The conversation view is the gist the model reads. It is capped at the view's
    settle budget; older spans stay in the tree and remain zoomable. Without the cap
    a session sends its whole rendered view every turn, so an over-budget view turns
    into an over-budget prompt on every single call.
    """
    backend = FakeBackend(['Acknowledged.'])
    config = session_config(tmp_path)
    with Session(config, backend=backend) as session:
        limit = config.view.low_bytes
        line = 'y' * 400
        seeded = (limit // 400) + 40
        for index in range(seeded):
            session.log.append('user', f'{index} {line}')
        assert len(session.view.render().encode()) > limit
        run_turn(session, 'Continue')
        gist = backend.calls[0]['messages'][1]['content']
        # The wrapper and the omission notice are outside the line budget.
        assert len(gist.encode()) <= limit + 200
        assert gist.startswith('<chat>')
        assert 'Older coverage is omitted' in gist


def test_hook_block_prevents_side_effect_and_still_captures(tmp_path):
    hooks = HookBus()
    events = []
    hooks.register('pre_tool', lambda p: {'block': True, 'reason': 'test gate'})
    hooks.register('post_tool', lambda p: events.append(p))
    backend = FakeBackend([
        {'tool_calls': [{'id': 'a', 'name': 'write_file', 'arguments': {'path': 'forbidden', 'text': 'x'}}]},
        'The tool was blocked.',
    ])
    with Session(session_config(tmp_path), backend=backend, hooks=hooks) as session:
        assert session.run_turn('Try writing with write_file') == 'The tool was blocked.'
        assert not (tmp_path / 'forbidden').exists()
        assert events[0]['blocked'] is True
        assert 'test gate' in session.log.get(3).text
        assert session.capture.debt()


def test_approval_refusal_from_the_registry_reports_blocked_not_a_failed_tool(tmp_path):
    """A refusal is not a failure: the side effect never happened.

    The registry raises ApprovalRefused when policy forbids a risky tool. If the turn
    loop swallows it into the generic error arm, `blocked` stays False and every reader
    of that field (a hook, a board card, a judge) sees a prevented mutation as one that
    ran. A prevented deletion must not be recorded as a tool that ran.
    """
    events = []
    hooks = HookBus()
    hooks.register('post_tool', lambda p: events.append(p))
    backend = FakeBackend([
        {'tool_calls': [{'id': 'a', 'name': 'shell', 'arguments': {'command': 'rm marker.txt'}}]},
        'Policy blocked deletion; marker.txt remains.',
    ])
    config = session_config(tmp_path)
    config.tools.approval = 'deny_risky'
    (tmp_path / 'marker.txt').write_text('keep')
    with Session(config, backend=backend, hooks=hooks) as session:
        session.run_turn('Run rm marker.txt; the policy should block deletion.')
        assert (tmp_path / 'marker.txt').read_text() == 'keep', 'the side effect must not happen'
        assert events[0]['blocked'] is True, 'a refusal must report blocked, not a failed tool'
        assert events[0]['success'] is False
        assert events[0]['result'].startswith('Blocked: Approval required for shell')


def test_turn_continues_past_the_old_iteration_limit(tmp_path):
    config = session_config(tmp_path)
    backend = FakeBackend([
        {'tool_calls': [{'id': 'a', 'name': 'date', 'arguments': {}}]},
        {'tool_calls': [{'id': 'b', 'name': 'date', 'arguments': {}}]},
        'Finished the long turn.',
    ])
    ended = []
    hooks = HookBus()
    hooks.register('turn_end', ended.append)
    with Session(config, backend=backend, hooks=hooks) as session:
        reply = session.run_turn('Keep using tools')
        assert reply == 'Finished the long turn.'
        assert ended[0]['success'] is True
        assert 'iteration limit' not in reply.lower()


def test_backend_error_is_logged_and_turn_capture_runs(tmp_path):
    hooks = HookBus()
    ended = []
    hooks.register('turn_end', ended.append)
    with Session(session_config(tmp_path), backend=FakeBackend([RuntimeError('private detail')]), hooks=hooks) as session:
        with pytest.raises(RuntimeError):
            session.run_turn('Hello')
        assert ended[0]['success'] is False
        assert 'private detail' not in str(list(session.log.iter()))
        assert session.capture.debt()


def test_unavailable_lock_backend_refuses_the_turn_before_the_model_or_a_write(tmp_path):
    """The engine's capabilities report is read before the turn does any work."""
    from integration_helpers import FakeMcpServer
    from tessera.aethermind import AetherMindClient, EMBEDDED_LOCK_REFUSAL

    runtime = {
        'runtime_id': 'aethermind-python',
        'contract_version': 'adapter-v1',
        'layer_types': ['discovery', 'decision', 'correction', 'friction'],
        'lock_backend': 'unavailable',
    }
    backend = FakeBackend(['The model must not be called.'])
    config = session_config(tmp_path)
    ended = []
    hooks = HookBus()
    hooks.register('turn_end', ended.append)
    with FakeMcpServer(dialect='path', tool_results={'capabilities': {'success': True, 'runtime': runtime}}) as server:
        client = AetherMindClient(endpoints=[server.url], timeout=1)
        with Session(config, backend=backend, hooks=hooks) as session:
            config.aethermind.url = 'https://example.invalid/mcp'
            session.uses_embedded_engine = True
            session.capture.client = client
            reply = session.run_turn('Say hello')
            names = [request.get('params', {}).get('name') for request, _ in server.requests
                     if request.get('method') == 'tools/call']
            assert EMBEDDED_LOCK_REFUSAL == (
                'embedded AetherMind needs file locking that native Windows lacks; '
                'run Tessera under WSL, or configure an external AetherMind')
            assert reply == EMBEDDED_LOCK_REFUSAL
            assert backend.calls == []
            assert names == ['capabilities']
            assert session.capture.debt() == []
            assert [message.text for message in session.log.iter() if message.kind == 'agent'] == [EMBEDDED_LOCK_REFUSAL]
            assert ended[0]['error'] == EMBEDDED_LOCK_REFUSAL
            assert ended[0]['success'] is False
            session.capture.client = None


def test_a_session_off_the_embedded_engine_is_not_refused_for_its_lock(tmp_path):
    """A configured URL is not the signal. The session's own engine is."""
    from integration_helpers import FakeMcpServer
    from tessera.aethermind import AetherMindClient, EMBEDDED_LOCK_REFUSAL

    runtime = {
        'runtime_id': 'aethermind-python',
        'contract_version': 'adapter-v1',
        'layer_types': ['discovery', 'decision', 'correction', 'friction'],
        'lock_backend': 'unavailable',
    }
    backend = FakeBackend(['External reply.'])
    config = session_config(tmp_path)
    with FakeMcpServer(dialect='path', tool_results={'capabilities': {'success': True, 'runtime': runtime}}) as server:
        client = AetherMindClient(endpoints=[server.url], timeout=1)
        with Session(config, backend=backend) as session:
            config.aethermind.url = server.url
            config.aethermind.source = 'external'
            session.memory_choice = {
                'source': 'external', 'url': server.url, 'endpoints': [],
                'reason': 'External service validated',
            }
            session.uses_embedded_engine = False
            session.capture.client = client
            reply = session.run_turn('Say hello')
            assert reply == 'External reply.'
            assert reply != EMBEDDED_LOCK_REFUSAL
            assert backend.calls
            session.capture.client = None


def test_session_redacts_configured_credentials_from_outputs_and_capture(tmp_path, monkeypatch):
    config = session_config(tmp_path)
    config.jev.api_key_env = 'SESSION_SECRET'
    monkeypatch.setenv('SESSION_SECRET', 'private-credential-value')
    backend = FakeBackend([
        {'tool_calls': [{'id': 'x', 'name': 'read_file', 'arguments': {'path': 'secret.txt'}}]},
        'Found private-credential-value',
    ])
    (tmp_path / 'secret.txt').write_text('private-credential-value')
    with Session(config, backend=backend) as session:
        assert session.run_turn('Read the file') == 'Found [REDACTED]'
        assert 'private-credential-value' not in str(session.capture.debt())
        for path in session.root.rglob('*'):
            if path.is_file():
                assert 'private-credential-value' not in path.read_text()


def test_shell_failure_reaches_capture_and_tool_context(tmp_path):
    backend = FakeBackend([
        {'tool_calls': [{'id': 'x', 'name': 'shell', 'arguments': {'command': 'exit 7'}}]},
        'The command failed.',
    ])
    with Session(session_config(tmp_path), backend=backend) as session:
        session.run_turn('Run the shell command')
        echo = next(m for m in session.log.iter() if m.kind == 'echo')
        assert echo.meta['success'] is False
        assert 'Failure recorded' in session.capture.debt()[0]['layer']['body']


def test_risky_shell_ask_can_be_approved_once(tmp_path):
    config = session_config(tmp_path)
    config.tools.approval = 'ask'
    approved = []
    def approve(name, arguments):
        approved.append(name)
        return True
    backend = FakeBackend([
        {'tool_calls': [{'id': 'x', 'name': 'shell', 'arguments': {'command': 'echo approved > permitted'}}]},
        'Approved command completed.',
    ])
    with Session(config, backend=backend, approve=approve) as session:
        session.run_turn('Run the shell command')
    assert (tmp_path / 'permitted').read_text().strip() == 'approved'
    assert approved == ['shell']


def test_session_start_shell_hook_context_is_delivered(tmp_path):
    hooks = HookBus()
    hooks.register('session_start', lambda p: {'context': 'session hook instructions'})
    backend = FakeBackend(['Hello'])
    with Session(session_config(tmp_path), backend=backend, hooks=hooks) as session:
        session.run_turn('Hello')
    assert 'session hook instructions' in backend.calls[0]['messages'][3]['content']


def test_capture_flags_advisorially_without_a_configured_judge(tmp_path):
    # A judge is off by default. An honest "I cannot finish" must not become a
    # captured failure, and no judge scoring should be recorded for a judge that
    # was never configured.
    backend = FakeBackend(['I cannot finish this request.'])
    with Session(session_config(tmp_path), backend=backend) as session:
        session.run_turn('Hello')
        reply = session.log.get(1)
        assert 'judge_score' not in reply.meta
        assert 'judge_source' not in reply.meta
        layer = session.capture.debt()[0]['layer']
        assert '?' not in layer['markers']
        assert 'Failure recorded' not in layer['body']


def test_no_judge_leaves_no_scoring_or_output_gate_on_a_turn(tmp_path):
    # The reported bug: a fresh build with no Jev endpoint still recorded a
    # heuristic judge score and an output-gate refusal on every turn, which the
    # infrastructure board reported as "Judge fell back" and "denied". With the
    # judge and session gate both off, a turn must carry no judge metadata.
    backend = FakeBackend(['Done, tests passed.'])
    with Session(session_config(tmp_path), backend=backend) as session:
        assert session.run_turn('Run tests') == 'Done, tests passed.'
        reply = session.log.get(1)
        for key in ('judge_score', 'judge_source', 'verification', 'output_gate'):
            assert key not in reply.meta, key


@pytest.mark.parametrize('missing', ['url', 'model', 'selector', 'credential'])
def test_enabled_but_unconfigured_judge_does_not_score_a_turn(tmp_path, monkeypatch, missing):
    config = session_config(tmp_path)
    config.jev.enabled = True
    config.jev.url = '' if missing == 'url' else 'https://judge.example.com/evaluate'
    config.jev.model = '' if missing == 'model' else 'test-judge'
    config.jev.api_key_env = '' if missing == 'selector' else 'JEV_TEST_KEY'
    config.jev.credential_ref = ''
    if missing == 'credential':
        monkeypatch.delenv('JEV_TEST_KEY', raising=False)
    else:
        monkeypatch.setenv('JEV_TEST_KEY', 'fixture-key')
    def unexpected(*args, **kwargs):
        raise AssertionError('An unconfigured judge must not be used')
    monkeypatch.setattr('tessera.jev.JudgeKit.verify_output', unexpected)
    monkeypatch.setattr('tessera.jev.JudgeKit.output_gate', unexpected)
    monkeypatch.setattr('tessera.jev.urllib.request.urlopen', unexpected)
    with Session(config, backend=FakeBackend(['Done.'])) as session:
        assert session.run_turn('Hello') == 'Done.'
        reply = session.log.get(1)
        for key in ('judge_score', 'judge_source', 'verification', 'output_gate'):
            assert key not in reply.meta, key


def test_memory_decision_causes_read_before_answer(tmp_path):
    from integration_helpers import FakeMcpServer
    from tessera.aethermind import AetherMindClient
    from tessera.capture import Capture
    with FakeMcpServer() as server:
        capture = Capture(AetherMindClient(server.url, timeout=1), tmp_path / 'outbox', 'work', '', 'test')
        backend = FakeBackend(['I recalled the prior decision.'])
        with Session(session_config(tmp_path), backend=backend, capture=capture) as session:
            session.run_turn('Recall the previous decision')
        calls = []
        for body, headers in server.requests:
            if body.get('method') == 'tools/call':
                calls.append(body['params'])
        # The shipped recall flow reads the store with paginated reads for this
        # project (query-based read_layers was replaced by ranked client-side
        # lookup plus inheritance/prefixed reads).
        assert any(call['name'] == 'read_layers' and call['arguments'].get('project_root') == 'work' for call in calls)
        assert 'Memory recall' in backend.calls[0]['messages'][3]['content']


def test_parallel_tool_results_precede_hook_context_messages(tmp_path):
    hooks = HookBus()
    hooks.register('pre_tool', lambda p: {'context': 'Policy hook context'})
    backend = FakeBackend([
        {'tool_calls': [{'id': 'a', 'name': 'date', 'arguments': {}}, {'id': 'b', 'name': 'date', 'arguments': {}}]},
        'Both dates returned.',
    ])
    with Session(session_config(tmp_path), backend=backend, hooks=hooks) as session:
        session.run_turn('Use tools to return dates')
    messages = backend.calls[1]['messages']
    assistant_index = next(i for i, message in enumerate(messages) if message['role'] == 'assistant')
    assert [message['role'] for message in messages[assistant_index + 1:assistant_index + 3]] == ['tool', 'tool']
    assert messages[assistant_index + 3]['role'] == 'user'


def test_explicit_zoom_request_uses_tools_despite_generic_direct_fallback(tmp_path):
    backend = FakeBackend([
        'Hello.',
        {'tool_calls': [{'id': 'z', 'name': 'zoom', 'arguments': {'start': 0, 'count': 1}}]},
        'The original message was Hello.',
    ])
    with Session(session_config(tmp_path), backend=backend) as session:
        session.run_turn('Hello')
        session.run_turn('Zoom message zero')
        echo = next(m for m in session.log.iter() if m.kind == 'echo')
        assert echo.text == 'Hello'
        assert echo.meta['success'] is True


@pytest.mark.parametrize("finish_reason", ["length", "content_filter"])
def test_incomplete_final_answer_fails_hooks_capture_and_log_then_session_recovers(tmp_path, finish_reason):
    hooks = HookBus()
    ended = []
    hooks.register("turn_end", ended.append)
    backend = FakeBackend([{"content": "Misleading complete-looking answer.", "finish_reason": finish_reason},
                           {"content": "A completed reply.", "finish_reason": "stop"}])
    with Session(session_config(tmp_path), backend=backend, hooks=hooks) as session:
        with pytest.raises(BackendError, match="incomplete answer"):
            session.run_turn("Answer this synthetic request.")
        assert session.last_turn_success is False
        assert len(ended) == 1 and ended[0]["success"] is False and ended[0]["failed"] is True
        assert ended[0]["reply"] == "" and ended[0]["error"]
        messages = list(session.log.iter())
        assert messages[-1].meta["success"] is False
        assert "Misleading complete-looking answer." not in [message.text for message in messages]
        debt = session.capture.debt()
        assert len(debt) == 1 and "?" in debt[0]["layer"]["markers"]
        assert "Failure recorded during this turn." in debt[0]["layer"]["body"]
        assert session.run_turn("Try a fresh request.") == "A completed reply."
        assert session.last_turn_success is True
        assert len(ended) == 2 and ended[1]["success"] is True
        assert len(backend.calls) == 2


def test_clipped_tool_output_is_visible_in_model_context_log_and_capture(tmp_path):
    config = session_config(tmp_path)
    config.tools.clip_chars = 64
    text = "HEAD " + "synthetic words " * 40 + " TAIL"
    (tmp_path / "synthetic.txt").write_text(text)
    backend = FakeBackend([
        {"tool_calls": [{"id": "read-placeholder", "name": "read_file",
                         "arguments": {"path": "synthetic.txt"}}]},
        "The file result was clipped.",
    ])
    with Session(config, backend=backend) as session:
        session.run_turn("Use read_file to inspect the synthetic file.")
        echo = next(message for message in session.log.iter() if message.kind == "echo")
        assert echo.meta["success"] is True
        assert len(echo.text) <= config.tools.clip_chars
        assert "[clipped]" in echo.text and echo.text.startswith("HEAD ") and echo.text.endswith(" TAIL")
        result = next(message for message in backend.calls[1]["messages"] if message["role"] == "tool")
        assert result["content"] == echo.text
        body = session.capture.debt()[0]["layer"]["body"]
        assert echo.text not in body
        assert "read_file: ok" in body


@pytest.mark.parametrize("tool", ["read_file", "zoom"])
@pytest.mark.parametrize("clip_chars", [1, 128])
def test_minimum_tool_pages_keep_notices_through_loop_and_capture(tmp_path, tool, clip_chars):
    config = session_config(tmp_path)
    config.tools.clip_chars = clip_chars
    (tmp_path / "synthetic.txt").write_text("SOURCE contents exceed one character.")
    arguments = {"path": "synthetic.txt", "limit": 1} if tool == "read_file" else {
        "start": 0, "count": 1, "limit": 1}
    backend = FakeBackend([
        {"tool_calls": [{"id": "partial-placeholder", "name": tool, "arguments": arguments}]},
        "The result was partial.",
    ])
    with Session(config, backend=backend) as session:
        session.run_turn(f"Use {tool} to inspect the synthetic input.")
        echo = next(message for message in session.log.iter() if message.kind == "echo")
        assert echo.meta["success"] is True
        if tool == "read_file" or clip_chars == 1:
            assert "\n[clipped]\n" in echo.text
        else:
            assert "Continue with zoom(0, 1, offset=1).]" in echo.text
        result = next(message for message in backend.calls[1]["messages"] if message["role"] == "tool")
        assert result["content"] == echo.text
        body = session.capture.debt()[0]["layer"]["body"]
        assert echo.text not in body
        assert f"{tool}: ok" in body


def test_session_briefs_reach_the_model_once_then_on_demand(tmp_path):
    hooks = HookBus()
    hooks.register('session_start', lambda p: {'context': 'BRIEF: heavy companion orientation'})
    backend = FakeBackend(['First.', 'Second.'])
    with Session(session_config(tmp_path), backend=backend, hooks=hooks) as session:
        run_turn(session, 'One')
        run_turn(session, 'Two')
        first, second = (call['messages'][3]['content'] for call in backend.calls)
        assert 'BRIEF: heavy companion orientation' in first
        assert 'BRIEF' not in second and 'call session_briefs' in second
        names = {schema['function']['name'] for schema in session.tools.schemas()}
        assert 'session_briefs' in names
        assert session.tools.execute('session_briefs', {})['briefs'] == ['BRIEF: heavy companion orientation']


def test_sent_prompt_names_only_tools_that_route_offers(tmp_path):
    backend = FakeBackend(['Hi.', 'Done.'])
    with Session(session_config(tmp_path), backend=backend) as session:
        session.run_turn('Hello')
        offered = {tool['function']['name'] for tool in backend.calls[0]['tools']}
        registered = {tool['function']['name'] for tool in session.tools.schemas()}
        chunks = [message['content'] for message in backend.calls[0]['messages'] if isinstance(message.get('content'), str)]
        chunks.extend(tool['function'].get('description', '') for tool in backend.calls[0]['tools'])
        text = '\n'.join(chunks)
        named = {name for name in registered if re.search(r'\b' + re.escape(name) + r'\b', text)}
        assert named <= offered
        assert 'build_tree' not in text
        session.run_turn('audit this repo')
        tools_offered = {tool['function']['name'] for tool in backend.calls[1]['tools']}
        assert 'read_file' in tools_offered and 'shell' in tools_offered


def test_checkout_line_reads_head_and_dirty_from_a_real_repo(tmp_path):
    import os
    import subprocess
    environment = dict(os.environ)
    environment.update(GIT_AUTHOR_NAME='Tessera Test', GIT_AUTHOR_EMAIL='test@example.com',
                       GIT_COMMITTER_NAME='Tessera Test', GIT_COMMITTER_EMAIL='test@example.com')
    repo = tmp_path / 'repo'
    repo.mkdir()
    subprocess.run(['git', 'init', '-q', str(repo)], check=True, capture_output=True)
    (repo / 'note.txt').write_text('hello', encoding='utf-8')
    subprocess.run(['git', '-C', str(repo), 'add', 'note.txt'], check=True, capture_output=True, env=environment)
    subprocess.run(['git', '-C', str(repo), 'commit', '-q', '-m', 'note'], check=True, capture_output=True, env=environment)
    expected = subprocess.run(['git', '-C', str(repo), 'rev-parse', 'HEAD'],
                              check=True, capture_output=True, text=True).stdout.strip()
    config = session_config(tmp_path)
    config.cwd = repo
    backend = FakeBackend(['Hi.', 'Hi.'])
    with Session(config, backend=backend) as session:
        session.run_turn('Hello')
        assert f'git HEAD {expected}' in backend.calls[0]['messages'][3]['content']
        assert 'dirty clean' in backend.calls[0]['messages'][3]['content']
        (repo / 'note.txt').write_text('changed', encoding='utf-8')
        session.run_turn('Hello again')
        assert 'dirty dirty' in backend.calls[1]['messages'][3]['content']


def test_checkout_line_is_computed_for_the_live_cwd(tmp_path):
    backend = FakeBackend(['Hi.'])
    config = session_config(tmp_path)
    with Session(config, backend=backend) as session:
        session.run_turn('Hello')
        turn = backend.calls[0]['messages'][3]['content']
        assert f'cwd {config.cwd}' in turn
        assert 'git HEAD' in turn and 'dirty' in turn
        assert 'not a git checkout' in turn or 'unavailable' in turn


def test_memory_guidance_turn_reminder_and_direct_route_memory_tools(tmp_path):
    config = session_config(tmp_path)
    config.aethermind.project = 'tessera-harness'
    config.tools.direct_route_tools = ['memory_recall', 'memory_tree']
    from tessera.config import RemoteConfig
    config.remotes['desk'] = RemoteConfig(name='desk', host='192.0.2.5', user='operator', description='office desk', desk=True)
    backend = FakeBackend(['Hi.', 'Ok.'])
    with Session(config, backend=backend) as session:
        prompt = session.system_prompt
        assert 'Your body (from config):' in prompt and '- machine desk (remote_*, desk_*): office desk' in prompt
        assert 'AetherMind' in prompt and 'spawn_agent' in prompt
        session.run_turn('Hello')
        call = backend.calls[0]
        turn = call['messages'][3]['content']
        assert 'manual lookup tool, not injected context' in session.system_prompt
        assert 'tessera-harness (this harness)' in session.system_prompt
        assert 'memory_tree' in turn and 'call memory_recall before answering' in turn
        assert 'memory_note' in turn and 'spawn_agent' in turn and 'Delegation:' in turn
        names = sorted(t['function']['name'] for t in call['tools'] or [])
        assert names == sorted(tool['function']['name'] for tool in session.tools.schemas())
        assert {'read_file', 'shell', 'write_file', 'process', 'web_fetch', 'memory_recall'} <= set(names)
        session.run_turn('Read the repo and audit the source')
        tools_turn = backend.calls[1]['messages'][3]['content']
        assert 'Delegation:' not in tools_turn and 'spawn_agent' not in tools_turn
        assert config.prompt.turn_reminder not in tools_turn
        assert 'Delegation:' in session.system_prompt
    config.prompt.memory_guidance = ''
    config.prompt.body_map = False
    config.prompt.architecture = 'Sparks serve the main model.'
    config.prompt.turn_reminder = ''
    backend = FakeBackend(['Hi.'])
    with Session(config, backend=backend) as session:
        assert 'Your body' not in session.system_prompt
        assert 'Sparks serve the main model.' in session.system_prompt
        session.run_turn('Hello')
        assert 'Memory check' not in backend.calls[0]['messages'][3]['content']


def test_later_turns_drop_the_repeated_lecture(tmp_path):
    """A long thread should not re-teach the same rules on every turn."""
    backend = FakeBackend(['Hi.', 'Still here.'])
    with Session(session_config(tmp_path), backend=backend) as session:
        session.run_turn('Hello')
        first = backend.calls[0]['messages'][3]['content']
        assert 'AetherMind' in first and 'Delegation:' in first
        session.run_turn('How does it feel?')
        later = backend.calls[1]['messages'][3]['content']
    assert 'AetherMind' not in later
    assert 'Delegation:' not in later
    assert 'prompt dump' not in later
    assert 'Checkout:' in later and 'Turn:' in later


def test_a_conscience_abstain_with_no_score_does_not_write_a_note(tmp_path):
    """Every tool call was logging "Conscience abstain: no pressure line".

    That is not a score, it is the judge never being asked. It filled the transcript of
    every session with one line per tool call and told nobody anything. A real verdict
    still records.
    """
    with Session(session_config(tmp_path), backend=FakeBackend([])) as session:
        session._note_conscience('shell', {'verdict': 'abstain', 'reason': 'no pressure line'})
        session._note_conscience('shell', {'verdict': 'abstain', 'reason': 'cloud Jev off'})
        assert [m.kind for m in session.log.iter()] == []
        session._note_conscience('shell', {'verdict': 'abstain', 'probability': 0.5})
        session._note_conscience('shell', {'verdict': 'yes', 'probability': 0.93})
        session._note_conscience('shell', {'block': True, 'reason': 'continues a previous wake'})
        notes = [m.text for m in session.log.iter() if m.kind == 'note']
        assert len(notes) == 3
        assert 'abstain' in notes[0] and '0.50' in notes[0]
        assert 'yes' in notes[1] and '0.93' in notes[1]
        assert 'no on shell' in notes[2]
