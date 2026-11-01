"""Bound the core's own output file so a steady-state floor cannot fill the disk.

The core is started with stdout and stderr pointing at a file (the desktop
launches `logs/core.log`; an operator launcher may use `core.out`). A line per
tick with no rotation turns a healthy home into a 90k-line file that hides real
errors. Rotation happens once at startup: rename the file aside, move the old
ones down, then reopen the same path onto the same descriptors so the process
keeps writing to the file it is named after.
"""

import os


def stream_file(fd):
    """The regular file this descriptor writes to, or None.

    A terminal, a pipe or a socket is not ours to rotate: the operator is
    reading it. Only a real file on disk is bounded here.
    """
    try:
        target = os.readlink('/proc/self/fd/' + str(fd))
    except OSError:
        return None
    if not target.startswith('/'):
        return None
    try:
        if not os.path.isfile(target):
            return None
    except OSError:
        return None
    return target


def rotate_file(path, cap_bytes, keep):
    """Rename path to path.1 (and shift older files) when it exceeds cap_bytes.

    Returns the rotated path, or None when the file is small enough or absent.
    A rotation keeps `keep` older files; anything past that is removed.
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        return None
    if size <= cap_bytes:
        return None
    if keep <= 0:
        os.remove(path)
        return path
    oldest = path + '.' + str(keep)
    if os.path.exists(oldest):
        os.remove(oldest)
    # Shift each archive one number older, newest first, so no rename
    # overwrites a file that has not been moved yet.
    number = keep - 1
    while number >= 1:
        source = path + '.' + str(number)
        target = path + '.' + str(number + 1)
        if os.path.exists(source):
            os.rename(source, target)
        number -= 1
    os.rename(path, path + '.1')
    return path + '.1'


def reopen(path, fd):
    """Point this descriptor at a fresh append handle on path.

    The process keeps writing to its named log file after a rotation; without
    this, everything after the rename would still land in the rotated file.
    """
    handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.dup2(handle, fd)
    finally:
        os.close(handle)


def rotate_core_streams(cap_bytes, keep, fds=(1, 2)):
    """Rotate the file behind the core's output descriptors, if there is one.

    Returns {'rotated': path, 'file': path} or empty values, so the caller can
    print one line at startup and nothing on the common case.

    The descriptor is mapped to its file BEFORE the rename: after the rename
    that path no longer exists, and a second lookup cannot tell which stream
    had been writing there. Every descriptor that pointed at the rotated file
    is reopened onto the fresh file, or the core keeps writing to the archive.
    """
    cap_bytes = int(cap_bytes)
    keep = int(keep)
    if keep < 0:
        raise ValueError('log rotation needs a nonnegative keep count')
    if cap_bytes <= 0:
        # Zero is the documented off switch: leave the operator's file alone.
        return {'rotated': '', 'file': ''}
    streamed = {}
    for fd in fds:
        streamed[fd] = stream_file(fd)
    for path in dict.fromkeys(value for value in streamed.values() if value):
        rotated = rotate_file(path, cap_bytes, keep)
        if rotated is None:
            continue
        for fd, target in streamed.items():
            if target == path:
                reopen(path, fd)
        return {'rotated': rotated, 'file': path}
    return {'rotated': '', 'file': ''}
