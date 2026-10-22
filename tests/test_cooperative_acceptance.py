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
    AcceptanceCandidate,
    baseline_receipt,
    candidate_receipt,
    corpus_digest,
    human_review_template,
    load_baseline,
    load_candidate,
    load_corpus,
    load_human_review,
    run_mock_candidate,
)


@pytest.mark.parametrize("document_kind", ["corpus", "baseline", "candidate", "review"])
def test_evaluation_loaders_reject_duplicate_root_keys(tmp_path, document_kind) -> None:
    corpus = load_corpus()
    candidate = load_candidate(corpus=corpus)
    documents = {
        "corpus": corpus,
        "baseline": load_baseline(corpus=corpus),
        "candidate": candidate,
        "review": human_review_template(corpus, candidate),
    }
    path = tmp_path / "duplicate.json"
    # Even identical values are ambiguous and must not be silently accepted.
    path.write_text('{"schema_version": 1,' + documents[document_kind].model_dump_json()[1:],
                    encoding="utf-8")
    loaders = {
        "corpus": lambda: load_corpus(path),
        "baseline": lambda: load_baseline(path, corpus),
        "candidate": lambda: load_candidate(path, corpus),
        "review": lambda: load_human_review(path, corpus, candidate),
    }
    with pytest.raises(ValueError, match="doppelte Objektschlüssel"):
        loaders[document_kind]()


@pytest.mark.parametrize("second_key", ['"verdict"', '"verdi\\u0063t"'])
def test_human_review_rejects_nested_conflicting_and_escaped_duplicate_keys(tmp_path, second_key) -> None:
    document = json.dumps(_completed_human_review_document())
    document = document.replace('"verdict": "failed"',
                                '"verdict": "passed", ' + second_key + ': "failed"', 1)
    path = tmp_path / "ambiguous-review.json"
    path.write_text(document, encoding="utf-8")
    corpus = load_corpus()
    with pytest.raises(ValueError, match="doppelte Objektschlüssel"):
        load_human_review(path, corpus, load_candidate(corpus=corpus))


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


def test_candidate_case_diagnostics_match_aggregate_without_exposing_artifacts() -> None:
    corpus = load_corpus()
    candidate = load_candidate(corpus=corpus)
    receipt = candidate_receipt(corpus, load_baseline(corpus=corpus), candidate)
    results = receipt["case_results"]
    assert [item["case_id"] for item in results] == [case.case_id for case in corpus.cases]
    assert sum(item["successful"] for item in results) == receipt["successful_cases"]
    assert sum(
        criterion["verdict"] == "failed"
        for item in results for criterion in item["criteria"]
    ) == receipt["criteria_failed"]
    for case, result in zip(corpus.cases, results):
        assert set(result) == {"case_id", "successful", "criteria"}
        assert [item["criterion_id"] for item in result["criteria"]] == [
            item.criterion_id for item in case.acceptance_criteria
        ]
        for criterion, diagnostic in zip(case.acceptance_criteria, result["criteria"]):
            assert set(diagnostic) == {
                "criterion_id", "evaluator", "verdict", "human_review_required"
            }
            assert diagnostic["evaluator"] == criterion.evaluator
            assert diagnostic["human_review_required"] == (criterion.evaluator == "human")
    reordered = candidate.model_copy(update={"observations": list(reversed(candidate.observations))})
    assert candidate_receipt(corpus, load_baseline(corpus=corpus), reordered)["case_results"] == results


def test_current_mock_execution_reproduces_versioned_plan_admission_candidate() -> None:
    corpus = load_corpus()
    # Preserve the original five-call candidate and its historical revision.
    # The admission gate intentionally changes output and call count, so bind
    # current execution to its separately recorded candidate, not the old one.
    expected = load_candidate(
        DEFAULT_CANDIDATE.with_name("cooperative_acceptance_v1_plan_admission_candidate.json"), corpus
    )
    current = run_mock_candidate(
        corpus,
        source_revision=expected.source_revision,
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
    assert all(item.metrics.llm_calls == 2 for item in current.observations)
    assert all("nicht ausgeführt" in item.output for item in current.observations)
    receipt = candidate_receipt(corpus, load_baseline(corpus=corpus), current)
    assert receipt["successful_cases"] == 0 and receipt["pilot_eligible"] is False
    assert receipt["human_reviewed"] is False


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


def _completed_human_review_document() -> dict[str, object]:
    corpus = load_corpus()
    candidate = load_candidate(corpus=corpus)
    document = human_review_template(corpus, candidate).model_dump(mode="json")
    document.update(
        {
            "review_id": "independent-review-1",
            "reviewer_identity": "human-reviewer-1",
            "reviewed_at": "2026-10-14T08:00:00Z",
            "independent_reviewer_attested": True,
        }
    )
    for review_case in document["cases"]:
        for decision in review_case["decisions"]:
            decision["verdict"] = "failed"
            decision["evidence"] = "Das Candidate-Artefakt belegt dieses Kriterium nicht."
    return document


@pytest.mark.parametrize("identity", ["   ", "\t\n ", "self", "padded-self", "uppercase-self"])
def test_human_review_rejects_empty_and_equivalent_system_identity(tmp_path, identity) -> None:
    corpus = load_corpus()
    candidate = load_candidate(corpus=corpus)
    identities = {
        "self": candidate.system_identity,
        "padded-self": f"  {candidate.system_identity}\t",
        "uppercase-self": candidate.system_identity.upper(),
    }
    document = _completed_human_review_document()
    document["reviewer_identity"] = identities.get(identity, identity)
    path = tmp_path / "review.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="Prüferkennung|unabhängiger Prüfer"):
        load_human_review(path, corpus, candidate)


@pytest.mark.parametrize("timestamp", ["2026-10-14T08:00:00", "2026-10-14"])
def test_completed_review_requires_explicit_timezone(tmp_path, timestamp) -> None:
    document = _completed_human_review_document()
    document["reviewed_at"] = timestamp
    path = tmp_path / "review.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    corpus = load_corpus()
    with pytest.raises(ValueError, match="explizite Zeitzone"):
        load_human_review(path, corpus, load_candidate(corpus=corpus))


@pytest.mark.parametrize("timestamp", [
    "2026-10-14T08:00:00Z", "2026-10-14T10:00:00+02:00", "2026-10-14T03:00:00-05:00",
])
def test_review_receipt_normalizes_equivalent_instants_to_utc(tmp_path, timestamp) -> None:
    document = _completed_human_review_document()
    document["reviewed_at"] = timestamp
    path = tmp_path / "review.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    corpus = load_corpus()
    candidate = load_candidate(corpus=corpus)
    review = load_human_review(path, corpus, candidate)
    receipt = candidate_receipt(corpus, load_baseline(corpus=corpus), candidate, review)
    assert receipt["human_reviewed_at"] == "2026-10-14T08:00:00+00:00"
    assert receipt["candidate_weighted_score"] == 0.1778
    assert receipt["pilot_eligible"] is False


def test_human_review_template_binds_exact_candidate_and_human_criteria() -> None:
    corpus = load_corpus()
    candidate = load_candidate(corpus=corpus)
    review = human_review_template(corpus, candidate)

    assert review.review_id is None
    assert review.reviewer_identity is None
    assert review.source_revision == candidate.source_revision
    assert review.corpus_sha256 == corpus_digest(corpus)
    assert len(review.cases) == 3
    assert sum(len(item.decisions) for item in review.cases) == 8
    assert all(
        decision.verdict == "pending"
        for item in review.cases
        for decision in item.decisions
    )
    assert {
        item.case.case_id: item.output_sha256 for item in review.cases
    } == {
        item.case_id: item.output_sha256 for item in candidate.observations
    }


def test_completed_human_review_updates_receipt_without_claiming_pilot(tmp_path) -> None:
    review_path = tmp_path / "review.json"
    review_path.write_text(
        json.dumps(_completed_human_review_document()), encoding="utf-8"
    )
    corpus = load_corpus()
    baseline = load_baseline(corpus=corpus)
    candidate = load_candidate(corpus=corpus)
    review = load_human_review(review_path, corpus, candidate)
    receipt = candidate_receipt(corpus, baseline, candidate, review)

    assert receipt["human_reviewed"] is True
    assert receipt["human_review_id"] == "independent-review-1"
    assert receipt["candidate_weighted_score"] == 0.1778
    assert receipt["pilot_eligible"] is False
    assert receipt["quality_claim"] == "simulation-candidate-human-reviewed"

    updated = _completed_human_review_document()
    updated["cases"][0]["decisions"][0]["verdict"] = "passed"
    updated["cases"][0]["decisions"][0]["evidence"] = (
        "Synthetisches Positivurteil zur Prüfung der gewichteten Receipt-Berechnung."
    )
    review_path.write_text(json.dumps(updated), encoding="utf-8")
    rescored = candidate_receipt(
        corpus,
        baseline,
        candidate,
        load_human_review(review_path, corpus, candidate),
    )
    assert rescored["candidate_weighted_score"] > receipt["candidate_weighted_score"]
    assert rescored["pilot_eligible"] is False
    changed_id = updated["cases"][0]["decisions"][0]["criterion_id"]
    changes = []
    for before_case, after_case in zip(receipt["case_results"], rescored["case_results"]):
        for before, after in zip(before_case["criteria"], after_case["criteria"]):
            assert after["human_review_required"] is False
            if before != after:
                changes.append(after["criterion_id"])
                assert after["verdict"] == "passed"
                assert after["evaluator"] == "human"
    assert changes == [changed_id]


def test_human_review_rejects_pending_or_tampered_artifact(tmp_path) -> None:
    corpus = load_corpus()
    candidate = load_candidate(corpus=corpus)
    pending = _completed_human_review_document()
    pending["cases"][0]["decisions"][0]["verdict"] = "pending"
    pending_path = tmp_path / "pending.json"
    pending_path.write_text(json.dumps(pending), encoding="utf-8")
    with pytest.raises(ValueError, match="noch offen"):
        load_human_review(pending_path, corpus, candidate)

    tampered = _completed_human_review_document()
    tampered["cases"][0]["candidate_output"] += " manipuliert"
    tampered_path = tmp_path / "tampered.json"
    tampered_path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(ValueError, match="Artefakt"):
        load_human_review(tampered_path, corpus, candidate)


def test_candidate_cli_writes_human_review_template(tmp_path) -> None:
    review_path = tmp_path / "review-template.json"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "tankai.evaluation.cooperative_acceptance",
            "--candidate",
            str(DEFAULT_CANDIDATE),
            "--write-human-review-template",
            str(review_path),
        ],
        check=True,
        text=True,
        capture_output=True,
    )

    assert json.loads(completed.stdout)["human_reviewed"] is False
    template = json.loads(review_path.read_text(encoding="utf-8"))
    assert template["source_revision"] == load_candidate().source_revision
    assert all(
        decision["verdict"] == "pending"
        for item in template["cases"]
        for decision in item["decisions"]
    )


@pytest.mark.parametrize("target_kind", ["file", "symlink", "dangling_symlink"])
def test_review_template_cli_never_overwrites_existing_target(tmp_path, target_kind) -> None:
    target = tmp_path / "review.json"
    original = json.dumps(_completed_human_review_document()).encode("utf-8")
    linked = tmp_path / "completed-review.json"
    if target_kind == "file":
        target.write_bytes(original)
    else:
        if target_kind == "symlink":
            linked.write_bytes(original)
        target.symlink_to(linked)
    completed = subprocess.run(
        [sys.executable, "-m", "tankai.evaluation.cooperative_acceptance",
         "--candidate", str(DEFAULT_CANDIDATE),
         "--write-human-review-template", str(target)],
        text=True, capture_output=True,
    )
    assert completed.returncode == 2
    assert completed.stdout == ""
    assert "Review-Ziel existiert bereits" in completed.stderr
    if target_kind == "file":
        assert target.read_bytes() == original
    else:
        assert target.is_symlink()
        if target_kind == "symlink":
            assert linked.read_bytes() == original
        else:
            assert not linked.exists()


@pytest.mark.parametrize("claim_passed", [False, True])
def test_review_flag_alone_cannot_authorize_receipt(tmp_path, claim_passed) -> None:
    document = json.loads(DEFAULT_CANDIDATE.read_text(encoding="utf-8"))
    document["human_reviewed"] = True
    if claim_passed:
        observation = document["observations"][0]
        observation["criteria_passed"].append(observation["criteria_failed"].pop(0))
    candidate = AcceptanceCandidate.model_validate(document)
    corpus = load_corpus()
    baseline = load_baseline(corpus=corpus)
    with pytest.raises(ValueError, match="Human-Review-Beleg"):
        candidate_receipt(corpus, baseline, candidate)

    path = tmp_path / "flag-only.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, "-m", "tankai.evaluation.cooperative_acceptance",
         "--candidate", str(path)],
        text=True, capture_output=True,
    )
    assert completed.returncode != 0
    assert completed.stdout == ""
    assert "Human-Review-Beleg" in completed.stderr


def test_bound_review_overrides_self_declared_human_passes(tmp_path) -> None:
    corpus = load_corpus()
    baseline = load_baseline(corpus=corpus)
    candidate = load_candidate(corpus=corpus)
    candidate = candidate.model_copy(update={"human_reviewed": True})
    observation = candidate.observations[0]
    observation.criteria_passed.extend(observation.criteria_failed)
    observation.criteria_failed.clear()
    path = tmp_path / "review.json"
    path.write_text(json.dumps(_completed_human_review_document()), encoding="utf-8")
    review = load_human_review(path, corpus, candidate)
    receipt = candidate_receipt(corpus, baseline, candidate, review)
    assert receipt["human_reviewed"] is True
    assert receipt["candidate_weighted_score"] == 0.1778
    assert receipt["criteria_failed"] == 12
    assert receipt["pilot_eligible"] is False
