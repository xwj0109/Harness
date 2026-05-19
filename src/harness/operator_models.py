from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field

from harness.models import RunMode, utc_now


class HarnessAgentPhase(str, Enum):
    IDLE = "idle"
    TURN = "turn"
    WAITING_APPROVAL = "waiting_approval"
    COMPACTION = "compaction"
    RETRY = "retry"
    PROJECT_SWITCH = "project_switch"


class HarnessTurnState(BaseModel):
    schema_version: str = "harness.turn_state/v1"
    turn_id: str
    session_id: str
    project_root: str
    cwd: str
    model_profile_id: str
    backend_id: str
    agent_id: str
    workbench_id: str | None = None
    run_mode: RunMode
    active_tools: list[str] = Field(default_factory=list)
    effective_policy_sha256: str
    context_pack_sha256: str | None = None
    stream_options: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class HarnessToolCallRecord(BaseModel):
    schema_version: str = "harness.tool_call/v1"
    tool_call_id: str
    turn_id: str | None = None
    session_id: str
    tool_id: str
    raw_args: dict[str, Any] = Field(default_factory=dict)
    normalized_args: dict[str, Any] = Field(default_factory=dict)
    cwd: str
    permission_state: Literal["not_required", "pending", "approved", "denied"]
    started_at: datetime | None = None
    finished_at: datetime | None = None
    status: Literal["pending", "running", "completed", "failed", "blocked"]
    result_artifact_ids: list[str] = Field(default_factory=list)


class HarnessSavePoint(BaseModel):
    schema_version: str = "harness.save_point/v1"
    save_point_id: str
    turn_id: str
    session_id: str
    flushed_event_count: int
    flushed_artifact_count: int
    next_turn_state_sha256: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class HarnessOperatorQueueKind(str, Enum):
    STEER = "steer"
    FOLLOW_UP = "follow_up"
    NEXT_TURN = "next_turn"


class HarnessOperatorStatus(BaseModel):
    schema_version: str = "harness.operator_status/v1"
    phase: HarnessAgentPhase
    project_root: str
    cwd: str
    active_tools: list[str] = Field(default_factory=list)
    turn_id: str | None = None
    session_id: str | None = None
    waiting_approval_id: str | None = None
    current_turn: HarnessTurnState | None = None
    latest_save_point: dict[str, Any] | None = None
