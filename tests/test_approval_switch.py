import json

import pytest

from fake_backend import FakeBackend
from tessera.config import Config
from tessera.sessions import SessionManager
from tessera.tools import ToolRegistry, build_registry
from test_server import running_server, call


def config_for(root, approval='deny_risky'):
    config = Config(home=root, cwd=root)
    config.jev.enabled = False
    config.tools.approval = approval
    return config


def test_yolo_registry_runs_risky_tools_without_approval(tmp_path):
    registry = build_registry(tmp_path, approval='deny_risky')
    with pytest.raises(PermissionError):
        registry.execute('write_file', {'path': 'a.txt', 'text': 'x'})
    registry.approval = 'yolo'
    registry.execute('write_file', {'path': 'a.txt', 'text': 'x'})
    assert (tmp_path / 'a.txt').read_text() == 'x'
    with pytest.raises(ValueError):
        ToolRegistry('maybe')


def test_switch_applies_to_resident_sessions_and_persists(tmp_path):
    config = config_for(tmp_path)
    with SessionManager(config, backend_factory=lambda *a: FakeBackend(['ok', 'ok'])) as manager:
        assert manager.approval_status() == {'mode': 'deny_risky', 'yolo': False, 'configured': 'deny_risky',
                                             'safe_mode': 'deny_risky', 'switchable': True}
        slug = manager.create('Switch')['slug']
        manager.submit(slug, 'hi')
        assert manager.wait(slug, 5)
        session = manager.get(slug)
        assert manager.set_yolo(True)['mode'] == 'yolo'
        assert session.tools.approval == 'yolo'
        session.tools.execute('write_file', {'path': 'y.txt', 'text': 'y'})
        state = json.loads((tmp_path / 'state' / 'approval.json').read_text())
        assert state['mode'] == 'yolo'
    with SessionManager(config_for(tmp_path), backend_factory=lambda *a: FakeBackend([])) as restored:
        assert restored.approval_status()['mode'] == 'yolo'
        assert restored.set_yolo(False)['mode'] == 'deny_risky'
    with SessionManager(config_for(tmp_path), backend_factory=lambda *a: FakeBackend([])) as restored:
        assert restored.approval_status()['mode'] == 'deny_risky'


def test_switch_can_be_disabled_in_config_and_then_ignores_state(tmp_path):
    (tmp_path / 'state').mkdir()
    (tmp_path / 'state' / 'approval.json').write_text('{"mode": "yolo"}')
    config = config_for(tmp_path, 'ask')
    config.tools.yolo_switch = False
    with SessionManager(config, backend_factory=lambda *a: FakeBackend([])) as manager:
        assert manager.approval_status()['mode'] == 'ask'
        assert manager.approval_status()['switchable'] is False
        with pytest.raises(ValueError, match='disabled'):
            manager.set_yolo(True)


def test_http_approval_endpoint_and_status(tmp_path):
    with running_server(tmp_path) as server:
        status, body = call(server, 'GET', '/api/approval')
        assert status == 200 and body['mode'] == 'ask' and body['yolo'] is False
        status, body = call(server, 'POST', '/api/approval', {'yolo': True})
        assert status == 200 and body['yolo'] is True and body['mode'] == 'yolo'
        assert call(server, 'GET', '/api/status')[1]['approval']['yolo'] is True
        assert call(server, 'POST', '/api/approval', {'yolo': 'yes'})[0] == 400
        assert call(server, 'POST', '/api/approval', {'mode': 'yolo'})[0] == 400
        assert call(server, 'POST', '/api/approval', {'yolo': False})[1]['mode'] == 'ask'
        assert call(server, 'POST', '/api/approval', {'yolo': True},
                    {'Origin': 'https://evil.example'})[0] == 403


def test_registries_built_before_a_switch_enforce_the_new_mode(tmp_path):
    config = config_for(tmp_path)
    with SessionManager(config, backend_factory=lambda *a: FakeBackend([])) as manager:
        session = manager.get(manager.create('Child')['slug'])
        child = session.tools.restricted(['write_file'])
        with pytest.raises(PermissionError):
            child.execute('write_file', {'path': 'c.txt', 'text': 'c'})
        manager.set_yolo(True)
        child.execute('write_file', {'path': 'c.txt', 'text': 'c'})
        assert (tmp_path / 'c.txt').read_text() == 'c'
        manager.set_yolo(False)
        assert child.approval == session.tools.approval == manager.approval_status()['mode'] == 'deny_risky'


@pytest.mark.parametrize('configured, yolo, prompts', [
    ('deny_risky', True, False), ('deny_risky', False, True), ('ask', True, False), ('ask', False, True)])
def test_a_wake_turn_obeys_the_switch(tmp_path, configured, yolo, prompts):
    from test_autonomy import call, deliver, install_fake_clock, messages
    config = config_for(tmp_path, configured)
    config.autonomy.enabled = True
    config.autonomy.tick_seconds = 3600
    config.autonomy.min_wake_gap_seconds = 0
    acted = []
    backend = FakeBackend([call('mcp_act'), 'Done.'])
    with SessionManager(config, backend_factory=lambda *a: backend) as manager:
        clock = install_fake_clock(manager)
        slug = manager.create('Wake')['slug']
        manager.get(slug).tools.register('mcp_act', 'acts outside', {'type': 'object', 'properties': {}},
                                         lambda: acted.append('acted') or 'acted', risky=True)
        manager.autonomy.set_timer(slug, 'write the file', in_seconds=60)
        assert manager.set_yolo(yolo)['yolo'] is yolo
        clock.now += 61
        deliver(manager, slug)
        log = messages(manager, slug)
        assert any(m.meta.get('wake') for m in log if m.kind == 'user')
        approvals = [m for m in log if m.meta.get('approval')]
        assert bool(approvals) is prompts
        assert acted == ([] if prompts else ['acted'])
        expected = 'yolo' if yolo else configured
        assert manager.approval_status()['mode'] == manager.get(slug).tools.approval == expected


def test_yolo_banner_setting_is_on_by_default_and_persists_to_config(tmp_path):
    from tessera.config import load_config
    with running_server(tmp_path) as server:
        assert call(server, 'GET', '/api/status')[1]['ui']['yolo_banner'] is True
        assert call(server, 'POST', '/api/ui', {'yolo_banner': False}) == (200, {'yolo_banner': False})
        assert call(server, 'GET', '/api/status')[1]['ui']['yolo_banner'] is False
        assert call(server, 'POST', '/api/ui', {'yolo_banner': 'no'})[0] == 400
    assert load_config(tmp_path).ui.yolo_banner is False
