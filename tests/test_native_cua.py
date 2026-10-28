"""Native computer use speaks JSON to a helper. The fake helper is not a Mac."""

import base64
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest

from fake_backend import FakeBackend
from tessera.config import Config, load_config
from tessera.cua import MacHelper, register_cua_tools
from tessera.host_cua import tiny_png
from tessera.loop import Session
from tessera.mcp_servers import computer_backend


ROOT = Path(__file__).resolve().parents[1]
HELPER = Path(__file__).with_name('fake_native_cua.py')
SOURCE = ROOT / 'tessera' / 'native' / 'mac' / 'TesseraBackground.swift'
ALLOWED = {
    'id', 'op', 'app', 'element', 'x', 'y', 'to_element', 'to_x', 'to_y', 'dx', 'dy',
    'value', 'key', 'text', 'max_px', 'button',
}
TOOL_NAMES = (
    'list_apps', 'get_app_state', 'click', 'perform_secondary_action', 'set_value',
    'select_text', 'scroll', 'drag', 'press_key', 'type_text',
)


def base_config(tmp_path, helper):
    config = Config(home=tmp_path, cwd=tmp_path)
    config.tools.approval = 'never'
    config.jev.enabled = False
    config.cua.backend = 'auto'
    config.skills.enabled = False
    config.computer_use.provider = 'native'
    config.computer_use.helper = str(helper)
    config.computer_use.timeout = 5
    return config


def executable(path):
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def logged(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]


def test_loader_accepts_native_and_expands_the_helper_path(tmp_path, monkeypatch):
    monkeypatch.setenv('CUA_HELPER_NAME', 'TesseraCUA')
    monkeypatch.delenv('UNSET_CUA_HELPER', raising=False)
    (tmp_path / 'config.toml').write_text(
        '[computer_use]\n'
        'provider = "native"\n'
        'helper = "~/cua/${CUA_HELPER_NAME}.app"\n'
        'timeout = 12\n',
        encoding='utf-8')
    config = load_config(tmp_path)
    assert config.computer_use.provider == 'native'
    assert config.computer_use.helper == str(Path('~/cua/TesseraCUA.app').expanduser())
    assert config.computer_use.timeout == 12
    config.validate()
    (tmp_path / 'config.toml').write_text(
        '[computer_use]\n'
        'provider = "native"\n'
        'helper = "${UNSET_CUA_HELPER}/TesseraCUA.app"\n',
        encoding='utf-8')
    literal = load_config(tmp_path)
    assert literal.computer_use.helper == '${UNSET_CUA_HELPER}/TesseraCUA.app'
    broken = Config(home=tmp_path, cwd=tmp_path)
    broken.computer_use.provider = 'codex'
    with pytest.raises(ValueError, match='native'):
        broken.validate()


def test_backend_choice_native_is_the_mac_default_and_linux_keeps_its_tools(tmp_path, monkeypatch):
    linux = Config(home=tmp_path, cwd=tmp_path)
    linux.cua.backend = 'auto'
    # The name says "linux", but the assertion only means anything on a Linux host.
    # computer_backend() reads sys.platform, so on this Mac it correctly answers
    # 'native'. Pin the platform instead of skipping: the mapping is what is under
    # test, and it can be checked from any host.
    monkeypatch.setattr(sys, 'platform', 'linux')
    assert computer_backend(linux) == 'linux'
    monkeypatch.setattr(sys, 'platform', 'darwin')
    assert computer_backend(linux) == 'native'
    pinned = Config(home=tmp_path, cwd=tmp_path)
    pinned.cua.backend = 'mac'
    pinned.computer_use.provider = 'native'
    assert computer_backend(pinned) == 'native'
    fake = Config(home=tmp_path, cwd=tmp_path)
    fake.cua.backend = 'fake'
    assert computer_backend(fake) == 'fake'


def test_native_tools_on_every_route_pass_the_window_image_and_cap_the_tree(tmp_path):
    executable(HELPER)
    log_path = tmp_path / 'helper.log'
    wrapper = tmp_path / 'helper'
    wrapper.write_text(f'#!/bin/sh\nexec {sys.executable} {HELPER} {log_path} "$@"\n', encoding='utf-8')
    executable(wrapper)
    config = base_config(tmp_path, wrapper)
    config.computer_use.text_chars = 500
    config.cua.mac_enabled = True
    backend = FakeBackend([
        'Hello.',
        {'tool_calls': [{'id': 'shot', 'name': 'computer_get_app_state', 'arguments': {'app': 'LongTree'}}]},
        'Saw the window.',
        'Done.',
    ])
    with Session(config, backend=backend) as session:
        assert computer_backend(config) == 'native'
        assert session.tools.host_cua is None
        names = {tool['function']['name'] for tool in session.tools.schemas()}
        assert {f'computer_{name}' for name in TOOL_NAMES} <= names
        # Both capture shapes exist. The native helper keeps one-app capture in
        # get_app_state AND offers a whole-desktop screenshot, so this name is
        # expected now. The older Mac-only names stay out of the native surface.
        assert 'computer_screenshot' in names
        for hidden in ('computer_mouse', 'computer_type', 'computer_key',
                       'computer_windows', 'computer_focus', 'mac_look', 'mac_act'):
            assert hidden not in names
        assert session.tools._tools['computer_list_apps'].risky is False
        assert session.tools._tools['computer_get_app_state'].risky is False
        assert session.tools._tools['computer_click'].risky is True
        assert session.tools._tools['computer_type_text'].risky is True
        state = session.tools._tools['computer_get_app_state']
        assert 'accessibility' in state.description
        assert state.parameters['additionalProperties'] is False
        register_cua_tools(session.tools, config)
        assert 'mac_look' not in {tool['function']['name'] for tool in session.tools.schemas()}
        assert 'TesseraCUA background computer use' in session.system_prompt
        assert session.router.intake('How is the afternoon going?')['route'] == 'direct'
        session.run_turn('How is the afternoon going?')
        direct = {tool['function']['name']: tool for tool in backend.calls[0]['tools']}
        full = {tool['function']['name']: tool for tool in session.tools.schemas()}
        assert set(direct) == set(full)
        for name, tool in full.items():
            assert direct[name]['function']['parameters'] == tool['function']['parameters']
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
    requests = logged(log_path)
    assert [item['op'] for item in requests] == ['get_app_state']
    assert requests[0]['app'] == 'LongTree'
    assert requests[0]['max_px'] == config.cua.max_image_px
    assert set(requests[0]) <= ALLOWED
    pid = int(Path(str(log_path) + '.pid').read_text(encoding='utf-8'))
    assert not Path(f'/proc/{pid}').exists()


def test_every_action_is_posted_as_json_and_approval_gates_the_risky_ones(tmp_path):
    executable(HELPER)
    log_path = tmp_path / 'actions.log'
    wrapper = tmp_path / 'helper'
    wrapper.write_text(f'#!/bin/sh\nexec {sys.executable} {HELPER} {log_path}\n', encoding='utf-8')
    executable(wrapper)
    config = base_config(tmp_path, wrapper)
    config.tools.approval = 'ask'
    with Session(config, backend=FakeBackend([])) as session:
        listed = session.tools.execute('computer_list_apps', {})
        assert 'TextEdit pid=42 com.apple.TextEdit' in listed
        assert 'Notes pid=43' in listed
        with pytest.raises(PermissionError):
            session.tools.execute('computer_click', {'element': 3})
        session.tools.approval = 'yolo'
        with pytest.raises(ValueError, match='element'):
            session.tools.execute('computer_click', {})
        assert session.tools.execute('computer_click', {'element': 3, 'x': 4, 'y': 5}) == 'click ok'
        assert session.tools.execute('computer_perform_secondary_action', {'element': 1}) == 'perform_secondary_action ok'
        assert session.tools.execute('computer_set_value', {'element': 2, 'value': 'hello'}) == 'set_value ok'
        assert session.tools.execute('computer_select_text', {'element': 2}) == 'select_text ok'
        assert session.tools.execute('computer_scroll', {'dy': 3}) == 'scroll ok'
        assert session.tools.execute('computer_drag', {'x': 1, 'y': 2, 'to_x': 8, 'to_y': 9}) == 'drag ok'
        assert session.tools.execute('computer_press_key', {'key': 'cmd+a', 'app': 'TextEdit'}) == 'press_key ok'
        assert session.tools.execute('computer_type_text', {'text': 'hi'}) == 'type_text ok'
        state = session.tools.execute('computer_get_app_state', {'app': 'TextEdit'})
        assert state['image']['data_url'].startswith('data:image/png;base64,')
        assert '0 AXWindow TextEdit' in state['text']
    requests = logged(log_path)
    ops = [item['op'] for item in requests]
    assert ops == ['list_apps', 'click', 'perform_secondary_action', 'set_value', 'select_text',
                   'scroll', 'drag', 'press_key', 'type_text', 'get_app_state']
    for item in requests:
        assert set(item) <= ALLOWED
    click = requests[1]
    assert click['element'] == 3 and click['x'] == 4 and click['y'] == 5
    assert requests[6]['to_x'] == 8 and requests[7]['key'] == 'cmd+a' and requests[7]['app'] == 'TextEdit'
    denied = session_permission(tmp_path, wrapper, 'Denied')
    assert denied.startswith('unavailable:') and 'Accessibility' in denied and 'TesseraCUA' in denied
    assert 'System Settings' in denied
    quiet = session_permission(tmp_path, wrapper, 'NoCapture')
    assert 'Screen Recording' in quiet and '0 AXWindow partial' in quiet


def session_permission(tmp_path, wrapper, app):
    home = tmp_path / app
    home.mkdir()
    config = base_config(home, wrapper)
    with Session(config, backend=FakeBackend([])) as session:
        return session.tools.execute('computer_get_app_state', {'app': app})


def test_a_missing_helper_names_the_build_and_a_hung_helper_times_out(tmp_path):
    config = base_config(tmp_path, tmp_path / 'missing-helper')
    with Session(config, backend=FakeBackend([])) as session:
        message = session.tools.execute('computer_list_apps', {})
    assert message.startswith('unavailable:')
    assert 'macOS' in message and 'bash scripts/build-cua-helper.sh' in message
    script = tmp_path / 'sleep-helper'
    script.write_text('#!/bin/sh\nsleep 30\n', encoding='utf-8')
    executable(script)
    slow = base_config(tmp_path, script)
    slow.computer_use.timeout = 0.4
    with Session(slow, backend=FakeBackend([])) as session:
        timed = session.tools.execute('computer_list_apps', {})
    assert 'timed out' in timed


def test_linux_prompt_stays_on_the_local_desktop_tools(tmp_path, monkeypatch):
    # The session builds its tool registry from sys.platform. On this Mac that
    # registry is the native helper's, which has no computer_screenshot. The test
    # is about what a Linux host offers, so pin the platform rather than skip it.
    monkeypatch.setattr(sys, 'platform', 'linux')
    config = Config(home=tmp_path, cwd=tmp_path)
    config.tools.approval = 'never'
    config.jev.enabled = False
    config.skills.enabled = False
    with Session(config, backend=FakeBackend([])) as session:
        names = {tool['function']['name'] for tool in session.tools.schemas()}
        assert 'computer_screenshot' in names and 'computer_list_apps' not in names
        assert 'TesseraCUA background' not in session.system_prompt


def test_background_helper_source_posts_to_the_pid(tmp_path):
    source = SOURCE.read_text(encoding='utf-8')
    for forbidden in ('cghidEventTap', 'CGWarpMouseCursorPosition', '.activate(', 'AXRaise', 'post(tap:',
                      'CGRequestScreenCaptureAccess', 'kAXTrustedCheckOptionPrompt',
                      'CGWindowListCreateImage', 'CGWindowListCreateImageFromArray',
                      'CGDisplayCreateImage', 'CGDisplayStreamCreate', 'CGDisplayStreamUpdateGetRects'):
        assert forbidden not in source
    for required in ('postToPid', 'kAXPressAction', 'kAXShowMenuAction', 'kAXValueAttribute',
                     'kAXSelectedTextRangeAttribute', 'SCShareableContent', 'excludingDesktopWindows',
                     'desktopIndependentWindow', 'SCScreenshotManager', 'captureImage',
                     'CGPreflightScreenCaptureAccess', 'AXIsProcessTrusted', 'kAXSubroleAttribute',
                     'kAXDescriptionAttribute', 'kAXHelpAttribute', 'Screen Recording',
                     'Accessibility', 'System Settings', 'wheel(-dy)', 'screen.height -', 'dispatchMain'):
        assert required in source
    legacy = (ROOT / 'tessera' / 'native' / 'mac' / 'TesseraMacLook.swift').read_text(encoding='utf-8')
    assert 'cghidEventTap' in legacy
    assert MacHelper.source(MacHelper.__new__(MacHelper)).name == 'TesseraMacLook.swift'
    script = (ROOT / 'scripts' / 'build-cua-helper.sh').read_text(encoding='utf-8')
    assert 'TesseraBackground.swift' in script and 'TesseraMacLook' not in script
    assert '-target' in script and 'apple-macos' in script and '14.0' in script
    assert 'ScreenCaptureKit' in script
    subprocess.run(['bash', '-n', str(ROOT / 'scripts' / 'build-cua-helper.sh')], check=True)
    env = os.environ.copy()
    env.pop('TESSERA_HOME', None)
    env.pop('TESSERA_CUA_APP', None)
    completed = subprocess.run(['sh', str(ROOT / 'scripts' / 'build-cua-helper.sh')], capture_output=True, text=True, env=env)
    assert completed.returncode == 2 and 'usage' in completed.stderr
