from __future__ import annotations

import sqlite3
from pathlib import Path

from harness import __version__
from harness.config import HARNESS_DIR
from harness.memory.sqlite_store import SQLiteStore
from harness.models import TaskStatus


TUI_PIXEL_ART = [
    "   /\\_____/\\   ",
    "  /  o   o  \\  ",
    " ( ==  ^  == ) ",
    "  )         (  ",
    " (           ) ",
    " ( (  ) (  ) )",
    "(__(__)_(__)__)",
]

COMMAND_PALETTE_GROUPS = [
    {"id": "orientation", "title": "Orientation"},
    {"id": "agent_authoring", "title": "Agent Authoring"},
    {"id": "project_agents", "title": "Project Agents"},
    {"id": "built_in_specs", "title": "Built-In Specs"},
    {"id": "objectives_tasks", "title": "Objectives And Tasks"},
    {"id": "daemon_control", "title": "Daemon Control Plane"},
    {"id": "read_only_adapter", "title": "Authorized Read-Only Adapter"},
    {"id": "runtime_evidence", "title": "Runtime Evidence"},
    {"id": "packaging_smoke", "title": "Packaging Smoke"},
]

COMMAND_PALETTE_ENTRIES = [
    {
        "id": "orientation.home",
        "group_id": "orientation",
        "title": "Open project dashboard",
        "command": "harness home --project .",
        "description": "Show local project summary in text form.",
        "mutates_when_run": False,
        "safety_note": "Read-only orientation command.",
    },
    {
        "id": "orientation.quickstart_agent",
        "group_id": "orientation",
        "title": "Show agent quickstart",
        "command": "harness quickstart agent --project .",
        "description": "Print the MVP agent command sequence without running it.",
        "mutates_when_run": False,
        "safety_note": "Command composition only.",
    },
    {
        "id": "agent_authoring.scaffold",
        "group_id": "agent_authoring",
        "title": "Scaffold an agent bundle",
        "command": "harness agents scaffold my_agent --workbench quant --kind specialist --parent quant_research --model-profile local_reasoning --tool-policy read_only --memory-scope quant --output agents/my_agent --output-format json",
        "description": "Create a local explicit-path custom agent bundle.",
        "mutates_when_run": True,
        "safety_note": "Creates files only at the explicit output path when manually run.",
    },
    {
        "id": "agent_authoring.validate",
        "group_id": "agent_authoring",
        "title": "Validate an agent bundle",
        "command": "harness agents validate agents/my_agent --output json",
        "description": "Validate a custom agent bundle against packaged built-ins.",
        "mutates_when_run": False,
        "safety_note": "Read-only validation.",
    },
    {
        "id": "agent_authoring.preview",
        "group_id": "agent_authoring",
        "title": "Preview an agent bundle",
        "command": "harness agents preview agents/my_agent --output json",
        "description": "Preview effective custom agent metadata.",
        "mutates_when_run": False,
        "safety_note": "Read-only preview.",
    },
    {
        "id": "project_agents.import",
        "group_id": "project_agents",
        "title": "Import a project agent",
        "command": "harness agents import agents/my_agent --project . --output json",
        "description": "Persist validated agent metadata into initialized harness state.",
        "mutates_when_run": True,
        "safety_note": "Metadata import only when manually run.",
    },
    {
        "id": "project_agents.list",
        "group_id": "project_agents",
        "title": "List project agents",
        "command": "harness agents list --project .",
        "description": "List imported project agents.",
        "mutates_when_run": False,
        "safety_note": "Read-only inspection.",
    },
    {
        "id": "project_agents.inspect",
        "group_id": "project_agents",
        "title": "Inspect a project agent",
        "command": "harness agents inspect my_agent --project .",
        "description": "Inspect one imported project agent.",
        "mutates_when_run": False,
        "safety_note": "Read-only inspection.",
    },
    {
        "id": "built_in_specs.list",
        "group_id": "built_in_specs",
        "title": "List built-in specs",
        "command": "harness specs --output json",
        "description": "Inspect packaged built-in spec registry.",
        "mutates_when_run": False,
        "safety_note": "Read-only registry inspection.",
    },
    {
        "id": "built_in_specs.preview_agent",
        "group_id": "built_in_specs",
        "title": "Preview built-in agent policy",
        "command": "harness specs preview agent commodities_researcher --output json",
        "description": "Preview effective declarative agent metadata.",
        "mutates_when_run": False,
        "safety_note": "Read-only preview.",
    },
    {
        "id": "objectives_tasks.add_task",
        "group_id": "objectives_tasks",
        "title": "Add read-only task",
        "command": "harness tasks add --title \"Read-only summary\" --agent my_agent --workbench quant --execution-adapter read_only_summary --task-type read_only_repo_summary --project . --output json",
        "description": "Create a manual task record for the authorized read-only adapter.",
        "mutates_when_run": True,
        "safety_note": "Queue metadata only; does not execute when manually run.",
    },
    {
        "id": "objectives_tasks.list_tasks",
        "group_id": "objectives_tasks",
        "title": "List tasks",
        "command": "harness tasks list --project .",
        "description": "List manual task queue records.",
        "mutates_when_run": False,
        "safety_note": "Read-only queue inspection.",
    },
    {
        "id": "objectives_tasks.graph",
        "group_id": "objectives_tasks",
        "title": "Inspect task graph",
        "command": "harness tasks graph --project . --output json",
        "description": "Show task/objective dependency graph.",
        "mutates_when_run": False,
        "safety_note": "Read-only graph output.",
    },
    {
        "id": "daemon_control.run_once",
        "group_id": "daemon_control",
        "title": "Lease one eligible task",
        "command": "harness daemon run-once --project . --output json",
        "description": "Acquire one daemon lease without executing work.",
        "mutates_when_run": True,
        "safety_note": "Lease-only control-plane mutation when manually run.",
    },
    {
        "id": "daemon_control.inspect_lease",
        "group_id": "daemon_control",
        "title": "Inspect a lease",
        "command": "harness daemon inspect-lease task_lease_abc123 --project . --output json",
        "description": "Inspect lease/task/attempt/run linkage.",
        "mutates_when_run": False,
        "safety_note": "Read-only lease inspection.",
    },
    {
        "id": "read_only_adapter.execute",
        "group_id": "read_only_adapter",
        "title": "Execute authorized read-only adapter",
        "command": "harness daemon execute-read-only task_lease_abc123 --project . --output json",
        "description": "Bind an existing active lease to the read-only repo summary adapter.",
        "mutates_when_run": True,
        "safety_note": "Authorized bounded adapter only when manually run.",
    },
    {
        "id": "runtime_evidence.runs",
        "group_id": "runtime_evidence",
        "title": "List runs",
        "command": "harness runs --project .",
        "description": "List run records.",
        "mutates_when_run": False,
        "safety_note": "Read-only evidence inspection.",
    },
    {
        "id": "runtime_evidence.policy",
        "group_id": "runtime_evidence",
        "title": "Explain task policy",
        "command": "harness policy explain --subject-kind task --subject-id task_abc123 --project . --output json",
        "description": "Explain runtime effective policy for a task.",
        "mutates_when_run": False,
        "safety_note": "Read-only policy evidence.",
    },
    {
        "id": "runtime_evidence.artifacts",
        "group_id": "runtime_evidence",
        "title": "List artifacts",
        "command": "harness artifacts list run_abc123 --project . --output json",
        "description": "List artifact metadata and evidence status.",
        "mutates_when_run": False,
        "safety_note": "Metadata only; does not print artifact files.",
    },
    {
        "id": "packaging_smoke.wheel",
        "group_id": "packaging_smoke",
        "title": "Build local wheel",
        "command": "python3 -m pip wheel --no-deps --no-build-isolation -w /tmp/harness-wheel .",
        "description": "Build a local wheel for packaging smoke checks.",
        "mutates_when_run": True,
        "safety_note": "Writes only to the explicit temporary wheelhouse when manually run.",
    },
]

TUI_VIEW_SECTIONS = [
    {
        "id": "project_overview",
        "title": "Project Overview",
        "pane_ids": ["pixel_art", "overview", "guidance", "commands"],
    },
    {
        "id": "queue_daemon",
        "title": "Queue And Daemon",
        "pane_ids": ["tasks", "leases", "daemon"],
    },
    {
        "id": "agents_specs",
        "title": "Agents And Specs",
        "pane_ids": ["agents"],
    },
    {
        "id": "runtime_evidence",
        "title": "Runtime Evidence",
        "pane_ids": ["runs"],
    },
    {
        "id": "command_palette",
        "title": "Command Palette",
        "pane_ids": [
            "command_palette",
            "command_palette_orientation",
            "command_palette_agent_authoring",
            "command_palette_project_agents",
            "command_palette_built_in_specs",
            "command_palette_objectives_tasks",
            "command_palette_daemon_control",
            "command_palette_read_only_adapter",
            "command_palette_runtime_evidence",
            "command_palette_packaging_smoke",
            "command_palette_selected",
        ],
    },
    {
        "id": "safety",
        "title": "Safety",
        "pane_ids": ["safety"],
    },
]

TUI_NAVIGATION_HINTS = [
    {"key": "/", "label": "Focus chat command input"},
    {"key": "escape", "label": "Clear search"},
    {"key": "tab", "label": "Next pane"},
    {"key": "shift+tab", "label": "Previous pane"},
    {"key": "ctrl+q", "label": "Quit"},
    {"key": "enter", "label": "Send slash command"},
    {"key": "copy-only", "label": "Slash commands render command templates only"},
]

SLASH_COMMAND_ALIASES = {
    "help": "orientation.quickstart_agent",
    "home": "orientation.home",
    "quickstart": "orientation.quickstart_agent",
    "scaffold": "agent_authoring.scaffold",
    "validate": "agent_authoring.validate",
    "preview": "agent_authoring.preview",
    "import-agent": "project_agents.import",
    "agents": "project_agents.list",
    "agent": "project_agents.inspect",
    "specs": "built_in_specs.list",
    "spec": "built_in_specs.preview_agent",
    "task": "objectives_tasks.add_task",
    "tasks": "objectives_tasks.list_tasks",
    "graph": "objectives_tasks.graph",
    "lease": "daemon_control.run_once",
    "inspect-lease": "daemon_control.inspect_lease",
    "execute-read-only": "read_only_adapter.execute",
    "runs": "runtime_evidence.runs",
    "policy": "runtime_evidence.policy",
    "artifacts": "runtime_evidence.artifacts",
    "wheel": "packaging_smoke.wheel",
}


def build_tui_dashboard(project_root: Path) -> dict:
    initialized = (project_root / HARNESS_DIR / "harness.sqlite").exists()
    dashboard = {
        "schema_version": "harness.tui_dashboard/v1",
        "ok": True,
        "project_root": str(project_root),
        "initialized": initialized,
        "version": __version__,
        "pixel_art": list(TUI_PIXEL_ART),
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
            "id": "pixel_art",
            "title": "Agent Harness",
            "lines": dashboard["pixel_art"],
        },
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
                *(
                    [
                        f"State error: {dashboard['state_error']['type']}: {dashboard['state_error']['message']}",
                    ]
                    if dashboard.get("state_error")
                    else []
                ),
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


def build_command_palette() -> dict:
    return {
        "schema_version": "harness.tui_command_palette/v1",
        "ok": True,
        "groups": [dict(group) for group in COMMAND_PALETTE_GROUPS],
        "entries": [dict(entry) for entry in COMMAND_PALETTE_ENTRIES],
    }


def filter_command_palette(palette: dict, query: str) -> dict:
    normalized_query = query.strip().casefold()
    if not normalized_query:
        entries = [dict(entry) for entry in palette["entries"]]
    else:
        entries = [
            dict(entry)
            for entry in palette["entries"]
            if _palette_entry_matches(entry, normalized_query)
        ]
    group_ids = {entry["group_id"] for entry in entries}
    return {
        "schema_version": "harness.tui_command_palette_filter/v1",
        "ok": True,
        "query": query.strip(),
        "total_matches": len(entries),
        "groups": [dict(group) for group in palette["groups"] if group["id"] in group_ids],
        "entries": entries,
    }


def _palette_entry_matches(entry: dict, normalized_query: str) -> bool:
    haystack = " ".join(
        str(entry[key])
        for key in ("id", "group_id", "title", "command", "description", "safety_note")
    )
    return normalized_query in haystack.casefold()


def build_command_palette_panes(filtered_palette: dict) -> list[dict]:
    panes = [
        {
            "id": "command_palette",
            "title": "Command Palette",
            "lines": [
                "Copy-only command templates.",
                "The TUI displays commands for manual use; it does not execute or copy them.",
                f"Visible commands: {filtered_palette['total_matches']}",
            ],
        }
    ]
    entries_by_group: dict[str, list[dict]] = {}
    for entry in filtered_palette["entries"]:
        entries_by_group.setdefault(entry["group_id"], []).append(entry)
    for group in filtered_palette["groups"]:
        group_entries = entries_by_group.get(group["id"], [])
        panes.append(
            {
                "id": f"command_palette_{group['id']}",
                "title": f"Palette: {group['title']}",
                "lines": [
                    f"{entry['id']} | {entry['title']} | mutates_when_run={entry['mutates_when_run']}"
                    for entry in group_entries
                ]
                or ["none"],
            }
        )
    if filtered_palette["entries"]:
        entry = filtered_palette["entries"][0]
        panes.append(
            {
                "id": "command_palette_selected",
                "title": "Selected Command",
                "lines": [
                    f"ID: {entry['id']}",
                    f"Group: {entry['group_id']}",
                    f"Title: {entry['title']}",
                    f"Mutates when run: {entry['mutates_when_run']}",
                    "Command:",
                    entry["command"],
                    "Description:",
                    entry["description"],
                    "Safety:",
                    entry["safety_note"],
                ],
            }
        )
    else:
        panes.append(
            {
                "id": "command_palette_selected",
                "title": "Selected Command",
                "lines": ["No matching command template."],
            }
        )
    return panes


def build_slash_commands(palette: dict | None = None) -> dict:
    palette = palette or build_command_palette()
    entries_by_id = {entry["id"]: entry for entry in palette["entries"]}
    commands = []
    for name, entry_id in SLASH_COMMAND_ALIASES.items():
        entry = entries_by_id[entry_id]
        commands.append(
            {
                "name": name,
                "slash": f"/{name}",
                "entry_id": entry_id,
                "group_id": entry["group_id"],
                "title": entry["title"],
                "description": entry["description"],
                "command": entry["command"],
                "mutates_when_run": entry["mutates_when_run"],
                "safety_note": entry["safety_note"],
            }
        )
    return {
        "schema_version": "harness.tui_slash_commands/v1",
        "ok": True,
        "commands": commands,
    }


def filter_slash_commands(slash_commands: dict, query: str) -> dict:
    normalized_query = query.strip().lstrip("/").casefold()
    if not normalized_query:
        commands = [dict(command) for command in slash_commands["commands"]]
    else:
        commands = [
            dict(command)
            for command in slash_commands["commands"]
            if _slash_command_matches(command, normalized_query)
        ]
    return {
        "schema_version": "harness.tui_slash_command_filter/v1",
        "ok": True,
        "query": query.strip(),
        "total_matches": len(commands),
        "commands": commands,
    }


def _slash_command_matches(command: dict, normalized_query: str) -> bool:
    haystack = " ".join(
        str(command[key])
        for key in ("name", "entry_id", "group_id", "title", "description", "command", "safety_note")
    )
    return normalized_query in haystack.casefold()


def build_chat_welcome_message(project_root: Path) -> dict:
    return {
        "role": "assistant",
        "title": "Harness chat",
        "lines": [
            *TUI_PIXEL_ART,
            "",
            f"Project: {project_root}",
            "Type /help to list slash commands.",
            "Slash commands render existing CLI command templates for manual operator use.",
        ],
    }


def handle_slash_command(text: str, slash_commands: dict | None = None) -> dict:
    slash_commands = slash_commands or build_slash_commands()
    raw_text = text.strip()
    if not raw_text:
        return {
            "schema_version": "harness.tui_chat_response/v1",
            "ok": False,
            "kind": "empty",
            "request": text,
            "messages": [
                {
                    "role": "assistant",
                    "title": "No input",
                    "lines": ["Type /help to list available slash commands."],
                }
            ],
        }
    if not raw_text.startswith("/"):
        return {
            "schema_version": "harness.tui_chat_response/v1",
            "ok": False,
            "kind": "plain_text_unsupported",
            "request": text,
            "messages": [
                {
                    "role": "assistant",
                    "title": "Slash commands only",
                    "lines": ["This local TUI accepts slash commands. Type /help to list them."],
                }
            ],
        }

    command_name = raw_text[1:].split(maxsplit=1)[0]
    if command_name in {"help", "commands"}:
        filtered = filter_slash_commands(slash_commands, "")
        return {
            "schema_version": "harness.tui_chat_response/v1",
            "ok": True,
            "kind": "help",
            "request": text,
            "messages": [
                {
                    "role": "assistant",
                    "title": "Slash commands",
                    "lines": [
                        f"{command['slash']} - {command['title']}"
                        for command in filtered["commands"]
                    ],
                }
            ],
        }

    filtered = filter_slash_commands(slash_commands, command_name)
    exact_matches = [
        command for command in filtered["commands"] if command["name"] == command_name
    ]
    if len(exact_matches) == 1:
        command = exact_matches[0]
        return {
            "schema_version": "harness.tui_chat_response/v1",
            "ok": True,
            "kind": "command_template",
            "request": text,
            "command": command,
            "messages": [
                {
                    "role": "assistant",
                    "title": command["title"],
                    "lines": [
                        f"Slash: {command['slash']}",
                        f"Mutates when run manually: {command['mutates_when_run']}",
                        "Command:",
                        command["command"],
                        "Description:",
                        command["description"],
                        "Safety:",
                        command["safety_note"],
                    ],
                }
            ],
        }
    if filtered["commands"]:
        return {
            "schema_version": "harness.tui_chat_response/v1",
            "ok": False,
            "kind": "ambiguous",
            "request": text,
            "messages": [
                {
                    "role": "assistant",
                    "title": "Matching slash commands",
                    "lines": [
                        f"{command['slash']} - {command['title']}"
                        for command in filtered["commands"][:10]
                    ],
                }
            ],
        }
    return {
        "schema_version": "harness.tui_chat_response/v1",
        "ok": False,
        "kind": "unknown",
        "request": text,
        "messages": [
            {
                "role": "assistant",
                "title": "Unknown slash command",
                "lines": [f"No slash command matched {raw_text}.", "Type /help to list available commands."],
            }
        ],
    }


def render_chat_message(message: dict) -> str:
    role = message["role"]
    title = message.get("title") or role
    lines = [f"{role}: {title}", ""]
    lines.extend(str(line) for line in message.get("lines", []))
    return "\n".join(lines)


def build_tui_view_model(filtered: dict, filtered_palette: dict) -> dict:
    no_matches = not filtered["panes"] and not filtered_palette["entries"]
    dashboard_panes = [dict(pane) for pane in filtered["panes"]]
    palette_panes = [] if no_matches else build_command_palette_panes(filtered_palette)
    panes_by_id = {pane["id"]: pane for pane in [*dashboard_panes, *palette_panes]}
    sections = []
    ordered_panes = []
    for section in TUI_VIEW_SECTIONS:
        section_panes = [
            dict(panes_by_id[pane_id])
            for pane_id in section["pane_ids"]
            if pane_id in panes_by_id
        ]
        if not section_panes:
            continue
        sections.append(
            {
                "id": section["id"],
                "title": section["title"],
                "pane_ids": [pane["id"] for pane in section_panes],
                "pane_count": len(section_panes),
            }
        )
        ordered_panes.extend(section_panes)
    empty_state = None
    if no_matches:
        empty_state = {
            "title": "No matches",
            "message": "No matching panes or command templates.",
            "query": filtered["query"] or filtered_palette["query"],
        }
    return {
        "schema_version": "harness.tui_view/v1",
        "ok": True,
        "query": filtered["query"] or filtered_palette["query"],
        "sections": sections,
        "panes": ordered_panes,
        "pane_order": [pane["id"] for pane in ordered_panes],
        "navigation_hints": [dict(hint) for hint in TUI_NAVIGATION_HINTS],
        "empty_state": empty_state,
        "search": {
            "dashboard_matches": filtered["total_matches"],
            "dashboard_panes": len(filtered["panes"]),
            "palette_matches": filtered_palette["total_matches"],
            "palette_groups": len(filtered_palette["groups"]),
        },
    }


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
    lines = ["Agent Harness", "", *dashboard["pixel_art"]]
    for pane in build_tui_panes(dashboard):
        pane_lines = _render_pane_content(pane).splitlines()
        lines.extend(["", pane_lines[0]])
        lines.extend(f"  {line}" for line in pane_lines[2:])
    lines.extend(["", "Press q to exit."])
    return "\n".join(lines)


def render_filter_status(filtered: dict) -> str:
    query = filtered["query"] or "none"
    pane_count = len(filtered["panes"])
    return f"Search: {query} | Matches: {filtered['total_matches']} | Panes: {pane_count}"


def render_palette_status(filtered_palette: dict) -> str:
    query = filtered_palette["query"] or "none"
    return (
        f"Palette search: {query} | Commands: {filtered_palette['total_matches']} | "
        f"Groups: {len(filtered_palette['groups'])}"
    )


def render_view_status(view: dict) -> str:
    query = view["query"] or "none"
    search = view["search"]
    return (
        f"View search: {query} | Sections: {len(view['sections'])} | Panes: {len(view['panes'])} | "
        f"Dashboard matches: {search['dashboard_matches']} | Palette commands: {search['palette_matches']}"
    )


def _render_pane_content(pane: dict) -> str:
    title = pane["title"]
    if "match_count" in pane:
        title = f"{title} ({pane['match_count']})"
    return "\n".join([title, "", *[str(line) for line in pane["lines"]]])


def _render_section_content(section: dict) -> str:
    return "\n".join(
        [
            section["title"],
            "",
            f"Panes: {section['pane_count']}",
            f"IDs: {', '.join(section['pane_ids'])}",
        ]
    )


def _render_navigation_hints(view: dict) -> str:
    return " | ".join(f"{hint['key']}: {hint['label']}" for hint in view["navigation_hints"])


def run_read_only_tui(project_root: Path) -> None:
    from textual.app import App, ComposeResult
    from textual.containers import Horizontal, Vertical, VerticalScroll
    from textual.widgets import Footer, Header, Input, Static

    dashboard = build_tui_dashboard(project_root)
    panes = build_tui_panes(dashboard)
    palette = build_command_palette()
    slash_commands = build_slash_commands(palette)
    initial_filter = filter_tui_panes(panes, "")
    initial_palette_filter = filter_command_palette(palette, "")
    initial_view = build_tui_view_model(initial_filter, initial_palette_filter)
    initial_messages = [build_chat_welcome_message(project_root)]

    class HarnessReadOnlyTui(App):
        CSS = """
        #layout {
            height: 1fr;
        }

        #chat {
            width: 2fr;
            border: round $surface;
            margin: 1 0 1 1;
            padding: 1;
        }

        #side {
            width: 1fr;
            border: round $surface;
            margin: 1 1 1 0;
            padding: 1;
        }

        #prompt {
            margin: 0 1 1 1;
        }

        .message {
            border: round $surface;
            margin: 0 0 1 0;
            padding: 1;
        }

        .message:focus {
            border: round $accent;
        }

        .pane {
            margin: 0 0 1 0;
            padding: 0 1;
        }

        .section {
            margin: 1 0 0 0;
            padding: 0 1;
            text-style: bold;
        }
        """
        BINDINGS = [
            ("ctrl+q", "quit", "Quit"),
            ("/", "focus_search", "Search"),
            ("escape", "clear_search", "Clear input"),
            ("tab", "focus_next", "Next"),
            ("shift+tab", "focus_previous", "Previous"),
        ]

        def __init__(self) -> None:
            super().__init__()
            self._messages = [dict(message) for message in initial_messages]

        def compose(self) -> ComposeResult:
            yield Header(show_clock=False)
            with Horizontal(id="layout"):
                with VerticalScroll(id="chat"):
                    yield Static("", id="chat-content")
                with VerticalScroll(id="side"):
                    yield Static(render_view_status(initial_view), id="search-status")
                    yield Static(_render_navigation_hints(initial_view), id="palette-status")
                    yield Static("", id="slash-status")
                    yield Static("", id="pane-container")
            yield Input(placeholder="Type /help or a slash command", id="prompt")
            yield Footer()

        def on_mount(self) -> None:
            self.query_one("#prompt", Input).focus()
            self._render_chat()
            self._render_view(initial_view)

        def on_input_changed(self, event: Input.Changed) -> None:
            if event.input.id == "prompt":
                self._render_view(
                    build_tui_view_model(
                        filter_tui_panes(panes, event.value),
                        filter_command_palette(palette, event.value),
                    )
                )
                filtered_slash = filter_slash_commands(slash_commands, event.value)
                self.query_one("#slash-status", Static).update(
                    f"Slash commands: {filtered_slash['total_matches']}"
                )

        def on_input_submitted(self, event: Input.Submitted) -> None:
            if event.input.id != "prompt":
                return
            request = event.value.strip()
            if not request:
                return
            self._messages.append({"role": "user", "title": request, "lines": []})
            response = handle_slash_command(request, slash_commands)
            self._messages.extend(response["messages"])
            event.input.value = ""
            self._render_chat()
            self._render_view(initial_view)

        def action_focus_search(self) -> None:
            self.query_one("#prompt", Input).focus()

        def action_clear_search(self) -> None:
            prompt = self.query_one("#prompt", Input)
            prompt.value = ""
            self._render_view(initial_view)

        def _render_chat(self) -> None:
            transcript = "\n\n".join(render_chat_message(message) for message in self._messages)
            self.query_one("#chat-content", Static).update(transcript)

        def _render_view(self, view: dict) -> None:
            self.query_one("#search-status", Static).update(render_view_status(view))
            self.query_one("#palette-status", Static).update(_render_navigation_hints(view))
            container = self.query_one("#pane-container", Static)
            if view["empty_state"]:
                container.update(view["empty_state"]["message"])
                return
            panes_by_id = {pane["id"]: pane for pane in view["panes"]}
            rendered = []
            for section in view["sections"]:
                rendered.extend([_render_section_content(section), ""])
                for pane_id in section["pane_ids"]:
                    pane = panes_by_id[pane_id]
                    rendered.extend([_render_pane_content(pane), ""])
            container.update("\n".join(rendered).strip())

    HarnessReadOnlyTui().run()
