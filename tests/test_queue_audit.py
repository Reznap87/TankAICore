"""Regression cases from the full queue lease/recovery audit."""
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from test_job_queue import queue_env, enqueue, init_repo, pipeline
from tankai.dev_orchestrator.job_queue import DevelopmentJobQueue, JobState, QueuedWorkerDispatcher


def test_blocked_first_candidate_page_cannot_starve_an_available_repository(queue_env):
    env, queue = queue_env, queue_env["queue"]
    policy = queue.get_policy(env["workspace"])
    policy.max_queued = 300
    policy.max_jobs_per_user_hour = 300
    queue.set_policy(actor_user_id=env["owner"], workspace_id=env["workspace"], policy=policy)
    for index in range(200):
        enqueue(env, key="blocked-" + str(index))
    queue.fence_store.acquire(scope_key=env["binding"].repository_id, job_id="other-active-job",
                             owner_id="other-worker", lease_token="other-secret", lease_seconds=300)
    repo = env["tmp"] / "repositories/available"
    init_repo(repo)
    available = queue.register_repository(actor_user_id=env["owner"], workspace_id=env["workspace"],
        name="Available", repository_path=repo, workspace_root=env["tmp"] / "worktrees/available",
        state_path=env["tmp"] / "states/available.json")
    job = queue.enqueue(actor_user_id=env["owner"], workspace_id=env["workspace"],
        repository_id=available.repository_id, pipeline=pipeline(), idempotency_key="available")
    lease = queue.claim_next(worker_id="available-worker")
    assert lease is not None and lease.job.job_id == job.job_id


@pytest.mark.parametrize("audit_available", [True, False])
def test_post_commit_fence_database_failure_does_not_reclassify_success(queue_env, monkeypatch, audit_available):
    env, queue = queue_env, queue_env["queue"]
    job = enqueue(env)
    calls = []
    original_connect = queue._connect
    def execute(*args):
        calls.append(True)
        return {"verified": True}
    def fail_release(**kwargs):
        if not audit_available:
            def unavailable():
                raise sqlite3.OperationalError("fixture audit database temporarily unavailable")
            monkeypatch.setattr(queue, "_connect", unavailable)
        raise sqlite3.OperationalError("fixture fence database temporarily unavailable")
    monkeypatch.setattr(queue.fence_store, "release", fail_release)
    dispatcher = QueuedWorkerDispatcher(queue, worker_id="worker", execute_pipeline=execute)
    result = dispatcher.run_once()
    monkeypatch.setattr(queue, "_connect", original_connect)
    assert result.state == JobState.SUCCEEDED
    assert queue.get_job(actor_user_id=env["owner"], workspace_id=env["workspace"], job_id=job.job_id).state == JobState.SUCCEEDED
    assert dispatcher.run_once() is None and len(calls) == 1
    assert queue.fence_store.current(env["binding"].repository_id).active


def test_independent_queue_instances_cannot_claim_one_job_twice(queue_env):
    env, first = queue_env, queue_env["queue"]
    job = enqueue(env)
    queues = [first] + [DevelopmentJobQueue(first.path, repository_base=first.repository_base,
        workspace_base=first.workspace_base, state_base=first.state_base) for _ in range(3)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(lambda item: item[1].claim_next(worker_id="worker-" + str(item[0])), enumerate(queues)))
    leases = [lease for lease in outcomes if lease is not None]
    assert len(leases) == 1 and leases[0].job.job_id == job.job_id
    assert leases[0].job.attempts == 1


def test_claim_transaction_failure_rolls_back_job_and_releases_external_fence(queue_env, monkeypatch):
    env, queue = queue_env, queue_env["queue"]
    job = enqueue(env)
    original_event = queue._event
    def fail_event(conn, job_id, event_type, *args):
        if event_type == "job_leased":
            raise sqlite3.OperationalError("fixture queue write failed after fence acquisition")
        return original_event(conn, job_id, event_type, *args)
    monkeypatch.setattr(queue, "_event", fail_event)
    with pytest.raises(sqlite3.OperationalError):
        queue.claim_next(worker_id="failed-worker")
    state = queue.get_job(actor_user_id=env["owner"], workspace_id=env["workspace"], job_id=job.job_id)
    assert state.state == JobState.QUEUED and state.attempts == 0
    assert not queue.fence_store.current(env["binding"].repository_id).active
    monkeypatch.setattr(queue, "_event", original_event)
    lease = queue.claim_next(worker_id="new-worker")
    assert lease.job.job_id == job.job_id and lease.fence_epoch == 2
