"""Real lifecycle checkpoints observe work without changing its outcome."""

import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from fake_backend import FakeBackend
from tessera.compactor import Compactor
from tessera.config import Config
from tessera.hooks import HookBus
from tessera.log import ChatLog
from tessera.loop import Session
from tessera.tree import SummaryTree
from tessera.view import View


@pytest.fixture
def reviewers(monkeypatch):
    made = []

    class Reviewer:
        def __init__(self, router, settings):
            self.settings = settings
            self.calls = []
            self.resets = 0
            made.append(self)

        def enabled(self, checkpoint):
            return bool(getattr(self.settings, checkpoint + '_review_enabled', False))

        def begin_turn(self):
            self.resets += 1

        def review(self, checkpoint, request, **evidence):
            self.calls.append((checkpoint, request, evidence))
            return {'checkpoint': checkpoint, 'advisory': True, 'state': 'reviewed',
                    'behavior_preservation': 'failed', 'reason': 'A deliberately adverse advisory opinion',
                    'findings': [{'requirement': 'Keep the requested behavior.', 'evidence_ids': ['diff'],
                                  'remedy': 'Restore the requested behavior.'}]}

    monkeypatch.setitem(sys.modules, 'tessera.jev_review', SimpleNamespace(AdvisoryReviewer=Reviewer))
    return made


def config_for(path, *checkpoints):
    config = Config(home=path, cwd=path)
    config.tools.approval = 'never'
    config.jev.enabled = False
    for checkpoint in checkpoints:
        setattr(config.jev, checkpoint + '_review_enabled', True)
    return config


def test_default_session_never_captures_a_diff_or_adds_review_logs(tmp_path, reviewers, monkeypatch):
    monkeypatch.setattr(Session, '_review_diff', lambda _: pytest.fail('default-off review read Git'))
    with Session(config_for(tmp_path), backend=FakeBackend(['Done.'])) as session:
        assert session.run_turn('Hello') == 'Done.'
        assert session.last_turn_success
        assert not reviewers[0].calls
        assert session.compactor.advisory_review is None
        assert [row.kind for row in session.log.iter()] == ['user', 'agent']


def test_adverse_completion_review_keeps_reply_success_and_tool_facts(tmp_path, reviewers, monkeypatch):
    monkeypatch.setattr(Session, '_review_diff', lambda _: 'A bounded real checkout patch')
    backend = FakeBackend([
        {'tool_calls': [{'id': 'read', 'name': 'read_file', 'arguments': {'path': 'missing.txt'}}]},
        'The requested file is missing.',
    ])
    with Session(config_for(tmp_path, 'completion'), backend=backend) as session:
        reply = session.run_turn('Read missing.txt')
        assert reply == 'The requested file is missing.'
        assert session.last_turn_success  # Existing conversational-success semantics.
        checkpoint, request, evidence = reviewers[0].calls[0]
        assert checkpoint == 'completion' and request == 'Read missing.txt'
        assert evidence['tool_outputs'][0]['success'] is False
        assert evidence['diff'] == 'A bounded real checkout patch'
        final = list(session.log.iter())[-1]
        assert final.kind == 'agent' and final.text == reply
        assert final.meta['advisory_review']['behavior_preservation'] == 'failed'
        note = next(row for row in session.log.iter() if row.kind == 'note')
        assert 'Keep the requested behavior.' in note.text
        assert 'Restore the requested behavior.' in note.text
        assert 'evidence_ids' in note.text and 'diff' in note.text
        assert 'judge_score' not in final.meta
        assert reviewers[0].resets == 1


def test_only_a_real_permitted_plan_submission_is_reviewed(tmp_path, reviewers):
    executed = []
    hooks = HookBus()
    hooks.register('pre_tool', lambda payload: {'block': True} if payload['arguments'].get('blocked') else None)

    def register(registry):
        registry.register('board_set_steps', 'Submit a plan', {'type': 'object'},
                          lambda **arguments: executed.append(arguments) or {'saved': True})

    backend = FakeBackend([
        {'tool_calls': [{'id': 'blocked', 'name': 'board_set_steps',
                         'arguments': {'steps': [{'text': 'Blocked plan'}], 'blocked': True}}]},
        {'tool_calls': [{'id': 'plan', 'name': 'board_set_steps',
                         'arguments': {'steps': [{'text': 'Preserve the existing function'}]}}]},
        'Submitted the plan.',
    ])
    with Session(config_for(tmp_path, 'plan'), backend=backend, hooks=hooks,
                 configure_tools=register) as session:
        assert session.run_turn('Use board_set_steps to record the plan') == 'Submitted the plan.'
        assert len(executed) == 1
        calls = reviewers[0].calls
        assert len(calls) == 1 and calls[0][0] == 'plan'
        assert 'Preserve the existing function' in calls[0][2]['reply']
        assert session.last_turn_success


def test_worker_handoff_review_keeps_the_actual_report_and_success(tmp_path, reviewers):
    with Session(config_for(tmp_path, 'handoff'), backend=FakeBackend([]),
                 agent_backend_factory=lambda: FakeBackend(['Read the file.'])) as session:
        session.agents.spawn('reader', 'Inspect the requested file.', ['read_file'])
        assert session.agents.wait(timeout=5)
        report = next(row for row in session.log.iter() if row.kind == 'work')
        assert report.text == '[reader] Read the file.'
        assert report.meta['success'] is True
        assert report.meta['advisory_review']['behavior_preservation'] == 'failed'
        calls = reviewers[0].calls
        assert len(calls) == 2 and all(call[0] == 'handoff' for call in calls)
        assert calls[-1][2]['tool_outputs'][0]['success'] is True


def test_worker_handoff_keeps_failed_tool_evidence_even_when_worker_finishes(tmp_path, reviewers):
    worker = FakeBackend([
        {'tool_calls': [{'id': 'missing', 'name': 'read_file', 'arguments': {'path': 'missing.txt'}}]},
        'The file is missing; I have finished inspecting.',
    ])
    with Session(config_for(tmp_path, 'handoff'), backend=FakeBackend([]),
                 agent_backend_factory=lambda: worker) as session:
        session.agents.spawn('reader', 'Inspect the requested file.', ['read_file'])
        assert session.agents.wait(timeout=5)
        report = next(row for row in session.log.iter() if row.kind == 'work')
        assert report.meta['success'] is True  # Preserve existing completion semantics.
        outputs = reviewers[0].calls[-1][2]['tool_outputs']
        assert outputs[0]['name'] == 'worker_report' and outputs[0]['success'] is True
        assert any(row['name'] == 'worker_tool_output' and row['success'] is False for row in outputs)


@pytest.mark.parametrize('raises', [False, True])
def test_summary_advice_never_rewrites_the_summary_or_runs_extra_model_work(tmp_path, raises):
    seen = []
    with ChatLog(tmp_path / 'log') as log:
        tree = SummaryTree(tmp_path, log, node_bytes=64)
        view = View(tmp_path, tree)
        log.subscribe(view.on_message)
        source = 'Preserve the exact original request ' * 8
        log.append('user', source)
        backend = FakeBackend(['[user] preserve the request'])

        def review(text, source):
            seen.append((text, source))
            if raises:
                raise ValueError('private endpoint text must not be persisted')
            return {'advisory': True, 'state': 'reviewed', 'fidelity': 'failed'}

        worker = Compactor(tree, view, backend, 'system', [], workers=1, advisory_review=review)
        worker.start()
        try:
            assert worker.wait_until_summarized(0, timeout=3)
        finally:
            worker.stop()
        assert tree.node(0, 0) == '[user] preserve the request'
        assert len(backend.calls) == 1
        assert seen == [('[user] preserve the request', '[user] ' + source)]
        meta = tree.node_meta(0, 0)['advisory_review']
        assert meta['state'] == ('unavailable' if raises else 'reviewed')
        assert 'private endpoint' not in json.dumps(meta)


def test_diff_is_bounded_read_only_and_labels_its_attribution_limits(tmp_path):
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    tracked = tmp_path / 'tracked.txt'
    tracked.write_text('before\n')
    subprocess.run(['git', 'add', '.'], cwd=tmp_path, check=True)
    subprocess.run(['git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.com',
                    'commit', '-qm', 'Fixture'], cwd=tmp_path, check=True)
    tracked.write_text('after\n' * 300)
    (tmp_path / 'untracked.txt').write_text('Excluded content')
    before = subprocess.check_output(['git', 'status', '--porcelain'], cwd=tmp_path)
    stub = SimpleNamespace(config=SimpleNamespace(jev=SimpleNamespace(review_max_input_chars=120)),
                           workspace=SimpleNamespace(path=tmp_path))
    diff = Session._review_diff(stub)
    after = subprocess.check_output(['git', 'status', '--porcelain'], cwd=tmp_path)
    assert before == after
    limitations = ' '.join(diff['limitations']).lower()
    assert 'pre-existing' in limitations and 'untracked' in limitations and 'already committed' in limitations
    assert diff['state'] == 'partial' and 'truncated' in limitations
    assert 'Excluded content' not in diff['text']
    assert len(diff['text']) <= 120


def test_lesson_advice_runs_after_deterministic_learning_and_never_changes_it(tmp_path):
    from tessera.learning import LearningStore
    from tessera.sessions import _Runtime
    learning = LearningStore(tmp_path / 'learning.json')
    lesson = learning.record_lesson('work', 'Verify the database build', 'The prior command failed.',
                                    'Run the check successfully', skill='database-check')
    reviewed = []

    def review(checkpoint, request, **evidence):
        row = learning._lessons[lesson['id']]
        assert row['checks'][-1]['tools_failed'] is True
        reviewed.append((checkpoint, request, evidence))
        return {'state': 'reviewed', 'advisory': True, 'retention': 'approve'}

    session = SimpleNamespace(
        log=SimpleNamespace(redact=lambda value: value),
        capture=SimpleNamespace(inheritance_block=lambda: '', debt=lambda: []),
        advisory_reviews=SimpleNamespace(enabled=lambda checkpoint: checkpoint == 'lesson'),
        review_advisory=review)
    manager = SimpleNamespace(autonomy=SimpleNamespace(learning=learning),
                              _save_runtime=lambda runtime: None, advance_trials=lambda slug: None)
    runtime = SimpleNamespace(session=session, manager=manager, slug='work', active={'id': 'turn-1'},
                              events=SimpleNamespace(append=lambda *args: None))
    _Runtime._on_turn_end(runtime, {'message': 'Verify the database build', 'reply': 'Build failed.',
                                   'success': True, 'judge_score': None, 'tools_failed': True,
                                   'pinned_skills': ['database-check'],
                                   'tools': [{'name': 'shell', 'result': {'exit_code': 1}}]})
    row = learning._lessons[lesson['id']]
    assert row['status'] != 'absorbed'
    assert row['checks'][-1]['passed'] is False
    assert len(reviewed) == 1 and reviewed[0][0] == 'lesson'
    assert reviewed[0][2]['tool_outputs'][0]['result']['exit_code'] == 1
    # The supplied lesson snapshot predates the update; it is not rewritten by it.
    snapshot = json.loads(reviewed[0][2]['evidence'][0]['text'])[0]
    assert not snapshot.get('checks')


def test_completion_review_waits_for_ordered_user_corrections(tmp_path, reviewers, monkeypatch):
    monkeypatch.setattr(Session, '_review_diff', lambda _: {'state': 'empty', 'text': '', 'limitations': []})
    session = None

    def first_draft(messages, tools):
        assert session.steer('First correction: preserve the Stop button.')
        return {'content': 'An outdated first draft.'}

    def second_draft(messages, tools):
        assert session.steer('Second correction: Stop must leave the server stopped.')
        return {'content': 'An outdated second draft.'}

    with Session(config_for(tmp_path, 'completion'),
                 backend=FakeBackend([first_draft, second_draft, 'The corrected answer.'])) as session:
        assert session.run_turn('Repair Start and Stop.') == 'The corrected answer.'
        calls = reviewers[0].calls
        assert len(calls) == 1, 'Discarded drafts must not consume the review budget.'
        checkpoint, request, evidence = calls[0]
        assert checkpoint == 'completion' and evidence['reply'] == 'The corrected answer.'
        assert request.startswith('Repair Start and Stop.')
        assert request.index('First correction') < request.index('Second correction')
        assert session._action_goal == request


@pytest.mark.parametrize('checkpoint', ['plan', 'handoff'])
def test_plan_and_handoff_review_use_the_latest_effective_request(tmp_path, reviewers, checkpoint):
    with Session(config_for(tmp_path, checkpoint), backend=FakeBackend([])) as session:
        session._action_goal = 'Original request\n\nFirst correction\n\nSecond correction'
        session.review_advisory(checkpoint, 'A stale earlier assignment', reply='A proposal')
        reviewed = reviewers[0].calls[-1][1]
        assert reviewed.startswith(session._action_goal)
        assert reviewed.index('First correction') < reviewed.index('Second correction')
        if checkpoint == 'handoff':
            assert 'A stale earlier assignment' in reviewed  # Keep assignment evidence too.


def test_background_handoff_review_does_not_adopt_an_unrelated_later_goal(tmp_path, reviewers):
    with Session(config_for(tmp_path, 'handoff'), backend=FakeBackend([])) as session:
        session._action_goal = 'An unrelated later user turn.'
        session.review_advisory('handoff', 'The worker original assignment.', record_note=False,
                                 reply='Worker report')
        assert reviewers[0].calls[-1][1] == 'The worker original assignment.'


@pytest.mark.parametrize('checkout', [False, True])
def test_real_review_cannot_use_missing_or_empty_diff_as_outcome_proof(tmp_path, checkout):
    from tessera.jev import JudgeKit
    from tessera.jev_review import AdvisoryReviewer

    if checkout:
        subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
        (tmp_path / 'tracked.txt').write_text('Unchanged\n')
        subprocess.run(['git', 'add', '.'], cwd=tmp_path, check=True)
        subprocess.run(['git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.com',
                        'commit', '-qm', 'Fixture'], cwd=tmp_path, check=True)
    stub = SimpleNamespace(config=SimpleNamespace(jev=SimpleNamespace(review_max_input_chars=24000)),
                           workspace=SimpleNamespace(path=tmp_path))
    diff = Session._review_diff(stub)
    assert diff['state'] == ('empty' if checkout else 'unavailable')
    assert diff['text'] == ''

    def judge(state, questions):
        # An overconfident judge cites the absence record as purported proof.
        answers = {}
        for name, question in questions.items():
            selected = 'concern' if name.startswith('check_') else 'diff'
            answers[name] = {'type': 'choice', 'choice': selected, 'confidence': 1,
                             'probabilities': {key: float(key == selected)
                                               for key in question['criteria']}}
        return {'answers': answers}

    settings = SimpleNamespace(completion_review_enabled=True, review_max_calls_per_turn=1,
                               review_max_input_chars=24000)
    review = AdvisoryReviewer(JudgeKit(enabled=True, evaluator=judge), settings)
    report = review.review('completion', 'Preserve the existing Stop behavior.', diff=diff)
    assert report['assessments']
    assert all(item['status'] == 'uncertain' for item in report['assessments'])
    assert report['findings'] == []
