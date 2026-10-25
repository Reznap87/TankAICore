"""Offline, actual SQLite backup/restore, including uncertainty and WAL pages."""
from dataclasses import replace
from contextlib import closing
import hashlib
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest

from tankai.core.monetary_budget import (
    BudgetDenied, BudgetStateError, MAX_LEDGER_BYTES, MonetaryBudget,
)
from test_monetary_budget import Clock, NOW, OWNER, begin, policy, quote, reserve


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs): pytest.fail("budget recovery must stay offline")
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)


@pytest.fixture
def budget(tmp_path):
    with MonetaryBudget.create(tmp_path/"source.sqlite", policy(), clock=Clock()) as ledger:
        yield ledger


def restore(tmp_path, data, expected_policy, **changes):
    options = dict(data=data, expected_sha256=hashlib.sha256(data).hexdigest(),
                   expected_policy=expected_policy, clock=Clock())
    options.update(changes)
    return MonetaryBudget.restore_new(tmp_path/"recovered.sqlite", **options)


@pytest.mark.parametrize("state", ["held", "unknown", "settled", "closed-unknown"])
def test_exact_state_policy_attempts_and_replay_survive_restore(tmp_path, budget, state):
    begin(budget)
    reserve(budget)
    if state == "unknown" or state == "closed-unknown":
        budget.settle("attempt", owner_id=OWNER, actual_micro_eur=None)
    if state == "settled":
        budget.settle("attempt", owner_id=OWNER, actual_micro_eur=12_345)
    if state == "closed-unknown": budget.close_job("job", owner_id=OWNER)
    before = budget.snapshot()
    image = budget.export_snapshot()
    assert budget.snapshot() == before  # Export cannot settle or unlock.
    with restore(tmp_path, image, budget.policy) as recovered:
        assert recovered.snapshot() == before
        assert not reserve(recovered).dispatch_allowed
        assert recovered.snapshot()["attempts"] == 1
        if state != "closed-unknown":
            with pytest.raises(BudgetDenied, match="concurrent_job_limit"):
                begin(recovered, "replacement-job")
    with MonetaryBudget(tmp_path/"recovered.sqlite", clock=Clock()) as reopened:
        assert reopened.snapshot() == before


def test_wal_committed_reservation_is_included_without_checkpoint(tmp_path, budget):
    writer = sqlite3.connect(budget.path, isolation_level=None)
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        writer.execute("PRAGMA wal_autocheckpoint=0")
        begin(budget)
        reserve(budget)
        assert Path(str(budget.path)+"-wal").stat().st_size > 0
        with restore(tmp_path, budget.export_snapshot(), budget.policy) as recovered:
            assert recovered.snapshot()["held_micro_eur"] == 50_000
            assert not reserve(recovered).dispatch_allowed
    finally:
        writer.close()


def test_export_excludes_other_writers_until_consistent_copy_completes(tmp_path, budget, monkeypatch):
    begin(budget)
    reserve(budget)
    connect = sqlite3.connect
    observed = []
    class ReadPeer:
        def __init__(self, conn): self.conn = conn
        def execute(self, *args): return self.conn.execute(*args)
        def close(self): return self.conn.close()
        def backup(self, destination, **kwargs):
            probe = connect(budget.path, isolation_level=None, timeout=0)
            try:
                with pytest.raises(sqlite3.OperationalError, match="locked"):
                    probe.execute("BEGIN IMMEDIATE")
                observed.append("writer blocked")
            finally:
                probe.close()
            return self.conn.backup(destination, **kwargs)
    def connection(*args, **kwargs):
        conn = connect(*args, **kwargs)
        return ReadPeer(conn) if str(args[0]).endswith("?mode=ro") else conn
    monkeypatch.setattr(sqlite3, "connect", connection)
    image = budget.export_snapshot()
    reserve(budget, "later-attempt", amount=1)
    assert observed == ["writer blocked"]
    with restore(tmp_path, image, budget.policy) as recovered:
        assert recovered.snapshot()["attempts"] == 1
    assert budget.snapshot()["attempts"] == 2  # Snapshot is not an anti-rollback anchor.


@pytest.mark.parametrize("kind", ["halt", "expired", "clock-regression", "daily-cap", "job-quota"])
def test_restore_keeps_stops_and_consumed_capacity(tmp_path, kind):
    limits = policy(total_micro_eur=50_000, daily_micro_eur=50_000, per_job_micro_eur=50_000,
                    **({"max_jobs_daily":1, "max_jobs_total":1} if kind=="job-quota" else {}))
    clock = Clock()
    with MonetaryBudget.create(tmp_path/"source", limits, clock=clock) as ledger:
        begin(ledger)
        reserve(ledger)
        if kind == "halt":
            with pytest.raises(BudgetDenied): ledger.settle("attempt", owner_id=OWNER, actual_micro_eur=50_001)
        if kind == "expired":
            clock.now = limits.expires_at
            with pytest.raises(BudgetDenied): reserve(ledger, "expired")
        if kind == "clock-regression":
            clock.now = NOW+1
            ledger.settle("attempt", owner_id=OWNER, actual_micro_eur=None)
        if kind in {"daily-cap", "job-quota"}: ledger.close_job("job", owner_id=OWNER)
        before = ledger.snapshot()
        image = ledger.export_snapshot()
    with restore(tmp_path, image, limits) as recovered:
        assert recovered.snapshot() == before
        if kind in {"halt", "expired", "clock-regression"}:
            reason = {"halt":"pilot_halted", "expired":"clock_regression", "clock-regression":"clock_regression"}[kind]
            with pytest.raises(BudgetDenied, match=reason): reserve(recovered, "after-recovery")
        elif kind == "job-quota":
            with pytest.raises(BudgetDenied, match="total_job_quota"):
                begin(recovered, "after-recovery")
        else:
            begin(recovered, "after-recovery")
            with pytest.raises(BudgetDenied, match="daily_budget|total_budget"):
                reserve(recovered, "after-recovery", job_id="after-recovery", amount=1)


@pytest.mark.parametrize("pin", [None, "", "A"*64, "0"*64, True])
def test_invalid_or_missing_pin_never_creates_destination(tmp_path, budget, pin):
    with pytest.raises(BudgetStateError): restore(tmp_path, budget.export_snapshot(), budget.policy, expected_sha256=pin)
    assert not (tmp_path/"recovered.sqlite").exists()


@pytest.mark.parametrize("data", [None, b"", b"not SQLite", bytearray(b"SQLite format 3\x00"),
                                  b"SQLite format 3\x00"+b"x"*150])
def test_missing_malformed_or_mutable_image_fails_closed(tmp_path, budget, data):
    with pytest.raises(BudgetStateError):
        MonetaryBudget.restore_new(tmp_path/"recovered.sqlite", data=data,
            expected_sha256="0"*64, expected_policy=budget.policy)
    assert not (tmp_path/"recovered.sqlite").exists()


def test_image_size_checked_before_hashing_or_io(tmp_path, budget):
    with pytest.raises(BudgetStateError):
        MonetaryBudget.restore_new(tmp_path/"recovered.sqlite", data=b"x"*(MAX_LEDGER_BYTES+1),
                                   expected_sha256="0"*64, expected_policy=budget.policy)
    assert not (tmp_path/"recovered.sqlite").exists()


@pytest.mark.parametrize("change", [{"owner_id":"another-owner"}, {"expires_at":NOW+86_400},
                                    {"total_micro_eur":9_000_000}])
def test_matching_digest_does_not_accept_wrong_approved_policy(tmp_path, budget, change):
    with pytest.raises(BudgetStateError, match="policy"):
        restore(tmp_path, budget.export_snapshot(), replace(budget.policy, **change))
    assert not (tmp_path/"recovered.sqlite").exists()


@pytest.mark.parametrize("sql", [
    "CREATE TABLE foreign_data(x)",
    "CREATE TRIGGER sqliteXerase AFTER INSERT ON attempts BEGIN DELETE FROM attempts; END",
    "UPDATE pilot SET schema_version=99",
    "UPDATE pilot SET policy_json='{}'",
    "UPDATE attempts SET reserved_micro_eur=1",
])
def test_corrupt_or_foreign_budget_with_recomputed_digest_cannot_restore(tmp_path, budget, sql):
    begin(budget)
    reserve(budget)
    image_path = tmp_path/"tampered.sqlite"
    image_path.write_bytes(budget.export_snapshot())
    with closing(sqlite3.connect(image_path)) as conn, conn: conn.execute(sql)
    with pytest.raises(BudgetStateError): restore(tmp_path, image_path.read_bytes(), budget.policy)
    assert not (tmp_path/"recovered.sqlite").exists()


@pytest.mark.parametrize("suffix", ["", "-wal", "-shm", "-journal"])
def test_existing_target_or_sidecars_are_preserved_byte_for_byte(tmp_path, budget, suffix):
    existing = Path(str(tmp_path/"recovered.sqlite")+suffix)
    existing.write_bytes(b"existing operator data")
    with pytest.raises(BudgetStateError): restore(tmp_path, budget.export_snapshot(), budget.policy)
    assert existing.read_bytes() == b"existing operator data"
    if suffix: assert not (tmp_path/"recovered.sqlite").exists()


def test_destination_creation_race_cannot_overwrite_another_file(tmp_path, budget, monkeypatch):
    image = budget.export_snapshot()
    open_file = os.open
    destination = tmp_path/"recovered.sqlite"
    def race(path, *args, **kwargs):
        if Path(path) == destination: destination.write_bytes(b"created concurrently")
        return open_file(path, *args, **kwargs)
    monkeypatch.setattr(os, "open", race)
    with pytest.raises(BudgetStateError): restore(tmp_path, image, budget.policy)
    assert destination.read_bytes() == b"created concurrently"


def test_failed_new_file_write_is_not_replaced_on_retry(tmp_path, budget, monkeypatch):
    image = budget.export_snapshot()
    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", lambda fd:(_ for _ in ()).throw(OSError("synthetic sync failure")))
        with pytest.raises(BudgetStateError): restore(tmp_path, image, budget.policy)
    before = (tmp_path/"recovered.sqlite").read_bytes()
    with pytest.raises(BudgetStateError): restore(tmp_path, image, budget.policy)
    assert (tmp_path/"recovered.sqlite").read_bytes() == before


def test_symlink_destination_never_overwrites_its_target(tmp_path, budget):
    target = tmp_path/"operator-file"
    target.write_bytes(b"untouched")
    try: (tmp_path/"recovered.sqlite").symlink_to(target)
    except (OSError, NotImplementedError): pytest.skip("symlink privilege unavailable")
    with pytest.raises(BudgetStateError): restore(tmp_path, budget.export_snapshot(), budget.policy)
    assert target.read_bytes() == b"untouched"


def test_closed_export_is_rejected(tmp_path, budget):
    budget.close()
    with pytest.raises(BudgetStateError): budget.export_snapshot()


def test_copy_deadline_aborts_without_releasing_original_holds(budget, monkeypatch):
    from tankai.core import monetary_budget as module
    begin(budget)
    reserve(budget)
    before = budget.snapshot()
    elapsed = iter([0.0, 6.0])
    with monkeypatch.context() as patch:
        patch.setattr(module, "time", SimpleNamespace(monotonic=lambda:next(elapsed)))
        with pytest.raises(BudgetStateError, match="time limit"):
            budget.export_snapshot()
    assert budget.snapshot() == before


def test_wal_logical_size_is_bounded_before_backup_allocation(tmp_path, budget):
    writer = sqlite3.connect(budget.path, isolation_level=None)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE scratch(value)")
        writer.execute("INSERT INTO scratch VALUES (zeroblob(?))", (MAX_LEDGER_BYTES,))
        writer.execute("DROP TABLE scratch")
        assert budget.path.stat().st_size < MAX_LEDGER_BYTES
        with pytest.raises(BudgetStateError, match="capacity"):
            budget.export_snapshot()
    finally:
        writer.close()


def test_fresh_process_can_read_recovered_hold_and_cannot_resume_it(tmp_path, budget):
    begin(budget)
    reserve(budget)
    with restore(tmp_path, budget.export_snapshot(), budget.policy): pass
    script = '''
import sys
from tankai.core.monetary_budget import MonetaryBudget, BudgetDenied
with MonetaryBudget(sys.argv[1], clock=lambda:1790000000) as ledger:
    assert ledger.snapshot()["held_micro_eur"] == 50000
    assert ledger.snapshot()["active_jobs"] == 1
    try: ledger.begin_job("new", owner_id="test-owner")
    except BudgetDenied: print("RESTORED_HOLD_BLOCKS")
    else: raise AssertionError("lost durable active job")
'''
    completed = subprocess.run([sys.executable, "-X", "utf8", "-c", script, str(tmp_path/"recovered.sqlite")],
                               capture_output=True, text=True, timeout=15)
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "RESTORED_HOLD_BLOCKS"
