from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from uuid import uuid4

import pytest

from tankai.web.auth import AuthStore, hash_password
from tankai.web.auth_state import bootstrap_owner
from tankai.web.owner_recovery import apply_owner_recovery


EMAIL = "owner@synthetic.example"
OLD_PASSWORD = "Initial-synthetic-owner-password-123"
NEW_PASSWORD = "Fresh-synthetic-owner-password-456"


def setup_owner(tmp_path):
    store = AuthStore(tmp_path / "auth.db")
    owner = {
        "email": EMAIL,
        "password_hash": hash_password(OLD_PASSWORD),
        "user_id": str(uuid4()),
        "tenant_id": str(uuid4()),
        "workspace_id": str(uuid4()),
    }
    bootstrap_owner(store, json.dumps(owner))
    return store, owner


def recovery(owner, *, expires_at=None, **changes):
    now = datetime.now(timezone.utc)
    expiry = expires_at if expires_at is not None else now + timedelta(minutes=15)
    policy = {
        "recovery_id": str(uuid4()),
        "email": owner["email"],
        "expires_at": int(expiry.timestamp()) if isinstance(expiry, datetime) else expiry,
        "password_hash": hash_password(NEW_PASSWORD),
    }
    policy.update(changes)
    return json.dumps(policy)


def test_recovery_preserves_identity_revokes_sessions_and_survives_restart(tmp_path):
    store, owner = setup_owner(tmp_path)
    previous = store.authenticate(email=EMAIL, password=OLD_PASSWORD)
    assert previous is not None
    policy = recovery(owner)

    assert apply_owner_recovery(store, bootstrap_config=json.dumps(owner), recovery_config=policy)
    assert store.resolve_session(previous.token) is None
    assert store.authenticate(email=EMAIL, password=OLD_PASSWORD) is None
    assert store.get_user_id_by_email(EMAIL) == owner["user_id"]
    assert len(store.list_users()) == 1
    workspaces = store.list_workspaces(owner["user_id"])
    assert len(workspaces) == 1
    assert workspaces[0].id == owner["workspace_id"]
    assert workspaces[0].tenant_id == owner["tenant_id"]
    assert workspaces[0].role == "owner"

    reopened = AuthStore(store.path)
    assert reopened.authenticate(email=EMAIL, password=NEW_PASSWORD) is not None


def test_recovery_replay_is_idempotent_including_after_expiry(tmp_path):
    store, owner = setup_owner(tmp_path)
    start = datetime.now(timezone.utc)
    policy = recovery(owner, expires_at=start + timedelta(minutes=1))
    assert apply_owner_recovery(store, bootstrap_config=json.dumps(owner), recovery_config=policy, now=start)
    new_session = store.authenticate(email=EMAIL, password=NEW_PASSWORD)
    assert new_session is not None

    restarted = AuthStore(store.path)
    assert not apply_owner_recovery(
        restarted, bootstrap_config=json.dumps(owner), recovery_config=policy,
        now=start + timedelta(days=2),
    )
    assert restarted.resolve_session(new_session.token) is not None
    with restarted._connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM audit_events WHERE event_type='owner_password_recovered'"
        ).fetchone()[0] == 1


@pytest.mark.parametrize("override", [
    {"email": "attacker@synthetic.example"},
    {"password_hash": "not-a-scrypt-hash"},
    {"expires_at": "tomorrow"},
    {"expires_at": datetime.now(timezone.utc) + timedelta(days=1)},
    {"recovery_id": "not-a-uuid"},
    {"unexpected": "field"},
])
def test_invalid_policy_never_changes_credentials(tmp_path, override):
    store, owner = setup_owner(tmp_path)
    policy = recovery(owner, **override)
    with pytest.raises(ValueError):
        apply_owner_recovery(
            store, bootstrap_config=json.dumps(owner), recovery_config=policy
        )
    assert store.authenticate(email=EMAIL, password=OLD_PASSWORD) is not None
    assert len(store.list_users()) == 1


def test_expired_policy_fails_closed(tmp_path):
    store, owner = setup_owner(tmp_path)
    policy = recovery(owner, expires_at=datetime.now(timezone.utc) - timedelta(minutes=1))
    with pytest.raises(ValueError, match="expired"):
        apply_owner_recovery(store, bootstrap_config=json.dumps(owner), recovery_config=policy)
    assert store.authenticate(email=EMAIL, password=OLD_PASSWORD) is not None


def test_mismatched_owner_id_never_overwrites_account(tmp_path):
    store, owner = setup_owner(tmp_path)
    bad_bootstrap = {**owner, "user_id": str(uuid4())}
    with pytest.raises(ValueError, match="does not match"):
        apply_owner_recovery(
            store, bootstrap_config=json.dumps(bad_bootstrap),
            recovery_config=recovery(owner),
        )
    assert store.authenticate(email=EMAIL, password=OLD_PASSWORD) is not None


def test_no_recovery_secret_is_a_noop(tmp_path):
    store, owner = setup_owner(tmp_path)
    assert not apply_owner_recovery(
        store, bootstrap_config=json.dumps(owner), recovery_config=""
    )
    assert store.authenticate(email=EMAIL, password=OLD_PASSWORD) is not None


def test_recovery_requires_bootstrap(tmp_path):
    store, owner = setup_owner(tmp_path)
    with pytest.raises(ValueError, match="bootstrap"):
        apply_owner_recovery(store, bootstrap_config="", recovery_config=recovery(owner))


def test_recovery_cannot_create_owner_when_durable_snapshot_is_missing(
    tmp_path, monkeypatch
):
    """A recovery secret must not turn a missing backup into a fresh account."""
    from tankai.web import server as web

    class MissingSnapshotState:
        def restore(self, path):
            assert not path.exists()

        def save(self, store):
            pass

    owner = {
        "email": EMAIL,
        "password_hash": hash_password(OLD_PASSWORD),
        "user_id": str(uuid4()),
        "tenant_id": str(uuid4()),
        "workspace_id": str(uuid4()),
    }
    root = tmp_path / "web"
    database = root / "auth.db"
    monkeypatch.setattr(
        web.AuthState,
        "from_env",
        classmethod(lambda cls: MissingSnapshotState()),
    )
    monkeypatch.setenv("TANKAI_AUTH_MODE", "session")
    monkeypatch.setenv("TANKAI_DATA_ROOT", str(root))
    monkeypatch.setenv("TANKAI_AUTH_DB", str(database))
    monkeypatch.setenv("TANKAI_BOOTSTRAP_OWNER", json.dumps(owner))
    monkeypatch.setenv("TANKAI_OWNER_RECOVERY", recovery(owner))
    monkeypatch.setenv("TANKAI_LLM", "mock")
    monkeypatch.setenv("TANKAI_EMBEDDER", "hashing")
    monkeypatch.setenv("TANKAI_DEV_QUEUE_ENABLED", "0")

    with pytest.raises(ValueError, match="does not match recovery policy"):
        web.AppContext._from_env_unlocked("127.0.0.1")

    assert AuthStore(database).user_count() == 0
