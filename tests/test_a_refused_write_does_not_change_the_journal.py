"""What a refusal may and may not change.

`_require_room` refuses a write the reader's cap cannot hold even after compaction. Its
contract is "this record did not fit": the caller has no reason to expect the store to
have moved, so the guard itself must be read-only.

The guard itself does not repair. Every mutating entry point calls
`_load_cards(repair=True)` BEFORE it reaches the guard (board.py:586, 639, 664, 684, 702,
887, 908, 924, 953, 980, 1007). So on the real call path the tail is already healed by the
time the guard runs. These tests pin
the two layers separately so neither claim is overstated:

- the guard, called directly, changes nothing;
- create() adds no card and preserves the torn tail as evidence, but does heal it first.
"""

import pytest

from tessera.board import BoardStore, BoardSizeError, validate_card


def fill(board, count):
    for index in range(count):
        board.create('Card %d' % index, 'one', 0, 0, note='x' * 120,
                     card_id='card-%d' % index)


def torn_tail(board):
    """Leave the journal as a crash would: a partial record, no newline."""
    with board.path.open('ab') as stream:
        stream.write(b'{"card": {"id": "torn", "title": "half-writ')


def incomplete_backups(board):
    return sorted(path.name for path in board.path.parent.iterdir()
                  if '.incomplete-' in path.name)


def an_oversized_card():
    return {'id': 'overflow', 'title': 'Overflow', 'session_slug': 'one',
            'log_start': 0, 'log_end': 0, 'attempt': 0, 'note': 'x' * 120,
            'owner': 'one', 'project': '', 'creation': {}, 'status': 'queued',
            'version': 1, 'artifacts': [], 'questions': [], 'created_at': 0.0,
            'updated_at': 0.0, 'updated_by': 'one', 'removed': False, 'execution': None}


def test_the_guard_itself_is_read_only_when_it_refuses(tmp_path):
    """DISCRIMINATES: fails on d37eb80, where the guard read with repair=True."""
    board = BoardStore(tmp_path / 'board.jsonl', enabled=True, max_journal_bytes=6000)
    fill(board, 8)
    torn_tail(board)
    before = board.path.read_bytes()

    with pytest.raises(BoardSizeError):
        board._require_room(board.path, an_oversized_card(), 'card', validate_card)

    assert board.path.read_bytes() == before, 'the guard changed the journal while refusing'
    assert incomplete_backups(board) == [], 'the guard repaired the journal while refusing'


def test_the_guard_permits_a_write_that_fits_without_touching_the_journal(tmp_path):
    """Control: the guard returns on a single stat, so nothing is read or written."""
    board = BoardStore(tmp_path / 'board.jsonl', enabled=True, max_journal_bytes=10 ** 7)
    fill(board, 3)
    torn_tail(board)
    before = board.path.read_bytes()

    board._require_room(board.path, an_oversized_card(), 'card', validate_card)

    assert board.path.read_bytes() == before
    assert incomplete_backups(board) == []


def test_a_refused_create_adds_no_card_and_keeps_the_torn_tail_as_evidence(tmp_path):
    """create() heals the tail first (that is deliberate), but must not create a card."""
    board = BoardStore(tmp_path / 'board.jsonl', enabled=True, max_journal_bytes=6000)
    fill(board, 8)
    torn_tail(board)
    cards_before, _ = board._read_journal(board.path, repair=False)

    with pytest.raises(BoardSizeError):
        board.create('Overflow', 'one', 0, 0, note='x' * 120, card_id='overflow')

    cards_after, _ = board._read_journal(board.path)
    assert set(cards_after) == set(cards_before), 'the refused create added or removed a card'
    assert 'overflow' not in cards_after
    # The torn bytes are discarded, not lost: they are kept beside the journal as evidence.
    assert incomplete_backups(board), 'the torn tail was discarded without being preserved'


def test_the_refusal_names_the_cap_and_the_bytes_it_needs(tmp_path):
    board = BoardStore(tmp_path / 'board.jsonl', enabled=True, max_journal_bytes=6000)
    fill(board, 8)

    with pytest.raises(BoardSizeError) as raised:
        board.create('Overflow', 'one', 0, 0, note='x' * 120, card_id='overflow')

    message = str(raised.value)
    assert '6000 bytes' in message
    assert 'compacted' in message


def test_the_allow_path_still_heals_a_torn_tail_before_appending(tmp_path):
    """Control: this is why repair=True must stay on the allow path.

    Appending to an unhealed torn tail would glue the new record onto a broken line and
    make the journal unreadable for good.
    """
    board = BoardStore(tmp_path / 'board.jsonl', enabled=True, max_journal_bytes=10 ** 7)
    fill(board, 3)
    torn_tail(board)

    moved = board.update_status('card-0', 'running', actor='one', expected_version=1)
    assert moved['status'] == 'running'

    assert incomplete_backups(board), 'the torn tail was not preserved as evidence'
    items, _ = board._read_journal(board.path)
    assert len(items) == 3
    assert 'card-0' in items
