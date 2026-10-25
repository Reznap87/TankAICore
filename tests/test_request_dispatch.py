"""Actual Core/adapter code with in-process fake SDKs; never network or real keys."""
from __future__ import annotations

from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
import json
import sys
from types import SimpleNamespace

import pytest

from tankai import TankAI
from tankai.core.llm import AnthropicLLM, BaseLLM, BudgetedLLM, LLMCallBudget, OpenAILLM
from tankai.core.monetary_budget import BudgetDenied, MonetaryBudget, PilotPolicy
from tankai.core.request_dispatch import DispatchDenied, FrozenTextRequest, MonetaryDispatch
from tankai.core.request_pricing import PricingError, TextTariff


NOW = 1_790_000_000
OWNER = "owner-test"
ENDPOINTS = {("openai", "main-v1"): "https://openai.example.test/v1",
             ("anthropic", "critic-v1"): "https://anthropic.example.test"}


def tariff(provider="openai", model="main-v1", **changes):
    return replace(TextTariff(
        "offline-tariff-v1", provider, model, NOW, NOW + 86_400,
        "https://example.test/pricing", "EUR", "1", "1", "0", "1", "0", "0", "0", None, True,
    ), **changes)


@pytest.fixture
def ledger(tmp_path):
    with MonetaryBudget.create(tmp_path / "budget.sqlite", PilotPolicy(OWNER, NOW, NOW + 86_400),
                               clock=lambda: NOW) as result:
        result.begin_job("job", owner_id=OWNER)
        yield result


def guard(ledger, *, counter=None, tariffs=None, endpoints=None, **changes):
    return MonetaryDispatch(
        ledger, owner_id=OWNER, job_id="job",
        tariffs=tariffs or {key: tariff(*key) for key in ENDPOINTS},
        endpoints=endpoints or ENDPOINTS,
        input_token_bound=counter if counter is not None else lambda request: 1000,
        clock=lambda: NOW, **changes)


def answer(parameters):
    messages = parameters["messages"]
    system = parameters.get("system", next((m["content"] for m in messages if m["role"] == "system"), ""))
    prompt = messages[-1]["content"]
    if "planner" in system.lower() or "erstelle einen plan" in prompt.lower():
        return json.dumps({"rationale": "offline bounded fixture", "reused_pattern": False,
                           "steps": [{"description": "Analyse ausführen", "specialist_type": "analysis",
                                      "expected_output": "klare Antwort"}]})
    if "critic" in system.lower():
        return '{"passed":true,"score":1.0,"issues":[],"suggestions":[]}'
    return "step result"


@pytest.fixture
def sdks(monkeypatch, ledger):
    roots = []

    class Transport:
        def __init__(self, **kwargs):
            self.follow_redirects = kwargs["follow_redirects"]
            assert kwargs["trust_env"] is False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    class Client:
        def __init__(self, *, provider="openai", root=None, **kwargs):
            self.provider = provider
            self.root = root or self
            self.base_url = kwargs.get("base_url") or ENDPOINTS[(provider, "main-v1" if provider == "openai" else "critic-v1")]
            self.max_retries = kwargs.get("max_retries", 1)
            self._client = kwargs.get("http_client")
            if root is None:
                self.calls, self.options, self.exposures = [], [], []
                self.error, self.ignore_retry_override, self.malformed = None, False, False
                roots.append(self)
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))
            self.messages = SimpleNamespace(create=self.create)

        def with_options(self, **kwargs):
            self.root.options.append({k: v for k, v in kwargs.items() if k != "http_client"})
            return Client(provider=self.provider, root=self.root, base_url=self.base_url,
                          http_client=kwargs["http_client"],
                          max_retries=self.max_retries if self.root.ignore_retry_override else kwargs["max_retries"])

        def create(self, **parameters):
            # A separate DB connection must see the committed reservation before
            # the fake outgoing SDK call can happen.
            with MonetaryBudget(ledger.path, clock=lambda: NOW) as independent:
                self.root.exposures.append(independent.snapshot()["held_micro_eur"])
            self.root.calls.append(parameters)
            if self.root.error:
                raise self.root.error
            if self.root.malformed:
                return SimpleNamespace(choices=[])
            text = answer(parameters)
            if self.provider == "openai":
                return SimpleNamespace(model=parameters["model"],
                    choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
                    usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20))
            return SimpleNamespace(model=parameters["model"], content=[SimpleNamespace(text=text)],
                                   usage=SimpleNamespace(input_tokens=100, output_tokens=20))

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(
        OpenAI=lambda **kw: Client(**kw), DefaultHttpxClient=Transport))
    monkeypatch.setitem(sys.modules, "anthropic", SimpleNamespace(
        Anthropic=lambda **kw: Client(provider="anthropic", **kw), DefaultHttpxClient=Transport))
    return roots


def adapters():
    return (OpenAILLM(model="main-v1", api_key="synthetic-offline-key", max_tokens=1000, max_retries=3),
            AnthropicLLM(model="critic-v1", api_key="synthetic-offline-key", max_tokens=1000, max_retries=3))


def wrapped(ledger, sdks, *, dispatcher=None):
    main, critic = adapters()
    budget = LLMCallBudget(40, monetary_dispatch=dispatcher or guard(ledger))
    return BudgetedLLM(main, budget), BudgetedLLM(critic, budget), budget


def test_both_actual_adapter_paths_commit_before_sdk_and_force_zero_internal_retries(ledger, sdks):
    main, critic, _ = wrapped(ledger, sdks)
    assert main.complete("task", system="main system") == "step result"
    assert critic.complete("task", system="review system") == "step result"
    assert ledger.snapshot()["attempts"] == 2
    assert ledger.snapshot()["unresolved_attempts"] == 2
    for sdk in sdks:
        assert sdk.options == [{"max_retries": 0, "timeout": 30.0}]
        assert len(sdk.calls) == 1 and sdk.exposures[0] > 0


def test_counter_receives_complete_final_parameters_including_default_system(ledger, sdks):
    seen = []
    def count(request):
        assert type(request) is FrozenTextRequest
        seen.append(request.parameters())
        assert "private task" not in repr(request)
        return 1000
    main, critic, _ = wrapped(ledger, sdks, dispatcher=guard(ledger, counter=count))
    main.complete("private task", system="operator system")
    critic.complete("another task")
    assert seen[0]["messages"][0] == {"role": "system", "content": "operator system"}
    assert seen[1]["system"] == "Du bist ein präziser, hilfreicher Assistent."
    assert seen[0] == sdks[0].calls[0]
    assert seen[1] == sdks[1].calls[0]


def test_counter_cannot_mutate_parameters_later_sent_to_sdk(ledger, sdks):
    def count(request):
        mutable_copy = request.parameters()
        mutable_copy["messages"][0]["content"] = "tampered"
        mutable_copy["max_tokens"] = 999_999
        return 1000
    main, _, _ = wrapped(ledger, sdks, dispatcher=guard(ledger, counter=count))
    main.complete("original")
    assert sdks[0].calls[0]["messages"][0]["content"] == "original"
    assert sdks[0].calls[0]["max_tokens"] == 1000


@pytest.mark.parametrize("count", [None, True, -1, 0, 12_001, 1.5])
def test_missing_or_excessive_complete_input_bound_prevents_every_sdk_send(ledger, sdks, count):
    main, _, _ = wrapped(ledger, sdks, dispatcher=guard(ledger, counter=lambda request: count))
    with pytest.raises(PricingError):
        main.complete("blocked")
    assert not sdks[0].calls
    assert ledger.snapshot()["attempts"] == 0


@pytest.mark.parametrize("tokens", [True, None, 0, 2049, 1.5, "1000"])
def test_per_call_output_override_cannot_bypass_pilot_limit(ledger, sdks, tokens):
    main, _, _ = wrapped(ledger, sdks)
    with pytest.raises(DispatchDenied):
        main.complete("blocked", max_tokens=tokens)
    assert not sdks[0].calls and ledger.snapshot()["attempts"] == 0


def test_main_and_critic_share_money_and_reset_does_not_refund_or_reopen_job(ledger, sdks):
    tariffs = {key: tariff(*key, input_per_million="120", output_per_million="0") for key in ENDPOINTS}
    main, critic, budget = wrapped(ledger, sdks, dispatcher=guard(ledger, tariffs=tariffs))
    main.complete("first")
    budget.reset()
    with pytest.raises(BudgetDenied, match="job_budget"):
        critic.complete("second")
    assert len(sdks[0].calls) == 1 and not sdks[1].calls
    assert ledger.snapshot()["held_micro_eur"] == 120_000


def test_sixth_actual_send_is_blocked_even_when_legacy_call_limit_is_larger(ledger, sdks):
    main, _, budget = wrapped(ledger, sdks)
    for index in range(5):
        main.complete(str(index))
        budget.reset()
    with pytest.raises(BudgetDenied, match="attempt_limit"):
        main.complete("sixth")
    assert len(sdks[0].calls) == 5 and ledger.snapshot()["attempts"] == 5


@pytest.mark.parametrize("error", [TimeoutError("fixture timeout"), RuntimeError("fixture failure"), KeyboardInterrupt()])
def test_failure_and_cancellation_keep_full_reservation_without_retry(ledger, sdks, error):
    main, _, _ = wrapped(ledger, sdks)
    sdks[0].error = error
    with pytest.raises(type(error)):
        main.complete("request")
    assert len(sdks[0].calls) == 1
    assert ledger.snapshot()["held_micro_eur"] == 2000
    assert ledger.snapshot()["unresolved_attempts"] == 1


def test_malformed_response_keeps_reserved_cost(ledger, sdks):
    main, _, _ = wrapped(ledger, sdks)
    sdks[0].malformed = True
    with pytest.raises(RuntimeError, match="choices"):
        main.complete("request")
    assert ledger.snapshot()["held_micro_eur"] == 2000


@pytest.mark.parametrize("malformed", [False, True])
def test_uncertain_outcome_blocks_main_critic_and_reset_in_same_job_context(ledger, sdks, malformed):
    dispatcher = guard(ledger)
    main, critic, budget = wrapped(ledger, sdks, dispatcher=dispatcher)
    if malformed:
        sdks[0].malformed = True
    else:
        sdks[0].error = TimeoutError("unknown upstream result")
    with pytest.raises((TimeoutError, RuntimeError)):
        main.complete("first attempt")
    assert dispatcher.uncertain_upstream
    sdks[0].malformed, sdks[0].error = False, None
    budget.reset()
    for adapter in (main, critic):
        with pytest.raises(DispatchDenied, match="uncertain upstream"):
            adapter.complete("must not retry or fall back")
    assert sum(len(sdk.calls) for sdk in sdks) == 1
    assert ledger.snapshot()["unresolved_attempts"] == 1
    assert ledger.snapshot()["held_micro_eur"] == 2000


def test_queued_siblings_do_not_start_after_first_uncertain_failure(ledger, sdks):
    main, _, _ = wrapped(ledger, sdks)
    sdks[0].error = TimeoutError("unknown upstream result")
    def invoke(index):
        try:
            main.complete(str(index))
        except (TimeoutError, DispatchDenied):
            return
        pytest.fail("unexpected success")
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(invoke, range(12)))
    assert len(sdks[0].calls) == 1
    assert ledger.snapshot()["attempts"] == 1


def test_successful_send_is_not_refunded_when_ledger_is_reopened(ledger, sdks):
    main, _, _ = wrapped(ledger, sdks)
    main.complete("first")
    with MonetaryBudget(ledger.path, clock=lambda: NOW) as reopened:
        assert reopened.snapshot()["held_micro_eur"] == 2000
        assert reopened.snapshot()["unresolved_attempts"] == 1


def test_client_ignoring_retry_override_fails_closed_before_reservation_or_send(ledger, sdks):
    main, _, _ = wrapped(ledger, sdks)
    sdks[0].ignore_retry_override = True
    with pytest.raises(DispatchDenied, match="retries"):
        main.complete("blocked")
    assert not sdks[0].calls and ledger.snapshot()["attempts"] == 0


def test_unapproved_endpoint_prevents_dispatch_even_with_same_model_and_price(ledger, sdks):
    main, _, _ = wrapped(ledger, sdks)
    sdks[0].base_url = "https://different.example.test/v1"
    with pytest.raises(DispatchDenied, match="endpoint"):
        main.complete("blocked")
    assert not sdks[0].calls and ledger.snapshot()["attempts"] == 0


def test_missing_model_tariff_cannot_fall_back_to_another_model(ledger, sdks):
    main, _, _ = wrapped(ledger, sdks)
    main.delegate.model = "unpriced-model"
    with pytest.raises(DispatchDenied):
        main.complete("blocked")
    assert not sdks[0].calls and ledger.snapshot()["attempts"] == 0


def test_closed_job_prevents_another_send(ledger, sdks):
    main, _, _ = wrapped(ledger, sdks)
    ledger.close_job("job", owner_id=OWNER)
    with pytest.raises(BudgetDenied, match="active_job"):
        main.complete("blocked")
    assert not sdks[0].calls


def test_unsupported_delegate_cannot_silently_ignore_admission(ledger):
    class Unreviewed(BaseLLM):
        calls = 0
        def complete(self, prompt, **kwargs):
            self.calls += 1
            return "should not happen"
    delegate = Unreviewed()
    wrapper = BudgetedLLM(delegate, LLMCallBudget(40, monetary_dispatch=guard(ledger)))
    with pytest.raises(DispatchDenied, match="delegate"):
        wrapper.complete("blocked")
    assert delegate.calls == 0


def test_caller_cannot_replace_internal_admission_context(ledger, sdks):
    main, _, _ = wrapped(ledger, sdks)
    with pytest.raises(DispatchDenied):
        main.complete("blocked", _budget_dispatch=None)
    assert not sdks[0].calls


def test_counter_failure_sends_nothing_and_reserves_nothing(ledger, sdks):
    def broken(request):
        raise ValueError("fixture counter failed")
    main, _, _ = wrapped(ledger, sdks, dispatcher=guard(ledger, counter=broken))
    with pytest.raises(ValueError):
        main.complete("blocked")
    assert not sdks[0].calls and ledger.snapshot()["attempts"] == 0


@pytest.mark.parametrize("provider_index", [0, 1])
def test_raw_protected_adapter_cannot_bypass_its_budget_wrapper(ledger, sdks, provider_index):
    wrappers = wrapped(ledger, sdks)
    with pytest.raises(DispatchDenied, match="requires request admission"):
        wrappers[provider_index].delegate.complete("bypass attempt")
    assert not any(sdk.calls for sdk in sdks)


def test_parallel_actual_adapter_invocations_cannot_exceed_five_dispatches(ledger, sdks):
    main, _, _ = wrapped(ledger, sdks)
    def invoke(index):
        try:
            main.complete(str(index))
            return True
        except BudgetDenied:
            return False
    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(invoke, range(12)))
    assert sum(outcomes) == 5
    assert len(sdks[0].calls) == 5
    assert ledger.snapshot()["attempts"] == 5
    assert ledger.snapshot()["held_micro_eur"] == 10_000


def test_duplicate_admission_id_never_resends_even_when_prior_result_was_successful(ledger):
    dispatcher = guard(ledger)
    sends = []
    kwargs = dict(attempt_id="one-attempt", provider="openai", model="main-v1",
        endpoint=ENDPOINTS[("openai", "main-v1")], sdk_max_retries=0, timeout_seconds=30,
        parameters={"model":"main-v1", "messages":[{"role":"user", "content":"task"}],
                    "max_tokens":1000, "temperature":0.2},
        send_once=lambda parameters: sends.append(parameters) or "answer")
    assert dispatcher.execute(**kwargs) == "answer"
    with pytest.raises(DispatchDenied, match="already admitted"):
        dispatcher.execute(**kwargs)
    assert len(sends) == 1
    assert ledger.snapshot()["held_micro_eur"] == 2000


@pytest.mark.parametrize("content", ["a" * 128_001, "ä" * 65_000, {"image": "unsupported"}],
                         ids=["oversize-ascii", "oversize-utf8", "non-text"])
def test_non_text_or_oversized_request_is_rejected_before_counter_and_sdk(ledger, sdks, content):
    counted = []
    def count(request):
        counted.append(request)
        return 1000
    main, _, _ = wrapped(ledger, sdks, dispatcher=guard(ledger, counter=count))
    with pytest.raises(DispatchDenied):
        main.complete(content)
    assert not counted and not sdks[0].calls
    assert ledger.snapshot()["attempts"] == 0


def test_protected_request_clamps_sdk_timeout_to_thirty_seconds(ledger, sdks):
    main, _, _ = wrapped(ledger, sdks)
    main.delegate.timeout_seconds = 120.0
    main.complete("request")
    assert sdks[0].options[0] == {"max_retries":0, "timeout":30.0}


def test_complete_existing_core_pipeline_uses_one_shared_guard_for_five_sdk_attempts(ledger, sdks):
    main, critic = adapters()
    tank = TankAI(llm=main, critic_llm=critic, monetary_dispatch=guard(ledger),
                  require_independent_critic=True, max_retries=0, max_llm_calls_per_run=5,
                  require_research_evidence=False, enable_tools=False, verbose=False,
                  run_store_path=None, verified_single_step=True)
    try:
        result = tank.run("Führe eine begrenzte Offline-Prüfung aus")
        assert result.final_answer == "step result"
        assert sum(len(sdk.calls) for sdk in sdks) == 5
        assert ledger.snapshot()["attempts"] == 5
        assert ledger.snapshot()["unresolved_attempts"] == 5
        assert all(option["max_retries"] == 0 for sdk in sdks for option in sdk.options)
    finally:
        tank.memory.close()
