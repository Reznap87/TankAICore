"""A rejected plan must not dispatch work or spend optional inference."""
from __future__ import annotations

import json

import pytest

from tankai import TankAI
from tankai.core.llm import BaseLLM
from tankai.core.models import TaskStatus


class PlanAdmissionLLM(BaseLLM):
    provider_name = "plan-admission-test"
    model_name = "scripted"

    def __init__(self, *, accept_after: int | None = None, simulation: bool = False):
        self.accept_after = accept_after
        self.is_simulation = simulation
        self.calls: list[str] = []
        self.plan_checks = 0

    def complete(self, prompt, *, system="", **kwargs):
        if "planner" in system.lower():
            self.calls.append("plan")
            steps = [
                {"description": "Analyse", "specialist_type": "analysis", "expected_output": "analysis"},
                {"description": "Write", "specialist_type": "writing", "expected_output": "answer"},
            ]
            if "Maximale Anzahl Planschritte: 1" in prompt:
                steps = steps[:1]
            return json.dumps({"rationale": "Bounded steps", "steps": steps})
        if "Prüfe den folgenden Plan" in prompt:
            self.calls.append("plan_check")
            self.plan_checks += 1
            accepted = self.accept_after is not None and self.plan_checks >= self.accept_after
            return json.dumps({"passed": accepted, "score": 1.0 if accepted else 0.1,
                               "issues": [] if accepted else ["Belege fehlen"],
                               "suggestions": [] if accepted else ["Belege einplanen"]})
        self.calls.append("execution")
        if "critic" in system.lower():
            return '{"passed":true,"score":1.0,"issues":[],"suggestions":[]}'
        return "Executed answer"


@pytest.mark.parametrize("parallel", [False, True])
@pytest.mark.parametrize("simulation", [False, True])
def test_final_plan_rejection_stops_before_dispatch_and_preserves_record(tmp_path, parallel, simulation):
    llm = PlanAdmissionLLM(simulation=simulation)
    tank = TankAI(llm=llm, parallel=parallel, max_retries=1, verbose=False,
                  require_research_evidence=False, enable_tools=False, use_ltm=True,
                  ltm_db=str(tmp_path / "ltm.db"), ltm_vectors=str(tmp_path / "vectors.npz"),
                  run_store_path=str(tmp_path / "runs.jsonl"))

    def forbidden(*args, **kwargs):
        pytest.fail("Rejected plans must not dispatch work or consolidate knowledge")

    tank._run_steps_parallel = forbidden
    tank._execute_and_critique = forbidden
    tank.synthesizer.run = forbidden
    tank.ltm.consolidate = forbidden
    tank.ltm.promote_to_procedure = forbidden
    result = tank.run("Rejected plan with no authorised work")

    assert llm.calls == ["plan", "plan_check", "plan", "plan_check"]
    assert result.status == (TaskStatus.SIMULATED if simulation else TaskStatus.FAILED)
    assert tank.current_goal.status == result.status
    assert result.plan.version == 2
    assert all(step.status == TaskStatus.PENDING and step.result is None for step in result.plan.steps)
    assert not result.plan_gate_passed and not result.verification_passed and not result.release_ready
    assert result.source_ids == result.source_urls == result.failed_step_ids == []
    assert result.memory_entries_created == 0
    assert "nicht ausgeführt" in result.final_answer
    rejection = next(r for r in result.receipts if r.action == "reject_plan")
    assert not rejection.success
    assert rejection.details["skipped_step_ids"] == [s.id for s in result.plan.steps]
    assert rejection.details["plan_version"] == 2
    assert rejection.details["plan_checks"] == 2
    assert rejection.details["issues"] == ["Belege fehlen"]
    assert rejection.details["suggestions"] == ["Belege einplanen"]
    assert "Belege fehlen" in result.final_answer
    budget = next(r.details for r in result.receipts if r.action == "llm_call_budget")
    assert budget["used"] == 4
    usage = next(r.details for r in result.receipts if r.action == "llm_usage")
    assert usage["adapter_attempts"] == 4
    assert {r.action for r in result.receipts}.isdisjoint({"synthesize", "critique_result"})
    saved = tank.run_store.list_recent(1)[0]
    assert saved["goal_id"] == result.goal_id and saved["release_ready"] is False
    episode = tank.ltm._conn.execute("SELECT metadata, status FROM runs WHERE id=?", (result.goal_id,)).fetchone()
    assert json.loads(episode["metadata"])["verification_passed"] is False
    assert episode["status"] == result.status.value
    persisted_rejection = tank.ltm._conn.execute(
        "SELECT details FROM receipts WHERE run_id=? AND action='reject_plan'", (result.goal_id,)
    ).fetchone()
    assert json.loads(persisted_rejection["details"])["issues"] == ["Belege fehlen"]
    entries = tank.ltm._conn.execute("SELECT memory_type, validity FROM memory_entries").fetchall()
    assert [(e["memory_type"], e["validity"]) for e in entries] == [("episodic", "unknown")]
    assert tank.ltm._conn.execute("SELECT count(*) FROM consolidation_jobs").fetchone()[0] == 0


def test_repaired_plan_can_execute_and_later_runs_reset_budget(tmp_path):
    llm = PlanAdmissionLLM(accept_after=2)
    tank = TankAI(llm=llm, max_retries=1, verbose=False, require_research_evidence=False,
                  enable_tools=False, run_store_path=None)
    first = tank.run("Plan repaired before dispatch")
    assert first.release_ready and first.plan_gate_passed
    assert first.plan.version == 2
    assert "execution" in llm.calls
    assert not any(r.action == "reject_plan" for r in first.receipts)
    llm.accept_after = None
    llm.calls.clear()
    second = tank.run("Next plan rejected")
    assert not second.release_ready and not second.plan_gate_passed
    assert llm.calls == ["plan", "plan_check", "plan", "plan_check"]
    assert tank.llm_call_budget.snapshot()["used"] == 4


def test_single_step_rejection_stops_with_two_calls():
    llm = PlanAdmissionLLM()
    tank = TankAI(llm=llm, max_retries=0, verified_single_step=True, verbose=False,
                  require_research_evidence=False, enable_tools=False, run_store_path=None)
    result = tank.run("Rejected single step")
    assert llm.calls == ["plan", "plan_check"]
    assert result.status == TaskStatus.FAILED and not result.release_ready
    assert tank.llm_call_budget.snapshot()["used"] == 2
