from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from harness.config import HARNESS_DIR
from harness.objective_evidence import read_objective_evidence_events, verify_objective_evidence
from harness.paths import resolve_project_root
from harness.security import sanitize_for_logging


ORCHESTRATION_REPLAY_AUDIT_SCHEMA_VERSION = "harness.orchestration_replay_audit/v1"
ORCHESTRATION_REPLAY_CASE_SCHEMA_VERSION = "harness.orchestration_replay_case/v1"
ORCHESTRATION_REPLAY_SUMMARY_SCHEMA_VERSION = "harness.orchestration_replay_summary/v1"

ReplayStatus = Literal["pass", "fail", "skipped"]


class OrchestrationReplayCase(BaseModel):
    schema_version: str = ORCHESTRATION_REPLAY_CASE_SCHEMA_VERSION
    id: str
    status: ReplayStatus
    source_kind: Literal["synthetic", "objective_evidence"]
    message: str
    event_count: int = 0
    expected_issue_codes: list[str] = Field(default_factory=list)
    detected_issue_codes: list[str] = Field(default_factory=list)
    replay_summary: dict[str, Any] = Field(default_factory=dict)
    evidence: dict[str, Any] = Field(default_factory=dict)
    gaps: list[str] = Field(default_factory=list)
    next_actions: list[str] = Field(default_factory=list)


class OrchestrationReplayAudit(BaseModel):
    schema_version: str = ORCHESTRATION_REPLAY_AUDIT_SCHEMA_VERSION
    ok: bool
    suite: str = "orchestration-replay"
    project_root: Path
    initialized: bool
    safety: dict[str, bool] = Field(default_factory=dict)
    summary: dict[str, int] = Field(default_factory=dict)
    cases: list[OrchestrationReplayCase] = Field(default_factory=list)


def run_orchestration_replay_audit(project_root: Path) -> OrchestrationReplayAudit:
    """Replay synthetic and captured orchestration event logs without executing work."""

    root = resolve_project_root(project_root)
    initialized = (root / HARNESS_DIR / "harness.sqlite").exists()
    cases = [*_synthetic_replay_cases(), _captured_objective_evidence_case(root, initialized=initialized)]
    summary = _summary(cases)
    return OrchestrationReplayAudit(
        ok=summary["fail"] == 0,
        project_root=root,
        initialized=initialized,
        safety=_safety_flags(),
        summary=summary,
        cases=cases,
    )


def summarize_orchestration_replay(audit: OrchestrationReplayAudit) -> dict[str, Any]:
    failing = [case.id for case in audit.cases if case.status == "fail"]
    skipped = [case.id for case in audit.cases if case.status == "skipped"]
    status = "fail" if failing else "pass"
    return {
        "schema_version": ORCHESTRATION_REPLAY_SUMMARY_SCHEMA_VERSION,
        "ok": audit.ok,
        "status": status,
        "initialized": audit.initialized,
        "summary": dict(audit.summary),
        "failing_case_ids": failing,
        "skipped_case_ids": skipped,
        "safety": dict(audit.safety),
        "next_action": f"harness evals run --suite orchestration-replay --project {audit.project_root} --output json",
        "command": f"harness evals run --suite orchestration-replay --project {audit.project_root} --output json",
    }


def replay_objective_event_log(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Reduce an objective event log into a deterministic semantic summary."""

    issues: list[dict[str, Any]] = []
    event_type_counts: Counter[str] = Counter()
    event_indexes: dict[int, int] = {}
    event_ids: dict[str, int] = {}
    expected_index = 1
    started_count = 0
    stopped_count = 0
    stopped_seen = False
    blocking_event_seen = False
    dispatch_count = 0
    execution_error_count = 0
    dispatch_fingerprints: dict[tuple[str | None, str | None, str | None], int] = {}
    batch_plans: dict[int, set[str]] = {}
    batch_starts: dict[int, set[str]] = {}
    batch_terminal_tasks: dict[int, set[str]] = defaultdict(set)
    completed_batches: set[int] = set()
    checkpoint_blocked = False
    stop_reason: str | None = None
    head_event_sha256: str | None = None

    for position, event in enumerate(events, start=1):
        event_name = str(event.get("event") or "")
        if event_name:
            event_type_counts[event_name] += 1
        event_index = event.get("event_index")
        if not isinstance(event_index, int) or isinstance(event_index, bool):
            _issue(issues, "event_index_missing_or_invalid", position=position, value=event_index)
        else:
            if event_index in event_indexes:
                _issue(
                    issues,
                    "event_index_duplicate",
                    position=position,
                    event_index=event_index,
                    previous_position=event_indexes[event_index],
                )
            event_indexes[event_index] = position
            if event_index != expected_index:
                _issue(issues, "event_index_out_of_sequence", position=position, expected=expected_index, actual=event_index)
            expected_index += 1

        event_id = event.get("objective_event_id")
        if isinstance(event_id, str) and event_id:
            if event_id in event_ids:
                _issue(issues, "objective_event_id_duplicate", position=position, previous_position=event_ids[event_id])
            event_ids[event_id] = position
        else:
            _issue(issues, "objective_event_id_missing", position=position)

        if stopped_seen:
            _issue(issues, "event_after_stopped", position=position, event=event_name)

        if event_name == "started":
            started_count += 1
        elif started_count == 0:
            _issue(issues, "event_before_started", position=position, event=event_name)

        if blocking_event_seen and event_name == "adapter_dispatched":
            _issue(issues, "dispatch_after_blocking_event", position=position)

        if event_name == "checkpoint_blocked":
            checkpoint_blocked = True
            blocking_event_seen = True
        elif event_name in {"autonomy_stopped", "lease_guard_stopped"}:
            blocking_event_seen = True
            batch = _positive_int_or_none(event.get("batch"))
            task_id = _str_or_none(event.get("task_id"))
            if batch is not None and task_id:
                batch_terminal_tasks[batch].add(task_id)
        elif event_name == "batch_planned":
            batch = _positive_int_or_none(event.get("batch"))
            if batch is not None:
                batch_plans[batch] = set(_string_items(event.get("selected_task_ids")))
        elif event_name == "batch_started":
            batch = _positive_int_or_none(event.get("batch"))
            if batch is not None:
                batch_starts[batch] = set(_string_items(event.get("task_ids")))
                if batch not in batch_plans:
                    _issue(issues, "batch_started_without_plan", position=position, batch=batch)
        elif event_name == "adapter_dispatched":
            dispatch_count += 1
            batch = _positive_int_or_none(event.get("batch"))
            task_id = _str_or_none(event.get("task_id"))
            lease_id = _str_or_none(event.get("lease_id"))
            run_id = _str_or_none(event.get("run_id"))
            if batch is not None and task_id:
                batch_terminal_tasks[batch].add(task_id)
            fingerprint = (task_id, lease_id, run_id)
            if all(fingerprint):
                if fingerprint in dispatch_fingerprints:
                    _issue(
                        issues,
                        "duplicate_side_effect_dispatch",
                        position=position,
                        previous_position=dispatch_fingerprints[fingerprint],
                        task_id=task_id,
                        lease_id=lease_id,
                        run_id=run_id,
                    )
                dispatch_fingerprints[fingerprint] = position
        elif event_name == "execution_error":
            execution_error_count += 1
            batch = _positive_int_or_none(event.get("batch"))
            task_id = _str_or_none(event.get("task_id"))
            if batch is not None and task_id:
                batch_terminal_tasks[batch].add(task_id)
        elif event_name == "batch_completed":
            batch = _positive_int_or_none(event.get("batch"))
            if batch is not None:
                completed_batches.add(batch)
                if batch not in batch_starts:
                    _issue(issues, "batch_completed_without_start", position=position, batch=batch)
                selected = batch_plans.get(batch, set())
                terminal = batch_terminal_tasks.get(batch, set())
                missing_terminal = sorted(selected - terminal)
                if missing_terminal:
                    _issue(
                        issues,
                        "batch_completed_missing_terminal_task",
                        position=position,
                        batch=batch,
                        missing_task_ids=missing_terminal,
                    )
                task_ids = set(_string_items(event.get("task_ids")))
                if selected and task_ids and selected != task_ids:
                    _issue(
                        issues,
                        "batch_completed_task_ids_drift",
                        position=position,
                        batch=batch,
                        expected_task_ids=sorted(selected),
                        actual_task_ids=sorted(task_ids),
                    )
        elif event_name == "stopped":
            stopped_count += 1
            stopped_seen = True
            stop_reason = _str_or_none(event.get("stop_reason"))
            expected_dispatches = event.get("adapter_dispatches")
            if isinstance(expected_dispatches, int) and not isinstance(expected_dispatches, bool):
                if expected_dispatches != dispatch_count:
                    _issue(
                        issues,
                        "stopped_summary_adapter_dispatches_mismatch",
                        position=position,
                        expected=expected_dispatches,
                        actual=dispatch_count,
                    )
            expected_batches = event.get("batches")
            if isinstance(expected_batches, int) and not isinstance(expected_batches, bool):
                if expected_batches != len(completed_batches):
                    _issue(
                        issues,
                        "stopped_summary_batches_mismatch",
                        position=position,
                        expected=expected_batches,
                        actual=len(completed_batches),
                    )
            if checkpoint_blocked and stop_reason != "checkpoint_blocked":
                _issue(
                    issues,
                    "checkpoint_blocked_stop_reason_mismatch",
                    position=position,
                    expected="checkpoint_blocked",
                    actual=stop_reason,
                )

        if isinstance(event.get("event_sha256"), str):
            head_event_sha256 = event["event_sha256"]

    if started_count == 0:
        _issue(issues, "missing_started_event")
    if started_count > 1:
        _issue(issues, "multiple_started_events", count=started_count)
    if stopped_count == 0:
        _issue(issues, "missing_stopped_event")
    if stopped_count > 1:
        _issue(issues, "multiple_stopped_events", count=stopped_count)

    issue_codes = sorted({str(issue["code"]) for issue in issues})
    return sanitize_for_logging(
        {
            "event_count": len(events),
            "event_type_counts": dict(sorted(event_type_counts.items())),
            "adapter_dispatches": dispatch_count,
            "execution_errors": execution_error_count,
            "batches_planned": len(batch_plans),
            "batches_started": len(batch_starts),
            "batches_completed": len(completed_batches),
            "checkpoint_blocked": checkpoint_blocked,
            "stop_reason": stop_reason,
            "head_event_sha256": head_event_sha256,
            "issue_count": len(issues),
            "issue_codes": issue_codes,
            "issues": issues,
        }
    )


def _synthetic_replay_cases() -> list[OrchestrationReplayCase]:
    scenarios = [
        (
            "synthetic_happy_checkpoint_stop",
            _checkpoint_blocked_events(),
            [],
            "Synthetic checkpoint-blocked objective replayed without semantic drift.",
        ),
        (
            "synthetic_duplicate_dispatch_detection",
            _duplicate_dispatch_events(),
            ["duplicate_side_effect_dispatch"],
            "Synthetic duplicate side-effect dispatch was detected.",
        ),
        (
            "synthetic_slow_branch_barrier_detection",
            _missing_branch_completion_events(),
            ["batch_completed_missing_terminal_task"],
            "Synthetic fan-out/fan-in replay detected a missing branch terminal event.",
        ),
        (
            "synthetic_approval_reject_detection",
            _approval_reject_dispatch_events(),
            ["dispatch_after_blocking_event"],
            "Synthetic approval-reject replay detected dispatch after a blocking event.",
        ),
        (
            "synthetic_missing_terminal_detection",
            _missing_terminal_events(),
            ["missing_stopped_event"],
            "Synthetic missing-terminal replay was detected.",
        ),
    ]
    cases: list[OrchestrationReplayCase] = []
    for case_id, events, expected_codes, message in scenarios:
        replay = replay_objective_event_log(events)
        detected_codes = list(replay["issue_codes"])
        missing = sorted(set(expected_codes) - set(detected_codes))
        unexpected = sorted(set(detected_codes) - set(expected_codes))
        status: ReplayStatus = "pass" if not missing and not unexpected else "fail"
        cases.append(
            OrchestrationReplayCase(
                id=case_id,
                status=status,
                source_kind="synthetic",
                message=message if status == "pass" else "Synthetic replay detector drifted from expected issue codes.",
                event_count=len(events),
                expected_issue_codes=list(expected_codes),
                detected_issue_codes=detected_codes,
                replay_summary=_compact_replay_summary(replay),
                evidence={"missing_expected_issue_codes": missing, "unexpected_issue_codes": unexpected},
                gaps=[]
                if status == "pass"
                else ["Synthetic replay reducer no longer detects the expected failure semantics."],
                next_actions=[]
                if status == "pass"
                else ["Inspect orchestration replay reducer issue-code expectations before changing event semantics."],
            )
        )
    return cases


def _captured_objective_evidence_case(project_root: Path, *, initialized: bool) -> OrchestrationReplayCase:
    if not initialized:
        return OrchestrationReplayCase(
            id="captured_objective_evidence_replay",
            status="skipped",
            source_kind="objective_evidence",
            message="Project is not initialized; no captured objective evidence was replayed.",
            evidence={"initialized": False},
            next_actions=["Initialize a project and run objectives before using captured objective replay as drift evidence."],
        )
    evidence_dir = project_root / HARNESS_DIR / "autonomy" / "objectives"
    evidence_paths = sorted(
        path
        for path in evidence_dir.glob("*.jsonl")
        if path.is_file() and not path.name.endswith(".checkpoints.jsonl")
    )
    if not evidence_paths:
        return OrchestrationReplayCase(
            id="captured_objective_evidence_replay",
            status="skipped",
            source_kind="objective_evidence",
            message="No captured objective evidence JSONL files were present.",
            evidence={"initialized": True, "evidence_dir": str(evidence_dir)},
            next_actions=["Run or reconcile an objective before using captured objective replay as drift evidence."],
        )

    checked: list[dict[str, Any]] = []
    failed_objective_ids: list[str] = []
    total_event_type_counts: Counter[str] = Counter()
    all_issue_codes: set[str] = set()
    total_events = 0
    for evidence_path in evidence_paths:
        objective_id = evidence_path.stem
        events_with_lines, parse_errors = read_objective_evidence_events(evidence_path)
        events = [event for _, event in events_with_lines]
        replay = replay_objective_event_log(events)
        verification = verify_objective_evidence(project_root, objective_id, evidence_path=evidence_path)
        event_type_counts = replay.get("event_type_counts") or {}
        total_event_type_counts.update({str(key): int(value) for key, value in event_type_counts.items()})
        issue_codes = list(replay.get("issue_codes") or [])
        all_issue_codes.update(issue_codes)
        total_events += len(events)
        ok = not parse_errors and not issue_codes and verification.ok
        if not ok:
            failed_objective_ids.append(objective_id)
        checked.append(
            {
                "objective_id": objective_id,
                "event_count": len(events),
                "parse_error_count": len(parse_errors),
                "verification_ok": verification.ok,
                "verification_failed_check_ids": [
                    check.id for check in verification.checks if check.status == "fail"
                ],
                "replay_issue_codes": issue_codes,
                "replay_summary": _compact_replay_summary(replay),
                "evidence_path": str(evidence_path),
            }
        )

    status: ReplayStatus = "pass" if not failed_objective_ids else "fail"
    return OrchestrationReplayCase(
        id="captured_objective_evidence_replay",
        status=status,
        source_kind="objective_evidence",
        message="Captured objective evidence replayed without semantic drift."
        if status == "pass"
        else "One or more captured objective evidence logs failed replay drift checks.",
        event_count=total_events,
        detected_issue_codes=sorted(all_issue_codes),
        replay_summary={
            "objective_count": len(evidence_paths),
            "failed_objective_count": len(failed_objective_ids),
            "event_type_counts": dict(sorted(total_event_type_counts.items())),
        },
        evidence={
            "initialized": True,
            "evidence_dir": str(evidence_dir),
            "checked_objectives": checked,
            "failed_objective_ids": failed_objective_ids,
            "artifact_bodies_read": False,
        },
        gaps=[] if status == "pass" else ["Captured objective evidence does not replay to the current semantics."],
        next_actions=[]
        if status == "pass"
        else ["Run `harness objectives verify-evidence <objective_id> --output json` for each failed objective."],
    )


def _compact_replay_summary(replay: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_count": replay.get("event_count"),
        "event_type_counts": replay.get("event_type_counts") or {},
        "adapter_dispatches": replay.get("adapter_dispatches"),
        "execution_errors": replay.get("execution_errors"),
        "batches_planned": replay.get("batches_planned"),
        "batches_started": replay.get("batches_started"),
        "batches_completed": replay.get("batches_completed"),
        "checkpoint_blocked": replay.get("checkpoint_blocked"),
        "stop_reason": replay.get("stop_reason"),
        "head_event_sha256": replay.get("head_event_sha256"),
        "issue_count": replay.get("issue_count"),
        "issue_codes": replay.get("issue_codes") or [],
    }


def _event(index: int, event: str, **payload: Any) -> dict[str, Any]:
    base = {
        "schema_version": "harness.autonomous_objective_event/v1",
        "objective_id": "obj_replay",
        "objective_run_id": "orun_replay",
        "objective_event_id": f"oevt_replay_{index}",
        "event_index": index,
        "previous_event_sha256": None if index == 1 else f"prev_{index - 1}",
        "event_sha256": f"sha_{index}",
        "event": event,
    }
    base.update(payload)
    return base


def _checkpoint_blocked_events() -> list[dict[str, Any]]:
    return [
        _event(1, "started"),
        _event(
            2,
            "checkpoint_blocked",
            gate_id="checkpoint_approved",
            gate_status="blocked",
            pending_checkpoint_ids=["ockpt_1"],
            rejected_checkpoint_ids=[],
            reasons=["checkpoint pending"],
            required_checkpoint_count=1,
        ),
        _event(
            3,
            "stopped",
            ok=False,
            stop_reason="checkpoint_blocked",
            adapter_dispatches=0,
            batches=0,
        ),
    ]


def _duplicate_dispatch_events() -> list[dict[str, Any]]:
    return [
        _event(1, "started"),
        _event(2, "batch_planned", batch=1, selected_task_ids=["task_1"]),
        _event(3, "batch_started", batch=1, task_ids=["task_1"]),
        _event(4, "adapter_dispatched", batch=1, task_id="task_1", lease_id="lease_1", run_id="run_1"),
        _event(5, "adapter_dispatched", batch=1, task_id="task_1", lease_id="lease_1", run_id="run_1"),
        _event(6, "batch_completed", batch=1, task_ids=["task_1"], batch_dispatches=2, cumulative_adapter_dispatches=2),
        _event(7, "stopped", ok=True, stop_reason="complete", adapter_dispatches=2, batches=1),
    ]


def _missing_branch_completion_events() -> list[dict[str, Any]]:
    return [
        _event(1, "started"),
        _event(2, "batch_planned", batch=1, selected_task_ids=["task_fast", "task_slow"]),
        _event(3, "batch_started", batch=1, task_ids=["task_fast", "task_slow"]),
        _event(4, "adapter_dispatched", batch=1, task_id="task_fast", lease_id="lease_fast", run_id="run_fast"),
        _event(5, "batch_completed", batch=1, task_ids=["task_fast", "task_slow"], batch_dispatches=1),
        _event(6, "stopped", ok=False, stop_reason="complete", adapter_dispatches=1, batches=1),
    ]


def _approval_reject_dispatch_events() -> list[dict[str, Any]]:
    return [
        _event(1, "started"),
        _event(2, "autonomy_stopped", batch=1, task_id="task_denied", autonomy_decision_id="adec_denied"),
        _event(3, "adapter_dispatched", batch=1, task_id="task_denied", lease_id="lease_denied", run_id="run_denied"),
        _event(4, "stopped", ok=False, stop_reason="approval_required", adapter_dispatches=1, batches=0),
    ]


def _missing_terminal_events() -> list[dict[str, Any]]:
    return [
        _event(1, "started"),
        _event(2, "batch_planned", batch=1, selected_task_ids=["task_1"]),
        _event(3, "batch_started", batch=1, task_ids=["task_1"]),
        _event(4, "adapter_dispatched", batch=1, task_id="task_1", lease_id="lease_1", run_id="run_1"),
    ]


def _issue(issues: list[dict[str, Any]], code: str, **metadata: Any) -> None:
    issues.append({"code": code, **metadata})


def _positive_int_or_none(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _string_items(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _summary(cases: list[OrchestrationReplayCase]) -> dict[str, int]:
    return {
        "total": len(cases),
        "pass": sum(1 for case in cases if case.status == "pass"),
        "fail": sum(1 for case in cases if case.status == "fail"),
        "skipped": sum(1 for case in cases if case.status == "skipped"),
        "synthetic": sum(1 for case in cases if case.source_kind == "synthetic"),
        "captured": sum(1 for case in cases if case.source_kind == "objective_evidence"),
    }


def _safety_flags() -> dict[str, bool]:
    return {
        "read_only": True,
        "synthetic_probe_only": True,
        "reference_code_imported": False,
        "reference_contents_included": False,
        "provider_called": False,
        "network_called": False,
        "adapter_execution_started": False,
        "filesystem_modified": False,
        "permission_granting": False,
        "artifact_bodies_read": False,
        "model_context_allowed": False,
    }
