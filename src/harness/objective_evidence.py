from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from harness.config import HARNESS_DIR
from harness.memory.sqlite_store import SQLiteStore
from harness.objective_batch_plan import OBJECTIVE_BATCH_PLAN_SCHEMA_VERSION, ObjectiveBatchPlan
from harness.objective_runner import OBJECTIVE_RUNNER_EVENT_SCHEMA_VERSION
from harness.paths import resolve_project_root
from harness.security import sanitize_for_logging


OBJECTIVE_EVIDENCE_VERIFICATION_SCHEMA_VERSION = "harness.objective_evidence_verification/v1"
DISPATCH_AUTONOMY_TOOL_NAME = "dispatch_registered_adapter"
AUTONOMY_DECISION_PAYLOAD_FIELDS = (
    "schema_version",
    "status",
    "policy_id",
    "tool_name",
    "adapter_id",
    "task_type",
    "boundary",
    "risk",
    "reasons",
    "requires_human",
    "evidence_required",
)
SCHEDULER_POLICY_SORT_KEYS = [
    "priority_desc",
    "critical_path_depth_desc",
    "downstream_task_count_desc",
    "created_at_asc",
    "task_id_asc",
]
SCHEDULER_POLICY_ID = "priority_then_critical_path"


class ObjectiveEvidenceCheck(BaseModel):
    id: str
    status: Literal["pass", "fail"]
    message: str
    evidence: dict[str, Any] = Field(default_factory=dict)


class ObjectiveEvidenceVerification(BaseModel):
    schema_version: str = OBJECTIVE_EVIDENCE_VERIFICATION_SCHEMA_VERSION
    ok: bool
    project_root: Path
    objective_id: str
    evidence_path: Path
    checks: list[ObjectiveEvidenceCheck] = Field(default_factory=list)
    summary: dict[str, int] = Field(default_factory=dict)


ObjectiveEvent = tuple[int, dict[str, Any]]


@dataclass
class AutonomyRecordIndex:
    path: Path
    id_key: str
    schema_version: str
    records: dict[str, dict[str, Any]] = field(default_factory=dict)
    record_lines: dict[str, int] = field(default_factory=dict)
    parse_errors: list[dict[str, Any]] = field(default_factory=list)
    duplicate_ids: dict[str, list[int]] = field(default_factory=dict)


def verify_objective_evidence(
    project_root: Path,
    objective_id: str,
    *,
    evidence_path: Path | None = None,
) -> ObjectiveEvidenceVerification:
    project_root = resolve_project_root(project_root)
    evidence_path = evidence_path or project_root / HARNESS_DIR / "autonomy" / "objectives" / f"{objective_id}.jsonl"
    store = SQLiteStore(project_root)
    checks: list[ObjectiveEvidenceCheck] = []

    try:
        objective = store.get_objective(objective_id)
        checks.append(_pass("objective_exists", "Objective exists in Harness persistence.", {"status": objective.status.value}))
    except KeyError as exc:
        checks.append(_fail("objective_exists", "Objective is missing from Harness persistence.", {"error": str(exc)}))
        return _verification(project_root, objective_id, evidence_path, checks)

    if not evidence_path.exists():
        checks.append(_fail("evidence_file_exists", "Objective evidence JSONL file is missing.", {"path": str(evidence_path)}))
        return _verification(project_root, objective_id, evidence_path, checks)
    checks.append(_pass("evidence_file_exists", "Objective evidence JSONL file exists.", {"path": str(evidence_path)}))

    events, parse_errors = _read_objective_evidence_events_raw(evidence_path)
    if parse_errors:
        checks.append(
            _fail(
                "jsonl_parse",
                "Objective evidence contains malformed JSONL records.",
                {"line_count": len(events) + len(parse_errors), "errors": parse_errors},
            )
        )
    else:
        checks.append(_pass("jsonl_parse", "Objective evidence JSONL parses cleanly.", {"event_count": len(events)}))

    checks.append(_event_schema_check(events))
    checks.append(_event_payload_schema_check(events))
    checks.append(_event_identity_check(events))
    checks.append(_event_hash_chain_check(events))
    checks.append(_event_timestamp_check(events))
    checks.append(_event_lifecycle_check(events))
    checks.append(_objective_scope_check(events, objective_id))
    checks.append(_dispatch_link_check(project_root, store, events, objective_id))
    checks.append(_reconciled_run_link_check(store, events, objective_id))
    checks.append(_batch_plan_check(project_root, store, events, objective_id))
    checks.append(_batch_lifecycle_check(events))
    checks.append(_stopped_summary_check(project_root, store, events, objective_id))

    return _verification(project_root, objective_id, evidence_path, checks)


def read_objective_evidence_events(path: Path) -> tuple[list[ObjectiveEvent], list[dict[str, Any]]]:
    events, errors = _read_objective_evidence_events_raw(path)
    return [(line_number, sanitize_for_logging(event)) for line_number, event in events], sanitize_for_logging(errors)


def _read_objective_evidence_events_raw(path: Path) -> tuple[list[ObjectiveEvent], list[dict[str, Any]]]:
    events: list[ObjectiveEvent] = []
    errors: list[dict[str, Any]] = []
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw_line.strip():
            continue
        try:
            payload = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            errors.append({"line": line_number, "error": f"{exc.__class__.__name__}: {exc.msg}"})
            continue
        if not isinstance(payload, dict):
            errors.append({"line": line_number, "error": "JSONL record is not an object."})
            continue
        events.append((line_number, payload))
    return events, errors


def _read_objective_events(path: Path) -> tuple[list[ObjectiveEvent], list[dict[str, Any]]]:
    return read_objective_evidence_events(path)


def _event_schema_check(events: list[ObjectiveEvent]) -> ObjectiveEvidenceCheck:
    invalid: list[dict[str, Any]] = []
    for line_number, event in events:
        if event.get("schema_version") != OBJECTIVE_RUNNER_EVENT_SCHEMA_VERSION:
            invalid.append({"line": line_number, "reason": "schema_version", "value": event.get("schema_version")})
        if not isinstance(event.get("objective_id"), str) or not event.get("objective_id"):
            invalid.append({"line": line_number, "reason": "objective_id"})
        if not isinstance(event.get("objective_run_id"), str) or not event.get("objective_run_id"):
            invalid.append({"line": line_number, "reason": "objective_run_id"})
        if not isinstance(event.get("objective_event_id"), str) or not event.get("objective_event_id"):
            invalid.append({"line": line_number, "reason": "objective_event_id"})
        if not isinstance(event.get("event_index"), int) or isinstance(event.get("event_index"), bool) or event.get("event_index") <= 0:
            invalid.append({"line": line_number, "reason": "event_index", "value": event.get("event_index")})
        if "previous_event_sha256" not in event:
            invalid.append({"line": line_number, "reason": "previous_event_sha256"})
        elif event.get("previous_event_sha256") is not None and not isinstance(event.get("previous_event_sha256"), str):
            invalid.append({"line": line_number, "reason": "previous_event_sha256", "value": event.get("previous_event_sha256")})
        if not isinstance(event.get("event_sha256"), str) or not event.get("event_sha256"):
            invalid.append({"line": line_number, "reason": "event_sha256"})
        if not isinstance(event.get("event"), str) or not event.get("event"):
            invalid.append({"line": line_number, "reason": "event"})
    if invalid:
        return _fail("event_schema", "Objective evidence records do not match the event envelope.", {"invalid": invalid})
    return _pass("event_schema", "Objective evidence records match the event envelope.", {"event_count": len(events)})


def _event_payload_schema_check(events: list[ObjectiveEvent]) -> ObjectiveEvidenceCheck:
    issues: list[dict[str, Any]] = []
    event_types: set[str] = set()
    known_events = {
        "started",
        "recovery_checked",
        "adapter_dispatched",
        "autonomy_stopped",
        "lease_guard_stopped",
        "execution_error",
        "checkpoint_blocked",
        "batch_planned",
        "batch_started",
        "batch_completed",
        "reconciled_existing_run",
        "stopped",
    }
    for line_number, event in events:
        event_name = event.get("event")
        if not isinstance(event_name, str) or not event_name:
            continue
        event_types.add(event_name)
        if event_name not in known_events:
            issues.append({"line": line_number, "event": event_name, "reason": "unknown_event_type"})
            continue
        if event_name == "started":
            _require_str(event, "autonomy_profile_id", line_number, event_name, issues)
            _require_dict(event, "budget", line_number, event_name, issues)
        elif event_name == "recovery_checked":
            for key in ("renewed_lease_ids", "expired_lease_ids", "recovered_task_ids", "event_ids"):
                _require_string_list(event, key, line_number, event_name, issues)
        elif event_name == "adapter_dispatched":
            for key in (
                "task_id",
                "lease_id",
                "adapter_id",
                "decision",
                "autonomy_decision_id",
                "autonomous_approval_id",
                "autonomous_outcome_id",
                "policy_id",
            ):
                _require_str(event, key, line_number, event_name, issues)
            _require_string_list(event, "artifact_ids", line_number, event_name, issues)
            _require_bool(event, "ok", line_number, event_name, issues)
            _require_optional_positive_int(event, "batch", line_number, event_name, issues)
            _require_optional_str(event, "stop_reason", line_number, event_name, issues)
            if event.get("ok") is True:
                _require_str(event, "run_id", line_number, event_name, issues)
            else:
                _require_optional_str(event, "run_id", line_number, event_name, issues)
        elif event_name == "autonomy_stopped":
            for key in ("task_id", "autonomy_decision_id"):
                _require_str(event, key, line_number, event_name, issues)
            _require_optional_str(event, "lease_id", line_number, event_name, issues)
            _require_dict(event, "decision", line_number, event_name, issues)
            _require_optional_positive_int(event, "batch", line_number, event_name, issues)
        elif event_name == "lease_guard_stopped":
            for key in ("task_id", "adapter_id", "autonomy_decision_id", "stop_reason"):
                _require_str(event, key, line_number, event_name, issues)
            _require_optional_str(event, "lease_id", line_number, event_name, issues)
            _require_optional_str(event, "task_type", line_number, event_name, issues)
            _require_dict(event, "decision", line_number, event_name, issues)
            _require_optional_positive_int(event, "batch", line_number, event_name, issues)
            if not isinstance(event.get("guard_pause_reasons"), list):
                issues.append({"line": line_number, "event": event_name, "reason": "guard_pause_reasons_not_list"})
        elif event_name == "execution_error":
            for key in (
                "task_id",
                "lease_id",
                "adapter_id",
                "policy_id",
                "autonomy_decision_id",
                "autonomous_approval_id",
                "autonomous_outcome_id",
                "error",
            ):
                _require_str(event, key, line_number, event_name, issues)
            _require_optional_positive_int(event, "batch", line_number, event_name, issues)
        elif event_name == "checkpoint_blocked":
            for key in ("gate_id", "gate_status"):
                _require_str(event, key, line_number, event_name, issues)
            for key in ("pending_checkpoint_ids", "rejected_checkpoint_ids", "reasons"):
                _require_string_list(event, key, line_number, event_name, issues)
            _require_nonnegative_int(event, "required_checkpoint_count", line_number, event_name, issues)
        elif event_name == "batch_planned":
            _validate_batch_plan_payload(event, line_number, event_name, issues)
            if event.get("plan_schema_version") != OBJECTIVE_BATCH_PLAN_SCHEMA_VERSION:
                issues.append(
                    {
                        "line": line_number,
                        "event": event_name,
                        "field": "plan_schema_version",
                        "reason": "expected_objective_batch_plan_v1",
                    }
                )
            for key in ("batch", "max_parallel", "batch_capacity"):
                _require_positive_int(event, key, line_number, event_name, issues)
            _require_nonnegative_int(event, "remaining_dispatch_budget", line_number, event_name, issues)
            for key in ("scheduler_mode", "scheduler_policy"):
                _require_str(event, key, line_number, event_name, issues)
            for key in ("candidate_task_ids", "blocked_task_ids", "selected_task_ids", "selected_lease_ids"):
                _require_string_list(event, key, line_number, event_name, issues)
            _require_dict(event, "schedule_profiles", line_number, event_name, issues)
            _require_list(event, "selected", line_number, event_name, issues)
            _require_list(event, "dependency_snapshots", line_number, event_name, issues)
            _require_optional_str(event, "pending_stop_reason", line_number, event_name, issues)
        elif event_name == "batch_started":
            _require_positive_int(event, "batch", line_number, event_name, issues)
            for key in ("task_ids", "lease_ids"):
                _require_string_list(event, key, line_number, event_name, issues)
            _require_positive_int(event, "max_parallel", line_number, event_name, issues)
            _require_nonnegative_int(event, "remaining_dispatch_budget", line_number, event_name, issues)
        elif event_name == "batch_completed":
            _require_positive_int(event, "batch", line_number, event_name, issues)
            _require_string_list(event, "task_ids", line_number, event_name, issues)
            for key in ("batch_dispatches", "cumulative_adapter_dispatches", "adapter_dispatches", "execution_errors"):
                _require_nonnegative_int(event, key, line_number, event_name, issues)
            _require_bool(event, "failed", line_number, event_name, issues)
            _require_optional_str(event, "pending_stop_reason", line_number, event_name, issues)
        elif event_name == "reconciled_existing_run":
            for key in ("reconciliation_source", "run_id", "run_status"):
                _require_str(event, key, line_number, event_name, issues)
            for key in ("task_id", "task_type", "run_created_at", "run_updated_at"):
                _require_optional_str(event, key, line_number, event_name, issues)
            _require_string_list(event, "artifact_ids", line_number, event_name, issues)
            _require_nonnegative_int(event, "run_event_count", line_number, event_name, issues)
        elif event_name == "stopped":
            _require_bool(event, "ok", line_number, event_name, issues)
            for key in ("objective_id", "autonomy_profile_id", "scheduler_mode", "stop_reason"):
                _require_str(event, key, line_number, event_name, issues)
            for key in ("steps", "batches", "max_parallel", "adapter_dispatches", "new_tasks_created", "consecutive_failures"):
                _require_nonnegative_int(event, key, line_number, event_name, issues)
            _require_list(event, "step_results", line_number, event_name, issues)
            _require_dict(event, "final_task_statuses", line_number, event_name, issues)
            _require_list(event, "pause_reasons", line_number, event_name, issues)
            _require_list(event, "errors", line_number, event_name, issues)
            if "reconciled_run_ids" in event:
                _require_string_list(event, "reconciled_run_ids", line_number, event_name, issues)
            if "reconciled_run_count" in event:
                _require_nonnegative_int(event, "reconciled_run_count", line_number, event_name, issues)
    if issues:
        return _fail("event_payload_schema", "Objective evidence event payloads do not match their event-type schema.", {"issues": issues})
    return _pass(
        "event_payload_schema",
        "Objective evidence event payloads match their event-type schema.",
        {"event_count": len(events), "event_types": sorted(event_types)},
    )


def _require_str(event: dict[str, Any], key: str, line_number: int, event_name: str, issues: list[dict[str, Any]]) -> None:
    value = event.get(key)
    if not isinstance(value, str) or not value:
        _append_payload_schema_issue(line_number, event_name, key, "string", issues)


def _require_optional_str(event: dict[str, Any], key: str, line_number: int, event_name: str, issues: list[dict[str, Any]]) -> None:
    value = event.get(key)
    if value is not None and not isinstance(value, str):
        _append_payload_schema_issue(line_number, event_name, key, "string_or_null", issues)


def _require_bool(event: dict[str, Any], key: str, line_number: int, event_name: str, issues: list[dict[str, Any]]) -> None:
    if not isinstance(event.get(key), bool):
        _append_payload_schema_issue(line_number, event_name, key, "bool", issues)


def _require_positive_int(event: dict[str, Any], key: str, line_number: int, event_name: str, issues: list[dict[str, Any]]) -> None:
    if not _is_positive_int(event.get(key)):
        _append_payload_schema_issue(line_number, event_name, key, "positive_int", issues)


def _require_optional_positive_int(event: dict[str, Any], key: str, line_number: int, event_name: str, issues: list[dict[str, Any]]) -> None:
    value = event.get(key)
    if value is not None and not _is_positive_int(value):
        _append_payload_schema_issue(line_number, event_name, key, "positive_int_or_null", issues)


def _require_nonnegative_int(event: dict[str, Any], key: str, line_number: int, event_name: str, issues: list[dict[str, Any]]) -> None:
    value = event.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        _append_payload_schema_issue(line_number, event_name, key, "nonnegative_int", issues)


def _require_string_list(event: dict[str, Any], key: str, line_number: int, event_name: str, issues: list[dict[str, Any]]) -> None:
    value = event.get(key)
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        _append_payload_schema_issue(line_number, event_name, key, "string_list", issues)


def _require_list(event: dict[str, Any], key: str, line_number: int, event_name: str, issues: list[dict[str, Any]]) -> None:
    if not isinstance(event.get(key), list):
        _append_payload_schema_issue(line_number, event_name, key, "list", issues)


def _require_dict(event: dict[str, Any], key: str, line_number: int, event_name: str, issues: list[dict[str, Any]]) -> None:
    if not isinstance(event.get(key), dict):
        _append_payload_schema_issue(line_number, event_name, key, "object", issues)


def _append_payload_schema_issue(
    line_number: int,
    event_name: str,
    key: str,
    expected: str,
    issues: list[dict[str, Any]],
) -> None:
    issues.append({"line": line_number, "event": event_name, "field": key, "reason": f"expected_{expected}"})


def _validate_batch_plan_payload(
    event: dict[str, Any],
    line_number: int,
    event_name: str,
    issues: list[dict[str, Any]],
) -> None:
    payload = {key: event[key] for key in ObjectiveBatchPlan.model_fields if key in event}
    try:
        ObjectiveBatchPlan.model_validate(payload)
    except ValidationError as exc:
        issues.append(
            {
                "line": line_number,
                "event": event_name,
                "field": "plan_schema_version",
                "reason": "objective_batch_plan_validation_failed",
                "errors": sanitize_for_logging(exc.errors()),
            }
        )


def _event_identity_check(events: list[ObjectiveEvent]) -> ObjectiveEvidenceCheck:
    issues: list[dict[str, Any]] = []
    seen_ids: dict[str, int] = {}
    seen_indexes: dict[int, int] = {}
    for expected_index, (line_number, event) in enumerate(events, start=1):
        event_id = event.get("objective_event_id")
        if isinstance(event_id, str) and event_id:
            previous_line = seen_ids.get(event_id)
            if previous_line is not None:
                issues.append(
                    {
                        "line": line_number,
                        "reason": "objective_event_id_duplicate",
                        "objective_event_id": event_id,
                        "previous_line": previous_line,
                    }
                )
            seen_ids[event_id] = line_number
        event_index = event.get("event_index")
        if isinstance(event_index, int) and not isinstance(event_index, bool):
            previous_line = seen_indexes.get(event_index)
            if previous_line is not None:
                issues.append(
                    {
                        "line": line_number,
                        "reason": "event_index_duplicate",
                        "event_index": event_index,
                        "previous_line": previous_line,
                    }
                )
            seen_indexes[event_index] = line_number
            if event_index != expected_index:
                issues.append(
                    {
                        "line": line_number,
                        "reason": "event_index_out_of_sequence",
                        "expected": expected_index,
                        "actual": event_index,
                    }
                )
    if issues:
        return _fail(
            "event_identity",
            "Objective evidence event ids or indexes are duplicated or out of sequence.",
            {"issues": issues},
        )
    return _pass(
        "event_identity",
        "Objective evidence event ids are unique and indexes match JSONL order.",
        {"event_count": len(events), "unique_event_ids": len(seen_ids)},
    )


def _event_hash_chain_check(events: list[ObjectiveEvent]) -> ObjectiveEvidenceCheck:
    issues: list[dict[str, Any]] = []
    previous_hash: str | None = None
    for line_number, event in events:
        recorded_previous_hash = event.get("previous_event_sha256")
        if recorded_previous_hash != previous_hash:
            issues.append(
                {
                    "line": line_number,
                    "reason": "previous_event_sha256_mismatch",
                    "expected": previous_hash,
                    "actual": recorded_previous_hash,
                }
            )
        recorded_hash = event.get("event_sha256")
        recomputed_hash = _objective_event_sha256(event)
        if recorded_hash != recomputed_hash:
            issues.append(
                {
                    "line": line_number,
                    "reason": "event_sha256_mismatch",
                    "expected": recomputed_hash,
                    "actual": recorded_hash,
                }
            )
        previous_hash = recorded_hash if isinstance(recorded_hash, str) and recorded_hash else None
    if issues:
        return _fail(
            "event_hash_chain",
            "Objective evidence event hashes or previous-hash links do not match the JSONL records.",
            {"issues": issues},
        )
    return _pass(
        "event_hash_chain",
        "Objective evidence event hashes match JSONL records and previous-hash links.",
        {"event_count": len(events), "head_sha256": previous_hash},
    )


def _objective_event_sha256(event: dict[str, Any]) -> str:
    stable = {key: value for key, value in event.items() if key != "event_sha256"}
    return hashlib.sha256(json.dumps(stable, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _event_timestamp_check(events: list[ObjectiveEvent]) -> ObjectiveEvidenceCheck:
    invalid: list[dict[str, Any]] = []
    grouped: dict[str, list[tuple[int, str, datetime]]] = {}
    parsed_count = 0
    for line_number, event in events:
        created_at = event.get("created_at")
        if not isinstance(created_at, str) or not created_at:
            invalid.append({"line": line_number, "reason": "created_at_missing"})
            continue
        try:
            timestamp = datetime.fromisoformat(created_at)
        except ValueError as exc:
            invalid.append({"line": line_number, "reason": "created_at_malformed", "created_at": created_at, "error": str(exc)})
            continue
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            invalid.append({"line": line_number, "reason": "created_at_timezone_missing", "created_at": created_at})
            continue
        parsed_count += 1
        objective_run_id = event.get("objective_run_id")
        if isinstance(objective_run_id, str) and objective_run_id:
            grouped.setdefault(objective_run_id, []).append((line_number, created_at, timestamp))

    for objective_run_id, run_timestamps in sorted(grouped.items()):
        previous: tuple[int, str, datetime] | None = None
        for line_number, created_at, timestamp in run_timestamps:
            if previous is not None and timestamp < previous[2]:
                invalid.append(
                    {
                        "line": line_number,
                        "objective_run_id": objective_run_id,
                        "reason": "created_at_out_of_order",
                        "created_at": created_at,
                        "previous_line": previous[0],
                        "previous_created_at": previous[1],
                    }
                )
            previous = (line_number, created_at, timestamp)

    if invalid:
        return _fail(
            "event_timestamps",
            "Objective evidence event timestamps are missing, malformed, timezone-naive, or out of order.",
            {"invalid": invalid},
        )
    return _pass(
        "event_timestamps",
        "Objective evidence event timestamps are parseable, timezone-aware, and ordered.",
        {"event_count": parsed_count, "objective_runs": len(grouped)},
    )


def _event_lifecycle_check(events: list[ObjectiveEvent]) -> ObjectiveEvidenceCheck:
    if not events:
        return _fail("run_lifecycle", "Objective evidence contains no events.", {})
    grouped: dict[str, list[ObjectiveEvent]] = {}
    for item in events:
        run_id = item[1].get("objective_run_id")
        if isinstance(run_id, str) and run_id:
            grouped.setdefault(run_id, []).append(item)
    issues: list[dict[str, Any]] = []
    for run_id, run_events in sorted(grouped.items()):
        names = [event.get("event") for _, event in run_events]
        if not names or names[0] != "started":
            issues.append({"objective_run_id": run_id, "reason": "first_event_not_started", "events": names})
        if not names or names[-1] != "stopped":
            issues.append({"objective_run_id": run_id, "reason": "last_event_not_stopped", "events": names})
    if issues:
        return _fail("run_lifecycle", "One or more objective runs have an incomplete event lifecycle.", {"issues": issues})
    return _pass("run_lifecycle", "Every objective run starts and stops in the evidence log.", {"objective_runs": len(grouped)})


def _objective_scope_check(events: list[ObjectiveEvent], objective_id: str) -> ObjectiveEvidenceCheck:
    mismatches: list[dict[str, Any]] = []
    for line_number, event in events:
        event_objective_id = event.get("objective_id")
        if event_objective_id is not None and event_objective_id != objective_id:
            mismatches.append({"line": line_number, "objective_id": event_objective_id})
    if mismatches:
        return _fail("objective_scope", "Objective evidence contains records for another objective.", {"mismatches": mismatches})
    return _pass("objective_scope", "Objective-scoped records match the requested objective.", {"objective_id": objective_id})


def _dispatch_link_check(
    project_root: Path,
    store: SQLiteStore,
    events: list[ObjectiveEvent],
    objective_id: str,
) -> ObjectiveEvidenceCheck:
    decisions = _read_autonomy_records(
        project_root / HARNESS_DIR / "autonomy" / "decisions.jsonl",
        "record_id",
        "harness.autonomy_decision/v1",
    )
    approvals = _read_autonomy_records(
        project_root / HARNESS_DIR / "autonomy" / "approvals.jsonl",
        "id",
        "harness.autonomous_approval/v1",
    )
    outcomes = _read_autonomy_records(
        project_root / HARNESS_DIR / "autonomy" / "outcomes.jsonl",
        "record_id",
        "harness.autonomous_outcome/v1",
    )
    issues: list[dict[str, Any]] = []
    dispatches = [(line, event) for line, event in events if event.get("event") == "adapter_dispatched"]
    execution_errors = [(line, event) for line, event in events if event.get("event") == "execution_error"]

    for line_number, event in [*dispatches, *execution_errors]:
        task_id = event.get("task_id")
        lease_id = event.get("lease_id")
        run_id = event.get("run_id")
        artifact_ids = event.get("artifact_ids") if isinstance(event.get("artifact_ids"), list) else []
        if not isinstance(task_id, str) or not task_id:
            issues.append({"line": line_number, "reason": "missing_task_id"})
            continue
        if not isinstance(lease_id, str) or not lease_id:
            issues.append({"line": line_number, "reason": "missing_lease_id", "task_id": task_id})
            continue
        try:
            task = store.get_task(task_id)
        except KeyError as exc:
            issues.append({"line": line_number, "reason": "task_missing", "task_id": task_id, "error": str(exc)})
            continue
        if task.objective_id != objective_id:
            issues.append({"line": line_number, "reason": "task_objective_mismatch", "task_id": task_id})
        lease_metadata: dict[str, Any] | None = None
        try:
            lease = store.get_task_lease(lease_id)
            lease_metadata = lease.metadata
            if lease.task_id != task_id:
                issues.append({"line": line_number, "reason": "lease_task_mismatch", "lease_id": lease_id, "task_id": task_id})
        except KeyError as exc:
            issues.append({"line": line_number, "reason": "lease_missing", "lease_id": lease_id, "error": str(exc)})
        if event.get("ok") is True and not isinstance(run_id, str):
            issues.append({"line": line_number, "reason": "successful_dispatch_missing_run", "task_id": task_id})
        if isinstance(run_id, str) and run_id:
            _append_run_issues(
                store,
                line_number,
                objective_id,
                task_id,
                run_id,
                artifact_ids,
                event,
                lease_metadata,
                issues,
            )
        _append_autonomy_record_issues(line_number, event, objective_id, decisions, approvals, outcomes, issues)
        _append_autonomy_payload_consistency_issues(line_number, event, decisions, approvals, outcomes, issues)

    if issues:
        return _fail("dispatch_links", "One or more dispatch events do not link to persisted task, lease, run, artifact, or autonomy evidence.", {"issues": issues})
    return _pass(
        "dispatch_links",
        "Dispatch and execution-error events link to persisted task, lease, run, artifact, and autonomy evidence.",
        {"dispatch_count": len(dispatches), "execution_error_count": len(execution_errors)},
    )


def _reconciled_run_link_check(store: SQLiteStore, events: list[ObjectiveEvent], objective_id: str) -> ObjectiveEvidenceCheck:
    reconciled = [(line, event) for line, event in events if event.get("event") == "reconciled_existing_run"]
    issues: list[dict[str, Any]] = []
    seen_run_ids: dict[str, int] = {}
    for line_number, event in reconciled:
        run_id = event.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            issues.append({"line": line_number, "reason": "missing_run_id"})
            continue
        previous_line = seen_run_ids.get(run_id)
        if previous_line is not None:
            issues.append({"line": line_number, "reason": "duplicate_reconciled_run", "run_id": run_id, "previous_line": previous_line})
        seen_run_ids[run_id] = line_number
        try:
            run = store.get_run(run_id)
        except KeyError as exc:
            issues.append({"line": line_number, "reason": "run_missing", "run_id": run_id, "error": str(exc)})
            continue
        if run.objective_id != objective_id:
            issues.append({"line": line_number, "reason": "run_objective_mismatch", "run_id": run_id})
        if event.get("run_status") != run.status:
            issues.append(
                {
                    "line": line_number,
                    "reason": "run_status_mismatch",
                    "run_id": run_id,
                    "expected": run.status,
                    "actual": event.get("run_status"),
                }
            )
        task_id = event.get("task_id")
        if task_id != run.task_id:
            issues.append(
                {
                    "line": line_number,
                    "reason": "run_task_mismatch",
                    "run_id": run_id,
                    "expected": run.task_id,
                    "actual": task_id,
                }
            )
        if isinstance(task_id, str) and task_id:
            try:
                task = store.get_task(task_id)
            except KeyError as exc:
                issues.append({"line": line_number, "reason": "task_missing", "task_id": task_id, "error": str(exc)})
            else:
                if task.objective_id != objective_id:
                    issues.append({"line": line_number, "reason": "task_objective_mismatch", "task_id": task_id})
        artifact_ids = event.get("artifact_ids") if isinstance(event.get("artifact_ids"), list) else []
        persisted_artifacts = store.list_artifacts(run_id)
        persisted_artifact_ids = [artifact.id for artifact in persisted_artifacts]
        if artifact_ids != persisted_artifact_ids:
            issues.append(
                {
                    "line": line_number,
                    "reason": "artifact_ids_mismatch",
                    "run_id": run_id,
                    "expected": persisted_artifact_ids,
                    "actual": artifact_ids,
                }
            )
        for artifact_id in artifact_ids:
            if not isinstance(artifact_id, str) or not artifact_id:
                issues.append({"line": line_number, "reason": "invalid_artifact_id", "run_id": run_id, "artifact_id": artifact_id})
                continue
            try:
                artifact = store.get_artifact(artifact_id)
            except KeyError as exc:
                issues.append({"line": line_number, "reason": "artifact_missing", "run_id": run_id, "artifact_id": artifact_id, "error": str(exc)})
                continue
            if artifact.run_id != run_id:
                issues.append({"line": line_number, "reason": "artifact_run_mismatch", "run_id": run_id, "artifact_id": artifact_id})
        run_event_count = event.get("run_event_count")
        persisted_run_event_count = len(store.list_events(run_id))
        if run_event_count != persisted_run_event_count:
            issues.append(
                {
                    "line": line_number,
                    "reason": "run_event_count_mismatch",
                    "run_id": run_id,
                    "expected": persisted_run_event_count,
                    "actual": run_event_count,
                }
            )
    if issues:
        return _fail("reconciled_run_links", "One or more reconciled run records do not link to persisted run evidence.", {"issues": issues})
    return _pass(
        "reconciled_run_links",
        "Reconciled run records link to persisted run and artifact evidence.",
        {"reconciled_run_count": len(reconciled)},
    )


def _append_run_issues(
    store: SQLiteStore,
    line_number: int,
    objective_id: str,
    task_id: str,
    run_id: str,
    artifact_ids: list[Any],
    event: dict[str, Any],
    lease_metadata: dict[str, Any] | None,
    issues: list[dict[str, Any]],
) -> None:
    try:
        run = store.get_run(run_id)
    except KeyError as exc:
        issues.append({"line": line_number, "reason": "run_missing", "run_id": run_id, "error": str(exc)})
        return
    if run.task_id != task_id:
        issues.append({"line": line_number, "reason": "run_task_mismatch", "run_id": run_id, "task_id": task_id})
    if run.objective_id != objective_id:
        issues.append({"line": line_number, "reason": "run_objective_mismatch", "run_id": run_id})
    expected_ok = event.get("ok")
    if isinstance(expected_ok, bool) and not _run_status_matches_dispatch_ok(run.status, expected_ok):
        issues.append(
            {
                "line": line_number,
                "reason": "run_status_ok_mismatch",
                "run_id": run_id,
                "expected_ok": expected_ok,
                "run_status": run.status,
            }
        )
    expected_decision = event.get("decision")
    lease_decision = (lease_metadata or {}).get("decision")
    if isinstance(expected_decision, str) and lease_decision != expected_decision:
        issues.append(
            {
                "line": line_number,
                "reason": "lease_decision_mismatch",
                "run_id": run_id,
                "expected": expected_decision,
                "actual": lease_decision,
            }
        )
    for artifact_id in artifact_ids:
        if not isinstance(artifact_id, str) or not artifact_id:
            issues.append({"line": line_number, "reason": "invalid_artifact_id", "run_id": run_id, "artifact_id": artifact_id})
            continue
        try:
            artifact = store.get_artifact(artifact_id)
        except KeyError as exc:
            issues.append({"line": line_number, "reason": "artifact_missing", "run_id": run_id, "artifact_id": artifact_id, "error": str(exc)})
            continue
        if artifact.run_id != run_id:
            issues.append({"line": line_number, "reason": "artifact_run_mismatch", "run_id": run_id, "artifact_id": artifact_id})


def _run_status_matches_dispatch_ok(run_status: str, ok: bool) -> bool:
    if ok:
        return run_status.startswith("completed")
    return run_status == "failed"


def _append_autonomy_record_issues(
    line_number: int,
    event: dict[str, Any],
    objective_id: str,
    decisions: AutonomyRecordIndex,
    approvals: AutonomyRecordIndex,
    outcomes: AutonomyRecordIndex,
    issues: list[dict[str, Any]],
) -> None:
    expected = {
        "objective_run_id": event.get("objective_run_id"),
        "objective_id": objective_id,
        "task_id": event.get("task_id"),
        "lease_id": event.get("lease_id"),
    }
    for event_key, records, record_id_key in (
        ("autonomy_decision_id", decisions, "record_id"),
        ("autonomous_approval_id", approvals, "id"),
        ("autonomous_outcome_id", outcomes, "record_id"),
    ):
        record_id = event.get(event_key)
        if not isinstance(record_id, str) or not record_id:
            issues.append({"line": line_number, "reason": f"missing_{event_key}"})
            continue
        _append_referenced_autonomy_store_issues(line_number, event_key, record_id, records, issues)
        record = records.records.get(record_id)
        if record is None:
            issues.append({"line": line_number, "reason": f"{event_key}_not_found", event_key: record_id})
            continue
        if record.get(record_id_key) != record_id:
            issues.append({"line": line_number, "reason": f"{event_key}_id_mismatch", event_key: record_id})
        for key, value in expected.items():
            if record.get(key) != value:
                issues.append({"line": line_number, "reason": f"{event_key}_{key}_mismatch", event_key: record_id})


def _append_autonomy_payload_consistency_issues(
    line_number: int,
    event: dict[str, Any],
    decisions: AutonomyRecordIndex,
    approvals: AutonomyRecordIndex,
    outcomes: AutonomyRecordIndex,
    issues: list[dict[str, Any]],
) -> None:
    decision = _referenced_record(event, "autonomy_decision_id", decisions)
    approval = _referenced_record(event, "autonomous_approval_id", approvals)
    outcome = _referenced_record(event, "autonomous_outcome_id", outcomes)
    event_name = event.get("event")
    expected_policy_id = event.get("policy_id")
    expected_adapter_id = event.get("adapter_id")
    if decision is not None:
        _append_field_mismatch_issue(
            line_number,
            "autonomy_decision_id",
            event.get("autonomy_decision_id"),
            "policy_id",
            expected_policy_id,
            decision.get("policy_id"),
            issues,
        )
        _append_field_mismatch_issue(
            line_number,
            "autonomy_decision_id",
            event.get("autonomy_decision_id"),
            "adapter_id",
            expected_adapter_id,
            decision.get("adapter_id"),
            issues,
        )
        _append_field_mismatch_issue(
            line_number,
            "autonomy_decision_id",
            event.get("autonomy_decision_id"),
            "tool_name",
            DISPATCH_AUTONOMY_TOOL_NAME,
            decision.get("tool_name"),
            issues,
        )
    if approval is not None:
        _append_field_mismatch_issue(
            line_number,
            "autonomous_approval_id",
            event.get("autonomous_approval_id"),
            "policy_id",
            expected_policy_id,
            approval.get("policy_id"),
            issues,
        )
        _append_field_mismatch_issue(
            line_number,
            "autonomous_approval_id",
            event.get("autonomous_approval_id"),
            "adapter_id",
            expected_adapter_id,
            approval.get("adapter_id"),
            issues,
        )
        _append_field_mismatch_issue(
            line_number,
            "autonomous_approval_id",
            event.get("autonomous_approval_id"),
            "tool_name",
            DISPATCH_AUTONOMY_TOOL_NAME,
            approval.get("tool_name"),
            issues,
        )
        if decision is not None:
            _append_field_mismatch_issue(
                line_number,
                "autonomous_approval_id",
                event.get("autonomous_approval_id"),
                "decision_status",
                decision.get("status"),
                approval.get("decision_status"),
                issues,
            )
            for field in ("task_type", "boundary", "risk", "reasons"):
                _append_field_mismatch_issue(
                    line_number,
                    "autonomous_approval_id",
                    event.get("autonomous_approval_id"),
                    field,
                    decision.get(field),
                    approval.get(field),
                    issues,
                )
    if outcome is None:
        return
    _append_field_mismatch_issue(
        line_number,
        "autonomous_outcome_id",
        event.get("autonomous_outcome_id"),
        "policy_id",
        expected_policy_id,
        outcome.get("policy_id"),
        issues,
    )
    _append_field_mismatch_issue(
        line_number,
        "autonomous_outcome_id",
        event.get("autonomous_outcome_id"),
        "adapter_id",
        expected_adapter_id,
        outcome.get("adapter_id"),
        issues,
    )
    _append_field_mismatch_issue(
        line_number,
        "autonomous_outcome_id",
        event.get("autonomous_outcome_id"),
        "tool_name",
        DISPATCH_AUTONOMY_TOOL_NAME,
        outcome.get("tool_name"),
        issues,
    )
    if decision is not None:
        _append_field_mismatch_issue(
            line_number,
            "autonomous_outcome_id",
            event.get("autonomous_outcome_id"),
            "decision_status",
            decision.get("status"),
            outcome.get("decision_status"),
            issues,
        )
        _append_field_mismatch_issue(
            line_number,
            "autonomous_outcome_id",
            event.get("autonomous_outcome_id"),
            "task_type",
            decision.get("task_type"),
            outcome.get("task_type"),
            issues,
        )
    expected_run_id = event.get("run_id") if event_name == "adapter_dispatched" else None
    _append_field_mismatch_issue(
        line_number,
        "autonomous_outcome_id",
        event.get("autonomous_outcome_id"),
        "run_id",
        expected_run_id,
        outcome.get("run_id"),
        issues,
    )
    expected_artifact_ids = event.get("artifact_ids") if event_name == "adapter_dispatched" else []
    if not isinstance(expected_artifact_ids, list):
        expected_artifact_ids = []
    if outcome.get("artifact_ids") != expected_artifact_ids:
        issues.append(
            {
                "line": line_number,
                "reason": "autonomous_outcome_id_artifact_ids_mismatch",
                "autonomous_outcome_id": event.get("autonomous_outcome_id"),
                "expected": expected_artifact_ids,
                "actual": outcome.get("artifact_ids"),
            }
        )
    expected_ok = event.get("ok") if event_name == "adapter_dispatched" else False
    if outcome.get("ok") != expected_ok:
        issues.append(
            {
                "line": line_number,
                "reason": "autonomous_outcome_id_ok_mismatch",
                "autonomous_outcome_id": event.get("autonomous_outcome_id"),
                "expected": expected_ok,
                "actual": outcome.get("ok"),
            }
        )
    if event_name == "execution_error":
        if outcome.get("error") != event.get("error"):
            issues.append(
                {
                    "line": line_number,
                    "reason": "autonomous_outcome_id_error_mismatch",
                    "autonomous_outcome_id": event.get("autonomous_outcome_id"),
                    "expected": event.get("error"),
                    "actual": outcome.get("error"),
                }
            )
    elif "error" in outcome:
        issues.append(
            {
                "line": line_number,
                "reason": "autonomous_outcome_id_unexpected_error",
                "autonomous_outcome_id": event.get("autonomous_outcome_id"),
            }
        )


def _referenced_record(
    event: dict[str, Any],
    event_key: str,
    index: AutonomyRecordIndex,
) -> dict[str, Any] | None:
    record_id = event.get(event_key)
    return index.records.get(record_id) if isinstance(record_id, str) else None


def _append_field_mismatch_issue(
    line_number: int,
    event_key: str,
    record_id: Any,
    field: str,
    expected: Any,
    actual: Any,
    issues: list[dict[str, Any]],
) -> None:
    if actual != expected:
        issues.append(
            {
                "line": line_number,
                "reason": f"{event_key}_{field}_mismatch",
                event_key: record_id,
                "expected": expected,
                "actual": actual,
            }
        )


def _append_referenced_autonomy_store_issues(
    line_number: int,
    event_key: str,
    record_id: str,
    index: AutonomyRecordIndex,
    issues: list[dict[str, Any]],
) -> None:
    if index.parse_errors:
        issues.append(
            {
                "line": line_number,
                "reason": f"{event_key}_store_malformed",
                event_key: record_id,
                "store_path": str(index.path),
                "errors": index.parse_errors,
            }
        )
    duplicate_lines = index.duplicate_ids.get(record_id)
    if duplicate_lines:
        issues.append(
            {
                "line": line_number,
                "reason": f"{event_key}_duplicate_record_id",
                event_key: record_id,
                "store_path": str(index.path),
                "store_lines": duplicate_lines,
            }
        )
    record = index.records.get(record_id)
    if record is not None and record.get("schema_version") != index.schema_version:
        issues.append(
            {
                "line": line_number,
                "reason": f"{event_key}_schema_version_mismatch",
                event_key: record_id,
                "expected": index.schema_version,
                "actual": record.get("schema_version"),
                "store_line": index.record_lines.get(record_id),
            }
        )


def _batch_plan_check(
    project_root: Path,
    store: SQLiteStore,
    events: list[ObjectiveEvent],
    objective_id: str,
) -> ObjectiveEvidenceCheck:
    decisions = _read_autonomy_records(
        project_root / HARNESS_DIR / "autonomy" / "decisions.jsonl",
        "record_id",
        "harness.autonomy_decision/v1",
    )
    issues: list[dict[str, Any]] = []
    plans = [(line, event) for line, event in events if event.get("event") == "batch_planned"]
    for line_number, event in plans:
        if event.get("plan_schema_version") != OBJECTIVE_BATCH_PLAN_SCHEMA_VERSION:
            issues.append({"line": line_number, "reason": "plan_schema_version", "value": event.get("plan_schema_version")})
        selected = event.get("selected")
        if not isinstance(selected, list):
            issues.append({"line": line_number, "reason": "selected_not_list"})
            selected = []
        selected_task_ids = event.get("selected_task_ids") if isinstance(event.get("selected_task_ids"), list) else []
        selected_lease_ids = event.get("selected_lease_ids") if isinstance(event.get("selected_lease_ids"), list) else []
        if selected_task_ids != [item.get("task_id") for item in selected if isinstance(item, dict)]:
            issues.append({"line": line_number, "reason": "selected_task_ids_mismatch"})
        if selected_lease_ids != [item.get("lease_id") for item in selected if isinstance(item, dict)]:
            issues.append({"line": line_number, "reason": "selected_lease_ids_mismatch"})
        _append_batch_schedule_policy_issues(store, line_number, event, objective_id, issues)
        for item in selected:
            if isinstance(item, dict):
                _append_selected_plan_issues(store, decisions, line_number, item, objective_id, event.get("objective_run_id"), issues)
    if issues:
        return _fail(
            "batch_plan_links",
            "One or more batch plan records do not link to selected task, lease, autonomy decision, or scheduler-policy evidence.",
            {"issues": issues},
        )
    return _pass(
        "batch_plan_links",
        "Batch plan records link to selected task, lease, autonomy decision, and scheduler-policy evidence.",
        {"batch_plan_count": len(plans)},
    )


def _batch_lifecycle_check(events: list[ObjectiveEvent]) -> ObjectiveEvidenceCheck:
    grouped: dict[str, list[ObjectiveEvent]] = {}
    for item in events:
        objective_run_id = item[1].get("objective_run_id")
        if isinstance(objective_run_id, str) and objective_run_id:
            grouped.setdefault(objective_run_id, []).append(item)

    issues: list[dict[str, Any]] = []
    planned_batches = 0
    started_batches = 0
    completed_batches = 0
    for objective_run_id, run_events in sorted(grouped.items()):
        plans: dict[int, list[ObjectiveEvent]] = {}
        starts: dict[int, list[ObjectiveEvent]] = {}
        completes: dict[int, list[ObjectiveEvent]] = {}
        dispatches: dict[int, list[ObjectiveEvent]] = {}
        execution_errors: dict[int, list[ObjectiveEvent]] = {}
        has_batch_plan = any(event.get("event") == "batch_planned" for _, event in run_events)

        for line_number, event in run_events:
            event_name = event.get("event")
            if event_name not in {"batch_planned", "batch_started", "batch_completed", "adapter_dispatched", "execution_error"}:
                continue
            batch = event.get("batch")
            if event_name in {"adapter_dispatched", "execution_error"} and batch is None:
                if has_batch_plan:
                    issues.append(
                        {
                            "line": line_number,
                            "objective_run_id": objective_run_id,
                            "reason": f"{event_name}_missing_batch",
                        }
                    )
                continue
            if not _is_positive_int(batch):
                issues.append(
                    {
                        "line": line_number,
                        "objective_run_id": objective_run_id,
                        "reason": "invalid_batch",
                        "event": event_name,
                        "batch": batch,
                    }
                )
                continue
            batch_index = int(batch)
            if event_name == "batch_planned":
                plans.setdefault(batch_index, []).append((line_number, event))
            elif event_name == "batch_started":
                starts.setdefault(batch_index, []).append((line_number, event))
            elif event_name == "batch_completed":
                completes.setdefault(batch_index, []).append((line_number, event))
            elif event_name == "adapter_dispatched":
                dispatches.setdefault(batch_index, []).append((line_number, event))
            elif event_name == "execution_error":
                execution_errors.setdefault(batch_index, []).append((line_number, event))

        planned_batches += sum(len(items) for items in plans.values())
        started_batches += sum(len(items) for items in starts.values())
        completed_batches += sum(len(items) for items in completes.values())
        for batch in sorted(set(plans) | set(starts) | set(completes) | set(dispatches) | set(execution_errors)):
            plan = _single_batch_event(plans.get(batch, []), objective_run_id, batch, "batch_plan", issues)
            start = _single_batch_event(starts.get(batch, []), objective_run_id, batch, "batch_started", issues)
            complete = _single_batch_event(completes.get(batch, []), objective_run_id, batch, "batch_completed", issues)
            if plan is None and (start is not None or complete is not None or dispatches.get(batch) or execution_errors.get(batch)):
                issues.append({"objective_run_id": objective_run_id, "batch": batch, "reason": "batch_missing_plan"})

            selected_pairs: list[tuple[str, str]] = []
            selected_task_ids: list[str] = []
            selected_lease_ids: list[str] = []
            if plan is not None:
                plan_line, plan_event = plan
                selected_task_ids = _string_list_field(plan_line, objective_run_id, batch, plan_event, "selected_task_ids", issues)
                selected_lease_ids = _string_list_field(plan_line, objective_run_id, batch, plan_event, "selected_lease_ids", issues)
                if len(selected_task_ids) != len(selected_lease_ids):
                    issues.append(
                        {
                            "line": plan_line,
                            "objective_run_id": objective_run_id,
                            "batch": batch,
                            "reason": "selected_task_lease_count_mismatch",
                            "task_count": len(selected_task_ids),
                            "lease_count": len(selected_lease_ids),
                        }
                    )
                selected_pairs = list(zip(selected_task_ids, selected_lease_ids))
                if selected_pairs and start is None:
                    issues.append({"line": plan_line, "objective_run_id": objective_run_id, "batch": batch, "reason": "batch_selected_without_start"})
                if selected_pairs and complete is None:
                    issues.append({"line": plan_line, "objective_run_id": objective_run_id, "batch": batch, "reason": "batch_selected_without_completion"})
                if not selected_pairs and start is not None:
                    issues.append({"line": start[0], "objective_run_id": objective_run_id, "batch": batch, "reason": "empty_batch_started"})
                if not selected_pairs and complete is not None:
                    issues.append({"line": complete[0], "objective_run_id": objective_run_id, "batch": batch, "reason": "empty_batch_completed"})

            started_task_ids: list[str] = []
            started_lease_ids: list[str] = []
            if start is not None:
                start_line, start_event = start
                started_task_ids = _string_list_field(start_line, objective_run_id, batch, start_event, "task_ids", issues)
                started_lease_ids = _string_list_field(start_line, objective_run_id, batch, start_event, "lease_ids", issues)
                if plan is not None and start_line <= plan[0]:
                    issues.append({"line": start_line, "objective_run_id": objective_run_id, "batch": batch, "reason": "batch_started_before_plan"})
                if plan is not None and selected_task_ids != started_task_ids:
                    issues.append(
                        {
                            "line": start_line,
                            "objective_run_id": objective_run_id,
                            "batch": batch,
                            "reason": "batch_started_task_ids_mismatch",
                            "expected": selected_task_ids,
                            "actual": started_task_ids,
                        }
                    )
                if plan is not None and selected_lease_ids != started_lease_ids:
                    issues.append(
                        {
                            "line": start_line,
                            "objective_run_id": objective_run_id,
                            "batch": batch,
                            "reason": "batch_started_lease_ids_mismatch",
                            "expected": selected_lease_ids,
                            "actual": started_lease_ids,
                        }
                    )

            if complete is not None:
                complete_line, complete_event = complete
                completed_task_ids = _string_list_field(complete_line, objective_run_id, batch, complete_event, "task_ids", issues)
                if start is None:
                    issues.append({"line": complete_line, "objective_run_id": objective_run_id, "batch": batch, "reason": "batch_completed_without_start"})
                elif complete_line <= start[0]:
                    issues.append({"line": complete_line, "objective_run_id": objective_run_id, "batch": batch, "reason": "batch_completed_before_start"})
                if start is not None and completed_task_ids != started_task_ids:
                    issues.append(
                        {
                            "line": complete_line,
                            "objective_run_id": objective_run_id,
                            "batch": batch,
                            "reason": "batch_completed_task_ids_mismatch",
                            "expected": started_task_ids,
                            "actual": completed_task_ids,
                        }
                    )
                actual_dispatch_count = complete_event.get("adapter_dispatches")
                expected_cumulative_dispatch_count = sum(
                    1
                    for dispatch_line, dispatch_event in run_events
                    if dispatch_line <= complete_line
                    and dispatch_event.get("event") == "adapter_dispatched"
                    and _is_positive_int(dispatch_event.get("batch"))
                )
                expected_batch_dispatch_count = sum(
                    1
                    for dispatch_line, _ in dispatches.get(batch, [])
                    if dispatch_line < complete_line
                )
                if not isinstance(actual_dispatch_count, int) or isinstance(actual_dispatch_count, bool):
                    issues.append({"line": complete_line, "objective_run_id": objective_run_id, "batch": batch, "reason": "batch_completed_adapter_dispatches_missing"})
                elif actual_dispatch_count != expected_cumulative_dispatch_count:
                    issues.append(
                        {
                            "line": complete_line,
                            "objective_run_id": objective_run_id,
                            "batch": batch,
                            "reason": "batch_completed_adapter_dispatches_mismatch",
                            "expected": expected_cumulative_dispatch_count,
                            "actual": actual_dispatch_count,
                        }
                    )
                actual_cumulative_dispatch_count = complete_event.get("cumulative_adapter_dispatches")
                if not isinstance(actual_cumulative_dispatch_count, int) or isinstance(actual_cumulative_dispatch_count, bool):
                    issues.append({"line": complete_line, "objective_run_id": objective_run_id, "batch": batch, "reason": "batch_completed_cumulative_adapter_dispatches_missing"})
                elif actual_cumulative_dispatch_count != expected_cumulative_dispatch_count:
                    issues.append(
                        {
                            "line": complete_line,
                            "objective_run_id": objective_run_id,
                            "batch": batch,
                            "reason": "batch_completed_cumulative_adapter_dispatches_mismatch",
                            "expected": expected_cumulative_dispatch_count,
                            "actual": actual_cumulative_dispatch_count,
                        }
                    )
                if (
                    isinstance(actual_dispatch_count, int)
                    and not isinstance(actual_dispatch_count, bool)
                    and isinstance(actual_cumulative_dispatch_count, int)
                    and not isinstance(actual_cumulative_dispatch_count, bool)
                    and actual_dispatch_count != actual_cumulative_dispatch_count
                ):
                    issues.append(
                        {
                            "line": complete_line,
                            "objective_run_id": objective_run_id,
                            "batch": batch,
                            "reason": "batch_completed_legacy_cumulative_mismatch",
                            "adapter_dispatches": actual_dispatch_count,
                            "cumulative_adapter_dispatches": actual_cumulative_dispatch_count,
                        }
                    )
                actual_batch_dispatch_count = complete_event.get("batch_dispatches")
                if not isinstance(actual_batch_dispatch_count, int) or isinstance(actual_batch_dispatch_count, bool):
                    issues.append({"line": complete_line, "objective_run_id": objective_run_id, "batch": batch, "reason": "batch_completed_batch_dispatches_missing"})
                elif actual_batch_dispatch_count != expected_batch_dispatch_count:
                    issues.append(
                        {
                            "line": complete_line,
                            "objective_run_id": objective_run_id,
                            "batch": batch,
                            "reason": "batch_completed_batch_dispatches_mismatch",
                            "expected": expected_batch_dispatch_count,
                            "actual": actual_batch_dispatch_count,
                        }
                    )
                actual_execution_errors = complete_event.get("execution_errors")
                expected_execution_errors = sum(
                    1
                    for error_line, _ in execution_errors.get(batch, [])
                    if error_line < complete_line
                )
                if not isinstance(actual_execution_errors, int) or isinstance(actual_execution_errors, bool):
                    issues.append({"line": complete_line, "objective_run_id": objective_run_id, "batch": batch, "reason": "batch_completed_execution_errors_missing"})
                elif actual_execution_errors != expected_execution_errors:
                    issues.append(
                        {
                            "line": complete_line,
                            "objective_run_id": objective_run_id,
                            "batch": batch,
                            "reason": "batch_completed_execution_errors_mismatch",
                            "expected": expected_execution_errors,
                            "actual": actual_execution_errors,
                        }
                    )

            _append_batch_item_issues(
                objective_run_id,
                batch,
                "adapter_dispatched",
                dispatches.get(batch, []),
                selected_pairs,
                start,
                complete,
                issues,
            )
            _append_batch_item_issues(
                objective_run_id,
                batch,
                "execution_error",
                execution_errors.get(batch, []),
                selected_pairs,
                start,
                complete,
                issues,
            )
            _append_selected_pair_terminal_issues(
                objective_run_id,
                batch,
                selected_pairs,
                [*dispatches.get(batch, []), *execution_errors.get(batch, [])],
                issues,
            )

    if issues:
        return _fail("batch_lifecycle", "One or more batch lifecycle records are inconsistent with the planned selections.", {"issues": issues})
    return _pass(
        "batch_lifecycle",
        "Batch lifecycle records consistently connect plans, starts, dispatches, and completions.",
        {
            "objective_runs": len(grouped),
            "planned_batches": planned_batches,
            "started_batches": started_batches,
            "completed_batches": completed_batches,
        },
    )


def _single_batch_event(
    items: list[ObjectiveEvent],
    objective_run_id: str,
    batch: int,
    label: str,
    issues: list[dict[str, Any]],
) -> ObjectiveEvent | None:
    if len(items) > 1:
        issues.append({"objective_run_id": objective_run_id, "batch": batch, "reason": f"{label}_count", "count": len(items)})
    if not items:
        return None
    return items[0]


def _string_list_field(
    line_number: int,
    objective_run_id: str,
    batch: int,
    event: dict[str, Any],
    key: str,
    issues: list[dict[str, Any]],
) -> list[str]:
    value = event.get(key)
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        issues.append({"line": line_number, "objective_run_id": objective_run_id, "batch": batch, "reason": f"{key}_not_string_list"})
        return []
    return list(value)


def _append_batch_item_issues(
    objective_run_id: str,
    batch: int,
    event_name: str,
    items: list[ObjectiveEvent],
    selected_pairs: list[tuple[str, str]],
    start: ObjectiveEvent | None,
    complete: ObjectiveEvent | None,
    issues: list[dict[str, Any]],
) -> None:
    seen_pairs: set[tuple[str, str]] = set()
    for line_number, event in items:
        task_id = event.get("task_id")
        lease_id = event.get("lease_id")
        if not isinstance(task_id, str) or not isinstance(lease_id, str):
            issues.append({"line": line_number, "objective_run_id": objective_run_id, "batch": batch, "reason": f"{event_name}_missing_task_or_lease"})
            continue
        pair = (task_id, lease_id)
        if start is not None and line_number <= start[0]:
            issues.append({"line": line_number, "objective_run_id": objective_run_id, "batch": batch, "reason": f"{event_name}_before_batch_started"})
        if complete is not None and line_number >= complete[0]:
            issues.append({"line": line_number, "objective_run_id": objective_run_id, "batch": batch, "reason": f"{event_name}_after_batch_completed"})
        if selected_pairs and pair not in selected_pairs:
            issues.append(
                {
                    "line": line_number,
                    "objective_run_id": objective_run_id,
                    "batch": batch,
                    "reason": f"{event_name}_not_selected",
                    "task_id": task_id,
                    "lease_id": lease_id,
                }
            )
        if pair in seen_pairs:
            issues.append(
                {
                    "line": line_number,
                    "objective_run_id": objective_run_id,
                    "batch": batch,
                    "reason": f"{event_name}_duplicate_task_lease",
                    "task_id": task_id,
                    "lease_id": lease_id,
                }
            )
        seen_pairs.add(pair)


def _append_selected_pair_terminal_issues(
    objective_run_id: str,
    batch: int,
    selected_pairs: list[tuple[str, str]],
    terminal_items: list[ObjectiveEvent],
    issues: list[dict[str, Any]],
) -> None:
    if not selected_pairs:
        return
    terminal_by_pair: dict[tuple[str, str], list[ObjectiveEvent]] = {}
    for line_number, event in terminal_items:
        task_id = event.get("task_id")
        lease_id = event.get("lease_id")
        if isinstance(task_id, str) and isinstance(lease_id, str):
            terminal_by_pair.setdefault((task_id, lease_id), []).append((line_number, event))
    for task_id, lease_id in selected_pairs:
        terminals = terminal_by_pair.get((task_id, lease_id), [])
        if not terminals:
            issues.append(
                {
                    "objective_run_id": objective_run_id,
                    "batch": batch,
                    "reason": "selected_pair_missing_terminal_event",
                    "task_id": task_id,
                    "lease_id": lease_id,
                }
            )
        elif len(terminals) > 1:
            issues.append(
                {
                    "line": terminals[-1][0],
                    "objective_run_id": objective_run_id,
                    "batch": batch,
                    "reason": "selected_pair_duplicate_terminal_event",
                    "task_id": task_id,
                    "lease_id": lease_id,
                    "events": [event.get("event") for _, event in terminals],
                }
            )


def _append_batch_schedule_policy_issues(
    store: SQLiteStore,
    line_number: int,
    event: dict[str, Any],
    objective_id: str,
    issues: list[dict[str, Any]],
) -> None:
    if event.get("scheduler_policy") != SCHEDULER_POLICY_ID:
        issues.append(
            {
                "line": line_number,
                "reason": "scheduler_policy_unsupported",
                "expected": SCHEDULER_POLICY_ID,
                "actual": event.get("scheduler_policy"),
            }
        )
        return
    _append_policy_evidence_issues(line_number, event, issues)

    tasks = store.list_tasks(objective_id=objective_id)
    task_by_id = {task.id: task for task in tasks}
    recomputed_profiles = _evidence_schedule_profiles(tasks)
    schedule_profiles = event.get("schedule_profiles")
    if not isinstance(schedule_profiles, dict):
        return

    candidate_task_ids = _event_string_list(event.get("candidate_task_ids"))
    blocked_task_ids = _event_string_list(event.get("blocked_task_ids"))
    selected_task_ids = _event_string_list(event.get("selected_task_ids"))
    referenced_task_ids = [*candidate_task_ids, *blocked_task_ids, *selected_task_ids]
    referenced_task_ids.extend(_dependency_snapshot_task_ids(event.get("dependency_snapshots")))
    referenced_task_ids.extend(str(task_id) for task_id in schedule_profiles if isinstance(task_id, str))

    for task_id in sorted(set(referenced_task_ids)):
        if task_id not in task_by_id:
            issues.append({"line": line_number, "reason": "schedule_task_missing", "task_id": task_id})
            continue
        profile = schedule_profiles.get(task_id)
        if not isinstance(profile, dict):
            issues.append({"line": line_number, "reason": "schedule_profile_missing", "task_id": task_id})
            continue
        expected_profile = recomputed_profiles[task_id]
        for field, expected in expected_profile.items():
            if profile.get(field) != expected:
                issues.append(
                    {
                        "line": line_number,
                        "reason": "schedule_profile_mismatch",
                        "task_id": task_id,
                        "field": field,
                        "expected": expected,
                        "actual": profile.get(field),
                    }
                )

    if len(set(candidate_task_ids)) != len(candidate_task_ids):
        issues.append({"line": line_number, "reason": "candidate_task_ids_duplicate"})
    if len(set(selected_task_ids)) != len(selected_task_ids):
        issues.append({"line": line_number, "reason": "selected_task_ids_duplicate"})

    known_candidate_ids = [task_id for task_id in candidate_task_ids if task_id in task_by_id]
    expected_candidate_ids = sorted(
        known_candidate_ids,
        key=lambda task_id: _evidence_schedule_sort_key(task_id, task_by_id, recomputed_profiles),
    )
    if known_candidate_ids and candidate_task_ids == known_candidate_ids and candidate_task_ids != expected_candidate_ids:
        issues.append(
            {
                "line": line_number,
                "reason": "candidate_task_ids_policy_order_mismatch",
                "expected": expected_candidate_ids,
                "actual": candidate_task_ids,
            }
        )

    selected_sources = _selected_schedule_sources(event.get("selected"))
    if len(selected_sources) != len(selected_task_ids):
        issues.append(
            {
                "line": line_number,
                "reason": "selected_source_count_mismatch",
                "selected_task_count": len(selected_task_ids),
                "selected_source_count": len(selected_sources),
            }
        )
        return

    candidate_set = set(candidate_task_ids)
    for task_id in selected_task_ids:
        if task_id not in candidate_set:
            issues.append({"line": line_number, "reason": "selected_task_not_candidate", "task_id": task_id})

    seen_fresh_selection = False
    for task_id, _lease_id, source in selected_sources:
        if source == "new_guarded_lease":
            seen_fresh_selection = True
        elif source == "resumed_active_lease" and seen_fresh_selection:
            issues.append({"line": line_number, "reason": "resumed_selection_after_fresh_selection", "task_id": task_id})

    resumed_sources = [(task_id, lease_id) for task_id, lease_id, source in selected_sources if source == "resumed_active_lease"]
    fresh_task_ids = [task_id for task_id, _lease_id, source in selected_sources if source == "new_guarded_lease"]
    resumed_task_ids = [task_id for task_id, _lease_id in resumed_sources]
    candidate_without_resumed = [task_id for task_id in candidate_task_ids if task_id not in set(resumed_task_ids)]
    expected_fresh_task_ids = candidate_without_resumed[: len(fresh_task_ids)]
    if fresh_task_ids != expected_fresh_task_ids:
        issues.append(
            {
                "line": line_number,
                "reason": "selected_task_ids_not_policy_prefix",
                "expected": expected_fresh_task_ids,
                "actual": fresh_task_ids,
                "resumed_task_ids": resumed_task_ids,
            }
        )

    if resumed_sources:
        lease_by_id = {}
        missing_lease_ids: list[str] = []
        for _task_id, lease_id in resumed_sources:
            try:
                lease_by_id[lease_id] = store.get_task_lease(lease_id)
            except KeyError:
                missing_lease_ids.append(lease_id)
        if missing_lease_ids:
            issues.append({"line": line_number, "reason": "resumed_selection_lease_missing", "lease_ids": missing_lease_ids})
            return
        resumed_lease_ids = [lease_id for _task_id, lease_id in resumed_sources]
        expected_resumed_lease_ids = sorted(
            resumed_lease_ids,
            key=lambda lease_id: (lease_by_id[lease_id].acquired_at, lease_id),
        )
        if resumed_lease_ids != expected_resumed_lease_ids:
            issues.append(
                {
                    "line": line_number,
                    "reason": "resumed_lease_order_mismatch",
                    "expected": expected_resumed_lease_ids,
                    "actual": resumed_lease_ids,
                }
            )


def _append_policy_evidence_issues(
    line_number: int,
    event: dict[str, Any],
    issues: list[dict[str, Any]],
) -> None:
    policy_evidence = event.get("policy_evidence")
    if policy_evidence is None:
        return
    if not isinstance(policy_evidence, dict):
        issues.append({"line": line_number, "reason": "policy_evidence_not_object"})
        return
    expected = {
        "policy_id": SCHEDULER_POLICY_ID,
        "sort_keys": SCHEDULER_POLICY_SORT_KEYS,
        "candidate_order_basis": "candidate_task_ids_sorted_by_policy",
        "resumed_lease_order_basis": "lease_acquired_at_then_lease_id",
        "fresh_selection_basis": "policy_prefix_after_resumed_leases",
    }
    for field, expected_value in expected.items():
        if policy_evidence.get(field) != expected_value:
            issues.append(
                {
                    "line": line_number,
                    "reason": "policy_evidence_mismatch",
                    "field": field,
                    "expected": expected_value,
                    "actual": policy_evidence.get(field),
                }
            )


def _event_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _dependency_snapshot_task_ids(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    task_ids: list[str] = []
    for item in value:
        if isinstance(item, dict) and isinstance(item.get("task_id"), str) and item["task_id"]:
            task_ids.append(item["task_id"])
    return task_ids


def _selected_schedule_sources(value: Any) -> list[tuple[str, str, str]]:
    if not isinstance(value, list):
        return []
    sources: list[tuple[str, str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        task_id = item.get("task_id")
        lease_id = item.get("lease_id")
        if not isinstance(task_id, str) or not isinstance(lease_id, str):
            continue
        source = item.get("selection_source", "new_guarded_lease")
        if source not in {"resumed_active_lease", "new_guarded_lease"}:
            source = "new_guarded_lease"
        sources.append((task_id, lease_id, source))
    return sources


def _evidence_schedule_profiles(tasks: list[Any]) -> dict[str, dict[str, int | str]]:
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
        task.id: {
            "task_id": task.id,
            "priority": task.priority,
            "critical_path_depth": critical_path_depth(task.id),
            "downstream_task_count": downstream_count(task.id),
        }
        for task in tasks
    }


def _evidence_schedule_sort_key(
    task_id: str,
    task_by_id: dict[str, Any],
    profiles: dict[str, dict[str, int | str]],
) -> tuple[int, int, int, Any, str]:
    profile = profiles[task_id]
    task = task_by_id[task_id]
    return (
        -int(profile["priority"]),
        -int(profile["critical_path_depth"]),
        -int(profile["downstream_task_count"]),
        task.created_at,
        task_id,
    )


def _is_positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _stopped_summary_check(
    project_root: Path,
    store: SQLiteStore,
    events: list[ObjectiveEvent],
    objective_id: str,
) -> ObjectiveEvidenceCheck:
    decisions = _read_autonomy_records(
        project_root / HARNESS_DIR / "autonomy" / "decisions.jsonl",
        "record_id",
        "harness.autonomy_decision/v1",
    )
    issues: list[dict[str, Any]] = []
    grouped: dict[str, list[ObjectiveEvent]] = {}
    for item in events:
        objective_run_id = item[1].get("objective_run_id")
        if isinstance(objective_run_id, str) and objective_run_id:
            grouped.setdefault(objective_run_id, []).append(item)

    latest_stopped: ObjectiveEvent | None = None
    for objective_run_id, run_events in sorted(grouped.items()):
        stopped_events = [(line, event) for line, event in run_events if event.get("event") == "stopped"]
        if len(stopped_events) != 1:
            issues.append(
                {
                    "objective_run_id": objective_run_id,
                    "reason": "stopped_event_count",
                    "count": len(stopped_events),
                }
            )
            continue
        stopped_line, stopped = stopped_events[0]
        if latest_stopped is None or stopped_line > latest_stopped[0]:
            latest_stopped = (stopped_line, stopped)
        _append_stopped_summary_issues(objective_run_id, run_events, stopped_line, stopped, decisions, issues)

    if latest_stopped is not None:
        _append_latest_status_issues(store, objective_id, latest_stopped[0], latest_stopped[1], issues)

    if issues:
        return _fail("stopped_summary", "One or more stopped summaries do not match objective event evidence.", {"issues": issues})
    return _pass("stopped_summary", "Stopped summaries match objective event evidence.", {"objective_runs": len(grouped)})


def _append_stopped_summary_issues(
    objective_run_id: str,
    run_events: list[ObjectiveEvent],
    stopped_line: int,
    stopped: dict[str, Any],
    decisions: AutonomyRecordIndex,
    issues: list[dict[str, Any]],
) -> None:
    dispatches = [event for _, event in run_events if event.get("event") == "adapter_dispatched"]
    batch_completed = [event for _, event in run_events if event.get("event") == "batch_completed"]
    step_results = stopped.get("step_results")
    if not isinstance(step_results, list):
        issues.append({"line": stopped_line, "objective_run_id": objective_run_id, "reason": "step_results_not_list"})
        step_results = []
    expected_counts = {
        "adapter_dispatches": len(dispatches),
        "batches": len(batch_completed),
        "steps": len(step_results),
    }
    for key, expected in expected_counts.items():
        value = stopped.get(key)
        if isinstance(value, int) and value != expected:
            issues.append(
                {
                    "line": stopped_line,
                    "objective_run_id": objective_run_id,
                    "reason": f"{key}_mismatch",
                    "expected": expected,
                    "actual": value,
                }
            )
        elif not isinstance(value, int):
            issues.append({"line": stopped_line, "objective_run_id": objective_run_id, "reason": f"{key}_missing"})

    stop_reason = stopped.get("stop_reason")
    ok = stopped.get("ok")
    if not isinstance(stop_reason, str) or not stop_reason:
        issues.append({"line": stopped_line, "objective_run_id": objective_run_id, "reason": "stop_reason_missing"})
    elif isinstance(ok, bool) and ok != (stop_reason == "objective_succeeded"):
        issues.append(
            {
                "line": stopped_line,
                "objective_run_id": objective_run_id,
                "reason": "ok_stop_reason_mismatch",
                "stop_reason": stop_reason,
                "ok": ok,
            }
        )
    elif not isinstance(ok, bool):
        issues.append({"line": stopped_line, "objective_run_id": objective_run_id, "reason": "ok_missing"})

    dispatch_keys = {
        (
            event.get("task_id"),
            event.get("lease_id"),
            event.get("run_id"),
        )
        for event in dispatches
        if event.get("run_id")
    }
    for index, step in enumerate(step_results):
        if not isinstance(step, dict):
            issues.append({"line": stopped_line, "objective_run_id": objective_run_id, "reason": "step_not_object", "index": index})
            continue
        run_id = step.get("run_id")
        if run_id is None:
            continue
        key = (step.get("task_id"), step.get("lease_id"), run_id)
        if key not in dispatch_keys:
            issues.append(
                {
                    "line": stopped_line,
                    "objective_run_id": objective_run_id,
                    "reason": "step_dispatch_missing",
                    "index": index,
                    "task_id": step.get("task_id"),
                    "lease_id": step.get("lease_id"),
                    "run_id": run_id,
                }
            )
    _append_step_event_summary_issues(objective_run_id, run_events, stopped_line, step_results, decisions, issues)
    _append_reconciled_stopped_summary_issues(objective_run_id, run_events, stopped_line, stopped, issues)


def _append_reconciled_stopped_summary_issues(
    objective_run_id: str,
    run_events: list[ObjectiveEvent],
    stopped_line: int,
    stopped: dict[str, Any],
    issues: list[dict[str, Any]],
) -> None:
    reconciled_run_ids = [
        event.get("run_id")
        for _, event in run_events
        if event.get("event") == "reconciled_existing_run" and isinstance(event.get("run_id"), str) and event.get("run_id")
    ]
    if not reconciled_run_ids and stopped.get("stop_reason") != "reconciled_existing_evidence":
        return
    stopped_run_ids = stopped.get("reconciled_run_ids")
    if stopped_run_ids != reconciled_run_ids:
        issues.append(
            {
                "line": stopped_line,
                "objective_run_id": objective_run_id,
                "reason": "reconciled_run_ids_mismatch",
                "expected": reconciled_run_ids,
                "actual": stopped_run_ids,
            }
        )
    stopped_run_count = stopped.get("reconciled_run_count")
    if stopped_run_count != len(reconciled_run_ids):
        issues.append(
            {
                "line": stopped_line,
                "objective_run_id": objective_run_id,
                "reason": "reconciled_run_count_mismatch",
                "expected": len(reconciled_run_ids),
                "actual": stopped_run_count,
            }
        )


def _append_step_event_summary_issues(
    objective_run_id: str,
    run_events: list[ObjectiveEvent],
    stopped_line: int,
    step_results: list[Any],
    decisions: AutonomyRecordIndex,
    issues: list[dict[str, Any]],
) -> None:
    expected = _expected_step_records(objective_run_id, run_events, decisions, issues)
    steps = [step for step in step_results if isinstance(step, dict)]
    if len(steps) != len(expected):
        issues.append(
            {
                "line": stopped_line,
                "objective_run_id": objective_run_id,
                "reason": "step_event_count_mismatch",
                "expected": len(expected),
                "actual": len(steps),
            }
        )

    expected_by_key: dict[tuple[Any, ...], dict[str, Any]] = {}
    for record in expected:
        key = _step_record_key(record)
        if key in expected_by_key:
            issues.append(
                {
                    "line": record.get("line"),
                    "objective_run_id": objective_run_id,
                    "reason": "expected_step_key_duplicate",
                    "key": list(key),
                }
            )
        expected_by_key[key] = record

    matched_keys: set[tuple[Any, ...]] = set()
    for index, step in enumerate(steps):
        key = _step_record_key(step)
        expected_record = expected_by_key.get(key)
        if expected_record is None:
            issues.append(
                {
                    "line": stopped_line,
                    "objective_run_id": objective_run_id,
                    "reason": "step_event_missing",
                    "index": index,
                    "task_id": step.get("task_id"),
                    "lease_id": step.get("lease_id"),
                    "run_id": step.get("run_id"),
                    "batch": step.get("batch"),
                }
            )
            continue
        matched_keys.add(key)
        for field in ("adapter_id", "task_type", "decision_status", "execution_decision", "stop_reason"):
            expected_value = expected_record.get(field)
            if step.get(field) != expected_value:
                issues.append(
                    {
                        "line": stopped_line,
                        "objective_run_id": objective_run_id,
                        "reason": f"step_{field}_mismatch",
                        "index": index,
                        "task_id": step.get("task_id"),
                        "lease_id": step.get("lease_id"),
                        "expected": expected_value,
                        "actual": step.get(field),
                    }
                )
    for key, record in expected_by_key.items():
        if key not in matched_keys:
            issues.append(
                {
                    "line": stopped_line,
                    "objective_run_id": objective_run_id,
                    "reason": "expected_step_missing",
                    "event_line": record.get("line"),
                    "event": record.get("event"),
                    "task_id": record.get("task_id"),
                    "lease_id": record.get("lease_id"),
                    "run_id": record.get("run_id"),
                    "batch": record.get("batch"),
                }
            )


def _expected_step_records(
    objective_run_id: str,
    run_events: list[ObjectiveEvent],
    decisions: AutonomyRecordIndex,
    issues: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line_number, event in run_events:
        event_name = event.get("event")
        if event_name == "adapter_dispatched":
            decision = _decision_for_event(objective_run_id, line_number, event, decisions, issues)
            records.append(
                {
                    "line": line_number,
                    "event": event_name,
                    "task_id": event.get("task_id"),
                    "lease_id": event.get("lease_id"),
                    "run_id": event.get("run_id"),
                    "batch": event.get("batch"),
                    "adapter_id": event.get("adapter_id") or decision.get("adapter_id"),
                    "task_type": decision.get("task_type"),
                    "decision_status": decision.get("status"),
                    "execution_decision": event.get("decision"),
                    "stop_reason": event.get("stop_reason"),
                }
            )
        elif event_name == "execution_error":
            decision = _decision_for_event(objective_run_id, line_number, event, decisions, issues)
            records.append(
                {
                    "line": line_number,
                    "event": event_name,
                    "task_id": event.get("task_id"),
                    "lease_id": event.get("lease_id"),
                    "run_id": None,
                    "batch": event.get("batch"),
                    "adapter_id": decision.get("adapter_id"),
                    "task_type": decision.get("task_type"),
                    "decision_status": decision.get("status"),
                    "execution_decision": None,
                    "stop_reason": "execution_error",
                }
            )
        elif event_name == "autonomy_stopped":
            decision = _decision_for_event(objective_run_id, line_number, event, decisions, issues)
            records.append(
                {
                    "line": line_number,
                    "event": event_name,
                    "task_id": event.get("task_id"),
                    "lease_id": event.get("lease_id"),
                    "run_id": None,
                    "batch": event.get("batch"),
                    "adapter_id": decision.get("adapter_id"),
                    "task_type": decision.get("task_type"),
                    "decision_status": decision.get("status"),
                    "execution_decision": None,
                    "stop_reason": decision.get("status"),
                }
            )
        elif event_name == "lease_guard_stopped":
            decision = _decision_for_event(objective_run_id, line_number, event, decisions, issues)
            records.append(
                {
                    "line": line_number,
                    "event": event_name,
                    "task_id": event.get("task_id"),
                    "lease_id": event.get("lease_id"),
                    "run_id": None,
                    "batch": event.get("batch"),
                    "adapter_id": event.get("adapter_id") or decision.get("adapter_id"),
                    "task_type": decision.get("task_type"),
                    "decision_status": decision.get("status"),
                    "execution_decision": None,
                    "stop_reason": event.get("stop_reason"),
                }
            )
    return records


def _decision_for_event(
    objective_run_id: str,
    line_number: int,
    event: dict[str, Any],
    decisions: AutonomyRecordIndex,
    issues: list[dict[str, Any]],
) -> dict[str, Any]:
    decision_id = event.get("autonomy_decision_id")
    if not isinstance(decision_id, str) or not decision_id:
        issues.append(
            {
                "line": line_number,
                "objective_run_id": objective_run_id,
                "reason": "step_decision_missing",
                "autonomy_decision_id": decision_id,
            }
        )
        return {}
    _append_referenced_autonomy_store_issues(line_number, "autonomy_decision_id", decision_id, decisions, issues)
    decision = decisions.records.get(decision_id)
    if decision is None:
        issues.append(
            {
                "line": line_number,
                "objective_run_id": objective_run_id,
                "reason": "autonomy_decision_id_not_found",
                "autonomy_decision_id": decision_id,
            }
        )
        return {}
    _append_decision_record_consistency_issues(objective_run_id, line_number, event, decision_id, decision, issues)
    return decision


def _append_decision_record_consistency_issues(
    objective_run_id: str,
    line_number: int,
    event: dict[str, Any],
    decision_id: str,
    decision: dict[str, Any],
    issues: list[dict[str, Any]],
) -> None:
    expected_fields = {
        "record_id": decision_id,
        "objective_run_id": objective_run_id,
        "objective_id": event.get("objective_id"),
        "task_id": event.get("task_id"),
        "lease_id": event.get("lease_id"),
        "tool_name": DISPATCH_AUTONOMY_TOOL_NAME,
    }
    for field, expected in expected_fields.items():
        _append_field_mismatch_issue(
            line_number,
            "autonomy_decision_id",
            decision_id,
            field,
            expected,
            decision.get(field),
            issues,
        )

    embedded = event.get("decision")
    if not isinstance(embedded, dict):
        return
    for field in AUTONOMY_DECISION_PAYLOAD_FIELDS:
        _append_field_mismatch_issue(
            line_number,
            "autonomy_decision_id",
            decision_id,
            f"embedded_{field}",
            decision.get(field),
            embedded.get(field),
            issues,
        )


def _step_record_key(record: dict[str, Any]) -> tuple[Any, ...]:
    run_id = record.get("run_id")
    if isinstance(run_id, str) and run_id:
        return ("adapter_dispatched", record.get("task_id"), record.get("lease_id"), run_id, record.get("batch"))
    return ("non_run", record.get("task_id"), record.get("lease_id"), record.get("batch"))


def _append_latest_status_issues(
    store: SQLiteStore,
    objective_id: str,
    stopped_line: int,
    stopped: dict[str, Any],
    issues: list[dict[str, Any]],
) -> None:
    final_task_statuses = stopped.get("final_task_statuses")
    if not isinstance(final_task_statuses, dict):
        issues.append({"line": stopped_line, "reason": "final_task_statuses_not_object"})
        return
    persisted_statuses = {task.id: task.status.value for task in store.list_tasks(objective_id=objective_id)}
    if final_task_statuses != persisted_statuses:
        issues.append(
            {
                "line": stopped_line,
                "reason": "final_task_statuses_mismatch",
                "expected": persisted_statuses,
                "actual": final_task_statuses,
            }
        )


def _append_selected_plan_issues(
    store: SQLiteStore,
    decisions: AutonomyRecordIndex,
    line_number: int,
    item: dict[str, Any],
    objective_id: str,
    objective_run_id: Any,
    issues: list[dict[str, Any]],
) -> None:
    task_id = item.get("task_id")
    lease_id = item.get("lease_id")
    decision_id = item.get("autonomy_decision_id")
    if not isinstance(task_id, str) or not task_id:
        issues.append({"line": line_number, "reason": "selected_missing_task_id"})
        return
    if not isinstance(lease_id, str) or not lease_id:
        issues.append({"line": line_number, "reason": "selected_missing_lease_id", "task_id": task_id})
        return
    try:
        task = store.get_task(task_id)
        if task.objective_id != objective_id:
            issues.append({"line": line_number, "reason": "selected_task_objective_mismatch", "task_id": task_id})
    except KeyError as exc:
        issues.append({"line": line_number, "reason": "selected_task_missing", "task_id": task_id, "error": str(exc)})
    try:
        lease = store.get_task_lease(lease_id)
        if lease.task_id != task_id:
            issues.append({"line": line_number, "reason": "selected_lease_task_mismatch", "task_id": task_id, "lease_id": lease_id})
    except KeyError as exc:
        issues.append({"line": line_number, "reason": "selected_lease_missing", "lease_id": lease_id, "error": str(exc)})
    if isinstance(decision_id, str):
        _append_referenced_autonomy_store_issues(line_number, "autonomy_decision_id", decision_id, decisions, issues)
    if not isinstance(decision_id, str) or decision_id not in decisions.records:
        issues.append({"line": line_number, "reason": "selected_autonomy_decision_missing", "autonomy_decision_id": decision_id})
        return
    decision = decisions.records[decision_id]
    expected_fields = {
        "record_id": decision_id,
        "objective_run_id": objective_run_id,
        "objective_id": objective_id,
        "task_id": task_id,
        "lease_id": lease_id,
        "tool_name": DISPATCH_AUTONOMY_TOOL_NAME,
        "adapter_id": item.get("adapter_id"),
        "task_type": item.get("task_type"),
        "status": item.get("decision_status"),
    }
    for field, expected in expected_fields.items():
        _append_field_mismatch_issue(
            line_number,
            "selected_autonomy_decision",
            decision_id,
            field,
            expected,
            decision.get(field),
            issues,
        )


def _read_autonomy_records(path: Path, id_key: str, schema_version: str) -> AutonomyRecordIndex:
    index = AutonomyRecordIndex(path=path, id_key=id_key, schema_version=schema_version)
    if not path.exists():
        return index
    events, parse_errors = _read_objective_evidence_events_raw(path)
    index.parse_errors = parse_errors
    for line_number, payload in events:
        record_id = payload.get(id_key)
        if not isinstance(record_id, str) or not record_id:
            continue
        if record_id in index.records:
            index.duplicate_ids.setdefault(record_id, [index.record_lines[record_id]]).append(line_number)
            continue
        index.records[record_id] = payload
        index.record_lines[record_id] = line_number
    return index


def _verification(
    project_root: Path,
    objective_id: str,
    evidence_path: Path,
    checks: list[ObjectiveEvidenceCheck],
) -> ObjectiveEvidenceVerification:
    summary = {
        "total": len(checks),
        "pass": sum(1 for check in checks if check.status == "pass"),
        "fail": sum(1 for check in checks if check.status == "fail"),
    }
    return ObjectiveEvidenceVerification(
        ok=summary["fail"] == 0,
        project_root=project_root,
        objective_id=objective_id,
        evidence_path=evidence_path,
        checks=checks,
        summary=summary,
    )


def _pass(check_id: str, message: str, evidence: dict[str, Any]) -> ObjectiveEvidenceCheck:
    return ObjectiveEvidenceCheck(id=check_id, status="pass", message=message, evidence=sanitize_for_logging(evidence))


def _fail(check_id: str, message: str, evidence: dict[str, Any]) -> ObjectiveEvidenceCheck:
    return ObjectiveEvidenceCheck(id=check_id, status="fail", message=message, evidence=sanitize_for_logging(evidence))
