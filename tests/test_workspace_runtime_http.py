"""Real loopback requests; occupied runtime capacity cannot start inference."""
from __future__ import annotations

import threading
from uuid import uuid4

from tankai.web import server as web_server
from test_web_server import Client, _configure


def test_occupied_runtime_capacity_returns_retryable_503_on_every_workspace_route(tmp_path, monkeypatch):
    store = _configure(monkeypatch, tmp_path)
    monkeypatch.setenv("TANKAI_RUNTIME_CACHE_MAX", "1")
    monkeypatch.setenv("TANKAI_EMBEDDER", "hashing")
    identities = []
    for name in ("active", "waiting"):
        _, tenant, workspace = store.create_user_with_tenant(
            email=name + "@example.com", password="Runtime-cache-test-password-123",
            display_name=name, tenant_name=name,
        )
        identities.append((tenant, workspace))
    app = web_server.AppContext.from_env("127.0.0.1")
    server = web_server.ThreadedHTTPServer(("127.0.0.1", 0), web_server.Handler, app=app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = Client(f"http://127.0.0.1:{server.server_address[1]}")
    try:
        status, _, login = client.post("/api/auth/login", {
            "email": "waiting@example.com", "password": "Runtime-cache-test-password-123",
        })
        assert status == 200
        csrf = login["csrf_token"]
        run_id = str(uuid4())
        routes = [
            ("GET", "/api/health", None),
            ("GET", "/api/history", None),
            ("POST", "/api/run", {"goal": "No model may run while capacity is full."}),
        ]
        tenant, workspace = identities[0]
        with app.runtimes.acquire(tenant_id=tenant, workspace_id=workspace) as active:
            active.history.append({"goal": "Stored active workspace data"})
            before = active.tank.llm_call_budget.snapshot()
            for method, path, body in routes:
                status, headers, payload = (client.get(path) if method == "GET"
                                             else client.post(path, body, csrf=csrf))
                assert status == 503, path
                assert headers["Retry-After"] == "1"
                assert set(payload) == {"error"}
                assert "503" not in payload["error"]
                assert len(app.runtimes._runtimes) == 1
                assert not active._closed
                assert active.tank.llm_call_budget.snapshot() == before
        status, _, payload = client.get("/api/history")
        assert status == 200 and payload == []
        assert active._closed
        assert len(app.runtimes._runtimes) == 1
        with app.runtimes.acquire(tenant_id=tenant, workspace_id=workspace) as restored:
            assert restored is not active
            assert restored.history.list_recent()[0]["goal"] == "Stored active workspace data"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
