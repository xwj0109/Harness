from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel

from harness.models import (
    BackendDescriptor,
    BillingMode,
    DataBoundary,
    EffectivePolicy,
    ExecutionLocation,
    PolicyLevel,
    PolicySource,
    RunMode,
    RunRecord,
    TaskRecord,
    run_mode_for_task_type,
)
from harness.registry import SpecRegistry
from harness.specs import ToolPermission

POLICY_KEYS = (
    "local_filesystem",
    "active_repo_write",
    "hosted_boundary",
    "external_network",
    "docker_execution",
    "paid_provider",
    "task_queue_execution",
    "background_scheduling",
)

_STRICTNESS = {
    PolicyLevel.ALLOWED: 0,
    PolicyLevel.APPROVAL_REQUIRED: 1,
    PolicyLevel.FORBIDDEN: 2,
}


def stricter_policy_level(left: PolicyLevel, right: PolicyLevel) -> PolicyLevel:
    return left if _STRICTNESS[left] >= _STRICTNESS[right] else right


def stable_json_sha256(value: Any, *, exclude_resolved_at: bool = False) -> str:
    sanitized = _to_jsonable(value)
    if exclude_resolved_at and isinstance(sanitized, dict):
        sanitized = dict(sanitized)
        sanitized.pop("resolved_at", None)
    payload = json.dumps(sanitized, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def effective_policy_sha256(policy: EffectivePolicy) -> str:
    return stable_json_sha256(policy, exclude_resolved_at=True)


def backend_descriptor_sha256(descriptor: BackendDescriptor | None) -> str | None:
    if descriptor is None:
        return None
    return stable_json_sha256(descriptor)


def resolve_run_effective_policy(
    run: RunRecord,
    backend_descriptor: BackendDescriptor | None,
) -> EffectivePolicy:
    run_mode = run_mode_for_task_type(run.task_type)
    sources = [
        _base_source(),
        _run_mode_source(run_mode),
        _task_type_source(run.task_type),
    ]
    if run.approval_id:
        sources.append(
            PolicySource(
                kind="approval",
                id=run.approval_id,
                description="Run has an associated approval id.",
                levels={"hosted_boundary": PolicyLevel.APPROVAL_REQUIRED},
                required_approvals=[run.approval_id],
            )
        )
    if backend_descriptor is not None:
        sources.append(_backend_source(backend_descriptor))
    return _build_policy("run", run.id, sources)


def resolve_task_effective_policy(task: TaskRecord) -> EffectivePolicy:
    sources = [
        _base_source(),
        PolicySource(
            kind="task",
            id=task.id,
            description="Manual queue task metadata.",
            levels={
                "local_filesystem": PolicyLevel.ALLOWED,
                "active_repo_write": PolicyLevel.APPROVAL_REQUIRED,
                "task_queue_execution": PolicyLevel.FORBIDDEN,
                "background_scheduling": PolicyLevel.FORBIDDEN,
            },
            required_approvals=sorted(set(task.required_approvals)),
        ),
    ]
    if task.required_approvals:
        sources.append(
            PolicySource(
                kind="approval_metadata",
                id=task.id,
                description="Task records unresolved required approvals.",
                levels={"hosted_boundary": PolicyLevel.APPROVAL_REQUIRED},
                required_approvals=sorted(set(task.required_approvals)),
            )
        )
    if task.agent_id:
        sources.append(
            PolicySource(
                kind="agent_ref",
                id=task.agent_id,
                description="Task references a built-in agent id.",
                levels={},
            )
        )
    if task.workbench_id:
        sources.append(
            PolicySource(
                kind="workbench_ref",
                id=task.workbench_id,
                description="Task references a built-in workbench id.",
                levels={},
            )
        )
    return _build_policy("task", task.id, sources)


def resolve_agent_effective_policy(registry: SpecRegistry, agent_id: str) -> EffectivePolicy:
    agent = registry.get_agent(agent_id)
    tool_policy = registry.tool_policies[agent.tool_policy]
    sources = [
        _base_source(),
        _tool_policy_source(f"agent:{agent.id}:tool_policy:{agent.tool_policy}", tool_policy),
        PolicySource(
            kind="agent",
            id=agent.id,
            description="Built-in agent declaration.",
            levels={"background_scheduling": PolicyLevel.FORBIDDEN},
        ),
    ]
    return _build_policy("agent", agent.id, sources)


def resolve_workbench_effective_policy(registry: SpecRegistry, workbench_id: str) -> EffectivePolicy:
    workbench = registry.get_workbench(workbench_id)
    sources = [
        _base_source(),
        PolicySource(
            kind="workbench",
            id=workbench.id,
            description="Built-in workbench declaration.",
            levels=_workbench_forbidden_levels(workbench.forbidden_actions),
        ),
    ]
    for key, value in sorted(workbench.approval_policy.items()):
        sources.append(
            PolicySource(
                kind="workbench_approval_policy",
                id=f"{workbench.id}:{key}",
                description="Workbench-local approval policy.",
                levels={key: _from_tool_permission(value)},
            )
        )
    return _build_policy("workbench", workbench.id, sources)


def resolve_backend_effective_policy(descriptor: BackendDescriptor) -> EffectivePolicy:
    return _build_policy("backend", descriptor.name, [_base_source(), _backend_source(descriptor)])


def _build_policy(subject_kind: str, subject_id: str, sources: list[PolicySource]) -> EffectivePolicy:
    levels = {key: PolicyLevel.ALLOWED for key in POLICY_KEYS}
    required_approvals: set[str] = set()
    forbidden_reasons: list[str] = []
    for source in sources:
        required_approvals.update(source.required_approvals)
        for key, level in source.levels.items():
            levels[key] = stricter_policy_level(levels.get(key, PolicyLevel.ALLOWED), level)
            if level == PolicyLevel.FORBIDDEN and source.description:
                forbidden_reasons.append(f"{key}: {source.description}")
    return EffectivePolicy(
        subject_kind=subject_kind,
        subject_id=subject_id,
        resolved_at=datetime.now(timezone.utc),
        levels=dict(sorted(levels.items())),
        sources=sources,
        required_approvals=sorted(required_approvals),
        forbidden_reasons=sorted(set(forbidden_reasons)),
        monotonicity_checked=True,
    )


def _base_source() -> PolicySource:
    return PolicySource(
        kind="harness_default",
        id="local_first_safety",
        description="Harness local-first safety defaults.",
        levels={
            "local_filesystem": PolicyLevel.ALLOWED,
            "active_repo_write": PolicyLevel.APPROVAL_REQUIRED,
            "hosted_boundary": PolicyLevel.APPROVAL_REQUIRED,
            "external_network": PolicyLevel.FORBIDDEN,
            "docker_execution": PolicyLevel.APPROVAL_REQUIRED,
            "paid_provider": PolicyLevel.FORBIDDEN,
            "task_queue_execution": PolicyLevel.FORBIDDEN,
            "background_scheduling": PolicyLevel.FORBIDDEN,
        },
    )


def _run_mode_source(run_mode: RunMode) -> PolicySource:
    levels = {
        "active_repo_write": PolicyLevel.FORBIDDEN,
        "docker_execution": PolicyLevel.FORBIDDEN,
        "task_queue_execution": PolicyLevel.FORBIDDEN,
        "background_scheduling": PolicyLevel.FORBIDDEN,
    }
    if run_mode in {RunMode.LOCAL_EDIT, RunMode.CODEX_EDIT}:
        levels["active_repo_write"] = PolicyLevel.APPROVAL_REQUIRED
    if run_mode == RunMode.TEST:
        levels["docker_execution"] = PolicyLevel.APPROVAL_REQUIRED
    if run_mode == RunMode.CODEX_EDIT:
        levels["hosted_boundary"] = PolicyLevel.APPROVAL_REQUIRED
    return PolicySource(
        kind="run_mode",
        id=run_mode.value,
        description=f"Run mode {run_mode.value}.",
        levels=levels,
    )


def _task_type_source(task_type: str | None) -> PolicySource:
    levels: dict[str, PolicyLevel] = {}
    required_approvals: list[str] = []
    if task_type == "codex_code_edit":
        levels["hosted_boundary"] = PolicyLevel.APPROVAL_REQUIRED
        levels["active_repo_write"] = PolicyLevel.APPROVAL_REQUIRED
        required_approvals.append("hosted_provider")
    elif task_type == "docker_run_tests":
        levels["docker_execution"] = PolicyLevel.APPROVAL_REQUIRED
        required_approvals.append("docker_execution")
    return PolicySource(
        kind="task_type",
        id=task_type or "unknown",
        description="Run task type policy.",
        levels=levels,
        required_approvals=required_approvals,
    )


def _backend_source(descriptor: BackendDescriptor) -> PolicySource:
    levels = {
        "external_network": PolicyLevel.APPROVAL_REQUIRED
        if descriptor.metadata.allow_network
        else PolicyLevel.FORBIDDEN,
        "hosted_boundary": PolicyLevel.APPROVAL_REQUIRED
        if descriptor.metadata.data_boundary != DataBoundary.LOCAL_ONLY
        else PolicyLevel.ALLOWED,
        "paid_provider": PolicyLevel.FORBIDDEN
        if descriptor.metadata.billing_mode == BillingMode.PAID_API
        else PolicyLevel.ALLOWED,
    }
    if descriptor.metadata.execution_location in {ExecutionLocation.HOSTED, ExecutionLocation.MIXED}:
        levels["hosted_boundary"] = PolicyLevel.APPROVAL_REQUIRED
    required_approvals = []
    if levels["hosted_boundary"] == PolicyLevel.APPROVAL_REQUIRED:
        required_approvals.append("hosted_provider")
    if levels["external_network"] == PolicyLevel.APPROVAL_REQUIRED:
        required_approvals.append("external_network")
    return PolicySource(
        kind="backend",
        id=descriptor.name,
        description="Configured backend descriptor boundary metadata.",
        levels=levels,
        required_approvals=required_approvals,
    )


def _tool_policy_source(source_id: str, tool_policy) -> PolicySource:
    levels = {
        "active_repo_write": _from_tool_permission(tool_policy.active_repo_write),
        "external_network": _from_tool_permission(tool_policy.network),
        "hosted_boundary": _from_tool_permission(tool_policy.hosted_boundary),
    }
    if tool_policy.tools.get("docker_tests") is not None:
        levels["docker_execution"] = _from_tool_permission(tool_policy.tools["docker_tests"])
    return PolicySource(
        kind="tool_policy",
        id=source_id,
        description="Built-in declarative tool policy.",
        levels=levels,
    )


def _workbench_forbidden_levels(forbidden_actions: list[str]) -> dict[str, PolicyLevel]:
    levels: dict[str, PolicyLevel] = {}
    for action in forbidden_actions:
        if action in {"paid_api_fallback", "hosted_fallback"}:
            levels["paid_provider"] = PolicyLevel.FORBIDDEN
            levels["hosted_boundary"] = PolicyLevel.APPROVAL_REQUIRED
        elif action in {"live_trading", "broker_action", "capital_allocation"}:
            levels["external_network"] = PolicyLevel.FORBIDDEN
        elif action in {"email_send", "application_submit", "external_message_send"}:
            levels["external_network"] = PolicyLevel.FORBIDDEN
    levels["background_scheduling"] = PolicyLevel.FORBIDDEN
    return levels


def _from_tool_permission(permission: ToolPermission) -> PolicyLevel:
    return PolicyLevel(permission.value)


def _to_jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(key): _to_jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [_to_jsonable(item) for item in value]
    return value
