"""
Tool-Use für TankAI-Specialists.
"""

from __future__ import annotations

import ast
import hashlib
import json
import math
import operator
import re
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Optional

from .web_research import WebResearchTool, build_web_research_tool_from_env


class BaseTool(ABC):
    name: str
    description: str

    @abstractmethod
    def run(self, **kwargs: Any) -> str:
        ...

    def schema(self) -> dict:
        return {"name": self.name, "description": self.description}


class CalculatorTool(BaseTool):
    name = "calculator"
    description = "Berechnet mathematische Ausdrücke (z.B. '2+2*3', 'sqrt(16)')."

    _ops = {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.Mult: operator.mul,
        ast.Div: operator.truediv,
        ast.Pow: operator.pow,
        ast.USub: operator.neg,
        ast.Mod: operator.mod,
    }
    _funcs = {
        "sqrt": math.sqrt,
        "sin": math.sin,
        "cos": math.cos,
        "log": math.log,
        "abs": abs,
        "round": round,
    }

    def run(self, expression: str = "", **kwargs: Any) -> str:
        expression = expression or kwargs.get("expr", "")
        try:
            tree = ast.parse(expression, mode="eval")
            result = self._eval(tree.body)
            return f"Ergebnis: {result}"
        except Exception as e:
            return f"Rechenfehler: {e}"

    def _eval(self, node):
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.BinOp):
            return self._ops[type(node.op)](self._eval(node.left), self._eval(node.right))
        if isinstance(node, ast.UnaryOp):
            return self._ops[type(node.op)](self._eval(node.operand))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            fn = self._funcs.get(node.func.id)
            if not fn:
                raise ValueError(f"Unbekannte Funktion: {node.func.id}")
            return fn(*[self._eval(a) for a in node.args])
        if isinstance(node, ast.Name) and node.id in ("pi", "e"):
            return math.pi if node.id == "pi" else math.e
        raise ValueError(f"Nicht erlaubt: {type(node).__name__}")


class DateTimeTool(BaseTool):
    name = "datetime"
    description = "Aktuelles Datum/Uhrzeit (UTC)."

    def run(self, **kwargs: Any) -> str:
        now = datetime.now(timezone.utc)
        return f"UTC jetzt: {now.isoformat()}"


class HashTool(BaseTool):
    name = "hash"
    description = "Berechnet SHA-256 oder MD5 eines Textes. Params: text, algo=sha256|md5"

    def run(self, text: str = "", algo: str = "sha256", **kwargs: Any) -> str:
        text = text or kwargs.get("input", "")
        algo = (algo or "sha256").lower()
        data = text.encode("utf-8")
        if algo == "md5":
            return f"md5: {hashlib.md5(data).hexdigest()}"
        return f"sha256: {hashlib.sha256(data).hexdigest()}"


class TextStatsTool(BaseTool):
    name = "text_stats"
    description = "Zählt Zeichen, Wörter, Zeilen eines Textes. Param: text"

    def run(self, text: str = "", **kwargs: Any) -> str:
        text = text or kwargs.get("input", "")
        lines = text.splitlines() or [text]
        words = re.findall(r"\w+", text, flags=re.UNICODE)
        return (
            f"Zeichen: {len(text)} | Wörter: {len(words)} | "
            f"Zeilen: {len(lines)} | Ohne Leerzeichen: {len(re.sub(r'\\s', '', text))}"
        )


class JsonTool(BaseTool):
    name = "json_format"
    description = "Formatiert oder validiert JSON. Param: text"

    def run(self, text: str = "", **kwargs: Any) -> str:
        text = text or kwargs.get("input", "")
        try:
            obj = json.loads(text)
            return json.dumps(obj, ensure_ascii=False, indent=2)
        except json.JSONDecodeError as e:
            return f"Ungültiges JSON: {e}"


class MemorySearchTool(BaseTool):
    name = "memory_search"
    description = "Sucht im Langzeitgedächtnis nach relevantem Wissen. Param: query, k=3"

    def __init__(self, ltm: Any = None) -> None:
        self.ltm = ltm

    def run(self, query: str = "", k: int = 3, **kwargs: Any) -> str:
        if not self.ltm:
            return "Memory nicht verfügbar."
        try:
            k = int(k)
        except (TypeError, ValueError):
            k = 3
        hits = self.ltm.retrieve(query, k=k)
        if not hits:
            return "Keine Treffer."
        lines = [
            f"- [{h['memory_type']}|{h['score']:.2f}] {h['content'][:200]}"
            for h in hits
        ]
        return "Memory-Treffer:\n" + "\n".join(lines)


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Optional[BaseTool]:
        return self._tools.get(name)

    def list_tools(self) -> list[dict]:
        return [t.schema() for t in self._tools.values()]

    def run(self, name: str, **kwargs: Any) -> str:
        tool = self._tools.get(name)
        if not tool:
            return f"Unbekanntes Tool: {name}"
        return tool.run(**kwargs)

    def describe_for_prompt(self) -> str:
        if not self._tools:
            return "(keine Tools verfügbar)"
        lines = [f"- {t.name}: {t.description}" for t in self._tools.values()]
        return "\n".join(lines)

    def register_defaults(
        self,
        ltm: Any = None,
        *,
        enable_web_research: bool = True,
        strict_web_research: bool = False,
        web_research_tool: WebResearchTool | None = None,
    ) -> None:
        self.register(CalculatorTool())
        self.register(DateTimeTool())
        self.register(HashTool())
        self.register(TextStatsTool())
        self.register(JsonTool())
        if ltm is not None:
            self.register(MemorySearchTool(ltm))
        if enable_web_research:
            tool = web_research_tool or build_web_research_tool_from_env(
                strict=strict_web_research
            )
            if tool is not None:
                self.register(tool)

    def web_research_status(self) -> str:
        tool = self.get("web_research")
        if tool is None:
            return "disabled"
        backend = getattr(tool, "backend", None)
        provider = getattr(backend, "provider_name", "configured")
        return str(provider)
