"""Durable WIP records, independent of providers and background scheduling."""
import pytest


def board_at(tmp_path, **options):
    from tessera.board import BoardStore
    return BoardStore(tmp_path / 'board.jsonl', enabled=True, **options)


def test_disabled_board_has_no_storage_or_time_side_effect(tmp_path):
    from tessera.board import BoardStore, BoardError
    store = BoardStore(tmp_path / 'absent' / 'board.jsonl', clock=lambda: pytest.fail('disabled clock'))
    assert store.list() == []
    with pytest.raises(BoardError, match='disabled'):
        store.create('Inspect source', 'one', 0, 2)
    assert not (tmp_path / 'absent').exists()


def test_cards_reopen_with_owned_links_artifacts_and_human_question(tmp_path):
    board = board_at(tmp_path)
    card = board.create('Inspect source', 'one', 2, 5, card_id='stable-card')
    assert card['status'] == 'queued' and card['version'] == 1 and card['attempt'] == 0
    updated = board.update_status(card['id'], 'running', attempt=1, actor='one', expected_version=1)
    assert updated['version'] == 2
    board.attach_artifact(card['id'], 'artifact-placeholder', actor='one', expected_version=2)
    board.ask_human(card['id'], 'May I proceed?', actor='one', pending=True)
    restored = board_at(tmp_path).get(card['id'])
    assert restored['session_slug'] == 'one' and (restored['log_start'], restored['log_end']) == (2, 5)
    assert restored['artifacts'] == ['artifact-placeholder']
    assert restored['questions'][0]['text'] == 'May I proceed?' and restored['questions'][0]['pending']
    assert restored['status'] == 'blocked' and restored['attempt'] == 1


def test_create_and_artifact_retries_are_idempotent_and_copies_detached(tmp_path):
    board = board_at(tmp_path)
    card = board.create('Inspect', 'one', 0, 0, card_id='stable-card')
    before = board.path.read_bytes()
    assert board.create('Inspect', 'one', 0, 0, card_id='stable-card') == card
    assert board.path.read_bytes() == before
    attached = board.attach_artifact(card['id'], 'artifact-placeholder')
    before = board.path.read_bytes()
    assert board.attach_artifact(card['id'], 'artifact-placeholder') == attached
    assert board.path.read_bytes() == before
    attached['artifacts'].append('caller-mutated')
    assert board.get(card['id'])['artifacts'] == ['artifact-placeholder']
    with pytest.raises(ValueError, match='id'):
        board.create('Different', 'one', 0, 0, card_id='stable-card')


def test_owner_version_and_numeric_boundaries_reject_without_append(tmp_path):
    board = board_at(tmp_path)
    card = board.create('Inspect', 'one', 0, 1)
    before = board.path.read_bytes()
    for options in ({'actor': 'two'}, {'expected_version': 0}, {'attempt': True}, {'attempt': -1}):
        with pytest.raises(ValueError):
            board.update_status(card['id'], 'running', **options)
        assert board.path.read_bytes() == before
    for start, end in ((2, 1), (-1, 1), (True, 2), (0, 2**53)):
        with pytest.raises(ValueError):
            board.create('Inspect', 'one', start, end)
    with pytest.raises(ValueError):
        board.update_status(card['id'], 'private-status-placeholder')


def test_torn_tail_is_preserved_before_mutation_and_complete_corruption_fails(tmp_path):
    board = board_at(tmp_path)
    card = board.create('Inspect', 'one', 0, 1)
    with board.path.open('ab') as stream:
        stream.write(b'{torn')
    board.update_status(card['id'], 'running')
    assert list(tmp_path.glob('*.incomplete-*'))[0].read_bytes() == b'{torn'
    with board.path.open('ab') as stream:
        stream.write(b'not-json\n')
    with pytest.raises(ValueError, match='journal'):
        board.get(card['id'])


def test_collection_and_utf8_limits_do_not_open_artifact_paths(tmp_path):
    board = board_at(tmp_path, max_text_bytes=24, max_artifacts=1, max_questions=1)
    card = board.create('Inspect', 'one', 0, 1)
    board.attach_artifact(card['id'], '/artifact-placeholder')
    with pytest.raises(ValueError, match='limit'):
        board.attach_artifact(card['id'], '/another-placeholder')
    board.ask_human(card['id'], 'Choose a source?')
    with pytest.raises(ValueError, match='limit'):
        board.ask_human(card['id'], 'Choose another?')
    with pytest.raises(ValueError, match='limit'):
        board.create('é' * 13, 'one', 0, 0)


def test_remove_keeps_the_record_and_takes_the_card_off_the_board(tmp_path):
    """A card can become unnecessary before it is worked.
    I could not remove it." Removal is a tombstone, not an erase: the journal keeps the
    card as evidence, and every reader stops showing it as work.
    """
    from tessera.board import BoardError
    board = board_at(tmp_path)
    keep = board.create('Still work', 'one', 0, 0, card_id='keep-card')
    gone = board.create('No longer work', 'one', 0, 0, card_id='gone-card')
    assert [card['id'] for card in board.list()] == ['keep-card', 'gone-card']

    removed = board.remove('gone-card', expected_version=1)
    assert removed['removed'] is True and removed['version'] == 2

    # The record survives on disk, and the reopened store still has it.
    assert board_at(tmp_path).get('gone-card')['removed'] is True
    # But it is not work any more.
    assert [card['id'] for card in board_at(tmp_path).list()] == ['keep-card']
    assert keep['id'] == 'keep-card'

    with pytest.raises(BoardError, match='already removed'):
        board.remove('gone-card', expected_version=2)


def test_remove_refuses_a_stale_version_and_a_claimed_card(tmp_path):
    from tessera.board import BoardError
    board = board_at(tmp_path)
    # A card created with owner='' is open to anyone, so another session can claim it.
    board.create('Mine', 'one', 0, 0, card_id='c', owner='')
    board.claim('c', 'other-session', actor='other-session', expected_version=1)
    # Another session cannot remove a card that someone else claimed.
    with pytest.raises(BoardError, match='claimed by another session'):
        board.remove('c', actor='not-the-owner', expected_version=2)
    # The operator (actor=None) can.
    assert board.remove('c', expected_version=2)['removed'] is True


def test_appending_a_note_keeps_the_new_line_when_the_field_is_full(tmp_path):
    """A long-running card's note reached the 8192-byte cap and
    every further append failed with the same message as an empty note, so real progress
    could not be recorded. The newest line is the evidence, so it is kept whole and the
    oldest history is trimmed with a visible marker.
    """
    board = board_at(tmp_path, max_text_bytes=200)
    card = board.create('Long work', 'one', 0, 0, note='start', card_id='long')
    for index in range(40):
        board.append_note('long', 'step %d of the long grind' % index, expected_version=card['version'])
        card = board.get('long')
    note = board.get('long')['note']
    assert len(note.encode()) <= 200
    # The line just written survives intact.
    assert note.endswith('step 39 of the long grind')
    # The trim is announced rather than silent.
    assert note.startswith('[earlier note history trimmed]')
    # And it is still one readable block of lines, never half a line.
    assert all(line == line.strip() for line in note.split('\n'))


def test_a_note_line_larger_than_the_whole_field_names_the_measured_size(tmp_path):
    from tessera.board import BoardError
    board = board_at(tmp_path, max_text_bytes=40)
    board.create('Small field', 'one', 0, 0, card_id='small')
    with pytest.raises(BoardError, match='the limit is 40 bytes'):
        board.append_note('small', 'x' * 41, expected_version=1)
    with pytest.raises(BoardError, match='note text is empty'):
        board.append_note('small', '   ', expected_version=1)
    # An empty append did not bump the version, so nothing was written.
    assert board.get('small')['version'] == 1


def test_replacing_a_note_is_still_a_full_replace_and_reports_its_size(tmp_path):
    from tessera.board import BoardError
    board = board_at(tmp_path, max_text_bytes=40)
    board.create('Correct me', 'one', 0, 0, note='the plan', card_id='edit')
    replaced = board.revise('edit', note='the corrected plan', expected_version=1)
    assert replaced['note'] == 'the corrected plan'
    with pytest.raises(BoardError, match='note is empty'):
        board.revise('edit', note='', expected_version=2)
    with pytest.raises(BoardError, match='the limit is 40 bytes'):
        board.revise('edit', note='y' * 41, expected_version=2)


def test_the_journal_compacts_before_it_reaches_the_readers_cap(tmp_path):
    """The board must stay readable while it keeps being written to.

    The failure this prevents: 96 live cards, a journal 166 bytes
    over status.max_file_bytes, and every category on the page showing zero cards. The
    writer has no cap, the passive reader does, so the journal can only grow past it and
    the board is unreadable from then on — including after a restart.

    Cap 8000, not 6000. Eight cards cost about 5993 bytes compacted, so a
    6000 cap leaves a few bytes of margin and the test fails on timestamp length
    alone. The cap has to be far enough above the compacted floor that the assertion is
    about compaction, not about a rounding race.
    """
    board = board_at(tmp_path, max_journal_bytes=8000)
    for index in range(8):
        card = board.create('Card %d' % index, 'one', 0, 0, note='x' * 120, card_id='card-%d' % index)
        # Compaction restarts each card's version fence at 1, and a write that triggers a
        # compaction returns the card as the journal now holds it — so the version handed
        # back is always one that can be written against.
        moved = board.update_status(card['id'], 'running', actor='one', expected_version=card['version'])
        board.update_status(card['id'], 'succeeded', actor='one', expected_version=moved['version'])
    # Written well past the point where an uncompacted journal would have crossed it.
    assert board.path.stat().st_size < 8000
    assert len(board.list()) == 8
    # The compacted journal is still a valid journal for a reader that predates compaction.
    from tessera.board import replay_cards
    records = [__import__('json').loads(line) for line in board.path.read_text().splitlines()]
    cards, sequence = replay_cards(records)
    assert sequence == len(records) and len(cards) == 8
    assert all(card['version'] == 1 for card in cards.values())
    # The previous journal is kept as evidence rather than deleted.
    assert board.path.with_name('board.jsonl.pre-compact').exists()


def test_compaction_keeps_every_card_field_and_a_fresh_version_fence(tmp_path):
    from tessera.board import BoardStore
    # Cap 8000: this shape is nine cards once compacted (about 6730 bytes), so a 6000 cap
    # cannot hold it and the guard refuses the ninth write.
    board = board_at(tmp_path, max_journal_bytes=8000)
    card = board.create('Keep me', 'one', 0, 0, note='first note', card_id='keeper')
    board.update_status(card['id'], 'blocked', actor='one', expected_version=1)
    board.attach_artifact(card['id'], 'artifact-placeholder', actor='one', expected_version=2)
    board.ask_human(card['id'], 'May I proceed?', actor='one', pending=True)
    for index in range(8):
        board.create('Filler %d' % index, 'one', 0, 0, note='y' * 120, card_id='filler-%d' % index)
    reopened = BoardStore(tmp_path / 'board.jsonl', enabled=True, max_journal_bytes=8000)
    kept = reopened.get('keeper')
    assert kept['status'] == 'blocked' and kept['note'] == 'first note'
    assert kept['artifacts'] == ['artifact-placeholder']
    assert kept['questions'][0]['text'] == 'May I proceed?' and kept['questions'][0]['pending']
    assert kept['version'] == 1
    # A page fenced to the pre-compaction version is told the card changed, not silently
    # shown a card the fence will refuse.
    from tessera.board import BoardError
    with pytest.raises(BoardError, match='stale'):
        reopened.update_status('keeper', 'running', actor='one', expected_version=4)


def test_compaction_keeps_removed_cards_out_of_the_board_but_in_the_journal(tmp_path):
    # Cap 8000: ten cards compacted is ~7020 bytes; 6000 could not hold them.
    board = board_at(tmp_path, max_journal_bytes=8000)
    kept = board.create('Keep', 'one', 0, 0, card_id='keep')
    gone = board.create('Drop', 'one', 0, 0, card_id='drop')
    board.remove('drop', actor='one', expected_version=1)
    for index in range(8):
        board.create('Filler %d' % index, 'one', 0, 0, note='z' * 120, card_id='filler-%d' % index)
    assert [card['id'] for card in board.list()] == ['keep'] + ['filler-%d' % index for index in range(8)]
    assert board.get('drop')['removed'] is True


def test_the_project_journal_compacts_before_it_reaches_the_readers_cap(tmp_path):
    """Projects are read through the same bounded reader as cards, so they need the same cap.

    The project journal is read through the same status.max_file_bytes cap as the
    cards journal. A project journal crossing that cap makes every project vanish
    from the page, and the next write makes it bigger. Project records re-store the
    whole project on every version bump (steps and note included), so a long-lived
    plan grows the same way the card journal does.
    """
    board = board_at(tmp_path, max_journal_bytes=6000)
    project = board.create_project('A plan with a real shape', 'one',
                                   description='why it exists', note='the intent',
                                   project_id='plan')
    steps = [{'text': 'step %d' % index, 'done': False} for index in range(8)]
    project = board.set_steps('plan', steps)
    # Bump the plan well past the point where an uncompacted journal crosses the cap.
    for index in range(12):
        project = board.check_step('plan', project['steps'][0]['id'], index % 2 == 0)

    # The journal that the passive reader must be able to read is under the cap.
    assert board.project_path.stat().st_size < 6000

    # And it is still a valid project journal for a reader that predates compaction.
    from tessera.board import replay_projects
    records = [__import__('json').loads(line) for line in board.project_path.read_text().splitlines()]
    projects, sequence = replay_projects(records)
    assert sequence == len(records)
    assert list(projects) == ['plan']
    # Every field survives the rewrite; only the version fence restarts.
    kept = projects['plan']
    assert kept['title'] == 'A plan with a real shape'
    assert kept['description'] == 'why it exists'
    assert kept['note'] == 'the intent'
    assert [step['text'] for step in kept['steps']] == ['step %d' % index for index in range(8)]
    # The last bump was index 11, so step 0 ends False and every other step was never ticked.
    assert [step['done'] for step in kept['steps']] == [False] * 8
    # The version fence restarted: one record per project per version, so the version a
    # reader sees equals the number of records this project has. Pre-compaction it would
    # have been 14.
    assert kept['version'] == len(records) < 14
    # The previous journal is kept as evidence rather than deleted.
    assert board.project_path.with_name('board.projects.jsonl.pre-compact').exists()


def test_compacting_a_board_rewrites_both_journals(tmp_path):
    """`compact()` repairs an already-over-cap board; it must not skip the projects file.

    Set below the auto-compact threshold on purpose, so the only thing that rewrites the
    journals here is `compact()` itself. Five card records become one, five project
    records become one, both version fences return to 1, and nothing is dropped.
    """
    board = board_at(tmp_path, max_journal_bytes=200000)
    card = board.create('A card', 'one', 0, 0, card_id='c1')
    for index in range(4):
        card = board.update_status('c1', 'running', attempt=index, actor='one',
                                   expected_version=card['version'])
    project = board.create_project('A plan', 'one', project_id='plan')
    for index in range(4):
        project = board.add_step('plan', 'step %d' % index)
    # Both journals carry one record per version bump; that redundancy is what compaction
    # reclaims. No write above crossed the threshold, so nothing has compacted yet.
    assert len(board.path.read_text().splitlines()) == 5
    assert len(board.project_path.read_text().splitlines()) == 5

    sizes = board.compact()

    assert set(sizes) == {'cards', 'projects'}
    for name in ('cards', 'projects'):
        assert sizes[name] is not None, name
        assert sizes[name]['after'] < sizes[name]['before'], name
    assert len(board.path.read_text().splitlines()) == 1
    assert len(board.project_path.read_text().splitlines()) == 1
    # Nothing was dropped, and both fences restarted at 1.
    assert [item['id'] for item in board.list()] == ['c1']
    assert [item['id'] for item in board.list_projects()] == ['plan']
    assert board.get('c1')['version'] == 1
    assert board.get_project('plan')['version'] == 1
    assert [step['text'] for step in board.get_project('plan')['steps']] == \
        ['step %d' % index for index in range(4)]
    # Both previous journals are kept as evidence.
    assert board.path.with_name('board.jsonl.pre-compact').exists()
    assert board.project_path.with_name('board.projects.jsonl.pre-compact').exists()


def test_the_acceptance_gate_is_off_by_default(tmp_path):
    """An existing install must not suddenly require a sign-off it cannot perform."""
    board = board_at(tmp_path)
    card = board.create('Work', 'one', 0, 0)
    assert board.acceptance_required is False
    board.update_status(card['id'], 'succeeded')
    assert board.list()[0]['status'] == 'succeeded'
    # And nothing invents an acceptance record on the way.
    assert 'acceptances' not in board.list()[0]


def test_a_gated_card_cannot_be_succeeded_without_acceptance(tmp_path):
    """A card is not accepted as work until cleared."""
    board = board_at(tmp_path)
    board.acceptance_required = True
    card = board.create('Work', 'one', 0, 0)
    with pytest.raises(Exception) as refused:
        board.update_status(card['id'], 'succeeded')
    assert 'needs acceptance' in str(refused.value)
    # The card did not move.
    assert board.list()[0]['status'] == 'queued'


def test_an_acceptance_is_recorded_history_not_a_status_field(tmp_path):
    board = board_at(tmp_path)
    board.acceptance_required = True
    card = board.create('Work', 'one', 0, 0)
    board.accept(card['id'], 'reviewer', evidence='tests green at abc1234', note='checked the diff')
    board.update_status(card['id'], 'succeeded')
    card = board.list()[0]
    assert card['status'] == 'succeeded'
    assert len(card['acceptances']) == 1
    record = card['acceptances'][0]
    assert record['by'] == 'reviewer'
    assert record['evidence'] == 'tests green at abc1234'
    assert record['note'] == 'checked the diff'
    assert isinstance(record['at'], float)
    # Reopening the card keeps the acceptance: history, not a flag.
    board.update_status(card['id'], 'queued')
    reopened = board.list()[0]
    assert reopened['status'] == 'queued'
    assert len(reopened['acceptances']) == 1
    assert reopened['acceptances'][0]['by'] == 'reviewer'


def test_only_a_named_acceptor_may_accept_when_a_policy_is_set(tmp_path):
    board = board_at(tmp_path)
    board.acceptance_required = True
    board.acceptance_by = ('reviewer',)
    card = board.create('Work', 'one', 0, 0)
    with pytest.raises(Exception) as refused:
        board.accept(card['id'], 'worker')
    assert 'does not accept from' in str(refused.value)
    board.accept(card['id'], 'reviewer', evidence='read it')
    board.update_status(card['id'], 'succeeded')
    assert board.list()[0]['status'] == 'succeeded'


def test_a_non_succeeded_transition_is_not_gated(tmp_path):
    """The gate is on the transition INTO success, not on every status move."""
    board = board_at(tmp_path)
    board.acceptance_required = True
    card = board.create('Work', 'one', 0, 0)
    board.update_status(card['id'], 'running')
    board.update_status(card['id'], 'blocked')
    board.update_status(card['id'], 'failed')
    assert board.list()[0]['status'] == 'failed'


def test_an_acceptance_covers_the_version_it_was_given_against(tmp_path):
    """The audit finding: one acceptance must not authorize every later close.

    Reproduced before the fix: accept at v1 -> succeeded -> reopen to queued (card advances)
    -> succeeded again with no new acceptance, so a card reopened BECAUSE the work was wrong
    closed on the sign-off of the wrong work.
    """
    board = board_at(tmp_path)
    board.acceptance_required = True
    card = board.create('Work', 'one', 0, 0)
    board.accept(card['id'], 'reviewer', evidence='read it')
    accepted = board.get(card['id'])
    # The acceptance carries the version that holds it.
    assert accepted['acceptances'][0]['version'] == accepted['version']
    board.update_status(card['id'], 'succeeded')

    # Reopen: the card advances past the acceptance.
    board.update_status(card['id'], 'queued')
    reopened = board.get(card['id'])
    assert reopened['version'] > reopened['acceptances'][0]['version']
    # Re-closing on the old sign-off must be refused.
    with pytest.raises(Exception) as refused:
        board.update_status(card['id'], 'succeeded')
    assert 'predates its current version' in str(refused.value)
    # A fresh acceptance of the work as it stands allows it.
    board.accept(card['id'], 'reviewer', evidence='re-read after reopen')
    board.update_status(card['id'], 'succeeded')
    assert board.get(card['id'])['status'] == 'succeeded'


def test_a_swarm_job_store_carries_the_acceptance_gate(tmp_path):
    """The jobs lane is a second card-status writer, so it carries the same acceptance gate.

    With acceptance required, SwarmJobs' own BoardStore has to require acceptance
    and must not close a job card as succeeded with none.
    """
    from tessera.config import Config
    from tessera.jobs import SwarmJobs
    config = Config(home=tmp_path, cwd=tmp_path)
    config.board.enabled = True
    config.board.acceptance_required = True
    config.board.acceptance_by = ['reviewer']
    config.swarm.enabled = True
    jobs = SwarmJobs(config)
    assert jobs.board.acceptance_required is True
    assert jobs.board.acceptance_by == ('reviewer',)
    # And the gate actually reaches that store.
    card = jobs.board.create('Job card', 'one', 0, 0)
    with pytest.raises(Exception) as refused:
        jobs.board.update_status(card['id'], 'succeeded')
    assert 'needs acceptance' in str(refused.value)
