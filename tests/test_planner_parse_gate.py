"""Malformed planner output cannot create or dispatch invented work."""
from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from tankai import TankAI
from tankai.agents.planner import Planner
from tankai.core.llm import BaseLLM
from tankai.core.models import Goal, Plan, PlanStep, TaskStatus


STEP = {"description": "Compute requested result", "specialist_type": "analysis",
        "expected_output": "Verified result"}
VALID = json.dumps({"rationale": "Direct work", "steps": [STEP]})
INVALID_CASES = [
    ("not JSON", "invalid_json"),
    ('{"steps": [', "invalid_json"),
    ('prefix ' + VALID, "invalid_json"),
    ("null", "invalid_root"),
    ("true", "invalid_root"),
    ("1", "invalid_root"),
    ("[]", "invalid_root"),
    ("[" + VALID + "]", "invalid_root"),
    ("{}", "invalid_steps"),
    ('{"steps":[]}', "invalid_steps"),
    ('{"steps":{}}', "invalid_steps"),
    ('{"steps":null}', "invalid_steps"),
    ('{"steps":[null]}', "invalid_step"),
    (json.dumps({"steps": [STEP, {}]}), "invalid_step"),
    (json.dumps({"steps": [dict(STEP, description=7)]}), "invalid_step"),
    (json.dumps({"steps": [dict(STEP, description=" ")]}), "invalid_step"),
    (json.dumps({"steps": [dict(STEP, description="a" * 2001)]}), "invalid_step"),
    (json.dumps({"steps": [dict(STEP, expected_output=[])]}), "invalid_step"),
    (json.dumps({"steps": [dict(STEP, expected_output="")]}), "invalid_step"),
    (json.dumps({"steps": [dict(STEP, specialist_type="shell")]}), "invalid_step"),
    (json.dumps({"steps": [dict(STEP, specialist_type=None)]}), "invalid_step"),
    (json.dumps({"steps": [STEP] * 6}), "step_limit_exceeded"),
]


class ScriptedLLM(BaseLLM):
    provider_name = "local-test"
    model_name = "plan-parse-gate"

    def __init__(self, plans: list[str], *, accept_plan: bool = True,
                 simulation: bool = False) -> None:
        self.plans = plans
        self.accept_plan = accept_plan
        self.calls: list[str] = []
        self.plan_prompts: list[str] = []
        self.is_simulation = simulation

    def complete(self, prompt: str, *, system: str = "", **kwargs) -> str:
        if "planner" in system.lower():
            self.calls.append("plan")
            self.plan_prompts.append(prompt)
            return self.plans[min(len(self.plan_prompts) - 1, len(self.plans) - 1)]
        if "Prüfe den folgenden Plan" in prompt:
            self.calls.append("plan_check")
            return json.dumps({"passed": self.accept_plan, "score": 1.0,
                               "issues": [] if self.accept_plan else ["Needs repair"],
                               "suggestions": []})
        if "critic" in system.lower():
            self.calls.append("result_check")
            return '{"passed":true,"score":1.0,"issues":[],"suggestions":[]}'
        self.calls.append("execution")
        return "Verified answer"


@pytest.mark.parametrize("raw,reason", INVALID_CASES)
def test_invalid_plan_has_explicit_failed_receipt_and_no_replacement_steps(raw, reason):
    llm = ScriptedLLM([raw])
    plan, receipt = Planner(llm).run(Goal(description="A specific goal", definition_of_done="Done"))
    assert plan.steps == []
    assert plan.parse_fallback is True and plan.parse_error == reason
    assert not receipt.success
    assert receipt.details["parse_fallback"] is True
    assert receipt.details["parse_error"] == reason
    assert receipt.details["executable_plan"] is False
    assert not receipt.details["reused_pattern"]
    assert llm.calls == ["plan"]


@pytest.mark.parametrize("raw", [VALID, "```json\n" + VALID + "\n```"])
def test_valid_plan_without_optional_goal_analysis_is_executable(raw):
    plan, receipt = Planner(ScriptedLLM([raw])).run(
        Goal(description="Compute", definition_of_done="Checked"))
    assert not plan.parse_fallback and plan.parse_error is None
    assert len(plan.steps) == 1 and receipt.success
    assert plan.goal_analysis.analysis_status == "fallback"
    assert receipt.details["executable_plan"] is True


@pytest.mark.parametrize("raw,reason", INVALID_CASES)
@pytest.mark.parametrize("parallel", [False, True])
def test_invalid_plan_cannot_be_approved_or_dispatched(raw, reason, parallel):
    llm = ScriptedLLM([raw])
    tank = TankAI(llm=llm, max_retries=0, parallel=parallel, verbose=False,
                  require_research_evidence=False, enable_tools=False, run_store_path=None)

    def forbidden(*args, **kwargs):
        pytest.fail("Malformed plans must stop before critic inference or execution")

    tank.critic.critique_plan = forbidden
    tank._execute_and_critique = forbidden
    tank._run_steps_parallel = forbidden
    tank.synthesizer.run = forbidden
    result = tank.run("Specific task with no generic replacement")
    assert llm.calls == ["plan"]
    assert result.status == TaskStatus.FAILED
    assert not result.plan_gate_passed and not result.release_ready and not result.verification_passed
    assert result.plan.steps == [] and result.plan.parse_error == reason
    assert result.memory_entries_created == 0
    assert result.failed_step_ids == []
    assert "nicht ausgeführt" in result.final_answer
    gate = next(r for r in result.receipts if r.action == "critique_plan")
    assert gate.details["deterministic_plan_structure_gate"] is True
    assert not gate.details["llm_called"] and not gate.success
    rejection = next(r for r in result.receipts if r.action == "reject_plan")
    assert rejection.details["reason"] == "plan_parse_failed"
    assert rejection.details["parse_fallback"] is True
    assert rejection.details["parse_error"] == reason
    assert next(r.details["used"] for r in result.receipts if r.action == "llm_call_budget") == 1


def test_parse_repairs_share_existing_bounded_budget_and_are_persisted(tmp_path):
    llm = ScriptedLLM(["bad JSON"])
    tank = TankAI(llm=llm, max_retries=2, verbose=False, require_research_evidence=False,
                  enable_tools=False, run_store_path=str(tmp_path / "runs.jsonl"), use_ltm=True,
                  ltm_db=str(tmp_path / "ltm.db"), ltm_vectors=str(tmp_path / "vectors.npz"))
    result = tank.run("Repair bounded plan")
    assert llm.calls == ["plan"] * 3
    assert result.plan.version == 3 and result.status == TaskStatus.FAILED
    assert len([r for r in result.receipts if r.action == "create_plan"]) == 3
    assert [r.details["version"] for r in result.receipts if r.action == "create_plan"] == [1, 2, 3]
    assert "invalid_json" in llm.plan_prompts[-1]
    assert tank.llm_call_budget.snapshot()["used"] == 3
    saved = tank.run_store.list_recent(1)[0]
    assert saved["plan_steps"] == 0 and not saved["plan_gate_passed"]
    receipts = tank.ltm._conn.execute(
        "SELECT details FROM receipts WHERE run_id=? AND action='create_plan'", (result.goal_id,)
    ).fetchall()
    assert len(receipts) == 3
    assert all(json.loads(r["details"])["parse_fallback"] is True for r in receipts)
    assert all(json.loads(r["details"])["parse_error"] == "invalid_json" for r in receipts)


def test_valid_repair_can_execute_without_conflating_optional_analysis_fallback():
    llm = ScriptedLLM(["bad JSON", VALID])
    tank = TankAI(llm=llm, max_retries=1, verbose=False, require_research_evidence=False,
                  enable_tools=False, run_store_path=None)
    result = tank.run("A valid repaired plan")
    assert llm.calls[:3] == ["plan", "plan", "plan_check"]
    assert result.plan.version == 2
    assert result.release_ready and result.plan_gate_passed and result.verification_passed
    assert result.plan.goal_analysis.analysis_status == "fallback"
    assert not result.plan.parse_fallback
    receipts = [r for r in result.receipts if r.action == "create_plan"]
    assert [r.success for r in receipts] == [False, True]
    assert not any(r.action == "reject_plan" for r in result.receipts)


def test_malformed_repair_after_critic_rejection_is_blocked():
    llm = ScriptedLLM([VALID, '{"steps":[]}'], accept_plan=False)
    tank = TankAI(llm=llm, max_retries=1, verbose=False, require_research_evidence=False,
                  enable_tools=False, run_store_path=None)
    result = tank.run("Critic repair produced an invalid plan")
    assert llm.calls == ["plan", "plan_check", "plan"]
    assert not result.plan_gate_passed and not result.release_ready
    assert result.plan.parse_fallback and result.plan.parse_error == "invalid_steps"


def test_invalid_single_step_plan_does_not_spend_remaining_calls():
    llm = ScriptedLLM(["bad JSON"])
    tank = TankAI(llm=llm, max_retries=0, verified_single_step=True, verbose=False,
                  require_research_evidence=False, enable_tools=False, run_store_path=None)
    result = tank.run("No valid single step")
    assert llm.calls == ["plan"]
    assert not result.release_ready and not result.plan_gate_passed


@pytest.mark.parametrize("simulation", [False, True])
def test_parse_failure_preserves_simulation_status_without_success(simulation):
    llm = ScriptedLLM(["null"], simulation=simulation)
    tank = TankAI(llm=llm, max_retries=0, verbose=False, require_research_evidence=False,
                  enable_tools=False, run_store_path=None)
    result = tank.run("No invented plan")
    assert result.status == (TaskStatus.SIMULATED if simulation else TaskStatus.FAILED)
    assert not result.release_ready and not result.verification_passed


def test_parse_failure_model_cannot_contain_executable_steps():
    with pytest.raises(ValidationError):
        Plan(goal_id="goal", steps=[PlanStep(**STEP)], rationale="Invalid",
             parse_fallback=True, parse_error="invalid_json")
    with pytest.raises(ValidationError):
        Plan(goal_id="goal", steps=[], rationale="Invalid", parse_fallback=True)
