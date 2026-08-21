"""Controlled development-agent replication and merge-gate orchestration."""

from __future__ import annotations

import fnmatch
import re
from collections import defaultdict, deque
from pathlib import PurePosixPath
from typing import Iterable

from .models import (
    AgentGovernancePolicy,
    AgentSpec,
    AgentStatus,
    AuditEvent,
    CapabilityAction,
    CapabilitySpec,
    CapabilityStatus,
    DevelopmentRole,
    FileLock,
    ProjectState,
    QAStatus,
    ReviewDecision,
    SpawnRequest,
    TaskSpec,
    TaskState,
    TestExecution,
    WorkerIsolationSpec,
    WorkerPhase,
    WorkerRunRecord,
    WorkerRunState,
    WorkerStatusMessage,
    utcnow,
)
from .state_store import ProjectStateStore


class OrchestrationError(RuntimeError):
    pass


class ValidationError(OrchestrationError):
    pass


class ConflictError(OrchestrationError):
    pass


class TransitionError(OrchestrationError):
    pass


def normalize_repo_path(value: str) -> str:
    if not isinstance(value, str):
        raise ValidationError("Repository-Pfad muss Text sein")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValidationError(f"Kontrollzeichen im Repository-Pfad: {value!r}")
    raw = value.strip().replace("\\", "/")
    raw = re.sub(r"/+", "/", raw).lstrip("/")
    if not raw:
        raise ValidationError("Leerer Repository-Pfad ist nicht erlaubt")
    parts = PurePosixPath(raw).parts
    if any(part in {"", ".", ".."} for part in parts):
        raise ValidationError(f"Unsicherer Repository-Pfad: {value!r}")
    if parts and parts[0].endswith(":"):
        raise ValidationError(f"Absoluter Laufwerkspfad ist nicht erlaubt: {value!r}")
    return "/".join(parts)


def _scope_anchor(scope: str) -> tuple[str, bool, bool]:
    normalized = normalize_repo_path(scope)
    wildcard_index = min(
        (normalized.find(char) for char in "*[?" if char in normalized),
        default=-1,
    )
    if wildcard_index < 0:
        return normalized.rstrip("/"), False, False
    prefix = normalized[:wildcard_index].rstrip("/")
    recursive = normalized.endswith("/**")
    return prefix, True, recursive


def path_matches_scope(path: str, scope: str) -> bool:
    normalized_path = normalize_repo_path(path)
    normalized_scope = normalize_repo_path(scope)
    if normalized_scope.endswith("/**"):
        base = normalized_scope[:-3].rstrip("/")
        return normalized_path == base or normalized_path.startswith(base + "/")
    return fnmatch.fnmatchcase(normalized_path, normalized_scope)


def scopes_overlap(first: str, second: str) -> bool:
    """Conservative overlap check for repository write scopes."""
    a, a_wild, a_recursive = _scope_anchor(first)
    b, b_wild, b_recursive = _scope_anchor(second)

    if not a_wild and not b_wild:
        return a == b
    if a_recursive and not b_wild:
        return b == a or b.startswith(a + "/")
    if b_recursive and not a_wild:
        return a == b or a.startswith(b + "/")
    if a_recursive and b_recursive:
        return a == b or a.startswith(b + "/") or b.startswith(a + "/")

    # Generic glob patterns are treated conservatively from their static anchors.
    if not a or not b:
        return True
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def task_graph_order(tasks: dict[str, TaskSpec]) -> list[str]:
    indegree = {task_id: 0 for task_id in tasks}
    followers: dict[str, list[str]] = defaultdict(list)
    for task_id, task in tasks.items():
        for dependency in task.dependencies:
            if dependency not in tasks:
                raise ValidationError(
                    f"Task {task_id} referenziert unbekannte Abhängigkeit {dependency}"
                )
            indegree[task_id] += 1
            followers[dependency].append(task_id)

    queue = deque(sorted(task_id for task_id, degree in indegree.items() if degree == 0))
    order: list[str] = []
    while queue:
        current = queue.popleft()
        order.append(current)
        for follower in sorted(followers[current]):
            indegree[follower] -= 1
            if indegree[follower] == 0:
                queue.append(follower)
    if len(order) != len(tasks):
        cyclic = sorted(task_id for task_id, degree in indegree.items() if degree > 0)
        raise ValidationError(f"Zyklischer Task-Graph: {', '.join(cyclic)}")
    return order


class DevelopmentOrchestrator:
    _NON_TERMINAL_AGENT_STATUSES = {
        AgentStatus.CREATED,
        AgentStatus.INITIALIZING,
        AgentStatus.READY,
        AgentStatus.ACTIVE,
        AgentStatus.BLOCKED,
        AgentStatus.WAITING_FOR_REVIEW,
    }
    _QA_ROLES = {
        DevelopmentRole.QA,
        DevelopmentRole.QUALITY_LEAD,
        DevelopmentRole.UNIT_TEST,
        DevelopmentRole.INTEGRATION_TEST,
        DevelopmentRole.E2E,
        DevelopmentRole.REGRESSION,
        DevelopmentRole.PERFORMANCE_TEST,
        DevelopmentRole.FUZZ_TEST,
        DevelopmentRole.COMPATIBILITY_TEST,
        DevelopmentRole.USER_FLOW_TEST,
        DevelopmentRole.RELEASE_VALIDATION,
    }
    _SECURITY_ROLES = {
        DevelopmentRole.SECURITY,
        DevelopmentRole.SECURITY_LEAD,
        DevelopmentRole.APPSEC,
        DevelopmentRole.INFRA_SECURITY,
        DevelopmentRole.SECRETS,
        DevelopmentRole.PRIVACY,
        DevelopmentRole.THREAT_MODEL,
        DevelopmentRole.RED_TEAM,
        DevelopmentRole.DEPENDENCY_SECURITY,
        DevelopmentRole.AI_SAFETY,
    }
    _REVIEW_ROLES = {
        DevelopmentRole.REVIEWER,
        DevelopmentRole.CHIEF_ARCHITECT,
        DevelopmentRole.SOLUTION_ARCHITECT,
        DevelopmentRole.QUALITY_LEAD,
        DevelopmentRole.RELEASE_VALIDATION,
    }

    def __init__(
        self,
        store: ProjectStateStore,
        *,
        max_active_agents: int = 40,
        max_total_agents_per_cycle: int = 80,
        max_clone_depth: int = 5,
        max_children_per_agent: int = 3,
        max_agents_per_file: int = 1,
        max_agents_per_module: int = 4,
    ) -> None:
        self.store = store
        self.governance = AgentGovernancePolicy(
            max_active_agents=max_active_agents,
            max_total_agents_per_cycle=max_total_agents_per_cycle,
            max_clone_depth=max_clone_depth,
            max_children_per_agent=max_children_per_agent,
            max_agents_per_file=max_agents_per_file,
            max_agents_per_module=max_agents_per_module,
        )
        # Compatibility attributes for callers that inspected the old limits.
        self.max_active_agents = self.governance.max_active_agents
        self.max_total_agents_per_cycle = self.governance.max_total_agents_per_cycle
        self.max_clone_depth = self.governance.max_clone_depth
        self.max_children_per_agent = self.governance.max_children_per_agent
        self.max_agents_per_module = self.governance.max_agents_per_module

    @classmethod
    def initialize(
        cls,
        state_path: str,
        *,
        current_version: str,
        current_branch: str,
        current_commit: str,
        architecture_status: str = "reviewed",
        **limits: int,
    ) -> "DevelopmentOrchestrator":
        store = ProjectStateStore(state_path)
        orchestrator = cls(store, **limits)
        state = ProjectState(
            current_version=current_version,
            current_branch=current_branch,
            current_commit=current_commit,
            architecture_status=architecture_status,
            governance=orchestrator.governance.model_copy(deep=True),
        )
        orchestrator._event(state, "project_initialized", "TECH_AI_ORCHESTRATOR", {
            "version": current_version,
            "branch": current_branch,
            "commit": current_commit,
        })
        store.create(state)
        return orchestrator

    def state(self) -> ProjectState:
        return self.store.load()

    def begin_cycle(self, *, reason: str) -> ProjectState:
        """Start a fresh bounded development cycle after all prior agents terminate."""
        normalized_reason = reason.strip()
        if not normalized_reason:
            raise ValidationError("Grund für den neuen Entwicklungszyklus fehlt")

        def mutate(state: ProjectState) -> ProjectState:
            non_terminal = [
                agent.agent_id
                for agent in state.agents.values()
                if agent.status in self._NON_TERMINAL_AGENT_STATUSES
            ]
            if non_terminal:
                raise TransitionError(
                    "Neuer Entwicklungszyklus ist mit nicht-terminalen Agenten gesperrt: "
                    + ", ".join(sorted(non_terminal))
                )
            state.cycle_sequence += 1
            state.cycle_id = f"cycle-{state.cycle_sequence:06d}"
            state.cycle_agent_ids = []
            self._event(state, "development_cycle_started", "TECH_AI_ORCHESTRATOR", {
                "cycle_id": state.cycle_id,
                "reason": normalized_reason,
            })
            return state.model_copy(deep=True)

        state, _ = self.store.transaction(mutate)
        return state

    def create_task(self, task: TaskSpec) -> TaskSpec:
        def mutate(state: ProjectState) -> TaskSpec:
            if task.task_id in state.tasks:
                raise ConflictError(f"Task existiert bereits: {task.task_id}")
            if task.base_commit != state.current_commit:
                raise ValidationError(
                    f"Task-Basis {task.base_commit} ist nicht CURRENT_STABLE_COMMIT "
                    f"{state.current_commit}"
                )
            normalized = task.model_copy(deep=True)
            self._validate_capability_task(state, normalized)
            normalized.allowed_paths = self._normalize_scopes(normalized.allowed_paths)
            normalized.denied_paths = self._normalize_scopes(normalized.denied_paths)
            if not normalized.acceptance_criteria:
                raise ValidationError("Task benötigt mindestens ein messbares Abnahmekriterium")
            for dependency in normalized.dependencies:
                if dependency not in state.tasks:
                    raise ValidationError(f"Unbekannte Task-Abhängigkeit: {dependency}")
            state.tasks[normalized.task_id] = normalized
            task_graph_order(state.tasks)
            self._event(state, "task_created", "TECH_AI_ORCHESTRATOR", {
                "task_id": normalized.task_id,
                "allowed_paths": normalized.allowed_paths,
            })
            return normalized.model_copy(deep=True)

        _, created = self.store.transaction(mutate)
        return created

    def start_agent(
        self,
        task_id: str,
        role: DevelopmentRole,
        *,
        agent_id: str | None = None,
    ) -> AgentSpec:
        def mutate(state: ProjectState) -> AgentSpec:
            task = self._task(state, task_id)
            if task.assigned_agent_id:
                raise ConflictError(f"Task {task_id} ist bereits {task.assigned_agent_id} zugewiesen")
            self._assert_dependencies_integrated(state, task)
            if task.base_commit != state.current_commit:
                previous_base = task.base_commit
                task.base_commit = state.current_commit
                task.updated_at = utcnow()
                self._event(state, "task_rebased_before_start", "TECH_AI_ORCHESTRATOR", {
                    "task_id": task_id,
                    "previous_base_commit": previous_base,
                    "new_base_commit": state.current_commit,
                })
            self._assert_capacity(state)
            self._assert_module_capacity(state, task)
            self._assert_scopes_available(state, task.allowed_paths)
            new_id = agent_id or self._next_agent_id(state, role)
            if new_id in state.agents:
                raise ConflictError(f"Agent existiert bereits: {new_id}")
            agent = AgentSpec(
                agent_id=new_id,
                role=role,
                generation=0,
                cycle_id=state.cycle_id,
                base_commit=state.current_commit,
                task_id=task_id,
                allowed_paths=list(task.allowed_paths),
                denied_paths=list(task.denied_paths),
                acceptance_criteria=list(task.acceptance_criteria),
                required_tests=list(task.required_tests),
                reviewer_agent_id=task.reviewer_agent_id,
                priority=task.priority,
                deadlock_rules=list(task.deadlock_rules),
            )
            state.agents[new_id] = agent
            self._register_cycle_agent(state, new_id)
            task.assigned_agent_id = new_id
            task.state = TaskState.ACTIVE
            task.updated_at = utcnow()
            self._acquire_locks(state, agent)
            self._event(state, "agent_started", new_id, {
                "task_id": task_id,
                "role": role.value,
                "generation": 0,
            })
            return agent.model_copy(deep=True)

        _, agent = self.store.transaction(mutate)
        return agent

    def approve_spawn(self, request: SpawnRequest) -> AgentSpec:
        def mutate(state: ProjectState) -> AgentSpec:
            parent = self._agent(state, request.parent_agent_id)
            if parent.status != AgentStatus.ACTIVE:
                raise TransitionError(f"Eltern-Agent ist nicht aktiv: {parent.agent_id}")
            if request.requested_role != parent.role:
                raise ValidationError("Folge-Agent muss dieselbe Rolle wie der Eltern-Agent besitzen")
            if request.base_commit != state.current_commit or request.base_commit != parent.base_commit:
                raise ValidationError(
                    "Spawn-Request basiert nicht auf CURRENT_STABLE_COMMIT beziehungsweise Eltern-Basis"
                )
            generation = parent.generation + 1
            if generation > state.governance.max_clone_depth:
                raise ValidationError(
                    "MAX_CLONE_DEPTH überschritten: "
                    f"{generation}>{state.governance.max_clone_depth}"
                )
            children = sum(
                1 for agent in state.agents.values() if agent.parent_agent_id == parent.agent_id
            )
            if children >= state.governance.max_children_per_agent:
                raise ValidationError(
                    "MAX_CHILDREN_PER_AGENT erreicht: "
                    f"{state.governance.max_children_per_agent}"
                )
            if request.task_id in state.tasks:
                raise ConflictError(f"Spawn-Task existiert bereits: {request.task_id}")
            self._assert_capacity(state)
            allowed_paths = self._normalize_scopes(request.allowed_paths)
            denied_paths = self._normalize_scopes(request.denied_paths)
            acceptance_criteria = [item.strip() for item in request.acceptance_criteria if item.strip()]
            if not acceptance_criteria:
                raise ValidationError("Spawn-Task benötigt mindestens ein messbares Abnahmekriterium")
            self._assert_scopes_available(state, allowed_paths)
            for dependency in request.dependencies:
                if dependency not in state.tasks:
                    raise ValidationError(f"Unbekannte Spawn-Abhängigkeit: {dependency}")

            task = TaskSpec(
                task_id=request.task_id,
                goal=request.assigned_subtask,
                base_commit=request.base_commit,
                affected_components=request.affected_components,
                allowed_paths=allowed_paths,
                denied_paths=denied_paths,
                dependencies=request.dependencies,
                acceptance_criteria=acceptance_criteria,
                required_tests=request.required_tests,
                priority=request.priority,
                deadlock_rules=request.deadlock_rules,
                requires_security_review=request.requires_security_review,
                state=TaskState.ACTIVE,
            )
            self._assert_dependencies_integrated(state, task)
            self._assert_module_capacity(state, task)
            state.tasks[task.task_id] = task
            task_graph_order(state.tasks)
            agent_id = self._next_agent_id(state, request.requested_role)
            child = AgentSpec(
                agent_id=agent_id,
                role=request.requested_role,
                parent_agent_id=parent.agent_id,
                generation=generation,
                cycle_id=state.cycle_id,
                base_commit=request.base_commit,
                task_id=task.task_id,
                allowed_paths=allowed_paths,
                denied_paths=denied_paths,
                acceptance_criteria=list(acceptance_criteria),
                required_tests=list(request.required_tests),
                priority=request.priority,
                deadlock_rules=list(request.deadlock_rules),
                worklog=[f"Spawn genehmigt: {request.reason}"],
            )
            state.agents[agent_id] = child
            self._register_cycle_agent(state, agent_id)
            task.assigned_agent_id = agent_id
            self._acquire_locks(state, child)
            self._event(state, "spawn_approved", parent.agent_id, {
                "child_agent_id": agent_id,
                "task_id": task.task_id,
                "generation": generation,
                "reason": request.reason,
            })
            return child.model_copy(deep=True)

        _, child = self.store.transaction(mutate)
        return child

    def bind_workspace(self, agent_id: str, *, branch: str, workspace_path: str) -> AgentSpec:
        branch = branch.strip()
        workspace_path = workspace_path.strip()
        if not branch or not workspace_path:
            raise ValidationError("Branch und Workspace-Pfad müssen gesetzt sein")

        def mutate(state: ProjectState) -> AgentSpec:
            agent = self._agent(state, agent_id)
            if agent.status != AgentStatus.ACTIVE:
                raise TransitionError(f"Workspace kann nur an aktiven Agenten gebunden werden: {agent_id}")
            for other in state.agents.values():
                if other.agent_id == agent_id:
                    continue
                if other.branch == branch:
                    raise ConflictError(f"Branch ist bereits gebunden: {branch}")
                if other.workspace_path == workspace_path:
                    raise ConflictError(f"Workspace ist bereits gebunden: {workspace_path}")
            agent.branch = branch
            agent.workspace_path = workspace_path
            agent.updated_at = utcnow()
            self._event(state, "workspace_bound", agent_id, {
                "branch": branch,
                "workspace_path": workspace_path,
            })
            return agent.model_copy(deep=True)

        _, agent = self.store.transaction(mutate)
        return agent

    def unbind_workspace(self, agent_id: str) -> AgentSpec:
        def mutate(state: ProjectState) -> AgentSpec:
            agent = self._agent(state, agent_id)
            previous_branch = agent.branch
            previous_path = agent.workspace_path
            agent.branch = None
            agent.workspace_path = None
            agent.updated_at = utcnow()
            self._event(state, "workspace_unbound", agent_id, {
                "branch": previous_branch,
                "workspace_path": previous_path,
            })
            return agent.model_copy(deep=True)

        _, agent = self.store.transaction(mutate)
        return agent

    def append_worklog(self, agent_id: str, message: str) -> None:
        message = message.strip()
        if not message:
            raise ValidationError("Leerer Worklog-Eintrag")

        def mutate(state: ProjectState) -> None:
            agent = self._agent(state, agent_id)
            agent.worklog.append(message)
            agent.updated_at = utcnow()
            self._event(state, "worklog_appended", agent_id, {"message": message})

        self.store.transaction(mutate)

    def begin_worker_run(self, record: WorkerRunRecord) -> WorkerRunRecord:
        def mutate(state: ProjectState) -> WorkerRunRecord:
            if record.run_id in state.worker_runs:
                raise ConflictError(f"Worker-Run existiert bereits: {record.run_id}")
            agent = self._agent(state, record.agent_id)
            task = self._task(state, record.task_id)
            if agent.task_id != task.task_id or task.assigned_agent_id != agent.agent_id:
                raise ValidationError("Worker-Run passt nicht zur Agent-/Task-Zuweisung")
            if agent.status != AgentStatus.ACTIVE or task.state != TaskState.ACTIVE:
                raise TransitionError("Worker-Run kann nur für einen aktiven Agenten-Task starten")
            if record.base_commit != agent.base_commit or record.base_commit != task.base_commit:
                raise ValidationError("Worker-Run verwendet nicht die bestätigte Agent-/Task-Basis")
            stored = record.model_copy(deep=True)
            stored.state = WorkerRunState.RUNNING
            stored.phase = WorkerPhase.PREPARE
            stored.status_messages = [
                WorkerStatusMessage(
                    sequence=1,
                    phase=WorkerPhase.PREPARE,
                    message=(
                        "Containerisolierter Worker-Run gestartet."
                        if stored.execution_backend.value == "docker"
                        else "Git-Worktree-Worker-Run gestartet."
                    ),
                )
            ]
            state.worker_runs[stored.run_id] = stored
            task.worker_run_id = stored.run_id
            task.updated_at = utcnow()
            self._event(state, "worker_run_started", agent.agent_id, {
                "run_id": stored.run_id,
                "task_id": task.task_id,
                "branch": stored.branch,
                "workspace_path": stored.workspace_path,
                "execution_backend": stored.execution_backend.value,
                "container_image": stored.isolation.image if stored.isolation else None,
            })
            return stored.model_copy(deep=True)

        _, created = self.store.transaction(mutate)
        return created

    def update_worker_run(
        self,
        run_id: str,
        *,
        phase: WorkerPhase,
        message: str,
        implementation_executions: list[TestExecution] | None = None,
        test_executions: list[TestExecution] | None = None,
        review_executions: list[TestExecution] | None = None,
        qa_executions: list[TestExecution] | None = None,
        security_executions: list[TestExecution] | None = None,
        integration_executions: list[TestExecution] | None = None,
        changed_files: list[str] | None = None,
        implementation_commit: str | None = None,
        rebased_from_commit: str | None = None,
        rebased_commit: str | None = None,
        integration_commit: str | None = None,
        integration_isolation: WorkerIsolationSpec | None = None,
        state_value: WorkerRunState | None = None,
    ) -> WorkerRunRecord:
        message = message.strip()
        if not message:
            raise ValidationError("Worker-Statusmeldung darf nicht leer sein")

        def mutate(state: ProjectState) -> WorkerRunRecord:
            try:
                run = state.worker_runs[run_id]
            except KeyError as exc:
                raise ValidationError(f"Unbekannter Worker-Run: {run_id}") from exc
            if run.state == WorkerRunState.INTEGRATED:
                raise TransitionError("Integrierter Worker-Run kann nicht nachträglich fehlschlagen")
            if run.state in {
                WorkerRunState.FAILED,
                WorkerRunState.BLOCKED,
                WorkerRunState.READY_TO_INTEGRATE,
                WorkerRunState.INTEGRATED,
            }:
                raise TransitionError(f"Worker-Run ist bereits abgeschlossen: {run.state.value}")
            run.phase = phase
            run.status_messages.append(WorkerStatusMessage(
                sequence=len(run.status_messages) + 1,
                phase=phase,
                message=message,
            ))
            if implementation_executions is not None:
                run.implementation_executions = list(implementation_executions)
            if test_executions is not None:
                run.test_executions = list(test_executions)
            if review_executions is not None:
                run.review_executions = list(review_executions)
            if qa_executions is not None:
                run.qa_executions = list(qa_executions)
            if security_executions is not None:
                run.security_executions = list(security_executions)
            if integration_executions is not None:
                run.integration_executions = list(integration_executions)
            if changed_files is not None:
                run.changed_files = sorted({normalize_repo_path(path) for path in changed_files})
            if implementation_commit is not None:
                commit = implementation_commit.strip()
                if not commit:
                    raise ValidationError("Leerer Implementierungs-Commit")
                run.implementation_commit = commit
            if rebased_from_commit is not None:
                run.rebased_from_commit = rebased_from_commit.strip() or None
            if rebased_commit is not None:
                run.rebased_commit = rebased_commit.strip() or None
            if integration_commit is not None:
                run.integration_commit = integration_commit.strip() or None
            if integration_isolation is not None:
                run.integration_isolation = integration_isolation.model_copy(deep=True)
            if state_value is not None:
                run.state = state_value
            self._event(state, "worker_run_updated", run.agent_id, {
                "run_id": run_id,
                "phase": phase.value,
                "state": run.state.value,
                "message": message,
            })
            return run.model_copy(deep=True)

        _, updated = self.store.transaction(mutate)
        return updated

    def fail_worker_run(
        self,
        run_id: str,
        *,
        error: str,
        blocked: bool = False,
    ) -> WorkerRunRecord:
        error = error.strip()
        if not error:
            raise ValidationError("Worker-Fehler darf nicht leer sein")

        def mutate(state: ProjectState) -> WorkerRunRecord:
            try:
                run = state.worker_runs[run_id]
            except KeyError as exc:
                raise ValidationError(f"Unbekannter Worker-Run: {run_id}") from exc
            if run.state == WorkerRunState.INTEGRATED:
                raise TransitionError("Integrierter Worker-Run kann nicht nachträglich fehlschlagen")
            task = self._task(state, run.task_id)
            agent = self._agent(state, run.agent_id)
            run.state = WorkerRunState.BLOCKED if blocked else WorkerRunState.FAILED
            run.phase = WorkerPhase.FAILED
            run.error = error
            run.finished_at = utcnow()
            run.status_messages.append(WorkerStatusMessage(
                sequence=len(run.status_messages) + 1,
                phase=WorkerPhase.FAILED,
                message=error,
            ))
            task.state = TaskState.BLOCKED if blocked else TaskState.FAILED
            task.updated_at = utcnow()
            agent.status = AgentStatus.BLOCKED if blocked else AgentStatus.FAILED
            agent.worklog.append(f"Worker-Run {run_id} fehlgeschlagen: {error}")
            agent.updated_at = utcnow()
            if task.task_id not in state.blocked_tasks:
                state.blocked_tasks.append(task.task_id)
            state.open_errors.append(f"{run_id}: {error}")
            state.release_status = "development"
            self._event(state, "worker_run_failed", run.agent_id, {
                "run_id": run_id,
                "task_id": run.task_id,
                "blocked": blocked,
                "error": error,
            })
            return run.model_copy(deep=True)

        _, failed = self.store.transaction(mutate)
        return failed

    def complete_worker_run(self, run_id: str) -> WorkerRunRecord:
        def mutate(state: ProjectState) -> WorkerRunRecord:
            try:
                run = state.worker_runs[run_id]
            except KeyError as exc:
                raise ValidationError(f"Unbekannter Worker-Run: {run_id}") from exc
            task = self._task(state, run.task_id)
            if task.state != TaskState.READY_TO_INTEGRATE:
                raise TransitionError(
                    f"Worker-Run kann vor bestandenen Gates nicht freigegeben werden: {task.state.value}"
                )
            if not run.implementation_commit or task.implementation_commit != run.implementation_commit:
                raise TransitionError("Worker-Run und Task besitzen keinen identischen Implementierungs-Commit")
            run.state = WorkerRunState.READY_TO_INTEGRATE
            run.phase = WorkerPhase.COMPLETE
            run.finished_at = utcnow()
            run.status_messages.append(WorkerStatusMessage(
                sequence=len(run.status_messages) + 1,
                phase=WorkerPhase.COMPLETE,
                message="Review-, QA- und erforderliches Security-Gate bestanden.",
            ))
            self._event(state, "worker_run_ready_to_integrate", run.agent_id, {
                "run_id": run_id,
                "task_id": run.task_id,
                "implementation_commit": run.implementation_commit,
            })
            return run.model_copy(deep=True)

        _, completed = self.store.transaction(mutate)
        return completed

    def begin_worker_integration(
        self,
        run_id: str,
        *,
        expected_project_commit: str,
    ) -> WorkerRunRecord:
        """Reserve a ready run for one serialized, real Git integration attempt."""

        def mutate(state: ProjectState) -> WorkerRunRecord:
            try:
                run = state.worker_runs[run_id]
            except KeyError as exc:
                raise ValidationError(f"Unbekannter Worker-Run: {run_id}") from exc
            task = self._task(state, run.task_id)
            if state.current_commit != expected_project_commit:
                raise ConflictError(
                    "CURRENT_STABLE_COMMIT änderte sich vor Integrationsbeginn: "
                    f"erwartet {expected_project_commit}, aktuell {state.current_commit}"
                )
            if run.state != WorkerRunState.READY_TO_INTEGRATE:
                raise TransitionError(
                    f"Worker-Run ist nicht integrationsbereit: {run.state.value}"
                )
            if task.state != TaskState.READY_TO_INTEGRATE:
                raise TransitionError(f"Task ist nicht integrationsbereit: {task.state.value}")
            if task.worker_run_id != run_id:
                raise ValidationError("Task verweist nicht auf den zu integrierenden Worker-Run")
            if not run.implementation_commit or task.implementation_commit != run.implementation_commit:
                raise TransitionError("Geprüfter Implementierungs-Commit fehlt oder stimmt nicht überein")
            run.state = WorkerRunState.INTEGRATING
            run.phase = WorkerPhase.REBASE
            run.status_messages.append(WorkerStatusMessage(
                sequence=len(run.status_messages) + 1,
                phase=WorkerPhase.REBASE,
                message="Reale Git-Integration exklusiv gestartet.",
            ))
            state.release_status = "integrating"
            self._event(state, "worker_integration_started", "TECH_AI_ORCHESTRATOR", {
                "run_id": run_id,
                "task_id": task.task_id,
                "project_commit": expected_project_commit,
            })
            return run.model_copy(deep=True)

        _, run = self.store.transaction(mutate)
        return run

    def record_worker_rebase(
        self,
        run_id: str,
        *,
        old_base_commit: str,
        new_base_commit: str,
        rebased_commit: str,
        changed_files: list[str],
    ) -> WorkerRunRecord:
        def mutate(state: ProjectState) -> WorkerRunRecord:
            try:
                run = state.worker_runs[run_id]
            except KeyError as exc:
                raise ValidationError(f"Unbekannter Worker-Run: {run_id}") from exc
            if run.state != WorkerRunState.INTEGRATING:
                raise TransitionError("Rebase kann nur während einer laufenden Integration erfasst werden")
            task = self._task(state, run.task_id)
            agent = self._agent(state, run.agent_id)
            if state.current_commit != new_base_commit:
                raise ConflictError("Rebase-Ziel ist nicht mehr CURRENT_STABLE_COMMIT")
            if run.base_commit != old_base_commit or task.base_commit != old_base_commit:
                raise ConflictError("Persistierte Task-Basis änderte sich während des Git-Rebase")
            normalized_files = sorted({normalize_repo_path(path) for path in changed_files})
            if normalized_files != sorted(run.changed_files):
                raise ValidationError(
                    "Rebase veränderte den geprüften Datei-Scope: "
                    f"vorher={sorted(run.changed_files)}, nachher={normalized_files}"
                )
            normalized_commit = rebased_commit.strip()
            if not normalized_commit:
                raise ValidationError("Rebased Commit fehlt")
            run.rebased_from_commit = run.implementation_commit
            run.rebased_commit = normalized_commit
            run.base_commit = new_base_commit
            run.implementation_commit = normalized_commit
            run.phase = WorkerPhase.REBASE
            run.status_messages.append(WorkerStatusMessage(
                sequence=len(run.status_messages) + 1,
                phase=WorkerPhase.REBASE,
                message=(
                    f"Implementierungs-Branch von {old_base_commit} auf "
                    f"{new_base_commit} rebased; neuer Commit {normalized_commit}."
                ),
            ))
            task.base_commit = new_base_commit
            task.implementation_commit = normalized_commit
            task.changed_files = normalized_files
            task.updated_at = utcnow()
            agent.base_commit = new_base_commit
            agent.updated_at = utcnow()
            self._event(state, "worker_branch_rebased", "TECH_AI_ORCHESTRATOR", {
                "run_id": run_id,
                "task_id": task.task_id,
                "old_base_commit": old_base_commit,
                "new_base_commit": new_base_commit,
                "rebased_commit": normalized_commit,
            })
            return run.model_copy(deep=True)

        _, run = self.store.transaction(mutate)
        return run

    def finalize_worker_integration(
        self,
        run_id: str,
        *,
        expected_previous_commit: str,
        integration_commit: str,
        executions: list[TestExecution],
    ) -> WorkerRunRecord:
        """Atomically advance ProjectState after Git and post-merge tests succeeded."""

        def mutate(state: ProjectState) -> WorkerRunRecord:
            try:
                run = state.worker_runs[run_id]
            except KeyError as exc:
                raise ValidationError(f"Unbekannter Worker-Run: {run_id}") from exc
            task = self._task(state, run.task_id)
            agent = self._agent(state, run.agent_id)
            if run.state != WorkerRunState.INTEGRATING:
                raise TransitionError(f"Worker-Run wird nicht integriert: {run.state.value}")
            if task.state != TaskState.READY_TO_INTEGRATE:
                raise TransitionError(f"Task ist nicht integrationsbereit: {task.state.value}")
            if state.current_commit != expected_previous_commit:
                raise ConflictError(
                    "CURRENT_STABLE_COMMIT änderte sich während der Git-Integration"
                )
            normalized_commit = integration_commit.strip()
            if not normalized_commit or normalized_commit == expected_previous_commit:
                raise ValidationError("Integrations-Commit fehlt oder ist unverändert")
            if normalized_commit != run.implementation_commit:
                raise ValidationError(
                    "Fast-Forward-Ziel entspricht nicht dem geprüften Worker-Commit"
                )
            if not executions or not all(execution.passed for execution in executions):
                raise TransitionError("Post-Merge-Gesamttests sind nicht vollständig bestanden")
            run.integration_executions = list(executions)
            run.integration_commit = normalized_commit
            run.state = WorkerRunState.INTEGRATED
            run.phase = WorkerPhase.INTEGRATED
            run.finished_at = utcnow()
            run.status_messages.append(WorkerStatusMessage(
                sequence=len(run.status_messages) + 1,
                phase=WorkerPhase.INTEGRATED,
                message=(
                    f"Fast-Forward nach {state.current_branch} und "
                    f"{len(executions)} Post-Merge-Prüfung(en) bestanden."
                ),
            ))
            task.state = TaskState.INTEGRATED
            task.integration_commit = normalized_commit
            task.updated_at = utcnow()
            agent.status = AgentStatus.MERGED
            agent.worklog.append(f"Task in {state.current_branch} integriert: {normalized_commit}")
            agent.updated_at = utcnow()
            state.current_commit = normalized_commit
            if task.task_id not in state.completed_tasks:
                state.completed_tasks.append(task.task_id)
            state.blocked_tasks = [item for item in state.blocked_tasks if item != task.task_id]
            state.pending_reviews = [item for item in state.pending_reviews if item != task.task_id]
            state.file_locks = [lock for lock in state.file_locks if lock.task_id != task.task_id]
            state.test_status = "passed"
            state.release_status = "stable"
            self._event(state, "worker_task_integrated", "TECH_AI_ORCHESTRATOR", {
                "run_id": run_id,
                "task_id": task.task_id,
                "previous_commit": expected_previous_commit,
                "integration_commit": normalized_commit,
                "tests": [execution.command for execution in executions],
            })
            return run.model_copy(deep=True)

        _, run = self.store.transaction(mutate)
        return run

    def reset_worker_integration(self, run_id: str, *, reason: str) -> WorkerRunRecord:
        """Recover an interrupted attempt before MAIN was durably advanced."""
        reason = reason.strip()
        if not reason:
            raise ValidationError("Recovery-Grund fehlt")

        def mutate(state: ProjectState) -> WorkerRunRecord:
            try:
                run = state.worker_runs[run_id]
            except KeyError as exc:
                raise ValidationError(f"Unbekannter Worker-Run: {run_id}") from exc
            task = self._task(state, run.task_id)
            if run.state != WorkerRunState.INTEGRATING:
                return run.model_copy(deep=True)
            if task.state != TaskState.READY_TO_INTEGRATE:
                raise TransitionError("Unterbrochene Integration kann nicht sicher zurückgesetzt werden")
            run.state = WorkerRunState.READY_TO_INTEGRATE
            run.phase = WorkerPhase.COMPLETE
            run.status_messages.append(WorkerStatusMessage(
                sequence=len(run.status_messages) + 1,
                phase=WorkerPhase.COMPLETE,
                message=f"Unterbrochene Integration zurückgesetzt: {reason}",
            ))
            state.release_status = "development"
            self._event(state, "worker_integration_recovered", "TECH_AI_ORCHESTRATOR", {
                "run_id": run_id,
                "task_id": task.task_id,
                "reason": reason,
            })
            return run.model_copy(deep=True)

        _, run = self.store.transaction(mutate)
        return run

    def submit_task_completion(
        self,
        agent_id: str,
        *,
        implementation_summary: str,
        changed_files: list[str],
        tests: list[TestExecution],
        implementation_commit: str | None = None,
        worker_run_id: str | None = None,
    ) -> TaskSpec:
        def mutate(state: ProjectState) -> TaskSpec:
            agent = self._agent(state, agent_id)
            if agent.status != AgentStatus.ACTIVE:
                raise TransitionError(f"Agent ist nicht aktiv: {agent_id}")
            task = self._task(state, agent.task_id)
            if task.assigned_agent_id != agent_id or task.state != TaskState.ACTIVE:
                raise TransitionError(f"Task {task.task_id} ist nicht aktiv bei {agent_id}")
            normalized_files = [normalize_repo_path(path) for path in changed_files]
            for path in normalized_files:
                if not any(path_matches_scope(path, scope) for scope in agent.allowed_paths):
                    raise ValidationError(f"Datei außerhalb des Schreibbereichs: {path}")
                if any(path_matches_scope(path, scope) for scope in agent.denied_paths):
                    raise ValidationError(f"Datei liegt in gesperrtem Bereich: {path}")
            failed = [test.command for test in tests if not test.passed]
            if failed:
                raise TransitionError(f"Fehlgeschlagene Agent-Tests: {', '.join(failed)}")
            executed_commands = {test.command for test in tests if test.passed}
            missing = [command for command in task.required_tests if command not in executed_commands]
            if missing:
                raise TransitionError(f"Erforderliche Tests fehlen: {', '.join(missing)}")

            summary = implementation_summary.strip()
            if not summary:
                raise ValidationError("Implementierungszusammenfassung fehlt")
            if implementation_commit is not None and not implementation_commit.strip():
                raise ValidationError("Implementierungs-Commit ist leer")
            if worker_run_id is not None:
                run = state.worker_runs.get(worker_run_id)
                if run is None or run.agent_id != agent_id or run.task_id != task.task_id:
                    raise ValidationError("Worker-Run passt nicht zur Task-Einreichung")
                if run.state != WorkerRunState.RUNNING:
                    raise TransitionError(f"Worker-Run ist nicht aktiv: {run.state.value}")
            task.implementation_summary = summary
            task.changed_files = sorted(set(normalized_files))
            task.implementation_commit = implementation_commit.strip() if implementation_commit else None
            task.worker_run_id = worker_run_id or task.worker_run_id
            task.test_executions = list(tests)
            task.state = TaskState.REVIEW_PENDING
            task.updated_at = utcnow()
            agent.test_results.extend(tests)
            agent.status = AgentStatus.WAITING_FOR_REVIEW
            agent.updated_at = utcnow()
            if task.task_id not in state.pending_reviews:
                state.pending_reviews.append(task.task_id)
            self._event(state, "task_submitted_for_review", agent_id, {
                "task_id": task.task_id,
                "changed_files": normalized_files,
                "tests": [test.command for test in tests],
                "implementation_commit": task.implementation_commit,
                "worker_run_id": task.worker_run_id,
            })
            return task.model_copy(deep=True)

        _, task = self.store.transaction(mutate)
        return task

    def assign_reviewer(self, task_id: str, reviewer_agent_id: str) -> None:
        def mutate(state: ProjectState) -> None:
            task = self._task(state, task_id)
            reviewer = self._agent(state, reviewer_agent_id)
            if task.state != TaskState.REVIEW_PENDING:
                raise TransitionError(f"Task ist nicht review_pending: {task_id}")
            if reviewer.role not in self._REVIEW_ROLES:
                raise ValidationError(f"Agent {reviewer_agent_id} ist kein Reviewer")
            if reviewer_agent_id == task.assigned_agent_id:
                raise ValidationError("Implementierender Agent darf nicht selbst reviewen")
            if reviewer.status != AgentStatus.ACTIVE:
                raise TransitionError("Reviewer ist nicht aktiv")
            task.reviewer_agent_id = reviewer_agent_id
            task.updated_at = utcnow()
            self._event(state, "reviewer_assigned", reviewer_agent_id, {"task_id": task_id})

        self.store.transaction(mutate)

    def record_review(
        self,
        task_id: str,
        reviewer_agent_id: str,
        *,
        approved: bool,
        notes: str,
    ) -> TaskSpec:
        def mutate(state: ProjectState) -> TaskSpec:
            task = self._task(state, task_id)
            if task.reviewer_agent_id != reviewer_agent_id:
                raise ValidationError("Reviewer ist diesem Task nicht zugewiesen")
            if task.state != TaskState.REVIEW_PENDING:
                raise TransitionError(f"Task ist nicht review_pending: {task_id}")
            task.review_decision = (
                ReviewDecision.APPROVED if approved else ReviewDecision.REJECTED
            )
            task.review_notes = notes.strip()
            task.updated_at = utcnow()
            if approved:
                task.state = TaskState.QA_PENDING
            else:
                task.state = TaskState.BLOCKED
                if task.assigned_agent_id:
                    state.agents[task.assigned_agent_id].status = AgentStatus.REJECTED
                    state.agents[task.assigned_agent_id].updated_at = utcnow()
                if task_id not in state.blocked_tasks:
                    state.blocked_tasks.append(task_id)
            state.pending_reviews = [item for item in state.pending_reviews if item != task_id]
            self._event(state, "review_recorded", reviewer_agent_id, {
                "task_id": task_id,
                "approved": approved,
            })
            return task.model_copy(deep=True)

        _, task = self.store.transaction(mutate)
        return task

    def assign_qa(self, task_id: str, qa_agent_id: str) -> None:
        def mutate(state: ProjectState) -> None:
            task = self._task(state, task_id)
            qa = self._agent(state, qa_agent_id)
            if task.state != TaskState.QA_PENDING:
                raise TransitionError(f"Task ist nicht qa_pending: {task_id}")
            if qa.role not in self._QA_ROLES:
                raise ValidationError(f"Agent {qa_agent_id} ist kein QA-Agent")
            if qa_agent_id == task.assigned_agent_id:
                raise ValidationError("Implementierender Agent darf nicht sein eigener QA-Agent sein")
            if qa.status != AgentStatus.ACTIVE:
                raise TransitionError("QA-Agent ist nicht aktiv")
            task.qa_agent_id = qa_agent_id
            task.updated_at = utcnow()
            self._event(state, "qa_assigned", qa_agent_id, {"task_id": task_id})

        self.store.transaction(mutate)

    def record_qa(
        self,
        task_id: str,
        qa_agent_id: str,
        *,
        executions: list[TestExecution],
    ) -> TaskSpec:
        def mutate(state: ProjectState) -> TaskSpec:
            task = self._task(state, task_id)
            if task.qa_agent_id != qa_agent_id:
                raise ValidationError("QA-Agent ist diesem Task nicht zugewiesen")
            if task.state != TaskState.QA_PENDING:
                raise TransitionError(f"Task ist nicht qa_pending: {task_id}")
            task.qa_executions = list(executions)
            passed = bool(executions) and all(execution.passed for execution in executions)
            task.qa_status = QAStatus.PASSED if passed else QAStatus.FAILED
            task.updated_at = utcnow()
            state.test_status = "passed" if passed else "failed"
            if passed:
                self._refresh_ready_state(task)
            else:
                task.state = TaskState.BLOCKED
                if task_id not in state.blocked_tasks:
                    state.blocked_tasks.append(task_id)
            self._event(state, "qa_recorded", qa_agent_id, {
                "task_id": task_id,
                "passed": passed,
                "commands": [execution.command for execution in executions],
            })
            return task.model_copy(deep=True)

        _, task = self.store.transaction(mutate)
        return task

    def record_security_review(
        self,
        task_id: str,
        security_agent_id: str,
        *,
        approved: bool,
        notes: str,
    ) -> TaskSpec:
        def mutate(state: ProjectState) -> TaskSpec:
            task = self._task(state, task_id)
            security = self._agent(state, security_agent_id)
            if security.role not in self._SECURITY_ROLES:
                raise ValidationError(f"Agent {security_agent_id} ist kein Security-Agent")
            if security.status != AgentStatus.ACTIVE:
                raise TransitionError("Security-Agent ist nicht aktiv")
            if security_agent_id == task.assigned_agent_id:
                raise ValidationError("Implementierender Agent darf nicht selbst Security freigeben")
            if task.state not in {TaskState.REVIEW_PENDING, TaskState.QA_PENDING}:
                raise TransitionError("Security-Review ist in diesem Task-Zustand nicht zulässig")
            task.security_agent_id = security_agent_id
            task.security_approved = approved
            task.security_notes = notes.strip()
            task.updated_at = utcnow()
            if not approved:
                task.state = TaskState.BLOCKED
                if task_id not in state.blocked_tasks:
                    state.blocked_tasks.append(task_id)
            else:
                self._refresh_ready_state(task)
            self._event(state, "security_review_recorded", security_agent_id, {
                "task_id": task_id,
                "approved": approved,
            })
            return task.model_copy(deep=True)

        _, task = self.store.transaction(mutate)
        return task

    def reopen_task(self, task_id: str, *, reason: str) -> TaskSpec:
        reason = reason.strip()
        if not reason:
            raise ValidationError("Grund für die Wiedereröffnung fehlt")

        def mutate(state: ProjectState) -> TaskSpec:
            task = self._task(state, task_id)
            if task.state != TaskState.BLOCKED:
                raise TransitionError(f"Nur blockierte Tasks können wiedereröffnet werden: {task.state.value}")
            if not task.assigned_agent_id:
                raise TransitionError("Task besitzt keinen implementierenden Agenten")
            agent = self._agent(state, task.assigned_agent_id)
            if agent.status not in {
                AgentStatus.COMPLETED,
                AgentStatus.BLOCKED,
                AgentStatus.REJECTED,
                AgentStatus.WAITING_FOR_REVIEW,
            }:
                raise TransitionError(f"Agent kann nicht reaktiviert werden: {agent.status.value}")
            if task.base_commit != state.current_commit:
                if task.implementation_commit:
                    raise TransitionError(
                        "Worker-Task benötigt vor der Nacharbeit ein reales Git-Rebase; "
                        "reine Zustandsänderung ist gesperrt"
                    )
                previous_base = task.base_commit
                task.base_commit = state.current_commit
                agent.base_commit = state.current_commit
                self._event(state, "task_rebased_for_rework", "TECH_AI_ORCHESTRATOR", {
                    "task_id": task_id,
                    "previous_base_commit": previous_base,
                    "new_base_commit": state.current_commit,
                })
            task.state = TaskState.ACTIVE
            task.implementation_summary = ""
            task.changed_files = []
            task.implementation_commit = None
            task.test_executions = []
            task.worker_run_id = None
            task.review_decision = ReviewDecision.PENDING
            task.review_notes = ""
            task.qa_status = QAStatus.PENDING
            task.qa_executions = []
            task.security_approved = None
            task.security_notes = ""
            task.reviewer_agent_id = None
            task.qa_agent_id = None
            task.security_agent_id = None
            task.updated_at = utcnow()
            agent.status = AgentStatus.ACTIVE
            agent.worklog.append(f"Rework eröffnet: {reason}")
            agent.updated_at = utcnow()
            state.blocked_tasks = [item for item in state.blocked_tasks if item != task_id]
            state.pending_reviews = [item for item in state.pending_reviews if item != task_id]
            self._event(state, "task_reopened", "TECH_AI_ORCHESTRATOR", {
                "task_id": task_id,
                "agent_id": agent.agent_id,
                "reason": reason,
            })
            return task.model_copy(deep=True)

        _, task = self.store.transaction(mutate)
        return task

    def cancel_task(self, task_id: str, *, reason: str) -> TaskSpec:
        reason = reason.strip()
        if not reason:
            raise ValidationError("Grund für den Abbruch fehlt")

        def mutate(state: ProjectState) -> TaskSpec:
            task = self._task(state, task_id)
            if task.state == TaskState.INTEGRATED:
                raise TransitionError("Integrierter Task kann nicht abgebrochen werden")
            task.state = TaskState.CANCELLED
            task.updated_at = utcnow()
            if task.assigned_agent_id:
                agent = self._agent(state, task.assigned_agent_id)
                agent.status = AgentStatus.TERMINATED
                agent.worklog.append(f"Task abgebrochen: {reason}")
                agent.updated_at = utcnow()
            state.file_locks = [lock for lock in state.file_locks if lock.task_id != task_id]
            state.pending_reviews = [item for item in state.pending_reviews if item != task_id]
            state.blocked_tasks = [item for item in state.blocked_tasks if item != task_id]
            self._event(state, "task_cancelled", "TECH_AI_ORCHESTRATOR", {
                "task_id": task_id,
                "reason": reason,
            })
            return task.model_copy(deep=True)

        _, task = self.store.transaction(mutate)
        return task

    def rebase_task(self, task_id: str, *, new_base_commit: str) -> TaskSpec:
        def mutate(state: ProjectState) -> TaskSpec:
            task = self._task(state, task_id)
            if new_base_commit != state.current_commit:
                raise ValidationError("Rebase-Ziel muss CURRENT_STABLE_COMMIT sein")
            if task.implementation_commit:
                raise TransitionError(
                    "Worker-Task kann nicht durch reine Zustandsänderung rebased werden; "
                    "Git-Branch und Commit müssen real rebased und erneut geprüft werden"
                )
            if task.state not in {
                TaskState.ACTIVE,
                TaskState.REVIEW_PENDING,
                TaskState.QA_PENDING,
                TaskState.READY_TO_INTEGRATE,
            }:
                raise TransitionError(f"Task kann in Zustand {task.state.value} nicht rebased werden")
            task.base_commit = new_base_commit
            task.updated_at = utcnow()
            if task.assigned_agent_id:
                state.agents[task.assigned_agent_id].base_commit = new_base_commit
                state.agents[task.assigned_agent_id].updated_at = utcnow()
            self._event(state, "task_rebased", "TECH_AI_ORCHESTRATOR", {
                "task_id": task_id,
                "new_base_commit": new_base_commit,
            })
            return task.model_copy(deep=True)

        _, task = self.store.transaction(mutate)
        return task

    def integrate_task(self, task_id: str, *, new_commit: str) -> TaskSpec:
        def mutate(state: ProjectState) -> TaskSpec:
            task = self._task(state, task_id)
            if task.state != TaskState.READY_TO_INTEGRATE:
                raise TransitionError(f"Task ist nicht integrationsbereit: {task.state.value}")
            if task.worker_run_id:
                raise TransitionError(
                    "Worker-Task benötigt eine reale Git-Integration; reine Zustandsfreigabe ist gesperrt"
                )
            if task.base_commit != state.current_commit:
                raise TransitionError(
                    f"Task basiert auf {task.base_commit}, MAIN steht auf {state.current_commit}; Rebase erforderlich"
                )
            if not new_commit.strip() or new_commit == state.current_commit:
                raise ValidationError("Neuer Commit fehlt oder ist unverändert")
            normalized_commit = new_commit.strip()
            if task.implementation_commit and normalized_commit != task.implementation_commit:
                raise ValidationError(
                    "Integrations-Commit entspricht nicht dem geprüften Implementierungs-Commit"
                )
            task.state = TaskState.INTEGRATED
            task.integration_commit = normalized_commit
            task.updated_at = utcnow()
            if task.assigned_agent_id:
                agent = self._agent(state, task.assigned_agent_id)
                agent.status = AgentStatus.MERGED
                agent.updated_at = utcnow()
            state.current_commit = normalized_commit
            state.completed_tasks.append(task_id)
            state.blocked_tasks = [item for item in state.blocked_tasks if item != task_id]
            state.file_locks = [lock for lock in state.file_locks if lock.task_id != task_id]
            state.release_status = "stable"
            self._event(state, "task_integrated", "TECH_AI_ORCHESTRATOR", {
                "task_id": task_id,
                "new_commit": normalized_commit,
            })
            return task.model_copy(deep=True)

        _, task = self.store.transaction(mutate)
        return task

    def graph_order(self) -> list[str]:
        return task_graph_order(self.state().tasks)

    def _refresh_ready_state(self, task: TaskSpec) -> None:
        security_ok = not task.requires_security_review or task.security_approved is True
        if (
            task.review_decision == ReviewDecision.APPROVED
            and task.qa_status == QAStatus.PASSED
            and security_ok
        ):
            task.state = TaskState.READY_TO_INTEGRATE


    @staticmethod
    def _validate_capability_task(state: ProjectState, task: TaskSpec) -> None:
        if task.capability_id is None:
            if task.capability_action is not None:
                raise ValidationError("Capability-Aktion benötigt capability_id")
            return
        capability = state.capabilities.get(task.capability_id)
        if capability is None:
            raise ValidationError(f"Unbekannte Capability: {task.capability_id}")
        if task.capability_action is None:
            raise ValidationError("Capability-Task benötigt capability_action")
        if task.capability_action == CapabilityAction.CREATE and capability.status != CapabilityStatus.NOT_STARTED:
            raise ConflictError("CREATE ist nur bei NOT_STARTED zulässig")
        if task.capability_action != CapabilityAction.CREATE and capability.status == CapabilityStatus.NOT_STARTED:
            raise ConflictError("NOT_STARTED-Capability erfordert CREATE")
        active_states = {TaskState.ACTIVE, TaskState.REVIEW_PENDING, TaskState.QA_PENDING, TaskState.READY_TO_INTEGRATE}
        for existing in state.tasks.values():
            if existing.capability_id == task.capability_id and existing.state in active_states:
                raise ConflictError(
                    f"Capability {task.capability_id} wird bereits aktiv durch {existing.task_id} bearbeitet"
                )

    def register_capability(self, capability: CapabilitySpec) -> CapabilitySpec:
        def mutate(state: ProjectState) -> CapabilitySpec:
            if capability.capability_id in state.capabilities:
                raise ConflictError(f"Capability existiert bereits: {capability.capability_id}")
            for dependency in capability.dependencies:
                if dependency not in state.capabilities:
                    raise ValidationError(f"Unbekannte Capability-Abhängigkeit: {dependency}")
            state.capabilities[capability.capability_id] = capability.model_copy(deep=True)
            self._event(state, "capability_registered", "TECH_AI_ORCHESTRATOR", {
                "capability_id": capability.capability_id,
                "module_id": capability.module_id,
                "status": capability.status.value,
            })
            return capability.model_copy(deep=True)
        _, created = self.store.transaction(mutate)
        return created

    def set_capability_status(
        self,
        capability_id: str,
        status: CapabilityStatus,
        *,
        owner_agent_id: str | None = None,
        source_ref: str | None = None,
    ) -> CapabilitySpec:
        def mutate(state: ProjectState) -> CapabilitySpec:
            capability = state.capabilities.get(capability_id)
            if capability is None:
                raise ValidationError(f"Unbekannte Capability: {capability_id}")
            if owner_agent_id is not None and owner_agent_id not in state.agents:
                raise ValidationError(f"Unbekannter Owner-Agent: {owner_agent_id}")
            capability.status = status
            if owner_agent_id is not None:
                capability.owner_agent_id = owner_agent_id
            if source_ref is not None:
                capability.source_ref = source_ref
            self._event(state, "capability_status_changed", owner_agent_id or "TECH_AI_ORCHESTRATOR", {
                "capability_id": capability_id,
                "status": status.value,
                "source_ref": capability.source_ref,
            })
            return capability.model_copy(deep=True)
        _, updated = self.store.transaction(mutate)
        return updated

    def _assert_dependencies_integrated(self, state: ProjectState, task: TaskSpec) -> None:
        blocked = [
            dependency
            for dependency in task.dependencies
            if state.tasks[dependency].state != TaskState.INTEGRATED
        ]
        if blocked:
            raise TransitionError(f"Nicht integrierte Abhängigkeiten: {', '.join(blocked)}")

    def _assert_capacity(self, state: ProjectState) -> None:
        active = sum(
            agent.status in self._NON_TERMINAL_AGENT_STATUSES
            for agent in state.agents.values()
        )
        if active >= state.governance.max_active_agents:
            raise ValidationError(
                f"MAX_ACTIVE_AGENTS erreicht: {state.governance.max_active_agents}"
            )
        if len(state.cycle_agent_ids) >= state.governance.max_total_agents_per_cycle:
            raise ValidationError(
                "MAX_TOTAL_AGENTS_PER_CYCLE erreicht: "
                f"{state.governance.max_total_agents_per_cycle}"
            )

    def _assert_module_capacity(self, state: ProjectState, task: TaskSpec) -> None:
        modules = self._task_modules(task)
        if not modules:
            return
        counts: dict[str, int] = defaultdict(int)
        for agent in state.agents.values():
            if agent.status not in self._NON_TERMINAL_AGENT_STATUSES:
                continue
            existing_task = state.tasks.get(agent.task_id)
            if existing_task is None:
                continue
            for module in self._task_modules(existing_task):
                counts[module] += 1
        saturated = [
            module
            for module in modules
            if counts[module] >= state.governance.max_agents_per_module
        ]
        if saturated:
            raise ValidationError(
                "MAX_AGENTS_PER_MODULE erreicht: " + ", ".join(sorted(saturated))
            )

    @staticmethod
    def _task_modules(task: TaskSpec) -> set[str]:
        modules: set[str] = set()
        for component in task.affected_components:
            normalized = component.strip().replace("\\", "/").strip("/")
            if normalized:
                modules.add(normalized.casefold())
        if modules:
            return modules
        for scope in task.allowed_paths:
            anchor, _, _ = _scope_anchor(scope)
            if anchor:
                parts = PurePosixPath(anchor).parts
                modules.add("/".join(parts[:2]).casefold())
        return modules

    @staticmethod
    def _register_cycle_agent(state: ProjectState, agent_id: str) -> None:
        if agent_id in state.cycle_agent_ids:
            raise ConflictError(f"Agent ist im Zyklus bereits registriert: {agent_id}")
        state.cycle_agent_ids.append(agent_id)

    def _assert_scopes_available(self, state: ProjectState, scopes: Iterable[str]) -> None:
        for scope in scopes:
            for lock in state.file_locks:
                if scopes_overlap(scope, lock.scope):
                    raise ConflictError(
                        f"Schreibbereich {scope} kollidiert mit {lock.scope} "
                        f"von {lock.agent_id}/{lock.task_id}"
                    )

    def _acquire_locks(self, state: ProjectState, agent: AgentSpec) -> None:
        for scope in agent.allowed_paths:
            state.file_locks.append(
                FileLock(scope=scope, agent_id=agent.agent_id, task_id=agent.task_id)
            )

    @staticmethod
    def _normalize_scopes(scopes: Iterable[str]) -> list[str]:
        out: list[str] = []
        for scope in scopes:
            normalized = normalize_repo_path(scope)
            if normalized not in out:
                out.append(normalized)
        return out

    @staticmethod
    def _task(state: ProjectState, task_id: str) -> TaskSpec:
        try:
            return state.tasks[task_id]
        except KeyError as exc:
            raise ValidationError(f"Unbekannter Task: {task_id}") from exc

    @staticmethod
    def _agent(state: ProjectState, agent_id: str) -> AgentSpec:
        try:
            return state.agents[agent_id]
        except KeyError as exc:
            raise ValidationError(f"Unbekannter Agent: {agent_id}") from exc

    @staticmethod
    def _next_agent_id(state: ProjectState, role: DevelopmentRole) -> str:
        prefix = f"AGENT_{role.value.upper()}_"
        numbers = []
        for agent_id in state.agents:
            if agent_id.startswith(prefix):
                suffix = agent_id[len(prefix):]
                if suffix.isdigit():
                    numbers.append(int(suffix))
        return f"{prefix}{max(numbers, default=0) + 1:02d}"

    @staticmethod
    def _event(
        state: ProjectState,
        event_type: str,
        actor_id: str,
        details: dict,
    ) -> None:
        state.audit_log.append(
            AuditEvent(
                sequence=len(state.audit_log) + 1,
                event_type=event_type,
                actor_id=actor_id,
                details=details,
            )
        )
