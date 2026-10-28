import threading
from types import SimpleNamespace

import pytest

from fake_backend import FakeBackend
from test_loop import session_config
from tessera.action_judge import DependencyResolver, QUESTIONS
from tessera.jev import JudgeKit
from tessera.loop import Session
from tessera.tools import build_registry


def evaluator(values):
    def evaluate(state, questions):
        answers = {}
        for name in questions:
            lookup = 'disrupts_reasoning' if name.startswith('interrupts_resource_') else name
            answers[name] = {'type': 'noul', 'noul': values.get(lookup, 0.99 if name == 'advances_goal' else 0.01)}
        return {'model': 'test-judge', 'answers': answers}
    return evaluate


@pytest.mark.parametrize('values,blocked', [
    # A confident interrupt blocks on its own, whatever changes_hosting says:
    # stopping a resource is a hosting change, so the judge being sure about
    # the resource is enough.
    ({'disrupts_reasoning': 0.99}, True),
    # The uncertain band needs corroboration. 0.5 on the interrupt ceiling with
    # changes_hosting left at the 0.01 default is not a refusal: a max() over
    # nine resources is not evidence the action touches hosting.
    ({'disrupts_reasoning': 0.5}, False),
    # Both questions elevated: the judge believes this action touches hosting
    # AND could interrupt a required resource. That is the corroborated refusal.
    ({'disrupts_reasoning': 0.5, 'changes_hosting': 0.5}, True),
    ({'withdrawn_authorization': 0.99}, True),
    ({'unproductive_repetition': 0.99, 'advances_goal': 0.01}, True),
    ({'unproductive_repetition': 0.99, 'advances_goal': 0.99}, False),
    ({}, False),
])
def test_action_judgment_enforced_before_side_effect_under_yolo(tmp_path, values, blocked):
    config = session_config(tmp_path)
    config.jev.action_gate_enabled = True
    config.tools.approval = 'yolo'
    judge = JudgeKit(enabled=True, action_gate_enabled=True, evaluator=evaluator(values))
    backend = FakeBackend([])
    with Session(config, backend=backend, router=judge) as session:
        session._action_goal = 'Write the requested file.'
        session._call_tool({'id': 'action', 'name': 'write_file',
                            'arguments': {'path': 'receipt.txt', 'text': 'done'}})
        assert (tmp_path / 'receipt.txt').exists() is not blocked
        echo = list(session.log.iter())[-1]
        assert echo.meta['blocked'] is blocked
        assert echo.meta['action_judgment']['source'] == 'jev'
        assert all(row['point'] == 'action_gate' for row in judge.decisions)


def test_unavailable_judge_never_fabricates_permission_and_read_repair_lane_remains(tmp_path):
    config = session_config(tmp_path)
    config.jev.action_gate_enabled = True
    judge = JudgeKit(enabled=False, action_gate_enabled=True)
    (tmp_path / 'config.txt').write_text('repair evidence')
    with Session(config, backend=FakeBackend([]), router=judge) as session:
        session._call_tool({'id': 'write', 'name': 'write_file', 'arguments': {'path': 'bad', 'text': 'bad'}})
        assert not (tmp_path / 'bad').exists()
        result, _, _ = session._call_tool({'id': 'read', 'name': 'read_file', 'arguments': {'path': 'config.txt'}})
        assert 'repair evidence' in str(result)
        assert {row['source'] for row in judge.decisions} == {'unavailable'}


def test_action_state_includes_pending_operator_correction_without_prompt_bloat(tmp_path):
    captured = []
    def evaluate(state, questions):
        captured.append(state)
        return evaluator({})(state, questions)
    config = session_config(tmp_path)
    config.jev.action_gate_enabled = True
    with Session(config, backend=FakeBackend([]),
                 router=JudgeKit(enabled=True, action_gate_enabled=True, evaluator=evaluate)) as session:
        original = session.system_prompt
        session._action_goal = 'Original request'
        session._in_turn.set()
        session.steer('Stop refining metrics. Fix the important cards.')
        session._call_tool({'id': 'read', 'name': 'list_dir', 'arguments': {}})
        session._in_turn.clear()
        assert captured[0]['pending_operator_guidance'] == ['Stop refining metrics. Fix the important cards.']
        assert session.system_prompt == original
        assert 'disrupts_reasoning' not in original


def test_router_dependency_refresh_tracks_model_and_rank_swaps(tmp_path, monkeypatch):
    config = session_config(tmp_path)
    config.jev.router_probes = {'http://router/v1': ['read-live-router']}
    resolver = DependencyResolver(config.jev)
    reports = iter([
        {'routes': {'auto': {'revision': 1, 'candidates': [{'model': 'first', 'base_url': 'http://head/v1',
            'rank_resolution': 'inventory_and_live_model', 'ranks': [{'host': 'head'}, {'host': 'worker'}]}]}}},
        {'routes': {'auto': {'revision': 2, 'candidates': [{'model': 'replacement', 'base_url': 'http://new/v1'}]}}},
        {'error': 'unavailable'},
    ])
    monkeypatch.setattr(resolver, '_probe', lambda command, registry: next(reports))
    backend = SimpleNamespace(name='auto-backend', base_url='http://router/v1', model='auto')
    with Session(config, backend=FakeBackend([])) as session:
        first = resolver.resolve(backend, session.tools)
        assert first['engines'][0]['model'] == 'first'
        assert len(first['engines'][0]['ranks']) == 2
        resolver._cache.clear()
        second = resolver.resolve(backend, session.tools)
        assert second['engines'][0]['model'] == 'replacement'
        assert second['engines'][0]['rank_resolution'] == 'unknown'
        assert backend.model == 'auto'
        resolver._cache.clear()
        assert resolver.resolve(backend, session.tools)['resolution'] == 'unknown'


def test_invalid_remote_answer_is_unavailable_in_strict_mode():
    judge = JudgeKit(enabled=True, evaluator=lambda *args: {'answers': {}})
    answers = judge._batch('action_gate', {}, QUESTIONS, strict=True)
    assert all(answer['source'] == 'unavailable' for answer in answers.values())


def test_unresolved_router_blocks_hosting_but_not_an_ordinary_edit(tmp_path, monkeypatch):
    config = session_config(tmp_path)
    config.jev.action_gate_enabled = True
    judge = JudgeKit(enabled=True, action_gate_enabled=True, evaluator=evaluator({'changes_hosting': 0.99}))
    with Session(config, backend=FakeBackend([]), router=judge) as session:
        monkeypatch.setattr(session.dependency_resolver, 'resolve',
                            lambda *args: {'resolution': 'unknown', 'endpoint': 'http://router/v1'})
        _, record, _ = session._call_tool({'id': 'host', 'name': 'shell', 'arguments': {'command': 'restart-hosting'}})
        assert record['blocked']
        judge.evaluator = evaluator({})
        _, record, _ = session._call_tool({'id': 'edit', 'name': 'write_file',
                                         'arguments': {'path': 'ordinary.txt', 'text': 'ok'}})
        assert not record['blocked']


def test_guidance_arriving_during_judgment_fences_the_action(tmp_path):
    config = session_config(tmp_path)
    config.jev.action_gate_enabled = True
    with Session(config, backend=FakeBackend([])) as session:
        def evaluate(state, questions):
            session._steers.put({'text': 'Stop that work.', 'context': None, 'metadata': {}})
            return evaluator({})(state, questions)
        session.router.enabled = True
        session.router.evaluator = evaluate
        _, record, _ = session._call_tool({'id': 'edit', 'name': 'write_file',
                                         'arguments': {'path': 'forbidden', 'text': 'bad'}})
        assert record['blocked']
        assert not (tmp_path / 'forbidden').exists()


def test_stop_breaks_a_stalled_native_judge_request(tmp_path, monkeypatch):
    from test_stop_now import stalled_provider

    with stalled_provider(False) as (url, entered):
        registry = build_registry(tmp_path)
        judge = JudgeKit(enabled=True, model='judge', url=url, evaluator=None, api_key_env='JEV_CANCEL_TEST')
        monkeypatch.setenv('JEV_CANCEL_TEST', 'test-only')
        errors = []
        def run():
            try:
                judge._batch('action_gate', {}, QUESTIONS, strict=True, registry=registry)
            except RuntimeError:
                errors.append('cancelled')
        worker = threading.Thread(target=run)
        worker.start()
        assert entered.wait(2)
        registry.cancel()
        worker.join(1)
        assert not worker.is_alive()
        assert errors == ['cancelled']


def test_the_soft_disrupts_band_does_not_claim_ownership_is_uncertain(tmp_path, monkeypatch):
    """A refusal on the soft disrupts band must not instruct the session to
    resolve serving ownership.

    A heartbeat whose judge score sits in the soft band, and whose proposed tool
    changes no hosting, must not be told that serving ownership is uncertain.
    Both halves of that branch would otherwise emit the same sentence, and the
    session would spend a turn resolving an ownership dispute that nobody proposed.

    The two cases below differ in exactly one property: whether the judge is
    confident the action changes hosting. Case A is a real ownership concern
    and must keep the ownership sentence. Case B is the soft band and must not
    claim ownership is uncertain.
    """
    config = session_config(tmp_path)
    config.jev.action_gate_enabled = True

    def refusal(values, name, arguments):
        judge = JudgeKit(enabled=True, action_gate_enabled=True, evaluator=evaluator(values))
        with Session(config, backend=FakeBackend([]), router=judge) as session:
            monkeypatch.setattr(session.dependency_resolver, 'resolve',
                                lambda *args: {'resolution': 'unknown', 'endpoint': 'http://router/v1'})
            _, record, _ = session._call_tool({'id': name, 'name': name, 'arguments': arguments})
        return record

    # A. Confident hosting change against an unresolved route: ownership is the point.
    # The refusal sentence reaches the caller as the record's output, prefixed
    # 'Blocked: ' (loop.py:1090), not as a 'reason' key.
    hosting = refusal({'changes_hosting': 0.99, 'disrupts_reasoning': 0.1},
                      'shell', {'command': 'restart-hosting'})
    assert hosting['blocked']
    assert 'ownership is uncertain' in hosting['output']

    # B. Soft band only. changes_hosting is low, so no hosting change is proposed.
    # The refusal this docstring was written to prevent was the *sentence*, not
    # the block. The block itself was the fault too:
    # 0.05 is nowhere near 0.25, and an action the judge says does not touch
    # hosting must not be refused because a max() over nine resources drifted to
    # 0.27. So this case now passes, and the sentence test it carried moves to
    # the corroborated case below where a refusal is genuinely warranted.
    soft = refusal({'changes_hosting': 0.05, 'disrupts_reasoning': 0.27},
                   'write_file', {'path': 'ordinary.txt', 'text': 'ok'})
    assert not soft['blocked'], soft['output']

    # C. Corroborated soft band: both questions elevated, so the refusal stands
    # and it still must not claim ownership is uncertain.
    corroborated = refusal({'changes_hosting': 0.45, 'disrupts_reasoning': 0.27},
                           'write_file', {'path': 'ordinary.txt', 'text': 'ok'})
    assert corroborated['blocked']
    assert 'ownership is uncertain' not in corroborated['output']
    assert 'may interrupt' in corroborated['output']
