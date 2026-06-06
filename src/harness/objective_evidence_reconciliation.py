from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from harness.config import HARNESS_DIR
from harness.events import json_default
from harness.memory.sqlite_store import SQLiteStore
from harness.models import utc_now
from harness.objective_evidence import verify_objective_evidence
from harness.objective_runner import OBJECTIVE_RUNNER_EVENT_SCHEMA_VERSION
from harness.paths import resolve_project_root
from harness.security import sanitize_for_logging


OBJECTIVE_EVIDENCE_RECONCILIATION_SCHEMA_VERSION = "harness.objective_evidence_reconciliation/v1"
OBJECTIVE_EVIDENCE_RECONCILIATION_PROFILE = "evidence_reconciliation"

ReconciliationStatus = Literal["reconciled", "would_reconcile", "already_exists", "no_run_evidence"]


class ObjectiveEvidenceReconciliation(BaseModel):
    schema_version: str = OBJECTIVE_EVIDENCE_RECONCILIATION_SCHEMA_VERSION
    ok: bool
    status: ReconciliationStatus
    project_root: Path
    objective_id: str
    evidence_path: Path
    run_ids: list[str] = Field(default_factory=list)
    events_written: int = 0
    verification_ok: bool | None = None
    verification_summary: dict[str, int] | None = None
    message: str
    dry_run: bool = False
    mutation_scope: str = "objective_evidence_jsonl_only"
    existing_runs_mutated: bool = False
    tasks_mutated: bool = False
    sessions_mutated: bool = False
    artifacts_mutated: bool = False
    run_records_mutated: bool = False
    process_started: bool = False
    provider_called: bool = False
    network_called: bool = False
    filesystem_modified: bool = False
    permission_granting: bool = False


def reconcile_objective_evidence(
    project_root: Path,
    objective_id: str,
    *,
    actor: str = "operator",
    dry_run: bool = False,
) -> ObjectiveEvidenceReconciliation:
    project_root = resolve_project_root(project_root)
    store = SQLiteStore(project_root)
    objective = store.get_objective(objective_id)
    evidence_path = project_root / HARNESS_DIR / "autonomy" / "objectives" / f"{objective.id}.jsonl"
    runs = sorted(
        [run for run in store.list_runs() if run.objective_id == objective.id],
        key=lambda run: (run.created_at, run.id),
    )
    run_ids = [run.id for run in runs]
    base = {
        "project_root": project_root,
        "objective_id": objective.id,
        "evidence_path": evidence_path,
        "run_ids": run_ids,
        "dry_run": dry_run,
        "filesystem_modified": False,
    }
    if evidence_path.exists():
        verification = verify_objective_evidence(project_root, objective.id)
        return ObjectiveEvidenceReconciliation(
            **base,
            ok=verification.ok,
            status="already_exists",
            events_written=0,
            verification_ok=verification.ok,
            verification_summary=verification.summary,
            message="Objective evidence JSONL already exists; no reconciliation was written.",
        )
    if not runs:
        return ObjectiveEvidenceReconciliation(
            **base,
            ok=False,
            status="no_run_evidence",
            events_written=0,
            message="Objective has no persisted run evidence to reconcile.",
        )
    if dry_run:
        return ObjectiveEvidenceReconciliation(
            **base,
            ok=True,
            status="would_reconcile",
            events_written=len(runs) + 2,
            message="Objective run evidence can be reconciled into a new objective JSONL chain.",
        )

    objective_run_id = f"objrecon_{uuid.uuid4().hex[:12]}"
    event_payloads: list[tuple[str, dict[str, Any]]] = [
        (
            "started",
            {
                "autonomy_profile_id": OBJECTIVE_EVIDENCE_RECONCILIATION_PROFILE,
                "budget": {
                    "max_adapter_dispatches": 0,
                    "max_parallel": 0,
                    "source": "objective_evidence_reconciliation",
                },
                "reconciliation_actor": str(sanitize_for_logging(actor)).strip() or "operator",
            },
        )
    ]
    for run in runs:
        artifacts = store.list_artifacts(run.id)
        run_events = store.list_events(run.id)
        event_payloads.append(
            (
                "reconciled_existing_run",
                {
                    "reconciliation_source": "persisted_run_records",
                    "run_id": run.id,
                    "run_status": run.status,
                    "task_id": run.task_id,
                    "task_type": run.task_type,
                    "artifact_ids": [artifact.id for artifact in artifacts],
                    "run_event_count": len(run_events),
                    "run_created_at": run.created_at.isoformat(),
                    "run_updated_at": run.updated_at.isoformat(),
                },
            )
        )
    tasks = store.list_tasks(objective_id=objective.id)
    event_payloads.append(
        (
            "stopped",
            {
                "ok": False,
                "autonomy_profile_id": OBJECTIVE_EVIDENCE_RECONCILIATION_PROFILE,
                "scheduler_mode": "reconciliation",
                "stop_reason": "reconciled_existing_evidence",
                "steps": 0,
                "batches": 0,
                "max_parallel": 0,
                "adapter_dispatches": 0,
                "new_tasks_created": 0,
                "consecutive_failures": 0,
                "step_results": [],
                "final_task_statuses": {task.id: task.status.value for task in tasks},
                "pause_reasons": [],
                "errors": [],
                "reconciled_run_ids": run_ids,
                "reconciled_run_count": len(run_ids),
            },
        )
    )
    try:
        _write_reconciliation_events_atomically(
            evidence_path,
            _build_reconciliation_events(objective.id, objective_run_id, event_payloads),
        )
    except FileExistsError:
        verification = verify_objective_evidence(project_root, objective.id)
        return ObjectiveEvidenceReconciliation(
            **base,
            ok=verification.ok,
            status="already_exists",
            events_written=0,
            verification_ok=verification.ok,
            verification_summary=verification.summary,
            message="Objective evidence JSONL already exists; no reconciliation was written.",
        )
    verification = verify_objective_evidence(project_root, objective.id)
    return ObjectiveEvidenceReconciliation(
        **{**base, "filesystem_modified": True},
        ok=verification.ok,
        status="reconciled",
        events_written=len(runs) + 2,
        verification_ok=verification.ok,
        verification_summary=verification.summary,
        message="Objective run evidence was reconciled into a new objective JSONL chain.",
    )


def _build_reconciliation_events(
    objective_id: str,
    objective_run_id: str,
    event_payloads: list[tuple[str, dict[str, Any]]],
) -> list[dict[str, Any]]:
    previous_event_sha256: str | None = None
    events: list[dict[str, Any]] = []
    for event_index, (event_name, payload) in enumerate(event_payloads, start=1):
        event_record = {
            "schema_version": OBJECTIVE_RUNNER_EVENT_SCHEMA_VERSION,
            **sanitize_for_logging(payload),
            "objective_id": objective_id,
            "objective_run_id": objective_run_id,
            "objective_event_id": f"oevt_{uuid.uuid4().hex[:12]}",
            "event_index": event_index,
            "event": event_name,
            "created_at": utc_now().isoformat(),
            "previous_event_sha256": previous_event_sha256,
        }
        event_record["event_sha256"] = _objective_event_sha256(event_record)
        previous_event_sha256 = event_record["event_sha256"]
        events.append(event_record)
    return events


def _write_reconciliation_events_atomically(path: Path, events: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temp_path.open("x", encoding="utf-8") as handle:
            for event in events:
                handle.write(json.dumps(event, default=json_default, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temp_path, path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise
    else:
        temp_path.unlink(missing_ok=True)


def _objective_event_sha256(event: dict[str, Any]) -> str:
    stable = {key: value for key, value in event.items() if key != "event_sha256"}
    return hashlib.sha256(json.dumps(stable, sort_keys=True, default=str).encode("utf-8")).hexdigest()
