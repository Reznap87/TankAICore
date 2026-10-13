from __future__ import annotations

import json
import subprocess
import sys

import pytest
from pydantic import ValidationError

from tankai.evaluation.cooperative_acceptance import (
    DEFAULT_BASELINE,
    DEFAULT_CANDIDATE,
    DEFAULT_CORPUS,
    AcceptanceBaseline,
    AcceptanceCorpus,
    baseline_receipt,
    candidate_receipt,
    corpus_digest,
    load_baseline,
    load_candidate,
    load_corpus,
    run_mock_candidate,
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

@pytest.mark.parametrize(
    ("change", "message"),
    [
        ("duplicate_case", "jeden Fall genau einmal"),
        ("unknown_case", "jeden Fall genau einmal"),
        ("missing_criterion", "Kriterien"),
        ("unknown_criterion", "Kriterien"),
        ("duration_seconds", "Zeitlimit"),
        ("llm_calls", "LLM-Limit"),
        ("external_cost_usd", "Kostenlimit"),
        ("output_chars", "Ausgabelimit"),
        ("simulation_cost", "ohne externe Aufrufe"),
    ],
)
def test_direct_receipt_rejects_invalid_baseline(change: str, message: str) -> None:
    corpus = load_corpus()
    document = json.loads(DEFAULT_BASELINE.read_text(encoding="utf-8"))
    first = document["observations"][0]
    if change == "duplicate_case":
        document["observations"].append(first.copy())
    elif change == "unknown_case":
        first["case_id"] = "coop-v1-unknown"
    elif change == "missing_criterion":
        first["criteria_failed"].pop()
    elif change == "unknown_criterion":
        first["criteria_failed"][0] = "unknown_criterion"
    elif change == "simulation_cost":
        first["metrics"]["external_cost_usd"] = 0.01
    else:
        limits = {
            "duration_seconds": 91,
            "llm_calls": 13,
            "external_cost_usd": 0.21,
            "output_chars": 8001,
        }
        first["metrics"][change] = limits[change]
    baseline = AcceptanceBaseline.model_validate(document)

    with pytest.raises(ValueError, match=message):
        baseline_receipt(corpus, baseline)


def test_receipt_rechecks_mutable_criterion_lists() -> None:
    corpus = load_corpus()
    baseline = load_baseline(corpus=corpus)
    baseline.observations[0].criteria_failed.append(
        baseline.observations[0].criteria_passed[0]
    )

    with pytest.raises(ValueError, match="Kriterien"):
        baseline_receipt(corpus, baseline)


def test_loader_rejects_cost_without_external_calls(tmp_path) -> None:
    document = json.loads(DEFAULT_BASELINE.read_text(encoding="utf-8"))
    document["observations"][0]["metrics"]["external_cost_usd"] = 0.01
    changed = tmp_path / "simulation-cost.json"
    changed.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ValueError, match="ohne externe Aufrufe"):
        load_baseline(changed, load_corpus())


def test_checked_in_candidate_retains_artifacts_and_honest_comparison() -> None:
    corpus = load_corpus()
    baseline = load_baseline(corpus=corpus)
    candidate = load_candidate(corpus=corpus)
    receipt = candidate_receipt(corpus, baseline, candidate)

    assert candidate.corpus_sha256 == corpus_digest(corpus)
    assert all(item.output for item in candidate.observations)
    assert all(item.metrics.llm_calls == 5 for item in candidate.observations)
    assert receipt["candidate_weighted_score"] == 0.1778
    assert receipt["baseline_weighted_score"] == 0.1778
    assert receipt["score_delta"] == 0.0
    assert receipt["improves_baseline"] is False
    assert receipt["successful_cases"] == 0
    assert receipt["pilot_call_limit_met"] is True
    assert receipt["pilot_eligible"] is False
    assert receipt["source_revision"] == "65e0b78054f756d0e08a2c2509377c2bdc6d9acc"
    assert receipt["human_reviewed"] is False
    assert receipt["quality_claim"] == "simulation-candidate-only"


def test_current_mock_execution_reproduces_checked_in_candidate_artifacts() -> None:
    corpus = load_corpus()
    expected = load_candidate(corpus=corpus)
    current = run_mock_candidate(
        corpus,
        source_revision="65e0b78054f756d0e08a2c2509377c2bdc6d9acc",
    )

    assert {
        item.case_id: item.output_sha256 for item in current.observations
    } == {
        item.case_id: item.output_sha256 for item in expected.observations
    }
    assert [item.criteria_passed for item in current.observations] == [
        item.criteria_passed for item in expected.observations
    ]
    assert [item.criteria_failed for item in current.observations] == [
        item.criteria_failed for item in expected.observations
    ]


def test_candidate_rejects_tampered_artifact(tmp_path) -> None:
    document = json.loads(DEFAULT_CANDIDATE.read_text(encoding="utf-8"))
    document["observations"][0]["output"] += " manipuliert"
    changed = tmp_path / "tampered-candidate.json"
    changed.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ValidationError, match="Artefakt-Hash"):
        load_candidate(changed, load_corpus())


def test_internal_candidate_case_rejects_external_calls(tmp_path) -> None:
    document = json.loads(DEFAULT_CANDIDATE.read_text(encoding="utf-8"))
    internal = next(
        item for item in document["observations"] if item["case_id"] == "coop-v1-document-analysis"
    )
    internal["external_calls_made"] = True
    changed = tmp_path / "external-internal-candidate.json"
    changed.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ValueError, match="Interner Candidate-Fall"):
        load_candidate(changed, load_corpus())


def test_unreviewed_candidate_cannot_claim_human_criteria(tmp_path) -> None:
    document = json.loads(DEFAULT_CANDIDATE.read_text(encoding="utf-8"))
    idea = document["observations"][0]
    claimed = idea["criteria_failed"].pop(0)
    idea["criteria_passed"].append(claimed)
    changed = tmp_path / "unreviewed-human-claim.json"
    changed.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ValueError, match="keine Human-Kriterien"):
        load_candidate(changed, load_corpus())


def test_candidate_cli_emits_machine_readable_comparison() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "tankai.evaluation.cooperative_acceptance",
            "--candidate",
            str(DEFAULT_CANDIDATE),
        ],
        check=True,
        text=True,
        capture_output=True,
    )

    receipt = json.loads(completed.stdout)
    assert receipt["candidate_id"] == "tankai-current-mock-candidate-v1"
    assert receipt["score_delta"] == 0.0
    assert receipt["pilot_eligible"] is False
