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
        return BackendDescriptor(
            name=self.name,
            kind=self.kind,
            metadata=self.metadata,
            capabilities=self.capabilities,
        )


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


class EventRecord(BaseModel):
    id: str
    run_id: str
    created_at: datetime
    level: str
    event_type: str
    message: str
    payload: dict[str, Any] = Field(default_factory=dict)


class ArtifactRecord(BaseModel):
    id: str
    run_id: str
    kind: str
    path: Path
    created_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)


class ManifestArtifact(BaseModel):
    kind: str
    path: Path
    created_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)


class RunManifest(BaseModel):
    schema_version: str = "harness.manifest/v1"
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
