"""Persistent tenant-bound development job queue with admission control.

The queue never accepts host paths from untrusted job submissions. Repositories,
worktree roots, and orchestrator state paths are registered by an authenticated
workspace owner/admin and are constrained to operator-configured base folders.
Every queued job is bound to a user, tenant, workspace, repository, immutable
container image, and resource budget before a worker may lease it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import secrets
import sqlite3
import subprocess
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from tankai.web.auth import AuthStore, WorkspaceAccess

from .container_runtime import ContainerRuntimeError, DockerCommandExecutor
from .fencing import FenceBusy, FenceError, FenceLost, LeaseFenceStore
from .git_workspace import GitWorkspaceManager
from .models import TaskState, WorkerPipelineJob, WorkerRunState
from .orchestrator import DevelopmentOrchestrator
from .state_store import ProjectStateStore
from .worker import WorkerPipelineRunner


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _token_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _payload_hash(
    *,
    user_id: str,
    tenant_id: str,
    workspace_id: str,
    repository_id: str,
    pipeline: WorkerPipelineJob,
) -> str:
    encoded = _canonical_json({
        "user_id": user_id,
        "tenant_id": tenant_id,
        "workspace_id": workspace_id,
        "repository_id": repository_id,
        "pipeline": pipeline.model_dump(mode="json"),
    }).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _is_under(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


class QueueError(RuntimeError):
    pass


class AdmissionDenied(QueueError):
    pass


class LeaseError(QueueError):
    pass


class JobState(str, Enum):
    QUEUED = "queued"
    LEASED = "leased"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class QueueModel(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")


class WorkspaceQueuePolicy(QueueModel):
    tenant_id: str = Field(min_length=1, max_length=120)
    workspace_id: str = Field(min_length=1, max_length=120)
    enabled: bool = True
    max_queued: int = Field(default=20, ge=1, le=10000)
    max_running: int = Field(default=2, ge=1, le=64)
    max_memory_mb: int = Field(default=2048, ge=64, le=65536)
    max_cpus: float = Field(default=4.0, gt=0, le=64)
    max_pids: int = Field(default=512, ge=16, le=8192)
    max_runtime_seconds: int = Field(default=3600, ge=1, le=86400)
    max_attempts: int = Field(default=3, ge=1, le=10)
    max_jobs_per_user_hour: int = Field(default=20, ge=1, le=1000)
    submit_roles: list[str] = Field(default_factory=lambda: ["owner", "admin"], min_length=1, max_length=3)
    allowed_images: list[str] = Field(min_length=1, max_length=64)

    @field_validator("submit_roles")
    @classmethod
    def _validate_submit_roles(cls, value: list[str]) -> list[str]:
        allowed = {"owner", "admin", "member"}
        result: list[str] = []
        for role in value:
            role = role.strip().lower()
            if role not in allowed:
                raise ValueError("Ungültige Queue-Einreicherrolle")
            if role not in result:
                result.append(role)
        return result

    @field_validator("allowed_images")
    @classmethod
    def _validate_images(cls, value: list[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for image in value:
            image = image.strip()
            immutable = (
                image.startswith("sha256:") and len(image) == 71
            ) or ("@sha256:" in image and len(image.rsplit("@sha256:", 1)[1]) == 64)
            digest = image.rsplit("sha256:", 1)[-1]
            if not immutable or any(ch not in "0123456789abcdefABCDEF" for ch in digest):
                raise ValueError("Queue-Images müssen per sha256-Digest oder Image-ID fixiert sein")
            if image not in seen:
                seen.add(image)
                result.append(image)
        return result


class RepositoryBinding(QueueModel):
    repository_id: str
    tenant_id: str
    workspace_id: str
    name: str
    repository_path: str
    workspace_root: str
    state_path: str
    enabled: bool
    created_by: str
    created_at: datetime


class QueuedDevelopmentJob(QueueModel):
    job_id: str
    user_id: str
    tenant_id: str
    workspace_id: str
    repository_id: str
    state: JobState
    priority: int
    idempotency_key: str
    payload_sha256: str
    pipeline: WorkerPipelineJob
    image: str
    memory_mb: int
    cpus: float
    pids_limit: int
    runtime_seconds: int
    attempts: int
    max_attempts: int
    created_at: datetime
    available_at: datetime
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    fence_epoch: int | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    result: dict[str, Any] | None = None
    error: str = ""


class JobLease(QueueModel):
    lease_token: str
    fence_epoch: int
    job: QueuedDevelopmentJob
    repository: RepositoryBinding


@dataclass(frozen=True)
class DispatchResult:
    job_id: str
    state: JobState
    result: dict[str, Any] | None = None
    error: str = ""


class DevelopmentJobQueue:
    SCHEMA_VERSION = 3

    def __init__(
        self,
        path: str | Path,
        *,
        auth_store: AuthStore | None = None,
        repository_base: str | Path,
        workspace_base: str | Path,
        state_base: str | Path,
        fence_path: str | Path | None = None,
    ) -> None:
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        self.auth = auth_store
        self.repository_base = Path(repository_base).resolve()
        self.workspace_base = Path(workspace_base).resolve()
        self.state_base = Path(state_base).resolve()
        resolved_fence_path = (
            Path(fence_path).resolve()
            if fence_path is not None
            else self.path.with_name(f"{self.path.stem}-fences.db")
        )
        if resolved_fence_path == self.path:
            raise ValueError("Fence-Datenbank muss von der Queue-Datenbank getrennt sein")
        self.fence_store = LeaseFenceStore(resolved_fence_path)
        self._lock = threading.RLock()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _init_schema(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(
                """
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS queue_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS queue_policies (
                    workspace_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
                    max_queued INTEGER NOT NULL,
                    max_running INTEGER NOT NULL,
                    max_memory_mb INTEGER NOT NULL,
                    max_cpus REAL NOT NULL,
                    max_pids INTEGER NOT NULL,
                    max_runtime_seconds INTEGER NOT NULL,
                    max_attempts INTEGER NOT NULL,
                    max_jobs_per_user_hour INTEGER NOT NULL,
                    submit_roles_json TEXT NOT NULL,
                    allowed_images_json TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS repositories (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    workspace_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    repository_path TEXT NOT NULL,
                    workspace_root TEXT NOT NULL,
                    state_path TEXT NOT NULL,
                    enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(workspace_id, name),
                    UNIQUE(repository_path)
                );
                CREATE INDEX IF NOT EXISTS idx_repositories_workspace
                    ON repositories(tenant_id, workspace_id, enabled);
                CREATE TABLE IF NOT EXISTS development_jobs (
                    id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    workspace_id TEXT NOT NULL,
                    repository_id TEXT NOT NULL REFERENCES repositories(id),
                    payload_json TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('queued','leased','running','succeeded','failed','cancelled')),
                    priority INTEGER NOT NULL,
                    image TEXT NOT NULL,
                    memory_mb INTEGER NOT NULL,
                    cpus REAL NOT NULL,
                    pids_limit INTEGER NOT NULL,
                    runtime_seconds INTEGER NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    available_at TEXT NOT NULL,
                    lease_owner TEXT,
                    lease_token_hash TEXT,
                    lease_expires_at TEXT,
                    fence_epoch INTEGER,
                    started_at TEXT,
                    finished_at TEXT,
                    result_json TEXT,
                    error TEXT NOT NULL DEFAULT '',
                    UNIQUE(user_id, workspace_id, idempotency_key)
                );
                CREATE INDEX IF NOT EXISTS idx_jobs_claim
                    ON development_jobs(state, available_at, priority DESC, created_at);
                CREATE INDEX IF NOT EXISTS idx_jobs_workspace_state
                    ON development_jobs(workspace_id, state);
                CREATE INDEX IF NOT EXISTS idx_jobs_user_created
                    ON development_jobs(user_id, created_at);
                CREATE TABLE IF NOT EXISTS job_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT,
                    event_type TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    details_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                INSERT OR REPLACE INTO queue_meta(key,value) VALUES('schema_version','3');
                COMMIT;
                """
            )
            conn.execute("BEGIN IMMEDIATE")
            try:
                columns = {
                    row["name"]
                    for row in conn.execute("PRAGMA table_info(queue_policies)").fetchall()
                }
                if "submit_roles_json" not in columns:
                    conn.execute(
                        "ALTER TABLE queue_policies ADD COLUMN submit_roles_json TEXT NOT NULL "
                        "DEFAULT '[\"owner\",\"admin\"]'"
                    )
                job_columns = {
                    row["name"]
                    for row in conn.execute("PRAGMA table_info(development_jobs)").fetchall()
                }
                if "fence_epoch" not in job_columns:
                    conn.execute("ALTER TABLE development_jobs ADD COLUMN fence_epoch INTEGER")
                duplicates = conn.execute(
                    """
                    SELECT repository_id,GROUP_CONCAT(id) AS ids
                    FROM development_jobs
                    WHERE state IN ('leased','running')
                    GROUP BY repository_id HAVING COUNT(*) > 1
                    """
                ).fetchall()
                for duplicate in duplicates:
                    ids = [item for item in str(duplicate["ids"]).split(",") if item]
                    for stale_id in ids[1:]:
                        conn.execute(
                            """
                            UPDATE development_jobs
                            SET state='failed',finished_at=?,error=?,lease_owner=NULL,
                                lease_token_hash=NULL,lease_expires_at=NULL,fence_epoch=NULL
                            WHERE id=?
                            """,
                            (
                                _iso(_utcnow()),
                                "Schema-Migration blockierte doppelten aktiven Repository-Lease",
                                stale_id,
                            ),
                        )
                conn.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_active_repository
                    ON development_jobs(repository_id)
                    WHERE state IN ('leased','running')
                    """
                )
                conn.execute(
                    "INSERT OR REPLACE INTO queue_meta(key,value) VALUES('schema_version',?)",
                    (str(self.SCHEMA_VERSION),),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def _access(self, user_id: str, workspace_id: str) -> WorkspaceAccess:
        if self.auth is None:
            raise PermissionError("Diese Queue-Instanz besitzt keinen Auth-Zugriff")
        access = self.auth.workspace_access(user_id, workspace_id)
        if access is None:
            raise PermissionError("Kein Zugriff auf diesen Workspace")
        return access

    @staticmethod
    def _require_admin(access: WorkspaceAccess) -> None:
        if access.role not in {"owner", "admin"}:
            raise PermissionError("Nur Owner oder Admins dürfen Queue-Richtlinien ändern")

    def set_policy(
        self,
        *,
        actor_user_id: str,
        workspace_id: str,
        policy: WorkspaceQueuePolicy,
    ) -> WorkspaceQueuePolicy:
        access = self._access(actor_user_id, workspace_id)
        self._require_admin(access)
        if policy.workspace_id != workspace_id or policy.tenant_id != access.tenant_id:
            raise AdmissionDenied("Queue-Richtlinie ist nicht an den authentifizierten Workspace gebunden")
        now = _iso(_utcnow())
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO queue_policies(
                    workspace_id,tenant_id,enabled,max_queued,max_running,max_memory_mb,max_cpus,
                    max_pids,max_runtime_seconds,max_attempts,max_jobs_per_user_hour,
                    submit_roles_json,allowed_images_json,updated_by,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(workspace_id) DO UPDATE SET
                    tenant_id=excluded.tenant_id,enabled=excluded.enabled,
                    max_queued=excluded.max_queued,max_running=excluded.max_running,
                    max_memory_mb=excluded.max_memory_mb,max_cpus=excluded.max_cpus,
                    max_pids=excluded.max_pids,max_runtime_seconds=excluded.max_runtime_seconds,
                    max_attempts=excluded.max_attempts,
                    max_jobs_per_user_hour=excluded.max_jobs_per_user_hour,
                    submit_roles_json=excluded.submit_roles_json,
                    allowed_images_json=excluded.allowed_images_json,
                    updated_by=excluded.updated_by,updated_at=excluded.updated_at
                """,
                (
                    policy.workspace_id,
                    policy.tenant_id,
                    int(policy.enabled),
                    policy.max_queued,
                    policy.max_running,
                    policy.max_memory_mb,
                    policy.max_cpus,
                    policy.max_pids,
                    policy.max_runtime_seconds,
                    policy.max_attempts,
                    policy.max_jobs_per_user_hour,
                    _canonical_json(policy.submit_roles),
                    _canonical_json(policy.allowed_images),
                    actor_user_id,
                    now,
                ),
            )
            self._event(conn, None, "policy_updated", actor_user_id, policy.model_dump(mode="json"))
        return policy

    def get_policy(self, workspace_id: str) -> WorkspaceQueuePolicy | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM queue_policies WHERE workspace_id=?", (workspace_id,)).fetchone()
        if row is None:
            return None
        return WorkspaceQueuePolicy(
            tenant_id=row["tenant_id"],
            workspace_id=row["workspace_id"],
            enabled=bool(row["enabled"]),
            max_queued=row["max_queued"],
            max_running=row["max_running"],
            max_memory_mb=row["max_memory_mb"],
            max_cpus=row["max_cpus"],
            max_pids=row["max_pids"],
            max_runtime_seconds=row["max_runtime_seconds"],
            max_attempts=row["max_attempts"],
            max_jobs_per_user_hour=row["max_jobs_per_user_hour"],
            submit_roles=json.loads(row["submit_roles_json"]),
            allowed_images=json.loads(row["allowed_images_json"]),
        )

    def _resolve_operator_path(
        self,
        value: str | Path,
        *,
        base: Path,
        must_exist: bool,
        create_directory: bool = False,
    ) -> Path:
        raw = Path(value)
        if not raw.is_absolute():
            raw = base / raw
        try:
            resolved = raw.resolve(strict=must_exist)
        except OSError as exc:
            raise AdmissionDenied(f"Pfad ist nicht auflösbar: {raw}") from exc
        if not _is_under(resolved, base):
            raise AdmissionDenied(f"Pfad liegt außerhalb des freigegebenen Basisverzeichnisses: {resolved}")
        if create_directory:
            resolved.mkdir(parents=True, exist_ok=True)
            resolved = resolved.resolve(strict=True)
            if not _is_under(resolved, base):
                raise AdmissionDenied("Erzeugter Pfad verlässt das freigegebene Basisverzeichnis")
        return resolved

    def register_repository(
        self,
        *,
        actor_user_id: str,
        workspace_id: str,
        name: str,
        repository_path: str | Path,
        workspace_root: str | Path,
        state_path: str | Path,
    ) -> RepositoryBinding:
        access = self._access(actor_user_id, workspace_id)
        self._require_admin(access)
        clean_name = name.strip()
        if not clean_name or len(clean_name) > 120:
            raise ValueError("Repository-Name fehlt oder ist zu lang")
        repo = self._resolve_operator_path(
            repository_path, base=self.repository_base, must_exist=True
        )
        if not repo.is_dir():
            raise AdmissionDenied("Repository-Pfad ist kein Verzeichnis")
        try:
            root = subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "--show-toplevel"],
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError) as exc:
            raise AdmissionDenied("Repository-Pfad ist kein gültiges Git-Repository") from exc
        if Path(root).resolve() != repo:
            raise AdmissionDenied("Nur die Wurzel eines Git-Repositories darf registriert werden")
        work_root = self._resolve_operator_path(
            workspace_root,
            base=self.workspace_base,
            must_exist=False,
            create_directory=True,
        )
        state = self._resolve_operator_path(
            state_path, base=self.state_base, must_exist=False
        )
        state.parent.mkdir(parents=True, exist_ok=True)
        if _is_under(work_root, repo) or _is_under(state, repo):
            raise AdmissionDenied("Worktree- und State-Pfade müssen außerhalb des Repositorys liegen")
        now = _utcnow()
        repository_id = str(uuid4())
        with self._lock, self._connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    """
                    INSERT INTO repositories(
                        id,tenant_id,workspace_id,name,repository_path,workspace_root,state_path,
                        enabled,created_by,created_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        repository_id,
                        access.tenant_id,
                        workspace_id,
                        clean_name,
                        str(repo),
                        str(work_root),
                        str(state),
                        1,
                        actor_user_id,
                        _iso(now),
                    ),
                )
                self._event(conn, None, "repository_registered", actor_user_id, {
                    "repository_id": repository_id,
                    "workspace_id": workspace_id,
                    "name": clean_name,
                })
                conn.execute("COMMIT")
            except sqlite3.IntegrityError as exc:
                conn.execute("ROLLBACK")
                raise AdmissionDenied("Repository-Name oder Pfad ist bereits registriert") from exc
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return RepositoryBinding(
            repository_id=repository_id,
            tenant_id=access.tenant_id,
            workspace_id=workspace_id,
            name=clean_name,
            repository_path=str(repo),
            workspace_root=str(work_root),
            state_path=str(state),
            enabled=True,
            created_by=actor_user_id,
            created_at=now,
        )

    def repository(
        self, repository_id: str, *, validate_filesystem: bool = False
    ) -> RepositoryBinding:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM repositories WHERE id=?", (repository_id,)).fetchone()
        if row is None:
            raise AdmissionDenied("Repository ist nicht registriert")
        binding = self._repository_from_row(row)
        if validate_filesystem:
            self._validate_repository_binding(binding)
        return binding

    def list_repositories(self, *, actor_user_id: str, workspace_id: str) -> list[RepositoryBinding]:
        access = self._access(actor_user_id, workspace_id)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM repositories WHERE tenant_id=? AND workspace_id=? ORDER BY name,id",
                (access.tenant_id, workspace_id),
            ).fetchall()
        return [self._repository_from_row(row) for row in rows]

    def fence_status(
        self,
        *,
        actor_user_id: str,
        workspace_id: str,
        repository_id: str,
    ) -> dict[str, Any] | None:
        access = self._access(actor_user_id, workspace_id)
        self._require_admin(access)
        repository = self.repository(repository_id)
        if (
            repository.tenant_id != access.tenant_id
            or repository.workspace_id != workspace_id
        ):
            raise PermissionError("Repository gehört nicht zum authentifizierten Workspace")
        status = self.fence_store.current(repository_id)
        if status is None:
            return None
        return {
            "scope_key": status.scope_key,
            "epoch": status.epoch,
            "job_id": status.job_id,
            "owner_id": status.owner_id,
            "expires_at": status.expires_at.isoformat() if status.expires_at else None,
            "active": status.active,
        }

    def force_expire_fence(
        self,
        *,
        actor_user_id: str,
        workspace_id: str,
        repository_id: str,
        expected_epoch: int,
        expected_job_id: str,
    ) -> dict[str, Any]:
        access = self._access(actor_user_id, workspace_id)
        self._require_admin(access)
        repository = self.repository(repository_id)
        if (
            repository.tenant_id != access.tenant_id
            or repository.workspace_id != workspace_id
        ):
            raise PermissionError("Repository gehört nicht zum authentifizierten Workspace")
        status = self.fence_store.current(repository_id)
        if status is None or not status.active:
            raise LeaseError("Repository besitzt keinen aktiven externen Fence")
        if status.epoch != int(expected_epoch):
            raise LeaseError("Fence-Epoche hat sich seit der Operator-Prüfung geändert")
        if status.job_id != expected_job_id.strip():
            raise LeaseError("Fence gehört nicht zum bestätigten Job")
        self.fence_store.force_expire_for_recovery(
            repository_id, expected_epoch=int(expected_epoch)
        )
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._event(conn, expected_job_id, "fence_force_expired", actor_user_id, {
                "repository_id": repository_id,
                "fence_epoch": int(expected_epoch),
            })
            conn.execute("COMMIT")
        updated = self.fence_store.current(repository_id)
        assert updated is not None
        return {
            "scope_key": updated.scope_key,
            "epoch": updated.epoch,
            "job_id": updated.job_id,
            "owner_id": updated.owner_id,
            "expires_at": updated.expires_at.isoformat() if updated.expires_at else None,
            "active": updated.active,
        }

    def reap_worktrees(
        self,
        *,
        actor_user_id: str,
        workspace_id: str,
        repository_id: str,
        min_age_seconds: float = 3600.0,
        dry_run: bool = True,
        expected_stale_run_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Operator-controlled cleanup of clean orphaned TankAI worktrees.

        The operation is blocked while the repository has an active fence or an
        active queue job. Dirty worktrees are quarantined, never deleted. A
        non-terminal ProjectState run may only be ignored through an exact run-id
        confirmation after queue/fence recovery.
        """
        access = self._access(actor_user_id, workspace_id)
        self._require_admin(access)
        repository = self.repository(repository_id)
        if repository.tenant_id != access.tenant_id or repository.workspace_id != workspace_id:
            raise PermissionError("Repository gehört nicht zum authentifizierten Workspace")
        fence = self.fence_store.current(repository_id)
        if fence is not None and fence.active:
            raise LeaseError("Worktree-Reaper ist bei aktivem Repository-Fence blockiert")
        with self._connect() as conn:
            active = conn.execute(
                "SELECT id FROM development_jobs WHERE repository_id=? AND state IN ('leased','running')",
                (repository_id,),
            ).fetchall()
        if active:
            raise LeaseError("Worktree-Reaper ist bei aktivem Queue-Job blockiert")

        orchestrator = DevelopmentOrchestrator(ProjectStateStore(repository.state_path))
        state = orchestrator.state()
        confirmed_run = None
        if expected_stale_run_id:
            confirmed_run = state.worker_runs.get(expected_stale_run_id.strip())
            if confirmed_run is None:
                raise QueueError("Bestätigter stale Worker-Run existiert nicht")

        active_task_states = {
            TaskState.ACTIVE, TaskState.REVIEW_PENDING, TaskState.QA_PENDING,
            TaskState.READY_TO_INTEGRATE,
        }
        active_run_states = {
            WorkerRunState.PENDING, WorkerRunState.RUNNING, WorkerRunState.SUBMITTED,
            WorkerRunState.READY_TO_INTEGRATE, WorkerRunState.INTEGRATING,
        }
        protected_paths: set[str] = set()
        for agent in state.agents.values():
            if not agent.workspace_path:
                continue
            task = state.tasks.get(agent.task_id)
            confirmed_path = (
                str(Path(confirmed_run.workspace_path).resolve())
                if confirmed_run is not None
                else None
            )
            agent_path = str(Path(agent.workspace_path).resolve())
            protect = bool(
                task
                and task.state in active_task_states
                and agent_path != confirmed_path
            )
            for run in state.worker_runs.values():
                if run.workspace_path == agent.workspace_path and run.state in active_run_states:
                    if confirmed_run is None or run.run_id != confirmed_run.run_id:
                        protect = True
            if protect:
                protected_paths.add(str(Path(agent.workspace_path).resolve()))

        manager = GitWorkspaceManager(repository.repository_path, repository.workspace_root)
        records = manager.reap_managed_worktrees(
            protected_paths=sorted(protected_paths),
            min_age_seconds=min_age_seconds,
            dry_run=dry_run,
        )
        removed_paths = {
            str(Path(item.workspace_path).resolve())
            for item in records
            if item.action == "removed"
        }
        if removed_paths and not dry_run:
            confirmed_run_path = (
                str(Path(confirmed_run.workspace_path).resolve())
                if confirmed_run is not None and confirmed_run.workspace_path
                else None
            )
            if confirmed_run is not None and confirmed_run_path in removed_paths:
                if confirmed_run.state in active_run_states:
                    orchestrator.fail_worker_run(
                        confirmed_run.run_id,
                        error="Operator-Reaper schloss einen stale Worker-Run nach Lease-/Fence-Verlust.",
                        blocked=True,
                    )
            refreshed = orchestrator.state()
            for agent in refreshed.agents.values():
                if agent.workspace_path and str(Path(agent.workspace_path).resolve()) in removed_paths:
                    orchestrator.unbind_workspace(agent.agent_id)

        payload = [
            {
                "workspace_path": item.workspace_path,
                "branch": item.branch,
                "action": item.action,
                "reason": item.reason,
            }
            for item in records
        ]
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._event(conn, None, "worktree_reaper", actor_user_id, {
                "repository_id": repository_id,
                "dry_run": dry_run,
                "min_age_seconds": min_age_seconds,
                "expected_stale_run_id": expected_stale_run_id,
                "records": payload,
            })
            conn.execute("COMMIT")
        return payload

    def reap_containers(
        self,
        *,
        actor_user_id: str,
        workspace_id: str,
        repository_id: str,
        runtime_binary: str = "docker",
        min_age_seconds: float = 3600.0,
        dry_run: bool = True,
        expected_stale_job_id: str | None = None,
        expected_fence_epoch: int | None = None,
        container_executor: DockerCommandExecutor | None = None,
    ) -> list[dict[str, Any]]:
        """Remove only stale, label-bound TankAI containers for one repository.

        Containers belonging to a live queue lease or the current external fence
        are always protected. Unknown jobs and stale non-terminal jobs require an
        exact operator confirmation consisting of job id and fence epoch.
        """
        access = self._access(actor_user_id, workspace_id)
        self._require_admin(access)
        repository = self.repository(repository_id)
        if repository.tenant_id != access.tenant_id or repository.workspace_id != workspace_id:
            raise PermissionError("Repository gehört nicht zum authentifizierten Workspace")
        if not math.isfinite(min_age_seconds) or min_age_seconds < 0:
            raise ValueError("--min-age-seconds muss eine nichtnegative endliche Zahl sein")
        if (expected_stale_job_id is None) != (expected_fence_epoch is None):
            raise ValueError("Stale-Container-Bestätigung benötigt Job-ID und Fence-Epoche")
        if expected_fence_epoch is not None and expected_fence_epoch < 1:
            raise ValueError("Fence-Epoche muss positiv sein")

        executor = container_executor or DockerCommandExecutor(
            runtime_binary, require_rootless=True
        )
        executor.ensure_available()
        containers = executor.list_managed_containers(repository_id=repository_id)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM development_jobs WHERE repository_id=?",
                (repository_id,),
            ).fetchall()
        jobs = {str(row["id"]): row for row in rows}
        fence = self.fence_store.current(repository_id)
        now = _utcnow()
        results: list[dict[str, Any]] = []
        removal_failures: list[str] = []

        for container in containers:
            labels = container.labels
            base = {
                "container_id": container.container_id,
                "name": container.name,
                "state": container.state,
                "created_at": _iso(container.created_at),
                "run_id": labels.get("tankai.run_id", ""),
                "job_id": labels.get("tankai.job_id", ""),
                "fence_epoch": labels.get("tankai.fence_epoch", ""),
            }
            required_exact = {
                "tankai.managed": "true",
                "tankai.repository_id": repository_id,
                "tankai.workspace_id": workspace_id,
                "tankai.tenant_id": access.tenant_id,
            }
            if any(labels.get(key) != value for key, value in required_exact.items()):
                results.append({**base, "action": "skipped", "reason": "invalid_scope_labels"})
                continue
            job_id = labels.get("tankai.job_id", "").strip()
            run_id = labels.get("tankai.run_id", "").strip()
            phase = labels.get("tankai.phase", "").strip()
            try:
                epoch = int(labels.get("tankai.fence_epoch", ""))
            except ValueError:
                epoch = 0
            if not job_id or not run_id or not phase or epoch < 1:
                results.append({**base, "action": "skipped", "reason": "incomplete_identity_labels"})
                continue
            age_seconds = max(0.0, (now - container.created_at).total_seconds())
            base["age_seconds"] = round(age_seconds, 3)
            if age_seconds < min_age_seconds:
                results.append({**base, "action": "skipped", "reason": "below_minimum_age"})
                continue

            row = jobs.get(job_id)
            row_state = str(row["state"]) if row is not None else "unknown"
            row_epoch = int(row["fence_epoch"]) if row is not None and row["fence_epoch"] is not None else None
            lease_expires = _parse_time(row["lease_expires_at"]) if row is not None else None
            live_queue = bool(
                row is not None
                and row_state in {JobState.LEASED.value, JobState.RUNNING.value}
                and row_epoch == epoch
                and lease_expires is not None
                and lease_expires > now
            )
            live_fence = bool(
                fence is not None
                and fence.active
                and fence.job_id == job_id
                and fence.epoch == epoch
            )
            base["job_state"] = row_state
            if live_queue or live_fence:
                reason = "protected_by_live_queue_and_fence" if live_queue and live_fence else (
                    "protected_by_live_queue" if live_queue else "protected_by_live_fence"
                )
                results.append({**base, "action": "protected", "reason": reason})
                continue
            if row_state == JobState.QUEUED.value:
                results.append({**base, "action": "protected", "reason": "job_not_started"})
                continue

            needs_confirmation = row is None or row_state in {
                JobState.LEASED.value, JobState.RUNNING.value
            }
            confirmed = bool(
                expected_stale_job_id == job_id and expected_fence_epoch == epoch
            )
            if needs_confirmation and not confirmed:
                results.append({
                    **base,
                    "action": "skipped",
                    "reason": "exact_stale_confirmation_required",
                })
                continue

            if dry_run:
                results.append({**base, "action": "would_remove", "reason": "stale_container"})
                continue
            try:
                executor.remove_container(container.container_id)
            except ContainerRuntimeError as exc:
                removal_failures.append(f"{container.container_id}: {exc}")
                results.append({**base, "action": "failed", "reason": str(exc)})
            else:
                results.append({**base, "action": "removed", "reason": "stale_container"})

        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._event(conn, None, "container_reaper", actor_user_id, {
                "repository_id": repository_id,
                "runtime": runtime_binary,
                "dry_run": dry_run,
                "min_age_seconds": min_age_seconds,
                "expected_stale_job_id": expected_stale_job_id,
                "expected_fence_epoch": expected_fence_epoch,
                "records": results,
            })
            conn.execute("COMMIT")
        if removal_failures:
            raise QueueError("Container-Reaper konnte nicht alle Container entfernen: " + "; ".join(removal_failures))
        return results

    def _validate_repository_binding(
        self, binding: RepositoryBinding, *, check_git: bool = True
    ) -> None:
        repo = Path(binding.repository_path).resolve(strict=True)
        work = Path(binding.workspace_root).resolve(strict=False)
        state = Path(binding.state_path).resolve(strict=False)
        if not _is_under(repo, self.repository_base):
            raise AdmissionDenied("Registriertes Repository liegt außerhalb der Operator-Allowlist")
        if not _is_under(work, self.workspace_base):
            raise AdmissionDenied("Registrierter Worktree-Pfad liegt außerhalb der Operator-Allowlist")
        if not _is_under(state, self.state_base):
            raise AdmissionDenied("Registrierter State-Pfad liegt außerhalb der Operator-Allowlist")
        if check_git:
            try:
                root = subprocess.run(
                    ["git", "-C", str(repo), "rev-parse", "--show-toplevel"],
                    check=True, capture_output=True, text=True, timeout=30,
                ).stdout.strip()
            except (OSError, subprocess.SubprocessError) as exc:
                raise AdmissionDenied("Registriertes Repository ist nicht mehr gültig") from exc
            if Path(root).resolve() != repo:
                raise AdmissionDenied("Registrierte Repository-Wurzel stimmt nicht mehr")

    @staticmethod
    def _requested_runtime(pipeline: WorkerPipelineJob) -> int:
        commands = [
            *pipeline.worker.implementation_commands,
            *pipeline.worker.test_commands,
            *pipeline.gates.review_commands,
            *pipeline.gates.qa_commands,
            *pipeline.gates.security_commands,
        ]
        return max(1, math.ceil(sum(item.timeout_seconds for item in commands)))

    @staticmethod
    def _validate_no_inline_secrets(pipeline: WorkerPipelineJob) -> None:
        sensitive = ("password", "passwd", "secret", "token", "api_key", "apikey", "credential")
        commands = [
            *pipeline.worker.implementation_commands,
            *pipeline.worker.test_commands,
            *pipeline.gates.review_commands,
            *pipeline.gates.qa_commands,
            *pipeline.gates.security_commands,
        ]
        for command in commands:
            for key in command.env:
                normalized = key.casefold()
                if any(marker in normalized for marker in sensitive):
                    raise AdmissionDenied(
                        "Inline-Secrets sind in Queue-Jobs verboten; kurzlebige Credentials benötigen einen Broker"
                    )

    def enqueue(
        self,
        *,
        actor_user_id: str,
        workspace_id: str,
        repository_id: str,
        pipeline: WorkerPipelineJob,
        idempotency_key: str,
        priority: int = 0,
    ) -> QueuedDevelopmentJob:
        access = self._access(actor_user_id, workspace_id)
        key = idempotency_key.strip()
        if not key or len(key) > 200 or any(ord(ch) < 32 for ch in key):
            raise ValueError("Ungültiger Idempotency-Key")
        if priority < -100 or priority > 100:
            raise ValueError("Priorität muss zwischen -100 und 100 liegen")
        repository = self.repository(repository_id)
        if (
            not repository.enabled
            or repository.workspace_id != workspace_id
            or repository.tenant_id != access.tenant_id
        ):
            raise PermissionError("Repository gehört nicht zum authentifizierten Workspace")
        policy = self.get_policy(workspace_id)
        if policy is None or not policy.enabled:
            raise AdmissionDenied("Für diesen Workspace ist keine aktive Queue-Richtlinie konfiguriert")
        if policy.tenant_id != access.tenant_id:
            raise AdmissionDenied("Queue-Richtlinie verletzt die Mandantentrennung")
        if access.role not in policy.submit_roles:
            raise PermissionError("Die Workspace-Rolle darf keine Development-Jobs einreichen")
        if access.role not in {"owner", "admin"} and priority != 0:
            raise AdmissionDenied("Mitglieder dürfen die Queue-Priorität nicht verändern")
        self._validate_no_inline_secrets(pipeline)
        isolation = pipeline.isolation
        if isolation is None:
            raise AdmissionDenied("Online-Worker benötigen verpflichtende Container-Isolation")
        runtime_seconds = self._requested_runtime(pipeline)
        violations: list[str] = []
        if isolation.image not in policy.allowed_images:
            violations.append("Container-Image ist nicht freigegeben")
        if isolation.memory_mb > policy.max_memory_mb:
            violations.append("RAM-Budget überschritten")
        if isolation.cpus > policy.max_cpus:
            violations.append("CPU-Budget überschritten")
        if isolation.pids_limit > policy.max_pids:
            violations.append("PID-Budget überschritten")
        if runtime_seconds > policy.max_runtime_seconds:
            violations.append("Laufzeitbudget überschritten")
        if violations:
            raise AdmissionDenied("; ".join(violations))

        payload_sha256 = _payload_hash(
            user_id=actor_user_id,
            tenant_id=access.tenant_id,
            workspace_id=workspace_id,
            repository_id=repository_id,
            pipeline=pipeline,
        )
        payload_json = _canonical_json(pipeline.model_dump(mode="json"))
        if len(payload_json.encode("utf-8")) > 1_000_000:
            raise AdmissionDenied("Worker-Payload überschreitet 1 MB")
        now = _utcnow()
        job_id = str(uuid4())
        with self._lock, self._connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                existing = conn.execute(
                    """
                    SELECT * FROM development_jobs
                    WHERE user_id=? AND workspace_id=? AND idempotency_key=?
                    """,
                    (actor_user_id, workspace_id, key),
                ).fetchone()
                if existing is not None:
                    if existing["payload_sha256"] != payload_sha256 or existing["repository_id"] != repository_id:
                        raise AdmissionDenied("Idempotency-Key wurde bereits für einen anderen Auftrag verwendet")
                    conn.execute("COMMIT")
                    return self._job_from_row(existing)
                queued = conn.execute(
                    "SELECT COUNT(*) AS n FROM development_jobs WHERE workspace_id=? AND state='queued'",
                    (workspace_id,),
                ).fetchone()["n"]
                if int(queued) >= policy.max_queued:
                    raise AdmissionDenied("Queue-Limit des Workspaces ist erreicht")
                cutoff = _iso(now - timedelta(hours=1))
                recent = conn.execute(
                    "SELECT COUNT(*) AS n FROM development_jobs WHERE user_id=? AND created_at>=?",
                    (actor_user_id, cutoff),
                ).fetchone()["n"]
                if int(recent) >= policy.max_jobs_per_user_hour:
                    raise AdmissionDenied("Stündliches Nutzerlimit ist erreicht")
                conn.execute(
                    """
                    INSERT INTO development_jobs(
                        id,idempotency_key,user_id,tenant_id,workspace_id,repository_id,
                        payload_json,payload_sha256,state,priority,image,memory_mb,cpus,pids_limit,
                        runtime_seconds,attempts,max_attempts,created_at,available_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        job_id,
                        key,
                        actor_user_id,
                        access.tenant_id,
                        workspace_id,
                        repository_id,
                        payload_json,
                        payload_sha256,
                        JobState.QUEUED.value,
                        priority,
                        isolation.image,
                        isolation.memory_mb,
                        isolation.cpus,
                        isolation.pids_limit,
                        runtime_seconds,
                        0,
                        policy.max_attempts,
                        _iso(now),
                        _iso(now),
                    ),
                )
                self._event(conn, job_id, "job_enqueued", actor_user_id, {
                    "repository_id": repository_id,
                    "image": isolation.image,
                    "memory_mb": isolation.memory_mb,
                    "cpus": isolation.cpus,
                    "pids_limit": isolation.pids_limit,
                    "runtime_seconds": runtime_seconds,
                })
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return self.get_job(actor_user_id=actor_user_id, workspace_id=workspace_id, job_id=job_id)

    def list_jobs(
        self,
        *,
        actor_user_id: str,
        workspace_id: str,
        limit: int = 100,
    ) -> list[QueuedDevelopmentJob]:
        access = self._access(actor_user_id, workspace_id)
        limit = max(1, min(int(limit), 500))
        with self._connect() as conn:
            if access.role in {"owner", "admin"}:
                rows = conn.execute(
                    """
                    SELECT * FROM development_jobs
                    WHERE tenant_id=? AND workspace_id=?
                    ORDER BY created_at DESC,id DESC LIMIT ?
                    """,
                    (access.tenant_id, workspace_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM development_jobs
                    WHERE tenant_id=? AND workspace_id=? AND user_id=?
                    ORDER BY created_at DESC,id DESC LIMIT ?
                    """,
                    (access.tenant_id, workspace_id, actor_user_id, limit),
                ).fetchall()
        return [self._job_from_row(row) for row in rows]

    def get_job(
        self,
        *,
        actor_user_id: str,
        workspace_id: str,
        job_id: str,
    ) -> QueuedDevelopmentJob:
        access = self._access(actor_user_id, workspace_id)
        with self._connect() as conn:
            if access.role in {"owner", "admin"}:
                row = conn.execute(
                    "SELECT * FROM development_jobs WHERE id=? AND tenant_id=? AND workspace_id=?",
                    (job_id, access.tenant_id, workspace_id),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT * FROM development_jobs
                    WHERE id=? AND tenant_id=? AND workspace_id=? AND user_id=?
                    """,
                    (job_id, access.tenant_id, workspace_id, actor_user_id),
                ).fetchone()
        if row is None:
            raise PermissionError("Auftrag nicht gefunden oder nicht zugreifbar")
        return self._job_from_row(row)

    def cancel_job(
        self,
        *,
        actor_user_id: str,
        workspace_id: str,
        job_id: str,
    ) -> QueuedDevelopmentJob:
        access = self._access(actor_user_id, workspace_id)
        now = _iso(_utcnow())
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM development_jobs WHERE id=? AND tenant_id=? AND workspace_id=?",
                (job_id, access.tenant_id, workspace_id),
            ).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                raise PermissionError("Auftrag nicht gefunden oder nicht zugreifbar")
            if row["user_id"] != actor_user_id and access.role not in {"owner", "admin"}:
                conn.execute("ROLLBACK")
                raise PermissionError("Nur der Ersteller oder ein Admin darf den Auftrag abbrechen")
            if row["state"] != JobState.QUEUED.value:
                conn.execute("ROLLBACK")
                raise QueueError("Nur noch nicht geleaste Aufträge können abgebrochen werden")
            conn.execute(
                "UPDATE development_jobs SET state='cancelled',finished_at=? WHERE id=?",
                (now, job_id),
            )
            self._event(conn, job_id, "job_cancelled", actor_user_id, {})
            conn.execute("COMMIT")
        return self.get_job(actor_user_id=actor_user_id, workspace_id=workspace_id, job_id=job_id)

    def claim_next(self, *, worker_id: str, lease_seconds: int = 300) -> JobLease | None:
        worker = worker_id.strip()
        if not worker or len(worker) > 120 or any(ord(ch) < 32 for ch in worker):
            raise ValueError("Ungültige Worker-ID")
        lease_seconds = max(30, min(int(lease_seconds), 86400))
        now = _utcnow()
        raw_token = secrets.token_urlsafe(48)
        acquired_scope: str | None = None
        acquired_epoch: int | None = None
        acquired_job_id: str | None = None
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._recover_expired_leases(conn, now)
                candidates = conn.execute(
                    """
                    SELECT j.*,p.max_running,p.enabled AS policy_enabled,
                           p.max_memory_mb AS policy_max_memory_mb,
                           p.max_cpus AS policy_max_cpus,p.max_pids AS policy_max_pids,
                           p.max_runtime_seconds AS policy_max_runtime_seconds,
                           p.allowed_images_json AS policy_allowed_images_json
                    FROM development_jobs j
                    JOIN queue_policies p ON p.workspace_id=j.workspace_id AND p.tenant_id=j.tenant_id
                    JOIN repositories r ON r.id=j.repository_id
                    WHERE j.state='queued' AND j.available_at<=? AND p.enabled=1 AND r.enabled=1
                    ORDER BY j.priority DESC,j.created_at,j.id
                    LIMIT 200
                    """,
                    (_iso(now),),
                ).fetchall()
                chosen = None
                chosen_repo = None
                chosen_fence = None
                for row in candidates:
                    try:
                        self._job_from_row(row)
                        repository_row = conn.execute(
                            "SELECT * FROM repositories WHERE id=?", (row["repository_id"],)
                        ).fetchone()
                        if repository_row is None:
                            raise AdmissionDenied("Repository-Bindung fehlt")
                        repository_binding = self._repository_from_row(repository_row)
                        if (
                            repository_binding.tenant_id != row["tenant_id"]
                            or repository_binding.workspace_id != row["workspace_id"]
                        ):
                            raise AdmissionDenied("Job- und Repository-Bindung stimmen nicht überein")
                        self._validate_repository_binding(repository_binding, check_git=False)
                        allowed_images = json.loads(row["policy_allowed_images_json"])
                        if row["image"] not in allowed_images:
                            raise AdmissionDenied("Container-Image wurde nachträglich gesperrt")
                        if int(row["memory_mb"]) > int(row["policy_max_memory_mb"]):
                            raise AdmissionDenied("RAM-Richtlinie wurde nachträglich unterschritten")
                        if float(row["cpus"]) > float(row["policy_max_cpus"]):
                            raise AdmissionDenied("CPU-Richtlinie wurde nachträglich unterschritten")
                        if int(row["pids_limit"]) > int(row["policy_max_pids"]):
                            raise AdmissionDenied("PID-Richtlinie wurde nachträglich unterschritten")
                        if int(row["runtime_seconds"]) > int(row["policy_max_runtime_seconds"]):
                            raise AdmissionDenied("Laufzeitrichtlinie wurde nachträglich unterschritten")
                    except Exception as exc:
                        conn.execute(
                            "UPDATE development_jobs SET state='failed',finished_at=?,error=? WHERE id=?",
                            (_iso(now), f"Admission-Integritätsprüfung fehlgeschlagen: {str(exc)[:1000]}", row["id"]),
                        )
                        self._event(conn, row["id"], "job_integrity_failed", "queue", {"error": str(exc)[:1000]})
                        continue
                    active = conn.execute(
                        """
                        SELECT COUNT(*) AS n FROM development_jobs
                        WHERE workspace_id=? AND state IN ('leased','running')
                        """,
                        (row["workspace_id"],),
                    ).fetchone()["n"]
                    if int(active) >= int(row["max_running"]):
                        continue
                    active_repository = conn.execute(
                        """
                        SELECT COUNT(*) AS n FROM development_jobs
                        WHERE repository_id=? AND state IN ('leased','running')
                        """,
                        (row["repository_id"],),
                    ).fetchone()["n"]
                    if int(active_repository) > 0:
                        continue
                    try:
                        fence = self.fence_store.acquire(
                            scope_key=row["repository_id"],
                            job_id=row["id"],
                            owner_id=worker,
                            lease_token=raw_token,
                            lease_seconds=lease_seconds,
                        )
                    except FenceBusy:
                        # The independent store is authoritative. A stale queue
                        # snapshot must not supersede an unexpired repository fence.
                        continue
                    chosen = row
                    chosen_repo = repository_row
                    chosen_fence = fence
                    acquired_scope = fence.scope_key
                    acquired_epoch = fence.epoch
                    acquired_job_id = fence.job_id
                    break
                if chosen is None or chosen_repo is None or chosen_fence is None:
                    conn.execute("COMMIT")
                    return None
                cursor = conn.execute(
                    """
                    UPDATE development_jobs
                    SET state='leased',lease_owner=?,lease_token_hash=?,lease_expires_at=?,
                        fence_epoch=?,attempts=attempts+1,error=''
                    WHERE id=? AND state='queued'
                    """,
                    (
                        worker,
                        _token_hash(raw_token),
                        _iso(chosen_fence.expires_at),
                        chosen_fence.epoch,
                        chosen["id"],
                    ),
                )
                if cursor.rowcount != 1:
                    raise LeaseError("Queue-Lease konnte nicht atomar gespeichert werden")
                self._event(conn, chosen["id"], "job_leased", worker, {
                    "lease_expires_at": _iso(chosen_fence.expires_at),
                    "fence_epoch": chosen_fence.epoch,
                    "fence_scope": chosen_fence.scope_key,
                })
                row = conn.execute("SELECT * FROM development_jobs WHERE id=?", (chosen["id"],)).fetchone()
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                if acquired_scope and acquired_epoch is not None and acquired_job_id:
                    try:
                        self.fence_store.release(
                            scope_key=acquired_scope,
                            job_id=acquired_job_id,
                            epoch=acquired_epoch,
                            lease_token=raw_token,
                            reason="queue claim rollback",
                        )
                    except FenceError:
                        pass
                raise
        return JobLease(
            lease_token=raw_token,
            fence_epoch=int(row["fence_epoch"]),
            job=self._job_from_row(row),
            repository=self._repository_from_row(chosen_repo),
        )

    def start_job(self, *, job_id: str, lease_token: str) -> QueuedDevelopmentJob:
        now = _utcnow()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._leased_row(conn, job_id, lease_token, {JobState.LEASED})
            cursor = conn.execute(
                "UPDATE development_jobs SET state='running',started_at=? WHERE id=? AND state='leased'",
                (_iso(now), job_id),
            )
            if cursor.rowcount != 1:
                conn.execute("ROLLBACK")
                raise LeaseError("Auftrag konnte nicht gestartet werden")
            self._event(conn, job_id, "job_started", row["lease_owner"], {
                "fence_epoch": int(row["fence_epoch"]),
            })
            result = conn.execute("SELECT * FROM development_jobs WHERE id=?", (job_id,)).fetchone()
            conn.execute("COMMIT")
        return self._job_from_row(result)

    def assert_lease_active(self, *, job_id: str, lease_token: str) -> QueuedDevelopmentJob:
        """Fail closed unless both queue lease and external fence are current."""
        with self._connect() as conn:
            conn.execute("BEGIN")
            row = self._leased_row(
                conn, job_id, lease_token, {JobState.LEASED, JobState.RUNNING}
            )
            conn.execute("COMMIT")
        return self._job_from_row(row)

    def heartbeat(self, *, job_id: str, lease_token: str, lease_seconds: int = 300) -> datetime:
        lease_seconds = max(30, min(int(lease_seconds), 86400))
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._leased_row(conn, job_id, lease_token, {JobState.LEASED, JobState.RUNNING})
            try:
                fence = self.fence_store.renew(
                    scope_key=row["repository_id"],
                    job_id=job_id,
                    epoch=int(row["fence_epoch"]),
                    lease_token=lease_token,
                    lease_seconds=lease_seconds,
                )
            except FenceError as exc:
                conn.execute("ROLLBACK")
                raise LeaseError(f"Externer Fence konnte nicht erneuert werden: {exc}") from exc
            conn.execute(
                "UPDATE development_jobs SET lease_expires_at=? WHERE id=?",
                (_iso(fence.expires_at), job_id),
            )
            conn.execute("COMMIT")
        return fence.expires_at

    def complete_job(
        self,
        *,
        job_id: str,
        lease_token: str,
        result: dict[str, Any],
    ) -> None:
        result_json = _canonical_json(result)
        if len(result_json.encode("utf-8")) > 1_000_000:
            raise QueueError("Worker-Ergebnis überschreitet 1 MB")
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._leased_row(conn, job_id, lease_token, {JobState.RUNNING})
            conn.execute(
                """
                UPDATE development_jobs
                SET state='succeeded',finished_at=?,result_json=?,error='',lease_owner=NULL,
                    lease_token_hash=NULL,lease_expires_at=NULL
                WHERE id=?
                """,
                (_iso(_utcnow()), result_json, job_id),
            )
            self._event(conn, job_id, "job_succeeded", row["lease_owner"], {
                "fence_epoch": int(row["fence_epoch"]),
            })
            conn.execute("COMMIT")
        self._release_terminal_fence(row, lease_token, reason="job succeeded")

    def fail_job(
        self,
        *,
        job_id: str,
        lease_token: str,
        error: str,
        retryable: bool,
        retry_delay_seconds: int = 0,
    ) -> JobState:
        clean_error = (error or "Worker-Auftrag fehlgeschlagen")[:20_000]
        retry_delay_seconds = max(0, min(int(retry_delay_seconds), 86400))
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._leased_row(conn, job_id, lease_token, {JobState.LEASED, JobState.RUNNING})
            retry = retryable and int(row["attempts"]) < int(row["max_attempts"])
            target = JobState.QUEUED if retry else JobState.FAILED
            available = _utcnow() + timedelta(seconds=retry_delay_seconds)
            conn.execute(
                """
                UPDATE development_jobs
                SET state=?,available_at=?,finished_at=?,error=?,lease_owner=NULL,
                    lease_token_hash=NULL,lease_expires_at=NULL,fence_epoch=?
                WHERE id=?
                """,
                (
                    target.value,
                    _iso(available),
                    None if retry else _iso(_utcnow()),
                    clean_error,
                    None if retry else int(row["fence_epoch"]),
                    job_id,
                ),
            )
            self._event(conn, job_id, "job_requeued" if retry else "job_failed", row["lease_owner"], {
                "error": clean_error,
                "retryable": retryable,
                "fence_epoch": int(row["fence_epoch"]),
            })
            conn.execute("COMMIT")
        self._release_terminal_fence(row, lease_token, reason=target.value)
        return target

    def _leased_row(
        self,
        conn: sqlite3.Connection,
        job_id: str,
        lease_token: str,
        states: set[JobState],
    ) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM development_jobs WHERE id=?", (job_id,)).fetchone()
        if row is None or row["state"] not in {item.value for item in states}:
            conn.execute("ROLLBACK")
            raise LeaseError("Auftrag besitzt keinen passenden aktiven Lease")
        expires = _parse_time(row["lease_expires_at"])
        if expires is None or expires <= _utcnow():
            conn.execute("ROLLBACK")
            raise LeaseError("Queue-Lease ist abgelaufen")
        supplied = _token_hash(lease_token)
        expected = row["lease_token_hash"] or ""
        if not hmac.compare_digest(supplied, expected):
            conn.execute("ROLLBACK")
            raise LeaseError("Ungültiger Lease-Token")
        if row["fence_epoch"] is None:
            conn.execute("ROLLBACK")
            raise LeaseError("Externe Fence-Epoche fehlt")
        try:
            self.fence_store.assert_active(
                scope_key=row["repository_id"],
                job_id=job_id,
                epoch=int(row["fence_epoch"]),
                lease_token=lease_token,
            )
        except FenceError as exc:
            conn.execute("ROLLBACK")
            raise LeaseError(f"Externer Fence ist nicht mehr gültig: {exc}") from exc
        return row

    def _release_terminal_fence(
        self,
        row: sqlite3.Row,
        lease_token: str,
        *,
        reason: str,
    ) -> None:
        try:
            self.fence_store.release(
                scope_key=row["repository_id"],
                job_id=row["id"],
                epoch=int(row["fence_epoch"]),
                lease_token=lease_token,
                reason=reason,
            )
        except FenceError as exc:
            # The terminal queue state is authoritative. A failed release is
            # fail-safe because the fence remains occupied until its expiry.
            with self._lock, self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                self._event(conn, row["id"], "fence_release_failed", "queue", {
                    "error": str(exc)[:1000],
                    "fence_epoch": int(row["fence_epoch"]),
                })
                conn.execute("COMMIT")

    def _recover_expired_leases(self, conn: sqlite3.Connection, now: datetime) -> None:
        rows = conn.execute(
            """
            SELECT * FROM development_jobs
            WHERE state IN ('leased','running') AND lease_expires_at IS NOT NULL AND lease_expires_at<=?
            """,
            (_iso(now),),
        ).fetchall()
        for row in rows:
            fence = self.fence_store.current(row["repository_id"])
            if (
                fence is not None
                and fence.active
                and fence.job_id == row["id"]
                and row["fence_epoch"] is not None
                and fence.epoch == int(row["fence_epoch"])
                and fence.expires_at is not None
            ):
                conn.execute(
                    "UPDATE development_jobs SET lease_expires_at=? WHERE id=?",
                    (_iso(fence.expires_at), row["id"]),
                )
                self._event(conn, row["id"], "queue_lease_reconciled", "queue", {
                    "fence_epoch": fence.epoch,
                    "lease_expires_at": _iso(fence.expires_at),
                })
                continue
            retry = int(row["attempts"]) < int(row["max_attempts"])
            target = JobState.QUEUED if retry else JobState.FAILED
            conn.execute(
                """
                UPDATE development_jobs
                SET state=?,available_at=?,finished_at=?,error=?,lease_owner=NULL,
                    lease_token_hash=NULL,lease_expires_at=NULL,fence_epoch=?
                WHERE id=?
                """,
                (
                    target.value,
                    _iso(now),
                    None if retry else _iso(now),
                    "Worker-Lease und externer Fence sind abgelaufen",
                    None if retry else row["fence_epoch"],
                    row["id"],
                ),
            )
            self._event(conn, row["id"], "lease_expired", "queue", {
                "requeued": retry,
                "previous_fence_epoch": row["fence_epoch"],
            })

    @staticmethod
    def _event(
        conn: sqlite3.Connection,
        job_id: str | None,
        event_type: str,
        actor_id: str,
        details: dict[str, Any],
    ) -> None:
        conn.execute(
            "INSERT INTO job_events(job_id,event_type,actor_id,details_json,created_at) VALUES(?,?,?,?,?)",
            (job_id, event_type[:80], actor_id[:120], _canonical_json(details)[:20_000], _iso(_utcnow())),
        )

    @staticmethod
    def _repository_from_row(row: sqlite3.Row) -> RepositoryBinding:
        return RepositoryBinding(
            repository_id=row["id"],
            tenant_id=row["tenant_id"],
            workspace_id=row["workspace_id"],
            name=row["name"],
            repository_path=row["repository_path"],
            workspace_root=row["workspace_root"],
            state_path=row["state_path"],
            enabled=bool(row["enabled"]),
            created_by=row["created_by"],
            created_at=_parse_time(row["created_at"]),
        )

    @staticmethod
    def _job_from_row(row: sqlite3.Row) -> QueuedDevelopmentJob:
        pipeline = WorkerPipelineJob.model_validate_json(row["payload_json"])
        expected = _payload_hash(
            user_id=row["user_id"],
            tenant_id=row["tenant_id"],
            workspace_id=row["workspace_id"],
            repository_id=row["repository_id"],
            pipeline=pipeline,
        )
        if not hmac.compare_digest(expected, row["payload_sha256"]):
            raise QueueError("Persistierter Queue-Payload hat eine ungültige Prüfsumme")
        return QueuedDevelopmentJob(
            job_id=row["id"],
            user_id=row["user_id"],
            tenant_id=row["tenant_id"],
            workspace_id=row["workspace_id"],
            repository_id=row["repository_id"],
            state=JobState(row["state"]),
            priority=row["priority"],
            idempotency_key=row["idempotency_key"],
            payload_sha256=row["payload_sha256"],
            pipeline=pipeline,
            image=row["image"],
            memory_mb=row["memory_mb"],
            cpus=row["cpus"],
            pids_limit=row["pids_limit"],
            runtime_seconds=row["runtime_seconds"],
            attempts=row["attempts"],
            max_attempts=row["max_attempts"],
            created_at=_parse_time(row["created_at"]),
            available_at=_parse_time(row["available_at"]),
            lease_owner=row["lease_owner"],
            lease_expires_at=_parse_time(row["lease_expires_at"]),
            fence_epoch=int(row["fence_epoch"]) if row["fence_epoch"] is not None else None,
            started_at=_parse_time(row["started_at"]),
            finished_at=_parse_time(row["finished_at"]),
            result=json.loads(row["result_json"]) if row["result_json"] else None,
            error=row["error"] or "",
        )


class QueuedWorkerDispatcher:
    """Lease and execute one admitted worker pipeline.

    The optional executor callback is used by tests and alternative runtimes. The
    production default always constructs a container-required WorkerPipelineRunner
    from the repository binding stored by the queue.
    """

    def __init__(
        self,
        queue: DevelopmentJobQueue,
        *,
        worker_id: str,
        container_runtime: str = "docker",
        execute_pipeline: Callable[[RepositoryBinding, WorkerPipelineJob], dict[str, Any]] | None = None,
        heartbeat_interval_seconds: float | None = None,
        container_executor: DockerCommandExecutor | None = None,
    ) -> None:
        self.queue = queue
        self.worker_id = worker_id
        self.container_runtime = container_runtime
        self.execute_pipeline = execute_pipeline
        self.heartbeat_interval_seconds = heartbeat_interval_seconds
        self.container_executor = container_executor

    def run_once(self, *, lease_seconds: int = 300) -> DispatchResult | None:
        lease = self.queue.claim_next(worker_id=self.worker_id, lease_seconds=lease_seconds)
        if lease is None:
            return None
        effective_lease_seconds = min(
            86400, max(int(lease_seconds), int(lease.job.runtime_seconds) + 120)
        )
        stop = threading.Event()
        cancellation_requested = threading.Event()
        heartbeat_errors: list[Exception] = []
        interval = self.heartbeat_interval_seconds
        if interval is None:
            interval = max(5.0, min(30.0, effective_lease_seconds / 3))

        def keep_lease_alive() -> None:
            while not stop.wait(interval):
                try:
                    self.queue.heartbeat(
                        job_id=lease.job.job_id,
                        lease_token=lease.lease_token,
                        lease_seconds=effective_lease_seconds,
                    )
                except Exception as exc:
                    heartbeat_errors.append(exc)
                    cancellation_requested.set()
                    stop.set()
                    return

        heartbeat_thread: threading.Thread | None = None

        def execution_guard(stage: str) -> None:
            if cancellation_requested.is_set() or heartbeat_errors:
                detail = heartbeat_errors[-1] if heartbeat_errors else "Lease wurde widerrufen"
                raise LeaseError(
                    f"Lease-Heartbeat vor Phase {stage} fehlgeschlagen: {detail}"
                )
            self.queue.assert_lease_active(
                job_id=lease.job.job_id,
                lease_token=lease.lease_token,
            )

        try:
            self.queue._validate_repository_binding(lease.repository, check_git=True)
            self.queue.heartbeat(
                job_id=lease.job.job_id, lease_token=lease.lease_token,
                lease_seconds=effective_lease_seconds,
            )
            self.queue.start_job(job_id=lease.job.job_id, lease_token=lease.lease_token)
            heartbeat_thread = threading.Thread(
                target=keep_lease_alive,
                name=f"tankai-lease-{lease.job.job_id[:8]}",
                daemon=True,
            )
            heartbeat_thread.start()
            execution_guard("dispatcher_before_pipeline")
            if self.execute_pipeline is None:
                result = self._execute_default(
                    lease.repository,
                    lease.job.pipeline,
                    execution_guard=execution_guard,
                    container_metadata={
                        "job_id": lease.job.job_id,
                        "repository_id": lease.repository.repository_id,
                        "workspace_id": lease.job.workspace_id,
                        "tenant_id": lease.job.tenant_id,
                        "fence_epoch": str(lease.fence_epoch),
                        "worker_id": self.worker_id,
                    },
                )
            else:
                result = self.execute_pipeline(lease.repository, lease.job.pipeline)
            execution_guard("dispatcher_after_pipeline")
            self.queue.complete_job(
                job_id=lease.job.job_id,
                lease_token=lease.lease_token,
                result=result,
            )
            return DispatchResult(lease.job.job_id, JobState.SUCCEEDED, result=result)
        except Exception as exc:
            try:
                state = self.queue.fail_job(
                    job_id=lease.job.job_id,
                    lease_token=lease.lease_token,
                    error=str(exc),
                    retryable=False,
                )
            except Exception as persist_exc:
                raise QueueError(
                    f"Worker fehlgeschlagen und Queue-Status konnte nicht gespeichert werden: {persist_exc}"
                ) from exc
            return DispatchResult(lease.job.job_id, state, error=str(exc))
        finally:
            stop.set()
            if heartbeat_thread is not None:
                heartbeat_thread.join(timeout=max(1.0, interval * 2))

    def _execute_default(
        self,
        repository: RepositoryBinding,
        pipeline: WorkerPipelineJob,
        *,
        execution_guard: Callable[[str], None],
        container_metadata: dict[str, str],
    ) -> dict[str, Any]:
        orchestrator = DevelopmentOrchestrator(ProjectStateStore(repository.state_path))
        manager = GitWorkspaceManager(repository.repository_path, repository.workspace_root)
        result = WorkerPipelineRunner(
            orchestrator,
            manager,
            container_executor=(
                self.container_executor
                or DockerCommandExecutor(self.container_runtime, require_rootless=True)
            ),
            require_container_isolation=True,
            execution_guard=execution_guard,
            container_metadata=container_metadata,
        ).run(pipeline)
        return {
            "run": result.run.model_dump(mode="json"),
            "workspace": {
                "path": str(result.workspace.path),
                "branch": result.workspace.branch,
                "base_commit": result.workspace.base_commit,
            },
        }
