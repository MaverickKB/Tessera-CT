"""Directory durability where the platform exposes directory fsync."""

import errno
import json
import os


def open_regular(path):
    """Open a regular file and refuse a symlink.

    POSIX uses ``O_NOFOLLOW`` (and ``O_NONBLOCK`` when the platform has it).
    Windows has neither flag, so a symlink is rejected before the open and the
    caller still sees ``ELOOP``.
    """
    if os.name == 'nt':
        if os.path.islink(path):
            raise OSError(errno.ELOOP, 'is a symlink', path)
        return os.open(path, os.O_RDONLY)
    flags = os.O_RDONLY
    if hasattr(os, 'O_NONBLOCK'):
        flags |= os.O_NONBLOCK
    if hasattr(os, 'O_NOFOLLOW'):
        flags |= os.O_NOFOLLOW
    return os.open(path, flags)


def sync_directory(path):
    # Windows CRT cannot open directories for fsync. Callers still flush file
    # contents; directory-entry survival across power loss is not guaranteed.
    if os.name == 'nt':
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_json_atomic(path, data):
    """Write JSON through a temp sibling so a reader never sees a torn file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(data, stream, ensure_ascii=False, indent=1, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
