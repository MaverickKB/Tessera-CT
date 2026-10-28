"""The live buffer is where a mid-turn reader gets its view of the work."""

from tessera.sessions import EventJournal


def journal(tmp_path):
    return EventJournal(tmp_path / 'events.jsonl')


def test_turn_snapshot_folds_only_the_named_turn(tmp_path):
    events = journal(tmp_path)
    events.push_live('delta', {'turn_id': 'a', 'kind': 'thought', 'text': 'considering '})
    events.push_live('delta', {'turn_id': 'a', 'kind': 'thought', 'text': 'the card'})
    events.push_live('delta', {'turn_id': 'a', 'kind': 'text', 'text': 'Working '})
    events.push_live('delta', {'turn_id': 'a', 'kind': 'text', 'text': 'on it.'})
    events.push_live('delta', {'turn_id': 'a', 'kind': 'tool', 'text': 'shell'})
    events.push_live('delta', {'turn_id': 'b', 'kind': 'text', 'text': 'other turn'})
    snapshot = events.turn_snapshot('a')
    assert snapshot['turn_id'] == 'a'
    assert snapshot['thought'] == 'considering the card'
    assert snapshot['text'] == 'Working on it.'
    assert snapshot['tool'] == 'shell'
    # status is what a reader who attaches mid-turn actually needs: the fold reports
    # what the turn is doing now, not just the text it has accumulated.
    assert snapshot['status'] == 'Calling shell...'


def test_turn_snapshot_resets_text_and_reports_nothing_for_a_quiet_turn(tmp_path):
    events = journal(tmp_path)
    events.push_live('delta', {'turn_id': 'a', 'kind': 'text', 'text': 'draft'})
    events.push_live('delta', {'turn_id': 'a', 'kind': 'reset'})
    events.push_live('delta', {'turn_id': 'a', 'kind': 'text', 'text': 'final'})
    assert events.turn_snapshot('a')['text'] == 'final'
    assert events.turn_snapshot('b') is None
    assert events.turn_snapshot('') is None
