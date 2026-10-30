"""A review is useful only when its advice is tied to actual supplied evidence."""

from concurrent.futures import ThreadPoolExecutor
import json
from types import SimpleNamespace

import pytest

from tessera.backend import BackendError
from tessera.jev import JudgeKit
from tessera.jev_review import AdvisoryReviewer, CHECKPOINTS


def settings(**changes):
    values = {checkpoint + '_review_enabled': False for checkpoint in CHECKPOINTS}
    values.update(completion_review_enabled=True, review_max_calls_per_turn=2,
                  review_max_input_chars=24000)
    values.update(changes)
    return SimpleNamespace(**values)


def answers_for(questions, *, status='supported', citation='reply'):
    answers = {}
    for name, question in questions.items():
        selected = status if name.startswith('check_') else citation
        answers[name] = {'type': 'choice', 'choice': selected, 'confidence': 1,
                         'probabilities': {key: float(key == selected)
                                           for key in question['criteria']}}
    return {'answers': answers}


def reviewer(callback=None, **changes):
    calls = []

    def evaluate(state, questions):
        calls.append((state, questions))
        if callback:
            return callback(state, questions)
        return answers_for(questions)

    return AdvisoryReviewer(JudgeKit(enabled=True, evaluator=evaluate), settings(**changes)), calls


@pytest.mark.parametrize('checkpoint', CHECKPOINTS)
def test_each_checkpoint_is_independently_opted_in(checkpoint):
    flags = {name + '_review_enabled': False for name in CHECKPOINTS}
    review, calls = reviewer(**flags)
    assert not review.enabled(checkpoint)
    assert review.review(checkpoint, 'Preserve behavior')['state'] == 'skipped'
    assert not calls
    setattr(review.settings, checkpoint + '_review_enabled', True)
    assert review.enabled(checkpoint)
    assert review.review(checkpoint, 'Preserve behavior', reply='A proposal')['state'] == 'reviewed'
    assert len(calls) == 1


@pytest.mark.parametrize('enabled', [False, True])
def test_no_judge_attempt_when_disabled_or_unconfigured(enabled, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('No judge calls without configuration')

    monkeypatch.setattr(JudgeKit, '_batch', forbidden)
    review = AdvisoryReviewer(JudgeKit(enabled=enabled), settings())
    assert not review.enabled('completion')
    result = review.review('completion', 'Keep the requested functionality')
    assert result['state'] == 'skipped'
    assert result['advisory'] is True


def test_request_and_actual_diff_are_reviewed_as_separate_dimensions():
    def evaluate(state, questions):
        assert state['request'] == 'The Stop button must stop the server.'
        assert state['evidence'][1]['id'] == 'diff'
        assert 'remove_stop_button' in state['evidence'][1]['content']
        result = answers_for(questions, citation='diff')
        for name, answer in result['answers'].items():
            if name.startswith('check_'):
                answer['choice'] = 'concern'
                answer['probabilities'] = {key: float(key == 'concern')
                                           for key in questions[name]['criteria']}
        return result

    review, calls = reviewer(evaluate)
    result = review.review('completion', 'The Stop button must stop the server.',
                           reply='Fixed.', diff='-stop_server()\n+remove_stop_button()',
                           requirements=['Stop must stop the server.'])
    assert result['state'] == 'reviewed'
    assert {item['dimension'] for item in result['assessments']} == {
        'requested_outcome', 'behavior_preservation', 'claims_support'}
    assert all(item['evidence_ids'] == ['diff'] for item in result['findings'])
    assert all(item['requirement'] and item['remedy'] for item in result['findings'])
    assert result['advisory'] is True
    assert 'allowed' not in result and 'success' not in result
    assert len(calls) == 1


@pytest.mark.parametrize('failure', [
    {'success': False}, {'exit_code': 1}, {'blocked': True},
    {'error': 'build failed'}, {'result': {'exit_code': 2}},
])
def test_model_approval_cannot_erase_failed_tool_evidence(failure):
    review, _ = reviewer()
    result = review.review('completion', 'Build the application.', reply='Done.',
                           tool_outputs=[{'name': 'shell', **failure}])
    assert result['facts'] == [{'id': 'tool_failure:1', 'evidence_id': 'tool:1',
                                'tools_failed': True}]
    fact = next(item for item in result['findings'] if item['kind'] == 'fact')
    assert fact['source'] == 'tool_result'
    assert fact['status'] == 'concern' and fact['evidence_ids'] == ['tool:1']
    # The report is separate from the actual command and never changes its result.
    assert result['evidence'][1]['content'] == json.dumps(
        {'name': 'shell', **failure}, ensure_ascii=False, sort_keys=True)


def test_made_up_citations_are_unavailable_not_actionable_findings():
    def invalid_citation(state, questions):
        result = answers_for(questions, status='concern')
        result['answers']['evidence_0']['choice'] = 'imaginary-file'
        return result

    review, _ = reviewer(invalid_citation)
    result = review.review('completion', 'Keep controls usable.', reply='Done.')
    assert result['state'] == 'partial'
    first = result['assessments'][0]
    assert first['status'] == 'uncertain' and first['source'] == 'unavailable'
    assert not first['evidence_ids']
    assert all(finding['id'] != first['id'] for finding in result['findings'])


def test_judge_outage_never_produces_heuristic_review_or_hides_failure_facts():
    def unavailable(state, questions):
        raise BackendError('Private remote details')

    review, _ = reviewer(unavailable)
    result = review.review('completion', 'Build.', reply='Done.', tool_outputs=[{'exit_code': 2}])
    assert result['state'] == result['source'] == 'unavailable'
    assert all(item['status'] == 'uncertain' for item in result['assessments'])
    assert result['facts'][0]['tools_failed'] is True
    assert 'Private remote details' not in json.dumps(result)


def test_absent_evidence_cannot_be_cited_as_success():
    review, _ = reviewer(lambda state, questions: answers_for(questions, citation='none'))
    result = review.review('completion', 'Run and verify the build.')
    assert result['evidence'][0]['kind'] == 'absence'
    assert all(item['status'] == 'uncertain' for item in result['assessments'])
    assert not result['findings']


@pytest.mark.parametrize('status', ['supported', 'concern'])
def test_absence_record_never_supports_model_judgments(status):
    review, _ = reviewer(lambda state, questions: answers_for(
        questions, status=status, citation='evidence_manifest'))
    result = review.review('completion', 'Run and verify the build.')
    assert all(item['status'] == 'uncertain' for item in result['assessments'])
    assert not result['findings']


def test_reply_alone_can_support_prose_but_not_execution_claims():
    review, _ = reviewer()
    prose = review.review('completion', 'Explain how Python works.', reply='Python is a programming language.')
    assert all(item['status'] == 'supported' for item in prose['assessments'])
    claimed = review.review('completion', 'Run the tests.', reply='All tests passed.')
    support = next(item for item in claimed['assessments'] if item['dimension'] == 'claims_support')
    assert support['status'] == 'uncertain'
    assert 'does not corroborate' in support['limitation']


def test_full_review_input_is_bounded_and_truncation_cannot_prove_success():
    cap = 10000

    def evaluate(state, questions):
        assert len(json.dumps({'state': state, 'questions': questions}, ensure_ascii=False)) <= cap
        return answers_for(questions, citation='diff')

    review, _ = reviewer(evaluate, review_max_input_chars=cap)
    result = review.review('completion', 'Keep behavior.', reply='Done.', diff='change\n' * 50000)
    assert result['truncated'] is True and result['state'] == 'partial'
    assert all(item['status'] == 'uncertain' for item in result['assessments'])
    assert result['omitted_inputs']


def test_truncated_requirements_are_labeled_and_cannot_create_findings():
    review, _ = reviewer(lambda state, questions: answers_for(
        questions, status='concern'), review_max_input_chars=10000)
    result = review.review('completion', 'Keep behavior.' * 50000, reply='Done.')
    assert result['state'] == 'partial'
    assert result['assessments'][0]['requirement_complete'] is False
    assert all(item['status'] == 'uncertain' for item in result['assessments'])
    assert not result['findings']


def test_budget_is_shared_by_concurrent_checkpoints_and_resets_for_next_turn():
    review, calls = reviewer(plan_review_enabled=True, handoff_review_enabled=True)
    jobs = [('completion', 'Complete.'), ('plan', 'Plan.'), ('handoff', 'Handoff.')]
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda item: review.review(*item, reply='Evidence.'), jobs))
    assert len(calls) == 2
    assert sum(item['state'] == 'skipped' for item in results) == 1
    assert next(item['reason'] for item in results if item['state'] == 'skipped') == 'Review budget exhausted'
    review.begin_turn()
    assert review.review('completion', 'Another turn.', reply='Evidence.')['state'] == 'reviewed'
    assert len(calls) == 3


def test_budget_counts_unavailable_attempts_and_tiny_inputs_do_not_call_judge():
    review, calls = reviewer(lambda state, questions: {'answers': {}}, review_max_calls_per_turn=1)
    assert review.review('completion', 'Build.', reply='Done.')['state'] == 'unavailable'
    assert review.review('completion', 'Retry.', reply='Done.')['state'] == 'skipped'
    assert len(calls) == 1
    tiny, calls = reviewer(review_max_input_chars=32)
    assert tiny.review('completion', 'Build.', reply='Done.')['state'] == 'skipped'
    assert not calls


def test_cancellation_is_not_converted_into_an_advisory_result():
    def cancelled(state, questions):
        raise KeyboardInterrupt()

    review, _ = reviewer(cancelled)
    with pytest.raises(KeyboardInterrupt):
        review.review('completion', 'Build.', reply='Done.')


def test_synthetic_worker_report_is_a_claim_not_execution_evidence():
    review, _ = reviewer(lambda state, questions: answers_for(questions, citation='tool:1'),
                          handoff_review_enabled=True)
    result = review.review('handoff', 'Run and verify the build.', reply='All checks passed.',
                           tool_outputs=[{'name': 'worker_report', 'success': True,
                                          'result': 'All checks passed.'}])
    assert result['evidence'][1]['kind'] == 'worker_report'
    for item in result['assessments']:
        if item['dimension'] in ('requested_outcome', 'claims_support'):
            assert item['status'] == 'uncertain'
            assert 'repeats a claim' in item['limitation']


def test_actual_worker_tool_output_can_corroborate_execution():
    review, _ = reviewer(lambda state, questions: answers_for(questions, citation='tool:2'),
                          handoff_review_enabled=True)
    result = review.review('handoff', 'Run and verify the build.', reply='All checks passed.',
                           tool_outputs=[{'name': 'worker_report', 'success': True,
                                          'result': 'All checks passed.'},
                                         {'name': 'worker_tool_output', 'success': True,
                                          'result': 'Build completed with exit code 0.'}])
    assert result['evidence'][2]['kind'] == 'tool'
    assert all(item['status'] == 'supported' for item in result['assessments'])


@pytest.mark.parametrize('state', ['unavailable', 'empty', 'invalid'])
@pytest.mark.parametrize('status', ['supported', 'concern'])
def test_unavailable_or_empty_diff_is_absence_not_proof(state, status):
    review, _ = reviewer(lambda packet, questions: answers_for(
        questions, status=status, citation='diff'))
    result = review.review('completion', 'Modify the checkout while preserving its behavior.',
                           reply='Done.', diff={'state': state, 'text': '',
                                                'limitations': 'No readable tracked-file changes.'})
    assert next(item for item in result['evidence'] if item['id'] == 'diff')['kind'] == 'absence'
    assert all(item['status'] == 'uncertain' for item in result['assessments'])
    assert not result['findings']


def test_partial_diff_marks_incomplete_evidence_without_a_positive_verdict():
    review, _ = reviewer(lambda state, questions: answers_for(questions, citation='diff'))
    result = review.review('completion', 'Preserve the requested behavior.', reply='Done.',
                           diff={'state': 'partial', 'text': '+ only a fragment',
                                 'limitations': 'The patch exceeded the capture limit.'})
    assert result['state'] == 'partial' and result['truncated'] is True
    assert 'diff' in result['omitted_inputs']
    assert all(item['status'] == 'uncertain' for item in result['assessments'])


@pytest.mark.parametrize('diff', ['+ real patch', {'state': 'available', 'text': '+ real patch',
                                                'limitations': 'Tracked files only.'}])
def test_complete_diff_formats_remain_supported(diff):
    review, _ = reviewer(lambda state, questions: answers_for(questions, citation='diff'))
    result = review.review('completion', 'Preserve the requested behavior.', reply='Done.', diff=diff)
    assert result['state'] == 'reviewed'
    assert all(item['status'] == 'supported' for item in result['assessments'])
