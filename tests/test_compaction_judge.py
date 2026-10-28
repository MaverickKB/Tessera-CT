"""Compaction faithfulness through real worker queues and immutable nodes."""

from contextlib import contextmanager

import pytest

from fake_backend import FakeBackend
from tessera.compactor import Compactor
from tessera.config import Config, load_config
from tessera.jev import JevRouter
from tessera.log import ChatLog
from tessera.loop import Session
from tessera.tree import SummaryTree
from tessera.view import View


class FakeJudge:
    def __init__(self, scores):
        self.scores = iter(scores)
        self.calls = []

    def __call__(self, text, source):
        self.calls.append((text, source))
        score = next(self.scores)
        if isinstance(score, Exception):
            raise score
        return score


@pytest.fixture
def memory(tmp_path):
    with ChatLog(tmp_path / 'log') as log:
        tree = SummaryTree(tmp_path, log, node_bytes=32)
        view = View(tmp_path, tree)
        log.subscribe(view.on_message)
        yield log, tree, view


@contextmanager
def compact(memory, backend, judge=None, **kwargs):
    log, tree, view = memory
    worker = Compactor(tree, view, backend, 'unchanged system', [], workers=1,
                       judge=judge, **kwargs)
    worker.start()
    try:
        assert worker.wait_until_summarized(log.count() - 1, timeout=3), worker.errors
        yield worker
    finally:
        worker.stop()


def test_passing_leaf_records_the_score_of_the_stored_text(memory):
    log, tree, view = memory
    message = log.append('user', 'keep the exact user decision ' * 8)
    backend = FakeBackend(['[user] keep the decision'])
    judge = FakeJudge([.9])
    with compact(memory, backend, judge):
        assert tree.node(0, 0) == '[user] keep the decision'
        assert judge.calls == [('[user] keep the decision', '[user] ' + message.text)]
        assert tree.node_meta(0, 0) == {
            'faithfulness_score': .9, 'faithfulness_scores': [.9],
            'faithfulness_threshold': .8, 'faithfulness_retried': False,
            'faithfulness_passed': True,
        }
        assert len(backend.calls) == 1


def test_failed_leaf_is_repaired_once_with_failure_noted(memory):
    log, tree, view = memory
    message = log.append('user', 'original exact request ' * 10)
    backend = FakeBackend(['[user] invented claim', '[user] original exact request'])
    judge = FakeJudge([.1, .95])
    with compact(memory, backend, judge):
        assert tree.node(0, 0) == '[user] original exact request'
        assert len(backend.calls) == 2
        assert len(judge.calls) == 2
        assert all(source == '[user] ' + message.text for text, source in judge.calls)
        repair = backend.calls[1]
        assert repair['messages'][0] == backend.calls[0]['messages'][0]
        assert repair['tools'] == backend.calls[0]['tools']
        assert repair['tool_choice'] == 'none'
        assert 'faithfulness' in repair['messages'][-1]['content'].lower()
        assert '0.1' in repair['messages'][-1]['content']
        assert tree.node_meta(0, 0)['faithfulness_scores'] == [.1, .95]
        assert tree.node_meta(0, 0)['faithfulness_retried'] is True
        assert tree.node_meta(0, 0)['faithfulness_passed'] is True


def test_worse_repair_keeps_the_better_scored_candidate_without_looping(memory):
    log, tree, view = memory
    log.append('user', 'original request ' * 10)
    backend = FakeBackend(['[user] partial request', '[user] invented claim'])
    with compact(memory, backend, FakeJudge([.4, .1])):
        assert tree.node(0, 0) == '[user] partial request'
        meta = tree.node_meta(0, 0)
        assert meta['faithfulness_score'] == .4
        assert meta['faithfulness_scores'] == [.4, .1]
        assert meta['faithfulness_passed'] is False
        assert len(backend.calls) == 2


def test_merge_is_judged_against_children_and_exact_leaves_are_not_judged(memory):
    log, tree, view = memory
    log.append('user', 'keep exact decision')
    log.append('tool', 'verification failed')
    judge = FakeJudge([.85])
    with compact(memory, FakeBackend(['[user] decision; [tool] failed']), judge):
        assert judge.calls == [('[user] decision; [tool] failed',
                                '[user] keep exact decision\n[tool] verification failed')]
        assert tree.node_meta(0, 0) == {}
        assert tree.node_meta(0, 1) == {}
        assert tree.node_meta(1, 0)['faithfulness_score'] == .85


@pytest.mark.parametrize('repair', [False, True])
def test_judge_receives_utf8_bounded_text_including_the_repair(memory, repair):
    log, tree, view = memory
    log.append('user', 'retain the original facts ' * 10)
    answers = ['🙂' * 12]
    scores = [.9]
    if repair:
        answers.insert(0, '[user] bad')
        scores.insert(0, .1)
    backend = FakeBackend(answers)
    judge = FakeJudge(scores)
    with compact(memory, backend, judge, retries=0):
        assert tree.node(0, 0) == '🙂' * 8
        assert judge.calls[-1][0] == tree.node(0, 0)
        assert len(backend.calls) == (2 if repair else 1)


@pytest.mark.parametrize('score', [RuntimeError('private detail'), float('nan'),
                                 float('inf'), -.1, 1.1, '0.9', True])
def test_unavailable_or_invalid_judge_never_blocks_compaction(memory, score):
    log, tree, view = memory
    log.append('user', 'keep original request ' * 10)
    backend = FakeBackend(['[user] original request'])
    with compact(memory, backend, FakeJudge([score])) as worker:
        assert tree.node(0, 0) == '[user] original request'
        meta = tree.node_meta(0, 0)
        assert meta['faithfulness_score'] is None
        assert meta['faithfulness_passed'] is None
        assert meta['faithfulness_error'] in ('RuntimeError', 'ValueError')
        assert 'private detail' not in repr(meta)
        assert len(backend.calls) == 1
        assert worker.errors == []


@pytest.mark.parametrize('backend', [None, FakeBackend([RuntimeError('offline')])])
def test_extractive_fallback_does_not_call_judge(memory, backend):
    log, tree, view = memory
    log.append('user', 'keep original request ' * 10)
    judge = FakeJudge([])
    with compact(memory, backend, judge):
        assert tree.node(0, 0).startswith('[user] keep original request')
        assert tree.node_meta(0, 0) == {}
        assert judge.calls == []


def test_judging_is_off_without_callback(memory):
    log, tree, view = memory
    log.append('user', 'keep original request ' * 10)
    backend = FakeBackend(['[user] original request'])
    with compact(memory, backend):
        assert tree.node_meta(0, 0) == {}
        assert len(backend.calls) == 1


def test_failed_repair_request_preserves_the_existing_score(memory):
    log, tree, view = memory
    log.append('user', 'keep original request ' * 10)
    backend = FakeBackend(['[user] partial request', RuntimeError('private detail')])
    judge = FakeJudge([.3])
    with compact(memory, backend, judge):
        assert tree.node(0, 0) == '[user] partial request'
        meta = tree.node_meta(0, 0)
        assert meta['faithfulness_score'] == .3
        assert meta['faithfulness_retry_failed'] is True
        assert meta['faithfulness_retried'] is True
        assert len(backend.calls) == 2
        assert len(judge.calls) == 1


def test_threshold_is_configurable_and_inclusive(memory):
    log, tree, view = memory
    log.append('user', 'keep original request ' * 10)
    backend = FakeBackend(['[user] original request'])
    with compact(memory, backend, FakeJudge([.5]), judge_threshold=.5):
        assert tree.node_meta(0, 0)['faithfulness_passed'] is True
        assert len(backend.calls) == 1


def test_session_wires_faithfulness_without_reversing_summary_and_source(tmp_path, monkeypatch):
    config = Config(home=tmp_path, cwd=tmp_path)
    config.compactor.judge_enabled = True
    config.view.node_bytes = 64
    router = JevRouter(enabled=False)
    checked = []

    def faithfulness(source, summary, **context):
        checked.append((source, summary))
        return .93

    monkeypatch.setattr(router, 'faithfulness', faithfulness)
    summary = '[user] preserve the exact constraints'
    backend = FakeBackend([summary])
    with Session(config, backend=FakeBackend([]), router=router,
                 compactor_backend=backend) as session:
        message = session.log.append('user', 'preserve the exact constraints ' * 10)
        assert session.compactor.wait_until_summarized(0, timeout=3)
        assert checked == [('[user] ' + message.text, summary)]
        assert session.tree.node(0, 0) == summary
        assert session.tree.node_meta(0, 0)['faithfulness_score'] == .93
        assert len(backend.calls) == 1


def test_session_takes_compactor_options_from_settings_and_env(tmp_path, monkeypatch):
    (tmp_path / 'config.toml').write_text(
        '[compactor]\nworkers=2\nretries=0\ncontext_bytes=96\n'
        'judge_threshold=0.6\njudge_enabled=false\n', encoding='utf-8')
    monkeypatch.setenv('TESSERA_COMPACTOR_CONTEXT_BYTES', '72')
    config = load_config(tmp_path)
    router = JevRouter(enabled=False)
    with Session(config, backend=FakeBackend([]), router=router) as session:
        worker = session.compactor
        assert worker.workers == 2
        assert worker.retries == 0
        assert worker.context_bytes == 72
        assert worker.judge_threshold == .6
        assert worker.judge is None
