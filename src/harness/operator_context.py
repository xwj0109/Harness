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
from harness.model_registry import resolve_model_for_session
from harness.model_discovery import list_cached_discovered_models
from harness.models import EventStreamType, SessionPartKind, SessionPermissionStatus, SessionStatus, TaskStatus
from harness.operator_loop import session_operator_status_projection
from harness.orchestration_efficiency import (
    run_orchestration_efficiency_audit,
    run_orchestration_microbenchmarks,
    summarize_orchestration_efficiency,
    summarize_orchestration_microbenchmarks,
)
from harness.orchestration_synthesis import summarize_orchestration_synthesis_sources
from harness.paths import resolve_project_root
from harness.pending_chat_actions import (
    PENDING_CHAT_ACTION_METADATA_KEY,
    pending_chat_action_audit,
    pending_chat_action_projection,
    pending_chat_action_search_text,
)
from harness.progress import build_orchestration_progress
from harness.security import sanitize_for_logging
from harness.session_cwd import session_cwd_payload
from harness.session_events import read_session_events_with_diagnostics, session_events_read_health_payload
from harness.session_health import active_run_reference_counts, session_active_run_reference_projection
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
    orchestration_readiness = _orchestration_readiness_dashboard_summary(project_root)
    orchestration_efficiency = _orchestration_efficiency_dashboard_summary(project_root)
    orchestration_microbenchmarks = _orchestration_microbenchmarks_dashboard_summary(project_root)
    orchestration_synthesis = summarize_orchestration_synthesis_sources(
        project_root,
        readiness_summary=orchestration_readiness,
        efficiency_summary=orchestration_efficiency,
        microbenchmark_summary=orchestration_microbenchmarks,
        include_references=False,
    )
    orchestration_synthesis["source"] = "operator_context_no_reference_synthesis"
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
            "pending_chat_actions": 0,
            "invalid_pending_chat_actions": 0,
            "stale_pending_chat_actions": 0,
            "active_run_refs": 0,
            "valid_active_run_refs": 0,
            "stale_active_run_refs": 0,
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
            "counts": {
                "total": 0,
                "open": 0,
                "running": 0,
                "waiting_approval": 0,
                "pending_chat_actions": 0,
                "invalid_pending_chat_actions": 0,
                "stale_pending_chat_actions": 0,
                "active_run_refs": 0,
                "valid_active_run_refs": 0,
                "stale_active_run_refs": 0,
                "archived": 0,
                "filtered": 0,
            },
        },
        "active_session": None,
        "live_activity": _empty_live_activity("setup_needed" if not initialized else "idle"),
        "model_catalog": _model_catalog_projection(
            catalog_config,
            provider_catalog,
            model_catalog,
            sessions=[],
            preferences=[],
            provider_accounts=[],
            source=catalog_source,
            config_error=catalog_error,
        ),
        "memory": {
            "schema_version": "harness.memory_summary/v1",
            "total": 0,
            "recent": [],
        },
        "session_tools": _session_tools_summary(project_root, active_session=None),
        "orchestration_readiness": orchestration_readiness,
        "orchestration_efficiency": orchestration_efficiency,
        "orchestration_microbenchmarks": orchestration_microbenchmarks,
        "orchestration_synthesis": orchestration_synthesis,
        "progress": {
            "schema_version": "harness.orchestration_progress_summary/v1",
            "objective_id": None,
            "objective_title": None,
            "mode": "idle",
            "next_action": None,
            "active_lease_ids": [],
            "active_run_ids": [],
            "blocked_reasons": [],
            "objective_evidence": None,
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
        provider_accounts = store.list_provider_accounts()
        provider_catalog = list_provider_catalog(catalog_config, provider_accounts=provider_accounts)
        agents = store.list_project_agents()
        objectives = store.list_objectives()
        tasks = store.list_tasks()
        leases = store.list_task_leases()
        all_runs = store.list_runs()
        runs = all_runs[:5]
        known_run_ids = {run.id for run in all_runs}
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
        base_model_catalog = list_model_catalog(cfg, provider_accounts=provider_accounts)
        model_catalog = [*base_model_catalog, *list_cached_discovered_models(cfg, store)]
        catalog_cache = store.replace_provider_model_catalog_cache(provider_catalog, base_model_catalog)
        daemon_status = store.daemon_status()
        controls = store.list_execution_controls()
        breakers = store.list_adapter_breaker_states(
            [descriptor.id for descriptor in list_execution_adapter_descriptors()]
        )
        terminal_tabs = _terminal_tabs_summary(store)
        model_preferences = store.list_model_preferences()
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
    pending_action_audits = [_pending_chat_action_audit_for_session(store, session) for session in all_open_sessions]
    transcript_health = [_session_transcript_health_projection(store, session.id) for session in all_open_sessions]
    run_ref_counts = active_run_reference_counts(
        store,
        all_open_sessions,
        known_run_ids=known_run_ids,
        project_root=project_root,
    )

    dashboard["summary"] = {
        "imported_agents": len(agents),
        "objectives": len(objectives),
        "tasks_total": len(tasks),
        "active_leases": len(active_leases),
        "active_daemons": len(daemon_status.active_daemons),
        "recent_runs": len(runs),
        "recent_sessions": len(sessions),
        "pending_chat_actions": sum(1 for audit in pending_action_audits if audit["recoverable"]),
        "invalid_pending_chat_actions": sum(1 for audit in pending_action_audits if audit["status"] == "invalid"),
        "stale_pending_chat_actions": sum(1 for audit in pending_action_audits if audit["status"] == "stale"),
        "malformed_session_transcripts": sum(1 for health in transcript_health if not health["ok"]),
        **run_ref_counts,
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
            "pending_action": _pending_chat_action_projection_for_session(store, session),
            "pending_action_audit": _pending_chat_action_audit_for_session(store, session),
            "transcript_health": _session_transcript_health_projection(store, session.id),
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
        store=store,
        sessions=model_sessions,
        preferences=model_preferences,
        provider_accounts=provider_accounts,
        cache=catalog_cache,
        source=catalog_source,
        config_error=catalog_error,
    )
    dashboard["session_tools"] = _session_tools_summary(project_root, active_session=dashboard.get("active_session"))
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
                "checkpoints": progress.checkpoints,
                "objective_evidence": progress.objective_evidence,
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
            "pending_chat_actions": 0,
            "invalid_pending_chat_actions": 0,
            "stale_pending_chat_actions": 0,
            "active_run_refs": 0,
            "valid_active_run_refs": 0,
            "stale_active_run_refs": 0,
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
        "pending_chat_actions": int((dashboard.get("summary") or {}).get("pending_chat_actions", 0) or 0),
        "invalid_pending_chat_actions": int((dashboard.get("summary") or {}).get("invalid_pending_chat_actions", 0) or 0),
        "stale_pending_chat_actions": int((dashboard.get("summary") or {}).get("stale_pending_chat_actions", 0) or 0),
        "stale_active_run_refs": int((dashboard.get("summary") or {}).get("stale_active_run_refs", 0) or 0),
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
    if counts.get("pending_chat_actions"):
        return "approval_required"
    if counts.get("invalid_pending_chat_actions") or counts.get("stale_pending_chat_actions") or counts.get("stale_active_run_refs"):
        return "blocked"
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
            "counts": {
                "total": 0,
                "open": 0,
                "running": 0,
                "waiting_approval": 0,
                "pending_chat_actions": 0,
                "invalid_pending_chat_actions": 0,
                "stale_pending_chat_actions": 0,
                "active_run_refs": 0,
                "valid_active_run_refs": 0,
                "stale_active_run_refs": 0,
                "archived": 0,
                "filtered": 0,
            },
            "ok": False,
        }
    store = SQLiteStore.open_initialized(project_root)
    all_sessions = store.list_sessions()
    normalized_filter = status_filter if status_filter in {"open", "running", "archived", "all"} else "open"
    normalized_query = query.strip().casefold()
    running_statuses = {SessionStatus.RUNNING, SessionStatus.WAITING_APPROVAL}
    pending_action_audits = [_pending_chat_action_audit_for_session(store, session) for session in all_sessions]
    transcript_health = [_session_transcript_health_projection(store, session.id) for session in all_sessions]
    known_run_ids = {run.id for run in store.list_runs()}
    run_ref_counts = active_run_reference_counts(
        store,
        all_sessions,
        known_run_ids=known_run_ids,
        project_root=project_root,
    )
    counts = {
        "total": len(all_sessions),
        "open": sum(1 for session in all_sessions if session.status != SessionStatus.ARCHIVED),
        "running": sum(1 for session in all_sessions if session.status in running_statuses),
        "waiting_approval": sum(1 for session in all_sessions if session.status == SessionStatus.WAITING_APPROVAL),
        "pending_chat_actions": sum(1 for audit in pending_action_audits if audit["recoverable"]),
        "invalid_pending_chat_actions": sum(1 for audit in pending_action_audits if audit["status"] == "invalid"),
        "stale_pending_chat_actions": sum(1 for audit in pending_action_audits if audit["status"] == "stale"),
        "malformed_session_transcripts": sum(1 for health in transcript_health if not health["ok"]),
        **run_ref_counts,
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
        )
        pending_audit = row.get("pending_action_audit") if (row.get("pending_action_audit") or {}).get("present") else None
        run_ref = row.get("active_run_reference") or {}
        searchable = (
            f"{searchable} "
            f"{pending_chat_action_search_text(row.get('pending_action') or pending_audit)} "
            f"{run_ref.get('status') or ''} {run_ref.get('missing_run_id') or ''}"
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
        "pending_action": _pending_chat_action_projection_for_session(store, session),
        "pending_action_audit": _pending_chat_action_audit_for_session(store, session),
        "transcript_health": _session_transcript_health_projection(store, session.id),
        "active_run_reference": session_active_run_reference_projection(store, session),
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
    from harness.session_tools import session_planning_mode_projection

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
        active_tools=_operator_active_tools(project_root=project_root),
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
        "token_input": session.token_input,
        "token_output": session.token_output,
        "token_reasoning": session.token_reasoning,
        "token_cache_read": session.token_cache_read,
        "token_cache_write": session.token_cache_write,
        "cwd": cwd,
        "planning_mode": session_planning_mode_projection(session.metadata),
        "pending_action": _pending_chat_action_projection_for_session(store, session),
        "pending_action_audit": _pending_chat_action_audit_for_session(store, session),
        "transcript_health": _session_transcript_health_projection(store, session.id),
        "active_run_reference": session_active_run_reference_projection(store, session, project_root=project_root),
        "operator": operator,
        "ui_preferences": sanitize_for_logging(session.ui_preferences),
        "latest_ui_activation": _ui_activation_preview(latest_ui_activation) if latest_ui_activation else None,
        "composer_context": _session_composer_context(store, session_id, project_root),
        "timeline": [render_timeline_event(event) for event in timeline_events],
        "transcript": [render_transcript_entry(entry) for entry in transcript_entries],
    }


def _session_transcript_health_projection(store: SQLiteStore, session_id: str) -> dict:
    return session_events_read_health_payload(read_session_events_with_diagnostics(store.project_root, session_id))


def _pending_chat_action_projection_for_session(store: SQLiteStore, session) -> dict | None:
    return pending_chat_action_projection(
        session.metadata,
        session_id=session.id,
        lease_status=_pending_chat_action_lease_status(store, session.metadata),
    )


def _pending_chat_action_audit_for_session(store: SQLiteStore, session) -> dict:
    return pending_chat_action_audit(
        session.metadata,
        session_id=session.id,
        lease_status=_pending_chat_action_lease_status(store, session.metadata),
    )


def _pending_chat_action_lease_status(store: SQLiteStore, metadata: dict | None) -> str | None:
    raw = (metadata or {}).get(PENDING_CHAT_ACTION_METADATA_KEY)
    if not isinstance(raw, dict) or raw.get("kind") != "execute_lease":
        return None
    lease_id = raw.get("lease_id")
    if not isinstance(lease_id, str) or not lease_id.strip():
        return None
    try:
        return store.get_task_lease(lease_id).status.value
    except KeyError:
        return "missing"


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


def _operator_active_tools(*, project_root: Path | None = None) -> list[str]:
    from harness.session_tools import model_visible_session_tool_ids

    return model_visible_session_tool_ids(project_root=project_root)


def _session_tools_summary(project_root: Path, *, active_session: dict | None) -> dict:
    from harness.session_tools import session_tool_catalog_projection

    planning_mode = (active_session or {}).get("planning_mode") or {"active": False}
    try:
        catalog = session_tool_catalog_projection(project_root=project_root)
        tools = []
        for tool in catalog.get("tools") or []:
            if tool.get("id") not in {"plan-enter", "plan-exit", "web-fetch", "web-search"}:
                continue
            policy = tool.get("policy") or {}
            tools.append(
                {
                    "id": tool.get("id"),
                    "title": tool.get("title"),
                    "enabled": bool(policy.get("enabled")),
                    "disabled_reason": sanitize_for_logging(policy.get("disabled_reason")),
                    "permission_required": bool(policy.get("permission_required")),
                    "boundary_kind": sanitize_for_logging(policy.get("boundary_kind")),
                    "risk": sanitize_for_logging(policy.get("risk")),
                    "maturity": list(policy.get("maturity") or []),
                    "required_config": list(policy.get("required_config") or []),
                    "policy_reasons": [
                        sanitize_for_logging(reason)
                        for reason in (policy.get("policy_reasons") or [])
                    ],
                    "planning_only": bool(policy.get("planning_only")),
                }
            )
    except Exception as exc:
        tools = []
        error = sanitize_for_logging(str(exc))
    else:
        error = None
    by_id = {str(tool.get("id")): tool for tool in tools}
    web_search = by_id.get("web-search") or {}
    web_fetch = by_id.get("web-fetch") or {}
    plan_enter = by_id.get("plan-enter") or {}
    return {
        "schema_version": "harness.session_tools_summary/v1",
        "planning_mode": planning_mode,
        "tools": tools,
        "plan_mode_enabled": bool(plan_enter.get("enabled", True)),
        "web_search_enabled": bool(web_search.get("enabled")),
        "web_fetch_enabled": bool(web_fetch.get("enabled")),
        "web_search_disabled_reason": web_search.get("disabled_reason"),
        "web_fetch_disabled_reason": web_fetch.get("disabled_reason"),
        "web_requires_approval": bool(web_search.get("permission_required") or web_fetch.get("permission_required")),
        "network_called": False,
        "process_started": False,
        "filesystem_modified": False,
        "permission_granting": False,
        "error": error,
    }


def _session_composer_context(store: SQLiteStore, session_id: str, project_root: Path) -> dict:
    attachments: list[dict] = []
    transcript_bytes = 0
    transcript_part_count = 0
    for part in store.list_session_parts(session_id):
        if part.kind in {
            SessionPartKind.TEXT,
            SessionPartKind.TOOL_RESULT,
            SessionPartKind.REASONING_SUMMARY,
            SessionPartKind.SUMMARY,
        }:
            transcript_part_count += 1
            transcript_bytes += len(str(part.text or "").encode("utf-8"))
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
    attachment_bytes = sum(int(item["size_bytes"]) for item in attachments)
    attachment_tokens = _estimate_tokens_for_bytes(attachment_bytes)
    transcript_tokens = _estimate_tokens_for_bytes(transcript_bytes)
    return {
        "schema_version": "harness.tui_composer_context/v1",
        "attachment_count": len(attachments),
        "attachments": attachments,
        "text_part_count": transcript_part_count,
        "transcript_bytes": transcript_bytes,
        "transcript_estimated_tokens": transcript_tokens,
        "total_attachment_bytes": attachment_bytes,
        "attachment_estimated_tokens": attachment_tokens,
        "total_estimated_tokens": transcript_tokens + attachment_tokens,
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
    store: SQLiteStore | None = None,
    sessions: list,
    preferences: list | None = None,
    provider_accounts: list[dict] | None = None,
    cache: dict | None = None,
    source: str = "project_config",
    config_error: dict | None = None,
) -> dict:
    providers_by_id = {provider.provider_id: provider for provider in provider_catalog}
    preferences = preferences or []
    preferences_by_ref = {preference.get("raw_model_ref"): preference for preference in preferences}
    active_model = _active_model_summary(cfg, sessions, model_catalog, provider_accounts=provider_accounts, store=store)
    active_ref = active_model.get("raw_model_ref") if active_model else None

    def _model_row(model) -> dict:
        provider = providers_by_id.get(model.provider_id)
        preference = preferences_by_ref.get(model.raw_model_ref) or preferences_by_ref.get(model.canonical_model_ref)
        if model.alias_of is not None or provider is None:
            validation = validate_model_selection(
                cfg,
                model.raw_model_ref,
                model_overlays=[item for item in model_catalog if item.source == "discovered"],
                provider_accounts=provider_accounts,
            )
            provider_enabled = validation.provider_enabled
            executable = validation.executable
            blocked_reasons = validation.blocked_reasons
            no_hidden_fallback = validation.no_hidden_fallback
        else:
            provider_enabled = provider.enabled
            blocked_reasons = list(getattr(model, "blocked_reasons", []) or ([] if provider.enabled else ["provider_disabled"]))
            executable = bool(getattr(model, "executable_model", False))
            no_hidden_fallback = True
        return {
            "provider_id": model.provider_id,
            "model_id": sanitize_for_logging(model.model_id),
            "raw_model_ref": sanitize_for_logging(model.raw_model_ref),
            "canonical_model_ref": sanitize_for_logging(model.canonical_model_ref),
            "alias_of": sanitize_for_logging(model.alias_of),
            "protocol": sanitize_for_logging(model.protocol),
            "status": sanitize_for_logging(model.status),
            "variant": sanitize_for_logging(model.variant),
            "variant_list": sanitize_for_logging(list(getattr(model, "variant_list", []) or [])),
            "model_profile_id": sanitize_for_logging(model.model_profile_id),
            "source": model.source,
            "context_limit": model.context_limit,
            "max_output_tokens": model.max_output_tokens,
            "cost": sanitize_for_logging(model.cost),
            "tool_support": model.tool_support,
            "reasoning_support": model.reasoning_support,
            "modalities": model.modalities,
            "last_refresh_at": sanitize_for_logging(getattr(model, "last_refresh_at", None)),
            "cache_status": sanitize_for_logging(getattr(model, "cache_status", None)),
            "refresh_supported": bool(getattr(model, "refresh_supported", False)),
            "data_boundary": provider.metadata.data_boundary.value if provider is not None else "unknown",
            "endpoint": sanitize_for_logging((provider.settings_preview or {}).get("base_url")) if provider is not None else None,
            "provider_enabled": provider_enabled,
            "provider_connected": bool(getattr(model, "provider_connected", False)),
            "known_catalog_model": bool(getattr(model, "known_catalog_model", True)),
            "available_model": bool(getattr(model, "available_model", False)),
            "executable_model": bool(getattr(model, "executable_model", executable)),
            "selected_model": bool(active_ref and model.raw_model_ref == active_ref),
            "availability": sanitize_for_logging(getattr(model, "availability", "available" if executable else "blocked")),
            "executable": executable,
            "blocked_reasons": blocked_reasons,
            "no_hidden_fallback": no_hidden_fallback,
            "favorite": bool(preference.get("favorite")) if preference else False,
            "is_default": bool(preference.get("is_default")) if preference else False,
            "selection_count": int(preference.get("selection_count") or 0) if preference else 0,
            "last_selected_at": sanitize_for_logging(preference.get("last_selected_at")) if preference else None,
            "last_reasoning_effort": sanitize_for_logging(preference.get("last_reasoning_effort")) if preference else None,
            "preference_source": sanitize_for_logging(preference.get("source")) if preference else None,
        }

    def _model_sort_key(model: dict) -> tuple:
        if model.get("raw_model_ref") == active_ref:
            group = 0
        elif model.get("favorite"):
            group = 1
        elif model.get("last_selected_at"):
            group = 2
        elif model.get("executable"):
            group = 3
        else:
            group = 4
        recency = str(model.get("last_selected_at") or "")
        return (
            group,
            "" if group in {0, 3, 4} else _invert_sort_text(recency),
            str(model.get("provider_id") or ""),
            str(model.get("model_id") or ""),
            str(model.get("raw_model_ref") or ""),
        )

    model_rows = sorted([_model_row(model) for model in model_catalog], key=_model_sort_key)

    projection = {
        "schema_version": "harness.operator_model_catalog/v1",
        "providers": [
            {
                "provider_id": provider.provider_id,
                "display_name": provider.display_name,
                "enabled": provider.enabled,
                "connected": bool(getattr(provider, "connected", False)),
                "kind": provider.kind.value,
                "credential_status": provider.credential_status.value,
                "credential_source": _redacted_credential_source(getattr(provider, "credential_source", "unknown")),
                "active_account_id": sanitize_for_logging(getattr(provider, "active_account_id", None)),
                "auth_methods": [_redacted_credential_source(item) for item in list(getattr(provider, "auth_methods", []) or [])],
                "model_count": getattr(provider, "model_count", 0),
                "available_model_count": getattr(provider, "available_model_count", 0),
                "refresh_supported": bool(getattr(provider, "refresh_supported", False))
                or any(model.get("provider_id") == provider.provider_id and model.get("refresh_supported") for model in model_rows),
                "refresh_status": _provider_refresh_status(provider.provider_id, model_rows),
                "data_boundary": provider.metadata.data_boundary.value,
                "endpoint": sanitize_for_logging((provider.settings_preview or {}).get("base_url")),
                "constraints": provider.constraints,
            }
            for provider in provider_catalog
        ],
        "models": model_rows,
        "preferences": [sanitize_for_logging(preference) for preference in preferences],
        "active_model": active_model,
        "source": source,
        "permission_granting": False,
        "no_hidden_fallback": True,
    }
    if cache is not None:
        projection["cache"] = cache
    if config_error is not None:
        projection["config_error"] = config_error
    return projection


def _invert_sort_text(value: str) -> str:
    return "".join(chr(0x10FFFF - ord(char)) for char in value)


def _provider_refresh_status(provider_id: str, model_rows: list[dict]) -> str:
    provider_models = [model for model in model_rows if model.get("provider_id") == provider_id]
    statuses = sorted({str(model.get("cache_status")) for model in provider_models if model.get("cache_status")})
    if statuses:
        return statuses[0] if len(statuses) == 1 else "mixed"
    if any(model.get("refresh_supported") for model in provider_models):
        return "not_refreshed"
    return "unsupported"


def _redacted_credential_source(value) -> str:
    text = str(value or "unknown")
    if text.startswith("env:"):
        return "env:<redacted>"
    return str(sanitize_for_logging(text))


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


def _active_model_summary(
    cfg,
    sessions: list,
    model_catalog: list,
    *,
    provider_accounts: list[dict] | None = None,
    store: SQLiteStore | None = None,
) -> dict | None:
    if not sessions:
        return None
    latest = sessions[0]
    resolution = resolve_model_for_session(cfg, store, latest.id) if store is not None else None
    raw_ref = resolution.raw_model_ref if resolution is not None else latest.raw_model_ref
    validation = validate_model_selection(cfg, raw_ref, provider_accounts=provider_accounts) if raw_ref else None
    matched = validation.matched_model if validation else None
    return {
        "session_id": latest.id,
        "raw_model_ref": sanitize_for_logging(raw_ref),
        "selection_source": resolution.source.value if resolution is not None and resolution.source is not None else ("session_override" if latest.raw_model_ref else None),
        "model_resolution": sanitize_for_logging(resolution.model_dump(mode="json")) if resolution is not None else None,
        "provider_id": sanitize_for_logging(latest.provider_id or (matched.provider_id if matched else None)),
        "model_id": sanitize_for_logging(latest.model_id or (matched.model_id if matched else None)),
        "model_variant": sanitize_for_logging(latest.model_variant),
        "canonical_model_ref": sanitize_for_logging(validation.canonical_model_ref if validation else None),
        "protocol": sanitize_for_logging(validation.protocol if validation else None),
        "alias_used": sanitize_for_logging(validation.alias_used if validation else None),
        "known_catalog_entry": matched is not None,
        "executable": validation.executable if validation else None,
        "provider_known": validation.provider_known if validation else None,
        "provider_enabled": validation.provider_enabled if validation else None,
        "blocked_reasons": validation.blocked_reasons if validation else (resolution.blocked_reasons if resolution else []),
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


def _orchestration_readiness_dashboard_summary(project_root: Path) -> dict:
    try:
        root = resolve_project_root(project_root)
        initialized = _is_initialized(root)
        return {
            "schema_version": "harness.orchestration_readiness_summary/v1",
            "ok": True,
            "status": "pass",
            "initialized": initialized,
            "summary": {
                "total": 0,
                "pass": 0,
                "warning": 0,
                "fail": 0,
                "skipped": 0,
                "deep_audit_required": True,
            },
            "failing_check_ids": [],
            "warning_check_ids": [],
            "skipped_check_ids": [],
            "reference_root": None,
            "reference_systems": [
                "microsoft_agent_framework",
                "langgraph",
                "temporal",
                "dapr",
                "openai_agents",
                "google_adk",
                "opentelemetry",
            ],
            "safety": {
                "read_only": True,
                "reference_code_imported": False,
                "reference_contents_included": False,
                "provider_called": False,
                "network_called": False,
                "adapter_execution_started": False,
                "filesystem_modified": False,
                "permission_granting": False,
            },
            "next_action": f"harness orchestration audit --project {root} --no-references --output json",
            "command": f"harness orchestration audit --project {root} --no-references --output json",
            "source": "operator_context_bounded_passive_readiness_sample",
        }
    except Exception as exc:
        return {
            "schema_version": "harness.orchestration_readiness_summary/v1",
            "ok": False,
            "status": "fail",
            "initialized": False,
            "summary": {"total": 0, "pass": 0, "warning": 0, "fail": 1, "skipped": 0},
            "failing_check_ids": ["orchestration_readiness_projection"],
            "warning_check_ids": [],
            "skipped_check_ids": [],
            "reference_root": None,
            "reference_systems": [],
            "safety": {
                "read_only": True,
                "reference_code_imported": False,
                "reference_contents_included": False,
                "provider_called": False,
                "network_called": False,
                "adapter_execution_started": False,
                "filesystem_modified": False,
                "permission_granting": False,
            },
            "next_action": f"harness orchestration audit --project {project_root} --output json",
            "command": f"harness orchestration audit --project {project_root} --output json",
            "source": "operator_context_no_reference_repositories",
            "error": str(sanitize_for_logging(f"{exc.__class__.__name__}: {exc}")),
        }


def _orchestration_efficiency_dashboard_summary(project_root: Path) -> dict:
    try:
        audit = run_orchestration_efficiency_audit(project_root)
        summary = summarize_orchestration_efficiency(audit)
        summary["source"] = "operator_context_metadata_only"
        return summary
    except Exception as exc:
        return {
            "schema_version": "harness.orchestration_efficiency_summary/v1",
            "ok": False,
            "status": "fail",
            "summary": {"total": 0, "pass": 0, "warning": 0, "fail": 1, "skipped": 0},
            "failing_check_ids": ["orchestration_efficiency_projection"],
            "warning_check_ids": [],
            "skipped_check_ids": [],
            "check_ids": [],
            "safety": {
                "read_only": True,
                "reference_code_imported": False,
                "reference_contents_included": False,
                "provider_called": False,
                "network_called": False,
                "adapter_execution_started": False,
                "filesystem_modified": False,
                "permission_granting": False,
                "artifact_bodies_read": False,
            },
            "next_action": f"harness evals run --suite orchestration-efficiency --project {project_root} --output json",
            "command": f"harness evals run --suite orchestration-efficiency --project {project_root} --output json",
            "source": "operator_context_metadata_only",
            "error": str(sanitize_for_logging(f"{exc.__class__.__name__}: {exc}")),
        }


def _orchestration_microbenchmarks_dashboard_summary(project_root: Path) -> dict:
    try:
        result = run_orchestration_microbenchmarks(project_root, samples=1)
        summary = summarize_orchestration_microbenchmarks(result)
        summary["source"] = "operator_context_bounded_passive_sample"
        return summary
    except Exception as exc:
        return {
            "schema_version": "harness.orchestration_microbenchmarks_summary/v1",
            "ok": False,
            "status": "fail",
            "summary": {"total": 0, "pass": 0, "warning": 0, "fail": 1, "skipped": 0},
            "failing_benchmark_ids": ["orchestration_microbenchmarks_projection"],
            "warning_benchmark_ids": [],
            "skipped_benchmark_ids": [],
            "benchmark_ids": [],
            "safety": {
                "read_only": True,
                "reference_code_imported": False,
                "reference_contents_included": False,
                "provider_called": False,
                "network_called": False,
                "adapter_execution_started": False,
                "filesystem_modified": False,
                "permission_granting": False,
                "artifact_bodies_read": False,
            },
            "next_action": f"harness evals run --suite orchestration-microbenchmarks --project {project_root} --output json",
            "command": f"harness evals run --suite orchestration-microbenchmarks --project {project_root} --output json",
            "source": "operator_context_bounded_passive_sample",
            "error": str(sanitize_for_logging(f"{exc.__class__.__name__}: {exc}")),
        }


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
        f"recent_sessions={summary.get('recent_sessions', 0)} "
        f"pending_actions={summary.get('pending_chat_actions', 0)} "
        f"invalid_pending={summary.get('invalid_pending_chat_actions', 0)} "
        f"stale_pending={summary.get('stale_pending_chat_actions', 0)}",
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
            + (
                f" | pending: {session['pending_action']['label']}"
                if session.get("pending_action")
                else ""
            )
            + (
                f" | pending metadata: {session['pending_action_audit']['status']}"
                if (session.get("pending_action_audit") or {}).get("status") in {"invalid", "stale"}
                else ""
            )
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
