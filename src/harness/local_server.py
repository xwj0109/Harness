from __future__ import annotations

import json
import mimetypes
import re
import secrets
import subprocess
import struct
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from harness import __version__
from harness.config import load_config
from harness.memory.sqlite_store import SQLiteStore
from harness.model_catalog import list_model_catalog, list_provider_catalog
from harness.models import EventStreamType, RedactionState, SessionMessageRole, SessionPartKind
from harness.paths import is_excluded_relative, relative_to_project, resolve_project_root, resolve_under_project
from harness.security import assert_not_secret_path, is_secret_path, redact_secret_text, sanitize_for_logging


LOCAL_SERVER_SCHEMA_VERSION = "harness.local_server/v1"
OPENAPI_SCHEMA_VERSION = "harness.local_server.openapi/v1"


def generate_server_token() -> str:
    return "harness_" + secrets.token_urlsafe(24)


def build_openapi_spec(*, server_url: str = "http://127.0.0.1:8765") -> dict[str, Any]:
    bearer = [{"bearerAuth": []}]
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "Harness Local Server",
            "version": "0.1.0",
            "x-harness-schema-version": OPENAPI_SCHEMA_VERSION,
            "description": (
                "Local Harness API backed by the same session/catalog store used by CLI and TUI. "
                "Write routes persist session records only; they do not start execution or grant permissions."
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
            }
        },
        "paths": {
            "/health": {"get": {"summary": "Health check", "security": bearer, "responses": _json_response()}},
            "/providers": {"get": {"summary": "List provider metadata", "security": bearer, "responses": _json_response()}},
            "/models": {"get": {"summary": "List model metadata", "security": bearer, "responses": _json_response()}},
            "/config": {"get": {"summary": "Read sanitized project config metadata", "security": bearer, "responses": _json_response()}},
            "/agents": {"get": {"summary": "List imported project agents", "security": bearer, "responses": _json_response()}},
            "/artifacts": {"get": {"summary": "List artifact metadata", "security": bearer, "responses": _json_response()}},
            "/files": {"get": {"summary": "List project file metadata", "security": bearer, "responses": _json_response()}},
            "/files/content": {"get": {"summary": "Read a redacted project file preview", "security": bearer, "responses": _json_response()}},
            "/files/status": {"get": {"summary": "List changed file status without file contents", "security": bearer, "responses": _json_response()}},
            "/references": {"get": {"summary": "List configured named references without loading contents", "security": bearer, "responses": _json_response()}},
            "/instructions": {"get": {"summary": "Discover project instruction files without loading contents", "security": bearer, "responses": _json_response()}},
            "/symbols": {"get": {"summary": "List static code symbols without launching LSP servers", "security": bearer, "responses": _json_response()}},
            "/lsp/diagnostics": {"get": {"summary": "List configured LSP diagnostics projection without launching servers", "security": bearer, "responses": _json_response()}},
            "/formatters": {"get": {"summary": "List formatter configuration without running formatters", "security": bearer, "responses": _json_response()}},
            "/mcp/status": {"get": {"summary": "List MCP server configuration without connecting", "security": bearer, "responses": _json_response()}},
            "/mcp/resources": {"get": {"summary": "List cached MCP resources without connecting", "security": bearer, "responses": _json_response()}},
            "/plugins": {"get": {"summary": "List plugin metadata and origin without loading plugins", "security": bearer, "responses": _json_response()}},
            "/skills": {"get": {"summary": "List skill metadata and origin without loading skills", "security": bearer, "responses": _json_response()}},
            "/web/tools": {"get": {"summary": "List web fetch/search policy without network access", "security": bearer, "responses": _json_response()}},
            "/worktrees": {"get": {"summary": "List git worktree metadata without creating, removing, or resetting worktrees", "security": bearer, "responses": _json_response()}},
            "/pty/sessions": {"get": {"summary": "List managed PTY session metadata without starting processes", "security": bearer, "responses": _json_response()}},
            "/pty/shells": {"get": {"summary": "List shell candidate metadata without probing shells", "security": bearer, "responses": _json_response()}},
            "/distribution/status": {"get": {"summary": "Inspect distribution and packaging status without modifying the Python environment", "security": bearer, "responses": _json_response()}},
            "/version/check": {"get": {"summary": "Return offline version-check contract without calling the network", "security": bearer, "responses": _json_response()}},
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
            "/sessions/{session_id}": {
                "get": {
                    "summary": "Get one session",
                    "security": bearer,
                    "parameters": [_path_param("session_id")],
                    "responses": _json_response(),
                }
            },
            "/sessions/{session_id}/events": {
                "get": {
                    "summary": "Replay persisted session events",
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
            "/sessions/{session_id}/permissions": {
                "get": {
                    "summary": "List session permission requests",
                    "security": bearer,
                    "parameters": [_path_param("session_id")],
                    "responses": _json_response(),
                }
            },
            "/sessions/{session_id}/diffs": {
                "get": {
                    "summary": "List session-linked diff artifact metadata and bounded previews without applying changes",
                    "security": bearer,
                    "parameters": [_path_param("session_id")],
                    "responses": _json_response(),
                }
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
        },
    }


def serve_local_http(project_root: Path, *, host: str, port: int, token: str) -> None:
    create_local_http_server(project_root, host=host, port=port, token=token).serve_forever()


def create_local_http_server(project_root: Path, *, host: str, port: int, token: str) -> ThreadingHTTPServer:
    project_root = resolve_project_root(project_root)
    store = SQLiteStore(project_root)
    cfg = load_config(project_root)

    class Handler(BaseHTTPRequestHandler):
        server_version = "HarnessLocalServer/0.1"

        def do_GET(self) -> None:  # noqa: N802
            if not _authorized(self.headers.get("Authorization"), token):
                self._write_json({"ok": False, "error": "Unauthorized."}, status=HTTPStatus.UNAUTHORIZED)
                return
            parsed = urlparse(self.path)
            try:
                if parsed.path.startswith("/sessions/") and parsed.path.endswith("/events/stream"):
                    self._write_sse(build_session_sse_stream(store, parsed.path))
                    return
                payload = _route_get(
                    parsed.path,
                    query=parse_qs(parsed.query),
                    project_root=project_root,
                    store=store,
                    cfg=cfg,
                    host=host,
                    port=port,
                )
            except KeyError as exc:
                self._write_json({"ok": False, "error": str(exc).strip("'")}, status=HTTPStatus.NOT_FOUND)
                return
            except ValueError as exc:
                self._write_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            if payload is None:
                self._write_json({"ok": False, "error": "Not found."}, status=HTTPStatus.NOT_FOUND)
                return
            self._write_json(payload)

        def do_POST(self) -> None:  # noqa: N802
            if not _authorized(self.headers.get("Authorization"), token):
                self._write_json({"ok": False, "error": "Unauthorized."}, status=HTTPStatus.UNAUTHORIZED)
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
            except KeyError as exc:
                self._write_json({"ok": False, "error": str(exc).strip("'")}, status=HTTPStatus.NOT_FOUND)
                return
            except ValueError as exc:
                self._write_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            if payload is None:
                self._write_json({"ok": False, "error": "Not found."}, status=HTTPStatus.NOT_FOUND)
                return
            self._write_json(payload, status=HTTPStatus.CREATED)

        def do_OPTIONS(self) -> None:  # noqa: N802
            self.send_response(HTTPStatus.NO_CONTENT.value)
            self._write_common_headers()
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

        def _read_json_body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or "0")
            raw = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON body: {exc.msg}") from exc
            if not isinstance(payload, dict):
                raise ValueError("JSON body must be an object.")
            return payload

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

        def _write_common_headers(self) -> None:
            self.send_header("Access-Control-Allow-Origin", "http://127.0.0.1")
            self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("X-Harness-Permission-Granting", "false")

    return ThreadingHTTPServer((host, port), Handler)


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
    query = query or {}
    if path == "/health":
        return {
            "schema_version": LOCAL_SERVER_SCHEMA_VERSION,
            "ok": True,
            "project_root": str(project_root),
            "permission_granting": False,
        }
    if path == "/openapi.json":
        return build_openapi_spec(server_url=f"http://{host}:{port}")
    if path == "/providers":
        providers = list_provider_catalog(cfg)
        models = list_model_catalog(cfg)
        cache = store.replace_provider_model_catalog_cache(providers, models)
        return {
            "schema_version": "harness.providers/v1",
            "ok": True,
            "cache": cache,
            "providers": [provider.model_dump(mode="json") for provider in providers],
            "permission_granting": False,
            "no_hidden_fallback": True,
        }
    if path == "/models":
        providers = list_provider_catalog(cfg)
        models = list_model_catalog(cfg)
        cache = store.replace_provider_model_catalog_cache(providers, models)
        return {
            "schema_version": "harness.models/v1",
            "ok": True,
            "cache": cache,
            "models": [model.model_dump(mode="json") for model in models],
            "permission_granting": False,
            "no_hidden_fallback": True,
        }
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
    if path == "/agents":
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
    if path == "/files":
        return {
            "schema_version": "harness.files/v1",
            "ok": True,
            "files": _project_file_metadata(project_root, cfg.context_excludes),
            "contents_included": False,
            "permission_granting": False,
        }
    if path == "/files/content":
        requested_path = _single_query_value(query, "path")
        if not requested_path:
            raise ValueError("Missing required query parameter: path")
        return _file_content_preview(project_root, requested_path, cfg.context_excludes)
    if path == "/files/status":
        return _file_status_projection(project_root, cfg.context_excludes)
    if path == "/references":
        return _reference_catalog(project_root, cfg)
    if path == "/instructions":
        return _instruction_file_catalog(project_root, cfg.context_excludes)
    if path == "/symbols":
        return _symbol_catalog(project_root, cfg.context_excludes, query=query)
    if path == "/lsp/diagnostics":
        return _lsp_diagnostics_projection(cfg)
    if path == "/formatters":
        return _formatter_catalog(cfg)
    if path == "/mcp/status":
        return _mcp_status_projection(cfg)
    if path == "/mcp/resources":
        return _mcp_resources_projection(cfg)
    if path == "/plugins":
        return _plugin_catalog(project_root, cfg)
    if path == "/skills":
        return _skill_catalog(project_root, cfg)
    if path == "/web/tools":
        return _web_tool_policy_projection(cfg)
    if path == "/worktrees":
        return _worktree_projection(project_root)
    if path == "/pty/sessions":
        return _pty_session_projection()
    if path == "/pty/shells":
        return _pty_shell_projection()
    if path == "/distribution/status":
        return _distribution_status_projection(project_root)
    if path == "/version/check":
        return _version_check_projection()
    if path == "/sessions":
        return {
            "schema_version": "harness.sessions/v1",
            "ok": True,
            "sessions": [session.model_dump(mode="json") for session in store.list_sessions()],
        }
    if path.startswith("/sessions/"):
        parts = [part for part in path.split("/") if part]
        if len(parts) == 2:
            session = store.get_session(parts[1])
            return {"schema_version": "harness.session/v1", "ok": True, "session": session.model_dump(mode="json")}
        if len(parts) == 3 and parts[2] == "events":
            events = store.list_store_events(EventStreamType.SESSION, parts[1])
            return {
                "schema_version": "harness.session_events/v1",
                "ok": True,
                "session_id": parts[1],
                "events": [event.model_dump(mode="json") for event in events],
            }
        if len(parts) == 3 and parts[2] == "messages":
            store.get_session(parts[1])
            messages = store.list_session_messages(parts[1])
            parts_by_message = {message.id: store.list_session_parts(parts[1], message.id) for message in messages}
            return {
                "schema_version": "harness.session_messages/v1",
                "ok": True,
                "session_id": parts[1],
                "messages": [message.model_dump(mode="json") for message in messages],
                "parts": {
                    message_id: [part.model_dump(mode="json") for part in message_parts]
                    for message_id, message_parts in parts_by_message.items()
                },
                "permission_granting": False,
            }
        if len(parts) == 3 and parts[2] == "permissions":
            store.get_session(parts[1])
            return {
                "schema_version": "harness.session_permissions/v1",
                "ok": True,
                "session_id": parts[1],
                "permissions": [
                    permission.model_dump(mode="json") for permission in store.list_session_permissions(parts[1])
                ],
                "permission_granting": False,
            }
        if len(parts) == 3 and parts[2] == "diffs":
            return _session_diff_projection(store, parts[1])
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
    del host, port
    if path == "/sessions":
        prompt = _optional_body_text(body, "prompt")
        title = _optional_body_text(body, "title") or _title_from_prompt(prompt) or "Server session"
        raw_model_ref = _optional_body_text(body, "raw_model_ref") or _optional_body_text(body, "model")
        agent_id = _optional_body_text(body, "agent_id")
        session = store.create_session(
            title=title,
            raw_model_ref=raw_model_ref,
            agent_id=agent_id,
            intent="server_prompt" if prompt else "server_session",
            metadata={
                "created_by": "harness_serve",
                "execution_started": False,
                "permission_granting": False,
            },
        )
        message_payload: dict[str, Any] = {}
        if prompt:
            message_payload = _append_server_user_message(store, session.id, prompt, agent_id=agent_id)
        return {
            "schema_version": "harness.local_server_session_create/v1",
            "ok": True,
            "session": store.get_session(session.id).model_dump(mode="json"),
            **message_payload,
            "execution_started": False,
            "permission_granting": False,
            "no_hidden_fallback": True,
        }
    if path.startswith("/sessions/"):
        parts = [part for part in path.split("/") if part]
        if len(parts) == 3 and parts[2] == "messages":
            session_id = parts[1]
            store.get_session(session_id)
            role = _optional_body_text(body, "role") or SessionMessageRole.USER.value
            if role != SessionMessageRole.USER.value:
                raise ValueError("Only user messages can be appended through the local server MVP.")
            content = _optional_body_text(body, "content") or _optional_body_text(body, "prompt")
            if not content:
                raise ValueError("Missing required message content.")
            return {
                "schema_version": "harness.local_server_message_append/v1",
                "ok": True,
                "session_id": session_id,
                **_append_server_user_message(store, session_id, content),
                "execution_started": False,
                "permission_granting": False,
                "no_hidden_fallback": True,
            }
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
        if len(parts) == 3 and parts[2] == "apply-hunk":
            session_id = parts[1]
            store.get_session(session_id)
            return _session_mutation_unsupported("apply-hunk", session_id, body)
    if path.startswith("/pty/"):
        parts = [part for part in path.split("/") if part]
        if len(parts) == 2 and parts[1] == "sessions":
            return _pty_action_unsupported("create", body)
        if len(parts) == 3 and parts[1] == "sessions":
            return _pty_action_unsupported("update", body, pty_id=parts[2])
        if len(parts) == 4 and parts[1] == "sessions" and parts[3] in {"write", "resize", "close"}:
            return _pty_action_unsupported(parts[3], body, pty_id=parts[2])
    if path == "/pr/checkout":
        return _pr_action_unsupported("checkout", body)
    if path == "/pr/run":
        return _pr_action_unsupported("run", body)
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
        "process_started": False,
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
            }
            for path in candidates
        ],
        "probed": False,
        "process_started": False,
        "permission_required": True,
        "permission_granting": False,
    }


def _pty_action_unsupported(action: str, body: dict[str, Any], *, pty_id: str | None = None) -> dict[str, Any]:
    return {
        "schema_version": "harness.pty_action/v1",
        "ok": False,
        "action": action,
        "pty_id": pty_id,
        "requested": sanitize_for_logging(body),
        "error": f"PTY {action} is not implemented yet; refusing to start or control terminal processes.",
        "process_started": False,
        "input_written": False,
        "terminal_resized": False,
        "terminal_closed": False,
        "websocket_token_issued": False,
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


def _pr_action_unsupported(action: str, body: dict[str, Any]) -> dict[str, Any]:
    pr_ref = _normalize_pr_ref(body.get("pr") or body.get("ref") or body.get("url") or body.get("number"))
    return {
        "schema_version": "harness.pr_action/v1",
        "ok": False,
        "action": action,
        "pr": pr_ref,
        "adapter": body.get("adapter"),
        "error": (
            f"PR {action} is not implemented yet; refusing to call network, fetch refs, checkout, create worktrees, or run adapters."
        ),
        "network_called": False,
        "git_mutation_started": False,
        "worktree_created": False,
        "checkout_started": False,
        "adapter_started": False,
        "permission_granting": False,
    }


def _normalize_pr_ref(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _single_query_value(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key) or []
    return values[0] if values else None


def _optional_body_text(body: dict[str, Any], key: str) -> str | None:
    value = body.get(key)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


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
                    "kind": kind,
                    "target": target,
                    "resolved": True,
                    **reference,
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
    search = (_single_query_value(query, "q") or "").strip().lower()
    requested_path = _single_query_value(query, "path")
    roots = [resolve_under_project(project_root, requested_path)] if requested_path else [project_root]
    symbols: list[dict[str, Any]] = []
    for root in roots:
        paths = [root] if root.is_file() else sorted(root.rglob("*"))
        for path in paths:
            if not path.is_file() or path.suffix not in {".py", ".js", ".jsx", ".ts", ".tsx"}:
                continue
            rel = relative_to_project(project_root, path)
            if is_excluded_relative(rel, excludes) or is_secret_path(path):
                continue
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
        "source": "static_scan",
        "lsp_backed": False,
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
        servers.append(
            {
                "name": name,
                "enabled": bool(cfg.lsp.enabled and server.enabled),
                "configured": bool(server.command),
                "file_extensions": list(server.file_extensions),
                "command_configured": bool(server.command),
                "process_started": False,
                "diagnostics": [],
            }
        )
    return {
        "schema_version": "harness.lsp_diagnostics/v1",
        "ok": True,
        "enabled": bool(cfg.lsp.enabled),
        "servers": servers,
        "diagnostics": [],
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
        servers.append(
            {
                "name": name,
                "kind": server.kind,
                "enabled": bool(cfg.mcp.enabled and server.enabled),
                "description": server.description,
                "command_configured": bool(server.command),
                "url_configured": bool(server.url),
                "requires_network": bool(server.url),
                "connected": False,
                "oauth_authenticated": False,
                "tool_registration_enabled": False,
                "process_started": False,
                "permission_granting": False,
            }
        )
    return {
        "schema_version": "harness.mcp_status/v1",
        "ok": True,
        "enabled": bool(cfg.mcp.enabled),
        "servers": servers,
        "connected": False,
        "tool_registration_enabled": False,
        "process_started": False,
        "network_called": False,
        "permission_granting": False,
    }


def _mcp_resources_projection(cfg) -> dict[str, Any]:
    return {
        "schema_version": "harness.mcp_resources/v1",
        "ok": True,
        "enabled": bool(cfg.mcp.enabled),
        "resources": [],
        "cached_only": True,
        "connected": False,
        "process_started": False,
        "network_called": False,
        "permission_granting": False,
    }


def _plugin_catalog(project_root: Path, cfg) -> dict[str, Any]:
    plugins: list[dict[str, Any]] = []
    for name, plugin in sorted(cfg.plugins.project.items()):
        payload: dict[str, Any] = {
            "name": name,
            "scope": "project",
            "origin": "config",
            "enabled": bool(cfg.plugins.enabled and plugin.enabled),
            "description": plugin.description,
            "version": plugin.version,
            "url": plugin.url,
            "runtime_loaded": False,
            "tools_registered": False,
            "permission_granting": False,
        }
        if plugin.path:
            path = resolve_under_project(project_root, plugin.path)
            payload.update(
                {
                    "path": relative_to_project(project_root, path),
                    "exists": path.exists(),
                    "directory": path.is_dir(),
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
                    "path": str(child),
                    "exists": True,
                    "directory": True,
                    "runtime_loaded": False,
                    "tools_registered": False,
                    "permission_granting": False,
                }
            )
    return {
        "schema_version": "harness.plugins/v1",
        "ok": True,
        "enabled": bool(cfg.plugins.enabled),
        "plugins": plugins,
        "runtime_loaded": False,
        "tools_registered": False,
        "install_supported": False,
        "update_supported": False,
        "remove_supported": False,
        "permission_granting": False,
    }


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
        payload: dict[str, Any] = {
            "name": name,
            "scope": "project",
            "origin": "config",
            "enabled": bool(cfg.skills.enabled and skill.enabled),
            "description": skill.description,
            "runtime_loaded": False,
            "tool_registered": False,
            "permission_granting": False,
        }
        if skill.path:
            path = resolve_under_project(project_root, skill.path)
            payload.update(
                {
                    "path": relative_to_project(project_root, path),
                    "exists": path.exists(),
                    "directory": path.is_dir(),
                    "skill_file": str(path / "SKILL.md") if path.is_dir() else str(path),
                    "skill_file_exists": (path / "SKILL.md").exists() if path.is_dir() else path.exists(),
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
                    "path": str(child),
                    "exists": True,
                    "directory": child.is_dir(),
                    "skill_file": str(skill_file),
                    "skill_file_exists": True,
                    "runtime_loaded": False,
                    "tool_registered": False,
                    "permission_granting": False,
                }
            )
    return {
        "schema_version": "harness.skills/v1",
        "ok": True,
        "enabled": bool(cfg.skills.enabled),
        "skills": skills,
        "runtime_loaded": False,
        "tool_registered": False,
        "load_supported": False,
        "permission_granting": False,
    }


def _global_skill_paths() -> list[Path]:
    home = Path.home()
    return [
        home / ".harness" / "skills",
        home / ".codex" / "skills",
        home / ".agents" / "skills",
    ]


def _web_tool_policy_projection(cfg) -> dict[str, Any]:
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
        "network_called": False,
        "execution_supported": False,
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
        "network_called": False,
        "execution_supported": False,
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


def build_session_sse_stream(store: SQLiteStore, path: str) -> str:
    parts = [part for part in path.split("/") if part]
    if len(parts) != 4 or parts[0] != "sessions" or parts[2] != "events" or parts[3] != "stream":
        raise ValueError("Unsupported SSE path.")
    session_id = parts[1]
    store.get_session(session_id)
    events = store.list_store_events(EventStreamType.SESSION, session_id)
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
    for event in events:
        lines.extend(
            [
                f"id: {event.seq}",
                f"event: {event.kind}",
                "data: " + json.dumps(event.model_dump(mode="json"), sort_keys=True, default=str),
                "",
            ]
        )
    return "\n".join(lines) + "\n"


def _authorized(header: str | None, token: str) -> bool:
    return bool(token) and header == f"Bearer {token}"


def _json_response(*, status: str = "200") -> dict[str, Any]:
    return {status: {"description": "JSON response", "content": {"application/json": {"schema": {"type": "object"}}}}}


def _path_param(name: str) -> dict[str, Any]:
    return {"name": name, "in": "path", "required": True, "schema": {"type": "string"}}
