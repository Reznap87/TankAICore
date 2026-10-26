from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from tankai.core.monetary_budget import (
    BudgetDenied, BudgetStateError, DAY_SECONDS, IdempotencyConflict,
    MonetaryBudget, PilotPolicy, PriceQuote,
)


NOW = 1_790_000_000
OWNER = "test-owner"


class Clock:
    def __init__(self, now: int = NOW):
        self.now = now

    def __call__(self) -> int:
        return self.now


def policy(**kwargs) -> PilotPolicy:
    return replace(PilotPolicy(OWNER, NOW, NOW + 7 * DAY_SECONDS), **kwargs)


def quote(amount: int = 50_000, **kwargs) -> PriceQuote:
    return replace(PriceQuote(
        "test-tariff-v1", "test-provider", "test-model", NOW,
        NOW + 7 * DAY_SECONDS, amount, "https://example.test/pricing",
        "Deterministic test tariff only; fixed all-in euro ceiling for bounded request.",
    ), **kwargs)


def begin(ledger: MonetaryBudget, job_id: str = "job") -> None:
    assert ledger.begin_job(job_id, owner_id=OWNER).created


def reserve(ledger: MonetaryBudget, attempt_id: str = "attempt", *,
            job_id: str = "job", amount: int = 50_000):
    return ledger.reserve(job_id, attempt_id, owner_id=OWNER, quote=quote(amount))


def test_reservation_commits_before_dispatch_and_restart_replay_cannot_send_twice(tmp_path):
    path = tmp_path / "budget.sqlite"
    with MonetaryBudget.create(path, policy(), clock=Clock()) as ledger:
        begin(ledger)
        first = reserve(ledger)
        assert first.dispatch_allowed is True
        assert first.state == "held"
        # An independent connection sees the reservation before dispatch could
        # begin. An interrupted caller must replay, never spend a second time.
        with MonetaryBudget(path, clock=Clock()) as independent:
            assert independent.snapshot()["held_micro_eur"] == 50_000
            assert reserve(independent).dispatch_allowed is False
    with MonetaryBudget(path, clock=Clock()) as restarted:
        assert not restarted.begin_job("job", owner_id=OWNER).created
        assert not reserve(restarted).dispatch_allowed
        assert restarted.snapshot()["attempts"] == 1


def test_confirmed_settlement_refunds_unused_reservation_but_not_attempt_count(tmp_path):
    with MonetaryBudget.create(tmp_path / "budget", policy(per_job_micro_eur=100_000), clock=Clock()) as ledger:
        begin(ledger)
        reserve(ledger, amount=100_000)
        with pytest.raises(BudgetDenied, match="job_budget"):
            reserve(ledger, "blocked", amount=1)
        result = ledger.settle("attempt", owner_id=OWNER, actual_micro_eur=25_000)
        assert result.charged_micro_eur == 25_000
        assert not result.dispatch_allowed
        assert ledger.settle("attempt", owner_id=OWNER, actual_micro_eur=25_000) == result
        reserve(ledger, "second", amount=75_000)
        status = ledger.snapshot()
        assert status["exposure_micro_eur"] == 100_000
        assert status["confirmed_micro_eur"] == 25_000
        assert status["held_micro_eur"] == 75_000
        assert status["attempts"] == 2


def test_main_critic_and_provider_fallback_share_one_job_and_pilot_balance(tmp_path):
    path = tmp_path / "budget"
    with MonetaryBudget.create(path, policy(), clock=Clock()) as main:
        begin(main)
        main.reserve("job", "main", owner_id=OWNER,
                     quote=quote(120_000, provider="main-provider", model="main-model"))
        with MonetaryBudget(path, clock=Clock()) as critic:
            critic.reserve("job", "critic", owner_id=OWNER,
                           quote=quote(80_000, provider="critic-provider", model="critic-model"))
            with pytest.raises(BudgetDenied, match="job_budget"):
                critic.reserve("job", "fallback", owner_id=OWNER,
                               quote=quote(1, provider="fallback-provider", model="fallback-model"))
        assert main.snapshot()["exposure_micro_eur"] == 200_000
        assert main.snapshot()["attempts"] == 2
        assert main.snapshot()["remaining_total_micro_eur"] == 9_800_000


def test_unknown_outcome_and_closed_job_keep_hold_after_restart(tmp_path):
    path = tmp_path / "budget"
    limits = policy(total_micro_eur=100_000, daily_micro_eur=100_000, per_job_micro_eur=100_000)
    with MonetaryBudget.create(path, limits, clock=Clock()) as ledger:
        begin(ledger)
        reserve(ledger, amount=60_000)
        unknown = ledger.settle("attempt", owner_id=OWNER, actual_micro_eur=None)
        assert unknown.state == "unknown" and unknown.charged_micro_eur is None
        ledger.close_job("job", owner_id=OWNER)
        assert ledger.snapshot()["held_micro_eur"] == 60_000
    with MonetaryBudget(path, clock=Clock()) as ledger:
        begin(ledger, "next-job")
        with pytest.raises(BudgetDenied, match="daily_budget|total_budget"):
            reserve(ledger, "next-attempt", job_id="next-job", amount=60_000)
        assert ledger.snapshot()["exposure_micro_eur"] == 60_000


def test_closing_job_conservatively_marks_pending_as_unknown(tmp_path):
    with MonetaryBudget.create(tmp_path / "budget", policy(), clock=Clock()) as ledger:
        begin(ledger)
        reserve(ledger)
        ledger.close_job("job", owner_id=OWNER)
        assert reserve(ledger).state == "unknown"
        assert not ledger.begin_job("job", owner_id=OWNER).created
        with pytest.raises(BudgetDenied, match="active_job_required"):
            reserve(ledger, "new")
        assert ledger.snapshot()["held_micro_eur"] == 50_000


def test_only_exact_attempt_replay_and_exact_settlement_are_idempotent(tmp_path):
    with MonetaryBudget.create(tmp_path / "budget", policy(), clock=Clock()) as ledger:
        begin(ledger)
        reserve(ledger)
        with pytest.raises(IdempotencyConflict):
            reserve(ledger, amount=40_000)
        with pytest.raises(IdempotencyConflict):
            ledger.reserve("other-job", "attempt", owner_id=OWNER, quote=quote())
        ledger.settle("attempt", owner_id=OWNER, actual_micro_eur=0)
        with pytest.raises(IdempotencyConflict):
            ledger.settle("attempt", owner_id=OWNER, actual_micro_eur=1)
        with pytest.raises(IdempotencyConflict):
            ledger.settle("attempt", owner_id=OWNER, actual_micro_eur=None)
        assert ledger.snapshot()["exposure_micro_eur"] == 0


def test_settlement_above_reservation_is_rejected_and_never_releases_hold(tmp_path):
    path = tmp_path / "budget"
    with MonetaryBudget.create(path, policy(), clock=Clock()) as ledger:
        begin(ledger)
        reserve(ledger)
        with pytest.raises(BudgetDenied, match="actual_exceeds_reservation"):
            ledger.settle("attempt", owner_id=OWNER, actual_micro_eur=50_001)
        assert ledger.snapshot()["held_micro_eur"] == 50_000
        assert ledger.snapshot()["confirmed_micro_eur"] == 0
        assert ledger.snapshot()["halt_reason"] == "pricing_bound_exceeded"
    with MonetaryBudget(path, clock=Clock()) as ledger:
        with pytest.raises(BudgetDenied, match="pilot_halted"):
            reserve(ledger, "new-request")
        # A later correction can settle the attempt, but cannot resume spending.
        ledger.settle("attempt", owner_id=OWNER, actual_micro_eur=50_000)
        ledger.close_job("job", owner_id=OWNER)
        with pytest.raises(BudgetDenied, match="pilot_halted"):
            begin(ledger, "new-job")


def test_all_five_attempts_count_even_after_zero_charge_settlement(tmp_path):
    with MonetaryBudget.create(tmp_path / "budget", policy(), clock=Clock()) as ledger:
        begin(ledger)
        for i in range(5):
            reserve(ledger, f"request-{i}", amount=1)
            ledger.settle(f"request-{i}", owner_id=OWNER, actual_micro_eur=0)
        with pytest.raises(BudgetDenied, match="attempt_limit"):
            reserve(ledger, "retry-or-fallback", amount=1)
        assert ledger.snapshot()["attempts"] == 5


def test_owner_and_one_concurrent_job_persist_across_connections(tmp_path):
    path = tmp_path / "budget"
    with MonetaryBudget.create(path, policy(), clock=Clock()) as ledger:
        with pytest.raises(BudgetDenied, match="owner_only"):
            ledger.begin_job("wrong", owner_id="someone-else")
        begin(ledger)
        with MonetaryBudget(path, clock=Clock()) as other:
            with pytest.raises(BudgetDenied, match="concurrent_job_limit"):
                other.begin_job("second", owner_id=OWNER)
            with pytest.raises(BudgetDenied, match="owner_only"):
                other.reserve("job", "attempt", owner_id="someone-else", quote=quote())
        ledger.close_job("job", owner_id=OWNER)
        begin(ledger, "second")


def test_job_quota_is_not_refunded_by_close_or_restart(tmp_path):
    path = tmp_path / "budget"
    clock = Clock()
    limits = policy(max_jobs_daily=1, max_jobs_total=2)
    with MonetaryBudget.create(path, limits, clock=clock) as ledger:
        begin(ledger, "first")
        ledger.close_job("first", owner_id=OWNER)
    with MonetaryBudget(path, clock=clock) as ledger:
        with pytest.raises(BudgetDenied, match="daily_job_quota"):
            begin(ledger, "second")
        clock.now += DAY_SECONDS
        begin(ledger, "second")
        ledger.close_job("second", owner_id=OWNER)
        clock.now += DAY_SECONDS
        with pytest.raises(BudgetDenied, match="total_job_quota"):
            begin(ledger, "third")


def test_daily_cost_uses_pilot_day_of_dispatch_and_total_never_resets(tmp_path):
    clock = Clock()
    limits = policy(total_micro_eur=150_000, daily_micro_eur=100_000, per_job_micro_eur=100_000)
    with MonetaryBudget.create(tmp_path / "budget", limits, clock=clock) as ledger:
        begin(ledger, "first")
        reserve(ledger, "first-call", job_id="first", amount=100_000)
        ledger.close_job("first", owner_id=OWNER)
        begin(ledger, "second")
        with pytest.raises(BudgetDenied, match="daily_budget"):
            reserve(ledger, "same-day", job_id="second", amount=1)
        ledger.close_job("second", owner_id=OWNER)
        clock.now += DAY_SECONDS
        begin(ledger, "third")
        reserve(ledger, "next-day", job_id="third", amount=50_000)
        with pytest.raises(BudgetDenied, match="total_budget"):
            reserve(ledger, "beyond-total", job_id="third", amount=1)
        assert ledger.snapshot()["remaining_total_micro_eur"] == 0


def test_request_crossing_day_boundary_charges_new_day_and_same_job_cap(tmp_path):
    clock = Clock(NOW + DAY_SECONDS - 30)
    limits = policy(total_micro_eur=150_000, daily_micro_eur=100_000, per_job_micro_eur=100_000)
    with MonetaryBudget.create(tmp_path / "budget", limits, clock=clock) as ledger:
        begin(ledger)
        reserve(ledger, "before-midnight", amount=60_000)
        clock.now += 31
        with pytest.raises(BudgetDenied, match="job_budget"):
            reserve(ledger, "after-midnight", amount=41_000)
        reserve(ledger, "after-midnight", amount=40_000)
        assert ledger.snapshot()["exposure_micro_eur"] == 100_000


def test_expiration_and_job_deadline_block_new_dispatch_but_allow_late_settlement(tmp_path):
    clock = Clock()
    with MonetaryBudget.create(tmp_path / "budget", policy(), clock=clock) as ledger:
        begin(ledger)
        reserve(ledger)
        clock.now += 120
        with pytest.raises(BudgetDenied, match="job_deadline"):
            reserve(ledger, "too-late")
        clock.now = NOW + 7 * DAY_SECONDS
        with pytest.raises(BudgetDenied, match="pilot_expired"):
            reserve(ledger, "expired")
        # A replay is a lookup, not permission to resend an expired request.
        assert not reserve(ledger).dispatch_allowed
        assert ledger.settle("attempt", owner_id=OWNER, actual_micro_eur=10_000).state == "settled"
        ledger.close_job("job", owner_id=OWNER)
        with pytest.raises(BudgetDenied, match="pilot_expired"):
            begin(ledger, "expired-job")


def test_tariff_expiry_and_future_tariff_fail_closed_without_consuming_attempt(tmp_path):
    clock = Clock()
    with MonetaryBudget.create(tmp_path / "budget", policy(), clock=clock) as ledger:
        begin(ledger)
        for q in (quote(valid_from=NOW-10, valid_until=NOW), quote(valid_from=NOW+1)):
            with pytest.raises(BudgetDenied, match="tariff_not_current"):
                ledger.reserve("job", "not-dispatched", owner_id=OWNER, quote=q)
        assert ledger.snapshot()["attempts"] == 0
        with pytest.raises(ValueError):
            ledger.reserve("job", "missing-tariff", owner_id=OWNER, quote=None)


def test_not_started_and_clock_regression_remain_blocked_after_restart(tmp_path):
    clock = Clock(NOW - 1)
    path = tmp_path / "budget"
    with MonetaryBudget.create(path, policy(), clock=clock) as ledger:
        with pytest.raises(BudgetDenied, match="pilot_not_started"):
            begin(ledger)
        clock.now = NOW + 10
        begin(ledger)
    with MonetaryBudget(path, clock=Clock(NOW + 9)) as ledger:
        with pytest.raises(BudgetDenied, match="clock_regression"):
            reserve(ledger)


def test_denied_expiry_observation_prevents_clock_rollback_from_reopening_pilot(tmp_path):
    path = tmp_path / "budget"
    clock = Clock()
    with MonetaryBudget.create(path, policy(), clock=clock) as ledger:
        begin(ledger)
        clock.now = NOW + 7 * DAY_SECONDS
        with pytest.raises(BudgetDenied, match="pilot_expired"):
            reserve(ledger)
    with MonetaryBudget(path, clock=Clock()) as ledger:
        with pytest.raises(BudgetDenied, match="clock_regression"):
            reserve(ledger)


def test_parallel_connections_share_exact_cost_ceiling(tmp_path):
    path = tmp_path / "budget"
    with MonetaryBudget.create(path, policy(per_job_micro_eur=100_000), clock=Clock()) as ledger:
        begin(ledger)

        def work(index):
            with MonetaryBudget(path, clock=Clock()) as connection:
                try:
                    return reserve(connection, f"attempt-{index}", amount=30_000).dispatch_allowed
                except BudgetDenied:
                    return False

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(work, range(8)))
        assert sum(results) == 3
        assert ledger.snapshot()["held_micro_eur"] == 90_000
        assert ledger.snapshot()["attempts"] == 3


def test_parallel_processes_share_transactional_reservations(tmp_path):
    path = tmp_path / "budget"
    with MonetaryBudget.create(path, policy(per_job_micro_eur=100_000), clock=Clock()) as ledger:
        begin(ledger)
    script = """
import sys
from pathlib import Path
from tankai.core.monetary_budget import MonetaryBudget, PriceQuote, BudgetDenied
sys.stdin.readline()
q = PriceQuote('test-tariff-v1', 'provider', 'model', 1790000000, 1790604800,
               60000, 'https://example.test/pricing', 'Bounded deterministic all-in test quote')
with MonetaryBudget(Path(sys.argv[1]), clock=lambda: 1790000000) as ledger:
    try:
        result = ledger.reserve('job', sys.argv[2], owner_id='test-owner', quote=q)
        print('reserved' if result.dispatch_allowed else 'replayed')
    except BudgetDenied:
        print('denied')
"""
    processes = [subprocess.Popen(
        [sys.executable, "-c", script, str(path), f"process-{i}"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, cwd=SCRIPT_ROOT,
    ) for i in range(4)]
    try:
        # Start all independent interpreters before opening the stdin barrier.
        for process in processes:
            process.stdin.write("go\n")
            process.stdin.flush()
        results = [process.communicate(timeout=30) for process in processes]
        assert all(process.returncode == 0 for process in processes), results
        assert [out.strip() for out, _ in results].count("reserved") == 1
        assert [out.strip() for out, _ in results].count("denied") == 3
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate()
    with MonetaryBudget(path, clock=Clock()) as ledger:
        assert ledger.snapshot()["held_micro_eur"] == 60_000


def test_abrupt_process_exit_does_not_refund_committed_dispatch(tmp_path):
    path = tmp_path / "budget"
    with MonetaryBudget.create(path, policy(), clock=Clock()) as ledger:
        begin(ledger)
    script = """
import os, sys
from pathlib import Path
from tankai.core.monetary_budget import MonetaryBudget, PriceQuote
ledger = MonetaryBudget(Path(sys.argv[1]), clock=lambda: 1790000000)
quote = PriceQuote('test-tariff-v1', 'test-provider', 'test-model', 1790000000,
    1790604800, 50000, 'https://example.test/pricing',
    'Deterministic test tariff only; fixed all-in euro ceiling for bounded request.')
reservation = ledger.reserve('job', 'attempt', owner_id='test-owner', quote=quote)
assert reservation.dispatch_allowed
os._exit(0)
"""
    result = subprocess.run([sys.executable, "-c", script, str(path)], cwd=SCRIPT_ROOT,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    with MonetaryBudget(path, clock=Clock()) as ledger:
        assert ledger.snapshot()["held_micro_eur"] == 50_000
        assert ledger.snapshot()["active_jobs"] == 1
        assert not reserve(ledger).dispatch_allowed


SCRIPT_ROOT = Path(__file__).resolve().parents[1]


def test_missing_empty_corrupt_or_existing_database_is_never_initialized_silently(tmp_path):
    missing = tmp_path / "missing"
    with pytest.raises(BudgetStateError):
        MonetaryBudget(missing)
    assert not missing.exists()
    for name, content in (("empty", b""), ("corrupt", b"not a sqlite database")):
        path = tmp_path / name
        path.write_bytes(content)
        with pytest.raises(BudgetStateError):
            MonetaryBudget(path)
        with pytest.raises(FileExistsError):
            MonetaryBudget.create(path, policy(), clock=Clock())
        assert path.read_bytes() == content


def test_hardlinked_ledger_is_rejected_before_open(tmp_path):
    path = tmp_path / "budget.sqlite"
    alias = tmp_path / "budget-alias.sqlite"
    with MonetaryBudget.create(path, policy(), clock=Clock()) as ledger:
        assert ledger.snapshot()["jobs"] == 0
    try:
        os.link(path, alias)
    except (OSError, NotImplementedError):
        pytest.skip("hard links are unavailable")
    with pytest.raises(BudgetStateError):
        MonetaryBudget(path, clock=Clock())
    with pytest.raises(BudgetStateError):
        MonetaryBudget(alias, clock=Clock())


def test_new_hardlink_fails_closed_on_open_connection(tmp_path):
    path = tmp_path / "budget.sqlite"
    alias = tmp_path / "budget-alias.sqlite"
    with MonetaryBudget.create(path, policy(), clock=Clock()) as ledger:
        try:
            os.link(path, alias)
        except (OSError, NotImplementedError):
            pytest.skip("hard links are unavailable")
        with pytest.raises(BudgetStateError):
            ledger.begin_job("blocked", owner_id=OWNER)


def test_oversized_state_is_rejected_before_loading_or_integrity_work(tmp_path):
    path = tmp_path / "budget"
    with path.open("wb") as stream:
        stream.truncate(8 * 1024 * 1024 + 1)
    with pytest.raises(BudgetStateError):
        MonetaryBudget(path)


@pytest.mark.parametrize("tamper", [
    "DELETE FROM pilot",
    "UPDATE pilot SET schema_version=999",
    "CREATE VIEW injected AS SELECT * FROM jobs",
    "CREATE TRIGGER injected AFTER INSERT ON jobs BEGIN DELETE FROM attempts; END",
    "UPDATE pilot SET policy_json='{}'",
    "UPDATE pilot SET last_observed_at=-1",
])
def test_tampered_schema_or_policy_fails_closed(tmp_path, tamper):
    path = tmp_path / "budget"
    MonetaryBudget.create(path, policy(), clock=Clock()).close()
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA ignore_check_constraints=ON")
        conn.execute(tamper)
    with pytest.raises(BudgetStateError):
        MonetaryBudget(path, clock=Clock())


def test_negative_corrupt_reservation_is_rejected_even_if_sql_checks_disabled(tmp_path):
    path = tmp_path / "budget"
    with MonetaryBudget.create(path, policy(), clock=Clock()) as ledger:
        begin(ledger)
        reserve(ledger)
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA ignore_check_constraints=ON")
        conn.execute("UPDATE attempts SET charged_micro_eur=-1, state='settled', settled_at=?", (NOW,))
    with pytest.raises(BudgetStateError):
        MonetaryBudget(path, clock=Clock())


@pytest.mark.parametrize("name", [
    "sqliteX_refund", "SQLITEX_REFUND", "sqlite-refund", "sqliteBudgetRefund",
])
def test_sqlite_like_trigger_name_cannot_hide_a_refund_on_open_connection(tmp_path, name):
    path = tmp_path / "budget"
    with MonetaryBudget.create(path, policy(), clock=Clock()) as ledger:
        begin(ledger)
        reserve(ledger, "existing", amount=50_000)
        # Only the literal sqlite_ prefix is reserved by SQLite. SQL LIKE's
        # underscore wildcard would also hide these ordinary attacker names.
        with sqlite3.connect(path) as conn:
            conn.execute(f'CREATE TRIGGER "{name}" AFTER INSERT ON attempts '
                         'BEGIN DELETE FROM attempts WHERE attempt_id <> NEW.attempt_id; END')
        with pytest.raises(BudgetStateError, match="schema"):
            reserve(ledger, "new-request", amount=40_000)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*), SUM(reserved_micro_eur) FROM attempts").fetchone() == (1, 50_000)


def test_sqlite_like_extra_table_is_rejected_when_reopening(tmp_path):
    path = tmp_path / "budget"
    MonetaryBudget.create(path, policy(), clock=Clock()).close()
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE sqliteX_hidden (value TEXT)")
    with pytest.raises(BudgetStateError, match="schema"):
        MonetaryBudget(path, clock=Clock())


def test_policy_mutation_while_connection_open_is_not_adopted(tmp_path):
    path = tmp_path / "budget"
    with MonetaryBudget.create(path, policy(), clock=Clock()) as ledger:
        with sqlite3.connect(path) as conn:
            raw = json.loads(conn.execute("SELECT policy_json FROM pilot").fetchone()[0])
            raw["per_job_micro_eur"] = 150_000
            changed = json.dumps(raw, sort_keys=True, separators=(",", ":"))
            conn.execute("UPDATE pilot SET policy_json=?", (changed,))
        with pytest.raises(BudgetStateError):
            begin(ledger)


@pytest.mark.parametrize(("field", "value"), [
    ("total_micro_eur", -1), ("total_micro_eur", True),
    ("daily_micro_eur", 2_000_001), ("per_job_micro_eur", 200_001),
    ("max_attempts_per_job", 6), ("max_jobs_daily", 21),
    ("max_jobs_total", 51), ("max_concurrent_jobs", 2),
    ("job_deadline_seconds", 121), ("expires_at", NOW + 7 * DAY_SECONDS + 1),
    ("starts_at", 1.5), ("owner_id", "bad\nowner"),
])
def test_invalid_or_expanded_pilot_policy_is_rejected(field, value):
    with pytest.raises(ValueError):
        policy(**{field: value})


@pytest.mark.parametrize(("field", "value"), [
    ("maximum_micro_eur", -1), ("maximum_micro_eur", 0),
    ("maximum_micro_eur", True), ("maximum_micro_eur", 1.2),
    ("maximum_micro_eur", 10**12 + 1), ("tariff_id", ""),
    ("source_url", "http://example.test/pricing"),
    ("source_url", "https://user:password@example.test/pricing"),
    ("source_url", "https://example.test/pricing?key=secret"),
    ("billing_basis", ""), ("valid_until", NOW),
])
def test_missing_ambiguous_or_invalid_quote_metadata_fails_closed(field, value):
    with pytest.raises(ValueError):
        quote(**{field: value})


@pytest.mark.parametrize("actual", [-1, True, 1.2, 10**12 + 1])
def test_invalid_settlement_values_leave_reservation_untouched(tmp_path, actual):
    with MonetaryBudget.create(tmp_path / "budget", policy(), clock=Clock()) as ledger:
        begin(ledger)
        reserve(ledger)
        with pytest.raises(ValueError):
            ledger.settle("attempt", owner_id=OWNER, actual_micro_eur=actual)
        assert ledger.snapshot()["held_micro_eur"] == 50_000


def test_closed_connection_refuses_new_operations(tmp_path):
    ledger = MonetaryBudget.create(tmp_path / "budget", policy(), clock=Clock())
    ledger.close()
    ledger.close()
    with pytest.raises(BudgetStateError):
        begin(ledger)
