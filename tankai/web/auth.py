"""Persistente Benutzer-, Sitzungs- und Workspace-Authentifizierung für TankAI.

Die Datenbank ist die verbindliche Quelle. Clientseitige Nutzer- oder Workspace-IDs
werden niemals ungeprüft für Datenzugriffe verwendet.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import secrets
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Protocol

from tankai.core.llm import LLMRateLimitExceeded
from uuid import UUID, uuid4

_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_NAME_RE = re.compile(r"[^a-z0-9_-]+")
_ALLOWED_ROLES = {"owner", "admin", "member"}
AGENT_TOKEN_SCOPES = frozenset(
    {"repositories:read", "jobs:submit", "jobs:read", "jobs:cancel"}
)
_AGENT_TOKEN_PREFIX = "tkai_v1_"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _safe_slug(value: str, fallback: str = "workspace") -> str:
    slug = _NAME_RE.sub("-", value.strip().lower()).strip("-_")
    return (slug or fallback)[:64]


def normalize_email(email: str) -> str:
    normalized = email.strip().casefold()
    if len(normalized) > 254 or not _EMAIL_RE.match(normalized):
        raise ValueError("Ungültige E-Mail-Adresse")
    return normalized


def validate_password(password: str) -> None:
    if not isinstance(password, str):
        raise ValueError("Passwort muss Text sein")
    if len(password) < 12:
        raise ValueError("Passwort muss mindestens 12 Zeichen enthalten")
    if len(password) > 256:
        raise ValueError("Passwort ist zu lang")
    if password.strip() != password:
        raise ValueError("Passwort darf nicht mit Leerzeichen beginnen oder enden")


def hash_password(password: str) -> str:
    validate_password(password)
    salt = secrets.token_bytes(16)
    n, r, p = 2**14, 8, 1
    derived = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=32)
    return f"scrypt${n}${r}${p}${salt.hex()}${derived.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n_raw, r_raw, p_raw, salt_hex, digest_hex = encoded.split("$", 5)
        if algorithm != "scrypt":
            return False
        n, r, p = int(n_raw), int(r_raw), int(p_raw)
        if n < 2**14 or n > 2**18 or r < 1 or p < 1:
            return False
        expected = bytes.fromhex(digest_hex)
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=bytes.fromhex(salt_hex),
            n=n,
            r=r,
            p=p,
            dklen=len(expected),
        )
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError, MemoryError):
        return False


_DUMMY_PASSWORD_HASH = hash_password("invalid-password-placeholder")

@dataclass(frozen=True)
class WorkspaceAccess:
    id: str
    tenant_id: str
    name: str
    slug: str
    role: str


@dataclass(frozen=True)
class AuthContext:
    session_id: str
    user_id: str
    email: str
    display_name: str
    tenant_id: str
    workspace_id: str
    workspace_name: str
    role: str
    csrf_token: str
    expires_at: datetime


class AgentManagementActor(Protocol):
    """Minimal server-side identity required for service-agent administration."""

    user_id: str
    tenant_id: str
    workspace_id: str
    role: str


@dataclass(frozen=True)
class SessionCreated:
    token: str
    context: AuthContext


@dataclass(frozen=True)
class ServiceAgent:
    agent_id: str
    tenant_id: str
    workspace_id: str
    name: str
    description: str
    owner_user_id: str
    is_active: bool
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class AgentAuthContext:
    agent_id: str
    token_id: str
    agent_name: str
    owner_user_id: str
    tenant_id: str
    workspace_id: str
    workspace_name: str
    owner_role: str
    scopes: frozenset[str]
    repository_ids: frozenset[str]
    expires_at: datetime

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes


@dataclass(frozen=True)
class AgentTokenCreated:
    token_id: str
    token: str
    token_prefix: str
    scopes: tuple[str, ...]
    repository_ids: tuple[str, ...]
    created_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class AgentTokenInfo:
    token_id: str
    token_prefix: str
    scopes: tuple[str, ...]
    repository_ids: tuple[str, ...]
    label: str
    created_at: datetime
    expires_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None


class AuthStore:
    """SQLite-basierter Auth-Store mit widerrufbaren, opaken Sessions."""

    SCHEMA_VERSION = 3

    def __init__(self, path: str | Path, *, session_hours: int = 12) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        self.session_hours = max(1, min(int(session_hours), 24 * 30))
        self._lock = threading.RLock()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    def _init_schema(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(
                """
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS auth_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    email TEXT NOT NULL UNIQUE,
                    display_name TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0,1)),
                    created_at TEXT NOT NULL,
                    password_changed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tenants (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    created_by TEXT NOT NULL REFERENCES users(id)
                );
                CREATE TABLE IF NOT EXISTS workspaces (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    slug TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    created_by TEXT NOT NULL REFERENCES users(id),
                    UNIQUE(tenant_id, slug)
                );
                CREATE TABLE IF NOT EXISTS memberships (
                    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
                    role TEXT NOT NULL CHECK (role IN ('owner','admin','member')),
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(user_id, workspace_id)
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    token_hash TEXT NOT NULL UNIQUE,
                    csrf_hash TEXT NOT NULL,
                    csrf_token TEXT NOT NULL,
                    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    active_workspace_id TEXT NOT NULL REFERENCES workspaces(id),
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    revoked_at TEXT,
                    user_agent_hash TEXT NOT NULL DEFAULT '',
                    ip_hash TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
                CREATE INDEX IF NOT EXISTS idx_sessions_expiry ON sessions(expires_at);
                CREATE TABLE IF NOT EXISTS audit_events (
                    id TEXT PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    user_id TEXT,
                    workspace_id TEXT,
                    success INTEGER NOT NULL CHECK (success IN (0,1)),
                    details TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS provider_call_events (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    occurred_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_provider_calls_bucket
                    ON provider_call_events(user_id, provider, occurred_at);
                CREATE TABLE IF NOT EXISTS service_agents (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
                    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    owner_user_id TEXT NOT NULL REFERENCES users(id),
                    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0,1)),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(workspace_id, name)
                );
                CREATE INDEX IF NOT EXISTS idx_service_agents_workspace
                    ON service_agents(tenant_id, workspace_id, is_active);
                CREATE TABLE IF NOT EXISTS agent_tokens (
                    id TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL REFERENCES service_agents(id) ON DELETE CASCADE,
                    token_prefix TEXT NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE,
                    scopes_json TEXT NOT NULL,
                    repository_ids_json TEXT NOT NULL,
                    label TEXT NOT NULL DEFAULT '',
                    created_by TEXT NOT NULL REFERENCES users(id),
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    last_used_at TEXT,
                    revoked_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_agent_tokens_agent
                    ON agent_tokens(agent_id, revoked_at, expires_at);
                CREATE TABLE IF NOT EXISTS agent_job_grants (
                    agent_id TEXT NOT NULL REFERENCES service_agents(id) ON DELETE CASCADE,
                    job_id TEXT NOT NULL,
                    repository_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(agent_id, job_id)
                );
                CREATE INDEX IF NOT EXISTS idx_agent_job_grants_recent
                    ON agent_job_grants(agent_id, created_at DESC);
                INSERT OR REPLACE INTO auth_meta(key,value) VALUES('schema_version','3');
                COMMIT;
                """
            )
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def user_count(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()
            return int(row["n"])

    def create_user_with_tenant(
        self,
        *,
        email: str,
        password: str,
        display_name: str,
        tenant_name: str,
        workspace_name: str = "Standard",
    ) -> tuple[str, str, str]:
        email_n = normalize_email(email)
        validate_password(password)
        display = display_name.strip()
        tenant = tenant_name.strip()
        workspace = workspace_name.strip()
        if not display or len(display) > 120:
            raise ValueError("Anzeigename fehlt oder ist zu lang")
        if not tenant or len(tenant) > 120:
            raise ValueError("Mandantenname fehlt oder ist zu lang")
        if not workspace or len(workspace) > 120:
            raise ValueError("Workspace-Name fehlt oder ist zu lang")

        user_id = str(uuid4())
        tenant_id = str(uuid4())
        workspace_id = str(uuid4())
        now = _iso(_utcnow())
        password_encoded = hash_password(password)
        slug = _safe_slug(workspace)
        with self._lock, self._connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "INSERT INTO users(id,email,display_name,password_hash,created_at,password_changed_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (user_id, email_n, display, password_encoded, now, now),
                )
                conn.execute(
                    "INSERT INTO tenants(id,name,created_at,created_by) VALUES(?,?,?,?)",
                    (tenant_id, tenant, now, user_id),
                )
                conn.execute(
                    "INSERT INTO workspaces(id,tenant_id,name,slug,created_at,created_by) "
                    "VALUES(?,?,?,?,?,?)",
                    (workspace_id, tenant_id, workspace, slug, now, user_id),
                )
                conn.execute(
                    "INSERT INTO memberships(user_id,workspace_id,role,created_at) VALUES(?,?,?,?)",
                    (user_id, workspace_id, "owner", now),
                )
                conn.execute("COMMIT")
            except sqlite3.IntegrityError as exc:
                conn.execute("ROLLBACK")
                raise ValueError("E-Mail-Adresse existiert bereits") from exc
            except Exception:
                conn.execute("ROLLBACK")
                raise
        self.audit("user_created", user_id=user_id, workspace_id=workspace_id, success=True)
        return user_id, tenant_id, workspace_id

    def add_user_to_workspace(self, *, user_id: str, workspace_id: str, role: str) -> None:
        role_n = role.strip().lower()
        if role_n not in _ALLOWED_ROLES:
            raise ValueError("Ungültige Rolle")
        now = _iso(_utcnow())
        with self._lock, self._connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                user = conn.execute("SELECT id FROM users WHERE id=? AND is_active=1", (user_id,)).fetchone()
                workspace = conn.execute("SELECT id FROM workspaces WHERE id=?", (workspace_id,)).fetchone()
                if not user or not workspace:
                    raise ValueError("Nutzer oder Workspace nicht gefunden")
                conn.execute(
                    "INSERT OR REPLACE INTO memberships(user_id,workspace_id,role,created_at) VALUES(?,?,?,?)",
                    (user_id, workspace_id, role_n, now),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def create_workspace(self, *, actor: AuthContext, name: str) -> WorkspaceAccess:
        clean = name.strip()
        if not clean or len(clean) > 120:
            raise ValueError("Workspace-Name fehlt oder ist zu lang")
        if actor.role not in {"owner", "admin"}:
            raise PermissionError("Nur Owner oder Admins dürfen Workspaces erstellen")
        workspace_id = str(uuid4())
        now = _iso(_utcnow())
        base_slug = _safe_slug(clean)
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                slug = base_slug
                counter = 1
                while conn.execute(
                    "SELECT 1 FROM workspaces WHERE tenant_id=? AND slug=?",
                    (actor.tenant_id, slug),
                ).fetchone():
                    counter += 1
                    slug = f"{base_slug[:56]}-{counter}"
                conn.execute(
                    "INSERT INTO workspaces(id,tenant_id,name,slug,created_at,created_by) VALUES(?,?,?,?,?,?)",
                    (workspace_id, actor.tenant_id, clean, slug, now, actor.user_id),
                )
                conn.execute(
                    "INSERT INTO memberships(user_id,workspace_id,role,created_at) VALUES(?,?,?,?)",
                    (actor.user_id, workspace_id, actor.role, now),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        self.audit("workspace_created", user_id=actor.user_id, workspace_id=workspace_id, success=True)
        return WorkspaceAccess(workspace_id, actor.tenant_id, clean, slug, actor.role)

    def list_workspaces(self, user_id: str) -> list[WorkspaceAccess]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT w.id,w.tenant_id,w.name,w.slug,m.role
                FROM memberships m
                JOIN workspaces w ON w.id=m.workspace_id
                WHERE m.user_id=?
                ORDER BY w.name COLLATE NOCASE, w.id
                """,
                (user_id,),
            ).fetchall()
        return [WorkspaceAccess(**dict(row)) for row in rows]

    def authenticate(
        self,
        *,
        email: str,
        password: str,
        user_agent: str = "",
        client_ip: str = "",
    ) -> SessionCreated | None:
        try:
            email_n = normalize_email(email)
        except ValueError:
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id,email,display_name,password_hash,is_active FROM users WHERE email=?",
                (email_n,),
            ).fetchone()
        # Konstante Ersatzarbeit reduziert triviale User-Enumeration per Timing.
        encoded = row["password_hash"] if row else _DUMMY_PASSWORD_HASH
        valid = verify_password(password, encoded)
        if not row or not row["is_active"] or not valid:
            self.audit("login", user_id=row["id"] if row else None, success=False)
            return None
        workspaces = self.list_workspaces(row["id"])
        if not workspaces:
            self.audit("login", user_id=row["id"], success=False, details='{"reason":"no_workspace"}')
            return None
        raw_token = secrets.token_urlsafe(48)
        csrf = secrets.token_urlsafe(32)
        session_id = str(uuid4())
        created = _utcnow()
        expires = created + timedelta(hours=self.session_hours)
        active = workspaces[0]
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sessions(
                    id,token_hash,csrf_hash,csrf_token,user_id,active_workspace_id,
                    created_at,expires_at,last_seen_at,user_agent_hash,ip_hash
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    session_id,
                    _token_hash(raw_token),
                    _token_hash(csrf),
                    csrf,
                    row["id"],
                    active.id,
                    _iso(created),
                    _iso(expires),
                    _iso(created),
                    _token_hash(user_agent) if user_agent else "",
                    _token_hash(client_ip) if client_ip else "",
                ),
            )
        self.audit("login", user_id=row["id"], workspace_id=active.id, success=True)
        context = AuthContext(
            session_id=session_id,
            user_id=row["id"],
            email=row["email"],
            display_name=row["display_name"],
            tenant_id=active.tenant_id,
            workspace_id=active.id,
            workspace_name=active.name,
            role=active.role,
            csrf_token=csrf,
            expires_at=expires,
        )
        return SessionCreated(raw_token, context)

    def resolve_session(self, token: str, *, touch: bool = True) -> AuthContext | None:
        if not token or len(token) > 512:
            return None
        now = _utcnow()
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT s.id AS session_id,s.csrf_token,s.expires_at,
                       u.id AS user_id,u.email,u.display_name,u.is_active,
                       w.id AS workspace_id,w.tenant_id,w.name AS workspace_name,m.role
                FROM sessions s
                JOIN users u ON u.id=s.user_id
                JOIN workspaces w ON w.id=s.active_workspace_id
                JOIN memberships m ON m.user_id=u.id AND m.workspace_id=w.id
                WHERE s.token_hash=? AND s.revoked_at IS NULL
                """,
                (_token_hash(token),),
            ).fetchone()
            if not row or not row["is_active"] or _parse_time(row["expires_at"]) <= now:
                return None
            if touch:
                conn.execute("UPDATE sessions SET last_seen_at=? WHERE id=?", (_iso(now), row["session_id"]))
        return AuthContext(
            session_id=row["session_id"],
            user_id=row["user_id"],
            email=row["email"],
            display_name=row["display_name"],
            tenant_id=row["tenant_id"],
            workspace_id=row["workspace_id"],
            workspace_name=row["workspace_name"],
            role=row["role"],
            csrf_token=row["csrf_token"],
            expires_at=_parse_time(row["expires_at"]),
        )

    def verify_csrf(self, context: AuthContext, supplied: str) -> bool:
        if not supplied or len(supplied) > 512:
            return False
        return hmac.compare_digest(_token_hash(supplied), _token_hash(context.csrf_token))

    def select_workspace(self, context: AuthContext, workspace_id: str) -> AuthContext:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT w.id,w.tenant_id,w.name,w.slug,m.role
                FROM memberships m JOIN workspaces w ON w.id=m.workspace_id
                WHERE m.user_id=? AND w.id=?
                """,
                (context.user_id, workspace_id),
            ).fetchone()
            if not row:
                self.audit("workspace_select", user_id=context.user_id, workspace_id=workspace_id, success=False)
                raise PermissionError("Kein Zugriff auf diesen Workspace")
            conn.execute(
                "UPDATE sessions SET active_workspace_id=?,last_seen_at=? WHERE id=? AND user_id=?",
                (workspace_id, _iso(_utcnow()), context.session_id, context.user_id),
            )
        self.audit("workspace_select", user_id=context.user_id, workspace_id=workspace_id, success=True)
        refreshed = self.resolve_session_by_id(context.session_id)
        if refreshed is None:
            raise RuntimeError("Session konnte nach Workspace-Wechsel nicht geladen werden")
        return refreshed

    def resolve_session_by_id(self, session_id: str) -> AuthContext | None:
        # Direkte, sichere Auflösung ohne Kenntnis des Roh-Tokens.
        now = _utcnow()
        with self._connect() as conn:
            record = conn.execute(
                """
                SELECT s.id AS session_id,s.csrf_token,s.expires_at,
                       u.id AS user_id,u.email,u.display_name,u.is_active,
                       w.id AS workspace_id,w.tenant_id,w.name AS workspace_name,m.role
                FROM sessions s
                JOIN users u ON u.id=s.user_id
                JOIN workspaces w ON w.id=s.active_workspace_id
                JOIN memberships m ON m.user_id=u.id AND m.workspace_id=w.id
                WHERE s.id=? AND s.revoked_at IS NULL
                """,
                (session_id,),
            ).fetchone()
        if not record or not record["is_active"] or _parse_time(record["expires_at"]) <= now:
            return None
        return AuthContext(
            session_id=record["session_id"],
            user_id=record["user_id"],
            email=record["email"],
            display_name=record["display_name"],
            tenant_id=record["tenant_id"],
            workspace_id=record["workspace_id"],
            workspace_name=record["workspace_name"],
            role=record["role"],
            csrf_token=record["csrf_token"],
            expires_at=_parse_time(record["expires_at"]),
        )

    def revoke_session(self, session_id: str, *, user_id: str | None = None) -> None:
        with self._lock, self._connect() as conn:
            if user_id:
                conn.execute(
                    "UPDATE sessions SET revoked_at=? WHERE id=? AND user_id=? AND revoked_at IS NULL",
                    (_iso(_utcnow()), session_id, user_id),
                )
            else:
                conn.execute(
                    "UPDATE sessions SET revoked_at=? WHERE id=? AND revoked_at IS NULL",
                    (_iso(_utcnow()), session_id),
                )

    def revoke_all_user_sessions(self, user_id: str) -> int:
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "UPDATE sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
                (_iso(_utcnow()), user_id),
            )
            return int(cursor.rowcount)

    def cleanup_expired_sessions(self) -> int:
        with self._lock, self._connect() as conn:
            cursor = conn.execute("DELETE FROM sessions WHERE expires_at<=? OR revoked_at IS NOT NULL", (_iso(_utcnow()),))
            return int(cursor.rowcount)

    def audit(
        self,
        event_type: str,
        *,
        user_id: str | None = None,
        workspace_id: str | None = None,
        success: bool,
        details: str = "{}",
    ) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO audit_events(id,event_type,user_id,workspace_id,success,details,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (str(uuid4()), event_type[:80], user_id, workspace_id, int(success), details[:4000], _iso(_utcnow())),
            )

    def consume_provider_call_event(
        self,
        *,
        user_id: str,
        provider: str,
        limit: int,
        window_seconds: int,
        now: datetime | None = None,
    ) -> int | None:
        """Atomar einen Provider-Call verbuchen oder Retry-After-Sekunden liefern."""
        provider_n = provider.strip().lower()
        if not re.fullmatch(r"[a-z0-9_.-]{1,64}", provider_n):
            raise ValueError("Ungültiger Provider für Rate-Limit")
        if not user_id or len(user_id) > 128:
            raise ValueError("Ungültige Nutzer-ID für Rate-Limit")
        if limit < 1 or limit > 240:
            raise ValueError("Provider-Rate-Limit muss zwischen 1 und 240 liegen")
        if window_seconds < 1 or window_seconds > 3600:
            raise ValueError("Provider-Rate-Fenster muss zwischen 1 und 3600 Sekunden liegen")

        current = (now or _utcnow()).astimezone(timezone.utc)
        cutoff = current - timedelta(seconds=window_seconds)
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "DELETE FROM provider_call_events "
                    "WHERE user_id=? AND provider=? AND occurred_at<?",
                    (user_id, provider_n, _iso(cutoff)),
                )
                row = conn.execute(
                    "SELECT COUNT(*) AS c, MIN(occurred_at) AS oldest "
                    "FROM provider_call_events WHERE user_id=? AND provider=?",
                    (user_id, provider_n),
                ).fetchone()
                count = int(row["c"] if row else 0)
                if count >= limit:
                    oldest_raw = str(row["oldest"] or "") if row else ""
                    oldest = _parse_time(oldest_raw) if oldest_raw else current
                    retry_after = max(
                        1,
                        math.ceil(
                            (oldest + timedelta(seconds=window_seconds) - current).total_seconds()
                        ),
                    )
                    conn.execute("COMMIT")
                    return retry_after
                conn.execute(
                    "INSERT INTO provider_call_events(id,user_id,provider,occurred_at) "
                    "VALUES(?,?,?,?)",
                    (str(uuid4()), user_id, provider_n, _iso(current)),
                )
                conn.execute("COMMIT")
                return None
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def set_password(self, user_id: str, password: str) -> None:
        encoded = hash_password(password)
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "UPDATE users SET password_hash=?,password_changed_at=? WHERE id=?",
                (encoded, _iso(_utcnow()), user_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Nutzer nicht gefunden")
        self.revoke_all_user_sessions(user_id)

    def get_user_id_by_email(self, email: str) -> str | None:
        email_n = normalize_email(email)
        with self._connect() as conn:
            row = conn.execute("SELECT id FROM users WHERE email=?", (email_n,)).fetchone()
            return str(row["id"]) if row else None

    def list_users(self) -> list[dict[str, str]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT id,email,display_name,created_at FROM users ORDER BY email").fetchall()
        return [dict(row) for row in rows]

    def workspace_access(self, user_id: str, workspace_id: str) -> WorkspaceAccess | None:
        """Return the server-side membership binding for one user/workspace pair."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT w.id,w.tenant_id,w.name,w.slug,m.role
                FROM memberships m
                JOIN workspaces w ON w.id=m.workspace_id
                JOIN users u ON u.id=m.user_id
                WHERE m.user_id=? AND w.id=? AND u.is_active=1
                """,
                (user_id, workspace_id),
            ).fetchone()
        return WorkspaceAccess(**dict(row)) if row else None

    def membership_exists(self, user_id: str, workspace_id: str) -> bool:
        return self.workspace_access(user_id, workspace_id) is not None

    def workspace_ids(self) -> Iterable[str]:
        with self._connect() as conn:
            rows = conn.execute("SELECT id FROM workspaces ORDER BY id").fetchall()
        return [str(row["id"]) for row in rows]

    @staticmethod
    def _require_agent_admin(actor: AgentManagementActor) -> None:
        if actor.role not in {"owner", "admin"}:
            raise PermissionError("Nur Owner oder Admins dürfen KI-Agenten verwalten")

    @staticmethod
    def _service_agent_from_row(row: sqlite3.Row) -> ServiceAgent:
        return ServiceAgent(
            agent_id=str(row["id"]),
            tenant_id=str(row["tenant_id"]),
            workspace_id=str(row["workspace_id"]),
            name=str(row["name"]),
            description=str(row["description"]),
            owner_user_id=str(row["owner_user_id"]),
            is_active=bool(row["is_active"]),
            created_at=_parse_time(str(row["created_at"])),
            updated_at=_parse_time(str(row["updated_at"])),
        )

    @staticmethod
    def _normalize_agent_scopes(scopes: Iterable[str]) -> tuple[str, ...]:
        if isinstance(scopes, (str, bytes)):
            raise ValueError("Agenten-Scopes müssen als Liste übergeben werden")
        normalized: set[str] = set()
        for scope in scopes:
            if not isinstance(scope, str):
                raise ValueError("Agenten-Scopes müssen Textwerte sein")
            normalized.add(scope.strip().lower())
        if not normalized:
            raise ValueError("Mindestens ein Agenten-Scope ist erforderlich")
        invalid = sorted(normalized - AGENT_TOKEN_SCOPES)
        if invalid:
            raise ValueError("Unbekannte Agenten-Scopes: " + ", ".join(invalid))
        if "jobs:submit" in normalized and "jobs:read" not in normalized:
            raise ValueError("jobs:submit benötigt zusätzlich jobs:read")
        if "jobs:cancel" in normalized and "jobs:read" not in normalized:
            raise ValueError("jobs:cancel benötigt zusätzlich jobs:read")
        return tuple(sorted(normalized))

    @staticmethod
    def _normalize_repository_ids(repository_ids: Iterable[str]) -> tuple[str, ...]:
        if isinstance(repository_ids, (str, bytes)):
            raise ValueError("Repository-IDs müssen als Liste übergeben werden")
        normalized: set[str] = set()
        for repository_id in repository_ids:
            if not isinstance(repository_id, str):
                raise ValueError("Repository-IDs müssen Textwerte sein")
            value = repository_id.strip()
            try:
                UUID(value)
            except ValueError as exc:
                raise ValueError("Ungültige Repository-ID") from exc
            normalized.add(value)
        if not normalized:
            raise ValueError("Mindestens ein Repository muss freigegeben werden")
        if len(normalized) > 50:
            raise ValueError("Ein Agenten-Token darf höchstens 50 Repositories freigeben")
        return tuple(sorted(normalized))

    def create_service_agent(
        self,
        *,
        actor: AgentManagementActor,
        name: str,
        description: str = "",
    ) -> ServiceAgent:
        self._require_agent_admin(actor)
        clean_name = name.strip()
        clean_description = description.strip()
        if not clean_name or len(clean_name) > 120:
            raise ValueError("Agentenname fehlt oder ist zu lang")
        if len(clean_description) > 1_000:
            raise ValueError("Agentenbeschreibung ist zu lang")
        now = _utcnow()
        agent_id = str(uuid4())
        with self._lock, self._connect() as conn:
            active_count = conn.execute(
                "SELECT COUNT(*) AS n FROM service_agents "
                "WHERE workspace_id=? AND is_active=1",
                (actor.workspace_id,),
            ).fetchone()
            if int(active_count["n"] if active_count else 0) >= 100:
                raise ValueError("Workspace-Limit von 100 aktiven KI-Agenten erreicht")
            try:
                conn.execute(
                    """
                    INSERT INTO service_agents(
                        id,tenant_id,workspace_id,name,description,owner_user_id,
                        is_active,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        agent_id,
                        actor.tenant_id,
                        actor.workspace_id,
                        clean_name,
                        clean_description,
                        actor.user_id,
                        1,
                        _iso(now),
                        _iso(now),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("Agentenname existiert in diesem Workspace bereits") from exc
        self.audit(
            "service_agent_created",
            user_id=actor.user_id,
            workspace_id=actor.workspace_id,
            success=True,
            details=json.dumps({"agent_id": agent_id}, separators=(",", ":")),
        )
        return ServiceAgent(
            agent_id=agent_id,
            tenant_id=actor.tenant_id,
            workspace_id=actor.workspace_id,
            name=clean_name,
            description=clean_description,
            owner_user_id=actor.user_id,
            is_active=True,
            created_at=now,
            updated_at=now,
        )

    def list_service_agents(
        self, *, actor: AgentManagementActor
    ) -> list[ServiceAgent]:
        self._require_agent_admin(actor)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM service_agents WHERE tenant_id=? AND workspace_id=? "
                "ORDER BY name COLLATE NOCASE,id",
                (actor.tenant_id, actor.workspace_id),
            ).fetchall()
        return [self._service_agent_from_row(row) for row in rows]

    def _service_agent_for_actor(
        self,
        *,
        actor: AgentManagementActor,
        agent_id: str,
        active_only: bool = False,
    ) -> ServiceAgent:
        self._require_agent_admin(actor)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM service_agents WHERE id=? AND tenant_id=? AND workspace_id=?",
                (agent_id, actor.tenant_id, actor.workspace_id),
            ).fetchone()
        if row is None:
            raise ValueError("KI-Agent nicht gefunden")
        agent = self._service_agent_from_row(row)
        if active_only and not agent.is_active:
            raise ValueError("KI-Agent ist deaktiviert")
        return agent

    def create_agent_token(
        self,
        *,
        actor: AgentManagementActor,
        agent_id: str,
        scopes: Iterable[str],
        repository_ids: Iterable[str],
        expires_in_days: int = 30,
        label: str = "",
    ) -> AgentTokenCreated:
        agent = self._service_agent_for_actor(
            actor=actor, agent_id=agent_id, active_only=True
        )
        normalized_scopes = self._normalize_agent_scopes(scopes)
        normalized_repositories = self._normalize_repository_ids(repository_ids)
        if not isinstance(expires_in_days, int) or isinstance(expires_in_days, bool):
            raise ValueError("Token-Laufzeit muss eine Ganzzahl sein")
        if expires_in_days < 1 or expires_in_days > 365:
            raise ValueError("Token-Laufzeit muss zwischen 1 und 365 Tagen liegen")
        clean_label = label.strip()
        if len(clean_label) > 120:
            raise ValueError("Token-Bezeichnung ist zu lang")

        token_id = str(uuid4())
        raw_token = _AGENT_TOKEN_PREFIX + secrets.token_urlsafe(48)
        token_prefix = raw_token[:20]
        created = _utcnow()
        expires = created + timedelta(days=expires_in_days)
        with self._lock, self._connect() as conn:
            active_count = conn.execute(
                "SELECT COUNT(*) AS n FROM agent_tokens "
                "WHERE agent_id=? AND revoked_at IS NULL AND expires_at>?",
                (agent.agent_id, _iso(created)),
            ).fetchone()
            if int(active_count["n"] if active_count else 0) >= 20:
                raise ValueError("Agenten-Limit von 20 aktiven Tokens erreicht")
            conn.execute(
                """
                INSERT INTO agent_tokens(
                    id,agent_id,token_prefix,token_hash,scopes_json,repository_ids_json,
                    label,created_by,created_at,expires_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    token_id,
                    agent.agent_id,
                    token_prefix,
                    _token_hash(raw_token),
                    json.dumps(normalized_scopes, separators=(",", ":")),
                    json.dumps(normalized_repositories, separators=(",", ":")),
                    clean_label,
                    actor.user_id,
                    _iso(created),
                    _iso(expires),
                ),
            )
        self.audit(
            "agent_token_created",
            user_id=actor.user_id,
            workspace_id=actor.workspace_id,
            success=True,
            details=json.dumps(
                {"agent_id": agent.agent_id, "token_id": token_id},
                separators=(",", ":"),
            ),
        )
        return AgentTokenCreated(
            token_id=token_id,
            token=raw_token,
            token_prefix=token_prefix,
            scopes=normalized_scopes,
            repository_ids=normalized_repositories,
            created_at=created,
            expires_at=expires,
        )

    def list_agent_tokens(
        self, *, actor: AgentManagementActor, agent_id: str
    ) -> list[AgentTokenInfo]:
        agent = self._service_agent_for_actor(actor=actor, agent_id=agent_id)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM agent_tokens WHERE agent_id=? ORDER BY created_at DESC,id",
                (agent.agent_id,),
            ).fetchall()
        return [
            AgentTokenInfo(
                token_id=str(row["id"]),
                token_prefix=str(row["token_prefix"]),
                scopes=tuple(json.loads(row["scopes_json"])),
                repository_ids=tuple(json.loads(row["repository_ids_json"])),
                label=str(row["label"]),
                created_at=_parse_time(str(row["created_at"])),
                expires_at=_parse_time(str(row["expires_at"])),
                last_used_at=(
                    _parse_time(str(row["last_used_at"]))
                    if row["last_used_at"]
                    else None
                ),
                revoked_at=(
                    _parse_time(str(row["revoked_at"])) if row["revoked_at"] else None
                ),
            )
            for row in rows
        ]

    def revoke_agent_token(
        self, *, actor: AgentManagementActor, agent_id: str, token_id: str
    ) -> None:
        agent = self._service_agent_for_actor(actor=actor, agent_id=agent_id)
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "UPDATE agent_tokens SET revoked_at=? "
                "WHERE id=? AND agent_id=? AND revoked_at IS NULL",
                (_iso(_utcnow()), token_id, agent.agent_id),
            )
        if cursor.rowcount != 1:
            raise ValueError("Aktiver Agenten-Token nicht gefunden")
        self.audit(
            "agent_token_revoked",
            user_id=actor.user_id,
            workspace_id=actor.workspace_id,
            success=True,
            details=json.dumps(
                {"agent_id": agent.agent_id, "token_id": token_id},
                separators=(",", ":"),
            ),
        )

    def deactivate_service_agent(
        self, *, actor: AgentManagementActor, agent_id: str
    ) -> None:
        agent = self._service_agent_for_actor(actor=actor, agent_id=agent_id)
        now = _iso(_utcnow())
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "UPDATE service_agents SET is_active=0,updated_at=? WHERE id=?",
                    (now, agent.agent_id),
                )
                conn.execute(
                    "UPDATE agent_tokens SET revoked_at=? "
                    "WHERE agent_id=? AND revoked_at IS NULL",
                    (now, agent.agent_id),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        self.audit(
            "service_agent_deactivated",
            user_id=actor.user_id,
            workspace_id=actor.workspace_id,
            success=True,
            details=json.dumps({"agent_id": agent.agent_id}, separators=(",", ":")),
        )

    def resolve_agent_token(self, token: str) -> AgentAuthContext | None:
        if (
            not token
            or len(token) > 512
            or not token.startswith(_AGENT_TOKEN_PREFIX)
        ):
            return None
        now = _utcnow()
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT t.id AS token_id,t.scopes_json,t.repository_ids_json,t.expires_at,
                       a.id AS agent_id,a.name AS agent_name,a.owner_user_id,a.is_active,
                       a.tenant_id,a.workspace_id,w.name AS workspace_name,
                       u.is_active AS owner_active,m.role AS owner_role
                FROM agent_tokens t
                JOIN service_agents a ON a.id=t.agent_id
                JOIN workspaces w ON w.id=a.workspace_id AND w.tenant_id=a.tenant_id
                JOIN users u ON u.id=a.owner_user_id
                JOIN memberships m
                  ON m.user_id=a.owner_user_id AND m.workspace_id=a.workspace_id
                WHERE t.token_hash=? AND t.revoked_at IS NULL
                """,
                (_token_hash(token),),
            ).fetchone()
            if (
                row is None
                or not row["is_active"]
                or not row["owner_active"]
                or _parse_time(str(row["expires_at"])) <= now
            ):
                return None
            try:
                scopes = self._normalize_agent_scopes(json.loads(row["scopes_json"]))
                repository_ids = self._normalize_repository_ids(
                    json.loads(row["repository_ids_json"])
                )
            except (ValueError, TypeError, json.JSONDecodeError):
                return None
            conn.execute(
                "UPDATE agent_tokens SET last_used_at=? WHERE id=?",
                (_iso(now), row["token_id"]),
            )
        return AgentAuthContext(
            agent_id=str(row["agent_id"]),
            token_id=str(row["token_id"]),
            agent_name=str(row["agent_name"]),
            owner_user_id=str(row["owner_user_id"]),
            tenant_id=str(row["tenant_id"]),
            workspace_id=str(row["workspace_id"]),
            workspace_name=str(row["workspace_name"]),
            owner_role=str(row["owner_role"]),
            scopes=frozenset(scopes),
            repository_ids=frozenset(repository_ids),
            expires_at=_parse_time(str(row["expires_at"])),
        )

    def grant_agent_job(
        self, *, context: AgentAuthContext, job_id: str, repository_id: str
    ) -> None:
        if repository_id not in context.repository_ids:
            raise PermissionError("Repository ist für diesen KI-Agenten nicht freigegeben")
        try:
            UUID(job_id)
        except ValueError as exc:
            raise ValueError("Ungültige Job-ID") from exc
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO agent_job_grants(agent_id,job_id,repository_id,created_at) "
                "VALUES(?,?,?,?)",
                (context.agent_id, job_id, repository_id, _iso(_utcnow())),
            )

    def agent_job_ids(self, *, agent_id: str, limit: int = 100) -> list[str]:
        bounded_limit = max(1, min(int(limit), 1_000))
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT job_id FROM agent_job_grants WHERE agent_id=? "
                "ORDER BY created_at DESC,job_id LIMIT ?",
                (agent_id, bounded_limit),
            ).fetchall()
        return [str(row["job_id"]) for row in rows]

    def agent_can_access_job(self, *, agent_id: str, job_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM agent_job_grants WHERE agent_id=? AND job_id=?",
                (agent_id, job_id),
            ).fetchone()
        return row is not None


class ProviderCallRateLimiter:
    """Persistente per-user/provider Sliding-Window-Grenze auf Basis des AuthStore."""

    def __init__(
        self,
        store: AuthStore,
        *,
        limit: int = 40,
        window_seconds: int = 60,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.limit = max(1, min(int(limit), 240))
        self.window_seconds = max(1, min(int(window_seconds), 3600))
        self._clock = clock or _utcnow

    @staticmethod
    def _provider(identity: str) -> str:
        provider = str(identity or "").split(":", 1)[0].strip().lower()
        if not re.fullmatch(r"[a-z0-9_.-]{1,64}", provider):
            raise ValueError("LLM-Identität enthält keinen gültigen Provider")
        return provider

    def consume(self, user_id: str, identity: str) -> None:
        provider = self._provider(identity)
        retry_after = self.store.consume_provider_call_event(
            user_id=user_id,
            provider=provider,
            limit=self.limit,
            window_seconds=self.window_seconds,
            now=self._clock(),
        )
        if retry_after is not None:
            raise LLMRateLimitExceeded(
                provider=provider,
                limit=self.limit,
                window_seconds=self.window_seconds,
                retry_after_seconds=retry_after,
            )
