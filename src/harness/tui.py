from __future__ import annotations

from pathlib import Path

from harness import __version__
from harness.config import HARNESS_DIR
from harness.memory.sqlite_store import SQLiteStore
from harness.models import TaskStatus


def build_tui_dashboard(project_root: Path) -> dict:
    initialized = (project_root / HARNESS_DIR / "harness.sqlite").exists()
    dashboard = {
        "schema_version": "harness.tui_dashboard/v1",
        "ok": True,
        "project_root": str(project_root),
        "initialized": initialized,
        "version": __version__,
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
        "command_suggestions": [
            f"harness home --project {project_root}",
            f"harness quickstart agent --project {project_root}",
            f"harness agents list --project {project_root}",
            f"harness tasks list --project {project_root}",
            f"harness daemon status --project {project_root}",
            f"harness runs --project {project_root}",
        ],
        "safety_boundaries": [
            "read_only_tui",
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
        dashboard["guidance"] = [
            {
                "id": "initialize_project",
                "command": f"harness init --project {project_root}",
                "description": "Initialize local harness persistence for this project.",
            }
        ]
        return dashboard

    store = SQLiteStore(project_root)
    agents = store.list_project_agents()
    objectives = store.list_objectives()
    tasks = store.list_tasks()
    leases = store.list_task_leases()
    runs = store.list_runs()[:5]
    daemon_status = store.daemon_status()
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
                "--parent quant_research --model-profile local_reasoning --tool-policy read_only "
                "--memory-scope quant --output agents/my_agent",
                "description": "Scaffold a declarative custom agent bundle.",
            }
        )
    return dashboard


def render_dashboard_text(dashboard: dict) -> str:
    summary = dashboard["summary"]
    task_status_counts = dashboard["task_status_counts"]
    active_statuses = [
        f"{status}={count}"
        for status, count in task_status_counts.items()
        if count
    ]
    lines = [
        "Agent Harness",
        "",
        "Project",
        f"  Path: {dashboard['project_root']}",
        f"  Initialized: {dashboard['initialized']}",
        f"  Version: {dashboard['version']}",
        "",
        "Summary",
        f"  Imported agents: {summary['imported_agents']}",
        f"  Objectives: {summary['objectives']}",
        f"  Tasks: {summary['tasks_total']}",
        f"  Active leases: {summary['active_leases']}",
        f"  Active daemons: {summary['active_daemons']}",
        f"  Recent runs: {summary['recent_runs']}",
        "",
        "Task Status",
        f"  Counts: {', '.join(active_statuses) if active_statuses else 'none'}",
        "",
        "Agents",
    ]
    if dashboard["agents"]:
        for agent in dashboard["agents"]:
            lines.append(
                f"  {agent['agent_id']} workbench={agent['workbench_id']} profiles={agent['profiles']}"
            )
    else:
        lines.append("  none")
    lines.extend(["", "Tasks"])
    if dashboard["tasks"]:
        for task in dashboard["tasks"]:
            lines.append(
                f"  {task['id']} {task['status']} priority={task['priority']} {task['title']}"
            )
    else:
        lines.append("  none")
    lines.extend(["", "Active Leases"])
    if dashboard["active_leases"]:
        for lease in dashboard["active_leases"]:
            lines.append(
                f"  {lease['id']} task={lease['task_id']} attempt={lease['attempt_id'] or 'none'}"
            )
    else:
        lines.append("  none")
    lines.extend(
        [
            "",
            "Daemon",
            f"  Active daemons: {dashboard['daemon']['active_daemons']}",
            f"  Paused tasks: {dashboard['daemon']['paused_tasks']}",
        ]
    )
    if dashboard["daemon"]["latest_events"]:
        lines.append("  Latest events:")
        for event in dashboard["daemon"]["latest_events"]:
            lines.append(f"    {event['event_type']}: {event['message']}")
    else:
        lines.append("  Latest events: none")
    lines.extend(
        [
            "",
            "Recent Runs",
        ]
    )
    if dashboard["recent_runs"]:
        for run in dashboard["recent_runs"]:
            lines.append(f"  {run['id']} {run['status']} {run.get('task_type') or 'none'}")
    else:
        lines.append("  none")
    lines.extend(["", "Commands"])
    for command in dashboard["command_suggestions"]:
        lines.append(f"  {command}")
    if dashboard.get("guidance"):
        lines.extend(["", "Guidance"])
        for item in dashboard["guidance"]:
            lines.append(f"  {item['id']}: {item['command']}")
    lines.extend(["", "Safety"])
    for boundary in dashboard["safety_boundaries"]:
        lines.append(f"  {boundary}")
    lines.extend(["", "Press q to exit."])
    return "\n".join(lines)


def run_read_only_tui(project_root: Path) -> None:
    from textual.app import App, ComposeResult
    from textual.widgets import Footer, Header, Static

    dashboard_text = render_dashboard_text(build_tui_dashboard(project_root))

    class HarnessReadOnlyTui(App):
        BINDINGS = [("q", "quit", "Quit")]

        def compose(self) -> ComposeResult:
            yield Header(show_clock=False)
            yield Static(dashboard_text)
            yield Footer()

    HarnessReadOnlyTui().run()
