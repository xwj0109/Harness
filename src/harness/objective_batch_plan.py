from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, NonNegativeInt, PositiveInt


OBJECTIVE_BATCH_PLAN_SCHEMA_VERSION = "harness.objective_batch_plan/v1"


class ObjectiveScheduleProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    priority: int
    critical_path_depth: NonNegativeInt
    downstream_task_count: NonNegativeInt


class ObjectiveSchedulePolicyEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    policy_id: Literal["priority_then_critical_path"] = "priority_then_critical_path"
    sort_keys: list[
        Literal[
            "priority_desc",
            "critical_path_depth_desc",
            "downstream_task_count_desc",
            "created_at_asc",
            "task_id_asc",
        ]
    ] = Field(
        default_factory=lambda: [
            "priority_desc",
            "critical_path_depth_desc",
            "downstream_task_count_desc",
            "created_at_asc",
            "task_id_asc",
        ]
    )
    candidate_order_basis: Literal["candidate_task_ids_sorted_by_policy"] = "candidate_task_ids_sorted_by_policy"
    resumed_lease_order_basis: Literal["lease_acquired_at_then_lease_id"] = "lease_acquired_at_then_lease_id"
    fresh_selection_basis: Literal["policy_prefix_after_resumed_leases"] = "policy_prefix_after_resumed_leases"


class ObjectiveBatchSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    lease_id: str
    adapter_id: str
    task_type: str | None = None
    selection_source: Literal["resumed_active_lease", "new_guarded_lease"] = "new_guarded_lease"
    decision_status: str
    autonomy_decision_id: str
    depends_on: list[str] = Field(default_factory=list)
    workflow_stage: str | None = None
    schedule_profile: ObjectiveScheduleProfile


class ObjectiveDependencySnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    status: str
    priority: int
    depends_on: list[str] = Field(default_factory=list)
    dependency_statuses: dict[str, str] = Field(default_factory=dict)
    unresolved_dependency_ids: list[str] = Field(default_factory=list)
    schedule_profile: ObjectiveScheduleProfile
    execution_adapter: str | None = None
    task_type: str | None = None
    workflow_stage: str | None = None


class ObjectiveBatchPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_schema_version: Literal["harness.objective_batch_plan/v1"] = OBJECTIVE_BATCH_PLAN_SCHEMA_VERSION
    batch: PositiveInt
    scheduler_mode: Literal["bounded_parallel"]
    scheduler_policy: Literal["priority_then_critical_path"]
    max_parallel: PositiveInt
    batch_capacity: PositiveInt
    remaining_dispatch_budget: NonNegativeInt
    policy_evidence: ObjectiveSchedulePolicyEvidence = Field(default_factory=ObjectiveSchedulePolicyEvidence)
    candidate_task_ids: list[str] = Field(default_factory=list)
    blocked_task_ids: list[str] = Field(default_factory=list)
    schedule_profiles: dict[str, ObjectiveScheduleProfile] = Field(default_factory=dict)
    selected_task_ids: list[str] = Field(default_factory=list)
    selected_lease_ids: list[str] = Field(default_factory=list)
    selected: list[ObjectiveBatchSelection] = Field(default_factory=list)
    dependency_snapshots: list[ObjectiveDependencySnapshot] = Field(default_factory=list)
    pending_stop_reason: str | None = None
