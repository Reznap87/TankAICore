"""Encrypted common generation for a bounded, stopped, managed owner pilot.

Operator-only Python API. No routes, key discovery, uploads, root adoption or
activation of restored budgets. Plain SQLite validation uses private scratch.
"""
from __future__ import annotations

import base64
from contextlib import ExitStack, closing
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import tempfile
import time
from uuid import uuid4
import zipfile

from tankai.core.budget_backup import _cipher
from tankai.core.monetary_budget import MonetaryBudget, PilotPolicy, _sync_directory
from tankai.web.application_root import (
    ApplicationRootError, LOCK, MARKER, PENDING, RootLease, identity, initialize_root,
    marker_bytes, read_regular, regular, root_metadata, safe_path, write_new,
)
from tankai.web.auth_state import _REQUIRED_AUTH_COLUMNS, _validate_snapshot
from tankai.web.source_manifest import load_source_manifest

MAGIC = b"TANKAI-APPLICATION-CHECKPOINT\x00\x01"
MAX_FILE = 8 * 1024 * 1024
MAX_TOTAL = 32 * 1024 * 1024
MAX_ENCRYPTED = 46 * 1024 * 1024
MAX_FILES = 256
MAX_VECTOR_ROWS = 4096
UUID_RE = r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}"
WORKSPACE = re.compile(rf"tenants/({UUID_RE})/workspaces/({UUID_RE})/(memory.db|ltm.db|vectors.npz|runs.jsonl|web_history.jsonl|cold/memory_archive.jsonl)")
SOURCE = re.compile(rf"sources/({UUID_RE})\.json")
DIRECTORY = re.compile(rf"(?:sources|tenants|tenants/{UUID_RE}|tenants/{UUID_RE}/workspaces|tenants/{UUID_RE}/workspaces/{UUID_RE}(?:/cold)?)")
WORKSPACE_COLUMNS = {
    "memory": "id content source validity confidence related_goal_id conflicts_with created_at metadata",
    "runs": "id goal_description definition_of_done status final_answer duration_seconds created_at metadata",
    "receipts": "id run_id action actor input_summary output_summary success details timestamp",
    "memory_entries": "id content source memory_type validity confidence related_goal_id related_run_id conflicts_with provenance created_at last_accessed access_count retention_policy metadata",
}


class CheckpointError(ValueError):
    """Diagnostics contain no keys, paths, account identifiers or data."""


def _need(condition, diagnostic):
    if not condition:
        raise CheckpointError(diagnostic)


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _object(pairs):
    result = {}
    for name, value in pairs:
        _need(name not in result, "duplicate checkpoint field")
        result[name] = value
    return result


def _parse(data):
    def constant(_):
        raise CheckpointError("nonfinite JSON value")
    return json.loads(data, object_pairs_hook=_object, parse_constant=constant)


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _context(root_id, policy, source_pins):
    from uuid import UUID
    _need(type(root_id) is str and str(UUID(root_id)) == root_id, "trusted root identity required")
    _need(type(policy) is PilotPolicy and type(source_pins) is dict and 1 <= len(source_pins) <= 32,
          "independent pilot policy and source pins required")
    for path, digest in source_pins.items():
        _need(type(path) is str and SOURCE.fullmatch(path) is not None
              and type(digest) is str and re.fullmatch(r"[a-f0-9]{64}", digest), "invalid source pin contract")
    return _json({"root_id":root_id, "policy":asdict(policy), "source_pins":source_pins})


def _inventory(root):
    files, directories = {}, []
    for parent, names, filenames in os.walk(root, followlinks=False):
        _need(len(files) + len(directories) <= MAX_FILES * 2, "application tree exceeds capacity")
        for name in names:
            path = Path(parent) / name
            info = safe_path(path).lstat()
            relative = path.relative_to(root).as_posix()
            _need(stat.S_ISDIR(info.st_mode) and DIRECTORY.fullmatch(relative), "unsupported application directory")
            directories.append(relative)
        for name in filenames:
            path = Path(parent) / name
            info = regular(path)
            relative = path.relative_to(root).as_posix()
            if relative in {MARKER, LOCK}:
                continue
            # Recognized SQLite sidecars are absorbed by SQLite's committed backup.
            base = re.sub(r"-(wal|shm|journal)$", "", relative)
            match = WORKSPACE.fullmatch(base)
            _need(base in {"auth.db", "monetary.sqlite"} or SOURCE.fullmatch(relative)
                  or (match and (base == relative or match[3].endswith(".db"))),
                  "unsupported application file")
            _need(info.st_size <= MAX_FILE, "application file exceeds capacity")
            if base != relative:
                _need((root / base).is_file(), "orphan SQLite sidecar")
                continue
            files[relative] = identity(info)
    _need(len(files) <= MAX_FILES and sum((root / p).stat().st_size for p in files) <= MAX_TOTAL,
          "application data exceeds capacity")
    return files, sorted(directories)


def _sqlite_image(path):
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)) as source, \
            closing(sqlite3.connect(":memory:")) as image:
        page_size = source.execute("PRAGMA page_size").fetchone()[0]
        deadline = time.monotonic() + 5
        def progress(status, remaining, total):
            _need(total * page_size <= MAX_FILE and time.monotonic() <= deadline,
                  "SQLite capture capacity or deadline exceeded")
        source.backup(image, pages=128, progress=progress, sleep=0.01)
        data = image.serialize()
        _need(len(data) <= MAX_FILE, "SQLite image exceeds capacity")
        # SQLite backup copied all committed pages. Standalone images must not
        # ask deserialize/restore to find a WAL belonging to the old host.
        return data[:18] + b"\x01\x01" + data[20:]


def _database(data):
    _need(data.startswith(b"SQLite format 3\x00") and data[18:20] == b"\x01\x01",
          "standalone SQLite image required")
    db = sqlite3.connect(":memory:")
    try:
        db.deserialize(data)
        db.execute("PRAGMA trusted_schema=OFF")
        _need(db.execute("PRAGMA quick_check").fetchone() == ("ok",)
              and not db.execute("PRAGMA foreign_key_check").fetchone()
              and not db.execute("SELECT 1 FROM sqlite_schema WHERE type IN ('trigger','view')").fetchone(),
              "invalid database integrity or executable schema")
        return db
    except BaseException:
        db.close()
        raise


def _vectors(data):
    import numpy as np
    from tankai.core.vector_store import VectorStore
    expected = {"format_version", "dim", "ids", "vectors", "metadatas"}
    # Check both decompressed sizes and NPY shapes BEFORE numpy allocates arrays.
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        members = archive.infolist()
        _need(len(members) == 5 and {m.filename for m in members} == {n + ".npy" for n in expected}
              and sum(m.file_size for m in members) <= MAX_FILE, "invalid vector archive capacity")
        for member in members:
            with archive.open(member) as handle:
                version = np.lib.format.read_magic(handle)
                _need(version in {(1, 0), (2, 0)}, "unsupported vector array format")
                reader = np.lib.format.read_array_header_1_0 if version == (1, 0) else np.lib.format.read_array_header_2_0
                shape, fortran, dtype = reader(handle, max_header_size=8192)
                _need(not dtype.hasobject and dtype.itemsize > 0 and len(shape) <= 2
                      and all(type(n) is int and 0 <= n <= MAX_FILE for n in shape)
                      and math.prod(shape) * dtype.itemsize == member.file_size - handle.tell(),
                      "invalid vector array capacity")
                name = member.filename[:-4]
                if name in {"format_version", "dim"}:
                    _need(shape == (1,) and dtype == np.dtype("int64"), "invalid vector scalar header")
                elif name in {"ids", "metadatas"}:
                    _need(len(shape) == 1 and shape[0] <= MAX_VECTOR_ROWS and dtype.kind == "U",
                          "invalid vector text header")
                else:
                    _need(len(shape) == 2 and shape[0] <= MAX_VECTOR_ROWS and shape[1] == 384
                          and dtype == np.dtype("float32"), "invalid vector matrix header")
    with np.load(io.BytesIO(data), allow_pickle=False) as arrays:
        version, dim = arrays["format_version"], arrays["dim"]
        ids, vectors, metadata = arrays["ids"], arrays["vectors"], arrays["metadatas"]
        _need(version.shape == dim.shape == (1,) and int(version[0]) == VectorStore.FORMAT_VERSION and int(dim[0]) == 384
              and ids.ndim == metadata.ndim == 1 and vectors.shape == (len(ids), 384)
              and ids.dtype.kind == metadata.dtype.kind == "U" and vectors.dtype == np.dtype("float32")
              and len(metadata) == len(ids) and np.isfinite(vectors).all()
              and len(set(ids.tolist())) == len(ids), "incompatible vector data")
        _need(all(type(_parse(item)) is dict for item in metadata.tolist()), "invalid vector metadata")


def _validate(files, directories, policy, pins):
    _need({"auth.db", "monetary.sqlite"}.issubset(files)
          and {p for p in files if SOURCE.fullmatch(p)} == set(pins), "missing checkpoint components")
    _need(len(files) <= MAX_FILES and sum(map(len, files.values())) <= MAX_TOTAL
          and all(type(v) is bytes and len(v) <= MAX_FILE for v in files.values()), "checkpoint exceeds capacity")
    with tempfile.TemporaryDirectory(prefix="tankai-application-check-") as temporary:
        auth_path = Path(temporary) / "auth.db"
        auth_path.write_bytes(files["auth.db"])
        _validate_snapshot(str(auth_path))
        MonetaryBudget._check_snapshot(files["monetary.sqlite"], policy)
        with closing(_database(files["auth.db"])) as auth:
            tables = {r[0] for r in auth.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name NOT GLOB 'sqlite_*'")}
            _need(tables == set(_REQUIRED_AUTH_COLUMNS), "unsupported account schema")
            workspaces = set(auth.execute("SELECT tenant_id,id FROM workspaces"))
            owner = auth.execute("SELECT 1 FROM users WHERE id=?", (policy.owner_id,)).fetchone()
            _need(owner, "budget owner missing from accounts")
            for name, data in files.items():
                source = SOURCE.fullmatch(name)
                match = WORKSPACE.fullmatch(name)
                _need(name in {"auth.db", "monetary.sqlite"} or source or match, "invalid checkpoint path")
                if source:
                    _need(_digest(data) == pins[name], "source approval digest mismatch")
                    source_path = Path(temporary) / source[1]
                    source_path.write_bytes(data)
                    grant = load_source_manifest(source_path, expected_sha256=pins[name])
                    _need(grant.workspace_id == source[1] and grant.owner_id == policy.owner_id
                          and (grant.tenant_id, grant.workspace_id) in workspaces
                          and policy.starts_at <= grant.valid_from < grant.valid_until <= policy.expires_at,
                          "source approval scope mismatch")
                    _need(auth.execute("SELECT 1 FROM memberships WHERE user_id=? AND workspace_id=? AND role='owner'",
                                       (grant.owner_id, grant.workspace_id)).fetchone(), "source owner membership missing")
                if match:
                    _need((match[1], match[2]) in workspaces, "orphan workspace data")
                    base = name.rsplit("/", 1)[0]
                    if match[3] == "cold/memory_archive.jsonl":
                        base = base.rsplit("/", 1)[0]
                    _need(base + "/memory.db" in files and base + "/ltm.db" in files,
                          "incomplete workspace databases")
                    if name.endswith(".db"):
                        with closing(_database(data)) as db:
                            expected = {"memory"} if match[3] == "memory.db" else {"runs", "receipts", "memory_entries"}
                            actual = {r[0] for r in db.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name NOT GLOB 'sqlite_*'")}
                            _need(actual == expected, "unsupported workspace schema")
                            for table in expected:
                                columns = {row[1] for row in db.execute(f'PRAGMA table_info("{table}")')}
                                _need(columns == set(WORKSPACE_COLUMNS[table].split()), "incompatible workspace columns")
                    elif name.endswith(".npz"):
                        _vectors(data)
                    else:
                        _need(all(type(_parse(line)) is dict for line in data.decode("utf-8").splitlines()),
                              "invalid workspace JSONL")
            for directory in directories:
                _need(type(directory) is str and DIRECTORY.fullmatch(directory), "invalid checkpoint directory")
                match = re.fullmatch(rf"tenants/({UUID_RE})/workspaces/({UUID_RE})(?:/cold)?", directory)
                if match:
                    _need((match[1], match[2]) in workspaces, "orphan workspace directory")


def _receipt(data, manifest):
    return {"ok":True, "format":"tankai-application-checkpoint-v1", "cipher":"AES-256-GCM",
            "encrypted_sha256":_digest(data), "encrypted_bytes":len(data),
            "generation_id":manifest["generation_id"], "files":len(manifest["files"]),
            "manifest_sha256":_digest(_json(manifest)), "production_ready":False,
            "scope":"single-managed-root", "freshness_verified":False}


def _decode(source, key, expected_sha256, root_id, policy, pins):
    context = _context(root_id, policy, pins)
    cipher = _cipher(key)
    _need(type(expected_sha256) is str and re.fullmatch(r"[a-f0-9]{64}", expected_sha256),
          "independent encrypted digest required")
    data = read_regular(Path(source), MAX_ENCRYPTED)
    _need(data.startswith(MAGIC) and _digest(data) == expected_sha256, "checkpoint pin mismatch")
    payload = _parse(cipher.decrypt(data[len(MAGIC):len(MAGIC)+12], data[len(MAGIC)+12:], MAGIC + context))
    _need(type(payload) is dict and set(payload) == {"manifest", "files"}, "invalid checkpoint envelope")
    manifest, encoded = payload["manifest"], payload["files"]
    _need(type(manifest) is dict and set(manifest) == {"format", "root_id", "generation_id", "created_at", "directories", "files"}
          and manifest["format"] == "tankai-application-checkpoint-v1" and manifest["root_id"] == root_id
          and type(encoded) is dict and type(manifest["files"]) is dict
          and set(encoded) == set(manifest["files"]) and len(encoded) <= MAX_FILES
          and type(manifest["directories"]) is list and len(manifest["directories"]) <= MAX_FILES * 2,
          "invalid checkpoint inventory")
    from uuid import UUID
    _need(str(UUID(manifest["generation_id"])) == manifest["generation_id"]
          and type(manifest["created_at"]) is str
          and datetime.fromisoformat(manifest["created_at"]).tzinfo is not None,
          "invalid generation metadata")
    files = {name:base64.b64decode(value, validate=True) for name, value in encoded.items()}
    _need(manifest["files"] == {name:{"bytes":len(value), "sha256":_digest(value)} for name,value in files.items()},
          "checkpoint component digest mismatch")
    _validate(files, manifest["directories"], policy, pins)
    return files, manifest, _receipt(data, manifest)


def _fail_closed(function):
    from functools import wraps
    @wraps(function)
    def guarded(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except CheckpointError:
            raise
        except Exception:
            raise CheckpointError("application checkpoint operation failed closed") from None
    return guarded


@_fail_closed
def create_checkpoint(root: Path, destination: Path, key: bytes, *, expected_root_id: str,
                      expected_policy: PilotPolicy, expected_source_pins: dict[str, str]) -> dict:
    context = _context(expected_root_id, expected_policy, expected_source_pins)
    cipher = _cipher(key)
    root, destination = safe_path(root), safe_path(destination)
    _need(root != destination and root not in destination.parents, "checkpoint must be stored outside application root")
    with RootLease(root) as lease, ExitStack() as stack:
        metadata = root_metadata(root)
        _need(metadata["state"] == "active" and metadata["root_id"] == expected_root_id, "unexpected source root")
        inventory, directories = _inventory(root)
        # Hold every database's write reservation simultaneously, including WAL
        # databases, before capturing the first committed image.
        for name in sorted(inventory):
            if name.endswith((".db", ".sqlite")):
                db = stack.enter_context(closing(sqlite3.connect((root / name).as_uri()+"?mode=rw", uri=True,
                                                                 timeout=0, isolation_level=None)))
                db.execute("PRAGMA trusted_schema=OFF")
                db.execute("BEGIN IMMEDIATE")
        _need(_inventory(root) == (inventory, directories), "application inventory changed before capture")
        files = {name:(_sqlite_image(root/name) if name.endswith((".db", ".sqlite"))
                      else read_regular(root/name, MAX_FILE)) for name in sorted(inventory)}
        _validate(files, directories, expected_policy, expected_source_pins)
        # Non-SQLite writers cannot take a database barrier: check their bytes
        # again while participating web writers remain fenced by the root lock.
        for name, value in files.items():
            if not name.endswith((".db", ".sqlite")):
                _need(read_regular(root/name, MAX_FILE) == value, "workspace files changed during capture")
        _need(_inventory(root) == (inventory, directories) and root_metadata(root) == metadata,
              "application inventory changed during capture")
        lease.check()
        manifest = {"format":"tankai-application-checkpoint-v1", "root_id":expected_root_id,
                    "generation_id":str(uuid4()), "created_at":datetime.now(timezone.utc).isoformat(),
                    "directories":directories,
                    "files":{name:{"bytes":len(value), "sha256":_digest(value)} for name,value in files.items()}}
        payload = _json({"manifest":manifest, "files":{name:base64.b64encode(value).decode("ascii") for name,value in files.items()}})
        nonce = secrets.token_bytes(12)
        data = MAGIC + nonce + cipher.encrypt(nonce, payload, MAGIC + context)
        _need(len(data) <= MAX_ENCRYPTED, "encrypted checkpoint exceeds capacity")
        created = write_new(destination, data)
        _, persisted, receipt = _decode(destination, key, _digest(data), expected_root_id, expected_policy, expected_source_pins)
        _need(persisted == manifest and identity(regular(destination)) == created
              and read_regular(destination, MAX_ENCRYPTED) == data, "persisted checkpoint was replaced")
        lease.check()
        return {**receipt, "persisted_ciphertext_verified":True, "dispatch_authorized":False}


@_fail_closed
def verify_checkpoint(source: Path, key: bytes, *, expected_sha256: str, expected_root_id: str,
                      expected_policy: PilotPolicy, expected_source_pins: dict[str, str]) -> dict:
    return _decode(source, key, expected_sha256, expected_root_id, expected_policy, expected_source_pins)[2]


@_fail_closed
def restore_checkpoint(source: Path, destination: Path, key: bytes, *, expected_sha256: str,
                       expected_root_id: str, expected_policy: PilotPolicy,
                       expected_source_pins: dict[str, str]) -> dict:
    files, manifest, receipt = _decode(source, key, expected_sha256, expected_root_id, expected_policy, expected_source_pins)
    # Revoke access only in the restored copy. Passwords, memberships, money,
    # unresolved reservations, source approvals and histories are retained.
    with closing(_database(files["auth.db"])) as auth:
        timestamp = datetime.now(timezone.utc).isoformat()
        with auth:
            sessions = auth.execute("UPDATE sessions SET revoked_at=? WHERE revoked_at IS NULL", (timestamp,)).rowcount
            tokens = auth.execute("UPDATE agent_tokens SET revoked_at=? WHERE revoked_at IS NULL", (timestamp,)).rowcount
        files["auth.db"] = auth.serialize()
    _validate(files, manifest["directories"], expected_policy, expected_source_pins)
    root = safe_path(destination)
    root.mkdir(mode=0o700)  # No replacement, no merge, no reuse after partial failure.
    root_identity = identity(root.lstat())
    write_new(root/PENDING, b"restore pending; do not start web\n")
    created, dir_ids = {}, {"":root_identity}
    for name in sorted(manifest["directories"], key=lambda x:(x.count("/"), x)):
        target = root/name
        target.mkdir(mode=0o700)
        dir_ids[name] = identity(target.lstat())
    for name, data in files.items():
        _need((root/name).parent.is_dir(), "missing checkpoint parent directory")
        created[name] = write_new(root/name, data)
    restored_id = str(uuid4())
    created[LOCK] = write_new(root/LOCK, b"0")
    restored_marker = marker_bytes(restored_id, "staged-restore")
    created[MARKER] = write_new(root/MARKER, restored_marker)
    expected_files = {**files, LOCK:b"0", MARKER:restored_marker}
    def verify_created(pending):
        for name, expected in expected_files.items():
            _need(identity(regular(root/name)) == created[name] and read_regular(root/name, MAX_FILE) == expected,
                  "restored application file changed before completion")
        for name, expected in dir_ids.items():
            _need(identity(safe_path(root/name).lstat()) == expected, "restore directory was replaced")
        actual = {p.relative_to(root).as_posix() for p in root.rglob("*") if not p.is_dir()}
        _need(actual == set(expected_files) | ({PENDING} if pending else set()), "unexpected restored sidecars or files")
    verify_created(True)
    for name in dir_ids:
        _sync_directory(root/name)
    verify_created(True)
    (root/PENDING).unlink()
    _sync_directory(root)
    verify_created(False)
    _need(root_metadata(root) == {"format":"tankai-root-v1", "root_id":restored_id, "state":"staged-restore"}
          and identity(root.lstat()) == root_identity,
          "restore completion was replaced")
    return {**receipt, "restored_state_verified":True, "state":"staged-restore",
            "sessions_invalidated":sessions, "agent_tokens_invalidated":tokens,
            "restored_auth_sha256":_digest(files["auth.db"]), "dispatch_authorized":False,
            "web_startup_authorized":False}
