from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from harness.models import (
    DelegateBudgetPolicy,
    ExecutionAdapterDescriptor,
    SandboxActiveRepoWritePolicy,
    SandboxNetworkPolicy,
    SandboxProfileDescriptor,
    SandboxTier,
)
from harness.sandbox_profiles import get_sandbox_profile


DELEGATE_BUDGET_SCHEMA_VERSION = "harness.delegate_budget/v1"
NUMERIC_BUDGET_METADATA_KEYS = {
    "timeout": "timeout_seconds",
    "timeout_seconds": "timeout_seconds",
    "max_runtime_invocations": "max_runtime_invocations",
    "max_model_calls": "max_model_calls",
    "max_tool_calls": "max_tool_calls",
    "max_parallel_branches": "max_parallel_branches",
    "max_input_tokens": "max_input_tokens",
    "max_output_tokens": "max_output_tokens",
    "max_cost_usd": "max_cost_usd",
    "cpu_seconds": "max_cpu_seconds",
    "max_cpu_seconds": "max_cpu_seconds",
    "memory_mb": "max_memory_mb",
    "max_memory_mb": "max_memory_mb",
}
NUMERIC_BUDGET_METADATA_MINIMUMS = {
    "max_parallel_branches": Decimal("1"),
}


def adapter_delegate_budget_projection(descriptor: ExecutionAdapterDescriptor) -> dict[str, Any]:
    budget = descriptor.delegate_budget
    profile, profile_error = _sandbox_profile(descriptor)
    gaps = validate_adapter_delegate_budget(descriptor, profile=profile, profile_error=profile_error)
    return {
        "adapter_id": descriptor.id,
        "budget": budget.model_dump(mode="json"),
        "sandbox_profile_id": descriptor.sandbox_profile_id,
        "sandbox_tier": profile.tier.value if profile is not None else None,
        "sandbox_network": profile.network.value if profile is not None else None,
        "sandbox_active_repo_write": profile.active_repo_write.value if profile is not None else None,
        "gaps": gaps,
        "budget_limited": not gaps,
    }


def validate_adapter_delegate_budget(
    descriptor: ExecutionAdapterDescriptor,
    *,
    profile: SandboxProfileDescriptor | None = None,
    profile_error: str | None = None,
) -> list[str]:
    budget = descriptor.delegate_budget
    gaps: list[str] = []

    if budget.schema_version != DELEGATE_BUDGET_SCHEMA_VERSION:
        gaps.append(f"delegate_budget schema_version must be {DELEGATE_BUDGET_SCHEMA_VERSION}")
    if budget.timeout_seconds <= 0:
        gaps.append("delegate_budget timeout_seconds must be positive")
    if budget.max_cpu_seconds is None:
        gaps.append("delegate_budget max_cpu_seconds must be explicit")
    if budget.max_memory_mb is None:
        gaps.append("delegate_budget max_memory_mb must be explicit")
    if budget.max_runtime_invocations and budget.max_cpu_seconds is not None and budget.max_cpu_seconds <= 0:
        gaps.append("delegate_budget max_cpu_seconds must be positive when runtime invocation is allowed")
    if budget.max_runtime_invocations and budget.max_memory_mb is not None and budget.max_memory_mb <= 0:
        gaps.append("delegate_budget max_memory_mb must be positive when runtime invocation is allowed")
    if budget.max_parallel_branches != 1:
        gaps.append("execution adapter delegate_budget must keep max_parallel_branches=1; graph fan-out belongs to the scheduler")
    if budget.max_runtime_invocations > 1:
        gaps.append("execution adapter delegate_budget must allow at most one runtime invocation per lease")
    if budget.cost_policy == "record_only" and (
        budget.max_runtime_invocations or budget.max_model_calls or budget.max_tool_calls
    ):
        gaps.append("record_only delegate_budget cannot allow runtime, model, or tool calls")
    if budget.cost_policy == "paid_cost_cap" and budget.max_cost_usd is None:
        gaps.append("paid_cost_cap delegate_budget requires max_cost_usd")
    if budget.max_tool_calls and not budget.tool_allowlist:
        gaps.append("delegate_budget with tool calls must declare a tool_allowlist")
    if descriptor.required_approvals and budget.cost_policy in {"record_only", "local_no_api_cost"}:
        gaps.append("approval-gated adapters must use a hosted/subscription/provider/capped cost policy")
    if descriptor.autonomy_default == "forbidden" and budget.max_runtime_invocations:
        gaps.append("forbidden-autonomy adapters cannot allow runtime invocation through their default budget")

    if profile_error:
        gaps.append(profile_error)
    if profile is not None:
        gaps.extend(_sandbox_alignment_gaps(budget, profile))

    return gaps


def task_delegate_budget_rejection_reasons(
    descriptor: ExecutionAdapterDescriptor,
    metadata: dict[str, Any],
) -> list[str]:
    budget = descriptor.delegate_budget
    reasons: list[str] = []
    for metadata_key, budget_key in NUMERIC_BUDGET_METADATA_KEYS.items():
        if metadata_key not in metadata:
            continue
        requested = _decimal(metadata.get(metadata_key))
        if requested is None:
            reasons.append(f"Task metadata {metadata_key} must be numeric when supplied.")
            continue
        minimum = NUMERIC_BUDGET_METADATA_MINIMUMS.get(metadata_key, Decimal("0"))
        if requested < minimum:
            reasons.append(f"Task metadata {metadata_key}={metadata.get(metadata_key)} must be at least {minimum}.")
            continue
        budget_value = getattr(budget, budget_key)
        if budget_value is None:
            continue
        limit = _decimal(budget_value)
        if limit is None:
            reasons.append(f"{descriptor.id} delegate budget {budget_key} must be numeric when supplied.")
            continue
        if requested > limit:
            reasons.append(
                f"Task metadata {metadata_key}={metadata.get(metadata_key)} exceeds "
                f"{descriptor.id} delegate budget {budget_key}={budget_value}."
            )

    if "network_policy" in metadata and metadata.get("network_policy") != budget.network_policy.value:
        reasons.append(
            f"Task metadata network_policy={metadata.get('network_policy')} conflicts with "
            f"{descriptor.id} delegate budget network_policy={budget.network_policy.value}."
        )
    if "external_network" in metadata and metadata.get("external_network") != budget.network_policy.value:
        reasons.append(
            f"Task metadata external_network={metadata.get('external_network')} conflicts with "
            f"{descriptor.id} delegate budget network_policy={budget.network_policy.value}."
        )
    if "active_repo_write" in metadata and metadata.get("active_repo_write") != budget.active_repo_write.value:
        reasons.append(
            f"Task metadata active_repo_write={metadata.get('active_repo_write')} conflicts with "
            f"{descriptor.id} delegate budget active_repo_write={budget.active_repo_write.value}."
        )
    if "filesystem_scope" in metadata and metadata.get("filesystem_scope") != budget.filesystem_scope:
        reasons.append(
            f"Task metadata filesystem_scope={metadata.get('filesystem_scope')} conflicts with "
            f"{descriptor.id} delegate budget filesystem_scope={budget.filesystem_scope}."
        )
    return reasons


def _sandbox_alignment_gaps(
    budget: DelegateBudgetPolicy,
    profile: SandboxProfileDescriptor,
) -> list[str]:
    gaps: list[str] = []
    if budget.network_policy != profile.network:
        gaps.append(
            f"delegate_budget network_policy={budget.network_policy.value} does not match sandbox profile network={profile.network.value}"
        )
    if budget.active_repo_write != profile.active_repo_write:
        gaps.append(
            "delegate_budget active_repo_write="
            f"{budget.active_repo_write.value} does not match sandbox profile active_repo_write={profile.active_repo_write.value}"
        )
    if profile.network == SandboxNetworkPolicy.FORBIDDEN and budget.network_policy != SandboxNetworkPolicy.FORBIDDEN:
        gaps.append("delegate_budget cannot allow network when the sandbox profile forbids it")
    if (
        profile.active_repo_write == SandboxActiveRepoWritePolicy.FORBIDDEN
        and budget.active_repo_write != SandboxActiveRepoWritePolicy.FORBIDDEN
    ):
        gaps.append("delegate_budget cannot allow active repo writes when the sandbox profile forbids them")
    if profile.tier == SandboxTier.READ_ONLY and budget.filesystem_scope != "project_read_only":
        gaps.append("read-only sandbox adapters must use filesystem_scope=project_read_only")
    if profile.tier == SandboxTier.ISOLATED_WORKSPACE and budget.filesystem_scope != "isolated_workspace":
        gaps.append("isolated workspace adapters must use filesystem_scope=isolated_workspace")
    if profile.tier == SandboxTier.NONE and budget.filesystem_scope not in {"harness_artifacts", "session_policy"}:
        gaps.append("none sandbox adapters must stay within harness_artifacts or session_policy filesystem scope")
    return gaps


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _sandbox_profile(
    descriptor: ExecutionAdapterDescriptor,
) -> tuple[SandboxProfileDescriptor | None, str | None]:
    if not descriptor.sandbox_profile_id:
        return None, "adapter is missing sandbox_profile_id"
    try:
        return get_sandbox_profile(descriptor.sandbox_profile_id), None
    except KeyError:
        return None, f"adapter references unknown sandbox_profile_id={descriptor.sandbox_profile_id}"
