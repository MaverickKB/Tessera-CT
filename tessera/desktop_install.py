"""Native desktop shell: locate, install, and launch the Tessera window.

The native app is a compiled Tauri binary (`desktop/`, crate `tessera-desktop`)
that spawns `python -m tessera desktop` as its child and opens the bundled UI in
a platform window. This module is the Python side of that handoff: it finds an
installed shell, downloads and installs a prebuilt bundle when one is missing,
and launches it. It never silently substitutes a browser: a missing or failed
install or launch is reported as a reason the caller can act on.

The download and install path is explicit, idempotent, and verified. A download
that cannot be verified against a required sha256 is refused before any bytes
are written, so an automatic install is never an unverified executable.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path


APP_ID = 'app.tessera.desktop'
BIN_NAME = 'tessera-desktop'
# A 64-hex sha256 is the only accepted digest shape; anything else is malformed
# and refused, never treated as "no checksum".
_SHA256_RE = __import__('re').compile(r'^[0-9a-fA-F]{64}$')


class NativeShellNotFound(Exception):
    """No installed binary, and none could be obtained or launched."""


def candidates() -> list[Path]:
    """Locations where an installed shell binary may live, in lookup order."""
    found: list[Path] = []
    explicit = os.environ.get('TESSERA_DESKTOP_BIN')
    if explicit:
        found.append(Path(explicit).expanduser())
    if sys.platform == 'darwin':
        # The bundle's executable inside Tessera.app, in per-user and system
        # Applications, is the canonical macOS install location.
        for base in (Path.home() / 'Applications', Path('/Applications')):
            found.append(base / 'Tessera.app' / 'Contents' / 'MacOS' / BIN_NAME)
    elif sys.platform.startswith('win'):
        found.append(Path.home() / 'AppData' / 'Local' / 'Programs' / 'Tessera' / (BIN_NAME + '.exe'))
        found.append(Path.home() / '.local' / 'bin' / (BIN_NAME + '.exe'))
    else:
        found.append(Path.home() / '.local' / 'bin' / BIN_NAME)
    which = shutil.which(BIN_NAME if not sys.platform.startswith('win') else BIN_NAME + '.exe')
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
    core itself, so this process does not serve. On POSIX and macOS this execs
    and never returns; on Windows it detaches and returns after a clean start.
    A missing binary or a failed start raises NativeShellNotFound.
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
    if sys.platform == 'darwin':
        # Launch the .app so the window is owned by LaunchServices rather than
        # this terminal process.
        app = binary.parents[2]
        try:
            os.execvp('open', ['open', str(app)])
        except OSError as error:
            raise NativeShellNotFound(f'Could not launch {app}: {error}')
    elif sys.platform.startswith('win'):
        try:
            subprocess.Popen([str(binary)], env=env, close_fds=True)
        except OSError as error:
            raise NativeShellNotFound(f'Could not launch {binary}: {error}')
    else:
        # Replace the current process: the shell becomes the supervisor. execve
        # carries the inherited environment plus this process's TESSERA_HOME, so
        # the shell reads the exact home the operator asked for.
        try:
            os.execve(str(binary), [str(binary)], env)
        except OSError as error:
            raise NativeShellNotFound(f'Could not launch {binary}: {error}')
    # Reaching here without exec'ing (Windows) is a successful detached launch.
    if sys.platform.startswith('win'):
        return


def _package_version() -> str:
    """The installed package version, from the wheel metadata when available."""
    from importlib.metadata import version
    try:
        return version('tessera')
    except Exception:
        return '0.0.0'


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


def _platform_asset_suffix() -> str:
    """The published bundle suffix for this platform, for a tag release."""
    if sys.platform.startswith('win'):
        return '.msi'      # the NSIS .exe is also published; .msi is the silent path
    if sys.platform == 'darwin':
        return '.app.tar.gz'
    return '.AppImage'


def _release_asset(release_url: str | None, checksum: str | None) -> tuple[str, str | None]:
    """Resolve the exact bundle URL and its sha256 for this host.

    Precedence: an explicit ``release_url`` (and ``checksum``) wins. Otherwise
    derive from the install's own provenance (the git origin recorded by pip or
    the checkout) and the packaged version. A missing origin or version is a
    reason, not a fallback to a guessed URL.
    """
    if release_url:
        return release_url, checksum
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
    asset = f'tessera-{_platform_tag()}-{_machine_arch()}{_platform_asset_suffix()}'
    url = f'https://github.com/{slug}/releases/download/v{version}/{asset}'
    return url, checksum


def _required_checksum(checksum: str | None) -> str:
    """Validate a sha256 digest or refuse.

    ``_SHA256_RE`` requires exactly 64 hex characters; empty, short, or
    non-hex input is malformed and raises, never downgraded to "no checksum".
    """
    if checksum and _SHA256_RE.fullmatch(checksum.strip()):
        return checksum.strip()
    raise NativeShellNotFound(
        'a valid 64-character lowercase or uppercase sha256 is required before '
        'the desktop bundle can be installed; got ' + repr(checksum)
    )


def _fetch(url: str, timeout: float) -> bytes:
    """Download bytes, normalizing every network failure to NativeShellNotFound."""
    request = urllib.request.Request(url, headers={'User-Agent': 'tessera-installer'})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise NativeShellNotFound(f'could not download {url}: {error}') from error


def _fetch_checksum(release_url: str, timeout: float) -> str:
    """Read and validate the sidecar ``.sha256`` beside a bundle, or refuse.

    A missing or malformed sidecar is a failure here: verification is mandatory,
    not best-effort. The caller decides whether that failure is recoverable.
    """
    data = _fetch(release_url + '.sha256', timeout=timeout).decode('utf-8').strip()
    return _required_checksum(data.split()[0] if data.split() else None)


def ensure_installed(config=None, timeout: float | None = None) -> Path:
    """Find the shell, or download+install it from the derived release.

    A missing shell is installed from the release published for this host, and
    the download requires a valid sha256 (from ``TESSERA_DESKTOP_RELEASE_SHA256``
    or the release's sidecar). Failure is raised as NativeShellNotFound and
    never installs or launches an unverified binary.
    """
    binary = find_binary()
    if binary is not None:
        return binary
    timeout = _timeout(config, timeout)
    release_url = os.environ.get('TESSERA_DESKTOP_RELEASE_URL') or None
    checksum = os.environ.get('TESSERA_DESKTOP_RELEASE_SHA256') or None
    url, checksum = _release_asset(release_url, checksum)
    if not checksum:
        checksum = _fetch_checksum(url, timeout)
    checksum = _required_checksum(checksum)
    return install(url, checksum, timeout=timeout)


def _timeout(config, explicit):
    """The download timeout from Config, or an explicit override.

    The default lives in Config.desktop_install_timeout, not as a literal here,
    so it is operator-tunable like every other timeout.
    """
    if explicit is not None:
        return float(explicit)
    return float(getattr(config, 'desktop_install_timeout'))


def install(release_url: str, checksum: str | None, timeout: float) -> Path:
    """Download a bundle, verify it against a required sha256, and install per-user.

    ``checksum`` is required and must be a valid 64-hex sha256; the download is
    refused (no file written) on mismatch or malformed digest. Per-user install
    needs no privilege. This is the verified automatic path; the manual
    ``.deb``/``.dmg``/NSIS installers remain on the release page for operators
    who prefer them.
    """
    checksum = _required_checksum(checksum)
    data = _fetch(release_url, timeout=timeout)
    actual = hashlib.sha256(data).hexdigest()
    if actual.lower() != checksum.lower():
        raise NativeShellNotFound(
            f'download checksum mismatch for {release_url}: got {actual[:16]}, want {checksum[:16]}'
        )
    if sys.platform == 'darwin':
        return _install_macos_app(release_url, data)
    if sys.platform.startswith('win'):
        return _install_windows(release_url, data)
    return _install_appimage(data)


def _install_appimage(data: bytes) -> Path:
    target_dir = Path.home() / '.local' / 'bin'
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / BIN_NAME
    target.write_bytes(data)
    target.chmod(0o755)
    return target


def _install_macos_app(_url: str, data: bytes) -> Path:
    """A ``.app.tar.gz`` is extracted to per-user Applications and made executable."""
    import tarfile
    import io
    apps = Path.home() / 'Applications'
    apps.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as archive:
        members = archive.getmembers()
        root = members[0].name.split('/')[0]
        if not any(m.name == root + '.app' or m.name.startswith(root + '.app/') for m in members):
            raise NativeShellNotFound('the macOS bundle did not contain a .app')
        archive.extractall(apps, filter='data')
    return apps / (root + '.app')


def _install_windows(_url: str, data: bytes) -> Path:
    """Run a downloaded ``.msi`` silently, then return the expected binary path.

    The MSI installs to the per-user Program Files location the locator checks;
    the return value is that expected path, verified by the caller on launch.
    """
    with tempfile.NamedTemporaryFile(suffix='.msi', delete=False) as handle:
        handle.write(data)
        msi = Path(handle.name)
    try:
        result = subprocess.run(
            ['msiexec', '/i', str(msi), '/qn', '/norestart'],
            capture_output=True, text=True, timeout=300,
        )
        if result.returncode != 0:
            raise NativeShellNotFound(
                f'Windows installer exited {result.returncode}: '
                + (result.stderr or result.stdout or '').strip()[:200]
            )
    finally:
        msi.unlink(missing_ok=True)
    return Path.home() / 'AppData' / 'Local' / 'Programs' / 'Tessera' / (BIN_NAME + '.exe')