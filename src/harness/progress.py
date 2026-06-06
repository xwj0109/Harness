from __future__ import annotations

from pathlib import Path
from typing import Any

from harness.approvals import ApprovalStore
from harness.config import HARNESS_DIR
from harness.execution import (
    builtin_execution_adapters,
    evaluate_registered_adapter_security_decision,
    inspect_execution_eligibility,
)
from harness.memory.sqlite_store import SQLiteStore
from harness.models import (
    ObjectiveStatus,
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
from harness.objective_checkpoints import ObjectiveCheckpointGate, evaluate_objective_checkpoint_gate
from harness.objective_evidence import read_objective_evidence_events, verify_objective_evidence
from harness.paths import resolve_project_root
from harness.security import sanitize_for_logging
from harness.security_explanations import explanations_from_reasons


TERMINAL_TASK_STATUSES = {
    TaskStatus.SUCCEEDED,
    TaskStatus.FAILED,
    TaskStatus.CANCELLED,
    TaskStatus.SKIPPED,
}

TERMINAL_OBJECTIVE_STATUSES = {
    ObjectiveStatus.COMPLETED,
    ObjectiveStatus.CANCELLED,
    ObjectiveStatus.TIMED_OUT,
}
CREATED_OBJECTIVE_REASON = "objective_created: start objective before dispatch"
SUSPENDED_OBJECTIVE_REASON = "objective_suspended: resume objective before dispatch"
WAITING_APPROVAL_OBJECTIVE_REASON = "objective_waiting_approval: approve required objective checkpoints before dispatch"
RETRYING_OBJECTIVE_REASON = "objective_retrying: finish or resume objective retry before dispatch"


def build_orchestration_progress(project_root: Path, objective_id: str) -> OrchestrationProgress:
    project_root = resolve_project_root(project_root)
    store = SQLiteStore(project_root)
    objective = store.get_objective(objective_id)
    tasks = store.list_tasks(objective_id=objective_id)
    checkpoint_gate = evaluate_objective_checkpoint_gate(project_root, objective_id)
    graph = store.build_task_graph(objective_id=objective_id)
    graph_blocked = graph.get("blocked_reasons", {})
    leases_by_task = _latest_leases_by_task(store, [task.id for task in tasks])
    attempts_by_task = _latest_attempts_by_task(store, [task.id for task in tasks])
    progress_tasks = [
        _task_progress(
            project_root=project_root,
            store=store,
            task=task,
            lease=leases_by_task.get(task.id),
            attempt=attempts_by_task.get(task.id),
            graph_reasons=graph_blocked.get(task.id, []),
            objective_id=objective.id,
            objective_status=objective.status,
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
        [
            *(reason for item in progress_tasks for reason in item.blocked_reasons),
            *checkpoint_gate.reasons,
        ]
    )
    mode = _mode_for_progress(objective.status, tasks, progress_tasks, active_lease_ids, blocked_reasons)
    objective_evidence = _objective_evidence_summary(project_root, objective.id)
    has_objective_evidence = objective_evidence is not None
    next_action = _next_action_for_progress(
        project_root,
        objective_id,
        mode,
        progress_tasks,
        active_lease_ids,
        has_objective_evidence=has_objective_evidence,
        checkpoint_gate=checkpoint_gate,
        objective_status=objective.status,
    )
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
        checkpoints=_checkpoint_progress_summary(checkpoint_gate),
        objective_evidence=objective_evidence,
        next_action=next_action,
        equivalent_commands=_equivalent_commands(
            project_root,
            objective_id,
            progress_tasks,
            active_lease_ids,
            has_objective_evidence=has_objective_evidence,
            checkpoint_gate=checkpoint_gate,
            objective_status=objective.status,
        ),
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
    store: SQLiteStore,
    task: TaskRecord,
    lease: TaskLease | None,
    attempt: TaskAttempt | None,
    graph_reasons: list[dict[str, Any]],
    objective_id: str,
    objective_status: ObjectiveStatus,
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
    else:
        eligibility = store.daemon_task_eligibility(task)
        blocked_reasons.extend(_daemon_eligibility_blockers(eligibility, existing=blocked_reasons))
    if objective_status == ObjectiveStatus.CREATED:
        blocked_reasons.append(CREATED_OBJECTIVE_REASON)
    if objective_status == ObjectiveStatus.SUSPENDED:
        blocked_reasons.append(SUSPENDED_OBJECTIVE_REASON)
    if objective_status == ObjectiveStatus.WAITING_APPROVAL:
        blocked_reasons.append(WAITING_APPROVAL_OBJECTIVE_REASON)
    if objective_status == ObjectiveStatus.RETRYING:
        blocked_reasons.append(RETRYING_OBJECTIVE_REASON)
    next_action = _next_action_for_task(project_root, task, lease, blocked_reasons)
    if objective_status == ObjectiveStatus.CREATED:
        next_action = f"harness objectives start {objective_id} --project {project_root} --output json"
    elif objective_status == ObjectiveStatus.SUSPENDED:
        next_action = f"harness objectives resume {objective_id} --project {project_root} --output json"
    elif objective_status == ObjectiveStatus.WAITING_APPROVAL:
        next_action = (
            f"harness objectives checkpoints gate {objective_id} --project {project_root} --output json; "
            f"harness objectives checkpoints list {objective_id} --project {project_root} --output json"
        )
    elif objective_status == ObjectiveStatus.RETRYING:
        next_action = f"harness objectives resume {objective_id} --project {project_root} --output json"
    elif objective_status == ObjectiveStatus.TIMED_OUT:
        blocked_reasons.append("objective_timed_out: objective is terminal unless explicitly retried")
        next_action = f"harness objectives retry {objective_id} --project {project_root} --output json"
    elif objective_status in TERMINAL_OBJECTIVE_STATUSES:
        blocked_reasons.append(f"objective_{objective_status.value}: objective is terminal")
        next_action = f"harness objectives inspect {objective_id} --project {project_root} --output json"
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
        next_action=next_action,
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


def _daemon_eligibility_blockers(eligibility: dict[str, Any], *, existing: list[str]) -> list[str]:
    decision = str(eligibility.get("decision") or "")
    if decision in {"eligible", "skipped_status", ""}:
        return []
    if decision == "waiting_approval" and _existing_reason_mentions(existing, "approval"):
        return []
    if decision == "blocked_dependency" and _existing_reason_mentions(existing, "dependency"):
        return []
    if decision == "control_disabled":
        target_kind = str(eligibility.get("target_kind") or "adapter")
        target_id = str(eligibility.get("target_id") or eligibility.get("adapter_id") or "unknown")
        reason = str(eligibility.get("control_reason") or eligibility.get("reason") or "").strip()
        rendered = f"control_disabled: {target_kind}:{target_id}"
        if reason:
            rendered = f"{rendered}. {reason}"
        return [str(sanitize_for_logging(rendered))]
    if decision == "breaker_open":
        adapter_id = str(eligibility.get("adapter_id") or "unknown")
        failure_count = eligibility.get("failure_count")
        threshold = eligibility.get("threshold")
        window_seconds = eligibility.get("window_seconds")
        if failure_count is not None and threshold is not None and window_seconds is not None:
            return [
                str(
                    sanitize_for_logging(
                        f"breaker_open: {adapter_id} {failure_count}/{threshold} failures in {window_seconds} seconds"
                    )
                )
            ]
        return [str(sanitize_for_logging(f"breaker_open: {adapter_id}"))]
    if decision == "policy_forbidden":
        forbidden = ", ".join(str(item) for item in eligibility.get("forbidden_policy_keys") or [])
        reason = forbidden or str(eligibility.get("reason") or "daemon policy")
        return [str(sanitize_for_logging(f"policy_forbidden: {reason}"))]
    if decision == "waiting_approval":
        missing = eligibility.get("missing_approvals") or eligibility.get("required_approvals") or []
        rendered = ", ".join(str(item) for item in missing) or str(eligibility.get("reason") or "unknown")
        return [str(sanitize_for_logging(f"missing required task approvals: {rendered}"))]
    if decision == "blocked_dependency":
        dependency_ids = ", ".join(str(item) for item in eligibility.get("blocked_dependency_ids") or [])
        reason = dependency_ids or str(eligibility.get("reason") or "unknown")
        return [str(sanitize_for_logging(f"blocked dependency: {reason}"))]
    if decision == "active_lease":
        return [str(sanitize_for_logging("task already has an active lease"))]
    reason = str(eligibility.get("reason") or "blocked")
    return [str(sanitize_for_logging(f"{decision}: {reason}"))]


def _existing_reason_mentions(reasons: list[str], token: str) -> bool:
    folded = token.casefold()
    return any(folded in reason.casefold() for reason in reasons)


def _mode_for_progress(
    objective_status: ObjectiveStatus,
    tasks: list[TaskRecord],
    progress_tasks: list[OrchestrationProgressTask],
    active_lease_ids: list[str],
    blocked_reasons: list[str],
) -> OrchestrationProgressMode:
    if objective_status == ObjectiveStatus.CREATED:
        return OrchestrationProgressMode.BLOCKED
    if objective_status == ObjectiveStatus.SUSPENDED:
        return OrchestrationProgressMode.BLOCKED
    if objective_status == ObjectiveStatus.WAITING_APPROVAL:
        return OrchestrationProgressMode.BLOCKED
    if objective_status == ObjectiveStatus.RETRYING:
        return OrchestrationProgressMode.BLOCKED
    if objective_status in TERMINAL_OBJECTIVE_STATUSES:
        return OrchestrationProgressMode.TERMINAL
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
    *,
    has_objective_evidence: bool,
    checkpoint_gate: ObjectiveCheckpointGate,
    objective_status: ObjectiveStatus,
) -> str:
    if objective_status == ObjectiveStatus.CREATED:
        return f"harness objectives start {objective_id} --project {project_root} --output json"
    if objective_status == ObjectiveStatus.SUSPENDED:
        return f"harness objectives resume {objective_id} --project {project_root} --output json"
    if objective_status == ObjectiveStatus.WAITING_APPROVAL:
        if not checkpoint_gate.ok:
            return (
                f"harness objectives checkpoints gate {objective_id} --project {project_root} --output json; "
                f"harness objectives checkpoints list {objective_id} --project {project_root} --output json"
            )
        return f"harness objectives resume {objective_id} --project {project_root} --output json"
    if objective_status == ObjectiveStatus.RETRYING:
        return f"harness objectives resume {objective_id} --project {project_root} --output json"
    if objective_status == ObjectiveStatus.CANCELLED:
        return f"harness objectives inspect {objective_id} --project {project_root} --output json"
    if objective_status == ObjectiveStatus.TIMED_OUT:
        return f"harness objectives retry {objective_id} --project {project_root} --output json"
    if objective_status == ObjectiveStatus.COMPLETED:
        if has_objective_evidence:
            return (
                f"harness objectives verify-evidence {objective_id} --project {project_root} --output json; "
                f"harness traces export-objective {objective_id} --format otel-json --project {project_root} --output json"
            )
        return f"harness objectives inspect {objective_id} --project {project_root} --output json"
    if not checkpoint_gate.ok:
        return (
            f"harness objectives checkpoints gate {objective_id} --project {project_root} --output json; "
            f"harness objectives checkpoints list {objective_id} --project {project_root} --output json"
        )
    if active_lease_ids:
        lease_id = active_lease_ids[0]
        return (
            f"harness daemon inspect-lease {lease_id} --project {project_root} --output json; "
            f"harness daemon execute {lease_id} --project {project_root} --output json"
        )
    if mode == OrchestrationProgressMode.READY:
        return f"harness daemon run-once --project {project_root} --output json"
    if mode == OrchestrationProgressMode.TERMINAL:
        if has_objective_evidence:
            return (
                f"harness objectives verify-evidence {objective_id} --project {project_root} --output json; "
                f"harness traces export-objective {objective_id} --format otel-json --project {project_root} --output json"
            )
        return "Inspect runs/artifacts or create a follow-up objective."
    if mode == OrchestrationProgressMode.IDLE:
        return f"Create tasks for objective {objective_id}."
    blocked = next((task for task in tasks if task.blocked_reasons), None)
    if blocked is not None:
        reason_text = "; ".join(blocked.blocked_reasons)
        if "control_disabled" in reason_text:
            return (
                f"harness controls list --project {project_root} --output json; "
                f"harness tasks inspect {blocked.task_id} --project {project_root} --output json"
            )
        if "breaker_open" in reason_text:
            return (
                f"harness controls breaker-status --project {project_root} --output json; "
                f"harness tasks inspect {blocked.task_id} --project {project_root} --output json"
            )
        if "approval" in reason_text:
            return f"harness tasks inspect {blocked.task_id} --project {project_root} --output json"
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
        reason_text = "; ".join(blocked_reasons)
        if "control_disabled" in reason_text:
            return (
                f"harness controls list --project {project_root} --output json; "
                f"harness tasks inspect {task.id} --project {project_root} --output json"
            )
        if "breaker_open" in reason_text:
            return (
                f"harness controls breaker-status --project {project_root} --output json; "
                f"harness tasks inspect {task.id} --project {project_root} --output json"
            )
        if any("approval" in reason for reason in blocked_reasons):
            return f"harness tasks inspect {task.id} --project {project_root} --output json"
        return f"harness tasks inspect {task.id} --project {project_root} --output json"
    if task.status == TaskStatus.READY:
        return f"harness daemon run-once --project {project_root} --output json"
    return None


def _equivalent_commands(
    project_root: Path,
    objective_id: str,
    tasks: list[OrchestrationProgressTask],
    active_lease_ids: list[str],
    *,
    has_objective_evidence: bool,
    checkpoint_gate: ObjectiveCheckpointGate,
    objective_status: ObjectiveStatus,
) -> list[str]:
    commands = [
        f"harness progress --objective {objective_id} --project {project_root} --output json",
        f"harness tasks graph --objective {objective_id} --project {project_root} --output json",
    ]
    if objective_status == ObjectiveStatus.CREATED:
        commands.extend(
            [
                f"harness objectives inspect {objective_id} --project {project_root} --output json",
                f"harness objectives start {objective_id} --project {project_root} --output json",
            ]
        )
        if has_objective_evidence:
            commands.extend(
                [
                    f"harness objectives verify-evidence {objective_id} --project {project_root} --output json",
                    f"harness traces export-objective {objective_id} --format otel-json --project {project_root} --output json",
                ]
            )
        return commands
    if objective_status == ObjectiveStatus.SUSPENDED:
        commands.extend(
            [
                f"harness objectives inspect {objective_id} --project {project_root} --output json",
                f"harness objectives resume {objective_id} --project {project_root} --output json",
            ]
        )
        if has_objective_evidence:
            commands.extend(
                [
                    f"harness objectives verify-evidence {objective_id} --project {project_root} --output json",
                    f"harness traces export-objective {objective_id} --format otel-json --project {project_root} --output json",
                ]
            )
        return commands
    if objective_status == ObjectiveStatus.WAITING_APPROVAL:
        commands.extend(
            [
                f"harness objectives inspect {objective_id} --project {project_root} --output json",
                f"harness objectives checkpoints gate {objective_id} --project {project_root} --output json",
                f"harness objectives checkpoints list {objective_id} --project {project_root} --output json",
            ]
        )
        if checkpoint_gate.ok:
            commands.append(f"harness objectives resume {objective_id} --project {project_root} --output json")
        if has_objective_evidence:
            commands.extend(
                [
                    f"harness objectives verify-evidence {objective_id} --project {project_root} --output json",
                    f"harness traces export-objective {objective_id} --format otel-json --project {project_root} --output json",
                ]
            )
        return commands
    if objective_status == ObjectiveStatus.RETRYING:
        commands.extend(
            [
                f"harness objectives inspect {objective_id} --project {project_root} --output json",
                f"harness objectives resume {objective_id} --project {project_root} --output json",
            ]
        )
        if has_objective_evidence:
            commands.extend(
                [
                    f"harness objectives verify-evidence {objective_id} --project {project_root} --output json",
                    f"harness traces export-objective {objective_id} --format otel-json --project {project_root} --output json",
                ]
            )
        return commands
    if objective_status == ObjectiveStatus.TIMED_OUT:
        commands.extend(
            [
                f"harness objectives inspect {objective_id} --project {project_root} --output json",
                f"harness objectives retry {objective_id} --project {project_root} --output json",
            ]
        )
        if has_objective_evidence:
            commands.extend(
                [
                    f"harness objectives verify-evidence {objective_id} --project {project_root} --output json",
                    f"harness traces export-objective {objective_id} --format otel-json --project {project_root} --output json",
                ]
            )
        return commands
    if objective_status in TERMINAL_OBJECTIVE_STATUSES:
        commands.append(f"harness objectives inspect {objective_id} --project {project_root} --output json")
        if has_objective_evidence:
            commands.extend(
                [
                    f"harness objectives verify-evidence {objective_id} --project {project_root} --output json",
                    f"harness traces export-objective {objective_id} --format otel-json --project {project_root} --output json",
                ]
            )
        return commands
    if checkpoint_gate.required_checkpoint_count or not checkpoint_gate.ok:
        commands.extend(
            [
                f"harness objectives checkpoints gate {objective_id} --project {project_root} --output json",
                f"harness objectives checkpoints list {objective_id} --project {project_root} --output json",
            ]
        )
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
    if has_objective_evidence:
        commands.extend(
            [
                f"harness objectives verify-evidence {objective_id} --project {project_root} --output json",
                f"harness traces export-objective {objective_id} --format otel-json --project {project_root} --output json",
            ]
        )
    return commands


def _checkpoint_progress_summary(checkpoint_gate: ObjectiveCheckpointGate) -> dict[str, Any]:
    return {
        "schema_version": "harness.objective_checkpoint_progress_summary/v1",
        "ok": checkpoint_gate.ok,
        "status": checkpoint_gate.status,
        "gate_id": checkpoint_gate.gate_id,
        "required_checkpoint_count": checkpoint_gate.required_checkpoint_count,
        "pending_checkpoint_ids": list(checkpoint_gate.pending_checkpoint_ids),
        "rejected_checkpoint_ids": list(checkpoint_gate.rejected_checkpoint_ids),
        "reasons": list(checkpoint_gate.reasons),
        "contents_included": False,
        "execution_allowed": False,
        "model_context_allowed": False,
        "network_required": False,
        "mutation_allowed": False,
        "permission_granting": False,
    }


def _objective_evidence_summary(project_root: Path, objective_id: str) -> dict[str, Any] | None:
    evidence_path = project_root / HARNESS_DIR / "autonomy" / "objectives" / f"{objective_id}.jsonl"
    if not evidence_path.exists():
        return None
    event_summary = _objective_event_summary(evidence_path)
    try:
        verification = verify_objective_evidence(project_root, objective_id)
    except Exception as exc:
        return {
            "schema_version": "harness.objective_evidence_progress_summary/v1",
            "exists": True,
            "ok": False,
            "evidence_path": str(evidence_path),
            "error": f"{exc.__class__.__name__}: {exc}",
            **event_summary,
        }

    checks_by_id = {check.id: check for check in verification.checks}
    hash_chain = checks_by_id.get("event_hash_chain")
    return {
        "schema_version": "harness.objective_evidence_progress_summary/v1",
        "exists": True,
        "ok": verification.ok,
        "evidence_path": str(verification.evidence_path),
        "summary": verification.summary,
        "event_count": hash_chain.evidence.get("event_count") if hash_chain else None,
        "head_sha256": hash_chain.evidence.get("head_sha256") if hash_chain else None,
        **event_summary,
        "check_statuses": {
            check_id: checks_by_id[check_id].status
            for check_id in (
                "event_schema",
                "event_payload_schema",
                "event_identity",
                "event_hash_chain",
                "event_timestamps",
                "run_lifecycle",
                "batch_lifecycle",
                "stopped_summary",
            )
            if check_id in checks_by_id
        },
    }


def _objective_event_summary(evidence_path: Path) -> dict[str, Any]:
    events, parse_errors = read_objective_evidence_events(evidence_path)
    event_type_counts: dict[str, int] = {}
    lease_guard_stops: list[dict[str, Any]] = []
    last_event_type: str | None = None
    for _, event in events:
        event_type = event.get("event")
        if not isinstance(event_type, str) or not event_type:
            continue
        last_event_type = event_type
        event_type_counts[event_type] = event_type_counts.get(event_type, 0) + 1
        if event_type == "lease_guard_stopped":
            guard_pause_reasons = event.get("guard_pause_reasons")
            guard_reason = guard_pause_reasons[0] if isinstance(guard_pause_reasons, list) and guard_pause_reasons else {}
            lease_guard_stops.append(
                {
                    "task_id": event.get("task_id"),
                    "lease_id": event.get("lease_id"),
                    "adapter_id": event.get("adapter_id"),
                    "task_type": event.get("task_type"),
                    "stop_reason": event.get("stop_reason"),
                    "guard_decision": guard_reason.get("decision") if isinstance(guard_reason, dict) else None,
                    "guard_reason": guard_reason.get("reason") if isinstance(guard_reason, dict) else None,
                }
            )
    return {
        "event_type_counts": dict(sorted(event_type_counts.items())),
        "last_event_type": last_event_type,
        "parse_error_count": len(parse_errors),
        "lease_guard_stop_count": len(lease_guard_stops),
        "last_lease_guard_stop": lease_guard_stops[-1] if lease_guard_stops else None,
    }


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
