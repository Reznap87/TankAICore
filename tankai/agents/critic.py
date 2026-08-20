"""
Critic-Layer: prüft Pläne und Ergebnisse auf Fehler, Lücken und unbelegte Claims.
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..core.llm import BaseLLM
from ..core.models import Critique, Goal, Plan, Role
from .base import BaseAgent


class Critic(BaseAgent):
    def __init__(self, llm: BaseLLM) -> None:
        super().__init__(llm, name="Critic", role=Role.CRITIC)

    def run(self, *args, **kwargs):
        """Erfüllt die abstrakte Schnittstelle. Bevorzugt critique_plan / critique_result nutzen."""
        raise NotImplementedError("Verwende critique_plan() oder critique_result()")

    def critique_plan(self, plan: Plan, goal: Goal) -> tuple[Critique, Any]:
        """Prüft einen Plan gegen das Ziel und die Definition of Done."""
        system = (
            "Du bist der Critic. Deine Aufgabe ist es, Pläne und Ergebnisse "
            "streng aber fair zu prüfen. Suche nach Lücken, unbelegten Claims, "
            "fehlender Ausrichtung auf die Definition of Done und unklaren Schritten. "
            "Antworte ausschließlich mit validem JSON."
        )

        prompt = f"""Prüfe den folgenden Plan:

Ziel: {goal.description}
Definition of Done: {goal.definition_of_done}
Constraints: {goal.constraints}

Plan-Rationale: {plan.rationale}
Schritte:
{self._format_steps(plan)}

Antworte im JSON-Format:
{{
  "passed": true/false,
  "score": 0.0-1.0,
  "issues": ["..."],
  "suggestions": ["..."]
}}
"""
        raw = self.llm.complete(prompt, system=system)
        critique = self._parse_critique(raw, plan.id)

        receipt = self._create_receipt(
            action="critique_plan",
            input_summary=f"Plan {plan.id}",
            output_summary=f"passed={critique.passed}, score={critique.score}",
            success=critique.passed,
            details={"critique_id": critique.id},
        )
        return critique, receipt

    def critique_result(
        self,
        result: str,
        step_description: str,
        goal: Goal,
    ) -> tuple[Critique, Any]:
        """Prüft ein Einzelergebnis eines Spezialisten."""
        system = (
            "Du bist der Critic. Prüfe, ob das Ergebnis den Auftrag erfüllt, "
            "Belege enthält und zur Definition of Done beiträgt. "
            "Antworte ausschließlich mit validem JSON."
        )

        prompt = f"""Prüfe folgendes Ergebnis:

Ziel: {goal.description}
Definition of Done: {goal.definition_of_done}
Auftrag des Schritts: {step_description}

Ergebnis:
{result}

Antworte im JSON-Format:
{{
  "passed": true/false,
  "score": 0.0-1.0,
  "issues": ["..."],
  "suggestions": ["..."]
}}
"""
        raw = self.llm.complete(prompt, system=system)
        critique = self._parse_critique(raw, "result")

        receipt = self._create_receipt(
            action="critique_result",
            input_summary=step_description[:150],
            output_summary=f"passed={critique.passed}, score={critique.score}",
            success=critique.passed,
            details={"critique_id": critique.id},
        )
        return critique, receipt

    def _format_steps(self, plan: Plan) -> str:
        lines = []
        for i, s in enumerate(plan.steps, 1):
            lines.append(
                f"{i}. [{s.specialist_type}] {s.description} "
                f"→ erwartet: {s.expected_output}"
            )
        return "\n".join(lines)

    def _parse_critique(self, raw: str, target_id: str) -> Critique:
        try:
            match = re.search(r"\{[\s\S]*\}", raw)
            data = json.loads(match.group(0) if match else raw)
        except (json.JSONDecodeError, TypeError, AttributeError):
            data = {
                "passed": False,
                "score": 0.3,
                "issues": ["Critic konnte Antwort nicht parsen"],
                "suggestions": ["Erneute Prüfung empfohlen"],
            }

        return Critique(
            target_id=target_id,
            passed=bool(data.get("passed", False)),
            issues=data.get("issues", []),
            suggestions=data.get("suggestions", []),
            score=float(data.get("score", 0.5)),
        )
