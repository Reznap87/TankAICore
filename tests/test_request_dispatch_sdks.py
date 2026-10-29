"""Real SDK serialization and retry policy, with all HTTP intercepted in process.

Only synthetic keys, model names, prices and reserved .test domains are used.
No provider access or available quota is needed or inferred by these tests.
"""
from __future__ import annotations

import json
import importlib

import httpx2
import pytest

from tankai.core.llm import AnthropicLLM, BudgetedLLM, LLMCallBudget, OpenAILLM
from tankai.core.monetary_budget import MonetaryBudget, PilotPolicy
from tankai.core.request_dispatch import DispatchDenied, MonetaryDispatch
from tankai.core.request_pricing import TextTariff


NOW = 1_790_000_000


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
@pytest.mark.parametrize("outcome", ["success", "server-error", "timeout", "redirect", "malformed"])
def test_real_sdk_makes_exactly_one_reserved_http_attempt(tmp_path, monkeypatch, provider, outcome):
    endpoint = "https://reserved.example.test/v1" if provider == "openai" else "https://reserved.example.test"
    model = "offline-model-v1"
    sent = []
    path = tmp_path / "budget.sqlite"
    with MonetaryBudget.create(path, PilotPolicy("owner", NOW, NOW + 86_400), clock=lambda: NOW) as ledger:
        ledger.begin_job("job", owner_id="owner")
        tariff = TextTariff("offline-tariff", provider, model, NOW, NOW + 86_400,
                           "https://reserved.example.test/pricing", "EUR", "1", "1", "0",
                           "1", "0", "0", "0", None, True)
        dispatcher = MonetaryDispatch(ledger, owner_id="owner", job_id="job",
            tariffs={(provider, model): tariff}, endpoints={(provider, model): endpoint},
            input_token_bound=lambda request: 1000, clock=lambda: NOW)

        def intercept(transport, request):
            with MonetaryBudget(path, clock=lambda: NOW) as independent:
                sent.append((request, independent.snapshot()["held_micro_eur"]))
            if outcome == "timeout":
                raise httpx2.ReadTimeout("offline timeout", request=request)
            if outcome == "server-error":
                return httpx2.Response(500, json={"error": {"message": "offline failure"}}, request=request)
            if outcome == "redirect":
                return httpx2.Response(307, headers={"location": "https://redirect.example.test"}, request=request)
            if provider == "openai":
                body = {"id": "offline", "object": "chat.completion", "created": NOW,
                    "model": model, "choices": [] if outcome == "malformed" else [
                        {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "offline answer"}}]}
            else:
                body = {"id": "offline", "type": "message", "role": "assistant", "model": model,
                    "content": None if outcome == "malformed" else [{"type": "text", "text": "offline answer"}],
                    "stop_reason": "end_turn", "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 2}}
            return httpx2.Response(200, json=body, request=request)

        monkeypatch.setattr(httpx2.HTTPTransport, "handle_request", intercept)
        if provider == "openai":
            adapter = OpenAILLM(model=model, api_key="synthetic-offline-key", base_url=endpoint,
                                max_tokens=1000, max_retries=3)
        else:
            monkeypatch.setenv("ANTHROPIC_BASE_URL", endpoint)
            adapter = AnthropicLLM(model=model, api_key="synthetic-offline-key", max_tokens=1000, max_retries=3)
        try:
            wrapped = BudgetedLLM(adapter, LLMCallBudget(40, monetary_dispatch=dispatcher))
            if outcome == "success":
                assert wrapped.complete("offline task", system="offline system") == "offline answer"
            else:
                sdk = importlib.import_module(provider)
                expected_error = {
                    "server-error": sdk.InternalServerError,
                    "timeout": sdk.APITimeoutError,
                    "redirect": sdk.APIStatusError,
                    "malformed": RuntimeError,
                }[outcome]
                with pytest.raises(expected_error):
                    wrapped.complete("offline task", system="offline system")
                with pytest.raises(DispatchDenied, match="uncertain upstream"):
                    wrapped.complete("no automatic retry")
            assert len(sent) == 1
            request, held_before_http = sent[0]
            assert held_before_http == 2000
            data = json.loads(request.content)
            assert data["model"] == model and data["max_tokens"] == 1000
            assert request.url.host == "reserved.example.test"
            timeouts = request.extensions["timeout"]
            assert timeouts and all(0 < value <= 30 for value in timeouts.values())
            assert ledger.snapshot()["attempts"] == 1
            assert ledger.snapshot()["held_micro_eur"] == 2000
            assert ledger.snapshot()["unresolved_attempts"] == 1
        finally:
            adapter.client.close()
