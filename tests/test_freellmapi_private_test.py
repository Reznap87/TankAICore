import importlib.util
import json
from pathlib import Path
import re
from uuid import uuid4

import pytest


ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location("freellmapi_test", ROOT / "scripts/freellmapi_test.py")
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


def test_private_compose_budget_allows_workspace_startup(tmp_path, monkeypatch):
    from tankai.web.runtime import WorkspaceRuntimeManager

    compose = (ROOT / "docker-compose.freellmapi.yml").read_text(encoding="utf-8")
    budget = re.search(r'^\s+TANKAI_LLM_MAX_CALLS_PER_RUN: "(\d+)"\s*$', compose, re.MULTILINE)
    assert budget is not None
    monkeypatch.setenv("TANKAI_LLM_MAX_CALLS_PER_RUN", budget.group(1))
    # Exercise the real workspace startup with the shipped limit, without any
    # network access, provider credentials or inference.
    monkeypatch.setenv("TANKAI_LLM", "mock")
    monkeypatch.setenv("TANKAI_CRITIC_LLM", "")
    monkeypatch.setenv("TANKAI_EMBEDDER", "hashing")
    monkeypatch.setenv("TANKAI_REQUIRE_INDEPENDENT_CRITIC", "0")
    manager = WorkspaceRuntimeManager(tmp_path)
    try:
        runtime = manager.get(tenant_id=str(uuid4()), workspace_id=str(uuid4()))
        assert runtime.root.is_dir()
        assert runtime.tank.llm.is_simulation
    finally:
        manager.close()


def test_private_compose_budget_completes_five_steps_and_memory(tmp_path, monkeypatch):
    from tankai.core.llm import BaseLLM
    from tankai.web.runtime import WorkspaceRuntimeManager

    class OfflinePipeline(BaseLLM):
        # Exercise the live-path memory processing using scripted local replies.
        # No provider SDK, credentials or network requests are involved.
        provider_name = "offline-test"
        model_name = "scripted-five-step"

        def complete(self, prompt, *, system="", **kwargs):
            if "planner" in system.lower():
                return json.dumps({"rationale": "Five bounded analysis steps", "steps": [
                    {"description": f"Compute part {i}", "specialist_type": "analysis",
                     "expected_output": "391"} for i in range(5)
                ]})
            if "critic" in system.lower():
                return '{"passed":true,"score":1.0,"issues":[],"suggestions":[]}'
            if "memory-consolidator" in system.lower():
                return "[]"
            return "23 * 17 = 391"

    compose = (ROOT / "docker-compose.freellmapi.yml").read_text(encoding="utf-8")
    budget = re.search(r'^\s+TANKAI_LLM_MAX_CALLS_PER_RUN: "(\d+)"\s*$', compose, re.MULTILINE)
    assert budget is not None
    monkeypatch.setenv("TANKAI_LLM_MAX_CALLS_PER_RUN", budget.group(1))
    monkeypatch.setenv("TANKAI_CRITIC_LLM", "")
    monkeypatch.setenv("TANKAI_EMBEDDER", "hashing")
    monkeypatch.setenv("TANKAI_REQUIRE_INDEPENDENT_CRITIC", "0")
    monkeypatch.setenv("TANKAI_REQUIRE_RESEARCH_EVIDENCE", "1")
    monkeypatch.setattr("tankai.web.runtime.get_llm", OfflinePipeline)
    manager = WorkspaceRuntimeManager(tmp_path)
    try:
        runtime = manager.get(tenant_id=str(uuid4()), workspace_id=str(uuid4()))
        result = runtime.tank.run("Berechne 23 mal 17 ohne Websuche")
        assert len(result.plan.steps) == 5
        assert result.verification_passed
        assert result.final_answer == "23 * 17 = 391"
        budget_receipt = next(r for r in result.receipts if r.action == "llm_call_budget")
        assert 14 <= budget_receipt.details["used"] <= int(budget.group(1))
        assert runtime.tank.run_store.list_recent(1)[0]["final_answer"] == result.final_answer
    finally:
        manager.close()


def test_setup_preserves_encryption_and_keeps_credentials_private(tmp_path):
    path = tmp_path / "private" / "gateway.env"
    assert helper.initialize(path)
    original = helper.load_config(path)[helper.ENCRYPTION]
    assert path.stat().st_mode & 0o777 == 0o600
    assert not helper.initialize(path)
    helper.connect("freellmapi-test-unified-key", path)
    assert helper.load_config(path)[helper.ENCRYPTION] == original
    assert helper.load_config(path)[helper.UNIFIED] == "freellmapi-test-unified-key"
    assert path.stat().st_mode & 0o777 == 0o600


def test_setup_rejects_symlink_and_public_secret_file(tmp_path):
    real = tmp_path / "real"
    helper.initialize(real)
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(ValueError, match="symlink"):
        helper.initialize(link)
    real.chmod(0o644)
    with pytest.raises(ValueError, match="0600"):
        helper.load_config(real)


def test_connect_rejects_env_injection_without_changing_file(tmp_path):
    path = tmp_path / "gateway.env"
    helper.initialize(path)
    original = path.read_bytes()
    with pytest.raises(ValueError):
        helper.connect("gateway-token\nEXTRA=bad", path)
    assert path.read_bytes() == original


def configured(tmp_path):
    path = tmp_path / "gateway.env"
    helper.initialize(path)
    helper.connect("freellmapi-test-unified-key", path)
    return path


def test_virtual_router_entries_cannot_pass_readiness(tmp_path, monkeypatch):
    path = configured(tmp_path)
    monkeypatch.setattr(helper, "request_json", lambda *args: ({"data": [
        {"id": "auto", "execution_status": "ready"},
        {"id": "auto:coding", "execution_status": "ready"},
        {"id": "fusion", "execution_status": "ready"},
        {"id": "real-model", "execution_status": "needsKey"},
    ]}, ""))
    with pytest.raises(ValueError, match="no ready model"):
        helper.check(path=path)


def test_check_never_requests_inference(tmp_path, monkeypatch):
    path = configured(tmp_path)
    calls = []

    def request(route, key, payload=None):
        calls.append((route, payload))
        return {"data": [{"id": "model", "execution_status": "ready"}]}, ""

    monkeypatch.setattr(helper, "request_json", request)
    result = helper.check(path=path)
    assert calls == [("/v1/models?available=true", None)]
    assert result["inference_requested"] is False
    assert result["production_ready"] is False


def test_smoke_is_one_bounded_request_and_does_not_print_secret(tmp_path, monkeypatch):
    path = configured(tmp_path)
    calls = []

    def request(route, key, payload=None):
        calls.append((route, payload))
        if payload is None:
            return {"data": [{"id": "model", "execution_status": "ready"}]}, ""
        return {"model": "actual-model", "choices": [{"message": {"content": "TANKAI_OK"}}]}, "provider/actual-model"

    monkeypatch.setattr(helper, "request_json", request)
    result = helper.check(smoke=True, path=path)
    assert len(calls) == 2
    assert calls[1][1]["max_tokens"] == 32
    assert result["expected_reply"] is True
    assert result["routed_via"] == "provider/actual-model"
    assert result["production_ready"] is False
    assert "freellmapi-test-unified-key" not in json.dumps(result)


def test_smoke_rejects_empty_model_response(tmp_path, monkeypatch):
    path = configured(tmp_path)

    def request(route, key, payload=None):
        if payload is None:
            return {"data": [{"id": "model", "execution_status": "ready"}]}, ""
        return {"choices": []}, ""

    monkeypatch.setattr(helper, "request_json", request)
    with pytest.raises(ValueError, match="no textual model"):
        helper.check(smoke=True, path=path)
