"""
Synthesizer: führt alle geprüften Ergebnisse zu einer finalen Antwort zusammen.
"""

from __future__ import annotations

from typing import Any

from ..core.llm import BaseLLM
from ..core.models import Goal, Role
from .base import BaseAgent


class Synthesizer(BaseAgent):
    def __init__(self, llm: BaseLLM) -> None:
        super().__init__(llm, name="Synthesizer", role=Role.SYNTHESIZER)

    def run(
        self,
        goal: Goal,
        step_results: list[tuple[str, str]],  # (step_description, result)
        critiques_summary: str = "",
    ) -> tuple[str, Any]:
        """
        Erzeugt die finale Antwort.
        Gibt (final_answer, receipt) zurück.
        """
        system = (
            "Du bist der Synthesizer. Deine Aufgabe ist es, aus den "
            "geprüften Zwischenergebnissen eine einzige klare, "
            "überprüfbare Endantwort zu formulieren, die die "
            "Definition of Done erfüllt."
        )

        results_text = "\n\n".join(
            f"### Schritt: {desc}\n{res}" for desc, res in step_results
        )

        prompt = f"""Erstelle die finale Antwort.

Ziel: {goal.description}
Definition of Done: {goal.definition_of_done}

Geprüfte Zwischenergebnisse:
{results_text}

Critic-Zusammenfassung:
{critiques_summary or "Keine kritischen Issues"}

Formuliere eine klare, vollständige Endantwort.
"""

        final = self.llm.complete(prompt, system=system)

        receipt = self._create_receipt(
            action="synthesize",
            input_summary=f"Goal: {goal.description[:100]}",
            output_summary=final[:200],
            details={"num_steps_used": len(step_results)},
        )
        return final, receipt
