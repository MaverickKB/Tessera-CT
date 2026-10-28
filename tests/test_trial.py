"""Bounded A/B trials: the measurement an 'auto' oversight policy waits for.

The 'auto' setting states a precondition the product has to be able to meet.
These pin the rules that make
the measurement trustworthy rather than theatrical:

  * an instrument that cannot separate the arms is refused
  * a tie or a loss reverts, and only a margin that clears the threshold wins
  * a win is the only path that keeps a change
  * a trial may only change a backend or a reasoning level

The instrument choice is the load-bearing part. The oversight lane's own ceiling
finding says heuristic-graded turns pass at one value only, so a completion-score
comparison reads the same in both arms. A trial built on it would adopt a change
on noise.
"""
import json

import pytest

from tessera.config import BackendConfig, Config
from tessera.trial import (INSTRUMENTS, MIN_TOOL_CALLS, advance, decide, describe,
                           reading, start, validate)


def config(**backends):
    cfg = Config()
    for name in backends:
        cfg.backends[name] = BackendConfig(name=name, model='m-' + name,
                                           base_url='http://127.0.0.1:1')
    return cfg


def turn_row(turn, date, kind='message', meta=None, text='x'):
    return {'event': 'message', 'data': {'kind': kind, 'text': text, 'date': date,
                                         'meta': {'turn': turn, **(meta or {})}}}


def tool_rows(turn, base, failures):
    """One turn with a tool call and an echo per entry in `failures`."""
    rows = []
    for index, code in enumerate(failures):
        rows.append(turn_row(turn, f'2026-10-24T00:{base + index * 2:02d}:00+00:00',
                             'tool', text=json.dumps({'name': 'shell', 'arguments': {}})))
        rows.append(turn_row(turn, f'2026-10-24T00:{base + index * 2 + 1:02d}:00+00:00',
                             'echo', text=json.dumps({'exit_code': code, 'output': ''})))
    return rows


def test_a_completion_score_is_not_an_instrument():
    """The judge cannot separate two arms, so it must not be offered as one."""
    assert 'completion' not in INSTRUMENTS and 'judge_score' not in INSTRUMENTS
    with pytest.raises(ValueError, match='cannot separate two arms'):
        validate('backend', 'b', 5, 'judge_score', 'it seemed fine', config(b='b'))


def test_an_instrument_needs_a_reason_it_can_tell_the_arms_apart():
    with pytest.raises(ValueError, match='why this instrument'):
        validate('reasoning', 'high', 5, 'repeat_failures', '   ', config())


def test_a_trial_may_only_change_a_backend_or_a_reasoning_level():
    with pytest.raises(ValueError, match='lever must be one of'):
        validate('config_path', 'anything', 5, 'repeat_failures', 'because', config())


def test_a_backend_candidate_must_be_a_configured_chat_backend():
    with pytest.raises(ValueError, match='configured backend'):
        validate('backend', 'not-configured', 5, 'repeat_failures', 'because', config())
    validate('backend', 'b', 5, 'repeat_failures', 'because', config(b='b'))


def test_a_reasoning_candidate_must_be_a_real_level():
    with pytest.raises(ValueError, match='reasoning candidate'):
        validate('reasoning', 'turbo', 5, 'repeat_failures', 'because', config())
    validate('reasoning', 'high', 5, 'repeat_failures', 'because', config())


def test_a_candidate_identical_to_the_current_value_is_refused():
    with pytest.raises(ValueError, match='already the current value'):
        start('t1', 's', 'reasoning', 'low', 'low', 5, 'repeat_failures', 'because')


def test_a_thin_arm_cannot_be_read():
    """Below the tool-call floor a rate is noise, so it reads as nothing."""
    assert reading({'tool_calls': MIN_TOOL_CALLS - 1, 'tool_nonzero': 0},
                   'tool_nonzero_rate') is None
    assert reading({'tool_calls': MIN_TOOL_CALLS, 'tool_nonzero': 2},
                   'tool_nonzero_rate') == 2 / MIN_TOOL_CALLS


def test_insufficient_arms_decide_nothing():
    verdict = decide({'tool_calls': 3}, {'tool_calls': 3}, 'tool_nonzero_rate')
    assert verdict['outcome'] == 'insufficient'


def test_a_margin_that_clears_the_threshold_is_a_win():
    verdict = decide({'tool_calls': 100, 'tool_nonzero': 10},
                     {'tool_calls': 100, 'tool_nonzero': 4}, 'tool_nonzero_rate')
    assert verdict['outcome'] == 'win'


def test_a_small_improvement_is_a_tie_not_a_win():
    """0.10 -> 0.09 is inside the margin: the default must be to leave it alone."""
    verdict = decide({'tool_calls': 100, 'tool_nonzero': 10},
                     {'tool_calls': 100, 'tool_nonzero': 9}, 'tool_nonzero_rate')
    assert verdict['outcome'] == 'tie'


def test_the_stored_margin_decides_and_the_default_stays_at_eighty_percent():
    """The margin is a setting on the trial. Omitting it keeps the historical 0.8 rule."""
    from tessera.trial import RATE_MARGIN, SECONDS_MARGIN, effective_margin
    assert RATE_MARGIN == SECONDS_MARGIN == 0.8
    trial = start('t-margin', 'subject', 'reasoning', 'high', 'low', 1,
                  'tool_nonzero_rate', 'because')
    assert trial['margin'] == 0.8
    assert effective_margin({}) == 0.8
    assert effective_margin({'margin': 0.5}) == 0.5
    baseline = {'tool_calls': 100, 'tool_nonzero': 10}
    candidate = {'tool_calls': 100, 'tool_nonzero': 9}
    assert decide(baseline, candidate, 'tool_nonzero_rate', margin=0.95)['outcome'] == 'win'
    with pytest.raises(ValueError, match='margin'):
        start('t-zero', 'subject', 'reasoning', 'high', 'low', 1,
              'tool_nonzero_rate', 'because', margin=0)
    with pytest.raises(ValueError, match='margin'):
        start('t-bool', 'subject', 'reasoning', 'high', 'low', 1,
              'tool_nonzero_rate', 'because', margin=True)


def test_worse_is_a_loss():
    verdict = decide({'tool_calls': 100, 'tool_nonzero': 4},
                     {'tool_calls': 100, 'tool_nonzero': 10}, 'tool_nonzero_rate')
    assert verdict['outcome'] == 'loss'


def test_strictly_fewer_repeated_failures_is_a_win():
    assert decide({'repeat_failures': 4}, {'repeat_failures': 1},
                  'repeat_failures')['outcome'] == 'win'
    assert decide({'repeat_failures': 4}, {'repeat_failures': 4},
                  'repeat_failures')['outcome'] == 'tie'
    assert decide({'repeat_failures': 2}, {'repeat_failures': 5},
                  'repeat_failures')['outcome'] == 'loss'


def test_a_slower_median_is_a_loss_and_a_faster_one_is_a_win():
    assert decide({'turn_seconds_median': 100}, {'turn_seconds_median': 70},
                  'turn_seconds_median')['outcome'] == 'win'
    assert decide({'turn_seconds_median': 100}, {'turn_seconds_median': 90},
                  'turn_seconds_median')['outcome'] == 'tie'
    assert decide({'turn_seconds_median': 100}, {'turn_seconds_median': 140},
                  'turn_seconds_median')['outcome'] == 'loss'


def test_the_baseline_arm_runs_for_its_bounded_turns_then_applies_the_candidate():
    """The trial holds the session still, measures, and only then changes it."""
    cfg = config(b='b')
    trial = start('t1', 'subject', 'reasoning', 'high', 'low', 2, 'repeat_failures', 'because')
    rows = tool_rows(1, 0, [1, 1])

    # Turn 1 predates the trial: the baseline window must not include it.
    trial, instruction = advance(trial, rows, cfg, 'subject')
    assert trial['phase'] == 'baseline' and trial['baseline_first'] == 2
    assert instruction is None

    # Two complete baseline turns.
    rows = tool_rows(2, 0, [1, 1]) + tool_rows(3, 10, [1, 1])
    trial, instruction = advance(trial, rows, cfg, 'subject')
    assert trial['phase'] == 'candidate'
    assert trial['baseline']['first'] == 2 and trial['baseline']['last'] == 3
    assert trial['baseline']['reading'] == 2
    assert instruction == {'apply': {'slug': 'subject', 'lever': 'reasoning', 'value': 'high'}}


def test_a_tie_reverts_the_change():
    cfg = config()
    trial = start('t1', 'subject', 'reasoning', 'high', 'low', 1, 'repeat_failures', 'because')
    trial = {**trial, 'phase': 'baseline', 'baseline_first': 1}
    rows = tool_rows(1, 0, [1, 1])
    trial, _ = advance(trial, rows, cfg, 'subject')
    assert trial['phase'] == 'candidate' and trial['candidate_first'] == 2

    rows = rows + tool_rows(2, 10, [1, 1])
    trial, instruction = advance(trial, rows, cfg, 'subject')
    assert trial['phase'] == 'decided'
    assert trial['verdict']['outcome'] == 'tie'
    assert trial['verdict']['margin'] == 0.8
    assert trial['applied'] is False
    assert instruction == {'apply': {'slug': 'subject', 'lever': 'reasoning', 'value': 'low'}}


def test_a_win_keeps_the_change_and_applies_nothing_more():
    cfg = config()
    trial = start('t1', 'subject', 'reasoning', 'high', 'low', 1, 'repeat_failures', 'because')
    trial = {**trial, 'phase': 'baseline', 'baseline_first': 1}
    trial, _ = advance(trial, tool_rows(1, 0, [1, 1]), cfg, 'subject')
    rows = tool_rows(1, 0, [1, 1]) + tool_rows(2, 10, [0])
    trial, instruction = advance(trial, rows, cfg, 'subject')
    assert trial['verdict']['outcome'] == 'win'
    assert trial['applied'] is True
    # The candidate is already in place; there is nothing left to apply.
    assert instruction is None


def test_a_decided_trial_is_not_advanced_again():
    cfg = config()
    trial = {**start('t1', 's', 'reasoning', 'high', 'low', 1, 'repeat_failures', 'b'),
             'phase': 'decided'}
    same, instruction = advance(trial, [], cfg, 's')
    assert same is trial and instruction is None


def test_a_duration_trial_stores_its_own_margin(monkeypatch):
    """A turn-duration trial keeps the duration margin, and an unusable stored one is flagged."""
    from tessera.trial import RATE_MARGIN, effective_margin, margin_sentence, margin_state
    monkeypatch.setattr('tessera.trial.SECONDS_MARGIN', 0.5)
    trial = start('t-seconds', 'subject', 'reasoning', 'high', 'low', 1,
                  'turn_seconds_median', 'because')
    assert trial['margin'] == 0.5
    rate = start('t-rate', 'subject', 'reasoning', 'high', 'low', 1,
                 'tool_nonzero_rate', 'because')
    assert rate['margin'] == RATE_MARGIN
    assert effective_margin({'instrument': 'turn_seconds_median'}) == 0.5
    margin, invalid = margin_state({'instrument': 'turn_seconds_median', 'margin': 4})
    assert invalid is True and margin == 0.5
    verdict = decide({'turn_seconds_median': 100}, {'turn_seconds_median': 70},
                     'turn_seconds_median', margin=trial['margin'])
    assert verdict['outcome'] == 'tie'
    sentence = margin_sentence({'instrument': 'turn_seconds_median', 'margin': 4})
    assert 'at most 50%' in sentence
    assert 'not usable' in sentence


def test_a_trial_describes_itself_in_the_terms_a_reader_needs():
    trial = start('t1', 'subject', 'backend', 'b', 'a', 10, 'turn_seconds_median', 'because')
    line = describe(trial)
    assert 'backend a -> b' in line and 'baseline arm' in line
    assert 'turn seconds median' in line
    assert 'at most 80% of the baseline' in line
