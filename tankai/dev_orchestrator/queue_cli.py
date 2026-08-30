"""Operator and worker CLI for the tenant-bound development job queue."""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError as PydanticValidationError

from tankai.web.auth import AgentManagementActor, AuthStore

from .job_queue import (
    DevelopmentJobQueue,
    QueueError,
    QueuedWorkerDispatcher,
    WorkspaceQueuePolicy,
)
from .models import WorkerPipelineJob


@dataclass(frozen=True)
class _OperatorActor:
    user_id: str
    tenant_id: str
    workspace_id: str
    role: str


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tankai-job-queue")
    parser.add_argument("--queue-db", required=True)
    parser.add_argument(
        "--fence-db",
        help="Separate monotone Fence-Datenbank; Standard: <queue-db>-fences.db",
    )
    parser.add_argument("--auth-db")
    parser.add_argument("--repository-base", required=True)
    parser.add_argument("--workspace-base", required=True)
    parser.add_argument("--state-base", required=True)
    sub = parser.add_subparsers(dest="command", required=True)

    policy = sub.add_parser("set-policy", help="Setzt die Admission-Richtlinie eines Workspaces")
    policy.add_argument("--actor-email", required=True)
    policy.add_argument("--workspace-id", required=True)
    policy.add_argument("--allowed-image", action="append", required=True)
    policy.add_argument("--max-queued", type=int, default=20)
    policy.add_argument("--max-running", type=int, default=2)
    policy.add_argument("--max-memory-mb", type=int, default=2048)
    policy.add_argument("--max-cpus", type=float, default=4.0)
    policy.add_argument("--max-pids", type=int, default=512)
    policy.add_argument("--max-runtime-seconds", type=int, default=3600)
    policy.add_argument("--max-attempts", type=int, default=3)
    policy.add_argument("--max-jobs-per-user-hour", type=int, default=20)
    policy.add_argument(
        "--submit-role", action="append", choices=["owner", "admin", "member"],
        help="Standard: owner und admin",
    )
    policy.add_argument("--disabled", action="store_true")

    register = sub.add_parser("register-repository", help="Registriert operatorseitige Repository-Pfade")
    register.add_argument("--actor-email", required=True)
    register.add_argument("--workspace-id", required=True)
    register.add_argument("--name", required=True)
    register.add_argument("--repository", required=True)
    register.add_argument("--worktrees", required=True)
    register.add_argument("--state", required=True)

    repos = sub.add_parser("list-repositories")
    repos.add_argument("--actor-email", required=True)
    repos.add_argument("--workspace-id", required=True)

    enqueue = sub.add_parser("enqueue", help="Validiert und speichert einen WorkerPipelineJob")
    enqueue.add_argument("--actor-email", required=True)
    enqueue.add_argument("--workspace-id", required=True)
    enqueue.add_argument("--repository-id", required=True)
    enqueue.add_argument("--job", required=True)
    enqueue.add_argument("--idempotency-key", required=True)
    enqueue.add_argument("--priority", type=int, default=0)

    jobs = sub.add_parser("list-jobs")
    jobs.add_argument("--actor-email", required=True)
    jobs.add_argument("--workspace-id", required=True)
    jobs.add_argument("--limit", type=int, default=100)

    cancel = sub.add_parser("cancel")
    cancel.add_argument("--actor-email", required=True)
    cancel.add_argument("--workspace-id", required=True)
    cancel.add_argument("--job-id", required=True)

    create_agent = sub.add_parser(
        "create-service-agent",
        help="Legt einen workspacegebundenen Service-Agenten an",
    )
    create_agent.add_argument("--actor-email", required=True)
    create_agent.add_argument("--workspace-id", required=True)
    create_agent.add_argument("--name", required=True)
    create_agent.add_argument("--description", default="")

    list_agents = sub.add_parser(
        "list-service-agents",
        help="Listet Service-Agenten ohne Token-Geheimnisse",
    )
    list_agents.add_argument("--actor-email", required=True)
    list_agents.add_argument("--workspace-id", required=True)

    create_token = sub.add_parser(
        "create-agent-token",
        help="Erzeugt einen einmalig sichtbaren, repositorygebundenen Agenten-Token",
    )
    create_token.add_argument("--actor-email", required=True)
    create_token.add_argument("--workspace-id", required=True)
    create_token.add_argument("--agent-id", required=True)
    create_token.add_argument(
        "--scope",
        action="append",
        required=True,
        choices=["repositories:read", "jobs:submit", "jobs:read", "jobs:cancel"],
    )
    create_token.add_argument("--repository-id", action="append", required=True)
    create_token.add_argument("--expires-in-days", type=int, default=30)
    create_token.add_argument("--label", default="")

    list_tokens = sub.add_parser(
        "list-agent-tokens",
        help="Listet Token-Metadaten; Roh-Tokens werden nie erneut ausgegeben",
    )
    list_tokens.add_argument("--actor-email", required=True)
    list_tokens.add_argument("--workspace-id", required=True)
    list_tokens.add_argument("--agent-id", required=True)

    revoke_token = sub.add_parser(
        "revoke-agent-token",
        help="Widerruft genau einen aktiven Agenten-Token",
    )
    revoke_token.add_argument("--actor-email", required=True)
    revoke_token.add_argument("--workspace-id", required=True)
    revoke_token.add_argument("--agent-id", required=True)
    revoke_token.add_argument("--token-id", required=True)

    deactivate_agent = sub.add_parser(
        "deactivate-service-agent",
        help="Deaktiviert einen Service-Agenten und widerruft alle seine Tokens",
    )
    deactivate_agent.add_argument("--actor-email", required=True)
    deactivate_agent.add_argument("--workspace-id", required=True)
    deactivate_agent.add_argument("--agent-id", required=True)

    fence_status = sub.add_parser("fence-status", help="Zeigt den externen Repository-Fence")
    fence_status.add_argument("--actor-email", required=True)
    fence_status.add_argument("--workspace-id", required=True)
    fence_status.add_argument("--repository-id", required=True)

    force_fence = sub.add_parser(
        "force-expire-fence",
        help="Operator-Recovery: widerruft exakt eine bestätigte Fence-Epoche",
    )
    force_fence.add_argument("--actor-email", required=True)
    force_fence.add_argument("--workspace-id", required=True)
    force_fence.add_argument("--repository-id", required=True)
    force_fence.add_argument("--expected-epoch", required=True, type=int)
    force_fence.add_argument("--expected-job-id", required=True)

    reaper = sub.add_parser(
        "reap-worktrees",
        help="Bereinigt saubere verwaiste TankAI-Worktrees; Branches bleiben erhalten",
    )
    reaper.add_argument("--actor-email", required=True)
    reaper.add_argument("--workspace-id", required=True)
    reaper.add_argument("--repository-id", required=True)
    reaper.add_argument("--min-age-seconds", type=float, default=3600.0)
    reaper.add_argument("--apply", action="store_true", help="Ohne --apply nur Dry-Run")
    reaper.add_argument(
        "--expected-stale-run-id",
        help="Exakte Operator-Bestätigung für einen stale nicht-terminalen Worker-Run",
    )

    container_reaper = sub.add_parser(
        "reap-containers",
        help="Bereinigt stale labelgebundene TankAI-Container; Standard ist Dry-Run",
    )
    container_reaper.add_argument("--actor-email", required=True)
    container_reaper.add_argument("--workspace-id", required=True)
    container_reaper.add_argument("--repository-id", required=True)
    container_reaper.add_argument("--container-runtime", default="docker")
    container_reaper.add_argument("--min-age-seconds", type=float, default=3600.0)
    container_reaper.add_argument("--apply", action="store_true", help="Ohne --apply nur Dry-Run")
    container_reaper.add_argument("--expected-stale-job-id")
    container_reaper.add_argument("--expected-fence-epoch", type=int)

    run = sub.add_parser("run-once", help="Least und führt maximal einen Queue-Auftrag aus")
    run.add_argument("--worker-id", required=True)
    run.add_argument("--container-runtime", default="docker")
    run.add_argument("--lease-seconds", type=int, default=300)

    worker = sub.add_parser("run-worker", help="Verarbeitet die Queue fortlaufend")
    worker.add_argument("--worker-id", required=True)
    worker.add_argument("--container-runtime", default="docker")
    worker.add_argument("--lease-seconds", type=int, default=300)
    worker.add_argument("--poll-seconds", type=float, default=2.0)
    worker.add_argument("--max-jobs", type=int, default=0, help="0 bedeutet unbegrenzt")
    worker.add_argument("--exit-when-idle", action="store_true")
    return parser


def _queue(args: argparse.Namespace) -> DevelopmentJobQueue:
    auth = AuthStore(args.auth_db) if args.auth_db else None
    if args.command not in {"run-once", "run-worker"} and auth is None:
        raise ValueError("--auth-db ist für diesen Befehl erforderlich")
    return DevelopmentJobQueue(
        args.queue_db,
        auth_store=auth,
        repository_base=args.repository_base,
        workspace_base=args.workspace_base,
        state_base=args.state_base,
        fence_path=args.fence_db,
    )


def _actor(queue: DevelopmentJobQueue, email: str) -> str:
    if queue.auth is None:
        raise ValueError("Auth-Datenbank fehlt")
    user_id = queue.auth.get_user_id_by_email(email)
    if not user_id:
        raise ValueError("Nutzer nicht gefunden")
    return user_id


def _agent_actor(
    queue: DevelopmentJobQueue, email: str, workspace_id: str
) -> AgentManagementActor:
    if queue.auth is None:
        raise ValueError("Auth-Datenbank fehlt")
    user_id = _actor(queue, email)
    access = queue.auth.workspace_access(user_id, workspace_id)
    if access is None:
        raise PermissionError("Kein Zugriff auf diesen Workspace")
    return _OperatorActor(
        user_id=user_id,
        tenant_id=access.tenant_id,
        workspace_id=access.id,
        role=access.role,
    )


def _load_pipeline(path: str) -> WorkerPipelineJob:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Worker-Job ist nicht lesbar: {path}: {exc}") from exc
    return WorkerPipelineJob.model_validate(payload)


def _dump(value) -> None:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _service_agent_payload(agent) -> dict[str, object]:
    return {
        "agent_id": agent.agent_id,
        "tenant_id": agent.tenant_id,
        "workspace_id": agent.workspace_id,
        "name": agent.name,
        "description": agent.description,
        "owner_user_id": agent.owner_user_id,
        "is_active": agent.is_active,
        "created_at": agent.created_at.isoformat(),
        "updated_at": agent.updated_at.isoformat(),
    }


def _agent_token_payload(token) -> dict[str, object]:
    return {
        "token_id": token.token_id,
        "token_prefix": token.token_prefix,
        "scopes": list(token.scopes),
        "repository_ids": list(token.repository_ids),
        "label": token.label,
        "created_at": token.created_at.isoformat(),
        "expires_at": token.expires_at.isoformat(),
        "last_used_at": token.last_used_at.isoformat() if token.last_used_at else None,
        "revoked_at": token.revoked_at.isoformat() if token.revoked_at else None,
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        queue = _queue(args)
        if args.command == "set-policy":
            actor = _actor(queue, args.actor_email)
            access = queue.auth.workspace_access(actor, args.workspace_id)
            if access is None:
                raise PermissionError("Kein Zugriff auf diesen Workspace")
            result = queue.set_policy(
                actor_user_id=actor,
                workspace_id=args.workspace_id,
                policy=WorkspaceQueuePolicy(
                    tenant_id=access.tenant_id,
                    workspace_id=args.workspace_id,
                    enabled=not args.disabled,
                    max_queued=args.max_queued,
                    max_running=args.max_running,
                    max_memory_mb=args.max_memory_mb,
                    max_cpus=args.max_cpus,
                    max_pids=args.max_pids,
                    max_runtime_seconds=args.max_runtime_seconds,
                    max_attempts=args.max_attempts,
                    max_jobs_per_user_hour=args.max_jobs_per_user_hour,
                    submit_roles=args.submit_role or ["owner", "admin"],
                    allowed_images=args.allowed_image,
                ),
            )
            _dump(result)
        elif args.command == "register-repository":
            result = queue.register_repository(
                actor_user_id=_actor(queue, args.actor_email),
                workspace_id=args.workspace_id,
                name=args.name,
                repository_path=args.repository,
                workspace_root=args.worktrees,
                state_path=args.state,
            )
            _dump(result)
        elif args.command == "list-repositories":
            result = queue.list_repositories(
                actor_user_id=_actor(queue, args.actor_email),
                workspace_id=args.workspace_id,
            )
            _dump([item.model_dump(mode="json") for item in result])
        elif args.command == "enqueue":
            result = queue.enqueue(
                actor_user_id=_actor(queue, args.actor_email),
                workspace_id=args.workspace_id,
                repository_id=args.repository_id,
                pipeline=_load_pipeline(args.job),
                idempotency_key=args.idempotency_key,
                priority=args.priority,
            )
            _dump(result)
        elif args.command == "list-jobs":
            result = queue.list_jobs(
                actor_user_id=_actor(queue, args.actor_email),
                workspace_id=args.workspace_id,
                limit=args.limit,
            )
            _dump([item.model_dump(mode="json") for item in result])
        elif args.command == "cancel":
            result = queue.cancel_job(
                actor_user_id=_actor(queue, args.actor_email),
                workspace_id=args.workspace_id,
                job_id=args.job_id,
            )
            _dump(result)
        elif args.command == "create-service-agent":
            if queue.auth is None:
                raise ValueError("Auth-Datenbank fehlt")
            result = queue.auth.create_service_agent(
                actor=_agent_actor(queue, args.actor_email, args.workspace_id),
                name=args.name,
                description=args.description,
            )
            _dump({"agent": _service_agent_payload(result)})
        elif args.command == "list-service-agents":
            if queue.auth is None:
                raise ValueError("Auth-Datenbank fehlt")
            result = queue.auth.list_service_agents(
                actor=_agent_actor(queue, args.actor_email, args.workspace_id)
            )
            _dump({"agents": [_service_agent_payload(item) for item in result]})
        elif args.command == "create-agent-token":
            if queue.auth is None:
                raise ValueError("Auth-Datenbank fehlt")
            actor = _agent_actor(queue, args.actor_email, args.workspace_id)
            visible = {
                item.repository_id
                for item in queue.list_repositories(
                    actor_user_id=actor.user_id,
                    workspace_id=actor.workspace_id,
                )
                if item.enabled
            }
            requested = {item.strip() for item in args.repository_id}
            if not requested or not requested.issubset(visible):
                raise PermissionError(
                    "Mindestens ein aktives Repository ist erforderlich und muss zum Workspace gehören"
                )
            result = queue.auth.create_agent_token(
                actor=actor,
                agent_id=args.agent_id,
                scopes=args.scope,
                repository_ids=args.repository_id,
                expires_in_days=args.expires_in_days,
                label=args.label,
            )
            _dump(
                {
                    "token": {
                        "token_id": result.token_id,
                        "secret": result.token,
                        "token_prefix": result.token_prefix,
                        "scopes": list(result.scopes),
                        "repository_ids": list(result.repository_ids),
                        "created_at": result.created_at.isoformat(),
                        "expires_at": result.expires_at.isoformat(),
                        "shown_once": True,
                    }
                }
            )
        elif args.command == "list-agent-tokens":
            if queue.auth is None:
                raise ValueError("Auth-Datenbank fehlt")
            result = queue.auth.list_agent_tokens(
                actor=_agent_actor(queue, args.actor_email, args.workspace_id),
                agent_id=args.agent_id,
            )
            _dump({"tokens": [_agent_token_payload(item) for item in result]})
        elif args.command == "revoke-agent-token":
            if queue.auth is None:
                raise ValueError("Auth-Datenbank fehlt")
            queue.auth.revoke_agent_token(
                actor=_agent_actor(queue, args.actor_email, args.workspace_id),
                agent_id=args.agent_id,
                token_id=args.token_id,
            )
            _dump({"ok": True})
        elif args.command == "deactivate-service-agent":
            if queue.auth is None:
                raise ValueError("Auth-Datenbank fehlt")
            queue.auth.deactivate_service_agent(
                actor=_agent_actor(queue, args.actor_email, args.workspace_id),
                agent_id=args.agent_id,
            )
            _dump({"ok": True})
        elif args.command == "fence-status":
            result = queue.fence_status(
                actor_user_id=_actor(queue, args.actor_email),
                workspace_id=args.workspace_id,
                repository_id=args.repository_id,
            )
            _dump(result)
        elif args.command == "force-expire-fence":
            result = queue.force_expire_fence(
                actor_user_id=_actor(queue, args.actor_email),
                workspace_id=args.workspace_id,
                repository_id=args.repository_id,
                expected_epoch=args.expected_epoch,
                expected_job_id=args.expected_job_id,
            )
            _dump(result)
        elif args.command == "reap-worktrees":
            result = queue.reap_worktrees(
                actor_user_id=_actor(queue, args.actor_email),
                workspace_id=args.workspace_id,
                repository_id=args.repository_id,
                min_age_seconds=args.min_age_seconds,
                dry_run=not args.apply,
                expected_stale_run_id=args.expected_stale_run_id,
            )
            _dump(result)
        elif args.command == "reap-containers":
            result = queue.reap_containers(
                actor_user_id=_actor(queue, args.actor_email),
                workspace_id=args.workspace_id,
                repository_id=args.repository_id,
                runtime_binary=args.container_runtime,
                min_age_seconds=args.min_age_seconds,
                dry_run=not args.apply,
                expected_stale_job_id=args.expected_stale_job_id,
                expected_fence_epoch=args.expected_fence_epoch,
            )
            _dump(result)
        elif args.command == "run-once":
            result = QueuedWorkerDispatcher(
                queue,
                worker_id=args.worker_id,
                container_runtime=args.container_runtime,
            ).run_once(lease_seconds=args.lease_seconds)
            _dump(
                {"claimed": False}
                if result is None
                else {
                    "claimed": True,
                    "job_id": result.job_id,
                    "state": result.state.value,
                    "result": result.result,
                    "error": result.error,
                }
            )
            return 0 if result is None or result.state.value == "succeeded" else 2
        elif args.command == "run-worker":
            if args.poll_seconds < 0.1 or args.poll_seconds > 300:
                raise ValueError("--poll-seconds muss zwischen 0.1 und 300 liegen")
            if args.max_jobs < 0:
                raise ValueError("--max-jobs darf nicht negativ sein")
            dispatcher = QueuedWorkerDispatcher(
                queue,
                worker_id=args.worker_id,
                container_runtime=args.container_runtime,
            )
            processed = 0
            failed = 0
            try:
                while args.max_jobs == 0 or processed < args.max_jobs:
                    result = dispatcher.run_once(lease_seconds=args.lease_seconds)
                    if result is None:
                        if args.exit_when_idle:
                            break
                        time.sleep(args.poll_seconds)
                        continue
                    processed += 1
                    if result.state.value != "succeeded":
                        failed += 1
                    _dump({
                        "job_id": result.job_id,
                        "state": result.state.value,
                        "error": result.error,
                    })
            except KeyboardInterrupt:
                pass
            _dump({"worker_id": args.worker_id, "processed": processed, "failed": failed})
            return 0 if failed == 0 else 2
        return 0
    except (QueueError, PydanticValidationError, PermissionError, ValueError) as exc:
        print(f"Fehler: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
