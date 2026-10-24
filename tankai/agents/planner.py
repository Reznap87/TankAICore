"""
Planner-Agent: zerlegt das Ziel in überprüfbare Schritte.
Nutzt optional Procedural Memory (erfolgreiche Plan-Muster).
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from ..core.llm import BaseLLM
from ..core.models import ApproachOption, Goal, GoalAnalysis, Plan, PlanStep, Role
from .base import BaseAgent


class Planner(BaseAgent):
    def __init__(self, llm: BaseLLM) -> None:
        super().__init__(llm, name="Planner", role=Role.PLANNER)

    def run(
        self,
        goal: Goal,
        procedural_context: Optional[str] = None,
        critic_feedback: Optional[str] = None,
        max_steps: int = 5,
    ) -> tuple[Plan, Any]:
        """
        Erstellt einen Plan für das gegebene Ziel.
        procedural_context: formatierte erfolgreiche Plan-Muster aus dem LTM.
        Gibt (Plan, Receipt) zurück.
        """
        if max_steps < 1 or max_steps > 5:
            raise ValueError("max_steps muss zwischen 1 und 5 liegen")
        step_instruction = (
            "genau einen klaren, überprüfbaren Schritt"
            if max_steps == 1
            else f"2–{max_steps} klare, überprüfbare Schritte"
        )
        system = (
            "Du bist der Planner in einem Multi-Agenten-System. "
            f"Du zerlegst Ziele in {step_instruction}. "
            "Du klärst im selben Aufruf zuerst gewünschtes Ergebnis und ausdrücklich "
            "verlangtes Vorgehen. Nur bei einer nicht-trivialen Gestaltungsentscheidung "
            "vergleichst du zwei bis drei wirklich unterschiedliche Optionen und benennst "
            "entscheidende Unsicherheit, Empfehlung und kleinsten aussagekräftigen Versuch. "
            "Bei direkten oder trivialen Aufgaben erzeugst du keine künstlichen Alternativen. "
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
Maximale Anzahl Planschritte: {max_steps}
{memory_block}
{feedback_block}
Antworte im JSON-Format:
{{
  "rationale": "...",
  "reused_pattern": true/false,
  "goal_analysis": {{
    "desired_outcome": "gewünschtes Ergebnis, nicht bloß die Tätigkeit",
    "analysis_status": "direct|workshop",
    "requested_method": "ausdrücklich verlangtes Vorgehen oder null",
    "alternatives_warranted": true/false,
    "alternatives_reason": "warum ein Vergleich nützt oder bewusst entfällt",
    "options": [
      {{
        "option_id": "option-a",
        "title": "...",
        "approach": "...",
        "benefit": "...",
        "tradeoff": "..."
      }}
    ],
    "critical_uncertainty": "entscheidende unbelegte Annahme oder null",
    "recommended_option_id": "option-a oder null",
    "smallest_experiment": "kleinster messbarer Versuch oder null"
  }},
  "steps": [
    {{
      "description": "...",
      "specialist_type": "research|analysis|code|writing|other",
      "expected_output": "..."
    }}
  ]
}}

Wenn alternatives_warranted false ist, müssen options leer und critical_uncertainty,
recommended_option_id sowie smallest_experiment null sein.
"""

        raw = self.llm.complete(prompt, system=system)
        plan = self._parse_plan(raw, goal, max_steps=max_steps)

        reused = False
        try:
            data = self._decode_plan(raw)
            reused = (
                not plan.parse_fallback
                and isinstance(data, dict)
                and data.get("reused_pattern") is True
            )
        except (json.JSONDecodeError, TypeError):
            pass

        receipt = self._create_receipt(
            action="create_plan",
            input_summary=goal.description,
            output_summary=(
                f"Planstruktur ungültig: {plan.parse_error}; Ausführung blockiert"
                if plan.parse_fallback
                else f"Plan mit {len(plan.steps)} Schritten erstellt"
                + (" (Pattern wiederverwendet)" if reused else "")
            ),
            success=not plan.parse_fallback,
            details={
                "plan_id": plan.id,
                "version": plan.version,
                "reused_pattern": reused,
                "had_procedural_context": bool(procedural_context),
                "had_critic_feedback": bool(critic_feedback),
                "goal_analysis_present": plan.goal_analysis is not None,
                "goal_analysis_status": (
                    plan.goal_analysis.analysis_status if plan.goal_analysis else "missing"
                ),
                "alternatives_warranted": bool(
                    plan.goal_analysis and plan.goal_analysis.alternatives_warranted
                ),
                "alternative_count": len(plan.goal_analysis.options) if plan.goal_analysis else 0,
                "max_steps": max_steps,
                "parse_fallback": plan.parse_fallback,
                "parse_error": plan.parse_error,
                "executable_plan": not plan.parse_fallback and bool(plan.steps),
            },
        )
        return plan, receipt

    def _parse_plan(self, raw: str, goal: Goal, *, max_steps: int = 5) -> Plan:
        """Reject malformed plan structure without inventing executable work."""
        def invalid(reason: str) -> Plan:
            return Plan(
                goal_id=goal.id,
                steps=[],
                rationale="Planstruktur ungültig; es wurden keine Ersatzschritte erzeugt.",
                parse_fallback=True,
                parse_error=reason,
            )

        try:
            data = self._decode_plan(raw)
        except (json.JSONDecodeError, TypeError):
            return invalid("invalid_json")
        if not isinstance(data, dict):
            return invalid("invalid_root")

        allowed_types = {"research", "analysis", "code", "writing", "other"}
        raw_steps = data.get("steps", [])
        if not isinstance(raw_steps, list) or not raw_steps:
            return invalid("invalid_steps")
        if len(raw_steps) > max_steps:
            return invalid("step_limit_exceeded")
        steps = []
        for item in raw_steps:
            if not isinstance(item, dict):
                return invalid("invalid_step")
            description = item.get("description")
            expected = item.get("expected_output")
            specialist_type = item.get("specialist_type")
            if (
                not isinstance(description, str) or not description.strip()
                or len(description) > 2000
                or not isinstance(expected, str) or not expected.strip() or len(expected) > 2000
                or not isinstance(specialist_type, str)
                or specialist_type.strip().lower() not in allowed_types
            ):
                return invalid("invalid_step")
            steps.append(PlanStep(
                description=description.strip(),
                specialist_type=specialist_type.strip().lower(),
                expected_output=expected.strip(),
            ))

        rationale = str(data.get("rationale") or "")[:4000]
        if data.get("reused_pattern") is True:
            rationale = f"[Pattern wiederverwendet] {rationale}"

        goal_analysis = self._parse_goal_analysis(data.get("goal_analysis"), goal)

        return Plan(
            goal_id=goal.id,
            steps=steps,
            rationale=rationale,
            goal_analysis=goal_analysis,
        )

    @staticmethod
    def _decode_plan(raw: str) -> object:
        """Accept JSON or one complete JSON fence, never an embedded partial object."""
        if not isinstance(raw, str):
            raise TypeError("Planner-Antwort muss Text sein")
        fenced = re.fullmatch(r"\s*```(?:json)?\s*([\s\S]*?)\s*```\s*", raw)
        return json.loads(fenced.group(1) if fenced else raw)

    @staticmethod
    def _parse_goal_analysis(raw: object, goal: Goal) -> GoalAnalysis:
        """Parse the bounded P2 contract; invalid workshops safely degrade to direct work."""
        fallback = GoalAnalysis(
            desired_outcome=goal.description[:2000],
            analysis_status="fallback",
            requested_method=None,
            alternatives_warranted=False,
            alternatives_reason=(
                "Kein vollständiges strukturiertes Ideenlabor geliefert; "
                "das ausdrückliche Ziel wird direkt bearbeitet."
            ),
        )
        if not isinstance(raw, dict):
            return fallback
        try:
            options_raw = raw.get("options", [])
            if (
                not isinstance(options_raw, list)
                or len(options_raw) > 3
                or any(not isinstance(item, dict) for item in options_raw)
            ):
                return fallback
            options = [
                ApproachOption.model_validate(item)
                for item in options_raw
            ]
            return GoalAnalysis(
                desired_outcome=str(raw.get("desired_outcome") or goal.description)[:2000],
                analysis_status=(
                    "workshop" if raw.get("alternatives_warranted") is True else "direct"
                ),
                requested_method=(
                    str(raw["requested_method"])[:1000]
                    if raw.get("requested_method") is not None
                    else None
                ),
                alternatives_warranted=raw.get("alternatives_warranted") is True,
                alternatives_reason=str(raw.get("alternatives_reason") or "Nicht begründet")[:1000],
                options=options,
                critical_uncertainty=(
                    str(raw["critical_uncertainty"])[:1000]
                    if raw.get("critical_uncertainty") is not None
                    else None
                ),
                recommended_option_id=(
                    str(raw["recommended_option_id"])[:32]
                    if raw.get("recommended_option_id") is not None
                    else None
                ),
                smallest_experiment=(
                    str(raw["smallest_experiment"])[:1000]
                    if raw.get("smallest_experiment") is not None
                    else None
                ),
            )
        except (TypeError, ValueError):
            return fallback
