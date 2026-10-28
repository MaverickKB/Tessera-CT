from fake_backend import FakeBackend
from tessera.server import create_server
from test_loop import session_config
from test_server import call
import threading


class MemorySource:
    def __init__(self, missing_tree=False):
        self.calls = []
        self.missing_tree = missing_tree

    def tree_view(self, project, budget):
        self.calls.append(('view', project, budget))
        if self.missing_tree:
            return {'error': 'No summary tree for this project yet'}
        return {'view': 'A real memory overview', 'success': True}

    def tree_zoom(self, project, level, index):
        self.calls.append(('zoom', project, level, index))
        return {'node': 'Summary', 'layers': [{'id': 'original', 'body': 'Full original'}]}

    def read_layers(self, project, last_n):
        self.calls.append(('layers', project, last_n))
        return {'layers': [{'id': '1', 'type': 'decision', 'body': 'An original record'}]}


def test_memory_inspection_reads_the_configured_store_and_keeps_original_records(tmp_path):
    server = create_server(session_config(tmp_path), backend_factory=lambda *args: FakeBackend([]))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        slug = server.manager.create('Memory inspection')['slug']
        session = server.manager.get(slug)
        source = MemorySource()
        session.capture.client = source
        session.capture.project_id = 'session-store'
        session.capture.personal_project = 'personal-store'
        path = '/api/sessions/' + slug + '/memory'
        status, result = call(server, 'GET', path)
        assert status == 200
        assert result['tree']['view'] == 'A real memory overview'
        assert source.calls == [('view', 'session-store', 4000)]
        source.missing_tree = True
        status, result = call(server, 'GET', path + '?scope=personal')
        assert status == 200
        assert result['tree']['state'] == 'no_tree'
        assert result['tree']['recall']['layers'][0]['body'] == 'An original record'
        assert source.calls[-2:] == [('view', 'personal-store', 4000), ('layers', 'personal-store', session.capture.recall_limit)]
        status, result = call(server, 'GET', path + '?scope=personal&level=0&index=2')
        assert status == 200
        assert result['tree']['layers'][0]['body'] == 'Full original'
        assert source.calls[-1] == ('zoom', 'personal-store', 0, 2)
        assert call(server, 'GET', path + '?level=1')[0] == 400
        assert call(server, 'GET', path + '?level=-1&index=0')[0] == 400
        assert call(server, 'GET', path + '?scope=other')[0] == 400
        assert list(session.log.iter()) == []
        assert session.capture.debt() == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
