"""Prepare reproducible own-model evaluations without inference or training.

The public development pack is deliberately not a hidden holdout. A prepared
run has no scores; it cannot be mistaken for completed model evidence.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


DEFAULT_PACK = Path(__file__).resolve().parents[2] / "evaluation/model_development_v1.json"
CATEGORIES = {"reasoning", "code", "language", "grounding", "tool_safety", "recovery"}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ModelCase(StrictModel):
    case_id: str = Field(pattern=r"^model-dev-v1-[a-z0-9-]+$")
    category: Literal["reasoning", "code", "language", "grounding", "tool_safety", "recovery"]
    prompt: str = Field(min_length=30, max_length=30_000)
    rubric: list[str] = Field(min_length=2, max_length=8)
    max_output_chars: int = Field(ge=100, le=20_000)

    @model_validator(mode="after")
    def distinct_nonempty_rubric(self) -> "ModelCase":
        if (len(set(self.rubric)) != len(self.rubric)
                or any(not 10 <= len(item.strip()) <= 1000 for item in self.rubric)):
            raise ValueError("Each case needs distinct, substantive review criteria")
        return self


class ModelDevelopmentPack(StrictModel):
    schema_version: Literal[1]
    pack_id: Literal["tankai-model-development-v1"]
    created_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    split: Literal["public_development"]
    training_allowed: Literal[False]
    provenance: Literal["original_synthetic_tasks_no_private_conversations"]
    cases: list[ModelCase] = Field(min_length=12, max_length=120)

    @model_validator(mode="after")
    def complete_pack(self) -> "ModelDevelopmentPack":
        date.fromisoformat(self.created_on)
        ids = [case.case_id for case in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate model evaluation case")
        if {case.category for case in self.cases} != CATEGORIES:
            raise ValueError("Model evaluation must cover all six development categories")
        if any(sum(case.category == category for case in self.cases) < 2 for category in CATEGORIES):
            raise ValueError("Each category needs at least two development cases")
        return self


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate evaluation JSON key")
        result[key] = value
    return result


def load_pack(path: Path = DEFAULT_PACK) -> ModelDevelopmentPack:
    if path.stat().st_size > 1_000_000:
        raise ValueError("Evaluation pack exceeds size limit")
    return ModelDevelopmentPack.model_validate(json.loads(
        path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object))


def pack_digest(pack: ModelDevelopmentPack) -> str:
    # Revalidate public objects as well as file input before producing evidence.
    valid = ModelDevelopmentPack.model_validate(pack.model_dump())
    canonical = json.dumps(valid.model_dump(), sort_keys=True, ensure_ascii=False,
                           separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def manifest(pack: ModelDevelopmentPack) -> dict:
    digest = pack_digest(pack)
    return {"pack_id": pack.pack_id, "pack_sha256": digest, "split": pack.split,
            "training_allowed": False, "case_count": len(pack.cases),
            "categories": sorted({case.category for case in pack.cases}),
            "inference_requested": False, "training_started": False,
            "quality_result": None, "frontier_parity_established": False,
            "missing_gates": ["sealed_independent_holdout", "licensed_base_checkpoint",
                              "approved_compute_budget", "measured_reference_and_candidate_runs"]}


def prepare_run(pack: ModelDevelopmentPack, *, model_identity: str,
                track: Literal["raw_model", "tankai_system"]) -> dict:
    if (not isinstance(model_identity, str) or not 3 <= len(model_identity.strip()) <= 300
            or any(ord(c) < 32 for c in model_identity)):
        raise ValueError("A concrete model/checkpoint identity is required")
    if track not in {"raw_model", "tankai_system"}:
        raise ValueError("Unknown evaluation track")
    return {**manifest(pack), "status": "prepared_not_executed",
            "prepared_at": datetime.now(timezone.utc).isoformat(),
            "model_identity": model_identity, "track": track,
            "tool_policy": "none" if track == "raw_model" else "must_be_frozen_before_execution",
            "budget_authorized": False, "observations": [
                {"case_id": case.case_id, "status": "pending", "output": None,
                 "output_sha256": None, "duration_seconds": None, "external_cost_usd": None,
                 "input_tokens": None, "output_tokens": None,
                 "criteria": [{"description": criterion, "verdict": "pending", "evidence": None}
                              for criterion in case.rubric]}
                for case in pack.cases]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("manifest", "prepare-run"))
    parser.add_argument("--pack", type=Path, default=DEFAULT_PACK)
    parser.add_argument("--model-identity")
    parser.add_argument("--track", choices=("raw_model", "tankai_system"), default="raw_model")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    pack = load_pack(args.pack)
    if args.action == "prepare-run":
        if not args.model_identity:
            parser.error("prepare-run requires --model-identity")
        result = prepare_run(pack, model_identity=args.model_identity, track=args.track)
    else:
        result = manifest(pack)
    encoded = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        # Never replace a previous observation or receipt by accident.
        with args.output.open("x", encoding="utf-8") as handle:
            handle.write(encoded)
    else:
        print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
