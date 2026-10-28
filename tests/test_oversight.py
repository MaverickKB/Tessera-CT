"""Oversight grades structure and reports the judge's blind spot honestly.

The fallback can only return 0.0, 0.1, 0.65 or 0.9 for completion, and the
gate needs >= 0.8, so 0.65 can never pass. These tests pin that arithmetic
so the oversight finding cannot silently become false, and pin that
oversight never ranks a session by the judge score.
"""
import json
from pathlib import Path

from tessera.config import Config
from tessera.jev import JudgeKit
from tessera.oversight import grade_session, oversight_report, posture, HEURISTIC_SCORES
from tessera.sessions import SessionManager


def kit():
    # The thresholds these tests pin.
    return JudgeKit(enabled=False, goal_threshold=0.8, gate_threshold=0.7)


def test_the_heuristic_has_exactly_one_passing_value_at_live_thresholds():
    judge = kit()
    passed = []
    for score in HEURISTIC_SCORES:
        values = {'independent': 1, 'merge_conflict': 0.1, 'goal_met': score,
                  'looping': 0, 'escalate_to_human': 0.05, 'source': 'heuristic'}
        if judge._gate_result(dict(values), phase='session', human=None)['allowed']:
            passed.append(score)
    assert passed == [0.9]
    assert 0.65 not in passed


def test_the_recorded_live_shape_is_refused_by_arithmetic():
    # The exact recorded gate from glm-rsi-watch turns 3..6.
    values = {'independent': 1, 'merge_conflict': 0.1, 'goal_met': 0.65,
              'looping': 0, 'escalate_to_human': 0.05, 'source': 'heuristic'}
    assert kit()._gate_result(dict(values), phase='session', human=None)['allowed'] is False


def write_events(root, rows):
    root.mkdir(parents=True, exist_ok=True)
    path = root / 'events.jsonl'
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows))


def agent_row(turn, score, source, allowed, gate_source, claims=None, done_but_failed=None):
    return {'event': 'message', 'data': {
        'kind': 'agent', 'text': 'a reply', 'date': f'2026-10-24T00:0{turn}:00+00:00',
        'meta': {'turn': turn, 'judge_score': score, 'judge_source': source,
                 'output_gate': {'allowed': allowed, 'source': gate_source},
                 'verification': {'claims_supported': claims, 'claims_done_but_failed': done_but_failed}}}}


def test_a_session_whose_judge_fell_back_is_named_degraded_not_ranked(tmp_path):
    root = tmp_path / 'sessions' / 'fallback-subject'
    write_events(root, [
        agent_row(1, 0.65, 'heuristic', False, 'heuristic', 0.9, 0.05),
        agent_row(2, 0.65, 'heuristic', False, 'heuristic', 0.9, 0.05),
        agent_row(3, 0.9, 'jev', True, 'jev', 0.95, 0.02),
        {'event': 'turn_end', 'data': {'turn_id': 'a', 'success': True, 'interrupted': False}},
    ])
    config = Config(home=tmp_path)
    grade = grade_session(root, config)
    assert grade['graded'] == 3
    assert grade['judge_answered'] == 1
    assert grade['judge_sources'] == {'heuristic': 2, 'jev': 1}
    assert grade['heuristic_scores'] == [0.65]
    # Judge answered only 1 of 3: degraded, and the reason is the judge.
    assert posture(grade, config) == 'judge-degraded'


def test_the_report_says_the_refusals_are_arithmetic_not_judgement(tmp_path):
    root = tmp_path / 'sessions' / 'heuristic-subject'
    write_events(root, [agent_row(1, 0.65, 'heuristic', False, 'heuristic', 0.9, 0.05)])
    config = Config(home=tmp_path)
    report = oversight_report(config, [root])
    joined = ' '.join(report['findings'])
    assert 'arithmetic' in joined
    assert '0.65' in joined or 'goal_threshold' in joined


def test_a_session_without_graded_turns_is_unjudged_not_steady(tmp_path):
    root = tmp_path / 'sessions' / 'quiet-subject'
    write_events(root, [{'event': 'turn_end', 'data': {'turn_id': 'a', 'success': True}}])
    config = Config(home=tmp_path)
    assert posture(grade_session(root, config), config) == 'unjudged'


def test_a_missing_events_log_is_unreadable_not_zero_scored(tmp_path):
    root = tmp_path / 'sessions' / 'empty-subject'
    root.mkdir(parents=True)
    config = Config(home=tmp_path)
    grade = grade_session(root, config)
    assert grade['readable'] is True  # an absent log is an empty read, not a failure
    assert grade['graded'] == 0
    assert posture(grade, config) == 'unjudged'


def test_claims_that_outran_evidence_lead_the_posture(tmp_path):
    root = tmp_path / 'sessions' / 'claims-subject'
    write_events(root, [
        agent_row(1, 0.9, 'jev', True, 'jev', 0.2, 0.9),
        agent_row(2, 0.9, 'jev', True, 'jev', 0.9, 0.05),
    ])
    config = Config(home=tmp_path)
    grade = grade_session(root, config)
    assert grade['claims_unsupported'] == 1
    assert posture(grade, config) == 'claims-unreliable'


def test_a_deterministic_refusal_is_not_counted_as_a_claim_judgment(tmp_path):
    """jev.py:338-341 hard-codes claims_done_but_failed=1 on this path.

    That literal means "a tool failed"; the reply is never examined. Counting
    it as a claim reading would report a constant as evidence -- the exact
    failure ls-2708a8 names.
    """
    root = tmp_path / 'sessions' / 'deterministic-subject'
    write_events(root, [
        agent_row(1, 0, 'deterministic', False, 'deterministic', 0, 1),
        agent_row(2, 0, 'deterministic', False, 'deterministic', 0, 1),
    ])
    config = Config(home=tmp_path)
    grade = grade_session(root, config)
    assert grade['refused_deterministic'] == 2
    assert grade['claims_unexamined'] == 2
    assert grade['claims_unsupported'] == 0
    report = oversight_report(config, [root])
    assert 'not as unsupported claims' in ' '.join(report['findings'])


def test_tool_outcomes_are_read_from_echo_exit_codes(tmp_path):
    root = tmp_path / 'sessions' / 'tools-subject'
    rows = [
        {'event': 'message', 'data': {'kind': 'tool', 'meta': {'turn': 1}, 'text': '{"name": "shell"}'}},
        {'event': 'message', 'data': {'kind': 'echo', 'meta': {'turn': 1},
                                      'text': json.dumps({'exit_code': 0, 'output': 'ok'})}},
        {'event': 'message', 'data': {'kind': 'echo', 'meta': {'turn': 1},
                                      'text': json.dumps({'exit_code': 2, 'output': 'bad'})}},
        agent_row(1, 0.9, 'jev', True, 'jev', 0.95, 0.01),
    ]
    write_events(root, rows)
    config = Config(home=tmp_path)
    grade = grade_session(root, config)
    assert grade['tool_calls'] == 1
    assert grade['tool_nonzero'] == 1


def test_the_report_is_read_only_and_touches_no_storage(tmp_path):
    root = tmp_path / 'sessions' / 'stable-subject'
    write_events(root, [agent_row(1, 0.9, 'jev', True, 'jev', 0.95, 0.01)])
    config = Config(home=tmp_path)
    before = {path: path.read_bytes() for path in tmp_path.rglob('*') if path.is_file()}
    oversight_report(config, [root])
    after = {path: path.read_bytes() for path in tmp_path.rglob('*') if path.is_file()}
    assert before == after


def test_the_route_answers_on_the_live_server(tmp_path):
    from test_server import call, running_server
    with running_server(tmp_path) as server:
        status, body = call(server, 'GET', '/api/oversight')
    assert status == 200
    assert body['enabled'] is True
    assert 'sessions' in body and 'findings' in body and 'limits' in body


def test_a_session_can_read_its_own_oversight_through_a_tool(tmp_path):
    """The lane is reachable from inside a session, not only from the page."""
    from fake_backend import FakeBackend
    config = Config(home=tmp_path, cwd=tmp_path)
    config.server.port = 0
    with SessionManager(config, backend_factory=lambda slug, name: FakeBackend(['ok'])) as manager:
        manager.create('Tool subject')
        slug = manager.list_sessions()[0]['slug']
        names = manager.runtime(slug, internal=True).session.tools.names()
    assert 'session_quality' in names


def timed_row(turn, kind, date, text='', meta=None):
    data = {'kind': kind, 'date': date, 'meta': meta if meta is not None else {'turn': turn}}
    if text:
        data['text'] = text
    return {'event': 'message', 'data': data}


def test_a_turn_span_is_measured_from_the_dates_on_its_own_records(tmp_path):
    """A turn span is read from the dates on its own records, not guessed.

    turn_start/turn_end records carry no timestamp, so the only wall-clock
    reading in the log is the date on each message. A span is therefore the
    distance between a turn's first and last recorded message, which includes
    waiting and is stated as such rather than as model latency.
    """
    root = tmp_path / 'sessions' / 'speed-subject'
    write_events(root, [
        timed_row(1, 'agent', '2026-10-24T00:00:00+00:00', 'start'),
        timed_row(1, 'agent', '2026-10-24T00:00:30+00:00', 'end'),
        timed_row(2, 'agent', '2026-10-24T00:10:00+00:00', 'start'),
        timed_row(2, 'agent', '2026-10-24T00:12:00+00:00', 'end'),
    ])
    config = Config(home=tmp_path)
    grade = grade_session(root, config)
    assert grade['turns_timed'] == 2
    assert grade['turn_seconds_median'] == 30.0
    assert grade['turn_seconds_max'] == 120.0
    assert grade['slowest_turn'] == {'turn': 2, 'seconds': 120.0, 'tool_calls': 0}


def test_a_tool_failing_twice_in_one_turn_is_a_retry_loop_and_names_the_tool(tmp_path):
    """Retry pressure, not a quality verdict. The finding must say so."""
    root = tmp_path / 'sessions' / 'loop-subject'
    write_events(root, [
        timed_row(3, 'tool', '2026-10-24T00:00:00+00:00', json.dumps({'name': 'shell', 'arguments': {}})),
        timed_row(3, 'echo', '2026-10-24T00:00:01+00:00', json.dumps({'exit_code': 2, 'output': ''})),
        timed_row(3, 'tool', '2026-10-24T00:00:02+00:00', json.dumps({'name': 'shell', 'arguments': {}})),
        timed_row(3, 'echo', '2026-10-24T00:00:03+00:00', json.dumps({'exit_code': 1, 'output': ''})),
        # A single failure is not a loop.
        timed_row(4, 'tool', '2026-10-24T00:01:00+00:00', json.dumps({'name': 'board_note', 'arguments': {}})),
        timed_row(4, 'echo', '2026-10-24T00:01:01+00:00', json.dumps({'exit_code': 1, 'output': ''})),
    ])
    config = Config(home=tmp_path)
    grade = grade_session(root, config)
    assert grade['repeat_failures'] == 1
    assert grade['repeat_failure_detail'] == [{'turn': 3, 'tool': 'shell', 'failures': 2}]
    assert grade['repeat_failure_tools'] == ['shell']

    report = oversight_report(config, [root])
    joined = ' '.join(report['findings'])
    assert 'shell failed 2x in turn 3' in joined
    assert 'no adjustment was made' in joined
    # The lane reports; it does not tune. Nothing in the payload is a proposal.
    assert not any('switch' in key or 'effort' in key for key in report['totals'])


# The oversight policy is a trust dial the operator sets in Settings.
# The default is 'approve': a reading becomes something the operator accepts
# or rejects. These pin that default and the rule that survives every setting:
# a reading alone never adopts a change.

def _loop_session(tmp_path):
    root = tmp_path / 'sessions' / 'loop-subject'
    write_events(root, [
        timed_row(3, 'tool', '2026-10-24T00:00:00+00:00', json.dumps({'name': 'shell', 'arguments': {}})),
        timed_row(3, 'echo', '2026-10-24T00:00:01+00:00', json.dumps({'exit_code': 2, 'output': ''})),
        timed_row(3, 'tool', '2026-10-24T00:00:02+00:00', json.dumps({'name': 'shell', 'arguments': {}})),
        timed_row(3, 'echo', '2026-10-24T00:00:03+00:00', json.dumps({'exit_code': 1, 'output': ''})),
    ])
    return root


def test_the_default_oversight_policy_is_ask_before_applying():
    """B is the default: try before adopt, not silence and not auto."""
    assert Config().autonomy.oversight_policy == 'approve'


def test_ask_before_applying_turns_a_reading_into_a_question_not_an_action(tmp_path):
    root = _loop_session(tmp_path)
    config = Config(home=tmp_path)
    report = oversight_report(config, [root])
    assert report['policy'] == 'approve'
    assert report['proposals'], 'a measured retry loop must offer a decision'
    proposal = report['proposals'][0]
    assert proposal['session'] == 'loop-subject' and proposal['kind'] == 'retry-pressure'
    assert 'shell failed 2x in turn 3' in proposal['reading']
    assert proposal['question'].endswith('?')
    # A proposal is a question, not a setting: no switch, effort or backend value.
    assert not any('switch' in key or 'effort' in key or 'backend' in key for key in proposal)
    assert 'await your decision' in ' '.join(report['findings'])


def test_naming_it_changes_nothing_and_offers_nothing(tmp_path):
    root = _loop_session(tmp_path)
    config = Config(home=tmp_path)
    config.autonomy.oversight_policy = 'findings'
    report = oversight_report(config, [root])
    assert report['policy'] == 'findings'
    assert report['proposals'] == []
    assert 'shell failed 2x in turn 3' in ' '.join(report['findings'])
    assert 'no adjustment was made' in ' '.join(report['findings'])


def test_automatic_application_still_waits_for_an_ab_measurement(tmp_path):
    """Highest trust is not a license to adopt on a reading alone.

    The sentence points at the trials that do the measuring, so this pins the
    guarantee rather than one phrasing of it: with no trial, the lane applies
    nothing, offers nothing, and says what a measurement would need.
    """
    root = _loop_session(tmp_path)
    config = Config(home=tmp_path)
    config.autonomy.oversight_policy = 'auto'
    report = oversight_report(config, [root])
    assert report['policy'] == 'auto'
    joined = ' '.join(report['findings'])
    assert 'a reading alone never adopts' in joined
    assert 'Start a trial' in joined
    assert 'nothing is applied' in joined
    assert 'default win margin is 80%' in joined
    # Nothing is applied here: the lane reports the requirement, it does not act.
    assert not any('applied' in key for key in report['totals'])
    assert report['proposals'] == []


def test_auto_reports_the_trials_it_is_measuring(tmp_path):
    """With a trial open, 'auto' reports the measurement instead of a bare caveat."""
    root = _loop_session(tmp_path)
    config = Config(home=tmp_path)
    config.autonomy.oversight_policy = 'auto'
    trials = [{'id': 't1', 'slug': 'loop-subject', 'lever': 'reasoning',
               'from': 'low', 'to': 'high', 'phase': 'candidate',
               'instrument': 'repeat_failures'}]
    report = oversight_report(config, [root], trials=trials)
    joined = ' '.join(report['findings'])
    assert '1 trial(s) are measuring a change' in joined
    assert 'reasoning low -> high' in joined
    assert 'repeat failures' in joined
    assert 'at most 80% of the baseline' in joined


def test_an_unknown_policy_is_refused_at_config_validation(tmp_path):
    config = Config(home=tmp_path)
    config.autonomy.oversight_policy = 'yolo'
    try:
        config.validate()
    except ValueError as error:
        assert 'autonomy' in str(error).lower()
    else:
        raise AssertionError('an unknown oversight policy must be refused')
