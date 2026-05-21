from __future__ import annotations

from pydantic import BaseModel, Field


GOVERNANCE_TASK_SCHEMA_VERSION = "harness.governance_task/v1"


class GovernanceGateResult(BaseModel):
    id: str
    passed: bool
    evidence: str = ""


class GovernanceCommandPayload(BaseModel):
    schema_version: str
    ok: bool = True
    hard_gates: list[GovernanceGateResult] = Field(default_factory=list)


class GovernanceTaskMetadata(BaseModel):
    schema_version: str = GOVERNANCE_TASK_SCHEMA_VERSION
    task_id: str
    slug: str
    branch: str
    base: str
    base_sha: str
    worktree_path: str
    session_id: str
    agent: str
    model_profile: str
    permission_profile: str
    sandbox_profile: str
    goal: str
    allowed_paths: list[str] = Field(default_factory=list)
    expected_artifacts: list[str] = Field(default_factory=list)
    context_pack_hash: str | None = None
    latest_test_run_path: str | None = None
    latest_merge_check_verdict: str | None = None
    status: str = "active"
    created_at: str
    closed_at: str | None = None


class GovernanceContextPackResult(BaseModel):
    schema_version: str = "harness.governance_context_pack/v1"
    ok: bool = True
    task_id: str
    path: str
    sha256: str
    payload: dict


class GovernanceTestPlanResult(BaseModel):
    schema_version: str = "harness.governance_test_plan/v1"
    ok: bool = True
    task_id: str
    task_type: str
    policy_hash: str
    payload: dict


class GovernanceTestRunResult(BaseModel):
    schema_version: str = "harness.governance_test_run/v1"
    ok: bool
    task_id: str
    run_id: str
    status: str
    path: str
    policy_hash: str
    payload: dict
