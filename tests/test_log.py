import json
import threading

import pytest

from tessera.log import ChatLog


def test_log_reopens_durable_records_and_notifies_in_order(tmp_path):
    seen = []
    with ChatLog(tmp_path) as log:
        log.subscribe(seen.append)
        first = log.append('user', 'hello 🌍', {'turn': 1})
        log.append('agent', 'reply')
        assert first.i == 0
        assert first.size == len('hello 🌍'.encode())
        assert [m.kind for m in seen] == ['user', 'agent']
        with pytest.raises(RuntimeError, match='locked'):
            ChatLog(tmp_path)
    with ChatLog(tmp_path) as reopened:
        assert reopened.count() == 2
        assert reopened.get(0).meta == {'turn': 1}
        assert [m.text for m in reopened.iter(1)] == ['reply']
    rows = []
    for path in tmp_path.glob('*.jsonl'):
        rows.extend(json.loads(line) for line in path.read_text().splitlines())
    assert len(rows) == 2


def test_concurrent_appends_have_contiguous_ids(tmp_path):
    with ChatLog(tmp_path) as log:
        threads = []
        for n in range(5):
            thread = threading.Thread(target=lambda: [log.append('work', 'ok') for _ in range(20)])
            threads.append(thread)
            thread.start()
        for thread in threads:
            thread.join()
        assert [m.i for m in log.iter()] == list(range(100))


def test_subscriber_can_wait_for_another_log_reader(tmp_path):
    observed = []
    with ChatLog(tmp_path) as log:
        def subscriber(message):
            reader = threading.Thread(target=lambda: observed.append(log.get(message.i).text), daemon=True)
            reader.start()
            reader.join(timeout=1)
            assert not reader.is_alive(), 'Subscriber ran while the reader lock was held'
        log.subscribe(subscriber)
        log.append('work', 'visible')
    assert observed == ['visible']


def test_credentials_are_redacted_in_text_and_metadata(tmp_path):
    with ChatLog(tmp_path, secret_values=['credential-value']) as log:
        message = log.append('tool', 'credential-value', {'arguments': {'token': 'credential-value'}})
        assert message.text == '[REDACTED]'
        assert message.meta['arguments']['token'] == '[REDACTED]'
    assert 'credential-value' not in next(tmp_path.glob('*.jsonl')).read_text()


def test_crash_partial_tail_preserves_committed_messages_and_allows_append(tmp_path):
    with ChatLog(tmp_path) as log:
        log.append('user', 'committed')
    path = next(tmp_path.glob('*.jsonl'))
    original = path.read_bytes()
    fragment = b'{"i":1,"kind":"agent","text":"torn \xf0\x9f'
    path.write_bytes(original + fragment)
    with ChatLog(tmp_path) as recovered:
        assert recovered.count() == 1
        recovered.append('agent', 'recovered')
    with ChatLog(tmp_path) as final:
        assert [m.text for m in final.iter()] == ['committed', 'recovered']
    assert any(file.read_bytes() == fragment for file in tmp_path.glob('*.incomplete*'))
