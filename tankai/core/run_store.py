"""Einfache Persistenz abgeschlossener Runs als JSONL."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from .models import RunResult, utcnow


class RunStore:
    def __init__(self, path: str | Path = "tankai_runs.jsonl") -> None:
        self.path = Path(path)

    def append(self, result: RunResult, goal_description: str) -> None:
        record = {
            "ts": utcnow().isoformat(),
            "goal": goal_description,
            "goal_id": result.goal_id,
            "status": result.status.value if hasattr(result.status, "value") else str(result.status),
            "execution_mode": result.execution_mode,
            "main_llm_identity": result.main_llm_identity,
            "critic_llm_identity": result.critic_llm_identity,
            "critic_independent": result.critic_independent,
            "verification_passed": result.verification_passed,
            "release_ready": result.release_ready,
            "plan_gate_passed": result.plan_gate_passed,
            "failed_step_ids": result.failed_step_ids,
            "web_research_provider": result.web_research_provider,
            "source_ids": result.source_ids,
            "source_urls": result.source_urls,
            "definition_of_done": result.definition_of_done,
            "final_answer": result.final_answer,
            "duration_seconds": result.duration_seconds,
            "receipts": len(result.receipts),
            "plan_steps": len(result.plan.steps) if result.plan else 0,
            "rationale": result.plan.rationale if result.plan else "",
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as e:
            print(f"[RunStore] Warnung: {e}")

    def list_recent(self, n: int = 20) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out = []
        for line in lines[-n:]:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return list(reversed(out))
