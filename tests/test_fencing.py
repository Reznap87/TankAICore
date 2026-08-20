from __future__ import annotations

from pathlib import Path

import pytest

from tankai.dev_orchestrator.fencing import (
    FenceBusy,
    FenceLost,
    LeaseFenceStore,
)
from tankai.dev_orchestrator.job_queue import DevelopmentJobQueue


def test_monotonic_fence_rejects_stale_epoch_and_token(tmp_path: Path) -> None:
    store = LeaseFenceStore(tmp_path / "fences.db")
    first = store.acquire(
        scope_key="repository-1",
        job_id="job-1",
        owner_id="worker-1",
        lease_token="token-1",
        lease_seconds=60,
    )
    assert first.epoch == 1
    with pytest.raises(FenceBusy):
        store.acquire(
            scope_key="repository-1",
            job_id="job-2",
            owner_id="worker-2",
            lease_token="token-2",
            lease_seconds=60,
        )

    store.force_expire_for_recovery("repository-1", expected_epoch=first.epoch)
    second = store.acquire(
        scope_key="repository-1",
        job_id="job-2",
        owner_id="worker-2",
        lease_token="token-2",
        lease_seconds=60,
    )
    assert second.epoch == 2
    with pytest.raises(FenceLost, match="Epoche"):
        store.assert_active(
            scope_key="repository-1",
            job_id="job-1",
            epoch=first.epoch,
            lease_token="token-1",
        )
    with pytest.raises(FenceLost, match="Token"):
        store.assert_active(
            scope_key="repository-1",
            job_id="job-2",
            epoch=second.epoch,
            lease_token="wrong",
        )


def test_fence_renew_and_release_are_compare_and_swap_guarded(tmp_path: Path) -> None:
    store = LeaseFenceStore(tmp_path / "fences.db")
    lease = store.acquire(
        scope_key="repository-1",
        job_id="job-1",
        owner_id="worker-1",
        lease_token="token-1",
        lease_seconds=30,
    )
    renewed = store.renew(
        scope_key="repository-1",
        job_id="job-1",
        epoch=lease.epoch,
        lease_token="token-1",
        lease_seconds=120,
    )
    assert renewed.expires_at > lease.expires_at
    with pytest.raises(FenceLost):
        store.release(
            scope_key="repository-1",
            job_id="job-1",
            epoch=lease.epoch,
            lease_token="wrong",
            reason="invalid",
        )
    store.release(
        scope_key="repository-1",
        job_id="job-1",
        epoch=lease.epoch,
        lease_token="token-1",
        reason="complete",
    )
    current = store.current("repository-1")
    assert current is not None
    assert current.epoch == lease.epoch
    assert current.active is False
    next_lease = store.acquire(
        scope_key="repository-1",
        job_id="job-2",
        owner_id="worker-2",
        lease_token="token-2",
        lease_seconds=60,
    )
    assert next_lease.epoch == lease.epoch + 1


def test_operator_recovery_cannot_expire_newer_epoch(tmp_path: Path) -> None:
    store = LeaseFenceStore(tmp_path / "fences.db")
    lease = store.acquire(
        scope_key="repository-1",
        job_id="job-1",
        owner_id="worker-1",
        lease_token="token-1",
        lease_seconds=60,
    )
    with pytest.raises(FenceLost, match="Epoche"):
        store.force_expire_for_recovery("repository-1", expected_epoch=lease.epoch + 1)
    assert store.current("repository-1").active is True


def test_queue_and_fence_databases_must_be_separate(tmp_path: Path) -> None:
    same = tmp_path / "queue.db"
    with pytest.raises(ValueError, match="getrennt"):
        DevelopmentJobQueue(
            same,
            repository_base=tmp_path / "repositories",
            workspace_base=tmp_path / "worktrees",
            state_base=tmp_path / "states",
            fence_path=same,
        )
