"""Full coding-harness tools: files, shell, processes, fetch, and fake computer use."""

import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import sys

import pytest

from fake_backend import FakeBackend
from tessera.config import Config
from tessera.host_cua import mac_permission_message, tiny_png
from tessera.loop import Session


def session_config(tmp_path):
    config = Config(home=tmp_path, cwd=tmp_path)
    config.tools.approval = 'never'
    config.jev.enabled = False
    config.cua.backend = 'fake'
    return config


def test_every_route_offers_the_same_schemas(tmp_path):
    backend = FakeBackend(['Hello.', 'Done.'])
    with Session(session_config(tmp_path), backend=backend) as session:
        assert session.router.intake('How is the afternoon going?')['route'] == 'direct'
        session.run_turn('How is the afternoon going?')
        direct = {tool['function']['name']: tool for tool in backend.calls[0]['tools']}
        full = {tool['function']['name']: tool for tool in session.tools.schemas()}
        assert set(direct) == set(full)
        for name, tool in full.items():
            assert direct[name]['function']['parameters'] == tool['function']['parameters']
            assert direct[name]['function']['description'] == tool['function']['description']
        session.run_turn('audit this repo')
        tools = {tool['function']['name']: tool for tool in backend.calls[1]['tools']}
        assert set(tools) == set(full)


def test_file_tools_cover_ranges_patch_and_binary(tmp_path):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    outside = tmp_path / 'outside'
    config = session_config(workspace)
    config.workspace = str(workspace)
    with Session(config, backend=FakeBackend([])) as session:
        tools = session.tools
        assert 'Created' in tools.execute('mkdir', {'path': 'sub'})
        tools.execute('write_file', {'path': 'sub/note.txt', 'text': 'one\ntwo\nthree\n'})
        ranged = tools.execute('read_file', {'path': 'sub/note.txt', 'start_line': 2, 'end_line': 3})
        assert '2|two' in ranged and '3|three' in ranged and 'one' not in ranged.splitlines()[0]
        found = tools.execute('glob', {'pattern': 'sub/*.txt'})
        assert 'sub/note.txt' in found
        patch = '*** Begin Patch\n*** Update File: sub/note.txt\n@@\n-two\n+TWO\n*** Add File: sub/new.txt\n+fresh\n*** End Patch'
        assert 'Updated sub/note.txt' in tools.execute('apply_patch', {'patch': patch})
        assert 'TWO' in (workspace / 'sub' / 'note.txt').read_text()
        assert (workspace / 'sub' / 'new.txt').read_text() == 'fresh\n'
        tools.execute('copy_path', {'src': 'sub/new.txt', 'dest': 'sub/copy.txt'})
        tools.execute('move_path', {'src': 'sub/copy.txt', 'dest': 'moved.txt'})
        assert (workspace / 'moved.txt').read_text() == 'fresh\n'
        tools.execute('delete_path', {'path': 'moved.txt'})
        assert not (workspace / 'moved.txt').exists()
        target = outside / 'abs.txt'
        tools.execute('write_file', {'path': str(target), 'text': 'absolute'})
        assert target.read_text() == 'absolute'
        binary = workspace / 'blob.bin'
        binary.write_bytes(b'\xff\x00hi')
        assert tools.execute('read_file', {'path': 'blob.bin'}).startswith('unavailable:')
        decoded = base64.b64decode(tools.execute('read_bytes', {'path': 'blob.bin'}).split('\n', 1)[0])
        assert decoded == b'\xff\x00hi'
        image = workspace / 'dot.png'
        image.write_bytes(tiny_png())
        shown = tools.execute('read_image', {'path': 'dot.png'})
        assert shown['image']['data_url'].startswith('data:image/png;base64,')


def test_shell_spills_long_output_and_process_accepts_stdin(tmp_path):
    config = session_config(tmp_path)
    config.tools.clip_chars = 40
    config.tools.output_tail_chars = 20
    with Session(config, backend=FakeBackend([])) as session:
        ran = session.tools.execute('shell', {'command': 'printf probe-env', 'env': {'PROBE': '1'}})
        assert ran['exit_code'] == 0 and ran['output'] == 'probe-env'
        marker = session.tools.execute('shell', {'command': 'printf "$PROBE"', 'env': {'PROBE': 'from-env'}})
        assert marker['output'] == 'from-env'
        fed = session.tools.execute('shell', {'command': 'cat', 'stdin': 'piped\n'})
        assert fed['output'] == 'piped\n'
        long = session.tools.execute('shell', {'command': 'printf %s ' + ('x' * 80)})
        assert '[clipped]' in long['output'] and long['output_path']
        saved = open(long['output_path'], encoding='utf-8').read()
        assert 'x' * 80 in saved
        import sys
        import time
        reader = sys.executable + ' -c "import sys; sys.stdout.write(sys.stdin.readline()); sys.stdout.flush()"'
        started = session.tools.execute('process', {'action': 'start', 'command': reader})
        session.tools.execute('process', {'action': 'write', 'id': started['id'], 'stdin': 'bg-line\n'})
        polled = {}
        for _ in range(40):
            polled = session.tools.execute('process', {'action': 'poll', 'id': started['id']})
            if 'bg-line' in polled['output']:
                break
            time.sleep(0.05)
        assert 'bg-line' in polled['output']
        killed = session.tools.execute('process', {'action': 'kill', 'id': started['id']})
        assert killed['killed'] is True


def test_web_fetch_extracts_text_and_stops_at_the_cap(tmp_path):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b'<html><style>x{}</style><p>Harness page</p></html>'
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f'http://127.0.0.1:{server.server_port}/page'
        with Session(session_config(tmp_path / 'full'), backend=FakeBackend([])) as session:
            page = session.tools.execute('web_fetch', {'url': url, 'headers': {'Accept': 'text/html'}})
            assert page['status'] == 200 and page['text'] == 'Harness page' and page['truncated'] is False
        capped = session_config(tmp_path / 'capped')
        capped.tools.fetch_max_bytes = 12
        with Session(capped, backend=FakeBackend([])) as session:
            shown = session.tools.execute('web_fetch', {'url': url})
            assert shown['truncated'] is True and '<p>' not in shown['text']
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_fake_cua_screenshot_pointer_and_windows(tmp_path):
    with Session(session_config(tmp_path), backend=FakeBackend([])) as session:
        shot = session.tools.execute('computer_screenshot', {})
        assert shot['image']['data_url'].startswith('data:image/png;base64,')
        assert session.tools.execute('computer_mouse', {'action': 'click', 'x': 3, 'y': 4, 'button': 'right'}) == 'click ok'
        assert session.tools.execute('computer_mouse', {'action': 'double', 'x': 1, 'y': 1}) == 'double ok'
        assert session.tools.execute('computer_mouse', {'action': 'drag', 'x': 1, 'y': 1, 'to_x': 5, 'to_y': 6}) == 'drag ok'
        assert session.tools.execute('computer_scroll', {'x': 1, 'y': 1, 'dy': 2}) == 'scroll ok'
        assert session.tools.execute('computer_type', {'text': 'hi'}) == 'typed 2 characters'
        assert session.tools.execute('computer_key', {'keys': 'ctrl+l'}) == 'key ctrl+l'
        listed = session.tools.execute('computer_windows', {})
        assert listed['windows'][0]['title'] == 'Fake window'
        assert session.tools.execute('computer_focus', {'target': 'Fake window'}) == 'focused 1'
        names = [call[0] for call in session.tools.host_cua.calls]
        assert names == ['screenshot', 'mouse', 'mouse', 'mouse', 'scroll', 'type', 'key', 'windows', 'focus']


def test_linux_and_mac_backends_say_why_they_are_unavailable(tmp_path, monkeypatch):
    config = session_config(tmp_path)
    config.cua.backend = 'linux'
    monkeypatch.delenv('DISPLAY', raising=False)
    monkeypatch.delenv('WAYLAND_DISPLAY', raising=False)
    with Session(config, backend=FakeBackend([])) as session:
        assert session.tools.execute('computer_screenshot', {}).startswith('unavailable: no DISPLAY')
    # The mac branch refuses with 'macOS computer use needs macOS' on any host that
    # is not macOS. Run on this Mac, register_host_cua takes the native branch and
    # returns a real screenshot instead, so the assertion can only hold with the
    # platform pinned. The message is the thing under test, not the host.
    mac = session_config(tmp_path)
    mac.cua.backend = 'mac'
    monkeypatch.setattr(sys, 'platform', 'linux')
    with Session(mac, backend=FakeBackend([])) as session:
        assert 'macOS' in session.tools.execute('computer_screenshot', {})
    binary = '/usr/bin/python3'
    screen = mac_permission_message('could not create image from display', binary)
    assert 'Screen Recording' in screen and binary in screen
    assist = mac_permission_message('osascript is not allowed assistive access. (-1719)', binary)
    assert 'Accessibility' in assist and binary in assist


def test_safe_mode_asks_and_yolo_runs(tmp_path):
    config = session_config(tmp_path)
    config.tools.approval = 'ask'
    with Session(config, backend=FakeBackend([])) as session:
        with pytest.raises(PermissionError):
            session.tools.execute('delete_path', {'path': 'missing'})
        session.tools.approval = 'yolo'
        (tmp_path / 'gone.txt').write_text('x', encoding='utf-8')
        assert 'Deleted' in session.tools.execute('delete_path', {'path': 'gone.txt'})
        assert not (tmp_path / 'gone.txt').exists()
