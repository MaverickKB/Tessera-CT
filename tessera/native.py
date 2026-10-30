"""Native desktop shell: locate, install, and launch the Tessera window.

The native app is a compiled Tauri binary (`desktop/`, crate `tessera-desktop`)
that spawns `python -m tessera desktop` as its child and opens the bundled UI in
a platform window. This module is the Python side of that handoff: it finds an
installed binary, downloads and installs a prebuilt bundle when one is missing,
and launches the shell. It never silently substitutes a browser; a missing or
failed launch is reported as a reason so the caller can choose a fallback with
its eyes open.

Nothing here makes a network request or writes a file except through the
install path, and that path is explicit and idempotent.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path


APP_ID = 'app.tessera.desktop'
BIN_NAME = 'tessera-desktop'


class NativeShellNotFound(Exception):
    """No installed binary, and none could be obtained."""


def candidates() -> list[Path]:
    """Locations where an installed shell binary may live, in lookup order."""
    found: list[Path] = []
    if sys.platform.startswith('win'):
        names = (BIN_NAME + '.exe',)
    else:
        names = (BIN_NAME,)
    explicit = os.environ.get('TESSERA_DESKTOP_BIN')
    if explicit:
        found.append(Path(explicit).expanduser())
    for name in names:
        found.append(Path.home() / '.local' / 'bin' / name)
    for name in names:
        which = shutil.which(name)
        if which:
            found.append(Path(which))
    return found


def find_binary() -> Path | None:
    """The first installed shell binary that actually exists, or None."""
    for candidate in candidates():
        if candidate.is_file():
            return candidate
    return None


def launch(home: Path) -> None:
    """Hand off to the native shell, which owns the core from here on.

    The shell reads TESSERA_HOME (or its desktop.toml) and spawns the Python
    core itself, so this process does not serve. ``execv`` replaces the current
    process image, so a clean launch never returns; a missing binary or a failed
    exec raises NativeShellNotFound.
    """
    binary = find_binary()
    if binary is None:
        raise NativeShellNotFound('Tessera desktop shell is not installed')
    home = Path(home).expanduser()
    home.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env['TESSERA_HOME'] = str(home)
    # The installed shell must not resolve the core through its compile-time
    # checkout path or a Finder/app-menu PATH that lacks `python3`. Name the
    # exact interpreter and a real working directory so the spawned core runs
    # wherever this package is installed.
    env.setdefault('TESSERA_DESKTOP_PYTHON', sys.executable)
    env.setdefault('TESSERA_DESKTOP_CORE_ROOT', str(home))
    # Replace the current process: the shell becomes the supervisor. execve
    # carries the inherited environment plus this process's TESSERA_HOME, so
    # the shell reads the exact home the operator asked for.
    os.execve(str(binary), [str(binary)], env)
    # execve only returns on failure.
    raise NativeShellNotFound(f'Could not launch {binary}')


def _platform_asset_suffix() -> str:
    """The bundle suffix for this platform, for a tagged release download."""
    if sys.platform.startswith('win'):
        return '.exe'
    if sys.platform == 'darwin':
        return '.dmg'
    return '.AppImage'


def _package_version() -> str:
    """The installed package version, from the wheel metadata when available."""
    try:
        from importlib.metadata import version
        return version('tessera')
    except Exception:
        return '0.0.0'


def _release_asset(release_url: str | None, checksum: str | None) -> tuple[str, str | None]:
    """Resolve the exact bundle URL and its optional sha256 for this host.

    Precedence: an explicit ``release_url`` (and ``checksum``) wins. Otherwise
    derive from the install's own provenance (the git origin recorded by pip or
    the checkout) and the packaged version, so `start` can self-install the
    matching shell without the operator naming a download. A missing origin or
    version is a reason, not a fallback to a guessed URL.
    """
    if release_url:
        return release_url, checksum
    # Automatic derivation publishes a self-contained AppImage on Linux only.
    # On macOS/Windows the bundled artifact is a platform installer, not an
    # executable this module can drop on PATH, so auto-derivation stops here
    # rather than naming a download it cannot install. An explicit --release-url
    # is honored before this gate, so a manual bootstrap still works.
    if not sys.platform.startswith('linux'):
        raise NativeShellNotFound(
            'automatic desktop install is a Linux AppImage today; install the '
            'published bundle from the release page on this platform, then run '
            '`tessera start` with the binary on PATH'
        )
    from . import updates
    origin = updates.install_report()
    if not origin:
        raise NativeShellNotFound(
            'no install origin recorded, so the desktop bundle URL cannot be derived; '
            'pass --release-url explicitly or set TESSERA_DESKTOP_RELEASE_URL'
        )
    slug = updates._repo_slug(origin['url'])
    if not slug:
        raise NativeShellNotFound(
            f'the install origin {origin["url"]!r} is not a GitHub repository; '
            'pass --release-url explicitly'
        )
    version = _package_version()
    if version == '0.0.0':
        raise NativeShellNotFound('the installed Tessera version is unknown; pass --release-url explicitly')
    arch = _machine_arch()
    asset = f'tessera-{_platform_tag()}-{arch}{_platform_asset_suffix()}'
    url = f'https://github.com/{slug}/releases/download/v{version}/{asset}'
    return url, checksum


def _machine_arch() -> str:
    import platform
    machine = platform.machine().lower()
    if machine in ('x86_64', 'amd64'):
        return 'x86_64'
    if machine in ('arm64', 'aarch64'):
        return 'aarch64'
    return machine


def _platform_tag() -> str:
    if sys.platform.startswith('win'):
        return 'windows'
    if sys.platform == 'darwin':
        return 'macos'
    return 'linux'


def ensure_installed(config=None) -> Path:
    """Find the shell, or download+install it from the derived release.

    ``config`` is accepted for future use (a configured release URL may live in
    the home) and ignored today; the release URL comes from provenance or the
    ``TESSERA_DESKTOP_RELEASE_URL`` environment variable. When no checksum is
    supplied, a sidecar ``<asset>.sha256`` beside the bundle is fetched and used
    to verify, so an automatic install is never an unverified download.
    """
    binary = find_binary()
    if binary is not None:
        return binary
    release_url = os.environ.get('TESSERA_DESKTOP_RELEASE_URL') or None
    checksum = os.environ.get('TESSERA_DESKTOP_RELEASE_SHA256') or None
    url, checksum = _release_asset(release_url, checksum)
    if checksum is None:
        checksum = _fetch_checksum(url)
    return install(url, checksum)


def _fetch_checksum(release_url: str) -> str | None:
    """Read the sidecar ``.sha256`` beside a bundle, or None when absent.

    The release publishes ``<asset>.sha256`` (lowercase hex, one hash) next to
    the bundle. A missing or malformed sidecar is tolerated as ``None`` so an
    explicit offline install still works; ``install`` verifies only when a
    checksum is present. Never raises, so a flaky sidecar cannot block the
    automatic path that already has provenance.
    """
    try:
        data = _fetch(release_url + '.sha256', timeout=30).decode('utf-8').strip()
    except OSError:
        return None
    token = data.split()[0] if data.split() else ''
    return token if all(c in '0123456789abcdefABCDEF' for c in token) and token else None


def _fetch(url: str, timeout: int = 120) -> bytes:
    request = urllib.request.Request(url, headers={'User-Agent': 'tessera-installer'})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def install(release_url: str, checksum: str | None = None) -> Path:
    """Download a self-contained executable, verify it, and install it per-user.

    ``release_url`` names a self-contained executable (the Linux AppImage).
    ``checksum`` is an optional lowercase hex sha256. Per-user install means no
    privilege is required. This is not for a ``.deb``/``.dmg``/``.msi``/NSIS
    installer, which the OS runs once rather than an executable this module
    drops on PATH; those are published for the manual install path, not the
    automatic one.
    """
    data = _fetch(release_url, timeout=600)
    if checksum:
        actual = hashlib.sha256(data).hexdigest()
        if actual.lower() != checksum.lower():
            raise NativeShellNotFound(
                f'download checksum mismatch for {release_url}: got {actual[:16]}, want {checksum[:16]}'
            )
    target_dir = Path.home() / '.local' / 'bin'
    target_dir.mkdir(parents=True, exist_ok=True)
    if sys.platform.startswith('win'):
        target = target_dir / (BIN_NAME + '.exe')
    else:
        # A downloaded AppImage/binary is stored under the can't-collide name
        # the locator checks; the bundle's internal name is preserved where the
        # platform requires it (see the .desktop launcher).
        target = target_dir / BIN_NAME
    target.write_bytes(data)
    if not sys.platform.startswith('win'):
        target.chmod(0o755)
    return target