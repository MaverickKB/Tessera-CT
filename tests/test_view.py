import json
import random

import pytest

from tessera.log import ChatLog
from tessera.tree import SummaryTree
from tessera.view import View


def test_a_damaged_tree_line_is_skipped_instead_of_refusing_to_open(tmp_path):
    """The tree is derived data: a damaged node is rebuilt, never fatal.

    A terminated line that cannot be decoded used to raise, which stopped the core
    from serving any session. The log still holds the message, so the node rebuilds.
    """
    with ChatLog(tmp_path / "log") as log:
        SummaryTree(tmp_path / "tree-home", log, node_bytes=64)
        log.append("user", "hello there")
    damaged = tmp_path / "tree-home" / "tree" / "0.jsonl"
    damaged.write_bytes(b'{"level": 0, "index": 0, "text": "bad \\xff\\xfe"}\n')
    with ChatLog(tmp_path / "log") as log:
        tree = SummaryTree(tmp_path / "tree-home", log, node_bytes=64)
        # The damaged node rebuilds from the log; the chat still opens.
        assert tree.node(0, 0) == "hello there"


def setup_view(root, **kwargs):
    log = ChatLog(root / 'log')
    tree = SummaryTree(root, log, node_bytes=32)
    view = View(root, tree, **kwargs)
    log.subscribe(view.on_message)
    return log, tree, view


def build_ready(tree):
    queue = tree.ready_merges()
    while not queue.empty():
        level, index = queue.get_nowait()
        tree.set_node(level, index, 'summary')


def assert_coverage(view, total):
    position = 0
    for level, index in view.nodes:
        assert index * (1 << level) == position
        position += 1 << level
    assert position == total


@pytest.mark.parametrize('seed', range(8))
def test_random_arrival_and_completion_preserves_coverage_and_budget(tmp_path, seed):
    log, tree, view = setup_view(tmp_path, high_bytes=180, low_bytes=95)
    try:
        rng = random.Random(seed)
        for i in range(100):
            log.append(rng.choice(['user', 'agent', 'tool']), 'é' * rng.randrange(1, 15))
            assert_coverage(view, i + 1)
            if rng.random() < .6:
                build_ready(tree)
                assert_coverage(view, i + 1)
                assert len(view.render().encode()) <= 180
        build_ready(tree)
        assert len(view.render().encode()) <= 180
    finally:
        log.close()


def test_binary_counter_merges_and_prefix_stability(tmp_path):
    log, tree, view = setup_view(tmp_path, high_bytes=85, low_bytes=40)
    try:
        snapshots = {}
        for i in range(8):
            log.append('user', 'x' * 20)
            build_ready(tree)
            snapshots[i + 1] = list(view.nodes)
        assert snapshots[2] == [(0, 0), (0, 1)]
        assert snapshots[3] == [(1, 0), (0, 2)]
        assert snapshots[4] == [(1, 0), (1, 1)]
        assert snapshots[5] == [(1, 0), (1, 1), (0, 4)]
        assert snapshots[6] == [(2, 0), (1, 2)]
        assert snapshots[8] == [(2, 0), (2, 1)]
        prefix = view.render().removesuffix('</chat>')
        log.append('agent', 'a')
        assert view.render().startswith(prefix)
    finally:
        log.close()


def test_persistent_view_is_loaded_without_rebuilding(tmp_path):
    log, tree, view = setup_view(tmp_path, high_bytes=85, low_bytes=40)
    for _ in range(7):
        log.append('user', 'x' * 20)
        build_ready(tree)
    expected = view.render()
    saved = (tmp_path / 'view.json').read_bytes()
    log.close()
    with ChatLog(tmp_path / 'log') as reopened:
        restored_tree = SummaryTree(tmp_path, reopened, node_bytes=32)
        restored = View(tmp_path, restored_tree, high_bytes=85, low_bytes=40)
        assert restored.render() == expected
        assert (tmp_path / 'view.json').read_bytes() == saved
        assert restored.nodes == view.nodes


def test_zoom_validates_ranges_and_pages_full_utf8_message(tmp_path):
    log, tree, view = setup_view(tmp_path)
    try:
        raw = '🙂é\n' * 100
        log.append('tool', raw)
        assert '(no summary yet; zoom in for the raw messages)' in view.render()
        log.append('user', 'request')
        assert view.zoom(0, 2).startswith('0+1|')
        assert '1+1|request' in view.zoom(0, 2)
        assert view.zoom(0, 1) == raw
        offset = 0
        pages = []
        while offset < len(raw.encode()):
            page = view.zoom(0, 1, offset=offset, limit=17)
            assert len(page.encode()) <= 17
            pages.append(page)
            offset += len(page.encode())
        assert ''.join(pages) == raw
        assert view.date(0) == log.get(0).date
        for start, count in [(0, 0), (0, 3), (1, 2), (-1, 1), (2, 1)]:
            with pytest.raises((ValueError, IndexError)):
                view.zoom(start, count)
    finally:
        log.close()


def test_waiting_batch_resumes_as_parents_become_available(tmp_path):
    log, tree, view = setup_view(tmp_path, high_bytes=90, low_bytes=30)
    try:
        for _ in range(4):
            log.append('user', 'x' * 20)
        assert len(view.render().encode()) > 90
        tree.set_node(1, 1, 'newer')
        assert_coverage(view, 4)
        tree.set_node(1, 0, 'older')
        tree.set_node(2, 0, 'all')
        assert view.nodes == [(2, 0)]
        assert len(view.render().encode()) <= 30
        state = json.loads((tmp_path / 'view.json').read_text())
        assert state['nodes'] == [[2, 0]]
    finally:
        log.close()


def test_huge_raw_zoom_uses_pages_by_default(tmp_path):
    log, tree, view = setup_view(tmp_path)
    try:
        log.append('tool', '🙂' * 10000)
        page = view.zoom(0, 1)
        assert len(page.encode()) < 16500
        assert 'offset=16000' in page
        assert '🙂' * 4000 in page
    finally:
        log.close()


def test_log_suffix_recovery_preserves_saved_prefix(tmp_path):
    log, tree, view = setup_view(tmp_path)
    log.append('user', 'first')
    prefix = view.render().removesuffix('</chat>')
    saved = (tmp_path / 'view.json').read_bytes()
    log.append('agent', 'second')
    log.close()
    # Simulate a crash after the second log append but before view persistence.
    (tmp_path / 'view.json').write_bytes(saved)
    with ChatLog(tmp_path / 'log') as reopened:
        restored = View(tmp_path, SummaryTree(tmp_path, reopened, node_bytes=32))
        assert restored.render().startswith(prefix)
        assert_coverage(restored, 2)
        assert '1+1|second' in restored.render()


def test_due_priority_prefers_newer_small_span_over_older_large_span(tmp_path):
    log, tree, view = setup_view(tmp_path, high_bytes=500, low_bytes=130)
    try:
        for _ in range(8):
            log.append('user', 'x' * 20)
        build_ready(tree)
        view.high_bytes = 180
        view.on_node(3, 0)
        assert view.nodes == [(1, 0), (1, 1), (1, 2), (0, 6), (0, 7)]
    finally:
        log.close()


def test_equal_due_priority_prefers_older_span(tmp_path):
    log, tree, view = setup_view(tmp_path, high_bytes=500, low_bytes=102)
    try:
        for _ in range(7):
            log.append('user', 'x' * 20)
        build_ready(tree)
        # Seed a valid persisted frontier: two older pairs and three leaves.
        state = {'version': 1, 'nodes': [[1, 0], [1, 1], [0, 4], [0, 5], [0, 6]],
                 'count': 7, 'compacting': False}
        (tmp_path / 'view.json').write_text(json.dumps(state))
        view.load()
        view.high_bytes = 110
        view.on_node(2, 0)
        assert view.nodes == [(2, 0), (0, 4), (0, 5), (0, 6)]
    finally:
        log.close()


def test_lines_return_prompt_byte_cost_raw_text_and_newest_first_age_rank(tmp_path):
    log, tree, view = setup_view(tmp_path)
    try:
        assert view.lines() == []
        log.append('user', 'é\n\\')
        log.append('agent', 'reply')
        log.append('tool', 'large output ' * 20)
        assert view.lines() == [
            {'start': 0, 'count': 1, 'level': 0, 'text': 'é\n\\', 'bytes': 11, 'age_rank': 2},
            {'start': 1, 'count': 1, 'level': 0, 'text': 'reply', 'bytes': 10, 'age_rank': 1},
            {'start': 2, 'count': 1, 'level': 0,
             'text': '(no summary yet; zoom in for the raw messages)', 'bytes': 51, 'age_rank': 0},
        ]
        lines = view.lines()
        assert sum(line['bytes'] for line in lines) + 14 == len(view.render().encode())
        # The caller can annotate its JSON snapshot without changing the view.
        lines[0]['text'] = 'changed'
        lines.pop()
        assert view.lines()[0]['text'] == 'é\n\\'
        assert len(view.lines()) == 3
    finally:
        log.close()


def test_prompt_render_keeps_the_newest_lines_and_the_saved_view(tmp_path):
    log, tree, view = setup_view(tmp_path, high_bytes=400, low_bytes=200)
    try:
        for index in range(6):
            log.append('user', f'line-{index}-' + ('x' * 12))
        full = view.render()
        saved = json.loads((tmp_path / 'view.json').read_text())
        prompt = view.prompt_render(80)
        assert 'Older coverage is omitted' in prompt
        assert 'line-5-' in prompt
        assert 'line-0-' not in prompt
        assert len(prompt.encode()) < len(full.encode())
        assert view.render() == full
        assert json.loads((tmp_path / 'view.json').read_text()) == saved
        assert view.prompt_render(100000) == full
    finally:
        log.close()


def test_lines_describe_persisted_mixed_level_frontier(tmp_path):
    log, tree, view = setup_view(tmp_path, high_bytes=85, low_bytes=40)
    try:
        for _ in range(6):
            log.append('user', 'x' * 20)
            build_ready(tree)
        expected = [
            {'start': 0, 'count': 4, 'level': 2, 'text': 'summary', 'bytes': 12, 'age_rank': 1},
            {'start': 4, 'count': 2, 'level': 1, 'text': 'summary', 'bytes': 12, 'age_rank': 0},
        ]
        assert view.lines() == expected
        view.load()
        assert view.lines() == expected
    finally:
        log.close()
