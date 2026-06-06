from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from harness.config import HARNESS_DIR
from harness.memory.sqlite_store import DAEMON_TASK_PAUSE_DECISIONS, SQLiteStore
from harness.models import ArtifactRecord, EventRecord, RunRecord, TaskLease, TaskRecord
from harness.policy import effective_policy_sha256, resolve_run_effective_policy
from harness.security import is_secret_path, sanitize_for_logging


CORE_RUN_PROJECTION_SCHEMA_VERSION = "harness.core_run_projection/v1"
CORE_EVIDENCE_BUNDLE_PROJECTION_SCHEMA_VERSION = "harness.core_evidence_bundle_projection/v1"
CORE_EVENT_PROJECTION_SCHEMA_VERSION = "harness.core_event_projection/v1"
CORE_RUN_EVENTS_PROJECTION_SCHEMA_VERSION = "harness.core_run_events_projection/v1"
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
    kind: str
    event_type: str
    level: str
    message: str
    visibility: str
    redaction_state: str
    created_at: datetime
    payload: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class CoreRunEventsProjection(BaseModel):
    schema_version: str = CORE_RUN_EVENTS_PROJECTION_SCHEMA_VERSION
    ok: bool
    run_id: str
    project_root: Path
    events: list[CoreEventProjection] = Field(default_factory=list)
    event_count: int = 0
    next_commands: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class CoreTaskProjection(BaseModel):
    schema_version: str = CORE_TASK_PROJECTION_SCHEMA_VERSION
    ok: bool = True
    task_id: str
    objective_id: str | None = None
    lease_id: str | None = None
    run_id: str | None = None
    adapter_id: str | None = None
    task_type: str | None = None
    status: str
    decision: str
    manifest: str | None = None
    artifact_ids: list[str] = Field(default_factory=list)
    approval_id: str | None = None
    policy_sha256: str | None = None
    required_approvals: list[str] = Field(default_factory=list)
    approval_state: str | None = None
    errors: list[str] = Field(default_factory=list)
    blocked_reasons: list[str] = Field(default_factory=list)
    next_commands: list[str] = Field(default_factory=list)


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


class CoreEvidenceBundleProjection(BaseModel):
    schema_version: str = CORE_EVIDENCE_BUNDLE_PROJECTION_SCHEMA_VERSION
    ok: bool
    project_root: Path
    run_id: str | None = None
    task_id: str | None = None
    mode: str | None = None
    decision: str
    status: str
    run: CoreRunProjection | None = None
    task: CoreTaskProjection | None = None
    blocked_state: CoreBlockedStateProjection | None = None
    events: CoreRunEventsProjection | None = None
    artifacts: list[CoreArtifactProjection] = Field(default_factory=list)
    manifest: str | None = None
    errors: list[str] = Field(default_factory=list)
    next_commands: list[str] = Field(default_factory=list)


def build_core_run_projection(project_root: Path, run_id: str) -> CoreRunProjection:
    root = Path(project_root).resolve()
    store = _store_for_projection(root)
    run = store.get_run(run_id)
    task = _safe_get_task(store, run.task_id)
    lease = _latest_lease(store, task.id if task is not None else run.task_id)
    artifacts = [_artifact_projection(artifact) for artifact in store.list_artifacts(run.id)]
    errors = _run_errors(run)
    blocked_reasons = _blocked_reasons_for_run(store, run, task, lease)
    adapter_id = _adapter_id(task)
    policy_sha256 = _policy_sha256_for_run(store, run)
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
        approval_id=run.approval_id,
        policy_sha256=policy_sha256,
        errors=errors,
        blocked_reasons=blocked_reasons,
        next_commands=_next_commands(root, run_id=run.id, task_id=run.task_id, lease_id=lease.id if lease else None),
        task=_task_projection(task) if task is not None else None,
    )


def build_core_evidence_bundle(
    project_root: Path,
    *,
    run_id: str | None = None,
    task_id: str | None = None,
) -> CoreEvidenceBundleProjection:
    root = Path(project_root).resolve()
    if bool(run_id) == bool(task_id):
        raise ValueError("Exactly one of run_id or task_id is required.")
    if run_id is not None:
        return _evidence_bundle_for_run(root, run_id)
    return _evidence_bundle_for_task(root, str(task_id))


def list_core_run_events(project_root: Path, run_id: str) -> list[CoreEventProjection]:
    root = Path(project_root).resolve()
    store = _store_for_projection(root)
    store.get_run(run_id)
    return [_event_projection(event) for event in store.list_events(run_id)]


def build_core_run_events_projection(project_root: Path, run_id: str) -> CoreRunEventsProjection:
    root = Path(project_root).resolve()
    events = list_core_run_events(root, run_id)
    return CoreRunEventsProjection(
        ok=True,
        run_id=run_id,
        project_root=root,
        events=events,
        event_count=len(events),
        next_commands=_next_commands(root, run_id=run_id, task_id=_task_id_from_events(events), lease_id=None),
        errors=[],
    )


def _evidence_bundle_for_run(project_root: Path, run_id: str) -> CoreEvidenceBundleProjection:
    run_projection = build_core_run_projection(project_root, run_id)
    task_projection = build_core_task_projection(project_root, run_projection.task_id) if run_projection.task_id else None
    events_projection = build_core_run_events_projection(project_root, run_id)
    commands = _bundle_next_commands(
        Path(project_root).resolve(),
        run_id=run_projection.run_id,
        task_id=run_projection.task_id,
        lease_id=run_projection.lease_id,
    )
    return CoreEvidenceBundleProjection(
        ok=run_projection.ok and (task_projection.ok if task_projection is not None else True) and events_projection.ok,
        project_root=Path(project_root).resolve(),
        run_id=run_projection.run_id,
        task_id=run_projection.task_id,
        mode=_mode_from_projection(run_projection.adapter_id, run_projection.task_type),
        decision=run_projection.decision,
        status=run_projection.status,
        run=run_projection,
        task=task_projection,
        blocked_state=None,
        events=events_projection,
        artifacts=list(run_projection.artifacts),
        manifest=run_projection.manifest,
        errors=[*run_projection.errors, *(task_projection.errors if task_projection is not None else []), *events_projection.errors],
        next_commands=commands,
    )


def _evidence_bundle_for_task(project_root: Path, task_id: str) -> CoreEvidenceBundleProjection:
    task_projection = build_core_task_projection(project_root, task_id)
    if task_projection.run_id is not None:
        run_projection = build_core_run_projection(project_root, task_projection.run_id)
        events_projection = build_core_run_events_projection(project_root, task_projection.run_id)
        commands = _bundle_next_commands(
            Path(project_root).resolve(),
            run_id=task_projection.run_id,
            task_id=task_projection.task_id,
            lease_id=task_projection.lease_id,
        )
        return CoreEvidenceBundleProjection(
            ok=task_projection.ok and run_projection.ok and events_projection.ok,
            project_root=Path(project_root).resolve(),
            run_id=task_projection.run_id,
            task_id=task_projection.task_id,
            mode=_mode_from_projection(task_projection.adapter_id, task_projection.task_type),
            decision=run_projection.decision,
            status=task_projection.status,
            run=run_projection,
            task=task_projection,
            blocked_state=None,
            events=events_projection,
            artifacts=list(run_projection.artifacts),
            manifest=run_projection.manifest,
            errors=[*task_projection.errors, *run_projection.errors, *events_projection.errors],
            next_commands=commands,
        )
    blocked_projection = build_core_blocked_state_projection(project_root, task_id)
    if not blocked_projection.blocked_reasons:
        raise ValueError(f"Task has no blocked state and no run evidence: {task_id}")
    commands = _bundle_next_commands(
        Path(project_root).resolve(),
        run_id=None,
        task_id=task_projection.task_id,
        lease_id=blocked_projection.lease_id or task_projection.lease_id,
    )
    return CoreEvidenceBundleProjection(
        ok=False,
        project_root=Path(project_root).resolve(),
        run_id=None,
        task_id=task_projection.task_id,
        mode=_mode_from_projection(task_projection.adapter_id, task_projection.task_type),
        decision=blocked_projection.decision,
        status=blocked_projection.status or task_projection.status,
        run=None,
        task=task_projection,
        blocked_state=blocked_projection,
        events=None,
        artifacts=[],
        manifest=None,
        errors=[*task_projection.errors, *blocked_projection.errors],
        next_commands=commands,
    )


def build_core_task_projection(project_root: Path, task_id: str) -> CoreTaskProjection:
    root = Path(project_root).resolve()
    store = _store_for_projection(root)
    task = store.get_task(task_id)
    lease = _latest_lease(store, task.id)
    run = _safe_get_run(store, task.run_id)
    manifest_path: str | None = None
    artifact_ids: list[str] = []
    policy_sha256: str | None = None
    approval_id: str | None = None
    if run is not None:
        manifest_path = _manifest_path(root, run.id)
        artifact_ids = [artifact.id for artifact in store.list_artifacts(run.id)]
        policy_sha256 = _policy_sha256_for_run(store, run)
        approval_id = run.approval_id
    return CoreTaskProjection(
        ok=True,
        task_id=task.id,
        objective_id=task.objective_id,
        lease_id=lease.id if lease is not None else None,
        run_id=task.run_id,
        adapter_id=_adapter_id(task),
        task_type=_task_type(task),
        status=task.status.value,
        decision=_decision_for_task(task, run),
        manifest=manifest_path,
        artifact_ids=artifact_ids,
        approval_id=approval_id,
        policy_sha256=policy_sha256,
        required_approvals=list(task.required_approvals),
        approval_state=task.approval_state,
        errors=[],
        blocked_reasons=[],
        next_commands=_next_commands(root, run_id=task.run_id, task_id=task.id, lease_id=lease.id if lease else None),
    )


def build_core_blocked_state_projection(project_root: Path, task_id: str) -> CoreBlockedStateProjection:
    root = Path(project_root).resolve()
    store = _store_for_projection(root)
    task = store.get_task(task_id)
    lease = _latest_lease(store, task.id)
    daemon_metadata = _latest_daemon_rejection_metadata(store, task.id, lease.id if lease is not None else None)
    eligibility = _daemon_eligibility_projection(store, task)
    blocked_reasons = _dedupe(
        [
            *[f"Missing required approval: {approval}." for approval in task.required_approvals],
            *[str(item) for item in daemon_metadata.get("rejection_reasons", [])],
            *_blocked_reasons_from_daemon_eligibility(eligibility),
        ]
    )
    policy_sha256 = daemon_metadata.get("policy_sha256") or eligibility.get("effective_policy_sha256")
    decision = _blocked_decision_from_metadata_or_eligibility(daemon_metadata, eligibility)
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


def _daemon_eligibility_projection(store: SQLiteStore, task: TaskRecord) -> dict[str, Any]:
    try:
        with store.connect() as conn:
            eligibility = store.daemon_task_eligibility(task, conn=conn)
    except Exception:
        return {}
    sanitized = sanitize_for_logging(eligibility)
    return sanitized if isinstance(sanitized, dict) else {}


def _blocked_decision_from_metadata_or_eligibility(
    daemon_metadata: dict[str, Any],
    eligibility: dict[str, Any],
) -> str:
    if daemon_metadata:
        return str(daemon_metadata.get("decision") or daemon_metadata.get("event_type") or "blocked")
    eligibility_decision = str(eligibility.get("decision") or "")
    if eligibility_decision == "waiting_approval":
        return "approval_required"
    if eligibility_decision in DAEMON_TASK_PAUSE_DECISIONS:
        return eligibility_decision
    return "blocked"


def _blocked_reasons_from_daemon_eligibility(eligibility: dict[str, Any]) -> list[str]:
    if not eligibility or eligibility.get("decision") not in DAEMON_TASK_PAUSE_DECISIONS:
        return []
    reasons: list[str] = []
    reason = eligibility.get("reason")
    if reason:
        reasons.append(str(reason))
    for approval in eligibility.get("missing_approvals") or []:
        reasons.append(f"Missing required approval: {approval}.")
    if eligibility.get("decision") and eligibility.get("adapter_id"):
        detail_parts = [
            f"decision={eligibility.get('decision')}",
            f"adapter={eligibility.get('adapter_id')}",
        ]
        if eligibility.get("task_type"):
            detail_parts.append(f"task_type={eligibility.get('task_type')}")
        missing = eligibility.get("missing_approvals") or []
        if missing:
            detail_parts.append("missing_approvals=" + ",".join(str(item) for item in missing))
        reasons.append("Daemon eligibility blocked task before lease: " + "; ".join(detail_parts) + ".")
    return _dedupe(sanitize_for_logging(reasons))


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
    metadata = payload if isinstance(payload, dict) else {}
    return CoreEventProjection(
        event_id=event.id,
        run_id=event.run_id,
        task_id=event.task_id,
        seq=event.seq,
        kind=event.event_type,
        event_type=event.event_type,
        level=event.level,
        message=str(sanitize_for_logging(event.message)),
        visibility=event.visibility.value,
        redaction_state=event.redaction_state.value,
        created_at=event.created_at,
        payload=metadata,
        metadata=metadata,
    )


def _task_projection(task: TaskRecord) -> CoreTaskProjection:
    return CoreTaskProjection(
        task_id=task.id,
        objective_id=task.objective_id,
        lease_id=None,
        run_id=task.run_id,
        adapter_id=_adapter_id(task),
        task_type=_task_type(task),
        status=task.status.value,
        decision=_decision_for_task(task, None),
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


def _safe_get_run(store: SQLiteStore, run_id: str | None) -> RunRecord | None:
    if run_id is None:
        return None
    try:
        return store.get_run(run_id)
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


def _decision_for_task(task: TaskRecord, run: RunRecord | None) -> str:
    if run is not None:
        return _decision_for_run(run)
    if task.status.value in {"blocked", "waiting_approval"}:
        return "blocked"
    if task.status.value == "succeeded":
        return "task_succeeded"
    return f"task_{task.status.value}"


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


def _policy_sha256_for_run(store: SQLiteStore, run: RunRecord) -> str:
    return _policy_sha256_from_events(store, run.id) or effective_policy_sha256(resolve_run_effective_policy(run, None))


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
        commands.append(f"harness core inspect-evidence --run {run_id} --project {project} --output json")
        commands.extend(
            [
                f"harness core inspect-run {run_id} --project {project} --output json",
                f"harness core inspect-events {run_id} --project {project} --output json",
                f"harness events {run_id} --project {project} --jsonl",
                f"harness artifacts list {run_id} --project {project} --output json",
            ]
        )
    if task_id is not None:
        commands.append(f"harness core inspect-evidence --task {task_id} --project {project} --output json")
        commands.append(f"harness core inspect-task {task_id} --project {project} --output json")
        commands.append(f"harness tasks inspect {task_id} --project {project} --output json")
    if lease_id is not None:
        commands.append(f"harness daemon inspect-lease {lease_id} --project {project} --output json")
    return commands


def _bundle_next_commands(project_root: Path, *, run_id: str | None, task_id: str | None, lease_id: str | None) -> list[str]:
    return _next_commands(project_root, run_id=run_id, task_id=task_id, lease_id=lease_id)


def _mode_from_projection(adapter_id: str | None, task_type: str | None) -> str | None:
    if adapter_id == "dry_run" or task_type == "phase_1a_test":
        return "dry_run"
    if adapter_id == "repo_planning" or task_type == "repo_planning":
        return "repo_planning"
    if adapter_id == "codex_isolated_edit" or task_type == "codex_code_edit":
        return "codex_isolated_edit"
    return adapter_id or task_type


def _task_id_from_events(events: list[CoreEventProjection]) -> str | None:
    for event in events:
        if event.task_id:
            return event.task_id
    return None


def _dedupe(values: Any) -> list[str]:
    result: list[str] = []
    for item in values or []:
        text = str(item)
        if text and text not in result:
            result.append(text)
    return result
