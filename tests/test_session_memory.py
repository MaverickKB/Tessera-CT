"""Memory isolation through the persistent-session owner."""

import http.client
import json
import threading

from fake_backend import FakeBackend
from tessera.config import Config
from tessera.server import create_server
from tessera.sessions import SessionManager


def test_managed_sessions_keep_logs_frontiers_workers_and_restarts_isolated(tmp_path):
    config = Config(home=tmp_path, cwd=tmp_path)
    config.jev.enabled = False
    config.view.node_bytes = 48
    config.view.high_bytes = 1024
    config.view.low_bytes = 512

    def backend_for(slug, backend):
        return FakeBackend([f'{slug} reply with remembered details ' * 3] * 40)

    with SessionManager(config, backend_factory=backend_for) as manager:
        manager.create('alpha')
        manager.create('beta')
        for i in range(16):
            manager.submit('alpha', f'alpha request {i}: ' * 8)
            manager.submit('beta', f'beta request {i}: ' * 8)
        assert manager.wait('alpha', timeout=10)
        assert manager.wait('beta', timeout=10)
        alpha = manager.get('alpha')
        beta = manager.get('beta')
        assert alpha.tree.root == tmp_path / 'sessions' / 'alpha'
        assert beta.tree.root == tmp_path / 'sessions' / 'beta'
        assert alpha.compactor.wait_until_summarized(alpha.log.count() - 1, 10)
        assert beta.compactor.wait_until_summarized(beta.log.count() - 1, 10)
        assert alpha.tree.ready_leaves() is not beta.tree.ready_leaves()
        assert alpha.tree.ready_merges() is not beta.tree.ready_merges()
        beta_rows = beta.view.lines()
        beta_render = beta.view.render()
        beta_saved = beta.view.path.read_bytes()
        for i in range(16, 32):
            manager.submit('alpha', f'alpha request {i}: ' * 8)
        assert manager.wait('alpha', timeout=10)
        assert alpha.compactor.wait_until_summarized(alpha.log.count() - 1, 10)
        assert alpha.log.count() == 64
        assert beta.log.count() == 32
        assert beta.view.render() == beta_render
        assert beta.view.lines() == beta_rows
        assert beta.view.path.read_bytes() == beta_saved
        for row in alpha.view.lines():
            assert 'beta' not in row['text']
        for row in beta.view.lines():
            assert 'alpha' not in row['text']
        assert len(alpha.view.render().encode()) <= 1024
        assert len(beta.view.render().encode()) <= 1024
        assert alpha.view.zoom(0, 1).startswith('alpha request 0')
        assert beta.view.zoom(0, 1).startswith('beta request 0')
        expected_alpha = alpha.view.render()
    with SessionManager(config, backend_factory=backend_for) as manager:
        alpha = manager.get('alpha')
        beta = manager.get('beta')
        assert alpha.log.count() == 64
        assert beta.log.count() == 32
        assert alpha.view.render() == expected_alpha
        assert beta.view.render() == beta_render
        assert beta.view.lines() == beta_rows


def test_http_view_uses_one_snapshot_for_lines_and_byte_total(tmp_path, monkeypatch):
    config = Config(home=tmp_path, cwd=tmp_path)
    config.server.port = 0
    config.jev.enabled = False
    server = create_server(config, backend_factory=lambda slug, name: FakeBackend([]))
    server.manager.create('snapshot')
    session = server.manager.get('snapshot')
    session.log.append('user', 'first')
    original_lines = session.view.lines

    def append_after_snapshot():
        rows = original_lines()
        # Force a legal interleaving with the session worker: a new message
        # arrives after rows are captured but before the response is built.
        session.log.append('note', 'arrived after the snapshot')
        return rows

    monkeypatch.setattr(session.view, 'lines', append_after_snapshot)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = http.client.HTTPConnection(*server.server_address, timeout=5)
    try:
        connection.request('GET', '/api/sessions/snapshot/view')
        response = connection.getresponse()
        assert response.status == 200
        payload = json.loads(response.read())
        assert len(payload['lines']) == 1
        assert payload['lines'][0]['text'] == 'first'
        assert payload['bytes'] == sum(row['bytes'] for row in payload['lines']) + 14
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join()
