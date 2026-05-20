from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from harness.config import HARNESS_DIR
from harness.memory.sqlite_store import SQLiteStore
from harness.models import ArtifactRecord, EventRecord, RunRecord, TaskLease, TaskRecord
from harness.security import is_secret_path, sanitize_for_logging


CORE_RUN_PROJECTION_SCHEMA_VERSION = "harness.core_run_projection/v1"
CORE_EVENT_PROJECTION_SCHEMA_VERSION = "harness.core_event_projection/v1"
CORE_ARTIFACT_PROJECTION_SCHEMA_VERSION = "harness.core_artifact_projection/v1"
CORE_BLOCKED_STATE_PROJECTION_SCHEMA_VERSION = "harness.core_blocked_state_projection/v1"
CORE_TASK_PROJECTION_SCHEMA_VERSION = "harness.core_task_projection/v1"


class CoreArtifactProjection(BaseModel):
    schema_version: str = CORE_ARTIFACT_PROJECTION_SCHEMA_VERSION
    artifact_id: str
    run_id: str
    kind: str
    path: str
    sha256: str | None = None
    size_bytes: int | None = None
    producer: str | None = None
    redaction_state: str
    evidence_status: str
    created_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)


class CoreEventProjection(BaseModel):
    schema_version: str = CORE_EVENT_PROJECTION_SCHEMA_VERSION
    event_id: str
    run_id: str
    task_id: str | None = None
    seq: int | None = None
    event_type: str
    level: str
    message: str
    visibility: str
    redaction_state: str
    created_at: datetime
    payload: dict[str, Any] = Field(default_factory=dict)


class CoreTaskProjection(BaseModel):
    schema_version: str = CORE_TASK_PROJECTION_SCHEMA_VERSION
    task_id: str
    objective_id: str | None = None
    run_id: str | None = None
    adapter_id: str | None = None
    task_type: str | None = None
    status: str
    approval_id: str | None = None
    required_approvals: list[str] = Field(default_factory=list)
    approval_state: str | None = None


class CoreBlockedStateProjection(BaseModel):
    schema_version: str = CORE_BLOCKED_STATE_PROJECTION_SCHEMA_VERSION
    ok: bool = False
    run_id: str | None = None
    task_id: str | None = None
    objective_id: str | None = None
    lease_id: str | None = None
    adapter_id: str | None = None
    task_type: str | None = None
    status: str | None = None
    decision: str
    manifest: str | None = None
    artifact_ids: list[str] = Field(default_factory=list)
    approval_id: str | None = None
    policy_sha256: str | None = None
    errors: list[str] = Field(default_factory=list)
    blocked_reasons: list[str] = Field(default_factory=list)
    next_commands: list[str] = Field(default_factory=list)


class CoreRunProjection(BaseModel):
    schema_version: str = CORE_RUN_PROJECTION_SCHEMA_VERSION
    ok: bool
    run_id: str
    task_id: str | None = None
    objective_id: str | None = None
    lease_id: str | None = None
    adapter_id: str | None = None
    task_type: str | None = None
    status: str
    decision: str
    manifest: str | None = None
    artifact_ids: list[str] = Field(default_factory=list)
    artifacts: list[CoreArtifactProjection] = Field(default_factory=list)
    approval_id: str | None = None
    policy_sha256: str | None = None
    errors: list[str] = Field(default_factory=list)
    blocked_reasons: list[str] = Field(default_factory=list)
    next_commands: list[str] = Field(default_factory=list)
    task: CoreTaskProjection | None = None


def build_core_run_projection(project_root: Path, run_id: str) -> CoreRunProjection:
    root = Path(project_root).resolve()
    store = _store_for_projection(root)
    run = store.get_run(run_id)
    task = _safe_get_task(store, run.task_id)
    lease = _latest_lease(store, task.id if task is not None else run.task_id)
    manifest = store.build_run_manifest(run.id)
    artifacts = [_artifact_projection(artifact) for artifact in store.list_artifacts(run.id)]
    errors = _run_errors(run)
    blocked_reasons = _blocked_reasons_for_run(store, run, task, lease)
    adapter_id = _adapter_id(task)
    policy_sha256 = manifest.effective_policy_sha256 or _policy_sha256_from_events(store, run.id)
    return CoreRunProjection(
        ok=not errors and not blocked_reasons,
        run_id=run.id,
        task_id=run.task_id,
        objective_id=run.objective_id,
        lease_id=lease.id if lease is not None else None,
        adapter_id=adapter_id,
        task_type=run.task_type,
        status=run.status,
        decision=_decision_for_run(run),
        manifest=_manifest_path(root, run.id),
        artifact_ids=[artifact.artifact_id for artifact in artifacts],
        artifacts=artifacts,
        approval_id=run.approval_id or manifest.approval_id,
        policy_sha256=policy_sha256,
        errors=errors,
        blocked_reasons=blocked_reasons,
        next_commands=_next_commands(root, run_id=run.id, task_id=run.task_id, lease_id=lease.id if lease else None),
        task=_task_projection(task) if task is not None else None,
    )


def list_core_run_events(project_root: Path, run_id: str) -> list[CoreEventProjection]:
    root = Path(project_root).resolve()
    store = _store_for_projection(root)
    store.get_run(run_id)
    return [_event_projection(event) for event in store.list_events(run_id)]


def build_core_blocked_state_projection(project_root: Path, task_id: str) -> CoreBlockedStateProjection:
    root = Path(project_root).resolve()
    store = _store_for_projection(root)
    task = store.get_task(task_id)
    lease = _latest_lease(store, task.id)
    daemon_metadata = _latest_daemon_rejection_metadata(store, task.id, lease.id if lease is not None else None)
    blocked_reasons = _dedupe(
        [
            *[f"Missing required approval: {approval}." for approval in task.required_approvals],
            *[str(item) for item in daemon_metadata.get("rejection_reasons", [])],
        ]
    )
    policy_sha256 = daemon_metadata.get("policy_sha256")
    decision = str(daemon_metadata.get("decision") or daemon_metadata.get("event_type") or "blocked")
    return CoreBlockedStateProjection(
        run_id=None,
        task_id=task.id,
        objective_id=task.objective_id,
        lease_id=lease.id if lease is not None else None,
        adapter_id=_adapter_id(task),
        task_type=_task_type(task),
        status=task.status.value,
        decision=decision,
        approval_id=None,
        policy_sha256=str(policy_sha256) if policy_sha256 else None,
        errors=[],
        blocked_reasons=blocked_reasons,
        next_commands=_next_commands(root, run_id=None, task_id=task.id, lease_id=lease.id if lease else None),
    )


def _artifact_projection(artifact: ArtifactRecord) -> CoreArtifactProjection:
    metadata = sanitize_for_logging(artifact.metadata)
    return CoreArtifactProjection(
        artifact_id=artifact.id,
        run_id=artifact.run_id,
        kind=artifact.kind,
        path=_safe_path(artifact.path),
        sha256=artifact.sha256,
        size_bytes=artifact.size_bytes,
        producer=artifact.producer,
        redaction_state=artifact.redaction_state,
        evidence_status=artifact.evidence_status,
        created_at=artifact.created_at,
        metadata=metadata if isinstance(metadata, dict) else {},
    )


def _event_projection(event: EventRecord) -> CoreEventProjection:
    payload = sanitize_for_logging(event.payload)
    return CoreEventProjection(
        event_id=event.id,
        run_id=event.run_id,
        task_id=event.task_id,
        seq=event.seq,
        event_type=event.event_type,
        level=event.level,
        message=str(sanitize_for_logging(event.message)),
        visibility=event.visibility.value,
        redaction_state=event.redaction_state.value,
        created_at=event.created_at,
        payload=payload if isinstance(payload, dict) else {},
    )


def _task_projection(task: TaskRecord) -> CoreTaskProjection:
    return CoreTaskProjection(
        task_id=task.id,
        objective_id=task.objective_id,
        run_id=task.run_id,
        adapter_id=_adapter_id(task),
        task_type=_task_type(task),
        status=task.status.value,
        approval_id=None,
        required_approvals=list(task.required_approvals),
        approval_state=task.approval_state,
    )


def _safe_get_task(store: SQLiteStore, task_id: str | None) -> TaskRecord | None:
    if task_id is None:
        return None
    try:
        return store.get_task(task_id)
    except KeyError:
        return None


def _store_for_projection(project_root: Path) -> SQLiteStore:
    root = Path(project_root).resolve()
    db_path = root / HARNESS_DIR / "harness.sqlite"
    if not db_path.exists():
        raise KeyError(f"Project state not initialized: {root}")
    return SQLiteStore(root)


def _latest_lease(store: SQLiteStore, task_id: str | None) -> TaskLease | None:
    if task_id is None:
        return None
    try:
        leases = store.list_task_leases(task_id)
    except KeyError:
        return None
    return leases[-1] if leases else None


def _adapter_id(task: TaskRecord | None) -> str | None:
    if task is None:
        return None
    value = task.metadata.get("execution_adapter")
    return str(value) if value else None


def _task_type(task: TaskRecord | None) -> str | None:
    if task is None:
        return None
    value = task.metadata.get("task_type")
    return str(value) if value else None


def _decision_for_run(run: RunRecord) -> str:
    if run.status == "completed" and run.task_type == "phase_1a_test":
        return "dry_run_no_tool_execution"
    if run.status.startswith("completed"):
        return f"{run.task_type or 'run'}_completed"
    if run.status in {"failed", "rejected", "blocked"}:
        return f"{run.task_type or 'run'}_{run.status}"
    return run.status


def _run_errors(run: RunRecord) -> list[str]:
    if run.status in {"completed", "completed_applied", "completed_denied", "completed_no_changes"}:
        return []
    return [f"Run status is {run.status}."]


def _blocked_reasons_for_run(
    store: SQLiteStore,
    run: RunRecord,
    task: TaskRecord | None,
    lease: TaskLease | None,
) -> list[str]:
    reasons: list[str] = []
    for event in store.list_events(run.id):
        payload = event.payload
        if event.event_type in {"execution_adapter_rejected", "run_blocked", "blocked"}:
            reasons.extend(str(item) for item in payload.get("rejection_reasons", []))
            if payload.get("reason"):
                reasons.append(str(payload["reason"]))
        if payload.get("blocked_reasons"):
            reasons.extend(str(item) for item in payload["blocked_reasons"])
    if task is not None and task.required_approvals and run.status not in {"completed", "completed_applied"}:
        reasons.extend(f"Missing required approval: {approval}." for approval in task.required_approvals)
    daemon_metadata = _latest_daemon_rejection_metadata(store, task.id if task is not None else None, lease.id if lease else None)
    reasons.extend(str(item) for item in daemon_metadata.get("rejection_reasons", []))
    return _dedupe(sanitize_for_logging(reasons))


def _latest_daemon_rejection_metadata(
    store: SQLiteStore,
    task_id: str | None,
    lease_id: str | None,
) -> dict[str, Any]:
    for event in store.list_daemon_events(limit=100):
        metadata = event.metadata
        if task_id is not None and metadata.get("task_id") != task_id:
            continue
        if lease_id is not None and metadata.get("lease_id") != lease_id:
            continue
        if event.event_type != "execution_adapter_rejected":
            continue
        sanitized = sanitize_for_logging(metadata)
        if not isinstance(sanitized, dict):
            return {}
        return {**sanitized, "event_type": event.event_type}
    return {}


def _policy_sha256_from_events(store: SQLiteStore, run_id: str) -> str | None:
    for event in reversed(store.list_events(run_id)):
        value = event.payload.get("policy_sha256")
        if value:
            return str(sanitize_for_logging(str(value)))
    return None


def _manifest_path(project_root: Path, run_id: str) -> str | None:
    path = Path(project_root).resolve() / ".harness" / "runs" / run_id / "manifest.json"
    return _safe_path(path) if path.exists() else None


def _safe_path(path: Path) -> str:
    resolved = Path(path)
    if is_secret_path(resolved):
        return "[REDACTED_SECRET_PATH]"
    return str(resolved)


def _next_commands(project_root: Path, *, run_id: str | None, task_id: str | None, lease_id: str | None) -> list[str]:
    project = str(Path(project_root).resolve())
    commands: list[str] = []
    if run_id is not None:
        commands.extend(
            [
                f"harness core inspect-run {run_id} --project {project} --output json",
                f"harness events {run_id} --project {project} --jsonl",
                f"harness artifacts list {run_id} --project {project} --output json",
            ]
        )
    if task_id is not None:
        commands.append(f"harness tasks inspect {task_id} --project {project} --output json")
    if lease_id is not None:
        commands.append(f"harness daemon inspect-lease {lease_id} --project {project} --output json")
    return commands


def _dedupe(values: Any) -> list[str]:
    result: list[str] = []
    for item in values or []:
        text = str(item)
        if text and text not in result:
            result.append(text)
    return result
