"""Start and stop a child in its own group on POSIX and Windows.

``start_new_session`` is a POSIX-only ``Popen`` argument: passing it on Windows
raises ``ValueError`` before the child exists. ``os.killpg`` and ``signal.SIGKILL``
are likewise absent there. Callers use this module so a stop reaches the child
on either operating system and a missing Windows API is not reported as a
configuration error.
"""

import os
import signal
import subprocess


def popen(argv, **kwargs):
    """``Popen`` in a new session on POSIX, a new process group on Windows."""
    kwargs.pop('start_new_session', None)
    if os.name == 'nt':
        flags = int(kwargs.pop('creationflags', 0)) | subprocess.CREATE_NEW_PROCESS_GROUP
        kwargs['creationflags'] = flags
    else:
        kwargs['start_new_session'] = True
    return subprocess.Popen(argv, **kwargs)


def shell_argv(command):
    """Run one command string through the platform shell, not a hard-coded ``/bin/sh``."""
    if os.name == 'nt':
        comspec = os.environ.get('COMSPEC') or 'cmd.exe'
        return [comspec, '/d', '/s', '/c', command]
    return ['/bin/sh', '-c', command]


def _taskkill(pid, *, force):
    """Run taskkill and surface its result.

    A missing process is ``ProcessLookupError``, the same signal POSIX
    ``killpg`` raises. Any other failure is an ``OSError`` whose message
    names taskkill and includes the output it printed.
    """
    command = ['taskkill', '/T', '/PID', str(pid)]
    if force:
        command.insert(1, '/F')
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode == 0:
        return
    output = '\n'.join(part.strip() for part in (result.stdout or '', result.stderr or '') if part and part.strip())
    lowered = output.lower()
    if 'not found' in lowered or 'no running instance' in lowered:
        raise ProcessLookupError(output or f'process {pid} not found')
    detail = output or f'exit {result.returncode}'
    raise OSError(f'taskkill failed: {detail}')


def terminate_group(pid):
    """Ask the group to exit. SIGTERM on POSIX; ``taskkill`` without ``/F`` on Windows."""
    if os.name == 'nt':
        _taskkill(pid, force=False)
        return
    os.killpg(pid, signal.SIGTERM)


def kill_group(pid):
    """Stop the group now. SIGKILL on POSIX; ``taskkill /F /T`` on Windows."""
    if os.name == 'nt':
        _taskkill(pid, force=True)
        return
    os.killpg(pid, signal.SIGKILL)


def alive(pid):
    """True when ``pid`` still names a process. A permission error means it exists."""
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, int(pid))
        if handle:
            kernel.CloseHandle(handle)
            return True
        # ERROR_ACCESS_DENIED: the process exists and this user cannot query it.
        return ctypes.get_last_error() == 5
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
