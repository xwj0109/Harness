from __future__ import annotations

import re
import time
from pathlib import Path

from harness.left_pane import (
    build_left_pane_view,
    left_pane_list_item_labels,
    left_pane_visible_items,
    render_left_pane_detail,
    render_left_pane_footer,
    render_left_pane_header,
    selected_left_pane_item,
)
from harness.memory.sqlite_store import (
    SESSION_SCHEMA_REPAIR_MESSAGE,
    SQLiteStore,
    is_missing_session_schema_error,
)
from harness.operator_context import build_session_pane_projection, build_tui_dashboard
from harness.procedure_renderer import render_procedure_event
from harness.right_pane import (
    COCKPIT_SECTION_IDS,
    build_right_pane_cockpit_model,
    render_right_pane_cockpit,
    render_right_pane_detail,
    render_right_pane_status,
    render_right_pane_top_context,
)
from rich.markup import escape
from textual.markup import MarkupError


SESSION_PREVIOUS_KEYS = {"alt+up", "option+up", "meta+up", "ctrl+left", "ctrl+pageup"}
SESSION_NEXT_KEYS = {"alt+down", "option+down", "meta+down", "ctrl+right", "ctrl+pagedown"}
SESSION_DELETE_KEYS = {"ctrl+d"}
ENTER_KEYS = {"enter", "numpad_enter", "ctrl+m"}
PROMPT_NEWLINE_KEYS = {"shift+enter", "shift+numpad_enter", "ctrl+j"}
SESSION_PANE_FILTERS = ("open", "running", "archived", "all")
COMPOSER_FOOTER_HINTS = "Enter send · Shift+Enter newline · Ctrl+X M models · / commands · ? shortcuts"

COMMAND_PALETTE_GROUPS = [
    {"id": "orientation", "title": "Orientation"},
    {"id": "ui_controls", "title": "UI Controls"},
    {"id": "model_selection", "title": "Model Selection"},
    {"id": "agent_authoring", "title": "Agent Authoring"},
    {"id": "native_agents", "title": "Native Agents"},
    {"id": "project_agents", "title": "Project Agents"},
    {"id": "built_in_specs", "title": "Built-In Specs"},
    {"id": "objectives_tasks", "title": "Objectives And Tasks"},
    {"id": "daemon_control", "title": "Daemon Control Plane"},
    {"id": "registered_adapters", "title": "Registered Adapters"},
    {"id": "runtime_evidence", "title": "Runtime Evidence"},
    {"id": "sessions", "title": "Sessions"},
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
        "id": "ui_controls.clear_search",
        "group_id": "ui_controls",
        "title": "Clear search",
        "command": "ui:clear-search",
        "description": "Clear the active TUI search/composer text without submitting a prompt.",
        "mutates_when_run": False,
        "safety_note": "In-process UI state only.",
    },
    {
        "id": "ui_controls.palette_focus",
        "group_id": "ui_controls",
        "title": "Focus command palette",
        "command": "ui:focus-palette",
        "description": "Switch the side panel into command palette focus.",
        "mutates_when_run": False,
        "safety_note": "In-process UI state only.",
    },
    {
        "id": "ui_controls.dashboard_focus",
        "group_id": "ui_controls",
        "title": "Focus dashboard",
        "command": "ui:focus-dashboard",
        "description": "Switch the side panel back to dashboard focus.",
        "mutates_when_run": False,
        "safety_note": "In-process UI state only.",
    },
    {
        "id": "ui_controls.toggle_section",
        "group_id": "ui_controls",
        "title": "Collapse or expand current section",
        "command": "ui:toggle-section",
        "description": "Toggle the currently selected dashboard section.",
        "mutates_when_run": False,
        "safety_note": "In-process UI state only.",
    },
    {
        "id": "ui_controls.expand_all",
        "group_id": "ui_controls",
        "title": "Expand all sections",
        "command": "ui:expand-all",
        "description": "Expand every dashboard section.",
        "mutates_when_run": False,
        "safety_note": "In-process UI state only.",
    },
    {
        "id": "ui_controls.settings",
        "group_id": "ui_controls",
        "title": "Show TUI settings",
        "command": "ui:settings",
        "description": "Focus the read-only TUI settings catalog.",
        "mutates_when_run": False,
        "safety_note": "In-process UI state only; preferences are not persisted from the palette.",
    },
    {
        "id": "ui_controls.theme_cycle",
        "group_id": "ui_controls",
        "title": "Switch theme",
        "command": "ui:switch-theme",
        "description": "Open the TUI theme picker.",
        "mutates_when_run": False,
        "safety_note": "In-process UI state only; theme choice is not persisted from the palette.",
    },
    {
        "id": "ui_controls.theme_light",
        "group_id": "ui_controls",
        "title": "Switch to light theme",
        "command": "ui:set-theme light",
        "description": "Use the light TUI theme for this running app.",
        "mutates_when_run": False,
        "safety_note": "In-process UI state only; theme choice is not persisted from the palette.",
    },
    {
        "id": "ui_controls.theme_dark",
        "group_id": "ui_controls",
        "title": "Switch to dark theme",
        "command": "ui:set-theme dark",
        "description": "Use the dark TUI theme for this running app.",
        "mutates_when_run": False,
        "safety_note": "In-process UI state only; theme choice is not persisted from the palette.",
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
        "id": "native_agents.build",
        "group_id": "native_agents",
        "title": "Use build agent",
        "command": "harness \"describe the change\" --agent build --project . --output json",
        "description": "Create a session-linked isolated Codex edit task.",
        "mutates_when_run": True,
        "safety_note": "Queues isolated edit metadata; active-workspace direct Codex requires --mode direct.",
    },
    {
        "id": "native_agents.select_build",
        "group_id": "native_agents",
        "title": "Select build agent",
        "command": "ui:select-agent build",
        "description": "Set the TUI composer agent mode to build without running a task.",
        "mutates_when_run": False,
        "safety_note": "In-process UI state only; submitting work still uses Harness CLI/session policy.",
    },
    {
        "id": "native_agents.plan",
        "group_id": "native_agents",
        "title": "Use plan agent",
        "command": "harness \"plan the change\" --agent plan --project . --output json",
        "description": "Create a read-only session-local planning task.",
        "mutates_when_run": True,
        "safety_note": "Read/glob/grep/artifact-read only; active repo writes are forbidden.",
    },
    {
        "id": "native_agents.select_plan",
        "group_id": "native_agents",
        "title": "Select plan agent",
        "command": "ui:select-agent plan",
        "description": "Set the TUI composer agent mode to plan without running a task.",
        "mutates_when_run": False,
        "safety_note": "In-process UI state only; plan submissions remain read/glob/grep/artifact-read bounded.",
    },
    {
        "id": "native_agents.general",
        "group_id": "native_agents",
        "title": "Use general subagent",
        "command": "harness \"@general investigate this\" --project . --output json",
        "description": "Create a bounded read-only subagent placeholder task.",
        "mutates_when_run": True,
        "safety_note": "Read-only metadata and artifact-backed work; no shell, network, or active edits.",
    },
    {
        "id": "native_agents.explore",
        "group_id": "native_agents",
        "title": "Use explore subagent",
        "command": "harness \"@explore inspect this area\" --project . --output json",
        "description": "Create a bounded read-only exploration placeholder task.",
        "mutates_when_run": True,
        "safety_note": "Read-only metadata and artifact-backed work; no shell, network, or active edits.",
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
        "id": "objectives_tasks.add_repo_planning_task",
        "group_id": "objectives_tasks",
        "title": "Add repo planning task",
        "command": "harness tasks add --title \"Plan repo change\" --execution-adapter repo_planning --task-type repo_planning --project . --output json",
        "description": "Create a manual task record for the registered repo-planning adapter.",
        "mutates_when_run": True,
        "safety_note": "Queue metadata only; repo planning still requires lease, approval, and registered dispatch.",
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
        "id": "registered_adapters.execute_read_only",
        "group_id": "registered_adapters",
        "title": "Execute authorized read-only adapter",
        "command": "harness daemon execute-read-only task_lease_abc123 --project . --output json",
        "description": "Bind an existing active lease to the read-only repo summary adapter.",
        "mutates_when_run": True,
        "safety_note": "Compatibility command for the bounded read-only adapter when manually run.",
    },
    {
        "id": "registered_adapters.execute",
        "group_id": "registered_adapters",
        "title": "Dispatch registered adapter",
        "command": "harness daemon execute task_lease_abc123 --project . --output json",
        "description": "Dispatch one already-leased task through its registered adapter.",
        "mutates_when_run": True,
        "safety_note": "Registered adapter dispatch only; no adapter, unknown adapter, or unsafe metadata fails closed.",
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
        "id": "sessions.list",
        "group_id": "sessions",
        "title": "List sessions",
        "command": "harness session list --project .",
        "description": "List interactive session records.",
        "mutates_when_run": False,
        "safety_note": "Read-only session continuity inspection.",
    },
    {
        "id": "sessions.continue_last",
        "group_id": "sessions",
        "title": "Continue last session",
        "command": "harness \"continue this work\" --project . --continue",
        "description": "Append a prompt to the most recently updated non-archived session.",
        "mutates_when_run": True,
        "safety_note": "Creates a new message and may start a supervised foreground run when manually run.",
    },
    {
        "id": "sessions.tail",
        "group_id": "sessions",
        "title": "Tail a session",
        "command": "harness session tail sess_abc123 --project .",
        "description": "Replay persisted session events.",
        "mutates_when_run": False,
        "safety_note": "Read-only append-only event replay.",
    },
    {
        "id": "sessions.transcript",
        "group_id": "sessions",
        "title": "Show session transcript",
        "command": "harness session transcript sess_abc123 --project .",
        "description": "Reconstruct a session transcript from persisted messages and parts.",
        "mutates_when_run": False,
        "safety_note": "Read-only transcript reconstruction.",
    },
    {
        "id": "sessions.tools",
        "group_id": "sessions",
        "title": "List session tools",
        "command": "harness session tools --output json",
        "description": "Inspect low-risk session tool descriptors.",
        "mutates_when_run": False,
        "safety_note": "Descriptors are metadata only; they do not grant permission.",
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
        "pane_ids": ["overview", "models", "guidance", "commands"],
    },
    {
        "id": "sessions",
        "title": "Sessions",
        "pane_ids": ["sessions"],
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
        "pane_ids": ["runs", "terminal"],
    },
    {
        "id": "settings",
        "title": "Settings",
        "pane_ids": ["settings"],
    },
    {
        "id": "command_palette",
        "title": "Command Palette",
        "pane_ids": [
            "command_palette",
            "command_palette_orientation",
            "command_palette_ui_controls",
            "command_palette_model_selection",
            "command_palette_agent_authoring",
            "command_palette_project_agents",
            "command_palette_built_in_specs",
            "command_palette_objectives_tasks",
            "command_palette_daemon_control",
            "command_palette_registered_adapters",
            "command_palette_runtime_evidence",
            "command_palette_sessions",
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
    {"key": "ctrl+x m", "label": "Models"},
    {"key": "c", "label": "Collapse"},
    {"key": "shift+c", "label": "Expand"},
    {"key": "ctrl+q", "label": "Quit"},
    {"key": "enter", "label": "Details/Send"},
    {"key": "shift+enter", "label": "New line"},
    {"key": "safe-actions", "label": "UI-only actions"},
]

TUI_FOCUS_MODES = frozenset({"dashboard", "palette"})
RIGHT_PANEL_SECTION_IDS = COCKPIT_SECTION_IDS
RIGHT_PANEL_SECTION_ALIASES = {
    "action": "active_work",
    "now": "context",
    "sessions": "context",
    "progress": "active_work",
    "queue": "orchestrations",
    "recent": "evidence",
    "adapters": "context",
    "project": "context",
    "assistant": "context",
    "next": "attention",
    "project_overview": "context",
    "queue_daemon": "orchestrations",
    "runtime_evidence": "evidence",
    "agents_specs": "context",
    "settings": "context",
    "command_palette": "commands",
    "safety": "attention",
}

SLASH_COMMAND_ALIASES = {
    "help": "orientation.quickstart_agent",
    "home": "orientation.home",
    "clear": "ui_controls.clear_search",
    "palette": "ui_controls.palette_focus",
    "dashboard": "ui_controls.dashboard_focus",
    "toggle-section": "ui_controls.toggle_section",
    "expand-all": "ui_controls.expand_all",
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
    "plan-task": "objectives_tasks.add_repo_planning_task",
    "tasks": "objectives_tasks.list_tasks",
    "graph": "objectives_tasks.graph",
    "lease": "daemon_control.run_once",
    "inspect-lease": "daemon_control.inspect_lease",
    "execute-read-only": "registered_adapters.execute_read_only",
    "execute": "registered_adapters.execute",
    "runs": "runtime_evidence.runs",
    "sessions": "sessions.list",
    "session": "sessions.list",
    "settings": "ui_controls.settings",
    "theme": "ui_controls.theme_cycle",
    "light-mode": "ui_controls.theme_light",
    "dark-mode": "ui_controls.theme_dark",
    "continue-session": "sessions.continue_last",
    "tail-session": "sessions.tail",
    "transcript-session": "sessions.transcript",
    "session-tools": "sessions.tools",
    "build": "native_agents.build",
    "plan": "native_agents.plan",
    "general": "native_agents.general",
    "explore": "native_agents.explore",
    "policy": "runtime_evidence.policy",
    "artifacts": "runtime_evidence.artifacts",
    "wheel": "packaging_smoke.wheel",
}


FUNCTIONALITY_TABLE_GROUPS = [
    {"id": "suggested", "title": "Suggested"},
    {"id": "session", "title": "Session"},
    {"id": "agent", "title": "Agent"},
    {"id": "tasks", "title": "Tasks"},
    {"id": "adapters", "title": "Adapters"},
    {"id": "evidence", "title": "Evidence"},
    {"id": "provider", "title": "Provider"},
    {"id": "system", "title": "System"},
]

FUNCTIONALITY_TABLE_LAYOUT = [
    ("suggested", ["model", "continue-session", "runs"]),
    ("session", ["sessions", "continue-session", "tail-session", "transcript-session", "session-tools"]),
    ("agent", ["model", "build", "plan", "general", "explore", "scaffold", "validate", "preview", "agents", "agent", "import-agent"]),
    ("tasks", ["task", "plan-task", "tasks", "graph", "lease", "inspect-lease"]),
    ("adapters", ["execute-read-only", "execute"]),
    ("evidence", ["runs", "policy", "artifacts"]),
    ("provider", ["models", "model"]),
    ("system", ["home", "settings", "theme", "palette", "dashboard", "clear", "toggle-section", "expand-all", "help", "quickstart", "specs", "spec", "wheel"]),
]

FUNCTIONALITY_INVOKES = {
    "model": "ctrl+x m",
    "models": "/models",
    "clear": "esc",
    "palette": "ctrl+p",
    "home": "/home",
    "settings": "/settings",
    "theme": "ctrl+x t",
    "dark-mode": "/dark-mode",
    "light-mode": "/light-mode",
}

FUNCTIONALITY_TITLES = {
    "model": "Switch model",
    "models": "Model catalog",
    "sessions": "Switch session",
    "continue-session": "Continue session",
    "tasks": "Task queue",
    "runs": "Runs",
    "theme": "Switch theme",
    "dark-mode": "Switch to dark mode",
    "light-mode": "Switch to light mode",
}

THEME_DIALOG_ENTRIES = [
    {
        "id": "light",
        "title": "Light",
        "description": "Use a bright high-contrast Harness light theme.",
        "textual_theme": "harness-light",
    },
    {
        "id": "dark",
        "title": "Dark",
        "description": "Use the dark TUI theme for this running app.",
        "textual_theme": "textual-dark",
    },
    {
        "id": "system",
        "title": "System",
        "description": "Use Textual's standard light theme for this running app.",
        "textual_theme": "textual-light",
    },
]

FUNCTIONALITY_EVIDENCE = {
    "model": "session.model_selected",
    "models": "none",
    "task": "task id",
    "plan-task": "task id",
    "lease": "lease id",
    "execute": "run id/artifacts",
    "execute-read-only": "run id/artifacts",
    "continue-session": "session event/run id",
    "tail-session": "event stream",
    "transcript-session": "transcript",
    "wheel": "wheelhouse path",
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
                f"Recent sessions: {summary.get('recent_sessions', 0)}",
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
        *(
            [
                {
                    "id": "terminal",
                    "title": "Terminal Tabs",
                    "lines": _terminal_tab_pane_rows(dashboard),
                }
            ]
            if (dashboard.get("terminal_tabs") or {}).get("tab_count", 0)
            else []
        ),
        {
            "id": "settings",
            "title": "TUI Settings",
            "lines": _tui_settings_pane_rows(
                build_tui_settings_catalog(
                    (dashboard.get("active_session") or {}).get("ui_preferences") or {},
                    source="active_session" if dashboard.get("active_session") else "defaults",
                    session_id=(dashboard.get("active_session") or {}).get("id"),
                ),
            ),
        },
        {
            "id": "sessions",
            "title": "Recent Sessions",
            "lines": (
                [
                    (
                        f"{session['status']} "
                        f"{session.get('display_title') or session.get('title') or session.get('intent') or 'untitled'} "
                        f"cwd={session.get('cwd') or '.'} "
                        f"model={session.get('raw_model_ref') or 'default'} "
                        f"run={session.get('active_run_id') or 'none'}"
                    )
                    for session in dashboard.get("recent_sessions", [])
                ]
                or ["none"]
            )
            + (
                [
                    "",
                    "Timeline:",
                    *(dashboard.get("active_session", {}).get("timeline") or ["none"])[-5:],
                    *_active_session_ui_activation_rows(dashboard.get("active_session") or {}),
                    "",
                    "Transcript:",
                    *(dashboard.get("active_session", {}).get("transcript") or ["none"])[-3:],
                ]
                if dashboard.get("active_session")
                else []
            ),
        },
        {
            "id": "models",
            "title": "Models",
            "lines": _model_catalog_pane_rows(dashboard),
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


def _tui_settings_pane_rows(catalog: dict, *, source: str | None = None) -> list[str]:
    preferences = catalog.get("preferences") or {}
    themes = catalog.get("themes") or []
    keybindings = catalog.get("keybindings") or []
    settings = catalog.get("settings") or []
    source_label = source or catalog.get("source_label") or "defaults"
    return [
        f"Source: {source_label}",
        f"Session: {catalog.get('session_id') or 'none'}",
        f"Policy: {(catalog.get('policy_boundary') or {}).get('kind') or 'unknown'}",
        f"Evidence: {catalog.get('evidence_status') or 'unknown'}",
        "Preferences:",
        *[f"{key}={preferences[key]}" for key in sorted(preferences)],
        "Themes:",
        *[
            f"{theme['id']} textual={theme.get('textual_theme') or 'system'} default={theme.get('default', False)}"
            for theme in themes
        ],
        "Keybindings:",
        *[f"{binding['key']} -> {binding['action']}" for binding in keybindings],
        "Setting definitions:",
        *[
            f"{setting['key']} kind={setting['kind']} scope={setting['scope']} default={setting['default']}"
            for setting in settings
        ],
        f"Filesystem modified: {catalog.get('filesystem_modified', False)}",
        f"Process started: {catalog.get('process_started', False)}",
        f"Permission granting: {catalog.get('permission_granting', False)}",
        f"Preferences persisted: {catalog.get('preferences_persisted', False)}",
        f"Backend settings exposed: {catalog.get('backend_settings_exposed', False)}",
        f"Persist command: {catalog.get('persist_command') or 'none'}",
    ]


def _active_session_ui_activation_rows(active_session: dict) -> list[str]:
    activation = active_session.get("latest_ui_activation") or {}
    if not activation:
        return []
    return [
        "",
        (
            "Latest UI action: "
            f"{activation.get('entry_id') or 'unknown'} "
            f"action={activation.get('action_type') or 'unknown'} "
            f"source={activation.get('source') or 'unknown'}"
        ),
        (
            "UI flags: "
            f"command={activation.get('command_started', False)} "
            f"process={activation.get('process_started', False)} "
            f"filesystem={activation.get('filesystem_modified', False)} "
            f"permission={activation.get('permission_granting', False)} "
            f"authority={activation.get('authority_granting', False)}"
        ),
    ]


def build_command_palette(custom_commands: list[dict] | None = None, model_catalog: dict | None = None) -> dict:
    groups = [dict(group) for group in COMMAND_PALETTE_GROUPS]
    entries = [_with_palette_activation(entry) for entry in COMMAND_PALETTE_ENTRIES]
    entries.extend(_model_selection_palette_entries(model_catalog or {}))
    if custom_commands:
        groups.append({"id": "project_commands", "title": "Project Commands"})
        for command in custom_commands:
            entries.append(
                _with_palette_activation(
                    {
                        "id": f"project_commands.{command['name']}",
                        "group_id": "project_commands",
                        "title": command["title"],
                        "command": f"harness commands run {command['name']} --project .",
                        "description": command["description"],
                        "mutates_when_run": command.get("mutates_when_run"),
                        "safety_note": command["safety_note"],
                        "custom_command": True,
                        "command_id": command["id"],
                    }
                )
            )
    return {
        "schema_version": "harness.tui_command_palette/v1",
        "ok": True,
        "groups": groups,
        "entries": entries,
    }


def _model_selection_palette_entries(model_catalog: dict) -> list[dict]:
    models = model_catalog.get("models") or []
    providers = {provider.get("provider_id"): provider for provider in model_catalog.get("providers") or []}
    active = model_catalog.get("active_model") or {}
    active_ref = active.get("raw_model_ref")
    entries: list[dict] = []
    seen_refs: set[str] = set()
    for index, model in enumerate(models):
        raw_ref = str(model.get("raw_model_ref") or "").strip()
        if not raw_ref or raw_ref in seen_refs:
            continue
        seen_refs.add(raw_ref)
        provider = providers.get(model.get("provider_id")) or {}
        enabled = bool(provider.get("enabled", True))
        credential_status = str(provider.get("credential_status") or "unknown")
        boundary = str(provider.get("data_boundary") or (provider.get("metadata") or {}).get("data_boundary") or "unknown")
        suffix = "active" if raw_ref == active_ref else ("enabled" if enabled else "blocked")
        entries.append(
            _with_palette_activation(
                {
                    "id": f"model_selection.select_{index}",
                    "group_id": "model_selection",
                    "title": f"Select model {raw_ref}",
                    "command": f"ui:select-model {raw_ref}",
                    "description": f"{boundary} | credentials={credential_status} | {suffix}",
                    "mutates_when_run": True,
                    "safety_note": "Persists active session model metadata and validation evidence only; no provider call or fallback.",
                    "model_ref": raw_ref,
                    "provider_id": model.get("provider_id"),
                    "model_id": model.get("model_id"),
                    "provider_enabled": enabled,
                    "credential_status": credential_status,
                    "data_boundary": boundary,
                }
            )
        )
    return entries


_SAFE_PALETTE_UI_ACTIONS = {
    "orientation.home": {
        "type": "focus_section",
        "section_id": "project_overview",
        "focus_mode": "dashboard",
        "evidence_status": "ui_focus_in_memory",
        "state_fields": ["focus_mode", "active_section_id", "active_section_index"],
    },
    "ui_controls.clear_search": {
        "type": "clear_search",
        "focus_mode": "dashboard",
        "evidence_status": "ui_search_cleared_in_memory",
        "state_fields": ["focus_mode", "query"],
    },
    "ui_controls.palette_focus": {
        "type": "set_focus_mode",
        "focus_mode": "palette",
        "evidence_status": "ui_focus_in_memory",
        "state_fields": ["focus_mode"],
    },
    "ui_controls.dashboard_focus": {
        "type": "set_focus_mode",
        "focus_mode": "dashboard",
        "evidence_status": "ui_focus_in_memory",
        "state_fields": ["focus_mode"],
    },
    "ui_controls.toggle_section": {
        "type": "toggle_section",
        "evidence_status": "ui_section_toggle_in_memory",
        "state_fields": ["active_section_id", "collapsed_section_ids"],
    },
    "ui_controls.expand_all": {
        "type": "expand_all",
        "evidence_status": "ui_sections_expanded_in_memory",
        "state_fields": ["collapsed_section_ids"],
    },
    "ui_controls.settings": {
        "type": "focus_section",
        "section_id": "settings",
        "focus_mode": "dashboard",
        "evidence_status": "ui_focus_in_memory",
        "state_fields": ["focus_mode", "active_section_id", "active_section_index"],
    },
    "ui_controls.theme_cycle": {
        "type": "set_theme",
        "theme_id": "cycle",
        "evidence_status": "ui_theme_selected_in_memory",
        "state_fields": ["selected_theme"],
    },
    "ui_controls.theme_light": {
        "type": "set_theme",
        "theme_id": "light",
        "evidence_status": "ui_theme_selected_in_memory",
        "state_fields": ["selected_theme"],
    },
    "ui_controls.theme_dark": {
        "type": "set_theme",
        "theme_id": "dark",
        "evidence_status": "ui_theme_selected_in_memory",
        "state_fields": ["selected_theme"],
    },
    "native_agents.select_build": {
        "type": "select_agent",
        "agent_id": "build",
        "evidence_status": "ui_agent_selected_in_memory",
        "state_fields": ["selected_agent_id"],
    },
    "native_agents.select_plan": {
        "type": "select_agent",
        "agent_id": "plan",
        "evidence_status": "ui_agent_selected_in_memory",
        "state_fields": ["selected_agent_id"],
    },
    "sessions.list": {
        "type": "focus_section",
        "section_id": "sessions",
        "focus_mode": "dashboard",
        "evidence_status": "ui_focus_in_memory",
        "state_fields": ["focus_mode", "active_section_id", "active_section_index"],
    },
    "runtime_evidence.runs": {
        "type": "focus_section",
        "section_id": "runtime_evidence",
        "focus_mode": "dashboard",
        "evidence_status": "ui_focus_in_memory",
        "state_fields": ["focus_mode", "active_section_id", "active_section_index"],
    },
    "objectives_tasks.list_tasks": {
        "type": "focus_section",
        "section_id": "queue_daemon",
        "focus_mode": "dashboard",
        "evidence_status": "ui_focus_in_memory",
        "state_fields": ["focus_mode", "active_section_id", "active_section_index"],
    },
}


def _safe_palette_policy_boundary() -> dict:
    return {
        "kind": "safe_ui_activation",
        "source": "tui_command_palette",
        "command_execution_allowed": False,
        "provider_call_allowed": False,
        "shell_allowed": False,
        "adapter_dispatch_allowed": False,
        "child_process_allowed": False,
        "filesystem_mutation_allowed": False,
        "permission_grant_allowed": False,
        "authority_grant_allowed": False,
        "session_message_allowed": False,
        "in_memory_ui_state_only": True,
    }


def _palette_no_side_effect_flags() -> dict:
    return {
        "request_started": False,
        "command_started": False,
        "provider_started": False,
        "shell_started": False,
        "adapter_started": False,
        "child_process_started": False,
        "process_started": False,
        "filesystem_modified": False,
        "permission_granting": False,
        "authority_granting": False,
        "session_message_created": False,
    }


def _with_palette_activation(entry: dict) -> dict:
    item = dict(entry)
    if str(item.get("group_id")) == "model_selection" and item.get("model_ref"):
        item["activation"] = {
            "kind": "session_model_selection",
            "supported": True,
            "action": {
                "type": "select_model",
                "raw_model_ref": item["model_ref"],
                "provider_id": item.get("provider_id"),
                "model_id": item.get("model_id"),
                "provider_enabled": item.get("provider_enabled"),
                "credential_status": item.get("credential_status"),
                "data_boundary": item.get("data_boundary"),
                "evidence_status": "session_model_selection_requested",
                "state_fields": ["selected_model_ref"],
            },
            "evidence_status": "session_model_selection_requested",
            "policy_boundary": _model_selection_policy_boundary(),
            "blocked_reasons": [],
            **_palette_no_side_effect_flags(),
        }
        return item
    action = _SAFE_PALETTE_UI_ACTIONS.get(str(item.get("id")))
    if action:
        item["activation"] = {
            "kind": "ui_action",
            "supported": True,
            "action": dict(action),
            "evidence_status": "ui_only_in_memory",
            "policy_boundary": _safe_palette_policy_boundary(),
            "blocked_reasons": [],
            **_palette_no_side_effect_flags(),
        }
    else:
        item["activation"] = {
            "kind": "manual_command",
            "supported": False,
            "reason": "This palette entry is exposed as an explicit command preview and is not executed by the TUI.",
            "evidence_status": "manual_preview_only",
            "policy_boundary": _safe_palette_policy_boundary(),
            "blocked_reasons": ["manual_command_preview_only"],
            **_palette_no_side_effect_flags(),
        }
    return item


def activate_command_palette_entry(
    palette: dict,
    entry_id: str,
    view_state: dict | None = None,
) -> dict:
    no_side_effects = _palette_no_side_effect_flags()
    policy_boundary = _safe_palette_policy_boundary()
    entry = next((item for item in palette.get("entries", []) if item.get("id") == entry_id), None)
    if entry is None:
        return {
            "schema_version": "harness.tui_palette_activation/v1",
            "ok": False,
            "entry_id": entry_id,
            "error": "Command palette entry not found.",
            "activation_kind": "missing",
            "ui_action_applied": False,
            "evidence_status": "missing_entry",
            "policy_boundary": policy_boundary,
            "blocked_reasons": ["palette_entry_not_found"],
            **no_side_effects,
            "view_state": dict(view_state or {}),
        }
    activation = entry.get("activation") or {}
    if activation.get("kind") != "ui_action" or not activation.get("supported"):
        if activation.get("kind") == "session_model_selection" and activation.get("supported"):
            action = dict(activation.get("action") or {})
            state = dict(view_state or {})
            if action.get("raw_model_ref"):
                state["selected_model_ref"] = action["raw_model_ref"]
            return {
                "schema_version": "harness.tui_palette_activation/v1",
                "ok": True,
                "entry_id": entry_id,
                "activation_kind": "session_model_selection",
                "action": action,
                "ui_action_applied": False,
                "session_model_selection_requested": True,
                "evidence_status": action.get("evidence_status") or "session_model_selection_requested",
                "policy_boundary": activation.get("policy_boundary") or _model_selection_policy_boundary(),
                "blocked_reasons": [],
                **no_side_effects,
                "harness_state_modified": False,
                "view_state": state,
            }
        return {
            "schema_version": "harness.tui_palette_activation/v1",
            "ok": False,
            "entry_id": entry_id,
            "error": activation.get("reason") or "Palette entry is not an in-process TUI action.",
            "activation_kind": activation.get("kind") or "manual_command",
            "ui_action_applied": False,
            "evidence_status": activation.get("evidence_status") or "manual_preview_only",
            "policy_boundary": activation.get("policy_boundary") or policy_boundary,
            "blocked_reasons": activation.get("blocked_reasons") or ["manual_command_preview_only"],
            **no_side_effects,
            "command": entry.get("command"),
            "view_state": dict(view_state or {}),
        }
    state = dict(view_state or {})
    collapsed = set(normalize_right_panel_collapsed_sections(state.get("collapsed_section_ids")))
    action = dict(activation.get("action") or {})
    if action.get("type") == "focus_section":
        resolved_section_id = _right_panel_resolve_section_id(action.get("section_id"))
        state["focus_mode"] = action.get("focus_mode") or "dashboard"
        state["active_section_id"] = resolved_section_id
        state["requested_section_id"] = action.get("section_id")
        state["active_section_index"] = _right_panel_section_index(resolved_section_id)
    elif action.get("type") == "clear_search":
        state["focus_mode"] = action.get("focus_mode") or "dashboard"
        state["query"] = ""
    elif action.get("type") == "set_focus_mode":
        state["focus_mode"] = action.get("focus_mode") or "dashboard"
    elif action.get("type") == "toggle_section":
        section_id = _right_panel_resolve_section_id(state.get("active_section_id"))
        if not state.get("active_section_id"):
            section_id = _right_panel_section_id_at_index(state.get("active_section_index"))
        if section_id in collapsed:
            collapsed.remove(section_id)
        else:
            collapsed.add(section_id)
        state["active_section_id"] = section_id
        state["active_section_index"] = _right_panel_section_index(section_id)
        state["collapsed_section_ids"] = sorted(collapsed)
    elif action.get("type") == "expand_all":
        collapsed.clear()
        state["collapsed_section_ids"] = []
    elif action.get("type") == "select_agent":
        agent_id = str(action.get("agent_id") or "").strip()
        if agent_id:
            state["selected_agent_id"] = agent_id
    elif action.get("type") == "set_theme":
        requested_theme = str(action.get("theme_id") or "cycle")
        current_theme = str(state.get("selected_theme") or "light")
        state["selected_theme"] = _resolve_next_tui_theme(current_theme, requested_theme)
    local_state_changes = {
        "changed_fields": list(action.get("state_fields") or []),
        "creates_message": False,
        "starts_request": False,
        "executes_command": False,
        "mutates_filesystem": False,
        "grants_permission": False,
    }
    return {
        "schema_version": "harness.tui_palette_activation/v1",
        "ok": True,
        "entry_id": entry_id,
        "activation_kind": "ui_action",
        "action": action,
        "ui_action_applied": True,
        "evidence_status": action.get("evidence_status") or activation.get("evidence_status") or "ui_only_in_memory",
        "policy_boundary": activation.get("policy_boundary") or policy_boundary,
        "blocked_reasons": [],
        "local_state_changes": local_state_changes,
        **no_side_effects,
        "view_state": state,
}


def _resolve_next_tui_theme(current_theme: str, requested_theme: str) -> str:
    themes = ["light", "dark"]
    if requested_theme in themes:
        return requested_theme
    normalized_current = current_theme if current_theme in themes else "light"
    return themes[(themes.index(normalized_current) + 1) % len(themes)]


def _model_selection_policy_boundary() -> dict:
    boundary = _safe_palette_policy_boundary()
    return {
        **boundary,
        "kind": "session_model_selection",
        "session_metadata_mutation_allowed": True,
        "session_message_allowed": False,
        "in_memory_ui_state_only": False,
        "provider_call_allowed": False,
        "model_execution_allowed": False,
        "hidden_fallback_allowed": False,
    }


def _section_index(section_id: object) -> int:
    for index, section in enumerate(TUI_VIEW_SECTIONS):
        if section["id"] == section_id:
            return index
    return 0


def _right_panel_resolve_section_id(section_id: object) -> str:
    value = str(section_id or "").strip()
    value = RIGHT_PANEL_SECTION_ALIASES.get(value, value)
    return value if value in RIGHT_PANEL_SECTION_IDS else "active_work"


def _right_panel_section_index(section_id: object) -> int:
    resolved = _right_panel_resolve_section_id(section_id)
    try:
        return list(RIGHT_PANEL_SECTION_IDS).index(resolved)
    except ValueError:
        return 0


def _right_panel_section_id_at_index(index: object) -> str:
    try:
        value = int(index or 0)
    except (TypeError, ValueError):
        value = 0
    sections = list(RIGHT_PANEL_SECTION_IDS)
    return sections[value % len(sections)]


def _section_id_at_index(index: object) -> str:
    try:
        value = int(index or 0)
    except (TypeError, ValueError):
        value = 0
    if not TUI_VIEW_SECTIONS:
        return ""
    return str(TUI_VIEW_SECTIONS[value % len(TUI_VIEW_SECTIONS)]["id"])


TUI_SETTING_DEFINITIONS = [
    {
        "key": "theme",
        "label": "Theme",
        "kind": "choice",
        "default": "light",
        "choices": ["light", "dark", "system"],
        "scope": "session",
    },
    {
        "key": "terminal_font_size",
        "label": "Terminal font size",
        "kind": "integer",
        "default": 13,
        "min": 9,
        "max": 24,
        "scope": "session",
    },
    {
        "key": "keybinding_preset",
        "label": "Keybinding preset",
        "kind": "choice",
        "default": "harness",
        "choices": ["harness", "opencode-like"],
        "scope": "session",
    },
    {
        "key": "composer_mode",
        "label": "Composer mode",
        "kind": "choice",
        "default": "multiline",
        "choices": ["multiline", "single-line"],
        "scope": "session",
    },
]


def build_tui_settings_catalog(
    preferences: dict | None = None,
    *,
    source: str = "defaults",
    session_id: str | None = None,
) -> dict:
    normalized = normalize_tui_preferences(preferences or {})
    is_session_source = source == "active_session" and bool(session_id)
    return {
        "schema_version": "harness.tui_settings/v1",
        "ok": True,
        "source": "active_session" if is_session_source else "defaults",
        "source_label": "active session preferences" if is_session_source else "defaults",
        "session_id": session_id if is_session_source else None,
        "evidence_status": "read_only_settings_metadata",
        "policy_boundary": {
            "kind": "tui_settings_read_only",
            "source": "settings_catalog",
            "preference_persistence_allowed": False,
            "backend_settings_allowed": False,
            "process_start_allowed": False,
            "filesystem_mutation_allowed": False,
            "permission_grant_allowed": False,
            "authority_grant_allowed": False,
        },
        "settings": [dict(setting) for setting in TUI_SETTING_DEFINITIONS],
        "preferences": normalized,
        "preference_source": "session_ui_preferences" if is_session_source else "defaults",
        "persist_command": (
            f"harness session preferences {session_id} --project . --set key=value"
            if is_session_source
            else "harness session preferences <session-id> --project . --set key=value"
        ),
        "themes": [
            {"id": "light", "textual_theme": "harness-light", "default": True},
            {"id": "dark", "textual_theme": "textual-dark", "default": False},
            {"id": "system", "textual_theme": None, "default": False},
        ],
        "keybindings": [
            {"key": "ctrl+q", "action": "quit", "label": "Quit", "customizable": False},
            {"key": "escape", "action": "clear_search", "label": "Clear input", "customizable": True},
            {"key": "tab", "action": "section_next", "label": "Next section", "customizable": True},
            {"key": "shift+tab", "action": "section_previous", "label": "Previous section", "customizable": True},
            {"key": "ctrl+p", "action": "toggle_palette_focus", "label": "Palette focus", "customizable": True},
            {"key": "f2", "action": "toggle_palette_focus", "label": "Palette focus", "customizable": True},
        ],
        "preferences_persisted": False,
        "backend_settings_exposed": False,
        "authority_granting": False,
        "filesystem_modified": False,
        "process_started": False,
        "permission_granting": False,
    }


def normalize_tui_preferences(preferences: dict) -> dict:
    definitions = {setting["key"]: setting for setting in TUI_SETTING_DEFINITIONS}
    normalized = {key: definition["default"] for key, definition in definitions.items()}
    for key, value in preferences.items():
        if key not in definitions:
            continue
        definition = definitions[key]
        if definition["kind"] == "choice":
            text = str(value).strip()
            if text in definition["choices"]:
                normalized[key] = text
        elif definition["kind"] == "integer":
            try:
                number = int(value)
            except (TypeError, ValueError):
                continue
            normalized[key] = max(definition["min"], min(definition["max"], number))
    return normalized


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
                "Safe UI actions activate in-process; command entries remain manual previews.",
                "The TUI never starts providers, shells, adapters, or filesystem mutation from the palette.",
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
                    f"Activation: {entry.get('activation', {}).get('kind', 'manual_command')}",
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
    return build_right_pane_cockpit_model(dashboard, view_state, query, focus_mode)


def _right_panel_base_sections(dashboard: dict, state: dict) -> list[dict]:
    active_orchestrator = state.get("active_orchestrator") or "coding_orchestrator"
    chat_mode = state.get("chat_mode") or "normal"
    branch = dashboard.get("branch") or "unknown"
    sections = [
        {
            "id": "action",
            "title": "Action",
            "rows": _right_panel_action_rows(dashboard, state),
        },
        {
            "id": "now",
            "title": "Now",
            "rows": _right_panel_now_rows(dashboard, state),
        },
        {
            "id": "sessions",
            "title": "Sessions",
            "rows": _right_panel_session_rows(dashboard, state),
        },
        {
            "id": "progress",
            "title": "Progress",
            "rows": _right_panel_progress_rows(dashboard),
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
            "id": "project",
            "title": "Project",
            "rows": [
                f"State: {'ready' if dashboard.get('initialized') else 'needs setup'}",
                f"Root: {Path(dashboard['project_root']).name}",
                f"Branch: {branch}",
                f"Mode: {_status_label(chat_mode)}",
                f"Operator: {_humanize_identifier(active_orchestrator)}",
            ],
        },
        {
            "id": "assistant",
            "title": "Assistant",
            "rows": _right_panel_assistant_rows(dashboard, state),
        },
        {
            "id": "next",
            "title": "Next",
            "rows": _right_panel_next_rows(dashboard),
        },
    ]
    return sections


def _first_line(value: object, *, limit: int = 96) -> str:
    text = str(value or "").splitlines()[0].strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _humanize_identifier(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return "unknown"
    text = re.sub(r"[_./-]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or "unknown"


def _title_case_identifier(value: object) -> str:
    words = []
    for word in _humanize_identifier(value).split():
        lowered = word.casefold()
        if lowered == "ui":
            words.append("UI")
        elif lowered == "tui":
            words.append("TUI")
        else:
            words.append(word.title())
    return " ".join(words)


def _status_label(value: object) -> str:
    labels = {
        "setup_needed": "needs setup",
        "approval_required": "approval needed",
        "waiting_approval": "needs approval",
        "pending_contract": "needs confirmation",
        "ui_action": "UI updated",
        "in_flight": "running",
    }
    raw = str(value or "idle").strip()
    return labels.get(raw, _humanize_identifier(raw))


def _session_title(session: dict) -> str:
    return str(
        session.get("display_title")
        or session.get("title")
        or session.get("intent")
        or "Untitled session"
    )


def _task_title_by_id(dashboard: dict) -> dict[str, str]:
    return {
        str(task.get("id")): str(task.get("title") or "Untitled task")
        for task in dashboard.get("tasks", [])
        if task.get("id")
    }


def _task_title(dashboard: dict, task_id: object, *, fallback: str = "selected task") -> str:
    if not task_id:
        return fallback
    return _task_title_by_id(dashboard).get(str(task_id), fallback)


def _clean_event_summary(value: object) -> str:
    text = _first_line(value, limit=88)
    text = re.sub(r"^\d+\s+", "", text).strip()
    text = re.sub(
        r"\s*\([^)]*\b(?:artifact|lease|msg|perm|run|sess|task|todo)_[^)]+\)",
        "",
        text,
    )
    text = re.sub(
        r"\b(?:artifact|lease|msg|perm|run|sess|task|todo)_[A-Za-z0-9_./-]+",
        "item",
        text,
    )
    return text or "no events"


def _right_panel_active_signal(dashboard: dict, state: dict) -> str:
    if state.get("request_in_flight"):
        return "responding"
    return str((dashboard.get("live_activity") or {}).get("active_signal") or "idle")


def _approval_command(active_session: dict) -> str | None:
    operator = active_session.get("operator") or {}
    card = operator.get("approval_card") if isinstance(operator.get("approval_card"), dict) else {}
    command = card.get("command") or card.get("operation")
    return str(command) if command else None


def _first_blocker(progress: dict) -> tuple[str | None, str | None]:
    for task in progress.get("tasks", []) or []:
        blocked = task.get("blocked_state_explanations") or []
        if blocked:
            return str(blocked[0].get("code") or "blocked"), str(task.get("task_id") or "")
        reasons = task.get("blocked_reasons") or []
        if reasons:
            return str(reasons[0]), str(task.get("task_id") or "")
    reasons = progress.get("blocked_reasons") or []
    if reasons:
        return str(reasons[0]), None
    return None, None


def _right_panel_assistant_rows(dashboard: dict, state: dict) -> list[str]:
    chat_cfg = dashboard.get("chat") or {}
    model_catalog = dashboard.get("model_catalog") or {}
    active_model = model_catalog.get("active_model") or {}
    model_label = (
        active_model.get("raw_model_ref")
        or active_model.get("model_profile_id")
        or chat_cfg.get("default_model_profile")
        or state.get("model_profile")
        or "codex_cli"
    )
    rows = [
        f"Model: {model_label}",
        f"Provider: {_humanize_identifier(active_model.get('provider_id') or 'default')}",
        f"Mode: {_status_label(state.get('chat_mode') or chat_cfg.get('mode') or 'normal')}",
        "Safety: action contracts",
        "Fallback: explicit failure only",
    ]
    if active_model.get("blocked_reasons"):
        rows.append("Blocked: " + ", ".join(_humanize_identifier(reason) for reason in active_model["blocked_reasons"]))
    if model_catalog.get("models"):
        rows.append(f"Catalog models: {len(model_catalog['models'])}")
    latest_response = state.get("latest_response") or {}
    tool_results = latest_response.get("tool_results") or []
    if tool_results:
        rows.append("Tools: " + ", ".join(str(item.get("tool")) for item in tool_results[:4]))
    manifest = latest_response.get("context_manifest") or {}
    blocks = manifest.get("blocks") or []
    if blocks:
        rows.append("Context: " + ", ".join(str(block.get("kind")) for block in blocks[:4]))
    return rows


def _right_panel_action_rows(dashboard: dict, state: dict) -> list[str]:
    live_activity = dashboard.get("live_activity") or {}
    active_session = dashboard.get("active_session") or {}
    progress = dashboard.get("progress") or {}
    signal = _right_panel_active_signal(dashboard, state)
    contract = state.get("pending_action_contract")
    if contract:
        return [
            "State: needs confirmation",
            f"Pending: {contract.get('summary')}",
            f"Tool: {_humanize_identifier(contract.get('tool'))}",
            f"Risk: {_humanize_identifier(contract.get('risk'))}",
            "Confirm: yes or /confirm",
            "Cancel: no",
        ]
    pending_permissions = live_activity.get("pending_permissions") or []
    if pending_permissions:
        permission = pending_permissions[-1]
        rows = [
            "State: approval needed",
            f"Permission: {_humanize_identifier(permission.get('tool_id'))} {_humanize_identifier(permission.get('action'))}",
            f"Target: {_first_line(permission.get('target'), limit=72)}",
            f"Risk: {_humanize_identifier(permission.get('risk') or 'unknown')}",
        ]
        command = _approval_command(active_session)
        rows.append(f"Next: {command or 'approve or decline in chat'}")
        return rows
    latest_activation = state.get("latest_palette_activation") or {}
    if latest_activation:
        rows = [
            "State: UI updated",
            f"Action: {_title_case_identifier(latest_activation.get('entry_id') or 'unknown')}",
            f"Status: {'succeeded' if latest_activation.get('ok') else 'failed'}",
        ]
        action = latest_activation.get("action") or {}
        if action:
            rows.append(f"Kind: {_humanize_identifier(action.get('type') or latest_activation.get('activation_kind') or 'unknown')}")
        if latest_activation.get("raw_model_ref"):
            rows.append(f"Model: {latest_activation.get('raw_model_ref')}")
        if "session_model_selected" in latest_activation:
            rows.append(f"Model selected: {latest_activation.get('session_model_selected')}")
        if latest_activation.get("model_validation"):
            validation = latest_activation["model_validation"]
            if validation.get("blocked_reasons"):
                rows.append("Blocked: " + ", ".join(_humanize_identifier(reason) for reason in validation["blocked_reasons"]))
        if "harness_state_modified" in latest_activation:
            rows.append(f"Saved: {latest_activation.get('harness_state_modified', False)}")
        if "session_event_persisted" in latest_activation:
            rows.append(f"Recorded: {latest_activation.get('session_event_persisted')}")
        return rows
    latest_response = state.get("latest_response") or {}
    if latest_response.get("kind") == "self_managed_local_action":
        report_path = latest_response.get("report_path")
        if not report_path and isinstance(latest_response.get("extra"), dict):
            report_path = latest_response["extra"].get("report_path")
        rows = ["Latest action", f"Status: {'succeeded' if latest_response.get('ok') else 'failed'}"]
        if report_path:
            rows.append(f"Report: {Path(str(report_path)).name}")
        return rows
    if dashboard.get("active_leases"):
        lease = dashboard["active_leases"][0]
        return [
            "State: running",
            f"Work: {_task_title(dashboard, lease.get('task_id'))}",
            "Next: open details or dispatch from the command palette",
        ]
    blocker, blocked_task_id = _first_blocker(progress)
    if signal == "blocked" and blocker:
        rows = ["State: blocked", f"Blocker: {blocker}"]
        if blocked_task_id:
            rows.append(f"Work: {_task_title(dashboard, blocked_task_id)}")
        rows.append("Next: inspect progress details")
        return rows
    if signal == "ready":
        ready = (live_activity.get("counts") or {}).get("ready", 0)
        return ["State: ready", f"Ready: {ready}", "Next: lease the next task"]
    if signal == "setup_needed":
        return ["State: needs setup", "Next: /init", "Then: summarize this repo"]
    latest = []
    if state.get("latest_task_id"):
        latest.append("Task: updated")
    if state.get("latest_lease_id"):
        latest.append("Lease: active")
    if state.get("latest_run_id"):
        latest.append("Run: recorded")
    if latest:
        return latest
    return [f"State: {_status_label(signal)}", "Pending: none", "Next: ask Harness or inspect progress"]


def _right_panel_now_rows(dashboard: dict, state: dict) -> list[str]:
    live_activity = dashboard.get("live_activity") or {}
    active_session = dashboard.get("active_session") or {}
    operator = active_session.get("operator") or {}
    cwd = active_session.get("cwd")
    if isinstance(cwd, dict):
        cwd = cwd.get("cwd")
    model_catalog = dashboard.get("model_catalog") or {}
    active_model = model_catalog.get("active_model") or {}
    session_label = _session_title(active_session) if active_session else "none"
    counts = live_activity.get("counts") or {}
    rows = [
        f"State: {_status_label(_right_panel_active_signal(dashboard, state))}",
        f"Operator: {_status_label(operator.get('phase') or 'idle')}",
        f"Request: {'running' if state.get('request_in_flight') else 'idle'}",
        f"Session: {session_label}",
        f"cwd: {cwd or '.'}",
        f"Model: {active_model.get('raw_model_ref') or active_session.get('raw_model_ref') or 'default'}",
        f"Queue: {counts.get('ready', 0)} ready / {counts.get('running', 0)} running / {counts.get('blocked', 0)} blocked",
    ]
    rows.append(
        "Open: "
        f"{counts.get('pending_permissions', 0)} approvals, "
        f"{counts.get('open_todos', 0)} todos, "
        f"{counts.get('recent_artifacts', 0)} artifacts"
    )
    if dashboard.get("active_leases"):
        lease = dashboard["active_leases"][0]
        rows.append(f"Work: {_task_title(dashboard, lease.get('task_id'))}")
    return rows


def _right_panel_session_rows(dashboard: dict, state: dict) -> list[str]:
    sessions = dashboard.get("recent_sessions") or []
    active_session = dashboard.get("active_session") or {}
    live_activity = dashboard.get("live_activity") or {}
    rows = []
    if active_session or sessions:
        latest = active_session or sessions[0]
        rows.append(f"Active: {_session_title(latest)}")
        rows.append(f"Status: {_status_label(latest.get('status') or 'unknown')}")
        if latest.get("raw_model_ref"):
            rows.append(f"Model: {latest['raw_model_ref']}")
        model_catalog = dashboard.get("model_catalog") or {}
        active_model = model_catalog.get("active_model") or {}
        if latest.get("agent_id"):
            rows.append(f"Agent: {_humanize_identifier(latest['agent_id'])}")
        operator = active_session.get("operator") or {}
        if operator:
            rows.append(f"Operator: {_status_label(operator.get('phase') or 'idle')}")
            if operator.get("waiting_approval_id"):
                rows.append("Waiting: approval")
                command = _approval_command(active_session)
                if command:
                    rows.append(f"Approval: {command}")
        rows.append(
            "Open: "
            f"todos={len(live_activity.get('open_todos') or [])} "
            f"permissions={len(live_activity.get('pending_permissions') or [])}"
        )
        if active_model:
            rows.append(f"Provider: {_humanize_identifier(active_model.get('provider_id') or 'default')}")
        timeline = active_session.get("timeline") or []
        if timeline:
            rows.append(f"Latest: {_clean_event_summary(timeline[-1])}")
        activation = active_session.get("latest_ui_activation") or {}
        if activation:
            rows.append(f"UI: {_title_case_identifier(activation.get('entry_id') or 'unknown')}")
        transcript = active_session.get("transcript") or []
        if transcript:
            first_line = str(transcript[-1]).splitlines()[0]
            rows.append(f"Last message: {first_line}")
        rows.append("Continue: ask \"continue this work\"")
        return rows
    rows.append("No sessions yet.")
    rows.append("Start: type a prompt")
    return rows


def _model_catalog_pane_rows(dashboard: dict) -> list[str]:
    catalog = dashboard.get("model_catalog") or {}
    providers = catalog.get("providers") or []
    models = catalog.get("models") or []
    active = catalog.get("active_model") or {}
    rows = [
        f"Providers: {len(providers)}",
        f"Models: {len(models)}",
        f"No hidden fallback: {catalog.get('no_hidden_fallback', True)}",
        "List: /models",
        "Select: /model <number|search|provider/model>",
    ]
    if active:
        rows.append(
            "Active: "
            f"{active.get('raw_model_ref') or active.get('model_id') or 'default'} "
            f"known={active.get('known_catalog_entry')} executable={active.get('executable')}"
        )
        if active.get("session_id"):
            rows.append(f"Switch: harness session model {active['session_id']} <provider/model> --project .")
            rows.append("In app: /models then /model <number>")
        rows.append(f"Provider enabled: {active.get('provider_enabled')}")
        if active.get("blocked_reasons"):
            rows.append("Blocked: " + ", ".join(active["blocked_reasons"]))
        rows.extend(
            [
                f"Provider execution: {active.get('provider_execution_started', False)}",
                f"Model execution: {active.get('model_execution_started', False)}",
                f"Network: {active.get('network_accessed', False)}",
                f"Hidden fallback: provider={active.get('hidden_provider_fallback', False)} model={active.get('hidden_model_fallback', False)}",
                f"Permission grant: {active.get('permission_granting', False)}",
                f"Authority grant: {active.get('authority_granting', False)}",
            ]
        )
    rows.append("Provider status:")
    rows.extend(
        [
            f"{provider['provider_id']} enabled={provider['enabled']} credentials={provider['credential_status']}"
            for provider in providers[:4]
        ]
        or ["none"]
    )
    rows.append("Model refs:")
    rows.extend(
        [
            f"{index}. {model['raw_model_ref']} profile={model.get('model_profile_id') or '-'}"
            for index, model in enumerate(_unique_model_catalog_entries(models)[:8], start=1)
        ]
        or ["none"]
    )
    return rows


def render_model_selection_dialog(dashboard: dict, *, query: str = "", selected_index: int = 0) -> str:
    catalog = dashboard.get("model_catalog") or {}
    providers = {provider.get("provider_id"): provider for provider in catalog.get("providers") or []}
    models = _model_selection_dialog_entries(dashboard, query=query)
    active = catalog.get("active_model") or {}
    active_ref = active.get("raw_model_ref")
    selected_index = min(max(selected_index, 0), max(len(models) - 1, 0))
    lines = [
        "[bold deep_sky_blue1]Select model[/bold deep_sky_blue1]  [dim]session scope | no provider call[/dim]                 [dim]esc[/dim]",
        f"[dim]{'─' * 76}[/dim]",
        f"[bold steel_blue1]Search[/bold steel_blue1] {escape(query or 'type to filter')}",
    ]
    lines.append("")
    if active_ref:
        lines.append("[bold dark_orange3]Recent[/bold dark_orange3]")
        active_provider = active.get("provider_id") or str(active_ref).split("/", 1)[0]
        lines.append(f"  [blue]{escape(str(active_ref).split('/', 1)[-1])}[/] [dim]{escape(str(active_provider))}[/] [dim]current[/dim]")
        lines.append("")
    if not models:
        lines.append("[dim]No models match.[/dim]")
    grouped: dict[str, list[dict]] = {}
    for model in models:
        grouped.setdefault(str(model.get("provider_id") or "unknown"), []).append(model)
    row_number = 0
    for provider_id, provider_models in grouped.items():
        provider = providers.get(provider_id) or {}
        credential_status = str(provider.get("credential_status") or "unknown")
        lines.append(f"[bold dark_orange3]{escape(provider_id)}[/bold dark_orange3] [dim]{escape(credential_status)}[/]")
        for model in provider_models:
            row_number += 1
            raw_ref = str(model.get("raw_model_ref") or "")
            model_name = raw_ref.split("/", 1)[-1]
            marker = "*" if raw_ref == active_ref else " "
            text = f"{row_number:>2}. {marker} {model_name}"
            suffix = ""
            if not provider.get("enabled", True):
                suffix = " [dim]disabled[/dim]"
            elif credential_status == "missing":
                suffix = " [dim]credentials missing[/dim]"
            if row_number - 1 == selected_index:
                lines.append(f"[white on #5f87d7]{escape(('> ' + text)[:66].ljust(66))}[/]{suffix}")
            else:
                lines.append(f"  {escape(text)}{suffix}")
        lines.append("")
    lines.extend(
        [
            f"[dim]{'─' * 76}[/dim]",
            "[bold steel_blue1]Enter[/bold steel_blue1] select   [bold steel_blue1]Slash[/bold steel_blue1] /model <number> or /model <name>   [bold steel_blue1]Arrows[/bold steel_blue1] move",
            "[bold steel_blue1]Connect provider[/bold steel_blue1] ctrl+a   [bold steel_blue1]Favorite[/bold steel_blue1] ctrl+f",
        ]
    )
    return "\n".join(lines).rstrip()


def render_theme_selection_dialog(*, selected_theme: str = "light", selected_index: int = 0) -> str:
    entries = THEME_DIALOG_ENTRIES
    selected_index = min(max(selected_index, 0), len(entries) - 1)
    lines = [
        "[bold deep_sky_blue1]Switch theme[/bold deep_sky_blue1]  [dim]runtime only | no preference write[/dim]                [dim]esc[/dim]",
        f"[dim]{'─' * 76}[/dim]",
        "[bold dark_orange3]Theme[/bold dark_orange3]",
    ]
    for index, entry in enumerate(entries):
        marker = "*" if entry["id"] == selected_theme else " "
        current = "current" if entry["id"] == selected_theme else ""
        text = f"{marker} {entry['title']:<10} {current:<8} {entry['description']}"
        if index == selected_index:
            lines.append(f"[white on #5f87d7]{escape(('> ' + text)[:76].ljust(76))}[/]")
        else:
            lines.append(f"  {escape(text[:74])}")
    lines.extend(
        [
            "",
            "[bold steel_blue1]Preview[/bold steel_blue1] Light uses a brighter Harness surface; System keeps Textual default.",
            f"[dim]{'─' * 76}[/dim]",
            "[bold steel_blue1]Enter[/bold steel_blue1] select   [bold steel_blue1]Arrows[/bold steel_blue1] move   [bold steel_blue1]Does not[/bold steel_blue1] persist preferences",
        ]
    )
    return "\n".join(lines).rstrip()


def _model_selection_dialog_entries(dashboard: dict, *, query: str = "") -> list[dict]:
    catalog = dashboard.get("model_catalog") or {}
    models = _unique_model_catalog_entries(catalog.get("models") or [])
    normalized = query.strip().casefold()
    if not normalized:
        return models
    return [
        model
        for model in models
        if normalized in str(model.get("raw_model_ref") or "").casefold()
        or normalized in str(model.get("model_id") or "").casefold()
        or normalized in str(model.get("provider_id") or "").casefold()
    ]


def build_functionality_table(slash_commands: dict | None = None) -> dict:
    slash_commands = slash_commands or build_slash_commands()
    commands_by_name = {command["name"]: command for command in slash_commands["commands"]}
    rows: list[dict] = []
    for group_id, names in FUNCTIONALITY_TABLE_LAYOUT:
        for name in names:
            command = commands_by_name.get(name)
            if command is None:
                continue
            row = _functionality_table_row(command, group_id)
            row["id"] = f"{group_id}.{name}"
            rows.append(row)
    return {
        "schema_version": "harness.tui_functionality_table/v1",
        "ok": True,
        "groups": [dict(group) for group in FUNCTIONALITY_TABLE_GROUPS],
        "rows": rows,
    }


def filter_functionality_table(table: dict, query: str) -> dict:
    normalized = query.strip().casefold()
    if not normalized:
        rows = [dict(row) for row in table.get("rows", [])]
    else:
        rows = [
            dict(row)
            for row in table.get("rows", [])
            if normalized
            in " ".join(
                str(row.get(key) or "")
                for key in (
                    "title",
                    "invoke",
                    "slash",
                    "group_id",
                    "authority",
                    "surface",
                    "status",
                    "evidence",
                    "description",
                    "safety_note",
                )
            ).casefold()
        ]
    return {
        "schema_version": "harness.tui_functionality_filter/v1",
        "ok": True,
        "query": query.strip(),
        "total_matches": len(rows),
        "rows": rows,
    }


def render_functionality_table_dialog(
    table: dict | None = None,
    *,
    query: str = "",
    selected_index: int = 0,
    limit: int = 20,
) -> str:
    table = table or build_functionality_table()
    filtered = filter_functionality_table(table, query)
    rows = filtered["rows"]
    selected_index = min(max(selected_index, 0), max(len(rows) - 1, 0))
    visible_limit = max(1, limit)
    if len(rows) <= visible_limit:
        start_index = 0
    else:
        start_index = max(0, min(selected_index - visible_limit + 1, len(rows) - visible_limit))
    visible_rows = rows[start_index : start_index + visible_limit]
    group_titles = {group["id"]: group["title"] for group in table.get("groups", [])}
    search_label = escape(query or "type to filter")
    lines = [
        f"[bold deep_sky_blue1]Commands[/bold deep_sky_blue1]  [dim]{len(rows)} matches | enter runs safe UI rows or stages command text[/dim]       [dim]esc[/dim]",
        f"[dim]{'─' * 76}[/dim]",
        f"[bold steel_blue1]Search[/bold steel_blue1] {search_label}",
    ]
    if start_index > 0:
        lines.append(f"[dim]... {start_index} previous. Use arrows to navigate.[/dim]")
    current_group = None
    for offset, row in enumerate(visible_rows):
        row_index = start_index + offset
        if row.get("group_id") != current_group:
            current_group = row.get("group_id")
            lines.extend(["", f"[bold dark_orange3]{escape(group_titles.get(str(current_group), str(current_group)))}[/bold dark_orange3]"])
        title = str(row.get("title") or row.get("name") or "")
        invoke = str(row.get("invoke") or row.get("slash") or "")
        status = str(row.get("status") or "")
        text = _dialog_row_text(title, invoke, status)
        if row_index == selected_index:
            lines.append(f"[white on #5f87d7]{escape(('> ' + text)[:76].ljust(76))}[/]")
        else:
            lines.append(f"  {escape(text[:74])}")
    remaining = len(rows) - (start_index + len(visible_rows))
    if remaining > 0:
        lines.append(f"[dim]... {remaining} more. Use arrows to navigate.[/dim]")
    if not rows:
        lines.extend(["", "[dim]No commands match.[/dim]"])
    elif rows:
        selected = rows[selected_index]
        lines.extend(
            [
                "",
                f"[dim]{'─' * 76}[/dim]",
                f"[bold steel_blue1]Authority[/bold steel_blue1] {escape(str(selected.get('authority') or 'unknown'))}   "
                f"[bold steel_blue1]Surface[/bold steel_blue1] {escape(str(selected.get('surface') or 'unknown'))}",
                f"[bold steel_blue1]Does not[/bold steel_blue1] {escape(str(selected.get('does_not') or 'hide work'))}",
                f"[bold steel_blue1]Evidence[/bold steel_blue1] {escape(str(selected.get('evidence') or 'none'))}   "
                f"[bold steel_blue1]Next[/bold steel_blue1] {escape(str(selected.get('next') or selected.get('slash') or ''))}",
            ]
        )
    return "\n".join(lines)


def render_command_menu_dialog(table: dict | None = None, *, query: str = "", selected_index: int = 0) -> str:
    return render_functionality_table_dialog(table, query=query, selected_index=selected_index)


def _functionality_table_row(command: dict, group_id: str) -> dict:
    name = str(command["name"])
    activation_kind = str((command.get("activation") or {}).get("kind") or "manual_command")
    authority = _functionality_authority(command)
    slash = str(command["slash"])
    title = FUNCTIONALITY_TITLES.get(name, str(command["title"]))
    invoke = FUNCTIONALITY_INVOKES.get(name, slash)
    surface = _functionality_surface(command)
    return {
        "name": name,
        "group_id": group_id,
        "title": title,
        "invoke": invoke,
        "slash": slash,
        "entry_id": command.get("entry_id"),
        "surface": surface,
        "authority": authority,
        "status": _functionality_status(command),
        "evidence": FUNCTIONALITY_EVIDENCE.get(name, _functionality_default_evidence(command)),
        "description": command.get("description"),
        "safety_note": command.get("safety_note"),
        "activation_kind": activation_kind,
        "mutates_when_run": bool(command.get("mutates_when_run")),
        "does_not": _functionality_does_not(command),
        "next": _functionality_next(command, invoke),
    }


def _functionality_authority(command: dict) -> str:
    activation_kind = str((command.get("activation") or {}).get("kind") or "manual_command")
    if activation_kind == "ui_action":
        return "ui-only"
    if activation_kind == "model_list":
        return "read-only"
    if activation_kind == "session_model_selection":
        return "session metadata"
    if command.get("group_id") == "registered_adapters":
        return "registered dispatch"
    if command.get("mutates_when_run"):
        return "manual mutation"
    return "read-only preview"


def _functionality_surface(command: dict) -> str:
    activation_kind = str((command.get("activation") or {}).get("kind") or "manual_command")
    if activation_kind == "ui_action":
        return "dashboard"
    if activation_kind in {"model_list", "session_model_selection"}:
        return "dialog"
    if command.get("group_id") == "registered_adapters":
        return "manual command"
    if command.get("mutates_when_run"):
        return "chat/manual command"
    return "command preview"


def _functionality_status(command: dict) -> str:
    activation_kind = str((command.get("activation") or {}).get("kind") or "manual_command")
    if activation_kind == "ui_action":
        return "ui"
    if activation_kind == "model_list":
        return "read"
    if activation_kind == "session_model_selection":
        return "state"
    if command.get("group_id") == "registered_adapters":
        return "dispatch"
    if command.get("mutates_when_run"):
        return "action"
    return "preview"


def _functionality_default_evidence(command: dict) -> str:
    if command.get("mutates_when_run"):
        return "explicit command output"
    return "none"


def _functionality_does_not(command: dict) -> str:
    activation_kind = str((command.get("activation") or {}).get("kind") or "manual_command")
    if activation_kind == "ui_action":
        return "start process, mutate files, grant permission"
    if activation_kind == "model_list":
        return "call provider, execute model, mutate state"
    if activation_kind == "session_model_selection":
        return "call provider, execute model, hidden fallback"
    if command.get("group_id") == "registered_adapters":
        return "dispatch unknown adapter or unsafe metadata"
    return "execute from this dialog"


def _functionality_next(command: dict, invoke: str) -> str:
    activation_kind = str((command.get("activation") or {}).get("kind") or "manual_command")
    if activation_kind == "session_model_selection":
        return "/model <number|search|provider/model>"
    if activation_kind == "model_list":
        return "/models"
    if activation_kind == "ui_action":
        return invoke
    return str(command.get("command") or command.get("slash") or invoke)


def _dialog_row_text(title: str, invoke: str, status: str, *, width: int = 72) -> str:
    left = title[:38].ljust(40)
    middle = invoke[:20].rjust(20)
    right = status[:10].rjust(10)
    text = f"{left}{middle} {right}"
    return text[:width].ljust(width)


def _unique_model_catalog_entries(models: list[dict]) -> list[dict]:
    unique: list[dict] = []
    seen: set[str] = set()
    for model in models:
        raw_ref = str(model.get("raw_model_ref") or "").strip()
        if not raw_ref or raw_ref in seen:
            continue
        seen.add(raw_ref)
        unique.append(model)
    return unique


def _terminal_tab_pane_rows(dashboard: dict) -> list[str]:
    payload = dashboard.get("terminal_tabs") or {}
    tabs = payload.get("tabs") or []
    if not tabs:
        return ["none"]
    rows = [
        "Read-only persisted PTY tab projection.",
        f"Policy: {(payload.get('policy_boundary') or {}).get('kind') or 'unknown'}",
        f"Blocked: {','.join(payload.get('blocked_reasons') or ['none'])}",
        "No terminal process, websocket, live stream, artifact content read, or terminal control is started.",
    ]
    for tab in tabs[:5]:
        preview = str(tab.get("scrollback_preview") or "").replace("\n", "\\n")
        if len(preview) > 120:
            preview = preview[:117] + "..."
        rows.append(
            (
                f"{tab.get('id')} {tab.get('status')} "
                f"title={tab.get('title') or 'untitled'} "
                f"events={tab.get('event_count', 0)} "
                f"output={tab.get('output_event_count', 0)} "
                f"artifacts={tab.get('artifact_ref_count', 0)} "
                f"restore={tab.get('restoration_ready')}"
            )
        )
        tab_blocked = ",".join(tab.get("blocked_reasons") or ["none"])
        rows.append(f"boundary={((tab.get('policy_boundary') or {}).get('kind') or 'unknown')} blocked={tab_blocked}")
        if preview:
            rows.append(f"preview: {preview}")
    rows.append("Terminal tabs are disabled until PTY policy gates are implemented.")
    return rows


def _right_panel_queue_rows(dashboard: dict) -> list[str]:
    labels = {
        "ready": "Ready",
        "leased": "Running",
        "waiting_approval": "Needs approval",
        "blocked": "Blocked",
        "failed": "Failed",
        "succeeded": "Done",
    }
    counts = dashboard.get("task_status_counts") or {}
    live_counts = (dashboard.get("live_activity") or {}).get("counts") or {}
    rows = [
        f"{label}: {counts.get(status, 0)}"
        for status, label in labels.items()
        if counts.get(status, 0)
    ]
    if not rows:
        rows = ["No queued tasks."]
    rows.append(
        "Total: "
        f"{dashboard['summary']['tasks_total']} tasks, "
        f"{dashboard['summary']['objectives']} objectives, "
        f"{dashboard['summary']['active_leases']} active"
    )
    return rows


def _right_panel_recent_rows(dashboard: dict) -> list[str]:
    live_activity = dashboard.get("live_activity") or {}
    rows = []
    for event in (live_activity.get("latest_events") or [])[-3:]:
        rows.append(f"Event: {_clean_event_summary(event.get('line'))}")
    if dashboard.get("recent_sessions"):
        session = dashboard["recent_sessions"][0]
        rows.append(f"Session: {_status_label(session['status'])} | {_session_title(session)}")
    if dashboard.get("tasks"):
        task = dashboard["tasks"][0]
        rows.append(f"Task: {_status_label(task['status'])} | {task['title']}")
    if dashboard.get("recent_runs"):
        run = dashboard["recent_runs"][0]
        rows.append(f"Run: {_status_label(run['status'])} | {_humanize_identifier(run.get('task_type') or 'unknown')}")
    artifacts = live_activity.get("recent_artifacts") or []
    if artifacts:
        artifact = artifacts[0]
        rows.append(
            f"Artifact: {_humanize_identifier(artifact.get('kind'))} | "
            f"{Path(str(artifact.get('path') or 'artifact')).name} | "
            f"{_humanize_identifier(artifact.get('evidence_status'))}"
        )
    return rows[:7] or ["No recent task, run, or event."]


def _right_panel_adapter_rows(dashboard: dict) -> list[str]:
    capabilities = dashboard.get("capabilities", {}).get("capabilities", [])
    if capabilities:
        ready = [
            _title_case_identifier(capability.get("id"))
            for capability in capabilities
            if str(capability.get("readiness") or "").casefold() in {"ready", "available"}
        ]
        blocked = [
            _title_case_identifier(capability.get("id"))
            for capability in capabilities
            if capability.get("blocked_state_explanations")
        ]
        rows = [f"Available: {len(capabilities)}"]
        if ready:
            rows.append("Ready: " + ", ".join(ready[:3]))
        if blocked:
            rows.append("Blocked: " + ", ".join(blocked[:3]))
        breakers = (dashboard.get("runtime_controls") or {}).get("breakers") or []
        open_breakers = [breaker for breaker in breakers if breaker.get("state") not in {None, "closed"}]
        if open_breakers:
            rows.append(f"Breakers: {len(open_breakers)} open")
        return rows
    adapters = dashboard.get("registered_adapters", [])
    if not adapters:
        return ["No registered adapters."]
    rows = []
    for adapter in adapters[:6]:
        task_types = ", ".join(_humanize_identifier(item) for item in adapter.get("supported_task_types", [])) or "no task types"
        rows.append(f"{_title_case_identifier(adapter.get('id'))}: {task_types}")
    return rows


def _right_panel_progress_rows(dashboard: dict) -> list[str]:
    progress = dashboard.get("progress") or {}
    objective_id = progress.get("objective_id")
    if not objective_id:
        return ["Mode: idle", "Objective: none", "Next: create or select an objective"]
    rows = [
        f"Mode: {_status_label(progress.get('mode') or 'idle')}",
        f"Objective: {progress.get('objective_title') or 'selected objective'}",
    ]
    if progress.get("active_lease_ids"):
        rows.append("Lease: active")
    if progress.get("active_run_ids"):
        rows.append("Run: active")
    for task in progress.get("tasks", [])[:3]:
        label = f"{_status_label(task.get('status') or 'unknown')} | {task.get('title') or 'Untitled task'}"
        blocked = task.get("blocked_state_explanations") or []
        if blocked:
            label += f" | {blocked[0].get('code')}"
        rows.append(label)
    if progress.get("next_action"):
        rows.append("Next: inspect progress details")
    return rows


def _right_panel_next_rows(dashboard: dict) -> list[str]:
    if not dashboard.get("initialized"):
        return ["/init", "Then try: summarize this repo"]
    if dashboard.get("active_leases"):
        return [
            "Open details for the active lease",
            "Dispatch from the command palette when ready",
        ]
    if dashboard["task_status_counts"].get("ready", 0):
        ready_repo_planning = any(
            task.get("status") == "ready" and task.get("execution_adapter") == "repo_planning"
            for task in dashboard.get("tasks", [])
        )
        if ready_repo_planning:
            return [
                "Lease the next planning task",
                "Dispatch the lease after review",
            ]
        return ["Lease the next task", "Inspect or execute the lease"]
    return [
        "summarize this repo",
        "create dry run task",
        "open the command palette for templates",
    ]


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
    return render_right_pane_cockpit(model)


def _render_right_panel_row(row: str) -> str:
    label, sep, value = row.partition(":")
    if sep and 1 <= len(label) <= 28 and re.match(r"^[A-Za-z0-9 _./-]+$", label):
        return f"  [dim]-[/dim] [bold]{escape(label)}: {escape(value.strip())}[/bold]"
    return f"  [dim]-[/dim] {escape(row)}"


def render_right_panel_detail(model: dict, section_id: str | None = None) -> str:
    return render_right_pane_detail(model, section_id)


def render_right_panel_status(model: dict) -> str:
    return render_right_pane_status(model)


def build_codex_mode_model(project_root: Path, run_id: str | None = None) -> dict:
    store = SQLiteStore(project_root)
    if not store.db_path.exists():
        return {
            "schema_version": "harness.tui_codex_mode/v1",
            "ok": False,
            "project_root": str(project_root),
            "run_id": run_id,
            "status": "unavailable",
            "error": "Project is not initialized.",
            "panes": _empty_codex_panes("Project is not initialized."),
            "controls": _codex_controls(None),
        }
    selected_run = None
    runs = []
    try:
        store.initialize()
        runs = store.list_runs()
        selected_run = store.get_run(run_id) if run_id else (runs[0] if runs else None)
    except Exception as exc:
        return {
            "schema_version": "harness.tui_codex_mode/v1",
            "ok": False,
            "project_root": str(project_root),
            "run_id": run_id,
            "status": "unavailable",
            "error": f"{type(exc).__name__}: {exc}",
            "panes": _empty_codex_panes(f"State unavailable: {type(exc).__name__}: {exc}"),
            "controls": _codex_controls(None),
        }
    if selected_run is None:
        return {
            "schema_version": "harness.tui_codex_mode/v1",
            "ok": True,
            "project_root": str(project_root),
            "run_id": None,
            "status": "queued",
            "state": "Queued",
            "header": ["No live run selected.", "Submit a prompt to create a run."],
            "panes": _empty_codex_panes("No persisted run events yet."),
            "controls": _codex_controls(None),
        }
    events = [event.jsonl_envelope() for event in store.list_events(selected_run.id)]
    artifacts = []
    try:
        artifacts = store.list_artifacts(selected_run.id)
    except Exception:
        artifacts = []
    latest_usage = {}
    for event in events:
        if event.get("type") == "token_usage.updated":
            latest_usage = dict(event.get("payload") or {})
    model_output = _codex_model_output_rows(events)
    panes = [
        {
            "id": "live_procedure",
            "title": "Live Procedure",
            "lines": [render_procedure_event(event) for event in events if event.get("visibility") == "user_visible"]
            or ["No procedure events recorded."],
        },
        {
            "id": "model_output",
            "title": "Model Output",
            "lines": model_output or ["No model output recorded."],
        },
        {
            "id": "artifacts",
            "title": "Artifacts",
            "lines": [
                f"{artifact.kind}: {artifact.path.name} | {artifact.redaction_state} | {artifact.evidence_status}"
                for artifact in artifacts
            ]
            or ["No artifacts registered."],
        },
        {
            "id": "controls",
            "title": "Controls",
            "lines": _codex_controls(selected_run.id),
        },
    ]
    return {
        "schema_version": "harness.tui_codex_mode/v1",
        "ok": True,
        "project_root": str(project_root),
        "run_id": selected_run.id,
        "task_id": selected_run.task_id,
        "agent": "code_editor",
        "backend": selected_run.backend_name or "none",
        "mode": selected_run.task_type or "unknown",
        "status": selected_run.status,
        "state": _codex_visual_state(selected_run.status, events),
        "token_usage": latest_usage,
        "header": [
            f"Run: {selected_run.id}",
            f"Status: {_codex_visual_state(selected_run.status, events)}",
            f"Backend: {selected_run.backend_name or 'none'}",
            f"Mode: {selected_run.task_type or 'unknown'}",
            _format_token_usage(latest_usage),
        ],
        "panes": panes,
        "controls": panes[-1]["lines"],
    }


def render_codex_mode(model: dict) -> str:
    lines = ["Codex Mode"]
    for row in model.get("header", []):
        lines.append(f"  {row}")
    for pane in model.get("panes", []):
        lines.extend(["", pane["title"]])
        for row in pane.get("lines", []):
            lines.append(f"  {row}")
    return "\n".join(lines).strip()


def _empty_codex_panes(message: str) -> list[dict]:
    return [
        {"id": "live_procedure", "title": "Live Procedure", "lines": [message]},
        {"id": "model_output", "title": "Model Output", "lines": ["No model output recorded."]},
        {"id": "artifacts", "title": "Artifacts", "lines": ["No artifacts registered."]},
        {"id": "controls", "title": "Controls", "lines": _codex_controls(None)},
    ]


def _codex_model_output_rows(events: list[dict]) -> list[str]:
    rows: list[str] = []
    for event in events:
        payload = event.get("payload") or {}
        if event.get("type") in {"model.message_delta", "model.token"}:
            text = payload.get("delta") or payload.get("text")
            if text:
                rows.append(str(text))
        elif event.get("type") == "reasoning.summary_delta":
            text = payload.get("delta") or payload.get("text")
            if text:
                rows.append(f"thinking summary: {text}")
    return rows


def _codex_controls(run_id: str | None) -> list[str]:
    if not run_id:
        return ["Stop run: unavailable", "Approve hosted boundary: unavailable", "Approve apply-back: unavailable"]
    return [
        f"Stop run: harness controls disable --target run:{run_id}",
        "Approve hosted boundary: harness approvals add --backend codex_cli --data-boundary hosted_provider --project .",
        f"Approve apply-back: harness apply {run_id} --project .",
        f"Inspect diff: harness diff {run_id} --project .",
        f"Open isolated workspace: harness show {run_id} --project .",
    ]


def _codex_visual_state(status: str, events: list[dict]) -> str:
    if status in {"failed"} or any(event.get("type") == "run.failed" for event in events):
        return "Failed"
    if status in {"cancelled", "canceled"}:
        return "Cancelled"
    if status.startswith("completed") or any(event.get("type") == "run.finished" for event in events):
        return "Succeeded"
    if any(event.get("type") == "approval.required" for event in events):
        return "Waiting approval"
    if any(event.get("type") == "test.started" for event in events) and not any(
        event.get("type") == "test.finished" for event in events
    ):
        return "Running tests"
    if any(event.get("type") == "tool_call.started" for event in events) and not any(
        event.get("type") == "tool_call.finished" for event in events
    ):
        return "Calling tool"
    if any(event.get("type") == "file.write" for event in events):
        return "Editing"
    if any(event.get("type") == "backend.started" for event in events):
        return "Thinking"
    if any(event.get("type") == "workspace.prepared" for event in events):
        return "Preparing workspace"
    if any(event.get("type") == "policy.resolved" for event in events):
        return "Resolving policy"
    if any(event.get("type") == "run.started" for event in events):
        return "Thinking"
    return "Queued"


def _format_token_usage(usage: dict) -> str:
    if not usage:
        return "Tokens: unavailable"
    total = usage.get("total_tokens")
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    reasoning = usage.get("reasoning_tokens")
    cost = usage.get("estimated_cost_usd")
    parts = []
    if total is not None:
        parts.append(f"total={total}")
    if input_tokens is not None:
        parts.append(f"input={input_tokens}")
    if output_tokens is not None:
        parts.append(f"output={output_tokens}")
    if reasoning is not None:
        parts.append(f"reasoning_count={reasoning}")
    if cost is not None:
        parts.append(f"cost=${cost}")
    return "Tokens: " + ", ".join(parts)


def build_slash_commands(palette: dict | None = None, custom_commands: list[dict] | None = None) -> dict:
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
                "activation": dict(entry.get("activation") or {}),
                "custom_command": False,
            }
        )
    commands.extend(
        [
            {
                "name": "models",
                "slash": "/models",
                "entry_id": "model_selection.list",
                "group_id": "model_selection",
                "title": "List selectable models",
                "description": "Show configured model refs with selection numbers.",
                "command": "/models",
                "mutates_when_run": False,
                "safety_note": "Read-only model catalog projection; no provider call.",
                "activation": {
                    "kind": "model_list",
                    "supported": True,
                    "process_started": False,
                    "filesystem_modified": False,
                    "permission_granting": False,
                },
                "custom_command": False,
            },
            {
                "name": "model",
                "slash": "/model",
                "entry_id": "model_selection.select",
                "group_id": "model_selection",
                "title": "Select session model",
                "description": "Select the active session model by number, search, or provider/model ref.",
                "command": "/model <number|search|provider/model>",
                "mutates_when_run": True,
                "safety_note": "Persists active session model metadata and validation evidence only; no provider call or fallback.",
                "activation": {
                    "kind": "session_model_selection",
                    "supported": True,
                    "process_started": False,
                    "filesystem_modified": False,
                    "permission_granting": False,
                },
                "custom_command": False,
            },
        ]
    )
    for command in custom_commands or []:
        commands.append(
            {
                "name": command["name"],
                "slash": command["slash"],
                "entry_id": f"project_commands.{command['name']}",
                "group_id": "project_commands",
                "title": command["title"],
                "description": command["description"],
                "command": f"harness commands run {command['name']} --project .",
                "mutates_when_run": command.get("mutates_when_run"),
                "safety_note": command["safety_note"],
                "activation": {
                    "kind": "manual_command",
                    "supported": False,
                    "process_started": False,
                    "filesystem_modified": False,
                    "permission_granting": False,
                },
                "custom_command": True,
                "command_id": command["id"],
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


def render_slash_command_suggestions(
    slash_commands: dict,
    query: str,
    *,
    selected_index: int = 0,
    limit: int = 8,
) -> str:
    raw_query = query.strip()
    if not raw_query.startswith("/"):
        return ""

    matching_commands = _matching_slash_commands(slash_commands, raw_query)
    if not matching_commands:
        return f"[dim]No slash commands match {escape(raw_query)}. Type /help to list commands.[/dim]"

    selected_index = min(max(selected_index, 0), len(matching_commands) - 1)
    visible_limit = max(1, limit)
    if len(matching_commands) <= visible_limit:
        start_index = 0
    else:
        start_index = max(0, min(selected_index - visible_limit + 1, len(matching_commands) - visible_limit))
    visible_commands = matching_commands[start_index : start_index + visible_limit]
    slash_width = min(max(len(command["slash"]) for command in visible_commands), 24)
    lines = []
    if start_index > 0:
        lines.append(f"[dim]... {start_index} previous. Keep using arrows to navigate.[/dim]")
    for index, command in enumerate(visible_commands):
        command_index = start_index + index
        slash = escape(command["slash"].ljust(slash_width))
        description = escape(command["description"])
        if command_index == selected_index:
            lines.append(f"[bold blue]{slash}[/]  [bold blue]{description}[/]")
        else:
            lines.append(f"[bold]{slash}[/]  [dim]{description}[/]")
    remaining = len(matching_commands) - (start_index + len(visible_commands))
    if remaining > 0:
        lines.append(f"[dim]... {remaining} more. Keep typing to filter.[/dim]")
    return "\n".join(lines)


def _matching_slash_commands(slash_commands: dict, query: str) -> list[dict]:
    raw_query = query.strip()
    if not raw_query.startswith("/"):
        return []
    command_query = raw_query.split(maxsplit=1)[0]
    filtered = filter_slash_commands(slash_commands, command_query)
    normalized_query = command_query.lstrip("/").casefold()
    if not normalized_query:
        return filtered["commands"]

    def rank(command: dict) -> int:
        name = str(command["name"]).casefold()
        slash = str(command["slash"]).casefold()
        title = str(command["title"]).casefold()
        description = str(command["description"]).casefold()
        if name.startswith(normalized_query) or slash.startswith(f"/{normalized_query}"):
            return 0
        if normalized_query in name or normalized_query in slash:
            return 1
        if title.startswith(normalized_query):
            return 2
        if normalized_query in title or normalized_query in description:
            return 3
        return 4

    return sorted(filtered["commands"], key=rank)


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


CODEX_ACCENT = "dark_cyan"
CODEX_SEPARATOR_STYLE = "dim"
CODEX_SEPARATOR_CHAR = "─"
CODEX_IMPORTANT_PHRASES = (
    "Agent Harness",
    "local-first",
    "supervised control plane",
    "Codex",
    "README",
    "CLI",
    "TUI",
)


def render_codex_like_transcript(
    messages: list[dict], *, working_seconds: int | None = None, separator_width: int = 96
) -> str:
    separator_width = max(20, separator_width)
    lines = [
        "[bold]Tip:[/bold] GPT-5.5 is now available in Codex. It's our strongest agentic coding model yet, built to reason through large codebases, check assumptions with tools, and keep going until the work is done.",
        "",
        f"[bold]Learn more:[/bold] [{CODEX_ACCENT}]https://openai.com/index/introducing-gpt-5-5/[/{CODEX_ACCENT}]",
    ]
    for message in messages:
        rendered = _render_codex_like_message(message, separator_width=separator_width)
        if rendered:
            lines.extend(["", rendered])
    if working_seconds is not None:
        lines.extend(["", f"[dim]○[/dim] [dim]Working ({working_seconds}s • esc to interrupt)[/dim]"])
    return "\n".join(lines).strip()


def _update_markup_static(widget, markup: str) -> None:
    try:
        widget.update(markup)
    except MarkupError:
        widget.update(escape(markup))


def _render_codex_like_message(message: dict, *, separator_width: int) -> str:
    role = message.get("role")
    title = str(message.get("title") or "").strip()
    body_lines = [str(line) for line in message.get("lines", []) if str(line).strip()]
    if role == "user":
        return f"[on #eeeeee][dim]›[/dim] {_style_inline_text(title)}[/]"
    if title == "Harness chat":
        return ""
    if title == "Codex-Like Mode":
        return ""
    if body_lines == ["Starting model turn..."]:
        return ""
    rendered: list[str] = []
    if title and title not in {"Assistant", "Assistant Streaming"}:
        rendered.append(_style_prose_line(title))
    in_procedure_block = False
    needs_separator_before_prose = False
    previous_kind = "prose" if rendered else None
    previous_raw = title if rendered else ""
    for line in body_lines:
        line_kind, line_rendered = _render_codex_like_line(line, in_procedure_block=in_procedure_block)
        if line_kind in {"prose", "list"} and needs_separator_before_prose:
            rendered.append(_style_separator(separator_width))
            needs_separator_before_prose = False
            previous_kind = "separator"
            previous_raw = ""
        if _needs_paragraph_gap(line_kind, previous_kind, previous_raw):
            rendered.append("")
        rendered.extend(line_rendered)
        if line_kind == "procedure":
            in_procedure_block = True
            needs_separator_before_prose = True
        elif line_kind == "child":
            in_procedure_block = True
        elif line_kind == "reasoning":
            in_procedure_block = False
        elif line_kind in {"prose", "list"}:
            in_procedure_block = False
        if line_kind != "blank":
            previous_kind = line_kind
            previous_raw = line.strip()
    if not rendered and title:
        rendered.append(_style_prose_line(title))
    return "\n".join(rendered)


def _render_codex_like_line(line: str, *, in_procedure_block: bool) -> tuple[str, list[str]]:
    stripped = line.strip()
    if not stripped:
        return "blank", []
    lowered = stripped.casefold()
    if lowered.startswith("ran "):
        return "procedure", [_style_ran_line(stripped)]
    if lowered.startswith("explored"):
        suffix = stripped[len("Explored") :].strip()
        tail = f" {_style_inline_text(suffix)}" if suffix else ""
        return "procedure", [f"[dim]•[/dim] [bold]Explored[/bold]{tail}"]
    if lowered.startswith("reasoning:"):
        _, _, reasoning = stripped.partition(":")
        return "reasoning", [_style_reasoning_line(reasoning.strip() or "model reasoning")]
    if stripped.startswith("Tool calls:"):
        return "procedure", [f"[dim]•[/dim] [bold]Tool calls:[/bold]"]
    if stripped.startswith("- "):
        if in_procedure_block:
            return "child", [_style_child_line(stripped[2:].strip())]
        return "list", [_style_list_line(stripped[2:].strip())]
    if stripped.startswith("* "):
        return "list", [_style_list_line(stripped[2:].strip())]
    numbered_match = re.match(r"^(\d+[.)])\s+(.+)$", stripped)
    if numbered_match:
        return "list", [_style_numbered_list_line(numbered_match.group(1), numbered_match.group(2))]
    return "prose", [_style_prose_line(stripped)]


def _style_prose_line(text: str) -> str:
    label_match = re.match(r"^([A-Z][^:]{1,48}:)\s+(.+)$", text)
    if label_match and "://" not in label_match.group(1):
        return f"[bold]{escape(label_match.group(1))}[/bold] {_style_inline_text(label_match.group(2))}"
    if _looks_like_prose_heading(text):
        return f"[bold]{_style_inline_text(text)}[/bold]"
    return _style_inline_text(text)


def _style_list_line(text: str) -> str:
    return f"[dim]•[/dim] {_style_inline_text(text)}"


def _style_reasoning_line(text: str) -> str:
    return f"[dim]•[/dim] [dim]{_style_inline_text(text)}[/dim]"


def _style_numbered_list_line(marker: str, text: str) -> str:
    return f"[dim]{escape(marker)}[/dim] {_style_inline_text(text)}"


def _style_separator(width: int) -> str:
    return f"[{CODEX_SEPARATOR_STYLE}]{CODEX_SEPARATOR_CHAR * width}[/{CODEX_SEPARATOR_STYLE}]"


def _needs_paragraph_gap(line_kind: str, previous_kind: str | None, previous_raw: str) -> bool:
    if not previous_kind or previous_kind in {"procedure", "child", "separator"}:
        return False
    if line_kind == "reasoning":
        return previous_kind in {"prose", "list"}
    if line_kind == "prose" and previous_kind in {"prose", "list"}:
        return True
    if line_kind == "list" and previous_kind == "prose" and not previous_raw.rstrip().endswith(":"):
        return True
    return False


def _looks_like_prose_heading(text: str) -> bool:
    stripped = text.strip()
    return stripped.endswith(":") and len(stripped) <= 72 and not stripped.startswith(("-", "*"))


def _style_ran_line(text: str) -> str:
    _, _, tail = text.partition(" ")
    command, _, args = tail.partition(" ")
    if command:
        command_text = f" [dim]{escape(command)}[/dim]"
        if args:
            command_text += f" [dim]{escape(args)}[/dim]"
    else:
        command_text = ""
    return f"[green]●[/green] [bold]Ran[/bold]{command_text}"


def _style_child_line(text: str) -> str:
    label, sep, rest = text.partition(":")
    if sep and label:
        return f"  [dim]└[/dim] [{CODEX_ACCENT}]{escape(label)}:[/{CODEX_ACCENT}] [dim]{escape(rest.strip())}[/dim]"
    first, _, tail = text.partition(" ")
    if first in {"List", "Read"}:
        return f"  [dim]└[/dim] [{CODEX_ACCENT}]{escape(first)}[/{CODEX_ACCENT}] {_style_inline_text(tail)}"
    return f"  [dim]└[/dim] [dim]{_style_inline_text(text)}[/dim]"


def _style_inline_text(text: str) -> str:
    styled = escape(text)
    styled = re.sub(r"\*\*(.+?)\*\*", r"[bold]\1[/bold]", styled)
    styled = re.sub(r"__(.+?)__", r"[bold]\1[/bold]", styled)
    styled = re.sub(r"`([^`]+)`", r"[dim]\1[/dim]", styled)
    styled = re.sub(r"(?<![\\[])(/[\w./-]+)", rf"[{CODEX_ACCENT}]\1[/{CODEX_ACCENT}]", styled)
    styled = _highlight_important_phrases(styled)
    return styled


def _highlight_important_phrases(styled: str) -> str:
    for phrase in CODEX_IMPORTANT_PHRASES:
        pattern = rf"(?<![\w\]/])({re.escape(phrase)})(?![\w\[])"
        styled = re.sub(pattern, r"[bold]\1[/bold]", styled)
    return styled


def _append_streaming_content(lines: list[str], content: str) -> list[str]:
    """Append model deltas as prose, while respecting explicit newlines."""
    updated = list(lines)
    chunks = content.splitlines(keepends=True)
    if not chunks:
        return updated
    for chunk in chunks:
        text = chunk.rstrip("\r\n")
        if text.strip():
            if updated and not _is_codex_procedure_line(updated[-1]):
                normalized = text.strip()
                separator = " " if text[:1].isspace() and not updated[-1].endswith((" ", "\t")) else ""
                updated[-1] = f"{updated[-1]}{separator}{normalized}"
            else:
                updated.append(text.strip())
        if chunk.endswith(("\n", "\r")) and (not updated or updated[-1].strip()):
            updated.append("")
    return [line for line in updated if line.strip()]


def _is_codex_procedure_line(line: str) -> bool:
    stripped = line.strip()
    lowered = stripped.casefold()
    return (
        lowered.startswith("ran ")
        or lowered.startswith("explored")
        or lowered.startswith("reasoning:")
        or lowered.startswith("turn ")
        or lowered.startswith("tool ")
        or lowered.startswith("tool calls:")
        or stripped.startswith("- ")
    )


def _merge_codex_stream_and_final_lines(stream_lines: list[str], final_lines: list[str]) -> list[str]:
    merged = [line for line in stream_lines if line.strip()]
    for line in final_lines:
        clean = line.strip()
        if clean and clean not in merged:
            merged.append(clean)
    return merged[-120:]


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


def normalize_right_panel_collapsed_sections(collapsed_section_ids: set[str] | list[str] | tuple[str, ...] | None) -> list[str]:
    if not collapsed_section_ids:
        return []
    normalized = []
    for section_id in collapsed_section_ids:
        resolved = _right_panel_resolve_section_id(section_id)
        if resolved in RIGHT_PANEL_SECTION_IDS and resolved not in normalized:
            normalized.append(resolved)
    return sorted(normalized)


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
    if view.get("cockpit_schema_version"):
        return render_right_pane_top_context(view)
    return " | ".join(f"{hint['key']}: {hint['label']}" for hint in view["navigation_hints"])


def _render_composer_status(
    dashboard: dict,
    selected_agent_id: str | None = None,
    selected_session_id: str | None = None,
) -> str:
    active_session = dashboard.get("active_session") or {}
    if selected_session_id:
        selected_summary = next(
            (
                session
                for session in dashboard.get("recent_sessions", [])
                if session.get("id") == selected_session_id
            ),
            None,
        )
        if selected_summary and selected_summary.get("id") != active_session.get("id"):
            active_session = selected_summary
    model_catalog = dashboard.get("model_catalog") or {}
    active_model = model_catalog.get("active_model") or {}
    composer_context = active_session.get("composer_context") or {}
    operator = active_session.get("operator") or {}
    agent_id = selected_agent_id or active_session.get("agent_id") or "default"
    model_ref = active_model.get("raw_model_ref") or active_session.get("raw_model_ref") or "default"
    attachment_count = composer_context.get("attachment_count", 0)
    context_tokens = composer_context.get("total_estimated_tokens", 0)
    phase = operator.get("phase") or "idle"
    waiting = operator.get("waiting_approval_id")
    approval = f" approval={waiting}" if waiting else ""
    card = operator.get("approval_card") if isinstance(operator.get("approval_card"), dict) else {}
    command = card.get("command") or card.get("operation")
    if command and len(str(command)) > 48:
        command = f"{str(command)[:45]}..."
    approval_detail = f" command={command}" if waiting and command else ""
    cwd = operator.get("cwd") or active_session.get("cwd") or "."
    cwd_detail = f" cwd={cwd}" if cwd and cwd != "." else ""
    return f"Model {model_ref} · Agent {agent_id} · Operator: {phase}{approval}{cwd_detail}{approval_detail} · ctx {context_tokens} · attachments {attachment_count}"


def _render_session_rail(dashboard: dict, *, selected_index: int = 0) -> str:
    sessions = dashboard.get("recent_sessions") or []
    active_session = dashboard.get("active_session") or {}
    lines = ["Sessions", "enter switch | n new | / search"]
    if not sessions:
        lines.extend(["", "No sessions", "Start by typing a prompt"])
        return "\n".join(lines)
    selected_index = max(0, min(int(selected_index or 0), len(sessions[:12]) - 1))
    active_id = active_session.get("id")
    for index, session in enumerate(sessions[:12]):
        marker = ">" if index == selected_index else " "
        active_marker = "*" if session.get("id") == active_id else " "
        title = _session_title(session)
        status = _status_label(session.get("status") or "unknown")
        lines.append(f"{marker}{active_marker} {title[:34]}")
        lines.append(f"   {status}")
    selected = sessions[selected_index]
    lines.extend(["", f"Selected: {_session_title(selected)}"])
    return "\n".join(lines)


def _session_pane_row_label(session: dict, *, selected: bool = False, active: bool = False) -> str:
    marker = ">" if selected else " "
    active_marker = "*" if active else " "
    status = _status_label(session.get("status") or "unknown")
    title = _session_title(session)
    if len(title) > 30:
        title = title[:27].rstrip() + "..."
    return f"{marker}{active_marker} {title:<30} {status}"


def _render_session_pane_header(projection: dict, *, focused: bool) -> str:
    counts = projection.get("counts") or {}
    focus = "focused" if focused else "ctrl+x s"
    active_filter = projection.get("filter") or "open"
    filter_label = "" if active_filter == "open" else f" | {active_filter}"
    return (
        f"Sessions | {focus}{filter_label} | {counts.get('filtered', 0)} shown "
        f"| {counts.get('open', 0)} open | {counts.get('running', 0)} running"
    )


def _render_session_pane_detail(projection: dict) -> str:
    sessions = projection.get("sessions") or []
    selected_id = projection.get("selected_session_id")
    selected = next((session for session in sessions if session.get("id") == selected_id), None)
    query = projection.get("query") or ""
    if selected is None:
        suffix = f" | search={query}" if query else ""
        return f"No matching sessions{suffix}."
    latest = _clean_event_summary(selected.get("latest_event") or "no events")
    title = _session_title(selected)
    active_bits = []
    if selected.get("active_task_id"):
        active_bits.append("task linked")
    if selected.get("active_run_id"):
        active_bits.append("run linked")
    active_work = f" | {', '.join(active_bits)}" if active_bits else ""
    return (
        f"{title}\n"
        f"{_status_label(selected.get('status'))} | {selected.get('message_count', 0)} messages{active_work}\n"
        f"{latest}"
    )


def _render_session_pane_actions(*, search_active: bool, query: str) -> str:
    if search_active:
        return f"Search: {query or '<type>'} | enter/esc finish | backspace edit"
    return "enter switch | n new | e rename | / search | f filter | a archive"


def render_session_delete_dialog(session: dict, *, buffer: str = "") -> str:
    lines = [
        "[bold red]Hard delete session[/bold red]",
        "",
        f"Title: {escape(str(session.get('display_title') or session.get('title') or 'Untitled session'))}",
        f"ID: {escape(str(session.get('id') or 'unknown'))}",
        f"Status: {escape(str(session.get('status') or 'unknown'))}",
        f"Messages: {session.get('message_count', 0)}",
        f"Run: {escape(str(session.get('active_run_id') or 'none'))}",
        f"Task: {escape(str(session.get('active_task_id') or 'none'))}",
        "",
        "This removes session-owned messages, parts, todos, permissions, links, events, and session folder.",
        "Linked runs, tasks, artifacts, and active repository files are retained.",
        "",
        f"Type DELETE then enter: {escape(buffer)}",
        "Esc cancels.",
    ]
    return "\n".join(lines)


def render_session_rename_dialog(session: dict, *, buffer: str = "") -> str:
    return "\n".join(
        [
            "[bold]Rename session[/bold]",
            "",
            f"Current: {escape(str(session.get('display_title') or session.get('title') or 'Untitled session'))}",
            f"ID: {escape(str(session.get('id') or 'unknown'))}",
            "",
            f"New title: {escape(buffer)}",
            "Enter saves. Esc cancels.",
        ]
    )


def render_agent_selection_dialog(agents: list[str], *, selected_agent: str | None, selected_index: int = 0) -> str:
    if not agents:
        agents = ["default"]
    selected_index = min(max(selected_index, 0), len(agents) - 1)
    lines = ["[bold]Select agent[/bold]", ""]
    for index, agent in enumerate(agents):
        marker = ">" if index == selected_index else " "
        current = " current" if agent == (selected_agent or "default") else ""
        lines.append(f"{marker} {escape(agent)}{current}")
    lines.append("")
    lines.append("Enter selects. Esc cancels.")
    return "\n".join(lines)


def create_read_only_tui_app(project_root: Path):
    return create_harness_app(project_root)


def create_harness_app(project_root: Path, *, codex_like: bool = False, app_service=None):
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Container, Horizontal, Vertical, VerticalScroll
    from textual.css.query import NoMatches
    from textual.theme import Theme
    from textual.widgets import Header, Label, ListItem, ListView, Static, TextArea
    from harness.chat import ChatSessionState, handle_chat_input, route_chat_intent
    from harness.core_service import HarnessAppService

    service = app_service or HarnessAppService(project_root)
    dashboard = service.dashboard()
    panes = build_tui_panes(dashboard)
    initial_palette = build_command_palette(model_catalog=dashboard.get("model_catalog") or {})
    slash_commands = build_slash_commands(initial_palette)
    initial_view = build_right_panel_model(
        dashboard,
        {
            "palette": initial_palette,
            "active_section_id": "active_work",
            "active_section_index": _right_panel_section_index("active_work"),
            "collapsed_section_ids": set(),
            "active_orchestrator": "coding_orchestrator",
            "chat_mode": "live" if codex_like else "normal",
            "right_pane_mode": "overview",
        },
        "",
        "dashboard",
    )
    initial_messages = [build_chat_welcome_message(project_root)]

    harness_light_theme = Theme(
        name="harness-light",
        primary="#0b5cad",
        secondary="#6a4fb3",
        accent="#d97706",
        foreground="#111827",
        background="#fffdf7",
        surface="#ffffff",
        panel="#f2f6ff",
        warning="#b45309",
        error="#b91c1c",
        success="#047857",
        dark=False,
        luminosity_spread=0.22,
        text_alpha=1.0,
        variables={
            "block-cursor-foreground": "#ffffff",
            "block-cursor-background": "#0b5cad",
            "input-selection-background": "#bfdbfe",
        },
    )

    class HarnessPromptInput(TextArea):
        def __init__(self, *, placeholder: str, id: str) -> None:
            super().__init__("", id=id)
            self.placeholder = placeholder

        @property
        def value(self) -> str:
            return self.text

        @value.setter
        def value(self, text: str) -> None:
            self.load_text(text)

        def on_key(self, event) -> None:
            if event.key == "ctrl+x":
                event.prevent_default()
                event.stop()
                self.app.action_leader_key()
            elif self.app.session_text_dialog_active:
                event.prevent_default()
                event.stop()
                self.app.action_handle_text_dialog_key(event)
            elif event.key in SESSION_PREVIOUS_KEYS | SESSION_NEXT_KEYS:
                event.prevent_default()
                event.stop()
                if event.key in SESSION_PREVIOUS_KEYS:
                    self.app.action_session_previous()
                else:
                    self.app.action_session_next()
            elif event.key in SESSION_DELETE_KEYS:
                event.prevent_default()
                event.stop()
                self.app.action_delete_selected_session()
            elif self.app.dialog_visible and event.key in {"down", "up"}:
                event.prevent_default()
                event.stop()
                self.app.action_move_dialog_selection(1 if event.key == "down" else -1)
            elif self.app.dialog_visible and event.key in ENTER_KEYS:
                event.prevent_default()
                event.stop()
                self.app.action_activate_dialog_selection()
            elif self.app.leader_key_active:
                if self.app.is_leader_shortcut(event.key, event.character):
                    event.prevent_default()
                    event.stop()
                    self.app.action_handle_leader_key(event.key, event.character)
                else:
                    self.app.action_cancel_leader_key()
            elif event.key == "ctrl+enter":
                event.prevent_default()
                event.stop()
                self.app.action_submit_prompt()
            elif event.key in PROMPT_NEWLINE_KEYS:
                event.prevent_default()
                event.stop()
                self.insert("\n")
            elif event.key in ENTER_KEYS and self.app.should_insert_slash_suggestion:
                event.prevent_default()
                event.stop()
                self.app.action_insert_selected_slash_suggestion()
            elif event.key in ENTER_KEYS and self.value.strip().startswith("/"):
                event.prevent_default()
                event.stop()
                if not self.app.action_activate_safe_slash_command():
                    self.app.action_submit_prompt()
            elif event.key in ENTER_KEYS and self.app.palette_focus_active:
                event.prevent_default()
                event.stop()
                self.app.action_activate_selected_palette_entry()
            elif event.key in ENTER_KEYS:
                event.prevent_default()
                event.stop()
                self.app.action_submit_prompt()
            elif event.key in {"ctrl+up", "ctrl+down"}:
                event.prevent_default()
                event.stop()
                self.app.action_cycle_prompt_history(-1 if event.key == "ctrl+up" else 1)
            elif event.key in {"down", "up"} and self.app.slash_suggestions_visible:
                event.prevent_default()
                event.stop()
                self.app.action_move_slash_suggestion(1 if event.key == "down" else -1)
            elif event.key == "tab":
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
        theme = "harness-light"
        CSS = """
        Screen {
            layers: base overlay;
        }

        #layout {
            height: 1fr;
        }

        #chat {
            width: 2fr;
            border: round $primary;
            margin: 1 0 1 1;
            padding: 1;
            background: $surface;
        }

        #session-pane {
            width: 36;
            border: round $primary;
            margin: 1 0 1 1;
            padding: 1;
            background: $surface;
        }

        #session-pane:focus-within {
            border: round $accent;
        }

        #session-list {
            height: 1fr;
        }

        #session-list > ListItem {
            height: auto;
            min-height: 3;
            padding: 0 1;
        }

        #session-pane-header,
        #session-pane-detail,
        #session-pane-actions {
            height: auto;
        }

        #side {
            width: 1fr;
            border: round $primary;
            margin: 1 1 1 0;
            padding: 1;
            background: $surface;
        }

        #prompt {
            margin: 0 1;
            height: 5;
        }

        #composer-status {
            margin: 0 1;
            padding: 0 1 0 1;
            background: $surface;
        }

        #composer-footer {
            margin: 0 1 1 1;
            padding: 0 1;
            background: $surface;
            color: $text-muted;
        }

        #slash-status {
            margin: 0 1;
            padding: 0 1;
            border: round $accent;
            background: $surface;
        }

        #slash-status.hidden {
            display: none;
        }

        #dialog-overlay {
            layer: overlay;
            width: 100%;
            height: 100%;
            align: center middle;
            background: transparent;
        }

        #dialog-panel {
            width: 84;
            height: auto;
            max-height: 88%;
            padding: 1 2;
            border: round $accent;
            background: $surface;
        }

        #dialog-overlay.hidden {
            display: none;
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
            Binding("alt+up,ctrl+left,ctrl+pageup", "session_previous", "Previous session", priority=True),
            Binding("alt+down,ctrl+right,ctrl+pagedown", "session_next", "Next session", priority=True),
            Binding("ctrl+d", "delete_selected_session", "Purge session", priority=True),
            Binding("ctrl+x", "leader_key", "Leader", priority=True),
            Binding("ctrl+p,f2", "toggle_palette_focus", "Palette focus", priority=True),
        ]

        def __init__(self) -> None:
            super().__init__()
            self._app_service = service
            self.register_theme(harness_light_theme)
            self.theme = "harness-light"
            self._messages = [dict(message) for message in initial_messages]
            self._chat_state = ChatSessionState(codex_like_mode=codex_like)
            self._latest_response: dict = {}
            self._latest_palette_activation: dict = {}
            self._focus_mode = "dashboard"
            self._right_pane_mode = "overview"
            self._show_all_orchestrations = False
            self._selected_orchestration_id: str | None = None
            self._selected_graph_node_id: str | None = None
            self._pinned_orchestration_id: str | None = None
            self._active_section_id = "active_work"
            self._collapsed_section_ids: set[str] = set()
            self._section_cursor_index = _right_panel_section_index("active_work")
            self._session_cursor_index = 0
            self._selected_session_id: str | None = None
            self._session_filter = "open"
            self._session_query = ""
            self._session_search_active = False
            self._left_pane_focused = False
            self._left_pane_mode = "sessions"
            self._left_selected_item_id: str | None = None
            self._slash_suggestion_index = 0
            self._request_in_flight = False
            self._request_started_at: float | None = None
            self._prompt_history: list[str] = []
            self._prompt_history_index: int | None = None
            self._leader_key_active = False
            self._dialog_visible = False
            self._dialog_kind = ""
            self._dialog_query = ""
            self._dialog_selected_index = 0
            self._dialog_text_buffer = ""
            self._dialog_context: dict = {}
            self._selected_agent_id = "plan"
            self._selected_theme_id = "light"
            self._dashboard_cache: dict | None = dict(dashboard)
            self._dashboard_cache_session_id: str | None = None
            self._dashboard_cache_at = time.monotonic()
            self._refresh_timer = None
            self._live_refresh_failures = 0
            self._global_event_subscription = None
            self._session_event_subscription = None
            self._session_event_subscription_id: str | None = None
            self._last_closed_session_event_subscription = None
            self._event_refresh_dirty = False
            self._event_refresh_count = 0
            self._event_seen_ids: set[str] = set()
            self._transient_session_events: list[dict] = []
            self._event_stream_status = "starting"
            self._event_stream_failures = 0

        @property
        def slash_suggestions_visible(self) -> bool:
            prompt = self.query_one("#prompt", TextArea)
            return bool(_matching_slash_commands(slash_commands, prompt.value))

        @property
        def should_insert_slash_suggestion(self) -> bool:
            prompt = self.query_one("#prompt", TextArea)
            request = prompt.value.strip()
            if not request:
                return False
            inserted = self._request_from_prompt_submission(request)
            return bool(inserted and inserted != request)

        @property
        def palette_focus_active(self) -> bool:
            return self._focus_mode == "palette"

        @property
        def leader_key_active(self) -> bool:
            return self._leader_key_active

        @property
        def dialog_visible(self) -> bool:
            return self._dialog_visible

        @property
        def session_text_dialog_active(self) -> bool:
            return self._dialog_kind in {"session_delete", "session_rename"}

        def compose(self) -> ComposeResult:
            yield Header(show_clock=False)
            with Horizontal(id="layout"):
                with Vertical(id="session-pane"):
                    yield Static("", id="session-pane-header")
                    yield ListView(id="session-list")
                    yield Static("", id="session-pane-detail")
                    yield Static("", id="session-pane-actions")
                with VerticalScroll(id="chat"):
                    yield Static("", id="chat-content")
                with VerticalScroll(id="side"):
                    yield Static(render_right_panel_status(initial_view), id="search-status")
                    yield Static(_render_navigation_hints(initial_view), id="palette-status")
                    yield Static("", id="pane-container")
            with Container(id="dialog-overlay", classes="hidden"):
                yield Static("", id="dialog-panel")
            yield Static("", id="slash-status", classes="hidden")
            yield Static(
                _render_composer_status(dashboard, self._selected_agent_id, self._chat_state.session_id),
                id="composer-status",
            )
            yield HarnessPromptInput(placeholder="Ask Harness or type /help", id="prompt")
            yield Static(COMPOSER_FOOTER_HINTS, id="composer-footer")

        def on_mount(self) -> None:
            self._apply_theme_selection(self._selected_theme_id)
            self.query_one("#prompt", TextArea).focus()
            self._render_chat()
            self._render_current_view()
            self._start_event_subscriptions()
            self._refresh_timer = self.set_interval(1.5, self._refresh_live_view)

        def on_unmount(self) -> None:
            timer = self._refresh_timer
            if timer is not None and hasattr(timer, "stop"):
                timer.stop()
            self._close_event_subscriptions()

        def on_key(self, event) -> None:
            if self.session_text_dialog_active:
                event.prevent_default()
                event.stop()
                self.action_handle_text_dialog_key(event)
                return
            if event.key == "ctrl+x":
                event.prevent_default()
                event.stop()
                self.action_leader_key()
                return
            if event.key in SESSION_PREVIOUS_KEYS | SESSION_NEXT_KEYS:
                event.prevent_default()
                event.stop()
                if event.key in SESSION_PREVIOUS_KEYS:
                    self.action_session_previous()
                else:
                    self.action_session_next()
                return
            if event.key in SESSION_DELETE_KEYS:
                event.prevent_default()
                event.stop()
                self.action_delete_selected_session()
                return
            if self._dialog_visible and event.key in {"down", "up"}:
                event.prevent_default()
                event.stop()
                self.action_move_dialog_selection(1 if event.key == "down" else -1)
                return
            if self._dialog_visible and event.key in ENTER_KEYS:
                event.prevent_default()
                event.stop()
                self.action_activate_dialog_selection()
                return
            if self._left_pane_focused:
                if self._handle_session_pane_key(event):
                    return
            if self._leader_key_active:
                if self.is_leader_shortcut(event.key, event.character):
                    event.prevent_default()
                    event.stop()
                    self.action_handle_leader_key(event.key, event.character)
                    return
                self.action_cancel_leader_key()
            if isinstance(self.focused, TextArea):
                return
            if self._handle_right_pane_key(event):
                return
            if event.character == "c":
                event.prevent_default()
                event.stop()
                self.action_toggle_section_collapse()
            elif event.key == "shift+c" or event.character == "C":
                event.prevent_default()
                event.stop()
                self.action_expand_all_sections()

        def action_leader_key(self) -> None:
            self._leader_key_active = True
            self._dialog_query = ""
            self._dialog_selected_index = 0
            self._show_functionality_dialog()
            self._render_palette_activation_status("Leader: m Models, s Sessions.", ok=True)

        def is_leader_shortcut(self, key: str, character: str | None = None) -> bool:
            pressed = (character or key or "").casefold()
            return pressed in {"m", "s", "t"}

        def action_cancel_leader_key(self) -> None:
            self._leader_key_active = False

        def action_handle_leader_key(self, key: str, character: str | None = None) -> None:
            pressed = (character or key or "").casefold()
            self._leader_key_active = False
            if pressed == "m":
                self._show_models_list(source="leader", slash="ctrl+x m")
                return
            if pressed == "s":
                self.action_focus_session_pane()
                return
            if pressed == "t":
                self._show_theme_dialog()
                return
            self._latest_palette_activation = {
                "schema_version": "harness.tui_palette_activation/v1",
                "ok": False,
                "entry_id": f"leader.{pressed or 'unknown'}",
                "activation_kind": "leader_key",
                "ui_action_applied": False,
                "source": "leader",
                "slash": "ctrl+x",
                "slash_consumed": True,
                "chat_submitted": False,
                "model_request_started": False,
                "slash_suggestion_inserted": False,
                "evidence_status": "leader_key_unknown",
                "policy_boundary": _safe_palette_policy_boundary(),
                "blocked_reasons": ["leader_key_unknown"],
                **_palette_no_side_effect_flags(),
            }
            self._hide_dialog()
            self._render_palette_activation_status(f"Unknown leader key: {pressed or key}.", ok=False)
            self._render_current_view()

        def on_text_area_changed(self, event: TextArea.Changed) -> None:
            if event.text_area.id == "prompt":
                self._slash_suggestion_index = 0
                if self._dialog_kind == "models":
                    model_query = event.text_area.text.strip()
                    if model_query == "/model" or model_query.startswith("/model "):
                        model_query = model_query.removeprefix("/model").strip()
                    elif model_query == "/models" or model_query.startswith("/models "):
                        model_query = ""
                    if model_query != self._dialog_query:
                        self._dialog_selected_index = 0
                    self._dialog_query = model_query
                    self._show_model_dialog(query=model_query, selected_index=self._dialog_selected_index)
                elif self._dialog_kind == "commands":
                    command_query = event.text_area.text.strip()
                    if command_query != self._dialog_query:
                        self._dialog_selected_index = 0
                    self._dialog_query = command_query
                    self._show_functionality_dialog(query=command_query, selected_index=self._dialog_selected_index)
                self._render_current_view()
                self._render_slash_suggestions(event.text_area.text)

        def action_submit_prompt(self) -> None:
            prompt = self.query_one("#prompt", TextArea)
            if self._focus_mode == "palette":
                self.action_activate_selected_palette_entry()
                return
            if self._activate_models_slash_command(prompt.value):
                return
            if self._activate_model_slash_command(prompt.value):
                return
            if prompt.value.strip() == "/theme":
                prompt.value = ""
                self._show_theme_dialog()
                self._render_palette_activation_status("Select a theme with arrows, then enter.", ok=True)
                return
            if self._activate_safe_slash_command(prompt.value):
                return
            request = self._request_from_prompt_submission(prompt.value)
            if not request and self._focus_mode == "dashboard" and not self._request_in_flight:
                view = self._current_view()
                self._show_dialog(render_right_panel_detail(view), kind="right_panel_detail")
                self._latest_palette_activation = {
                    "schema_version": "harness.tui_palette_activation/v1",
                    "ok": True,
                    "entry_id": "right_panel.detail",
                    "activation_kind": "ui_action",
                    "ui_action_applied": True,
                    "source": "right_panel_enter",
                    "chat_submitted": False,
                    "model_request_started": False,
                    "slash_suggestion_inserted": False,
                    "evidence_status": "right_panel_detail_rendered",
                    "policy_boundary": _safe_palette_policy_boundary(),
                    "blocked_reasons": [],
                    **_palette_no_side_effect_flags(),
                }
                self._render_palette_activation_status("Opened right-panel detail.", ok=True)
                self._render_current_view()
                return
            if not request or self._request_in_flight:
                return
            self._prompt_history.append(request)
            self._prompt_history_index = None
            use_runtime_prompt = self._should_use_runtime_prompt(request)
            session_error: dict | None = None
            session_id: str | None = None
            if use_runtime_prompt:
                try:
                    session_id = self._ensure_runtime_prompt_session(request)
                except Exception as exc:
                    session_error = {
                        "ok": False,
                        "kind": "runtime_prompt_error",
                        "title": "Prompt Not Submitted",
                        "lines": [
                            SESSION_SCHEMA_REPAIR_MESSAGE if is_missing_session_schema_error(exc) else str(exc),
                        ],
                    }
            self._messages.append({"role": "user", "title": request, "lines": []})
            stream_index = len(self._messages)
            placeholder = "Queuing runtime turn..." if use_runtime_prompt and session_error is None else "Starting model turn..."
            self._messages.append({"role": "assistant", "title": "Assistant", "lines": [placeholder]})
            prompt.value = ""
            self._slash_suggestion_index = 0
            self._render_slash_suggestions("")
            prompt.placeholder = "Model is responding..."
            self._request_in_flight = True
            self._request_started_at = time.monotonic()
            self._render_chat()
            self._render_current_view()
            if session_error is not None:
                self._finish_chat_request(stream_index, session_error)
            elif use_runtime_prompt and session_id is not None:
                self.run_worker(lambda: self._run_runtime_prompt_request(request, stream_index, session_id), thread=True)
            else:
                self.run_worker(lambda: self._run_chat_request(request, stream_index), thread=True)

        def action_cycle_prompt_history(self, step: int) -> None:
            if not self._prompt_history:
                return
            if self._prompt_history_index is None:
                self._prompt_history_index = len(self._prompt_history) - 1 if step < 0 else 0
            else:
                self._prompt_history_index = (self._prompt_history_index + step) % len(self._prompt_history)
            prompt = self.query_one("#prompt", TextArea)
            prompt.value = self._prompt_history[self._prompt_history_index]
            self._render_slash_suggestions(prompt.value)

        def _run_chat_request(self, request: str, stream_index: int) -> None:
            def progress(update: dict) -> None:
                self.call_from_thread(self._append_stream_update, stream_index, update)

            try:
                response = handle_chat_input(request, project_root, self._chat_state, progress_callback=progress)
            except Exception as exc:
                error = SESSION_SCHEMA_REPAIR_MESSAGE if is_missing_session_schema_error(exc) else str(exc)
                response = {
                    "ok": False,
                    "kind": "chat_error",
                    "title": "Chat Error",
                    "lines": [error],
                }
            self.call_from_thread(self._finish_chat_request, stream_index, response)

        def _run_runtime_prompt_request(self, request: str, stream_index: int, session_id: str) -> None:
            try:
                dashboard = self._app_service.dashboard(selected_session_id=session_id)
                active_session = dashboard.get("active_session") or {}
                active_model = dashboard.get("active_model") or {}
                raw_model_ref = active_model.get("raw_model_ref") or active_session.get("raw_model_ref")
                response = self._app_service.prompt_async(
                    session_id,
                    {
                        "content": request,
                        "agent_id": self._selected_agent_id or active_session.get("agent_id"),
                        "raw_model_ref": raw_model_ref,
                        "queue_policy": "follow_up",
                        "source": "tui_prompt_submit",
                        "tui_submit": True,
                        "metadata": {"cwd": "."},
                    },
                )
                message = _runtime_prompt_response_to_tui_response(response)
            except Exception as exc:
                error = SESSION_SCHEMA_REPAIR_MESSAGE if is_missing_session_schema_error(exc) else str(exc)
                message = {
                    "ok": False,
                    "kind": "runtime_prompt_error",
                    "title": "Prompt Not Submitted",
                    "lines": [error],
                }
            self.call_from_thread(self._finish_chat_request, stream_index, message)

        def _should_use_runtime_prompt(self, request: str) -> bool:
            if request.startswith("/"):
                return False
            try:
                if not self._dashboard_snapshot().get("initialized"):
                    return False
            except Exception:
                return False
            if (
                self._chat_state.pending_draft is not None
                or self._chat_state.pending_orchestration is not None
                or self._chat_state.pending_execute_lease_id is not None
                or self._chat_state.pending_action_contract is not None
                or self._chat_state.pending_session_tool_call is not None
                or self._chat_state.pending_hosted_approval
            ):
                return False
            if getattr(self._chat_state.operator_runtime.phase, "value", "idle") != "idle":
                return False
            try:
                return route_chat_intent(request).get("intent") == "unsupported"
            except Exception:
                return False

        def _ensure_runtime_prompt_session(self, request: str) -> str:
            selected_id = self._selected_session_id or self._chat_state.session_id
            if selected_id:
                return selected_id
            title = request[:64].strip() or "New session"
            result = self._app_service.create_session(
                {
                    "title": title,
                    "intent": "tui_prompt_session",
                    "metadata": {"created_by": "tui_prompt_submit", "cwd": "."},
                    "agent_id": self._selected_agent_id,
                }
            )
            session = result["session"]
            session_id = str(session["id"])
            self._selected_session_id = session_id
            self._chat_state.session_id = session_id
            self._left_selected_item_id = f"session:{session_id}"
            self._session_filter = "open"
            return session_id

        def _append_stream_update(self, stream_index: int, update: dict) -> None:
            if stream_index >= len(self._messages):
                return
            message = self._messages[stream_index]
            lines = list(message.get("lines") or [])
            content = str(update.get("content") or "").strip()
            if not content:
                return
            if lines == ["Starting model turn..."]:
                lines = []
            kind = str(update.get("kind") or "content")
            if kind == "content":
                lines = _append_streaming_content(lines, content)
            else:
                for line in content.splitlines():
                    clean = line.strip()
                    if clean and (not lines or lines[-1] != clean):
                        lines.append(clean)
            message["title"] = "Assistant Streaming"
            message["lines"] = lines[-80:]
            self._render_chat()

        def _finish_chat_request(self, stream_index: int, response: dict) -> None:
            self._latest_response = dict(response)
            existing_lines: list[str] = []
            if stream_index < len(self._messages):
                existing_lines = [
                    str(line)
                    for line in self._messages[stream_index].get("lines", [])
                    if str(line).strip() and str(line).strip() not in {"Starting model turn...", "Queuing runtime turn..."}
                ]
            final_message = _chat_response_to_tui_message(response)
            if existing_lines:
                final_lines = [str(line) for line in final_message.get("lines", []) if str(line).strip()]
                final_title = str(final_message.get("title") or "")
                if final_title in {"", "Assistant"}:
                    final_message["title"] = "Assistant Streaming"
                final_message["lines"] = _merge_codex_stream_and_final_lines(existing_lines, final_lines)
            if stream_index < len(self._messages):
                self._messages[stream_index] = final_message
            else:
                self._messages.append(final_message)
            self._request_in_flight = False
            prompt = self.query_one("#prompt", TextArea)
            if self._chat_state.pending_action_contract is not None:
                prompt.placeholder = "Type yes to confirm, no to cancel, or ask a follow-up"
            else:
                prompt.placeholder = "Ask Harness or type /help"
            self._render_chat()
            self._render_current_view()
            self._render_slash_suggestions(prompt.value)
            self._request_started_at = None
            if response.get("kind") == "quit":
                self.exit()

        def action_clear_search(self) -> None:
            if self._dialog_visible or self._leader_key_active:
                self._leader_key_active = False
                self._hide_dialog()
                return
            if self._session_search_active:
                self._session_search_active = False
                self._session_query = ""
                self._render_current_view()
                return
            if self._left_pane_focused:
                self.action_focus_composer()
                return
            prompt = self.query_one("#prompt", TextArea)
            if prompt.value:
                prompt.value = ""
                self._render_slash_suggestions("")
            else:
                self._focus_mode = "dashboard"
                self._collapsed_section_ids.clear()
                self._right_pane_mode = "overview"
                self._show_all_orchestrations = False
                self._section_cursor_index = _right_panel_section_index("active_work")
                self._active_section_id = "active_work"
                self._render_current_view()

        def action_section_next(self) -> None:
            self._move_section_cursor(1)

        def action_section_previous(self) -> None:
            self._move_section_cursor(-1)

        def _handle_right_pane_key(self, event) -> bool:
            pressed = str(event.character or event.key or "")
            key = str(event.key or "")
            if key in ENTER_KEYS:
                event.prevent_default()
                event.stop()
                self.action_open_right_panel_detail()
                return True
            if pressed in {"?", "/?"} or key == "?":
                event.prevent_default()
                event.stop()
                self.action_show_right_panel_shortcuts()
                return True
            if pressed.isdigit() and pressed != "0":
                event.prevent_default()
                event.stop()
                self.action_select_orchestration_index(int(pressed) - 1)
                return True
            normalized = pressed.casefold()
            if normalized == "o":
                event.prevent_default()
                event.stop()
                self.action_set_right_pane_mode("overview")
                return True
            if normalized == "g":
                event.prevent_default()
                event.stop()
                self.action_toggle_graph_mode()
                return True
            if normalized == "e":
                event.prevent_default()
                event.stop()
                self.action_set_right_pane_mode("evidence")
                return True
            if normalized == "p":
                event.prevent_default()
                event.stop()
                self.action_focus_right_pane_section("active_work")
                return True
            if normalized == "a":
                event.prevent_default()
                event.stop()
                self.action_focus_attention_node()
                return True
            if normalized == "r":
                event.prevent_default()
                event.stop()
                self.action_set_right_pane_mode("evidence")
                self.action_focus_right_pane_section("evidence")
                return True
            if normalized == "f":
                event.prevent_default()
                event.stop()
                self.action_focus_attention_node()
                return True
            if key == "space" or pressed == " ":
                event.prevent_default()
                event.stop()
                self.action_toggle_pinned_orchestration()
                return True
            return False

        def action_set_right_pane_mode(self, mode: str) -> None:
            self._right_pane_mode = mode if mode in {"overview", "graph", "evidence"} else "overview"
            if self._right_pane_mode != "graph":
                self._show_all_orchestrations = False
            default_section = "graph" if self._right_pane_mode == "graph" else "evidence" if self._right_pane_mode == "evidence" else "active_work"
            self.action_focus_right_pane_section(default_section, render=False)
            self._render_current_view()

        def action_toggle_graph_mode(self) -> None:
            if self._right_pane_mode == "graph":
                self._show_all_orchestrations = not self._show_all_orchestrations
            else:
                self._right_pane_mode = "graph"
                self._show_all_orchestrations = False
            self.action_focus_right_pane_section("graph", render=False)
            self._render_current_view()

        def action_focus_right_pane_section(self, section_id: str, *, render: bool = True) -> None:
            resolved = _right_panel_resolve_section_id(section_id)
            self._active_section_id = resolved
            self._section_cursor_index = _right_panel_section_index(resolved)
            if render:
                self._render_current_view()

        def action_select_orchestration_index(self, index: int) -> None:
            view = self._current_view()
            instances = view.get("orchestration_instances") or []
            if not instances:
                self._render_palette_activation_status("No orchestration to select.", ok=False)
                return
            selected = instances[max(0, min(index, len(instances) - 1))]
            self._selected_orchestration_id = str(selected.get("orchestration_id") or "")
            graph = next(
                (item for item in view.get("all_graphs") or [] if item.get("orchestration_id") == self._selected_orchestration_id),
                None,
            )
            self._selected_graph_node_id = (graph or {}).get("selected_node_id")
            self._right_pane_mode = "overview"
            self._show_all_orchestrations = False
            self.action_focus_right_pane_section("active_work", render=False)
            self._render_current_view()

        def action_focus_attention_node(self) -> None:
            view = self._current_view()
            graph = view.get("graph") or {}
            node_id = None
            for candidate in graph.get("attention_node_ids") or []:
                node_id = candidate
                break
            if node_id is None:
                for candidate in graph.get("active_node_ids") or []:
                    node_id = candidate
                    break
            if node_id is None and graph.get("nodes"):
                node_id = graph["nodes"][0].get("id")
            self._selected_graph_node_id = str(node_id) if node_id else None
            self._active_section_id = "node_details"
            self._section_cursor_index = _right_panel_section_index("node_details")
            if self._right_pane_mode == "overview":
                self._right_pane_mode = "graph"
            self._render_current_view()

        def action_toggle_pinned_orchestration(self) -> None:
            if self._selected_orchestration_id and self._pinned_orchestration_id != self._selected_orchestration_id:
                self._pinned_orchestration_id = self._selected_orchestration_id
                self._render_palette_activation_status("Pinned orchestration.", ok=True)
            else:
                self._pinned_orchestration_id = None
                self._render_palette_activation_status("Unpinned orchestration.", ok=True)
            self._render_current_view()

        def action_show_right_panel_shortcuts(self) -> None:
            view = self._current_view()
            self._show_dialog(render_right_panel_detail({**view, "shortcuts_visible": True}, "shortcuts"), kind="right_panel_shortcuts")
            self._latest_palette_activation = {
                "schema_version": "harness.tui_palette_activation/v1",
                "ok": True,
                "entry_id": "right_panel.shortcuts",
                "activation_kind": "ui_action",
                "ui_action_applied": True,
                "source": "right_panel_shortcuts",
                "chat_submitted": False,
                "model_request_started": False,
                "slash_suggestion_inserted": False,
                "evidence_status": "right_panel_shortcuts_rendered",
                "policy_boundary": _safe_palette_policy_boundary(),
                "blocked_reasons": [],
                **_palette_no_side_effect_flags(),
            }
            self._render_palette_activation_status("Opened right-panel shortcuts.", ok=True)
            self._render_current_view()

        def action_open_right_panel_detail(self) -> None:
            view = self._current_view()
            self._show_dialog(render_right_panel_detail(view), kind="right_panel_detail")
            self._latest_palette_activation = {
                "schema_version": "harness.tui_palette_activation/v1",
                "ok": True,
                "entry_id": "right_panel.detail",
                "activation_kind": "ui_action",
                "ui_action_applied": True,
                "source": "right_panel_enter",
                "chat_submitted": False,
                "model_request_started": False,
                "slash_suggestion_inserted": False,
                "evidence_status": "right_panel_detail_rendered",
                "policy_boundary": _safe_palette_policy_boundary(),
                "blocked_reasons": [],
                **_palette_no_side_effect_flags(),
            }
            self._render_palette_activation_status("Opened right-panel detail.", ok=True)
            self._render_current_view()

        def action_session_next(self) -> None:
            self._move_session_cursor(1)

        def action_session_previous(self) -> None:
            self._move_session_cursor(-1)

        def action_delete_selected_session(self) -> None:
            self.action_open_session_delete_dialog()

        def action_focus_session_pane(self) -> None:
            self._left_pane_focused = True
            self._leader_key_active = False
            self._hide_dialog()
            self._render_current_view()
            try:
                self.query_one("#session-list", ListView).focus()
            except NoMatches:
                return

        def action_focus_composer(self) -> None:
            self._left_pane_focused = False
            self._session_search_active = False
            try:
                self.query_one("#prompt", TextArea).focus()
            except NoMatches:
                return
            self._render_current_view()

        def _handle_session_pane_key(self, event) -> bool:
            if self._session_search_active:
                event.prevent_default()
                event.stop()
                self._handle_session_search_key(event)
                return True
            key = event.key
            character = event.character or ""
            if key in {"up", "k"}:
                event.prevent_default()
                event.stop()
                self._move_left_pane_selection(-1)
                return True
            if key in {"down", "j"}:
                event.prevent_default()
                event.stop()
                self._move_left_pane_selection(1)
                return True
            if key == "enter":
                event.prevent_default()
                event.stop()
                self.action_activate_left_nav_selection()
                return True
            if character in {"1", "2", "3", "4"}:
                event.prevent_default()
                event.stop()
                self.action_set_left_pane_mode(int(character) - 1)
                return True
            if key == "escape":
                event.prevent_default()
                event.stop()
                self.action_focus_composer()
                return True
            if character == "/" or character.casefold() == "s":
                event.prevent_default()
                event.stop()
                self._session_search_active = True
                self._session_query = ""
                self._render_current_view()
                return True
            if character == "n":
                event.prevent_default()
                event.stop()
                self.action_create_blank_session()
                return True
            if character == "a":
                event.prevent_default()
                event.stop()
                if self._left_pane_mode == "sessions":
                    self.action_archive_selected_session()
                else:
                    self._render_palette_activation_status("Archive applies to sessions.", ok=False)
                return True
            if character == "r":
                event.prevent_default()
                event.stop()
                self.action_restore_selected_session()
                return True
            if character == "d":
                event.prevent_default()
                event.stop()
                self.action_open_session_delete_dialog()
                return True
            if character == "x":
                event.prevent_default()
                event.stop()
                self.action_abort_selected_session()
                return True
            if character == "e":
                event.prevent_default()
                event.stop()
                if self._left_pane_mode == "sessions":
                    self.action_open_session_rename_dialog()
                else:
                    self._render_palette_activation_status("Rename applies to sessions.", ok=False)
                return True
            if character == "F" or key == "shift+f":
                event.prevent_default()
                event.stop()
                self.action_fork_selected_session()
                return True
            if character == "f":
                event.prevent_default()
                event.stop()
                self.action_cycle_session_filter()
                return True
            if character == "m":
                event.prevent_default()
                event.stop()
                self._show_model_dialog()
                return True
            if character == "g":
                event.prevent_default()
                event.stop()
                if not self.action_go_to_active_orchestration():
                    self._show_agent_dialog()
                return True
            if character == "b":
                event.prevent_default()
                event.stop()
                self.action_go_to_first_blocked_item()
                return True
            return False

        def _handle_session_search_key(self, event) -> None:
            if event.key in {"escape", "enter"}:
                self._session_search_active = False
                self._render_current_view()
                return
            if event.key == "backspace":
                self._session_query = self._session_query[:-1]
                self._render_current_view()
                return
            if event.key == "ctrl+u":
                self._session_query = ""
                self._render_current_view()
                return
            if event.character and event.character.isprintable():
                self._session_query += event.character
                self._render_current_view()

        def _session_pane_projection(self) -> dict:
            selected_id = self._selected_session_id or self._chat_state.session_id
            projection = self._app_service.session_pane(
                selected_session_id=selected_id,
                status_filter=self._session_filter,
                query=self._session_query,
            )
            self._selected_session_id = projection.get("selected_session_id")
            self._sync_session_event_subscription()
            sessions = projection.get("sessions") or []
            self._session_cursor_index = next(
                (index for index, session in enumerate(sessions) if session.get("id") == self._selected_session_id),
                0,
            )
            return projection

        def _left_pane_projection(
            self,
            dashboard_snapshot: dict | None = None,
            right_view: dict | None = None,
        ) -> dict:
            dashboard_view = dashboard_snapshot or self._dashboard_snapshot()
            cockpit_view = right_view or self._current_view()
            view = build_left_pane_view(
                dashboard_view,
                {
                    "left_pane_mode": self._left_pane_mode,
                    "left_selected_item_id": f"session:{self._selected_session_id}"
                    if self._left_pane_mode == "sessions" and self._selected_session_id
                    else self._left_selected_item_id,
                    "session_filter": self._session_filter,
                    "selected_session_id": self._selected_session_id or self._chat_state.session_id,
                    "active_session_id": self._chat_state.session_id,
                    "selected_orchestration_id": self._selected_orchestration_id,
                    "model_profile": self._selected_agent_id,
                },
                self._session_query,
                right_pane_model=cockpit_view,
            )
            self._left_selected_item_id = view.get("selected_item_id")
            if self._left_pane_mode == "sessions" and self._left_selected_item_id:
                prefix = "session:"
                if str(self._left_selected_item_id).startswith(prefix):
                    self._selected_session_id = str(self._left_selected_item_id)[len(prefix) :]
            return view

        def _selected_session_row(self) -> dict | None:
            projection = self._session_pane_projection()
            selected_id = projection.get("selected_session_id")
            return next((session for session in projection.get("sessions", []) if session.get("id") == selected_id), None)

        def _move_session_pane_selection(self, step: int) -> None:
            self._move_left_pane_selection(step)

        def _move_left_pane_selection(self, step: int) -> None:
            view = self._left_pane_projection()
            items = left_pane_visible_items(view)
            if not items:
                self._left_selected_item_id = None
                self._session_cursor_index = 0
                self._render_current_view()
                return
            current = self._left_selected_item_id or view.get("selected_item_id") or items[0].get("nav_id")
            current_index = next((index for index, item in enumerate(items) if item.get("nav_id") == current), 0)
            selected = items[(current_index + step) % len(items)]
            self._left_selected_item_id = str(selected.get("nav_id") or "")
            if selected.get("kind") == "session":
                self._selected_session_id = str(selected.get("id") or "")
                self._session_cursor_index = (current_index + step) % len(items)
            self._render_current_view()

        def action_set_left_pane_mode(self, index: int) -> None:
            modes = ("sessions", "orchestrations", "agents", "queue")
            self._left_pane_mode = modes[max(0, min(index, len(modes) - 1))]
            self._left_selected_item_id = None
            self._session_search_active = False
            self._render_current_view()

        def action_activate_left_nav_selection(self) -> None:
            view = self._left_pane_projection()
            item = selected_left_pane_item(view)
            if item is None:
                self._render_palette_activation_status("No navigator item selected.", ok=False)
                return
            kind = item.get("kind")
            if kind == "session":
                self._activate_session_nav_item(item)
                return
            if kind == "orchestration":
                self._activate_orchestration_nav_item(item)
                return
            if kind == "agent":
                self._activate_agent_nav_item(item)
                return
            if kind == "queue_task":
                self._activate_queue_nav_item(item)
                return
            self._render_palette_activation_status("Navigator item is not openable.", ok=False)

        def _activate_session_nav_item(self, item: dict) -> None:
            session_id = str(item.get("id") or "")
            if not session_id:
                self._render_palette_activation_status("No session selected.", ok=False)
                return
            self._chat_state.session_id = session_id
            self._selected_session_id = session_id
            if item.get("agent_id"):
                self._selected_agent_id = str(item.get("agent_id"))
            self._right_pane_mode = "overview"
            self.action_focus_right_pane_section("context", render=False)
            self._render_palette_activation_status(f"Switched to {item.get('title') or session_id}.", ok=True)
            self._render_current_view()

        def _activate_orchestration_nav_item(self, item: dict) -> None:
            orchestration_id = str(item.get("id") or "")
            if not orchestration_id:
                self._render_palette_activation_status("No orchestration selected.", ok=False)
                return
            self._selected_orchestration_id = orchestration_id
            self._right_pane_mode = "overview"
            self._show_all_orchestrations = False
            self.action_focus_right_pane_section("active_work", render=False)
            self._render_palette_activation_status(f"Inspecting {item.get('title') or orchestration_id}.", ok=True)
            self._render_current_view()

        def _activate_agent_nav_item(self, item: dict) -> None:
            orchestration_id = item.get("current_orchestration_id")
            task_id = item.get("current_task_id")
            if orchestration_id:
                self._selected_orchestration_id = str(orchestration_id)
            if task_id:
                self._selected_graph_node_id = f"task:{task_id}"
            self._right_pane_mode = "graph"
            self._show_all_orchestrations = False
            self.action_focus_right_pane_section("node_details", render=False)
            self._render_palette_activation_status(f"Inspecting agent {item.get('label') or item.get('id')}.", ok=True)
            self._render_current_view()

        def _activate_queue_nav_item(self, item: dict) -> None:
            if item.get("orchestration_id"):
                self._selected_orchestration_id = str(item["orchestration_id"])
            if item.get("id"):
                self._selected_graph_node_id = str(item["id"])
            self._right_pane_mode = "graph"
            self._show_all_orchestrations = False
            section = "attention" if item.get("state") == "blocked" else "node_details"
            self.action_focus_right_pane_section(section, render=False)
            self._render_palette_activation_status(f"Inspecting task {item.get('title') or item.get('id')}.", ok=True)
            self._render_current_view()

        def action_go_to_active_orchestration(self) -> bool:
            view = self._left_pane_projection()
            candidates = [
                item
                for item in view.get("orchestrations") or []
                if item.get("state") in {"running", "blocked"}
            ]
            if not candidates:
                return False
            selected = candidates[0]
            self._left_pane_mode = "orchestrations"
            self._left_selected_item_id = f"orchestration:{selected['id']}"
            self._activate_orchestration_nav_item({"kind": "orchestration", **selected})
            return True

        def action_go_to_first_blocked_item(self) -> None:
            view = self._left_pane_projection()
            blocked = [
                item
                for item in (view.get("queue") or {}).get("items") or []
                if item.get("state") == "blocked"
            ]
            if not blocked:
                self._render_palette_activation_status("No blocked queue item.", ok=False)
                return
            selected = blocked[0]
            self._left_pane_mode = "queue"
            self._left_selected_item_id = str(selected.get("id") or "")
            self._activate_queue_nav_item({"kind": "queue_task", **selected})

        def _move_session_pane_selection_old(self, step: int) -> None:
            projection = self._session_pane_projection()
            sessions = projection.get("sessions") or []
            if not sessions:
                self._selected_session_id = None
                self._session_cursor_index = 0
                self._render_current_view()
                return
            self._session_cursor_index = (self._session_cursor_index + step) % len(sessions)
            self._selected_session_id = str(sessions[self._session_cursor_index].get("id") or "")
            self._render_current_view()

        def action_switch_to_selected_session(self) -> None:
            row = self._selected_session_row()
            if row is None:
                self._render_palette_activation_status("No session selected.", ok=False)
                return
            self._chat_state.session_id = str(row["id"])
            self._left_selected_item_id = f"session:{row['id']}"
            self._selected_agent_id = str(row.get("agent_id") or self._selected_agent_id)
            self._render_palette_activation_status(f"Switched to {row.get('display_title') or row['id']}.", ok=True)
            self._render_current_view()

        def action_create_blank_session(self) -> None:
            try:
                result = self._app_service.create_session(
                    {
                        "title": "New session",
                        "intent": "tui_blank_session",
                        "metadata": {"created_by": "tui_session_pane", "cwd": "."},
                    }
                )
            except Exception as exc:
                self._render_palette_activation_status(f"New session failed: {exc}", ok=False)
                return
            session = result["session"]
            self._selected_session_id = session["id"]
            self._left_selected_item_id = f"session:{session['id']}"
            self._chat_state.session_id = session["id"]
            self._session_filter = "open"
            self._session_query = ""
            self._session_search_active = False
            self.query_one("#prompt", TextArea).value = ""
            self._dashboard_snapshot(force=True)
            self._render_palette_activation_status(f"Created session {session['id']}.", ok=True)
            self._render_current_view()

        def action_archive_selected_session(self) -> None:
            row = self._selected_session_row()
            if row is None:
                self._render_palette_activation_status("No session selected.", ok=False)
                return
            if not row.get("can_archive"):
                self._render_palette_activation_status("Selected session is already archived.", ok=False)
                return
            try:
                self._app_service.archive_session(str(row["id"]))
            except Exception as exc:
                self._render_palette_activation_status(f"Archive failed: {exc}", ok=False)
                return
            self._after_session_removed_or_hidden(str(row["id"]))
            self._render_palette_activation_status(f"Archived {row.get('display_title') or row['id']}.", ok=True)
            self._render_current_view()

        def action_restore_selected_session(self) -> None:
            row = self._selected_session_row()
            if row is None:
                self._render_palette_activation_status("No session selected.", ok=False)
                return
            if not row.get("can_restore"):
                self._render_palette_activation_status("Selected session is not archived.", ok=False)
                return
            try:
                result = self._app_service.restore_session(str(row["id"]))
            except Exception as exc:
                self._render_palette_activation_status(f"Restore failed: {exc}", ok=False)
                return
            session = result["session"]
            self._selected_session_id = session["id"]
            self._left_selected_item_id = f"session:{session['id']}"
            self._chat_state.session_id = session["id"]
            if self._session_filter == "archived":
                self._session_filter = "open"
            self._dashboard_snapshot(force=True)
            self._render_palette_activation_status(f"Restored {row.get('display_title') or row['id']}.", ok=True)
            self._render_current_view()

        def action_abort_selected_session(self) -> None:
            row = self._selected_session_row()
            if row is None:
                self._render_palette_activation_status("No session selected.", ok=False)
                return
            if not row.get("can_abort"):
                self._render_palette_activation_status("Only running or waiting sessions can be aborted here.", ok=False)
                return
            try:
                self._app_service.abort_session(str(row["id"]), {"reason": "tui_session_pane"})
            except Exception as exc:
                self._render_palette_activation_status(f"Abort failed: {exc}", ok=False)
                return
            self._dashboard_snapshot(force=True)
            self._render_palette_activation_status("Cancelled session metadata. No process was stopped.", ok=True)
            self._render_current_view()

        def action_fork_selected_session(self) -> None:
            row = self._selected_session_row()
            if row is None:
                self._render_palette_activation_status("No session selected.", ok=False)
                return
            try:
                result = self._app_service.fork_session(
                    str(row["id"]),
                    {"title": f"Fork of {row.get('display_title') or row['id']}"},
                )
            except Exception as exc:
                self._render_palette_activation_status(f"Fork failed: {exc}", ok=False)
                return
            child = result["session"]
            self._selected_session_id = child["id"]
            self._left_selected_item_id = f"session:{child['id']}"
            self._chat_state.session_id = child["id"]
            self._session_filter = "open"
            self._dashboard_snapshot(force=True)
            self._render_palette_activation_status(f"Forked session {child['id']}.", ok=True)
            self._render_current_view()

        def action_cycle_session_filter(self) -> None:
            index = SESSION_PANE_FILTERS.index(self._session_filter) if self._session_filter in SESSION_PANE_FILTERS else 0
            self._session_filter = SESSION_PANE_FILTERS[(index + 1) % len(SESSION_PANE_FILTERS)]
            self._session_query = ""
            self._session_search_active = False
            self._render_palette_activation_status(f"Session filter: {self._session_filter}.", ok=True)
            self._render_current_view()

        def action_open_session_delete_dialog(self) -> None:
            row = self._selected_session_row()
            if row is None:
                self._render_palette_activation_status("No session selected.", ok=False)
                return
            if self._request_in_flight and row.get("id") == self._chat_state.session_id:
                self._render_palette_activation_status("Cannot hard delete the active in-flight session.", ok=False)
                return
            self._dialog_text_buffer = ""
            self._dialog_context = {"session": row}
            self._show_dialog(render_session_delete_dialog(row, buffer=""), kind="session_delete")

        def action_open_session_rename_dialog(self) -> None:
            row = self._selected_session_row()
            if row is None:
                self._render_palette_activation_status("No session selected.", ok=False)
                return
            self._dialog_text_buffer = ""
            self._dialog_context = {"session": row}
            self._show_dialog(render_session_rename_dialog(row, buffer=""), kind="session_rename")

        def _after_session_removed_or_hidden(self, removed_session_id: str) -> None:
            self._dashboard_snapshot(force=True)
            projection = self._app_service.session_pane(
                selected_session_id=None,
                status_filter=self._session_filter,
                query=self._session_query,
            )
            remaining = projection.get("sessions") or []
            self._selected_session_id = projection.get("selected_session_id")
            self._left_selected_item_id = f"session:{self._selected_session_id}" if self._selected_session_id else None
            if self._chat_state.session_id == removed_session_id:
                self._chat_state.session_id = self._selected_session_id
            if not remaining:
                self._session_cursor_index = 0

        def action_toggle_palette_focus(self) -> None:
            self._focus_mode = "palette" if self._focus_mode == "dashboard" else "dashboard"
            self._render_current_view()

        def action_activate_selected_palette_entry(self) -> None:
            prompt = self.query_one("#prompt", TextArea)
            palette = self._palette_snapshot()
            filtered = filter_command_palette(palette, prompt.value)
            entry = filtered["entries"][0] if filtered["entries"] else None
            if entry is None:
                self._latest_palette_activation = {
                    **activate_command_palette_entry(
                        palette,
                        str(prompt.value or "missing"),
                        {
                            "focus_mode": self._focus_mode,
                            "active_section_id": self._active_section_id,
                            "active_section_index": self._section_cursor_index,
                            "collapsed_section_ids": self._collapsed_section_ids,
                            "selected_theme": self._selected_theme_id,
                        },
                    ),
                    "source": "palette_enter",
                    "enter_consumed": True,
                    "chat_submitted": False,
                    "slash_suggestion_inserted": False,
                }
                self._render_palette_activation_status("No matching palette action.", ok=False)
                self._render_current_view()
                return
            if entry.get("id") == "ui_controls.theme_cycle":
                prompt.value = ""
                self._show_theme_dialog()
                self._render_palette_activation_status("Select a theme with arrows, then enter.", ok=True)
                return
            activation = activate_command_palette_entry(
                palette,
                str(entry["id"]),
                {
                    "focus_mode": self._focus_mode,
                    "active_section_id": self._active_section_id,
                    "active_section_index": self._section_cursor_index,
                    "collapsed_section_ids": self._collapsed_section_ids,
                    "selected_theme": self._selected_theme_id,
                },
            )
            self._latest_palette_activation = {
                **activation,
                "source": "palette_enter",
                "enter_consumed": True,
                "chat_submitted": False,
                "slash_suggestion_inserted": False,
            }
            if activation.get("ok"):
                state = activation.get("view_state") or {}
                if activation.get("activation_kind") == "session_model_selection":
                    activation = self._persist_model_selection(activation, source="palette")
                    self._latest_palette_activation = {
                        **activation,
                        "source": "palette_enter",
                        "enter_consumed": True,
                        "chat_submitted": False,
                        "slash_suggestion_inserted": False,
                    }
                    if activation.get("ok"):
                        self._focus_mode = "dashboard"
                        prompt.value = ""
                        self._render_palette_activation_status(
                            f"Selected model {activation.get('raw_model_ref')}.",
                            ok=True,
                        )
                    else:
                        self._focus_mode = "palette"
                        self._render_palette_activation_status(
                            f"Model selection blocked: {', '.join(activation.get('blocked_reasons') or ['unknown'])}.",
                            ok=False,
                        )
                else:
                    self._focus_mode = str(state.get("focus_mode") or self._focus_mode)
                    self._active_section_id = _right_panel_resolve_section_id(
                        state.get("active_section_id")
                        or _right_panel_section_id_at_index(state.get("active_section_index"))
                    )
                    self._section_cursor_index = _right_panel_section_index(self._active_section_id)
                    self._collapsed_section_ids = set(normalize_right_panel_collapsed_sections(state.get("collapsed_section_ids")))
                    self._selected_agent_id = str(state.get("selected_agent_id") or self._selected_agent_id)
                    self._apply_theme_selection(str(state.get("selected_theme") or self._selected_theme_id))
                    prompt.value = str(state.get("query", ""))
                    self._record_palette_activation_event(activation, source="palette")
                    self._render_palette_activation_status(f"Activated {entry['id']}.", ok=True)
            else:
                self._focus_mode = "palette"
                self._render_palette_activation_status(
                    f"Manual preview only: {entry.get('command') or entry['id']}",
                    ok=False,
                )
            self._render_current_view()

        def _activate_model_slash_command(self, value: str) -> bool:
            request = value.strip()
            if not (request == "/model" or request.startswith("/model ")):
                return False
            prompt = self.query_one("#prompt", TextArea)
            query = request.removeprefix("/model").strip()
            if not query:
                self._focus_mode = "dashboard"
                self._active_section_id = "project"
                self._section_cursor_index = _right_panel_section_index("project")
                prompt.value = "/model "
                try:
                    prompt.cursor_position = len(prompt.value)
                except AttributeError:
                    pass
                self._latest_palette_activation = {
                    "schema_version": "harness.tui_palette_activation/v1",
                    "ok": True,
                    "entry_id": "slash.model",
                    "activation_kind": "model_picker_help",
                    "ui_action_applied": True,
                    "source": "slash",
                    "slash": "/model",
                    "slash_consumed": True,
                    "chat_submitted": False,
                    "model_request_started": False,
                    "slash_suggestion_inserted": False,
                    "evidence_status": "ui_focus_in_memory",
                    "policy_boundary": _safe_palette_policy_boundary(),
                    "blocked_reasons": [],
                    **_palette_no_side_effect_flags(),
                }
                self._show_model_dialog()
                self._render_palette_activation_status("Type /model <provider/model> or /model <search>.", ok=True)
                self._render_current_view()
                return True

            palette = self._palette_snapshot()
            model_entries = [entry for entry in palette.get("entries", []) if entry.get("group_id") == "model_selection"]
            matches: list[dict]
            if query.isdigit():
                index = int(query)
                matches = [model_entries[index - 1]] if 1 <= index <= len(model_entries) else []
            else:
                lowered = query.casefold()
                matches = [
                    entry
                    for entry in model_entries
                    if lowered in str(entry.get("model_ref") or "").casefold()
                    or lowered in str(entry.get("title") or "").casefold()
                    or lowered in str(entry.get("description") or "").casefold()
                    or lowered in str(entry.get("provider_id") or "").casefold()
                    or lowered in str(entry.get("model_id") or "").casefold()
                ]
            exact = [
                entry
                for entry in matches
                if query == str(entry.get("model_ref") or "")
                or query == str(entry.get("model_id") or "")
            ]
            if len(exact) == 1:
                matches = exact
            if len(matches) != 1:
                prompt.value = query
                self._focus_mode = "palette"
                self._latest_palette_activation = {
                    "schema_version": "harness.tui_palette_activation/v1",
                    "ok": False,
                    "entry_id": "slash.model",
                    "activation_kind": "session_model_selection",
                    "ui_action_applied": False,
                    "source": "slash",
                    "slash": "/model",
                    "slash_consumed": True,
                    "chat_submitted": False,
                    "model_request_started": False,
                    "slash_suggestion_inserted": False,
                    "evidence_status": "session_model_selection_needs_unique_match",
                    "policy_boundary": _model_selection_policy_boundary(),
                    "blocked_reasons": ["model_match_missing" if not matches else "model_match_ambiguous"],
                    "match_count": len(matches),
                    **_palette_no_side_effect_flags(),
                }
                if matches:
                    visible = ", ".join(str(entry.get("model_ref")) for entry in matches[:4])
                    self._render_palette_activation_status(f"Model query matched {len(matches)} models: {visible}.", ok=False)
                else:
                    self._render_palette_activation_status(f"No model matched {query}.", ok=False)
                self._show_model_dialog(query=query)
                self._render_current_view()
                return True

            entry = matches[0]
            activation = activate_command_palette_entry(
                palette,
                str(entry["id"]),
                {
                    "focus_mode": self._focus_mode,
                    "active_section_id": self._active_section_id,
                    "active_section_index": self._section_cursor_index,
                    "collapsed_section_ids": self._collapsed_section_ids,
                },
            )
            activation = self._persist_model_selection(activation, source="slash")
            self._latest_palette_activation = {
                **activation,
                "source": "slash",
                "slash": "/model",
                "slash_consumed": True,
                "chat_submitted": False,
                "model_request_started": False,
                "slash_suggestion_inserted": False,
            }
            if activation.get("ok"):
                self._focus_mode = "dashboard"
                prompt.value = ""
                self._hide_dialog()
                self._render_palette_activation_status(f"Selected model {activation.get('raw_model_ref')}.", ok=True)
            else:
                self._focus_mode = "palette"
                prompt.value = query
                self._show_model_dialog(query=query)
                self._render_palette_activation_status(
                    f"Model selection blocked: {', '.join(activation.get('blocked_reasons') or ['unknown'])}.",
                    ok=False,
                )
            self._render_current_view()
            return True

        def _activate_models_slash_command(self, value: str) -> bool:
            request = value.strip()
            if request not in {"/models", "/models list"}:
                return False
            prompt = self.query_one("#prompt", TextArea)
            self._show_models_list(source="slash", slash="/models")
            prompt.value = ""
            return True

        def _show_models_list(self, *, source: str, slash: str) -> None:
            dashboard = self._dashboard_snapshot(force=True)
            models = _unique_model_catalog_entries((dashboard.get("model_catalog") or {}).get("models") or [])
            active = ((dashboard.get("model_catalog") or {}).get("active_model") or {}).get("raw_model_ref")
            lines = ["Models:"]
            if not models:
                lines.append("none")
            for index, model in enumerate(models[:12], start=1):
                raw_ref = str(model.get("raw_model_ref") or "")
                marker = "*" if raw_ref == active else " "
                lines.append(f"{index}. {marker} {raw_ref}")
            lines.extend(["Select: /model <number>", "Search: /model <name>", "Exact: /model <provider/model>"])
            self._messages.append({"role": "assistant", "title": "Model Selection", "lines": lines})
            self._latest_palette_activation = {
                "schema_version": "harness.tui_palette_activation/v1",
                "ok": True,
                "entry_id": "slash.models",
                "activation_kind": "model_list",
                "ui_action_applied": True,
                "source": source,
                "slash": slash,
                "slash_consumed": True,
                "chat_submitted": False,
                "model_request_started": False,
                "slash_suggestion_inserted": False,
                "evidence_status": "model_list_rendered",
                "policy_boundary": _safe_palette_policy_boundary(),
                "blocked_reasons": [],
                "model_count": len(models),
                **_palette_no_side_effect_flags(),
            }
            self._show_model_dialog()
            self._render_chat()
            self._render_palette_activation_status("Listed models. Select with /model <number>.", ok=True)
            self._render_current_view()

        def _show_model_dialog(self, *, query: str = "", selected_index: int = 0) -> None:
            dashboard = self._app_service.dashboard(
                selected_session_id=self._selected_session_id or self._chat_state.session_id,
            )
            self._dialog_query = query
            self._dialog_selected_index = selected_index
            self._show_dialog(
                render_model_selection_dialog(dashboard, query=query, selected_index=selected_index),
                kind="models",
            )

        def _show_functionality_dialog(self, *, query: str = "", selected_index: int = 0) -> None:
            self._dialog_query = query
            self._dialog_selected_index = selected_index
            self._show_dialog(
                render_command_menu_dialog(build_functionality_table(slash_commands), query=query, selected_index=selected_index),
                kind="commands",
            )

        def action_move_dialog_selection(self, step: int) -> None:
            if not self._dialog_visible:
                return
            row_count = self._dialog_row_count()
            if row_count <= 0:
                self._dialog_selected_index = 0
            else:
                self._dialog_selected_index = (self._dialog_selected_index + step) % row_count
            if self._dialog_kind == "models":
                self._show_model_dialog(query=self._dialog_query, selected_index=self._dialog_selected_index)
            elif self._dialog_kind == "commands":
                self._show_functionality_dialog(query=self._dialog_query, selected_index=self._dialog_selected_index)
            elif self._dialog_kind == "themes":
                self._show_theme_dialog(selected_index=self._dialog_selected_index)
            elif self._dialog_kind == "agents":
                self._show_agent_dialog(selected_index=self._dialog_selected_index)

        def action_activate_dialog_selection(self) -> None:
            if self._dialog_kind == "models":
                self._activate_selected_model_dialog_entry()
            elif self._dialog_kind == "commands":
                self._activate_selected_functionality_row()
            elif self._dialog_kind == "themes":
                self._activate_selected_theme_dialog_entry()
            elif self._dialog_kind == "agents":
                self._activate_selected_agent_dialog_entry()

        def _dialog_row_count(self) -> int:
            if self._dialog_kind == "models":
                return len(_model_selection_dialog_entries(self._dashboard_snapshot(), query=self._dialog_query))
            if self._dialog_kind == "commands":
                table = build_functionality_table(slash_commands)
                return len(filter_functionality_table(table, self._dialog_query)["rows"])
            if self._dialog_kind == "themes":
                return len(THEME_DIALOG_ENTRIES)
            if self._dialog_kind == "agents":
                return len(self._agent_dialog_entries())
            return 0

        def action_handle_text_dialog_key(self, event) -> None:
            if event.key == "escape":
                self._hide_dialog()
                self._render_palette_activation_status("Session action cancelled.", ok=False)
                return
            if event.key == "backspace":
                self._dialog_text_buffer = self._dialog_text_buffer[:-1]
                self._refresh_text_dialog()
                return
            if event.key == "ctrl+u":
                self._dialog_text_buffer = ""
                self._refresh_text_dialog()
                return
            if event.key == "enter":
                if self._dialog_kind == "session_delete":
                    self._confirm_session_hard_delete()
                elif self._dialog_kind == "session_rename":
                    self._confirm_session_rename()
                return
            if event.character and event.character.isprintable():
                self._dialog_text_buffer += event.character
                self._refresh_text_dialog()

        def _refresh_text_dialog(self) -> None:
            session = self._dialog_context.get("session") or {}
            if self._dialog_kind == "session_delete":
                self._show_dialog(render_session_delete_dialog(session, buffer=self._dialog_text_buffer), kind="session_delete")
            elif self._dialog_kind == "session_rename":
                self._show_dialog(render_session_rename_dialog(session, buffer=self._dialog_text_buffer), kind="session_rename")

        def _confirm_session_hard_delete(self) -> None:
            session = self._dialog_context.get("session") or {}
            session_id = str(session.get("id") or "")
            if self._dialog_text_buffer != "DELETE":
                self._render_palette_activation_status("Type DELETE to confirm hard delete.", ok=False)
                self._refresh_text_dialog()
                return
            if self._request_in_flight and session_id == self._chat_state.session_id:
                self._hide_dialog()
                self._render_palette_activation_status("Cannot hard delete the active in-flight session.", ok=False)
                return
            try:
                result = self._app_service.hard_delete_session(session_id)
                counts = result["counts"]
            except Exception as exc:
                self._hide_dialog()
                self._render_palette_activation_status(f"Hard delete failed: {exc}", ok=False)
                return
            self._hide_dialog()
            self._after_session_removed_or_hidden(session_id)
            self._render_palette_activation_status(
                f"Hard deleted {session_id}; runs/tasks/artifacts retained.",
                ok=True,
            )
            self._latest_palette_activation = {
                "schema_version": "harness.tui_session_action/v1",
                "ok": True,
                "entry_id": "sessions.hard_delete",
                "activation_kind": "session_state_mutation",
                "deletion_counts": counts,
                "process_started": False,
                "filesystem_modified": True,
                "active_repo_modified": False,
                "permission_granting": False,
                "authority_granting": False,
            }
            self._render_current_view()

        def _confirm_session_rename(self) -> None:
            session = self._dialog_context.get("session") or {}
            session_id = str(session.get("id") or "")
            title = self._dialog_text_buffer.strip()
            if not title:
                self._render_palette_activation_status("Session title cannot be empty.", ok=False)
                self._refresh_text_dialog()
                return
            try:
                self._app_service.update_session_title(session_id, title)
            except Exception as exc:
                self._hide_dialog()
                self._render_palette_activation_status(f"Rename failed: {exc}", ok=False)
                return
            self._hide_dialog()
            self._dashboard_snapshot(force=True)
            self._render_palette_activation_status(f"Renamed session to {title}.", ok=True)
            self._render_current_view()

        def _activate_selected_model_dialog_entry(self) -> None:
            dashboard = self._dashboard_snapshot()
            models = _model_selection_dialog_entries(dashboard, query=self._dialog_query)
            if not models:
                self._render_palette_activation_status("No model selected.", ok=False)
                return
            selected_index = min(max(self._dialog_selected_index, 0), len(models) - 1)
            raw_ref = str(models[selected_index].get("raw_model_ref") or "")
            if not raw_ref:
                self._render_palette_activation_status("Selected model has no model ref.", ok=False)
                return
            prompt = self.query_one("#prompt", TextArea)
            prompt.value = f"/model {raw_ref}"
            self._activate_model_slash_command(prompt.value)

        def _activate_selected_functionality_row(self) -> None:
            table = build_functionality_table(slash_commands)
            rows = filter_functionality_table(table, self._dialog_query)["rows"]
            if not rows:
                self._render_palette_activation_status("No command selected.", ok=False)
                return
            selected_index = min(max(self._dialog_selected_index, 0), len(rows) - 1)
            self._activate_functionality_row(rows[selected_index])

        def _activate_functionality_row(self, row: dict) -> None:
            slash = str(row.get("slash") or "")
            prompt = self.query_one("#prompt", TextArea)
            self._leader_key_active = False
            if slash == "/model":
                prompt.value = "/model "
                try:
                    prompt.cursor_position = len(prompt.value)
                except AttributeError:
                    pass
                self._show_model_dialog()
                self._render_palette_activation_status("Type a model search or use arrows, then enter.", ok=True)
                return
            if slash == "/models":
                prompt.value = ""
                self._show_models_list(source="command_table", slash="/models")
                return
            if slash == "/theme":
                prompt.value = ""
                self._show_theme_dialog()
                self._render_palette_activation_status("Select a theme with arrows, then enter.", ok=True)
                return
            if self._activate_safe_slash_command(slash):
                self._hide_dialog()
                return
            prompt.value = f"{slash} "
            try:
                prompt.cursor_position = len(prompt.value)
            except AttributeError:
                pass
            self._hide_dialog()
            self._render_palette_activation_status(f"Command ready: {slash}. Fill arguments, then submit.", ok=True)

        def _show_theme_dialog(self, *, selected_index: int | None = None) -> None:
            if selected_index is None:
                selected_index = next(
                    (index for index, entry in enumerate(THEME_DIALOG_ENTRIES) if entry["id"] == self._selected_theme_id),
                    0,
                )
            self._dialog_query = ""
            self._dialog_selected_index = selected_index
            self._show_dialog(
                render_theme_selection_dialog(
                    selected_theme=self._selected_theme_id,
                    selected_index=selected_index,
                ),
                kind="themes",
            )

        def _agent_dialog_entries(self) -> list[str]:
            dashboard = self._dashboard_snapshot()
            project_agents = [
                str(agent.get("agent_id"))
                for agent in dashboard.get("agents", [])
                if agent.get("agent_id")
            ]
            entries = ["default", "plan", "build", *project_agents]
            deduped = []
            for entry in entries:
                if entry not in deduped:
                    deduped.append(entry)
            return deduped

        def _show_agent_dialog(self, *, selected_index: int | None = None) -> None:
            row = self._selected_session_row()
            current_agent = str((row or {}).get("agent_id") or "default")
            entries = self._agent_dialog_entries()
            if selected_index is None:
                selected_index = entries.index(current_agent) if current_agent in entries else 0
            self._dialog_selected_index = selected_index
            self._show_dialog(
                render_agent_selection_dialog(entries, selected_agent=current_agent, selected_index=selected_index),
                kind="agents",
            )

        def _activate_selected_agent_dialog_entry(self) -> None:
            row = self._selected_session_row()
            if row is None:
                self._render_palette_activation_status("No session selected.", ok=False)
                return
            entries = self._agent_dialog_entries()
            if not entries:
                self._render_palette_activation_status("No agents available.", ok=False)
                return
            index = min(max(self._dialog_selected_index, 0), len(entries) - 1)
            selected_agent = entries[index]
            agent_value = None if selected_agent == "default" else selected_agent
            try:
                self._app_service.update_session_agent(str(row["id"]), agent_value, source="tui_session_pane")
            except Exception as exc:
                self._render_palette_activation_status(f"Agent selection failed: {exc}", ok=False)
                return
            self._selected_agent_id = selected_agent
            self._hide_dialog()
            self._dashboard_snapshot(force=True)
            self._render_palette_activation_status(f"Selected agent {selected_agent}.", ok=True)
            self._render_current_view()

        def _activate_selected_theme_dialog_entry(self) -> None:
            index = min(max(self._dialog_selected_index, 0), len(THEME_DIALOG_ENTRIES) - 1)
            theme_id = str(THEME_DIALOG_ENTRIES[index]["id"])
            self._apply_theme_selection(theme_id)
            self._latest_palette_activation = {
                "schema_version": "harness.tui_palette_activation/v1",
                "ok": True,
                "entry_id": f"ui_controls.theme_{theme_id}",
                "activation_kind": "ui_action",
                "action": {"type": "set_theme", "theme_id": theme_id},
                "ui_action_applied": True,
                "source": "theme_dialog",
                "slash_consumed": False,
                "chat_submitted": False,
                "model_request_started": False,
                "slash_suggestion_inserted": False,
                "evidence_status": "ui_theme_selected_in_memory",
                "policy_boundary": _safe_palette_policy_boundary(),
                "blocked_reasons": [],
                "local_state_changes": {
                    "changed_fields": ["selected_theme"],
                    "creates_message": False,
                    "starts_request": False,
                    "executes_command": False,
                    "mutates_filesystem": False,
                    "grants_permission": False,
                },
                **_palette_no_side_effect_flags(),
            }
            self._hide_dialog()
            self._render_palette_activation_status(f"Selected theme {theme_id}.", ok=True)
            self._render_current_view()

        def _show_dialog(self, content: str, *, kind: str) -> None:
            try:
                overlay = self.query_one("#dialog-overlay", Container)
                panel = self.query_one("#dialog-panel", Static)
            except NoMatches:
                return
            panel.update(content)
            overlay.remove_class("hidden")
            self._dialog_visible = True
            self._dialog_kind = kind

        def _hide_dialog(self) -> None:
            try:
                overlay = self.query_one("#dialog-overlay", Container)
                panel = self.query_one("#dialog-panel", Static)
            except NoMatches:
                self._dialog_visible = False
                self._dialog_kind = ""
                self._dialog_context = {}
                self._dialog_text_buffer = ""
                return
            panel.update("")
            overlay.add_class("hidden")
            self._dialog_visible = False
            self._dialog_kind = ""
            self._dialog_context = {}
            self._dialog_text_buffer = ""

        def _persist_model_selection(self, activation: dict, *, source: str) -> dict:
            action = activation.get("action") or {}
            raw_model_ref = str(action.get("raw_model_ref") or "").strip()
            no_side_effects = _palette_no_side_effect_flags()
            if not raw_model_ref:
                return {
                    **activation,
                    "ok": False,
                    "blocked_reasons": ["model_ref_missing"],
                    "error": "Model ref missing.",
                    **no_side_effects,
                    "harness_state_modified": False,
                    "provider_execution_started": False,
                    "model_execution_started": False,
                    "network_accessed": False,
                    "hidden_provider_fallback": False,
                    "hidden_model_fallback": False,
                    "no_hidden_fallback": True,
                    "permission_granting": False,
                    "authority_granting": False,
                }
            try:
                dashboard = self._dashboard_snapshot(force=True)
                active_session = dashboard.get("active_session") or {}
                session_id = self._selected_session_id or self._chat_state.session_id or active_session.get("id")
                if not session_id:
                    return {
                        **activation,
                        "ok": False,
                        "raw_model_ref": raw_model_ref,
                        "blocked_reasons": ["session_missing"],
                        "error": "No active session exists for model selection.",
                        **no_side_effects,
                        "harness_state_modified": False,
                        "provider_execution_started": False,
                        "model_execution_started": False,
                        "network_accessed": False,
                        "hidden_provider_fallback": False,
                        "hidden_model_fallback": False,
                        "no_hidden_fallback": True,
                        "permission_granting": False,
                        "authority_granting": False,
                    }
                result = self._app_service.update_session_model_selection(
                    str(session_id),
                    raw_model_ref,
                    source="tui_model_picker",
                )
                validation_payload = result["model_validation"]
                if not result.get("session_model_selected"):
                    self._dashboard_snapshot(force=True)
                    return {
                        **activation,
                        "ok": False,
                        "raw_model_ref": raw_model_ref,
                        "session_id": str(session_id),
                        "session_model_selected": False,
                        "model_validation": validation_payload,
                        "blocked_reasons": result.get("blocked_reasons") or [],
                        "evidence_status": "session_model_selection_blocked",
                        "harness_state_modified": True,
                        "session_event_persisted": True,
                        "source": source,
                        **no_side_effects,
                        "provider_execution_started": False,
                        "model_execution_started": False,
                        "network_accessed": False,
                        "hidden_provider_fallback": False,
                        "hidden_model_fallback": False,
                        "no_hidden_fallback": True,
                        "permission_granting": False,
                        "authority_granting": False,
                    }
                session = result["session"]
                self._dashboard_snapshot(force=True)
                return {
                    **activation,
                    "ok": True,
                    "raw_model_ref": raw_model_ref,
                    "session_id": session["id"],
                    "session_model_selected": True,
                    "model_validation": validation_payload,
                    "blocked_reasons": result.get("blocked_reasons") or [],
                    "evidence_status": "session_model_selection_persisted",
                    "harness_state_modified": True,
                    "session_event_persisted": True,
                    "source": source,
                    **no_side_effects,
                    "provider_execution_started": False,
                    "model_execution_started": False,
                    "network_accessed": False,
                    "hidden_provider_fallback": False,
                    "hidden_model_fallback": False,
                    "no_hidden_fallback": True,
                    "permission_granting": False,
                    "authority_granting": False,
                }
            except Exception as exc:
                return {
                    **activation,
                    "ok": False,
                    "raw_model_ref": raw_model_ref,
                    "blocked_reasons": ["session_model_selection_error"],
                    "error": str(exc),
                    **no_side_effects,
                    "harness_state_modified": False,
                    "provider_execution_started": False,
                    "model_execution_started": False,
                    "network_accessed": False,
                    "hidden_provider_fallback": False,
                    "hidden_model_fallback": False,
                    "no_hidden_fallback": True,
                    "permission_granting": False,
                    "authority_granting": False,
                }

        def _record_palette_activation_event(self, activation: dict, *, source: str) -> None:
            if not activation.get("ok"):
                return
            try:
                dashboard = self._dashboard_snapshot(force=True)
                active_session = dashboard.get("active_session") or {}
                session_id = self._selected_session_id or self._chat_state.session_id or active_session.get("id")
                if not session_id:
                    return
                store = SQLiteStore.open_initialized(project_root)
                payload = {
                    "source": source,
                    "entry_id": activation.get("entry_id"),
                    "activation_kind": activation.get("activation_kind"),
                    "action": activation.get("action") or {},
                    "ui_action_applied": bool(activation.get("ui_action_applied")),
                    "command_started": bool(activation.get("command_started")),
                    "provider_started": bool(activation.get("provider_started")),
                    "shell_started": bool(activation.get("shell_started")),
                    "adapter_started": bool(activation.get("adapter_started")),
                    "child_process_started": bool(activation.get("child_process_started")),
                    "process_started": bool(activation.get("process_started")),
                    "filesystem_modified": bool(activation.get("filesystem_modified")),
                    "permission_granting": bool(activation.get("permission_granting")),
                    "authority_granting": bool(activation.get("authority_granting")),
                    "session_message_created": bool(activation.get("session_message_created")),
                    "evidence_status": "ui_only_persisted",
                    "policy_boundary": activation.get("policy_boundary") or _safe_palette_policy_boundary(),
                    "blocked_reasons": activation.get("blocked_reasons") or [],
                }
                store.append_store_event(
                    "session",
                    str(session_id),
                    "tui.ui_activation.applied",
                    payload,
                    session_id=str(session_id),
                    redaction_state="redacted",
                )
                self._latest_palette_activation = {
                    **activation,
                    "session_event_persisted": True,
                    "session_id": str(session_id),
                }
            except Exception:
                self._latest_palette_activation = {
                    **activation,
                    "session_event_persisted": False,
                }

        def _activate_safe_slash_command(self, value: str) -> bool:
            request = value.strip()
            if not request.startswith("/"):
                return False
            command_name = request[1:].split(maxsplit=1)[0]
            try:
                filtered = filter_slash_commands(slash_commands, command_name)
                exact_matches = [command for command in filtered["commands"] if command["name"] == command_name]
                if len(exact_matches) != 1:
                    return False
                command = exact_matches[0]
                activation = command.get("activation") or {}
                if activation.get("kind") != "ui_action" or not activation.get("supported"):
                    return False
                prompt = self.query_one("#prompt", TextArea)
                result = activate_command_palette_entry(
                    self._palette_snapshot(),
                    str(command["entry_id"]),
                    {
                        "focus_mode": self._focus_mode,
                        "active_section_id": self._active_section_id,
                        "active_section_index": self._section_cursor_index,
                        "collapsed_section_ids": self._collapsed_section_ids,
                        "selected_theme": self._selected_theme_id,
                    },
                )
                if not result.get("ok"):
                    return False
                self._latest_palette_activation = {
                    **result,
                    "source": "slash",
                    "slash": command["slash"],
                    "slash_consumed": True,
                    "chat_submitted": False,
                    "model_request_started": False,
                    "slash_suggestion_inserted": False,
                }
                state = result.get("view_state") or {}
                self._focus_mode = str(state.get("focus_mode") or self._focus_mode)
                self._active_section_id = _right_panel_resolve_section_id(
                    state.get("active_section_id")
                    or _right_panel_section_id_at_index(state.get("active_section_index"))
                )
                self._section_cursor_index = _right_panel_section_index(self._active_section_id)
                self._collapsed_section_ids = set(normalize_right_panel_collapsed_sections(state.get("collapsed_section_ids")))
                self._selected_agent_id = str(state.get("selected_agent_id") or self._selected_agent_id)
                self._apply_theme_selection(str(state.get("selected_theme") or self._selected_theme_id))
                prompt.value = str(state.get("query", ""))
                self._record_palette_activation_event(result, source="slash")
                self._render_palette_activation_status(f"Activated {command['slash']}.", ok=True)
                self._render_current_view()
                return True
            except Exception as exc:
                self._latest_palette_activation = {
                    "schema_version": "harness.tui_palette_activation/v1",
                    "ok": False,
                    "entry_id": f"slash.{command_name}" if command_name else "slash",
                    "error": str(exc),
                    "activation_kind": "slash_error",
                    "ui_action_applied": False,
                    "source": "slash",
                    "slash": f"/{command_name}" if command_name else request,
                    "slash_consumed": True,
                    "chat_submitted": False,
                    "model_request_started": False,
                    "slash_suggestion_inserted": False,
                    "blocked_reasons": ["slash_activation_error"],
                    **_palette_no_side_effect_flags(),
                }
                self._render_palette_activation_status(f"Slash command failed safely: {exc}", ok=False)
                self._render_current_view()
                return True

        def action_activate_safe_slash_command(self) -> bool:
            prompt = self.query_one("#prompt", TextArea)
            return self._activate_safe_slash_command(prompt.value)

        def _render_palette_activation_status(self, message: str, *, ok: bool) -> None:
            status = self.query_one("#slash-status", Static)
            status.remove_class("hidden")
            prefix = "Palette" if ok else "Palette"
            status.update(f"{prefix}: {escape(message)}")

        def action_move_slash_suggestion(self, step: int) -> None:
            prompt = self.query_one("#prompt", TextArea)
            matching_commands = _matching_slash_commands(slash_commands, prompt.value)
            if not matching_commands:
                self._slash_suggestion_index = 0
                self._render_slash_suggestions(prompt.value)
                return
            self._slash_suggestion_index = (self._slash_suggestion_index + step) % len(matching_commands)
            self._render_slash_suggestions(prompt.value)

        def action_insert_selected_slash_suggestion(self) -> None:
            prompt = self.query_one("#prompt", TextArea)
            inserted = self._request_from_prompt_submission(prompt.value)
            if not inserted:
                return
            prompt.value = inserted
            try:
                prompt.cursor_position = len(inserted)
            except AttributeError:
                pass
            self._slash_suggestion_index = 0
            self._render_slash_suggestions("")

        def _request_from_prompt_submission(self, prompt_value: str) -> str:
            request = prompt_value.strip()
            matching_commands = _matching_slash_commands(slash_commands, request)
            if not matching_commands:
                return request
            command_token = request.split(maxsplit=1)[0]
            if any(command_token == str(command["slash"]) for command in slash_commands["commands"]):
                return request
            selected_index = min(max(self._slash_suggestion_index, 0), len(matching_commands) - 1)
            selected_slash = str(matching_commands[selected_index]["slash"])
            _, _, args = request.partition(" ")
            return f"{selected_slash} {args}".strip()

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
            graph = view.get("graph") or {}
            nodes = graph.get("nodes") or []
            if nodes and self._right_pane_mode in {"overview", "graph", "evidence"}:
                ordered = sorted(nodes, key=lambda item: ((item.get("row") or 0), item.get("lane_id") or "", item.get("id") or ""))
                node_ids = [str(item.get("id")) for item in ordered if item.get("id")]
                if node_ids:
                    current = self._selected_graph_node_id or graph.get("selected_node_id") or node_ids[0]
                    current_index = node_ids.index(current) if current in node_ids else 0
                    self._selected_graph_node_id = node_ids[(current_index + step) % len(node_ids)]
                    if self._right_pane_mode in {"graph", "evidence"}:
                        self._active_section_id = "node_details"
                        self._section_cursor_index = _right_panel_section_index("node_details")
                    self._render_current_view()
                    return
            if not view["sections"]:
                self._section_cursor_index = 0
                self._active_section_id = "active_work"
                return
            self._section_cursor_index = (self._section_cursor_index + step) % len(view["sections"])
            self._active_section_id = str(view["sections"][self._section_cursor_index]["id"])
            self._render_current_view()

        def _move_session_cursor(self, step: int) -> None:
            projection = self._app_service.session_pane(
                selected_session_id=self._chat_state.session_id or self._selected_session_id,
                status_filter="open",
                query="",
            )
            sessions = projection.get("sessions") or []
            if not sessions:
                self._session_cursor_index = 0
                self._selected_session_id = None
                self._render_current_view()
                return
            current_id = self._chat_state.session_id or projection.get("selected_session_id")
            current_index = next((index for index, session in enumerate(sessions) if session.get("id") == current_id), 0)
            self._session_cursor_index = (current_index + step) % len(sessions)
            self._selected_session_id = str(sessions[self._session_cursor_index].get("id") or "")
            self._left_selected_item_id = f"session:{self._selected_session_id}"
            self._chat_state.session_id = self._selected_session_id
            self._focus_mode = "dashboard"
            self._active_section_id = "sessions"
            self._section_cursor_index = _right_panel_section_index("sessions")
            self._render_palette_activation_status(
                f"Selected session: {sessions[self._session_cursor_index].get('display_title') or 'Untitled session'}.",
                ok=True,
            )
            self._render_current_view()

        def _current_view(self) -> dict:
            prompt = self.query_one("#prompt", TextArea)
            refreshed_dashboard = self._dashboard_snapshot()
            return build_right_panel_model(
                refreshed_dashboard,
                {
                    "palette": self._palette_snapshot(refreshed_dashboard),
                    "active_section_id": self._active_section_id,
                    "active_section_index": self._section_cursor_index,
                    "collapsed_section_ids": self._collapsed_section_ids,
                    "right_pane_mode": self._right_pane_mode,
                    "show_all_orchestrations": self._show_all_orchestrations,
                    "selected_orchestration_id": self._selected_orchestration_id,
                    "selected_graph_node_id": self._selected_graph_node_id,
                    "pinned_orchestration_id": self._pinned_orchestration_id,
                    "active_orchestrator": self._chat_state.selected_orchestrator_id or "coding_orchestrator",
                    "chat_mode": "live" if self._chat_state.codex_like_mode else "normal",
                    "pending_action_contract": self._chat_state.pending_action_contract.to_payload()
                    if self._chat_state.pending_action_contract
                    else None,
                    "latest_task_id": self._chat_state.latest_task_id,
                    "latest_lease_id": self._chat_state.latest_lease_id,
                    "latest_run_id": self._chat_state.latest_run_id,
                    "latest_response": self._latest_response,
                    "latest_palette_activation": self._latest_palette_activation,
                    "selected_agent_id": self._selected_agent_id,
                    "selected_theme": self._selected_theme_id,
                    "active_session_id": self._chat_state.session_id,
                    "request_in_flight": self._request_in_flight,
                },
                prompt.value,
                focus_mode=self._focus_mode,
            )

        def _palette_snapshot(self, dashboard_snapshot: dict | None = None) -> dict:
            snapshot = dashboard_snapshot or self._dashboard_snapshot()
            return build_command_palette(model_catalog=snapshot.get("model_catalog") or {})

        def _apply_theme_selection(self, theme_id: str) -> None:
            if theme_id not in {"light", "dark", "system"}:
                return
            self._selected_theme_id = theme_id
            textual_theme = {
                "light": "harness-light",
                "dark": "textual-dark",
                "system": "textual-light",
            }[theme_id]
            self.theme = textual_theme
            # Harness sets the theme after mount from UI actions; Textual's class-level
            # theme default does not refresh runtime CSS in this nested app without
            # explicitly applying the watcher.
            self._watch_theme(textual_theme)

        def _render_current_view(self) -> None:
            try:
                self._render_view(self._current_view())
            except NoMatches:
                return

        def _render_slash_suggestions(self, prompt_value: str) -> None:
            status = self.query_one("#slash-status", Static)
            matching_commands = _matching_slash_commands(slash_commands, prompt_value)
            if matching_commands and self._slash_suggestion_index >= len(matching_commands):
                self._slash_suggestion_index = len(matching_commands) - 1
            elif not matching_commands:
                self._slash_suggestion_index = 0
            rendered = render_slash_command_suggestions(
                slash_commands,
                prompt_value,
                selected_index=self._slash_suggestion_index,
            )
            if rendered:
                status.remove_class("hidden")
                status.update(rendered)
            else:
                status.update("")
                status.add_class("hidden")

        def _refresh_live_view(self) -> None:
            try:
                if self._request_in_flight:
                    self._render_chat()
                needs_full_refresh = (
                    self._request_in_flight
                    or self._dashboard_cache is None
                    or time.monotonic() - self._dashboard_cache_at >= 1.5
                )
                self._dashboard_snapshot(force=False)
                if needs_full_refresh:
                    self._render_current_view()
                else:
                    self._render_right_pane_only()
                self._live_refresh_failures = 0
            except NoMatches:
                return
            except Exception as exc:
                self._live_refresh_failures += 1
                if self._live_refresh_failures >= 3 and self._refresh_timer is not None and hasattr(self._refresh_timer, "stop"):
                    self._refresh_timer.stop()
                try:
                    self._render_palette_activation_status(
                        f"Live refresh paused: {exc.__class__.__name__}: {exc}",
                        ok=False,
                    )
                except Exception:
                    return

        def _dashboard_snapshot(self, *, force: bool = False) -> dict:
            now = time.monotonic()
            selected_session_id = self._selected_session_id or self._chat_state.session_id
            if (
                force
                or self._dashboard_cache is None
                or selected_session_id != self._dashboard_cache_session_id
                or now - self._dashboard_cache_at >= 1.5
            ):
                self._dashboard_cache = self._app_service.dashboard(selected_session_id=selected_session_id)
                self._dashboard_cache_session_id = selected_session_id
                self._dashboard_cache_at = now
            return self._dashboard_cache

        def _clamp_section_cursor(self, view: dict) -> None:
            if not view["sections"]:
                self._section_cursor_index = 0
                self._active_section_id = "active_work"
            elif self._section_cursor_index >= len(view["sections"]):
                self._section_cursor_index = len(view["sections"]) - 1
                self._active_section_id = str(view["sections"][self._section_cursor_index]["id"])
            elif view.get("active_section_id"):
                self._active_section_id = str(view["active_section_id"])
                self._section_cursor_index = int(view.get("active_section_index") or self._section_cursor_index)

        def _render_session_pane(self, dashboard_snapshot: dict | None = None, right_view: dict | None = None) -> None:
            try:
                header = self.query_one("#session-pane-header", Static)
                session_list = self.query_one("#session-list", ListView)
                detail = self.query_one("#session-pane-detail", Static)
                actions = self.query_one("#session-pane-actions", Static)
            except NoMatches:
                return
            projection = self._left_pane_projection(dashboard_snapshot=dashboard_snapshot, right_view=right_view)
            items = left_pane_visible_items(projection)
            items = [
                ListItem(Label(label))
                for label in left_pane_list_item_labels(projection, width=32)
            ]
            session_list.clear()
            if items:
                session_list.extend(items)
            selected_index = next(
                (
                    index
                    for index, item in enumerate(left_pane_visible_items(projection))
                    if item.get("nav_id") == projection.get("selected_item_id")
                ),
                None,
            )
            try:
                session_list.index = selected_index
            except Exception:
                pass
            header.update(render_left_pane_header(projection, width=32, focused=self._left_pane_focused))
            detail.update(render_left_pane_detail(projection, width=32))
            if self._session_search_active:
                actions.update(_render_session_pane_actions(search_active=True, query=self._session_query))
            else:
                actions.update(render_left_pane_footer(projection, width=32))

        def _render_chat(self) -> None:
            working_seconds = None
            if self._request_in_flight and self._request_started_at is not None:
                working_seconds = max(0, int(time.monotonic() - self._request_started_at))
            chat_width = max(60, min(160, self.query_one("#chat", VerticalScroll).size.width - 4))
            transcript = render_codex_like_transcript(
                self._messages,
                working_seconds=working_seconds,
                separator_width=chat_width,
            )
            _update_markup_static(self.query_one("#chat-content", Static), transcript)
            self.call_after_refresh(lambda: self.query_one("#chat", VerticalScroll).scroll_end(animate=False))

        def _render_view(self, view: dict) -> None:
            self._clamp_section_cursor(view)
            self._selected_orchestration_id = view.get("selected_orchestration_id") or self._selected_orchestration_id
            self._selected_graph_node_id = view.get("selected_node_id") or self._selected_graph_node_id
            self._pinned_orchestration_id = view.get("pinned_orchestration_id") or self._pinned_orchestration_id
            self._show_all_orchestrations = bool(view.get("show_all_orchestrations", self._show_all_orchestrations))
            self.query_one("#search-status", Static).update(render_right_panel_status(view))
            self.query_one("#palette-status", Static).update(_render_navigation_hints(view))
            refreshed_dashboard = self._dashboard_snapshot()
            self._render_session_pane(refreshed_dashboard, right_view=view)
            self.query_one("#composer-status", Static).update(
                _render_composer_status(refreshed_dashboard, self._selected_agent_id, self._chat_state.session_id)
            )
            container = self.query_one("#pane-container", Static)
            container.update(render_right_panel(view))

        def _render_right_pane_only(self) -> None:
            view = self._current_view()
            self._clamp_section_cursor(view)
            self._selected_orchestration_id = view.get("selected_orchestration_id") or self._selected_orchestration_id
            self._selected_graph_node_id = view.get("selected_node_id") or self._selected_graph_node_id
            self._pinned_orchestration_id = view.get("pinned_orchestration_id") or self._pinned_orchestration_id
            self._show_all_orchestrations = bool(view.get("show_all_orchestrations", self._show_all_orchestrations))
            self.query_one("#search-status", Static).update(render_right_panel_status(view))
            self.query_one("#palette-status", Static).update(_render_navigation_hints(view))
            self.query_one("#pane-container", Static).update(render_right_panel(view))

    return HarnessUnifiedApp()


def run_harness_app(project_root: Path, *, codex_like: bool = False) -> None:
    create_harness_app(project_root, codex_like=codex_like).run()


def run_read_only_tui(project_root: Path) -> None:
    run_harness_app(project_root)


def _chat_response_to_tui_message(response: dict, *, debug: bool = False) -> dict:
    lines = list(response.get("lines", []))
    if response.get("kind") == "self_managed_local_action":
        return {
            "role": "assistant",
            "title": response.get("title") or "Done",
            "lines": lines,
        }
    if response.get("tool_results"):
        lines.append("Tool calls:")
        for item in response["tool_results"]:
            status = "ok" if item.get("ok") else item.get("error_type") or "failed"
            lines.append(f"- {item.get('tool')}: {status}")
    approval_card = response.get("approval_card") if isinstance(response.get("approval_card"), dict) else None
    if approval_card and response.get("kind") == "session_tool_permission_required":
        if not any(str(line).startswith("approval:") for line in lines):
            lines.append(f"approval: {approval_card.get('approval_id') or approval_card.get('permission_id')}")
        if approval_card.get("command") and not any(str(line).startswith("command:") for line in lines):
            lines.append(f"command: {approval_card['command']}")
        if approval_card.get("cwd") and not any(str(line).startswith("cwd:") for line in lines):
            lines.append(f"cwd: {approval_card['cwd']}")
    if response.get("contract"):
        contract = response["contract"]
        lines.extend(
            [
                "Action contract:",
                f"- Tool: {contract.get('tool')}",
                f"- Risk: {contract.get('risk')}",
                f"- Confirmations: {', '.join(contract.get('required_confirmations') or []) or 'none'}",
            ]
        )
    if response.get("context_manifest"):
        blocks = response["context_manifest"].get("blocks") or []
        if blocks:
            lines.append("Context:")
            lines.append("- " + ", ".join(str(block.get("kind")) for block in blocks[:6]))
    if debug:
        debug_payload = response.get("debug") if isinstance(response.get("debug"), dict) else {}
        if response.get("event_sequence"):
            lines.append("Debug:")
            lines.append("- events: " + ", ".join(str(kind) for kind in response.get("event_sequence", [])[:12]))
        if debug_payload.get("exception_type"):
            lines.append(f"- exception: {debug_payload.get('exception_type')}")
        if debug_payload.get("traceback"):
            lines.append("- traceback captured")
    return {
        "role": "assistant",
        "title": response.get("title") or response.get("kind") or "Harness",
        "lines": lines,
    }


def _runtime_prompt_response_to_tui_response(response: dict) -> dict:
    runtime = response.get("runtime") if isinstance(response.get("runtime"), dict) else {}
    runtime_state = runtime.get("runtime") if isinstance(runtime.get("runtime"), dict) else {}
    if not response.get("ok") or not response.get("accepted"):
        lines = [
            str(response.get("error") or runtime.get("reason") or "Runtime rejected the prompt."),
            str(response.get("guidance") or "Create a new session or fork the current one before retrying."),
        ]
        return {
            "ok": False,
            "kind": "runtime_prompt_rejected",
            "title": "Prompt Rejected",
            "lines": [line for line in lines if line.strip()],
            "runtime": runtime,
        }
    phase = runtime_state.get("phase") or runtime.get("phase") or "queued"
    prompt_id = response.get("prompt_id") or runtime.get("prompt_id")
    lines = [
        f"Session: {response.get('session_id')}",
        f"Prompt: {prompt_id or 'accepted'}",
        f"Runtime: {phase}",
    ]
    if runtime.get("worker_started") or response.get("execution_started"):
        lines.append("Worker: started")
    else:
        lines.append("Worker: queued")
    lines.append("Transcript will refresh from persisted session messages and events.")
    return {
        "ok": True,
        "kind": "runtime_prompt_submitted",
        "title": "Prompt Submitted",
        "lines": lines,
        "runtime": runtime,
    }
