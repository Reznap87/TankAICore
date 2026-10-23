"""Offline data snapshot and isolated restore; never overwrite an installation.

Stop all writers first. SQLite backup makes each DB consistent, but cannot
coordinate separate DBs, JSONL archives and the vector index by itself.
Configuration and provider secrets outside the data root are not included.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3

WORKSPACE = r"tenants/[0-9a-f-]{36}/workspaces/[0-9a-f-]{36}/"
MANIFEST = "snapshot.json"


def _allowed(name: str) -> bool:
    return name in {"auth.db", "development-jobs.db", "spend.db"} or bool(re.fullmatch(
        WORKSPACE + r"(?:memory\.db|ltm\.db|usage\.db|runs\.jsonl|web_history\.jsonl|vectors\.npz|cold/memory_archive\.jsonl)", name))


def _files(root: Path, *, sqlite_sidecars: bool = False) -> dict[str, Path]:
    files = {}
    for path in root.rglob("*"):
        if path.is_symlink() or path.is_junction():
            raise ValueError("Linked snapshot paths are not supported")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError("Nonregular snapshot source")
        name = path.relative_to(root).as_posix()
        # Sidecars are included in the SQLite transaction copied by backup().
        if sqlite_sidecars and name.endswith(("-wal", "-shm", "-journal")) and _allowed(name.rsplit("-", 1)[0]):
            continue
        if name != MANIFEST and not _allowed(name):
            raise ValueError("Unrecognized data file; snapshot would be incomplete")
        files[name] = path
    return files


def _digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _generation(root: Path) -> dict:
    files = _files(root, sqlite_sidecars=True)
    result = {name: (path.stat().st_size, _digest(path)) for name, path in files.items()}
    # SQLite readers can create/update SHM and empty WAL sidecars themselves.
    # Nonempty WAL/journals contain durable state and must remain unchanged.
    for path in root.rglob("*"):
        if path.is_file() and path.name.endswith(("-wal", "-journal")) and path.stat().st_size:
            result[path.relative_to(root).as_posix()] = (path.stat().st_size, _digest(path))
    return result


def _new_root(path: Path, source: Path) -> Path:
    # Check before resolve so a preexisting link cannot be hidden by resolve().
    if path.exists() or path.is_symlink() or path.is_junction():
        raise FileExistsError("Destination must be a new isolated directory")
    result = path.resolve()
    if result == source or source in result.parents or result in source.parents:
        raise ValueError("Source and destination must be separate")
    result.mkdir(mode=0o700)
    return result


def _integrity(path: Path) -> None:
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)) as connection:
        if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ValueError("Database integrity check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValueError("Database foreign-key check failed")


def create_snapshot(source: Path, destination: Path, *, offline: bool = False) -> dict:
    if not offline:
        raise ValueError("Stop every writer and explicitly select offline mode")
    if source.is_symlink() or source.is_junction():
        raise ValueError("Linked data root")
    source = source.resolve(strict=True)
    files = _files(source, sqlite_sidecars=True)
    if "auth.db" not in files or MANIFEST in files:
        raise ValueError("Expected application data root with auth.db")
    # Detect accidental writes during the offline operation, including sidecars.
    before = _generation(source)
    target = _new_root(destination, source)
    entries = []
    for name, path in sorted(files.items()):
        output = target / name
        output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.suffix == ".db":
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as original, closing(sqlite3.connect(output)) as copied:
                original.backup(copied)
                copied.execute("PRAGMA journal_mode=DELETE")
                copied.commit()
            _integrity(output)
        else:
            shutil.copyfile(path, output)
        output.chmod(0o600)
        entries.append({"path": name, "size": output.stat().st_size, "sha256": _digest(output)})
    after = _generation(source)
    if before != after:
        raise ValueError("Source changed; offline snapshot is incomplete")
    manifest = {"version": 1, "created_at": datetime.now(timezone.utc).isoformat(), "files": entries,
                "consistency": "offline", "secrets_outside_data_root_included": False}
    with (target / MANIFEST).open("x", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2)
    (target / MANIFEST).chmod(0o600)
    verify_snapshot(target)
    return {"verified": True, "files": len(entries), "database_files": sum(e["path"].endswith(".db") for e in entries)}


def verify_snapshot(source: Path) -> dict:
    if source.is_symlink() or source.is_junction():
        raise ValueError("Linked snapshot root")
    source = source.resolve(strict=True)
    files = _files(source)
    manifest = json.loads((source / MANIFEST).read_text(encoding="utf-8"))
    if manifest.get("version") != 1 or manifest.get("consistency") != "offline":
        raise ValueError("Unsupported snapshot contract")
    names = set()
    for entry in manifest["files"]:
        name = entry["path"]
        if (not isinstance(name, str) or not _allowed(name) or name in names
                or PurePosixPath(name).is_absolute() or ".." in PurePosixPath(name).parts):
            raise ValueError("Invalid snapshot path")
        names.add(name)
        path = files.get(name)
        if path is None or path.stat().st_size != entry["size"] or _digest(path) != entry["sha256"]:
            raise ValueError("Snapshot checksum mismatch")
        if path.suffix == ".db":
            _integrity(path)
    if "auth.db" not in names or set(files) != names | {MANIFEST}:
        raise ValueError("Snapshot file inventory mismatch")
    return manifest


def restore_snapshot(source: Path, destination: Path) -> dict:
    manifest = verify_snapshot(source)
    target = _new_root(destination, source.resolve())
    for entry in manifest["files"]:
        output = target / entry["path"]
        output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copyfile(source / entry["path"], output)
        output.chmod(0o600)
        if output.stat().st_size != entry["size"] or _digest(output) != entry["sha256"]:
            raise ValueError("Restore checksum mismatch")
        if output.suffix == ".db":
            _integrity(output)
    return {"restored": True, "files": len(manifest["files"]), "existing_installation_replaced": False}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("create", "verify", "restore"))
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path, nargs="?")
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()
    if args.operation == "verify":
        result = {"verified": True, "files": len(verify_snapshot(args.source)["files"])}
    elif args.destination is None:
        parser.error("A new destination is required")
    elif args.operation == "create":
        result = create_snapshot(args.source, args.destination, offline=args.offline)
    else:
        result = restore_snapshot(args.source, args.destination)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
