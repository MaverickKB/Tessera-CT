"""Work reports retain evidence and allow the agent to manage subject groupings."""
import base64
import pytest

from test_server import running_server, call


def enable_board(server):
    server.manager.config.board.enabled = True
    server.manager.board.enabled = True
    return server.manager.create('Report owner')['slug']


def test_report_evidence_roundtrip_and_invalid_reference_never_creates_card(tmp_path):
    with running_server(tmp_path) as server:
        slug = enable_board(server)
        manager = server.manager
        image = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aZ1kAAAAASUVORK5CYII=')
        stored = manager.add_attachment(slug, image, 'symptom.png', 'image/png')
        evidence = {'session_slug': slug, 'id': stored['id'], 'name': stored['name'],
                    'media_type': stored['media_type'], 'size': stored['bytes']}
        code, reply = call(server, 'POST', '/api/board/cards', {
            'title': 'The control jumps', 'note': 'What happened:\nIt moves on hover.', 'evidence': [evidence]})
        assert code == 200
        card = next(item for item in reply['view']['cards'] if item['card_id'] == reply['card_id'])
        assert card['evidence'] == [evidence] and card['artifact_count'] == 1
        assert card['project'] == '' and card['owner'] == ''
        passive = call(server, 'GET', '/api/board')[1]['cards'][0]
        assert passive['artifact_count'] == 1 and 'evidence' not in passive
        before = manager.board.path.read_bytes()
        code, _ = call(server, 'POST', '/api/board/cards', {
            'title': 'Forged size', 'evidence': [{**evidence, 'size': stored['bytes'] + 1}]})
        assert code == 409 and manager.board.path.read_bytes() == before
        tools = manager.get(slug).tools
        resolved = tools.execute('board_read_evidence', {'card_id': reply['card_id'], 'evidence_id': stored['id']})
        assert resolved['inspect_with'] == 'read_image'
        from pathlib import Path
        assert Path(resolved['path']).read_bytes() == image
        with pytest.raises(ValueError):
            tools.execute('board_read_evidence', {'card_id': reply['card_id'], 'evidence_id': 'f' * 32})


def test_subject_classification_and_agent_filing_preserve_workspace_identity(tmp_path):
    with running_server(tmp_path) as server:
        slug = enable_board(server)
        manager = server.manager
        tools = manager.get(slug).tools
        group = tools.execute('board_create_project', {'title': 'Refresh behavior', 'kind': 'subject'})
        pid = group['project_id']
        assert group['kind'] == 'subject'
        manager.create('Different worker')
        card = manager.board.create('Jitter in chat', slug, 0, 0, owner='different-worker')
        tools.execute('board_file_card', {'card_id': card['id'], 'project': pid})
        assert manager.board.get(card['id'])['project'] == pid
        assert manager.board.get(card['id'])['owner'] == 'different-worker'
        tools.execute('board_update_project', {'project_id': pid, 'kind': 'planning'})
        assert manager.board.get_project(pid)['kind'] == 'planning'
        assert manager.board.get(card['id'])['project'] == pid
        assert 'Tessera files cards' in manager.board_brief()['filing_guidance']
        legacy = manager.board.create_project('Existing plan', slug)
        assert manager.board.get_project(legacy['id'])['kind'] == 'planning'
        code, reply = call(server, 'POST', '/api/board/projects', {'title': 'Voice recovery', 'kind': 'subject'})
        assert code == 200
        project = next(item for item in reply['view']['projects'] if item['id'] == reply['project_id'])
        assert project['kind'] == 'subject' and project['steps'] == []
        assert call(server, 'POST', '/api/board/projects', {'title': 'Invalid', 'kind': 'unknown'})[0] == 409
