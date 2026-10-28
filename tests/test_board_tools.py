"""The actual FakeBackend tool loop drives the board independently of channels."""
import io
import json
from unittest.mock import patch

import pytest

from fake_backend import FakeBackend
from tessera.config import Config, load_config
from tessera.sessions import SessionManager


def turn_state(call):
    """The turn-state block, found by its prefix: its index moves with hook context."""
    return next(m['content'] for m in call['messages'] if str(m.get('content', '')).startswith('<turn-state>'))


def test_fake_session_creates_updates_attaches_and_asks_with_real_log_links(tmp_path):
    config = Config(home=tmp_path, cwd=tmp_path)
    config.board.enabled = True
    def create(messages, tools):
        names = [item['function']['name'] for item in tools]
        assert 'board_create_card' in names and 'send_to_session' not in names
        return {'tool_calls': [{'id': 'create', 'name': 'board_create_card', 'arguments': {'title': 'Inspect source'}}]}
    def mutate(messages, tools):
        previous = json.loads(messages[-1]['content'])
        card_id = previous['card_id']
        return {'tool_calls': [
            {'id': 'run', 'name': 'board_update_status', 'arguments': {'card_id': card_id, 'status': 'running', 'attempt': 1}},
            {'id': 'artifact', 'name': 'board_attach_artifact', 'arguments': {'card_id': card_id, 'reference': 'artifact-placeholder'}},
            {'id': 'human', 'name': 'board_ask_human', 'arguments': {'card_id': card_id, 'question': 'May I proceed?', 'required': True}},
        ]}
    backend = FakeBackend([create, mutate, 'The board records the source review and pending question.'])
    with SessionManager(config, backend_factory=lambda *args: backend) as manager:
        manager.create('one')
        manager.submit('one', 'Use the board tools to track the source review')
        assert manager.wait('one', 5)
        cards = manager.board.list()
        assert len(cards) == 1 and cards[0]['status'] == 'blocked' and cards[0]['attempt'] == 1
        assert cards[0]['artifacts'] == ['artifact-placeholder'] and cards[0]['questions'][0]['pending']
        log = manager.get('one').log
        assert 'board_ask_human' in log.get(cards[0]['log_end']).text
        assert cards[0]['session_slug'] == 'one'
        projection = manager.status()['board_status']
        assert manager.board_link(1, projection['revision'])['log_end'] == cards[0]['log_end']


def test_disabled_board_registers_no_tools_and_does_not_create_journal(tmp_path):
    config = Config(home=tmp_path, cwd=tmp_path)
    with SessionManager(config, backend_factory=lambda *args: FakeBackend([])) as manager:
        manager.create('one')
        assert 'board_create_card' not in json.dumps(manager.get('one').tools.schemas())
        assert not manager.board.path.exists()


def test_ask_human_is_offline_by_default_and_configured_batch_is_optional(tmp_path, monkeypatch):
    from tessera.jev import JudgeKit
    monkeypatch.setenv('EXAMPLE_BOARD_KEY', 'fixture-key')
    kit = JudgeKit(enabled=False)
    with patch('tessera.credentials.CredentialResolver.resolve', side_effect=AssertionError('offline')):
        assert kit.ask_human_gate('Need approval to proceed')['ask_human'] > .5
    kit = JudgeKit(enabled=True, url='https://judge.example.com/evaluate', model='judge-placeholder', api_key_env='EXAMPLE_BOARD_KEY')
    calls = []
    def respond(request, timeout):
        body = json.loads(request.data)
        calls.append(body)
        return io.BytesIO(json.dumps({'answers': {'ask_human': {'type': 'noul', 'noul': .2},
                                                 'needs_authority': {'type': 'noul', 'noul': .1}}}).encode())
    with patch('tessera.jev.urllib.request.urlopen', side_effect=respond):
        assert kit.ask_human_gate('Can this proceed?')['ask_human'] == .2
    assert len(calls) == 1 and set(calls[0]['questions']) == {'ask_human', 'needs_authority'}
    with patch.object(kit._credentials, 'resolve', side_effect=AssertionError('required first')):
        assert kit.ask_human_gate('Proceed?', required=True)['ask_human'] == 1


def test_board_settings_load_from_env_without_starting_storage(tmp_path, monkeypatch):
    monkeypatch.setenv('TESSERA_BOARD_ENABLED', 'true')
    config = load_config(tmp_path)
    assert config.board.enabled and not config.board.judge_asks
    assert not (tmp_path / 'board.jsonl').exists()
    (tmp_path / 'config.toml').write_text('[board]\ncard_limit = 100\n')
    assert not hasattr(load_config(tmp_path).board, 'card_limit')


def test_board_judge_toggle_required_boundary_and_secret_scrubbing(tmp_path, monkeypatch):
    from unittest.mock import Mock
    config = Config(home=tmp_path, cwd=tmp_path)
    config.board.enabled = True
    config.jev.api_key_env = 'EXAMPLE_BOARD_KEY'
    monkeypatch.setenv('EXAMPLE_BOARD_KEY', 'fixture-secret')
    with SessionManager(config, backend_factory=lambda *args: FakeBackend([])) as manager:
        manager.create('one')
        session = manager.get('one')
        session.log.append('note', 'Existing board evidence')
        card = manager.board.create('Inspect source', 'one', 0, 0)
        config.jev.enabled = True
        config.jev.url = 'https://judge.example.com/evaluate'
        config.jev.model = 'judge-placeholder'
        resolver = Mock(side_effect=AssertionError('Board judge is not enabled'))
        transport = Mock(side_effect=AssertionError('Board judge is not enabled'))
        with patch('tessera.credentials.CredentialResolver.resolve', resolver), patch('tessera.jev.urllib.request.urlopen', transport):
            result = session.tools.execute('board_ask_human', {'card_id': card['id'], 'question': 'Need approval fixture-secret'})
            assert result['question_pending']
        resolver.assert_not_called()
        transport.assert_not_called()
        config.board.judge_asks = True
        with patch('tessera.credentials.CredentialResolver.resolve', resolver):
            result = session.tools.execute('board_ask_human', {'card_id': card['id'], 'question': 'Explicit boundary', 'required': True})
            assert result['human_gate']['source'] == 'deterministic'
        resolver.assert_not_called()
        other = manager.board.create('Inspect another source', 'one', 0, 0)
        calls = []
        def respond(request, timeout):
            body = json.loads(request.data)
            calls.append(body)
            return io.BytesIO(json.dumps({'answers': {'ask_human': {'type': 'noul', 'noul': .2},
                                                     'needs_authority': {'type': 'noul', 'noul': .1}}}).encode())
        with patch('tessera.jev.urllib.request.urlopen', side_effect=respond):
            result = session.tools.execute('board_ask_human', {'card_id': other['id'], 'question': 'Can evidence resolve this?'})
        assert len(calls) == 1 and not result['question_pending']
        assert manager.board.get(other['id'])['questions'][0]['pending'] is False
        for path in tmp_path.rglob('*.jsonl'):
            assert 'fixture-secret' not in path.read_text()


def test_ownership_and_future_log_links_reject_before_judge_lookup(tmp_path):
    from unittest.mock import Mock
    config = Config(home=tmp_path, cwd=tmp_path)
    config.board.enabled = True
    with SessionManager(config, backend_factory=lambda *args: FakeBackend([])) as manager:
        manager.create('one')
        manager.create('two')
        session = manager.get('two')
        session.log.append('note', 'Existing board evidence')
        card = manager.board.create('Inspect', 'one', 0, 0)
        resolver = Mock(side_effect=AssertionError('Must reject before lookup'))
        with patch('tessera.credentials.CredentialResolver.resolve', resolver):
            with pytest.raises(ValueError, match='another session'):
                session.tools.execute('board_ask_human', {'card_id': card['id'], 'question': 'Proceed?'})
            with pytest.raises(ValueError, match='evidence'):
                session.tools.execute('board_create_card', {'title': 'Inspect', 'log_start': 0, 'log_end': 100})
        resolver.assert_not_called()


def test_boards_are_on_demand_tools_not_turn_context(tmp_path):
    config = Config(home=tmp_path, cwd=tmp_path)
    config.board.enabled = True
    backend = FakeBackend(['Nothing to do.'])
    with SessionManager(config, backend_factory=lambda *args: backend) as manager:
        manager.create('one')
        tools = manager.get('one').tools
        names = {item['function']['name'] for item in tools.schemas()}
        assert {'wip_board', 'infra_status'} <= names
        assert tools.execute('wip_board', {})['cards'] == []
        assert 'hosts' in tools.execute('infra_status', {}) or 'enabled' in tools.execute('infra_status', {})
        manager.submit('one', 'Hello')
        assert manager.wait('one', 5)
        state = turn_state(backend.calls[0])
        assert '"cards"' not in state and '"hosts"' not in state


def test_board_is_shared_and_an_unclaimed_card_can_be_taken_by_another_session(tmp_path):
    """One board, every session reads it, unclaimed work is pickable."""
    config = Config(home=tmp_path, cwd=tmp_path)
    config.board.enabled = True
    with SessionManager(config, backend_factory=lambda *args: FakeBackend([])) as manager:
        for name in ('one', 'two'):
            manager.create(name)
        first, second = manager.get('one'), manager.get('two')
        first.log.append('user', 'Evidence for the shared card')
        card = first.tools.execute('board_create_card', {'title': 'Fix the board', 'note': 'Read-only smoke today',
                                                         'claim': False})
        brief = second.tools.execute('wip_board', {})
        assert brief['unclaimed'] == [{'ordinal': 1, 'card_id': card['card_id'], 'title': 'Fix the board', 'status': 'queued',
                                       'age_seconds': brief['unclaimed'][0]['age_seconds'], 'note': 'Read-only smoke today',
                                       'project': ''}]
        assert brief['cards'][0]['owner'] == ''
        # A reader must be able to act on what it just read: the brief names the card id.
        assert brief['cards'][0]['card_id'] == card['card_id']
        claimed = second.tools.execute('board_claim_card', {'card_id': card['card_id']})
        assert claimed['status'] == 'queued'
        restored = manager.board.get(card['card_id'])
        assert restored['owner'] == 'two'
        assert second.tools.execute('wip_board', {})['unclaimed'] == []
        # The claimant owns progress; the author cannot move it.
        second.tools.execute('board_update_status', {'card_id': card['card_id'], 'status': 'running'})
        with pytest.raises(ValueError, match='claimed by another session'):
            first.tools.execute('board_update_status', {'card_id': card['card_id'], 'status': 'succeeded'})
        # Release returns it to the shared pool.
        second.tools.execute('board_release_card', {'card_id': card['card_id']})
        assert manager.board.get(card['card_id'])['owner'] == ''
        assert first.tools.execute('wip_board', {})['unclaimed'][0]['ordinal'] == 1


def test_board_note_keeps_plan_and_progress_readable_to_every_session(tmp_path):
    config = Config(home=tmp_path, cwd=tmp_path)
    config.board.enabled = True
    with SessionManager(config, backend_factory=lambda *args: FakeBackend([])) as manager:
        manager.create('one')
        session = manager.get('one')
        card = session.tools.execute('board_create_card', {'title': 'Board work', 'note': 'Plan: shared board'})
        session.tools.execute('board_note', {'card_id': card['card_id'], 'text': 'Finished the store claim path.'})
        session.tools.execute('board_note', {'card_id': card['card_id'], 'text': 'Left: the UI note editor.'})
        note = manager.board.get(card['card_id'])['note']
        assert note == 'Plan: shared board\nFinished the store claim path.\nLeft: the UI note editor.'
        brief = session.tools.execute('wip_board', {})
        assert brief['cards'][0]['note'] == note
        assert brief['cards'][0]['updated_by'] == 'one'
        assert brief['counts']['queued'] == 1 and brief['open'] == 1


def test_board_revise_corrects_a_title_without_moving_status(tmp_path):
    config = Config(home=tmp_path, cwd=tmp_path)
    config.board.enabled = True
    with SessionManager(config, backend_factory=lambda *args: FakeBackend([])) as manager:
        manager.create('one')
        session = manager.get('one')
        card = session.tools.execute('board_create_card', {'title': 'Smoke page', 'claim': False})
        session.tools.execute('board_claim_card', {'card_id': card['card_id']})
        session.tools.execute('board_update_status', {'card_id': card['card_id'], 'status': 'running'})
        session.tools.execute('board_revise_card', {'card_id': card['card_id'], 'title': 'Shared board'})
        restored = manager.board.get(card['card_id'])
        assert (restored['title'], restored['status'], restored['owner']) == ('Shared board', 'running', 'one')


def test_a_project_holds_the_plan_and_the_cards_filed_under_it(tmp_path):
    """One board of cards, and a project card that carries the plan.

    A project is not a second board. It is the intent and the ordered steps, with the
    cards that break the plan into work filed beneath it.
    """
    config = Config(home=tmp_path, cwd=tmp_path)
    config.board.enabled = True
    with SessionManager(config, backend_factory=lambda *args: FakeBackend([])) as manager:
        manager.create('one')
        session = manager.get('one')
        project = session.tools.execute('board_create_project', {
            'title': 'Shared board', 'description': 'One board, many threads',
            'manager': 'one'})
        assert project['status'] == 'active' and project['manager'] == 'one'
        assert project['steps_total'] == 0
        planned = session.tools.execute('board_set_steps', {
            'project_id': project['project_id'],
            'steps': [{'text': 'Store the plan'}, {'text': 'Render the plan'}, {'text': 'Prove it live'}]})
        assert planned['steps_total'] == 3 and planned['steps_done'] == 0
        view = manager.board_view()
        step_ids = [step['id'] for step in view['projects'][0]['steps']]
        session.tools.execute('board_check_step', {'project_id': project['project_id'],
                                                   'step_id': step_ids[0], 'done': True})
        card = session.tools.execute('board_create_card', {'title': 'Render the plan',
                                                           'project': project['project_id']})
        assert card['project'] == project['project_id']
        view = manager.board_view()
        row = view['projects'][0]
        assert (row['steps_done'], row['steps_total'], row['cards_total'], row['cards_open']) == (1, 3, 1, 1)
        assert row['card_counts']['queued'] == 1
        assert row['cards_settled'] == 0
        assert row['plan_ahead_of_work'] is True
        # Plan checks and settled work are exposed separately.
        assert view['cards'][0]['project'] == project['project_id']


def test_a_card_cannot_be_filed_under_a_project_that_does_not_exist(tmp_path):
    config = Config(home=tmp_path, cwd=tmp_path)
    config.board.enabled = True
    with SessionManager(config, backend_factory=lambda *args: FakeBackend([])) as manager:
        manager.create('one')
        session = manager.get('one')
        with pytest.raises(ValueError, match='project does not exist'):
            session.tools.execute('board_create_card', {'title': 'Orphan', 'project': 'missing-project'})
        card = session.tools.execute('board_create_card', {'title': 'Unfiled'})
        with pytest.raises(ValueError, match='project does not exist'):
            session.tools.execute('board_file_card', {'card_id': card['card_id'], 'project': 'missing-project'})


def test_the_board_brief_names_the_project_a_card_belongs_to(tmp_path):
    """A session picking up unclaimed work must know which project it serves."""
    config = Config(home=tmp_path, cwd=tmp_path)
    config.board.enabled = True
    with SessionManager(config, backend_factory=lambda *args: FakeBackend([])) as manager:
        manager.create('one')
        session = manager.get('one')
        project = session.tools.execute('board_create_project', {'title': 'Swarm prep'})
        session.tools.execute('board_create_card', {'title': 'Split the work orders', 'claim': False,
                                                    'project': project['project_id']})
        brief = session.tools.execute('wip_board', {})
        assert brief['unclaimed'][0]['project'] == project['project_id']
        assert brief['cards'][0]['project'] == project['project_id']


def test_the_board_brief_names_the_cards_that_still_need_filing(tmp_path):
    """The model determines the appropriate board and assigns. A person can't
    create boards or projects, so this is a model job."

    The pressure line can only say "one card is unfiled". A session needs to know
    WHICH card, because filing it is the session's job and not the person's.
    """
    config = Config(home=tmp_path, cwd=tmp_path)
    config.board.enabled = True
    with SessionManager(config, backend_factory=lambda *args: FakeBackend([])) as manager:
        manager.create('one')
        session = manager.get('one')
        project = session.tools.execute('board_create_project', {'title': 'Swarm prep'})
        session.tools.execute('board_create_card', {'title': 'Filed card', 'project': project['project_id']})
        session.tools.execute('board_create_card', {'title': 'Unfiled card'})
        brief = session.tools.execute('wip_board', {})
        titles = [row['title'] for row in brief['unfiled']]
        assert titles == ['Unfiled card']
        assert brief['unfiled'][0]['ordinal'] == 2


def test_a_session_is_told_to_start_a_card_it_is_working(tmp_path):
    """The board has a running state, and a session has to be told to enter it.

    The store method, the POST route, the tool and the in-flight bucket all
    exist. The line a session reads is what makes status match the work.

    This pins that line where a session actually reads it, through the real
    manager, and pins the gating too: the line rides the tool it names, so a
    route that is not offered board writes is not told to call one.
    """
    config = Config(home=tmp_path, cwd=tmp_path)
    config.board.enabled = True
    config.jev.enabled = False
    with SessionManager(config, backend_factory=lambda *args: FakeBackend([])) as manager:
        manager.create('one')
        session = manager.get('one')
        offered = {item['function']['name'] for item in session.tools.schemas()}
        assert 'board_start_card' in offered
        prompt = session.system_prompt
        assert 'board_start_card' in prompt
        assert 'transition from claimed to running' in prompt
        assert 'in-flight work' in prompt
        # The exit half. Entering running without a stated way out is what left a
        # card reading in-flight with nobody on it, so both exits are named here:
        # settling the card, and releasing it when handing off instead of finishing.
        assert 'board_update_status' in prompt
        assert 'how a card leaves running' in prompt
        assert 'board_release_card' in prompt
        assert 'returns a running card to the open pool' in prompt


def test_the_start_line_is_absent_when_the_tool_is_not_offered(tmp_path):
    """Gating: guidance names only tools this route offers."""
    config = Config(home=tmp_path, cwd=tmp_path)
    config.board.enabled = False
    config.jev.enabled = False
    with SessionManager(config, backend_factory=lambda *args: FakeBackend([])) as manager:
        manager.create('one')
        session = manager.get('one')
        assert 'board_start_card' not in {item['function']['name'] for item in session.tools.schemas()}
        assert 'board_start_card' not in session.system_prompt


def test_a_session_records_an_acceptance_through_the_tool(tmp_path):
    """The gate has to be reachable from a session, not only from the store."""
    config = Config(home=tmp_path, cwd=tmp_path)
    config.board.enabled = True
    config.board.acceptance_required = True
    with SessionManager(config, backend_factory=lambda *args: FakeBackend([])) as manager:
        manager.create('one')
        first = manager.get('one')
        card = first.tools.execute('board_create_card', {'title': 'Gated work', 'claim': True})
        # The move into succeeded is refused by the gate.
        with pytest.raises(ValueError, match='needs acceptance'):
            first.tools.execute('board_update_status', {'card_id': card['card_id'], 'status': 'succeeded'})
        # The guidance names the tool a session needs to clear it.
        assert 'board_accept_card' in first.system_prompt
        first.tools.execute('board_accept_card',
                            {'card_id': card['card_id'], 'evidence': 'suite green',
                             'note': 'read the diff'})
        # The tool result is the compact card projection, so read the acceptance where it
        # is published: the board view, which is what a reader sees.
        row = [c for c in first.tools.execute('wip_board', {})['cards']
               if c['card_id'] == card['card_id']][0]
        assert row['acceptances'][0]['by'] == 'one'
        assert row['acceptances'][0]['evidence'] == 'suite green'
        # Now the move is allowed and the acceptance stays on the card.
        moved = first.tools.execute('board_update_status', {'card_id': card['card_id'], 'status': 'succeeded'})
        assert moved['status'] == 'succeeded'
        row = [c for c in first.tools.execute('wip_board', {})['cards']
               if c['card_id'] == card['card_id']][0]
        assert row['status'] == 'succeeded'
        assert row['acceptances'][0]['by'] == 'one'
