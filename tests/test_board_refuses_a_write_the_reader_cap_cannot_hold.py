"""The board must not accept a write the bounded reader can never see again.

Compaction keeps one record per item, so the compacted form is the smallest the journal
can be. Once that form exceeds status.max_file_bytes, `passive_status.read_snapshot`
returns rows=None and every category on the page shows zero cards. Compaction
cannot recover that either, because the next write makes the journal bigger again.
Rewriting the whole file on every write never comes back under the cap, so the
board stays blind.
"""

import json
from types import SimpleNamespace

import pytest

from tessera.board import BoardSizeError, BoardStore, replay_cards


# This card shape, with the default time.time() clock: eight cards compact to
# 5998-6008 bytes and nine compact to 6756-6770. The
# spread is the JSON length of the timestamps. A 6000 cap sits inside the
# eight-card band, so the eighth write is refused whenever a timestamp encodes
# long. 6400 is above that band and below the nine-card floor. This is the
# journal reader cap (status.max_file_bytes), not a swarm concurrency cap.
READER_CAP = 6400


def settings_for(cap):
    return SimpleNamespace(max_file_bytes=cap, max_line_bytes=10 ** 6)


def open_board(path, cap):
    # time.time() does not have a fixed JSON width, so the same eight cards
    # sit under or over a 6000-byte cap depending on the clock. Pin it.
    return BoardStore(path, enabled=True, max_journal_bytes=cap, clock=lambda: 1700000000)


def fill(board, count, note='x' * 120):
    """Write cards the way a real session does: create, start, finish."""
    for index in range(count):
        card = board.create('Card %d' % index, 'one', 0, 0, note=note,
                            card_id='card-%d' % index)
        moved = board.update_status(card['id'], 'running', actor='one',
                                    expected_version=card['version'])
        board.update_status(card['id'], 'succeeded', actor='one',
                            expected_version=moved['version'])


def test_a_write_the_compacted_form_cannot_fit_is_refused_not_silently_stored(tmp_path):
    """The cap is below what eight cards need compacted, so the ninth write is refused.

    Eight cards compact to 5998-6008 bytes. READER_CAP holds those eight, and no
    compaction can make room for a ninth: nine cards compact to 6756-6770 bytes.
    The old code appended anyway, leaving a journal the bounded reader refused to read.
    """
    from tessera.passive_status import read_snapshot

    board = BoardStore(tmp_path / 'board.jsonl', enabled=True, max_journal_bytes=READER_CAP)
    fill(board, 8)
    # The eight-card board is still readable, which is what the cap was set for.
    ok, rows, _, _ = read_snapshot(board.path, settings_for(READER_CAP))
    assert ok and rows is not None and len(rows) == 8

    with pytest.raises(BoardSizeError, match='cannot hold'):
        board.create('One too many', 'one', 0, 0, note='x' * 120, card_id='overflow')

    # The refusal left the journal exactly as it was: still readable, still eight cards.
    ok, rows, _, _ = read_snapshot(board.path, settings_for(READER_CAP))
    assert ok and rows is not None and len(rows) == 8
    assert len(board.list()) == 8


def test_the_refusal_names_the_cap_and_what_the_compacted_form_needs(tmp_path):
    """A person told "this changed, refresh" learns nothing. Name the numbers."""
    board = BoardStore(tmp_path / 'board.jsonl', enabled=True, max_journal_bytes=READER_CAP)
    fill(board, 8)
    with pytest.raises(BoardSizeError) as raised:
        board.create('One too many', 'one', 0, 0, note='x' * 120, card_id='overflow')
    message = str(raised.value)
    assert str(READER_CAP) in message and '9 items' in message


def test_a_board_over_the_cap_does_not_rewrite_the_whole_journal_on_every_write(tmp_path):
    """Compaction that reclaims nothing must not fire.

    Once the compacted form is over the cap, `size <= cap * COMPACT_AT` is false on every
    write. Rewriting the whole file on each refused write never comes back under the cap.
    """
    board = BoardStore(tmp_path / 'board.jsonl', enabled=True, max_journal_bytes=READER_CAP)
    fill(board, 8)
    before = board.path.read_text()
    backup = board.path.with_name('board.jsonl.pre-compact')
    # The fill itself compacted legitimately while it grew, so the backup exists and is
    # current. What must NOT happen is a rewrite on the refused writes below.
    assert backup.exists()
    backup_before = backup.read_bytes()

    # Every one of these writes is refused, so none of them can trigger a rewrite.
    for index in range(5):
        with pytest.raises(BoardSizeError):
            board.create('Overflow %d' % index, 'one', 0, 0, note='x' * 120,
                         card_id='overflow-%d' % index)

    assert board.path.read_text() == before
    # A rewrite would have replaced the backup with the pre-rewrite journal.
    assert backup.read_bytes() == backup_before


def test_a_journal_that_still_fits_is_never_refused(tmp_path):
    """The guard must not fire on the ordinary case: room to spare means append freely."""
    board = open_board(tmp_path / 'board.jsonl', 200000)
    fill(board, 40)
    assert len(board.list()) == 40
    assert board.path.stat().st_size < 200000


def test_compaction_still_happens_when_it_reclaims_bytes(tmp_path):
    """Skipping pointless rewrites must not disable the ones that matter."""
    board = open_board(tmp_path / 'board.jsonl', 8000)
    fill(board, 8)
    records = [json.loads(line) for line in board.path.read_text().splitlines()]
    cards, sequence = replay_cards(records)
    # Eight cards written as 24 records compact to 8, every fence back to 1.
    assert len(records) == 8 and sequence == 8
    assert all(card['version'] == 1 for card in cards.values())
