from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import hashlib
import json
from time import monotonic
import uuid
from pathlib import Path
from typing import Any, Literal

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
from harness.execution import (
    execute_lease,
    get_execution_adapter_descriptor,
    runtime_control_matches_descriptor,
)
from harness.memory.sqlite_store import SQLiteStore
from harness.models import ObjectiveRecord, ObjectiveStatus, TaskLease, TaskLeaseStatus, TaskRecord, TaskStatus, utc_now
from harness.objective_batch_plan import (
    ObjectiveBatchPlan,
    ObjectiveBatchSelection,
    ObjectiveDependencySnapshot,
    ObjectiveScheduleProfile,
)
from harness.objective_checkpoints import ObjectiveCheckpointGate, evaluate_objective_checkpoint_gate
from harness.paths import resolve_project_root
from harness.security import sanitize_for_logging


OBJECTIVE_RUNNER_SCHEMA_VERSION = "harness.autonomous_objective_run/v1"
OBJECTIVE_RUNNER_EVENT_SCHEMA_VERSION = "harness.autonomous_objective_event/v1"

OBJECTIVE_RUNNER_OWNER = "autonomous_objective_runner"
ObjectiveSelectionSource = Literal["resumed_active_lease", "new_guarded_lease"]


class ObjectiveRunnerStep(BaseModel):
    step: int
    batch: int | None = None
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
    scheduler_mode: str = "sequential"
    stop_reason: str
    steps: int = 0
    batches: int = 0
    max_parallel: int = 1
    adapter_dispatches: int = 0
    new_tasks_created: int = 0
    consecutive_failures: int = 0
    evidence_path: Path
    step_results: list[ObjectiveRunnerStep] = Field(default_factory=list)
    final_task_statuses: dict[str, str] = Field(default_factory=dict)
    pause_reasons: list[dict[str, Any]] = Field(default_factory=list)
    autonomy_decision: AutonomyDecision | None = None
    errors: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class _PreparedDispatch:
    batch: int
    task: TaskRecord
    lease: TaskLease
    selection_source: ObjectiveSelectionSource
    decision: AutonomyDecision
    decision_evidence: dict[str, Any]
    approval: AutonomousApprovalRecord


@dataclass(frozen=True)
class _DispatchOutcome:
    prepared: _PreparedDispatch
    result: Any | None = None
    error: str | None = None


@dataclass(frozen=True)
class _TaskScheduleProfile:
    task_id: str
    priority: int
    critical_path_depth: int
    downstream_task_count: int


def _timeout_deadline(timeout_seconds: int | None) -> float | None:
    if timeout_seconds is None:
        return None
    if timeout_seconds < 0:
        raise ValueError("timeout_seconds must be non-negative")
    return monotonic() + timeout_seconds


def _timeout_expired(deadline: float | None) -> bool:
    return deadline is not None and monotonic() >= deadline


def _mark_objective_timed_out(
    store: SQLiteStore,
    objective: ObjectiveRecord,
    *,
    timeout_seconds: int | None,
    run_id: str,
    scheduler_mode: str,
    actor: str,
) -> ObjectiveRecord:
    if objective.status == ObjectiveStatus.TIMED_OUT:
        return objective
    return store.update_objective_status(
        objective.id,
        ObjectiveStatus.TIMED_OUT,
        reason=f"Objective runner timeout expired after {timeout_seconds} seconds.",
        actor=actor,
        metadata={
            "source": "objective_runner",
            "objective_run_id": run_id,
            "scheduler_mode": scheduler_mode,
            "timeout_seconds": timeout_seconds,
        },
    )


def _timeout_pause_reasons(objective: ObjectiveRecord, *, timeout_seconds: int | None) -> list[dict[str, Any]]:
    return [
        {
            "decision": "timed_out",
            "objective_status": objective.status.value,
            "timeout_seconds": timeout_seconds,
            "reason": f"Objective runner timeout expired after {timeout_seconds} seconds before additional dispatch.",
        }
    ]


def _mark_objective_waiting_approval(
    store: SQLiteStore,
    objective: ObjectiveRecord,
    checkpoint_gate: ObjectiveCheckpointGate,
    *,
    run_id: str,
    scheduler_mode: str,
    actor: str,
) -> ObjectiveRecord:
    if objective.status == ObjectiveStatus.WAITING_APPROVAL:
        return objective
    if objective.status != ObjectiveStatus.ACTIVE:
        return objective
    return store.update_objective_status(
        objective.id,
        ObjectiveStatus.WAITING_APPROVAL,
        reason="Required objective checkpoint is waiting for human approval.",
        actor=actor,
        metadata={
            "source": "objective_runner",
            "objective_run_id": run_id,
            "scheduler_mode": scheduler_mode,
            "gate_id": checkpoint_gate.gate_id,
            "pending_checkpoint_ids": list(checkpoint_gate.pending_checkpoint_ids),
            "rejected_checkpoint_ids": list(checkpoint_gate.rejected_checkpoint_ids),
            "required_checkpoint_count": checkpoint_gate.required_checkpoint_count,
        },
    )


def run_objective_autonomously(
    project_root: Path,
    objective_id: str,
    *,
    autonomy_profile_id: str = "safe-local",
    max_steps: int | None = None,
    timeout_seconds: int | None = None,
    owner: str = OBJECTIVE_RUNNER_OWNER,
) -> ObjectiveRunnerResult:
    project_root = resolve_project_root(project_root)
    timeout_deadline = _timeout_deadline(timeout_seconds)
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
            "timeout_seconds": timeout_seconds,
        },
    )
    if objective.status == ObjectiveStatus.WAITING_APPROVAL:
        checkpoint_gate = evaluate_objective_checkpoint_gate(project_root, objective_id)
        if not checkpoint_gate.ok:
            result = _checkpoint_blocked_result(
                project_root,
                store,
                objective_id,
                autonomy_profile_id,
                evidence_path,
                checkpoint_gate,
            )
            _append_checkpoint_blocked_events(evidence_path, run_id, checkpoint_gate, result)
            return result
        objective = store.update_objective_status(
            objective.id,
            ObjectiveStatus.ACTIVE,
            reason="Required objective checkpoints are approved.",
            actor=owner,
            metadata={"source": "objective_runner", "objective_run_id": run_id, "scheduler_mode": "sequential"},
        )
    if objective.status != ObjectiveStatus.ACTIVE:
        result = _objective_inactive_result(
            project_root,
            store,
            objective,
            autonomy_profile_id,
            evidence_path,
        )
        _append_objective_event(
            evidence_path,
            run_id,
            "stopped",
            result.model_dump(mode="json", exclude={"schema_version", "project_root", "evidence_path"}),
        )
        return result
    if _timeout_expired(timeout_deadline):
        objective = _mark_objective_timed_out(
            store,
            objective,
            timeout_seconds=timeout_seconds,
            run_id=run_id,
            scheduler_mode="sequential",
            actor=owner,
        )
        result = _objective_timed_out_result(
            project_root,
            store,
            objective,
            autonomy_profile_id,
            evidence_path,
            timeout_seconds=timeout_seconds,
        )
        _append_objective_event(
            evidence_path,
            run_id,
            "stopped",
            result.model_dump(mode="json", exclude={"schema_version", "project_root", "evidence_path"}),
        )
        return result
    checkpoint_gate = evaluate_objective_checkpoint_gate(project_root, objective_id)
    if not checkpoint_gate.ok:
        objective = _mark_objective_waiting_approval(
            store,
            objective,
            checkpoint_gate,
            run_id=run_id,
            scheduler_mode="sequential",
            actor=owner,
        )
        result = _checkpoint_blocked_result(
            project_root,
            store,
            objective.id,
            autonomy_profile_id,
            evidence_path,
            checkpoint_gate,
        )
        _append_checkpoint_blocked_events(evidence_path, run_id, checkpoint_gate, result)
        return result
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
        if _timeout_expired(timeout_deadline):
            objective = _mark_objective_timed_out(
                store,
                objective,
                timeout_seconds=timeout_seconds,
                run_id=run_id,
                scheduler_mode="sequential",
                actor=owner,
            )
            stop_reason = "timed_out"
            pause_reasons = _timeout_pause_reasons(objective, timeout_seconds=timeout_seconds)
            break
        tasks = store.list_tasks(objective_id=objective_id)
        if not tasks:
            stop_reason = "no_tasks"
            break
        terminal = _terminal_stop_reason(tasks)
        if terminal is not None:
            stop_reason = terminal
            break

        lease = _active_objective_lease(store, tasks, owner=owner)
        task: TaskRecord | None = None
        if lease is not None:
            task = store.get_task(lease.task_id)
            selection = {"task": task, "lease": lease}
        else:
            candidate = _next_scheduled_task_candidate(store, objective_id=objective_id)
            if candidate is None:
                stop_reason = "blocked_or_no_ready_task"
                pause_reasons = _objective_pause_reasons(store, objective_id, owner=owner)
                break
            decision = _evaluate_task_dispatch_autonomy(project_root, policy.id, candidate, None)
            final_decision = decision
            if decision.status != AutonomyDecisionStatus.AUTO_ALLOWED:
                decision_evidence = _record_autonomy_decision(project_root, run_id, objective_id, candidate, None, decision)
                stop_reason = decision.status.value
                pause_reasons = [
                    {
                        "task_id": candidate.id,
                        "lease_id": None,
                        "adapter_id": decision.adapter_id,
                        "decision": decision.status.value,
                        "reasons": decision.reasons,
                    }
                ]
                step_results.append(
                    ObjectiveRunnerStep(
                        step=adapter_dispatches + 1,
                        task_id=candidate.id,
                        lease_id=None,
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
                        "task_id": candidate.id,
                        "lease_id": None,
                        "autonomy_decision_id": decision_evidence["record_id"],
                        "decision": decision.model_dump(mode="json"),
                    },
                )
                break
            selection, guard_pause_reasons = store.select_guarded_task_for_lease(
                candidate.id,
                owner=owner,
                objective_id=objective_id,
            )
            if selection is None:
                if guard_pause_reasons:
                    decision_evidence = _record_autonomy_decision(
                        project_root,
                        run_id,
                        objective_id,
                        candidate,
                        None,
                        decision,
                    )
                    stop_reason = _record_lease_guard_stop(
                        evidence_path,
                        run_id,
                        adapter_dispatches + 1,
                        candidate,
                        None,
                        decision,
                        decision_evidence,
                        guard_pause_reasons,
                        step_results,
                    )
                    pause_reasons = guard_pause_reasons
                else:
                    stop_reason = "blocked_or_no_ready_task"
                    pause_reasons = _objective_pause_reasons(store, objective_id, owner=owner)
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
            error = str(sanitize_for_logging(str(exc)))
            outcome = _record_autonomous_outcome(
                project_root,
                run_id,
                objective_id,
                task,
                lease,
                decision,
                False,
                None,
                [],
                error=error,
            )
            consecutive_failures += 1
            errors.append(error)
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
                {
                    "task_id": task.id,
                    "lease_id": lease.id,
                    "adapter_id": decision.adapter_id,
                    "policy_id": decision.policy_id,
                    "autonomy_decision_id": decision_evidence["record_id"],
                    "autonomous_approval_id": approval.id,
                    "autonomous_outcome_id": outcome["record_id"],
                    "error": error,
                },
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
        if _timeout_expired(timeout_deadline):
            objective = _mark_objective_timed_out(
                store,
                objective,
                timeout_seconds=timeout_seconds,
                run_id=run_id,
                scheduler_mode="sequential",
                actor=owner,
            )
            stop_reason = "timed_out"
            pause_reasons = _timeout_pause_reasons(objective, timeout_seconds=timeout_seconds)
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


def run_objective_parallel(
    project_root: Path,
    objective_id: str,
    *,
    autonomy_profile_id: str = "safe-local",
    max_steps: int | None = None,
    max_parallel: int = 2,
    timeout_seconds: int | None = None,
    owner: str = OBJECTIVE_RUNNER_OWNER,
) -> ObjectiveRunnerResult:
    """Run ready objective tasks in deterministic bounded parallel batches.

    The scheduler deliberately keeps Harness as the control plane: every task is
    leased through SQLite, checked by the autonomy policy, dispatched through the
    registered adapter boundary, and recorded as ordinary task/run/artifact
    evidence. Parallelism only changes how many already-authorized leases are
    dispatched in one batch.
    """
    if max_parallel < 1:
        raise ValueError("max_parallel must be at least 1")
    project_root = resolve_project_root(project_root)
    timeout_deadline = _timeout_deadline(timeout_seconds)
    policy = get_builtin_autonomy_policy(autonomy_profile_id)
    store = SQLiteStore(project_root)
    objective = store.get_objective(objective_id)
    run_id = f"objrun_{uuid.uuid4().hex[:12]}"
    evidence_path = project_root / HARNESS_DIR / "autonomy" / "objectives" / f"{objective_id}.jsonl"
    step_limit = min(max_steps if max_steps is not None else policy.budget.max_adapter_dispatches, policy.budget.max_adapter_dispatches)
    bounded_parallelism = max(1, min(max_parallel, step_limit if step_limit > 0 else 1))
    step_results: list[ObjectiveRunnerStep] = []
    adapter_dispatches = 0
    consecutive_failures = 0
    stop_reason = "not_started"
    pause_reasons: list[dict[str, Any]] = []
    final_decision: AutonomyDecision | None = None
    errors: list[str] = []
    batches = 0

    _append_objective_event(
        evidence_path,
        run_id,
        "started",
        {
            "objective_id": objective.id,
            "autonomy_profile_id": autonomy_profile_id,
            "budget": policy.budget.model_dump(mode="json"),
            "scheduler_mode": "bounded_parallel",
            "max_parallel": bounded_parallelism,
            "timeout_seconds": timeout_seconds,
        },
    )
    if objective.status == ObjectiveStatus.WAITING_APPROVAL:
        checkpoint_gate = evaluate_objective_checkpoint_gate(project_root, objective_id)
        if not checkpoint_gate.ok:
            result = _checkpoint_blocked_result(
                project_root,
                store,
                objective_id,
                autonomy_profile_id,
                evidence_path,
                checkpoint_gate,
                scheduler_mode="bounded_parallel",
                max_parallel=bounded_parallelism,
            )
            _append_checkpoint_blocked_events(evidence_path, run_id, checkpoint_gate, result)
            return result
        objective = store.update_objective_status(
            objective.id,
            ObjectiveStatus.ACTIVE,
            reason="Required objective checkpoints are approved.",
            actor=owner,
            metadata={
                "source": "objective_runner",
                "objective_run_id": run_id,
                "scheduler_mode": "bounded_parallel",
            },
        )
    if objective.status != ObjectiveStatus.ACTIVE:
        result = _objective_inactive_result(
            project_root,
            store,
            objective,
            autonomy_profile_id,
            evidence_path,
            scheduler_mode="bounded_parallel",
            max_parallel=bounded_parallelism,
        )
        _append_objective_event(
            evidence_path,
            run_id,
            "stopped",
            result.model_dump(mode="json", exclude={"schema_version", "project_root", "evidence_path"}),
        )
        return result
    if _timeout_expired(timeout_deadline):
        objective = _mark_objective_timed_out(
            store,
            objective,
            timeout_seconds=timeout_seconds,
            run_id=run_id,
            scheduler_mode="bounded_parallel",
            actor=owner,
        )
        result = _objective_timed_out_result(
            project_root,
            store,
            objective,
            autonomy_profile_id,
            evidence_path,
            timeout_seconds=timeout_seconds,
            scheduler_mode="bounded_parallel",
            max_parallel=bounded_parallelism,
        )
        _append_objective_event(
            evidence_path,
            run_id,
            "stopped",
            result.model_dump(mode="json", exclude={"schema_version", "project_root", "evidence_path"}),
        )
        return result
    checkpoint_gate = evaluate_objective_checkpoint_gate(project_root, objective_id)
    if not checkpoint_gate.ok:
        objective = _mark_objective_waiting_approval(
            store,
            objective,
            checkpoint_gate,
            run_id=run_id,
            scheduler_mode="bounded_parallel",
            actor=owner,
        )
        result = _checkpoint_blocked_result(
            project_root,
            store,
            objective.id,
            autonomy_profile_id,
            evidence_path,
            checkpoint_gate,
            scheduler_mode="bounded_parallel",
            max_parallel=bounded_parallelism,
        )
        _append_checkpoint_blocked_events(evidence_path, run_id, checkpoint_gate, result)
        return result
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
        if _timeout_expired(timeout_deadline):
            objective = _mark_objective_timed_out(
                store,
                objective,
                timeout_seconds=timeout_seconds,
                run_id=run_id,
                scheduler_mode="bounded_parallel",
                actor=owner,
            )
            stop_reason = "timed_out"
            pause_reasons = _timeout_pause_reasons(objective, timeout_seconds=timeout_seconds)
            break
        tasks = store.list_tasks(objective_id=objective_id)
        if not tasks:
            stop_reason = "no_tasks"
            break
        terminal = _terminal_stop_reason(tasks)
        if terminal is not None:
            stop_reason = terminal
            break

        batch_index = batches + 1
        batch_capacity = min(bounded_parallelism, step_limit - adapter_dispatches)
        prepared: list[_PreparedDispatch] = []
        pending_stop_reason: str | None = None
        pending_pause_reasons: list[dict[str, Any]] = []

        for lease in _active_objective_leases(store, tasks, owner=owner, limit=batch_capacity):
            task = store.get_task(lease.task_id)
            dispatch = _prepare_autonomous_dispatch(
                project_root,
                run_id,
                objective_id,
                policy.id,
                batch_index,
                task,
                lease,
                selection_source="resumed_active_lease",
            )
            final_decision = dispatch.decision
            if dispatch.decision.status != AutonomyDecisionStatus.AUTO_ALLOWED:
                pending_stop_reason, pending_pause_reasons = _record_parallel_autonomy_stop(
                    evidence_path,
                    run_id,
                    adapter_dispatches + len(prepared) + 1,
                    batch_index,
                    task,
                    lease,
                    dispatch.decision,
                    dispatch.decision_evidence,
                    step_results,
                )
                break
            prepared.append(dispatch)

        while len(prepared) < batch_capacity and pending_stop_reason is None:
            candidate = _next_scheduled_task_candidate(store, objective_id=objective_id)
            if candidate is None:
                break
            decision = _evaluate_task_dispatch_autonomy(project_root, policy.id, candidate, None)
            final_decision = decision
            if decision.status != AutonomyDecisionStatus.AUTO_ALLOWED:
                decision_evidence = _record_autonomy_decision(project_root, run_id, objective_id, candidate, None, decision)
                pending_stop_reason, pending_pause_reasons = _record_parallel_autonomy_stop(
                    evidence_path,
                    run_id,
                    adapter_dispatches + len(prepared) + 1,
                    batch_index,
                    candidate,
                    None,
                    decision,
                    decision_evidence,
                    step_results,
                )
                break
            selection, guard_pause_reasons = store.select_guarded_task_for_lease(
                candidate.id,
                owner=owner,
                objective_id=objective_id,
            )
            if selection is None:
                if guard_pause_reasons:
                    decision_evidence = _record_autonomy_decision(
                        project_root,
                        run_id,
                        objective_id,
                        candidate,
                        None,
                        decision,
                    )
                    pending_stop_reason = _record_lease_guard_stop(
                        evidence_path,
                        run_id,
                        adapter_dispatches + len(prepared) + 1,
                        candidate,
                        None,
                        decision,
                        decision_evidence,
                        guard_pause_reasons,
                        step_results,
                        batch=batch_index,
                    )
                    pending_pause_reasons = guard_pause_reasons
                break
            task = selection["task"]  # type: ignore[assignment]
            lease = selection["lease"]  # type: ignore[assignment]
            dispatch = _prepare_autonomous_dispatch(
                project_root,
                run_id,
                objective_id,
                policy.id,
                batch_index,
                task,
                lease,
                selection_source="new_guarded_lease",
            )
            final_decision = dispatch.decision
            if dispatch.decision.status != AutonomyDecisionStatus.AUTO_ALLOWED:
                pending_stop_reason, pending_pause_reasons = _record_parallel_autonomy_stop(
                    evidence_path,
                    run_id,
                    adapter_dispatches + len(prepared) + 1,
                    batch_index,
                    task,
                    lease,
                    dispatch.decision,
                    dispatch.decision_evidence,
                    step_results,
                )
                break
            prepared.append(dispatch)

        if not prepared:
            _append_objective_event(
                evidence_path,
                run_id,
                "batch_planned",
                _parallel_batch_plan_event(
                    batch_index=batch_index,
                    tasks=store.list_tasks(objective_id=objective_id),
                    prepared=prepared,
                    batch_capacity=batch_capacity,
                    max_parallel=bounded_parallelism,
                    remaining_dispatch_budget=step_limit - adapter_dispatches,
                    pending_stop_reason=pending_stop_reason,
                ),
            )
            if pending_stop_reason is not None:
                stop_reason = pending_stop_reason
                pause_reasons = pending_pause_reasons
                break
            stop_reason = "blocked_or_no_ready_task"
            pause_reasons = _objective_pause_reasons(store, objective_id, owner=owner)
            break

        batches += 1
        _append_objective_event(
            evidence_path,
            run_id,
            "batch_planned",
            _parallel_batch_plan_event(
                batch_index=batch_index,
                tasks=store.list_tasks(objective_id=objective_id),
                prepared=prepared,
                batch_capacity=batch_capacity,
                max_parallel=bounded_parallelism,
                remaining_dispatch_budget=step_limit - adapter_dispatches,
                pending_stop_reason=pending_stop_reason,
            ),
        )
        _append_objective_event(
            evidence_path,
            run_id,
            "batch_started",
            {
                "batch": batch_index,
                "task_ids": [item.task.id for item in prepared],
                "lease_ids": [item.lease.id for item in prepared],
                "max_parallel": bounded_parallelism,
                "remaining_dispatch_budget": step_limit - adapter_dispatches,
            },
        )
        batch_start_dispatches = adapter_dispatches
        batch_execution_errors = 0
        outcomes = _execute_prepared_dispatches(project_root, owner, prepared)
        batch_failed = False
        for outcome in outcomes:
            dispatch = outcome.prepared
            task = dispatch.task
            lease = dispatch.lease
            decision = dispatch.decision
            if outcome.error is not None:
                outcome_record = _record_autonomous_outcome(
                    project_root,
                    run_id,
                    objective_id,
                    task,
                    lease,
                    decision,
                    False,
                    None,
                    [],
                    error=outcome.error,
                )
                consecutive_failures += 1
                errors.append(outcome.error)
                batch_execution_errors += 1
                stop_reason = "execution_error"
                batch_failed = True
                step_results.append(
                    ObjectiveRunnerStep(
                        step=adapter_dispatches + 1,
                        batch=batch_index,
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
                    {
                        "batch": batch_index,
                        "task_id": task.id,
                        "lease_id": lease.id,
                        "adapter_id": decision.adapter_id,
                        "policy_id": decision.policy_id,
                        "autonomy_decision_id": dispatch.decision_evidence["record_id"],
                        "autonomous_approval_id": dispatch.approval.id,
                        "autonomous_outcome_id": outcome_record["record_id"],
                        "error": outcome.error,
                    },
                )
                continue

            result = outcome.result
            adapter_dispatches += 1
            if result.ok:
                consecutive_failures = 0
            else:
                consecutive_failures += 1
            artifact_ids = [artifact.id for artifact in result.manifest.artifacts] if result.manifest else []
            outcome_record = _record_autonomous_outcome(
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
            _record_run_autonomy_event(project_root, dispatch.decision_evidence, decision, dispatch.approval, outcome_record)
            step_results.append(
                ObjectiveRunnerStep(
                    step=adapter_dispatches,
                    batch=batch_index,
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
                    "batch": batch_index,
                    "task_id": task.id,
                    "lease_id": lease.id,
                    "run_id": result.run.id if result.run else None,
                    "artifact_ids": artifact_ids,
                    "adapter_id": result.adapter_id,
                    "ok": result.ok,
                    "decision": result.decision,
                    "autonomy_decision_id": dispatch.decision_evidence["record_id"],
                    "autonomous_approval_id": dispatch.approval.id,
                    "autonomous_outcome_id": outcome_record["record_id"],
                    "policy_id": decision.policy_id,
                    "stop_reason": None,
                },
            )
            if not result.ok and not batch_failed:
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
                batch_failed = True

        batch_dispatches = adapter_dispatches - batch_start_dispatches
        _append_objective_event(
            evidence_path,
            run_id,
            "batch_completed",
            {
                "batch": batch_index,
                "task_ids": [item.task.id for item in prepared],
                "batch_dispatches": batch_dispatches,
                "cumulative_adapter_dispatches": adapter_dispatches,
                "adapter_dispatches": adapter_dispatches,
                "execution_errors": batch_execution_errors,
                "failed": batch_failed,
                "pending_stop_reason": pending_stop_reason,
            },
        )
        if batch_failed:
            break
        if pending_stop_reason is not None:
            stop_reason = pending_stop_reason
            pause_reasons = pending_pause_reasons
            break
        terminal = _terminal_stop_reason(store.list_tasks(objective_id=objective_id))
        if terminal is not None:
            stop_reason = terminal
            break
        if _timeout_expired(timeout_deadline):
            objective = _mark_objective_timed_out(
                store,
                objective,
                timeout_seconds=timeout_seconds,
                run_id=run_id,
                scheduler_mode="bounded_parallel",
                actor=owner,
            )
            stop_reason = "timed_out"
            pause_reasons = _timeout_pause_reasons(objective, timeout_seconds=timeout_seconds)
            break
        if consecutive_failures >= policy.budget.max_consecutive_failures:
            stop_reason = "consecutive_failure_budget_exhausted"
            break
    else:
        stop_reason = "adapter_dispatch_budget_exhausted"

    if stop_reason == "not_started":
        stop_reason = "adapter_dispatch_budget_exhausted"

    final_statuses = {task.id: task.status.value for task in store.list_tasks(objective_id=objective_id)}
    ordered_step_results = sorted(step_results, key=lambda step: (step.step, step.batch or 0, step.task_id or ""))
    result = ObjectiveRunnerResult(
        ok=stop_reason == "objective_succeeded",
        project_root=project_root,
        objective_id=objective_id,
        autonomy_profile_id=autonomy_profile_id,
        scheduler_mode="bounded_parallel",
        stop_reason=stop_reason,
        steps=len(step_results),
        batches=batches,
        max_parallel=bounded_parallelism,
        adapter_dispatches=adapter_dispatches,
        consecutive_failures=consecutive_failures,
        evidence_path=evidence_path,
        step_results=ordered_step_results,
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
    max_parallel: int = 1,
    timeout_seconds: int | None = None,
    owner: str = OBJECTIVE_RUNNER_OWNER,
) -> ObjectiveRunnerResult | None:
    project_root = resolve_project_root(project_root)
    store = SQLiteStore(project_root)
    objectives = [objective for objective in store.list_objectives() if objective.status.value == "active"]
    for objective in objectives:
        tasks = store.list_tasks(objective_id=objective.id)
        if any(task.status in {TaskStatus.READY, TaskStatus.BLOCKED, TaskStatus.LEASED} for task in tasks):
            if max_parallel > 1:
                return run_objective_parallel(
                    project_root,
                    objective.id,
                    autonomy_profile_id=autonomy_profile_id,
                    max_steps=max_steps,
                    max_parallel=max_parallel,
                    timeout_seconds=timeout_seconds,
                    owner=owner,
                )
            return run_objective_autonomously(
                project_root,
                objective.id,
                autonomy_profile_id=autonomy_profile_id,
                max_steps=max_steps,
                timeout_seconds=timeout_seconds,
                owner=owner,
            )
    return None


def _objective_inactive_result(
    project_root: Path,
    store: SQLiteStore,
    objective: ObjectiveRecord,
    autonomy_profile_id: str,
    evidence_path: Path,
    *,
    scheduler_mode: str = "sequential",
    max_parallel: int = 1,
) -> ObjectiveRunnerResult:
    return ObjectiveRunnerResult(
        ok=False,
        project_root=project_root,
        objective_id=objective.id,
        autonomy_profile_id=autonomy_profile_id,
        scheduler_mode=scheduler_mode,
        stop_reason="objective_inactive",
        steps=0,
        batches=0,
        max_parallel=max_parallel,
        adapter_dispatches=0,
        consecutive_failures=0,
        evidence_path=evidence_path,
        step_results=[],
        final_task_statuses={task.id: task.status.value for task in store.list_tasks(objective_id=objective.id)},
        pause_reasons=[
            {
                "decision": "objective_inactive",
                "objective_status": objective.status.value,
                "reason": f"Objective status is {objective.status.value}; only active objectives can be dispatched.",
            }
        ],
        errors=[],
    )


def _objective_timed_out_result(
    project_root: Path,
    store: SQLiteStore,
    objective: ObjectiveRecord,
    autonomy_profile_id: str,
    evidence_path: Path,
    *,
    timeout_seconds: int | None,
    scheduler_mode: str = "sequential",
    max_parallel: int = 1,
) -> ObjectiveRunnerResult:
    return ObjectiveRunnerResult(
        ok=False,
        project_root=project_root,
        objective_id=objective.id,
        autonomy_profile_id=autonomy_profile_id,
        scheduler_mode=scheduler_mode,
        stop_reason="timed_out",
        steps=0,
        batches=0,
        max_parallel=max_parallel,
        adapter_dispatches=0,
        consecutive_failures=0,
        evidence_path=evidence_path,
        step_results=[],
        final_task_statuses={task.id: task.status.value for task in store.list_tasks(objective_id=objective.id)},
        pause_reasons=_timeout_pause_reasons(objective, timeout_seconds=timeout_seconds),
        errors=[],
    )


def _checkpoint_blocked_result(
    project_root: Path,
    store: SQLiteStore,
    objective_id: str,
    autonomy_profile_id: str,
    evidence_path: Path,
    checkpoint_gate: ObjectiveCheckpointGate,
    *,
    scheduler_mode: str = "sequential",
    max_parallel: int = 1,
) -> ObjectiveRunnerResult:
    return ObjectiveRunnerResult(
        ok=False,
        project_root=project_root,
        objective_id=objective_id,
        autonomy_profile_id=autonomy_profile_id,
        scheduler_mode=scheduler_mode,
        stop_reason="checkpoint_blocked",
        steps=0,
        batches=0,
        max_parallel=max_parallel,
        adapter_dispatches=0,
        consecutive_failures=0,
        evidence_path=evidence_path,
        step_results=[],
        final_task_statuses={task.id: task.status.value for task in store.list_tasks(objective_id=objective_id)},
        pause_reasons=_checkpoint_gate_pause_reasons(checkpoint_gate),
        errors=[],
    )


def _append_checkpoint_blocked_events(
    evidence_path: Path,
    run_id: str,
    checkpoint_gate: ObjectiveCheckpointGate,
    result: ObjectiveRunnerResult,
) -> None:
    _append_objective_event(
        evidence_path,
        run_id,
        "checkpoint_blocked",
        {
            "gate_id": checkpoint_gate.gate_id,
            "gate_status": checkpoint_gate.status,
            "pending_checkpoint_ids": checkpoint_gate.pending_checkpoint_ids,
            "rejected_checkpoint_ids": checkpoint_gate.rejected_checkpoint_ids,
            "required_checkpoint_count": checkpoint_gate.required_checkpoint_count,
            "reasons": checkpoint_gate.reasons,
        },
    )
    _append_objective_event(
        evidence_path,
        run_id,
        "stopped",
        result.model_dump(mode="json", exclude={"schema_version", "project_root", "evidence_path"}),
    )


def _checkpoint_gate_pause_reasons(checkpoint_gate: ObjectiveCheckpointGate) -> list[dict[str, Any]]:
    return [
        {
            "gate_id": checkpoint_gate.gate_id,
            "decision": checkpoint_gate.status,
            "pending_checkpoint_ids": list(checkpoint_gate.pending_checkpoint_ids),
            "rejected_checkpoint_ids": list(checkpoint_gate.rejected_checkpoint_ids),
            "reasons": list(checkpoint_gate.reasons),
        }
    ]


def _prepare_autonomous_dispatch(
    project_root: Path,
    run_id: str,
    objective_id: str,
    policy_id: str,
    batch: int,
    task: TaskRecord,
    lease: TaskLease,
    *,
    selection_source: ObjectiveSelectionSource = "new_guarded_lease",
) -> _PreparedDispatch:
    decision = _evaluate_task_dispatch_autonomy(project_root, policy_id, task, lease)
    decision_evidence = _record_autonomy_decision(project_root, run_id, objective_id, task, lease, decision)
    if decision.status == AutonomyDecisionStatus.AUTO_ALLOWED:
        approval = _record_autonomous_approval(project_root, run_id, objective_id, task, lease, decision)
    else:
        approval = AutonomousApprovalRecord(
            id=f"auto_not_granted_{uuid.uuid4().hex[:8]}",
            policy_id=decision.policy_id,
            decision_status=decision.status,
            tool_name="dispatch_registered_adapter",
            adapter_id=decision.adapter_id,
            task_type=decision.task_type,
            boundary=decision.boundary or "unknown",
            risk=decision.risk or "sandboxed_execution",
            reasons=decision.reasons,
        )
    return _PreparedDispatch(
        batch=batch,
        task=task,
        lease=lease,
        selection_source=selection_source,
        decision=decision,
        decision_evidence=decision_evidence,
        approval=approval,
    )


def _record_parallel_autonomy_stop(
    evidence_path: Path,
    run_id: str,
    step: int,
    batch: int,
    task: TaskRecord,
    lease: TaskLease | None,
    decision: AutonomyDecision,
    decision_evidence: dict[str, Any],
    step_results: list[ObjectiveRunnerStep],
) -> tuple[str, list[dict[str, Any]]]:
    lease_id = lease.id if lease is not None else None
    stop_reason = decision.status.value
    pause_reasons = [
        {
            "task_id": task.id,
            "lease_id": lease_id,
            "adapter_id": decision.adapter_id,
            "decision": decision.status.value,
            "reasons": decision.reasons,
        }
    ]
    step_results.append(
        ObjectiveRunnerStep(
            step=step,
            batch=batch,
            task_id=task.id,
            lease_id=lease_id,
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
            "batch": batch,
            "task_id": task.id,
            "lease_id": lease_id,
            "autonomy_decision_id": decision_evidence["record_id"],
            "decision": decision.model_dump(mode="json"),
        },
    )
    return stop_reason, pause_reasons


def _record_lease_guard_stop(
    evidence_path: Path,
    run_id: str,
    step: int,
    task: TaskRecord,
    lease: TaskLease | None,
    decision: AutonomyDecision,
    decision_evidence: dict[str, Any],
    guard_pause_reasons: list[dict[str, Any]],
    step_results: list[ObjectiveRunnerStep],
    *,
    batch: int | None = None,
) -> str:
    lease_id = lease.id if lease is not None else None
    stop_reason = _pause_stop_reason(str(guard_pause_reasons[0].get("decision") or "blocked_or_no_ready_task"))
    step_results.append(
        ObjectiveRunnerStep(
            step=step,
            batch=batch,
            task_id=task.id,
            lease_id=lease_id,
            adapter_id=decision.adapter_id,
            task_type=decision.task_type,
            decision_status=decision.status.value,
            stop_reason=stop_reason,
        )
    )
    _append_objective_event(
        evidence_path,
        run_id,
        "lease_guard_stopped",
        {
            "batch": batch,
            "task_id": task.id,
            "lease_id": lease_id,
            "adapter_id": decision.adapter_id,
            "task_type": decision.task_type,
            "autonomy_decision_id": decision_evidence["record_id"],
            "decision": decision.model_dump(mode="json"),
            "stop_reason": stop_reason,
            "guard_pause_reasons": guard_pause_reasons,
        },
    )
    return stop_reason


def _parallel_batch_plan_event(
    *,
    batch_index: int,
    tasks: list[TaskRecord],
    prepared: list[_PreparedDispatch],
    batch_capacity: int,
    max_parallel: int,
    remaining_dispatch_budget: int,
    pending_stop_reason: str | None,
) -> dict[str, Any]:
    task_by_id = {task.id: task for task in tasks}
    schedule_profiles = _task_schedule_profiles(tasks)
    candidate_statuses = {TaskStatus.READY, TaskStatus.LEASED}
    dependency_snapshots = [
        _task_dependency_snapshot(task, task_by_id, schedule_profiles)
        for task in sorted(tasks, key=lambda item: _task_schedule_sort_key(item, schedule_profiles))
        if task.depends_on or task.status in candidate_statuses
    ]
    plan = ObjectiveBatchPlan(
        batch=batch_index,
        scheduler_mode="bounded_parallel",
        scheduler_policy="priority_then_critical_path",
        max_parallel=max_parallel,
        batch_capacity=batch_capacity,
        remaining_dispatch_budget=remaining_dispatch_budget,
        candidate_task_ids=[
            task.id
            for task in sorted(tasks, key=lambda item: _task_schedule_sort_key(item, schedule_profiles))
            if task.status in candidate_statuses
        ],
        blocked_task_ids=[task.id for task in tasks if task.status == TaskStatus.BLOCKED],
        schedule_profiles={
            task_id: _objective_schedule_profile(profile)
            for task_id, profile in sorted(schedule_profiles.items())
        },
        selected_task_ids=[dispatch.task.id for dispatch in prepared],
        selected_lease_ids=[dispatch.lease.id for dispatch in prepared],
        selected=[
            ObjectiveBatchSelection(
                task_id=dispatch.task.id,
                lease_id=dispatch.lease.id,
                adapter_id=dispatch.decision.adapter_id,
                task_type=dispatch.decision.task_type,
                selection_source=dispatch.selection_source,
                decision_status=dispatch.decision.status.value,
                autonomy_decision_id=dispatch.decision_evidence["record_id"],
                depends_on=list(dispatch.task.depends_on),
                workflow_stage=dispatch.task.metadata.get("workflow_stage"),
                schedule_profile=_objective_schedule_profile(schedule_profiles[dispatch.task.id]),
            )
            for dispatch in prepared
        ],
        dependency_snapshots=dependency_snapshots,
        pending_stop_reason=pending_stop_reason,
    )
    return plan.model_dump(mode="json")


def _task_dependency_snapshot(
    task: TaskRecord,
    task_by_id: dict[str, TaskRecord],
    schedule_profiles: dict[str, _TaskScheduleProfile],
) -> ObjectiveDependencySnapshot:
    unresolved: list[str] = []
    dependency_statuses: dict[str, str] = {}
    for dependency_id in task.depends_on:
        dependency = task_by_id.get(dependency_id)
        status = dependency.status.value if dependency is not None else "missing"
        dependency_statuses[dependency_id] = status
        if dependency is None or dependency.status not in {TaskStatus.SUCCEEDED, TaskStatus.SKIPPED}:
            unresolved.append(dependency_id)
    return ObjectiveDependencySnapshot(
        task_id=task.id,
        status=task.status.value,
        priority=task.priority,
        depends_on=list(task.depends_on),
        dependency_statuses=dependency_statuses,
        unresolved_dependency_ids=unresolved,
        schedule_profile=_objective_schedule_profile(schedule_profiles[task.id]),
        execution_adapter=task.metadata.get("execution_adapter"),
        task_type=task.metadata.get("task_type"),
        workflow_stage=task.metadata.get("workflow_stage"),
    )


def _next_scheduled_task_candidate(
    store: SQLiteStore,
    *,
    objective_id: str,
) -> TaskRecord | None:
    tasks = store.list_tasks(objective_id=objective_id)
    task_by_id = {task.id: task for task in tasks}
    schedule_profiles = _task_schedule_profiles(tasks)
    candidates = [
        task
        for task in tasks
        if task.status in {TaskStatus.READY, TaskStatus.BLOCKED}
        and not task.required_approvals
        and _dependencies_completed_in_snapshot(task, task_by_id)
    ]
    for task in sorted(candidates, key=lambda item: _task_schedule_sort_key(item, schedule_profiles)):
        if any(lease.status == TaskLeaseStatus.ACTIVE for lease in store.list_task_leases(task.id)):
            continue
        return task
    return None


def _dependencies_completed_in_snapshot(task: TaskRecord, task_by_id: dict[str, TaskRecord]) -> bool:
    for dependency_id in task.depends_on:
        dependency = task_by_id.get(dependency_id)
        if dependency is None or dependency.status != TaskStatus.SUCCEEDED:
            return False
    return True


def _task_schedule_profiles(tasks: list[TaskRecord]) -> dict[str, _TaskScheduleProfile]:
    task_by_id = {task.id: task for task in tasks}
    downstream: dict[str, list[str]] = {task.id: [] for task in tasks}
    for task in tasks:
        for dependency_id in task.depends_on:
            if dependency_id in downstream:
                downstream[dependency_id].append(task.id)

    def critical_path_depth(task_id: str, visiting: frozenset[str] = frozenset()) -> int:
        if task_id in visiting:
            return 0
        children = downstream.get(task_id, [])
        if not children:
            return 0
        next_visiting = visiting | {task_id}
        return 1 + max(critical_path_depth(child_id, next_visiting) for child_id in children)

    def downstream_count(task_id: str) -> int:
        seen: set[str] = set()
        stack = list(downstream.get(task_id, []))
        while stack:
            child_id = stack.pop()
            if child_id in seen:
                continue
            seen.add(child_id)
            stack.extend(downstream.get(child_id, []))
        return len(seen)

    return {
        task.id: _TaskScheduleProfile(
            task_id=task.id,
            priority=task.priority,
            critical_path_depth=critical_path_depth(task.id),
            downstream_task_count=downstream_count(task.id),
        )
        for task in task_by_id.values()
    }


def _task_schedule_sort_key(
    task: TaskRecord,
    profiles: dict[str, _TaskScheduleProfile],
) -> tuple[int, int, int, Any, str]:
    profile = profiles[task.id]
    return (
        -profile.priority,
        -profile.critical_path_depth,
        -profile.downstream_task_count,
        task.created_at,
        task.id,
    )


def _task_schedule_profile_json(profile: _TaskScheduleProfile) -> dict[str, int | str]:
    return _objective_schedule_profile(profile).model_dump(mode="json")


def _objective_schedule_profile(profile: _TaskScheduleProfile) -> ObjectiveScheduleProfile:
    return ObjectiveScheduleProfile(
        task_id=profile.task_id,
        priority=profile.priority,
        critical_path_depth=profile.critical_path_depth,
        downstream_task_count=profile.downstream_task_count,
    )


def _execute_prepared_dispatches(
    project_root: Path,
    owner: str,
    prepared: list[_PreparedDispatch],
) -> list[_DispatchOutcome]:
    outcomes_by_lease_id: dict[str, _DispatchOutcome] = {}
    with ThreadPoolExecutor(max_workers=len(prepared)) as executor:
        futures = {
            executor.submit(execute_lease, project_root, item.lease.id, owner): item
            for item in prepared
        }
        for future in as_completed(futures):
            item = futures[future]
            try:
                outcomes_by_lease_id[item.lease.id] = _DispatchOutcome(prepared=item, result=future.result())
            except Exception as exc:  # Defensive boundary: one worker must not hide sibling results.
                outcomes_by_lease_id[item.lease.id] = _DispatchOutcome(
                    prepared=item,
                    error=str(sanitize_for_logging(str(exc))),
                )
    return [outcomes_by_lease_id[item.lease.id] for item in prepared]


def _evaluate_task_dispatch_autonomy(
    project_root: Path,
    policy_id: str,
    task: TaskRecord,
    lease: TaskLease | None,
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
        idempotency_key=task.idempotency_key or (lease.id if lease is not None else task.id),
        evidence_contract="task,lease,run,artifact_manifest"
        if lease is not None
        else "task,pre_lease_autonomy_decision",
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
    descriptor = get_execution_adapter_descriptor(request.adapter_id)
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


def _active_objective_lease(
    store: SQLiteStore,
    tasks: list[TaskRecord],
    *,
    owner: str | None = None,
) -> TaskLease | None:
    active = _active_objective_leases(store, tasks, owner=owner)
    return active[0] if active else None


def _active_objective_leases(
    store: SQLiteStore,
    tasks: list[TaskRecord],
    *,
    owner: str | None = None,
    limit: int | None = None,
) -> list[TaskLease]:
    active: list[TaskLease] = []
    task_ids = {task.id for task in tasks}
    for task_id in task_ids:
        active.extend(
            lease
            for lease in store.list_task_leases(task_id)
            if lease.status == TaskLeaseStatus.ACTIVE and (owner is None or lease.owner == owner)
        )
    ordered = sorted(active, key=lambda lease: (lease.acquired_at, lease.id))
    return ordered[:limit] if limit is not None else ordered


def _objective_pause_reasons(
    store: SQLiteStore,
    objective_id: str,
    *,
    owner: str | None = None,
) -> list[dict[str, Any]]:
    reasons: list[dict[str, Any]] = []
    for task in store.list_tasks(objective_id=objective_id):
        active_leases = [
            lease
            for lease in store.list_task_leases(task.id)
            if lease.status == TaskLeaseStatus.ACTIVE and (owner is None or lease.owner != owner)
        ]
        if active_leases:
            reasons.append(
                {
                    "task_id": task.id,
                    "status": task.status.value,
                    "decision": "active_lease",
                    "reason": "Task has an active lease owned by another runner.",
                    "active_lease_ids": [lease.id for lease in active_leases],
                    "active_lease_owners": sorted({lease.owner for lease in active_leases}),
                }
            )
            continue
        eligibility = store.daemon_task_eligibility(task)
        if eligibility["decision"] in {
            "active_lease",
            "blocked_dependency",
            "breaker_open",
            "control_disabled",
            "policy_forbidden",
            "waiting_approval",
        }:
            reasons.append(eligibility)
    return reasons


def _pause_stop_reason(decision: str) -> str:
    return "approval_required" if decision == "waiting_approval" else decision


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
    descriptor = get_execution_adapter_descriptor(adapter_id)
    for control in controls:
        if descriptor is not None and runtime_control_matches_descriptor(control, descriptor, task_type):
            return True
        if descriptor is None and _fallback_control_matches_unknown_adapter(control.target_kind.value, control.target_id, adapter_id, task_type):
            return True
    return False


def _fallback_control_matches_unknown_adapter(
    target_kind: str,
    target_id: str,
    adapter_id: str | None,
    task_type: str | None,
) -> bool:
    target = target_id or "*"
    if target_kind == "adapter":
        return target in {"*", adapter_id}
    if target_kind == "task_type":
        return target in {"*", task_type}
    return False


def _record_autonomy_decision(
    project_root: Path,
    run_id: str,
    objective_id: str,
    task: TaskRecord,
    lease: TaskLease | None,
    decision: AutonomyDecision,
) -> dict[str, Any]:
    lease_id = lease.id if lease is not None else None
    payload = {
        **decision.model_dump(mode="json"),
        "record_id": f"adec_{uuid.uuid4().hex[:12]}",
        "objective_run_id": run_id,
        "objective_id": objective_id,
        "task_id": task.id,
        "lease_id": lease_id,
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
    *,
    error: str | None = None,
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
    if error is not None:
        payload["error"] = str(sanitize_for_logging(error))
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
    event_index = _next_objective_event_index(path)
    event_record = {
        "schema_version": OBJECTIVE_RUNNER_EVENT_SCHEMA_VERSION,
        **sanitize_for_logging(payload),
        "objective_id": path.stem,
        "objective_run_id": run_id,
        "objective_event_id": f"oevt_{uuid.uuid4().hex[:12]}",
        "event_index": event_index,
        "event": event,
        "created_at": utc_now().isoformat(),
        "previous_event_sha256": _previous_objective_event_sha256(path),
    }
    event_record["event_sha256"] = _objective_event_sha256(event_record)
    append_jsonl(path, event_record)


def _next_objective_event_index(path: Path) -> int:
    if not path.exists():
        return 1
    return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip()) + 1


def _previous_objective_event_sha256(path: Path) -> str | None:
    if not path.exists():
        return None
    for line in reversed(path.read_text(encoding="utf-8").splitlines()):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return None
        event_sha256 = event.get("event_sha256") if isinstance(event, dict) else None
        return event_sha256 if isinstance(event_sha256, str) and event_sha256 else None
    return None


def _objective_event_sha256(event: dict[str, Any]) -> str:
    stable = {key: value for key, value in event.items() if key != "event_sha256"}
    return hashlib.sha256(json.dumps(stable, sort_keys=True, default=str).encode("utf-8")).hexdigest()
