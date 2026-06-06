from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from harness.config import HARNESS_DIR
from harness.events import append_jsonl
from harness.memory.sqlite_store import SQLiteStore
from harness.models import ObjectiveStatus, utc_now
from harness.paths import resolve_project_root
from harness.security import sanitize_for_logging


OBJECTIVE_CHECKPOINT_EVENT_SCHEMA_VERSION = "harness.objective_checkpoint_event/v1"
OBJECTIVE_CHECKPOINTS_SCHEMA_VERSION = "harness.objective_checkpoints/v1"
OBJECTIVE_CHECKPOINT_GATE_SCHEMA_VERSION = "harness.objective_checkpoint_gate/v1"
OBJECTIVE_CHECKPOINT_EVIDENCE_VERIFICATION_SCHEMA_VERSION = "harness.objective_checkpoint_evidence_verification/v1"

CheckpointStatus = Literal["pending", "approved", "rejected"]
CheckpointEventName = Literal["checkpoint_created", "checkpoint_approved", "checkpoint_rejected"]
CheckpointEvent = tuple[int, dict[str, Any]]


class ObjectiveCheckpointRecord(BaseModel):
    schema_version: str = "harness.objective_checkpoint/v1"
    checkpoint_id: str
    objective_id: str
    label: str
    required: bool = True
    status: CheckpointStatus
    reason: str = ""
    created_at: str
    updated_at: str
    created_by: str
    resolved_by: str | None = None
    approval_id: str | None = None
    verdict_reason: str | None = None
    event_count: int = 0
    latest_event_sha256: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    contents_included: bool = False
    execution_allowed: bool = False
    model_context_allowed: bool = False
    network_required: bool = False
    mutation_allowed: bool = False
    permission_granting: bool = False


class ObjectiveCheckpointEvidenceCheck(BaseModel):
    id: str
    status: Literal["pass", "fail"]
    message: str
    evidence: dict[str, Any] = Field(default_factory=dict)


class ObjectiveCheckpointEvidenceVerification(BaseModel):
    schema_version: str = OBJECTIVE_CHECKPOINT_EVIDENCE_VERIFICATION_SCHEMA_VERSION
    ok: bool
    project_root: Path
    objective_id: str
    evidence_path: Path
    checks: list[ObjectiveCheckpointEvidenceCheck] = Field(default_factory=list)
    summary: dict[str, int] = Field(default_factory=dict)
    contents_included: bool = False
    execution_allowed: bool = False
    model_context_allowed: bool = False
    network_required: bool = False
    mutation_allowed: bool = False
    permission_granting: bool = False


class ObjectiveCheckpointsProjection(BaseModel):
    schema_version: str = OBJECTIVE_CHECKPOINTS_SCHEMA_VERSION
    ok: bool = True
    project_root: Path
    objective_id: str
    evidence_path: Path
    checkpoints: list[ObjectiveCheckpointRecord] = Field(default_factory=list)
    event_count: int = 0
    head_sha256: str | None = None
    evidence_ok: bool = True
    evidence_summary: dict[str, int] = Field(default_factory=dict)
    evidence_failed_check_ids: list[str] = Field(default_factory=list)
    contents_included: bool = False
    execution_allowed: bool = False
    model_context_allowed: bool = False
    network_required: bool = False
    mutation_allowed: bool = False
    permission_granting: bool = False


class ObjectiveCheckpointGate(BaseModel):
    schema_version: str = OBJECTIVE_CHECKPOINT_GATE_SCHEMA_VERSION
    ok: bool
    project_root: Path
    objective_id: str
    gate_id: str = "checkpoint_approved"
    status: Literal["pass", "blocked"]
    required_checkpoint_count: int
    pending_checkpoint_ids: list[str] = Field(default_factory=list)
    rejected_checkpoint_ids: list[str] = Field(default_factory=list)
    checkpoints: list[ObjectiveCheckpointRecord] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    evidence_ok: bool = True
    evidence_summary: dict[str, int] = Field(default_factory=dict)
    evidence_failed_check_ids: list[str] = Field(default_factory=list)
    contents_included: bool = False
    execution_allowed: bool = False
    model_context_allowed: bool = False
    network_required: bool = False
    mutation_allowed: bool = False
    permission_granting: bool = False


def create_objective_checkpoint(
    project_root: Path,
    objective_id: str,
    *,
    label: str,
    reason: str = "",
    required: bool = True,
    actor: str = "operator",
    metadata: dict[str, Any] | None = None,
) -> ObjectiveCheckpointRecord:
    project_root = resolve_project_root(project_root)
    store = SQLiteStore(project_root)
    objective = store.get_objective(objective_id)
    _assert_checkpoint_evidence_writable(project_root, objective_id)
    clean_label = str(sanitize_for_logging(label)).strip()
    if not clean_label:
        raise ValueError("checkpoint label is required")
    checkpoint_id = f"ockpt_{uuid.uuid4().hex[:12]}"
    timestamp = utc_now().isoformat()
    _append_checkpoint_event(
        _checkpoint_events_path(project_root, objective_id),
        objective_id=objective_id,
        checkpoint_id=checkpoint_id,
        event="checkpoint_created",
        payload={
            "label": clean_label,
            "required": bool(required),
            "status": "pending",
            "reason": str(sanitize_for_logging(reason)).strip(),
            "actor": str(sanitize_for_logging(actor)).strip() or "operator",
            "created_at": timestamp,
            "metadata": sanitize_for_logging(metadata or {}),
        },
    )
    if required and objective.status == ObjectiveStatus.ACTIVE:
        store.update_objective_status(
            objective_id,
            ObjectiveStatus.WAITING_APPROVAL,
            reason="Required objective checkpoint is pending approval.",
            actor=str(sanitize_for_logging(actor)).strip() or "operator",
            metadata={
                "source": "objective_checkpoint",
                "checkpoint_id": checkpoint_id,
                "checkpoint_status": "pending",
                "gate_id": "checkpoint_approved",
            },
        )
    return get_objective_checkpoint(project_root, objective_id, checkpoint_id)


def resolve_objective_checkpoint(
    project_root: Path,
    objective_id: str,
    checkpoint_id: str,
    *,
    verdict: Literal["approved", "rejected"],
    reason: str = "",
    approval_id: str | None = None,
    actor: str = "operator",
) -> ObjectiveCheckpointRecord:
    project_root = resolve_project_root(project_root)
    existing = get_objective_checkpoint(project_root, objective_id, checkpoint_id)
    _assert_checkpoint_evidence_writable(project_root, objective_id)
    if existing.status in {"approved", "rejected"}:
        raise ValueError(f"checkpoint already resolved: {checkpoint_id}")
    if verdict == "approved" and not str(approval_id or "").strip():
        raise ValueError("approval_id is required when approving a checkpoint")
    timestamp = utc_now().isoformat()
    _append_checkpoint_event(
        _checkpoint_events_path(project_root, objective_id),
        objective_id=objective_id,
        checkpoint_id=checkpoint_id,
        event="checkpoint_approved" if verdict == "approved" else "checkpoint_rejected",
        payload={
            "label": existing.label,
            "required": existing.required,
            "status": verdict,
            "reason": existing.reason,
            "actor": existing.created_by,
            "resolved_by": str(sanitize_for_logging(actor)).strip() or "operator",
            "resolved_at": timestamp,
            "approval_id": str(sanitize_for_logging(approval_id)).strip() if approval_id else None,
            "verdict_reason": str(sanitize_for_logging(reason)).strip(),
            "metadata": existing.metadata,
        },
    )
    checkpoint = get_objective_checkpoint(project_root, objective_id, checkpoint_id)
    if verdict == "approved":
        _resume_waiting_objective_if_checkpoint_gate_passes(
            project_root,
            objective_id,
            checkpoint_id=checkpoint_id,
            approval_id=approval_id,
            actor=actor,
        )
    return checkpoint


def _resume_waiting_objective_if_checkpoint_gate_passes(
    project_root: Path,
    objective_id: str,
    *,
    checkpoint_id: str,
    approval_id: str | None,
    actor: str,
) -> None:
    store = SQLiteStore(project_root)
    objective = store.get_objective(objective_id)
    if objective.status != ObjectiveStatus.WAITING_APPROVAL:
        return
    gate = evaluate_objective_checkpoint_gate(project_root, objective_id)
    if not gate.ok:
        return
    store.update_objective_status(
        objective_id,
        ObjectiveStatus.ACTIVE,
        reason="Required objective checkpoints are approved.",
        actor=str(sanitize_for_logging(actor)).strip() or "operator",
        metadata={
            "source": "objective_checkpoint",
            "checkpoint_id": checkpoint_id,
            "approval_id": str(sanitize_for_logging(approval_id)).strip() if approval_id else None,
            "gate_id": gate.gate_id,
            "pending_checkpoint_ids": list(gate.pending_checkpoint_ids),
            "rejected_checkpoint_ids": list(gate.rejected_checkpoint_ids),
        },
    )


def list_objective_checkpoints(project_root: Path, objective_id: str) -> ObjectiveCheckpointsProjection:
    project_root = resolve_project_root(project_root)
    SQLiteStore(project_root).get_objective(objective_id)
    evidence_path = _checkpoint_events_path(project_root, objective_id)
    events = _read_checkpoint_events(evidence_path)
    records = _reduce_checkpoint_events(objective_id, events)
    verification = verify_objective_checkpoint_evidence(project_root, objective_id, evidence_path=evidence_path)
    return ObjectiveCheckpointsProjection(
        ok=verification.ok,
        project_root=project_root,
        objective_id=objective_id,
        evidence_path=evidence_path,
        checkpoints=records,
        event_count=len(events),
        head_sha256=_head_sha256(events),
        evidence_ok=verification.ok,
        evidence_summary=dict(verification.summary),
        evidence_failed_check_ids=[check.id for check in verification.checks if check.status == "fail"],
    )


def get_objective_checkpoint(project_root: Path, objective_id: str, checkpoint_id: str) -> ObjectiveCheckpointRecord:
    projection = list_objective_checkpoints(project_root, objective_id)
    for checkpoint in projection.checkpoints:
        if checkpoint.checkpoint_id == checkpoint_id:
            return checkpoint
    raise KeyError(f"Objective checkpoint not found: {checkpoint_id}")


def evaluate_objective_checkpoint_gate(project_root: Path, objective_id: str) -> ObjectiveCheckpointGate:
    project_root = resolve_project_root(project_root)
    projection = list_objective_checkpoints(project_root, objective_id)
    required = [checkpoint for checkpoint in projection.checkpoints if checkpoint.required]
    pending = [checkpoint.checkpoint_id for checkpoint in required if checkpoint.status == "pending"]
    rejected = [checkpoint.checkpoint_id for checkpoint in required if checkpoint.status == "rejected"]
    reasons: list[str] = []
    if pending:
        reasons.append(f"required objective checkpoints pending: {', '.join(pending)}")
    if rejected:
        reasons.append(f"required objective checkpoints rejected: {', '.join(rejected)}")
    if not projection.evidence_ok:
        reasons.append(
            "objective checkpoint evidence verification failed: "
            + ", ".join(projection.evidence_failed_check_ids or ["unknown"])
        )
    blocked = bool(pending or rejected or not projection.evidence_ok)
    return ObjectiveCheckpointGate(
        ok=not blocked,
        project_root=project_root,
        objective_id=objective_id,
        status="blocked" if blocked else "pass",
        required_checkpoint_count=len(required),
        pending_checkpoint_ids=pending,
        rejected_checkpoint_ids=rejected,
        checkpoints=required,
        reasons=reasons,
        evidence_ok=projection.evidence_ok,
        evidence_summary=dict(projection.evidence_summary),
        evidence_failed_check_ids=list(projection.evidence_failed_check_ids),
    )


def verify_objective_checkpoint_evidence(
    project_root: Path,
    objective_id: str,
    *,
    evidence_path: Path | None = None,
) -> ObjectiveCheckpointEvidenceVerification:
    project_root = resolve_project_root(project_root)
    evidence_path = evidence_path or _checkpoint_events_path(project_root, objective_id)
    checks: list[ObjectiveCheckpointEvidenceCheck] = []

    try:
        SQLiteStore(project_root).get_objective(objective_id)
    except KeyError as exc:
        checks.append(_verification_fail("objective_exists", "Objective is missing from Harness persistence.", {"error": str(exc)}))
        return _checkpoint_verification(project_root, objective_id, evidence_path, checks)
    checks.append(_verification_pass("objective_exists", "Objective exists in Harness persistence.", {}))

    if not evidence_path.exists():
        checks.append(
            _verification_pass(
                "evidence_file",
                "No checkpoint evidence file exists; no objective checkpoints have been recorded.",
                {"path": str(evidence_path), "exists": False},
            )
        )
        return _checkpoint_verification(project_root, objective_id, evidence_path, checks)

    events, parse_errors = _read_checkpoint_events_with_errors(evidence_path)
    if parse_errors:
        checks.append(
            _verification_fail(
                "jsonl_parse",
                "Objective checkpoint evidence contains malformed JSONL records.",
                {"line_count": len(events) + len(parse_errors), "errors": sanitize_for_logging(parse_errors)},
            )
        )
    else:
        checks.append(
            _verification_pass(
                "jsonl_parse",
                "Objective checkpoint evidence JSONL parses cleanly.",
                {"event_count": len(events), "path": str(evidence_path)},
            )
        )

    checks.append(_checkpoint_event_schema_check(events))
    checks.append(_checkpoint_event_identity_check(events))
    checks.append(_checkpoint_event_hash_chain_check(events))
    checks.append(_checkpoint_event_timestamp_check(events))
    checks.append(_checkpoint_event_lifecycle_check(events, objective_id))
    return _checkpoint_verification(project_root, objective_id, evidence_path, checks)


def _assert_checkpoint_evidence_writable(project_root: Path, objective_id: str) -> None:
    verification = verify_objective_checkpoint_evidence(project_root, objective_id)
    if verification.ok:
        return
    failed = ", ".join(check.id for check in verification.checks if check.status == "fail") or "unknown"
    raise ValueError(f"objective checkpoint evidence verification failed: {failed}")


def _checkpoint_events_path(project_root: Path, objective_id: str) -> Path:
    return project_root / HARNESS_DIR / "autonomy" / "objectives" / f"{objective_id}.checkpoints.jsonl"


def _append_checkpoint_event(
    path: Path,
    *,
    objective_id: str,
    checkpoint_id: str,
    event: CheckpointEventName,
    payload: dict[str, Any],
) -> None:
    previous_sha = _previous_checkpoint_event_sha256(path)
    event_record = {
        "schema_version": OBJECTIVE_CHECKPOINT_EVENT_SCHEMA_VERSION,
        "objective_id": objective_id,
        "checkpoint_id": checkpoint_id,
        "checkpoint_event_id": f"ockpt_evt_{uuid.uuid4().hex[:12]}",
        "event_index": _next_checkpoint_event_index(path),
        "event": event,
        "created_at": utc_now().isoformat(),
        "previous_event_sha256": previous_sha,
        "contents_included": False,
        "execution_allowed": False,
        "model_context_allowed": False,
        "network_required": False,
        "mutation_allowed": False,
        "permission_granting": False,
        **sanitize_for_logging(payload),
    }
    event_record["event_sha256"] = _checkpoint_event_sha256(event_record)
    append_jsonl(path, event_record)


def _read_checkpoint_events(path: Path) -> list[dict[str, Any]]:
    events, _ = _read_checkpoint_events_with_errors(path)
    return [sanitize_for_logging(event) for _, event in events]


def _read_checkpoint_events_with_errors(path: Path) -> tuple[list[CheckpointEvent], list[dict[str, Any]]]:
    events: list[CheckpointEvent] = []
    errors: list[dict[str, Any]] = []
    if not path.exists():
        return events, errors
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw_line.strip():
            continue
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            errors.append({"line": line_number, "error": f"{exc.__class__.__name__}: {exc.msg}"})
            continue
        if not isinstance(event, dict):
            errors.append({"line": line_number, "error": "JSONL record is not an object."})
            continue
        events.append((line_number, event))
    return events, errors


def _checkpoint_event_schema_check(events: list[CheckpointEvent]) -> ObjectiveCheckpointEvidenceCheck:
    invalid: list[dict[str, Any]] = []
    known_events = {"checkpoint_created", "checkpoint_approved", "checkpoint_rejected"}
    for line_number, event in events:
        if event.get("schema_version") != OBJECTIVE_CHECKPOINT_EVENT_SCHEMA_VERSION:
            invalid.append({"line": line_number, "reason": "schema_version", "value": event.get("schema_version")})
        _require_checkpoint_str(event, "objective_id", line_number, invalid)
        _require_checkpoint_str(event, "checkpoint_id", line_number, invalid)
        _require_checkpoint_str(event, "checkpoint_event_id", line_number, invalid)
        _require_checkpoint_positive_int(event, "event_index", line_number, invalid)
        _require_checkpoint_str(event, "created_at", line_number, invalid)
        if "previous_event_sha256" not in event:
            invalid.append({"line": line_number, "reason": "previous_event_sha256"})
        elif event.get("previous_event_sha256") is not None and not isinstance(event.get("previous_event_sha256"), str):
            invalid.append({"line": line_number, "reason": "previous_event_sha256", "value": event.get("previous_event_sha256")})
        _require_checkpoint_str(event, "event_sha256", line_number, invalid)
        event_name = event.get("event")
        if event_name not in known_events:
            invalid.append({"line": line_number, "reason": "event", "value": event_name})
            continue
        for key in (
            "contents_included",
            "execution_allowed",
            "model_context_allowed",
            "network_required",
            "mutation_allowed",
            "permission_granting",
        ):
            if event.get(key) is not False:
                invalid.append({"line": line_number, "reason": key, "expected": False, "actual": event.get(key)})
        if event_name == "checkpoint_created":
            _require_checkpoint_str(event, "label", line_number, invalid)
            _require_checkpoint_bool(event, "required", line_number, invalid)
            _require_checkpoint_str(event, "actor", line_number, invalid)
            if event.get("status") != "pending":
                invalid.append({"line": line_number, "reason": "created_status", "expected": "pending", "actual": event.get("status")})
            if "metadata" in event and not isinstance(event.get("metadata"), dict):
                invalid.append({"line": line_number, "reason": "metadata", "expected": "object"})
        else:
            _require_checkpoint_str(event, "label", line_number, invalid)
            _require_checkpoint_bool(event, "required", line_number, invalid)
            _require_checkpoint_str(event, "resolved_by", line_number, invalid)
            _require_checkpoint_str(event, "resolved_at", line_number, invalid)
            expected_status = "approved" if event_name == "checkpoint_approved" else "rejected"
            if event.get("status") != expected_status:
                invalid.append(
                    {
                        "line": line_number,
                        "reason": "resolution_status",
                        "expected": expected_status,
                        "actual": event.get("status"),
                    }
                )
            if event_name == "checkpoint_approved":
                _require_checkpoint_str(event, "approval_id", line_number, invalid)
    if invalid:
        return _verification_fail(
            "event_schema",
            "Objective checkpoint evidence records do not match the checkpoint event envelope.",
            {"invalid": sanitize_for_logging(invalid)},
        )
    return _verification_pass(
        "event_schema",
        "Objective checkpoint evidence records match the checkpoint event envelope.",
        {"event_count": len(events)},
    )


def _checkpoint_event_identity_check(events: list[CheckpointEvent]) -> ObjectiveCheckpointEvidenceCheck:
    issues: list[dict[str, Any]] = []
    seen_ids: dict[str, int] = {}
    seen_indexes: dict[int, int] = {}
    for expected_index, (line_number, event) in enumerate(events, start=1):
        event_id = event.get("checkpoint_event_id")
        if isinstance(event_id, str) and event_id:
            previous_line = seen_ids.get(event_id)
            if previous_line is not None:
                issues.append({"line": line_number, "reason": "checkpoint_event_id_duplicate", "previous_line": previous_line})
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
        return _verification_fail(
            "event_identity",
            "Objective checkpoint event ids or indexes are duplicated or out of sequence.",
            {"issues": sanitize_for_logging(issues)},
        )
    return _verification_pass(
        "event_identity",
        "Objective checkpoint event ids are unique and indexes match JSONL order.",
        {"event_count": len(events), "unique_event_ids": len(seen_ids)},
    )


def _checkpoint_event_hash_chain_check(events: list[CheckpointEvent]) -> ObjectiveCheckpointEvidenceCheck:
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
        recomputed_hash = _checkpoint_event_sha256(event)
        if recorded_hash != recomputed_hash:
            issues.append(
                {
                    "line": line_number,
                    "reason": "event_sha256_mismatch",
                    "expected": recomputed_hash,
                    "actual": recorded_hash,
                }
            )
        previous_hash = recomputed_hash
    if issues:
        return _verification_fail(
            "event_hash_chain",
            "Objective checkpoint event hashes or previous-hash links do not match the JSONL records.",
            {"issues": sanitize_for_logging(issues)},
        )
    return _verification_pass(
        "event_hash_chain",
        "Objective checkpoint event hashes match JSONL records and previous-hash links.",
        {"event_count": len(events), "head_sha256": previous_hash},
    )


def _checkpoint_event_timestamp_check(events: list[CheckpointEvent]) -> ObjectiveCheckpointEvidenceCheck:
    invalid: list[dict[str, Any]] = []
    previous: tuple[int, str, datetime] | None = None
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
        if previous is not None and timestamp < previous[2]:
            invalid.append(
                {
                    "line": line_number,
                    "reason": "created_at_out_of_order",
                    "created_at": created_at,
                    "previous_line": previous[0],
                    "previous_created_at": previous[1],
                }
            )
        previous = (line_number, created_at, timestamp)
    if invalid:
        return _verification_fail(
            "event_timestamps",
            "Objective checkpoint event timestamps are missing, malformed, timezone-naive, or out of order.",
            {"invalid": sanitize_for_logging(invalid)},
        )
    return _verification_pass(
        "event_timestamps",
        "Objective checkpoint event timestamps are parseable, timezone-aware, and ordered.",
        {"event_count": len(events)},
    )


def _checkpoint_event_lifecycle_check(events: list[CheckpointEvent], objective_id: str) -> ObjectiveCheckpointEvidenceCheck:
    issues: list[dict[str, Any]] = []
    created: set[str] = set()
    resolved: set[str] = set()
    for line_number, event in events:
        event_objective_id = event.get("objective_id")
        if event_objective_id != objective_id:
            issues.append({"line": line_number, "reason": "objective_id_mismatch", "actual": event_objective_id})
        checkpoint_id = event.get("checkpoint_id")
        event_name = event.get("event")
        if not isinstance(checkpoint_id, str) or not checkpoint_id:
            continue
        if event_name == "checkpoint_created":
            if checkpoint_id in created:
                issues.append({"line": line_number, "reason": "duplicate_checkpoint_created", "checkpoint_id": checkpoint_id})
            if checkpoint_id in resolved:
                issues.append({"line": line_number, "reason": "checkpoint_created_after_resolution", "checkpoint_id": checkpoint_id})
            created.add(checkpoint_id)
        elif event_name in {"checkpoint_approved", "checkpoint_rejected"}:
            if checkpoint_id not in created:
                issues.append({"line": line_number, "reason": "checkpoint_resolved_without_create", "checkpoint_id": checkpoint_id})
            if checkpoint_id in resolved:
                issues.append({"line": line_number, "reason": "duplicate_checkpoint_resolution", "checkpoint_id": checkpoint_id})
            resolved.add(checkpoint_id)
    if issues:
        return _verification_fail(
            "checkpoint_lifecycle",
            "Objective checkpoint evidence has invalid create/resolve lifecycle records.",
            {"issues": sanitize_for_logging(issues)},
        )
    return _verification_pass(
        "checkpoint_lifecycle",
        "Objective checkpoint create/resolve lifecycle records are consistent.",
        {"checkpoint_count": len(created), "resolved_checkpoint_count": len(resolved)},
    )


def _require_checkpoint_str(event: dict[str, Any], key: str, line_number: int, invalid: list[dict[str, Any]]) -> None:
    value = event.get(key)
    if not isinstance(value, str) or not value:
        invalid.append({"line": line_number, "reason": key, "expected": "string"})


def _require_checkpoint_bool(event: dict[str, Any], key: str, line_number: int, invalid: list[dict[str, Any]]) -> None:
    if not isinstance(event.get(key), bool):
        invalid.append({"line": line_number, "reason": key, "expected": "bool"})


def _require_checkpoint_positive_int(event: dict[str, Any], key: str, line_number: int, invalid: list[dict[str, Any]]) -> None:
    value = event.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        invalid.append({"line": line_number, "reason": key, "expected": "positive_int", "actual": value})


def _verification_pass(id: str, message: str, evidence: dict[str, Any]) -> ObjectiveCheckpointEvidenceCheck:
    return ObjectiveCheckpointEvidenceCheck(id=id, status="pass", message=message, evidence=evidence)


def _verification_fail(id: str, message: str, evidence: dict[str, Any]) -> ObjectiveCheckpointEvidenceCheck:
    return ObjectiveCheckpointEvidenceCheck(id=id, status="fail", message=message, evidence=evidence)


def _checkpoint_verification(
    project_root: Path,
    objective_id: str,
    evidence_path: Path,
    checks: list[ObjectiveCheckpointEvidenceCheck],
) -> ObjectiveCheckpointEvidenceVerification:
    summary = {
        "total": len(checks),
        "pass": sum(1 for check in checks if check.status == "pass"),
        "fail": sum(1 for check in checks if check.status == "fail"),
    }
    return ObjectiveCheckpointEvidenceVerification(
        ok=summary["fail"] == 0,
        project_root=project_root,
        objective_id=objective_id,
        evidence_path=evidence_path,
        checks=checks,
        summary=summary,
    )


def _reduce_checkpoint_events(
    objective_id: str,
    events: list[dict[str, Any]],
) -> list[ObjectiveCheckpointRecord]:
    by_id: dict[str, ObjectiveCheckpointRecord] = {}
    counts: dict[str, int] = {}
    for event in events:
        if event.get("schema_version") != OBJECTIVE_CHECKPOINT_EVENT_SCHEMA_VERSION:
            continue
        if event.get("objective_id") != objective_id:
            continue
        checkpoint_id = str(event.get("checkpoint_id") or "").strip()
        if not checkpoint_id:
            continue
        counts[checkpoint_id] = counts.get(checkpoint_id, 0) + 1
        event_name = event.get("event")
        if event_name == "checkpoint_created":
            created_at = str(event.get("created_at") or "")
            by_id[checkpoint_id] = ObjectiveCheckpointRecord(
                checkpoint_id=checkpoint_id,
                objective_id=objective_id,
                label=str(event.get("label") or checkpoint_id),
                required=bool(event.get("required", True)),
                status="pending",
                reason=str(event.get("reason") or ""),
                created_at=created_at,
                updated_at=created_at,
                created_by=str(event.get("actor") or "operator"),
                event_count=counts[checkpoint_id],
                latest_event_sha256=_event_sha(event),
                metadata=_dict(event.get("metadata")),
            )
        elif event_name in {"checkpoint_approved", "checkpoint_rejected"} and checkpoint_id in by_id:
            current = by_id[checkpoint_id]
            updated_at = str(event.get("resolved_at") or event.get("created_at") or current.updated_at)
            by_id[checkpoint_id] = current.model_copy(
                update={
                    "status": "approved" if event_name == "checkpoint_approved" else "rejected",
                    "updated_at": updated_at,
                    "resolved_by": str(event.get("resolved_by") or "operator"),
                    "approval_id": event.get("approval_id"),
                    "verdict_reason": str(event.get("verdict_reason") or ""),
                    "event_count": counts[checkpoint_id],
                    "latest_event_sha256": _event_sha(event),
                }
            )
    return sorted(by_id.values(), key=lambda item: (item.created_at, item.checkpoint_id))


def _previous_checkpoint_event_sha256(path: Path) -> str | None:
    if not path.exists():
        return None
    for raw_line in reversed(path.read_text(encoding="utf-8").splitlines()):
        if not raw_line.strip():
            continue
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            return None
        event_sha = event.get("event_sha256") if isinstance(event, dict) else None
        return event_sha if isinstance(event_sha, str) and event_sha else None
    return None


def _next_checkpoint_event_index(path: Path) -> int:
    if not path.exists():
        return 1
    return 1 + sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())


def _checkpoint_event_sha256(event: dict[str, Any]) -> str:
    stable = {key: value for key, value in event.items() if key != "event_sha256"}
    encoded = json.dumps(stable, sort_keys=True, default=str, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _head_sha256(events: list[dict[str, Any]]) -> str | None:
    for event in reversed(events):
        event_sha = event.get("event_sha256")
        if isinstance(event_sha, str) and event_sha:
            return event_sha
    return None


def _event_sha(event: dict[str, Any]) -> str | None:
    value = event.get("event_sha256")
    return value if isinstance(value, str) and value else None


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}
