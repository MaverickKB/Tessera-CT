"""Live-path shakedown: workspace, audit routing, capture, and brief clipping."""

import json
import subprocess

from fake_backend import FakeBackend
from tessera.capture import Capture
from tessera.companions import _text
from tessera.config import Config
from tessera.jev import JevRouter
from tessera.loop import Session
from tessera.sessions import SessionManager


def turn_state(call):
    """The turn-state block, found by its prefix: its index moves with hook context."""
    return next(m['content'] for m in call['messages'] if str(m.get('content', '')).startswith('<turn-state>'))


def session_config(tmp_path):
    config = Config(home=tmp_path, cwd=tmp_path)
    config.tools.approval = 'never'
    config.jev.enabled = False
    return config


def git_repo(path):
    environment = dict(**{key: value for key, value in __import__('os').environ.items()})
    environment.update(GIT_AUTHOR_NAME='Tessera Test', GIT_AUTHOR_EMAIL='test@example.com',
                       GIT_COMMITTER_NAME='Tessera Test', GIT_COMMITTER_EMAIL='test@example.com')
    path.mkdir()
    subprocess.run(['git', 'init', '-q', str(path)], check=True, capture_output=True)
    (path / 'note.txt').write_text('from-the-checkout', encoding='utf-8')
    subprocess.run(['git', '-C', str(path), 'add', 'note.txt'], check=True, capture_output=True, env=environment)
    subprocess.run(['git', '-C', str(path), 'commit', '-q', '-m', 'note'], check=True, capture_output=True, env=environment)
    head = subprocess.run(['git', '-C', str(path), 'rev-parse', 'HEAD'],
                          check=True, capture_output=True, text=True).stdout.strip()
    return head


def test_shakedown_and_change_requests_use_the_tools_route():
    router = JevRouter(enabled=False)
    for message in (
        'shakedown of this checkout and a list of changes worth making',
        'What needs to be changed?',
    ):
        decision = router.intake(message)
        assert decision['route'] == 'tools', message
        assert decision['source'] == 'heuristic'
        assert decision['needs_memory'] < .5
        assert decision['probabilities']['tools'] > decision['probabilities']['direct']
    assert router.intake('hello')['route'] == 'direct'


def test_audit_route_offers_read_list_and_grep(tmp_path):
    backend = FakeBackend(['Noted.', 'Hello.'])
    config = session_config(tmp_path)
    with Session(config, backend=backend) as session:
        session.run_turn('shakedown of this checkout and a list of changes worth making')
        offered = {tool['function']['name'] for tool in backend.calls[0]['tools']}
        assert {'read_file', 'list_dir', 'grep', 'shell'} <= offered
        session.run_turn('hello')
        direct = {tool['function']['name'] for tool in backend.calls[1]['tools'] or []}
        assert {'read_file', 'list_dir', 'grep', 'git_read', 'shell', 'write_file'} <= direct


def test_session_workspace_is_the_checkout_and_not_the_memory_project(tmp_path):
    home = tmp_path / 'home'
    home.mkdir()
    repo = tmp_path / 'checkout'
    head = git_repo(repo)
    config = session_config(home)
    config.aethermind.project = '/configured/memory-project'
    config.workspace = str(repo)
    backend = FakeBackend(['Hi.', {'tool_calls': [{'id': 'r', 'name': 'read_file', 'arguments': {'path': 'note.txt'}}]}, 'Read it.'])
    with Session(config, backend=backend) as session:
        session.run_turn('hello')
        turn = turn_state(backend.calls[0])
        assert f'cwd {repo.resolve()}' in turn
        assert f'git HEAD {head}' in turn
        assert '/configured/memory-project' not in turn.split('Checkout:', 1)[1].split('\n', 1)[0]
        assert config.aethermind.project == '/configured/memory-project'
        session.run_turn('read the note')
        echo = [message.text for message in session.log.iter() if message.kind == 'echo'][-1]
        assert 'from-the-checkout' in echo


def test_per_session_workspace_overrides_the_default(tmp_path):
    home = tmp_path / 'home'
    home.mkdir()
    default = tmp_path / 'default-repo'
    chosen = tmp_path / 'chosen-repo'
    git_repo(default)
    git_repo(chosen)
    (chosen / 'only-here.txt').write_text('chosen-workspace', encoding='utf-8')
    config = session_config(home)
    config.workspace = str(default)
    manager = SessionManager(config, backend_factory=lambda slug, name: FakeBackend(['Hi.']))
    try:
        created = manager.create('Audit', workspace=str(chosen))
        assert created['workspace'] == str(chosen.resolve())
        session = manager.get(created['slug'])
        line = session._checkout_line()
        assert str(chosen.resolve()) in line
        assert 'not a git checkout' not in line
        assert session.tools.execute('read_file', {'path': 'only-here.txt'}) == 'chosen-workspace'
        updated = manager.update(created['slug'], workspace=str(default))
        assert updated['workspace'] == str(default.resolve())
        assert str(default.resolve()) in session._checkout_line()
        listed = session.tools.execute('list_dir', {'path': '.'})
        assert 'note.txt' in listed
        assert 'only-here.txt' not in listed
    finally:
        manager.close()


def test_unset_workspace_stays_on_the_configured_cwd(tmp_path):
    config = session_config(tmp_path)
    assert config.workspace == ''
    backend = FakeBackend(['Hi.'])
    with Session(config, backend=backend) as session:
        session.run_turn('hello')
        assert f'cwd {config.cwd}' in turn_state(backend.calls[0])


def test_capture_stores_a_decision_not_the_turn(tmp_path):
    capture = Capture(None, tmp_path, 'work', '', 'tester')
    capture.session_start({'session_id': 'session-one'})
    capture.turn_start({'message': 'Turn 1 of the shakedown. Read note.txt and tell me the args.'})
    capture.pre_tool({'name': 'read_file', 'id': 'r', 'arguments': {'path': 'note.txt', 'limit': 20}})
    capture.post_tool({'name': 'read_file', 'id': 'r', 'result': 'from-the-checkout', 'success': True})
    capture.turn_end({
        'reply': 'I called read_file with arguments {"path": "note.txt", "limit": 20}. The note says keep the decision.',
        'success': True,
    })
    body = capture.debt()[-1]['layer']['body']
    assert body.startswith('Decision:')
    assert 'Turn 1' not in body
    assert 'User:' not in body
    assert 'note.txt' not in body or 'keep the decision' in body
    assert '{"path"' not in body
    assert 'limit' not in body
    assert 'keep the decision' in body
    assert 'read_file: ok' in body
    assert 'from-the-checkout' not in body


def test_recall_omits_this_sessions_captures(tmp_path):
    class Client:
        def read_layers(self, project, query=None, last_n=10, **kwargs):
            return {'success': True, 'layers': [
                {'id': '0100', 'body': 'The workspace is chosen per session.', 'author': 'alex', 'thread_key': 'older'},
                {'id': '0162', 'body': 'Turn 1\nUser: shakedown\narguments {"path": "a"}', 'author': 'tessera',
                 'type': 'friction', 'thread_key': 'session-one'},
                {'id': '0163', 'body': 'Turn 2\nUser: what needs to change', 'author': 'tessera',
                 'type': 'discovery', 'thread_key': 'other-session'},
            ]}

    capture = Capture(Client(), tmp_path, 'work', '', 'tester')
    capture.session_start({'session_id': 'session-one'})
    result = capture.memory_recall(query='workspace')
    ids = [layer['id'] for layer in result['layers']]
    assert ids == ['0100', '0163']
    assert result['omitted_session_captures'] == 1
    assert 'arguments' not in json.dumps(result['layers'])


def test_injected_text_clips_on_record_boundaries_and_drops_inactive_ids(tmp_path):
    records = [f'Record {index} stays whole.' for index in range(6)]
    records.insert(2, 'aa11 thread-replaced bb22 thread-replaced cc33')
    records.append('Ember decision Cutover stays if the budget holds the record.')
    text = '\n\n'.join(records)
    shown = _text({'content': [{'type': 'text', 'text': text}]}, 80, page_with='session_briefs')
    assert 'Cutove' not in shown
    assert 'more:' in shown and 'page with session_briefs' in shown
    assert 'thread-replaced' not in shown
    assert not any(token.endswith('Reco') for token in shown.split())

    from tessera.tools import clip_records, drop_inactive_chains
    inherited = 'Keep this decision.\n' + 'id1 thread-replaced id2 superseded id3 inactive\n' + 'Another decision that does not fit in the page.'
    cleaned = drop_inactive_chains(inherited)
    assert 'thread-replaced' not in cleaned
    assert 'Keep this decision.' in cleaned and 'Another decision that does not fit in the page.' in cleaned
    clipped = clip_records(cleaned, 64, 'memory_recall')
    assert 'more:' in clipped and 'page with memory_recall' in clipped
    assert 'Keep this decision.' in clipped
    assert 'Another decision' not in clipped
    partial = clip_records('Cutover is the word that must stay whole.', 24, 'session_briefs')
    assert partial.split()[0] == 'Cutover'


def test_direct_route_offers_read_tools_and_not_shell(tmp_path):
    """A normal message stays direct, and the read tools on that route are the same schemas as the full registry."""
    backend = FakeBackend(['Hello.', 'Done.'])
    config = session_config(tmp_path)
    with Session(config, backend=backend) as session:
        assert session.router.intake('How is the afternoon going?')['route'] == 'direct'
        session.run_turn('How is the afternoon going?')
        direct = {tool['function']['name']: tool for tool in backend.calls[0]['tools']}
        full = {tool['function']['name']: tool for tool in session.tools.schemas()}
        assert set(direct) == set(full)
        # Host tools are not in this list: which computer_* names exist depends on
        # the backend the selector picks for this host (native on macOS captures one
        # app's window inside get_app_state; the linux and mac backends register
        # computer_screenshot). Naming one here asserts a host, not a behaviour.
        for name in ('read_file', 'list_dir', 'grep', 'git_read', 'shell', 'write_file', 'edit_file', 'process', 'web_fetch'):
            assert name in direct
            assert direct[name]['function']['description'] == full[name]['function']['description']
            assert direct[name]['function']['parameters'] == full[name]['function']['parameters']
        # Whatever computer tools this host does have must reach the direct route with
        # the same schema, so the assertion stays real on every host.
        computer = [name for name in full if name.startswith('computer_')]
        assert computer, 'the registry must offer computer use on some backend'
        for name in computer:
            assert name in direct
            assert direct[name]['function']['description'] == full[name]['function']['description']
            assert direct[name]['function']['parameters'] == full[name]['function']['parameters']
        session.run_turn('audit this repo')
        tools = {tool['function']['name']: tool for tool in backend.calls[1]['tools']}
        assert set(tools) == set(full)


def test_git_read_reports_status_log_diff_and_head(tmp_path):
    repo = tmp_path / 'checkout'
    head = git_repo(repo)
    config = session_config(tmp_path)
    config.workspace = str(repo)
    backend = FakeBackend([])
    with Session(config, backend=backend) as session:
        assert head in session.tools.execute('git_read', {'action': 'head'})
        status = session.tools.execute('git_read', {'action': 'status'})
        assert 'note.txt' not in status
        log = session.tools.execute('git_read', {'action': 'log', 'limit': 5})
        assert 'note' in log
        (repo / 'note.txt').write_text('changed', encoding='utf-8')
        diff = session.tools.execute('git_read', {'action': 'diff'})
        assert 'changed' in diff or 'note.txt' in diff
        dirty = session.tools.execute('git_read', {'action': 'status'})
        assert 'note.txt' in dirty
        try:
            session.tools.execute('git_read', {'action': 'push'})
        except ValueError:
            pass
        else:
            raise AssertionError('git_read accepted an action that changes the checkout')


def test_inheritance_drops_inactive_id_chains_and_labels_heads(tmp_path):
    from tessera.tools import drop_inactive_chains

    chain = ' '.join(f'{index:04x}ecfe thread-replaced' for index in range(40))
    assert 'thread-replaced' not in drop_inactive_chains(chain)
    assert 'ecfe' not in drop_inactive_chains(chain)

    class Client:
        def prepare_inheritance(self, *args):
            return {'envelope': {
                'native': {'kept': [{
                    'id': '0048', 'type': 'decision', 'author': 'operator', 'ctx': 'checkout',
                    'body': 'Keep the workspace on the session.',
                    'supersedes': ['0001', '0002'],
                }]},
                'currentness': {
                    'active_ids': [f'{index:08x}-aaaa-bbbb-cccc-dddddddddddd' for index in range(40)],
                    'inactive': {'0001': 'thread-replaced', '0002': 'superseded'},
                },
            }}

        def tree_view(self, *args):
            return 'A personal cover.'

    capture = Capture(Client(), tmp_path, 'work', 'personal', 'tester', inheritance_budget=800)
    block = capture.inheritance_block()
    assert 'Keep the workspace on the session.' in block
    assert '0048' in block and 'decision' in block
    assert 'thread-replaced' not in block
    assert '0001' not in block and '0002' not in block
    assert 'aaaa-bbbb' not in block
    assert 'more: 1, page with memory_recall' not in block


def test_turn_tells_the_model_not_to_mine_prompt_dumps(tmp_path):
    """A clipped task is already in the chat. Do not send the model after prompt dumps."""
    backend = FakeBackend(['Noted.'])
    with Session(session_config(tmp_path), backend=backend) as session:
        session.run_turn('Ok. You should be able to inspect the repo now.')
        turn = turn_state(backend.calls[0])
    assert 'prompt dump' in turn
    assert '.grok/sessions' in turn
    assert 'already in the chat' in turn
