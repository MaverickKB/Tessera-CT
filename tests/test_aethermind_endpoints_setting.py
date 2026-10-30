"""The page must show the ordered endpoints that decide where AetherMind goes.

The wiring lives in `aethermind.endpoints`, which overrides `aethermind.url` in
AetherMindClient. The page shows that list as rows a person can add, remove and
reorder one at a time, because the order is the failover order. A typed URL that
is silently ignored by the client was the original defect.
"""
import threading

from tessera.config import load_config
from tessera.server import create_server
from tessera.credentials import MemoryKeychain
from test_server import call


HOME = '''
[backends.fake]
base_url = "http://127.0.0.1:9/v1"
model = "scripted-local-fake"
chat = true
enabled = true

[roles]
main = "fake"

[voice]
enabled = false

[aethermind]
project = "/synthetic-project"
endpoints = ["http://first.example:1/mcp", "http://second.example:2/mcp"]
'''

FIRST = 'http://first.example:1/mcp'
SECOND = 'http://second.example:2/mcp'


def serve(tmp_path, text=HOME):
    (tmp_path / 'workers').mkdir()
    (tmp_path / 'config.toml').write_text(text)
    config = load_config(tmp_path)
    config.server.port = 0
    server = create_server(config)
    server.manager.settings.keychain = MemoryKeychain()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def stop(server, thread):
    server.shutdown()
    server.server_close()
    thread.join()


def integration_fields(server):
    code, body = call(server, 'GET', '/api/settings')
    assert code == 200
    integrations = next(s for s in body['sections'] if s['id'] == 'integrations')
    return {f['id']: f for f in integrations['fields']}


def test_the_page_shows_the_ordered_endpoints_that_win(tmp_path):
    server, thread = serve(tmp_path)
    try:
        fields = integration_fields(server)
    finally:
        stop(server, thread)
    assert 'aethermind_endpoints' in fields, 'the page cannot show the list that decides'
    records = fields['aethermind_endpoints']['records']
    assert [row['name'] for row in records] == [FIRST, SECOND]
    assert records[0]['status'] == 'First choice'
    assert records[1]['status'] == 'Failover 1'
    # The URL box is empty while two endpoints are live: that is the confusion.
    assert fields['aethermind.url']['value'] == ''


def test_removing_then_adding_an_endpoint_persists_the_new_order(tmp_path):
    server, thread = serve(tmp_path)
    try:
        removed = call(server, 'POST', '/api/settings', {
            'id': 'aethermind_endpoints', 'action': 'remove', 'name': FIRST})
        assert removed[0] == 200, removed[1]
        assert load_config(tmp_path).aethermind.endpoints == [SECOND]
        added = call(server, 'POST', '/api/settings', {
            'id': 'aethermind_endpoints', 'action': 'add', 'record': {'value': FIRST}})
        assert added[0] == 200, added[1]
        assert added[1]['changed'] is True and added[1]['restart_required'] is True
        # Order is the point: the first row wins, so a reorder must persist.
        assert load_config(tmp_path).aethermind.endpoints == [SECOND, FIRST]
    finally:
        stop(server, thread)


def test_url_save_is_refused_while_endpoints_are_set(tmp_path):
    server, thread = serve(tmp_path)
    try:
        before = (tmp_path / 'config.toml').read_text()
        code, body = call(server, 'POST', '/api/settings', {
            'id': 'aethermind.url', 'value': 'http://ignored.example:9/mcp'})
        assert code == 400, body
        assert 'take precedence' in body.get('error', ''), body
        # Refused means untouched, not written-then-rolled-back.
        assert (tmp_path / 'config.toml').read_text() == before
    finally:
        stop(server, thread)


def test_url_save_is_allowed_once_endpoints_are_cleared(tmp_path):
    server, thread = serve(tmp_path)
    try:
        for url in (FIRST, SECOND):
            cleared = call(server, 'POST', '/api/settings', {
                'id': 'aethermind_endpoints', 'action': 'remove', 'name': url})
            assert cleared[0] == 200, cleared[1]
        assert load_config(tmp_path).aethermind.endpoints == []
        code, body = call(server, 'POST', '/api/settings', {
            'id': 'aethermind.url', 'value': 'http://now-used.example:9/mcp'})
        assert code == 200, body
        saved = load_config(tmp_path).aethermind
        assert saved.url == 'http://now-used.example:9/mcp' and saved.endpoints == []
    finally:
        stop(server, thread)


def test_a_credential_bearing_endpoint_is_refused_before_the_file_is_touched(tmp_path):
    server, thread = serve(tmp_path)
    try:
        before = (tmp_path / 'config.toml').read_text()
        code, body = call(server, 'POST', '/api/settings', {
            'id': 'aethermind_endpoints', 'action': 'add',
            'record': {'value': 'https://user:secret@example.com/mcp'}})
        assert code == 400, body
        assert 'without credentials' in body.get('error', ''), body
        assert (tmp_path / 'config.toml').read_text() == before
    finally:
        stop(server, thread)


def test_a_duplicate_endpoint_is_refused(tmp_path):
    server, thread = serve(tmp_path)
    try:
        code, body = call(server, 'POST', '/api/settings', {
            'id': 'aethermind_endpoints', 'action': 'add', 'record': {'value': FIRST}})
        assert code == 400, body
        assert 'already in the list' in body.get('error', ''), body
        assert load_config(tmp_path).aethermind.endpoints == [FIRST, SECOND]
    finally:
        stop(server, thread)


def test_the_url_box_says_out_loud_that_it_is_not_used(tmp_path):
    """A glance at the page must not read as 'AetherMind points nowhere'."""
    server, thread = serve(tmp_path)
    try:
        fields = integration_fields(server)
    finally:
        stop(server, thread)
    # Assert the meaning, not one exact sentence. The status legitimately gained the
    # way back out ("Clear that list...") in 07feac7, and pinning the full string made
    # an improvement read as a regression. What this test exists to protect: the page
    # says the URL is not in use, says how many endpoints supersede it, and says they
    # win. Substring checks match how the rest of this file asserts prose.
    status = fields['aethermind.url']['status']
    assert status.startswith('Not used: 2 ordered endpoint'), status
    assert 'take precedence' in status, status
    # The field that is superseded is also marked inert, so the page can disable it
    # rather than render a box that refuses every save.
    assert fields['aethermind.url']['inert'] is True


def test_no_endpoints_reports_the_packaged_engine(tmp_path):
    server, thread = serve(tmp_path, HOME.replace(
        'endpoints = ["http://first.example:1/mcp", "http://second.example:2/mcp"]', ''))
    try:
        fields = integration_fields(server)
    finally:
        stop(server, thread)
    assert fields['aethermind.url']['status'].startswith('bytes present, not started')
    assert 'build receipt' in fields['aethermind.url']['status']
    assert fields['aethermind_endpoints']['records'] == []
