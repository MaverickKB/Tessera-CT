"""A healthy bound lesson says how to close, and a full cap names what filled it.

A bound lesson at status 'written' is delivered with the check that closes it.
When the open cap is full, the refusal names the lessons that fill it.
"""
from tessera.learning import LearningStore


def store_at(tmp_path, **options):
    return LearningStore(tmp_path / 'lessons.json', clock=lambda: 100.0, **options)


def test_a_bound_lesson_names_the_step_that_closes_it(tmp_path):
    """The healthy case must carry a next step, like the other two cases do."""
    store = store_at(tmp_path)
    lesson = store.record_lesson('home', 'anchor every read to a revision',
                                 'a read landed on another commit',
                                 'the revision is named',
                                 skill='revision-anchored-measurement')
    assert lesson['status'] == 'written'

    line = store.agenda_lines('home')[0]
    assert 'Skill: revision-anchored-measurement.' in line, line
    # The close step, naming this lesson's own id, not some other lesson's.
    assert 'lesson_check(' in line, line
    assert lesson['id'] in line, line
    assert 'passed=true' in line, line


def test_an_unbound_lesson_still_says_to_write_the_skill(tmp_path):
    """The fix must not replace the unbound instruction, only add to the bound one."""
    store = store_at(tmp_path)
    store.record_lesson('home', 'rebuild the desk helper', 'missing binary',
                        'binary exists')
    line = store.agenda_lines('home')[0]
    assert 'No skill bound yet. Write one and bind it.' in line, line
    assert 'lesson_check(' not in line, line


def test_a_needs_revision_lesson_is_told_to_revise_not_to_close(tmp_path):
    """A lesson that has failed twice is not ready to be closed as passed."""
    store = store_at(tmp_path, pass_score=1.0, fail_limit=2)
    lesson = store.record_lesson('home', 'rebuild the desk helper', 'missing binary',
                                 'binary exists', skill='desk-helper')
    for _ in range(2):
        store.observe_turn('home', 'rebuild the desk helper now', [], None, True, False)
    assert store.list_lessons('home')[0]['status'] == 'needs_revision'

    line = store.agenda_lines('home')[0]
    assert 'Revise the skill or retire the lesson.' in line, line
    assert 'lesson_check(' not in line, line


def test_the_cap_refusal_names_the_bound_lessons_that_filled_it(tmp_path):
    """A full store must say what filled it, not just that it is full."""
    store = store_at(tmp_path, max_open=2)
    first = store.record_lesson('home', 'anchor every read to a revision',
                                'a read landed on another commit', 'the revision is named',
                                skill='revision-anchored-measurement')
    second = store.record_lesson('home', 'name the pid before a live claim',
                                 'the pid was assumed', 'the pid is measured',
                                 skill='check-running-process-before-live-claim')

    try:
        store.record_lesson('home', 'a third lesson that cannot fit',
                            'the store was full', 'the refusal names the blockers')
    except ValueError as error:
        message = str(error)
    else:
        raise AssertionError('the cap must refuse a third lesson')

    assert 'Open lesson cap reached (2)' in message, message
    assert 'bound and never evaluated' in message, message
    assert first['id'] in message, message
    assert second['id'] in message, message
    assert 'lesson_check(' in message, message


def test_the_cap_refusal_stays_plain_when_nothing_is_bound(tmp_path):
    """An empty-of-skills store must not be told to close lessons it cannot close."""
    store = store_at(tmp_path, max_open=1)
    store.record_lesson('home', 'rebuild the desk helper', 'missing binary', 'binary exists')
    try:
        store.record_lesson('home', 'another unbound lesson', 'the store was full',
                            'the refusal is honest')
    except ValueError as error:
        message = str(error)
    else:
        raise AssertionError('the cap must refuse a second lesson')

    # The count must be named, and no close instruction may appear: there is
    # nothing bound to close, so telling the reader to close one would send it
    # looking for a lesson that does not exist.
    assert 'Open lesson cap reached (1)' in message, message
    assert 'bound and never evaluated' not in message, message
    assert 'lesson_check(' not in message, message
