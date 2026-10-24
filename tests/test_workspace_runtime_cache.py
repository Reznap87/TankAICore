"""Offline cache capacity, runtime leases, shutdown and persistence checks."""

import threading
from types import SimpleNamespace
from uuid import UUID

import pytest

from tankai.web.runtime import (
    WorkspaceHistory,
    WorkspaceRuntime,
    WorkspaceRuntimeManager,
    WorkspaceRuntimeUnavailable,
)


TENANT = str(UUID(int=1))


def workspace(number):
    return str(UUID(int=number + 100))


def lookup(manager, number):
    return manager.get(tenant_id=TENANT, workspace_id=workspace(number))


def lease(manager, number):
    return manager.acquire(tenant_id=TENANT, workspace_id=workspace(number))


class Clock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class FakeRuntime:
    def __init__(self, tenant_id, workspace_id):
        self.tenant_id = tenant_id
        self.workspace_id = workspace_id
        self.lock = threading.RLock()
        self.close_calls = 0

    def close(self):
        self.close_calls += 1


class FakeManager(WorkspaceRuntimeManager):
    def __init__(self, *args, **kwargs):
        self.created = []
        super().__init__(*args, **kwargs)

    def _create_runtime(self, tenant_id, workspace_id):
        runtime = FakeRuntime(tenant_id, workspace_id)
        self.created.append(runtime)
        return runtime


@pytest.fixture(autouse=True)
def deterministic_cache_environment(monkeypatch):
    monkeypatch.delenv("TANKAI_RUNTIME_CACHE_MAX", raising=False)
    monkeypatch.delenv("TANKAI_RUNTIME_IDLE_TTL_SECONDS", raising=False)


def test_cache_hits_reuse_runtime_and_capacity_evicts_least_recently_used(tmp_path):
    clock = Clock()
    manager = FakeManager(tmp_path, max_runtimes=2, idle_ttl_seconds=100, clock=clock)
    first = lookup(manager, 1)
    clock.advance(1)
    second = lookup(manager, 2)
    clock.advance(1)
    assert lookup(manager, 1) is first
    clock.advance(1)
    third = lookup(manager, 3)
    assert first.close_calls == 0
    assert second.close_calls == 1
    assert third.close_calls == 0
    assert len(manager._runtimes) == 2
    replacement = lookup(manager, 2)
    assert replacement is not second
    assert len(manager._runtimes) == 2
    manager.close()


def test_environment_controls_capacity_and_idle_ttl(tmp_path, monkeypatch):
    monkeypatch.setenv("TANKAI_RUNTIME_CACHE_MAX", "2")
    monkeypatch.setenv("TANKAI_RUNTIME_IDLE_TTL_SECONDS", "5")
    clock = Clock()
    manager = FakeManager(tmp_path, clock=clock)
    first = lookup(manager, 1)
    second = lookup(manager, 2)
    lookup(manager, 3)
    assert first.close_calls == 1
    clock.advance(6)
    lookup(manager, 4)
    assert second.close_calls == 1
    assert len(manager._runtimes) == 1
    manager.close()


def test_default_limits_and_explicit_overrides_are_used(tmp_path, monkeypatch):
    manager = FakeManager(tmp_path / "defaults")
    assert manager.max_runtimes == 32
    assert manager.idle_ttl_seconds == 900
    manager.close()
    monkeypatch.setenv("TANKAI_RUNTIME_CACHE_MAX", "0")
    monkeypatch.setenv("TANKAI_RUNTIME_IDLE_TTL_SECONDS", "0")
    manager = FakeManager(tmp_path / "explicit", max_runtimes=1, idle_ttl_seconds=2)
    assert manager.max_runtimes == 1
    assert manager.idle_ttl_seconds == 2
    manager.close()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("TANKAI_RUNTIME_CACHE_MAX", "0"),
        ("TANKAI_RUNTIME_CACHE_MAX", "1025"),
        ("TANKAI_RUNTIME_CACHE_MAX", "invalid"),
        ("TANKAI_RUNTIME_IDLE_TTL_SECONDS", "0"),
        ("TANKAI_RUNTIME_IDLE_TTL_SECONDS", "86401"),
        ("TANKAI_RUNTIME_IDLE_TTL_SECONDS", "1.5"),
    ],
)
def test_invalid_environment_bounds_fail_closed(tmp_path, monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises((ValueError, RuntimeError)):
        FakeManager(tmp_path)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_runtimes": 0}, {"max_runtimes": 1025}, {"max_runtimes": True},
        {"max_runtimes": 1.5}, {"idle_ttl_seconds": 0},
        {"idle_ttl_seconds": 86401}, {"idle_ttl_seconds": True},
        {"idle_ttl_seconds": 1.5},
    ],
)
def test_invalid_explicit_cache_bounds_fail_closed(tmp_path, kwargs):
    with pytest.raises((ValueError, RuntimeError)):
        FakeManager(tmp_path, **kwargs)


def test_idle_expiry_is_lazy_and_cache_hits_refresh_idle_time(tmp_path):
    clock = Clock()
    manager = FakeManager(tmp_path, max_runtimes=3, idle_ttl_seconds=10, clock=clock)
    first = lookup(manager, 1)
    clock.advance(9)
    assert lookup(manager, 1) is first
    clock.advance(9)
    lookup(manager, 2)
    assert first.close_calls == 0
    clock.advance(2)
    assert first.close_calls == 0  # No background closer runs.
    lookup(manager, 3)
    assert first.close_calls == 1
    manager.close()


def test_active_lease_is_not_expired_or_evicted_and_capacity_is_hard(tmp_path):
    clock = Clock()
    manager = FakeManager(tmp_path, max_runtimes=1, idle_ttl_seconds=1, clock=clock)
    with lease(manager, 1) as first:
        clock.advance(10)
        with pytest.raises(WorkspaceRuntimeUnavailable):
            lookup(manager, 2)
        assert first.close_calls == 0
        assert len(manager.created) == 1
        assert len(manager._runtimes) == 1
    second = lookup(manager, 2)
    assert first.close_calls == 1
    assert second is not first
    manager.close()


def test_lru_skips_leased_runtime_and_evicts_idle_runtime(tmp_path):
    manager = FakeManager(tmp_path, max_runtimes=2)
    with lease(manager, 1) as first:
        second = lookup(manager, 2)
        third = lookup(manager, 3)
        assert first.close_calls == 0
        assert second.close_calls == 1
        assert third.close_calls == 0
        assert len(manager._runtimes) == 2
    manager.close()


def test_active_time_does_not_consume_idle_ttl_after_release(tmp_path):
    clock = Clock()
    manager = FakeManager(tmp_path, max_runtimes=2, idle_ttl_seconds=10, clock=clock)
    with lease(manager, 1) as first:
        clock.advance(100)
    clock.advance(9)
    lookup(manager, 2)
    assert first.close_calls == 0
    assert lookup(manager, 1) is first
    manager.close()


def test_lock_owned_by_another_thread_cannot_be_evicted(tmp_path):
    manager = FakeManager(tmp_path, max_runtimes=1)
    runtime = lookup(manager, 1)
    held = threading.Event()
    release = threading.Event()

    def hold():
        with runtime.lock:
            held.set()
            assert release.wait(5)

    thread = threading.Thread(target=hold)
    thread.start()
    try:
        assert held.wait(5)
        with pytest.raises(WorkspaceRuntimeUnavailable):
            lookup(manager, 2)
        assert runtime.close_calls == 0
    finally:
        release.set()
        thread.join(5)
        manager.close()
    assert not thread.is_alive()


def test_reentrant_lock_owned_by_current_thread_cannot_be_evicted(tmp_path):
    manager = FakeManager(tmp_path, max_runtimes=1)
    runtime = lookup(manager, 1)
    try:
        with runtime.lock:
            with pytest.raises(WorkspaceRuntimeUnavailable):
                lookup(manager, 2)
            assert runtime.close_calls == 0
        lookup(manager, 2)
        assert runtime.close_calls == 1
    finally:
        manager.close()


def test_acquire_holds_workspace_lock_until_context_exits(tmp_path):
    manager = FakeManager(tmp_path, max_runtimes=1)
    with lease(manager, 1) as runtime:
        observed = []

        def probe():
            acquired = runtime.lock.acquire(blocking=False)
            observed.append(acquired)
            if acquired:
                runtime.lock.release()

        thread = threading.Thread(target=probe)
        thread.start()
        thread.join(5)
        assert not thread.is_alive()
        assert observed == [False]
    manager.close()


def test_acquire_exception_releases_lease_and_lock(tmp_path):
    manager = FakeManager(tmp_path, max_runtimes=1)
    with pytest.raises(ValueError, match="request failed"):
        with lease(manager, 1) as runtime:
            raise ValueError("request failed")
    lookup(manager, 2)
    assert runtime.close_calls == 1
    manager.close()


class WaitAwareLock:
    """Expose a queued acquisition without relying on a timing-only sleep."""

    def __init__(self):
        self._lock = threading.RLock()
        self.queued = threading.Event()

    def acquire(self, blocking=True, timeout=-1):
        if blocking and threading.current_thread().name == "queued-request":
            self.queued.set()
        return self._lock.acquire(blocking, timeout)

    def release(self):
        self._lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *args):
        self.release()


def test_shutdown_defers_close_until_active_and_already_queued_leases_finish(tmp_path):
    manager = FakeManager(tmp_path, max_runtimes=1)
    runtime = lookup(manager, 1)
    runtime.lock = WaitAwareLock()
    owner_ready = threading.Event()
    owner_release = threading.Event()
    queued_ready = threading.Event()
    queued_release = threading.Event()
    errors = []

    def request(ready, release):
        try:
            with lease(manager, 1) as acquired:
                assert acquired is runtime
                assert runtime.close_calls == 0
                ready.set()
                assert release.wait(5)
                assert runtime.close_calls == 0
        except BaseException as exc:
            errors.append(exc)

    owner = threading.Thread(target=request, args=(owner_ready, owner_release), name="active-request")
    queued = threading.Thread(target=request, args=(queued_ready, queued_release), name="queued-request")
    owner.start()
    try:
        assert owner_ready.wait(5)
        queued.start()
        assert runtime.lock.queued.wait(5)
        manager.close()
        assert runtime.close_calls == 0
        with pytest.raises(WorkspaceRuntimeUnavailable):
            lookup(manager, 2)
        with pytest.raises(WorkspaceRuntimeUnavailable):
            with lease(manager, 1):
                pytest.fail("A new request was accepted after shutdown")
        owner_release.set()
        assert queued_ready.wait(5)
        assert runtime.close_calls == 0
        queued_release.set()
        owner.join(5)
        queued.join(5)
        assert not owner.is_alive()
        assert not queued.is_alive()
        assert errors == []
        assert runtime.close_calls == 1
        manager.close()
        assert runtime.close_calls == 1
    finally:
        owner_release.set()
        queued_release.set()
        owner.join(5)
        if queued.ident is not None:
            queued.join(5)
        manager.close()


def test_manager_shutdown_is_terminal_and_idempotent(tmp_path):
    manager = FakeManager(tmp_path, max_runtimes=2)
    first = lookup(manager, 1)
    second = lookup(manager, 2)
    manager.close()
    manager.close()
    assert first.close_calls == second.close_calls == 1
    for number in (1, 3):
        with pytest.raises(WorkspaceRuntimeUnavailable):
            lookup(manager, number)
    assert len(manager.created) == 2


class CloseResource:
    def __init__(self, failures=0):
        self.calls = 0
        self.failures = failures

    def close(self):
        self.calls += 1
        if self.failures:
            self.failures -= 1
            raise RuntimeError("close failed")


def make_runtime(tmp_path, ltm, memory):
    return WorkspaceRuntime(
        workspace_id=workspace(1), tenant_id=TENANT, root=tmp_path,
        tank=SimpleNamespace(ltm=ltm, memory=memory),
        history=WorkspaceHistory(tmp_path / "history.jsonl"),
        lock=threading.RLock(),
    )


def test_runtime_close_attempts_every_resource_and_successful_close_is_idempotent(tmp_path):
    ltm, memory = CloseResource(), CloseResource()
    runtime = make_runtime(tmp_path, ltm, memory)
    runtime.close()
    runtime.close()
    assert (ltm.calls, memory.calls) == (1, 1)


def test_runtime_close_failure_does_not_skip_remaining_resources_or_reclose_successes(tmp_path):
    ltm, memory = CloseResource(failures=1), CloseResource()
    runtime = make_runtime(tmp_path, ltm, memory)
    with pytest.raises(RuntimeError):
        runtime.close()
    assert (ltm.calls, memory.calls) == (1, 1)
    runtime.close()
    runtime.close()
    assert (ltm.calls, memory.calls) == (2, 1)


def test_failed_eviction_retains_capacity_until_resources_can_close(tmp_path):
    class FailureManager(FakeManager):
        def _create_runtime(self, tenant_id, workspace_id):
            if not self.created:
                runtime = make_runtime(tmp_path, CloseResource(failures=1), CloseResource())
                self.created.append(runtime)
                return runtime
            return super()._create_runtime(tenant_id, workspace_id)

    manager = FailureManager(tmp_path, max_runtimes=1)
    first = lookup(manager, 1)
    with pytest.raises(RuntimeError):
        lookup(manager, 2)
    assert len(manager._runtimes) == 1
    assert len(manager.created) == 1
    assert first in manager._runtimes.values()
    replacement = lookup(manager, 1)
    assert replacement is not first
    assert first._closed is True
    assert len(manager._runtimes) == 1
    manager.close()


class PersistentCloseFailureManager(FakeManager):
    def __init__(self, *args, **kwargs):
        self.failing_resource = CloseResource(failures=100)
        super().__init__(*args, **kwargs)

    def _create_runtime(self, tenant_id, workspace_id):
        if workspace_id == workspace(1):
            runtime = make_runtime(self.data_root, self.failing_resource, CloseResource())
            self.created.append(runtime)
            return runtime
        return super()._create_runtime(tenant_id, workspace_id)


def test_expired_workspace_cleanup_failure_does_not_block_healthy_cached_workspace(tmp_path):
    clock = Clock()
    manager = PersistentCloseFailureManager(
        tmp_path, max_runtimes=2, idle_ttl_seconds=10, clock=clock,
    )
    try:
        broken = lookup(manager, 1)
        clock.advance(5)
        healthy = lookup(manager, 2)
        clock.advance(6)
        assert lookup(manager, 2) is healthy
        assert broken._closing is True
        assert broken._closed is False
        assert healthy.close_calls == 0
        assert len(manager._runtimes) == 2
        assert len(manager.created) == 2
        assert len(manager._cleanup_failures) == 1
        with pytest.raises(WorkspaceRuntimeUnavailable):
            lookup(manager, 1)
        assert lookup(manager, 2) is healthy
        assert len(manager._cleanup_failures) == 1
    finally:
        manager.failing_resource.failures = 0
        manager.close()


def test_capacity_scan_skips_cleanup_failure_and_evicts_healthy_idle_workspace(tmp_path):
    clock = Clock()
    manager = PersistentCloseFailureManager(
        tmp_path, max_runtimes=2, idle_ttl_seconds=100, clock=clock,
    )
    try:
        broken = lookup(manager, 1)
        clock.advance(1)
        healthy = lookup(manager, 2)
        clock.advance(1)
        admitted = lookup(manager, 3)
        assert broken._closing is True
        assert broken._closed is False
        assert healthy.close_calls == 1
        assert admitted.close_calls == 0
        assert len(manager._runtimes) == 2
        assert len(manager.created) == 3
        assert broken in manager._runtimes.values()
        assert healthy not in manager._runtimes.values()
        assert admitted in manager._runtimes.values()
        assert len(manager._cleanup_failures) == 1
        with pytest.raises(WorkspaceRuntimeUnavailable):
            lookup(manager, 1)
        assert lookup(manager, 3) is admitted
        assert len(manager._runtimes) == 2
    finally:
        manager.failing_resource.failures = 0
        manager.close()


def test_real_eviction_and_recreate_preserves_local_workspace_data(tmp_path, monkeypatch):
    monkeypatch.setenv("TANKAI_LLM", "mock")
    monkeypatch.delenv("TANKAI_CRITIC_LLM", raising=False)
    monkeypatch.delenv("TANKAI_SEARCH_PROVIDER", raising=False)
    monkeypatch.setenv("TANKAI_EMBEDDER", "hashing")
    manager = WorkspaceRuntimeManager(tmp_path, max_runtimes=1)
    try:
        with lease(manager, 1) as original:
            memory_entry = original.tank.memory.add("Persistent test memory", "test", validity="valid")
            original.tank.ltm.add_semantic("Local archive persistence", source="test")
            original.history.append({"goal": "Persist history", "result": "done"})
            root = original.root
        lookup(manager, 2)
        with lease(manager, 1) as restored:
            assert restored is not original
            assert restored.root == root
            assert restored.tank.memory.get(memory_entry.id).content == "Persistent test memory"
            assert any(hit["content"] == "Local archive persistence"
                       for hit in restored.tank.ltm.retrieve("Local archive persistence", k=3))
            assert restored.history.list_recent()[0]["goal"] == "Persist history"
    finally:
        manager.close()
