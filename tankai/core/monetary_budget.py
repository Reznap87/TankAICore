"""Standalone, single-host monetary reservations for a bounded owner pilot.

Amounts are integer micro-euros (1 EUR == 1_000_000). A new reservation must be
committed BEFORE dispatch. Replaying an attempt ID never authorizes another
dispatch. Failures/timeouts retain their full reservation until a trustworthy
settlement is available. Closing a job does not refund its unresolved attempts.

This module contains no provider client. Opt-in text dispatch and owner-pilot
admission use it; default/public startup does not activate them. Before real requests,
the caller must establish complete token bounds, validate current all-in tariffs,
guard every actual outbound attempt, and durably preserve this database.
It neither verifies provider invoices nor makes a local SQLite file distributed
or resistant to administrator rollback. No network requests occur here.
"""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
from typing import Callable, Iterator
from urllib.parse import urlsplit


DAY_SECONDS = 86_400
MAX_TIMESTAMP = 253_402_300_799
MAX_AMOUNT = 10**12
MAX_LEDGER_BYTES = 8 * 1024 * 1024
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@/+\-]{0,159}\Z")
_SCHEMA_VERSION = 1


class BudgetError(RuntimeError):
    """No new request may be dispatched after a budget error."""


class BudgetDenied(BudgetError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class BudgetStateError(BudgetError):
    """Missing, replaced, uninitialized or invalid durable budget state."""


class IdempotencyConflict(BudgetError):
    """An existing identifier was reused for different work or settlement."""


def _integer(value: int, name: str, *, minimum: int = 0, maximum: int = MAX_AMOUNT) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer in [{minimum}, {maximum}]")
    return value


def _identifier(value: str, name: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"invalid {name}")
    return value


def _json(value: dict) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


@dataclass(frozen=True)
class PilotPolicy:
    owner_id: str
    starts_at: int
    expires_at: int
    total_micro_eur: int = 10_000_000
    daily_micro_eur: int = 2_000_000
    per_job_micro_eur: int = 200_000
    max_attempts_per_job: int = 5
    max_jobs_daily: int = 20
    max_jobs_total: int = 50
    max_concurrent_jobs: int = 1
    job_deadline_seconds: int = 120

    def __post_init__(self) -> None:
        _identifier(self.owner_id, "owner_id")
        _integer(self.starts_at, "starts_at", maximum=MAX_TIMESTAMP)
        _integer(self.expires_at, "expires_at", maximum=MAX_TIMESTAMP)
        if not 0 < self.expires_at - self.starts_at <= 7 * DAY_SECONDS:
            raise ValueError("pilot duration must be positive and at most seven days")
        _integer(self.total_micro_eur, "total_micro_eur", minimum=1, maximum=10_000_000)
        _integer(self.daily_micro_eur, "daily_micro_eur", minimum=1, maximum=2_000_000)
        _integer(self.per_job_micro_eur, "per_job_micro_eur", minimum=1, maximum=200_000)
        if not self.per_job_micro_eur <= self.daily_micro_eur <= self.total_micro_eur:
            raise ValueError("per-job, daily and total caps must be ordered")
        for name, maximum in (("max_attempts_per_job", 5), ("max_jobs_daily", 20),
                              ("max_jobs_total", 50), ("max_concurrent_jobs", 1),
                              ("job_deadline_seconds", 120)):
            _integer(getattr(self, name), name, minimum=1, maximum=maximum)
        if self.max_jobs_daily > self.max_jobs_total:
            raise ValueError("daily job quota exceeds total quota")


@dataclass(frozen=True)
class PriceQuote:
    """Trusted external all-in maximum for ONE actual outbound request.

    This ledger cannot verify the truth of a quote. Its producer must include all
    billable token kinds, fees, taxes and conservative FX, and enforce the input
    and output bounds used to obtain this ceiling. Metadata is persisted so an
    attempt cannot be replayed with a different price or request tariff.
    """
    tariff_id: str
    provider: str
    model: str
    valid_from: int
    valid_until: int
    maximum_micro_eur: int
    source_url: str
    billing_basis: str

    def __post_init__(self) -> None:
        for name in ("tariff_id", "provider", "model"):
            _identifier(getattr(self, name), name)
        _integer(self.valid_from, "valid_from", maximum=MAX_TIMESTAMP)
        _integer(self.valid_until, "valid_until", maximum=MAX_TIMESTAMP)
        if self.valid_until <= self.valid_from:
            raise ValueError("quote validity must be positive")
        _integer(self.maximum_micro_eur, "maximum_micro_eur", minimum=1)
        if not isinstance(self.source_url, str) or len(self.source_url) > 2048:
            raise ValueError("invalid price source URL")
        source = urlsplit(self.source_url)
        if (source.scheme != "https" or not source.hostname or source.username
                or source.password or source.query or source.fragment
                or any(ord(char) < 33 for char in self.source_url)):
            raise ValueError("price source must be an HTTPS URL without credentials or query")
        if (not isinstance(self.billing_basis, str) or not self.billing_basis.strip()
                or len(self.billing_basis) > 2000
                or any(ord(char) < 32 for char in self.billing_basis)):
            raise ValueError("explicit all-in billing basis is required")


@dataclass(frozen=True)
class Job:
    job_id: str
    status: str
    started_at: int
    ended_at: int | None
    created: bool


@dataclass(frozen=True)
class Reservation:
    attempt_id: str
    job_id: str
    reserved_micro_eur: int
    charged_micro_eur: int | None
    state: str
    dispatch_allowed: bool


_TABLES = {
    "pilot": """CREATE TABLE pilot (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        schema_version INTEGER NOT NULL,
        policy_json TEXT NOT NULL,
        last_observed_at INTEGER NOT NULL CHECK (last_observed_at >= 0),
        halt_reason TEXT)""",
    "jobs": """CREATE TABLE jobs (
        job_id TEXT PRIMARY KEY NOT NULL,
        owner_id TEXT NOT NULL,
        day_index INTEGER NOT NULL CHECK (day_index >= 0),
        started_at INTEGER NOT NULL,
        ended_at INTEGER,
        status TEXT NOT NULL CHECK (status IN ('active', 'closed')))""",
    "attempts": """CREATE TABLE attempts (
        attempt_id TEXT PRIMARY KEY NOT NULL,
        job_id TEXT NOT NULL REFERENCES jobs(job_id),
        day_index INTEGER NOT NULL CHECK (day_index >= 0),
        quote_json TEXT NOT NULL,
        reserved_micro_eur INTEGER NOT NULL CHECK (reserved_micro_eur > 0),
        charged_micro_eur INTEGER CHECK (charged_micro_eur >= 0),
        state TEXT NOT NULL CHECK (state IN ('held', 'unknown', 'settled')),
        created_at INTEGER NOT NULL,
        settled_at INTEGER)""",
}


def _sql_signature(sql: str) -> str:
    return " ".join(sql.split()).lower()


class MonetaryBudget:
    """One immutable pilot policy with shared transactional reservations.

    `create` exclusively initializes a NEW file. The constructor only opens an
    existing initialized ledger and never silently creates, migrates or resets.
    Separate instances/processes serialize with BEGIN IMMEDIATE on local SQLite.
    """

    @classmethod
    def create(cls, path: Path, policy: PilotPolicy, *,
               clock: Callable[[], int] | None = None) -> "MonetaryBudget":
        if not isinstance(policy, PilotPolicy):
            raise ValueError("an explicit PilotPolicy is required")
        path = Path(path).absolute()
        now = cls._now(clock)
        # O_EXCL includes an existing empty file or symlink: neither is replaced.
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
        conn = None
        try:
            conn = sqlite3.connect(str(path), isolation_level=None)
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("BEGIN IMMEDIATE")
            for statement in _TABLES.values():
                conn.execute(statement)
            conn.execute("INSERT INTO pilot VALUES (1, ?, ?, ?, NULL)",
                         (_SCHEMA_VERSION, _json(asdict(policy)), now))
            conn.commit()
        finally:
            if conn is not None:
                conn.close()
        # An initialization failure deliberately leaves the failed file in place
        # for diagnosis; a retry must not silently replace an existing budget.
        return cls(path, clock=clock)

    @staticmethod
    def _now(clock: Callable[[], int] | None) -> int:
        return _integer(clock() if clock is not None else int(time.time()),
                        "clock", maximum=MAX_TIMESTAMP)

    def __init__(self, path: Path, *, clock: Callable[[], int] | None = None) -> None:
        self.path = Path(path).absolute()
        self._clock = clock
        self._lock = threading.RLock()
        self._closed = False
        self._conn: sqlite3.Connection | None = None
        try:
            if self.path.is_symlink() or not self.path.is_file():
                raise BudgetStateError("existing regular budget database required")
            stat = self.path.stat()
            if stat.st_size > MAX_LEDGER_BYTES:
                raise BudgetStateError("budget database exceeds bounded pilot size")
            self._file_identity = (stat.st_dev, stat.st_ino)
            self._conn = sqlite3.connect(
                self.path.as_uri() + "?mode=rw", uri=True, isolation_level=None,
                check_same_thread=False, timeout=30,
            )
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA trusted_schema=OFF")
            self._conn.execute("PRAGMA synchronous=FULL")
            if self._conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise BudgetStateError("budget integrity check failed")
            self._conn.execute("BEGIN IMMEDIATE")
            self.policy = self._validate_state()
            self._conn.commit()
        except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
            if self._conn is not None:
                self._conn.close()
            raise BudgetStateError("cannot open a valid initialized budget database") from exc
        except BaseException:
            if self._conn is not None:
                self._conn.close()
            raise

    def _validate_state(self) -> PilotPolicy:
        assert self._conn is not None
        # GLOB treats '_' literally. LIKE 'sqlite_%' would also hide ordinary
        # objects named sqliteX..., including triggers that erase earlier holds.
        schema = self._conn.execute(
            "SELECT name,type,sql FROM sqlite_master WHERE name NOT GLOB 'sqlite_*' LIMIT 4"
        ).fetchall()
        if ({row["name"] for row in schema} != set(_TABLES)
                or any(row["type"] != "table" or _sql_signature(row["sql"])
                       != _sql_signature(_TABLES[row["name"]]) for row in schema)):
            raise BudgetStateError("unexpected budget schema")
        metadata = self._conn.execute("SELECT * FROM pilot").fetchall()
        if (len(metadata) != 1 or metadata[0]["singleton"] != 1
                or metadata[0]["schema_version"] != _SCHEMA_VERSION):
            raise BudgetStateError("missing or unsupported pilot policy")
        meta = metadata[0]
        if meta["halt_reason"] not in {None, "pricing_bound_exceeded"}:
            raise BudgetStateError("invalid pilot halt reason")
        policy = PilotPolicy(**json.loads(meta["policy_json"]))
        if meta["policy_json"] != _json(asdict(policy)):
            raise BudgetStateError("noncanonical pilot policy")
        if hasattr(self, "policy") and policy != self.policy:
            raise BudgetStateError("pilot policy changed while open")
        last = _integer(meta["last_observed_at"], "last_observed_at", maximum=MAX_TIMESTAMP)
        jobs = self._conn.execute("SELECT * FROM jobs LIMIT ?", (policy.max_jobs_total + 1,)).fetchall()
        if len(jobs) > policy.max_jobs_total:
            raise BudgetStateError("total job quota exceeded in stored state")
        job_map = {}
        job_days: Counter[int] = Counter()
        active = 0
        for job in jobs:
            _identifier(job["job_id"], "job_id")
            started = _integer(job["started_at"], "started_at", maximum=MAX_TIMESTAMP)
            if (job["owner_id"] != policy.owner_id or not policy.starts_at <= started < policy.expires_at
                    or job["day_index"] != (started - policy.starts_at) // DAY_SECONDS
                    or started > last):
                raise BudgetStateError("invalid stored job")
            if job["status"] == "active" and job["ended_at"] is None:
                active += 1
            elif job["status"] == "closed":
                ended = _integer(job["ended_at"], "ended_at", maximum=MAX_TIMESTAMP)
                if not started <= ended <= last:
                    raise BudgetStateError("invalid stored job end")
            else:
                raise BudgetStateError("invalid stored job status")
            job_days[job["day_index"]] += 1
            job_map[job["job_id"]] = job
        if active > policy.max_concurrent_jobs or any(n > policy.max_jobs_daily for n in job_days.values()):
            raise BudgetStateError("stored concurrency or daily quota exceeded")
        costs: Counter[str] = Counter()
        days: Counter[int] = Counter()
        attempts: Counter[str] = Counter()
        max_attempts = policy.max_jobs_total * policy.max_attempts_per_job
        stored_attempts = self._conn.execute("SELECT * FROM attempts LIMIT ?", (max_attempts + 1,)).fetchall()
        if len(stored_attempts) > max_attempts:
            raise BudgetStateError("stored global attempt ceiling exceeded")
        for attempt in stored_attempts:
            _identifier(attempt["attempt_id"], "attempt_id")
            job = job_map.get(attempt["job_id"])
            if job is None:
                raise BudgetStateError("orphan budget attempt")
            quote = PriceQuote(**json.loads(attempt["quote_json"]))
            if attempt["quote_json"] != _json(asdict(quote)):
                raise BudgetStateError("noncanonical price quote")
            created = _integer(attempt["created_at"], "created_at", maximum=MAX_TIMESTAMP)
            if (not job["started_at"] <= created < min(policy.expires_at,
                    job["started_at"] + policy.job_deadline_seconds)
                    or not quote.valid_from <= created < quote.valid_until or created > last
                    or attempt["day_index"] != (created - policy.starts_at) // DAY_SECONDS
                    or attempt["reserved_micro_eur"] != quote.maximum_micro_eur
                    or (job["ended_at"] is not None and created > job["ended_at"])):
                raise BudgetStateError("invalid stored reservation")
            if attempt["state"] == "settled":
                charged = _integer(attempt["charged_micro_eur"], "charged_micro_eur",
                                   maximum=quote.maximum_micro_eur)
                settled = _integer(attempt["settled_at"], "settled_at", maximum=MAX_TIMESTAMP)
                if not created <= settled <= last:
                    raise BudgetStateError("invalid stored settlement")
            elif attempt["state"] in {"held", "unknown"}:
                if attempt["charged_micro_eur"] is not None or attempt["settled_at"] is not None:
                    raise BudgetStateError("unresolved attempt contains a refund")
                charged = quote.maximum_micro_eur
            else:
                raise BudgetStateError("invalid stored attempt state")
            costs[attempt["job_id"]] += charged
            days[attempt["day_index"]] += charged
            attempts[attempt["job_id"]] += 1
        if (sum(costs.values()) > policy.total_micro_eur
                or any(n > policy.per_job_micro_eur for n in costs.values())
                or any(n > policy.daily_micro_eur for n in days.values())
                or any(n > policy.max_attempts_per_job for n in attempts.values())):
            raise BudgetStateError("stored exposure or attempt ceiling exceeded")
        return policy

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            if self._closed or self._conn is None:
                raise BudgetStateError("budget database is closed")
            try:
                if self.path.is_symlink() or not self.path.is_file():
                    raise BudgetStateError("budget database is missing or replaced")
                stat = self.path.stat()
                if stat.st_size > MAX_LEDGER_BYTES:
                    raise BudgetStateError("budget database exceeds bounded pilot size")
                if (stat.st_dev, stat.st_ino) != self._file_identity:
                    raise BudgetStateError("budget database was replaced")
                self._conn.execute("BEGIN IMMEDIATE")
                self._validate_state()
                yield self._conn
                self._validate_state()
                self._conn.commit()
            except BudgetDenied:
                # A denied request does not consume an attempt, but observing
                # expiry or a tariff overrun must survive restart/clock rollback.
                # Denial branches only change the clock or sticky halt marker.
                try:
                    self._validate_state()
                    self._conn.commit()
                except BaseException:
                    if self._conn.in_transaction:
                        self._conn.rollback()
                    raise
                raise
            except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
                if self._conn.in_transaction:
                    self._conn.rollback()
                raise BudgetStateError("budget operation failed closed") from exc
            except BaseException:
                if self._conn.in_transaction:
                    self._conn.rollback()
                raise

    def _observe(self, conn: sqlite3.Connection) -> int:
        now = self._now(self._clock)
        last = conn.execute("SELECT last_observed_at FROM pilot").fetchone()[0]
        if now < last:
            raise BudgetDenied("clock_regression")
        conn.execute("UPDATE pilot SET last_observed_at=?", (now,))
        return now

    def _owner(self, owner_id: str) -> None:
        _identifier(owner_id, "owner_id")
        if owner_id != self.policy.owner_id:
            raise BudgetDenied("owner_only")

    def _window(self, now: int) -> None:
        assert self._conn is not None
        if self._conn.execute("SELECT halt_reason FROM pilot").fetchone()[0] is not None:
            raise BudgetDenied("pilot_halted")
        if now < self.policy.starts_at:
            raise BudgetDenied("pilot_not_started")
        if now >= self.policy.expires_at:
            raise BudgetDenied("pilot_expired")

    @staticmethod
    def _job(row: sqlite3.Row, *, created: bool) -> Job:
        return Job(row["job_id"], row["status"], row["started_at"], row["ended_at"], created)

    @staticmethod
    def _reservation(row: sqlite3.Row, *, dispatch_allowed: bool = False) -> Reservation:
        return Reservation(row["attempt_id"], row["job_id"], row["reserved_micro_eur"],
                           row["charged_micro_eur"], row["state"], dispatch_allowed)

    def begin_job(self, job_id: str, *, owner_id: str) -> Job:
        _identifier(job_id, "job_id")
        self._owner(owner_id)
        with self._transaction() as conn:
            now = self._observe(conn)
            existing = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if existing is not None:
                return self._job(existing, created=False)
            self._window(now)
            day = (now - self.policy.starts_at) // DAY_SECONDS
            if conn.execute("SELECT COUNT(*) FROM jobs WHERE status='active'").fetchone()[0] >= self.policy.max_concurrent_jobs:
                raise BudgetDenied("concurrent_job_limit")
            if conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] >= self.policy.max_jobs_total:
                raise BudgetDenied("total_job_quota")
            if conn.execute("SELECT COUNT(*) FROM jobs WHERE day_index=?", (day,)).fetchone()[0] >= self.policy.max_jobs_daily:
                raise BudgetDenied("daily_job_quota")
            conn.execute("INSERT INTO jobs VALUES (?, ?, ?, ?, NULL, 'active')",
                         (job_id, owner_id, day, now))
            return Job(job_id, "active", now, None, True)

    def reserve(self, job_id: str, attempt_id: str, *, owner_id: str,
                quote: PriceQuote) -> Reservation:
        _identifier(job_id, "job_id")
        _identifier(attempt_id, "attempt_id")
        self._owner(owner_id)
        if not isinstance(quote, PriceQuote):
            raise ValueError("an explicit PriceQuote is required")
        quote_json = _json(asdict(quote))
        with self._transaction() as conn:
            now = self._observe(conn)
            existing = conn.execute("SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
            if existing is not None:
                if existing["job_id"] != job_id or existing["quote_json"] != quote_json:
                    raise IdempotencyConflict("attempt_id already belongs to a different request")
                return self._reservation(existing)
            self._window(now)
            job = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if job is None or job["status"] != "active":
                raise BudgetDenied("active_job_required")
            if now >= job["started_at"] + self.policy.job_deadline_seconds:
                raise BudgetDenied("job_deadline")
            if not quote.valid_from <= now < quote.valid_until:
                raise BudgetDenied("tariff_not_current")
            day = (now - self.policy.starts_at) // DAY_SECONDS
            attempts = conn.execute("SELECT COUNT(*) FROM attempts WHERE job_id=?", (job_id,)).fetchone()[0]
            if attempts >= self.policy.max_attempts_per_job:
                raise BudgetDenied("attempt_limit")
            exposure_sql = "SUM(COALESCE(charged_micro_eur, reserved_micro_eur))"
            for reason, suffix, params, cap in (
                ("job_budget", " WHERE job_id=?", (job_id,), self.policy.per_job_micro_eur),
                ("daily_budget", " WHERE day_index=?", (day,), self.policy.daily_micro_eur),
                ("total_budget", "", (), self.policy.total_micro_eur),
            ):
                used = conn.execute(f"SELECT {exposure_sql} FROM attempts" + suffix, params).fetchone()[0] or 0
                if used + quote.maximum_micro_eur > cap:
                    raise BudgetDenied(reason)
            conn.execute("INSERT INTO attempts VALUES (?, ?, ?, ?, ?, NULL, 'held', ?, NULL)",
                         (attempt_id, job_id, day, quote_json, quote.maximum_micro_eur, now))
            row = conn.execute("SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
            return self._reservation(row, dispatch_allowed=True)

    def settle(self, attempt_id: str, *, owner_id: str,
               actual_micro_eur: int | None) -> Reservation:
        """Refund only confirmed actual <= reserved; None keeps the full hold.

        An actual value above the reservation is rejected, never clamped, and
        permanently halts new dispatches. Investigate the pricing-bound failure;
        this module provides no automatic resume, top-up or reset operation.
        """
        _identifier(attempt_id, "attempt_id")
        self._owner(owner_id)
        if actual_micro_eur is not None:
            _integer(actual_micro_eur, "actual_micro_eur")
        with self._transaction() as conn:
            now = self._observe(conn)
            row = conn.execute("SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
            if row is None:
                raise BudgetDenied("unknown_attempt")
            if row["state"] == "settled":
                if actual_micro_eur != row["charged_micro_eur"]:
                    raise IdempotencyConflict("settled actual cost cannot be changed")
                return self._reservation(row)
            if actual_micro_eur is None:
                conn.execute("UPDATE attempts SET state='unknown' WHERE attempt_id=?", (attempt_id,))
            else:
                if actual_micro_eur > row["reserved_micro_eur"]:
                    conn.execute("UPDATE pilot SET halt_reason='pricing_bound_exceeded'")
                    raise BudgetDenied("actual_exceeds_reservation")
                conn.execute("UPDATE attempts SET charged_micro_eur=?, state='settled', settled_at=? WHERE attempt_id=?",
                             (actual_micro_eur, now, attempt_id))
            row = conn.execute("SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
            return self._reservation(row)

    def close_job(self, job_id: str, *, owner_id: str) -> Job:
        """End local work without cancelling upstream requests or releasing holds."""
        _identifier(job_id, "job_id")
        self._owner(owner_id)
        with self._transaction() as conn:
            now = self._observe(conn)
            row = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                raise BudgetDenied("unknown_job")
            if row["status"] == "active":
                conn.execute("UPDATE jobs SET status='closed', ended_at=? WHERE job_id=?", (now, job_id))
                conn.execute("UPDATE attempts SET state='unknown' WHERE job_id=? AND state='held'", (job_id,))
                row = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            return self._job(row, created=False)

    def snapshot(self) -> dict:
        with self._transaction() as conn:
            rows = conn.execute("SELECT * FROM attempts").fetchall()
            confirmed = sum(row["charged_micro_eur"] for row in rows if row["state"] == "settled")
            held = sum(row["reserved_micro_eur"] for row in rows if row["state"] != "settled")
            return {
                "policy": asdict(self.policy),
                "confirmed_micro_eur": confirmed,
                "held_micro_eur": held,
                "exposure_micro_eur": confirmed + held,
                "remaining_total_micro_eur": self.policy.total_micro_eur - confirmed - held,
                "attempts": len(rows),
                "unresolved_attempts": sum(row["state"] != "settled" for row in rows),
                "jobs": conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0],
                "active_jobs": conn.execute("SELECT COUNT(*) FROM jobs WHERE status='active'").fetchone()[0],
                "halt_reason": conn.execute("SELECT halt_reason FROM pilot").fetchone()[0],
            }

    def close(self) -> None:
        with self._lock:
            if not self._closed and self._conn is not None:
                self._conn.close()
                self._closed = True

    def __enter__(self) -> "MonetaryBudget":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
