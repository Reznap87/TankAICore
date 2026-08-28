from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / ".github" / "scripts" / "production_live_provider_readiness.py"
SPEC = importlib.util.spec_from_file_location("production_live_provider_readiness", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)
evaluate = MODULE.evaluate


def _write(root: Path, relative: str, content: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _fixture(root: Path, *, wired: bool) -> None:
    llm = """
class OpenAILLM: pass
class AnthropicLLM: pass
OPENAI_MODEL fehlt; Modell muss explizit konfiguriert werden
ANTHROPIC_MODEL fehlt; Modell muss explizit konfiguriert werden
"""
    if wired:
        llm += "\n".join(
            (
                "TANKAI_LLM_MAX_TOKENS",
                "TANKAI_LLM_TIMEOUT_SECONDS",
                "TANKAI_LLM_MAX_RETRIES",
                "TANKAI_LLM_MAX_CALLS_PER_RUN",
                "TANKAI_LIVE_SMOKE_MAX_TOKENS",
                "class LLMCallBudget",
            )
        )
    _write(root, "tankai/core/llm.py", llm)
    _write(
        root,
        "tankai/web/runtime.py",
        "get_critic_llm TANKAI_REQUIRE_INDEPENDENT_CRITIC",
    )
    _write(
        root,
        "tankai/web/auth.py",
        "class ProviderCallRateLimiter provider_call_events",
    )
    _write(
        root,
        "tankai/web/server.py",
        "TANKAI_PROVIDER_CALLS_PER_WINDOW TANKAI_PROVIDER_RATE_WINDOW_SECONDS "
        "set_call_guard LLMRateLimitExceeded provider_limiter.consume(context.user_id, identity)",
    )
    _write(
        root,
        "tankai/core/web_research.py",
        'provider == "brave"\nprovider == "tavily"\n',
    )
    if wired:
        cloudflare = """
TANKAI_LIVE_PROVIDER_ENABLED?: string;
private readonly liveProviderEnabled = enabled(this.env.TANKAI_LIVE_PROVIDER_ENABLED);
TANKAI_LLM: this.liveProviderEnabled ? clean(this.env.TANKAI_LLM) : "mock",
OPENAI_API_KEY: this.liveProviderEnabled ? clean(this.env.OPENAI_API_KEY) : "",
ANTHROPIC_API_KEY: this.liveProviderEnabled ? clean(this.env.ANTHROPIC_API_KEY) : "",
TANKAI_CRITIC_LLM: this.liveProviderEnabled ? clean(this.env.TANKAI_CRITIC_LLM) : "",
TANKAI_REQUIRE_INDEPENDENT_CRITIC: this.liveProviderEnabled ? "1" : "0",
TANKAI_SEARCH_PROVIDER: this.liveProviderEnabled ? clean(this.env.TANKAI_SEARCH_PROVIDER) : "",
BRAVE_SEARCH_API_KEY: this.liveProviderEnabled ? clean(this.env.BRAVE_SEARCH_API_KEY) : "",
TAVILY_API_KEY: this.liveProviderEnabled ? clean(this.env.TAVILY_API_KEY) : "",
TANKAI_LLM_MAX_TOKENS: clean(this.env.TANKAI_LLM_MAX_TOKENS, "2048"),
TANKAI_LLM_TIMEOUT_SECONDS: clean(this.env.TANKAI_LLM_TIMEOUT_SECONDS, "30"),
TANKAI_LLM_MAX_RETRIES: clean(this.env.TANKAI_LLM_MAX_RETRIES, "1"),
TANKAI_LLM_MAX_CALLS_PER_RUN: clean(this.env.TANKAI_LLM_MAX_CALLS_PER_RUN, "40"),
TANKAI_PROVIDER_CALLS_PER_WINDOW: clean(this.env.TANKAI_PROVIDER_CALLS_PER_WINDOW, "40"),
TANKAI_PROVIDER_RATE_WINDOW_SECONDS: clean(this.env.TANKAI_PROVIDER_RATE_WINDOW_SECONDS, "60"),
TANKAI_LIVE_SMOKE_MAX_TOKENS: clean(this.env.TANKAI_LIVE_SMOKE_MAX_TOKENS, "256")
"""
    else:
        cloudflare = 'TANKAI_LLM: "mock",\nTANKAI_EMBEDDER: "hashing",\n'
    _write(root, "src/cloudflare.ts", cloudflare)
    _write(root, "DEPLOY.md", "Rollback to the verified mock baseline.")
    _write(root, "TANKAI_MASTERPLAN.md", "ROLLBACK remains mandatory.")


def _check(result: dict[str, object], name: str) -> dict[str, str]:
    checks = result["checks"]
    assert isinstance(checks, list)
    for item in checks:
        assert isinstance(item, dict)
        if item.get("name") == name:
            return item
    raise AssertionError(f"missing check {name}")


def test_readiness_fails_closed_for_mock_only_container(tmp_path: Path) -> None:
    _fixture(tmp_path, wired=False)
    result = evaluate(tmp_path)
    assert result["overall"] == "NOT_READY"
    assert result["read_only"] is True
    assert result["secret_values_requested"] is False
    assert result["paid_provider_call"] is False
    assert result["deployment_triggered"] is False
    assert _check(result, "container_provider_selection")["status"] == "FAIL"
    assert _check(result, "live_mode_fail_closed")["status"] == "FAIL"
    assert _check(result, "provider_secret_forwarding")["status"] == "FAIL"
    assert _check(result, "runtime_budget_contract")["status"] == "FAIL"


def test_fully_wired_explicit_independent_target_can_be_ready(tmp_path: Path) -> None:
    _fixture(tmp_path, wired=True)
    result = evaluate(
        tmp_path,
        main_provider="openai",
        main_model="main-model",
        critic_provider="anthropic",
        critic_model="critic-model",
        search_provider="brave",
        present_secrets={"OPENAI_API_KEY", "ANTHROPIC_API_KEY", "BRAVE_SEARCH_API_KEY"},
    )
    assert result["overall"] == "READY"
    assert all(item["status"] == "PASS" for item in result["checks"])


def test_same_provider_family_is_not_independent(tmp_path: Path) -> None:
    _fixture(tmp_path, wired=True)
    result = evaluate(
        tmp_path,
        main_provider="openai",
        main_model="main-model",
        critic_provider="openai",
        critic_model="critic-model",
        search_provider="tavily",
        present_secrets={"OPENAI_API_KEY", "TAVILY_API_KEY"},
    )
    target = _check(result, "target_provider_policy")
    assert target["status"] == "FAIL"
    assert "must differ" in target["detail"]
    assert result["overall"] == "NOT_READY"


def test_secret_check_uses_names_only_and_reports_missing_name(tmp_path: Path) -> None:
    _fixture(tmp_path, wired=True)
    fake_secret_value = "super-secret-value-must-never-appear"
    result = evaluate(
        tmp_path,
        main_provider="anthropic",
        main_model="main-model",
        critic_provider="openai",
        critic_model="critic-model",
        search_provider="brave",
        present_secrets={"ANTHROPIC_API_KEY", "OPENAI_API_KEY"},
    )
    secret_check = _check(result, "required_secret_presence")
    assert secret_check["status"] == "FAIL"
    assert "BRAVE_SEARCH_API_KEY" in secret_check["detail"]
    assert fake_secret_value not in repr(result)


def test_repository_run_without_target_selection_is_unknown_only_for_target_metadata(tmp_path: Path) -> None:
    _fixture(tmp_path, wired=True)
    result = evaluate(tmp_path)
    assert result["overall"] == "UNKNOWN"
    assert _check(result, "target_provider_policy")["status"] == "UNKNOWN"
    assert _check(result, "required_secret_presence")["status"] == "UNKNOWN"
