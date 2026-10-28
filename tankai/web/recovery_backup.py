"""Encrypted, coordinated account + CSV recovery for the single-host private pilot."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import stat
import struct
import sys
import time

from . import file_backup as csv_backup
from .auth_state import (MAX_SNAPSHOT as MAX_AUTH_BYTES, RESTORE_PENDING_FILE, RECOVERY_RECEIPT_FILE,
                         _REQUIRED_AUTH_COLUMNS, _validate_auth_connection)

MAGIC = b"TANKAI-ACCOUNT-CSV-RECOVERY\x00\x01"
AAD = MAGIC + b";AES-256-GCM;auth-v3;csv-v1"
LENGTHS = struct.Struct(">II")
MAX_PAYLOAD = LENGTHS.size + MAX_AUTH_BYTES + csv_backup.MAX_SNAPSHOT_BYTES
MAX_ENCRYPTED_BYTES = len(MAGIC) + 12 + MAX_PAYLOAD + 16
WRITE_BARRIER_SECONDS = 5
SNAPSHOT_SECONDS = 15
RECEIPT_FILE = RECOVERY_RECEIPT_FILE


def _validate_auth(db):
    db.execute("PRAGMA trusted_schema=OFF")
    _validate_auth_connection(db)
    tables = {row[0] for row in db.execute(
        "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT GLOB 'sqlite_*'")}
    if tables != set(_REQUIRED_AUTH_COLUMNS) or db.execute(
            "SELECT 1 FROM sqlite_schema WHERE type IN ('view','trigger') LIMIT 1").fetchone():
        raise csv_backup.FileBackupError("Unbekannte Objekte im Kontenspeicher")
    return {"users": db.execute("SELECT COUNT(*) FROM users").fetchone()[0],
            "workspaces": db.execute("SELECT COUNT(*) FROM workspaces").fetchone()[0]}


def _decode_payload(payload):
    if not LENGTHS.size < len(payload) <= MAX_PAYLOAD:
        raise csv_backup.FileBackupError("Ungültige Größe des Wiederherstellungspakets")
    auth_length, csv_length = LENGTHS.unpack_from(payload)
    if (not 0 < auth_length <= MAX_AUTH_BYTES or not 0 < csv_length <= csv_backup.MAX_SNAPSHOT_BYTES
            or len(payload) != LENGTHS.size + auth_length + csv_length):
        raise csv_backup.FileBackupError("Ungültige Bestandteile des Wiederherstellungspakets")
    auth_data = payload[LENGTHS.size:LENGTHS.size + auth_length]
    csv_data = payload[LENGTHS.size + auth_length:]
    for data in (auth_data, csv_data):
        if not data.startswith(b"SQLite format 3\x00") or data[18:20] != b"\x01\x01":
            raise csv_backup.FileBackupError("Die Sicherung enthält keine eigenständigen Datenbanken")
    auth_db, csv_db = sqlite3.connect(":memory:"), sqlite3.connect(":memory:")
    try:
        auth_db.deserialize(auth_data)
        csv_db.deserialize(csv_data)
        metadata = {**_validate_auth(auth_db), **csv_backup._validate_database(csv_db)}
        for row in csv_db.execute("SELECT DISTINCT tenant,workspace,owner FROM csv_jobs"):
            known = auth_db.execute("""SELECT 1 FROM workspaces w JOIN users u ON u.id=?
                WHERE w.id=? AND w.tenant_id=?""", (row[2], row[1], row[0])).fetchone()
            if known is None:
                raise csv_backup.FileBackupError("Dateiaufträge und Kontenzuordnung passen nicht zusammen")
        return auth_data, csv_data, metadata
    finally:
        auth_db.close()
        csv_db.close()


@contextmanager
def _write_barriers(paths):
    """Take write reservations in a fixed order; copy using separate read connections."""
    guards = []
    deadline = time.monotonic() + WRITE_BARRIER_SECONDS
    try:
        for path in paths:
            remaining = max(0, deadline - time.monotonic())
            db = sqlite3.connect(path.resolve().as_uri() + "?mode=rw", uri=True, timeout=remaining)
            guards.append(db)
            db.execute("BEGIN IMMEDIATE")
        yield
    finally:
        for db in reversed(guards):
            try:
                if db.in_transaction:
                    db.rollback()
            finally:
                db.close()


def _snapshot_pair(root):
    root = Path(root)
    if root.is_symlink() or not root.is_dir() or (root / RESTORE_PENDING_FILE).exists():
        raise csv_backup.FileBackupError("Ungültiger oder unvollständiger Quelldatenbestand")
    auth_path, csv_path = root / "auth.db", root / "csv-jobs.db"
    for path, limit in ((auth_path, MAX_AUTH_BYTES), (csv_path, csv_backup.MAX_SNAPSHOT_BYTES)):
        if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
            raise csv_backup.FileBackupError("Konten- oder Dateiauftragsspeicher fehlt oder ist ungültig")
    if auth_path.samefile(csv_path):
        raise csv_backup.FileBackupError("Konten und Dateiaufträge benötigen getrennte Datenbanken")
    with _write_barriers((auth_path, csv_path)):
        deadline = time.monotonic() + SNAPSHOT_SECONDS
        auth_data, _ = csv_backup._sqlite_snapshot(auth_path, MAX_AUTH_BYTES, _validate_auth, deadline=deadline)
        csv_data, _ = csv_backup._sqlite_snapshot(csv_path, csv_backup.MAX_SNAPSHOT_BYTES,
                                                csv_backup._validate_database, deadline=deadline)
    payload = LENGTHS.pack(len(auth_data), len(csv_data)) + auth_data + csv_data
    _, _, metadata = _decode_payload(payload)
    return payload, metadata


def _decrypt(data, key):
    cipher = csv_backup._cipher(key)
    if not len(MAGIC) + 28 < len(data) <= MAX_ENCRYPTED_BYTES or not data.startswith(MAGIC):
        raise csv_backup.FileBackupError("Unbekanntes oder beschädigtes Wiederherstellungsformat")
    from cryptography.exceptions import InvalidTag
    try:
        payload = cipher.decrypt(data[len(MAGIC):len(MAGIC) + 12], data[len(MAGIC) + 12:], AAD)
    except InvalidTag:
        raise csv_backup.FileBackupError("Wiederherstellungspaket oder Schlüssel ist ungültig") from None
    auth_data, csv_data, metadata = _decode_payload(payload)
    return payload, auth_data, csv_data, metadata


def _receipt(data, auth_data, csv_data, metadata):
    return {"format": "tankai-account-csv-recovery-v1", "cipher": "AES-256-GCM", **metadata,
            "encryptedBytes": len(data), "encryptedSha256": hashlib.sha256(data).hexdigest(),
            "authSnapshotSha256": hashlib.sha256(auth_data).hexdigest(),
            "csvSnapshotSha256": hashlib.sha256(csv_data).hexdigest()}


def create_backup(root, destination, key):
    cipher = csv_backup._cipher(key)
    payload, _ = _snapshot_pair(root)
    nonce = secrets.token_bytes(12)
    data = MAGIC + nonce + cipher.encrypt(nonce, payload, AAD)
    destination = Path(destination)
    csv_backup._write_new(destination, data)
    csv_backup._sync_directory(destination.parent)
    saved = csv_backup._read_regular(destination, MAX_ENCRYPTED_BYTES)
    verified, auth_data, csv_data, metadata = _decrypt(saved, key)
    if verified != payload:
        raise csv_backup.FileBackupError("Die gespeicherte Sicherung stimmt nicht mit der Momentaufnahme überein")
    return {"ok": True, "operation": "backup", **_receipt(saved, auth_data, csv_data, metadata),
            "persistedCiphertextVerified": True, "coordinatedWriteBarrier": True}


def verify_backup(source, key):
    data = csv_backup._read_regular(Path(source), MAX_ENCRYPTED_BYTES)
    _, auth_data, csv_data, metadata = _decrypt(data, key)
    return {"ok": True, "operation": "verify", **_receipt(data, auth_data, csv_data, metadata)}


def _revoke_snapshot_access(auth_data):
    db = sqlite3.connect(":memory:")
    try:
        db.deserialize(auth_data)
        timestamp = datetime.now(timezone.utc).isoformat()
        with db:
            sessions = db.execute("UPDATE sessions SET revoked_at=? WHERE revoked_at IS NULL", (timestamp,)).rowcount
            tokens = db.execute("UPDATE agent_tokens SET revoked_at=? WHERE revoked_at IS NULL", (timestamp,)).rowcount
        _validate_auth(db)
        return db.serialize(), {"sessionsInvalidated": sessions, "agentTokensInvalidated": tokens}
    finally:
        db.close()


def _verify_restored_file(path, expected, limit, identity, label):
    def require_identity():
        try:
            current = path.lstat()
        except OSError:
            raise csv_backup.FileBackupError(f"{label} wurde vor Abschluss entfernt") from None
        if (not stat.S_ISREG(current.st_mode)
                or csv_backup._write_identity(current) != identity
                or (os.name != "nt" and current.st_nlink != 1)):
            raise csv_backup.FileBackupError(f"{label} wurde vor Abschluss ersetzt")

    require_identity()
    restored = csv_backup._read_regular(path, limit)
    require_identity()
    if restored != expected:
        raise csv_backup.FileBackupError(f"{label} stimmt nicht mit dem Wiederherstellungspaket überein")


def restore_backup(source, destination, key):
    data = csv_backup._read_regular(Path(source), MAX_ENCRYPTED_BYTES)
    _, auth_data, csv_data, metadata = _decrypt(data, key)
    restored_auth, access = _revoke_snapshot_access(auth_data)
    directory = Path(destination)
    directory.mkdir(mode=0o700)
    csv_backup._sync_directory(directory.parent)
    pending = directory / RESTORE_PENDING_FILE
    pending_identity = csv_backup._write_new(pending, b"account-csv-recovery-v1\n")
    auth_path, csv_path = directory / "auth.db", directory / "csv-jobs.db"
    auth_identity = csv_backup._write_new(auth_path, restored_auth)
    csv_identity = csv_backup._write_new(csv_path, csv_data)
    csv_backup._sync_directory(directory)
    _verify_restored_file(auth_path, restored_auth, MAX_AUTH_BYTES, auth_identity,
                          "Die wiederhergestellte Kontendatenbank")
    _verify_restored_file(csv_path, csv_data, csv_backup.MAX_SNAPSHOT_BYTES, csv_identity,
                          "Die wiederhergestellte Dateiauftragsdatenbank")
    result = {"ok": True, "operation": "restore", **_receipt(data, auth_data, csv_data, metadata),
              **access, "restoredAuthSha256": hashlib.sha256(restored_auth).hexdigest(),
              "restoredDatabasesVerified": True, "freshLoginRequired": True}
    receipt_path = directory / RECEIPT_FILE
    receipt_data = (json.dumps(result) + "\n").encode()
    receipt_identity = csv_backup._write_new(receipt_path, receipt_data)
    csv_backup._sync_directory(directory)
    _verify_restored_file(receipt_path, receipt_data, len(receipt_data), receipt_identity,
                          "Das Recovery-Receipt")
    # The receipt write is a separate durability boundary. Recheck both databases
    # afterwards so the pending marker cannot be removed for a changed artifact set.
    _verify_restored_file(auth_path, restored_auth, MAX_AUTH_BYTES, auth_identity,
                          "Die wiederhergestellte Kontendatenbank")
    _verify_restored_file(csv_path, csv_data, csv_backup.MAX_SNAPSHOT_BYTES, csv_identity,
                          "Die wiederhergestellte Dateiauftragsdatenbank")
    try:
        current_pending = pending.lstat()
    except OSError:
        raise csv_backup.FileBackupError("Recovery-Pending-Marker wurde vor Abschluss entfernt") from None
    if (not stat.S_ISREG(current_pending.st_mode)
            or csv_backup._write_identity(current_pending) != pending_identity
            or (os.name != "nt" and current_pending.st_nlink != 1)):
        raise csv_backup.FileBackupError("Recovery-Pending-Marker wurde vor Abschluss ersetzt")
    pending.unlink()
    csv_backup._sync_directory(directory)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="Gemeinsame verschlüsselte Sicherung von Konten und CSV-Aufträgen")
    parser.add_argument("operation", choices=("backup", "verify", "restore"))
    parser.add_argument("--source", required=True, type=Path,
                        help="Bei backup: Datenordner mit auth.db und csv-jobs.db; sonst Sicherungsdatei")
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--key-file", required=True, type=Path)
    args = parser.parse_args(argv)
    if (args.operation == "verify") != (args.destination is None):
        parser.error("backup/restore benötigen --destination; verify verwendet kein Ziel")
    try:
        key = csv_backup._read_regular(args.key_file, 32, key_file=True)
        if args.operation == "backup":
            result = create_backup(args.source, args.destination, key)
        elif args.operation == "verify":
            result = verify_backup(args.source, key)
        else:
            result = restore_backup(args.source, args.destination, key)
    except (csv_backup.FileBackupError, RuntimeError, OSError, sqlite3.Error):
        print(json.dumps({"ok": False, "error": "Konten und Dateiaufträge konnten nicht sicher gesichert oder wiederhergestellt werden"}), file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
