#!/usr/bin/env python3
"""CLI for the persistent controlled development orchestrator."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from pydantic import ValidationError as PydanticValidationError

from .container_runtime import DockerCommandExecutor
from .git_workspace import GitWorkspaceManager
from .models import (
    DevelopmentRole,
    IntegrationJob,
    SpawnRequest,
    TaskSpec,
    WorkerPipelineJob,
    WorkerPoolJob,
)
from .orchestrator import DevelopmentOrchestrator, OrchestrationError
from .state_store import ProjectStateStore
from .worker import WorkerExecutionError, WorkerPipelineRunner
from .pool import WorkerPoolError, WorkerPoolRunner
from .integration import IntegrationExecutionError, WorkerIntegrationRunner


def _state_summary(orchestrator: DevelopmentOrchestrator) -> dict:
    state = orchestrator.state()
    return {
        "schema_version": state.schema_version,
        "revision": state.revision,
        "version": state.current_version,
        "branch": state.current_branch,
        "commit": state.current_commit,
        "architecture_status": state.architecture_status,
        "release_status": state.release_status,
        "governance": state.governance.model_dump(mode="json"),
        "cycle": {
            "cycle_id": state.cycle_id,
            "sequence": state.cycle_sequence,
            "agent_count": len(state.cycle_agent_ids),
            "agent_ids": list(state.cycle_agent_ids),
        },
        "active_agents": [
            {
                "agent_id": agent.agent_id,
                "role": agent.role.value,
                "status": agent.status.value,
                "task_id": agent.task_id,
                "generation": agent.generation,
                "cycle_id": agent.cycle_id,
            }
            for agent in state.agents.values()
            if agent.status in DevelopmentOrchestrator._NON_TERMINAL_AGENT_STATUSES
        ],
        "tasks": {task_id: task.state.value for task_id, task in state.tasks.items()},
        "worker_runs": {
            run_id: {
                "state": run.state.value,
                "phase": run.phase.value,
                "task_id": run.task_id,
                "agent_id": run.agent_id,
                "execution_backend": run.execution_backend.value,
                "container_image": run.isolation.image if run.isolation else None,
                "implementation_commit": run.implementation_commit,
                "rebased_commit": run.rebased_commit,
                "integration_commit": run.integration_commit,
            }
            for run_id, run in state.worker_runs.items()
        },
        "file_locks": [lock.model_dump(mode="json") for lock in state.file_locks],
        "task_graph": orchestrator.graph_order(),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tankai-orchestrator")
    parser.add_argument("--state", default=".tankai/project-state.json")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="Initialisiert die zentrale Projektquelle")
    init.add_argument("--version", required=True)
    init.add_argument("--branch", default="main")
    init.add_argument("--commit", required=True)
    init.add_argument("--max-active-agents", type=int, default=40)
    init.add_argument("--max-total-agents-per-cycle", type=int, default=80)
    init.add_argument("--max-clone-depth", type=int, default=5)
    init.add_argument("--max-children-per-agent", type=int, default=3)
    init.add_argument("--max-agents-per-module", type=int, default=4)

    sub.add_parser("status", help="Zeigt den persistenten Orchestrator-Zustand")
    cycle = sub.add_parser("begin-cycle", help="Startet einen neuen kontrollierten Agentenzyklus")
    cycle.add_argument("--reason", required=True)
    sub.add_parser("demo", help="Erzeugt einen kontrollierten Spawn mit getrennten Pfaden")

    sub.add_parser("code-proposal-schema", help="Zeigt das Schema für Agenten-Dateivorschläge")
    prepare = sub.add_parser("prepare-code-proposal", help="Bereitet einen Datei-Vorschlag als Worker-Job vor")
    prepare.add_argument("--agent", required=True)
    prepare.add_argument("--proposal", required=True)
    prepare.add_argument("--checks", required=True, help="Operatorseitige Tests, Gates und Isolation")
    prepare.add_argument("--output", required=True, help="Neuer Pfad für den vorbereiteten Job")
    prepare.add_argument("--python-executable", default="python3")

    sub.add_parser("code-response-schema", help="Zeigt das Schema gebundener Agentenantworten")
    request = sub.add_parser("prepare-code-request", help="Bereitet Ziel und festen Codekontext vor")
    request.add_argument("--agent", required=True)
    request.add_argument("--repository", required=True)
    request.add_argument("--context-file", action="append", required=True)
    request.add_argument("--checks", required=True)
    request.add_argument("--data-class", choices=("public", "internal"), default="internal")
    request.add_argument("--output", required=True)

    response = sub.add_parser("prepare-code-response", help="Prüft die Antwort auf einen Codeauftrag")
    response.add_argument("--agent", required=True)
    response.add_argument("--repository", required=True)
    response.add_argument("--request", required=True)
    response.add_argument("--response", required=True)
    response.add_argument("--checks", required=True)
    response.add_argument("--python-executable", default="python3")
    response.add_argument("--output", required=True)

    run = sub.add_parser(
        "run-pipeline",
        help="Führt einen genehmigten Worker-Job in einem isolierten Git-Worktree aus",
    )
    run.add_argument("--repository", required=True)
    run.add_argument("--workspace-root", required=True)
    run.add_argument("--job", required=True, help="JSON-Datei mit WorkerPipelineJob")
    run.add_argument(
        "--container-runtime",
        default=os.environ.get("TANKAI_WORKER_CONTAINER_RUNTIME", "docker"),
    )
    run.add_argument(
        "--require-container-isolation",
        action="store_true",
        default=os.environ.get("TANKAI_REQUIRE_WORKER_ISOLATION", "0") == "1",
    )

    pool = sub.add_parser(
        "run-pool",
        help="Führt mehrere konfliktfreie Programmier-Agenten parallel aus",
    )
    pool.add_argument("--repository", required=True)
    pool.add_argument("--workspace-root", required=True)
    pool.add_argument("--job", required=True, help="JSON-Datei mit WorkerPoolJob")
    pool.add_argument(
        "--container-runtime",
        default=os.environ.get("TANKAI_WORKER_CONTAINER_RUNTIME", "docker"),
    )
    pool.add_argument(
        "--require-container-isolation",
        action="store_true",
        default=os.environ.get("TANKAI_REQUIRE_WORKER_ISOLATION", "0") == "1",
    )

    integrate = sub.add_parser(
        "integrate",
        help="Rebased, merged und testet einen freigegebenen Worker-Run auf MAIN",
    )
    integrate.add_argument("--repository", required=True)
    integrate.add_argument("--workspace-root", required=True)
    integrate.add_argument("--job", required=True, help="JSON-Datei mit IntegrationJob")
    integrate.add_argument(
        "--container-runtime",
        default=os.environ.get("TANKAI_WORKER_CONTAINER_RUNTIME", "docker"),
    )
    integrate.add_argument(
        "--require-container-isolation",
        action="store_true",
        default=os.environ.get("TANKAI_REQUIRE_WORKER_ISOLATION", "0") == "1",
    )
    return parser


def _load_pipeline_job(path: str) -> WorkerPipelineJob:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Worker-Job ist nicht lesbar: {path}: {exc}") from exc
    return WorkerPipelineJob.model_validate(payload)


def _load_pool_job(path: str) -> WorkerPoolJob:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Worker-Pool-Job ist nicht lesbar: {path}: {exc}") from exc
    return WorkerPoolJob.model_validate(payload)


def _load_integration_job(path: str) -> IntegrationJob:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Integrations-Job ist nicht lesbar: {path}: {exc}") from exc
    return IntegrationJob.model_validate(payload)


def _write_new_json(path: str, payload: dict) -> None:
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "code-proposal-schema":
            from .code_proposal import CodeProposal
            print(json.dumps(CodeProposal.model_json_schema(), ensure_ascii=False, indent=2))
            return 0
        if args.command == "code-response-schema":
            from .code_request import CodeResponse
            print(json.dumps(CodeResponse.model_json_schema(), ensure_ascii=False, indent=2))
            return 0
        if args.command == "init":
            orchestrator = DevelopmentOrchestrator.initialize(
                args.state,
                current_version=args.version,
                current_branch=args.branch,
                current_commit=args.commit,
                max_active_agents=args.max_active_agents,
                max_total_agents_per_cycle=args.max_total_agents_per_cycle,
                max_clone_depth=args.max_clone_depth,
                max_children_per_agent=args.max_children_per_agent,
                max_agents_per_module=args.max_agents_per_module,
            )
        else:
            orchestrator = DevelopmentOrchestrator(ProjectStateStore(args.state))

        if args.command == "prepare-code-proposal":
            from .code_proposal import CodeProposal, ProposalChecks, load_proposal_json, prepare_code_proposal
            try:
                proposal = CodeProposal.model_validate(load_proposal_json(args.proposal))
                checks = ProposalChecks.model_validate(load_proposal_json(args.checks, limit=262_144))
            except (ValueError, OSError) as exc:
                raise ValueError("Codevorschlag oder Prüfvertrag ist nicht gültig lesbar") from exc
            job = prepare_code_proposal(proposal, state=orchestrator.state(), agent_id=args.agent,
                                        checks=checks, python_executable=args.python_executable)
            # Preserve existing jobs/reviews, including symlink destinations.
            _write_new_json(args.output, job.model_dump(mode="json"))
            print(json.dumps({"prepared": True, "submitted": False, "executed": False,
                              "proposal_sha256": proposal.digest(), "files": len(proposal.files),
                              "output": str(Path(args.output).resolve())}, ensure_ascii=False))
            return 0
        elif args.command in {"prepare-code-request", "prepare-code-response"}:
            from .code_proposal import ProposalChecks, load_proposal_json
            from .code_request import CodeRequestPackage, CodeResponse, build_code_request, prepare_code_response
            try:
                checks = ProposalChecks.model_validate(load_proposal_json(args.checks, limit=262_144))
                if args.command == "prepare-code-request":
                    package = build_code_request(
                        state=orchestrator.state(), agent_id=args.agent, repository=args.repository,
                        context_paths=args.context_file, checks=checks, data_class=args.data_class,
                    )
                    payload = package.model_dump(mode="json")
                    metadata = {"request_prepared": True, "request_sha256": package.request_sha256,
                                "context_files": len(package.request.context_files), "model_called": False}
                else:
                    package = CodeRequestPackage.model_validate(load_proposal_json(args.request, limit=131_072))
                    response = CodeResponse.model_validate(load_proposal_json(args.response, limit=16_384))
                    job = prepare_code_response(
                        package, response, state=orchestrator.state(), agent_id=args.agent,
                        repository=args.repository, checks=checks, python_executable=args.python_executable,
                    )
                    payload = job.model_dump(mode="json")
                    metadata = {"prepared": True, "request_sha256": package.request_sha256,
                                "proposal_sha256": response.proposal.digest(), "model_called": False}
            except PydanticValidationError as exc:
                raise ValueError("Codeauftrag oder Agentenantwort verletzt den Vertrag") from exc
            _write_new_json(args.output, payload)
            print(json.dumps({**metadata, "submitted": False, "executed": False,
                              "output": str(Path(args.output).resolve())}, ensure_ascii=False))
            return 0
        elif args.command == "begin-cycle":
            orchestrator.begin_cycle(reason=args.reason)
        elif args.command == "demo":
            state = orchestrator.state()
            if "DEMO-BACKEND" not in state.tasks:
                orchestrator.create_task(TaskSpec(
                    task_id="DEMO-BACKEND",
                    goal="Implementiere das Authentifizierungsmodul.",
                    base_commit=state.current_commit,
                    affected_components=["backend/auth"],
                    allowed_paths=["backend/src/auth/**", "backend/tests/auth/**"],
                    acceptance_criteria=["Authentifizierung serverseitig geprüft"],
                ))
                parent = orchestrator.start_agent("DEMO-BACKEND", DevelopmentRole.BACKEND)
                orchestrator.approve_spawn(SpawnRequest(
                    parent_agent_id=parent.agent_id,
                    requested_role=DevelopmentRole.BACKEND,
                    reason="Benachrichtigungen sind unabhängig vom Auth-Modul.",
                    task_id="DEMO-NOTIFICATIONS",
                    assigned_subtask="Implementiere ausschließlich das Benachrichtigungsmodul.",
                    allowed_paths=[
                        "backend/src/notifications/**",
                        "backend/tests/notifications/**",
                    ],
                    base_commit=state.current_commit,
                    acceptance_criteria=["Modul besitzt isolierte Integrationstests"],
                ))
        elif args.command == "run-pipeline":
            job = _load_pipeline_job(args.job)
            manager = GitWorkspaceManager(args.repository, args.workspace_root)
            result = WorkerPipelineRunner(
                orchestrator,
                manager,
                container_executor=DockerCommandExecutor(args.container_runtime),
                require_container_isolation=args.require_container_isolation,
            ).run(job)
            print(json.dumps({
                "run": result.run.model_dump(mode="json"),
                "workspace": {
                    "path": str(result.workspace.path),
                    "branch": result.workspace.branch,
                    "base_commit": result.workspace.base_commit,
                },
                "project": _state_summary(orchestrator),
            }, ensure_ascii=False, indent=2))
            return 0
        elif args.command == "run-pool":
            job = _load_pool_job(args.job)
            manager = GitWorkspaceManager(args.repository, args.workspace_root)
            result = WorkerPoolRunner(
                orchestrator,
                manager,
                container_executor=DockerCommandExecutor(args.container_runtime),
                require_container_isolation=args.require_container_isolation,
            ).run(job)
            print(json.dumps({
                "passed": result.passed,
                "completed": {
                    agent_id: item.run.model_dump(mode="json")
                    for agent_id, item in result.completed.items()
                },
                "failures": result.failures,
                "cancelled": list(result.cancelled),
                "project": _state_summary(orchestrator),
            }, ensure_ascii=False, indent=2))
            return 0 if result.passed else 2
        elif args.command == "integrate":
            job = _load_integration_job(args.job)
            manager = GitWorkspaceManager(args.repository, args.workspace_root)
            result = WorkerIntegrationRunner(
                orchestrator,
                manager,
                container_executor=DockerCommandExecutor(args.container_runtime),
                require_container_isolation=args.require_container_isolation,
            ).run(job)
            print(json.dumps({
                "run": result.run.model_dump(mode="json"),
                "integration": {
                    "previous_commit": result.previous_commit,
                    "integration_commit": result.integration_commit,
                    "rebased": result.rebased,
                    "cleanup_warning": result.cleanup_warning,
                },
                "project": _state_summary(orchestrator),
            }, ensure_ascii=False, indent=2))
            return 0
        print(json.dumps(_state_summary(orchestrator), ensure_ascii=False, indent=2))
        return 0
    except (
        OrchestrationError,
        WorkerExecutionError,
        WorkerPoolError,
        IntegrationExecutionError,
        PydanticValidationError,
        OSError,
        RuntimeError,
        ValueError,
    ) as exc:
        print(f"Fehler: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
