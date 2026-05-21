from __future__ import annotations

import json
import hmac
import hashlib
import mimetypes
import os
import re
import secrets
import sqlite3
import subprocess
import struct
import sys
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from harness import __version__
from harness.command_catalog import build_command_catalog, command_action_unsupported
from harness.config import load_config
from harness.event_broker import reset_event_broker, subscribe_global_events, subscribe_store_events
from harness.memory.sqlite_store import (
    SESSION_SCHEMA_REPAIR_MESSAGE,
    SQLiteStore,
    is_missing_session_schema_error,
)
from harness.model_catalog import catalog_projection_evidence, list_model_catalog, list_provider_catalog, validate_model_selection
from harness.models import (
    EventStreamType,
    RedactionState,
    SessionMessageRole,
    SessionPartKind,
    SessionPermissionSource,
    SessionPermissionStatus,
    SessionStatus,
)
from harness.operator_loop import session_operator_status_projection
from harness.paths import is_excluded_relative, relative_to_project, resolve_project_root, resolve_under_project
from harness.process_supervisor import reset_process_supervisor
from harness.security import assert_not_secret_path, is_secret_path, redact_secret_text, sanitize_for_logging
from harness.session_cwd import CwdResolutionError, cwd_recovery_message, session_cwd_payload
from harness.session_replay import build_session_replay_projection
from harness.session_runtime import (
    SessionPromptQueuePolicy,
    SessionPromptRequest,
    SessionRuntimeManager,
    SessionRuntimePhase,
    reset_session_runtime_state,
)
from harness.session_share import build_local_session_share_snapshot, hosted_share_unsupported
from harness.session_tools import (
    build_session_approval_card,
    execute_session_tool,
    pending_session_tool_call_from_permission,
    persist_session_tool_denial,
    session_tool_catalog_projection,
    session_planning_mode_projection,
)
from harness.task_operator_bridge import apply_operator_task_permission_resolution
from harness.tui import build_tui_settings_catalog
from harness.workspace_catalog import build_workspace_catalog, build_workspace_clients_projection, workspace_action_unsupported


LOCAL_SERVER_SCHEMA_VERSION = "harness.local_server/v1"
OPENAPI_SCHEMA_VERSION = "harness.local_server.openapi/v1"
LOCAL_SERVER_ERROR_SCHEMA_VERSION = "harness.local_server_error/v1"
DEFAULT_MAX_REQUEST_BODY_BYTES = 1_048_576
DEFAULT_CORS_ORIGIN = "http://127.0.0.1"


class LocalServerHTTPError(ValueError):
    def __init__(self, status: HTTPStatus, error_code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.error_code = error_code
        self.message = message


def generate_server_token() -> str:
    return "harness_" + secrets.token_urlsafe(24)


def _normalize_opencode_session_path(path: str) -> str:
    if path == "/session":
        return "/sessions"
    if path.startswith("/session/"):
        return "/sessions/" + path.removeprefix("/session/")
    return path


def _add_opencode_session_aliases(spec: dict[str, Any]) -> dict[str, Any]:
    paths = spec.get("paths", {})
    for path, operations in list(paths.items()):
        if not path.startswith("/sessions"):
            continue
        alias_path = path.replace("/sessions", "/session", 1)
        alias_operations = dict(operations)
        alias_operations["x-harness-alias-for"] = path
        paths.setdefault(alias_path, alias_operations)
    return spec


def build_openapi_spec(*, server_url: str = "http://127.0.0.1:8765") -> dict[str, Any]:
    bearer = [{"bearerAuth": []}]
    spec = {
        "openapi": "3.1.0",
        "info": {
            "title": "Harness Local Server",
            "version": "0.1.0",
            "x-harness-schema-version": OPENAPI_SCHEMA_VERSION,
            "description": (
                "Local Harness API backed by the same session/catalog store used by CLI and TUI. "
                "Write routes persist session records only; they do not start execution. "
                "Permission replies are explicit, scoped permission decisions."
            ),
        },
        "servers": [{"url": server_url}],
        "components": {
            "securitySchemes": {
                "bearerAuth": {
                    "type": "http",
                    "scheme": "bearer",
                    "description": "Local token supplied when starting `harness serve`.",
                }
            },
            "schemas": {
                "LocalServerError": {
                    "type": "object",
                    "required": ["schema_version", "ok", "error", "error_code", "status"],
                    "properties": {
                        "schema_version": {"const": LOCAL_SERVER_ERROR_SCHEMA_VERSION},
                        "ok": {"const": False},
                        "error": {"type": "string"},
                        "error_code": {"type": "string"},
                        "status": {"type": "integer"},
                        "permission_granting": {"const": False},
                    },
                }
            },
        },
        "paths": {
            "/health": {"get": {"summary": "Health check", "security": bearer, "responses": _json_response()}},
            "/event": {
                "get": {
                    "summary": "OpenCode-compatible persisted event SSE stream",
                    "security": bearer,
                    "responses": _sse_response(),
                }
            },
            "/global/health": {"get": {"summary": "OpenCode-compatible global health projection", "security": bearer, "responses": _json_response()}},
            "/global/event": {
                "get": {
                    "summary": "OpenCode-compatible global persisted event SSE stream",
                    "security": bearer,
                    "responses": _sse_response(),
                }
            },
            "/global/config": {"get": {"summary": "OpenCode-compatible sanitized global config projection", "security": bearer, "responses": _json_response()}},
            "/global/dispose": {"post": {"summary": "Fail-closed placeholder for global dispose", "security": bearer, "responses": _json_response(status="501")}},
            "/global/upgrade": {"post": {"summary": "Fail-closed placeholder for global upgrade", "security": bearer, "responses": _json_response(status="501")}},
            "/server/lifecycle": {"get": {"summary": "Inspect local server lifecycle capabilities without mutating process state", "security": bearer, "responses": _json_response()}},
            "/server/mdns": {"get": {"summary": "Inspect mDNS advertisement status without broadcasting on the LAN", "security": bearer, "responses": _json_response()}},
            "/server/dispose": {"post": {"summary": "Fail-closed placeholder for local server dispose", "security": bearer, "responses": _json_response(status="501")}},
            "/providers": {"get": {"summary": "List provider metadata", "security": bearer, "responses": _json_response()}},
            "/models": {"get": {"summary": "List model metadata", "security": bearer, "responses": _json_response()}},
            "/models/validate": {"get": {"summary": "Validate a model ref without provider execution", "security": bearer, "responses": _json_response()}},
            "/provider": {"get": {"summary": "OpenCode-compatible provider metadata list", "security": bearer, "responses": _json_response()}},
            "/provider/auth": {"get": {"summary": "OpenCode-compatible provider auth method projection without secrets", "security": bearer, "responses": _json_response()}},
            "/provider/{provider_id}/oauth/authorize": {
                "post": {
                    "summary": "Fail-closed placeholder for provider OAuth authorization",
                    "security": bearer,
                    "parameters": [_path_param("provider_id")],
                    "responses": _json_response(status="501"),
                }
            },
            "/provider/{provider_id}/oauth/callback": {
                "post": {
                    "summary": "Fail-closed placeholder for provider OAuth callback",
                    "security": bearer,
                    "parameters": [_path_param("provider_id")],
                    "responses": _json_response(status="501"),
                }
            },
            "/auth/{provider_id}": {
                "put": {
                    "summary": "Fail-closed placeholder for setting provider auth credentials",
                    "security": bearer,
                    "parameters": [_path_param("provider_id")],
                    "responses": _json_response(status="501"),
                },
                "delete": {
                    "summary": "Fail-closed placeholder for removing provider auth credentials",
                    "security": bearer,
                    "parameters": [_path_param("provider_id")],
                    "responses": _json_response(status="501"),
                },
            },
            "/log": {"post": {"summary": "Fail-closed placeholder for client log ingestion", "security": bearer, "responses": _json_response(status="501")}},
            "/api/provider": {"get": {"summary": "OpenCode v2-compatible provider metadata list", "security": bearer, "responses": _json_response()}},
            "/api/provider/{provider_id}": {
                "get": {
                    "summary": "OpenCode v2-compatible provider metadata lookup",
                    "security": bearer,
                    "parameters": [_path_param("provider_id")],
                    "responses": _json_response(),
                }
            },
            "/api/model": {"get": {"summary": "OpenCode v2-compatible model metadata list", "security": bearer, "responses": _json_response()}},
            "/api/model/validate": {"get": {"summary": "OpenCode v2-compatible model selection validation", "security": bearer, "responses": _json_response()}},
            "/config/providers": {"get": {"summary": "OpenCode-compatible configured provider projection", "security": bearer, "responses": _json_response()}},
            "/config": {
                "get": {"summary": "Read sanitized project config metadata", "security": bearer, "responses": _json_response()},
                "patch": {"summary": "Fail-closed placeholder for config updates", "security": bearer, "responses": _json_response(status="501")},
            },
            "/path": {"get": {"summary": "OpenCode-compatible local path projection", "security": bearer, "responses": _json_response()}},
            "/project": {"get": {"summary": "OpenCode-compatible project list projection", "security": bearer, "responses": _json_response()}},
            "/project/current": {"get": {"summary": "OpenCode-compatible current project projection", "security": bearer, "responses": _json_response()}},
            "/project/git/init": {"post": {"summary": "Fail-closed placeholder for git init", "security": bearer, "responses": _json_response(status="501")}},
            "/project/{project_id}": {
                "patch": {
                    "summary": "Fail-closed placeholder for project metadata updates",
                    "security": bearer,
                    "parameters": [_path_param("project_id")],
                    "responses": _json_response(status="501"),
                }
            },
            "/vcs": {"get": {"summary": "OpenCode-compatible VCS metadata projection", "security": bearer, "responses": _json_response()}},
            "/vcs/status": {"get": {"summary": "OpenCode-compatible changed file status projection", "security": bearer, "responses": _json_response()}},
            "/vcs/diff": {"get": {"summary": "OpenCode-compatible bounded VCS diff projection", "security": bearer, "responses": _json_response()}},
            "/vcs/diff/raw": {"get": {"summary": "OpenCode-compatible raw VCS diff projection", "security": bearer, "responses": _json_response()}},
            "/vcs/apply": {"post": {"summary": "Fail-closed placeholder for VCS patch apply", "security": bearer, "responses": _json_response(status="501")}},
            "/agent": {"get": {"summary": "OpenCode-compatible alias for project agents", "security": bearer, "responses": _json_response()}},
            "/skill": {"get": {"summary": "OpenCode-compatible alias for skills", "security": bearer, "responses": _json_response()}},
            "/lsp": {"get": {"summary": "OpenCode-compatible LSP status projection", "security": bearer, "responses": _json_response()}},
            "/formatter": {"get": {"summary": "OpenCode-compatible formatter status projection", "security": bearer, "responses": _json_response()}},
            "/agents": {"get": {"summary": "List imported project agents", "security": bearer, "responses": _json_response()}},
            "/artifacts": {"get": {"summary": "List artifact metadata", "security": bearer, "responses": _json_response()}},
            "/find": {"get": {"summary": "OpenCode-compatible bounded text search across project files", "security": bearer, "responses": _json_response()}},
            "/find/file": {"get": {"summary": "OpenCode-compatible file-name search without loading file contents", "security": bearer, "responses": _json_response()}},
            "/find/symbol": {"get": {"summary": "OpenCode-compatible static symbol search without launching LSP servers", "security": bearer, "responses": _json_response()}},
            "/file": {"get": {"summary": "OpenCode-compatible alias for project file metadata", "security": bearer, "responses": _json_response()}},
            "/file/content": {"get": {"summary": "OpenCode-compatible alias for redacted project file preview", "security": bearer, "responses": _json_response()}},
            "/file/status": {"get": {"summary": "OpenCode-compatible alias for changed file status without file contents", "security": bearer, "responses": _json_response()}},
            "/files": {"get": {"summary": "List project file metadata", "security": bearer, "responses": _json_response()}},
            "/files/content": {"get": {"summary": "Read a redacted project file preview", "security": bearer, "responses": _json_response()}},
            "/files/status": {"get": {"summary": "List changed file status without file contents", "security": bearer, "responses": _json_response()}},
            "/references": {"get": {"summary": "List configured named references without loading contents", "security": bearer, "responses": _json_response()}},
            "/instructions": {"get": {"summary": "Discover project instruction files without loading contents", "security": bearer, "responses": _json_response()}},
            "/symbols": {"get": {"summary": "List static code symbols without launching LSP servers", "security": bearer, "responses": _json_response()}},
            "/lsp/diagnostics": {"get": {"summary": "List configured LSP diagnostics projection without launching servers", "security": bearer, "responses": _json_response()}},
            "/formatters": {"get": {"summary": "List formatter configuration without running formatters", "security": bearer, "responses": _json_response()}},
            "/mcp/status": {"get": {"summary": "List MCP server configuration without connecting", "security": bearer, "responses": _json_response()}},
            "/mcp": {
                "get": {"summary": "OpenCode-compatible MCP status projection without connecting", "security": bearer, "responses": _json_response()},
                "post": {"summary": "Fail-closed placeholder for adding an MCP server", "security": bearer, "responses": _json_response(status="501")},
            },
            "/mcp/{name}/auth": {
                "post": {"summary": "Fail-closed placeholder for MCP OAuth start", "security": bearer, "parameters": [_path_param("name")], "responses": _json_response(status="501")},
                "delete": {"summary": "Fail-closed placeholder for MCP OAuth removal", "security": bearer, "parameters": [_path_param("name")], "responses": _json_response(status="501")},
            },
            "/mcp/{name}/auth/callback": {"post": {"summary": "Fail-closed placeholder for MCP OAuth callback", "security": bearer, "parameters": [_path_param("name")], "responses": _json_response(status="501")}},
            "/mcp/{name}/auth/authenticate": {"post": {"summary": "Fail-closed placeholder for MCP OAuth browser authentication", "security": bearer, "parameters": [_path_param("name")], "responses": _json_response(status="501")}},
            "/mcp/{name}/connect": {"post": {"summary": "Fail-closed placeholder for MCP connect", "security": bearer, "parameters": [_path_param("name")], "responses": _json_response(status="501")}},
            "/mcp/{name}/disconnect": {"post": {"summary": "Fail-closed placeholder for MCP disconnect", "security": bearer, "parameters": [_path_param("name")], "responses": _json_response(status="501")}},
            "/mcp/resources": {"get": {"summary": "List cached MCP resources without connecting", "security": bearer, "responses": _json_response()}},
            "/plugins": {"get": {"summary": "List plugin metadata and origin without loading plugins", "security": bearer, "responses": _json_response()}},
            "/skills": {"get": {"summary": "List skill metadata and origin without loading skills", "security": bearer, "responses": _json_response()}},
            "/web/tools": {"get": {"summary": "List web fetch/search policy without network access", "security": bearer, "responses": _json_response()}},
            "/extensions/status": {"get": {"summary": "Summarize MCP/plugin/skill/web extensibility policy without side effects", "security": bearer, "responses": _json_response()}},
            "/web/client": {"get": {"summary": "Inspect web client availability without serving static assets", "security": bearer, "responses": _json_response()}},
            "/web/open": {"post": {"summary": "Fail-closed placeholder for opening the web client", "security": bearer, "responses": _json_response(status="501")}},
            "/worktrees": {
                "get": {"summary": "List git worktree metadata without creating, removing, or resetting worktrees", "security": bearer, "responses": _json_response()},
                "post": {"summary": "Fail-closed worktree create plan without git mutation", "security": bearer, "responses": _json_response(status="501")},
            },
            "/worktrees/create": {"post": {"summary": "Fail-closed worktree create plan without git mutation", "security": bearer, "responses": _json_response(status="501")}},
            "/worktrees/remove": {"post": {"summary": "Fail-closed worktree remove plan without git mutation", "security": bearer, "responses": _json_response(status="501")}},
            "/worktrees/reset": {"post": {"summary": "Fail-closed worktree reset plan without git mutation", "security": bearer, "responses": _json_response(status="501")}},
            "/dev-loop/status": {"get": {"summary": "Summarize PTY, worktree, snapshot, and revert readiness without mutation", "security": bearer, "responses": _json_response()}},
            "/workspaces": {"get": {"summary": "List current workspace metadata without scanning or attaching to other projects", "security": bearer, "responses": _json_response()}},
            "/workspaces/clients": {"get": {"summary": "List workspace client attachment metadata without registering clients", "security": bearer, "responses": _json_response()}},
            "/workspaces/attach": {"post": {"summary": "Fail-closed placeholder for remote workspace attach", "security": bearer, "responses": _json_response(status="501")}},
            "/workspaces/sync": {"post": {"summary": "Fail-closed placeholder for workspace sync/replay", "security": bearer, "responses": _json_response(status="501")}},
            "/workspaces/steal": {"post": {"summary": "Fail-closed placeholder for stealing workspace client ownership", "security": bearer, "responses": _json_response(status="501")}},
            "/workspaces/dispose": {"post": {"summary": "Fail-closed placeholder for disposing remote workspace clients", "security": bearer, "responses": _json_response(status="501")}},
            "/sync/start": {"post": {"summary": "Fail-closed placeholder for workspace sync start", "security": bearer, "responses": _json_response(status="501")}},
            "/sync/replay": {"post": {"summary": "Fail-closed placeholder for workspace sync replay", "security": bearer, "responses": _json_response(status="501")}},
            "/sync/steal": {"post": {"summary": "Fail-closed placeholder for workspace sync steal", "security": bearer, "responses": _json_response(status="501")}},
            "/sync/history": {"post": {"summary": "Return empty sync history projection without registering clients", "security": bearer, "responses": _json_response(status="200")}},
            "/experimental/workspace/adapter": {"get": {"summary": "List experimental workspace adapter metadata", "security": bearer, "responses": _json_response()}},
            "/experimental/workspace": {
                "get": {"summary": "OpenCode-compatible experimental workspace list projection", "security": bearer, "responses": _json_response()},
                "post": {"summary": "Fail-closed placeholder for workspace creation", "security": bearer, "responses": _json_response(status="501")},
            },
            "/experimental/workspace/sync-list": {"post": {"summary": "Fail-closed placeholder for workspace sync-list", "security": bearer, "responses": _json_response(status="501")}},
            "/experimental/workspace/status": {"get": {"summary": "OpenCode-compatible workspace status projection", "security": bearer, "responses": _json_response()}},
            "/experimental/workspace/warp": {"post": {"summary": "Fail-closed placeholder for workspace warp", "security": bearer, "responses": _json_response(status="501")}},
            "/pty": {
                "get": {"summary": "OpenCode-compatible PTY session metadata without starting processes", "security": bearer, "responses": _json_response()},
                "post": {"summary": "Fail-closed placeholder for PTY creation", "security": bearer, "responses": _json_response(status="501")},
            },
            "/pty/{pty_id}": {
                "get": {"summary": "OpenCode-compatible PTY detail projection without process attachment", "security": bearer, "parameters": [_path_param("pty_id")], "responses": _json_response()},
                "put": {"summary": "Fail-closed placeholder for PTY update", "security": bearer, "parameters": [_path_param("pty_id")], "responses": _json_response(status="501")},
                "delete": {"summary": "Fail-closed placeholder for PTY removal", "security": bearer, "parameters": [_path_param("pty_id")], "responses": _json_response(status="501")},
            },
            "/pty/{pty_id}/connect-token": {"post": {"summary": "Fail-closed placeholder for PTY websocket connect-token", "security": bearer, "parameters": [_path_param("pty_id")], "responses": _json_response(status="501")}},
            "/pty/{pty_id}/connect": {"get": {"summary": "Fail-closed placeholder for PTY websocket connect", "security": bearer, "parameters": [_path_param("pty_id")], "responses": _json_response(status="501")}},
            "/pty/{pty_id}/restoration": {"get": {"summary": "Explain PTY terminal output restoration readiness without reading live streams", "security": bearer, "parameters": [_path_param("pty_id")], "responses": _json_response()}},
            "/pty/{pty_id}/tab": {"get": {"summary": "Project one PTY as a terminal tab without starting processes", "security": bearer, "parameters": [_path_param("pty_id")], "responses": _json_response()}},
            "/pty/sessions": {"get": {"summary": "List managed PTY session metadata without starting processes", "security": bearer, "responses": _json_response()}},
            "/pty/shells": {"get": {"summary": "List shell candidate metadata without probing shells", "security": bearer, "responses": _json_response()}},
            "/pty/restoration": {"get": {"summary": "Explain PTY terminal output restoration readiness without reading live streams", "security": bearer, "responses": _json_response()}},
            "/pty/tabs": {"get": {"summary": "List terminal tab projections without starting PTY processes", "security": bearer, "responses": _json_response()}},
            "/distribution/status": {"get": {"summary": "Inspect distribution and packaging status without modifying the Python environment", "security": bearer, "responses": _json_response()}},
            "/distribution/packaging-smoke": {"get": {"summary": "Inspect packaging smoke plan without building artifacts", "security": bearer, "responses": _json_response()}},
            "/distribution/packaging-smoke/run": {"post": {"summary": "Fail-closed placeholder for packaging smoke execution", "security": bearer, "responses": _json_response(status="501")}},
            "/desktop/status": {"get": {"summary": "Inspect desktop packaging status without launching desktop clients", "security": bearer, "responses": _json_response()}},
            "/desktop/launch": {"post": {"summary": "Fail-closed placeholder for desktop launch", "security": bearer, "responses": _json_response(status="501")}},
            "/version/check": {"get": {"summary": "Return offline version-check contract without calling the network", "security": bearer, "responses": _json_response()}},
            "/settings/tui": {"get": {"summary": "List supported TUI themes, keybindings, and settings without mutating preferences", "security": bearer, "responses": _json_response()}},
            "/tui/append-prompt": {"post": {"summary": "Record fail-closed TUI append-prompt intent", "security": bearer, "responses": _json_response(status="202")}},
            "/tui/open-help": {"post": {"summary": "Record fail-closed TUI open-help intent", "security": bearer, "responses": _json_response(status="202")}},
            "/tui/open-sessions": {"post": {"summary": "Record fail-closed TUI open-sessions intent", "security": bearer, "responses": _json_response(status="202")}},
            "/tui/open-themes": {"post": {"summary": "Record fail-closed TUI open-themes intent", "security": bearer, "responses": _json_response(status="202")}},
            "/tui/open-models": {"post": {"summary": "Record fail-closed TUI open-models intent", "security": bearer, "responses": _json_response(status="202")}},
            "/tui/submit-prompt": {"post": {"summary": "Record fail-closed TUI submit-prompt intent", "security": bearer, "responses": _json_response(status="202")}},
            "/tui/clear-prompt": {"post": {"summary": "Record fail-closed TUI clear-prompt intent", "security": bearer, "responses": _json_response(status="202")}},
            "/tui/execute-command": {"post": {"summary": "Record fail-closed TUI execute-command intent", "security": bearer, "responses": _json_response(status="202")}},
            "/tui/show-toast": {"post": {"summary": "Record fail-closed TUI show-toast intent", "security": bearer, "responses": _json_response(status="202")}},
            "/tui/publish": {"post": {"summary": "Record fail-closed TUI publish intent", "security": bearer, "responses": _json_response(status="202")}},
            "/tui/select-session": {"post": {"summary": "Record fail-closed TUI select-session intent", "security": bearer, "responses": _json_response(status="202")}},
            "/tui/control/next": {"get": {"summary": "Inspect empty TUI control queue without blocking", "security": bearer, "responses": _json_response()}},
            "/tui/control/response": {"post": {"summary": "Record fail-closed TUI control response intent", "security": bearer, "responses": _json_response(status="202")}},
            "/command": {"get": {"summary": "OpenCode-compatible alias for command template discovery without execution", "security": bearer, "responses": _json_response()}},
            "/permission": {"get": {"summary": "OpenCode-compatible list of pending permission requests across sessions", "security": bearer, "responses": _json_response()}},
            "/permission/{permission_id}/reply": {
                "post": {
                    "summary": "OpenCode-compatible reply to a pending permission request",
                    "security": bearer,
                    "parameters": [_path_param("permission_id")],
                    "responses": _json_response(status="200"),
                }
            },
            "/question": {"get": {"summary": "OpenCode-compatible list of pending session questions", "security": bearer, "responses": _json_response()}},
            "/question/{question_id}/reply": {
                "post": {
                    "summary": "OpenCode-compatible append-only reply event for a session question",
                    "security": bearer,
                    "parameters": [_path_param("question_id")],
                    "responses": _json_response(status="200"),
                }
            },
            "/question/{question_id}/reject": {
                "post": {
                    "summary": "OpenCode-compatible append-only reject event for a session question",
                    "security": bearer,
                    "parameters": [_path_param("question_id")],
                    "responses": _json_response(status="200"),
                }
            },
            "/commands": {"get": {"summary": "Discover project command templates without executing them", "security": bearer, "responses": _json_response()}},
            "/tools": {"get": {"summary": "List Harness session tool descriptors with policy projections", "security": bearer, "responses": _json_response()}},
            "/commands/run": {"post": {"summary": "Fail-closed placeholder for user-defined command execution", "security": bearer, "responses": _json_response(status="501")}},
            "/pr/checkout": {"post": {"summary": "Fail-closed placeholder for PR checkout without network or git mutation", "security": bearer, "responses": _json_response(status="501")}},
            "/pr/run": {"post": {"summary": "Fail-closed placeholder for PR checkout and run without network, git mutation, or adapter execution", "security": bearer, "responses": _json_response(status="501")}},
            "/sessions": {
                "get": {"summary": "List sessions", "security": bearer, "responses": _json_response()},
                "post": {
                    "summary": "Create a persisted session without starting execution",
                    "security": bearer,
                    "responses": _json_response(status="201"),
                },
            },
            "/api/session": {"get": {"summary": "OpenCode v2-compatible session list projection", "security": bearer, "responses": _json_response()}},
            "/api/session/{session_id}/prompt": {
                "post": {
                    "summary": "OpenCode v2-compatible append-only prompt persistence without operator/provider execution",
                    "security": bearer,
                    "parameters": [_path_param("session_id")],
                    "responses": _json_response(status="201"),
                }
            },
            "/api/session/{session_id}/compact": {
                "post": {
                    "summary": "Fail-closed placeholder for OpenCode v2 session compaction",
                    "security": bearer,
                    "parameters": [_path_param("session_id")],
                    "responses": _json_response(status="501"),
                }
            },
            "/api/session/{session_id}/wait": {
                "post": {
                    "summary": "OpenCode v2-compatible wait projection without blocking on provider execution",
                    "security": bearer,
                    "parameters": [_path_param("session_id")],
                    "responses": _json_response(status="200"),
                }
            },
            "/api/session/{session_id}/context": {
                "get": {
                    "summary": "OpenCode v2-compatible session context projection",
                    "security": bearer,
                    "parameters": [_path_param("session_id")],
                    "responses": _json_response(),
                }
            },
            "/api/session/{session_id}/message": {
                "get": {
                    "summary": "OpenCode v2-compatible session message list projection",
                    "security": bearer,
                    "parameters": [_path_param("session_id")],
                    "responses": _json_response(),
                }
            },
            "/sessions/status": {
                "get": {
                    "summary": "Get aggregate lifecycle status for all sessions without starting execution",
                    "security": bearer,
                    "responses": _json_response(),
                }
            },
            "/sessions/{session_id}": {
                "get": {
                    "summary": "Get one session",
                    "security": bearer,
                    "parameters": [_path_param("session_id")],
                    "responses": _json_response(),
                },
                "patch": {
                    "summary": "Update mutable session metadata without changing historical messages or events",
                    "security": bearer,
                    "parameters": [_path_param("session_id")],
                    "responses": _json_response(status="200"),
                },
                "delete": {
                    "summary": "Archive a session by default; hard deletion is intentionally unsupported",
                    "security": bearer,
                    "parameters": [_path_param("session_id")],
                    "responses": _json_response(status="200"),
                },
            },
            "/sessions/{session_id}/events": {
                "get": {
                    "summary": "Replay persisted session events",
                    "security": bearer,
                    "parameters": [_path_param("session_id")],
                    "responses": _json_response(),
                }
            },
            "/sessions/{session_id}/status": {
                "get": {
                    "summary": "Inspect session lifecycle status and child sessions without starting execution",
                    "security": bearer,
                    "parameters": [_path_param("session_id")],
                    "responses": _json_response(),
                }
            },
            "/sessions/{session_id}/children": {
                "get": {
                    "summary": "List child sessions forked from this session without starting execution",
                    "security": bearer,
                    "parameters": [_path_param("session_id")],
                    "responses": _json_response(),
                }
            },
            "/sessions/{session_id}/fork": {
                "post": {
                    "summary": "Fork a session at an optional message without starting execution",
                    "security": bearer,
                    "parameters": [_path_param("session_id")],
                    "responses": _json_response(status="201"),
                }
            },
            "/sessions/{session_id}/summary": {
                "post": {
                    "summary": "Update mutable session summary and token rollups with an append-only event",
                    "security": bearer,
                    "parameters": [_path_param("session_id")],
                    "responses": _json_response(status="200"),
                }
            },
            "/sessions/{session_id}/summarize": {
                "post": {
                    "summary": "OpenCode-compatible summary route; updates provided summary without provider execution",
                    "security": bearer,
                    "parameters": [_path_param("session_id")],
                    "responses": _json_response(status="200"),
                }
            },
            "/sessions/{session_id}/abort": {
                "post": {
                    "summary": "Append a metadata-only session cancellation event without stopping processes",
                    "security": bearer,
                    "parameters": [_path_param("session_id")],
                    "responses": _json_response(status="200"),
                }
            },
            "/sessions/{session_id}/replay": {
                "get": {
                    "summary": "Replay append-only session events from a cursor without starting execution",
                    "security": bearer,
                    "parameters": [_path_param("session_id")],
                    "responses": _json_response(),
                }
            },
            "/sessions/{session_id}/messages": {
                "get": {
                    "summary": "List session messages and parts",
                    "security": bearer,
                    "parameters": [_path_param("session_id")],
                    "responses": _json_response(),
                },
                "post": {
                    "summary": "Append a persisted user message without starting execution",
                    "security": bearer,
                    "parameters": [_path_param("session_id")],
                    "responses": _json_response(status="201"),
                },
            },
            "/sessions/{session_id}/message": {
                "get": {
                    "summary": "OpenCode-compatible alias for listing session messages and parts",
                    "security": bearer,
                    "parameters": [_path_param("session_id")],
                    "responses": _json_response(),
                },
                "post": {
                    "summary": "OpenCode-compatible alias for appending a persisted user message without execution",
                    "security": bearer,
                    "parameters": [_path_param("session_id")],
                    "responses": _json_response(status="201"),
                },
            },
            "/sessions/{session_id}/messages/{message_id}/retract": {
                "post": {
                    "summary": "Append a message retraction event without mutating historical message rows",
                    "security": bearer,
                    "parameters": [_path_param("session_id"), _path_param("message_id")],
                    "responses": _json_response(status="200"),
                }
            },
            "/sessions/{session_id}/messages/{message_id}": {
                "get": {
                    "summary": "Read one persisted session message with its immutable parts",
                    "security": bearer,
                    "parameters": [_path_param("session_id"), _path_param("message_id")],
                    "responses": _json_response(status="200"),
                }
            },
            "/sessions/{session_id}/message/{message_id}": {
                "get": {
                    "summary": "OpenCode-compatible alias for reading one persisted session message",
                    "security": bearer,
                    "parameters": [_path_param("session_id"), _path_param("message_id")],
                    "responses": _json_response(status="200"),
                },
                "delete": {
                    "summary": "OpenCode-compatible message delete as append-only retraction",
                    "security": bearer,
                    "parameters": [_path_param("session_id"), _path_param("message_id")],
                    "responses": _json_response(status="200"),
                },
            },
            "/sessions/{session_id}/message/{message_id}/part/{part_id}": {
                "patch": {
                    "summary": "OpenCode-compatible part update as append-only correction",
                    "security": bearer,
                    "parameters": [_path_param("session_id"), _path_param("message_id"), _path_param("part_id")],
                    "responses": _json_response(status="200"),
                },
                "delete": {
                    "summary": "OpenCode-compatible part delete as append-only retraction",
                    "security": bearer,
                    "parameters": [_path_param("session_id"), _path_param("message_id"), _path_param("part_id")],
                    "responses": _json_response(status="200"),
                },
            },
            "/sessions/{session_id}/prompt_async": {
                "post": {
                    "summary": "Append a persisted user prompt asynchronously without waiting for execution",
                    "security": bearer,
                    "parameters": [_path_param("session_id")],
                    "responses": _json_response(status="202"),
                }
            },
            "/sessions/{session_id}/prompt": {
                "post": {
                    "summary": "Run a natural-language prompt through the shared Harness operator loop",
                    "security": bearer,
                    "parameters": [_path_param("session_id")],
                    "responses": _json_response(status="200"),
                }
            },
            "/sessions/{session_id}/command": {
                "post": {
                    "summary": "OpenCode-compatible fail-closed placeholder for session slash command execution",
                    "security": bearer,
                    "parameters": [_path_param("session_id")],
                    "responses": _json_response(status="501"),
                }
            },
            "/sessions/{session_id}/init": {
                "post": {
                    "summary": "Fail-closed placeholder for project/session initialization generation",
                    "security": bearer,
                    "parameters": [_path_param("session_id")],
                    "responses": _json_response(status="501"),
                }
            },
            "/sessions/{session_id}/shell": {
                "post": {
                    "summary": "Run the permissioned session shell tool through the session-tool gateway",
                    "security": bearer,
                    "parameters": [_path_param("session_id")],
                    "responses": _json_response(status="200"),
                }
            },
            "/sessions/{session_id}/tool": {
                "get": {
                    "summary": "List Harness session tool descriptors with policy projections",
                    "security": bearer,
                    "parameters": [_path_param("session_id")],
                    "responses": _json_response(status="200"),
                },
                "post": {
                    "summary": "Run a session tool through the same gateway used by CLI and chat",
                    "security": bearer,
                    "parameters": [_path_param("session_id")],
                    "responses": _json_response(status="200"),
                }
            },
            "/sessions/{session_id}/tools": {
                "get": {
                    "summary": "List Harness session tool descriptors with policy projections",
                    "security": bearer,
                    "parameters": [_path_param("session_id")],
                    "responses": _json_response(status="200"),
                },
                "post": {
                    "summary": "Run a session tool through the same gateway used by CLI and chat",
                    "security": bearer,
                    "parameters": [_path_param("session_id")],
                    "responses": _json_response(status="200"),
                },
            },
            "/sessions/{session_id}/parts/{part_id}/correct": {
                "post": {
                    "summary": "Append a part correction event without mutating historical part rows",
                    "security": bearer,
                    "parameters": [_path_param("session_id"), _path_param("part_id")],
                    "responses": _json_response(status="200"),
                }
            },
            "/sessions/{session_id}/permissions": {
                "get": {
                    "summary": "List session permission requests",
                    "security": bearer,
                    "parameters": [_path_param("session_id")],
                    "responses": _json_response(),
                }
            },
            "/sessions/{session_id}/permissions/snapshot": {
                "get": {
                    "summary": "Summarize session permission state for operator clients",
                    "security": bearer,
                    "parameters": [_path_param("session_id")],
                    "responses": _json_response(),
                }
            },
            "/sessions/{session_id}/permissions/{permission_id}/reply": {
                "post": {
                    "summary": "Reply to a pending session permission request without starting execution",
                    "security": bearer,
                    "parameters": [_path_param("session_id"), _path_param("permission_id")],
                    "responses": _json_response(status="200"),
                }
            },
            "/sessions/{session_id}/permissions/{permission_id}": {
                "post": {
                    "summary": "OpenCode-compatible alias for replying to a pending permission request",
                    "security": bearer,
                    "parameters": [_path_param("session_id"), _path_param("permission_id")],
                    "responses": _json_response(status="200"),
                }
            },
            "/sessions/{session_id}/approval/{approval_id}": {
                "post": {
                    "summary": "Approve, deny, or resume a pending Harness session approval",
                    "security": bearer,
                    "parameters": [_path_param("session_id"), _path_param("approval_id")],
                    "responses": _json_response(status="200"),
                }
            },
            "/sessions/{session_id}/todos": {
                "get": {
                    "summary": "List session-local todos without starting execution",
                    "security": bearer,
                    "parameters": [_path_param("session_id")],
                    "responses": _json_response(),
                },
                "post": {
                    "summary": "Append a session-local todo and persisted timeline event",
                    "security": bearer,
                    "parameters": [_path_param("session_id")],
                    "responses": _json_response(status="201"),
                },
            },
            "/sessions/{session_id}/todo": {
                "get": {
                    "summary": "OpenCode-compatible alias for listing session-local todos",
                    "security": bearer,
                    "parameters": [_path_param("session_id")],
                    "responses": _json_response(),
                }
            },
            "/sessions/{session_id}/questions": {
                "get": {
                    "summary": "List session-local questions from persisted parts without starting execution",
                    "security": bearer,
                    "parameters": [_path_param("session_id")],
                    "responses": _json_response(),
                },
                "post": {
                    "summary": "Append a session-local question and persisted timeline event",
                    "security": bearer,
                    "parameters": [_path_param("session_id")],
                    "responses": _json_response(status="201"),
                },
            },
            "/sessions/{session_id}/diffs": {
                "get": {
                    "summary": "List session-linked diff artifact metadata and bounded previews without applying changes",
                    "security": bearer,
                    "parameters": [_path_param("session_id")],
                    "responses": _json_response(),
                }
            },
            "/sessions/{session_id}/diff": {
                "get": {
                    "summary": "OpenCode-compatible alias for session-linked diff metadata",
                    "security": bearer,
                    "parameters": [_path_param("session_id")],
                    "responses": _json_response(),
                }
            },
            "/sessions/{session_id}/changed-files": {
                "get": {
                    "summary": "Summarize session-linked changed files without applying, reverting, or reading full contents",
                    "security": bearer,
                    "parameters": [_path_param("session_id")],
                    "responses": _json_response(),
                }
            },
            "/sessions/{session_id}/snapshots": {
                "get": {
                    "summary": "List per-message snapshot metadata without enabling revert",
                    "security": bearer,
                    "parameters": [_path_param("session_id")],
                    "responses": _json_response(),
                }
            },
            "/sessions/{session_id}/messages/{message_id}/snapshots": {
                "get": {
                    "summary": "List snapshot metadata for one message without enabling revert",
                    "security": bearer,
                    "parameters": [_path_param("session_id"), _path_param("message_id")],
                    "responses": _json_response(),
                }
            },
            "/sessions/{session_id}/revert-readiness": {
                "get": {
                    "summary": "Explain session revert readiness without mutating files",
                    "security": bearer,
                    "parameters": [_path_param("session_id")],
                    "responses": _json_response(),
                }
            },
            "/sessions/{session_id}/messages/{message_id}/revert-readiness": {
                "get": {
                    "summary": "Explain revert readiness for one message without mutating files",
                    "security": bearer,
                    "parameters": [_path_param("session_id"), _path_param("message_id")],
                    "responses": _json_response(),
                }
            },
            "/sessions/{session_id}/share": {
                "get": {
                    "summary": "Build a sanitized local-only session share snapshot without uploading data",
                    "security": bearer,
                    "parameters": [_path_param("session_id")],
                    "responses": _json_response(),
                },
                "post": {
                    "summary": "Fail-closed placeholder for hosted session sharing",
                    "security": bearer,
                    "parameters": [_path_param("session_id")],
                    "responses": _json_response(status="501"),
                },
                "delete": {
                    "summary": "Fail-closed placeholder for hosted session unsharing",
                    "security": bearer,
                    "parameters": [_path_param("session_id")],
                    "responses": _json_response(status="501"),
                },
            },
            "/sessions/{session_id}/revert": {
                "post": {
                    "summary": "Fail-closed placeholder for session revert; does not mutate files",
                    "security": bearer,
                    "parameters": [_path_param("session_id")],
                    "responses": _json_response(status="501"),
                }
            },
            "/sessions/{session_id}/unrevert": {
                "post": {
                    "summary": "Fail-closed placeholder for session unrevert; does not mutate files",
                    "security": bearer,
                    "parameters": [_path_param("session_id")],
                    "responses": _json_response(status="501"),
                }
            },
            "/sessions/{session_id}/apply-hunk": {
                "post": {
                    "summary": "Fail-closed placeholder for selected hunk apply; does not mutate files",
                    "security": bearer,
                    "parameters": [_path_param("session_id")],
                    "responses": _json_response(status="501"),
                }
            },
            "/sessions/{session_id}/mentions/resolve": {
                "post": {
                    "summary": "Resolve prompt mentions and persist the resolution event without loading contents",
                    "security": bearer,
                    "parameters": [_path_param("session_id")],
                    "responses": _json_response(status="201"),
                },
            },
            "/sessions/{session_id}/attachments": {
                "post": {
                    "summary": "Prepare metadata-only file attachments for a session",
                    "security": bearer,
                    "parameters": [_path_param("session_id")],
                    "responses": _json_response(status="201"),
                },
            },
            "/sessions/{session_id}/context/estimate": {
                "post": {
                    "summary": "Estimate composer context budget without loading referenced contents",
                    "security": bearer,
                    "parameters": [_path_param("session_id")],
                    "responses": _json_response(status="201"),
                },
            },
            "/sessions/{session_id}/events/stream": {
                "get": {
                    "summary": "Stream persisted session events as server-sent events",
                    "security": bearer,
                    "parameters": [_path_param("session_id")],
                    "responses": {
                        "200": {
                            "description": "SSE event stream",
                            "content": {"text/event-stream": {"schema": {"type": "string"}}},
                        }
                    },
                }
            },
            "/openapi.json": {"get": {"summary": "OpenAPI document", "security": bearer, "responses": _json_response()}},
        },
        "x-harness": {
            "permission_granting": False,
            "no_hidden_fallback": True,
            "authority": "local_persistence_no_execution",
            "max_request_body_bytes": DEFAULT_MAX_REQUEST_BODY_BYTES,
            "cors_default_origin": DEFAULT_CORS_ORIGIN,
        },
    }
    return _add_opencode_session_aliases(spec)


def serve_local_http(project_root: Path, *, host: str, port: int, token: str) -> None:
    create_local_http_server(project_root, host=host, port=port, token=token).serve_forever()


class HarnessLocalHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, server_address: tuple[str, int], handler_class: type[BaseHTTPRequestHandler], *, project_root: Path) -> None:
        super().__init__(server_address, handler_class)
        self.project_root = Path(project_root).resolve()
        self._harness_closed = False

    def server_close(self) -> None:
        if not self._harness_closed:
            self._harness_closed = True
            reset_event_broker(self.project_root)
            reset_session_runtime_state(self.project_root)
            reset_process_supervisor(self.project_root)
        super().server_close()


def create_local_http_server(
    project_root: Path,
    *,
    host: str,
    port: int,
    token: str,
    max_body_bytes: int | None = None,
    cors_origin: str | None = None,
) -> ThreadingHTTPServer:
    project_root = resolve_project_root(project_root)
    store = SQLiteStore(project_root)
    cfg = load_config(project_root)
    request_body_limit = max(0, int(max_body_bytes if max_body_bytes is not None else _env_int("HARNESS_SERVER_MAX_BODY_BYTES", DEFAULT_MAX_REQUEST_BODY_BYTES)))
    allowed_origin = cors_origin or os.environ.get("HARNESS_SERVER_CORS_ORIGIN") or DEFAULT_CORS_ORIGIN

    class Handler(BaseHTTPRequestHandler):
        server_version = "HarnessLocalServer/0.1"

        def do_GET(self) -> None:  # noqa: N802
            if not _authorized(self.headers.get("Authorization"), token):
                self._write_error(HTTPStatus.UNAUTHORIZED, "unauthorized", "Unauthorized.")
                return
            parsed = urlparse(self.path)
            try:
                normalized_path = _normalize_opencode_session_path(parsed.path)
                if normalized_path.startswith("/sessions/") and normalized_path.endswith("/events/stream"):
                    _ensure_session_schema_ready(store)
                    if _query_flag(parse_qs(parsed.query), "live"):
                        self._write_live_session_sse(store, normalized_path, parse_qs(parsed.query))
                        return
                    self._write_sse(build_session_sse_stream(store, normalized_path))
                    return
                if normalized_path in {"/event", "/global/event"}:
                    if _query_flag(parse_qs(parsed.query), "live"):
                        self._write_live_global_sse(store, project_root)
                        return
                    self._write_sse(build_global_event_sse_stream(store, project_root))
                    return
                payload = _route_get(
                    normalized_path,
                    query=parse_qs(parsed.query),
                    project_root=project_root,
                    store=store,
                    cfg=cfg,
                    host=host,
                    port=port,
                )
            except KeyError as exc:
                self._write_error(HTTPStatus.NOT_FOUND, "not_found", str(exc).strip("'"))
                return
            except ValueError as exc:
                self._write_error(HTTPStatus.BAD_REQUEST, "bad_request", str(exc))
                return
            except sqlite3.Error as exc:
                if is_missing_session_schema_error(exc):
                    self._write_error(HTTPStatus.INTERNAL_SERVER_ERROR, "session_schema_missing", SESSION_SCHEMA_REPAIR_MESSAGE)
                    return
                raise
            if payload is None:
                self._write_error(HTTPStatus.NOT_FOUND, "not_found", "Not found.")
                return
            self._write_json(payload)

        def do_POST(self) -> None:  # noqa: N802
            if not _authorized(self.headers.get("Authorization"), token):
                self._write_error(HTTPStatus.UNAUTHORIZED, "unauthorized", "Unauthorized.")
                return
            parsed = urlparse(self.path)
            try:
                body = self._read_json_body()
                payload = _route_post(
                    parsed.path,
                    body=body,
                    project_root=project_root,
                    store=store,
                    cfg=cfg,
                    host=host,
                    port=port,
                )
            except LocalServerHTTPError as exc:
                self._write_error(exc.status, exc.error_code, exc.message)
                return
            except KeyError as exc:
                self._write_error(HTTPStatus.NOT_FOUND, "not_found", str(exc).strip("'"))
                return
            except ValueError as exc:
                self._write_error(HTTPStatus.BAD_REQUEST, "bad_request", str(exc))
                return
            except sqlite3.Error as exc:
                if is_missing_session_schema_error(exc):
                    self._write_error(HTTPStatus.INTERNAL_SERVER_ERROR, "session_schema_missing", SESSION_SCHEMA_REPAIR_MESSAGE)
                    return
                raise
            if payload is None:
                self._write_error(HTTPStatus.NOT_FOUND, "not_found", "Not found.")
                return
            self._write_json(payload, status=HTTPStatus.CREATED)

        def do_PUT(self) -> None:  # noqa: N802
            if not _authorized(self.headers.get("Authorization"), token):
                self._write_error(HTTPStatus.UNAUTHORIZED, "unauthorized", "Unauthorized.")
                return
            parsed = urlparse(self.path)
            try:
                body = self._read_json_body()
                payload = _route_post(
                    parsed.path,
                    body=body,
                    project_root=project_root,
                    store=store,
                    cfg=cfg,
                    host=host,
                    port=port,
                )
            except LocalServerHTTPError as exc:
                self._write_error(exc.status, exc.error_code, exc.message)
                return
            except KeyError as exc:
                self._write_error(HTTPStatus.NOT_FOUND, "not_found", str(exc).strip("'"))
                return
            except ValueError as exc:
                self._write_error(HTTPStatus.BAD_REQUEST, "bad_request", str(exc))
                return
            except sqlite3.Error as exc:
                if is_missing_session_schema_error(exc):
                    self._write_error(HTTPStatus.INTERNAL_SERVER_ERROR, "session_schema_missing", SESSION_SCHEMA_REPAIR_MESSAGE)
                    return
                raise
            if payload is None:
                self._write_error(HTTPStatus.NOT_FOUND, "not_found", "Not found.")
                return
            self._write_json(payload, status=HTTPStatus.OK)

        def do_PATCH(self) -> None:  # noqa: N802
            if not _authorized(self.headers.get("Authorization"), token):
                self._write_error(HTTPStatus.UNAUTHORIZED, "unauthorized", "Unauthorized.")
                return
            parsed = urlparse(self.path)
            try:
                body = self._read_json_body()
                payload = _route_patch(parsed.path, body=body, store=store, cfg=cfg)
            except LocalServerHTTPError as exc:
                self._write_error(exc.status, exc.error_code, exc.message)
                return
            except KeyError as exc:
                self._write_error(HTTPStatus.NOT_FOUND, "not_found", str(exc).strip("'"))
                return
            except ValueError as exc:
                self._write_error(HTTPStatus.BAD_REQUEST, "bad_request", str(exc))
                return
            except sqlite3.Error as exc:
                if is_missing_session_schema_error(exc):
                    self._write_error(HTTPStatus.INTERNAL_SERVER_ERROR, "session_schema_missing", SESSION_SCHEMA_REPAIR_MESSAGE)
                    return
                raise
            if payload is None:
                self._write_error(HTTPStatus.NOT_FOUND, "not_found", "Not found.")
                return
            self._write_json(payload, status=HTTPStatus.OK)

        def do_DELETE(self) -> None:  # noqa: N802
            if not _authorized(self.headers.get("Authorization"), token):
                self._write_error(HTTPStatus.UNAUTHORIZED, "unauthorized", "Unauthorized.")
                return
            parsed = urlparse(self.path)
            try:
                payload = _route_delete(parsed.path, store=store)
            except KeyError as exc:
                self._write_error(HTTPStatus.NOT_FOUND, "not_found", str(exc).strip("'"))
                return
            except ValueError as exc:
                self._write_error(HTTPStatus.BAD_REQUEST, "bad_request", str(exc))
                return
            except sqlite3.Error as exc:
                if is_missing_session_schema_error(exc):
                    self._write_error(HTTPStatus.INTERNAL_SERVER_ERROR, "session_schema_missing", SESSION_SCHEMA_REPAIR_MESSAGE)
                    return
                raise
            if payload is None:
                self._write_error(HTTPStatus.NOT_FOUND, "not_found", "Not found.")
                return
            self._write_json(payload, status=HTTPStatus.OK)

        def do_OPTIONS(self) -> None:  # noqa: N802
            self.send_response(HTTPStatus.NO_CONTENT.value)
            self._write_common_headers()
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

        def _read_json_body(self) -> dict[str, Any]:
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError as exc:
                raise LocalServerHTTPError(HTTPStatus.BAD_REQUEST, "invalid_content_length", "Invalid Content-Length header.") from exc
            if length < 0:
                raise LocalServerHTTPError(HTTPStatus.BAD_REQUEST, "invalid_content_length", "Invalid Content-Length header.")
            if length > request_body_limit:
                raise LocalServerHTTPError(
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                    "request_body_too_large",
                    f"Request body exceeds {request_body_limit} bytes.",
                )
            raw = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError as exc:
                raise LocalServerHTTPError(HTTPStatus.BAD_REQUEST, "invalid_json", f"Invalid JSON body: {exc.msg}") from exc
            if not isinstance(payload, dict):
                raise LocalServerHTTPError(HTTPStatus.BAD_REQUEST, "invalid_json", "JSON body must be an object.")
            return payload

        def _write_error(self, status: HTTPStatus, error_code: str, message: str) -> None:
            self._write_json(_local_server_error(status, error_code, message), status=status)

        def _write_json(self, payload: dict[str, Any], *, status: HTTPStatus = HTTPStatus.OK) -> None:
            body = json.dumps(payload, indent=2, sort_keys=True, default=str).encode("utf-8")
            self.send_response(status.value)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self._write_common_headers()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _write_sse(self, body: str, *, status: HTTPStatus = HTTPStatus.OK) -> None:
            data = body.encode("utf-8")
            self.send_response(status.value)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self._write_common_headers()
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _write_live_session_sse(self, store: SQLiteStore, path: str, query: dict[str, list[str]]) -> None:
            session_id = _session_id_from_sse_path(path)
            store.get_session(session_id)
            after_seq = _sse_after_seq(query, self.headers.get("Last-Event-ID"))
            subscription = subscribe_store_events(store, EventStreamType.SESSION, session_id, after_seq=after_seq)
            ready = _session_sse_ready_event(session_id)
            try:
                self._write_live_sse_headers()
                self._write_sse_chunk(ready)
                while True:
                    event = subscription.next(timeout=15.0)
                    if event is None:
                        self._write_sse_chunk(_sse_heartbeat_event())
                        continue
                    self._write_sse_chunk(_session_sse_event(event))
            except (BrokenPipeError, ConnectionResetError, OSError):
                return
            finally:
                subscription.close()

        def _write_live_global_sse(self, store: SQLiteStore, project_root: Path) -> None:
            subscription = subscribe_global_events(project_root)
            for event in _global_session_events(store):
                subscription.enqueue(event)
            ready = _global_sse_ready_event(project_root)
            try:
                self._write_live_sse_headers()
                self._write_sse_chunk(ready)
                while True:
                    event = subscription.next(timeout=15.0)
                    if event is None:
                        self._write_sse_chunk(_sse_heartbeat_event())
                        continue
                    self._write_sse_chunk(_global_sse_event(project_root, event.id, event))
            except (BrokenPipeError, ConnectionResetError, OSError):
                return
            finally:
                subscription.close()

        def _write_live_sse_headers(self, *, status: HTTPStatus = HTTPStatus.OK) -> None:
            self.send_response(status.value)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self._write_common_headers()
            self.end_headers()

        def _write_sse_chunk(self, body: str) -> None:
            self.wfile.write(body.encode("utf-8"))
            self.wfile.flush()

        def _write_common_headers(self) -> None:
            self.send_header("Access-Control-Allow-Origin", allowed_origin)
            self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, DELETE, OPTIONS")
            self.send_header("X-Harness-Permission-Granting", "false")
            self.send_header("X-Harness-Max-Request-Body-Bytes", str(request_body_limit))

    return HarnessLocalHTTPServer((host, port), Handler, project_root=project_root)


def _route_get(
    path: str,
    *,
    project_root: Path,
    store: SQLiteStore,
    cfg,
    host: str,
    port: int,
    query: dict[str, list[str]] | None = None,
) -> dict[str, Any] | None:
    path = _normalize_opencode_session_path(path)
    query = query or {}
    if _uses_session_schema(path):
        _ensure_session_schema_ready(store)
    if path == "/health":
        return {
            "schema_version": LOCAL_SERVER_SCHEMA_VERSION,
            "ok": True,
            "project_root": str(project_root),
            "permission_granting": False,
        }
    if path == "/global/health":
        return {
            "schema_version": "harness.global_health/v1",
            "ok": True,
            "healthy": True,
            "version": __version__,
            "project_root": str(project_root),
            "permission_granting": False,
        }
    if path in {"/event", "/global/event"}:
        return _global_event_projection(store, project_root)
    if path == "/global/config":
        return _global_config_projection(project_root, cfg)
    if path == "/tui/control/next":
        return {
            "schema_version": "harness.tui_control_next/v1",
            "ok": True,
            "request": None,
            "queue_empty": True,
            "blocking": False,
            "control_queue_enabled": False,
            "process_started": False,
            "permission_granting": False,
        }
    if path == "/server/lifecycle":
        return _server_lifecycle_projection(project_root, host=host, port=port)
    if path == "/server/mdns":
        return _server_mdns_projection(host=host, port=port)
    if path == "/openapi.json":
        return build_openapi_spec(server_url=f"http://{host}:{port}")
    if path in {"/providers", "/provider", "/api/provider", "/config/providers"}:
        return _provider_catalog_projection(store, cfg)
    if path.startswith("/api/provider/"):
        provider_id = path.removeprefix("/api/provider/").strip("/")
        return _provider_catalog_projection(store, cfg, provider_id=provider_id)
    if path == "/provider/auth":
        return _provider_auth_projection(cfg)
    if path in {"/models/validate", "/api/model/validate"}:
        raw_model_ref = _single_query_value(query, "model") or _single_query_value(query, "raw_model_ref")
        if not raw_model_ref:
            raise ValueError("Missing required query parameter: model")
        return _model_selection_validation_projection(cfg, raw_model_ref)
    if path in {"/models", "/api/model"}:
        return _model_catalog_projection(store, cfg)
    if path == "/config":
        return {
            "schema_version": "harness.config_projection/v1",
            "ok": True,
            "config": {
                "project_name": cfg.project_name,
                "chat": cfg.chat.model_dump(mode="json"),
                "backends": [backend.to_descriptor().model_dump(mode="json") for backend in cfg.backends.values()],
                "context_excludes": cfg.context_excludes,
                "isolation_copy_excludes": cfg.isolation_copy_excludes,
            },
            "permission_granting": False,
        }
    if path == "/path":
        return _path_projection(project_root)
    if path == "/project":
        return _project_list_projection(project_root, cfg)
    if path == "/project/current":
        return _project_current_projection(project_root, cfg)
    if path == "/vcs":
        return _vcs_projection(project_root)
    if path in {"/vcs/status", "/file/status", "/files/status"}:
        return _file_status_projection(project_root, cfg.context_excludes)
    if path == "/vcs/diff":
        return _vcs_diff_projection(project_root, raw=False)
    if path == "/vcs/diff/raw":
        return _vcs_diff_projection(project_root, raw=True)
    if path in {"/agents", "/agent"}:
        return {
            "schema_version": "harness.project_agents/v1",
            "ok": True,
            "agents": [agent.model_dump(mode="json") for agent in store.list_project_agents()],
            "permission_granting": False,
        }
    if path == "/artifacts":
        return {
            "schema_version": "harness.artifacts/v1",
            "ok": True,
            "artifacts": _all_artifact_metadata(store),
            "contents_included": False,
            "permission_granting": False,
        }
    if path in {"/files", "/file"}:
        return {
            "schema_version": "harness.files/v1",
            "ok": True,
            "files": _project_file_metadata(project_root, cfg.context_excludes),
            "contents_included": False,
            "permission_granting": False,
        }
    if path in {"/files/content", "/file/content"}:
        requested_path = _single_query_value(query, "path")
        if not requested_path:
            raise ValueError("Missing required query parameter: path")
        return _file_content_preview(project_root, requested_path, cfg.context_excludes)
    if path == "/find":
        pattern = _single_query_value(query, "pattern") or _single_query_value(query, "query")
        if not pattern:
            raise ValueError("Missing required query parameter: pattern")
        return _find_text_projection(project_root, cfg.context_excludes, pattern=pattern)
    if path == "/find/file":
        search = _single_query_value(query, "query") or _single_query_value(query, "q")
        if not search:
            raise ValueError("Missing required query parameter: query")
        return _find_file_projection(project_root, cfg.context_excludes, query=search)
    if path == "/find/symbol":
        return _symbol_catalog(project_root, cfg.context_excludes, query=query)
    if path == "/references":
        return _reference_catalog(project_root, cfg)
    if path == "/instructions":
        return _instruction_file_catalog(project_root, cfg.context_excludes)
    if path == "/symbols":
        return _symbol_catalog(project_root, cfg.context_excludes, query=query)
    if path in {"/lsp/diagnostics", "/lsp"}:
        return _lsp_diagnostics_projection(cfg)
    if path in {"/formatters", "/formatter"}:
        return _formatter_catalog(cfg)
    if path in {"/mcp/status", "/mcp"}:
        return _mcp_status_projection(cfg)
    if path == "/mcp/resources":
        return _mcp_resources_projection(cfg)
    if path == "/plugins":
        return _plugin_catalog(project_root, cfg)
    if path in {"/skills", "/skill"}:
        return _skill_catalog(project_root, cfg)
    if path == "/web/tools":
        return _web_tool_policy_projection(cfg)
    if path == "/extensions/status":
        return _extensibility_status_projection(project_root, cfg)
    if path == "/web/client":
        return _web_client_projection(host=host, port=port)
    if path == "/worktrees":
        return _worktree_projection(project_root)
    if path == "/dev-loop/status":
        return _dev_loop_status_projection(
            store,
            project_root,
            cfg,
            session_id=_single_query_value(query, "session_id"),
        )
    if path in {"/workspaces", "/experimental/workspace"}:
        return build_workspace_catalog(project_root)
    if path == "/workspaces/clients":
        return build_workspace_clients_projection(project_root)
    if path == "/experimental/workspace/adapter":
        return _workspace_adapter_projection(project_root)
    if path == "/experimental/workspace/status":
        return _workspace_status_projection(project_root)
    if path == "/pty":
        return _pty_session_projection()
    if path == "/pty/sessions":
        return _pty_session_projection()
    if path == "/pty/shells":
        return _pty_shell_projection()
    if path == "/pty/restoration":
        return _pty_restoration_readiness_projection(store)
    if path == "/pty/tabs":
        return _pty_terminal_tabs_projection(store, pty_id=_single_query_value(query, "pty_id"))
    if path.startswith("/pty/"):
        parts = [part for part in path.split("/") if part]
        if len(parts) == 3 and parts[2] == "restoration":
            return _pty_restoration_readiness_projection(store, pty_id=parts[1])
        if len(parts) == 3 and parts[2] == "tab":
            return _pty_terminal_tabs_projection(store, pty_id=parts[1])
        if len(parts) == 2:
            return _pty_detail_projection(parts[1])
        if len(parts) == 3 and parts[2] == "connect":
            return _pty_action_unsupported("connect", {}, pty_id=parts[1])
    if path == "/distribution/status":
        return _distribution_status_projection(project_root)
    if path == "/distribution/packaging-smoke":
        return _packaging_smoke_projection(project_root)
    if path == "/desktop/status":
        return _desktop_status_projection()
    if path == "/version/check":
        return _version_check_projection()
    if path == "/settings/tui":
        return build_tui_settings_catalog()
    if path in {"/commands", "/command"}:
        return build_command_catalog(project_root)
    if path == "/tools":
        return session_tool_catalog_projection(project_root=project_root)
    if path.startswith("/tools/"):
        tool_id = path.removeprefix("/tools/").strip("/")
        if tool_id:
            return session_tool_catalog_projection(project_root=project_root, tool_id=tool_id)
    if path == "/permission":
        return _global_permission_queue_projection(store)
    if path == "/question":
        return _global_question_queue_projection(store)
    if path == "/api/session":
        return _api_session_list_projection(store, query)
    if path.startswith("/api/session/"):
        parts = [part for part in path.split("/") if part]
        if len(parts) == 4 and parts[3] == "message":
            return _api_session_messages_projection(store, parts[2], query)
        if len(parts) == 4 and parts[3] == "context":
            return _api_session_context_projection(store, parts[2], query)
    if path == "/sessions":
        return {
            "schema_version": "harness.sessions/v1",
            "ok": True,
            "sessions": [session.model_dump(mode="json") for session in store.list_sessions()],
        }
    if path.startswith("/sessions/"):
        parts = [part for part in path.split("/") if part]
        if len(parts) == 2 and parts[1] == "status":
            return _sessions_status_projection(store)
        if len(parts) == 2:
            session = store.get_session(parts[1])
            try:
                cwd = session_cwd_payload(project_root, session.metadata, cfg.context_excludes)
            except Exception:
                cwd = {"cwd": session.metadata.get("cwd", "."), "resolved_abs_path": None}
            return {
                "schema_version": "harness.session/v1",
                "ok": True,
                "session": session.model_dump(mode="json"),
                "cwd": cwd,
                "latest_ui_activation": _latest_session_ui_activation(store, session.id),
                "permission_granting": False,
            }
        if len(parts) == 3 and parts[2] == "events":
            events = store.list_store_events(EventStreamType.SESSION, parts[1])
            return {
                "schema_version": "harness.session_events/v1",
                "ok": True,
                "session_id": parts[1],
                "events": [event.model_dump(mode="json") for event in events],
            }
        if len(parts) == 3 and parts[2] == "status":
            return _session_status_projection(store, parts[1])
        if len(parts) == 3 and parts[2] in {"tool", "tools"}:
            store.get_session(parts[1])
            return {
                **session_tool_catalog_projection(project_root=project_root),
                "session_id": parts[1],
            }
        if len(parts) == 4 and parts[2] in {"tool", "tools"}:
            store.get_session(parts[1])
            return {
                **session_tool_catalog_projection(project_root=project_root, tool_id=parts[3]),
                "session_id": parts[1],
            }
        if len(parts) == 3 and parts[2] == "children":
            return _session_children_projection(store, parts[1])
        if len(parts) == 3 and parts[2] == "replay":
            return build_session_replay_projection(
                store,
                parts[1],
                after_seq=_optional_query_int(query, "after_seq"),
                limit=_optional_query_int(query, "limit"),
            )
        if len(parts) == 3 and parts[2] in {"messages", "message"}:
            store.get_session(parts[1])
            messages = store.list_session_messages(parts[1])
            limit = _optional_query_int(query, "limit")
            if limit is not None:
                messages = messages[-limit:] if limit else []
            parts_by_message = {message.id: store.list_session_parts(parts[1], message.id) for message in messages}
            return {
                "schema_version": "harness.session_messages/v1",
                "ok": True,
                "session_id": parts[1],
                "limit": limit,
                "messages": [message.model_dump(mode="json") for message in messages],
                "parts": {
                    message_id: [part.model_dump(mode="json") for part in message_parts]
                    for message_id, message_parts in parts_by_message.items()
                },
                "permission_granting": False,
            }
        if len(parts) == 4 and parts[2] in {"messages", "message"}:
            return _session_message_detail_projection(store, parts[1], parts[3])
        if len(parts) == 3 and parts[2] == "permissions":
            store.get_session(parts[1])
            permissions = store.list_session_permissions(parts[1])
            return {
                "schema_version": "harness.session_permissions/v1",
                "ok": True,
                "session_id": parts[1],
                "permissions": _session_permission_payloads(store, permissions),
                "snapshot": _session_permission_snapshot_payload(parts[1], permissions, store=store),
                "permission_granting": False,
            }
        if len(parts) == 4 and parts[2] == "permissions" and parts[3] == "snapshot":
            store.get_session(parts[1])
            permissions = store.list_session_permissions(parts[1])
            return {
                "schema_version": "harness.session_permission_snapshot/v1",
                "ok": True,
                **_session_permission_snapshot_payload(parts[1], permissions, store=store),
                "permission_granting": False,
            }
        if len(parts) == 3 and parts[2] in {"todos", "todo"}:
            store.get_session(parts[1])
            status = _single_query_value(query, "status")
            return {
                "schema_version": "harness.session_todos/v1",
                "ok": True,
                "session_id": parts[1],
                "todos": [todo.model_dump(mode="json") for todo in store.list_session_todos(parts[1], status=status)],
                "execution_started": False,
                "permission_granting": False,
            }
        if len(parts) == 3 and parts[2] == "questions":
            return _session_questions_projection(store, parts[1])
        if len(parts) == 3 and parts[2] in {"diffs", "diff"}:
            return _session_diff_projection(store, parts[1])
        if len(parts) == 3 and parts[2] == "changed-files":
            return _session_changed_files_projection(store, parts[1], project_root, cfg.context_excludes)
        if len(parts) == 3 and parts[2] == "snapshots":
            return _session_snapshots_projection(store, parts[1], project_root, cfg.context_excludes)
        if len(parts) == 3 and parts[2] == "revert-readiness":
            return _session_revert_readiness_projection(store, parts[1], project_root, cfg.context_excludes)
        if len(parts) == 5 and parts[2] in {"messages", "message"} and parts[4] == "snapshots":
            return _session_snapshots_projection(
                store,
                parts[1],
                project_root,
                cfg.context_excludes,
                message_id=parts[3],
            )
        if len(parts) == 5 and parts[2] in {"messages", "message"} and parts[4] == "revert-readiness":
            return _session_revert_readiness_projection(
                store,
                parts[1],
                project_root,
                cfg.context_excludes,
                message_id=parts[3],
            )
        if len(parts) == 3 and parts[2] == "share":
            return build_local_session_share_snapshot(store, parts[1])
        if len(parts) == 4 and parts[2] == "events" and parts[3] == "stream":
            return {
                "schema_version": "harness.session_event_stream/v1",
                "ok": True,
                "session_id": parts[1],
                "transport": "sse",
                "permission_granting": False,
            }
    return None


def _route_post(
    path: str,
    *,
    body: dict[str, Any],
    project_root: Path,
    store: SQLiteStore,
    cfg,
    host: str,
    port: int,
) -> dict[str, Any] | None:
    path = _normalize_opencode_session_path(path)
    if _uses_session_schema(path):
        _ensure_session_schema_ready(store)
    if path == "/sessions":
        prompt = _optional_body_text(body, "prompt")
        title = _optional_body_text(body, "title") or _title_from_prompt(prompt) or "Server session"
        raw_model_ref = _body_model_ref(body)
        agent_id = _optional_body_text(body, "agent_id")
        session = store.create_session(
            title=title,
            raw_model_ref=raw_model_ref,
            agent_id=agent_id,
            intent="server_prompt" if prompt else "server_session",
            metadata={
                "created_by": "harness_serve",
                "cwd": ".",
                "execution_started": False,
                "permission_granting": False,
            },
        )
        model_validation = _append_session_model_validation_event(
            store,
            cfg,
            session.id,
            raw_model_ref,
            source="local_server_session_create",
        )
        message_payload: dict[str, Any] = {}
        if prompt:
            message_payload = _append_server_user_message(store, session.id, prompt, agent_id=agent_id)
        return {
            "schema_version": "harness.local_server_session_create/v1",
            "ok": True,
            "session": store.get_session(session.id).model_dump(mode="json"),
            "model_validation": model_validation,
            **message_payload,
            "execution_started": False,
            "provider_execution_started": False,
            "model_execution_started": False,
            "permission_granting": False,
            "authority_granting": False,
            "no_hidden_fallback": True,
        }
    if path.startswith("/permission/"):
        parts = [part for part in path.split("/") if part]
        if len(parts) == 3 and parts[2] == "reply":
            existing = store.get_session_permission(parts[1])
            return _reply_to_session_permission(store, existing.session_id, existing.id, body)
    if path.startswith("/question/"):
        parts = [part for part in path.split("/") if part]
        if len(parts) == 3 and parts[2] in {"reply", "reject"}:
            return _reply_to_session_question(store, parts[1], body, rejected=parts[2] == "reject")
    if path.startswith("/provider/"):
        parts = [part for part in path.split("/") if part]
        if len(parts) == 4 and parts[2] == "oauth" and parts[3] in {"authorize", "callback"}:
            return _provider_oauth_unsupported(parts[1], parts[3], body)
    if path.startswith("/auth/"):
        parts = [part for part in path.split("/") if part]
        if len(parts) == 2:
            return _auth_action_unsupported("set", parts[1], body)
    if path == "/log":
        return _client_log_unsupported(body)
    if path in {"/worktrees", "/worktrees/create", "/experimental/worktree"}:
        return _worktree_action_unsupported("create", body, project_root)
    if path in {"/worktrees/remove", "/experimental/worktree/remove"}:
        return _worktree_action_unsupported("remove", body, project_root)
    if path in {"/worktrees/reset", "/experimental/worktree/reset"}:
        return _worktree_action_unsupported("reset", body, project_root)
    if path.startswith("/api/session/"):
        parts = [part for part in path.split("/") if part]
        if len(parts) == 4 and parts[3] == "prompt":
            session_id = parts[2]
            store.get_session(session_id)
            content = _message_content_from_body(body)
            if not content:
                raise ValueError("Missing required prompt content.")
            agent_id = _optional_body_text(body, "agent_id") or _optional_body_text(body, "agent")
            model_validation = _append_session_model_validation_event(
                store,
                cfg,
                session_id,
                _body_model_ref(body),
                source="api_session_prompt",
            )
            return {
                "schema_version": "harness.api_session_prompt/v1",
                "ok": True,
                "session_id": session_id,
                "mode": "append_only",
                "assistant_execution": False,
                "model_validation": model_validation,
                **_append_server_user_message(store, session_id, content, agent_id=agent_id),
                "assistant_response_started": False,
                "execution_started": False,
                "provider_execution_started": False,
                "model_execution_started": False,
                "permission_granting": False,
                "authority_granting": False,
                "no_hidden_fallback": True,
            }
        if len(parts) == 4 and parts[3] == "compact":
            return _api_session_compact_projection(store, parts[2], body)
        if len(parts) == 4 and parts[3] == "wait":
            return _api_session_wait_projection(store, parts[2], body)
    if path.startswith("/sessions/"):
        parts = [part for part in path.split("/") if part]
        if len(parts) == 3 and parts[2] == "abort":
            session_id = parts[1]
            reason = _optional_body_text(body, "reason")
            session = store.cancel_session(session_id, reason=reason)
            return {
                "schema_version": "harness.session_abort/v1",
                "ok": True,
                "session": session.model_dump(mode="json"),
                "process_stopped": False,
                "run_cancelled": False,
                "task_cancelled": False,
                "permission_granting": False,
            }
        if len(parts) == 3 and parts[2] == "fork":
            session_id = parts[1]
            child = store.fork_session(
                session_id,
                message_id=_optional_body_text(body, "message_id") or _optional_body_text(body, "messageID"),
                title=_optional_body_text(body, "title"),
                metadata={
                    "created_by": "harness_serve",
                    "execution_started": False,
                    "permission_granting": False,
                },
            )
            return {
                "schema_version": "harness.session_fork/v1",
                "ok": True,
                "parent_session_id": session_id,
                "session": child.model_dump(mode="json"),
                "execution_started": False,
                "permission_granting": False,
            }
        if len(parts) == 3 and parts[2] in {"summary", "summarize"}:
            session_id = parts[1]
            if parts[2] == "summarize" and not _optional_body_text(body, "summary"):
                raise ValueError("Harness summarize requires an explicit summary; provider-backed compaction is not enabled.")
            session = store.update_session_summary(
                session_id,
                summary=_optional_body_text(body, "summary"),
                token_input=_optional_body_int(body, "token_input"),
                token_output=_optional_body_int(body, "token_output"),
                token_reasoning=_optional_body_int(body, "token_reasoning"),
                token_cache_read=_optional_body_int(body, "token_cache_read"),
                token_cache_write=_optional_body_int(body, "token_cache_write"),
                estimated_cost_usd=_optional_body_text(body, "estimated_cost_usd"),
            )
            return {
                "schema_version": "harness.session_summary/v1",
                "ok": True,
                "session": session.model_dump(mode="json"),
                "mutable_projection": True,
                "provider_execution_started": False,
                "permission_granting": False,
            }
        if len(parts) == 3 and parts[2] in {"messages", "message"}:
            session_id = parts[1]
            store.get_session(session_id)
            role = _optional_body_text(body, "role") or SessionMessageRole.USER.value
            if role != SessionMessageRole.USER.value:
                raise ValueError("Only user messages can be appended through the local server MVP.")
            content = _message_content_from_body(body)
            if not content:
                raise ValueError("Missing required message content.")
            model_validation = _append_session_model_validation_event(
                store,
                cfg,
                session_id,
                _body_model_ref(body),
                source="local_server_message_append",
            )
            return {
                "schema_version": "harness.local_server_message_append/v1",
                "ok": True,
                "session_id": session_id,
                "model_validation": model_validation,
                **_append_server_user_message(store, session_id, content),
                "execution_started": False,
                "provider_execution_started": False,
                "model_execution_started": False,
                "permission_granting": False,
                "authority_granting": False,
                "no_hidden_fallback": True,
            }
        if len(parts) == 3 and parts[2] == "prompt_async":
            session_id = parts[1]
            store.get_session(session_id)
            content = _message_content_from_body(body)
            if not content:
                raise ValueError("Missing required prompt content.")
            agent_id = _optional_body_text(body, "agent_id") or _optional_body_text(body, "agent")
            model_validation = _append_session_model_validation_event(
                store,
                cfg,
                session_id,
                _body_model_ref(body),
                source="local_server_prompt_async",
            )
            message_payload = _append_server_user_message(store, session_id, content, agent_id=agent_id)
            runtime_acceptance = SessionRuntimeManager.for_store(store).submit_prompt(
                SessionPromptRequest(
                    session_id=session_id,
                    content=content,
                    mode="async",
                    queue_policy=SessionPromptQueuePolicy.FOLLOW_UP,
                    agent_id=agent_id,
                    model_ref=_body_model_ref(body),
                    message_id=message_payload["message"]["id"],
                    part_id=message_payload["part"]["id"],
                    metadata={"source": "local_server_prompt_async"},
                )
            )
            return {
                "schema_version": "harness.local_server_prompt_async/v1",
                "ok": True,
                "session_id": session_id,
                "model_validation": model_validation,
                **message_payload,
                "runtime": runtime_acceptance.model_dump(mode="json"),
                "async_accepted": True,
                "waited_for_response": False,
                "assistant_response_started": False,
                "execution_started": runtime_acceptance.execution_started,
                "provider_execution_started": runtime_acceptance.execution_started,
                "model_execution_started": runtime_acceptance.execution_started,
                "turn_id": runtime_acceptance.runtime.active_turn_id,
                "permission_granting": False,
                "authority_granting": False,
                "no_hidden_fallback": True,
            }
        if len(parts) == 3 and parts[2] == "prompt":
            session_id = parts[1]
            store.get_session(session_id)
            content = _message_content_from_body(body)
            if not content:
                raise ValueError("Missing required prompt content.")
            return _session_prompt_operator_response(
                store,
                project_root,
                session_id,
                content,
                debug=_optional_body_bool(body, "debug"),
            )
        if len(parts) == 3 and parts[2] == "command":
            session_id = parts[1]
            store.get_session(session_id)
            payload = command_action_unsupported("session_command", _optional_body_text(body, "command"), body)
            payload.update(
                {
                    "session_id": session_id,
                    "schema_version": "harness.session_command_action/v1",
                    "execution_started": False,
                    "provider_execution_started": False,
                }
            )
            return payload
        if len(parts) == 3 and parts[2] == "init":
            session_id = parts[1]
            store.get_session(session_id)
            return _session_init_unsupported(session_id, body)
        if len(parts) == 3 and parts[2] == "shell":
            session_id = parts[1]
            store.get_session(session_id)
            return _session_tool_execution_response(store, project_root, session_id, "shell", body)
        if len(parts) == 3 and parts[2] in {"tool", "tools"}:
            session_id = parts[1]
            store.get_session(session_id)
            tool_id = _optional_body_text(body, "tool_id") or _optional_body_text(body, "tool")
            if not tool_id:
                raise ValueError("Missing required body field: tool_id")
            arguments = body.get("arguments") if isinstance(body.get("arguments"), dict) else {
                key: value for key, value in body.items() if key not in {"tool", "tool_id"}
            }
            return _session_tool_execution_response(store, project_root, session_id, tool_id, arguments)
        if len(parts) == 4 and parts[2] in {"tool", "tools"}:
            session_id = parts[1]
            store.get_session(session_id)
            arguments = body.get("arguments") if isinstance(body.get("arguments"), dict) else body
            return _session_tool_execution_response(store, project_root, session_id, parts[3], arguments)
        if len(parts) == 5 and parts[2] == "messages" and parts[4] == "retract":
            session_id = parts[1]
            message_id = parts[3]
            event = store.record_session_message_retraction(
                session_id,
                message_id,
                reason=_optional_body_text(body, "reason"),
            )
            return {
                "schema_version": "harness.session_message_retraction/v1",
                "ok": True,
                "session_id": session_id,
                "message_id": message_id,
                "event": event.model_dump(mode="json"),
                "message_mutated": False,
                "parts_mutated": False,
                "permission_granting": False,
            }
        if len(parts) == 5 and parts[2] == "parts" and parts[4] == "correct":
            session_id = parts[1]
            part_id = parts[3]
            corrected_text = _optional_body_text(body, "corrected_text") or _optional_body_text(body, "text")
            if not corrected_text:
                raise ValueError("Missing required corrected text.")
            event = store.record_session_part_correction(
                session_id,
                part_id,
                corrected_text=corrected_text,
                reason=_optional_body_text(body, "reason"),
            )
            return {
                "schema_version": "harness.session_part_correction/v1",
                "ok": True,
                "session_id": session_id,
                "part_id": part_id,
                "event": event.model_dump(mode="json"),
                "part_mutated": False,
                "message_mutated": False,
                "permission_granting": False,
            }
        if len(parts) == 3 and parts[2] == "todos":
            session_id = parts[1]
            content = _optional_body_text(body, "content")
            if not content:
                raise ValueError("Missing required todo content.")
            todo = store.append_session_todo(
                session_id,
                content,
                status=_optional_body_text(body, "status") or "pending",
                priority=_optional_body_int(body, "priority") or 0,
                source_message_id=_optional_body_text(body, "source_message_id"),
            )
            return {
                "schema_version": "harness.session_todo/v1",
                "ok": True,
                "todo": todo.model_dump(mode="json"),
                "execution_started": False,
                "permission_granting": False,
            }
        if len(parts) == 3 and parts[2] == "questions":
            session_id = parts[1]
            question = _optional_body_text(body, "question")
            if not question:
                raise ValueError("Missing required question.")
            part = store.append_session_question(
                session_id,
                question,
                choices=_optional_body_text_list(body, "choices"),
                source_message_id=_optional_body_text(body, "source_message_id"),
            )
            return {
                "schema_version": "harness.session_question/v1",
                "ok": True,
                "part": part.model_dump(mode="json"),
                "execution_started": False,
                "permission_granting": False,
            }
        if (
            len(parts) == 5
            and parts[2] == "permissions"
            and parts[4] == "reply"
        ) or (len(parts) == 4 and parts[2] == "permissions"):
            session_id = parts[1]
            permission_id = parts[3]
            return _reply_to_session_permission(store, session_id, permission_id, body)
        if len(parts) in {4, 5} and parts[2] == "approval":
            session_id = parts[1]
            permission_id = parts[3]
            action = parts[4] if len(parts) == 5 else _optional_body_text(body, "action")
            return _reply_to_session_permission(
                store,
                session_id,
                permission_id,
                _approval_reply_body(body, action),
                project_root=project_root,
                resume=True,
            )
        if len(parts) == 4 and parts[2] == "mentions" and parts[3] == "resolve":
            session_id = parts[1]
            store.get_session(session_id)
            prompt = _optional_body_text(body, "prompt")
            mentions = _extract_mentions(prompt or "")
            resolved = [_resolve_mention(project_root, store, mention, cfg) for mention in mentions]
            store.append_store_event(
                EventStreamType.SESSION,
                session_id,
                "session.mentions.resolved",
                {
                    "mention_count": len(resolved),
                    "mentions": resolved,
                    "contents_included": False,
                    "permission_granting": False,
                },
                session_id=session_id,
                redaction_state=RedactionState.REDACTED,
            )
            return {
                "schema_version": "harness.mention_resolution/v1",
                "ok": True,
                "session_id": session_id,
                "mentions": resolved,
                "contents_included": False,
                "permission_granting": False,
                "execution_started": False,
            }
        if len(parts) == 3 and parts[2] == "attachments":
            session_id = parts[1]
            store.get_session(session_id)
            paths = body.get("paths")
            if not isinstance(paths, list) or not paths:
                raise ValueError("Missing required attachment paths.")
            attachments = [
                _prepare_attachment(project_root, str(path), cfg.context_excludes)
                for path in paths
            ]
            store.append_store_event(
                EventStreamType.SESSION,
                session_id,
                "session.attachments.prepared",
                {
                    "attachment_count": len(attachments),
                    "attachments": attachments,
                    "contents_included": False,
                    "permission_granting": False,
                },
                session_id=session_id,
                redaction_state=RedactionState.REDACTED,
            )
            return {
                "schema_version": "harness.attachment_preparation/v1",
                "ok": True,
                "session_id": session_id,
                "attachments": attachments,
                "contents_included": False,
                "permission_granting": False,
                "execution_started": False,
            }
        if len(parts) == 4 and parts[2] == "context" and parts[3] == "estimate":
            session_id = parts[1]
            store.get_session(session_id)
            estimate = _context_budget_estimate(project_root, store, cfg, body)
            store.append_store_event(
                EventStreamType.SESSION,
                session_id,
                "session.context.estimated",
                estimate,
                session_id=session_id,
                redaction_state=RedactionState.REDACTED,
            )
            return {
                "schema_version": "harness.context_estimate/v1",
                "ok": True,
                "session_id": session_id,
                **estimate,
                "execution_started": False,
            }
        if len(parts) == 3 and parts[2] in {"revert", "unrevert"}:
            session_id = parts[1]
            store.get_session(session_id)
            return _session_mutation_unsupported(parts[2], session_id, body)
        if len(parts) == 3 and parts[2] == "share":
            session_id = parts[1]
            store.get_session(session_id)
            return hosted_share_unsupported(session_id, body)
        if len(parts) == 3 and parts[2] == "apply-hunk":
            session_id = parts[1]
            store.get_session(session_id)
            return _session_mutation_unsupported("apply-hunk", session_id, body)
    if path == "/pty":
        return _pty_action_unsupported("create", body)
    if path.startswith("/pty/"):
        parts = [part for part in path.split("/") if part]
        if len(parts) == 2 and parts[1] == "sessions":
            return _pty_action_unsupported("create", body)
        if len(parts) == 2:
            return _pty_action_unsupported("update", body, pty_id=parts[1])
        if len(parts) == 3 and parts[1] == "sessions":
            return _pty_action_unsupported("update", body, pty_id=parts[2])
        if len(parts) == 3 and parts[2] == "connect-token":
            return _pty_action_unsupported("connect-token", body, pty_id=parts[1])
        if len(parts) == 4 and parts[1] == "sessions" and parts[3] in {"write", "resize", "close"}:
            return _pty_action_unsupported(parts[3], body, pty_id=parts[2])
    if path == "/pr/checkout":
        return _pr_action_unsupported("checkout", body)
    if path == "/pr/run":
        return _pr_action_unsupported("run", body)
    if path == "/vcs/apply":
        return _vcs_apply_unsupported(body)
    if path == "/project/git/init":
        return _project_action_unsupported("git.init", None, body)
    if path == "/commands/run":
        return command_action_unsupported("run", _optional_body_text(body, "command_id") or _optional_body_text(body, "name"), body)
    if path.startswith("/sync/"):
        action = path.removeprefix("/sync/")
        if action == "history":
            return _sync_history_projection(body)
        return workspace_action_unsupported(f"sync.{action}", _optional_body_text(body, "workspace_id") or _optional_body_text(body, "sessionID"), body)
    if path.startswith("/experimental/workspace"):
        parts = [part for part in path.split("/") if part]
        action = parts[-1] if len(parts) > 2 else "create"
        return workspace_action_unsupported(f"experimental.{action}", _optional_body_text(body, "id") or _optional_body_text(body, "workspace_id"), body)
    if path == "/mcp":
        return _mcp_action_unsupported("add", _optional_body_text(body, "name"), body)
    if path.startswith("/mcp/"):
        parts = [part for part in path.split("/") if part]
        if len(parts) >= 2:
            name = parts[1]
            action = ".".join(parts[2:]) if len(parts) > 2 else "action"
            return _mcp_action_unsupported(action, name, body)
    if path == "/workspaces/attach":
        return workspace_action_unsupported("attach", _optional_body_text(body, "workspace_id"), body)
    if path == "/workspaces/sync":
        return workspace_action_unsupported("sync", _optional_body_text(body, "workspace_id"), body)
    if path == "/workspaces/steal":
        return workspace_action_unsupported("steal", _optional_body_text(body, "workspace_id"), body)
    if path == "/workspaces/dispose":
        return workspace_action_unsupported("dispose", _optional_body_text(body, "workspace_id"), body)
    if path == "/server/dispose":
        return _server_dispose_unsupported(body)
    if path == "/global/dispose":
        return _global_action_unsupported("dispose", body)
    if path == "/global/upgrade":
        return _global_action_unsupported("upgrade", body)
    if path.startswith("/tui/"):
        return _tui_action_projection(path, body)
    if path == "/web/open":
        return _web_open_unsupported(body, host=host, port=port)
    if path == "/desktop/launch":
        return _desktop_action_unsupported("launch", body)
    if path == "/distribution/packaging-smoke/run":
        return _packaging_smoke_action_unsupported(body)
    return None


def _uses_session_schema(path: str) -> bool:
    return (
        path == "/sessions"
        or path.startswith("/sessions/")
        or path == "/api/session"
        or path.startswith("/api/session/")
        or path == "/permission"
        or path.startswith("/permission/")
        or path == "/question"
        or path.startswith("/question/")
    )


def _ensure_session_schema_ready(store: SQLiteStore) -> None:
    if not store.db_path.exists():
        raise ValueError(f"Project is not initialized: {store.project_root}")
    store.initialize()


def _route_patch(path: str, *, body: dict[str, Any], store: SQLiteStore, cfg: Any | None = None) -> dict[str, Any] | None:
    path = _normalize_opencode_session_path(path)
    if _uses_session_schema(path):
        _ensure_session_schema_ready(store)
    if path == "/config":
        return _config_update_unsupported(body)
    if path.startswith("/project/"):
        parts = [part for part in path.split("/") if part]
        if len(parts) == 2 and parts[1] != "git":
            return _project_action_unsupported("update", parts[1], body)
    if path.startswith("/sessions/"):
        parts = [part for part in path.split("/") if part]
        if len(parts) == 6 and parts[2] == "message" and parts[4] == "part":
            session_id = parts[1]
            message_id = parts[3]
            part_id = parts[5]
            _require_part_in_message(store, session_id, message_id, part_id)
            corrected_text = _optional_body_text(body, "corrected_text") or _optional_body_text(body, "text") or _message_content_from_body(body)
            if not corrected_text:
                raise ValueError("Missing required corrected text.")
            event = store.record_session_part_correction(
                session_id,
                part_id,
                corrected_text=corrected_text,
                reason=_optional_body_text(body, "reason"),
            )
            return {
                "schema_version": "harness.session_part_correction/v1",
                "ok": True,
                "session_id": session_id,
                "message_id": message_id,
                "part_id": part_id,
                "event": event.model_dump(mode="json"),
                "part_mutated": False,
                "message_mutated": False,
                "permission_granting": False,
            }
        if len(parts) == 2 and parts[1] != "status":
            session_id = parts[1]
            store.get_session(session_id)
            title_updated = False
            model_updated = False
            if "title" in body:
                store.update_session_title(session_id, _optional_body_text(body, "title"))
                title_updated = True
            raw_model_ref = _body_model_ref(body)
            model_validation: dict[str, Any] | None = None
            if raw_model_ref:
                store.update_session_model(
                    session_id,
                    raw_model_ref=raw_model_ref,
                    provider_id=_optional_body_text(body, "provider_id") or _optional_body_text(body, "providerID"),
                    model_id=_optional_body_text(body, "model_id") or _optional_body_text(body, "modelID"),
                    model_variant=_optional_body_text(body, "model_variant") or _optional_body_text(body, "variant"),
                )
                if cfg is not None:
                    model_validation = _append_session_model_validation_event(
                        store,
                        cfg,
                        session_id,
                        raw_model_ref,
                        source="local_server_session_update",
                    )
                model_updated = True
            if not title_updated and not model_updated:
                raise ValueError("No supported mutable session fields provided.")
            return {
                "schema_version": "harness.session_update/v1",
                "ok": True,
                "session": store.get_session(session_id).model_dump(mode="json"),
                "model_validation": model_validation,
                "title_updated": title_updated,
                "model_updated": model_updated,
                "messages_mutated": False,
                "parts_mutated": False,
                "execution_started": False,
                "permission_granting": False,
                "no_hidden_fallback": True,
            }
    return None


def _route_delete(path: str, *, store: SQLiteStore) -> dict[str, Any] | None:
    path = _normalize_opencode_session_path(path)
    if _uses_session_schema(path):
        _ensure_session_schema_ready(store)
    if path.startswith("/auth/"):
        parts = [part for part in path.split("/") if part]
        if len(parts) == 2:
            return _auth_action_unsupported("remove", parts[1], {})
    if path.startswith("/mcp/"):
        parts = [part for part in path.split("/") if part]
        if len(parts) >= 3 and parts[2] == "auth":
            return _mcp_action_unsupported("auth.remove", parts[1], {})
    if path.startswith("/pty/"):
        parts = [part for part in path.split("/") if part]
        if len(parts) == 2:
            return _pty_action_unsupported("remove", {}, pty_id=parts[1])
    if path.startswith("/sessions/"):
        parts = [part for part in path.split("/") if part]
        if len(parts) == 3 and parts[2] == "share":
            store.get_session(parts[1])
            return _session_unshare_unsupported(parts[1])
        if len(parts) == 4 and parts[2] == "message":
            session_id = parts[1]
            message_id = parts[3]
            event = store.record_session_message_retraction(session_id, message_id)
            return {
                "schema_version": "harness.session_message_retraction/v1",
                "ok": True,
                "session_id": session_id,
                "message_id": message_id,
                "event": event.model_dump(mode="json"),
                "message_deleted": False,
                "message_mutated": False,
                "parts_deleted": False,
                "parts_mutated": False,
                "permission_granting": False,
            }
        if len(parts) == 6 and parts[2] == "message" and parts[4] == "part":
            session_id = parts[1]
            message_id = parts[3]
            part_id = parts[5]
            _require_part_in_message(store, session_id, message_id, part_id)
            event = store.record_session_part_retraction(session_id, part_id)
            return {
                "schema_version": "harness.session_part_retraction/v1",
                "ok": True,
                "session_id": session_id,
                "message_id": message_id,
                "part_id": part_id,
                "event": event.model_dump(mode="json"),
                "part_deleted": False,
                "part_mutated": False,
                "message_mutated": False,
                "permission_granting": False,
            }
        if len(parts) == 2 and parts[1] != "status":
            session = store.archive_session(parts[1])
            return {
                "schema_version": "harness.session_archive/v1",
                "ok": True,
                "session": session.model_dump(mode="json"),
                "archived": True,
                "hard_deleted": False,
                "messages_deleted": False,
                "parts_deleted": False,
                "events_deleted": False,
                "execution_started": False,
                "permission_granting": False,
            }
    return None


def _append_server_user_message(
    store: SQLiteStore,
    session_id: str,
    content: str,
    *,
    agent_id: str | None = None,
) -> dict[str, Any]:
    message = store.append_session_message(
        session_id,
        SessionMessageRole.USER,
        content,
        agent_id=agent_id,
    )
    part = store.append_session_part(
        session_id,
        message.id,
        SessionPartKind.TEXT,
        text=content,
        metadata={"source": "harness_serve"},
        redaction_state=RedactionState.REDACTED,
    )
    return {
        "message": message.model_dump(mode="json"),
        "part": part.model_dump(mode="json"),
    }


def _session_prompt_operator_response(
    store: SQLiteStore,
    project_root: Path,
    session_id: str,
    prompt: str,
    *,
    debug: bool = False,
) -> dict[str, Any]:
    from harness.chat import ChatSessionState, handle_chat_input

    before_event_ids = {event.id for event in store.list_session_store_events(session_id)}
    state = ChatSessionState(session_id=session_id, active_project_root=str(project_root))
    try:
        chat_response = handle_chat_input(prompt, project_root, state)
    except Exception as exc:  # pragma: no cover - debug payload is asserted through direct helper tests.
        message = SESSION_SCHEMA_REPAIR_MESSAGE if is_missing_session_schema_error(exc) else "The operator prompt failed before completion."
        chat_response = {
            "kind": "operator_prompt_failed",
            "title": "Prompt Failed",
            "ok": False,
            "lines": [message],
        }
        if debug:
            chat_response["debug"] = {
                "exception_type": type(exc).__name__,
                "exception": str(sanitize_for_logging(str(exc))),
                "traceback": traceback.format_exc(),
            }
    events = [event for event in store.list_session_store_events(session_id) if event.id not in before_event_ids]
    status = _session_status_projection(store, session_id)
    payload = {
        "schema_version": "harness.session_prompt_response/v1",
        "ok": bool(chat_response.get("ok")),
        "session_id": session_id,
        "kind": chat_response.get("kind"),
        "title": chat_response.get("title") or chat_response.get("kind") or "Assistant",
        "lines": [str(sanitize_for_logging(line)) for line in (chat_response.get("lines") or [])],
        "operator_status": chat_response.get("operator_status") or status.get("operator"),
        "permission_required": chat_response.get("kind") == "session_tool_permission_required",
        "permission_id": chat_response.get("permission_id"),
        "approval_card": chat_response.get("approval_card"),
        "tool_results": _prompt_tool_result_summaries(chat_response),
        "event_sequence": [event.kind for event in events],
        "event_count": len(events),
        "model_execution_started": bool(chat_response.get("native_tool_loop")),
        "provider_execution_started": False,
        "execution_started": bool(chat_response.get("ok")) and any(event.kind == "tool_call.output" for event in events),
        "permission_granting": False,
        "no_hidden_fallback": True,
    }
    if debug:
        payload["debug"] = {
            "chat_response": sanitize_for_logging(chat_response),
            "events": [event.model_dump(mode="json") for event in events],
        }
    return payload


def _prompt_tool_result_summaries(chat_response: dict[str, Any]) -> list[dict[str, Any]]:
    if isinstance(chat_response.get("tool_results"), list):
        return [
            {
                "tool": item.get("tool"),
                "ok": bool(item.get("ok")),
                "error_type": item.get("error_type"),
            }
            for item in chat_response["tool_results"]
            if isinstance(item, dict)
        ]
    result = chat_response.get("result")
    if isinstance(result, dict) and result.get("tool_id"):
        return [
            {
                "tool": result.get("tool_id"),
                "ok": bool(result.get("ok")),
                "error_type": result.get("error_type"),
            }
        ]
    return []


def _approval_reply_body(body: dict[str, Any], action: str | None) -> dict[str, Any]:
    reply = (_optional_body_text(body, "reply") or "").strip().lower()
    decision = (_optional_body_text(body, "decision") or "").strip().lower()
    if reply or decision:
        return body
    normalized = (action or "resume").strip().lower()
    mapped_reply = "once" if normalized in {"approve", "approved", "allow", "allowed", "resume"} else normalized
    if mapped_reply in {"deny", "denied"}:
        mapped_reply = "reject"
    return {**body, "reply": mapped_reply}


def _message_content_from_body(body: dict[str, Any]) -> str | None:
    direct = _optional_body_text(body, "content") or _optional_body_text(body, "text")
    if direct:
        return direct
    prompt = body.get("prompt")
    if isinstance(prompt, str):
        prompt_text = prompt.strip()
        if prompt_text:
            return prompt_text
    if isinstance(prompt, dict):
        nested = _message_content_from_body(prompt)
        if nested:
            return nested
    parts = body.get("parts")
    if not isinstance(parts, list):
        return None
    texts: list[str] = []
    for part in parts:
        if isinstance(part, str):
            text = part.strip()
        elif isinstance(part, dict):
            text = str(part.get("text") or part.get("content") or "").strip()
        else:
            text = ""
        if text:
            texts.append(text)
    return "\n\n".join(texts) if texts else None


def _session_message_detail_projection(store: SQLiteStore, session_id: str, message_id: str) -> dict[str, Any]:
    store.get_session(session_id)
    message = next((candidate for candidate in store.list_session_messages(session_id) if candidate.id == message_id), None)
    if message is None:
        raise KeyError(f"Session message not found: {message_id}")
    parts = store.list_session_parts(session_id, message_id)
    return {
        "schema_version": "harness.session_message/v1",
        "ok": True,
        "session_id": session_id,
        "message_id": message_id,
        "message": message.model_dump(mode="json"),
        "parts": [part.model_dump(mode="json") for part in parts],
        "execution_started": False,
        "permission_granting": False,
    }


def _api_session_list_projection(store: SQLiteStore, query: dict[str, list[str]]) -> dict[str, Any]:
    sessions = store.list_sessions()
    limit = _optional_query_int(query, "limit")
    if limit is not None:
        sessions = sessions[-limit:] if limit else []
    return {
        "schema_version": "harness.api_sessions/v1",
        "ok": True,
        "items": [session.model_dump(mode="json") for session in sessions],
        "sessions": [session.model_dump(mode="json") for session in sessions],
        "limit": limit,
        "cursor": None,
        "execution_started": False,
        "permission_granting": False,
        "no_hidden_fallback": True,
    }


def _api_session_messages_projection(store: SQLiteStore, session_id: str, query: dict[str, list[str]]) -> dict[str, Any]:
    store.get_session(session_id)
    messages = store.list_session_messages(session_id)
    limit = _optional_query_int(query, "limit")
    if limit is not None:
        messages = messages[-limit:] if limit else []
    items = [_api_session_message_item(store, session_id, message) for message in messages]
    return {
        "schema_version": "harness.api_session_messages/v1",
        "ok": True,
        "session_id": session_id,
        "items": items,
        "messages": [item["message"] for item in items],
        "limit": limit,
        "cursor": None,
        "execution_started": False,
        "permission_granting": False,
    }


def _api_session_context_projection(store: SQLiteStore, session_id: str, query: dict[str, list[str]]) -> dict[str, Any]:
    messages = _api_session_messages_projection(store, session_id, query)
    event_limit = _optional_query_int(query, "event_limit")
    events = store.list_store_events(EventStreamType.SESSION, session_id)
    if event_limit is not None:
        events = events[-event_limit:] if event_limit else []
    return {
        "schema_version": "harness.api_session_context/v1",
        "ok": True,
        "session_id": session_id,
        "messages": messages["items"],
        "events": [event.model_dump(mode="json") for event in events],
        "context_window_loaded": False,
        "provider_execution_started": False,
        "execution_started": False,
        "permission_granting": False,
        "no_hidden_fallback": True,
    }


def _api_session_message_item(store: SQLiteStore, session_id: str, message: Any) -> dict[str, Any]:
    return {
        "info": message.model_dump(mode="json"),
        "message": message.model_dump(mode="json"),
        "parts": [
            part.model_dump(mode="json")
            for part in store.list_session_parts(session_id, message.id)
        ],
    }


def _api_session_compact_projection(store: SQLiteStore, session_id: str, body: dict[str, Any]) -> dict[str, Any]:
    store.get_session(session_id)
    return {
        "schema_version": "harness.api_session_compact/v1",
        "ok": False,
        "session_id": session_id,
        "requested": sanitize_for_logging(body),
        "error": "Provider-backed compaction is not implemented; refusing to summarize through a hidden model fallback.",
        "compacted": False,
        "summary_mutated": False,
        "provider_execution_started": False,
        "execution_started": False,
        "permission_granting": False,
        "no_hidden_fallback": True,
    }


def _api_session_wait_projection(store: SQLiteStore, session_id: str, body: dict[str, Any]) -> dict[str, Any]:
    session = store.get_session(session_id)
    timeout = _optional_body_float(body, "timeout")
    runtime = SessionRuntimeManager.for_store(store).wait(session_id, timeout=timeout if timeout is not None else 0.0)
    running = runtime.phase in {
        SessionRuntimePhase.RUNNING,
        SessionRuntimePhase.WAITING_PERMISSION,
        SessionRuntimePhase.COMPACTING,
        SessionRuntimePhase.RETRY_WAIT,
        SessionRuntimePhase.ABORTING,
    }
    return {
        "schema_version": "harness.api_session_wait/v1",
        "ok": True,
        "session_id": session_id,
        "session": session.model_dump(mode="json"),
        "requested": sanitize_for_logging(body),
        "runtime": runtime.model_dump(mode="json"),
        "waited": timeout is not None and timeout > 0,
        "agent_loop_running": running,
        "provider_execution_started": runtime.execution_enabled,
        "execution_started": runtime.execution_enabled,
        "permission_granting": False,
        "no_hidden_fallback": True,
    }


def _require_part_in_message(store: SQLiteStore, session_id: str, message_id: str, part_id: str) -> None:
    message = next((candidate for candidate in store.list_session_messages(session_id) if candidate.id == message_id), None)
    if message is None:
        raise KeyError(f"Session message not found: {message_id}")
    part = next((candidate for candidate in store.list_session_parts(session_id, message_id) if candidate.id == part_id), None)
    if part is None:
        raise KeyError(f"Session part not found in message: {part_id}")


def _project_file_metadata(project_root: Path, excludes: list[str], *, limit: int = 1000) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    for path in sorted(project_root.rglob("*")):
        if not path.is_file():
            continue
        rel = relative_to_project(project_root, path)
        if is_excluded_relative(rel, excludes):
            continue
        secret_like = is_secret_path(path)
        if secret_like:
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        files.append(
            {
                "path": rel,
                "size_bytes": size,
                "secret_like": False,
                "excluded": False,
                "contents_included": False,
            }
        )
        if len(files) >= limit:
            break
    return files


def _find_file_projection(project_root: Path, excludes: list[str], *, query: str, limit: int = 100) -> dict[str, Any]:
    search = query.strip().lower()
    matches: list[str] = []
    for item in sorted(project_root.rglob("*")):
        rel = relative_to_project(project_root, item)
        if is_excluded_relative(rel, excludes) or is_secret_path(item):
            continue
        if search in rel.lower():
            matches.append(rel)
            if len(matches) >= limit:
                break
    return {
        "schema_version": "harness.find_file/v1",
        "ok": True,
        "query": query,
        "matches": matches,
        "contents_included": False,
        "permission_granting": False,
    }


def _find_text_projection(
    project_root: Path,
    excludes: list[str],
    *,
    pattern: str,
    limit: int = 100,
    max_file_bytes: int = 1024 * 1024,
) -> dict[str, Any]:
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"Invalid search pattern: {exc}") from exc
    matches: list[dict[str, Any]] = []
    for path in sorted(project_root.rglob("*")):
        if not path.is_file():
            continue
        rel = relative_to_project(project_root, path)
        if is_excluded_relative(rel, excludes) or is_secret_path(path):
            continue
        try:
            if path.stat().st_size > max_file_bytes:
                continue
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for line_number, line in enumerate(text.splitlines(), start=1):
            submatches = [
                {"match": redact_secret_text(match.group(0)), "start": match.start(), "end": match.end()}
                for match in regex.finditer(line)
            ]
            if not submatches:
                continue
            matches.append(
                {
                    "path": rel,
                    "line_number": line_number,
                    "lines": redact_secret_text(line),
                    "submatches": submatches,
                    "contents_included": True,
                }
            )
            if len(matches) >= limit:
                return {
                    "schema_version": "harness.find_text/v1",
                    "ok": True,
                    "pattern": pattern,
                    "matches": matches,
                    "truncated": True,
                    "permission_granting": False,
                }
    return {
        "schema_version": "harness.find_text/v1",
        "ok": True,
        "pattern": pattern,
        "matches": matches,
        "truncated": False,
        "permission_granting": False,
    }


def _file_content_preview(project_root: Path, requested_path: str, excludes: list[str], *, max_bytes: int = 16 * 1024) -> dict[str, Any]:
    path = resolve_under_project(project_root, requested_path)
    rel = relative_to_project(project_root, path)
    if is_excluded_relative(rel, excludes):
        raise ValueError(f"Path is excluded from server file previews: {rel}")
    assert_not_secret_path(path)
    if not path.is_file():
        raise KeyError(f"File not found: {requested_path}")
    data = path.read_bytes()
    preview_bytes = data[:max_bytes]
    try:
        preview = preview_bytes.decode("utf-8")
        binary = False
    except UnicodeDecodeError:
        preview = ""
        binary = True
    return {
        "schema_version": "harness.file_content/v1",
        "ok": True,
        "path": rel,
        "size_bytes": len(data),
        "preview": redact_secret_text(preview),
        "truncated": len(data) > max_bytes,
        "binary": binary,
        "permission_granting": False,
    }


def _file_status_projection(project_root: Path, excludes: list[str]) -> dict[str, Any]:
    if not (project_root / ".git").exists():
        return {
            "schema_version": "harness.file_status/v1",
            "ok": True,
            "vcs": "git",
            "available": False,
            "reason": "Project is not a git worktree.",
            "files": [],
            "contents_included": False,
            "permission_granting": False,
        }
    result = subprocess.run(
        ["git", "status", "--porcelain=v1", "-z"],
        cwd=project_root,
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
    )
    if result.returncode != 0:
        return {
            "schema_version": "harness.file_status/v1",
            "ok": True,
            "vcs": "git",
            "available": False,
            "reason": "Git status failed.",
            "files": [],
            "contents_included": False,
            "permission_granting": False,
        }
    files = []
    entries = [entry for entry in result.stdout.split("\0") if entry]
    index = 0
    while index < len(entries):
        entry = entries[index]
        code = entry[:2]
        path = entry[3:] if len(entry) > 3 else ""
        old_path = None
        if code[0] in {"R", "C"} and index + 1 < len(entries):
            old_path = entries[index + 1]
            index += 1
        if path and not _status_path_blocked(project_root, path, excludes):
            files.append(
                {
                    "path": path,
                    "old_path": old_path,
                    "index_status": code[0],
                    "worktree_status": code[1],
                    "untracked": code == "??",
                    "contents_included": False,
                }
            )
        index += 1
    return {
        "schema_version": "harness.file_status/v1",
        "ok": True,
        "vcs": "git",
        "available": True,
        "files": files,
        "contents_included": False,
        "permission_granting": False,
    }


def _provider_catalog_projection(store: SQLiteStore, cfg, *, provider_id: str | None = None) -> dict[str, Any]:
    providers = list_provider_catalog(cfg)
    models = list_model_catalog(cfg)
    cache = store.replace_provider_model_catalog_cache(providers, models)
    provider_payloads = [provider.model_dump(mode="json") for provider in providers]
    if provider_id is not None:
        provider_payloads = [provider for provider in provider_payloads if provider["provider_id"] == provider_id]
        if not provider_payloads:
            raise KeyError(f"Provider not found: {provider_id}")
    return {
        "schema_version": "harness.providers/v1",
        "ok": True,
        "cache": cache,
        "providers": provider_payloads,
        "provider": provider_payloads[0] if provider_id is not None else None,
        **catalog_projection_evidence("providers_catalog_projection"),
    }


def _model_catalog_projection(store: SQLiteStore, cfg) -> dict[str, Any]:
    providers = list_provider_catalog(cfg)
    models = list_model_catalog(cfg)
    cache = store.replace_provider_model_catalog_cache(providers, models)
    return {
        "schema_version": "harness.models/v1",
        "ok": True,
        "cache": cache,
        "models": [model.model_dump(mode="json") for model in models],
        **catalog_projection_evidence("models_catalog_projection"),
    }


def _model_selection_validation_projection(cfg, raw_model_ref: str) -> dict[str, Any]:
    validation = validate_model_selection(cfg, raw_model_ref)
    return {
        "schema_version": "harness.model_selection_validation_result/v1",
        "ok": validation.executable,
        "validation": validation.model_dump(mode="json"),
        "provider_execution_started": False,
        "model_execution_started": False,
        "network_accessed": False,
        "credentials_included": False,
        "hidden_provider_fallback": False,
        "hidden_model_fallback": False,
        "no_hidden_fallback": True,
        "permission_granting": False,
        "authority_granting": False,
    }


def _body_model_ref(body: dict[str, Any]) -> str | None:
    raw_model_ref = _optional_body_text(body, "raw_model_ref") or _optional_body_text(body, "model")
    if raw_model_ref:
        return raw_model_ref
    model_id = _optional_body_text(body, "model_id") or _optional_body_text(body, "modelID")
    provider_id = _optional_body_text(body, "provider_id") or _optional_body_text(body, "providerID")
    if model_id and provider_id:
        return f"{provider_id}/{model_id}"
    return model_id


def _append_session_model_validation_event(
    store: SQLiteStore,
    cfg,
    session_id: str,
    raw_model_ref: str | None,
    *,
    source: str,
) -> dict[str, Any] | None:
    if not raw_model_ref:
        return None
    validation = validate_model_selection(cfg, raw_model_ref)
    payload = validation.model_dump(mode="json")
    store.append_store_event(
        EventStreamType.SESSION,
        session_id,
        "session.model_validation",
        {
            **payload,
            "source": source,
            "summary": "Model selection validated." if validation.executable else "Model selection blocked before execution.",
            "provider_execution_started": False,
            "model_execution_started": False,
            "hidden_provider_fallback": False,
            "hidden_model_fallback": False,
            "no_hidden_fallback": True,
            "permission_granting": False,
            "authority_granting": False,
        },
        session_id=session_id,
        redaction_state=RedactionState.NOT_REQUIRED,
    )
    return payload


def _provider_auth_projection(cfg) -> dict[str, Any]:
    providers = list_provider_catalog(cfg)
    return {
        "schema_version": "harness.provider_auth_methods/v1",
        "ok": True,
        "providers": [
            {
                "provider_id": provider.provider_id,
                "credential_status": provider.credential_status.value,
                "configured": provider.credential_status.value in {"configured", "not_required"},
                "enabled": provider.enabled,
                "auth_methods": ["environment", "config_reference"],
                "oauth_supported": False,
                "credentials_included": False,
            }
            for provider in providers
        ],
        "credentials_included": False,
        "permission_granting": False,
    }


def _provider_oauth_unsupported(provider_id: str, action: str, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "harness.provider_oauth_action/v1",
        "ok": False,
        "provider_id": provider_id,
        "action": action,
        "requested": sanitize_for_logging(body),
        "error": f"Provider OAuth {action} is not implemented yet; refusing to open browser, call network, or store credentials.",
        "oauth_supported": False,
        "browser_opened": False,
        "network_called": False,
        "credentials_stored": False,
        "filesystem_modified": False,
        "permission_granting": False,
        "no_hidden_fallback": True,
    }


def _auth_action_unsupported(action: str, provider_id: str, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "harness.auth_action/v1",
        "ok": False,
        "action": action,
        "provider_id": provider_id,
        "requested": sanitize_for_logging(body),
        "error": f"Auth {action} is not implemented through the local server; refusing to store, remove, or reveal credentials.",
        "credentials_stored": False,
        "credentials_removed": False,
        "credentials_included": False,
        "filesystem_modified": False,
        "network_called": False,
        "permission_granting": False,
        "no_hidden_fallback": True,
    }


def _path_projection(project_root: Path) -> dict[str, Any]:
    harness_dir = project_root / ".harness"
    return {
        "schema_version": "harness.path_projection/v1",
        "ok": True,
        "home": str(Path.home()),
        "state": str(harness_dir),
        "config": str(harness_dir / "config.yaml"),
        "worktree": str(project_root),
        "directory": str(project_root),
        "permission_granting": False,
    }


def _project_current_projection(project_root: Path, cfg) -> dict[str, Any]:
    workspace = build_workspace_catalog(project_root)["workspaces"][0]
    return {
        "schema_version": "harness.project_info/v1",
        "ok": True,
        "id": workspace["id"],
        "name": cfg.project_name,
        "path": str(project_root),
        "directory": str(project_root),
        "current": True,
        "commands": build_command_catalog(project_root)["commands"],
        "permission_granting": False,
    }


def _project_list_projection(project_root: Path, cfg) -> dict[str, Any]:
    current = _project_current_projection(project_root, cfg)
    return {
        "schema_version": "harness.projects/v1",
        "ok": True,
        "projects": [current],
        "registry_scope": "current_project_only",
        "permission_granting": False,
    }


def _project_action_unsupported(action: str, project_id: str | None, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "harness.project_action/v1",
        "ok": False,
        "action": action,
        "project_id": project_id,
        "requested": sanitize_for_logging(body),
        "error": f"Project {action} is not implemented through the local server; refusing to mutate project metadata, git, or commands.",
        "git_initialized": False,
        "filesystem_modified": False,
        "process_started": False,
        "permission_granting": False,
    }


def _config_update_unsupported(body: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "harness.config_update_action/v1",
        "ok": False,
        "requested": sanitize_for_logging(body),
        "error": "Config update is not implemented through the local server; refusing to rewrite project or global configuration.",
        "config_mutated": False,
        "filesystem_modified": False,
        "credentials_included": False,
        "permission_granting": False,
        "no_hidden_fallback": True,
    }


def _workspace_adapter_projection(project_root: Path) -> dict[str, Any]:
    workspace = build_workspace_catalog(project_root)["workspaces"][0]
    return {
        "schema_version": "harness.workspace_adapters/v1",
        "ok": True,
        "adapters": [
            {
                "id": "local-current-project",
                "title": "Current project",
                "workspace_id": workspace["id"],
                "path": workspace["path"],
                "create_supported": False,
                "sync_supported": False,
                "warp_supported": False,
            }
        ],
        "process_started": False,
        "filesystem_modified": False,
        "network_called": False,
        "permission_granting": False,
    }


def _workspace_status_projection(project_root: Path) -> dict[str, Any]:
    workspace = build_workspace_catalog(project_root)["workspaces"][0]
    return {
        "schema_version": "harness.workspace_status/v1",
        "ok": True,
        "statuses": [
            {
                "workspace_id": workspace["id"],
                "path": workspace["path"],
                "connected": True,
                "current": True,
                "sync_enabled": False,
            }
        ],
        "network_called": False,
        "process_started": False,
        "permission_granting": False,
    }


def _sync_history_projection(body: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "harness.sync_history/v1",
        "ok": True,
        "requested": sanitize_for_logging(body),
        "events": [],
        "history_available": False,
        "sync_started": False,
        "client_registered": False,
        "network_called": False,
        "filesystem_modified": False,
        "permission_granting": False,
    }


def _vcs_projection(project_root: Path) -> dict[str, Any]:
    if not (project_root / ".git").exists():
        return {
            "schema_version": "harness.vcs/v1",
            "ok": True,
            "available": False,
            "vcs": "git",
            "branch": None,
            "root": str(project_root),
            "process_started": False,
            "permission_granting": False,
        }
    branch = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=project_root,
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
    )
    root = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=project_root,
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
    )
    return {
        "schema_version": "harness.vcs/v1",
        "ok": True,
        "available": branch.returncode == 0,
        "vcs": "git",
        "branch": branch.stdout.strip() or None,
        "root": root.stdout.strip() or str(project_root),
        "process_started": True,
        "permission_granting": False,
    }


def _vcs_diff_projection(project_root: Path, *, raw: bool, max_preview_bytes: int = 64 * 1024) -> dict[str, Any]:
    if not (project_root / ".git").exists():
        return {
            "schema_version": "harness.vcs_diff/v1",
            "ok": True,
            "available": False,
            "vcs": "git",
            "diff": "" if raw else [],
            "raw": raw,
            "process_started": False,
            "mutation_started": False,
            "permission_granting": False,
        }
    result = subprocess.run(
        ["git", "diff", "--no-ext-diff", "--"],
        cwd=project_root,
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
    )
    diff = redact_secret_text(result.stdout[:max_preview_bytes])
    return {
        "schema_version": "harness.vcs_diff/v1",
        "ok": True,
        "available": result.returncode == 0,
        "vcs": "git",
        "raw": raw,
        "diff": diff if raw else _paths_from_diff_preview(diff),
        "preview": diff,
        "truncated": len(result.stdout.encode("utf-8")) > max_preview_bytes,
        "process_started": True,
        "mutation_started": False,
        "permission_granting": False,
    }


def _vcs_apply_unsupported(body: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "harness.vcs_apply_action/v1",
        "ok": False,
        "requested": sanitize_for_logging(body),
        "error": "VCS apply is not implemented through the local server; refusing to mutate the active worktree.",
        "patch_applied": False,
        "filesystem_modified": False,
        "git_mutation_started": False,
        "permission_granting": False,
    }


def _global_config_projection(project_root: Path, cfg) -> dict[str, Any]:
    return {
        "schema_version": "harness.global_config/v1",
        "ok": True,
        "project_root": str(project_root),
        "config": {
            "project_name": cfg.project_name,
            "chat": cfg.chat.model_dump(mode="json"),
            "backend_count": len(cfg.backends),
            "context_excludes": cfg.context_excludes,
        },
        "secrets_included": False,
        "permission_granting": False,
    }


def _global_session_events(store: SQLiteStore, *, limit: int = 250) -> list[Any]:
    events: list[Any] = []
    for session in store.list_sessions():
        events.extend(store.list_session_store_events(session.id))
    events.sort(key=lambda event: (event.created_at, event.id))
    return events[-limit:]


def _global_event_projection(store: SQLiteStore, project_root: Path) -> dict[str, Any]:
    events = _global_session_events(store)
    return {
        "schema_version": "harness.global_events/v1",
        "ok": True,
        "project_root": str(project_root),
        "events": [event.model_dump(mode="json") for event in events],
        "event_count": len(events),
        "source": "append_only_event_store",
        "transport": "json_projection",
        "permission_granting": False,
    }


def _session_status_projection(store: SQLiteStore, session_id: str) -> dict[str, Any]:
    session = store.get_session(session_id)
    events = store.list_session_store_events(session.id)
    messages = store.list_session_messages(session.id)
    children = store.list_child_sessions(session.id)
    runtime = SessionRuntimeManager.for_store(store).status(session.id)
    try:
        cwd = session_cwd_payload(store.project_root, session.metadata, load_config(store.project_root).context_excludes)
    except Exception:
        cwd = {"cwd": session.metadata.get("cwd", "."), "resolved_abs_path": None}
    return {
        "schema_version": "harness.session_status/v1",
        "ok": True,
        "session_id": session.id,
        "status": session.status.value,
        "title": session.title,
        "active_run_id": session.active_run_id,
        "active_task_id": session.active_task_id,
        "objective_id": session.objective_id,
        "summary": session.summary,
        "token_input": session.token_input,
        "token_output": session.token_output,
        "token_reasoning": session.token_reasoning,
        "token_cache_read": session.token_cache_read,
        "token_cache_write": session.token_cache_write,
        "estimated_cost_usd": str(session.estimated_cost_usd) if session.estimated_cost_usd is not None else None,
        "message_count": len(messages),
        "event_count": len(events),
        "cwd": cwd,
        "planning_mode": session_planning_mode_projection(session.metadata),
        "operator": session_operator_status_projection(
            store,
            session.id,
            project_root=store.project_root,
            cwd=str(cwd.get("cwd") or "."),
            active_tools=_operator_active_tools(),
        ),
        "runtime": runtime.model_dump(mode="json"),
        "child_session_ids": [child.id for child in children],
        "latest_ui_activation": _latest_session_ui_activation(store, session.id),
        "terminal": session.status
        in {SessionStatus.COMPLETED, SessionStatus.FAILED, SessionStatus.CANCELLED, SessionStatus.ARCHIVED},
        "process_running": runtime.process_running,
        "permission_granting": False,
    }


def _operator_active_tools() -> list[str]:
    from harness.session_tools import default_session_tool_descriptors

    return sorted(descriptor.id for descriptor in default_session_tool_descriptors() if descriptor.enabled)


def _latest_session_ui_activation(store: SQLiteStore, session_id: str) -> dict[str, Any] | None:
    events = store.list_session_store_events(session_id)
    event = next((item for item in reversed(events) if item.kind == "tui.ui_activation.applied"), None)
    if event is None:
        return None
    payload = event.payload or {}
    action = payload.get("action") if isinstance(payload.get("action"), dict) else {}
    return {
        "seq": event.seq,
        "event_id": event.id,
        "entry_id": payload.get("entry_id"),
        "source": payload.get("source"),
        "activation_kind": payload.get("activation_kind"),
        "action_type": action.get("type"),
        "evidence_status": payload.get("evidence_status") or "ui_only_persisted",
        "policy_boundary": payload.get("policy_boundary") or {
            "kind": "safe_ui_activation",
            "ui_state_only": True,
            "command_execution_allowed": False,
            "process_start_allowed": False,
            "filesystem_mutation_allowed": False,
            "permission_grant_allowed": False,
            "authority_grant_allowed": False,
        },
        "blocked_reasons": payload.get("blocked_reasons") or [],
        "ui_action_applied": bool(payload.get("ui_action_applied")),
        "command_started": bool(payload.get("command_started")),
        "process_started": bool(payload.get("process_started")),
        "filesystem_modified": bool(payload.get("filesystem_modified")),
        "permission_granting": bool(payload.get("permission_granting")),
        "authority_granting": bool(payload.get("authority_granting")),
    }


def _sessions_status_projection(store: SQLiteStore) -> dict[str, Any]:
    sessions = store.list_sessions()
    runtime_manager = SessionRuntimeManager.for_store(store)
    status_by_session = {session.id: session.status.value for session in sessions}
    active_session_ids = [
        session.id
        for session in sessions
        if session.status not in {SessionStatus.COMPLETED, SessionStatus.FAILED, SessionStatus.CANCELLED, SessionStatus.ARCHIVED}
    ]
    return {
        "schema_version": "harness.sessions_status/v1",
        "ok": True,
        "status_by_session": status_by_session,
        "sessions": [
            {
                "session_id": session.id,
                "status": session.status.value,
                "active_run_id": session.active_run_id,
                "active_task_id": session.active_task_id,
                "runtime": runtime_manager.status(session.id).model_dump(mode="json"),
                "updated_at": session.updated_at.isoformat(),
            }
            for session in sessions
        ],
        "active_session_ids": active_session_ids,
        "session_count": len(sessions),
        "execution_started": False,
        "permission_granting": False,
    }


def _session_children_projection(store: SQLiteStore, session_id: str) -> dict[str, Any]:
    parent = store.get_session(session_id)
    children = store.list_child_sessions(parent.id)
    return {
        "schema_version": "harness.session_children/v1",
        "ok": True,
        "session_id": parent.id,
        "children": [child.model_dump(mode="json") for child in children],
        "child_session_ids": [child.id for child in children],
        "execution_started": False,
        "permission_granting": False,
    }


def _session_questions_projection(store: SQLiteStore, session_id: str) -> dict[str, Any]:
    store.get_session(session_id)
    questions = [
        part
        for part in store.list_session_parts(session_id)
        if part.kind == SessionPartKind.QUESTION
    ]
    return {
        "schema_version": "harness.session_questions/v1",
        "ok": True,
        "session_id": session_id,
        "questions": [part.model_dump(mode="json") for part in questions],
        "execution_started": False,
        "permission_granting": False,
    }


def _global_question_queue_projection(store: SQLiteStore) -> dict[str, Any]:
    questions: list[Any] = []
    for session in store.list_sessions():
        questions.extend(
            part
            for part in store.list_session_parts(session.id)
            if part.kind == SessionPartKind.QUESTION
        )
    return {
        "schema_version": "harness.global_questions/v1",
        "ok": True,
        "questions": [part.model_dump(mode="json") for part in questions],
        "pending_count": len(questions),
        "execution_started": False,
        "permission_granting": False,
    }


def _find_session_question(store: SQLiteStore, question_id: str) -> Any:
    for session in store.list_sessions():
        for part in store.list_session_parts(session.id):
            if part.id == question_id and part.kind == SessionPartKind.QUESTION:
                return part
    raise KeyError(f"Session question not found: {question_id}")


def _reply_to_session_question(
    store: SQLiteStore,
    question_id: str,
    body: dict[str, Any],
    *,
    rejected: bool,
) -> dict[str, Any]:
    question = _find_session_question(store, question_id)
    answers = body.get("answers")
    if answers is None:
        answer = _optional_body_text(body, "answer") or _optional_body_text(body, "message")
        answers = [] if answer is None else [answer]
    event = store.append_store_event(
        EventStreamType.SESSION,
        question.session_id,
        "question.rejected" if rejected else "question.resolved",
        {
            "question_id": question.id,
            "message_id": question.message_id,
            "answers": answers,
            "summary": "question rejected" if rejected else "question answered",
        },
        session_id=question.session_id,
        message_id=question.message_id,
        redaction_state=RedactionState.REDACTED,
    )
    return {
        "schema_version": "harness.session_question_reply/v1",
        "ok": True,
        "session_id": question.session_id,
        "question_id": question.id,
        "rejected": rejected,
        "answers": answers,
        "event": event.model_dump(mode="json"),
        "part_mutated": False,
        "message_mutated": False,
        "execution_started": False,
        "permission_granting": False,
    }


def _global_permission_queue_projection(store: SQLiteStore) -> dict[str, Any]:
    permissions: list[Any] = []
    for session in store.list_sessions():
        permissions.extend(store.list_session_permissions(session.id, status=SessionPermissionStatus.PENDING))
    return {
        "schema_version": "harness.global_permissions/v1",
        "ok": True,
        "permissions": _session_permission_payloads(store, permissions),
        "approval_cards": _approval_cards_for_permissions(store, permissions),
        "pending_count": len(permissions),
        "execution_started": False,
        "permission_granting": False,
    }


def _session_permission_payloads(store: SQLiteStore, permissions: list[Any]) -> list[dict[str, Any]]:
    payloads = []
    for permission in permissions:
        payload = permission.model_dump(mode="json")
        try:
            payload["approval_card"] = build_session_approval_card(store, permission.session_id, permission.id)
        except Exception:
            payload["approval_card"] = None
        payloads.append(payload)
    return payloads


def _approval_cards_for_permissions(store: SQLiteStore, permissions: list[Any]) -> list[dict[str, Any]]:
    cards = []
    for permission in permissions:
        try:
            cards.append(build_session_approval_card(store, permission.session_id, permission.id))
        except Exception:
            continue
    return cards


def _session_permission_snapshot_payload(
    session_id: str,
    permissions: list[Any],
    *,
    store: SQLiteStore | None = None,
) -> dict[str, Any]:
    counts = {status.value: 0 for status in SessionPermissionStatus}
    pending_ids: list[str] = []
    approval_cards: list[dict[str, Any]] = []
    for permission in permissions:
        status = permission.status.value
        counts[status] = counts.get(status, 0) + 1
        if permission.status == SessionPermissionStatus.PENDING:
            pending_ids.append(permission.id)
            if store is not None:
                try:
                    approval_cards.append(build_session_approval_card(store, session_id, permission.id))
                except Exception:
                    pass
    return {
        "session_id": session_id,
        "counts": counts,
        "pending_permission_ids": pending_ids,
        "approval_cards": approval_cards,
        "pending_approval_cards": approval_cards,
        "pending_count": len(pending_ids),
        "blocked_on_permission": bool(pending_ids),
        "reply_route": "/sessions/{session_id}/permissions/{permission_id}/reply",
        "approval_route": "/sessions/{session_id}/approval/{approval_id}",
        "execution_started": False,
    }


def _reply_to_session_permission(
    store: SQLiteStore,
    session_id: str,
    permission_id: str,
    body: dict[str, Any],
    *,
    project_root: Path | None = None,
    resume: bool = False,
) -> dict[str, Any]:
    store.get_session(session_id)
    existing = store.get_session_permission(permission_id)
    if existing.session_id != session_id:
        raise ValueError(f"Permission {permission_id} does not belong to session {session_id}.")
    approval_card = build_session_approval_card(store, session_id, permission_id)
    reply = _optional_body_text(body, "reply") or _optional_body_text(body, "response")
    decision = _optional_body_text(body, "decision") or _optional_body_text(body, "status")
    status = _permission_reply_status(reply, decision)
    reason = _optional_body_text(body, "reason") or _optional_body_text(body, "message")
    permission = store.resolve_session_permission(
        permission_id,
        status,
        source=SessionPermissionSource.USER,
        reason=reason,
    )
    denial = None
    model_visible_error = None
    pending_tool_call = None
    resumed_result = None
    task_operator_resume = None
    resume_skipped_reason = None
    runtime_resolution = None
    if status == SessionPermissionStatus.DENIED:
        denial = persist_session_tool_denial(store, session_id, permission_id, feedback=reason)
        model_visible_error = denial.get("model_visible_error")
        if project_root is not None:
            task_operator_resume = apply_operator_task_permission_resolution(
                project_root,
                session_id,
                permission_id,
                status=status,
                feedback=reason,
            )
    elif status == SessionPermissionStatus.ALLOWED and resume:
        pending_tool_call = pending_session_tool_call_from_permission(store, session_id, permission_id)
        if pending_tool_call is None:
            resume_skipped_reason = "No persisted pending tool call evidence was found for this approval."
        elif project_root is None:
            resume_skipped_reason = "No project root was available to resume the pending tool call."
        else:
            resumed = execute_session_tool(
                store,
                project_root,
                session_id,
                str(pending_tool_call.get("tool_id") or ""),
                dict(pending_tool_call.get("arguments") or {}),
            )
            resumed_result = resumed.model_dump(mode="json")
            permission = store.get_session_permission(permission_id)
            task_operator_resume = apply_operator_task_permission_resolution(
                project_root,
                session_id,
                permission_id,
                status=status,
                resumed_result=resumed_result,
            )
    runtime_resolution = SessionRuntimeManager.for_store(store).permission_resolved(
        session_id,
        permission_id,
        decision=status.value,
        resumed=bool(resumed_result),
    )
    permissions = store.list_session_permissions(session_id)
    snapshot = _session_permission_snapshot_payload(session_id, permissions, store=store)
    tool_execution_started = bool(resumed_result and resumed_result.get("ok"))
    return {
        "schema_version": "harness.session_permission_reply/v1",
        "ok": True,
        "session_id": session_id,
        "permission_id": permission_id,
        "reply": reply,
        "decision": status.value,
        "permission": permission.model_dump(mode="json"),
        "approval_card": approval_card,
        "pending_tool_call": pending_tool_call,
        "resumed_result": resumed_result,
        "task_operator_resume": task_operator_resume,
        "runtime": runtime_resolution.model_dump(mode="json"),
        "resume_skipped_reason": resume_skipped_reason,
        "denial": denial,
        "model_visible_error": model_visible_error,
        "snapshot": snapshot,
        "execution_started": tool_execution_started,
        "tool_execution_started": tool_execution_started,
        "scope_broadened": False,
        "permission_granting": status == SessionPermissionStatus.ALLOWED,
    }


def _permission_reply_status(reply: str | None, decision: str | None) -> SessionPermissionStatus:
    if reply:
        normalized = reply.strip().lower()
        if normalized in {"once", "always", "allow", "allowed"}:
            return SessionPermissionStatus.ALLOWED
        if normalized in {"reject", "deny", "denied"}:
            return SessionPermissionStatus.DENIED
        if normalized in {"cancel", "cancelled", "canceled"}:
            return SessionPermissionStatus.CANCELLED
        raise ValueError("Unsupported permission reply. Use once, always, reject, or cancel.")
    if not decision:
        raise ValueError("Missing permission reply or decision.")
    normalized = decision.strip().lower()
    if normalized == "allow":
        normalized = SessionPermissionStatus.ALLOWED.value
    if normalized == "deny":
        normalized = SessionPermissionStatus.DENIED.value
    if normalized == "cancel":
        normalized = SessionPermissionStatus.CANCELLED.value
    return SessionPermissionStatus(normalized)


def _worktree_projection(project_root: Path) -> dict[str, Any]:
    if not (project_root / ".git").exists():
        return {
            "schema_version": "harness.worktrees/v1",
            "ok": True,
            "vcs": "git",
            "available": False,
            "reason": "Project is not a git worktree.",
            "worktrees": [],
            "mutation_supported": False,
            "process_started": False,
            "permission_granting": False,
        }
    result = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=project_root,
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
    )
    if result.returncode != 0:
        return {
            "schema_version": "harness.worktrees/v1",
            "ok": True,
            "vcs": "git",
            "available": False,
            "reason": "Git worktree list failed.",
            "worktrees": [],
            "mutation_supported": False,
            "process_started": True,
            "permission_granting": False,
        }
    return {
        "schema_version": "harness.worktrees/v1",
        "ok": True,
        "vcs": "git",
        "available": True,
        "worktrees": _parse_git_worktree_porcelain(result.stdout, project_root),
        "mutation_supported": False,
        "process_started": True,
        "permission_granting": False,
    }


def _parse_git_worktree_porcelain(output: str, project_root: Path) -> list[dict[str, Any]]:
    worktrees: list[dict[str, Any]] = []
    current: dict[str, Any] = {}
    for line in output.splitlines():
        if not line:
            if current:
                worktrees.append(_finalize_worktree_payload(current, project_root))
                current = {}
            continue
        key, _, value = line.partition(" ")
        if key == "worktree":
            if current:
                worktrees.append(_finalize_worktree_payload(current, project_root))
            current = {"path": value}
        elif key in {"HEAD", "branch"}:
            current[key.lower()] = value
        elif key in {"bare", "detached", "prunable"}:
            current[key] = True
    if current:
        worktrees.append(_finalize_worktree_payload(current, project_root))
    return worktrees


def _finalize_worktree_payload(payload: dict[str, Any], project_root: Path) -> dict[str, Any]:
    path = str(payload.get("path") or "")
    return {
        "path": path,
        "is_current": Path(path).resolve() == project_root.resolve() if path else False,
        "head": payload.get("head"),
        "branch": payload.get("branch"),
        "bare": bool(payload.get("bare", False)),
        "detached": bool(payload.get("detached", False)),
        "prunable": bool(payload.get("prunable", False)),
        "mutation_supported": False,
    }


def _worktree_action_unsupported(action: str, body: dict[str, Any], project_root: Path) -> dict[str, Any]:
    target = _normalize_worktree_target(body.get("path") or body.get("directory") or body.get("target") or body.get("name"))
    branch = _normalize_pr_ref(body.get("branch")) or "HEAD"
    plan = _worktree_action_plan(action, target, branch)
    return {
        "schema_version": "harness.worktree_action/v1",
        "ok": False,
        "action": action,
        "target": target,
        "branch": branch,
        "plan": plan,
        "execution_supported": False,
        "mutation_supported": False,
        "approval_required": True,
        "required_approval": "managed_worktree_mutation",
        "policy_boundary": plan["policy_boundary"],
        "blocked_reasons": plan["blocked_reasons"],
        "error": (
            f"Worktree {action} is not implemented yet; refusing to create, remove, reset, or mutate git worktrees implicitly."
        ),
        "git_mutation_started": False,
        "filesystem_modified": False,
        "worktree_created": False,
        "worktree_removed": False,
        "worktree_reset": False,
        "process_started": False,
        "permission_granting": False,
    }


def _normalize_worktree_target(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _managed_worktree_target_name(target: str | None) -> str | None:
    if not target:
        return None
    target_path = Path(target)
    if target_path.is_absolute():
        return None
    parts = target_path.parts
    if len(parts) == 1:
        name = parts[0]
    elif len(parts) == 3 and parts[0] == ".harness" and parts[1] == "worktrees":
        name = parts[2]
    else:
        return None
    if name in {"", ".", ".."} or any(part in {"", ".", ".."} for part in parts):
        return None
    if re.fullmatch(r"[A-Za-z0-9._-]+", name) is None:
        return None
    return name


def _managed_worktree_path(target: str | None) -> str | None:
    name = _managed_worktree_target_name(target)
    if not name:
        return None
    return str(Path(".harness") / "worktrees" / name)


def _worktree_action_plan(action: str, target: str | None, branch: str) -> dict[str, Any]:
    managed_path = _managed_worktree_path(target)
    valid_target = bool(managed_path)
    blocked_reasons = ["worktree_mutation_disabled"]
    if not target:
        blocked_reasons.insert(0, "missing_worktree_target")
    elif not valid_target:
        blocked_reasons.insert(0, "target_must_be_managed_worktree_name")
    steps: list[dict[str, Any]] = []
    if action == "create" and valid_target:
        if managed_path:
            steps.append(
                {
                    "name": "create_worktree",
                    "command": ["git", "worktree", "add", "--detach", managed_path, branch],
                    "git_mutation": True,
                    "filesystem_mutation": True,
                    "executed": False,
                }
            )
    elif action == "remove" and valid_target:
        if managed_path:
            steps.append(
                {
                    "name": "remove_worktree",
                    "command": ["git", "worktree", "remove", "--force", managed_path],
                    "git_mutation": True,
                    "filesystem_mutation": True,
                    "executed": False,
                }
            )
    elif action == "reset" and valid_target:
        if managed_path:
            steps.extend(
                [
                    {
                        "name": "fetch_default_branch",
                        "command": ["git", "-C", managed_path, "fetch", "origin", branch],
                        "network_required": True,
                        "git_mutation": True,
                        "filesystem_mutation": False,
                        "executed": False,
                    },
                    {
                        "name": "reset_worktree",
                        "command": ["git", "-C", managed_path, "reset", "--hard", f"origin/{branch}"],
                        "network_required": False,
                        "git_mutation": True,
                        "filesystem_mutation": True,
                        "executed": False,
                    },
                ]
            )
    return {
        "schema_version": "harness.worktree_plan/v1",
        "source": "opencode_worktree_flow_adapted_as_fail_closed_plan",
        "action": action,
        "target": target,
        "managed_path": managed_path,
        "branch": branch,
        "valid_target": valid_target,
        "steps": steps,
        "execution_supported": False,
        "mutation_supported": False,
        "approval_required": True,
        "required_approval": "managed_worktree_mutation",
        "policy_boundary": {
            "kind": "managed_worktree",
            "managed_root": ".harness/worktrees",
            "active_workspace_mutation_allowed": False,
            "requires_lease": True,
            "requires_approval": True,
            "cleanup_policy_required": True,
        },
        "blocked_reasons": blocked_reasons,
        "executed": False,
        "network_called": False,
        "git_mutation_started": False,
        "filesystem_modified": False,
        "worktree_created": False,
        "worktree_removed": False,
        "worktree_reset": False,
        "notes": [
            "OpenCode exposes create/remove/reset worktree endpoints; Harness records the intended operation first.",
            "Execution stays disabled until worktree leases, approval policy, and cleanup boundaries are implemented.",
        ],
    }


def _status_path_blocked(project_root: Path, relative_path: str, excludes: list[str]) -> bool:
    try:
        path = resolve_under_project(project_root, relative_path)
        rel = relative_to_project(project_root, path)
    except ValueError:
        return True
    return is_excluded_relative(rel, excludes) or is_secret_path(path)


def _pty_session_projection() -> dict[str, Any]:
    return {
        "schema_version": "harness.pty_sessions/v1",
        "ok": True,
        "sessions": [],
        "managed_pty_supported": False,
        "websocket_supported": False,
        "terminal_output_restoration_supported": False,
        "approval_required": True,
        "required_approval": "managed_pty_control",
        "policy_boundary": {
            "kind": "shell_pty_deferred",
            "source": "managed_pty_metadata_projection",
            "shell_execution_allowed": False,
            "managed_pty_allowed": False,
            "model_auto_run_allowed": False,
            "process_start_allowed": False,
            "websocket_allowed": False,
            "terminal_control_allowed": False,
            "filesystem_mutation_allowed": False,
            "permission_grant_allowed": False,
        },
        "blocked_reasons": ["shell_execution_disabled", "managed_pty_disabled", "model_auto_run_disabled"],
        "process_started": False,
        "websocket_opened": False,
        "filesystem_modified": False,
        "permission_granting": False,
    }


def _pty_shell_projection() -> dict[str, Any]:
    candidates = ["/bin/zsh", "/bin/bash", "/bin/sh"]
    return {
        "schema_version": "harness.pty_shells/v1",
        "ok": True,
        "shells": [
            {
                "path": path,
                "exists": Path(path).exists(),
                "acceptable": False,
                "reason": "Shell execution is not enabled until PTY policy gates are implemented.",
                "blocked_reasons": ["shell_execution_disabled", "managed_pty_disabled"],
            }
            for path in candidates
        ],
        "probed": False,
        "approval_required": True,
        "required_approval": "managed_pty_control",
        "policy_boundary": {
            "kind": "shell_pty_deferred",
            "source": "static_shell_candidate_projection",
            "shell_execution_allowed": False,
            "shell_probe_allowed": False,
            "managed_pty_allowed": False,
            "model_auto_run_allowed": False,
            "process_start_allowed": False,
            "filesystem_mutation_allowed": False,
            "permission_grant_allowed": False,
        },
        "blocked_reasons": ["shell_execution_disabled", "shell_probe_disabled", "managed_pty_disabled", "model_auto_run_disabled"],
        "process_started": False,
        "filesystem_modified": False,
        "permission_required": True,
        "permission_granting": False,
    }


def _pty_detail_projection(pty_id: str) -> dict[str, Any]:
    return {
        "schema_version": "harness.pty_session/v1",
        "ok": True,
        "pty_id": pty_id,
        "found": False,
        "session": None,
        "managed_pty_supported": False,
        "websocket_supported": False,
        "approval_required": True,
        "required_approval": "managed_pty_control",
        "policy_boundary": {
            "kind": "shell_pty_deferred",
            "source": "pty_detail_metadata_projection",
            "shell_execution_allowed": False,
            "managed_pty_allowed": False,
            "model_auto_run_allowed": False,
            "process_start_allowed": False,
            "websocket_allowed": False,
            "terminal_control_allowed": False,
            "filesystem_mutation_allowed": False,
            "permission_grant_allowed": False,
        },
        "blocked_reasons": ["shell_execution_disabled", "managed_pty_disabled", "model_auto_run_disabled"],
        "process_started": False,
        "process_running": False,
        "websocket_opened": False,
        "filesystem_modified": False,
        "permission_required": True,
        "permission_granting": False,
    }


def _pty_action_unsupported(action: str, body: dict[str, Any], *, pty_id: str | None = None) -> dict[str, Any]:
    plan = _pty_action_plan(action, body, pty_id=pty_id)
    return {
        "schema_version": "harness.pty_action/v1",
        "ok": False,
        "action": action,
        "pty_id": pty_id,
        "plan": plan,
        "requested": sanitize_for_logging(body),
        "error": f"PTY {action} is not implemented yet; refusing to start or control terminal processes.",
        "execution_supported": False,
        "approval_required": True,
        "required_approval": "managed_pty_control",
        "policy_boundary": plan["policy_boundary"],
        "blocked_reasons": plan["blocked_reasons"],
        "process_started": False,
        "input_written": False,
        "terminal_resized": False,
        "terminal_closed": False,
        "websocket_token_issued": False,
        "websocket_opened": False,
        "live_stream_read": False,
        "filesystem_modified": False,
        "permission_granting": False,
    }


def _pty_action_plan(action: str, body: dict[str, Any], *, pty_id: str | None = None) -> dict[str, Any]:
    command = _normalize_pr_ref(body.get("command"))
    shell = _normalize_pr_ref(body.get("shell")) or "/bin/zsh"
    cols = body.get("cols", body.get("width", 80))
    rows = body.get("rows", body.get("height", 24))
    data = body.get("data", body.get("input"))
    blocked_reasons = ["managed_pty_disabled", "pty_process_start_disabled"]
    if action == "write":
        blocked_reasons.append("terminal_input_write_disabled")
    if action in {"update", "resize"}:
        blocked_reasons.append("terminal_resize_disabled")
    if action in {"connect-token", "connect"}:
        blocked_reasons.append("pty_websocket_disabled")
    if action in {"close", "remove"}:
        blocked_reasons.append("terminal_close_disabled")
    if action != "create" and not pty_id:
        blocked_reasons.insert(0, "pty_id_required")
    steps: list[dict[str, Any]] = []
    if action == "create":
        steps.append(
            {
                "name": "create_pty_process",
                "shell": shell,
                "command": command,
                "process_start": True,
                "executed": False,
            }
        )
        steps.append(
            {
                "name": "persist_terminal_output_stream",
                "event_type": "pty.output",
                "executed": False,
            }
        )
    elif action in {"update", "resize"}:
        steps.append(
            {
                "name": "resize_terminal",
                "pty_id": pty_id,
                "cols": cols,
                "rows": rows,
                "executed": False,
            }
        )
    elif action == "write":
        data_preview = "" if data is None else str(data)[:256]
        steps.append(
            {
                "name": "write_terminal_input",
                "pty_id": pty_id,
                "data_preview": data_preview,
                "input_bytes": len(str(data or "").encode("utf-8")),
                "executed": False,
            }
        )
    elif action == "connect-token":
        steps.append(
            {
                "name": "issue_websocket_connect_token",
                "pty_id": pty_id,
                "token_issued": False,
                "executed": False,
            }
        )
    elif action == "connect":
        steps.append(
            {
                "name": "open_websocket_stream",
                "pty_id": pty_id,
                "websocket_opened": False,
                "executed": False,
            }
        )
    elif action in {"close", "remove"}:
        steps.append(
            {
                "name": "terminate_pty_process",
                "pty_id": pty_id,
                "executed": False,
            }
        )
        steps.append(
            {
                "name": "persist_terminal_close_event",
                "event_type": "pty.closed",
                "executed": False,
            }
        )
    return {
        "schema_version": "harness.pty_plan/v1",
        "source": "opencode_pty_api_adapted_as_fail_closed_plan",
        "action": action,
        "pty_id": pty_id,
        "shell": shell if action == "create" else None,
        "command": command if action == "create" else None,
        "cols": cols if action in {"update", "resize"} else None,
        "rows": rows if action in {"update", "resize"} else None,
        "steps": steps,
        "execution_supported": False,
        "approval_required": True,
        "required_approval": "managed_pty_control",
        "policy_boundary": {
            "kind": "managed_pty",
            "process_start_allowed": False,
            "input_write_allowed": False,
            "resize_allowed": False,
            "close_allowed": False,
            "websocket_token_allowed": False,
            "live_stream_allowed": False,
            "artifact_content_read_allowed": False,
            "requires_lease": True,
            "requires_approval": True,
            "requires_output_persistence": True,
        },
        "blocked_reasons": blocked_reasons,
        "executed": False,
        "process_started": False,
        "input_written": False,
        "terminal_resized": False,
        "terminal_closed": False,
        "websocket_token_issued": False,
        "websocket_opened": False,
        "live_stream_read": False,
        "filesystem_modified": False,
        "permission_required": True,
        "notes": [
            "OpenCode manages PTY sessions over HTTP and websocket routes; Harness records the intended operation first.",
            "Execution stays disabled until terminal leases, output persistence, and approval policy are implemented.",
        ],
    }


def _pty_restoration_readiness_projection(store: SQLiteStore, *, pty_id: str | None = None) -> dict[str, Any]:
    pty_stream_id = f"pty:{pty_id}" if pty_id else None
    events = store.list_store_events(EventStreamType.SESSION, pty_stream_id) if pty_stream_id else []
    event_kinds = [event.kind for event in events]
    output_events = [event for event in events if event.kind in {"pty.output", "pty.output.artifact"}]
    artifact_refs = sorted({ref for event in output_events for ref in event.artifact_refs})
    output_preview_bytes = sum(int(event.payload.get("preview_bytes") or 0) for event in output_events)
    required_events = ["pty.created", "pty.output", "pty.updated", "pty.exited"]
    missing_events = [kind for kind in required_events if kind not in set(event_kinds)]
    blockers = [
        {
            "code": "managed_pty_not_enabled",
            "message": "Harness has not enabled managed PTY process ownership.",
        },
        {
            "code": "terminal_output_restoration_not_enabled",
            "message": "Terminal output restoration is not enabled until PTY output events and artifacts are durable.",
        },
    ]
    if pty_id is None:
        blockers.append(
            {
                "code": "pty_id_required_for_replay",
                "message": "A PTY id is required before a concrete terminal tab can be restored.",
            }
        )
    if missing_events:
        blockers.append(
            {
                "code": "missing_required_pty_events",
                "message": "Required PTY lifecycle/output events are not present in the append-only event store.",
                "missing_events": missing_events,
            }
        )
    if not artifact_refs:
        blockers.append(
            {
                "code": "missing_output_artifacts",
                "message": "No PTY output artifacts are linked for restoration beyond inline previews.",
            }
        )
    blocked_reasons = [blocker["code"] for blocker in blockers]
    return {
        "schema_version": "harness.pty_restoration_readiness/v1",
        "ok": True,
        "pty_id": pty_id,
        "event_stream_type": EventStreamType.SESSION.value,
        "event_stream_id": pty_stream_id,
        "ready": False,
        "terminal_output_restoration_supported": False,
        "managed_pty_supported": False,
        "websocket_supported": False,
        "event_count": len(events),
        "output_event_count": len(output_events),
        "artifact_ref_count": len(artifact_refs),
        "artifact_refs": artifact_refs,
        "output_preview_bytes": output_preview_bytes,
        "required_events": required_events,
        "missing_events": missing_events,
        "blockers": blockers,
        "blocked_reasons": blocked_reasons,
        "policy_boundary": {
            "kind": "pty_restoration_readiness",
            "managed_pty_allowed": False,
            "live_stream_allowed": False,
            "artifact_content_read_allowed": False,
            "terminal_tab_restoration_allowed": False,
            "requires_append_only_events": True,
            "requires_artifact_evidence": True,
        },
        "required_evidence": [
            "append-only pty.created event with shell, command, cwd, dimensions, and lease id",
            "append-only pty.output events with bounded previews and artifact overflow references",
            "SHA-256, byte size, content type, producer, and redaction state for PTY output artifacts",
            "append-only pty.updated resize events for terminal dimensions",
            "append-only pty.exited or pty.deleted lifecycle event before final restoration state",
            "session or workspace link proving which operator surface owns the terminal tab",
        ],
        "restoration_plan": [
            {"step": "load_pty_lifecycle_events", "ready": bool(events), "executed": False},
            {"step": "load_output_artifacts", "ready": bool(artifact_refs), "executed": False},
            {"step": "reconstruct_scrollback", "ready": False, "executed": False},
            {"step": "restore_terminal_dimensions", "ready": "pty.updated" in set(event_kinds), "executed": False},
            {"step": "render_terminal_tab", "ready": False, "executed": False},
        ],
        "source": "opencode_pty_scrollback_model_adapted_as_harness_restoration_contract",
        "process_started": False,
        "live_stream_read": False,
        "artifact_contents_included": False,
        "permission_granting": False,
    }


def _pty_terminal_tabs_projection(store: SQLiteStore, *, pty_id: str | None = None) -> dict[str, Any]:
    tabs: list[dict[str, Any]] = []
    pty_ids = [pty_id] if pty_id else _known_pty_ids(store)
    blocked_reasons: list[str] = []
    for item in pty_ids:
        if not item:
            continue
        readiness = _pty_restoration_readiness_projection(store, pty_id=item)
        events = store.list_store_events(EventStreamType.SESSION, f"pty:{item}")
        created = next((event for event in events if event.kind == "pty.created"), None)
        updated = [event for event in events if event.kind == "pty.updated"]
        exited = next((event for event in reversed(events) if event.kind in {"pty.exited", "pty.deleted"}), None)
        output_events = [event for event in events if event.kind in {"pty.output", "pty.output.artifact"}]
        preview = "".join(str(event.payload.get("preview") or "") for event in output_events)
        if len(preview) > 16 * 1024:
            preview = preview[-16 * 1024:]
        latest_size = updated[-1].payload if updated else {}
        initial = created.payload if created else {}
        tab_blocked_reasons = list(readiness["blocked_reasons"])
        for reason in ["terminal_tab_projection_disabled", "terminal_control_disabled"]:
            if reason not in tab_blocked_reasons:
                tab_blocked_reasons.append(reason)
        for reason in tab_blocked_reasons:
            if reason not in blocked_reasons:
                blocked_reasons.append(reason)
        tabs.append(
            {
                "id": item,
                "title": str(initial.get("title") or initial.get("command") or initial.get("shell") or item),
                "status": "exited" if exited else "unavailable",
                "shell": initial.get("shell"),
                "command": initial.get("command"),
                "cwd": initial.get("cwd"),
                "cols": latest_size.get("cols") or initial.get("cols"),
                "rows": latest_size.get("rows") or initial.get("rows"),
                "event_stream_id": readiness["event_stream_id"],
                "event_count": readiness["event_count"],
                "output_event_count": readiness["output_event_count"],
                "artifact_ref_count": readiness["artifact_ref_count"],
                "artifact_refs": readiness["artifact_refs"],
                "scrollback_preview": preview,
                "scrollback_preview_truncated": bool(output_events and readiness["output_preview_bytes"] > len(preview.encode("utf-8"))),
                "restoration_ready": readiness["ready"],
                "restoration_blockers": readiness["blockers"],
                "blocked_reasons": tab_blocked_reasons,
                "policy_boundary": {
                    "kind": "pty_terminal_tab_projection",
                    "source": "persisted_pty_events",
                    "process_start_allowed": False,
                    "websocket_allowed": False,
                    "live_stream_allowed": False,
                    "artifact_content_read_allowed": False,
                    "terminal_control_allowed": False,
                    "requires_append_only_events": True,
                    "bounded_preview_only": True,
                },
                "restoration_plan": readiness["restoration_plan"],
                "source": "persisted_pty_events",
                "managed_pty_supported": False,
                "process_started": False,
                "websocket_opened": False,
                "live_stream_read": False,
                "artifact_contents_included": False,
                "permission_granting": False,
            }
        )
    if not blocked_reasons:
        blocked_reasons = ["managed_pty_not_enabled", "terminal_tab_projection_disabled"]
    return {
        "schema_version": "harness.pty_terminal_tabs/v1",
        "ok": True,
        "pty_id": pty_id,
        "tabs": tabs,
        "tab_count": len(tabs),
        "terminal_tabs_supported": False,
        "managed_pty_supported": False,
        "terminal_output_restoration_supported": False,
        "policy_boundary": {
            "kind": "pty_terminal_tabs_projection",
            "source": "persisted_pty_events",
            "process_start_allowed": False,
            "websocket_allowed": False,
            "live_stream_allowed": False,
            "artifact_content_read_allowed": False,
            "terminal_control_allowed": False,
            "requires_append_only_events": True,
            "bounded_preview_only": True,
        },
        "blocked_reasons": blocked_reasons,
        "source": "persisted_pty_events",
        "terminal_control_supported": False,
        "websocket_supported": False,
        "process_started": False,
        "websocket_opened": False,
        "live_stream_read": False,
        "artifact_contents_included": False,
        "permission_granting": False,
        "notes": [
            "Terminal tabs are projected from persisted PTY lifecycle/output events only.",
            "No PTY process, websocket stream, live terminal read, or artifact content read is started by this projection.",
        ],
    }


def _known_pty_ids(store: SQLiteStore) -> list[str]:
    with store.connect() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT stream_id
            FROM event_store
            WHERE stream_type = ? AND stream_id LIKE 'pty:%'
            ORDER BY stream_id ASC
            LIMIT 50
            """,
            (EventStreamType.SESSION.value,),
        ).fetchall()
    return [str(row["stream_id"]).removeprefix("pty:") for row in rows]


def _dev_loop_status_projection(
    store: SQLiteStore,
    project_root: Path,
    cfg,
    *,
    session_id: str | None = None,
) -> dict[str, Any]:
    pty_sessions = _pty_session_projection()
    pty_shells = _pty_shell_projection()
    terminal_tabs = _pty_terminal_tabs_projection(store)
    worktrees = _worktree_projection(project_root)
    session_payload: dict[str, Any] | None = None
    if session_id:
        store.get_session(session_id)
        diffs = _session_diff_projection(store, session_id)
        changed = _session_changed_files_projection(store, session_id, project_root, cfg.context_excludes)
        snapshots = _session_snapshots_projection(store, session_id, project_root, cfg.context_excludes)
        revert_readiness = _session_revert_readiness_projection(store, session_id, project_root, cfg.context_excludes)
        share = build_local_session_share_snapshot(store, session_id)
        session_payload = {
            "session_id": session_id,
            "snapshot_count": snapshots["snapshot_count"],
            "derived_snapshot_count": snapshots["derived_snapshot_count"],
            "explicit_snapshot_count": snapshots["explicit_snapshot_count"],
            "diff_artifact_count": len(diffs["diffs"]),
            "changed_file_count": changed["file_count"],
            "local_snapshot_available": bool(share.get("snapshot_sha256")),
            "snapshot_sha256": share.get("snapshot_sha256"),
            "revert_supported": diffs["revert_supported"],
            "unrevert_supported": diffs["unrevert_supported"],
            "selected_hunk_apply_supported": diffs["selected_hunk_apply_supported"],
            "revert_readiness_ready": revert_readiness["ready"],
            "revert_blocked_reasons": revert_readiness["blocked_reasons"],
            "revert_policy_boundary": revert_readiness["policy_boundary"],
            "snapshot_policy_boundary": snapshots["policy_boundary"],
            "mutation_started": bool(diffs["mutation_started"] or changed["mutation_started"]),
            "filesystem_modified": False,
            "git_mutation_started": False,
        }
    blocked_reasons = list(terminal_tabs.get("blocked_reasons") or [])
    for reason in [
        "worktree_mutation_disabled",
        "worktree_creation_disabled",
        "active_workspace_revert_disabled",
        "selected_hunk_apply_disabled",
    ]:
        if reason not in blocked_reasons:
            blocked_reasons.append(reason)
    if session_payload:
        for reason in session_payload.get("revert_blocked_reasons") or []:
            if reason not in blocked_reasons:
                blocked_reasons.append(reason)
    return {
        "schema_version": "harness.dev_loop_status/v1",
        "ok": True,
        "phase": "phase_9_dev_loop_audit_surface",
        "policy_boundary": {
            "kind": "dev_loop_status_projection",
            "source": "metadata_only_status",
            "terminal_process_allowed": False,
            "terminal_websocket_allowed": False,
            "terminal_live_stream_allowed": False,
            "terminal_artifact_content_read_allowed": False,
            "worktree_creation_allowed": False,
            "worktree_mutation_allowed": False,
            "active_workspace_revert_allowed": False,
            "selected_hunk_apply_allowed": False,
            "git_mutation_allowed": False,
            "filesystem_mutation_allowed": False,
            "permission_grant_allowed": False,
        },
        "blocked_reasons": blocked_reasons,
        "pty": {
            "managed_pty_supported": pty_sessions["managed_pty_supported"],
            "session_count": len(pty_sessions["sessions"]),
            "websocket_supported": pty_sessions["websocket_supported"],
            "terminal_output_restoration_supported": pty_sessions["terminal_output_restoration_supported"],
            "process_started": pty_sessions["process_started"],
            "shell_candidates": len(pty_shells["shells"]),
            "shells_acceptable": any(shell["acceptable"] for shell in pty_shells["shells"]),
        },
        "terminal_tabs": {
            "tab_count": terminal_tabs["tab_count"],
            "restorable_tab_count": len([tab for tab in terminal_tabs["tabs"] if tab["restoration_ready"]]),
            "output_event_count": sum(int(tab.get("output_event_count") or 0) for tab in terminal_tabs["tabs"]),
            "artifact_ref_count": sum(int(tab.get("artifact_ref_count") or 0) for tab in terminal_tabs["tabs"]),
            "terminal_tabs_supported": terminal_tabs["terminal_tabs_supported"],
            "terminal_output_restoration_supported": terminal_tabs["terminal_output_restoration_supported"],
            "policy_boundary": terminal_tabs["policy_boundary"],
            "blocked_reasons": terminal_tabs["blocked_reasons"],
            "source": terminal_tabs["source"],
            "terminal_control_supported": terminal_tabs["terminal_control_supported"],
            "websocket_supported": terminal_tabs["websocket_supported"],
            "process_started": terminal_tabs["process_started"],
            "websocket_opened": terminal_tabs["websocket_opened"],
            "live_stream_read": terminal_tabs["live_stream_read"],
            "artifact_contents_included": terminal_tabs["artifact_contents_included"],
            "permission_granting": terminal_tabs["permission_granting"],
        },
        "worktrees": {
            "available": worktrees["available"],
            "worktree_count": len(worktrees["worktrees"]),
            "mutation_supported": worktrees["mutation_supported"],
            "creation_supported": False,
            "reset_supported": False,
            "remove_supported": False,
            "blocked_reasons": ["worktree_mutation_disabled", "worktree_creation_disabled"],
            "policy_boundary": {
                "kind": "worktree_status_projection",
                "source": "git_worktree_list_metadata",
                "worktree_creation_allowed": False,
                "worktree_remove_allowed": False,
                "worktree_reset_allowed": False,
                "git_mutation_allowed": False,
                "filesystem_mutation_allowed": False,
            },
            "process_started": worktrees["process_started"],
            "filesystem_modified": False,
            "git_mutation_started": False,
        },
        "session": session_payload,
        "policy": {
            "permission_granting": False,
            "terminal_process_started": False,
            "terminal_websocket_opened": False,
            "terminal_live_stream_read": False,
            "terminal_artifact_contents_included": False,
            "terminal_control_started": False,
            "workspace_mutation_started": False,
            "filesystem_modified": False,
            "git_mutation_started": False,
            "revert_supported": False,
            "unrevert_supported": False,
            "selected_hunk_apply_supported": False,
            "blocked_reasons": blocked_reasons,
            "notes": [
                "This projection aggregates PTY, worktree, snapshot, and revert readiness only.",
                "No PTY process, terminal live stream, worktree mutation, file revert, unrevert, or selected-hunk apply is started by this status route.",
            ],
        },
        "permission_granting": False,
    }


def _distribution_status_projection(project_root: Path) -> dict[str, Any]:
    return {
        "schema_version": "harness.distribution_status/v1",
        "ok": True,
        "version": __version__,
        "project_root": str(project_root),
        "python_executable": sys.executable,
        "python_version": sys.version.split()[0],
        "packaging_path": "python_wheel_first",
        "standalone_binary_supported": False,
        "desktop_wrapper_supported": False,
        "local_development_install_supported": True,
        "server_supported": True,
        "session_cli_supported": True,
        "network_called": False,
        "filesystem_modified": False,
        "subprocess_started": False,
        "permission_granting": False,
    }


def _packaging_smoke_projection(project_root: Path) -> dict[str, Any]:
    pyproject = project_root / "pyproject.toml"
    return {
        "schema_version": "harness.packaging_smoke/v1",
        "ok": True,
        "packaging_path": "python_wheel_first",
        "project_root": str(project_root),
        "pyproject_exists": pyproject.exists(),
        "wheel_smoke_supported": True,
        "sdist_smoke_supported": False,
        "standalone_binary_smoke_supported": False,
        "desktop_package_smoke_supported": False,
        "execution_supported": False,
        "commands": [
            "python3 -m build --wheel --no-isolation",
            "python3 -m pip install --no-deps <wheel>",
            "harness --help",
            "harness serve --openapi --output json",
            "harness session list --output json",
        ],
        "covers": ["cli_entrypoint", "local_server_openapi", "session_cli"],
        "artifact_output_supported": False,
        "network_called": False,
        "filesystem_modified": False,
        "subprocess_started": False,
        "permission_granting": False,
    }


def _packaging_smoke_action_unsupported(body: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "harness.packaging_smoke_action/v1",
        "ok": False,
        "requested": sanitize_for_logging(body),
        "error": "Packaging smoke execution is not implemented yet; refusing to build wheels, install packages, or write artifacts.",
        "build_started": False,
        "install_started": False,
        "subprocess_started": False,
        "filesystem_modified": False,
        "network_called": False,
        "permission_granting": False,
    }


def _version_check_projection() -> dict[str, Any]:
    return {
        "schema_version": "harness.version_check/v1",
        "ok": True,
        "current_version": __version__,
        "latest_version": None,
        "update_available": None,
        "notification_enabled": False,
        "network_called": False,
        "subprocess_started": False,
        "permission_granting": False,
        "reason": "Offline version-check contract only; remote update lookup is not implemented in this phase.",
    }


def _distribution_action_unsupported(action: str, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "harness.distribution_action/v1",
        "ok": False,
        "action": action,
        "requested": sanitize_for_logging(body),
        "error": (
            f"Distribution {action} is not implemented yet; refusing to modify the Python environment, "
            "call package managers, or contact update services."
        ),
        "network_called": False,
        "filesystem_modified": False,
        "subprocess_started": False,
        "package_manager_started": False,
        "permission_granting": False,
    }


def _desktop_status_projection() -> dict[str, Any]:
    return {
        "schema_version": "harness.desktop_status/v1",
        "ok": True,
        "packaging_decision": "python_wheel_first",
        "desktop_wrapper_supported": False,
        "desktop_app_installed": False,
        "launch_supported": False,
        "auto_update_supported": False,
        "bundled_web_client_required": True,
        "requires_local_server": True,
        "roadmap": [
            "stabilize local server contracts",
            "serve static web client assets",
            "select desktop wrapper packaging",
            "add signed platform packages",
        ],
        "network_called": False,
        "process_started": False,
        "filesystem_modified": False,
        "permission_granting": False,
    }


def _desktop_action_unsupported(action: str, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "harness.desktop_action/v1",
        "ok": False,
        "action": action,
        "requested": sanitize_for_logging(body),
        "error": f"Desktop {action} is not implemented yet; refusing to launch desktop clients or package wrappers.",
        "desktop_wrapper_supported": False,
        "desktop_app_launched": False,
        "process_started": False,
        "network_called": False,
        "filesystem_modified": False,
        "permission_granting": False,
    }


def _server_lifecycle_projection(project_root: Path, *, host: str, port: int) -> dict[str, Any]:
    return {
        "schema_version": "harness.local_server_lifecycle/v1",
        "ok": True,
        "project_root": str(project_root),
        "server_url": f"http://{host}:{port}",
        "auth": "bearer",
        "dispose_supported": False,
        "remote_attach_supported": True,
        "sse_supported": True,
        "websocket_supported": False,
        "mdns_supported": False,
        "process_mutation_supported": False,
        "process_stopped": False,
        "network_called": False,
        "filesystem_modified": False,
        "permission_granting": False,
    }


def _server_mdns_projection(*, host: str, port: int) -> dict[str, Any]:
    return {
        "schema_version": "harness.local_server_mdns/v1",
        "ok": True,
        "server_url": f"http://{host}:{port}",
        "enabled": False,
        "advertised": False,
        "service_name": None,
        "service_type": "_harness._tcp",
        "lan_discovery_supported": False,
        "network_broadcast_started": False,
        "network_called": False,
        "permission_granting": False,
    }


def _server_dispose_unsupported(body: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "harness.local_server_dispose/v1",
        "ok": False,
        "requested": sanitize_for_logging(body),
        "error": "Local server dispose is not implemented yet; refusing to stop the current process from an API request.",
        "dispose_supported": False,
        "process_stopped": False,
        "filesystem_modified": False,
        "permission_granting": False,
    }


def _global_action_unsupported(action: str, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "harness.global_action/v1",
        "ok": False,
        "action": action,
        "requested": sanitize_for_logging(body),
        "error": f"Global {action} is not implemented yet; refusing to mutate process, config, install, or network state.",
        "process_stopped": False,
        "process_started": False,
        "filesystem_modified": False,
        "network_called": False,
        "permission_granting": False,
    }


def _client_log_unsupported(body: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "harness.client_log_action/v1",
        "ok": False,
        "requested": sanitize_for_logging(body),
        "error": "Client log ingestion is not implemented through the local server; refusing to write arbitrary log records.",
        "log_written": False,
        "filesystem_modified": False,
        "permission_granting": False,
    }


def _tui_action_projection(path: str, body: dict[str, Any]) -> dict[str, Any]:
    action = path.removeprefix("/tui/").replace("/", ".")
    return {
        "schema_version": "harness.tui_control_action/v1",
        "ok": True,
        "action": action,
        "requested": sanitize_for_logging(body),
        "queued": False,
        "control_queue_enabled": False,
        "live_tui_controlled": False,
        "process_started": False,
        "filesystem_modified": False,
        "execution_started": False,
        "permission_granting": False,
    }


def _web_client_projection(*, host: str, port: int) -> dict[str, Any]:
    server_url = f"http://{host}:{port}"
    return {
        "schema_version": "harness.web_client/v1",
        "ok": True,
        "server_url": server_url,
        "client_url": f"{server_url}/web",
        "client_available": False,
        "static_assets_served": False,
        "desktop_wrapper_available": False,
        "open_supported": False,
        "requires_running_server": True,
        "network_called": False,
        "browser_opened": False,
        "process_started": False,
        "filesystem_modified": False,
        "permission_granting": False,
    }


def _web_open_unsupported(body: dict[str, Any], *, host: str, port: int) -> dict[str, Any]:
    payload = _web_client_projection(host=host, port=port)
    return {
        "schema_version": "harness.web_client_action/v1",
        "ok": False,
        "action": "open",
        "requested": sanitize_for_logging(body),
        "client": payload,
        "error": "Harness web client is not implemented yet; refusing to open a browser or assume static assets exist.",
        "network_called": False,
        "browser_opened": False,
        "process_started": False,
        "filesystem_modified": False,
        "permission_granting": False,
    }


def _pr_action_unsupported(action: str, body: dict[str, Any]) -> dict[str, Any]:
    pr_ref = _normalize_pr_ref(body.get("pr") or body.get("ref") or body.get("url") or body.get("number"))
    parsed = _parse_pr_ref(pr_ref)
    plan = _pr_checkout_plan(action, pr_ref, parsed, body)
    return {
        "schema_version": "harness.pr_action/v1",
        "ok": False,
        "action": action,
        "pr": pr_ref,
        "parsed": parsed,
        "plan": plan,
        "adapter": body.get("adapter"),
        "execution_supported": False,
        "approval_required": True,
        "required_approval": "pr_checkout_or_run",
        "policy_boundary": plan["policy_boundary"],
        "blocked_reasons": plan["blocked_reasons"],
        "error": (
            f"PR {action} is not implemented yet; refusing to call network, fetch refs, checkout, create worktrees, or run adapters."
        ),
        "network_called": False,
        "git_mutation_started": False,
        "worktree_created": False,
        "checkout_started": False,
        "adapter_started": False,
        "process_started": False,
        "filesystem_modified": False,
        "permission_granting": False,
    }


def _normalize_pr_ref(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_pr_ref(pr_ref: str | None) -> dict[str, Any]:
    parsed: dict[str, Any] = {
        "kind": "unknown",
        "owner": None,
        "repo": None,
        "number": None,
        "url": pr_ref,
        "valid": False,
    }
    if not pr_ref:
        parsed["reason"] = "No PR reference was provided."
        return parsed

    text = pr_ref.strip()
    url_match = re.match(r"^https?://github\.com/([^/\s]+)/([^/\s]+)/pull/(\d+)(?:[/?#].*)?$", text)
    if url_match:
        owner, repo, number = url_match.groups()
        parsed.update({"kind": "github_url", "owner": owner, "repo": repo, "number": int(number), "valid": True})
        return parsed

    shorthand_match = re.match(r"^([^/\s#]+)/([^/\s#]+)#(\d+)$", text)
    if shorthand_match:
        owner, repo, number = shorthand_match.groups()
        parsed.update({"kind": "github_shorthand", "owner": owner, "repo": repo, "number": int(number), "valid": True})
        return parsed

    pull_ref_match = re.match(r"^(?:pull/|pr/|#)?(\d+)$", text, flags=re.IGNORECASE)
    if pull_ref_match:
        parsed.update({"kind": "number", "number": int(pull_ref_match.group(1)), "valid": True})
        return parsed

    parsed["reason"] = "Expected a GitHub PR URL, owner/repo#number, pull/number, pr/number, #number, or number."
    return parsed


def _pr_checkout_plan(
    action: str,
    pr_ref: str | None,
    parsed: dict[str, Any],
    body: dict[str, Any],
) -> dict[str, Any]:
    number = parsed.get("number")
    branch_name = f"harness/pr-{number}" if number is not None else None
    worktree_path = f".harness/pr-worktrees/pr-{number}" if number is not None else None
    fetch_ref = f"+refs/pull/{number}/head:refs/remotes/origin/pr/{number}" if number is not None else None
    valid_pr_ref = bool(parsed.get("valid"))
    blocked_reasons = [
        "network_fetch_disabled",
        "git_mutation_disabled",
        "worktree_creation_disabled",
    ]
    if action == "run":
        blocked_reasons.append("adapter_execution_disabled")
    if not valid_pr_ref:
        blocked_reasons.insert(0, "invalid_pr_ref")
    if parsed.get("owner") is None or parsed.get("repo") is None:
        blocked_reasons.append("repo_resolution_required")
    checkout_steps: list[dict[str, Any]] = []
    if valid_pr_ref and fetch_ref:
        checkout_steps.append(
            {
                "name": "fetch_pr_head",
                "command": ["git", "fetch", "origin", fetch_ref],
                "network_required": True,
                "git_mutation": True,
                "filesystem_mutation": False,
                "executed": False,
            }
        )
    if valid_pr_ref and worktree_path and branch_name:
        checkout_steps.append(
            {
                "name": "create_isolated_worktree",
                "command": ["git", "worktree", "add", "-B", branch_name, worktree_path, f"refs/remotes/origin/pr/{number}"],
                "network_required": False,
                "git_mutation": True,
                "filesystem_mutation": True,
                "executed": False,
            }
        )
    adapter = body.get("adapter")
    if action == "run":
        checkout_steps.append(
            {
                "name": "run_adapter",
                "adapter": adapter,
                "worktree": worktree_path,
                "network_required": False,
                "git_mutation": False,
                "filesystem_mutation": False,
                "executed": False,
            }
        )
    return {
        "schema_version": "harness.pr_checkout_plan/v1",
        "source": "opencode_github_flow_adapted_as_fail_closed_plan",
        "valid_pr_ref": valid_pr_ref,
        "requires_repo_resolution": parsed.get("owner") is None or parsed.get("repo") is None,
        "owner": parsed.get("owner"),
        "repo": parsed.get("repo"),
        "number": number,
        "branch": branch_name,
        "worktree_path": worktree_path,
        "fetch_ref": fetch_ref,
        "adapter": adapter,
        "steps": checkout_steps,
        "execution_supported": False,
        "approval_required": True,
        "required_approval": "pr_checkout_or_run",
        "policy_boundary": {
            "kind": "pull_request_worktree",
            "managed_root": ".harness/pr-worktrees",
            "active_workspace_mutation_allowed": False,
            "network_fetch_allowed": False,
            "git_mutation_allowed": False,
            "worktree_creation_allowed": False,
            "adapter_execution_allowed": False,
            "requires_approval": True,
            "requires_lease": True,
            "requires_repo_resolution": parsed.get("owner") is None or parsed.get("repo") is None,
        },
        "blocked_reasons": blocked_reasons,
        "executed": False,
        "network_called": False,
        "git_mutation_started": False,
        "worktree_created": False,
        "checkout_started": False,
        "adapter_started": False,
        "process_started": False,
        "filesystem_modified": False,
        "notes": [
            "OpenCode checks out PR branches directly; Harness records the intended flow first.",
            "Execution stays disabled until PR checkout policy, worktree leases, and adapter boundaries are implemented.",
        ],
        "requested": sanitize_for_logging({"pr": pr_ref, **body}),
    }


def _single_query_value(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key) or []
    return values[0] if values else None


def _optional_query_int(query: dict[str, list[str]], key: str) -> int | None:
    value = _single_query_value(query, key)
    if value is None or value == "":
        return None
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"Invalid integer query parameter: {key}") from exc
    if parsed < 0:
        raise ValueError(f"Query parameter must be non-negative: {key}")
    return parsed


def _optional_body_text(body: dict[str, Any], key: str) -> str | None:
    value = body.get(key)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_body_int(body: dict[str, Any], key: str) -> int | None:
    value = body.get(key)
    if value is None or value == "":
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid integer body field: {key}") from exc
    if parsed < 0:
        raise ValueError(f"Body field must be non-negative: {key}")
    return parsed


def _optional_body_float(body: dict[str, Any], key: str) -> float | None:
    value = body.get(key)
    if value is None or value == "":
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid number body field: {key}") from exc
    if parsed < 0:
        raise ValueError(f"Body field must be non-negative: {key}")
    return parsed


def _optional_body_bool(body: dict[str, Any], key: str) -> bool:
    value = body.get(key)
    if value is None or value == "":
        return False
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    raise ValueError(f"Invalid boolean body field: {key}")


def _optional_body_text_list(body: dict[str, Any], key: str) -> list[str]:
    value = body.get(key)
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    return [text] if text else []


def _title_from_prompt(prompt: str | None) -> str | None:
    if not prompt:
        return None
    first_line = prompt.strip().splitlines()[0].strip()
    return first_line[:80] if first_line else None


def _extract_mentions(prompt: str) -> list[dict[str, str]]:
    mentions: list[dict[str, str]] = []
    for token in prompt.split():
        if ":" not in token or not token.startswith("@"):
            continue
        kind, target = token[1:].split(":", 1)
        target = target.strip().rstrip(".,;)")
        if kind in {"file", "directory", "reference", "session"} and target:
            mentions.append({"kind": kind, "target": target})
    return mentions


def _resolve_mention(
    project_root: Path,
    store: SQLiteStore,
    mention: dict[str, str],
    cfg,
) -> dict[str, Any]:
    kind = mention["kind"]
    target = mention["target"]
    if kind == "session":
        session = store.get_session(target)
        return {
            "kind": kind,
            "target": target,
            "resolved": True,
            "session_id": session.id,
            "title": session.title,
            "status": session.status.value,
            "contents_included": False,
        }
    if kind == "reference":
        references = _reference_catalog(project_root, cfg)["references"]
        for reference in references:
            if reference["name"] == target:
                return {
                    **reference,
                    "kind": kind,
                    "reference_kind": reference["kind"],
                    "target": target,
                    "resolved": True,
                    "contents_included": False,
                }
        raise ValueError(f"Reference mention does not resolve to a configured reference: {target}")
    path = resolve_under_project(project_root, target)
    rel = relative_to_project(project_root, path)
    if is_excluded_relative(rel, cfg.context_excludes):
        raise ValueError(f"Mention target is excluded: {rel}")
    assert_not_secret_path(path)
    if kind == "file":
        if not path.is_file():
            raise ValueError(f"File mention does not resolve to a file: {target}")
        return {
            "kind": kind,
            "target": target,
            "resolved": True,
            "path": rel,
            "size_bytes": path.stat().st_size,
            "estimated_tokens": _estimate_tokens_for_bytes(path.stat().st_size),
            "contents_included": False,
        }
    if kind == "directory":
        if not path.is_dir():
            raise ValueError(f"Directory mention does not resolve to a directory: {target}")
        files = []
        for child in sorted(path.rglob("*")):
            if not child.is_file():
                continue
            child_rel = relative_to_project(project_root, child)
            if is_excluded_relative(child_rel, cfg.context_excludes) or is_secret_path(child):
                continue
            files.append({"path": child_rel, "size_bytes": child.stat().st_size, "contents_included": False})
        size_bytes = sum(int(file["size_bytes"]) for file in files)
        return {
            "kind": kind,
            "target": target,
            "resolved": True,
            "path": rel,
            "file_count": len(files),
            "size_bytes": size_bytes,
            "estimated_tokens": _estimate_tokens_for_bytes(size_bytes),
            "contents_included": False,
        }
    raise ValueError(f"Unsupported mention kind: {kind}")


def _estimate_tokens_for_bytes(size_bytes: int) -> int:
    return max(1, (size_bytes + 3) // 4) if size_bytes else 0


def _reference_catalog(project_root: Path, cfg) -> dict[str, Any]:
    references = []
    for name, reference in sorted(cfg.references.items()):
        payload: dict[str, Any] = {
            "name": name,
            "kind": reference.kind,
            "description": reference.description,
            "contents_included": False,
        }
        if reference.kind == "local":
            if not reference.path:
                payload.update({"resolved": False, "reason": "Missing local reference path."})
            else:
                path = resolve_under_project(project_root, reference.path)
                rel = relative_to_project(project_root, path)
                if is_excluded_relative(rel, cfg.context_excludes):
                    payload.update({"resolved": False, "path": rel, "reason": "Reference path is excluded."})
                elif is_secret_path(path):
                    payload.update({"resolved": False, "path": rel, "reason": "Reference path is secret-like."})
                else:
                    payload.update(
                        {
                            "resolved": path.exists(),
                            "path": rel,
                            "exists": path.exists(),
                            "directory": path.is_dir(),
                            "file": path.is_file(),
                            "size_bytes": path.stat().st_size if path.is_file() else None,
                        }
                    )
        elif reference.kind == "git":
            payload.update({"resolved": bool(reference.url), "url": reference.url, "network_required": True})
        else:
            payload.update({"resolved": False, "reason": f"Unsupported reference kind: {reference.kind}"})
        references.append(payload)
    return {
        "schema_version": "harness.references/v1",
        "ok": True,
        "references": references,
        "contents_included": False,
        "permission_granting": False,
    }


def _instruction_file_catalog(project_root: Path, excludes: list[str]) -> dict[str, Any]:
    candidates = [
        "AGENTS.md",
        "CLAUDE.md",
        "GEMINI.md",
        "CONTRIBUTING.md",
        ".cursorrules",
        ".cursor/rules",
        ".github/copilot-instructions.md",
    ]
    files = []
    for candidate in candidates:
        path = project_root / candidate
        if not path.exists():
            continue
        if path.is_dir():
            for child in sorted(path.rglob("*")):
                if child.is_file():
                    item = _instruction_file_metadata(project_root, child, excludes)
                    if item is not None:
                        files.append(item)
            continue
        item = _instruction_file_metadata(project_root, path, excludes)
        if item is not None:
            files.append(item)
    return {
        "schema_version": "harness.instructions/v1",
        "ok": True,
        "files": files,
        "contents_included": False,
        "permission_granting": False,
    }


def _instruction_file_metadata(project_root: Path, path: Path, excludes: list[str]) -> dict[str, Any] | None:
    try:
        rel = relative_to_project(project_root, path)
    except ValueError:
        return None
    if is_excluded_relative(rel, excludes) or is_secret_path(path):
        return None
    size_bytes = path.stat().st_size
    return {
        "path": rel,
        "size_bytes": size_bytes,
        "estimated_tokens": _estimate_tokens_for_bytes(size_bytes),
        "content_type": mimetypes.guess_type(path.name)[0] or "text/plain",
        "contents_included": False,
    }


def _symbol_catalog(
    project_root: Path,
    excludes: list[str],
    *,
    query: dict[str, list[str]] | None = None,
    limit: int = 500,
) -> dict[str, Any]:
    query = query or {}
    search = (_single_query_value(query, "q") or _single_query_value(query, "query") or "").strip().lower()
    requested_path = _single_query_value(query, "path")
    roots = [resolve_under_project(project_root, requested_path)] if requested_path else [project_root]
    symbols: list[dict[str, Any]] = []
    files_scanned = 0
    skipped_paths = 0
    for root in roots:
        paths = [root] if root.is_file() else sorted(root.rglob("*"))
        for path in paths:
            if not path.is_file() or path.suffix not in {".py", ".js", ".jsx", ".ts", ".tsx"}:
                continue
            rel = relative_to_project(project_root, path)
            if is_excluded_relative(rel, excludes) or is_secret_path(path):
                skipped_paths += 1
                continue
            files_scanned += 1
            symbols.extend(_static_symbols_for_file(project_root, path, search=search))
            if len(symbols) >= limit:
                symbols = symbols[:limit]
                break
        if len(symbols) >= limit:
            break
    return {
        "schema_version": "harness.symbols/v1",
        "ok": True,
        "symbols": symbols,
        "symbol_count": len(symbols),
        "files_scanned": files_scanned,
        "skipped_path_count": skipped_paths,
        "query": search or None,
        "requested_path": requested_path,
        "source": "static_scan",
        "lsp_backed": False,
        "live_lsp_supported": False,
        "diagnostics_included": False,
        "policy_boundary": {
            "kind": "static_symbol_scan",
            "process_backed_lsp_allowed": False,
            "lsp_server_launch_allowed": False,
            "contents_included": False,
            "blocked_path_filtering": True,
        },
        "blocked_reasons": ["lsp_process_launch_disabled"],
        "process_started": False,
        "contents_included": False,
        "permission_granting": False,
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


def _lsp_diagnostics_projection(cfg) -> dict[str, Any]:
    servers = []
    for name, server in sorted(cfg.lsp.servers.items()):
        enabled = bool(cfg.lsp.enabled and server.enabled)
        servers.append(
            {
                "name": name,
                "enabled": enabled,
                "configured": bool(server.command),
                "file_extensions": list(server.file_extensions),
                "command_configured": bool(server.command),
                "launch_supported": False,
                "diagnostics_collection_supported": False,
                "blocked_reasons": ["lsp_process_launch_disabled"] if enabled else ["lsp_server_disabled"],
                "process_started": False,
                "diagnostics": [],
            }
        )
    blocked_reasons = ["lsp_process_launch_disabled"]
    if not cfg.lsp.enabled:
        blocked_reasons.insert(0, "lsp_disabled")
    return {
        "schema_version": "harness.lsp_diagnostics/v1",
        "ok": True,
        "enabled": bool(cfg.lsp.enabled),
        "servers": servers,
        "diagnostics": [],
        "diagnostic_count": 0,
        "live_lsp_supported": False,
        "diagnostics_collection_supported": False,
        "policy_boundary": {
            "kind": "lsp_diagnostics_projection",
            "process_backed_lsp_allowed": False,
            "server_launch_allowed": False,
            "diagnostics_collection_allowed": False,
            "contents_included": False,
            "requires_explicit_lsp_policy": True,
        },
        "blocked_reasons": blocked_reasons,
        "process_started": False,
        "contents_included": False,
        "permission_granting": False,
    }


def _formatter_catalog(cfg) -> dict[str, Any]:
    profiles = []
    for name, profile in sorted(cfg.formatter.profiles.items()):
        profiles.append(
            {
                "name": name,
                "enabled": bool(cfg.formatter.enabled and profile.enabled),
                "configured": bool(profile.command),
                "file_extensions": list(profile.file_extensions),
                "command_configured": bool(profile.command),
                "format_on_accepted_edit": bool(profile.format_on_accepted_edit),
                "process_started": False,
            }
        )
    return {
        "schema_version": "harness.formatters/v1",
        "ok": True,
        "enabled": bool(cfg.formatter.enabled),
        "profiles": profiles,
        "process_started": False,
        "permission_granting": False,
    }


def _mcp_status_projection(cfg) -> dict[str, Any]:
    servers = []
    for name, server in sorted(cfg.mcp.servers.items()):
        enabled = bool(cfg.mcp.enabled and server.enabled)
        blocked_reasons = ["mcp_process_launch_disabled", "mcp_network_connection_disabled", "mcp_tool_execution_disabled"]
        if not enabled:
            blocked_reasons.insert(0, "mcp_server_disabled")
        servers.append(
            {
                "name": name,
                "kind": server.kind,
                "enabled": enabled,
                "description": server.description,
                "command_configured": bool(server.command),
                "url_configured": bool(server.url),
                "requires_network": bool(server.url),
                "connected": False,
                "oauth_authenticated": False,
                "tool_registration_enabled": False,
                "tool_execution_supported": False,
                "resource_reads_cached_only": True,
                "blocked_reasons": blocked_reasons,
                "process_started": False,
                "network_called": False,
                "permission_granting": False,
            }
        )
    blocked_reasons = ["mcp_process_launch_disabled", "mcp_network_connection_disabled", "mcp_tool_execution_disabled"]
    if not cfg.mcp.enabled:
        blocked_reasons.insert(0, "mcp_disabled")
    return {
        "schema_version": "harness.mcp_status/v1",
        "ok": True,
        "enabled": bool(cfg.mcp.enabled),
        "servers": servers,
        "server_count": len(servers),
        "connected": False,
        "tool_registration_enabled": False,
        "tool_execution_supported": False,
        "resource_reads_cached_only": True,
        "policy_boundary": {
            "kind": "mcp_metadata_projection",
            "process_launch_allowed": False,
            "network_connection_allowed": False,
            "oauth_allowed": False,
            "tool_registration_allowed": False,
            "tool_execution_allowed": False,
            "resource_read_source": "configured_cached_metadata_only",
            "requires_explicit_mcp_policy": True,
        },
        "blocked_reasons": blocked_reasons,
        "process_started": False,
        "network_called": False,
        "permission_granting": False,
    }


def _mcp_resources_projection(cfg) -> dict[str, Any]:
    resources: list[dict[str, Any]] = []
    for server_name, server in sorted(cfg.mcp.servers.items()):
        for resource_name, resource in sorted(server.resources.items()):
            resources.append(
                {
                    "name": resource_name,
                    "server": server_name,
                    "uri": resource.uri,
                    "enabled": bool(cfg.mcp.enabled and server.enabled and resource.enabled),
                    "cached": bool(resource.path),
                    "path": resource.path,
                    "content_type": resource.content_type,
                    "description": resource.description,
                    "contents_included": False,
                    "evidence_status": "metadata_only",
                    "resource_read_supported": False,
                    "session_tool_resource_read_supported": True,
                    "tool_execution_supported": False,
                    "requires_permission": True,
                    "policy_boundary": {
                        "kind": "mcp_cached_resource_metadata",
                        "server": server_name,
                        "process_launch_allowed": False,
                        "network_connection_allowed": False,
                        "tool_execution_allowed": False,
                        "session_tool_permission_required": True,
                        "contents_included": False,
                    },
                    "blocked_reasons": ["mcp_resource_read_requires_permission", "mcp_connection_disabled"],
                    "connected": False,
                    "process_started": False,
                    "network_called": False,
                    "permission_granting": False,
                }
            )
    return {
        "schema_version": "harness.mcp_resources/v1",
        "ok": True,
        "enabled": bool(cfg.mcp.enabled),
        "resources": resources,
        "resource_count": len(resources),
        "cached_only": True,
        "contents_included": False,
        "tool_execution_supported": False,
        "resource_read_supported": False,
        "session_tool_resource_read_supported": True,
        "policy_boundary": {
            "kind": "mcp_resources_projection",
            "process_launch_allowed": False,
            "network_connection_allowed": False,
            "tool_execution_allowed": False,
            "contents_included": False,
            "resource_read_source": "configured_cached_metadata_only",
            "requires_permission": True,
        },
        "blocked_reasons": ["mcp_connection_disabled", "mcp_tool_execution_disabled"],
        "connected": False,
        "process_started": False,
        "network_called": False,
        "permission_granting": False,
    }


def _mcp_action_unsupported(action: str, name: str | None, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "harness.mcp_action/v1",
        "ok": False,
        "action": action,
        "name": name,
        "requested": sanitize_for_logging(body),
        "error": f"MCP {action} is not implemented through the local server; refusing to connect, authenticate, launch processes, or store credentials.",
        "policy_boundary": {
            "kind": "mcp_action",
            "process_launch_allowed": False,
            "network_connection_allowed": False,
            "oauth_allowed": False,
            "credentials_storage_allowed": False,
            "tool_registration_allowed": False,
            "tool_execution_allowed": False,
            "requires_explicit_mcp_policy": True,
        },
        "blocked_reasons": ["mcp_action_disabled", "mcp_process_launch_disabled", "mcp_network_connection_disabled"],
        "connected": False,
        "oauth_started": False,
        "oauth_completed": False,
        "credentials_stored": False,
        "tool_registration_enabled": False,
        "tool_execution_started": False,
        "process_started": False,
        "network_called": False,
        "filesystem_modified": False,
        "permission_granting": False,
    }


def _plugin_catalog(project_root: Path, cfg) -> dict[str, Any]:
    plugins: list[dict[str, Any]] = []
    for name, plugin in sorted(cfg.plugins.project.items()):
        normalized_spec = plugin.spec or plugin.path or plugin.url or name
        payload: dict[str, Any] = {
            "name": name,
            "scope": "project",
            "origin": "config",
            "enabled": bool(cfg.plugins.enabled and plugin.enabled),
            "description": plugin.description,
            "version": plugin.version,
            "url": plugin.url,
            "spec": normalized_spec,
            "entrypoint": plugin.entrypoint,
            "options_configured": bool(plugin.options),
            "option_keys": sorted(plugin.options),
            "source_kind": "remote" if plugin.url else "local" if plugin.path else "spec",
            "origin_review_required": True,
            "runtime_load_supported": False,
            "tool_execution_supported": False,
            "install_supported": False,
            "update_supported": False,
            "remove_supported": False,
            "policy_boundary": {
                "kind": "plugin_metadata_projection",
                "scope": "project",
                "runtime_load_allowed": False,
                "tool_registration_allowed": False,
                "tool_execution_allowed": False,
                "filesystem_mutation_allowed": False,
                "network_fetch_allowed": False,
                "origin_review_required": True,
            },
            "blocked_reasons": ["plugin_origin_review_required", "plugin_runtime_load_disabled", "plugin_tool_execution_disabled"],
            "runtime_loaded": False,
            "tools_registered": False,
            "filesystem_modified": False,
            "network_called": False,
            "permission_granting": False,
        }
        if plugin.path:
            path = resolve_under_project(project_root, plugin.path)
            manifest = _plugin_manifest_path(path)
            payload.update(
                {
                    "path": relative_to_project(project_root, path),
                    "exists": path.exists(),
                    "directory": path.is_dir(),
                    "manifest_path": relative_to_project(project_root, manifest) if manifest else None,
                    "manifest_exists": bool(manifest and manifest.exists()),
                }
            )
        plugins.append(payload)
    for path in _global_plugin_paths():
        if not path.exists() or not path.is_dir():
            continue
        for child in sorted(path.iterdir()):
            if not child.is_dir():
                continue
            plugins.append(
                {
                    "name": child.name,
                    "scope": "global",
                    "origin": str(path),
                    "enabled": False,
                    "description": None,
                    "version": None,
                    "url": None,
                    "spec": str(child),
                    "entrypoint": None,
                    "options_configured": False,
                    "option_keys": [],
                    "source_kind": "local",
                    "path": str(child),
                    "exists": True,
                    "directory": True,
                    "manifest_path": str(_plugin_manifest_path(child)) if _plugin_manifest_path(child) else None,
                    "manifest_exists": bool(_plugin_manifest_path(child)),
                    "origin_review_required": True,
                    "runtime_load_supported": False,
                    "tool_execution_supported": False,
                    "install_supported": False,
                    "update_supported": False,
                    "remove_supported": False,
                    "policy_boundary": {
                        "kind": "plugin_metadata_projection",
                        "scope": "global",
                        "runtime_load_allowed": False,
                        "tool_registration_allowed": False,
                        "tool_execution_allowed": False,
                        "filesystem_mutation_allowed": False,
                        "network_fetch_allowed": False,
                        "origin_review_required": True,
                    },
                    "blocked_reasons": [
                        "plugin_origin_review_required",
                        "plugin_runtime_load_disabled",
                        "plugin_tool_execution_disabled",
                    ],
                    "runtime_loaded": False,
                    "tools_registered": False,
                    "filesystem_modified": False,
                    "network_called": False,
                    "permission_granting": False,
                }
            )
    return {
        "schema_version": "harness.plugins/v1",
        "ok": True,
        "enabled": bool(cfg.plugins.enabled),
        "plugins": plugins,
        "plugin_count": len(plugins),
        "project_plugin_count": len([plugin for plugin in plugins if plugin.get("scope") == "project"]),
        "global_plugin_count": len([plugin for plugin in plugins if plugin.get("scope") == "global"]),
        "runtime_loaded": False,
        "tools_registered": False,
        "tool_execution_supported": False,
        "origin_review_required": True,
        "install_supported": False,
        "update_supported": False,
        "remove_supported": False,
        "policy_boundary": {
            "kind": "plugin_catalog_metadata",
            "runtime_load_allowed": False,
            "tool_registration_allowed": False,
            "tool_execution_allowed": False,
            "filesystem_mutation_allowed": False,
            "network_fetch_allowed": False,
            "origin_review_required": True,
        },
        "blocked_reasons": ["plugin_origin_review_required", "plugin_runtime_load_disabled", "plugin_tool_execution_disabled"],
        "filesystem_modified": False,
        "network_called": False,
        "permission_granting": False,
    }


def _plugin_manifest_path(path: Path) -> Path | None:
    if path.is_file():
        return path
    for name in ("plugin.json", "package.json", "opencode.json", "opencode.jsonc"):
        candidate = path / name
        if candidate.exists():
            return candidate
    return None


def _global_plugin_paths() -> list[Path]:
    home = Path.home()
    return [
        home / ".harness" / "plugins",
        home / ".codex" / "plugins",
        home / ".agents" / "plugins",
    ]


def _skill_catalog(project_root: Path, cfg) -> dict[str, Any]:
    skills: list[dict[str, Any]] = []
    for name, skill in sorted(cfg.skills.project.items()):
        normalized_spec = skill.spec or skill.path or name
        payload: dict[str, Any] = {
            "name": name,
            "scope": "project",
            "origin": "config",
            "enabled": bool(cfg.skills.enabled and skill.enabled),
            "description": skill.description,
            "version": skill.version,
            "spec": normalized_spec,
            "source_kind": "local" if skill.path else "spec",
            "runtime_loaded": False,
            "skill_body_loaded": False,
            "tool_registered": False,
            "session_tool_load_supported": True,
            "policy_boundary": {
                "kind": "skill_metadata_projection",
                "scope": "project",
                "runtime_load_allowed": False,
                "skill_body_load_allowed": False,
                "session_tool_load_allowed_after_permission": True,
                "tool_registration_allowed": False,
                "filesystem_mutation_allowed": False,
                "network_fetch_allowed": False,
            },
            "blocked_reasons": ["skill_runtime_load_disabled", "skill_body_load_requires_permission"],
            "filesystem_modified": False,
            "network_called": False,
            "permission_granting": False,
        }
        if skill.path:
            path = resolve_under_project(project_root, skill.path)
            skill_file = path / "SKILL.md" if path.is_dir() else path
            payload.update(
                {
                    "path": relative_to_project(project_root, path),
                    "exists": path.exists(),
                    "directory": path.is_dir(),
                    "skill_file": str(skill_file),
                    "skill_file_path": relative_to_project(project_root, skill_file),
                    "skill_file_exists": skill_file.exists(),
                    "content_bytes": skill_file.stat().st_size if skill_file.exists() and skill_file.is_file() else None,
                }
            )
        skills.append(payload)
    for path in _global_skill_paths():
        if not path.exists() or not path.is_dir():
            continue
        for child in sorted(path.iterdir()):
            skill_file = child / "SKILL.md" if child.is_dir() else child
            if not skill_file.exists():
                continue
            skills.append(
                {
                    "name": child.name,
                    "scope": "global",
                    "origin": str(path),
                    "enabled": False,
                    "description": None,
                    "version": None,
                    "spec": str(child),
                    "source_kind": "local",
                    "path": str(child),
                    "exists": True,
                    "directory": child.is_dir(),
                    "skill_file": str(skill_file),
                    "skill_file_path": str(skill_file),
                    "skill_file_exists": True,
                    "content_bytes": skill_file.stat().st_size,
                    "runtime_loaded": False,
                    "skill_body_loaded": False,
                    "tool_registered": False,
                    "session_tool_load_supported": True,
                    "policy_boundary": {
                        "kind": "skill_metadata_projection",
                        "scope": "global",
                        "runtime_load_allowed": False,
                        "skill_body_load_allowed": False,
                        "session_tool_load_allowed_after_permission": True,
                        "tool_registration_allowed": False,
                        "filesystem_mutation_allowed": False,
                        "network_fetch_allowed": False,
                    },
                    "blocked_reasons": ["skill_runtime_load_disabled", "skill_body_load_requires_permission"],
                    "filesystem_modified": False,
                    "network_called": False,
                    "permission_granting": False,
                }
            )
    return {
        "schema_version": "harness.skills/v1",
        "ok": True,
        "enabled": bool(cfg.skills.enabled),
        "skills": skills,
        "runtime_loaded": False,
        "skill_body_loaded": False,
        "tool_registered": False,
        "load_supported": False,
        "session_tool_load_supported": True,
        "policy_boundary": {
            "kind": "skill_catalog_metadata",
            "runtime_load_allowed": False,
            "skill_body_load_allowed": False,
            "session_tool_load_allowed_after_permission": True,
            "tool_registration_allowed": False,
            "filesystem_mutation_allowed": False,
            "network_fetch_allowed": False,
        },
        "blocked_reasons": ["skill_runtime_load_disabled", "skill_body_load_requires_permission"],
        "filesystem_modified": False,
        "network_called": False,
        "permission_granting": False,
    }


def _global_skill_paths() -> list[Path]:
    home = Path.home()
    return [
        home / ".harness" / "skills",
        home / ".codex" / "skills",
        home / ".agents" / "skills",
    ]


def _extensibility_status_projection(project_root: Path, cfg) -> dict[str, Any]:
    mcp_status = _mcp_status_projection(cfg)
    mcp_resources = _mcp_resources_projection(cfg)
    plugins = _plugin_catalog(project_root, cfg)
    skills = _skill_catalog(project_root, cfg)
    web_tools = _web_tool_policy_projection(cfg)
    web_tool_decisions = {tool["id"]: tool["decision"] for tool in web_tools["tools"]}
    project_plugins = [plugin for plugin in plugins["plugins"] if plugin.get("scope") == "project"]
    global_plugins = [plugin for plugin in plugins["plugins"] if plugin.get("scope") == "global"]
    project_skills = [skill for skill in skills["skills"] if skill.get("scope") == "project"]
    global_skills = [skill for skill in skills["skills"] if skill.get("scope") == "global"]
    return {
        "schema_version": "harness.extensions_status/v1",
        "ok": True,
        "phase": "phase_8_extensibility_audit_surface",
        "mcp": {
            "enabled": mcp_status["enabled"],
            "server_count": len(mcp_status["servers"]),
            "resource_count": len(mcp_resources["resources"]),
            "connected": mcp_status["connected"],
            "process_started": mcp_status["process_started"],
            "network_called": mcp_status["network_called"],
            "tool_registration_enabled": mcp_status["tool_registration_enabled"],
            "cached_resources_only": mcp_resources["cached_only"],
        },
        "plugins": {
            "enabled": plugins["enabled"],
            "plugin_count": len(plugins["plugins"]),
            "project_plugin_count": len(project_plugins),
            "global_plugin_count": len(global_plugins),
            "runtime_loaded": plugins["runtime_loaded"],
            "tools_registered": plugins["tools_registered"],
            "install_supported": plugins["install_supported"],
            "update_supported": plugins["update_supported"],
            "remove_supported": plugins["remove_supported"],
            "filesystem_modified": plugins["filesystem_modified"],
            "network_called": plugins["network_called"],
        },
        "skills": {
            "enabled": skills["enabled"],
            "skill_count": len(skills["skills"]),
            "project_skill_count": len(project_skills),
            "global_skill_count": len(global_skills),
            "runtime_loaded": skills["runtime_loaded"],
            "skill_body_loaded": skills["skill_body_loaded"],
            "tool_registered": skills["tool_registered"],
            "load_supported": skills["load_supported"],
            "session_tool_load_supported": skills["session_tool_load_supported"],
            "filesystem_modified": skills["filesystem_modified"],
            "network_called": skills["network_called"],
        },
        "web_tools": {
            "enabled": web_tools["enabled"],
            "execution_supported": web_tools["execution_supported"],
            "network_called": web_tools["network_called"],
            "allowed_domains": web_tools["allowed_domains"],
            "decisions": web_tool_decisions,
        },
        "policy": {
            "permission_granting": False,
            "runtime_loaded": False,
            "process_started": False,
            "network_called": False,
            "filesystem_modified": False,
            "hidden_provider_fallback": False,
            "notes": [
                "This projection aggregates extensibility policy and metadata only.",
                "MCP connections, plugin loading, skill body loading, and web network calls are not started by this status route.",
            ],
        },
        "permission_granting": False,
    }


def _web_tool_policy_projection(cfg) -> dict[str, Any]:
    search_provider = getattr(cfg.web_tools, "search_provider", "configured_http")
    search_endpoint_configured = bool(getattr(cfg.web_tools, "search_endpoint_url", None))
    search_backend_configured = bool(search_provider != "configured_http" or search_endpoint_configured)
    tools = [
        _web_tool_projection(
            "web-fetch",
            enabled=bool(cfg.web_tools.enabled and cfg.web_tools.fetch_enabled),
            description="Fetch a URL through an external-network approval boundary.",
            cfg=cfg,
        ),
        _web_tool_projection(
            "web-search",
            enabled=bool(cfg.web_tools.enabled and cfg.web_tools.search_enabled),
            description="Search the web through an external-network approval boundary.",
            cfg=cfg,
        ),
    ]
    return {
        "schema_version": "harness.web_tools/v1",
        "ok": True,
        "enabled": bool(cfg.web_tools.enabled),
        "tools": tools,
        "allowed_domains": list(cfg.web_tools.allowed_domains),
        "search_provider": search_provider,
        "search_endpoint_configured": search_endpoint_configured,
        "search_backend_configured": search_backend_configured,
        "network_called": False,
        "execution_supported": False,
        "session_tool_execution_supported": True,
        "permission_granting": False,
    }


def _web_tool_projection(tool_id: str, *, enabled: bool, description: str, cfg) -> dict[str, Any]:
    decision = "approval_required" if enabled and cfg.web_tools.approval_required else "denied"
    if enabled and not cfg.web_tools.approval_required:
        decision = "policy_enabled"
    return {
        "id": tool_id,
        "enabled": enabled,
        "description": description,
        "boundary_kind": "external_network",
        "side_effect_level": "network",
        "decision": decision,
        "approval_required": bool(enabled and cfg.web_tools.approval_required),
        "allowed_domains": list(cfg.web_tools.allowed_domains),
        "search_provider": getattr(cfg.web_tools, "search_provider", "configured_http") if tool_id == "web-search" else None,
        "search_endpoint_configured": bool(getattr(cfg.web_tools, "search_endpoint_url", None)) if tool_id == "web-search" else None,
        "search_backend_configured": (
            bool(
                getattr(cfg.web_tools, "search_provider", "configured_http") != "configured_http"
                or getattr(cfg.web_tools, "search_endpoint_url", None)
            )
            if tool_id == "web-search"
            else None
        ),
        "network_called": False,
        "execution_supported": False,
        "session_tool_execution_supported": True,
        "permission_granting": False,
    }


def _prepare_attachment(project_root: Path, requested_path: str, excludes: list[str]) -> dict[str, Any]:
    path = resolve_under_project(project_root, requested_path)
    rel = relative_to_project(project_root, path)
    if is_excluded_relative(rel, excludes):
        raise ValueError(f"Attachment path is excluded: {rel}")
    assert_not_secret_path(path)
    if not path.is_file():
        raise ValueError(f"Attachment path does not resolve to a file: {requested_path}")
    size_bytes = path.stat().st_size
    content_type, _ = mimetypes.guess_type(path.name)
    max_inline_bytes = 256 * 1024
    max_attachment_bytes = 10 * 1024 * 1024
    image = bool((content_type or "").startswith("image/"))
    image_metadata = _image_metadata(path, content_type) if image else {}
    max_image_pixels = 20_000_000
    image_pixels = image_metadata.get("width", 0) * image_metadata.get("height", 0)
    return {
        "attachment_kind": "file_ref",
        "path": rel,
        "size_bytes": size_bytes,
        "content_type": content_type or "application/octet-stream",
        "image": image,
        "image_width": image_metadata.get("width"),
        "image_height": image_metadata.get("height"),
        "image_pixels": image_pixels if image else None,
        "image_metadata_available": bool(image_metadata),
        "max_image_pixels": max_image_pixels,
        "image_requires_resize": bool(image and image_pixels > max_image_pixels),
        "estimated_tokens": _estimate_tokens_for_bytes(size_bytes),
        "accepted": size_bytes <= max_attachment_bytes and not (image and image_pixels > max_image_pixels),
        "requires_artifact_overflow": size_bytes > max_inline_bytes,
        "max_inline_bytes": max_inline_bytes,
        "max_attachment_bytes": max_attachment_bytes,
        "contents_included": False,
    }


def _image_metadata(path: Path, content_type: str | None) -> dict[str, int]:
    data = path.read_bytes()[:32]
    if content_type == "image/png" and len(data) >= 24 and data.startswith(b"\x89PNG\r\n\x1a\n"):
        width, height = struct.unpack(">II", data[16:24])
        return {"width": int(width), "height": int(height)}
    if content_type in {"image/jpeg", "image/jpg"}:
        return _jpeg_dimensions(path)
    return {}


def _jpeg_dimensions(path: Path) -> dict[str, int]:
    data = path.read_bytes()
    index = 2
    if not data.startswith(b"\xff\xd8"):
        return {}
    while index + 9 < len(data):
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        index += 2
        if marker in {0xD8, 0xD9}:
            continue
        if index + 2 > len(data):
            break
        segment_length = int.from_bytes(data[index:index + 2], "big")
        if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
            if index + 7 <= len(data):
                height = int.from_bytes(data[index + 3:index + 5], "big")
                width = int.from_bytes(data[index + 5:index + 7], "big")
                return {"width": width, "height": height}
            break
        index += max(segment_length, 2)
    return {}


def _context_budget_estimate(project_root: Path, store: SQLiteStore, cfg, body: dict[str, Any]) -> dict[str, Any]:
    prompt = _optional_body_text(body, "prompt") or ""
    attachment_paths = body.get("attachment_paths") or []
    if not isinstance(attachment_paths, list):
        raise ValueError("attachment_paths must be a list when provided.")
    include_instructions = bool(body.get("include_instructions", False))
    prompt_bytes = len(prompt.encode("utf-8"))
    prompt_tokens = _estimate_tokens_for_bytes(prompt_bytes)
    mentions = [_resolve_mention(project_root, store, mention, cfg) for mention in _extract_mentions(prompt)]
    attachments = [_prepare_attachment(project_root, str(path), cfg.context_excludes) for path in attachment_paths]
    instructions = _instruction_file_catalog(project_root, cfg.context_excludes)["files"] if include_instructions else []
    items = [{"kind": "prompt", "size_bytes": prompt_bytes, "estimated_tokens": prompt_tokens, "contents_included": False}]
    for item in mentions:
        payload = dict(item)
        payload["kind"] = f"mention:{item['kind']}"
        items.append(payload)
    for item in attachments:
        payload = dict(item)
        payload["kind"] = "attachment"
        items.append(payload)
    for item in instructions:
        payload = dict(item)
        payload["kind"] = "instruction"
        items.append(payload)
    total_bytes = sum(int(item.get("size_bytes") or 0) for item in items)
    total_tokens = sum(int(item.get("estimated_tokens") or 0) for item in items)
    budget_tokens = body.get("budget_tokens")
    if budget_tokens is not None:
        try:
            budget_tokens = int(budget_tokens)
        except (TypeError, ValueError) as exc:
            raise ValueError("budget_tokens must be an integer when provided.") from exc
    return {
        "prompt_bytes": prompt_bytes,
        "prompt_estimated_tokens": prompt_tokens,
        "total_bytes": total_bytes,
        "total_estimated_tokens": total_tokens,
        "budget_tokens": budget_tokens,
        "within_budget": None if budget_tokens is None else total_tokens <= budget_tokens,
        "items": items,
        "contents_included": False,
        "permission_granting": False,
    }


def _all_artifact_metadata(store: SQLiteStore) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    for run in store.list_runs():
        for artifact in store.list_artifacts(run.id):
            payload = artifact.model_dump(mode="json")
            payload["contents_included"] = False
            artifacts.append(payload)
    return artifacts


_DIFF_ARTIFACT_KINDS = {
    "isolated_unified_diff",
    "isolated_diff",
    "diff",
    "patch",
    "diff.patch",
    "session_tool_patch",
}


def _session_diff_projection(store: SQLiteStore, session_id: str, *, max_preview_bytes: int = 16 * 1024) -> dict[str, Any]:
    store.get_session(session_id)
    diffs: list[dict[str, Any]] = []
    seen: set[str] = set()
    session_run_ids = {run.id for run in store.list_runs() if run.session_id == session_id}
    for run_id in session_run_ids:
        for artifact in store.list_artifacts(run_id):
            if artifact.id in seen or not _is_diff_artifact(artifact.kind):
                continue
            if artifact.session_id not in {None, session_id}:
                continue
            seen.add(artifact.id)
            diffs.append(_diff_artifact_projection(artifact, max_preview_bytes=max_preview_bytes))
    diffs.sort(key=lambda item: str(item["created_at"]))
    return {
        "schema_version": "harness.session_diffs/v1",
        "ok": True,
        "session_id": session_id,
        "diffs": diffs,
        "contents_included": True,
        "preview_max_bytes": max_preview_bytes,
        "revert_supported": False,
        "unrevert_supported": False,
        "selected_hunk_apply_supported": False,
        "mutation_started": False,
        "permission_granting": False,
    }


def _session_changed_files_projection(
    store: SQLiteStore,
    session_id: str,
    project_root: Path,
    excludes: list[str],
    *,
    max_preview_bytes: int = 16 * 1024,
) -> dict[str, Any]:
    diffs = _session_diff_projection(store, session_id, max_preview_bytes=max_preview_bytes)
    by_path: dict[str, dict[str, Any]] = {}
    for diff in diffs["diffs"]:
        for path in _paths_from_diff_preview(str(diff.get("preview") or "")):
            if is_excluded_relative(path, excludes):
                continue
            entry = by_path.setdefault(
                path,
                {
                    "path": path,
                    "sources": [],
                    "diff_artifact_ids": [],
                    "active_repo_status": None,
                    "contents_included": False,
                    "revert_supported": False,
                    "selected_hunk_apply_supported": False,
                },
            )
            if "diff_artifact" not in entry["sources"]:
                entry["sources"].append("diff_artifact")
            entry["diff_artifact_ids"].append(diff["id"])
    active_status = _file_status_projection(project_root, excludes)
    for item in active_status.get("files", []):
        path = item["path"]
        entry = by_path.setdefault(
            path,
            {
                "path": path,
                "sources": [],
                "diff_artifact_ids": [],
                "active_repo_status": None,
                "contents_included": False,
                "revert_supported": False,
                "selected_hunk_apply_supported": False,
            },
        )
        if "active_repo_status" not in entry["sources"]:
            entry["sources"].append("active_repo_status")
        entry["active_repo_status"] = {
            "index_status": item.get("index_status"),
            "worktree_status": item.get("worktree_status"),
            "untracked": item.get("untracked"),
            "old_path": item.get("old_path"),
            "contents_included": False,
        }
    files = sorted(by_path.values(), key=lambda item: item["path"])
    return {
        "schema_version": "harness.session_changed_files/v1",
        "ok": True,
        "session_id": session_id,
        "files": files,
        "file_count": len(files),
        "diff_artifact_count": len(diffs["diffs"]),
        "active_repo_status_available": bool(active_status.get("available")),
        "active_repo_status_error": active_status.get("error"),
        "contents_included": False,
        "process_started": bool(active_status.get("process_started")),
        "mutation_started": False,
        "revert_supported": False,
        "selected_hunk_apply_supported": False,
        "permission_granting": False,
    }


def _session_snapshots_projection(
    store: SQLiteStore,
    session_id: str,
    project_root: Path,
    excludes: list[str],
    *,
    message_id: str | None = None,
) -> dict[str, Any]:
    store.get_session(session_id)
    messages = store.list_session_messages(session_id)
    if message_id is not None:
        messages = [message for message in messages if message.id == message_id]
        if not messages:
            raise KeyError(f"Session message not found: {message_id}")
    all_parts = store.list_session_parts(session_id)
    parts_by_message: dict[str, list[Any]] = {}
    for part in all_parts:
        parts_by_message.setdefault(part.message_id, []).append(part)
    snapshots: list[dict[str, Any]] = []
    evidence_contract = _snapshot_evidence_contract()
    for message in messages:
        message_parts = parts_by_message.get(message.id, [])
        run_ids = sorted(
            {
                run_id
                for run_id in [message.run_id, *(part.run_id for part in message_parts)]
                if run_id
            }
        )
        artifact_ids = sorted({part.artifact_id for part in message_parts if part.artifact_id})
        diff_artifacts: list[dict[str, Any]] = []
        for run_id in run_ids:
            for artifact in store.list_artifacts(run_id):
                if artifact.session_id not in {None, session_id}:
                    continue
                if artifact.id not in artifact_ids:
                    artifact_ids.append(artifact.id)
                if _is_diff_artifact(artifact.kind):
                    diff_artifacts.append(_diff_artifact_snapshot_reference(artifact))
        changed_paths = sorted(
            {
                path
                for artifact in diff_artifacts
                for path in artifact["changed_paths"]
                if not is_excluded_relative(path, excludes)
            }
        )
        explicit_snapshot_parts = [part for part in message_parts if part.kind == SessionPartKind.SNAPSHOT_REF]
        for part in explicit_snapshot_parts:
            snapshots.append(
                {
                    "snapshot_id": str(part.metadata.get("snapshot_id") or part.id),
                    "snapshot_kind": str(part.metadata.get("snapshot_kind") or "snapshot_ref"),
                    "source": "session_part",
                    "session_id": session_id,
                    "message_id": message.id,
                    "message_role": message.role.value,
                    "run_ids": run_ids,
                    "artifact_ids": sorted(set(artifact_ids)),
                    "diff_artifacts": diff_artifacts,
                    "changed_paths": changed_paths,
                    "changed_file_count": len(changed_paths),
                    "part_id": part.id,
                    "reversible": bool(part.metadata.get("reversible", False)),
                    "mutation_reversibility": "not_reversible_metadata_only",
                    "evidence_contract": evidence_contract,
                    "revert_supported": False,
                    "unrevert_supported": False,
                    "selected_hunk_apply_supported": False,
                    "mutation_started": False,
                    "filesystem_modified": False,
                    "git_mutation_started": False,
                    "permission_granting": False,
                }
            )
        if not explicit_snapshot_parts and (run_ids or diff_artifacts):
            seed = json.dumps(
                {
                    "session_id": session_id,
                    "message_id": message.id,
                    "run_ids": run_ids,
                    "artifact_ids": sorted(set(artifact_ids)),
                    "changed_paths": changed_paths,
                },
                sort_keys=True,
            ).encode("utf-8")
            snapshots.append(
                {
                    "snapshot_id": "snap_" + hashlib.sha256(seed).hexdigest()[:16],
                    "snapshot_kind": "message_effects_metadata",
                    "source": "derived_from_message_run_artifacts",
                    "session_id": session_id,
                    "message_id": message.id,
                    "message_role": message.role.value,
                    "run_ids": run_ids,
                    "artifact_ids": sorted(set(artifact_ids)),
                    "diff_artifacts": diff_artifacts,
                    "changed_paths": changed_paths,
                    "changed_file_count": len(changed_paths),
                    "part_id": None,
                    "reversible": False,
                    "mutation_reversibility": "not_reversible_metadata_only",
                    "evidence_contract": evidence_contract,
                    "revert_supported": False,
                    "unrevert_supported": False,
                    "selected_hunk_apply_supported": False,
                    "mutation_started": False,
                    "filesystem_modified": False,
                    "git_mutation_started": False,
                    "permission_granting": False,
                }
            )
    return {
        "schema_version": "harness.session_snapshots/v1",
        "ok": True,
        "session_id": session_id,
        "message_id": message_id,
        "snapshots": snapshots,
        "snapshot_count": len(snapshots),
        "derived_snapshot_count": len([snapshot for snapshot in snapshots if snapshot["source"] == "derived_from_message_run_artifacts"]),
        "explicit_snapshot_count": len([snapshot for snapshot in snapshots if snapshot["source"] == "session_part"]),
        "mutation_reversibility": "not_reversible_metadata_only",
        "evidence_contract": evidence_contract,
        "policy_boundary": {
            "kind": "snapshot_metadata_projection",
            "active_workspace_mutation_allowed": False,
            "requires_snapshot_pair": True,
            "requires_apply_back_boundary": True,
            "requires_approval": True,
        },
        "revert_supported": False,
        "unrevert_supported": False,
        "selected_hunk_apply_supported": False,
        "mutation_started": False,
        "filesystem_modified": False,
        "git_mutation_started": False,
        "permission_granting": False,
    }


def _session_revert_readiness_projection(
    store: SQLiteStore,
    session_id: str,
    project_root: Path,
    excludes: list[str],
    *,
    message_id: str | None = None,
) -> dict[str, Any]:
    snapshots = _session_snapshots_projection(store, session_id, project_root, excludes, message_id=message_id)
    diffs = _session_diff_projection(store, session_id)
    changed = _session_changed_files_projection(store, session_id, project_root, excludes)
    target_snapshots = snapshots["snapshots"]
    changed_paths = sorted({path for snapshot in target_snapshots for path in snapshot.get("changed_paths", [])})
    diff_artifact_ids = sorted(
        {artifact["id"] for snapshot in target_snapshots for artifact in snapshot.get("diff_artifacts", [])}
    )
    active_conflicts = [
        {
            "path": item["path"],
            "active_repo_status": item.get("active_repo_status"),
            "sources": item.get("sources") or [],
        }
        for item in changed["files"]
        if item.get("active_repo_status") and (not changed_paths or item["path"] in changed_paths)
    ]
    blockers = [
        {
            "code": "active_revert_policy_missing",
            "message": "Harness has not enabled an active session revert policy for file mutation.",
        },
        {
            "code": "apply_back_boundary_missing",
            "message": "Revert must be tied to isolated workspace/apply-back evidence before touching the active project.",
        },
        {
            "code": "snapshot_restore_not_implemented",
            "message": "Snapshot restore/revert/unrevert execution is not implemented in Harness yet.",
        },
    ]
    if not target_snapshots:
        blockers.append(
            {
                "code": "no_message_snapshot_metadata",
                "message": "No explicit or derived snapshot metadata is available for the requested scope.",
            }
        )
    if not diff_artifact_ids:
        blockers.append(
            {
                "code": "no_diff_artifacts",
                "message": "No diff artifacts are linked to the requested scope.",
            }
        )
    if active_conflicts:
        blockers.append(
            {
                "code": "active_workspace_changes_present",
                "message": "The active workspace has changed-file status for paths in this scope; Harness will not imply safe undo.",
            }
        )
    blocker_codes = [blocker["code"] for blocker in blockers]
    return {
        "schema_version": "harness.session_revert_readiness/v1",
        "ok": True,
        "session_id": session_id,
        "message_id": message_id,
        "ready": False,
        "mutation_reversibility": "not_reversible_readiness_only",
        "policy_boundary": {
            "kind": "session_revert_readiness",
            "active_workspace_mutation_allowed": False,
            "requires_snapshot_pair": True,
            "requires_apply_back_boundary": True,
            "requires_approval": True,
            "requires_verification_artifact": True,
        },
        "revert_supported": False,
        "unrevert_supported": False,
        "selected_hunk_apply_supported": False,
        "snapshot_count": snapshots["snapshot_count"],
        "derived_snapshot_count": snapshots["derived_snapshot_count"],
        "explicit_snapshot_count": snapshots["explicit_snapshot_count"],
        "diff_artifact_count": len(diff_artifact_ids),
        "session_diff_artifact_count": len(diffs["diffs"]),
        "changed_file_count": len(changed_paths),
        "active_conflict_count": len(active_conflicts),
        "changed_paths": changed_paths,
        "diff_artifact_ids": diff_artifact_ids,
        "active_conflicts": active_conflicts,
        "blockers": blockers,
        "blocked_reasons": blocker_codes,
        "required_evidence": [
            "append-only session event for the requested revert action",
            "message-linked before and after snapshot ids",
            "diff artifacts with SHA-256, size, content type, producer, and redaction state",
            "changed-path set after blocked-path and secret-path filtering",
            "isolated workspace or worktree lease proving the mutation boundary",
            "explicit approval decision for revert/unrevert or selected-hunk apply",
            "post-revert verification artifact before apply-back to an active project",
        ],
        "execution_plan": [
            {"step": "resolve_message_scope", "ready": bool(target_snapshots), "executed": False},
            {"step": "validate_snapshot_pair", "ready": False, "executed": False},
            {"step": "validate_diff_artifacts", "ready": bool(diff_artifact_ids), "executed": False},
            {"step": "check_active_workspace_conflicts", "ready": not active_conflicts, "executed": False},
            {"step": "request_revert_approval", "ready": False, "executed": False},
            {"step": "apply_revert_in_isolated_boundary", "ready": False, "executed": False},
            {"step": "verify_and_record_evidence", "ready": False, "executed": False},
        ],
        "source": "opencode_snapshot_revert_model_adapted_as_harness_readiness_contract",
        "mutation_started": False,
        "filesystem_modified": False,
        "git_mutation_started": False,
        "permission_granting": False,
    }


def _diff_artifact_snapshot_reference(artifact) -> dict[str, Any]:
    data = artifact.path.read_bytes()
    try:
        preview = data[:16 * 1024].decode("utf-8")
    except UnicodeDecodeError:
        preview = ""
    return {
        "id": artifact.id,
        "run_id": artifact.run_id,
        "kind": artifact.kind,
        "sha256": artifact.sha256,
        "size_bytes": artifact.size_bytes,
        "content_type": artifact.metadata.get("content_type")
        or ("text/x-patch" if _is_diff_artifact(artifact.kind) else None)
        or mimetypes.guess_type(str(artifact.path))[0]
        or "application/octet-stream",
        "redaction_state": artifact.redaction_state,
        "producer": artifact.producer,
        "evidence_status": artifact.evidence_status,
        "changed_paths": _paths_from_diff_preview(preview),
        "contents_included": False,
        "revert_supported": False,
        "selected_hunk_apply_supported": False,
    }


def _snapshot_evidence_contract() -> dict[str, Any]:
    return {
        "contents_included": False,
        "artifact_files_included": False,
        "diff_preview_max_bytes": 16 * 1024,
        "requires_sha256": True,
        "requires_size_bytes": True,
        "requires_content_type": True,
        "requires_redaction_state": True,
        "requires_producer": True,
        "append_only_events_required_for_mutation": True,
    }


def _paths_from_diff_preview(preview: str) -> list[str]:
    paths: list[str] = []
    for line in preview.splitlines():
        candidate: str | None = None
        if line.startswith("diff --git "):
            parts = line.split()
            if len(parts) >= 4:
                candidate = _strip_diff_prefix(parts[3])
        elif line.startswith("+++ ") or line.startswith("--- "):
            token = line[4:].strip().split("\t", 1)[0]
            candidate = _strip_diff_prefix(token)
        if candidate and candidate != "/dev/null" and candidate not in paths:
            paths.append(candidate)
    return paths


def _strip_diff_prefix(path: str) -> str:
    if path.startswith("a/") or path.startswith("b/"):
        return path[2:]
    return path


def _is_diff_artifact(kind: str) -> bool:
    normalized = kind.replace("-", "_")
    return kind in _DIFF_ARTIFACT_KINDS or "diff" in normalized or "patch" in normalized


def _diff_artifact_projection(artifact, *, max_preview_bytes: int) -> dict[str, Any]:
    data = artifact.path.read_bytes()
    preview_bytes = data[:max_preview_bytes]
    try:
        preview = preview_bytes.decode("utf-8")
        binary = False
    except UnicodeDecodeError:
        preview = ""
        binary = True
    payload = artifact.model_dump(mode="json")
    payload.update(
        {
            "preview": redact_secret_text(preview),
            "preview_truncated": len(data) > max_preview_bytes,
            "binary": binary,
            "contents_included": True,
            "revert_supported": False,
            "selected_hunk_apply_supported": False,
        }
    )
    return payload


def _session_mutation_unsupported(action: str, session_id: str, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "harness.session_mutation_action/v1",
        "ok": False,
        "session_id": session_id,
        "action": action,
        "message_id": body.get("message_id"),
        "artifact_id": body.get("artifact_id"),
        "hunk_id": body.get("hunk_id"),
        "error": (
            f"Session {action} is not implemented yet; refusing to mutate active workspace files or git state."
        ),
        "mutation_started": False,
        "git_mutation_started": False,
        "filesystem_modified": False,
        "revert_supported": False,
        "unrevert_supported": False,
        "selected_hunk_apply_supported": False,
        "permission_granting": False,
    }


def _session_init_unsupported(session_id: str, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "harness.session_init_action/v1",
        "ok": False,
        "session_id": session_id,
        "requested": sanitize_for_logging(body),
        "error": "Session init is not implemented through the local server; refusing to generate or write project instruction files.",
        "agents_file_written": False,
        "filesystem_modified": False,
        "provider_execution_started": False,
        "execution_started": False,
        "permission_granting": False,
        "no_hidden_fallback": True,
    }


def _session_shell_unsupported(session_id: str, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "harness.session_shell_action/v1",
        "ok": False,
        "session_id": session_id,
        "requested": sanitize_for_logging(body),
        "error": "Session shell execution is not implemented through the local server; refusing to start shell processes.",
        "process_started": False,
        "command_executed": False,
        "tool_execution_started": False,
        "provider_execution_started": False,
        "execution_started": False,
        "permission_granting": False,
        "no_hidden_fallback": True,
    }


def _session_tool_execution_response(
    store: SQLiteStore,
    project_root: Path,
    session_id: str,
    tool_id: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    try:
        result = execute_session_tool(store, project_root, session_id, tool_id, arguments)
    except CwdResolutionError as exc:
        return {
            "schema_version": "harness.session_shell_action/v1" if tool_id == "shell" else "harness.session_tool_execution_response/v1",
            "ok": False,
            "session_id": session_id,
            "tool_id": tool_id,
            "lifecycle": "direct_tool_execution",
            "save_point_emitted": False,
            "error": cwd_recovery_message(exc),
            "error_type": "invalid_cwd",
            "recovery": {"repair_command": "harness doctor --repair", "reset_cwd": "."},
            "permission_required": False,
            "permission_id": None,
            "approval_card": None,
            "process_started": False,
            "command_executed": False,
            "tool_execution_started": False,
            "provider_execution_started": False,
            "execution_started": False,
            "permission_granting": False,
            "authority_granting": False,
        }
    session = store.get_session(session_id)
    approval_card = None
    if result.permission_id:
        try:
            approval_card = build_session_approval_card(
                store,
                session_id,
                result.permission_id,
                fallback_arguments=arguments,
            )
        except Exception:
            approval_card = None
    try:
        cwd = session_cwd_payload(project_root, session.metadata, load_config(project_root).context_excludes)
    except Exception:
        cwd = {"cwd": session.metadata.get("cwd", "."), "resolved_abs_path": None}
    return {
        "schema_version": "harness.session_shell_action/v1" if tool_id == "shell" else "harness.session_tool_execution_response/v1",
        "ok": result.ok,
        "session_id": session_id,
        "tool_id": result.tool_id,
        "lifecycle": "direct_tool_execution",
        "save_point_emitted": False,
        "result": result.model_dump(mode="json"),
        "cwd": cwd,
        "permission_required": result.error_type == "permission_required",
        "permission_id": result.permission_id,
        "approval_card": approval_card,
        "process_started": result.tool_id == "shell" and result.ok,
        "command_executed": result.tool_id == "shell" and result.ok,
        "tool_execution_started": result.ok,
        "provider_execution_started": False,
        "execution_started": result.ok,
        "permission_granting": False,
        "authority_granting": False,
    }


def _session_unshare_unsupported(session_id: str) -> dict[str, Any]:
    return {
        "schema_version": "harness.session_unshare_action/v1",
        "ok": False,
        "session_id": session_id,
        "error": "Hosted session sharing is not implemented; there is no remote share to remove.",
        "hosted_share_supported": False,
        "network_called": False,
        "share_removed": False,
        "permission_granting": False,
    }


def _query_flag(query: dict[str, list[str]], name: str) -> bool:
    values = query.get(name)
    if not values:
        return False
    return any(value.lower() not in {"0", "false", "no", "off"} for value in values)


def _sse_after_seq(query: dict[str, list[str]], last_event_id: str | None) -> int | None:
    for key in ("since_seq", "after_seq"):
        for value in query.get(key, []):
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
    if last_event_id:
        try:
            return int(last_event_id)
        except ValueError:
            return None
    return None


def _session_id_from_sse_path(path: str) -> str:
    path = _normalize_opencode_session_path(path)
    parts = [part for part in path.split("/") if part]
    if len(parts) != 4 or parts[0] != "sessions" or parts[2] != "events" or parts[3] != "stream":
        raise ValueError("Unsupported SSE path.")
    return parts[1]


def _session_sse_ready_event(session_id: str) -> str:
    lines = [
        "event: harness.ready",
        "data: "
        + json.dumps(
            {
                "schema_version": "harness.session_event_stream/v1",
                "ok": True,
                "session_id": session_id,
                "transport": "sse",
                "permission_granting": False,
            },
            sort_keys=True,
        ),
        "",
    ]
    return "\n".join(lines) + "\n"


def _session_sse_event(event: Any) -> str:
    lines = [
        f"id: {event.seq}",
        f"event: {event.kind}",
        "data: " + json.dumps(event.model_dump(mode="json"), sort_keys=True, default=str),
        "",
    ]
    return "\n".join(lines) + "\n"


def _global_sse_ready_event(project_root: Path) -> str:
    lines = [
        "event: server.connected",
        "data: "
        + json.dumps(
            {
                "schema_version": "harness.global_event_stream/v1",
                "ok": True,
                "project_root": str(project_root),
                "transport": "sse",
                "source": "append_only_event_store",
                "permission_granting": False,
            },
            sort_keys=True,
        ),
        "",
    ]
    return "\n".join(lines) + "\n"


def _global_sse_event(project_root: Path, event_id: int | str, event: Any) -> str:
    lines = [
        f"id: {event_id}",
        f"event: {event.kind}",
        "data: "
        + json.dumps(
            {
                "directory": str(project_root),
                "project": project_root.name,
                "workspace": str(project_root),
                "payload": event.model_dump(mode="json"),
            },
            sort_keys=True,
            default=str,
        ),
        "",
    ]
    return "\n".join(lines) + "\n"


def _sse_heartbeat_event() -> str:
    return "event: harness.heartbeat\ndata: {}\n\n"


def build_session_sse_stream(store: SQLiteStore, path: str) -> str:
    session_id = _session_id_from_sse_path(path)
    store.get_session(session_id)
    events = store.list_store_events(EventStreamType.SESSION, session_id)
    return _session_sse_ready_event(session_id) + "".join(_session_sse_event(event) for event in events)


def build_global_event_sse_stream(store: SQLiteStore, project_root: Path) -> str:
    events = _global_session_events(store)
    return _global_sse_ready_event(project_root) + "".join(
        _global_sse_event(project_root, index, event) for index, event in enumerate(events, start=1)
    )


def _authorized(header: str | None, token: str) -> bool:
    if not token or not header:
        return False
    expected = f"Bearer {token}"
    return hmac.compare_digest(header.encode("utf-8"), expected.encode("utf-8"))


def _local_server_error(status: HTTPStatus, error_code: str, message: str) -> dict[str, Any]:
    return {
        "schema_version": LOCAL_SERVER_ERROR_SCHEMA_VERSION,
        "ok": False,
        "status": int(status.value),
        "error_code": error_code,
        "error": sanitize_for_logging(message),
        "permission_granting": False,
    }


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _json_response(*, status: str = "200") -> dict[str, Any]:
    return {status: {"description": "JSON response", "content": {"application/json": {"schema": {"type": "object"}}}}}


def _sse_response() -> dict[str, Any]:
    return {
        "200": {
            "description": "SSE event stream",
            "content": {"text/event-stream": {"schema": {"type": "string"}}},
        }
    }


def _path_param(name: str) -> dict[str, Any]:
    return {"name": name, "in": "path", "required": True, "schema": {"type": "string"}}
