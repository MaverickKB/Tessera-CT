"""Bounded read-only probe transport. No shell, stdin, prompts or diagnostics."""
import errno
import os
import selectors
import subprocess
import sys
import time

from .procutil import kill_group, popen


class ProbeLimitError(RuntimeError):
    pass


def run_probe(argv, *, timeout, max_output_bytes, **options):
    """Run argv and return when it exits and both pipes reach EOF.

    ``timeout`` is one deadline for the process and for draining stdout and
    stderr. A descendant that inherited a pipe and left the process group
    must not extend that deadline. Cleanup closes the read ends without
    waiting for that descendant.
    """
    process = popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, shell=False)
    deadline = time.monotonic() + timeout
    try:
        if os.name == 'nt':
            chunks = _drain_polling(process, deadline, timeout, max_output_bytes)
        else:
            chunks = _drain_selector(process, deadline, timeout, max_output_bytes)
        remaining = deadline - time.monotonic()
        try:
            process.wait(timeout=max(0, remaining))
        except subprocess.TimeoutExpired:
            raise subprocess.TimeoutExpired('configured probe', timeout) from None
        return subprocess.CompletedProcess(
            argv, process.returncode, b''.join(chunks).decode('utf-8'), '')
    finally:
        _stop(process)


def _drain_selector(process, deadline, timeout, max_output_bytes):
    """POSIX drain. ``selectors`` handles fds at or above 1024; ``select.select`` does not."""
    chunks = []
    consumed = 0
    with selectors.DefaultSelector() as selector:
        for stream in (process.stdout, process.stderr):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired('configured probe', timeout)
            try:
                events = selector.select(remaining)
            except InterruptedError:
                continue
            if not events:
                if time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired('configured probe', timeout)
                continue
            for key, _mask in events:
                data = _read_available(key.fileobj.fileno())
                if data is None:
                    continue
                if not data:
                    selector.unregister(key.fileobj)
                    continue
                consumed += len(data)
                if consumed > max_output_bytes:
                    raise ProbeLimitError('Probe exceeded configured output limit')
                if key.fileobj is process.stdout:
                    chunks.append(data)
    return chunks


def _drain_polling(process, deadline, timeout, max_output_bytes):
    """Windows drain. Pipes there are not selectable, so the wait is a poll."""
    keep = {process.stdout.fileno(): True, process.stderr.fileno(): False}
    open_fds = set(keep)
    chunks = []
    consumed = 0
    while open_fds:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired('configured probe', timeout)
        ready = _wait_readable_windows(list(open_fds), remaining)
        if not ready:
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired('configured probe', timeout)
            continue
        for fd in ready:
            data = _read_available(fd)
            if data is None:
                continue
            if not data:
                open_fds.discard(fd)
                continue
            consumed += len(data)
            if consumed > max_output_bytes:
                raise ProbeLimitError('Probe exceeded configured output limit')
            if keep[fd]:
                chunks.append(data)
    return chunks


# PeekNamedPipe / ReadFile codes that mean the write end is gone.
_CLOSED_PIPE = frozenset({109, 232, 233})


def _read_available(fd):
    """Bytes ready now. None means not ready. b'' means the pipe reached EOF.

    A would-block or an interrupted read is not ready. A closed pipe is EOF.
    Any other read error propagates with its own errno and message.
    """
    try:
        if os.name == 'nt':
            amount = _windows_available(fd)
            if amount < 0:
                return b''
            if amount == 0:
                return None
            return os.read(fd, min(65536, amount))
        return os.read(fd, 65536)
    except InterruptedError:
        return None
    except BlockingIOError:
        return None
    except OSError as error:
        if error.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
            return None
        if getattr(error, 'winerror', None) in _CLOSED_PIPE:
            return b''
        raise


def _wait_readable_windows(fds, timeout):
    deadline = time.monotonic() + max(0, timeout)
    while True:
        ready = [fd for fd in fds if _windows_available(fd) != 0]
        if ready or time.monotonic() >= deadline:
            return ready
        time.sleep(min(0.02, max(0, deadline - time.monotonic())))


def _windows_available(fd):
    """Bytes waiting in a pipe, or -1 when the write end is gone."""
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel = _windows_available.kernel
    if kernel is None:
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.PeekNamedPipe.argtypes = [
            wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD)]
        kernel.PeekNamedPipe.restype = wintypes.BOOL
        _windows_available.kernel = kernel
    available = wintypes.DWORD(0)
    handle = msvcrt.get_osfhandle(fd)
    if not kernel.PeekNamedPipe(handle, None, 0, None, ctypes.byref(available), None):
        code = ctypes.get_last_error()
        if code in _CLOSED_PIPE:
            return -1
        raise OSError(None, f'PeekNamedPipe failed: {code}', None, code)
    return int(available.value)


_windows_available.kernel = None


def _stop(process):
    """Reap the child. Cleanup must not replace an error the probe already raised."""
    propagating = sys.exc_info()[0] is not None
    try:
        _reap(process)
    except (subprocess.TimeoutExpired, PermissionError):
        if not propagating:
            raise
    finally:
        for stream in (process.stdout, process.stderr):
            if stream is None:
                continue
            try:
                stream.close()
            except OSError:
                pass


def _reap(process):
    try:
        kill_group(process.pid)
    except ProcessLookupError:
        pass
    except PermissionError:
        # An already-reaped group can raise EPERM. The short wait below reaps
        # a zombie. A child that is still alive is handled on the escalation
        # path, which reports the permission error instead of waiting forever.
        pass
    if process.poll() is None:
        try:
            process.wait(timeout=0.2)
        except subprocess.TimeoutExpired:
            denied = None
            try:
                kill_group(process.pid)
            except ProcessLookupError:
                pass
            except PermissionError as error:
                denied = error
            try:
                process.wait(timeout=0.2)
            except subprocess.TimeoutExpired as expired:
                if denied is not None:
                    raise denied from expired
                raise
