from contextlib import contextmanager
import http.client
import json
from pathlib import Path
import threading
import time

import pytest

from fake_backend import FakeBackend
from tessera.config import Config
from tessera.server import create_server


@contextmanager
def running_server(tmp_path, script=None, web_root=None):
    config = Config(home=tmp_path, cwd=tmp_path)
    config.server.port = 0
    config.jev.enabled = False
    server = create_server(config, web_root=web_root, backend_factory=lambda slug, name: FakeBackend(script or ['Hello from the model']))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def call(server, method, path, body=None, headers=None):
    connection = http.client.HTTPConnection(*server.server_address, timeout=5)
    encoded = None if body is None else json.dumps(body)
    supplied = {'Content-Type': 'application/json'}
    supplied.update(headers or {})
    connection.request(method, path, body=encoded, headers=supplied)
    response = connection.getresponse()
    data = response.read()
    status = response.status
    connection.close()
    return status, json.loads(data) if data else None


def read_event(response):
    lines = []
    while True:
        line = response.readline().decode().rstrip('\r\n')
        if not line:
            if lines:
                break
            continue
        if not line.startswith(':'):
            lines.append(line)
    event = {}
    for line in lines:
        key, value = line.split(':', 1)
        event[key] = value.strip()
    if 'data' in event:
        event['data'] = json.loads(event['data'])
    return event


def test_http_session_chat_view_zoom_patch_and_status(tmp_path):
    with running_server(tmp_path) as server:
        status, created = call(server, 'POST', '/api/sessions', {'title': 'API subject'})
        assert status == 201
        slug = created['slug']
        status, turn = call(server, 'POST', f'/api/sessions/{slug}/messages', {'text': 'Hello'})
        assert status == 202 and turn['turn_id']
        assert server.manager.wait(slug, 5)
        status, messages = call(server, 'GET', f'/api/sessions/{slug}/messages?after=-1&limit=1')
        assert status == 200
        assert [m['text'] for m in messages] == ['Hello']
        status, view = call(server, 'GET', f'/api/sessions/{slug}/view')
        assert status == 200 and view['lines'][0]['start'] == 0
        assert view['bytes'] > 0 and 'inheritance' in view
        status, zoom = call(server, 'GET', f'/api/sessions/{slug}/zoom?start=0&count=1')
        assert status == 200 and zoom['text'] == 'Hello'
        status, metadata = call(server, 'PATCH', f'/api/sessions/{slug}', {'title': 'Updated', 'archived': True})
        assert status == 200 and metadata['title'] == 'Updated' and metadata['archived']
        status, failed = call(server, 'POST', f'/api/sessions/{slug}/messages', {'text': 'Blocked'})
        assert status == 409
        status, state = call(server, 'GET', '/api/status')
        assert status == 200 and state['debt_total'] == 1


def test_phone_voice_submit_is_accepted(tmp_path):
    with running_server(tmp_path) as server:
        status, created = call(server, 'POST', '/api/sessions', {'title': 'Phone'})
        slug = created['slug']
        status, voiced = call(server, 'POST', f'/api/sessions/{slug}/messages', {'text': 'From the phone', 'source': 'voice'})
        assert status == 202 and voiced['turn_id']
        assert server.manager.autonomy.state['presence'].get(slug, {}).get('last_message') is None


def test_sse_replays_events_and_supports_last_event_id(tmp_path):
    with running_server(tmp_path) as server:
        call(server, 'POST', '/api/sessions', {'title': 'events'})
        call(server, 'POST', '/api/sessions/events/messages', {'text': 'Hello'})
        assert server.manager.wait('events', 5)
        connection = http.client.HTTPConnection(*server.server_address, timeout=5)
        connection.request('GET', '/api/sessions/events/events')
        response = connection.getresponse()
        assert response.status == 200
        assert response.getheader('Content-Type').startswith('text/event-stream')
        events = []
        while True:
            event = read_event(response)
            events.append(event)
            if event.get('event') == 'turn_end':
                break
        connection.close()
        kinds = {event['event'] for event in events}
        assert {'message', 'turn_start', 'route', 'capture', 'turn_end'} <= kinds
        checkpoint = int(events[0]['id'])
        resumed = http.client.HTTPConnection(*server.server_address, timeout=5)
        resumed.request('GET', '/api/sessions/events/events', headers={'Last-Event-ID': str(checkpoint)})
        replay = resumed.getresponse()
        assert int(read_event(replay)['id']) == checkpoint + 1
        resumed.close()


def test_fresh_page_can_skip_replayed_message_bodies(tmp_path):
    """The page fetches the transcript, so the stream must not repeat it."""
    with running_server(tmp_path) as server:
        call(server, 'POST', '/api/sessions', {'title': 'skip'})
        for text in ('Hello', 'Again'):
            call(server, 'POST', '/api/sessions/skip/messages', {'text': text})
            assert server.manager.wait('skip', 5)
        connection = http.client.HTTPConnection(*server.server_address, timeout=5)
        connection.request('GET', '/api/sessions/skip/events?messages=skip')
        response = connection.getresponse()
        assert response.status == 200
        events = []
        while True:
            event = read_event(response)
            events.append(event)
            # The checkpoint is written after the replay window, so read past turn_end.
            if event.get('event') == 'checkpoint':
                break
        connection.close()
        kinds = {event['event'] for event in events}
        assert 'message' not in kinds
        assert {'turn_start', 'route', 'capture', 'turn_end'} <= kinds
        # The checkpoint event carries the newest journal id so a reconnect resumes
        # after the window instead of replaying it.
        checkpoint = [event for event in events if event['event'] == 'checkpoint']
        assert len(checkpoint) == 1 and int(checkpoint[0]['id']) > 0


def test_fresh_sse_connection_replays_only_the_newest_events(tmp_path):
    """A page that opens a long session must not be handed the whole journal."""
    with running_server(tmp_path) as server:
        server.config.server.event_replay_limit = 3
        call(server, 'POST', '/api/sessions', {'title': 'long'})
        call(server, 'POST', '/api/sessions/long/messages', {'text': 'Hello'})
        assert server.manager.wait('long', 5)
        runtime = server.manager.runtime('long')
        total = len(runtime.events.records)
        assert total > 3
        connection = http.client.HTTPConnection(*server.server_address, timeout=5)
        connection.request('GET', '/api/sessions/long/events')
        response = connection.getresponse()
        assert response.status == 200
        first = read_event(response)
        connection.close()
        assert int(first['id']) == total - 3 + 1


def test_sse_checkpoint_still_resumes_exactly(tmp_path):
    """A client holding a checkpoint keeps its place even when a bound is set."""
    with running_server(tmp_path) as server:
        server.config.server.event_replay_limit = 2
        call(server, 'POST', '/api/sessions', {'title': 'held'})
        call(server, 'POST', '/api/sessions/held/messages', {'text': 'Hello'})
        assert server.manager.wait('held', 5)
        connection = http.client.HTTPConnection(*server.server_address, timeout=5)
        connection.request('GET', '/api/sessions/held/events', headers={'Last-Event-ID': '1'})
        response = connection.getresponse()
        assert response.status == 200
        first = read_event(response)
        connection.close()
        assert int(first['id']) == 2


def test_status_poll_does_not_carry_the_inheritance_digest(tmp_path):
    """Status is polled every 15s, so it must not ship view-sized text."""
    with running_server(tmp_path) as server:
        call(server, 'POST', '/api/sessions', {'title': 'poll'})
        call(server, 'POST', '/api/sessions/poll/messages', {'text': 'Hello'})
        assert server.manager.wait('poll', 5)
        status, report = call(server, 'GET', '/api/status')
        assert status == 200
        row = report['sessions']['poll']
        assert 'inheritance' not in row
        assert 'inheritance_loaded' in row
        # The digest still reaches the page, but through the panel it belongs to.
        status, view = call(server, 'GET', '/api/sessions/poll/view')
        assert status == 200 and 'inheritance' in view


def test_view_panel_opens_on_the_newest_lines(tmp_path):
    """The panel must not ship the whole view on every open."""
    with running_server(tmp_path) as server:
        server.config.ui.view_page_lines = 2
        call(server, 'POST', '/api/sessions', {'title': 'panel'})
        for text in ('Hello', 'Again', 'Once more'):
            call(server, 'POST', '/api/sessions/panel/messages', {'text': text})
            assert server.manager.wait('panel', 5)
        code, full = call(server, 'GET', '/api/sessions/panel/view?tail=0')
        assert code == 200 and full['omitted'] == 0
        total = len(full['lines'])
        assert total > 2
        code, tailed = call(server, 'GET', '/api/sessions/panel/view?tail=2')
        assert code == 200
        assert len(tailed['lines']) == 2
        assert tailed['omitted'] == total - 2
        # The newest lines are the ones kept.
        assert [row['start'] for row in tailed['lines']] == [row['start'] for row in full['lines'][-2:]]
        # The byte budget still describes the whole view, not the page of it.
        assert tailed['bytes'] == full['bytes']
        code, whole = call(server, 'GET', '/api/sessions/panel/view?tail=0')
        assert code == 200 and len(whole['lines']) == total and whole['omitted'] == 0


def test_loopback_api_ignores_legacy_auth_and_keeps_static_path_fences(tmp_path, monkeypatch):
    web = tmp_path / 'web'
    web.mkdir()
    (web / 'index.html').write_text('Tessera test page')
    (web / 'app.js').write_text('console.log("safe")')
    (tmp_path / 'private.txt').write_text('private')
    (web / 'escape').symlink_to(tmp_path / 'private.txt')
    monkeypatch.setenv('TESSERA_SERVER_TOKEN', 'test-server-key')
    with running_server(tmp_path / 'home', web_root=web) as server:
        assert call(server, 'GET', '/api/sessions') == (200, [])
        for path, headers in (('/api/status', {'Authorization': 'Bearer wrong'}),
                              ('/api/status', {'Authorization': 'Bearer test-server-key'}),
                              ('/api/status?token=test-server-key', {})):
            code, status = call(server, 'GET', path, headers=headers)
            assert code == 200
            assert 'test-server-key' not in json.dumps(status)
        for path in ('/static/../private.txt', '/static/%2e%2e/private.txt', '/static/escape'):
            connection = http.client.HTTPConnection(*server.server_address, timeout=5)
            connection.request('GET', path)
            response = connection.getresponse()
            assert response.status == 404
            assert b'private' not in response.read()
            connection.close()
        code, created = call(server, 'POST', '/api/sessions', {'title': 'x'})
        assert code == 201 and created['title'] == 'x'
        port = server.server_address[1]
        same = f'http://127.0.0.1:{port}'
        assert call(server, 'GET', '/api/sessions', headers={'Origin': same})[0] == 200
        for headers in ({'Host': 'desktop-main'}, {'Host': f'attacker.example:{port}'},
                        {'Host': '127.0.0.1:1'}, {'Origin': 'null'},
                        {'Origin': 'https://attacker.example'}, {'Origin': f'http://attacker.example:{port}'}):
            code, body = call(server, 'POST', '/api/sessions', {'title': 'rebound'}, headers)
            assert code == 403, headers
        assert [row['title'] for row in server.manager.list_sessions()] == ['x']
        status, single = call(server, 'GET', f"/api/sessions/{created['slug']}")
        assert status == 200 and single == created
        assert call(server, 'GET', '/api/sessions/missing-slug')[0] == 404
        assert call(server, 'POST', f"/api/sessions/{created['slug']}", {})[0] == 405


def test_nonloopback_bind_is_refused(tmp_path):
    config = Config(home=tmp_path, cwd=tmp_path)
    config.server.host = '0.0.0.0'
    config.server.port = 0
    with pytest.raises(ValueError, match='loopback'):
        create_server(config)


def test_burst_of_simultaneous_connections_is_accepted_not_reset(tmp_path):
    # A browser behind tailscale serve opens many upstream connections at once for the page's
    # assets. With socketserver's default backlog of 5, macOS resets the overflow and the proxy
    # answers 502, so scripts never load and every button on the phone is dead.
    with running_server(tmp_path) as server:
        assert server.request_queue_size >= 64
        barrier = threading.Barrier(48)
        results = []

        def fetch():
            connection = http.client.HTTPConnection(*server.server_address, timeout=10)
            try:
                barrier.wait()
                connection.request('GET', '/api/status')
                response = connection.getresponse()
                response.read()
                results.append(response.status)
            except OSError as error:
                results.append(repr(error))
            finally:
                connection.close()

        workers = [threading.Thread(target=fetch) for _ in range(48)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        assert results == [200] * 48, results


def test_status_reports_a_ui_build_that_changes_with_the_assets(tmp_path):
    from tessera.server import ui_build_id
    web = tmp_path / 'webroot'
    web.mkdir()
    (web / 'index.html').write_text('<p>a</p>')
    first = ui_build_id(web)
    assert first == ui_build_id(web)
    (web / 'index.html').write_text('<p>b</p>')
    assert ui_build_id(web) != first


def test_ui_build_id_ignores_files_the_document_never_loads(tmp_path):
    """The reload hash must ignore files the document never loads.

    It hashed every .js under web/, including the browser test files no page ever
    fetches, so a session committing its own tests reloaded the operator's open page
    mid-sentence. The hash is now the document plus the scripts it loads.
    """
    from tessera.server import ui_build_id
    web = tmp_path / 'webroot'
    (web / 'tests').mkdir(parents=True)
    (web / 'index.html').write_text(
        '<script src="/static/app.js" defer></script>'
        '<link rel="stylesheet" href="/static/app.css">'
    )
    (web / 'app.js').write_text('let a = 1;')
    (web / 'app.css').write_text('.a{color:red}')
    (web / 'tests' / 'browser.test.js').write_text('test("x", () => {})')
    base = ui_build_id(web)

    # A test file the browser never loads must not reload anyone's page.
    (web / 'tests' / 'browser.test.js').write_text('test("y", () => {})')
    assert ui_build_id(web) == base

    # A stylesheet applies on the next navigation without a reload, so it is not in the
    # hash either: including it is the same symptom by another door.
    (web / 'app.css').write_text('.a{color:blue}')
    assert ui_build_id(web) == base

    # A script the document does load is a real rebuild.
    (web / 'app.js').write_text('let a = 2;')
    assert ui_build_id(web) != base


def test_a_page_that_attaches_mid_turn_learns_the_turn_is_running(tmp_path):
    """Opening a session mid-work learns that the turn is running.

    Live deltas are never replayed, so a fresh subscriber would otherwise see a
    stalled transcript. The stream sends a resume record naming the running turn
    and the text it has accumulated, and the snapshot does not leak across turns.
    """
    def slow(messages, tools):
        import time
        time.sleep(1.5)
        return {'content': 'slow answer'}

    with running_server(tmp_path, script=[slow]) as server:
        call(server, 'POST', '/api/sessions', {'title': 'mid-turn'})
        call(server, 'POST', '/api/sessions/mid-turn/messages', {'text': 'Work on it'})
        deadline = time.time() + 5
        while time.time() < deadline and not server.manager.runtime('mid-turn').active:
            time.sleep(0.02)
        assert server.manager.runtime('mid-turn').active, 'the turn never started'
        connection = http.client.HTTPConnection(*server.server_address, timeout=5)
        connection.request('GET', '/api/sessions/mid-turn/events')
        response = connection.getresponse()
        assert response.status == 200
        resume = None
        while resume is None:
            event = read_event(response)
            if event.get('event') == 'resume':
                resume = event['data']
        connection.close()
        assert resume['busy'] is True
        assert resume['turn_id']
        assert resume['live'] is None or resume['live']['turn_id'] == resume['turn_id']
        assert server.manager.wait('mid-turn', 10)
