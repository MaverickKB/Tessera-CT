"""Synthetic subprocess probes prove timeout/output bounds without live hosts."""
import errno
import json
import os
import subprocess
import sys
import time

import pytest


def test_synthetic_probe_returns_json_without_a_shell():
    from tessera.probe_process import run_probe
    result=run_probe([sys.executable,'-c','print(\'{"metrics":{"cpu_percent":4}}\')'],timeout=2,max_output_bytes=1024)
    assert result.returncode == 0 and json.loads(result.stdout)['metrics']['cpu_percent'] == 4
    assert result.stderr == ''


def test_probe_limits_stdout_stderr_and_timeout():
    from tessera.probe_process import run_probe, ProbeLimitError
    for stream in ('stdout','stderr'):
        with pytest.raises(ProbeLimitError):
            run_probe([sys.executable,'-c',f'import sys; sys.{stream}.write("x"*4096)'],timeout=2,max_output_bytes=100)
    with pytest.raises(subprocess.TimeoutExpired):
        run_probe([sys.executable,'-c','import time; time.sleep(10)'],timeout=.1,max_output_bytes=1024)


def test_a_probe_error_survives_a_killpg_that_raises_eperm(monkeypatch):
    """On macOS killpg() on an already-reaped group leader returns EPERM.

    The kill is best-effort and runs in a `finally`, so an exception there
    replaces whatever the caller was already raising. Catching only
    ProcessLookupError let EPERM escape and mask the real probe failure.
    """
    import tessera.probe_process as probe

    def eperm_kill(*_args, **_kwargs):
        raise PermissionError(errno.EPERM, 'Operation not permitted')

    # POSIX cleanup calls os.killpg. Windows has no killpg; taskkill is reached
    # through kill_group, so the same PermissionError is injected there.
    if hasattr(os, 'killpg'):
        monkeypatch.setattr(os, 'killpg', eperm_kill)
    else:
        monkeypatch.setattr(probe, 'kill_group', eperm_kill)
    # A probe that exceeds its output bound fails for its own reason. That reason
    # must be what the caller sees, not the cleanup's PermissionError.
    with pytest.raises(probe.ProbeLimitError, match='Probe exceeded configured output limit'):
        probe.run_probe([sys.executable, '-c', 'print("x" * 5000)'],
                        max_output_bytes=64, timeout=10)


def test_a_descendant_holding_the_pipes_cannot_outrun_a_tenth_second():
    """The child exits immediately. Its descendant keeps stdout and stderr open
    from a new session, so killing the child group does not close the pipes.

    The 0.1s deadline has to cover that drain. Returning success, or blocking
    in cleanup until the descendant exits, is the failure.
    """
    from tessera.probe_process import run_probe

    holder = (
        'import os, subprocess, sys\n'
        'command = [sys.executable, "-c", "import time; time.sleep(3)"]\n'
        'kwargs = {"stdin": subprocess.DEVNULL, "stdout": sys.stdout, "stderr": sys.stderr}\n'
        'if sys.platform == "win32":\n'
        '    kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP\n'
        'else:\n'
        '    kwargs["start_new_session"] = True\n'
        'subprocess.Popen(command, **kwargs)\n'
        'os._exit(0)\n'
    )
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        run_probe([sys.executable, '-c', holder], timeout=0.1, max_output_bytes=1024)
    elapsed = time.monotonic() - started
    assert elapsed < 1.0, elapsed


def test_a_read_error_is_not_reported_as_end_of_output(monkeypatch):
    """A real read failure must keep its errno. b'' is only a closed pipe."""
    import tessera.probe_process as probe

    monkeypatch.setattr(probe.os, 'name', 'posix')

    def fail(fd, size):
        raise OSError(errno.EIO, 'Input/output error')

    monkeypatch.setattr(probe.os, 'read', fail)
    with pytest.raises(OSError) as caught:
        probe._read_available(0)
    assert caught.value.errno == errno.EIO
    assert caught.value.strerror == 'Input/output error'


def test_would_block_interrupt_and_eof_are_the_only_empty_reads(monkeypatch):
    import tessera.probe_process as probe

    monkeypatch.setattr(probe.os, 'name', 'posix')

    def block(fd, size):
        raise BlockingIOError(errno.EAGAIN, 'Resource temporarily unavailable')

    monkeypatch.setattr(probe.os, 'read', block)
    assert probe._read_available(0) is None

    def interrupt(fd, size):
        raise InterruptedError()

    monkeypatch.setattr(probe.os, 'read', interrupt)
    assert probe._read_available(0) is None

    def again(fd, size):
        raise OSError(errno.EAGAIN, 'Resource temporarily unavailable')

    monkeypatch.setattr(probe.os, 'read', again)
    assert probe._read_available(0) is None

    monkeypatch.setattr(probe.os, 'read', lambda fd, size: b'')
    assert probe._read_available(0) == b''


def test_a_closed_windows_pipe_is_end_of_output_and_other_errors_are_not(monkeypatch):
    import tessera.probe_process as probe

    monkeypatch.setattr(probe.os, 'name', 'nt')
    monkeypatch.setattr(probe, '_windows_available', lambda fd: 8)

    def closed(fd, size):
        error = OSError(22, 'Invalid argument')
        error.winerror = 109
        raise error

    monkeypatch.setattr(probe.os, 'read', closed)
    assert probe._read_available(0) == b''

    def peek_failed(fd):
        raise OSError(None, 'PeekNamedPipe failed: 5', None, 5)

    monkeypatch.setattr(probe, '_windows_available', peek_failed)
    with pytest.raises(OSError, match='PeekNamedPipe failed: 5'):
        probe._read_available(0)


def test_a_child_that_survives_the_first_stop_is_killed_and_reaped(monkeypatch):
    """The first wait stays short. A child still alive after it is killed again and reaped."""
    import tessera.probe_process as probe
    from tessera.procutil import popen

    real = probe.kill_group
    calls = []

    def wrapped(pid):
        calls.append(pid)
        if len(calls) == 1:
            return
        real(pid)

    monkeypatch.setattr(probe, 'kill_group', wrapped)
    process = popen(
        [sys.executable, '-c', 'import time; time.sleep(30)'],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    started = time.monotonic()
    try:
        probe._stop(process)
        elapsed = time.monotonic() - started
    finally:
        if process.poll() is None:
            real(process.pid)
            process.wait(timeout=5)
    assert process.poll() is not None
    assert len(calls) >= 2
    assert elapsed < 2, elapsed


def test_a_probe_with_pipe_fds_at_or_above_1024_still_returns():
    """``select.select`` raises ValueError once an fd is >= 1024. The POSIX probe must not."""
    reserved = []
    try:
        while True:
            read_fd, write_fd = os.pipe()
            if read_fd >= 1024 or write_fd >= 1024:
                os.close(read_fd)
                os.close(write_fd)
                break
            reserved.extend((read_fd, write_fd))
            if len(reserved) > 4096:
                pytest.skip('could not open a descriptor at or above 1024')
        from tessera.probe_process import run_probe
        result = run_probe(
            [sys.executable, '-c', 'print("high-fd")'],
            timeout=5, max_output_bytes=1024)
        assert result.returncode == 0
        assert result.stdout.strip() == 'high-fd'
    finally:
        for fd in reserved:
            os.close(fd)


def test_cleanup_does_not_hide_the_probe_error(monkeypatch):
    """A TimeoutExpired or PermissionError from reap must not replace the probe failure."""
    import tessera.probe_process as probe
    from tessera.procutil import popen

    process = popen(
        [sys.executable, '-c', 'import time; time.sleep(30)'],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def deny(pid):
        raise PermissionError(errno.EPERM, 'Operation not permitted')

    def still_running(timeout=None):
        raise subprocess.TimeoutExpired('configured probe', timeout or 0.2)

    monkeypatch.setattr(probe, 'kill_group', deny)
    monkeypatch.setattr(process, 'wait', still_running)
    monkeypatch.setattr(process, 'poll', lambda: None)
    try:
        with pytest.raises(probe.ProbeLimitError, match='Probe exceeded configured output limit'):
            try:
                raise probe.ProbeLimitError('Probe exceeded configured output limit')
            finally:
                probe._stop(process)
    finally:
        monkeypatch.undo()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_a_stop_that_cannot_signal_the_child_reports_the_permission_error(monkeypatch):
    """PermissionError must not disappear into an unbounded wait."""
    import tessera.probe_process as probe
    from tessera.procutil import popen

    calls = []

    def wrapped(pid):
        calls.append(pid)
        if len(calls) == 1:
            return
        raise PermissionError(errno.EPERM, 'Operation not permitted')

    monkeypatch.setattr(probe, 'kill_group', wrapped)
    process = popen(
        [sys.executable, '-c', 'import time; time.sleep(30)'],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    started = time.monotonic()
    try:
        with pytest.raises(PermissionError) as caught:
            probe._stop(process)
        elapsed = time.monotonic() - started
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
    assert caught.value.errno == errno.EPERM
    assert len(calls) >= 2
    assert elapsed < 2, elapsed
