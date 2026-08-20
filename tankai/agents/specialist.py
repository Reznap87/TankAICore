"""
Spezialisten-Agenten: führen einzelne Plan-Schritte aus.
Optional mit Tool-Use.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from ..core.llm import BaseLLM
from ..core.models import PlanStep, Role
from ..core.tools import ToolRegistry
from .base import BaseAgent


class Specialist(BaseAgent):
    def __init__(
        self,
        llm: BaseLLM,
        specialist_type: str = "general",
        tools: Optional[ToolRegistry] = None,
    ) -> None:
        super().__init__(llm, name=f"Specialist[{specialist_type}]", role=Role.SPECIALIST)
        self.specialist_type = specialist_type
        self.tools = tools

    def run(self, step: PlanStep, context: str = "") -> tuple[str, Any]:
        system = self._system_prompt()
        tools_block = ""
        if self.tools and self.tools.list_tools():
            tools_block = (
                "\nVerfügbare Tools (bei Bedarf im Format "
                "TOOL:name{param=value} aufrufen):\n"
                + self.tools.describe_for_prompt()
            )

        prompt = f"""Führe folgenden Auftrag aus:

Typ: {step.specialist_type}
Auftrag: {step.description}
Erwartetes Ergebnis: {step.expected_output}

Kontext aus vorherigen Schritten / Memory:
{context or "(kein vorheriger Kontext)"}
{tools_block}

Liefere eine klare, nutzbare Antwort.
"""

        result = self.llm.complete(prompt, system=system)

        # Einfaches Tool-Parsing: TOOL:name{key=val, ...}
        if self.tools:
            result = self._resolve_tools(result)

        receipt = self._create_receipt(
            action=f"execute_step:{step.specialist_type}",
            input_summary=step.description,
            output_summary=result[:200],
            details={
                "step_id": step.id,
                "specialist_type": step.specialist_type,
                "tools_available": bool(self.tools and self.tools.list_tools()),
            },
        )
        return result, receipt

    def _resolve_tools(self, text: str) -> str:
        """Ersetzt TOOL:name{...} Aufrufe durch Tool-Ergebnisse."""
        pattern = re.compile(r"TOOL:(\w+)\{([^}]*)\}")

        def replacer(match: re.Match) -> str:
            name = match.group(1)
            params_raw = match.group(2)
            kwargs: dict[str, str] = {}
            for part in params_raw.split(","):
                part = part.strip()
                if "=" in part:
                    k, v = part.split("=", 1)
                    kwargs[k.strip()] = v.strip().strip("'\"")
            out = self.tools.run(name, **kwargs)
            return f"[Tool:{name} → {out}]"

        return pattern.sub(replacer, text)

    def _system_prompt(self) -> str:
        base = "Du bist ein spezialisierter Agent in einem Multi-Agenten-System. "
        mapping = {
            "research": base + "Du recherchierst und sammelst Fakten. Nenne Quellen, wo möglich.",
            "analysis": base + "Du analysierst Informationen kritisch und strukturiert.",
            "code": base + "Du schreibst und erklärst Code. Achte auf Korrektheit und Lesbarkeit.",
            "writing": base + "Du formulierst klare, präzise und gut strukturierte Texte.",
            "other": base + "Du erledigst den dir zugewiesenen Auftrag sorgfältig.",
        }
        return mapping.get(self.specialist_type, mapping["other"])
