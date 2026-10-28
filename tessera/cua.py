"""Legacy Mac look/act and remote desks.

Background computer use on this Mac is the native helper (native_cua.py), built by
scripts/build-cua-helper.sh into TesseraCUA.app. mac_look, mac_act, and mac_permissions
use a separate TesseraMacLook.app, launched with open, and only when cua.mac_enabled
is set and the computer-use backend is the older mac backend. Remote desks use SSH:
a GNOME screenshot and a stdlib uinput helper (native/desk_input.py). Acting tools
are risky and follow the approval policy.
"""

import base64
import re
import hashlib
import json
from pathlib import Path
import plistlib
import shlex
import struct
import subprocess
import sys
import tempfile

from .remote import run_remote, ssh_argv
from .tools import object_schema

NATIVE = Path(__file__).with_name('native')
ACTION_TYPES = ['move', 'click', 'double_click', 'drag', 'scroll', 'key', 'type', 'wait']
ACTION_SCHEMA = {'type': 'array', 'items': {'type': 'object', 'properties': {
    'type': {'type': 'string', 'enum': ACTION_TYPES}, 'x': {'type': 'number'}, 'y': {'type': 'number'},
    'to_x': {'type': 'number'}, 'to_y': {'type': 'number'}, 'button': {'type': 'string', 'enum': ['left', 'right', 'middle']},
    'dx': {'type': 'number'}, 'dy': {'type': 'number'}, 'keys': {'type': 'string'}, 'text': {'type': 'string'},
    'seconds': {'type': 'number'}}, 'required': ['type']}}
STATUS_SCRIPT = r'''export XDG_RUNTIME_DIR=/run/user/$(id -u) DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$(id -u)/bus
s=$(loginctl list-sessions --no-legend | awk -v u="$(id -un)" '$3==u && $4=="seat0"{print $1; exit}')
echo "session=${s:-none} $( [ -n "$s" ] && loginctl show-session "$s" -p Type -p State --value | tr '\n' ' ')"
echo "idle_ms=$(gdbus call --session -d org.gnome.Mutter.IdleMonitor -o /org/gnome/Mutter/IdleMonitor/Core -m org.gnome.Mutter.IdleMonitor.GetIdletime 2>/dev/null)"
echo "open=$(systemctl --user list-units --type=scope --state=running --no-legend 'app-*' 2>/dev/null | grep -v gnome-session-binary | sed -E 's/^ *app-(gnome-)?//; s/-[0-9]+\.scope.*//' | tr '\n' ',' )"
echo "uptime=$(uptime -p)"
echo "load=$(cut -d' ' -f1-3 /proc/loadavg)"
'''


def idle_seconds(reply):
    # gdbus prints "(uint64 12345,)"; stripping non-digits would glue the type's 64 onto the value.
    match = re.search(r'uint64\s+(\d+)', reply)
    return int(match.group(1)) // 1000 if match else None


def png_size(data):
    if data[:8] != b'\x89PNG\r\n\x1a\n':
        raise ValueError('Not a PNG image')
    return struct.unpack('>II', data[16:24])


def shrink(path, max_px, sips):
    data = Path(path).read_bytes()
    width, height = png_size(data)
    if max(width, height) > max_px and sips and Path(sips).exists():
        subprocess.run([sips, '-Z', str(max_px), str(path)], check=True, capture_output=True, timeout=30)
        data = Path(path).read_bytes()
        width, height = png_size(data)
    return data, width, height


def image_result(data, width, height, caption, **extra):
    url = 'data:image/png;base64,' + base64.b64encode(data).decode('ascii')
    return {'width': width, 'height': height, 'coordinates': f'pixels in this {width}x{height} image',
            **extra, 'image': {'caption': caption, 'data_url': url}}


class MacHelper:
    def __init__(self, config):
        self.config = config
        self.settings = config.cua
        app = Path(self.settings.mac_helper_app).expanduser()
        self.app = app if app.is_absolute() else Path(config.home) / app
        self.last_size = None

    def source(self):
        return NATIVE / 'mac' / 'TesseraMacLook.swift'

    def ensure_built(self):
        source = self.source().read_bytes()
        stamp = hashlib.sha256(source + self.settings.codesign_identity.encode() + self.settings.mac_bundle_id.encode()).hexdigest()
        marker = self.app / 'Contents' / 'Resources' / 'source.sha256'
        if marker.exists() and marker.read_text().strip() == stamp:
            return False
        contents = self.app / 'Contents'
        (contents / 'MacOS').mkdir(parents=True, exist_ok=True)
        (contents / 'Resources').mkdir(parents=True, exist_ok=True)
        subprocess.run([self.settings.swiftc, '-O', '-o', str(contents / 'MacOS' / 'TesseraMacLook'), str(self.source())],
                       check=True, capture_output=True, timeout=600)
        info = {'CFBundleIdentifier': self.settings.mac_bundle_id, 'CFBundleName': 'TesseraMacLook',
                'CFBundleExecutable': 'TesseraMacLook', 'CFBundlePackageType': 'APPL', 'CFBundleVersion': stamp[:12],
                'CFBundleShortVersionString': '1.0', 'LSUIElement': True,
                'NSAppleEventsUsageDescription': 'Tessera computer use controls apps on this Mac when you allow it.'}
        with (contents / 'Info.plist').open('wb') as stream:
            plistlib.dump(info, stream)
        subprocess.run(['/usr/bin/codesign', '--force', '--sign', self.settings.codesign_identity, str(self.app)],
                       check=True, capture_output=True, timeout=120)
        marker.write_text(stamp)
        return True

    def run(self, *arguments):
        if sys.platform != 'darwin':
            raise RuntimeError('Mac computer use needs macOS')
        self.ensure_built()
        with tempfile.TemporaryDirectory(prefix='tessera-cua-') as directory:
            output = Path(directory) / 'result.json'
            completed = subprocess.run(['/usr/bin/open', '-W', '-n', '-g', '-a', str(self.app), '--args', str(output),
                                        *arguments], capture_output=True, text=True, timeout=self.settings.timeout)
            if not output.exists():
                raise RuntimeError('TesseraMacLook did not answer: ' + (completed.stderr or completed.stdout).strip()[-400:])
            result = json.loads(output.read_text())
        if result.get('error'):
            raise RuntimeError(result['error'])
        return result

    def look(self):
        with tempfile.TemporaryDirectory(prefix='tessera-look-') as directory:
            path = Path(directory) / 'screen.png'
            result = self.run('screenshot', str(path))
            if not result['screen_recording']:
                raise PermissionError(f'Screen Recording is not granted to TesseraMacLook ({self.app}). '
                                      'System Settings > Privacy & Security > Screen & System Audio Recording.')
            if result.get('exit_code') or not path.exists():
                raise RuntimeError(f'screencapture failed (exit {result.get("exit_code")})')
            data, width, height = shrink(path, self.settings.max_image_px, self.settings.sips)
        self.last_size = (width, height)
        return image_result(data, width, height, f"The host Mac screen ({width}x{height})",
                            operator_idle_seconds=round(result['idle_seconds']))

    def act(self, actions):
        if self.last_size is None:
            self.look()
        width, height = self.last_size
        return self.run('act', json.dumps({'width': width, 'height': height, 'actions': actions}))


class Desk:
    def __init__(self, name, remote, config):
        self.name = name
        self.remote = remote
        self.config = config
        self.last_size = None

    def look(self):
        argv = ssh_argv(self.remote, self.config.home) + [self.remote.desk_screenshot_command]
        completed = subprocess.run(argv, capture_output=True, timeout=self.config.cua.timeout, stdin=subprocess.DEVNULL)
        if completed.returncode != 0 or not completed.stdout.startswith(b'\x89PNG'):
            raise RuntimeError(f'{self.name} screenshot failed (exit {completed.returncode}): '
                               + completed.stderr.decode('utf-8', 'replace')[-400:])
        with tempfile.TemporaryDirectory(prefix='tessera-desk-') as directory:
            path = Path(directory) / 'desk.png'
            path.write_bytes(completed.stdout)
            data, width, height = shrink(path, self.config.cua.max_image_px, self.config.cua.sips)
        native = png_size(completed.stdout)
        self.last_size = native
        return image_result(data, width, height, f'Your desk, {self.name} ({width}x{height})',
                            screen=f'{native[0]}x{native[1]}')

    def act(self, actions):
        if self.last_size is None:
            self.look()
        width, height = self.last_size
        request = json.dumps({'width': width, 'height': height, 'actions': actions})
        script = (NATIVE / 'desk_input.py').read_text(encoding='utf-8')
        result = run_remote(self.remote, self.config.home, self.remote.desk_input_command + ' ' + shlex.quote(request),
                            self.config.cua.timeout, 4000, stdin_text=script)
        if result['exit_code'] != 0 or result['timed_out']:
            raise RuntimeError(f'{self.name} input failed: ' + result['output'][-600:])
        return json.loads(result['output'].strip().splitlines()[-1])

    def status(self):
        result = run_remote(self.remote, self.config.home, STATUS_SCRIPT, min(20.0, self.config.cua.timeout), 4000)
        if result['exit_code'] != 0 or result['timed_out']:
            return {'up': False, 'detail': result['output'][-300:]}
        fields = {}
        for line in result['output'].splitlines():
            if '=' in line:
                key, value = line.split('=', 1)
                fields[key] = value.strip()
        return {'up': True, 'session': fields.get('session', ''), 'idle_seconds': idle_seconds(fields.get('idle_ms', '')),
                'open': [item for item in fields.get('open', '').split(',') if item], 'uptime': fields.get('uptime', ''),
                'load': fields.get('load', '')}


def desks(config):
    return {name: Desk(name, remote, config) for name, remote in sorted(config.remotes.items())
            if remote.desk}


_mac = {}


def mac_helper(config):
    key = id(config)
    if key not in _mac:
        _mac[key] = MacHelper(config)
    return _mac[key]


def register_cua_tools(registry, config):
    from .mcp_servers import computer_backend
    # The MCP provider is the only computer-use implementation when it is selected.
    if config.cua.mac_enabled and computer_backend(config) not in ('mcp', 'native'):
        mac = mac_helper(config)
        registry.register('mac_look', "Take a screenshot of the host Mac screen to see what the operator sees. "
                          "You receive the image.",
                          object_schema({}), mac.look)
        registry.register('mac_act', "Use mouse and keyboard on the host Mac. Coordinates are pixels in your latest "
                          'mac_look image. The operator may be using it; look first. Requires approval under ask.',
                          object_schema({'actions': ACTION_SCHEMA}, ['actions']), mac.act, risky=True)
        registry.register('mac_permissions', 'Report which macOS permissions TesseraMacLook has (Screen Recording, '
                          'Accessibility, Full Disk Access) and how long the operator has been idle.',
                          object_schema({}), lambda: mac.run('status'))
    available = desks(config)
    if not available:
        return
    names = sorted(available)
    desk_enum = {'type': 'string', 'enum': names}

    def pick(desk):
        return available[desk or names[0]]

    listing = ', '.join(f'{name} ({config.remotes[name].description or config.remotes[name].host})' for name in names)
    registry.register('desk_look', f'Look at your own desk computer, the one in your office: {listing}. It is yours to '
                      'use whenever you choose. Returns a screenshot image.',
                      object_schema({'desk': desk_enum}), lambda desk=None: pick(desk).look())
    registry.register('desk_act', 'Use mouse and keyboard on your desk computer. Coordinates are pixels in your latest '
                      'desk_look image. Actions: move, click, double_click, drag, scroll, key (e.g. ctrl+l), type, wait. '
                      'Requires approval under ask.',
                      object_schema({'desk': desk_enum, 'actions': ACTION_SCHEMA}, ['actions']),
                      lambda actions, desk=None: pick(desk).act(actions), risky=True)
    registry.register('desk_status', 'Glance at your desk computer: up, idle time, open apps, uptime.',
                      object_schema({'desk': desk_enum}), lambda desk=None: pick(desk).status())
