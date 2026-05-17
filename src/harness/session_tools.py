from __future__ import annotations

import hashlib
import json
import re
import subprocess
import urllib.error
import urllib.request
from datetime import datetime, timezone
from enum import Enum
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse

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
WEB_FETCH_MAX_RESPONSE_BYTES = 5 * 1024 * 1024
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
                "Evidence must state that active workspace mutation, git mutation, and apply are disabled.",
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
            description="Plan a web search through an external-network policy gate.",
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
                "Search execution is approval-required and requires an explicit configured search endpoint.",
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
            safety_notes=disabled_notes + ["MCP calls must produce the same permission and evidence records as built-ins."],
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
    if tool_id not in {"web-fetch", "web-search", "repo-clone", "mcp-resource"} and _has_allowed_permission(store, session_id, tool_id, action, target, descriptor.boundary_kind):
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
        and tool_id not in {"patch", "direct-write", "docker-test", "web-fetch", "web-search", "repo-clone"}
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


def _tool_policy_evidence(tool_id: str, *, descriptor: SessionToolDescriptor) -> dict[str, Any]:
    if tool_id in {"read", "glob", "grep"}:
        return {
            "policy_boundary": {
                "kind": "project_read_only",
                "boundary_kind": descriptor.boundary_kind.value,
                "source": "session_tool_read_glob_grep",
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
        "read": "read",
        "glob": "list",
        "grep": "search",
        "artifact-read": "artifact-read",
        "lsp-diagnostics": "lsp-diagnostics",
        "lsp-symbols": "lsp-symbols",
        "mcp-resource": "mcp-resource",
        "policy-explain": "policy-explain",
        "repo-clone": "repo-clone",
        "repo-overview": "repo-overview",
        "skill-load": "skill-load",
        "web-fetch": "web-fetch",
        "web-search": "web-search",
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
    if tool_id == "lsp-diagnostics":
        return str(arguments.get("path") or ".")
    if tool_id == "lsp-symbols":
        return str(arguments.get("path") or ".")
    if tool_id == "mcp-resource":
        return f"{arguments.get('server') or ''}:{arguments.get('uri') or ''}"
    if tool_id == "policy-explain":
        return f"{arguments.get('subject_kind') or 'session'}:{arguments.get('subject_id') or ''}"
    if tool_id == "repo-clone":
        return str(arguments.get("repository") or arguments.get("url") or "")
    if tool_id == "repo-overview":
        return str(arguments.get("path") or arguments.get("repository") or ".")
    if tool_id == "skill-load":
        return str(arguments.get("skill") or arguments.get("name") or "")
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
    if tool_id == "web-fetch":
        plan_payload = _web_fetch_plan_values(root, arguments)
        response_payload = _execute_web_fetch(plan_payload)
        content = str(sanitize_for_logging(response_payload["content"]))
        content_path = store.runs_dir / run_id / "session_tool_web_fetch_content.txt"
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
            },
            producer="session_tool",
            redaction_state="redacted",
            session_id=session_id,
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
        search_payload = _execute_web_search(plan_payload)
        results_text = str(sanitize_for_logging(search_payload["output"]))
        results_path = store.runs_dir / run_id / "session_tool_web_search_results.txt"
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
            },
            producer="session_tool",
            redaction_state="redacted",
            session_id=session_id,
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
        clone_payload = _execute_repo_clone(plan_payload)
        metadata_payload = {
            **plan_payload,
            **clone_payload,
            "network_called": True,
            "clone_executed": clone_payload["status"] == "cloned",
            "fetch_executed": clone_payload["status"] == "refreshed",
            "external_cache_used": True,
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
    if tool_id == "lsp-diagnostics":
        return _lsp_diagnostics_tool(root, arguments, excludes, allow_excluded=allow_excluded)
    if tool_id == "lsp-symbols":
        return _lsp_symbols_tool(root, arguments, excludes, allow_excluded=allow_excluded)
    if tool_id == "mcp-resource":
        return _mcp_resource_tool(store, root, session_id, arguments, run_id=run_id)
    if tool_id == "policy-explain":
        subject_kind = str(arguments.get("subject_kind") or "session")
        subject_id = arguments.get("subject_id")
        return _policy_explanation(store, project_root, session_id, subject_kind, str(subject_id) if subject_id else None)
    if tool_id == "repo-overview":
        return _repo_overview(root, arguments, excludes, allow_excluded=allow_excluded)
    if tool_id == "skill-load":
        return _skill_load_tool(store, root, session_id, arguments, run_id=run_id)
    raise KeyError(f"Session tool is not executable in Phase 4A: {tool_id}")


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
            "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
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
        "path": skill["path"],
        "skill_file_path": skill["skill_file_path"],
        "base_dir_uri": skill["base_dir_uri"],
        "content_bytes": len(content.encode("utf-8")),
        "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "content_artifact_id": content_artifact.id,
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
        "path": relative_to_project(project_root, path),
        "skill_file": str(skill_file),
        "skill_file_path": relative_to_project(project_root, skill_file),
        "base_dir_uri": path.resolve().as_uri() if path.is_dir() else skill_file.parent.resolve().as_uri(),
    }


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
        "content_bytes": len(content.encode("utf-8")),
        "content_artifact_id": content_artifact.id,
        "cached_only": True,
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
    }


def _repo_overview(root: Path, arguments: dict[str, Any], excludes: list[str], *, allow_excluded: bool = False) -> str:
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
        target = _resolve_allowed_path(root, path_arg, excludes, action="repo-overview", allow_excluded=allow_excluded)
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


def _validate_docker_test_plan(root: Path, arguments: dict[str, Any]) -> str:
    _command, _cwd, target = _docker_test_plan_values(root, arguments)
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
    endpoint = str(cfg.search_endpoint_url or "").strip()
    if not endpoint:
        raise ValueError("Web search endpoint is not configured by project web_tools policy.")
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
    return {
        "schema_version": "harness.session_tool_web_search_plan/v1",
        "query": query,
        "target": query,
        "num_results": num_results,
        "search_type": search_type,
        "livecrawl": livecrawl,
        "context_max_characters": context_max_characters,
        "allowed_domains": effective_domains,
        "provider": "configured_http",
        "endpoint": endpoint,
        "endpoint_target": endpoint_target,
        "requires_network": True,
        "network_called": False,
        "search_executed": False,
        "results_artifact_id": None,
        "notes": [
            "Search execution must persist provider metadata, result hashes, redaction state, and result artifacts before display.",
        ],
    }


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
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            status_code = int(getattr(response, "status", 200))
            headers = {str(key).lower(): str(value) for key, value in response.headers.items()}
            body = response.read(WEB_FETCH_MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        status_code = int(exc.code)
        headers = {str(key).lower(): str(value) for key, value in exc.headers.items()}
        body = exc.read(WEB_FETCH_MAX_RESPONSE_BYTES + 1)
    except urllib.error.URLError as exc:
        raise ValueError(f"Web search failed: {exc.reason}") from exc
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
