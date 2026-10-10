"""Frozen cooperative-AI corpus and baseline receipt validation.

This module deliberately does not call a model or provider.  It binds the task,
budget and scoring contract before candidate work begins and verifies the
checked-in baseline observation against that immutable corpus.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import date, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CORPUS = ROOT / "evaluation" / "cooperative_acceptance_v1.json"
DEFAULT_BASELINE = ROOT / "evaluation" / "cooperative_acceptance_v1_baseline.json"
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


def load_baseline(
    path: Path = DEFAULT_BASELINE,
    corpus: AcceptanceCorpus | None = None,
) -> AcceptanceBaseline:
    corpus = corpus or load_corpus()
    baseline = AcceptanceBaseline.model_validate(_read_json(path))
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
        observed_ids = set(observation.criteria_passed) | set(observation.criteria_failed)
        if observed_ids != criterion_ids:
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
    return baseline


def baseline_receipt(
    corpus: AcceptanceCorpus,
    baseline: AcceptanceBaseline,
) -> dict[str, object]:
    if baseline.corpus_id != corpus.corpus_id or baseline.corpus_sha256 != corpus_digest(corpus):
        raise ValueError("Baseline ist nicht an diesen Korpus gebunden")
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TankAI cooperative acceptance baseline")
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    args = parser.parse_args(argv)
    corpus = load_corpus(args.corpus)
    baseline = load_baseline(args.baseline, corpus)
    print(json.dumps(baseline_receipt(corpus, baseline), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
