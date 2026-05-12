from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from harness.capabilities import build_capability_catalog
from harness.config import DEFAULT_CONTEXT_EXCLUDES, load_config
from harness.context_pack import pack_chat_context
from harness.memory.sqlite_store import SQLiteStore
from harness.operator_context import build_operator_context
from harness.progress import build_orchestration_progress
from harness.registry import builtin_spec_registry
from harness.sandbox_profiles import list_sandbox_profiles
from harness.security import sanitize_for_logging
from harness.tools.base import ToolContext
from harness.tools.readonly import GitDiffTool, ListFilesTool, ReadFileTool


ChatToolRisk = Literal["read", "control_plane_write", "sandboxed_execution", "repo_mutation"]


@dataclass(frozen=True)
class ChatToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    risk: ChatToolRisk
    requires_confirmation: bool
    evidence_required: bool


@dataclass(frozen=True)
class ChatToolRequest:
    type: str
    tool: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ChatToolResult:
    tool: str
    ok: bool
    content: str
    data: dict[str, Any] = field(default_factory=dict)
    evidence_refs: list[str] = field(default_factory=list)
    error_type: str | None = None

    def to_message(self) -> str:
        return json.dumps(
            {
                "type": "harness.tool_result/v1",
                "tool": self.tool,
                "ok": self.ok,
                "content": self.content,
                "data": self.data,
                "evidence_refs": self.evidence_refs,
                "error_type": self.error_type,
            },
            sort_keys=True,
            default=str,
        )


@dataclass(frozen=True)
class ChatToolContext:
    project_root: Path
    context_excludes: list[str]


class ChatTool(Protocol):
    spec: ChatToolSpec

    def run(self, request: ChatToolRequest, context: ChatToolContext) -> ChatToolResult:
        ...


def parse_tool_request(content: str) -> ChatToolRequest | None:
    stripped = content.strip()
    if not stripped:
        return None
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or payload.get("type") != "harness.tool_request/v1":
        return None
    tool = payload.get("tool")
    arguments = payload.get("arguments", {})
    if not isinstance(tool, str) or not isinstance(arguments, dict):
        return None
    return ChatToolRequest(type="harness.tool_request/v1", tool=tool, arguments=arguments)


def default_chat_tools() -> dict[str, ChatTool]:
    tools: list[ChatTool] = [
        RepoTreeChatTool(),
        ReadFileChatTool(),
        SearchRepoChatTool(),
        ShowDiffChatTool(),
        JsonContextTool("show_recent_runs", "Show recent Harness runs.", _recent_runs_payload),
        JsonContextTool("show_progress", "Show current Harness orchestration progress.", _progress_payload),
        JsonContextTool("show_capabilities", "Show Harness capability catalog.", _capabilities_payload),
        JsonContextTool("show_task", "Show one Harness task.", _task_payload),
        JsonContextTool("show_run", "Show one Harness run.", _run_payload),
        JsonContextTool("show_artifact", "Show one Harness artifact metadata record.", _artifact_payload),
        JsonContextTool("explain_policy", "Show Harness security and policy summary.", _policy_payload),
        JsonContextTool("list_agents", "List built-in and imported agents.", _agents_payload),
        JsonContextTool("show_agent", "Show one built-in or imported agent.", _agent_payload),
        JsonContextTool("list_workbenches", "List built-in workbenches.", _workbenches_payload),
        JsonContextTool("list_model_profiles", "List built-in model profiles.", _model_profiles_payload),
        JsonContextTool("list_tool_policies", "List built-in tool policies.", _tool_policies_payload),
        JsonContextTool("list_memory_scopes", "List built-in memory scopes.", _memory_scopes_payload),
        JsonContextTool("show_objectives", "Show Harness objectives.", _objectives_payload),
        JsonContextTool("show_objective", "Show one Harness objective.", _objective_payload),
        JsonContextTool("show_task_graph", "Show the Harness task graph.", _task_graph_payload),
        JsonContextTool("show_leases", "Show Harness task leases.", _leases_payload),
        JsonContextTool("show_lease", "Show one Harness lease.", _lease_payload),
        JsonContextTool("show_registered_adapters", "Show registered execution adapters.", _adapters_payload),
        JsonContextTool("show_adapter", "Show one registered execution adapter.", _adapter_payload),
        JsonContextTool("show_approvals", "Show approval summary.", _approvals_payload),
        JsonContextTool("show_security_summary", "Show security layer summary.", _policy_payload),
        JsonContextTool("show_sandbox_profiles", "Show sandbox profiles.", _sandbox_profiles_payload),
        JsonContextTool("show_trace", "Show trace availability for recent runs.", _trace_payload),
        JsonContextTool("show_apply_back_state", "Show latest apply-back related artifact metadata.", _apply_back_payload),
        JsonContextTool("explain_blocked_state", "Explain blocked state reasons.", _blocked_state_payload),
        GatedActionTool(
            "create_objective",
            "Create a Harness objective after action-contract confirmation.",
            "control_plane_write",
        ),
        GatedActionTool(
            "create_task",
            "Create a Harness task after action-contract confirmation.",
            "control_plane_write",
        ),
        GatedActionTool(
            "create_task_graph",
            "Create a Harness objective/task graph after action-contract confirmation.",
            "control_plane_write",
        ),
        GatedActionTool(
            "request_approval",
            "Request or guide an approval after action-contract confirmation.",
            "control_plane_write",
        ),
        GatedActionTool(
            "dispatch_registered_adapter",
            "Dispatch a registered Harness adapter after validation and confirmation.",
            "sandboxed_execution",
        ),
        GatedActionTool(
            "edit_isolated",
            "Run an isolated edit workflow after validation and confirmation.",
            "repo_mutation",
        ),
        GatedActionTool(
            "run_tests",
            "Run tests through Harness-controlled test execution after confirmation.",
            "sandboxed_execution",
        ),
        GatedActionTool(
            "apply_back",
            "Apply an inspected isolated diff back to the active repo after separate confirmation.",
            "repo_mutation",
        ),
        GatedActionTool(
            "deny_apply_back",
            "Deny an apply-back request after confirmation.",
            "control_plane_write",
        ),
        GatedActionTool(
            "revert_pending_change",
            "Revert a pending Harness-managed change after confirmation.",
            "repo_mutation",
        ),
        GatedActionTool(
            "remember",
            "Create a local memory record after confirmation.",
            "control_plane_write",
        ),
        GatedActionTool(
            "forget_memory",
            "Forget a local memory record after confirmation.",
            "control_plane_write",
        ),
    ]
    return {tool.spec.name: tool for tool in tools}


def run_chat_tool(request: ChatToolRequest, context: ChatToolContext, tools: dict[str, ChatTool] | None = None) -> ChatToolResult:
    registry = tools or default_chat_tools()
    tool = registry.get(request.tool)
    if tool is None:
        return ChatToolResult(
            tool=request.tool,
            ok=False,
            content=f"Unknown chat tool: {request.tool}",
            error_type="unknown_tool",
        )
    if tool.spec.risk != "read" or tool.spec.requires_confirmation:
        return ChatToolResult(
            tool=request.tool,
            ok=False,
            content=(
                "This Harness tool is side-effecting and requires a validated action contract before execution. "
                "Ask the user to confirm the action contract before Harness runs it."
            ),
            data={
                "type": "harness.action_contract_required/v1",
                "tool": request.tool,
                "risk": tool.spec.risk,
                "requires_confirmation": True,
                "arguments": sanitize_for_logging(request.arguments),
            },
            error_type="action_contract_required",
        )
    return tool.run(request, context)


def chat_tool_specs_payload(tools: dict[str, ChatTool] | None = None) -> list[dict[str, Any]]:
    return [tool.spec.__dict__ for tool in (tools or default_chat_tools()).values()]


def default_chat_tool_context(project_root: Path) -> ChatToolContext:
    try:
        excludes = load_config(project_root).context_excludes
    except FileNotFoundError:
        excludes = list(DEFAULT_CONTEXT_EXCLUDES)
    return ChatToolContext(project_root=project_root, context_excludes=list(excludes))


class RepoTreeChatTool:
    spec = ChatToolSpec(
        name="repo_tree",
        description="List non-secret files under the project root.",
        input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
        risk="read",
        requires_confirmation=False,
        evidence_required=False,
    )

    def run(self, request: ChatToolRequest, context: ChatToolContext) -> ChatToolResult:
        result = ListFilesTool().run(
            {"path": request.arguments.get("path", ".")},
            ToolContext(project_root=context.project_root, context_excludes=context.context_excludes),
        )
        return _tool_result(self.spec.name, result.ok, result.output, result.data, result.error_type)


class ReadFileChatTool:
    spec = ChatToolSpec(
        name="read_file",
        description="Read a non-secret UTF-8 text file under the project root.",
        input_schema={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
        risk="read",
        requires_confirmation=False,
        evidence_required=False,
    )

    def run(self, request: ChatToolRequest, context: ChatToolContext) -> ChatToolResult:
        result = ReadFileTool().run(
            {"path": request.arguments.get("path")},
            ToolContext(project_root=context.project_root, context_excludes=context.context_excludes),
        )
        return _tool_result(self.spec.name, result.ok, result.output, result.data, result.error_type)


class SearchRepoChatTool:
    spec = ChatToolSpec(
        name="search_repo",
        description="Search repository text with ripgrep, respecting Harness context excludes.",
        input_schema={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
        risk="read",
        requires_confirmation=False,
        evidence_required=False,
    )

    def run(self, request: ChatToolRequest, context: ChatToolContext) -> ChatToolResult:
        query = str(request.arguments.get("query", "")).strip()
        if not query:
            return ChatToolResult(self.spec.name, False, "Missing query.", error_type="validation")
        command = [
            "rg",
            "--line-number",
            "--hidden",
            "--glob",
            "!*.sqlite",
            "--glob",
            "!*.pem",
            "--glob",
            "!*.key",
            "--glob",
            "!.env",
            "--glob",
            "!.env.*",
            "--glob",
            "!secrets/**",
            "--glob",
            "!.codex/**",
        ]
        for pattern in context.context_excludes:
            command.extend(["--glob", f"!{pattern}"])
        command.extend([query, str(context.project_root)])
        try:
            result = subprocess.run(command, text=True, capture_output=True, timeout=20)
        except FileNotFoundError:
            return ChatToolResult(self.spec.name, False, "ripgrep is not installed.", error_type="missing_rg")
        output = result.stdout.strip()
        if result.returncode not in {0, 1}:
            return ChatToolResult(self.spec.name, False, result.stderr.strip(), error_type="search_error")
        return ChatToolResult(self.spec.name, True, _relativize_search_output(context.project_root, output), data={"query": query})


class ShowDiffChatTool:
    spec = ChatToolSpec(
        name="show_diff",
        description="Show current git diff stat and patch.",
        input_schema={"type": "object", "properties": {}},
        risk="read",
        requires_confirmation=False,
        evidence_required=False,
    )

    def run(self, request: ChatToolRequest, context: ChatToolContext) -> ChatToolResult:
        result = GitDiffTool().run({}, ToolContext(project_root=context.project_root, context_excludes=context.context_excludes))
        return _tool_result(self.spec.name, result.ok, result.output, result.data, result.error_type)


@dataclass
class GatedActionTool:
    name: str
    description: str
    risk: ChatToolRisk

    @property
    def spec(self) -> ChatToolSpec:
        return ChatToolSpec(
            name=self.name,
            description=self.description,
            input_schema={"type": "object", "properties": {}},
            risk=self.risk,
            requires_confirmation=True,
            evidence_required=True,
        )

    def run(self, request: ChatToolRequest, context: ChatToolContext) -> ChatToolResult:
        return ChatToolResult(
            self.name,
            False,
            "This side-effecting Harness tool requires a validated action contract before execution.",
            data={
                "type": "harness.action_contract_required/v1",
                "tool": self.name,
                "risk": self.risk,
                "arguments": sanitize_for_logging(request.arguments),
            },
            error_type="action_contract_required",
        )


@dataclass
class JsonContextTool:
    name: str
    description: str
    payload_builder: Any

    @property
    def spec(self) -> ChatToolSpec:
        return ChatToolSpec(
            name=self.name,
            description=self.description,
            input_schema={"type": "object", "properties": {}},
            risk="read",
            requires_confirmation=False,
            evidence_required=False,
        )

    def run(self, request: ChatToolRequest, context: ChatToolContext) -> ChatToolResult:
        try:
            payload = self.payload_builder(context.project_root, request.arguments)
        except (KeyError, ValueError, OSError) as exc:
            return ChatToolResult(self.name, False, str(exc), error_type=exc.__class__.__name__)
        content = json.dumps(sanitize_for_logging(payload), sort_keys=True, indent=2, default=str)
        return ChatToolResult(self.name, True, content, data={"payload": sanitize_for_logging(payload)})


def _tool_result(name: str, ok: bool, output: str, data: dict[str, Any], error_type: str | None) -> ChatToolResult:
    return ChatToolResult(name, ok, str(sanitize_for_logging(output)), data=sanitize_for_logging(data), error_type=error_type)


def _store(project_root: Path) -> SQLiteStore:
    return SQLiteStore(project_root)


def _operator(project_root: Path) -> dict[str, Any]:
    return build_operator_context(project_root)


def _recent_runs_payload(project_root: Path, args: dict[str, Any]) -> Any:
    return _operator(project_root).get("recent_runs", [])


def _progress_payload(project_root: Path, args: dict[str, Any]) -> Any:
    objective_id = args.get("objective_id")
    if objective_id:
        return build_orchestration_progress(project_root, str(objective_id)).model_dump(mode="json")
    return _operator(project_root).get("progress", {})


def _capabilities_payload(project_root: Path, args: dict[str, Any]) -> Any:
    return build_capability_catalog(project_root).model_dump(mode="json")


def _task_payload(project_root: Path, args: dict[str, Any]) -> Any:
    task_id = args.get("task_id") or args.get("id")
    if not task_id:
        return _operator(project_root).get("tasks", [])
    return _store(project_root).get_task(str(task_id)).model_dump(mode="json")


def _run_payload(project_root: Path, args: dict[str, Any]) -> Any:
    run_id = args.get("run_id") or args.get("id")
    if not run_id:
        return _operator(project_root).get("recent_runs", [])
    return _store(project_root).get_run(str(run_id)).model_dump(mode="json")


def _artifact_payload(project_root: Path, args: dict[str, Any]) -> Any:
    artifact_id = args.get("artifact_id") or args.get("id")
    if artifact_id:
        artifact = _store(project_root).get_artifact(str(artifact_id))
        return artifact.model_dump(mode="json")
    run_id = args.get("run_id")
    if run_id:
        return [artifact.model_dump(mode="json") for artifact in _store(project_root).list_artifacts(str(run_id))]
    return pack_chat_context(project_root).to_payload().get("blocks", [])


def _agents_payload(project_root: Path, args: dict[str, Any]) -> Any:
    registry = builtin_spec_registry()
    payload = {"built_in": {key: value.model_dump(mode="json") for key, value in registry.agents.items()}}
    if (project_root / ".harness" / "harness.sqlite").exists():
        payload["imported"] = [agent.model_dump(mode="json") for agent in _store(project_root).list_project_agents()]
    else:
        payload["imported"] = []
    return payload


def _agent_payload(project_root: Path, args: dict[str, Any]) -> Any:
    agent_id = args.get("agent_id") or args.get("id")
    if not agent_id:
        return _agents_payload(project_root, args)
    registry = builtin_spec_registry()
    if str(agent_id) in registry.agents:
        return registry.agents[str(agent_id)].model_dump(mode="json")
    return _store(project_root).get_project_agent(str(agent_id)).model_dump(mode="json")


def _workbenches_payload(project_root: Path, args: dict[str, Any]) -> Any:
    return {key: value.model_dump(mode="json") for key, value in builtin_spec_registry().workbenches.items()}


def _model_profiles_payload(project_root: Path, args: dict[str, Any]) -> Any:
    return {key: value.model_dump(mode="json") for key, value in builtin_spec_registry().model_profiles.items()}


def _tool_policies_payload(project_root: Path, args: dict[str, Any]) -> Any:
    return {key: value.model_dump(mode="json") for key, value in builtin_spec_registry().tool_policies.items()}


def _memory_scopes_payload(project_root: Path, args: dict[str, Any]) -> Any:
    return {key: value.model_dump(mode="json") for key, value in builtin_spec_registry().memory_scopes.items()}


def _objectives_payload(project_root: Path, args: dict[str, Any]) -> Any:
    if not (project_root / ".harness" / "harness.sqlite").exists():
        return []
    return [objective.model_dump(mode="json") for objective in _store(project_root).list_objectives()]


def _objective_payload(project_root: Path, args: dict[str, Any]) -> Any:
    objective_id = args.get("objective_id") or args.get("id")
    if not objective_id:
        return _objectives_payload(project_root, args)
    return _store(project_root).get_objective(str(objective_id)).model_dump(mode="json")


def _task_graph_payload(project_root: Path, args: dict[str, Any]) -> Any:
    objective_id = args.get("objective_id")
    return _store(project_root).build_task_graph(str(objective_id) if objective_id else None)


def _leases_payload(project_root: Path, args: dict[str, Any]) -> Any:
    if not (project_root / ".harness" / "harness.sqlite").exists():
        return []
    return [lease.model_dump(mode="json") for lease in _store(project_root).list_task_leases()]


def _lease_payload(project_root: Path, args: dict[str, Any]) -> Any:
    lease_id = args.get("lease_id") or args.get("id")
    if not lease_id:
        return _leases_payload(project_root, args)
    return _store(project_root).inspect_task_lease(str(lease_id)).model_dump(mode="json")


def _adapters_payload(project_root: Path, args: dict[str, Any]) -> Any:
    from harness.execution import list_execution_adapter_descriptors

    return [descriptor.model_dump(mode="json") for descriptor in list_execution_adapter_descriptors()]


def _adapter_payload(project_root: Path, args: dict[str, Any]) -> Any:
    adapter_id = args.get("adapter_id") or args.get("id")
    adapters = _adapters_payload(project_root, args)
    if not adapter_id:
        return adapters
    for adapter in adapters:
        if adapter.get("id") == adapter_id:
            return adapter
    raise KeyError(f"Adapter not found: {adapter_id}")


def _approvals_payload(project_root: Path, args: dict[str, Any]) -> Any:
    return {"summary": "Approvals are stored locally and required before hosted/data-boundary execution.", "operator_context": _operator(project_root).get("capabilities")}


def _policy_payload(project_root: Path, args: dict[str, Any]) -> Any:
    return {
        "security_layer": [
            "path guards block project escapes and secret-like paths",
            "context excludes remove hidden state and build/cache directories",
            "registered adapters fail closed on unknown adapters, unsafe metadata, missing approvals, and breaker controls",
            "Codex chat is read-only; edits use isolated workspaces and apply-back approval",
        ],
        "capabilities": build_capability_catalog(project_root).model_dump(mode="json"),
    }


def _sandbox_profiles_payload(project_root: Path, args: dict[str, Any]) -> Any:
    return [profile.model_dump(mode="json") for profile in list_sandbox_profiles()]


def _trace_payload(project_root: Path, args: dict[str, Any]) -> Any:
    if not (project_root / ".harness" / "harness.sqlite").exists():
        return []
    return [{"run_id": run.id, "trace_available": True} for run in _store(project_root).list_runs()[:10]]


def _apply_back_payload(project_root: Path, args: dict[str, Any]) -> Any:
    if not (project_root / ".harness" / "harness.sqlite").exists():
        return []
    payload = []
    for run in _store(project_root).list_runs()[:10]:
        artifacts = _store(project_root).list_artifacts(run.id)
        selected = [artifact for artifact in artifacts if "diff" in artifact.kind or "patch" in artifact.kind]
        if selected:
            payload.append({"run": run.model_dump(mode="json"), "artifacts": [item.model_dump(mode="json") for item in selected]})
    return payload


def _blocked_state_payload(project_root: Path, args: dict[str, Any]) -> Any:
    return _operator(project_root).get("progress", {}).get("blocked_reasons", [])


def _relativize_search_output(project_root: Path, output: str) -> str:
    if not output:
        return ""
    root = str(project_root)
    return output.replace(root + "/", "")
