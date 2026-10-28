"""No-tree reads are normal states; raw diagnostics never authorize a builder."""

from unittest.mock import Mock

from integration_helpers import FakeMcpServer
from tessera.aethermind import AetherMindClient, tree_read_display
from tessera.capture import Capture


NO_TREE = 'No summary tree for this project yet; use read_layers or prepare_inheritance.'


def test_display_removes_remote_build_instruction_and_private_path():
    raw = {'success': False, 'error': 'no summary tree at /private-placeholder/tree.aem; run build_tree first',
           'error_code': 'INVALID_ARGUMENT'}
    result = tree_read_display(raw)
    assert result == {'state': 'no_tree', 'message': NO_TREE}
    assert 'build_tree' in raw['error']  # The diagnostic original is preserved.
    assert tree_read_display({'success': False, 'error': NO_TREE}) == result
    assert tree_read_display('No summary tree for this project yet.') == result
    successful = {'success': True, 'view': 'A source record mentions no summary tree.'}
    assert tree_read_display(successful) is successful
    unavailable = {'error': 'memory service unavailable'}
    assert tree_read_display(unavailable) is unavailable


def test_native_mapping_records_passive_per_project_observations_without_writes(monkeypatch):
    missing = {'success': False, 'error': 'No summary tree; run build_tree first'}
    spawn = Mock(side_effect=AssertionError('No builder or subprocess permitted'))
    monkeypatch.setattr('subprocess.Popen', spawn)
    with FakeMcpServer(dialect='path', tool_results={'view': missing, 'zoom': {'success': False, 'error': 'No node'}}) as server:
        client = AetherMindClient(endpoints=[server.url], timeout=1,
                                  read_only_projects=['/personal-placeholder'])
        assert client.tree_status_snapshot('/personal-placeholder') == (None, None)
        assert server.requests == []
        assert client.list_projects('/personal-placeholder')['success'] is True
        assert client.tree_view('/personal-placeholder') == missing
        stamp, state = client.tree_status_snapshot('/personal-placeholder')
        assert stamp is not None
        assert state == {'present': False, 'view_available': False, 'zoom_available': False}
        state['present'] = True
        assert client.tree_status_snapshot('/personal-placeholder')[1]['present'] is False
        assert client.tree_status_snapshot('/other-placeholder') == (None, None)
        assert client.tree_zoom('/personal-placeholder', 0, 0)['error'] == 'No node'
        assert client.tree_status_snapshot('/personal-placeholder')[1]['present'] is False
        assert client.status()['endpoints'][0]['healthy'] is True
        calls = [q['params'] for q, _ in server.requests if q['method'] == 'tools/call']
        # The adapter-v1 probe (list tools + capabilities) precedes routed calls.
        assert [q['name'] for q in calls] == ['capabilities', 'status', 'view', 'zoom']
        assert all(q['arguments'].get('project_root') == '/personal-placeholder'
                   for q in calls if q['name'] != 'capabilities')
        before = len(server.requests)
        client.tree_status_snapshot('/personal-placeholder')
        assert len(server.requests) == before
        assert server.native_write_calls == 0
    spawn.assert_not_called()


def test_later_real_navigation_clears_missing_tree_and_unknown_results_prove_nothing():
    with FakeMcpServer(dialect='path', tool_results={'view': {'success': False, 'error': NO_TREE}}) as server:
        client = AetherMindClient(endpoints=[server.url], timeout=1)
        client.tree_view('/personal-placeholder')
        server.tool_results['zoom'] = {'success': True, 'text': 'Synthetic node'}
        client.tree_zoom('/personal-placeholder', 0, 0)
        assert client.tree_status_snapshot('/personal-placeholder')[1] == {
            'present': True, 'view_available': None, 'zoom_available': True}
        server.tool_results['view'] = {}
        client.tree_view('/unknown-placeholder')
        assert client.tree_status_snapshot('/unknown-placeholder')[1] == {
            'present': None, 'view_available': None, 'zoom_available': None}
        server.tool_results['zoom'] = 'No node at the requested coordinates'
        client.tree_zoom('/unknown-placeholder', 0, 0)
        assert client.tree_status_snapshot('/unknown-placeholder')[1] == {
            'present': None, 'view_available': None, 'zoom_available': False}
        server.tool_results['view'] = {'success': True, 'view': 'Synthetic overview'}
        client.tree_view('/personal-placeholder')
        assert client.tree_status_snapshot('/personal-placeholder')[1]['view_available'] is True


def test_inheritance_display_never_forwards_server_build_cta(tmp_path):
    with FakeMcpServer(dialect='path', tool_results={'view': {'success': False, 'error': 'No summary tree; run build_tree first'}}) as server:
        client = AetherMindClient(endpoints=[server.url], timeout=1)
        capture = Capture(client, tmp_path, '/project-placeholder', '/personal-placeholder', 'tester')
        rendered = capture.inheritance_block()
        assert NO_TREE in rendered
        assert 'build_tree' not in rendered
        assert server.native_write_calls == 0


def test_session_echo_treats_no_tree_as_normal_read_state(tmp_path):
    import json
    from fake_backend import FakeBackend
    from tessera.config import Config
    from tessera.loop import Session
    from types import SimpleNamespace

    config = Config(home=tmp_path, cwd=tmp_path)
    with Session(config, backend=FakeBackend([])) as session:
        session.capture.client = SimpleNamespace(tree_zoom=lambda *args: {
            'success': False, 'error': 'No summary tree; run build_tree first'})
        try:
            output, record, _ = session._call_tool({'id': 'read-placeholder', 'name': 'memory_recall',
                                                   'arguments': {'level': 0, 'index': 0}})
            assert json.loads(output) == {'state': 'no_tree', 'message': NO_TREE}
            assert record['success'] is True
            assert 'build_tree' not in session.log.get(session.log.count() - 1).text
        finally:
            session.capture.client = None


def test_manager_projects_only_passive_tree_observations_for_its_configured_project(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from fake_backend import FakeBackend
    from tessera.config import Config
    from tessera.credentials import CredentialResolver
    from tessera.sessions import SessionManager

    with FakeMcpServer(dialect='path', tool_results={'view': {'success': False, 'error': NO_TREE}}) as server:
        config = Config(home=tmp_path, cwd=tmp_path)
        config.aethermind.endpoints = [server.url]
        config.aethermind.project = '/project-placeholder'
        config.aethermind.personal_project = '/personal-placeholder'
        client = AetherMindClient(endpoints=[server.url], timeout=1)
        client.tree_view(config.aethermind.personal_project)
        no_io = Mock(side_effect=AssertionError('Status must not probe or resolve credentials'))
        monkeypatch.setattr(client.mcp, '_post', no_io)
        monkeypatch.setattr(CredentialResolver, 'resolve', no_io)
        manager = SessionManager(config)
        manager.create('alpha')
        fake = FakeBackend([])
        # status() now reports delegated agents from the live session, so the stub has
        # to carry the same shape. An empty registry is what this test means: no children.
        session = SimpleNamespace(backend=fake, fast_backend=fake,
                                  agents=SimpleNamespace(active_details=lambda: []),
                                  capture=SimpleNamespace(client=client, _inheritance=None))
        manager._runtimes['alpha'] = SimpleNamespace(session=session, inheritance='Synthetic inheritance',
                                                    active=False, pending=[], failure=None,
                                                    operator_stopped=False)
        try:
            report = manager.status()['aethermind']
            assert report['configured'] is True
            assert report['reachable'] is True  # A no-tree read is a healthy connection.
            assert report['tree_status'] == {'present': False, 'view_available': False, 'zoom_available': False}
            config.aethermind.personal_project = '/other-placeholder'
            assert 'tree_status' not in manager.status()['aethermind']
            config.aethermind.endpoints = ['https://memory.example.com/other']
            assert manager.status()['aethermind']['reachable'] is None
            assert server.url not in str(report) and '/personal-placeholder' not in str(report)
            no_io.assert_not_called()
        finally:
            manager._runtimes.clear()
            manager.close()
