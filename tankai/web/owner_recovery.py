"""One-shot operator-approved owner password recovery.

Never exposed over HTTP. The protected Cloudflare binding supplies only a scrypt
hash, short expiry, and nonce; the existing bootstrap identity is authoritative.
The successful reset is committed to the same durable auth snapshot as sessions.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import re
from uuid import UUID, uuid4

from .auth import AuthStore, _iso, _utcnow, normalize_email

_HASH = re.compile(r"scrypt\\$16384\\$8\\$1\\$[0-9a-f]{32}\\$[0-9a-f]{64}")
_REQUIRED = frozenset({"recovery_id", "email", "expires_at", "password_hash"})


def apply_owner_recovery(
    store: AuthStore, *,
    bootstrap_config: str,
    recovery_config: str,
    now: datetime | None = None,
) -> bool:
    """Reset exactly the provisioned owner, at most once per persistent nonce.

    Returns True only when the password was updated. A replay returns False.
    On any invalid or mismatched request, fail closed without altering accounts.
    """
    if not recovery_config:
        return False
    if not bootstrap_config:
        raise ValueError("Owner recovery requires existing bootstrap identity")
    try:
        bootstrap = json.loads(bootstrap_config)
        policy = json.loads(recovery_config)
        if not isinstance(bootstrap, dict) or not isinstance(policy, dict):
            raise ValueError("Invalid owner recovery policy")
        if set(policy) != _REQUIRED:
            raise ValueError("Invalid owner recovery fields")
        owner_id = str(UUID(bootstrap["user_id"]))
        workspace_id = str(UUID(bootstrap["workspace_id"]))
        tenant_id = str(UUID(bootstrap["tenant_id"]))
        email = normalize_email(bootstrap["email"])
        if normalize_email(policy["email"]) != email:
            raise ValueError("Owner recovery email does not match bootstrap")
        nonce = str(UUID(policy["recovery_id"]))
        if nonce != policy["recovery_id"]:
            raise ValueError("Noncanonical owner recovery identifier")
        expiry = policy["expires_at"]
        new_hash = policy["password_hash"]
        if type(expiry) is not int or expiry < 0 or expiry > 4_102_444_800:
            raise ValueError("Invalid owner recovery expiry")
        if not isinstance(new_hash, str) or _HASH.fullmatch(new_hash) is None:
            raise ValueError("Invalid owner recovery password hash")
    except (KeyError, TypeError, json.JSONDecodeError, AttributeError) as exc:
        raise ValueError("Invalid owner recovery configuration") from exc

    marker = "owner_password_recovery:" + nonce
    timestamp = _iso((now or _utcnow()).astimezone(timezone.utc))
    now_epoch = int((now or _utcnow()).timestamp())

    with store._lock, store._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """SELECT u.id FROM users u
            JOIN memberships m ON m.user_id=u.id
            JOIN workspaces w ON w.id=m.workspace_id
            WHERE u.id=? AND u.email=? AND u.is_active=1 AND m.role='owner'
              AND w.id=? AND w.tenant_id=?""",
            (owner_id, email, workspace_id, tenant_id),
        ).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            raise ValueError("Existing owner identity does not match recovery policy")
        if conn.execute("SELECT 1 FROM auth_meta WHERE key=?", (marker,)).fetchone():
            conn.execute("COMMIT")
            return False
        if now_epoch > expiry:
            conn.execute("ROLLBACK")
            raise ValueError("Owner recovery policy expired")
        conn.execute(
            "UPDATE users SET password_hash=?, password_changed_at=? WHERE id=?",
            (new_hash, timestamp, owner_id),
        )
        conn.execute(
            "UPDATE sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
            (timestamp, owner_id),
        )
        conn.execute(
            "INSERT INTO auth_meta(key,value) VALUES(?,?)",
            (marker, timestamp),
        )
        conn.execute(
            """INSERT INTO audit_events
            (id,event_type,user_id,workspace_id,success,details,created_at)
            VALUES(?,?,?,?,?,?,?)""",
            (str(uuid4()), "owner_password_recovered", owner_id, workspace_id, 1, "{}", timestamp),
        )
        conn.execute("COMMIT")
        return True
