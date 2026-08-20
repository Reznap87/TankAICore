"""
TankAI — Web Intelligence OS (Prototype)

Multi-Agenten-Kern mit:
- Commander / Planner / Specialists / Critic / Synthesizer
- PLAN → ROUTE → VERIFY → LEARN
- Langzeitgedächtnis (Episodic, Semantic, Procedural)
- Vector-Suche, Retention, Cold-Storage
- Tool-Use, parallele Specialists
- LLM-Adapter (Mock, OpenAI, Anthropic)
"""

__version__ = "0.5.0-proto"

from .core.loop import TankAI
from .core.models import Goal, Receipt, TaskStatus
from .core.llm import get_llm, MockLLM, BaseLLM, describe_llm_setup, OpenAILLM, AnthropicLLM

__all__ = [
    "TankAI",
    "Goal",
    "Receipt",
    "TaskStatus",
    "get_llm",
    "MockLLM",
    "BaseLLM",
    "OpenAILLM",
    "AnthropicLLM",
    "describe_llm_setup",
]
