"""Persistente Benutzer-, Sitzungs- und Workspace-Authentifizierung für TankAI.

Die Datenbank ist die verbindliche Quelle. Clientseitige Nutzer- oder Workspace-IDs
werden niemals ungeprüft für Datenzugriffe verwendet.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable
from uuid import uuid4

_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_NAME_RE = re.compile(r"[^a-z0-9_-]+")
_ALLOWED_ROLES = {"owner", "admin", "member"}


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


@dataclass(frozen=True)
class SessionCreated:
    token: str
    context: AuthContext


class AuthStore:
    """SQLite-basierter Auth-Store mit widerrufbaren, opaken Sessions."""

    SCHEMA_VERSION = 1

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
                INSERT OR REPLACE INTO auth_meta(key,value) VALUES('schema_version','1');
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
