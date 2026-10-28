"""The raw config door must say what the running core holds, not just what the file says.

Why this file exists: POST /api/config (server.py) writes config.toml through
ConfigEditor and never calls adopt_config, while POST /api/settings adopts its edit
into the running core. A raw edit can set jev.enabled = false on disk while the
running process still holds true, and the route can answer changed with
restart_required. A caller reading 'changed' as 'in effect' would be told the
truth about the file and a lie about the process.

The correction is to report the disagreement instead of adopting it, because adopting
would let a raw edit change model routes in a running core mid-turn. These tests pin
the report: the reply carries what each side holds, and restart_required follows the
comparison rather than being hardcoded true.
"""

from tessera.config import load_config
from test_server import call
from test_settings_model import HOME, serve, stop


def _field(body, ident):
    for section in body['sections']:
        for field in section['fields']:
            if field['id'] == ident:
                return field
    raise AssertionError(f'no field {ident} in projection')


def test_raw_door_reports_the_disagreement_it_creates(tmp_path):
    server, thread = serve(tmp_path)
    try:
        # jev.enabled is False in both the file and the running core to start with.
        assert load_config(tmp_path).jev.enabled is False
        assert server.manager.config.jev.enabled is False

        code, body = call(server, 'POST', '/api/config',
                          {'edits': [{'path': ['jev', 'enabled'], 'value': True}]})
        assert code == 200, body
        assert body['changed'] is True

        # The file moved. The running core did not. Both facts are in the reply.
        assert load_config(tmp_path).jev.enabled is True
        assert server.manager.config.jev.enabled is False

        core = body['running_core']
        assert core['known'] is True and core['agrees'] is False
        pending = {tuple(row['path']): row for row in core['pending']}
        assert pending[('jev', 'enabled')]['file'] is True
        assert pending[('jev', 'enabled')]['running'] is False

        # restart_required now follows the comparison, not a constant.
        assert body['restart_required'] is True
        assert body['applied'] == 'restart'
        assert 'jev.enabled' in body['detail']

        # And the core's own status page still shows the value it actually holds.
        settings = call(server, 'GET', '/api/settings')[1]
        assert _field(settings, 'jev.enabled')['value'] is False
    finally:
        stop(server, thread)


def test_raw_door_is_live_when_the_core_already_holds_the_value(tmp_path):
    server, thread = serve(tmp_path)
    try:
        # Write the value the core already holds: nothing for a reload to do.
        code, body = call(server, 'POST', '/api/config',
                          {'edits': [{'path': ['identity_max_bytes'], 'value': 65536}]})
        assert code == 200, body
        assert body['changed'] is True
        assert body['running_core']['known'] is True and body['running_core']['agrees'] is True
        assert body['restart_required'] is False
        assert body['applied'] == 'live'
        assert server.manager.config.identity_max_bytes == 65536
    finally:
        stop(server, thread)


def test_a_later_settings_save_adopts_the_raw_edit_and_the_report_stops(tmp_path):
    """A later settings save adopts the raw edit, and the disagreement report stops."""
    server, thread = serve(tmp_path)
    try:
        call(server, 'POST', '/api/config', {'edits': [{'path': ['jev', 'enabled'], 'value': True}]})
        assert server.manager.config.jev.enabled is False

        # A settings save re-reads the whole file, so it adopts the raw edit it never made.
        code, body = call(server, 'POST', '/api/settings', {'id': 'jev.timeout', 'value': 9.0})
        assert code == 200, body
        assert server.manager.config.jev.timeout == 9.0
        assert server.manager.config.jev.enabled is True

        # Now the raw door's report is honest about agreement: jev.gate_threshold is
        # 0.7 in both the adopted core and the file, so writing it explicitly lands
        # as a live change with nothing pending.
        code, body = call(server, 'POST', '/api/config',
                          {'edits': [{'path': ['jev', 'gate_threshold'], 'value': 0.7}]})
        assert code == 200, body
        assert body['changed'] is True
        assert body['running_core']['agrees'] is True and body['restart_required'] is False
    finally:
        stop(server, thread)


def test_an_unchanged_raw_write_says_nothing_was_changed(tmp_path):
    server, thread = serve(tmp_path)
    try:
        code, body = call(server, 'POST', '/api/config',
                          {'edits': [{'path': ['identity_max_bytes'], 'value': 65536}]})
        # First write changes the file only if it differs; 65536 is the default, so it
        # is written as an explicit line. Then the identical write is a no-op.
        assert code == 200
        code, body = call(server, 'POST', '/api/config',
                          {'edits': [{'path': ['identity_max_bytes'], 'value': 65536}]})
        assert code == 200 and body['changed'] is False
        assert body['applied'] == 'live'
        assert 'running_core' not in body
    finally:
        stop(server, thread)
