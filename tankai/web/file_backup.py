"""Operator-only encrypted CSV snapshots; no web route, key generation or network I/O."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import sys
import time
from uuid import UUID

from .file_jobs import CSV_SCHEMA, MAX_BYTES, MAX_STORED_BYTES, preview_csv

MAGIC = b"TANKAI-CSV-BACKUP\x00\x01"
AAD = MAGIC + b";AES-256-GCM;sqlite-v1"
MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024
MAX_ENCRYPTED_BYTES = MAX_SNAPSHOT_BYTES + len(MAGIC) + 12 + 16
SNAPSHOT_TIMEOUT_SECONDS = 15


class FileBackupError(ValueError):
    pass


def _cipher(key: bytes):
    if not isinstance(key, bytes) or len(key) != 32:
        raise FileBackupError("Ein binärer 256-Bit-Sicherungsschlüssel ist erforderlich")
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        raise FileBackupError("Bitte die optionalen requirements-backup.txt installieren") from None
    return AESGCM(key)


def _read_regular(path: Path, limit: int, *, key_file=False) -> bytes:
    path = Path(path)
    if path.is_symlink():
        raise FileBackupError("Verknüpfte Eingabedateien sind nicht erlaubt")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise FileBackupError("Eingabedatei ist ungültig oder zu groß")
        if key_file and os.name != "nt" and info.st_mode & 0o077:
            raise FileBackupError("Die Schlüsseldatei darf nur dem Besitzer zugänglich sein")
        data = handle.read(limit + 1)
        if len(data) > limit:
            raise FileBackupError("Eingabedatei ist zu groß")
        return data


def _schema(db):
    # Compare schema semantics, not formatting: older stores use different indentation.
    objects = list(db.execute("SELECT type,name,tbl_name FROM sqlite_schema ORDER BY type,name"))
    columns = list(db.execute('PRAGMA table_info("csv_jobs")'))
    indexes = sorted((row[1], row[2], row[3], row[4])
                     for row in db.execute('PRAGMA index_list("csv_jobs")'))
    indexed_columns = [(name, list(db.execute(f'PRAGMA index_info("{name}")')))
                       for name in ("sqlite_autoindex_csv_jobs_1", "sqlite_autoindex_csv_jobs_2")]
    return objects, columns, indexes, indexed_columns


def _validate_database(db) -> dict:
    """Only the bounded v1 CSV store can be restored, including immutable delivery bytes."""
    db.execute("PRAGMA trusted_schema=OFF")
    canonical = sqlite3.connect(":memory:")
    try:
        canonical.execute(CSV_SCHEMA)
        if _schema(db) != _schema(canonical):
            raise FileBackupError("Die Sicherung enthält keinen kompatiblen CSV-Auftragsspeicher")
    finally:
        canonical.close()
    if db.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
        raise FileBackupError("Die Datenbankprüfung ist fehlgeschlagen")
    count = db.execute("SELECT COUNT(*) FROM csv_jobs").fetchone()[0]
    if count > 100:
        raise FileBackupError("Die Sicherung enthält zu viele Aufträge")
    db.row_factory = sqlite3.Row
    grouped, total, completed = Counter(), 0, 0
    for row in db.execute("SELECT * FROM csv_jobs LIMIT 101"):
        try:
            for name in ("id", "tenant", "workspace", "owner", "request_id"):
                if not isinstance(row[name], str) or str(UUID(row[name])) != row[name]:
                    raise ValueError
            if (not isinstance(row["name"], str) or not row["name"].lower().endswith(".csv")
                    or not 1 <= len(row["name"]) <= 120
                    or re.search(r'[<>:"/\\|?*\x00-\x1f\x7f]', row["name"])):
                raise ValueError
            if not isinstance(row["goal"], str) or len(row["goal"]) > 1000:
                raise ValueError
            if not isinstance(row["created"], str) or not 1 <= len(row["created"]) <= 80:
                raise ValueError
            if type(row["revision"]) is not int or row["revision"] < 1:
                raise ValueError
            source = row["source"]
            if not isinstance(source, bytes) or not 0 < len(source) <= MAX_BYTES:
                raise ValueError
            if row["delimiter"] not in (",", ";", "\t"):
                raise ValueError
            if not isinstance(row["options"], str) or len(row["options"]) > 1000:
                raise ValueError
            detail, expected_csv = preview_csv(source, row["delimiter"], json.loads(row["options"]))
            if row["csv"] is None:
                if row["report"] is not None:
                    raise ValueError
            else:
                if not detail["valid"] or row["csv"] != expected_csv:
                    raise ValueError
                if not isinstance(row["report"], str) or len(row["report"]) > 10_000:
                    raise ValueError
                report = json.loads(row["report"])
                required = {**detail["report"], "jobId": row["id"], "revision": row["revision"],
                    "sourceFile": row["name"], "sourceSha256": hashlib.sha256(source).hexdigest(),
                    "csvSha256": hashlib.sha256(row["csv"]).hexdigest(), "csvByteLength": len(row["csv"]),
                    "encoding": "UTF-8 with BOM", "lineEnding": "CRLF",
                    "processing": "deterministic-csv-tool", "externalModelCalls": 0,
                    "semanticCorrectnessVerified": False}
                if not isinstance(report, dict) or any(report.get(k) != v for k, v in required.items()):
                    raise ValueError
                completed += 1
            grouped[(row["tenant"], row["workspace"], row["owner"])] += 1
            total += len(source) + (len(row["csv"]) if row["csv"] is not None else 0)
        except (ValueError, TypeError, UnicodeError, OverflowError):
            raise FileBackupError("Ein gespeicherter CSV-Auftrag ist ungültig") from None
    if total > MAX_STORED_BYTES or any(value > 10 for value in grouped.values()):
        raise FileBackupError("Die Sicherung überschreitet die Speicherbegrenzung")
    return {"jobs": count, "completedJobs": completed, "draftJobs": count - completed}


def _snapshot(source: Path) -> tuple[bytes, dict]:
    return _sqlite_snapshot(source, MAX_SNAPSHOT_BYTES, _validate_database)


def _sqlite_snapshot(source: Path, max_bytes: int, validator, *, deadline=None) -> tuple[bytes, dict]:
    source = Path(source)
    if source.is_symlink() or not source.is_file() or source.stat().st_size > max_bytes:
        raise FileBackupError("Die Quelldatenbank fehlt oder ist ungültig")
    if deadline is None:
        deadline = time.monotonic() + SNAPSHOT_TIMEOUT_SECONDS
    src = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    snapshot = sqlite3.connect(":memory:")
    try:
        src.execute("PRAGMA query_only=ON")
        page_size = src.execute("PRAGMA page_size").fetchone()[0]

        def progress(status, remaining, total):
            if time.monotonic() > deadline or total * page_size > max_bytes:
                raise FileBackupError("Die Momentaufnahme überschreitet ihre Zeit- oder Speichergrenze")

        src.backup(snapshot, pages=128, progress=progress, sleep=0.05)
        metadata = validator(snapshot)
        data = snapshot.serialize()
        if len(data) > max_bytes:
            raise FileBackupError("Die Momentaufnahme ist zu groß")
        # SQLite backup includes committed WAL pages. Its in-memory serialization can
        # still carry WAL header flags; SQLite explicitly requires rollback flags for
        # deserialize: https://www.sqlite.org/c3ref/deserialize.html
        if data[18:20] not in (b"\x01\x01", b"\x02\x02"):
            raise FileBackupError("Die Momentaufnahme hat ein unbekanntes Journalformat")
        data = data[:18] + b"\x01\x01" + data[20:]
        return data, metadata
    finally:
        snapshot.close()
        src.close()


def _decrypt(data: bytes, key: bytes) -> tuple[bytes, dict]:
    cipher = _cipher(key)
    if not len(MAGIC) + 28 < len(data) <= MAX_ENCRYPTED_BYTES or not data.startswith(MAGIC):
        raise FileBackupError("Unbekanntes oder beschädigtes Sicherungsformat")
    from cryptography.exceptions import InvalidTag
    try:
        payload = cipher.decrypt(data[len(MAGIC):len(MAGIC) + 12], data[len(MAGIC) + 12:], AAD)
    except InvalidTag:
        raise FileBackupError("Die Sicherung oder der Sicherungsschlüssel ist ungültig") from None
    if not payload.startswith(b"SQLite format 3\x00") or len(payload) > MAX_SNAPSHOT_BYTES:
        raise FileBackupError("Die entschlüsselte Datenbank ist ungültig")
    if payload[18:20] != b"\x01\x01":
        raise FileBackupError("Die Sicherung ist keine eigenständige Datenbank")
    db = sqlite3.connect(":memory:")
    try:
        db.deserialize(payload)
        metadata = _validate_database(db)
    finally:
        db.close()
    return payload, metadata


def _write_new(path: Path, data: bytes) -> None:
    """Never replace a file; remove only our own inode on an interrupted write."""
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    identity = os.fstat(descriptor)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            current = path.lstat()
            if (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino):
                path.unlink()
        except OSError:
            pass
        raise


def _sync_directory(path: Path) -> None:
    """Persist directory-entry changes before a success receipt on POSIX."""
    if os.name == "nt":
        # Python cannot open Windows directories for FlushFileBuffers. Directory
        # ACLs and crash-consistent metadata remain an operator/filesystem duty.
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(Path(path), flags)
    try:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise FileBackupError("Sicherungsmetadaten konnten nicht dauerhaft geschrieben werden")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _receipt(data, payload, metadata):
    return {"format": "tankai-csv-backup-v1", "cipher": "AES-256-GCM", **metadata,
            "encryptedBytes": len(data), "encryptedSha256": hashlib.sha256(data).hexdigest(),
            "snapshotBytes": len(payload), "snapshotSha256": hashlib.sha256(payload).hexdigest()}


def create_backup(source: Path, destination: Path, key: bytes) -> dict:
    cipher = _cipher(key)
    payload, metadata = _snapshot(source)
    nonce = secrets.token_bytes(12)
    data = MAGIC + nonce + cipher.encrypt(nonce, payload, AAD)
    destination = Path(destination)
    _write_new(destination, data)
    _sync_directory(destination.parent)
    # Verify persisted ciphertext rather than trusting an in-memory encryption buffer.
    saved = _read_regular(destination, MAX_ENCRYPTED_BYTES)
    verified, checked_metadata = _decrypt(saved, key)
    if verified != payload or checked_metadata != metadata:
        raise FileBackupError("Die gespeicherte Sicherung stimmt nicht mit der Momentaufnahme überein")
    return {"ok": True, "operation": "backup", **_receipt(saved, verified, metadata),
            "persistedCiphertextVerified": True}


def verify_backup(source: Path, key: bytes) -> dict:
    data = _read_regular(source, MAX_ENCRYPTED_BYTES)
    payload, metadata = _decrypt(data, key)
    return {"ok": True, "operation": "verify", **_receipt(data, payload, metadata)}


def restore_backup(source: Path, destination_directory: Path, key: bytes) -> dict:
    data = _read_regular(source, MAX_ENCRYPTED_BYTES)
    payload, metadata = _decrypt(data, key)
    directory = Path(destination_directory)
    # A new directory also rules out existing WAL/SHM/journal sidecars at the destination.
    directory.mkdir(mode=0o700)
    _sync_directory(directory.parent)
    destination = directory / "csv-jobs.db"
    _write_new(destination, payload)
    _sync_directory(directory)
    saved = _read_regular(destination, MAX_SNAPSHOT_BYTES)
    if saved != payload:
        raise FileBackupError("Die zurückgespielte Datenbank stimmt nicht mit der Sicherung überein")
    return {"ok": True, "operation": "restore", **_receipt(data, saved, metadata),
            "restoredDatabaseVerified": True}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Verschlüsselte Sicherung privater CSV-Dateiaufträge")
    parser.add_argument("operation", choices=("backup", "verify", "restore"))
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--key-file", required=True, type=Path,
                        help="Geschützte Datei mit genau 32 binären Schlüsselbytes")
    parser.add_argument("--destination", type=Path,
                        help="Neue Sicherungsdatei oder neuer Restore-Ordner")
    args = parser.parse_args(argv)
    if (args.operation == "verify") != (args.destination is None):
        parser.error("backup/restore benötigen --destination; verify verwendet kein Ziel")
    try:
        key = _read_regular(args.key_file, 32, key_file=True)
        if args.operation == "backup":
            result = create_backup(args.source, args.destination, key)
        elif args.operation == "verify":
            result = verify_backup(args.source, key)
        else:
            result = restore_backup(args.source, args.destination, key)
    except FileBackupError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    except (OSError, sqlite3.Error):
        print(json.dumps({"ok": False, "error": "Sicherung fehlgeschlagen: Dateizugriff oder Datenbank ungültig"}), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
