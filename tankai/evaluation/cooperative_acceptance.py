"""Frozen cooperative-AI corpus and baseline receipt validation.

This module deliberately does not call a model or provider.  It binds the task,
budget and scoring contract before candidate work begins and verifies the
checked-in baseline observation against that immutable corpus.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CORPUS = ROOT / "evaluation" / "cooperative_acceptance_v1.json"
DEFAULT_BASELINE = ROOT / "evaluation" / "cooperative_acceptance_v1_baseline.json"
DEFAULT_CANDIDATE = ROOT / "evaluation" / "cooperative_acceptance_v1_candidate.json"
REQUIRED_CATEGORIES = {
    "idea_development",
    "document_analysis",
    "code_prototype",
}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Limits(StrictModel):
    max_duration_seconds: int = Field(ge=1, le=300)
    max_llm_calls: int = Field(ge=1, le=40)
    max_external_cost_usd: float = Field(ge=0, le=1)
    max_output_chars: int = Field(ge=100, le=50_000)


class AcceptanceCriterion(StrictModel):
    criterion_id: str = Field(pattern=r"^[a-z][a-z0-9_]{2,63}$")
    description: str = Field(min_length=10, max_length=500)
    evaluator: Literal["contract", "human"]
    weight: int = Field(ge=1, le=10)


class InputArtifact(StrictModel):
    artifact_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,63}$")
    media_type: Literal["text/plain", "text/markdown", "application/json"]
    content: str = Field(min_length=1, max_length=20_000)


class AcceptanceCase(StrictModel):
    case_id: str = Field(pattern=r"^coop-v1-[a-z0-9-]+$")
    category: Literal["idea_development", "document_analysis", "code_prototype"]
    goal: str = Field(min_length=20, max_length=2_000)
    definition_of_done: str = Field(min_length=20, max_length=2_000)
    constraints: list[str] = Field(min_length=1, max_length=12)
    input_artifacts: list[InputArtifact] = Field(max_length=4)
    allowed_tools: list[Literal["none", "calculator", "local_files", "code_runner"]] = Field(
        min_length=1, max_length=4
    )
    data_class: Literal["public", "internal"]
    limits: Limits
    acceptance_criteria: list[AcceptanceCriterion] = Field(min_length=3, max_length=12)

    @model_validator(mode="after")
    def unique_contract_fields(self) -> "AcceptanceCase":
        if len(set(self.allowed_tools)) != len(self.allowed_tools):
            raise ValueError("allowed_tools enthält Duplikate")
        if "none" in self.allowed_tools and len(self.allowed_tools) != 1:
            raise ValueError("none darf nicht mit anderen Werkzeugen kombiniert werden")
        ids = [item.criterion_id for item in self.acceptance_criteria]
        if len(set(ids)) != len(ids):
            raise ValueError("criterion_id muss pro Fall eindeutig sein")
        return self


class AcceptanceCorpus(StrictModel):
    schema_version: Literal[1]
    corpus_id: Literal["tankai-cooperative-acceptance-v1"]
    frozen_on: date
    purpose: str = Field(min_length=20, max_length=1_000)
    cases: list[AcceptanceCase] = Field(min_length=3, max_length=3)

    @model_validator(mode="after")
    def complete_unique_corpus(self) -> "AcceptanceCorpus":
        ids = [item.case_id for item in self.cases]
        if len(set(ids)) != len(ids):
            raise ValueError("case_id muss im Korpus eindeutig sein")
        categories = {item.category for item in self.cases}
        if categories != REQUIRED_CATEGORIES:
            raise ValueError("Korpus muss genau alle drei P1-Kategorien abdecken")
        return self


class BaselineMetrics(StrictModel):
    duration_seconds: float = Field(ge=0, le=300)
    llm_calls: int = Field(ge=0, le=40)
    external_cost_usd: float = Field(ge=0, le=1)
    output_chars: int = Field(ge=0, le=50_000)
    source_count: int = Field(ge=0, le=1_000)
    receipt_count: int = Field(ge=0, le=1_000)


class BaselineObservation(StrictModel):
    case_id: str
    result_status: Literal["completed", "failed", "simulated"]
    criteria_passed: list[str]
    criteria_failed: list[str]
    failures: list[str]
    metrics: BaselineMetrics

    @model_validator(mode="after")
    def disjoint_criteria(self) -> "BaselineObservation":
        observed = self.criteria_passed + self.criteria_failed
        if len(set(observed)) != len(observed):
            raise ValueError("Baseline-Kriterien dürfen nicht doppelt vorkommen")
        if set(self.criteria_passed) & set(self.criteria_failed):
            raise ValueError("Kriterium darf nicht zugleich bestanden und fehlgeschlagen sein")
        if self.criteria_failed and not self.failures:
            raise ValueError("Fehlgeschlagene Kriterien benötigen eine Fehlerbeschreibung")
        return self


class AcceptanceBaseline(StrictModel):
    schema_version: Literal[1]
    baseline_id: Literal["tankai-cooperative-mock-baseline-v1"]
    corpus_id: Literal["tankai-cooperative-acceptance-v1"]
    corpus_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_at: datetime
    execution_mode: Literal["simulation"]
    system_identity: str = Field(min_length=5, max_length=200)
    external_calls_made: Literal[False]
    observations: list[BaselineObservation] = Field(min_length=3, max_length=20)


class CandidateObservation(StrictModel):
    case_id: str
    result_status: Literal["completed", "failed", "simulated"]
    output: str = Field(min_length=1, max_length=50_000)
    output_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    criteria_passed: list[str]
    criteria_failed: list[str]
    failures: list[str]
    external_calls_made: bool
    metrics: BaselineMetrics

    @model_validator(mode="after")
    def internally_consistent_observation(self) -> "CandidateObservation":
        observed = self.criteria_passed + self.criteria_failed
        if len(set(observed)) != len(observed):
            raise ValueError("Candidate-Kriterien dürfen nicht doppelt vorkommen")
        if self.criteria_failed and not self.failures:
            raise ValueError("Fehlgeschlagene Candidate-Kriterien benötigen eine Begründung")
        encoded = self.output.encode("utf-8")
        if hashlib.sha256(encoded).hexdigest() != self.output_sha256:
            raise ValueError("Candidate-Artefakt-Hash stimmt nicht")
        if len(self.output) != self.metrics.output_chars:
            raise ValueError("Candidate-Ausgabelänge stimmt nicht")
        if not self.external_calls_made and self.metrics.external_cost_usd != 0:
            raise ValueError("Candidate ohne externe Aufrufe darf keine externen Kosten melden")
        return self


class AcceptanceCandidate(StrictModel):
    schema_version: Literal[1]
    candidate_id: Literal["tankai-current-mock-candidate-v1"]
    corpus_id: Literal["tankai-cooperative-acceptance-v1"]
    corpus_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_at: datetime
    execution_mode: Literal["simulation"]
    system_identity: str = Field(min_length=5, max_length=200)
    source_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    human_reviewed: bool
    observations: list[CandidateObservation] = Field(min_length=3, max_length=20)


class HumanReviewDecision(StrictModel):
    criterion_id: str = Field(pattern=r"^[a-z][a-z0-9_]{2,63}$")
    description: str = Field(min_length=10, max_length=500)
    weight: int = Field(ge=1, le=10)
    verdict: Literal["pending", "passed", "failed"]
    evidence: str = Field(max_length=2_000)


class HumanReviewCase(StrictModel):
    case: AcceptanceCase
    candidate_output: str = Field(min_length=1, max_length=50_000)
    output_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    decisions: list[HumanReviewDecision] = Field(max_length=12)


class CandidateHumanReview(StrictModel):
    """Tamper-bound worksheet for an independent review of candidate artifacts."""

    schema_version: Literal[1]
    review_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_-]{2,63}$")
    candidate_id: Literal["tankai-current-mock-candidate-v1"]
    corpus_id: Literal["tankai-cooperative-acceptance-v1"]
    corpus_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    reviewer_identity: str | None = Field(default=None, min_length=3, max_length=200)
    reviewed_at: datetime | None = None
    independent_reviewer_attested: bool
    cases: list[HumanReviewCase] = Field(min_length=3, max_length=3)


def _read_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def load_corpus(path: Path = DEFAULT_CORPUS) -> AcceptanceCorpus:
    return AcceptanceCorpus.model_validate(_read_json(path))


def corpus_digest(corpus: AcceptanceCorpus) -> str:
    canonical = json.dumps(
        corpus.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _validate_baseline_contract(
    corpus: AcceptanceCorpus,
    baseline: AcceptanceBaseline,
) -> None:
    """Validate every scoring boundary, including direct receipt callers."""
    if baseline.corpus_id != corpus.corpus_id:
        raise ValueError("Baseline verweist auf einen anderen Korpus")
    if baseline.corpus_sha256 != corpus_digest(corpus):
        raise ValueError("Baseline-Korpus-Hash stimmt nicht")
    cases = {item.case_id: item for item in corpus.cases}
    observations = {item.case_id: item for item in baseline.observations}
    if set(observations) != set(cases) or len(observations) != len(baseline.observations):
        raise ValueError("Baseline muss jeden Fall genau einmal beobachten")
    for case_id, observation in observations.items():
        criterion_ids = {item.criterion_id for item in cases[case_id].acceptance_criteria}
        observed = observation.criteria_passed + observation.criteria_failed
        if set(observed) != criterion_ids or len(observed) != len(criterion_ids):
            raise ValueError(f"Baseline-Kriterien für {case_id} sind unvollständig")
        limits = cases[case_id].limits
        metrics = observation.metrics
        if metrics.duration_seconds > limits.max_duration_seconds:
            raise ValueError(f"Baseline überschreitet Zeitlimit für {case_id}")
        if metrics.llm_calls > limits.max_llm_calls:
            raise ValueError(f"Baseline überschreitet LLM-Limit für {case_id}")
        if metrics.external_cost_usd > limits.max_external_cost_usd:
            raise ValueError(f"Baseline überschreitet Kostenlimit für {case_id}")
        if metrics.output_chars > limits.max_output_chars:
            raise ValueError(f"Baseline überschreitet Ausgabelimit für {case_id}")
        if not baseline.external_calls_made and metrics.external_cost_usd != 0:
            raise ValueError(
                f"Baseline ohne externe Aufrufe darf keine externen Kosten für {case_id} melden"
            )


def load_baseline(
    path: Path = DEFAULT_BASELINE,
    corpus: AcceptanceCorpus | None = None,
) -> AcceptanceBaseline:
    corpus = corpus or load_corpus()
    baseline = AcceptanceBaseline.model_validate(_read_json(path))
    _validate_baseline_contract(corpus, baseline)
    return baseline


def _validate_candidate_contract(
    corpus: AcceptanceCorpus,
    candidate: AcceptanceCandidate,
) -> None:
    if candidate.corpus_id != corpus.corpus_id:
        raise ValueError("Candidate verweist auf einen anderen Korpus")
    if candidate.corpus_sha256 != corpus_digest(corpus):
        raise ValueError("Candidate-Korpus-Hash stimmt nicht")
    cases = {item.case_id: item for item in corpus.cases}
    observations = {item.case_id: item for item in candidate.observations}
    if set(observations) != set(cases) or len(observations) != len(candidate.observations):
        raise ValueError("Candidate muss jeden Fall genau einmal beobachten")
    for case_id, observation in observations.items():
        case = cases[case_id]
        criterion_ids = {item.criterion_id for item in case.acceptance_criteria}
        observed = observation.criteria_passed + observation.criteria_failed
        if set(observed) != criterion_ids or len(observed) != len(criterion_ids):
            raise ValueError(f"Candidate-Kriterien für {case_id} sind unvollständig")
        human_ids = {
            item.criterion_id for item in case.acceptance_criteria if item.evaluator == "human"
        }
        if not candidate.human_reviewed and human_ids & set(observation.criteria_passed):
            raise ValueError(
                f"Candidate ohne menschliche Prüfung darf keine Human-Kriterien für {case_id} bestehen"
            )
        metrics = observation.metrics
        if metrics.duration_seconds > case.limits.max_duration_seconds:
            raise ValueError(f"Candidate überschreitet Zeitlimit für {case_id}")
        if metrics.llm_calls > case.limits.max_llm_calls:
            raise ValueError(f"Candidate überschreitet LLM-Limit für {case_id}")
        if metrics.external_cost_usd > case.limits.max_external_cost_usd:
            raise ValueError(f"Candidate überschreitet Kostenlimit für {case_id}")
        if metrics.output_chars > case.limits.max_output_chars:
            raise ValueError(f"Candidate überschreitet Ausgabelimit für {case_id}")
        if case.data_class == "internal" and observation.external_calls_made:
            raise ValueError(f"Interner Candidate-Fall {case_id} darf keine externen Aufrufe melden")


def load_candidate(
    path: Path = DEFAULT_CANDIDATE,
    corpus: AcceptanceCorpus | None = None,
) -> AcceptanceCandidate:
    corpus = corpus or load_corpus()
    candidate = AcceptanceCandidate.model_validate(_read_json(path))
    _validate_candidate_contract(corpus, candidate)
    return candidate


def human_review_template(
    corpus: AcceptanceCorpus,
    candidate: AcceptanceCandidate,
) -> CandidateHumanReview:
    """Create a local worksheet bound to the exact corpus and candidate output."""
    _validate_candidate_contract(corpus, candidate)
    observations = {item.case_id: item for item in candidate.observations}
    return CandidateHumanReview(
        schema_version=1,
        candidate_id=candidate.candidate_id,
        corpus_id=corpus.corpus_id,
        corpus_sha256=candidate.corpus_sha256,
        source_revision=candidate.source_revision,
        independent_reviewer_attested=False,
        cases=[
            HumanReviewCase(
                case=case,
                candidate_output=observations[case.case_id].output,
                output_sha256=observations[case.case_id].output_sha256,
                decisions=[
                    HumanReviewDecision(
                        criterion_id=criterion.criterion_id,
                        description=criterion.description,
                        weight=criterion.weight,
                        verdict="pending",
                        evidence="",
                    )
                    for criterion in case.acceptance_criteria
                    if criterion.evaluator == "human"
                ],
            )
            for case in corpus.cases
        ],
    )


def _validate_human_review(
    corpus: AcceptanceCorpus,
    candidate: AcceptanceCandidate,
    review: CandidateHumanReview,
    *,
    require_completed: bool,
) -> None:
    _validate_candidate_contract(corpus, candidate)
    if review.candidate_id != candidate.candidate_id:
        raise ValueError("Human-Review verweist auf einen anderen Candidate")
    if review.corpus_id != corpus.corpus_id or review.corpus_sha256 != corpus_digest(corpus):
        raise ValueError("Human-Review verweist auf einen anderen Korpus")
    if review.source_revision != candidate.source_revision:
        raise ValueError("Human-Review verweist auf eine andere Source-Revision")

    cases = {item.case_id: item for item in corpus.cases}
    observations = {item.case_id: item for item in candidate.observations}
    reviewed_cases = {item.case.case_id: item for item in review.cases}
    if set(reviewed_cases) != set(cases) or len(reviewed_cases) != len(review.cases):
        raise ValueError("Human-Review muss jeden Candidate-Fall genau einmal enthalten")

    for case_id, review_case in reviewed_cases.items():
        case = cases[case_id]
        observation = observations[case_id]
        if review_case.case != case:
            raise ValueError(f"Human-Review-Fallvertrag für {case_id} wurde verändert")
        if (
            review_case.candidate_output != observation.output
            or review_case.output_sha256 != observation.output_sha256
        ):
            raise ValueError(f"Human-Review-Artefakt für {case_id} wurde verändert")
        expected = {
            item.criterion_id: item
            for item in case.acceptance_criteria
            if item.evaluator == "human"
        }
        decisions = {item.criterion_id: item for item in review_case.decisions}
        if set(decisions) != set(expected) or len(decisions) != len(review_case.decisions):
            raise ValueError(f"Human-Review-Kriterien für {case_id} sind unvollständig")
        for criterion_id, decision in decisions.items():
            criterion = expected[criterion_id]
            if decision.description != criterion.description or decision.weight != criterion.weight:
                raise ValueError(
                    f"Human-Review-Kriterium {criterion_id} für {case_id} wurde verändert"
                )
            if require_completed and decision.verdict == "pending":
                raise ValueError(f"Human-Review-Kriterium {criterion_id} ist noch offen")
            if require_completed and len(decision.evidence.strip()) < 10:
                raise ValueError(
                    f"Human-Review-Kriterium {criterion_id} benötigt eine Begründung"
                )

    if require_completed:
        if review.review_id is None or review.reviewer_identity is None or review.reviewed_at is None:
            raise ValueError("Abgeschlossenes Human-Review benötigt ID, Prüfer und Zeitpunkt")
        if not review.independent_reviewer_attested:
            raise ValueError("Abgeschlossenes Human-Review benötigt die Unabhängigkeitsbestätigung")
        if review.reviewer_identity == candidate.system_identity:
            raise ValueError("Candidate-System darf nicht als unabhängiger Prüfer auftreten")


def load_human_review(
    path: Path,
    corpus: AcceptanceCorpus,
    candidate: AcceptanceCandidate,
) -> CandidateHumanReview:
    review = CandidateHumanReview.model_validate(_read_json(path))
    _validate_human_review(corpus, candidate, review, require_completed=True)
    return review


def baseline_receipt(
    corpus: AcceptanceCorpus,
    baseline: AcceptanceBaseline,
) -> dict[str, object]:
    _validate_baseline_contract(corpus, baseline)
    cases = {item.case_id: item for item in corpus.cases}
    passed_weight = 0
    total_weight = 0
    failed_cases = 0
    failures = 0
    for observation in baseline.observations:
        criteria = {item.criterion_id: item for item in cases[observation.case_id].acceptance_criteria}
        total_weight += sum(item.weight for item in criteria.values())
        passed_weight += sum(criteria[item].weight for item in observation.criteria_passed)
        failures += len(observation.criteria_failed)
        failed_cases += bool(observation.criteria_failed)
    return {
        "receipt_version": 1,
        "baseline_id": baseline.baseline_id,
        "corpus_id": corpus.corpus_id,
        "corpus_sha256": baseline.corpus_sha256,
        "frozen_on": corpus.frozen_on.isoformat(),
        "case_count": len(corpus.cases),
        "categories": sorted(item.category for item in corpus.cases),
        "criteria_total": sum(len(item.acceptance_criteria) for item in corpus.cases),
        "criteria_failed": failures,
        "failed_cases": failed_cases,
        "weighted_score": round(passed_weight / total_weight, 4) if total_weight else 0.0,
        "external_calls_made": baseline.external_calls_made,
        "total_external_cost_usd": round(
            sum(item.metrics.external_cost_usd for item in baseline.observations), 6
        ),
        "quality_claim": "simulation-baseline-only",
    }


def candidate_receipt(
    corpus: AcceptanceCorpus,
    baseline: AcceptanceBaseline,
    candidate: AcceptanceCandidate,
    human_review: CandidateHumanReview | None = None,
) -> dict[str, object]:
    _validate_baseline_contract(corpus, baseline)
    _validate_candidate_contract(corpus, candidate)
    cases = {item.case_id: item for item in corpus.cases}
    if candidate.human_reviewed and human_review is None:
        raise ValueError("human_reviewed erfordert einen vollständig validierten Human-Review-Beleg")
    if human_review is not None:
        _validate_human_review(corpus, candidate, human_review, require_completed=True)
    human_verdicts = {
        (review_case.case.case_id, decision.criterion_id): decision.verdict
        for review_case in (human_review.cases if human_review is not None else [])
        for decision in review_case.decisions
    }
    total_weight = sum(
        criterion.weight for case in corpus.cases for criterion in case.acceptance_criteria
    )

    def weighted_passed(observations: list[BaselineObservation] | list[CandidateObservation]) -> int:
        return sum(
            {
                criterion.criterion_id: criterion.weight
                for criterion in cases[observation.case_id].acceptance_criteria
            }[criterion_id]
            for observation in observations
            for criterion_id in observation.criteria_passed
        )

    baseline_score = round(weighted_passed(baseline.observations) / total_weight, 4)
    effective_passed: dict[str, set[str]] = {}
    effective_failed: dict[str, set[str]] = {}
    for observation in candidate.observations:
        if human_review is None:
            effective_passed[observation.case_id] = set(observation.criteria_passed)
            effective_failed[observation.case_id] = set(observation.criteria_failed)
            continue
        criterion_types = {
            item.criterion_id: item.evaluator
            for item in cases[observation.case_id].acceptance_criteria
        }
        passed = {
            item for item in observation.criteria_passed if criterion_types[item] == "contract"
        }
        failed = {
            item for item in observation.criteria_failed if criterion_types[item] == "contract"
        }
        for criterion_id, evaluator in criterion_types.items():
            if evaluator != "human":
                continue
            verdict = human_verdicts.get((observation.case_id, criterion_id))
            if verdict == "passed":
                passed.add(criterion_id)
            else:
                failed.add(criterion_id)
        effective_passed[observation.case_id] = passed
        effective_failed[observation.case_id] = failed
    candidate_passed_weight = sum(
        criterion.weight
        for case in corpus.cases
        for criterion in case.acceptance_criteria
        if criterion.criterion_id in effective_passed[case.case_id]
    )
    candidate_score = round(candidate_passed_weight / total_weight, 4)
    successful_cases = sum(not item for item in effective_failed.values())
    total_cost = round(
        sum(item.metrics.external_cost_usd for item in candidate.observations), 6
    )
    pilot_call_limit_met = all(item.metrics.llm_calls <= 5 for item in candidate.observations)
    return {
        "receipt_version": 1,
        "candidate_id": candidate.candidate_id,
        "baseline_id": baseline.baseline_id,
        "corpus_id": corpus.corpus_id,
        "corpus_sha256": candidate.corpus_sha256,
        "case_count": len(corpus.cases),
        "criteria_total": sum(len(item.acceptance_criteria) for item in corpus.cases),
        "criteria_failed": sum(len(item) for item in effective_failed.values()),
        "successful_cases": successful_cases,
        "baseline_weighted_score": baseline_score,
        "candidate_weighted_score": candidate_score,
        "score_delta": round(candidate_score - baseline_score, 4),
        "improves_baseline": candidate_score > baseline_score,
        "external_calls_made": any(
            item.external_calls_made for item in candidate.observations
        ),
        "total_external_cost_usd": total_cost,
        "cost_per_successful_case_usd": (
            round(total_cost / successful_cases, 6) if successful_cases else None
        ),
        "pilot_call_limit_met": pilot_call_limit_met,
        "pilot_eligible": False,
        "source_revision": candidate.source_revision,
        "human_reviewed": human_review is not None,
        "human_review_id": human_review.review_id if human_review is not None else None,
        "human_reviewed_at": (
            human_review.reviewed_at.isoformat() if human_review is not None else None
        ),
        "artifact_sha256": {
            item.case_id: item.output_sha256 for item in candidate.observations
        },
        "quality_claim": (
            "simulation-candidate-human-reviewed"
            if human_review is not None
            else "simulation-candidate-only"
        ),
    }


def run_mock_candidate(
    corpus: AcceptanceCorpus,
    *,
    source_revision: str,
) -> AcceptanceCandidate:
    """Execute the current TankAI loop without providers and retain its actual artifacts."""
    from tankai.core.llm import MockLLM
    from tankai.core.loop import TankAI

    observations: list[CandidateObservation] = []
    for case in corpus.cases:
        goal = case.goal
        if case.input_artifacts:
            artifacts = "\n\n".join(item.content for item in case.input_artifacts)
            goal = f"{goal}\n\nBereitgestellte Artefakte:\n{artifacts}"
        tank = TankAI(
            llm=MockLLM(),
            critic_llm=MockLLM(),
            max_retries=0,
            max_llm_calls_per_run=min(case.limits.max_llm_calls, 5),
            verbose=False,
            enable_tools=False,
            run_store_path=None,
            verified_single_step=True,
        )
        result = tank.run(goal, case.definition_of_done, case.constraints)
        output = result.final_answer
        passed: list[str] = []
        failed: list[str] = []
        failures: list[str] = []
        for criterion in case.acceptance_criteria:
            if criterion.criterion_id == "run_contract":
                passed.append(criterion.criterion_id)
            elif criterion.criterion_id == "internal_data_boundary":
                passed.append(criterion.criterion_id)
            else:
                failed.append(criterion.criterion_id)
        if failed:
            failures.append(
                "Die providerfreie Mock-Ausgabe belegt die fachlichen Kriterien nicht; "
                "sie bleibt eine Simulation ohne unabhängige menschliche Qualitätsfreigabe."
            )
        if not result.verification_passed:
            failures.append(
                "Die finale interne Prüfung wurde nicht bestanden; das Ergebnis ist nicht release-ready."
            )
        observations.append(
            CandidateObservation(
                case_id=case.case_id,
                result_status="simulated",
                output=output,
                output_sha256=hashlib.sha256(output.encode("utf-8")).hexdigest(),
                criteria_passed=passed,
                criteria_failed=failed,
                failures=failures,
                external_calls_made=False,
                metrics=BaselineMetrics(
                    duration_seconds=result.duration_seconds,
                    llm_calls=tank.llm_call_budget.snapshot()["used"],
                    external_cost_usd=0,
                    output_chars=len(output),
                    source_count=len(result.source_ids),
                    receipt_count=len(result.receipts),
                ),
            )
        )
    return AcceptanceCandidate(
        schema_version=1,
        candidate_id="tankai-current-mock-candidate-v1",
        corpus_id=corpus.corpus_id,
        corpus_sha256=corpus_digest(corpus),
        observed_at=datetime.now(timezone.utc),
        execution_mode="simulation",
        system_identity=(
            "TankAI current MockLLM, tools disabled, max_retries=0, "
            "verified_single_step=true"
        ),
        source_revision=source_revision,
        human_reviewed=False,
        observations=observations,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TankAI cooperative acceptance evaluation")
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--human-review", type=Path)
    parser.add_argument("--write-human-review-template", type=Path)
    parser.add_argument("--run-mock-candidate", action="store_true")
    parser.add_argument("--write-candidate", type=Path)
    parser.add_argument("--source-revision")
    args = parser.parse_args(argv)
    corpus = load_corpus(args.corpus)
    baseline = load_baseline(args.baseline, corpus)
    if args.run_mock_candidate and args.candidate:
        parser.error("--candidate und --run-mock-candidate schließen sich aus")
    if (args.human_review or args.write_human_review_template) and not args.candidate:
        parser.error("Human-Review-Optionen erfordern --candidate")
    if args.human_review and args.write_human_review_template:
        parser.error("--human-review und --write-human-review-template schließen sich aus")
    if args.write_candidate and not args.run_mock_candidate:
        parser.error("--write-candidate erfordert --run-mock-candidate")
    if args.run_mock_candidate and not args.source_revision:
        parser.error("--run-mock-candidate erfordert --source-revision")
    if args.run_mock_candidate:
        candidate = run_mock_candidate(corpus, source_revision=args.source_revision)
        if args.write_candidate:
            args.write_candidate.write_text(
                json.dumps(candidate.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        receipt = candidate_receipt(corpus, baseline, candidate)
    elif args.candidate:
        candidate = load_candidate(args.candidate, corpus)
        if args.write_human_review_template:
            review_template = human_review_template(corpus, candidate)
            args.write_human_review_template.write_text(
                json.dumps(review_template.model_dump(mode="json"), ensure_ascii=False, indent=2)
                + "\n",
                encoding="utf-8",
            )
        human_review = (
            load_human_review(args.human_review, corpus, candidate)
            if args.human_review
            else None
        )
        receipt = candidate_receipt(corpus, baseline, candidate, human_review)
    else:
        receipt = baseline_receipt(corpus, baseline)
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
