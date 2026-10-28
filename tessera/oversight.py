"""Session quality oversight: a bounded, read-only reading of how sessions behave.

Quality first, speed second. This module grades what the harness actually
recorded and reports its own blind spots rather than filling them with a
number.

Why it does not rank sessions by the judge's completion score
-------------------------------------------------------------
With the default config (goal_threshold=0.8, gate_threshold=0.7):

  jev.py:_fallback, the ``judge`` branch, can only return completion values
  0.0, 0.1, 0.65 or 0.9. ``_gate_result`` requires goal_met >= 0.8, so 0.65
  can never pass. Every heuristic-graded turn is therefore refused by
  arithmetic, not by judgment. Confirmed by reproducing the exact recorded
  shape from glm-rsi-watch turns 3..6: completion 0.65 -> allowed False.

  The same branch drops completion to 0.2 when the reply contains any of
  unable|cannot|failed|failure|error|stopped|blocked. A reply that honestly
  reports a failure and fixes it scores worse than a reply that echoes the
  request's own words. That is a keyword signal, not a quality measure.

So a session's pass rate under the heuristic says nothing about its quality.
This module therefore grades structure -- what the tools did, whether the
judge actually answered, whether claims outran evidence -- and states the
judge's own unreliability as a finding instead of trusting its score.

Nothing here executes, probes, resolves credentials, or repairs storage. It
reads committed records and returns a projection.
"""

import json
import os
from datetime import datetime
from pathlib import Path
import stat

from .passive_status import read_snapshot

# The heuristic's reachable completion scores (jev.py:_fallback, 'judge').
HEURISTIC_SCORES = (0.0, 0.1, 0.65, 0.9)

# Turn-record kinds this reader needs, and the bound on bytes read per file.
TAIL_BYTES = 4 * 1024 * 1024


def _tail_rows(path, byte_limit):
    """Complete JSON rows from the end of a file, newest last.

    Only newline-terminated rows count: an interrupted append leaves a
    fragment that is not yet a record, and parsing it would raise.
    """
    try:
        from .durability import open_regular
        descriptor = open_regular(path)
    except FileNotFoundError:
        return []
    except OSError:
        return None
    with os.fdopen(descriptor, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            return None
        start = max(0, info.st_size - byte_limit)
        stream.seek(start)
        raw = stream.read(info.st_size - start)
    lines = raw.split(b'\n')
    if start > 0:
        # The first line is cut mid-record; drop it.
        lines = lines[1:]
    rows = []
    for line in lines:
        if not line:
            continue
        try:
            row = json.loads(line)
        except (ValueError, UnicodeError):
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _echo_exit(text):
    """The exit code an echo record reports, or None."""
    if not isinstance(text, str):
        return None
    try:
        payload = json.loads(text)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    code = payload.get('exit_code')
    return code if type(code) is int else None


def _recorded_at(value):
    """A message's own timestamp, or None when absent or unparsable."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _tool_name(text):
    """The tool a `tool` record calls, or '' when the record is unreadable."""
    if not isinstance(text, str):
        return ''
    try:
        payload = json.loads(text)
    except ValueError:
        return ''
    if not isinstance(payload, dict):
        return ''
    name = payload.get('name')
    return name if isinstance(name, str) else ''


def _nearest_rank(sorted_values, fraction):
    """Nearest-rank percentile over an already-sorted list."""
    if not sorted_values:
        return None
    rank = max(1, int(len(sorted_values) * fraction + 0.9999))
    return sorted_values[min(rank, len(sorted_values)) - 1]


def grade_session(root, config, byte_limit=None):
    """One session's structural quality facts. No judgement score is trusted."""
    root = Path(root)
    limit = config.status.max_file_bytes if byte_limit is None else byte_limit
    rows = _tail_rows(root / 'events.jsonl', min(limit, TAIL_BYTES))
    if rows is None:
        return {'slug': root.name, 'readable': False}
    return grade_rows(rows, root.name, config)


def grade_rows(rows, slug, config):
    """Grade one already-read slice of records.

    Split out of grade_session so a bounded window can be graded on the same
    instrument as a whole session. An A/B trial needs exactly that: two arms
    measured by the same rules, so the comparison is between the arms and not
    between two different readings.
    """

    turns = {}
    tools = 0
    tool_nonzero = 0
    turn_ends = 0
    turn_failed = 0
    errors = 0
    interrupted = 0
    # Speed is recorded, not inferred. Every message carries its own date, so a
    # turn's span is the distance between its first and last recorded message.
    # turn_start/turn_end records carry no timestamp, so this is the only honest
    # wall-clock reading available from the log.
    turn_spans = {}
    turn_tools = {}
    turn_failures = {}
    last_tool = {}

    for row in rows:
        event = row.get('event')
        data = row.get('data')
        if not isinstance(data, dict):
            continue
        meta = data.get('meta')
        if not isinstance(meta, dict):
            meta = {}
        turn = meta.get('turn')
        date = data.get('date')
        stamp = _recorded_at(date)
        if turn is not None and stamp is not None:
            span = turn_spans.get(turn)
            if span is None:
                turn_spans[turn] = [stamp, stamp]
            else:
                if stamp < span[0]:
                    span[0] = stamp
                if stamp > span[1]:
                    span[1] = stamp

        if event == 'turn_end':
            turn_ends += 1
            if data.get('success') is False:
                turn_failed += 1
            if data.get('interrupted'):
                interrupted += 1
            continue
        if event == 'error':
            errors += 1
            continue
        if event != 'message':
            continue

        kind = data.get('kind')
        if kind == 'tool':
            tools += 1
            turn_tools[turn] = turn_tools.get(turn, 0) + 1
            name = _tool_name(data.get('text'))
            if name:
                last_tool[turn] = name
            continue
        if kind == 'echo':
            code = _echo_exit(data.get('text'))
            if code is not None and code != 0:
                tool_nonzero += 1
                # A tool failing twice inside one turn is a retry loop, which is
                # a different fact from two tools each failing once.
                name = last_tool.get(turn)
                if name:
                    key = (turn, name)
                    turn_failures[key] = turn_failures.get(key, 0) + 1
            continue
        if kind != 'agent' or 'judge_score' not in meta:
            continue

        gate = meta.get('output_gate')
        gate = gate if isinstance(gate, dict) else {}
        verification = meta.get('verification')
        verification = verification if isinstance(verification, dict) else {}
        row_turn = turns.setdefault(turn, {'dates': []})
        row_turn.update({
            'score': meta.get('judge_score'),
            'source': meta.get('judge_source') or '',
            'allowed': gate.get('allowed'),
            'gate_source': gate.get('source') or '',
            'claims_supported': verification.get('claims_supported'),
            'claims_done_but_failed': verification.get('claims_done_but_failed'),
        })
        if date:
            row_turn['dates'].append(date)

    graded = [value for value in turns.values() if 'score' in value]
    by_source = {}
    refusals = {}
    unsupported = 0
    unexamined = 0
    spans = []
    for value in graded:
        source = value['source'] or 'unknown'
        by_source[source] = by_source.get(source, 0) + 1
        if value['allowed'] is False:
            gate_source = value['gate_source'] or 'unknown'
            refusals[gate_source] = refusals.get(gate_source, 0) + 1
        if value['gate_source'] == 'deterministic':
            # jev.py:338-341 sets claims_done_but_failed to the literal 1 on this
            # path. That value means "a tool failed", never "the reply overclaimed
            # while failing" -- the reply is not examined here at all. Counting it
            # as a claim judgment would report a constant as evidence.
            unexamined += 1
        else:
            failed_claim = value.get('claims_done_but_failed')
            if isinstance(failed_claim, (int, float)) and failed_claim >= config.jev.gate_threshold:
                unsupported += 1
        if len(value['dates']) >= 2:
            spans.append(sorted(value['dates']))

    heuristic_scores = sorted({
        value['score'] for value in graded
        if value['source'] == 'heuristic' and isinstance(value['score'], (int, float))
    })

    durations = sorted((span[1] - span[0]).total_seconds() for span in turn_spans.values())
    slowest = None
    if durations:
        worst = max(turn_spans.items(), key=lambda item: item[1][1] - item[1][0])
        slowest = {'turn': worst[0],
                   'seconds': round((worst[1][1] - worst[1][0]).total_seconds(), 1),
                   'tool_calls': turn_tools.get(worst[0], 0)}
    repeats = {key: count for key, count in turn_failures.items() if count >= 2}

    return {
        'slug': slug,
        'readable': True,
        'graded': len(graded),
        'judge_sources': by_source,
        'judge_answered': by_source.get('jev', 0),
        'refusals': refusals,
        'refused_deterministic': refusals.get('deterministic', 0),
        'claims_unsupported': unsupported,
        'claims_unexamined': unexamined,
        'tool_calls': tools,
        'tool_nonzero': tool_nonzero,
        'turn_ends': turn_ends,
        'turns_failed': turn_failed,
        'errors': errors,
        'interrupted': interrupted,
        'heuristic_scores': heuristic_scores,
        # Speed and retry pressure, measured from the records themselves.
        'turns_timed': len(durations),
        'turn_seconds_median': round(_nearest_rank(durations, 0.5), 1) if durations else None,
        'turn_seconds_p90': round(_nearest_rank(durations, 0.9), 1) if durations else None,
        'turn_seconds_max': round(durations[-1], 1) if durations else None,
        'slowest_turn': slowest,
        'repeat_failures': len(repeats),
        'repeat_failure_tools': sorted({name for (_turn, name) in repeats}),
        'repeat_failure_detail': [
            {'turn': turn, 'tool': name, 'failures': count}
            for (turn, name), count in sorted(repeats.items(), key=lambda item: (-item[1], item[0][0]))
        ],
    }


def posture(grade, config):
    """A coarse label from explicit rules, never a composite score.

    The order is the requirement: a judge problem outranks a throughput
    problem, and an unjudged session is reported as unknown rather than
    averaged in as if it had been measured.
    """
    if not grade.get('readable'):
        return 'unreadable'
    graded = grade['graded']
    if not graded:
        return 'unjudged'
    answered = grade['judge_answered'] / graded
    if answered < 0.5:
        return 'judge-degraded'
    if grade['claims_unsupported'] / graded >= 0.25:
        return 'claims-unreliable'
    if grade['tool_calls'] and grade['tool_nonzero'] / grade['tool_calls'] >= 0.1:
        return 'tool-failures'
    if grade['turn_ends'] and grade['turns_failed'] / grade['turn_ends'] >= 0.25:
        return 'turn-failures'
    return 'steady'


def _ceiling_finding(grades, config):
    """Say plainly when a session's passes sit on the heuristic's own ceiling.

    Not an accusation against the session. It is a statement about the
    instrument: at these thresholds the heuristic has one passing value.

    Returns (headline, detail) or None. The headline exists because the detail
    is a paragraph: on the page it was set as a heading, so eleven rows of prose
    read as one wall and a reader could not tell a fault from a caveat.
    """
    heuristic = [grade for grade in grades if grade.get('heuristic_scores')]
    if not heuristic:
        return None
    observed = sorted({score for grade in heuristic for score in grade['heuristic_scores']})
    if not observed or not set(observed) <= set(HEURISTIC_SCORES):
        return None
    passing = [score for score in observed if score >= config.jev.goal_threshold]
    if not passing:
        return ('No heuristic value can pass',
                f'{len(heuristic)} session(s) were graded by the heuristic and none of its '
                f'reachable completion values ({", ".join(str(s) for s in observed)}) reaches '
                f'goal_threshold {config.jev.goal_threshold}. Those refusals are arithmetic, '
                'not judgement.')
    if len(passing) == 1 and passing[0] == max(HEURISTIC_SCORES):
        return (f'Heuristic can only pass at {passing[0]:.2f}',
                f'Heuristic-graded turns only ever pass at {passing[0]:.2f}, which is the '
                'heuristic\'s single passing value. A pass rate from this instrument does not '
                'separate good turns from poor ones; read the structural facts instead.')
    return None


def oversight_report(config, session_roots, *, byte_limit=None, trials=None):
    """Quality-first oversight projection across sessions. Read-only."""
    trials = trials or []
    grades = [grade_session(root, config, byte_limit=byte_limit) for root in session_roots]
    grades = [grade for grade in grades if grade.get('slug')]
    for grade in grades:
        grade['posture'] = posture(grade, config)

    # The same precedence posture() uses: a broken instrument outranks a claim
    # problem, because a claim reading taken through a fallback judge is not
    # trustworthy evidence in the first place.
    rank = {'unreadable': 0, 'unjudged': 1, 'judge-degraded': 2, 'claims-unreliable': 3,
            'tool-failures': 4, 'turn-failures': 5, 'steady': 6}
    # Quality first: the worst posture leads, because that is what needs attention.
    ordered = sorted(grades, key=lambda grade: (rank.get(grade['posture'], 0), grade['slug']))

    # Each finding is a headline plus its detail, so a caveat does not look
    # like a fault and a row is not a wall of prose. `findings` stays a list
    # of the detail strings, because the lane's tests and a session-side
    # reader join it as text. `findings_detail` carries the structure the
    # page needs.
    findings_detail = []

    def finding(kind, headline, detail, tone='attention'):
        findings_detail.append({'kind': kind, 'headline': headline, 'detail': detail, 'tone': tone})

    ceiling = _ceiling_finding(grades, config)
    if ceiling:
        finding('instrument', ceiling[0], ceiling[1])
    degraded = [grade['slug'] for grade in grades if grade['posture'] == 'judge-degraded']
    if degraded:
        finding('judge', 'Judge fell back in %d of %d sessions' % (len(degraded), len(grades)),
                'Judge fell back on most turns in: ' + ', '.join(degraded)
                + '. Their grades came from the heuristic, not TypeSafe.')
    unexamined = sum(grade.get('claims_unexamined') or 0 for grade in grades)
    if unexamined:
        finding('refused', '%d turns refused before judging' % unexamined,
                f'{unexamined} turn(s) were refused by a deterministic check before the '
                'reply was judged, so no claim reading exists for them. They are counted '
                'as refused, not as unsupported claims.')
    unreadable = [grade['slug'] for grade in grades if grade['posture'] == 'unreadable']
    if unreadable:
        finding('unreadable', '%d session log(s) not readable' % len(unreadable),
                'Events not readable (missing, non-regular, or unparsable): '
                + ', '.join(unreadable))

    # Retry pressure is a named finding, not a trigger. A tool failing twice in
    # one turn is the shape of a loop; a person reads it and decides. Nothing
    # here changes a session's settings, backend, or effort.
    loops = [(grade['slug'], grade.get('repeat_failure_detail') or []) for grade in grades
             if grade.get('repeat_failures')]
    if loops:
        # One line, one session, its worst repeat. A full list of every turn
        # would be a wall of text that a reader skips. The detail stays in
        # each session row.
        named = ', '.join(f"{slug} ({details[0]['tool']} failed {details[0]['failures']}x "
                          f"in turn {details[0]['turn']})" for slug, details in loops)
        total = sum(grade.get('repeat_failures') or 0 for grade in grades)
        finding('retry', '%d turns retried a failing tool' % total,
                f'{total} turn(s) had a tool fail twice or more: ' + named
                + '. That is retry pressure, not a quality verdict; no adjustment was made.')
    slow = [(grade['slug'], grade.get('slowest_turn') or {}) for grade in grades
            if (grade.get('slowest_turn') or {}).get('seconds')]
    if slow:
        worst = max(slow, key=lambda item: item[1]['seconds'])
        finding('speed', 'Slowest turn: %.0fs in %s' % (worst[1]['seconds'], worst[0]),
                f"Slowest single turn: {worst[0]} turn {worst[1]['turn']} at "
                f"{worst[1]['seconds']:.0f}s with {worst[1]['tool_calls']} tool calls. "
                'Turn spans are measured from recorded message dates, so they include '
                'waiting, not only model time.')

    totals = {
        'sessions': len(grades),
        'graded': sum(grade.get('graded') or 0 for grade in grades),
        'judge_answered': sum(grade.get('judge_answered') or 0 for grade in grades),
        'refused_deterministic': sum(grade.get('refused_deterministic') or 0 for grade in grades),
        'claims_unsupported': sum(grade.get('claims_unsupported') or 0 for grade in grades),
        'claims_unexamined': sum(grade.get('claims_unexamined') or 0 for grade in grades),
        'tool_calls': sum(grade.get('tool_calls') or 0 for grade in grades),
        'tool_nonzero': sum(grade.get('tool_nonzero') or 0 for grade in grades),
        'repeat_failures': sum(grade.get('repeat_failures') or 0 for grade in grades),
        'turns_timed': sum(grade.get('turns_timed') or 0 for grade in grades),
    }

    policy = getattr(config.autonomy, 'oversight_policy', 'approve')
    # The policy is a trust dial the operator sets in Settings. It decides what
    # the lane does with a reading, never whether the reading is taken.
    if policy == 'approve':
        # Try before adopt: every finding that names a changeable pressure
        # becomes something the operator can accept or reject. The report says
        # so plainly rather than implying an adjustment already happened.
        proposals = _proposals(loops, slow)
        if proposals:
            finding('proposal', '%d proposals await your decision' % len(proposals),
                    f'{len(proposals)} proposal(s) await your decision. Nothing is '
                    'applied until you accept one.')
    elif policy == 'auto':
        # Highest trust: a change is still only adopted after an A/B measurement
        # shows it is better. The trials are that measurement, so this branch
        # reports their state rather than asserting a precondition nobody meets.
        # A reading alone still never adopts anything: only a decided win does,
        # and the win is decided from structural facts, not a judge score.
        open_trials = [t for t in trials if t.get('phase') != 'decided']
        if open_trials:
            finding('trial', '%d trial(s) measuring a change' % len(open_trials),
                    f'{len(open_trials)} trial(s) are measuring a change: '
                    + '; '.join(_trial_line(t) for t in open_trials)
                    + '. A change is kept only if its arm wins on the named instrument.')
        else:
            from .trial import RATE_MARGIN
            finding('trial', 'Automatic application is on; nothing adopts a reading',
                    'Automatic application is enabled, but a reading alone never adopts '
                    'a change. Start a trial at POST /api/trials: name the session, the change, '
                    'the number of turns per arm, and the instrument that can tell the arms '
                    f'apart. The default win margin is {RATE_MARGIN:.0%}: a rate or duration win '
                    'keeps the change only when the candidate is at most that fraction of the '
                    'baseline, and that margin is stored on the trial before it runs. '
                    'Until a trial wins, nothing is applied.', tone='')
        proposals = []
    else:
        proposals = []

    findings = [item['detail'] for item in findings_detail]
    from .trial import annotate_trial

    return {
        'enabled': True,
        'policy': policy,
        'posture_counts': _counts(grade['posture'] for grade in grades),
        'totals': totals,
        'sessions': ordered,
        'findings': findings,
        # The same findings with a headline, so the page can lead with the fact
        # instead of the paragraph. `findings` above is unchanged for every reader
        # that joins it as text.
        'findings_detail': findings_detail,
        'proposals': proposals,
        # The measurements 'auto' waits for, described so a reader can check the
        # claim rather than take the finding's word for it.
        'trials': [annotate_trial(dict(trial, line=_trial_line(trial))) for trial in trials],
        'limits': [label + ': ' + text for label, text in _LIMITS],
        # Labelled the same way, so a caveat is never set in the same voice as a fault.
        'limits_detail': [{'label': label, 'text': text} for label, text in _LIMITS],
    }


_LIMITS = [
    ('Scope',
     'This grades structure only: tool outcomes, judge source, claim/evidence mismatch. '
     'It does not judge whether the work was worth doing.'),
    ('Window',
     'It reads the newest bytes of each events log, so a very long session is graded on '
     'its recent turns rather than its whole history.'),
    ('Ranking',
     'The judge completion score is reported but never used to rank a session, because '
     'the heuristic cannot discriminate at the configured thresholds.'),
    ('Timing',
     'Turn spans come from the dates on recorded messages, so they measure elapsed time '
     'including waiting between calls, not model latency alone.'),
    ('Authority',
     'This lane never changes a session on a reading alone. Under "ask before applying" '
     'a finding becomes a proposal you accept or reject; under automatic application it '
     'still waits for an A/B measurement to show the change is better.'),
]


def _counts(values):
    counts = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return counts


def _trial_line(trial):
    """One trial, in the terms the reader needs: what changes, what measures it, and the margin."""
    from .trial import margin_sentence
    return (f"{trial.get('slug')} {trial.get('lever')} {trial.get('from')} -> {trial.get('to')} "
            f"in its {trial.get('phase')} arm, read by "
            f"{str(trial.get('instrument') or '').replace('_', ' ')}. "
            + margin_sentence(trial))


def _proposals(loops, slow):
    """Name the changes a person could choose, without choosing for them.

    A proposal is a question, not an action: it says what was measured and what
    could be changed, and it carries no switch, effort or backend value, because
    the operator decides that. Under the 'approve' policy these are what the
    card offers; nothing here applies them.
    """
    proposals = []
    for slug, details in loops:
        worst = details[0]
        proposals.append({
            'session': slug,
            'kind': 'retry-pressure',
            'reading': f"{worst['tool']} failed {worst['failures']}x in turn {worst['turn']}",
            'question': 'This session repeated a failing tool. Do you want to change how that '
                        'tool is called here, or leave it as it is?',
        })
    for slug, worst in slow:
        proposals.append({
            'session': slug,
            'kind': 'slow-turn',
            'reading': f"turn {worst['turn']} took {worst['seconds']:.0f}s across "
                       f"{worst['tool_calls']} tool calls",
            'question': 'This turn was slow. Turn spans include waiting, not only model time. '
                        'Do you want to look at what it was waiting on, or leave it?',
        })
    return proposals
