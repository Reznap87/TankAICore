from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from test_job_queue import IMAGE, OTHER_IMAGE, queue_env
from test_worker_pipeline import LocalRecordingContainerExecutor, support_agent
from tankai.dev_orchestrator import (
    CommandSpec, DevelopmentOrchestrator, DevelopmentRole, GateJob, GitWorkspaceManager,
    TaskSpec, WorkerIsolationSpec,
)
from tankai.dev_orchestrator.code_proposal import CodeProposal, ProposedFile, ProposalChecks
from tankai.dev_orchestrator.code_request import CodeResponse, CodeResponseSubmission, build_code_request
from tankai.dev_orchestrator.job_queue import AdmissionDenied, DevelopmentJobQueue, JobState, QueuedWorkerDispatcher
from tankai.dev_orchestrator.queue_cli import main

SOURCE = "src/value.py"
ORIGINAL = "def value():\n    return 0\n"
FIXED = "def value():\n    return 1\n"


@pytest.fixture
def code_case(queue_env, monkeypatch):
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    env = queue_env
    binding = env["binding"]
    repo = Path(binding.repository_path)
    (repo / "src").mkdir()
    (repo / SOURCE).write_bytes(ORIGINAL.encode())
    for args in [("config", "core.autocrlf", "false"), ("add", "--all"), ("commit", "-m", "Code source")]:
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    orchestrator = DevelopmentOrchestrator.initialize(binding.state_path,
        current_version="code-response-test", current_branch="main", current_commit=base)
    check = CommandSpec(argv=[sys.executable, "-B", "-c", "from src.value import value; assert value() == 1"],
                        timeout_seconds=5)
    from tankai.dev_orchestrator import render_command
    orchestrator.create_task(TaskSpec(task_id="VALUE-1", goal="Return the integer one", base_commit=base,
        allowed_paths=["src/**"], required_tests=[render_command(check)], acceptance_criteria=["The fixed test passes"]))
    agent = orchestrator.start_agent("VALUE-1", DevelopmentRole.BACKEND)
    checks = ProposalChecks(test_commands=[check], gates=GateJob(
        reviewer_agent_id=support_agent(orchestrator, base, "REVIEW", DevelopmentRole.REVIEWER),
        review_commands=[check], qa_agent_id=support_agent(orchestrator, base, "QA", DevelopmentRole.QA),
        qa_commands=[check]), isolation=WorkerIsolationSpec(image=IMAGE, memory_mb=256, pids_limit=64, user="1000:1000"))
    package = build_code_request(state=orchestrator.state(), agent_id=agent.agent_id, repository=repo,
                                 context_paths=[SOURCE], checks=checks)
    response = CodeResponse(request_sha256=package.request_sha256, proposal=CodeProposal(
        task_id="VALUE-1", base_commit=base, files=(ProposedFile(path=SOURCE, content=FIXED,
        expected_sha256=package.request.context_files[0].sha256),)))
    manager = GitWorkspaceManager(repo, binding.workspace_root)
    env.update(orchestrator=orchestrator, manager=manager, agent=agent.agent_id,
        executor=LocalRecordingContainerExecutor(manager),
        submission=CodeResponseSubmission(package=package, response=response, checks=checks,
                                          python_executable=sys.executable))
    return env


def submit(env, **changes):
    args = dict(actor_user_id=env["owner"], workspace_id=env["workspace"],
                repository_id=env["binding"].repository_id, submission=env["submission"])
    args.update(changes)
    return env["queue"].enqueue_code_response_with_outcome(**args)


def dispatch(env, **changes):
    return QueuedWorkerDispatcher(env["queue"], worker_id="code-response-worker",
                                  container_executor=env["executor"], **changes).run_once()


def change_state(env, field, value):
    def change(state):
        if field == "current_commit":
            state.current_commit = value
        else:
            setattr(state.tasks["VALUE-1"], field, value)
    env["orchestrator"].store.transaction(change)


def test_bound_code_response_survives_queue_reload_and_runs_with_unchanged_checks(code_case):
    env = code_case
    before = env["orchestrator"].store.path.read_bytes()
    item = submit(env)
    assert item.job.code_response == env["submission"] and item.job.code_repair is None
    assert env["orchestrator"].store.path.read_bytes() == before
    assert not list(Path(env["binding"].workspace_root).glob("**/src/value.py"))
    old = env["queue"]
    env["queue"] = DevelopmentJobQueue(old.path, auth_store=env["auth"], repository_base=old.repository_base,
        workspace_base=old.workspace_base, state_base=old.state_base, fence_path=old.fence_store.path)
    result = dispatch(env)
    assert result.state == JobState.SUCCEEDED
    run = result.result["run"]
    assert (Path(run["workspace_path"]) / SOURCE).read_bytes() == FIXED.encode()
    assert (Path(env["binding"].repository_path) / SOURCE).read_bytes() == ORIGINAL.encode()
    assert [call["phase"] for call in env["executor"].calls] == ["implement", "test", "review", "qa"]
    assert all(call["metadata_labels"]["job_id"] == item.job.job_id for call in env["executor"].calls)
    final = env["orchestrator"].store.path.read_bytes()
    replay = submit(env)
    assert replay.idempotent_replay and replay.job.job_id == item.job.job_id
    assert env["orchestrator"].store.path.read_bytes() == final
    assert dispatch(env) is None


@pytest.mark.parametrize("kind", ["member", "foreign", "image", "no_isolation", "checks", "different_reply"])
def test_untrusted_actor_checks_and_conflicting_reply_are_rejected(code_case, kind):
    env = code_case
    before = env["orchestrator"].store.path.read_bytes()
    args = {}
    data = env["submission"].model_dump(mode="json")
    if kind == "member":
        args["actor_user_id"] = env["member"]
    elif kind == "foreign":
        args.update(actor_user_id=env["foreign"], workspace_id=env["foreign_workspace"])
    elif kind == "different_reply":
        submit(env)
        data["response"]["proposal"]["files"][0]["content"] += "# conflicting reply\n"
    elif kind == "image":
        policy = env["queue"].get_policy(env["workspace"])
        policy.allowed_images = [OTHER_IMAGE]
        env["queue"].set_policy(actor_user_id=env["owner"], workspace_id=env["workspace"], policy=policy)
    elif kind == "no_isolation":
        data["checks"]["isolation"] = None
    else:
        data["checks"]["gates"]["review_commands"][0]["argv"] = ["python", "-c", "pass"]
    args["submission"] = CodeResponseSubmission.model_validate(data)
    with pytest.raises((ValueError, PermissionError, AdmissionDenied)):
        submit(env, **args)
    assert env["orchestrator"].store.path.read_bytes() == before
    assert not env["executor"].calls


@pytest.mark.parametrize("field,value", [("goal", "Changed goal"), ("current_commit", "f" * 40),
                                          ("worker_run_id", "ALREADY-STARTED")])
def test_stale_or_started_request_is_rejected_before_enqueue(code_case, field, value):
    env = code_case
    change_state(env, field, value)
    with pytest.raises(ValueError):
        submit(env)
    assert env["queue"].list_jobs(actor_user_id=env["owner"], workspace_id=env["workspace"]) == []


@pytest.mark.parametrize("field,value", [("goal", "Changed after enqueue"), ("current_commit", "f" * 40),
                                          ("worker_run_id", "ALREADY-STARTED")])
def test_source_contract_is_rechecked_under_worker_lease(code_case, field, value):
    env = code_case
    submit(env)
    change_state(env, field, value)
    before = env["orchestrator"].store.path.read_bytes()
    result = dispatch(env)
    assert result.state == JobState.FAILED
    assert env["orchestrator"].store.path.read_bytes() == before
    assert not env["executor"].calls


@pytest.mark.parametrize("kind", ["role", "policy", "callback"])
def test_worker_rechecks_admin_policy_and_refuses_custom_callback(code_case, kind):
    env = code_case
    item = submit(env)
    kwargs = {}
    if kind == "role":
        env["auth"].add_user_to_workspace(user_id=env["owner"], workspace_id=env["workspace"], role="member")
    elif kind == "policy":
        policy = env["queue"].get_policy(env["workspace"])
        policy.allowed_images = [OTHER_IMAGE]
        env["queue"].set_policy(actor_user_id=env["owner"], workspace_id=env["workspace"], policy=policy)
    else:
        kwargs["execute_pipeline"] = lambda *args: pytest.fail("Uncontrolled runner invoked")
    before = env["orchestrator"].store.path.read_bytes()
    result = dispatch(env, **kwargs)
    # Admission can refuse a revoked image before leasing any job.
    if kind == "policy":
        assert result is None
    else:
        assert result.state == JobState.FAILED
    assert env["orchestrator"].store.path.read_bytes() == before
    assert not env["executor"].calls


def test_contract_change_during_workspace_binding_blocks_before_any_command(code_case, monkeypatch):
    env = code_case
    submit(env)
    original = DevelopmentOrchestrator.bind_workspace
    def bind(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        change_state(env, "goal", "Changed while binding workspace")
        return result
    monkeypatch.setattr(DevelopmentOrchestrator, "bind_workspace", bind)
    assert dispatch(env).state == JobState.FAILED
    assert not env["executor"].calls
    assert not env["orchestrator"].state().worker_runs


def test_concurrent_admins_share_one_immutable_initial_job(code_case):
    env = code_case
    env["auth"].add_user_to_workspace(user_id=env["member"], workspace_id=env["workspace"], role="admin")
    old = env["queue"]
    connections = [DevelopmentJobQueue(old.path, auth_store=env["auth"], repository_base=old.repository_base,
        workspace_base=old.workspace_base, state_base=old.state_base, fence_path=old.fence_store.path) for _ in range(2)]
    def enqueue(pair):
        connection, actor = pair
        return connection.enqueue_code_response_with_outcome(actor_user_id=actor, workspace_id=env["workspace"],
            repository_id=env["binding"].repository_id, submission=env["submission"])
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(enqueue, zip(connections, [env["owner"], env["member"]])))
    assert len({result.job.job_id for result in results}) == 1
    assert sum(result.idempotent_replay for result in results) == 1


def test_queue_checksum_covers_request_and_operator_checks(code_case):
    env = code_case
    job = submit(env).job
    with sqlite3.connect(env["queue"].path) as conn:
        raw = json.loads(conn.execute("SELECT payload_json FROM development_jobs WHERE id=?", (job.job_id,)).fetchone()[0])
        raw["code_response"]["checks"]["gates"]["review_commands"][0]["timeout_seconds"] += 1
        conn.execute("UPDATE development_jobs SET payload_json=? WHERE id=?", (json.dumps(raw), job.job_id))
    before = env["orchestrator"].store.path.read_bytes()
    assert dispatch(env) is None
    assert env["orchestrator"].store.path.read_bytes() == before and not env["executor"].calls


def test_cli_enqueues_bound_response_and_reports_only_identifiers(code_case, tmp_path, capsys):
    env = code_case
    sub = env["submission"]
    for name, model in [("request", sub.package), ("response", sub.response), ("checks", sub.checks)]:
        (tmp_path / f"{name}.json").write_text(model.model_dump_json(), encoding="utf-8")
    args = ["--queue-db", str(env["queue"].path), "--auth-db", str(env["auth"].path),
        "--repository-base", str(env["queue"].repository_base), "--workspace-base", str(env["queue"].workspace_base),
        "--state-base", str(env["queue"].state_base), "enqueue-code-response", "--actor-email", "owner@example.com",
        "--workspace-id", env["workspace"], "--repository-id", env["binding"].repository_id,
        "--request", str(tmp_path / "request.json"), "--response", str(tmp_path / "response.json"),
        "--checks", str(tmp_path / "checks.json"), "--python-executable", sys.executable]
    before = env["orchestrator"].store.path.read_bytes()
    assert main(args) == 0
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload["submitted"] and not payload["executed"] and not payload["model_called"]
    assert not payload["idempotent_replay"] and FIXED not in output and SOURCE not in output
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)["idempotent_replay"]
    assert env["orchestrator"].store.path.read_bytes() == before
