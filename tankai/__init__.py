"""
TankAI — Web Intelligence OS (Prototype)

Multi-Agenten-Kern mit:
- Commander / Planner / Specialists / Critic / Synthesizer
- PLAN → ROUTE → VERIFY → LEARN
- Langzeitgedächtnis (Episodic, Semantic, Procedural)
- Vector-Suche, Retention, Cold-Storage
- Tool-Use, parallele Specialists
- LLM-Adapter (Mock, OpenAI, Anthropic)
- kontrolliertem Development-Orchestrator
"""

__version__ = "1.9.0-agent-governance-v2"

from .core.llm import (
    AnthropicLLM,
    BaseLLM,
    MockLLM,
    OpenAILLM,
    describe_llm_setup,
    get_critic_llm,
    get_llm,
    llm_identity,
)
from .core.loop import TankAI
from .core.models import Goal, Receipt, TaskStatus
from .dev_orchestrator import (
    AgentGovernancePolicy,
    DevelopmentOrchestrator,
    CommandSpec,
    DevelopmentRole,
    DockerCommandExecutor,
    RuntimeSecurityProfile,
    GateJob,
    FenceBusy,
    FenceError,
    FenceLease,
    FenceLost,
    FenceStatus,
    LeaseFenceStore,
    IntegrationExecutionError,
    IntegrationJob,
    IntegrationResult,
    SpawnRequest,
    TaskSpec,
    WorkerExecutionBackend,
    WorkerIsolationSpec,
    WorkerJob,
    WorkerPipelineJob,
    WorkerPipelineRunner,
    WorkerPoolFailurePolicy,
    WorkerPoolJob,
    WorkerPoolResult,
    WorkerPoolRunner,
    WorkerIntegrationRunner,
)
from .dev_orchestrator.release_publication import (
    PublicationError,
    PublicationLedgerStore,
    PublicationStatus,
    PublicationTarget,
    create_publication_ledger,
    record_artifact_receipt,
    record_source_receipt,
    verify_publication_ledger,
)
from .dev_orchestrator.release_backup import (
    BackupArtifacts,
    BackupFile,
    BackupPolicy,
    BackupVerification,
    ReleaseBackupError,
    collect_backup_files,
    create_release_backup,
    verify_checksum_file,
    verify_release_backup,
)

__all__ = [
    "TankAI",
    "Goal",
    "Receipt",
    "TaskStatus",
    "get_llm",
    "get_critic_llm",
    "llm_identity",
    "MockLLM",
    "BaseLLM",
    "OpenAILLM",
    "AnthropicLLM",
    "describe_llm_setup",
    "AgentGovernancePolicy",
    "DevelopmentOrchestrator",
    "CommandSpec",
    "DevelopmentRole",
    "DockerCommandExecutor",
    "RuntimeSecurityProfile",
    "GateJob",
    "FenceBusy",
    "FenceError",
    "FenceLease",
    "FenceLost",
    "FenceStatus",
    "LeaseFenceStore",
    "IntegrationExecutionError",
    "IntegrationJob",
    "IntegrationResult",
    "SpawnRequest",
    "TaskSpec",
    "WorkerExecutionBackend",
    "WorkerIsolationSpec",
    "WorkerJob",
    "WorkerPipelineJob",
    "WorkerPipelineRunner",
    "WorkerPoolFailurePolicy",
    "WorkerPoolJob",
    "WorkerPoolResult",
    "WorkerPoolRunner",
    "WorkerIntegrationRunner",
    "PublicationError",
    "PublicationLedgerStore",
    "PublicationStatus",
    "PublicationTarget",
    "create_publication_ledger",
    "record_artifact_receipt",
    "record_source_receipt",
    "verify_publication_ledger",
    "BackupArtifacts",
    "BackupFile",
    "BackupPolicy",
    "BackupVerification",
    "ReleaseBackupError",
    "collect_backup_files",
    "create_release_backup",
    "verify_checksum_file",
    "verify_release_backup",
]
