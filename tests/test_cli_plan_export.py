"""Malformed and repaired plans keep explicit provenance through real CLI export."""
import json

import pytest

import tankai
from tankai.cli import main
from test_planner_parse_gate import ScriptedLLM, VALID


@pytest.mark.parametrize("plans, expected_fallback, expected_error, version", [
    (["bad JSON"], True, "invalid_json", 3),
    (['{"steps":[]}'], True, "invalid_steps", 3),
    (["bad JSON",VALID], False, None, 2),
    ([VALID], False, None, 1),
])
def test_cli_and_history_export_plan_state_without_hiding_a_parse_failure(
        tmp_path, monkeypatch, capsys, plans, expected_fallback, expected_error, version):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TANKAI_EMBEDDER","hashing")
    monkeypatch.setenv("TANKAI_CRITIC_LLM","")
    monkeypatch.setenv("TANKAI_REQUIRE_INDEPENDENT_CRITIC","0")
    monkeypatch.setenv("TANKAI_REQUIRE_RESEARCH_EVIDENCE","0")
    llm=ScriptedLLM(plans,simulation=True)
    monkeypatch.setattr(tankai,"get_llm",lambda *args,**kwargs:llm)
    instances=[]
    original=tankai.TankAI
    def create(**kwargs):
        instance=original(**kwargs)
        instances.append(instance)
        return instance
    monkeypatch.setattr(tankai,"TankAI",create)
    output=tmp_path/"cli-result.json"
    try:
        arguments=["An offline synthetic export test","--llm","mock","--no-tools","--json",str(output)]
        if not expected_fallback:
            arguments.append("--quiet")
        assert main(arguments)==4  # Existing CLI convention: simulation, never a live success.
        if expected_fallback:
            assert f"Planstruktur ungültig ({expected_error})" in capsys.readouterr().out
        exported=json.loads(output.read_text(encoding="utf-8"))
        assert exported["plan"]["parse_fallback"] is expected_fallback
        assert exported["plan"]["parse_error"]==expected_error
        assert exported["plan"]["version"]==version
        saved=json.loads((tmp_path/"tankai_runs.jsonl").read_text(encoding="utf-8").splitlines()[-1])
        assert saved["plan_parse_fallback"] is expected_fallback
        assert saved["plan_parse_error"]==expected_error
        assert saved["plan_version"]==version
        assert exported["plan_gate_passed"] is not expected_fallback
        if expected_fallback:
            assert exported["plan"]["steps"]==[]
            assert not exported["release_ready"] and not saved["release_ready"]
            assert llm.calls==["plan"]*3
    finally:
        for instance in instances:
            if instance.ltm is not None:
                instance.ltm.close()
            instance.memory.close()
