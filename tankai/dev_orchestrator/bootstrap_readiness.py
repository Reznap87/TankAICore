"""Non-activating readiness receipt for single-host queue configuration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from tankai.web.auth import AgentManagementActor

from .job_queue import DevelopmentJobQueue, QueueError


REQUIRED_SUBMISSION_SCOPES = frozenset({"jobs:read", "jobs:submit"})


@dataclass(frozen=True)
class _VerifiedActor:
    user_id: str
    tenant_id: str
    workspace_id: str
    role: str


def evaluate_bootstrap_configuration(
    queue: DevelopmentJobQueue,
    *,
    actor: AgentManagementActor,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Inspect persistent queue and agent configuration without activating it."""
    if queue.auth is None:
        raise ValueError("Auth-Datenbank fehlt")
    access = queue.auth.workspace_access(actor.user_id, actor.workspace_id)
    if (
        access is None
        or access.tenant_id != actor.tenant_id
        or access.role not in {"owner", "admin"}
    ):
        raise PermissionError(
            "Nur Owner oder Admins dürfen die Runner-Konfiguration prüfen"
        )
    verified_actor = _VerifiedActor(
        user_id=actor.user_id,
        tenant_id=access.tenant_id,
        workspace_id=access.id,
        role=access.role,
    )

    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("Prüfzeitpunkt muss eine Zeitzone besitzen")

    policy = queue.get_policy(verified_actor.workspace_id)
    policy_ready = (
        policy is not None
        and policy.enabled
        and policy.tenant_id == verified_actor.tenant_id
    )

    repositories = queue.list_repositories(
        actor_user_id=verified_actor.user_id,
        workspace_id=verified_actor.workspace_id,
    )
    enabled_repositories = [item for item in repositories if item.enabled]
    valid_repository_ids: set[str] = set()
    for repository in enabled_repositories:
        try:
            queue.repository(repository.repository_id, validate_filesystem=True)
        except (OSError, QueueError, ValueError):
            continue
        valid_repository_ids.add(repository.repository_id)

    agents = queue.auth.list_service_agents(actor=verified_actor)
    active_agents = [item for item in agents if item.is_active]
    authorized_agents = []
    for agent in active_agents:
        owner_access = queue.auth.workspace_access(
            agent.owner_user_id, verified_actor.workspace_id
        )
        if (
            policy_ready
            and owner_access is not None
            and owner_access.tenant_id == verified_actor.tenant_id
            and owner_access.role in policy.submit_roles
        ):
            authorized_agents.append(agent)

    active_token_count = 0
    submission_token_count = 0
    for agent in authorized_agents:
        for token in queue.auth.list_agent_tokens(
            actor=verified_actor, agent_id=agent.agent_id
        ):
            if token.revoked_at is not None or token.expires_at <= current:
                continue
            active_token_count += 1
            if (
                REQUIRED_SUBMISSION_SCOPES.issubset(token.scopes)
                and valid_repository_ids.intersection(token.repository_ids)
            ):
                submission_token_count += 1

    checks = [
        {
            "id": "queue.policy_active",
            "status": "PASS" if policy_ready else "FAIL",
            "summary": (
                f"active=true; immutable_images={len(policy.allowed_images)}"
                if policy_ready and policy is not None
                else "Keine aktive, mandantengebundene Queue-Richtlinie"
            ),
        },
        {
            "id": "queue.repository_binding",
            "status": "PASS" if valid_repository_ids else "FAIL",
            "summary": (
                f"valid_active={len(valid_repository_ids)}; "
                f"enabled={len(enabled_repositories)}; total={len(repositories)}"
            ),
        },
        {
            "id": "agents.authorized_service_agent",
            "status": "PASS" if authorized_agents else "FAIL",
            "summary": (
                f"authorized_active={len(authorized_agents)}; "
                f"active={len(active_agents)}; total={len(agents)}"
            ),
        },
        {
            "id": "agents.submission_token",
            "status": "PASS" if submission_token_count else "FAIL",
            "summary": (
                f"submission_ready={submission_token_count}; "
                f"active_for_authorized_agents={active_token_count}"
            ),
        },
    ]

    next_actions: list[str] = []
    if not policy_ready:
        next_actions.append(
            "Mit set-policy eine aktive Workspace-Richtlinie und Image-Digests festlegen."
        )
    if not valid_repository_ids:
        next_actions.append(
            "Mit register-repository mindestens eine erreichbare Git-Wurzel registrieren."
        )
    if not authorized_agents:
        next_actions.append(
            "Mit create-service-agent einen aktiven Agenten mit "
            "einreichungsberechtigtem Owner anlegen."
        )
    if not submission_token_count:
        next_actions.append(
            "Mit create-agent-token jobs:read und jobs:submit für ein gültiges "
            "Repository vergeben."
        )

    return {
        "schema_version": 1,
        "gate": "ops.development.single_host_runner_bootstrap.configuration",
        "ready": all(item["status"] == "PASS" for item in checks),
        "snapshot_only": True,
        "final_submit_revalidates": True,
        "host_readiness_not_evaluated": True,
        "runtime_activation_not_evaluated": True,
        "checks": checks,
        "counts": {
            "repositories_total": len(repositories),
            "repositories_enabled": len(enabled_repositories),
            "repositories_valid": len(valid_repository_ids),
            "service_agents_total": len(agents),
            "service_agents_active": len(active_agents),
            "service_agents_authorized": len(authorized_agents),
            "active_tokens_for_authorized_agents": active_token_count,
            "submission_ready_tokens": submission_token_count,
        },
        "next_actions": next_actions,
    }
