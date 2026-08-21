"""Bounded parallel execution for independent programming-agent pipelines.

The pool does not invent agents or tasks. Every entry must reference an already
approved active programming agent with a disjoint write scope. Each pipeline
uses its own GitWorkspaceManager instance while the persistent orchestrator
serializes state mutations.
"""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from .container_runtime import DockerCommandExecutor
from .git_workspace import GitWorkspaceManager
from .models import (
    AgentStatus,
    WorkerPipelineJob,
    WorkerPoolFailurePolicy,
    WorkerPoolJob,
)
from .orchestrator import (
    DevelopmentOrchestrator,
    ValidationError,
    scopes_overlap,
)
from .worker import WorkerPipelineResult, WorkerPipelineRunner


class WorkerPoolError(RuntimeError):
    """Invalid pool definition or a failure before a pipeline could start."""


@dataclass(frozen=True)
class WorkerPoolResult:
    completed: dict[str, WorkerPipelineResult] = field(default_factory=dict)
    failures: dict[str, str] = field(default_factory=dict)
    cancelled: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return not self.failures and not self.cancelled


class WorkerPoolRunner:
    """Run up to twelve approved coding agents concurrently.

    Integration remains deliberately separate. Parallel workers can submit
    reviewed branches, but MAIN is still changed only by the serialized
    integration runner.
    """

    def __init__(
        self,
        orchestrator: DevelopmentOrchestrator,
        workspace_manager: GitWorkspaceManager,
        *,
        container_executor: DockerCommandExecutor | None = None,
        require_container_isolation: bool = False,
    ) -> None:
        self.orchestrator = orchestrator
        self.workspace_manager = workspace_manager
        self.container_executor = container_executor
        self.require_container_isolation = require_container_isolation

    def run(self, job: WorkerPoolJob) -> WorkerPoolResult:
        self._preflight(job)
        completed: dict[str, WorkerPipelineResult] = {}
        failures: dict[str, str] = {}
        cancelled: list[str] = []
        def execute(pipeline: WorkerPipelineJob) -> WorkerPipelineResult:
            # Separate manager objects prevent accidental mutable cross-talk.
            manager = GitWorkspaceManager(
                self.workspace_manager.repository,
                self.workspace_manager.workspace_root,
                command_timeout_seconds=self.workspace_manager.command_timeout_seconds,
            )
            return WorkerPipelineRunner(
                self.orchestrator,
                manager,
                container_executor=self.container_executor,
                require_container_isolation=self.require_container_isolation,
            ).run(pipeline)

        futures: dict[Future[WorkerPipelineResult], str] = {}
        with ThreadPoolExecutor(
            max_workers=job.max_parallel,
            thread_name_prefix="tankai-coder",
        ) as pool:
            for pipeline in job.pipelines:
                future = pool.submit(execute, pipeline)
                futures[future] = pipeline.worker.agent_id

            stop_requested = False
            for future in as_completed(futures):
                agent_id = futures[future]
                if future.cancelled():
                    cancelled.append(agent_id)
                    continue
                try:
                    result = future.result()
                except Exception as exc:  # individual runner persists its failure
                    failures[agent_id] = str(exc)
                    if job.failure_policy == WorkerPoolFailurePolicy.STOP_SCHEDULING:
                        stop_requested = True
                        for pending, pending_agent in futures.items():
                            if pending is future or pending.done():
                                continue
                            if pending.cancel():
                                cancelled.append(pending_agent)
                else:
                    completed[agent_id] = result
                if stop_requested:
                    # Running workers cannot be killed safely here; they finish and
                    # are still collected. Only jobs not started by the executor are cancelled.
                    continue

        return WorkerPoolResult(
            completed=dict(sorted(completed.items())),
            failures=dict(sorted(failures.items())),
            cancelled=tuple(sorted(set(cancelled))),
        )

    def _preflight(self, job: WorkerPoolJob) -> None:
        state = self.orchestrator.state()
        scopes: list[tuple[str, str]] = []
        for pipeline in job.pipelines:
            agent_id = pipeline.worker.agent_id
            agent = state.agents.get(agent_id)
            if agent is None:
                raise WorkerPoolError(f"Unbekannter Programmier-Agent: {agent_id}")
            if agent.status != AgentStatus.ACTIVE:
                raise WorkerPoolError(f"Programmier-Agent ist nicht aktiv: {agent_id}")
            task = state.tasks.get(agent.task_id)
            if task is None or task.assigned_agent_id != agent_id:
                raise WorkerPoolError(
                    f"Agent {agent_id} besitzt keine gültige exklusive Task-Zuweisung"
                )
            if not agent.allowed_paths:
                raise WorkerPoolError(
                    f"Programmier-Agent {agent_id} besitzt keinen Schreibbereich"
                )
            for scope in agent.allowed_paths:
                scopes.append((agent_id, scope))

        for index, (first_agent, first_scope) in enumerate(scopes):
            for second_agent, second_scope in scopes[index + 1 :]:
                if first_agent == second_agent:
                    continue
                if scopes_overlap(first_scope, second_scope):
                    raise ValidationError(
                        "Parallele Programmier-Agenten besitzen überlappende Schreibbereiche: "
                        f"{first_agent}:{first_scope} <-> {second_agent}:{second_scope}"
                    )
