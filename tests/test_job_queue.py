from __future__ import annotations

import json
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tankai.dev_orchestrator.queue_cli import main as queue_cli_main
from tankai.dev_orchestrator.job_queue import (
    AdmissionDenied,
    DevelopmentJobQueue,
    JobState,
    LeaseError,
    QueueError,
    QueuedWorkerDispatcher,
    WorkspaceQueuePolicy,
)
from tankai.dev_orchestrator.models import (
    CommandSpec,
    GateJob,
    WorkerIsolationSpec,
    WorkerJob,
    WorkerPipelineJob,
)
from tankai.web.auth import AuthStore

IMAGE = "tankai-worker@sha256:" + "a" * 64
OTHER_IMAGE = "tankai-worker@sha256:" + "b" * 64


def init_repo(path: Path) -> None:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, stdout=subprocess.PIPE)
    subprocess.run(["git", "config", "user.name", "Queue Test"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "queue@example.invalid"], cwd=path, check=True)
    (path / "README.md").write_text("queue baseline\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "baseline"], cwd=path, check=True, stdout=subprocess.PIPE)


def pipeline(*, image: str = IMAGE, memory_mb: int = 256, cpus: float = 1.0, timeout: float = 10) -> WorkerPipelineJob:
    command = CommandSpec(argv=["python", "-c", "pass"], timeout_seconds=timeout)
    return WorkerPipelineJob(
        worker=WorkerJob(
            agent_id="AGENT_BACKEND_01",
            implementation_summary="Implement queue test task",
            commit_message="Implement queue test task",
            implementation_commands=[command],
            test_commands=[command],
        ),
        gates=GateJob(
            reviewer_agent_id="AGENT_REVIEWER_01",
            review_commands=[command],
            qa_agent_id="AGENT_QA_01",
            qa_commands=[command],
        ),
        isolation=WorkerIsolationSpec(
            image=image,
            memory_mb=memory_mb,
            cpus=cpus,
            pids_limit=64,
            user="1000:1000",
        ),
    )


@pytest.fixture
def queue_env(tmp_path: Path):
    auth = AuthStore(tmp_path / "auth.db")
    owner, tenant, workspace = auth.create_user_with_tenant(
        email="owner@example.com",
        password="Owner-password-123",
        display_name="Owner",
        tenant_name="Tenant",
    )
    member, _, _ = auth.create_user_with_tenant(
        email="member@example.com",
        password="Member-password-123",
        display_name="Member",
        tenant_name="Member tenant",
    )
    auth.add_user_to_workspace(user_id=member, workspace_id=workspace, role="member")
    foreign, foreign_tenant, foreign_workspace = auth.create_user_with_tenant(
        email="foreign@example.com",
        password="Foreign-password-123",
        display_name="Foreign",
        tenant_name="Foreign tenant",
    )
    repo_base = tmp_path / "repositories"
    work_base = tmp_path / "worktrees"
    state_base = tmp_path / "states"
    repo = repo_base / "project"
    init_repo(repo)
    queue = DevelopmentJobQueue(
        tmp_path / "queue.db",
        auth_store=auth,
        repository_base=repo_base,
        workspace_base=work_base,
        state_base=state_base,
    )
    policy = WorkspaceQueuePolicy(
        tenant_id=tenant,
        workspace_id=workspace,
        max_queued=3,
        max_running=1,
        max_memory_mb=512,
        max_cpus=2,
        max_pids=128,
        max_runtime_seconds=120,
        max_attempts=2,
        max_jobs_per_user_hour=5,
        submit_roles=["owner", "admin", "member"],
        allowed_images=[IMAGE],
    )
    queue.set_policy(actor_user_id=owner, workspace_id=workspace, policy=policy)
    binding = queue.register_repository(
        actor_user_id=owner,
        workspace_id=workspace,
        name="Main",
        repository_path=repo,
        workspace_root=work_base / "project",
        state_path=state_base / "project-state.json",
    )
    return {
        "auth": auth,
        "queue": queue,
        "owner": owner,
        "member": member,
        "tenant": tenant,
        "workspace": workspace,
        "foreign": foreign,
        "foreign_tenant": foreign_tenant,
        "foreign_workspace": foreign_workspace,
        "binding": binding,
        "tmp": tmp_path,
    }


def enqueue(env, *, user=None, key="job-1", priority=0, job=None):
    return env["queue"].enqueue(
        actor_user_id=user or env["owner"],
        workspace_id=env["workspace"],
        repository_id=env["binding"].repository_id,
        pipeline=job or pipeline(),
        idempotency_key=key,
        priority=priority,
    )


def test_auth_store_exposes_exact_workspace_binding(queue_env) -> None:
    env = queue_env
    access = env["auth"].workspace_access(env["member"], env["workspace"])
    assert access is not None
    assert access.tenant_id == env["tenant"]
    assert access.role == "member"
    assert env["auth"].workspace_access(env["foreign"], env["workspace"]) is None


def test_only_admin_can_set_policy_or_register_repository(queue_env) -> None:
    env = queue_env
    with pytest.raises(PermissionError):
        env["queue"].set_policy(
            actor_user_id=env["member"],
            workspace_id=env["workspace"],
            policy=env["queue"].get_policy(env["workspace"]),
        )
    outside = env["tmp"] / "outside"
    init_repo(outside)
    with pytest.raises(PermissionError):
        env["queue"].register_repository(
            actor_user_id=env["member"],
            workspace_id=env["workspace"],
            name="Forbidden",
            repository_path=outside,
            workspace_root=env["tmp"] / "worktrees" / "forbidden",
            state_path=env["tmp"] / "states" / "forbidden.json",
        )


def test_repository_paths_are_fail_closed(queue_env) -> None:
    env = queue_env
    outside = env["tmp"] / "outside-repo"
    init_repo(outside)
    with pytest.raises(AdmissionDenied, match="außerhalb"):
        env["queue"].register_repository(
            actor_user_id=env["owner"],
            workspace_id=env["workspace"],
            name="Outside",
            repository_path=outside,
            workspace_root=env["tmp"] / "worktrees" / "outside",
            state_path=env["tmp"] / "states" / "outside.json",
        )


def test_enqueue_is_tenant_bound_and_idempotent(queue_env) -> None:
    env = queue_env
    first = enqueue(env, user=env["member"])
    second = enqueue(env, user=env["member"])
    assert first.job_id == second.job_id
    assert first.user_id == env["member"]
    assert first.tenant_id == env["tenant"]
    assert first.workspace_id == env["workspace"]
    with pytest.raises(AdmissionDenied, match="anderen Auftrag"):
        enqueue(env, user=env["member"], job=pipeline(memory_mb=300))
    with pytest.raises(PermissionError):
        env["queue"].enqueue(
            actor_user_id=env["foreign"],
            workspace_id=env["foreign_workspace"],
            repository_id=env["binding"].repository_id,
            pipeline=pipeline(),
            idempotency_key="foreign",
        )


def test_admission_blocks_image_resource_and_runtime_overruns(queue_env) -> None:
    env = queue_env
    with pytest.raises(AdmissionDenied, match="Image"):
        enqueue(env, key="bad-image", job=pipeline(image=OTHER_IMAGE))
    with pytest.raises(AdmissionDenied, match="RAM"):
        enqueue(env, key="bad-memory", job=pipeline(memory_mb=1024))
    with pytest.raises(AdmissionDenied, match="CPU"):
        enqueue(env, key="bad-cpu", job=pipeline(cpus=3))
    with pytest.raises(AdmissionDenied, match="Laufzeit"):
        enqueue(env, key="bad-runtime", job=pipeline(timeout=40))


def test_claim_is_priority_ordered_and_respects_workspace_concurrency(queue_env) -> None:
    env = queue_env
    low = enqueue(env, key="low", priority=-1)
    high = enqueue(env, key="high", priority=10)
    lease = env["queue"].claim_next(worker_id="worker-1", lease_seconds=60)
    assert lease is not None and lease.job.job_id == high.job_id
    assert env["queue"].claim_next(worker_id="worker-2", lease_seconds=60) is None
    env["queue"].start_job(job_id=high.job_id, lease_token=lease.lease_token)
    env["queue"].complete_job(job_id=high.job_id, lease_token=lease.lease_token, result={"ok": True})
    second = env["queue"].claim_next(worker_id="worker-2", lease_seconds=60)
    assert second is not None and second.job.job_id == low.job_id


def test_lease_token_is_required_and_completion_is_persisted(queue_env) -> None:
    env = queue_env
    job = enqueue(env)
    lease = env["queue"].claim_next(worker_id="worker-1", lease_seconds=60)
    assert lease is not None
    with pytest.raises(LeaseError):
        env["queue"].start_job(job_id=job.job_id, lease_token="wrong")
    env["queue"].start_job(job_id=job.job_id, lease_token=lease.lease_token)
    env["queue"].complete_job(
        job_id=job.job_id,
        lease_token=lease.lease_token,
        result={"run_id": "real-run"},
    )
    stored = env["queue"].get_job(
        actor_user_id=env["owner"], workspace_id=env["workspace"], job_id=job.job_id
    )
    assert stored.state == JobState.SUCCEEDED
    assert stored.result == {"run_id": "real-run"}
    assert stored.lease_expires_at is None


def test_failed_job_can_requeue_only_within_attempt_budget(queue_env) -> None:
    env = queue_env
    job = enqueue(env)
    lease1 = env["queue"].claim_next(worker_id="worker-1", lease_seconds=60)
    assert lease1 is not None
    state = env["queue"].fail_job(
        job_id=job.job_id,
        lease_token=lease1.lease_token,
        error="temporary",
        retryable=True,
    )
    assert state == JobState.QUEUED
    lease2 = env["queue"].claim_next(worker_id="worker-2", lease_seconds=60)
    assert lease2 is not None
    state = env["queue"].fail_job(
        job_id=job.job_id,
        lease_token=lease2.lease_token,
        error="still broken",
        retryable=True,
    )
    assert state == JobState.FAILED


def test_queue_expiry_does_not_supersede_live_external_fence(queue_env) -> None:
    env = queue_env
    job = enqueue(env)
    lease = env["queue"].claim_next(worker_id="live-worker", lease_seconds=60)
    assert lease is not None
    with sqlite3.connect(env["queue"].path) as conn:
        expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        conn.execute("UPDATE development_jobs SET lease_expires_at=? WHERE id=?", (expired, job.job_id))
        conn.commit()
    assert env["queue"].claim_next(worker_id="new-worker", lease_seconds=60) is None
    stored = env["queue"].get_job(
        actor_user_id=env["owner"], workspace_id=env["workspace"], job_id=job.job_id
    )
    assert stored.state == JobState.LEASED
    assert stored.lease_expires_at is not None
    assert stored.lease_expires_at > datetime.now(timezone.utc)


def test_expired_queue_and_external_fence_are_recovered_with_new_epoch(queue_env) -> None:
    env = queue_env
    job = enqueue(env)
    lease = env["queue"].claim_next(worker_id="dead-worker", lease_seconds=60)
    assert lease is not None
    env["queue"].fence_store.force_expire_for_recovery(
        env["binding"].repository_id, expected_epoch=lease.fence_epoch
    )
    with sqlite3.connect(env["queue"].path) as conn:
        expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        conn.execute("UPDATE development_jobs SET lease_expires_at=? WHERE id=?", (expired, job.job_id))
        conn.commit()
    recovered = env["queue"].claim_next(worker_id="new-worker", lease_seconds=60)
    assert recovered is not None
    assert recovered.job.job_id == job.job_id
    assert recovered.job.attempts == 2
    assert recovered.fence_epoch == lease.fence_epoch + 1
    with pytest.raises(LeaseError, match="Lease-Token|Fence"):
        env["queue"].assert_lease_active(job_id=job.job_id, lease_token=lease.lease_token)


def test_cancel_is_scoped_to_creator_or_workspace_admin(queue_env) -> None:
    env = queue_env
    job = enqueue(env, user=env["member"])
    cancelled = env["queue"].cancel_job(
        actor_user_id=env["owner"], workspace_id=env["workspace"], job_id=job.job_id
    )
    assert cancelled.state == JobState.CANCELLED
    with pytest.raises(PermissionError):
        env["queue"].get_job(
            actor_user_id=env["foreign"],
            workspace_id=env["foreign_workspace"],
            job_id=job.job_id,
        )


def test_dispatcher_marks_real_callback_result_and_failure(queue_env) -> None:
    env = queue_env
    success = enqueue(env, key="success")
    dispatcher = QueuedWorkerDispatcher(
        env["queue"],
        worker_id="dispatcher-1",
        execute_pipeline=lambda repository, submitted: {
            "repository_id": repository.repository_id,
            "agent_id": submitted.worker.agent_id,
        },
    )
    result = dispatcher.run_once()
    assert result is not None
    assert result.job_id == success.job_id
    assert result.state == JobState.SUCCEEDED

    failed = enqueue(env, key="failure")
    broken = QueuedWorkerDispatcher(
        env["queue"],
        worker_id="dispatcher-2",
        execute_pipeline=lambda repository, submitted: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    result = broken.run_once()
    assert result is not None and result.job_id == failed.job_id
    assert result.state == JobState.FAILED
    stored = env["queue"].get_job(
        actor_user_id=env["owner"], workspace_id=env["workspace"], job_id=failed.job_id
    )
    assert stored.state == JobState.FAILED
    assert "boom" in stored.error






def test_operator_fence_recovery_requires_admin_and_exact_confirmation(queue_env) -> None:
    env = queue_env
    job = enqueue(env, key="operator-fence")
    lease = env["queue"].claim_next(worker_id="worker-1", lease_seconds=60)
    assert lease is not None
    with pytest.raises(PermissionError):
        env["queue"].fence_status(
            actor_user_id=env["member"],
            workspace_id=env["workspace"],
            repository_id=env["binding"].repository_id,
        )
    status = env["queue"].fence_status(
        actor_user_id=env["owner"],
        workspace_id=env["workspace"],
        repository_id=env["binding"].repository_id,
    )
    assert status is not None
    assert status["active"] is True
    assert status["epoch"] == lease.fence_epoch
    with pytest.raises(LeaseError, match="bestätigten Job"):
        env["queue"].force_expire_fence(
            actor_user_id=env["owner"],
            workspace_id=env["workspace"],
            repository_id=env["binding"].repository_id,
            expected_epoch=lease.fence_epoch,
            expected_job_id="wrong-job",
        )
    expired = env["queue"].force_expire_fence(
        actor_user_id=env["owner"],
        workspace_id=env["workspace"],
        repository_id=env["binding"].repository_id,
        expected_epoch=lease.fence_epoch,
        expected_job_id=job.job_id,
    )
    assert expired["active"] is False

def test_dispatcher_never_marks_success_after_external_fence_loss(queue_env) -> None:
    env = queue_env
    job = enqueue(env, key="fence-loss")

    def lose_fence(repository, submitted):
        current = env["queue"].fence_store.current(repository.repository_id)
        assert current is not None and current.active and current.job_id == job.job_id
        env["queue"].fence_store.force_expire_for_recovery(
            repository.repository_id, expected_epoch=current.epoch
        )
        return {"would_have_been": "success"}

    dispatcher = QueuedWorkerDispatcher(
        env["queue"],
        worker_id="fenced-worker",
        execute_pipeline=lose_fence,
    )
    with pytest.raises(QueueError, match="Queue-Status"):
        dispatcher.run_once(lease_seconds=60)
    stored = env["queue"].get_job(
        actor_user_id=env["owner"], workspace_id=env["workspace"], job_id=job.job_id
    )
    assert stored.state == JobState.RUNNING
    assert stored.result is None

def test_inline_secrets_are_rejected(queue_env) -> None:
    env = queue_env
    secret_command = CommandSpec(
        argv=["python", "-c", "pass"],
        env={"OPENAI_API_KEY": "do-not-store-this"},
    )
    submitted = pipeline().model_copy(update={
        "worker": pipeline().worker.model_copy(update={
            "implementation_commands": [secret_command]
        })
    })
    with pytest.raises(AdmissionDenied, match="Inline-Secrets"):
        enqueue(env, key="secret", job=submitted)


def test_payload_tampering_is_detected_before_worker_lease(queue_env) -> None:
    env = queue_env
    job = enqueue(env)
    with sqlite3.connect(env["queue"].path) as conn:
        conn.execute(
            "UPDATE development_jobs SET payload_json=? WHERE id=?",
            ('{"worker":{}}', job.job_id),
        )
        conn.commit()
    assert env["queue"].claim_next(worker_id="worker", lease_seconds=60) is None
    with sqlite3.connect(env["queue"].path) as conn:
        row = conn.execute("SELECT state,error FROM development_jobs WHERE id=?", (job.job_id,)).fetchone()
    assert row[0] == "failed"
    assert "Integritätsprüfung" in row[1]


def test_member_lists_only_own_jobs(queue_env) -> None:
    env = queue_env
    owner_job = enqueue(env, user=env["owner"], key="owner-job")
    member_job = enqueue(env, user=env["member"], key="member-job")
    member_jobs = env["queue"].list_jobs(
        actor_user_id=env["member"], workspace_id=env["workspace"]
    )
    assert [item.job_id for item in member_jobs] == [member_job.job_id]
    with pytest.raises(PermissionError):
        env["queue"].get_job(
            actor_user_id=env["member"],
            workspace_id=env["workspace"],
            job_id=owner_job.job_id,
        )


def test_dispatcher_renews_lease_during_long_execution(queue_env) -> None:
    import time

    env = queue_env
    job = enqueue(env)
    calls = {"heartbeats": 0}
    original = env["queue"].heartbeat

    def heartbeat(**kwargs):
        calls["heartbeats"] += 1
        return original(**kwargs)

    env["queue"].heartbeat = heartbeat  # type: ignore[method-assign]
    dispatcher = QueuedWorkerDispatcher(
        env["queue"],
        worker_id="heartbeat-worker",
        heartbeat_interval_seconds=0.01,
        execute_pipeline=lambda repository, submitted: (time.sleep(0.05) or {"ok": True}),
    )
    result = dispatcher.run_once(lease_seconds=60)
    assert result is not None and result.job_id == job.job_id
    assert result.state == JobState.SUCCEEDED
    assert calls["heartbeats"] >= 2


def test_queue_cli_configures_enqueues_lists_and_cancels(tmp_path, capsys) -> None:
    auth_db = tmp_path / "auth.db"
    auth = AuthStore(auth_db)
    owner, tenant, workspace = auth.create_user_with_tenant(
        email="cli-owner@example.com",
        password="CLI-owner-password-123",
        display_name="CLI Owner",
        tenant_name="CLI Tenant",
    )
    repo_base = tmp_path / "repositories"
    work_base = tmp_path / "worktrees"
    state_base = tmp_path / "states"
    repo = repo_base / "main"
    init_repo(repo)
    queue_db = tmp_path / "queue.db"
    common = [
        "--queue-db", str(queue_db),
        "--auth-db", str(auth_db),
        "--repository-base", str(repo_base),
        "--workspace-base", str(work_base),
        "--state-base", str(state_base),
    ]
    assert queue_cli_main(common + [
        "set-policy",
        "--actor-email", "cli-owner@example.com",
        "--workspace-id", workspace,
        "--allowed-image", IMAGE,
        "--max-runtime-seconds", "120",
    ]) == 0
    capsys.readouterr()
    assert queue_cli_main(common + [
        "register-repository",
        "--actor-email", "cli-owner@example.com",
        "--workspace-id", workspace,
        "--name", "Main",
        "--repository", str(repo),
        "--worktrees", str(work_base / "main"),
        "--state", str(state_base / "main.json"),
    ]) == 0
    repository = json.loads(capsys.readouterr().out)
    pipeline_path = tmp_path / "pipeline.json"
    pipeline_path.write_text(json.dumps(pipeline().model_dump(mode="json")), encoding="utf-8")
    assert queue_cli_main(common + [
        "enqueue",
        "--actor-email", "cli-owner@example.com",
        "--workspace-id", workspace,
        "--repository-id", repository["repository_id"],
        "--job", str(pipeline_path),
        "--idempotency-key", "cli-job",
    ]) == 0
    created = json.loads(capsys.readouterr().out)
    assert created["tenant_id"] == tenant
    assert created["state"] == "queued"
    assert queue_cli_main(common + [
        "list-jobs",
        "--actor-email", "cli-owner@example.com",
        "--workspace-id", workspace,
    ]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert [item["job_id"] for item in listed] == [created["job_id"]]
    assert queue_cli_main(common + [
        "cancel",
        "--actor-email", "cli-owner@example.com",
        "--workspace-id", workspace,
        "--job-id", created["job_id"],
    ]) == 0
    cancelled = json.loads(capsys.readouterr().out)
    assert cancelled["state"] == "cancelled"


def test_queue_cli_manages_service_agent_token_lifecycle(queue_env, capsys) -> None:
    env = queue_env
    common = [
        "--queue-db", str(env["tmp"] / "queue.db"),
        "--fence-db", str(env["queue"].fence_store.path),
        "--auth-db", str(env["tmp"] / "auth.db"),
        "--repository-base", str(env["tmp"] / "repositories"),
        "--workspace-base", str(env["tmp"] / "worktrees"),
        "--state-base", str(env["tmp"] / "states"),
    ]
    actor = [
        "--actor-email", "owner@example.com",
        "--workspace-id", env["workspace"],
    ]

    assert queue_cli_main(
        common
        + ["create-service-agent"]
        + actor
        + ["--name", "CLI Coder", "--description", "Operator-managed client"]
    ) == 0
    created = json.loads(capsys.readouterr().out)["agent"]
    assert created["name"] == "CLI Coder"
    assert created["workspace_id"] == env["workspace"]
    agent_id = created["agent_id"]

    assert queue_cli_main(common + ["list-service-agents"] + actor) == 0
    listed_agents = json.loads(capsys.readouterr().out)["agents"]
    assert [item["agent_id"] for item in listed_agents] == [agent_id]

    assert queue_cli_main(
        common
        + ["create-agent-token"]
        + actor
        + [
            "--agent-id", agent_id,
            "--scope", "repositories:read",
            "--scope", "jobs:submit",
            "--scope", "jobs:read",
            "--scope", "jobs:cancel",
            "--repository-id", env["binding"].repository_id,
            "--expires-in-days", "7",
            "--label", "CLI lifecycle",
        ]
    ) == 0
    created_token = json.loads(capsys.readouterr().out)["token"]
    secret = created_token["secret"]
    token_id = created_token["token_id"]
    assert secret.startswith("tkai_v1_")
    assert created_token["shown_once"] is True
    assert env["auth"].resolve_agent_token(secret) is not None

    assert queue_cli_main(
        common + ["list-agent-tokens"] + actor + ["--agent-id", agent_id]
    ) == 0
    listed_tokens = json.loads(capsys.readouterr().out)["tokens"]
    assert listed_tokens[0]["token_id"] == token_id
    assert "secret" not in listed_tokens[0]

    assert queue_cli_main(
        common
        + ["revoke-agent-token"]
        + actor
        + ["--agent-id", agent_id, "--token-id", token_id]
    ) == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True}
    assert env["auth"].resolve_agent_token(secret) is None

    assert queue_cli_main(
        common
        + ["create-agent-token"]
        + actor
        + [
            "--agent-id", agent_id,
            "--scope", "jobs:read",
            "--repository-id", env["binding"].repository_id,
        ]
    ) == 0
    replacement_secret = json.loads(capsys.readouterr().out)["token"]["secret"]
    assert queue_cli_main(
        common
        + ["deactivate-service-agent"]
        + actor
        + ["--agent-id", agent_id]
    ) == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True}
    assert env["auth"].resolve_agent_token(replacement_secret) is None

    assert queue_cli_main(common + ["list-service-agents"] + actor) == 0
    assert json.loads(capsys.readouterr().out)["agents"][0]["is_active"] is False


def test_queue_cli_rejects_member_and_unregistered_agent_scope(queue_env, capsys) -> None:
    env = queue_env
    common = [
        "--queue-db", str(env["tmp"] / "queue.db"),
        "--fence-db", str(env["queue"].fence_store.path),
        "--auth-db", str(env["tmp"] / "auth.db"),
        "--repository-base", str(env["tmp"] / "repositories"),
        "--workspace-base", str(env["tmp"] / "worktrees"),
        "--state-base", str(env["tmp"] / "states"),
    ]
    assert queue_cli_main(
        common
        + [
            "create-service-agent",
            "--actor-email", "member@example.com",
            "--workspace-id", env["workspace"],
            "--name", "Forbidden",
        ]
    ) == 2
    assert "Nur Owner oder Admins" in capsys.readouterr().out

    owner = [
        "--actor-email", "owner@example.com",
        "--workspace-id", env["workspace"],
    ]
    assert queue_cli_main(
        common + ["create-service-agent"] + owner + ["--name", "Scoped"]
    ) == 0
    agent_id = json.loads(capsys.readouterr().out)["agent"]["agent_id"]
    assert queue_cli_main(
        common
        + ["create-agent-token"]
        + owner
        + [
            "--agent-id", agent_id,
            "--scope", "jobs:read",
            "--repository-id", "00000000-0000-0000-0000-000000000001",
        ]
    ) == 2
    assert "muss zum Workspace gehören" in capsys.readouterr().out
    assert queue_cli_main(
        common + ["list-agent-tokens"] + owner + ["--agent-id", agent_id]
    ) == 0
    assert json.loads(capsys.readouterr().out) == {"tokens": []}


def test_queue_worker_cli_can_run_without_auth_db_when_idle(tmp_path, capsys) -> None:
    assert queue_cli_main([
        "--queue-db", str(tmp_path / "queue.db"),
        "--repository-base", str(tmp_path / "repositories"),
        "--workspace-base", str(tmp_path / "worktrees"),
        "--state-base", str(tmp_path / "states"),
        "run-worker",
        "--worker-id", "idle-worker",
        "--exit-when-idle",
        "--max-jobs", "1",
    ]) == 0
    output = capsys.readouterr().out
    assert '"processed": 0' in output
    assert '"failed": 0' in output


def test_default_policy_denies_member_submissions(tmp_path: Path) -> None:
    auth = AuthStore(tmp_path / "auth.db")
    owner, tenant, workspace = auth.create_user_with_tenant(
        email="owner-default@example.com",
        password="Owner-default-password-123",
        display_name="Owner",
        tenant_name="Tenant",
    )
    member, _, _ = auth.create_user_with_tenant(
        email="member-default@example.com",
        password="Member-default-password-123",
        display_name="Member",
        tenant_name="Other",
    )
    auth.add_user_to_workspace(user_id=member, workspace_id=workspace, role="member")
    repo_base = tmp_path / "repositories"
    work_base = tmp_path / "worktrees"
    state_base = tmp_path / "states"
    repo = repo_base / "main"
    init_repo(repo)
    queue = DevelopmentJobQueue(
        tmp_path / "queue.db",
        auth_store=auth,
        repository_base=repo_base,
        workspace_base=work_base,
        state_base=state_base,
    )
    queue.set_policy(
        actor_user_id=owner,
        workspace_id=workspace,
        policy=WorkspaceQueuePolicy(
            tenant_id=tenant,
            workspace_id=workspace,
            allowed_images=[IMAGE],
        ),
    )
    binding = queue.register_repository(
        actor_user_id=owner,
        workspace_id=workspace,
        name="Main",
        repository_path=repo,
        workspace_root=work_base / "main",
        state_path=state_base / "main.json",
    )
    with pytest.raises(PermissionError, match="Rolle"):
        queue.enqueue(
            actor_user_id=member,
            workspace_id=workspace,
            repository_id=binding.repository_id,
            pipeline=pipeline(),
            idempotency_key="member-denied",
        )


def test_member_cannot_raise_queue_priority(queue_env) -> None:
    env = queue_env
    with pytest.raises(AdmissionDenied, match="Priorität"):
        enqueue(env, user=env["member"], key="priority", priority=1)


def test_tightened_policy_invalidates_queued_job_before_lease(queue_env) -> None:
    env = queue_env
    job = enqueue(env, key="policy-tightening")
    current = env["queue"].get_policy(env["workspace"])
    assert current is not None
    env["queue"].set_policy(
        actor_user_id=env["owner"],
        workspace_id=env["workspace"],
        policy=current.model_copy(update={"allowed_images": [OTHER_IMAGE]}),
    )
    assert env["queue"].claim_next(worker_id="worker", lease_seconds=60) is None
    with sqlite3.connect(env["queue"].path) as conn:
        row = conn.execute("SELECT state,error FROM development_jobs WHERE id=?", (job.job_id,)).fetchone()
    assert row[0] == "failed"
    assert "nachträglich gesperrt" in row[1]


def test_queue_schema_v1_migrates_submit_roles_atomically(tmp_path: Path) -> None:
    db = tmp_path / "queue.db"
    with sqlite3.connect(db) as conn:
        conn.executescript(
            """
            CREATE TABLE queue_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            INSERT INTO queue_meta(key,value) VALUES('schema_version','1');
            CREATE TABLE queue_policies (
                workspace_id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                enabled INTEGER NOT NULL,
                max_queued INTEGER NOT NULL,
                max_running INTEGER NOT NULL,
                max_memory_mb INTEGER NOT NULL,
                max_cpus REAL NOT NULL,
                max_pids INTEGER NOT NULL,
                max_runtime_seconds INTEGER NOT NULL,
                max_attempts INTEGER NOT NULL,
                max_jobs_per_user_hour INTEGER NOT NULL,
                allowed_images_json TEXT NOT NULL,
                updated_by TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            INSERT INTO queue_policies VALUES(
                'workspace','tenant',1,20,2,2048,4.0,512,3600,3,20,
                '["sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"]',
                'owner','2026-01-01T00:00:00+00:00'
            );
            """
        )
    auth = AuthStore(tmp_path / "auth.db")
    queue = DevelopmentJobQueue(
        db,
        auth_store=auth,
        repository_base=tmp_path / "repositories",
        workspace_base=tmp_path / "worktrees",
        state_base=tmp_path / "states",
    )
    with sqlite3.connect(db) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(queue_policies)")}
        job_columns = {row[1] for row in conn.execute("PRAGMA table_info(development_jobs)")}
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(development_jobs)")}
        version = conn.execute(
            "SELECT value FROM queue_meta WHERE key='schema_version'"
        ).fetchone()[0]
        roles = conn.execute(
            "SELECT submit_roles_json FROM queue_policies WHERE workspace_id='workspace'"
        ).fetchone()[0]
    assert "submit_roles_json" in columns
    assert "fence_epoch" in job_columns
    assert "idx_jobs_active_repository" in indexes
    assert version == str(queue.SCHEMA_VERSION)
    assert json.loads(roles) == ["owner", "admin"]


def _initialize_reaper_state(env):
    from tankai.dev_orchestrator import (
        DevelopmentOrchestrator,
        DevelopmentRole,
        GitWorkspaceManager,
        TaskSpec,
    )

    repository = Path(env["binding"].repository_path)
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repository, check=True,
        text=True, stdout=subprocess.PIPE,
    ).stdout.strip()
    orchestrator = DevelopmentOrchestrator.initialize(
        env["binding"].state_path,
        current_version="reaper-test",
        current_branch="main",
        current_commit=commit,
    )
    orchestrator.create_task(TaskSpec(
        task_id="REAP-001",
        goal="Create reaper test workspace",
        base_commit=commit,
        allowed_paths=["src/**"],
        acceptance_criteria=["Worktree lifecycle is controlled"],
    ))
    agent = orchestrator.start_agent("REAP-001", DevelopmentRole.BACKEND)
    manager = GitWorkspaceManager(repository, env["binding"].workspace_root)
    workspace = manager.create_workspace(agent)
    orchestrator.bind_workspace(
        agent.agent_id, branch=workspace.branch, workspace_path=str(workspace.path)
    )
    return orchestrator, manager, agent, workspace


def test_operator_reaper_is_blocked_while_repository_fence_is_active(queue_env) -> None:
    env = queue_env
    enqueue(env, key="reaper-fence")
    lease = env["queue"].claim_next(worker_id="reaper-worker", lease_seconds=60)
    assert lease is not None
    with pytest.raises(LeaseError, match="aktivem Repository-Fence"):
        env["queue"].reap_worktrees(
            actor_user_id=env["owner"],
            workspace_id=env["workspace"],
            repository_id=env["binding"].repository_id,
            min_age_seconds=0,
            dry_run=False,
        )


def test_operator_reaper_removes_clean_terminal_workspace_and_unbinds_state(queue_env) -> None:
    env = queue_env
    orchestrator, manager, agent, workspace = _initialize_reaper_state(env)
    orchestrator.cancel_task("REAP-001", reason="test completed")

    result = env["queue"].reap_worktrees(
        actor_user_id=env["owner"],
        workspace_id=env["workspace"],
        repository_id=env["binding"].repository_id,
        min_age_seconds=0,
        dry_run=False,
    )
    assert any(item["action"] == "removed" for item in result)
    assert not workspace.path.exists()
    refreshed = orchestrator.state().agents[agent.agent_id]
    assert refreshed.workspace_path is None
    assert refreshed.branch is None
    assert manager.branch_head(workspace.branch)


def test_operator_reaper_requires_exact_confirmation_for_stale_active_run(queue_env) -> None:
    from tankai.dev_orchestrator import WorkerRunRecord, WorkerRunState

    env = queue_env
    orchestrator, _, agent, workspace = _initialize_reaper_state(env)
    run = WorkerRunRecord(
        run_id="RUN-STALE-REAPER",
        agent_id=agent.agent_id,
        task_id="REAP-001",
        base_commit=agent.base_commit,
        branch=workspace.branch,
        workspace_path=str(workspace.path),
    )
    orchestrator.begin_worker_run(run)

    preview = env["queue"].reap_worktrees(
        actor_user_id=env["owner"],
        workspace_id=env["workspace"],
        repository_id=env["binding"].repository_id,
        min_age_seconds=0,
        dry_run=True,
    )
    assert any(item["reason"] == "protected_by_project_state" for item in preview)

    applied = env["queue"].reap_worktrees(
        actor_user_id=env["owner"],
        workspace_id=env["workspace"],
        repository_id=env["binding"].repository_id,
        min_age_seconds=0,
        dry_run=False,
        expected_stale_run_id=run.run_id,
    )
    assert any(item["action"] == "removed" for item in applied)
    state = orchestrator.state()
    assert state.worker_runs[run.run_id].state == WorkerRunState.BLOCKED
    assert state.agents[agent.agent_id].workspace_path is None


def test_dispatcher_heartbeat_loss_actively_terminates_running_worker_command(queue_env) -> None:
    import os
    import time
    from tankai.dev_orchestrator import (
        DevelopmentOrchestrator,
        DevelopmentRole,
        TaskSpec,
        TestExecution,
    )
    from tankai.dev_orchestrator.process_control import run_bounded_process

    env = queue_env
    repository = Path(env["binding"].repository_path)
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repository, check=True,
        text=True, stdout=subprocess.PIPE,
    ).stdout.strip()
    orchestrator = DevelopmentOrchestrator.initialize(
        env["binding"].state_path,
        current_version="dispatcher-cancel-test",
        current_branch="main",
        current_commit=commit,
    )
    orchestrator.create_task(TaskSpec(
        task_id="QUEUE-CANCEL-001",
        goal="Cancel running queue worker",
        base_commit=commit,
        allowed_paths=["backend/**"],
        acceptance_criteria=["Command is terminated on lease loss"],
        required_tests=["python check"],
    ))
    author = orchestrator.start_agent("QUEUE-CANCEL-001", DevelopmentRole.BACKEND)
    for task_id, role in (("QUEUE-REVIEW", DevelopmentRole.REVIEWER), ("QUEUE-QA", DevelopmentRole.QA)):
        orchestrator.create_task(TaskSpec(
            task_id=task_id,
            goal=f"Support {role.value}",
            base_commit=commit,
            allowed_paths=[],
            acceptance_criteria=["Gate executed"],
        ))
        orchestrator.start_agent(task_id, role)

    started = env["tmp"] / "queue-command-started.txt"
    finished = env["tmp"] / "queue-command-finished.txt"
    implementation = CommandSpec(
        argv=[
            "python",
            "-c",
            (
                "from pathlib import Path; import time; "
                "Path('backend').mkdir(exist_ok=True); "
                "Path('backend/change.py').write_text('VALUE = 1\\n', encoding='utf-8'); "
                f"Path({str(started)!r}).write_text('started', encoding='utf-8'); "
                "time.sleep(30); "
                f"Path({str(finished)!r}).write_text('finished', encoding='utf-8')"
            ),
        ],
        timeout_seconds=30,
    )
    check = CommandSpec(argv=["python", "-c", "pass"], timeout_seconds=1)
    submitted = WorkerPipelineJob(
        worker=WorkerJob(
            agent_id=author.agent_id,
            implementation_summary="Cancellation test",
            commit_message="Cancellation test",
            implementation_commands=[implementation],
            test_commands=[check],
        ),
        gates=GateJob(
            reviewer_agent_id="AGENT_REVIEWER_01",
            review_commands=[check],
            qa_agent_id="AGENT_QA_01",
            qa_commands=[check],
        ),
        isolation=WorkerIsolationSpec(
            image=IMAGE,
            memory_mb=256,
            cpus=1.0,
            pids_limit=64,
            user="1000:1000",
        ),
    )
    enqueue(env, key="dispatcher-active-cancel", job=submitted)

    class LocalCancellableExecutor:
        def ensure_available(self):
            return "test-runtime"

        def execute(
            self,
            workspace,
            command,
            isolation,
            *,
            allowed_paths,
            denied_paths=(),
            read_only_workspace,
            run_id,
            phase,
            cancellation_check=None,
        ):
            completed = run_bounded_process(
                command.argv,
                cwd=workspace.path,
                env=os.environ.copy(),
                timeout_seconds=command.timeout_seconds,
                cancellation_check=cancellation_check,
            )
            return TestExecution(
                command=" ".join(command.argv),
                passed=completed.returncode == 0 and not completed.timed_out,
                exit_code=completed.returncode if not completed.timed_out else None,
                summary=completed.output,
            )

    original_heartbeat = env["queue"].heartbeat
    calls = {"count": 0}

    def failing_heartbeat(**kwargs):
        calls["count"] += 1
        if started.exists():
            raise LeaseError("simulated heartbeat failure")
        return original_heartbeat(**kwargs)

    env["queue"].heartbeat = failing_heartbeat  # type: ignore[method-assign]
    began = time.monotonic()
    result = QueuedWorkerDispatcher(
        env["queue"],
        worker_id="active-cancel-worker",
        heartbeat_interval_seconds=0.01,
        container_executor=LocalCancellableExecutor(),  # type: ignore[arg-type]
    ).run_once(lease_seconds=60)
    assert result is not None
    assert result.state == JobState.FAILED
    assert "simulated heartbeat failure" in result.error
    assert time.monotonic() - began < 5
    time.sleep(0.2)
    assert started.exists()
    assert not finished.exists()


def test_queue_cli_reaper_defaults_to_dry_run(queue_env, capsys) -> None:
    env = queue_env
    orchestrator, _, _, workspace = _initialize_reaper_state(env)
    orchestrator.cancel_task("REAP-001", reason="CLI dry-run")
    args = [
        "--queue-db", str(env["tmp"] / "queue.db"),
        "--fence-db", str(env["queue"].fence_store.path),
        "--auth-db", str(env["tmp"] / "auth.db"),
        "--repository-base", str(env["tmp"] / "repositories"),
        "--workspace-base", str(env["tmp"] / "worktrees"),
        "--state-base", str(env["tmp"] / "states"),
        "reap-worktrees",
        "--actor-email", "owner@example.com",
        "--workspace-id", env["workspace"],
        "--repository-id", env["binding"].repository_id,
        "--min-age-seconds", "0",
    ]
    assert queue_cli_main(args) == 0
    output = capsys.readouterr().out
    assert '"action": "candidate"' in output
    assert workspace.path.exists()


class RecordingContainerReaperExecutor:
    def __init__(self, records):
        self.records = list(records)
        self.removed: list[str] = []
        self.available_checks = 0

    def ensure_available(self) -> str:
        self.available_checks += 1
        return "rootless-test-runtime"

    def list_managed_containers(self, *, repository_id: str):
        return list(self.records)

    def remove_container(self, container_id: str) -> None:
        self.removed.append(container_id)


def _managed_container(env, *, job_id: str, fence_epoch: int, container_id: str = "b" * 64):
    from tankai.dev_orchestrator import ManagedContainerRecord

    return ManagedContainerRecord(
        container_id=container_id,
        name=f"tankai-{job_id.lower()}",
        state="running",
        created_at=datetime.now(timezone.utc) - timedelta(hours=2),
        labels={
            "tankai.managed": "true",
            "tankai.run_id": "RUN-CONTAINER-REAPER",
            "tankai.phase": "implement",
            "tankai.job_id": job_id,
            "tankai.repository_id": env["binding"].repository_id,
            "tankai.workspace_id": env["workspace"],
            "tankai.tenant_id": env["tenant"],
            "tankai.fence_epoch": str(fence_epoch),
            "tankai.worker_id": "runner-01",
        },
    )


def test_container_reaper_protects_live_queue_and_fence(queue_env) -> None:
    env = queue_env
    job = enqueue(env, key="container-live")
    lease = env["queue"].claim_next(worker_id="runner-live", lease_seconds=60)
    assert lease is not None
    fake = RecordingContainerReaperExecutor([
        _managed_container(env, job_id=job.job_id, fence_epoch=lease.fence_epoch)
    ])
    result = env["queue"].reap_containers(
        actor_user_id=env["owner"],
        workspace_id=env["workspace"],
        repository_id=env["binding"].repository_id,
        min_age_seconds=0,
        dry_run=False,
        container_executor=fake,
    )
    assert result[0]["action"] == "protected"
    assert result[0]["reason"] == "protected_by_live_queue_and_fence"
    assert fake.removed == []


def test_container_reaper_removes_terminal_job_container(queue_env) -> None:
    env = queue_env
    job = enqueue(env, key="container-terminal")
    lease = env["queue"].claim_next(worker_id="runner-terminal", lease_seconds=60)
    assert lease is not None
    env["queue"].start_job(job_id=job.job_id, lease_token=lease.lease_token)
    env["queue"].complete_job(
        job_id=job.job_id,
        lease_token=lease.lease_token,
        result={"ok": True},
    )
    record = _managed_container(env, job_id=job.job_id, fence_epoch=lease.fence_epoch)
    fake = RecordingContainerReaperExecutor([record])
    result = env["queue"].reap_containers(
        actor_user_id=env["owner"],
        workspace_id=env["workspace"],
        repository_id=env["binding"].repository_id,
        min_age_seconds=0,
        dry_run=False,
        container_executor=fake,
    )
    assert result[0]["action"] == "removed"
    assert fake.removed == [record.container_id]


def test_container_reaper_requires_exact_confirmation_for_unknown_job(queue_env) -> None:
    env = queue_env
    record = _managed_container(env, job_id="JOB-UNKNOWN", fence_epoch=9, container_id="c" * 64)
    fake = RecordingContainerReaperExecutor([record])
    preview = env["queue"].reap_containers(
        actor_user_id=env["owner"],
        workspace_id=env["workspace"],
        repository_id=env["binding"].repository_id,
        min_age_seconds=0,
        dry_run=False,
        container_executor=fake,
    )
    assert preview[0]["reason"] == "exact_stale_confirmation_required"
    assert fake.removed == []

    applied = env["queue"].reap_containers(
        actor_user_id=env["owner"],
        workspace_id=env["workspace"],
        repository_id=env["binding"].repository_id,
        min_age_seconds=0,
        dry_run=False,
        expected_stale_job_id="JOB-UNKNOWN",
        expected_fence_epoch=9,
        container_executor=fake,
    )
    assert applied[0]["action"] == "removed"
    assert fake.removed == [record.container_id]


def test_container_reaper_rejects_cross_workspace_labels(queue_env) -> None:
    env = queue_env
    record = _managed_container(env, job_id="JOB-FOREIGN", fence_epoch=3, container_id="d" * 64)
    record.labels["tankai.workspace_id"] = env["foreign_workspace"]
    fake = RecordingContainerReaperExecutor([record])
    result = env["queue"].reap_containers(
        actor_user_id=env["owner"],
        workspace_id=env["workspace"],
        repository_id=env["binding"].repository_id,
        min_age_seconds=0,
        dry_run=False,
        expected_stale_job_id="JOB-FOREIGN",
        expected_fence_epoch=3,
        container_executor=fake,
    )
    assert result[0]["reason"] == "invalid_scope_labels"
    assert fake.removed == []


def test_queue_cli_container_reaper_defaults_to_dry_run(queue_env, capsys) -> None:
    env = queue_env
    runtime = env["tmp"] / "fake-rootless-runtime"
    runtime.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "cmd = sys.argv[1] if len(sys.argv) > 1 else ''\n"
        "if cmd == 'version': print('1.0')\n"
        "elif cmd == 'info': print(json.dumps({'SecurityOptions':['name=rootless'],'OSType':'linux','CgroupVersion':'2'}))\n"
        "elif cmd == 'ps': pass\n"
        "else: raise SystemExit(2)\n",
        encoding="utf-8",
    )
    runtime.chmod(0o755)
    args = [
        "--queue-db", str(env["tmp"] / "queue.db"),
        "--fence-db", str(env["queue"].fence_store.path),
        "--auth-db", str(env["tmp"] / "auth.db"),
        "--repository-base", str(env["tmp"] / "repositories"),
        "--workspace-base", str(env["tmp"] / "worktrees"),
        "--state-base", str(env["tmp"] / "states"),
        "reap-containers",
        "--actor-email", "owner@example.com",
        "--workspace-id", env["workspace"],
        "--repository-id", env["binding"].repository_id,
        "--container-runtime", str(runtime),
        "--min-age-seconds", "0",
    ]
    assert queue_cli_main(args) == 0
    assert json.loads(capsys.readouterr().out) == []
