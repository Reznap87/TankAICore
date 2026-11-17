"""Offline contract tests for the live chat gate; no network or paid calls."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


PATH = Path(__file__).resolve().parents[1] / "scripts" / "chat_mvp_probe.py"
SPEC = importlib.util.spec_from_file_location("chat_mvp_probe", PATH)
assert SPEC and SPEC.loader
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


def live_health():
    return {
        "ok": True,
        "execution_mode": "live",
        "live_provider": True,
        "critic_independent": True,
        "verification_ready": True,
        "user": "synthetic@example.test",
        "workspace": "Synthetic workspace",
        "llm": "openai:main",
        "critic_llm": "anthropic:critic",
    }


def live_result():
    return {
        "workspace_id": "workspace-id",
        "status": "completed",
        "execution_mode": "live",
        "release_ready": True,
        "verification_passed": True,
        "critic_independent": True,
        "main_llm": "openai:main",
        "critic_llm": "anthropic:critic",
        "final_answer": "TankAI ist erreichbar.",
    }


@pytest.mark.parametrize("url", [
    "http://example.com",
    "https://user:password@example.com",
    "https://example.com/api/health",
    "https://example.com?token=secret",
    "file:///etc/passwd",
])
def test_disallows_unsafe_origins(url):
    with pytest.raises(probe.ProbeError):
        probe.checked_origin(url)


def test_local_http_allowed_but_not_remote():
    assert probe.checked_origin("http://127.0.0.1:8765/") == "http://127.0.0.1:8765"


def login_payload():
    return {
        "csrf_token": "synthetic-token",
        "user": {"email": "synthetic@example.test"},
        "workspace": {"id": "workspace-id", "name": "Synthetic workspace"},
    }


@pytest.mark.parametrize("delta", [
    {"csrf_token": ""},
    {"user": {"email": "other@example.test"}},
    {"user": None},
    {"workspace": {"id": "", "name": "Synthetic workspace"}},
    {"workspace": None},
])
def test_login_must_be_bound_to_requested_account_and_workspace(delta):
    with pytest.raises(probe.ProbeError):
        probe.checked_login(login_payload() | delta, "synthetic@example.test")


@pytest.mark.parametrize("delta", [
    {"execution_mode": "simulation", "live_provider": False},
    {"execution_mode": "mixed", "live_provider": False},
    {"critic_independent": False},
    {"verification_ready": False},
    {"llm": ""},
    {"critic_llm": None},
    {"user": "other@example.test"},
    {"workspace": ""},
    {"workspace": "Other workspace"},
    {"ok": False},
])
def test_readiness_fails_closed(delta):
    data = live_health() | delta
    with pytest.raises(probe.ProbeError):
        probe.checked_health(
            data,
            expected_email="synthetic@example.test",
            expected_workspace_name="Synthetic workspace",
        )


@pytest.mark.parametrize("delta", [
    {"status": "simulated"},
    {"status": "blocked"},
    {"execution_mode": "simulation"},
    {"release_ready": False},
    {"verification_passed": False},
    {"critic_independent": False},
    {"main_llm": "other"},
    {"critic_llm": "other"},
    {"workspace_id": "other-workspace-id"},
    {"final_answer": "  "},
])
def test_result_fails_closed(delta):
    models = probe.checked_health(live_health(), expected_workspace_id="workspace-id")
    with pytest.raises(probe.ProbeError):
        probe.checked_result(live_result() | delta, models)


def test_offline_contract_succeeds_for_valid_live_response():
    models = probe.checked_health(live_health(), expected_workspace_id="workspace-id")
    assert probe.checked_result(live_result(), models)["live_answer_verified"] is True


def test_readonly_preflight_never_sends_run(monkeypatch, capsys):
    monkeypatch.setenv("TANKAI_SMOKE_EMAIL", "synthetic@example.test")
    monkeypatch.setenv("TANKAI_SMOKE_PASSWORD", "synthetic-password")
    paths = []

    def fake_request(opener, origin, path, *, body=None, csrf=""):
        paths.append(path)
        return login_payload() if path.endswith("login") else live_health()

    monkeypatch.setattr(probe, "request_json", fake_request)
    assert probe.main(["--base-url", "http://127.0.0.1:8765"]) == 0
    assert paths == ["/api/auth/login", "/api/health"]
    assert '"live_answer_verified": false' in capsys.readouterr().out


def test_run_requires_two_explicit_approvals(monkeypatch, capsys):
    monkeypatch.setenv("TANKAI_SMOKE_EMAIL", "synthetic@example.test")
    monkeypatch.setenv("TANKAI_SMOKE_PASSWORD", "synthetic-password")
    def forbidden(*args, **kwargs):
        raise AssertionError("Must not perform network request")
    monkeypatch.setattr(probe, "request_json", forbidden)
    assert probe.main(["--run"]) == 1
    assert probe.main(["--run", "--approve-provider-cost"]) == 1
    assert '"readiness": "failed"' in capsys.readouterr().err


def test_mock_mode_never_sends_run_even_with_approvals(monkeypatch):
    monkeypatch.setenv("TANKAI_SMOKE_EMAIL", "synthetic@example.test")
    monkeypatch.setenv("TANKAI_SMOKE_PASSWORD", "synthetic-password")
    monkeypatch.setenv("TANKAI_ALLOW_BILLED_SMOKE", "1")
    paths = []

    def fake_request(opener, origin, path, *, body=None, csrf=""):
        paths.append(path)
        if path.endswith("login"):
            return login_payload()
        if path.endswith("health"):
            return live_health() | {"execution_mode": "simulation", "live_provider": False}
        raise AssertionError("Mock mode must never run")

    monkeypatch.setattr(probe, "request_json", fake_request)
    assert probe.main(["--run", "--approve-provider-cost"]) == 1
    assert paths == ["/api/auth/login", "/api/health"]


def test_approved_live_path_checks_final_model_identity(monkeypatch, capsys):
    monkeypatch.setenv("TANKAI_SMOKE_EMAIL", "synthetic@example.test")
    monkeypatch.setenv("TANKAI_SMOKE_PASSWORD", "synthetic-password")
    monkeypatch.setenv("TANKAI_ALLOW_BILLED_SMOKE", "1")
    paths = []

    def fake_request(opener, origin, path, *, body=None, csrf=""):
        paths.append((path, csrf))
        if path.endswith("login"):
            return login_payload()
        if path.endswith("health"):
            return live_health()
        return live_result()

    monkeypatch.setattr(probe, "request_json", fake_request)
    assert probe.main(["--run", "--approve-provider-cost"]) == 0
    assert paths == [
        ("/api/auth/login", ""),
        ("/api/health", ""),
        ("/api/run", "synthetic-token"),
    ]
    assert '"live_answer_verified": true' in capsys.readouterr().out
