from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any


ACTIVE_RUN_REFERENCE_SCHEMA_VERSION = "harness.session_active_run_reference/v1"


def session_active_run_reference_projection(
    store: Any,
    session: Any,
    *,
    known_run_ids: set[str] | None = None,
    project_root: Path | None = None,
) -> dict[str, Any]:
    session_id = str(getattr(session, "id", "") or "")
    active_run_id = getattr(session, "active_run_id", None)
    base = {
        "schema_version": ACTIVE_RUN_REFERENCE_SCHEMA_VERSION,
        "session_id": session_id,
        "active_run_id": active_run_id,
        "status": "none",
        "ok": True,
        "present": False,
        "stale": False,
        "repairable": False,
        "repair_command": None,
        "repair_scope": "session_active_run_pointer_only",
        "process_started": False,
        "provider_called": False,
        "network_called": False,
        "filesystem_modified": False,
        "permission_granting": False,
    }
    if not active_run_id:
        return base

    run_record = None
    run_exists = False
    if known_run_ids is not None:
        run_exists = str(active_run_id) in known_run_ids
    else:
        try:
            run_record = store.get_run(str(active_run_id))
        except KeyError:
            run_exists = False
        else:
            run_exists = True

    if run_exists:
        projection = {
            **base,
            "status": "ok",
            "present": True,
            "run_status": getattr(run_record, "status", None),
            "run_task_id": getattr(run_record, "task_id", None),
        }
        return projection

    root = project_root or Path(getattr(store, "project_root", "."))
    return {
        **base,
        "status": "stale",
        "ok": False,
        "stale": True,
        "repairable": True,
        "missing_run_id": active_run_id,
        "repair_command": _doctor_repair_command(root),
    }


def active_run_reference_counts(
    store: Any,
    sessions: list[Any],
    *,
    known_run_ids: set[str] | None = None,
    project_root: Path | None = None,
) -> dict[str, int]:
    if known_run_ids is None:
        known_run_ids = {str(run.id) for run in store.list_runs()}
    projections = [
        session_active_run_reference_projection(
            store,
            session,
            known_run_ids=known_run_ids,
            project_root=project_root,
        )
        for session in sessions
    ]
    return {
        "active_run_refs": sum(1 for item in projections if item.get("active_run_id")),
        "valid_active_run_refs": sum(1 for item in projections if item.get("status") == "ok"),
        "stale_active_run_refs": sum(1 for item in projections if item.get("status") == "stale"),
    }


def _doctor_repair_command(project_root: Path) -> str:
    return f"harness doctor --repair --project {shlex.quote(str(project_root))} --output json"
