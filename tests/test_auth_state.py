from __future__ import annotations

import json
import secrets
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from tankai.web.auth import AuthStore, hash_password
from tankai.web.auth_state import AuthState, bootstrap_owner


@pytest.fixture
def remote():
    state = {"data": None, "revision": 0, "fail": False}
    token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.headers.get("Authorization") != "Bearer " + token:
                self.send_error(403)
            elif state["data"] is None:
                self.send_error(404)
            else:
                self.send_response(200)
                self.send_header("X-Auth-Revision", str(state["revision"]))
                self.end_headers()
                self.wfile.write(state["data"])

        def do_PUT(self):
            data = self.rfile.read(int(self.headers["Content-Length"]))
            if state["fail"] or self.headers.get("Authorization") != "Bearer " + token:
                self.send_error(503)
            elif self.headers.get("If-Match") != str(state["revision"]):
                self.send_error(409)
            else:
                state.update(data=data, revision=state["revision"] + 1)
                self.send_response(204)
                self.send_header("X-Auth-Revision", str(state["revision"]))
                self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def client():
        result = AuthState("https://state.example.test/auth", token)
        result.url = f"http://127.0.0.1:{server.server_port}/auth"
        return result

    yield state, client
    server.shutdown()
    server.server_close()
    thread.join()


def owner_config(password="test-owner-password-123"):
    return json.dumps({"email": "owner@example.test", "password_hash": hash_password(password),
                       "user_id": str(uuid4()), "tenant_id": str(uuid4()), "workspace_id": str(uuid4())})


def test_owner_and_revoked_session_survive_fresh_container(tmp_path, remote):
    _, client = remote
    first = client()
    path = tmp_path / "first" / "auth.db"
    first.restore(path)
    store = AuthStore(path)
    config = owner_config()
    bootstrap_owner(store, config)
    ids = store.list_users()
    session = store.authenticate(email="owner@example.test", password="test-owner-password-123")
    assert session is not None
    first.save(store)
    second = client()
    path2 = tmp_path / "second" / "auth.db"
    second.restore(path2)
    restored = AuthStore(path2)
    assert restored.list_users() == ids
    assert restored.resolve_session(session.token) is not None
    restored.revoke_session(session.context.session_id)
    second.save(restored)
    third = client()
    path3 = tmp_path / "third" / "auth.db"
    third.restore(path3)
    assert AuthStore(path3).resolve_session(session.token) is None


def test_bootstrap_never_overwrites_an_existing_password(tmp_path):
    store = AuthStore(tmp_path / "auth.db")
    config = owner_config()
    bootstrap_owner(store, config)
    user = store.get_user_id_by_email("owner@example.test")
    store.set_password(user, "changed-owner-password-123")
    bootstrap_owner(store, config)
    assert store.user_count() == 1
    assert store.authenticate(email="owner@example.test", password="changed-owner-password-123") is not None
    assert store.authenticate(email="owner@example.test", password="test-owner-password-123") is None


def test_remote_failure_and_stale_writer_cannot_acknowledge_save(tmp_path, remote):
    state, client = remote
    first, stale = client(), client()
    store = AuthStore(tmp_path / "auth.db")
    bootstrap_owner(store, owner_config())
    first.save(store)
    original = state["data"]
    with pytest.raises(RuntimeError, match="storage is unavailable"):
        stale.save(store)
    assert state["data"] == original
    state["fail"] = True
    with pytest.raises(RuntimeError, match="storage is unavailable"):
        first.save(store)


def test_corrupt_backup_fails_closed_without_replacing_existing_db(tmp_path, remote):
    state, client = remote
    path = tmp_path / "auth.db"
    store = AuthStore(path)
    bootstrap_owner(store, owner_config())
    original = store.list_users()
    state.update(data=b"not a sqlite database", revision=1)
    with pytest.raises(RuntimeError, match="Invalid auth state"):
        client().restore(path)
    assert AuthStore(path).list_users() == original


@pytest.mark.parametrize("kind", ["foreign", "wrong_version"])
def test_healthy_but_incompatible_sqlite_backup_cannot_replace_auth_db(
    tmp_path, remote, kind
):
    state, client = remote
    path = tmp_path / "auth.db"
    store = AuthStore(path)
    bootstrap_owner(store, owner_config())
    original = store.list_users()

    incompatible = tmp_path / f"{kind}.db"
    with sqlite3.connect(incompatible) as connection:
        if kind == "foreign":
            connection.execute("CREATE TABLE unrelated(value TEXT)")
        else:
            connection.execute("CREATE TABLE auth_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            connection.execute(
                "INSERT INTO auth_meta(key,value) VALUES('schema_version','999')"
            )
    state.update(data=incompatible.read_bytes(), revision=1)

    with pytest.raises(RuntimeError, match="Invalid auth state database"):
        client().restore(path)
    assert AuthStore(path).list_users() == original


@pytest.mark.parametrize("kind", ["missing_columns", "broken_relations"])
def test_snapshot_schema_and_foreign_keys_are_validated(tmp_path, remote, kind):
    state, client = remote
    path = tmp_path / "auth.db"
    store = AuthStore(path)
    bootstrap_owner(store, owner_config())
    original = store.list_users()

    incompatible = tmp_path / f"{kind}.db"
    if kind == "missing_columns":
        tables = (
            "users", "tenants", "workspaces", "memberships", "sessions",
            "audit_events", "provider_call_events", "service_agents",
            "agent_tokens", "agent_job_grants",
        )
        with sqlite3.connect(incompatible) as connection:
            connection.execute(
                "CREATE TABLE auth_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT INTO auth_meta(key,value) VALUES('schema_version','3')"
            )
            for table in tables:
                connection.execute(f'CREATE TABLE "{table}"(placeholder TEXT)')
    else:
        broken = AuthStore(incompatible)
        with broken._connect() as connection:
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute(
                "INSERT INTO memberships(user_id,workspace_id,role,created_at) "
                "VALUES('missing-user','missing-workspace','member','2026-10-22T00:00:00+00:00')"
            )
    state.update(data=incompatible.read_bytes(), revision=1)

    with pytest.raises(RuntimeError, match="Invalid auth state database"):
        client().restore(path)
    assert AuthStore(path).list_users() == original


@pytest.mark.parametrize("kind", ["view", "trigger"])
def test_snapshot_auth_objects_must_be_passive_tables(tmp_path, remote, kind):
    state, client = remote
    path = tmp_path / "auth.db"
    store = AuthStore(path)
    bootstrap_owner(store, owner_config())
    original = store.list_users()

    incompatible = tmp_path / f"{kind}.db"
    AuthStore(incompatible)
    with sqlite3.connect(incompatible) as connection:
        if kind == "view":
            connection.execute("DROP TABLE sessions")
            connection.execute(
                "CREATE VIEW sessions AS SELECT "
                "NULL AS id,NULL AS token_hash,NULL AS csrf_hash,NULL AS csrf_token,"
                "NULL AS user_id,NULL AS active_workspace_id,NULL AS created_at,"
                "NULL AS expires_at,NULL AS last_seen_at,NULL AS revoked_at,"
                "NULL AS user_agent_hash,NULL AS ip_hash WHERE 0"
            )
        else:
            connection.execute(
                "CREATE TRIGGER mutate_users AFTER INSERT ON audit_events "
                "BEGIN DELETE FROM users; END"
            )
    state.update(data=incompatible.read_bytes(), revision=1)

    with pytest.raises(RuntimeError, match="Invalid auth state database"):
        client().restore(path)
    assert AuthStore(path).list_users() == original


def test_login_does_not_issue_a_cookie_until_session_is_durable(tmp_path, remote, monkeypatch):
    from tankai.web import server as web
    state, client = remote
    backup = client()
    monkeypatch.setattr(AuthState, "from_env", classmethod(lambda cls: backup))
    monkeypatch.setenv("TANKAI_AUTH_MODE", "session")
    monkeypatch.setenv("TANKAI_DATA_ROOT", str(tmp_path / "web"))
    monkeypatch.setenv("TANKAI_AUTH_DB", str(tmp_path / "web/auth.db"))
    monkeypatch.setenv("TANKAI_BOOTSTRAP_OWNER", owner_config())
    app = web.AppContext.from_env("127.0.0.1")
    http = web.ThreadedHTTPServer(("127.0.0.1", 0), web.Handler, app=app)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    body = json.dumps({"email": "owner@example.test", "password": "test-owner-password-123"}).encode()
    try:
        state["fail"] = True
        request = Request(f"http://127.0.0.1:{http.server_port}/api/auth/login", data=body, headers={"Content-Type": "application/json"})
        with pytest.raises(HTTPError) as failed:
            urlopen(request)
        assert failed.value.code == 503
        assert failed.value.headers.get("Set-Cookie") is None
        state["fail"] = False
        with urlopen(request) as response:
            assert response.status == 200
            assert response.headers.get("Set-Cookie", "").startswith("tankai_session=")
    finally:
        http.shutdown()
        http.server_close()
        thread.join()


@pytest.mark.parametrize("url,token", [("http://example.test/auth", "a" * 43), ("https://example.test/auth", "short"), ("https://user:password@example.test/auth", "a" * 43)])
def test_transport_rejects_insecure_configuration(url, token):
    with pytest.raises(ValueError):
        AuthState(url, token)


def test_failed_startup_reports_only_a_safe_stage_and_keeps_login_disabled():
    from tankai.web.server import _startup_failure_handler
    from tankai.web.auth_state import AuthStateUnavailable
    server = ThreadingHTTPServer(("127.0.0.1", 0), _startup_failure_handler(AuthStateUnavailable("restore", 403)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises(HTTPError) as failure:
            urlopen(f"http://127.0.0.1:{server.server_port}/api/health")
        assert failure.value.code == 503
        assert json.load(failure.value) == {"ok": False, "error": "Kontospeicher konnte nicht gestartet werden", "startup_stage": "restore", "upstream_status": 403, "error_type": "AuthStateUnavailable"}
        with pytest.raises(HTTPError) as login:
            urlopen(Request(f"http://127.0.0.1:{server.server_port}/api/auth/login", data=b"{}"))
        assert login.value.code == 503
        assert "startup_stage" not in json.load(login.value)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
