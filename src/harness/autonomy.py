from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field

from harness.models import PolicyLevel, utc_now


AUTONOMY_POLICY_SCHEMA_VERSION = "harness.autonomy_policy/v1"
AUTONOMY_DECISION_SCHEMA_VERSION = "harness.autonomy_decision/v1"
AUTONOMOUS_APPROVAL_SCHEMA_VERSION = "harness.autonomous_approval/v1"

READ_ONLY_TOOLS = {
    "repo_tree",
    "read_file",
    "search_repo",
    "show_diff",
    "show_recent_runs",
    "show_progress",
    "show_capabilities",
    "show_task",
    "show_run",
    "show_artifact",
    "explain_policy",
    "list_agents",
    "show_agent",
    "list_workbenches",
    "list_model_profiles",
    "list_tool_policies",
    "list_memory_scopes",
    "show_objectives",
    "show_objective",
    "show_task_graph",
    "show_leases",
    "show_lease",
    "show_registered_adapters",
    "show_adapter",
    "show_approvals",
    "show_security_summary",
    "show_sandbox_profiles",
    "show_trace",
    "show_apply_back_state",
    "explain_blocked_state",
}

CONTROL_PLANE_WRITE_TOOLS = {
    "create_objective",
    "create_task",
    "create_task_graph",
    "request_approval",
    "remember",
    "forget_memory",
}
AUTONOMOUS_CONTROL_PLANE_WRITE_TOOLS = CONTROL_PLANE_WRITE_TOOLS - {"request_approval"}

SANDBOXED_EXECUTION_TOOLS = {"dispatch_registered_adapter", "run_tests"}
REPO_MUTATION_TOOLS = {"edit_isolated", "apply_back", "deny_apply_back", "revert_pending_change"}

SAFE_LOCAL_TOOLS = sorted(READ_ONLY_TOOLS | AUTONOMOUS_CONTROL_PLANE_WRITE_TOOLS | {"dispatch_registered_adapter"})
SUPERVISED_CODEX_TOOLS = sorted(set(SAFE_LOCAL_TOOLS) | {"dispatch_registered_adapter", "edit_isolated"})

POLICY_KEY_BY_BOUNDARY = {
    "local_read": "local_filesystem",
    "local_control_plane": "task_queue_execution",
    "local_artifact": "local_filesystem",
    "sandboxed_execution": "docker_execution",
    "docker_execution": "docker_execution",
    "active_repo": "active_repo_write",
    "active_repo_apply_back": "active_repo_write",
    "hosted_provider": "hosted_boundary",
    "hosted_provider_codex": "hosted_boundary",
    "external_network": "external_network",
    "paid_provider": "paid_provider",
}


class AutonomyDecisionStatus(str, Enum):
    AUTO_ALLOWED = "auto_allowed"
    APPROVAL_REQUIRED = "approval_required"
    DENIED = "denied"
    BUDGET_EXCEEDED = "budget_exceeded"
    POLICY_MISMATCH = "policy_mismatch"
    REQUIRES_HUMAN_BOUNDARY = "requires_human_boundary"


class AutonomyScope(str, Enum):
    PROJECT = "project"
    WORKBENCH = "workbench"
    AGENT = "agent"
    OBJECTIVE = "objective"
    TASK = "task"


class AutonomyBudget(BaseModel):
    max_model_turns: int = 20
    max_tool_calls: int = 60
    max_side_effect_actions: int = 10
    max_runtime_seconds: int = 900
    max_adapter_dispatches: int = 3
    max_new_tasks: int = 10
    max_consecutive_failures: int = 2
    max_cost_usd: Decimal | None = None


class AutonomyPolicy(BaseModel):
    schema_version: Literal["harness.autonomy_policy/v1"] = AUTONOMY_POLICY_SCHEMA_VERSION
    id: str
    scope: AutonomyScope = AutonomyScope.PROJECT
    allowed_tools: list[str] = Field(default_factory=list)
    allowed_adapters: list[str] = Field(default_factory=list)
    allowed_task_types: list[str] = Field(default_factory=list)
    allowed_boundaries: list[str] = Field(default_factory=list)
    auto_confirm_risks: list[str] = Field(default_factory=list)
    pause_on_risks: list[str] = Field(default_factory=list)
    forbidden_risks: list[str] = Field(default_factory=list)
    budget: AutonomyBudget = Field(default_factory=AutonomyBudget)
    require_evidence: bool = True
    require_idempotency: bool = True
    require_sandbox: bool = True
    allow_create_objectives: bool = False
    allow_create_tasks: bool = False
    allow_memory_writes: bool = False
    allow_adapter_dispatch: bool = False
    allow_active_repo_mutation: bool = False
    allow_hosted_provider_autonomy: bool = False


class AutonomyEvaluationInput(BaseModel):
    tool_name: str
    risk: str
    boundary: str
    adapter_id: str | None = None
    task_type: str | None = None
    has_scoped_approval: bool = False
    would_mutate_active_repo: bool = False
    requires_network: bool = False
    requires_paid_or_hosted_boundary: bool = False
    requires_sandbox: bool = False
    sandbox_enforced: bool = False
    idempotency_key: str | None = None
    evidence_contract: str | None = None
    effective_policy_levels: dict[str, PolicyLevel] = Field(default_factory=dict)
    kill_switch_active: bool = False
    adapter_breaker_open: bool = False
    model_turns_used: int = 0
    tool_calls_used: int = 0
    side_effect_actions_used: int = 0
    runtime_seconds_used: int = 0
    adapter_dispatches_used: int = 0
    new_tasks_used: int = 0
    consecutive_failures: int = 0
    cost_usd_used: Decimal | None = None


class AutonomyDecision(BaseModel):
    schema_version: Literal["harness.autonomy_decision/v1"] = AUTONOMY_DECISION_SCHEMA_VERSION
    status: AutonomyDecisionStatus
    policy_id: str
    tool_name: str | None = None
    adapter_id: str | None = None
    task_type: str | None = None
    boundary: str | None = None
    risk: str | None = None
    reasons: list[str] = Field(default_factory=list)
    requires_human: bool = False
    evidence_required: bool = True


class AutonomousApprovalRecord(BaseModel):
    schema_version: Literal["harness.autonomous_approval/v1"] = AUTONOMOUS_APPROVAL_SCHEMA_VERSION
    id: str
    policy_id: str
    decision_status: AutonomyDecisionStatus
    tool_name: str
    adapter_id: str | None = None
    task_type: str | None = None
    boundary: str
    risk: str
    reasons: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)

    def to_jsonl_payload(self) -> dict[str, object]:
        return self.model_dump(mode="json")


class AutonomyViolation(BaseModel):
    schema_version: Literal["harness.autonomy_violation/v1"] = "harness.autonomy_violation/v1"
    policy_id: str
    code: str
    message: str
    details: list[str] = Field(default_factory=list)


def builtin_autonomy_policies() -> dict[str, AutonomyPolicy]:
    safe_local = AutonomyPolicy(
        id="safe-local",
        allowed_tools=SAFE_LOCAL_TOOLS,
        allowed_adapters=["dry_run"],
        allowed_task_types=["phase_1a_test"],
        allowed_boundaries=["local_read", "local_control_plane", "local_artifact", "sandboxed_execution"],
        auto_confirm_risks=["read", "control_plane_write", "sandboxed_execution"],
        pause_on_risks=[],
        forbidden_risks=["repo_mutation"],
        allow_create_objectives=True,
        allow_create_tasks=True,
        allow_memory_writes=True,
        allow_adapter_dispatch=True,
    )
    return {
        "manual": AutonomyPolicy(
            id="manual",
            allowed_tools=sorted(
                READ_ONLY_TOOLS | CONTROL_PLANE_WRITE_TOOLS | SANDBOXED_EXECUTION_TOOLS | REPO_MUTATION_TOOLS
            ),
            allowed_boundaries=[
                "local_read",
                "local_artifact",
                "local_control_plane",
                "sandboxed_execution",
                "docker_execution",
                "hosted_provider",
                "hosted_provider_codex",
                "active_repo",
                "active_repo_apply_back",
            ],
            auto_confirm_risks=["read"],
            pause_on_risks=["control_plane_write", "sandboxed_execution", "repo_mutation"],
            forbidden_risks=[],
            require_idempotency=False,
            require_sandbox=False,
        ),
        "safe-local": safe_local,
        "supervised-codex": AutonomyPolicy(
            id="supervised-codex",
            budget=AutonomyBudget(max_adapter_dispatches=10, max_side_effect_actions=10, max_runtime_seconds=900),
            allowed_tools=SUPERVISED_CODEX_TOOLS,
            allowed_adapters=["dry_run", "read_only_summary", "repo_planning", "codex_isolated_edit"],
            allowed_task_types=["phase_1a_test", "read_only_repo_summary", "repo_planning", "codex_code_edit"],
            allowed_boundaries=[
                "local_read",
                "local_control_plane",
                "local_artifact",
                "hosted_provider",
                "hosted_provider_codex",
            ],
            auto_confirm_risks=["read", "control_plane_write", "sandboxed_execution", "repo_mutation"],
            pause_on_risks=[],
            forbidden_risks=[],
            allow_create_objectives=True,
            allow_create_tasks=True,
            allow_memory_writes=True,
            allow_adapter_dispatch=True,
            allow_hosted_provider_autonomy=True,
        ),
        "daemon-safe": safe_local.model_copy(
            update={
                "id": "daemon-safe",
                "budget": AutonomyBudget(
                    max_model_turns=10,
                    max_tool_calls=30,
                    max_side_effect_actions=5,
                    max_runtime_seconds=300,
                    max_adapter_dispatches=1,
                    max_new_tasks=3,
                    max_consecutive_failures=1,
                ),
            }
        ),
    }


def get_builtin_autonomy_policy(profile_id: str) -> AutonomyPolicy:
    policies = builtin_autonomy_policies()
    try:
        return policies[profile_id]
    except KeyError as exc:
        raise KeyError(f"Unknown autonomy profile: {profile_id}") from exc


def evaluate_autonomy(policy: AutonomyPolicy, request: AutonomyEvaluationInput) -> AutonomyDecision:
    reasons: list[str] = []

    if request.kill_switch_active:
        return _decision(
            policy,
            request,
            AutonomyDecisionStatus.DENIED,
            ["runtime kill switch is active"],
        )
    if request.adapter_breaker_open:
        return _decision(
            policy,
            request,
            AutonomyDecisionStatus.DENIED,
            ["adapter breaker is open"],
        )

    budget_reason = _budget_exhaustion_reason(policy.budget, request)
    if budget_reason:
        return _decision(policy, request, AutonomyDecisionStatus.BUDGET_EXCEEDED, [budget_reason])

    policy_decision = _effective_policy_decision(request)
    if policy_decision is not None:
        status, reason = policy_decision
        return _decision(policy, request, status, [reason])

    if request.would_mutate_active_repo and not policy.allow_active_repo_mutation:
        return _decision(
            policy,
            request,
            AutonomyDecisionStatus.DENIED,
            ["active repo mutation is not auto-allowed by this autonomy policy"],
        )

    if request.tool_name not in policy.allowed_tools:
        return _decision(
            policy,
            request,
            AutonomyDecisionStatus.DENIED,
            [f"tool is not allowed by autonomy profile: {request.tool_name}"],
        )
    reasons.append(f"tool is allowed by autonomy profile: {request.tool_name}")

    if request.risk in policy.forbidden_risks:
        return _decision(
            policy,
            request,
            AutonomyDecisionStatus.DENIED,
            [f"tool risk is forbidden by autonomy profile: {request.risk}"],
        )
    if request.risk in policy.pause_on_risks:
        return _decision(
            policy,
            request,
            AutonomyDecisionStatus.APPROVAL_REQUIRED,
            [f"tool risk requires approval by autonomy profile: {request.risk}"],
        )
    if request.risk not in policy.auto_confirm_risks:
        return _decision(
            policy,
            request,
            AutonomyDecisionStatus.APPROVAL_REQUIRED,
            [f"tool risk is not auto-confirmed by autonomy profile: {request.risk}"],
        )
    reasons.append(f"tool risk is auto-confirmed by autonomy profile: {request.risk}")

    if request.boundary not in policy.allowed_boundaries:
        return _decision(
            policy,
            request,
            AutonomyDecisionStatus.REQUIRES_HUMAN_BOUNDARY,
            [f"boundary is not auto-allowed by autonomy profile: {request.boundary}"],
        )
    reasons.append(f"boundary is allowed by autonomy profile: {request.boundary}")

    if request.requires_network and not request.has_scoped_approval:
        return _decision(
            policy,
            request,
            AutonomyDecisionStatus.APPROVAL_REQUIRED,
            ["network boundary requires scoped approval"],
        )
    if request.requires_paid_or_hosted_boundary and not request.has_scoped_approval:
        if (
            policy.allow_hosted_provider_autonomy
            and request.boundary == "hosted_provider_codex"
            and request.tool_name == "edit_isolated"
        ):
            reasons.append("hosted-provider Codex boundary is auto-authorized for isolated edit by this autonomy profile")
        else:
            return _decision(
                policy,
                request,
                AutonomyDecisionStatus.APPROVAL_REQUIRED,
                ["hosted or paid provider boundary requires scoped approval"],
            )

    if request.adapter_id is not None:
        if not policy.allow_adapter_dispatch:
            return _decision(policy, request, AutonomyDecisionStatus.APPROVAL_REQUIRED, ["adapter dispatch is not auto-enabled by this autonomy policy"])
        if request.adapter_id not in policy.allowed_adapters:
            return _decision(
                policy,
                request,
                AutonomyDecisionStatus.DENIED,
                [f"adapter is not allowed by autonomy profile: {request.adapter_id}"],
            )
        reasons.append(f"adapter is allowed by autonomy profile: {request.adapter_id}")

    if request.task_type is not None:
        if request.task_type not in policy.allowed_task_types:
            return _decision(
                policy,
                request,
                AutonomyDecisionStatus.DENIED,
                [f"task type is not allowed by autonomy profile: {request.task_type}"],
            )
        reasons.append(f"task type is allowed by autonomy profile: {request.task_type}")

    if policy.require_sandbox and request.requires_sandbox and not request.sandbox_enforced:
        return _decision(policy, request, AutonomyDecisionStatus.APPROVAL_REQUIRED, ["required sandbox is not enforced"])

    if _is_side_effect(request) and policy.require_idempotency and not request.idempotency_key:
        return _decision(policy, request, AutonomyDecisionStatus.APPROVAL_REQUIRED, ["side-effecting autonomous action requires an idempotency key"])

    if _is_side_effect(request) and policy.require_evidence and not request.evidence_contract:
        return _decision(policy, request, AutonomyDecisionStatus.APPROVAL_REQUIRED, ["side-effecting autonomous action requires an evidence contract"])

    return _decision(policy, request, AutonomyDecisionStatus.AUTO_ALLOWED, reasons)


def _decision(
    policy: AutonomyPolicy,
    request: AutonomyEvaluationInput,
    status: AutonomyDecisionStatus,
    reasons: list[str],
) -> AutonomyDecision:
    return AutonomyDecision(
        status=status,
        policy_id=policy.id,
        tool_name=request.tool_name,
        adapter_id=request.adapter_id,
        task_type=request.task_type,
        boundary=request.boundary,
        risk=request.risk,
        reasons=reasons,
        requires_human=status
        in {AutonomyDecisionStatus.APPROVAL_REQUIRED, AutonomyDecisionStatus.REQUIRES_HUMAN_BOUNDARY},
        evidence_required=policy.require_evidence,
    )


def _is_side_effect(request: AutonomyEvaluationInput) -> bool:
    return request.risk != "read"


def _effective_policy_decision(
    request: AutonomyEvaluationInput,
) -> tuple[AutonomyDecisionStatus, str] | None:
    policy_key = POLICY_KEY_BY_BOUNDARY.get(request.boundary)
    if policy_key is None:
        return None
    level = request.effective_policy_levels.get(policy_key)
    if level is None:
        return None
    if level == PolicyLevel.FORBIDDEN:
        return AutonomyDecisionStatus.POLICY_MISMATCH, f"effective policy forbids boundary: {policy_key}"
    if level == PolicyLevel.APPROVAL_REQUIRED and not request.has_scoped_approval:
        return (
            AutonomyDecisionStatus.APPROVAL_REQUIRED,
            f"effective policy requires approval for boundary: {policy_key}",
        )
    return None


def _budget_exhaustion_reason(policy: AutonomyBudget, request: AutonomyEvaluationInput) -> str | None:
    if request.model_turns_used >= policy.max_model_turns:
        return "model turn budget exhausted"
    if request.tool_calls_used >= policy.max_tool_calls:
        return "tool call budget exhausted"
    if request.side_effect_actions_used >= policy.max_side_effect_actions:
        return "side-effect action budget exhausted"
    if request.runtime_seconds_used >= policy.max_runtime_seconds:
        return "runtime budget exhausted"
    if request.adapter_dispatches_used >= policy.max_adapter_dispatches:
        return "adapter dispatch budget exhausted"
    if request.new_tasks_used >= policy.max_new_tasks:
        return "new task budget exhausted"
    if request.consecutive_failures >= policy.max_consecutive_failures:
        return "consecutive failure budget exhausted"
    if policy.max_cost_usd is not None and request.cost_usd_used is not None and request.cost_usd_used >= policy.max_cost_usd:
        return "cost budget exhausted"
    return None
