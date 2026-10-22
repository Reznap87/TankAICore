"""Accounting tests use scripted SDK objects, never providers or credentials."""
from concurrent.futures import ThreadPoolExecutor
import json
from types import SimpleNamespace as NS

import pytest

from tankai.core.llm import BaseLLM, BudgetedLLM, LLMCallBudget, OpenAILLM, AnthropicLLM
from tankai.core.usage import CompletionResult, UsageLedger, token_counts


class Scripted(BaseLLM):
    calls = 0
    def complete(self, prompt, **kwargs):
        self.calls += 1
        return "private answer never stored in accounting"


def openai_fixture(response):
    adapter = object.__new__(OpenAILLM)
    adapter.model = "auto:tankai"
    adapter.temperature = 0
    adapter.max_tokens = 32
    adapter.client = NS(chat=NS(completions=NS(create=lambda **kwargs: response)))
    return adapter


def test_adapter_preserves_reported_usage_without_changing_text_interface():
    adapter = openai_fixture(NS(model="reported/model", choices=[NS(message=NS(content=" answer "))],
                                usage=NS(prompt_tokens=11, completion_tokens=7, total_tokens=18,
                                         prompt_tokens_details=NS(cached_tokens=4),
                                         completion_tokens_details=NS(reasoning_tokens=2))))
    assert adapter.complete("private prompt") == "answer"
    response = adapter.complete_with_usage("private prompt")
    assert response.reported_model == "reported/model"
    assert response.tokens == {"input_tokens": 11, "output_tokens": 7, "total_tokens": 18,
                               "cached_input_tokens": 4, "reasoning_output_tokens": 2}


def test_missing_and_invalid_usage_are_not_invented_zero_counts():
    adapter = openai_fixture(NS(choices=[NS(message=NS(content="answer"))]))
    assert adapter.complete_with_usage("question").tokens == {}
    assert token_counts({"prompt_tokens": True, "completion_tokens": -1, "total_tokens": 1.5}) == {}
    assert token_counts({"prompt_tokens": 0, "completion_tokens": 0}) == {"input_tokens": 0, "output_tokens": 0}


def test_anthropic_cache_components_remain_separate_from_input_count():
    adapter = object.__new__(AnthropicLLM)
    adapter.model = "test"
    adapter.temperature = 0
    adapter.max_tokens = 32
    adapter.client = NS(messages=NS(create=lambda **kwargs: NS(
        model="test", content=[NS(text="answer")],
        usage=NS(input_tokens=10, output_tokens=3, cache_read_input_tokens=50, cache_creation_input_tokens=20))))
    result = adapter.complete_with_usage("question")
    assert result.text == "answer"
    assert result.tokens == {"input_tokens": 10, "output_tokens": 3,
                             "cache_read_input_tokens": 50, "cache_creation_input_tokens": 20}


def test_started_and_failed_attempts_survive_restart_without_private_text(tmp_path):
    path = tmp_path / "usage.db"
    ledger = UsageLedger(path)
    budget = LLMCallBudget(3)
    budget.reset(run_id="test-run")
    budget.set_usage_sink(ledger.record)
    started = budget.begin_attempt("test", "model", False)
    class Failing(Scripted):
        def complete(self, prompt, **kwargs):
            raise TimeoutError("private credentials must never be persisted")
    llm = BudgetedLLM(Failing(), budget)
    with pytest.raises(TimeoutError):
        llm.complete("private prompt")
    ledger.close()
    ledger = UsageLedger(path)
    try:
        summary = ledger.summary("test-run")
        assert summary["adapter_attempts"] == 2
        assert summary["attempts_with_incomplete_usage"] == 2
        assert summary["reported_tokens"] == {}
        assert summary["cost_status"] == "not_priced"
        assert {a["status"] for a in summary["attempts"]} == {"started", "failed"}
        assert "private" not in json.dumps(summary)
        assert summary["upstream_attempts_confirmed"] is False
    finally:
        ledger.close()


def test_no_sdk_dispatch_when_durable_accounting_cannot_start():
    budget = LLMCallBudget(3)
    def failed_sink(event):
        raise OSError("disk unavailable")
    budget.set_usage_sink(failed_sink)
    delegate = Scripted()
    llm = BudgetedLLM(delegate, budget)
    with pytest.raises(OSError):
        llm.complete("question")
    assert delegate.calls == 0


def test_parallel_attempts_are_durable_and_reset_does_not_mix_runs(tmp_path):
    ledger = UsageLedger(tmp_path / "usage.db")
    budget = LLMCallBudget(8)
    budget.set_usage_sink(ledger.record)
    budget.reset(run_id="first")
    llm = BudgetedLLM(Scripted(), budget)
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            assert len(list(pool.map(llm.complete, ["question"] * 8))) == 8
        first = ledger.summary("first")
        assert first["adapter_attempts"] == 8
        assert {a["adapter_attempt"] for a in first["attempts"]} == set(range(1, 9))
        assert len({a["attempt_id"] for a in first["attempts"]}) == 8
        budget.reset(run_id="second")
        assert budget.usage_snapshot()["adapter_attempts"] == 0
        llm.complete("question")
        assert ledger.summary("second")["adapter_attempts"] == 1
        assert ledger.summary("first")["adapter_attempts"] == 8
        assert "private answer" not in json.dumps(first)
    finally:
        ledger.close()


def test_whitelist_drops_provider_extras_and_secret_shaped_labels(tmp_path):
    ledger = UsageLedger(tmp_path / "usage.db")
    budget = LLMCallBudget(1)
    budget.set_usage_sink(ledger.record)
    event = budget.begin_attempt("test", "sk-DO-NOT-STORE", False)
    event.update(reported_model="https://private.example/key", tokens={"input_tokens": 4, "key": "secret"},
                 private_prompt="secret", status="completed")
    ledger.record(event)
    try:
        data = ledger.summary(event["run_id"])
        assert data["reported_tokens"] == {"input_tokens": 4}
        assert "secret" not in json.dumps(data)
        assert "DO-NOT-STORE" not in json.dumps(data)
        assert data["attempts"][0]["requested_model"] is None
        assert data["attempts"][0]["reported_model"] is None
    finally:
        ledger.close()
