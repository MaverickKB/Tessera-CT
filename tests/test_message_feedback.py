from fake_backend import FakeBackend
from test_server import running_server, call


def wait_idle(server, slug):
    """Wait for a reply and for the runtime to report idle.

    A reply is appended at loop.py before turn_end fires, and the runtime's
    active flag clears only after the turn_end hooks finish (capture, runtime
    save, learning observation). Returning on the reply alone left a window in
    which a caller could act on a session the manager still calls busy: the
    delete route refuses in that window, correctly, because deleting there would
    drop the hooks' work. So wait on the same predicate delete() checks.
    """
    import time
    deadline = time.time() + 10
    messages = []
    replied = False
    while time.time() < deadline:
        messages = call(server, 'GET', f'/api/sessions/{slug}/messages')[1]
        replied = any(m['kind'] == 'agent' and m['text'] for m in messages)
        if replied:
            runtime = server.manager._runtimes.get(slug)
            if runtime is None or runtime.idle():
                return messages
        time.sleep(0.05)
    raise AssertionError('reply landed but the session never went idle' if replied else 'no reply')


def test_marks_flags_resolve_and_clear_through_the_api(tmp_path):
    with running_server(tmp_path) as server:
        slug = call(server, 'POST', '/api/sessions', {'title': 'Marks'})[1]['slug']
        call(server, 'POST', f'/api/sessions/{slug}/messages', {'text': 'hello'})
        reply = [m for m in wait_idle(server, slug) if m['kind'] == 'agent'][-1]['i']
        path = f'/api/sessions/{slug}/feedback'
        assert call(server, 'GET', path) == (200, {})
        status, marks = call(server, 'POST', path, {'i': reply, 'rating': 'flag', 'note': 'wrong number'})
        assert status == 200 and marks[str(reply)]['rating'] == 'flag' and marks[str(reply)]['resolved'] is False
        marks = call(server, 'POST', path, {'i': reply, 'resolved': True})[1]
        assert marks[str(reply)] | {'updated': None} == {'rating': 'flag', 'resolved': True, 'note': 'wrong number',
                                                          'updated': None}
        assert call(server, 'GET', path)[1] == marks
        marks = call(server, 'POST', path, {'i': reply, 'rating': 'good'})[1]
        assert marks[str(reply)]['rating'] == 'good' and marks[str(reply)]['resolved'] is False
        assert call(server, 'POST', path, {'i': reply, 'resolved': True})[0] == 400
        assert call(server, 'POST', path, {'i': reply, 'rating': None})[1] == {}
        assert call(server, 'POST', path, {'i': 999, 'rating': 'good'})[0] == 400
        assert call(server, 'POST', path, {'i': reply, 'rating': 'meh'})[0] == 400
        assert call(server, 'POST', path, {'i': reply, 'resolved': True})[0] == 400
        assert len(list(tmp_path.rglob(f'{slug}/feedback.json'))) == 1


def test_a_reply_quotes_the_earlier_message_into_the_turn(tmp_path):
    with running_server(tmp_path, ['Line one.\nLine two.', 'Got it.']) as server:
        slug = call(server, 'POST', '/api/sessions', {'title': 'Reply'})[1]['slug']
        call(server, 'POST', f'/api/sessions/{slug}/messages', {'text': 'first'})
        reply = [m for m in wait_idle(server, slug) if m['kind'] == 'agent'][-1]
        status, _ = call(server, 'POST', f'/api/sessions/{slug}/messages', {'text': 'about that', 'reply_to': reply['i']})
        assert status == 202
        import time
        deadline = time.time() + 10
        while time.time() < deadline:
            users = [m for m in call(server, 'GET', f'/api/sessions/{slug}/messages')[1] if m['kind'] == 'user']
            if len(users) == 2:
                break
            time.sleep(0.05)
        quoted = users[-1]['text']
        assert quoted.startswith(f'> Replying to ') and f'(message {reply["i"]})' in quoted
        assert '> ' + reply['text'].splitlines()[0] in quoted and quoted.endswith('\n\nabout that')
        assert call(server, 'POST', f'/api/sessions/{slug}/messages', {'text': 'x', 'reply_to': 'one'})[0] == 400


def test_messages_tail_and_clip_keep_session_open_light(tmp_path):
    (tmp_path / 'long.txt').write_text('y' * 500)
    script = [{'tool_calls': [{'id': 'c1', 'name': 'read_file', 'arguments': {'path': str(tmp_path / 'long.txt')}}]}, 'x' * 50]
    with running_server(tmp_path, script) as server:
        slug = call(server, 'POST', '/api/sessions', {'title': 'Tail'})[1]['slug']
        call(server, 'POST', f'/api/sessions/{slug}/messages', {'text': 'read long.txt with read_file'})
        import time
        deadline = time.time() + 10
        while time.time() < deadline:
            everything = call(server, 'GET', f'/api/sessions/{slug}/messages')[1]
            if any(m['kind'] == 'agent' and m['text'] == 'x' * 50 for m in everything):
                break
            time.sleep(0.05)
        newest = call(server, 'GET', f'/api/sessions/{slug}/messages?tail=1')[1]
        assert [m['i'] for m in newest] == [everything[-1]['i']]
        notes = [m for m in everything if m['kind'] in ('note', 'echo') and len(m['text']) > 10]
        assert notes, [m['kind'] for m in everything]
        clipped = {m['i']: m for m in call(server, 'GET', f'/api/sessions/{slug}/messages?clip=10')[1]}
        for note in notes:
            assert clipped[note['i']]['text'] == note['text'][:10]
            assert clipped[note['i']]['meta']['clipped'] == len(note['text'])
        agent = [m for m in everything if m['kind'] == 'agent'][-1]
        assert clipped[agent['i']]['text'] == agent['text']
        assert call(server, 'GET', f'/api/sessions/{slug}/messages?tail=-1')[0] == 400
