"""Plugin settings are the configured MCP servers, applied to open sessions."""
import json
import os
import signal
import sys
import threading
import time

from tessera.config import load_config
from tessera.mcp_servers import CATALOG
from tessera.mcp_stdio import StdioMcpClient

from test_settings_model import HOME, call, serve, stop


PLUGIN = r'''
import json, os, sys
log = sys.argv[1] if len(sys.argv) > 1 else ""
if log:
    with open(log, "a", encoding="utf-8") as stream:
        stream.write("start\n")
        stream.write("pid %s\n" % os.getpid())

def reply(message_id, result):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": message_id, "result": result}) + "\n")
    sys.stdout.flush()

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        message = json.loads(line)
    except ValueError:
        continue
    if not isinstance(message, dict) or "id" not in message:
        continue
    method = message.get("method")
    if method == "initialize":
        reply(message["id"], {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}},
                              "serverInfo": {"name": "clock", "version": "0"}})
    elif method == "tools/list":
        reply(message["id"], {"tools": [
            {"name": "ping", "description": "Reply pong.", "inputSchema": {"type": "object", "properties": {}}},
            {"name": "tick", "description": "Reply tick.", "inputSchema": {"type": "object", "properties": {}}},
        ]})
    elif method == "tools/call":
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        token = os.environ.get("TOKEN", "")
        text = ("tick:" if params.get("name") == "tick" else "pong:") + token
        reply(message["id"], {"content": [{"type": "text", "text": text}]})
    else:
        reply(message["id"], {})
'''


def plugins(body):
    section = next(item for item in body['sections'] if item['id'] == 'plugins')
    return next(field for field in section['fields'] if field['id'] == 'mcp_servers')


def record(body, name):
    return next(item for item in plugins(body)['records'] if item['name'] == name)


def test_add_enable_disable_remove_persists_and_open_sessions_use_the_plugin(tmp_path):
    script = tmp_path / 'clock_plugin.py'
    script.write_text(PLUGIN)
    log = tmp_path / 'starts.txt'
    server, thread = serve(tmp_path)
    try:
        created = call(server, 'POST', '/api/sessions', {'title': 'Plugins'})
        assert created[0] in (200, 201)
        slug = created[1]['slug']
        session = server.manager.get(slug)
        assert 'clock__ping' not in session.tools.names()

        added = call(server, 'POST', '/api/settings', {'id': 'mcp_servers', 'action': 'add', 'record': {
            'name': 'clock', 'enabled': True, 'command': sys.executable,
            'args': f'{script}\n{log}', 'env': 'TOKEN=VOICE_NAME', 'session_tools': True}})
        assert added[0] == 200, added[1]
        assert added[1]['restart_required'] is False
        assert added[1]['applied'] == 'live'
        assert 'Open sessions' in added[1]['detail']
        saved = load_config(tmp_path).mcp_servers['clock']
        assert saved.command == [sys.executable, str(script), str(log)]
        assert saved.env == {'TOKEN': 'VOICE_NAME'}
        assert saved.enabled is True and saved.session_tools is True
        assert 'clock__ping' in session.tools.names()
        shown = record(added[1]['settings'], 'clock')
        assert shown['connection']['ok'] is True
        assert 'offered in 1 open session' in shown['connection']['detail']
        assert 'config.toml [mcp_servers.clock]' in shown['summary']
        assert sys.executable in shown['source']
        assert 'No token env' in shown['summary']

        previous = session.tools.approval
        session.tools.approval = 'yolo'
        try:
            assert session.tools.execute('clock__ping', {}) == 'pong:VOICE_NAME'
        finally:
            session.tools.approval = previous
        assert log.read_text().count('start') >= 1

        hidden = call(server, 'POST', '/api/settings', {'id': 'mcp_servers', 'action': 'edit', 'name': 'clock',
                                                        'record': {'session_tools': False}})
        assert hidden[0] == 200, hidden[1]
        assert load_config(tmp_path).mcp_servers['clock'].session_tools is False
        assert 'clock__ping' not in session.tools.names()
        assert 'none added to sessions' in record(hidden[1]['settings'], 'clock')['connection']['detail']
        assert 'none added to sessions' in hidden[1]['detail']
        assert 'use these plugins' not in hidden[1]['detail']
        restored = call(server, 'POST', '/api/settings', {'id': 'mcp_servers', 'action': 'edit', 'name': 'clock',
                                                          'record': {'session_tools': True}})
        assert restored[0] == 200
        assert 'clock__ping' in session.tools.names()

        other = call(server, 'POST', '/api/sessions', {'title': 'After'})
        assert other[0] in (200, 201)
        assert 'clock__ping' in server.manager.get(other[1]['slug']).tools.names()

        disabled = call(server, 'POST', '/api/settings', {'id': 'mcp_servers', 'action': 'edit', 'name': 'clock',
                                                          'record': {'enabled': False}})
        assert disabled[0] == 200 and disabled[1]['restart_required'] is False
        assert 'Disabled. Not connected.' in disabled[1]['detail']
        assert 'do not offer its tools' in disabled[1]['detail']
        assert 'use these plugins' not in disabled[1]['detail']
        assert load_config(tmp_path).mcp_servers['clock'].enabled is False
        assert 'clock__ping' not in session.tools.names()
        starts = log.read_text().count('start')
        checked = call(server, 'POST', '/api/settings', {'id': 'mcp_servers', 'action': 'check', 'name': 'clock'})
        assert checked[0] == 200
        assert checked[1]['detail'] == 'Disabled. Not connected.'
        assert log.read_text().count('start') == starts

        enabled = call(server, 'POST', '/api/settings', {'id': 'mcp_servers', 'action': 'edit', 'name': 'clock',
                                                         'record': {'enabled': True}})
        assert enabled[0] == 200
        assert 'clock__ping' in session.tools.names()
        session.tools.approval = 'yolo'
        try:
            assert session.tools.execute('clock__ping', {}) == 'pong:VOICE_NAME'
        finally:
            session.tools.approval = previous

        removed = call(server, 'POST', '/api/settings', {'id': 'mcp_servers', 'action': 'remove', 'name': 'clock'})
        assert removed[0] == 200 and removed[1]['restart_required'] is False
        assert 'removed from open sessions' in removed[1]['detail']
        assert 'clock' not in load_config(tmp_path).mcp_servers
        assert 'clock__ping' not in session.tools.names()
        assert 'clock' not in (tmp_path / 'config.toml').read_text()
    finally:
        CATALOG.close_all()
        stop(server, thread)


def test_a_missing_command_is_a_real_error_and_is_not_called_connected(tmp_path):
    server, thread = serve(tmp_path)
    try:
        added = call(server, 'POST', '/api/settings', {'id': 'mcp_servers', 'action': 'add', 'record': {
            'name': 'missing', 'enabled': True, 'command': str(tmp_path / 'no-such-plugin')}})
        assert added[0] == 200, added[1]
        shown = record(added[1]['settings'], 'missing')
        connection = shown['connection']
        assert shown['status'] == 'Error'
        assert connection['ok'] is False
        assert connection['state'] == 'error'
        assert 'FileNotFoundError' in connection['detail']
        assert 'Connected' not in connection['detail']
        assert connection['last_error'] == connection['detail']
        assert 'FileNotFoundError' in added[1]['detail']
        assert 'Connected' not in added[1]['detail']
        assert 'use these plugins' not in added[1]['detail']
        created = call(server, 'POST', '/api/sessions', {'title': 'Still missing'})
        assert created[0] in (200, 201)
        assert not any(name.startswith('missing__') for name in server.manager.get(created[1]['slug']).tools.names())
        disabled = call(server, 'POST', '/api/settings', {'id': 'mcp_servers', 'action': 'edit', 'name': 'missing',
                                                          'record': {'enabled': False}})
        assert disabled[0] == 200
        row = record(disabled[1]['settings'], 'missing')
        assert row['status'] == 'Disabled'
        assert row['connection']['detail'] == 'Disabled. Not connected.'
        assert 'FileNotFoundError' in row['connection']['last_error']
        assert 'config.toml [mcp_servers.missing]' in row['summary']
    finally:
        CATALOG.close_all()
        stop(server, thread)


def test_aethermind_row_shows_the_reported_state_and_does_not_choose_it(tmp_path):
    from tessera.settings_model import _aethermind_record

    class EmbeddedReport:
        def status(self):
            return {'aethermind': {
                'enabled': False,
                'active': 'embedded',
                'reason': 'External service did not validate',
                'external_error': 'URLError: refused',
            }}

    class ExternalReport:
        def status(self):
            return {'aethermind': {'active': 'external', 'reason': 'External service validated'}}

    (tmp_path / 'config.toml').write_text(HOME)
    config = load_config(tmp_path)
    config.aethermind.url = 'http://127.0.0.1:9/mcp'
    embedded = _aethermind_record(config, EmbeddedReport())
    assert embedded['status'] == 'Embedded'
    assert embedded['source'] == 'Embedded AetherMind'
    assert embedded['actions'] == []
    assert embedded['fields'] == []
    assert 'External service did not validate' in embedded['summary']
    assert embedded['connection']['state'] == 'error'
    assert embedded['connection']['detail'] == 'URLError: refused'
    assert 'no off switch' in embedded['note']
    assert 'PR ' not in embedded['note']
    assert 'switched off' not in embedded['summary']
    external = _aethermind_record(config, ExternalReport())
    assert external['status'] == 'External'
    assert external['source'] == 'http://127.0.0.1:9/mcp'
    assert 'External service validated' in external['summary']
    assert external['actions'] == []

    server, thread = serve(tmp_path)
    try:
        code, body = call(server, 'GET', '/api/settings')
        assert code == 200
        row = record(body, 'aethermind')
        assert row['template'] is True
        assert row['title'] == 'AetherMind'
        assert row['status'] == 'Unreported'
        assert row['summary'] == 'This core does not report which AetherMind is active yet.'
        assert row['actions'] == []
        assert row['fields'] == []
        assert row['source'] == 'None saved'
        assert 'no off switch' in row['note']
        assert 'PR ' not in row['note']
        assert 'PR ' not in row['summary']
        assert 'optional' not in json.dumps(row).lower()
        assert 'switched off' not in row['summary']
        plugins = next(section for section in body['sections'] if section['id'] == 'plugins')
        listed = next(field for field in plugins['fields'] if field['id'] == 'mcp_servers')
        assert 'does not report which AetherMind is active' in listed['hint']
        assert 'PR ' not in listed['hint']
        assert 'no off switch' in listed['hint']
        assert 'turn AetherMind off' not in listed['hint']
        assert 'on/off' not in listed['hint'].lower()
        integrations = next(section for section in body['sections'] if section['id'] == 'integrations')
        assert 'optional' not in integrations['start_hint'].lower()
        assert 'enable AetherMind' not in integrations['start_hint']
        ids = [field.get('id') for section in body['sections'] for field in section['fields']]
        assert 'aethermind.enabled' not in ids
        assert 'aethermind' not in load_config(tmp_path).mcp_servers
        blob = json.dumps(row)
        assert 'AETHERMIND_TOKEN' not in blob
        assert 'Off keeps AetherMind quiet' not in json.dumps(body)

        before = (tmp_path / 'config.toml').read_text()
        refused = call(server, 'POST', '/api/settings', {'id': 'aethermind.enabled', 'value': False})
        assert refused[0] == 400
        for action, payload in (
            ('edit', {'record': {'enabled': False}}),
            ('edit', {'record': {'url': 'http://127.0.0.1:9/mcp'}}),
            ('check', {}),
            ('remove', {}),
        ):
            body_post = {'id': 'mcp_servers', 'action': action, 'name': 'aethermind'}
            body_post.update(payload)
            answer = call(server, 'POST', '/api/settings', body_post)
            assert answer[0] == 400, answer[1]
            assert 'read-only' in answer[1]['error']
            assert 'PR ' not in answer[1]['error']
        assert (tmp_path / 'config.toml').read_text() == before
        assert '["aethermind"]' not in before

        yolo = call(server, 'POST', '/api/settings', {'id': 'permissions.mode', 'value': 'yolo'})
        assert yolo[0] == 200 and yolo[1]['approval']['yolo'] is True
        yolo_row = record(yolo[1]['settings'], 'aethermind')
        assert yolo_row['status'] == 'Unreported'
        assert yolo_row['actions'] == []
        assert yolo_row['fields'] == []
        yolo_ids = [field.get('id') for section in yolo[1]['settings']['sections'] for field in section['fields']]
        assert 'aethermind.enabled' not in yolo_ids
    finally:
        CATALOG.close_all()
        stop(server, thread)


def test_a_status_failure_is_an_error_on_the_aethermind_row(tmp_path):
    from tessera.settings_model import _aethermind_record
    (tmp_path / 'config.toml').write_text(HOME)
    config = load_config(tmp_path)

    class Broken:
        def status(self):
            raise RuntimeError('status boom')

    row = _aethermind_record(config, Broken())
    assert row['status'] == 'Error'
    assert row['connection']['state'] == 'error'
    assert row['connection']['detail'] == 'RuntimeError: status boom'
    assert row['connection']['last_error'] == 'RuntimeError: status boom'
    assert row['summary'] == 'RuntimeError: status boom'
    assert 'Unreported' not in json.dumps(row)
    assert 'PR ' not in json.dumps(row)

    class Odd(Exception):
        pass

    class Unexpected:
        def status(self):
            raise Odd('not a status error')

    try:
        _aethermind_record(config, Unexpected())
    except Odd as error:
        assert str(error) == 'not a status error'
    else:
        raise AssertionError('an unexpected status failure was hidden')


def test_a_failed_plugin_close_is_logged(capsys):
    from tessera.mcp_servers import Catalog
    catalog = Catalog()

    class BrokenPipe:
        def close(self):
            raise OSError('broken pipe')

    catalog._clients[('clock', 'http://127.0.0.1:9/mcp', (), (), '')] = BrokenPipe()
    catalog.close_named('clock')
    assert 'mcp: closing a plugin client failed (OSError: broken pipe)' in capsys.readouterr().err
    assert catalog._clients == {}

    class StillRunning:
        def close(self):
            raise RuntimeError('still running')

    catalog._clients[('desk', '', (), (), '')] = StillRunning()
    catalog.close_all()
    assert 'mcp: closing a plugin client failed (RuntimeError: still running)' in capsys.readouterr().err

    class NotACloseError:
        def close(self):
            raise ValueError('not a close error')

    catalog._clients[('other', '', (), (), '')] = NotACloseError()
    try:
        catalog.close_named('other')
    except ValueError as error:
        assert str(error) == 'not a close error'
    else:
        raise AssertionError('close swallowed ValueError')
    assert 'mcp: closing a plugin client failed' not in capsys.readouterr().err


def test_a_row_names_plugin_settings_this_page_does_not_edit(tmp_path):
    text = HOME + '''
[mcp_servers.shelf]
command = ["tool-server"]
cwd = "workers"
include = ["ping"]
tool_prefix = "shelf_"
timeout = 12
'''
    server, thread = serve(tmp_path, text)
    try:
        code, body = call(server, 'GET', '/api/settings')
        assert code == 200
        row = record(body, 'shelf')
        assert row['connection']['detail'] == 'Not checked yet.'
        assert 'not edited on this page' in row['note']
        assert 'workers' in row['note']
        assert 'ping' in row['note']
        assert 'shelf_' in row['note']
        assert '12s' in row['note']
        assert 'http://' not in row['note']
    finally:
        stop(server, thread)


def test_a_manual_off_switch_stays_off_in_yolo(tmp_path):
    """Unleashed drops system guards. A plugin or tool turned off by hand stays off.

    exclude is the per-tool switch and lives in config.toml. Enabled and
    Session tools are the switches on this page. None of them flip on when
    the approval mode becomes Unleashed.
    """
    script = tmp_path / 'clock_plugin.py'
    script.write_text(PLUGIN)
    log = tmp_path / 'starts.txt'
    text = HOME + f'''
[mcp_servers.clock]
enabled = true
session_tools = true
command = ["{sys.executable}", "{script}", "{log}"]
exclude = ["ping"]
'''
    server, thread = serve(tmp_path, text)
    try:
        created = call(server, 'POST', '/api/sessions', {'title': 'Held'})
        assert created[0] in (200, 201)
        session = server.manager.get(created[1]['slug'])
        assert 'clock__tick' in session.tools.names()
        assert 'clock__ping' not in session.tools.names()

        switched = call(server, 'POST', '/api/settings', {'id': 'permissions.mode', 'value': 'yolo'})
        assert switched[0] == 200 and switched[1]['approval']['yolo'] is True
        assert session.tools.approval == 'yolo'
        saved = load_config(tmp_path).mcp_servers['clock']
        assert saved.enabled is True and saved.session_tools is True and saved.exclude == ['ping']
        assert 'clock__tick' in session.tools.names()
        assert 'clock__ping' not in session.tools.names()
        asked = []
        session.tools.approve = lambda name, arguments: asked.append(name) or False
        assert session.tools.execute('clock__tick', {}) == 'tick:'
        assert asked == []
        plugins_section = next(item for item in switched[1]['settings']['sections'] if item['id'] == 'plugins')
        assert 'stay off when you turn them off, in Unleashed too' in plugins_section['start_hint']
        shown = record(switched[1]['settings'], 'clock')
        hints = {item['key']: item.get('hint', '') for item in shown['fields']}
        assert 'including in Unleashed' in hints['enabled']
        assert 'including in Unleashed' in hints['session_tools']
        assert 'secret' not in hints['env'].lower()

        hidden = call(server, 'POST', '/api/settings', {'id': 'mcp_servers', 'action': 'edit', 'name': 'clock',
                                                        'record': {'session_tools': False}})
        assert hidden[0] == 200, hidden[1]
        assert load_config(tmp_path).mcp_servers['clock'].session_tools is False
        assert load_config(tmp_path).mcp_servers['clock'].exclude == ['ping']
        assert 'clock__tick' not in session.tools.names()
        assert 'clock__ping' not in session.tools.names()
        assert server.manager.approval_status()['yolo'] is True

        disabled = call(server, 'POST', '/api/settings', {'id': 'mcp_servers', 'action': 'edit', 'name': 'clock',
                                                          'record': {'enabled': False, 'session_tools': True}})
        assert disabled[0] == 200, disabled[1]
        assert load_config(tmp_path).mcp_servers['clock'].enabled is False
        assert 'clock__tick' not in session.tools.names()
        starts = log.read_text().count('start')
        checked = call(server, 'POST', '/api/settings', {'id': 'mcp_servers', 'action': 'check', 'name': 'clock'})
        assert checked[0] == 200
        assert checked[1]['detail'] == 'Disabled. Not connected.'
        assert log.read_text().count('start') == starts
        assert server.manager.approval_status()['yolo'] is True
    finally:
        CATALOG.close_all()
        stop(server, thread)


def test_yolo_stores_a_literal_env_and_runs_the_tool_without_approval(tmp_path):
    """The value is stored as written in both modes. Safe warns; Unleashed does not.

    The tool stays risky. Safe mode uses the existing approval policy and caps
    an environment value at 500 characters. Unleashed stores a longer value, does not
    warn, and runs the tool with no approval. This page adds no second gate.
    """
    from tessera.tools import ApprovalRefused
    script = tmp_path / 'clock_plugin.py'
    script.write_text(PLUGIN)
    log = tmp_path / 'starts.txt'
    server, thread = serve(tmp_path)
    try:
        created = call(server, 'POST', '/api/sessions', {'title': 'Mode'})
        assert created[0] in (200, 201)
        session = server.manager.get(created[1]['slug'])
        added = call(server, 'POST', '/api/settings', {'id': 'mcp_servers', 'action': 'add', 'record': {
            'name': 'clock', 'enabled': True, 'command': sys.executable,
            'args': f'{script}\n{log}', 'env': 'TOKEN=voice-secret', 'session_tools': True}})
        assert added[0] == 200, added[1]
        hints = {item['key']: item.get('hint', '') for item in record(added[1]['settings'], 'clock')['fields']}
        assert 'do not put secrets' in hints['env']
        assert 'passed as-is' in hints['env']
        assert load_config(tmp_path).mcp_servers['clock'].env == {'TOKEN': 'voice-secret'}
        assert 'clock__ping' in session.tools.names()
        capped = call(server, 'POST', '/api/settings', {'id': 'mcp_servers', 'action': 'add', 'record': {
            'name': 'wide', 'enabled': False, 'command': sys.executable,
            'args': f'{script}\n{log}', 'env': 'TOKEN=' + ('x' * 501)}})
        assert capped[0] == 400
        assert 'wide' not in load_config(tmp_path).mcp_servers
        asked = []
        session.tools.approve = lambda name, arguments: asked.append(name) or False
        try:
            session.tools.execute('clock__ping', {})
            raise AssertionError('safe mode ran a risky plugin tool')
        except ApprovalRefused as refused:
            assert refused.policy == 'ask'
            assert refused.tool == 'clock__ping'
        assert asked == ['clock__ping']

        switched = call(server, 'POST', '/api/settings', {'id': 'permissions.mode', 'value': 'yolo'})
        assert switched[0] == 200, switched[1]
        assert switched[1]['approval']['yolo'] is True
        assert session.tools.approval == 'yolo'
        hints = {item['key']: item.get('hint', '') for item in record(switched[1]['settings'], 'clock')['fields']}
        assert 'secret' not in hints['env'].lower()
        assert 'not looked up' in hints['env']
        widened = call(server, 'POST', '/api/settings', {'id': 'mcp_servers', 'action': 'add', 'record': {
            'name': 'wide', 'enabled': False, 'command': sys.executable,
            'args': f'{script}\n{log}', 'env': 'TOKEN=' + ('x' * 501)}})
        assert widened[0] == 200, widened[1]
        assert load_config(tmp_path).mcp_servers['wide'].env == {'TOKEN': 'x' * 501}
        assert session.tools.execute('clock__ping', {}) == 'pong:voice-secret'
        assert asked == ['clock__ping']

        safe = call(server, 'POST', '/api/settings', {'id': 'permissions.mode', 'value': 'safe'})
        assert safe[0] == 200 and safe[1]['approval']['yolo'] is False
        assert session.tools.approval == 'ask'
        assert 'clock__ping' in session.tools.names()
        try:
            session.tools.execute('clock__ping', {})
            raise AssertionError('safe mode ran a risky plugin tool')
        except ApprovalRefused as refused:
            assert refused.policy == 'ask'
            assert refused.tool == 'clock__ping'
        assert asked == ['clock__ping', 'clock__ping']
    finally:
        CATALOG.close_all()
        stop(server, thread)


SLOW = r'''
import json, os, sys, time
log = sys.argv[1]
with open(log, "a", encoding="utf-8") as stream:
    stream.write("start\n")
    stream.write("pid %s\n" % os.getpid())
    stream.flush()

def reply(message_id, result):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": message_id, "result": result}) + "\n")
    sys.stdout.flush()

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        message = json.loads(line)
    except ValueError:
        continue
    if not isinstance(message, dict) or "id" not in message:
        continue
    method = message.get("method")
    if method == "initialize":
        reply(message["id"], {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}},
                              "serverInfo": {"name": "slow", "version": "0"}})
    elif method == "tools/list":
        reply(message["id"], {"tools": [
            {"name": "wait", "description": "Sleep.", "inputSchema": {"type": "object", "properties": {}}},
        ]})
    elif method == "tools/call":
        with open(log, "a", encoding="utf-8") as stream:
            stream.write("call\n")
            stream.flush()
        time.sleep(60)
        with open(log, "a", encoding="utf-8") as stream:
            stream.write("done\n")
            stream.flush()
        reply(message["id"], {"content": [{"type": "text", "text": "late"}]})
    else:
        reply(message["id"], {})
'''


def _plugin_pids(log):
    pids = []
    for line in log.read_text().splitlines():
        if line.startswith('pid '):
            pids.append(int(line.split()[1]))
    return pids


def _process_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_discovery_outside_only_runs_before_the_manager_lock():
    """apply() re-registers every enabled server. Listing one must not call tools() under the lock."""
    from tessera.mcp_servers import reload_installed_plugins

    lock = threading.Lock()
    seen = []

    class Server:
        def __init__(self, name):
            self.name = name
            self.enabled = True
            self.session_tools = True
            self.include = ()
            self.exclude = ()
            self.safe_tools = ()
            self.tool_prefix = ''

    clock = Server('clock')
    other = Server('other')

    class Config:
        mcp_servers = {'clock': clock, 'other': other}
        computer_use = type('Computer', (), {'provider': ''})()
        tools = type('ToolsConfig', (), {'clip_chars': 4000})()

    class Registry:
        def __init__(self):
            self._tools = {}

        def register(self, name, description, parameters, handler, risky=False, origin='builtin'):
            seen.append(('register', name, lock.locked()))
            self._tools[name] = handler

        def names(self):
            return list(self._tools)

    class Session:
        depth = 0
        config = Config()
        tools = Registry()
        _plugin_tools = ()
        _plugin_servers = ()
        tool_ceiling = set()

    class Manager:
        _lock = lock
        _runtimes = {'open': type('Runtime', (), {'session': Session()})()}

    class Listing:
        def inspect(self, server, *, keep=False):
            seen.append(('inspect', server.name, lock.locked()))
            return [{'name': 'ping', 'description': 'Reply.', 'inputSchema': {'type': 'object'}}], None

        def tools(self, server):
            seen.append(('tools', server.name, lock.locked()))
            return [{'name': 'pong', 'description': 'Reply.', 'inputSchema': {'type': 'object'}}]

        def close_named(self, name):
            seen.append(('close', name, lock.locked()))

    session = Manager._runtimes['open'].session
    statuses, error = reload_installed_plugins(Manager(), Listing(), only={'clock'})
    assert error == ''
    assert statuses['clock']['state'] == 'connected'
    assert 'other' not in statuses
    tool_calls = [item for item in seen if item[0] == 'tools']
    assert tool_calls == [('tools', 'other', False)]
    assert all(not locked for kind, _name, locked in seen if kind == 'tools')
    assert ('inspect', 'clock', False) in seen
    assert any(locked for kind, _name, locked in seen if kind == 'register')
    assert 'clock__ping' in session.tools.names()
    assert 'other__pong' in session.tools.names()


def test_stop_still_works_while_disabling_a_slow_stdio_call(tmp_path):
    """Disable must not hold the manager lock across stdio close.

    The running call holds the client lock. Stop needs the manager lock to
    cancel that call. If close waits under the manager lock, Stop cannot
    return until the tool finishes.
    """
    script = tmp_path / 'slow_plugin.py'
    script.write_text(SLOW)
    log = tmp_path / 'slow.txt'
    server, thread = serve(tmp_path)
    original_close = StdioMcpClient.close
    threads = []
    pid = None
    try:
        created = call(server, 'POST', '/api/sessions', {'title': 'Slow'})
        assert created[0] in (200, 201)
        slug = created[1]['slug']
        session = server.manager.get(slug)
        added = call(server, 'POST', '/api/settings', {'id': 'mcp_servers', 'action': 'add', 'record': {
            'name': 'slow', 'enabled': True, 'command': sys.executable,
            'args': f'{script}\n{log}', 'session_tools': True}})
        assert added[0] == 200, added[1]
        assert 'slow__wait' in session.tools.names()
        clients = [client for key, client in CATALOG._clients.items() if key and key[0] == 'slow']
        assert len(clients) == 1
        clients[0].timeout = 8
        session.tools.approval = 'yolo'
        pid = _plugin_pids(log)[-1]
        assert _process_alive(pid)

        holder = {}

        def run_tool():
            try:
                holder['result'] = session.tools.execute('slow__wait', {})
            except Exception as error:
                holder['error'] = error

        tool_thread = threading.Thread(target=run_tool)
        threads.append(tool_thread)
        tool_thread.start()
        deadline = time.monotonic() + 10
        while 'call' not in log.read_text():
            assert time.monotonic() < deadline, holder
            time.sleep(0.02)

        closing = threading.Event()

        def close(self):
            closing.set()
            return original_close(self)

        StdioMcpClient.close = close
        disabled = {}

        def run_disable():
            disabled['result'] = call(server, 'POST', '/api/settings', {
                'id': 'mcp_servers', 'action': 'edit', 'name': 'slow', 'record': {'enabled': False}})

        disable_thread = threading.Thread(target=run_disable)
        threads.append(disable_thread)
        disable_thread.start()
        assert closing.wait(10), 'disable did not reach stdio close while the call was in flight'

        stopped = {}

        def run_stop():
            stopped['at'] = time.monotonic()
            stopped['result'] = server.manager.interrupt(slug)
            stopped['elapsed'] = time.monotonic() - stopped['at']

        stop_thread = threading.Thread(target=run_stop)
        threads.append(stop_thread)
        stop_thread.start()
        stop_thread.join(3)
        assert not stop_thread.is_alive(), 'Stop waited on the in-flight stdio call'
        assert stopped['elapsed'] < 3
        assert stopped['result']['stopped'] is True
        assert 'call' in log.read_text()
        assert 'done' not in log.read_text()
        tool_thread.join(5)
        disable_thread.join(5)
        assert not tool_thread.is_alive()
        assert not disable_thread.is_alive()
        assert disabled['result'][0] == 200, disabled['result']
        assert load_config(tmp_path).mcp_servers['slow'].enabled is False
        assert 'slow__wait' not in session.tools.names()
        assert not _process_alive(pid)
        assert not any(key and key[0] == 'slow' for key in CATALOG._clients)
    finally:
        StdioMcpClient.close = original_close
        if pid is not None:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        for worker in threads:
            worker.join(5)
        CATALOG.close_all()
        stop(server, thread)


def test_disabling_with_zero_runtimes_closes_the_inspected_client(tmp_path):
    """A Check or add starts the process before any session exists. Disable closes it."""
    script = tmp_path / 'clock_plugin.py'
    script.write_text(PLUGIN)
    log = tmp_path / 'starts.txt'
    server, thread = serve(tmp_path)
    try:
        assert server.manager._runtimes == {}
        added = call(server, 'POST', '/api/settings', {'id': 'mcp_servers', 'action': 'add', 'record': {
            'name': 'clock', 'enabled': True, 'command': sys.executable,
            'args': f'{script}\n{log}', 'session_tools': True}})
        assert added[0] == 200, added[1]
        assert server.manager._runtimes == {}
        assert 'No session is open' in added[1]['detail']
        pid = _plugin_pids(log)[-1]
        assert _process_alive(pid)
        assert any(key and key[0] == 'clock' for key in CATALOG._clients)

        disabled = call(server, 'POST', '/api/settings', {'id': 'mcp_servers', 'action': 'edit', 'name': 'clock',
                                                          'record': {'enabled': False}})
        assert disabled[0] == 200, disabled[1]
        assert 'Disabled. Not connected.' in disabled[1]['detail']
        assert server.manager._runtimes == {}
        assert load_config(tmp_path).mcp_servers['clock'].enabled is False
        assert not _process_alive(pid)
        assert not any(key and key[0] == 'clock' for key in CATALOG._clients)

        starts = log.read_text().count('start')
        switched = call(server, 'POST', '/api/settings', {'id': 'permissions.mode', 'value': 'yolo'})
        assert switched[0] == 200 and switched[1]['approval']['yolo'] is True
        assert load_config(tmp_path).mcp_servers['clock'].enabled is False
        checked = call(server, 'POST', '/api/settings', {'id': 'mcp_servers', 'action': 'check', 'name': 'clock'})
        assert checked[0] == 200
        assert checked[1]['detail'] == 'Disabled. Not connected.'
        assert log.read_text().count('start') == starts
        assert not _process_alive(pid)
        assert server.manager._runtimes == {}
    finally:
        CATALOG.close_all()
        stop(server, thread)


def _memory_plugin_config(tmp_path, script, log, *, enabled=True):
    return HOME + f'''
[mcp_servers.x]
enabled = {'true' if enabled else 'false'}
session_tools = true
command = ["{sys.executable}", "{script}", "{log}"]

[aethermind]
endpoints = ["mcp:x"]
'''


def _started_memory_client(server, log):
    """Open the client memory uses, and return the process ids it started."""
    from tessera.mcp_servers import resolve_endpoint_factory
    client = resolve_endpoint_factory(server.manager.config)('mcp:x')
    client.list_tools()
    pids = _plugin_pids(log)
    assert pids and all(_process_alive(pid) for pid in pids)
    return client, pids


def _assert_same_processes(log, pids):
    assert _plugin_pids(log) == pids
    assert all(_process_alive(pid) for pid in pids)


def test_disabling_the_plugin_that_serves_memory_is_refused(tmp_path):
    """endpoints=['mcp:x'] keeps using x. Disable is refused and the row says so."""
    script = tmp_path / 'clock_plugin.py'
    script.write_text(PLUGIN)
    log = tmp_path / 'starts.txt'
    server, thread = serve(tmp_path, _memory_plugin_config(tmp_path, script, log))
    try:
        _client, pids = _started_memory_client(server, log)
        shown = call(server, 'GET', '/api/settings')
        assert shown[0] == 200
        row = record(shown[1], 'x')
        enabled_field = next(field for field in row['fields'] if field['key'] == 'enabled')
        assert row['status'] == 'Memory'
        assert 'mcp:x' in row['summary']
        assert 'AetherMind' in row['summary']
        assert 'Disabled. Not connected.' not in row['connection']['detail']
        assert 'aethermind.endpoints includes mcp:x' in row['note']
        assert 'AetherMind has no off switch' in row['note']
        assert enabled_field['value'] is True
        assert enabled_field['inert'] is True
        _assert_same_processes(log, pids)

        disabled = call(server, 'POST', '/api/settings', {'id': 'mcp_servers', 'action': 'edit', 'name': 'x',
                                                          'record': {'enabled': False}})
        assert disabled[0] == 400, disabled[1]
        assert 'mcp:x' in disabled[1]['error']
        assert 'aethermind.endpoints' in disabled[1]['error']
        assert 'AetherMind has no off switch' in disabled[1]['error']
        assert load_config(tmp_path).mcp_servers['x'].enabled is True
        _assert_same_processes(log, pids)
        again = call(server, 'GET', '/api/settings')
        row = record(again[1], 'x')
        assert row['status'] == 'Memory'
        assert row['connection']['detail'] != 'Disabled. Not connected.'
        assert next(field for field in row['fields'] if field['key'] == 'enabled')['inert'] is True

        checked = call(server, 'POST', '/api/settings', {'id': 'mcp_servers', 'action': 'check', 'name': 'x'})
        assert checked[0] == 200, checked[1]
        assert 'Disabled. Not connected.' not in checked[1]['detail']
        assert 'mcp:x' in checked[1]['detail']
        assert 'live memory process stays running' in checked[1]['detail']
        checked_row = record(checked[1]['settings'], 'x')
        assert checked_row['status'] == 'Memory'
        assert 'mcp:x' in checked_row['summary']
        assert next(field for field in checked_row['fields'] if field['key'] == 'enabled')['inert'] is True
        _assert_same_processes(log, pids)

        edited = call(server, 'POST', '/api/settings', {'id': 'mcp_servers', 'action': 'edit', 'name': 'x',
                                                        'record': {'token_env': 'MEMORY_TOKEN'}})
        assert edited[0] == 200, edited[1]
        assert 'live memory process stays running' in edited[1]['detail']
        assert load_config(tmp_path).mcp_servers['x'].token_env == 'MEMORY_TOKEN'
        assert load_config(tmp_path).mcp_servers['x'].enabled is True
        edited_row = record(edited[1]['settings'], 'x')
        assert edited_row['status'] == 'Memory'
        assert 'Disabled. Not connected.' not in edited_row['connection']['detail']
        _assert_same_processes(log, pids)

        other = tmp_path / 'other_plugin.py'
        other.write_text(PLUGIN)
        retargeted = call(server, 'POST', '/api/settings', {'id': 'mcp_servers', 'action': 'edit', 'name': 'x',
                                                            'record': {'command': sys.executable,
                                                                       'args': f'{other}\n{log}'}})
        assert retargeted[0] == 200, retargeted[1]
        assert 'separate connection' in retargeted[1]['detail']
        assert 'mcp:x' in retargeted[1]['detail']
        assert all(_process_alive(pid) for pid in pids)
        assert _plugin_pids(log)[:len(pids)] == pids
        assert len(_plugin_pids(log)) > len(pids)

        removed = call(server, 'POST', '/api/settings', {'id': 'mcp_servers', 'action': 'remove', 'name': 'x'})
        assert removed[0] == 400, removed[1]
        assert 'mcp:x' in removed[1]['error']
        assert 'aethermind.endpoints' in removed[1]['error']
        assert 'x' in load_config(tmp_path).mcp_servers
        assert all(_process_alive(pid) for pid in pids)
        assert _plugin_pids(log)[:len(pids)] == pids
    finally:
        CATALOG.close_all()
        stop(server, thread)


def test_a_memory_plugin_stored_off_still_shows_memory_and_stays_running(tmp_path):
    """A file that already has enabled off still serves memory. Check and edit leave the process."""
    script = tmp_path / 'clock_plugin.py'
    script.write_text(PLUGIN)
    log = tmp_path / 'starts.txt'
    server, thread = serve(tmp_path, _memory_plugin_config(tmp_path, script, log, enabled=False))
    try:
        _client, pids = _started_memory_client(server, log)
        shown = call(server, 'GET', '/api/settings')
        assert shown[0] == 200
        row = record(shown[1], 'x')
        enabled_field = next(field for field in row['fields'] if field['key'] == 'enabled')
        assert row['status'] == 'Memory'
        assert 'Memory keeps using this plugin' in row['summary']
        assert 'mcp:x' in row['note']
        assert row['connection']['detail'] != 'Disabled. Not connected.'
        assert 'Serves AetherMind memory (mcp:x)' in row['connection']['detail']
        assert enabled_field['value'] is False
        assert not enabled_field.get('inert')

        checked = call(server, 'POST', '/api/settings', {'id': 'mcp_servers', 'action': 'check', 'name': 'x'})
        assert checked[0] == 200, checked[1]
        assert checked[1]['detail'] != 'Disabled. Not connected.'
        assert 'live memory process stays running' in checked[1]['detail']
        assert record(checked[1]['settings'], 'x')['status'] == 'Memory'
        _assert_same_processes(log, pids)

        edited = call(server, 'POST', '/api/settings', {'id': 'mcp_servers', 'action': 'edit', 'name': 'x',
                                                        'record': {'session_tools': False}})
        assert edited[0] == 200, edited[1]
        assert 'Disabled. Not connected.' not in edited[1]['detail']
        assert 'live memory process stays running' in edited[1]['detail']
        assert load_config(tmp_path).mcp_servers['x'].enabled is False
        assert load_config(tmp_path).mcp_servers['x'].session_tools is False
        _assert_same_processes(log, pids)

        turned_on = call(server, 'POST', '/api/settings', {'id': 'mcp_servers', 'action': 'edit', 'name': 'x',
                                                           'record': {'enabled': True}})
        assert turned_on[0] == 200, turned_on[1]
        assert load_config(tmp_path).mcp_servers['x'].enabled is True
        turned_row = record(turned_on[1]['settings'], 'x')
        assert turned_row['status'] == 'Memory'
        assert next(field for field in turned_row['fields'] if field['key'] == 'enabled')['inert'] is True
        _assert_same_processes(log, pids)
    finally:
        CATALOG.close_all()
        stop(server, thread)


def test_plugin_timeout_note_reads_the_dataclass_default(tmp_path):
    from tessera.config import Config, McpServerConfig
    from tessera.settings_model import project

    config = Config(home=tmp_path, cwd=tmp_path)
    config.mcp_servers['x'] = McpServerConfig(
        name='x', command=['echo'], timeout=McpServerConfig.timeout, list_ttl=McpServerConfig.list_ttl)
    row = record(project(config, {}), 'x')
    assert 'timeout' not in row['note']
    assert 'tool list cached' not in row['note']
    config.mcp_servers['x'] = McpServerConfig(
        name='x', command=['echo'], timeout=McpServerConfig.timeout + 5,
        list_ttl=McpServerConfig.list_ttl + 5)
    noted = record(project(config, {}), 'x')['note']
    assert f'timeout {_plain(McpServerConfig.timeout + 5)}s' in noted
    assert f'tool list cached for {_plain(McpServerConfig.list_ttl + 5)}s' in noted


def _plain(value):
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)
