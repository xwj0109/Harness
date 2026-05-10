from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

from harness import __version__
from harness.config import HARNESS_DIR
from harness.execution import list_execution_adapter_descriptors
from harness.memory.sqlite_store import SQLiteStore
from harness.models import TaskStatus
from harness.paths import resolve_project_root


OPERATOR_CONTEXT_SCHEMA_VERSION = "harness.operator_context/v1"


def build_operator_context(project_root: Path) -> dict:
    project_root = resolve_project_root(project_root)
    initialized = _is_initialized(project_root)
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
        "registered_adapters": [
            descriptor.model_dump(mode="json")
            for descriptor in list_execution_adapter_descriptors()
        ],
        "command_suggestions": [
            f"harness home --project {project_root}",
            f"harness chat --project {project_root}",
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

    store = SQLiteStore(project_root)
    try:
        agents = store.list_project_agents()
        objectives = store.list_objectives()
        tasks = store.list_tasks()
        leases = store.list_task_leases()
        runs = store.list_runs()[:5]
        daemon_status = store.daemon_status()
    except sqlite3.Error as exc:
        dashboard["initialized"] = False
        dashboard["state_error"] = {
            "type": exc.__class__.__name__,
            "message": str(exc),
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
    dashboard["summary"] = {
        "imported_agents": len(agents),
        "objectives": len(objectives),
        "tasks_total": len(tasks),
        "active_leases": len(active_leases),
        "active_daemons": len(daemon_status.active_daemons),
        "recent_runs": len(runs),
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
            "title": task.title,
            "status": task.status.value,
            "priority": task.priority,
            "objective_id": task.objective_id,
            "agent_id": task.agent_id,
            "workbench_id": task.workbench_id,
            "execution_adapter": task.metadata.get("execution_adapter"),
            "task_type": task.metadata.get("task_type"),
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
            "goal": run.goal,
            "created_at": run.created_at.isoformat(),
        }
        for run in runs
    ]
    dashboard["daemon"] = {
        "active_daemons": len(daemon_status.active_daemons),
        "paused_tasks": len(daemon_status.paused_tasks),
        "latest_events": [
            {
                "id": event.id,
                "daemon_id": event.daemon_id,
                "event_type": event.event_type,
                "message": event.message,
                "created_at": event.created_at.isoformat(),
            }
            for event in daemon_status.latest_events[:5]
        ],
    }
    dashboard["guidance"] = []
    if task_status_counts.get("ready", 0) > 0 and not active_leases:
        dashboard["guidance"].append(
            {
                "id": "lease_ready_task",
                "command": f"harness daemon run-once --project {project_root}",
                "description": "Lease the highest-priority eligible task without executing it.",
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
    return dashboard


def build_tui_dashboard(project_root: Path) -> dict:
    context = build_operator_context(project_root)
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
    lines = [
        f"Project: {context['project_root']}",
        f"Branch: {context.get('branch') or 'unknown'}",
        f"Initialized: {context['initialized']}",
        f"Active orchestrator: {active_orchestrator or 'none'}",
        "Summary: "
        f"tasks={summary['tasks_total']} objectives={summary['objectives']} "
        f"active_leases={summary['active_leases']} recent_runs={summary['recent_runs']}",
        f"Adapters: {adapters or 'none'}",
    ]
    if context.get("state_error"):
        lines.append(f"State error: {context['state_error']['type']}: {context['state_error']['message']}")
    if context.get("guidance"):
        lines.append("Guidance:")
        lines.extend(f"- {item['description']} ({item['command']})" for item in context["guidance"])
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
