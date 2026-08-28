from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from tankai import TankAI
from tankai.core.llm import BaseLLM, BudgetedLLM, LLMCallBudget


class CountingLLM(BaseLLM):
    provider_name = "counting"
    model_name = "counting-model"
    is_simulation = True

    def __init__(self) -> None:
        self.calls = 0
        self._lock = threading.Lock()

    def complete(self, prompt: str, *, system: str = "", **kwargs) -> str:
        with self._lock:
            self.calls += 1
        return "ok"


class ScriptedLLM(BaseLLM):
    provider_name = "scripted"
    model_name = "scripted-model"
    is_simulation = True

    def complete(self, prompt: str, *, system: str = "", **kwargs) -> str:
        system_lower = system.lower()
        prompt_lower = prompt.lower()
        if "planner" in system_lower or "erstelle einen plan" in prompt_lower:
            return (
                '{"rationale":"bounded","reused_pattern":false,"steps":['
                '{"description":"Analyse ausführen","specialist_type":"analysis",'
                '"expected_output":"klare Antwort"}]}'
            )
        if "critic" in system_lower:
            return '{"passed":true,"score":1.0,"issues":[],"suggestions":[]}'
        if "synthesizer" in system_lower:
            return "final answer"
        return "step result"


def _budget_receipt(result):
    return next(receipt for receipt in result.receipts if receipt.action == "llm_call_budget")


def test_budget_blocks_before_delegate_call() -> None:
    budget = LLMCallBudget(2)
    delegate = CountingLLM()
    llm = BudgetedLLM(delegate, budget)

    assert llm.complete("one") == "ok"
    assert llm.complete("two") == "ok"
    with pytest.raises(RuntimeError, match="LLM-Call-Budget erschöpft"):
        llm.complete("blocked")

    assert delegate.calls == 2
    assert budget.snapshot() == {"used": 2, "max": 2, "remaining": 0}


def test_budget_has_hard_core_ceiling_of_40() -> None:
    with pytest.raises(ValueError, match="zwischen 1 und 40"):
        LLMCallBudget(41)


def test_shared_budget_is_thread_safe() -> None:
    budget = LLMCallBudget(8)
    delegate = CountingLLM()
    llm = BudgetedLLM(delegate, budget)

    def invoke(index: int) -> bool:
        try:
            llm.complete(str(index))
            return True
        except RuntimeError:
            return False

    with ThreadPoolExecutor(max_workers=16) as pool:
        outcomes = list(pool.map(invoke, range(24)))

    assert sum(outcomes) == 8
    assert delegate.calls == 8
    assert budget.snapshot() == {"used": 8, "max": 8, "remaining": 0}


def test_tank_run_resets_shared_main_critic_budget_and_receipts_usage() -> None:
    tank = TankAI(
        llm=ScriptedLLM(),
        max_llm_calls_per_run=7,
        require_research_evidence=False,
        enable_tools=False,
        verbose=False,
        run_store_path=None,
    )

    assert tank.llm is tank.critic_llm
    first = tank.run("Ersten begrenzten Run ausführen")
    first_budget = _budget_receipt(first)
    assert first_budget.details == {
        "used": 6,
        "max": 7,
        "remaining": 1,
        "shared_main_and_critic": True,
        "reset_scope": "run",
    }

    assert tank.llm.complete("außerhalb des Runs") == "step result"
    assert tank.llm_call_budget.snapshot()["used"] == 7

    second = tank.run("Zweiten begrenzten Run ausführen")
    second_budget = _budget_receipt(second)
    assert second_budget.details["used"] == 6
    assert second_budget.details["max"] == 7
    assert second_budget.details["remaining"] == 1
