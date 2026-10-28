import pytest

from tessera.config import Config
from test_message_feedback import wait_idle
from test_server import running_server, call


def test_delete_removes_one_session_and_its_files_only(tmp_path):
    with running_server(tmp_path) as server:
        keep = call(server, 'POST', '/api/sessions', {'title': 'Keep'})[1]['slug']
        gone = call(server, 'POST', '/api/sessions', {'title': 'Throwaway'})[1]['slug']
        call(server, 'POST', f'/api/sessions/{gone}/messages', {'text': 'hello'})
        wait_idle(server, gone)
        root = tmp_path / 'sessions'
        assert (root / gone).is_dir()
        assert call(server, 'DELETE', f'/api/sessions/{gone}') == (200, {'deleted': gone})
        assert not (root / gone).exists() and (root / keep).is_dir()
        assert [s['slug'] for s in call(server, 'GET', '/api/sessions')[1]] == [keep]
        assert call(server, 'GET', f'/api/sessions/{gone}')[0] == 404
        assert call(server, 'DELETE', f'/api/sessions/{gone}')[0] == 404
        assert call(server, 'DELETE', '/api/sessions/..%2Fx')[0] in (400, 404)


def test_delete_refuses_a_session_with_work_in_flight(tmp_path):
    with running_server(tmp_path) as server:
        slug = call(server, 'POST', '/api/sessions', {'title': 'Busy'})[1]['slug']

        class Busy:
            def idle(self):
                return False

        server.manager._runtimes[slug] = Busy()
        try:
            status, body = call(server, 'DELETE', f'/api/sessions/{slug}')
            assert status == 409 and 'busy' in body['error']
            assert (tmp_path / 'sessions' / slug).is_dir()
        finally:
            server.manager._runtimes.pop(slug)


def test_ui_tuning_comes_from_config_through_status(tmp_path):
    with running_server(tmp_path) as server:
        server.manager.config.ui.history_page = 7
        server.manager.config.ui.item_body_chars = 900
        ui = call(server, 'GET', '/api/status')[1]['ui']
        # Assert the tuning keys this test is about, not the whole dict: the payload
        # also carries appearance settings (skin, accent_color) that are not tuning
        # and that a new one must not turn into a red test.
        expected = {'yolo_banner': True, 'history_page': 7, 'view_page_lines': 80,
                    'item_body_chars': 900, 'item_summary_chars': 120, 'long_press_ms': 500,
                    'approval_poll_ms': 1000, 'error_banner_seconds': 600.0, 'theme': 'system',
                    'show_thoughts': True, 'send_shortcut': 'enter'}
        assert {key: ui[key] for key in expected} == expected


@pytest.mark.parametrize('name,value', [('history_page', 0), ('item_body_chars', 'big'),
                                        ('item_summary_chars', 5), ('long_press_ms', True)])
def test_ui_tuning_is_validated(tmp_path, name, value):
    config = Config(home=tmp_path, cwd=tmp_path)
    setattr(config.ui, name, value)
    with pytest.raises(ValueError, match=f'ui.{name}'):
        config.validate()
