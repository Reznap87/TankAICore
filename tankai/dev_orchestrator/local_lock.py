"""Process-owned file locks for a single local host, released by the OS on exit.

The anchor remains on disk. Never unlink an anchor while writers may use it:
replacing its inode would let two processes lock different files at one path.
All writers must be stopped when upgrading from the legacy age/unlink protocol.
Not a multi-host coordinator; use durable local storage, not NFS.
"""
from contextlib import contextmanager
import errno
import math
import os
from pathlib import Path
import stat
import time
from typing import Iterator


class LocalLockTimeout(TimeoutError):
    """Acquisition timed out, distinct from errors inside a locked mutation."""


@contextmanager
def exclusive_local_lock(path: Path, *, timeout_seconds: float) -> Iterator[None]:
    if not math.isfinite(timeout_seconds) or timeout_seconds < 0:
        raise ValueError("Lock timeout must be finite and nonnegative")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError("Lock anchor must not be a symlink")
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("Lock anchor must be a regular file")
        if os.name == "nt":
            import msvcrt
            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"\0")
            def acquire():
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            def acquire():
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                acquire()
                break
            except OSError as error:
                if error.errno not in {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK, errno.EDEADLK, errno.EINTR}:
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise LocalLockTimeout("Local writer lock is occupied") from error
                time.sleep(min(0.05, remaining))
        yield
    finally:
        # Closing also releases the byte-range lock on Windows. It works after
        # exceptions and the kernel does the same after process termination.
        os.close(descriptor)
