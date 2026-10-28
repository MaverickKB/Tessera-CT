"""Enable, disable, and skill directories are saved and change what the loop can call."""

from pathlib import Path

import pytest

from tessera.config import McpServerConfig, load_config
from tessera.settings_model import tools_view

from test_skills_tools_view import HOME, _write_skill, call, serve, stop


def _offered(session):
    return {item['function']['name'] for item in session._route_schema({'route': 'tools'})}


def _session(server):
    created = call(server, 'POST', '/api/sessions', {'title': 'Control'})
    assert created[0] == 201, created[1]
    return created[1]['slug'], server.manager.get(created[1]['slug'])


def test_disabling_a_tool_persists_and_the_loaded_session_cannot_call_it(tmp_path):
    server, thread = serve(tmp_path)
    try:
        slug, session = _session(server)
        assert 'shell' in _offered(session)
        code, body = call(server, 'POST', '/api/tools', {'name': 'shell', 'enabled': False})
        assert code == 200, body
        row = next(item for item in body['tools'] if item['name'] == 'shell')
        assert row['enabled'] is False and row['outcome'] == 'disabled'
        assert 'shell' in load_config(tmp_path).tools.disabled
        assert 'next turn' in body['effect']
        assert 'shell' not in _offered(session)
        output, record, _contexts = session._call_tool(
            {'id': 'call-1', 'name': 'shell', 'arguments': {'command': 'echo should-not-run'}},
            permitted=True)
        assert record['success'] is False
        assert 'disabled' in output
        assert 'should-not-run' not in output
        code, body = call(server, 'POST', '/api/tools', {'name': 'shell', 'enabled': True})
        assert code == 200, body
        assert 'shell' not in load_config(tmp_path).tools.disabled
        assert 'shell' in _offered(session)
        assert server.manager.get(slug) is session
    finally:
        server.manager.close()
        stop(server, thread)


def test_a_tool_disabled_before_a_session_starts_is_not_offered(tmp_path):
    server, thread = serve(tmp_path)
    try:
        code, body = call(server, 'POST', '/api/tools', {'name': 'shell', 'enabled': False})
        assert code == 200, body
        assert body['callable'] is False
        assert 'not callable' in body['gap'].lower() or 'until a session is loaded' in body['gap']
        _slug, session = _session(server)
        assert 'shell' not in _offered(session)
        with pytest.raises(ValueError, match='disabled'):
            session.tools.execute('shell', {'command': 'echo should-not-run'})
    finally:
        server.manager.close()
        stop(server, thread)


def test_an_unknown_tool_is_rejected_and_nothing_is_written(tmp_path):
    server, thread = serve(tmp_path)
    try:
        before = (tmp_path / 'config.toml').read_text()
        code, body = call(server, 'POST', '/api/tools', {'name': 'not-a-real-tool', 'enabled': False})
        assert code == 400, body
        assert 'Unknown tool' in body['error']
        assert (tmp_path / 'config.toml').read_text() == before
    finally:
        stop(server, thread)


def test_disabling_a_skill_persists_and_the_shelf_stops_offering_it(tmp_path):
    _write_skill(tmp_path, 'desk-look', 'Look at the desk')
    _write_skill(tmp_path, 'other-skill', 'Something else')
    server, thread = serve(tmp_path)
    try:
        _slug, session = _session(server)
        found = session.tools.execute('skill_search', {'query': 'desk'})
        assert 'desk-look' in found
        code, body = call(server, 'POST', '/api/skills', {'name': 'desk-look', 'enabled': False})
        assert code == 200, body
        row = next(item for item in body['skills'] if item['name'] == 'desk-look')
        assert row['enabled'] is False and row['offered'] is False
        assert 'desk-look' in load_config(tmp_path).skills.disabled
        missed = session.tools.execute('skill_search', {'query': 'desk'})
        assert 'desk-look' not in missed
        assert 'other-skill' in session.tools.execute('skill_search', {'query': 'else'})
        output, record, _contexts = session._call_tool(
            {'id': 'load-1', 'name': 'skill_load', 'arguments': {'name': 'desk-look'}},
            permitted=True)
        assert record['success'] is False
        assert 'disabled' in output.lower()
        code, body = call(server, 'POST', '/api/skills', {'name': 'desk-look', 'enabled': True})
        assert code == 200, body
        assert 'desk-look' not in load_config(tmp_path).skills.disabled
        loaded = session.tools.execute('skill_load', {'name': 'desk-look'})
        assert 'Do the thing' in loaded
    finally:
        server.manager.close()
        stop(server, thread)


def test_adding_and_removing_a_skill_directory_changes_what_load_can_read(tmp_path):
    extra = tmp_path / 'extra-skills'
    _write_skill_at(extra, 'from-extra', 'Loaded from the added directory')
    server, thread = serve(tmp_path)
    try:
        _slug, session = _session(server)
        code, body = call(server, 'POST', '/api/skills', {'add_dir': 'extra-skills'})
        assert code == 400, body
        code, body = call(server, 'POST', '/api/skills', {'add_dir': str(extra)})
        assert code == 200, body
        assert any(row['source'] == 'added' and row['error'] == '' for row in body['directories'])
        assert 'from-extra' in {row['name'] for row in body['skills']}
        assert str(extra) in load_config(tmp_path).skills.extra_dirs
        loaded = session.tools.execute('skill_load', {'name': 'from-extra'})
        assert 'Loaded from the added directory' in loaded
        code, body = call(server, 'POST', '/api/skills', {'remove_dir': str(tmp_path / 'skills')})
        assert code == 400, body
        assert 'user shelf' in body['error']
        code, body = call(server, 'POST', '/api/skills', {'remove_dir': str(extra)})
        assert code == 200, body
        assert 'from-extra' not in {row['name'] for row in body['skills']}
        assert load_config(tmp_path).skills.extra_dirs == []
        output, record, _contexts = session._call_tool(
            {'id': 'load-2', 'name': 'skill_load', 'arguments': {'name': 'from-extra'}},
            permitted=True)
        assert record['success'] is False
        assert 'Unknown skill' in output
    finally:
        server.manager.close()
        stop(server, thread)


def test_turning_the_shelf_off_is_saved_and_the_loaded_shelf_stops_offering(tmp_path):
    _write_skill(tmp_path, 'desk-look', 'Look at the desk')
    server, thread = serve(tmp_path)
    try:
        _slug, session = _session(server)
        assert 'desk-look' in session.tools.execute('skill_search', {'query': 'desk'})
        code, body = call(server, 'POST', '/api/skills', {'shelf_enabled': False})
        assert code == 200, body
        assert body['enabled'] is False
        assert load_config(tmp_path).skills.enabled is False
        assert 'shelf' in body['effect'].lower()
        missed = session.tools.execute('skill_search', {'query': 'desk'})
        assert 'desk-look' not in missed
        output, record, _contexts = session._call_tool(
            {'id': 'load-3', 'name': 'skill_load', 'arguments': {'name': 'desk-look'}},
            permitted=True)
        assert record['success'] is False
        assert 'off' in output.lower()
    finally:
        server.manager.close()
        stop(server, thread)


def test_turning_the_shelf_on_does_not_attach_tools_to_a_session_that_started_off(tmp_path):
    _write_skill(tmp_path, 'desk-look', 'Look at the desk')
    server, thread = serve(tmp_path, HOME.replace('enabled = true', 'enabled = false', 1))
    try:
        _slug, session = _session(server)
        assert session.shelf is None
        assert 'skill_load' not in _offered(session)
        code, body = call(server, 'POST', '/api/skills', {'shelf_enabled': True})
        assert code == 200, body
        assert body['enabled'] is True
        assert load_config(tmp_path).skills.enabled is True
        assert 'no skill tools' in body['effect']
        assert session.shelf is None
        assert 'skill_load' not in _offered(session)
        _later, started = _session(server)
        assert started.shelf is not None
        assert 'skill_load' in _offered(started)
        loaded = started.tools.execute('skill_load', {'name': 'desk-look'})
        assert 'Do the thing' in loaded
    finally:
        server.manager.close()
        stop(server, thread)


def test_yolo_refuses_a_tool_the_operator_turned_off_and_keeps_the_switch(tmp_path):
    server, thread = serve(tmp_path)
    try:
        _slug, session = _session(server)
        code, body = call(server, 'POST', '/api/tools', {'name': 'shell', 'enabled': False})
        assert code == 200, body
        assert 'shell' not in _offered(session)
        status = server.manager.set_yolo(True)
        assert status['mode'] == 'yolo'
        assert 'shell' in load_config(tmp_path).tools.disabled
        assert 'shell' not in _offered(session)
        output, record, _contexts = session._call_tool(
            {'id': 'call-yolo', 'name': 'shell', 'arguments': {'command': 'echo yolo-runs'}},
            permitted=True)
        assert record['success'] is False
        assert 'disabled' in output.lower()
        assert 'yolo-runs' not in output
        listed = call(server, 'GET', '/api/tools')[1]
        row = next(item for item in listed['tools'] if item['name'] == 'shell')
        assert row['enabled'] is False and row['outcome'] == 'disabled' and listed['yolo'] is True
        turned_on = call(server, 'POST', '/api/tools', {'name': 'shell', 'enabled': True})
        assert turned_on[0] == 200
        again = call(server, 'POST', '/api/tools', {'name': 'shell', 'enabled': False})[1]
        assert 'every mode' in again['effect']
        assert 'including Unleashed' in again['effect']
        assert 'does not stop' not in again['effect']
        assert 'shell' not in _offered(session)
    finally:
        server.manager.close()
        stop(server, thread)


def test_yolo_skips_approval_for_a_tool_that_is_on(tmp_path):
    server, thread = serve(tmp_path)
    try:
        _slug, session = _session(server)
        assert 'shell' not in (load_config(tmp_path).tools.disabled or [])
        blocked, record, _contexts = session._call_tool(
            {'id': 'call-safe', 'name': 'shell', 'arguments': {'command': 'echo should-ask'}},
            permitted=True)
        assert record['success'] is False
        assert 'Approval' in blocked
        status = server.manager.set_yolo(True)
        assert status['mode'] == 'yolo'
        output, record, _contexts = session._call_tool(
            {'id': 'call-on', 'name': 'shell', 'arguments': {'command': 'echo yolo-runs'}},
            permitted=True)
        assert record['success'] is True
        assert 'yolo-runs' in output
        assert 'Approval' not in output
    finally:
        server.manager.close()
        stop(server, thread)


def test_safe_allows_still_refuses_a_tool_the_operator_turned_off(tmp_path):
    text = HOME.replace('approval = "deny_risky"', 'approval = "never"', 1)
    server, thread = serve(tmp_path, text)
    try:
        _slug, session = _session(server)
        assert server.manager.approval_status()['mode'] == 'never'
        code, body = call(server, 'POST', '/api/tools', {'name': 'shell', 'enabled': False})
        assert code == 200, body
        assert body['tools']
        row = next(item for item in body['tools'] if item['name'] == 'shell')
        assert row['enabled'] is False and row['outcome'] == 'disabled'
        assert 'shell' not in _offered(session)
        output, record, _contexts = session._call_tool(
            {'id': 'call-never', 'name': 'shell', 'arguments': {'command': 'echo should-not-run'}},
            permitted=True)
        assert record['success'] is False
        assert 'disabled' in output
        assert 'should-not-run' not in output
    finally:
        server.manager.close()
        stop(server, thread)


def test_yolo_refuses_to_load_a_skill_the_operator_turned_off(tmp_path):
    _write_skill(tmp_path, 'desk-look', 'Look at the desk')
    server, thread = serve(tmp_path)
    try:
        _slug, session = _session(server)
        code, _body = call(server, 'POST', '/api/skills', {'name': 'desk-look', 'enabled': False})
        assert code == 200
        assert 'desk-look' not in session.tools.execute('skill_search', {'query': 'desk'})
        server.manager.set_yolo(True)
        with pytest.raises(ValueError, match='Skill disabled'):
            session.tools.execute('skill_load', {'name': 'desk-look'})
        assert 'desk-look' not in session.tools.execute('skill_search', {'query': 'desk'})
        assert 'desk-look' in load_config(tmp_path).skills.disabled
        listed = call(server, 'GET', '/api/skills')[1]
        row = next(item for item in listed['skills'] if item['name'] == 'desk-look')
        assert row['enabled'] is False and row['offered'] is False and listed['yolo'] is True
        assert 'does not stop' not in listed.get('note', '')
    finally:
        server.manager.close()
        stop(server, thread)


def test_yolo_keeps_skill_tools_off_when_the_shelf_started_off(tmp_path):
    _write_skill(tmp_path, 'desk-look', 'Look at the desk')
    server, thread = serve(tmp_path, HOME.replace('enabled = true', 'enabled = false', 1))
    try:
        _slug, session = _session(server)
        assert session.shelf is None
        assert 'skill_load' not in _offered(session)
        ceiling = set(session.tool_ceiling)
        server.manager.set_yolo(True)
        assert load_config(tmp_path).skills.enabled is False
        assert session.shelf is None
        assert 'skill_load' not in _offered(session)
        assert session.tool_ceiling == ceiling
        listed = call(server, 'GET', '/api/skills')[1]
        assert listed['yolo'] is True and listed['enabled'] is False
        assert 'does not offer' in listed['note']
        assert 'every mode' in listed['note']
        assert 'YOLO is on, so' not in listed['note']
    finally:
        server.manager.close()
        stop(server, thread)


def test_yolo_cannot_give_skill_write_to_a_restricted_session(tmp_path):
    """A session limited to read_file stays limited when YOLO turns on.

    Skill tools register at construction and then pass through the same allowlist
    and ceiling as every other tool. There is no later attach, and the ceiling
    is not widened. A user off switch stays off in YOLO as well.
    """
    from tessera.config import load_config
    from tessera.loop import Session
    from fake_backend import FakeBackend

    _write_skill(tmp_path, 'desk-look', 'Look at the desk')
    (tmp_path / 'config.toml').write_text(HOME)
    config = load_config(tmp_path)
    config.tools.approval = 'deny_risky'
    config.skills.disabled = ['desk-look']
    with Session(config, backend=FakeBackend([]), allowed_tools=['read_file'], tool_ceiling={'read_file'}) as session:
        assert session.tools.names() == ['read_file']
        assert session.tool_ceiling == {'read_file'}
        session.tools.approval = 'yolo'
        offered = {item['function']['name'] for item in session._route_schema({'route': 'tools'})}
        assert offered == {'read_file'}
        assert 'skill_write' not in session.tool_ceiling
        with pytest.raises(ValueError, match='Skill disabled'):
            session.shelf.find('desk-look')


def test_disabling_a_tool_removes_it_from_a_live_child(tmp_path):
    import threading

    from fake_backend import FakeBackend

    entered = threading.Event()
    release = threading.Event()

    def work(messages, tools):
        entered.set()
        assert release.wait(5)
        return 'Done'

    # Safe mode allows one request on this local model and the session holds it.
    # The hand-set 2 leaves a slot for the child this test starts.
    text = HOME.replace(
        'model = "scripted-local-fake"\n',
        'model = "scripted-local-fake"\nmax_concurrent_requests = 2\n',
    )
    server, thread = serve(tmp_path, text)
    try:
        _slug, session = _session(server)
        session.agent_backend_factory = lambda: FakeBackend([work])
        session.agents.spawn('worker', 'Hold the turn', ['read_file', 'shell'])
        assert entered.wait(5)
        child = session.agents._children['worker']
        assert child._config_owner is session._config_owner
        assert 'shell' in _offered(child)
        ceiling = set(child.tool_ceiling)
        code, body = call(server, 'POST', '/api/tools', {'name': 'shell', 'enabled': False})
        assert code == 200, body
        assert 'shell' not in _offered(child)
        with pytest.raises(ValueError, match='Tool disabled'):
            child.tools.execute('shell', {'command': 'echo should-not-run'})
        assert child.tool_ceiling == ceiling
        assert 'skill_write' not in child.tool_ceiling
        server.manager.set_yolo(True)
        assert child.tools.approval == 'yolo'
        assert 'shell' not in _offered(child)
        with pytest.raises(ValueError, match='Tool disabled'):
            child.tools.execute('shell', {'command': 'echo yolo-still-off'})
    finally:
        release.set()
        if 'session' in locals():
            session.agents.wait(timeout=5)
        server.manager.close()
        stop(server, thread)


def test_mcp_tools_come_from_the_catalog_and_a_failure_is_the_error(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    (home / 'config.toml').write_text(HOME)
    config = load_config(home)
    config.mcp_servers['desk'] = McpServerConfig(name='desk', url='http://127.0.0.1:9', session_tools=True)

    class Catalog:
        def __init__(self):
            self.rows = [{'name': 'ping', 'description': 'Ping the desk', 'inputSchema': {'type': 'object', 'properties': {}}}]
            self.error = ''

        def tools(self, server):
            assert server.name == 'desk'
            return None if self.error else self.rows

        def last_error(self, name):
            return self.error if name == 'desk' else ''

    catalog = Catalog()
    monkeypatch.setattr('tessera.mcp_servers.CATALOG', catalog)
    listed = tools_view(config, {'mode': 'ask'})
    assert listed['callable'] is False
    names = {row['name']: row for row in listed['tools']}
    assert 'desk__ping' in names
    assert names['desk__ping']['origin'] == 'mcp'
    assert names['desk__ping']['enabled'] is True
    assert listed['mcp_errors'] == []

    catalog.error = 'connection refused'
    catalog.rows = None
    failed = tools_view(config, {'mode': 'ask'})
    assert 'desk__ping' not in {row['name'] for row in failed['tools']}
    assert failed['mcp_errors'] == [{'server': 'desk', 'error': 'connection refused'}]


def _write_skill_at(root, name, description):
    directory = Path(root) / name
    directory.mkdir(parents=True)
    (directory / 'SKILL.md').write_text(
        f'---\nname: {name}\ndescription: {description}\n---\nDo the thing.\n', encoding='utf-8')
