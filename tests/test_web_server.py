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

    def get(self, path: str, *, bearer: str | None = None):
        request = Request(self.base + path)
        if bearer:
            request.add_header("Authorization", f"Bearer {bearer}")
        try:
            with self.opener.open(request, timeout=30) as response:
                return response.status, dict(response.headers), json.load(response)
        except HTTPError as exc:
            return exc.code, dict(exc.headers), json.load(exc)

    def post(
        self,
        path: str,
        payload: dict,
        *,
        csrf: str | None = None,
        bearer: str | None = None,
    ):
        headers = {"Content-Type": "application/json"}
        if csrf:
            headers["X-CSRF-Token"] = csrf
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
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


def test_external_agent_gateway_is_scoped_revocable_and_job_isolated(
    tmp_path, monkeypatch
) -> None:
    store = _configure(monkeypatch, tmp_path)
    owner, tenant, workspace = store.create_user_with_tenant(
        email="agent-owner@example.com",
        password="Agent-owner-password-123",
        display_name="Agent Owner",
        tenant_name="Agent Tenant",
    )
    repo_base = tmp_path / "repositories"
    work_base = tmp_path / "worktrees"
    state_base = tmp_path / "states"
    allowed_repo = repo_base / "allowed"
    blocked_repo = repo_base / "blocked"
    _init_web_queue_repo(allowed_repo)
    _init_web_queue_repo(blocked_repo)
    monkeypatch.setenv("TANKAI_DEV_QUEUE_ENABLED", "1")
    monkeypatch.setenv("TANKAI_REPOSITORY_BASE", str(repo_base))
    monkeypatch.setenv("TANKAI_WORKTREE_BASE", str(work_base))
    monkeypatch.setenv("TANKAI_STATE_BASE", str(state_base))

    image = "tankai-worker@sha256:" + "d" * 64
    app = web_server.AppContext.from_env("127.0.0.1")
    assert app.job_queue is not None
    app.job_queue.set_policy(
        actor_user_id=owner,
        workspace_id=workspace,
        policy=WorkspaceQueuePolicy(
            tenant_id=tenant,
            workspace_id=workspace,
            max_queued=10,
            max_running=1,
            max_memory_mb=512,
            max_cpus=2,
            max_pids=128,
            max_runtime_seconds=120,
            allowed_images=[image],
        ),
    )
    allowed = app.job_queue.register_repository(
        actor_user_id=owner,
        workspace_id=workspace,
        name="Allowed",
        repository_path=allowed_repo,
        workspace_root=work_base / "allowed",
        state_path=state_base / "allowed.json",
    )
    blocked = app.job_queue.register_repository(
        actor_user_id=owner,
        workspace_id=workspace,
        name="Blocked",
        repository_path=blocked_repo,
        workspace_root=work_base / "blocked",
        state_path=state_base / "blocked.json",
    )

    server = web_server.ThreadedHTTPServer(
        ("127.0.0.1", 0), web_server.Handler, app=app
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = Client(f"http://127.0.0.1:{server.server_address[1]}")
    try:
        status, _, unauthenticated = client.get("/api/v1/capabilities")
        assert status == 401
        assert "Bearer" in unauthenticated["error"]
        status, _, unauthenticated_schema = client.get("/api/v1/job-schema")
        assert status == 401
        assert "Bearer" in unauthenticated_schema["error"]

        status, _, login = client.post(
            "/api/auth/login",
            {
                "email": "agent-owner@example.com",
                "password": "Agent-owner-password-123",
            },
        )
        assert status == 200
        csrf = login["csrf_token"]

        status, _, created_agent = client.post(
            "/api/agents",
            {"name": "External Coder", "description": "CI programming client"},
            csrf=csrf,
        )
        assert status == 201
        agent_id = created_agent["agent"]["agent_id"]

        status, _, created_token = client.post(
            f"/api/agents/{agent_id}/tokens",
            {
                "label": "integration-test",
                "scopes": [
                    "repositories:read",
                    "jobs:submit",
                    "jobs:read",
                    "jobs:cancel",
                ],
                "repository_ids": [allowed.repository_id],
                "expires_in_days": 7,
            },
            csrf=csrf,
        )
        assert status == 201
        token_id = created_token["token"]["token_id"]
        secret = created_token["token"]["secret"]
        assert secret.startswith("tkai_v1_")
        assert created_token["token"]["shown_once"] is True

        status, _, token_list = client.get(f"/api/agents/{agent_id}/tokens")
        assert status == 200
        assert token_list["tokens"][0]["token_id"] == token_id
        assert "secret" not in token_list["tokens"][0]

        status, _, capabilities = client.get(
            "/api/v1/capabilities", bearer=secret
        )
        assert status == 200
        assert capabilities["api_version"] == "v1"
        assert capabilities["agent"]["agent_id"] == agent_id
        assert capabilities["repository_ids"] == [allowed.repository_id]
        assert capabilities["job_submission"] == {
            "method": "POST",
            "path": "/api/v1/jobs",
            "preflight_path": "/api/v1/jobs/preflight",
            "schema_path": "/api/v1/job-schema",
            "schema_version": 1,
            "validation_errors": {
                "version": 1,
                "path_format": "json-pointer",
                "max_errors": 20,
            },
        }

        status, _, job_schema = client.get(
            "/api/v1/job-schema", bearer=secret
        )
        assert status == 200
        assert job_schema["api_version"] == "v1"
        assert job_schema["schema_version"] == 1
        assert job_schema["submission"] == {
            "method": "POST",
            "path": "/api/v1/jobs",
            "preflight_path": "/api/v1/jobs/preflight",
            "content_type": "application/json",
            "required_scope": "jobs:submit",
            "validation_errors": {
                "version": 1,
                "path_format": "json-pointer",
                "max_errors": 20,
            },
        }
        schema = job_schema["schema"]
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert schema["$id"] == "urn:tankai:external-agent-job-submission:v1"
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == {
            "repository_id",
            "idempotency_key",
            "pipeline",
        }
        assert schema["properties"]["idempotency_key"]["maxLength"] == 150
        assert schema["properties"]["priority"]["minimum"] == -100
        assert schema["properties"]["priority"]["maximum"] == 100
        assert schema["$defs"]["WorkerIsolationSpec"]["properties"][
            "network_mode"
        ]["pattern"] == "^none$"

        for invalid_envelope, expected_path, expected_code in (
            (
                {
                    "repository_id": allowed.repository_id,
                    "idempotency_key": "schema-priority",
                    "priority": 101,
                    "pipeline": _web_queue_pipeline(image),
                },
                "/priority",
                "less_than_equal",
            ),
            (
                {
                    "repository_id": allowed.repository_id,
                    "idempotency_key": "schema\ncontrol",
                    "pipeline": _web_queue_pipeline(image),
                },
                "/idempotency_key",
                "value_error",
            ),
        ):
            status, _, rejected = client.post(
                "/api/v1/jobs", invalid_envelope, bearer=secret
            )
            assert status == 400
            assert rejected["error"] == "Ungültiger Entwicklungsauftrag"
            assert rejected["validation"] == {
                "version": 1,
                "path_format": "json-pointer",
                "error_count": 1,
                "truncated": False,
                "errors": [{"path": expected_path, "code": expected_code}],
            }

        status, _, rejected_preflight = client.post(
            "/api/v1/jobs/preflight",
            {
                "repository_id": allowed.repository_id,
                "idempotency_key": "preflight-invalid",
                "priority": 101,
                "pipeline": _web_queue_pipeline(image),
            },
            bearer=secret,
        )
        assert status == 400
        assert rejected_preflight["validation"]["errors"] == [
            {"path": "/priority", "code": "less_than_equal"}
        ]

        oversized_error = {
            "repository_id": allowed.repository_id,
            "idempotency_key": "bounded-errors",
            "pipeline": _web_queue_pipeline(image),
            "secret\nfield": "DO_NOT_REFLECT_THIS_VALUE",
        }
        oversized_error.update(
            {f"unexpected_{index}": "DO_NOT_REFLECT_THIS_VALUE" for index in range(25)}
        )
        status, _, rejected = client.post(
            "/api/v1/jobs", oversized_error, bearer=secret
        )
        assert status == 400
        assert rejected["error"] == "Unbekannte Felder im Entwicklungsauftrag"
        assert rejected["validation"]["error_count"] == 26
        assert rejected["validation"]["truncated"] is True
        assert len(rejected["validation"]["errors"]) == 20
        assert rejected["validation"]["errors"][0] == {
            "path": "/field",
            "code": "extra_forbidden",
        }
        serialized_rejection = json.dumps(rejected)
        assert "DO_NOT_REFLECT_THIS_VALUE" not in serialized_rejection
        assert "secret\\nfield" not in serialized_rejection

        status, _, repositories = client.get(
            "/api/v1/repositories", bearer=secret
        )
        assert status == 200
        assert repositories == {
            "repositories": [
                {
                    "repository_id": allowed.repository_id,
                    "name": "Allowed",
                    "enabled": True,
                }
            ]
        }

        status, _, scope_denied = client.post(
            "/api/v1/jobs/preflight",
            {
                "repository_id": blocked.repository_id,
                "idempotency_key": "blocked-job",
                "pipeline": _web_queue_pipeline(image),
            },
            bearer=secret,
        )
        assert status == 403
        assert "nicht freigegeben" in scope_denied["error"]

        job_payload = {
            "repository_id": allowed.repository_id,
            "idempotency_key": "agent-job-1",
            "pipeline": _web_queue_pipeline(image),
        }
        status, _, preflight = client.post(
            "/api/v1/jobs/preflight", job_payload, bearer=secret
        )
        assert status == 200
        payload_bytes = preflight["preflight"].pop("payload_bytes")
        assert payload_bytes > 0
        assert preflight["preflight"] == {
            "valid": True,
            "snapshot_only": True,
            "job_enqueued": False,
            "final_submit_revalidates": True,
            "queue_capacity_reserved": False,
            "idempotency_reserved": False,
            "repository_id": allowed.repository_id,
            "image": image,
            "memory_mb": 256,
            "cpus": 1.0,
            "pids_limit": 64,
            "runtime_seconds": 40,
            "max_attempts": 3,
            "dynamic_checks_deferred": [
                "idempotency",
                "queue_capacity",
                "user_rate_limit",
            ],
        }
        assert app.job_queue.list_jobs(
            actor_user_id=owner, workspace_id=workspace
        ) == []
        assert app.auth.agent_job_ids(agent_id=agent_id, limit=100) == []

        status, _, created_job = client.post(
            "/api/v1/jobs", job_payload, bearer=secret
        )
        assert status == 202
        job_id = created_job["job"]["job_id"]
        stored_job = app.job_queue.get_job(
            actor_user_id=owner, workspace_id=workspace, job_id=job_id
        )
        assert stored_job.pipeline.worker.agent_id.startswith(
            f"EXT_{agent_id.replace('-', '')[:12]}_"
        )
        assert stored_job.pipeline.worker.agent_id != "AGENT_BACKEND_01"
        stored_job.result = {
            "run": {
                "run_id": "safe-run",
                "state": "succeeded",
                "changed_files": ["tankai/safe.py"],
                "workspace_path": "/srv/private/worktrees/secret",
            },
            "workspace": {"path": "/srv/private/worktrees/secret"},
        }
        stored_job.error = "failed under /srv/private/worktrees/secret"
        safe_payload = web_server.Handler._external_job_payload(stored_job)
        assert safe_payload["result_receipt"]["run_id"] == "safe-run"
        assert safe_payload["error"] == "Development job failed"
        assert "/srv/private" not in json.dumps(safe_payload)
        status, _, duplicate_job = client.post(
            "/api/v1/jobs", job_payload, bearer=secret
        )
        assert status == 202
        assert duplicate_job["job"]["job_id"] == job_id

        status, _, jobs = client.get("/api/v1/jobs", bearer=secret)
        assert status == 200
        assert [item["job_id"] for item in jobs["jobs"]] == [job_id]
        assert "pipeline" not in jobs["jobs"][0]
        status, _, job = client.get(f"/api/v1/jobs/{job_id}", bearer=secret)
        assert status == 200
        assert job["job"]["job_id"] == job_id

        status, _, second_agent = client.post(
            "/api/agents", {"name": "Read Only"}, csrf=csrf
        )
        assert status == 201
        second_agent_id = second_agent["agent"]["agent_id"]
        status, _, second_token = client.post(
            f"/api/agents/{second_agent_id}/tokens",
            {
                "scopes": ["jobs:read"],
                "repository_ids": [allowed.repository_id],
            },
            csrf=csrf,
        )
        assert status == 201
        second_secret = second_token["token"]["secret"]
        status, _, hidden = client.get(
            f"/api/v1/jobs/{job_id}", bearer=second_secret
        )
        assert status == 404
        assert "nicht gefunden" in hidden["error"]
        status, _, submit_denied = client.post(
            "/api/v1/jobs", job_payload, bearer=second_secret
        )
        assert status == 403
        assert "jobs:submit" in submit_denied["error"]

        status, _, cancelled = client.post(
            f"/api/v1/jobs/{job_id}/cancel", {}, bearer=secret
        )
        assert status == 200
        assert cancelled["job"]["state"] == "cancelled"

        status, _, revoked = client.post(
            f"/api/agents/{agent_id}/tokens/{token_id}/revoke", {}, csrf=csrf
        )
        assert status == 200 and revoked["ok"] is True
        status, _, invalid = client.get("/api/v1/capabilities", bearer=secret)
        assert status == 401
        assert "widerrufen" in invalid["error"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
