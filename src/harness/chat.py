from __future__ import annotations

import hashlib
import json
import shlex
import sqlite3
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, TextIO

from harness.action_executors import execute_managed_action
from harness.action_policy import decide_managed_action
from harness.action_proposals import ActionContract, contract_from_tool_request
from harness.action_router import ManagedActionDecisionStatus, ManagedActionResult, route_managed_action
from harness.approvals import ApprovalStore
from harness.autonomy import (
    AutonomyDecision,
    AutonomyDecisionStatus,
    AutonomyEvaluationInput,
    AutonomousApprovalRecord,
    evaluate_autonomy,
    get_builtin_autonomy_policy,
)
from harness.backends.local_openai import LocalEndpointUnavailable
from harness.capabilities import build_capability_catalog
from harness.chat_model import ChatContext, ChatMessage, ChatModel, ChatResponse, build_default_chat_model
from harness.chat_tools import (
    ChatToolRequest,
    chat_tool_specs_payload,
    default_chat_tool_context,
    parse_tool_request,
    run_chat_tool,
)
from harness.config import HARNESS_DIR, default_config, load_config, write_default_config
from harness.context_pack import pack_chat_context
from harness.execution import execute_lease, list_execution_adapter_descriptors
from harness.events import append_jsonl
from harness.memory.sqlite_store import SQLiteStore
from harness.models import ArtifactRecord, RunRecord, TaskLease, TaskRecord
from harness.objective_runner import run_objective_autonomously
from harness.operator_context import build_operator_context, render_operator_context_lines
from harness.paths import resolve_project_root
from harness.progress import build_orchestration_progress
from harness.registry import builtin_spec_registry
from harness.security import sanitize_for_logging
from harness.security_explanations import explanations_from_reasons
from harness.test_runner import DockerTestRunner, RunTestsDecision
from harness.tools.patch import apply_planned_updates, plan_unified_diff
from harness.workflow_templates import WorkflowTemplate, template_for_intent


CHAT_SCHEMA_VERSION = "harness.chat/v1"
CHAT_RESPONSE_SCHEMA_VERSION = "harness.chat_response/v1"
CHAT_INTENT_SCHEMA_VERSION = "harness.chat_intent/v1"
ORCHESTRATION_DRAFT_SCHEMA_VERSION = "harness.chat_orchestration_draft/v1"
AUTONOMOUS_READ_LOOP_SCHEMA_VERSION = "harness.autonomous_read_loop/v1"

CODEX_ORCHESTRATION_ADAPTER = "codex_isolated_edit"
CODEX_ORCHESTRATION_TASK_TYPE = "codex_code_edit"
DEFAULT_ORCHESTRATOR_ID = "coding_orchestrator"
ORCHESTRATION_OWNER = "chat_orchestrator"
MAX_CHAT_TOOL_CALLS = 3


def run_autonomous_read_loop(
    goal: str,
    project_root: Path,
    *,
    autonomy_profile_id: str = "safe-local",
    chat_model: ChatModel | None = None,
    allow_action_contracts: bool = False,
    auto_run_created_objective: bool = False,
) -> dict[str, Any]:
    project_root = resolve_project_root(project_root)
    policy = get_builtin_autonomy_policy(autonomy_profile_id)
    loop_id = f"act_{uuid.uuid4().hex[:12]}"
    evidence_path = project_root / HARNESS_DIR / "autonomy" / f"{loop_id}.jsonl"
    state = ChatSessionState(autonomy_profile_id=autonomy_profile_id)
    context_payload = chat_context(project_root)
    context_manifest = pack_chat_context(project_root)
    chat_ctx = ChatContext(
        project_root=str(project_root),
        model_profile=context_payload["chat"]["default_model_profile"],
        mode="act",
        context_blocks=[block.to_payload() for block in context_manifest.blocks],
        safety_boundaries=list(context_payload["safety_boundaries"]),
    )
    messages = _model_messages(goal, state, chat_ctx)
    observations: list[dict[str, Any]] = []
    stop_reason = "not_started"
    final_answer = ""
    model_turns = 0
    tool_calls = 0
    consecutive_failures = 0
    append_jsonl(
        evidence_path,
        {
            "schema_version": "harness.autonomous_read_loop_event/v1",
            "loop_id": loop_id,
            "event": "started",
            "goal": sanitize_for_logging(goal),
            "autonomy_profile_id": autonomy_profile_id,
            "budgets": policy.budget.model_dump(mode="json"),
        },
    )
    try:
        model = chat_model or build_default_chat_model(project_root)
        while model_turns < policy.budget.max_model_turns:
            model_turns += 1
            model_turn_id = f"{loop_id}:turn:{model_turns}"
            model_response = model.complete(messages, chat_ctx)
            content = str(sanitize_for_logging(model_response.content)).strip()
            append_jsonl(
                evidence_path,
                {
                    "schema_version": "harness.autonomous_read_loop_event/v1",
                    "loop_id": loop_id,
                    "event": "model_turn",
                    "model_turn": model_turns,
                    "model_turn_id": model_turn_id,
                    "content": content,
                },
            )
            tool_request = parse_tool_request(content)
            if tool_request is None:
                final_answer = content or "The model returned an empty response."
                stop_reason = "final_answer"
                break
            if tool_calls >= policy.budget.max_tool_calls:
                final_answer = "Stopped before running another tool because the tool-call budget is exhausted."
                stop_reason = "tool_budget_exhausted"
                break
            tool_result = run_chat_tool(tool_request, default_chat_tool_context(project_root))
            tool_calls += 1
            observation = {
                "tool": tool_result.tool,
                "ok": tool_result.ok,
                "error_type": tool_result.error_type,
            }
            observations.append(observation)
            append_jsonl(
                evidence_path,
                {
                    "schema_version": "harness.autonomous_read_loop_event/v1",
                    "loop_id": loop_id,
                    "event": "tool_observation",
                    "model_turn": model_turns,
                    "model_turn_id": model_turn_id,
                    "tool_request": {
                        "tool": tool_request.tool,
                        "arguments": sanitize_for_logging(tool_request.arguments),
                    },
                    "observation": observation,
                },
            )
            if tool_result.error_type == "action_contract_required":
                if not allow_action_contracts:
                    final_answer = "Stopped because the model requested a side-effecting Harness tool."
                    stop_reason = "side_effect_tool_rejected"
                    break
                action_response = _action_contract_response(project_root, state, tool_request)
                action_observation = {
                    "tool": tool_request.tool,
                    "ok": bool(action_response.get("ok")),
                    "error_type": None if action_response.get("ok") else str(action_response.get("kind")),
                    "kind": action_response.get("kind"),
                }
                observations.append(action_observation)
                append_jsonl(
                    evidence_path,
                    {
                        "schema_version": "harness.autonomous_read_loop_event/v1",
                        "loop_id": loop_id,
                        "event": "action_contract_observation",
                        "model_turn": model_turns,
                        "model_turn_id": model_turn_id,
                        "observation": action_observation,
                        "response": sanitize_for_logging(action_response),
                    },
                )
                if not action_response.get("ok"):
                    final_answer = "\n".join(str(line) for line in action_response.get("lines", []))
                    stop_reason = str(action_response.get("kind") or "action_contract_failed")
                    break
                if (
                    auto_run_created_objective
                    and tool_request.tool == "create_task_graph"
                    and state.latest_objective_id
                    and _is_initialized(project_root)
                ):
                    objective_result = run_objective_autonomously(
                        project_root,
                        state.latest_objective_id,
                        autonomy_profile_id=autonomy_profile_id,
                    )
                    objective_observation = {
                        "tool": "objectives.run",
                        "ok": objective_result.ok,
                        "error_type": None if objective_result.ok else objective_result.stop_reason,
                        "kind": "autonomous_objective_run",
                        "objective_id": objective_result.objective_id,
                        "stop_reason": objective_result.stop_reason,
                        "adapter_dispatches": objective_result.adapter_dispatches,
                    }
                    observations.append(objective_observation)
                    append_jsonl(
                        evidence_path,
                        {
                            "schema_version": "harness.autonomous_read_loop_event/v1",
                            "loop_id": loop_id,
                            "event": "objective_run_observation",
                            "model_turn": model_turns,
                            "model_turn_id": model_turn_id,
                            "observation": objective_observation,
                            "response": objective_result.model_dump(mode="json"),
                        },
                    )
                    state.latest_run_id = (
                        objective_result.step_results[-1].run_id if objective_result.step_results else state.latest_run_id
                    )
                    if not objective_result.ok and objective_result.stop_reason not in {
                        "approval_required",
                        "requires_human_boundary",
                    }:
                        final_answer = f"Stopped after autonomous objective run: {objective_result.stop_reason}"
                        stop_reason = objective_result.stop_reason
                        break
                messages.append(ChatMessage(role="assistant", content=content))
                messages.append(ChatMessage(role="user", content=f"Harness action result:\n{json.dumps(sanitize_for_logging(action_response), sort_keys=True, default=str)}"))
                continue
            if not tool_result.ok:
                consecutive_failures += 1
                if consecutive_failures >= policy.budget.max_consecutive_failures:
                    final_answer = "Stopped because tool failures exceeded the autonomy budget."
                    stop_reason = "tool_failure_budget_exhausted"
                    break
            else:
                consecutive_failures = 0
            messages.append(ChatMessage(role="assistant", content=content))
            messages.append(ChatMessage(role="user", content=f"Harness tool result:\n{tool_result.to_message()}"))
        else:
            final_answer = "Stopped because the model-turn budget is exhausted."
            stop_reason = "model_turn_budget_exhausted"
    except LocalEndpointUnavailable as exc:
        final_answer = "\n".join(_local_model_unavailable_response(project_root, exc)["lines"])
        stop_reason = "chat_model_unavailable"
    append_jsonl(
        evidence_path,
        {
            "schema_version": "harness.autonomous_read_loop_event/v1",
            "loop_id": loop_id,
            "event": "stopped",
            "stop_reason": stop_reason,
            "model_turns": model_turns,
            "tool_calls": tool_calls,
            "observations": observations,
        },
    )
    return {
        "schema_version": AUTONOMOUS_READ_LOOP_SCHEMA_VERSION,
        "ok": stop_reason in {"final_answer", "tool_budget_exhausted", "model_turn_budget_exhausted"},
        "loop_id": loop_id,
        "project_root": str(project_root),
        "autonomy_profile_id": autonomy_profile_id,
        "goal": sanitize_for_logging(goal),
        "stop_reason": stop_reason,
        "final_answer": final_answer,
        "lines": final_answer.splitlines() if final_answer else [],
        "model_turns": model_turns,
        "tool_calls": tool_calls,
        "tool_results": observations,
        "evidence_path": str(evidence_path),
    }


@dataclass
class OrchestratedTaskDraft:
    title: str
    description: str
    agent_id: str
    workbench_id: str
    execution_adapter: str = CODEX_ORCHESTRATION_ADAPTER
    task_type: str = CODEX_ORCHESTRATION_TASK_TYPE
    depends_on_indexes: list[int] = field(default_factory=list)
    priority: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "description": self.description,
            "agent_id": self.agent_id,
            "workbench_id": self.workbench_id,
            "depends_on_indexes": self.depends_on_indexes,
            "priority": self.priority,
            "execution_adapter": self.execution_adapter,
            "task_type": self.task_type,
            "metadata": self.metadata,
        }


@dataclass
class OrchestratedRunDraft:
    objective_title: str
    objective_description: str
    orchestrator_id: str
    workbench_id: str
    tasks: list[OrchestratedTaskDraft]
    interpreted_intent: str = "codex_isolated_edit"
    proposed_action: str = "Create a visible objective/task graph and run it through registered adapters."
    required_approvals: list[str] = field(default_factory=lambda: ["hosted_provider_codex"])
    safety_notes: list[str] = field(default_factory=list)
    equivalent_commands: list[str] = field(default_factory=list)
    confirm_prompt: str = "Type yes, /confirm, or /run to create the objective and run this graph in the foreground."

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": ORCHESTRATION_DRAFT_SCHEMA_VERSION,
            "objective_title": self.objective_title,
            "objective_description": self.objective_description,
            "orchestrator_id": self.orchestrator_id,
            "workbench_id": self.workbench_id,
            "interpreted_intent": self.interpreted_intent,
            "proposed_action": self.proposed_action,
            "tasks": [task.to_payload() for task in self.tasks],
            "required_approvals": self.required_approvals,
            "safety_notes": self.safety_notes,
            "equivalent_commands": self.equivalent_commands,
            "confirm_prompt": self.confirm_prompt,
        }


@dataclass
class ChatDraftTask:
    title: str
    description: str
    execution_adapter: str
    task_type: str
    interpreted_intent: str = "task"
    proposed_action: str = "Create one Harness task from this draft."
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
            "interpreted_intent": self.interpreted_intent,
            "proposed_action": self.proposed_action,
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
    pending_action_contract: ActionContract | None = None
    pending_hosted_approval: bool = False
    selected_orchestrator_id: str | None = None
    latest_objective_id: str | None = None
    latest_orchestration: dict[str, Any] | None = None
    stop_requested: bool = False
    codex_like_mode: bool = False
    autonomy_profile_id: str = "manual"
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
        self.pending_action_contract = None
        self.pending_hosted_approval = False
        self.selected_orchestrator_id = None
        self.latest_objective_id = None
        self.latest_orchestration = None
        self.stop_requested = False
        self.codex_like_mode = False
        self.autonomy_profile_id = "manual"
        self.transcript = []
        self.progress = []


def chat_context(project_root: Path) -> dict[str, Any]:
    project_root = resolve_project_root(project_root)
    context = build_operator_context(project_root)
    try:
        cfg = load_config(project_root)
    except FileNotFoundError:
        cfg = default_config()
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
        "capabilities": context["capabilities"],
        "runtime_controls": context.get("runtime_controls"),
        "chat": {
            "default_model_profile": cfg.chat.default_model_profile,
            "mode": cfg.chat.mode,
            "allow_hosted_chat": cfg.chat.allow_hosted_chat,
            "allow_codex_subscription_chat": cfg.chat.allow_codex_subscription_chat,
            "hosted_fallback": False,
        },
        "context_warnings": context.get("memory", {}).get("warnings", []),
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
    chat_model: ChatModel | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    state = state or ChatSessionState()
    project_root = resolve_project_root(project_root)
    raw = text.strip()
    response = _dispatch_chat_input(raw, project_root, state, chat_model=chat_model, progress_callback=progress_callback)
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


def _dispatch_chat_input(
    raw: str,
    project_root: Path,
    state: ChatSessionState,
    *,
    chat_model: ChatModel | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    if not raw:
        return _response("empty", "No input", ["Type /help for available commands."])
    _emit_progress(progress_callback, "procedure", "Turn started")
    if raw in {"/quit", "quit", "exit"}:
        return _response("quit", "Goodbye", ["Exiting harness chat."], ok=True)
    if raw in {"/confirm", "yes", "y"}:
        return _confirm_pending(project_root, state)
    if raw in {"/decline", "no", "n", "cancel"}:
        state.pending_draft = None
        state.pending_orchestration = None
        state.pending_execute_lease_id = None
        state.pending_action_contract = None
        state.pending_hosted_approval = False
        return _response("declined", "Declined", ["No action was taken."], ok=True)
    if raw.startswith("/"):
        return _handle_slash(raw, project_root, state, chat_model=chat_model, progress_callback=progress_callback)
    return _handle_intent(raw, project_root, state, chat_model=chat_model, progress_callback=progress_callback)


def render_chat_response(response: dict[str, Any]) -> str:
    title = response.get("title") or response.get("kind") or "response"
    lines = [f"Harness: {title}"]
    for line in response.get("lines", []):
        lines.append(str(line))
    return "\n".join(lines)


def run_chat_loop(
    project_root: Path,
    stdin: TextIO,
    stdout: TextIO,
    *,
    codex_like: bool = False,
    autonomy_profile_id: str = "manual",
) -> int:
    project_root = resolve_project_root(project_root)
    state = ChatSessionState(codex_like_mode=codex_like, autonomy_profile_id=autonomy_profile_id)
    state.selected_orchestrator_id = _default_orchestrator_id()
    context = chat_context(project_root)
    stdout.write("Harness chat\n")
    stdout.write(f"Project: {context['project_root']}\n")
    stdout.write(f"Orchestrator: {state.selected_orchestrator_id or 'none'}\n")
    stdout.write(f"Mode: {'codex-like' if state.codex_like_mode else 'normal'}\n")
    stdout.write(f"Autonomy: {state.autonomy_profile_id}\n")
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
    elif normalized in {
        "show capabilities",
        "capabilities",
        "list capabilities",
        "what can harness do here",
        "what can harness do here?",
        "which actions need approval",
        "which actions need approval?",
    }:
        intent = "show_capabilities"
    elif normalized in {"show memory", "memory", "list memory"}:
        intent = "show_memory"
    elif normalized in {"show progress", "show orchestration progress", "where are we", "progress"}:
        intent = "show_progress"
    elif normalized in {"show tasks", "tasks", "list tasks"}:
        intent = "show_tasks"
    elif normalized in {"show latest run", "latest run", "show runs", "runs", "show recent runs", "recent runs"}:
        intent = "show_runs"
    elif normalized in {"review the last result", "review last result", "show last result", "last result"}:
        intent = "show_last_result"
    elif "run" in normalized and ("adapter" in normalized or "registered" in normalized):
        intent = "execute_adapter"
    elif "adapter" in normalized:
        intent = "show_adapters"
    elif normalized in {"why is this blocked", "why is this blocked?", "explain blocked", "security blockers"}:
        intent = "show_blocked"
    elif "blocked" in normalized:
        intent = "show_blocked"
    elif (
        normalized in {"what should i do next", "what next", "next steps", "what should i do"}
        or ("what" in normalized and "next" in normalized)
    ):
        intent = "recommend_next"
    elif "current project state" in normalized or normalized in {"status", "home"}:
        intent = "show_status"
    elif normalized in {"continue", "continue workflow", "keep going", "run next"}:
        intent = "continue_workflow"
    elif normalized in {"stop", "stop workflow", "stop work"}:
        intent = "stop_workflow"
    elif "dry run" in normalized:
        intent = "draft_dry_run"
    elif "read only summary" in normalized or "summary" in normalized or "summarize" in normalized or "inspect this repo" in normalized:
        intent = "repo_summary"
    elif normalized.startswith("plan ") or " repo planning" in normalized or "implementation plan" in normalized:
        intent = "repo_planning"
    elif (
        "fix" in normalized
        or "bug" in normalized
        or "failing test" in normalized
        or "implement" in normalized
        or "build" in normalized
        or "codex" in normalized
        or "isolated edit" in normalized
    ):
        intent = "coding_fix"
    elif "orchestrate" in normalized or "multi agent" in normalized:
        intent = "draft_orchestration"
    elif "lease" in normalized and "next" in normalized:
        intent = "lease_next"
    elif "inspect" in normalized and "lease" in normalized:
        intent = "inspect_lease"
    elif normalized in {"that diff", "show diff", "show that diff"}:
        intent = "show_diff"
    elif normalized in {
        "apply it",
        "apply the diff",
        "apply changes",
        "apply the changes",
        "approve the diff",
        "approve apply back",
        "approve apply-back",
    }:
        intent = "approve_apply_back"
    elif normalized in {"deny apply back", "deny apply-back", "deny the diff", "do not apply it"}:
        intent = "deny_apply_back"
    elif "apply-back" in normalized or "apply back" in normalized:
        intent = "apply_back_review"
    else:
        intent = "unsupported"
    return {"schema_version": CHAT_INTENT_SCHEMA_VERSION, "ok": True, "input": text, "intent": intent}


def _emit_progress(
    progress_callback: Callable[[dict[str, Any]], None] | None,
    kind: str,
    content: str,
) -> None:
    if progress_callback is not None and content.strip():
        progress_callback({"kind": kind, "content": content})


def _handle_slash(
    raw: str,
    project_root: Path,
    state: ChatSessionState,
    *,
    chat_model: ChatModel | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    parts = raw.split()
    command = parts[0][1:]
    arg = parts[1] if len(parts) > 1 else None
    tail = raw.partition(" ")[2].strip()
    _emit_progress(progress_callback, "procedure", "Ran slash command routing")
    _emit_progress(progress_callback, "procedure", f"- command: /{command}")
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
                "/capabilities - list Harness capability catalog entries",
                "/adapters - list registered adapters including repo_planning",
                "/memory - list explicit local memory records",
                "/remember <text> - save a project-scoped operator memory note",
                "/forget <memory_id> - forget a memory record",
                "/progress [objective_id] - show read-only orchestration progress",
                "/task <id> - show task details",
                "/run <id> - show run manifest summary",
                "/artifact <id> - show artifact metadata",
                "/lease [id] - inspect a lease",
                "/execute [lease_id] - prepare registered adapter dispatch",
                "/plan [request] - ask the assistant for a plan or show pending/latest orchestration plan",
                "/act <request> - let the assistant inspect and propose Harness-backed action contracts",
                "/diff - show latest isolated diff artifact or git diff",
                "/test [command] - propose a sandboxed test run through an action contract",
                "/apply [approve|deny|keep] - review/apply inspected isolated changes",
                "/revert - prepare a Harness revert action contract for pending isolated state",
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
    if command == "capabilities":
        return _capabilities_response(project_root)
    if command == "adapters":
        return _adapters_response(project_root)
    if command == "memory":
        return _memory_response(project_root)
    if command == "remember":
        note = raw.partition(" ")[2].strip()
        return _remember_response(project_root, note)
    if command == "forget":
        return _forget_memory_response(project_root, arg)
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
        if tail:
            return _model_chat_response(
                "Plan this Harness-backed change. Inspect read-only context as needed and propose an action "
                f"contract only if the next step needs side effects:\n{tail}",
                project_root,
                state,
                chat_model=chat_model,
                mode_override="plan",
                progress_callback=progress_callback,
            )
        return _plan_response(state)
    if command == "act":
        if not tail:
            return _response(
                "act_needs_request",
                "Act Mode",
                [
                    "Usage: /act <request>",
                    "I will inspect with read-only tools and propose Harness action contracts for side effects.",
                ],
                ok=False,
            )
        return _model_chat_response(
            "Act on this request through Harness. Use read-only tools autonomously first when useful. "
            "For side effects, request a Harness action contract instead of claiming completion:\n"
            f"{tail}",
            project_root,
            state,
            chat_model=chat_model,
            mode_override="act",
            progress_callback=progress_callback,
        )
    if command == "diff":
        return _artifact_response(project_root, state.latest_diff_artifact) if state.latest_diff_artifact else _diff_response(project_root)
    if command == "test":
        return _action_contract_response(
            project_root,
            state,
            ChatToolRequest(
                type="harness.tool_request/v1",
                tool="run_tests",
                arguments={"suggested_command": tail or "pytest -q", "scope": "chat"},
            ),
        )
    if command == "apply":
        choice = arg if arg in {"approve", "deny", "keep"} else None
        return _apply_back_review_response(project_root, state, choice=choice)
    if command == "revert":
        return _action_contract_response(
            project_root,
            state,
            ChatToolRequest(
                type="harness.tool_request/v1",
                tool="revert_pending_change",
                arguments={"goal": tail or "revert pending isolated/apply-back state"},
            ),
        )
    if command == "stop":
        state.stop_requested = True
        return _response("stop_requested", "Stop Requested", ["Foreground orchestration will stop at the next boundary."], ok=True)
    if command == "apply-back":
        choice = arg if arg in {"approve", "deny", "keep"} else None
        return _apply_back_review_response(project_root, state, choice=choice)
    if command == "progress":
        return _orchestration_progress_response(project_root, arg or state.latest_objective_id, state)
    if command == "reset":
        state.reset()
        return _response("reset", "Session Reset", ["Session-local references were cleared."], ok=True)
    return _response("unknown", "Unknown Command", [f"No chat command matched {raw}.", "Type /help."])


def _handle_intent(
    raw: str,
    project_root: Path,
    state: ChatSessionState,
    *,
    chat_model: ChatModel | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    managed_action_response = _maybe_run_managed_action(raw, project_root, state)
    if managed_action_response is not None:
        return managed_action_response
    intent = route_chat_intent(raw)["intent"]
    _emit_progress(progress_callback, "procedure", "Ran intent routing")
    _emit_progress(progress_callback, "procedure", f"- intent: {intent}")
    if intent == "init_project":
        return _init_response(project_root, state)
    if intent == "mode_codex_like":
        return _mode_response("codex-like", state)
    if intent == "mode_normal":
        return _mode_response("normal", state)
    if intent == "show_tasks":
        return _tasks_response(project_root)
    if intent == "show_runs":
        return _runs_response(project_root, state)
    if intent == "show_last_result":
        return _last_result_response(project_root, state)
    if intent == "show_adapters":
        return _adapters_response(project_root)
    if intent == "show_capabilities":
        return _capabilities_response(project_root)
    if intent == "show_memory":
        return _memory_response(project_root)
    if intent == "show_progress":
        return _orchestration_progress_response(project_root, state.latest_objective_id, state)
    if intent == "show_blocked":
        return _blocked_response(project_root)
    if intent == "recommend_next":
        return _recommend_next_response(project_root, state)
    if intent == "show_status":
        return _status_response(project_root, state)
    if intent == "continue_workflow":
        return _continue_response(project_root, state)
    if intent == "stop_workflow":
        state.stop_requested = True
        return _response("stop_requested", "Stop Requested", ["Foreground orchestration will stop at the next boundary."], ok=True)
    if intent == "repo_summary":
        return _draft_response(project_root, state, _draft_from_template(template_for_intent("repo_summary", raw, project_root)))
    if intent == "repo_planning":
        return _draft_response(project_root, state, _draft_from_template(template_for_intent("repo_planning", raw, project_root)))
    if intent == "coding_fix":
        return _orchestration_draft_response(
            project_root,
            state,
            _orchestration_from_template(template_for_intent("coding_fix", raw, project_root), state),
        )
    if intent == "draft_orchestration":
        return _orchestration_draft_response(project_root, state, _draft_orchestration(project_root, state, raw))
    if intent == "draft_dry_run":
        return _draft_response(project_root, state, _draft_dry_run(project_root))
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
    if intent == "approve_apply_back":
        return _apply_back_review_response(project_root, state, choice="approve")
    if intent == "deny_apply_back":
        return _apply_back_review_response(project_root, state, choice="deny")
    return _model_chat_response(raw, project_root, state, chat_model=chat_model, progress_callback=progress_callback)


def _deterministic_chat_guidance(raw: str, project_root: Path, state: ChatSessionState) -> dict[str, Any]:
    lines = [
        "I can inspect local Harness state and prepare explicit actions.",
        "I do not call Codex, Docker, shell, providers, or model backends directly from chat.",
        "Try: 'summarize this repo', 'fix this bug', 'show capabilities', 'what should I do next?', or /help.",
    ]
    if (
        state.pending_draft
        or state.pending_orchestration
        or state.pending_execute_lease_id
        or state.pending_action_contract
        or state.pending_hosted_approval
    ):
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
            "equivalent_commands": ["/help", "/home", "/capabilities", "/adapters"],
        },
    )


def _maybe_run_managed_action(
    raw: str,
    project_root: Path,
    state: ChatSessionState,
) -> dict[str, Any] | None:
    route = route_managed_action(raw, project_root)
    decision = decide_managed_action(route, project_root)
    if decision.status == ManagedActionDecisionStatus.UNSUPPORTED:
        return None
    if not _is_initialized(project_root):
        return _uninitialized_response(project_root)
    if decision.status == ManagedActionDecisionStatus.DENIED:
        return _response(
            "managed_action_denied",
            "Action Denied",
            decision.reasons,
            ok=False,
            extra={"route": route.model_dump(mode="json"), "decision": decision.model_dump(mode="json")},
        )
    if decision.status == ManagedActionDecisionStatus.APPROVAL_REQUIRED:
        return _response(
            "managed_action_approval_required",
            "Approval Required",
            [
                "Approval required before Harness can run this action.",
                *decision.reasons,
            ],
            ok=False,
            extra={"route": route.model_dump(mode="json"), "decision": decision.model_dump(mode="json")},
        )
    store = _require_store(project_root)
    try:
        result = execute_managed_action(project_root, route, decision, store)
    except ValueError as exc:
        return _response(
            "managed_action_failed",
            "Action Failed",
            [str(exc)],
            ok=False,
            extra={"route": route.model_dump(mode="json"), "decision": decision.model_dump(mode="json")},
        )
    state.latest_run_id = result.run_id
    state.progress.append(f"managed action: {route.intent} run={result.run_id}")
    return _managed_action_response(result)


def _managed_action_response(result: ManagedActionResult) -> dict[str, Any]:
    manifest_path = result.manifest_path or (result.report_path.parent / "manifest.json" if result.report_path else None)
    lines = [result.message]
    if result.run_id:
        lines.append(f"Run: {result.run_id}")
    if result.report_path:
        lines.append(f"Report: {result.report_path}")
    if manifest_path:
        lines.append(f"Manifest: {manifest_path}")
    return _response(
        "self_managed_local_action",
        "Done",
        lines,
        ok=result.ok,
        extra=result.model_dump(mode="json"),
    )


def _model_chat_response(
    raw: str,
    project_root: Path,
    state: ChatSessionState,
    *,
    chat_model: ChatModel | None = None,
    mode_override: str | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    context_payload = chat_context(project_root)
    context_manifest = pack_chat_context(project_root)
    _emit_progress(progress_callback, "procedure", "Explored")
    for line in _context_manifest_progress_lines(context_manifest):
        _emit_progress(progress_callback, "procedure", line)
    chat_ctx = ChatContext(
        project_root=str(project_root),
        model_profile=context_payload["chat"]["default_model_profile"],
        mode=mode_override or ("codex-like" if state.codex_like_mode else "normal"),
        context_blocks=[block.to_payload() for block in context_manifest.blocks],
        safety_boundaries=list(context_payload["safety_boundaries"]),
    )
    messages = _model_messages(raw, state, chat_ctx)
    tool_results: list[dict[str, Any]] = []
    try:
        model = chat_model or build_default_chat_model(project_root)
        _emit_progress(progress_callback, "procedure", "Ran model turn")
        model_response = _complete_model_turn(model, messages, chat_ctx, progress_callback)
        for _index in range(MAX_CHAT_TOOL_CALLS):
            tool_request = parse_tool_request(model_response.content)
            if tool_request is None:
                break
            _emit_progress(progress_callback, "procedure", f"Ran {tool_request.tool}")
            tool_result = run_chat_tool(tool_request, default_chat_tool_context(project_root))
            if tool_result.error_type == "action_contract_required":
                return _action_contract_response(project_root, state, tool_request)
            if tool_result.error_type == "unknown_tool":
                return _response(
                    "action_contract_rejected",
                    "Action Contract Rejected",
                    [tool_result.content],
                    ok=False,
                    extra={
                        "tool_request": {
                            "tool": tool_request.tool,
                            "arguments": sanitize_for_logging(tool_request.arguments),
                        },
                        "error_type": tool_result.error_type,
                    },
                )
            tool_results.append(
                {
                    "tool": tool_result.tool,
                    "ok": tool_result.ok,
                    "error_type": tool_result.error_type,
                }
            )
            if progress_callback is not None:
                progress_callback(
                    {
                        "kind": "procedure",
                        "content": f"- {tool_result.tool}: {'ok' if tool_result.ok else tool_result.error_type or 'failed'}",
                    }
                )
            messages.append(ChatMessage(role="assistant", content=model_response.content))
            messages.append(ChatMessage(role="user", content=f"Harness tool result:\n{tool_result.to_message()}"))
            _emit_progress(progress_callback, "procedure", "Ran model turn")
            model_response = _complete_model_turn(model, messages, chat_ctx, progress_callback)
    except LocalEndpointUnavailable as exc:
        return _local_model_unavailable_response(project_root, exc)
    content = str(sanitize_for_logging(model_response.content)).strip()
    if not content:
        content = "The local chat model returned an empty response."
    fallback_request = _fallback_action_request_for_user_intent(raw)
    if fallback_request is not None and _model_missed_side_effect_request(content):
        return _action_contract_response(project_root, state, fallback_request)
    return _response(
        "llm_chat",
        "Assistant",
        content.splitlines(),
        ok=True,
        extra={
            "model_profile": chat_ctx.model_profile,
            "mode": chat_ctx.mode,
            "hosted_fallback": False,
            "context_manifest": {
                "blocks": [
                    {
                        "kind": block.kind,
                        "title": block.title,
                        "source": block.source,
                        "token_estimate": block.token_estimate,
                        "truncated": block.truncated,
                    }
                    for block in context_manifest.blocks
                ],
                "blocked_paths": context_manifest.blocked_paths,
                "warnings": context_manifest.warnings,
            },
            "tool_results": tool_results,
            "action_proposals": model_response.action_proposals,
        },
    )


def _context_manifest_progress_lines(context_manifest: Any) -> list[str]:
    lines = [f"- Project: {context_manifest.project_root}"]
    block_labels: list[str] = []
    read_sources: list[str] = []
    for block in context_manifest.blocks:
        if block.source:
            read_sources.append(str(block.source))
        else:
            block_labels.append(str(block.kind))
    if block_labels:
        lines.append(f"- Context blocks: {', '.join(block_labels[:8])}")
    if read_sources:
        lines.append(f"- Read {', '.join(read_sources[:6])}")
    if context_manifest.blocked_paths:
        lines.append(f"- Blocked paths: {len(context_manifest.blocked_paths)}")
    if context_manifest.warnings:
        lines.append(f"- Warnings: {', '.join(context_manifest.warnings[:3])}")
    return lines


def _complete_model_turn(
    model: ChatModel,
    messages: list[ChatMessage],
    chat_ctx: ChatContext,
    progress_callback: Callable[[dict[str, Any]], None] | None,
) -> Any:
    if progress_callback is None:
        return model.complete(messages, chat_ctx)
    chunks: list[str] = []
    action_proposals: list[dict[str, Any]] = []
    saw_delta = False
    for delta in model.stream(messages, chat_ctx):
        saw_delta = True
        content = str(sanitize_for_logging(delta.content))
        kind = getattr(delta, "kind", "content")
        if kind == "content":
            chunks.append(content)
        if progress_callback is not None and content.strip():
            progress_callback({"kind": kind, "content": content})
    if not saw_delta:
        return model.complete(messages, chat_ctx)
    return ChatResponse(content="".join(chunks), action_proposals=action_proposals)


def _model_messages(raw: str, state: ChatSessionState, context: ChatContext) -> list[ChatMessage]:
    messages = [
        ChatMessage(
            role="system",
            content=(
                "You are the Harness terminal coding and research assistant. "
                "The user speaks in intentions. Answer naturally and use the provided Harness context. "
                "You are not limited to read-only conversation. Read-only tools run automatically inside the chat "
                "loop. Side-effecting tools are also available, but Harness converts them into validated action "
                "contracts and asks the user for confirmation before execution. Do not claim to mutate files, run "
                "tests, apply patches, or create records directly; request the appropriate Harness tool instead. "
                "When a tool is needed, respond with exactly one JSON object of type harness.tool_request/v1 using "
                "one of the listed tools. After Harness returns a read-only tool result, answer the user normally. "
                "If the user asks to edit, create, delete, test, apply, approve, orchestrate, or otherwise do work, "
                "emit the gated tool request rather than saying chat is read-only."
            ),
        )
    ]
    if context.mode == "act":
        messages.append(
            ChatMessage(
                role="system",
                content=(
                    "Act mode is enabled. You may run a bounded read-only tool loop to inspect context. "
                    "Any side-effecting step must be emitted as a harness.tool_request/v1 for a gated Harness "
                    "action contract. Do not say edits, tests, or apply-back are complete until Harness returns evidence."
                ),
            )
        )
    elif context.mode == "plan":
        messages.append(
            ChatMessage(
                role="system",
                content=(
                    "Plan mode is enabled. Produce a concrete implementation plan grounded in Harness context. "
                    "If the plan should become work, emit a gated Harness action request rather than relying on "
                    "deterministic intent routing."
                ),
            )
        )
    messages.append(
        ChatMessage(
            role="system",
            content="Available Harness chat tools. Tools with risk=read can run in the chat loop; other tools become action contracts:\n"
            + json.dumps(chat_tool_specs_payload(), sort_keys=True, default=str),
        )
    )
    if context.context_blocks:
        context_text = "\n\n".join(f"{block['title']}:\n{block['content']}" for block in context.context_blocks)
        messages.append(ChatMessage(role="system", content=f"Current Harness context:\n{context_text}"))
    for item in state.transcript[-12:]:
        role = item.get("role")
        if role == "user":
            messages.append(ChatMessage(role="user", content=str(item.get("content", ""))))
        elif role == "assistant":
            lines = item.get("lines", [])
            if isinstance(lines, list):
                messages.append(ChatMessage(role="assistant", content="\n".join(str(line) for line in lines)))
    messages.append(ChatMessage(role="user", content=raw))
    return messages


def _local_model_unavailable_response(project_root: Path, exc: Exception) -> dict[str, Any]:
    return _response(
        "chat_model_unavailable",
        "Chat Model Unavailable",
        [
            "The configured chat model is unavailable.",
            str(exc),
            "Fix the configured chat backend, then retry. Harness does not fall back to paid hosted chat automatically.",
        ],
        ok=False,
        extra={
            "project_root": str(project_root),
            "model_profile": "configured_chat_model",
            "hosted_fallback": False,
        },
    )


def _fallback_action_request_for_user_intent(raw: str) -> ChatToolRequest | None:
    normalized = _normalize(raw)
    if not normalized:
        return None
    if _looks_like_test_request(normalized):
        return ChatToolRequest(
            type="harness.tool_request/v1",
            tool="run_tests",
            arguments={"suggested_command": _test_command_from_user_text(raw), "scope": "chat"},
        )
    if _looks_like_apply_request(normalized):
        return ChatToolRequest(type="harness.tool_request/v1", tool="apply_back", arguments={"goal": raw})
    if _looks_like_repo_mutation_request(normalized):
        return ChatToolRequest(type="harness.tool_request/v1", tool="edit_isolated", arguments={"goal": raw})
    return None


def _looks_like_repo_mutation_request(normalized: str) -> bool:
    mutation_verbs = {
        "add",
        "apply",
        "build",
        "change",
        "create",
        "delete",
        "edit",
        "fix",
        "implement",
        "modify",
        "patch",
        "remove",
        "rename",
        "update",
        "write",
    }
    words = set(normalized.split())
    if not words.intersection(mutation_verbs):
        return False
    return any(
        marker in normalized
        for marker in (
            " file",
            " repo",
            " repository",
            " code",
            " test",
            " bug",
            " failing",
            " implementation",
            " feature",
            " docs",
            " readme",
        )
    )


def _looks_like_test_request(normalized: str) -> bool:
    return normalized in {"test", "run tests", "run the tests"} or (
        "test" in normalized and any(verb in normalized.split() for verb in {"run", "execute", "start"})
    )


def _looks_like_apply_request(normalized: str) -> bool:
    return normalized in {"apply it", "apply the diff", "apply changes", "apply the changes"} or "apply back" in normalized


def _test_command_from_user_text(raw: str) -> str:
    stripped = raw.strip()
    if stripped.startswith("pytest"):
        return stripped
    return "pytest -q"


def _model_missed_side_effect_request(content: str) -> bool:
    normalized = _normalize(content)
    if not normalized:
        return True
    refusal_markers = (
        "read only",
        "read-only",
        "can't",
        "cannot",
        "can not",
        "i need",
        "need one detail",
        "would propose",
        "i'd propose",
        "should be confirmed",
    )
    return any(marker in normalized for marker in refusal_markers)


def _diff_response(project_root: Path) -> dict[str, Any]:
    try:
        diff_stat = subprocess.run(
            ["git", "-C", str(project_root), "diff", "--stat"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        diff_patch = subprocess.run(
            ["git", "-C", str(project_root), "diff", "--", "."],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _response("diff_unavailable", "Diff Unavailable", [str(exc)], ok=False)
    stat = str(sanitize_for_logging(diff_stat.stdout.strip()))
    patch = str(sanitize_for_logging(diff_patch.stdout.strip()))
    if not stat and not patch:
        return _response("diff", "Diff", ["No current git diff and no latest isolated diff artifact is selected."], ok=True)
    lines = []
    if stat:
        lines.extend(["Git diff stat:", *stat.splitlines()])
    if patch:
        preview = patch[:6000]
        if len(patch) > len(preview):
            preview += "\n[diff truncated]"
        lines.extend(["Git diff:", *preview.splitlines()])
    return _response("diff", "Diff", lines, ok=True)


def _action_contract_response(project_root: Path, state: ChatSessionState, tool_request: Any) -> dict[str, Any]:
    try:
        contract = contract_from_tool_request(tool_request, project_root=project_root)
    except ValueError as exc:
        return _response(
            "action_contract_rejected",
            "Action Contract Rejected",
            [str(exc)],
            ok=False,
        )
    autonomy_decision = _evaluate_action_contract_autonomy(project_root, state, contract)
    if state.autonomy_profile_id != "manual":
        decision_evidence = _record_autonomy_decision(project_root, contract, autonomy_decision) if _is_initialized(project_root) else None
        if autonomy_decision.status == AutonomyDecisionStatus.AUTO_ALLOWED:
            if not _is_initialized(project_root):
                return _uninitialized_response(project_root)
            approval = _record_autonomous_approval(project_root, contract, autonomy_decision)
            response = _execute_action_contract(project_root, state, contract, prepare_required_approvals=False)
            response["autonomy_decision"] = autonomy_decision.model_dump(mode="json")
            if decision_evidence is not None:
                response["autonomy_decision_evidence"] = decision_evidence
            response["autonomous_approval"] = approval.model_dump(mode="json")
            outcome = _record_autonomous_outcome(project_root, contract, autonomy_decision, response)
            _record_run_autonomy_event(project_root, decision_evidence, approval, outcome)
            response["autonomous_outcome"] = outcome
            response["lines"] = [
                f"Autonomy profile {state.autonomy_profile_id} auto-approved this action contract.",
                *response.get("lines", []),
            ]
            return response
        if autonomy_decision.status in {
            AutonomyDecisionStatus.DENIED,
            AutonomyDecisionStatus.POLICY_MISMATCH,
            AutonomyDecisionStatus.BUDGET_EXCEEDED,
        }:
            state.pending_action_contract = None
            return _response(
                "action_contract_denied",
                "Action Contract Denied",
                [
                    f"Autonomy profile: {state.autonomy_profile_id}",
                    f"Tool: {contract.tool}",
                    f"Decision: {autonomy_decision.status.value}",
                    *autonomy_decision.reasons,
                ],
                ok=False,
                extra={
                    "contract": contract.to_payload(),
                    "autonomy_decision": autonomy_decision.model_dump(mode="json"),
                    "autonomy_decision_evidence": decision_evidence,
                },
            )
    state.pending_action_contract = contract
    lines = [
        "Ready to manage this through Harness.",
        "Next: type yes or /confirm to approve this contract, or no to cancel.",
        f"Action: {contract.summary}",
        f"Tool: {contract.tool}",
        f"Risk: {contract.risk}",
        f"Required confirmations: {', '.join(contract.required_confirmations) or 'none'}",
        f"Required approvals: {', '.join(contract.required_approvals) or 'none'}",
        "Execution plan:",
    ]
    lines.extend(f"- {step}" for step in contract.execution_plan)
    if not _is_initialized(project_root):
        lines.append("This project must be initialized before confirmed actions can create Harness records.")
    return _response(
        "action_contract",
        "Action Contract",
        lines,
        ok=True,
        extra={"contract": contract.to_payload(), "autonomy_decision": autonomy_decision.model_dump(mode="json")},
    )


def _execute_action_contract(
    project_root: Path,
    state: ChatSessionState,
    contract: ActionContract,
    *,
    prepare_required_approvals: bool = True,
) -> dict[str, Any]:
    approval_lines = _ensure_contract_required_approvals(project_root, contract) if prepare_required_approvals else []
    if contract.tool == "create_objective":
        response = _confirm_create_objective_contract(project_root, state, contract)
    elif contract.tool == "create_task":
        response = _confirm_create_task_contract(project_root, state, contract)
    elif contract.tool == "create_task_graph":
        response = _confirm_create_task_graph_contract(project_root, state, contract)
    elif contract.tool == "remember":
        response = _confirm_remember_contract(project_root, contract)
    elif contract.tool == "edit_isolated":
        response = _confirm_edit_isolated_contract(project_root, state, contract)
    elif contract.tool == "dispatch_registered_adapter":
        response = _confirm_dispatch_contract(project_root, state, contract)
    elif contract.tool == "run_tests":
        response = _confirm_run_tests_contract(project_root, state, contract)
    elif contract.tool == "apply_back":
        response = _apply_back_review_response(project_root, state, choice="approve")
    elif contract.tool == "deny_apply_back":
        response = _apply_back_review_response(project_root, state, choice="deny")
    else:
        response = _response(
            "action_contract_confirmed_not_executed",
            "Action Contract Confirmed",
            [
                f"Confirmed contract: {contract.id}",
                f"Tool: {contract.tool}",
                "This side-effecting tool is validated, but execution is not wired in this slice yet.",
                "Next implementation step: route this contract through the corresponding Harness control-plane executor.",
            ],
            ok=False,
            extra={"contract": contract.to_payload()},
        )
    if approval_lines:
        response["lines"] = [*approval_lines, *response.get("lines", [])]
        response["auto_approvals"] = approval_lines
    return response


def _ensure_contract_required_approvals(project_root: Path, contract: ActionContract) -> list[str]:
    if "hosted_provider_codex" not in contract.required_approvals:
        return []
    task_types = _hosted_codex_task_types_for_contract(contract)
    approvals = ApprovalStore(project_root)
    missing = [
        task_type
        for task_type in task_types
        if approvals.find_valid("codex_cli", "hosted_provider", task_type) is None
    ]
    if not missing:
        return []
    approvals.add(
        backend="codex_cli",
        data_boundary="hosted_provider",
        task_types=task_types,
        duration_days=1,
        reason=f"Created from confirmed Harness action contract {contract.id}.",
    )
    return [
        "Prepared required hosted-provider Codex approval for this confirmed action contract.",
        "This permits scoped Codex planning/edit execution only; apply-back still requires a separate approval.",
    ]


def _hosted_codex_task_types_for_contract(contract: ActionContract) -> list[str]:
    task_types = {"codex_code_edit", "repo_planning"}
    if contract.tool == "dispatch_registered_adapter":
        task_type = contract.normalized_arguments.get("task_type")
        if isinstance(task_type, str) and task_type:
            task_types.add(task_type)
    if contract.tool == "create_task_graph":
        for task in contract.normalized_arguments.get("tasks") or []:
            if isinstance(task, dict) and isinstance(task.get("task_type"), str):
                task_types.add(task["task_type"])
    return sorted(task_types)


def _evaluate_action_contract_autonomy(
    project_root: Path,
    state: ChatSessionState,
    contract: ActionContract,
) -> AutonomyDecision:
    policy = get_builtin_autonomy_policy(state.autonomy_profile_id)
    request = _autonomy_input_from_contract(project_root, state, contract)
    decision = evaluate_autonomy(policy, request)
    return _apply_adapter_autonomy_metadata(policy.id, request, decision)


def _apply_adapter_autonomy_metadata(
    policy_id: str,
    request: AutonomyEvaluationInput,
    decision: AutonomyDecision,
) -> AutonomyDecision:
    if request.adapter_id is None:
        if request.tool_name == "dispatch_registered_adapter":
            return decision.model_copy(
                update={
                    "status": AutonomyDecisionStatus.DENIED,
                    "reasons": [*decision.reasons, "adapter dispatch requires a registered adapter id"],
                    "requires_human": False,
                }
            )
        return decision

    descriptor = _execution_adapter_descriptor(request.adapter_id)
    if descriptor is None:
        return decision.model_copy(
            update={
                "status": AutonomyDecisionStatus.DENIED,
                "reasons": [*decision.reasons, f"adapter is not registered: {request.adapter_id}"],
                "requires_human": False,
            }
        )

    if decision.status in {
        AutonomyDecisionStatus.DENIED,
        AutonomyDecisionStatus.POLICY_MISMATCH,
        AutonomyDecisionStatus.BUDGET_EXCEEDED,
    }:
        return decision.model_copy(
            update={
                "reasons": [
                    *decision.reasons,
                    f"adapter autonomy default is {descriptor.autonomy_default}: {descriptor.id}",
                ]
            }
        )

    if descriptor.autonomy_default == "forbidden":
        return decision.model_copy(
            update={
                "status": AutonomyDecisionStatus.DENIED,
                "reasons": [*decision.reasons, f"adapter forbids autonomous dispatch: {descriptor.id}"],
                "requires_human": False,
            }
        )

    if descriptor.required_autonomy_scopes and policy_id not in descriptor.required_autonomy_scopes:
        return decision.model_copy(
            update={
                "status": AutonomyDecisionStatus.APPROVAL_REQUIRED,
                "reasons": [
                    *decision.reasons,
                    f"adapter requires an autonomy scope in {sorted(descriptor.required_autonomy_scopes)}",
                ],
                "requires_human": True,
            }
        )

    if (
        descriptor.autonomy_default == "approval_required"
        and decision.status == AutonomyDecisionStatus.AUTO_ALLOWED
        and descriptor.required_approvals
        and not request.has_scoped_approval
    ):
        return decision.model_copy(
            update={
                "status": AutonomyDecisionStatus.APPROVAL_REQUIRED,
                "reasons": [
                    *decision.reasons,
                    f"adapter requires scoped approval before autonomous dispatch: {descriptor.id}",
                ],
                "requires_human": True,
            }
        )

    return decision.model_copy(
        update={
            "reasons": [
                *decision.reasons,
                f"adapter autonomy default is {descriptor.autonomy_default}: {descriptor.id}",
            ]
        }
    )


def _autonomy_input_from_contract(
    project_root: Path,
    state: ChatSessionState,
    contract: ActionContract,
) -> AutonomyEvaluationInput:
    adapter_id = _adapter_id_for_contract(project_root, state, contract)
    task_type = _task_type_for_contract(project_root, state, contract)
    boundary = _boundary_for_contract(contract, adapter_id)
    return AutonomyEvaluationInput(
        tool_name=contract.tool,
        risk=contract.risk,
        boundary=boundary,
        adapter_id=adapter_id,
        task_type=task_type,
        has_scoped_approval=_has_scoped_approval_for_contract(
            project_root,
            state,
            contract,
            adapter_id=adapter_id,
            task_type=task_type,
        ),
        would_mutate_active_repo=contract.tool == "apply_back",
        requires_network=False,
        requires_paid_or_hosted_boundary=boundary in {"hosted_provider", "hosted_provider_codex"},
        requires_sandbox=contract.tool in {"dispatch_registered_adapter", "edit_isolated", "run_tests"},
        sandbox_enforced=contract.tool in {"dispatch_registered_adapter", "edit_isolated"},
        adapter_breaker_open=_adapter_breaker_open(project_root, adapter_id),
        idempotency_key=_contract_idempotency_key(contract),
        evidence_contract=",".join(contract.evidence_plan) or "chat_action_contract",
    )


def _execution_adapter_descriptor(adapter_id: str):
    for descriptor in list_execution_adapter_descriptors():
        if descriptor.id == adapter_id:
            return descriptor
    return None


def _adapter_breaker_open(project_root: Path, adapter_id: str | None) -> bool:
    if adapter_id is None or not _is_initialized(project_root):
        return False
    try:
        return SQLiteStore(project_root).adapter_breaker_state(adapter_id).status.value == "open"
    except Exception:
        return True


def _adapter_id_for_contract(project_root: Path, state: ChatSessionState, contract: ActionContract) -> str | None:
    if contract.tool == "dispatch_registered_adapter":
        adapter_id = contract.normalized_arguments.get("adapter_id") or contract.normalized_arguments.get("execution_adapter")
        if adapter_id:
            return str(adapter_id)
        task = _task_for_latest_lease(project_root, state)
        if task is not None:
            adapter = task.metadata.get("execution_adapter")
            return str(adapter) if adapter else None
        return None
    if contract.tool == "edit_isolated":
        return "codex_isolated_edit"
    if contract.tool == "create_task":
        return str(contract.normalized_arguments.get("execution_adapter") or "")
    return None


def _task_type_for_contract(project_root: Path, state: ChatSessionState, contract: ActionContract) -> str | None:
    if contract.tool in {"create_task", "dispatch_registered_adapter"}:
        task_type = contract.normalized_arguments.get("task_type")
        if task_type:
            return str(task_type)
        if contract.tool == "dispatch_registered_adapter":
            task = _task_for_latest_lease(project_root, state)
            if task is not None:
                lease_task_type = task.metadata.get("task_type")
                return str(lease_task_type) if lease_task_type else None
        return None
    if contract.tool == "edit_isolated":
        return "codex_code_edit"
    if contract.tool == "create_task_graph":
        task_types = {
            str(task.get("task_type"))
            for task in contract.normalized_arguments.get("tasks") or []
            if isinstance(task, dict) and task.get("task_type")
        }
        if len(task_types) == 1:
            return sorted(task_types)[0]
        return None
    return None


def _contract_idempotency_key(contract: ActionContract) -> str:
    stable = json.dumps(
        {
            "schema_version": contract.schema_version,
            "tool": contract.tool,
            "arguments": sanitize_for_logging(contract.normalized_arguments),
        },
        sort_keys=True,
        default=str,
    )
    return f"chat_contract:{hashlib.sha256(stable.encode('utf-8')).hexdigest()[:24]}"


def _task_for_latest_lease(project_root: Path, state: ChatSessionState) -> TaskRecord | None:
    if not state.latest_lease_id or not _is_initialized(project_root):
        return None
    store = SQLiteStore(project_root)
    try:
        lease = store.get_task_lease(state.latest_lease_id)
        return store.get_task(lease.task_id)
    except KeyError:
        return None


def _boundary_for_contract(contract: ActionContract, adapter_id: str | None) -> str:
    if contract.tool == "apply_back":
        return "active_repo_apply_back"
    if contract.tool == "dispatch_registered_adapter":
        if adapter_id in {"read_only_summary", "repo_planning", "codex_isolated_edit"}:
            return "hosted_provider_codex"
        return "local_artifact"
    if "hosted_provider_codex" in contract.required_approvals or adapter_id in {
        "read_only_summary",
        "repo_planning",
        "codex_isolated_edit",
    }:
        return "hosted_provider_codex"
    if contract.tool == "run_tests":
        return "docker_execution"
    if contract.risk == "control_plane_write":
        return "local_control_plane"
    return "local_artifact"


def _has_scoped_approval_for_contract(
    project_root: Path,
    state: ChatSessionState,
    contract: ActionContract,
    *,
    adapter_id: str | None = None,
    task_type: str | None = None,
) -> bool:
    if "hosted_provider_codex" not in contract.required_approvals:
        return False
    if not _is_initialized(project_root):
        return False
    if contract.tool == "dispatch_registered_adapter" and task_type:
        task_types = [task_type]
    else:
        task_types = _hosted_codex_task_types_for_contract(contract)
    approvals = ApprovalStore(project_root)
    task = _task_for_latest_lease(project_root, state) if contract.tool == "dispatch_registered_adapter" else None
    return all(
        approvals.find_valid(
            "codex_cli",
            "hosted_provider",
            task_type,
            adapter_id=adapter_id,
            workbench_id=task.workbench_id if task else contract.normalized_arguments.get("workbench_id"),
            objective_id=task.objective_id if task else contract.normalized_arguments.get("objective_id"),
            autonomy_scope=state.autonomy_profile_id,
            strict_scope=state.autonomy_profile_id != "manual",
        )
        is not None
        for task_type in task_types
    )


def _record_autonomous_approval(
    project_root: Path,
    contract: ActionContract,
    decision: AutonomyDecision,
) -> AutonomousApprovalRecord:
    record = AutonomousApprovalRecord(
        id=f"auto_{uuid.uuid4().hex[:12]}",
        policy_id=decision.policy_id,
        decision_status=decision.status,
        tool_name=contract.tool,
        adapter_id=decision.adapter_id,
        task_type=decision.task_type,
        boundary=decision.boundary or "unknown",
        risk=decision.risk or str(contract.risk),
        reasons=decision.reasons,
    )
    append_jsonl(
        resolve_project_root(project_root) / HARNESS_DIR / "autonomy" / "approvals.jsonl",
        record.to_jsonl_payload(),
    )
    return record


def _record_autonomy_decision(
    project_root: Path,
    contract: ActionContract,
    decision: AutonomyDecision,
) -> dict[str, Any]:
    payload = {
        **decision.model_dump(mode="json"),
        "contract_id": contract.id,
        "contract_schema_version": contract.schema_version,
        "record_id": f"adec_{uuid.uuid4().hex[:12]}",
    }
    append_jsonl(resolve_project_root(project_root) / HARNESS_DIR / "autonomy" / "decisions.jsonl", payload)
    return payload


def _record_autonomous_outcome(
    project_root: Path,
    contract: ActionContract,
    decision: AutonomyDecision,
    response: dict[str, Any],
) -> dict[str, Any]:
    payload = {
        "schema_version": "harness.autonomous_outcome/v1",
        "record_id": f"aout_{uuid.uuid4().hex[:12]}",
        "contract_id": contract.id,
        "tool_name": contract.tool,
        "policy_id": decision.policy_id,
        "decision_status": decision.status.value,
        "adapter_id": decision.adapter_id,
        "task_type": decision.task_type,
        "response_kind": response.get("kind"),
        "ok": bool(response.get("ok")),
        "objective_id": _response_record_id(response, "objective"),
        "task_id": _response_record_id(response, "task"),
        "memory_id": _response_record_id(response, "memory"),
        "run_id": _response_record_id(response, "run"),
        "artifact_ids": _response_artifact_ids(response),
    }
    append_jsonl(resolve_project_root(project_root) / HARNESS_DIR / "autonomy" / "outcomes.jsonl", payload)
    return payload


def _record_run_autonomy_event(
    project_root: Path,
    decision_evidence: dict[str, Any] | None,
    approval: AutonomousApprovalRecord,
    outcome: dict[str, Any],
) -> None:
    run_id = outcome.get("run_id")
    if not run_id:
        return
    store = SQLiteStore(project_root)
    store.append_event(
        str(run_id),
        "info",
        "autonomy_decision",
        "Autonomous approval metadata linked to this run.",
        {
            "autonomy_decision_id": decision_evidence.get("record_id") if decision_evidence else None,
            "autonomous_approval_id": approval.id,
            "autonomous_outcome_id": outcome.get("record_id"),
            "autonomy_policy_id": approval.policy_id,
            "adapter_id": approval.adapter_id,
            "task_type": approval.task_type,
        },
    )
    store.write_run_manifest(str(run_id))


def _response_record_id(response: dict[str, Any], key: str) -> str | None:
    direct = _nested_id(response.get(key))
    if direct is not None:
        return direct
    result = response.get("result")
    if isinstance(result, dict):
        return _nested_id(result.get(key))
    return None


def _response_artifact_ids(response: dict[str, Any]) -> list[str]:
    artifact_ids = [
        str(item.get("id"))
        for item in response.get("artifacts", [])
        if isinstance(item, dict) and item.get("id")
    ]
    result = response.get("result")
    if isinstance(result, dict):
        manifest = result.get("manifest")
        if isinstance(manifest, dict):
            artifact_ids.extend(
                str(item.get("id"))
                for item in manifest.get("artifacts", [])
                if isinstance(item, dict) and item.get("id")
            )
    return sorted(set(artifact_ids))


def _nested_id(value: Any) -> str | None:
    if isinstance(value, dict) and value.get("id"):
        return str(value["id"])
    return None


def _confirm_create_objective_contract(project_root: Path, state: ChatSessionState, contract: ActionContract) -> dict[str, Any]:
    store = _require_store(project_root)
    args = contract.normalized_arguments
    objective = store.create_objective(
        title=str(args["title"]),
        description=str(args.get("description") or ""),
        workbench_id=args.get("workbench_id"),
        metadata={"created_from": "chat_action_contract", "contract_id": contract.id, "tool": contract.tool},
    )
    state.latest_objective_id = objective.id
    return _response(
        "action_contract_executed",
        "Objective Created",
        [f"Objective: {objective.id}", f"Title: {objective.title}", "Next: say what tasks should be added, or ask me to plan the task graph."],
        ok=True,
        extra={"contract": contract.to_payload(), "objective": objective.model_dump(mode="json")},
    )


def _confirm_create_task_contract(project_root: Path, state: ChatSessionState, contract: ActionContract) -> dict[str, Any]:
    store = _require_store(project_root)
    args = contract.normalized_arguments
    task = store.create_task(
        title=str(args["title"]),
        description=str(args.get("description") or ""),
        objective_id=args.get("objective_id"),
        workbench_id=args.get("workbench_id"),
        agent_id=args.get("agent_id"),
        idempotency_key=_contract_idempotency_key(contract),
        metadata={
            "execution_adapter": args["execution_adapter"],
            "task_type": args["task_type"],
            "created_from": "chat_action_contract",
            "contract_id": contract.id,
            "idempotency_key": _contract_idempotency_key(contract),
        },
    )
    state.latest_task_id = task.id
    return _response(
        "action_contract_executed",
        "Task Created",
        [f"Task: {task.id}", f"Adapter: {args['execution_adapter']}", f"Task type: {args['task_type']}", "Next: say 'lease the next task' or ask me to continue."],
        ok=True,
        extra={"contract": contract.to_payload(), "task": task.model_dump(mode="json")},
    )


def _confirm_create_task_graph_contract(project_root: Path, state: ChatSessionState, contract: ActionContract) -> dict[str, Any]:
    store = _require_store(project_root)
    args = contract.normalized_arguments
    objective = store.create_objective(
        title=str(args["goal"]),
        description=str(args["goal"]),
        workbench_id=args.get("workbench_id"),
        metadata={"created_from": "chat_action_contract", "contract_id": contract.id, "tool": contract.tool},
    )
    created_tasks = []
    raw_tasks = [task for task in args.get("tasks") or [] if isinstance(task, dict)]
    for raw_task in raw_tasks:
        if not isinstance(raw_task, dict):
            continue
        depends_on = []
        for index in raw_task.get("depends_on_indexes") or []:
            if isinstance(index, int) and 0 <= index < len(created_tasks):
                depends_on.append(created_tasks[index].id)
        task_contract = contract_from_tool_request(
            ChatToolRequest(
                type="harness.tool_request/v1",
                tool="create_task",
                arguments={**raw_task, "objective_id": objective.id},
            ),
            project_root=project_root,
        )
        task_args = task_contract.normalized_arguments
        task_idempotency_key = _contract_idempotency_key(task_contract)
        created_tasks.append(
            store.create_task(
                title=str(task_args["title"]),
                description=str(task_args.get("description") or ""),
                objective_id=objective.id,
                workbench_id=task_args.get("workbench_id"),
                agent_id=task_args.get("agent_id"),
                priority=int(raw_task.get("priority") or 0),
                depends_on=depends_on,
                idempotency_key=task_idempotency_key,
                metadata={
                    **dict(task_args.get("metadata") or {}),
                    "execution_adapter": task_args["execution_adapter"],
                    "task_type": task_args["task_type"],
                    "created_from": "chat_action_contract",
                    "contract_id": contract.id,
                    "idempotency_key": task_idempotency_key,
                },
            )
        )
    state.latest_objective_id = objective.id
    if created_tasks:
        state.latest_task_id = created_tasks[-1].id
    return _response(
        "action_contract_executed",
        "Task Graph Created",
        [f"Objective: {objective.id}", f"Tasks: {len(created_tasks)}", "Next: ask me to inspect progress or continue execution."],
        ok=True,
        extra={"contract": contract.to_payload(), "objective": objective.model_dump(mode="json"), "tasks": [task.model_dump(mode="json") for task in created_tasks]},
    )


def _confirm_remember_contract(project_root: Path, contract: ActionContract) -> dict[str, Any]:
    note = str(contract.normalized_arguments.get("summary") or contract.normalized_arguments.get("goal") or "").strip()
    if not note:
        return _response("action_contract_invalid", "Missing Memory Note", ["The remember contract did not include a summary."], ok=False)
    response = _remember_response(project_root, note)
    response["contract"] = contract.to_payload()
    return response


def _confirm_edit_isolated_contract(project_root: Path, state: ChatSessionState, contract: ActionContract) -> dict[str, Any]:
    goal = str(contract.normalized_arguments.get("goal") or contract.normalized_arguments.get("summary") or contract.summary)
    template = template_for_intent("coding_fix", goal, project_root)
    draft = _orchestration_from_template(template, state)
    draft.objective_title = str(contract.normalized_arguments.get("title") or draft.objective_title)
    draft.objective_description = goal
    response = _create_and_run_orchestration(project_root, state, draft)
    response["contract"] = contract.to_payload()
    return response


def _confirm_dispatch_contract(project_root: Path, state: ChatSessionState, contract: ActionContract) -> dict[str, Any]:
    lease_id = contract.normalized_arguments.get("lease_id") or contract.normalized_arguments.get("id") or state.latest_lease_id
    if not lease_id:
        return _response(
            "action_contract_missing_lease",
            "Missing Lease",
            ["The dispatch contract needs a lease_id or an existing latest lease."],
            ok=False,
            extra={"contract": contract.to_payload()},
        )
    response = _execute_response(project_root, str(lease_id), state)
    response["contract"] = contract.to_payload()
    return response


class _ChatConfirmedTestApprovalProvider:
    def decide(self, details: str) -> RunTestsDecision:
        return RunTestsDecision(decision="approved", reason="Approved by explicit chat action contract confirmation.")


def _confirm_run_tests_contract(project_root: Path, state: ChatSessionState, contract: ActionContract) -> dict[str, Any]:
    command = _test_command_from_contract(contract)
    if not command:
        return _response(
            "action_contract_invalid",
            "Missing Test Command",
            ["The run_tests contract needs suggested_command or command arguments."],
            ok=False,
            extra={"contract": contract.to_payload()},
        )
    store = _require_store(project_root)
    runner = DockerTestRunner(project_root, load_config(project_root), store, _ChatConfirmedTestApprovalProvider())
    result = runner.run(command)
    state.latest_run_id = str(result.get("run_id") or state.latest_run_id)
    state.progress.append(f"tests run: {state.latest_run_id} status={result.get('status')}")
    return _response(
        "action_contract_executed",
        "Tests Run",
        [
            f"Run: {result.get('run_id')}",
            f"Status: {result.get('status')}",
            f"Approval decision: {result.get('approval_decision')}",
            f"Artifacts: {result.get('artifacts')}",
        ],
        ok=result.get("status") in {"tests_passed", "tests_failed", "tests_timed_out", "execution_denied"},
        extra={"contract": contract.to_payload(), "test_result": result},
    )


def _test_command_from_contract(contract: ActionContract) -> list[str]:
    args = contract.normalized_arguments
    command = args.get("command")
    if isinstance(command, list):
        return [str(part) for part in command]
    if isinstance(command, str):
        return shlex.split(command)
    suggested = args.get("suggested_command")
    if isinstance(suggested, str):
        return shlex.split(suggested)
    return []


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
    if state.pending_action_contract is not None:
        if not _is_initialized(project_root):
            return _uninitialized_response(project_root)
        contract = state.pending_action_contract
        state.pending_action_contract = None
        return _execute_action_contract(project_root, state, contract)
    if state.pending_hosted_approval:
        if not _is_initialized(project_root):
            return _uninitialized_response(project_root)
        ApprovalStore(project_root).add(
            backend="codex_cli",
            data_boundary="hosted_provider",
            task_types=["codex_code_edit", "read_only_repo_summary", "repo_planning"],
            duration_days=1,
            reason="Created from explicit harness chat confirmation.",
        )
        state.pending_hosted_approval = False
        lines = [
            "Created a one-day Codex hosted-boundary approval profile.",
            "It permits scoped Codex execution for read-only summaries, repo planning, and isolated edits.",
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
        required_approvals=[],
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
            f"Interpreted intent: {draft.interpreted_intent}",
            f"Proposed action: {draft.proposed_action}",
            f"Title: {draft.title}",
            f"Adapter: {draft.execution_adapter}",
            f"Task type: {draft.task_type}",
            f"Required approvals: {draft.required_approvals or ['none']}",
            f"Mutates when confirmed: {draft.mutates_when_confirmed}",
            "Safety boundary:",
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
        + f" adapter={task.execution_adapter} task_type={task.task_type}"
        + (f" depends_on={','.join(str(i + 1) for i in task.depends_on_indexes)}" if task.depends_on_indexes else "")
        for idx, task in enumerate(draft.tasks)
    ]
    return _response(
        "orchestration_draft",
        "Orchestration Draft",
        [
            f"Interpreted intent: {draft.interpreted_intent}",
            f"Proposed action: {draft.proposed_action}",
            f"Objective: {draft.objective_title}",
            f"Orchestrator: {draft.orchestrator_id}",
            f"Workbench: {draft.workbench_id}",
            "Tasks:",
            *task_lines,
            f"Required approvals: {draft.required_approvals}",
            "Safety boundary:",
            *[f"- {note}" for note in draft.safety_notes],
            "Equivalent commands:",
            *draft.equivalent_commands,
            draft.confirm_prompt,
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
        interpreted_intent="repo_summary",
        proposed_action="Create one read-only repository summary task.",
        agent_id="repo_inspector",
        workbench_id="coding",
        required_approvals=["hosted_provider_codex"],
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
        interpreted_intent="codex_isolated_edit",
        proposed_action="Create one Codex isolated edit task.",
        agent_id="code_editor",
        workbench_id="coding",
        required_approvals=["hosted_provider_codex"],
        safety_notes=[
            "Codex requires hosted-boundary approval before run creation.",
            "Hosted-boundary approval is not apply-back approval.",
            "Codex edits only an isolated workspace.",
            "Apply-back remains denied by default unless a separate inspected-diff approval path approves it.",
        ],
        equivalent_command=f'harness tasks add --title "Chat Codex isolated edit" --execution-adapter codex_isolated_edit --task-type codex_code_edit --project {project_root} --output json',
    )


def _draft_from_template(template: WorkflowTemplate) -> ChatDraftTask:
    if len(template.tasks) != 1:
        raise ValueError(f"Single-task draft requires one task, got {len(template.tasks)}")
    task = template.tasks[0]
    return ChatDraftTask(
        title=task.title,
        description=task.description,
        execution_adapter=task.execution_adapter,
        task_type=task.task_type,
        interpreted_intent=template.interpreted_intent,
        proposed_action=template.proposed_action,
        agent_id=task.agent_id,
        workbench_id=task.workbench_id,
        required_approvals=template.required_approvals,
        safety_notes=template.safety_boundary,
        equivalent_command=template.equivalent_commands[0] if template.equivalent_commands else "",
        mutates_when_confirmed=True,
    )


def _orchestration_from_template(template: WorkflowTemplate, state: ChatSessionState) -> OrchestratedRunDraft:
    tasks = [
        OrchestratedTaskDraft(
            title=task.title,
            description=task.description,
            agent_id=task.agent_id or "repo_inspector",
            workbench_id=task.workbench_id or "coding",
            execution_adapter=task.execution_adapter,
            task_type=task.task_type,
            depends_on_indexes=list(task.depends_on_indexes),
            priority=task.priority,
            metadata=task.metadata(),
        )
        for task in template.tasks
    ]
    orchestrator_id = _active_orchestrator_id(state)
    workbench_id = tasks[0].workbench_id if tasks else _workbench_for_orchestrator(orchestrator_id)
    return OrchestratedRunDraft(
        objective_title=template.objective_title,
        objective_description=template.objective_description,
        orchestrator_id=orchestrator_id,
        workbench_id=workbench_id,
        tasks=tasks,
        interpreted_intent=template.interpreted_intent,
        proposed_action=template.proposed_action,
        required_approvals=template.required_approvals,
        safety_notes=template.safety_boundary,
        equivalent_commands=template.equivalent_commands,
        confirm_prompt=template.confirm_prompt,
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


def _last_result_response(project_root: Path, state: ChatSessionState) -> dict[str, Any]:
    if not _is_initialized(project_root):
        return _uninitialized_response(project_root)
    store = _require_store(project_root)
    run_id = state.latest_run_id
    if run_id is None:
        runs = store.list_runs()
        run_id = runs[0].id if runs else None
    if run_id is None:
        return _response("last_result_missing", "Last Result", ["No run evidence exists yet."], ok=False)
    try:
        run = store.get_run(run_id)
        manifest = store.build_run_manifest(run_id)
    except KeyError as exc:
        return _response("run_missing", "Run Not Found", [str(exc).strip("'")], ok=False)
    state.latest_run_id = run.id
    if manifest.task_id:
        state.latest_task_id = manifest.task_id
    lines = [
        f"Task: {manifest.task_id or 'none'}",
        f"Status: {run.status}",
        f"Adapter: {_adapter_from_manifest_task(store, manifest.task_id) or 'none'}",
        f"Run: {run.id}",
    ]
    if manifest.artifacts:
        lines.append("Artifacts:")
        for artifact in manifest.artifacts[:6]:
            lines.append(f"- {artifact.kind}: {artifact.path}")
    lines.extend(
        [
            "Next:",
            f"harness show {run.id} --project {project_root} --output json",
            f"harness artifacts list {run.id} --project {project_root}",
        ]
    )
    if manifest.objective_id:
        lines.append(f"harness progress --objective {manifest.objective_id} --project {project_root} --output json")
    return _response(
        "last_result",
        "Last Result",
        lines,
        ok=True,
        extra={"run": run.model_dump(mode="json"), "manifest": manifest.model_dump(mode="json")},
    )


def _adapter_from_manifest_task(store: SQLiteStore, task_id: str | None) -> str | None:
    if not task_id:
        return None
    try:
        task = store.get_task(task_id)
    except KeyError:
        return None
    return str(task.metadata.get("execution_adapter") or "") or None


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


def _capabilities_response(project_root: Path) -> dict[str, Any]:
    catalog = build_capability_catalog(project_root)
    lines = [
        (
            f"{capability.id}: task_types={','.join(capability.supported_task_types) or 'none'} "
            f"readiness={capability.readiness} approvals="
            f"{','.join(capability.required_approvals) if capability.required_approvals else 'none'}"
        )
        for capability in catalog.capabilities
    ]
    return _response(
        "capabilities",
        "Capability Catalog",
        lines or ["No capabilities registered."],
        ok=True,
        extra={"capability_catalog": catalog.model_dump(mode="json")},
    )


def _memory_response(project_root: Path) -> dict[str, Any]:
    if not _is_initialized(project_root):
        return _uninitialized_response(project_root)
    records = _require_store(project_root).list_memory_records()[:10]
    lines = ["No memory records found."] if not records else [
        f"{record.id} [{record.scope_type.value}:{record.scope_id}] {record.summary}"
        for record in records
    ]
    return _response("memory", "Memory", lines, ok=True, extra={"memory": [record.model_dump(mode="json") for record in records]})


def _remember_response(project_root: Path, note: str) -> dict[str, Any]:
    if not _is_initialized(project_root):
        return _uninitialized_response(project_root)
    store = _require_store(project_root)
    try:
        store.initialize()
        record = store.save_memory_note("project", str(resolve_project_root(project_root)), note)
    except ValueError as exc:
        return _response("memory_error", "Memory Error", [str(exc)], ok=False)
    return _response(
        "memory_saved",
        "Memory Saved",
        [
            f"Memory: {record.id}",
            f"Scope: {record.scope_type.value}:{record.scope_id}",
            f"Redaction: {record.redaction_state.value}",
        ],
        ok=True,
        extra={"memory": record.model_dump(mode="json")},
    )


def _forget_memory_response(project_root: Path, memory_id: str | None) -> dict[str, Any]:
    if not _is_initialized(project_root):
        return _uninitialized_response(project_root)
    if not memory_id:
        return _response("memory_error", "Memory Error", ["Provide a memory id to forget."], ok=False)
    try:
        record = _require_store(project_root).forget_memory_record(memory_id)
    except KeyError as exc:
        return _response("memory_error", "Memory Error", [str(exc).strip("'")], ok=False)
    return _response(
        "memory_forgotten",
        "Memory Forgotten",
        [f"Memory: {record.id}", f"Redaction: {record.redaction_state.value}"],
        ok=True,
        extra={"memory": record.model_dump(mode="json")},
    )


def _orchestration_progress_response(
    project_root: Path,
    objective_id: str | None,
    state: ChatSessionState,
) -> dict[str, Any]:
    if objective_id is None:
        return _response(
            "progress",
            "Progress",
            state.progress or ["No objective is selected and no progress events exist in this chat session."],
            ok=True,
            extra={"progress": state.progress, "objective_id": None},
        )
    try:
        progress = build_orchestration_progress(project_root, objective_id)
    except KeyError as exc:
        message = str(exc).strip("'")
        return _response(
            "progress_error",
            "Progress Error",
            [message],
            ok=False,
            extra={
                "progress": {
                    "schema_version": "harness.orchestration_progress/v1",
                    "ok": False,
                    "objective_id": objective_id,
                    "errors": [message],
                }
            },
        )
    lines = [
        f"Objective: {progress.objective_id} | {progress.objective_title}",
        f"Mode: {progress.mode.value}",
        f"Next: {progress.next_action or 'none'}",
    ]
    if progress.active_lease_ids:
        lines.append(f"Active lease: {progress.active_lease_ids[0]}")
    if progress.active_run_ids:
        lines.append(f"Active run: {progress.active_run_ids[0]}")
    for task in progress.tasks[:6]:
        detail = f"{task.task_id}: {task.status.value} | {task.execution_adapter or 'no_adapter'}"
        if task.lease_id:
            detail += f" | lease={task.lease_id}"
        if task.run_id:
            detail += f" | run={task.run_id}"
        if task.blocked_reasons:
            detail += f" | blocked={'; '.join(task.blocked_reasons[:2])}"
        lines.append(detail)
    return _response(
        "progress",
        "Progress",
        lines,
        ok=True,
        extra={"progress": progress.model_dump(mode="json")},
    )


def _blocked_response(project_root: Path) -> dict[str, Any]:
    if not _is_initialized(project_root):
        return _uninitialized_response(project_root)
    store = _require_store(project_root)
    tasks = [task for task in store.list_tasks() if task.status.value in {"blocked", "waiting_approval"}]
    lines = ["No blocked or waiting-approval tasks."] if not tasks else [
        _blocked_task_line(task)
        for task in tasks
    ]
    explanations = {
        task.id: [
            explanation.model_dump(mode="json")
            for explanation in explanations_from_reasons(
                [
                    *task.required_approvals,
                    *(
                        ["task is waiting for approval"]
                        if task.status.value == "waiting_approval"
                        else []
                    ),
                    *(
                        ["task is blocked"]
                        if task.status.value == "blocked"
                        else []
                    ),
                ],
                inspect_command=f"harness tasks inspect {task.id} --project {project_root} --output json",
            )
        ]
        for task in tasks
    }
    return _response(
        "blocked",
        "Blocked Tasks",
        lines,
        ok=True,
        extra={"tasks": [task.model_dump(mode="json") for task in tasks], "blocked_state_explanations": explanations},
    )


def _blocked_task_line(task) -> str:
    reasons = []
    if task.required_approvals:
        reasons.append("missing_approval")
    elif task.status.value == "blocked":
        reasons.append("blocked_by_policy")
    suffix = f" | {', '.join(reasons)}" if reasons else ""
    return f"{task.id} [{task.status.value}] {task.title}{suffix}"


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
        result.adapter_id in {"codex_isolated_edit", "read_only_summary", "repo_planning"}
        and result.decision in {"execution_adapter_rejected", "codex_isolated_edit_blocked_policy", "repo_planning_blocked_policy"}
        and _needs_hosted_approval(result.rejection_reasons + result.errors)
    ):
        state.pending_hosted_approval = True
    lines = [
        f"Task: {_status_label_for_result(result)}",
        f"Adapter: {result.adapter_id or 'none'}",
        f"Lease: {result.lease.id if result.lease else 'none'}",
        f"Run: {result.run.id if result.run else 'none'}",
        f"Decision: {result.decision}",
        f"Rejection reasons: {result.rejection_reasons}",
        f"Errors: {result.errors}",
    ]
    lines.extend(_evidence_next_lines(project_root, result))
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


def _continue_response(project_root: Path, state: ChatSessionState) -> dict[str, Any]:
    if state.pending_orchestration is not None or state.pending_draft is not None or state.pending_execute_lease_id is not None:
        return _confirm_pending(project_root, state)
    if state.pending_hosted_approval:
        return _response(
            "continue_needs_confirmation",
            "Confirmation Required",
            [
                "A hosted-boundary approval is pending from the last blocked Codex dispatch.",
                "Type yes or /confirm to create it, or no to cancel.",
                "Hosted-boundary approval is not apply-back approval.",
            ],
            ok=True,
        )
    if state.latest_objective_id is not None:
        return _run_orchestration_loop(project_root, state, state.latest_objective_id)
    if state.latest_lease_id is not None:
        return _prepare_execute_response(project_root, state.latest_lease_id, state)
    return _recommend_next_response(project_root, state)


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
        if task.metadata.get("execution_adapter") == "repo_planning":
            lines = [
                f"Lease the ready repo-planning task: {task.id} [{task.title}]",
                f"Equivalent command: harness daemon run-once --project {resolve_project_root(project_root)} --output json",
                "Then dispatch the leased task with: harness daemon execute <lease_id> --project . --output json",
                "Repo planning still requires hosted-boundary approval before run creation.",
            ]
            rec_id = "lease_repo_planning_task"
        else:
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
            "For metadata-only evidence: say 'create dry run task'.",
            'For repo planning: harness tasks add --title "Plan repo change" --execution-adapter repo_planning --task-type repo_planning --project . --output json',
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
                **dict(task_draft.metadata),
                "execution_adapter": task_draft.execution_adapter,
                "task_type": task_draft.task_type,
                "chat_orchestrated": True,
                "orchestrator_id": draft.orchestrator_id,
                "workflow_intent": draft.interpreted_intent,
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
        selected_task = tick.selected_task
        if selected_task is None:
            try:
                selected_task = store.get_task(tick.lease.task_id)
            except KeyError:
                selected_task = None
        if selected_task is None or selected_task.objective_id != objective_id:
            lines.append("Lease did not belong to this orchestration objective; stopping without dispatch.")
            break
        state.latest_task_id = selected_task.id
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
                f"Task: {result.task.id if result.task else selected_task.id} [{_status_label_for_result(result)}]",
                f"Lease: {tick.lease.id}",
                f"Adapter: {result.adapter_id or 'none'}",
                f"Decision: {result.decision}",
                f"Run: {result.run.id if result.run else 'none'}",
            ]
        )
        lines.extend(_evidence_next_lines(project_root, result))
        if result.rejection_reasons:
            lines.append(f"Rejection reasons: {result.rejection_reasons}")
        lines.extend(_recovery_lines_for_execution(result))
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


def _status_label_for_result(result: Any) -> str:
    task = getattr(result, "task", None)
    if task is not None:
        status = getattr(getattr(task, "status", None), "value", None) or getattr(task, "status", None)
        if status:
            return str(status)
    if getattr(result, "ok", False):
        return "succeeded"
    decision = str(getattr(result, "decision", ""))
    reasons = getattr(result, "rejection_reasons", []) or getattr(result, "errors", []) or []
    if "approval" in " ".join(str(reason) for reason in reasons).casefold():
        return "waiting approval"
    if "blocked" in decision or "rejected" in decision:
        return "blocked"
    return "failed"


def _evidence_next_lines(project_root: Path, result: Any) -> list[str]:
    run = getattr(result, "run", None)
    task = getattr(result, "task", None)
    lease = getattr(result, "lease", None)
    manifest = getattr(result, "manifest", None)
    lines: list[str] = []
    artifacts = list(getattr(manifest, "artifacts", []) or [])
    if artifacts:
        lines.append("Artifacts:")
        for artifact in artifacts[:6]:
            label = getattr(artifact, "kind", "artifact")
            path = getattr(artifact, "path", "")
            lines.append(f"- {label}: {path}")
    lines.append("Next:")
    if run is not None:
        lines.append(f"harness show {run.id} --project {project_root} --output json")
        lines.append(f"harness artifacts list {run.id} --project {project_root}")
    elif lease is not None:
        lines.append(f"harness daemon inspect-lease {lease.id} --project {project_root} --output json")
    if task is not None and getattr(task, "objective_id", None):
        lines.append(f"harness progress --objective {task.objective_id} --project {project_root} --output json")
    if getattr(result, "adapter_id", None) == "codex_isolated_edit" and run is not None:
        lines.append("Apply-back state: not approved by this run; inspect artifacts before any separate apply-back approval.")
    return lines


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
            "Command: harness approvals add --backend codex_cli --data-boundary hosted_provider --task-types codex_code_edit,read_only_repo_summary,repo_planning --duration-days 1 --project .",
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
    interesting = [
        artifact
        for artifact in artifacts
        if artifact.kind in {"baseline_manifest", "isolated_unified_diff", "isolated_diff_stat", "final_report"}
    ]
    lines = [
        "Apply-back review uses existing inspected diff artifacts only.",
        "Hosted-boundary approval is not apply-back approval.",
        "This chat path does not parse or apply patches from chat text.",
    ]
    if choice == "deny":
        store.append_event(
            state.latest_run_id,
            "info",
            "apply_back_decision",
            "Apply-back was denied from chat review.",
            {"decision": "denied", "reason": "Denied from Harness chat."},
        )
        store.write_run_manifest(state.latest_run_id)
        lines.append("Apply-back denied from chat review; active repository mutation was not requested.")
    elif choice == "keep":
        lines.append("Isolation retained for operator inspection; active repository mutation was not requested.")
    elif choice == "approve":
        return _approve_apply_back_response(project_root, store, state.latest_run_id, interesting)
    lines.extend(f"{artifact.kind}: {artifact.path}" for artifact in interesting)
    return _response(
        "apply_back_review",
        "Apply-Back Review",
        lines,
        ok=choice != "approve",
        extra={"artifacts": [artifact.model_dump(mode="json") for artifact in interesting]},
    )


def _approve_apply_back_response(
    project_root: Path,
    store: SQLiteStore,
    run_id: str,
    artifacts: list[ArtifactRecord],
) -> dict[str, Any]:
    diff_artifact = _artifact_by_kind(artifacts, "isolated_unified_diff")
    baseline_artifact = _artifact_by_kind(artifacts, "baseline_manifest")
    inspection = _latest_event_payload(store, run_id, "isolated_diff_inspected")
    if diff_artifact is None or baseline_artifact is None:
        return _response(
            "apply_back_missing_artifacts",
            "Apply-Back Blocked",
            ["Missing isolated diff or baseline manifest artifact for this run."],
            ok=False,
            extra={"run_id": run_id},
        )
    if diff_artifact.redaction_state == "redacted":
        return _response(
            "apply_back_redacted_diff",
            "Apply-Back Blocked",
            ["The isolated diff artifact was redacted, so it cannot be used as an apply-back source."],
            ok=False,
            extra={"run_id": run_id, "diff_artifact": diff_artifact.model_dump(mode="json")},
        )
    if _latest_event_payload(store, run_id, "apply_back_applied") is not None:
        return _response(
            "apply_back_already_applied",
            "Apply-Back Already Applied",
            ["This run already has an apply_back_applied event. Refusing duplicate active repo mutation."],
            ok=False,
            extra={"run_id": run_id},
        )
    violations = list((inspection or {}).get("violations") or [])
    allowed_files = list((inspection or {}).get("allowed_changed_files") or [])
    if violations:
        return _response(
            "apply_back_policy_violation",
            "Apply-Back Blocked",
            ["The inspected isolated diff has policy violations.", f"Violations: {violations}"],
            ok=False,
            extra={"run_id": run_id, "violations": violations},
        )
    if not allowed_files:
        return _response(
            "apply_back_no_changes",
            "No Apply-Back Changes",
            ["The inspected isolated diff has no allowed file changes to apply."],
            ok=False,
            extra={"run_id": run_id},
        )
    control_reason = _active_repo_apply_back_disabled_reason(store)
    if control_reason is not None:
        store.append_event(
            run_id,
            "warning",
            "apply_back_control_disabled",
            "Apply-back denied by active repo apply-back kill switch.",
            {"reason": control_reason},
        )
        store.append_event(
            run_id,
            "info",
            "apply_back_decision",
            "Apply-back approval was blocked by runtime control.",
            {"decision": "denied", "reason": control_reason},
        )
        store.write_run_manifest(run_id)
        return _response(
            "apply_back_control_disabled",
            "Apply-Back Blocked",
            [control_reason],
            ok=False,
            extra={"run_id": run_id},
        )
    try:
        baseline = _load_baseline_manifest(baseline_artifact.path)
        pre_status = str((inspection or {}).get("active_pre_isolation_git_status") or "")
        if not pre_status:
            pre_status = _active_pre_isolation_status(store, run_id)
        freshness = _check_apply_back_freshness(project_root, allowed_files, baseline["hashes"], pre_status)
        store.append_event(
            run_id,
            "info",
            "apply_back_decision",
            "Apply-back was approved from chat review.",
            {"decision": "approved", "reason": "Approved from Harness chat."},
        )
        if not freshness["ok"]:
            store.append_event(
                run_id,
                "warning",
                "apply_back_freshness_failed",
                "Apply-back failed closed because active project changed since isolation.",
                freshness,
            )
            store.write_run_manifest(run_id)
            return _response(
                "apply_back_freshness_failed",
                "Apply-Back Freshness Failed",
                [
                    freshness["reason"] or "Freshness check failed.",
                    f"Target hash checks: {freshness['target_hash_checks']}",
                ],
                ok=False,
                extra={"run_id": run_id, "freshness": freshness},
            )
        patch = diff_artifact.path.read_text(encoding="utf-8")
        summary, updates = plan_unified_diff(patch, project_root, baseline["excluded_patterns"])
        apply_planned_updates(updates)
    except Exception as exc:
        reason = str(sanitize_for_logging(str(exc)))
        store.append_event(
            run_id,
            "error",
            "apply_back_validation_or_apply_failed",
            "Apply-back failed validation or atomic application.",
            {"reason": reason},
        )
        store.write_run_manifest(run_id)
        return _response(
            "apply_back_failed",
            "Apply-Back Failed",
            [reason],
            ok=False,
            extra={"run_id": run_id},
        )
    post_status = _git_status_porcelain(project_root)
    store.append_event(
        run_id,
        "info",
        "apply_back_applied",
        "Approved isolated diff was applied to the active project from chat.",
        {"files": summary.files, "active_post_apply_status": post_status},
    )
    store.update_run_status(run_id, "completed_applied")
    store.write_run_manifest(run_id)
    return _response(
        "apply_back_applied",
        "Apply-Back Applied",
        [
            f"Run: {run_id}",
            f"Applied files: {', '.join(summary.files)}",
            f"Added lines: {summary.added_lines}",
            f"Removed lines: {summary.removed_lines}",
            "Active repo mutation used the stored inspected diff artifact only.",
        ],
        ok=True,
        extra={
            "run_id": run_id,
            "applied_files": summary.files,
            "added_lines": summary.added_lines,
            "removed_lines": summary.removed_lines,
            "diff_artifact": diff_artifact.model_dump(mode="json"),
        },
    )


def _artifact_by_kind(artifacts: list[ArtifactRecord], kind: str) -> ArtifactRecord | None:
    for artifact in reversed(artifacts):
        if artifact.kind == kind:
            return artifact
    return None


def _latest_event_payload(store: SQLiteStore, run_id: str, event_type: str) -> dict[str, Any] | None:
    for event in reversed(store.list_events(run_id)):
        if event.event_type == event_type:
            return event.payload
    return None


def _active_pre_isolation_status(store: SQLiteStore, run_id: str) -> str:
    payload = _latest_event_payload(store, run_id, "isolation_created") or {}
    return str(payload.get("active_pre_isolation_git_status") or "")


def _active_repo_apply_back_disabled_reason(store: SQLiteStore) -> str | None:
    try:
        for control in store.active_execution_controls():
            if control.target_kind.value == "active_repo_apply_back" and control.target_id == "*":
                return str(
                    sanitize_for_logging(
                        f"Execution control disabled active_repo_apply_back:*. {control.reason}"
                    )
                )
    except Exception as exc:
        return str(sanitize_for_logging(f"Runtime control state unavailable: {exc}"))
    return None


def _load_baseline_manifest(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    hashes = {
        str(entry["path"]): str(entry["sha256"])
        for entry in payload.get("entries", [])
        if isinstance(entry, dict) and "path" in entry and "sha256" in entry
    }
    return {
        "hashes": hashes,
        "excluded_patterns": [str(item) for item in payload.get("excluded_patterns", [])],
    }


def _check_apply_back_freshness(
    project_root: Path,
    target_files: list[str],
    baseline_hashes: dict[str, str],
    pre_isolation_status: str,
) -> dict[str, Any]:
    active_status = _git_status_porcelain(project_root)
    checks: list[dict[str, str | bool]] = []
    ok = True
    reason = None
    if active_status != pre_isolation_status:
        ok = False
        reason = "Active git status changed since isolation was created."
    for relative_path in target_files:
        expected = baseline_hashes.get(relative_path, "")
        path = project_root / relative_path
        exists = path.exists() and path.is_file() and not path.is_symlink()
        actual = _sha256_path(path) if exists else ""
        matches = bool(exists and expected and actual == expected)
        checks.append(
            {
                "path": relative_path,
                "expected_sha256": expected,
                "actual_sha256": actual,
                "matches": matches,
            }
        )
        if not matches:
            ok = False
            reason = reason or f"Target file changed since isolation was created: {relative_path}"
    return {
        "ok": ok,
        "reason": reason,
        "active_pre_apply_status": active_status,
        "target_hash_checks": checks,
    }


def _git_status_porcelain(project_root: Path) -> str:
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=project_root,
        text=True,
        capture_output=True,
        timeout=30,
    )
    if result.returncode != 0:
        return f"GIT_STATUS_UNAVAILABLE: {(result.stderr or result.stdout).strip()}"
    return result.stdout


def _sha256_path(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


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
