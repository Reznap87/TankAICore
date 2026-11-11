from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from test_code_request import SOURCE, FIXED, prepare, request, response_for, source_contract
from tankai.dev_orchestrator import WorkerExecutionError, WorkerPipelineRunner, WorkerRunState
from tankai.dev_orchestrator.cli import main
from tankai.dev_orchestrator.code_repair import (
    authorize_code_repair, build_code_repair_request, prepare_code_repair_response,
)

WRONG = "def authenticate(token):\n    return token == 'wrong'\n"


@pytest.fixture
def failed_case(source_contract):
    _, orchestrator, manager, _, _ = source_contract
    original = request(source_contract)
    previous = prepare(source_contract, original, response_for(original, content=WRONG))
    with pytest.raises(WorkerExecutionError) as error:
        WorkerPipelineRunner(orchestrator, manager).run(previous)
    saved = orchestrator.state().worker_runs[error.value.run_id]
    assert saved.state == WorkerRunState.BLOCKED
    return source_contract, previous, saved.run_id


def build(case, **changes):
    contract, previous, run_id = case
    repository, orchestrator, _, _, _ = contract
    arguments = dict(state=orchestrator.state(), previous_job=previous, run_id=run_id,
                     repository=repository, editable_paths=[SOURCE], context_paths=[SOURCE])
    arguments.update(changes)
    return build_code_repair_request(**arguments)


def prepare_repair(case, package, response):
    contract, previous, _ = case
    repository, orchestrator, _, _, _ = contract
    return prepare_code_repair_response(package, response, state=orchestrator.state(),
                                        previous_job=previous, repository=repository,
                                        python_executable=sys.executable)


def authorize(case, package, response):
    contract, previous, _ = case
    _, orchestrator, manager, _, _ = contract
    return authorize_code_repair(package, response, orchestrator=orchestrator, previous_job=previous,
                                  workspace_manager=manager, python_executable=sys.executable)


def test_failed_test_can_be_corrected_with_same_checks_and_preserved_failure(failed_case):
    contract, previous, parent_id = failed_case
    repository, orchestrator, manager, _, _ = contract
    parent_before = orchestrator.state().worker_runs[parent_id].model_dump_json()
    before = orchestrator.store.path.read_bytes()
    package = build(failed_case)
    assert package.request.context_commit == orchestrator.state().worker_runs[parent_id].implementation_commit
    assert package.request.context_files[0].content == WRONG
    assert package.request.attempt == 1
    reply = response_for(package)
    job = prepare_repair(failed_case, package, reply)
    assert job.worker.test_commands == previous.worker.test_commands
    assert job.gates == previous.gates and job.isolation == previous.isolation
    assert orchestrator.store.path.read_bytes() == before
    receipt = authorize(failed_case, package, reply)
    assert not receipt.replay
    pending = orchestrator.state().tasks[package.request.task_id]
    assert pending.code_repair_attempts == 1 and pending.pending_code_repair == receipt.reservation
    reserved_bytes = orchestrator.store.path.read_bytes()
    assert authorize(failed_case, package, reply).replay
    assert orchestrator.store.path.read_bytes() == reserved_bytes
    result = WorkerPipelineRunner(orchestrator, manager).run(job)
    assert result.run.state == WorkerRunState.READY_TO_INTEGRATE
    assert result.run.run_id != parent_id and result.run.code_repair == receipt.reservation
    assert orchestrator.state().worker_runs[parent_id].model_dump_json() == parent_before
    assert len(orchestrator.state().worker_runs) == 2
    assert orchestrator.state().tasks[package.request.task_id].pending_code_repair is None
    assert (result.workspace.path / SOURCE).read_bytes() == FIXED.encode()
    assert manager.repository_head() == package.request.base_commit
    assert (repository / SOURCE).read_bytes() != FIXED.encode()
    final = orchestrator.store.path.read_bytes()
    replay = authorize(failed_case, package, reply)
    assert replay.replay and replay.started_run_id == result.run.run_id
    assert orchestrator.store.path.read_bytes() == final


def test_two_failed_repairs_exhaust_persistent_limit(failed_case):
    contract, previous, parent = failed_case
    _, orchestrator, manager, _, _ = contract
    for attempt in (1, 2):
        case = contract, previous, parent
        package = build(case)
        assert package.request.attempt == attempt
        wrong = f"def authenticate(token):\n    return token == 'wrong-{attempt}'\n"
        reply = response_for(package, content=wrong)
        job = prepare_repair(case, package, reply)
        authorize(case, package, reply)
        with pytest.raises(WorkerExecutionError) as error:
            WorkerPipelineRunner(orchestrator, manager).run(job)
        parent = error.value.run_id
        previous = job
        assert orchestrator.state().worker_runs[parent].code_repair.attempt == attempt
    before = orchestrator.store.path.read_bytes()
    with pytest.raises(ValueError, match="limit"):
        build((contract, previous, parent))
    assert orchestrator.store.path.read_bytes() == before
    assert len(orchestrator.state().worker_runs) == 3


def test_failure_feedback_contains_identifiers_not_raw_test_output(failed_case):
    contract, _, run_id = failed_case
    _, orchestrator, _, _, _ = contract
    output = "SENSITIVE_EXAMPLE_ONLY: test emitted private material"
    orchestrator.store.transaction(lambda state: setattr(state.worker_runs[run_id].test_executions[-1],
                                                         "summary", output))
    package = build(failed_case)
    failure = package.request.failed_tests[0]
    assert failure.output_sha256 == hashlib.sha256(output.encode()).hexdigest()
    assert failure.output_bytes == len(output.encode())
    assert failure.exit_code != 0 and failure.test_index == 1
    assert output not in package.model_dump_json()


@pytest.mark.parametrize("path", ["backend/src/auth/other.py", "tests/test_auth.py", "../escape", SOURCE.upper()])
def test_edit_scope_cannot_expand_beyond_selected_changed_files(failed_case, path):
    with pytest.raises(ValueError):
        build(failed_case, editable_paths=[path])


@pytest.mark.parametrize("kind", ["wrong_request", "preimage", "outside", "new_file", "tampered_feedback"])
def test_bad_responses_or_forged_requests_do_not_reopen_task(failed_case, kind):
    package = build(failed_case)
    if kind == "tampered_feedback":
        altered = package.request.model_copy(update={"failed_tests": ()})
        package = package.model_copy(update={"request": altered, "request_sha256": altered.digest()})
    reply = response_for(package)
    if kind == "wrong_request":
        reply = reply.model_copy(update={"request_sha256": "0" * 64})
    elif kind == "preimage":
        reply = response_for(package, expected="0" * 64)
    elif kind == "outside":
        reply = response_for(package, path="backend/src/auth/other.py")
    elif kind == "new_file":
        reply = response_for(package, path="backend/src/auth/new.py", expected=None)
    orchestrator = failed_case[0][1]
    before = orchestrator.store.path.read_bytes()
    with pytest.raises(ValueError):
        authorize(failed_case, package, reply)
    assert orchestrator.store.path.read_bytes() == before


@pytest.mark.parametrize("kind", ["goal", "checks", "legacy", "integrity", "current_source"])
def test_stale_contract_or_non_test_failure_is_refused(failed_case, kind):
    contract, previous, run_id = failed_case
    _, orchestrator, _, _, _ = contract
    if kind == "checks":
        previous.worker.test_commands[0].env["ALTERED"] = "1"
    else:
        def mutate(state):
            run = state.worker_runs[run_id]
            if kind == "goal":
                state.tasks[run.task_id].goal = "changed goal"
            elif kind == "legacy":
                run.resume_contract_sha256 = None
            elif kind == "integrity":
                run.test_executions[-1].command = "[worker-test-integrity]"
            else:
                state.current_commit = "b" * 40
        orchestrator.store.transaction(mutate)
    before = orchestrator.store.path.read_bytes()
    with pytest.raises(ValueError):
        build(failed_case)
    assert orchestrator.store.path.read_bytes() == before


@pytest.mark.parametrize("kind", ["job", "contract", "head", "current_source"])
def test_reserved_repair_rejects_changes_before_execution(failed_case, kind):
    package = build(failed_case)
    reply = response_for(package)
    job = prepare_repair(failed_case, package, reply)
    authorize(failed_case, package, reply)
    _, orchestrator, manager, _, _ = failed_case[0]
    if kind == "job":
        job.worker.test_commands[0].timeout_seconds += 1
    elif kind == "contract":
        orchestrator.store.transaction(lambda state: setattr(state.tasks[package.request.task_id], "goal", "changed"))
    elif kind == "current_source":
        orchestrator.store.transaction(lambda state: setattr(state, "current_commit", "b" * 40))
    else:
        worktree = Path(orchestrator.state().agents[package.request.agent_id].workspace_path)
        subprocess.run(["git", "commit", "--allow-empty", "-m", "unexpected"], cwd=worktree,
                       capture_output=True, check=True)
    before = orchestrator.store.path.read_bytes()
    with pytest.raises(RuntimeError, match="Korrektur"):
        WorkerPipelineRunner(orchestrator, manager).run(job)
    assert orchestrator.store.path.read_bytes() == before
    assert orchestrator.state().tasks[package.request.task_id].pending_code_repair is not None


def test_dirty_worktree_is_preserved_and_no_attempt_is_reserved(failed_case):
    package = build(failed_case)
    reply = response_for(package)
    _, orchestrator, _, _, _ = failed_case[0]
    worktree = Path(orchestrator.state().agents[package.request.agent_id].workspace_path)
    note = worktree / "notes.txt"
    note.write_text("preserve work", encoding="utf-8")
    before = orchestrator.store.path.read_bytes()
    with pytest.raises(RuntimeError):
        authorize(failed_case, package, reply)
    assert orchestrator.store.path.read_bytes() == before
    assert note.read_text() == "preserve work"


def test_cli_preparation_and_authorization_are_separate(failed_case, tmp_path, capsys):
    contract, previous, run_id = failed_case
    repository, orchestrator, manager, _, _ = contract
    previous_path = tmp_path / "previous.json"
    previous_path.write_text(previous.model_dump_json(), encoding="utf-8")
    package_path, response_path, job_path = (tmp_path / name for name in ("repair.json", "response.json", "job.json"))
    common = ["--state", str(orchestrator.store.path)]
    inputs = ["--repository", str(repository), "--previous-job", str(previous_path)]
    before = orchestrator.store.path.read_bytes()
    assert main(common + ["prepare-code-repair-request", *inputs, "--run", run_id,
                          "--edit-file", SOURCE, "--context-file", SOURCE, "--output", str(package_path)]) == 0
    assert not json.loads(capsys.readouterr().out)["executed"]
    package = build(failed_case)
    response_path.write_text(response_for(package).model_dump_json(), encoding="utf-8")
    bound = [*inputs, "--request", str(package_path), "--response", str(response_path),
             "--python-executable", sys.executable]
    args = common + ["prepare-code-repair-response", *bound, "--output", str(job_path)]
    assert main(args) == 0
    capsys.readouterr()
    assert orchestrator.store.path.read_bytes() == before
    preserved = job_path.read_bytes()
    assert main(args) == 2
    assert job_path.read_bytes() == preserved
    capsys.readouterr()
    assert main(common + ["authorize-code-repair", *bound, "--workspace-root", str(manager.workspace_root)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["authorized"] and not result["submitted"] and not result["executed"]
    assert orchestrator.state().tasks[package.request.task_id].code_repair_attempts == 1


def test_repair_schema_discovery_needs_no_state(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["code-repair-request-schema"]) == 0
    assert "request" in json.loads(capsys.readouterr().out)["properties"]
    assert not (tmp_path / ".tankai").exists()


def test_repair_retains_complete_cumulative_committed_file_manifest(source_contract):
    from tankai.dev_orchestrator.code_proposal import ProposedFile

    _, orchestrator, manager, _, _ = source_contract
    original = request(source_contract)
    candidate = response_for(original, content=WRONG)
    candidate = candidate.model_copy(update={"proposal": candidate.proposal.model_copy(update={"files": (
        *candidate.proposal.files, ProposedFile(path="backend/src/auth/helper.py", content="VALUE = 1\n"),
    )})})
    previous = prepare(source_contract, original, candidate)
    with pytest.raises(WorkerExecutionError) as error:
        WorkerPipelineRunner(orchestrator, manager).run(previous)
    case = source_contract, previous, error.value.run_id
    package = build(case)
    reply = response_for(package)
    job = prepare_repair(case, package, reply)
    authorize(case, package, reply)
    result = WorkerPipelineRunner(orchestrator, manager).run(job)
    assert result.run.changed_files == ["backend/src/auth/helper.py", SOURCE]
    assert orchestrator.state().tasks[package.request.task_id].changed_files == result.run.changed_files
    assert (result.workspace.path / "backend/src/auth/helper.py").read_text() == "VALUE = 1\n"


def test_revision_race_does_not_partially_reopen_or_consume_attempt(failed_case, monkeypatch):
    package = build(failed_case)
    reply = response_for(package)
    _, orchestrator, _, agent, _ = failed_case[0]
    reserve = orchestrator.reserve_code_repair

    def raced(reservation, *, expected_revision):
        orchestrator.append_worklog(agent, "Independent status update")
        return reserve(reservation, expected_revision=expected_revision)

    monkeypatch.setattr(orchestrator, "reserve_code_repair", raced)
    with pytest.raises(RuntimeError, match="verändert"):
        authorize(failed_case, package, reply)
    task = orchestrator.state().tasks[package.request.task_id]
    assert task.state.value == "blocked" and task.code_repair_attempts == 0
    assert task.pending_code_repair is None


def test_local_repair_cannot_activate_container_job_or_downgrade_checks(source_contract):
    from test_worker_pipeline import LocalRecordingContainerExecutor
    from tankai.dev_orchestrator import WorkerIsolationSpec

    _, orchestrator, manager, _, checks = source_contract
    checks.isolation = WorkerIsolationSpec(image="tankai-worker@sha256:" + "a" * 64, user="1000:1000")
    original = request(source_contract)
    previous = prepare(source_contract, original, response_for(original, content=WRONG))
    fake = LocalRecordingContainerExecutor(manager)
    with pytest.raises(WorkerExecutionError) as error:
        WorkerPipelineRunner(orchestrator, manager, container_executor=fake).run(previous)
    case = source_contract, previous, error.value.run_id
    package = build(case)
    reply = response_for(package)
    job = prepare_repair(case, package, reply)
    assert job.isolation == previous.isolation
    before = orchestrator.store.path.read_bytes()
    with pytest.raises(ValueError, match="queue/fence"):
        authorize(case, package, reply)
    assert orchestrator.store.path.read_bytes() == before


@pytest.mark.parametrize("kind", ["unknown_task", "missing_reviewer"])
def test_missing_contract_entities_return_bounded_errors(failed_case, kind):
    package = build(failed_case)
    if kind == "unknown_task":
        forged = package.request.model_copy(update={"task_id": "UNKNOWN"})
        package = package.model_copy(update={"request": forged, "request_sha256": forged.digest()})
    else:
        _, orchestrator, _, _, _ = failed_case[0]
        reviewer = failed_case[1].gates.reviewer_agent_id
        def remove(state):
            state.agents.pop(reviewer)
            state.cycle_agent_ids.remove(reviewer)
            state.tasks.pop("REVIEW-POOL")
        orchestrator.store.transaction(remove)
    reply = response_for(package)
    orchestrator = failed_case[0][1]
    before = orchestrator.store.path.read_bytes()
    with pytest.raises(ValueError, match="unavailable|contract"):
        authorize(failed_case, package, reply)
    assert orchestrator.store.path.read_bytes() == before
