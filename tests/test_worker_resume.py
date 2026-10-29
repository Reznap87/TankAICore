from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
import subprocess
import sys

import pytest

from test_code_request import SOURCE, FIXED, prepare, request, response_for, source_contract
from tankai.dev_orchestrator import (
    DevelopmentOrchestrator, GitWorkspaceError, ProjectStateStore, TaskState,
    WorkerExecutionError, WorkerPhase, WorkerPipelineRunner, WorkerRunState,
)


class AbruptExit(BaseException):
    """Simulate loss of the process without normal exception persistence."""


def interrupt(contract, stage="test:command:1:before", *, content=FIXED):
    repository, orchestrator, manager, _, _ = contract
    package = request(contract)
    job = prepare(contract, package, response_for(package, content=content))

    def stop(current):
        if current == stage:
            raise AbruptExit(current)

    with pytest.raises(AbruptExit):
        WorkerPipelineRunner(orchestrator, manager, execution_guard=stop).run(job)
    run = next(iter(orchestrator.state().worker_runs.values()))
    restarted = DevelopmentOrchestrator(ProjectStateStore(orchestrator.store.path))
    return job, run, restarted, manager


@pytest.mark.parametrize("stage", [
    "test:command:1:before", "submit_for_review", "begin_independent_gates", "review_gate",
    "security:command:1:before", "qa:command:1:before", "complete_worker_run",
])
def test_resume_bound_code_at_durable_checkpoints_without_reimplementation(source_contract, monkeypatch, stage):
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    job, saved, restarted, manager = interrupt(source_contract, stage)
    stages = []
    runner = WorkerPipelineRunner(restarted, manager, execution_guard=stages.append)
    result = runner.resume(job, run_id=saved.run_id)
    assert result.run.run_id == saved.run_id
    assert result.run.implementation_commit == saved.implementation_commit
    assert result.run.implementation_executions == saved.implementation_executions
    assert result.run.state == WorkerRunState.READY_TO_INTEGRATE
    assert all(item.passed for item in result.run.test_executions + result.run.review_executions
               + result.run.security_executions + result.run.qa_executions)
    assert not any(stage.startswith("implement:") for stage in stages)
    assert len(restarted.state().worker_runs) == 1
    assert (result.workspace.path / SOURCE).read_bytes() == FIXED.encode()
    assert manager.repository_head() == saved.base_commit
    before = restarted.store.path.read_bytes()
    stages.clear()
    assert runner.resume(job, run_id=saved.run_id).run == result.run
    assert restarted.store.path.read_bytes() == before
    assert not any(":command:" in stage for stage in stages)


def test_real_process_exit_and_cli_restart_release_lock_and_keep_same_run(source_contract, monkeypatch, tmp_path):
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    repository, orchestrator, manager, _, _ = source_contract
    package = request(source_contract)
    job = prepare(source_contract, package, response_for(package))
    job_path = tmp_path / "prepared.json"
    job_path.write_text(job.model_dump_json(), encoding="utf-8")
    script = """
import os, sys
from pathlib import Path
from tankai.dev_orchestrator import DevelopmentOrchestrator, ProjectStateStore, GitWorkspaceManager, WorkerPipelineRunner, WorkerPipelineJob
state, repository, worktrees, job = sys.argv[1:]
def stop(stage):
    if stage == 'test:command:1:before':
        os._exit(91)
WorkerPipelineRunner(DevelopmentOrchestrator(ProjectStateStore(state)), GitWorkspaceManager(repository, worktrees), execution_guard=stop).run(WorkerPipelineJob.model_validate_json(Path(job).read_text(encoding='utf-8')))
"""
    cwd = Path(__file__).resolve().parents[1]
    stopped = subprocess.run([sys.executable, "-c", script, str(orchestrator.store.path), str(repository),
                              str(manager.workspace_root), str(job_path)], cwd=cwd,
                             capture_output=True, timeout=30)
    assert stopped.returncode == 91, stopped.stderr.decode(errors="replace")
    saved = next(iter(orchestrator.state().worker_runs.values()))
    args = [sys.executable, "-m", "tankai.dev_orchestrator.cli", "--state", str(orchestrator.store.path),
            "resume-pipeline", "--repository", str(repository), "--workspace-root", str(manager.workspace_root),
            "--job", str(job_path), "--run", saved.run_id]
    resumed = subprocess.run(args, cwd=cwd, capture_output=True, timeout=30)
    assert resumed.returncode == 0, resumed.stdout.decode(errors="replace") + resumed.stderr.decode(errors="replace")
    result = json.loads(resumed.stdout)["run"]
    assert result["run_id"] == saved.run_id
    assert result["state"] == "ready_to_integrate"
    assert result["implementation_commit"] == saved.implementation_commit
    assert manager.repository_head() == saved.base_commit
    before = orchestrator.store.path.read_bytes()
    repeated = subprocess.run(args, cwd=cwd, capture_output=True, timeout=30)
    assert repeated.returncode == 0
    assert orchestrator.store.path.read_bytes() == before


@pytest.mark.parametrize("kind", ["job", "tests", "gates", "task", "scope", "legacy"])
def test_changed_or_legacy_contracts_are_refused_without_mutations(source_contract, kind):
    job, saved, restarted, manager = interrupt(source_contract)
    if kind == "job":
        job.worker.commit_message = "different request"
    elif kind == "tests":
        job.worker.test_commands[0].env["CHANGED"] = "1"
    elif kind == "gates":
        job.gates.qa_commands[0].timeout_seconds += 1
    else:
        def mutate(state):
            if kind == "task":
                state.tasks[saved.task_id].goal = "different goal"
            elif kind == "scope":
                state.agents[saved.agent_id].allowed_paths = ["**"]
            else:
                state.worker_runs[saved.run_id].resume_contract_sha256 = None
        restarted.store.transaction(mutate)
    before = restarted.store.path.read_bytes()
    with pytest.raises(RuntimeError, match="vertrag"):
        WorkerPipelineRunner(restarted, manager).resume(job, run_id=saved.run_id)
    assert restarted.store.path.read_bytes() == before


@pytest.mark.parametrize("kind", ["tracked", "untracked", "head", "journal"])
def test_changed_workspace_or_journal_is_preserved_and_refused(source_contract, kind):
    job, saved, restarted, manager = interrupt(source_contract)
    worktree = Path(saved.workspace_path)
    if kind == "tracked":
        (worktree / SOURCE).write_text("preserve local edits", encoding="utf-8")
    elif kind == "untracked":
        (worktree / "notes.txt").write_text("preserve notes", encoding="utf-8")
    elif kind == "head":
        subprocess.run(["git", "commit", "--allow-empty", "-m", "unexpected commit"],
                       cwd=worktree, check=True, capture_output=True)
    else:
        restarted.store.transaction(lambda state: state.worker_runs[saved.run_id].changed_files.clear())
    before = restarted.store.path.read_bytes()
    with pytest.raises(RuntimeError):
        WorkerPipelineRunner(restarted, manager).resume(job, run_id=saved.run_id)
    assert restarted.store.path.read_bytes() == before
    if kind == "tracked":
        assert (worktree / SOURCE).read_text() == "preserve local edits"
    if kind == "untracked":
        assert (worktree / "notes.txt").read_text() == "preserve notes"


def test_precommit_interruption_cannot_be_guessed_or_replayed(source_contract):
    job, saved, restarted, manager = interrupt(source_contract, "before_implementation_commit")
    before = restarted.store.path.read_bytes()
    assert saved.implementation_commit is None
    runner = WorkerPipelineRunner(restarted, manager)
    with pytest.raises(RuntimeError, match="Implementierungscommit"):
        runner.resume(job, run_id=saved.run_id)
    with pytest.raises(RuntimeError, match="vorhandenen Run"):
        runner.run(job)
    assert restarted.store.path.read_bytes() == before
    assert (Path(saved.workspace_path) / SOURCE).read_bytes() == FIXED.encode()


def test_failing_resumed_test_blocks_the_same_run_before_gates(source_contract):
    wrong = "def authenticate(token):\n    return token == 'wrong'\n"
    job, saved, restarted, manager = interrupt(source_contract, content=wrong)
    with pytest.raises(WorkerExecutionError) as error:
        WorkerPipelineRunner(restarted, manager).resume(job, run_id=saved.run_id)
    assert error.value.run_id == saved.run_id
    run = restarted.state().worker_runs[saved.run_id]
    assert run.state == WorkerRunState.BLOCKED
    assert restarted.state().tasks[saved.task_id].state == TaskState.BLOCKED
    assert not run.review_executions and not run.qa_executions
    with pytest.raises(RuntimeError):
        WorkerPipelineRunner(restarted, manager).resume(job, run_id=saved.run_id)


def test_resume_preserves_container_requirement(source_contract):
    job, saved, restarted, manager = interrupt(source_contract)
    with pytest.raises(RuntimeError, match="Container-Isolation"):
        WorkerPipelineRunner(restarted, manager, require_container_isolation=True).resume(job, run_id=saved.run_id)


def test_execution_lock_rejects_concurrent_process_and_releases_normally(source_contract):
    repository, orchestrator, manager, agent, _ = source_contract
    script = """
import sys
from tankai.dev_orchestrator import GitWorkspaceManager
with GitWorkspaceManager(sys.argv[1], sys.argv[2]).worker_execution_lock(sys.argv[3]):
    pass
"""
    arguments = [sys.executable, "-c", script, str(repository), str(manager.workspace_root), agent]
    with manager.worker_execution_lock(agent):
        result = subprocess.run(arguments, cwd=Path(__file__).resolve().parents[1],
                                capture_output=True, timeout=15)
        assert result.returncode != 0
        assert "bereits ein Worker" in result.stderr.decode("utf-8", errors="replace")
        with pytest.raises(GitWorkspaceError):
            with manager.worker_execution_lock(agent):
                pytest.fail("Concurrent execution was admitted")
    with manager.worker_execution_lock(agent):
        pass


def test_resume_handles_submission_persisted_before_run_status(source_contract, monkeypatch):
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    _, orchestrator, manager, _, _ = source_contract
    original = orchestrator.submit_task_completion

    def stop(*args, **kwargs):
        original(*args, **kwargs)
        raise AbruptExit("submission persisted")

    monkeypatch.setattr(orchestrator, "submit_task_completion", stop)
    package = request(source_contract)
    job = prepare(source_contract, package, response_for(package))
    with pytest.raises(AbruptExit):
        WorkerPipelineRunner(orchestrator, manager).run(job)
    saved = next(iter(orchestrator.state().worker_runs.values()))
    assert saved.state == WorkerRunState.RUNNING
    assert orchestrator.state().tasks[saved.task_id].state == TaskState.REVIEW_PENDING
    restarted = DevelopmentOrchestrator(ProjectStateStore(orchestrator.store.path))
    result = WorkerPipelineRunner(restarted, manager).resume(job, run_id=saved.run_id)
    assert result.run.state == WorkerRunState.READY_TO_INTEGRATE


def test_resume_requires_submitted_implementation_summary_to_match(source_contract, monkeypatch):
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    job, saved, restarted, manager = interrupt(source_contract, "begin_independent_gates")
    restarted.store.transaction(
        lambda state: setattr(
            state.tasks[saved.task_id],
            "implementation_summary",
            "different persisted implementation summary",
        )
    )
    before = restarted.store.path.read_bytes()
    with pytest.raises(RuntimeError, match="Implementierungszusammenfassung"):
        WorkerPipelineRunner(restarted, manager).resume(job, run_id=saved.run_id)
    assert restarted.store.path.read_bytes() == before


@pytest.mark.parametrize("kind", ["phase", "finished_at", "error"])
def test_resume_requires_coherent_terminal_run_receipt(source_contract, monkeypatch, kind):
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    job, saved, restarted, manager = interrupt(source_contract, "complete_worker_run")
    runner = WorkerPipelineRunner(restarted, manager)
    completed = runner.resume(job, run_id=saved.run_id).run

    def mutate(state):
        run = state.worker_runs[completed.run_id]
        if kind == "phase":
            run.phase = WorkerPhase.QA
        elif kind == "finished_at":
            run.finished_at = None
        else:
            run.error = "unexpected terminal error"

    restarted.store.transaction(mutate)
    before = restarted.store.path.read_bytes()
    with pytest.raises(RuntimeError, match="Abschlussbeleg"):
        runner.resume(job, run_id=completed.run_id)
    assert restarted.store.path.read_bytes() == before


@pytest.mark.parametrize("kind", ["run", "execution", "status"])
def test_resume_requires_coherent_terminal_receipt_timeline(
    source_contract, monkeypatch, kind,
):
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    job, saved, restarted, manager = interrupt(source_contract, "complete_worker_run")
    runner = WorkerPipelineRunner(restarted, manager)
    completed = runner.resume(job, run_id=saved.run_id).run

    def mutate(state):
        run = state.worker_runs[completed.run_id]
        if kind == "run":
            run.started_at = run.finished_at + timedelta(seconds=1)
        elif kind == "execution":
            run.implementation_executions[0].executed_at = run.finished_at + timedelta(seconds=1)
        else:
            run.status_messages[-1].created_at = run.finished_at + timedelta(seconds=1)

    restarted.store.transaction(mutate)
    before = restarted.store.path.read_bytes()
    with pytest.raises(RuntimeError, match="Zeitachse"):
        runner.resume(job, run_id=completed.run_id)
    assert restarted.store.path.read_bytes() == before


@pytest.mark.parametrize("kind", ["execution-order", "status-order", "status-sequence"])
def test_resume_requires_ordered_terminal_receipt_journal(
    source_contract, monkeypatch, kind,
):
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    job, saved, restarted, manager = interrupt(source_contract, "complete_worker_run")
    runner = WorkerPipelineRunner(restarted, manager)
    completed = runner.resume(job, run_id=saved.run_id).run

    def mutate(state):
        run = state.worker_runs[completed.run_id]
        if kind == "execution-order":
            run.implementation_executions[-1].executed_at = (
                run.test_executions[0].executed_at + timedelta(microseconds=1)
            )
        elif kind == "status-order":
            run.status_messages[0].created_at = run.status_messages[1].created_at
            run.status_messages[1].created_at = run.started_at
        else:
            run.status_messages[1].sequence = run.status_messages[0].sequence

    restarted.store.transaction(mutate)
    before = restarted.store.path.read_bytes()
    with pytest.raises(RuntimeError, match="Zeitachse|Statusfolge"):
        runner.resume(job, run_id=completed.run_id)
    assert restarted.store.path.read_bytes() == before


@pytest.mark.parametrize("kind", ["phase-regression", "premature-complete"])
def test_resume_requires_coherent_status_phase_journal(
    source_contract, monkeypatch, kind,
):
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    job, saved, restarted, manager = interrupt(source_contract, "complete_worker_run")
    runner = WorkerPipelineRunner(restarted, manager)
    completed = runner.resume(job, run_id=saved.run_id).run

    def mutate(state):
        run = state.worker_runs[completed.run_id]
        if kind == "phase-regression":
            run.status_messages[1].phase = WorkerPhase.COMPLETE
        else:
            run.status_messages[-2].phase = WorkerPhase.COMPLETE

    restarted.store.transaction(mutate)
    before = restarted.store.path.read_bytes()
    with pytest.raises(RuntimeError, match="Phasenfolge"):
        runner.resume(job, run_id=completed.run_id)
    assert restarted.store.path.read_bytes() == before


def test_resume_requires_latest_status_to_match_current_phase(
    source_contract, monkeypatch,
):
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    job, saved, restarted, manager = interrupt(source_contract)
    restarted.store.transaction(
        lambda state: setattr(
            state.worker_runs[saved.run_id], "phase", WorkerPhase.TEST,
        )
    )
    before = restarted.store.path.read_bytes()
    with pytest.raises(RuntimeError, match="Phasenfolge"):
        WorkerPipelineRunner(restarted, manager).resume(job, run_id=saved.run_id)
    assert restarted.store.path.read_bytes() == before


def test_saved_approvals_require_the_matching_command_receipts(source_contract, monkeypatch):
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    job, saved, restarted, manager = interrupt(source_contract, "qa:command:1:before")
    restarted.store.transaction(lambda state: state.worker_runs[saved.run_id].review_executions.clear())
    before = restarted.store.path.read_bytes()
    with pytest.raises(RuntimeError, match="Befehlsbelege"):
        WorkerPipelineRunner(restarted, manager).resume(job, run_id=saved.run_id)
    assert restarted.store.path.read_bytes() == before


def test_resume_rejects_success_receipt_with_nonzero_exit_code(
    source_contract, monkeypatch,
):
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    job, saved, restarted, manager = interrupt(source_contract)
    restarted.store.transaction(
        lambda state: setattr(
            state.worker_runs[saved.run_id].implementation_executions[0],
            "exit_code",
            1,
        )
    )
    before = restarted.store.path.read_bytes()
    with pytest.raises(RuntimeError, match="Befehlsbelege"):
        WorkerPipelineRunner(restarted, manager).resume(job, run_id=saved.run_id)
    assert restarted.store.path.read_bytes() == before


def test_resume_requires_the_original_implementation_receipts(source_contract, monkeypatch):
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    job, saved, restarted, manager = interrupt(source_contract)
    restarted.store.transaction(
        lambda state: state.worker_runs[saved.run_id].implementation_executions.clear()
    )
    before = restarted.store.path.read_bytes()
    with pytest.raises(RuntimeError, match="Befehlsbelege"):
        WorkerPipelineRunner(restarted, manager).resume(job, run_id=saved.run_id)
    assert restarted.store.path.read_bytes() == before


def test_resume_requires_task_and_run_qa_receipts_to_match(source_contract, monkeypatch):
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    job, saved, restarted, manager = interrupt(source_contract, "complete_worker_run")

    def mutate(state):
        state.worker_runs[saved.run_id].qa_executions[0].summary = "different persisted QA evidence"

    restarted.store.transaction(mutate)
    before = restarted.store.path.read_bytes()
    with pytest.raises(RuntimeError, match="QA-Belege"):
        WorkerPipelineRunner(restarted, manager).resume(job, run_id=saved.run_id)
    assert restarted.store.path.read_bytes() == before


@pytest.mark.parametrize("gate", ["review", "security"])
def test_resume_requires_task_and_run_approval_receipts_to_match(
    source_contract, monkeypatch, gate,
):
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    job, saved, restarted, manager = interrupt(source_contract, "qa:command:1:before")

    def mutate(state):
        executions = getattr(state.worker_runs[saved.run_id], f"{gate}_executions")
        executions[0].summary = f"different persisted {gate} evidence"

    restarted.store.transaction(mutate)
    before = restarted.store.path.read_bytes()
    with pytest.raises(RuntimeError, match=f"{gate.capitalize()}-Belege"):
        WorkerPipelineRunner(restarted, manager).resume(job, run_id=saved.run_id)
    assert restarted.store.path.read_bytes() == before


@pytest.mark.parametrize("gate", ["review", "security"])
def test_resume_requires_approval_summary_to_match_its_receipts(
    source_contract, monkeypatch, gate,
):
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    job, saved, restarted, manager = interrupt(source_contract, "qa:command:1:before")

    def mutate(state):
        setattr(state.tasks[saved.task_id], f"{gate}_notes", "different persisted approval summary")

    restarted.store.transaction(mutate)
    before = restarted.store.path.read_bytes()
    with pytest.raises(RuntimeError, match=f"{gate.capitalize()}-Zusammenfassung"):
        WorkerPipelineRunner(restarted, manager).resume(job, run_id=saved.run_id)
    assert restarted.store.path.read_bytes() == before


@pytest.mark.parametrize("kind", ["extra", "reordered"])
def test_saved_approvals_require_exact_ordered_command_receipts(
    source_contract, monkeypatch, kind,
):
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    job, saved, restarted, manager = interrupt(source_contract, "qa:command:1:before")

    def mutate(state):
        receipts = state.worker_runs[saved.run_id].review_executions
        if kind == "extra":
            receipts.append(receipts[-1].model_copy(deep=True))
        else:
            receipts.reverse()

    restarted.store.transaction(mutate)
    before = restarted.store.path.read_bytes()
    with pytest.raises(RuntimeError, match="Befehlsbelege"):
        WorkerPipelineRunner(restarted, manager).resume(job, run_id=saved.run_id)
    assert restarted.store.path.read_bytes() == before


@pytest.mark.parametrize("guarded,orphan", [(False, False), (True, True), (True, False)])
def test_container_resume_requires_fence_and_no_remaining_run_container(source_contract, monkeypatch, guarded, orphan):
    from types import SimpleNamespace
    from test_worker_pipeline import LocalRecordingContainerExecutor
    from tankai.dev_orchestrator import WorkerIsolationSpec

    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    _, orchestrator, manager, _, checks = source_contract
    checks.isolation = WorkerIsolationSpec(image="tankai-worker@sha256:" + "a" * 64, user="1000:1000")
    package = request(source_contract)
    job = prepare(source_contract, package, response_for(package))
    fake = LocalRecordingContainerExecutor(manager)

    def stop(stage):
        if stage == "test:command:1:before":
            raise AbruptExit(stage)

    with pytest.raises(AbruptExit):
        WorkerPipelineRunner(orchestrator, manager, container_executor=fake, execution_guard=stop).run(job)
    saved = next(iter(orchestrator.state().worker_runs.values()))
    fake.list_managed_containers = lambda **kwargs: ([SimpleNamespace(labels={"tankai.run_id": saved.run_id})]
                                                     if orphan else [])
    restarted = DevelopmentOrchestrator(ProjectStateStore(orchestrator.store.path))
    runner = WorkerPipelineRunner(restarted, manager, container_executor=fake,
                                  execution_guard=(lambda stage: None) if guarded else None,
                                  container_metadata={"repository_id": "registered-repo"} if guarded else {})
    before = restarted.store.path.read_bytes()
    previous_calls = len(fake.calls)
    if not guarded or orphan:
        with pytest.raises(RuntimeError, match="Fence|Container"):
            runner.resume(job, run_id=saved.run_id)
        assert restarted.store.path.read_bytes() == before
        assert len(fake.calls) == previous_calls
    else:
        assert runner.resume(job, run_id=saved.run_id).run.state == WorkerRunState.READY_TO_INTEGRATE
        assert all(call["read_only"] for call in fake.calls[previous_calls:])
        assert all(call["image"] == checks.isolation.image for call in fake.calls)


def test_lost_execution_guard_does_not_execute_or_rewrite_run(source_contract):
    job, saved, restarted, manager = interrupt(source_contract)
    before = restarted.store.path.read_bytes()

    def revoked(stage):
        raise RuntimeError("fence revoked")

    with pytest.raises(RuntimeError, match="fence revoked"):
        WorkerPipelineRunner(restarted, manager, execution_guard=revoked).resume(job, run_id=saved.run_id)
    assert restarted.store.path.read_bytes() == before


def test_foreign_repository_cannot_replace_journaled_worktree(source_contract, tmp_path):
    job, saved, restarted, manager = interrupt(source_contract)
    fake = manager.workspace_root / "foreign"
    subprocess.run(["git", "clone", "--no-hardlinks", str(manager.repository), str(fake)],
                   check=True, capture_output=True)
    subprocess.run(["git", "checkout", saved.branch], cwd=fake, check=True, capture_output=True)

    def mutate(state):
        state.worker_runs[saved.run_id].workspace_path = str(fake)
        state.agents[saved.agent_id].workspace_path = str(fake)
    restarted.store.transaction(mutate)
    before = restarted.store.path.read_bytes()
    with pytest.raises(GitWorkspaceError, match="anderen Repository"):
        WorkerPipelineRunner(restarted, manager).resume(job, run_id=saved.run_id)
    assert restarted.store.path.read_bytes() == before
