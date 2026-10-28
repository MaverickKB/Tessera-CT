"""Host desktop computer use.

The backend is pluggable. ``auto`` uses macOS facilities on Darwin and the Linux
tools that are installed (xdotool, scrot, and similar). Tests use the fake backend.
Missing tools and missing macOS permissions return ``unavailable: ...`` and do not
pretend the action happened.
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .cua import image_result, png_size, shrink
from .tools import object_schema


def unavailable(reason):
    return f'unavailable: {reason}'


def tiny_png(width=1, height=1, rgb=(8, 16, 32)):
    """A valid RGB PNG with no third-party image library."""
    import struct
    import zlib

    def chunk(tag, data):
        return struct.pack('>I', len(data)) + tag + data + struct.pack('>I', zlib.crc32(tag + data) & 0xffffffff)

    raw = b''.join(b'\x00' + bytes(rgb) * width for _ in range(height))
    ihdr = struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', ihdr) + chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b'')


def mac_permission_message(text, binary):
    """Name the macOS permission a helper refused, or None when the text is some other error."""
    lowered = (text or '').lower()
    screen = ('screen recording' in lowered or 'could not create image' in lowered
              or 'cgdisplaysstream' in lowered)
    if screen:
        return (f'Screen Recording permission is not granted to {binary}. '
                'System Settings > Privacy & Security > Screen & System Audio Recording.')
    assist = ('accessibility' in lowered or 'assistive' in lowered or 'not authorized' in lowered
              or '-1743' in lowered or '-1719' in lowered or '-25211' in lowered)
    if assist:
        return (f'Accessibility permission is not granted to {binary}. '
                'System Settings > Privacy & Security > Accessibility.')
    return None


def _which(command):
    if not command:
        return None
    path = Path(command)
    if path.is_absolute():
        return str(path) if path.exists() else None
    return shutil.which(command)


def _run(argv, timeout, check_display=False):
    if check_display and not os.environ.get('DISPLAY') and not os.environ.get('WAYLAND_DISPLAY'):
        return None, unavailable('no DISPLAY or WAYLAND_DISPLAY')
    try:
        completed = subprocess.run(argv, capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        return None, unavailable(f'{argv[0]} failed ({type(error).__name__})')
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).decode('utf-8', 'replace').strip()
        return None, unavailable(detail[-400:] or f'{Path(argv[0]).name} exited {completed.returncode}')
    return completed, None


class UnavailableCua:
    """Desktop computer use is not implemented for this operating system.

    Every action returns ``unavailable`` text. It does not click, type, or
    invent a screenshot.
    """

    def __init__(self, config):
        self.config = config

    def _no(self):
        return unavailable('Desktop computer use is not supported on this operating system.')

    def screenshot(self):
        return self._no()

    def mouse(self, action, x=None, y=None, to_x=None, to_y=None, button='left'):
        return self._no()

    def scroll(self, x=None, y=None, dx=0, dy=0):
        return self._no()

    def type_text(self, text):
        return self._no()

    def key(self, keys):
        return self._no()

    def windows(self):
        return self._no()

    def focus(self, target):
        return self._no()


class FakeCua:
    """Records actions and returns a one-pixel screenshot. No display and no OS calls."""

    def __init__(self, config):
        self.config = config
        self.calls = []
        self.focused = '1'
        self._windows = [{'id': '1', 'title': 'Fake window', 'app': 'fake'}]

    def screenshot(self):
        self.calls.append(('screenshot',))
        data = tiny_png()
        width, height = png_size(data)
        return image_result(data, width, height, f'Fake screen ({width}x{height})')

    def mouse(self, action, x=None, y=None, to_x=None, to_y=None, button='left'):
        self.calls.append(('mouse', action, x, y, to_x, to_y, button))
        return f'{action} ok'

    def scroll(self, x=None, y=None, dx=0, dy=0):
        self.calls.append(('scroll', x, y, dx, dy))
        return 'scroll ok'

    def type_text(self, text):
        self.calls.append(('type', text))
        return f'typed {len(text)} characters'

    def key(self, keys):
        self.calls.append(('key', keys))
        return f'key {keys}'

    def windows(self):
        self.calls.append(('windows',))
        return {'windows': list(self._windows), 'focused': self.focused}

    def focus(self, target):
        match = next((item for item in self._windows if item['id'] == target or item['title'] == target), None)
        if match is None:
            return unavailable(f'no window {target}')
        self.focused = match['id']
        self.calls.append(('focus', target))
        return f'focused {match["id"]}'


class LinuxCua:
    def __init__(self, config):
        self.config = config
        self.settings = config.cua

    def _input(self, *args):
        binary = _which(self.settings.input_command)
        if binary is None:
            return None, unavailable(f'{self.settings.input_command} is not installed')
        return _run([binary, *args], self.settings.timeout, check_display=True)

    def screenshot(self):
        if not os.environ.get('DISPLAY') and not os.environ.get('WAYLAND_DISPLAY'):
            return unavailable('no DISPLAY or WAYLAND_DISPLAY')
        chosen = next((_which(name) for name in self.settings.screenshot_commands if _which(name)), None)
        if chosen is None:
            names = ', '.join(self.settings.screenshot_commands)
            return unavailable(f'no screenshot command is installed ({names})')
        scale = self.settings.screenshot_scale
        max_px = max(64, int(self.settings.max_image_px * scale))
        with tempfile.TemporaryDirectory(prefix='tessera-screen-') as directory:
            path = Path(directory) / 'screen.png'
            name = Path(chosen).name
            if name == 'scrot':
                argv = [chosen, '-o', str(path)]
            elif name == 'import':
                argv = [chosen, '-window', 'root', str(path)]
            else:
                argv = [chosen, '-f', str(path)]
            _, error = _run(argv, self.settings.timeout, check_display=True)
            if error:
                return error
            if not path.is_file() or path.stat().st_size < 24:
                return unavailable(f'{name} produced no image')
            data, width, height = shrink(path, max_px, '')
        return image_result(data, width, height, f'Host screen ({width}x{height})')

    def mouse(self, action, x=None, y=None, to_x=None, to_y=None, button='left'):
        button_map = {'left': '1', 'middle': '2', 'right': '3'}
        code = button_map.get(button or 'left')
        if code is None:
            return unavailable('button must be left, middle, or right')
        if action == 'move':
            _, error = self._input('mousemove', '--sync', str(int(x)), str(int(y)))
        elif action == 'click':
            _, error = self._input('mousemove', '--sync', str(int(x)), str(int(y)))
            if error:
                return error
            _, error = self._input('click', code)
        elif action == 'double':
            _, error = self._input('mousemove', '--sync', str(int(x)), str(int(y)))
            if error:
                return error
            _, error = self._input('click', '--repeat', '2', code)
        elif action == 'right':
            _, error = self._input('mousemove', '--sync', str(int(x)), str(int(y)))
            if error:
                return error
            _, error = self._input('click', '3')
        elif action == 'drag':
            _, error = self._input('mousemove', '--sync', str(int(x)), str(int(y)))
            if error:
                return error
            _, error = self._input('mousedown', code)
            if error:
                return error
            _, error = self._input('mousemove', '--sync', str(int(to_x)), str(int(to_y)))
            if error:
                return error
            _, error = self._input('mouseup', code)
        else:
            return unavailable('mouse action must be move, click, double, right, or drag')
        return error or f'{action} ok'

    def scroll(self, x=None, y=None, dx=0, dy=0):
        if x is not None and y is not None:
            _, error = self._input('mousemove', '--sync', str(int(x)), str(int(y)))
            if error:
                return error
        steps = int(dy or dx or 0)
        if steps == 0:
            return 'scroll ok'
        code = '4' if steps > 0 else '5'
        _, error = self._input('click', '--repeat', str(abs(steps)), code)
        return error or 'scroll ok'

    def type_text(self, text):
        _, error = self._input('type', '--', text)
        return error or f'typed {len(text)} characters'

    def key(self, keys):
        _, error = self._input('key', '--', keys)
        return error or f'key {keys}'

    def windows(self):
        wmctrl = _which(self.settings.window_command)
        if wmctrl:
            completed, error = _run([wmctrl, '-l'], self.settings.timeout, check_display=True)
            if error:
                return error
            rows = []
            for line in completed.stdout.decode('utf-8', 'replace').splitlines():
                parts = line.split(None, 3)
                if len(parts) >= 4:
                    rows.append({'id': parts[0], 'title': parts[3], 'app': parts[2]})
            return {'windows': rows}
        binary = _which(self.settings.input_command)
        if binary is None:
            return unavailable(f'{self.settings.input_command} is not installed')
        completed, error = _run([binary, 'search', '--onlyvisible', '--name', '.'], self.settings.timeout, check_display=True)
        if error:
            return error
        rows = []
        for window_id in completed.stdout.decode('utf-8', 'replace').split():
            name, name_error = _run([binary, 'getwindowname', window_id], self.settings.timeout, check_display=True)
            title = '' if name_error or name is None else name.stdout.decode('utf-8', 'replace').strip()
            rows.append({'id': window_id, 'title': title, 'app': ''})
        return {'windows': rows}

    def focus(self, target):
        binary = _which(self.settings.input_command)
        if binary is None:
            return unavailable(f'{self.settings.input_command} is not installed')
        _, error = _run([binary, 'windowactivate', '--sync', target], self.settings.timeout, check_display=True)
        if error is None:
            return f'focused {target}'
        # A bare display has no window manager. Focusing the X window still works.
        _, fallback = _run([binary, 'windowfocus', '--sync', target], self.settings.timeout, check_display=True)
        return fallback or f'focused {target}'


class MacCua:
    """screencapture plus osascript. Quartz is used when pyobjc is installed."""

    def __init__(self, config):
        self.config = config
        self.settings = config.cua
        self.binary = sys.executable

    def _need_mac(self):
        if sys.platform != 'darwin':
            return unavailable('macOS computer use needs macOS')
        return None

    def _osascript(self, script):
        binary = _which(self.settings.osascript)
        if binary is None:
            return None, unavailable(f'{self.settings.osascript} is not installed')
        try:
            completed = subprocess.run([binary, '-e', script], capture_output=True, text=True,
                                       timeout=self.settings.timeout, check=False)
        except (OSError, subprocess.SubprocessError) as error:
            return None, unavailable(f'osascript failed ({type(error).__name__})')
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or '').strip()
            permission = mac_permission_message(detail, self.binary)
            if permission:
                return None, unavailable(permission)
            return None, unavailable(detail[-400:] or 'osascript failed')
        return completed.stdout, None

    def _quartz(self):
        try:
            import Quartz
        except ImportError:
            return None
        return Quartz

    def screenshot(self):
        missing = self._need_mac()
        if missing:
            return missing
        quartz = self._quartz()
        preflight = getattr(quartz, 'CGPreflightScreenCaptureAccess', None) if quartz else None
        if preflight is not None and not preflight():
            return unavailable(mac_permission_message('screen recording', self.binary))
        capture = _which(self.settings.screencapture)
        if capture is None:
            return unavailable(f'{self.settings.screencapture} is not installed')
        scale = self.settings.screenshot_scale
        max_px = max(64, int(self.settings.max_image_px * scale))
        with tempfile.TemporaryDirectory(prefix='tessera-screen-') as directory:
            path = Path(directory) / 'screen.png'
            try:
                completed = subprocess.run([capture, '-x', str(path)], capture_output=True, text=True,
                                           timeout=self.settings.timeout, check=False)
            except (OSError, subprocess.SubprocessError) as error:
                return unavailable(f'screencapture failed ({type(error).__name__})')
            detail = (completed.stderr or completed.stdout or '').strip()
            permission = mac_permission_message(detail, self.binary)
            if completed.returncode != 0 or not path.is_file():
                if permission:
                    return unavailable(permission)
                return unavailable(detail[-400:] or f'screencapture exited {completed.returncode}')
            data, width, height = shrink(path, max_px, self.settings.sips)
        return image_result(data, width, height, f'Host screen ({width}x{height})')

    def _trusted(self):
        quartz = self._quartz()
        trusted = getattr(quartz, 'AXIsProcessTrusted', None) if quartz else None
        if trusted is None:
            return None
        return bool(trusted())

    def _click(self, x, y, kind):
        quartz = self._quartz()
        event = getattr(quartz, 'CGEventCreateMouseEvent', None) if quartz else None
        post = getattr(quartz, 'CGEventPost', None) if quartz else None
        if event and post:
            moved = quartz.kCGEventMouseMoved
            down = quartz.kCGEventLeftMouseDown
            up = quartz.kCGEventLeftMouseUp
            point = quartz.CGPoint(x, y)
            post(quartz.kCGHIDEventTap, event(None, moved, point, quartz.kCGMouseButtonLeft))
            clicks = 2 if kind == 'double' else 1
            button = quartz.kCGMouseButtonRight if kind == 'right' else quartz.kCGMouseButtonLeft
            if kind == 'right':
                down, up = quartz.kCGEventRightMouseDown, quartz.kCGEventRightMouseUp
            for index in range(clicks):
                post(quartz.kCGHIDEventTap, event(None, down, point, button))
                post(quartz.kCGHIDEventTap, event(None, up, point, button))
            return f'{kind} ok'
        script = f'tell application "System Events" to click at {{{int(x)}, {int(y)}}}'
        if kind == 'double':
            script = script + '\n' + script
        if kind == 'right':
            script = (f'tell application "System Events" to tell (first process whose frontmost is true) '
                      f'to perform action "AXShowMenu" of (click at {{{int(x)}, {int(y)}}})')
        _, error = self._osascript(script)
        return error or f'{kind} ok'

    def mouse(self, action, x=None, y=None, to_x=None, to_y=None, button='left'):
        missing = self._need_mac()
        if missing:
            return missing
        trusted = self._trusted()
        if trusted is False:
            return unavailable(mac_permission_message('accessibility', self.binary))
        if action == 'move':
            quartz = self._quartz()
            event = getattr(quartz, 'CGEventCreateMouseEvent', None) if quartz else None
            if event:
                point = quartz.CGPoint(x, y)
                quartz.CGEventPost(quartz.kCGHIDEventTap, event(None, quartz.kCGEventMouseMoved, point, quartz.kCGMouseButtonLeft))
                return 'move ok'
            _, error = self._osascript(f'tell application "System Events" to set position of mouse to {{{int(x)}, {int(y)}}}')
            return error or 'move ok'
        if action == 'drag':
            quartz = self._quartz()
            event = getattr(quartz, 'CGEventCreateMouseEvent', None) if quartz else None
            if not event:
                return unavailable('drag on macOS needs the optional pyobjc-framework-Quartz extra')
            down = quartz.kCGEventLeftMouseDown
            up = quartz.kCGEventLeftMouseUp
            drag = quartz.kCGEventLeftMouseDragged
            start = quartz.CGPoint(x, y)
            end = quartz.CGPoint(to_x, to_y)
            quartz.CGEventPost(quartz.kCGHIDEventTap, event(None, down, start, quartz.kCGMouseButtonLeft))
            quartz.CGEventPost(quartz.kCGHIDEventTap, event(None, drag, end, quartz.kCGMouseButtonLeft))
            quartz.CGEventPost(quartz.kCGHIDEventTap, event(None, up, end, quartz.kCGMouseButtonLeft))
            return 'drag ok'
        kind = {'click': 'click', 'double': 'double', 'right': 'right'}.get(action)
        if kind is None:
            return unavailable('mouse action must be move, click, double, right, or drag')
        if button == 'right':
            kind = 'right'
        return self._click(x, y, kind)

    def scroll(self, x=None, y=None, dx=0, dy=0):
        missing = self._need_mac()
        if missing:
            return missing
        quartz = self._quartz()
        scroll = getattr(quartz, 'CGEventCreateScrollWheelEvent', None) if quartz else None
        if scroll:
            if x is not None and y is not None:
                self.mouse('move', x=x, y=y)
            event = scroll(None, quartz.kCGScrollEventUnitPixel, 2, int(dy or 0), int(dx or 0))
            quartz.CGEventPost(quartz.kCGHIDEventTap, event)
            return 'scroll ok'
        return unavailable('scroll on macOS needs the optional pyobjc-framework-Quartz extra')

    def type_text(self, text):
        missing = self._need_mac()
        if missing:
            return missing
        escaped = text.replace('\\', '\\\\').replace('"', '\\"')
        _, error = self._osascript(f'tell application "System Events" to keystroke "{escaped}"')
        return error or f'typed {len(text)} characters'

    def key(self, keys):
        missing = self._need_mac()
        if missing:
            return missing
        parts = [part.strip() for part in keys.split('+') if part.strip()]
        if not parts:
            return unavailable('keys required')
        modifiers = {'cmd': 'command down', 'command': 'command down', 'ctrl': 'control down',
                     'control': 'control down', 'alt': 'option down', 'option': 'option down',
                     'shift': 'shift down'}
        flags = [modifiers[part.lower()] for part in parts[:-1] if part.lower() in modifiers]
        key = parts[-1].replace('"', '')
        using = ' using {' + ', '.join(flags) + '}' if flags else ''
        _, error = self._osascript(f'tell application "System Events" to keystroke "{key}"{using}')
        return error or f'key {keys}'

    def windows(self):
        missing = self._need_mac()
        if missing:
            return missing
        script = ('tell application "System Events" to get {name, unix id} of every process '
                  'whose background only is false')
        text, error = self._osascript(script)
        if error:
            return error
        return {'windows': [{'id': '', 'title': item.strip(), 'app': item.strip()}
                            for item in (text or '').split(',') if item.strip()]}

    def focus(self, target):
        missing = self._need_mac()
        if missing:
            return missing
        escaped = target.replace('\\', '\\\\').replace('"', '\\"')
        _, error = self._osascript(f'tell application "{escaped}" to activate')
        return error or f'focused {target}'


def select_backend(config):
    from .mcp_servers import computer_backend
    choice = computer_backend(config)
    if choice in ('mcp', 'native'):
        return None
    if choice == 'fake':
        return FakeCua(config)
    if choice == 'linux':
        return LinuxCua(config)
    if choice == 'mac':
        return MacCua(config)
    if choice == 'unsupported':
        return UnavailableCua(config)
    if sys.platform == 'darwin':
        return MacCua(config)
    if sys.platform.startswith('linux'):
        return LinuxCua(config)
    return UnavailableCua(config)


def register_host_cua(registry, config):
    from .mcp_servers import computer_backend, register_computer_use_tools
    choice = computer_backend(config)
    if choice == 'mcp':
        # One set: the MCP provider. Tessera's pointer tools would fight the user for the cursor.
        registry.host_cua = None
        register_computer_use_tools(registry, config, config.tools.clip_chars)
        return
    if choice == 'native':
        from .native_cua import register_native_tools
        registry.host_cua = None
        register_native_tools(registry, config)
        return
    backend = select_backend(config)
    registry.host_cua = backend
    number = {'type': 'number'}
    string = {'type': 'string'}
    registry.register('computer_screenshot', 'Take a screenshot of the host desktop. You receive the image.',
                      object_schema({}), backend.screenshot)
    registry.register('computer_mouse', 'Move or click the host pointer. action is move, click, double, right, or drag. '
                      'Coordinates are pixels in the latest computer_screenshot. Requires approval under ask.',
                      object_schema({'action': {'type': 'string', 'enum': ['move', 'click', 'double', 'right', 'drag']},
                                     'x': number, 'y': number, 'to_x': number, 'to_y': number,
                                     'button': {'type': 'string', 'enum': ['left', 'middle', 'right']}}, ['action']),
                      lambda action, x=None, y=None, to_x=None, to_y=None, button='left':
                      backend.mouse(action, x, y, to_x, to_y, button), risky=True)
    registry.register('computer_scroll', 'Scroll the host desktop at a pixel. Positive dy scrolls up. Requires approval under ask.',
                      object_schema({'x': number, 'y': number, 'dx': number, 'dy': number}),
                      lambda x=None, y=None, dx=0, dy=0: backend.scroll(x, y, dx, dy), risky=True)
    registry.register('computer_type', 'Type text into the focused host window. Requires approval under ask.',
                      object_schema({'text': string}, ['text']), backend.type_text, risky=True)
    registry.register('computer_key', 'Press a host key combo such as cmd+c or ctrl+l. Requires approval under ask.',
                      object_schema({'keys': string}, ['keys']), backend.key, risky=True)
    registry.register('computer_windows', 'List windows on the host desktop.',
                      object_schema({}), backend.windows)
    registry.register('computer_focus', 'Focus a host window or app by id or title. Requires approval under ask.',
                      object_schema({'target': string}, ['target']), backend.focus, risky=True)
