import json

import pytest

from tessera.log import ChatLog
from tessera.tree import SummaryTree


def test_short_leaves_are_exact_and_nodes_are_immutable(tmp_path):
    with ChatLog(tmp_path / 'log') as log:
        tree = SummaryTree(tmp_path, log, node_bytes=16)
        log.append('user', 'é' * 8)
        assert tree.node(0, 0) == 'é' * 8
        assert tree.ready_leaves().empty()
        log.append('tool', 'long output ' * 10)
        assert tree.ready_leaves().get_nowait() == (0, 1)
        tree.set_node(0, 1, '[tool] result')
        assert tree.ready_merges().get_nowait() == (1, 0)
        tree.set_node(1, 0, '[user] request')
        tree.set_node(1, 0, '[user] request')
        with pytest.raises(ValueError, match='immutable'):
            tree.set_node(1, 0, 'changed')
        with pytest.raises(ValueError, match='bytes'):
            tree.set_node(0, 1, '🙂' * 5)
    records = []
    for path in (tmp_path / 'tree').glob('*.jsonl'):
        for line in path.read_text().splitlines():
            records.append(json.loads(line))
    assert len(records) == 3
    with ChatLog(tmp_path / 'log') as log:
        restored = SummaryTree(tmp_path, log, node_bytes=16)
        assert restored.node(1, 0) == '[user] request'
        assert restored.ready_leaves().empty()
        assert restored.ready_merges().empty()


def test_restart_queues_only_missing_dependencies(tmp_path):
    with ChatLog(tmp_path / 'log') as log:
        tree = SummaryTree(tmp_path, log, node_bytes=16)
        for _ in range(4):
            log.append('user', 'short')
        tree.set_node(1, 0, 'first two')
        restored = SummaryTree(tmp_path, log, node_bytes=16)
        assert restored.ready_merges().get_nowait() == (1, 1)
        restored.set_node(1, 1, 'last two')
        assert restored.ready_merges().get_nowait() == (2, 0)
        with pytest.raises(ValueError):
            restored.set_node(2, 1, 'missing children')


@pytest.mark.parametrize('tail', [b'{"level":0,"index":1,"text":"partial', b'{"text":"\xf0\x9f'])
def test_torn_final_record_is_preserved_and_missing_work_resumes(tmp_path, tail):
    with ChatLog(tmp_path / 'log') as log:
        tree = SummaryTree(tmp_path, log, node_bytes=16)
        log.append('user', 'intact')
        log.append('user', 'long original request ' * 10)
    damaged = tmp_path / 'tree' / '0.jsonl'
    original = damaged.read_bytes() + tail
    damaged.write_bytes(original)
    with ChatLog(tmp_path / 'log') as log:
        restored = SummaryTree(tmp_path, log, node_bytes=16)
        assert restored.node(0, 0) == 'intact'
        assert restored.node(0, 1) is None
        assert restored.ready_leaves().get_nowait() == (0, 1)
        restored.set_node(0, 1, '[user] request')
    assert damaged.read_bytes() == original
    with ChatLog(tmp_path / 'log') as log:
        restored = SummaryTree(tmp_path, log, node_bytes=16)
        assert restored.node(0, 0) == 'intact'
        assert restored.node(0, 1) == '[user] request'
        log.append('agent', 'reply')
    assert len(list((tmp_path / 'tree').glob('0*.jsonl'))) == 2


def test_complete_corrupt_record_is_rejected(tmp_path):
    with ChatLog(tmp_path / 'log') as log:
        tree = SummaryTree(tmp_path, log)
        log.append('user', 'intact')
    with (tmp_path / 'tree' / '0.jsonl').open('ab') as stream:
        stream.write(b'{corrupt}\n')
    with ChatLog(tmp_path / 'log') as log:
        with pytest.raises(json.JSONDecodeError):
            SummaryTree(tmp_path, log)


def test_node_metadata_is_atomic_detached_immutable_and_survives_restart(tmp_path):
    metadata = {'faithfulness_score': .95, 'faithfulness_scores': [.2, .95]}
    with ChatLog(tmp_path / 'log') as log:
        tree = SummaryTree(tmp_path, log, node_bytes=32)
        log.append('user', 'long original request ' * 10)
        assert tree.node_meta(0, 0) is None
        tree.set_node(0, 0, '[user] original request', meta=metadata)
        metadata['faithfulness_scores'].append(.1)
        snapshot = tree.node_meta(0, 0)
        snapshot['faithfulness_scores'].append(.4)
        expected = {'faithfulness_score': .95, 'faithfulness_scores': [.2, .95]}
        assert tree.node_meta(0, 0) == expected
        tree.set_node(0, 0, '[user] original request', meta=expected)
        tree.set_node(0, 0, '[user] original request')
        with pytest.raises(ValueError, match='immutable'):
            tree.set_node(0, 0, '[user] original request', meta={'faithfulness_score': .5})
        log.append('agent', 'legacy raw leaf')
        assert tree.node_meta(0, 1) == {}
    records = []
    for path in (tmp_path / 'tree').glob('*.jsonl'):
        records.extend(json.loads(line) for line in path.read_text().splitlines())
    assert len(records) == 2
    assert records[0]['text'] == '[user] original request'
    assert records[0]['meta'] == expected
    assert 'meta' not in records[1]
    with ChatLog(tmp_path / 'log') as log:
        tree = SummaryTree(tmp_path, log, node_bytes=32)
        assert tree.node_meta(0, 0) == expected
        assert tree.node_meta(0, 1) == {}


def test_invalid_metadata_does_not_commit_a_node(tmp_path):
    with ChatLog(tmp_path / 'log') as log:
        tree = SummaryTree(tmp_path, log, node_bytes=16)
        log.append('user', 'request ' * 20)
        for meta in ([.9], {'faithfulness_score': float('nan')}):
            with pytest.raises(ValueError):
                tree.set_node(0, 0, 'summary', meta=meta)
            assert tree.node(0, 0) is None
        assert list((tmp_path / 'tree').glob('*.jsonl')) == []
