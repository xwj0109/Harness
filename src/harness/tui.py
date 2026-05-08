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


def build_tui_panes(dashboard: dict) -> list[dict]:
    summary = dashboard["summary"]
    task_status_counts = dashboard["task_status_counts"]
    active_statuses = [
        f"{status}={count}"
        for status, count in task_status_counts.items()
        if count
    ]
    panes = [
        {
            "id": "overview",
            "title": "Overview",
            "lines": [
                f"Project: {dashboard['project_root']}",
                f"Initialized: {dashboard['initialized']}",
                f"Version: {dashboard['version']}",
                f"Imported agents: {summary['imported_agents']}",
                f"Objectives: {summary['objectives']}",
                f"Tasks: {summary['tasks_total']}",
                f"Active leases: {summary['active_leases']}",
                f"Active daemons: {summary['active_daemons']}",
                f"Recent runs: {summary['recent_runs']}",
                f"Task status: {', '.join(active_statuses) if active_statuses else 'none'}",
            ],
        },
        {
            "id": "agents",
            "title": "Agents",
            "lines": [
                f"{agent['agent_id']} workbench={agent['workbench_id']} profiles={agent['profiles']}"
                for agent in dashboard["agents"]
            ]
            or ["none"],
        },
        {
            "id": "tasks",
            "title": "Tasks",
            "lines": [
                f"{task['id']} {task['status']} priority={task['priority']} {task['title']}"
                for task in dashboard["tasks"]
            ]
            or ["none"],
        },
        {
            "id": "leases",
            "title": "Active Leases",
            "lines": [
                f"{lease['id']} task={lease['task_id']} attempt={lease['attempt_id'] or 'none'}"
                for lease in dashboard["active_leases"]
            ]
            or ["none"],
        },
        {
            "id": "daemon",
            "title": "Daemon",
            "lines": [
                f"Active daemons: {dashboard['daemon']['active_daemons']}",
                f"Paused tasks: {dashboard['daemon']['paused_tasks']}",
                "Latest events:",
                *(
                    [
                        f"{event['event_type']}: {event['message']}"
                        for event in dashboard["daemon"]["latest_events"]
                    ]
                    or ["none"]
                ),
            ],
        },
        {
            "id": "runs",
            "title": "Recent Runs",
            "lines": [
                f"{run['id']} {run['status']} {run.get('task_type') or 'none'}"
                for run in dashboard["recent_runs"]
            ]
            or ["none"],
        },
        {
            "id": "commands",
            "title": "Commands",
            "lines": dashboard["command_suggestions"],
        },
    ]
    if dashboard.get("guidance"):
        panes.append(
            {
                "id": "guidance",
                "title": "Guidance",
                "lines": [
                    f"{item['id']}: {item['command']}"
                    for item in dashboard["guidance"]
                ],
            }
        )
    panes.append(
        {
            "id": "safety",
            "title": "Safety",
            "lines": dashboard["safety_boundaries"],
        }
    )
    return panes


def filter_tui_panes(panes: list[dict], query: str) -> dict:
    normalized_query = query.strip().casefold()
    if not normalized_query:
        return {
            "schema_version": "harness.tui_filter/v1",
            "ok": True,
            "query": "",
            "total_matches": sum(len(pane["lines"]) for pane in panes),
            "panes": [
                {
                    **pane,
                    "match_count": len(pane["lines"]),
                }
                for pane in panes
            ],
        }

    filtered_panes = []
    total_matches = 0
    for pane in panes:
        title_matches = normalized_query in pane["title"].casefold()
        matched_lines = [
            line for line in pane["lines"] if normalized_query in str(line).casefold()
        ]
        if title_matches and not matched_lines:
            matched_lines = pane["lines"]
        if title_matches or matched_lines:
            match_count = len(matched_lines)
            total_matches += match_count
            filtered_panes.append(
                {
                    **pane,
                    "lines": matched_lines,
                    "match_count": match_count,
                }
            )
    return {
        "schema_version": "harness.tui_filter/v1",
        "ok": True,
        "query": query,
        "total_matches": total_matches,
        "panes": filtered_panes,
    }


def render_dashboard_text(dashboard: dict) -> str:
    lines = ["Agent Harness"]
    for pane in build_tui_panes(dashboard):
        lines.extend(["", pane["title"]])
        lines.extend(f"  {line}" for line in pane["lines"])
    lines.extend(["", "Press q to exit."])
    return "\n".join(lines)


def run_read_only_tui(project_root: Path) -> None:
    from textual.app import App, ComposeResult
    from textual.containers import VerticalScroll
    from textual.widgets import Footer, Header, Static

    dashboard = build_tui_dashboard(project_root)
    panes = build_tui_panes(dashboard)

    class HarnessReadOnlyTui(App):
        CSS = """
        VerticalScroll {
            padding: 1;
        }

        .pane {
            border: round $surface;
            margin: 0 0 1 0;
            padding: 1;
        }

        .pane:focus {
            border: round $accent;
        }
        """
        BINDINGS = [
            ("q", "quit", "Quit"),
            ("tab", "focus_next", "Next pane"),
            ("shift+tab", "focus_previous", "Previous pane"),
        ]

        def compose(self) -> ComposeResult:
            yield Header(show_clock=False)
            with VerticalScroll():
                for pane in panes:
                    content = "\n".join([pane["title"], "", *pane["lines"]])
                    widget = Static(content, id=f"pane-{pane['id']}", classes="pane")
                    widget.can_focus = True
                    yield widget
            yield Footer()

    HarnessReadOnlyTui().run()
