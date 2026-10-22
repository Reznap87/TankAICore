"""Optional memory inference must not discard an already verified run."""
import json
from uuid import uuid4

import pytest

from tankai.core.llm import BaseLLM
from tankai.web.runtime import WorkspaceRuntimeManager


@pytest.mark.parametrize("passes", [True, False])
def test_memory_provider_failure_preserves_result_and_verification(tmp_path, monkeypatch, passes):
    class OfflinePipeline(BaseLLM):
        provider_name = "offline-test"
        model_name = "scripted-memory-failure"

        def complete(self, prompt, *, system="", **kwargs):
            if "memory-consolidator" in system.lower():
                raise RuntimeError("provider limited; private-marker-must-not-escape")
            if "planner" in system.lower():
                return json.dumps({"rationale": "direct calculation", "steps": [
                    {"description": "Calculate 31 times 13", "specialist_type": "analysis",
                     "expected_output": "403 with calculation"}
                ]})
            if "critic" in system.lower():
                return json.dumps({"passed": passes, "score": 1.0 if passes else 0.2,
                                   "issues": [] if passes else ["rejected"], "suggestions": []})
            return "31 * 13 = 31 * 10 + 31 * 3 = 310 + 93 = 403"

    monkeypatch.setattr("tankai.web.runtime.get_llm", OfflinePipeline)
    monkeypatch.setenv("TANKAI_CRITIC_LLM", "")
    monkeypatch.setenv("TANKAI_REQUIRE_INDEPENDENT_CRITIC", "0")
    monkeypatch.setenv("TANKAI_LLM_MAX_CALLS_PER_RUN", "20")
    monkeypatch.setenv("TANKAI_REQUIRE_RESEARCH_EVIDENCE", "1")
    monkeypatch.setenv("TANKAI_EMBEDDER", "hashing")
    manager = WorkspaceRuntimeManager(tmp_path)
    try:
        runtime = manager.get(tenant_id=str(uuid4()), workspace_id=str(uuid4()))
        result = runtime.tank.run("Berechne 31 mal 13 ohne Websuche")
        assert result.verification_passed is passes
        assert result.release_ready is passes
        assert result.status.value == ("completed" if passes else "failed")
        assert result.final_answer.endswith("403")
        deferred = next(r for r in result.receipts if r.action == "defer_memory_consolidation")
        assert deferred.success is False
        assert deferred.details == {"error_type": "RuntimeError", "episode_stored": True,
                                    "deferred": True, "retry_scheduled": False}
        assert "private-marker" not in result.model_dump_json()
        saved = runtime.tank.run_store.list_recent(1)[0]
        assert saved["final_answer"] == result.final_answer
        assert saved["verification_passed"] is passes
    finally:
        manager.close()
