"""Exclusive local file locks with the same interface on POSIX and Windows."""

import errno
import os
import time

if os.name == 'nt':
    import msvcrt
else:
    import fcntl

LOCK_EX, LOCK_NB, LOCK_UN = 2, 4, 8


def flock(file, operation):
    descriptor = file if isinstance(file, int) else file.fileno()
    if os.name != 'nt':
        return fcntl.flock(descriptor, operation)
    # All owners lock byte zero, including empty lock files. Restore the offset
    # so callers can keep using the handle without depending on this detail.
    position = os.lseek(descriptor, 0, os.SEEK_CUR)
    try:
        while True:
            os.lseek(descriptor, 0, os.SEEK_SET)
            try:
                mode = msvcrt.LK_UNLCK if operation == LOCK_UN else msvcrt.LK_NBLCK
                msvcrt.locking(descriptor, mode, 1)
                return
            except OSError as error:
                if operation == LOCK_UN or error.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise
                if operation & LOCK_NB:
                    raise BlockingIOError(errno.EAGAIN, 'File is locked') from error
                time.sleep(0.05)
    finally:
        os.lseek(descriptor, position, os.SEEK_SET)
