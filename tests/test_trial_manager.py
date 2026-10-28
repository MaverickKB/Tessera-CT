"""The trial engine wired to a real SessionManager.

These prove the two things a trust dial has to get right end to end: a trial can
change a session, and a tie puts it back. The engine's own rules are pinned in
tests/test_trial.py; these check that the manager applies exactly what the
engine instructs and nothing more.
"""
import json

import pytest

from fake_backend import FakeBackend
from tessera.config import BackendConfig, Config
from tessera.sessions import SessionManager


def config_for(root, policy='auto'):
    config = Config(home=root, cwd=root)
    config.jev.enabled = False
    config.tools.approval = 'never'
    config.autonomy.enabled = False
    config.autonomy.oversight_policy = policy
    config.autonomy.trial_poll_seconds = 0.0
    config.backends['candidate'] = BackendConfig(name='candidate', model='candidate',
                                                 base_url='http://127.0.0.1:1')
    config.backends['base'] = BackendConfig(name='base', model='base',
                                            base_url='http://127.0.0.1:1')
    return config


def write_events(root, rows):
    root.mkdir(parents=True, exist_ok=True)
    (root / 'events.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in rows))


def trial_rows(turn, base, failures):
    rows = []
    for index, code in enumerate(failures):
        rows.append({'event': 'message', 'data': {
            'kind': 'tool', 'text': json.dumps({'name': 'shell', 'arguments': {}}),
            'date': f'2026-10-24T00:{base + index * 2:02d}:00+00:00',
            'meta': {'turn': turn}}})
        rows.append({'event': 'message', 'data': {
            'kind': 'echo', 'text': json.dumps({'exit_code': code, 'output': ''}),
            'date': f'2026-10-24T00:{base + index * 2 + 1:02d}:00+00:00',
            'meta': {'turn': turn}}})
    return rows


def test_a_trial_changes_a_real_session_and_a_tie_puts_it_back(tmp_path):
    config = config_for(tmp_path)
    with SessionManager(config, backend_factory=lambda *a: FakeBackend([])) as manager:
        slug = manager.create('Trial subject')['slug']
        manager.update(slug, reasoning='low')
        trial = manager.start_trial(slug, 'reasoning', 'high', 1, 'repeat_failures',
                                    'a repeated failing tool is a structural fact, not a score')
        assert trial['phase'] == 'baseline' and trial['from'] == 'low'
        assert manager.trials.open_for(slug), 'the trial must be on disk, not only in memory'

        root = manager.path(slug)
        # A turn that predates the trial, so the baseline window has a floor.
        write_events(root, trial_rows(1, 0, [1, 1]))
        manager.advance_trials()
        # Baseline arm: one repeat.
        write_events(root, trial_rows(1, 0, [1, 1]) + trial_rows(2, 10, [1, 1]))
        manager.advance_trials()
        assert manager.describe(slug)['reasoning'] == 'high', 'the candidate arm must be applied'

        # Candidate arm: one repeat too, so the arms tie.
        write_events(root, trial_rows(1, 0, [1, 1]) + trial_rows(2, 10, [1, 1])
                     + trial_rows(3, 20, [1, 1]))
        manager.advance_trials()
        decided = manager.trials.trials[trial['id']]
        assert decided['phase'] == 'decided'
        assert decided['verdict']['outcome'] == 'tie'
        assert manager.describe(slug)['reasoning'] == 'low', 'a tie must revert the change'


def test_a_win_keeps_the_change(tmp_path):
    config = config_for(tmp_path)
    with SessionManager(config, backend_factory=lambda *a: FakeBackend([])) as manager:
        slug = manager.create('Winner')['slug']
        manager.update(slug, reasoning='low')
        manager.start_trial(slug, 'reasoning', 'high', 1, 'repeat_failures', 'because')
        write_events(manager.path(slug), trial_rows(1, 0, [1, 1]))
        manager.advance_trials()
        # Baseline: one repeat.
        write_events(manager.path(slug), trial_rows(1, 0, [1, 1]) + trial_rows(2, 10, [1, 1]))
        manager.advance_trials()
        assert manager.describe(slug)['reasoning'] == 'high'
        # Candidate: none.
        write_events(manager.path(slug), trial_rows(1, 0, [1, 1]) + trial_rows(2, 10, [1, 1])
                     + trial_rows(3, 20, [0]))
        manager.advance_trials()
        assert manager.describe(slug)['reasoning'] == 'high', 'a win keeps the change'


def test_a_backend_trial_uses_the_same_write_path_a_person_uses(tmp_path):
    config = config_for(tmp_path)
    with SessionManager(config, backend_factory=lambda *a: FakeBackend([])) as manager:
        slug = manager.create('Backend trial')['slug']
        manager.update(slug, backend='base')
        manager.start_trial(slug, 'backend', 'candidate', 1, 'repeat_failures', 'because')
        write_events(manager.path(slug), trial_rows(1, 0, [1, 1]))
        manager.advance_trials()
        write_events(manager.path(slug), trial_rows(1, 0, [1, 1]) + trial_rows(2, 10, [1, 1]))
        manager.advance_trials()
        assert manager.describe(slug)['backend'] == 'candidate'


def test_one_open_trial_per_session(tmp_path):
    config = config_for(tmp_path)
    with SessionManager(config, backend_factory=lambda *a: FakeBackend([])) as manager:
        slug = manager.create('Only one')['slug']
        manager.start_trial(slug, 'reasoning', 'high', 1, 'repeat_failures', 'because')
        with pytest.raises(ValueError, match='already has an open trial'):
            manager.start_trial(slug, 'reasoning', 'medium', 1, 'repeat_failures', 'because')


def test_the_auto_policy_reports_the_trials_it_is_waiting_on(tmp_path):
    config = config_for(tmp_path)
    with SessionManager(config, backend_factory=lambda *a: FakeBackend([])) as manager:
        slug = manager.create('Reported')['slug']
        manager.start_trial(slug, 'reasoning', 'high', 4, 'turn_seconds_median', 'because')
        report = manager.oversight_report()
        joined = ' '.join(report['findings'])
        assert 'trial(s) are measuring a change' in joined
        assert 'reasoning low -> high' in joined
        assert 'turn seconds median' in joined
        # Still no proposals under 'auto': a trial is a measurement, not a question.
        assert report['proposals'] == []


def test_a_turn_ending_moves_a_trial_even_with_autonomy_off(tmp_path):
    """A trial measures turns, so the turn-end path has to move it.

    With autonomy disabled nothing ticks, and a trial started on a quiet house
    would sit in its baseline arm forever. This pins the turn-end hook.
    """
    config = config_for(tmp_path)
    assert config.autonomy.enabled is False
    with SessionManager(config, backend_factory=lambda *a: FakeBackend(['ok'] * 6)) as manager:
        slug = manager.create('Quiet house')['slug']
        manager.update(slug, reasoning='low')
        trial = manager.start_trial(slug, 'reasoning', 'high', 1, 'repeat_failures', 'because')

        # Turn 1 only sets the baseline window's floor.
        manager.submit(slug, 'first')
        assert manager.wait(slug, 10)
        assert manager.trials.trials[trial['id']]['baseline_first'] == 2
        assert manager.describe(slug)['reasoning'] == 'low'

        # Turn 2 completes the baseline arm, so the candidate is applied.
        manager.submit(slug, 'second')
        assert manager.wait(slug, 10)
        assert manager.describe(slug)['reasoning'] == 'high'

        # Turn 3 completes the candidate arm: both arms had no repeated failures,
        # so the arms tie and the change reverts.
        manager.submit(slug, 'third')
        assert manager.wait(slug, 10)
        decided = manager.trials.trials[trial['id']]
        assert decided['phase'] == 'decided'
        assert decided['verdict']['outcome'] == 'tie'
        assert manager.describe(slug)['reasoning'] == 'low'
