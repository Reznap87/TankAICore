"""Offline integration tests for the explicit zero-cost OpenRouter route.

No network calls, no real API keys, and no production provider authorization.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import sys

import pytest

from tankai.core.llm import (
    LLMRateLimitExceeded, OpenRouterFreeLLM, get_llm, openrouter_free_scope,
)
from tankai.core.request_dispatch import DispatchDenied
from tankai.web import auth as auth_module
from tankai.web import server as web_server
from test_web_owner_pilot import serving
from test_web_server import Client, _configure


@pytest.fixture
def fake_openrouter(monkeypatch):
    calls = []
    clients = []

    class Transport:
        def __init__(self, **kwargs):
            assert kwargs["follow_redirects"] is False
            assert kwargs["trust_env"] is False
            self.kwargs = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class ClientStub:
        def __init__(self, *, root=None, **kwargs):
            if root is None:
                assert kwargs["base_url"] == "https://openrouter.ai/api/v1"
                assert kwargs["api_key"] == "synthetic-local-key"
                assert kwargs["max_retries"] == 0
                self.root = self
                self.reply_model = "nvidia/synthetic-free:free"
                clients.append(self)
            else:
                self.root = root
            self._client = kwargs.get("http_client")
            self.max_retries = kwargs.get("max_retries", 0)
            self.base_url = kwargs.get("base_url", "https://openrouter.ai/api/v1")
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

        def with_options(self, **kwargs):
            assert kwargs["max_retries"] == 0
            assert kwargs["timeout"] == 30.0
            assert kwargs["http_client"] is not None
            return ClientStub(root=self.root, **kwargs)

        def create(self, **parameters):
            calls.append(parameters)
            return SimpleNamespace(
                model=self.root.reply_model,
                choices=[SimpleNamespace(
                    message=SimpleNamespace(content="Antwort vom kostenlosen Testmodell")
                )],
            )

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(
        OpenAI=lambda **kwargs: ClientStub(**kwargs),
        DefaultHttpxClient=Transport,
    ))
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-local-key")
    monkeypatch.setenv("OPENROUTER_MODEL", "openrouter/free")
    return SimpleNamespace(calls=calls, clients=clients)


def test_free_adapter_accepts_only_free_models_and_requires_scope(fake_openrouter):
    model = get_llm("openrouter")
    assert type(model) is OpenRouterFreeLLM
    assert model.model == "openrouter/free"
    assert model.provider_name == "openrouter_free"
    with pytest.raises(DispatchDenied):
        model.complete("private chat")
    assert fake_openrouter.calls == []

    with openrouter_free_scope():
        assert model.complete("test prompt", system="test system") == (
            "Antwort vom kostenlosen Testmodell"
        )
    assert len(fake_openrouter.calls) == 1
    assert fake_openrouter.calls[0]["model"] == "openrouter/free"
    assert fake_openrouter.calls[0]["max_tokens"] == 512
    assert model.resolved_model == "nvidia/synthetic-free:free"

    with pytest.raises(ValueError):
        get_llm("openrouter", model="openrouter/auto")
    with pytest.raises(ValueError):
        get_llm("openrouter", model="openai/gpt-paid")
    with pytest.raises(ValueError):
        get_llm("openrouter", model="openrouter/free:paid")
    with pytest.raises(ValueError):
        get_llm("openrouter", base_url="https://example.invalid/v1")
    with openrouter_free_scope(), pytest.raises(DispatchDenied):
        model.complete("test prompt", max_tokens=513)
    assert len(fake_openrouter.calls) == 1


def test_free_adapter_rejects_unproved_response_model(fake_openrouter):
    adapter = get_llm("openrouter")
    fake_openrouter.clients[0].reply_model = "openai/synthetic-paid"
    with openrouter_free_scope(), pytest.raises(DispatchDenied):
        adapter.complete("test")
    assert adapter.resolved_model == ""


def _setup_free(monkeypatch, tmp_path):
    store = _configure(monkeypatch, tmp_path)
    monkeypatch.setenv("TANKAI_LLM", "openrouter")
    monkeypatch.setenv("TANKAI_OPENROUTER_FREE_ENABLED", "1")
    monkeypatch.setenv("TANKAI_LIVE_PROVIDER_ENABLED", "1")
    monkeypatch.setenv("TANKAI_EMBEDDER", "hashing")
    monkeypatch.delenv("TANKAI_CRITIC_LLM", raising=False)
    store.create_user_with_tenant(
        email="free-owner@example.test", password="Safe-free-test-password-123",
        display_name="Free Owner", tenant_name="Free Tenant",
    )
    return store


def test_authenticated_free_chat_returns_real_model_identity_and_history(
    monkeypatch, tmp_path, fake_openrouter
):
    _setup_free(monkeypatch, tmp_path)
    with serving(None) as (app, base):
        client = Client(base)
        status, _, login = client.post("/api/auth/login", {
            "email": "free-owner@example.test",
            "password": "Safe-free-test-password-123",
        })
        assert status == 200
        status, _, health = client.get("/api/health")
        assert status == 200 and health["direct_chat_available"] is True
        assert health["free_route_admission_configured"] is True
        assert health["production_ready"] is False

        status, _, answer = client.post(
            "/api/chat", {"message": "Hallo, TankAI"}, csrf=login["csrf_token"]
        )
        assert status == 200, answer
        assert answer["final_answer"] == "Antwort vom kostenlosen Testmodell"
        assert answer["main_llm"].startswith("openrouter_free:nvidia/synthetic-free:free:")
        assert answer["free_model_admission"]["per_day"] == 50
        assert answer["critic_used"] is False
        assert answer["release_ready"] is False
        assert len(fake_openrouter.calls) == 1

        status, _, follow_up = client.post(
            "/api/chat", {"message": "Was habe ich gefragt?", "reply_to": answer["run_id"]},
            csrf=login["csrf_token"],
        )
        assert status == 200, follow_up
        assert follow_up["context_turns_used"] == 1
        assert "Hallo, TankAI" in fake_openrouter.calls[1]["messages"][-1]["content"]
        assert len(fake_openrouter.calls) == 2
        status, _, history = client.get("/api/history")
        assert status == 200 and len(history) == 2
        assert history[0]["reply_to"] == answer["run_id"]
        with app.runtimes.acquire(
            tenant_id=login["workspace"]["tenant_id"] if "tenant_id" in login["workspace"] else login["user"]["tenant_id"],
            workspace_id=login["workspace"]["id"],
        ) as runtime:
            with pytest.raises(DispatchDenied):
                runtime.tank.llm.complete("unapproved extra request")
        assert len(fake_openrouter.calls) == 2


def test_free_mode_rejects_paid_critic_before_runtime_start(monkeypatch, tmp_path):
    _setup_free(monkeypatch, tmp_path)
    monkeypatch.setenv("TANKAI_CRITIC_LLM", "openai")
    with pytest.raises(RuntimeError, match="kostenpflichtigen Critic"):
        web_server.AppContext.from_env("127.0.0.1")


def test_free_route_denied_when_global_flag_is_off(monkeypatch, tmp_path, fake_openrouter):
    _setup_free(monkeypatch, tmp_path)
    monkeypatch.setenv("TANKAI_LIVE_PROVIDER_ENABLED", "0")
    with serving(None) as (_, base):
        client = Client(base)
        _, _, login = client.post("/api/auth/login", {
            "email": "free-owner@example.test",
            "password": "Safe-free-test-password-123",
        })
        status, _, health = client.get("/api/health")
        assert status == 200 and health["direct_chat_available"] is False
        status, _, denied = client.post("/api/chat", {"message": "blocked"}, csrf=login["csrf_token"])
        assert status == 503 and denied["error_code"] == "monetary_admission_required"
        assert fake_openrouter.calls == []


def test_free_key_has_shared_persistent_minute_and_day_limits(monkeypatch, tmp_path):
    store = _configure(monkeypatch, tmp_path)
    current = [datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)]
    monkeypatch.setattr(auth_module, "_utcnow", lambda: current[0])
    app = web_server.AppContext.from_env("127.0.0.1")
    try:
        for minute, count in enumerate((20, 20, 10)):
            current[0] = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc) + timedelta(minutes=minute * 2)
            for _ in range(count):
                app.consume_openrouter_free_quota()
        with pytest.raises(LLMRateLimitExceeded) as blocked:
            app.consume_openrouter_free_quota()
        assert blocked.value.limit == 50
        assert blocked.value.window_seconds == 86_400
        # A separate context sees the same store and cannot bypass the daily ceiling.
        reopened = web_server.AppContext.from_env("127.0.0.1")
        try:
            with pytest.raises(LLMRateLimitExceeded):
                reopened.consume_openrouter_free_quota()
        finally:
            reopened.close()
    finally:
        app.close()
