from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from pydantic import ValidationError

from tankai.dev_orchestrator import (
    AgentSpec, CommandSpec, DevelopmentRole, GateJob, ProjectState, TaskSpec,
    TaskState, WorkerExecutionError, WorkerPipelineJob, WorkerPipelineRunner,
    WorkerRunState, render_command,
)
from tankai.dev_orchestrator.cli import main
from tankai.dev_orchestrator.code_proposal import (
    CodeProposal, ProposedFile, ProposalChecks, load_proposal_json, prepare_code_proposal,
)
from test_worker_pipeline import make_pipeline
from test_job_queue import queue_env, IMAGE, OTHER_IMAGE
from tankai.dev_orchestrator.job_queue import AdmissionDenied
from tankai.dev_orchestrator.models import WorkerIsolationSpec


BASE = "a" * 40


@pytest.fixture
def contract():
    test = CommandSpec(argv=[sys.executable, "-S", "-c", "assert True"])
    task = TaskSpec(task_id="CODE-1", goal="Implement a small helper", base_commit=BASE,
                    allowed_paths=["src/**"], denied_paths=["src/private/**"],
                    acceptance_criteria=["Helper passes its tests"],
                    required_tests=[render_command(test)], assigned_agent_id="CODER",
                    state=TaskState.ACTIVE)
    agent = AgentSpec(agent_id="CODER", role=DevelopmentRole.BACKEND, task_id=task.task_id,
                      base_commit=BASE, allowed_paths=["src/**"],
                      acceptance_criteria=task.acceptance_criteria,
                      required_tests=task.required_tests)
    state = ProjectState(current_version="test", current_branch="main", current_commit=BASE,
                         tasks={task.task_id: task}, agents={agent.agent_id: agent},
                         cycle_agent_ids=[agent.agent_id])
    checks = ProposalChecks(test_commands=[test], gates=GateJob(
        reviewer_agent_id="REVIEW", review_commands=[test],
        qa_agent_id="QA", qa_commands=[test]))
    return state, checks


def proposal(*files: ProposedFile, **updates) -> CodeProposal:
    return CodeProposal(task_id="CODE-1", base_commit=BASE,
                        files=files or (ProposedFile(path="src/helper.py", content="value = 1\n"),),
                        **updates)


def prepare(item, contract):
    state, checks = contract
    return prepare_code_proposal(item, state=state, agent_id="CODER", checks=checks,
                                 python_executable=sys.executable)


def run_receiver(job, path: Path, **env_changes):
    command = job.worker.implementation_commands[0]
    env = {**os.environ, **command.env, **env_changes}
    return subprocess.run(command.argv, cwd=path, env=env, capture_output=True,
                          encoding="utf-8", timeout=15)


@pytest.mark.parametrize("path", [
    "../escape.py", "/absolute.py", "src/../escape.py", "src//helper.py",
    "C:/escape.py", "src\\helper.py", ".git/config", ".GIT/hooks/run",
    "src/CON.py", "src/aux.txt", "src/helper.py.", "src/a:stream", "src/a\n.py",
])
def test_rejects_unsafe_or_aliased_paths(path):
    with pytest.raises(ValidationError):
        ProposedFile(path=path, content="untrusted")


@pytest.mark.parametrize("paths", [
    ("src/a.py", "src/a.py"), ("src/a.py", "src/A.py"), ("src/a", "src/a/b.py"),
])
def test_rejects_duplicate_and_overlapping_files(paths):
    with pytest.raises(ValidationError):
        proposal(*(ProposedFile(path=p, content="x") for p in paths))


def test_compile_preserves_code_checks_and_v1_job_without_writing_state(contract):
    state, checks = contract
    before = state.model_dump_json()
    content = "\n  # Grüße\ndef helper():\n    return 2\n\n"
    candidate = proposal(ProposedFile(path="src/helper.py", content=content))
    job = prepare(candidate, contract)
    assert WorkerPipelineJob.model_validate_json(job.model_dump_json()) == job
    assert job.worker.test_commands == checks.test_commands
    assert job.gates == checks.gates
    assert state.model_dump_json() == before
    assert content not in render_command(job.worker.implementation_commands[0])
    assert candidate.digest() in job.worker.implementation_summary


@pytest.mark.parametrize("path", ["README.md", "src/private/token.py", "src/PRIVATE/token.py"])
def test_rejects_files_outside_task_or_denied_scope(contract, path):
    with pytest.raises(ValueError, match="write scope"):
        prepare(proposal(ProposedFile(path=path, content="x")), contract)


def test_agent_cannot_expand_the_persisted_task_scope(contract):
    state, _ = contract
    state.agents["CODER"].allowed_paths = ["**"]
    with pytest.raises(ValueError, match="write scope"):
        prepare(proposal(ProposedFile(path="other/file.py", content="x")), contract)


@pytest.mark.parametrize("field,value", [("task_id", "OTHER"), ("base_commit", "b" * 40)])
def test_rejects_wrong_task_or_source_revision(contract, field, value):
    candidate = proposal().model_copy(update={field: value})
    with pytest.raises(ValueError, match="source revision"):
        prepare(candidate, contract)


def test_revalidates_direct_constructed_proposals(contract):
    candidate = proposal().model_copy(update={"files": (
        ProposedFile.model_construct(path="../escape", content="x", expected_sha256=None),)})
    with pytest.raises(ValueError, match="Invalid proposal"):
        prepare(candidate, contract)


def test_rejects_inactive_tasks_and_missing_required_tests(contract):
    state, checks = contract
    state.tasks["CODE-1"].state = TaskState.BLOCKED
    with pytest.raises(ValueError, match="must be active"):
        prepare(proposal(), contract)
    state.tasks["CODE-1"].state = TaskState.ACTIVE
    checks.test_commands = [CommandSpec(argv=[sys.executable, "-c", "pass"])]
    with pytest.raises(ValueError, match="required task tests"):
        prepare(proposal(), contract)


def test_bounded_input_rejects_duplicate_json_and_large_payload(tmp_path):
    source = tmp_path / "proposal.json"
    source.write_text('{"files":[{"path":"a","path":"b"}]}', encoding="utf-8")
    with pytest.raises(ValueError, match="Duplicate JSON"):
        load_proposal_json(source)
    source.write_bytes(b" " * 8193)
    with pytest.raises(ValueError, match="size limit"):
        load_proposal_json(source)
    with pytest.raises(ValidationError):
        proposal(ProposedFile(path="src/a.py", content="ü" * 4096))
    with pytest.raises(ValidationError):
        proposal(schema_version=True)


def test_receiver_creates_and_replaces_exact_bytes(contract, tmp_path):
    (tmp_path / "src").mkdir()
    old = b"old without newline"
    (tmp_path / "src/old.py").write_bytes(old)
    content = "\n    # Grüße\nvalue = 2"
    item = proposal(ProposedFile(path="src/old.py", expected_sha256=hashlib.sha256(old).hexdigest(),
                                 content=content),
                    ProposedFile(path="src/new.py", content="new\n"))
    result = run_receiver(prepare(item, contract), tmp_path)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "src/old.py").read_bytes() == content.encode("utf-8")
    assert (tmp_path / "src/new.py").read_bytes() == b"new\n"
    assert json.loads(result.stdout) == {"proposal_sha256": item.digest(), "files_written": 2}
    assert content not in result.stdout


def test_all_preimages_are_checked_before_any_write(contract, tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/old.py").write_bytes(b"preserve me")
    item = proposal(ProposedFile(path="src/new.py", content="new"),
                    ProposedFile(path="src/old.py", expected_sha256="0" * 64, content="wrong"))
    result = run_receiver(prepare(item, contract), tmp_path)
    assert result.returncode != 0
    assert not (tmp_path / "src/new.py").exists()
    assert (tmp_path / "src/old.py").read_bytes() == b"preserve me"


def test_missing_preimage_and_create_overwrite_are_refused(contract, tmp_path):
    item = proposal(ProposedFile(path="src/absent.py", expected_sha256="0" * 64, content="x"))
    assert run_receiver(prepare(item, contract), tmp_path).returncode != 0
    (tmp_path / "src").mkdir()
    (tmp_path / "src/helper.py").write_bytes(b"keep")
    assert run_receiver(prepare(proposal(), contract), tmp_path).returncode != 0
    assert (tmp_path / "src/helper.py").read_bytes() == b"keep"


def test_payload_tampering_is_rejected_before_writes(contract, tmp_path):
    result = run_receiver(prepare(proposal(), contract), tmp_path,
                          TANKAI_CODE_PROPOSAL_B64="e30=")
    assert result.returncode != 0
    assert not (tmp_path / "src").exists()


def test_hardlinked_targets_are_not_modified(contract, tmp_path):
    (tmp_path / "src").mkdir()
    outside = tmp_path / "preserve.txt"
    outside.write_bytes(b"original")
    os.link(outside, tmp_path / "src/helper.py")
    item = proposal(ProposedFile(path="src/helper.py",
                                 expected_sha256=hashlib.sha256(b"original").hexdigest(), content="bad"))
    assert run_receiver(prepare(item, contract), tmp_path).returncode != 0
    assert outside.read_bytes() == b"original"


def test_parent_symlinks_are_refused(contract, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    try:
        (work / "src").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Symlink creation unavailable")
    assert run_receiver(prepare(proposal(), contract), work).returncode != 0
    assert not (outside / "helper.py").exists()


def prepare_real_job(tmp_path, content):
    repository, orchestrator, manager, template = make_pipeline(tmp_path, security=True)
    task = orchestrator.state().tasks["AUTH-001"]
    item = CodeProposal(task_id=task.task_id, base_commit=task.base_commit,
                        files=(ProposedFile(path="backend/src/auth/service.py", content=content),))
    checks = ProposalChecks(test_commands=template.worker.test_commands, gates=template.gates)
    job = prepare_code_proposal(item, state=orchestrator.state(), agent_id=template.worker.agent_id,
                                checks=checks, python_executable=sys.executable)
    return repository, orchestrator, manager, item, checks, job


@pytest.mark.parametrize("valid", [True, False])
def test_proposal_runs_through_existing_worker_and_test_gates(tmp_path, valid):
    content = "def authenticate(token):\n    return " + ("token == 'ok'" if valid else "False") + "\n"
    repository, orchestrator, manager, item, _, job = prepare_real_job(tmp_path, content)
    original_head = manager.repository_head()
    if valid:
        result = WorkerPipelineRunner(orchestrator, manager).run(job)
        assert result.run.state == WorkerRunState.READY_TO_INTEGRATE
        assert result.run.changed_files == ["backend/src/auth/service.py"]
        assert all(x.passed for x in result.run.test_executions + result.run.review_executions
                   + result.run.qa_executions + result.run.security_executions)
        receipt = json.loads(result.run.implementation_executions[0].summary)
        assert receipt["proposal_sha256"] == item.digest()
    else:
        with pytest.raises(WorkerExecutionError) as error:
            WorkerPipelineRunner(orchestrator, manager).run(job)
        record = orchestrator.state().worker_runs[error.value.run_id]
        assert record.state == WorkerRunState.BLOCKED
        assert not record.review_executions and not record.qa_executions
    assert manager.repository_head() == original_head
    assert not (repository / "backend/src/auth/service.py").exists()


def test_cli_exports_only_prepared_job_and_preserves_existing_output(tmp_path, capsys):
    _, orchestrator, _, item, checks, job = prepare_real_job(
        tmp_path, "def authenticate(token):\n    return token == 'ok'\n")
    source, check_path, output = (tmp_path / name for name in ("proposal.json", "checks.json", "job.json"))
    source.write_text(item.model_dump_json(), encoding="utf-8")
    check_path.write_text(checks.model_dump_json(), encoding="utf-8")
    state_path = tmp_path / "state.json"
    before = state_path.read_bytes()
    args = ["--state", str(state_path), "prepare-code-proposal", "--agent", job.worker.agent_id,
            "--proposal", str(source), "--checks", str(check_path), "--output", str(output),
            "--python-executable", sys.executable]
    assert main(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["prepared"] and not result["submitted"] and not result["executed"]
    assert WorkerPipelineJob.model_validate_json(output.read_text(encoding="utf-8")) == job
    assert state_path.read_bytes() == before
    assert not orchestrator.state().worker_runs
    preserved = output.read_bytes()
    assert main(args) == 2
    assert output.read_bytes() == preserved


def test_schema_discovery_needs_no_state_file(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["code-proposal-schema"]) == 0
    schema = json.loads(capsys.readouterr().out)
    assert schema["additionalProperties"] is False
    assert "files" in schema["properties"]
    assert "implementation_commands" not in schema["properties"]
    assert not (tmp_path / ".tankai").exists()


def test_prepared_jobs_obey_existing_queue_admission_and_replay(queue_env, contract):
    state, checks = contract
    for command in [*checks.test_commands, *checks.gates.review_commands, *checks.gates.qa_commands]:
        command.timeout_seconds = 10
    checks.isolation = WorkerIsolationSpec(image=IMAGE, memory_mb=256, pids_limit=64, user="1000:1000")
    job = prepare(proposal(), contract)
    env = queue_env
    arguments = dict(actor_user_id=env["owner"], workspace_id=env["workspace"],
                     repository_id=env["binding"].repository_id, idempotency_key="proposal-1")
    first = env["queue"].enqueue(pipeline=job, **arguments)
    repeated = env["queue"].enqueue(pipeline=job, **arguments)
    assert first.job_id == repeated.job_id
    denied = job.model_copy(update={"isolation": checks.isolation.model_copy(update={"image": OTHER_IMAGE})})
    with pytest.raises(AdmissionDenied, match="Image"):
        env["queue"].enqueue(pipeline=denied, **{**arguments, "idempotency_key": "bad-image"})
    with pytest.raises(AdmissionDenied, match="Isolation"):
        env["queue"].enqueue(pipeline=job.model_copy(update={"isolation": None}),
                             **{**arguments, "idempotency_key": "no-isolation"})
