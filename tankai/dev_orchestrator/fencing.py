"""External monotonic lease fencing for development workers.

The fence database is intentionally separate from the job queue database. A
monotonically increasing epoch is issued per repository scope. Every mutating
worker stage must prove that its epoch and opaque lease token are still current.
A newer lease therefore invalidates stale workers even if they still hold an old
queue snapshot or a restored queue database.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _token_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class FenceError(RuntimeError):
    """Base error for the independent fencing store."""


class FenceBusy(FenceError):
    """Raised when an unexpired lease already owns the fence scope."""


class FenceLost(FenceError):
    """Raised when a worker no longer owns the current fence epoch."""


@dataclass(frozen=True)
class FenceLease:
    scope_key: str
    job_id: str
    owner_id: str
    epoch: int
    expires_at: datetime


@dataclass(frozen=True)
class FenceStatus:
    scope_key: str
    epoch: int
    job_id: str | None
    owner_id: str | None
    expires_at: datetime | None
    active: bool


class LeaseFenceStore:
    """SQLite-backed monotonic fencing store on an independent database file.

    SQLite is suitable for one local host. The database must live on durable
    local storage and must not be placed on NFS. Multi-host deployments require
    a transactional external coordinator with equivalent compare-and-swap
    semantics.
    """

    SCHEMA_VERSION = 1

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        self._lock = threading.RLock()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _init_schema(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(
                """
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS fence_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS lease_fences (
                    scope_key TEXT PRIMARY KEY,
                    epoch INTEGER NOT NULL CHECK(epoch >= 0),
                    job_id TEXT,
                    owner_id TEXT,
                    token_hash TEXT,
                    expires_at TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS fence_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    scope_key TEXT NOT NULL,
                    epoch INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    job_id TEXT,
                    owner_id TEXT,
                    details TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_fence_events_scope
                    ON fence_events(scope_key, sequence);
                INSERT OR REPLACE INTO fence_meta(key,value)
                    VALUES('schema_version','1');
                COMMIT;
                """
            )
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    @staticmethod
    def _validate_identifier(value: str, label: str) -> str:
        cleaned = value.strip()
        if not cleaned or len(cleaned) > 200 or any(ord(ch) < 32 for ch in cleaned):
            raise ValueError(f"Ungültige {label}")
        return cleaned

    def acquire(
        self,
        *,
        scope_key: str,
        job_id: str,
        owner_id: str,
        lease_token: str,
        lease_seconds: int,
    ) -> FenceLease:
        scope = self._validate_identifier(scope_key, "Fence-Scope")
        job = self._validate_identifier(job_id, "Job-ID")
        owner = self._validate_identifier(owner_id, "Worker-ID")
        if not lease_token:
            raise ValueError("Lease-Token fehlt")
        duration = max(30, min(int(lease_seconds), 86400))
        now = _utcnow()
        expires = now + timedelta(seconds=duration)
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT * FROM lease_fences WHERE scope_key=?", (scope,)
                ).fetchone()
                if row is not None:
                    current_expires = _parse_time(row["expires_at"])
                    if current_expires is not None and current_expires > now:
                        raise FenceBusy(
                            f"Repository-Fence ist bis {current_expires.isoformat()} belegt"
                        )
                    epoch = int(row["epoch"]) + 1
                    conn.execute(
                        """
                        UPDATE lease_fences
                        SET epoch=?,job_id=?,owner_id=?,token_hash=?,expires_at=?,updated_at=?
                        WHERE scope_key=?
                        """,
                        (
                            epoch,
                            job,
                            owner,
                            _token_hash(lease_token),
                            _iso(expires),
                            _iso(now),
                            scope,
                        ),
                    )
                else:
                    epoch = 1
                    conn.execute(
                        """
                        INSERT INTO lease_fences(
                            scope_key,epoch,job_id,owner_id,token_hash,expires_at,updated_at
                        ) VALUES(?,?,?,?,?,?,?)
                        """,
                        (
                            scope,
                            epoch,
                            job,
                            owner,
                            _token_hash(lease_token),
                            _iso(expires),
                            _iso(now),
                        ),
                    )
                self._event(
                    conn,
                    scope_key=scope,
                    epoch=epoch,
                    event_type="fence_acquired",
                    job_id=job,
                    owner_id=owner,
                    details=f"expires_at={_iso(expires)}",
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return FenceLease(scope, job, owner, epoch, expires)

    def assert_active(
        self,
        *,
        scope_key: str,
        job_id: str,
        epoch: int,
        lease_token: str,
    ) -> FenceLease:
        scope = self._validate_identifier(scope_key, "Fence-Scope")
        job = self._validate_identifier(job_id, "Job-ID")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM lease_fences WHERE scope_key=?", (scope,)
            ).fetchone()
        return self._validated_lease(row, scope, job, epoch, lease_token)

    def renew(
        self,
        *,
        scope_key: str,
        job_id: str,
        epoch: int,
        lease_token: str,
        lease_seconds: int,
    ) -> FenceLease:
        duration = max(30, min(int(lease_seconds), 86400))
        now = _utcnow()
        expires = now + timedelta(seconds=duration)
        scope = self._validate_identifier(scope_key, "Fence-Scope")
        job = self._validate_identifier(job_id, "Job-ID")
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT * FROM lease_fences WHERE scope_key=?", (scope,)
                ).fetchone()
                lease = self._validated_lease(row, scope, job, epoch, lease_token)
                cursor = conn.execute(
                    """
                    UPDATE lease_fences
                    SET expires_at=?,updated_at=?
                    WHERE scope_key=? AND epoch=? AND job_id=?
                    """,
                    (_iso(expires), _iso(now), scope, epoch, job),
                )
                if cursor.rowcount != 1:
                    raise FenceLost("Fence konnte nicht atomar erneuert werden")
                self._event(
                    conn,
                    scope_key=scope,
                    epoch=epoch,
                    event_type="fence_renewed",
                    job_id=job,
                    owner_id=lease.owner_id,
                    details=f"expires_at={_iso(expires)}",
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return FenceLease(scope, job, lease.owner_id, epoch, expires)

    def release(
        self,
        *,
        scope_key: str,
        job_id: str,
        epoch: int,
        lease_token: str,
        reason: str,
    ) -> None:
        scope = self._validate_identifier(scope_key, "Fence-Scope")
        job = self._validate_identifier(job_id, "Job-ID")
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT * FROM lease_fences WHERE scope_key=?", (scope,)
                ).fetchone()
                lease = self._validated_lease(row, scope, job, epoch, lease_token)
                cursor = conn.execute(
                    """
                    UPDATE lease_fences
                    SET job_id=NULL,owner_id=NULL,token_hash=NULL,expires_at=NULL,updated_at=?
                    WHERE scope_key=? AND epoch=? AND job_id=?
                    """,
                    (_iso(_utcnow()), scope, epoch, job),
                )
                if cursor.rowcount != 1:
                    raise FenceLost("Fence konnte nicht atomar freigegeben werden")
                self._event(
                    conn,
                    scope_key=scope,
                    epoch=epoch,
                    event_type="fence_released",
                    job_id=job,
                    owner_id=lease.owner_id,
                    details=reason[:1000],
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def current(self, scope_key: str) -> FenceStatus | None:
        scope = self._validate_identifier(scope_key, "Fence-Scope")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM lease_fences WHERE scope_key=?", (scope,)
            ).fetchone()
        if row is None:
            return None
        expires = _parse_time(row["expires_at"])
        active = bool(
            row["job_id"]
            and row["token_hash"]
            and expires is not None
            and expires > _utcnow()
        )
        return FenceStatus(
            scope_key=scope,
            epoch=int(row["epoch"]),
            job_id=row["job_id"],
            owner_id=row["owner_id"],
            expires_at=expires,
            active=active,
        )

    def force_expire_for_recovery(self, scope_key: str, *, expected_epoch: int) -> None:
        """Expire a known epoch for operator-led disaster recovery.

        This is intentionally strict and is not called automatically by workers.
        It exists so an operator can recover after proving that the old process is
        dead. A mismatched epoch is rejected to avoid revoking a newer worker.
        """

        scope = self._validate_identifier(scope_key, "Fence-Scope")
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT * FROM lease_fences WHERE scope_key=?", (scope,)
                ).fetchone()
                if row is None or int(row["epoch"]) != int(expected_epoch):
                    raise FenceLost("Fence-Epoche stimmt nicht mit der Recovery-Anforderung überein")
                conn.execute(
                    "UPDATE lease_fences SET expires_at=?,updated_at=? WHERE scope_key=?",
                    (_iso(_utcnow() - timedelta(seconds=1)), _iso(_utcnow()), scope),
                )
                self._event(
                    conn,
                    scope_key=scope,
                    epoch=int(row["epoch"]),
                    event_type="fence_force_expired",
                    job_id=row["job_id"],
                    owner_id=row["owner_id"],
                    details="operator recovery",
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    @staticmethod
    def _validated_lease(
        row: sqlite3.Row | None,
        scope_key: str,
        job_id: str,
        epoch: int,
        lease_token: str,
    ) -> FenceLease:
        if row is None:
            raise FenceLost("Kein externer Fence für diesen Repository-Scope vorhanden")
        if int(row["epoch"]) != int(epoch):
            raise FenceLost("Fence-Epoche wurde durch einen neueren Worker ersetzt")
        if row["job_id"] != job_id:
            raise FenceLost("Fence gehört zu einem anderen Auftrag")
        expected = row["token_hash"] or ""
        supplied = _token_hash(lease_token)
        if not expected or not hmac.compare_digest(expected, supplied):
            raise FenceLost("Ungültiger externer Fence-Token")
        expires = _parse_time(row["expires_at"])
        if expires is None or expires <= _utcnow():
            raise FenceLost("Externer Fence ist abgelaufen")
        owner = row["owner_id"] or ""
        return FenceLease(scope_key, job_id, owner, int(epoch), expires)

    @staticmethod
    def _event(
        conn: sqlite3.Connection,
        *,
        scope_key: str,
        epoch: int,
        event_type: str,
        job_id: str | None,
        owner_id: str | None,
        details: str,
    ) -> None:
        conn.execute(
            """
            INSERT INTO fence_events(
                scope_key,epoch,event_type,job_id,owner_id,details,created_at
            ) VALUES(?,?,?,?,?,?,?)
            """,
            (
                scope_key,
                int(epoch),
                event_type[:80],
                job_id,
                owner_id,
                details[:2000],
                _iso(_utcnow()),
            ),
        )

    def events(self, scope_key: str, *, limit: int = 100) -> list[dict[str, Any]]:
        scope = self._validate_identifier(scope_key, "Fence-Scope")
        limit = max(1, min(int(limit), 1000))
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT sequence,scope_key,epoch,event_type,job_id,owner_id,details,created_at
                FROM fence_events WHERE scope_key=? ORDER BY sequence DESC LIMIT ?
                """,
                (scope, limit),
            ).fetchall()
        return [dict(row) for row in rows]
