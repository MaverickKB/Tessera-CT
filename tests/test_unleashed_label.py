"""The approval mode value stays yolo. Unleashed is the label a person reads."""

import http.client

import pytest

from tessera.config import Config, load_config
from tessera.settings_model import project
from tessera.sessions import SessionManager
from test_server import call, running_server


def _mode_field(body):
    return next(field for section in body['sections'] if section['id'] == 'permissions'
                for field in section['fields'] if field['id'] == 'permissions.mode')


def _egg_field(body):
    return next(field for section in body['sections'] if section['id'] == 'goblin'
                for field in section['fields'] if field['id'] == 'ui.easter_egg_f_it')


def test_stored_approval_mode_stays_yolo_and_the_label_is_unleashed(tmp_path):
    (tmp_path / 'config.toml').write_text('[tools]\napproval = "yolo"\n')
    config = load_config(tmp_path)
    assert config.tools.approval == 'yolo'
    with running_server(tmp_path) as server:
        server.manager.config.tools.approval = 'yolo'
        status, body = call(server, 'GET', '/api/approval')
        assert status == 200
        assert body['mode'] == 'yolo' and body['yolo'] is True
        status, posted = call(server, 'POST', '/api/approval', {'yolo': True})
        assert status == 200 and posted['mode'] == 'yolo' and posted['yolo'] is True
        assert server.manager.config.tools.approval == 'yolo'
        settings = call(server, 'GET', '/api/settings')[1]
    field = _mode_field(settings)
    assert field['value'] == 'yolo'
    assert field['options'] == [{'value': 'safe', 'label': 'Safe'}, {'value': 'yolo', 'label': 'Unleashed'}]


def test_easter_egg_is_off_by_default_and_settings_show_that_value(tmp_path):
    config = load_config(tmp_path)
    assert config.ui.easter_egg_f_it is False
    with SessionManager(config) as manager:
        assert manager.status()['ui']['easter_egg_f_it'] is False
        field = _egg_field(manager.settings_view())
    assert field['type'] == 'bool'
    assert field['value'] is False
    assert field['label'] == 'F-it button'
    assert 'yolo' in field['hint']


def test_easter_egg_round_trips_through_toml_env_and_settings(tmp_path, monkeypatch):
    (tmp_path / 'config.toml').write_text('[ui]\neaster_egg_f_it = true\n')
    assert load_config(tmp_path).ui.easter_egg_f_it is True
    monkeypatch.setenv('TESSERA_UI__EASTER_EGG_F_IT', 'false')
    assert load_config(tmp_path).ui.easter_egg_f_it is False
    monkeypatch.setenv('TESSERA_UI__EASTER_EGG_F_IT', 'true')
    assert load_config(tmp_path).ui.easter_egg_f_it is True

    (tmp_path / 'config.toml').write_text('[ui]\neaster_egg_f_it = false\n')
    monkeypatch.delenv('TESSERA_UI__EASTER_EGG_F_IT')
    with SessionManager(load_config(tmp_path)) as manager:
        for value in (True, False):
            result = manager.apply_settings({'id': 'ui.easter_egg_f_it', 'value': value})
            assert result['changed'] is True
            assert result['applied'] == 'live'
            assert manager.config.ui.easter_egg_f_it is value
            assert manager.status()['ui']['easter_egg_f_it'] is value
            assert _egg_field(manager.settings_view())['value'] is value
            assert load_config(tmp_path).ui.easter_egg_f_it is value
            assert manager.approval_status()['mode'] == 'ask'
        with pytest.raises(ValueError, match='true or false'):
            manager.apply_settings({'id': 'ui.easter_egg_f_it', 'value': 'yes'})


def test_easter_egg_rejects_non_booleans_and_does_not_change_approval(tmp_path):
    (tmp_path / 'config.toml').write_text('[ui]\neaster_egg_f_it = "yes"\n')
    with pytest.raises(ValueError, match='ui.easter_egg_f_it'):
        load_config(tmp_path)
    config = Config(home=tmp_path, cwd=tmp_path)
    config.ui.easter_egg_f_it = 'yes'
    with pytest.raises(ValueError, match='ui.easter_egg_f_it'):
        config.validate()
    config.ui.easter_egg_f_it = True
    config.tools.approval = 'ask'
    config.validate()
    with SessionManager(config) as manager:
        assert manager.status()['ui']['easter_egg_f_it'] is True
        assert manager.set_yolo(True)['mode'] == 'yolo'
        assert manager.config.tools.approval == 'yolo'
        assert manager.status()['ui']['easter_egg_f_it'] is True
        state = (tmp_path / 'state' / 'approval.json').read_text()
        assert '"mode": "yolo"' in state
        assert 'f-it' not in state


def _mark_field(body):
    return next(field for section in body['sections'] if section['id'] == 'appearance'
                for field in section['fields'] if field['id'] == 'ui.mark')


def test_icon_and_logo_values_are_visible_and_default_to_cairn(tmp_path, monkeypatch):
    config = load_config(tmp_path)
    assert config.ui.mark == 'cairn'
    with SessionManager(config) as manager:
        assert manager.status()['ui']['mark'] == 'cairn'
        field = _mark_field(manager.settings_view())
        egg = _egg_field(manager.settings_view())
    assert field['label'] == 'Icon and logo'
    assert [option['value'] for option in field['options']] == ['cairn', 'gremlin']
    assert [option['label'] for option in field['options']] == ['Tessera', 'Gremlin']
    assert 'Tessera-CT' in field['status']
    assert 'yolo' in egg['hint']
    (tmp_path / 'config.toml').write_text('[ui]\nmark = "gremlin"\neaster_egg_f_it = false\n')
    assert load_config(tmp_path).ui.mark == 'gremlin'
    monkeypatch.setenv('TESSERA_UI__MARK', 'cairn')
    assert load_config(tmp_path).ui.mark == 'cairn'
    monkeypatch.delenv('TESSERA_UI__MARK')
    (tmp_path / 'config.toml').write_text('[ui]\nmark = "nope"\n')
    with pytest.raises(ValueError, match='cairn or gremlin'):
        load_config(tmp_path)


def test_icon_and_logo_saves_both_values_without_a_second_switch(tmp_path):
    (tmp_path / 'config.toml').write_text('[ui]\nmark = "cairn"\n')
    with SessionManager(load_config(tmp_path)) as manager:
        for value in ('gremlin', 'cairn'):
            result = manager.apply_settings({'id': 'ui.mark', 'value': value})
            assert result['changed'] is True and result['applied'] == 'live'
            assert manager.config.ui.mark == value
            assert manager.status()['ui']['mark'] == value
            assert _mark_field(manager.settings_view())['value'] == value
        with pytest.raises(ValueError, match='not an allowed value'):
            manager.apply_settings({'id': 'ui.mark', 'value': 'secret'})
        manager.apply_settings({'id': 'ui.mark', 'value': 'gremlin'})
        manager.apply_settings({'id': 'ui.easter_egg_f_it', 'value': False})
        waiting = _mark_field(manager.settings_view())
        assert waiting['value'] == 'gremlin'
        assert waiting['status'] == 'Showing the gremlin logo.'
        assert 'F-it button' not in waiting['status']
        manager.apply_settings({'id': 'ui.easter_egg_f_it', 'value': True})
        showing = _mark_field(manager.settings_view())
        assert showing['value'] == 'gremlin'
        assert showing['status'] == 'F-it button is on, so the gremlin logo, favicon and crew are showing.'
        assert manager.approval_status()['mode'] == 'ask'
        ids = [field['id'] for section in manager.settings_view()['sections'] if section['id'] == 'appearance'
               for field in section['fields']]
        # F-it and the goblin it shows live in Goblin Mode now, not Appearance.
        assert ids.count('ui.easter_egg_f_it') == 0
        goblin_ids = [field['id'] for section in manager.settings_view()['sections'] if section['id'] == 'goblin'
                      for field in section['fields']]
        assert goblin_ids.count('ui.easter_egg_f_it') == 1
        assert 'ui.gremlin' not in ids


def _favicon(server):
    connection = http.client.HTTPConnection(*server.server_address, timeout=5)
    connection.request('GET', '/favicon.ico')
    response = connection.getresponse()
    body = response.read()
    result = (response.status, response.getheader('Content-Type') or '', body)
    connection.close()
    return result


def test_favicon_follows_the_f_it_setting_alone(tmp_path):
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    icon = (root / 'web' / 'brand' / 'icon-32.png').read_bytes()
    cartoon = (root / 'web' / 'brand' / 'gremlin-logo.svg').read_bytes()
    (tmp_path / 'config.toml').write_text('[ui]\nmark = "cairn"\neaster_egg_f_it = false\n')
    with running_server(tmp_path) as server:
        status, content_type, body = _favicon(server)
        assert status == 200 and body == icon and content_type.startswith('image/png')
        server.manager.apply_settings({'id': 'ui.mark', 'value': 'gremlin'})
        assert server.manager.config is server.config
        assert server.config.ui.mark == 'gremlin'
        status, content_type, body = _favicon(server)
        assert status == 200 and body == icon and content_type.startswith('image/png')
        server.manager.apply_settings({'id': 'ui.mark', 'value': 'cairn'})
        server.manager.apply_settings({'id': 'ui.easter_egg_f_it', 'value': True})
        assert server.manager.config.ui.mark == 'cairn'
        status, content_type, body = _favicon(server)
        assert status == 200 and body == cartoon and content_type.startswith('image/svg+xml')
        server.manager.apply_settings({'id': 'ui.easter_egg_f_it', 'value': False})
        status, content_type, body = _favicon(server)
        assert status == 200 and body == icon and content_type.startswith('image/png')
