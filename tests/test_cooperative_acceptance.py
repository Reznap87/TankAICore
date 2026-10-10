from __future__ import annotations

import json
import subprocess
import sys

import pytest
from pydantic import ValidationError

from tankai.evaluation.cooperative_acceptance import (
    DEFAULT_BASELINE,
    DEFAULT_CORPUS,
    AcceptanceCorpus,
    baseline_receipt,
    corpus_digest,
    load_baseline,
    load_corpus,
)


def test_frozen_corpus_covers_p1_cases_with_bounded_contracts() -> None:
    corpus = load_corpus()

    assert corpus.corpus_id == "tankai-cooperative-acceptance-v1"
    assert corpus.frozen_on.isoformat() == "2026-10-10"
    assert {item.category for item in corpus.cases} == {
        "idea_development",
        "document_analysis",
        "code_prototype",
    }
    assert len({item.case_id for item in corpus.cases}) == 3
    for case in corpus.cases:
        assert len(case.acceptance_criteria) >= 3
        assert case.limits.max_llm_calls <= 40
        assert case.limits.max_duration_seconds <= 300
        assert case.limits.max_external_cost_usd <= 1


def test_internal_document_case_forbids_external_tools_and_cost() -> None:
    case = next(item for item in load_corpus().cases if item.category == "document_analysis")

    assert case.data_class == "internal"
    assert case.allowed_tools == ["none"]
    assert case.limits.max_external_cost_usd == 0
    assert case.input_artifacts[0].artifact_id == "pilot-note"


def test_checked_in_baseline_is_bound_to_exact_corpus_and_reports_gaps() -> None:
    corpus = load_corpus()
    baseline = load_baseline(corpus=corpus)
    receipt = baseline_receipt(corpus, baseline)

    assert baseline.corpus_sha256 == corpus_digest(corpus)
    assert receipt == {
        "receipt_version": 1,
        "baseline_id": "tankai-cooperative-mock-baseline-v1",
        "corpus_id": "tankai-cooperative-acceptance-v1",
        "corpus_sha256": "8badde3d517fcd3491c0adb6f8ccf16998970393952393fa01c5056bbc5d98c3",
        "frozen_on": "2026-10-10",
        "case_count": 3,
        "categories": ["code_prototype", "document_analysis", "idea_development"],
        "criteria_total": 15,
        "criteria_failed": 12,
        "failed_cases": 3,
        "weighted_score": 0.1778,
        "external_calls_made": False,
        "total_external_cost_usd": 0.0,
        "quality_claim": "simulation-baseline-only",
    }


def test_changed_corpus_invalidates_baseline(tmp_path) -> None:
    document = json.loads(DEFAULT_CORPUS.read_text(encoding="utf-8"))
    document["cases"][0]["goal"] += " Geändert."
    changed = tmp_path / "changed.json"
    changed.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ValueError, match="Korpus-Hash"):
        load_baseline(DEFAULT_BASELINE, load_corpus(changed))


def test_baseline_rejects_observation_outside_case_budget(tmp_path) -> None:
    document = json.loads(DEFAULT_BASELINE.read_text(encoding="utf-8"))
    document["observations"][0]["metrics"]["duration_seconds"] = 91
    changed = tmp_path / "over-budget.json"
    changed.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ValueError, match="Zeitlimit"):
        load_baseline(changed, load_corpus())


def test_corpus_rejects_missing_category() -> None:
    document = json.loads(DEFAULT_CORPUS.read_text(encoding="utf-8"))
    document["cases"][2]["category"] = "idea_development"

    with pytest.raises(ValidationError, match="alle drei P1-Kategorien"):
        AcceptanceCorpus.model_validate(document)


def test_baseline_cli_emits_machine_readable_receipt() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "tankai.evaluation.cooperative_acceptance"],
        check=True,
        text=True,
        capture_output=True,
    )

    receipt = json.loads(completed.stdout)
    assert receipt["case_count"] == 3
    assert receipt["criteria_failed"] == 12
    assert receipt["external_calls_made"] is False
