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
            "final_answer": result.final_answer,
            "duration_seconds": result.duration_seconds,
            "receipts": len(result.receipts),
            "plan_steps": len(result.plan.steps) if result.plan else 0,
            "rationale": result.plan.rationale if result.plan else "",
        }
        try:
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
