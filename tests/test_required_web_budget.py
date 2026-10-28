"""Real HTTP admission must never fall through to unpriced legacy adapters."""
from __future__ import annotations

from contextlib import contextmanager
import threading

import pytest

from tankai.core.llm import MockLLM
from tankai.web import runtime as runtime_module, server as web_server
from test_request_dispatch import sdks
from test_web_owner_pilot import accounts, ledger, provider_factory, login, calls
from test_web_server import Client


@contextmanager
def unconfigured_server():
    app = web_server.AppContext.from_env("127.0.0.1")
    server = web_server.ThreadedHTTPServer(("127.0.0.1", 0), web_server.Handler, app=app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield app, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(5)
        server.server_close()


@pytest.mark.parametrize("simulation", [None, "main", "critic"])
def test_real_or_mixed_adapter_without_shared_budget_never_sends(accounts, provider_factory, monkeypatch, simulation):
    monkeypatch.setenv("TANKAI_REQUIRE_INDEPENDENT_CRITIC", "0")
    if simulation == "main":
        monkeypatch.setattr(runtime_module, "get_llm", lambda: MockLLM())
    if simulation == "critic":
        monkeypatch.setattr(runtime_module, "get_critic_llm", lambda default: MockLLM())
    with unconfigured_server() as (app, base):
        client, csrf = login(base)
        for _ in range(2):
            status, _, body = client.post("/api/run", {"goal": "Analyse fixture", "parallel": False}, csrf=csrf)
            assert status == 503, body
            assert body["error_code"] == "monetary_admission_required"
        assert calls(provider_factory) == 0
        assert client.get("/api/auth/me")[0] == 200
        assert client.get("/api/history")[0] == 200
        assert client.get("/api/history")[2] == []


def test_closed_money_gate_still_preserves_session_and_csrf_checks(accounts, provider_factory):
    with unconfigured_server() as (_, base):
        assert Client(base).post("/api/run", {"goal": "fixture"})[0] == 401
        client, _ = login(base)
        assert client.post("/api/run", {"goal": "fixture"})[0] == 403
        assert calls(provider_factory) == 0


def test_health_reports_policy_without_claiming_production_or_configured_budget(accounts, provider_factory):
    with unconfigured_server() as (_, base):
        public = Client(base).get("/api/health")[2]
        assert public["live_model_requests_require_budget"] is True
        assert public["monetary_admission_configured"] is False
        assert public["production_ready"] is False
        client, _ = login(base)
        health = client.get("/api/health")[2]
        assert health["execution_mode"] == "live"
        assert health["monetary_admission_configured"] is False
        assert health["verification_ready"] is False
        assert calls(provider_factory) == 0
