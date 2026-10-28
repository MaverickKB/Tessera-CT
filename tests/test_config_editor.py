"""Settings editor: validated, backed up, atomic, and served over loopback HTTP."""
import threading

from tessera.config import Config, load_config
from tessera.config_editor import ConfigEditor
from tessera.server import create_server
from test_server import call


def test_apply_validates_backs_up_and_keeps_other_lines(tmp_path):
    path = tmp_path / 'config.toml'
    path.write_text('# keep me\nagent_name = "Tessera"\nidentity_max_bytes = 20000\n\n[tools]\nclip_chars = 30000\n')
    editor = ConfigEditor(path)
    reply = editor.apply([{'path': ['identity_max_bytes'], 'value': 30000}, {'path': ['agent_name'], 'value': 'Alex'}])
    assert reply['changed'] and reply['restart_required'] and (tmp_path / reply['backup']).is_file()
    text = path.read_text()
    assert '# keep me' in text and load_config(tmp_path).identity_max_bytes == 30000 and load_config(tmp_path).agent_name == 'Alex'
    before = path.read_text()
    try:
        editor.apply([{'path': ['tools', 'not_an_option'], 'value': 1}])
        raise AssertionError('invalid config accepted')
    except ValueError as error:
        assert 'Rejected' in str(error)
    assert path.read_text() == before


def test_http_config_round_trip_and_restart_needs_supervision(tmp_path):
    (tmp_path / 'config.toml').write_text('identity_max_bytes = 20000\n')
    config = load_config(tmp_path)
    config.server.port = 0
    server = create_server(config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        code, body = call(server, 'GET', '/api/config')
        assert code == 200 and body['config']['identity_max_bytes'] == 20000 and body['supervised'] is False
        code, body = call(server, 'POST', '/api/config', {'edits': [{'path': ['identity_max_bytes'], 'value': 25000}]})
        assert code == 200 and body['changed']
        assert call(server, 'GET', '/api/config')[1]['config']['identity_max_bytes'] == 25000
        assert call(server, 'POST', '/api/config', {'edits': [{'path': ['tools', 'bogus'], 'value': 1}]})[0] == 400
        assert call(server, 'POST', '/api/core/restart', {})[0] == 409
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_null_edit_on_a_table_removes_the_whole_entry(tmp_path):
    path = tmp_path / 'config.toml'
    path.write_text('[backends.keep]\nbase_url = "http://127.0.0.1:1/v1"\nmodel = "m"\n\n'
                    '[backends.gone]\nbase_url = "http://127.0.0.1:2/v1"\nmodel = "x"\n\n[backends.gone.extra]\nretries = 0\n\n'
                    '[roles]\nmain = "keep"\n')
    reply = ConfigEditor(path).apply([{'path': ['backends', 'gone'], 'value': None}])
    assert reply['changed'] and (tmp_path / reply['backup']).is_file()
    assert 'gone' not in path.read_text() and sorted(load_config(tmp_path).backends) == ['keep']
    try:
        ConfigEditor(path).apply([{'path': ['backends', 'keep'], 'value': None}])
        raise AssertionError('removing the main backend must be rejected')
    except ValueError:
        pass
    assert 'keep' in path.read_text()


def test_concurrent_saves_do_not_lose_an_acknowledged_edit(tmp_path):
    """Two clients saving different keys must both land.

    Reproduced before the fix: ConfigEditor.apply read the whole file, built the
    new text, validated, then replaced. Two callers each read the same original,
    both returned 200, and the second replace discarded the first edit. The
    atomic replace alone never protected the read-modify-write transaction.
    """
    (tmp_path / 'config.toml').write_text(
        'identity_max_bytes = 20000\nagent_name = "Tessera"\n\n[tools]\nclip_chars = 30000\n')
    config = load_config(tmp_path)
    config.server.port = 0
    server = create_server(config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        barrier = threading.Barrier(2)
        results = {}

        def save(tag, edits):
            barrier.wait()
            results[tag] = call(server, 'POST', '/api/config', {'edits': edits})

        first = threading.Thread(target=save, args=(
            'identity', [{'path': ['identity_max_bytes'], 'value': 31000}]))
        second = threading.Thread(target=save, args=(
            'tools', [{'path': ['tools', 'clip_chars'], 'value': 41000}]))
        first.start(); second.start(); first.join(); second.join()

        assert results['identity'][0] == 200 and results['tools'][0] == 200
        saved = load_config(tmp_path)
        assert saved.identity_max_bytes == 31000
        assert saved.tools.clip_chars == 41000
    finally:
        server.shutdown()
        server.server_close()
