"""Codex computer use is an MCP provider. The fake server is not the real client."""

import base64
import json
from pathlib import Path
import sys

import pytest

from fake_backend import FakeBackend
from tessera.config import Config, load_config
from tessera.cua import register_cua_tools
from tessera.host_cua import tiny_png
from tessera.loop import Session
from tessera.mcp_servers import CATALOG, computer_backend


SERVER = Path(__file__).with_name('fake_computer_use_mcp.py')
TOOL_NAMES = (
    'list_apps', 'get_app_state', 'click', 'perform_secondary_action', 'set_value',
    'select_text', 'scroll', 'drag', 'press_key', 'type_text',
)


def server(name, log_path, session_tools=False):
    from tessera.config import McpServerConfig
    return McpServerConfig(
        name=name, description='Codex computer use', session_tools=session_tools,
        command=[sys.executable, str(SERVER), str(log_path)])


def base_config(tmp_path):
    config = Config(home=tmp_path, cwd=tmp_path)
    config.tools.approval = 'never'
    config.jev.enabled = False
    config.cua.backend = 'auto'
    config.skills.enabled = False
    return config


def close_servers(config):
    for item in config.mcp_servers.values():
        CATALOG.close(item)


def test_loader_expands_tilde_and_set_variables_and_keeps_unset_ones(tmp_path, monkeypatch):
    monkeypatch.setenv('CODEX_HOME', str(tmp_path / 'codex home'))
    monkeypatch.delenv('UNSET_CODEX_HOME', raising=False)
    (tmp_path / 'config.toml').write_text(
        '[mcp_servers.codex]\n'
        'command = ["${CODEX_HOME}/SkyComputerUseClient", "mcp"]\n'
        'cwd = "~/cua-work"\n'
        'session_tools = false\n'
        '[mcp_servers.literal]\n'
        'command = ["${UNSET_CODEX_HOME}/SkyComputerUseClient", "mcp"]\n'
        '[computer_use]\n'
        'provider = "mcp:codex"\n',
        encoding='utf-8')
    config = load_config(tmp_path)
    assert config.mcp_servers['codex'].command == [str(tmp_path / 'codex home' / 'SkyComputerUseClient'), 'mcp']
    assert config.mcp_servers['codex'].cwd == str(Path('~/cua-work').expanduser())
    assert config.mcp_servers['literal'].command[0] == '${UNSET_CODEX_HOME}/SkyComputerUseClient'
    assert config.computer_use.provider == 'mcp:codex'
    close_servers(config)


def test_mcp_provider_is_one_tool_set_on_every_route_and_passes_the_image(tmp_path):
    config = base_config(tmp_path)
    config.computer_use.backend = 'mcp'
    config.computer_use.provider = 'mcp:codex'
    config.computer_use.text_chars = 500
    config.cua.mac_enabled = True
    log_path = tmp_path / 'codex.log'
    shelf_log = tmp_path / 'shelf.log'
    config.mcp_servers['codex'] = server('codex', log_path, session_tools=True)
    config.mcp_servers['shelf'] = server('shelf', shelf_log, session_tools=True)
    backend = FakeBackend([
        'Hello.',
        {'tool_calls': [{'id': 'shot', 'name': 'computer_get_app_state', 'arguments': {'app': 'TextEdit'}}]},
        'Saw TextEdit.',
        'Done.',
    ])
    try:
        with Session(config, backend=backend) as session:
            assert computer_backend(config) == 'mcp'
            assert session.tools.host_cua is None
            names = {tool['function']['name'] for tool in session.tools.schemas()}
            expected = {f'computer_{name}' for name in TOOL_NAMES}
            assert expected <= names
            for hidden in ('computer_screenshot', 'computer_mouse', 'computer_type', 'computer_key',
                           'computer_windows', 'computer_focus', 'mac_look', 'mac_act', 'mac_permissions',
                           'codex__list_apps', 'codex__get_app_state'):
                assert hidden not in names
            assert 'shelf__list_apps' in names
            assert session.tools._tools['computer_list_apps'].risky is False
            assert session.tools._tools['computer_get_app_state'].risky is False
            assert session.tools._tools['computer_click'].risky is True
            assert session.tools._tools['computer_type_text'].risky is True
            register_cua_tools(session.tools, config)
            assert 'mac_look' not in {tool['function']['name'] for tool in session.tools.schemas()}
            assert 'configured computer-use provider' in session.system_prompt
            assert session.router.intake('How is the afternoon going?')['route'] == 'direct'
            session.run_turn('How is the afternoon going?')
            direct = {tool['function']['name']: tool for tool in backend.calls[0]['tools']}
            full = {tool['function']['name']: tool for tool in session.tools.schemas()}
            assert set(direct) == set(full)
            for name, tool in full.items():
                assert direct[name]['function']['parameters'] == tool['function']['parameters']
                assert direct[name]['function']['description'] == tool['function']['description']
            state = direct['computer_get_app_state']['function']
            assert state['parameters']['properties']['app']['type'] == 'string'
            assert 'accessibility' in state['description']
            session.run_turn('Look at the TextEdit window')
            shot = backend.calls[2]
            images = [block for message in shot['messages'] if isinstance(message.get('content'), list) for block in message['content']]
            urls = [block['image_url']['url'] for block in images if block.get('type') == 'image_url']
            assert urls == ['data:image/png;base64,' + base64.b64encode(tiny_png(rgb=(1, 2, 3))).decode('ascii')]
            tool_body = next(message['content'] for message in shot['messages'] if message.get('role') == 'tool')
            assert '[clipped]' in tool_body and 'AXWindow TextEdit' in tool_body
            assert len(tool_body) < 500 + 80
            session.run_turn('audit this repo')
            tools_route = {tool['function']['name']: tool for tool in backend.calls[3]['tools']}
            assert set(tools_route) == set(direct)
            for name, tool in direct.items():
                assert tools_route[name]['function']['parameters'] == tool['function']['parameters']
        logged = [json.loads(line) for line in log_path.read_text(encoding='utf-8').splitlines()]
        methods = [item['method'] for item in logged]
        assert methods[0] == 'initialize'
        assert 'tools/list' in methods
        calls = [item for item in logged if item['method'] == 'tools/call']
        assert calls == [{'method': 'tools/call', 'params': {'name': 'get_app_state', 'arguments': {'app': 'TextEdit'}}}]
        assert all(item['method'] != 'act' for item in logged)
    finally:
        close_servers(config)


def test_read_only_computer_tools_skip_approval_and_actions_follow_it(tmp_path):
    config = base_config(tmp_path)
    config.tools.approval = 'ask'
    config.computer_use.backend = 'mcp'
    config.computer_use.provider = 'mcp:codex'
    log_path = tmp_path / 'codex.log'
    config.mcp_servers['codex'] = server('codex', log_path)
    try:
        with Session(config, backend=FakeBackend([])) as session:
            listed = session.tools.execute('computer_list_apps', {})
            assert listed == 'list_apps ok'
            with pytest.raises(PermissionError):
                session.tools.execute('computer_click', {'element': 1})
            session.tools.approval = 'yolo'
            assert session.tools.execute('computer_type_text', {'text': 'hello'}) == 'type_text ok'
        logged = [json.loads(line)['method'] + ' ' + str((json.loads(line).get('params') or {}).get('name', ''))
                  for line in log_path.read_text(encoding='utf-8').splitlines()]
        assert any(item.endswith(' list_apps') or 'list_apps' in item for item in logged)
        assert not any('click' in item and 'tools/call' in item for item in logged)
        assert any('type_text' in item for item in logged)
    finally:
        close_servers(config)


def test_linux_auto_keeps_local_computer_tools_when_a_provider_is_configured(tmp_path, monkeypatch):
    monkeypatch.delenv('DISPLAY', raising=False)
    monkeypatch.delenv('WAYLAND_DISPLAY', raising=False)
    # computer_backend() reads sys.platform. On this Mac an mcp: provider wins
    # ('mcp'); the rule under test is that Linux keeps the local desktop tools
    # even with a provider set. Pin the platform so the rule is checked here too.
    monkeypatch.setattr(sys, 'platform', 'linux')
    config = base_config(tmp_path)
    config.computer_use.provider = 'mcp:codex'
    config.mcp_servers['codex'] = server('codex', tmp_path / 'codex.log')
    assert computer_backend(config) == 'linux'
    try:
        with Session(config, backend=FakeBackend([])) as session:
            names = {tool['function']['name'] for tool in session.tools.schemas()}
            assert 'computer_screenshot' in names and 'computer_mouse' in names
            assert 'computer_list_apps' not in names and 'computer_get_app_state' not in names
            assert session.tools.execute('computer_screenshot', {}).startswith('unavailable: no DISPLAY')
        assert not (tmp_path / 'codex.log').exists()
    finally:
        close_servers(config)


def test_darwin_auto_uses_the_mcp_provider(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, 'platform', 'darwin')
    config = base_config(tmp_path)
    config.computer_use.provider = 'mcp:codex'
    log_path = tmp_path / 'codex.log'
    config.mcp_servers['codex'] = server('codex', log_path)
    assert computer_backend(config) == 'mcp'
    try:
        with Session(config, backend=FakeBackend([])) as session:
            names = {tool['function']['name'] for tool in session.tools.schemas()}
            assert 'computer_list_apps' in names and 'computer_screenshot' not in names
            assert session.tools.execute('computer_press_key', {'key': 'a'}) == 'press_key ok'
        assert 'press_key' in log_path.read_text(encoding='utf-8')
    finally:
        close_servers(config)
