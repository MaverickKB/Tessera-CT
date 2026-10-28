"""An evidence path settles a lesson only when the check is about that file.

settle_answerable's docstring promised to absorb lessons "whose file check is
already true", but the code never read the check: any lesson carrying an
evidence path was absorbed as soon as the file existed. A lesson whose check was
a multi-step procedure and whose evidence_path was an incidentally related file
was closed at creation with a passed check reading 'file present'. The procedure
was never evaluated.

The rule is a string comparison, not a reading of intent: the check must name the
evidence file, by basename or by full path.
"""

import json

from test_autonomy import open_manager


def _saved(tmp_path):
    return json.loads((tmp_path / 'learning' / 'lessons.json').read_text(encoding='utf-8'))['lessons']


def test_a_check_that_names_the_file_still_absorbs(tmp_path):
    """The legitimate shape this mechanism was built for must keep working."""
    helper = tmp_path / 'helper'
    helper.write_text('ok', encoding='utf-8')
    manager, slug, clock, _ = open_manager(tmp_path, ['quiet'], self_prompt_gap_seconds=0,
                                           standing_floor=False)
    with manager:
        lesson = manager.autonomy.learning.record_lesson(
            slug, 'rebuild the desk helper', 'the helper was not compiled',
            'the helper binary exists', evidence_path=str(helper))
        clock.now += 5
        manager.autonomy.tick()
        assert manager.autonomy.learning.list_lessons(slug) == []
        assert _saved(tmp_path)[lesson['id']]['status'] == 'absorbed'


def test_a_procedure_whose_evidence_file_is_incidental_stays_open(tmp_path):
    """The fault: the file exists, but the check is not about it."""
    unrelated = tmp_path / 'test_learning.py'
    unrelated.write_text('# a real file, not what the check is about\n', encoding='utf-8')
    manager, slug, clock, _ = open_manager(tmp_path, ['quiet'], self_prompt_gap_seconds=0,
                                           standing_floor=False)
    with manager:
        lesson = manager.autonomy.learning.record_lesson(
            slug,
            'a full-suite run shows a red test another session is editing',
            'I treated their in-flight test as my regression',
            'Check whether the failing test exists at HEAD before treating it as mine',
            evidence_path=str(unrelated))
        clock.now += 5
        manager.autonomy.tick()
        # Still open: nothing has verified the procedure.
        assert manager.autonomy.learning.list_lessons(slug)
        row = _saved(tmp_path)[lesson['id']]
        assert row['status'] != 'absorbed'
        assert not any(entry.get('note') == 'file present' for entry in row.get('checks') or [])


def test_a_check_naming_the_full_path_absorbs(tmp_path):
    """Either identifier counts: basename or full path."""
    helper = tmp_path / 'bin' / 'desk-helper'
    helper.parent.mkdir()
    helper.write_text('ok', encoding='utf-8')
    manager, slug, clock, _ = open_manager(tmp_path, ['quiet'], self_prompt_gap_seconds=0,
                                           standing_floor=False)
    with manager:
        lesson = manager.autonomy.learning.record_lesson(
            slug, 'deploy the desk helper', 'the deploy did not run',
            f'{helper} exists and is executable', evidence_path=str(helper))
        clock.now += 5
        manager.autonomy.tick()
        assert _saved(tmp_path)[lesson['id']]['status'] == 'absorbed'


def test_a_missing_file_never_settles_even_when_named(tmp_path):
    """Naming the file is necessary, not sufficient: it must also exist."""
    missing = tmp_path / 'never-created'
    manager, slug, clock, _ = open_manager(tmp_path, ['quiet'], self_prompt_gap_seconds=0,
                                           standing_floor=False)
    with manager:
        lesson = manager.autonomy.learning.record_lesson(
            slug, 'build the missing artefact', 'the build did not run',
            'never-created exists', evidence_path=str(missing))
        clock.now += 5
        manager.autonomy.tick()
        assert _saved(tmp_path)[lesson['id']]['status'] != 'absorbed'
