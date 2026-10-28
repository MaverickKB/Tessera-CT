import threading

from tessera.compactor import Compactor
from tessera.log import ChatLog
from tessera.tree import SummaryTree
from tessera.view import View


class ScriptedBackend:
    def __init__(self, answers):
        self.answers = iter(answers)
        self.calls = []

    def chat(self, messages, tools=None, **kwargs):
        self.calls.append((messages, tools, kwargs))
        answer = next(self.answers)
        if isinstance(answer, Exception):
            raise answer
        return {'content': answer, 'tool_calls': [], 'usage': {}}


def test_retry_keeps_shortest_and_cuts_on_utf8_boundary(tmp_path):
    with ChatLog(tmp_path / 'log') as log:
        tree = SummaryTree(tmp_path, log, node_bytes=24)
        view = View(tmp_path, tree)
        log.subscribe(view.on_message)
        log.append('tool', 'very long tool output ' * 30)
        backend = ScriptedBackend(['🙂' * 8, 'é' * 20, 'x' * 50])
        schema = [{'type': 'function', 'function': {'name': 'zoom'}}]
        compactor = Compactor(tree, view, backend, 'fixed system', schema,
                              workers=1, retries=2, context_bytes=80)
        compactor.start()
        try:
            assert compactor.wait_until_summarized(0, timeout=3)
            assert tree.node(0, 0) == '🙂' * 6
            assert len(backend.calls) == 3
            for messages, tools, kwargs in backend.calls:
                assert messages[0] == {'role': 'system', 'content': 'fixed system'}
                assert tools == schema
                assert kwargs['tool_choice'] == 'none'
                assert len(messages[1]['content'].encode()) <= 80
                assert '1234567890' in messages[2]['content']
            assert 'cut point' in backend.calls[1][0][-1]['content']
        finally:
            compactor.stop()


def test_offline_workers_build_full_tree_preserve_user_words_and_restart(tmp_path):
    with ChatLog(tmp_path / 'log') as log:
        tree = SummaryTree(tmp_path, log, node_bytes=64)
        view = View(tmp_path, tree, high_bytes=200, low_bytes=100)
        log.subscribe(view.on_message)
        compactor = Compactor(tree, view, None, 'system', [], workers=4)
        compactor.start()
        for i in range(16):
            log.append('user' if i % 2 else 'tool', ('keep user words ' if i % 2 else 'output ') * 30)
        assert compactor.wait_until_summarized(15, timeout=5)
        assert tree.is_built(4, 0)
        assert tree.node(1, 0).startswith('[user] keep user words')
        assert len(view.render().encode()) <= 200
        compactor.stop()
        assert not any(t.is_alive() for t in compactor.threads)
        log.append('note', 'long note ' * 30)
        compactor.start()
        try:
            assert compactor.wait_until_summarized(16, timeout=3)
        finally:
            compactor.stop()


def test_backend_error_falls_back_and_wait_times_out_without_workers(tmp_path):
    with ChatLog(tmp_path / 'log') as log:
        tree = SummaryTree(tmp_path, log, node_bytes=32)
        view = View(tmp_path, tree)
        log.subscribe(view.on_message)
        log.append('user', 'important exact request ' * 20)
        compactor = Compactor(tree, view, ScriptedBackend([RuntimeError('offline')]),
                              'system', [], workers=1, retries=0)
        assert not compactor.wait_until_summarized(0, timeout=.01)
        compactor.start()
        try:
            assert compactor.wait_until_summarized(0, timeout=3)
            assert tree.node(0, 0).startswith('[user] important exact request')
        finally:
            compactor.stop()


def test_short_leaf_needs_no_backend_and_successful_retry_is_preserved(tmp_path):
    with ChatLog(tmp_path / 'log') as log:
        tree = SummaryTree(tmp_path, log, node_bytes=32)
        view = View(tmp_path, tree)
        log.subscribe(view.on_message)
        backend = ScriptedBackend(['too much ' * 10, '[user] remember the request'])
        compactor = Compactor(tree, view, backend, 'system', [], workers=1)
        compactor.start()
        try:
            log.append('user', 'short')
            assert compactor.wait_until_summarized(0, .1)
            assert backend.calls == []
            log.append('user', 'remember the request ' * 10)
            assert compactor.wait_until_summarized(1, 3)
            assert tree.node(0, 1) == '[user] remember the request'
        finally:
            compactor.stop()


def test_concurrent_arrivals_and_workers_do_not_lose_coverage(tmp_path):
    with ChatLog(tmp_path / 'log') as log:
        tree = SummaryTree(tmp_path, log, node_bytes=48)
        view = View(tmp_path, tree, high_bytes=240, low_bytes=120)
        log.subscribe(view.on_message)
        compactor = Compactor(tree, view, None, 'system', [], workers=4)
        compactor.start()
        errors = []
        # Bound a single round's progress rather than 40 durable appends at once.
        # All three writers contend for the log while the four workers run.
        rounds = threading.Barrier(4, timeout=10)

        def append_messages():
            try:
                for _ in range(40):
                    rounds.wait()
                    log.append('user', 'request é ' * 12)
                    view.render()
                    rounds.wait()
            except Exception as error:
                errors.append(error)
                rounds.abort()

        writers = []
        for _ in range(3):
            thread = threading.Thread(target=append_messages, daemon=True)
            thread.start()
            writers.append(thread)
        try:
            for _ in range(40):
                rounds.wait()
                rounds.wait()
            for thread in writers:
                thread.join(timeout=10)
                assert not thread.is_alive(), 'lock ordering deadlock'
            assert errors == []
            assert compactor.wait_until_summarized(119, timeout=10)
            assert len(view.render().encode()) <= 240
            end = 0
            for level, index in view.nodes:
                assert index * (1 << level) == end
                end += 1 << level
            assert end == 120
        finally:
            rounds.abort()
            for thread in writers:
                thread.join(timeout=10)
            compactor.stop()


def test_start_resumes_batch_when_nodes_were_saved_before_view_notification(tmp_path):
    with ChatLog(tmp_path / 'log') as log:
        tree = SummaryTree(tmp_path, log, node_bytes=32)
        view = View(tmp_path, tree, high_bytes=90, low_bytes=30)
        log.subscribe(view.on_message)
        for _ in range(4):
            log.append('user', 'x' * 20)
        pending_view = (tmp_path / 'view.json').read_bytes()
        tree.set_node(1, 0, 'first')
        tree.set_node(1, 1, 'last')
        tree.set_node(2, 0, 'all')
    (tmp_path / 'view.json').write_bytes(pending_view)
    with ChatLog(tmp_path / 'log') as log:
        tree = SummaryTree(tmp_path, log, node_bytes=32)
        view = View(tmp_path, tree, high_bytes=90, low_bytes=30)
        assert len(view.render().encode()) > 90
        compactor = Compactor(tree, view, None, 'system', [])
        compactor.start()
        try:
            assert view.nodes == [(2, 0)]
            assert len(view.render().encode()) <= 30
        finally:
            compactor.stop()
