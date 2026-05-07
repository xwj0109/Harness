from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

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


class ObjectiveStatus(str, Enum):
    CREATED = "created"
    ACTIVE = "active"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


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


def run_mode_for_task_type(task_type: str | None) -> RunMode:
    mapping = {
        "read_only_repo_summary": RunMode.READ_ONLY,
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
    metadata: dict[str, Any] = Field(default_factory=dict)


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
    id: str
    run_id: str
    created_at: datetime
    level: str
    event_type: str
    message: str
    payload: dict[str, Any] = Field(default_factory=dict)


class ArtifactRecord(BaseModel):
    schema_version: str = "harness.artifact/v1"
    id: str
    run_id: str
    kind: str
    path: Path
    created_at: datetime
    sha256: str | None = None
    size_bytes: int | None = None
    producer: str | None = None
    redaction_state: str = "unknown"
    evidence_status: str = "unknown"
    metadata: dict[str, Any] = Field(default_factory=dict)


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
    validation_results: dict[str, Any] | None = None


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


class TraceSpan(BaseModel):
    trace_id: str
    span_id: str
    parent_span_id: str | None = None
    name: str
    kind: str = "INTERNAL"
    start_time: datetime
    end_time: datetime
    attributes: dict[str, Any] = Field(default_factory=dict)


class TraceExport(BaseModel):
    schema_version: str = "harness.trace_export/v1"
    ok: bool = True
    format: str = "otel-json"
    run_id: str
    trace_id: str
    spans: list[TraceSpan] = Field(default_factory=list)
