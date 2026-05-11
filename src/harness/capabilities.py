from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from harness.execution import list_execution_adapter_descriptors
from harness.memory.sqlite_store import SQLiteStore
from harness.models import (
    BreakerStatus,
    CapabilityCatalog,
    CapabilityRecord,
    ExecutionAdapterDescriptor,
    KillSwitchRecord,
    KillSwitchTargetKind,
)
from harness.paths import resolve_project_root
from harness.sandbox_profiles import sandbox_profile_dict
from harness.security import sanitize_for_logging
from harness.security_explanations import explanations_from_reasons


def build_capability_catalog(project_root: Path) -> CapabilityCatalog:
    """Build a read-only capability catalog from registered adapter descriptors."""

    resolved_root = resolve_project_root(project_root)
    controls, breaker_by_adapter = _runtime_control_snapshot(resolved_root)
    capabilities = []
    for descriptor in list_execution_adapter_descriptors():
        capability = _capability_from_descriptor(descriptor, resolved_root)
        blocked = _capability_block_reasons(descriptor, controls)
        breaker = breaker_by_adapter.get(descriptor.id)
        if breaker is not None and breaker.status == BreakerStatus.OPEN:
            blocked.append(
                f"breaker_open: {breaker.failure_count}/{breaker.threshold} failures in {breaker.window_seconds} seconds"
            )
        if blocked:
            capability.readiness = "unavailable"
            capability.readiness_reasons = [str(sanitize_for_logging(reason)) for reason in blocked]
            capability.blocked_state_explanations = explanations_from_reasons(
                blocked,
                inspect_command=f"harness capabilities inspect {descriptor.id} --project {resolved_root} --output json",
            )
        capabilities.append(capability)
    return CapabilityCatalog(project_root=resolved_root, capabilities=capabilities)


def get_capability(project_root: Path, capability_id: str) -> CapabilityRecord:
    catalog = build_capability_catalog(project_root)
    for capability in catalog.capabilities:
        if capability.id == capability_id:
            return capability
    raise KeyError(f"Capability not found: {capability_id}")


def _capability_from_descriptor(descriptor: ExecutionAdapterDescriptor, project_root: Path) -> CapabilityRecord:
    readiness_reasons = ["Registered adapter descriptor is available."]
    if descriptor.supported_task_types:
        readiness = "ready_for_task_drafting"
        readiness_reasons.append("One or more supported task types are declared.")
    else:
        readiness = "missing_supported_task_type"
        readiness_reasons.append("No supported task types are declared.")
    if descriptor.required_approvals:
        readiness = "requires_approval_before_execution"
        readiness_reasons.append("Execution requires explicit approval before run creation.")

    task_type = descriptor.supported_task_types[0] if descriptor.supported_task_types else "<task_type>"
    return CapabilityRecord(
        id=descriptor.id,
        title=_title_from_id(descriptor.id),
        description=descriptor.description,
        execution_adapter=descriptor.id,
        supported_task_types=list(descriptor.supported_task_types),
        required_approvals=list(descriptor.required_approvals),
        backend_requirements=list(descriptor.backend_requirements),
        sandbox_requirements=list(descriptor.sandbox_requirements),
        sandbox_profile=sandbox_profile_dict(descriptor.sandbox_profile_id),
        side_effect_summary=descriptor.side_effect_summary,
        replay_policy=descriptor.replay_policy,
        readiness=readiness,
        readiness_reasons=readiness_reasons,
        safety_notes=[
            *descriptor.safety_notes,
            "Capability catalog entries are documentation and validation metadata, not permission grants.",
        ],
        equivalent_commands=[
            (
                f"harness tasks add --title \"{_title_from_id(descriptor.id)}\" "
                f"--execution-adapter {descriptor.id} --task-type {task_type} "
                f"--project {project_root} --output json"
            ),
            f"harness daemon run-once --project {project_root} --output json",
            f"harness daemon execute <lease_id> --project {project_root} --output json",
        ],
    )


def _title_from_id(value: str) -> str:
    return value.replace("_", " ").replace("-", " ").title()


def _runtime_control_snapshot(project_root: Path) -> tuple[list[KillSwitchRecord], dict[str, Any]]:
    db_path = project_root / ".harness" / "harness.sqlite"
    if not db_path.exists():
        return [], {}
    try:
        store = SQLiteStore(project_root)
        controls = store.active_execution_controls()
        adapter_ids = [descriptor.id for descriptor in list_execution_adapter_descriptors()]
        breakers = {state.adapter_id: state for state in store.list_adapter_breaker_states(adapter_ids)}
        return controls, breakers
    except Exception as exc:
        reason = str(sanitize_for_logging(f"control_state_unavailable: {exc}"))
        controls = [
            KillSwitchRecord(
                id="control_state_unavailable",
                target_kind=KillSwitchTargetKind.HOSTED_BOUNDARY,
                target_id="*",
                disabled=True,
                reason=reason,
                actor="system",
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
        ]
        return controls, {}


def _capability_block_reasons(descriptor: ExecutionAdapterDescriptor, controls: list[KillSwitchRecord]) -> list[str]:
    reasons: list[str] = []
    task_type = descriptor.supported_task_types[0] if descriptor.supported_task_types else None
    for control in controls:
        if not control.disabled:
            continue
        if _control_matches_descriptor(control, descriptor, task_type):
            reasons.append(f"control_disabled: {control.target_kind.value}:{control.target_id}. {control.reason}")
    return reasons


def _control_matches_descriptor(
    control: KillSwitchRecord,
    descriptor: ExecutionAdapterDescriptor,
    task_type: str | None,
) -> bool:
    target = control.target_id or "*"
    if control.target_kind == KillSwitchTargetKind.ADAPTER:
        return target in {"*", descriptor.id}
    if control.target_kind == KillSwitchTargetKind.TASK_TYPE:
        return target == "*" or target == task_type or target in descriptor.supported_task_types
    if control.target_kind == KillSwitchTargetKind.BACKEND:
        return target in {"*", "codex_cli"} and any("codex_cli" in item for item in descriptor.backend_requirements)
    if control.target_kind == KillSwitchTargetKind.HOSTED_BOUNDARY:
        return target == "*" and (
            "hosted_provider_codex" in descriptor.required_approvals
            or any("data_boundary=hosted_provider" in item for item in descriptor.backend_requirements)
        )
    if control.target_kind == KillSwitchTargetKind.DOCKER_EXECUTION:
        return target == "*" and (task_type == "docker_run_tests" or "docker" in descriptor.id)
    return False
