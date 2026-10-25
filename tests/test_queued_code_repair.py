from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from test_job_queue import IMAGE, OTHER_IMAGE, queue_env
from test_worker_pipeline import LocalRecordingContainerExecutor, support_agent
from tankai.dev_orchestrator import (
    CommandSpec, DevelopmentOrchestrator, DevelopmentRole, GateJob, GitWorkspaceManager,
    TaskSpec, WorkerIsolationSpec,
)
from tankai.dev_orchestrator.code_proposal import CodeProposal, ProposedFile, ProposalChecks
from tankai.dev_orchestrator.code_request import CodeResponse, build_code_request, prepare_code_response
from tankai.dev_orchestrator.code_repair import (
    CodeRepairSubmission, build_code_repair_request, authorize_code_repair,
)
from tankai.dev_orchestrator.job_queue import (
    AdmissionDenied, DevelopmentJobQueue, JobState, QueueError, QueuedWorkerDispatcher,
)
from tankai.dev_orchestrator.queue_cli import main as queue_cli_main

SOURCE = "src/value.py"
ORIGINAL = "def value():\n    return 0\n"
WRONG = "def value():\n    return 2\n"
FIXED = "def value():\n    return 1\n"


class AbruptExit(BaseException):
    pass


@pytest.fixture
def queued_case(queue_env, monkeypatch):
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    env = queue_env
    binding = env["binding"]
    repo = Path(binding.repository_path)
    (repo / "src").mkdir()
    (repo / SOURCE).write_bytes(ORIGINAL.encode())
    subprocess.run(["git", "config", "core.autocrlf", "false"], cwd=repo, check=True)
    subprocess.run(["git", "add", "--all"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Source for queued corrections"], cwd=repo, check=True, capture_output=True)
    base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    orchestrator = DevelopmentOrchestrator.initialize(
        binding.state_path, current_version="queue-correction-test", current_branch="main", current_commit=base,
    )
    check = CommandSpec(argv=[sys.executable, "-B", "-c", "from src.value import value; assert value() == 1"],
                        timeout_seconds=5)
    from tankai.dev_orchestrator import render_command
    orchestrator.create_task(TaskSpec(task_id="VALUE-1", goal="Return the integer one", base_commit=base,
                                     allowed_paths=["src/**"], required_tests=[render_command(check)],
                                     acceptance_criteria=["The fixed test passes"]))
    agent = orchestrator.start_agent("VALUE-1", DevelopmentRole.BACKEND)
    checks = ProposalChecks(test_commands=[check], gates=GateJob(
        reviewer_agent_id=support_agent(orchestrator, base, "REVIEW", DevelopmentRole.REVIEWER),
        review_commands=[check], qa_agent_id=support_agent(orchestrator, base, "QA", DevelopmentRole.QA),
        qa_commands=[check]), isolation=WorkerIsolationSpec(image=IMAGE, memory_mb=256, pids_limit=64, user="1000:1000"))
    package = build_code_request(state=orchestrator.state(), agent_id=agent.agent_id, repository=repo,
                                 context_paths=[SOURCE], checks=checks)
    reply = CodeResponse(request_sha256=package.request_sha256, proposal=CodeProposal(
        task_id="VALUE-1", base_commit=base, files=(ProposedFile(path=SOURCE, content=WRONG,
        expected_sha256=package.request.context_files[0].sha256),)))
    previous = prepare_code_response(package, reply, state=orchestrator.state(), agent_id=agent.agent_id,
                                     repository=repo, checks=checks, python_executable=sys.executable)
    manager = GitWorkspaceManager(repo, binding.workspace_root)
    fake = LocalRecordingContainerExecutor(manager)
    fake.list_managed_containers = lambda **kwargs: []
    first = env["queue"].enqueue(actor_user_id=env["owner"], workspace_id=env["workspace"],
        repository_id=binding.repository_id, pipeline=previous, idempotency_key="initial-code")
    result = QueuedWorkerDispatcher(env["queue"], worker_id="initial-worker", container_executor=fake).run_once()
    assert result.state == JobState.FAILED
    parent = orchestrator.state().tasks["VALUE-1"].worker_run_id
    correction = build_code_repair_request(state=orchestrator.state(), previous_job=previous, run_id=parent,
        repository=repo, editable_paths=[SOURCE], context_paths=[SOURCE])
    response = CodeResponse(request_sha256=correction.request_sha256, proposal=CodeProposal(
        task_id="VALUE-1", base_commit=base, files=(ProposedFile(path=SOURCE, content=FIXED,
        expected_sha256=correction.request.context_files[0].sha256),)))
    env.update(orchestrator=orchestrator, manager=manager, executor=fake, parent=parent, initial=first,
               submission=CodeRepairSubmission(package=correction, response=response, previous_job=previous,
                                                python_executable=sys.executable))
    return env


def submit(env, **changes):
    arguments = dict(actor_user_id=env["owner"], workspace_id=env["workspace"],
                     repository_id=env["binding"].repository_id, submission=env["submission"])
    arguments.update(changes)
    return env["queue"].enqueue_code_repair_with_outcome(**arguments)


def dispatcher(env, **kwargs):
    return QueuedWorkerDispatcher(env["queue"], worker_id="correction-worker", container_executor=env["executor"], **kwargs)


def expire(env, job_id):
    job = env["queue"].get_job(actor_user_id=env["owner"], workspace_id=env["workspace"], job_id=job_id)
    env["queue"].fence_store.force_expire_for_recovery(env["binding"].repository_id, expected_epoch=job.fence_epoch)
    with sqlite3.connect(env["queue"].path) as conn:
        conn.execute("UPDATE development_jobs SET lease_expires_at=? WHERE id=?",
                     ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), job_id))


def test_queued_correction_activates_only_after_lease_and_preserves_checks(queued_case):
    env = queued_case
    orchestrator = env["orchestrator"]
    before = orchestrator.store.path.read_bytes()
    first = submit(env)
    assert first.job.state == JobState.QUEUED and not first.idempotent_replay
    assert first.job.code_repair == env["submission"]
    assert orchestrator.store.path.read_bytes() == before
    assert submit(env).job.job_id == first.job.job_id
    result = dispatcher(env).run_once()
    assert result.state == JobState.SUCCEEDED
    run = orchestrator.state().worker_runs[result.result["run"]["run_id"]]
    assert run.code_repair.queue_job_id == first.job.job_id and run.code_repair.attempt == 1
    assert orchestrator.state().worker_runs[env["parent"]].state.value == "blocked"
    assert orchestrator.state().tasks["VALUE-1"].code_repair_attempts == 1
    assert (Path(env["binding"].repository_path) / SOURCE).read_bytes() == ORIGINAL.encode()
    assert (Path(run.workspace_path) / SOURCE).read_bytes() == FIXED.encode()
    calls = [call for call in env["executor"].calls if call["metadata_labels"].get("job_id") == first.job.job_id]
    assert calls and all(call["image"] == IMAGE for call in calls)
    assert all(call["metadata_labels"]["repository_id"] == env["binding"].repository_id for call in calls)
    assert sum(call["phase"] == "implement" for call in calls) == 1
    final = orchestrator.store.path.read_bytes()
    repeated = submit(env)
    assert repeated.idempotent_replay and repeated.job.state == JobState.SUCCEEDED
    assert orchestrator.store.path.read_bytes() == final


def test_identical_submission_from_second_admin_does_not_duplicate_job(queued_case):
    env = queued_case
    first = submit(env)
    env["auth"].add_user_to_workspace(user_id=env["member"], workspace_id=env["workspace"], role="admin")
    repeated = submit(env, actor_user_id=env["member"])
    assert repeated.idempotent_replay and repeated.job.job_id == first.job.job_id


@pytest.mark.parametrize("kind", ["member", "foreign", "image", "different_response"])
def test_permission_policy_and_conflicting_corrections_fail_without_reservation(queued_case, kind):
    env = queued_case
    before = env["orchestrator"].store.path.read_bytes()
    changes = {}
    if kind == "member":
        changes["actor_user_id"] = env["member"]
    elif kind == "foreign":
        changes.update(actor_user_id=env["foreign"], workspace_id=env["foreign_workspace"])
    elif kind == "image":
        policy = env["queue"].get_policy(env["workspace"])
        policy.allowed_images = [OTHER_IMAGE]
        env["queue"].set_policy(actor_user_id=env["owner"], workspace_id=env["workspace"], policy=policy)
    else:
        submit(env)
        data = env["submission"].model_dump(mode="json")
        data["response"]["proposal"]["files"][0]["content"] = FIXED + "# alternative\n"
        changes["submission"] = CodeRepairSubmission.model_validate(data)
    with pytest.raises((PermissionError, AdmissionDenied)):
        submit(env, **changes)
    assert env["orchestrator"].store.path.read_bytes() == before


def test_revoked_admin_role_is_rechecked_before_activation(queued_case):
    env = queued_case
    submit(env)
    before = env["orchestrator"].store.path.read_bytes()
    env["auth"].add_user_to_workspace(user_id=env["owner"], workspace_id=env["workspace"], role="member")
    result = dispatcher(env).run_once()
    assert result.state == JobState.FAILED
    assert env["orchestrator"].store.path.read_bytes() == before


@pytest.mark.parametrize("checkpoint", ["reservation", "commit", "ready"])
def test_lease_recovery_keeps_same_attempt_and_never_replays_implementation(queued_case, monkeypatch, checkpoint):
    env = queued_case
    item = submit(env).job
    calls_before = len(env["executor"].calls)
    if checkpoint == "reservation":
        import tankai.dev_orchestrator.job_queue as module
        original = module.authorize_code_repair
        def stop(*args, **kwargs):
            original(*args, **kwargs)
            raise AbruptExit("after reservation")
        monkeypatch.setattr(module, "authorize_code_repair", stop)
    elif checkpoint == "commit":
        original_execute = env["executor"].execute
        def stop(*args, **kwargs):
            result = original_execute(*args, **kwargs)
            if kwargs["phase"] == "test":
                raise AbruptExit("after commit")
            return result
        monkeypatch.setattr(env["executor"], "execute", stop)
    else:
        def stop(**kwargs):
            raise AbruptExit("before queue completion")
        monkeypatch.setattr(env["queue"], "complete_job", stop)
    with pytest.raises(AbruptExit):
        dispatcher(env).run_once()
    task = env["orchestrator"].state().tasks["VALUE-1"]
    assert task.code_repair_attempts == 1
    run_id = task.worker_run_id
    monkeypatch.undo()
    expire(env, item.job_id)
    old = env["queue"]
    env["queue"] = DevelopmentJobQueue(old.path, auth_store=env["auth"],
        repository_base=old.repository_base, workspace_base=old.workspace_base, state_base=old.state_base,
        fence_path=old.fence_store.path)
    result = dispatcher(env).run_once()
    assert result.state == JobState.SUCCEEDED
    if run_id:
        assert result.result["run"]["run_id"] == run_id
    state = env["orchestrator"].state()
    assert state.tasks["VALUE-1"].code_repair_attempts == 1
    assert len(state.worker_runs) == 2
    assert sum(call["phase"] == "implement" for call in env["executor"].calls[calls_before:]) == 1


def test_orphan_container_and_custom_executor_cannot_bypass_controls(queued_case):
    env = queued_case
    submit(env)
    before = env["orchestrator"].store.path.read_bytes()
    env["executor"].list_managed_containers = lambda **kwargs: [SimpleNamespace(labels={"tankai.run_id": env["parent"]})]
    result = dispatcher(env).run_once()
    assert result.state == JobState.FAILED and "Container" in result.error
    assert env["orchestrator"].store.path.read_bytes() == before


def test_custom_callback_cannot_execute_a_correction(queued_case):
    env = queued_case
    submit(env)
    before = env["orchestrator"].store.path.read_bytes()
    invoked = []
    result = dispatcher(env, execute_pipeline=lambda *args: invoked.append(True)).run_once()
    assert result.state == JobState.FAILED and not invoked
    assert env["orchestrator"].store.path.read_bytes() == before


def test_lost_fence_rolls_back_attempt_reservation(queued_case, monkeypatch):
    env = queued_case
    item = submit(env).job
    before = env["orchestrator"].store.path.read_bytes()
    original = DevelopmentOrchestrator.reserve_code_repair

    def revoke(self, reservation, **kwargs):
        assert kwargs.get("execution_guard") is not None
        status = env["queue"].fence_store.current(env["binding"].repository_id)
        assert status is not None and status.active
        env["queue"].fence_store.force_expire_for_recovery(env["binding"].repository_id, expected_epoch=status.epoch)
        return original(self, reservation, **kwargs)

    monkeypatch.setattr(DevelopmentOrchestrator, "reserve_code_repair", revoke)
    with pytest.raises(QueueError):
        dispatcher(env).run_once()
    assert env["orchestrator"].store.path.read_bytes() == before
    assert env["orchestrator"].state().tasks["VALUE-1"].code_repair_attempts == 0


def test_early_interruption_is_not_replayed_or_reported_successful(queued_case, monkeypatch):
    env = queued_case
    item = submit(env).job
    previous_calls = len(env["executor"].calls)
    original = env["executor"].execute

    def stop(*args, **kwargs):
        result = original(*args, **kwargs)
        if kwargs["phase"] == "implement":
            raise AbruptExit("before durable commit")
        return result

    monkeypatch.setattr(env["executor"], "execute", stop)
    with pytest.raises(AbruptExit):
        dispatcher(env).run_once()
    monkeypatch.undo()
    expire(env, item.job_id)
    result = dispatcher(env).run_once()
    assert result.state == JobState.FAILED
    task = env["orchestrator"].state().tasks["VALUE-1"]
    assert task.code_repair_attempts == 1
    assert env["orchestrator"].state().worker_runs[task.worker_run_id].implementation_commit is None
    assert sum(call["phase"] == "implement" for call in env["executor"].calls[previous_calls:]) == 1


def test_queue_bound_reservation_cannot_be_used_by_local_runner(queued_case, monkeypatch):
    import tankai.dev_orchestrator.job_queue as module
    from tankai.dev_orchestrator import WorkerPipelineRunner

    env = queued_case
    item = submit(env).job
    authorize = module.authorize_code_repair

    def stop(*args, **kwargs):
        authorize(*args, **kwargs)
        raise AbruptExit("reserved")

    monkeypatch.setattr(module, "authorize_code_repair", stop)
    with pytest.raises(AbruptExit):
        dispatcher(env).run_once()
    before = env["orchestrator"].store.path.read_bytes()
    with pytest.raises(RuntimeError, match="Queue-Job"):
        WorkerPipelineRunner(env["orchestrator"], env["manager"], container_executor=env["executor"]).run(item.pipeline)
    assert env["orchestrator"].store.path.read_bytes() == before


def test_tampered_queue_context_is_rejected_before_claim(queued_case):
    env = queued_case
    job = submit(env).job
    before = env["orchestrator"].store.path.read_bytes()
    with sqlite3.connect(env["queue"].path) as conn:
        raw = json.loads(conn.execute("SELECT payload_json FROM development_jobs WHERE id=?", (job.job_id,)).fetchone()[0])
        raw["code_repair"]["python_executable"] = "different-python"
        conn.execute("UPDATE development_jobs SET payload_json=? WHERE id=?", (json.dumps(raw), job.job_id))
    assert dispatcher(env).run_once() is None
    assert env["orchestrator"].store.path.read_bytes() == before


def test_queue_cli_enqueues_without_activating_project(queued_case, tmp_path, capsys):
    env = queued_case
    sub = env["submission"]
    for name, model in [("request", sub.package), ("response", sub.response), ("previous", sub.previous_job)]:
        (tmp_path / f"{name}.json").write_text(model.model_dump_json(), encoding="utf-8")
    before = env["orchestrator"].store.path.read_bytes()
    args = ["--queue-db", str(env["queue"].path), "--auth-db", str(env["auth"].path),
            "--repository-base", str(env["queue"].repository_base), "--workspace-base", str(env["queue"].workspace_base),
            "--state-base", str(env["queue"].state_base), "enqueue-code-repair", "--actor-email", "owner@example.com",
            "--workspace-id", env["workspace"], "--repository-id", env["binding"].repository_id,
            "--previous-job", str(tmp_path / "previous.json"), "--request", str(tmp_path / "request.json"),
            "--response", str(tmp_path / "response.json"), "--python-executable", sys.executable]
    assert queue_cli_main(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["job"]["state"] == "queued" and not result["idempotent_replay"]
    assert env["orchestrator"].store.path.read_bytes() == before


def test_concurrent_admin_connections_share_one_immutable_queue_job(queued_case):
    from concurrent.futures import ThreadPoolExecutor

    env = queued_case
    env["auth"].add_user_to_workspace(user_id=env["member"], workspace_id=env["workspace"], role="admin")
    old = env["queue"]
    connections = [DevelopmentJobQueue(old.path, auth_store=env["auth"],
        repository_base=old.repository_base, workspace_base=old.workspace_base, state_base=old.state_base,
        fence_path=old.fence_store.path) for _ in range(2)]
    def enqueue(arguments):
        connection, actor = arguments
        return connection.enqueue_code_repair_with_outcome(actor_user_id=actor,
            workspace_id=env["workspace"], repository_id=env["binding"].repository_id, submission=env["submission"])
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(enqueue, zip(connections, [env["owner"], env["member"]])))
    assert len({result.job.job_id for result in results}) == 1
    assert sum(result.idempotent_replay for result in results) == 1
    assert env["orchestrator"].state().tasks["VALUE-1"].code_repair_attempts == 0


def test_previous_job_inline_credentials_are_not_stored_in_queue_context(queued_case):
    env = queued_case
    data = env["submission"].model_dump(mode="json")
    data["previous_job"]["worker"]["implementation_commands"][0]["env"]["EXAMPLE_API_KEY"] = "EXAMPLE_ONLY"
    with pytest.raises(AdmissionDenied, match="Inline-Secrets"):
        submit(env, submission=CodeRepairSubmission.model_validate(data))
    assert env["orchestrator"].state().tasks["VALUE-1"].code_repair_attempts == 0
