"""Real-log acceptance for bounded, persistent session memory."""

from tessera.compactor import Compactor
from tessera.log import ChatLog
from tessera.tree import SummaryTree
from tessera.view import View


def test_5000_messages_keep_bounded_views_stable_prefixes_and_restart(tmp_path):
    stable_prefixes = 0
    merge_batches = 0
    maximum_bytes = 0
    kinds = ('user', 'agent', 'tool', 'echo', 'work', 'note')
    with ChatLog(tmp_path / 'log') as log:
        tree = SummaryTree(tmp_path, log, node_bytes=64)
        view = View(tmp_path, tree, high_bytes=4096, low_bytes=2048)
        log.subscribe(view.on_message)
        compactor = Compactor(tree, view, None, 'system', [], workers=4)
        compactor.start()
        previous_nodes = []
        previous_prefix = b'<chat>\n'
        try:
            for i in range(5000):
                text = f'message {i}: remember café and 🙂'
                if i % 5 == 0:
                    text += '\n' + 'extra tool evidence \\ details ' * 8
                message = log.append(kinds[i % len(kinds)], text)
                assert compactor.wait_until_summarized(i, timeout=10), compactor.errors
                rendered = view.render().encode('utf-8')
                assert len(rendered) <= 4096, (i, len(rendered))
                maximum_bytes = max(maximum_bytes, len(rendered))
                rows = view.lines()
                assert sum(row['bytes'] for row in rows) + 14 == len(rendered)
                position = 0
                for row in rows:
                    assert row['start'] == position
                    position += row['count']
                assert position == i + 1
                nodes = list(view.nodes)
                if nodes[:len(previous_nodes)] == previous_nodes:
                    assert rendered.startswith(previous_prefix)
                    stable_prefixes += 1
                else:
                    merge_batches += 1
                previous_nodes = nodes
                previous_prefix = rendered.removesuffix(b'</chat>')
            assert log.count() == 5000
            assert stable_prefixes > 4000
            assert merge_batches > 10
            assert maximum_bytes > 2048
            assert compactor.errors == []
            assert view.zoom(4999, 1) == message.text
        finally:
            compactor.stop()
        expected_render = view.render()
        expected_rows = view.lines()
        saved_view = (tmp_path / 'view.json').read_bytes()
    with ChatLog(tmp_path / 'log') as log:
        restored_tree = SummaryTree(tmp_path, log, node_bytes=64)
        restored_view = View(tmp_path, restored_tree, high_bytes=4096, low_bytes=2048)
        assert restored_view.render() == expected_render
        assert restored_view.lines() == expected_rows
        assert (tmp_path / 'view.json').read_bytes() == saved_view
        assert restored_view.zoom(4999, 1) == message.text
    print(f'5000 messages: max view={maximum_bytes}/4096 bytes, '
          f'{stable_prefixes} stable prefixes, {merge_batches} merge batches; restart exact')


def test_memory_components_keep_distinct_session_roots_and_workers(tmp_path):
    alpha_root = tmp_path / 'alpha'
    beta_root = tmp_path / 'beta'
    with ChatLog(alpha_root / 'log') as alpha_log, ChatLog(beta_root / 'log') as beta_log:
        alpha_tree = SummaryTree(alpha_root, alpha_log, node_bytes=32)
        beta_tree = SummaryTree(beta_root, beta_log, node_bytes=48)
        alpha = View(alpha_root, alpha_tree, high_bytes=1024, low_bytes=512)
        beta = View(beta_root, beta_tree, high_bytes=2048, low_bytes=1024)
        alpha_log.subscribe(alpha.on_message)
        beta_log.subscribe(beta.on_message)
        alpha_compactor = Compactor(alpha_tree, alpha, None, 'alpha system', [], workers=2)
        beta_compactor = Compactor(beta_tree, beta, None, 'beta system', [], workers=2)
        alpha_compactor.start()
        beta_compactor.start()
        try:
            for i in range(64):
                alpha_log.append('user', f'alpha {i} ' * 15)
                beta_log.append('user', f'beta {i} ' * 15)
            assert alpha_compactor.wait_until_summarized(63, timeout=10)
            assert beta_compactor.wait_until_summarized(63, timeout=10)
            assert alpha_tree.node(6, 0).startswith('[user] alpha 0')
            assert beta_tree.node(6, 0).startswith('[user] beta 0')
            beta_render = beta.render()
            beta_saved = beta.path.read_bytes()
            for i in range(64, 80):
                alpha_log.append('agent', f'alpha only {i} ' * 15)
            assert alpha_compactor.wait_until_summarized(79, timeout=10)
            assert beta_log.count() == 64
            assert beta.render() == beta_render
            assert beta.path.read_bytes() == beta_saved
            assert 'beta' not in alpha.render()
            assert 'alpha' not in beta.render()
            assert alpha.zoom(63, 1).startswith('alpha 63')
            assert beta.zoom(63, 1).startswith('beta 63')
        finally:
            alpha_compactor.stop()
            beta_compactor.stop()
    with ChatLog(beta_root / 'log') as log:
        tree = SummaryTree(beta_root, log, node_bytes=48)
        restored = View(beta_root, tree, high_bytes=2048, low_bytes=1024)
        assert restored.render() == beta_render
        assert restored.path.read_bytes() == beta_saved
