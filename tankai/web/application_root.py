"""Opt-in single-process roots for the closed pilot; no adoption or cutover."""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
from uuid import UUID, uuid4

from tankai.core.monetary_budget import _sync_directory

MARKER = ".tankai-root.json"
LOCK = ".tankai-root.lock"
PENDING = ".tankai-restore-pending"


class ApplicationRootError(RuntimeError):
    """Fixed diagnostics deliberately exclude paths and account contents."""


def identity(info):
    return info.st_dev, info.st_ino


def safe_path(path: Path) -> Path:
    """Reject links/reparse points in every existing component, including parents."""
    path = Path(os.path.abspath(path))
    for part in (*reversed(path.parents), path):
        if not os.path.lexists(part):
            continue
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ApplicationRootError("linked application paths are forbidden")
    return path


def regular(path: Path):
    info = safe_path(path).lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ApplicationRootError("exclusively named regular file required")
    return info


def read_regular(path: Path, limit: int) -> bytes:
    before = regular(path)
    if not 0 <= before.st_size <= limit:
        raise ApplicationRootError("application file exceeds capacity")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    with os.fdopen(os.open(path, flags), "rb") as handle:
        opened = os.fstat(handle.fileno())
        data = handle.read(limit + 1)
        after = os.fstat(handle.fileno())
    signature = lambda i: (identity(i), i.st_nlink, i.st_size, i.st_mtime_ns)
    if (signature(before) != signature(opened) or signature(opened) != signature(after)
            or signature(after) != signature(regular(path)) or len(data) != before.st_size):
        raise ApplicationRootError("application file changed during read")
    return data


def write_new(path: Path, data: bytes):
    safe_path(path.parent)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
        created = identity(os.fstat(handle.fileno()))
    _sync_directory(path.parent)
    if identity(regular(path)) != created or read_regular(path, len(data)) != data:
        raise ApplicationRootError("new application file failed verification")
    return created


def root_metadata(root: Path) -> dict:
    root = safe_path(root)
    if os.path.lexists(root / PENDING):
        raise ApplicationRootError("application restore is incomplete")
    try:
        data = json.loads(read_regular(root / MARKER, 1024))
        if (set(data) != {"format", "root_id", "state"} or data["format"] != "tankai-root-v1"
                or str(UUID(data["root_id"])) != data["root_id"]
                or data["state"] not in {"active", "staged-restore"}):
            raise ValueError
        return data
    except (OSError, ValueError, TypeError, AttributeError):
        raise ApplicationRootError("invalid managed application root") from None


def marker_bytes(root_id: str, state: str) -> bytes:
    return json.dumps({"format":"tankai-root-v1", "root_id":root_id, "state":state}, sort_keys=True).encode()


def initialize_root(destination: Path) -> dict:
    """Create a NEW root only. Interrupted initialization stays blocked."""
    root = safe_path(destination)
    root.mkdir(mode=0o700)  # Deliberately no exist_ok and no adoption of old data.
    write_new(root / PENDING, b"initialization pending\n")
    root_id = str(uuid4())
    write_new(root / LOCK, b"0")
    write_new(root / MARKER, marker_bytes(root_id, "active"))
    (root / PENDING).unlink()
    _sync_directory(root)
    return root_metadata(root)


class RootLease:
    """Exclusive, nonblocking OS lock, shared by this web code and checkpointing.

    Local filesystem only. Older versions, raw database clients and administrators
    do not participate; this is not a distributed fencing token.
    """
    def __init__(self, root: Path):
        self.fd = None
        try:
            self.root = safe_path(root)
            self.root_identity = identity(self.root.lstat())
            before = regular(self.root / LOCK)
            self.fd = os.open(self.root / LOCK, os.O_RDWR | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
            self.lock_identity = identity(os.fstat(self.fd))
            if identity(before) != self.lock_identity or before.st_size != 1:
                raise ApplicationRootError("invalid application lock")
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.check()
        except BaseException:
            self.close()
            raise ApplicationRootError("application root is busy or unavailable") from None

    def check(self):
        if (identity(safe_path(self.root).lstat()) != self.root_identity
                or identity(regular(self.root / LOCK)) != self.lock_identity):
            raise ApplicationRootError("application root or lock was replaced")

    def close(self):
        if self.fd is not None:
            os.close(self.fd)  # Closing releases the kernel lock, including on process exit.
            self.fd = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def acquire_web_root(root: Path, *, auth_db: Path, owner_pilot, auth_mode: str):
    """Called before any auth restore/initialization can mutate a managed root."""
    root = Path(os.path.abspath(root))
    if not os.path.lexists(root / MARKER) and not os.path.lexists(root / PENDING):
        return None
    root = safe_path(root)
    if os.path.lexists(root / PENDING):
        raise ApplicationRootError("application restore is incomplete")
    lease = RootLease(root)
    try:
        metadata = root_metadata(root)
        if metadata["state"] != "active":
            raise ApplicationRootError("restored application root is staged; web startup is blocked")
        if (auth_mode != "session" or safe_path(auth_db) != root / "auth.db"
                or any(os.environ.get(name, "").strip() for name in (
                    "TANKAI_AUTH_STATE_URL", "TANKAI_AUTH_STATE_TOKEN", "TANKAI_BOOTSTRAP_OWNER"))
                or os.environ.get("TANKAI_DEV_QUEUE_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"}
                or os.environ.get("TANKAI_EMBEDDER", "hashing").strip() != "hashing"
                or (owner_pilot is not None and safe_path(owner_pilot.ledger.path) != root / "monetary.sqlite")):
            raise ApplicationRootError("managed pilot root requires local bounded storage configuration")
        return lease
    except BaseException:
        lease.close()
        raise
