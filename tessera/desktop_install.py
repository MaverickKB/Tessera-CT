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


class NativeShellNotFound(RuntimeError):
    """No installed binary, and none could be obtained or launched."""

    def __init__(self, message, *, fuse=False):
        super().__init__(message)
        self.fuse = fuse


# Phrases the AppImage runtime prints when fusermount is missing, is FUSE 3,
# or libfuse.so.2 cannot be loaded. Other launch failures must not match.
_FUSE_MARKERS = (
    'no suitable fusermount',
    'cannot mount appimage',
    'check your fuse setup',
    'fusermount_prog',
    'failed to exec fusermount',
    'libfuse.so.2',
)


def appimage_fuse_failure(text):
    """True when an AppImage refused to mount for lack of FUSE 2."""
    lowered = (text or '').lower()
    return any(marker in lowered for marker in _FUSE_MARKERS)


def _os_release(text=None):
    if text is None:
        try:
            text = Path('/etc/os-release').read_text(encoding='utf-8')
        except OSError:
            text = ''
    info = {}
    for line in text.splitlines():
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        info[key.strip().lower()] = value.strip().strip('"').strip("'")
    return info


def _version_at_least(version, major, minor=0):
    parts = []
    for piece in str(version or '').split('.'):
        digits = ''
        for character in piece:
            if character.isdigit():
                digits += character
            else:
                break
        if not digits:
            break
        parts.append(int(digits))
    if not parts:
        return False
    while len(parts) < 2:
        parts.append(0)
    return (parts[0], parts[1]) >= (major, minor)


def fuse2_install_command(os_release_text=None):
    """The one command that installs FUSE 2 on this distro.

    Unknown distros get every known command, each on its own line, so the
    operator is not handed a guess. Tessera never runs the command.
    """
    info = _os_release(os_release_text)
    distro = info.get('id', '')
    like = set((info.get('id_like') or '').split())
    version = info.get('version_id', '')
    names = {distro} | like
    if names & {'fedora', 'rhel', 'centos', 'rocky', 'almalinux'}:
        return 'sudo dnf install fuse fuse-libs'
    if names & {'arch', 'manjaro', 'endeavouros'}:
        return 'sudo pacman -S fuse2'
    debian = names & {'debian', 'ubuntu', 'linuxmint', 'pop'}
    if debian:
        modern = (
            (distro == 'ubuntu' and _version_at_least(version, 24, 4))
            or (distro == 'debian' and _version_at_least(version, 13))
            or (distro == 'linuxmint' and _version_at_least(version, 22))
            or (distro == 'pop' and _version_at_least(version, 24, 4))
        )
        if modern:
            return 'sudo apt install fuse'
        if version:
            return 'sudo apt install libfuse2'
        return 'sudo apt install fuse\nsudo apt install libfuse2'
    return '\n'.join((
        'sudo apt install fuse',
        'sudo apt install libfuse2',
        'sudo dnf install fuse fuse-libs',
        'sudo pacman -S fuse2',
    ))


def fuse_mount_lines(extract_enabled, command):
    """Plain sentences for a missing FUSE 2 mount. No sudo is run."""
    lines = [
        'the native AppImage cannot mount because FUSE 2 is missing.',
        'fusermount on this system is FUSE 3 or is not installed, and the AppImage refuses that.',
        'Install FUSE 2, then run tessera start again:',
    ]
    for part in str(command).splitlines():
        lines.append('  ' + part)
    lines.append('Tessera will not run sudo.')
    if extract_enabled:
        lines.append('desktop_appimage_extract is on, so Tessera will extract the AppImage and run the native app without mounting.')
        lines.append('Turn it off in Settings → Update → Run the Linux app without FUSE, or set desktop_appimage_extract = false in config.toml.')
    else:
        lines.append('Extract-and-run is off. Turn it on in Settings → Update → Run the Linux app without FUSE, or set desktop_appimage_extract = true in config.toml, to open the native app without installing FUSE 2.')
    return lines


def fuse_mount_message(extract_enabled, command=None):
    return '\n'.join(fuse_mount_lines(extract_enabled, command if command is not None else fuse2_install_command()))


def fuse2_present(lookup=None, runner=None):
    """True only for a FUSE 2 fusermount. fusermount3 is not accepted."""
    which = shutil.which if lookup is None else lookup
    binary = which('fusermount')
    if not binary:
        return False
    try:
        resolved = os.path.realpath(binary)
    except OSError:
        resolved = binary
    if os.path.basename(resolved) == 'fusermount3':
        return False
    run = subprocess.run if runner is None else runner
    try:
        result = run([binary, '-V'], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return False
    text = ((getattr(result, 'stdout', '') or '') + (getattr(result, 'stderr', '') or '')).lower()
    if 'fusermount3' in text or 'version: 3' in text:
        return False
    return 'version: 2' in text


def _looks_like_appimage(path):
    try:
        with open(path, 'rb') as handle:
            sample = handle.read(1_048_576)
    except OSError:
        return False
    return b'AppImage' in sample


def appimage_extract_enabled(config):
    """The Settings switch. Off unless the operator turns it on."""
    return getattr(config, 'desktop_appimage_extract', False) is True


def candidates() -> list[Path]:
    """Locations where an installed shell binary may live, in lookup order."""
    found: list[Path] = []
    explicit = os.environ.get('TESSERA_DESKTOP_BIN')
    if explicit:
        selected = Path(explicit).expanduser()
        if selected.suffix == '.app' and selected.is_dir():
            selected = selected / 'Contents' / 'MacOS' / BIN_NAME
        found.append(selected)
    if sys.platform == 'darwin':
        # The bundle's executable inside Tessera.app, in per-user and system
        # Applications, is the canonical macOS install location.
        for base in (Path.home() / 'Applications', Path('/Applications')):
            found.append(base / 'Tessera.app' / 'Contents' / 'MacOS' / BIN_NAME)
    elif sys.platform.startswith('win'):
        local = Path(os.environ.get('LOCALAPPDATA') or Path.home() / 'AppData' / 'Local')
        found.append(local / 'Programs' / 'Tessera' / (BIN_NAME + '.exe'))
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


def bootstrap_path(environment=None) -> Path:
    """Per-user native-menu handoff, separate from operator desktop settings."""
    environment = os.environ if environment is None else environment
    if sys.platform.startswith('win'):
        profile = Path(environment.get('USERPROFILE') or environment.get('HOME') or Path.home())
        directory = Path(environment.get('APPDATA') or profile / 'AppData' / 'Roaming')
    else:
        profile = Path(environment.get('HOME') or environment.get('USERPROFILE') or Path.home())
        directory = profile / '.config'
    return directory / 'tessera' / 'desktop-bootstrap.json'


def remember_install(home: Path, python: str, core_root: str) -> Path:
    """Remember nonsecret launch selection for later Finder/menu launches.

    Do not edit desktop.toml or resolve an interpreter symlink: resolving a
    venv interpreter would lose the installed package on the next launch.
    """
    import json
    path = bootstrap_path()
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {'home': str(Path(home).expanduser().resolve()),
                  'python': str(python), 'core_root': str(core_root)}
        if any(not value for value in record.values()):
            raise NativeShellNotFound('Cannot remember an empty native launch selection')
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix='.desktop-bootstrap-', delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(record, stream)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
    except OSError as error:
        raise NativeShellNotFound('Could not remember the native menu launch selection: ' + str(error)) from error
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return path


def launch(home: Path, *, port: int | None = None, startup_timeout: float | None = None,
           extract_appimage: bool = False) -> None:
    """Open and acknowledge the actual native window on every platform.

    The shell always owns its Python core. The short-lived public launcher
    waits for the shell's ready record rather than assuming exec success means
    a window opened. A failed launch is cleaned up before browser recovery.

    ``extract_appimage`` is the operator's Settings switch. It is off by
    default. On, and only when the AppImage cannot mount for lack of FUSE 2,
    the child is started with ``APPIMAGE_EXTRACT_AND_RUN=1`` and that choice
    is printed. Off, a stray value of that variable is removed so turning the
    switch off is what the launch does.
    """
    binary = find_binary()
    if binary is None:
        raise NativeShellNotFound('Tessera desktop shell is not installed')
    home = Path(home).expanduser().resolve()
    home.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.pop('APPIMAGE_EXTRACT_AND_RUN', None)
    env['TESSERA_HOME'] = str(home)
    env['TESSERA_DESKTOP_HOME'] = str(home)
    env.setdefault('TESSERA_DESKTOP_PYTHON', sys.executable)
    env.setdefault('TESSERA_DESKTOP_CORE_ROOT', str(Path.cwd()))
    remember_install(home, env['TESSERA_DESKTOP_PYTHON'], env['TESSERA_DESKTOP_CORE_ROOT'])
    if port is not None:
        env['TESSERA_DESKTOP_CORE_PORT'] = str(port)
    if startup_timeout is None:
        from .config import Config
        startup_timeout = Config().desktop_startup_timeout
    extracted = False
    if sys.platform.startswith('linux') and _looks_like_appimage(binary) and not fuse2_present():
        if not extract_appimage:
            raise NativeShellNotFound(fuse_mount_message(False), fuse=True)
        env['APPIMAGE_EXTRACT_AND_RUN'] = '1'
        _announce_extract()
        extracted = True
    try:
        _launch_native(binary, env, startup_timeout)
    except NativeShellNotFound as error:
        if not error.fuse:
            raise
        if extract_appimage and not extracted:
            env['APPIMAGE_EXTRACT_AND_RUN'] = '1'
            _announce_extract()
            try:
                _launch_native(binary, env, startup_timeout)
            except NativeShellNotFound as retry:
                if not retry.fuse:
                    raise
                raise NativeShellNotFound(
                    fuse_mount_message(True) + '\nExtract-and-run was already on, and the native window still did not open.',
                    fuse=True) from retry
            return
        raise NativeShellNotFound(fuse_mount_message(extract_appimage), fuse=True) from error


def _announce_extract():
    for line in fuse_mount_lines(True, fuse2_install_command()):
        print('tessera: ' + line, file=sys.stderr)


def _launch_native(binary, env, startup_timeout):
    import json
    import queue
    import threading
    try:
        process = subprocess.Popen([str(binary)], env=env, close_fds=True,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE,
                                   start_new_session=os.name != 'nt')
    except OSError as error:
        raise NativeShellNotFound(f'Could not launch {binary}: {error}') from error
    ready = queue.Queue()
    stderr_chunks = []

    def read_events():
        try:
            for line in iter(process.stdout.readline, b''):
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if isinstance(event, dict) and event.get('event') == 'tessera-desktop.window-ready':
                    ready.put(event)
                    # Keep draining while this launcher is alive. The shell's
                    # stdout is diagnostic only, never its lifetime pipe.
            ready.put(False)
        finally:
            process.stdout.close()

    def read_stderr():
        try:
            if process.stderr is not None:
                stderr_chunks.append(process.stderr.read())
        finally:
            if process.stderr is not None:
                process.stderr.close()

    reader = threading.Thread(target=read_events, daemon=True)
    reader.start()
    stderr_reader = threading.Thread(target=read_stderr, daemon=True)
    stderr_reader.start()
    try:
        loaded = ready.get(timeout=startup_timeout)
    except queue.Empty:
        loaded = False
    if loaded and process.poll() is None:
        loaded = {key: loaded[key] for key in ('event', 'startup_ms', 'shell_pid', 'core_pid') if key in loaded}
        loaded['native_owner_pid'] = process.pid
        print(json.dumps(loaded), flush=True)
        return
    # Each POSIX launch has a private process group. Even if the native shell
    # exits first, clean its owned children before a recovery core can start.
    if os.name != 'nt':
        import signal
        try:
            _signal_native_group(process.pid, signal.SIGTERM, startup_timeout)
        except ProcessLookupError:
            pass
    elif process.poll() is None:
        try:
            subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=startup_timeout)
        except (OSError, subprocess.SubprocessError):
            if process.poll() is None:
                process.terminate()
    try:
        process.wait(timeout=startup_timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
    if os.name != 'nt':
        import signal
        try:
            _signal_native_group(process.pid, signal.SIGKILL, startup_timeout)
        except ProcessLookupError:
            pass
    reader.join(timeout=startup_timeout)
    stderr_reader.join(timeout=startup_timeout)
    diagnostic = b''.join(stderr_chunks)[:8192].decode('utf-8', 'replace')
    if diagnostic.strip():
        # The AppImage's own words stay visible. The explanation is added by launch().
        print(diagnostic, file=sys.stderr, end='' if diagnostic.endswith('\n') else '\n')
    if appimage_fuse_failure(diagnostic):
        raise NativeShellNotFound('The native desktop window did not become ready', fuse=True)
    raise NativeShellNotFound('The native desktop window did not become ready')


def _signal_native_group(group, sig, timeout):
    try:
        os.killpg(group, sig)
    except PermissionError:
        # Darwin returns EPERM for a zombie-only process group. Confirm there
        # are no live members before treating that as already-cleaned; a real
        # permission failure must prevent recovery starting a competing core.
        result = subprocess.run(['/bin/ps', '-axo', 'pgid=,stat='],
                                stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, timeout=timeout)
        if result.returncode != 0:
            raise
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) != 2 or not parts[0].isdigit():
                raise
            if int(parts[0]) == group and not parts[1].startswith('Z'):
                raise


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
        return '.exe'      # per-user NSIS installer; MSI is a manual machine install
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
    if isinstance(checksum, str) and _SHA256_RE.fullmatch(checksum.strip()):
        return checksum.strip()
    raise NativeShellNotFound(
        'a valid 64-character lowercase or uppercase sha256 is required before '
        'the desktop bundle can be installed; got ' + repr(checksum)
    )


def _fetch(url: str, timeout: float) -> bytes:
    """Download bytes, normalizing every network failure to NativeShellNotFound."""
    try:
        request = urllib.request.Request(url, headers={'User-Agent': 'tessera-installer'})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise NativeShellNotFound(f'could not download {url}: {error}') from error


def _fetch_checksum(release_url: str, timeout: float) -> str:
    """Read and validate the sidecar ``.sha256`` beside a bundle, or refuse.

    A missing or malformed sidecar is a failure here: verification is mandatory,
    not best-effort. The caller decides whether that failure is recoverable.
    """
    try:
        data = _fetch(release_url + '.sha256', timeout=timeout).decode('utf-8').strip()
    except UnicodeError as error:
        raise NativeShellNotFound('desktop checksum sidecar is not UTF-8 text') from error
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
    try:
        if sys.platform == 'darwin':
            return _install_macos_app(release_url, data)
        if sys.platform.startswith('win'):
            return _install_windows(release_url, data, timeout)
        return _install_appimage(data)
    except (OSError, subprocess.SubprocessError) as error:
        raise NativeShellNotFound(f'could not install desktop bundle: {error}') from error


def _install_appimage(data: bytes) -> Path:
    """Replace the executable only after the complete verified file is ready."""
    target_dir = Path.home() / '.local' / 'bin'
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / BIN_NAME
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=target_dir, prefix='.tessera-', delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(data)
        temporary.chmod(0o755)
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return target


def _install_macos_app(_url: str, data: bytes) -> Path:
    """Stage and validate the app layout before replacing an installed app."""
    import io
    import tarfile
    from pathlib import PurePosixPath

    apps = Path.home() / 'Applications'
    apps.mkdir(parents=True, exist_ok=True)
    target = apps / 'Tessera.app'
    try:
        with tempfile.TemporaryDirectory(dir=apps, prefix='.tessera-install-') as directory:
            stage = Path(directory)
            with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as archive:
                members = archive.getmembers()
                if not members:
                    raise NativeShellNotFound('the macOS bundle is empty')
                for member in members:
                    path = PurePosixPath(member.name)
                    if (path.is_absolute() or '..' in path.parts or not path.parts
                            or path.parts[0] != 'Tessera.app'):
                        raise NativeShellNotFound('the macOS bundle must contain only Tessera.app')
                # The data filter rejects escaping links and special files. Extract
                # away from the existing app so an invalid archive cannot damage it.
                archive.extractall(stage, members=members, filter='data')
            app = stage / 'Tessera.app'
            executable = app / 'Contents' / 'MacOS' / BIN_NAME
            if (not executable.is_file() or not executable.resolve().is_relative_to(app.resolve())
                    or not os.access(executable, os.X_OK)):
                raise NativeShellNotFound('the macOS bundle lacks an executable Tessera core shell')
            backup = stage / 'previous.app'
            had_previous = target.exists() or target.is_symlink()
            if had_previous:
                os.replace(target, backup)
            try:
                os.replace(app, target)
            except OSError:
                if had_previous:
                    os.replace(backup, target)
                raise
    except (tarfile.TarError, ValueError, OSError) as error:
        raise NativeShellNotFound(f'could not install macOS app: {error}') from error
    return target / 'Contents' / 'MacOS' / BIN_NAME


def _install_windows(_url: str, data: bytes, timeout: float) -> Path:
    """Run the current-user NSIS package at an explicit, verified destination.

    Tauri's MSI template is per-machine. Its NSIS currentUser package instead
    requests user privileges and accepts /D= as its final argument. Explicitly
    selecting the directory avoids assuming the installer's default or a prior
    registry entry, and requires no administrator handoff.
    """
    local = Path(os.environ.get('LOCALAPPDATA') or Path.home() / 'AppData' / 'Local')
    destination = local / 'Programs' / 'Tessera'
    expected = destination / (BIN_NAME + '.exe')
    with tempfile.NamedTemporaryFile(suffix='.exe', delete=False) as handle:
        handle.write(data)
        installer = Path(handle.name)
    try:
        # NSIS requires /D= last and unquoted even when the directory has
        # spaces. shell=False keeps this a direct CreateProcess command line.
        command = subprocess.list2cmdline([str(installer), '/S']) + ' /D=' + str(destination)
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode != 0:
            raise NativeShellNotFound(f'Windows desktop installer exited {result.returncode}')
        if not expected.is_file():
            raise NativeShellNotFound('Windows desktop installer did not create the expected executable')
    finally:
        try:
            installer.unlink(missing_ok=True)
        except OSError:
            # Windows Defender or an installer helper can briefly keep the
            # verified public package open after its parent exits. Cleanup must
            # not replace a successful verified install or the actual failure.
            # The remaining file contains only a checksum-verified public
            # installer in the user's temporary directory, never credentials.
            pass
    return expected
