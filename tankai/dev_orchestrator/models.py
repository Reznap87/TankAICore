"""Typed state for the controlled TankAI development-agent orchestrator."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, ClassVar
import re

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class OrchestratorModel(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DevelopmentRole(str, Enum):
    """Complete TECH AI V2 role catalogue.

    The original coarse roles remain available for backward compatibility,
    while new work should prefer the most specific role possible.
    """

    TECH_AI_CORE = "tech_ai_core"
    TECH_AI_ORCHESTRATOR = "tech_ai_orchestrator"
    TECH_AI_DEPUTY_ORCHESTRATOR = "tech_ai_deputy_orchestrator"

    ARCHITECT = "architect"
    CHIEF_ARCHITECT = "chief_architect"
    SOLUTION_ARCHITECT = "solution_architect"
    REQUIREMENTS = "requirements"
    PRODUCT_TECH = "product_tech"
    PROJECT_CONTROL = "project_control"

    BACKEND = "backend"
    BACKEND_LEAD = "backend_lead"
    BACKEND_CORE = "backend_core"
    API = "api"
    AUTHENTICATION = "authentication"
    AUTHORIZATION = "authorization"
    REALTIME = "realtime"
    BACKGROUND_JOBS = "background_jobs"
    INTEGRATIONS = "integrations"

    FRONTEND = "frontend"
    FRONTEND_LEAD = "frontend_lead"
    UI_COMPONENTS = "ui_components"
    FRONTEND_STATE = "frontend_state"
    FRONTEND_API = "frontend_api"
    RESPONSIVE_UI = "responsive_ui"
    ACCESSIBILITY = "accessibility"

    DESKTOP_LEAD = "desktop_lead"
    WINDOWS = "windows"
    MACOS = "macos"
    LINUX = "linux"
    AUTO_UPDATE = "auto_update"

    MOBILE_LEAD = "mobile_lead"
    ANDROID = "android"
    IOS = "ios"
    MOBILE_OFFLINE = "mobile_offline"

    DATABASE = "database"
    DATABASE_LEAD = "database_lead"
    DATA_MODEL = "data_model"
    DATABASE_MIGRATION = "database_migration"
    DATABASE_PERFORMANCE = "database_performance"
    BACKUP_RECOVERY = "backup_recovery"
    DATA_ISOLATION = "data_isolation"

    AI_ARCHITECT = "ai_architect"
    LLM = "llm"
    PROMPT_ENGINEERING = "prompt_engineering"
    RAG = "rag"
    MEMORY = "memory"
    TOOLING = "tooling"
    MULTI_AGENT_PROTOCOL = "multi_agent_protocol"
    AI_EVALUATION = "ai_evaluation"
    AI_SAFETY = "ai_safety"
    AI_COST = "ai_cost"

    SECURITY = "security"
    SECURITY_LEAD = "security_lead"
    APPSEC = "appsec"
    INFRA_SECURITY = "infra_security"
    SECRETS = "secrets"
    PRIVACY = "privacy"
    THREAT_MODEL = "threat_model"
    RED_TEAM = "red_team"
    DEPENDENCY_SECURITY = "dependency_security"

    QA = "qa"
    QUALITY_LEAD = "quality_lead"
    UNIT_TEST = "unit_test"
    INTEGRATION_TEST = "integration_test"
    E2E = "e2e"
    REGRESSION = "regression"
    PERFORMANCE_TEST = "performance_test"
    FUZZ_TEST = "fuzz_test"
    COMPATIBILITY_TEST = "compatibility_test"
    TEST_DATA = "test_data"

    DEBUG = "debug"
    DEBUG_LEAD = "debug_lead"
    RUNTIME_DEBUG = "runtime_debug"
    BUILD_DEBUG = "build_debug"
    NETWORK_DEBUG = "network_debug"
    DATA_DEBUG = "data_debug"

    DEVOPS = "devops"
    DEVOPS_LEAD = "devops_lead"
    BUILD = "build"
    CI = "ci"
    CD = "cd"
    CONTAINER = "container"
    CLOUD = "cloud"
    IAC = "iac"
    ENVIRONMENT = "environment"

    LOGGING = "logging"
    METRICS = "metrics"
    TRACING = "tracing"
    ALERTING = "alerting"
    INCIDENT_RESPONSE = "incident_response"

    PERFORMANCE_LEAD = "performance_lead"
    BACKEND_PERFORMANCE = "backend_performance"
    FRONTEND_PERFORMANCE = "frontend_performance"
    REALTIME_PERFORMANCE = "realtime_performance"

    UX_LEAD = "ux_lead"
    INTERACTION_DESIGN = "interaction_design"
    DESIGN_SYSTEM = "design_system"
    UX_WRITING = "ux_writing"
    USER_FLOW_TEST = "user_flow_test"

    DOCUMENTATION = "documentation"
    DOCUMENTATION_LEAD = "documentation_lead"
    CODE_DOCUMENTATION = "code_documentation"
    API_DOCUMENTATION = "api_documentation"
    SETUP_DOCUMENTATION = "setup_documentation"
    ARCHITECTURE_DOCUMENTATION = "architecture_documentation"
    CHANGELOG = "changelog"

    REVIEWER = "reviewer"
    RELEASE_MANAGER = "release_manager"
    VERSIONING = "versioning"
    PACKAGING = "packaging"
    RELEASE_VALIDATION = "release_validation"
    ROLLBACK = "rollback"

    CPP_LEAD = "cpp_lead"
    JUCE = "juce"
    AUDIO_ENGINE = "audio_engine"
    REALTIME_AUDIO = "realtime_audio"
    MIDI = "midi"
    PLUGIN_HOST = "plugin_host"
    DSP = "dsp"
    AUDIO_FILE = "audio_file"
    DAW_TIMELINE = "daw_timeline"
    DAW_AUTOMATION = "daw_automation"
    UNDO_REDO = "undo_redo"
    PROJECT_PERSISTENCE = "project_persistence"


class AgentStatus(str, Enum):
    CREATED = "created"
    INITIALIZING = "initializing"
    READY = "ready"
    ACTIVE = "active"
    BLOCKED = "blocked"
    WAITING_FOR_REVIEW = "waiting_for_review"
    FAILED = "failed"
    COMPLETED = "completed"
    MERGED = "merged"
    REJECTED = "rejected"
    TERMINATED = "terminated"
    # Backward-compatible terminal status used by existing serialized states.
    STOPPED = "stopped"


class TaskState(str, Enum):
    PENDING = "pending"
    ACTIVE = "active"
    REVIEW_PENDING = "review_pending"
    QA_PENDING = "qa_pending"
    READY_TO_INTEGRATE = "ready_to_integrate"
    INTEGRATED = "integrated"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ReviewDecision(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class QAStatus(str, Enum):
    PENDING = "pending"
    PASSED = "passed"
    FAILED = "failed"
    NOT_TESTED = "not_tested"


class AgentGovernancePolicy(OrchestratorModel):
    """Persisted TECH AI V2 limits for controlled agent replication."""

    max_active_agents: int = Field(default=40, ge=1, le=400)
    max_total_agents_per_cycle: int = Field(default=80, ge=1, le=2_000)
    max_clone_depth: int = Field(default=5, ge=0, le=32)
    max_children_per_agent: int = Field(default=3, ge=0, le=32)
    max_agents_per_file: int = Field(default=1, ge=1, le=1)
    max_agents_per_module: int = Field(default=4, ge=1, le=64)

    @model_validator(mode="after")
    def _validate_capacity(self):
        if self.max_total_agents_per_cycle < self.max_active_agents:
            raise ValueError(
                "MAX_TOTAL_AGENTS_PER_CYCLE darf nicht kleiner als MAX_ACTIVE_AGENTS sein"
            )
        return self




class CapabilityStatus(str, Enum):
    NOT_STARTED = "NOT_STARTED"
    IN_PROGRESS = "IN_PROGRESS"
    IMPLEMENTED = "IMPLEMENTED"
    VERIFIED = "VERIFIED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    DEPRECATED = "DEPRECATED"


class CapabilityAction(str, Enum):
    CREATE = "CREATE"
    EXTEND = "EXTEND"
    FIX = "FIX"
    TEST = "TEST"
    REVIEW = "REVIEW"
    INTEGRATE = "INTEGRATE"


class CapabilitySpec(OrchestratorModel):
    capability_id: str = Field(min_length=1, max_length=200)
    module_id: str = Field(min_length=1, max_length=200)
    owner_agent_id: str | None = Field(default=None, max_length=120)
    status: CapabilityStatus = CapabilityStatus.NOT_STARTED
    source_ref: str = Field(default="", max_length=1000)
    dependencies: list[str] = Field(default_factory=list)
    interface: str = Field(default="", max_length=20_000)
    acceptance_tests: list[str] = Field(default_factory=list)

    @field_validator("dependencies", "acceptance_tests")
    @classmethod
    def _dedupe_capability_lists(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(item.strip() for item in value if item.strip()))


class WorkerPoolFailurePolicy(str, Enum):
    CONTINUE = "continue"
    STOP_SCHEDULING = "stop_scheduling"


class WorkerExecutionBackend(str, Enum):
    HOST = "host"
    DOCKER = "docker"


class WorkerRunState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUBMITTED = "submitted"
    READY_TO_INTEGRATE = "ready_to_integrate"
    INTEGRATING = "integrating"
    INTEGRATED = "integrated"
    BLOCKED = "blocked"
    FAILED = "failed"


class WorkerPhase(str, Enum):
    PREPARE = "prepare"
    IMPLEMENT = "implement"
    VALIDATE_SCOPE = "validate_scope"
    TEST = "test"
    COMMIT = "commit"
    SUBMIT = "submit"
    REVIEW = "review"
    QA = "qa"
    SECURITY = "security"
    COMPLETE = "complete"
    REBASE = "rebase"
    MERGE = "merge"
    INTEGRATION_TEST = "integration_test"
    INTEGRATED = "integrated"
    FAILED = "failed"


class TestExecution(OrchestratorModel):
    __test__: ClassVar[bool] = False
    command: str = Field(min_length=1, max_length=4000)
    passed: bool
    exit_code: int | None = None
    summary: str = Field(default="", max_length=10_000)
    executed_at: datetime = Field(default_factory=utcnow)


class CommandSpec(OrchestratorModel):
    """One process invocation. Commands are always executed without a shell."""

    argv: list[str] = Field(min_length=1, max_length=128)
    timeout_seconds: float = Field(default=120.0, gt=0, le=3600)
    env: dict[str, str] = Field(default_factory=dict)

    @field_validator("argv")
    @classmethod
    def _validate_argv(cls, value: list[str]) -> list[str]:
        if any(not item or "\x00" in item for item in value):
            raise ValueError("Befehlsargumente dürfen nicht leer sein oder NUL enthalten")
        return value

    @field_validator("env")
    @classmethod
    def _validate_env(cls, value: dict[str, str]) -> dict[str, str]:
        for key, item in value.items():
            if not key or "\x00" in key or "=" in key or "\x00" in item:
                raise ValueError("Ungültige Umgebungsvariable")
        return value


class WorkerIsolationSpec(OrchestratorModel):
    """Immutable container policy for one worker/gate execution cycle."""

    backend: WorkerExecutionBackend = WorkerExecutionBackend.DOCKER
    require_image_digest: bool = True
    image: str = Field(min_length=1, max_length=500)
    network_mode: str = Field(default="none", pattern=r"^none$")
    read_only_root: bool = True
    memory_mb: int = Field(default=512, ge=64, le=32768)
    cpus: float = Field(default=1.0, gt=0, le=32)
    pids_limit: int = Field(default=128, ge=16, le=4096)
    tmpfs_mb: int = Field(default=128, ge=16, le=4096)
    build_tmpfs_mb: int = Field(default=512, ge=32, le=8192)
    nofile_limit: int = Field(default=1024, ge=64, le=65536)
    user: str | None = Field(default=None, max_length=64)

    @field_validator("backend")
    @classmethod
    def _require_docker_backend(cls, value: WorkerExecutionBackend) -> WorkerExecutionBackend:
        if value != WorkerExecutionBackend.DOCKER:
            raise ValueError("WorkerIsolationSpec unterstützt ausschließlich den Docker/OCI-Backend")
        return value

    @model_validator(mode="after")
    def _validate_image(self):
        if self.image.startswith("-") or any(
            ch in self.image for ch in ("\x00", "\n", "\r", " ", "\t")
        ):
            raise ValueError("Container-Image enthält unzulässige Zeichen")
        immutable = re.search(r"@sha256:[0-9a-fA-F]{64}$", self.image) or re.fullmatch(
            r"sha256:[0-9a-fA-F]{64}", self.image
        )
        if self.require_image_digest and not immutable:
            raise ValueError("Produktive Worker-Images müssen per sha256-Digest oder Image-ID fixiert sein")
        return self

    @field_validator("read_only_root")
    @classmethod
    def _require_read_only_root(cls, value: bool) -> bool:
        if not value:
            raise ValueError("Worker-Container benötigen ein read-only Root-Dateisystem")
        return value

    @field_validator("user")
    @classmethod
    def _validate_user(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not re.fullmatch(r"[1-9][0-9]{0,9}:[1-9][0-9]{0,9}", value):
            raise ValueError("Container-User muss als nicht-root UID:GID angegeben werden")
        return value


class WorkerJob(OrchestratorModel):
    agent_id: str = Field(min_length=1, max_length=120)
    implementation_summary: str = Field(min_length=1, max_length=20_000)
    commit_message: str = Field(min_length=1, max_length=500)
    implementation_commands: list[CommandSpec] = Field(min_length=1)
    test_commands: list[CommandSpec] = Field(min_length=1)
    cleanup_workspace_on_failure: bool = False


class GateJob(OrchestratorModel):
    reviewer_agent_id: str = Field(min_length=1, max_length=120)
    review_commands: list[CommandSpec] = Field(min_length=1)
    qa_agent_id: str = Field(min_length=1, max_length=120)
    qa_commands: list[CommandSpec] = Field(min_length=1)
    security_agent_id: str | None = Field(default=None, min_length=1, max_length=120)
    security_commands: list[CommandSpec] = Field(default_factory=list)


class WorkerPipelineJob(OrchestratorModel):
    worker: WorkerJob
    gates: GateJob
    isolation: WorkerIsolationSpec | None = None


class WorkerPoolJob(OrchestratorModel):
    """A bounded set of independent coding-agent pipelines."""

    pipelines: list[WorkerPipelineJob] = Field(min_length=2, max_length=12)
    max_parallel: int = Field(default=6, ge=2, le=12)
    failure_policy: WorkerPoolFailurePolicy = WorkerPoolFailurePolicy.CONTINUE

    @model_validator(mode="after")
    def _validate_pool(self):
        agent_ids = [item.worker.agent_id for item in self.pipelines]
        if len(set(agent_ids)) != len(agent_ids):
            raise ValueError("Jeder Pool-Eintrag benötigt einen eigenen Programmier-Agenten")
        if self.max_parallel > len(self.pipelines):
            self.max_parallel = len(self.pipelines)
        return self


class IntegrationJob(OrchestratorModel):
    """A reviewed worker run plus the commands required on the merged main tree."""

    run_id: str = Field(min_length=1, max_length=160)
    test_commands: list[CommandSpec] = Field(min_length=1)
    isolation: WorkerIsolationSpec | None = None
    cleanup_workspace_on_success: bool = True
    delete_branch_on_success: bool = True


class WorkerStatusMessage(OrchestratorModel):
    sequence: int = Field(ge=1)
    phase: WorkerPhase
    message: str = Field(min_length=1, max_length=10_000)
    created_at: datetime = Field(default_factory=utcnow)


class WorkerRunRecord(OrchestratorModel):
    run_id: str = Field(min_length=1, max_length=160)
    agent_id: str = Field(min_length=1, max_length=120)
    task_id: str = Field(min_length=1, max_length=120)
    base_commit: str = Field(min_length=1, max_length=200)
    branch: str = Field(min_length=1, max_length=300)
    workspace_path: str = Field(min_length=1, max_length=4000)
    execution_backend: WorkerExecutionBackend = WorkerExecutionBackend.HOST
    isolation: WorkerIsolationSpec | None = None
    integration_isolation: WorkerIsolationSpec | None = None
    state: WorkerRunState = WorkerRunState.PENDING
    phase: WorkerPhase = WorkerPhase.PREPARE
    status_messages: list[WorkerStatusMessage] = Field(default_factory=list)
    implementation_executions: list[TestExecution] = Field(default_factory=list)
    test_executions: list[TestExecution] = Field(default_factory=list)
    review_executions: list[TestExecution] = Field(default_factory=list)
    qa_executions: list[TestExecution] = Field(default_factory=list)
    security_executions: list[TestExecution] = Field(default_factory=list)
    integration_executions: list[TestExecution] = Field(default_factory=list)
    changed_files: list[str] = Field(default_factory=list)
    implementation_commit: str | None = None
    rebased_from_commit: str | None = None
    rebased_commit: str | None = None
    integration_commit: str | None = None
    error: str = Field(default="", max_length=20_000)
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None


class TaskSpec(OrchestratorModel):
    task_id: str = Field(min_length=1, max_length=120)
    goal: str = Field(min_length=1, max_length=20_000)
    capability_id: str | None = Field(default=None, max_length=200)
    capability_action: CapabilityAction | None = None
    base_commit: str = Field(min_length=1, max_length=200)
    affected_components: list[str] = Field(default_factory=list)
    allowed_paths: list[str] = Field(default_factory=list)
    denied_paths: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    required_tests: list[str] = Field(default_factory=list)
    priority: int = Field(default=50, ge=0, le=100)
    deadlock_rules: list[str] = Field(default_factory=list)
    assigned_agent_id: str | None = None
    reviewer_agent_id: str | None = None
    qa_agent_id: str | None = None
    security_agent_id: str | None = None
    worker_run_id: str | None = None
    requires_security_review: bool = False
    security_approved: bool | None = None
    state: TaskState = TaskState.PENDING
    implementation_summary: str = ""
    changed_files: list[str] = Field(default_factory=list)
    implementation_commit: str | None = None
    test_executions: list[TestExecution] = Field(default_factory=list)
    review_decision: ReviewDecision = ReviewDecision.PENDING
    review_notes: str = ""
    qa_status: QAStatus = QAStatus.PENDING
    qa_executions: list[TestExecution] = Field(default_factory=list)
    security_notes: str = ""
    integration_commit: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    @field_validator(
        "affected_components",
        "allowed_paths",
        "denied_paths",
        "dependencies",
        "acceptance_criteria",
        "required_tests",
        "deadlock_rules",
        "changed_files",
    )
    @classmethod
    def _deduplicate_nonblank(cls, value: list[str]) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for item in value:
            item = item.strip()
            if item and item not in seen:
                seen.add(item)
                out.append(item)
        return out


class AgentSpec(OrchestratorModel):
    agent_id: str = Field(min_length=1, max_length=120)
    role: DevelopmentRole
    parent_agent_id: str | None = None
    generation: int = Field(default=0, ge=0)
    cycle_id: str = Field(default="cycle-000001", min_length=1, max_length=120)
    contract_version: int = Field(default=2, ge=1)
    base_commit: str = Field(min_length=1, max_length=200)
    task_id: str
    branch: str | None = None
    workspace_path: str | None = None
    allowed_paths: list[str] = Field(default_factory=list)
    denied_paths: list[str] = Field(default_factory=list)
    read_access: list[str] = Field(default_factory=lambda: ["repository/**"])
    acceptance_criteria: list[str] = Field(default_factory=list)
    required_tests: list[str] = Field(default_factory=list)
    reviewer_agent_id: str | None = None
    priority: int = Field(default=50, ge=0, le=100)
    deadlock_rules: list[str] = Field(default_factory=list)
    status: AgentStatus = AgentStatus.ACTIVE
    worklog: list[str] = Field(default_factory=list)
    test_results: list[TestExecution] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def _validate_contract(self):
        if self.generation > 0 and not self.parent_agent_id:
            raise ValueError("Replizierter Agent benötigt einen Eltern-Agenten")
        if not self.acceptance_criteria:
            raise ValueError("Agentenvertrag benötigt mindestens ein Abnahmekriterium")
        return self


class SpawnRequest(OrchestratorModel):
    parent_agent_id: str = Field(min_length=1, max_length=120)
    requested_role: DevelopmentRole
    reason: str = Field(min_length=1, max_length=5000)
    task_id: str = Field(min_length=1, max_length=120)
    assigned_subtask: str = Field(min_length=1, max_length=20_000)
    allowed_paths: list[str] = Field(min_length=1)
    denied_paths: list[str] = Field(default_factory=list)
    base_commit: str = Field(min_length=1, max_length=200)
    acceptance_criteria: list[str] = Field(min_length=1)
    required_tests: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    affected_components: list[str] = Field(default_factory=list)
    priority: int = Field(default=50, ge=0, le=100)
    deadlock_rules: list[str] = Field(default_factory=list)
    requires_security_review: bool = False


class FileLock(OrchestratorModel):
    scope: str
    agent_id: str
    task_id: str
    acquired_at: datetime = Field(default_factory=utcnow)


class AuditEvent(OrchestratorModel):
    sequence: int = Field(ge=1)
    event_type: str
    actor_id: str
    details: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)


class ProjectState(OrchestratorModel):
    schema_version: int = 6
    revision: int = Field(default=0, ge=0)
    governance: AgentGovernancePolicy = Field(default_factory=AgentGovernancePolicy)
    cycle_sequence: int = Field(default=1, ge=1)
    cycle_id: str = Field(default="cycle-000001", min_length=1, max_length=120)
    cycle_agent_ids: list[str] = Field(default_factory=list)
    current_version: str
    current_branch: str
    current_commit: str
    architecture_status: str = "unreviewed"
    agents: dict[str, AgentSpec] = Field(default_factory=dict)
    tasks: dict[str, TaskSpec] = Field(default_factory=dict)
    capabilities: dict[str, CapabilitySpec] = Field(default_factory=dict)
    worker_runs: dict[str, WorkerRunRecord] = Field(default_factory=dict)
    file_locks: list[FileLock] = Field(default_factory=list)
    open_errors: list[str] = Field(default_factory=list)
    completed_tasks: list[str] = Field(default_factory=list)
    blocked_tasks: list[str] = Field(default_factory=list)
    pending_reviews: list[str] = Field(default_factory=list)
    test_status: str = "not_run"
    release_status: str = "development"
    audit_log: list[AuditEvent] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def _validate_cycle_registry(self):
        if len(self.cycle_agent_ids) != len(set(self.cycle_agent_ids)):
            raise ValueError("Zyklusregister enthält doppelte Agenten-IDs")
        unknown = sorted(set(self.cycle_agent_ids) - set(self.agents))
        if unknown:
            raise ValueError(
                "Zyklusregister referenziert unbekannte Agenten: " + ", ".join(unknown)
            )
        missing = sorted(
            agent_id
            for agent_id, agent in self.agents.items()
            if agent.cycle_id == self.cycle_id and agent_id not in self.cycle_agent_ids
        )
        if missing:
            raise ValueError(
                "Aktuelle Zyklusagenten fehlen im Zyklusregister: " + ", ".join(missing)
            )
        return self
