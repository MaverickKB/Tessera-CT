import json

import pytest

from test_server import call, running_server
from tessera.codex_headers import codex_headers, official_codex_url
from tessera.log import ChatLog, read_tail


def test_read_tail_returns_the_newest_lines_without_the_older_file(tmp_path):
    log = tmp_path / 'log'
    log.mkdir()
    older = log / '2026-10-01.jsonl'
    newer = log / '2026-10-02.jsonl'
    older.write_text(json.dumps({'i': 1, 'text': 'old'}) + '\n', encoding='utf-8')
    lines = []
    for index in range(3):
        lines.append(json.dumps({'i': index + 2, 'text': 'new-' + str(index)}))
    newer.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    rows = read_tail(log, 2)
    assert [row['text'] for row in rows] == ['new-1', 'new-2']


def test_read_tail_skips_an_unterminated_final_record_in_the_newest_file(tmp_path):
    """An interrupted append must not deny the reader the committed history.

    ChatLog.append writes the newline before it fsyncs, so the bytes after the
    last newline are uncommitted. Parsing that fragment raised
    JSONDecodeError and lost the whole page, not just the interrupted message.
    """
    log = tmp_path / 'log'
    log.mkdir()
    (log / '2026-10-22.jsonl').write_bytes(
        b'{"i": 0, "text": "first"}\n{"i": 1, "text": "second"}\n'
        b'{"i": 2, "text": "hel'
    )
    rows = read_tail(log, 2)
    assert [row['text'] for row in rows] == ['first', 'second']


def test_read_tail_falls_back_to_the_older_file_when_the_newest_is_only_a_fragment(
    tmp_path,
):
    log = tmp_path / 'log'
    log.mkdir()
    (log / '2026-10-21.jsonl').write_bytes(
        b'{"i": 0, "text": "a"}\n{"i": 1, "text": "b"}\n'
    )
    (log / '2026-10-22.jsonl').write_bytes(b'{"i": 2,')
    rows = read_tail(log, 3)
    assert [row['text'] for row in rows] == ['a', 'b']


def test_read_tail_returns_nothing_when_only_a_fragment_was_written(tmp_path):
    log = tmp_path / 'log'
    log.mkdir()
    (log / '2026-10-22.jsonl').write_bytes(b'{"i": 0, "text": "hal')
    assert read_tail(log, 2) == []


def test_read_tail_still_rejects_a_corrupt_committed_record(tmp_path):
    """Tolerance is for the interrupted tail only, not for bad committed data."""
    log = tmp_path / 'log'
    log.mkdir()
    (log / '2026-10-22.jsonl').write_bytes(b'{"i": 0, "text": "a"}\nnot-json\n')
    with pytest.raises(json.JSONDecodeError):
        read_tail(log, 2)


def test_read_tail_still_rejects_a_fragment_in_a_closed_older_file(tmp_path):
    log = tmp_path / 'log'
    log.mkdir()
    (log / '2026-10-21.jsonl').write_bytes(b'{"i": 0, "text": "a"}\n{"i": 1,')
    (log / '2026-10-22.jsonl').write_bytes(b'{"i": 2, "text": "c"}\n')
    with pytest.raises(json.JSONDecodeError):
        read_tail(log, 3)


def test_chat_log_recovery_and_read_tail_agree_after_an_interrupted_append(
    tmp_path,
):
    log = tmp_path / 'log'
    log.mkdir()

    def record(index, kind, text):
        return (
            json.dumps(
                {
                    'i': index,
                    'kind': kind,
                    'text': text,
                    'size': len(text),
                    'date': '2026-10-22T00:00:00Z',
                    'meta': {},
                }
            )
            + '\n'
        ).encode('utf-8')

    (log / '2026-10-22.jsonl').write_bytes(
        record(0, 'user', 'first')
        + record(1, 'agent', 'second')
        + b'{"i": 2, "kind": "age'
    )
    assert [row['text'] for row in read_tail(log, 2)] == ['first', 'second']
    chat = ChatLog(log)
    try:
        assert [message.text for message in chat.iter(0)] == ['first', 'second']
    finally:
        chat.close()
    assert [row['text'] for row in read_tail(log, 2)] == ['first', 'second']
    # The fragment is kept as evidence rather than silently discarded.
    assert any('.incomplete-' in path.name for path in log.iterdir())


def test_messages_tail_route_still_answers_after_an_interrupted_append(tmp_path):
    """The user-facing symptom: opening a crashed session answered 400.

    The route calls read_tail only when the session has no runtime yet, which
    is exactly the state after a restart — the first paint of a session that
    crashed mid-append.
    """
    with running_server(tmp_path) as server:
        call(server, 'POST', '/api/sessions', {'title': 'crashed'})
        for text in ('one', 'two', 'three'):
            call(server, 'POST', '/api/sessions/crashed/messages', {'text': text})
            assert server.manager.wait('crashed', 5)
        code, whole = call(server, 'GET', '/api/sessions/crashed/messages?tail=10')
        assert code == 200
        committed = [(row['i'], row['text']) for row in whole]

    newest = sorted((tmp_path / 'sessions' / 'crashed' / 'log').glob('*.jsonl'))[-1]
    assert newest.read_bytes().endswith(b'\n')
    with newest.open('ab') as stream:
        stream.write(b'{"i": 99, "kind": "agent", "text": "interrupted mess')

    # A fresh manager has no runtime for this slug, so the route reads disk.
    with running_server(tmp_path) as server:
        code, rows = call(server, 'GET', '/api/sessions/crashed/messages?tail=10')
        assert code == 200
        assert [(row['i'], row['text']) for row in rows] == committed


def test_codex_headers_keep_the_account_claim_and_not_the_token():
    payload = json.dumps({
        'https://api.openai.com/auth': {
            'chatgpt_account_id': 'acct-1',
            'chatgpt_data_residency': 'us',
        },
    }).encode('utf-8')
    import base64
    body = base64.urlsafe_b64encode(payload).decode('ascii').rstrip('=')
    token = 'header.' + body + '.sig'
    headers = codex_headers(token)
    assert headers['ChatGPT-Account-ID'] == 'acct-1'
    assert headers['originator'] == 'tessera'
    assert token not in headers['ChatGPT-Account-ID']
    assert official_codex_url('https://chatgpt.com/backend-api/codex') is True
    assert official_codex_url('https://api.x.ai/v1') is False
