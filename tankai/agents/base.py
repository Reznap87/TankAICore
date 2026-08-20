"""
Basis-Klasse für alle Agenten.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from ..core.llm import BaseLLM
from ..core.models import Receipt, Role


class BaseAgent(ABC):
    def __init__(self, llm: BaseLLM, name: str, role: Role) -> None:
        self.llm = llm
        self.name = name
        self.role = role

    def _create_receipt(
        self,
        action: str,
        input_summary: str,
        output_summary: str,
        success: bool = True,
        details: dict[str, Any] | None = None,
    ) -> Receipt:
        return Receipt(
            action=action,
            actor=self.role,
            input_summary=input_summary[:300],
            output_summary=output_summary[:300],
            success=success,
            details=details or {},
        )

    @abstractmethod
    def run(self, *args: Any, **kwargs: Any) -> Any:
        ...
