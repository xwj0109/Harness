from __future__ import annotations

import sqlite3
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

from harness.approvals import ApprovalStore
from harness.config import HARNESS_DIR, write_default_config
from harness.execution import execute_lease, list_execution_adapter_descriptors
from harness.memory.sqlite_store import SQLiteStore
from harness.models import ArtifactRecord, RunRecord, TaskLease, TaskRecord
from harness.operator_context import build_operator_context, render_operator_context_lines
from harness.paths import resolve_project_root
from harness.registry import builtin_spec_registry
from harness.security import sanitize_for_logging


CHAT_SCHEMA_VERSION = "harness.chat/v1"
CHAT_RESPONSE_SCHEMA_VERSION = "harness.chat_response/v1"
CHAT_INTENT_SCHEMA_VERSION = "harness.chat_intent/v1"
ORCHESTRATION_DRAFT_SCHEMA_VERSION = "harness.chat_orchestration_draft/v1"

CODEX_ORCHESTRATION_ADAPTER = "codex_isolated_edit"
CODEX_ORCHESTRATION_TASK_TYPE = "codex_code_edit"
DEFAULT_ORCHESTRATOR_ID = "coding_orchestrator"
ORCHESTRATION_OWNER = "chat_orchestrator"


@dataclass
class OrchestratedTaskDraft:
    title: str
    description: str
    agent_id: str
    workbench_id: str
    depends_on_indexes: list[int] = field(default_factory=list)
    priority: int = 0

    def to_payload(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "description": self.description,
            "agent_id": self.agent_id,
            "workbench_id": self.workbench_id,
            "depends_on_indexes": self.depends_on_indexes,
            "priority": self.priority,
            "execution_adapter": CODEX_ORCHESTRATION_ADAPTER,
            "task_type": CODEX_ORCHESTRATION_TASK_TYPE,
        }


@dataclass
class OrchestratedRunDraft:
    objective_title: str
    objective_description: str
    orchestrator_id: str
    workbench_id: str
    tasks: list[OrchestratedTaskDraft]
    required_approvals: list[str] = field(default_factory=lambda: ["hosted_provider_codex"])
    safety_notes: list[str] = field(default_factory=list)
    equivalent_commands: list[str] = field(default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": ORCHESTRATION_DRAFT_SCHEMA_VERSION,
            "objective_title": self.objective_title,
            "objective_description": self.objective_description,
            "orchestrator_id": self.orchestrator_id,
            "workbench_id": self.workbench_id,
            "tasks": [task.to_payload() for task in self.tasks],
            "required_approvals": self.required_approvals,
            "safety_notes": self.safety_notes,
            "equivalent_commands": self.equivalent_commands,
        }


@dataclass
class ChatDraftTask:
    title: str
    description: str
    execution_adapter: str
    task_type: str
    agent_id: str | None = None
    workbench_id: str | None = None
    required_approvals: list[str] = field(default_factory=list)
    safety_notes: list[str] = field(default_factory=list)
    equivalent_command: str = ""
    mutates_when_confirmed: bool = True

    def metadata(self) -> dict[str, str]:
        return {"execution_adapter": self.execution_adapter, "task_type": self.task_type}

    def to_payload(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "description": self.description,
            "execution_adapter": self.execution_adapter,
            "task_type": self.task_type,
            "agent_id": self.agent_id,
            "workbench_id": self.workbench_id,
            "required_approvals": self.required_approvals,
            "safety_notes": self.safety_notes,
            "equivalent_command": self.equivalent_command,
            "mutates_when_confirmed": self.mutates_when_confirmed,
        }


@dataclass
class ChatSessionState:
    latest_task_id: str | None = None
    latest_lease_id: str | None = None
    latest_run_id: str | None = None
    latest_diff_artifact: str | None = None
    latest_failed_task_id: str | None = None
    pending_draft: ChatDraftTask | None = None
    pending_orchestration: OrchestratedRunDraft | None = None
    pending_execute_lease_id: str | None = None
    pending_hosted_approval: bool = False
    selected_orchestrator_id: str | None = None
    latest_objective_id: str | None = None
    latest_orchestration: dict[str, Any] | None = None
    stop_requested: bool = False
    codex_like_mode: bool = False
    transcript: list[dict[str, Any]] = field(default_factory=list)
    progress: list[str] = field(default_factory=list)

    def reset(self) -> None:
        self.latest_task_id = None
        self.latest_lease_id = None
        self.latest_run_id = None
        self.latest_diff_artifact = None
        self.latest_failed_task_id = None
        self.pending_draft = None
        self.pending_orchestration = None
        self.pending_execute_lease_id = None
        self.pending_hosted_approval = False
        self.selected_orchestrator_id = None
        self.latest_objective_id = None
        self.latest_orchestration = None
        self.stop_requested = False
        self.codex_like_mode = False
        self.transcript = []
        self.progress = []


def chat_context(project_root: Path) -> dict[str, Any]:
    project_root = resolve_project_root(project_root)
    context = build_operator_context(project_root)
    summary = dict(context["summary"])
    summary.setdefault("runs_total", summary.get("recent_runs", 0))
    return {
        "schema_version": CHAT_SCHEMA_VERSION,
        "ok": True,
        "project_root": str(project_root),
        "initialized": context["initialized"],
        "branch": context.get("branch"),
        "summary": summary,
        "registered_adapters": context["registered_adapters"],
        "dashboard": context,
        "safety_boundaries": [
            "chat_is_operator_surface_not_authority",
            "orchestration_is_explicit_task_graph",
            "no_backend_preflight",
            "no_hidden_execution",
            "no_generic_shell",
            "no_persistent_history",
        ],
    }


def handle_chat_input(
    text: str,
    project_root: Path,
    state: ChatSessionState | None = None,
) -> dict[str, Any]:
    state = state or ChatSessionState()
    project_root = resolve_project_root(project_root)
    raw = text.strip()
    response = _dispatch_chat_input(raw, project_root, state)
    if response.get("kind") != "reset":
        state.transcript.append({"role": "user", "content": str(sanitize_for_logging(raw))})
        state.transcript.append(
            {
                "role": "assistant",
                "kind": response.get("kind"),
                "lines": response.get("lines", []),
            }
        )
    return response


def _dispatch_chat_input(raw: str, project_root: Path, state: ChatSessionState) -> dict[str, Any]:
    if not raw:
        return _response("empty", "No input", ["Type /help for available commands."])
    if raw in {"/quit", "quit", "exit"}:
        return _response("quit", "Goodbye", ["Exiting harness chat."], ok=True)
    if raw in {"/confirm", "yes", "y"}:
        return _confirm_pending(project_root, state)
    if raw in {"/decline", "no", "n", "cancel"}:
        state.pending_draft = None
        state.pending_orchestration = None
        state.pending_execute_lease_id = None
        state.pending_hosted_approval = False
        return _response("declined", "Declined", ["No action was taken."], ok=True)
    if raw.startswith("/"):
        return _handle_slash(raw, project_root, state)
    return _handle_intent(raw, project_root, state)


def render_chat_response(response: dict[str, Any]) -> str:
    title = response.get("title") or response.get("kind") or "response"
    lines = [f"Harness: {title}"]
    for line in response.get("lines", []):
        lines.append(str(line))
    return "\n".join(lines)


def run_chat_loop(project_root: Path, stdin: TextIO, stdout: TextIO, *, codex_like: bool = False) -> int:
    project_root = resolve_project_root(project_root)
    state = ChatSessionState(codex_like_mode=codex_like)
    state.selected_orchestrator_id = _default_orchestrator_id()
    context = chat_context(project_root)
    stdout.write("Harness chat\n")
    stdout.write(f"Project: {context['project_root']}\n")
    stdout.write(f"Orchestrator: {state.selected_orchestrator_id or 'none'}\n")
    stdout.write(f"Mode: {'codex-like' if state.codex_like_mode else 'normal'}\n")
    stdout.write(f"Initialized: {context['initialized']}\n")
    stdout.write("Type /help for commands. Type /quit to exit.\n")
    while True:
        prompt = "harness"
        if state.codex_like_mode:
            prompt += "[codex-like]"
        if state.latest_task_id:
            prompt += f" task={state.latest_task_id}"
        stdout.write(f"{prompt}> ")
        stdout.flush()
        line = stdin.readline()
        if line == "":
            stdout.write("\n")
            return 0
        response = handle_chat_input(line, project_root, state)
        stdout.write(render_chat_response(response) + "\n")
        if response.get("kind") == "quit":
            return 0


def route_chat_intent(text: str) -> dict[str, Any]:
    normalized = _normalize(text)
    if normalized in {"initialize", "initialize project", "initialize this project", "init project"}:
        intent = "init_project"
    elif normalized in {"codex-like mode", "codex like mode", "testing mode"}:
        intent = "mode_codex_like"
    elif normalized in {"normal mode", "draft mode"}:
        intent = "mode_normal"
    elif normalized in {"show tasks", "tasks", "list tasks"}:
        intent = "show_tasks"
    elif normalized in {"show latest run", "latest run", "show runs", "runs"}:
        intent = "show_latest_run"
    elif "run" in normalized and ("adapter" in normalized or "registered" in normalized):
        intent = "execute_adapter"
    elif "adapter" in normalized:
        intent = "show_adapters"
    elif "blocked" in normalized:
        intent = "show_blocked"
    elif (
        normalized in {"what should i do next", "what next", "next steps", "what should i do"}
        or ("what" in normalized and "next" in normalized)
    ):
        intent = "recommend_next"
    elif "current project state" in normalized or normalized in {"status", "home"}:
        intent = "show_status"
    elif "dry run" in normalized:
        intent = "draft_dry_run"
    elif "read only summary" in normalized or "summary" in normalized or "summarize" in normalized or "inspect this repo" in normalized:
        intent = "draft_read_only_summary"
    elif (
        "orchestrate" in normalized
        or "multi agent" in normalized
        or "fix" in normalized
        or "bug" in normalized
        or "failing test" in normalized
        or "implement" in normalized
        or "build" in normalized
    ):
        intent = "draft_orchestration"
    elif "codex" in normalized or "isolated edit" in normalized:
        intent = "draft_codex"
    elif "lease" in normalized and "next" in normalized:
        intent = "lease_next"
    elif "inspect" in normalized and "lease" in normalized:
        intent = "inspect_lease"
    elif normalized in {"that diff", "show diff", "show that diff"}:
        intent = "show_diff"
    elif "apply-back" in normalized or "apply back" in normalized:
        intent = "apply_back_review"
    else:
        intent = "unsupported"
    return {"schema_version": CHAT_INTENT_SCHEMA_VERSION, "ok": True, "input": text, "intent": intent}


def _handle_slash(raw: str, project_root: Path, state: ChatSessionState) -> dict[str, Any]:
    parts = raw.split()
    command = parts[0][1:]
    arg = parts[1] if len(parts) > 1 else None
    if command == "help":
        return _response(
            "help",
            "Commands",
            [
                "/home, /status - show project state",
                "/init - initialize this project for Harness records",
                "/mode [normal|codex-like] - show or change chat action mode",
                "/dashboard - show passive dashboard context",
                "/orchestrators - list built-in orchestrators",
                "/use <orchestrator_id> - select an orchestrator for this chat session",
                "/agents - list built-in agents for the active workbench",
                "/tasks - list latest tasks",
                "/runs - list latest runs",
                "/leases - list active leases",
                "/adapters - list registered adapters",
                "/task <id> - show task details",
                "/run <id> - show run manifest summary",
                "/artifact <id> - show artifact metadata",
                "/lease [id] - inspect a lease",
                "/execute [lease_id] - prepare registered adapter dispatch",
                "/plan - show pending/latest orchestration plan",
                "/run - run pending orchestration or prepare latest lease dispatch",
                "/stop - stop the foreground orchestration loop",
                "/apply-back [deny|approve|keep] - review inspected Codex diff artifacts",
                "/confirm - confirm pending draft or execution",
                "/reset - clear in-memory chat references",
                "/quit - exit",
            ],
        )
    if command == "init":
        return _init_response(project_root, state)
    if command == "mode":
        return _mode_response(arg, state)
    if command in {"home", "status", "dashboard"}:
        return _status_response(project_root, state)
    if command == "orchestrators":
        return _orchestrators_response(state)
    if command == "use":
        return _use_orchestrator_response(arg, state)
    if command == "agents":
        return _agents_response(state)
    if command == "tasks":
        return _tasks_response(project_root)
    if command == "runs":
        return _runs_response(project_root, state)
    if command == "leases":
        return _leases_response(project_root, state)
    if command == "adapters":
        return _adapters_response(project_root)
    if command == "task":
        return _task_detail_response(project_root, _resolve_task_ref(arg, state))
    if command == "run":
        if arg:
            return _run_detail_response(project_root, _resolve_run_ref(arg, state))
        if state.pending_orchestration is not None:
            return _confirm_pending(project_root, state)
        if state.latest_objective_id is not None:
            return _run_orchestration_loop(project_root, state, state.latest_objective_id)
        if state.latest_lease_id is not None and state.codex_like_mode:
            return _execute_response(project_root, state.latest_lease_id, state)
        return _prepare_execute_response(project_root, state.latest_lease_id, state)
    if command == "artifact":
        return _artifact_response(project_root, arg or state.latest_diff_artifact)
    if command == "lease":
        return _inspect_lease_response(project_root, arg or state.latest_lease_id, state)
    if command == "execute":
        return _prepare_execute_response(project_root, arg or state.latest_lease_id, state)
    if command == "plan":
        return _plan_response(state)
    if command == "stop":
        state.stop_requested = True
        return _response("stop_requested", "Stop Requested", ["Foreground orchestration will stop at the next boundary."], ok=True)
    if command == "apply-back":
        choice = arg if arg in {"approve", "deny", "keep"} else None
        return _apply_back_review_response(project_root, state, choice=choice)
    if command == "progress":
        return _response("progress", "Progress", state.progress or ["No progress events in this chat session."])
    if command == "reset":
        state.reset()
        return _response("reset", "Session Reset", ["Session-local references were cleared."], ok=True)
    return _response("unknown", "Unknown Command", [f"No chat command matched {raw}.", "Type /help."])


def _handle_intent(raw: str, project_root: Path, state: ChatSessionState) -> dict[str, Any]:
    intent = route_chat_intent(raw)["intent"]
    if intent == "init_project":
        return _init_response(project_root, state)
    if intent == "mode_codex_like":
        return _mode_response("codex-like", state)
    if intent == "mode_normal":
        return _mode_response("normal", state)
    if intent == "show_tasks":
        return _tasks_response(project_root)
    if intent == "show_latest_run":
        return _runs_response(project_root, state)
    if intent == "show_adapters":
        return _adapters_response(project_root)
    if intent == "show_blocked":
        return _blocked_response(project_root)
    if intent == "recommend_next":
        return _recommend_next_response(project_root, state)
    if intent == "show_status":
        return _status_response(project_root, state)
    if intent == "draft_orchestration":
        return _orchestration_draft_response(project_root, state, _draft_orchestration(project_root, state, raw))
    if intent == "draft_dry_run":
        return _draft_response(project_root, state, _draft_dry_run(project_root))
    if intent == "draft_read_only_summary":
        return _draft_response(project_root, state, _draft_read_only(project_root, raw))
    if intent == "draft_codex":
        return _draft_response(project_root, state, _draft_codex(project_root, raw))
    if intent == "lease_next":
        return _lease_next_response(project_root, state)
    if intent == "inspect_lease":
        return _inspect_lease_response(project_root, state.latest_lease_id, state)
    if intent == "execute_adapter":
        return _prepare_execute_response(project_root, state.latest_lease_id, state)
    if intent == "show_diff":
        return _artifact_response(project_root, state.latest_diff_artifact)
    if intent == "apply_back_review":
        return _apply_back_review_response(project_root, state)
    return _deterministic_chat_guidance(raw, project_root, state)


def _deterministic_chat_guidance(raw: str, project_root: Path, state: ChatSessionState) -> dict[str, Any]:
    lines = [
        "I can inspect local Harness state and prepare explicit actions.",
        "I do not call Codex, Docker, shell, providers, or model backends directly from chat.",
        "Try: 'summarize this repo', 'fix this bug', 'show adapters', 'what should I do next?', or /help.",
    ]
    if state.pending_draft or state.pending_orchestration or state.pending_execute_lease_id or state.pending_hosted_approval:
        lines.append("There is a pending action. Type yes or /confirm to continue, or no to cancel.")
    if not _is_initialized(project_root):
        lines.append("This project is not initialized. Type /init to create local Harness records.")
    return _response(
        "deterministic_guidance",
        "Harness Guidance",
        lines,
        ok=True,
        extra={
            "input": raw,
            "mode": "codex-like" if state.codex_like_mode else "normal",
            "equivalent_commands": ["/help", "/home", "/adapters"],
        },
    )


def _init_response(project_root: Path, state: ChatSessionState) -> dict[str, Any]:
    project_root = resolve_project_root(project_root)
    project_root.mkdir(parents=True, exist_ok=True)
    already_initialized = _is_initialized(project_root)
    config_path = write_default_config(project_root)
    store = SQLiteStore(project_root)
    store.initialize()
    _update_chat_gitignore(project_root)
    state.progress.append("project initialized")
    context = build_operator_context(project_root)
    lines = [
        f"Project: {project_root}",
        f"Initialized: {'already initialized' if already_initialized else 'created'}",
        f"Config: {config_path}",
        f"Task count: {context['summary'].get('tasks_total', 0)}",
        f"Active leases: {context['summary'].get('active_leases', 0)}",
        "Next: say 'summarize this repo', 'show adapters', or 'fix this bug'.",
    ]
    return _response(
        "project_initialized",
        "Project Initialized",
        lines,
        ok=True,
        extra={"context": context, "already_initialized": already_initialized},
    )


def _mode_response(mode: str | None, state: ChatSessionState) -> dict[str, Any]:
    if mode is None:
        return _response(
            "mode",
            "Mode",
            [
                f"Current mode: {'codex-like' if state.codex_like_mode else 'normal'}",
                "normal: drafts tasks before confirmation and leaves manual lease/run steps explicit.",
                "codex-like: one confirmation creates the task or graph and drives foreground dispatch.",
            ],
            ok=True,
        )
    normalized = _normalize(mode).replace("_", "-")
    if normalized in {"codex-like", "codex", "testing"}:
        state.codex_like_mode = True
    elif normalized in {"normal", "draft", "draft-mode"}:
        state.codex_like_mode = False
    else:
        return _response("mode_unknown", "Unknown Mode", ["Use /mode normal or /mode codex-like."], ok=False)
    return _response(
        "mode_changed",
        "Mode Changed",
        [
            f"Current mode: {'codex-like' if state.codex_like_mode else 'normal'}",
            "Execution still goes through explicit Harness tasks, leases, registered adapters, runs, and artifacts.",
        ],
        ok=True,
        extra={"codex_like_mode": state.codex_like_mode},
    )


def _confirm_pending(project_root: Path, state: ChatSessionState) -> dict[str, Any]:
    if state.pending_hosted_approval:
        if not _is_initialized(project_root):
            return _uninitialized_response(project_root)
        ApprovalStore(project_root).add(
            backend="codex_cli",
            data_boundary="hosted_provider",
            task_types=["codex_code_edit", "read_only_repo_summary"],
            duration_days=1,
            reason="Created from explicit harness chat confirmation.",
        )
        state.pending_hosted_approval = False
        lines = [
            "Created a one-day Codex hosted-boundary approval profile.",
            "It permits scoped Codex execution for read-only summaries and isolated edits.",
            "It is not apply-back approval.",
        ]
        if state.latest_lease_id:
            lines.append("Type /run to continue the latest active lease.")
        return _response("hosted_approval_created", "Hosted Approval Created", lines, ok=True)
    if state.pending_draft is not None:
        if not _is_initialized(project_root):
            return _uninitialized_response(project_root)
        draft = state.pending_draft
        state.pending_draft = None
        task = _create_task_from_draft(project_root, state, draft)
        if state.codex_like_mode:
            return _run_single_task_response(project_root, state, task)
        return _task_created_response(task, draft)
    if state.pending_orchestration is not None:
        if not _is_initialized(project_root):
            return _uninitialized_response(project_root)
        draft = state.pending_orchestration
        state.pending_orchestration = None
        return _create_and_run_orchestration(project_root, state, draft)
    if state.pending_execute_lease_id is not None:
        lease_id = state.pending_execute_lease_id
        state.pending_execute_lease_id = None
        return _execute_response(project_root, lease_id, state)
    return _response("nothing_to_confirm", "Nothing Pending", ["There is no pending chat action to confirm."], ok=False)


def _create_task_from_draft(project_root: Path, state: ChatSessionState, draft: ChatDraftTask) -> TaskRecord:
    store = _require_store(project_root)
    task = store.create_task(
        title=draft.title,
        description=draft.description,
        priority=1000 if state.codex_like_mode else 0,
        workbench_id=draft.workbench_id,
        agent_id=draft.agent_id,
        required_approvals=draft.required_approvals,
        metadata=draft.metadata(),
    )
    state.latest_task_id = task.id
    state.progress.append(f"task created: {task.id}")
    return task


def _task_created_response(task: TaskRecord, draft: ChatDraftTask) -> dict[str, Any]:
    return _response(
        "task_created",
        "Task Created",
        [
            f"Task: {task.id}",
            f"Adapter: {draft.execution_adapter}",
            f"Task type: {draft.task_type}",
            "Next: say 'lease the next task' or use /tasks.",
        ],
        ok=True,
        extra={"task": task.model_dump(mode="json")},
    )


def _run_single_task_response(project_root: Path, state: ChatSessionState, task: TaskRecord) -> dict[str, Any]:
    store = _require_store(project_root)
    tick = store.daemon_run_once(owner=ORCHESTRATION_OWNER, pid=None)
    lines = [f"Task created: {task.id}"]
    if tick.lease is None:
        lines.extend([f"Lease decision: {tick.decision}", f"Pause reasons: {tick.pause_reasons}"])
        return _response(
            "codex_like_task_blocked",
            "Task Created, Not Leased",
            lines,
            ok=False,
            extra={"task": task.model_dump(mode="json"), "tick": tick.model_dump(mode="json")},
        )
    if tick.selected_task is None or tick.selected_task.id != task.id:
        state.latest_lease_id = tick.lease.id
        selected = tick.selected_task.id if tick.selected_task else "unknown"
        return _response(
            "codex_like_task_blocked",
            "Different Task Leased",
            [
                f"Task created: {task.id}",
                f"Lease acquired: {tick.lease.id}",
                f"Selected task: {selected}",
                "Stopping before dispatch so this chat request does not execute unrelated queued work.",
            ],
            ok=False,
            extra={"task": task.model_dump(mode="json"), "tick": tick.model_dump(mode="json")},
        )
    state.latest_lease_id = tick.lease.id
    if tick.selected_task:
        state.latest_task_id = tick.selected_task.id
    state.progress.append(f"lease acquired: {tick.lease.id}")
    lines.extend([f"Lease acquired: {tick.lease.id}", "Dispatching registered adapter."])
    result_response = _execute_response(project_root, tick.lease.id, state)
    result_lines = result_response.get("lines", [])
    return _response(
        "codex_like_task_result",
        "Foreground Task Result",
        [*lines, *result_lines],
        ok=bool(result_response.get("ok")),
        extra={
            "task": task.model_dump(mode="json"),
            "tick": tick.model_dump(mode="json"),
            "execution": result_response.get("result"),
        },
    )


def _draft_response(project_root: Path, state: ChatSessionState, draft: ChatDraftTask) -> dict[str, Any]:
    state.pending_draft = draft
    confirmation_line = (
        "Type yes or /confirm to create this task and run it in the foreground. Type no to cancel."
        if state.codex_like_mode
        else "Type yes or /confirm to create this task. Type no to cancel."
    )
    return _response(
        "task_draft",
        "Task Draft",
        [
            f"Title: {draft.title}",
            f"Adapter: {draft.execution_adapter}",
            f"Task type: {draft.task_type}",
            f"Mutates when confirmed: {draft.mutates_when_confirmed}",
            "Safety:",
            *[f"- {note}" for note in draft.safety_notes],
            "Equivalent command:",
            draft.equivalent_command,
            f"Mode: {'codex-like' if state.codex_like_mode else 'normal'}",
            confirmation_line,
        ],
        ok=True,
        extra={"draft": draft.to_payload()},
    )


def _orchestration_draft_response(
    project_root: Path,
    state: ChatSessionState,
    draft: OrchestratedRunDraft,
) -> dict[str, Any]:
    state.pending_orchestration = draft
    task_lines = [
        f"{idx + 1}. {task.agent_id}: {task.title}"
        + (f" depends_on={','.join(str(i + 1) for i in task.depends_on_indexes)}" if task.depends_on_indexes else "")
        for idx, task in enumerate(draft.tasks)
    ]
    return _response(
        "orchestration_draft",
        "Orchestration Draft",
        [
            f"Objective: {draft.objective_title}",
            f"Orchestrator: {draft.orchestrator_id}",
            f"Workbench: {draft.workbench_id}",
            "Tasks:",
            *task_lines,
            "Execution: codex_isolated_edit / codex_code_edit for every task.",
            f"Required approvals: {draft.required_approvals}",
            "Safety:",
            *[f"- {note}" for note in draft.safety_notes],
            "Type yes, /confirm, or /run to create the objective and run this graph in the foreground.",
            "Type no to cancel.",
        ],
        ok=True,
        extra={"draft": draft.to_payload()},
    )


def _draft_orchestration(project_root: Path, state: ChatSessionState, prompt: str) -> OrchestratedRunDraft:
    orchestrator_id = _active_orchestrator_id(state)
    workbench_id = _workbench_for_orchestrator(orchestrator_id)
    tasks = _orchestration_tasks_for(workbench_id, prompt)
    objective_title = _objective_title_for(prompt, orchestrator_id)
    equivalent = [
        f'harness objectives add --title "{objective_title}" --workbench {workbench_id} --project {project_root} --output json',
        "harness tasks add ... --execution-adapter codex_isolated_edit --task-type codex_code_edit",
        "harness daemon run-once --project . --output json",
        "harness daemon execute <lease_id> --project . --output json",
    ]
    return OrchestratedRunDraft(
        objective_title=objective_title,
        objective_description=f"Orchestrated chat request: {sanitize_for_logging(prompt)}",
        orchestrator_id=orchestrator_id,
        workbench_id=workbench_id,
        tasks=tasks,
        safety_notes=[
            "The chat UI creates explicit objective/task records before execution.",
            "One run approval drives only this foreground task graph through daemon run-once and daemon execute.",
            "Every task uses the registered codex_isolated_edit adapter.",
            "Hosted-boundary approval is not apply-back approval.",
            "Apply-back remains denied by default unless the inspected-diff approval path approves it.",
        ],
        equivalent_commands=equivalent,
    )


def _draft_dry_run(project_root: Path) -> ChatDraftTask:
    return ChatDraftTask(
        title="Chat dry-run task",
        description="Created from harness chat dry-run request.",
        execution_adapter="dry_run",
        task_type="phase_1a_test",
        safety_notes=[
            "Dry-run writes local harness evidence only.",
            "It does not call models, Codex, Docker, shell, network, hosted providers, or paid providers.",
        ],
        equivalent_command=f'harness tasks add --title "Chat dry-run task" --execution-adapter dry_run --task-type phase_1a_test --project {project_root} --output json',
    )


def _draft_read_only(project_root: Path, prompt: str) -> ChatDraftTask:
    return ChatDraftTask(
        title="Chat read-only summary",
        description=f"Read-only summary requested from chat: {sanitize_for_logging(prompt)}",
        execution_adapter="read_only_summary",
        task_type="read_only_repo_summary",
        safety_notes=[
            "Read-only summary uses Codex CLI subscription via ChatGPT auth through the registered dispatcher.",
            "Hosted-boundary approval is required before scoped context is sent to Codex.",
            "Chat does not call Codex directly.",
            "No active repository files are changed.",
        ],
        equivalent_command=f'harness tasks add --title "Chat read-only summary" --execution-adapter read_only_summary --task-type read_only_repo_summary --project {project_root} --output json',
    )


def _draft_codex(project_root: Path, prompt: str) -> ChatDraftTask:
    return ChatDraftTask(
        title="Chat Codex isolated edit",
        description=f"Codex isolated edit requested from chat: {sanitize_for_logging(prompt)}",
        execution_adapter="codex_isolated_edit",
        task_type="codex_code_edit",
        safety_notes=[
            "Codex requires hosted-boundary approval before run creation.",
            "Hosted-boundary approval is not apply-back approval.",
            "Codex edits only an isolated workspace.",
            "Apply-back remains denied by default unless a separate inspected-diff approval path approves it.",
        ],
        equivalent_command=f'harness tasks add --title "Chat Codex isolated edit" --execution-adapter codex_isolated_edit --task-type codex_code_edit --project {project_root} --output json',
    )


def _orchestrators_response(state: ChatSessionState) -> dict[str, Any]:
    registry = builtin_spec_registry()
    orchestrators = [
        agent
        for agent in sorted(registry.agents.values(), key=lambda item: item.id)
        if agent.kind.value == "orchestrator"
    ]
    lines = [
        f"{agent.id} model={agent.model_profile} tool_policy={agent.tool_policy}"
        + (" [active]" if agent.id == _active_orchestrator_id(state) else "")
        for agent in orchestrators
    ]
    return _response(
        "orchestrators",
        "Orchestrators",
        lines or ["No built-in orchestrators found."],
        ok=True,
        extra={"orchestrators": [agent.model_dump(mode="json") for agent in orchestrators]},
    )


def _use_orchestrator_response(orchestrator_id: str | None, state: ChatSessionState) -> dict[str, Any]:
    if not orchestrator_id:
        return _response("missing_orchestrator", "Missing Orchestrator", ["Use /use <orchestrator_id>."], ok=False)
    registry = builtin_spec_registry()
    try:
        agent = registry.get_agent(orchestrator_id)
    except KeyError as exc:
        return _response("orchestrator_missing", "Orchestrator Not Found", [str(exc).strip("'")], ok=False)
    if agent.kind.value != "orchestrator":
        return _response(
            "not_orchestrator",
            "Not An Orchestrator",
            [f"{orchestrator_id} is a {agent.kind.value}, not an orchestrator."],
            ok=False,
        )
    state.selected_orchestrator_id = orchestrator_id
    return _response(
        "orchestrator_selected",
        "Orchestrator Selected",
        [
            f"Active orchestrator: {orchestrator_id}",
            f"Workbench: {_workbench_for_orchestrator(orchestrator_id)}",
            "Selection is session-local only.",
        ],
        ok=True,
        extra={"orchestrator": agent.model_dump(mode="json")},
    )


def _agents_response(state: ChatSessionState) -> dict[str, Any]:
    registry = builtin_spec_registry()
    workbench_id = _workbench_for_orchestrator(_active_orchestrator_id(state))
    workbench = registry.get_workbench(workbench_id)
    lines = []
    agents = []
    for agent_id in workbench.allowed_agents:
        agent = registry.get_agent(agent_id)
        agents.append(agent)
        lines.append(f"{agent.id} kind={agent.kind.value} model={agent.model_profile} policy={agent.tool_policy}")
    return _response(
        "agents",
        "Workbench Agents",
        lines,
        ok=True,
        extra={"workbench_id": workbench_id, "agents": [agent.model_dump(mode="json") for agent in agents]},
    )


def _status_response(project_root: Path, state: ChatSessionState | None = None) -> dict[str, Any]:
    context = build_operator_context(project_root)
    active = _active_orchestrator_id(state or ChatSessionState())
    lines = render_operator_context_lines(context, active_orchestrator=active)
    return _response("status", "Project State", lines, ok=True, extra={"context": context})


def _tasks_response(project_root: Path) -> dict[str, Any]:
    if not _is_initialized(project_root):
        return _uninitialized_response(project_root)
    store = _require_store(project_root)
    tasks = store.list_tasks()[:10]
    lines = ["No tasks found."] if not tasks else [
        f"{task.id} [{task.status.value}] {task.title} adapter={task.metadata.get('execution_adapter', 'none')}"
        for task in tasks
    ]
    return _response("tasks", "Tasks", lines, ok=True, extra={"tasks": [task.model_dump(mode="json") for task in tasks]})


def _runs_response(project_root: Path, state: ChatSessionState) -> dict[str, Any]:
    if not _is_initialized(project_root):
        return _uninitialized_response(project_root)
    store = _require_store(project_root)
    runs = store.list_runs()[:10]
    if runs and state.latest_run_id is None:
        state.latest_run_id = runs[0].id
    lines = ["No runs found."] if not runs else [
        f"{run.id} [{run.status}] {run.task_type or 'unknown'}"
        for run in runs
    ]
    return _response("runs", "Runs", lines, ok=True, extra={"runs": [run.model_dump(mode="json") for run in runs]})


def _leases_response(project_root: Path, state: ChatSessionState) -> dict[str, Any]:
    if not _is_initialized(project_root):
        return _uninitialized_response(project_root)
    store = _require_store(project_root)
    leases = [lease for lease in store.list_task_leases() if lease.status.value == "active"]
    if leases:
        state.latest_lease_id = leases[0].id
    lines = ["No active leases."] if not leases else [
        f"{lease.id} task={lease.task_id} status={lease.status.value}"
        for lease in leases
    ]
    return _response("leases", "Active Leases", lines, ok=True, extra={"leases": [lease.model_dump(mode="json") for lease in leases]})


def _adapters_response(project_root: Path) -> dict[str, Any]:
    adapters = list_execution_adapter_descriptors()
    return _response(
        "adapters",
        "Registered Adapters",
        [
            f"{adapter.id}: task_types={','.join(adapter.supported_task_types)} side_effects={adapter.side_effect_summary}"
            for adapter in adapters
        ],
        ok=True,
        extra={"adapters": [adapter.model_dump(mode="json") for adapter in adapters]},
    )


def _blocked_response(project_root: Path) -> dict[str, Any]:
    if not _is_initialized(project_root):
        return _uninitialized_response(project_root)
    store = _require_store(project_root)
    tasks = [task for task in store.list_tasks() if task.status.value in {"blocked", "waiting_approval"}]
    lines = ["No blocked or waiting-approval tasks."] if not tasks else [
        f"{task.id} [{task.status.value}] {task.title}"
        for task in tasks
    ]
    return _response("blocked", "Blocked Tasks", lines, ok=True, extra={"tasks": [task.model_dump(mode="json") for task in tasks]})


def _task_detail_response(project_root: Path, task_id: str | None) -> dict[str, Any]:
    if not _is_initialized(project_root):
        return _uninitialized_response(project_root)
    if not task_id:
        return _response("missing_task_ref", "Missing Task", ["Provide a task id or create/select a task first."], ok=False)
    store = _require_store(project_root)
    try:
        task = store.get_task(task_id)
    except KeyError as exc:
        return _response("task_missing", "Task Not Found", [str(exc).strip("'")], ok=False)
    return _response(
        "task_detail",
        "Task Detail",
        [
            f"Task: {task.id}",
            f"Status: {task.status.value}",
            f"Title: {task.title}",
            f"Adapter: {task.metadata.get('execution_adapter', 'none')}",
            f"Task type: {task.metadata.get('task_type', 'none')}",
            f"Run: {task.run_id or 'none'}",
        ],
        ok=True,
        extra={"task": task.model_dump(mode="json")},
    )


def _run_detail_response(project_root: Path, run_id: str | None) -> dict[str, Any]:
    if not _is_initialized(project_root):
        return _uninitialized_response(project_root)
    if not run_id:
        return _response("missing_run_ref", "Missing Run", ["Provide a run id or execute a task first."], ok=False)
    store = _require_store(project_root)
    try:
        run = store.get_run(run_id)
        manifest = store.build_run_manifest(run_id)
    except KeyError as exc:
        return _response("run_missing", "Run Not Found", [str(exc).strip("'")], ok=False)
    return _response(
        "run_detail",
        "Run Detail",
        [
            f"Run: {run.id}",
            f"Status: {run.status}",
            f"Task type: {run.task_type or 'unknown'}",
            f"Task: {manifest.task_id or 'none'}",
            f"Artifacts: {len(manifest.artifacts)}",
        ],
        ok=True,
        extra={"run": run.model_dump(mode="json"), "manifest": manifest.model_dump(mode="json")},
    )


def _artifact_response(project_root: Path, artifact_id: str | None) -> dict[str, Any]:
    if not _is_initialized(project_root):
        return _uninitialized_response(project_root)
    if not artifact_id:
        return _response("missing_artifact_ref", "Missing Artifact", ["Provide an artifact id or run a task first."], ok=False)
    store = _require_store(project_root)
    try:
        artifact = store.get_artifact(artifact_id)
    except KeyError as exc:
        return _response("artifact_missing", "Artifact Not Found", [str(exc).strip("'")], ok=False)
    return _artifact_payload_response(artifact)


def _artifact_payload_response(artifact: ArtifactRecord) -> dict[str, Any]:
    return _response(
        "artifact_detail",
        "Artifact Metadata",
        [
            f"Artifact: {artifact.id}",
            f"Kind: {artifact.kind}",
            f"Run: {artifact.run_id}",
            f"Path: {artifact.path}",
            f"SHA256: {artifact.sha256 or 'unknown'}",
            f"Redaction: {artifact.redaction_state}",
        ],
        ok=True,
        extra={"artifact": artifact.model_dump(mode="json")},
    )


def _lease_next_response(project_root: Path, state: ChatSessionState) -> dict[str, Any]:
    if not _is_initialized(project_root):
        return _uninitialized_response(project_root)
    store = _require_store(project_root)
    selection = store.daemon_run_once(owner="chat_cli", pid=None)
    if selection.lease is None:
        return _response(
            "lease_none",
            "No Lease Acquired",
            [f"Decision: {selection.decision}", f"Pause reasons: {selection.pause_reasons}"],
            ok=False,
            extra={"tick": selection.model_dump(mode="json")},
        )
    state.latest_task_id = selection.selected_task.id if selection.selected_task else None
    state.latest_lease_id = selection.lease.id
    state.progress.append(f"lease acquired: {selection.lease.id}")
    return _response(
        "lease_acquired",
        "Lease Acquired",
        [
            f"Task: {selection.selected_task.id if selection.selected_task else 'unknown'}",
            f"Lease: {selection.lease.id}",
            "Next: inspect the lease or run the registered adapter.",
        ],
        ok=True,
        extra={"tick": selection.model_dump(mode="json")},
    )


def _inspect_lease_response(project_root: Path, lease_id: str | None, state: ChatSessionState) -> dict[str, Any]:
    if not _is_initialized(project_root):
        return _uninitialized_response(project_root)
    if not lease_id:
        return _response("missing_lease_ref", "Missing Lease", ["Provide a lease id or lease the next task first."], ok=False)
    store = _require_store(project_root)
    try:
        inspection = store.inspect_task_lease(lease_id)
    except KeyError as exc:
        return _response("lease_missing", "Lease Not Found", [str(exc).strip("'")], ok=False)
    state.latest_lease_id = lease_id
    if inspection.task:
        state.latest_task_id = inspection.task.id
    eligibility = inspection.execution_eligibility
    state.progress.append(f"eligibility checked: {lease_id}")
    return _response(
        "lease_inspection",
        "Lease Inspection",
        [
            f"Lease: {inspection.lease.id}",
            f"Status: {inspection.lease.status.value}",
            f"Task: {inspection.task.id if inspection.task else 'missing'}",
            f"Adapter: {eligibility.get('adapter_id') or 'none'}",
            f"Eligible: {eligibility.get('eligible')}",
            f"Policy: {eligibility.get('policy_sha256') or 'none'}",
            f"Rejection reasons: {eligibility.get('rejection_reasons') or []}",
            f"Recovery: {inspection.recovery_recommendation.get('action')}",
        ],
        ok=True,
        extra={"inspection": inspection.model_dump(mode="json")},
    )


def _prepare_execute_response(project_root: Path, lease_id: str | None, state: ChatSessionState) -> dict[str, Any]:
    if not _is_initialized(project_root):
        return _uninitialized_response(project_root)
    if not lease_id:
        return _response("missing_execute_lease", "Missing Lease", ["Provide a lease id or lease the next task first."], ok=False)
    inspection = _inspect_lease_response(project_root, lease_id, state)
    if not inspection.get("ok"):
        return inspection
    payload = inspection["inspection"]
    eligibility = payload["execution_eligibility"]
    if not eligibility.get("eligible"):
        return _response(
            "execute_ineligible",
            "Execution Ineligible",
            [
                f"Lease: {lease_id}",
                f"Adapter: {eligibility.get('adapter_id') or 'none'}",
                f"Rejection reasons: {eligibility.get('rejection_reasons') or []}",
                *_recovery_lines_for_rejection(eligibility.get("rejection_reasons") or []),
            ],
            ok=False,
            extra={"inspection": payload},
        )
    state.pending_execute_lease_id = lease_id
    return _response(
        "execute_confirmation_required",
        "Confirm Registered Adapter Dispatch",
        [
            f"Lease: {lease_id}",
            f"Adapter: {eligibility.get('adapter_id')}",
            "Type yes or /confirm to dispatch this registered adapter. Type no to cancel.",
        ],
        ok=True,
        extra={"inspection": payload},
    )


def _execute_response(project_root: Path, lease_id: str, state: ChatSessionState) -> dict[str, Any]:
    if not _is_initialized(project_root):
        return _uninitialized_response(project_root)
    result = execute_lease(project_root, lease_id, owner="chat_cli")
    if result.task:
        state.latest_task_id = result.task.id
        if result.task.status.value == "failed":
            state.latest_failed_task_id = result.task.id
    if result.lease:
        state.latest_lease_id = result.lease.id
    if result.run:
        state.latest_run_id = result.run.id
        state.progress.append(f"run terminal: {result.run.id} status={result.run.status}")
    if result.manifest:
        for artifact in result.manifest.artifacts:
            if artifact.kind in {"isolated_unified_diff", "final_report"}:
                state.latest_diff_artifact = artifact.id or state.latest_diff_artifact
    state.progress.append(f"adapter decision: {result.decision}")
    if (
        result.adapter_id in {"codex_isolated_edit", "read_only_summary"}
        and result.decision in {"execution_adapter_rejected", "codex_isolated_edit_blocked_policy"}
        and _needs_hosted_approval(result.rejection_reasons + result.errors)
    ):
        state.pending_hosted_approval = True
    lines = [
        f"Decision: {result.decision}",
        f"Adapter: {result.adapter_id or 'none'}",
        f"Task: {result.task.id if result.task else 'none'}",
        f"Run: {result.run.id if result.run else 'none'}",
        f"Rejection reasons: {result.rejection_reasons}",
        f"Errors: {result.errors}",
    ]
    lines.extend(_recovery_lines_for_execution(result))
    lines.extend(_summary_lines_from_result(project_root, result.adapter_result))
    if state.pending_hosted_approval:
        lines.extend(
            [
                "Hosted-boundary approval is required before Codex run creation.",
                "Hosted-boundary approval is not apply-back approval.",
                "This approval permits scoped Codex execution only.",
                "Type yes or /confirm to create a one-day Codex hosted-boundary approval profile.",
            ]
        )
    return _response(
        "execute_result",
        "Execution Result",
        lines,
        ok=result.ok,
        extra={"result": result.model_dump(mode="json")},
    )


def _recommend_next_response(project_root: Path, state: ChatSessionState) -> dict[str, Any]:
    if not _is_initialized(project_root):
        return _response(
            "next_recommendation",
            "Next Recommendation",
            [
                "Initialize the project before creating tasks or running adapters.",
                f"Equivalent command: harness init --project {resolve_project_root(project_root)}",
                "Chat shortcut: /init",
            ],
            ok=True,
            extra={"recommendation": {"id": "initialize_project", "command": f"harness init --project {resolve_project_root(project_root)}"}},
        )
    store = _require_store(project_root)
    tasks = store.list_tasks()
    leases = store.list_task_leases()
    runs = store.list_runs()
    active_leases = [lease for lease in leases if lease.status.value == "active"]
    blocked = [task for task in tasks if task.status.value in {"blocked", "waiting_approval"}]
    ready = [task for task in tasks if task.status.value == "ready"]
    failed_runs = [run for run in runs if run.status == "failed"]
    if state.pending_hosted_approval:
        lines = [
            "Create the pending Codex hosted-boundary approval if you want to continue the blocked Codex run.",
            "Chat shortcut: yes or /confirm",
            "Equivalent command: harness approvals add --backend codex_cli --data-boundary hosted_provider --task-types codex_code_edit --task-types read_only_repo_summary --duration-days 1 --project .",
            "This is not apply-back approval.",
        ]
        rec_id = "confirm_hosted_approval"
    elif state.pending_execute_lease_id:
        lines = [
            f"Dispatch the inspected registered adapter for lease {state.pending_execute_lease_id}.",
            "Chat shortcut: yes or /confirm",
            f"Equivalent command: harness daemon execute {state.pending_execute_lease_id} --project {resolve_project_root(project_root)}",
        ]
        rec_id = "confirm_dispatch"
    elif active_leases:
        lease = active_leases[0]
        lines = [
            f"Inspect and dispatch the active lease {lease.id}.",
            f"Equivalent command: harness daemon inspect-lease {lease.id} --project {resolve_project_root(project_root)}",
            f"Then: harness daemon execute {lease.id} --project {resolve_project_root(project_root)}",
        ]
        rec_id = "inspect_active_lease"
        state.latest_lease_id = lease.id
        state.latest_task_id = lease.task_id
    elif ready:
        task = ready[0]
        lines = [
            f"Lease the next ready task: {task.id} [{task.title}]",
            f"Equivalent command: harness daemon run-once --project {resolve_project_root(project_root)}",
            "Chat shortcut: lease the next task",
        ]
        rec_id = "lease_ready_task"
    elif blocked:
        task = blocked[0]
        lines = [
            f"Resolve the blocked task first: {task.id} [{task.status.value}] {task.title}",
            f"Equivalent command: harness tasks graph --project {resolve_project_root(project_root)} --output json",
            "Chat shortcut: what is blocked?",
        ]
        rec_id = "resolve_blocked_task"
    elif failed_runs:
        run = failed_runs[0]
        lines = [
            f"Inspect the latest failed run: {run.id}",
            f"Equivalent command: harness show {run.id} --project {resolve_project_root(project_root)}",
            "Then inspect artifacts or retry the task explicitly if the failure is understood.",
        ]
        rec_id = "inspect_failed_run"
        state.latest_run_id = run.id
    else:
        lines = [
            "Create a bounded task through chat.",
            "For repository context: say 'summarize this repo'.",
            "For an isolated coding attempt: say 'fix this bug with Codex'.",
            "For metadata-only evidence: say 'create dry run task'.",
        ]
        rec_id = "create_bounded_task"
    return _response(
        "next_recommendation",
        "Next Recommendation",
        lines,
        ok=True,
        extra={
            "recommendation": {
                "id": rec_id,
                "tasks_total": len(tasks),
                "ready_tasks": len(ready),
                "blocked_tasks": len(blocked),
                "active_leases": len(active_leases),
                "failed_runs": len(failed_runs),
            }
        },
    )


def _create_and_run_orchestration(
    project_root: Path,
    state: ChatSessionState,
    draft: OrchestratedRunDraft,
) -> dict[str, Any]:
    store = _require_store(project_root)
    objective = store.create_objective(
        title=draft.objective_title,
        description=draft.objective_description,
        priority=1000,
        workbench_id=draft.workbench_id,
        metadata={
            "created_by": "harness_chat",
            "orchestrator_id": draft.orchestrator_id,
            "execution_adapter": CODEX_ORCHESTRATION_ADAPTER,
        },
    )
    created_tasks: list[TaskRecord] = []
    for idx, task_draft in enumerate(draft.tasks):
        depends_on = [created_tasks[dep_idx].id for dep_idx in task_draft.depends_on_indexes]
        task = store.create_task(
            title=task_draft.title,
            description=task_draft.description,
            priority=1000 - idx,
            objective_id=objective.id,
            workbench_id=task_draft.workbench_id,
            agent_id=task_draft.agent_id,
            spec_source_kind="builtin",
            depends_on=depends_on,
            metadata={
                "execution_adapter": CODEX_ORCHESTRATION_ADAPTER,
                "task_type": CODEX_ORCHESTRATION_TASK_TYPE,
                "chat_orchestrated": True,
                "orchestrator_id": draft.orchestrator_id,
            },
        )
        created_tasks.append(task)
    state.latest_objective_id = objective.id
    state.latest_task_id = created_tasks[0].id if created_tasks else None
    state.latest_orchestration = {
        "draft": draft.to_payload(),
        "objective": objective.model_dump(mode="json"),
        "tasks": [task.model_dump(mode="json") for task in created_tasks],
    }
    state.progress.append(f"orchestration objective created: {objective.id}")
    return _run_orchestration_loop(project_root, state, objective.id)


def _run_orchestration_loop(project_root: Path, state: ChatSessionState, objective_id: str) -> dict[str, Any]:
    store = _require_store(project_root)
    lines = [f"Objective: {objective_id}", "Foreground orchestration started."]
    results: list[dict[str, Any]] = []
    max_steps = 24
    for _step in range(max_steps):
        if state.stop_requested:
            state.stop_requested = False
            lines.append("Stopped at operator request.")
            break
        objective_tasks = store.list_tasks(objective_id=objective_id)
        if objective_tasks and all(task.status.value in {"succeeded", "failed", "cancelled", "skipped"} for task in objective_tasks):
            lines.append("All objective tasks are terminal.")
            break
        tick = store.daemon_run_once(owner=ORCHESTRATION_OWNER, pid=None)
        if tick.lease is None:
            lines.append(f"Lease decision: {tick.decision}")
            if tick.pause_reasons:
                lines.append(f"Pause reasons: {tick.pause_reasons}")
            break
        if tick.selected_task is None or tick.selected_task.objective_id != objective_id:
            lines.append("Lease did not belong to this orchestration objective; stopping without dispatch.")
            break
        state.latest_task_id = tick.selected_task.id
        state.latest_lease_id = tick.lease.id
        state.progress.append(f"orchestration lease acquired: {tick.lease.id}")
        result = execute_lease(project_root, tick.lease.id, owner=ORCHESTRATION_OWNER)
        result_payload = result.model_dump(mode="json")
        results.append(result_payload)
        if result.task:
            state.latest_task_id = result.task.id
            if result.task.status.value == "failed":
                state.latest_failed_task_id = result.task.id
        if result.lease:
            state.latest_lease_id = result.lease.id
        if result.run:
            state.latest_run_id = result.run.id
        if result.manifest:
            for artifact in result.manifest.artifacts:
                if artifact.kind in {"isolated_unified_diff", "final_report"}:
                    state.latest_diff_artifact = artifact.id or state.latest_diff_artifact
        state.progress.append(f"orchestration adapter decision: {result.decision}")
        lines.extend(
            [
                f"Task: {result.task.id if result.task else tick.selected_task.id}",
                f"Lease: {tick.lease.id}",
                f"Adapter: {result.adapter_id or 'none'}",
                f"Decision: {result.decision}",
                f"Run: {result.run.id if result.run else 'none'}",
            ]
        )
        if result.rejection_reasons:
            lines.append(f"Rejection reasons: {result.rejection_reasons}")
        lines.extend(_summary_lines_from_result(project_root, result.adapter_result))
        if _needs_hosted_approval(result.rejection_reasons + result.errors):
            state.pending_hosted_approval = True
            lines.extend(
                [
                    "Hosted-boundary approval is required before Codex run creation.",
                    "Hosted-boundary approval is not apply-back approval.",
                    "Type yes or /confirm to create a one-day Codex hosted-boundary approval profile, then /run to continue.",
                ]
            )
            break
        if not result.ok:
            lines.append("Stopping orchestration after adapter failure/rejection.")
            break
    else:
        lines.append(f"Stopped after max foreground steps: {max_steps}")
    refreshed_tasks = store.list_tasks(objective_id=objective_id)
    state.latest_orchestration = {
        **(state.latest_orchestration or {}),
        "objective_id": objective_id,
        "tasks": [task.model_dump(mode="json") for task in refreshed_tasks],
        "results": results,
    }
    return _response(
        "orchestration_result",
        "Orchestration Result",
        lines,
        ok=not any(not item.get("ok", False) for item in results),
        extra={"orchestration": state.latest_orchestration},
    )


def _summary_lines_from_result(project_root: Path, adapter_result: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    if not adapter_result:
        return lines
    if "final_summary" in adapter_result:
        lines.append(f"Final summary: {adapter_result['final_summary']}")
    if "tools_executed" in adapter_result:
        lines.append(f"Tools executed: {adapter_result['tools_executed']}")
    if "invalid_model_command_count" in adapter_result:
        lines.append(f"Invalid command count: {adapter_result['invalid_model_command_count']}")
    if "changed_files" in adapter_result:
        lines.append(f"Changed files: {adapter_result['changed_files']}")
    if "apply_back_decision" in adapter_result:
        lines.append(f"Apply-back decision: {adapter_result['apply_back_decision']}")
    if "applied_files" in adapter_result:
        lines.append(f"Applied files: {adapter_result['applied_files']}")
    if "policy_violations" in adapter_result:
        lines.append(f"Policy violations: {adapter_result['policy_violations']}")
    artifacts = adapter_result.get("artifacts")
    if isinstance(artifacts, dict):
        lines.append("Artifacts:")
        for kind, path in sorted(artifacts.items()):
            lines.append(f"- {kind}: {path}")
    return lines


def _recovery_lines_for_execution(result: Any) -> list[str]:
    reasons = list(getattr(result, "rejection_reasons", []) or []) + list(getattr(result, "errors", []) or [])
    return _recovery_lines_for_rejection(reasons, decision=str(getattr(result, "decision", "")))


def _recovery_lines_for_rejection(reasons: list[Any], *, decision: str = "") -> list[str]:
    joined = " ".join(str(reason).casefold() for reason in reasons)
    decision = decision.casefold()
    if not reasons and not decision:
        return []
    if "hosted" in joined and "approval" in joined:
        return [
            "Recovery: add an explicit Codex hosted-boundary approval, then rerun the active lease.",
            "Command: harness approvals add --backend codex_cli --data-boundary hosted_provider --task-types codex_code_edit --duration-days 1 --project .",
        ]
    if "requires active lease" in joined or "released" in joined or "duplicate" in decision:
        return [
            "Recovery: inspect the lease; duplicate or released leases are not re-executed.",
            "Command: harness daemon inspect-lease <lease_id> --project . --output json",
        ]
    if "unknown execution adapter" in joined or "missing execution_adapter" in joined:
        return [
            "Recovery: create a task with one of the registered adapters.",
            "Command: harness daemon adapters --project . --output json",
        ]
    if "blocked" in joined or "policy" in joined:
        return [
            "Recovery: inspect the run or lease evidence and narrow the task; policy-blocked changes are not applied.",
            "Command: harness daemon inspect-lease <lease_id> --project . --output json",
        ]
    if "unavailable" in joined:
        return [
            "Recovery: run release diagnostics and inspect adapter eligibility before retrying explicitly.",
            "Command: harness doctor --release --project . --output json",
        ]
    return [
        "Recovery: inspect local evidence before retrying.",
        "Command: harness daemon inspect-lease <lease_id> --project . --output json",
    ]


def _apply_back_review_response(
    project_root: Path,
    state: ChatSessionState,
    *,
    choice: str | None = None,
) -> dict[str, Any]:
    if not _is_initialized(project_root):
        return _uninitialized_response(project_root)
    if not state.latest_run_id:
        return _response("missing_run_ref", "No Run", ["Run a Codex isolated edit before reviewing apply-back."], ok=False)
    store = _require_store(project_root)
    try:
        artifacts = store.list_artifacts(state.latest_run_id)
    except KeyError as exc:
        return _response("run_missing", "Run Not Found", [str(exc).strip("'")], ok=False)
    interesting = [artifact for artifact in artifacts if artifact.kind in {"isolated_unified_diff", "isolated_diff_stat", "final_report"}]
    lines = [
        "Apply-back review uses existing inspected diff artifacts only.",
        "Hosted-boundary approval is not apply-back approval.",
        "This chat path does not parse or apply patches from chat text.",
    ]
    if choice == "deny":
        lines.append("Apply-back denied from chat review; active repository mutation was not requested.")
    elif choice == "keep":
        lines.append("Isolation retained for operator inspection; active repository mutation was not requested.")
    elif choice == "approve":
        lines.append("Apply-back approval was not performed because chat can only use an existing explicit apply-back approval provider path.")
        lines.append("No active repository mutation was requested by chat.")
    lines.extend(f"{artifact.kind}: {artifact.path}" for artifact in interesting)
    return _response(
        "apply_back_review",
        "Apply-Back Review",
        lines,
        ok=choice != "approve",
        extra={"artifacts": [artifact.model_dump(mode="json") for artifact in interesting]},
    )


def _plan_response(state: ChatSessionState) -> dict[str, Any]:
    if state.pending_orchestration is not None:
        draft = state.pending_orchestration
        lines = [
            f"Pending objective: {draft.objective_title}",
            f"Orchestrator: {draft.orchestrator_id}",
            f"Workbench: {draft.workbench_id}",
            *[f"{idx + 1}. {task.agent_id}: {task.title}" for idx, task in enumerate(draft.tasks)],
        ]
        return _response("orchestration_plan", "Pending Plan", lines, ok=True, extra={"draft": draft.to_payload()})
    if state.latest_orchestration is not None:
        tasks = state.latest_orchestration.get("tasks", [])
        lines = [
            f"Objective: {state.latest_orchestration.get('objective_id') or state.latest_objective_id}",
            *[f"{task['id']} [{task['status']}] {task['agent_id']}: {task['title']}" for task in tasks],
        ]
        return _response("orchestration_plan", "Latest Plan", lines, ok=True, extra={"orchestration": state.latest_orchestration})
    return _response("missing_plan", "No Plan", ["No pending or latest orchestration plan in this chat session."], ok=False)


def _active_orchestrator_id(state: ChatSessionState) -> str:
    selected = state.selected_orchestrator_id
    registry = builtin_spec_registry()
    if selected in registry.agents and registry.agents[selected].kind.value == "orchestrator":
        return selected
    return _default_orchestrator_id()


def _default_orchestrator_id() -> str:
    registry = builtin_spec_registry()
    if DEFAULT_ORCHESTRATOR_ID in registry.agents:
        return DEFAULT_ORCHESTRATOR_ID
    orchestrators = sorted(agent.id for agent in registry.agents.values() if agent.kind.value == "orchestrator")
    return orchestrators[0] if orchestrators else ""


def _workbench_for_orchestrator(orchestrator_id: str) -> str:
    registry = builtin_spec_registry()
    for workbench in registry.workbenches.values():
        if orchestrator_id in workbench.allowed_agents:
            return workbench.id
    return "coding"


def _orchestration_tasks_for(workbench_id: str, prompt: str) -> list[OrchestratedTaskDraft]:
    goal = str(sanitize_for_logging(prompt))
    if workbench_id == "quant":
        return [
            OrchestratedTaskDraft(
                title="Research context and hypothesis plan",
                description=f"Use Codex isolation to prepare quant research context for: {goal}",
                agent_id="quant_researcher",
                workbench_id="quant",
                priority=1000,
            ),
            OrchestratedTaskDraft(
                title="Data and implementation requirements",
                description=f"Use Codex isolation to identify data and implementation requirements for: {goal}",
                agent_id="data_engineer",
                workbench_id="quant",
                depends_on_indexes=[0],
                priority=999,
            ),
            OrchestratedTaskDraft(
                title="Risk and validity review",
                description=f"Use Codex isolation to review risks, leakage, and validity for: {goal}",
                agent_id="statistical_validity_reviewer",
                workbench_id="quant",
                depends_on_indexes=[0, 1],
                priority=998,
            ),
            OrchestratedTaskDraft(
                title="Orchestrator synthesis",
                description=f"Use Codex isolation to synthesize the quant workflow evidence for: {goal}",
                agent_id="quant_orchestrator",
                workbench_id="quant",
                depends_on_indexes=[0, 1, 2],
                priority=997,
            ),
        ]
    if workbench_id == "personal":
        return [
            OrchestratedTaskDraft(
                title="Personal research draft",
                description=f"Use Codex isolation to prepare personal research notes for: {goal}",
                agent_id="job_researcher",
                workbench_id="personal",
                priority=1000,
            ),
            OrchestratedTaskDraft(
                title="Personal orchestrator synthesis",
                description=f"Use Codex isolation to synthesize personal research evidence for: {goal}",
                agent_id="personal_orchestrator",
                workbench_id="personal",
                depends_on_indexes=[0],
                priority=999,
            ),
        ]
    return [
        OrchestratedTaskDraft(
            title="Inspect repository context",
            description=f"Use Codex isolation to inspect repository context for: {goal}",
            agent_id="repo_inspector",
            workbench_id="coding",
            priority=1000,
        ),
        OrchestratedTaskDraft(
            title="Prepare isolated code edit",
            description=f"Use Codex isolation to prepare a scoped code edit for: {goal}",
            agent_id="code_editor",
            workbench_id="coding",
            depends_on_indexes=[0],
            priority=999,
        ),
        OrchestratedTaskDraft(
            title="Review test impact",
            description=f"Use Codex isolation to review expected test impact for: {goal}",
            agent_id="test_runner",
            workbench_id="coding",
            depends_on_indexes=[1],
            priority=998,
        ),
        OrchestratedTaskDraft(
            title="Orchestrator synthesis",
            description=f"Use Codex isolation to synthesize coding task evidence for: {goal}",
            agent_id="coding_orchestrator",
            workbench_id="coding",
            depends_on_indexes=[0, 1, 2],
            priority=997,
        ),
    ]


def _objective_title_for(prompt: str, orchestrator_id: str) -> str:
    normalized = " ".join(prompt.strip().split())
    if not normalized:
        normalized = "Chat orchestrated work"
    if len(normalized) > 72:
        normalized = normalized[:69].rstrip() + "..."
    return f"{orchestrator_id}: {normalized}"


def _needs_hosted_approval(reasons: list[str]) -> bool:
    joined = " ".join(reason.casefold() for reason in reasons)
    return "hosted" in joined and "approval" in joined


def _resolve_task_ref(value: str | None, state: ChatSessionState) -> str | None:
    if value in {None, "latest"}:
        return state.latest_task_id
    if value in {"failed", "the_failed_task"}:
        return state.latest_failed_task_id
    return value


def _resolve_run_ref(value: str | None, state: ChatSessionState) -> str | None:
    if value in {None, "latest"}:
        return state.latest_run_id
    return value


def _response(
    kind: str,
    title: str,
    lines: list[str],
    *,
    ok: bool = True,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "schema_version": CHAT_RESPONSE_SCHEMA_VERSION,
        "ok": ok,
        "kind": kind,
        "title": title,
        "lines": [str(sanitize_for_logging(line)) for line in lines],
    }
    if extra:
        payload.update(sanitize_for_logging(extra))
    return payload


def _uninitialized_response(project_root: Path) -> dict[str, Any]:
    return _response(
        "project_uninitialized",
        "Project Not Initialized",
        [
            f"Project is not initialized: {resolve_project_root(project_root)}",
            "Type /init or say 'initialize this project' to set up Harness records here.",
            "You can still ask normal read-only chat questions before initialization.",
        ],
        ok=False,
    )


def _require_store(project_root: Path) -> SQLiteStore:
    project_root = resolve_project_root(project_root)
    if not _is_initialized(project_root):
        raise ValueError(f"Project is not initialized: {project_root}")
    return SQLiteStore(project_root)


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


def _update_chat_gitignore(project_root: Path) -> None:
    path = resolve_project_root(project_root) / ".gitignore"
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    required_entries = [
        ".harness/runs/",
        ".harness/harness.sqlite",
        ".harness/approvals.yaml",
        ".harness/tmp/",
        "*.egg-info/",
    ]
    existing_lines = existing.splitlines()
    if all(entry in existing_lines for entry in required_entries):
        return
    content = existing.rstrip()
    if content:
        content += "\n\n"
    if "# Harness local artifacts" not in existing_lines:
        content += "# Harness local artifacts\n"
    for entry in required_entries:
        if entry not in existing_lines:
            content += f"{entry}\n"
    path.write_text(content, encoding="utf-8")


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


def _normalize(text: str) -> str:
    return " ".join(text.casefold().strip().replace("?", "").split())
