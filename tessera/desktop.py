"""App-owned core: one ready record, with stdin EOF or SIGTERM as shutdown."""

import ctypes
import ctypes.util
import json
import os
import select
import signal
import stat
import subprocess
import sys
import threading
from urllib.parse import urlsplit

from .server import create_server

# The tests replace these two helpers, not the os module. Patching os.fstat
# globally also changes pytest's capture manager, whose teardown calls fstat
# on its own output file.
_fstat = os.fstat
_close = os.close

# Windows execv creates a new process rather than replacing the native shell's
# monitored Child handle. A small owner stays alive and restarts only its worker.
# The marker is private child environment, not a user setting or a credential.
_WORKER_ENV = '_TESSERA_DESKTOP_WORKER'
_RESTART_EXIT = 75
_owned_worker = False


def owned_worker():
    return _owned_worker


def claim_owned_worker():
    global _owned_worker
    # Child tools must not inherit a claim to the owner's restart protocol.
    _owned_worker = os.environ.pop(_WORKER_ENV, '') == '1'
    return _owned_worker


def _run_owned(config, mode='desktop'):
    """Keep the shell-owned process alive while fresh workers serve its origin.

    This owner never creates a server, model thread, or memory manager. Desktop
    workers inherit the shell's stdin lifetime; kept serve workers stay detached.
    Worker EOF or actual failures exit the owner, while the explicit restart exit
    code alone starts a new interpreter.
    """
    environment = dict(os.environ)
    environment[_WORKER_ENV] = '1'
    port = config.server.port
    owner_stdin = sys.stdin.fileno()
    # Only the worker reads this pipe. A simultaneous owner read can hold the
    # Windows CRT descriptor lock while Popen duplicates fd 0, deadlocking before
    # the first worker starts. Worker EOF exits normally and is never restarted.
    # If EOF races a restart marker, the fresh worker inherits EOF and exits too.
    while True:
        command = [sys.executable, '-u', '-m', 'tessera', '--home', str(config.home),
                   mode, '--host', config.server.host, '--port', str(port)]
        with subprocess.Popen(command, env=environment, stdin=owner_stdin,
                              stdout=subprocess.PIPE, stderr=sys.stderr) as worker:
            ready = False
            for line in worker.stdout:
                if not ready:
                    try:
                        url = None
                        if mode == 'desktop':
                            record = json.loads(line)
                            if record.get('type') == 'tessera.ready' and record.get('version') == 1:
                                url = record['url']
                        elif line.startswith(b'Serving Tessera at '):
                            url = line.decode().split()[-1]
                        if url is not None:
                            address = urlsplit(url)
                            if address.port:
                                port = address.port
                                ready = True
                    except (ValueError, TypeError, KeyError, AttributeError):
                        pass
                # Preserve the existing readiness protocol and keep draining logs
                # so a worker cannot be stranded on a full stdout pipe.
                sys.stdout.buffer.write(line)
                sys.stdout.buffer.flush()
            status = worker.wait()
        if status != _RESTART_EXIT or not ready:
            # All owned worker resources are reaped; preserve its actual status.
            os._exit(status)


def _terminate(signum, frame):
    raise KeyboardInterrupt


def _open_fds():
    """Fds this process actually has open.

    A scan of ``0 .. OPEN_MAX`` misses the leaked writer when the parent
    already holds a few hundred descriptors: macOS ``OPEN_MAX`` is huge, and
    the pipe from ``posix_spawn`` keeps its original number. ``/dev/fd`` lists
    the open ones.
    """
    try:
        names = os.listdir('/dev/fd')
    except OSError:
        names = None
    if names is not None:
        for name in names:
            if name.isdigit():
                fd = int(name)
                if fd >= 3:
                    yield fd
        return
    try:
        limit = int(os.sysconf('SC_OPEN_MAX'))
    except (OSError, ValueError, AttributeError):
        limit = 256
    yield from range(3, min(limit, 1024))


def _stdin_pipe_identity():
    """``(st_dev, st_ino)`` when fd 0 is a pipe this process can match.

    Linux reports ``S_IFIFO``. macOS ``fstat`` on a pipe zeroes ``st_mode``
    and still fills a device and inode shared by both ends, so requiring
    ``S_IFIFO`` skips the leaked writer and the shell's close is not EOF.
    A zero inode is not a matchable pipe.
    """
    try:
        own = _fstat(0)
    except OSError:
        return None
    if own.st_ino == 0:
        return None
    if stat.S_ISFIFO(own.st_mode) or own.st_mode == 0:
        return (own.st_dev, own.st_ino)
    return None


def _darwin_pipe_ends(fd):
    """``(handle, peer_handle)`` for one macOS pipe, or None.

    ``fstat`` on the two ends of a Darwin pipe does not share an inode the
    closer can trust. ``proc_pidfdinfo`` names each end and its peer. The
    leaked writer is the fd whose handle is stdin's peer.
    """
    if sys.platform != 'darwin':
        return None
    library = ctypes.util.find_library('System')
    if not library:
        return None
    try:
        lib = ctypes.CDLL(library, use_errno=True)
    except OSError:
        return None

    class VinfoStat(ctypes.Structure):
        _fields_ = [
            ('vst_dev', ctypes.c_int32),
            ('vst_mode', ctypes.c_uint16),
            ('vst_nlink', ctypes.c_uint16),
            ('vst_ino', ctypes.c_uint64),
            ('vst_uid', ctypes.c_uint32),
            ('vst_gid', ctypes.c_uint32),
            ('vst_atime', ctypes.c_int64),
            ('vst_atimensec', ctypes.c_int64),
            ('vst_mtime', ctypes.c_int64),
            ('vst_mtimensec', ctypes.c_int64),
            ('vst_ctime', ctypes.c_int64),
            ('vst_ctimensec', ctypes.c_int64),
            ('vst_birthtime', ctypes.c_int64),
            ('vst_birthtimensec', ctypes.c_int64),
            ('vst_size', ctypes.c_int64),
            ('vst_blocks', ctypes.c_int64),
            ('vst_blksize', ctypes.c_int32),
            ('vst_flags', ctypes.c_uint32),
            ('vst_gen', ctypes.c_uint32),
            ('vst_rdev', ctypes.c_int32),
            ('vst_qspare', ctypes.c_int64 * 2),
        ]

    class PipeInfo(ctypes.Structure):
        _fields_ = [
            ('pipe_stat', VinfoStat),
            ('pipe_handle', ctypes.c_uint64),
            ('pipe_peerhandle', ctypes.c_uint64),
            ('pipe_status', ctypes.c_int),
            ('rfu_1', ctypes.c_int),
        ]

    class ProcFileInfo(ctypes.Structure):
        _fields_ = [
            ('fi_openflags', ctypes.c_uint32),
            ('fi_status', ctypes.c_uint32),
            ('fi_offset', ctypes.c_int64),
            ('fi_type', ctypes.c_int32),
            ('fi_guardflags', ctypes.c_uint32),
        ]

    class PipeFdInfo(ctypes.Structure):
        _fields_ = [
            ('pfi', ProcFileInfo),
            ('pipeinfo', PipeInfo),
        ]

    info = PipeFdInfo()
    lib.proc_pidfdinfo.argtypes = [
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
    lib.proc_pidfdinfo.restype = ctypes.c_int
    size = ctypes.sizeof(info)
    # PROC_PIDFDPIPEINFO is 6.
    count = lib.proc_pidfdinfo(os.getpid(), fd, 6, ctypes.byref(info), size)
    if count < size:
        return None
    handle = int(info.pipeinfo.pipe_handle)
    peer = int(info.pipeinfo.pipe_peerhandle)
    if handle == 0 or peer == 0:
        return None
    return handle, peer


def _closes_leaked_stdin(fd, identity, stdin_ends):
    if identity is not None:
        try:
            other = _fstat(fd)
        except OSError:
            other = None
        if other is not None and (other.st_dev, other.st_ino) == identity:
            return True
    if stdin_ends is None:
        return False
    ends = _darwin_pipe_ends(fd)
    if ends is None:
        return False
    _stdin_handle, stdin_peer = stdin_ends
    handle, _peer = ends
    return handle == stdin_peer


def _drop_leaked_stdin_writer():
    """Close an inherited copy of the stdin pipe's write end.

    A multi-threaded parent on macOS can posix_spawn this process and leave
    the write end open in the child. The shell's close is then not EOF, and
    the core stays up after the app drops stdin.
    """
    if os.name == 'nt':
        return
    identity = _stdin_pipe_identity()
    stdin_ends = _darwin_pipe_ends(0)
    if identity is None and stdin_ends is None:
        return
    for fd in _open_fds():
        if _closes_leaked_stdin(fd, identity, stdin_ends):
            try:
                _close(fd)
            except OSError:
                pass


def _wait_for_stdin_eof():
    """Block until the owner closes stdin, and notice SIGTERM while doing it.

    ``sys.stdin.read`` stays inside one syscall. On macOS that syscall is not
    interrupted when the signal is delivered to the server thread, so the
    handler never runs. Polling returns to Python often enough for the
    pending SIGTERM to become KeyboardInterrupt.
    """
    if os.name == 'nt':
        while sys.stdin.read(1):
            pass
        return
    fd = sys.stdin.fileno()
    while True:
        try:
            ready, _, _ = select.select([fd], [], [], 0.2)
        except InterruptedError:
            continue
        if not ready:
            continue
        try:
            chunk = os.read(fd, 65536)
        except InterruptedError:
            continue
        if not chunk:
            break


def run(config):
    claim_owned_worker()
    if os.name == 'nt' and not owned_worker():
        return _run_owned(config)
    _drop_leaked_stdin_writer()
    previous = signal.signal(signal.SIGTERM, _terminate)
    restart = None
    try:
        with create_server(config) as server:
            server.self_supervised = True
            owner_closed = threading.Event()

            def watch_owner():
                try:
                    _wait_for_stdin_eof()
                finally:
                    owner_closed.set()

            worker = threading.Thread(target=server.serve_forever, name='desktop-core', daemon=True)
            worker.start()
            try:
                record = {'type': 'tessera.ready', 'version': 1, 'transport': 'loopback-http',
                          'url': server.url, 'pid': os.getpid()}
                print(json.dumps(record), flush=True)
                # The desktop shell owns this pipe. EOF also handles an unexpected parent exit.
                threading.Thread(target=watch_owner, name='desktop-owner', daemon=True).start()
                while worker.is_alive() and not owner_closed.wait(0.1):
                    pass
                if server._restarting and not owner_closed.is_set():
                    restart = (server.config, server.server_address[1])
            finally:
                # shutdown must run outside the serve_forever thread.
                server.shutdown()
                worker.join()
    except KeyboardInterrupt:
        restart = None
    finally:
        signal.signal(signal.SIGTERM, previous)
    if restart is not None:
        from .updates import restart_process
        restart_process(restart[0], 'desktop', restart[1])
