"""
Planner-Agent: zerlegt das Ziel in überprüfbare Schritte.
Nutzt optional Procedural Memory (erfolgreiche Plan-Muster).
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from ..core.llm import BaseLLM
from ..core.models import Goal, Plan, PlanStep, Role
from .base import BaseAgent


class Planner(BaseAgent):
    def __init__(self, llm: BaseLLM) -> None:
        super().__init__(llm, name="Planner", role=Role.PLANNER)

    def run(
        self,
        goal: Goal,
        procedural_context: Optional[str] = None,
        critic_feedback: Optional[str] = None,
    ) -> tuple[Plan, Any]:
        """
        Erstellt einen Plan für das gegebene Ziel.
        procedural_context: formatierte erfolgreiche Plan-Muster aus dem LTM.
        Gibt (Plan, Receipt) zurück.
        """
        system = (
            "Du bist der Planner in einem Multi-Agenten-System. "
            "Du zerlegst Ziele in 2–5 klare, überprüfbare Schritte. "
            "Jeder Schritt braucht einen specialist_type "
            "(research, analysis, code, writing, other) "
            "und eine expected_output-Beschreibung. "
            "Wenn erfolgreiche Plan-Muster aus dem Memory vorliegen, "
            "nutze sie als starke Vorlage und passe sie nur so weit an, "
            "wie das aktuelle Ziel es erfordert. "
            "Antworte ausschließlich mit validem JSON."
        )

        memory_block = ""
        if procedural_context:
            memory_block = f"""
Erfolgreiche Plan-Muster aus dem Procedural Memory (ähnliche frühere Ziele):
{procedural_context}

Übernimm sinnvolle Strukturen und specialist_types aus diesen Mustern,
wenn sie zum aktuellen Ziel passen. Begründe in der rationale,
ob und wie du ein Muster wiederverwendet hast.
"""

        feedback_block = ""
        if critic_feedback:
            feedback_block = f"""
Der vorherige Plan wurde vom Critic abgelehnt. Repariere diese Punkte zwingend:
{critic_feedback}
"""

        prompt = f"""Erstelle einen Plan für folgendes Ziel:

Ziel: {goal.description}

Definition of Done: {goal.definition_of_done}

Constraints: {goal.constraints or "keine"}
{memory_block}
{feedback_block}
Antworte im JSON-Format:
{{
  "rationale": "...",
  "reused_pattern": true/false,
  "steps": [
    {{
      "description": "...",
      "specialist_type": "research|analysis|code|writing|other",
      "expected_output": "..."
    }}
  ]
}}
"""

        raw = self.llm.complete(prompt, system=system)
        plan = self._parse_plan(raw, goal.id)

        reused = False
        try:
            match = re.search(r"\{[\s\S]*\}", raw)
            data = json.loads(match.group(0) if match else raw)
            reused = bool(data.get("reused_pattern", False))
        except Exception:
            pass

        receipt = self._create_receipt(
            action="create_plan",
            input_summary=goal.description,
            output_summary=f"Plan mit {len(plan.steps)} Schritten erstellt"
            + (" (Pattern wiederverwendet)" if reused else ""),
            details={
                "plan_id": plan.id,
                "version": plan.version,
                "reused_pattern": reused,
                "had_procedural_context": bool(procedural_context),
                "had_critic_feedback": bool(critic_feedback),
            },
        )
        return plan, receipt

    def _parse_plan(self, raw: str, goal_id: str) -> Plan:
        """Versucht JSON aus der LLM-Antwort zu extrahieren."""
        try:
            match = re.search(r"\{[\s\S]*\}", raw)
            if match:
                data = json.loads(match.group(0))
            else:
                data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            data = {
                "rationale": "Fallback-Plan wegen Parse-Fehler",
                "steps": [
                    {
                        "description": "Informationen zum Ziel sammeln",
                        "specialist_type": "research",
                        "expected_output": "Zusammenfassung der relevanten Fakten",
                    },
                    {
                        "description": "Ergebnis formulieren",
                        "specialist_type": "writing",
                        "expected_output": "Klare Endantwort",
                    },
                ],
            }

        allowed_types = {"research", "analysis", "code", "writing", "other"}
        raw_steps = data.get("steps", [])
        if not isinstance(raw_steps, list):
            raw_steps = []
        steps = []
        for item in raw_steps[:5]:
            if not isinstance(item, dict):
                continue
            description = str(item.get("description") or "").strip()
            expected = str(item.get("expected_output") or "").strip()
            specialist_type = str(item.get("specialist_type") or "other").lower().strip()
            if specialist_type not in allowed_types:
                specialist_type = "other"
            if not description:
                continue
            steps.append(PlanStep(
                description=description[:2000],
                specialist_type=specialist_type,
                expected_output=expected[:2000],
            ))
        if not steps:
            steps = [
                PlanStep(
                    description="Ziel analysieren und überprüfbare Antwort erstellen",
                    specialist_type="analysis",
                    expected_output="Klare Antwort gegen die Definition of Done",
                )
            ]

        rationale = str(data.get("rationale") or "")[:4000]
        if data.get("reused_pattern") is True:
            rationale = f"[Pattern wiederverwendet] {rationale}"

        return Plan(
            goal_id=goal_id,
            steps=steps,
            rationale=rationale,
        )
