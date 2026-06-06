from __future__ import annotations

import hashlib
import json
import re
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
from harness.agent_discovery import (
    DELEGATE_ALLOCATION_SCHEMA_VERSION,
    AgentDiscoveryCard,
    build_agent_discovery_catalog,
    evaluate_delegate_allocation,
)
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
    ChatToolResult,
    chat_tool_specs_payload,
    default_chat_tool_context,
    parse_tool_request,
    run_chat_tool,
)
from harness.config import HARNESS_DIR, default_config, load_config, write_default_config
from harness.context_pack import pack_chat_context
from harness.execution import execute_lease, list_execution_adapter_descriptors
from harness.events import append_jsonl
from harness.memory.sqlite_store import (
    SESSION_SCHEMA_REPAIR_MESSAGE,
    SQLiteStore,
    is_missing_session_schema_error,
)
from harness.models import ArtifactRecord, RunMode, RunRecord, SessionPermissionStatus, TaskLease, TaskRecord
from harness.natural_language_router import (
    NaturalLanguageIntent,
    NaturalLanguageRoute,
    route_natural_language,
)
from harness.objective_checkpoints import (
    create_objective_checkpoint,
    list_objective_checkpoints,
    resolve_objective_checkpoint,
)
from harness.objective_runner import run_objective_autonomously
from harness.operator_context import build_operator_context, render_operator_context_lines
from harness.operator_loop import (
    HarnessAgentLoop,
    HarnessOperatorBusyError,
    HarnessOperatorRuntime,
    create_turn_state_from_session,
    model_supports_native_tools,
    plan_agent_tool_ids,
    persist_save_point,
    persist_turn_aborted,
    persist_turn_finished,
    persist_turn_started,
    persist_turn_waiting_approval,
)
from harness.pending_chat_actions import PENDING_CHAT_ACTION_METADATA_KEY, PENDING_CHAT_ACTION_SCHEMA_VERSION
from harness.paths import resolve_project_root
from harness.progress import build_orchestration_progress
from harness.registry import builtin_spec_registry
from harness.security import sanitize_for_logging
from harness.security_explanations import explanations_from_reasons
from harness.session_cwd import CwdResolutionError, cwd_recovery_message, session_cwd_payload
from harness.session_tools import (
    build_session_approval_card,
    default_session_tool_descriptors,
    execute_session_tool,
    get_session_tool_descriptor,
    model_visible_session_tool_ids,
    persist_session_tool_denial,
    session_planning_mode_projection,
    session_tool_catalog_projection,
    session_tool_descriptor_payload,
)
from harness.task_operator_bridge import apply_operator_task_permission_resolution
from harness.test_runner import DockerTestRunner, RunTestsDecision
from harness.tools.patch import apply_planned_updates, plan_unified_diff
from harness.workflow_templates import WorkflowTaskTemplate, WorkflowTemplate, template_for_intent


CHAT_SCHEMA_VERSION = "harness.chat/v1"
CHAT_RESPONSE_SCHEMA_VERSION = "harness.chat_response/v1"
CHAT_INTENT_SCHEMA_VERSION = "harness.chat_intent/v1"
ORCHESTRATION_DRAFT_SCHEMA_VERSION = "harness.chat_orchestration_draft/v1"
WORKFLOW_AGENT_SELECTION_SOURCE = "delegate_allocation"
AUTONOMOUS_READ_LOOP_SCHEMA_VERSION = "harness.autonomous_read_loop/v1"

CODEX_ORCHESTRATION_ADAPTER = "codex_isolated_edit"
CODEX_ORCHESTRATION_TASK_TYPE = "codex_code_edit"
DEFAULT_ORCHESTRATOR_ID = "coding_orchestrator"
ORCHESTRATION_OWNER = "chat_orchestrator"
MAX_CHAT_TOOL_CALLS = 3
HOSTED_CODEX_ADAPTER_TASK_TYPES = {
    "read_only_summary": "read_only_repo_summary",
    "repo_planning": "repo_planning",
    "codex_isolated_edit": "codex_code_edit",
}
HOSTED_CODEX_APPROVAL_RUNTIME_SECONDS = 2 * 60 * 60


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
    agent_selection: dict[str, Any] | None = None

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
            "agent_selection": self.agent_selection,
        }


@dataclass
class OrchestratedCheckpointDraft:
    label: str
    reason: str = ""
    required: bool = True
    actor: str = "harness_chat"
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        metadata = self.metadata if isinstance(self.metadata, dict) else {}
        return {
            "label": str(sanitize_for_logging(self.label)).strip(),
            "reason": str(sanitize_for_logging(self.reason)).strip(),
            "required": bool(self.required),
            "actor": str(sanitize_for_logging(self.actor)).strip() or "harness_chat",
            "metadata": sanitize_for_logging(dict(metadata)),
        }


@dataclass
class OrchestratedRunDraft:
    objective_title: str
    objective_description: str
    orchestrator_id: str
    workbench_id: str
    tasks: list[OrchestratedTaskDraft]
    checkpoints: list[OrchestratedCheckpointDraft] = field(default_factory=list)
    idempotency_key: str = field(default_factory=lambda: f"chat_orchestration:{uuid.uuid4().hex[:24]}")
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
            "idempotency_key": self.idempotency_key,
            "tasks": [task.to_payload() for task in self.tasks],
            "checkpoints": [checkpoint.to_payload() for checkpoint in self.checkpoints],
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
    session_id: str | None = None
    active_project_root: str | None = None
    latest_task_id: str | None = None
    latest_lease_id: str | None = None
    latest_run_id: str | None = None
    latest_diff_artifact: str | None = None
    latest_failed_task_id: str | None = None
    pending_draft: ChatDraftTask | None = None
    pending_orchestration: OrchestratedRunDraft | None = None
    pending_execute_lease_id: str | None = None
    pending_action_contract: ActionContract | None = None
    pending_session_tool_call: dict[str, Any] | None = None
    pending_hosted_approval: bool = False
    selected_orchestrator_id: str | None = None
    latest_objective_id: str | None = None
    latest_orchestration: dict[str, Any] | None = None
    stop_requested: bool = False
    codex_like_mode: bool = False
    autonomy_profile_id: str = "manual"
    transcript: list[dict[str, Any]] = field(default_factory=list)
    progress: list[str] = field(default_factory=list)
    operator_runtime: HarnessOperatorRuntime = field(default_factory=HarnessOperatorRuntime)

    def reset(self) -> None:
        self.session_id = None
        self.latest_task_id = None
        self.latest_lease_id = None
        self.latest_run_id = None
        self.latest_diff_artifact = None
        self.latest_failed_task_id = None
        self.pending_draft = None
        self.pending_orchestration = None
        self.pending_execute_lease_id = None
        self.pending_action_contract = None
        self.pending_session_tool_call = None
        self.pending_hosted_approval = False
        self.selected_orchestrator_id = None
        self.latest_objective_id = None
        self.latest_orchestration = None
        self.stop_requested = False
        self.codex_like_mode = False
        self.autonomy_profile_id = "manual"
        self.transcript = []
        self.progress = []
        self.operator_runtime.abort()


def chat_context(project_root: Path, *, detail: str = "full") -> dict[str, Any]:
    project_root = resolve_project_root(project_root)
    context = build_operator_context(project_root)
    try:
        cfg = load_config(project_root)
    except FileNotFoundError:
        cfg = default_config()
    summary = dict(context["summary"])
    summary.setdefault("runs_total", summary.get("recent_runs", 0))
    payload = {
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
    if detail == "full":
        payload["dashboard"] = context
    else:
        payload["detail"] = "compact"
        payload["dashboard"] = {
            "schema_version": context["schema_version"],
            "ok": context.get("ok", True),
            "project_root": context["project_root"],
            "initialized": context["initialized"],
            "version": context.get("version"),
            "branch": context.get("branch"),
            "summary": summary,
            "task_status_counts": context.get("task_status_counts", {}),
            "tasks": context.get("tasks", [])[:10],
            "memory": context.get("memory", {}),
            "recent_runs": context.get("recent_runs", [])[:5],
            "recent_sessions": context.get("recent_sessions", [])[:5],
            "command_suggestions": context.get("command_suggestions", [])[:8],
            "detail": "compact",
            "next_detail_command": f"harness home --project {project_root} --output json",
        }
    return payload


def handle_chat_input(
    text: str,
    project_root: Path,
    state: ChatSessionState | None = None,
    chat_model: ChatModel | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    state = state or ChatSessionState()
    project_root = resolve_project_root(state.active_project_root or project_root)
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
    _load_persisted_pending_chat_action(project_root, state)
    if raw in {"/quit", "quit", "exit"}:
        return _response("quit", "Goodbye", ["Exiting harness chat."], ok=True)
    if raw in {"/confirm", "yes", "y"}:
        return _confirm_pending(project_root, state)
    if raw in {"/decline", "no", "n", "cancel"}:
        denial = _deny_pending_session_tool_permission(project_root, state)
        state.pending_draft = None
        state.pending_orchestration = None
        state.pending_execute_lease_id = None
        state.pending_action_contract = None
        state.pending_session_tool_call = None
        state.pending_hosted_approval = False
        _persist_pending_chat_action(project_root, state)
        _persist_active_operator_turn_aborted(project_root, state, reason="declined")
        state.operator_runtime.abort()
        return _response(
            "declined",
            "Declined",
            _decline_response_lines(denial),
            ok=True,
            extra={"operator_status": _operator_status_payload(project_root, state), "denial": denial},
        )
    if state.pending_session_tool_call is not None and raw.casefold().startswith(("no ", "deny ")):
        feedback = raw.split(" ", 1)[1].strip()
        denial = _deny_pending_session_tool_permission(project_root, state, feedback=feedback)
        state.pending_session_tool_call = None
        state.pending_hosted_approval = False
        _persist_pending_chat_action(project_root, state)
        _persist_active_operator_turn_aborted(project_root, state, reason="declined")
        state.operator_runtime.abort()
        return _response(
            "declined",
            "Declined",
            _decline_response_lines(denial),
            ok=True,
            extra={"operator_status": _operator_status_payload(project_root, state), "denial": denial},
        )
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
    try:
        cfg = load_config(project_root)
    except FileNotFoundError:
        cfg = default_config()
    initialized = (project_root / HARNESS_DIR / "harness.sqlite").exists()
    stdout.write("Harness chat\n")
    stdout.write(f"Project: {project_root}\n")
    stdout.write(f"Orchestrator: {state.selected_orchestrator_id or 'none'}\n")
    stdout.write(f"Mode: {'codex-like' if state.codex_like_mode else 'normal'}\n")
    stdout.write(f"Autonomy: {state.autonomy_profile_id}\n")
    stdout.write(f"Initialized: {initialized}\n")
    stdout.write(f"Chat model profile: {cfg.chat.default_model_profile}\n")
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
    elif normalized in {"plan mode", "planning mode", "show plan mode", "plan mode status", "planning mode status"}:
        intent = "plan_mode_status"
    elif normalized in {"enter plan mode", "enable plan mode", "turn on plan mode", "start plan mode"}:
        intent = "plan_mode_enter"
    elif normalized in {"exit plan mode", "disable plan mode", "turn off plan mode", "finish plan mode"}:
        intent = "plan_mode_exit"
    elif _looks_like_web_browse_request(normalized):
        intent = "web_browse"
    elif _looks_like_research_request(normalized):
        intent = "deep_research"
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
    elif _looks_like_repo_mutation_request(normalized):
        intent = "coding_fix"
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


def _looks_like_web_browse_request(normalized: str) -> bool:
    return (
        normalized.startswith("browse ")
        or normalized.startswith("fetch http://")
        or normalized.startswith("fetch https://")
        or normalized.startswith("open url ")
    )


def _looks_like_research_request(normalized: str) -> bool:
    return (
        normalized.startswith("research ")
        or normalized.startswith("deep research ")
        or normalized.startswith("web research ")
        or normalized.startswith("web search ")
        or normalized.startswith("search web ")
        or normalized.startswith("search the web ")
    )


def _extract_browse_target(raw: str) -> str:
    text = raw.strip()
    for pattern in (
        r"^browse\s+(.+)$",
        r"^fetch\s+(.+)$",
        r"^open\s+url\s+(.+)$",
    ):
        match = re.match(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return text


def _extract_research_query(raw: str) -> str:
    text = raw.strip()
    for pattern in (
        r"^deep\s+research\s+(.+)$",
        r"^web\s+research\s+(.+)$",
        r"^research\s+(.+)$",
        r"^web\s+search\s+(?:for\s+)?(.+)$",
        r"^search\s+the\s+web\s+(?:for\s+)?(.+)$",
        r"^search\s+web\s+(?:for\s+)?(.+)$",
    ):
        match = re.match(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return text


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
        if tail not in {"all", "full"}:
            return _response(
                "help",
                "Commands",
                [
                    "Essentials",
                    "/status                 show project state",
                    "/init                   initialize local Harness records",
                    "/tasks                  list latest tasks",
                    "/runs                   list latest runs",
                    "/progress [id]          show objective progress",
                    "",
                    "Work",
                    "/execute [lease_id]     prepare registered adapter dispatch",
                    "/run                    run pending work or continue a lease",
                    "/confirm                confirm a pending governed action",
                    "/decline [feedback]     decline a pending governed action",
                    "/stop                   stop foreground orchestration at a boundary",
                    "",
                    "Session",
                    "/plan-mode [on|off|status]  manage session-local planning mode",
                    "/browse <url>           fetch a URL through web approval",
                    "/research <query>       deep web research through web approval",
                    "/reset                  clear in-memory chat references",
                    "/quit                   exit",
                    "",
                    "Advanced: /help all",
                ],
            )
        return _response(
            "help",
            "Commands",
            [
                "Navigation & Information",
                "/home, /status          show project state and dashboard",
                "/dashboard              show passive dashboard context",
                "/pwd                    show active project root and session cwd",
                "/tasks                  list latest tasks",
                "/runs                   list latest runs",
                "/leases                 list active leases",
                "/capabilities           list Harness capability catalog entries",
                "/adapters               list registered adapters",
                "/memory                 list explicit local memory records",
                "/progress [id]          show read-only orchestration progress",
                "/task <id>              show task details",
                "/run <id>               show run manifest summary",
                "/artifact <id>          show artifact metadata",
                "/lease [id]             inspect a lease",
                "",
                "Orchestration & Planning",
                "/orchestrators          list built-in orchestrators",
                "/use <id>               select an orchestrator for this chat",
                "/agents                 list built-in agents for the active workbench",
                "/plan [request]         ask the assistant for a plan",
                "/plan-mode on [reason]  enter session-local planning mode",
                "/plan-mode off <summary> exit planning mode with evidence",
                "/act <request>          propose Harness-backed action contracts",
                "/execute [lease_id]     prepare registered adapter dispatch",
                "/run                    run pending orchestration or dispatch lease",
                "/stop                   stop the foreground orchestration loop",
                "",
                "Session & Project",
                "/init                   initialize this project for Harness records",
                "/cd <path>              change session cwd inside the active project",
                "/project <path>         switch active root explicitly",
                "/mode [normal|codex]    show or change chat action mode",
                "/tools                  list model-visible session tools",
                "/browse <url>           fetch a URL through external-network approval",
                "/research <query>       deep web research through external-network approval",
                "/remember <text>        save a project-scoped memory note",
                "/forget <id>            forget a memory record",
                "",
                "Codex Edit & Review",
                "/diff                   show latest isolated diff or git diff",
                "/apply [approve|deny|keep]   review/apply inspected isolated changes",
                "/apply-back [deny|approve|keep]  review Codex diff artifacts",
                "/revert                 prepare a revert action contract",
                "/test [command]         propose a sandboxed test run",
                "",
                "Action Commands",
                "/confirm                confirm pending draft or execution",
                "/decline [feedback]     deny pending approval or execution",
                "/reset                  clear in-memory chat references",
                "/quit                   exit",
            ],
        )
    if command in {"decline", "deny"}:
        denial = _deny_pending_session_tool_permission(project_root, state, feedback=tail or None)
        state.pending_draft = None
        state.pending_orchestration = None
        state.pending_execute_lease_id = None
        state.pending_action_contract = None
        state.pending_session_tool_call = None
        state.pending_hosted_approval = False
        _persist_pending_chat_action(project_root, state)
        _persist_active_operator_turn_aborted(project_root, state, reason="declined")
        state.operator_runtime.abort()
        return _response(
            "declined",
            "Declined",
            _decline_response_lines(denial),
            ok=True,
            extra={"operator_status": _operator_status_payload(project_root, state), "denial": denial},
        )
    if command == "tools":
        return _session_tools_response(project_root)
    if command in {"plan-mode", "planning"}:
        return _plan_mode_response(project_root, state, tail)
    if command in {"browse", "fetch", "web-fetch"}:
        return _browse_response(project_root, state, tail)
    if command in {"research", "web-search", "search-web"}:
        return _research_response(project_root, state, tail)
    if command == "pwd":
        return _run_session_tool_response(project_root, state, "pwd", {})
    if command == "cd":
        target = tail or "."
        switch = _cd_project_switch_response(project_root, state, target)
        if switch is not None:
            return switch
        return _run_session_tool_response(project_root, state, "cd", {"path": target, "actor": "operator"})
    if command in {"project", "workspace"}:
        return _project_switch_response(project_root, state, tail, command=command)
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
        return _artifact_response(project_root, state.latest_diff_artifact) if state.latest_diff_artifact else _run_session_tool_response(project_root, state, "git-diff", {})
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
        _clear_persisted_pending_chat_action(project_root, state)
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
    if route_chat_intent(raw)["intent"] == "show_progress":
        return _orchestration_progress_response(project_root, state.latest_objective_id, state)
    start_error = _begin_operator_turn(raw, project_root, state)
    if start_error is not None:
        return start_error
    try:
        response = _handle_intent_routed(
            raw,
            project_root,
            state,
            chat_model=chat_model,
            progress_callback=progress_callback,
        )
    except Exception:
        _persist_active_operator_turn_aborted(project_root, state, reason="failed")
        state.operator_runtime.finish()
        raise
    _settle_operator_turn(project_root, state, response)
    return response


def _handle_intent_routed(
    raw: str,
    project_root: Path,
    state: ChatSessionState,
    *,
    chat_model: ChatModel | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    intent = route_chat_intent(raw)["intent"]
    if _active_session_planning_mode(project_root, state) and _plan_mode_should_capture_intent(intent):
        _emit_progress(progress_callback, "procedure", "Ran plan-mode routing")
        _emit_progress(progress_callback, "procedure", f"- intent: {intent}")
        return _plan_mode_prompt_response(
            project_root,
            state,
            raw,
            intent,
            chat_model=chat_model,
            progress_callback=progress_callback,
        )
    external_write_block = _external_filesystem_write_block_response(raw, project_root)
    if external_write_block is not None:
        return external_write_block
    natural_language_response = _natural_language_route_response(
        raw,
        project_root,
        state,
        progress_callback=progress_callback,
        defer_fuzzy_test_route=chat_model is not None,
    )
    if natural_language_response is not None:
        return natural_language_response
    isolated_edit_request = _isolated_edit_request_for_user_intent(raw)
    if isolated_edit_request is not None and state.codex_like_mode:
        return _action_contract_response(project_root, state, isolated_edit_request)
    managed_action_response = _maybe_run_managed_action(raw, project_root, state)
    if managed_action_response is not None:
        return managed_action_response
    if chat_model is not None and intent in {"coding_fix", "draft_orchestration", "draft_codex"}:
        return _model_chat_response(raw, project_root, state, chat_model=chat_model, progress_callback=progress_callback)
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
    if intent == "plan_mode_status":
        return _plan_mode_status_response(project_root, state)
    if intent == "plan_mode_enter":
        return _plan_mode_response(project_root, state, f"on {raw}")
    if intent == "plan_mode_exit":
        return _plan_mode_response(project_root, state, f"off {raw}")
    if intent == "web_browse":
        return _browse_response(project_root, state, _extract_browse_target(raw))
    if intent == "deep_research":
        return _research_response(project_root, state, _extract_research_query(raw))
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
            _orchestration_from_template(template_for_intent("coding_fix", raw, project_root), state, project_root),
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
        return _artifact_response(project_root, state.latest_diff_artifact) if state.latest_diff_artifact else _run_session_tool_response(project_root, state, "git-diff", {})
    if intent == "apply_back_review":
        return _apply_back_review_response(project_root, state)
    if intent == "approve_apply_back":
        return _apply_back_review_response(project_root, state, choice="approve")
    if intent == "deny_apply_back":
        return _apply_back_review_response(project_root, state, choice="deny")
    return _model_chat_response(raw, project_root, state, chat_model=chat_model, progress_callback=progress_callback)


def _begin_operator_turn(raw: str, project_root: Path, state: ChatSessionState) -> dict[str, Any] | None:
    if state.operator_runtime.phase.value != "idle":
        return _response(
            "operator_busy",
            "Operator Busy",
            [
                f"Phase: {state.operator_runtime.phase.value}",
                "Wait for the current turn to finish, approve it with /confirm, or cancel it with /decline.",
            ],
            ok=False,
            extra={"operator_status": _operator_status_payload(project_root, state)},
        )
    if not _is_initialized(project_root):
        return None
    try:
        store, session_id = _ensure_chat_session(project_root, state)
        session = store.get_session(session_id)
        try:
            cfg = load_config(project_root)
        except FileNotFoundError:
            cfg = default_config()
        active_tools = _operator_active_tools(project_root=project_root, plan_mode=_active_session_planning_mode(project_root, state))
        turn_state = create_turn_state_from_session(
            project_root=project_root,
            session=session,
            model_profile_id=cfg.chat.default_model_profile,
            backend_id=cfg.chat.default_model_profile,
            agent_id=session.agent_id or state.selected_orchestrator_id or "operator",
            workbench_id=session.workbench_id,
            active_tools=active_tools,
            run_mode=RunMode.READ_ONLY,
            stream_options={"stream": cfg.chat.stream},
        )
        state.operator_runtime.start_turn(turn_state)
        persist_turn_started(store, turn_state, prompt=raw)
    except HarnessOperatorBusyError:
        return _response(
            "operator_busy",
            "Operator Busy",
            ["Another operator turn is already active."],
            ok=False,
            extra={"operator_status": _operator_status_payload(project_root, state)},
        )
    except Exception:
        _persist_active_operator_turn_aborted(project_root, state, reason="start_failed")
        state.operator_runtime.finish()
        raise
    return None


def _settle_operator_turn(project_root: Path, state: ChatSessionState, response: dict[str, Any]) -> None:
    if response.get("kind") == "session_tool_permission_required":
        permission_id = response.get("permission_id")
        if permission_id is None and isinstance(response.get("tool_request"), dict):
            permission_id = response.get("tool_request", {}).get("permission_id")
        if permission_id is None and state.pending_session_tool_call is not None:
            permission_id = state.pending_session_tool_call.get("permission_id")
        state.operator_runtime.wait_for_approval(str(permission_id) if permission_id else None)
        _persist_active_operator_turn_waiting_approval(project_root, state, str(permission_id) if permission_id else None)
        response["operator_status"] = _operator_status_payload(project_root, state)
        return
    _persist_active_operator_turn_finished(project_root, state, reason="completed")
    state.operator_runtime.finish()
    response["operator_status"] = _operator_status_payload(project_root, state)


def _persist_operator_save_point(
    store: SQLiteStore,
    state: ChatSessionState,
    *,
    flushed_event_count: int,
    flushed_artifact_count: int = 0,
) -> None:
    turn_state = state.operator_runtime.active_turn_state
    if turn_state is None:
        return
    try:
        session = store.get_session(turn_state.session_id)
        next_turn_state = create_turn_state_from_session(
            project_root=Path(turn_state.project_root),
            session=session,
            model_profile_id=turn_state.model_profile_id,
            backend_id=turn_state.backend_id,
            agent_id=session.agent_id or turn_state.agent_id,
            workbench_id=session.workbench_id or turn_state.workbench_id,
            active_tools=_operator_active_tools(
                project_root=Path(turn_state.project_root),
                plan_mode=_active_session_planning_mode(Path(turn_state.project_root), state),
            ),
            run_mode=turn_state.run_mode,
            context_pack_sha256=turn_state.context_pack_sha256,
            stream_options=dict(turn_state.stream_options),
        )
        persist_save_point(
            store,
            turn_state,
            next_turn_state=next_turn_state,
            flushed_event_count=flushed_event_count,
            flushed_artifact_count=flushed_artifact_count,
        )
    except Exception:
        return


def _persist_active_operator_turn_finished(project_root: Path, state: ChatSessionState, *, reason: str) -> None:
    turn_state = state.operator_runtime.active_turn_state
    if turn_state is None or not _is_initialized(project_root):
        return
    try:
        persist_turn_finished(_require_store(project_root), turn_state, reason=reason)
    except Exception:
        return


def _persist_active_operator_turn_waiting_approval(
    project_root: Path,
    state: ChatSessionState,
    approval_id: str | None,
) -> None:
    turn_state = state.operator_runtime.active_turn_state
    if turn_state is None or not _is_initialized(project_root):
        return
    try:
        persist_turn_waiting_approval(_require_store(project_root), turn_state, waiting_approval_id=approval_id)
    except Exception:
        return


def _persist_active_operator_turn_aborted(project_root: Path, state: ChatSessionState, *, reason: str) -> None:
    turn_state = state.operator_runtime.active_turn_state
    if turn_state is None or not _is_initialized(project_root):
        return
    try:
        persist_turn_aborted(_require_store(project_root), turn_state, reason=reason)
    except Exception:
        return


def _operator_status_payload(project_root: Path, state: ChatSessionState) -> dict[str, Any]:
    return state.operator_runtime.status(
        project_root=resolve_project_root(state.active_project_root or project_root),
        cwd=_chat_session_cwd(project_root, state),
        active_tools=_operator_active_tools(project_root=project_root, plan_mode=_active_session_planning_mode(project_root, state)),
    ).model_dump(mode="json")


def _operator_active_tools(*, project_root: Path | None = None, plan_mode: bool = False) -> list[str]:
    if plan_mode:
        return plan_agent_tool_ids(project_root=project_root)
    return model_visible_session_tool_ids(project_root=project_root)


def _natural_language_route_response(
    raw: str,
    project_root: Path,
    state: ChatSessionState,
    *,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
    defer_fuzzy_test_route: bool = False,
) -> dict[str, Any] | None:
    route = route_natural_language(
        raw,
        project_root,
        session_cwd=_chat_session_cwd(project_root, state),
        context_excludes=_chat_context_excludes(project_root),
    )
    if route.intent in {NaturalLanguageIntent.UNSUPPORTED, NaturalLanguageIntent.DIRECT_SLASH_COMMAND}:
        return None
    if defer_fuzzy_test_route and route.intent == NaturalLanguageIntent.RUN_TESTS and " -k " in str(route.proposed_command or ""):
        return None
    _emit_progress(progress_callback, "procedure", "Ran natural-language routing")
    _emit_progress(progress_callback, "procedure", f"- intent: {route.intent.value}")
    return _execute_natural_language_route(project_root, state, route)


def _execute_natural_language_route(
    project_root: Path,
    state: ChatSessionState,
    route: NaturalLanguageRoute,
) -> dict[str, Any]:
    if not route.ok:
        if route.intent == NaturalLanguageIntent.PROJECT_SWITCH:
            return _response(
                "project_switch_boundary",
                "Project Boundary",
                [
                    route.message or "That path is outside the active project.",
                    f"Active project: {project_root}",
                    f"Target: {route.target_path or 'unknown'}",
                    f"Next: {route.proposed_command or '/project <path>'}",
                ],
                ok=False,
                extra={"route": route.model_dump(mode="json")},
            )
        return _response(
            "natural_language_route_blocked",
            "Route Blocked",
            [route.message or "Harness could not safely route that request."],
            ok=False,
            extra={"route": route.model_dump(mode="json")},
        )
    if route.intent in {NaturalLanguageIntent.PROJECT_SWITCH, NaturalLanguageIntent.WORKSPACE_SWITCH}:
        target = route.target_project_root or route.target_path or ""
        return _project_switch_response(
            project_root,
            state,
            target,
            command="workspace" if route.intent == NaturalLanguageIntent.WORKSPACE_SWITCH else "project",
        )
    if route.tool_id is not None:
        return _run_session_tool_response(project_root, state, route.tool_id, route.tool_arguments)
    return _response(
        "natural_language_route_unsupported",
        "Unsupported",
        ["Harness recognized the phrase but no safe route is implemented for it yet."],
        ok=False,
        extra={"route": route.model_dump(mode="json")},
    )


def _chat_context_excludes(project_root: Path) -> list[str]:
    try:
        return list(load_config(project_root).context_excludes)
    except FileNotFoundError:
        return list(default_config().context_excludes)


def _chat_session_cwd(project_root: Path, state: ChatSessionState) -> str:
    if not state.session_id or not _is_initialized(project_root):
        return "."
    try:
        return str(_require_store(project_root).get_session(state.session_id).metadata.get("cwd") or ".")
    except Exception:
        return "."


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
        or state.pending_session_tool_call
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
    return _managed_action_response(result, project_root=project_root, route=route, decision=decision)


def _managed_action_response(
    result: ManagedActionResult,
    *,
    project_root: Path,
    route: Any,
    decision: Any,
) -> dict[str, Any]:
    manifest_path = result.manifest_path or (result.report_path.parent / "manifest.json" if result.report_path else None)
    lines = [result.message]
    if result.created_paths:
        lines.append("Created: " + ", ".join(_project_relative_display_path(path, project_root) for path in result.created_paths))
    if result.changed_paths:
        lines.append("Changed: " + ", ".join(_project_relative_display_path(path, project_root) for path in result.changed_paths))
    sandbox = decision.sandbox_assessment.status.value if decision.sandbox_assessment else "not_recorded"
    lines.append(f"Policy: {decision.status.value}; sandbox={sandbox}; executor={route.executor}")
    lines.append("Boundary: no provider, shell, Docker, network, permission grant, or human approval prompt.")
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
        extra={
            **result.model_dump(mode="json"),
            "route": route.model_dump(mode="json"),
            "decision": decision.model_dump(mode="json"),
        },
    )


def _project_relative_display_path(path: Path, project_root: Path) -> str:
    try:
        return path.resolve().relative_to(resolve_project_root(project_root)).as_posix()
    except ValueError:
        return path.name


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
    if mode_override:
        mode = mode_override
    elif _active_session_planning_mode(project_root, state):
        mode = "plan"
    else:
        mode = "codex-like" if state.codex_like_mode else "normal"
    model_profile = context_payload["chat"]["default_model_profile"]
    context_manifest = pack_chat_context(
        project_root,
        query=raw,
        mode=mode,
        model_profile=model_profile,
        safety_boundaries=list(context_payload["safety_boundaries"]),
    )
    _attach_context_pack_hash(state, context_manifest)
    _emit_progress(progress_callback, "procedure", "Explored")
    for line in _context_manifest_progress_lines(context_manifest):
        _emit_progress(progress_callback, "procedure", line)
    chat_ctx = ChatContext(
        project_root=str(project_root),
        model_profile=model_profile,
        mode=mode,
        context_blocks=[block.to_payload() for block in context_manifest.blocks],
        safety_boundaries=list(context_payload["safety_boundaries"]),
    )
    plan_mode_active = chat_ctx.mode == "plan"
    messages = _model_messages(raw, state, chat_ctx)
    tool_results: list[dict[str, Any]] = []
    try:
        model = chat_model or build_default_chat_model(project_root)
        if model_supports_native_tools(model) and _is_initialized(project_root):
            return _native_agent_loop_response(
                raw,
                project_root,
                state,
                model=model,
                chat_ctx=chat_ctx,
                messages=messages,
                context_manifest=context_manifest,
                progress_callback=progress_callback,
            )
        _emit_progress(progress_callback, "procedure", "Ran model turn")
        model_response = _complete_model_turn(model, messages, chat_ctx, progress_callback)
        for _index in range(MAX_CHAT_TOOL_CALLS):
            tool_request = parse_tool_request(model_response.content)
            if tool_request is None:
                break
            if not _is_initialized(project_root) and tool_request.tool in {"repo_tree", "read_file", "search_repo", "show_diff"}:
                normalized_request = tool_request
            else:
                normalized_request = _normalize_session_tool_request(tool_request, user_prompt=raw)
            _emit_progress(progress_callback, "reasoning", f"Reasoning: requesting {normalized_request.tool}.")
            _emit_progress(progress_callback, "procedure", f"Ran {normalized_request.tool}")
            if plan_mode_active and not _plan_mode_tool_request_allowed(normalized_request):
                boundary_result = _plan_mode_tool_boundary_result(normalized_request)
                tool_results.append(
                    {
                        "tool": boundary_result.tool,
                        "ok": boundary_result.ok,
                        "error_type": boundary_result.error_type,
                    }
                )
                if progress_callback is not None:
                    progress_callback(
                        {
                            "kind": "procedure",
                            "content": f"- {boundary_result.tool}: {boundary_result.error_type}",
                        }
                    )
                messages.append(ChatMessage(role="assistant", content=model_response.content))
                messages.append(ChatMessage(role="user", content=f"Harness plan-mode boundary:\n{boundary_result.to_message()}"))
                _emit_progress(progress_callback, "procedure", "Ran model turn")
                model_response = _complete_model_turn(model, messages, chat_ctx, progress_callback)
                continue
            try:
                session_tool_result = _try_execute_model_session_tool(project_root, state, normalized_request)
            except ValueError as exc:
                return _response("session_tool_failed", "Tool Failed", [str(exc)], ok=False)
            if session_tool_result is not None:
                if not session_tool_result.ok and session_tool_result.error_type == "permission_required":
                    return _session_tool_permission_response(normalized_request, session_tool_result)
                if not session_tool_result.ok and session_tool_result.error_type in {
                    "permission_denied",
                    "path_security",
                    "secret_path",
                    "context_excluded",
                    "invalid_cwd",
                    "schema_validation_failed",
                }:
                    return _response(
                        "session_tool_blocked",
                        "Tool Blocked",
                        _session_tool_blocked_lines(normalized_request.tool, session_tool_result.preview),
                        ok=False,
                        extra={
                            "tool_request": {
                                "tool": normalized_request.tool,
                                "arguments": sanitize_for_logging(normalized_request.arguments),
                            },
                            "result": session_tool_result.model_dump(mode="json"),
                        },
                    )
                tool_results.append(
                    {
                        "tool": session_tool_result.tool_id,
                        "ok": session_tool_result.ok,
                        "error_type": session_tool_result.error_type,
                    }
                )
                if progress_callback is not None:
                    progress_callback(
                        {
                            "kind": "procedure",
                            "content": f"- {session_tool_result.tool_id}: {'ok' if session_tool_result.ok else session_tool_result.error_type or 'failed'}",
                        }
                    )
                messages.append(ChatMessage(role="assistant", content=model_response.content))
                messages.append(ChatMessage(role="user", content=f"Harness tool result:\n{_session_tool_result_message(session_tool_result)}"))
                _emit_progress(progress_callback, "procedure", "Ran model turn")
                model_response = _complete_model_turn(model, messages, chat_ctx, progress_callback)
                continue
            tool_result = run_chat_tool(normalized_request, default_chat_tool_context(project_root))
            if tool_result.error_type == "action_contract_required":
                return _action_contract_response(project_root, state, normalized_request)
            if tool_result.error_type == "unknown_tool":
                return _response(
                    "action_contract_rejected",
                    "Action Contract Rejected",
                    [tool_result.content],
                    ok=False,
                    extra={
                        "tool_request": {
                            "tool": normalized_request.tool,
                            "arguments": sanitize_for_logging(normalized_request.arguments),
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
    if plan_mode_active and parse_tool_request(content) is not None:
        return _response(
            "plan_mode_tool_request_blocked",
            "Plan",
            [
                "The model kept requesting a Harness tool instead of returning a plan.",
                "Plan mode did not create an approval, task, lease, adapter dispatch, provider run, or active-repo mutation.",
                "Retry with a more explicit planning request, or exit plan mode before asking for execution.",
            ],
            ok=False,
            extra={
                "model_profile": chat_ctx.model_profile,
                "mode": chat_ctx.mode,
                "hosted_fallback": False,
                "context_manifest": _context_manifest_response_payload(context_manifest),
                "tool_results": tool_results,
                "action_proposals": model_response.action_proposals,
                "permission_granting": False,
                "creates_pending_action": False,
            },
        )
    if not content:
        content = "The local chat model returned an empty response."
    fallback_request = _fallback_action_request_for_user_intent(raw)
    if not plan_mode_active and fallback_request is not None and _model_missed_side_effect_request(content):
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
            "context_manifest": _context_manifest_response_payload(context_manifest),
            "tool_results": tool_results,
            "action_proposals": model_response.action_proposals,
        },
    )


def _native_agent_loop_response(
    raw: str,
    project_root: Path,
    state: ChatSessionState,
    *,
    model: ChatModel,
    chat_ctx: ChatContext,
    messages: list[ChatMessage],
    context_manifest: Any,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    store, session_id = _ensure_chat_session(project_root, state)
    turn_state = state.operator_runtime.active_turn_state
    if turn_state is None:
        session = store.get_session(session_id)
        cfg = load_config(project_root)
        turn_state = create_turn_state_from_session(
            project_root=project_root,
            session=session,
            model_profile_id=cfg.chat.default_model_profile,
            backend_id=cfg.chat.default_model_profile,
            agent_id=session.agent_id or state.selected_orchestrator_id or "operator",
            workbench_id=session.workbench_id,
            active_tools=_operator_active_tools(project_root=project_root, plan_mode=chat_ctx.mode == "plan"),
            run_mode=RunMode.READ_ONLY,
            stream_options={"stream": cfg.chat.stream},
        )
        if state.operator_runtime.phase.value == "idle":
            state.operator_runtime.start_turn(turn_state)
        persist_turn_started(store, turn_state, prompt=raw)
    loop = HarnessAgentLoop(
        store=store,
        project_root=project_root,
        session_id=session_id,
        model=model,
        chat_context=chat_ctx,
        messages=_native_agent_loop_messages(messages, mode=chat_ctx.mode),
        turn_state=turn_state,
        progress_callback=progress_callback,
        queue_drain_callback=state.operator_runtime.drain_save_point_queues,
    )
    loop_result = loop.run(raw)
    state.operator_runtime.active_turn_state = loop_result.turn_state or loop.turn_state
    if loop_result.tool_results:
        last_run_id = next((item.run_id for item in reversed(loop_result.tool_results) if item.run_id), None)
        if last_run_id:
            state.latest_run_id = last_run_id
    tool_result_payloads = [item.to_payload() for item in loop_result.tool_results]
    if loop_result.status == "approval_required":
        if chat_ctx.mode == "plan":
            state.pending_session_tool_call = None
            return _response(
                "plan_mode_tool_request_blocked",
                "Plan",
                [
                    "The model requested a permission-gated tool while plan mode was active.",
                    "Plan mode did not create a pending approval, task, lease, adapter dispatch, provider run, or active-repo mutation.",
                    "Ask for a revised plan, or exit plan mode before requesting execution.",
                ],
                ok=False,
                extra={
                    "model_profile": chat_ctx.model_profile,
                    "mode": chat_ctx.mode,
                    "hosted_fallback": False,
                    "native_tool_loop": True,
                    "context_manifest": _context_manifest_response_payload(context_manifest),
                    "tool_results": tool_result_payloads,
                    "action_proposals": [],
                    "permission_granting": False,
                    "creates_pending_action": False,
                },
            )
        if loop_result.pending_tool_call is not None:
            state.pending_session_tool_call = loop_result.pending_tool_call
        permission_result = loop_result.permission_result
        if permission_result is None:
            return _response(
                "agent_loop_failed",
                "Tool Loop Blocked",
                ["The model requested approval, but Harness could not build a permission request."],
                ok=False,
                extra={"tool_results": tool_result_payloads, "native_tool_loop": True},
            )
        return _session_tool_permission_response(
            ChatToolRequest(
                type="harness.tool_request/v1",
                tool=permission_result.tool_id,
                arguments=dict((loop_result.pending_tool_call or {}).get("arguments") or {}),
            ),
            permission_result,
            store=store,
            session_id=session_id,
        )
    if loop_result.status != "final":
        title = "Tool Loop Guard" if loop_result.status == "guard_triggered" else "Tool Loop Blocked"
        return _response(
            "agent_loop_guard_triggered" if loop_result.status == "guard_triggered" else "agent_loop_failed",
            title,
            [loop_result.final_output],
            ok=False,
            extra={
                "native_tool_loop": True,
                "stop_reason": loop_result.stop_reason,
                "tool_results": tool_result_payloads,
            },
        )
    return _response(
        "llm_chat",
        "Assistant",
        loop_result.final_output.splitlines(),
        ok=True,
        extra={
            "model_profile": chat_ctx.model_profile,
            "mode": chat_ctx.mode,
            "hosted_fallback": False,
            "native_tool_loop": True,
            "context_manifest": _context_manifest_response_payload(context_manifest),
            "tool_results": tool_result_payloads,
            "action_proposals": [],
        },
    )


def _native_agent_loop_messages(messages: list[ChatMessage], *, mode: str = "normal") -> list[ChatMessage]:
    if mode == "plan":
        native_instruction = ChatMessage(
            role="system",
            content=(
                "This backend supports provider-native Harness session tools. Plan mode is active: use only the "
                "supplied read-only or session-local tools, and do not call tools for approval, execution, network, "
                "provider dispatch, active-repository mutation, or apply-back. If a side-effecting tool would be "
                "needed later, describe that as a future governed step and return the plan."
            ),
        )
        if not messages:
            return [native_instruction]
        return [*messages[:-1], native_instruction, messages[-1]]
    native_instruction = ChatMessage(
        role="system",
        content=(
            "This backend supports provider-native Harness session tools. "
            "Use the supplied native tool schemas for project inspection and session-local navigation. "
            "Do not emit harness.tool_request/v1 JSON unless native tools are unavailable. "
            "Permission-required tools pause for Harness approval; do not claim they ran until Harness returns evidence."
        ),
    )
    if not messages:
        return [native_instruction]
    return [*messages[:-1], native_instruction, messages[-1]]


def _context_manifest_response_payload(context_manifest: Any) -> dict[str, Any]:
    payload = context_manifest.to_payload() if hasattr(context_manifest, "to_payload") else None
    if isinstance(payload, dict):
        return payload
    return {
        "blocks": [
            {
                "kind": block.kind,
                "title": block.title,
                "source": block.source,
                "token_estimate": block.token_estimate,
                "truncated": block.truncated,
                "role": block.role,
            }
            for block in context_manifest.blocks
        ],
        "blocked_paths": context_manifest.blocked_paths,
        "warnings": context_manifest.warnings,
        "budget_report": context_manifest.budget_report.to_payload(),
        "role_summary": context_manifest.role_summary,
        "retriever": context_manifest.retriever,
        "selected_chunks": list(context_manifest.selected_chunks or []),
        "context_provenance": [
            record.model_dump(mode="json") for record in (context_manifest.context_provenance or [])
        ],
        "untrusted_context_warnings": list(context_manifest.untrusted_context_warnings or []),
    }


def _context_manifest_progress_lines(context_manifest: Any) -> list[str]:
    summary = context_manifest.to_payload().get("context_summary", {}) if hasattr(context_manifest, "to_payload") else {}
    lines = [f"- Project: {context_manifest.project_root}"]
    selected_count = int(summary.get("selected_block_count") or len(getattr(context_manifest, "blocks", [])))
    role_counts = summary.get("role_counts") or getattr(context_manifest, "role_summary", {})
    role_parts = _context_role_parts(role_counts)
    context_line = f"- Context: {selected_count} selected {_plural('block', selected_count)}"
    if role_parts:
        context_line += ", " + ", ".join(role_parts)
    lines.append(context_line)
    source_categories = [str(item) for item in summary.get("source_categories") or []]
    if source_categories:
        lines.append(f"- Sources: {', '.join(source_categories[:6])}")
    token_budget = summary.get("token_budget") or {}
    used_tokens = token_budget.get("used_input_tokens")
    max_tokens = token_budget.get("max_input_tokens")
    if used_tokens is not None and max_tokens is not None:
        lines.append(f"- Budget: {int(used_tokens):,} / {int(max_tokens):,} tokens")
    retriever = summary.get("retriever")
    selected_chunk_count = int(summary.get("selected_chunk_count") or 0)
    if retriever or selected_chunk_count:
        lines.append(
            f"- Retrieval: {retriever or 'none'}, {selected_chunk_count} selected {_plural('chunk', selected_chunk_count)}"
        )
    read_sources: list[str] = []
    for block in context_manifest.blocks:
        if block.source:
            read_sources.append(str(block.source))
    if read_sources:
        lines.append(f"- Read {', '.join(read_sources[:6])}")
    blocked_path_count = int(summary.get("blocked_path_count") or len(getattr(context_manifest, "blocked_paths", [])))
    if blocked_path_count:
        lines.append(f"- Blocked paths: {blocked_path_count}")
    warning_codes = [str(item) for item in summary.get("warning_codes") or getattr(context_manifest, "warnings", [])]
    if warning_codes:
        lines.append(f"- Warnings: {', '.join(warning_codes[:3])}")
    return lines


def _context_role_parts(role_counts: Any) -> list[str]:
    if not isinstance(role_counts, dict):
        return []
    parts: list[str] = []
    for role in ("pinned", "retrieved", "derived"):
        count = int(role_counts.get(role) or 0)
        if count:
            parts.append(f"{count} {role}")
    return parts


def _plural(noun: str, count: int) -> str:
    return noun if count == 1 else f"{noun}s"


def _attach_context_pack_hash(state: ChatSessionState, context_manifest: Any) -> None:
    turn_state = state.operator_runtime.active_turn_state
    if turn_state is None or not hasattr(context_manifest, "to_payload"):
        return
    try:
        payload = context_manifest.to_payload()
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        turn_state.context_pack_sha256 = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    except Exception:
        return


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
        should_emit_progress = not (kind == "content" and parse_tool_request(content) is not None)
        if progress_callback is not None and content.strip() and should_emit_progress:
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
                "loop. Side-effecting tools are also available, and Harness validates them through deterministic "
                "policy before either executing them with evidence or failing closed. Do not claim to mutate files, run "
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
                    "Plan mode is enabled. Produce a concrete, prompt-specific implementation plan grounded in "
                    "the user's request and Harness context. Do not return a generic template. Do not emit tool "
                    "requests that create approvals, tasks, leases, adapter dispatch, provider execution, shell, "
                    "network access, active-repository mutation, or apply-back. You may request only read-only "
                    "or session-local tools that are available in plan mode. If execution would be needed later, "
                    "describe it as a future governed step inside the plan."
                ),
            )
        )
    messages.append(
        ChatMessage(
            role="system",
            content=(
                "Available Harness session tools. Model-visible tool calls must use the "
                "harness.tool_request/v1 envelope and route through the session-tool registry. "
                "For web-search, always include arguments.query with the concrete search query inferred "
                "from the user's request; do not emit an empty arguments object. "
                "Permission-required tools are controlled boundaries. For file or folder write requests, prefer "
                "edit_isolated so Harness can use an isolated workspace and apply-back gates; raw write/edit tools "
                "are active-repo boundaries and must fail closed without exact authority:\n"
                + json.dumps(_model_tool_specs_payload(), sort_keys=True, default=str)
            ),
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


def _model_tool_specs_payload() -> list[dict[str, Any]]:
    session_specs = [session_tool_descriptor_payload(descriptor) for descriptor in default_session_tool_descriptors()]
    legacy_gated = [
        spec
        for spec in chat_tool_specs_payload()
        if spec.get("risk") != "read" or bool(spec.get("requires_confirmation"))
    ]
    return session_specs + legacy_gated


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


def _isolated_edit_request_for_user_intent(raw: str) -> ChatToolRequest | None:
    normalized = _normalize(raw)
    if normalized and _looks_like_repo_mutation_request(normalized) and (
        "isolated edit" in normalized or "action contract" in normalized
    ):
        return ChatToolRequest(type="harness.tool_request/v1", tool="edit_isolated", arguments={"goal": raw})
    return None


def _external_filesystem_write_block_response(raw: str, project_root: Path) -> dict[str, Any] | None:
    target_hint = _external_filesystem_write_target_hint(raw, project_root)
    if target_hint is None:
        return None
    project_root = resolve_project_root(project_root)
    return _response(
        "external_filesystem_write_blocked",
        "Write Blocked",
        [
            f"Requested target: {target_hint}",
            "Decision: blocked before orchestration.",
            "Reason: external filesystem writes are outside the project and isolated apply-back boundary.",
            "No file was created, no repo task was started, and no human approval prompt is needed.",
            f"Project boundary: {project_root}",
            "Supported autonomous path: request a project-relative path, then Harness can use isolated edit evidence and keep apply-back separate.",
        ],
        ok=False,
        extra={
            "policy_boundary": {
                "kind": "external_filesystem_write",
                "target": target_hint,
                "project_root": str(project_root),
                "filesystem_modified": False,
                "orchestration_started": False,
                "permission_granting": False,
                "authority_granting": False,
            }
        },
    )


def _external_filesystem_write_target_hint(raw: str, project_root: Path) -> str | None:
    normalized = _normalize(raw)
    if not normalized or not _looks_like_repo_mutation_request(normalized):
        return None
    explicit_path = _external_absolute_path_hint(raw, project_root)
    if explicit_path is not None:
        return explicit_path
    if re.search(r"\b(?:outside|external)\s+(?:the\s+)?(?:repo|repository|project|workspace)\b", normalized):
        return "outside project"
    home_markers = (
        (
            "Downloads",
            r"\b(?:in|into|inside|under|within|to|at)\s+(?:the\s+)?downloads?\b|\bdownloads?\s+(?:folder|directory)\b",
        ),
        (
            "Desktop",
            r"\b(?:in|into|inside|under|within|to|at)\s+(?:the\s+)?desktop\b|\bdesktop\s+(?:folder|directory)\b",
        ),
        (
            "home directory",
            r"\b(?:in|into|inside|under|within|to|at)\s+(?:the\s+)?home\s+(?:folder|directory)\b|\buser\s+home\b",
        ),
    )
    for label, pattern in home_markers:
        if re.search(pattern, normalized):
            return label
    return None


def _external_absolute_path_hint(raw: str, project_root: Path) -> str | None:
    root = resolve_project_root(project_root)
    pattern = re.compile(r"(?<!\S)(~[/\\][^\s]+|\$HOME[/\\][^\s]+|/[^\s]+|[A-Za-z]:\\[^\s]+)")
    for match in pattern.finditer(raw):
        token = match.group(1).strip().strip("'\"`").rstrip(".,;:)]}")
        if not token:
            continue
        candidate_text = token
        if token.startswith("$HOME"):
            candidate_text = str(Path.home()) + token[len("$HOME") :]
        candidate = Path(candidate_text).expanduser()
        if not candidate.is_absolute():
            continue
        try:
            candidate.resolve(strict=False).relative_to(root.resolve())
        except ValueError:
            return token
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
    if re.search(r"\b[a-z0-9_./-]+\.[a-z0-9]{1,12}\b", normalized):
        return True
    return any(
        marker in normalized
        for marker in (
            " file",
            " folder",
            " directory",
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
            " script",
            " program",
            " python",
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


def _ensure_chat_session(project_root: Path, state: ChatSessionState) -> tuple[SQLiteStore, str]:
    if not _is_initialized(project_root):
        raise ValueError(f"Project is not initialized: {project_root}")
    store = _require_store(project_root)
    if state.session_id is not None:
        try:
            store.get_session(state.session_id)
            return store, state.session_id
        except KeyError:
            state.session_id = None
    session = store.create_session(
        title="Harness chat",
        intent="session_tool_gateway",
        metadata={"cwd": "."},
    )
    state.session_id = session.id
    state.active_project_root = str(project_root)
    return store, session.id


def _has_in_memory_pending_chat_action(state: ChatSessionState) -> bool:
    return bool(
        state.pending_draft
        or state.pending_orchestration
        or state.pending_execute_lease_id
        or state.pending_action_contract
        or state.pending_hosted_approval
    )


def _pending_chat_action_payload(state: ChatSessionState) -> dict[str, Any] | None:
    base = {
        "schema_version": PENDING_CHAT_ACTION_SCHEMA_VERSION,
        "codex_like_mode": state.codex_like_mode,
        "latest_objective_id": state.latest_objective_id,
        "latest_task_id": state.latest_task_id,
        "latest_lease_id": state.latest_lease_id,
        "latest_run_id": state.latest_run_id,
    }
    if state.pending_action_contract is not None:
        return {**base, "kind": "action_contract", "contract": state.pending_action_contract.to_payload()}
    if state.pending_orchestration is not None:
        return {**base, "kind": "orchestration_draft", "draft": state.pending_orchestration.to_payload()}
    if state.pending_draft is not None:
        return {**base, "kind": "task_draft", "draft": state.pending_draft.to_payload()}
    if state.pending_execute_lease_id is not None:
        return {**base, "kind": "execute_lease", "lease_id": state.pending_execute_lease_id}
    if state.pending_hosted_approval:
        return {**base, "kind": "hosted_approval"}
    return None


def _persist_pending_chat_action(project_root: Path, state: ChatSessionState) -> None:
    if not _is_initialized(project_root):
        return
    payload = _pending_chat_action_payload(state)
    if payload is None and not state.session_id:
        return
    try:
        store, session_id = _ensure_chat_session(project_root, state)
        session = store.get_session(session_id)
    except Exception:
        return
    metadata = dict(session.metadata or {})
    if payload is None:
        metadata.pop(PENDING_CHAT_ACTION_METADATA_KEY, None)
    else:
        metadata[PENDING_CHAT_ACTION_METADATA_KEY] = sanitize_for_logging(payload)
    store.update_session(session_id, metadata=metadata)


def _clear_persisted_pending_chat_action(project_root: Path, state: ChatSessionState) -> None:
    if not state.session_id or not _is_initialized(project_root):
        return
    try:
        store = _require_store(project_root)
        session = store.get_session(state.session_id)
    except Exception:
        return
    metadata = dict(session.metadata or {})
    if PENDING_CHAT_ACTION_METADATA_KEY not in metadata:
        return
    metadata.pop(PENDING_CHAT_ACTION_METADATA_KEY, None)
    store.update_session(state.session_id, metadata=metadata)


def _load_persisted_pending_chat_action(project_root: Path, state: ChatSessionState) -> bool:
    if _has_in_memory_pending_chat_action(state) or not state.session_id or not _is_initialized(project_root):
        return False
    try:
        store = _require_store(project_root)
        session = store.get_session(state.session_id)
    except Exception:
        return False
    payload = session.metadata.get(PENDING_CHAT_ACTION_METADATA_KEY)
    if not isinstance(payload, dict):
        return False
    try:
        _restore_pending_chat_action_payload(payload, state)
    except Exception:
        _clear_persisted_pending_chat_action(project_root, state)
        return False
    state.progress.append(f"pending chat action restored: {payload.get('kind')}")
    return True


def _restore_pending_chat_action_payload(payload: dict[str, Any], state: ChatSessionState) -> None:
    if payload.get("schema_version") != PENDING_CHAT_ACTION_SCHEMA_VERSION:
        raise ValueError("unsupported pending chat action schema")
    state.codex_like_mode = bool(payload.get("codex_like_mode", state.codex_like_mode))
    state.latest_objective_id = _optional_payload_string(payload, "latest_objective_id") or state.latest_objective_id
    state.latest_task_id = _optional_payload_string(payload, "latest_task_id") or state.latest_task_id
    state.latest_lease_id = _optional_payload_string(payload, "latest_lease_id") or state.latest_lease_id
    state.latest_run_id = _optional_payload_string(payload, "latest_run_id") or state.latest_run_id
    kind = str(payload.get("kind") or "")
    if kind == "action_contract":
        contract_payload = payload.get("contract")
        if not isinstance(contract_payload, dict):
            raise ValueError("missing action contract payload")
        state.pending_action_contract = _action_contract_from_payload(contract_payload)
        return
    if kind == "orchestration_draft":
        draft_payload = payload.get("draft")
        if not isinstance(draft_payload, dict):
            raise ValueError("missing orchestration draft payload")
        state.pending_orchestration = _orchestration_draft_from_payload(draft_payload)
        return
    if kind == "task_draft":
        draft_payload = payload.get("draft")
        if not isinstance(draft_payload, dict):
            raise ValueError("missing task draft payload")
        state.pending_draft = _task_draft_from_payload(draft_payload)
        return
    if kind == "execute_lease":
        lease_id = _optional_payload_string(payload, "lease_id")
        if not lease_id:
            raise ValueError("missing lease id")
        state.pending_execute_lease_id = lease_id
        return
    if kind == "hosted_approval":
        state.pending_hosted_approval = True
        return
    raise ValueError(f"unknown pending chat action kind: {kind}")


def _action_contract_from_payload(payload: dict[str, Any]) -> ActionContract:
    return ActionContract(
        id=str(payload["id"]),
        tool=str(payload["tool"]),
        risk=str(payload["risk"]),  # type: ignore[arg-type]
        summary=str(payload.get("summary") or ""),
        normalized_arguments=dict(payload.get("normalized_arguments") or {}),
        required_confirmations=[str(item) for item in payload.get("required_confirmations") or []],
        required_approvals=[str(item) for item in payload.get("required_approvals") or []],
        execution_plan=[dict(item) for item in payload.get("execution_plan") or [] if isinstance(item, dict)],
        evidence_plan=[str(item) for item in payload.get("evidence_plan") or []],
        allowed_next_commands=[str(item) for item in payload.get("allowed_next_commands") or []],
        requires_confirmation=bool(payload.get("requires_confirmation", True)),
        schema_version=str(payload.get("schema_version") or ACTION_CONTRACT_SCHEMA_VERSION),
    )


def _task_draft_from_payload(payload: dict[str, Any]) -> ChatDraftTask:
    return ChatDraftTask(
        title=str(payload.get("title") or "Recovered task draft"),
        description=str(payload.get("description") or ""),
        execution_adapter=str(payload.get("execution_adapter") or "dry_run"),
        task_type=str(payload.get("task_type") or "phase_1a_test"),
        interpreted_intent=str(payload.get("interpreted_intent") or "task"),
        proposed_action=str(payload.get("proposed_action") or "Create one Harness task from this draft."),
        agent_id=_optional_payload_string(payload, "agent_id"),
        workbench_id=_optional_payload_string(payload, "workbench_id"),
        required_approvals=[str(item) for item in payload.get("required_approvals") or []],
        safety_notes=[str(item) for item in payload.get("safety_notes") or []],
        equivalent_command=str(payload.get("equivalent_command") or ""),
        mutates_when_confirmed=bool(payload.get("mutates_when_confirmed", True)),
    )


def _orchestration_draft_from_payload(payload: dict[str, Any]) -> OrchestratedRunDraft:
    raw_tasks = payload.get("tasks") if isinstance(payload.get("tasks"), list) else []
    raw_checkpoints = payload.get("checkpoints") if isinstance(payload.get("checkpoints"), list) else []
    return OrchestratedRunDraft(
        objective_title=str(payload.get("objective_title") or "Recovered orchestration"),
        objective_description=str(payload.get("objective_description") or ""),
        orchestrator_id=str(payload.get("orchestrator_id") or DEFAULT_ORCHESTRATOR_ID),
        workbench_id=str(payload.get("workbench_id") or "coding"),
        tasks=[_orchestration_task_from_payload(item) for item in raw_tasks if isinstance(item, dict)],
        checkpoints=[
            _orchestration_checkpoint_from_payload(item) for item in raw_checkpoints if isinstance(item, dict)
        ],
        idempotency_key=str(payload.get("idempotency_key") or f"chat_orchestration:{uuid.uuid4().hex[:24]}"),
        interpreted_intent=str(payload.get("interpreted_intent") or "codex_isolated_edit"),
        proposed_action=str(
            payload.get("proposed_action")
            or "Create a visible objective/task graph and run it through registered adapters."
        ),
        required_approvals=[str(item) for item in payload.get("required_approvals") or []],
        safety_notes=[str(item) for item in payload.get("safety_notes") or []],
        equivalent_commands=[str(item) for item in payload.get("equivalent_commands") or []],
        confirm_prompt=str(
            payload.get("confirm_prompt")
            or "Type yes, /confirm, or /run to create the objective and run this graph in the foreground."
        ),
    )


def _orchestration_task_from_payload(payload: dict[str, Any]) -> OrchestratedTaskDraft:
    return OrchestratedTaskDraft(
        title=str(payload.get("title") or "Recovered task"),
        description=str(payload.get("description") or ""),
        agent_id=str(payload.get("agent_id") or "repo_inspector"),
        workbench_id=str(payload.get("workbench_id") or "coding"),
        execution_adapter=str(payload.get("execution_adapter") or CODEX_ORCHESTRATION_ADAPTER),
        task_type=str(payload.get("task_type") or CODEX_ORCHESTRATION_TASK_TYPE),
        depends_on_indexes=[
            int(item)
            for item in payload.get("depends_on_indexes") or []
            if isinstance(item, int) and not isinstance(item, bool)
        ],
        priority=int(payload.get("priority") or 0),
        metadata=dict(payload.get("metadata") or {}),
    )


def _orchestration_checkpoint_from_payload(payload: dict[str, Any]) -> OrchestratedCheckpointDraft:
    return OrchestratedCheckpointDraft(
        label=str(payload.get("label") or "Recovered supervisor checkpoint"),
        reason=str(payload.get("reason") or ""),
        required=bool(payload.get("required", True)),
        actor=str(payload.get("actor") or "harness_chat"),
        metadata=dict(payload.get("metadata") or {}),
    )


def _optional_payload_string(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _session_tools_response(project_root: Path) -> dict[str, Any]:
    payload = session_tool_catalog_projection(project_root=project_root)
    descriptors = payload["tools"]
    lines = [
        (
            f"{descriptor['id']}: side_effect={descriptor['side_effect']} "
            f"permission_required={str(descriptor['permission_required']).lower()} "
            f"enabled={str(descriptor['policy']['enabled']).lower()} "
            f"maturity={','.join(descriptor['policy']['maturity'])}"
        )
        for descriptor in descriptors
    ]
    return _response(
        "session_tools",
        "Session Tools",
        lines,
        ok=True,
        extra={
            "project_root": str(project_root),
            "session_tools_schema_version": payload["schema_version"],
            "policy_projection_schema_version": payload["policy_projection_schema_version"],
            "policy_source": payload["policy_source"],
            "permission_granting": payload["permission_granting"],
            "tools": payload["tools"],
        },
    )


def _plan_mode_response(project_root: Path, state: ChatSessionState, tail: str) -> dict[str, Any]:
    parts, split_error = _split_command_tail(tail)
    if split_error:
        return _response("plan_mode_invalid", "Plan Mode", [split_error], ok=False)
    action = (parts[0].casefold() if parts else "status").strip()
    if action in {"", "status", "show"}:
        return _plan_mode_status_response(project_root, state)
    rest = " ".join(parts[1:]).strip() if parts else ""
    if action in {"on", "enter", "enable", "start"}:
        return _run_session_tool_response(
            project_root,
            state,
            "plan-enter",
            {"reason": rest or "Operator requested planning mode."},
        )
    if action in {"off", "exit", "disable", "done"}:
        if not rest:
            return _response(
                "plan_mode_summary_required",
                "Plan Mode",
                [
                    "Usage: /plan-mode off <summary>",
                    "Planning mode exit records the summary as session-local evidence.",
                    "No provider, shell, Docker, web request, filesystem mutation, or permission grant is started.",
                ],
                ok=False,
            )
        return _run_session_tool_response(
            project_root,
            state,
            "plan-exit",
            {"summary": rest, "next_action": "", "proposed_tools": []},
        )
    return _response(
        "plan_mode_invalid",
        "Plan Mode",
        [
            "Usage: /plan-mode [status|on|off]",
            "/plan-mode on [reason]",
            "/plan-mode off <summary>",
        ],
        ok=False,
    )


def _plan_mode_status_response(project_root: Path, state: ChatSessionState) -> dict[str, Any]:
    if not _is_initialized(project_root):
        return _response(
            "plan_mode_status",
            "Plan Mode",
            [
                "State: unavailable",
                "Initialized: false",
                "Next: /init before entering session-local plan mode.",
                "Boundary: read-only status; no provider, shell, Docker, web request, filesystem mutation, or permission grant was started.",
            ],
            ok=True,
            extra={"planning_mode": {"active": False}, "permission_granting": False},
        )
    planning_mode = {"active": False}
    session_id = state.session_id
    if session_id:
        try:
            store = _require_store(project_root)
            planning_mode = session_planning_mode_projection(store.get_session(session_id).metadata)
        except KeyError:
            planning_mode = {"active": False}
            session_id = None
    active = bool(planning_mode.get("active"))
    lines = [
        f"State: {'active' if active else 'inactive'}",
        f"Session: {session_id or 'none'}",
    ]
    if planning_mode.get("reason"):
        lines.append(f"Reason: {planning_mode['reason']}")
    if planning_mode.get("entered_at"):
        lines.append(f"Entered: {planning_mode['entered_at']}")
    if planning_mode.get("summary"):
        lines.append(f"Last summary: {planning_mode['summary']}")
    if planning_mode.get("next_action"):
        lines.append(f"Next action: {planning_mode['next_action']}")
    proposed = planning_mode.get("proposed_tools") or []
    if proposed:
        lines.append("Proposed tools: " + ", ".join(str(item) for item in proposed))
    lines.extend(
        [
            "Next: /plan-mode on <reason> or /plan-mode off <summary>.",
            "Boundary: session-local planning metadata only; no provider, shell, Docker, web request, filesystem mutation, or permission grant was started.",
        ]
    )
    return _response(
        "plan_mode_status",
        "Plan Mode",
        lines,
        ok=True,
        extra={"planning_mode": planning_mode, "session_id": session_id, "permission_granting": False},
    )


def _active_session_planning_mode(project_root: Path, state: ChatSessionState) -> bool:
    if not state.session_id or not _is_initialized(project_root):
        return False
    try:
        store = _require_store(project_root)
        return bool(session_planning_mode_projection(store.get_session(state.session_id).metadata).get("active"))
    except Exception:
        return False


_PLAN_MODE_PASSTHROUGH_INTENTS = {
    "plan_mode_status",
    "plan_mode_enter",
    "plan_mode_exit",
}


def _plan_mode_should_capture_intent(intent: str) -> bool:
    return intent not in _PLAN_MODE_PASSTHROUGH_INTENTS


def _plan_mode_prompt_response(
    project_root: Path,
    state: ChatSessionState,
    raw: str,
    intent: str,
    *,
    chat_model: ChatModel | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    state.pending_draft = None
    state.pending_orchestration = None
    state.pending_execute_lease_id = None
    state.pending_action_contract = None
    state.pending_hosted_approval = False
    state.pending_session_tool_call = None
    _persist_pending_chat_action(project_root, state)
    response = _model_chat_response(
        raw,
        project_root,
        state,
        chat_model=chat_model,
        mode_override="plan",
        progress_callback=progress_callback,
    )
    if response.get("kind") == "llm_chat":
        response["kind"] = "plan_mode_plan"
        response["title"] = "Plan"
    response.update(
        sanitize_for_logging(
            {
                "planning_mode": {"active": True},
                "captured_intent": intent,
                "creates_pending_action": False,
                "permission_granting": False,
                "provider_execution_started": False,
                "adapter_dispatch_started": False,
            }
        )
    )
    state.pending_draft = None
    state.pending_orchestration = None
    state.pending_execute_lease_id = None
    state.pending_action_contract = None
    state.pending_hosted_approval = False
    if response.get("kind") != "session_tool_permission_required":
        state.pending_session_tool_call = None
    _persist_pending_chat_action(project_root, state)
    return response


def _plan_mode_tool_request_allowed(request: ChatToolRequest) -> bool:
    try:
        descriptor = get_session_tool_descriptor(request.tool)
    except KeyError:
        return False
    return bool(descriptor.enabled and descriptor.allowed_in_plan_agent and not descriptor.permission_required)


def _plan_mode_tool_boundary_result(request: ChatToolRequest) -> ChatToolResult:
    return ChatToolResult(
        tool=request.tool,
        ok=False,
        content=(
            "Plan mode does not create approvals, run execution, call providers, use network, "
            "or mutate the active repository. Treat the requested tool as a future governed step and return "
            "a concrete plan for the user's prompt."
        ),
        data={
            "schema_version": "harness.plan_mode_tool_boundary/v1",
            "tool": request.tool,
            "arguments": sanitize_for_logging(request.arguments),
            "permission_granting": False,
            "approval_created": False,
            "execution_started": False,
            "provider_call_started": False,
            "active_repo_modified": False,
        },
        error_type="plan_mode_boundary",
    )


def _browse_response(project_root: Path, state: ChatSessionState, tail: str) -> dict[str, Any]:
    parts, split_error = _split_command_tail(tail)
    if split_error:
        return _response("browse_invalid", "Browse", [split_error], ok=False)
    if not parts:
        return _response(
            "browse_needs_url",
            "Browse",
            [
                "Usage: /browse <https-url> [markdown|text|html]",
                "Harness will validate project web_tools policy and pause for exact external-network approval before fetching.",
            ],
            ok=False,
        )
    requested_format = parts[1].casefold() if len(parts) > 1 else "markdown"
    return _run_session_tool_response(
        project_root,
        state,
        "web-fetch",
        {"url": parts[0], "format": requested_format, "timeout": 30},
    )


def _research_response(project_root: Path, state: ChatSessionState, tail: str) -> dict[str, Any]:
    query = tail.strip()
    if not query:
        return _response(
            "research_needs_query",
            "Deep Research",
            [
                "Usage: /research <query>",
                "Harness will validate project web_tools policy and pause for exact external-network approval before search.",
            ],
            ok=False,
        )
    return _run_session_tool_response(
        project_root,
        state,
        "web-search",
        {
            "query": query,
            "num_results": 10,
            "search_type": "deep",
            "livecrawl": "preferred",
            "context_max_characters": 30000,
        },
    )


def _split_command_tail(tail: str) -> tuple[list[str], str | None]:
    try:
        return shlex.split(tail), None
    except ValueError as exc:
        return [], f"Could not parse command arguments: {exc}"


def _run_session_tool_response(project_root: Path, state: ChatSessionState, tool_id: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if not _is_initialized(project_root):
        return _uninitialized_response(project_root)
    try:
        store, session_id = _ensure_chat_session(project_root, state)
        before_event_count = len(store.list_session_store_events(session_id))
        result = execute_session_tool(store, project_root, session_id, tool_id, arguments)
    except CwdResolutionError as exc:
        return _response(
            "session_cwd_invalid",
            "Session Cwd Recovery",
            [cwd_recovery_message(exc)],
            ok=False,
        )
    except Exception as exc:
        if is_missing_session_schema_error(exc):
            return _response(
                "session_schema_missing",
                "Session Database Repair Required",
                [SESSION_SCHEMA_REPAIR_MESSAGE],
                ok=False,
            )
        return _response("session_tool_failed", "Tool Failed", [str(exc)], ok=False)
    state.latest_run_id = result.run_id
    if result.error_type == "permission_required":
        state.pending_session_tool_call = {
            "project_root": str(project_root),
            "session_id": session_id,
            "tool_id": tool_id,
            "arguments": sanitize_for_logging(arguments),
            "permission_id": result.permission_id,
        }
        after_event_count = len(store.list_session_store_events(session_id))
        _persist_operator_save_point(
            store,
            state,
            flushed_event_count=max(0, after_event_count - before_event_count),
            flushed_artifact_count=0,
        )
        return _session_tool_permission_response(
            ChatToolRequest(type="harness.tool_request/v1", tool=tool_id, arguments=arguments),
            result,
            store=store,
            session_id=session_id,
        )
    after_event_count = len(store.list_session_store_events(session_id))
    _persist_operator_save_point(
        store,
        state,
        flushed_event_count=max(0, after_event_count - before_event_count),
        flushed_artifact_count=1 if result.artifact_id else 0,
    )
    if not result.ok and result.error_type in {
        "permission_denied",
        "path_security",
        "secret_path",
        "context_excluded",
        "invalid_cwd",
        "schema_validation_failed",
        "tool_error",
    }:
        lines = _session_tool_blocked_lines(tool_id, result.preview)
    else:
        lines = result.preview.splitlines() or [result.preview]
    if result.artifact_id:
        lines.append(f"Output artifact: {result.artifact_id}")
    return _response(
        "session_tool_result",
        f"Tool {tool_id}",
        lines,
        ok=result.ok,
        extra={"result": result.model_dump(mode="json")},
    )


def _session_tool_permission_response(
    request: ChatToolRequest,
    result: Any,
    *,
    store: SQLiteStore | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    permission_id = getattr(result, "permission_id", None)
    approval_card = None
    if permission_id:
        try:
            if store is None:
                raise ValueError("missing store")
            approval_card = build_session_approval_card(
                store,
                session_id or str(getattr(result, "session_id")),
                str(permission_id),
                fallback_arguments=dict(request.arguments or {}),
            )
        except Exception:
            approval_card = _fallback_approval_card(request, result)
    if approval_card is None:
        approval_card = _fallback_approval_card(request, result)
    state_lines = [
        "Approval required to run session tool:",
        f"tool: {approval_card.get('tool_id') or request.tool}",
        f"cwd: {approval_card.get('cwd') or '.'}",
    ]
    command = approval_card.get("command")
    operation = approval_card.get("operation")
    if command:
        state_lines.append(f"command: {command}")
    elif operation:
        state_lines.append(f"operation: {operation}")
    if approval_card.get("timeout_seconds") is not None:
        state_lines.append(f"timeout: {approval_card['timeout_seconds']}s")
    if approval_card.get("shell_executable"):
        state_lines.append(f"shell: {approval_card['shell_executable']}")
    if approval_card.get("sandbox_profile"):
        state_lines.append(f"sandbox: {approval_card['sandbox_profile']}")
    if approval_card.get("network_policy"):
        state_lines.append(f"network: {approval_card['network_policy']}")
    state_lines.extend(
        [
            f"approval: {permission_id or 'pending'}",
            "Next: /confirm to approve once, or /decline [feedback] to deny.",
        ]
    )
    preview = getattr(result, "preview", "")
    if preview:
        state_lines.extend(["", *str(preview).splitlines()])
    return _response(
        "session_tool_permission_required",
        "Permission Required",
        state_lines,
        ok=False,
        extra={
            "tool_request": {"tool": request.tool, "arguments": sanitize_for_logging(request.arguments)},
            "permission_id": permission_id,
            "approval_card": approval_card,
        },
    )


def _fallback_approval_card(request: ChatToolRequest, result: Any) -> dict[str, Any]:
    arguments = dict(request.arguments or {})
    return sanitize_for_logging(
        {
            "schema_version": "harness.session_approval_card/v1",
            "approval_id": getattr(result, "permission_id", None),
            "permission_id": getattr(result, "permission_id", None),
            "session_id": getattr(result, "session_id", None),
            "run_id": getattr(result, "run_id", None),
            "tool_id": request.tool,
            "cwd": arguments.get("cwd") or ".",
            "operation": arguments.get("command") or request.tool,
            "command": arguments.get("command"),
            "timeout_seconds": arguments.get("timeout_seconds") or arguments.get("timeout"),
            "shell_executable": arguments.get("shell_executable") or arguments.get("shell"),
            "sandbox_profile": None,
            "network_policy": None,
            "status": "pending",
            "approve_once": True,
        }
    )


def _try_execute_model_session_tool(project_root: Path, state: ChatSessionState, request: ChatToolRequest) -> Any | None:
    if request.tool == "shell":
        return None
    try:
        get_session_tool_descriptor(request.tool)
    except KeyError:
        return None
    if not _is_initialized(project_root):
        raise ValueError(f"Project is not initialized: {project_root}")
    store, session_id = _ensure_chat_session(project_root, state)
    result = execute_session_tool(store, project_root, session_id, request.tool, request.arguments)
    state.latest_run_id = result.run_id
    if result.error_type == "permission_required":
        state.pending_session_tool_call = {
            "project_root": str(project_root),
            "session_id": session_id,
            "tool_id": request.tool,
            "arguments": sanitize_for_logging(request.arguments),
            "permission_id": result.permission_id,
        }
    return result


def _session_tool_result_message(result: Any) -> str:
    return json.dumps(
        {
            "type": "harness.tool_result/v1",
            "tool": result.tool_id,
            "ok": result.ok,
            "content": result.preview,
            "artifact_id": result.artifact_id,
            "error_type": result.error_type,
            "run_id": result.run_id,
        },
        sort_keys=True,
        default=str,
    )


def _session_tool_blocked_lines(tool_id: str, preview: str) -> list[str]:
    lines = preview.splitlines() or [preview]
    normalized_preview = preview.lower()
    if tool_id == "web-search" and (
        "web search is disabled by project web_tools policy" in normalized_preview
        or "web search endpoint is not configured by project web_tools policy" in normalized_preview
    ):
        lines.extend(
            [
                "",
                "Web search is an external-network session tool and must be enabled in project config before it can request approval.",
                "Configure .harness/config.yaml:",
                "  web_tools.enabled: true",
                "  web_tools.search_enabled: true",
                "  web_tools.search_provider: exa_mcp",
                "  # or: web_tools.search_provider: configured_http with web_tools.search_endpoint_url set",
                "After that, Harness will still pause for exact web-search approval before any network request.",
            ]
        )
    if tool_id == "web-fetch" and "web fetch is disabled by project web_tools policy" in normalized_preview:
        lines.extend(
            [
                "",
                "Web browsing is an external-network session tool and must be enabled in project config before it can request approval.",
                "Configure .harness/config.yaml:",
                "  web_tools.enabled: true",
                "  web_tools.fetch_enabled: true",
                "  web_tools.allowed_domains: ['docs.example.com']",
                "After that, Harness will still pause for exact web-fetch approval before any network request.",
            ]
        )
    return lines


def _normalize_session_tool_request(request: ChatToolRequest, *, user_prompt: str | None = None) -> ChatToolRequest:
    aliases = {
        "repo_tree": "glob",
        "read_file": "read",
        "search_repo": "grep",
        "show_diff": "git-diff",
    }
    tool = aliases.get(request.tool, request.tool)
    arguments = dict(request.arguments)
    if request.tool == "repo_tree":
        path = arguments.get("path")
        arguments = {"pattern": "**/*", **({"cwd": path} if path else {})}
    elif request.tool == "read_file":
        arguments = {
            "path": arguments.get("path") or arguments.get("file") or "",
            **({"cwd": arguments.get("cwd")} if arguments.get("cwd") else {}),
        }
    elif request.tool == "search_repo":
        arguments = {
            "pattern": arguments.get("pattern") or arguments.get("query") or "",
            "path": arguments.get("path"),
            "regex": bool(arguments.get("regex") or False),
        }
    elif request.tool == "show_diff":
        arguments = {"path": arguments.get("path"), "stat_only": bool(arguments.get("stat_only") or False)}
    elif tool == "web-search":
        query = arguments.get("query") or arguments.get("q") or arguments.get("search_query") or arguments.get("text")
        if (query is None or not str(query).strip()) and user_prompt:
            query = user_prompt
        if query is not None:
            arguments["query"] = str(query).strip()
        for alias in ("q", "search_query", "text"):
            arguments.pop(alias, None)
    return ChatToolRequest(type=request.type, tool=tool, arguments=arguments)


def _cd_project_switch_response(project_root: Path, state: ChatSessionState, target: str) -> dict[str, Any] | None:
    try:
        candidate = Path(target).expanduser()
        resolved = candidate.resolve() if candidate.is_absolute() else (project_root / candidate).resolve()
        resolved.relative_to(project_root.resolve())
    except ValueError:
        return _project_switch_response(project_root, state, str(resolved), command="project", proposed_from_cd=True)
    except OSError:
        return None
    return None


def _project_switch_response(
    project_root: Path,
    state: ChatSessionState,
    target: str,
    *,
    command: str,
    proposed_from_cd: bool = False,
) -> dict[str, Any]:
    if not target.strip():
        active = resolve_project_root(state.active_project_root or project_root)
        return _response("project", "Project", [f"Project root: {active}", f"Session: {state.session_id or 'none'}"], ok=True)
    target_root = resolve_project_root(target)
    initialized = _is_initialized(target_root)
    state.active_project_root = str(target_root)
    state.session_id = None
    state.pending_session_tool_call = None
    if not initialized:
        return _response(
            "project_switch_requires_init",
            "Project Switch",
            [
                f"Target root: {target_root}",
                f"Initialized: false",
                "Run /init to initialize this root before session tools can persist events there.",
            ],
            ok=False,
            extra={"project_root": str(target_root), "source": "cd" if proposed_from_cd else command},
        )
    return _response(
        "project_switched",
        "Project Switched",
        [
            f"Project root: {target_root}",
            "Attached session: will attach or create on the next session tool call.",
        ],
        ok=True,
        extra={"project_root": str(target_root), "command": command},
    )


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
            response = _execute_action_contract(
                project_root,
                state,
                contract,
                prepare_required_approvals=True,
                autonomy_scope=state.autonomy_profile_id,
            )
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
    _persist_pending_chat_action(project_root, state)
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
    autonomy_scope: str | None = None,
) -> dict[str, Any]:
    approval_lines = (
        _ensure_contract_required_approvals(project_root, contract, autonomy_scope=autonomy_scope)
        if prepare_required_approvals
        else []
    )
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


def _ensure_contract_required_approvals(
    project_root: Path,
    contract: ActionContract,
    *,
    autonomy_scope: str | None = None,
) -> list[str]:
    if "hosted_provider_codex" not in contract.required_approvals:
        return []
    task_types = _hosted_codex_task_types_for_contract(contract)
    approvals = ApprovalStore(project_root)
    missing = [
        task_type
        for task_type in task_types
        if approvals.find_valid(
            "codex_cli",
            "hosted_provider",
            task_type,
            adapter_id=_hosted_codex_adapter_for_contract_task(contract, task_type),
            autonomy_scope=autonomy_scope,
            strict_scope=autonomy_scope is not None,
        )
        is None
    ]
    if not missing:
        return []
    allowed_adapters = None
    if contract.tool == "edit_isolated":
        allowed_adapters = ["repo_planning", "codex_isolated_edit"]
    elif contract.tool == "dispatch_registered_adapter":
        adapter_id = contract.normalized_arguments.get("adapter_id") or contract.normalized_arguments.get("execution_adapter")
        allowed_adapters = [str(adapter_id)] if adapter_id else None
    max_runs = 2 if contract.tool == "edit_isolated" else max(1, len(task_types))
    approvals.add(
        backend="codex_cli",
        data_boundary="hosted_provider",
        task_types=task_types,
        duration_days=1,
        reason=(
            f"Created by Harness autonomy profile {autonomy_scope} for action contract {contract.id}."
            if autonomy_scope
            else f"Created from confirmed Harness action contract {contract.id}."
        ),
        allowed_adapters=allowed_adapters,
        max_runs=max_runs,
        max_total_runtime_seconds=HOSTED_CODEX_APPROVAL_RUNTIME_SECONDS,
        autonomy_scope=autonomy_scope,
    )
    if autonomy_scope:
        return [
            f"Prepared autonomous hosted-provider Codex authority for profile {autonomy_scope}.",
            "This permits scoped Codex planning/edit execution only; active repo apply-back remains separate.",
        ]
    return [
        "Prepared required hosted-provider Codex approval for this confirmed action contract.",
        "This permits scoped Codex planning/edit execution only; apply-back still requires a separate approval.",
    ]


def _ensure_scoped_hosted_codex_approval_for_tasks(
    project_root: Path,
    tasks: list[TaskRecord],
    *,
    reason: str,
    autonomy_scope: str | None = None,
) -> list[str]:
    hosted_tasks = [
        task
        for task in tasks
        if _task_uses_hosted_codex_adapter(task)
    ]
    if not hosted_tasks:
        return []
    approvals = ApprovalStore(project_root)
    missing_tasks = [
        task
        for task in hosted_tasks
        if approvals.find_valid(
            "codex_cli",
            "hosted_provider",
            str(task.metadata.get("task_type") or ""),
            adapter_id=str(task.metadata.get("execution_adapter") or ""),
            workbench_id=task.workbench_id,
            objective_id=task.objective_id,
            autonomy_scope=autonomy_scope,
            strict_scope=autonomy_scope is not None,
        )
        is None
    ]
    if not missing_tasks:
        return []

    allowed_adapters = sorted({str(task.metadata.get("execution_adapter") or "") for task in hosted_tasks})
    allowed_workbenches = sorted({str(task.workbench_id) for task in hosted_tasks if task.workbench_id})
    allowed_objective_ids = sorted({str(task.objective_id) for task in hosted_tasks if task.objective_id})
    task_types = sorted({str(task.metadata.get("task_type") or "") for task in hosted_tasks})
    approvals.add(
        backend="codex_cli",
        data_boundary="hosted_provider",
        task_types=task_types,
        duration_days=1,
        reason=reason,
        allowed_adapters=allowed_adapters,
        allowed_workbenches=allowed_workbenches,
        allowed_objective_ids=allowed_objective_ids,
        max_runs=len(hosted_tasks),
        max_total_runtime_seconds=HOSTED_CODEX_APPROVAL_RUNTIME_SECONDS,
        autonomy_scope=autonomy_scope,
    )
    scope_parts = [
        f"adapters={','.join(allowed_adapters)}",
        f"task_types={','.join(task_types)}",
        f"max_runs={len(hosted_tasks)}",
    ]
    if allowed_workbenches:
        scope_parts.append(f"workbenches={','.join(allowed_workbenches)}")
    if allowed_objective_ids:
        scope_parts.append(f"objectives={','.join(allowed_objective_ids)}")
    return [
        "Prepared scoped hosted-provider Codex approval for this confirmed workflow.",
        f"Scope: {' '.join(scope_parts)}.",
        "This does not permit apply-back, active repo writes, shell, Docker, or arbitrary network.",
    ]


def _task_uses_hosted_codex_adapter(task: TaskRecord) -> bool:
    adapter_id = str(task.metadata.get("execution_adapter") or "")
    task_type = str(task.metadata.get("task_type") or "")
    return HOSTED_CODEX_ADAPTER_TASK_TYPES.get(adapter_id) == task_type


def _hosted_codex_task_types_for_contract(contract: ActionContract) -> list[str]:
    if contract.tool == "edit_isolated":
        return ["codex_code_edit", "repo_planning"]
    task_types: set[str] = set()
    if contract.tool == "dispatch_registered_adapter":
        task_type = contract.normalized_arguments.get("task_type")
        if isinstance(task_type, str) and task_type:
            task_types.add(task_type)
    if contract.tool == "create_task":
        adapter_id = contract.normalized_arguments.get("execution_adapter")
        task_type = contract.normalized_arguments.get("task_type")
        if isinstance(adapter_id, str) and isinstance(task_type, str):
            if HOSTED_CODEX_ADAPTER_TASK_TYPES.get(adapter_id) == task_type:
                task_types.add(task_type)
    if contract.tool == "create_task_graph":
        for task in contract.normalized_arguments.get("tasks") or []:
            if not isinstance(task, dict):
                continue
            adapter_id = task.get("execution_adapter")
            task_type = task.get("task_type")
            if isinstance(adapter_id, str) and isinstance(task_type, str):
                if HOSTED_CODEX_ADAPTER_TASK_TYPES.get(adapter_id) == task_type:
                    task_types.add(task_type)
    return sorted(task_types)


def _hosted_codex_adapter_for_contract_task(contract: ActionContract, task_type: str) -> str | None:
    if contract.tool == "edit_isolated":
        if task_type == "repo_planning":
            return "repo_planning"
        if task_type == "codex_code_edit":
            return "codex_isolated_edit"
    if contract.tool == "dispatch_registered_adapter":
        adapter_id = contract.normalized_arguments.get("adapter_id") or contract.normalized_arguments.get("execution_adapter")
        return str(adapter_id) if adapter_id else None
    return None


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
        if (
            _policy_allows_hosted_provider_autonomy(policy_id)
            and request.tool_name == "edit_isolated"
            and request.boundary == "hosted_provider_codex"
            and request.adapter_id == "codex_isolated_edit"
        ):
            return decision.model_copy(
                update={
                    "reasons": [
                        *decision.reasons,
                        f"adapter hosted boundary is auto-authorized for isolated edit by autonomy profile: {descriptor.id}",
                    ]
                }
            )
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


def _policy_allows_hosted_provider_autonomy(policy_id: str) -> bool:
    try:
        return bool(get_builtin_autonomy_policy(policy_id).allow_hosted_provider_autonomy)
    except KeyError:
        return False


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
        allow_internal_hosted_provider_authority=(
            contract.tool == "edit_isolated"
            and boundary == "hosted_provider_codex"
            and adapter_id == "codex_isolated_edit"
        ),
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


def _stable_idempotency_key(prefix: str, payload: dict[str, Any]) -> str:
    stable = json.dumps(
        sanitize_for_logging(payload),
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    )
    return f"{prefix}:{hashlib.sha256(stable.encode('utf-8')).hexdigest()[:24]}"


def _orchestration_objective_idempotency_key(source_key: str) -> str:
    return _stable_idempotency_key("orchestration_objective", {"source_key": source_key})


def _orchestration_task_idempotency_key(source_key: str, index: int, task_payload: dict[str, Any]) -> str:
    return _stable_idempotency_key(
        "orchestration_task",
        {"source_key": source_key, "task_index": index, "task": task_payload},
    )


def _orchestration_checkpoint_idempotency_key(
    source_key: str,
    objective_id: str,
    index: int,
    checkpoint_payload: dict[str, Any],
) -> str:
    return _stable_idempotency_key(
        "orchestration_checkpoint",
        {
            "source_key": source_key,
            "objective_id": objective_id,
            "checkpoint_index": index,
            "checkpoint": checkpoint_payload,
        },
    )


def _find_objective_by_orchestration_key(store: SQLiteStore, idempotency_key: str) -> Any | None:
    for objective in store.list_objectives():
        if objective.metadata.get("orchestration_idempotency_key") == idempotency_key:
            return objective
    return None


def _create_or_reuse_orchestration_objective(
    store: SQLiteStore,
    *,
    title: str,
    description: str,
    priority: int,
    workbench_id: str | None,
    metadata: dict[str, Any],
    idempotency_key: str,
) -> tuple[Any, bool]:
    existing = _find_objective_by_orchestration_key(store, idempotency_key)
    if existing is not None:
        return existing, True
    return (
        store.create_objective(
            title=title,
            description=description,
            priority=priority,
            workbench_id=workbench_id,
            metadata={**metadata, "orchestration_idempotency_key": idempotency_key},
        ),
        False,
    )


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


def _checkpoint_drafts_from_payloads(raw_checkpoints: Any) -> list[OrchestratedCheckpointDraft]:
    if raw_checkpoints in (None, ""):
        return []
    if not isinstance(raw_checkpoints, list):
        raise ValueError("checkpoint payloads must be a list")
    drafts: list[OrchestratedCheckpointDraft] = []
    for index, raw_checkpoint in enumerate(raw_checkpoints):
        if not isinstance(raw_checkpoint, dict):
            raise ValueError(f"checkpoint[{index}] must be an object")
        label = str(sanitize_for_logging(raw_checkpoint.get("label") or "")).strip()
        if not label:
            raise ValueError(f"checkpoint[{index}] label is required")
        required = raw_checkpoint.get("required", True)
        if not isinstance(required, bool):
            raise ValueError(f"checkpoint[{index}] required must be a boolean")
        metadata = raw_checkpoint.get("metadata") if isinstance(raw_checkpoint.get("metadata"), dict) else {}
        drafts.append(
            OrchestratedCheckpointDraft(
                label=label,
                reason=str(sanitize_for_logging(raw_checkpoint.get("reason") or "")).strip(),
                required=required,
                actor=str(sanitize_for_logging(raw_checkpoint.get("actor") or "harness_chat")).strip()
                or "harness_chat",
                metadata=dict(metadata),
            )
        )
    return drafts


def _create_and_approve_orchestration_checkpoints(
    project_root: Path,
    objective_id: str,
    checkpoints: list[OrchestratedCheckpointDraft],
    *,
    idempotency_key: str,
    approval_id: str,
    actor: str,
    verdict_reason: str,
    metadata: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if not checkpoints:
        return records
    for index, checkpoint in enumerate(checkpoints):
        payload = checkpoint.to_payload()
        checkpoint_idempotency_key = _orchestration_checkpoint_idempotency_key(
            idempotency_key,
            objective_id,
            index,
            payload,
        )
        existing = _find_orchestration_checkpoint(project_root, objective_id, checkpoint_idempotency_key)
        if existing is not None:
            if existing.status == "pending":
                existing = resolve_objective_checkpoint(
                    project_root,
                    objective_id,
                    existing.checkpoint_id,
                    verdict="approved",
                    reason=verdict_reason,
                    approval_id=approval_id,
                    actor=actor,
                )
            records.append(existing.model_dump(mode="json"))
            continue
        checkpoint_metadata = {
            **dict(payload.get("metadata") or {}),
            **dict(metadata or {}),
            "checkpoint_idempotency_key": checkpoint_idempotency_key,
            "checkpoint_template_index": index,
            "approval_source": actor,
        }
        created = create_objective_checkpoint(
            project_root,
            objective_id,
            label=str(payload["label"]),
            reason=str(payload.get("reason") or ""),
            required=bool(payload.get("required", True)),
            actor=str(payload.get("actor") or actor),
            metadata=checkpoint_metadata,
        )
        resolved = resolve_objective_checkpoint(
            project_root,
            objective_id,
            created.checkpoint_id,
            verdict="approved",
            reason=verdict_reason,
            approval_id=approval_id,
            actor=actor,
        )
        records.append(resolved.model_dump(mode="json"))
    return records


def _find_orchestration_checkpoint(
    project_root: Path,
    objective_id: str,
    checkpoint_idempotency_key: str,
) -> Any | None:
    try:
        projection = list_objective_checkpoints(project_root, objective_id)
    except KeyError:
        return None
    for checkpoint in projection.checkpoints:
        if checkpoint.metadata.get("checkpoint_idempotency_key") == checkpoint_idempotency_key:
            return checkpoint
    return None


def _checkpoint_evidence_lines(records: list[dict[str, Any]]) -> list[str]:
    if not records:
        return []
    return [
        f"Approved supervisor checkpoints: {len(records)}",
        *[
            f"- {record.get('checkpoint_id')}: {record.get('label')} [{record.get('status')}]"
            for record in records
        ],
    ]


def _confirm_create_task_graph_contract(project_root: Path, state: ChatSessionState, contract: ActionContract) -> dict[str, Any]:
    store = _require_store(project_root)
    args = contract.normalized_arguments
    checkpoint_drafts = _checkpoint_drafts_from_payloads(args.get("checkpoints") or [])
    contract_idempotency_key = _contract_idempotency_key(contract)
    objective_idempotency_key = _orchestration_objective_idempotency_key(contract_idempotency_key)
    objective, objective_reused = _create_or_reuse_orchestration_objective(
        store,
        title=str(args["goal"]),
        description=str(args["goal"]),
        priority=0,
        workbench_id=args.get("workbench_id"),
        metadata={
            "created_from": "chat_action_contract",
            "contract_id": contract.id,
            "tool": contract.tool,
            "contract_idempotency_key": contract_idempotency_key,
        },
        idempotency_key=objective_idempotency_key,
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
                arguments={**raw_task, "objective_id": objective.id, "depends_on": depends_on},
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
                depends_on=list(task_args.get("depends_on") or depends_on),
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
    checkpoint_records = _create_and_approve_orchestration_checkpoints(
        project_root,
        objective.id,
        checkpoint_drafts,
        idempotency_key=objective_idempotency_key,
        approval_id=_stable_idempotency_key(
            "action_contract_confirm",
            {"source_key": objective_idempotency_key, "objective_id": objective.id},
        ),
        actor="harness_chat",
        verdict_reason="Confirmed chat action contract before task graph creation.",
        metadata={
            "created_from": "chat_action_contract",
            "contract_id": contract.id,
            "tool": contract.tool,
            "template_id": args.get("template_id"),
            "task_count": len(created_tasks),
        },
    )
    state.latest_orchestration = {
        "contract": contract.to_payload(),
        "objective": objective.model_dump(mode="json"),
        "tasks": [task.model_dump(mode="json") for task in created_tasks],
        "checkpoints": checkpoint_records,
    }
    lines = [
        f"Objective: {objective.id}",
        *(
            ["Idempotency: reused existing objective graph for this confirmation."]
            if objective_reused
            else []
        ),
        f"Tasks: {len(created_tasks)}",
        *_checkpoint_evidence_lines(checkpoint_records),
        "Next: ask me to inspect progress or continue execution.",
    ]
    return _response(
        "action_contract_executed",
        "Task Graph Created",
        lines,
        ok=True,
        extra={
            "contract": contract.to_payload(),
            "objective": objective.model_dump(mode="json"),
            "tasks": [task.model_dump(mode="json") for task in created_tasks],
            "checkpoints": checkpoint_records,
            "orchestration": state.latest_orchestration,
        },
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
    draft = _orchestration_from_template(template, state, project_root)
    draft.objective_title = str(contract.normalized_arguments.get("title") or draft.objective_title)
    draft.objective_description = goal
    draft.idempotency_key = _contract_idempotency_key(contract)
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


def _deny_pending_session_tool_permission(
    project_root: Path,
    state: ChatSessionState,
    *,
    feedback: str | None = None,
) -> dict[str, Any] | None:
    if state.pending_session_tool_call is None:
        return None
    pending = dict(state.pending_session_tool_call)
    permission_id = str(pending.get("permission_id") or "")
    if not permission_id:
        return None
    pending_root = resolve_project_root(str(pending.get("project_root") or project_root))
    if not _is_initialized(pending_root):
        return None
    try:
        store = _require_store(pending_root)
        permission = store.resolve_session_permission(
            permission_id,
            SessionPermissionStatus.DENIED,
            reason=feedback or "Declined from harness chat confirmation.",
        )
        denial = persist_session_tool_denial(store, permission.session_id, permission_id, feedback=feedback)
        task_operator_resume = apply_operator_task_permission_resolution(
            pending_root,
            permission.session_id,
            permission_id,
            status=SessionPermissionStatus.DENIED,
            feedback=feedback,
        )
        if task_operator_resume is not None:
            denial["task_operator_resume"] = task_operator_resume
        return denial
    except (KeyError, ValueError):
        return None


def _decline_response_lines(denial: dict[str, Any] | None) -> list[str]:
    lines = ["No action was taken."]
    if denial is None:
        return lines
    card = denial.get("approval_card") if isinstance(denial.get("approval_card"), dict) else {}
    if card:
        lines.append(f"Denied approval: {card.get('approval_id') or denial.get('permission_id')}")
        lines.append(f"Tool: {card.get('tool_id') or denial.get('tool_id')}")
    feedback = denial.get("feedback")
    if feedback:
        lines.append(f"Feedback: {feedback}")
    error = denial.get("model_visible_error")
    if error:
        lines.append("Model-visible tool error recorded.")
    return lines


def _confirm_pending(project_root: Path, state: ChatSessionState) -> dict[str, Any]:
    if state.pending_session_tool_call is not None:
        state.operator_runtime.resume_waiting_turn()
        pending = dict(state.pending_session_tool_call)
        state.pending_session_tool_call = None
        pending_root = resolve_project_root(str(pending.get("project_root") or project_root))
        if not _is_initialized(pending_root):
            return _uninitialized_response(pending_root)
        store = _require_store(pending_root)
        permission_id = str(pending.get("permission_id") or "")
        if permission_id:
            store.resolve_session_permission(
                permission_id,
                SessionPermissionStatus.ALLOWED,
                reason="Approved from harness chat confirmation.",
            )
        state.active_project_root = str(pending_root)
        state.session_id = str(pending.get("session_id") or state.session_id or "")
        response = _run_session_tool_response(
            pending_root,
            state,
            str(pending.get("tool_id") or ""),
            dict(pending.get("arguments") or {}),
        )
        if permission_id:
            result_payload = response.get("result") if isinstance(response.get("result"), dict) else None
            task_operator_resume = apply_operator_task_permission_resolution(
                pending_root,
                state.session_id,
                permission_id,
                status=SessionPermissionStatus.ALLOWED,
                resumed_result=result_payload,
            )
            if task_operator_resume is not None:
                response["task_operator_resume"] = task_operator_resume
        _settle_operator_turn(pending_root, state, response)
        return response
    if state.pending_action_contract is not None:
        if not _is_initialized(project_root):
            return _uninitialized_response(project_root)
        contract = state.pending_action_contract
        state.pending_action_contract = None
        response = _execute_action_contract(project_root, state, contract)
        _persist_pending_chat_action(project_root, state)
        return response
    if state.pending_hosted_approval:
        if not _is_initialized(project_root):
            return _uninitialized_response(project_root)
        scoped_tasks: list[TaskRecord] = []
        store = _require_store(project_root)
        if state.latest_lease_id:
            try:
                lease = store.get_task_lease(state.latest_lease_id)
                scoped_tasks.append(store.get_task(lease.task_id))
            except KeyError:
                scoped_tasks = []
        if not scoped_tasks and state.latest_objective_id:
            scoped_tasks = store.list_tasks(objective_id=state.latest_objective_id)
        approval_lines = _ensure_scoped_hosted_codex_approval_for_tasks(
            project_root,
            scoped_tasks,
            reason="Created from explicit harness chat confirmation for the latest blocked hosted Codex task.",
        )
        state.pending_hosted_approval = False
        lines = [
            *(approval_lines or ["No scoped hosted Codex task is available for approval."]),
            "It is not apply-back approval.",
        ]
        if state.latest_lease_id:
            lines.append("Type /run to continue the latest active lease.")
        response = _response("hosted_approval_created", "Hosted Approval Created", lines, ok=True)
        _persist_pending_chat_action(project_root, state)
        return response
    if state.pending_draft is not None:
        if not _is_initialized(project_root):
            return _uninitialized_response(project_root)
        draft = state.pending_draft
        state.pending_draft = None
        task = _create_task_from_draft(project_root, state, draft)
        if state.codex_like_mode:
            response = _run_single_task_response(project_root, state, task)
        else:
            response = _task_created_response(task, draft)
        _persist_pending_chat_action(project_root, state)
        return response
    if state.pending_orchestration is not None:
        if not _is_initialized(project_root):
            return _uninitialized_response(project_root)
        draft = state.pending_orchestration
        state.pending_orchestration = None
        response = _create_and_run_orchestration(project_root, state, draft)
        _persist_pending_chat_action(project_root, state)
        return response
    if state.pending_execute_lease_id is not None:
        lease_id = state.pending_execute_lease_id
        state.pending_execute_lease_id = None
        approval_lines = []
        if _is_initialized(project_root):
            store = _require_store(project_root)
            try:
                lease = store.get_task_lease(lease_id)
                task = store.get_task(lease.task_id)
                approval_lines = _ensure_scoped_hosted_codex_approval_for_tasks(
                    project_root,
                    [task],
                    reason=f"Created from confirmed registered adapter dispatch for lease {lease_id}.",
                )
            except KeyError:
                approval_lines = []
        response = _execute_response(project_root, lease_id, state)
        _persist_pending_chat_action(project_root, state)
        if approval_lines:
            response["lines"] = [*approval_lines, *response.get("lines", [])]
            response["auto_approvals"] = approval_lines
        return response
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
    approval_lines = _ensure_scoped_hosted_codex_approval_for_tasks(
        project_root,
        [task],
        reason=f"Created from confirmed foreground chat task {task.id} before leasing.",
    )
    tick = store.daemon_run_once(owner=ORCHESTRATION_OWNER, pid=None)
    lines = [f"Task created: {task.id}", *approval_lines]
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
    lease_approval_lines = _ensure_scoped_hosted_codex_approval_for_tasks(
        project_root,
        [tick.selected_task or task],
        reason=f"Created from confirmed foreground chat task {task.id} after lease {tick.lease.id}.",
    )
    lines.extend(lease_approval_lines)
    result_response = _execute_response(project_root, tick.lease.id, state)
    result_lines = result_response.get("lines", [])
    response = _response(
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
    all_approval_lines = [*approval_lines, *lease_approval_lines]
    if all_approval_lines:
        response["auto_approvals"] = all_approval_lines
    return response


def _draft_response(project_root: Path, state: ChatSessionState, draft: ChatDraftTask) -> dict[str, Any]:
    state.pending_draft = draft
    _persist_pending_chat_action(project_root, state)
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
    _persist_pending_chat_action(project_root, state)
    task_lines = [
        f"{idx + 1}. {task.agent_id}: {task.title}"
        + f" adapter={task.execution_adapter} task_type={task.task_type}"
        + (f" depends_on={','.join(str(i + 1) for i in task.depends_on_indexes)}" if task.depends_on_indexes else "")
        for idx, task in enumerate(draft.tasks)
    ]
    checkpoint_lines = [
        f"{idx + 1}. {'required' if checkpoint.required else 'optional'}: {checkpoint.label}"
        for idx, checkpoint in enumerate(draft.checkpoints)
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
            *(
                [
                    "Supervisor checkpoints:",
                    *checkpoint_lines,
                    "On confirmation, Harness records and approves these checkpoints as objective evidence.",
                ]
                if checkpoint_lines
                else []
            ),
            f"Required approvals: {draft.required_approvals}",
            "Safety boundary:",
            *[f"- {note}" for note in draft.safety_notes],
            *(
                [
                    "On confirmation, Harness creates a scoped hosted-provider approval for this workflow only.",
                    "The approval is constrained by adapter, task type, workbench/objective, run count, and expiry.",
                ]
                if "hosted_provider_codex" in draft.required_approvals
                else []
            ),
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


def _orchestration_from_template(
    template: WorkflowTemplate,
    state: ChatSessionState,
    project_root: Path,
) -> OrchestratedRunDraft:
    tasks = []
    discovery_cards_by_workbench: dict[str, list[AgentDiscoveryCard]] = {}
    for task in template.tasks:
        workbench_id = task.workbench_id or "coding"
        if workbench_id not in discovery_cards_by_workbench:
            catalog = build_agent_discovery_catalog(
                project_root,
                workbench_id=workbench_id,
                include_sample_allocation=False,
            )
            if not catalog.ok:
                raise ValueError(
                    "Workflow agent discovery failed: "
                    f"workbench={workbench_id} errors={catalog.validation_errors} summary={catalog.summary}"
                )
            discovery_cards_by_workbench[workbench_id] = catalog.cards
        selected_agent_id, allocation_receipt = _allocate_workflow_task_agent(
            project_root,
            task,
            cards=discovery_cards_by_workbench[workbench_id],
        )
        tasks.append(
            OrchestratedTaskDraft(
                title=task.title,
                description=task.description,
                agent_id=selected_agent_id,
                workbench_id=workbench_id,
                execution_adapter=task.execution_adapter,
                task_type=task.task_type,
                depends_on_indexes=list(task.depends_on_indexes),
                priority=task.priority,
                metadata={
                    **task.metadata(),
                    "agent_selection_source": WORKFLOW_AGENT_SELECTION_SOURCE,
                    "delegate_allocation_schema_version": DELEGATE_ALLOCATION_SCHEMA_VERSION,
                    "delegate_allocation": allocation_receipt,
                },
                agent_selection=None if task.agent_selection is None else task.agent_selection.to_payload(),
            )
        )
    orchestrator_id = _active_orchestrator_id(state)
    workbench_id = tasks[0].workbench_id if tasks else _workbench_for_orchestrator(orchestrator_id)
    return OrchestratedRunDraft(
        objective_title=template.objective_title,
        objective_description=template.objective_description,
        orchestrator_id=orchestrator_id,
        workbench_id=workbench_id,
        tasks=tasks,
        checkpoints=[
            OrchestratedCheckpointDraft(
                label=checkpoint.label,
                reason=checkpoint.reason,
                required=checkpoint.required,
                actor="harness_chat",
                metadata=checkpoint.metadata,
            )
            for checkpoint in template.checkpoints
        ],
        interpreted_intent=template.interpreted_intent,
        proposed_action=template.proposed_action,
        required_approvals=template.required_approvals,
        safety_notes=template.safety_boundary,
        equivalent_commands=template.equivalent_commands,
        confirm_prompt=template.confirm_prompt,
    )


def _allocate_workflow_task_agent(
    project_root: Path,
    task: WorkflowTaskTemplate,
    *,
    cards: list[AgentDiscoveryCard],
) -> tuple[str, dict[str, Any]]:
    requirements = _workflow_task_allocation_requirements(task)
    allocation = evaluate_delegate_allocation(
        project_root,
        workbench_id=task.workbench_id or "coding",
        task_type=task.task_type,
        required_kind=requirements["required_kind"],
        required_tool_policy_id=requirements["required_tool_policy_id"],
        required_outputs=requirements["required_outputs"],
        required_tags=requirements["required_tags"],
        max_candidates=1,
        cards=cards,
    )
    if not allocation.ok or len(allocation.selected_agent_ids) != 1:
        raise ValueError(
            "Workflow task agent allocation failed: "
            f"{task.title} selected={allocation.selected_agent_ids} summary={allocation.summary}"
        )
    selected_agent_id = allocation.selected_agent_ids[0]
    selected_bid = next((bid for bid in allocation.bids if bid.bid_id in set(allocation.selected_bid_ids)), None)
    if selected_bid is None:
        raise ValueError(f"Workflow task agent allocation missing selected bid for {task.title}.")
    receipt = {
        "schema_version": DELEGATE_ALLOCATION_SCHEMA_VERSION,
        "selection_source": WORKFLOW_AGENT_SELECTION_SOURCE,
        "announcement_id": allocation.announcement.announcement_id,
        "selected_agent_id": selected_agent_id,
        "selected_bid_id": selected_bid.bid_id,
        "eligible_count": allocation.summary.get("eligible_count", 0),
        "requirements": requirements,
        "selected_bid": {
            "score": selected_bid.score,
            "matched": selected_bid.matched,
            "bid_terms": selected_bid.bid_terms,
        },
        "safety": {
            "read_only": allocation.safety.get("read_only") is True,
            "metadata_only": allocation.safety.get("metadata_only") is True,
            "provider_called": allocation.safety.get("provider_called") is True,
            "network_called": allocation.safety.get("network_called") is True,
            "agent_execution_started": allocation.safety.get("agent_execution_started") is True,
            "tool_execution_started": allocation.safety.get("tool_execution_started") is True,
            "adapter_execution_started": allocation.safety.get("adapter_execution_started") is True,
            "process_started": allocation.safety.get("process_started") is True,
            "filesystem_modified": allocation.safety.get("filesystem_modified") is True,
            "permission_granting": allocation.safety.get("permission_granting") is True,
            "budget_granting": allocation.safety.get("budget_granting") is True,
        },
    }
    return selected_agent_id, sanitize_for_logging(receipt)


def _workflow_task_allocation_requirements(task: WorkflowTaskTemplate) -> dict[str, Any]:
    metadata = task.metadata()
    workflow_stage = str(metadata.get("workflow_stage") or "")
    if task.agent_selection is not None:
        selection = task.agent_selection
        return {
            "schema_version": selection.to_payload()["schema_version"],
            "source": "workflow_template",
            "workbench_id": task.workbench_id or "coding",
            "required_kind": selection.required_kind,
            "required_tool_policy_id": selection.required_tool_policy_id,
            "required_outputs": list(selection.required_outputs),
            "required_tags": list(selection.required_tags),
            "task_type": task.task_type,
            "workflow_stage": workflow_stage or None,
        }
    required_kind: str | None = None
    required_tool_policy_id: str | None = None
    required_outputs: list[str] = []
    required_tags: list[str] = []

    if task.execution_adapter == "review_gate":
        required_kind = "reviewer"
        required_tool_policy_id = "read_only"
        required_outputs = [f"{task.task_type}.md"]
        if task.task_type == "security_review":
            required_tags = ["security"]
        elif task.task_type in {"implementation_review", "factuality_review"}:
            required_tags = ["review"]
    elif task.execution_adapter == "codex_isolated_edit":
        required_kind = "specialist"
        required_tool_policy_id = "isolated_code_edit"
        required_outputs = ["patch_summary.md"]
    elif task.execution_adapter == "repo_planning":
        required_kind = "specialist"
        required_tool_policy_id = "read_only"
        required_outputs = ["repo_summary.md"]
    elif task.execution_adapter == "read_only_summary":
        required_kind = "specialist"
        required_tool_policy_id = "read_only"
        required_outputs = ["repo_summary.md"]
    elif task.task_type == "phase_1a_test" and workflow_stage == "test_sandbox":
        required_kind = "specialist"
        required_tool_policy_id = "docker_test"
        required_outputs = ["test_report.md"]
    elif workflow_stage in {"final_report", "research_synthesis"}:
        required_kind = "orchestrator"
        required_outputs = ["final_orchestrator_summary.md"]
        required_tags = ["orchestrator"]
    else:
        required_kind = "specialist"
        required_tool_policy_id = "read_only"
        required_outputs = ["repo_summary.md"]

    return {
        "source": "compatibility_inference",
        "workbench_id": task.workbench_id or "coding",
        "required_kind": required_kind,
        "required_tool_policy_id": required_tool_policy_id,
        "required_outputs": required_outputs,
        "required_tags": required_tags,
        "task_type": task.task_type,
        "workflow_stage": workflow_stage or None,
    }


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
    cwd_payload: dict[str, Any] | None = None
    if state is not None and _is_initialized(project_root):
        try:
            store, session_id = _ensure_chat_session(project_root, state)
            session = store.get_session(session_id)
            cwd_payload = session_cwd_payload(project_root, session.metadata, load_config(project_root).context_excludes)
            lines.append(f"Session cwd: {cwd_payload['cwd']}")
            lines.append(f"Resolved cwd: {cwd_payload['resolved_abs_path']}")
        except Exception:
            cwd_payload = None
    return _response("status", "Project State", lines, ok=True, extra={"context": context, "session_cwd": cwd_payload})


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
    if progress.objective_evidence:
        lines.append(f"Objective evidence: {'pass' if progress.objective_evidence.get('ok') else 'fail'}")
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
    _persist_pending_chat_action(project_root, state)
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
    try:
        owner = _require_store(project_root).get_task_lease(lease_id).owner
    except KeyError:
        owner = "chat_cli"
    result = execute_lease(project_root, lease_id, owner=owner)
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
        _persist_pending_chat_action(project_root, state)
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
    objective_idempotency_key = _orchestration_objective_idempotency_key(draft.idempotency_key)
    # Cancel stale open objectives from the same workbench to prevent task pileup
    for existing in store.list_objectives():
        same_graph = existing.metadata.get("orchestration_idempotency_key") == objective_idempotency_key
        if (
            existing.workbench_id == draft.workbench_id
            and existing.status == "active"
            and existing.id != state.latest_objective_id
            and not same_graph
        ):
            for task in store.list_tasks(objective_id=existing.id):
                if task.status.value in {"ready", "leased", "blocked", "waiting_approval", "created"}:
                    store.cancel_task(task.id)
    objective, objective_reused = _create_or_reuse_orchestration_objective(
        store,
        title=draft.objective_title,
        description=draft.objective_description,
        priority=1000,
        workbench_id=draft.workbench_id,
        metadata={
            "created_by": "harness_chat",
            "orchestrator_id": draft.orchestrator_id,
            "execution_adapter": CODEX_ORCHESTRATION_ADAPTER,
            "draft_idempotency_key": draft.idempotency_key,
        },
        idempotency_key=objective_idempotency_key,
    )
    created_tasks: list[TaskRecord] = []
    for idx, task_draft in enumerate(draft.tasks):
        depends_on = [created_tasks[dep_idx].id for dep_idx in task_draft.depends_on_indexes]
        task_idempotency_key = _orchestration_task_idempotency_key(
            objective_idempotency_key,
            idx,
            task_draft.to_payload(),
        )
        task = store.create_task(
            title=task_draft.title,
            description=task_draft.description,
            priority=1000 - idx,
            objective_id=objective.id,
            workbench_id=task_draft.workbench_id,
            agent_id=task_draft.agent_id,
            spec_source_kind="builtin",
            depends_on=depends_on,
            idempotency_key=task_idempotency_key,
            metadata={
                **dict(task_draft.metadata),
                "execution_adapter": task_draft.execution_adapter,
                "task_type": task_draft.task_type,
                "chat_orchestrated": True,
                "orchestrator_id": draft.orchestrator_id,
                "workflow_intent": draft.interpreted_intent,
                "idempotency_key": task_idempotency_key,
            },
        )
        created_tasks.append(task)
    state.latest_objective_id = objective.id
    state.latest_task_id = created_tasks[0].id if created_tasks else None
    checkpoint_records = _create_and_approve_orchestration_checkpoints(
        project_root,
        objective.id,
        draft.checkpoints,
        idempotency_key=objective_idempotency_key,
        approval_id=_stable_idempotency_key(
            "chat_orchestration_confirm",
            {"source_key": objective_idempotency_key, "objective_id": objective.id},
        ),
        actor="harness_chat",
        verdict_reason="Operator confirmed the orchestration draft before foreground run.",
        metadata={
            "created_from": "chat_orchestration_draft",
            "orchestrator_id": draft.orchestrator_id,
            "workflow_intent": draft.interpreted_intent,
            "task_count": len(created_tasks),
        },
    )
    state.latest_orchestration = {
        "draft": draft.to_payload(),
        "objective": objective.model_dump(mode="json"),
        "tasks": [task.model_dump(mode="json") for task in created_tasks],
        "checkpoints": checkpoint_records,
    }
    approval_lines = _ensure_scoped_hosted_codex_approval_for_tasks(
        project_root,
        created_tasks,
        reason=f"Created from confirmed chat orchestration for objective {objective.id}.",
    )
    state.progress.append(
        f"orchestration objective {'reused' if objective_reused else 'created'}: {objective.id}"
    )
    response = _run_orchestration_loop(project_root, state, objective.id)
    if objective_reused:
        response["lines"] = [
            "Idempotency: reused existing objective graph for this confirmation.",
            *response.get("lines", []),
        ]
        response["idempotency"] = {
            "objective_reused": True,
            "orchestration_idempotency_key": objective_idempotency_key,
        }
    checkpoint_lines = _checkpoint_evidence_lines(checkpoint_records)
    if checkpoint_lines:
        response["lines"] = [*checkpoint_lines, *response.get("lines", [])]
        response["checkpoint_evidence"] = checkpoint_records
    if approval_lines:
        response["lines"] = [*approval_lines, *response.get("lines", [])]
        response["auto_approvals"] = approval_lines
    return response


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
        tick = store.daemon_run_once(owner=ORCHESTRATION_OWNER, pid=None, objective_id=objective_id)
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
        if result.errors:
            lines.append(f"Errors: {result.errors}")
        lines.extend(_recovery_lines_for_execution(result))
        lines.extend(_summary_lines_from_result(project_root, result.adapter_result))
        if _needs_hosted_approval(result.rejection_reasons + result.errors):
            state.pending_hosted_approval = True
            _persist_pending_chat_action(project_root, state)
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
    return SQLiteStore.open_initialized(project_root)


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
