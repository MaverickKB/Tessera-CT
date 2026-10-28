"""Bounded A/B trials, so a trust dial of 'auto' has something to measure.

The highest trust setting waits for an A/B measurement and applies nothing
until that measurement exists. This module is that measurement. It is
deliberately the smallest thing that can be honest:

  * one change at a time, named by a person, never invented by the lane
  * a bounded number of turns per arm, recorded so a later reader can check it
  * the same instrument on both arms, chosen before the run and justified
  * adopt on a win, revert on a tie or a loss, and write the reading down

Why the instrument has to be chosen and justified
-------------------------------------------------
The oversight lane's own ceiling finding says heuristic-graded turns only ever
pass at one value, so a completion-score comparison cannot separate two arms --
both would read the same. A trial that used it would "measure" nothing and then
adopt a change on noise. So the caller names an instrument from INSTRUMENTS and
says why it can tell the arms apart; an instrument that cannot discriminate is
refused rather than quietly accepted.

Nothing here applies a change. It returns instructions; the caller owns the
session. That keeps the engine testable without a running core.
"""

import json
import math
import os
import time
from pathlib import Path

from .oversight import TAIL_BYTES, _tail_rows, grade_rows


def session_rows(root, config):
    """The records a trial grades, read the same way the oversight lane reads them."""
    limit = config.status.max_file_bytes
    return _tail_rows(Path(root) / 'events.jsonl', min(limit, TAIL_BYTES)) or []

# Changes this engine is allowed to trial. Anything else is refused: a trial
# that could rewrite an arbitrary config path is not a trust dial, it is an
# unsupervised editor.
LEVERS = ('backend', 'reasoning')

# The instruments that can separate two arms, and which direction is better.
# Each reads a structural fact from the records, never a judge score.
INSTRUMENTS = {
    # Lower is better. Needs enough tool calls in both arms to mean anything.
    'tool_nonzero_rate': 'lower',
    # Lower is better. Counts turns where one tool failed twice or more.
    'repeat_failures': 'lower',
    # Lower is better. Median turn span in seconds, from recorded message dates.
    'turn_seconds_median': 'lower',
}

# A win has to be a real margin, not a rounding difference. A change that moves
# a reading by less than this is a tie, and a tie reverts: the default is to
# leave the session as it was unless the measurement says otherwise.
RATE_MARGIN = 0.8      # candidate rate must be at most 80% of baseline
SECONDS_MARGIN = 0.8   # candidate median must be at most 80% of baseline

# Below this many tool calls an arm's rate is too thin to compare.
MIN_TOOL_CALLS = 20

PHASES = ('baseline', 'candidate', 'decided')


def _now():
    return time.time()


def validate(lever, candidate, turns, instrument, why, config):
    """Refuse a trial that could not produce a trustworthy comparison."""
    if lever not in LEVERS:
        raise ValueError(f'lever must be one of {", ".join(LEVERS)}')
    if instrument not in INSTRUMENTS:
        raise ValueError(f'instrument must be one of {", ".join(INSTRUMENTS)}; '
                         'a completion score cannot separate two arms')
    if not isinstance(why, str) or not why.strip():
        raise ValueError('say why this instrument can tell the two arms apart')
    if type(turns) is not int or not 1 <= turns <= 200:
        raise ValueError('turns must be an integer from 1 to 200')
    if lever == 'backend':
        chosen = config.backends.get(candidate) if isinstance(candidate, str) else None
        if not isinstance(candidate, str) or not candidate or chosen is None:
            raise ValueError('backend candidate must name a configured backend')
        from .config import offered_for_chat
        if not offered_for_chat(candidate, chosen):
            raise ValueError('backend candidate is not offered for chat')
    elif lever == 'reasoning':
        if candidate not in ('off', 'low', 'medium', 'high'):
            raise ValueError('reasoning candidate must be off, low, medium, or high')
    return True


def default_margin(instrument):
    """The margin an instrument uses when the caller does not store one."""
    return SECONDS_MARGIN if instrument == 'turn_seconds_median' else RATE_MARGIN


def margin_state(trial):
    """(effective, invalid). A missing margin uses the instrument default and is not invalid."""
    instrument = trial.get('instrument') if isinstance(trial, dict) else None
    fallback = default_margin(instrument)
    if not isinstance(trial, dict) or 'margin' not in trial or trial.get('margin') is None:
        return fallback, False
    margin = trial.get('margin')
    if isinstance(margin, bool) or not isinstance(margin, (int, float)) or not math.isfinite(float(margin)):
        return fallback, True
    margin = float(margin)
    if not 0 < margin <= 1:
        return fallback, True
    return margin, False


def effective_margin(trial):
    """The margin the decision uses: the stored one, or the instrument default."""
    return margin_state(trial)[0]


def annotate_trial(trial):
    """Add the margin the reader should see, and whether the stored one was unusable."""
    margin, invalid = margin_state(trial)
    annotated = dict(trial)
    annotated['margin_effective'] = margin
    annotated['margin_invalid'] = invalid
    return annotated


def _check_margin(margin):
    if isinstance(margin, bool) or not isinstance(margin, (int, float)) or not math.isfinite(float(margin)):
        raise ValueError('margin must be a number greater than 0 and at most 1')
    margin = float(margin)
    if not 0 < margin <= 1:
        raise ValueError('margin must be a number greater than 0 and at most 1')
    return margin


def start(trial_id, slug, lever, candidate, current, turns, instrument, why, margin=None):
    """A new trial in its baseline arm, holding the session where it is."""
    if current == candidate:
        raise ValueError('the candidate is already the current value; nothing to compare')
    return {
        'id': trial_id,
        'slug': slug,
        'lever': lever,
        'from': current,
        'to': candidate,
        'turns': turns,
        'instrument': instrument,
        'why': why.strip(),
        'margin': _check_margin(margin) if margin is not None else default_margin(instrument),
        'phase': 'baseline',
        'started_at': _now(),
        'baseline': None,
        'candidate': None,
        'verdict': None,
        'applied': False,
        'reverted': False,
    }


def latest_turn(rows):
    """The highest turn number in a slice of records, or 0."""
    highest = 0
    for row in rows:
        data = row.get('data')
        if not isinstance(data, dict):
            continue
        meta = data.get('meta')
        if not isinstance(meta, dict):
            continue
        turn = meta.get('turn')
        if type(turn) is int and turn > highest:
            highest = turn
    return highest


def window(rows, first, last):
    """Records whose turn falls inside [first, last], both inclusive."""
    picked = []
    for row in rows:
        data = row.get('data')
        if not isinstance(data, dict):
            continue
        meta = data.get('meta')
        if not isinstance(meta, dict):
            continue
        turn = meta.get('turn')
        if type(turn) is int and first <= turn <= last:
            picked.append(row)
    return picked


def reading(grade, instrument):
    """The one number an instrument reads from a grade, or None."""
    if instrument == 'tool_nonzero_rate':
        calls = grade.get('tool_calls') or 0
        if calls < MIN_TOOL_CALLS:
            return None
        return (grade.get('tool_nonzero') or 0) / calls
    if instrument == 'repeat_failures':
        return grade.get('repeat_failures') or 0
    if instrument == 'turn_seconds_median':
        return grade.get('turn_seconds_median')
    return None


def decide(baseline, candidate, instrument, margin=None):
    """Win, tie, or loss from two readings of the same instrument.

    A tie is the common case and it reverts. Only a margin that clears the
    threshold is a win, because the default must be to leave the session alone.
    """
    before = reading(baseline, instrument)
    after = reading(candidate, instrument)
    if before is None or after is None:
        return {'outcome': 'insufficient',
                'detail': 'one or both arms did not record enough to read '
                          + instrument.replace('_', ' ') + ' honestly'}
    if instrument == 'repeat_failures':
        # A count, not a rate: strictly fewer repeated failures is a win.
        if after < before:
            return {'outcome': 'win', 'before': before, 'after': after,
                    'detail': f'{instrument.replace("_", " ")} fell from {before} to {after}'}
        return {'outcome': 'tie' if after == before else 'loss',
                'before': before, 'after': after,
                'detail': f'{instrument.replace("_", " ")} was {before} before and {after} after'}
    if margin is None:
        margin = SECONDS_MARGIN if instrument == 'turn_seconds_median' else RATE_MARGIN
    else:
        margin = _check_margin(margin)
    if after <= before * margin:
        return {'outcome': 'win', 'before': before, 'after': after,
                'detail': f'{instrument.replace("_", " ")} fell from {before:.3f} to {after:.3f}'}
    if after >= before:
        return {'outcome': 'loss', 'before': before, 'after': after,
                'detail': f'{instrument.replace("_", " ")} rose from {before:.3f} to {after:.3f}'}
    return {'outcome': 'tie', 'before': before, 'after': after,
            'detail': f'{instrument.replace("_", " ")} moved from {before:.3f} to {after:.3f}, '
                      f'inside the {margin:.0%} margin'}


def advance(trial, rows, config, slug):
    """Move a trial forward if its current arm is complete.

    Returns the updated trial plus an instruction for the caller, which owns the
    session: {'apply': {'slug', 'lever', 'value'}} or None. Nothing is applied
    here, so a caller that cannot reach the session changes nothing.
    """
    if trial['phase'] == 'decided':
        return trial, None

    highest = latest_turn(rows)
    bound = trial['turns']
    arm = trial['phase']

    if arm == 'baseline':
        first = trial.get('baseline_first')
        if first is None:
            # The baseline arm starts at the next turn, so its own window does
            # not include turns already on the record before the trial began.
            return {**trial, 'baseline_first': highest + 1}, None
        last = first + bound - 1
        if highest < last:
            return trial, None
        grade = grade_rows(window(rows, first, last), slug, config)
        trial = {**trial,
                 'baseline': {'first': first, 'last': last, 'grade': grade,
                              'reading': reading(grade, trial['instrument']),
                              'at': _now()},
                 'phase': 'candidate',
                 'candidate_first': highest + 1}
        return trial, {'apply': {'slug': slug, 'lever': trial['lever'], 'value': trial['to']}}

    first = trial.get('candidate_first')
    if first is None:
        return {**trial, 'candidate_first': highest + 1}, None
    last = first + bound - 1
    if highest < last:
        return trial, None
    grade = grade_rows(window(rows, first, last), slug, config)
    margin = effective_margin(trial)
    verdict = decide(trial['baseline']['grade'], grade, trial['instrument'], margin)
    verdict['at'] = _now()
    verdict['margin'] = margin
    trial = {**trial,
             'candidate': {'first': first, 'last': last, 'grade': grade,
                           'reading': reading(grade, trial['instrument']),
                           'at': _now()},
             'verdict': verdict,
             'phase': 'decided'}
    if verdict['outcome'] == 'win':
        # Keep the candidate. It is already applied, so there is nothing to do.
        return {**trial, 'applied': True}, None
    return trial, {'apply': {'slug': slug, 'lever': trial['lever'], 'value': trial['from']}}


def margin_sentence(trial):
    """The rule a reader sees before the trial runs, using the margin that will decide."""
    margin, invalid = margin_state(trial)
    sentence = (f"A win keeps the change only when the candidate is at most {margin:.0%} of the baseline. "
                f"Repeated failures win only when the count falls.")
    if invalid:
        sentence += ' The stored margin is not usable, so this is the default.'
    return sentence


def describe(trial):
    """One readable line about a trial, for a report or a card."""
    verdict = trial.get('verdict') or {}
    state = trial['phase']
    if state == 'decided':
        outcome = verdict.get('outcome', 'unknown')
        tail = ('kept the change' if trial.get('applied')
                else 'reverted to ' + str(trial['from']))
        return (f"Trial on {trial['slug']}: {trial['lever']} {trial['from']} -> {trial['to']} "
                f"read {outcome} by {trial['instrument'].replace('_', ' ')}; {tail}. "
                + margin_sentence(trial))
    return (f"Trial on {trial['slug']}: {trial['lever']} {trial['from']} -> {trial['to']} "
            f"in its {state} arm, {trial['turns']} turns per arm, read by "
            f"{trial['instrument'].replace('_', ' ')}. " + margin_sentence(trial))


class TrialStore:
    """Trials on disk, one file, written atomically.

    A trial that cannot be read back is reported, not silently dropped: a
    measurement nobody can check later is not a measurement.
    """

    def __init__(self, path):
        self.path = Path(path)
        self.unreadable = None
        self.trials = {}
        if self.path.is_file():
            try:
                saved = json.loads(self.path.read_text(encoding='utf-8'))
            except (OSError, json.JSONDecodeError) as problem:
                self.unreadable = f'{self.path}: {type(problem).__name__}'
                saved = {}
            trials = saved.get('trials') if isinstance(saved, dict) else None
            if isinstance(trials, dict):
                self.trials = {key: value for key, value in trials.items()
                               if isinstance(value, dict) and value.get('phase') in PHASES}

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + '.tmp')
        temporary.write_text(json.dumps({'trials': self.trials}, indent=2, sort_keys=True),
                             encoding='utf-8')
        os.replace(temporary, self.path)

    def add(self, trial):
        if trial['id'] in self.trials:
            raise ValueError('a trial with that id already exists')
        for existing in self.trials.values():
            if existing['slug'] == trial['slug'] and existing['phase'] != 'decided':
                raise ValueError(f"{trial['slug']} already has an open trial; one change at a time")
        self.trials[trial['id']] = trial
        self.save()
        return trial

    def open_for(self, slug):
        return [trial for trial in self.trials.values()
                if trial['slug'] == slug and trial['phase'] != 'decided']

    def open_for_all(self):
        return [trial for trial in self.trials.values() if trial['phase'] != 'decided']

    def update(self, trial):
        self.trials[trial['id']] = trial
        self.save()
        return trial

    def decided(self):
        return [trial for trial in self.trials.values() if trial['phase'] == 'decided']
