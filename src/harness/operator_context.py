from __future__ import annotations

import re
import sqlite3
import subprocess
from pathlib import Path

from harness import __version__
from harness.capabilities import build_capability_catalog
from harness.config import HARNESS_DIR, default_config, load_config
from harness.execution import list_execution_adapter_descriptors
from harness.memory.sqlite_store import (
    SESSION_SCHEMA_REPAIR_MESSAGE,
    SQLiteStore,
    is_missing_session_schema_error,
)
from harness.model_catalog import list_model_catalog, list_provider_catalog, validate_model_selection
from harness.models import EventStreamType, SessionPartKind, SessionPermissionStatus, SessionStatus, TaskStatus
from harness.operator_loop import session_operator_status_projection
from harness.paths import resolve_project_root
from harness.progress import build_orchestration_progress
from harness.security import sanitize_for_logging
from harness.session_cwd import session_cwd_payload
from harness.session_timeline import (
    list_session_timeline,
    list_session_transcript,
    render_timeline_event,
    render_transcript_entry,
)


OPERATOR_CONTEXT_SCHEMA_VERSION = "harness.operator_context/v1"


def build_operator_context(project_root: Path, *, selected_session_id: str | None = None) -> dict:
    project_root = resolve_project_root(project_root)
    initialized = _is_initialized(project_root)
    catalog_config, catalog_source, catalog_error = _load_catalog_config(project_root)
    provider_catalog = list_provider_catalog(catalog_config)
    model_catalog = list_model_catalog(catalog_config)
    capability_catalog = build_capability_catalog(project_root).model_dump(mode="json")
    dashboard = {
        "schema_version": OPERATOR_CONTEXT_SCHEMA_VERSION,
        "ok": True,
        "project_root": str(project_root),
        "initialized": initialized,
        "version": __version__,
        "branch": _git_branch(project_root),
        "summary": {
            "imported_agents": 0,
            "objectives": 0,
            "tasks_total": 0,
            "active_leases": 0,
            "active_daemons": 0,
            "recent_runs": 0,
            "recent_sessions": 0,
        },
        "task_status_counts": {status.value: 0 for status in TaskStatus},
        "agents": [],
        "tasks": [],
        "active_leases": [],
        "daemon": {
            "active_daemons": 0,
            "paused_tasks": 0,
            "latest_events": [],
        },
        "recent_runs": [],
        "recent_sessions": [],
        "session_pane": {
            "schema_version": "harness.session_pane/v1",
            "sessions": [],
            "selected_session_id": None,
            "filter": "open",
            "query": "",
            "counts": {"total": 0, "open": 0, "running": 0, "waiting_approval": 0, "archived": 0, "filtered": 0},
        },
        "active_session": None,
        "live_activity": _empty_live_activity("setup_needed" if not initialized else "idle"),
        "model_catalog": _model_catalog_projection(
            catalog_config,
            provider_catalog,
            model_catalog,
            sessions=[],
            source=catalog_source,
            config_error=catalog_error,
        ),
        "memory": {
            "schema_version": "harness.memory_summary/v1",
            "total": 0,
            "recent": [],
        },
        "progress": {
            "schema_version": "harness.orchestration_progress_summary/v1",
            "objective_id": None,
            "objective_title": None,
            "mode": "idle",
            "next_action": None,
            "active_lease_ids": [],
            "active_run_ids": [],
            "blocked_reasons": [],
            "tasks": [],
        },
        "terminal_tabs": {
            "schema_version": "harness.tui_terminal_tabs/v1",
            "tabs": [],
            "tab_count": 0,
            "terminal_tabs_supported": False,
            "policy_boundary": {
                "kind": "tui_terminal_panel_projection",
                "source": "persisted_pty_events",
                "process_start_allowed": False,
                "websocket_allowed": False,
                "live_stream_allowed": False,
                "artifact_content_read_allowed": False,
                "terminal_control_allowed": False,
                "requires_append_only_events": True,
                "bounded_preview_only": True,
            },
            "blocked_reasons": ["managed_pty_not_enabled", "terminal_panel_projection_disabled"],
            "source": "persisted_pty_events",
            "terminal_control_supported": False,
            "websocket_supported": False,
            "process_started": False,
            "websocket_opened": False,
            "live_stream_read": False,
            "artifact_contents_included": False,
            "permission_granting": False,
        },
        "registered_adapters": [
            descriptor.model_dump(mode="json")
            for descriptor in list_execution_adapter_descriptors()
        ],
        "capabilities": capability_catalog,
        "runtime_controls": {
            "schema_version": "harness.execution_controls_summary/v1",
            "controls": [],
            "breakers": [],
        },
        "command_suggestions": [
            f"harness home --project {project_root}",
            f"harness chat --project {project_root}",
            f"harness session list --project {project_root}",
            f"harness agents list --project {project_root}",
            f"harness tasks list --project {project_root}",
            f"harness daemon status --project {project_root}",
            f"harness runs --project {project_root}",
        ],
        "safety_boundaries": [
            "chat_is_operator_surface_not_authority",
            "passive_dashboard_context",
            "no_hidden_execution",
            "no_backend_preflight",
            "no_docker",
            "no_shell",
            "no_hosted_fallback",
            "no_paid_fallback",
            "no_openai_api_usage",
        ],
    }
    if not initialized:
        db_path = project_root / HARNESS_DIR / "harness.sqlite"
        if db_path.exists():
            dashboard["state_error"] = {
                "type": "OperationalError",
                "message": "Harness SQLite schema is missing required table: tasks",
            }
            guidance_id = "repair_project_state"
            description = "Repair or migrate local harness persistence for this project."
        else:
            guidance_id = "initialize_project"
            description = "Initialize local harness persistence for this project."
        dashboard["guidance"] = [
            {
                "id": guidance_id,
                "command": f"harness init --project {project_root}",
                "description": description,
            }
        ]
        return dashboard

    try:
        store = SQLiteStore.open_initialized(project_root)
        agents = store.list_project_agents()
        objectives = store.list_objectives()
        tasks = store.list_tasks()
        leases = store.list_task_leases()
        runs = store.list_runs()[:5]
        all_open_sessions = [
            session
            for session in store.list_sessions()
            if session.status != SessionStatus.ARCHIVED
        ]
        sessions = all_open_sessions[:5]
        selected_session = None
        if selected_session_id:
            try:
                selected_session = store.get_session(selected_session_id)
            except KeyError:
                selected_session = None
        memory_records = store.list_memory_records()[:5]
        cfg = catalog_config
        catalog_cache = store.replace_provider_model_catalog_cache(provider_catalog, model_catalog)
        daemon_status = store.daemon_status()
        controls = store.list_execution_controls()
        breakers = store.list_adapter_breaker_states(
            [descriptor.id for descriptor in list_execution_adapter_descriptors()]
        )
        terminal_tabs = _terminal_tabs_summary(store)
    except sqlite3.Error as exc:
        dashboard["initialized"] = False
        dashboard["live_activity"] = _empty_live_activity("blocked")
        message = SESSION_SCHEMA_REPAIR_MESSAGE if is_missing_session_schema_error(exc) else str(exc)
        dashboard["state_error"] = {
            "type": exc.__class__.__name__,
            "message": message,
        }
        dashboard["guidance"] = [
            {
                "id": "repair_project_state",
                "command": f"harness init --project {project_root}",
                "description": "Repair or migrate local harness persistence for this project.",
            }
        ]
        return dashboard

    active_leases = [lease for lease in leases if lease.status.value == "active"]
    task_status_counts = {status.value: 0 for status in TaskStatus}
    for task in tasks:
        task_status_counts[task.status.value] = task_status_counts.get(task.status.value, 0) + 1
    active_session_id = selected_session.id if selected_session is not None else (sessions[0].id if sessions else None)
    model_sessions = list(sessions)
    if selected_session is not None:
        model_sessions = [selected_session, *[session for session in model_sessions if session.id != selected_session.id]]

    dashboard["summary"] = {
        "imported_agents": len(agents),
        "objectives": len(objectives),
        "tasks_total": len(tasks),
        "active_leases": len(active_leases),
        "active_daemons": len(daemon_status.active_daemons),
        "recent_runs": len(runs),
        "recent_sessions": len(sessions),
    }
    dashboard["runtime_controls"] = {
        "schema_version": "harness.execution_controls_summary/v1",
        "controls": [control.model_dump(mode="json") for control in controls],
        "breakers": [breaker.model_dump(mode="json") for breaker in breakers],
    }
    dashboard["task_status_counts"] = task_status_counts
    dashboard["agents"] = [
        {
            "agent_id": agent.agent_id,
            "workbench_id": agent.workbench_id,
            "content_sha256": agent.content_sha256,
            "source_path": str(agent.source_path),
            "profiles": len(agent.profiles),
        }
        for agent in agents[:10]
    ]
    dashboard["tasks"] = [
        {
            "id": task.id,
            "title": sanitize_for_logging(task.title),
            "status": task.status.value,
            "priority": task.priority,
            "objective_id": task.objective_id,
            "agent_id": task.agent_id,
            "workbench_id": task.workbench_id,
            "execution_adapter": sanitize_for_logging(task.metadata.get("execution_adapter")),
            "task_type": sanitize_for_logging(task.metadata.get("task_type")),
        }
        for task in tasks[:10]
    ]
    dashboard["active_leases"] = [
        {
            "id": lease.id,
            "task_id": lease.task_id,
            "attempt_id": lease.attempt_id,
            "status": lease.status.value,
            "owner": lease.owner,
            "expires_at": lease.expires_at.isoformat(),
        }
        for lease in active_leases[:10]
    ]
    dashboard["recent_runs"] = [
        {
            "id": run.id,
            "status": run.status,
            "task_type": run.task_type,
            "goal": sanitize_for_logging(run.goal),
            "created_at": run.created_at.isoformat(),
        }
        for run in runs
    ]
    dashboard["recent_sessions"] = [
        {
            "id": session.id,
            "title": sanitize_for_logging(session.title),
            "display_title": _session_display_title(store, session),
            "status": session.status.value,
            "intent": sanitize_for_logging(session.intent),
            "agent_id": sanitize_for_logging(session.agent_id),
            "raw_model_ref": sanitize_for_logging(session.raw_model_ref),
            "active_run_id": session.active_run_id,
            "active_task_id": session.active_task_id,
            "cwd": sanitize_for_logging(session.metadata.get("cwd", ".")),
            "ui_preferences": sanitize_for_logging(session.ui_preferences),
            "updated_at": session.updated_at.isoformat(),
        }
        for session in sessions
    ]
    if active_session_id:
        dashboard["active_session"] = _session_preview(store, active_session_id, project_root)
    dashboard["session_pane"] = build_session_pane_projection(
        project_root,
        selected_session_id=active_session_id,
        status_filter="open",
        query="",
    )
    dashboard["model_catalog"] = _model_catalog_projection(
        cfg,
        provider_catalog,
        model_catalog,
        sessions=model_sessions,
        cache=catalog_cache,
        source=catalog_source,
        config_error=catalog_error,
    )
    dashboard["memory"] = {
        "schema_version": "harness.memory_summary/v1",
        "total": len(store.list_memory_records()),
        "warnings": ["memory_not_authority"] if memory_records else [],
        "recent": [
            {
                "id": record.id,
                "scope_type": record.scope_type.value,
                "scope_id": record.scope_id,
                "summary": sanitize_for_logging(record.summary),
                "redaction_state": record.redaction_state.value,
                "lineage": sanitize_for_logging(record.lineage),
                "created_at": record.created_at.isoformat(),
            }
            for record in memory_records
        ],
    }
    dashboard["terminal_tabs"] = terminal_tabs
    objective_for_progress = _objective_for_progress(objectives, tasks)
    if objective_for_progress is not None:
        try:
            progress = build_orchestration_progress(project_root, objective_for_progress.id)
            dashboard["progress"] = {
                "schema_version": "harness.orchestration_progress_summary/v1",
                "objective_id": progress.objective_id,
                "objective_title": progress.objective_title,
                "mode": progress.mode.value,
                "next_action": progress.next_action,
                "active_lease_ids": progress.active_lease_ids,
                "active_run_ids": progress.active_run_ids,
                "blocked_reasons": progress.blocked_reasons[:5],
                "tasks": [
                    {
                        "task_id": task.task_id,
                        "title": task.title,
                        "status": task.status.value,
                        "execution_adapter": task.execution_adapter,
                        "task_type": task.task_type,
                        "lease_id": task.lease_id,
                        "run_id": task.run_id,
                        "blocked_reasons": task.blocked_reasons[:3],
                        "blocked_state_explanations": [
                            explanation.model_dump(mode="json")
                            for explanation in task.blocked_state_explanations[:3]
                        ],
                        "next_action": task.next_action,
                    }
                    for task in progress.tasks[:5]
                ],
            }
        except (KeyError, sqlite3.Error):
            pass
    dashboard["daemon"] = {
        "active_daemons": len(daemon_status.active_daemons),
        "paused_tasks": len(daemon_status.paused_tasks),
        "latest_events": [
            {
                "id": event.id,
                "daemon_id": event.daemon_id,
                "event_type": event.event_type,
                "message": sanitize_for_logging(event.message),
                "created_at": event.created_at.isoformat(),
            }
            for event in daemon_status.latest_events[:5]
        ],
    }
    dashboard["guidance"] = []
    if task_status_counts.get("ready", 0) > 0 and not active_leases:
        ready_repo_planning = next(
            (
                task
                for task in tasks
                if task.status.value == "ready" and task.metadata.get("execution_adapter") == "repo_planning"
            ),
            None,
        )
        if ready_repo_planning is not None:
            dashboard["guidance"].append(
                {
                    "id": "lease_repo_planning_task",
                    "command": f"harness daemon run-once --project {project_root}",
                    "description": "Lease the ready repo-planning task, then dispatch the resulting lease through registered daemon execute.",
                }
            )
        else:
            dashboard["guidance"].append(
                {
                    "id": "lease_ready_task",
                    "command": f"harness daemon run-once --project {project_root}",
                    "description": "Lease the highest-priority eligible task without executing it.",
                }
            )
    if active_leases:
        lease = active_leases[0]
        dashboard["guidance"].append(
            {
                "id": "dispatch_active_lease",
                "command": f"harness daemon execute {lease.id} --project {project_root}",
                "description": "Dispatch the active lease through its registered adapter after inspection.",
            }
        )
    if not agents:
        dashboard["guidance"].append(
            {
                "id": "author_agent",
                "command": "harness agents scaffold my_agent --workbench quant --kind specialist "
                "--parent quant_research --model-profile codex_supervised --tool-policy read_only "
                "--memory-scope quant --output agents/my_agent",
                "description": "Scaffold a declarative custom agent bundle.",
            }
        )
    dashboard["live_activity"] = _build_live_activity_projection(
        store,
        dashboard,
        recent_runs=runs,
        active_session_id=active_session_id,
    )
    return dashboard


def _live_activity_policy_boundary() -> dict:
    return {
        "kind": "tui_live_activity_projection",
        "source": "persisted_harness_state",
        "process_started": False,
        "filesystem_modified": False,
        "active_repo_modified": False,
        "provider_execution_started": False,
        "model_execution_started": False,
        "shell_started": False,
        "docker_started": False,
        "adapter_dispatched": False,
        "permission_granting": False,
        "authority_granting": False,
        "artifact_contents_included": False,
    }


def _empty_live_activity(active_signal: str = "idle") -> dict:
    return {
        "schema_version": "harness.tui_live_activity/v1",
        "active_signal": active_signal,
        "pending_permissions": [],
        "open_todos": [],
        "latest_events": [],
        "recent_artifacts": [],
        "counts": {
            "ready": 0,
            "running": 0,
            "blocked": 0,
            "waiting_approval": 0,
            "done": 0,
            "recent_sessions": 0,
            "recent_runs": 0,
            "recent_artifacts": 0,
            "pending_permissions": 0,
            "open_todos": 0,
        },
        "policy_boundary": _live_activity_policy_boundary(),
    }


def _build_live_activity_projection(
    store: SQLiteStore,
    dashboard: dict,
    *,
    recent_runs: list,
    active_session_id: str | None,
) -> dict:
    pending_permissions = _pending_permission_rows(store, active_session_id)
    open_todos = _open_todo_rows(store, active_session_id)
    latest_events = _latest_session_event_rows(store, active_session_id)
    recent_artifacts = _recent_artifact_rows(store, recent_runs)
    counts = _live_activity_counts(dashboard, pending_permissions, open_todos, recent_artifacts)
    return {
        "schema_version": "harness.tui_live_activity/v1",
        "active_signal": _live_activity_signal(dashboard, counts),
        "pending_permissions": pending_permissions,
        "open_todos": open_todos,
        "latest_events": latest_events,
        "recent_artifacts": recent_artifacts,
        "counts": counts,
        "policy_boundary": _live_activity_policy_boundary(),
    }


def _pending_permission_rows(store: SQLiteStore, session_id: str | None) -> list[dict]:
    if not session_id:
        return []
    try:
        permissions = store.list_session_permissions(session_id, SessionPermissionStatus.PENDING)
    except Exception:
        return []
    return [
        {
            "id": permission.id,
            "tool_id": sanitize_for_logging(permission.tool_id),
            "action": sanitize_for_logging(permission.normalized_action),
            "target": sanitize_for_logging(permission.normalized_target_pattern),
            "risk": sanitize_for_logging(permission.risk),
            "scope": permission.scope.value,
            "expires_at": permission.expires_at.isoformat(),
        }
        for permission in permissions[-5:]
    ]


def _open_todo_rows(store: SQLiteStore, session_id: str | None) -> list[dict]:
    if not session_id:
        return []
    try:
        todos = [
            todo
            for todo in store.list_session_todos(session_id)
            if todo.status in {"pending", "in_progress"}
        ]
    except Exception:
        return []
    return [
        {
            "id": todo.id,
            "content": sanitize_for_logging(todo.content),
            "status": todo.status,
            "priority": todo.priority,
            "updated_at": todo.updated_at.isoformat(),
        }
        for todo in todos[:5]
    ]


def _latest_session_event_rows(store: SQLiteStore, session_id: str | None) -> list[dict]:
    if not session_id:
        return []
    try:
        events = list_session_timeline(store, session_id, limit=6)
    except Exception:
        return []
    return [
        {
            "id": event.id,
            "seq": event.seq,
            "kind": sanitize_for_logging(event.kind),
            "line": render_timeline_event(event),
            "created_at": event.created_at.isoformat(),
        }
        for event in events
    ]


def _recent_artifact_rows(store: SQLiteStore, recent_runs: list) -> list[dict]:
    rows: list[dict] = []
    for run in recent_runs[:5]:
        try:
            artifacts = store.list_artifacts(run.id)
        except Exception:
            continue
        for artifact in artifacts[-3:]:
            rows.append(
                {
                    "id": artifact.id,
                    "run_id": artifact.run_id,
                    "kind": sanitize_for_logging(artifact.kind),
                    "path": sanitize_for_logging(artifact.path.name),
                    "redaction_state": sanitize_for_logging(artifact.redaction_state),
                    "evidence_status": sanitize_for_logging(artifact.evidence_status),
                    "size_bytes": artifact.size_bytes,
                    "created_at": artifact.created_at.isoformat(),
                }
            )
    return rows[:6]


def _live_activity_counts(
    dashboard: dict,
    pending_permissions: list[dict],
    open_todos: list[dict],
    recent_artifacts: list[dict],
) -> dict:
    task_counts = dashboard.get("task_status_counts") or {}
    active_session = dashboard.get("active_session") or {}
    operator = active_session.get("operator") or {}
    progress = dashboard.get("progress") or {}
    running = int(task_counts.get("leased", 0) or 0) + len(progress.get("active_run_ids") or [])
    if operator.get("phase") == "turn":
        running += 1
    return {
        "ready": int(task_counts.get("ready", 0) or 0),
        "running": running,
        "blocked": int(task_counts.get("blocked", 0) or 0),
        "waiting_approval": int(task_counts.get("waiting_approval", 0) or 0),
        "done": int(task_counts.get("succeeded", 0) or 0),
        "recent_sessions": int((dashboard.get("summary") or {}).get("recent_sessions", 0) or 0),
        "recent_runs": int((dashboard.get("summary") or {}).get("recent_runs", 0) or 0),
        "recent_artifacts": len(recent_artifacts),
        "pending_permissions": len(pending_permissions),
        "open_todos": len(open_todos),
    }


def _live_activity_signal(dashboard: dict, counts: dict) -> str:
    if not dashboard.get("initialized"):
        return "setup_needed"
    active_session = dashboard.get("active_session") or {}
    operator = active_session.get("operator") or {}
    progress = dashboard.get("progress") or {}
    if counts.get("pending_permissions") or counts.get("waiting_approval") or operator.get("waiting_approval_id"):
        return "approval_required"
    if operator.get("phase") == "turn":
        return "responding"
    if dashboard.get("active_leases") or progress.get("active_lease_ids") or progress.get("active_run_ids") or counts.get("running"):
        return "running"
    if counts.get("blocked") or progress.get("mode") == "blocked" or progress.get("blocked_reasons"):
        return "blocked"
    if counts.get("ready"):
        return "ready"
    return "idle"


def build_session_pane_projection(
    project_root: Path,
    *,
    selected_session_id: str | None = None,
    status_filter: str = "open",
    query: str = "",
) -> dict:
    project_root = resolve_project_root(project_root)
    if not _is_initialized(project_root):
        return {
            "schema_version": "harness.session_pane/v1",
            "sessions": [],
            "selected_session_id": None,
            "filter": status_filter if status_filter in {"open", "running", "archived", "all"} else "open",
            "query": query.strip(),
            "counts": {"total": 0, "open": 0, "running": 0, "waiting_approval": 0, "archived": 0, "filtered": 0},
            "ok": False,
        }
    store = SQLiteStore.open_initialized(project_root)
    all_sessions = store.list_sessions()
    normalized_filter = status_filter if status_filter in {"open", "running", "archived", "all"} else "open"
    normalized_query = query.strip().casefold()
    running_statuses = {SessionStatus.RUNNING, SessionStatus.WAITING_APPROVAL}
    counts = {
        "total": len(all_sessions),
        "open": sum(1 for session in all_sessions if session.status != SessionStatus.ARCHIVED),
        "running": sum(1 for session in all_sessions if session.status in running_statuses),
        "waiting_approval": sum(1 for session in all_sessions if session.status == SessionStatus.WAITING_APPROVAL),
        "archived": sum(1 for session in all_sessions if session.status == SessionStatus.ARCHIVED),
    }
    filtered = []
    for session in all_sessions:
        if normalized_filter == "open" and session.status == SessionStatus.ARCHIVED:
            continue
        if normalized_filter == "running" and session.status not in running_statuses:
            continue
        if normalized_filter == "archived" and session.status != SessionStatus.ARCHIVED:
            continue
        row = _session_pane_row(store, session)
        searchable = " ".join(
            str(row.get(key) or "")
            for key in ("id", "display_title", "status", "agent_id", "raw_model_ref", "cwd", "active_run_id", "active_task_id")
        ).casefold()
        if normalized_query and normalized_query not in searchable:
            continue
        filtered.append(row)
    selected_id = selected_session_id if any(row["id"] == selected_session_id for row in filtered) else None
    if selected_id is None and filtered:
        selected_id = filtered[0]["id"]
    return {
        "schema_version": "harness.session_pane/v1",
        "ok": True,
        "sessions": filtered,
        "selected_session_id": selected_id,
        "filter": normalized_filter,
        "query": query.strip(),
        "counts": {**counts, "filtered": len(filtered)},
        "policy_boundary": {
            "kind": "session_pane_projection",
            "process_started": False,
            "filesystem_modified": False,
            "active_repo_modified": False,
            "permission_granting": False,
        },
    }


def _session_pane_row(store: SQLiteStore, session) -> dict:
    timeline = list_session_timeline(store, session.id, limit=1)
    messages = store.list_session_messages(session.id)
    terminal = session.status in {
        SessionStatus.COMPLETED,
        SessionStatus.FAILED,
        SessionStatus.CANCELLED,
        SessionStatus.ARCHIVED,
    }
    return {
        "id": session.id,
        "display_title": _session_display_title(store, session),
        "title": sanitize_for_logging(session.title),
        "status": session.status.value,
        "agent_id": sanitize_for_logging(session.agent_id),
        "raw_model_ref": sanitize_for_logging(session.raw_model_ref),
        "cwd": sanitize_for_logging(session.metadata.get("cwd", ".")),
        "active_run_id": session.active_run_id,
        "active_task_id": session.active_task_id,
        "updated_at": session.updated_at.isoformat(),
        "latest_event": render_timeline_event(timeline[-1]) if timeline else None,
        "message_count": len(messages),
        "can_archive": session.status != SessionStatus.ARCHIVED,
        "can_restore": session.status == SessionStatus.ARCHIVED,
        "can_abort": session.status in {SessionStatus.RUNNING, SessionStatus.WAITING_APPROVAL},
        "can_hard_delete": True,
        "is_terminal": terminal,
    }


def _session_preview(store: SQLiteStore, session_id: str, project_root: Path) -> dict:
    session = store.get_session(session_id)
    timeline_events = list_session_timeline(store, session_id, limit=8)
    transcript_entries = list_session_transcript(store, session_id)[-4:]
    latest_ui_activation = next((event for event in reversed(timeline_events) if event.kind == "tui.ui_activation.applied"), None)
    try:
        cwd = session_cwd_payload(project_root, session.metadata, load_config(project_root).context_excludes)
    except Exception:
        cwd = {"cwd": session.metadata.get("cwd", "."), "resolved_abs_path": None}
    operator = session_operator_status_projection(
        store,
        session_id,
        project_root=project_root,
        cwd=str(cwd.get("cwd") or "."),
        active_tools=_operator_active_tools(),
    )
    return {
        "schema_version": "harness.session_preview/v1",
        "id": session.id,
        "status": session.status.value,
        "title": sanitize_for_logging(session.title),
        "display_title": _session_display_title(store, session),
        "agent_id": sanitize_for_logging(session.agent_id),
        "provider_id": sanitize_for_logging(session.provider_id),
        "model_id": sanitize_for_logging(session.model_id),
        "model_variant": sanitize_for_logging(session.model_variant),
        "raw_model_ref": sanitize_for_logging(session.raw_model_ref),
        "active_run_id": session.active_run_id,
        "cwd": cwd,
        "operator": operator,
        "ui_preferences": sanitize_for_logging(session.ui_preferences),
        "latest_ui_activation": _ui_activation_preview(latest_ui_activation) if latest_ui_activation else None,
        "composer_context": _session_composer_context(store, session_id, project_root),
        "timeline": [render_timeline_event(event) for event in timeline_events],
        "transcript": [render_transcript_entry(entry) for entry in transcript_entries],
    }


def _session_display_title(store: SQLiteStore, session) -> str:
    title = str(sanitize_for_logging(session.title) or "").strip()
    if title and not _is_generic_session_title(title):
        return _compact_session_topic(title)
    for message in store.list_session_messages(session.id):
        if message.role.value != "user":
            continue
        topic = _compact_session_topic(str(sanitize_for_logging(message.content_preview) or ""))
        if topic:
            return topic
    if isinstance(session.metadata, dict):
        metadata_goal = _compact_session_topic(str(sanitize_for_logging(session.metadata.get("initial_goal_preview")) or ""))
        if metadata_goal:
            return metadata_goal
    summary = _compact_session_topic(str(sanitize_for_logging(session.summary) or ""))
    if summary:
        return summary
    intent = _compact_session_topic(str(sanitize_for_logging(session.intent) or ""))
    if intent and intent != "session tool gateway":
        return intent
    return "Untitled session"


def _is_generic_session_title(title: str) -> bool:
    normalized = re.sub(r"[\W_]+", " ", title).strip().casefold()
    return normalized in {"", "harness chat", "chat", "session", "new session", "untitled", "untitled session"}


def _compact_session_topic(text: str, *, max_chars: int = 54) -> str:
    cleaned = re.sub(r"\s+", " ", text).strip(" \t\r\n\"'")
    if not cleaned:
        return ""
    cleaned = re.sub(r"^(please|can you|could you|would you|i want to|we want to)\s+", "", cleaned, flags=re.IGNORECASE)
    if not cleaned:
        return ""
    cleaned = cleaned[0].upper() + cleaned[1:]
    if len(cleaned) <= max_chars:
        return cleaned
    clipped = cleaned[: max_chars + 1].rsplit(" ", 1)[0].rstrip(" ,.;:")
    return (clipped or cleaned[:max_chars].rstrip()) + "..."


def _operator_active_tools() -> list[str]:
    from harness.session_tools import default_session_tool_descriptors

    return sorted(descriptor.id for descriptor in default_session_tool_descriptors() if descriptor.enabled)


def _session_composer_context(store: SQLiteStore, session_id: str, project_root: Path) -> dict:
    attachments: list[dict] = []
    for part in store.list_session_parts(session_id):
        if part.kind != SessionPartKind.ARTIFACT_REF:
            continue
        metadata = part.metadata or {}
        if metadata.get("attachment_kind") != "file_ref":
            continue
        requested_path = str(metadata.get("path") or "")
        resolved_path = Path(str(metadata.get("resolved_path") or ""))
        size_bytes = 0
        if resolved_path.is_file():
            try:
                size_bytes = resolved_path.stat().st_size
            except OSError:
                size_bytes = 0
        attachments.append(
            {
                "path": sanitize_for_logging(requested_path),
                "resolved_inside_project": _is_relative_to_project(project_root, resolved_path),
                "size_bytes": size_bytes,
                "estimated_tokens": _estimate_tokens_for_bytes(size_bytes),
                "contents_included": False,
            }
        )
    total_bytes = sum(int(item["size_bytes"]) for item in attachments)
    return {
        "schema_version": "harness.tui_composer_context/v1",
        "attachment_count": len(attachments),
        "attachments": attachments,
        "total_attachment_bytes": total_bytes,
        "total_estimated_tokens": _estimate_tokens_for_bytes(total_bytes),
        "contents_included": False,
        "process_started": False,
        "filesystem_modified": False,
        "permission_granting": False,
    }


def _estimate_tokens_for_bytes(size_bytes: int) -> int:
    return max(1, (size_bytes + 3) // 4) if size_bytes else 0


def _is_relative_to_project(project_root: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(project_root.resolve())
        return True
    except ValueError:
        return False


def _ui_activation_preview(event) -> dict:
    payload = event.payload or {}
    action = payload.get("action") or {}
    return {
        "seq": event.seq,
        "entry_id": sanitize_for_logging(payload.get("entry_id")),
        "source": sanitize_for_logging(payload.get("source")),
        "action_type": sanitize_for_logging(action.get("type")),
        "command_started": bool(payload.get("command_started")),
        "process_started": bool(payload.get("process_started")),
        "filesystem_modified": bool(payload.get("filesystem_modified")),
        "permission_granting": bool(payload.get("permission_granting")),
        "authority_granting": bool(payload.get("authority_granting")),
    }


def _load_catalog_config(project_root: Path):
    try:
        return load_config(project_root), "project_config", None
    except FileNotFoundError:
        return default_config(), "default_config", None
    except Exception as exc:
        return (
            default_config(),
            "default_config",
            {
                "type": exc.__class__.__name__,
                "message": sanitize_for_logging(str(exc)),
            },
        )


def _model_catalog_projection(
    cfg,
    provider_catalog: list,
    model_catalog: list,
    *,
    sessions: list,
    cache: dict | None = None,
    source: str = "project_config",
    config_error: dict | None = None,
) -> dict:
    projection = {
        "schema_version": "harness.operator_model_catalog/v1",
        "providers": [
            {
                "provider_id": provider.provider_id,
                "enabled": provider.enabled,
                "kind": provider.kind.value,
                "credential_status": provider.credential_status.value,
                "data_boundary": provider.metadata.data_boundary.value,
                "constraints": provider.constraints,
            }
            for provider in provider_catalog
        ],
        "models": [
            {
                "provider_id": model.provider_id,
                "model_id": sanitize_for_logging(model.model_id),
                "raw_model_ref": sanitize_for_logging(model.raw_model_ref),
                "model_profile_id": sanitize_for_logging(model.model_profile_id),
                "source": model.source,
                "context_limit": model.context_limit,
                "tool_support": model.tool_support,
                "reasoning_support": model.reasoning_support,
                "modalities": model.modalities,
            }
            for model in model_catalog
        ],
        "active_model": _active_model_summary(cfg, sessions, model_catalog),
        "source": source,
        "permission_granting": False,
        "no_hidden_fallback": True,
    }
    if cache is not None:
        projection["cache"] = cache
    if config_error is not None:
        projection["config_error"] = config_error
    return projection


def _terminal_tabs_summary(store: SQLiteStore) -> dict:
    stream_ids: list[str] = []
    with store.connect() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT stream_id
            FROM event_store
            WHERE stream_type = ? AND stream_id LIKE 'pty:%'
            ORDER BY stream_id ASC
            LIMIT 10
            """,
            (EventStreamType.SESSION.value,),
        ).fetchall()
    stream_ids = [str(row["stream_id"]) for row in rows]
    tabs = [_terminal_tab_from_events(store, stream_id.removeprefix("pty:")) for stream_id in stream_ids]
    blocked_reasons: list[str] = []
    for tab in tabs:
        for reason in tab["blocked_reasons"]:
            if reason not in blocked_reasons:
                blocked_reasons.append(reason)
    if not blocked_reasons:
        blocked_reasons = ["managed_pty_not_enabled", "terminal_panel_projection_disabled"]
    return {
        "schema_version": "harness.tui_terminal_tabs/v1",
        "tabs": tabs,
        "tab_count": len(tabs),
        "terminal_tabs_supported": False,
        "policy_boundary": {
            "kind": "tui_terminal_panel_projection",
            "source": "persisted_pty_events",
            "process_start_allowed": False,
            "websocket_allowed": False,
            "live_stream_allowed": False,
            "artifact_content_read_allowed": False,
            "terminal_control_allowed": False,
            "requires_append_only_events": True,
            "bounded_preview_only": True,
        },
        "blocked_reasons": blocked_reasons,
        "source": "persisted_pty_events",
        "terminal_control_supported": False,
        "websocket_supported": False,
        "process_started": False,
        "websocket_opened": False,
        "live_stream_read": False,
        "artifact_contents_included": False,
        "permission_granting": False,
    }


def _terminal_tab_from_events(store: SQLiteStore, pty_id: str) -> dict:
    events = store.list_store_events(EventStreamType.SESSION, f"pty:{pty_id}")
    created = next((event for event in events if event.kind == "pty.created"), None)
    updated = [event for event in events if event.kind == "pty.updated"]
    exited = next((event for event in reversed(events) if event.kind in {"pty.exited", "pty.deleted"}), None)
    output_events = [event for event in events if event.kind in {"pty.output", "pty.output.artifact"}]
    preview = "".join(str(event.payload.get("preview") or "") for event in output_events)
    if len(preview) > 16 * 1024:
        preview = preview[-16 * 1024:]
    latest_size = updated[-1].payload if updated else {}
    initial = created.payload if created else {}
    artifact_refs = sorted({ref for event in output_events for ref in event.artifact_refs})
    blocked_reasons = [
        "managed_pty_not_enabled",
        "terminal_output_restoration_not_enabled",
        "terminal_panel_projection_disabled",
        "terminal_control_disabled",
    ]
    if "pty.created" not in {event.kind for event in events} or not any(event.kind in {"pty.exited", "pty.deleted"} for event in events):
        blocked_reasons.append("missing_required_pty_events")
    return {
        "id": pty_id,
        "title": sanitize_for_logging(initial.get("title") or initial.get("command") or initial.get("shell") or pty_id),
        "status": "exited" if exited else "unavailable",
        "shell": sanitize_for_logging(initial.get("shell")),
        "command": sanitize_for_logging(initial.get("command")),
        "cwd": sanitize_for_logging(initial.get("cwd")),
        "cols": latest_size.get("cols") or initial.get("cols"),
        "rows": latest_size.get("rows") or initial.get("rows"),
        "event_count": len(events),
        "output_event_count": len(output_events),
        "artifact_ref_count": len(artifact_refs),
        "artifact_refs": artifact_refs,
        "scrollback_preview": sanitize_for_logging(preview),
        "restoration_ready": False,
        "policy_boundary": {
            "kind": "tui_terminal_tab_projection",
            "source": "persisted_pty_events",
            "process_start_allowed": False,
            "websocket_allowed": False,
            "live_stream_allowed": False,
            "artifact_content_read_allowed": False,
            "terminal_control_allowed": False,
            "requires_append_only_events": True,
            "bounded_preview_only": True,
        },
        "blocked_reasons": blocked_reasons,
        "source": "persisted_pty_events",
        "terminal_control_supported": False,
        "websocket_supported": False,
        "process_started": False,
        "websocket_opened": False,
        "live_stream_read": False,
        "artifact_contents_included": False,
        "permission_granting": False,
    }


def _active_model_summary(cfg, sessions: list, model_catalog: list) -> dict | None:
    if not sessions:
        return None
    latest = sessions[0]
    raw_ref = latest.raw_model_ref
    validation = validate_model_selection(cfg, raw_ref) if raw_ref else None
    matched = validation.matched_model if validation else None
    return {
        "session_id": latest.id,
        "raw_model_ref": sanitize_for_logging(raw_ref),
        "provider_id": sanitize_for_logging(latest.provider_id or (matched.provider_id if matched else None)),
        "model_id": sanitize_for_logging(latest.model_id or (matched.model_id if matched else None)),
        "model_variant": sanitize_for_logging(latest.model_variant),
        "known_catalog_entry": matched is not None,
        "executable": validation.executable if validation else None,
        "provider_known": validation.provider_known if validation else None,
        "provider_enabled": validation.provider_enabled if validation else None,
        "blocked_reasons": validation.blocked_reasons if validation else [],
        "validation": validation.model_dump(mode="json") if validation else None,
        "model_profile_id": sanitize_for_logging(matched.model_profile_id if matched else None),
        "tool_support": matched.tool_support if matched else None,
        "context_limit": matched.context_limit if matched else None,
        "provider_execution_started": False,
        "model_execution_started": False,
        "network_accessed": False,
        "hidden_provider_fallback": False,
        "hidden_model_fallback": False,
        "permission_granting": False,
        "authority_granting": False,
        "no_hidden_fallback": True,
    }


def _objective_for_progress(objectives: list, tasks: list) -> object | None:
    if not objectives:
        return None
    non_terminal_objective_ids = {
        task.objective_id
        for task in tasks
        if task.objective_id is not None
        and task.status
        not in {
            TaskStatus.SUCCEEDED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
            TaskStatus.SKIPPED,
        }
    }
    for objective in objectives:
        if objective.id in non_terminal_objective_ids:
            return objective
    return objectives[0]


def build_tui_dashboard(project_root: Path, *, selected_session_id: str | None = None) -> dict:
    context = build_operator_context(project_root, selected_session_id=selected_session_id)
    dashboard = dict(context)
    dashboard["schema_version"] = "harness.tui_dashboard/v1"
    dashboard["safety_boundaries"] = [
        "read_only_tui",
        "passive_dashboard_context",
        "no_hidden_execution",
        "no_backend_preflight",
        "no_docker",
        "no_shell",
        "no_hosted_fallback",
        "no_paid_fallback",
        "no_openai_api_usage",
    ]
    return dashboard


def render_operator_context_lines(context: dict, *, active_orchestrator: str | None = None) -> list[str]:
    summary = context["summary"]
    adapters = ", ".join(adapter["id"] for adapter in context.get("registered_adapters", []))
    capabilities = ", ".join(
        capability["id"] for capability in context.get("capabilities", {}).get("capabilities", [])
    )
    lines = [
        f"Project: {context['project_root']}",
        f"Branch: {context.get('branch') or 'unknown'}",
        f"Initialized: {context['initialized']}",
        f"Active orchestrator: {active_orchestrator or 'none'}",
        "Summary: "
        f"tasks={summary['tasks_total']} objectives={summary['objectives']} "
        f"active_leases={summary['active_leases']} recent_runs={summary['recent_runs']} "
        f"recent_sessions={summary.get('recent_sessions', 0)}",
        f"Adapters: {adapters or 'none'}",
        f"Capabilities: {capabilities or 'none'}",
    ]
    if context.get("state_error"):
        lines.append(f"State error: {context['state_error']['type']}: {context['state_error']['message']}")
    if context.get("guidance"):
        lines.append("Guidance:")
        lines.extend(f"- {item['description']} ({item['command']})" for item in context["guidance"])
    if context.get("recent_sessions"):
        lines.append("Recent sessions:")
        lines.extend(
            f"- {session['id']} {session['status']} {session.get('title') or session.get('intent') or 'untitled'}"
            for session in context["recent_sessions"][:3]
        )
    lines.append("Safety: passive dashboard context, no backend preflight, no hidden execution.")
    return lines


def _is_initialized(project_root: Path) -> bool:
    db_path = resolve_project_root(project_root) / HARNESS_DIR / "harness.sqlite"
    if not db_path.exists():
        return False
    try:
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'tasks'"
            ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


def _git_branch(project_root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=project_root,
            text=True,
            capture_output=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None
