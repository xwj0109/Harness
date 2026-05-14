from __future__ import annotations

from pathlib import Path
from typing import Any

from harness.approvals import ApprovalStore
from harness.execution import (
    builtin_execution_adapters,
    evaluate_registered_adapter_security_decision,
    inspect_execution_eligibility,
)
from harness.memory.sqlite_store import SQLiteStore
from harness.models import (
    OrchestrationProgress,
    OrchestrationProgressMode,
    OrchestrationProgressTask,
    SecurityDecisionStatus,
    TaskAttempt,
    TaskLease,
    TaskLeaseStatus,
    TaskRecord,
    TaskStatus,
)
from harness.paths import resolve_project_root
from harness.security import sanitize_for_logging
from harness.security_explanations import explanations_from_reasons


TERMINAL_TASK_STATUSES = {
    TaskStatus.SUCCEEDED,
    TaskStatus.FAILED,
    TaskStatus.CANCELLED,
    TaskStatus.SKIPPED,
}


def build_orchestration_progress(project_root: Path, objective_id: str) -> OrchestrationProgress:
    project_root = resolve_project_root(project_root)
    store = SQLiteStore(project_root)
    objective = store.get_objective(objective_id)
    tasks = store.list_tasks(objective_id=objective_id)
    graph = store.build_task_graph(objective_id=objective_id)
    graph_blocked = graph.get("blocked_reasons", {})
    leases_by_task = _latest_leases_by_task(store, [task.id for task in tasks])
    attempts_by_task = _latest_attempts_by_task(store, [task.id for task in tasks])
    progress_tasks = [
        _task_progress(
            project_root=project_root,
            task=task,
            lease=leases_by_task.get(task.id),
            attempt=attempts_by_task.get(task.id),
            graph_reasons=graph_blocked.get(task.id, []),
        )
        for task in tasks
    ]
    active_lease_ids = [
        item.lease_id
        for item in progress_tasks
        if item.lease_id and leases_by_task.get(item.task_id) and leases_by_task[item.task_id].status == TaskLeaseStatus.ACTIVE
    ]
    active_run_ids = sorted({item.run_id for item in progress_tasks if item.run_id and item.status == TaskStatus.RUNNING})
    blocked_reasons = _dedupe(
        reason
        for item in progress_tasks
        for reason in item.blocked_reasons
    )
    mode = _mode_for_progress(tasks, progress_tasks, active_lease_ids, blocked_reasons)
    next_action = _next_action_for_progress(project_root, objective_id, mode, progress_tasks, active_lease_ids)
    context_warnings = ["memory_not_authority"] if store.list_memory_records() else []
    return OrchestrationProgress(
        project_root=project_root,
        objective_id=objective.id,
        objective_title=str(sanitize_for_logging(objective.title)),
        objective_status=objective.status,
        selected_orchestrator=_selected_orchestrator(objective.metadata),
        mode=mode,
        tasks=progress_tasks,
        active_lease_ids=active_lease_ids,
        active_run_ids=active_run_ids,
        blocked_reasons=blocked_reasons,
        untrusted_context_warnings=context_warnings,
        next_action=next_action,
        equivalent_commands=_equivalent_commands(project_root, objective_id, progress_tasks, active_lease_ids),
    )


def _latest_attempts_by_task(store: SQLiteStore, task_ids: list[str]) -> dict[str, TaskAttempt]:
    latest: dict[str, TaskAttempt] = {}
    for task_id in task_ids:
        attempts = store.list_task_attempts(task_id)
        if attempts:
            latest[task_id] = attempts[-1]
    return latest


def _latest_leases_by_task(store: SQLiteStore, task_ids: list[str]) -> dict[str, TaskLease]:
    latest: dict[str, TaskLease] = {}
    for task_id in task_ids:
        leases = store.list_task_leases(task_id)
        active = [lease for lease in leases if lease.status == TaskLeaseStatus.ACTIVE]
        if active:
            latest[task_id] = active[-1]
        elif leases:
            latest[task_id] = leases[-1]
    return latest


def _task_progress(
    *,
    project_root: Path,
    task: TaskRecord,
    lease: TaskLease | None,
    attempt: TaskAttempt | None,
    graph_reasons: list[dict[str, Any]],
) -> OrchestrationProgressTask:
    adapter_id = _string_metadata(task, "execution_adapter")
    task_type = _string_metadata(task, "task_type")
    run_id = task.run_id or (attempt.run_id if attempt is not None else None)
    blocked_reasons = _graph_reason_strings(graph_reasons)
    blocked_reasons.extend(_metadata_blockers(task, adapter_id, task_type))
    blocked_reasons.extend(_descriptor_approval_blockers(project_root, task, adapter_id, task_type))
    if task.status == TaskStatus.WAITING_APPROVAL and not task.required_approvals:
        blocked_reasons.append("task is waiting for approval")
    if task.status == TaskStatus.BLOCKED and not blocked_reasons:
        blocked_reasons.append("task is blocked")
    if lease is not None and lease.status == TaskLeaseStatus.ACTIVE:
        blocked_reasons.extend(_lease_security_blockers(project_root, lease, task, attempt))
    return OrchestrationProgressTask(
        task_id=task.id,
        title=str(sanitize_for_logging(task.title)),
        status=task.status,
        execution_adapter=adapter_id,
        task_type=task_type,
        attempt_id=attempt.id if attempt is not None else lease.attempt_id if lease is not None else None,
        lease_id=lease.id if lease is not None else None,
        run_id=run_id,
        terminal_decision=_terminal_decision(task, attempt, run_id),
        blocked_reasons=_dedupe(blocked_reasons),
        blocked_state_explanations=explanations_from_reasons(
            _dedupe(blocked_reasons),
            inspect_command=f"harness tasks inspect {task.id} --project {project_root} --output json",
        ),
        next_action=_next_action_for_task(project_root, task, lease, blocked_reasons),
    )


def _graph_reason_strings(reasons: list[dict[str, Any]]) -> list[str]:
    rendered: list[str] = []
    for reason in reasons:
        kind = reason.get("kind")
        if kind == "missing_dependency":
            rendered.append(f"missing dependency task {reason.get('task_id')}")
        elif kind == "unsatisfied_dependency":
            rendered.append(
                f"waiting for dependency {reason.get('task_id')} to succeed; current status={reason.get('status')}"
            )
        elif kind == "unresolved_required_approvals":
            approvals = ", ".join(reason.get("required_approvals") or [])
            rendered.append(f"missing required task approvals: {approvals or 'unknown'}")
        else:
            rendered.append(str(sanitize_for_logging(str(reason))))
    return rendered


def _metadata_blockers(task: TaskRecord, adapter_id: str | None, task_type: str | None) -> list[str]:
    blockers: list[str] = []
    if not adapter_id:
        blockers.append("task metadata is missing execution_adapter")
        return blockers
    adapters = builtin_execution_adapters()
    adapter = adapters.get(adapter_id)
    if adapter is None:
        blockers.append(f"unknown execution adapter: {adapter_id}")
        return blockers
    descriptor = adapter.descriptor
    for key, expected in descriptor.required_task_metadata.items():
        if task.metadata.get(key) != expected:
            blockers.append(f"execution requires {key}={expected}")
    rejected = sorted(key for key in descriptor.rejected_task_metadata if bool(task.metadata.get(key)))
    if rejected:
        blockers.append(f"unsafe task metadata: {', '.join(rejected)}")
    if task_type not in descriptor.supported_task_types:
        blockers.append(f"unsupported task_type for {adapter_id}: {task_type}")
    return blockers


def _descriptor_approval_blockers(
    project_root: Path,
    task: TaskRecord,
    adapter_id: str | None,
    task_type: str | None,
) -> list[str]:
    if not adapter_id or not task_type:
        return []
    adapter = builtin_execution_adapters().get(adapter_id)
    if adapter is None:
        return []
    missing = [
        approval
        for approval in adapter.descriptor.required_approvals
        if ApprovalStore(project_root).find_valid(
            backend=_approval_backend(approval),
            data_boundary=_approval_data_boundary(approval),
            task_type=task_type,
            adapter_id=adapter_id,
            workbench_id=task.workbench_id,
            objective_id=task.objective_id,
        )
        is None
    ]
    if not missing:
        return []
    return [f"missing required adapter approval: {', '.join(missing)}"]


def _lease_security_blockers(
    project_root: Path,
    lease: TaskLease,
    task: TaskRecord,
    attempt: TaskAttempt | None,
) -> list[str]:
    blockers: list[str] = []
    eligibility = inspect_execution_eligibility(project_root, lease, task, attempt)
    if not eligibility.get("eligible"):
        blockers.extend(str(sanitize_for_logging(str(item))) for item in eligibility.get("rejection_reasons", []))
    decision = evaluate_registered_adapter_security_decision(project_root, lease, task, attempt, owner=lease.owner)
    if decision.decision != SecurityDecisionStatus.ALLOW:
        blockers.extend(str(sanitize_for_logging(reason)) for reason in decision.reasons)
        if decision.missing_approvals:
            blockers.append(f"security decision requires approval: {', '.join(decision.missing_approvals)}")
        elif decision.reason_code:
            blockers.append(f"security decision {decision.decision.value}: {decision.reason_code}")
    return blockers


def _mode_for_progress(
    tasks: list[TaskRecord],
    progress_tasks: list[OrchestrationProgressTask],
    active_lease_ids: list[str],
    blocked_reasons: list[str],
) -> OrchestrationProgressMode:
    if not tasks:
        return OrchestrationProgressMode.IDLE
    if all(task.status in TERMINAL_TASK_STATUSES for task in tasks):
        return OrchestrationProgressMode.TERMINAL
    if active_lease_ids:
        return OrchestrationProgressMode.LEASED
    if blocked_reasons or any(task.status in {TaskStatus.BLOCKED, TaskStatus.WAITING_APPROVAL} for task in tasks):
        return OrchestrationProgressMode.BLOCKED
    if any(task.status == TaskStatus.RUNNING for task in tasks):
        return OrchestrationProgressMode.DISPATCHING
    if any(task.status == TaskStatus.READY and not item.blocked_reasons for task, item in zip(tasks, progress_tasks, strict=True)):
        return OrchestrationProgressMode.READY
    return OrchestrationProgressMode.BLOCKED


def _next_action_for_progress(
    project_root: Path,
    objective_id: str,
    mode: OrchestrationProgressMode,
    tasks: list[OrchestrationProgressTask],
    active_lease_ids: list[str],
) -> str:
    if active_lease_ids:
        lease_id = active_lease_ids[0]
        return (
            f"harness daemon inspect-lease {lease_id} --project {project_root} --output json; "
            f"harness daemon execute {lease_id} --project {project_root} --output json"
        )
    if mode == OrchestrationProgressMode.READY:
        return f"harness daemon run-once --project {project_root} --output json"
    if mode == OrchestrationProgressMode.TERMINAL:
        return "Inspect runs/artifacts or create a follow-up objective."
    if mode == OrchestrationProgressMode.IDLE:
        return f"Create tasks for objective {objective_id}."
    blocked = next((task for task in tasks if task.blocked_reasons), None)
    if blocked is not None:
        reason_text = "; ".join(blocked.blocked_reasons)
        if "approval" in reason_text:
            return "Review required approvals, then re-run progress."
        if "adapter" in reason_text or "metadata" in reason_text:
            return f"harness tasks inspect {blocked.task_id} --project {project_root} --output json"
    return f"harness tasks graph --objective {objective_id} --project {project_root} --output json"


def _next_action_for_task(
    project_root: Path,
    task: TaskRecord,
    lease: TaskLease | None,
    blocked_reasons: list[str],
) -> str | None:
    if lease is not None and lease.status == TaskLeaseStatus.ACTIVE:
        return (
            f"harness daemon inspect-lease {lease.id} --project {project_root} --output json; "
            f"harness daemon execute {lease.id} --project {project_root} --output json"
        )
    if task.status in TERMINAL_TASK_STATUSES:
        return "Inspect linked run/artifacts."
    if blocked_reasons:
        if any("approval" in reason for reason in blocked_reasons):
            return "Resolve the required approval before execution."
        return f"harness tasks inspect {task.id} --project {project_root} --output json"
    if task.status == TaskStatus.READY:
        return f"harness daemon run-once --project {project_root} --output json"
    return None


def _equivalent_commands(
    project_root: Path,
    objective_id: str,
    tasks: list[OrchestrationProgressTask],
    active_lease_ids: list[str],
) -> list[str]:
    commands = [
        f"harness progress --objective {objective_id} --project {project_root} --output json",
        f"harness tasks graph --objective {objective_id} --project {project_root} --output json",
    ]
    if active_lease_ids:
        lease_id = active_lease_ids[0]
        commands.extend(
            [
                f"harness daemon inspect-lease {lease_id} --project {project_root} --output json",
                f"harness daemon execute {lease_id} --project {project_root} --output json",
            ]
        )
    elif any(task.status == TaskStatus.READY and not task.blocked_reasons for task in tasks):
        commands.append(f"harness daemon run-once --project {project_root} --output json")
    return commands


def _terminal_decision(task: TaskRecord, attempt: TaskAttempt | None, run_id: str | None) -> str | None:
    if task.status not in TERMINAL_TASK_STATUSES:
        return None
    if attempt is not None and attempt.failure_code:
        return attempt.failure_code
    if run_id:
        return f"linked_run:{run_id}"
    return task.status.value


def _selected_orchestrator(metadata: dict[str, Any]) -> str | None:
    value = metadata.get("orchestrator_id") or metadata.get("selected_orchestrator")
    return str(sanitize_for_logging(str(value))) if value is not None else None


def _string_metadata(task: TaskRecord, key: str) -> str | None:
    value = task.metadata.get(key)
    return str(sanitize_for_logging(str(value))) if isinstance(value, str) and value else None


def _approval_backend(approval: str) -> str:
    if approval == "hosted_provider_codex":
        return "codex_cli"
    return approval


def _approval_data_boundary(approval: str) -> str:
    if approval == "hosted_provider_codex":
        return "hosted_provider"
    return approval


def _dedupe(items: Any) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        value = str(sanitize_for_logging(str(item)))
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result
