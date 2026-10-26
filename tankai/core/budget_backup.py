"""Optional operator-only encrypted monetary snapshots, never a live cutover.

Uses the same AES-256-GCM primitive and optional dependency as the account/CSV
draft, with a distinct authenticated monetary format. No web route, key-file
discovery/generation, scheduler, remote upload or source-ledger replacement.
"""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat

from .monetary_budget import (
    BudgetStateError, MAX_LEDGER_BYTES, MonetaryBudget, PilotPolicy, _sync_directory,
)


MAGIC = b"TANKAI-MONETARY-BACKUP\x00\x01"
AAD = MAGIC + b";AES-256-GCM;monetary-policy-v1;"
MAX_ENCRYPTED_BYTES = len(MAGIC) + 12 + MAX_LEDGER_BYTES + 16


class BudgetBackupError(ValueError):
    """Fixed diagnostics contain no keys, contents, identity values or paths."""


def _cipher(key: bytes):
    if type(key) is not bytes or len(key) != 32:
        raise BudgetBackupError("a binary 256-bit recovery key is required")
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        raise BudgetBackupError("optional requirements-backup.txt are required") from None
    return AESGCM(key)


def _policy_context(policy: PilotPolicy) -> bytes:
    if type(policy) is not PilotPolicy:
        raise BudgetBackupError("an independently approved pilot policy is required")
    canonical = json.dumps(asdict(policy), sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).digest()


def _read_regular(path: Path) -> bytes:
    path = Path(path)
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= MAX_ENCRYPTED_BYTES:
        raise BudgetBackupError("bounded regular encrypted backup required")
    if os.name != "nt" and before.st_nlink != 1:
        raise BudgetBackupError("exclusively named encrypted backup required")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags)
    def identity(info):
        # Windows lstat and descriptor stat can expose different legacy ctime
        # meanings (creation vs change time). File ID/size/mtime remain checked;
        # POSIX additionally compares ctime. The pinned hash and GCM tag cover bytes.
        return (info.st_dev, info.st_ino, info.st_nlink, info.st_size, info.st_mtime_ns,
                info.st_ctime_ns if os.name != "nt" else 0)
    with os.fdopen(descriptor, "rb") as handle:
        opened = os.fstat(handle.fileno())
        if not stat.S_ISREG(opened.st_mode) or identity(opened) != identity(before):
            raise BudgetBackupError("encrypted backup changed before reading")
        data = handle.read(MAX_ENCRYPTED_BYTES + 1)
        after = os.fstat(handle.fileno())
        current = path.lstat()
        if (len(data) != opened.st_size or identity(after) != identity(opened)
                or not stat.S_ISREG(current.st_mode) or identity(current) != identity(opened)):
            raise BudgetBackupError("encrypted backup changed during reading")
    return data


def _receipt(data: bytes, snapshot: bytes, policy: PilotPolicy) -> dict:
    return {"format":"tankai-monetary-backup-v1", "cipher":"AES-256-GCM",
            "encrypted_bytes":len(data), "encrypted_sha256":hashlib.sha256(data).hexdigest(),
            "snapshot_bytes":len(snapshot), "snapshot_sha256":hashlib.sha256(snapshot).hexdigest(),
            "policy_sha256":_policy_context(policy).hex()}


def _decode(source: Path, key: bytes, expected_sha256: str,
            expected_policy: PilotPolicy) -> tuple[bytes, dict]:
    cipher = _cipher(key)
    context = _policy_context(expected_policy)
    # No implicit trust in a digest packaged alongside an untrusted file.
    if type(expected_sha256) is not str or re.fullmatch(r"[a-f0-9]{64}", expected_sha256) is None:
        raise BudgetBackupError("an independently trusted encrypted-file digest is required")
    data = _read_regular(source)
    if (not len(MAGIC) + 12 + 16 + 100 <= len(data) <= MAX_ENCRYPTED_BYTES
            or not data.startswith(MAGIC) or hashlib.sha256(data).hexdigest() != expected_sha256):
        raise BudgetBackupError("invalid pinned encrypted monetary backup")
    from cryptography.exceptions import InvalidTag
    try:
        snapshot = cipher.decrypt(data[len(MAGIC):len(MAGIC)+12], data[len(MAGIC)+12:], AAD+context)
    except InvalidTag:
        raise BudgetBackupError("backup key, policy or authenticated content is invalid") from None
    if (not 100 <= len(snapshot) <= MAX_LEDGER_BYTES
            or not snapshot.startswith(b"SQLite format 3\x00")):
        raise BudgetBackupError("invalid monetary snapshot payload")
    MonetaryBudget._check_snapshot(snapshot, expected_policy)
    return snapshot, _receipt(data, snapshot, expected_policy)


def create_backup(ledger: MonetaryBudget, destination: Path, key: bytes) -> dict:
    """Save ciphertext only as the permanent artifact and verify it from disk.

    Ledger validation still uses private temporary SQLite copies; this is not a
    claim that plaintext never reaches scratch storage. No deployment key is
    generated and no ledger/source configuration changes.
    """
    if type(ledger) is not MonetaryBudget:
        raise BudgetBackupError("an initialized monetary ledger is required")
    cipher = _cipher(key)
    policy = ledger.policy
    context = _policy_context(policy)
    try:
        snapshot = ledger.export_snapshot()
        if ledger.policy != policy:
            raise BudgetBackupError("pilot policy changed during backup")
        nonce = secrets.token_bytes(12)
        data = MAGIC + nonce + cipher.encrypt(nonce, snapshot, AAD+context)
        destination = Path(destination)
        descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        _sync_directory(destination.parent)
        digest = hashlib.sha256(data).hexdigest()
        recovered, receipt = _decode(destination, key, digest, policy)
        if recovered != snapshot:
            raise BudgetBackupError("persisted ciphertext does not match captured snapshot")
        return {"ok":True, "operation":"backup", **receipt, "persisted_ciphertext_verified":True}
    except (OSError, sqlite3.Error, BudgetStateError):
        # A failed write may leave ciphertext for diagnosis; never replace it
        # on retry or silently acknowledge an unverified backup.
        raise BudgetBackupError("encrypted monetary backup failed closed") from None


def verify_backup(source: Path, key: bytes, *, expected_sha256: str,
                  expected_policy: PilotPolicy) -> dict:
    """Authenticate and validate without creating a recovered destination."""
    try:
        _, receipt = _decode(source, key, expected_sha256, expected_policy)
        return {"ok":True, "operation":"verify", **receipt}
    except (OSError, sqlite3.Error, BudgetStateError):
        raise BudgetBackupError("encrypted monetary verification failed closed") from None


def restore_backup(source: Path, destination: Path, key: bytes, *, expected_sha256: str,
                   expected_policy: PilotPolicy, clock=None) -> dict:
    """Validate before restore_new; never replace, renew or unlock a ledger."""
    try:
        snapshot, receipt = _decode(source, key, expected_sha256, expected_policy)
        with MonetaryBudget.restore_new(destination, data=snapshot,
                expected_sha256=receipt["snapshot_sha256"], expected_policy=expected_policy, clock=clock):
            pass
        return {"ok":True, "operation":"restore", **receipt, "restored_state_verified":True}
    except (OSError, sqlite3.Error, BudgetStateError):
        raise BudgetBackupError("encrypted monetary restore failed closed") from None
