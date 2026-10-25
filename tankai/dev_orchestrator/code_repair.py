"""Bounded local correction handoffs; no inference client or automatic execution."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .code_proposal import ProposalChecks, ProposedFile, prepare_code_proposal
from .code_request import (
    CodeRequest, CodeResponse, _GitSnapshot, _Versioned, _request_for_contract,
)
from .git_workspace import GitWorkspaceManager
from .models import (
    AgentSpec, AgentStatus, CodeRepairReservation, MAX_CODE_REPAIR_ATTEMPTS,
    ProjectState, TaskSpec, TaskState, WorkerPipelineJob, WorkerRunRecord, WorkerRunState,
)
from .orchestrator import DevelopmentOrchestrator
from .worker import _resume_contract, pipeline_digest, render_command


class FailedCodeTest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    test_index: int = Field(ge=1, le=32, strict=True)
    command: str = Field(min_length=1, max_length=4000)
    exit_code: int = Field(strict=True)
    output_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    output_bytes: int = Field(ge=0, le=40_000, strict=True)


class CodeRepairRequest(CodeRequest):
    purpose: Literal["repair"] = "repair"
    parent_run_id: str = Field(min_length=1, max_length=160)
    context_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    attempt: int = Field(ge=1, le=MAX_CODE_REPAIR_ATTEMPTS, strict=True)
    max_attempts: Literal[2] = MAX_CODE_REPAIR_ATTEMPTS
    editable_paths: tuple[str, ...] = Field(min_length=1, max_length=8)
    failed_tests: tuple[FailedCodeTest, ...] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def bounded_edit_scope(self) -> "CodeRepairRequest":
        paths = tuple(ProposedFile.safe_path(path) for path in self.editable_paths)
        if (len(paths) != len({path.casefold() for path in paths})
                or not set(paths).issubset({item.path for item in self.context_files})):
            raise ValueError("Repair files need unique, fully shared source context")
        return self


class CodeRepairRequestPackage(_Versioned):
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    request: CodeRepairRequest

    @model_validator(mode="after")
    def valid_binding(self) -> "CodeRepairRequestPackage":
        if self.request_sha256 != self.request.digest():
            raise ValueError("Code repair request digest mismatch")
        return self


class CodeRepairAuthorization(_Versioned):
    reservation: CodeRepairReservation
    replay: bool = False
    started_run_id: str | None = None


def _failed_contract(
    state: ProjectState, previous_job: WorkerPipelineJob, run_id: str,
) -> tuple[TaskSpec, AgentSpec, WorkerRunRecord, ProposalChecks]:
    run = state.worker_runs.get(run_id)
    if run is None or run.agent_id != previous_job.worker.agent_id:
        raise ValueError("Repair needs the matching failed worker run")
    task = state.tasks.get(run.task_id)
    agent = state.agents.get(run.agent_id)
    if (task is None or agent is None or task.assigned_agent_id != agent.agent_id
            or agent.task_id != task.task_id or task.worker_run_id != run_id
            or task.state != TaskState.BLOCKED or agent.status != AgentStatus.BLOCKED
            or run.state != WorkerRunState.BLOCKED or not run.implementation_commit
            or task.pending_code_repair is not None):
        raise ValueError("Repair needs the current blocked task and committed worker run")
    if not (run.base_commit == task.base_commit == agent.base_commit == state.current_commit):
        raise ValueError("Repair cannot change or rebase the confirmed source revision")
    try:
        fingerprint = _resume_contract(state, previous_job)
    except KeyError as exc:
        raise ValueError("Previous job or task/agent contract changed") from exc
    if not run.resume_contract_sha256 or run.resume_contract_sha256 != fingerprint:
        raise ValueError("Previous job or task/agent contract changed")
    previous_attempt = run.code_repair.attempt if run.code_repair else 0
    if task.code_repair_attempts != previous_attempt or previous_attempt >= MAX_CODE_REPAIR_ATTEMPTS:
        raise ValueError("Code repair attempt limit reached or journal differs")
    implementations = [render_command(command) for command in previous_job.worker.implementation_commands]
    if (not run.implementation_executions or not all(item.passed for item in run.implementation_executions)
            or [item.command for item in run.implementation_executions] != implementations
            or run.review_executions or run.qa_executions or run.security_executions):
        raise ValueError("Only committed implementation followed by a worker-test failure is repairable")
    tests = run.test_executions
    expected = [render_command(command) for command in previous_job.worker.test_commands]
    if (not 1 <= len(tests) <= 32 or len(tests) > len(expected)
            or [item.command for item in tests] != expected[:len(tests)]
            or any(not item.passed for item in tests[:-1]) or tests[-1].passed
            or tests[-1].exit_code is None or tests[-1].exit_code == 0):
        raise ValueError("Repair needs a recorded failing test, not a runtime or integrity failure")
    checks = ProposalChecks(test_commands=previous_job.worker.test_commands,
                            gates=previous_job.gates, isolation=previous_job.isolation)
    return task, agent, run, checks


def build_code_repair_request(
    *, state: ProjectState, previous_job: WorkerPipelineJob, run_id: str,
    repository: str | Path, editable_paths: Sequence[str], context_paths: Sequence[str],
    data_class: Literal["public", "internal"] = "internal",
) -> CodeRepairRequestPackage:
    previous_job = WorkerPipelineJob.model_validate(previous_job.model_dump(mode="json"))
    task, agent, run, checks = _failed_contract(state, previous_job, run_id)
    paths = tuple(ProposedFile.safe_path(path) for path in editable_paths)
    if not paths or not set(paths).issubset(run.changed_files):
        raise ValueError("Repair can edit only explicitly selected previously changed files")
    snapshot = _GitSnapshot(repository, run.implementation_commit or "")
    snapshot.run("merge-base", "--is-ancestor", run.base_commit, run.implementation_commit or "")
    base = _request_for_contract(
        task=task, agent=agent, repository=repository, context_paths=context_paths, checks=checks,
        data_class=data_class, context_commit=run.implementation_commit,
    )
    failures = tuple(FailedCodeTest(
        test_index=index, command=item.command, exit_code=item.exit_code,
        output_sha256=hashlib.sha256(item.summary.encode("utf-8")).hexdigest(),
        output_bytes=len(item.summary.encode("utf-8")),
    ) for index, item in enumerate(run.test_executions, start=1) if not item.passed)
    request = CodeRepairRequest(
        **base.request.model_dump(mode="json"), parent_run_id=run_id,
        context_commit=run.implementation_commit, attempt=task.code_repair_attempts + 1,
        editable_paths=tuple(sorted(paths)), failed_tests=failures,
    )
    return CodeRepairRequestPackage(request_sha256=request.digest(), request=request)


def prepare_code_repair_response(
    package: CodeRepairRequestPackage, response: CodeResponse, *, state: ProjectState,
    previous_job: WorkerPipelineJob, repository: str | Path, python_executable: str = "python3",
) -> WorkerPipelineJob:
    package = CodeRepairRequestPackage.model_validate(package.model_dump(mode="json"))
    response = CodeResponse.model_validate(response.model_dump(mode="json"))
    request = package.request
    current = build_code_repair_request(
        state=state, previous_job=previous_job, run_id=request.parent_run_id, repository=repository,
        editable_paths=request.editable_paths, context_paths=[item.path for item in request.context_files],
        data_class=request.data_class,
    )
    if current != package or response.request_sha256 != package.request_sha256:
        raise ValueError("Repair response belongs to a stale or different request")
    context = {item.path: item for item in request.context_files}
    for change in response.proposal.files:
        if (change.path not in request.editable_paths
                or change.expected_sha256 != context[change.path].sha256):
            raise ValueError("Repair response changed its exact file scope or source preimage")
    _, _, _, checks = _failed_contract(state, previous_job, request.parent_run_id)
    active = state.model_copy(deep=True)
    active.tasks[request.task_id].state = TaskState.ACTIVE
    active.agents[request.agent_id].status = AgentStatus.ACTIVE
    job = prepare_code_proposal(response.proposal, state=active, agent_id=request.agent_id,
                                checks=checks, python_executable=python_executable)
    job.worker.implementation_summary = (
        f"Code repair request {package.request_sha256}; proposal {response.proposal.digest()}; "
        f"parent {request.parent_run_id}; attempt {request.attempt}"
    )
    job.worker.commit_message = f"Repair task {request.task_id} (attempt {request.attempt})"
    return job


def authorize_code_repair(
    package: CodeRepairRequestPackage, response: CodeResponse, *,
    orchestrator: DevelopmentOrchestrator, previous_job: WorkerPipelineJob,
    workspace_manager: GitWorkspaceManager, python_executable: str = "python3",
) -> CodeRepairAuthorization:
    """Reserve local state only; queue submission/execution remain separate actions."""
    package = CodeRepairRequestPackage.model_validate(package.model_dump(mode="json"))
    response = CodeResponse.model_validate(response.model_dump(mode="json"))
    if response.request_sha256 != package.request_sha256:
        raise ValueError("Repair response belongs to another request")
    request = package.request
    with workspace_manager.worker_execution_lock(request.agent_id):
        state = orchestrator.state()
        for record in state.worker_runs.values():
            repair = record.code_repair
            if repair and repair.request_sha256 == package.request_sha256 and repair.proposal_sha256 == response.proposal.digest():
                return CodeRepairAuthorization(reservation=repair, replay=True, started_run_id=record.run_id)
        task = state.tasks.get(request.task_id)
        if task is None:
            raise ValueError("Repair task is unavailable")
        pending = task.pending_code_repair
        view = state
        if pending:
            if (pending.request_sha256 != package.request_sha256
                    or pending.proposal_sha256 != response.proposal.digest() or task.state != TaskState.ACTIVE
                    or task.code_repair_attempts != pending.attempt):
                raise ValueError("Another repair is already reserved")
            view = state.model_copy(deep=True)
            view.tasks[task.task_id].pending_code_repair = None
            view.tasks[task.task_id].code_repair_attempts = pending.attempt - 1
            view.tasks[task.task_id].state = TaskState.BLOCKED
            view.tasks[task.task_id].worker_run_id = pending.parent_run_id
            view.agents[request.agent_id].status = AgentStatus.BLOCKED
        job = prepare_code_repair_response(
            package, response, state=view, previous_job=previous_job,
            repository=workspace_manager.repository, python_executable=python_executable,
        )
        if job.isolation is not None:
            raise ValueError("Container repair activation needs queue/fence integration; local activation is unavailable")
        parent = state.worker_runs[request.parent_run_id]
        workspace = workspace_manager.workspace_from_binding(
            agent_id=parent.agent_id, branch=parent.branch, workspace_path=parent.workspace_path,
            base_commit=parent.base_commit,
        )
        agent = state.agents[request.agent_id]
        if parent.branch != agent.branch or parent.workspace_path != agent.workspace_path:
            raise ValueError("Repair workspace binding changed")
        workspace_manager.assert_head(workspace, request.context_commit)
        workspace_manager.assert_clean(workspace)
        changed = workspace_manager.validate_committed_changes(
            agent, workspace, base_commit=parent.base_commit, head_commit=request.context_commit,
        )
        if sorted(changed) != sorted(parent.changed_files):
            raise ValueError("Repair source and saved file journal differ")
        reservation = CodeRepairReservation(
            parent_run_id=parent.run_id, attempt=request.attempt, context_commit=request.context_commit,
            request_sha256=package.request_sha256, proposal_sha256=response.proposal.digest(),
            job_sha256=pipeline_digest(job), contract_sha256=_resume_contract(state, job),
        )
        if pending:
            if pending != reservation:
                raise ValueError("Reserved repair contract changed")
            return CodeRepairAuthorization(reservation=pending, replay=True)
        orchestrator.reserve_code_repair(reservation, expected_revision=state.revision)
        return CodeRepairAuthorization(reservation=reservation)
