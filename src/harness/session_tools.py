from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from harness.config import load_config
from harness.memory.sqlite_store import SQLiteStore
from harness.models import (
    EventStreamType,
    RedactionState,
    RunEventType,
    SessionMessageRole,
    SessionPartKind,
    SessionPermissionBoundaryKind,
    SessionPermissionScope,
    SessionPermissionStatus,
)
from harness.paths import PathSecurityError, is_excluded_relative, relative_to_project, resolve_under_project
from harness.policy import (
    backend_descriptor_sha256,
    effective_policy_sha256,
    resolve_agent_effective_policy,
    resolve_backend_effective_policy,
    resolve_task_effective_policy,
    resolve_workbench_effective_policy,
)
from harness.registry import builtin_spec_registry
from harness.sandbox import CommandValidationError, validate_test_command
from harness.security import assert_not_secret_path, is_secret_path, sanitize_for_logging
from harness.tools.patch import PatchValidationError, _is_blocked_edit_path, plan_unified_diff


class SessionToolSideEffect(str, Enum):
    NONE = "none"
    SESSION_LOCAL = "session_local"
    MUTATION = "mutation"
    EXECUTION = "execution"
    NETWORK = "network"


class SessionToolReplayPolicy(str, Enum):
    EVENT_AND_PREVIEW = "event_and_preview"
    ARTIFACT_FOR_LARGE_OUTPUT = "artifact_for_large_output"
    PERMISSION_EVENT_ONLY = "permission_event_only"


class SessionToolRisk(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class SessionToolPermissionDecisionStatus(str, Enum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


class SessionToolDescriptor(BaseModel):
    schema_version: str = "harness.session_tool_descriptor/v1"
    id: str
    title: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    side_effect: SessionToolSideEffect
    risk: SessionToolRisk
    boundary_kind: SessionPermissionBoundaryKind
    permission_key: str
    permission_required: bool
    replay_policy: SessionToolReplayPolicy
    inline_preview_limit_bytes: int = 16 * 1024
    event_payload_limit_bytes: int = 64 * 1024
    allowed_in_plan_agent: bool = False
    enabled: bool = True
    safety_notes: list[str] = Field(default_factory=list)


class SessionToolExecutionResult(BaseModel):
    schema_version: str = "harness.session_tool_execution/v1"
    ok: bool
    session_id: str
    run_id: str
    tool_id: str
    preview: str
    artifact_id: str | None = None
    truncated: bool = False
    error_type: str | None = None
    permission_id: str | None = None


class SessionToolPermissionDecision(BaseModel):
    schema_version: str = "harness.session_tool_permission_decision/v1"
    status: SessionToolPermissionDecisionStatus
    tool_id: str
    action: str
    target: str
    boundary_kind: SessionPermissionBoundaryKind
    reasons: list[str] = Field(default_factory=list)


TOOL_RESULT_INLINE_PREVIEW_BYTES = 16 * 1024


_TEXT_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "preview": {"type": "string"},
        "artifact_id": {"type": ["string", "null"]},
        "truncated": {"type": "boolean"},
    },
    "required": ["preview", "artifact_id", "truncated"],
}


def default_session_tool_descriptors() -> list[SessionToolDescriptor]:
    notes = [
        "Descriptors are documentation and validation metadata, not permission grants.",
        "Tool calls must be persisted as session events before any user-visible output.",
        "Large outputs must be truncated to a preview and stored as artifacts with checksum metadata.",
    ]
    disabled_notes = notes + [
        "This descriptor is registered for Phase 4B planning only and is disabled by default.",
        "Execution requires explicit permission, policy evaluation, and a gated Harness adapter path.",
    ]
    return [
        SessionToolDescriptor(
            id="read",
            title="Read file",
            description="Read a file inside the project boundary and return a redacted preview.",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
            output_schema=_TEXT_OUTPUT_SCHEMA,
            side_effect=SessionToolSideEffect.NONE,
            risk=SessionToolRisk.LOW,
            boundary_kind=SessionPermissionBoundaryKind.LOCAL_ONLY,
            permission_key="tool.read.project_file",
            permission_required=False,
            replay_policy=SessionToolReplayPolicy.ARTIFACT_FOR_LARGE_OUTPUT,
            allowed_in_plan_agent=True,
            safety_notes=notes + ["Read is auto-allowable only inside the project boundary."],
        ),
        SessionToolDescriptor(
            id="glob",
            title="Glob files",
            description="List project files matching a glob pattern.",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "pattern": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 200},
                },
                "required": ["pattern"],
            },
            output_schema=_TEXT_OUTPUT_SCHEMA,
            side_effect=SessionToolSideEffect.NONE,
            risk=SessionToolRisk.LOW,
            boundary_kind=SessionPermissionBoundaryKind.LOCAL_ONLY,
            permission_key="tool.glob.project_files",
            permission_required=False,
            replay_policy=SessionToolReplayPolicy.ARTIFACT_FOR_LARGE_OUTPUT,
            allowed_in_plan_agent=True,
            safety_notes=notes + ["Glob results must honor Harness context excludes and secret-path filters."],
        ),
        SessionToolDescriptor(
            id="grep",
            title="Search files",
            description="Search project text for a literal or regular-expression pattern.",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": ["string", "null"]},
                    "regex": {"type": "boolean", "default": False},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 200},
                },
                "required": ["pattern"],
            },
            output_schema=_TEXT_OUTPUT_SCHEMA,
            side_effect=SessionToolSideEffect.NONE,
            risk=SessionToolRisk.LOW,
            boundary_kind=SessionPermissionBoundaryKind.LOCAL_ONLY,
            permission_key="tool.grep.project_files",
            permission_required=False,
            replay_policy=SessionToolReplayPolicy.ARTIFACT_FOR_LARGE_OUTPUT,
            allowed_in_plan_agent=True,
            safety_notes=notes + ["Grep output must redact secret-like matches before rendering."],
        ),
        SessionToolDescriptor(
            id="artifact-read",
            title="Read artifact",
            description="Read metadata or a preview for a Harness artifact linked to the session, task, or run.",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "artifact_id": {"type": "string"},
                    "max_bytes": {"type": "integer", "minimum": 1, "maximum": 262144, "default": 16384},
                },
                "required": ["artifact_id"],
            },
            output_schema=_TEXT_OUTPUT_SCHEMA,
            side_effect=SessionToolSideEffect.NONE,
            risk=SessionToolRisk.LOW,
            boundary_kind=SessionPermissionBoundaryKind.LOCAL_ONLY,
            permission_key="tool.artifact_read",
            permission_required=False,
            replay_policy=SessionToolReplayPolicy.ARTIFACT_FOR_LARGE_OUTPUT,
            allowed_in_plan_agent=True,
            safety_notes=notes + ["Artifact previews must preserve artifact redaction state."],
        ),
        SessionToolDescriptor(
            id="policy-explain",
            title="Explain policy",
            description="Explain why a session, task, tool, or adapter is allowed, blocked, or approval-gated.",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "subject_kind": {"type": "string"},
                    "subject_id": {"type": ["string", "null"]},
                },
                "required": ["subject_kind"],
            },
            output_schema=_TEXT_OUTPUT_SCHEMA,
            side_effect=SessionToolSideEffect.NONE,
            risk=SessionToolRisk.LOW,
            boundary_kind=SessionPermissionBoundaryKind.LOCAL_ONLY,
            permission_key="tool.policy_explain",
            permission_required=False,
            replay_policy=SessionToolReplayPolicy.EVENT_AND_PREVIEW,
            allowed_in_plan_agent=True,
            safety_notes=notes + ["Policy explanations cannot broaden permissions."],
        ),
        SessionToolDescriptor(
            id="todo",
            title="Update todos",
            description="Create or update session-local todo records.",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "title": {"type": "string"},
                                "status": {"type": "string", "enum": ["pending", "in_progress", "done"]},
                            },
                            "required": ["title", "status"],
                        },
                    }
                },
                "required": ["items"],
            },
            output_schema=_TEXT_OUTPUT_SCHEMA,
            side_effect=SessionToolSideEffect.SESSION_LOCAL,
            risk=SessionToolRisk.LOW,
            boundary_kind=SessionPermissionBoundaryKind.LOCAL_ONLY,
            permission_key="tool.todo.session",
            permission_required=False,
            replay_policy=SessionToolReplayPolicy.EVENT_AND_PREVIEW,
            allowed_in_plan_agent=True,
            safety_notes=notes + ["Todo updates mutate only session-local state, never repository files."],
        ),
        SessionToolDescriptor(
            id="question",
            title="Ask question",
            description="Persist a model-to-user clarification question for the active session.",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "question": {"type": "string"},
                    "choices": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["question"],
            },
            output_schema=_TEXT_OUTPUT_SCHEMA,
            side_effect=SessionToolSideEffect.SESSION_LOCAL,
            risk=SessionToolRisk.LOW,
            boundary_kind=SessionPermissionBoundaryKind.LOCAL_ONLY,
            permission_key="tool.question.session",
            permission_required=False,
            replay_policy=SessionToolReplayPolicy.EVENT_AND_PREVIEW,
            allowed_in_plan_agent=True,
            safety_notes=notes + ["Questions pause for operator input; they do not grant authority."],
        ),
        SessionToolDescriptor(
            id="patch",
            title="Plan patch",
            description="Validate a patch and persist patch evidence without applying it to the active workspace.",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {"patch": {"type": "string"}},
                "required": ["patch"],
            },
            output_schema=_TEXT_OUTPUT_SCHEMA,
            side_effect=SessionToolSideEffect.MUTATION,
            risk=SessionToolRisk.HIGH,
            boundary_kind=SessionPermissionBoundaryKind.ACTIVE_REPO_WRITE,
            permission_key="tool.patch.active_repo_write",
            permission_required=True,
            replay_policy=SessionToolReplayPolicy.ARTIFACT_FOR_LARGE_OUTPUT,
            enabled=True,
            safety_notes=notes
            + [
                "Patch is enabled only as a permission-gated planning prototype.",
                "This tool validates and stores patch evidence but does not apply changes to the active workspace.",
                "Actual mutation must go through snapshot/apply-back rules.",
            ],
        ),
        SessionToolDescriptor(
            id="managed-action",
            title="Managed action",
            description="Run a Harness-managed local action through the action router.",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {"instruction": {"type": "string"}},
                "required": ["instruction"],
            },
            output_schema=_TEXT_OUTPUT_SCHEMA,
            side_effect=SessionToolSideEffect.MUTATION,
            risk=SessionToolRisk.MEDIUM,
            boundary_kind=SessionPermissionBoundaryKind.ACTIVE_REPO_WRITE,
            permission_key="tool.managed_action",
            permission_required=True,
            replay_policy=SessionToolReplayPolicy.EVENT_AND_PREVIEW,
            enabled=False,
            safety_notes=disabled_notes + ["Managed actions must remain routed through Harness validation and evidence records."],
        ),
        SessionToolDescriptor(
            id="docker-test",
            title="Plan Docker test",
            description="Validate a Docker test command and persist a planned execution artifact without running it.",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
            output_schema=_TEXT_OUTPUT_SCHEMA,
            side_effect=SessionToolSideEffect.EXECUTION,
            risk=SessionToolRisk.HIGH,
            boundary_kind=SessionPermissionBoundaryKind.SHELL,
            permission_key="tool.docker_test.execution",
            permission_required=True,
            replay_policy=SessionToolReplayPolicy.ARTIFACT_FOR_LARGE_OUTPUT,
            enabled=True,
            safety_notes=notes
            + [
                "Docker-test is enabled only as a permission-gated planning prototype.",
                "This tool validates and stores planned execution evidence but does not run Docker.",
                "Actual Docker execution must use Harness runtime controls and test artifacts.",
            ],
        ),
        SessionToolDescriptor(
            id="direct-write",
            title="Plan direct write",
            description="Validate direct file content and persist proposed write evidence without writing to the active workspace.",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"],
            },
            output_schema=_TEXT_OUTPUT_SCHEMA,
            side_effect=SessionToolSideEffect.MUTATION,
            risk=SessionToolRisk.HIGH,
            boundary_kind=SessionPermissionBoundaryKind.ACTIVE_REPO_WRITE,
            permission_key="tool.direct_write.active_repo_write",
            permission_required=True,
            replay_policy=SessionToolReplayPolicy.ARTIFACT_FOR_LARGE_OUTPUT,
            enabled=True,
            safety_notes=notes
            + [
                "Direct write is enabled only as a permission-gated planning prototype.",
                "This tool validates and stores proposed content but does not write to the active workspace.",
                "Actual writes require blocked-path checks and must not bypass apply-back policy.",
            ],
        ),
        SessionToolDescriptor(
            id="shell",
            title="Shell command",
            description="Run a shell command.",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {"command": {"type": "string"}, "cwd": {"type": ["string", "null"]}},
                "required": ["command"],
            },
            output_schema=_TEXT_OUTPUT_SCHEMA,
            side_effect=SessionToolSideEffect.EXECUTION,
            risk=SessionToolRisk.HIGH,
            boundary_kind=SessionPermissionBoundaryKind.SHELL,
            permission_key="tool.shell.execution",
            permission_required=True,
            replay_policy=SessionToolReplayPolicy.ARTIFACT_FOR_LARGE_OUTPUT,
            enabled=False,
            safety_notes=disabled_notes + ["Shell remains approval-required with no model auto-run by default."],
        ),
        SessionToolDescriptor(
            id="web-fetch",
            title="Web fetch",
            description="Fetch a URL through an external-network policy gate.",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
            output_schema=_TEXT_OUTPUT_SCHEMA,
            side_effect=SessionToolSideEffect.NETWORK,
            risk=SessionToolRisk.HIGH,
            boundary_kind=SessionPermissionBoundaryKind.EXTERNAL_NETWORK,
            permission_key="tool.web_fetch.external_network",
            permission_required=True,
            replay_policy=SessionToolReplayPolicy.ARTIFACT_FOR_LARGE_OUTPUT,
            enabled=False,
            safety_notes=disabled_notes + ["Network access must be externally approved and fully replayable."],
        ),
        SessionToolDescriptor(
            id="web-search",
            title="Web search",
            description="Search the web through an external-network policy gate.",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "query": {"type": "string"},
                    "allowed_domains": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["query"],
            },
            output_schema=_TEXT_OUTPUT_SCHEMA,
            side_effect=SessionToolSideEffect.NETWORK,
            risk=SessionToolRisk.HIGH,
            boundary_kind=SessionPermissionBoundaryKind.EXTERNAL_NETWORK,
            permission_key="tool.web_search.external_network",
            permission_required=True,
            replay_policy=SessionToolReplayPolicy.ARTIFACT_FOR_LARGE_OUTPUT,
            enabled=False,
            safety_notes=disabled_notes + ["Search results must be stored as replayable, redacted evidence before display."],
        ),
        SessionToolDescriptor(
            id="mcp",
            title="MCP tool",
            description="Call a configured MCP tool through the session permission envelope.",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {"server": {"type": "string"}, "tool": {"type": "string"}, "arguments": {"type": "object"}},
                "required": ["server", "tool"],
            },
            output_schema=_TEXT_OUTPUT_SCHEMA,
            side_effect=SessionToolSideEffect.EXECUTION,
            risk=SessionToolRisk.HIGH,
            boundary_kind=SessionPermissionBoundaryKind.MCP,
            permission_key="tool.mcp.execution",
            permission_required=True,
            replay_policy=SessionToolReplayPolicy.ARTIFACT_FOR_LARGE_OUTPUT,
            enabled=False,
            safety_notes=disabled_notes + ["MCP calls must produce the same permission and evidence records as built-ins."],
        ),
        SessionToolDescriptor(
            id="mcp-resource",
            title="MCP resource",
            description="Read a cached or connected MCP resource through the session permission envelope.",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {"server": {"type": "string"}, "uri": {"type": "string"}},
                "required": ["server", "uri"],
            },
            output_schema=_TEXT_OUTPUT_SCHEMA,
            side_effect=SessionToolSideEffect.EXECUTION,
            risk=SessionToolRisk.HIGH,
            boundary_kind=SessionPermissionBoundaryKind.MCP,
            permission_key="tool.mcp_resource.execution",
            permission_required=True,
            replay_policy=SessionToolReplayPolicy.ARTIFACT_FOR_LARGE_OUTPUT,
            enabled=False,
            safety_notes=disabled_notes + ["MCP resource reads must use the same permission and evidence records as MCP tools."],
        ),
        SessionToolDescriptor(
            id="plugin-tool",
            title="Plugin tool",
            description="Call a tool supplied by an installed plugin through the Harness session registry.",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {"plugin": {"type": "string"}, "tool": {"type": "string"}, "arguments": {"type": "object"}},
                "required": ["plugin", "tool"],
            },
            output_schema=_TEXT_OUTPUT_SCHEMA,
            side_effect=SessionToolSideEffect.EXECUTION,
            risk=SessionToolRisk.HIGH,
            boundary_kind=SessionPermissionBoundaryKind.LOCAL_ONLY,
            permission_key="tool.plugin.execution",
            permission_required=True,
            replay_policy=SessionToolReplayPolicy.ARTIFACT_FOR_LARGE_OUTPUT,
            enabled=False,
            safety_notes=disabled_notes + ["Plugin tool origin, scope, and version must be visible before any invocation."],
        ),
        SessionToolDescriptor(
            id="skill-load",
            title="Load skill",
            description="Load a configured skill body into the session context through an auditable tool event.",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {"skill": {"type": "string"}},
                "required": ["skill"],
            },
            output_schema=_TEXT_OUTPUT_SCHEMA,
            side_effect=SessionToolSideEffect.SESSION_LOCAL,
            risk=SessionToolRisk.MEDIUM,
            boundary_kind=SessionPermissionBoundaryKind.LOCAL_ONLY,
            permission_key="tool.skill_load.session",
            permission_required=True,
            replay_policy=SessionToolReplayPolicy.ARTIFACT_FOR_LARGE_OUTPUT,
            enabled=False,
            safety_notes=disabled_notes + ["Skill bodies must not be loaded unless the session records the source and redaction state."],
        ),
        SessionToolDescriptor(
            id="pty",
            title="PTY session",
            description="Open or interact with a pseudo-terminal.",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
            output_schema=_TEXT_OUTPUT_SCHEMA,
            side_effect=SessionToolSideEffect.EXECUTION,
            risk=SessionToolRisk.HIGH,
            boundary_kind=SessionPermissionBoundaryKind.PTY,
            permission_key="tool.pty.execution",
            permission_required=True,
            replay_policy=SessionToolReplayPolicy.ARTIFACT_FOR_LARGE_OUTPUT,
            enabled=False,
            safety_notes=disabled_notes + ["PTY use remains approval-required and cannot be model auto-run by default."],
        ),
        SessionToolDescriptor(
            id="repo-clone",
            title="Clone external repository",
            description="Clone or inspect an external repository through a managed network/cache boundary.",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
            output_schema=_TEXT_OUTPUT_SCHEMA,
            side_effect=SessionToolSideEffect.NETWORK,
            risk=SessionToolRisk.HIGH,
            boundary_kind=SessionPermissionBoundaryKind.EXTERNAL_NETWORK,
            permission_key="tool.repo_clone.external_network",
            permission_required=True,
            replay_policy=SessionToolReplayPolicy.ARTIFACT_FOR_LARGE_OUTPUT,
            enabled=False,
            safety_notes=disabled_notes + ["Repository cloning must use a managed external cache and network approval."],
        ),
    ]


def get_session_tool_descriptor(tool_id: str) -> SessionToolDescriptor:
    for descriptor in default_session_tool_descriptors():
        if descriptor.id == tool_id:
            return descriptor
    raise KeyError(f"Session tool not found: {tool_id}")


def decide_session_tool_permission(
    store: SQLiteStore,
    project_root: Path,
    session_id: str,
    tool_id: str,
    arguments: dict[str, Any],
) -> SessionToolPermissionDecision:
    descriptor = get_session_tool_descriptor(tool_id)
    action = _tool_action(tool_id)
    target = _tool_target(tool_id, arguments)
    if not descriptor.enabled:
        return SessionToolPermissionDecision(
            status=SessionToolPermissionDecisionStatus.DENY,
            tool_id=tool_id,
            action=action,
            target=target,
            boundary_kind=descriptor.boundary_kind,
            reasons=[f"Session tool is disabled by policy: {tool_id}"],
        )
    if _has_allowed_permission(store, session_id, tool_id, action, target, descriptor.boundary_kind):
        return SessionToolPermissionDecision(
            status=SessionToolPermissionDecisionStatus.ALLOW,
            tool_id=tool_id,
            action=action,
            target=target,
            boundary_kind=descriptor.boundary_kind,
            reasons=["Allowed by existing session permission."],
        )
    try:
        if tool_id == "patch":
            patch = str(arguments.get("patch") or "")
            summary, _updates = plan_unified_diff(patch, project_root.resolve(), load_config(project_root).context_excludes)
            target = ",".join(summary.files) if summary.files else "patch"
            if _has_allowed_permission(store, session_id, tool_id, action, target, descriptor.boundary_kind):
                return SessionToolPermissionDecision(
                    status=SessionToolPermissionDecisionStatus.ALLOW,
                    tool_id=tool_id,
                    action=action,
                    target=target,
                    boundary_kind=descriptor.boundary_kind,
                    reasons=["Allowed by existing session permission."],
                )
            return SessionToolPermissionDecision(
                status=SessionToolPermissionDecisionStatus.ASK,
                tool_id=tool_id,
                action=action,
                target=target,
                boundary_kind=descriptor.boundary_kind,
                reasons=["Patch planning requires explicit active-repo-write permission even though no files are applied."],
            )
        if tool_id == "direct-write":
            target = _validate_direct_write_target(project_root.resolve(), str(arguments.get("path") or ""), load_config(project_root).context_excludes)
            if _has_allowed_permission(store, session_id, tool_id, action, target, descriptor.boundary_kind):
                return SessionToolPermissionDecision(
                    status=SessionToolPermissionDecisionStatus.ALLOW,
                    tool_id=tool_id,
                    action=action,
                    target=target,
                    boundary_kind=descriptor.boundary_kind,
                    reasons=["Allowed by existing session permission."],
                )
            return SessionToolPermissionDecision(
                status=SessionToolPermissionDecisionStatus.ASK,
                tool_id=tool_id,
                action=action,
                target=target,
                boundary_kind=descriptor.boundary_kind,
                reasons=["Direct-write planning requires explicit active-repo-write permission even though no files are written."],
            )
        if tool_id == "docker-test":
            target = _validate_docker_test_plan(project_root.resolve(), arguments)
            if _has_allowed_permission(store, session_id, tool_id, action, target, descriptor.boundary_kind):
                return SessionToolPermissionDecision(
                    status=SessionToolPermissionDecisionStatus.ALLOW,
                    tool_id=tool_id,
                    action=action,
                    target=target,
                    boundary_kind=descriptor.boundary_kind,
                    reasons=["Allowed by existing session permission."],
                )
            return SessionToolPermissionDecision(
                status=SessionToolPermissionDecisionStatus.ASK,
                tool_id=tool_id,
                action=action,
                target=target,
                boundary_kind=descriptor.boundary_kind,
                reasons=["Docker-test planning requires explicit execution permission even though Docker is not run."],
            )
        if tool_id == "read":
            _resolve_allowed_path(project_root.resolve(), str(arguments.get("path") or ""), load_config(project_root).context_excludes, action=action)
        elif tool_id == "grep" and arguments.get("path"):
            _resolve_allowed_path(project_root.resolve(), str(arguments.get("path")), load_config(project_root).context_excludes, action=action)
        elif tool_id == "artifact-read":
            _assert_artifact_linked_to_session(store, session_id, str(arguments.get("artifact_id") or ""))
        elif tool_id == "policy-explain":
            _assert_policy_subject_linked_to_session(
                store,
                session_id,
                str(arguments.get("subject_kind") or "session"),
                str(arguments.get("subject_id")) if arguments.get("subject_id") else None,
            )
    except _DeniedToolCall as exc:
        status = SessionToolPermissionDecisionStatus.ASK if exc.error_type == "context_excluded" else SessionToolPermissionDecisionStatus.DENY
        return SessionToolPermissionDecision(
            status=status,
            tool_id=tool_id,
            action=exc.action,
            target=exc.target,
            boundary_kind=descriptor.boundary_kind,
            reasons=[exc.message],
        )
    except (PatchValidationError, PathSecurityError, CommandValidationError) as exc:
        return SessionToolPermissionDecision(
            status=SessionToolPermissionDecisionStatus.DENY,
            tool_id=tool_id,
            action=action,
            target=target,
            boundary_kind=descriptor.boundary_kind,
            reasons=[str(sanitize_for_logging(str(exc)))],
        )
    return SessionToolPermissionDecision(
        status=SessionToolPermissionDecisionStatus.ALLOW,
        tool_id=tool_id,
        action=action,
        target=target,
        boundary_kind=descriptor.boundary_kind,
        reasons=["Allowed by Phase 4A local/session read-only policy."],
    )


def execute_session_tool(
    store: SQLiteStore,
    project_root: Path,
    session_id: str,
    tool_id: str,
    arguments: dict[str, Any],
) -> SessionToolExecutionResult:
    descriptor = get_session_tool_descriptor(tool_id)
    if (
        descriptor.enabled
        and descriptor.side_effect not in {SessionToolSideEffect.NONE, SessionToolSideEffect.SESSION_LOCAL}
        and tool_id not in {"patch", "direct-write", "docker-test"}
    ):
        raise ValueError(f"Session tool is not enabled for Phase 4A execution: {tool_id}")
    store.get_session(session_id)
    run = store.create_run(
        f"Session tool {tool_id}",
        "session_tool_call",
        status="running",
        session_id=session_id,
    )
    store.append_run_event(
        run.id,
        RunEventType.TOOL_CALL_STARTED,
        {"tool_id": tool_id, "arguments": sanitize_for_logging(arguments)},
        message=f"Session tool {tool_id} started.",
        session_id=session_id,
    )
    store.append_store_event(
        EventStreamType.SESSION,
        session_id,
        "tool_call.started",
        {"tool_id": tool_id, "arguments": sanitize_for_logging(arguments), "summary": tool_id},
        session_id=session_id,
        run_id=run.id,
        redaction_state=RedactionState.REDACTED,
    )
    decision = decide_session_tool_permission(store, project_root, session_id, tool_id, arguments)
    store.append_store_event(
        EventStreamType.SESSION,
        session_id,
        "permission.checked",
        {
            "tool_id": tool_id,
            "decision": decision.status.value,
            "action": decision.action,
            "target": decision.target,
            "boundary_kind": decision.boundary_kind.value,
            "reasons": decision.reasons,
            "summary": f"{tool_id} {decision.status.value}",
        },
        session_id=session_id,
        run_id=run.id,
        redaction_state=RedactionState.REDACTED,
    )
    try:
        if decision.status != SessionToolPermissionDecisionStatus.ALLOW:
            message = "; ".join(decision.reasons) or f"Permission decision was {decision.status.value}."
            permission = store.request_session_permission(
                session_id,
                tool_id=tool_id,
                normalized_action=decision.action,
                normalized_target_pattern=decision.target,
                boundary_kind=decision.boundary_kind,
                risk=descriptor.risk.value,
                run_id=run.id,
                scope=SessionPermissionScope.ONCE,
                policy_reasons=decision.reasons,
            )
            if decision.status == SessionToolPermissionDecisionStatus.DENY:
                permission = store.resolve_session_permission(permission.id, SessionPermissionStatus.DENIED, reason=message)
                error_type = "permission_denied"
            else:
                error_type = "permission_required"
            output = message
            ok = False
            permission_id = permission.id
        else:
            output = _execute_low_risk_tool(
                store,
                project_root,
                session_id,
                tool_id,
                arguments,
                run_id=run.id,
                allow_excluded=bool(decision.reasons and "existing session permission" in decision.reasons[0]),
            )
            ok = True
            error_type = None
            permission_id = None
    except _DeniedToolCall as exc:
        output = exc.message
        ok = False
        error_type = exc.error_type
        permission = store.request_session_permission(
            session_id,
            tool_id=tool_id,
            normalized_action=exc.action,
            normalized_target_pattern=exc.target,
            boundary_kind=descriptor.boundary_kind,
            risk=descriptor.risk.value,
            run_id=run.id,
            scope=SessionPermissionScope.ONCE,
            policy_reasons=[exc.message],
        )
        permission = store.resolve_session_permission(permission.id, SessionPermissionStatus.DENIED, reason=exc.message)
        permission_id = permission.id
    except Exception as exc:
        output = str(sanitize_for_logging(str(exc)))
        ok = False
        error_type = "tool_error"
        permission_id = None

    preview, artifact_id, truncated = _persist_tool_output(
        store,
        run.id,
        session_id,
        tool_id,
        output,
        ok=ok,
        error_type=error_type,
    )
    message = store.append_session_message(
        session_id,
        SessionMessageRole.TOOL,
        preview,
        run_id=run.id,
    )
    store.append_session_part(
        session_id,
        message.id,
        SessionPartKind.TOOL_RESULT,
        text=preview,
        artifact_id=artifact_id,
        run_id=run.id,
        metadata={"tool_id": tool_id, "ok": ok, "truncated": truncated, "error_type": error_type},
        redaction_state=RedactionState.REDACTED,
    )
    payload = {
        "tool_id": tool_id,
        "ok": ok,
        "preview": preview,
        "truncated": truncated,
        "artifact_id": artifact_id,
        "error_type": error_type,
        "permission_id": permission_id,
        "summary": preview[:240],
    }
    store.append_run_event(
        run.id,
        RunEventType.TOOL_CALL_OUTPUT,
        payload,
        message=f"Session tool {tool_id} output.",
        session_id=session_id,
    )
    store.append_store_event(
        EventStreamType.SESSION,
        session_id,
        "tool_call.output",
        payload,
        session_id=session_id,
        run_id=run.id,
        artifact_id=artifact_id,
        redaction_state=RedactionState.REDACTED,
    )
    finish_status = "completed" if ok else "policy_violation" if error_type in {"path_security", "secret_path", "permission_denied"} else "failed"
    store.update_run_status(run.id, finish_status)
    store.append_run_event(
        run.id,
        RunEventType.TOOL_CALL_FINISHED,
        {"tool_id": tool_id, "ok": ok, "status": finish_status},
        message=f"Session tool {tool_id} finished.",
        session_id=session_id,
    )
    store.append_store_event(
        EventStreamType.SESSION,
        session_id,
        "tool_call.finished",
        {"tool_id": tool_id, "ok": ok, "status": finish_status, "summary": finish_status},
        session_id=session_id,
        run_id=run.id,
        redaction_state=RedactionState.NOT_REQUIRED,
    )
    return SessionToolExecutionResult(
        ok=ok,
        session_id=session_id,
        run_id=run.id,
        tool_id=tool_id,
        preview=preview,
        artifact_id=artifact_id,
        truncated=truncated,
        error_type=error_type,
        permission_id=permission_id,
    )


class _DeniedToolCall(Exception):
    def __init__(self, message: str, *, action: str, target: str, error_type: str) -> None:
        super().__init__(message)
        self.message = message
        self.action = action
        self.target = target
        self.error_type = error_type


def _tool_action(tool_id: str) -> str:
    return {
        "read": "read",
        "glob": "list",
        "grep": "search",
        "artifact-read": "artifact-read",
        "policy-explain": "policy-explain",
        "todo": "todo",
        "question": "question",
    }.get(tool_id, tool_id)


def _tool_target(tool_id: str, arguments: dict[str, Any]) -> str:
    if tool_id == "read":
        return str(arguments.get("path") or "")
    if tool_id == "glob":
        return str(arguments.get("pattern") or "**/*")
    if tool_id == "grep":
        return str(arguments.get("path") or ".")
    if tool_id == "artifact-read":
        return str(arguments.get("artifact_id") or "")
    if tool_id == "policy-explain":
        return f"{arguments.get('subject_kind') or 'session'}:{arguments.get('subject_id') or ''}"
    return tool_id


def _has_allowed_permission(
    store: SQLiteStore,
    session_id: str,
    tool_id: str,
    action: str,
    target: str,
    boundary_kind: SessionPermissionBoundaryKind,
) -> bool:
    now = datetime.now(timezone.utc)
    for permission in store.list_session_permissions(session_id, status=SessionPermissionStatus.ALLOWED):
        if permission.tool_id != tool_id:
            continue
        if permission.normalized_action != action:
            continue
        if permission.normalized_target_pattern != target:
            continue
        if permission.boundary_kind != boundary_kind:
            continue
        if permission.expires_at <= now:
            continue
        return True
    return False


def _execute_low_risk_tool(
    store: SQLiteStore,
    project_root: Path,
    session_id: str,
    tool_id: str,
    arguments: dict[str, Any],
    *,
    run_id: str,
    allow_excluded: bool = False,
) -> str:
    root = project_root.resolve()
    excludes = load_config(root).context_excludes
    if tool_id == "patch":
        patch = str(arguments.get("patch") or "")
        summary, updates = plan_unified_diff(patch, root, excludes)
        patch_path = store.runs_dir / run_id / "session_tool_patch.diff"
        patch_path.write_text(str(sanitize_for_logging(patch)), encoding="utf-8")
        patch_artifact = store.register_artifact(
            run_id,
            "session_tool_patch",
            patch_path,
            metadata={"tool_id": tool_id, "files": summary.files, "applies_to_active_workspace": False},
            producer="session_tool",
            redaction_state="redacted",
            session_id=session_id,
        )
        planned_path = store.runs_dir / run_id / "session_tool_patch_plan.json"
        planned_payload = {
            "schema_version": "harness.session_tool_patch_plan/v1",
            "files": summary.files,
            "added_lines": summary.added_lines,
            "removed_lines": summary.removed_lines,
            "planned_updates": [
                {"relative_path": update.relative_path, "bytes": len(update.content.encode("utf-8"))}
                for update in updates
            ],
            "patch_artifact_id": patch_artifact.id,
            "applied": False,
        }
        planned_path.write_text(json.dumps(sanitize_for_logging(planned_payload), indent=2, sort_keys=True), encoding="utf-8")
        plan_artifact = store.register_artifact(
            run_id,
            "session_tool_patch_plan",
            planned_path,
            metadata={"tool_id": tool_id, "patch_artifact_id": patch_artifact.id, "applies_to_active_workspace": False},
            producer="session_tool",
            redaction_state="redacted",
            session_id=session_id,
        )
        return (
            "Patch validated but not applied.\n"
            f"{summary.render()}\n"
            f"Patch artifact: {patch_artifact.id}\n"
            f"Plan artifact: {plan_artifact.id}"
        )
    if tool_id == "direct-write":
        relative_path = _validate_direct_write_target(root, str(arguments.get("path") or ""), excludes)
        content = str(sanitize_for_logging(str(arguments.get("content") or "")))
        proposal_path = store.runs_dir / run_id / "session_tool_direct_write_content.txt"
        proposal_path.write_text(content, encoding="utf-8")
        content_artifact = store.register_artifact(
            run_id,
            "session_tool_direct_write_content",
            proposal_path,
            metadata={"tool_id": tool_id, "target": relative_path, "applies_to_active_workspace": False},
            producer="session_tool",
            redaction_state="redacted",
            session_id=session_id,
        )
        plan_path = store.runs_dir / run_id / "session_tool_direct_write_plan.json"
        plan_payload = {
            "schema_version": "harness.session_tool_direct_write_plan/v1",
            "target": relative_path,
            "content_artifact_id": content_artifact.id,
            "content_bytes": len(content.encode("utf-8")),
            "applied": False,
        }
        plan_path.write_text(json.dumps(sanitize_for_logging(plan_payload), indent=2, sort_keys=True), encoding="utf-8")
        plan_artifact = store.register_artifact(
            run_id,
            "session_tool_direct_write_plan",
            plan_path,
            metadata={"tool_id": tool_id, "content_artifact_id": content_artifact.id, "applies_to_active_workspace": False},
            producer="session_tool",
            redaction_state="redacted",
            session_id=session_id,
        )
        return (
            "Direct write validated but not applied.\n"
            f"Target: {relative_path}\n"
            f"Content artifact: {content_artifact.id}\n"
            f"Plan artifact: {plan_artifact.id}"
        )
    if tool_id == "docker-test":
        command, cwd, target = _docker_test_plan_values(root, arguments)
        plan_payload = {
            "schema_version": "harness.session_tool_docker_test_plan/v1",
            "command": command,
            "cwd": cwd,
            "target": target,
            "execution_adapter": "docker_run_tests",
            "executed": False,
        }
        plan_path = store.runs_dir / run_id / "session_tool_docker_test_plan.json"
        plan_path.write_text(json.dumps(sanitize_for_logging(plan_payload), indent=2, sort_keys=True), encoding="utf-8")
        plan_artifact = store.register_artifact(
            run_id,
            "session_tool_docker_test_plan",
            plan_path,
            metadata={"tool_id": tool_id, "target": target, "executed": False},
            producer="session_tool",
            redaction_state="redacted",
            session_id=session_id,
        )
        return (
            "Docker test validated but not executed.\n"
            f"Command: {' '.join(command)}\n"
            f"Workdir: {cwd or '.'}\n"
            f"Plan artifact: {plan_artifact.id}"
        )
    if tool_id == "read":
        path_arg = str(arguments.get("path") or "")
        path = _resolve_allowed_path(root, path_arg, excludes, action="read", allow_excluded=allow_excluded)
        if not path.is_file():
            raise ValueError("Path is not a file.")
        raw = path.read_bytes()
        if b"\x00" in raw:
            raise ValueError("File appears to be binary.")
        return raw.decode("utf-8")
    if tool_id == "glob":
        pattern = str(arguments.get("pattern") or "**/*")
        limit = int(arguments.get("limit") or 200)
        files = _project_files(root, excludes)
        matches = [rel for rel in files if Path(rel).match(pattern) or re.fullmatch(fnmatch_to_regex(pattern), rel)]
        return "\n".join(matches[:limit])
    if tool_id == "grep":
        pattern = str(arguments.get("pattern") or "")
        if not pattern:
            raise ValueError("Missing pattern.")
        regex = bool(arguments.get("regex") or False)
        limit = int(arguments.get("limit") or 200)
        base = arguments.get("path")
        files = _project_files(root, excludes, start=str(base) if base else ".", allow_excluded=allow_excluded)
        hits: list[str] = []
        compiled = re.compile(pattern) if regex else None
        for rel in files:
            path = root / rel
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            for line_no, line in enumerate(text.splitlines(), start=1):
                matched = bool(compiled.search(line)) if compiled else pattern in line
                if matched:
                    hits.append(f"{rel}:{line_no}: {line}")
                    if len(hits) >= limit:
                        return "\n".join(hits)
        return "\n".join(hits)
    if tool_id == "artifact-read":
        artifact_id = str(arguments.get("artifact_id") or "")
        max_bytes = int(arguments.get("max_bytes") or TOOL_RESULT_INLINE_PREVIEW_BYTES)
        artifact = _assert_artifact_linked_to_session(store, session_id, artifact_id)
        if is_secret_path(artifact.path):
            raise _DeniedToolCall(
                f"Artifact path is secret-like: {artifact.path.name}",
                action="artifact-read",
                target=artifact_id,
                error_type="secret_path",
            )
        raw = artifact.path.read_bytes()[: max(1, min(max_bytes, 256 * 1024))]
        if b"\x00" in raw:
            preview = "[binary artifact preview omitted]"
        else:
            preview = raw.decode("utf-8", errors="replace")
        metadata = {
            "artifact_id": artifact.id,
            "kind": artifact.kind,
            "path": str(artifact.path),
            "sha256": artifact.sha256,
            "size_bytes": artifact.size_bytes,
            "redaction_state": artifact.redaction_state,
            "preview": preview,
        }
        return json.dumps(sanitize_for_logging(metadata), indent=2, sort_keys=True, default=str)
    if tool_id == "policy-explain":
        subject_kind = str(arguments.get("subject_kind") or "session")
        subject_id = arguments.get("subject_id")
        return _policy_explanation(store, project_root, session_id, subject_kind, str(subject_id) if subject_id else None)
    raise KeyError(f"Session tool is not executable in Phase 4A: {tool_id}")


def _policy_explanation(
    store: SQLiteStore,
    project_root: Path,
    session_id: str,
    subject_kind: str,
    subject_id: str | None,
) -> str:
    normalized = subject_kind.strip().lower()
    session = store.get_session(session_id)
    extra: dict[str, Any] = {}
    if normalized == "session":
        policy = {
            "schema_version": "harness.session_policy_explain/v1",
            "subject_kind": "session",
            "subject_id": session_id,
            "session_status": session.status.value,
            "active_task_id": session.active_task_id,
            "active_run_id": session.active_run_id,
            "agent_id": session.agent_id,
            "notes": [
                "Sessions are operator-facing continuity and evidence records, not authority grants.",
                "Tool execution remains constrained by Harness tool descriptors, permission records, and adapter policy.",
            ],
        }
        return json.dumps(sanitize_for_logging(policy), indent=2, sort_keys=True, default=str)
    if normalized == "run":
        target_id = subject_id or session.active_run_id
        if not target_id:
            raise ValueError("No run id supplied and session has no active run.")
        _assert_policy_subject_linked_to_session(store, session_id, normalized, target_id)
        manifest = store.build_run_manifest(target_id)
        if manifest.effective_policy is None:
            raise KeyError(f"Effective policy not found for run: {target_id}")
        policy = manifest.effective_policy
        extra["backend_descriptor_sha256"] = manifest.backend_descriptor_sha256
    elif normalized == "task":
        target_id = subject_id or session.active_task_id
        if not target_id:
            raise ValueError("No task id supplied and session has no active task.")
        task = _assert_policy_subject_linked_to_session(store, session_id, normalized, target_id)
        policy = resolve_task_effective_policy(task)
    elif normalized in {"agent", "workbench", "backend"}:
        target_id = subject_id or (session.agent_id if normalized == "agent" else session.workbench_id if normalized == "workbench" else None)
        if not target_id:
            raise ValueError(f"No {normalized} id supplied and session has no default.")
        if normalized == "agent":
            policy = resolve_agent_effective_policy(builtin_spec_registry(), target_id)
        elif normalized == "workbench":
            policy = resolve_workbench_effective_policy(builtin_spec_registry(), target_id)
        else:
            backend = load_config(project_root).backends[target_id]
            descriptor = backend.to_descriptor()
            policy = resolve_backend_effective_policy(descriptor)
            extra["backend_descriptor_sha256"] = backend_descriptor_sha256(descriptor)
    else:
        raise ValueError(f"Unsupported policy subject kind: {subject_kind}")
    payload = policy.model_dump(mode="json")
    payload.update({"policy_sha256": effective_policy_sha256(policy), **extra})
    return json.dumps(sanitize_for_logging(payload), indent=2, sort_keys=True, default=str)


def _assert_artifact_linked_to_session(store: SQLiteStore, session_id: str, artifact_id: str):
    artifact = store.get_artifact(artifact_id)
    if artifact.session_id == session_id:
        return artifact
    run = store.get_run(artifact.run_id)
    if run.session_id == session_id:
        return artifact
    raise _DeniedToolCall(
        f"Artifact is not linked to session: {artifact_id}",
        action="artifact-read",
        target=artifact_id,
        error_type="path_security",
    )


def _assert_policy_subject_linked_to_session(store: SQLiteStore, session_id: str, subject_kind: str, subject_id: str):
    normalized = subject_kind.strip().lower()
    if normalized == "run":
        run = store.get_run(subject_id)
        if run.session_id != session_id:
            raise _DeniedToolCall("Run is not linked to this session.", action="policy-explain", target=subject_id, error_type="path_security")
        return run
    if normalized == "task":
        task = store.get_task(subject_id)
        if task.session_id != session_id:
            raise _DeniedToolCall("Task is not linked to this session.", action="policy-explain", target=subject_id, error_type="path_security")
        return task
    return None


def _validate_direct_write_target(root: Path, requested: str, excludes: list[str]) -> str:
    if not requested:
        raise PatchValidationError("Missing direct-write path.")
    try:
        path = resolve_under_project(root, requested)
        relative_path = relative_to_project(root, path)
    except PathSecurityError as exc:
        raise _DeniedToolCall(str(exc), action="write", target=requested, error_type="path_security") from exc
    if _is_blocked_edit_path(relative_path):
        raise _DeniedToolCall(f"Blocked write path: {relative_path}", action="write", target=relative_path, error_type="path_security")
    try:
        assert_not_secret_path(path)
    except Exception as exc:
        raise _DeniedToolCall(str(exc), action="write", target=relative_path, error_type="secret_path") from exc
    if is_excluded_relative(relative_path, excludes):
        raise _DeniedToolCall(f"Write target is excluded from model context: {relative_path}", action="write", target=relative_path, error_type="context_excluded")
    if path.exists() and not path.is_file():
        raise PatchValidationError(f"Write target is not a file: {relative_path}")
    return relative_path


def _validate_docker_test_plan(root: Path, arguments: dict[str, Any]) -> str:
    _command, _cwd, target = _docker_test_plan_values(root, arguments)
    return target


def _docker_test_plan_values(root: Path, arguments: dict[str, Any]) -> tuple[list[str], str | None, str]:
    command_arg = arguments.get("command")
    command = command_arg if isinstance(command_arg, list) else [command_arg] if isinstance(command_arg, str) else []
    validate_test_command(command)
    cwd_arg = arguments.get("cwd")
    cwd = str(cwd_arg) if cwd_arg not in {None, ""} else None
    if cwd is not None:
        raw = Path(cwd)
        if raw.is_absolute():
            raise CommandValidationError("Docker test cwd must be project-relative.")
        try:
            resolved = resolve_under_project(root, raw)
        except PathSecurityError as exc:
            raise CommandValidationError(str(exc)) from exc
        if not resolved.exists():
            raise CommandValidationError(f"Docker test cwd does not exist: {cwd}")
        if not resolved.is_dir():
            raise CommandValidationError(f"Docker test cwd is not a directory: {cwd}")
        cwd = relative_to_project(root, resolved)
    return command, cwd, f"{cwd or '.'}:{' '.join(command)}"


def _resolve_allowed_path(root: Path, requested: str, excludes: list[str], *, action: str, allow_excluded: bool = False) -> Path:
    try:
        path = resolve_under_project(root, requested)
        rel = relative_to_project(root, path)
    except PathSecurityError as exc:
        raise _DeniedToolCall(str(exc), action=action, target=requested, error_type="path_security") from exc
    if is_excluded_relative(rel, excludes) and not allow_excluded:
        raise _DeniedToolCall(f"Path is excluded from context: {rel}", action=action, target=rel, error_type="context_excluded")
    try:
        assert_not_secret_path(path)
    except Exception as exc:
        raise _DeniedToolCall(str(exc), action=action, target=rel, error_type="secret_path") from exc
    return path


def _project_files(root: Path, excludes: list[str], *, start: str = ".", allow_excluded: bool = False) -> list[str]:
    start_path = _resolve_allowed_path(root, start, excludes, action="list", allow_excluded=allow_excluded)
    candidates = [start_path] if start_path.is_file() else [path for path in start_path.rglob("*") if path.is_file()]
    files: list[str] = []
    for path in sorted(candidates):
        rel = relative_to_project(root, path)
        if is_excluded_relative(rel, excludes) and not allow_excluded:
            continue
        if is_secret_path(path):
            continue
        files.append(rel)
    return files


def fnmatch_to_regex(pattern: str) -> str:
    escaped = re.escape(pattern).replace("\\*\\*", ".*").replace("\\*", "[^/]*")
    return escaped


def _persist_tool_output(
    store: SQLiteStore,
    run_id: str,
    session_id: str,
    tool_id: str,
    output: str,
    *,
    ok: bool,
    error_type: str | None,
) -> tuple[str, str | None, bool]:
    sanitized = str(sanitize_for_logging(output))
    encoded = sanitized.encode("utf-8")
    if len(encoded) <= TOOL_RESULT_INLINE_PREVIEW_BYTES:
        return sanitized, None, False
    preview = encoded[:TOOL_RESULT_INLINE_PREVIEW_BYTES].decode("utf-8", errors="ignore")
    path = store.runs_dir / run_id / f"session_tool_{tool_id}_output.txt"
    path.write_text(sanitized, encoding="utf-8")
    artifact = store.register_artifact(
        run_id,
        "session_tool_output",
        path,
        metadata={"tool_id": tool_id, "ok": ok, "error_type": error_type, "source": "session_tool"},
        producer="session_tool",
        redaction_state="redacted",
        session_id=session_id,
    )
    return preview, artifact.id, True
