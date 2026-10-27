"""Real, isolated command worker with persistent review/QA/security gates.

The runner never uses a shell. It executes explicit argv arrays in an agent's
Git worktree, validates all changed paths, commits the implementation and only
marks the run ready after independent gate agent identities have approved it.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import shlex
import uuid
from dataclasses import dataclass
from typing import Callable

from .container_runtime import ContainerRuntimeError, DockerCommandExecutor
from .git_workspace import GitWorkspaceError, GitWorkspaceManager, Workspace
from .models import (
    AgentSpec,
    CommandSpec,
    DevelopmentRole,
    GateJob,
    ProjectState,
    QAStatus,
    ReviewDecision,
    TaskState,
    TestExecution,
    WorkerExecutionBackend,
    WorkerIsolationSpec,
    WorkerJob,
    WorkerPhase,
    WorkerPipelineJob,
    WorkerRunRecord,
    WorkerRunState,
)
from .orchestrator import (
    DevelopmentOrchestrator,
    OrchestrationError,
    TransitionError,
    ValidationError,
)


class WorkerExecutionError(RuntimeError):
    """Raised after the failure was persisted in the authoritative state."""

    def __init__(self, message: str, *, run_id: str | None = None) -> None:
        super().__init__(message)
        self.run_id = run_id


def render_command(command: CommandSpec) -> str:
    return shlex.join(command.argv)


def _resume_contract(state: ProjectState, job: WorkerPipelineJob) -> str:
    agent = state.agents[job.worker.agent_id]
    task = state.tasks[agent.task_id]
    task_fields = {"task_id", "goal", "base_commit", "assigned_agent_id", "allowed_paths",
                   "denied_paths", "required_tests", "acceptance_criteria", "requires_security_review",
                   "dependencies", "capability_id", "capability_action", "affected_components"}
    agent_fields = {"agent_id", "task_id", "base_commit", "role", "allowed_paths", "denied_paths",
                    "read_access", "required_tests", "acceptance_criteria", "contract_version", "cycle_id"}
    identities = [agent.agent_id, job.gates.reviewer_agent_id, job.gates.qa_agent_id]
    if job.gates.security_agent_id:
        identities.append(job.gates.security_agent_id)
    contract = {"job": job.model_dump(mode="json"),
                "task": task.model_dump(mode="json", include=task_fields),
                "agents": {identity: state.agents[identity].model_dump(mode="json", include=agent_fields)
                           for identity in identities}}
    raw = json.dumps(contract, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def pipeline_digest(job: WorkerPipelineJob) -> str:
    raw = json.dumps(job.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class WorkerPipelineResult:
    run: WorkerRunRecord
    workspace: Workspace


class WorkerPipelineRunner:
    def __init__(
        self,
        orchestrator: DevelopmentOrchestrator,
        workspace_manager: GitWorkspaceManager,
        *,
        container_executor: DockerCommandExecutor | None = None,
        require_container_isolation: bool = False,
        execution_guard: Callable[[str], None] | None = None,
        container_metadata: dict[str, str] | None = None,
        start_validator: Callable[[ProjectState], None] | None = None,
    ) -> None:
        self.orchestrator = orchestrator
        self.workspace_manager = workspace_manager
        self.container_executor = container_executor
        self.require_container_isolation = require_container_isolation
        self.execution_guard = execution_guard
        self.container_metadata = dict(container_metadata or {})
        self.start_validator = start_validator

    def run(self, job: WorkerPipelineJob) -> WorkerPipelineResult:
        job = WorkerPipelineJob.model_validate(job.model_dump(mode="json"))
        with self.workspace_manager.worker_execution_lock(job.worker.agent_id):
            return self._run(job)

    def _run(self, job: WorkerPipelineJob) -> WorkerPipelineResult:
        self._validate_job(job.worker, job.gates, job.isolation)
        self._guard("prepare_workspace")
        state = self.orchestrator.state()
        agent = state.agents[job.worker.agent_id]
        task = state.tasks[agent.task_id]
        pending = task.pending_code_repair
        if pending and pending.queue_job_id and (
                self.execution_guard is None or self.container_metadata.get("job_id") != pending.queue_job_id):
            raise ValidationError("Korrekturfreigabe gehört zu einem anderen Queue-Job")
        if pending and (pending.job_sha256 != pipeline_digest(job)
                        or pending.contract_sha256 != _resume_contract(state, job)
                        or state.current_commit != task.base_commit
                        or task.code_repair_attempts != pending.attempt):
            raise ValidationError("Korrekturfreigabe passt nicht zum aktuellen Job oder Vertrag")
        reused_workspace = bool(agent.workspace_path or agent.branch)
        workspace = self.workspace_manager.get_or_create_workspace(agent)
        run_id = f"RUN-{task.task_id}-{uuid.uuid4().hex[:12]}"
        starting_head = self.workspace_manager.head_commit(workspace)
        if pending and starting_head != pending.context_commit:
            raise ValidationError("Korrektur-Workspace steht nicht auf dem freigegebenen Commit")
        run_started = False
        record = WorkerRunRecord(
            run_id=run_id,
            agent_id=agent.agent_id,
            task_id=task.task_id,
            base_commit=agent.base_commit,
            branch=workspace.branch,
            workspace_path=str(workspace.path),
            execution_backend=(
                WorkerExecutionBackend.DOCKER
                if job.isolation is not None
                else WorkerExecutionBackend.HOST
            ),
            isolation=job.isolation.model_copy(deep=True) if job.isolation else None,
            resume_contract_sha256=_resume_contract(state, job),
        )

        try:
            self._guard("begin_worker_run")
            self.orchestrator.bind_workspace(
                agent.agent_id,
                branch=workspace.branch,
                workspace_path=str(workspace.path),
            )
            if self.start_validator is None:
                self.orchestrator.begin_worker_run(record)
            else:
                self.orchestrator.begin_worker_run(record, state_validator=self.start_validator)
            run_started = True
            if job.isolation is not None:
                runtime_version = self._container_executor().ensure_available()
                self.orchestrator.update_worker_run(
                    run_id,
                    phase=WorkerPhase.PREPARE,
                    message=(
                        f"Container-Runtime {runtime_version}; Image {job.isolation.image}; "
                        f"Netzwerk=none; RAM={job.isolation.memory_mb} MiB; "
                        f"CPU={job.isolation.cpus}; PIDs={job.isolation.pids_limit}."
                    ),
                )
            implementation_executions = self._execute_commands(
                workspace,
                job.worker.implementation_commands,
                agent=agent,
                isolation=job.isolation,
                run_id=run_id,
                phase=WorkerPhase.IMPLEMENT,
                read_only_workspace=False,
            )
            self.orchestrator.update_worker_run(
                run_id,
                phase=WorkerPhase.IMPLEMENT,
                message=f"{len(implementation_executions)} Implementierungsbefehl(e) ausgeführt.",
                implementation_executions=implementation_executions,
            )
            self._require_passed(implementation_executions, "Implementierung")
            self.workspace_manager.assert_head(workspace, starting_head)

            changed_files = self.workspace_manager.validate_changes(agent, workspace)
            if not changed_files:
                raise ValidationError("Worker erzeugte keine überprüfbaren Dateiänderungen")
            self.orchestrator.update_worker_run(
                run_id,
                phase=WorkerPhase.VALIDATE_SCOPE,
                message=f"Schreibbereich geprüft; {len(changed_files)} Datei(en) geändert.",
                changed_files=changed_files,
            )

            self._guard("before_implementation_commit")
            implementation_commit = self.workspace_manager.commit_changes(
                agent,
                workspace,
                message=job.worker.commit_message,
            )
            self._guard("after_implementation_commit")
            self.workspace_manager.assert_clean(workspace)
            committed_files = self.workspace_manager.validate_committed_changes(
                agent,
                workspace,
                base_commit=agent.base_commit,
                head_commit=implementation_commit,
            )
            if not set(changed_files).issubset(set(committed_files)):
                raise ValidationError(
                    "Geprüfte Arbeitskopie ist nicht vollständig im Implementierungs-Commit enthalten: "
                    f"arbeitskopie={changed_files}, commit={committed_files}"
                )
            changed_files = committed_files
            self.orchestrator.update_worker_run(
                run_id,
                phase=WorkerPhase.COMMIT,
                message=f"Implementierung als Commit {implementation_commit} gespeichert.",
                implementation_commit=implementation_commit,
                changed_files=changed_files,
            )

            return self._finish_committed_run(run_id, workspace, job)
        except Exception as exc:
            guard_active = self._guard_allows_persistence()
            if guard_active:
                self._persist_failure(run_id, exc)
            should_cleanup = guard_active and (
                (not run_started and not reused_workspace)
                or (job.worker.cleanup_workspace_on_failure and not reused_workspace)
            )
            if should_cleanup:
                try:
                    self.workspace_manager.remove_workspace(workspace, delete_branch=True)
                    current = self.orchestrator.state().agents.get(agent.agent_id)
                    if current and (current.workspace_path or current.branch):
                        self.orchestrator.unbind_workspace(agent.agent_id)
                except Exception:
                    pass
            if isinstance(exc, WorkerExecutionError):
                raise
            raise WorkerExecutionError(str(exc), run_id=run_id) from exc

    def resume(self, job: WorkerPipelineJob, *, run_id: str) -> WorkerPipelineResult:
        """Continue a journaled commit; never replay implementation commands.

        The caller owns the existing queue/fence policy. This local operation
        neither claims a queue lease nor authorizes container/provider recovery.
        """
        job = WorkerPipelineJob.model_validate(job.model_dump(mode="json"))
        with self.workspace_manager.worker_execution_lock(job.worker.agent_id):
            self._guard("resume_pipeline")
            state = self.orchestrator.state()
            run = state.worker_runs.get(run_id)
            if run is None or run.agent_id != job.worker.agent_id:
                raise ValidationError("Wiederaufnahme benötigt den zugehörigen Worker-Run")
            self._validate_job(job.worker, job.gates, job.isolation, resume_run_id=run_id)
            agent = state.agents[run.agent_id]
            task = state.tasks[run.task_id]
            if (run.state not in {WorkerRunState.RUNNING, WorkerRunState.SUBMITTED,
                                  WorkerRunState.READY_TO_INTEGRATE}
                    or run.phase not in {WorkerPhase.COMMIT, WorkerPhase.TEST, WorkerPhase.SUBMIT,
                                         WorkerPhase.REVIEW, WorkerPhase.SECURITY, WorkerPhase.QA,
                                         WorkerPhase.COMPLETE}
                    or not run.implementation_commit):
                raise TransitionError("Wiederaufnahme benötigt einen gespeicherten Implementierungscommit")
            if (not run.resume_contract_sha256
                    or run.resume_contract_sha256 != _resume_contract(state, job)):
                raise ValidationError("Wiederaufnahmevertrag fehlt oder wurde verändert")
            if (run.task_id != task.task_id or run.base_commit != task.base_commit
                    or run.base_commit != agent.base_commit or run.branch != agent.branch
                    or run.workspace_path != agent.workspace_path or run.isolation != job.isolation
                    or (run.execution_backend == WorkerExecutionBackend.DOCKER) != (job.isolation is not None)):
                raise ValidationError("Worker-Journal und aktuelle Zuweisung stimmen nicht überein")
            if run.state == WorkerRunState.READY_TO_INTEGRATE:
                if run.phase != WorkerPhase.COMPLETE or run.finished_at is None or run.error:
                    raise ValidationError("Gespeicherter Worker-Abschlussbeleg ist inkonsistent")
            elif run.finished_at is not None or run.error:
                raise ValidationError("Unfertiger Worker-Run trägt einen Abschlussbeleg")
            workspace = self.workspace_manager.workspace_from_binding(
                agent_id=run.agent_id, branch=run.branch, workspace_path=run.workspace_path,
                base_commit=run.base_commit,
            )
            self.workspace_manager.assert_head(workspace, run.implementation_commit)
            self.workspace_manager.assert_clean(workspace)
            changed = self.workspace_manager.validate_committed_changes(
                agent, workspace, base_commit=run.base_commit, head_commit=run.implementation_commit,
            )
            if not changed or sorted(changed) != sorted(run.changed_files):
                raise ValidationError("Implementierungscommit und gespeicherte Dateien unterscheiden sich")
            self._require_saved_commands(
                run.implementation_executions,
                [render_command(command) for command in job.worker.implementation_commands],
            )
            if task.state != TaskState.ACTIVE:
                if (task.implementation_commit != run.implementation_commit
                        or sorted(task.changed_files) != sorted(run.changed_files)):
                    raise ValidationError("Task-Einreichung gehört nicht zum gespeicherten Commit")
                if task.implementation_summary != job.worker.implementation_summary:
                    raise ValidationError(
                        "Gespeicherte Implementierungszusammenfassung unterscheidet sich"
                    )
                self._require_saved_commands(
                    run.test_executions,
                    [render_command(command) for command in job.worker.test_commands],
                )
                if task.test_executions != run.test_executions:
                    raise ValidationError("Gespeicherte Testbelege unterscheiden sich")
            if task.state in {TaskState.QA_PENDING, TaskState.READY_TO_INTEGRATE}:
                if (task.review_decision != ReviewDecision.APPROVED
                        or task.reviewer_agent_id != job.gates.reviewer_agent_id):
                    raise ValidationError("Gespeicherte Reviewfreigabe fehlt")
                self._require_saved_commands(
                    run.review_executions,
                    [
                        f"git diff --check {run.base_commit}..{run.implementation_commit}",
                        *(render_command(command) for command in job.gates.review_commands),
                    ],
                )
                if task.review_executions != run.review_executions:
                    raise ValidationError("Gespeicherte Review-Belege unterscheiden sich")
                if task.review_notes != self._gate_summary("Review", run.review_executions):
                    raise ValidationError("Gespeicherte Review-Zusammenfassung unterscheidet sich")
            elif task.review_decision != ReviewDecision.PENDING:
                raise ValidationError("Reviewzustand passt nicht zum Wiederaufnahmepunkt")
            if task.security_approved is not None:
                if (not task.requires_security_review or task.security_approved is not True
                        or task.security_agent_id != job.gates.security_agent_id):
                    raise ValidationError("Gespeicherte Securityfreigabe ist ungültig")
                self._require_saved_commands(
                    run.security_executions,
                    [render_command(command) for command in job.gates.security_commands],
                )
                if task.security_executions != run.security_executions:
                    raise ValidationError("Gespeicherte Security-Belege unterscheiden sich")
                if task.security_notes != self._gate_summary("Security", run.security_executions):
                    raise ValidationError("Gespeicherte Security-Zusammenfassung unterscheidet sich")
            if task.qa_status == QAStatus.PASSED:
                if task.qa_agent_id != job.gates.qa_agent_id:
                    raise ValidationError("Gespeicherte QA-Identität unterscheidet sich")
                self._require_saved_commands(
                    run.qa_executions,
                    [render_command(command) for command in job.gates.qa_commands],
                )
                if task.qa_executions != run.qa_executions:
                    raise ValidationError("Gespeicherte QA-Belege unterscheiden sich")
            elif task.qa_status != QAStatus.PENDING:
                raise ValidationError("Fehlgeschlagene QA kann nicht fortgesetzt werden")
            if task.state == TaskState.READY_TO_INTEGRATE:
                if (task.qa_status != QAStatus.PASSED
                        or (task.requires_security_review and task.security_approved is not True)):
                    raise ValidationError("Gespeicherte Pflicht-Gates sind unvollständig")
                if run.state == WorkerRunState.READY_TO_INTEGRATE:
                    return WorkerPipelineResult(run=run, workspace=workspace)
            elif run.state == WorkerRunState.READY_TO_INTEGRATE:
                raise ValidationError("Worker-Run und Task-Freigabe unterscheiden sich")
            if run.code_repair and run.code_repair.queue_job_id and (
                    self.execution_guard is None
                    or self.container_metadata.get("job_id") != run.code_repair.queue_job_id):
                raise ValidationError("Wiederaufnahme gehört zu einem anderen Queue-Job")
            if job.isolation is not None:
                repository_id = self.container_metadata.get("repository_id")
                if self.execution_guard is None or not repository_id:
                    raise ValidationError("Container-Wiederaufnahme benötigt aktive Queue-/Fence-Kontrolle")
                containers = self._container_executor().list_managed_containers(repository_id=repository_id)
                if any(item.labels.get("tankai.run_id") == run_id for item in containers):
                    raise ValidationError("Run besitzt noch Container; zuerst vorhandene Recovery verwenden")
            try:
                self._guard("resume_before_checks")
                if job.isolation is not None:
                    self._container_executor().ensure_available()
                self.orchestrator.update_worker_run(
                    run_id, phase=run.phase,
                    message="Gespeicherten Commit geprüft; ohne erneute Implementierung fortgesetzt.",
                    state_value=(WorkerRunState.RUNNING if task.state == TaskState.ACTIVE
                                 else WorkerRunState.SUBMITTED),
                )
                return self._finish_committed_run(run_id, workspace, job)
            except Exception as exc:
                if self._guard_allows_persistence():
                    self._persist_failure(run_id, exc)
                raise WorkerExecutionError(str(exc), run_id=run_id) from exc

    @staticmethod
    def _require_saved_commands(executions: list[TestExecution], expected: list[str]) -> None:
        if (not executions or not all(item.passed for item in executions)
                or [item.command for item in executions] != expected):
            raise ValidationError("Gespeicherte Befehlsbelege sind unvollständig oder fehlgeschlagen")

    def _finish_committed_run(
        self, run_id: str, workspace: Workspace, job: WorkerPipelineJob,
    ) -> WorkerPipelineResult:
        state = self.orchestrator.state()
        run = state.worker_runs[run_id]
        agent = state.agents[run.agent_id]
        task = state.tasks[run.task_id]
        implementation_commit = run.implementation_commit or ""
        changed_files = run.changed_files
        if task.state == TaskState.ACTIVE:
            test_executions = self._execute_commands(
                workspace,
                job.worker.test_commands,
                agent=agent,
                isolation=job.isolation,
                run_id=run_id,
                phase=WorkerPhase.TEST,
                read_only_workspace=True,
            )
            try:
                self.workspace_manager.cleanup_check_artifacts(
                    workspace, implementation_commit
                )
            except GitWorkspaceError as exc:
                test_executions.append(TestExecution(
                    command="[worker-test-integrity]",
                    passed=False,
                    exit_code=None,
                    summary=str(exc),
                ))
            self.orchestrator.update_worker_run(
                run_id,
                phase=WorkerPhase.TEST,
                message=f"{len(test_executions)} Worker-Test(s) ausgeführt.",
                test_executions=test_executions,
            )
            self._require_passed(test_executions, "Worker-Tests", allow_empty=not task.required_tests)
            self._guard("submit_for_review")
            self.orchestrator.submit_task_completion(
                agent.agent_id,
                implementation_summary=job.worker.implementation_summary,
                changed_files=changed_files,
                tests=test_executions,
                implementation_commit=implementation_commit,
                worker_run_id=run_id,
            )
            self.orchestrator.update_worker_run(
                run_id,
                phase=WorkerPhase.SUBMIT,
                message="Implementierung zur unabhängigen Prüfung eingereicht.",
                state_value=WorkerRunState.SUBMITTED,
            )

        self._guard("begin_independent_gates")
        self._run_gates(run_id, workspace, job.gates, job.isolation)
        self._guard("complete_worker_run")
        completed = self.orchestrator.complete_worker_run(run_id)
        return WorkerPipelineResult(run=completed, workspace=workspace)

    def _run_gates(
        self,
        run_id: str,
        workspace: Workspace,
        gates: GateJob,
        isolation: WorkerIsolationSpec | None,
    ) -> None:
        self._guard("review_gate")
        state = self.orchestrator.state()
        run = state.worker_runs[run_id]
        task = state.tasks[run.task_id]
        agent = state.agents[run.agent_id]

        if task.review_decision != ReviewDecision.APPROVED:
            self.orchestrator.assign_reviewer(task.task_id, gates.reviewer_agent_id)
            diff_check = self.workspace_manager.run_command(
                workspace,
                ["git", "diff", "--check", f"{run.base_commit}..{run.implementation_commit}"],
            )
            diff_check.command = f"git diff --check {run.base_commit}..{run.implementation_commit}"
            review_executions = [diff_check]
            if diff_check.passed:
                review_executions.extend(self._execute_gate_commands(
                    workspace,
                    gates.review_commands,
                    expected_commit=run.implementation_commit or "",
                    agent=agent,
                    isolation=isolation,
                    run_id=run_id,
                    phase=WorkerPhase.REVIEW,
                ))
            review_passed = all(item.passed for item in review_executions)
            self.orchestrator.update_worker_run(
                run_id,
                phase=WorkerPhase.REVIEW,
                message=(
                    "Unabhängiges Review bestanden."
                    if review_passed
                    else "Unabhängiges Review fehlgeschlagen."
                ),
                review_executions=review_executions,
            )
            self.orchestrator.record_review(
                task.task_id,
                gates.reviewer_agent_id,
                approved=review_passed,
                notes=self._gate_summary("Review", review_executions),
                executions=review_executions,
            )
            if not review_passed:
                raise TransitionError("Review-Gate hat die Änderung abgelehnt")

        task = self.orchestrator.state().tasks[run.task_id]
        if task.requires_security_review and task.security_approved is not True:
            if not gates.security_agent_id or not gates.security_commands:
                raise ValidationError(
                    "Security-pflichtiger Task benötigt Security-Agent und Security-Befehle"
                )
            security_executions = self._execute_gate_commands(
                workspace,
                gates.security_commands,
                expected_commit=run.implementation_commit or "",
                agent=agent,
                isolation=isolation,
                run_id=run_id,
                phase=WorkerPhase.SECURITY,
            )
            security_passed = all(item.passed for item in security_executions)
            self.orchestrator.update_worker_run(
                run_id,
                phase=WorkerPhase.SECURITY,
                message=(
                    "Security-Gate bestanden."
                    if security_passed
                    else "Security-Gate fehlgeschlagen."
                ),
                security_executions=security_executions,
            )
            self.orchestrator.record_security_review(
                task.task_id,
                gates.security_agent_id,
                approved=security_passed,
                notes=self._gate_summary("Security", security_executions),
                executions=security_executions,
            )
            if not security_passed:
                raise TransitionError("Security-Gate hat die Änderung abgelehnt")

        task = self.orchestrator.state().tasks[run.task_id]
        if task.qa_status != QAStatus.PASSED:
            self.orchestrator.assign_qa(task.task_id, gates.qa_agent_id)
            qa_executions = self._execute_gate_commands(
                workspace,
                gates.qa_commands,
                expected_commit=run.implementation_commit or "",
                agent=agent,
                isolation=isolation,
                run_id=run_id,
                phase=WorkerPhase.QA,
            )
            self.orchestrator.update_worker_run(
                run_id,
                phase=WorkerPhase.QA,
                message=f"{len(qa_executions)} unabhängige QA-Prüfung(en) ausgeführt.",
                qa_executions=qa_executions,
            )
            result = self.orchestrator.record_qa(
                task.task_id,
                gates.qa_agent_id,
                executions=qa_executions,
            )
            if result.state != TaskState.READY_TO_INTEGRATE:
                raise TransitionError("QA-Gate hat die Änderung nicht zur Integration freigegeben")

    def _validate_job(
        self,
        worker: WorkerJob,
        gates: GateJob,
        isolation: WorkerIsolationSpec | None,
        *,
        resume_run_id: str | None = None,
    ) -> None:
        if self.require_container_isolation and isolation is None:
            raise ValidationError("Worker-Container-Isolation ist für diesen Runner verpflichtend")
        state = self.orchestrator.state()
        try:
            agent = state.agents[worker.agent_id]
            task = state.tasks[agent.task_id]
        except KeyError as exc:
            raise ValidationError(f"Unbekannter Worker-Agent oder Task: {worker.agent_id}") from exc
        if task.assigned_agent_id != agent.agent_id:
            raise ValidationError("Worker-Agent ist dem Task nicht zugewiesen")
        valid_states = {TaskState.ACTIVE} if resume_run_id is None else {
            TaskState.ACTIVE, TaskState.REVIEW_PENDING, TaskState.QA_PENDING, TaskState.READY_TO_INTEGRATE,
        }
        valid_statuses = {"active"} if task.state == TaskState.ACTIVE else {"waiting_for_review"}
        if agent.status.value not in valid_statuses or task.state not in valid_states:
            raise TransitionError("Worker-Agent und Task müssen aktiv sein")
        if task.worker_run_id != resume_run_id:
            raise TransitionError("Task besitzt einen anderen Worker-Run; vorhandenen Run fortsetzen")
        reviewer = state.agents.get(gates.reviewer_agent_id)
        qa = state.agents.get(gates.qa_agent_id)
        if reviewer is None or reviewer.role != DevelopmentRole.REVIEWER:
            raise ValidationError("Gate-Job benötigt einen existierenden Reviewer-Agenten")
        if qa is None or qa.role != DevelopmentRole.QA:
            raise ValidationError("Gate-Job benötigt einen existierenden QA-Agenten")
        identities = {agent.agent_id, reviewer.agent_id, qa.agent_id}
        if len(identities) != 3:
            raise ValidationError("Implementierung, Review und QA benötigen getrennte Agenten")
        if task.requires_security_review:
            security = state.agents.get(gates.security_agent_id or "")
            if security is None or security.role != DevelopmentRole.SECURITY:
                raise ValidationError("Security-pflichtiger Task benötigt einen Security-Agenten")
            if security.agent_id in identities:
                raise ValidationError("Security-Agent muss eine getrennte Identität besitzen")
            if not gates.security_commands:
                raise ValidationError("Security-pflichtiger Task benötigt Security-Befehle")
        elif gates.security_agent_id or gates.security_commands:
            raise ValidationError(
                "Security-Gate darf nur für Tasks mit requires_security_review gesetzt werden"
            )

    def _execute_commands(
        self,
        workspace: Workspace,
        commands: list[CommandSpec],
        *,
        agent: AgentSpec,
        isolation: WorkerIsolationSpec | None,
        run_id: str,
        phase: WorkerPhase,
        read_only_workspace: bool,
    ) -> list[TestExecution]:
        executions: list[TestExecution] = []
        for index, command in enumerate(commands, start=1):
            self._guard(f"{phase.value}:command:{index}:before")
            cancellation_check = (
                None
                if self.execution_guard is None
                else lambda: self._guard(
                    f"{phase.value}:command:{index}:running"
                )
            )
            if isolation is None:
                result = self.workspace_manager.run_command(
                    workspace,
                    command.argv,
                    timeout_seconds=command.timeout_seconds,
                    env=command.env,
                    cancellation_check=cancellation_check,
                )
            else:
                executor = self._container_executor()
                execute_kwargs = {
                    "allowed_paths": agent.allowed_paths,
                    "denied_paths": agent.denied_paths,
                    "read_only_workspace": read_only_workspace,
                    "run_id": run_id,
                    "phase": phase.value,
                }
                # Backwards-compatible test/adapter executors may not yet expose
                # cooperative cancellation. The hardened production executor does.
                parameters = inspect.signature(executor.execute).parameters
                if "cancellation_check" in parameters:
                    execute_kwargs["cancellation_check"] = cancellation_check
                if "metadata_labels" in parameters:
                    execute_kwargs["metadata_labels"] = self.container_metadata
                result = executor.execute(
                    workspace,
                    command,
                    isolation,
                    **execute_kwargs,
                )
            self._guard(f"{phase.value}:command:{index}:after")
            result.command = render_command(command)
            executions.append(result)
            if not result.passed:
                break
        return executions

    def _execute_gate_commands(
        self,
        workspace: Workspace,
        commands: list[CommandSpec],
        *,
        expected_commit: str,
        agent: AgentSpec,
        isolation: WorkerIsolationSpec | None,
        run_id: str,
        phase: WorkerPhase,
    ) -> list[TestExecution]:
        self.workspace_manager.assert_head(workspace, expected_commit)
        self.workspace_manager.assert_clean(workspace)
        executions = self._execute_commands(
            workspace,
            commands,
            agent=agent,
            isolation=isolation,
            run_id=run_id,
            phase=phase,
            read_only_workspace=True,
        )
        try:
            self.workspace_manager.cleanup_check_artifacts(workspace, expected_commit)
        except GitWorkspaceError as exc:
            executions.append(TestExecution(
                command="[gate-integrity]",
                passed=False,
                exit_code=None,
                summary=str(exc),
            ))
        return executions

    def _guard(self, stage: str) -> None:
        if self.execution_guard is not None:
            self.execution_guard(stage)

    def _guard_allows_persistence(self) -> bool:
        if self.execution_guard is None:
            return True
        try:
            self.execution_guard("persist_failure")
            return True
        except Exception:
            return False

    def _container_executor(self) -> DockerCommandExecutor:
        if self.container_executor is None:
            self.container_executor = DockerCommandExecutor()
        return self.container_executor

    @staticmethod
    def _require_passed(
        executions: list[TestExecution],
        label: str,
        *,
        allow_empty: bool = False,
    ) -> None:
        if not executions and not allow_empty:
            raise TransitionError(f"{label}: keine Prüfung ausgeführt")
        failed = [item.command for item in executions if not item.passed]
        if failed:
            raise TransitionError(f"{label} fehlgeschlagen: {', '.join(failed)}")

    @staticmethod
    def _gate_summary(label: str, executions: list[TestExecution]) -> str:
        passed = sum(item.passed for item in executions)
        failed = len(executions) - passed
        return f"{label}: {passed} bestanden, {failed} fehlgeschlagen."

    def _persist_failure(self, run_id: str, exc: Exception) -> None:
        try:
            state = self.orchestrator.state()
            run = state.worker_runs.get(run_id)
            if run is None or run.state in {
                WorkerRunState.FAILED,
                WorkerRunState.BLOCKED,
                WorkerRunState.READY_TO_INTEGRATE,
            }:
                return
            blocked = isinstance(exc, (ValidationError, TransitionError, GitWorkspaceError, ContainerRuntimeError))
            self.orchestrator.fail_worker_run(run_id, error=str(exc), blocked=blocked)
        except (OrchestrationError, RuntimeError):
            # Do not replace the original execution error with a secondary persistence error.
            return
