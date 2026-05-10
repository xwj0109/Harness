from __future__ import annotations

from pathlib import Path

from harness.operator_context import build_tui_dashboard


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
        "pane_ids": ["overview", "guidance", "commands"],
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
    {"key": "/", "label": "Search"},
    {"key": "escape", "label": "Clear"},
    {"key": "tab", "label": "Next"},
    {"key": "shift+tab", "label": "Previous"},
    {"key": "ctrl+p/f2", "label": "Palette"},
    {"key": "c", "label": "Collapse"},
    {"key": "shift+c", "label": "Expand"},
    {"key": "ctrl+q", "label": "Quit"},
    {"key": "enter", "label": "Send"},
    {"key": "copy-only", "label": "Read-only context"},
]

TUI_FOCUS_MODES = frozenset({"dashboard", "palette"})
RIGHT_PANEL_SECTION_IDS = ("project", "now", "queue", "recent", "adapters", "next", "commands")

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


def build_right_panel_model(
    dashboard: dict,
    view_state: dict | None,
    query: str,
    focus_mode: str,
) -> dict:
    state = view_state or {}
    mode = focus_mode if focus_mode in TUI_FOCUS_MODES else "dashboard"
    normalized_query = query.strip().casefold()
    collapsed_ids = {
        str(section_id)
        for section_id in state.get("collapsed_section_ids", set())
        if str(section_id) in RIGHT_PANEL_SECTION_IDS
    }
    palette = state.get("palette") or build_command_palette()
    sections = _right_panel_base_sections(dashboard, state)
    command_matches = _right_panel_command_rows(palette, query, mode=mode)
    if mode == "palette" or normalized_query:
        command_section = {
            "id": "commands",
            "title": "Commands",
            "rows": command_matches or ["No matching commands."],
        }
        sections = [*sections, command_section]
    if normalized_query and mode == "dashboard":
        sections = _filter_right_panel_sections(sections, normalized_query)
    for section in sections:
        section["collapsed"] = section["id"] in collapsed_ids
        section["match_count"] = len(section.get("rows", []))
    active_index = int(state.get("active_section_index", 0) or 0)
    active_section_id = sections[active_index % len(sections)]["id"] if sections else None
    empty_state = None
    if not sections:
        empty_state = {
            "title": "No matches",
            "message": "No matches. Try /help, tasks, runs, adapters.",
            "query": query.strip(),
        }
    return {
        "schema_version": "harness.tui_right_panel/v1",
        "ok": True,
        "mode": mode,
        "query": query.strip(),
        "sections": sections,
        "active_section_id": active_section_id,
        "collapsed_section_ids": sorted(collapsed_ids),
        "empty_state": empty_state,
        "summary": {
            "initialized": bool(dashboard.get("initialized")),
            "tasks_total": dashboard["summary"]["tasks_total"],
            "active_leases": dashboard["summary"]["active_leases"],
            "recent_runs": dashboard["summary"]["recent_runs"],
            "registered_adapters": len(dashboard.get("registered_adapters", [])),
        },
        "search": {
            "context_matches": sum(section.get("match_count", 0) for section in sections),
            "command_matches": len(command_matches),
        },
        "navigation_hints": [dict(hint) for hint in TUI_NAVIGATION_HINTS],
    }


def _right_panel_base_sections(dashboard: dict, state: dict) -> list[dict]:
    summary = dashboard["summary"]
    active_orchestrator = state.get("active_orchestrator") or "coding_orchestrator"
    chat_mode = state.get("chat_mode") or "normal"
    branch = dashboard.get("branch") or "unknown"
    sections = [
        {
            "id": "project",
            "title": "Project",
            "rows": [
                f"{'Ready' if dashboard.get('initialized') else 'Setup needed'} | {Path(dashboard['project_root']).name}",
                f"Branch: {branch}",
                f"Mode: {chat_mode}",
                f"Orchestrator: {active_orchestrator}",
            ],
        },
        {
            "id": "now",
            "title": "Now",
            "rows": _right_panel_now_rows(dashboard),
        },
        {
            "id": "queue",
            "title": "Queue",
            "rows": _right_panel_queue_rows(dashboard),
        },
        {
            "id": "recent",
            "title": "Recent",
            "rows": _right_panel_recent_rows(dashboard),
        },
        {
            "id": "adapters",
            "title": "Adapters",
            "rows": _right_panel_adapter_rows(dashboard),
        },
        {
            "id": "next",
            "title": "Next",
            "rows": _right_panel_next_rows(dashboard),
        },
    ]
    return sections


def _right_panel_now_rows(dashboard: dict) -> list[str]:
    if dashboard.get("active_leases"):
        lease = dashboard["active_leases"][0]
        return [f"Running: {lease['task_id']}", f"Lease: {lease['id']}"]
    waiting = dashboard["task_status_counts"].get("waiting_approval", 0)
    if waiting:
        return [f"Needs approval: {waiting} task{'s' if waiting != 1 else ''}"]
    ready = dashboard["task_status_counts"].get("ready", 0)
    if ready:
        return [f"Ready: {ready} task{'s' if ready != 1 else ''}", "Action: /run or lease next"]
    if dashboard.get("recent_runs"):
        run = dashboard["recent_runs"][0]
        return [f"Latest run: {run['status']}", run["id"]]
    return ["Idle", "Ask Harness what to do next."]


def _right_panel_queue_rows(dashboard: dict) -> list[str]:
    labels = {
        "ready": "Ready",
        "leased": "Running",
        "waiting_approval": "Needs approval",
        "blocked": "Blocked",
        "failed": "Failed",
        "succeeded": "Done",
    }
    rows = [
        f"{label}: {dashboard['task_status_counts'].get(status, 0)}"
        for status, label in labels.items()
        if dashboard["task_status_counts"].get(status, 0)
    ]
    if not rows:
        rows = ["No queued tasks."]
    rows.append(
        "Total: "
        f"{dashboard['summary']['tasks_total']} | "
        f"Objectives: {dashboard['summary']['objectives']} | "
        f"Leases: {dashboard['summary']['active_leases']}"
    )
    return rows


def _right_panel_recent_rows(dashboard: dict) -> list[str]:
    rows = []
    if dashboard.get("tasks"):
        task = dashboard["tasks"][0]
        rows.append(f"Task: {task['status']} | {task['title']}")
    if dashboard.get("recent_runs"):
        run = dashboard["recent_runs"][0]
        rows.append(f"Run: {run['status']} | {run.get('task_type') or 'unknown'}")
    return rows or ["No recent task or run."]


def _right_panel_adapter_rows(dashboard: dict) -> list[str]:
    adapters = [adapter["id"] for adapter in dashboard.get("registered_adapters", [])]
    if not adapters:
        return ["No registered adapters."]
    return [", ".join(adapters[:4])]


def _right_panel_next_rows(dashboard: dict) -> list[str]:
    if not dashboard.get("initialized"):
        return ["/init", "Then try: summarize this repo"]
    if dashboard.get("active_leases"):
        return ["/run", "/leases"]
    if dashboard["task_status_counts"].get("ready", 0):
        return ["lease the next task", "run the registered adapter"]
    return ["summarize this repo", "/tasks /runs /adapters"]


def _right_panel_command_rows(palette: dict, query: str, *, mode: str) -> list[str]:
    if mode == "palette":
        filtered = filter_command_palette(palette, query)
        entries = filtered["entries"]
    elif query.strip():
        filtered = filter_command_palette(palette, query)
        entries = filtered["entries"][:5]
    else:
        entries = []
    rows = []
    for entry in entries[:8]:
        rows.append(f"{entry['title']} | {entry['command']}")
    return rows


def _filter_right_panel_sections(sections: list[dict], normalized_query: str) -> list[dict]:
    filtered = []
    for section in sections:
        title_matches = normalized_query in section["title"].casefold()
        rows = [row for row in section["rows"] if normalized_query in str(row).casefold()]
        if title_matches and not rows:
            rows = section["rows"]
        if title_matches or rows:
            filtered.append({**section, "rows": rows})
    return filtered


def render_right_panel(model: dict) -> str:
    if model.get("empty_state"):
        return model["empty_state"]["message"]
    lines = []
    active_id = model.get("active_section_id")
    for section in model["sections"]:
        marker = "> " if section["id"] == active_id else "  "
        collapsed = section.get("collapsed", False)
        lines.append(f"{marker}{section['title']}")
        if collapsed:
            lines.append("  hidden")
            lines.append("")
            continue
        for row in section.get("rows", []):
            lines.append(f"  {row}")
        lines.append("")
    return "\n".join(lines).strip()


def render_right_panel_status(model: dict) -> str:
    mode = model.get("mode", "dashboard")
    query = model.get("query") or "ready"
    active = model.get("active_section_id") or "none"
    if mode == "palette":
        return f"Context | palette | {model['search']['command_matches']} commands | {query}"
    return f"Context | {active} | {query}"


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
            f"Project: {project_root}",
            "Type naturally to chat with the supervised Codex-backed assistant.",
            "Slash commands inspect Harness state and prepare explicit actions.",
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


def render_pixel_art():
    from rich.console import Group
    from rich.text import Text

    from harness.tui_assets.pixel_art import TUI_PIXEL_ART_HALF_BLOCKS

    lines = []
    for row in TUI_PIXEL_ART_HALF_BLOCKS:
        line = Text()
        for foreground, background in row:
            line.append("▀", style=f"{foreground} on {background}")
        lines.append(line)
    return Group(*lines)


def normalize_tui_collapsed_sections(collapsed_section_ids: set[str] | list[str] | tuple[str, ...] | None) -> list[str]:
    if not collapsed_section_ids:
        return []
    valid_section_ids = {section["id"] for section in TUI_VIEW_SECTIONS}
    return sorted(str(section_id) for section_id in collapsed_section_ids if str(section_id) in valid_section_ids)


def build_focused_tui_view_model(
    panes: list[dict],
    palette: dict,
    query: str,
    *,
    focus_mode: str = "dashboard",
    collapsed_section_ids: set[str] | list[str] | tuple[str, ...] | None = None,
) -> dict:
    mode = focus_mode if focus_mode in TUI_FOCUS_MODES else "dashboard"
    if mode == "palette":
        filtered = filter_tui_panes(panes, "")
        filtered_palette = filter_command_palette(palette, query)
    else:
        filtered = filter_tui_panes(panes, query)
        filtered_palette = filter_command_palette(palette, query)
    return build_tui_view_model(
        filtered,
        filtered_palette,
        focus_mode=mode,
        collapsed_section_ids=collapsed_section_ids,
    )


def build_tui_view_model(
    filtered: dict,
    filtered_palette: dict,
    *,
    focus_mode: str = "dashboard",
    collapsed_section_ids: set[str] | list[str] | tuple[str, ...] | None = None,
) -> dict:
    mode = focus_mode if focus_mode in TUI_FOCUS_MODES else "dashboard"
    collapsed_ids = set(normalize_tui_collapsed_sections(collapsed_section_ids))
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
        collapsed = section["id"] in collapsed_ids
        sections.append(
            {
                "id": section["id"],
                "title": section["title"],
                "pane_ids": [pane["id"] for pane in section_panes],
                "pane_count": len(section_panes),
                "collapsed": collapsed,
            }
        )
        if not collapsed:
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
        "focus_mode": mode,
        "collapsed_section_ids": sorted(collapsed_ids),
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
    lines = ["Agent Harness"]
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
    collapsed = len(view.get("collapsed_section_ids", []))
    focus_mode = view.get("focus_mode", "dashboard")
    return (
        f"View search: {query} | Focus: {focus_mode} | Collapsed: {collapsed} | "
        f"Sections: {len(view['sections'])} | Panes: {len(view['panes'])} | "
        f"Dashboard matches: {search['dashboard_matches']} | Palette commands: {search['palette_matches']}"
    )


def _render_pane_content(pane: dict) -> str:
    title = pane["title"]
    if "match_count" in pane:
        title = f"{title} ({pane['match_count']})"
    return "\n".join([title, "", *[str(line) for line in pane["lines"]]])


def _render_section_content(section: dict) -> str:
    state = "collapsed" if section.get("collapsed") else "expanded"
    return "\n".join(
        [
            section["title"],
            "",
            f"State: {state}",
            f"Panes: {section['pane_count']}",
            f"IDs: {', '.join(section['pane_ids'])}",
        ]
    )


def _render_navigation_hints(view: dict) -> str:
    return " | ".join(f"{hint['key']}: {hint['label']}" for hint in view["navigation_hints"])


def create_read_only_tui_app(project_root: Path):
    return create_harness_app(project_root)


def create_harness_app(project_root: Path, *, codex_like: bool = False):
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal, Vertical, VerticalScroll
    from textual.widgets import Footer, Header, Input, Static
    from harness.chat import ChatSessionState, handle_chat_input

    dashboard = build_tui_dashboard(project_root)
    panes = build_tui_panes(dashboard)
    palette = build_command_palette()
    slash_commands = build_slash_commands(palette)
    initial_view = build_right_panel_model(
        dashboard,
        {
            "palette": palette,
            "active_section_index": 0,
            "collapsed_section_ids": set(),
            "active_orchestrator": "coding_orchestrator",
            "chat_mode": "codex-like" if codex_like else "normal",
        },
        "",
        "dashboard",
    )
    initial_messages = [build_chat_welcome_message(project_root)]
    if codex_like:
        initial_messages.append(
            {
                "role": "assistant",
                "title": "Codex-Like Mode",
                "lines": [
                    "One confirmation creates Harness records and drives foreground registered-adapter dispatch.",
                    "Apply-back remains a separate explicit review.",
                ],
            }
        )

    class HarnessPromptInput(Input):
        def on_key(self, event) -> None:
            if event.key == "tab":
                event.prevent_default()
                event.stop()
                self.app.action_section_next()
            elif event.key in {"shift+tab", "backtab"}:
                event.prevent_default()
                event.stop()
                self.app.action_section_previous()
            elif event.key in {"ctrl+p", "f2"}:
                event.prevent_default()
                event.stop()
                self.app.action_toggle_palette_focus()

    class HarnessUnifiedApp(App):
        ENABLE_COMMAND_PALETTE = False
        theme = "textual-light"
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
            Binding("ctrl+q", "quit", "Quit", priority=True),
            Binding("escape", "clear_search", "Clear input", priority=True),
            Binding("tab", "section_next", "Next section", priority=True),
            Binding("shift+tab,backtab", "section_previous", "Previous section", priority=True),
            Binding("ctrl+p,f2", "toggle_palette_focus", "Palette focus", priority=True),
        ]

        def __init__(self) -> None:
            super().__init__()
            self._messages = [dict(message) for message in initial_messages]
            self._chat_state = ChatSessionState(codex_like_mode=codex_like)
            self._focus_mode = "dashboard"
            self._collapsed_section_ids: set[str] = set()
            self._section_cursor_index = 0

        def compose(self) -> ComposeResult:
            yield Header(show_clock=False)
            with Horizontal(id="layout"):
                with VerticalScroll(id="chat"):
                    yield Static(render_pixel_art(), id="pixel-art")
                    yield Static("", id="chat-content")
                with VerticalScroll(id="side"):
                    yield Static(render_right_panel_status(initial_view), id="search-status")
                    yield Static(_render_navigation_hints(initial_view), id="palette-status")
                    yield Static("", id="slash-status")
                    yield Static("", id="pane-container")
            yield HarnessPromptInput(placeholder="Ask Harness or type /help", id="prompt")
            yield Footer()

        def on_mount(self) -> None:
            self.query_one("#prompt", Input).focus()
            self._render_chat()
            self._render_current_view()

        def on_key(self, event) -> None:
            if isinstance(self.focused, Input):
                return
            if event.character == "c":
                event.prevent_default()
                event.stop()
                self.action_toggle_section_collapse()
            elif event.key == "shift+c" or event.character == "C":
                event.prevent_default()
                event.stop()
                self.action_expand_all_sections()

        def on_input_changed(self, event: Input.Changed) -> None:
            if event.input.id == "prompt":
                self._render_current_view()
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
            response = handle_chat_input(request, project_root, self._chat_state)
            self._messages.append(_chat_response_to_tui_message(response))
            event.input.value = ""
            self._render_chat()
            self._render_current_view()
            if response.get("kind") == "quit":
                self.exit()

        def action_clear_search(self) -> None:
            prompt = self.query_one("#prompt", Input)
            if prompt.value:
                prompt.value = ""
            else:
                self._focus_mode = "dashboard"
                self._collapsed_section_ids.clear()
                self._section_cursor_index = 0
                self._render_current_view()

        def action_section_next(self) -> None:
            self._move_section_cursor(1)

        def action_section_previous(self) -> None:
            self._move_section_cursor(-1)

        def action_toggle_palette_focus(self) -> None:
            self._focus_mode = "palette" if self._focus_mode == "dashboard" else "dashboard"
            self._render_current_view()

        def action_toggle_section_collapse(self) -> None:
            view = self._current_view()
            if not view["sections"]:
                return
            self._clamp_section_cursor(view)
            section_id = view["sections"][self._section_cursor_index]["id"]
            if section_id in self._collapsed_section_ids:
                self._collapsed_section_ids.remove(section_id)
            else:
                self._collapsed_section_ids.add(section_id)
            self._render_current_view()

        def action_expand_all_sections(self) -> None:
            self._collapsed_section_ids.clear()
            self._render_current_view()

        def _move_section_cursor(self, step: int) -> None:
            view = self._current_view()
            if not view["sections"]:
                self._section_cursor_index = 0
                return
            self._section_cursor_index = (self._section_cursor_index + step) % len(view["sections"])
            self._render_current_view()

        def _current_view(self) -> dict:
            prompt = self.query_one("#prompt", Input)
            refreshed_dashboard = build_tui_dashboard(project_root)
            return build_right_panel_model(
                refreshed_dashboard,
                {
                    "palette": palette,
                    "active_section_index": self._section_cursor_index,
                    "collapsed_section_ids": self._collapsed_section_ids,
                    "active_orchestrator": self._chat_state.selected_orchestrator_id or "coding_orchestrator",
                    "chat_mode": "codex-like" if self._chat_state.codex_like_mode else "normal",
                },
                prompt.value,
                focus_mode=self._focus_mode,
            )

        def _render_current_view(self) -> None:
            self._render_view(self._current_view())

        def _clamp_section_cursor(self, view: dict) -> None:
            if not view["sections"]:
                self._section_cursor_index = 0
            elif self._section_cursor_index >= len(view["sections"]):
                self._section_cursor_index = len(view["sections"]) - 1

        def _render_chat(self) -> None:
            transcript = "\n\n".join(render_chat_message(message) for message in self._messages)
            self.query_one("#chat-content", Static).update(transcript)

        def _render_view(self, view: dict) -> None:
            self._clamp_section_cursor(view)
            self.query_one("#search-status", Static).update(render_right_panel_status(view))
            self.query_one("#palette-status", Static).update(_render_navigation_hints(view))
            container = self.query_one("#pane-container", Static)
            container.update(render_right_panel(view))

    return HarnessUnifiedApp()


def run_harness_app(project_root: Path, *, codex_like: bool = False) -> None:
    create_harness_app(project_root, codex_like=codex_like).run()


def run_read_only_tui(project_root: Path) -> None:
    run_harness_app(project_root)


def _chat_response_to_tui_message(response: dict) -> dict:
    return {
        "role": "assistant",
        "title": response.get("title") or response.get("kind") or "Harness",
        "lines": response.get("lines", []),
    }
