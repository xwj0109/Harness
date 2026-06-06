from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class BillingMode(str, Enum):
    SUBSCRIPTION = "subscription"
    LOCAL_NO_API_COST = "local_no_api_cost"
    PAID_API = "paid_api"


class ExecutionLocation(str, Enum):
    LOCAL_MACHINE = "local_machine"
    HOSTED = "hosted"
    MIXED = "mixed"


class DataBoundary(str, Enum):
    LOCAL_ONLY = "local_only"
    HOSTED_PROVIDER = "hosted_provider"
    EXTERNAL_ROUTER = "external_router"


class BackendKind(str, Enum):
    EXTERNAL_AGENT = "external_agent"
    NATIVE_MODEL = "native_model"


class RunMode(str, Enum):
    READ_ONLY = "read_only"
    PLANNING = "planning"
    LOCAL_EDIT = "local_edit"
    CODEX_EDIT = "codex_edit"
    TEST = "test"
    DEV = "dev"


class TaskStatus(str, Enum):
    CREATED = "created"
    READY = "ready"
    BLOCKED = "blocked"
    WAITING_APPROVAL = "waiting_approval"
    LEASED = "leased"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"

    @classmethod
    def _missing_(cls, value: object) -> "TaskStatus | None":
        if isinstance(value, str):
            legacy = {
                "queued": cls.READY,
                "completed": cls.SUCCEEDED,
                "canceled": cls.CANCELLED,
            }
            return legacy.get(value)
        return None


class SessionStatus(str, Enum):
    ACTIVE = "active"
    IDLE = "idle"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ARCHIVED = "archived"


class ObjectiveStatus(str, Enum):
    CREATED = "created"
    ACTIVE = "active"
    WAITING_APPROVAL = "waiting_approval"
    SUSPENDED = "suspended"
    RETRYING = "retrying"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class TaskDependencyType(str, Enum):
    SUCCESS = "success"
    MANUAL = "manual"
    APPROVAL = "approval"
    ARTIFACT = "artifact"


class TaskLeaseStatus(str, Enum):
    ACTIVE = "active"
    RELEASED = "released"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class OrchestrationProgressMode(str, Enum):
    IDLE = "idle"
    READY = "ready"
    LEASED = "leased"
    DISPATCHING = "dispatching"
    BLOCKED = "blocked"
    TERMINAL = "terminal"


class DaemonStatus(str, Enum):
    RUNNING = "running"
    STOPPED = "stopped"
    STALE = "stale"


class PolicyLevel(str, Enum):
    FORBIDDEN = "forbidden"
    APPROVAL_REQUIRED = "approval_required"
    ALLOWED = "allowed"


class ToolSideEffectLevel(str, Enum):
    NONE = "none"
    ARTIFACT_WRITE = "artifact_write"
    WORKSPACE_WRITE = "workspace_write"
    ACTIVE_REPO_WRITE = "active_repo_write"
    EXTERNAL = "external"


class ToolReplayPolicy(str, Enum):
    SAFE = "safe"
    IDEMPOTENT_WITH_KEY = "idempotent_with_key"
    REQUIRES_FRESH_APPROVAL = "requires_fresh_approval"
    NOT_REPLAYABLE = "not_replayable"


class SecurityDecisionStatus(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    APPROVAL_REQUIRED = "approval_required"


class BlockedStateCode(str, Enum):
    MISSING_APPROVAL = "missing_approval"
    DISABLED_ADAPTER = "disabled_adapter"
    UNSAFE_METADATA = "unsafe_metadata"
    UNKNOWN_ADAPTER = "unknown_adapter"
    SANDBOX_PROFILE_MISMATCH = "sandbox_profile_mismatch"
    BREAKER_OPEN = "breaker_open"
    FORBIDDEN_PATH_OR_SECRET_LIKE_CONTENT = "forbidden_path_or_secret_like_content"
    BLOCKED_BY_POLICY = "blocked_by_policy"


class SecurityFindingSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    HIGH = "high"


class SecurityFindingStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"


class KillSwitchTargetKind(str, Enum):
    ADAPTER = "adapter"
    TASK_TYPE = "task_type"
    BACKEND = "backend"
    HOSTED_BOUNDARY = "hosted_boundary"
    DOCKER_EXECUTION = "docker_execution"
    ACTIVE_REPO_APPLY_BACK = "active_repo_apply_back"


class BreakerStatus(str, Enum):
    CLOSED = "closed"
    OPEN = "open"


class IntegritySubjectKind(str, Enum):
    BUILTIN_SPEC = "builtin_spec"
    ADAPTER_DESCRIPTOR = "adapter_descriptor"
    WORKFLOW_TEMPLATE = "workflow_template"
    SECURITY_DOC = "security_doc"
    ARTIFACT = "artifact"
    TRACE_EXPORT = "trace_export"
    TUI_STATIC_ASSET = "tui_static_asset"


class IntegrityCheckStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"


class ContextTrustLevel(str, Enum):
    TRUSTED_OPERATOR = "trusted_operator"
    UNTRUSTED_REPO = "untrusted_repo"
    UNTRUSTED_TOOL_OUTPUT = "untrusted_tool_output"
    GENERATED = "generated"
    ARTIFACT = "artifact"
    MEMORY = "memory"


class ContextSourceKind(str, Enum):
    USER_PROMPT = "user_prompt"
    REPO_FILE = "repo_file"
    TOOL_OUTPUT = "tool_output"
    ARTIFACT = "artifact"
    GENERATED_PLAN = "generated_plan"
    MEMORY_RECORD = "memory_record"
    TASK_METADATA = "task_metadata"
    RUN_GOAL = "run_goal"


class SandboxTier(str, Enum):
    NONE = "none"
    READ_ONLY = "read_only"
    ISOLATED_WORKSPACE = "isolated_workspace"
    DOCKER_SANDBOX = "docker_sandbox"
    FUTURE_STRONGER_ISOLATION = "future_stronger_isolation"


class SandboxNetworkPolicy(str, Enum):
    FORBIDDEN = "forbidden"
    APPROVAL_REQUIRED = "approval_required"
    ALLOWED = "allowed"


class SandboxActiveRepoWritePolicy(str, Enum):
    FORBIDDEN = "forbidden"
    APPROVAL_REQUIRED = "approval_required"


class SandboxHostFilesystemPolicy(str, Enum):
    FORBIDDEN = "forbidden"
    SANITIZED_COPY = "sanitized_copy"
    ISOLATED_WORKSPACE = "isolated_workspace"


class MemoryScopeType(str, Enum):
    PROJECT = "project"
    WORKBENCH = "workbench"
    AGENT = "agent"
    OBJECTIVE = "objective"
    TASK = "task"


class MemorySourceKind(str, Enum):
    OPERATOR_NOTE = "operator_note"
    ARTIFACT = "artifact"
    RUN = "run"
    TASK = "task"
    OBJECTIVE = "objective"
    ARTIFACT_SUMMARY = "artifact_summary"
    OBJECTIVE_STATE = "objective_state"
    RUN_REVIEW = "run_review"
    FAILED_ATTEMPT_SUMMARY = "failed_attempt_summary"


class MemoryRedactionState(str, Enum):
    NOT_REQUIRED = "not_required"
    REDACTED = "redacted"
    BLOCKED = "blocked"
    FORGOTTEN = "forgotten"


class RunEventType(str, Enum):
    RUN_STARTED = "run.started"
    POLICY_RESOLVED = "policy.resolved"
    APPROVAL_REQUIRED = "approval.required"
    WORKSPACE_PREPARED = "workspace.prepared"
    BACKEND_STARTED = "backend.started"
    MODEL_TOKEN = "model.token"
    MODEL_MESSAGE_DELTA = "model.message_delta"
    REASONING_SUMMARY_DELTA = "reasoning.summary_delta"
    TOOL_CALL_STARTED = "tool_call.started"
    TOOL_CALL_OUTPUT = "tool_call.output"
    TOOL_CALL_FINISHED = "tool_call.finished"
    FILE_READ = "file.read"
    FILE_WRITE = "file.write"
    DIFF_UPDATED = "diff.updated"
    TEST_STARTED = "test.started"
    TEST_OUTPUT = "test.output"
    TEST_FINISHED = "test.finished"
    TOKEN_USAGE_UPDATED = "token_usage.updated"
    ARTIFACT_REGISTERED = "artifact.registered"
    RUN_SUMMARY_CREATED = "run.summary_created"
    RUN_FINISHED = "run.finished"
    RUN_FAILED = "run.failed"


class EventVisibility(str, Enum):
    USER_VISIBLE = "user_visible"
    INTERNAL = "internal"
    RESTRICTED = "restricted"


class RedactionState(str, Enum):
    NOT_REQUIRED = "not_required"
    REDACTED = "redacted"
    RESTRICTED = "restricted"
    BLOCKED = "blocked"


class SessionPartKind(str, Enum):
    TEXT = "text"
    MODEL_DELTA = "model_delta"
    REASONING_SUMMARY = "reasoning_summary"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    PERMISSION_REQUEST = "permission_request"
    QUESTION = "question"
    TODO_UPDATE = "todo_update"
    DIFF = "diff"
    TEST_OUTPUT = "test_output"
    TERMINAL_OUTPUT = "terminal_output"
    ARTIFACT_REF = "artifact_ref"
    RUN_REF = "run_ref"
    SNAPSHOT_REF = "snapshot_ref"
    SUMMARY = "summary"


class SessionMessageRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"
    SYSTEM = "system"


class SessionMutationReversibility(str, Enum):
    NONE = "none"
    NOT_REVERSIBLE_ACTIVE_WORKSPACE = "not_reversible_active_workspace"
    REVERSIBLE_SNAPSHOT = "reversible_snapshot"
    REVERSIBLE_ISOLATED_WORKSPACE = "reversible_isolated_workspace"
    UNKNOWN = "unknown"


class SessionPermissionBoundaryKind(str, Enum):
    LOCAL_ONLY = "local_only"
    HOSTED_PROVIDER = "hosted_provider"
    EXTERNAL_NETWORK = "external_network"
    ACTIVE_REPO_WRITE = "active_repo_write"
    SHELL = "shell"
    MCP = "mcp"
    PTY = "pty"


class SessionPermissionScope(str, Enum):
    ONCE = "once"
    SESSION = "session"
    PROJECT = "project"
    PROFILE = "profile"


class SessionPermissionSource(str, Enum):
    USER = "user"
    POLICY = "policy"
    CONFIG = "config"
    APPROVAL_PROFILE = "approval_profile"


class SessionPermissionStatus(str, Enum):
    PENDING = "pending"
    ALLOWED = "allowed"
    DENIED = "denied"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class EventStreamType(str, Enum):
    SESSION = "session"
    RUN = "run"
    TASK = "task"
    ARTIFACT = "artifact"
    PERMISSION = "permission"
    ORCHESTRATION = "orchestration"


class TokenUsageSnapshot(BaseModel):
    input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    cached_input_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    total_tokens: int | None = None
    estimated_cost_usd: Decimal | None = None


def run_mode_for_task_type(task_type: str | None) -> RunMode:
    mapping = {
        "read_only_repo_summary": RunMode.READ_ONLY,
        "session_plan": RunMode.READ_ONLY,
        "session_read_only_research": RunMode.READ_ONLY,
        "session_operator": RunMode.READ_ONLY,
        "repo_planning": RunMode.PLANNING,
        "simple_code_edit": RunMode.LOCAL_EDIT,
        "codex_code_edit": RunMode.CODEX_EDIT,
        "docker_run_tests": RunMode.TEST,
        "phase_1a_test": RunMode.DEV,
    }
    return mapping.get(task_type or "", RunMode.DEV)


class BackendMetadata(BaseModel):
    billing_mode: BillingMode
    execution_location: ExecutionLocation
    data_boundary: DataBoundary
    allow_network: bool


class BackendCapabilities(BaseModel):
    structured_output: bool = False
    tool_calling: bool = False
    json_mode: bool = False
    max_context_tokens: int | None = None
    supports_exec: bool = False
    supports_read_only_sandbox: bool = False
    supports_json_events: bool = False
    supports_cd: bool = False
    supports_model_arg: bool = False
    supports_output_last_message: bool = False
    supports_output_schema: bool = False
    supports_login_status: bool = False
    supports_workspace_write_sandbox: bool = False
    supports_ask_for_approval: bool = False
    supports_network_control: bool = False
    supports_full_auto: bool = False
    supports_full_auto_workspace_write_on_request: bool = False
    supports_skip_git_repo_check: bool = False


class BackendStatus(BaseModel):
    available: bool
    reason: str | None = None
    metadata: BackendMetadata
    capabilities: BackendCapabilities


class BackendDescriptor(BaseModel):
    name: str
    kind: BackendKind
    metadata: BackendMetadata
    capabilities: BackendCapabilities = Field(default_factory=BackendCapabilities)
    operator_notes: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)


class BackendConfig(BaseModel):
    name: str
    kind: BackendKind
    metadata: BackendMetadata
    capabilities: BackendCapabilities = Field(default_factory=BackendCapabilities)
    settings: dict[str, Any] = Field(default_factory=dict)

    def to_descriptor(self) -> BackendDescriptor:
        constraints = []
        if self.name == "paid_openai_compatible":
            constraints = ["disabled_by_default", "no_automatic_fallback", "preflight_skipped"]
        return BackendDescriptor(
            name=self.name,
            kind=self.kind,
            metadata=self.metadata,
            capabilities=self.capabilities,
            constraints=constraints,
        )


class ToolCapabilityDescriptor(BaseModel):
    schema_version: str = "harness.tool_capability/v1"
    id: str
    description: str
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    side_effect_level: ToolSideEffectLevel
    data_boundary: DataBoundary
    approval_required: list[str] = Field(default_factory=list)
    sandbox_required: bool = False
    idempotency: str = "none"
    replay_policy: ToolReplayPolicy
    allowed_run_modes: list[RunMode] = Field(default_factory=list)
    policy_keys: list[str] = Field(default_factory=list)


class DelegateBudgetPolicy(BaseModel):
    schema_version: str = "harness.delegate_budget/v1"
    timeout_seconds: int = Field(ge=0)
    max_runtime_invocations: int = Field(ge=0)
    max_model_calls: int = Field(ge=0)
    max_tool_calls: int = Field(ge=0)
    max_parallel_branches: int = Field(default=1, ge=1)
    max_input_tokens: int | None = Field(default=None, ge=0)
    max_output_tokens: int | None = Field(default=None, ge=0)
    max_cost_usd: Decimal | None = Field(default=None, ge=0)
    max_cpu_seconds: int | None = Field(default=None, ge=0)
    max_memory_mb: int | None = Field(default=None, ge=0)
    cost_policy: Literal[
        "record_only",
        "local_no_api_cost",
        "subscription_boundary",
        "provider_policy_validated",
        "paid_cost_cap",
    ]
    network_policy: SandboxNetworkPolicy = SandboxNetworkPolicy.FORBIDDEN
    active_repo_write: SandboxActiveRepoWritePolicy = SandboxActiveRepoWritePolicy.FORBIDDEN
    filesystem_scope: Literal[
        "none",
        "harness_artifacts",
        "project_read_only",
        "isolated_workspace",
        "session_policy",
    ]
    tool_allowlist: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class ExecutionAdapterDescriptor(BaseModel):
    schema_version: str = "harness.execution_adapter/v1"
    id: str
    description: str
    supported_task_types: list[str] = Field(default_factory=list)
    required_task_metadata: dict[str, Any] = Field(default_factory=dict)
    rejected_task_metadata: list[str] = Field(default_factory=list)
    required_approvals: list[str] = Field(default_factory=list)
    backend_requirements: list[str] = Field(default_factory=list)
    sandbox_requirements: list[str] = Field(default_factory=list)
    sandbox_profile_id: str | None = None
    side_effect_summary: str
    replay_policy: ToolReplayPolicy
    safety_notes: list[str] = Field(default_factory=list)
    autonomy_default: Literal["auto_allowed", "approval_required", "forbidden"] = "approval_required"
    max_autonomous_retries: int = 0
    delegate_budget: DelegateBudgetPolicy
    required_autonomy_scopes: list[str] = Field(default_factory=list)
    output_contracts: list[str] = Field(default_factory=list)
    terminal_evidence_required: list[str] = Field(default_factory=list)


class CapabilityRecord(BaseModel):
    schema_version: str = "harness.capability/v1"
    id: str
    title: str
    description: str
    execution_adapter: str
    supported_task_types: list[str] = Field(default_factory=list)
    required_approvals: list[str] = Field(default_factory=list)
    backend_requirements: list[str] = Field(default_factory=list)
    sandbox_requirements: list[str] = Field(default_factory=list)
    sandbox_profile: dict[str, Any] | None = None
    delegate_budget: dict[str, Any]
    side_effect_summary: str
    replay_policy: ToolReplayPolicy
    readiness: str
    readiness_reasons: list[str] = Field(default_factory=list)
    blocked_state_explanations: list["BlockedStateExplanation"] = Field(default_factory=list)
    safety_notes: list[str] = Field(default_factory=list)
    equivalent_commands: list[str] = Field(default_factory=list)


class CapabilityCatalog(BaseModel):
    schema_version: str = "harness.capability_catalog/v1"
    ok: bool = True
    project_root: Path
    capabilities: list[CapabilityRecord] = Field(default_factory=list)


class SandboxProfileDescriptor(BaseModel):
    schema_version: str = "harness.sandbox_profile/v1"
    id: str
    tier: SandboxTier
    network: SandboxNetworkPolicy
    active_repo_write: SandboxActiveRepoWritePolicy
    host_filesystem: SandboxHostFilesystemPolicy
    resource_limits: dict[str, Any] = Field(default_factory=dict)
    forbidden_mounts: list[str] = Field(default_factory=list)
    secret_path_policy: str
    notes: list[str] = Field(default_factory=list)


class SandboxProfileCatalog(BaseModel):
    schema_version: str = "harness.sandbox_profiles/v1"
    ok: bool = True
    project_root: Path
    profiles: list[SandboxProfileDescriptor] = Field(default_factory=list)


class KillSwitchRecord(BaseModel):
    schema_version: str = "harness.kill_switch/v1"
    id: str
    target_kind: KillSwitchTargetKind
    target_id: str
    disabled: bool
    reason: str
    actor: str
    created_at: datetime
    updated_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)


class AdapterBreakerState(BaseModel):
    schema_version: str = "harness.adapter_breaker/v1"
    adapter_id: str
    status: BreakerStatus
    failure_count: int
    threshold: int
    window_seconds: int
    opened_at: datetime | None = None
    last_reset_at: datetime | None = None
    reasons: list[str] = Field(default_factory=list)


class MemoryRecord(BaseModel):
    schema_version: str = "harness.memory_record/v1"
    id: str
    scope_type: MemoryScopeType
    scope_id: str
    source_kind: MemorySourceKind
    source_id: str | None = None
    source_artifact_id: str | None = None
    summary: str
    redaction_state: MemoryRedactionState
    sha256: str
    size_bytes: int
    created_at: datetime
    updated_at: datetime
    lineage: dict[str, Any] = Field(default_factory=dict)


class ContextProvenanceRecord(BaseModel):
    schema_version: str = "harness.context_provenance/v1"
    id: str
    source_kind: ContextSourceKind
    trust_level: ContextTrustLevel
    label: str
    source_id: str | None = None
    artifact_id: str | None = None
    memory_id: str | None = None
    path: Path | None = None
    sha256: str | None = None
    redaction_state: str | None = None
    lineage: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


class BlockedStateExplanation(BaseModel):
    schema_version: str = "harness.blocked_state/v1"
    code: BlockedStateCode
    message: str
    details: list[str] = Field(default_factory=list)
    inspect_command: str | None = None


class IntegrityCheckRecord(BaseModel):
    schema_version: str = "harness.integrity_check/v1"
    id: str
    subject_kind: IntegritySubjectKind
    subject_id: str
    path: Path | None = None
    sha256: str | None = None
    expected_sha256: str | None = None
    status: IntegrityCheckStatus
    message: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class IntegrityCheckResult(BaseModel):
    schema_version: str = "harness.integrity_check_result/v1"
    ok: bool
    project_root: Path
    checks: list[IntegrityCheckRecord] = Field(default_factory=list)
    summary: dict[str, int] = Field(default_factory=dict)


class ArtifactProvenanceRecord(BaseModel):
    schema_version: str = "harness.artifact_provenance/v1"
    id: str
    artifact_id: str
    run_id: str
    producer: str | None = None
    source_kind: str
    source_id: str | None = None
    input_sha256: str | None = None
    output_sha256: str | None = None
    redaction_state: str = "unknown"
    lineage: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


GraphNodeKind = Literal[
    "objective",
    "task",
    "agent",
    "approval_gate",
    "adapter_run",
    "artifact",
    "verification",
    "blocker",
]

GraphEdgeKind = Literal[
    "contains",
    "depends_on",
    "assigned_to",
    "leased_by",
    "dispatches",
    "requires_approval",
    "produces",
    "consumes",
    "reviews",
    "verifies",
    "blocked_by",
    "continues_to",
]

GraphEntityState = Literal["ready", "running", "blocked", "failed", "completed", "waiting"]
RightPaneCockpitMode = Literal["overview", "graph", "evidence"]


class OrchestrationInstance(BaseModel):
    schema_version: str = "harness.orchestration_instance/v1"
    orchestration_id: str
    objective_id: str
    title: str
    state: Literal["ready", "running", "blocked", "failed", "completed"]
    assigned_workbench: str | None = None
    assigned_agents: list[str] = Field(default_factory=list)
    active_task_id: str | None = None
    attention_task_id: str | None = None
    last_event_seq: int = 0
    updated_at: str


class GraphNode(BaseModel):
    schema_version: str = "harness.graph_node/v1"
    id: str
    kind: GraphNodeKind
    title: str
    state: GraphEntityState = "waiting"
    entity_id: str | None = None
    entity_kind: str | None = None
    lane_id: str | None = None
    row: int | None = None
    active: bool = False
    attention: bool = False
    symbol: str | None = None
    detail_rows: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class GraphEdge(BaseModel):
    schema_version: str = "harness.graph_edge/v1"
    id: str
    source_node_id: str
    target_node_id: str
    kind: GraphEdgeKind
    title: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentLane(BaseModel):
    schema_version: str = "harness.agent_lane/v1"
    id: str
    title: str
    agent_id: str | None = None
    row: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)


class GraphEvent(BaseModel):
    schema_version: str = "harness.graph_event/v1"
    seq: int
    orchestration_id: str
    objective_id: str | None = None
    event_type: str
    entity_id: str | None = None
    timestamp: str
    summary: str | None = None


class LiveOrchestrationGraph(BaseModel):
    schema_version: str = "harness.live_orchestration_graph/v1"
    orchestration_id: str
    revision: int = 1
    nodes: list[GraphNode] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)
    lanes: list[AgentLane] = Field(default_factory=list)
    selected_node_id: str | None = None
    active_node_ids: list[str] = Field(default_factory=list)
    attention_node_ids: list[str] = Field(default_factory=list)
    timeline_tail: list[GraphEvent] = Field(default_factory=list)


class CockpitTopBarView(BaseModel):
    schema_version: str = "harness.cockpit_top_bar/v1"
    app_label: str = "Harness"
    live: bool = True
    state: str = "idle"
    mode: RightPaneCockpitMode = "overview"
    queue_ready: int = 0
    queue_active: int = 0
    queue_blocked: int = 0
    project: str
    branch: str = "unknown"
    model: str = "default"


class RightPaneCockpitModel(BaseModel):
    schema_version: str = "harness.right_pane_cockpit/v1"
    ok: bool = True
    mode: RightPaneCockpitMode = "overview"
    focus_mode: str = "dashboard"
    query: str = ""
    project: str
    branch: str = "unknown"
    model_label: str = "default"
    live_state: str = "idle"
    initialized: bool = False
    orchestration_instances: list[OrchestrationInstance] = Field(default_factory=list)
    selected_orchestration_id: str | None = None
    pinned_orchestration_id: str | None = None
    selected_node_id: str | None = None
    top_bar: CockpitTopBarView
    graph: LiveOrchestrationGraph | None = None
    all_graphs: list[LiveOrchestrationGraph] = Field(default_factory=list)
    active_work: dict[str, Any] = Field(default_factory=dict)
    attention: list[str] = Field(default_factory=list)
    evidence_rows: list[str] = Field(default_factory=list)
    footer: str = "Ctrl+X O/G/E modes · Tab section · Enter details · ? shortcuts"
    shortcuts_visible: bool = False
    sections: list[dict[str, Any]] = Field(default_factory=list)
    active_section_id: str | None = None
    active_section_index: int = 0
    active_signal: str = "idle"
    summary: dict[str, Any] = Field(default_factory=dict)
    live_activity: dict[str, Any] = Field(default_factory=dict)
    search: dict[str, Any] = Field(default_factory=dict)
    navigation_hints: list[dict[str, Any]] = Field(default_factory=list)
    empty_state: dict[str, Any] | None = None


class OrchestrationProgressTask(BaseModel):
    schema_version: str = "harness.orchestration_progress_task/v1"
    task_id: str
    title: str
    status: TaskStatus
    execution_adapter: str | None = None
    task_type: str | None = None
    attempt_id: str | None = None
    lease_id: str | None = None
    run_id: str | None = None
    terminal_decision: str | None = None
    blocked_reasons: list[str] = Field(default_factory=list)
    blocked_state_explanations: list[BlockedStateExplanation] = Field(default_factory=list)
    next_action: str | None = None


class OrchestrationProgress(BaseModel):
    schema_version: str = "harness.orchestration_progress/v1"
    ok: bool = True
    project_root: Path
    objective_id: str
    objective_title: str
    objective_status: ObjectiveStatus
    selected_orchestrator: str | None = None
    mode: OrchestrationProgressMode
    tasks: list[OrchestrationProgressTask] = Field(default_factory=list)
    active_lease_ids: list[str] = Field(default_factory=list)
    active_run_ids: list[str] = Field(default_factory=list)
    blocked_reasons: list[str] = Field(default_factory=list)
    untrusted_context_warnings: list[str] = Field(default_factory=list)
    checkpoints: dict[str, Any] | None = None
    objective_evidence: dict[str, Any] | None = None
    next_action: str | None = None
    equivalent_commands: list[str] = Field(default_factory=list)


class RunRecord(BaseModel):
    id: str
    goal: str | None = None
    task_type: str | None = None
    status: str
    project_root: Path
    created_at: datetime
    updated_at: datetime
    backend_name: str | None = None
    backend_kind: BackendKind | None = None
    billing_mode: BillingMode | None = None
    execution_location: ExecutionLocation | None = None
    data_boundary: DataBoundary | None = None
    allow_network: bool | None = None
    approval_id: str | None = None
    task_id: str | None = None
    objective_id: str | None = None
    session_id: str | None = None


class SessionSpec(BaseModel):
    schema_version: str = "harness.session/v1"
    id: str
    project_path: Path
    title: str | None = None
    parent_session_id: str | None = None
    forked_from_message_id: str | None = None
    objective_id: str | None = None
    active_task_id: str | None = None
    active_run_id: str | None = None
    workbench_id: str | None = None
    agent_id: str | None = None
    provider_id: str | None = None
    model_id: str | None = None
    model_variant: str | None = None
    raw_model_ref: str | None = None
    mode: RunMode | None = None
    intent: str | None = None
    status: SessionStatus
    summary: str | None = None
    token_input: int = 0
    token_output: int = 0
    token_reasoning: int = 0
    token_cache_read: int = 0
    token_cache_write: int = 0
    estimated_cost_usd: Decimal | None = None
    ui_preferences: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class SessionMessageRecord(BaseModel):
    schema_version: str = "harness.session_message/v1"
    id: str
    session_id: str
    parent_message_id: str | None = None
    role: SessionMessageRole
    agent_id: str | None = None
    run_id: str | None = None
    objective_id: str | None = None
    mutation_reversibility: SessionMutationReversibility = SessionMutationReversibility.NONE
    created_at: datetime
    content_preview: str


class SessionPartRecord(BaseModel):
    schema_version: str = "harness.session_part/v1"
    id: str
    session_id: str
    message_id: str
    kind: SessionPartKind
    ordinal: int
    text: str | None = None
    artifact_id: str | None = None
    run_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    redaction_state: RedactionState = RedactionState.NOT_REQUIRED
    created_at: datetime


class SessionTodoRecord(BaseModel):
    schema_version: str = "harness.session_todo/v1"
    id: str
    session_id: str
    content: str
    status: Literal["pending", "in_progress", "completed", "cancelled"]
    priority: int = 0
    source_message_id: str | None = None
    created_at: datetime
    updated_at: datetime


class SessionPermissionRequest(BaseModel):
    schema_version: str = "harness.session_permission/v1"
    id: str
    session_id: str
    run_id: str | None = None
    tool_id: str
    normalized_action: str
    normalized_target_pattern: str
    boundary_kind: SessionPermissionBoundaryKind
    risk: str
    status: SessionPermissionStatus
    scope: SessionPermissionScope
    source: SessionPermissionSource
    revocable: bool = True
    requested_at: datetime
    resolved_at: datetime | None = None
    expires_at: datetime
    policy_reasons: list[str] = Field(default_factory=list)


class StoredEventRecord(BaseModel):
    schema_version: str = "harness.event/v2"
    id: str
    stream_type: EventStreamType
    stream_id: str
    seq: int
    kind: str
    visibility: EventVisibility = EventVisibility.USER_VISIBLE
    redaction_state: RedactionState = RedactionState.REDACTED
    session_id: str | None = None
    message_id: str | None = None
    run_id: str | None = None
    task_id: str | None = None
    artifact_id: str | None = None
    actor: dict[str, Any] = Field(default_factory=dict)
    correlation_id: str | None = None
    causation_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    artifact_refs: list[str] = Field(default_factory=list)
    created_at: datetime

    def jsonl_envelope(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_id": self.id,
            "stream_type": self.stream_type.value,
            "stream_id": self.stream_id,
            "seq": self.seq,
            "kind": self.kind,
            "occurred_at": self.created_at,
            "session_id": self.session_id,
            "message_id": self.message_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "artifact_id": self.artifact_id,
            "actor": self.actor,
            "correlation_id": self.correlation_id,
            "causation_id": self.causation_id,
            "visibility": self.visibility.value,
            "redaction_state": self.redaction_state.value,
            "payload": self.payload,
            "artifact_refs": self.artifact_refs,
        }


class TaskRecord(BaseModel):
    id: str
    title: str
    description: str = ""
    status: TaskStatus
    project_root: Path
    created_at: datetime
    updated_at: datetime
    priority: int = 0
    objective_id: str | None = None
    workbench_id: str | None = None
    agent_id: str | None = None
    spec_source_kind: str | None = None
    spec_source_path: Path | None = None
    depends_on: list[str] = Field(default_factory=list)
    idempotency_key: str | None = None
    required_approvals: list[str] = Field(default_factory=list)
    approval_state: str | None = None
    run_id: str | None = None
    session_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ProjectAgentRecord(BaseModel):
    schema_version: str = "harness.project_agent/v1"
    agent_id: str
    workbench_id: str
    project_root: Path
    imported_at: datetime
    source_path: Path
    content_sha256: str
    agent: dict[str, Any]
    profiles: list[dict[str, Any]] = Field(default_factory=list)


class ObjectiveRecord(BaseModel):
    id: str
    title: str
    description: str = ""
    status: ObjectiveStatus
    project_root: Path
    created_at: datetime
    updated_at: datetime
    priority: int = 0
    workbench_id: str | None = None
    session_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class TaskDependency(BaseModel):
    id: str
    upstream_task_id: str
    downstream_task_id: str
    dependency_type: TaskDependencyType = TaskDependencyType.SUCCESS
    required_artifact_kind: str | None = None
    created_at: datetime


class TaskAttempt(BaseModel):
    id: str
    task_id: str
    attempt_number: int
    status: TaskStatus
    lease_id: str | None = None
    run_id: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    failure_code: str | None = None
    failure_message: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class TaskLease(BaseModel):
    id: str
    task_id: str
    attempt_id: str | None = None
    owner: str
    status: TaskLeaseStatus
    acquired_at: datetime
    expires_at: datetime
    heartbeat_at: datetime | None = None
    released_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class TaskTransitionRecord(BaseModel):
    id: str
    task_id: str
    from_status: TaskStatus | None = None
    to_status: TaskStatus
    reason: str
    actor: str
    created_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)


class DaemonRecord(BaseModel):
    id: str
    owner: str
    status: DaemonStatus
    pid: int | None = None
    project_root: Path
    started_at: datetime
    heartbeat_at: datetime
    stopped_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class DaemonEvent(BaseModel):
    id: str
    daemon_id: str
    event_type: str
    message: str
    created_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)


class DaemonTickResult(BaseModel):
    schema_version: str = "harness.daemon_tick/v1"
    ok: bool = True
    daemon_id: str
    owner: str
    project_root: Path
    tick_id: str
    decision: str
    selected_task: TaskRecord | None = None
    attempt: TaskAttempt | None = None
    lease: TaskLease | None = None
    pause_reasons: list[dict[str, Any]] = Field(default_factory=list)


class DaemonStatusResult(BaseModel):
    schema_version: str = "harness.daemon_status/v1"
    ok: bool = True
    project_root: Path
    active_daemons: list[DaemonRecord] = Field(default_factory=list)
    latest_events: list[DaemonEvent] = Field(default_factory=list)
    paused_tasks: list[dict[str, Any]] = Field(default_factory=list)
    stale_after_seconds: int


class DaemonRecoveryResult(BaseModel):
    schema_version: str = "harness.daemon_recovery/v1"
    ok: bool = True
    daemon_id: str
    owner: str
    project_root: Path
    renewed_leases: list[TaskLease] = Field(default_factory=list)
    expired_leases: list[TaskLease] = Field(default_factory=list)
    recovered_tasks: list[TaskRecord] = Field(default_factory=list)
    events: list[DaemonEvent] = Field(default_factory=list)


class PolicySource(BaseModel):
    kind: str
    id: str
    description: str = ""
    levels: dict[str, PolicyLevel] = Field(default_factory=dict)
    required_approvals: list[str] = Field(default_factory=list)


class EffectivePolicy(BaseModel):
    schema_version: str = "harness.effective_policy/v1"
    subject_kind: str
    subject_id: str
    resolved_at: datetime
    levels: dict[str, PolicyLevel]
    sources: list[PolicySource] = Field(default_factory=list)
    required_approvals: list[str] = Field(default_factory=list)
    forbidden_reasons: list[str] = Field(default_factory=list)
    monotonicity_checked: bool = True


class EventRecord(BaseModel):
    schema_version: str = "harness.event/v1"
    id: str
    run_id: str
    created_at: datetime
    level: str
    event_type: str
    message: str
    session_id: str | None = None
    task_id: str | None = None
    trace_id: str | None = None
    seq: int | None = None
    visibility: EventVisibility = EventVisibility.USER_VISIBLE
    redaction_state: RedactionState = RedactionState.REDACTED
    payload: dict[str, Any] = Field(default_factory=dict)

    def jsonl_envelope(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_id": self.id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "trace_id": self.trace_id,
            "seq": self.seq,
            "timestamp": self.created_at,
            "type": self.event_type,
            "visibility": self.visibility.value,
            "redaction_state": self.redaction_state.value,
            "payload": self.payload,
        }


class ArtifactRecord(BaseModel):
    schema_version: str = "harness.artifact/v1"
    id: str
    run_id: str
    session_id: str | None = None
    kind: str
    path: Path
    created_at: datetime
    sha256: str | None = None
    size_bytes: int | None = None
    producer: str | None = None
    redaction_state: str = "unknown"
    evidence_status: str = "unknown"
    metadata: dict[str, Any] = Field(default_factory=dict)
    provenance: ArtifactProvenanceRecord | None = None


class ManifestArtifact(BaseModel):
    schema_version: str = "harness.artifact/v1"
    id: str | None = None
    run_id: str | None = None
    kind: str
    path: Path
    created_at: datetime
    sha256: str | None = None
    size_bytes: int | None = None
    producer: str | None = None
    redaction_state: str = "unknown"
    evidence_status: str = "unknown"
    metadata: dict[str, Any] = Field(default_factory=dict)
    provenance: ArtifactProvenanceRecord | None = None


class RunManifest(BaseModel):
    schema_version: str = "harness.manifest/v1.1"
    run_id: str
    goal: str | None = None
    task_type: str | None = None
    run_mode: RunMode
    status: str
    project_root: Path
    created_at: datetime
    updated_at: datetime
    approval_id: str | None = None
    backend_descriptor: BackendDescriptor | None = None
    artifacts: list[ManifestArtifact] = Field(default_factory=list)
    trace_id: str | None = None
    task_id: str | None = None
    objective_id: str | None = None
    effective_policy: EffectivePolicy | None = None
    effective_policy_sha256: str | None = None
    backend_descriptor_sha256: str | None = None
    sandbox_profile: dict[str, Any] | None = None
    delegate_budget: dict[str, Any] | None = None
    validation_results: dict[str, Any] | None = None
    autonomy_decision_id: str | None = None
    autonomous_approval_id: str | None = None
    autonomous_outcome_id: str | None = None
    context_provenance: list[ContextProvenanceRecord] = Field(default_factory=list)
    untrusted_context_warnings: list[str] = Field(default_factory=list)


class SecurityDecision(BaseModel):
    schema_version: str = "harness.security_decision/v1"
    id: str
    created_at: datetime
    subject_kind: str
    subject_id: str
    resource_kind: str
    resource_id: str
    action: str
    decision: SecurityDecisionStatus
    policy_sha256: str | None = None
    required_approvals: list[str] = Field(default_factory=list)
    satisfied_approvals: list[str] = Field(default_factory=list)
    missing_approvals: list[str] = Field(default_factory=list)
    adapter_id: str | None = None
    task_type: str | None = None
    data_boundary: DataBoundary | None = None
    side_effect_level: ToolSideEffectLevel | None = None
    sandbox_profile_id: str | None = None
    replay_policy: ToolReplayPolicy | None = None
    reason_code: str
    reasons: list[str] = Field(default_factory=list)


class DaemonDryRunResult(BaseModel):
    schema_version: str = "harness.daemon_execute_dry_run/v1"
    ok: bool = True
    decision: str
    project_root: Path
    task: TaskRecord
    attempt: TaskAttempt
    lease: TaskLease
    run: RunRecord
    manifest: RunManifest
    policy_sha256: str


class DaemonReadOnlyResult(BaseModel):
    schema_version: str = "harness.daemon_execute_read_only/v1"
    ok: bool = True
    decision: str
    project_root: Path
    task: TaskRecord
    attempt: TaskAttempt
    lease: TaskLease
    run: RunRecord
    manifest: RunManifest
    policy_sha256: str
    errors: list[str] = Field(default_factory=list)


class DaemonExecuteResult(BaseModel):
    schema_version: str = "harness.daemon_execute/v1"
    ok: bool
    decision: str
    adapter_id: str | None = None
    project_root: Path
    task: TaskRecord | None = None
    attempt: TaskAttempt | None = None
    lease: TaskLease | None = None
    run: RunRecord | None = None
    manifest: RunManifest | None = None
    policy_sha256: str | None = None
    approval_id: str | None = None
    security_decision: SecurityDecision | None = None
    context_provenance: list[ContextProvenanceRecord] = Field(default_factory=list)
    untrusted_context_warnings: list[str] = Field(default_factory=list)
    blocked_state_explanations: list[BlockedStateExplanation] = Field(default_factory=list)
    rejection_reasons: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    adapter_result: dict[str, Any] = Field(default_factory=dict)


class DaemonLeaseInspection(BaseModel):
    schema_version: str = "harness.daemon_lease/v1"
    ok: bool = True
    project_root: Path
    lease: TaskLease
    task: TaskRecord | None = None
    attempt: TaskAttempt | None = None
    run: RunRecord | None = None
    manifest: RunManifest | None = None
    dry_run_eligibility: dict[str, Any] = Field(default_factory=dict)
    read_only_eligibility: dict[str, Any] = Field(default_factory=dict)
    execution_eligibility: dict[str, Any] = Field(default_factory=dict)
    security_decision: SecurityDecision | None = None
    context_provenance: list[ContextProvenanceRecord] = Field(default_factory=list)
    untrusted_context_warnings: list[str] = Field(default_factory=list)
    blocked_state_explanations: list[BlockedStateExplanation] = Field(default_factory=list)
    recovery_recommendation: dict[str, Any] = Field(default_factory=dict)


class RunBaselineRecord(BaseModel):
    schema_version: str = "harness.baseline/v1"
    name: str
    run_id: str
    created_at: datetime
    evidence_sha256: str
    snapshot: dict[str, Any]


class RunCompareResult(BaseModel):
    schema_version: str = "harness.compare/v1"
    ok: bool = True
    run_a: str
    run_b: str
    matches: bool
    changed_sections: list[str] = Field(default_factory=list)
    sections: dict[str, dict[str, Any]] = Field(default_factory=dict)


class SafetySmokeCheck(BaseModel):
    id: str
    status: str
    message: str
    evidence: dict[str, Any] = Field(default_factory=dict)


class SafetySmokeResult(BaseModel):
    schema_version: str = "harness.evals.safety_smoke/v1"
    ok: bool
    suite: str = "safety-smoke"
    checks: list[SafetySmokeCheck] = Field(default_factory=list)


class SecurityFinding(BaseModel):
    schema_version: str = "harness.security_finding/v1"
    id: str
    check_id: str
    status: SecurityFindingStatus
    severity: SecurityFindingSeverity
    message: str
    evidence: dict[str, Any] = Field(default_factory=dict)
    run_id: str | None = None
    task_id: str | None = None
    attempt_id: str | None = None
    lease_id: str | None = None
    adapter_id: str | None = None
    security_decision_id: str | None = None
    policy_sha256: str | None = None
    approval_id: str | None = None
    sandbox_profile_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class SecurityCheckResult(BaseModel):
    schema_version: str = "harness.security_check/v1"
    ok: bool
    project_root: Path
    findings: list[SecurityFinding] = Field(default_factory=list)
    summary: dict[str, int] = Field(default_factory=dict)


class SecurityLayerAuditCheck(BaseModel):
    schema_version: str = "harness.security_layer_audit_check/v1"
    id: str
    status: str
    message: str
    evidence: dict[str, Any] = Field(default_factory=dict)


class SecurityLayerAuditResult(BaseModel):
    schema_version: str = "harness.security_layer_audit/v1"
    ok: bool
    project_root: Path
    checks: list[SecurityLayerAuditCheck] = Field(default_factory=list)
    summary: dict[str, int] = Field(default_factory=dict)


class OrchestrationEfficiencyCheck(BaseModel):
    schema_version: str = "harness.orchestration_efficiency_check/v1"
    id: str
    status: str
    message: str
    reference_patterns: list[str] = Field(default_factory=list)
    measurements: dict[str, Any] = Field(default_factory=dict)
    gaps: list[str] = Field(default_factory=list)
    next_actions: list[str] = Field(default_factory=list)


class OrchestrationEfficiencyResult(BaseModel):
    schema_version: str = "harness.orchestration_efficiency/v1"
    ok: bool
    suite: str = "orchestration-efficiency"
    project_root: Path
    safety: dict[str, bool] = Field(default_factory=dict)
    summary: dict[str, int] = Field(default_factory=dict)
    checks: list[OrchestrationEfficiencyCheck] = Field(default_factory=list)


class OrchestrationMicrobenchmarkCase(BaseModel):
    schema_version: str = "harness.orchestration_microbenchmark_case/v1"
    id: str
    status: str
    measurement_mode: str
    message: str
    source_checks: list[str] = Field(default_factory=list)
    metrics: list[str] = Field(default_factory=list)
    measurements: dict[str, Any] = Field(default_factory=dict)
    samples: list[dict[str, Any]] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    next_actions: list[str] = Field(default_factory=list)


class OrchestrationMicrobenchmarkResult(BaseModel):
    schema_version: str = "harness.orchestration_microbenchmarks/v1"
    ok: bool
    suite: str = "orchestration-microbenchmarks"
    project_root: Path
    safety: dict[str, bool] = Field(default_factory=dict)
    summary: dict[str, int] = Field(default_factory=dict)
    benchmarks: list[OrchestrationMicrobenchmarkCase] = Field(default_factory=list)


class OrchestrationSynthesisReport(BaseModel):
    schema_version: str = "harness.orchestration_synthesis/v1"
    ok: bool
    suite: str = "orchestration-synthesis"
    project_root: Path
    reference_root: Path | None = None
    summary: dict[str, Any] = Field(default_factory=dict)
    source_reports: dict[str, Any] = Field(default_factory=dict)
    adopted_reference_patterns: list[dict[str, Any]] = Field(default_factory=list)
    deliberate_non_adoptions: list[dict[str, Any]] = Field(default_factory=list)
    security_complexity_posture: dict[str, Any] = Field(default_factory=dict)
    operator_commands: list[str] = Field(default_factory=list)
    safety: dict[str, bool] = Field(default_factory=dict)


class TraceSpan(BaseModel):
    trace_id: str
    span_id: str
    parent_span_id: str | None = None
    name: str
    kind: str = "INTERNAL"
    start_time: datetime
    end_time: datetime
    attributes: dict[str, Any] = Field(default_factory=dict)


TRACE_SEMANTIC_CONVENTIONS = [
    "opentelemetry.trace",
    "opentelemetry.semconv.gen_ai",
    "opentelemetry.semconv.gen_ai.agent",
    "opentelemetry.semconv.gen_ai.mcp",
]


TRACE_CONTEXT_PROPAGATION = {
    "w3c_trace_context": True,
    "carrier_keys": ["traceparent", "tracestate"],
    "external_protocol_propagation_required": True,
    "sensitive_bodies_included": False,
}


class TraceExport(BaseModel):
    schema_version: str = "harness.trace_export/v1"
    ok: bool = True
    format: str = "otel-json"
    semantic_conventions: list[str] = Field(default_factory=lambda: list(TRACE_SEMANTIC_CONVENTIONS))
    trace_context: dict[str, Any] = Field(default_factory=lambda: dict(TRACE_CONTEXT_PROPAGATION))
    run_id: str | None = None
    objective_id: str | None = None
    objective_run_ids: list[str] = Field(default_factory=list)
    trace_id: str
    spans: list[TraceSpan] = Field(default_factory=list)
