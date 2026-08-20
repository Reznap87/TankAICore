"""Crash-recoverable Git integration for reviewed worker runs.

The runner serializes MAIN mutations, rebases a reviewed worker branch onto the
current stable commit when necessary, performs a fast-forward merge, runs real
post-merge commands, and advances ProjectState only after Git and tests agree.
A small journal allows the next invocation to roll back a merge that completed
before the state transaction was durably committed.
"""

from __future__ import annotations

import inspect
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .container_runtime import DockerCommandExecutor
from .git_workspace import GitWorkspaceError, GitWorkspaceManager, Workspace
from .models import (
    IntegrationJob,
    TaskState,
    TestExecution,
    WorkerExecutionBackend,
    WorkerPhase,
    WorkerRunRecord,
    WorkerRunState,
)
from .orchestrator import (
    DevelopmentOrchestrator,
    OrchestrationError,
    TransitionError,
    ValidationError,
)
from .worker import render_command


class IntegrationExecutionError(RuntimeError):
    """Raised after rollback/state handling has been attempted."""

    def __init__(self, message: str, *, run_id: str | None = None) -> None:
        super().__init__(message)
        self.run_id = run_id


@dataclass(frozen=True)
class IntegrationResult:
    run: WorkerRunRecord
    workspace: Workspace
    previous_commit: str
    integration_commit: str
    rebased: bool
    cleanup_warning: str = ""


class _IntegrationJournal:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise GitWorkspaceError(f"Integrationsjournal ist beschädigt: {exc}") from exc
        required = {"stage", "run_id", "task_id", "branch", "previous_commit"}
        if not isinstance(payload, dict) or not required.issubset(payload):
            raise GitWorkspaceError("Integrationsjournal besitzt ein ungültiges Schema")
        return payload

    def write(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        fd, temp_name = tempfile.mkstemp(
            prefix=self.path.name + ".",
            suffix=".tmp",
            dir=self.path.parent,
            text=True,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.path)
            try:
                directory_fd = os.open(self.path.parent, os.O_RDONLY)
            except OSError:
                directory_fd = None
            if directory_fd is not None:
                try:
                    os.fsync(directory_fd)
                except OSError:
                    pass
                finally:
                    os.close(directory_fd)
        except Exception:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)


class WorkerIntegrationRunner:
    def __init__(
        self,
        orchestrator: DevelopmentOrchestrator,
        workspace_manager: GitWorkspaceManager,
        *,
        container_executor: DockerCommandExecutor | None = None,
        require_container_isolation: bool = False,
        container_metadata: dict[str, str] | None = None,
    ) -> None:
        self.orchestrator = orchestrator
        self.workspace_manager = workspace_manager
        self.container_executor = container_executor
        self.require_container_isolation = require_container_isolation
        self.container_metadata = dict(container_metadata or {})
        self.journal = _IntegrationJournal(
            workspace_manager.common_git_dir / "tankai-integration-journal.json"
        )

    def run(self, job: IntegrationJob) -> IntegrationResult:
        try:
            with self.workspace_manager.integration_lock():
                self.recover_interrupted_integration()
                state = self.orchestrator.state()
                try:
                    run = state.worker_runs[job.run_id]
                    task = state.tasks[run.task_id]
                    agent = state.agents[run.agent_id]
                except KeyError as exc:
                    raise IntegrationExecutionError(
                        f"Unbekannter Worker-Run oder inkonsistenter Zustand: {job.run_id}",
                        run_id=job.run_id,
                    ) from exc

                self._validate_ready_state(run, task.state, task.worker_run_id)
                self._validate_required_tests(task.required_tests, job)
                self._validate_isolation(run, job)
                if job.isolation is not None:
                    self._container_executor().ensure_available()
                workspace = self.workspace_manager.get_or_create_workspace(agent)
                self.workspace_manager.assert_head(
                    workspace, run.implementation_commit or ""
                )
                self.workspace_manager.assert_clean(workspace)
                previous_commit = state.current_commit
                self.workspace_manager.assert_repository_ready(
                    branch=state.current_branch,
                    expected_commit=previous_commit,
                )

                journal_payload: dict[str, Any] = {
                    "stage": "prepared",
                    "run_id": run.run_id,
                    "task_id": run.task_id,
                    "branch": workspace.branch,
                    "workspace_path": str(workspace.path),
                    "previous_commit": previous_commit,
                    "original_base_commit": run.base_commit,
                    "original_implementation_commit": run.implementation_commit,
                    "integration_commit": None,
                }
                self.journal.write(journal_payload)

                integration_started = False
                merged_commit: str | None = None
                rebased = False
                rollback_error = ""
                try:
                    self.orchestrator.begin_worker_integration(
                        run.run_id,
                        expected_project_commit=previous_commit,
                    )
                    integration_started = True

                    current_run = self.orchestrator.state().worker_runs[run.run_id]
                    implementation_commit = current_run.implementation_commit or ""
                    if current_run.base_commit != previous_commit:
                        workspace, implementation_commit = self.workspace_manager.rebase_workspace(
                            workspace,
                            old_base_commit=current_run.base_commit,
                            new_base_commit=previous_commit,
                            expected_head=implementation_commit,
                        )
                        changed_files = self.workspace_manager.validate_committed_changes(
                            agent,
                            workspace,
                            base_commit=previous_commit,
                            head_commit=implementation_commit,
                        )
                        self.orchestrator.record_worker_rebase(
                            run.run_id,
                            old_base_commit=current_run.base_commit,
                            new_base_commit=previous_commit,
                            rebased_commit=implementation_commit,
                            changed_files=changed_files,
                        )
                        rebased = True

                    journal_payload.update({
                        "stage": "rebased",
                        "integration_commit": implementation_commit,
                    })
                    self.journal.write(journal_payload)
                    self.orchestrator.update_worker_run(
                        run.run_id,
                        phase=WorkerPhase.MERGE,
                        message=(
                            f"Integrations-Branch {workspace.branch} wird per Fast-Forward "
                            f"nach {state.current_branch} übernommen."
                        ),
                    )

                    merged_commit = self.workspace_manager.fast_forward_repository(
                        branch=state.current_branch,
                        source_branch=workspace.branch,
                        expected_base_commit=previous_commit,
                    )
                    if merged_commit != implementation_commit:
                        raise GitWorkspaceError(
                            "Fast-Forward-Commit stimmt nicht mit dem geprüften Worker-Commit überein"
                        )
                    journal_payload.update({
                        "stage": "merged",
                        "integration_commit": merged_commit,
                    })
                    self.journal.write(journal_payload)

                    executions = self._execute_post_merge_tests(
                        job,
                        previous_commit=previous_commit,
                        expected_commit=merged_commit,
                    )
                    self.orchestrator.update_worker_run(
                        run.run_id,
                        phase=WorkerPhase.INTEGRATION_TEST,
                        message=f"{len(executions)} Post-Merge-Gesamttest(s) ausgeführt.",
                        integration_executions=executions,
                        integration_commit=merged_commit,
                        integration_isolation=job.isolation,
                    )
                    failed = [item.command for item in executions if not item.passed]
                    if failed:
                        raise TransitionError(
                            "Post-Merge-Gesamttests fehlgeschlagen: " + ", ".join(failed)
                        )

                    completed = self.orchestrator.finalize_worker_integration(
                        run.run_id,
                        expected_previous_commit=previous_commit,
                        integration_commit=merged_commit,
                        executions=executions,
                    )
                    journal_payload["stage"] = "state_committed"
                    self.journal.write(journal_payload)
                    self.journal.clear()

                    cleanup_warning = self._cleanup_success(
                        workspace,
                        run.agent_id,
                        cleanup=job.cleanup_workspace_on_success,
                        delete_branch=job.delete_branch_on_success,
                    )
                    return IntegrationResult(
                        run=completed,
                        workspace=workspace,
                        previous_commit=previous_commit,
                        integration_commit=merged_commit,
                        rebased=rebased,
                        cleanup_warning=cleanup_warning,
                    )
                except Exception as exc:
                    if merged_commit is not None:
                        try:
                            self.workspace_manager.reset_repository(
                                expected_current=merged_commit,
                                target_commit=previous_commit,
                            )
                        except Exception as rollback_exc:  # preserve journal for recovery
                            rollback_error = str(rollback_exc)
                    else:
                        try:
                            persisted = self.orchestrator.state().worker_runs.get(run.run_id)
                            workspace_head = self.workspace_manager.head_commit(workspace)
                            persisted_head = persisted.implementation_commit if persisted else None
                            original_head = journal_payload.get("original_implementation_commit")
                            if (
                                original_head
                                and workspace_head != original_head
                                and workspace_head != persisted_head
                            ):
                                self.workspace_manager.reset_workspace(
                                    workspace,
                                    expected_current=workspace_head,
                                    target_commit=str(original_head),
                                )
                        except Exception as rollback_exc:
                            rollback_error = str(rollback_exc)
                    if not rollback_error:
                        self.journal.clear()
                    if integration_started:
                        try:
                            current = self.orchestrator.state().worker_runs.get(run.run_id)
                            if current and current.state == WorkerRunState.INTEGRATING:
                                self.orchestrator.fail_worker_run(
                                    run.run_id,
                                    error=(
                                        str(exc)
                                        if not rollback_error
                                        else f"{exc}; ROLLBACK FEHLGESCHLAGEN: {rollback_error}"
                                    ),
                                    blocked=True,
                                )
                        except OrchestrationError:
                            pass
                    message = str(exc)
                    if rollback_error:
                        message += f"; ROLLBACK FEHLGESCHLAGEN: {rollback_error}"
                    raise IntegrationExecutionError(message, run_id=run.run_id) from exc

        except IntegrationExecutionError:
            raise
        except Exception as exc:
            raise IntegrationExecutionError(str(exc), run_id=job.run_id) from exc

    def recover_interrupted_integration(self) -> None:
        """Resolve a prior crash before accepting a new integration attempt."""
        payload = self.journal.load()
        if payload is None:
            return
        stage = str(payload["stage"])
        run_id = str(payload["run_id"])
        previous_commit = str(payload["previous_commit"])
        integration_commit = payload.get("integration_commit")
        state = self.orchestrator.state()
        run = state.worker_runs.get(run_id)
        repository_head = self.workspace_manager.repository_head()

        if integration_commit and (
            state.current_commit == integration_commit
            and repository_head == integration_commit
            and run is not None
            and run.state == WorkerRunState.INTEGRATED
        ):
            self.journal.clear()
            return

        if stage == "merged":
            if (
                integration_commit
                and state.current_commit == previous_commit
                and repository_head == integration_commit
            ):
                self.workspace_manager.assert_repository_ready(
                    branch=state.current_branch,
                    expected_commit=integration_commit,
                )
                self.workspace_manager.reset_repository(
                    expected_current=integration_commit,
                    target_commit=previous_commit,
                )
                if run is not None and run.state == WorkerRunState.INTEGRATING:
                    self.orchestrator.reset_worker_integration(
                        run_id,
                        reason="Crash-Recovery nach Git-Merge vor State-Commit",
                    )
                self.journal.clear()
                return
            raise GitWorkspaceError(
                "Unterbrochene Merge-Integration kann nicht automatisch zugeordnet werden"
            )

        if stage in {"prepared", "rebased"}:
            if repository_head != previous_commit or state.current_commit != previous_commit:
                raise GitWorkspaceError(
                    "Integrationsjournal und MAIN/ProjectState widersprechen sich"
                )
            workspace_path = Path(str(payload.get("workspace_path", ""))).resolve()
            original_implementation = payload.get("original_implementation_commit")
            if workspace_path.exists() and run is not None:
                workspace = self.workspace_manager.workspace_from_binding(
                    agent_id=run.agent_id,
                    branch=str(payload["branch"]),
                    workspace_path=workspace_path,
                    base_commit=str(payload.get("original_base_commit") or run.base_commit),
                )
                self.workspace_manager.abort_rebase_if_needed(workspace)
                branch_head = self.workspace_manager.head_commit(workspace)
                if branch_head != run.implementation_commit:
                    if original_implementation and run.implementation_commit == original_implementation:
                        self.workspace_manager.reset_workspace(
                            workspace,
                            expected_current=branch_head,
                            target_commit=str(original_implementation),
                        )
                    else:
                        raise GitWorkspaceError(
                            "Worker-Branch und ProjectState widersprechen sich nach Crash"
                        )
            if run is not None and run.state == WorkerRunState.INTEGRATING:
                self.orchestrator.reset_worker_integration(
                    run_id,
                    reason="Crash-Recovery vor Git-Merge",
                )
            self.journal.clear()
            return

        if stage == "state_committed":
            raise GitWorkspaceError(
                "State-Commit ist journalisiert, aber Git oder ProjectState stimmen nicht überein"
            )
        raise GitWorkspaceError(f"Unbekannte Integrationsjournal-Phase: {stage}")

    def _execute_post_merge_tests(
        self,
        job: IntegrationJob,
        *,
        previous_commit: str,
        expected_commit: str,
    ) -> list[TestExecution]:
        diff_check = self.workspace_manager.run_repository_command(
            ["git", "diff", "--check", f"{previous_commit}..{expected_commit}"],
        )
        diff_check.command = f"git diff --check {previous_commit}..{expected_commit}"
        executions: list[TestExecution] = [diff_check]
        if not diff_check.passed:
            return executions
        repository_workspace = Workspace(
            agent_id="TECH_AI_ORCHESTRATOR",
            branch=self.workspace_manager.repository_branch(),
            path=self.workspace_manager.repository,
            base_commit=previous_commit,
        )
        for command in job.test_commands:
            if job.isolation is None:
                execution = self.workspace_manager.run_repository_command(
                    command.argv,
                    timeout_seconds=command.timeout_seconds,
                    env=command.env,
                )
            else:
                executor = self._container_executor()
                execute_kwargs = {
                    "allowed_paths": (),
                    "denied_paths": (),
                    "read_only_workspace": True,
                    "run_id": job.run_id,
                    "phase": WorkerPhase.INTEGRATION_TEST.value,
                }
                if "metadata_labels" in inspect.signature(executor.execute).parameters:
                    execute_kwargs["metadata_labels"] = self.container_metadata
                execution = executor.execute(
                    repository_workspace,
                    command,
                    job.isolation,
                    **execute_kwargs,
                )
            execution.command = render_command(command)
            executions.append(execution)
            if not execution.passed:
                break
        try:
            self.workspace_manager.cleanup_repository_check_artifacts(expected_commit)
        except GitWorkspaceError as exc:
            executions.append(TestExecution(
                command="[post-merge-integrity]",
                passed=False,
                exit_code=None,
                summary=str(exc),
            ))
        if not executions:
            executions.append(TestExecution(
                command="[post-merge-tests]",
                passed=False,
                exit_code=None,
                summary="Keine Post-Merge-Prüfung ausgeführt",
            ))
        return executions

    def _container_executor(self) -> DockerCommandExecutor:
        if self.container_executor is None:
            self.container_executor = DockerCommandExecutor()
        return self.container_executor

    def _validate_isolation(self, run: WorkerRunRecord, job: IntegrationJob) -> None:
        if self.require_container_isolation and job.isolation is None:
            raise ValidationError("Container-Isolation ist für Integrationstests verpflichtend")
        if run.execution_backend == WorkerExecutionBackend.DOCKER:
            if job.isolation is None:
                raise ValidationError(
                    "Containergeprüfter Worker darf nicht durch Host-Integrationstests herabgestuft werden"
                )
            if run.isolation is None or job.isolation.image != run.isolation.image:
                raise ValidationError(
                    "Integrationstests müssen dasselbe fixierte Worker-Image verwenden"
                )

    @staticmethod
    def _validate_required_tests(required_tests: list[str], job: IntegrationJob) -> None:
        executed = {render_command(command) for command in job.test_commands}
        missing = [command for command in required_tests if command not in executed]
        if missing:
            raise ValidationError(
                "Integrations-Job wiederholt nicht alle verpflichtenden Tests: "
                + ", ".join(missing)
            )

    def _cleanup_success(
        self,
        workspace: Workspace,
        agent_id: str,
        *,
        cleanup: bool,
        delete_branch: bool,
    ) -> str:
        if not cleanup:
            return ""
        try:
            # Clear the authoritative binding first. If physical cleanup fails,
            # ProjectState never points at an already deleted worktree.
            self.orchestrator.unbind_workspace(agent_id)
            self.workspace_manager.remove_workspace(
                workspace,
                delete_branch=delete_branch,
            )
            return ""
        except Exception as exc:
            warning = f"Integration erfolgreich, Workspace-Bereinigung fehlgeschlagen: {exc}"
            try:
                self.orchestrator.append_worklog(agent_id, warning)
            except OrchestrationError:
                pass
            return warning

    @staticmethod
    def _validate_ready_state(
        run: WorkerRunRecord,
        task_state: TaskState,
        task_worker_run_id: str | None,
    ) -> None:
        if run.state != WorkerRunState.READY_TO_INTEGRATE:
            raise ValidationError(
                f"Worker-Run ist nicht integrationsbereit: {run.state.value}"
            )
        if task_state != TaskState.READY_TO_INTEGRATE:
            raise ValidationError(f"Task ist nicht integrationsbereit: {task_state.value}")
        if task_worker_run_id != run.run_id:
            raise ValidationError("Task verweist auf einen anderen Worker-Run")
        if not run.implementation_commit:
            raise ValidationError("Worker-Run besitzt keinen Implementierungs-Commit")
