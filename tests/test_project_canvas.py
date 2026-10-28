"""Canvas pieces remain shared project state rather than rendered chat decorations."""
import pytest
from tessera.board import BoardStore


def store(tmp_path):
    return BoardStore(tmp_path / 'board.jsonl', enabled=True)


def test_canvas_roundtrip_links_stacks_and_targeted_concurrent_edits(tmp_path):
    board = store(tmp_path)
    project = board.create_project('Planning', 'one')
    pid = project['id']
    board.update_canvas(pid, {'nodes': [{'id':'a','title':'Observe'}, {'id':'b','title':'Decide','x':350}],
                              'links':[{'id':'ab','from':'a','to':'b','label':'then'}]})
    other = store(tmp_path)
    other.update_canvas(pid, {'nodes':[{'id':'b','body':'Agent contribution'}]})
    board.update_canvas(pid, {'nodes':[{'id':'stack','kind':'group'}, {'id':'a','x':20,'parent':'stack'},
                                      {'id':'b','parent':'stack'}]})
    graph = store(tmp_path).get_project(pid)['canvas']
    assert graph['version'] == 3
    nodes = {n['id']: n for n in graph['nodes']}
    assert nodes['b']['body'] == 'Agent contribution'
    assert nodes['a']['parent'] == 'stack' and nodes['a']['x'] == 20
    assert graph['links'][0]['label'] == 'then'
    board.update_canvas(pid, {'remove_nodes':['stack']})
    assert all(not n.get('parent') for n in board.get_project(pid)['canvas']['nodes'])
    board.update_canvas(pid, {'remove_nodes':['a']})
    assert board.get_project(pid)['canvas']['links'] == []


def test_invalid_graph_and_stale_content_never_append(tmp_path):
    board = store(tmp_path)
    pid = board.create_project('Planning', 'one')['id']
    board.update_canvas(pid, {'nodes':[{'id':'g','kind':'group'}, {'id':'a'}]})
    before = board.canvas_path.read_bytes()
    for changes in [{'nodes':[{'id':'g','parent':'g'}]},
                    {'nodes':[{'id':'a','x':float('nan')}]},
                    {'links':[{'id':'bad','from':'a','to':'absent'}]},
                    {'nodes':[{'id':'a','script':'alert(1)'}]},
                    {'nodes':[{'id':'a','url':'javascript:alert(1)'}]},
                    {'expected_version':0,'nodes':[{'id':'a','body':'stale'}]}]:
        with pytest.raises(ValueError): board.update_canvas(pid, changes)
        assert board.canvas_path.read_bytes() == before


def test_task_and_step_pieces_reference_real_work(tmp_path):
    board = store(tmp_path)
    pid = board.create_project('Planning', 'one')['id']
    task = board.create('Investigate', 'one', 0, 0, project=pid)
    step = board.add_step(pid, 'Reproduce')['steps'][0]
    board.update_canvas(pid, {'nodes':[{'id':'task','kind':'task','card_id':task['id']},
                                      {'id':'step','kind':'step','step_id':step['id']}]})
    with pytest.raises(ValueError):
        board.update_canvas(pid, {'nodes':[{'id':'madeup','kind':'task','card_id':'absent'}]})
    board.set_steps(pid, [])
    board.update_canvas(pid, {'nodes':[{'id':'task','x':100}]})
    assert board.get_project(pid)['canvas']['nodes'][1]['step_id'] == step['id']


def test_project_contributions_append_to_latest_state(tmp_path):
    board = store(tmp_path)
    pid = board.create_project('Planning', 'one', note='Initial intent')['id']
    board.append_project_note(pid, 'Human observation')
    other = store(tmp_path)
    other.append_project_note(pid, 'Agent finding')
    board.add_step(pid, 'First step')
    other.add_step(pid, 'Second step')
    project = board.get_project(pid)
    assert all(text in project['note'] for text in ['Initial intent','Human observation','Agent finding'])
    assert [step['text'] for step in project['steps']] == ['First step','Second step']


def test_canvas_deltas_preserve_work_revision_and_recover_partial_tail(tmp_path):
    import json
    from tessera.board_status import board_status
    from tessera.config import Config
    config = Config(home=tmp_path, cwd=tmp_path)
    config.board.enabled = True
    config.board.journal_file = 'board.jsonl'
    board = store(tmp_path)
    first = board.create_project('First', 'one')['id']
    second = board.create_project('Second', 'one')['id']
    revision = board_status(config, ['one'])['revision']
    project_bytes = board.project_path.read_bytes()
    board.update_canvas(first, {'nodes': [{'id': 'piece', 'body': 'Keep this content'}]})
    board.update_canvas(second, {'nodes': [{'id': 'other'}]})
    with board.canvas_path.open('ab') as stream:
        stream.write(b'{"sequence":3')
    board.update_canvas(first, {'nodes': [{'id': 'piece', 'x': 120}]})
    assert board.project_path.read_bytes() == project_bytes
    assert board_status(config, ['one'])['revision'] == revision
    assert list(tmp_path.glob('board.canvas.jsonl.incomplete-*'))
    records = [json.loads(line) for line in board.canvas_path.read_text().splitlines()]
    assert records[-1]['changes'] == {'nodes': [{'id': 'piece', 'x': 120}]}
    reopened = store(tmp_path)
    assert reopened.get_project(first)['canvas']['version'] == 2
    assert reopened.get_project(first)['canvas']['nodes'][0]['body'] == 'Keep this content'
    assert reopened.get_project(second)['canvas']['version'] == 1
    board.add_step(first, 'Real work change')
    assert board_status(config, ['one'])['revision'] != revision


def test_project_http_contributions_and_canvas_share_the_board_owner(tmp_path):
    from test_server import running_server, call
    with running_server(tmp_path) as server:
        manager = server.manager
        manager.config.board.enabled = True
        manager.board.enabled = True
        slug = manager.create('Planning')['slug']
        pid = manager.board.create_project('Plan', slug)['id']
        route = '/api/board/projects/' + pid
        code, view = call(server, 'POST', route + '/note', {'text': 'Shared decision'})
        assert code == 200 and view['projects'][0]['note'] == 'Shared decision'
        code, view = call(server, 'POST', route + '/add-step', {'text': 'Verify the result'})
        assert code == 200 and view['projects'][0]['steps'][0]['text'] == 'Verify the result'
        code, graph = call(server, 'POST', route + '/canvas', {'nodes': [{'id': 'question', 'title': 'Question'}]})
        assert code == 200 and graph['canvas']['nodes'][0]['title'] == 'Question'
        assert call(server, 'GET', route + '/canvas')[1] == graph


def test_agent_tool_loop_creates_visual_graph_with_project_context(tmp_path):
    import json
    from fake_backend import FakeBackend
    from tessera.config import Config
    from tessera.sessions import SessionManager
    config = Config(home=tmp_path, cwd=tmp_path)
    config.board.enabled = True
    config.jev.enabled = False
    seen = {}
    def draw(messages, tools):
        content = json.dumps(messages)
        assert seen['project'] in content and 'Project workspace:' in content
        assert any(tool['function']['name'] == 'board_project_canvas' for tool in tools)
        return {'tool_calls':[{'id':'draw','name':'board_project_canvas','arguments':{
            'project_id':seen['project'],'changes':{'nodes':[{'id':'question','title':'Question'},
                                                          {'id':'evidence','title':'Evidence','x':340}],
                'links':[{'id':'reason','from':'question','to':'evidence','label':'investigate'}]}}}]}
    backend = FakeBackend([draw,'The plan is on the canvas.'])
    with SessionManager(config,backend_factory=lambda *args:backend) as manager:
        slug = manager.create('Project dialogue')['slug']
        seen['project'] = manager.board.create_project('Explore',slug)['id']
        manager.submit(slug,'Draw the plan',project_id=seen['project'],selected=[])
        assert manager.wait(slug,10)
        report = manager.board_project_canvas(seen['project'])
        assert [node['title'] for node in report['canvas']['nodes']] == ['Question','Evidence']
        assert report['canvas']['links'][0]['label'] == 'investigate'
        users = [row for row in manager.get(slug).log.iter() if row.kind == 'user']
        assert users[0].text == 'Draw the plan'
        assert users[0].meta['project_id'] == seen['project']


def test_agent_imports_real_image_file_into_shared_artifact_library(tmp_path):
    from fake_backend import FakeBackend
    from tessera.config import Config
    from tessera.sessions import SessionManager
    import base64
    image = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aZ1kAAAAASUVORK5CYII=')
    (tmp_path/'visual.png').write_bytes(image)
    config = Config(home=tmp_path,cwd=tmp_path)
    config.board.enabled = True
    config.jev.enabled = False
    config.tools.approval = 'yolo'
    seen = {}
    def attach(messages,tools):
        return {'tool_calls':[{'id':'image','name':'board_project_artifact','arguments':{
            'project_id':seen['project'],'path':'visual.png','title':'Visual explanation'}}]}
    with SessionManager(config,backend_factory=lambda *args:FakeBackend([attach,'Added the visual.'])) as manager:
        slug=manager.create('Visual dialogue')['slug']
        seen['project']=manager.board.create_project('Visual planning',slug)['id']
        manager.submit(slug,'Show the visual',project_id=seen['project'])
        assert manager.wait(slug,10)
        node=manager.board.get_project(seen['project'])['canvas']['nodes'][0]
        assert node['kind']=='image' and node['title']=='Visual explanation'
        path,media=manager.attachment_file(node['session_slug'],node['attachment_id'])
        assert media=='image/png' and path.read_bytes()==image
        assert path == (tmp_path / 'visual.png').resolve()
        assert list((manager.path(slug) / 'attachments').glob('*.ref'))
        manager.board.update_canvas(seen['project'],{'nodes':[{'id':node['id'],'placed':False}]})
        assert manager.board.get_project(seen['project'])['canvas']['nodes'][0]['placed'] is False
        assert path.exists()


def test_late_project_steer_retains_selected_pieces_in_recovered_turn(tmp_path):
    import json
    import threading
    from fake_backend import FakeBackend
    from tessera.config import Config
    from tessera.sessions import SessionManager
    started, release, received = threading.Event(), threading.Event(), threading.Event()
    config = Config(home=tmp_path, cwd=tmp_path)
    config.board.enabled = True
    seen = {}
    def first(messages, tools):
        started.set()
        assert release.wait(5)
        return 'First response finished'
    def recovery(messages, tools):
        content = json.dumps(messages)
        assert seen['project'] in content and 'Selected question' in content
        received.set()
        return 'Recovered guidance received'
    backend = FakeBackend([first, recovery])
    with SessionManager(config, backend_factory=lambda *args: backend) as manager:
        slug = manager.create('Planning steer')['slug']
        pid = manager.board.create_project('Shared plan', slug)['id']
        seen['project'] = pid
        manager.board.update_canvas(pid, {'nodes': [{'id': 'question', 'title': 'Selected question'}]})
        manager.submit(slug, 'Start the discussion')
        assert started.wait(5)
        result = manager.steer(slug, 'Consider this piece', project_id=pid, selected=['question'])
        assert result['mode'] == 'steer'
        release.set()
        assert received.wait(5)
        assert manager.wait(slug, 5)
        users = [row for row in manager.get(slug).log.iter() if row.kind == 'user']
        assert users[-1].text == 'Consider this piece'
        assert users[-1].meta['project_id'] == pid
        assert users[-1].meta['selected'] == ['question']
