"""Authenticated SQLite snapshots for the single Cloudflare Core container.

Only account/session data is backed up here; workspace files and run history
still require their own persistent storage.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import tempfile
import threading
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import UUID

MAX_SNAPSHOT = 8 * 1024 * 1024


class AuthStateUnavailable(RuntimeError):
    def __init__(self, stage, status=None):
        super().__init__("Account storage is unavailable")
        self.stage, self.status = stage, status


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


class AuthState:
    def __init__(self, url: str, token: str):
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Auth state requires an HTTPS service URL")
        if not re.fullmatch(r"[A-Za-z0-9_-]{43,128}", token):
            raise ValueError("Auth state token is missing or invalid")
        self.url, self.token, self.revision = url, token, "0"
        self.lock = threading.RLock()
        self.opener = build_opener(_NoRedirect())

    @classmethod
    def from_env(cls):
        url = os.environ.get("TANKAI_AUTH_STATE_URL", "").strip()
        token = os.environ.get("TANKAI_AUTH_STATE_TOKEN", "").strip()
        if not url and not token:
            return None
        return cls(url, token)

    def restore(self, path: Path) -> None:
        request = Request(self.url, headers={"Authorization": "Bearer " + self.token, "User-Agent": "TankAI-AuthState/1"})
        try:
            with self.opener.open(request, timeout=20) as response:
                data = response.read(MAX_SNAPSHOT + 1)
                revision = response.headers.get("X-Auth-Revision", "")
        except HTTPError as exc:
            if exc.code == 404:
                return
            raise AuthStateUnavailable("restore", exc.code) from None
        if not revision.isdecimal() or not data.startswith(b"SQLite format 3\x00") or len(data) > MAX_SNAPSHOT:
            raise RuntimeError("Invalid auth state snapshot")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".auth-restore-")
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(data)
            connection = sqlite3.connect(temporary)
            try:
                if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
                    raise RuntimeError("Invalid auth state database")
            finally:
                connection.close()
            # Called before any connection to the destination is opened.
            for suffix in ("-wal", "-shm"):
                Path(str(path) + suffix).unlink(missing_ok=True)
            os.replace(temporary, path)
            self.revision = revision
        finally:
            Path(temporary).unlink(missing_ok=True)

    def save(self, store) -> None:
        # SQLite backup includes committed WAL pages and gives a consistent image.
        # Serializing PUTs also keeps the optimistic revision correct across threads.
        with self.lock, store._lock:
            source = sqlite3.connect(store.path)
            snapshot = sqlite3.connect(":memory:")
            try:
                source.backup(snapshot)
                data = snapshot.serialize()
            finally:
                source.close()
                snapshot.close()
            if len(data) > MAX_SNAPSHOT:
                raise RuntimeError("Auth state exceeds snapshot capacity")
            request = Request(self.url, method="PUT", data=data, headers={
                "Authorization": "Bearer " + self.token,
                "User-Agent": "TankAI-AuthState/1",
                "Content-Type": "application/octet-stream",
                "If-Match": self.revision,
            })
            try:
                with self.opener.open(request, timeout=20) as response:
                    revision = response.headers.get("X-Auth-Revision", "")
            except HTTPError as exc:
                raise AuthStateUnavailable("persistence", exc.code) from None
            if not revision.isdecimal():
                raise RuntimeError("Invalid auth state revision")
            self.revision = revision


def bootstrap_owner(store, raw: str) -> None:
    """Create a secret-configured owner only in a completely empty auth database.

    Existing passwords and memberships are never reset by a deploy or restart.
    Stable UUIDs in the protected config survive loss of the container disk.
    """
    if not raw:
        return
    from .auth import normalize_email, _iso, _utcnow, _safe_slug
    config = json.loads(raw)
    if not isinstance(config, dict):
        raise ValueError("Invalid owner configuration")
    email = normalize_email(config["email"])
    ids = [str(UUID(config[key])) for key in ("user_id", "tenant_id", "workspace_id")]
    encoded = config["password_hash"]
    if not isinstance(encoded, str) or not re.fullmatch(r"scrypt\$16384\$8\$1\$[0-9a-f]{32}\$[0-9a-f]{64}", encoded):
        raise ValueError("Invalid owner password hash")
    now = _iso(_utcnow())
    with store._lock, store._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        if connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
            connection.execute("COMMIT")
            return
        user, tenant, workspace = ids
        connection.execute("INSERT INTO users(id,email,display_name,password_hash,created_at,password_changed_at) VALUES(?,?,?,?,?,?)", (user, email, "Tank", encoded, now, now))
        connection.execute("INSERT INTO tenants(id,name,created_at,created_by) VALUES(?,?,?,?)", (tenant, "TankAI", now, user))
        connection.execute("INSERT INTO workspaces(id,tenant_id,name,slug,created_at,created_by) VALUES(?,?,?,?,?,?)", (workspace, tenant, "Standard", _safe_slug("Standard"), now, user))
        connection.execute("INSERT INTO memberships(user_id,workspace_id,role,created_at) VALUES(?,?,?,?)", (user, workspace, "owner", now))
        connection.execute("COMMIT")
    store.audit("owner_bootstrapped", user_id=user, workspace_id=workspace, success=True)
