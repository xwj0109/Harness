from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from harness.approvals import ApprovalStore
from harness.autonomy import (
    AutonomyDecision,
    AutonomyDecisionStatus,
    AutonomyEvaluationInput,
    AutonomousApprovalRecord,
    evaluate_autonomy,
    get_builtin_autonomy_policy,
)
from harness.config import HARNESS_DIR
from harness.events import append_jsonl
from harness.execution import execute_lease, list_execution_adapter_descriptors
from harness.memory.sqlite_store import SQLiteStore
from harness.models import TaskLease, TaskLeaseStatus, TaskRecord, TaskStatus
from harness.paths import resolve_project_root
from harness.security import sanitize_for_logging


OBJECTIVE_RUNNER_SCHEMA_VERSION = "harness.autonomous_objective_run/v1"
OBJECTIVE_RUNNER_EVENT_SCHEMA_VERSION = "harness.autonomous_objective_event/v1"

OBJECTIVE_RUNNER_OWNER = "autonomous_objective_runner"


class ObjectiveRunnerStep(BaseModel):
    step: int
    task_id: str | None = None
    lease_id: str | None = None
    run_id: str | None = None
    adapter_id: str | None = None
    task_type: str | None = None
    decision_status: str | None = None
    execution_decision: str | None = None
    stop_reason: str | None = None


class ObjectiveRunnerResult(BaseModel):
    schema_version: str = OBJECTIVE_RUNNER_SCHEMA_VERSION
    ok: bool
    project_root: Path
    objective_id: str
    autonomy_profile_id: str
    stop_reason: str
    steps: int = 0
    adapter_dispatches: int = 0
    new_tasks_created: int = 0
    consecutive_failures: int = 0
    evidence_path: Path
    step_results: list[ObjectiveRunnerStep] = Field(default_factory=list)
    final_task_statuses: dict[str, str] = Field(default_factory=dict)
    pause_reasons: list[dict[str, Any]] = Field(default_factory=list)
    autonomy_decision: AutonomyDecision | None = None
    errors: list[str] = Field(default_factory=list)


def run_objective_autonomously(
    project_root: Path,
    objective_id: str,
    *,
    autonomy_profile_id: str = "safe-local",
    max_steps: int | None = None,
    owner: str = OBJECTIVE_RUNNER_OWNER,
) -> ObjectiveRunnerResult:
    project_root = resolve_project_root(project_root)
    policy = get_builtin_autonomy_policy(autonomy_profile_id)
    store = SQLiteStore(project_root)
    objective = store.get_objective(objective_id)
    run_id = f"objrun_{uuid.uuid4().hex[:12]}"
    evidence_path = project_root / HARNESS_DIR / "autonomy" / "objectives" / f"{objective_id}.jsonl"
    step_limit = min(max_steps if max_steps is not None else policy.budget.max_adapter_dispatches, policy.budget.max_adapter_dispatches)
    step_results: list[ObjectiveRunnerStep] = []
    adapter_dispatches = 0
    consecutive_failures = 0
    stop_reason = "not_started"
    pause_reasons: list[dict[str, Any]] = []
    final_decision: AutonomyDecision | None = None
    errors: list[str] = []
    _append_objective_event(
        evidence_path,
        run_id,
        "started",
        {
            "objective_id": objective.id,
            "autonomy_profile_id": autonomy_profile_id,
            "budget": policy.budget.model_dump(mode="json"),
        },
    )
    recovery = store.recover_daemon_leases(owner=owner, pid=None)
    _append_objective_event(
        evidence_path,
        run_id,
        "recovery_checked",
        {
            "renewed_lease_ids": [lease.id for lease in recovery.renewed_leases],
            "expired_lease_ids": [lease.id for lease in recovery.expired_leases],
            "recovered_task_ids": [task.id for task in recovery.recovered_tasks],
            "event_ids": [event.id for event in recovery.events],
        },
    )

    while adapter_dispatches < step_limit:
        tasks = store.list_tasks(objective_id=objective_id)
        if not tasks:
            stop_reason = "no_tasks"
            break
        terminal = _terminal_stop_reason(tasks)
        if terminal is not None:
            stop_reason = terminal
            break

        lease = _active_objective_lease(store, tasks)
        task: TaskRecord | None = None
        if lease is not None:
            task = store.get_task(lease.task_id)
            selection = {"task": task, "lease": lease}
        else:
            selection = store.select_next_task_for_lease(owner=owner, objective_id=objective_id)
            if selection is None:
                stop_reason = "blocked_or_no_ready_task"
                pause_reasons = _objective_pause_reasons(store, objective_id)
                break
            task = selection["task"]  # type: ignore[assignment]
            lease = selection["lease"]  # type: ignore[assignment]

        decision = _evaluate_task_dispatch_autonomy(project_root, policy.id, task, lease)
        final_decision = decision
        decision_evidence = _record_autonomy_decision(project_root, run_id, objective_id, task, lease, decision)
        if decision.status != AutonomyDecisionStatus.AUTO_ALLOWED:
            stop_reason = decision.status.value
            pause_reasons = [
                {
                    "task_id": task.id,
                    "lease_id": lease.id,
                    "adapter_id": decision.adapter_id,
                    "decision": decision.status.value,
                    "reasons": decision.reasons,
                }
            ]
            step_results.append(
                ObjectiveRunnerStep(
                    step=adapter_dispatches + 1,
                    task_id=task.id,
                    lease_id=lease.id,
                    adapter_id=decision.adapter_id,
                    task_type=decision.task_type,
                    decision_status=decision.status.value,
                    stop_reason=stop_reason,
                )
            )
            _append_objective_event(
                evidence_path,
                run_id,
                "autonomy_stopped",
                {
                    "task_id": task.id,
                    "lease_id": lease.id,
                    "autonomy_decision_id": decision_evidence["record_id"],
                    "decision": decision.model_dump(mode="json"),
                },
            )
            break

        approval = _record_autonomous_approval(project_root, run_id, objective_id, task, lease, decision)
        try:
            result = execute_lease(project_root, lease.id, owner=owner)
        except (KeyError, ValueError) as exc:
            consecutive_failures += 1
            errors.append(str(exc))
            stop_reason = "execution_error"
            step_results.append(
                ObjectiveRunnerStep(
                    step=adapter_dispatches + 1,
                    task_id=task.id,
                    lease_id=lease.id,
                    adapter_id=decision.adapter_id,
                    task_type=decision.task_type,
                    decision_status=decision.status.value,
                    stop_reason=stop_reason,
                )
            )
            _append_objective_event(
                evidence_path,
                run_id,
                "execution_error",
                {"task_id": task.id, "lease_id": lease.id, "error": str(sanitize_for_logging(str(exc)))},
            )
            break

        adapter_dispatches += 1
        if result.ok:
            consecutive_failures = 0
        else:
            consecutive_failures += 1
        artifact_ids = [artifact.id for artifact in result.manifest.artifacts] if result.manifest else []
        outcome = _record_autonomous_outcome(
            project_root,
            run_id,
            objective_id,
            task,
            lease,
            decision,
            result.ok,
            result.run.id if result.run else None,
            artifact_ids,
        )
        _record_run_autonomy_event(project_root, decision_evidence, decision, approval, outcome)
        step_results.append(
            ObjectiveRunnerStep(
                step=adapter_dispatches,
                task_id=task.id,
                lease_id=lease.id,
                run_id=result.run.id if result.run else None,
                adapter_id=result.adapter_id or decision.adapter_id,
                task_type=decision.task_type,
                decision_status=decision.status.value,
                execution_decision=result.decision,
            )
        )
        _append_objective_event(
            evidence_path,
            run_id,
            "adapter_dispatched",
            {
                "task_id": task.id,
                "lease_id": lease.id,
                "run_id": result.run.id if result.run else None,
                "artifact_ids": artifact_ids,
                "adapter_id": result.adapter_id,
                "ok": result.ok,
                "decision": result.decision,
                "autonomy_decision_id": decision_evidence["record_id"],
                "autonomous_approval_id": approval.id,
                "autonomous_outcome_id": outcome["record_id"],
                "policy_id": decision.policy_id,
                "stop_reason": None,
            },
        )
        if not result.ok:
            stop_reason = "execution_failed"
            pause_reasons = [
                {
                    "task_id": task.id,
                    "lease_id": lease.id,
                    "adapter_id": result.adapter_id,
                    "decision": result.decision,
                    "reasons": result.rejection_reasons + result.errors,
                }
            ]
            break
        terminal = _terminal_stop_reason(store.list_tasks(objective_id=objective_id))
        if terminal is not None:
            stop_reason = terminal
            break
        if consecutive_failures >= policy.budget.max_consecutive_failures:
            stop_reason = "consecutive_failure_budget_exhausted"
            break
    else:
        stop_reason = "adapter_dispatch_budget_exhausted"

    if stop_reason == "not_started":
        stop_reason = "adapter_dispatch_budget_exhausted"

    final_statuses = {task.id: task.status.value for task in store.list_tasks(objective_id=objective_id)}
    result = ObjectiveRunnerResult(
        ok=stop_reason == "objective_succeeded",
        project_root=project_root,
        objective_id=objective_id,
        autonomy_profile_id=autonomy_profile_id,
        stop_reason=stop_reason,
        steps=len(step_results),
        adapter_dispatches=adapter_dispatches,
        consecutive_failures=consecutive_failures,
        evidence_path=evidence_path,
        step_results=step_results,
        final_task_statuses=final_statuses,
        pause_reasons=pause_reasons,
        autonomy_decision=final_decision,
        errors=errors,
    )
    _append_objective_event(
        evidence_path,
        run_id,
        "stopped",
        result.model_dump(mode="json", exclude={"schema_version", "project_root", "evidence_path"}),
    )
    return result


def run_next_active_objective_autonomously(
    project_root: Path,
    *,
    autonomy_profile_id: str = "daemon-safe",
    max_steps: int | None = None,
    owner: str = OBJECTIVE_RUNNER_OWNER,
) -> ObjectiveRunnerResult | None:
    project_root = resolve_project_root(project_root)
    store = SQLiteStore(project_root)
    objectives = [objective for objective in store.list_objectives() if objective.status.value == "active"]
    for objective in objectives:
        tasks = store.list_tasks(objective_id=objective.id)
        if any(task.status in {TaskStatus.READY, TaskStatus.BLOCKED, TaskStatus.LEASED} for task in tasks):
            return run_objective_autonomously(
                project_root,
                objective.id,
                autonomy_profile_id=autonomy_profile_id,
                max_steps=max_steps,
                owner=owner,
            )
    return None


def _evaluate_task_dispatch_autonomy(
    project_root: Path,
    policy_id: str,
    task: TaskRecord,
    lease: TaskLease,
) -> AutonomyDecision:
    policy = get_builtin_autonomy_policy(policy_id)
    adapter_id = _task_adapter_id(task)
    task_type = _task_type(task)
    boundary = "hosted_provider_codex" if adapter_id in {"read_only_summary", "repo_planning", "codex_isolated_edit"} else "local_artifact"
    request = AutonomyEvaluationInput(
        tool_name="dispatch_registered_adapter",
        risk="sandboxed_execution",
        boundary=boundary,
        adapter_id=adapter_id,
        task_type=task_type,
        has_scoped_approval=_has_scoped_approval(project_root, task, adapter_id, task_type, boundary, policy_id),
        requires_paid_or_hosted_boundary=boundary == "hosted_provider_codex",
        requires_sandbox=True,
        sandbox_enforced=True,
        idempotency_key=task.idempotency_key or lease.id,
        evidence_contract="task,lease,run,artifact_manifest",
        kill_switch_active=_kill_switch_active(project_root, adapter_id, task_type),
        adapter_breaker_open=_adapter_breaker_open(project_root, adapter_id),
    )
    decision = evaluate_autonomy(policy, request)
    return _apply_adapter_descriptor_metadata(policy.id, request, decision)


def _apply_adapter_descriptor_metadata(
    policy_id: str,
    request: AutonomyEvaluationInput,
    decision: AutonomyDecision,
) -> AutonomyDecision:
    descriptor = _execution_adapter_descriptor(request.adapter_id)
    if descriptor is None:
        return decision.model_copy(
            update={
                "status": AutonomyDecisionStatus.DENIED,
                "reasons": [*decision.reasons, f"adapter is not registered: {request.adapter_id}"],
                "requires_human": False,
            }
        )
    if decision.status in {
        AutonomyDecisionStatus.DENIED,
        AutonomyDecisionStatus.POLICY_MISMATCH,
        AutonomyDecisionStatus.BUDGET_EXCEEDED,
    }:
        return decision.model_copy(
            update={
                "reasons": [
                    *decision.reasons,
                    f"adapter autonomy default is {descriptor.autonomy_default}: {descriptor.id}",
                ]
            }
        )
    if descriptor.autonomy_default == "forbidden":
        return decision.model_copy(
            update={
                "status": AutonomyDecisionStatus.DENIED,
                "reasons": [*decision.reasons, f"adapter forbids autonomous dispatch: {descriptor.id}"],
                "requires_human": False,
            }
        )
    if descriptor.required_autonomy_scopes and policy_id not in descriptor.required_autonomy_scopes:
        return decision.model_copy(
            update={
                "status": AutonomyDecisionStatus.APPROVAL_REQUIRED,
                "reasons": [
                    *decision.reasons,
                    f"adapter requires an autonomy scope in {sorted(descriptor.required_autonomy_scopes)}",
                ],
                "requires_human": True,
            }
        )
    if (
        descriptor.autonomy_default == "approval_required"
        and decision.status == AutonomyDecisionStatus.AUTO_ALLOWED
        and descriptor.required_approvals
        and not request.has_scoped_approval
    ):
        return decision.model_copy(
            update={
                "status": AutonomyDecisionStatus.APPROVAL_REQUIRED,
                "reasons": [
                    *decision.reasons,
                    f"adapter requires scoped approval before autonomous dispatch: {descriptor.id}",
                ],
                "requires_human": True,
            }
        )
    return decision.model_copy(
        update={
            "reasons": [
                *decision.reasons,
                f"adapter autonomy default is {descriptor.autonomy_default}: {descriptor.id}",
            ]
        }
    )


def _terminal_stop_reason(tasks: list[TaskRecord]) -> str | None:
    statuses = {task.status for task in tasks}
    if statuses and statuses <= {TaskStatus.SUCCEEDED, TaskStatus.SKIPPED}:
        return "objective_succeeded"
    if statuses and statuses <= {TaskStatus.SUCCEEDED, TaskStatus.SKIPPED, TaskStatus.FAILED, TaskStatus.CANCELLED}:
        return "objective_terminal_with_failures"
    return None


def _active_objective_lease(store: SQLiteStore, tasks: list[TaskRecord]) -> TaskLease | None:
    active: list[TaskLease] = []
    task_ids = {task.id for task in tasks}
    for task_id in task_ids:
        active.extend(lease for lease in store.list_task_leases(task_id) if lease.status == TaskLeaseStatus.ACTIVE)
    if not active:
        return None
    return sorted(active, key=lambda lease: (lease.acquired_at, lease.id))[0]


def _objective_pause_reasons(store: SQLiteStore, objective_id: str) -> list[dict[str, Any]]:
    reasons: list[dict[str, Any]] = []
    for task in store.list_tasks(objective_id=objective_id):
        eligibility = store.daemon_task_eligibility(task)
        if eligibility["decision"] in {"blocked_dependency", "waiting_approval", "policy_forbidden", "active_lease"}:
            reasons.append(eligibility)
    return reasons


def _task_adapter_id(task: TaskRecord) -> str | None:
    adapter = task.metadata.get("execution_adapter")
    return str(adapter) if adapter else None


def _task_type(task: TaskRecord) -> str | None:
    task_type = task.metadata.get("task_type")
    return str(task_type) if task_type else None


def _has_scoped_approval(
    project_root: Path,
    task: TaskRecord,
    adapter_id: str | None,
    task_type: str | None,
    boundary: str,
    autonomy_scope: str,
) -> bool:
    if boundary != "hosted_provider_codex" or not task_type:
        return False
    return (
        ApprovalStore(project_root).find_valid(
            "codex_cli",
            "hosted_provider",
            task_type,
            adapter_id=adapter_id,
            workbench_id=task.workbench_id,
            objective_id=task.objective_id,
            autonomy_scope=autonomy_scope,
            strict_scope=True,
        )
        is not None
    )


def _adapter_breaker_open(project_root: Path, adapter_id: str | None) -> bool:
    if adapter_id is None:
        return False
    try:
        return SQLiteStore(project_root).adapter_breaker_state(adapter_id).status.value == "open"
    except Exception:
        return True


def _kill_switch_active(project_root: Path, adapter_id: str | None, task_type: str | None) -> bool:
    try:
        controls = SQLiteStore(project_root).active_execution_controls()
    except Exception:
        return True
    for control in controls:
        target_kind = control.target_kind.value
        if target_kind == "adapter" and control.target_id in {"*", adapter_id}:
            return True
        if target_kind == "task_type" and control.target_id in {"*", task_type}:
            return True
        if target_kind == "docker_execution" and adapter_id == "docker_run_tests":
            return True
        if target_kind == "hosted_boundary" and adapter_id in {"read_only_summary", "repo_planning", "codex_isolated_edit"}:
            return True
    return False


def _execution_adapter_descriptor(adapter_id: str | None):
    if adapter_id is None:
        return None
    for descriptor in list_execution_adapter_descriptors():
        if descriptor.id == adapter_id:
            return descriptor
    return None


def _record_autonomy_decision(
    project_root: Path,
    run_id: str,
    objective_id: str,
    task: TaskRecord,
    lease: TaskLease,
    decision: AutonomyDecision,
) -> dict[str, Any]:
    payload = {
        **decision.model_dump(mode="json"),
        "record_id": f"adec_{uuid.uuid4().hex[:12]}",
        "objective_run_id": run_id,
        "objective_id": objective_id,
        "task_id": task.id,
        "lease_id": lease.id,
    }
    append_jsonl(resolve_project_root(project_root) / HARNESS_DIR / "autonomy" / "decisions.jsonl", payload)
    return payload


def _record_autonomous_approval(
    project_root: Path,
    run_id: str,
    objective_id: str,
    task: TaskRecord,
    lease: TaskLease,
    decision: AutonomyDecision,
) -> AutonomousApprovalRecord:
    record = AutonomousApprovalRecord(
        id=f"auto_{uuid.uuid4().hex[:12]}",
        policy_id=decision.policy_id,
        decision_status=decision.status,
        tool_name="dispatch_registered_adapter",
        adapter_id=decision.adapter_id,
        task_type=decision.task_type,
        boundary=decision.boundary or "unknown",
        risk=decision.risk or "sandboxed_execution",
        reasons=decision.reasons,
    )
    payload = {
        **record.to_jsonl_payload(),
        "objective_run_id": run_id,
        "objective_id": objective_id,
        "task_id": task.id,
        "lease_id": lease.id,
    }
    append_jsonl(resolve_project_root(project_root) / HARNESS_DIR / "autonomy" / "approvals.jsonl", payload)
    return record


def _record_autonomous_outcome(
    project_root: Path,
    run_id: str,
    objective_id: str,
    task: TaskRecord,
    lease: TaskLease,
    decision: AutonomyDecision,
    ok: bool,
    adapter_run_id: str | None,
    artifact_ids: list[str],
) -> dict[str, Any]:
    payload = {
        "schema_version": "harness.autonomous_outcome/v1",
        "record_id": f"aout_{uuid.uuid4().hex[:12]}",
        "objective_run_id": run_id,
        "objective_id": objective_id,
        "task_id": task.id,
        "lease_id": lease.id,
        "run_id": adapter_run_id,
        "tool_name": "dispatch_registered_adapter",
        "policy_id": decision.policy_id,
        "decision_status": decision.status.value,
        "adapter_id": decision.adapter_id,
        "task_type": decision.task_type,
        "ok": ok,
        "artifact_ids": artifact_ids,
    }
    append_jsonl(resolve_project_root(project_root) / HARNESS_DIR / "autonomy" / "outcomes.jsonl", payload)
    return payload


def _record_run_autonomy_event(
    project_root: Path,
    decision_evidence: dict[str, Any],
    decision: AutonomyDecision,
    approval: AutonomousApprovalRecord,
    outcome: dict[str, Any],
) -> None:
    run_id = outcome.get("run_id")
    if not run_id:
        return
    store = SQLiteStore(project_root)
    store.append_event(
        str(run_id),
        "info",
        "autonomy_decision",
        "Autonomous objective runner metadata linked to this run.",
        {
            "autonomy_decision_id": decision_evidence.get("record_id"),
            "autonomous_approval_id": approval.id,
            "autonomous_outcome_id": outcome.get("record_id"),
            "autonomy_policy_id": decision.policy_id,
            "adapter_id": approval.adapter_id,
            "task_type": approval.task_type,
        },
    )
    store.write_run_manifest(str(run_id))


def _append_objective_event(path: Path, run_id: str, event: str, payload: dict[str, Any]) -> None:
    append_jsonl(
        path,
        {
            "schema_version": OBJECTIVE_RUNNER_EVENT_SCHEMA_VERSION,
            "objective_run_id": run_id,
            "event": event,
            **sanitize_for_logging(payload),
        },
    )
