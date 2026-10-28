"""A check that names its evidence file only past the clip can never settle.

record_lesson clips the check to 300 characters, appending the ``[…]`` marker.
settle_answerable settles a lesson only when the *stored* check names the
evidence file. So a check that names the file only in its tail is stored without
the name: the row carries an evidence path, never settles against it, and keeps
an OPEN slot forever without saying why.

The fix refuses at write time, but only when the name was present before the
clip and absent after it. A check that never mentioned the file is the
incidental-evidence case, which must still be recorded and stay open for a real
evaluation.
"""

import pytest

from test_autonomy import open_manager


def test_a_check_naming_the_file_only_past_the_clip_is_refused(tmp_path):
    """The fault. On HEAD this row is created and then never settles."""
    helper = tmp_path / 'helper-binary'
    helper.write_text('#!/bin/sh\n', encoding='utf-8')
    manager, slug, _, _ = open_manager(tmp_path, ['quiet'], self_prompt_gap_seconds=0,
                                       standing_floor=False)
    with manager:
        long_check = (
            'First confirm the desk machine is reachable, then confirm the service '
            'unit is loaded, then read the last journal lines for the unit, then '
            'verify the helper exists and is executable by running it with --version '
            'and comparing the reported build against the installed package '
            'metadata, and only then treat the outcome as evidence. The evidence '
            'file is ' + helper.name)
        with pytest.raises(ValueError) as refusal:
            manager.autonomy.learning.record_lesson(
                slug, 'the helper binary reports a build that matches the package',
                'it reported a different build', long_check,
                evidence_path=str(helper))
        message = str(refusal.value)
        assert helper.name in message
        assert 'clip' in message
        # Nothing was written: the refusal is at the door, not after the fact.
        assert manager.autonomy.learning.list_lessons(slug) == []


def test_a_check_naming_the_file_early_is_still_accepted(tmp_path):
    """The same evidence path, with the name inside the clip, must still record."""
    helper = tmp_path / 'helper-binary'
    helper.write_text('#!/bin/sh\n', encoding='utf-8')
    manager, slug, _, _ = open_manager(tmp_path, ['quiet'], self_prompt_gap_seconds=0,
                                       standing_floor=False)
    with manager:
        lesson = manager.autonomy.learning.record_lesson(
            slug, 'the helper binary reports a build that matches the package',
            'it reported a different build',
            'Run ' + helper.name + ' with --version and compare the reported build '
            'against the installed package metadata, then confirm the service unit '
            'is loaded and read the last journal lines for it before treating the '
            'outcome as evidence of anything.',
            evidence_path=str(helper))
        assert lesson['status'] == 'open'
        assert helper.name in lesson['check']


def test_a_check_that_never_mentions_the_file_is_still_recorded(tmp_path):
    """The incidental-evidence case must not be caught by the clip refusal."""
    unrelated = tmp_path / 'test_learning.py'
    unrelated.write_text('# a real file, not what the check is about\n', encoding='utf-8')
    manager, slug, _, _ = open_manager(tmp_path, ['quiet'], self_prompt_gap_seconds=0,
                                       standing_floor=False)
    with manager:
        lesson = manager.autonomy.learning.record_lesson(
            slug, 'a full-suite run shows a red test another session is editing',
            'I treated their in-flight test as my regression',
            'Check whether the failing test exists at HEAD before treating it as mine',
            evidence_path=str(unrelated))
        assert lesson['status'] == 'open'
        assert unrelated.name not in lesson['check']
