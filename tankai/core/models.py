"""
Kern-Datenmodelle für TankAI.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, Optional
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TaskStatus(str, Enum):
    PENDING = "pending"
    PLANNING = "planning"
    ROUTING = "routing"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"
    SIMULATED = "simulated"


class Role(str, Enum):
    COMMANDER = "commander"
    PLANNER = "planner"
    SPECIALIST = "specialist"
    CRITIC = "critic"
    SYNTHESIZER = "synthesizer"
    MEMORY = "memory"


class Goal(BaseModel):
    """Das übergeordnete Ziel, das der Commander hält."""
    id: str = Field(default_factory=lambda: str(uuid4()))
    description: str
    definition_of_done: str
    constraints: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)
    status: TaskStatus = TaskStatus.PENDING


class Message(BaseModel):
    """Eine Nachricht im System (zwischen Agenten)."""
    id: str = Field(default_factory=lambda: str(uuid4()))
    role: Role
    content: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=utcnow)
    parent_id: Optional[str] = None


class Receipt(BaseModel):
    """
    Jede Aktion erzeugt ein Receipt.
    Keine Aktion ohne Nachweis.
    """
    id: str = Field(default_factory=lambda: str(uuid4()))
    action: str
    actor: Role
    input_summary: str
    output_summary: str
    success: bool
    details: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=utcnow)
    parent_receipt_id: Optional[str] = None


class PlanStep(BaseModel):
    """Ein einzelner Schritt im Plan."""
    id: str = Field(default_factory=lambda: str(uuid4()))
    description: str
    specialist_type: str  # z.B. "research", "code", "analysis", "writing"
    expected_output: str
    status: TaskStatus = TaskStatus.PENDING
    result: Optional[str] = None
    receipts: list[str] = Field(default_factory=list)  # Receipt-IDs


class ApproachOption(BaseModel):
    """Eine begründete, überprüfbar unterschiedliche Lösungsoption."""

    option_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,31}$")
    title: str = Field(min_length=1, max_length=120)
    approach: str = Field(min_length=1, max_length=1000)
    benefit: str = Field(min_length=1, max_length=500)
    tradeoff: str = Field(min_length=1, max_length=500)


class GoalAnalysis(BaseModel):
    """Zielklärung und optionales Ideenlabor ohne zusätzlichen Modellaufruf."""

    desired_outcome: str = Field(min_length=1, max_length=2000)
    analysis_status: Literal["direct", "workshop", "fallback"] = "direct"
    requested_method: Optional[str] = Field(default=None, max_length=1000)
    alternatives_warranted: bool = False
    alternatives_reason: str = Field(min_length=1, max_length=1000)
    options: list[ApproachOption] = Field(default_factory=list, max_length=3)
    critical_uncertainty: Optional[str] = Field(default=None, max_length=1000)
    recommended_option_id: Optional[str] = Field(default=None, max_length=32)
    smallest_experiment: Optional[str] = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def validate_idea_workshop(self) -> "GoalAnalysis":
        if not self.alternatives_warranted:
            if self.analysis_status == "workshop":
                raise ValueError("Workshop-Status benötigt Alternativen")
            if self.options or any(
                (
                    self.critical_uncertainty,
                    self.recommended_option_id,
                    self.smallest_experiment,
                )
            ):
                raise ValueError("Direkte Ziele dürfen kein partielles Ideenlabor enthalten")
            return self

        if self.analysis_status != "workshop":
            raise ValueError("Ideenlabor benötigt Workshop-Status")
        if not 2 <= len(self.options) <= 3:
            raise ValueError("Ideenlabor benötigt zwei bis drei Optionen")
        option_ids = [item.option_id for item in self.options]
        if len(set(option_ids)) != len(option_ids):
            raise ValueError("Optionen benötigen eindeutige IDs")
        if self.recommended_option_id not in set(option_ids):
            raise ValueError("Empfehlung muss auf eine vorhandene Option verweisen")
        if not self.critical_uncertainty or not self.smallest_experiment:
            raise ValueError("Ideenlabor benötigt Unsicherheit und kleinsten Versuch")
        return self


class Plan(BaseModel):
    """Der vom Planner erstellte Plan."""
    id: str = Field(default_factory=lambda: str(uuid4()))
    goal_id: str
    steps: list[PlanStep]
    rationale: str
    goal_analysis: Optional[GoalAnalysis] = None
    created_at: datetime = Field(default_factory=utcnow)
    version: int = 1


class Critique(BaseModel):
    """Ergebnis der Critic-Prüfung."""
    id: str = Field(default_factory=lambda: str(uuid4()))
    target_id: str  # Plan- oder Result-ID
    passed: bool
    issues: list[str] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)
    score: float = Field(ge=0.0, le=1.0, default=0.5)
    timestamp: datetime = Field(default_factory=utcnow)


class MemoryEntry(BaseModel):
    """Ein Eintrag im Memory mit Provenance."""
    id: str = Field(default_factory=lambda: str(uuid4()))
    content: str
    source: str  # z.B. "specialist:research", "user", "critic"
    validity: str = "unknown"  # "valid", "invalid", "conflicting", "unknown"
    confidence: float = Field(ge=0.0, le=1.0, default=0.5)
    related_goal_id: Optional[str] = None
    conflicts_with: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RunResult(BaseModel):
    """Endergebnis eines kompletten Runs."""
    goal_id: str
    final_answer: str
    status: TaskStatus
    definition_of_done: str = ""
    execution_mode: str = "live"
    main_llm_identity: str = ""
    critic_llm_identity: str = ""
    critic_independent: bool = False
    verification_passed: bool = False
    release_ready: bool = False
    plan_gate_passed: bool = False
    failed_step_ids: list[str] = Field(default_factory=list)
    web_research_provider: str = "disabled"
    source_ids: list[str] = Field(default_factory=list)
    source_urls: list[str] = Field(default_factory=list)
    plan: Optional[Plan] = None
    critiques: list[Critique] = Field(default_factory=list)
    receipts: list[Receipt] = Field(default_factory=list)
    memory_entries_created: int = 0
    duration_seconds: float = 0.0
    version: int = 1
