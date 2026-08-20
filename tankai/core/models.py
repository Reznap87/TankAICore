"""
Kern-Datenmodelle für TankAI.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
from uuid import uuid4

from pydantic import BaseModel, Field


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


class Plan(BaseModel):
    """Der vom Planner erstellte Plan."""
    id: str = Field(default_factory=lambda: str(uuid4()))
    goal_id: str
    steps: list[PlanStep]
    rationale: str
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
    plan: Optional[Plan] = None
    critiques: list[Critique] = Field(default_factory=list)
    receipts: list[Receipt] = Field(default_factory=list)
    memory_entries_created: int = 0
    duration_seconds: float = 0.0
    version: int = 1
