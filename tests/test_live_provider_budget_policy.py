from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from tankai.core.llm import AnthropicLLM, OpenAILLM, live_smoke_max_tokens


def test_openai_adapter_applies_bounded_runtime_controls(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class FakeOpenAI:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=FakeOpenAI))
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_MODEL", "test-main-model")
    monkeypatch.setenv("TANKAI_LLM_MAX_TOKENS", "1536")
    monkeypatch.setenv("TANKAI_LLM_TIMEOUT_SECONDS", "24")
    monkeypatch.setenv("TANKAI_LLM_MAX_RETRIES", "1")

    llm = OpenAILLM()

    assert llm.max_tokens == 1536
    assert llm.timeout_seconds == 24.0
    assert llm.max_retries == 1
    assert captured["api_key"] == "test-key"
    assert captured["timeout"] == 24.0
    assert captured["max_retries"] == 1


def test_anthropic_adapter_applies_bounded_runtime_controls(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class FakeAnthropic:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    monkeypatch.setitem(sys.modules, "anthropic", SimpleNamespace(Anthropic=FakeAnthropic))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("ANTHROPIC_MODEL", "test-critic-model")
    monkeypatch.setenv("TANKAI_LLM_MAX_TOKENS", "1024")
    monkeypatch.setenv("TANKAI_LLM_TIMEOUT_SECONDS", "18.5")
    monkeypatch.setenv("TANKAI_LLM_MAX_RETRIES", "2")

    llm = AnthropicLLM()

    assert llm.max_tokens == 1024
    assert llm.timeout_seconds == 18.5
    assert llm.max_retries == 2
    assert captured == {
        "api_key": "test-key",
        "timeout": 18.5,
        "max_retries": 2,
    }


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("TANKAI_LLM_MAX_TOKENS", "0"),
        ("TANKAI_LLM_MAX_TOKENS", "8193"),
        ("TANKAI_LLM_TIMEOUT_SECONDS", "0.5"),
        ("TANKAI_LLM_TIMEOUT_SECONDS", "121"),
        ("TANKAI_LLM_MAX_RETRIES", "4"),
    ],
)
def test_invalid_provider_budget_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
) -> None:
    class FakeOpenAI:
        def __init__(self, **_: object) -> None:
            pass

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=FakeOpenAI))
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_MODEL", "test-model")
    monkeypatch.setenv(name, value)

    with pytest.raises(RuntimeError):
        OpenAILLM()


def test_live_smoke_budget_is_independently_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TANKAI_LIVE_SMOKE_MAX_TOKENS", raising=False)
    assert live_smoke_max_tokens() == 256

    monkeypatch.setenv("TANKAI_LIVE_SMOKE_MAX_TOKENS", "128")
    assert live_smoke_max_tokens() == 128

    monkeypatch.setenv("TANKAI_LIVE_SMOKE_MAX_TOKENS", "1025")
    with pytest.raises(RuntimeError):
        live_smoke_max_tokens()
