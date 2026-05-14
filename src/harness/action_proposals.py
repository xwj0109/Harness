from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from harness.chat_tools import ChatToolRequest, ChatToolRisk, default_chat_tools
from harness.execution import list_execution_adapter_descriptors
from harness.security import sanitize_for_logging
from harness.workflow_templates import template_for_intent


ACTION_CONTRACT_SCHEMA_VERSION = "harness.action_contract/v1"

CONTROL_PLANE_TOOLS = {"create_objective", "create_task", "create_task_graph", "remember", "forget_memory", "request_approval"}
SANDBOXED_EXECUTION_TOOLS = {"dispatch_registered_adapter", "run_tests"}
REPO_MUTATION_TOOLS = {"edit_isolated", "apply_back", "deny_apply_back", "revert_pending_change"}


@dataclass(frozen=True)
class ActionProposal:
    source_tool_request: ChatToolRequest
    intent: str
    summary: str
    arguments: dict[str, Any]
    raw_model_payload: dict[str, Any]


@dataclass(frozen=True)
class ActionContract:
    id: str
    tool: str
    risk: ChatToolRisk
    summary: str
    normalized_arguments: dict[str, Any]
    required_confirmations: list[str]
    required_approvals: list[str]
    execution_plan: list[dict[str, Any]]
    evidence_plan: list[str]
    allowed_next_commands: list[str]
    requires_confirmation: bool = True
    schema_version: str = ACTION_CONTRACT_SCHEMA_VERSION

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "tool": self.tool,
            "risk": self.risk,
            "summary": self.summary,
            "normalized_arguments": self.normalized_arguments,
            "required_confirmations": self.required_confirmations,
            "required_approvals": self.required_approvals,
            "execution_plan": self.execution_plan,
            "evidence_plan": self.evidence_plan,
            "allowed_next_commands": self.allowed_next_commands,
            "requires_confirmation": self.requires_confirmation,
        }


def contract_from_tool_request(request: ChatToolRequest, project_root: Path | None = None) -> ActionContract:
    tool = default_chat_tools().get(request.tool)
    if tool is None:
        raise ValueError(f"Unknown Harness tool: {request.tool}")
    if tool.spec.risk == "read":
        raise ValueError(f"Read-only tool does not require an action contract: {request.tool}")
    normalized = _normalize_arguments(request.tool, request.arguments, project_root=project_root)
    return ActionContract(
        id=f"act_{uuid.uuid4().hex[:12]}",
        tool=request.tool,
        risk=tool.spec.risk,
        summary=_summary_for_tool(request.tool, normalized),
        normalized_arguments=normalized,
        required_confirmations=_required_confirmations(request.tool),
        required_approvals=_required_approvals(request.tool),
        execution_plan=_execution_plan(request.tool, normalized),
        evidence_plan=_evidence_plan(request.tool),
        allowed_next_commands=["/confirm", "/decline"],
    )


def _normalize_arguments(tool: str, arguments: dict[str, Any], *, project_root: Path | None = None) -> dict[str, Any]:
    safe_args = sanitize_for_logging(arguments)
    if not isinstance(safe_args, dict):
        safe_args = {}
    if tool == "create_objective":
        title = _string_arg(safe_args, "title") or _string_arg(safe_args, "goal") or "Chat-created objective"
        return {
            "title": title,
            "description": _string_arg(safe_args, "description") or _string_arg(safe_args, "goal") or "",
            "workbench_id": _optional_string_arg(safe_args, "workbench_id"),
        }
    if tool == "create_task":
        adapter = _optional_string_arg(safe_args, "execution_adapter") or "dry_run"
        task_type = _optional_string_arg(safe_args, "task_type") or _default_task_type(adapter)
        _validate_adapter_task_type(adapter, task_type)
        metadata = safe_args.get("metadata") if isinstance(safe_args.get("metadata"), dict) else {}
        _validate_adapter_task_metadata(adapter, metadata)
        return {
            "title": _string_arg(safe_args, "title") or _string_arg(safe_args, "goal") or "Chat-created task",
            "description": _string_arg(safe_args, "description") or _string_arg(safe_args, "goal") or "",
            "objective_id": _optional_string_arg(safe_args, "objective_id"),
            "workbench_id": _optional_string_arg(safe_args, "workbench_id"),
            "agent_id": _optional_string_arg(safe_args, "agent_id"),
            "execution_adapter": adapter,
            "task_type": task_type,
            "metadata": metadata,
        }
    if tool == "create_task_graph":
        goal = _string_arg(safe_args, "goal") or _string_arg(safe_args, "title") or "Chat-created objective"
        template_id = _optional_string_arg(safe_args, "template_id") or _optional_string_arg(safe_args, "workflow_template")
        if template_id:
            try:
                template = template_for_intent(template_id, goal, project_root or Path.cwd())
            except KeyError as exc:
                raise ValueError(str(exc)) from exc
            return {
                "goal": goal,
                "workbench_id": _optional_string_arg(safe_args, "workbench_id"),
                "template_id": template.id,
                "template": template.to_payload(),
                "tasks": [task.to_payload() for task in template.tasks],
            }
        raw_tasks = list(safe_args.get("tasks") or [])
        for raw_task in raw_tasks:
            if not isinstance(raw_task, dict):
                continue
            adapter = _optional_string_arg(raw_task, "execution_adapter") or "dry_run"
            task_type = _optional_string_arg(raw_task, "task_type") or _default_task_type(adapter)
            _validate_adapter_task_type(adapter, task_type)
            metadata = raw_task.get("metadata") if isinstance(raw_task.get("metadata"), dict) else {}
            _validate_adapter_task_metadata(adapter, metadata)
        return {"goal": goal, "workbench_id": _optional_string_arg(safe_args, "workbench_id"), "tasks": raw_tasks}
    if tool in {"edit_isolated", "run_tests", "dispatch_registered_adapter", "apply_back", "deny_apply_back", "revert_pending_change", "request_approval", "remember", "forget_memory"}:
        normalized = dict(safe_args)
        if "goal" not in normalized and "summary" in normalized:
            normalized["goal"] = normalized["summary"]
        return normalized
    return dict(safe_args)


def _summary_for_tool(tool: str, args: dict[str, Any]) -> str:
    if tool == "create_objective":
        return f"Create Harness objective: {args['title']}"
    if tool == "create_task":
        return f"Create Harness task: {args['title']}"
    if tool == "create_task_graph":
        return f"Create Harness task graph for: {args['goal']}"
    if tool == "edit_isolated":
        return f"Run isolated edit: {args.get('goal', 'requested change')}"
    if tool == "run_tests":
        return f"Run tests: {args.get('suggested_command') or args.get('scope') or 'requested test scope'}"
    if tool == "apply_back":
        return "Apply inspected isolated changes back to the active repo"
    return f"Run Harness tool: {tool}"


def _required_confirmations(tool: str) -> list[str]:
    if tool == "apply_back":
        return ["apply_back_separate"]
    if tool in REPO_MUTATION_TOOLS:
        return ["start_action", "apply_back_separate"]
    return ["start_action"]


def _required_approvals(tool: str) -> list[str]:
    if tool in {"edit_isolated", "dispatch_registered_adapter"}:
        return ["hosted_provider_codex"]
    if tool == "run_tests":
        return ["docker_execution"]
    return []


def _execution_plan(tool: str, args: dict[str, Any]) -> list[dict[str, Any]]:
    if tool == "create_objective":
        return [{"step": "create_objective", "title": args["title"]}]
    if tool == "create_task":
        return [
            {
                "step": "create_task",
                "execution_adapter": args["execution_adapter"],
                "task_type": args["task_type"],
            }
        ]
    if tool == "create_task_graph":
        plan = [{"step": "create_objective"}, {"step": "create_tasks", "count": len(args.get("tasks") or [])}]
        if args.get("template_id"):
            plan.insert(0, {"step": "select_workflow_template", "template_id": args["template_id"]})
        return plan
    if tool == "edit_isolated":
        return [
            {"step": "create_objective"},
            {"step": "create_task", "execution_adapter": "codex_isolated_edit", "task_type": "codex_code_edit"},
            {"step": "dispatch_registered_adapter"},
            {"step": "collect_diff_artifact"},
        ]
    if tool == "run_tests":
        return [{"step": "run_tests", "scope": args.get("scope"), "suggested_command": args.get("suggested_command")}]
    if tool == "apply_back":
        return [{"step": "apply_back"}]
    return [{"step": tool}]


def _evidence_plan(tool: str) -> list[str]:
    if tool in CONTROL_PLANE_TOOLS:
        return ["chat_confirmation", "sqlite_record"]
    if tool in SANDBOXED_EXECUTION_TOOLS:
        return ["chat_confirmation", "task", "lease", "run", "artifact_manifest"]
    if tool in REPO_MUTATION_TOOLS:
        return ["chat_confirmation", "task", "run", "diff_artifact", "apply_back_decision"]
    return ["chat_confirmation"]


def _default_task_type(adapter: str) -> str:
    for descriptor in list_execution_adapter_descriptors():
        if descriptor.id == adapter and descriptor.supported_task_types:
            return descriptor.supported_task_types[0]
    return "phase_1a_test" if adapter == "dry_run" else adapter


def _validate_adapter_task_type(adapter: str, task_type: str) -> None:
    for descriptor in list_execution_adapter_descriptors():
        if descriptor.id != adapter:
            continue
        if descriptor.supported_task_types and task_type not in descriptor.supported_task_types:
            raise ValueError(f"Task type {task_type} is not supported by adapter {adapter}")
        return
    raise ValueError(f"Unknown execution adapter: {adapter}")


def _validate_adapter_task_metadata(adapter: str, metadata: dict[str, Any]) -> None:
    if not metadata:
        return
    for descriptor in list_execution_adapter_descriptors():
        if descriptor.id != adapter:
            continue
        rejected = sorted(key for key in descriptor.rejected_task_metadata if metadata.get(key))
        if rejected:
            raise ValueError(f"Task metadata is rejected by adapter {adapter}: {', '.join(rejected)}")
        return


def _string_arg(arguments: dict[str, Any], key: str) -> str:
    value = arguments.get(key)
    return str(value).strip() if value is not None and str(value).strip() else ""


def _optional_string_arg(arguments: dict[str, Any], key: str) -> str | None:
    value = _string_arg(arguments, key)
    return value or None
