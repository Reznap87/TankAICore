from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from tankai.core.llm import LLMCallBudget, LLMRateLimitExceeded
from tankai.web.auth import AuthStore, ProviderCallRateLimiter


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


def test_rate_limit_is_separate_per_user_and_provider(tmp_path) -> None:
    store = AuthStore(tmp_path / "auth.db")
    clock = FakeClock()
    limiter = ProviderCallRateLimiter(store, limit=2, window_seconds=60, clock=clock)

    limiter.consume("user-a", "openai:main-model")
    limiter.consume("user-a", "openai:main-model")
    limiter.consume("user-b", "openai:main-model")
    limiter.consume("user-a", "anthropic:critic-model")

    with pytest.raises(LLMRateLimitExceeded) as exc_info:
        limiter.consume("user-a", "openai:another-model")
    exc = exc_info.value
    assert exc.provider == "openai"
    assert exc.limit == 2
    assert exc.window_seconds == 60
    assert exc.retry_after_seconds == 60


def test_sliding_window_reopens_after_oldest_event_expires(tmp_path) -> None:
    store = AuthStore(tmp_path / "auth.db")
    clock = FakeClock()
    limiter = ProviderCallRateLimiter(store, limit=1, window_seconds=60, clock=clock)

    limiter.consume("user-a", "openai:model")
    clock.advance(59)
    with pytest.raises(LLMRateLimitExceeded) as exc_info:
        limiter.consume("user-a", "openai:model")
    assert exc_info.value.retry_after_seconds == 1

    clock.advance(2)
    limiter.consume("user-a", "openai:model")


def test_rate_ledger_survives_store_reopen(tmp_path) -> None:
    path = tmp_path / "auth.db"
    clock = FakeClock()
    first = ProviderCallRateLimiter(AuthStore(path), limit=1, window_seconds=60, clock=clock)
    first.consume("user-a", "anthropic:model")

    restarted = ProviderCallRateLimiter(AuthStore(path), limit=1, window_seconds=60, clock=clock)
    with pytest.raises(LLMRateLimitExceeded):
        restarted.consume("user-a", "anthropic:other-model")


def test_concurrent_calls_are_atomically_limited(tmp_path) -> None:
    store = AuthStore(tmp_path / "auth.db")
    clock = FakeClock()
    limiter = ProviderCallRateLimiter(store, limit=8, window_seconds=60, clock=clock)

    def attempt(index: int) -> bool:
        try:
            limiter.consume("user-a", f"openai:model-{index}")
            return True
        except LLMRateLimitExceeded:
            return False

    with ThreadPoolExecutor(max_workers=16) as pool:
        outcomes = list(pool.map(attempt, range(24)))

    assert sum(outcomes) == 8


def test_call_guard_rejection_does_not_consume_run_budget() -> None:
    budget = LLMCallBudget(5)

    def reject(identity: str) -> None:
        raise LLMRateLimitExceeded(
            provider=identity.split(":", 1)[0],
            limit=1,
            window_seconds=60,
            retry_after_seconds=10,
        )

    budget.set_call_guard(reject)
    with pytest.raises(LLMRateLimitExceeded):
        budget.consume("openai:model")
    assert budget.snapshot() == {"used": 0, "max": 5, "remaining": 5}

    budget.set_call_guard(None)
    assert budget.consume("openai:model") == 1


def test_shared_upstream_survives_restart_and_different_users_aliases_protocols(tmp_path):
    path = tmp_path / "auth.db"
    clock = FakeClock()
    first = ProviderCallRateLimiter(AuthStore(path), clock=clock, shared_scope="private-router")
    first.consume("user-a", "openai:auto:main")
    restarted = ProviderCallRateLimiter(AuthStore(path), clock=clock, shared_scope="private-router")
    restarted.consume("user-b", "anthropic:critic")
    with pytest.raises(LLMRateLimitExceeded) as error:
        restarted.consume("user-c", "openai:different-alias")
    assert error.value.provider == "shared-upstream"
    assert error.value.retry_after_seconds == 60
    # Boundary is exclusive of expired events, not one extra second of denial.
    clock.advance(60)
    restarted.consume("user-c", "openai:different-alias")
    other = ProviderCallRateLimiter(AuthStore(path), clock=clock, shared_scope="other-egress")
    other.consume("user-d", "openai:model")


def test_shared_upstream_admission_is_atomic_across_store_instances(tmp_path):
    clock = FakeClock()
    limiters = [ProviderCallRateLimiter(AuthStore(tmp_path / "auth.db"), clock=clock,
                                      shared_scope="same-egress", shared_limit=3) for _ in range(4)]
    def attempt(index):
        try:
            limiters[index % 4].consume("user-" + str(index), "openai:auto:alias-" + str(index))
            return 1
        except LLMRateLimitExceeded:
            return 0
    with ThreadPoolExecutor(max_workers=12) as pool:
        assert sum(pool.map(attempt, range(20))) == 3


def test_shared_limit_does_not_count_local_mock_calls(tmp_path):
    limiter = ProviderCallRateLimiter(AuthStore(tmp_path / "auth.db"), shared_scope="egress", shared_limit=1)
    limiter.consume("user-a", "mock:model")
    limiter.consume("user-a", "openai:model")
    with pytest.raises(ValueError):
        ProviderCallRateLimiter(AuthStore(tmp_path / "auth.db"), shared_scope="http://secret/")
