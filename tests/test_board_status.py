"""Read-only board snapshots and ordinal navigation never open execution state."""
import http.client
import json
import threading
from unittest.mock import Mock

import pytest

from tessera.config import Config
from tessera.server import create_server
from tessera.sessions import SessionManager
from test_server import call


def test_spark_serving_plan_checks_do_not_claim_delivered_work():
    from tessera.board_status import project_rows

    project = {'id': 'spark-serving', 'status': 'active', 'updated_at': 1,
               'steps': [{'done': index < 4} for index in range(5)]}
    cards = [{'project': 'spark-serving', 'status': status, 'claimable': False}
             for status in ('queued', 'running', 'blocked')]
    row, = project_rows([project], cards)
    assert (row['steps_done'], row['steps_total']) == (4, 5)
    assert (row['cards_settled'], row['cards_total'], row['cards_open']) == (0, 3, 3)
    assert row['plan_ahead_of_work'] is True


@pytest.mark.parametrize('done,total,statuses,settled,ahead', [
    (1, 2, ['succeeded', 'queued'], 1, False),
    (1, 2, ['succeeded', 'failed', 'cancelled', 'blocked'], 3, False),
    (2, 2, ['succeeded', 'queued'], 1, True),
    (1, 1, [], 0, True),
    (0, 0, [], 0, False),
    (0, 0, ['blocked'], 0, False),
])
def test_project_settlement_and_plan_ahead_ratio(done, total, statuses, settled, ahead):
    from tessera.board_status import project_rows

    project = {'id': 'p', 'status': 'active', 'updated_at': 1,
               'steps': [{'done': index < done} for index in range(total)]}
    cards = [{'project': 'p', 'status': status, 'claimable': False}
             for status in statuses]
    row, = project_rows([project], cards)
    assert row['cards_settled'] == settled
    assert row['cards_total'] == len(statuses)
    assert row['plan_ahead_of_work'] is ahead


def configuration(tmp_path):
    config = Config(home=tmp_path, cwd=tmp_path)
    config.board.enabled = True
    return config


def test_passive_board_snapshot_is_bounded_private_and_links_exact_revision(tmp_path, monkeypatch):
    config = configuration(tmp_path)
    with SessionManager(config) as manager:
        manager.create('private-subject')
        card = manager.board.create('private-title', 'private-subject', 0, 2, card_id='private-card')
        manager.board.attach_artifact(card['id'], '/private-artifact-placeholder')
        manager.board.ask_human(card['id'], 'private-question')
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in tmp_path.rglob('*') if p.is_file()}
        tripwires = []
        # The passive reader must not reach into the board store at all. The method names
        # here are the store's own: the journal was split into cards and projects, so a
        # tripwire on the old single _load would silently stop guarding anything.
        for target in ('tessera.board.BoardStore._load_cards', 'tessera.board.BoardStore._load_projects',
                       'tessera.board.BoardStore.get',
                       'tessera.credentials.CredentialResolver.resolve', 'subprocess.Popen',
                       'urllib.request.OpenerDirector.open'):
            tripwire = Mock(side_effect=AssertionError('Passive board cannot execute'))
            monkeypatch.setattr(target, tripwire)
            tripwires.append(tripwire)
        first = manager.status()['board_status']
        assert first['enabled'] and first['configured'] and len(first['revision']) == 64
        assert first['cards'] == [{'ordinal': 1, 'status': 'blocked', 'attempt': 0,
                                   'artifact_count': 1, 'question_pending': True,
                                   'session_ordinal': 1, 'log_start': 0, 'log_end': 2}]
        assert first == manager.status()['board_status']
        assert 'private-' not in json.dumps(first)
        assert manager.board_link(1, first['revision']) == {'session_slug': 'private-subject', 'log_start': 0, 'log_end': 2}
        assert manager._runtimes == {}
        assert all((p.read_bytes(), p.stat().st_mtime_ns) == data for p, data in before.items())
        for tripwire in tripwires:
            tripwire.assert_not_called()


def test_board_http_etag_and_stale_ordinal_rejection(tmp_path):
    config = configuration(tmp_path)
    config.server.port = 0
    server = create_server(config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        server.manager.create('one')
        card = server.manager.board.create('Inspect', 'one', 0, 0)
        code, first = call(server, 'GET', '/api/board')
        assert code == 200 and first == server.manager.status()['board_status']
        connection = http.client.HTTPConnection(*server.server_address, timeout=5)
        connection.request('GET', '/api/board', headers={'If-None-Match': '"' + first['revision'] + '"'})
        response = connection.getresponse()
        assert response.status == 304 and response.read() == b''
        assert response.getheader('ETag') == '"' + first['revision'] + '"'
        connection.close()
        route = '/api/board/cards/1/link?revision=' + first['revision']
        assert call(server, 'GET', route) == (200, {'session_slug': 'one', 'log_start': 0, 'log_end': 0})
        server.manager.board.update_status(card['id'], 'running', attempt=1)
        assert call(server, 'GET', route)[0] == 409
        second = call(server, 'GET', '/api/board')[1]
        assert second['revision'] != first['revision'] and second['cards'][0]['attempt'] == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_disabled_corrupt_and_torn_board_snapshots_never_repair(tmp_path):
    config = Config(home=tmp_path)
    with SessionManager(config) as manager:
        assert manager.status()['board_status'] == {'enabled': False, 'configured': True}
        assert not (tmp_path / 'board.jsonl').exists()
    config.board.enabled = True
    with SessionManager(config) as manager:
        manager.create('one')
        manager.board.create('Inspect', 'one', 0, 1)
        with manager.board.path.open('ab') as stream:
            stream.write(b'{torn')
        before = manager.board.path.read_bytes()
        assert len(manager.status()['board_status']['cards']) == 1
        assert manager.board.path.read_bytes() == before and not list(tmp_path.glob('*.incomplete-*'))
        with manager.board.path.open('ab') as stream:
            stream.write(b'\n')
        result = manager.status()['board_status']
        assert 'cards' not in result and 'revision' not in result
        # The torn tail became a committed line that is not JSON. Naming it is the point:
        # this state never self-heals, because repair only heals an uncommitted tail.
        assert 'unreadable' in result and 'not valid JSON' in result['unreadable']


def test_full_projection_and_session_map_changes_invalidate_ordinal_links(tmp_path):
    config = configuration(tmp_path)
    with SessionManager(config) as manager:
        manager.create('one')
        first = manager.board.create('Inspect A', 'one', 0, 0)
        snapshot = manager.board_status()
        manager.board.create('Inspect B', 'one', 2, 3)
        newest = manager.board_status()
        assert len(newest['cards']) == 2
        assert newest['cards'][0]['ordinal'] == 1 and newest['cards'][0]['log_start'] == 0
        assert newest['cards'][1]['ordinal'] == 2 and newest['cards'][1]['log_start'] == 2
        assert 'cards_omitted' not in newest and 'card_limit' not in newest
        with pytest.raises(ValueError, match='changed'):
            manager.board_link(1, snapshot['revision'])
        manager.create('two')
        with pytest.raises(ValueError, match='changed'):
            manager.board_link(1, newest['revision'])


def test_over_limit_and_symlinked_board_remain_unknown_without_execution(tmp_path):
    config = configuration(tmp_path)
    with SessionManager(config) as manager:
        manager.create('one')
        manager.board.create('Inspect', 'one', 0, 0)
        size = manager.board.path.stat().st_size
        config.status.max_file_bytes = size - 1
        # Blank is not the same fact as empty. The reader says which wall it hit, so a
        # person reading a blank board learns it is unreadable and by how much.
        report = manager.board_status()
        assert report['enabled'] is True and report['configured'] is True
        assert 'cards' not in report and 'revision' not in report
        assert 'unreadable' in report and str(size) in report['unreadable']
        assert str(config.status.max_file_bytes) in report['unreadable']
        config.status.max_file_bytes = 4194304
        external = tmp_path / 'other-fixture'
        manager.board.path.rename(external)
        manager.board.path.symlink_to(external)
        before = external.read_bytes()
        report = manager.board_status()
        assert report['enabled'] is True and report['configured'] is True
        assert 'cards' not in report and 'revision' not in report
        assert 'unreadable' in report and 'symlink' in report['unreadable']
        assert external.read_bytes() == before


def test_board_view_shows_titles_and_move_keeps_the_passive_projection_private(tmp_path):
    config = configuration(tmp_path)
    config.server.port = 0
    server = create_server(config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        server.manager.create('one')
        card = server.manager.board.create('Watch the core', 'one', 0, 1)
        server.manager.board.attach_artifact(card['id'], '/private-artifact-placeholder')
        server.manager.board.ask_human(card['id'], 'private-question')
        passive = call(server, 'GET', '/api/board')[1]
        view = call(server, 'GET', '/api/board/view')[1]
        assert passive['cards'][0].keys() == {
            'ordinal', 'status', 'attempt', 'artifact_count', 'question_pending',
            'session_ordinal', 'log_start', 'log_end'}
        assert 'private-question' not in json.dumps(passive) and 'private-artifact' not in json.dumps(passive)
        assert view['revision'] == passive['revision']
        assert view['cards'][0]['title'] == 'Watch the core'
        assert view['cards'][0]['status'] == 'blocked'
        assert isinstance(view['cards'][0]['updated_at'], (int, float))
        # The operator view is where a person reads and answers a card, so the question it
        # asked has to be in it. What must never appear there is a file path: artifacts are
        # counts on both surfaces, and the passive projection carries no private text at all.
        assert 'private-artifact' not in json.dumps(view)
        assert view['cards'][0]['questions'][0]['text'] == 'private-question'
        assert view['cards'][0]['questions'][0]['pending'] is True
        assert 'id' not in view['cards'][0]
        stale = call(server, 'POST', '/api/board/cards/1/status', {'revision': 'stale', 'status': 'running'})
        assert stale[0] == 409 and server.manager.board_status()['cards'][0]['status'] == 'blocked'
        moved = call(server, 'POST', '/api/board/cards/1/status', {'revision': view['revision'], 'status': 'running'})
        assert moved[0] == 200 and moved[1]['cards'][0]['status'] == 'running'
        assert moved[1]['cards'][0]['title'] == 'Watch the core'
        assert call(server, 'GET', '/api/board')[1]['cards'][0]['status'] == 'running'
        assert 'Watch the core' not in json.dumps(call(server, 'GET', '/api/status')[1]['board_status'])
        assert call(server, 'POST', '/api/board/cards/1/status', {'revision': moved[1]['revision'], 'status': 'elsewhere'})[0] == 400
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_board_ask_human_decisions_normalize_to_finite_judge_category(tmp_path):
    from tessera.jev import JudgeKit
    config = configuration(tmp_path)
    with SessionManager(config) as manager:
        manager.create('one')
        kit = JudgeKit(enabled=False, decision_log=manager.path('one') / 'decisions.jsonl')
        gate = kit.ask_human_gate('Need approval to proceed')
        assert gate['needs_authority'] > .9
        assert manager.status()['jev']['last_decision_category'] == 'judge'


def test_project_rows_name_the_sessions_that_touched_the_work():
    """A project surface shows its own history, derived from what the board records.

    The creator of the project, its manager, each filed card's creator and each card's
    last writer all count. This is the evidence floor, so the test pins that it does
    not silently claim a session it never saw.
    """
    from tessera.board_status import project_rows

    project = {'id': 'p1', 'status': 'active', 'updated_at': 5, 'session_slug': 'founder',
               'manager': 'manager-now', 'steps': []}
    cards = [
        {'project': 'p1', 'status': 'queued', 'claimable': True, 'session_slug': 'founder',
         'updated_by': 'founder', 'updated_at': 3},
        {'project': 'p1', 'status': 'succeeded', 'claimable': False, 'session_slug': 'founder',
         'updated_by': 'helper', 'updated_at': 9},
        {'project': 'other', 'status': 'queued', 'claimable': True, 'session_slug': 'outsider',
         'updated_by': 'outsider', 'updated_at': 99},
    ]
    row, = project_rows([project], cards)
    named = {entry['slug']: entry for entry in row['sessions']}
    assert set(named) == {'founder', 'manager-now', 'helper'}
    assert 'outsider' not in named, 'a card filed under another project must not appear'
    assert named['founder']['cards'] == 2
    assert named['manager-now']['cards'] == 0
    # Ordered by most recent touch, so the page reads like history rather than a set.
    # founder and helper both last touched a card stamped 9, so the slug breaks the tie.
    assert [entry['slug'] for entry in row['sessions']] == ['founder', 'helper', 'manager-now']
    assert [entry['last_at'] for entry in row['sessions']] == [9, 9, 0]


def test_board_view_carries_the_session_that_wrote_each_card(tmp_path):
    """The operator view must name a card's creator, or a project cannot show history."""
    config = configuration(tmp_path)
    with SessionManager(config) as manager:
        manager.create('author')
        manager.board.create_project('A project', 'author', project_id='p1')
        manager.board.create('A card', 'author', 0, 1, project='p1')
        view = manager.board_view()
        assert view['cards'][0]['session_slug'] == 'author'
        project, = [row for row in view['projects'] if row['id'] == 'p1']
        assert project['sessions'][0]['slug'] == 'author'


def test_one_corrupt_committed_line_names_the_board_as_unreadable_not_empty(tmp_path):
    """A healthy board with one bad committed line must not read as "no cards".

    A committed line that is JSON but not an object is reported as unreadable,
    with the record at fault. The view must not look like an empty board.

    This state also never self-heals: `repair=True` heals an uncommitted torn tail,
    but a corrupt line that ends with a newline is committed and stays corrupt. So the
    message has to be good enough to act on.
    """
    config = configuration(tmp_path)
    with SessionManager(config) as manager:
        manager.create('one')
        manager.board.create('Card one', 'one', 0, 1)
        manager.board.create('Card two', 'one', 0, 2)
        healthy = manager.board_view()
        assert len(healthy['cards']) == 2

        # Insert a committed record in the middle. Valid JSON, not an object: the
        # passive reader refuses it before replay ever sees it.
        lines = manager.board.path.read_text().splitlines(keepends=True)
        lines.insert(1, '5\n')
        manager.board.path.write_text(''.join(lines))

        broken = manager.board_view()
        assert broken['enabled'] is True and broken['configured'] is True
        assert 'cards' not in broken and 'revision' not in broken
        assert broken['unreadable'] == 'the board journal line 2 is JSON but not an object'

        # The file is untouched: naming a fault is a read, not a repair.
        assert manager.board.path.read_text() == ''.join(lines)


def test_a_record_that_fails_replay_is_named_by_sequence(tmp_path):
    """Replay is a version-sequenced fold, so a bad record cannot be skipped.

    Skipping would invent a card state that never existed — the version fence is how
    every writer proves it is acting on the record it read. So the refusal stands, and
    it names the record number instead of saying only "invalid board journal".
    """
    config = configuration(tmp_path)
    with SessionManager(config) as manager:
        manager.create('one')
        card = manager.board.create('Card one', 'one', 0, 1)
        lines = manager.board.path.read_text().splitlines(keepends=True)
        # A well-formed record whose sequence jumps: replay refuses it, and the old
        # message ("Invalid board journal sequence") did not say where.
        record = json.loads(lines[0])
        record['sequence'] = 7
        lines.append(json.dumps(record) + '\n')
        manager.board.path.write_text(''.join(lines))

        broken = manager.board_view()
        assert 'cards' not in broken
        assert broken['unreadable'] == 'Invalid board journal sequence at record 2'


def test_acceptance_gate_end_to_end_through_the_http_route(tmp_path):
    """The gate must be reachable and refuse correctly on the real server, not just the store.

    A card is not accepted as work until cleared, acceptance is durable
    history, and the gate is off by default.
    """
    config = configuration(tmp_path)
    config.server.port = 0
    config.board.acceptance_required = True
    server = create_server(config)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        server.manager.create('one')
        card = server.manager.board.create('Gated work', 'one', 0, 1)
        view = call(server, 'GET', '/api/board/view')[1]
        ordinal = view['cards'][0]['ordinal']

        # 1. The move into succeeded is refused, and refused as a conflict, not a 500.
        code, body = call(server, 'POST', f'/api/board/cards/{ordinal}/status',
                          {'revision': view['revision'], 'status': 'succeeded'})
        assert code == 409, (code, body)
        assert 'needs acceptance' in body['error']
        assert call(server, 'GET', '/api/board/view')[1]['cards'][0]['status'] == 'queued'

        # 2. An unknown status is still a bad request, not a conflict.
        code, _ = call(server, 'POST', f'/api/board/cards/{ordinal}/status',
                       {'revision': view['revision'], 'status': 'elsewhere'})
        assert code == 400

        # 3. Record the acceptance through the route.
        view = call(server, 'GET', '/api/board/view')[1]
        code, body = call(server, 'POST', f'/api/board/cards/{ordinal}/accept',
                          {'revision': view['revision'], 'by': 'one',
                           'evidence': 'tests green', 'note': 'read the diff'})
        assert code == 200, (code, body)
        card_row = body['cards'][0]
        assert card_row['acceptances'][0]['by'] == 'one'
        assert card_row['acceptances'][0]['evidence'] == 'tests green'

        # 4. Now the move is allowed, and the acceptance survives on the card.
        code, body = call(server, 'POST', f'/api/board/cards/{ordinal}/status',
                          {'revision': body['revision'], 'status': 'succeeded'})
        assert code == 200, (code, body)
        assert body['cards'][0]['status'] == 'succeeded'
        assert body['cards'][0]['acceptances'][0]['by'] == 'one'

        # 5. With identity records OFF (this config), an acceptor name is a free string:
        # that is today's behaviour and must not change. Refusing an unknown acceptor is
        # the identity layer's job, and the store test covers it with records on.
        code, body = call(server, 'POST', f'/api/board/cards/{ordinal}/accept',
                          {'revision': body['revision'], 'by': 'someone-not-recorded'})
        assert code == 200, (code, body)
        assert body['cards'][0]['acceptances'][-1]['by'] == 'someone-not-recorded'
    finally:
        server.shutdown()
        server.server_close()


def test_intake_end_to_end_through_the_http_route(tmp_path):
    """A gathered artifact must be triaged, accepted or ignored from the page, not just the store.

    Two outcomes, both recorded: accepted becomes work; ignored stays as evidence
    with the reason.
    """
    config = configuration(tmp_path)
    config.server.port = 0
    server = create_server(config)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        server.manager.create('ops')
        card = server.manager.board.create('Gathered PR', 'ops', 0, 0, card_id='intake-e2e',
                                           intake={'source': 'github:owner/repo',
                                                   'reference': 'owner/repo#12',
                                                   'summary': 'adds a null check'})
        view = call(server, 'GET', '/api/board/view')[1]
        ordinal = view['cards'][0]['ordinal']
        assert view['cards'][0]['status'] == 'intake'
        assert view['cards'][0]['intake']['reference'] == 'owner/repo#12'
        # Not work: it is not offered as claimable.
        assert view['cards'][0]['claimable'] is False

        # Accepting before triage is refused, because the decision reads the subject.
        code, body = call(server, 'POST', f'/api/board/cards/{ordinal}/intake-accept',
                          {'revision': view['revision'], 'by': 'ops'})
        assert code == 409, (code, body)
        assert 'Triage' in body['error']

        # Triage it.
        view = call(server, 'GET', '/api/board/view')[1]
        code, body = call(server, 'POST', f'/api/board/cards/{ordinal}/intake-triage',
                          {'revision': view['revision'], 'subject': 'fix',
                           'compatibility': 'applies cleanly to HEAD', 'summary': 'null deref fix'})
        assert code == 200, (code, body)
        row = body['cards'][0]
        assert row['intake']['subject'] == 'fix'
        assert row['intake']['compatibility'] == 'applies cleanly to HEAD'

        # An unknown subject is a bad request.
        code, _ = call(server, 'POST', f'/api/board/cards/{ordinal}/intake-triage',
                       {'revision': body['revision'], 'subject': 'whatever'})
        assert code == 400

        # Accept as work.
        code, body = call(server, 'POST', f'/api/board/cards/{ordinal}/intake-accept',
                          {'revision': body['revision'], 'by': 'ops', 'note': 'we want this'})
        assert code == 200, (code, body)
        row = body['cards'][0]
        assert row['status'] == 'queued'
        assert row['claimable'] is True
        assert row['acceptances'][0]['kind'] == 'intake'
        assert row['acceptances'][0]['evidence'] == 'owner/repo#12'
    finally:
        server.shutdown()
        server.server_close()


def test_intake_triage_from_the_page_names_the_reviewing_session(tmp_path):
    """The page sends `session` as the reviewer, so the triaged card must name it.

    The gathering session and the reviewing session are different on purpose. Before this
    test, the page's actor was dropped at the store boundary and the card kept the
    gatherer as `updated_by`, so the board's history credited the wrong thread.
    """
    config = configuration(tmp_path)
    config.server.port = 0
    server = create_server(config)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        server.manager.create('gatherer')
        server.manager.create('desk')
        server.manager.board.create('Gathered PR', 'gatherer', 0, 0, card_id='intake-who',
                                    intake={'source': 'github:owner/repo',
                                            'reference': 'owner/repo#14'})
        view = call(server, 'GET', '/api/board/view')[1]
        ordinal = view['cards'][0]['ordinal']
        assert view['cards'][0]['updated_by'] == 'gatherer'

        code, body = call(server, 'POST', f'/api/board/cards/{ordinal}/intake-triage',
                          {'revision': view['revision'], 'subject': 'fix',
                           'session': 'desk', 'summary': 'null deref fix'})
        assert code == 200, (code, body)
        row = body['cards'][0]
        assert row['intake']['subject'] == 'fix'
        assert row['updated_by'] == 'desk'

        # The review survives a fresh read of the journal, not only the reply.
        reloaded = call(server, 'GET', '/api/board/view')[1]
        assert reloaded['cards'][0]['updated_by'] == 'desk'
    finally:
        server.shutdown()
        server.server_close()


def test_ignoring_an_intake_card_through_the_route_keeps_it_as_evidence(tmp_path):
    config = configuration(tmp_path)
    config.server.port = 0
    server = create_server(config)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        server.manager.create('ops')
        server.manager.board.create('Gathered PR', 'ops', 0, 0, card_id='intake-ign',
                                    intake={'reference': 'owner/repo#13', 'summary': 'big rewrite'})
        view = call(server, 'GET', '/api/board/view')[1]
        ordinal = view['cards'][0]['ordinal']
        code, body = call(server, 'POST', f'/api/board/cards/{ordinal}/intake-ignore',
                          {'revision': view['revision'], 'by': 'ops',
                           'reason': 'out of scope for this release'})
        assert code == 200, (code, body)
        row = body['cards'][0]
        assert row['status'] == 'cancelled'
        assert row['ignored'][0]['reason'] == 'out of scope for this release'
        # Still readable: ignored is not deleted.
        assert row['intake']['reference'] == 'owner/repo#13'
    finally:
        server.shutdown()
        server.server_close()
