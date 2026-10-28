"""A slow judge must not be reported as a dead one.

A judge that answers slower than jev.timeout is not a dead judge. The transport
exception is swallowed, and strict mode fills every question with noul=0.5 and
source='unavailable'. Any non-'jev' source is then read as unavailable, and the
gate blocks every mutating tool for that call with "Jev action judgment
unavailable. Inspect or repair the judge, then retry". That sentence is false
when the judge was merely slow.

The gate retries transport once, under a total batch deadline. A judge that
never answers still yields 'unavailable', and the gate still refuses the
mutation. A judge which answers on the second attempt is read as present.
"""
import io
import json
import time

from tessera.jev import JudgeKit

QUESTIONS = {
    'disrupts_reasoning': {
        'type': 'noul',
        'instructions': {'question': 'Would this action interrupt this session?'},
        'criteria': {'true': 'It stops serving.', 'false': 'It preserves serving.'},
    },
}


def good_envelope(model='jev-1.14.0'):
    return io.BytesIO(json.dumps({
        'model': model,
        'answers': {'disrupts_reasoning': {'type': 'noul', 'noul': 0.01}},
    }).encode())


def test_a_judge_that_times_out_once_then_answers_is_read_as_present(monkeypatch):
    """The discriminator. One slow response, then a good one, must not be 'unavailable'."""
    monkeypatch.setenv('JEV_SLOW_TEST', 'test-only')
    kit = JudgeKit(api_key_env='JEV_SLOW_TEST', url='https://judge.example.com/evaluate',
                   model='jev-1.14.0', enabled=True, timeout=8)
    calls = []

    def respond(request, timeout):
        calls.append(timeout)
        if len(calls) == 1:
            raise TimeoutError('judge was slower than the per-request timeout')
        return good_envelope()

    import tessera.jev as jev
    monkeypatch.setattr(jev.urllib.request, 'urlopen', respond)

    answers = kit._batch('action_gate', {}, QUESTIONS, strict=True)
    assert len(calls) == 2, 'the slow answer must be retried exactly once'
    assert answers['disrupts_reasoning']['source'] == 'jev'
    assert answers['disrupts_reasoning']['noul'] == 0.01


def test_a_judge_that_never_answers_stays_unavailable_and_is_not_retried_forever(monkeypatch):
    """The safety property. Absent judge still yields no fabricated permission."""
    monkeypatch.setenv('JEV_SLOW_TEST', 'test-only')
    kit = JudgeKit(api_key_env='JEV_SLOW_TEST', url='https://judge.example.com/evaluate',
                   model='jev-1.14.0', enabled=True, timeout=8)
    calls = []

    def respond(request, timeout):
        calls.append(timeout)
        raise TimeoutError('judge is not answering')

    import tessera.jev as jev
    monkeypatch.setattr(jev.urllib.request, 'urlopen', respond)

    answers = kit._batch('action_gate', {}, QUESTIONS, strict=True)
    assert len(calls) == 2, 'a bounded retry means two attempts, not an unbounded loop'
    assert answers['disrupts_reasoning']['source'] == 'unavailable'
    assert answers['disrupts_reasoning']['noul'] == 0.5, 'strict mode must not fabricate permission'


def test_the_never_fabricate_permission_property_is_unchanged_by_the_retry(monkeypatch):
    """This assertion holds on the old code and the new one. It is here so the
    retry cannot quietly buy availability by inventing a permission."""
    monkeypatch.setenv('JEV_SLOW_TEST', 'test-only')
    kit = JudgeKit(api_key_env='JEV_SLOW_TEST', url='https://judge.example.com/evaluate',
                   model='jev-1.14.0', enabled=True, timeout=8)

    def respond(request, timeout):
        raise ConnectionError('no route to the judge')

    import tessera.jev as jev
    monkeypatch.setattr(jev.urllib.request, 'urlopen', respond)

    answers = kit._batch('action_gate', {}, QUESTIONS, strict=True)
    assert answers['disrupts_reasoning']['source'] == 'unavailable'
    assert answers['disrupts_reasoning']['noul'] == 0.5
    assert answers['disrupts_reasoning']['noul'] < 0.7, 'must sit below DENY_PROBABILITY, not at a confident pass'


def test_the_retry_is_capped_by_a_total_batch_deadline(monkeypatch):
    """A dead judge must not hold a turn for two full timeouts."""
    monkeypatch.setenv('JEV_SLOW_TEST', 'test-only')
    kit = JudgeKit(api_key_env='JEV_SLOW_TEST', url='https://judge.example.com/evaluate',
                   model='jev-1.14.0', enabled=True, timeout=0.4)
    observed = []

    def respond(request, timeout):
        observed.append(timeout)
        time.sleep(timeout)
        raise TimeoutError('judge is not answering')

    import tessera.jev as jev
    monkeypatch.setattr(jev.urllib.request, 'urlopen', respond)

    started = time.monotonic()
    answers = kit._batch('action_gate', {}, QUESTIONS, strict=True)
    elapsed = time.monotonic() - started

    assert answers['disrupts_reasoning']['source'] == 'unavailable'
    assert abs(kit.batch_deadline - 0.6) < 1e-9, 'default deadline is one and a half request timeouts'
    # The claim is that the deadline caps the batch BELOW two full timeouts (0.8s).
    # It is not a claim about the clock being exact: the decision-log fsync costs
    # real milliseconds, so the bound carries headroom rather than a tight equality.
    assert elapsed < 2 * 0.4, 'the total must stay under two full timeouts'
    assert len(observed) == 2
    assert sum(observed) <= kit.batch_deadline + 0.05, 'the second attempt must be shortened to fit'


def test_a_deadline_shorter_than_one_request_is_rejected():
    try:
        JudgeKit(timeout=8, batch_deadline=4)
    except ValueError as error:
        assert 'deadline' in str(error)
    else:
        raise AssertionError('a deadline below the request timeout must be refused')


def test_a_rejected_answer_is_not_retried(monkeypatch):
    """Only transport failures retry. A judge that answers badly is answering."""
    monkeypatch.setenv('JEV_SLOW_TEST', 'test-only')
    kit = JudgeKit(api_key_env='JEV_SLOW_TEST', url='https://judge.example.com/evaluate',
                   model='jev-1.14.0', enabled=True, timeout=8)
    calls = []

    def respond(request, timeout):
        calls.append(timeout)
        # A well-formed envelope carrying a value the validator must reject.
        return io.BytesIO(json.dumps({
            'model': 'jev-1.14.0',
            'answers': {'disrupts_reasoning': {'type': 'noul', 'noul': 'not-a-number'}},
        }).encode())

    import tessera.jev as jev
    monkeypatch.setattr(jev.urllib.request, 'urlopen', respond)

    answers = kit._batch('action_gate', {}, QUESTIONS, strict=True)
    assert len(calls) == 1, 'an invalid answer is the judge disagreeing, not the wire failing'
    assert answers['disrupts_reasoning']['source'] == 'unavailable'


# --- The boundary of the retry, pinned so it cannot be overclaimed -------------
#
# test_a_judge_that_times_out_once_then_answers_is_read_as_present raises
# TimeoutError INSTANTLY, so the first attempt spends no budget and the second
# attempt gets the full per-request timeout. That covers the fast-failure class.
#
# It does NOT cover the class the live records actually show. A real timeout
# consumes the whole per-request budget, so the second attempt is left with
# (batch_deadline - timeout) = 0.5 * timeout, which is always LESS than the first
# attempt. A judge that needs more than `timeout` therefore cannot be rescued by
# the retry, at any timeout. A judge needing 8.2s against timeout=8.0 yields
# 'unavailable'; the same judge answers once timeout is raised above what it needs.
#
# The two tests below pin that boundary. They are characterization tests: they
# assert what the code DOES, including its limitation, so that a later change
# either keeps the limit honestly or has to update the record deliberately.


def _budget_consuming_responder(needs_seconds, calls, envelope):
    """A judge that behaves like a real slow server: if it cannot finish inside
    the attempt's budget it consumes the whole budget and then fails."""
    raw = envelope.getvalue() if hasattr(envelope, 'getvalue') else envelope
    def respond(request, timeout=None):
        calls.append(round(timeout, 3))
        if needs_seconds > timeout:
            time.sleep(timeout)
            raise TimeoutError('judge slower than this attempt')
        time.sleep(needs_seconds)
        return io.BytesIO(raw)
    return respond


def test_a_judge_slower_than_the_timeout_is_not_rescued_by_the_retry(monkeypatch):
    """The honest limit: the second attempt is SHORTER than the first, so a judge
    that needs more than `timeout` stays unavailable. Raising the timeout is the
    only lever that works for this class."""
    import tessera.jev as jev

    monkeypatch.setenv('JEV_SLOW_TEST', 'test-only')
    kit = JudgeKit(api_key_env='JEV_SLOW_TEST', url='https://judge.example.com/evaluate',
                   model='jev-1.14.0', enabled=True, timeout=0.4)
    calls = []
    # Unique state so the cache cannot answer for us: the cache key omits timeout.
    monkeypatch.setattr(jev.urllib.request, 'urlopen',
                        _budget_consuming_responder(0.5, calls, good_envelope()))

    answers = kit._batch('action_gate', {'probe': 'needs-more-than-one-timeout'},
                         QUESTIONS, strict=True)

    assert calls, 'the probe never reached the transport, so it proves nothing'
    assert len(calls) == 2, 'both attempts were made'
    assert calls[1] < calls[0], (
        'the second attempt must be shortened by the budget the first one spent; '
        f'observed budgets {calls}')
    assert answers['disrupts_reasoning']['source'] == 'unavailable', (
        'a judge needing more than the per-request timeout must not be reported '
        'as present: the retry cannot reach it')
    assert answers['disrupts_reasoning']['noul'] == 0.5, (
        'strict mode must still refuse to fabricate permission')


def test_raising_the_timeout_is_what_rescues_a_judge_that_needs_longer(monkeypatch):
    """The counterpart: same slow judge, larger per-request timeout, answers on the
    first attempt. This is the lever the boundary above implies."""
    import tessera.jev as jev

    monkeypatch.setenv('JEV_SLOW_TEST', 'test-only')
    kit = JudgeKit(api_key_env='JEV_SLOW_TEST', url='https://judge.example.com/evaluate',
                   model='jev-1.14.0', enabled=True, timeout=0.8)
    calls = []
    monkeypatch.setattr(jev.urllib.request, 'urlopen',
                        _budget_consuming_responder(0.5, calls, good_envelope()))

    answers = kit._batch('action_gate', {'probe': 'timeout-raised-past-need'},
                         QUESTIONS, strict=True)

    assert calls, 'the probe never reached the transport, so it proves nothing'
    assert len(calls) == 1, 'a judge inside the timeout needs no retry'
    assert answers['disrupts_reasoning']['source'] == 'jev'
    assert answers['disrupts_reasoning']['noul'] == 0.01
