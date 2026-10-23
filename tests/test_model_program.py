from __future__ import annotations

import json
import subprocess
import sys

import pytest
from pydantic import ValidationError

from tankai.evaluation.model_program import CATEGORIES, ModelDevelopmentPack, load_pack, manifest, pack_digest, prepare_run


def test_development_pack_covers_six_domains_without_claiming_model_evidence():
    pack = load_pack()
    result = manifest(pack)
    assert len(pack.cases) == 12
    assert set(result["categories"]) == CATEGORIES
    assert result["split"] == "public_development" and result["training_allowed"] is False
    assert result["quality_result"] is None and result["frontier_parity_established"] is False
    assert result["inference_requested"] is False and result["training_started"] is False
    assert "sealed_independent_holdout" in result["missing_gates"]


@pytest.mark.parametrize("mutation", ["duplicate-case", "missing-category", "training", "unknown", "empty-rubric"])
def test_evaluation_contract_rejects_ambiguous_or_weakened_packs(mutation):
    document = load_pack().model_dump()
    if mutation == "duplicate-case":
        document["cases"][-1] = document["cases"][0]
    if mutation == "missing-category":
        for case in document["cases"]:
            if case["category"] == "recovery":
                case["category"] = "reasoning"
    if mutation == "training":
        document["training_allowed"] = True
    if mutation == "unknown":
        document["frontier_parity_established"] = True
    if mutation == "empty-rubric":
        document["cases"][0]["rubric"] = [" ", " "]
    with pytest.raises(ValidationError):
        ModelDevelopmentPack.model_validate(document)


def test_duplicate_json_keys_and_direct_object_bypass_are_rejected(tmp_path):
    path = tmp_path / "pack.json"
    path.write_text('{"schema_version":1,' + load_pack().model_dump_json()[1:], encoding="utf-8")
    with pytest.raises(ValueError, match="Duplicate"):
        load_pack(path)
    bad = load_pack().model_copy(update={"training_allowed": True})
    with pytest.raises(ValidationError):
        manifest(bad)


def test_pack_hash_changes_with_prompts_and_not_with_file_formatting(tmp_path):
    pack = load_pack()
    path = tmp_path / "pack.json"
    path.write_text(pack.model_dump_json(indent=4), encoding="utf-8")
    assert pack_digest(pack) == pack_digest(load_pack(path))
    document = pack.model_dump()
    document["cases"][0]["prompt"] += " Zusätzlicher Kontext."
    assert pack_digest(pack) != pack_digest(ModelDevelopmentPack.model_validate(document))


@pytest.mark.parametrize("track", ["raw_model", "tankai_system"])
def test_prepared_runs_remain_pending_and_separate_model_from_system(track):
    run = prepare_run(load_pack(), model_identity="candidate@sha256:pending", track=track)
    assert run["status"] == "prepared_not_executed" and run["budget_authorized"] is False
    assert (run["tool_policy"] == "none") == (track == "raw_model")
    assert all(row["output"] is None and row["external_cost_usd"] is None
               and row["status"] == "pending" for row in run["observations"])
    assert all(criterion["verdict"] == "pending" and criterion["evidence"] is None
               for row in run["observations"] for criterion in row["criteria"])


def test_preparation_cannot_overwrite_previous_run(tmp_path):
    path = tmp_path / "previous.json"
    path.write_text("existing evidence", encoding="utf-8")
    result = subprocess.run([sys.executable, "-m", "tankai.evaluation.model_program", "prepare-run",
                             "--model-identity", "reference-version", "--output", str(path)],
                            capture_output=True, timeout=15)
    assert result.returncode != 0 and path.read_text(encoding="utf-8") == "existing evidence"


def test_cli_manifest_needs_no_provider_or_training_access():
    result = subprocess.run([sys.executable, "-m", "tankai.evaluation.model_program", "manifest"],
                            capture_output=True, timeout=15, check=True)
    receipt = json.loads(result.stdout)
    assert receipt["case_count"] == 12 and receipt["quality_result"] is None
