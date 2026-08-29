from __future__ import annotations

import json
import subprocess
import threading
from http.cookiejar import CookieJar
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener, urlopen

import pytest

from tankai import __version__

from tankai.dev_orchestrator.job_queue import WorkspaceQueuePolicy
from tankai.dev_orchestrator.models import (
    CommandSpec,
    GateJob,
    WorkerIsolationSpec,
    WorkerJob,
    WorkerPipelineJob,
)
from tankai.web.auth import AuthStore, verify_password, hash_password
from tankai.web import server as web_server


class Client:
    def __init__(self, base: str) -> None:
        self.base = base
        self.jar = CookieJar()
        self.opener = build_opener(HTTPCookieProcessor(self.jar))

    def get(self, path: str):
        try:
            with self.opener.open(self.base + path, timeout=30) as response:
                return response.status, dict(response.headers), json.load(response)
        except HTTPError as exc:
            return exc.code, dict(exc.headers), json.load(exc)

    def post(self, path: str, payload: dict, *, csrf: str | None = None):
        headers = {"Content-Type": "application/json"}
        if csrf:
            headers["X-CSRF-Token"] = csrf
        request = Request(
            self.base + path,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers=headers,
        )
        try:
            with self.opener.open(request, timeout=40) as response:
                return response.status, dict(response.headers), json.load(response)
        except HTTPError as exc:
            return exc.code, dict(exc.headers), json.load(exc)


def _configure(monkeypatch, tmp_path) -> AuthStore:
    monkeypatch.setenv("TANKAI_LLM", "mock")
    monkeypatch.delenv("TANKAI_CRITIC_LLM", raising=False)
    monkeypatch.delenv("TANKAI_SEARCH_PROVIDER", raising=False)
    monkeypatch.setenv("TANKAI_AUTH_MODE", "session")
    monkeypatch.setenv("TANKAI_COOKIE_SECURE", "0")
    monkeypatch.setenv("TANKAI_ALLOW_REGISTRATION", "0")
    monkeypatch.setenv("TANKAI_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("TANKAI_AUTH_DB", str(tmp_path / "data" / "auth.db"))
    monkeypatch.setenv("TANKAI_LTM_MEMORY", "0")
    return AuthStore(tmp_path / "data" / "auth.db")


def test_password_hashing_and_session_revocation(tmp_path) -> None:
    store = AuthStore(tmp_path / "auth.db")
    encoded = hash_password("A-very-long-password-123")
    assert verify_password("A-very-long-password-123", encoded)
    assert not verify_password("wrong-password-123", encoded)

    user_id, tenant_id, workspace_id = store.create_user_with_tenant(
        email="User@Example.com",
        password="A-very-long-password-123",
        display_name="User",
        tenant_name="Tenant",
    )
    session = store.authenticate(email="user@example.com", password="A-very-long-password-123")
    assert session is not None
    assert session.context.user_id == user_id
    assert session.context.tenant_id == tenant_id
    assert session.context.workspace_id == workspace_id
    assert store.resolve_session(session.token) is not None
    store.revoke_session(session.context.session_id, user_id=user_id)
    assert store.resolve_session(session.token) is None


def test_health_auth_csrf_and_tenant_isolation(tmp_path, monkeypatch) -> None:
    store = _configure(monkeypatch, tmp_path)
    _, tenant_a, workspace_a = store.create_user_with_tenant(
        email="a@example.com",
        password="User-A-password-123",
        display_name="User A",
        tenant_name="Tenant A",
    )
    _, tenant_b, workspace_b = store.create_user_with_tenant(
        email="b@example.com",
        password="User-B-password-123",
        display_name="User B",
        tenant_name="Tenant B",
    )

    app = web_server.AppContext.from_env("127.0.0.1")
    server = web_server.ThreadedHTTPServer(("127.0.0.1", 0), web_server.Handler, app=app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    user_a = Client(base)
    user_b = Client(base)
    try:
        status, headers, health = user_a.get("/api/health")
        assert status == 200
        assert health["version"] == __version__
        assert health["auth_required"] is True
        assert "llm" not in health
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert "Content-Security-Policy" in headers

        status, _, denied = user_a.get("/api/auth/me")
        assert status == 401
        assert denied["error"] == "Nicht angemeldet"

        status, _, login_a = user_a.post(
            "/api/auth/login",
            {"email": "a@example.com", "password": "User-A-password-123"},
        )
        assert status == 200
        csrf_a = login_a["csrf_token"]
        assert login_a["tenant"]["id"] == tenant_a
        assert login_a["workspace"]["id"] == workspace_a

        status, _, csrf_denied = user_a.post(
            "/api/run",
            {"goal": "Tenant A secret", "definition_of_done": "done", "parallel": False},
        )
        assert status == 403
        assert "CSRF" in csrf_denied["error"]

        status, _, result_a = user_a.post(
            "/api/run",
            {"goal": "<img src=x onerror=alert(1)> Tenant A secret", "definition_of_done": "<script>x</script>", "parallel": False},
            csrf=csrf_a,
        )
        assert status == 200
        assert result_a["workspace_id"] == workspace_a
        assert result_a["status"] == "simulated"

        status, _, history_a = user_a.get("/api/history")
        assert status == 200
        assert len(history_a) == 1
        assert history_a[0]["workspace_id"] == workspace_a
        assert "Tenant A secret" in history_a[0]["goal"]

        status, _, login_b = user_b.post(
            "/api/auth/login",
            {"email": "b@example.com", "password": "User-B-password-123"},
        )
        assert status == 200
        csrf_b = login_b["csrf_token"]
        assert login_b["tenant"]["id"] == tenant_b
        assert login_b["workspace"]["id"] == workspace_b

        status, _, history_b = user_b.get("/api/history")
        assert status == 200
        assert history_b == []

        status, _, forbidden = user_b.post(
            "/api/workspaces/select",
            {"workspace_id": workspace_a},
            csrf=csrf_b,
        )
        assert status == 403
        assert "Kein Zugriff" in forbidden["error"]

        status, _, created = user_a.post(
            "/api/workspaces",
            {"name": "Projekt Zwei"},
            csrf=csrf_a,
        )
        assert status == 201
        second_workspace = created["workspace"]["id"]

        status, _, switched = user_a.post(
            "/api/workspaces/select",
            {"workspace_id": second_workspace},
            csrf=csrf_a,
        )
        assert status == 200
        assert switched["workspace"]["id"] == second_workspace
        # CSRF bleibt an die Session gebunden und ändert sich nicht beim Workspace-Wechsel.
        assert switched["csrf_token"] == csrf_a
        status, _, second_history = user_a.get("/api/history")
        assert status == 200
        assert second_history == []

        data_root = tmp_path / "data" / "tenants"
        assert (data_root / tenant_a / "workspaces" / workspace_a / "ltm.db").exists()
        assert (data_root / tenant_b / "workspaces" / workspace_b / "ltm.db").exists()
        assert (data_root / tenant_a / "workspaces" / workspace_a).resolve() != (
            data_root / tenant_b / "workspaces" / workspace_b
        ).resolve()

        status, _, logged_out = user_a.post("/api/auth/logout", {}, csrf=csrf_a)
        assert status == 200 and logged_out["ok"] is True
        status, _, _ = user_a.get("/api/auth/me")
        assert status == 401
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_registration_disabled_and_public_auth_disable_blocked(tmp_path, monkeypatch) -> None:
    _configure(monkeypatch, tmp_path)
    app = web_server.AppContext.from_env("127.0.0.1")
    server = web_server.ThreadedHTTPServer(("127.0.0.1", 0), web_server.Handler, app=app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = Client(f"http://127.0.0.1:{server.server_address[1]}")
    try:
        status, _, payload = client.post(
            "/api/auth/register",
            {
                "email": "new@example.com",
                "password": "Registration-password-123",
                "display_name": "New",
                "tenant_name": "New Tenant",
            },
        )
        assert status == 403
        assert payload["error"] == "Registrierung ist deaktiviert"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    monkeypatch.setenv("TANKAI_AUTH_MODE", "disabled")
    with pytest.raises(RuntimeError, match="nur auf Loopback"):
        web_server.AppContext.from_env("0.0.0.0")


def test_html_uses_safe_dom_rendering() -> None:
    assert "innerHTML" not in web_server.HTML
    assert "catch{}" not in web_server.HTML
    assert "textContent" in web_server.HTML
    assert 'href="/favicon.ico"' in web_server.HTML
    assert 'src="/favicon.png"' in web_server.HTML
    assert "HttpOnly" in web_server.Handler._session_cookie.__code__.co_consts


def test_brand_assets_are_served(tmp_path, monkeypatch) -> None:
    _configure(monkeypatch, tmp_path)
    app = web_server.AppContext.from_env("127.0.0.1")
    server = web_server.ThreadedHTTPServer(("127.0.0.1", 0), web_server.Handler, app=app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        for path, content_type, signature in (
            ("/favicon.ico", "image/x-icon", b"\x00\x00\x01\x00"),
            ("/favicon.png", "image/png", b"\x89PNG\r\n\x1a\n"),
            ("/apple-touch-icon.png", "image/png", b"\x89PNG\r\n\x1a\n"),
        ):
            with urlopen(base + path, timeout=5) as response:
                assert response.status == 200
                assert response.headers.get_content_type() == content_type
                assert response.headers["Cache-Control"] == "public, max-age=86400"
                assert response.read().startswith(signature)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _web_queue_pipeline(image: str) -> dict:
    command = CommandSpec(argv=["python", "-c", "pass"], timeout_seconds=10)
    return WorkerPipelineJob(
        worker=WorkerJob(
            agent_id="AGENT_BACKEND_01",
            implementation_summary="Web queue task",
            commit_message="Implement web queue task",
            implementation_commands=[command],
            test_commands=[command],
        ),
        gates=GateJob(
            reviewer_agent_id="AGENT_REVIEWER_01",
            review_commands=[command],
            qa_agent_id="AGENT_QA_01",
            qa_commands=[command],
        ),
        isolation=WorkerIsolationSpec(
            image=image,
            memory_mb=256,
            cpus=1,
            pids_limit=64,
            user="1000:1000",
        ),
    ).model_dump(mode="json")


def _init_web_queue_repo(path) -> None:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, stdout=subprocess.PIPE)
    subprocess.run(["git", "config", "user.name", "Web Queue Test"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "web-queue@example.invalid"], cwd=path, check=True)
    (path / "README.md").write_text("web queue\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "baseline"], cwd=path, check=True, stdout=subprocess.PIPE)


def test_authenticated_development_queue_api_is_workspace_scoped(tmp_path, monkeypatch) -> None:
    store = _configure(monkeypatch, tmp_path)
    owner, tenant, workspace = store.create_user_with_tenant(
        email="queue-owner@example.com",
        password="Queue-owner-password-123",
        display_name="Queue Owner",
        tenant_name="Queue Tenant",
    )
    _, foreign_tenant, foreign_workspace = store.create_user_with_tenant(
        email="queue-foreign@example.com",
        password="Queue-foreign-password-123",
        display_name="Queue Foreign",
        tenant_name="Foreign Queue Tenant",
    )
    repo_base = tmp_path / "repositories"
    work_base = tmp_path / "worktrees"
    state_base = tmp_path / "states"
    repo = repo_base / "main"
    _init_web_queue_repo(repo)
    monkeypatch.setenv("TANKAI_DEV_QUEUE_ENABLED", "1")
    monkeypatch.setenv("TANKAI_REPOSITORY_BASE", str(repo_base))
    monkeypatch.setenv("TANKAI_WORKTREE_BASE", str(work_base))
    monkeypatch.setenv("TANKAI_STATE_BASE", str(state_base))

    image = "tankai-worker@sha256:" + "c" * 64
    app = web_server.AppContext.from_env("127.0.0.1")
    assert app.job_queue is not None
    app.job_queue.set_policy(
        actor_user_id=owner,
        workspace_id=workspace,
        policy=WorkspaceQueuePolicy(
            tenant_id=tenant,
            workspace_id=workspace,
            max_queued=5,
            max_running=1,
            max_memory_mb=512,
            max_cpus=2,
            max_pids=128,
            max_runtime_seconds=120,
            allowed_images=[image],
        ),
    )
    binding = app.job_queue.register_repository(
        actor_user_id=owner,
        workspace_id=workspace,
        name="Main",
        repository_path=repo,
        workspace_root=work_base / "main",
        state_path=state_base / "main.json",
    )

    server = web_server.ThreadedHTTPServer(("127.0.0.1", 0), web_server.Handler, app=app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    owner_client = Client(base)
    foreign_client = Client(base)
    try:
        status, _, login = owner_client.post(
            "/api/auth/login",
            {"email": "queue-owner@example.com", "password": "Queue-owner-password-123"},
        )
        assert status == 200
        csrf = login["csrf_token"]

        status, _, health = owner_client.get("/api/health")
        assert status == 200
        assert health["development_queue_enabled"] is True

        status, _, repositories = owner_client.get("/api/dev/repositories")
        assert status == 200
        assert repositories == {"repositories": [{
            "repository_id": binding.repository_id,
            "name": "Main",
            "enabled": True,
        }]}

        status, _, created = owner_client.post(
            "/api/dev/jobs",
            {
                "repository_id": binding.repository_id,
                "idempotency_key": "web-job-1",
                "priority": 3,
                "pipeline": _web_queue_pipeline(image),
            },
            csrf=csrf,
        )
        assert status == 202
        assert created["job"]["state"] == "queued"
        job_id = created["job"]["job_id"]

        status, _, jobs = owner_client.get("/api/dev/jobs")
        assert status == 200
        assert [item["job_id"] for item in jobs["jobs"]] == [job_id]
        assert "pipeline" not in jobs["jobs"][0]

        status, _, cancelled = owner_client.post(
            f"/api/dev/jobs/{job_id}/cancel", {}, csrf=csrf
        )
        assert status == 200
        assert cancelled["job"]["state"] == "cancelled"

        status, _, foreign_login = foreign_client.post(
            "/api/auth/login",
            {"email": "queue-foreign@example.com", "password": "Queue-foreign-password-123"},
        )
        assert status == 200
        assert foreign_login["tenant"]["id"] == foreign_tenant
        assert foreign_login["workspace"]["id"] == foreign_workspace
        status, _, foreign_repos = foreign_client.get("/api/dev/repositories")
        assert status == 200 and foreign_repos == {"repositories": []}
        status, _, foreign_jobs = foreign_client.get("/api/dev/jobs")
        assert status == 200 and foreign_jobs == {"jobs": []}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_development_queue_fails_closed_without_operator_bases(tmp_path, monkeypatch) -> None:
    _configure(monkeypatch, tmp_path)
    monkeypatch.setenv("TANKAI_DEV_QUEUE_ENABLED", "1")
    monkeypatch.delenv("TANKAI_REPOSITORY_BASE", raising=False)
    monkeypatch.delenv("TANKAI_WORKTREE_BASE", raising=False)
    monkeypatch.delenv("TANKAI_STATE_BASE", raising=False)
    with pytest.raises(RuntimeError, match="TANKAI_REPOSITORY_BASE"):
        web_server.AppContext.from_env("127.0.0.1")
