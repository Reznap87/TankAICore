"""
Synthesizer: führt alle geprüften Ergebnisse zu einer finalen Antwort zusammen.
"""

from __future__ import annotations

from typing import Any

from ..core.llm import BaseLLM
from ..core.models import Goal, GoalAnalysis, Role
from .base import BaseAgent


class Synthesizer(BaseAgent):
    def __init__(self, llm: BaseLLM) -> None:
        super().__init__(llm, name="Synthesizer", role=Role.SYNTHESIZER)

    def run(
        self,
        goal: Goal,
        step_results: list[tuple[str, str]],  # (step_description, result)
        critiques_summary: str = "",
        goal_analysis: GoalAnalysis | None = None,
    ) -> tuple[str, Any]:
        """
        Erzeugt die finale Antwort.
        Gibt (final_answer, receipt) zurück.
        """
        system = (
            "Du bist der Synthesizer. Deine Aufgabe ist es, aus den "
            "geprüften Zwischenergebnissen eine einzige klare, "
            "überprüfbare Endantwort zu formulieren, die die "
            "Definition of Done erfüllt. Vorhandene Quellen-IDs im Format "
            "[SRC-XXXXXXXX] müssen exakt erhalten bleiben; erfinde keine neuen IDs oder URLs."
        )

        results_text = "\n\n".join(
            f"### Schritt: {desc}\n{res}" for desc, res in step_results
        )

        planning_context = ""
        if goal_analysis is not None:
            options = "\n".join(
                f"- {item.option_id}: {item.title} — {item.approach} "
                f"(Nutzen: {item.benefit}; Nachteil: {item.tradeoff})"
                for item in goal_analysis.options
            ) or "- keine; direkte Bearbeitung"
            planning_context = f"""
Strukturierte Zielklärung:
- Gewünschtes Ergebnis: {goal_analysis.desired_outcome}
- Analyse-Status: {goal_analysis.analysis_status}
- Ausdrücklich verlangtes Vorgehen: {goal_analysis.requested_method or '(keines)'}
- Alternativen sinnvoll: {goal_analysis.alternatives_warranted}
- Begründung: {goal_analysis.alternatives_reason}
{options}
- Entscheidende Unsicherheit: {goal_analysis.critical_uncertainty or '(keine)'}
- Empfohlene Option: {goal_analysis.recommended_option_id or '(keine)'}
- Kleinster Versuch: {goal_analysis.smallest_experiment or '(keiner)'}
"""

        prompt = f"""Erstelle die finale Antwort.

Ziel: {goal.description}
Definition of Done: {goal.definition_of_done}
{planning_context}

Geprüfte Zwischenergebnisse:
{results_text}

Critic-Zusammenfassung:
{critiques_summary or "Keine kritischen Issues"}

Formuliere eine klare, vollständige Endantwort. Behalte die Quellenbelege unmittelbar an den zugehörigen Aussagen und führe eine kompakte Quellenliste aus den vorhandenen Zwischenergebnissen auf.
"""

        final = self.llm.complete(prompt, system=system)

        receipt = self._create_receipt(
            action="synthesize",
            input_summary=f"Goal: {goal.description[:100]}",
            output_summary=final[:200],
            details={"num_steps_used": len(step_results)},
        )
        return final, receipt
