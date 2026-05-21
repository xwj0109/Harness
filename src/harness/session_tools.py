from __future__ import annotations

import hashlib
import difflib
import json
import os
import re
import shlex
import ssl
import subprocess
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse

from pydantic import BaseModel, Field

from harness.config import load_config
from harness.governance.network import (
    GovernanceNetworkPolicy,
    build_session_tool_network_policy,
    evaluate_network_request,
    validate_network_policy,
    write_download_quarantine_record,
    write_network_policy_check,
    write_network_request_log,
)
from harness.governance.applyback import deferred_applyback_evidence
from harness.memory.sqlite_store import SQLiteStore
from harness.models import (
    EventStreamType,
    RedactionState,
    RunEventType,
    RunMode,
    SessionMessageRole,
    SessionPartKind,
    SessionPermissionBoundaryKind,
    SessionPermissionScope,
    SessionPermissionStatus,
)
from harness.operator_models import HarnessToolCallRecord
from harness.paths import PathSecurityError, is_excluded_relative, relative_to_project, resolve_under_project
from harness.policy import (
    backend_descriptor_sha256,
    effective_policy_sha256,
    resolve_agent_effective_policy,
    resolve_backend_effective_policy,
    resolve_task_effective_policy,
    resolve_workbench_effective_policy,
    stable_json_sha256,
)
from harness.process_supervisor import ProcessRecord, get_process_supervisor
from harness.registry import builtin_spec_registry
from harness.sandbox import CommandValidationError, validate_test_command
from harness.security import assert_not_secret_path, is_secret_path, sanitize_for_logging
from harness.session_cwd import CwdResolutionError, CwdResolver, cwd_recovery_message, session_cwd_from_metadata
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
    RERUN_FORBIDDEN = "rerun_forbidden"


class SessionToolRisk(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class SessionToolClass(str, Enum):
    READ_ONLY_PROJECT = "read_only_project"
    SESSION_LOCAL = "session_local"
    ACTIVE_REPO_WRITE = "active_repo_write"
    EXECUTION = "execution"
    EXTERNAL_NETWORK = "external_network"
    EXTENSION_BOUNDARY = "extension_boundary"


class SessionToolMaturity(str, Enum):
    IMPLEMENTED = "implemented"
    DISABLED_BY_DEFAULT = "disabled_by_default"
    PLANNING_ONLY = "planning_only"
    CONFIG_MISSING = "config_missing"
    CLIENT_UNSUPPORTED = "client_unsupported"
    MODEL_UNSUPPORTED = "model_unsupported"


class SessionToolPermissionDecisionStatus(str, Enum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


PLANNING_MODE_METADATA_KEY = "planning_mode"


HARNESS_SESSION_TOOL_IDS = [
    "pwd",
    "cd",
    "ls",
    "read",
    "glob",
    "find",
    "grep",
    "git-diff",
    "repo-overview",
    "artifact-read",
    "lsp-diagnostics",
    "lsp-symbols",
    "lsp-definition",
    "lsp-references",
    "todo",
    "question",
    "plan-enter",
    "plan-exit",
    "policy-explain",
    "invalid",
    "shell",
    "pty",
    "docker-test",
    "patch",
    "edit",
    "write",
    "direct-write",
    "managed-action",
    "web-fetch",
    "web-search",
    "repo-clone",
    "mcp",
    "mcp-resource",
    "plugin-tool",
    "skill-load",
    "task",
    "task-status",
]


class SessionToolPolicyProjection(BaseModel):
    schema_version: str = "harness.session_tool_policy_projection/v1"
    tool_id: str
    enabled: bool
    disabled_reason: str | None = None
    execution_supported: bool
    planning_only: bool
    permission_required: bool
    permission_key: str
    required_config: list[str] = Field(default_factory=list)
    required_client_capability: str | None = None
    required_model_capability: str | None = None
    boundary_kind: SessionPermissionBoundaryKind
    risk: SessionToolRisk
    replay_policy: SessionToolReplayPolicy
    policy_source: str = "session_tool_descriptor"
    maturity: list[SessionToolMaturity] = Field(default_factory=list)
    policy_reasons: list[str] = Field(default_factory=list)


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
    tool_class: SessionToolClass = SessionToolClass.READ_ONLY_PROJECT
    execution_supported: bool = True
    planning_only: bool = False
    disabled_reason: str | None = None
    source_inspiration: list[str] = Field(default_factory=lambda: ["harness"])
    requires_process_supervisor: bool = False
    requires_runtime: bool = False
    requires_project_config: bool = False
    requires_client_capability: str | None = None
    requires_model_capability: str | None = None
    feature_flag: str | None = None
    safety_notes: list[str] = Field(default_factory=list)
    policy: SessionToolPolicyProjection | None = None


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


def build_session_approval_card(
    store: SQLiteStore,
    session_id: str,
    permission_id: str,
    *,
    fallback_arguments: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a concise, operator-facing projection for an exact pending approval."""
    permission = store.get_session_permission(permission_id)
    if permission.session_id != session_id:
        raise ValueError(f"Permission {permission_id} does not belong to session {session_id}.")
    before_event = _latest_tool_call_before_event(store, session_id, permission)
    event_payload = before_event.payload if before_event is not None and isinstance(before_event.payload, dict) else {}
    record = event_payload.get("record") if isinstance(event_payload.get("record"), dict) else {}
    approval_target = event_payload.get("approval_target") if isinstance(event_payload.get("approval_target"), dict) else {}
    if not approval_target:
        normalized_args = record.get("normalized_args") if isinstance(record.get("normalized_args"), dict) else {}
        approval_target = (
            normalized_args.get("approval_target")
            if isinstance(normalized_args.get("approval_target"), dict)
            else {}
        )
    target_payload = _json_object_or_none(permission.normalized_target_pattern) or {}
    if not approval_target:
        approval_target = target_payload
    raw_args = record.get("raw_args") if isinstance(record.get("raw_args"), dict) else {}
    if not raw_args and isinstance(fallback_arguments, dict):
        raw_args = fallback_arguments
    command = (
        approval_target.get("normalized_command")
        or approval_target.get("command")
        or raw_args.get("command")
    )
    operation = (
        approval_target.get("normalized_operation")
        or command
        or permission.normalized_action
    )
    timeout_seconds = (
        approval_target.get("timeout_seconds")
        or approval_target.get("timeout")
        or raw_args.get("timeout_seconds")
        or raw_args.get("timeout")
    )
    cwd = approval_target.get("normalized_cwd") or approval_target.get("cwd") or record.get("cwd") or "."
    descriptor_ref: dict[str, Any] | None = None
    policy: dict[str, Any] | None = None
    try:
        descriptor = get_session_tool_descriptor(permission.tool_id)
        descriptor_ref = {
            "schema_version": descriptor.schema_version,
            "tool_id": descriptor.id,
            "permission_key": descriptor.permission_key,
            "boundary_kind": descriptor.boundary_kind.value,
            "risk": descriptor.risk.value,
            "descriptor_route": f"/tools/{descriptor.id}",
            "session_descriptor_route": f"/sessions/{session_id}/tools/{descriptor.id}",
        }
        policy = build_session_tool_policy_projection(descriptor).model_dump(mode="json")
    except Exception:
        descriptor_ref = None
        policy = None
    card = {
        "schema_version": "harness.session_approval_card/v1",
        "approval_id": permission.id,
        "permission_id": permission.id,
        "session_id": session_id,
        "run_id": permission.run_id,
        "tool_id": permission.tool_id,
        "cwd": cwd,
        "operation": operation,
        "command": command,
        "timeout_seconds": timeout_seconds,
        "shell_executable": approval_target.get("shell_executable") or raw_args.get("shell_executable") or raw_args.get("shell"),
        "sandbox_profile": approval_target.get("sandbox_profile"),
        "network_policy": approval_target.get("network_policy"),
        "env_policy": approval_target.get("env_policy"),
        "run_mode": approval_target.get("run_mode"),
        "boundary_kind": permission.boundary_kind.value,
        "risk": permission.risk,
        "status": permission.status.value,
        "scope": permission.scope.value,
        "policy_reasons": list(permission.policy_reasons),
        "descriptor_ref": descriptor_ref,
        "policy": policy,
        "approval_target": approval_target,
        "reply_route": f"/sessions/{session_id}/approval/{permission.id}",
        "approve_once": True,
        "resume_supported": bool(raw_args),
    }
    return sanitize_for_logging(card)


def pending_session_tool_call_from_permission(
    store: SQLiteStore,
    session_id: str,
    permission_id: str,
) -> dict[str, Any] | None:
    permission = store.get_session_permission(permission_id)
    if permission.session_id != session_id:
        raise ValueError(f"Permission {permission_id} does not belong to session {session_id}.")
    before_event = _latest_tool_call_before_event(store, session_id, permission)
    if before_event is None or not isinstance(before_event.payload, dict):
        return None
    record = before_event.payload.get("record")
    if not isinstance(record, dict):
        return None
    raw_args = record.get("raw_args")
    if not isinstance(raw_args, dict):
        normalized_args = record.get("normalized_args") if isinstance(record.get("normalized_args"), dict) else {}
        candidate = normalized_args.get("arguments")
        raw_args = candidate if isinstance(candidate, dict) else {}
    tool_id = str(record.get("tool_id") or permission.tool_id)
    if not tool_id or not raw_args:
        return None
    return sanitize_for_logging(
        {
            "schema_version": "harness.pending_session_tool_call/v1",
            "project_root": None,
            "session_id": session_id,
            "tool_id": tool_id,
            "arguments": raw_args,
            "permission_id": permission_id,
            "run_id": permission.run_id,
            "tool_call_id": record.get("tool_call_id"),
            "approval_card": build_session_approval_card(store, session_id, permission_id),
        }
    )


def persist_session_tool_denial(
    store: SQLiteStore,
    session_id: str,
    permission_id: str,
    *,
    feedback: str | None = None,
) -> dict[str, Any]:
    permission = store.get_session_permission(permission_id)
    if permission.session_id != session_id:
        raise ValueError(f"Permission {permission_id} does not belong to session {session_id}.")
    card = build_session_approval_card(store, session_id, permission_id)
    feedback_text = str(sanitize_for_logging(feedback or "")).strip()
    lines = [
        "Tool call denied by operator.",
        f"Tool: {permission.tool_id}",
        f"Permission: {permission.id}",
    ]
    if feedback_text:
        lines.append(f"Feedback: {feedback_text}")
    text = "\n".join(lines)
    event = store.append_store_event(
        EventStreamType.SESSION,
        session_id,
        "harness.approval.denied",
        {
            "permission_id": permission_id,
            "tool_id": permission.tool_id,
            "feedback": feedback_text or None,
            "approval_card": card,
            "model_visible_error": text,
            "summary": f"{permission.tool_id} denied",
        },
        session_id=session_id,
        run_id=permission.run_id,
        redaction_state=RedactionState.REDACTED,
    )
    message = store.append_session_message(
        session_id,
        SessionMessageRole.TOOL,
        text,
        run_id=permission.run_id,
    )
    store.append_session_part(
        session_id,
        message.id,
        SessionPartKind.TOOL_RESULT,
        text=text,
        run_id=permission.run_id,
        metadata={
            "tool_id": permission.tool_id,
            "ok": False,
            "error_type": "permission_denied",
            "permission_id": permission_id,
            "model_visible": True,
            "permission_granting": False,
        },
        redaction_state=RedactionState.REDACTED,
    )
    return {
        "schema_version": "harness.session_tool_denial/v1",
        "permission_id": permission_id,
        "tool_id": permission.tool_id,
        "feedback": feedback_text or None,
        "model_visible_error": text,
        "event": event.model_dump(mode="json"),
        "approval_card": card,
    }


def _latest_tool_call_before_event(store: SQLiteStore, session_id: str, permission: Any) -> Any | None:
    if not permission.run_id:
        return None
    events = store.list_session_store_events(session_id)
    candidates = [
        event
        for event in events
        if event.kind == "harness.tool_call.before"
        and event.run_id == permission.run_id
        and isinstance(event.payload, dict)
    ]
    return candidates[-1] if candidates else None


@dataclass(frozen=True)
class SessionToolCallGate:
    descriptor: SessionToolDescriptor
    decision: SessionToolPermissionDecision
    record: HarnessToolCallRecord
    effective_policy_sha256: str
    approval_target: dict[str, Any]
    allow_excluded: bool = False


TOOL_RESULT_INLINE_PREVIEW_BYTES = 16 * 1024
WEB_FETCH_MAX_RESPONSE_BYTES = 5 * 1024 * 1024
WEB_SEARCH_EXA_MCP_URL = "https://mcp.exa.ai/mcp"
WEB_SEARCH_PARALLEL_MCP_URL = "https://search.parallel.ai/mcp"
REPO_OVERVIEW_STRUCTURE_LIMIT = 200
REPO_OVERVIEW_IGNORED_DIRS = {
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "dist",
    "build",
    ".next",
    "target",
    "vendor",
}
REPO_OVERVIEW_DEPENDENCY_FILES = [
    "package.json",
    "package-lock.json",
    "bun.lock",
    "bun.lockb",
    "pnpm-lock.yaml",
    "yarn.lock",
    "requirements.txt",
    "pyproject.toml",
    "go.mod",
    "Cargo.toml",
    "Gemfile",
    "build.gradle",
    "build.gradle.kts",
    "pom.xml",
    "composer.json",
]
STATIC_SYMBOL_SUFFIXES = {".py", ".js", ".jsx", ".ts", ".tsx"}


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
    descriptors = [
        SessionToolDescriptor(
            id="pwd",
            title="Print session cwd",
            description="Show the active project root, session cwd, and resolved absolute cwd.",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {},
            },
            output_schema=_TEXT_OUTPUT_SCHEMA,
            side_effect=SessionToolSideEffect.SESSION_LOCAL,
            risk=SessionToolRisk.LOW,
            boundary_kind=SessionPermissionBoundaryKind.LOCAL_ONLY,
            permission_key="tool.pwd.session_cwd",
            permission_required=False,
            replay_policy=SessionToolReplayPolicy.EVENT_AND_PREVIEW,
            allowed_in_plan_agent=True,
            safety_notes=notes + ["Pwd reads durable session cwd state and starts no process."],
        ),
        SessionToolDescriptor(
            id="cd",
            title="Change session cwd",
            description="Change the durable session cwd inside the active project without starting a process.",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "path": {"type": "string"},
                    "actor": {"type": "string", "enum": ["operator", "model"], "default": "model"},
                },
                "required": ["path"],
            },
            output_schema=_TEXT_OUTPUT_SCHEMA,
            side_effect=SessionToolSideEffect.SESSION_LOCAL,
            risk=SessionToolRisk.LOW,
            boundary_kind=SessionPermissionBoundaryKind.LOCAL_ONLY,
            permission_key="tool.cd.session_cwd",
            permission_required=False,
            replay_policy=SessionToolReplayPolicy.EVENT_AND_PREVIEW,
            allowed_in_plan_agent=True,
            safety_notes=notes
            + [
                "Cd is a session state transition, not shell process execution.",
                "Moving outside the active project is rejected so the UI can propose /project or /workspace instead.",
            ],
        ),
        SessionToolDescriptor(
            id="read",
            title="Read file",
            description="Read a file inside the project boundary and return a redacted preview.",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {"path": {"type": "string"}, "cwd": {"type": ["string", "null"]}},
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
                    "cwd": {"type": ["string", "null"]},
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
                    "cwd": {"type": ["string", "null"]},
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
            id="git-diff",
            title="Git diff",
            description="Show read-only git diff output for the active project, optionally scoped to cwd or path.",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "path": {"type": ["string", "null"]},
                    "cwd": {"type": ["string", "null"]},
                    "stat_only": {"type": "boolean", "default": False},
                },
            },
            output_schema=_TEXT_OUTPUT_SCHEMA,
            side_effect=SessionToolSideEffect.NONE,
            risk=SessionToolRisk.LOW,
            boundary_kind=SessionPermissionBoundaryKind.LOCAL_ONLY,
            permission_key="tool.git_diff.project_read",
            permission_required=False,
            replay_policy=SessionToolReplayPolicy.ARTIFACT_FOR_LARGE_OUTPUT,
            allowed_in_plan_agent=True,
            safety_notes=notes + ["Git diff is read-only and must never start git mutation commands."],
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
            id="lsp-diagnostics",
            title="LSP diagnostics",
            description="Show configured LSP diagnostic status without starting language-server processes.",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {"path": {"type": "string"}},
            },
            output_schema=_TEXT_OUTPUT_SCHEMA,
            side_effect=SessionToolSideEffect.NONE,
            risk=SessionToolRisk.LOW,
            boundary_kind=SessionPermissionBoundaryKind.LOCAL_ONLY,
            permission_key="tool.lsp_diagnostics.project_config",
            permission_required=False,
            replay_policy=SessionToolReplayPolicy.ARTIFACT_FOR_LARGE_OUTPUT,
            allowed_in_plan_agent=True,
            enabled=True,
            safety_notes=notes
            + [
                "This projection does not launch LSP servers or read file contents; process-backed LSP operations remain deferred.",
            ],
        ),
        SessionToolDescriptor(
            id="lsp-symbols",
            title="LSP symbols",
            description="List project symbols with a static scan without starting language-server processes.",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "query": {"type": "string"},
                    "path": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 500},
                },
            },
            output_schema=_TEXT_OUTPUT_SCHEMA,
            side_effect=SessionToolSideEffect.NONE,
            risk=SessionToolRisk.LOW,
            boundary_kind=SessionPermissionBoundaryKind.LOCAL_ONLY,
            permission_key="tool.lsp_symbols.project_files",
            permission_required=False,
            replay_policy=SessionToolReplayPolicy.ARTIFACT_FOR_LARGE_OUTPUT,
            allowed_in_plan_agent=True,
            enabled=True,
            safety_notes=notes
            + [
                "This projection uses static symbol scanning only; process-backed workspaceSymbol/documentSymbol remain deferred.",
            ],
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
                "Evidence must state that apply-back, active workspace mutation, and git mutation are disabled.",
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
                "properties": {
                    "command": {"oneOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]},
                    "cwd": {"type": ["string", "null"]},
                },
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
                "Evidence must state that active workspace mutation, git mutation, and apply are disabled.",
            ],
        ),
        SessionToolDescriptor(
            id="shell",
            title="Shell command",
            description="Run a permissioned, auditable, bounded session shell command.",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "command": {"type": "string"},
                    "cwd": {"type": ["string", "null"]},
                    "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 900, "default": 120},
                    "shell_executable": {"type": ["string", "null"]},
                },
                "required": ["command"],
            },
            output_schema=_TEXT_OUTPUT_SCHEMA,
            side_effect=SessionToolSideEffect.EXECUTION,
            risk=SessionToolRisk.HIGH,
            boundary_kind=SessionPermissionBoundaryKind.SHELL,
            permission_key="tool.shell.execution",
            permission_required=True,
            replay_policy=SessionToolReplayPolicy.RERUN_FORBIDDEN,
            enabled=True,
            safety_notes=notes
            + [
                "Shell is exact-permission required and non-idempotent by default.",
                "Simple cd <path> is routed to the session-local cd tool and starts no process.",
                "No shell command is considered read-only unless added to a deliberate allowlist.",
            ],
        ),
        SessionToolDescriptor(
            id="web-fetch",
            title="Web fetch",
            description="Plan a URL fetch through an external-network policy gate.",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "url": {"type": "string"},
                    "format": {"type": "string", "enum": ["markdown", "text", "html"], "default": "markdown"},
                    "timeout": {"type": "number", "minimum": 1, "maximum": 120, "default": 30},
                },
                "required": ["url"],
            },
            output_schema=_TEXT_OUTPUT_SCHEMA,
            side_effect=SessionToolSideEffect.NETWORK,
            risk=SessionToolRisk.HIGH,
            boundary_kind=SessionPermissionBoundaryKind.EXTERNAL_NETWORK,
            permission_key="tool.web_fetch.external_network",
            permission_required=True,
            replay_policy=SessionToolReplayPolicy.ARTIFACT_FOR_LARGE_OUTPUT,
            enabled=True,
            safety_notes=[
                "Descriptors are documentation and validation metadata, not permission grants.",
                "Fetch execution is approval-required and persists response content plus metadata artifacts before display.",
                "Network access must be externally approved and fully replayable before content is fetched.",
            ],
        ),
        SessionToolDescriptor(
            id="web-search",
            title="Web search",
            description="Search the web through an external-network policy gate and configured search provider.",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "query": {"type": "string"},
                    "num_results": {"type": "number", "minimum": 1, "maximum": 20, "default": 8},
                    "search_type": {"type": "string", "enum": ["auto", "fast", "deep"], "default": "auto"},
                    "livecrawl": {"type": "string", "enum": ["fallback", "preferred"], "default": "fallback"},
                    "context_max_characters": {"type": "number", "minimum": 1000, "maximum": 50000, "default": 10000},
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
            enabled=True,
            safety_notes=[
                "Descriptors are documentation and validation metadata, not permission grants.",
                "Search execution is approval-required and requires an explicit project web_tools search provider.",
                "Search results must be stored as replayable, redacted evidence before display.",
            ],
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
            safety_notes=disabled_notes
            + [
                "MCP calls remain disabled until origin, version/checksum, allowed scopes, exact tool name, exact arguments, and replay policy are visible in permission records.",
                "MCP calls must produce the same permission and evidence records as built-ins.",
            ],
        ),
        SessionToolDescriptor(
            id="mcp-resource",
            title="MCP resource",
            description="Read a configured cached MCP resource through the session permission envelope.",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {"server": {"type": "string"}, "uri": {"type": "string"}},
                "required": ["server", "uri"],
            },
            output_schema=_TEXT_OUTPUT_SCHEMA,
            side_effect=SessionToolSideEffect.NONE,
            risk=SessionToolRisk.MEDIUM,
            boundary_kind=SessionPermissionBoundaryKind.MCP,
            permission_key="tool.mcp_resource.execution",
            permission_required=True,
            replay_policy=SessionToolReplayPolicy.ARTIFACT_FOR_LARGE_OUTPUT,
            enabled=True,
            safety_notes=notes
            + [
                "Only configured cached MCP resources are read in this phase; no MCP process or network connection is started.",
                "MCP resource reads must use the same permission and evidence records as MCP tools.",
            ],
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
            safety_notes=disabled_notes
            + [
                "Plugin tools remain disabled until plugin origin, version/checksum, allowed scopes, exact tool name, exact arguments, and replay policy are visible in permission records.",
                "Plugin tool origin, scope, and version must be visible before any invocation.",
            ],
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
            enabled=True,
            safety_notes=notes
            + [
                "Skill loading is permission-gated because it injects local instruction text into the session context.",
                "Only configured project skill bodies are loaded in this phase; no plugin tools are registered.",
            ],
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
            id="repo-overview",
            title="Repository overview",
            description="Summarize the structure and likely entrypoints of a project-local repository directory.",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "path": {"type": "string", "default": "."},
                    "cwd": {"type": ["string", "null"]},
                    "repository": {"type": "string"},
                    "depth": {"type": "number", "minimum": 1, "maximum": 6, "default": 3},
                },
            },
            output_schema=_TEXT_OUTPUT_SCHEMA,
            side_effect=SessionToolSideEffect.NONE,
            risk=SessionToolRisk.LOW,
            boundary_kind=SessionPermissionBoundaryKind.LOCAL_ONLY,
            permission_key="tool.repo_overview.project_files",
            permission_required=False,
            replay_policy=SessionToolReplayPolicy.ARTIFACT_FOR_LARGE_OUTPUT,
            allowed_in_plan_agent=True,
            enabled=True,
            safety_notes=notes
            + [
                "Project-local overview is read-only; external cached repository overview remains deferred behind repo-clone/cache policy.",
            ],
        ),
        SessionToolDescriptor(
            id="repo-clone",
            title="Clone external repository",
            description="Plan an external repository clone through a managed network/cache boundary.",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "repository": {"type": "string"},
                    "url": {"type": "string"},
                    "refresh": {"type": "boolean", "default": False},
                    "branch": {"type": "string"},
                },
            },
            output_schema=_TEXT_OUTPUT_SCHEMA,
            side_effect=SessionToolSideEffect.NETWORK,
            risk=SessionToolRisk.HIGH,
            boundary_kind=SessionPermissionBoundaryKind.EXTERNAL_NETWORK,
            permission_key="tool.repo_clone.external_network",
            permission_required=True,
            replay_policy=SessionToolReplayPolicy.ARTIFACT_FOR_LARGE_OUTPUT,
            enabled=True,
            safety_notes=[
                "Descriptors are documentation and validation metadata, not permission grants.",
                "Clone execution is approval-required and writes only to the managed external repository cache.",
                "Repository cloning must use a managed external cache and network approval.",
            ],
        ),
    ]
    return _complete_session_tool_descriptors(descriptors, notes=notes, disabled_notes=disabled_notes)


def _complete_session_tool_descriptors(
    descriptors: list[SessionToolDescriptor],
    *,
    notes: list[str],
    disabled_notes: list[str],
) -> list[SessionToolDescriptor]:
    by_id: dict[str, SessionToolDescriptor] = {}
    for descriptor in descriptors:
        by_id[descriptor.id] = _decorate_session_tool_descriptor(descriptor)
    for descriptor in _planned_session_tool_descriptors(notes=notes, disabled_notes=disabled_notes):
        by_id.setdefault(descriptor.id, _decorate_session_tool_descriptor(descriptor))
    return [by_id[tool_id] for tool_id in HARNESS_SESSION_TOOL_IDS if tool_id in by_id]


def _decorate_session_tool_descriptor(descriptor: SessionToolDescriptor) -> SessionToolDescriptor:
    tool_class = _session_tool_class(descriptor.id)
    supported = _session_tool_execution_supported(descriptor.id)
    planning_only = _session_tool_planning_only(descriptor.id)
    disabled_reason = descriptor.disabled_reason
    if not descriptor.enabled and disabled_reason is None:
        disabled_reason = _session_tool_actionable_disabled_reason(descriptor, supported=supported)
    decorated = descriptor.model_copy(
        update={
            "tool_class": tool_class,
            "execution_supported": supported,
            "planning_only": planning_only,
            "disabled_reason": disabled_reason,
            "source_inspiration": _session_tool_source_inspiration(descriptor.id),
            "requires_process_supervisor": descriptor.id in {"shell", "pty", "docker-test", "task"},
            "requires_runtime": descriptor.id in {"task", "task-status", "plan-enter", "plan-exit"},
            "requires_project_config": descriptor.id in {"web-fetch", "web-search", "repo-clone", "mcp", "mcp-resource", "plugin-tool", "skill-load"},
            "requires_client_capability": _session_tool_client_capability(descriptor.id),
            "requires_model_capability": _session_tool_model_capability(descriptor.id),
            "feature_flag": _session_tool_feature_flag(descriptor.id),
        }
    )
    return decorated.model_copy(update={"policy": build_session_tool_policy_projection(decorated)})


def session_tool_catalog_projection(
    *,
    project_root: Path | None = None,
    tool_id: str | None = None,
    plan_only: bool = False,
) -> dict[str, Any]:
    descriptors = [get_session_tool_descriptor(tool_id)] if tool_id else default_session_tool_descriptors()
    if plan_only:
        descriptors = [descriptor for descriptor in descriptors if descriptor.allowed_in_plan_agent]
    return {
        "schema_version": "harness.session_tools/v1",
        "ok": True,
        "permission_granting": False,
        "policy_projection_schema_version": "harness.session_tool_policy_projection/v1",
        "policy_source": "session_tool_descriptor",
        "tools": [
            session_tool_descriptor_payload(descriptor, project_root=project_root)
            for descriptor in descriptors
        ],
    }


def session_tool_descriptor_payload(
    descriptor: SessionToolDescriptor,
    *,
    project_root: Path | None = None,
) -> dict[str, Any]:
    payload = descriptor.model_dump(mode="json")
    payload["policy"] = build_session_tool_policy_projection(descriptor, project_root=project_root).model_dump(mode="json")
    return payload


def build_session_tool_policy_projection(
    descriptor: SessionToolDescriptor,
    *,
    project_root: Path | None = None,
) -> SessionToolPolicyProjection:
    required_config = _session_tool_required_config(descriptor.id)
    missing_config = _session_tool_missing_config(descriptor.id, project_root) if project_root is not None else []
    maturity: list[SessionToolMaturity] = []
    policy_reasons: list[str] = []
    effective_enabled = bool(descriptor.enabled)

    if descriptor.execution_supported and descriptor.enabled and not descriptor.planning_only:
        maturity.append(SessionToolMaturity.IMPLEMENTED)
    if not descriptor.enabled:
        maturity.append(SessionToolMaturity.DISABLED_BY_DEFAULT)
        effective_enabled = False
        policy_reasons.append(
            descriptor.disabled_reason
            or _session_tool_actionable_disabled_reason(descriptor, supported=descriptor.execution_supported)
        )
    if descriptor.planning_only:
        maturity.append(SessionToolMaturity.PLANNING_ONLY)
        policy_reasons.append("Execution is currently planning-only; it records evidence but does not perform the side effect.")
    if missing_config:
        maturity.append(SessionToolMaturity.CONFIG_MISSING)
        effective_enabled = False
        policy_reasons.append("Missing project configuration: " + ", ".join(missing_config))
    if descriptor.requires_client_capability:
        maturity.append(SessionToolMaturity.CLIENT_UNSUPPORTED)
        effective_enabled = False
        policy_reasons.append(f"Requires client capability: {descriptor.requires_client_capability}")
    if descriptor.requires_model_capability:
        maturity.append(SessionToolMaturity.MODEL_UNSUPPORTED)
        effective_enabled = False
        policy_reasons.append(f"Requires model capability: {descriptor.requires_model_capability}")
    if not maturity:
        maturity.append(SessionToolMaturity.IMPLEMENTED)

    disabled_reason = None
    if not effective_enabled:
        if policy_reasons:
            disabled_reason = policy_reasons[0]
        else:
            disabled_reason = _session_tool_actionable_disabled_reason(
                descriptor,
                supported=descriptor.execution_supported,
            )
    return SessionToolPolicyProjection(
        tool_id=descriptor.id,
        enabled=effective_enabled,
        disabled_reason=disabled_reason,
        execution_supported=descriptor.execution_supported,
        planning_only=descriptor.planning_only,
        permission_required=descriptor.permission_required,
        permission_key=descriptor.permission_key,
        required_config=required_config,
        required_client_capability=descriptor.requires_client_capability,
        required_model_capability=descriptor.requires_model_capability,
        boundary_kind=descriptor.boundary_kind,
        risk=descriptor.risk,
        replay_policy=descriptor.replay_policy,
        maturity=list(dict.fromkeys(maturity)),
        policy_reasons=list(dict.fromkeys(policy_reasons)),
    )


def _session_tool_actionable_disabled_reason(
    descriptor: SessionToolDescriptor,
    *,
    supported: bool,
) -> str:
    if not supported:
        return (
            f"{descriptor.id} is registered for catalog parity, but execution is not implemented yet; "
            "add a Harness adapter, evidence path, and permission policy before enabling it."
        )
    if descriptor.feature_flag:
        return f"{descriptor.id} is disabled by default; enable and configure the {descriptor.feature_flag} boundary first."
    return f"{descriptor.id} is disabled by default; enable it only after its policy boundary is configured."


def _planned_session_tool_descriptors(
    *,
    notes: list[str],
    disabled_notes: list[str],
) -> list[SessionToolDescriptor]:
    planned = disabled_notes + ["This public catalog entry is registered for parity visibility before execution is implemented."]
    return [
        SessionToolDescriptor(
            id="ls",
            title="List directory",
            description="List directory entries inside the project boundary with truncation and ignores.",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "path": {"type": "string", "default": "."},
                    "cwd": {"type": ["string", "null"]},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 200},
                    "include_hidden": {"type": "boolean", "default": False},
                },
            },
            output_schema=_TEXT_OUTPUT_SCHEMA,
            side_effect=SessionToolSideEffect.NONE,
            risk=SessionToolRisk.LOW,
            boundary_kind=SessionPermissionBoundaryKind.LOCAL_ONLY,
            permission_key="tool.ls.project_files",
            permission_required=False,
            replay_policy=SessionToolReplayPolicy.ARTIFACT_FOR_LARGE_OUTPUT,
            allowed_in_plan_agent=True,
            enabled=True,
            safety_notes=notes + ["Ls is read-only, honors context/secret filters, and never shells out."],
        ),
        SessionToolDescriptor(
            id="find",
            title="Find files",
            description="Find project files by human-friendly query without reading file bodies.",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "query": {"type": "string"},
                    "path": {"type": "string", "default": "."},
                    "cwd": {"type": ["string", "null"]},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 100},
                    "include_hidden": {"type": "boolean", "default": False},
                },
                "required": ["query"],
            },
            output_schema=_TEXT_OUTPUT_SCHEMA,
            side_effect=SessionToolSideEffect.NONE,
            risk=SessionToolRisk.LOW,
            boundary_kind=SessionPermissionBoundaryKind.LOCAL_ONLY,
            permission_key="tool.find.project_files",
            permission_required=False,
            replay_policy=SessionToolReplayPolicy.ARTIFACT_FOR_LARGE_OUTPUT,
            allowed_in_plan_agent=True,
            enabled=True,
            safety_notes=notes + ["Find is filename/path discovery only; content search remains the grep tool."],
        ),
        SessionToolDescriptor(
            id="lsp-definition",
            title="LSP definition",
            description="Find a symbol definition using static code intelligence without starting language-server processes.",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "path": {"type": "string"},
                    "line": {"type": "integer", "minimum": 1},
                    "character": {"type": "integer", "minimum": 0},
                    "symbol": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
                },
            },
            output_schema=_TEXT_OUTPUT_SCHEMA,
            side_effect=SessionToolSideEffect.NONE,
            risk=SessionToolRisk.LOW,
            boundary_kind=SessionPermissionBoundaryKind.LOCAL_ONLY,
            permission_key="tool.lsp_definition.project_files",
            permission_required=False,
            replay_policy=SessionToolReplayPolicy.ARTIFACT_FOR_LARGE_OUTPUT,
            allowed_in_plan_agent=True,
            enabled=True,
            safety_notes=notes
            + [
                "This projection uses static symbol scanning only; no language-server process is started.",
                "Process-backed textDocument/definition remains deferred behind Harness process policy.",
            ],
        ),
        SessionToolDescriptor(
            id="lsp-references",
            title="LSP references",
            description="Find static symbol references without starting language-server processes.",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "path": {"type": "string"},
                    "line": {"type": "integer", "minimum": 1},
                    "character": {"type": "integer", "minimum": 0},
                    "symbol": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 200},
                },
            },
            output_schema=_TEXT_OUTPUT_SCHEMA,
            side_effect=SessionToolSideEffect.NONE,
            risk=SessionToolRisk.LOW,
            boundary_kind=SessionPermissionBoundaryKind.LOCAL_ONLY,
            permission_key="tool.lsp_references.project_files",
            permission_required=False,
            replay_policy=SessionToolReplayPolicy.ARTIFACT_FOR_LARGE_OUTPUT,
            allowed_in_plan_agent=True,
            enabled=True,
            safety_notes=notes
            + [
                "This projection uses static identifier scanning only; no language-server process is started.",
                "Process-backed textDocument/references remains deferred behind Harness process policy.",
            ],
        ),
        SessionToolDescriptor(
            id="plan-enter",
            title="Enter planning mode",
            description="Enter explicit read-only planning mode for the current session.",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {"reason": {"type": "string"}},
            },
            output_schema=_TEXT_OUTPUT_SCHEMA,
            side_effect=SessionToolSideEffect.SESSION_LOCAL,
            risk=SessionToolRisk.LOW,
            boundary_kind=SessionPermissionBoundaryKind.LOCAL_ONLY,
            permission_key="tool.plan_enter.session",
            permission_required=False,
            replay_policy=SessionToolReplayPolicy.EVENT_AND_PREVIEW,
            allowed_in_plan_agent=True,
            enabled=True,
            safety_notes=notes + ["Planning mode is session-local and must not grant authority."],
        ),
        SessionToolDescriptor(
            id="plan-exit",
            title="Exit planning mode",
            description="Exit planning mode with a persisted summary and proposed next action.",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "summary": {"type": "string"},
                    "next_action": {"type": "string"},
                    "proposed_tools": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["summary"],
            },
            output_schema=_TEXT_OUTPUT_SCHEMA,
            side_effect=SessionToolSideEffect.SESSION_LOCAL,
            risk=SessionToolRisk.LOW,
            boundary_kind=SessionPermissionBoundaryKind.LOCAL_ONLY,
            permission_key="tool.plan_exit.session",
            permission_required=False,
            replay_policy=SessionToolReplayPolicy.EVENT_AND_PREVIEW,
            allowed_in_plan_agent=True,
            enabled=True,
            safety_notes=notes + ["Plan summaries are evidence, not approval grants."],
        ),
        SessionToolDescriptor(
            id="invalid",
            title="Invalid tool call",
            description="Persist a model-visible result for an unknown or malformed tool call.",
            input_schema={
                "type": "object",
                "additionalProperties": True,
                "properties": {
                    "requested_tool_id": {"type": "string"},
                    "arguments": {"type": "object"},
                    "reason": {"type": "string"},
                },
                "required": ["requested_tool_id"],
            },
            output_schema=_TEXT_OUTPUT_SCHEMA,
            side_effect=SessionToolSideEffect.SESSION_LOCAL,
            risk=SessionToolRisk.LOW,
            boundary_kind=SessionPermissionBoundaryKind.LOCAL_ONLY,
            permission_key="tool.invalid.session",
            permission_required=False,
            replay_policy=SessionToolReplayPolicy.EVENT_AND_PREVIEW,
            allowed_in_plan_agent=True,
            enabled=True,
            safety_notes=notes + ["Invalid tool calls are recorded as transcript evidence so the model can recover."],
        ),
        SessionToolDescriptor(
            id="edit",
            title="Edit file",
            description="Apply a targeted old/new text replacement with strict validation.",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "path": {"type": "string"},
                    "old": {"type": "string"},
                    "new": {"type": "string"},
                    "expected_replacements": {"type": "integer", "minimum": 1, "default": 1},
                    "mode": {"type": "string", "enum": ["apply", "plan"], "default": "apply"},
                },
                "required": ["path", "old", "new"],
            },
            output_schema=_TEXT_OUTPUT_SCHEMA,
            side_effect=SessionToolSideEffect.MUTATION,
            risk=SessionToolRisk.HIGH,
            boundary_kind=SessionPermissionBoundaryKind.ACTIVE_REPO_WRITE,
            permission_key="tool.edit.active_repo_write",
            permission_required=True,
            replay_policy=SessionToolReplayPolicy.ARTIFACT_FOR_LARGE_OUTPUT,
            enabled=True,
            safety_notes=notes + ["Plan mode does not mutate; apply mode requires exact active-repo-write approval."],
        ),
        SessionToolDescriptor(
            id="write",
            title="Write file",
            description="Create or replace a whole project file with approval and evidence.",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                    "mode": {"type": "string", "enum": ["apply", "plan"], "default": "apply"},
                    "create_dirs": {"type": "boolean", "default": False},
                },
                "required": ["path", "content"],
            },
            output_schema=_TEXT_OUTPUT_SCHEMA,
            side_effect=SessionToolSideEffect.MUTATION,
            risk=SessionToolRisk.HIGH,
            boundary_kind=SessionPermissionBoundaryKind.ACTIVE_REPO_WRITE,
            permission_key="tool.write.active_repo_write",
            permission_required=True,
            replay_policy=SessionToolReplayPolicy.ARTIFACT_FOR_LARGE_OUTPUT,
            enabled=True,
            safety_notes=notes + ["Plan mode does not mutate; apply mode requires exact active-repo-write approval."],
        ),
        SessionToolDescriptor(
            id="task",
            title="Start task",
            description="Create bounded child task/session records for delegated Harness-native work.",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "objective": {"type": "string"},
                    "allowed_tools": {"type": "array", "items": {"type": "string"}},
                    "boundary": {"type": "string"},
                    "output_expectation": {"type": "string"},
                    "agent": {"type": "string"},
                    "title": {"type": "string"},
                },
                "required": ["objective", "allowed_tools", "boundary", "output_expectation"],
            },
            output_schema=_TEXT_OUTPUT_SCHEMA,
            side_effect=SessionToolSideEffect.EXECUTION,
            risk=SessionToolRisk.MEDIUM,
            boundary_kind=SessionPermissionBoundaryKind.LOCAL_ONLY,
            permission_key="tool.task.execution",
            permission_required=True,
            replay_policy=SessionToolReplayPolicy.EVENT_AND_PREVIEW,
            enabled=True,
            safety_notes=notes
            + [
                "Task creation persists child session/task records but does not start a hidden process.",
                "Subagent execution requires scoped agent profiles and runtime ownership.",
            ],
        ),
        SessionToolDescriptor(
            id="task-status",
            title="Task status",
            description="Inspect a linked background task or delegated child session status and evidence.",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {"task_id": {"type": "string"}, "session_id": {"type": "string"}},
            },
            output_schema=_TEXT_OUTPUT_SCHEMA,
            side_effect=SessionToolSideEffect.SESSION_LOCAL,
            risk=SessionToolRisk.LOW,
            boundary_kind=SessionPermissionBoundaryKind.LOCAL_ONLY,
            permission_key="tool.task_status.session",
            permission_required=False,
            replay_policy=SessionToolReplayPolicy.EVENT_AND_PREVIEW,
            allowed_in_plan_agent=True,
            enabled=True,
            safety_notes=notes + ["Task status is read-only over persisted Harness records."],
        ),
    ]


def _disabled_descriptor(
    tool_id: str,
    title: str,
    description: str,
    *,
    input_properties: dict[str, Any] | None = None,
    required: list[str] | None = None,
    permission_key: str,
    side_effect: SessionToolSideEffect = SessionToolSideEffect.NONE,
    risk: SessionToolRisk = SessionToolRisk.LOW,
    boundary_kind: SessionPermissionBoundaryKind = SessionPermissionBoundaryKind.LOCAL_ONLY,
    permission_required: bool = False,
    safety_notes: list[str] | None = None,
) -> SessionToolDescriptor:
    return SessionToolDescriptor(
        id=tool_id,
        title=title,
        description=description,
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": input_properties or {},
            **({"required": required} if required else {}),
        },
        output_schema=_TEXT_OUTPUT_SCHEMA,
        side_effect=side_effect,
        risk=risk,
        boundary_kind=boundary_kind,
        permission_key=permission_key,
        permission_required=permission_required,
        replay_policy=SessionToolReplayPolicy.ARTIFACT_FOR_LARGE_OUTPUT,
        enabled=False,
        execution_supported=False,
        disabled_reason="not implemented yet",
        safety_notes=safety_notes or [],
    )


def _session_tool_class(tool_id: str) -> SessionToolClass:
    if tool_id in {
        "pwd",
        "ls",
        "read",
        "glob",
        "find",
        "grep",
        "git-diff",
        "repo-overview",
        "artifact-read",
        "lsp-diagnostics",
        "lsp-symbols",
        "lsp-definition",
        "lsp-references",
        "policy-explain",
    }:
        return SessionToolClass.READ_ONLY_PROJECT
    if tool_id in {"cd", "todo", "question", "plan-enter", "plan-exit", "skill-load", "task-status", "invalid"}:
        return SessionToolClass.SESSION_LOCAL
    if tool_id in {"patch", "edit", "write", "direct-write", "managed-action"}:
        return SessionToolClass.ACTIVE_REPO_WRITE
    if tool_id in {"shell", "pty", "docker-test", "task"}:
        return SessionToolClass.EXECUTION
    if tool_id in {"web-fetch", "web-search", "repo-clone"}:
        return SessionToolClass.EXTERNAL_NETWORK
    if tool_id in {"mcp", "mcp-resource", "plugin-tool"}:
        return SessionToolClass.EXTENSION_BOUNDARY
    return SessionToolClass.SESSION_LOCAL


def _session_tool_execution_supported(tool_id: str) -> bool:
    return tool_id in {
        "pwd",
        "cd",
        "ls",
        "read",
        "glob",
        "find",
        "grep",
        "git-diff",
        "repo-overview",
        "artifact-read",
        "lsp-diagnostics",
        "lsp-symbols",
        "lsp-definition",
        "lsp-references",
        "todo",
        "question",
        "plan-enter",
        "plan-exit",
        "policy-explain",
        "invalid",
        "shell",
        "docker-test",
        "patch",
        "edit",
        "write",
        "direct-write",
        "web-fetch",
        "web-search",
        "repo-clone",
        "mcp-resource",
        "skill-load",
        "task",
        "task-status",
    }


def _session_tool_planning_only(tool_id: str) -> bool:
    return tool_id in {"patch", "direct-write", "docker-test"} or not _session_tool_execution_supported(tool_id)


def _session_tool_source_inspiration(tool_id: str) -> list[str]:
    sources = ["harness"]
    if tool_id in {"read", "shell", "edit", "write", "grep", "find", "ls"}:
        sources.append("pi")
    if tool_id in {
        "shell",
        "read",
        "glob",
        "grep",
        "edit",
        "write",
        "task",
        "task-status",
        "web-fetch",
        "web-search",
        "todo",
        "repo-clone",
        "repo-overview",
        "skill-load",
        "patch",
        "question",
        "lsp-diagnostics",
        "lsp-symbols",
        "lsp-definition",
        "lsp-references",
        "plan-enter",
        "plan-exit",
        "plugin-tool",
        "invalid",
    }:
        sources.append("opencode")
    return list(dict.fromkeys(sources))


def _session_tool_client_capability(tool_id: str) -> str | None:
    if tool_id == "pty":
        return "pty"
    if tool_id in {"plan-enter", "plan-exit"}:
        return "planning_mode"
    if tool_id in {"task", "task-status"}:
        return "background_tasks"
    return None


def _session_tool_model_capability(tool_id: str) -> str | None:
    if tool_id in {"task", "task-status"}:
        return "tool_delegation"
    return None


def _session_tool_feature_flag(tool_id: str) -> str | None:
    if tool_id in {"mcp", "mcp-resource"}:
        return "mcp"
    if tool_id == "plugin-tool":
        return "plugins"
    if tool_id in {"web-fetch", "web-search"}:
        return "web_tools"
    if tool_id == "pty":
        return "pty"
    if tool_id in {"task", "task-status"}:
        return "subagents"
    return None


def _session_tool_required_config(tool_id: str) -> list[str]:
    return {
        "web-fetch": ["web_tools.enabled", "web_tools.fetch_enabled"],
        "web-search": [
            "web_tools.enabled",
            "web_tools.search_enabled",
            "web_tools.search_provider or web_tools.search_endpoint_url",
        ],
        "repo-clone": ["external repository cache policy"],
        "mcp": ["mcp.servers"],
        "mcp-resource": ["mcp.servers[].resources"],
        "plugin-tool": ["plugins.enabled", "plugin registry origin/version/scope metadata"],
        "skill-load": ["skills.enabled", "skills.project"],
        "pty": ["pty client capability"],
        "task": ["subagent profiles", "model tool_delegation capability"],
        "task-status": ["subagent profiles"],
    }.get(tool_id, [])


def _session_tool_missing_config(tool_id: str, project_root: Path | None) -> list[str]:
    if project_root is None:
        return []
    try:
        cfg = load_config(project_root)
    except FileNotFoundError:
        return _session_tool_required_config(tool_id)
    if tool_id == "web-fetch":
        missing = []
        if not cfg.web_tools.enabled:
            missing.append("web_tools.enabled")
        if not cfg.web_tools.fetch_enabled:
            missing.append("web_tools.fetch_enabled")
        return missing
    if tool_id == "web-search":
        missing = []
        if not cfg.web_tools.enabled:
            missing.append("web_tools.enabled")
        if not cfg.web_tools.search_enabled:
            missing.append("web_tools.search_enabled")
        search_provider = getattr(cfg.web_tools, "search_provider", "configured_http")
        if search_provider == "configured_http" and not getattr(cfg.web_tools, "search_endpoint_url", None):
            missing.append("web_tools.search_endpoint_url")
        return missing
    if tool_id in {"mcp", "mcp-resource"}:
        servers = getattr(cfg.mcp, "servers", {})
        if not servers:
            return ["mcp.servers"]
    if tool_id == "plugin-tool":
        missing = []
        if not getattr(cfg.plugins, "enabled", False):
            missing.append("plugins.enabled")
        return missing
    if tool_id == "skill-load":
        missing = []
        if not getattr(cfg.skills, "enabled", False):
            missing.append("skills.enabled")
        if not getattr(cfg.skills, "project", {}):
            missing.append("skills.project")
        return missing
    return []


def session_planning_mode_projection(metadata: dict[str, Any] | None) -> dict[str, Any]:
    raw = (metadata or {}).get(PLANNING_MODE_METADATA_KEY)
    if not isinstance(raw, dict):
        return {"active": False}
    proposed_tools_raw = raw.get("proposed_tools")
    proposed_tools = [str(item) for item in proposed_tools_raw] if isinstance(proposed_tools_raw, list) else []
    return {
        "active": bool(raw.get("active")),
        "entered_at": raw.get("entered_at") if raw.get("entered_at") is not None else None,
        "exited_at": raw.get("exited_at") if raw.get("exited_at") is not None else None,
        "reason": raw.get("reason") if raw.get("reason") is not None else None,
        "summary": raw.get("summary") if raw.get("summary") is not None else None,
        "next_action": raw.get("next_action") if raw.get("next_action") is not None else None,
        "proposed_tools": proposed_tools,
        "source": str(raw.get("source") or "session_tool"),
    }


def _session_planning_active(store: SQLiteStore, session_id: str) -> bool:
    return bool(session_planning_mode_projection(store.get_session(session_id).metadata).get("active"))


def get_session_tool_descriptor(tool_id: str) -> SessionToolDescriptor:
    for descriptor in default_session_tool_descriptors():
        if descriptor.id == tool_id:
            return descriptor
    raise KeyError(f"Session tool not found: {tool_id}")


def before_tool_call(
    store: SQLiteStore,
    project_root: Path,
    session_id: str,
    tool_id: str,
    arguments: dict[str, Any],
    *,
    tool_call_id: str | None = None,
    turn_id: str | None = None,
    run_id: str | None = None,
    run_mode: RunMode | str = RunMode.READ_ONLY,
) -> SessionToolCallGate:
    normalized_tool_id, normalized_args = _normalize_tool_identity(tool_id, arguments)
    descriptor = get_session_tool_descriptor(normalized_tool_id)
    validation_error = validate_session_tool_arguments(descriptor, normalized_args)
    if validation_error is not None:
        raise ValueError(validation_error)
    run_mode_value = RunMode(run_mode.value if isinstance(run_mode, RunMode) else run_mode)
    decision = decide_session_tool_permission(
        store,
        project_root,
        session_id,
        normalized_tool_id,
        normalized_args,
        run_mode=run_mode_value,
    )
    permission_state = _tool_call_permission_state(decision)
    record = HarnessToolCallRecord(
        tool_call_id=tool_call_id or f"call_{uuid.uuid4().hex[:12]}",
        turn_id=turn_id,
        session_id=session_id,
        tool_id=normalized_tool_id,
        raw_args=sanitize_for_logging(arguments),
        normalized_args=_normalized_record_args(
            project_root,
            store,
            session_id,
            normalized_tool_id,
            normalized_args,
            decision,
            run_mode=run_mode_value,
        ),
        cwd=_record_cwd(project_root, store, session_id, normalized_tool_id, decision),
        permission_state=permission_state,
        started_at=datetime.now(timezone.utc),
        status="running",
    )
    approval_target = _approval_target_payload(project_root, session_id, normalized_tool_id, decision, run_mode=run_mode_value)
    policy_snapshot = {
        "surface": "session_tool_gateway",
        "project_root": str(project_root.resolve()),
        "session_id": session_id,
        "tool_id": normalized_tool_id,
        "side_effect": descriptor.side_effect.value,
        "risk": descriptor.risk.value,
        "boundary_kind": descriptor.boundary_kind.value,
        "decision": decision.status.value,
        "run_mode": run_mode_value.value,
        "approval_target": approval_target,
    }
    policy_sha = stable_json_sha256(policy_snapshot)
    store.append_store_event(
        EventStreamType.SESSION,
        session_id,
        "harness.tool_call.before",
        {
            "record": record.model_dump(mode="json"),
            "decision": decision.model_dump(mode="json"),
            "approval_target": approval_target,
            "effective_policy_sha256": policy_sha,
            "side_effect": descriptor.side_effect.value,
            "risk": descriptor.risk.value,
            "summary": f"{normalized_tool_id} {decision.status.value}",
        },
        session_id=session_id,
        run_id=run_id,
        redaction_state=RedactionState.REDACTED,
    )
    return SessionToolCallGate(
        descriptor=descriptor,
        decision=decision,
        record=record,
        effective_policy_sha256=policy_sha,
        approval_target=approval_target,
        allow_excluded=bool(decision.reasons and "existing session permission" in decision.reasons[0]),
    )


def after_tool_call(
    store: SQLiteStore,
    gate: SessionToolCallGate,
    result: SessionToolExecutionResult,
) -> HarnessToolCallRecord:
    status = _tool_call_status_from_result(result)
    permission_state = gate.record.permission_state
    if result.error_type == "permission_required":
        permission_state = "pending"
    elif result.error_type in {"permission_denied", "path_security", "secret_path"}:
        permission_state = "denied"
    elif gate.decision.status == SessionToolPermissionDecisionStatus.ALLOW and gate.record.permission_state == "approved":
        permission_state = "approved"
    elif gate.decision.status == SessionToolPermissionDecisionStatus.ALLOW:
        permission_state = "not_required"
    record = gate.record.model_copy(
        update={
            "permission_state": permission_state,
            "finished_at": datetime.now(timezone.utc),
            "status": status,
            "result_artifact_ids": [result.artifact_id] if result.artifact_id else [],
        }
    )
    store.append_store_event(
        EventStreamType.SESSION,
        result.session_id,
        "harness.tool_call.after",
        {
            "record": record.model_dump(mode="json"),
            "result": {
                "ok": result.ok,
                "tool_id": result.tool_id,
                "run_id": result.run_id,
                "artifact_id": result.artifact_id,
                "truncated": result.truncated,
                "error_type": result.error_type,
                "permission_id": result.permission_id,
                "preview": result.preview[:4096],
            },
            "effective_policy_sha256": gate.effective_policy_sha256,
            "summary": f"{result.tool_id} {status}",
        },
        session_id=result.session_id,
        run_id=result.run_id,
        artifact_id=result.artifact_id,
        redaction_state=RedactionState.REDACTED,
    )
    return record


def validate_session_tool_arguments(descriptor: SessionToolDescriptor, arguments: dict[str, Any]) -> str | None:
    if not isinstance(arguments, dict):
        return f"Tool arguments for {descriptor.id} must be an object."
    schema = descriptor.input_schema or {}
    required = schema.get("required") or []
    if isinstance(required, list):
        for key in required:
            if isinstance(key, str) and key not in arguments:
                return f"Missing required argument for {descriptor.id}: {key}"
    if schema.get("additionalProperties") is False:
        properties = schema.get("properties") or {}
        if isinstance(properties, dict):
            extra = sorted(key for key in arguments if key not in properties)
            if extra:
                return f"Unexpected argument for {descriptor.id}: {extra[0]}"
    properties = schema.get("properties") or {}
    if isinstance(properties, dict):
        for key, value in arguments.items():
            property_schema = properties.get(key)
            if isinstance(property_schema, dict) and not _json_schema_value_matches(value, property_schema):
                return f"Invalid argument type for {descriptor.id}.{key}"
    return None


def _normalize_tool_identity(tool_id: str, arguments: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    normalized_args = dict(arguments)
    if tool_id == "shell":
        cd_path = _simple_shell_cd_path(str(normalized_args.get("command") or ""))
        if cd_path is not None:
            return "cd", {"path": cd_path, "actor": normalized_args.get("actor") or "model"}
    return tool_id, normalized_args


def _tool_call_permission_state(
    decision: SessionToolPermissionDecision,
) -> str:
    if decision.status == SessionToolPermissionDecisionStatus.ASK:
        return "pending"
    if decision.status == SessionToolPermissionDecisionStatus.DENY:
        return "denied"
    if decision.reasons and "existing session permission" in decision.reasons[0]:
        return "approved"
    return "not_required"


def _normalized_record_args(
    project_root: Path,
    store: SQLiteStore,
    session_id: str,
    tool_id: str,
    arguments: dict[str, Any],
    decision: SessionToolPermissionDecision,
    *,
    run_mode: RunMode,
) -> dict[str, Any]:
    target_payload = _approval_target_payload(project_root, session_id, tool_id, decision, run_mode=run_mode)
    normalized = {
        "arguments": sanitize_for_logging(arguments),
        "action": decision.action,
        "target": decision.target,
        "approval_target": target_payload,
    }
    if tool_id == "shell":
        normalized["shell"] = target_payload
    elif tool_id == "cd":
        normalized["cwd"] = decision.target
    else:
        normalized["cwd"] = _record_cwd(project_root, store, session_id, tool_id, decision)
    return normalized


def _record_cwd(
    project_root: Path,
    store: SQLiteStore,
    session_id: str,
    tool_id: str,
    decision: SessionToolPermissionDecision,
) -> str:
    parsed = _json_object_or_none(decision.target)
    if parsed is not None:
        for key in ("normalized_cwd", "cwd"):
            value = parsed.get(key)
            if isinstance(value, str) and value:
                return value
    if tool_id == "cd" and decision.target:
        return decision.target
    try:
        return session_cwd_from_metadata(store.get_session(session_id).metadata)
    except Exception:
        return "."


def _approval_target_payload(
    project_root: Path,
    session_id: str,
    tool_id: str,
    decision: SessionToolPermissionDecision,
    *,
    run_mode: RunMode,
) -> dict[str, Any]:
    target_payload = _json_object_or_none(decision.target) or {}
    normalized_cwd = target_payload.get("normalized_cwd") or target_payload.get("cwd")
    if not isinstance(normalized_cwd, str) or not normalized_cwd:
        normalized_cwd = "."
    project_root_fingerprint = hashlib.sha256(str(project_root.resolve()).encode("utf-8")).hexdigest()
    payload = {
        "schema_version": "harness.tool_approval_target/v1",
        "project_root_fingerprint": project_root_fingerprint,
        "project_fingerprint": target_payload.get("project_fingerprint") or project_root_fingerprint,
        "session_id": session_id,
        "tool_id": tool_id,
        "normalized_cwd": normalized_cwd,
        "normalized_operation": target_payload.get("normalized_operation") or target_payload.get("command") or decision.action,
        "normalized_command": target_payload.get("normalized_command") or target_payload.get("command"),
        "timeout": target_payload.get("timeout") or target_payload.get("timeout_seconds"),
        "timeout_seconds": target_payload.get("timeout_seconds"),
        "shell_executable": target_payload.get("shell_executable"),
        "env_policy": target_payload.get("env_policy") or "not_applicable",
        "network_policy": target_payload.get("network_policy") or "not_applicable",
        "sandbox_profile": target_payload.get("sandbox_profile") or "session_tool_gateway",
        "run_mode": target_payload.get("run_mode") or run_mode.value,
        "action": decision.action,
        "target": decision.target,
        "boundary_kind": decision.boundary_kind.value,
    }
    return sanitize_for_logging(payload)


def _json_object_or_none(value: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _tool_call_status_from_result(result: SessionToolExecutionResult) -> str:
    if result.ok:
        return "completed"
    if result.error_type in {"permission_required", "permission_denied", "path_security", "secret_path", "context_excluded", "invalid_cwd"}:
        return "blocked"
    return "failed"


def _json_schema_value_matches(value: Any, schema: dict[str, Any]) -> bool:
    if "oneOf" in schema and isinstance(schema["oneOf"], list):
        return any(isinstance(candidate, dict) and _json_schema_value_matches(value, candidate) for candidate in schema["oneOf"])
    expected = schema.get("type")
    if isinstance(expected, list):
        return any(_json_type_matches(value, item) for item in expected if isinstance(item, str))
    if isinstance(expected, str):
        return _json_type_matches(value, expected)
    return True


def _json_type_matches(value: Any, expected: str) -> bool:
    if expected == "null":
        return value is None
    if expected == "string":
        return isinstance(value, str)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    return True


def decide_session_tool_permission(
    store: SQLiteStore,
    project_root: Path,
    session_id: str,
    tool_id: str,
    arguments: dict[str, Any],
    *,
    run_mode: RunMode | str = RunMode.READ_ONLY,
) -> SessionToolPermissionDecision:
    run_mode_value = RunMode(run_mode.value if isinstance(run_mode, RunMode) else run_mode)
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
    if tool_id == "shell":
        cd_path = _simple_shell_cd_path(str(arguments.get("command") or ""))
        if cd_path is not None:
            return decide_session_tool_permission(
                store,
                project_root,
                session_id,
                "cd",
                {"path": cd_path, "actor": arguments.get("actor") or "model"},
                run_mode=run_mode_value,
            )
    dynamic_permission_tools = {
        "cd",
        "docker-test",
        "mcp-resource",
        "repo-clone",
        "shell",
        "skill-load",
        "task",
        "web-fetch",
        "web-search",
    }
    if tool_id not in dynamic_permission_tools and _has_allowed_permission(store, session_id, tool_id, action, target, descriptor.boundary_kind):
        return SessionToolPermissionDecision(
            status=SessionToolPermissionDecisionStatus.ALLOW,
            tool_id=tool_id,
            action=action,
            target=target,
            boundary_kind=descriptor.boundary_kind,
            reasons=["Allowed by existing session permission."],
        )
    try:
        if tool_id == "cd":
            context_excluded_target = False
            try:
                target = _validate_cd_target(store, project_root.resolve(), session_id, arguments)
            except CwdResolutionError as exc:
                if exc.error_type != "context_excluded":
                    raise
                target = _validate_cd_target(store, project_root.resolve(), session_id, arguments, allow_excluded=True)
                context_excluded_target = True
            if _has_allowed_permission(store, session_id, tool_id, action, target, descriptor.boundary_kind):
                return SessionToolPermissionDecision(
                    status=SessionToolPermissionDecisionStatus.ALLOW,
                    tool_id=tool_id,
                    action=action,
                    target=target,
                    boundary_kind=descriptor.boundary_kind,
                    reasons=["Allowed by existing session permission."],
                )
            if context_excluded_target:
                return SessionToolPermissionDecision(
                    status=SessionToolPermissionDecisionStatus.ASK,
                    tool_id=tool_id,
                    action=action,
                    target=target,
                    boundary_kind=descriptor.boundary_kind,
                    reasons=[f"Session cwd target is excluded from context: {target}"],
                )
            return SessionToolPermissionDecision(
                status=SessionToolPermissionDecisionStatus.ALLOW,
                tool_id=tool_id,
                action=action,
                target=target,
                boundary_kind=descriptor.boundary_kind,
                reasons=["Allowed as a session-local cwd transition inside the active project."],
            )
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
        if tool_id == "edit":
            target = _validate_edit_target(project_root.resolve(), arguments, load_config(project_root).context_excludes)
            if _tool_mode(arguments) == "plan":
                return SessionToolPermissionDecision(
                    status=SessionToolPermissionDecisionStatus.ALLOW,
                    tool_id=tool_id,
                    action="edit-plan",
                    target=target,
                    boundary_kind=descriptor.boundary_kind,
                    reasons=["Allowed as non-mutating edit plan evidence."],
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
            if _session_planning_active(store, session_id):
                return SessionToolPermissionDecision(
                    status=SessionToolPermissionDecisionStatus.ALLOW,
                    tool_id=tool_id,
                    action="edit-plan",
                    target=target,
                    boundary_kind=descriptor.boundary_kind,
                    reasons=["Planning mode is active, so edit apply is coerced into non-mutating plan evidence."],
                )
            return SessionToolPermissionDecision(
                status=SessionToolPermissionDecisionStatus.ASK,
                tool_id=tool_id,
                action=action,
                target=target,
                boundary_kind=descriptor.boundary_kind,
                reasons=["Edit apply requires explicit active-repo-write permission."],
            )
        if tool_id == "write":
            target = _validate_write_target(project_root.resolve(), arguments, load_config(project_root).context_excludes)
            if _tool_mode(arguments) == "plan":
                return SessionToolPermissionDecision(
                    status=SessionToolPermissionDecisionStatus.ALLOW,
                    tool_id=tool_id,
                    action="write-plan",
                    target=target,
                    boundary_kind=descriptor.boundary_kind,
                    reasons=["Allowed as non-mutating write plan evidence."],
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
            if _session_planning_active(store, session_id):
                return SessionToolPermissionDecision(
                    status=SessionToolPermissionDecisionStatus.ALLOW,
                    tool_id=tool_id,
                    action="write-plan",
                    target=target,
                    boundary_kind=descriptor.boundary_kind,
                    reasons=["Planning mode is active, so write apply is coerced into non-mutating plan evidence."],
                )
            return SessionToolPermissionDecision(
                status=SessionToolPermissionDecisionStatus.ASK,
                tool_id=tool_id,
                action=action,
                target=target,
                boundary_kind=descriptor.boundary_kind,
                reasons=["Write apply requires explicit active-repo-write permission."],
            )
        if tool_id == "docker-test":
            target = _validate_docker_test_plan(project_root.resolve(), store, session_id, arguments)
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
        if tool_id == "shell":
            context_excluded_target = False
            try:
                target = _shell_permission_target(project_root.resolve(), store, session_id, arguments, run_mode=run_mode_value)
            except CwdResolutionError as exc:
                if exc.error_type != "context_excluded":
                    raise
                target = _shell_permission_target(
                    project_root.resolve(),
                    store,
                    session_id,
                    arguments,
                    allow_excluded=True,
                    run_mode=run_mode_value,
                )
                context_excluded_target = True
            if _has_allowed_permission(store, session_id, tool_id, action, target, descriptor.boundary_kind):
                return SessionToolPermissionDecision(
                    status=SessionToolPermissionDecisionStatus.ALLOW,
                    tool_id=tool_id,
                    action=action,
                    target=target,
                    boundary_kind=descriptor.boundary_kind,
                    reasons=["Allowed by existing session permission."],
                )
            if context_excluded_target:
                return SessionToolPermissionDecision(
                    status=SessionToolPermissionDecisionStatus.ASK,
                    tool_id=tool_id,
                    action=action,
                    target=target,
                    boundary_kind=descriptor.boundary_kind,
                    reasons=["Shell cwd is excluded from context and requires exact shell permission."],
                )
            return SessionToolPermissionDecision(
                status=SessionToolPermissionDecisionStatus.ASK,
                tool_id=tool_id,
                action=action,
                target=target,
                boundary_kind=descriptor.boundary_kind,
                reasons=["Shell execution requires an exact normalized session-shell permission grant."],
            )
        if tool_id == "web-fetch":
            target = _validate_web_fetch_plan(project_root.resolve(), arguments)
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
                reasons=["Web fetch planning requires explicit external-network permission even though no network request is made."],
            )
        if tool_id == "web-search":
            target = _validate_web_search_plan(project_root.resolve(), arguments)
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
                reasons=["Web search planning requires explicit external-network permission even though no search request is made."],
            )
        if tool_id == "repo-clone":
            target = _validate_repo_clone_plan(project_root.resolve(), arguments)
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
                reasons=["Repository clone planning requires explicit external-network permission even though no clone or fetch is performed."],
            )
        if tool_id == "mcp-resource":
            target = _validate_mcp_resource_target(project_root.resolve(), arguments)
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
                reasons=["Cached MCP resource reads require explicit permission even though no MCP process or network connection is started."],
            )
        if tool_id == "task":
            plan = _validate_task_plan(store, session_id, arguments)
            target = _task_permission_target(plan)
            if _has_allowed_permission(store, session_id, tool_id, action, target, descriptor.boundary_kind):
                return SessionToolPermissionDecision(
                    status=SessionToolPermissionDecisionStatus.ALLOW,
                    tool_id=tool_id,
                    action=action,
                    target=target,
                    boundary_kind=descriptor.boundary_kind,
                    reasons=["Allowed by existing session permission."],
                )
            if plan["planning_only"]:
                return SessionToolPermissionDecision(
                    status=SessionToolPermissionDecisionStatus.ALLOW,
                    tool_id=tool_id,
                    action="task-plan",
                    target=target,
                    boundary_kind=descriptor.boundary_kind,
                    reasons=["No configured agent profiles are available, so task creation is planning-only."],
                )
            return SessionToolPermissionDecision(
                status=SessionToolPermissionDecisionStatus.ASK,
                tool_id=tool_id,
                action=action,
                target=target,
                boundary_kind=descriptor.boundary_kind,
                reasons=["Delegated task creation requires explicit session permission."],
            )
        if tool_id == "ls":
            _resolve_session_tool_path(
                project_root.resolve(),
                store,
                session_id,
                {"path": str(arguments.get("path") or "."), "cwd": arguments.get("cwd")},
                "path",
                action="list",
            )
        elif tool_id == "read":
            _resolve_session_tool_path(project_root.resolve(), store, session_id, arguments, "path", action=action)
        elif tool_id == "grep" and arguments.get("path"):
            _resolve_session_tool_path(project_root.resolve(), store, session_id, arguments, "path", action=action)
        elif tool_id in {"lsp-definition", "lsp-references"} and arguments.get("path"):
            _resolve_session_tool_path(project_root.resolve(), store, session_id, arguments, "path", action=action)
        elif tool_id == "find":
            _resolve_session_tool_path(
                project_root.resolve(),
                store,
                session_id,
                {"path": str(arguments.get("path") or "."), "cwd": arguments.get("cwd")},
                "path",
                action="find",
            )
        elif tool_id == "git-diff":
            _validate_git_diff_target(project_root.resolve(), store, session_id, arguments)
        elif tool_id == "artifact-read":
            _assert_artifact_linked_to_session(store, session_id, str(arguments.get("artifact_id") or ""))
        elif tool_id == "policy-explain":
            _assert_policy_subject_linked_to_session(
                store,
                session_id,
                str(arguments.get("subject_kind") or "session"),
                str(arguments.get("subject_id")) if arguments.get("subject_id") else None,
            )
        elif tool_id == "skill-load":
            target = _validate_skill_load_target(project_root.resolve(), arguments)
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
                reasons=["Skill loading requires explicit session permission before local instruction text is injected."],
            )
        elif tool_id == "task-status":
            _resolve_task_status_subject(store, session_id, arguments)
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
    except CwdResolutionError as exc:
        status = SessionToolPermissionDecisionStatus.ASK if exc.error_type == "context_excluded" else SessionToolPermissionDecisionStatus.DENY
        return SessionToolPermissionDecision(
            status=status,
            tool_id=tool_id,
            action=exc.action,
            target=exc.target,
            boundary_kind=descriptor.boundary_kind,
            reasons=[exc.message],
        )
    except (PatchValidationError, PathSecurityError, CommandValidationError, ValueError) as exc:
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
        reasons=["Allowed by local/session tool policy."],
    )


def execute_session_tool(
    store: SQLiteStore,
    project_root: Path,
    session_id: str,
    tool_id: str,
    arguments: dict[str, Any],
    *,
    tool_call_id: str | None = None,
    turn_id: str | None = None,
    run_mode: RunMode | str = RunMode.READ_ONLY,
) -> SessionToolExecutionResult:
    if not store.db_path.exists():
        raise ValueError(f"Project is not initialized: {store.project_root}")
    store.initialize()
    requested_tool_id = tool_id
    requested_arguments = dict(arguments)
    tool_id, arguments = _normalize_tool_identity(tool_id, arguments)
    try:
        descriptor = get_session_tool_descriptor(tool_id)
    except KeyError:
        arguments = {
            "requested_tool_id": str(sanitize_for_logging(requested_tool_id)),
            "arguments": sanitize_for_logging(requested_arguments),
            "reason": f"Session tool not found: {requested_tool_id}",
        }
        tool_id = "invalid"
        descriptor = get_session_tool_descriptor(tool_id)
    if (
        descriptor.enabled
        and descriptor.side_effect not in {SessionToolSideEffect.NONE, SessionToolSideEffect.SESSION_LOCAL}
        and tool_id
        not in {"patch", "edit", "write", "direct-write", "docker-test", "shell", "web-fetch", "web-search", "repo-clone", "task"}
    ):
        raise ValueError(f"Session tool is not enabled for session-gateway execution: {tool_id}")
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
    gate = before_tool_call(
        store,
        project_root,
        session_id,
        tool_id,
        arguments,
        tool_call_id=tool_call_id,
        turn_id=turn_id,
        run_id=run.id,
        run_mode=run_mode,
    )
    decision = gate.decision
    descriptor = gate.descriptor
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
                if tool_id == "shell" and "Shell executable" in message:
                    message = (
                        f"{message}\n"
                        "Configure a valid absolute shell path with shell_executable before retrying."
                    )
            else:
                error_type = "permission_required"
            output = message
            ok = False
            permission_id = permission.id
        else:
            matched_permission = _matching_allowed_permission(
                store,
                session_id,
                tool_id,
                decision.action,
                decision.target,
                decision.boundary_kind,
            )
            output = _execute_low_risk_tool(
                store,
                project_root,
                session_id,
                tool_id,
                arguments,
                run_id=run.id,
                allow_excluded=gate.allow_excluded,
                permission_id=getattr(matched_permission, "id", None),
                permission_expires_at=getattr(matched_permission, "expires_at", None),
            )
            ok = tool_id != "invalid"
            error_type = "invalid_tool_call" if tool_id == "invalid" else None
            permission_id = None
            consume_once_permission_after_execution(
                store,
                session_id,
                tool_id,
                decision.action,
                decision.target,
                decision.boundary_kind,
            )
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
    except CwdResolutionError as exc:
        output = cwd_recovery_message(exc)
        ok = False
        error_type = "invalid_cwd"
        permission_id = None
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
    evidence = _tool_policy_evidence(tool_id, descriptor=descriptor)
    part_metadata = {"tool_id": tool_id, "ok": ok, "truncated": truncated, "error_type": error_type}
    part_metadata.update(evidence)
    store.append_session_part(
        session_id,
        message.id,
        SessionPartKind.TOOL_RESULT,
        text=preview,
        artifact_id=artifact_id,
        run_id=run.id,
        metadata=part_metadata,
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
    payload.update(evidence)
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
    finish_status = (
        "completed"
        if ok
        else "policy_violation"
        if error_type in {"path_security", "secret_path", "permission_denied", "network_policy_required", "network_policy_denied"}
        else "failed"
    )
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
    result = SessionToolExecutionResult(
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
    after_tool_call(store, gate, result)
    return result


def _tool_policy_evidence(tool_id: str, *, descriptor: SessionToolDescriptor) -> dict[str, Any]:
    if tool_id in {"ls", "read", "glob", "find", "grep", "git-diff", "repo-overview"}:
        return {
            "policy_boundary": {
                "kind": "project_read_only",
                "boundary_kind": descriptor.boundary_kind.value,
                "source": (
                    "session_tool_read_glob_grep"
                    if tool_id in {"read", "glob", "grep"}
                    else "session_tool_ls_find"
                    if tool_id in {"ls", "find"}
                    else "session_tool_project_read"
                ),
            },
            "project_boundary_enforced": True,
            "context_excludes_enforced": True,
            "secret_path_filtering": True,
            "read_only": True,
            "process_started": False,
            "network_accessed": False,
            "shell_execution_started": False,
            "filesystem_modified": False,
            "active_repo_modified": False,
            "git_mutation_started": False,
            "permission_granting": False,
            "authority_granting": False,
            "blocked_reasons": [],
        }
    if tool_id in {"cd", "pwd", "plan-enter", "plan-exit", "task-status", "invalid"}:
        return {
            "policy_boundary": {
                "kind": "session_local_state",
                "boundary_kind": descriptor.boundary_kind.value,
                "source": "session_tool_session_local",
            },
            "session_local": True,
            "read_only": False,
            "process_started": False,
            "network_accessed": False,
            "shell_execution_started": False,
            "filesystem_modified": False,
            "active_repo_modified": False,
            "git_mutation_started": False,
            "permission_granting": False,
            "authority_granting": False,
            "blocked_reasons": [],
        }
    if tool_id == "task":
        return {
            "policy_boundary": {
                "kind": "delegated_task_record",
                "boundary_kind": descriptor.boundary_kind.value,
                "source": "session_tool_task",
            },
            "session_local": True,
            "read_only": False,
            "process_started": False,
            "network_accessed": False,
            "shell_execution_started": False,
            "filesystem_modified": False,
            "active_repo_modified": False,
            "git_mutation_started": False,
            "permission_granting": False,
            "authority_granting": False,
            "hidden_execution_started": False,
            "blocked_reasons": [],
        }
    return {}


class _DeniedToolCall(Exception):
    def __init__(self, message: str, *, action: str, target: str, error_type: str) -> None:
        super().__init__(message)
        self.message = message
        self.action = action
        self.target = target
        self.error_type = error_type


def _tool_action(tool_id: str) -> str:
    return {
        "cd": "cd",
        "ls": "list",
        "read": "read",
        "glob": "list",
        "find": "find",
        "grep": "search",
        "git-diff": "git-diff",
        "pwd": "pwd",
        "artifact-read": "artifact-read",
        "lsp-diagnostics": "lsp-diagnostics",
        "lsp-symbols": "lsp-symbols",
        "lsp-definition": "lsp-definition",
        "lsp-references": "lsp-references",
        "plan-enter": "plan-enter",
        "plan-exit": "plan-exit",
        "edit": "edit",
        "write": "write",
        "mcp-resource": "mcp-resource",
        "policy-explain": "policy-explain",
        "repo-clone": "repo-clone",
        "repo-overview": "repo-overview",
        "shell": "run",
        "skill-load": "skill-load",
        "task": "task-create",
        "task-status": "task-status",
        "web-fetch": "web-fetch",
        "web-search": "web-search",
        "todo": "todo",
        "question": "question",
    }.get(tool_id, tool_id)


def _tool_target(tool_id: str, arguments: dict[str, Any]) -> str:
    if tool_id == "cd":
        return str(arguments.get("path") or ".")
    if tool_id == "ls":
        return str(arguments.get("path") or arguments.get("cwd") or ".")
    if tool_id == "read":
        return str(arguments.get("path") or "")
    if tool_id == "glob":
        return str(arguments.get("pattern") or "**/*")
    if tool_id == "find":
        return f"{arguments.get('path') or '.'}:{arguments.get('query') or ''}"
    if tool_id == "grep":
        return str(arguments.get("path") or ".")
    if tool_id == "git-diff":
        return str(arguments.get("path") or arguments.get("cwd") or ".")
    if tool_id == "pwd":
        return "."
    if tool_id == "artifact-read":
        return str(arguments.get("artifact_id") or "")
    if tool_id == "lsp-diagnostics":
        return str(arguments.get("path") or ".")
    if tool_id == "lsp-symbols":
        return str(arguments.get("path") or ".")
    if tool_id in {"lsp-definition", "lsp-references"}:
        symbol = str(arguments.get("symbol") or "").strip()
        return f"{arguments.get('path') or '.'}:{symbol or arguments.get('line') or ''}:{arguments.get('character') or ''}"
    if tool_id in {"plan-enter", "plan-exit"}:
        return "planning_mode"
    if tool_id in {"edit", "write"}:
        return str(arguments.get("path") or "")
    if tool_id == "mcp-resource":
        return f"{arguments.get('server') or ''}:{arguments.get('uri') or ''}"
    if tool_id == "policy-explain":
        return f"{arguments.get('subject_kind') or 'session'}:{arguments.get('subject_id') or ''}"
    if tool_id == "repo-clone":
        return str(arguments.get("repository") or arguments.get("url") or "")
    if tool_id == "repo-overview":
        return str(arguments.get("path") or arguments.get("repository") or ".")
    if tool_id == "shell":
        return str(arguments.get("command") or "")
    if tool_id == "skill-load":
        return str(arguments.get("skill") or arguments.get("name") or "")
    if tool_id == "task":
        return f"{arguments.get('agent') or 'auto'}:{arguments.get('objective') or ''}"
    if tool_id == "task-status":
        return str(arguments.get("task_id") or arguments.get("session_id") or "active_task")
    if tool_id == "web-fetch":
        try:
            return _normalize_web_fetch_target(str(arguments.get("url") or ""))
        except ValueError:
            return str(arguments.get("url") or "")
    if tool_id == "web-search":
        return str(arguments.get("query") or "")
    return tool_id


def _has_allowed_permission(
    store: SQLiteStore,
    session_id: str,
    tool_id: str,
    action: str,
    target: str,
    boundary_kind: SessionPermissionBoundaryKind,
) -> bool:
    return _matching_allowed_permission(store, session_id, tool_id, action, target, boundary_kind) is not None


def _matching_allowed_permission(
    store: SQLiteStore,
    session_id: str,
    tool_id: str,
    action: str,
    target: str,
    boundary_kind: SessionPermissionBoundaryKind,
) -> Any | None:
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
        return permission
    return None


def consume_once_permission_after_execution(
    store: SQLiteStore,
    session_id: str,
    tool_id: str,
    action: str,
    target: str,
    boundary_kind: SessionPermissionBoundaryKind,
) -> None:
    permission = _matching_allowed_permission(store, session_id, tool_id, action, target, boundary_kind)
    if permission is not None and permission.scope == SessionPermissionScope.ONCE:
        store.expire_session_permission(
            permission.id,
            reason="One-shot session tool permission consumed.",
        )


def _execute_low_risk_tool(
    store: SQLiteStore,
    project_root: Path,
    session_id: str,
    tool_id: str,
    arguments: dict[str, Any],
    *,
    run_id: str,
    allow_excluded: bool = False,
    permission_id: str | None = None,
    permission_expires_at: datetime | None = None,
) -> str:
    root = project_root.resolve()
    excludes = load_config(root).context_excludes
    session = store.get_session(session_id)
    session_cwd = session_cwd_from_metadata(session.metadata)
    resolver = CwdResolver(project_root=root, context_excludes=excludes)
    if tool_id == "pwd":
        current = resolver.current(session_cwd, allow_excluded=True)
        return "\n".join(
            [
                f"Project root: {root}",
                f"Session cwd: {current.normalized_project_relative_cwd}",
                f"Resolved cwd: {current.resolved_abs_path}",
            ]
        )
    if tool_id == "cd":
        requested = str(arguments.get("path") or ".")
        actor = str(arguments.get("actor") or "model")
        if actor not in {"operator", "model"}:
            actor = "model"
        resolved = resolver.resolve_cd(
            session_cwd=session_cwd,
            requested_path=requested,
            allow_excluded=allow_excluded,
        )
        store.update_session_cwd(
            session_id,
            project_root=str(root),
            old_cwd=session_cwd,
            new_cwd=resolved.normalized_project_relative_cwd,
            requested_path=requested,
            resolved_abs_path=resolved.resolved_abs_path,
            actor=actor,
            tool_call_id=run_id,
            run_id=run_id,
        )
        return "\n".join(
            [
                f"Changed session cwd: {session_cwd} -> {resolved.normalized_project_relative_cwd}",
                f"Resolved cwd: {resolved.resolved_abs_path}",
                "No process was started.",
            ]
        )
    if tool_id == "plan-enter":
        return _plan_enter_tool(store, session_id, arguments, run_id=run_id)
    if tool_id == "plan-exit":
        return _plan_exit_tool(store, session_id, arguments, run_id=run_id)
    if tool_id == "git-diff":
        return _git_diff_tool(store, root, session_id, arguments, run_id=run_id, allow_excluded=allow_excluded)
    if tool_id == "shell":
        return _execute_shell_tool(store, root, session_id, arguments, run_id=run_id, allow_excluded=allow_excluded)
    if tool_id == "patch":
        patch = str(arguments.get("patch") or "")
        summary, updates = plan_unified_diff(patch, root, excludes)
        diff_summary = {
            "files": summary.files,
            "file_count": len(summary.files),
            "added_lines": summary.added_lines,
            "removed_lines": summary.removed_lines,
        }
        governance_applyback = deferred_applyback_evidence(
            changed_files=summary.files,
            diff_summary=diff_summary,
            reason="patch_apply_back_deferred",
        )
        policy_evidence = {
            "policy_boundary": {
                "kind": "patch_apply_back_deferred",
                "boundary_kind": SessionPermissionBoundaryKind.ACTIVE_REPO_WRITE.value,
                "source": "session_tool_patch_plan",
            },
            "approval_required": True,
            "required_approval": "active_repo_write",
            "apply_back_required": True,
            "snapshot_required": True,
            "apply_supported": False,
            "patch_apply_supported": False,
            "applies_to_active_workspace": False,
            "file_written": False,
            "filesystem_modified": False,
            "active_repo_modified": False,
            "git_mutation_started": False,
            "permission_granting": False,
            "authority_granting": False,
            "governance_applyback": governance_applyback,
            "blocked_reasons": [
                "patch_apply_disabled",
                "requires_interactive_permission",
                "requires_snapshot_apply_back",
            ],
        }
        patch_path = store.runs_dir / run_id / "session_tool_patch.diff"
        patch_path.write_text(str(sanitize_for_logging(patch)), encoding="utf-8")
        patch_artifact = store.register_artifact(
            run_id,
            "session_tool_patch",
            patch_path,
            metadata={"tool_id": tool_id, "files": summary.files, **policy_evidence},
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
            "diff_summary": diff_summary,
            "planned_updates": [
                {"relative_path": update.relative_path, "bytes": len(update.content.encode("utf-8"))}
                for update in updates
            ],
            "patch_artifact_id": patch_artifact.id,
            "applied": False,
            **policy_evidence,
        }
        planned_path.write_text(json.dumps(sanitize_for_logging(planned_payload), indent=2, sort_keys=True), encoding="utf-8")
        plan_artifact = store.register_artifact(
            run_id,
            "session_tool_patch_plan",
            planned_path,
            metadata={"tool_id": tool_id, "patch_artifact_id": patch_artifact.id, **policy_evidence},
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
        diff_summary = {
            "files": [relative_path],
            "file_count": 1,
            "added_lines": len(content.splitlines()),
            "removed_lines": 0,
        }
        governance_applyback = deferred_applyback_evidence(
            changed_files=[relative_path],
            diff_summary=diff_summary,
            reason="direct_write_apply_back_deferred",
        )
        policy_evidence = {
            "policy_boundary": {
                "kind": "direct_write_deferred",
                "boundary_kind": SessionPermissionBoundaryKind.ACTIVE_REPO_WRITE.value,
                "source": "session_tool_direct_write_plan",
            },
            "approval_required": True,
            "required_approval": "active_repo_write",
            "blocked_path_checks": True,
            "write_supported": False,
            "direct_write_supported": False,
            "apply_supported": False,
            "file_written": False,
            "filesystem_modified": False,
            "active_repo_modified": False,
            "git_mutation_started": False,
            "permission_granting": False,
            "governance_applyback": governance_applyback,
            "blocked_reasons": [
                "direct_write_apply_disabled",
                "requires_interactive_permission",
                "blocked_path_checks_required",
            ],
        }
        proposal_path = store.runs_dir / run_id / "session_tool_direct_write_content.txt"
        proposal_path.write_text(content, encoding="utf-8")
        content_artifact = store.register_artifact(
            run_id,
            "session_tool_direct_write_content",
            proposal_path,
            metadata={
                "tool_id": tool_id,
                "target": relative_path,
                "applies_to_active_workspace": False,
                **policy_evidence,
            },
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
            "diff_summary": diff_summary,
            "applied": False,
            **policy_evidence,
        }
        plan_path.write_text(json.dumps(sanitize_for_logging(plan_payload), indent=2, sort_keys=True), encoding="utf-8")
        plan_artifact = store.register_artifact(
            run_id,
            "session_tool_direct_write_plan",
            plan_path,
            metadata={
                "tool_id": tool_id,
                "content_artifact_id": content_artifact.id,
                "applies_to_active_workspace": False,
                **policy_evidence,
            },
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
    if tool_id == "edit":
        return _edit_tool(store, root, session_id, arguments, run_id=run_id)
    if tool_id == "write":
        return _write_tool(store, root, session_id, arguments, run_id=run_id)
    if tool_id == "task":
        return _task_tool(store, root, session_id, arguments, run_id=run_id)
    if tool_id == "task-status":
        return _task_status_tool(store, session_id, arguments)
    if tool_id == "docker-test":
        command, cwd, target = _docker_test_plan_values(root, store, session_id, arguments)
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
    if tool_id == "web-fetch":
        plan_payload = _web_fetch_plan_values(root, arguments)
        network_policy, network_decision, network_log_path = _session_tool_network_evidence(
            store,
            root,
            session,
            tool_id,
            plan_payload,
            permission_id=permission_id,
            permission_expires_at=permission_expires_at,
            request_url=str(plan_payload["url"]),
            allow_downloads=True,
        )
        response_payload = _execute_web_fetch(plan_payload)
        content = str(sanitize_for_logging(response_payload["content"]))
        content_path = root / network_policy.download_quarantine_path / "session_tool_web_fetch_content.txt"
        content_path.parent.mkdir(parents=True, exist_ok=True)
        content_path.write_text(content, encoding="utf-8")
        content_artifact = store.register_artifact(
            run_id,
            "session_tool_web_fetch_content",
            content_path,
            metadata={
                "tool_id": tool_id,
                "target": plan_payload["target"],
                "url": plan_payload["url"],
                "status_code": response_payload["status_code"],
                "content_type": response_payload["content_type"],
                "format": plan_payload["format"],
                "network_called": True,
                "fetch_executed": True,
                "network_policy": network_policy.to_sanitized_dict(),
                "network_decision": network_decision,
                "network_request_log_path": str(network_log_path),
                "quarantined": True,
                "approved_for_promotion": False,
            },
            producer="session_tool",
            redaction_state="redacted",
            session_id=session_id,
        )
        quarantine_record = write_download_quarantine_record(
            root,
            network_policy,
            source_url=str(plan_payload["url"]),
            artifact_path=str(content_artifact.path),
            sha256=content_artifact.sha256,
        )
        metadata_payload = {
            **plan_payload,
            "network_called": True,
            "fetch_executed": True,
            "status_code": response_payload["status_code"],
            "content_type": response_payload["content_type"],
            "content_length_header": response_payload["content_length_header"],
            "content_bytes": len(content.encode("utf-8")),
            "content_artifact_id": content_artifact.id,
            "response_headers": response_payload["headers"],
            "network_policy": network_policy.to_sanitized_dict(),
            "network_decision": network_decision,
            "network_request_log_path": str(network_log_path),
            "download_quarantine": quarantine_record,
        }
        metadata_path = store.runs_dir / run_id / "session_tool_web_fetch_metadata.json"
        metadata_path.write_text(json.dumps(sanitize_for_logging(metadata_payload), indent=2, sort_keys=True), encoding="utf-8")
        metadata_artifact = store.register_artifact(
            run_id,
            "session_tool_web_fetch_metadata",
            metadata_path,
            metadata={
                "tool_id": tool_id,
                "target": plan_payload["target"],
                "url": plan_payload["url"],
                "content_artifact_id": content_artifact.id,
                "network_called": True,
                "fetch_executed": True,
                "network_policy_id": network_policy.policy_id,
                "quarantined": True,
            },
            producer="session_tool",
            redaction_state="redacted",
            session_id=session_id,
        )
        preview = content[:4000]
        return (
            "Web fetch executed.\n"
            f"URL: {plan_payload['url']}\n"
            f"Status: {response_payload['status_code']}\n"
            f"Content-Type: {response_payload['content_type'] or 'unknown'}\n"
            f"Format: {plan_payload['format']}\n"
            f"Content artifact: {content_artifact.id}\n"
            f"Metadata artifact: {metadata_artifact.id}\n"
            "\n"
            f"{preview}"
        )
    if tool_id == "web-search":
        plan_payload = _web_search_plan_values(root, arguments)
        network_policy, network_decision, network_log_path = _session_tool_network_evidence(
            store,
            root,
            session,
            tool_id,
            plan_payload,
            permission_id=permission_id,
            permission_expires_at=permission_expires_at,
            request_url=str(plan_payload["endpoint"]),
            allow_downloads=True,
        )
        search_payload = _execute_web_search(plan_payload)
        results_text = str(sanitize_for_logging(search_payload["output"]))
        results_path = root / network_policy.download_quarantine_path / "session_tool_web_search_results.txt"
        results_path.parent.mkdir(parents=True, exist_ok=True)
        results_path.write_text(results_text, encoding="utf-8")
        results_artifact = store.register_artifact(
            run_id,
            "session_tool_web_search_results",
            results_path,
            metadata={
                "tool_id": tool_id,
                "target": plan_payload["target"],
                "query": plan_payload["query"],
                "provider": plan_payload["provider"],
                "status_code": search_payload["status_code"],
                "network_called": True,
                "search_executed": True,
                "network_policy": network_policy.to_sanitized_dict(),
                "network_decision": network_decision,
                "network_request_log_path": str(network_log_path),
                "quarantined": True,
                "approved_for_promotion": False,
            },
            producer="session_tool",
            redaction_state="redacted",
            session_id=session_id,
        )
        quarantine_record = write_download_quarantine_record(
            root,
            network_policy,
            source_url=str(plan_payload["endpoint"]),
            artifact_path=str(results_artifact.path),
            sha256=results_artifact.sha256,
        )
        metadata_payload = {
            **plan_payload,
            "network_called": True,
            "search_executed": True,
            "status_code": search_payload["status_code"],
            "content_type": search_payload["content_type"],
            "content_length_header": search_payload["content_length_header"],
            "response_headers": search_payload["headers"],
            "results_bytes": len(results_text.encode("utf-8")),
            "results_artifact_id": results_artifact.id,
            "network_policy": network_policy.to_sanitized_dict(),
            "network_decision": network_decision,
            "network_request_log_path": str(network_log_path),
            "download_quarantine": quarantine_record,
        }
        metadata_path = store.runs_dir / run_id / "session_tool_web_search_metadata.json"
        metadata_path.write_text(json.dumps(sanitize_for_logging(metadata_payload), indent=2, sort_keys=True), encoding="utf-8")
        metadata_artifact = store.register_artifact(
            run_id,
            "session_tool_web_search_metadata",
            metadata_path,
            metadata={
                "tool_id": tool_id,
                "target": plan_payload["target"],
                "query": plan_payload["query"],
                "results_artifact_id": results_artifact.id,
                "provider": plan_payload["provider"],
                "network_called": True,
                "search_executed": True,
                "network_policy_id": network_policy.policy_id,
                "quarantined": True,
            },
            producer="session_tool",
            redaction_state="redacted",
            session_id=session_id,
        )
        preview = results_text[:4000]
        return (
            "Web search executed.\n"
            f"Query: {plan_payload['query']}\n"
            f"Provider: {plan_payload['provider']}\n"
            f"Status: {search_payload['status_code']}\n"
            f"Result limit: {plan_payload['num_results']}\n"
            f"Results artifact: {results_artifact.id}\n"
            f"Metadata artifact: {metadata_artifact.id}\n"
            "\n"
            f"{preview}"
        )
    if tool_id == "repo-clone":
        plan_payload = _repo_clone_plan_values(root, arguments)
        network_policy, network_decision, network_log_path = _session_tool_network_evidence(
            store,
            root,
            session,
            tool_id,
            plan_payload,
            permission_id=permission_id,
            permission_expires_at=permission_expires_at,
            request_url=str(plan_payload["remote"]),
            allow_downloads=False,
        )
        clone_payload = _execute_repo_clone(plan_payload)
        metadata_payload = {
            **plan_payload,
            **clone_payload,
            "network_called": True,
            "clone_executed": clone_payload["status"] == "cloned",
            "fetch_executed": clone_payload["status"] == "refreshed",
            "external_cache_used": True,
            "network_policy": network_policy.to_sanitized_dict(),
            "network_decision": network_decision,
            "network_request_log_path": str(network_log_path),
            "download_quarantine": None,
        }
        metadata_path = store.runs_dir / run_id / "session_tool_repo_clone_metadata.json"
        metadata_path.write_text(json.dumps(sanitize_for_logging(metadata_payload), indent=2, sort_keys=True), encoding="utf-8")
        metadata_artifact = store.register_artifact(
            run_id,
            "session_tool_repo_clone_metadata",
            metadata_path,
            metadata={
                "tool_id": tool_id,
                "target": plan_payload["target"],
                "repository": plan_payload["repository"],
                "status": clone_payload["status"],
                "local_path": plan_payload["local_path"],
                "network_called": True,
                "external_cache_used": True,
                "network_policy_id": network_policy.policy_id,
            },
            producer="session_tool",
            redaction_state="redacted",
            session_id=session_id,
        )
        return (
            "Repository ready.\n"
            f"Repository: {plan_payload['repository']}\n"
            f"Status: {clone_payload['status']}\n"
            f"Local path: {plan_payload['local_path']}\n"
            f"Branch: {clone_payload['branch'] or 'unknown'}\n"
            f"HEAD: {clone_payload['head'] or 'unknown'}\n"
            f"Metadata artifact: {metadata_artifact.id}"
        )
    if tool_id == "read":
        path = _resolve_session_tool_path(root, store, session_id, arguments, "path", action="read", allow_excluded=allow_excluded)
        if not path.is_file():
            raise ValueError("Path is not a file.")
        raw = path.read_bytes()
        if b"\x00" in raw:
            raise ValueError("File appears to be binary.")
        return raw.decode("utf-8")
    if tool_id == "ls":
        return _ls_tool(root, store, session_id, arguments, allow_excluded=allow_excluded)
    if tool_id == "glob":
        pattern = str(arguments.get("pattern") or "**/*")
        limit = int(arguments.get("limit") or 200)
        cwd = resolver.resolve_cwd(
            session_cwd=session_cwd,
            call_cwd=str(arguments.get("cwd")) if arguments.get("cwd") not in {None, ""} else None,
            allow_excluded=allow_excluded,
            action="list",
        )
        files = _project_files(root, excludes, start=cwd.normalized_project_relative_cwd, allow_excluded=allow_excluded)
        matches = []
        for rel in files:
            try:
                local_rel = Path(rel).relative_to(Path(cwd.normalized_project_relative_cwd)).as_posix()
            except ValueError:
                local_rel = rel
            if Path(local_rel).match(pattern) or re.fullmatch(fnmatch_to_regex(pattern), local_rel):
                matches.append(rel)
        return "\n".join(matches[:limit])
    if tool_id == "find":
        return _find_tool(root, store, session_id, arguments, excludes, allow_excluded=allow_excluded)
    if tool_id == "grep":
        pattern = str(arguments.get("pattern") or "")
        if not pattern:
            raise ValueError("Missing pattern.")
        regex = bool(arguments.get("regex") or False)
        limit = int(arguments.get("limit") or 200)
        base_arg = arguments.get("path")
        base_path = _resolve_session_tool_path(
            root,
            store,
            session_id,
            {"path": str(base_arg) if base_arg not in {None, ""} else ".", "cwd": arguments.get("cwd")},
            "path",
            action="search",
            allow_excluded=allow_excluded,
        )
        files = _project_files(root, excludes, start=relative_to_project(root, base_path), allow_excluded=allow_excluded)
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
    if tool_id == "lsp-diagnostics":
        return _lsp_diagnostics_tool(root, arguments, excludes, allow_excluded=allow_excluded)
    if tool_id == "lsp-symbols":
        return _lsp_symbols_tool(root, arguments, excludes, allow_excluded=allow_excluded)
    if tool_id == "lsp-definition":
        return _lsp_definition_tool(root, arguments, excludes, allow_excluded=allow_excluded)
    if tool_id == "lsp-references":
        return _lsp_references_tool(root, arguments, excludes, allow_excluded=allow_excluded)
    if tool_id == "mcp-resource":
        return _mcp_resource_tool(store, root, session_id, arguments, run_id=run_id)
    if tool_id == "policy-explain":
        subject_kind = str(arguments.get("subject_kind") or "session")
        subject_id = arguments.get("subject_id")
        return _policy_explanation(store, project_root, session_id, subject_kind, str(subject_id) if subject_id else None)
    if tool_id == "repo-overview":
        return _repo_overview(root, store, session_id, arguments, excludes, allow_excluded=allow_excluded)
    if tool_id == "skill-load":
        return _skill_load_tool(store, root, session_id, arguments, run_id=run_id)
    if tool_id == "invalid":
        requested = str(arguments.get("requested_tool_id") or "unknown")
        reason = str(arguments.get("reason") or f"Invalid tool call: {requested}")
        return "\n".join(
            [
                "Invalid tool call.",
                f"Requested tool: {requested}",
                f"Reason: {reason}",
                f"Arguments: {json.dumps(sanitize_for_logging(arguments.get('arguments') or {}), sort_keys=True, default=str)}",
            ]
        )
    raise KeyError(f"Session tool is not executable through the session gateway: {tool_id}")


def _validate_skill_load_target(project_root: Path, arguments: dict[str, Any]) -> str:
    return str(_project_skill_info(project_root, arguments)["name"])


def _load_project_skill(project_root: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    info = _project_skill_info(project_root, arguments)
    skill_file = Path(info["skill_file"])
    content = skill_file.read_text(encoding="utf-8")
    return {**info, "content": sanitize_for_logging(content)}


def _skill_load_tool(
    store: SQLiteStore,
    project_root: Path,
    session_id: str,
    arguments: dict[str, Any],
    *,
    run_id: str,
) -> str:
    skill = _load_project_skill(project_root, arguments)
    content = str(skill["content"])
    content_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
    sections = _markdown_section_headings(content)
    content_path = store.runs_dir / run_id / "session_tool_skill_load_content.md"
    content_path.write_text(content, encoding="utf-8")
    content_artifact = store.register_artifact(
        run_id,
        "session_tool_skill_load_content",
        content_path,
        metadata={
            "tool_id": "skill-load",
            "skill": skill["name"],
            "source_path": skill["path"],
            "skill_file_path": skill["skill_file_path"],
            "origin": skill["origin"],
            "version": skill["version"],
            "content_sha256": content_sha256,
            "loaded_sections": sections,
            "runtime_loaded": False,
            "tool_registered": False,
        },
        producer="session_tool",
        redaction_state="redacted",
        session_id=session_id,
    )
    metadata_payload = {
        "schema_version": "harness.session_tool_skill_load/v1",
        "skill": skill["name"],
        "description": skill["description"],
        "version": skill["version"],
        "origin": skill["origin"],
        "source_kind": "project_config",
        "path": skill["path"],
        "skill_file_path": skill["skill_file_path"],
        "base_dir_uri": skill["base_dir_uri"],
        "content_bytes": len(content.encode("utf-8")),
        "content_sha256": content_sha256,
        "content_artifact_id": content_artifact.id,
        "loaded_sections": sections,
        "allowed_scope": "configured_project_skill_body",
        "runtime_loaded": False,
        "tool_registered": False,
        "plugin_tools_registered": False,
        "network_called": False,
        "filesystem_modified": False,
    }
    metadata_path = store.runs_dir / run_id / "session_tool_skill_load_metadata.json"
    metadata_path.write_text(json.dumps(sanitize_for_logging(metadata_payload), indent=2, sort_keys=True), encoding="utf-8")
    metadata_artifact = store.register_artifact(
        run_id,
        "session_tool_skill_load_metadata",
        metadata_path,
        metadata={
            "tool_id": "skill-load",
            "skill": skill["name"],
            "origin": skill["origin"],
            "version": skill["version"],
            "content_artifact_id": content_artifact.id,
            "runtime_loaded": False,
            "tool_registered": False,
        },
        producer="session_tool",
        redaction_state="redacted",
        session_id=session_id,
    )
    return "\n".join(
        [
            f'<skill_content name="{skill["name"]}">',
            f'# Skill: {skill["name"]}',
            "",
            content.strip(),
            "",
            f'Base directory for this skill: {skill["base_dir_uri"]}',
            "Relative paths in this skill (e.g., scripts/, references/) are relative to this base directory.",
            f"Content artifact: {content_artifact.id}",
            f"Metadata artifact: {metadata_artifact.id}",
            "No plugin tools were registered by this load.",
            "</skill_content>",
        ]
    )


def _project_skill_info(project_root: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    name = str(arguments.get("skill") or arguments.get("name") or "").strip()
    if not name:
        raise _DeniedToolCall("Missing skill name.", action="skill-load", target="", error_type="invalid_input")
    cfg = load_config(project_root)
    skill = cfg.skills.project.get(name)
    if skill is None:
        raise _DeniedToolCall(
            f"Configured project skill not found: {name}",
            action="skill-load",
            target=name,
            error_type="not_found",
        )
    if not cfg.skills.enabled or not skill.enabled:
        raise _DeniedToolCall(
            f"Configured project skill is disabled: {name}",
            action="skill-load",
            target=name,
            error_type="permission_denied",
        )
    if not skill.path:
        raise _DeniedToolCall(
            f"Configured project skill has no path: {name}",
            action="skill-load",
            target=name,
            error_type="not_found",
        )
    path = resolve_under_project(project_root, skill.path)
    skill_file = path / "SKILL.md" if path.is_dir() else path
    if not skill_file.exists() or not skill_file.is_file():
        raise _DeniedToolCall(
            f"Configured project skill file does not exist: {name}",
            action="skill-load",
            target=name,
            error_type="not_found",
        )
    if is_secret_path(skill_file):
        raise _DeniedToolCall(
            f"Configured project skill path is secret-like: {name}",
            action="skill-load",
            target=name,
            error_type="secret_path",
        )
    return {
        "name": name,
        "description": skill.description,
        "version": skill.version,
        "origin": "project_config",
        "path": relative_to_project(project_root, path),
        "skill_file": str(skill_file),
        "skill_file_path": relative_to_project(project_root, skill_file),
        "base_dir_uri": path.resolve().as_uri() if path.is_dir() else skill_file.parent.resolve().as_uri(),
    }


def _markdown_section_headings(content: str) -> list[str]:
    sections: list[str] = []
    for line in content.splitlines():
        match = re.match(r"^\s{0,3}#{1,6}\s+(.+?)\s*$", line)
        if match:
            sections.append(match.group(1).strip())
    return sections


def _validate_mcp_resource_target(project_root: Path, arguments: dict[str, Any]) -> str:
    info = _mcp_resource_info(project_root, arguments)
    return f"{info['server']}:{info['uri']}"


def _mcp_resource_tool(
    store: SQLiteStore,
    project_root: Path,
    session_id: str,
    arguments: dict[str, Any],
    *,
    run_id: str,
) -> str:
    info = _mcp_resource_info(project_root, arguments)
    path = Path(info["path"])
    raw = path.read_bytes()
    if b"\x00" in raw:
        raise ValueError("Cached MCP resource appears to be binary.")
    content = str(sanitize_for_logging(raw.decode("utf-8", errors="replace")))
    content_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
    content_path = store.runs_dir / run_id / "session_tool_mcp_resource_content.txt"
    content_path.write_text(content, encoding="utf-8")
    content_artifact = store.register_artifact(
        run_id,
        "session_tool_mcp_resource_content",
        content_path,
        metadata={
            "tool_id": "mcp-resource",
            "server": info["server"],
            "uri": info["uri"],
            "source_path": info["relative_path"],
            "content_type": info["content_type"],
            "server_kind": info["server_kind"],
            "origin": info["origin"],
            "content_sha256": content_sha256,
            "process_started": False,
            "network_called": False,
        },
        producer="session_tool",
        redaction_state="redacted",
        session_id=session_id,
    )
    metadata_payload = {
        "schema_version": "harness.session_tool_mcp_resource/v1",
        "server": info["server"],
        "uri": info["uri"],
        "resource_name": info["resource_name"],
        "path": info["relative_path"],
        "content_type": info["content_type"],
        "description": info["description"],
        "origin": info["origin"],
        "server_kind": info["server_kind"],
        "server_command_configured": info["server_command_configured"],
        "server_url_configured": info["server_url_configured"],
        "content_bytes": len(content.encode("utf-8")),
        "content_sha256": content_sha256,
        "content_artifact_id": content_artifact.id,
        "cached_only": True,
        "allowed_scope": "configured_cached_resource",
        "connected": False,
        "process_started": False,
        "network_called": False,
        "resource_read": True,
    }
    metadata_path = store.runs_dir / run_id / "session_tool_mcp_resource_metadata.json"
    metadata_path.write_text(json.dumps(sanitize_for_logging(metadata_payload), indent=2, sort_keys=True), encoding="utf-8")
    metadata_artifact = store.register_artifact(
        run_id,
        "session_tool_mcp_resource_metadata",
        metadata_path,
        metadata={
            "tool_id": "mcp-resource",
            "server": info["server"],
            "uri": info["uri"],
            "content_artifact_id": content_artifact.id,
            "origin": info["origin"],
            "server_kind": info["server_kind"],
            "process_started": False,
            "network_called": False,
        },
        producer="session_tool",
        redaction_state="redacted",
        session_id=session_id,
    )
    return (
        "MCP resource read from cache.\n"
        f"Server: {info['server']}\n"
        f"URI: {info['uri']}\n"
        f"Content artifact: {content_artifact.id}\n"
        f"Metadata artifact: {metadata_artifact.id}\n"
        "\n"
        f"{content[:4000]}"
    )


def _mcp_resource_info(project_root: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    server_name = str(arguments.get("server") or "").strip()
    requested_uri = str(arguments.get("uri") or "").strip()
    if not server_name:
        raise ValueError("Missing MCP server name.")
    if not requested_uri:
        raise ValueError("Missing MCP resource URI.")
    cfg = load_config(project_root)
    server = cfg.mcp.servers.get(server_name)
    if server is None:
        raise ValueError(f"Configured MCP server not found: {server_name}")
    if not cfg.mcp.enabled or not server.enabled:
        raise ValueError(f"Configured MCP server is disabled: {server_name}")
    matched_name = None
    resource = None
    for name, candidate in server.resources.items():
        if name == requested_uri or candidate.uri == requested_uri:
            matched_name = name
            resource = candidate
            break
    if resource is None:
        raise ValueError(f"Cached MCP resource not found: {server_name}:{requested_uri}")
    if not resource.enabled:
        raise ValueError(f"Cached MCP resource is disabled: {server_name}:{requested_uri}")
    path = resolve_under_project(project_root, resource.path)
    if not path.exists() or not path.is_file():
        raise ValueError(f"Cached MCP resource file does not exist: {server_name}:{requested_uri}")
    if is_secret_path(path):
        raise _DeniedToolCall(
            f"Cached MCP resource path is secret-like: {relative_to_project(project_root, path)}",
            action="mcp-resource",
            target=f"{server_name}:{resource.uri}",
            error_type="secret_path",
        )
    return {
        "server": server_name,
        "resource_name": matched_name,
        "uri": resource.uri,
        "path": str(path),
        "relative_path": relative_to_project(project_root, path),
        "content_type": resource.content_type or "text/plain",
        "description": resource.description,
        "origin": "project_config_cached_resource",
        "server_kind": server.kind,
        "server_command_configured": bool(server.command),
        "server_url_configured": bool(server.url),
    }


def _repo_overview(
    root: Path,
    store: SQLiteStore,
    session_id: str,
    arguments: dict[str, Any],
    excludes: list[str],
    *,
    allow_excluded: bool = False,
) -> str:
    if arguments.get("repository"):
        clone_plan = _repo_clone_plan_values(root, {"repository": arguments.get("repository")})
        target = Path(str(clone_plan["local_path"]))
        if not target.exists() or not target.is_dir():
            raise _DeniedToolCall(
                f"Repository is not in the managed cache: {clone_plan['repository']}. Run repo-clone first.",
                action="repo-overview",
                target=str(clone_plan["repository"]),
                error_type="not_found",
            )
        if not _is_managed_repo_cache_path(root, target):
            raise _DeniedToolCall(
                "Repository overview is limited to the managed external repository cache.",
                action="repo-overview",
                target=str(clone_plan["repository"]),
                error_type="path_security",
            )
        rel_target = str(target)
        repository_label = str(clone_plan["repository"])
        external_cache_used = True
    else:
        path_arg = str(arguments.get("path") or ".")
        target = _resolve_session_tool_path(
            root,
            store,
            session_id,
            {"path": path_arg, "cwd": arguments.get("cwd")},
            "path",
            action="repo-overview",
            allow_excluded=allow_excluded,
        )
        rel_target = relative_to_project(root, target)
        repository_label = None
        external_cache_used = False
    if not target.is_dir():
        raise ValueError("Repository overview path must be a directory.")
    depth = _bounded_int(arguments.get("depth", 3), "Repository overview depth", minimum=1, maximum=6)
    top_level = {entry.name for entry in target.iterdir()}
    dependency_files = [name for name in REPO_OVERVIEW_DEPENDENCY_FILES if name in top_level]
    package_json = _read_package_json(target / "package.json") if "package.json" in top_level else {}
    entrypoints = _repo_entrypoints(target, top_level, package_json)
    structure_lines, truncated = _repo_structure(target, depth)
    metadata = {
        "schema_version": "harness.session_tool_repo_overview/v1",
        "path": rel_target,
        "repository": repository_label,
        "branch": _git_output(target, ["branch", "--show-current"]),
        "head": _git_output(target, ["rev-parse", "HEAD"]),
        "package_manager": _repo_package_manager(top_level),
        "ecosystems": _repo_ecosystems(top_level),
        "dependency_files": dependency_files,
        "entrypoints": entrypoints,
        "depth": depth,
        "truncated": truncated,
        "external_cache_used": external_cache_used,
        "network_called": False,
    }
    return "\n".join(
        [
            f"Path: {rel_target}",
            *([f"Repository: {repository_label}"] if repository_label else []),
            *([f"Branch: {metadata['branch']}"] if metadata["branch"] else []),
            *([f"Head: {metadata['head']}"] if metadata["head"] else []),
            f"Ecosystems: {', '.join(metadata['ecosystems']) if metadata['ecosystems'] else 'unknown'}",
            f"Package manager: {metadata['package_manager'] or 'unknown'}",
            f"Dependency files: {', '.join(dependency_files) if dependency_files else 'none'}",
            f"Entrypoints: {', '.join(entrypoints) if entrypoints else 'none detected'}",
            f"Depth: {depth}",
            f"Truncated: {str(truncated).lower()}",
            "",
            "Structure:",
            *(structure_lines or ["[empty]"]),
            "",
            "Metadata:",
            json.dumps(sanitize_for_logging(metadata), indent=2, sort_keys=True, default=str),
        ]
    )


def _is_managed_repo_cache_path(root: Path, target: Path) -> bool:
    cache_root = (root / ".harness" / "external_repositories").resolve()
    try:
        target.resolve().relative_to(cache_root)
    except ValueError:
        return False
    return True


def _lsp_diagnostics_tool(root: Path, arguments: dict[str, Any], excludes: list[str], *, allow_excluded: bool = False) -> str:
    path_arg = arguments.get("path")
    target_path: Path | None = None
    target_rel: str | None = None
    if path_arg not in {None, ""}:
        target_path = _resolve_allowed_path(root, str(path_arg), excludes, action="lsp-diagnostics", allow_excluded=allow_excluded)
        target_rel = relative_to_project(root, target_path)
    cfg = load_config(root)
    suffix = target_path.suffix if target_path is not None and target_path.is_file() else None
    servers: list[dict[str, Any]] = []
    for name, server in sorted(cfg.lsp.servers.items()):
        extensions = list(server.file_extensions)
        matches_path = bool(suffix and suffix in extensions) if extensions else False
        servers.append(
            {
                "name": name,
                "enabled": bool(cfg.lsp.enabled and server.enabled),
                "configured": bool(server.command),
                "file_extensions": extensions,
                "command_configured": bool(server.command),
                "matches_path": matches_path,
                "process_started": False,
                "diagnostics": [],
            }
        )
    matching_servers = [server["name"] for server in servers if server["matches_path"]]
    payload = {
        "schema_version": "harness.session_tool_lsp_diagnostics/v1",
        "enabled": bool(cfg.lsp.enabled),
        "path": target_rel,
        "matching_servers": matching_servers,
        "servers": servers,
        "diagnostics": [],
        "lsp_backed": False,
        "process_started": False,
        "contents_included": False,
        "permission_granting": False,
        "notes": [
            "This is a configured LSP diagnostics projection only; no language-server process was started.",
            "OpenCode-style process-backed LSP operations remain deferred behind Harness permission and process policy.",
        ],
    }
    lines = [
        f"LSP enabled: {str(payload['enabled']).lower()}",
        f"Path: {target_rel or '.'}",
        f"Matching servers: {', '.join(matching_servers) if matching_servers else 'none'}",
        "Process started: false",
        "Diagnostics: none",
        "",
        "Servers:",
    ]
    if servers:
        for server in servers:
            lines.append(
                "- "
                f"{server['name']} enabled={str(server['enabled']).lower()} "
                f"configured={str(server['configured']).lower()} "
                f"extensions={','.join(server['file_extensions']) or 'none'} "
                f"matches_path={str(server['matches_path']).lower()}"
            )
    else:
        lines.append("- none")
    lines.extend(["", "Metadata:", json.dumps(sanitize_for_logging(payload), indent=2, sort_keys=True, default=str)])
    return "\n".join(lines)


def _lsp_symbols_tool(root: Path, arguments: dict[str, Any], excludes: list[str], *, allow_excluded: bool = False) -> str:
    path_arg = str(arguments.get("path") or ".")
    target = _resolve_allowed_path(root, path_arg, excludes, action="lsp-symbols", allow_excluded=allow_excluded)
    query = str(arguments.get("query") or "").strip().lower()
    limit = _bounded_int(arguments.get("limit", 500), "LSP symbol limit", minimum=1, maximum=1000)
    symbols: list[dict[str, Any]] = []
    paths = [target] if target.is_file() else sorted(target.rglob("*"))
    for path in paths:
        if not path.is_file() or path.suffix not in STATIC_SYMBOL_SUFFIXES:
            continue
        rel = relative_to_project(root, path)
        if is_excluded_relative(rel, excludes) and not allow_excluded:
            continue
        if is_secret_path(path):
            continue
        symbols.extend(_static_symbols_for_file(root, path, search=query))
        if len(symbols) >= limit:
            symbols = symbols[:limit]
            break
    payload = {
        "schema_version": "harness.session_tool_lsp_symbols/v1",
        "symbols": symbols,
        "source": "static_scan",
        "query": query,
        "path": relative_to_project(root, target),
        "limit": limit,
        "lsp_backed": False,
        "process_started": False,
        "contents_included": False,
        "permission_granting": False,
        "notes": [
            "This is a static symbol projection only; no language-server process was started.",
            "OpenCode-style process-backed workspaceSymbol/documentSymbol remain deferred behind Harness permission and process policy.",
        ],
    }
    lines = [
        f"Source: {payload['source']}",
        f"Path: {payload['path']}",
        f"Query: {query or '*'}",
        f"Symbols: {len(symbols)}",
        "Process started: false",
        "",
    ]
    if symbols:
        lines.append("Results:")
        for symbol in symbols:
            lines.append(
                f"- {symbol['kind']} {symbol['name']} {symbol['path']}:{symbol['line']}:{symbol['column']}"
            )
    else:
        lines.append("Results: none")
    lines.extend(["", "Metadata:", json.dumps(sanitize_for_logging(payload), indent=2, sort_keys=True, default=str)])
    return "\n".join(lines)


def _lsp_definition_tool(root: Path, arguments: dict[str, Any], excludes: list[str], *, allow_excluded: bool = False) -> str:
    symbol = _lsp_lookup_symbol(root, arguments, excludes, action="lsp-definition", allow_excluded=allow_excluded)
    limit = _bounded_int(arguments.get("limit", 20), "LSP definition limit", minimum=1, maximum=100)
    definitions = _static_symbol_definitions(root, arguments, excludes, symbol=symbol, limit=limit, allow_excluded=allow_excluded)
    payload = {
        "schema_version": "harness.session_tool_lsp_definition/v1",
        "symbol": symbol,
        "definitions": definitions,
        "definition_count": len(definitions),
        "source": "static_scan",
        "lsp_backed": False,
        "process_started": False,
        "contents_included": False,
        "truncated": len(definitions) >= limit,
        "permission_granting": False,
        "notes": [
            "This is a static definition projection only; no language-server process was started.",
            "OpenCode-style process-backed textDocument/definition remains deferred behind Harness permission and process policy.",
        ],
    }
    lines = [
        "Source: static_scan",
        f"Symbol: {symbol}",
        f"Definitions: {len(definitions)}",
        "Process started: false",
        "",
    ]
    if definitions:
        lines.append("Results:")
        for definition in definitions:
            lines.append(
                f"- {definition['kind']} {definition['name']} {definition['path']}:{definition['range']['start']['line']}:{definition['range']['start']['character']}"
            )
    else:
        lines.append("Results: none")
    lines.extend(["", "Metadata:", json.dumps(sanitize_for_logging(payload), indent=2, sort_keys=True, default=str)])
    return "\n".join(lines)


def _lsp_references_tool(root: Path, arguments: dict[str, Any], excludes: list[str], *, allow_excluded: bool = False) -> str:
    symbol = _lsp_lookup_symbol(root, arguments, excludes, action="lsp-references", allow_excluded=allow_excluded)
    limit = _bounded_int(arguments.get("limit", 200), "LSP references limit", minimum=1, maximum=1000)
    references = _static_symbol_references(root, arguments, excludes, symbol=symbol, limit=limit, allow_excluded=allow_excluded)
    payload = {
        "schema_version": "harness.session_tool_lsp_references/v1",
        "symbol": symbol,
        "references": references,
        "reference_count": len(references),
        "source": "static_scan",
        "lsp_backed": False,
        "process_started": False,
        "contents_included": False,
        "truncated": len(references) >= limit,
        "permission_granting": False,
        "notes": [
            "This is a static identifier-reference projection only; no language-server process was started.",
            "OpenCode-style process-backed textDocument/references remains deferred behind Harness permission and process policy.",
        ],
    }
    lines = [
        "Source: static_scan",
        f"Symbol: {symbol}",
        f"References: {len(references)}",
        "Process started: false",
        "",
    ]
    if references:
        lines.append("Results:")
        for reference in references:
            lines.append(
                f"- {reference['path']}:{reference['range']['start']['line']}:{reference['range']['start']['character']}"
            )
    else:
        lines.append("Results: none")
    lines.extend(["", "Metadata:", json.dumps(sanitize_for_logging(payload), indent=2, sort_keys=True, default=str)])
    return "\n".join(lines)


def _lsp_lookup_symbol(root: Path, arguments: dict[str, Any], excludes: list[str], *, action: str, allow_excluded: bool = False) -> str:
    symbol = str(arguments.get("symbol") or "").strip()
    if symbol:
        return symbol
    path_arg = str(arguments.get("path") or "").strip()
    if not path_arg:
        raise ValueError("Missing symbol or path/line/character lookup target.")
    if arguments.get("line") is None or arguments.get("character") is None:
        raise ValueError("Line and character are required when symbol is omitted.")
    target = _resolve_allowed_path(root, path_arg, excludes, action=action, allow_excluded=allow_excluded)
    if not target.is_file():
        raise ValueError("Path must be a file when resolving symbol from line/character.")
    return _identifier_at_position(target, _bounded_int(arguments.get("line"), "LSP line", minimum=1, maximum=10_000_000), _bounded_int(arguments.get("character"), "LSP character", minimum=0, maximum=10_000_000))


def _identifier_at_position(path: Path, line_number: int, character: int) -> str:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise ValueError("Lookup target is not valid UTF-8 text.") from exc
    if line_number > len(lines):
        raise ValueError("Lookup line is outside the target file.")
    line = lines[line_number - 1]
    index = max(0, min(character, max(0, len(line) - 1)))
    matches = list(re.finditer(r"[A-Za-z_$][A-Za-z0-9_$]*", line))
    for match in matches:
        if match.start() <= index < match.end():
            return match.group(0)
    raise ValueError("No identifier found at lookup position.")


def _static_symbol_search_roots(root: Path, arguments: dict[str, Any], excludes: list[str], *, action: str, allow_excluded: bool = False) -> list[Path]:
    path_arg = str(arguments.get("path") or "").strip()
    if not path_arg:
        return [root]
    target = _resolve_allowed_path(root, path_arg, excludes, action=action, allow_excluded=allow_excluded)
    if target.is_file():
        return [root]
    return [target]


def _static_symbol_files(root: Path, search_roots: list[Path], excludes: list[str], *, allow_excluded: bool = False) -> list[Path]:
    files: list[Path] = []
    for search_root in search_roots:
        paths = [search_root] if search_root.is_file() else sorted(search_root.rglob("*"))
        for path in paths:
            if not path.is_file() or path.suffix not in STATIC_SYMBOL_SUFFIXES:
                continue
            rel = relative_to_project(root, path)
            if is_excluded_relative(rel, excludes) and not allow_excluded:
                continue
            if is_secret_path(path):
                continue
            files.append(path)
    return files


def _static_symbol_definitions(
    root: Path,
    arguments: dict[str, Any],
    excludes: list[str],
    *,
    symbol: str,
    limit: int,
    allow_excluded: bool = False,
) -> list[dict[str, Any]]:
    definitions: list[dict[str, Any]] = []
    for path in _static_symbol_files(root, _static_symbol_search_roots(root, arguments, excludes, action="lsp-definition", allow_excluded=allow_excluded), excludes, allow_excluded=allow_excluded):
        for item in _static_symbols_for_file(root, path, search=""):
            if item["name"] != symbol:
                continue
            definitions.append(_lsp_location_payload(item, source="static_scan"))
            if len(definitions) >= limit:
                return definitions
    return definitions


def _static_symbol_references(
    root: Path,
    arguments: dict[str, Any],
    excludes: list[str],
    *,
    symbol: str,
    limit: int,
    allow_excluded: bool = False,
) -> list[dict[str, Any]]:
    references: list[dict[str, Any]] = []
    pattern = re.compile(rf"\b{re.escape(symbol)}\b")
    for path in _static_symbol_files(root, _static_symbol_search_roots(root, arguments, excludes, action="lsp-references", allow_excluded=allow_excluded), excludes, allow_excluded=allow_excluded):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        rel = relative_to_project(root, path)
        for line_number, line in enumerate(lines, start=1):
            for match in pattern.finditer(line):
                start_character = match.start() + 1
                references.append(
                    {
                        "symbol": symbol,
                        "path": rel,
                        "range": {
                            "start": {"line": line_number, "character": start_character},
                            "end": {"line": line_number, "character": start_character + len(symbol)},
                        },
                        "source": "static_scan",
                        "contents_included": False,
                    }
                )
                if len(references) >= limit:
                    return references
    return references


def _lsp_location_payload(symbol_item: dict[str, Any], *, source: str) -> dict[str, Any]:
    start_character = int(symbol_item["column"])
    end_character = start_character + len(str(symbol_item["name"]))
    return {
        "name": symbol_item["name"],
        "symbol": symbol_item["name"],
        "kind": symbol_item["kind"],
        "path": symbol_item["path"],
        "range": {
            "start": {"line": symbol_item["line"], "character": start_character},
            "end": {"line": symbol_item["line"], "character": end_character},
        },
        "source": source,
        "contents_included": False,
    }


def _static_symbols_for_file(project_root: Path, path: Path, *, search: str) -> list[dict[str, Any]]:
    patterns = {
        ".py": [
            ("class", re.compile(r"^\s*class\s+([A-Za-z_][A-Za-z0-9_]*)\b")),
            ("function", re.compile(r"^\s*(?:async\s+)?def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(")),
        ],
        ".js": [
            ("class", re.compile(r"^\s*class\s+([A-Za-z_$][A-Za-z0-9_$]*)\b")),
            ("function", re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*\(")),
            ("function", re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*(?:async\s*)?\(")),
        ],
        ".jsx": [],
        ".ts": [],
        ".tsx": [],
    }
    patterns[".jsx"] = patterns[".js"]
    patterns[".ts"] = patterns[".js"]
    patterns[".tsx"] = patterns[".js"]
    rel = relative_to_project(project_root, path)
    symbols: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError:
        return []
    for line_number, line in enumerate(lines, start=1):
        for kind, pattern in patterns.get(path.suffix, []):
            match = pattern.match(line)
            if not match:
                continue
            name = match.group(1)
            if search and search not in name.lower():
                continue
            symbols.append(
                {
                    "name": name,
                    "kind": kind,
                    "path": rel,
                    "line": line_number,
                    "column": max(1, line.find(name) + 1),
                    "contents_included": False,
                }
            )
    return symbols


def _validate_repo_clone_plan(root: Path, arguments: dict[str, Any]) -> str:
    return str(_repo_clone_plan_values(root, arguments)["target"])


def _repo_clone_plan_values(root: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    reference = _parse_repo_reference(str(arguments.get("repository") or arguments.get("url") or "").strip())
    branch = str(arguments.get("branch") or "").strip() or None
    if branch is not None:
        _validate_repo_branch(branch)
    refresh = bool(arguments.get("refresh") or False)
    cache_key = _repo_cache_key(reference["host"], reference["owner"], reference["name"])
    return {
        "schema_version": "harness.session_tool_repo_clone_plan/v1",
        "repository": reference["label"],
        "target": reference["label"],
        "host": reference["host"],
        "remote": reference["remote"],
        "origin": "external_git_repository",
        "cache_key": cache_key,
        "managed_cache_root": str(root / ".harness" / "external_repositories"),
        "local_path": str(root / ".harness" / "external_repositories" / cache_key),
        "refresh": refresh,
        "branch": branch,
        "requires_network": True,
        "network_called": False,
        "clone_executed": False,
        "fetch_executed": False,
        "external_cache_used": False,
        "permission_boundary": {
            "kind": "managed_external_repository_cache",
            "boundary_kind": SessionPermissionBoundaryKind.EXTERNAL_NETWORK.value,
            "origin": "external_git_repository",
            "remote": reference["remote"],
            "cache_key": cache_key,
            "managed_cache_root": str(root / ".harness" / "external_repositories"),
            "approval_required": True,
            "active_workspace_write": False,
        },
        "notes": [
            "Clone execution must use the managed external repository cache and persist remote, branch, head, cache path, redaction state, and artifact metadata before display.",
        ],
    }


def _execute_repo_clone(plan: dict[str, Any]) -> dict[str, Any]:
    local_path = Path(str(plan["local_path"]))
    cache_root = Path(str(plan["managed_cache_root"]))
    cache_root.mkdir(parents=True, exist_ok=True)
    status = "cached"
    if local_path.exists():
        if not (local_path / ".git").exists():
            raise ValueError(f"Managed cache path exists but is not a git repository: {local_path}")
        if plan.get("refresh"):
            _run_git(["fetch", "--prune", "origin"], cwd=local_path, timeout=120)
            status = "refreshed"
    else:
        _run_git(["clone", "--", str(plan["remote"]), str(local_path)], cwd=cache_root, timeout=180)
        status = "cloned"
    if plan.get("branch"):
        _run_git(["checkout", "--", str(plan["branch"])], cwd=local_path, timeout=60)
    branch = _git_output(local_path, ["branch", "--show-current"])
    head = _git_output(local_path, ["rev-parse", "HEAD"])
    return {"status": status, "branch": branch, "head": head}


def _run_git(args: list[str], *, cwd: Path, timeout: int) -> None:
    result = subprocess.run(["git", *args], cwd=cwd, check=False, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        message = (result.stderr or result.stdout or "git command failed").strip()
        raise ValueError(str(sanitize_for_logging(message)))


def _parse_repo_reference(raw: str) -> dict[str, str]:
    if not raw:
        raise ValueError("Missing repository reference.")
    if any(char.isspace() for char in raw):
        raise ValueError("Repository reference must not contain whitespace.")
    if raw.startswith("file://"):
        parsed = urlparse(raw)
        path = Path(urllib.request.url2pathname(parsed.path))
        if not path.name:
            raise ValueError("Repository file URL must include a repository path.")
        name = path.name[:-4] if path.name.endswith(".git") else path.name
        _validate_repo_slug_part(name, "Repository name")
        return {"host": "local", "owner": "file", "name": name, "label": f"local/file/{name}", "remote": raw}
    if raw.startswith(("http://", "https://", "git://", "ssh://")):
        parsed = urlparse(raw)
        if parsed.scheme not in {"https", "http", "git", "ssh"}:
            raise ValueError("Unsupported repository URL scheme.")
        if parsed.username or parsed.password:
            raise ValueError("Repository URL must not include credentials.")
        host = (parsed.hostname or "").lower()
        parts = [part for part in parsed.path.strip("/").split("/") if part]
        if len(parts) < 2 or not host:
            raise ValueError("Repository URL must include host, owner, and repository name.")
        owner, name = parts[-2], parts[-1]
        name = name[:-4] if name.endswith(".git") else name
        _validate_repo_slug_part(owner, "Repository owner")
        _validate_repo_slug_part(name, "Repository name")
        return {"host": host, "owner": owner, "name": name, "label": f"{host}/{owner}/{name}", "remote": raw}
    parts = raw.split("/")
    if len(parts) == 2:
        host = "github.com"
        owner, name = parts
    elif len(parts) == 3:
        host, owner, name = parts
    else:
        raise ValueError("Repository must be a git URL, host/path reference, or GitHub owner/repo shorthand.")
    name = name[:-4] if name.endswith(".git") else name
    _validate_repo_slug_part(host, "Repository host")
    _validate_repo_slug_part(owner, "Repository owner")
    _validate_repo_slug_part(name, "Repository name")
    return {
        "host": host.lower(),
        "owner": owner,
        "name": name,
        "label": f"{host.lower()}/{owner}/{name}",
        "remote": f"https://{host.lower()}/{owner}/{name}.git",
    }


def _validate_repo_slug_part(value: str, label: str) -> None:
    if not value or not re.fullmatch(r"[A-Za-z0-9._-]+", value):
        raise ValueError(f"{label} contains unsupported characters.")
    if value in {".", ".."}:
        raise ValueError(f"{label} is not valid.")


def _validate_repo_branch(branch: str) -> None:
    if len(branch) > 200:
        raise ValueError("Repository branch/ref is too long.")
    if branch.startswith(("-", "/", ".")) or branch.endswith(("/", ".", ".lock")):
        raise ValueError("Repository branch/ref is not valid.")
    if ".." in branch or "@{" in branch or "\\" in branch or any(char.isspace() for char in branch):
        raise ValueError("Repository branch/ref contains unsupported characters.")
    if not re.fullmatch(r"[A-Za-z0-9._/-]+", branch):
        raise ValueError("Repository branch/ref contains unsupported characters.")


def _repo_cache_key(host: str, owner: str, name: str) -> str:
    return "__".join(re.sub(r"[^A-Za-z0-9._-]+", "_", part).strip("._-") for part in (host, owner, name))


def _repo_structure(root: Path, depth: int) -> tuple[list[str], bool]:
    lines: list[str] = []
    truncated = False

    def visit(directory: Path, level: int) -> None:
        nonlocal truncated
        if level >= depth:
            return
        entries: list[tuple[str, Path, bool]] = []
        for entry in directory.iterdir():
            if entry.name in REPO_OVERVIEW_IGNORED_DIRS:
                continue
            entries.append((entry.name, entry, entry.is_dir()))
        entries.sort(key=lambda item: (not item[2], item[0].lower()))
        for name, path, is_dir in entries:
            if len(lines) >= REPO_OVERVIEW_STRUCTURE_LIMIT:
                truncated = True
                return
            lines.append(f"{'  ' * level}{name}{'/' if is_dir else ''}")
            if is_dir:
                visit(path, level + 1)

    visit(root, 0)
    return lines, truncated


def _read_package_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _repo_package_manager(files: set[str]) -> str | None:
    if "bun.lock" in files or "bun.lockb" in files:
        return "bun"
    if "pnpm-lock.yaml" in files:
        return "pnpm"
    if "yarn.lock" in files:
        return "yarn"
    if "package-lock.json" in files:
        return "npm"
    return None


def _repo_ecosystems(files: set[str]) -> list[str]:
    ecosystems: list[str] = []
    if "package.json" in files:
        ecosystems.append("Node.js")
    if "pyproject.toml" in files or "requirements.txt" in files:
        ecosystems.append("Python")
    if "go.mod" in files:
        ecosystems.append("Go")
    if "Cargo.toml" in files:
        ecosystems.append("Rust")
    if "Gemfile" in files:
        ecosystems.append("Ruby")
    if "build.gradle" in files or "build.gradle.kts" in files or "pom.xml" in files:
        ecosystems.append("Java/Kotlin")
    if "composer.json" in files:
        ecosystems.append("PHP")
    return ecosystems


def _repo_entrypoints(root: Path, top_level: set[str], package_json: dict[str, Any]) -> list[str]:
    entrypoints: list[str] = []
    for key in ("main", "module", "types"):
        value = package_json.get(key)
        if isinstance(value, str):
            entrypoints.append(f"{key}: {value}")
    bin_value = package_json.get("bin")
    if isinstance(bin_value, str):
        entrypoints.append(f"bin: {bin_value}")
    elif isinstance(bin_value, dict):
        entrypoints.extend(f"bin: {name}" for name in sorted(bin_value)[:10])
    exports_value = package_json.get("exports")
    if isinstance(exports_value, dict):
        entrypoints.extend(f"exports: {name}" for name in sorted(exports_value)[:10])
    common = [
        "index.ts",
        "index.tsx",
        "index.js",
        "index.mjs",
        "main.ts",
        "main.js",
        "src/index.ts",
        "src/index.tsx",
        "src/index.js",
        "src/main.ts",
        "src/main.js",
    ]
    for candidate in common:
        if "/" not in candidate and candidate in top_level:
            entrypoints.append(f"file: {candidate}")
        elif "/" in candidate and (root / candidate).is_file():
            entrypoints.append(f"file: {candidate}")
    return entrypoints


def _git_output(cwd: Path, args: list[str]) -> str | None:
    import subprocess

    try:
        result = subprocess.run(["git", *args], cwd=cwd, check=False, capture_output=True, text=True, timeout=5)
    except Exception:
        return None
    if result.returncode != 0:
        return None
    text = result.stdout.strip()
    return text or None


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


TASK_BOUNDARY_ALIASES = {
    "read_only": "read_only_project",
    "read-only": "read_only_project",
    "read_only_project": "read_only_project",
    "session": "session_local",
    "session_local": "session_local",
    "write": "active_repo_write",
    "active_repo_write": "active_repo_write",
    "execution": "execution",
    "network": "external_network",
    "external_network": "external_network",
    "extension": "extension_boundary",
    "extension_boundary": "extension_boundary",
}


def _available_task_agent_ids(store: SQLiteStore) -> list[str]:
    ids = {agent.agent_id for agent in store.list_project_agents()}
    registry = builtin_spec_registry()
    ids.update(registry.agents.keys())
    ids.update(profile.agent_id for profile in registry.agent_profiles.values())
    return sorted(ids)


def _validate_task_plan(store: SQLiteStore, session_id: str, arguments: dict[str, Any]) -> dict[str, Any]:
    store.get_session(session_id)
    objective = str(arguments.get("objective") or "").strip()
    if not objective:
        raise ValueError("Task objective is required.")
    output_expectation = str(arguments.get("output_expectation") or "").strip()
    if not output_expectation:
        raise ValueError("Task output_expectation is required.")
    raw_boundary = str(arguments.get("boundary") or "").strip().lower()
    boundary = TASK_BOUNDARY_ALIASES.get(raw_boundary)
    if boundary is None:
        raise ValueError("Task boundary must be one of read_only_project, session_local, active_repo_write, execution, external_network, or extension_boundary.")
    raw_tools = arguments.get("allowed_tools")
    if not isinstance(raw_tools, list) or not raw_tools:
        raise ValueError("Task allowed_tools must be a non-empty array.")
    allowed_tools: list[str] = []
    for item in raw_tools:
        tool_id = str(item).strip()
        if not tool_id:
            raise ValueError("Task allowed_tools cannot contain empty tool ids.")
        get_session_tool_descriptor(tool_id)
        allowed_tools.append(tool_id)
    allowed_tools = list(dict.fromkeys(allowed_tools))
    available_agents = _available_task_agent_ids(store)
    requested_agent = str(arguments.get("agent") or "").strip()
    agent_id = requested_agent or ("repo_inspector" if "repo_inspector" in available_agents else available_agents[0] if available_agents else "unconfigured")
    if requested_agent and requested_agent not in available_agents:
        raise ValueError(f"Task agent is not configured: {requested_agent}")
    title = str(arguments.get("title") or "").strip() or _task_title_from_objective(objective)
    return {
        "schema_version": "harness.session_tool_task_plan/v1",
        "title": title,
        "objective": objective,
        "output_expectation": output_expectation,
        "allowed_tools": allowed_tools,
        "boundary": boundary,
        "agent_id": agent_id,
        "planning_only": not bool(available_agents),
        "available_agent_count": len(available_agents),
    }


def _task_title_from_objective(objective: str) -> str:
    compact = " ".join(objective.split())
    return compact[:80] if len(compact) > 80 else compact


def _task_permission_target(plan: dict[str, Any]) -> str:
    digest = stable_json_sha256(
        {
            "agent_id": plan["agent_id"],
            "allowed_tools": plan["allowed_tools"],
            "boundary": plan["boundary"],
            "objective": plan["objective"],
            "output_expectation": plan["output_expectation"],
        }
    )[:16]
    return f"{plan['agent_id']}:{plan['boundary']}:{digest}"


def _task_tool(store: SQLiteStore, root: Path, session_id: str, arguments: dict[str, Any], *, run_id: str) -> str:
    plan = _validate_task_plan(store, session_id, arguments)
    if plan["planning_only"]:
        plan_path = store.runs_dir / run_id / "session_tool_task_plan.json"
        plan_payload = {
            **plan,
            "created": False,
            "execution_started": False,
            "process_started": False,
            "reason": "no_configured_agent_profiles",
        }
        plan_path.write_text(json.dumps(sanitize_for_logging(plan_payload), indent=2, sort_keys=True), encoding="utf-8")
        artifact = store.register_artifact(
            run_id,
            "session_tool_task_plan",
            plan_path,
            metadata={"tool_id": "task", "created": False, "planning_only": True},
            producer="session_tool",
            redaction_state="redacted",
            session_id=session_id,
        )
        return json.dumps(
            sanitize_for_logging(
                {
                    "schema_version": "harness.session_tool_task/v1",
                    "ok": True,
                    "created": False,
                    "planning_only": True,
                    "task_id": None,
                    "child_session_id": None,
                    "plan_artifact_id": artifact.id,
                    "execution_started": False,
                    "process_started": False,
                    "permission_granting": False,
                }
            ),
            indent=2,
            sort_keys=True,
        )

    parent = store.get_session(session_id)
    child = store.fork_session(
        session_id,
        title=f"Delegated: {plan['title']}",
        metadata={
            "delegated_from_session_id": session_id,
            "delegation_tool_run_id": run_id,
            "delegation": plan,
        },
    )
    task = store.create_task(
        plan["title"],
        description=plan["objective"],
        objective_id=parent.objective_id,
        workbench_id=parent.workbench_id,
        agent_id=plan["agent_id"],
        spec_source_kind="builtin" if plan["agent_id"] in builtin_spec_registry().agents else "project",
        metadata={
            "schema_version": "harness.session_tool_task_metadata/v1",
            "task_type": "session_delegate",
            "execution_adapter": "session_child_task",
            "execution_started": False,
            "hidden_process_started": False,
            "parent_session_id": session_id,
            "child_session_id": child.id,
            "source_tool_run_id": run_id,
            "allowed_tools": plan["allowed_tools"],
            "boundary": plan["boundary"],
            "output_expectation": plan["output_expectation"],
        },
        session_id=child.id,
    )
    store.update_session(session_id, active_task_id=task.id)
    store.append_store_event(
        EventStreamType.SESSION,
        session_id,
        "session.task.created",
        {
            "task_id": task.id,
            "child_session_id": child.id,
            "agent_id": task.agent_id,
            "boundary": plan["boundary"],
            "allowed_tools": plan["allowed_tools"],
            "execution_started": False,
            "process_started": False,
            "summary": f"Delegated task created: {task.title}",
        },
        session_id=session_id,
        run_id=run_id,
        task_id=task.id,
        redaction_state=RedactionState.REDACTED,
    )
    store.append_store_event(
        EventStreamType.SESSION,
        child.id,
        "session.task.assigned",
        {
            "task_id": task.id,
            "parent_session_id": session_id,
            "agent_id": task.agent_id,
            "execution_started": False,
            "summary": f"Task assigned: {task.title}",
        },
        session_id=child.id,
        run_id=run_id,
        task_id=task.id,
        redaction_state=RedactionState.REDACTED,
    )
    store.append_store_event(
        EventStreamType.TASK,
        task.id,
        "task.created_by_session_tool",
        {
            "task_id": task.id,
            "parent_session_id": session_id,
            "child_session_id": child.id,
            "tool_run_id": run_id,
            "execution_started": False,
            "summary": f"Task created by session tool: {task.title}",
        },
        session_id=child.id,
        run_id=run_id,
        task_id=task.id,
        redaction_state=RedactionState.REDACTED,
    )
    plan_path = store.runs_dir / run_id / "session_tool_task_plan.json"
    plan_payload = {
        **plan,
        "created": True,
        "task_id": task.id,
        "parent_session_id": session_id,
        "child_session_id": child.id,
        "execution_started": False,
        "process_started": False,
    }
    plan_path.write_text(json.dumps(sanitize_for_logging(plan_payload), indent=2, sort_keys=True), encoding="utf-8")
    artifact = store.register_artifact(
        run_id,
        "session_tool_task_plan",
        plan_path,
        metadata={"tool_id": "task", "task_id": task.id, "child_session_id": child.id, "created": True},
        producer="session_tool",
        redaction_state="redacted",
        session_id=session_id,
    )
    return json.dumps(
        sanitize_for_logging(
            {
                "schema_version": "harness.session_tool_task/v1",
                "ok": True,
                "created": True,
                "planning_only": False,
                "task_id": task.id,
                "child_session_id": child.id,
                "parent_session_id": session_id,
                "agent_id": task.agent_id,
                "status": task.status.value,
                "allowed_tools": plan["allowed_tools"],
                "boundary": plan["boundary"],
                "plan_artifact_id": artifact.id,
                "execution_started": False,
                "process_started": False,
                "permission_granting": False,
            }
        ),
        indent=2,
        sort_keys=True,
    )


def _resolve_task_status_subject(store: SQLiteStore, session_id: str, arguments: dict[str, Any]) -> tuple[Any, Any | None]:
    current = store.get_session(session_id)
    requested_session_id = str(arguments.get("session_id") or "").strip()
    if requested_session_id:
        requested_session = store.get_session(requested_session_id)
        if requested_session.id != session_id and requested_session.parent_session_id != session_id:
            raise _DeniedToolCall(
                "Task status session is not linked to this session.",
                action="task-status",
                target=requested_session_id,
                error_type="path_security",
            )
    else:
        requested_session = None
    task_id = str(arguments.get("task_id") or "").strip()
    if not task_id and requested_session is not None:
        task_id = requested_session.active_task_id or ""
    if not task_id:
        task_id = current.active_task_id or ""
    if not task_id:
        raise ValueError("No task_id was provided and the session has no active task.")
    task = store.get_task(task_id)
    linked_child_ids = {child.id for child in store.list_child_sessions(session_id)}
    parent_metadata_id = str(task.metadata.get("parent_session_id") or "")
    child_metadata_id = str(task.metadata.get("child_session_id") or "")
    linked = (
        task.session_id == session_id
        or task.session_id in linked_child_ids
        or parent_metadata_id == session_id
        or child_metadata_id in linked_child_ids
    )
    if not linked:
        raise _DeniedToolCall(
            "Task is not linked to this session.",
            action="task-status",
            target=task.id,
            error_type="path_security",
        )
    child_session = store.get_session(task.session_id) if task.session_id else None
    return task, child_session


def _task_status_tool(store: SQLiteStore, session_id: str, arguments: dict[str, Any]) -> str:
    task, child_session = _resolve_task_status_subject(store, session_id, arguments)
    attempts = store.list_task_attempts(task.id)
    leases = store.list_task_leases(task.id)
    task_events = store.list_store_events(EventStreamType.TASK, task.id)
    child_events = store.list_store_events(EventStreamType.SESSION, child_session.id) if child_session is not None else []
    artifact_ids: list[str] = []
    if task.run_id is not None:
        artifact_ids = [artifact.id for artifact in store.list_artifacts(task.run_id)]
    payload = {
        "schema_version": "harness.session_tool_task_status/v1",
        "ok": True,
        "task": task.model_dump(mode="json"),
        "child_session": child_session.model_dump(mode="json") if child_session is not None else None,
        "attempts": [attempt.model_dump(mode="json") for attempt in attempts],
        "leases": [lease.model_dump(mode="json") for lease in leases],
        "artifact_ids": artifact_ids,
        "task_events": [event.model_dump(mode="json") for event in task_events[-20:]],
        "child_session_events": [event.model_dump(mode="json") for event in child_events[-20:]],
        "execution_started": task.run_id is not None or bool(attempts),
        "process_started": False,
        "permission_granting": False,
    }
    return json.dumps(sanitize_for_logging(payload), indent=2, sort_keys=True)


def _plan_enter_tool(store: SQLiteStore, session_id: str, arguments: dict[str, Any], *, run_id: str) -> str:
    current = store.get_session(session_id)
    now = datetime.now(timezone.utc).isoformat()
    reason = str(sanitize_for_logging(arguments.get("reason") or "")).strip()
    metadata = dict(current.metadata or {})
    planning_mode = {
        "schema_version": "harness.session_planning_mode/v1",
        "active": True,
        "entered_at": now,
        "exited_at": None,
        "reason": reason or None,
        "summary": None,
        "next_action": None,
        "proposed_tools": [],
        "source": "session_tool",
        "run_id": run_id,
    }
    metadata[PLANNING_MODE_METADATA_KEY] = planning_mode
    store.update_session(session_id, metadata=metadata)
    store.append_store_event(
        EventStreamType.SESSION,
        session_id,
        "session.planning_mode.entered",
        {"planning_mode": planning_mode, "summary": "Planning mode entered."},
        session_id=session_id,
        run_id=run_id,
        redaction_state=RedactionState.REDACTED,
    )
    lines = ["Planning mode entered.", "Mutation and execution tools should produce plans unless explicitly approved."]
    if reason:
        lines.append(f"Reason: {reason}")
    return "\n".join(lines)


def _plan_exit_tool(store: SQLiteStore, session_id: str, arguments: dict[str, Any], *, run_id: str) -> str:
    current = store.get_session(session_id)
    now = datetime.now(timezone.utc).isoformat()
    previous = session_planning_mode_projection(current.metadata)
    summary = str(sanitize_for_logging(arguments.get("summary") or "")).strip()
    if not summary:
        raise ValueError("Plan exit summary is required.")
    next_action = str(sanitize_for_logging(arguments.get("next_action") or "")).strip()
    proposed_tools_raw = arguments.get("proposed_tools") or []
    proposed_tools = [str(sanitize_for_logging(item)).strip() for item in proposed_tools_raw if str(item).strip()]
    metadata = dict(current.metadata or {})
    planning_mode = {
        "schema_version": "harness.session_planning_mode/v1",
        "active": False,
        "entered_at": previous.get("entered_at"),
        "exited_at": now,
        "reason": previous.get("reason"),
        "summary": summary,
        "next_action": next_action or None,
        "proposed_tools": proposed_tools,
        "source": "session_tool",
        "run_id": run_id,
    }
    metadata[PLANNING_MODE_METADATA_KEY] = planning_mode
    store.update_session(session_id, metadata=metadata)
    store.append_store_event(
        EventStreamType.SESSION,
        session_id,
        "session.planning_mode.exited",
        {"planning_mode": planning_mode, "summary": summary},
        session_id=session_id,
        run_id=run_id,
        redaction_state=RedactionState.REDACTED,
    )
    lines = ["Planning mode exited.", f"Summary: {summary}"]
    if next_action:
        lines.append(f"Next action: {next_action}")
    if proposed_tools:
        lines.append(f"Proposed tools: {', '.join(proposed_tools)}")
    return "\n".join(lines)


def _validate_edit_target(root: Path, arguments: dict[str, Any], excludes: list[str]) -> str:
    relative_path = _validate_direct_write_target(root, str(arguments.get("path") or ""), excludes)
    path = root / relative_path
    if not path.exists():
        raise PatchValidationError(f"Edit target does not exist: {relative_path}")
    if not path.is_file():
        raise PatchValidationError(f"Edit target is not a file: {relative_path}")
    old = str(arguments.get("old") or "")
    if not old:
        raise PatchValidationError("Edit old text cannot be empty.")
    text = _read_text_file_for_mutation(path, relative_path)
    count = text.count(old)
    expected = _expected_replacements(arguments.get("expected_replacements"))
    if count != expected:
        raise PatchValidationError(f"Edit expected {expected} replacement(s) in {relative_path}, found {count}.")
    return relative_path


def _validate_write_target(root: Path, arguments: dict[str, Any], excludes: list[str]) -> str:
    relative_path = _validate_direct_write_target(root, str(arguments.get("path") or ""), excludes)
    path = root / relative_path
    if path.exists():
        _read_text_file_for_mutation(path, relative_path)
    elif not bool(arguments.get("create_dirs") or False) and not path.parent.exists():
        raise PatchValidationError(f"Write parent directory does not exist: {relative_path}")
    return relative_path


def _tool_mode(arguments: dict[str, Any]) -> str:
    mode = str(arguments.get("mode") or "apply").strip().lower()
    if mode not in {"apply", "plan"}:
        raise PatchValidationError("Tool mode must be apply or plan.")
    return mode


def _effective_mutation_mode(
    store: SQLiteStore,
    session_id: str,
    tool_id: str,
    action: str,
    target: str,
    boundary_kind: SessionPermissionBoundaryKind,
    arguments: dict[str, Any],
) -> str:
    mode = _tool_mode(arguments)
    if mode == "plan":
        return mode
    if _session_planning_active(store, session_id) and not _has_allowed_permission(store, session_id, tool_id, action, target, boundary_kind):
        return "plan"
    return mode


def _expected_replacements(value: Any) -> int:
    try:
        parsed = int(value if value is not None else 1)
    except (TypeError, ValueError) as exc:
        raise PatchValidationError("expected_replacements must be an integer.") from exc
    if parsed < 1:
        raise PatchValidationError("expected_replacements must be at least 1.")
    return parsed


def _read_text_file_for_mutation(path: Path, relative_path: str) -> str:
    raw = path.read_bytes()
    if b"\x00" in raw:
        raise PatchValidationError(f"Mutation target appears to be binary: {relative_path}")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PatchValidationError(f"Mutation target is not valid UTF-8 text: {relative_path}") from exc


def _edit_tool(store: SQLiteStore, root: Path, session_id: str, arguments: dict[str, Any], *, run_id: str) -> str:
    excludes = load_config(root).context_excludes
    relative_path = _validate_edit_target(root, arguments, excludes)
    path = root / relative_path
    before = _read_text_file_for_mutation(path, relative_path)
    old = str(arguments.get("old") or "")
    new = str(arguments.get("new") or "")
    expected = _expected_replacements(arguments.get("expected_replacements"))
    after = before.replace(old, new, expected)
    mode = _effective_mutation_mode(
        store,
        session_id,
        "edit",
        "edit",
        relative_path,
        SessionPermissionBoundaryKind.ACTIVE_REPO_WRITE,
        arguments,
    )
    return _persist_text_mutation(
        store,
        root,
        session_id,
        run_id,
        tool_id="edit",
        relative_path=relative_path,
        before=before,
        after=after,
        applied=mode == "apply",
        operation={"kind": "replace", "expected_replacements": expected, "actual_replacements": expected},
    )


def _write_tool(store: SQLiteStore, root: Path, session_id: str, arguments: dict[str, Any], *, run_id: str) -> str:
    excludes = load_config(root).context_excludes
    relative_path = _validate_write_target(root, arguments, excludes)
    path = root / relative_path
    before = _read_text_file_for_mutation(path, relative_path) if path.exists() else ""
    after = str(arguments.get("content") or "")
    mode = _effective_mutation_mode(
        store,
        session_id,
        "write",
        "write",
        relative_path,
        SessionPermissionBoundaryKind.ACTIVE_REPO_WRITE,
        arguments,
    )
    return _persist_text_mutation(
        store,
        root,
        session_id,
        run_id,
        tool_id="write",
        relative_path=relative_path,
        before=before,
        after=after,
        applied=mode == "apply",
        create_dirs=bool(arguments.get("create_dirs") or False),
        operation={"kind": "full_file_write", "created": not path.exists()},
    )


def _persist_text_mutation(
    store: SQLiteStore,
    root: Path,
    session_id: str,
    run_id: str,
    *,
    tool_id: str,
    relative_path: str,
    before: str,
    after: str,
    applied: bool,
    operation: dict[str, Any],
    create_dirs: bool = False,
) -> str:
    path = root / relative_path
    diff = _unified_text_diff(relative_path, before, after)
    before_sha = hashlib.sha256(before.encode("utf-8")).hexdigest() if before else None
    after_sha = hashlib.sha256(after.encode("utf-8")).hexdigest()
    policy_evidence = {
        "policy_boundary": {
            "kind": "active_repo_write",
            "boundary_kind": SessionPermissionBoundaryKind.ACTIVE_REPO_WRITE.value,
            "source": f"session_tool_{tool_id}",
        },
        "approval_required": applied,
        "required_approval": "active_repo_write",
        "applied": applied,
        "file_written": applied,
        "filesystem_modified": applied,
        "active_repo_modified": applied,
        "git_mutation_started": False,
        "permission_granting": False,
        "authority_granting": False,
    }
    if applied:
        _write_text_atomic(path, after, create_dirs=create_dirs)
    diff_path = store.runs_dir / run_id / f"session_tool_{tool_id}_diff.patch"
    diff_path.write_text(diff, encoding="utf-8")
    diff_artifact = store.register_artifact(
        run_id,
        f"session_tool_{tool_id}_diff",
        diff_path,
        metadata={
            "tool_id": tool_id,
            "target": relative_path,
            "before_sha256": before_sha,
            "after_sha256": after_sha,
            **policy_evidence,
        },
        producer="session_tool",
        redaction_state="redacted",
        session_id=session_id,
    )
    metadata_payload = {
        "schema_version": f"harness.session_tool_{tool_id}_mutation/v1",
        "tool_id": tool_id,
        "target": relative_path,
        "operation": operation,
        "before_sha256": before_sha,
        "after_sha256": after_sha,
        "before_bytes": len(before.encode("utf-8")),
        "after_bytes": len(after.encode("utf-8")),
        "diff_artifact_id": diff_artifact.id,
        **policy_evidence,
    }
    metadata_path = store.runs_dir / run_id / f"session_tool_{tool_id}_mutation.json"
    metadata_path.write_text(json.dumps(sanitize_for_logging(metadata_payload), indent=2, sort_keys=True), encoding="utf-8")
    metadata_artifact = store.register_artifact(
        run_id,
        f"session_tool_{tool_id}_mutation",
        metadata_path,
        metadata={
            "tool_id": tool_id,
            "target": relative_path,
            "diff_artifact_id": diff_artifact.id,
            **policy_evidence,
        },
        producer="session_tool",
        redaction_state="redacted",
        session_id=session_id,
    )
    verb = "applied" if applied else "planned"
    return (
        f"{tool_id} {verb}.\n"
        f"Target: {relative_path}\n"
        f"Before SHA256: {before_sha or 'new file'}\n"
        f"After SHA256: {after_sha}\n"
        f"Diff artifact: {diff_artifact.id}\n"
        f"Metadata artifact: {metadata_artifact.id}"
    )


def _write_text_atomic(path: Path, content: str, *, create_dirs: bool) -> None:
    if create_dirs:
        path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.harness-{uuid.uuid4().hex}.tmp")
    temp_path.write_text(content, encoding="utf-8")
    os.replace(temp_path, path)


def _unified_text_diff(relative_path: str, before: str, after: str) -> str:
    before_lines = before.splitlines(keepends=True)
    after_lines = after.splitlines(keepends=True)
    return "".join(
        difflib.unified_diff(
            before_lines,
            after_lines,
            fromfile=f"a/{relative_path}",
            tofile=f"b/{relative_path}",
        )
    )


def _validate_cd_target(store: SQLiteStore, root: Path, session_id: str, arguments: dict[str, Any], *, allow_excluded: bool = False) -> str:
    session = store.get_session(session_id)
    resolver = CwdResolver(project_root=root, context_excludes=load_config(root).context_excludes)
    resolved = resolver.resolve_cd(
        session_cwd=session_cwd_from_metadata(session.metadata),
        requested_path=str(arguments.get("path") or "."),
        allow_excluded=allow_excluded,
    )
    return resolved.normalized_project_relative_cwd


def _validate_git_diff_target(root: Path, store: SQLiteStore, session_id: str, arguments: dict[str, Any], *, allow_excluded: bool = False) -> str:
    target = _resolve_git_diff_path(root, store, session_id, arguments, allow_excluded=allow_excluded)
    return relative_to_project(root, target)


def _validate_docker_test_plan(root: Path, store: SQLiteStore, session_id: str, arguments: dict[str, Any]) -> str:
    _command, _cwd, target = _docker_test_plan_values(root, store, session_id, arguments)
    return target


def _validate_web_fetch_plan(root: Path, arguments: dict[str, Any]) -> str:
    return str(_web_fetch_plan_values(root, arguments)["target"])


def _web_fetch_plan_values(root: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    raw_url = str(arguments.get("url") or "").strip()
    target = _normalize_web_fetch_target(raw_url)
    parsed = urlparse(raw_url)
    cfg = load_config(root).web_tools
    if not cfg.enabled or not cfg.fetch_enabled:
        raise ValueError("Web fetch is disabled by project web_tools policy.")
    host = (parsed.hostname or "").lower()
    allowed_domains = [domain.lower().strip() for domain in cfg.allowed_domains if domain.strip()]
    if allowed_domains and host not in allowed_domains:
        raise ValueError(f"Web fetch host is not allowed by project web_tools policy: {host}")
    requested_format = str(arguments.get("format") or "markdown").strip().lower()
    if requested_format not in {"markdown", "text", "html"}:
        raise ValueError("Web fetch format must be markdown, text, or html.")
    timeout = arguments.get("timeout", 30)
    try:
        timeout_seconds = int(timeout)
    except (TypeError, ValueError) as exc:
        raise ValueError("Web fetch timeout must be a number of seconds.") from exc
    if timeout_seconds < 1 or timeout_seconds > 120:
        raise ValueError("Web fetch timeout must be between 1 and 120 seconds.")
    return {
        "schema_version": "harness.session_tool_web_fetch_plan/v1",
        "url": raw_url,
        "target": target,
        "host": host,
        "format": requested_format,
        "timeout_seconds": timeout_seconds,
        "allowed_domains": allowed_domains,
        "requires_network": True,
        "network_called": False,
        "fetch_executed": False,
        "content_artifact_id": None,
        "max_response_bytes": WEB_FETCH_MAX_RESPONSE_BYTES,
        "permission_boundary": {
            "kind": "external_network_fetch",
            "boundary_kind": SessionPermissionBoundaryKind.EXTERNAL_NETWORK.value,
            "target": target,
            "host": host,
            "approval_required": True,
            "allowed_domains": allowed_domains,
            "provider": "urllib",
        },
        "notes": [
            "Fetch execution must persist response metadata, content hash, redaction state, and content artifact before display.",
        ],
    }


def _normalize_web_fetch_target(raw_url: str) -> str:
    if not raw_url:
        raise ValueError("Missing web fetch URL.")
    parsed = urlparse(raw_url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Web fetch URL must start with http:// or https://.")
    if parsed.username or parsed.password:
        raise ValueError("Web fetch URL must not include credentials.")
    if not parsed.hostname:
        raise ValueError("Web fetch URL must include a host.")
    host = parsed.hostname.lower()
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{host}{port}"


def _session_tool_network_evidence(
    store: SQLiteStore,
    root: Path,
    session: Any,
    tool_id: str,
    plan_payload: dict[str, Any],
    *,
    permission_id: str | None,
    permission_expires_at: datetime | None,
    request_url: str,
    allow_downloads: bool,
) -> tuple[GovernanceNetworkPolicy, dict[str, Any], Path]:
    del store
    target = str(plan_payload.get("target") or request_url)
    if not permission_id or permission_expires_at is None:
        raise _DeniedToolCall(
            "Network-enabled execution requires exact approval evidence before any request starts.",
            action=tool_id,
            target=target,
            error_type="network_policy_required",
        )
    parsed = urlparse(request_url)
    protocol = (parsed.scheme or "https").lower()
    allowed_hosts = _network_policy_allowed_hosts(plan_payload, request_url)
    policy = build_session_tool_network_policy(
        root,
        session_id=session.id,
        task_id=session.active_task_id,
        tool_id=tool_id,
        target=target,
        approval_id=permission_id,
        expires_at=permission_expires_at.isoformat(timespec="seconds").replace("+00:00", "Z"),
        allowed_hosts=allowed_hosts,
        denied_hosts=["169.254.169.254", "metadata.google.internal"],
        allowed_protocols=[protocol],
        allowed_methods=["GET"],
        proxy_endpoint=None,
        allow_downloads=allow_downloads,
    )
    check = validate_network_policy(policy)
    if not check.ok:
        raise _DeniedToolCall(
            "Network policy invalid: " + "; ".join(check.errors),
            action=tool_id,
            target=target,
            error_type="network_policy_denied",
        )
    decision = evaluate_network_request(policy, request_url, method="GET")
    if not decision["allowed"]:
        raise _DeniedToolCall(
            "Network policy denied request: " + str(decision["reason"]),
            action=tool_id,
            target=target,
            error_type="network_policy_denied",
        )
    write_network_policy_check(root, policy)
    log_path = write_network_request_log(root, policy, [decision])
    return policy, decision, log_path


def _network_policy_allowed_hosts(plan_payload: dict[str, Any], request_url: str) -> list[str]:
    hosts: list[str] = []
    for key in ("host", "endpoint_target"):
        value = str(plan_payload.get(key) or "").strip()
        if not value:
            continue
        parsed = urlparse(value)
        host = (parsed.hostname or value.split("://", 1)[-1].split("/", 1)[0]).split(":", 1)[0].lower()
        if host:
            hosts.append(host)
    parsed_request = urlparse(request_url)
    if parsed_request.scheme == "file":
        hosts.append("local/file")
    elif parsed_request.hostname:
        hosts.append(parsed_request.hostname.lower())
    allowed_domains = plan_payload.get("allowed_domains")
    if isinstance(allowed_domains, list):
        hosts.extend(str(host).lower().strip() for host in allowed_domains if str(host).strip())
    return sorted(set(hosts))


def _execute_web_fetch(plan: dict[str, Any]) -> dict[str, Any]:
    headers = {
        "User-Agent": "harness-session-tools/1.0",
        "Accept": _web_fetch_accept_header(str(plan["format"])),
        "Accept-Language": "en-US,en;q=0.9",
    }
    request = urllib.request.Request(str(plan["url"]), headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=int(plan["timeout_seconds"])) as response:
            status_code = int(getattr(response, "status", 200))
            response_headers = {str(key).lower(): str(value) for key, value in response.headers.items()}
            content_length = response_headers.get("content-length")
            if content_length and int(content_length) > WEB_FETCH_MAX_RESPONSE_BYTES:
                raise ValueError("Response too large (exceeds 5MB limit).")
            body = response.read(WEB_FETCH_MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        status_code = int(exc.code)
        response_headers = {str(key).lower(): str(value) for key, value in exc.headers.items()}
        body = exc.read(WEB_FETCH_MAX_RESPONSE_BYTES + 1)
    except urllib.error.URLError as exc:
        raise ValueError(f"Web fetch failed: {exc.reason}") from exc
    if len(body) > WEB_FETCH_MAX_RESPONSE_BYTES:
        raise ValueError("Response too large (exceeds 5MB limit).")
    content_type = response_headers.get("content-type", "")
    charset = _web_fetch_charset(content_type)
    text = body.decode(charset, errors="replace")
    output = _format_web_fetch_content(text, content_type, str(plan["format"]))
    return {
        "status_code": status_code,
        "headers": response_headers,
        "content_type": content_type,
        "content_length_header": response_headers.get("content-length"),
        "content": output,
    }


def _web_fetch_accept_header(format_name: str) -> str:
    if format_name == "markdown":
        return "text/markdown;q=1.0, text/x-markdown;q=0.9, text/plain;q=0.8, text/html;q=0.7, */*;q=0.1"
    if format_name == "text":
        return "text/plain;q=1.0, text/markdown;q=0.9, text/html;q=0.8, */*;q=0.1"
    if format_name == "html":
        return "text/html;q=1.0, application/xhtml+xml;q=0.9, text/plain;q=0.8, text/markdown;q=0.7, */*;q=0.1"
    return "*/*"


def _web_fetch_charset(content_type: str) -> str:
    match = re.search(r"charset=([^;\s]+)", content_type, flags=re.IGNORECASE)
    return match.group(1).strip("\"'") if match else "utf-8"


def _format_web_fetch_content(text: str, content_type: str, format_name: str) -> str:
    if "text/html" not in content_type.lower():
        return text
    if format_name == "html":
        return text
    if format_name == "text":
        return _html_to_text(text)
    return _html_to_markdown(text)


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._skip_depth > 0 or tag.lower() in {"script", "style", "noscript", "iframe", "object", "embed"}:
            self._skip_depth += 1
            return
        if tag.lower() in {"p", "br", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if self._skip_depth > 0:
            self._skip_depth -= 1
            return
        if tag.lower() in {"p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self.parts.append(data)


def _html_to_text(html: str) -> str:
    parser = _HTMLTextExtractor()
    parser.feed(html)
    text = "".join(parser.parts)
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def _html_to_markdown(html: str) -> str:
    text = re.sub(r"(?is)<(script|style|noscript|iframe|object|embed)\b.*?</\1>", "", html)
    text = re.sub(r"(?is)<h([1-6])[^>]*>(.*?)</h\1>", lambda m: f"\n{'#' * int(m.group(1))} {_strip_html(m.group(2)).strip()}\n", text)
    text = re.sub(r"(?is)<li[^>]*>(.*?)</li>", lambda m: f"\n- {_strip_html(m.group(1)).strip()}", text)
    text = re.sub(r"(?is)<br\s*/?>", "\n", text)
    text = re.sub(r"(?is)</p\s*>|</div\s*>", "\n", text)
    return _html_to_text(text)


def _strip_html(html: str) -> str:
    return re.sub(r"(?is)<[^>]+>", "", html)


def _validate_web_search_plan(root: Path, arguments: dict[str, Any]) -> str:
    return str(_web_search_plan_values(root, arguments)["target"])


def _web_search_plan_values(root: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    query = str(arguments.get("query") or "").strip()
    if not query:
        raise ValueError("Missing web search query.")
    cfg = load_config(root).web_tools
    if not cfg.enabled or not cfg.search_enabled:
        raise ValueError("Web search is disabled by project web_tools policy.")
    provider = _normalize_web_search_provider(str(cfg.search_provider or "configured_http"))
    endpoint = _web_search_endpoint_for_provider(provider, str(cfg.search_endpoint_url or "").strip())
    endpoint_target = _normalize_web_search_endpoint(endpoint)
    num_results = _bounded_int(arguments.get("num_results", arguments.get("numResults", 8)), "Web search result limit", minimum=1, maximum=20)
    search_type = str(arguments.get("search_type") or arguments.get("type") or "auto").strip().lower()
    if search_type not in {"auto", "fast", "deep"}:
        raise ValueError("Web search type must be auto, fast, or deep.")
    livecrawl = str(arguments.get("livecrawl") or "fallback").strip().lower()
    if livecrawl not in {"fallback", "preferred"}:
        raise ValueError("Web search livecrawl must be fallback or preferred.")
    context_max_characters = _bounded_int(
        arguments.get("context_max_characters", arguments.get("contextMaxCharacters", 10000)),
        "Web search context max characters",
        minimum=1000,
        maximum=50000,
    )
    allowed_domains_arg = arguments.get("allowed_domains") or arguments.get("allowedDomains") or []
    if not isinstance(allowed_domains_arg, list):
        raise ValueError("Web search allowed_domains must be a list.")
    project_allowed_domains = [domain.lower().strip() for domain in cfg.allowed_domains if domain.strip()]
    requested_domains = [str(domain).lower().strip() for domain in allowed_domains_arg if str(domain).strip()]
    if project_allowed_domains and any(domain not in project_allowed_domains for domain in requested_domains):
        raise ValueError("Web search requested domain filter includes domains not allowed by project web_tools policy.")
    effective_domains = requested_domains or project_allowed_domains
    if provider in {"exa_mcp", "parallel_mcp"} and effective_domains:
        raise ValueError(
            f"Web search provider {provider} does not support Harness allowed_domains enforcement. "
            "Use configured_http with a domain-enforcing search endpoint or clear web_tools.allowed_domains."
        )
    return {
        "schema_version": "harness.session_tool_web_search_plan/v1",
        "query": query,
        "target": query,
        "num_results": num_results,
        "search_type": search_type,
        "livecrawl": livecrawl,
        "context_max_characters": context_max_characters,
        "allowed_domains": effective_domains,
        "provider": provider,
        "endpoint": endpoint,
        "endpoint_target": endpoint_target,
        "requires_network": True,
        "network_called": False,
        "search_executed": False,
        "results_artifact_id": None,
        "permission_boundary": {
            "kind": "external_network_search",
            "boundary_kind": SessionPermissionBoundaryKind.EXTERNAL_NETWORK.value,
            "provider": provider,
            "endpoint_target": endpoint_target,
            "approval_required": True,
            "allowed_domains": effective_domains,
        },
        "notes": [
            "Search execution must persist provider metadata, result hashes, redaction state, and result artifacts before display.",
        ],
    }


def _normalize_web_search_provider(raw_provider: str) -> str:
    provider = raw_provider.strip().lower().replace("-", "_")
    aliases = {
        "http": "configured_http",
        "configured": "configured_http",
        "configured_http": "configured_http",
        "exa": "exa_mcp",
        "exa_mcp": "exa_mcp",
        "opencode_exa": "exa_mcp",
        "parallel": "parallel_mcp",
        "parallel_mcp": "parallel_mcp",
        "opencode_parallel": "parallel_mcp",
    }
    normalized = aliases.get(provider)
    if normalized is None:
        raise ValueError("Web search provider must be configured_http, exa_mcp, or parallel_mcp.")
    return normalized


def _web_search_endpoint_for_provider(provider: str, configured_endpoint: str) -> str:
    if provider == "configured_http":
        if not configured_endpoint:
            raise ValueError("Web search endpoint is not configured by project web_tools policy.")
        return configured_endpoint
    if provider == "exa_mcp":
        return configured_endpoint or WEB_SEARCH_EXA_MCP_URL
    if provider == "parallel_mcp":
        return configured_endpoint or WEB_SEARCH_PARALLEL_MCP_URL
    raise ValueError("Web search provider must be configured_http, exa_mcp, or parallel_mcp.")


def _normalize_web_search_endpoint(raw_url: str) -> str:
    parsed = urlparse(raw_url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Web search endpoint URL must start with http:// or https://.")
    if parsed.username or parsed.password:
        raise ValueError("Web search endpoint URL must not include credentials.")
    if not parsed.hostname:
        raise ValueError("Web search endpoint URL must include a host.")
    host = parsed.hostname.lower()
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{host}{port}"


def _execute_web_search(plan: dict[str, Any]) -> dict[str, Any]:
    provider = str(plan.get("provider") or "configured_http")
    if provider in {"exa_mcp", "parallel_mcp"}:
        return _execute_mcp_web_search(plan, provider=provider)
    params = {
        "q": str(plan["query"]),
        "num_results": str(plan["num_results"]),
        "type": str(plan["search_type"]),
        "livecrawl": str(plan["livecrawl"]),
        "context_max_characters": str(plan["context_max_characters"]),
    }
    if plan.get("allowed_domains"):
        params["allowed_domains"] = ",".join(str(domain) for domain in plan["allowed_domains"])
    separator = "&" if "?" in str(plan["endpoint"]) else "?"
    url = f"{plan['endpoint']}{separator}{urlencode(params)}"
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "harness-session-tools/1.0",
            "Accept": "application/json, text/plain;q=0.8, */*;q=0.1",
            "Accept-Language": "en-US,en;q=0.9",
        },
        method="GET",
    )
    status_code, headers, body = _open_web_search_request(request)
    if len(body) > WEB_FETCH_MAX_RESPONSE_BYTES:
        raise ValueError("Search response too large (exceeds 5MB limit).")
    content_type = headers.get("content-type", "")
    charset = _web_fetch_charset(content_type)
    text = body.decode(charset, errors="replace")
    output = _format_web_search_output(text, content_type)
    return {
        "status_code": status_code,
        "headers": headers,
        "content_type": content_type,
        "content_length_header": headers.get("content-length"),
        "output": output,
    }


def _execute_mcp_web_search(plan: dict[str, Any], *, provider: str) -> dict[str, Any]:
    if provider == "parallel_mcp":
        tool_name = "web_search"
        arguments = {
            "objective": str(plan["query"]),
            "search_queries": [str(plan["query"])],
        }
    else:
        tool_name = "web_search_exa"
        arguments = {
            "query": str(plan["query"]),
            "type": str(plan["search_type"]),
            "numResults": int(plan["num_results"]),
            "livecrawl": str(plan["livecrawl"]),
            "contextMaxCharacters": int(plan["context_max_characters"]),
        }
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": tool_name,
            "arguments": arguments,
        },
    }
    headers = {
        "User-Agent": "harness-session-tools/1.0",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if provider == "parallel_mcp" and os.environ.get("PARALLEL_API_KEY"):
        headers["Authorization"] = f"Bearer {os.environ['PARALLEL_API_KEY']}"
    request = urllib.request.Request(
        str(plan["endpoint"]),
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    status_code, response_headers, body = _open_web_search_request(request)
    if len(body) > WEB_FETCH_MAX_RESPONSE_BYTES:
        raise ValueError("Search response too large (exceeds 5MB limit).")
    content_type = response_headers.get("content-type", "")
    charset = _web_fetch_charset(content_type)
    text = body.decode(charset, errors="replace")
    output = _parse_mcp_web_search_response(text) or "No search results found. Please try a different query."
    return {
        "status_code": status_code,
        "headers": response_headers,
        "content_type": content_type,
        "content_length_header": response_headers.get("content-length"),
        "output": output,
    }


def _open_web_search_request(request: urllib.request.Request) -> tuple[int, dict[str, str], bytes]:
    try:
        with _urlopen_with_project_certs(request, timeout=25) as response:
            status_code = int(getattr(response, "status", 200))
            headers = {str(key).lower(): str(value) for key, value in response.headers.items()}
            body = response.read(WEB_FETCH_MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        status_code = int(exc.code)
        headers = {str(key).lower(): str(value) for key, value in exc.headers.items()}
        body = exc.read(WEB_FETCH_MAX_RESPONSE_BYTES + 1)
    except urllib.error.URLError as exc:
        raise ValueError(f"Web search failed: {exc.reason}") from exc
    return status_code, headers, body


def _urlopen_with_project_certs(request: urllib.request.Request, *, timeout: int):
    if str(getattr(request, "full_url", "")).lower().startswith("https://"):
        context = _certifi_ssl_context()
        if context is not None:
            return urllib.request.urlopen(request, timeout=timeout, context=context)
    return urllib.request.urlopen(request, timeout=timeout)


def _certifi_ssl_context() -> ssl.SSLContext | None:
    try:
        import certifi  # type: ignore[import-not-found]
    except Exception:
        return None
    try:
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return None


def _parse_mcp_web_search_response(text: str) -> str | None:
    trimmed = text.strip()
    direct = _parse_mcp_web_search_payload(trimmed)
    if direct:
        return direct
    for line in text.splitlines():
        if not line.startswith("data: "):
            continue
        parsed = _parse_mcp_web_search_payload(line[6:].strip())
        if parsed:
            return parsed
    return None


def _parse_mcp_web_search_payload(payload: str) -> str | None:
    if not payload or not payload.startswith("{"):
        return None
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return None
    if isinstance(data, dict) and isinstance(data.get("error"), dict):
        error = data["error"]
        message = str(error.get("message") or "MCP web search failed.")
        raise ValueError(f"Web search failed: {sanitize_for_logging(message)}")
    result = data.get("result") if isinstance(data, dict) else None
    content = result.get("content") if isinstance(result, dict) else None
    if not isinstance(content, list):
        return None
    for item in content:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if isinstance(text, str) and text.strip():
            return text
    return None


def _format_web_search_output(text: str, content_type: str) -> str:
    if "json" not in content_type.lower():
        return text
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return text
    return json.dumps(sanitize_for_logging(payload), indent=2, sort_keys=True, default=str)


def _bounded_int(value: Any, label: str, *, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a number.") from exc
    if parsed < minimum or parsed > maximum:
        raise ValueError(f"{label} must be between {minimum} and {maximum}.")
    return parsed


def _docker_test_plan_values(root: Path, store: SQLiteStore, session_id: str, arguments: dict[str, Any]) -> tuple[list[str], str | None, str]:
    command_arg = arguments.get("command")
    command = command_arg if isinstance(command_arg, list) else [command_arg] if isinstance(command_arg, str) else []
    validate_test_command(command)
    try:
        session = store.get_session(session_id)
        resolver = CwdResolver(project_root=root, context_excludes=load_config(root).context_excludes)
        resolved = resolver.resolve_cwd(
            session_cwd=session_cwd_from_metadata(session.metadata),
            call_cwd=str(arguments.get("cwd")) if arguments.get("cwd") not in {None, ""} else None,
            action="docker-test",
        )
    except CwdResolutionError as exc:
        raise CommandValidationError(exc.message) from exc
    cwd = resolved.normalized_project_relative_cwd
    return command, cwd, f"{cwd or '.'}:{' '.join(command)}"


def _git_diff_tool(
    store: SQLiteStore,
    root: Path,
    session_id: str,
    arguments: dict[str, Any],
    *,
    run_id: str,
    allow_excluded: bool = False,
) -> str:
    target_path = _resolve_git_diff_path(root, store, session_id, arguments, allow_excluded=allow_excluded)
    target_rel = relative_to_project(root, target_path)
    if _git_output(root, ["rev-parse", "--is-inside-work-tree"]) != "true":
        return "Git diff unavailable: project root is not inside a git work tree."
    scope_args: list[str] = []
    if target_rel != ".":
        scope_args = ["--", target_rel]
    stat = _run_git_capture(root, ["diff", "--stat", *scope_args])
    stat_only = bool(arguments.get("stat_only") or arguments.get("statOnly") or False)
    patch = "" if stat_only else _run_git_capture(root, ["diff", *scope_args])
    metadata = {
        "schema_version": "harness.session_tool_git_diff/v1",
        "target": target_rel,
        "stat_only": stat_only,
        "process_started": True,
        "git_process_started": True,
        "git_mutation_started": False,
        "filesystem_modified": False,
        "active_repo_modified": False,
        "read_only": True,
    }
    metadata_path = store.runs_dir / run_id / "session_tool_git_diff_metadata.json"
    metadata_path.write_text(json.dumps(sanitize_for_logging(metadata), indent=2, sort_keys=True), encoding="utf-8")
    metadata_artifact = store.register_artifact(
        run_id,
        "session_tool_git_diff_metadata",
        metadata_path,
        metadata={"tool_id": "git-diff", "target": target_rel, "read_only": True},
        producer="session_tool",
        redaction_state="redacted",
        session_id=session_id,
    )
    body_parts = [
        "Git diff (read-only).",
        f"Target: {target_rel}",
        f"Metadata artifact: {metadata_artifact.id}",
        "",
        "Stat:",
        stat.strip() or "[no diff]",
    ]
    if not stat_only:
        if patch:
            diff_path = store.runs_dir / run_id / "session_tool_git_diff.patch"
            diff_path.write_text(str(sanitize_for_logging(patch)), encoding="utf-8")
            diff_artifact = store.register_artifact(
                run_id,
                "session_tool_git_diff_patch",
                diff_path,
                metadata={"tool_id": "git-diff", "target": target_rel, "read_only": True},
                producer="session_tool",
                redaction_state="redacted",
                session_id=session_id,
            )
            body_parts.extend(["", f"Patch artifact: {diff_artifact.id}", "", patch])
        else:
            body_parts.extend(["", "[no diff]"])
    return "\n".join(body_parts)


def _run_git_capture(cwd: Path, args: list[str]) -> str:
    completed = subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    output = completed.stdout if completed.returncode == 0 else completed.stderr or completed.stdout
    return str(sanitize_for_logging(output))


def _simple_shell_cd_path(command: str) -> str | None:
    stripped = command.strip()
    if not stripped or "\n" in stripped or "\r" in stripped:
        return None
    try:
        parts = shlex.split(stripped, posix=True)
    except ValueError:
        return None
    if len(parts) == 2 and parts[0] == "cd":
        return parts[1]
    return None


def _shell_permission_target(
    root: Path,
    store: SQLiteStore,
    session_id: str,
    arguments: dict[str, Any],
    *,
    allow_excluded: bool = False,
    run_mode: RunMode | str = RunMode.READ_ONLY,
) -> str:
    run_mode_value = RunMode(run_mode.value if isinstance(run_mode, RunMode) else run_mode)
    plan = _shell_plan_values(root, store, session_id, arguments, allow_excluded=allow_excluded, run_mode=run_mode_value)
    target = {
        "project_fingerprint": plan["project_fingerprint"],
        "project_root_fingerprint": plan["project_root_fingerprint"],
        "session_id": session_id,
        "tool_id": "shell",
        "normalized_cwd": plan["cwd"],
        "resolved_cwd": plan["resolved_cwd"],
        "command": plan["command"],
        "normalized_command": plan["command"],
        "normalized_operation": plan["command"],
        "timeout": plan["timeout_seconds"],
        "timeout_seconds": plan["timeout_seconds"],
        "shell_executable": plan["shell_executable"],
        "env_policy": plan["env_policy"],
        "network_policy": plan["network_policy"],
        "sandbox_profile": plan["sandbox_profile"],
        "run_mode": plan["run_mode"],
    }
    return json.dumps(target, sort_keys=True, separators=(",", ":"))


def _shell_plan_values(
    root: Path,
    store: SQLiteStore,
    session_id: str,
    arguments: dict[str, Any],
    *,
    allow_excluded: bool = False,
    run_mode: RunMode | str = RunMode.READ_ONLY,
) -> dict[str, Any]:
    run_mode_value = RunMode(run_mode.value if isinstance(run_mode, RunMode) else run_mode)
    command = str(arguments.get("command") or "").strip()
    if not command:
        raise ValueError("Missing shell command.")
    timeout_seconds = _bounded_int(arguments.get("timeout_seconds", arguments.get("timeout", 120)), "Shell timeout", minimum=1, maximum=900)
    shell_executable = _shell_executable(arguments)
    session = store.get_session(session_id)
    resolver = CwdResolver(project_root=root, context_excludes=load_config(root).context_excludes)
    cwd = resolver.resolve_cwd(
        session_cwd=session_cwd_from_metadata(session.metadata),
        call_cwd=str(arguments.get("cwd")) if arguments.get("cwd") not in {None, ""} else None,
        action="shell",
        allow_excluded=allow_excluded,
    )
    project_fingerprint = hashlib.sha256(str(root).encode("utf-8")).hexdigest()
    return {
        "schema_version": "harness.session_shell_plan/v1",
        "project_fingerprint": project_fingerprint,
        "project_root_fingerprint": project_fingerprint,
        "session_id": session_id,
        "cwd": cwd.normalized_project_relative_cwd,
        "resolved_cwd": cwd.resolved_abs_path,
        "command": command,
        "timeout_seconds": timeout_seconds,
        "shell_executable": shell_executable,
        "env_policy": "minimal_inherited_path_home",
        "network_policy": "host_network_available",
        "sandbox_profile": "session_tool_shell_exact",
        "run_mode": run_mode_value.value,
        "process_started": False,
        "shell_execution_started": False,
    }


def _shell_executable(arguments: dict[str, Any]) -> str:
    executable = str(arguments.get("shell_executable") or arguments.get("shell") or "/bin/sh").strip()
    if not executable:
        executable = "/bin/sh"
    path = Path(executable)
    if not path.is_absolute():
        raise ValueError("Shell executable must be an absolute path.")
    if not path.exists() or not os.access(path, os.X_OK):
        raise ValueError(f"Shell executable is not executable: {executable}")
    return str(path)


def _shell_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for key in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TERM"):
        value = os.environ.get(key)
        if value is not None:
            env[key] = value
    return env


def _execute_shell_tool(
    store: SQLiteStore,
    root: Path,
    session_id: str,
    arguments: dict[str, Any],
    *,
    run_id: str,
    allow_excluded: bool = False,
) -> str:
    plan = _shell_plan_values(root, store, session_id, arguments, allow_excluded=allow_excluded)
    started = False
    timed_out = False
    exit_code: int | None = None
    stdout = ""
    stderr = ""
    process_id: str | None = None
    supervisor = get_process_supervisor(store.project_root)

    def on_start(record: ProcessRecord) -> None:
        nonlocal started, process_id
        started = True
        process_id = record.process_id
        store.append_store_event(
            EventStreamType.SESSION,
            session_id,
            "harness.process.started",
            {
                "process": record.model_dump(mode="json"),
                "tool_id": "shell",
                "summary": f"shell process started: {record.process_id}",
            },
            session_id=session_id,
            run_id=run_id,
            redaction_state=RedactionState.REDACTED,
        )

    result = supervisor.run(
        plan["command"],
        shell=True,
        executable=plan["shell_executable"],
        cwd=plan["resolved_cwd"],
        env=_shell_env(),
        timeout_seconds=float(plan["timeout_seconds"]),
        owner="session_tool.shell",
        session_id=session_id,
        run_id=run_id,
        tool_call_id=str(arguments.get("tool_call_id") or "") or None,
        on_start=on_start,
    )
    started = True
    process_id = process_id or result.process_id
    timed_out = result.timed_out
    exit_code = result.exit_code
    stdout = result.stdout
    stderr = result.stderr
    store.append_store_event(
        EventStreamType.SESSION,
        session_id,
        "harness.process.finished",
        {
            "process": result.model_dump(mode="json", exclude={"stdout", "stderr"}),
            "tool_id": "shell",
            "summary": f"shell process {result.status}: {result.process_id}",
        },
        session_id=session_id,
        run_id=run_id,
        redaction_state=RedactionState.REDACTED,
    )
    stdout = str(sanitize_for_logging(stdout))
    stderr = str(sanitize_for_logging(stderr))
    stdout_artifact_id: str | None = None
    stderr_artifact_id: str | None = None
    if stdout:
        stdout_path = store.runs_dir / run_id / "session_tool_shell_stdout.txt"
        stdout_path.write_text(stdout, encoding="utf-8")
        stdout_artifact = store.register_artifact(
            run_id,
            "session_tool_shell_stdout",
            stdout_path,
            metadata={"tool_id": "shell", "stream": "stdout"},
            producer="session_tool",
            redaction_state="redacted",
            session_id=session_id,
        )
        stdout_artifact_id = stdout_artifact.id
    if stderr:
        stderr_path = store.runs_dir / run_id / "session_tool_shell_stderr.txt"
        stderr_path.write_text(stderr, encoding="utf-8")
        stderr_artifact = store.register_artifact(
            run_id,
            "session_tool_shell_stderr",
            stderr_path,
            metadata={"tool_id": "shell", "stream": "stderr"},
            producer="session_tool",
            redaction_state="redacted",
            session_id=session_id,
        )
        stderr_artifact_id = stderr_artifact.id
    evidence = {
        **plan,
        "process_started": started,
        "process_id": process_id,
        "process_owner": result.owner,
        "process_status": result.status,
        "shell_execution_started": started,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "stdout_bytes": len(stdout.encode("utf-8")),
        "stderr_bytes": len(stderr.encode("utf-8")),
        "stdout_artifact_id": stdout_artifact_id,
        "stderr_artifact_id": stderr_artifact_id,
        "filesystem_modified": None,
        "active_repo_modified": None,
        "git_mutation_started": None,
        "permission_granting": False,
        "authority_granting": False,
        "read_only": False,
    }
    metadata_path = store.runs_dir / run_id / "session_tool_shell_metadata.json"
    metadata_path.write_text(json.dumps(sanitize_for_logging(evidence), indent=2, sort_keys=True), encoding="utf-8")
    metadata_artifact = store.register_artifact(
        run_id,
        "session_tool_shell_metadata",
        metadata_path,
        metadata={
            "tool_id": "shell",
            "exit_code": exit_code,
            "timed_out": timed_out,
            "process_started": started,
            "process_id": process_id,
            "process_status": result.status,
            "shell_execution_started": started,
        },
        producer="session_tool",
        redaction_state="redacted",
        session_id=session_id,
    )
    stdout_preview = stdout[:4000]
    stderr_preview = stderr[:4000]
    lines = [
        "Shell command executed.",
        f"Command: {plan['command']}",
        f"Cwd: {plan['cwd']}",
        f"Shell: {plan['shell_executable']}",
        f"Timeout: {plan['timeout_seconds']}s",
        f"Exit code: {exit_code if exit_code is not None else 'timed out'}",
        f"Timed out: {str(timed_out).lower()}",
        f"Metadata artifact: {metadata_artifact.id}",
    ]
    if stdout_artifact_id:
        lines.append(f"Stdout artifact: {stdout_artifact_id}")
    if stderr_artifact_id:
        lines.append(f"Stderr artifact: {stderr_artifact_id}")
    if stdout_preview:
        lines.extend(["", "Stdout:", stdout_preview])
    if stderr_preview:
        lines.extend(["", "Stderr:", stderr_preview])
    return "\n".join(lines)


def _resolve_session_tool_path(
    root: Path,
    store: SQLiteStore,
    session_id: str,
    arguments: dict[str, Any],
    path_key: str,
    *,
    action: str,
    allow_excluded: bool = False,
) -> Path:
    session = store.get_session(session_id)
    resolver = CwdResolver(project_root=root, context_excludes=load_config(root).context_excludes)
    try:
        return resolver.resolve_tool_path(
            session_cwd=session_cwd_from_metadata(session.metadata),
            call_cwd=str(arguments.get("cwd")) if arguments.get("cwd") not in {None, ""} else None,
            requested_path=str(arguments.get(path_key) or "."),
            action=action,
            allow_excluded=allow_excluded,
        )
    except CwdResolutionError as exc:
        raise _DeniedToolCall(exc.message, action=exc.action, target=exc.target, error_type=exc.error_type) from exc


def _resolve_git_diff_path(
    root: Path,
    store: SQLiteStore,
    session_id: str,
    arguments: dict[str, Any],
    *,
    allow_excluded: bool = False,
) -> Path:
    path_arg = arguments.get("path")
    return _resolve_session_tool_path(
        root,
        store,
        session_id,
        {"path": str(path_arg) if path_arg not in {None, ""} else ".", "cwd": arguments.get("cwd")},
        "path",
        action="git-diff",
        allow_excluded=allow_excluded,
    )


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


def _ls_tool(
    root: Path,
    store: SQLiteStore,
    session_id: str,
    arguments: dict[str, Any],
    *,
    allow_excluded: bool = False,
) -> str:
    target = _resolve_session_tool_path(
        root,
        store,
        session_id,
        {"path": str(arguments.get("path") or "."), "cwd": arguments.get("cwd")},
        "path",
        action="list",
        allow_excluded=allow_excluded,
    )
    limit = _bounded_limit(arguments.get("limit"), default=200, maximum=1000)
    include_hidden = bool(arguments.get("include_hidden") or False)
    if not target.exists():
        raise ValueError("Path does not exist.")
    entries: list[Path]
    if target.is_file():
        entries = [target]
    elif target.is_dir():
        entries = sorted(target.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower()))
    else:
        raise ValueError("Path is not a file or directory.")
    visible: list[dict[str, Any]] = []
    skipped = 0
    for entry in entries:
        try:
            rel = relative_to_project(root, entry)
        except ValueError:
            skipped += 1
            continue
        if not include_hidden and _path_has_hidden_part(rel):
            skipped += 1
            continue
        if is_excluded_relative(rel, load_config(root).context_excludes) and not allow_excluded:
            skipped += 1
            continue
        if is_secret_path(entry):
            skipped += 1
            continue
        visible.append(
            {
                "path": rel,
                "name": entry.name,
                "kind": "directory" if entry.is_dir() else "file" if entry.is_file() else "other",
                "size_bytes": entry.stat().st_size if entry.is_file() else None,
            }
        )
    truncated = len(visible) > limit
    selected = visible[:limit]
    payload = {
        "schema_version": "harness.session_tool_ls/v1",
        "target": relative_to_project(root, target),
        "entry_count": len(visible),
        "returned_count": len(selected),
        "skipped_count": skipped,
        "limit": limit,
        "truncated": truncated,
        "entries": selected,
    }
    return json.dumps(sanitize_for_logging(payload), indent=2, sort_keys=True, default=str)


def _find_tool(
    root: Path,
    store: SQLiteStore,
    session_id: str,
    arguments: dict[str, Any],
    excludes: list[str],
    *,
    allow_excluded: bool = False,
) -> str:
    query = str(arguments.get("query") or "").strip()
    if not query:
        raise ValueError("Missing query.")
    base = _resolve_session_tool_path(
        root,
        store,
        session_id,
        {"path": str(arguments.get("path") or "."), "cwd": arguments.get("cwd")},
        "path",
        action="find",
        allow_excluded=allow_excluded,
    )
    limit = _bounded_limit(arguments.get("limit"), default=100, maximum=1000)
    include_hidden = bool(arguments.get("include_hidden") or False)
    files = _project_files(root, excludes, start=relative_to_project(root, base), allow_excluded=allow_excluded)
    terms = _find_terms(query)
    matches: list[dict[str, Any]] = []
    skipped_hidden = 0
    for rel in files:
        if not include_hidden and _path_has_hidden_part(rel):
            skipped_hidden += 1
            continue
        haystack = rel.lower()
        name = Path(rel).name.lower()
        if all(term in haystack or term in name for term in terms):
            matches.append({"path": rel, "name": Path(rel).name})
    truncated = len(matches) > limit
    selected = matches[:limit]
    payload = {
        "schema_version": "harness.session_tool_find/v1",
        "query": query,
        "base": relative_to_project(root, base),
        "match_count": len(matches),
        "returned_count": len(selected),
        "skipped_hidden_count": skipped_hidden,
        "limit": limit,
        "truncated": truncated,
        "matches": selected,
        "content_searched": False,
    }
    return json.dumps(sanitize_for_logging(payload), indent=2, sort_keys=True, default=str)


def _project_files(root: Path, excludes: list[str], *, start: str = ".", allow_excluded: bool = False) -> list[str]:
    start_path = _resolve_allowed_path(root, start, excludes, action="list", allow_excluded=allow_excluded)
    candidates = [start_path] if start_path.is_file() else [path for path in start_path.rglob("*") if path.is_file()]
    files: list[str] = []
    for path in sorted(candidates):
        try:
            rel = relative_to_project(root, path)
        except ValueError:
            continue
        if is_excluded_relative(rel, excludes) and not allow_excluded:
            continue
        if is_secret_path(path):
            continue
        files.append(rel)
    return files


def _bounded_limit(value: Any, *, default: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(parsed, maximum))


def _path_has_hidden_part(relative_path: str) -> bool:
    return any(part.startswith(".") for part in Path(relative_path).parts if part not in {"", "."})


def _find_terms(query: str) -> list[str]:
    terms = [term for term in re.split(r"[^A-Za-z0-9_.-]+", query.lower()) if term]
    return terms or [query.lower()]


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
