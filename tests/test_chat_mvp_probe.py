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
        "workspace_id": "workspace-id",
        "llm": "openai:main",
        "critic_llm": "anthropic:critic",
    }


def live_result():
    return {
        "run_id": "11111111-2222-4333-8444-555555555555",
        "workspace_id": "workspace-id",
        "goal": "Synthetic smoke goal",
        "status": "completed",
        "execution_mode": "live",
        "release_ready": True,
        "verification_passed": True,
        "critic_independent": True,
        "main_llm": "openai:main-resolved-2026-11-22",
        "critic_llm": "anthropic:critic-resolved-2026-11-22",
        "observed_main_llms": ["openai:main-resolved-2026-11-22"],
        "observed_critic_llms": ["anthropic:critic-resolved-2026-11-22"],
        "configured_main_llm": "openai:main",
        "configured_critic_llm": "anthropic:critic",
        "model_identity_verified": True,
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
    {"workspace_id": ""},
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


def test_readiness_rejects_same_named_workspace_with_different_id():
    with pytest.raises(probe.ProbeError, match="different workspace"):
        probe.checked_health(
            live_health() | {"workspace_id": "other-workspace-id"},
            expected_email="synthetic@example.test",
            expected_workspace_id="workspace-id",
            expected_workspace_name="Synthetic workspace",
        )


@pytest.mark.parametrize("delta", [
    {"status": "simulated"},
    {"status": "blocked"},
    {"execution_mode": "simulation"},
    {"release_ready": False},
    {"verification_passed": False},
    {"critic_independent": False},
    {"model_identity_verified": False},
    {"configured_main_llm": "other"},
    {"configured_critic_llm": "other"},
    {"main_llm": ""},
    {"critic_llm": ""},
    {"observed_main_llms": []},
    {"observed_main_llms": ["openai:main-resolved-2026-11-22", "openai:other"]},
    {"observed_critic_llms": []},
    {"observed_critic_llms": ["anthropic:other"]},
    {"main_llm": "anthropic:critic-resolved-2026-11-22"},
    {"run_id": ""},
    {"run_id": "not-a-run-id"},
    {"goal": "Different smoke goal"},
    {"workspace_id": "other-workspace-id"},
    {"final_answer": "  "},
])
def test_result_fails_closed(delta):
    models = probe.checked_health(live_health(), expected_workspace_id="workspace-id")
    with pytest.raises(probe.ProbeError):
        probe.checked_result(
            live_result() | delta,
            models,
            expected_goal="Synthetic smoke goal",
        )


def test_offline_contract_succeeds_for_valid_live_response():
    models = probe.checked_health(live_health(), expected_workspace_id="workspace-id")
    receipt = probe.checked_result(
        live_result(),
        models,
        expected_goal="Synthetic smoke goal",
    )
    assert receipt["live_answer_verified"] is True
    assert receipt["run_id"] == "11111111-2222-4333-8444-555555555555"


@pytest.mark.parametrize("history", [
    [],
    [live_result(), live_result()],
    [live_result() | {"run_id": "22222222-3333-4444-8555-666666666666"}],
    [live_result() | {"final_answer": "Different stored answer"}],
    [live_result() | {"observed_main_llms": ["openai:other"]}],
])
def test_history_must_contain_one_identical_completed_run(history):
    with pytest.raises(probe.ProbeError):
        probe.checked_history(history, live_result())


def test_history_contract_succeeds_for_identical_completed_run():
    assert probe.checked_history([live_result()], live_result()) == {
        "history_verified": True,
    }


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
    submitted_goal = []

    def fake_request(opener, origin, path, *, body=None, csrf="", expected_type=dict):
        paths.append((path, csrf))
        if path.endswith("login"):
            return login_payload()
        if path.endswith("health"):
            return live_health()
        if path.endswith("run"):
            submitted_goal.append(body["goal"])
            return live_result() | {"goal": body["goal"]}
        assert expected_type is list
        return [live_result() | {"goal": submitted_goal[0]}]

    monkeypatch.setattr(probe, "request_json", fake_request)
    assert probe.main(["--run", "--approve-provider-cost"]) == 0
    assert paths == [
        ("/api/auth/login", ""),
        ("/api/health", ""),
        ("/api/run", "synthetic-token"),
        ("/api/history", ""),
    ]
    output = capsys.readouterr().out
    assert '"live_answer_verified": true' in output
    assert '"history_verified": true' in output
