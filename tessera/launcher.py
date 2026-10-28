"""OS launchers for Tessera: Applications, app menu, start menu, one command.

`install` writes the platform launcher whose command is `tessera start`.
Linux gets a .desktop entry (app menu, then whatever launcher bar reads it);
macOS gets a .app bundle in /Applications; Windows gets a Start Menu shortcut.
Every branch is idempotent: writing an existing launcher replaces it with the
current interpreter and home, so a reinstall never leaves a stale pointer.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

NAME = 'Tessera'


def _start_command(home, python=None):
    """The launcher's Exec line: this interpreter, `tessera --home <home> start`.

    The interpreter is kept as-is, without resolving symlinks: a venv's
    python is a symlink to the base interpreter, and resolving it drops the
    environment's site-packages, so the launcher would fail with
    'No module named tessera'.
    """
    python = python or sys.executable
    home = Path(home).expanduser()
    return f'"{python}" -m tessera --home "{home}" start'


def _icon_source():
    """The packaged icon, in a checkout or an installed wheel."""
    from pathlib import Path
    from .assets import asset_path
    for candidate in ('brand/icon-512.png', 'brand/icon-128.png', 'brand/icon-32.png',
                      'web/brand/icon-512.png', 'web/brand/icon-128.png', 'web/brand/icon-32.png'):
        path = asset_path(candidate)
        if path.is_file():
            return path
    return None


def _linux_install(home):
    applications = Path(os.environ.get('HOME', '~')).expanduser() / '.local' / 'share' / 'applications'
    applications.mkdir(parents=True, exist_ok=True)
    icon_dir = applications / 'icons'
    icon_dir.mkdir(exist_ok=True)
    icon = _icon_source()
    icon_path = None
    if icon is not None:
        icon_path = icon_dir / 'tessera.png'
        shutil.copyfile(icon, icon_path)
    entry = applications / 'tessera.desktop'
    lines = [
        '[Desktop Entry]',
        'Type=Application',
        f'Name={NAME}',
        'Comment=Tessera agent harness with built-in AetherMind memory',
        f'Exec={_start_command(home)}',
        'Terminal=false',
        'Categories=Utility;',
        'StartupWMClass=Tessera',
    ]
    if icon_path is not None:
        lines.append(f'Icon={icon_path}')
    entry.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    try:
        subprocess.run(['update-desktop-database', str(applications)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, FileNotFoundError):
        pass
    return {'platform': 'linux', 'entry': str(entry), 'icon': str(icon_path) if icon_path else None}


def _mac_install(home):
    applications = Path('/Applications')
    bundle = applications / f'{NAME}.app'
    contents = bundle / 'Contents'
    (contents / 'MacOS').mkdir(parents=True, exist_ok=True)
    (contents / 'Resources').mkdir(parents=True, exist_ok=True)
    script = contents / 'MacOS' / NAME
    script.write_text(
        '#!/bin/sh\n'
        f'exec {_start_command(home)}\n', encoding='utf-8')
    os.chmod(script, 0o755)
    icon = _icon_source()
    icon_path = None
    if icon is not None:
        icon_path = contents / 'Resources' / 'tessera.icns'
        # The bundle ships PNG icons; macOS accepts them as the bundle icon
        # when named AppIcon.png under Resources.
        icon_path = contents / 'Resources' / 'AppIcon.png'
        shutil.copyfile(icon, icon_path)
    info = contents / 'Info.plist'
    info.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n<dict>\n'
        '  <key>CFBundleName</key><string>Tessera</string>\n'
        '  <key>CFBundleDisplayName</key><string>Tessera</string>\n'
        '  <key>CFBundleIdentifier</key><string>com.bandoly.tessera</string>\n'
        '  <key>CFBundleExecutable</key><string>Tessera</string>\n'
        '  <key>CFBundlePackageType</key><string>APPL</string>\n'
        '  <key>CFBundleIconFile</key><string>AppIcon</string>\n'
        '</dict>\n</plist>\n', encoding='utf-8')
    return {'platform': 'mac', 'entry': str(bundle), 'icon': str(icon_path) if icon_path else None}


def _windows_install(home):
    profile = Path(os.environ.get('USERPROFILE', '~')).expanduser()
    menu = profile / 'AppData' / 'Roaming' / 'Microsoft' / 'Windows' / 'Start Menu' / 'Programs'
    menu.mkdir(parents=True, exist_ok=True)
    shortcut = menu / 'Tessera.lnk'
    ps = (
        "$ws = New-Object -ComObject WScript.Shell; "
        f"$s = $ws.CreateShortcut('{shortcut}'); "
        f"$s.TargetPath = '{sys.executable}'; "
        f"$s.Arguments = '-m tessera --home \"{home}\" start'; "
        "$s.Save()"
    )
    subprocess.run(['powershell', '-NoProfile', '-Command', ps], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return {'platform': 'windows', 'entry': str(shortcut), 'icon': None}


def install(home):
    """Write this platform's launcher. Returns what was written."""
    platform = sys.platform
    if platform.startswith('win'):
        return _windows_install(home)
    if platform == 'darwin':
        return _mac_install(home)
    return _linux_install(home)
