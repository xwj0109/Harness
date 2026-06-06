from __future__ import annotations

import json
import sqlite3
import subprocess
import struct
import sys
import threading
import zlib
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
import yaml
from typer.testing import CliRunner

from harness.chat import ChatSessionState, handle_chat_input
from harness.cli.main import app
from harness.config import load_config
from harness.local_server import (
    _authorized,
    _route_delete,
    _route_get,
    _route_patch,
    _route_post,
    build_global_event_sse_stream,
    build_openapi_spec,
    build_session_sse_stream,
    create_local_http_server,
)
from harness.memory.sqlite_store import SQLiteStore
from harness.models import EventStreamType, SessionPermissionBoundaryKind, SessionPermissionScope, SessionPermissionStatus
from harness.objective_runner import run_objective_autonomously
from harness.operator_loop import create_turn_state_from_session, persist_turn_finished, persist_turn_started
from harness.operator_models import HarnessAgentPhase
from harness.process_supervisor import get_process_supervisor
from harness.provider_auth import read_provider_account_secret, read_provider_oauth_tokens
from harness.session_events import SessionEventKind, append_session_event


runner = CliRunner()


def _operator_prompt_event_kinds(store: SQLiteStore, session_id: str) -> list[str]:
    relevant = {
        "operator.turn.started",
        "tool_call.started",
        "harness.tool_call.before",
        "permission.checked",
        "tool_call.output",
        "tool_call.finished",
        "harness.tool_call.after",
    }
    return [event.kind for event in store.list_session_store_events(session_id) if event.kind in relevant]


def test_openapi_spec_exposes_phase_6_read_only_endpoints() -> None:
    spec = build_openapi_spec(server_url="http://127.0.0.1:9999")

    assert spec["openapi"] == "3.1.0"
    assert spec["info"]["x-harness-schema-version"] == "harness.local_server.openapi/v1"
    assert spec["x-harness"]["permission_granting"] is False
    assert spec["x-harness"]["no_hidden_fallback"] is True
    assert spec["x-harness"]["authority"] == "local_persistence_no_execution"
    assert spec["components"]["securitySchemes"]["bearerAuth"]["scheme"] == "bearer"
    assert {
        "/health",
        "/event",
        "/global/health",
        "/global/event",
        "/global/config",
        "/global/dispose",
        "/global/upgrade",
        "/server/lifecycle",
        "/server/mdns",
        "/server/dispose",
        "/providers",
        "/providers/{provider_id}",
        "/models",
        "/models/{provider_id}/{model_id}",
        "/models/validate",
        "/models/preferences",
        "/models/preferences/favorite",
        "/models/preferences/default",
        "/provider",
        "/provider/auth",
        "/provider/{provider_id}/oauth/authorize",
        "/provider/{provider_id}/oauth/callback",
        "/auth/{provider_id}",
        "/log",
        "/api/provider",
        "/api/provider/{provider_id}",
        "/api/catalog/model",
        "/api/catalog/model/{provider_id}/{model_id}",
        "/api/model",
        "/config/providers",
        "/config",
        "/path",
        "/project",
        "/project/current",
        "/project/git/init",
        "/project/{project_id}",
        "/vcs",
        "/vcs/status",
        "/vcs/diff",
        "/vcs/diff/raw",
        "/vcs/apply",
        "/agent",
        "/skill",
        "/lsp",
        "/formatter",
        "/agents",
        "/agents/discovery",
        "/agents/allocation",
        "/artifacts",
        "/find",
        "/find/file",
        "/find/symbol",
        "/file",
        "/file/content",
        "/file/status",
        "/files",
        "/files/content",
        "/files/status",
        "/references",
        "/instructions",
        "/symbols",
        "/lsp/diagnostics",
        "/formatters",
        "/mcp/status",
        "/mcp",
        "/mcp/{name}/auth",
        "/mcp/{name}/auth/callback",
        "/mcp/{name}/auth/authenticate",
        "/mcp/{name}/connect",
        "/mcp/{name}/disconnect",
        "/mcp/resources",
        "/plugins",
        "/skills",
        "/web/tools",
        "/extensions/status",
        "/web/client",
        "/web/open",
        "/worktrees",
        "/dev-loop/status",
        "/workspaces",
        "/workspaces/clients",
        "/workspaces/attach",
        "/workspaces/sync",
        "/workspaces/steal",
        "/workspaces/dispose",
        "/sync/start",
        "/sync/replay",
        "/sync/steal",
        "/sync/history",
        "/experimental/workspace/adapter",
        "/experimental/workspace",
        "/experimental/workspace/sync-list",
        "/experimental/workspace/status",
        "/experimental/workspace/warp",
        "/pty",
        "/pty/{pty_id}",
        "/pty/{pty_id}/restoration",
        "/pty/{pty_id}/tab",
        "/pty/{pty_id}/connect-token",
        "/pty/{pty_id}/connect",
        "/pty/sessions",
        "/pty/shells",
        "/pty/restoration",
        "/pty/tabs",
        "/distribution/status",
        "/distribution/packaging-smoke",
        "/distribution/packaging-smoke/run",
        "/desktop/status",
        "/desktop/launch",
        "/version/check",
        "/settings/tui",
        "/tui/append-prompt",
        "/tui/open-help",
        "/tui/open-sessions",
        "/tui/open-themes",
        "/tui/open-models",
        "/tui/submit-prompt",
        "/tui/clear-prompt",
        "/tui/execute-command",
        "/tui/show-toast",
        "/tui/publish",
        "/tui/select-session",
        "/tui/control/next",
        "/tui/control/response",
        "/command",
        "/permission",
        "/permission/{permission_id}/reply",
        "/question",
        "/question/{question_id}/reply",
        "/question/{question_id}/reject",
        "/commands",
        "/orchestration/readiness",
        "/orchestration/workflows",
        "/orchestration/scenarios",
        "/orchestration/efficiency",
        "/orchestration/microbenchmarks",
        "/orchestration/synthesis",
        "/objectives/{objective_id}/evidence",
        "/objectives/{objective_id}/trace",
        "/runs/{run_id}/trace",
        "/commands/run",
        "/pr/checkout",
        "/pr/run",
        "/worktrees/create",
        "/worktrees/remove",
        "/worktrees/reset",
        "/sessions",
        "/api/session",
        "/api/session/{session_id}/prompt",
        "/api/session/{session_id}/compact",
        "/api/session/{session_id}/wait",
        "/api/session/{session_id}/context",
        "/api/session/{session_id}/message",
        "/sessions/status",
        "/sessions/{session_id}",
        "/sessions/{session_id}/events",
        "/sessions/{session_id}/status",
        "/sessions/{session_id}/pending-action",
        "/sessions/{session_id}/children",
        "/sessions/{session_id}/fork",
        "/sessions/{session_id}/summary",
        "/sessions/{session_id}/summarize",
        "/sessions/{session_id}/model",
        "/sessions/{session_id}/abort",
        "/sessions/{session_id}/replay",
        "/sessions/{session_id}/messages",
        "/sessions/{session_id}/message",
        "/sessions/{session_id}/messages/{message_id}",
        "/sessions/{session_id}/message/{message_id}",
        "/sessions/{session_id}/message/{message_id}/part/{part_id}",
        "/sessions/{session_id}/messages/{message_id}/retract",
        "/sessions/{session_id}/prompt_async",
        "/sessions/{session_id}/command",
        "/sessions/{session_id}/init",
        "/sessions/{session_id}/shell",
        "/sessions/{session_id}/parts/{part_id}/correct",
        "/sessions/{session_id}/permissions",
        "/sessions/{session_id}/permissions/snapshot",
        "/sessions/{session_id}/permissions/{permission_id}/reply",
        "/sessions/{session_id}/permissions/{permission_id}",
        "/sessions/{session_id}/todos",
        "/sessions/{session_id}/todo",
        "/sessions/{session_id}/questions",
        "/sessions/{session_id}/diffs",
        "/sessions/{session_id}/diff",
        "/sessions/{session_id}/changed-files",
        "/sessions/{session_id}/snapshots",
        "/sessions/{session_id}/messages/{message_id}/snapshots",
        "/sessions/{session_id}/revert-readiness",
        "/sessions/{session_id}/messages/{message_id}/revert-readiness",
        "/sessions/{session_id}/share",
        "/sessions/{session_id}/revert",
        "/sessions/{session_id}/unrevert",
        "/sessions/{session_id}/apply-hunk",
        "/sessions/{session_id}/mentions/resolve",
        "/sessions/{session_id}/attachments",
        "/sessions/{session_id}/context/estimate",
        "/sessions/{session_id}/events/stream",
        "/session",
        "/session/status",
        "/session/{session_id}",
        "/session/{session_id}/message",
        "/session/{session_id}/prompt_async",
        "/session/{session_id}/command",
        "/session/{session_id}/permissions/{permission_id}",
        "/session/{session_id}/events/stream",
        "/openapi.json",
    } <= set(spec["paths"])
    assert "post" in spec["paths"]["/sessions"]
    assert "patch" in spec["paths"]["/config"]
    assert "put" in spec["paths"]["/auth/{provider_id}"]
    assert "delete" in spec["paths"]["/auth/{provider_id}"]
    assert "post" in spec["paths"]["/log"]
    assert "patch" in spec["paths"]["/project/{project_id}"]
    assert "post" in spec["paths"]["/pty"]
    assert "put" in spec["paths"]["/pty/{pty_id}"]
    assert "delete" in spec["paths"]["/pty/{pty_id}"]
    assert "get" in spec["paths"]["/api/session/{session_id}/message"]
    assert "post" in spec["paths"]["/api/session/{session_id}/prompt"]
    assert "append-only prompt persistence" in spec["paths"]["/api/session/{session_id}/prompt"]["post"]["summary"]
    assert spec["paths"]["/session"]["x-harness-alias-for"] == "/sessions"
    assert "patch" in spec["paths"]["/sessions/{session_id}"]
    assert "delete" in spec["paths"]["/sessions/{session_id}"]
    assert "get" in spec["paths"]["/sessions/{session_id}/pending-action"]
    assert "delete" in spec["paths"]["/sessions/{session_id}/pending-action"]
    assert "post" in spec["paths"]["/sessions/{session_id}/messages"]
    assert spec["paths"]["/session/{session_id}/message"]["x-harness-alias-for"] == "/sessions/{session_id}/message"
    assert spec["paths"]["/api/provider"]["x-harness-alias-for"] == "/providers"
    assert spec["paths"]["/api/provider/{provider_id}"]["x-harness-alias-for"] == "/providers/{provider_id}"
    assert spec["paths"]["/api/catalog/model"]["x-harness-alias-for"] == "/models"
    assert spec["paths"]["/api/catalog/model/{provider_id}/{model_id}"]["x-harness-alias-for"] == "/models/{provider_id}/{model_id}"
    schemas = spec["components"]["schemas"]
    for schema_name in (
        "ProviderCatalogResponse",
        "ModelCatalogResponse",
        "ModelDetailResponse",
        "ModelValidationResponse",
        "ProviderAuthMethodsResponse",
        "ModelPreferencesResponse",
        "ModelPreferenceUpdateResponse",
        "SessionModelSelectionResponse",
        "AgentDiscoveryCatalogResponse",
        "DelegateAllocationResponse",
        "OrchestrationEfficiencyAuditResponse",
        "ModelSuggestion",
        "ProviderSuggestion",
    ):
        assert schema_name in schemas
    assert schemas["ProviderCatalogResponse"]["properties"]["all"]
    assert schemas["ProviderCatalogResponse"]["properties"]["connected"]
    assert schemas["ProviderCatalogResponse"]["properties"]["default"]
    assert schemas["ProviderCatalogResponse"]["properties"]["blocked"]
    assert "oauth_support" in schemas["ProviderCatalogResponse"]["properties"]
    assert "provider_suggestions" in schemas["ProviderCatalogResponse"]["properties"]
    assert schemas["ModelCatalogResponse"]["properties"]["all"]
    assert schemas["ModelCatalogResponse"]["properties"]["connected"]
    assert schemas["ModelCatalogResponse"]["properties"]["default"]
    assert schemas["ModelCatalogResponse"]["properties"]["blocked"]
    assert "oauth_support" in schemas["ModelCatalogResponse"]["properties"]
    assert "model_suggestions" in schemas["ModelDetailResponse"]["properties"]
    assert "provider_suggestions" in schemas["ModelValidationResponse"]["properties"]
    assert "oauth_supported_providers" in schemas["ProviderAuthMethodsResponse"]["properties"]
    assert (
        spec["paths"]["/providers"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
        == "#/components/schemas/ProviderCatalogResponse"
    )
    assert (
        spec["paths"]["/providers/{provider_id}"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
        == "#/components/schemas/ProviderCatalogResponse"
    )
    assert (
        spec["paths"]["/models"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
        == "#/components/schemas/ModelCatalogResponse"
    )
    assert (
        spec["paths"]["/models/{provider_id}/{model_id}"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
        == "#/components/schemas/ModelDetailResponse"
    )
    assert (
        spec["paths"]["/models/validate"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
        == "#/components/schemas/ModelValidationResponse"
    )
    assert (
        spec["paths"]["/provider/auth"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
        == "#/components/schemas/ProviderAuthMethodsResponse"
    )
    assert (
        spec["paths"]["/sessions/{session_id}/model"]["post"]["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
        == "#/components/schemas/SessionModelSelectionResponse"
    )
    assert (
        spec["paths"]["/agents/discovery"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
        == "#/components/schemas/AgentDiscoveryCatalogResponse"
    )
    assert (
        spec["paths"]["/agents/allocation"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
        == "#/components/schemas/DelegateAllocationResponse"
    )
    assert spec["paths"]["/agents/discovery"]["get"]["x-harness-safety"]["agent_execution_started"] is False
    assert spec["paths"]["/agents/allocation"]["get"]["x-harness-safety"]["permission_granting"] is False
    assert spec["paths"]["/provider/{provider_id}/oauth/authorize"]["post"]["responses"].keys() == {"200"}
    assert spec["paths"]["/provider/{provider_id}/oauth/callback"]["post"]["responses"].keys() == {"200"}
    assert spec["paths"]["/provider/{provider_id}/oauth/callback"]["post"]["x-harness-safety"]["credential_written"] is True
    assert spec["paths"]["/provider/{provider_id}/oauth/callback"]["post"]["x-harness-safety"]["credentials_included"] is False
    assert spec["paths"]["/models"]["get"]["x-harness-safety"]["provider_execution_started"] is False
    assert spec["paths"]["/models/{provider_id}/{model_id}"]["get"]["x-harness-safety"]["hidden_model_fallback"] is False
    assert "/api/auth" not in spec["paths"]
    assert spec["paths"]["/sessions/{session_id}/events"]["get"]["security"] == [{"bearerAuth": []}]
    assert spec["paths"]["/event"]["get"]["responses"]["200"]["content"] == {"text/event-stream": {"schema": {"type": "string"}}}
    assert spec["paths"]["/global/event"]["get"]["responses"]["200"]["content"] == {"text/event-stream": {"schema": {"type": "string"}}}
    assert (
        spec["paths"]["/sessions/{session_id}/events/stream"]["get"]["responses"]["200"]["content"]
        == {"text/event-stream": {"schema": {"type": "string"}}}
    )


def test_local_server_model_catalog_reads_hot_reload_custom_models_config(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    cfg = load_config(tmp_path)
    custom_path = tmp_path / ".harness" / "models.yaml"

    custom_path.write_text(
        yaml.safe_dump(
            {
                "providers": {
                    "hot_router": {
                        "display_name": "Hot Router",
                        "enabled": True,
                        "data_boundary": "local_only",
                        "base_url": "http://localhost:11434/v1",
                        "protocol": "openai_chat",
                        "credential": {"kind": "static_local"},
                        "models": {"alpha": {"context_window": 4096}},
                    }
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    providers = _route_get("/providers", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    models = _route_get("/models", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    validation = _route_get(
        "/models/validate",
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
        query={"model": ["hot_router/alpha"]},
    )

    assert any(provider["provider_id"] == "hot_router" for provider in providers["providers"])
    assert "hot_router/alpha" in {model["raw_model_ref"] for model in models["models"]}
    assert validation["validation"]["known_catalog_entry"] is True
    assert validation["validation"]["executable"] is True

    custom_path.write_text(
        yaml.safe_dump(
            {
                "providers": {
                    "hot_router": {
                        "display_name": "Hot Router",
                        "enabled": True,
                        "data_boundary": "local_only",
                        "base_url": "http://localhost:11434/v1",
                        "protocol": "openai_chat",
                        "credential": {"kind": "static_local"},
                        "models": {"beta": {"context_window": 8192}},
                    }
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    reloaded = _route_get("/models", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    reloaded_refs = {model["raw_model_ref"] for model in reloaded["models"]}
    reloaded_validation = _route_get(
        "/models/validate",
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
        query={"model": ["hot_router/beta"]},
    )

    assert "hot_router/beta" in reloaded_refs
    assert "hot_router/alpha" not in reloaded_refs
    assert reloaded_validation["validation"]["known_catalog_entry"] is True
    assert reloaded_validation["validation"]["executable"] is True
    assert reloaded["metadata_only"] is True
    assert reloaded["network_accessed"] is False
    assert reloaded["credentials_included"] is False


def test_model_provider_stable_api_routes_expose_settings_surface_without_execution(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    cfg = load_config(tmp_path)
    session = store.create_session(title="Stable model API")

    providers = _route_get("/providers", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    provider_id = "codex_cli"
    provider = _route_get(f"/providers/{provider_id}", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    auth = _route_get("/provider/auth", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    models = _route_get("/models", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    opencode_models = _route_get("/api/catalog/model", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    model = _route_get(
        "/models/codex_cli/gpt-5.5",
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    opencode_model = _route_get(
        "/api/catalog/model/codex_cli/gpt-5.5",
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    validation = _route_get(
        "/models/validate",
        query={"model": ["codex_cli/gpt-5.5"]},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    selected = _route_post(
        f"/sessions/{session.id}/model",
        body={"raw_model_ref": "codex_cli/gpt-5.5"},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    favorite = _route_post(
        "/models/preferences/favorite",
        body={"raw_model_ref": "codex_cli/gpt-5.5", "favorite": True},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    default = _route_post(
        "/models/preferences/default",
        body={"raw_model_ref": "codex_cli/gpt-5.5"},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    preferences = _route_get(
        "/models/preferences",
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    providers_after_default = _route_get(
        "/providers",
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    models_after_default = _route_get(
        "/models",
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )

    assert any(item["provider_id"] == provider_id for item in providers["providers"])
    assert provider["provider"]["provider_id"] == provider_id
    assert any(item["provider_id"] == provider_id for item in auth["providers"])
    assert "codex_cli/gpt-5.5" in {item["raw_model_ref"] for item in models["models"]}
    assert opencode_models["models"] == models["models"]
    assert model["raw_model_ref"] == "codex_cli/gpt-5.5"
    assert opencode_model["model"] == model["model"]
    assert opencode_model["validation"] == model["validation"]
    assert model["provider_execution_started"] is False
    assert opencode_model["provider_execution_started"] is False
    assert model["model_execution_started"] is False
    assert opencode_model["model_execution_started"] is False
    assert validation["validation"]["executable"] is True
    assert selected["session_model_selected"] is True
    assert selected["provider_execution_started"] is False
    assert selected["model_execution_started"] is False
    assert favorite["preference"]["favorite"] is True
    assert default["preference"]["is_default"] is True
    assert preferences["default_preference"]["raw_model_ref"] == "codex_cli/gpt-5.5"
    assert preferences["favorite_preferences"][0]["raw_model_ref"] == "codex_cli/gpt-5.5"
    assert preferences["provider_execution_started"] is False
    assert preferences["model_execution_started"] is False
    assert preferences["credential_written"] is False
    assert providers_after_default["all"] == providers_after_default["all_providers"]
    assert providers_after_default["connected"] == providers_after_default["connected_providers"]
    assert providers_after_default["default"]["provider_id"] == "codex_cli"
    assert providers_after_default["default_provider"]["provider_id"] == "codex_cli"
    assert providers_after_default["distinctions"]["default"] == "codex_cli"
    assert "codex_cli" in providers_after_default["distinctions"]["connected"]
    assert all(item["is_connected"] for item in providers_after_default["connected"])
    assert all(item["is_blocked"] for item in providers_after_default["blocked"])
    provider_by_id = {item["provider_id"]: item for item in providers_after_default["all"]}
    assert provider_by_id["paid_openai_compatible"]["oauth_supported"] is True
    assert "oauth" in provider_by_id["paid_openai_compatible"]["auth_methods"]
    assert provider_by_id["codex_cli"]["oauth_supported"] is False
    assert "codex_login" in provider_by_id["codex_cli"]["auth_methods"]
    assert "paid_openai_compatible" in providers_after_default["oauth_supported_providers"]
    assert providers_after_default["oauth_support"]["paid_openai_compatible"] is True
    assert models_after_default["all"] == models_after_default["all_models"]
    assert models_after_default["connected"] == models_after_default["connected_models"]
    assert models_after_default["default"]["raw_model_ref"] == "codex_cli/gpt-5.5"
    assert models_after_default["default_model"]["raw_model_ref"] == "codex_cli/gpt-5.5"
    assert models_after_default["distinctions"]["default"] == "codex_cli/gpt-5.5"
    assert "codex_cli/gpt-5.5" in models_after_default["distinctions"]["connected"]
    assert all(item["is_connected"] for item in models_after_default["connected"])
    assert all(item["is_blocked"] for item in models_after_default["blocked"])
    assert {item["raw_model_ref"] for item in models_after_default["models"]} == set(models_after_default["distinctions"]["all"])
    assert models_after_default["default"]["favorite"] is True
    assert models_after_default["default"]["is_default"] is True
    default_model = models_after_default["default"]
    assert default_model["provider_oauth_supported"] is False
    assert "codex_login" in default_model["provider_auth_methods"]
    assert "paid_openai_compatible" in models_after_default["oauth_supported_providers"]
    assert models_after_default["oauth_support"]["paid_openai_compatible"] is True
    assert store.get_session(session.id).raw_model_ref == "codex_cli/gpt-5.5"


def test_models_get_returns_one_model_or_suggestions(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    cfg = load_config(tmp_path)
    session = store.create_session(title="Suggestion API")

    known = _route_get(
        "/models/codex_cli/gpt-5.5",
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    unknown_model = _route_get(
        "/models/codex_cli/not-a-real-model",
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    unknown_provider_model = _route_get(
        "/models/missing/gpt-5.5",
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    unknown_provider = _route_get(
        "/providers/missing",
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    validation = _route_get(
        "/models/validate",
        query={"model": ["codex_cli/not-a-real-model"]},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    selected = _route_post(
        f"/sessions/{session.id}/model",
        body={"raw_model_ref": "codex_cli/not-a-real-model"},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )

    assert known["ok"] is True
    assert known["model"]["raw_model_ref"] == "codex_cli/gpt-5.5"
    assert unknown_model["ok"] is False
    assert unknown_model["error_code"] == "model_unknown"
    assert unknown_model["model"] is None
    assert unknown_model["validation"]["blocked_reasons"] == ["model_unknown"]
    assert unknown_model["suggestion_only"] is True
    assert unknown_model["suggestions"]
    assert all(item["suggestion_only"] is True for item in unknown_model["suggestions"])
    assert all(item["selected_model"] is False for item in unknown_model["suggestions"])
    assert all(item["provider_execution_started"] is False for item in unknown_model["suggestions"])
    assert "codex_cli/gpt-5.5" in {item["raw_model_ref"] for item in unknown_model["suggestions"]}
    assert unknown_provider_model["ok"] is False
    assert unknown_provider_model["validation"]["blocked_reasons"] == ["provider_unknown", "model_unknown"]
    assert unknown_provider_model["provider_suggestions"]
    assert all(item["suggestion_only"] is True for item in unknown_provider_model["provider_suggestions"])
    assert unknown_provider["ok"] is False
    assert unknown_provider["error_code"] == "provider_unknown"
    assert unknown_provider["provider"] is None
    assert unknown_provider["provider_suggestions"]
    assert validation["ok"] is False
    assert validation["suggestions"]
    assert validation["validation"]["suggestions"] == validation["suggestions"]
    assert selected["ok"] is False
    assert selected["session_model_selected"] is False
    assert selected["suggestions"]
    assert selected["model_validation"]["suggestions"] == selected["suggestions"]
    assert store.get_session(session.id).raw_model_ref is None
    assert selected["provider_execution_started"] is False
    assert selected["model_execution_started"] is False
    assert selected["hidden_model_fallback"] is False


def test_harness_serve_openapi_cli_outputs_checked_contract(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0

    result = runner.invoke(app, ["serve", "--project", str(tmp_path), "--openapi", "--output", "json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["info"]["x-harness-schema-version"] == "harness.local_server.openapi/v1"
    assert payload["servers"][0]["url"] == "http://127.0.0.1:8765"
    assert "bearerAuth" in payload["components"]["securitySchemes"]


def test_local_server_routes_require_token_and_return_store_projections(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    (tmp_path / "README.md").write_text("hello server\nsk-abcdefghijklmnopqrstuvwxyz\n", encoding="utf-8")
    (tmp_path / ".env").write_text("TOKEN=secret-token\n", encoding="utf-8")
    store = SQLiteStore(tmp_path)
    session = store.create_session(title="Server session", raw_model_ref="codex_cli/gpt-5.5")
    message = store.append_session_message(session.id, "user", "Replay this")
    store.append_session_part(session.id, message.id, "text", text="Replay this")
    permission = store.request_session_permission(
        session.id,
        tool_id="read",
        normalized_action="read",
        normalized_target_pattern="README.md",
        boundary_kind="local_only",
        risk="low",
    )
    run = store.create_run("server artifact", "phase_1a_test", status="succeeded", session_id=session.id)
    artifact_path = store.initialize_run_artifacts(run.id)["final_report"]
    artifact_path.write_text("server artifact body\n", encoding="utf-8")
    artifact = store.register_artifact(run.id, "final_report", artifact_path, session_id=session.id)
    cfg = load_config(tmp_path)

    assert _authorized("Bearer local-token", "local-token") is True
    assert _authorized("Bearer wrong", "local-token") is False
    assert _authorized(None, "local-token") is False
    health = _route_get("/health", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    global_health = _route_get("/global/health", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    global_config = _route_get("/global/config", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    global_events = _route_get("/global/event", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    lifecycle = _route_get("/server/lifecycle", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    mdns = _route_get("/server/mdns", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    config = _route_get("/config", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    path_info = _route_get("/path", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    projects = _route_get("/project", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    current_project = _route_get("/project/current", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    vcs = _route_get("/vcs", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    vcs_status = _route_get("/vcs/status", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    vcs_diff = _route_get("/vcs/diff", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    vcs_diff_raw = _route_get("/vcs/diff/raw", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    agents = _route_get("/agents", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    agent_alias = _route_get("/agent", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    artifacts = _route_get("/artifacts", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    files = _route_get("/files", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    file_content = _route_get(
        "/files/content",
        query={"path": ["README.md"]},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    file_status = _route_get("/files/status", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    references = _route_get("/references", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    instructions = _route_get("/instructions", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    symbols = _route_get("/symbols", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    diagnostics = _route_get("/lsp/diagnostics", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    lsp_alias = _route_get("/lsp", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    formatters = _route_get("/formatters", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    formatter_alias = _route_get("/formatter", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    mcp_status = _route_get("/mcp/status", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    mcp_resources = _route_get("/mcp/resources", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    plugins = _route_get("/plugins", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    skills = _route_get("/skills", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    skill_alias = _route_get("/skill", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    web_tools = _route_get("/web/tools", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    extensions = _route_get("/extensions/status", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    web_client = _route_get("/web/client", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    worktrees = _route_get("/worktrees", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    dev_loop = _route_get("/dev-loop/status", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    workspaces = _route_get("/workspaces", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    experimental_workspaces = _route_get("/experimental/workspace", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    workspace_adapters = _route_get("/experimental/workspace/adapter", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    workspace_status = _route_get("/experimental/workspace/status", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    workspace_clients = _route_get("/workspaces/clients", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    pty_sessions = _route_get("/pty/sessions", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    pty_shells = _route_get("/pty/shells", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    distribution = _route_get("/distribution/status", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    packaging_smoke = _route_get("/distribution/packaging-smoke", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    desktop = _route_get("/desktop/status", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    version_check = _route_get("/version/check", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    tui_settings = _route_get("/settings/tui", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    command_alias = _route_get("/command", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    commands = _route_get("/commands", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    providers = _route_get("/providers", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    provider_alias = _route_get("/provider", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    provider_auth = _route_get("/provider/auth", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    api_providers = _route_get("/api/provider", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    first_provider_id = providers["providers"][0]["provider_id"]
    api_provider = _route_get(f"/api/provider/{first_provider_id}", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    models = _route_get("/models", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    api_models = _route_get("/api/model", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    model_validation = _route_get(
        "/models/validate",
        query={"model": ["codex_cli/gpt-5.5"]},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    api_model_validation = _route_get(
        "/api/model/validate",
        query={"model": ["codex_cli/not-a-real-model"]},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    sessions = _route_get("/sessions", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    sessions_status = _route_get("/sessions/status", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    inspected = _route_get(f"/sessions/{session.id}", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    events = _route_get(f"/sessions/{session.id}/events", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    session_status = _route_get(f"/sessions/{session.id}/status", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    session_children = _route_get(f"/sessions/{session.id}/children", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    replay = _route_get(
        f"/sessions/{session.id}/replay",
        query={"limit": ["1"]},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    messages = _route_get(f"/sessions/{session.id}/messages", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    permissions = _route_get(
        f"/sessions/{session.id}/permissions",
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    permission_snapshot = _route_get(
        f"/sessions/{session.id}/permissions/snapshot",
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    diffs = _route_get(f"/sessions/{session.id}/diffs", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    changed = _route_get(f"/sessions/{session.id}/changed-files", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    share = _route_get(f"/sessions/{session.id}/share", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    stream_projection = _route_get(
        f"/sessions/{session.id}/events/stream",
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    sse = build_session_sse_stream(store, f"/sessions/{session.id}/events/stream")
    global_sse = build_global_event_sse_stream(store, tmp_path)

    assert health["schema_version"] == "harness.local_server/v1"
    assert global_health["schema_version"] == "harness.global_health/v1"
    assert global_health["healthy"] is True
    assert global_health["permission_granting"] is False
    assert global_config["schema_version"] == "harness.global_config/v1"
    assert global_config["secrets_included"] is False
    assert global_config["permission_granting"] is False
    assert global_events["schema_version"] == "harness.global_events/v1"
    assert global_events["source"] == "append_only_event_store"
    assert global_events["event_count"] >= 3
    assert global_events["permission_granting"] is False
    assert lifecycle["schema_version"] == "harness.local_server_lifecycle/v1"
    assert lifecycle["dispose_supported"] is False
    assert lifecycle["remote_attach_supported"] is True
    assert lifecycle["sse_supported"] is True
    assert lifecycle["websocket_supported"] is False
    assert lifecycle["process_stopped"] is False
    assert lifecycle["permission_granting"] is False
    assert mdns["schema_version"] == "harness.local_server_mdns/v1"
    assert mdns["enabled"] is False
    assert mdns["advertised"] is False
    assert mdns["network_broadcast_started"] is False
    assert mdns["permission_granting"] is False
    assert config["schema_version"] == "harness.config_projection/v1"
    assert config["permission_granting"] is False
    assert path_info["schema_version"] == "harness.path_projection/v1"
    assert path_info["directory"] == str(tmp_path)
    assert path_info["worktree"] == str(tmp_path)
    assert path_info["permission_granting"] is False
    assert projects["schema_version"] == "harness.projects/v1"
    assert projects["projects"][0]["path"] == str(tmp_path)
    assert projects["permission_granting"] is False
    assert current_project["schema_version"] == "harness.project_info/v1"
    assert current_project["current"] is True
    assert current_project["path"] == str(tmp_path)
    assert vcs["schema_version"] == "harness.vcs/v1"
    assert vcs["available"] is False
    assert vcs["permission_granting"] is False
    assert vcs_status["schema_version"] == "harness.file_status/v1"
    assert vcs_status["contents_included"] is False
    assert vcs_diff["schema_version"] == "harness.vcs_diff/v1"
    assert vcs_diff["raw"] is False
    assert vcs_diff["mutation_started"] is False
    assert vcs_diff_raw["raw"] is True
    assert vcs_diff_raw["permission_granting"] is False
    assert agents["schema_version"] == "harness.project_agents/v1"
    assert agent_alias == agents
    assert artifacts["schema_version"] == "harness.artifacts/v1"
    assert artifacts["contents_included"] is False
    assert artifacts["artifacts"][0]["id"] == artifact.id
    assert artifacts["artifacts"][0]["contents_included"] is False
    assert files["schema_version"] == "harness.files/v1"
    assert files["contents_included"] is False
    assert any(file["path"] == "README.md" for file in files["files"])
    assert not any(file["path"] == ".env" for file in files["files"])
    assert file_content["schema_version"] == "harness.file_content/v1"
    assert file_content["path"] == "README.md"
    assert "hello server" in file_content["preview"]
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in file_content["preview"]
    assert "[REDACTED_SECRET]" in file_content["preview"]
    assert file_status["schema_version"] == "harness.file_status/v1"
    assert file_status["contents_included"] is False
    assert file_status["permission_granting"] is False
    assert references["schema_version"] == "harness.references/v1"
    assert references["contents_included"] is False
    assert references["permission_granting"] is False
    assert instructions["schema_version"] == "harness.instructions/v1"
    assert instructions["contents_included"] is False
    assert instructions["permission_granting"] is False
    assert symbols["schema_version"] == "harness.symbols/v1"
    assert symbols["lsp_backed"] is False
    assert symbols["live_lsp_supported"] is False
    assert symbols["policy_boundary"]["kind"] == "static_symbol_scan"
    assert symbols["policy_boundary"]["lsp_server_launch_allowed"] is False
    assert symbols["blocked_reasons"] == ["lsp_process_launch_disabled"]
    assert symbols["process_started"] is False
    assert symbols["contents_included"] is False
    assert symbols["permission_granting"] is False
    assert diagnostics["schema_version"] == "harness.lsp_diagnostics/v1"
    assert lsp_alias == diagnostics
    assert diagnostics["enabled"] is False
    assert diagnostics["live_lsp_supported"] is False
    assert diagnostics["policy_boundary"]["kind"] == "lsp_diagnostics_projection"
    assert diagnostics["policy_boundary"]["server_launch_allowed"] is False
    assert diagnostics["blocked_reasons"] == ["lsp_disabled", "lsp_process_launch_disabled"]
    assert diagnostics["process_started"] is False
    assert diagnostics["permission_granting"] is False
    assert formatters["schema_version"] == "harness.formatters/v1"
    assert formatter_alias == formatters
    assert formatters["enabled"] is False
    assert formatters["process_started"] is False
    assert formatters["permission_granting"] is False
    assert mcp_status["schema_version"] == "harness.mcp_status/v1"
    assert mcp_status["enabled"] is False
    assert mcp_status["connected"] is False
    assert mcp_status["tool_execution_supported"] is False
    assert mcp_status["resource_reads_cached_only"] is True
    assert mcp_status["policy_boundary"]["kind"] == "mcp_metadata_projection"
    assert mcp_status["policy_boundary"]["tool_execution_allowed"] is False
    assert mcp_status["blocked_reasons"] == [
        "mcp_disabled",
        "mcp_process_launch_disabled",
        "mcp_network_connection_disabled",
        "mcp_tool_execution_disabled",
    ]
    assert mcp_status["process_started"] is False
    assert mcp_status["network_called"] is False
    assert mcp_status["permission_granting"] is False
    assert mcp_resources["schema_version"] == "harness.mcp_resources/v1"
    assert mcp_resources["resources"] == []
    assert mcp_resources["cached_only"] is True
    assert mcp_resources["contents_included"] is False
    assert mcp_resources["tool_execution_supported"] is False
    assert mcp_resources["resource_read_supported"] is False
    assert mcp_resources["session_tool_resource_read_supported"] is True
    assert mcp_resources["policy_boundary"]["kind"] == "mcp_resources_projection"
    assert mcp_resources["permission_granting"] is False
    assert plugins["schema_version"] == "harness.plugins/v1"
    assert plugins["enabled"] is False
    assert plugins["runtime_loaded"] is False
    assert plugins["tools_registered"] is False
    assert plugins["tool_execution_supported"] is False
    assert plugins["policy_boundary"]["kind"] == "plugin_catalog_metadata"
    assert plugins["policy_boundary"]["runtime_load_allowed"] is False
    assert plugins["blocked_reasons"] == [
        "plugin_origin_review_required",
        "plugin_runtime_load_disabled",
        "plugin_tool_execution_disabled",
    ]
    assert plugins["permission_granting"] is False
    assert skills["schema_version"] == "harness.skills/v1"
    assert skill_alias == skills
    assert skills["enabled"] is False
    assert skills["runtime_loaded"] is False
    assert skills["tool_registered"] is False
    assert skills["session_tool_load_supported"] is True
    assert skills["permission_granting"] is False
    assert web_tools["schema_version"] == "harness.web_tools/v1"
    assert web_tools["enabled"] is False
    assert web_tools["network_called"] is False
    assert web_tools["execution_supported"] is False
    assert web_tools["session_tool_execution_supported"] is True
    assert web_tools["permission_granting"] is False
    assert {tool["id"] for tool in web_tools["tools"]} == {"web-fetch", "web-search"}
    assert all(tool["decision"] == "denied" for tool in web_tools["tools"])
    assert extensions["schema_version"] == "harness.extensions_status/v1"
    assert extensions["mcp"]["process_started"] is False
    assert extensions["plugins"]["runtime_loaded"] is False
    assert extensions["skills"]["skill_body_loaded"] is False
    assert extensions["web_tools"]["network_called"] is False
    assert extensions["policy"]["permission_granting"] is False
    assert extensions["policy"]["runtime_loaded"] is False
    assert extensions["policy"]["process_started"] is False
    assert extensions["policy"]["network_called"] is False
    assert extensions["policy"]["filesystem_modified"] is False
    assert web_client["schema_version"] == "harness.web_client/v1"
    assert web_client["client_available"] is False
    assert web_client["static_assets_served"] is False
    assert web_client["open_supported"] is False
    assert web_client["browser_opened"] is False
    assert web_client["process_started"] is False
    assert web_client["permission_granting"] is False
    assert worktrees["schema_version"] == "harness.worktrees/v1"
    assert worktrees["available"] is False
    assert worktrees["mutation_supported"] is False
    assert worktrees["permission_granting"] is False
    assert dev_loop["schema_version"] == "harness.dev_loop_status/v1"
    assert dev_loop["pty"]["managed_pty_supported"] is False
    assert dev_loop["pty"]["process_started"] is False
    assert dev_loop["worktrees"]["mutation_supported"] is False
    assert dev_loop["session"] is None
    assert dev_loop["policy"]["terminal_process_started"] is False
    assert dev_loop["policy"]["workspace_mutation_started"] is False
    assert dev_loop["policy"]["filesystem_modified"] is False
    assert dev_loop["policy"]["git_mutation_started"] is False
    assert dev_loop["permission_granting"] is False
    assert workspaces["schema_version"] == "harness.workspaces/v1"
    assert workspaces["workspace_routing_supported"] is True
    assert workspaces["global_registry_supported"] is False
    assert workspaces["remote_attach_supported"] is False
    assert workspaces["sync_supported"] is False
    assert workspaces["workspaces"][0]["current"] is True
    assert workspaces["network_called"] is False
    assert workspaces["filesystem_modified"] is False
    assert workspaces["permission_granting"] is False
    assert experimental_workspaces == workspaces
    assert workspace_adapters["schema_version"] == "harness.workspace_adapters/v1"
    assert workspace_adapters["adapters"][0]["create_supported"] is False
    assert workspace_adapters["permission_granting"] is False
    assert workspace_status["schema_version"] == "harness.workspace_status/v1"
    assert workspace_status["statuses"][0]["current"] is True
    assert workspace_status["statuses"][0]["sync_enabled"] is False
    assert workspace_status["permission_granting"] is False
    assert workspace_clients["schema_version"] == "harness.workspace_clients/v1"
    assert workspace_clients["clients"] == []
    assert workspace_clients["client_registration_supported"] is False
    assert workspace_clients["conflict_detection_supported"] is False
    assert workspace_clients["permission_granting"] is False
    assert pty_sessions["schema_version"] == "harness.pty_sessions/v1"
    assert pty_sessions["sessions"] == []
    assert pty_sessions["process_started"] is False
    assert pty_sessions["permission_granting"] is False
    assert pty_shells["schema_version"] == "harness.pty_shells/v1"
    assert pty_shells["probed"] is False
    assert pty_shells["process_started"] is False
    assert pty_shells["permission_granting"] is False
    assert distribution["schema_version"] == "harness.distribution_status/v1"
    assert distribution["packaging_path"] == "python_wheel_first"
    assert distribution["network_called"] is False
    assert distribution["filesystem_modified"] is False
    assert distribution["subprocess_started"] is False
    assert distribution["permission_granting"] is False
    assert packaging_smoke["schema_version"] == "harness.packaging_smoke/v1"
    assert packaging_smoke["packaging_path"] == "python_wheel_first"
    assert packaging_smoke["wheel_smoke_supported"] is True
    assert packaging_smoke["execution_supported"] is False
    assert packaging_smoke["subprocess_started"] is False
    assert packaging_smoke["filesystem_modified"] is False
    assert packaging_smoke["permission_granting"] is False
    assert "local_server_openapi" in packaging_smoke["covers"]
    assert desktop["schema_version"] == "harness.desktop_status/v1"
    assert desktop["packaging_decision"] == "python_wheel_first"
    assert desktop["desktop_wrapper_supported"] is False
    assert desktop["launch_supported"] is False
    assert desktop["process_started"] is False
    assert desktop["permission_granting"] is False
    assert version_check["schema_version"] == "harness.version_check/v1"
    assert version_check["network_called"] is False
    assert version_check["subprocess_started"] is False
    assert version_check["permission_granting"] is False
    assert tui_settings["schema_version"] == "harness.tui_settings/v1"
    assert tui_settings["filesystem_modified"] is False
    assert tui_settings["process_started"] is False
    assert tui_settings["permission_granting"] is False
    assert {setting["key"] for setting in tui_settings["settings"]} >= {"theme", "terminal_font_size", "keybinding_preset"}
    assert commands["schema_version"] == "harness.commands/v1"
    assert command_alias["schema_version"] == "harness.commands/v1"
    assert command_alias["execution_supported"] is False
    assert commands["execution_supported"] is False
    assert commands["contents_included"] is False
    assert commands["permission_granting"] is False
    assert providers["schema_version"] == "harness.providers/v1"
    assert providers["policy_boundary"]["kind"] == "providers_catalog_projection"
    assert providers["metadata_only"] is True
    assert providers["provider_execution_started"] is False
    assert providers["model_execution_started"] is False
    assert providers["network_accessed"] is False
    assert providers["credentials_included"] is False
    assert providers["credential_write_supported"] is False
    assert providers["credential_written"] is False
    assert providers["refresh_supported"] is False
    assert providers["hidden_provider_fallback"] is False
    assert providers["hidden_model_fallback"] is False
    assert providers["permission_granting"] is False
    assert providers["authority_granting"] is False
    assert providers["no_hidden_fallback"] is True
    assert provider_alias["providers"] == providers["providers"]
    assert provider_alias["credentials_included"] is False
    assert provider_auth["schema_version"] == "harness.provider_auth_methods/v1"
    assert provider_auth["credentials_included"] is False
    assert provider_auth["permission_granting"] is False
    assert provider_auth["credentials_included"] is False
    assert any(method["oauth_supported"] is True for method in provider_auth["providers"])
    assert api_providers["providers"] == providers["providers"]
    assert api_provider["provider"]["provider_id"] == first_provider_id
    assert api_provider["credentials_included"] is False
    assert api_provider["provider_execution_started"] is False
    assert models["no_hidden_fallback"] is True
    assert models["policy_boundary"]["kind"] == "models_catalog_projection"
    assert models["metadata_only"] is True
    assert models["provider_execution_started"] is False
    assert models["model_execution_started"] is False
    assert models["network_accessed"] is False
    assert models["credentials_included"] is False
    assert models["refresh_supported"] is False
    assert models["hidden_provider_fallback"] is False
    assert models["hidden_model_fallback"] is False
    assert models["permission_granting"] is False
    assert models["authority_granting"] is False
    assert api_models["models"] == models["models"]
    assert api_models["no_hidden_fallback"] is True
    assert model_validation["schema_version"] == "harness.model_selection_validation_result/v1"
    assert model_validation["ok"] is True
    assert model_validation["validation"]["raw_model_ref"] == "codex_cli/gpt-5.5"
    assert model_validation["validation"]["executable"] is True
    assert model_validation["validation"]["known_catalog_entry"] is True
    assert model_validation["provider_execution_started"] is False
    assert model_validation["model_execution_started"] is False
    assert model_validation["network_accessed"] is False
    assert model_validation["credentials_included"] is False
    assert model_validation["hidden_provider_fallback"] is False
    assert model_validation["hidden_model_fallback"] is False
    assert model_validation["no_hidden_fallback"] is True
    assert model_validation["permission_granting"] is False
    assert model_validation["authority_granting"] is False
    assert api_model_validation["ok"] is False
    assert api_model_validation["validation"]["blocked_reasons"] == ["model_unknown"]
    assert api_model_validation["validation"]["provider_execution_started"] is False
    assert api_model_validation["validation"]["hidden_model_fallback"] is False
    assert sessions["sessions"][0]["id"] == session.id
    assert sessions_status["schema_version"] == "harness.sessions_status/v1"
    assert sessions_status["status_by_session"][session.id] == "active"
    assert sessions_status["session_count"] == 1
    assert sessions_status["execution_started"] is False
    assert sessions_status["permission_granting"] is False
    assert inspected["session"]["id"] == session.id
    assert inspected["latest_ui_activation"] is None
    assert inspected["permission_granting"] is False
    assert events["session_id"] == session.id
    assert session_status["schema_version"] == "harness.session_status/v1"
    assert session_status["session_id"] == session.id
    assert session_status["status"] == "active"
    assert session_status["message_count"] == 1
    assert session_status["event_count"] >= 3
    assert session_status["latest_ui_activation"] is None
    assert session_status["process_running"] is False
    assert session_status["permission_granting"] is False
    assert session_children["schema_version"] == "harness.session_children/v1"
    assert session_children["session_id"] == session.id
    assert session_children["children"] == []
    assert session_children["execution_started"] is False
    assert session_children["permission_granting"] is False
    assert replay["schema_version"] == "harness.session_replay/v1"
    assert replay["source"] == "append_only_event_store"
    assert replay["execution_started"] is False
    assert replay["network_called"] is False
    assert replay["permission_granting"] is False
    assert messages["messages"][0]["id"] == message.id
    assert messages["parts"][message.id][0]["text"] == "Replay this"
    assert permissions["permissions"][0]["id"] == permission.id
    assert permissions["snapshot"]["pending_permission_ids"] == [permission.id]
    assert permissions["permission_granting"] is False
    assert permission_snapshot["schema_version"] == "harness.session_permission_snapshot/v1"
    assert permission_snapshot["pending_count"] == 1
    assert permission_snapshot["permission_granting"] is False
    assert diffs["schema_version"] == "harness.session_diffs/v1"
    assert diffs["revert_supported"] is False
    assert diffs["mutation_started"] is False
    assert diffs["permission_granting"] is False
    assert share["schema_version"] == "harness.session_share/v1"
    assert share["share_mode"] == "local_snapshot"
    assert share["hosted_share_supported"] is False
    assert share["artifact_files_included"] is False
    assert share["network_called"] is False
    assert share["filesystem_modified"] is False
    assert share["permission_granting"] is False
    assert stream_projection["transport"] == "sse"
    assert stream_projection["permission_granting"] is False
    assert any(event["kind"] == "session.message.appended" for event in events["events"])
    assert "event: harness.ready" in sse
    assert f'"session_id": "{session.id}"' in sse
    assert "event: session.message.appended" in sse
    assert "event: server.connected" in global_sse
    assert "event: session.message.appended" in global_sse
    assert str(tmp_path) in global_sse
    assert "\x1b[" not in sse
    assert "\x1b[" not in global_sse
    serialized = json.dumps([config, global_config, path_info, projects, current_project, vcs, vcs_status, vcs_diff, lifecycle, mdns, agents, artifacts, files, file_content, file_status, references, instructions, symbols, diagnostics, formatters, mcp_status, mcp_resources, plugins, skills, web_tools, extensions, web_client, worktrees, dev_loop, workspaces, workspace_adapters, workspace_status, workspace_clients, pty_sessions, pty_shells, distribution, packaging_smoke, desktop, version_check, tui_settings, command_alias, commands, providers, provider_auth, models, sessions, sessions_status, inspected, events, global_events, session_status, session_children, replay, messages, permissions, permission_snapshot, diffs, share])
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in serialized
    assert '"api_key":' not in serialized
    assert "ollama" not in serialized
    assert "server artifact body" not in serialized


def test_local_server_session_projections_surface_malformed_transcript_health_without_body(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    cfg = load_config(tmp_path)
    session = store.create_session(title="Malformed transcript")
    append_session_event(
        tmp_path,
        session_id=session.id,
        event_type=SessionEventKind.SESSION_STARTED,
        message="Started",
    )
    transcript_path = tmp_path / ".harness" / "sessions" / session.id / "transcript.jsonl"
    with transcript_path.open("a", encoding="utf-8") as handle:
        handle.write("not json with secret sk-abcdefghijklmnopqrstuvwxyz\n")

    sessions = _route_get("/sessions", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    api_sessions = _route_get("/api/session", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    sessions_status = _route_get("/sessions/status", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    inspected = _route_get(f"/sessions/{session.id}", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    status = _route_get(
        f"/sessions/{session.id}/status",
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )

    health_payloads = [
        sessions["sessions"][0]["transcript_health"],
        api_sessions["items"][0]["transcript_health"],
        sessions_status["sessions"][0]["transcript_health"],
        sessions_status["transcript_health_by_session"][session.id],
        inspected["transcript_health"],
        inspected["session"]["transcript_health"],
        status["transcript_health"],
    ]
    assert sessions_status["malformed_transcript_session_ids"] == [session.id]
    for health in health_payloads:
        assert health["schema_version"] == "harness.session_events_read/v1"
        assert health["ok"] is False
        assert health["parse_error_count"] == 1
        assert health["validation_error_count"] == 0
        assert health["contents_included"] is False
        assert health["permission_granting"] is False
    serialized = json.dumps([sessions, api_sessions, sessions_status, inspected, status])
    assert "not json" not in serialized
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in serialized
    assert "Traceback" not in serialized


def test_local_server_session_routes_expose_pending_chat_action_projection(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    state = ChatSessionState()
    draft = handle_chat_input("create dry run task", tmp_path, state)
    store = SQLiteStore(tmp_path)
    cfg = load_config(tmp_path)

    sessions = _route_get("/sessions", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    api_sessions = _route_get("/api/session", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    sessions_status = _route_get("/sessions/status", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    inspected = _route_get(f"/sessions/{state.session_id}", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    status = _route_get(f"/sessions/{state.session_id}/status", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)

    assert draft["kind"] == "task_draft"
    assert sessions["sessions"][0]["pending_action"]["kind"] == "task_draft"
    assert sessions["sessions"][0]["pending_action_audit"]["status"] == "recoverable"
    assert sessions["sessions"][0]["pending_action_audit"]["raw_metadata_exposed"] is False
    assert api_sessions["items"][0]["pending_action"]["requires_confirmation"] is True
    assert api_sessions["items"][0]["pending_action_audit"]["recoverable"] is True
    assert sessions_status["sessions"][0]["pending_action"]["process_started"] is False
    assert sessions_status["sessions"][0]["pending_action_audit"]["cleanup_supported"] is True
    assert inspected["pending_action"]["adapter_dispatch_started"] is False
    assert inspected["pending_action_audit"]["pending_action"]["kind"] == "task_draft"
    assert inspected["session"]["pending_action"]["permission_granting"] is False
    assert inspected["session"]["pending_action_audit"]["process_started"] is False
    assert status["pending_action"]["next_commands"] == ["/confirm", "/decline"]
    assert status["pending_action_audit"]["next_commands"] == ["/confirm", "/decline"]
    assert "pending_chat_action" not in sessions["sessions"][0]["metadata"]
    assert "pending_chat_action" not in api_sessions["items"][0]["metadata"]
    assert "pending_chat_action" not in inspected["session"]["metadata"]
    assert len(store.list_tasks()) == 0


def test_local_server_session_routes_expose_stale_active_run_projection_without_repair(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    cfg = load_config(tmp_path)
    session = store.create_session(title="Stale active run")
    missing_run_id = "run_missing_for_server_projection"
    with store.connect() as conn:
        conn.execute("UPDATE sessions SET active_run_id = ? WHERE id = ?", (missing_run_id, session.id))

    sessions = _route_get("/sessions", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    api_sessions = _route_get("/api/session", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    sessions_status = _route_get("/sessions/status", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    inspected = _route_get(f"/sessions/{session.id}", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    status = _route_get(f"/sessions/{session.id}/status", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)

    for payload in (
        sessions["sessions"][0]["active_run_reference"],
        api_sessions["items"][0]["active_run_reference"],
        sessions_status["sessions"][0]["active_run_reference"],
        inspected["active_run_reference"],
        inspected["session"]["active_run_reference"],
        status["active_run_reference"],
    ):
        assert payload["schema_version"] == "harness.session_active_run_reference/v1"
        assert payload["status"] == "stale"
        assert payload["missing_run_id"] == missing_run_id
        assert payload["repairable"] is True
        assert payload["repair_scope"] == "session_active_run_pointer_only"
        assert payload["process_started"] is False
        assert payload["provider_called"] is False
        assert payload["network_called"] is False
        assert payload["filesystem_modified"] is False
        assert payload["permission_granting"] is False
        assert "harness doctor --repair" in payload["repair_command"]

    assert sessions_status["stale_active_run_refs"] == 1
    assert sessions_status["valid_active_run_refs"] == 0
    assert sessions_status["active_run_refs"] == 1
    assert SQLiteStore(tmp_path).get_session(session.id).active_run_id == missing_run_id
    assert not SQLiteStore(tmp_path).list_runs()
    assert not SQLiteStore(tmp_path).list_tasks()


def test_local_server_audits_and_clears_invalid_pending_chat_action_metadata_only(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    cfg = load_config(tmp_path)
    session = store.create_session(
        title="Broken pending action",
        metadata={
            "pending_chat_action": {
                "schema_version": "harness.pending_chat_action/v1",
                "kind": "task_draft",
            },
            "cwd": ".",
        },
    )

    sessions = _route_get("/sessions", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    inspected = _route_get(f"/sessions/{session.id}", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    audit = _route_get(
        f"/sessions/{session.id}/pending-action",
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )

    assert sessions["sessions"][0]["pending_action"] is None
    assert sessions["sessions"][0]["pending_action_audit"]["status"] == "invalid"
    assert sessions["sessions"][0]["pending_action_audit"]["issues"][0]["code"] == "missing_task_draft"
    assert "pending_chat_action" not in sessions["sessions"][0]["metadata"]
    assert inspected["pending_action"] is None
    assert inspected["pending_action_audit"]["cleanup_route"] == f"DELETE /sessions/{session.id}/pending-action"
    assert audit["pending_action_audit"]["recoverable"] is False
    assert audit["pending_action_audit"]["process_started"] is False
    assert len(store.list_tasks()) == 0

    cleared = _route_delete(f"/sessions/{session.id}/pending-action", store=store, project_root=tmp_path, cfg=cfg)
    after = _route_get(
        f"/sessions/{session.id}/pending-action",
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )

    assert cleared["cleared"] is True
    assert cleared["audit_before"]["status"] == "invalid"
    assert cleared["audit_after"]["status"] == "missing"
    assert cleared["mutation_scope"] == "session_metadata_only"
    assert cleared["tasks_mutated"] is False
    assert cleared["leases_mutated"] is False
    assert cleared["runs_mutated"] is False
    assert cleared["approvals_mutated"] is False
    assert cleared["artifacts_mutated"] is False
    assert cleared["messages_mutated"] is False
    assert cleared["events_deleted"] is False
    assert "pending_chat_action" not in store.get_session(session.id).metadata
    assert after["pending_action_audit"]["status"] == "missing"
    assert len(store.list_tasks()) == 0


def test_local_server_session_projections_surface_latest_ui_activation_as_passive_context(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    cfg = load_config(tmp_path)
    session = store.create_session(title="UI activation context")
    store.append_store_event(
        EventStreamType.SESSION,
        session.id,
        "tui.ui_activation.applied",
        {
            "source": "slash",
            "entry_id": "ui_controls.settings",
            "activation_kind": "ui_toggle",
            "action": {"type": "open_settings"},
            "ui_action_applied": True,
            "command_started": False,
            "process_started": False,
            "filesystem_modified": False,
            "permission_granting": False,
            "authority_granting": False,
        },
        session_id=session.id,
    )

    inspected = _route_get(
        f"/sessions/{session.id}",
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    status = _route_get(
        f"/sessions/{session.id}/status",
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )

    assert inspected["latest_ui_activation"] == status["latest_ui_activation"]
    latest = inspected["latest_ui_activation"]
    assert latest["entry_id"] == "ui_controls.settings"
    assert latest["source"] == "slash"
    assert latest["activation_kind"] == "ui_toggle"
    assert latest["action_type"] == "open_settings"
    assert latest["evidence_status"] == "ui_only_persisted"
    assert latest["policy_boundary"]["kind"] == "safe_ui_activation"
    assert latest["policy_boundary"]["process_start_allowed"] is False
    assert latest["policy_boundary"]["filesystem_mutation_allowed"] is False
    assert latest["blocked_reasons"] == []
    assert latest["ui_action_applied"] is True
    assert latest["command_started"] is False
    assert latest["process_started"] is False
    assert latest["filesystem_modified"] is False
    assert latest["permission_granting"] is False
    assert latest["authority_granting"] is False
    assert inspected["permission_granting"] is False
    assert status["process_running"] is False
    assert status["permission_granting"] is False


def test_local_server_post_persists_session_prompt_without_execution(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    cfg = load_config(tmp_path)

    created = _route_post(
        "/sessions",
        body={
            "title": "API session",
            "prompt": "Plan a safe implementation",
            "raw_model_ref": "codex_cli/gpt-5.5",
            "agent_id": "plan",
        },
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )

    assert created is not None
    assert created["schema_version"] == "harness.local_server_session_create/v1"
    assert created["execution_started"] is False
    assert created["provider_execution_started"] is False
    assert created["model_execution_started"] is False
    assert created["permission_granting"] is False
    assert created["authority_granting"] is False
    assert created["no_hidden_fallback"] is True
    assert created["model_validation"]["raw_model_ref"] == "codex_cli/gpt-5.5"
    assert created["model_validation"]["executable"] is True
    assert created["model_validation"]["provider_execution_started"] is False
    assert created["model_validation"]["model_execution_started"] is False
    assert created["model_validation"]["hidden_model_fallback"] is False
    assert created["model_validation"]["permission_granting"] is False
    assert created["model_validation"]["authority_granting"] is False
    assert created["session"]["title"] == "API session"
    assert created["session"]["raw_model_ref"] == "codex_cli/gpt-5.5"
    assert created["message"]["role"] == "user"
    assert created["message"]["agent_id"] == "plan"
    assert created["message"]["content_preview"] == "Plan a safe implementation"
    assert created["part"]["kind"] == "text"
    assert created["part"]["text"] == "Plan a safe implementation"

    session_id = created["session"]["id"]
    appended = _route_post(
        f"/sessions/{session_id}/messages",
        body={"content": "Add tests first"},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    events = _route_get(
        f"/sessions/{session_id}/events",
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    messages = _route_get(
        f"/sessions/{session_id}/messages",
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    message_detail = _route_get(
        f"/sessions/{session_id}/messages/{appended['message']['id']}",
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    prompt_async = _route_post(
        f"/sessions/{session_id}/prompt_async",
        body={
            "agent": "plan",
            "parts": [
                {"type": "text", "text": "Async follow-up"},
                {"type": "text", "text": "Keep it read-only"},
            ],
        },
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    after_async = _route_get(
        f"/sessions/{session_id}/messages",
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    limited = _route_get(
        f"/sessions/{session_id}/messages",
        query={"limit": ["2"]},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    alias_appended = _route_post(
        f"/session/{session_id}/message",
        body={"parts": [{"text": "Alias message"}]},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    alias_messages = _route_get(
        f"/session/{session_id}/message",
        query={"limit": ["1"]},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    alias_detail = _route_get(
        f"/session/{session_id}/message/{alias_appended['message']['id']}",
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    v2_sessions = _route_get(
        "/api/session",
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    v2_prompt = _route_post(
        f"/api/session/{session_id}/prompt",
        body={
            "agent": "plan",
            "prompt": {
                "parts": [
                    {"type": "text", "text": "V2 prompt"},
                    {"type": "text", "text": "Persist only"},
                ]
            },
        },
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    v2_messages = _route_get(
        f"/api/session/{session_id}/message",
        query={"limit": ["1"]},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    v2_context = _route_get(
        f"/api/session/{session_id}/context",
        query={"limit": ["2"], "event_limit": ["2"]},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    v2_compact = _route_post(
        f"/api/session/{session_id}/compact",
        body={},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    v2_wait = _route_post(
        f"/api/session/{session_id}/wait",
        body={"timeout": 0},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    sse = build_session_sse_stream(store, f"/session/{session_id}/events/stream")

    assert appended is not None
    assert appended["schema_version"] == "harness.local_server_message_append/v1"
    assert appended["execution_started"] is False
    assert appended["model_validation"] is None
    assert appended["message"]["role"] == "user"
    assert message_detail["schema_version"] == "harness.session_message/v1"
    assert message_detail["message"]["id"] == appended["message"]["id"]
    assert message_detail["parts"][0]["text"] == "Add tests first"
    assert message_detail["execution_started"] is False
    assert message_detail["permission_granting"] is False
    assert prompt_async["schema_version"] == "harness.local_server_prompt_async/v1"
    assert prompt_async["async_accepted"] is True
    assert prompt_async["waited_for_response"] is False
    assert prompt_async["assistant_response_started"] is False
    assert prompt_async["execution_started"] is True
    assert prompt_async["provider_execution_started"] is True
    assert prompt_async["model_execution_started"] is True
    assert prompt_async["turn_id"]
    assert prompt_async["permission_granting"] is False
    assert prompt_async["authority_granting"] is False
    assert prompt_async["no_hidden_fallback"] is True
    assert prompt_async["model_validation"] is None
    assert prompt_async["message"]["agent_id"] == "plan"
    assert prompt_async["part"]["text"] == "Async follow-up\n\nKeep it read-only"
    assert [message["content_preview"] for message in messages["messages"]] == [
        "Plan a safe implementation",
        "Add tests first",
    ]
    assert [message["content_preview"] for message in after_async["messages"]] == [
        "Plan a safe implementation",
        "Add tests first",
        "Async follow-up\n\nKeep it read-only",
    ]
    assert limited["limit"] == 2
    assert [message["content_preview"] for message in limited["messages"]] == [
        "Add tests first",
        "Async follow-up\n\nKeep it read-only",
    ]
    assert set(limited["parts"]) == {message["id"] for message in limited["messages"]}
    assert alias_appended["schema_version"] == "harness.local_server_message_append/v1"
    assert alias_appended["execution_started"] is False
    assert alias_messages["schema_version"] == "harness.session_messages/v1"
    assert alias_messages["limit"] == 1
    assert alias_messages["messages"][0]["content_preview"] == "Alias message"
    assert alias_messages["parts"][alias_appended["message"]["id"]][0]["text"] == "Alias message"
    assert alias_detail["schema_version"] == "harness.session_message/v1"
    assert alias_detail["message"]["id"] == alias_appended["message"]["id"]
    assert alias_detail["parts"][0]["text"] == "Alias message"
    assert v2_sessions["schema_version"] == "harness.api_sessions/v1"
    assert any(item["id"] == session_id for item in v2_sessions["items"])
    assert v2_prompt["schema_version"] == "harness.api_session_prompt/v1"
    assert v2_prompt["mode"] == "append_only"
    assert v2_prompt["assistant_execution"] is False
    assert v2_prompt["part"]["text"] == "V2 prompt\n\nPersist only"
    assert v2_prompt["execution_started"] is False
    assert v2_prompt["provider_execution_started"] is False
    assert v2_prompt["model_execution_started"] is False
    assert v2_prompt["no_hidden_fallback"] is True
    assert v2_prompt["model_validation"] is None
    assert v2_messages["schema_version"] == "harness.api_session_messages/v1"
    assert v2_messages["items"][0]["message"]["content_preview"] == "V2 prompt\n\nPersist only"
    assert v2_messages["items"][0]["parts"][0]["text"] == "V2 prompt\n\nPersist only"
    assert v2_context["schema_version"] == "harness.api_session_context/v1"
    assert len(v2_context["messages"]) == 2
    assert v2_context["context_window_loaded"] is False
    assert v2_context["provider_execution_started"] is False
    assert v2_compact["schema_version"] == "harness.api_session_compact/v1"
    assert v2_compact["ok"] is False
    assert v2_compact["compacted"] is False
    assert v2_compact["no_hidden_fallback"] is True
    assert v2_wait["schema_version"] == "harness.api_session_wait/v1"
    assert v2_wait["waited"] is False
    assert v2_wait["agent_loop_running"] is False
    assert [event["kind"] for event in events["events"]] == [
        "session.created",
        "session.model_validation",
        "session.message.appended",
        "session.part.appended",
        "session.message.appended",
        "session.part.appended",
    ]
    assert "event: session.message.appended" in sse
    assert "Add tests first" in sse

    with pytest.raises(ValueError, match="Only user messages"):
        _route_post(
            f"/sessions/{session_id}/messages",
            body={"role": "assistant", "content": "No execution path here"},
            project_root=tmp_path,
            store=store,
            cfg=cfg,
            host="127.0.0.1",
            port=8765,
        )


def test_local_server_model_refs_validate_without_hidden_fallback(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    cfg = load_config(tmp_path)

    created = _route_post(
        "/sessions",
        body={"title": "Unknown model", "model": "codex_cli/not-a-real-model", "prompt": "Persist only"},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    session_id = created["session"]["id"]
    prompted = _route_post(
        f"/api/session/{session_id}/prompt",
        body={"providerID": "codex_cli", "modelID": "not-a-real-model", "prompt": "Second prompt"},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    patched = _route_patch(
        f"/sessions/{session_id}",
        body={"modelID": "gpt-5.5", "providerID": "codex_cli"},
        store=store,
        cfg=cfg,
    )
    events = _route_get(
        f"/sessions/{session_id}/events",
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )

    assert created["ok"] is True
    assert created["execution_started"] is False
    assert created["provider_execution_started"] is False
    assert created["model_execution_started"] is False
    assert created["no_hidden_fallback"] is True
    assert created["model_validation"]["raw_model_ref"] == "codex_cli/not-a-real-model"
    assert created["model_validation"]["executable"] is False
    assert created["model_validation"]["blocked_reasons"] == ["model_unknown"]
    assert created["model_validation"]["hidden_model_fallback"] is False
    assert prompted["ok"] is True
    assert prompted["mode"] == "append_only"
    assert prompted["assistant_execution"] is False
    assert prompted["execution_started"] is False
    assert prompted["model_validation"]["raw_model_ref"] == "codex_cli/not-a-real-model"
    assert prompted["model_validation"]["executable"] is False
    assert prompted["model_validation"]["blocked_reasons"] == ["model_unknown"]
    assert prompted["model_execution_started"] is False
    assert prompted["no_hidden_fallback"] is True
    assert patched["model_updated"] is True
    assert patched["session"]["raw_model_ref"] == "codex_cli/gpt-5.5"
    assert patched["model_validation"]["raw_model_ref"] == "codex_cli/gpt-5.5"
    assert patched["model_validation"]["executable"] is True
    assert patched["model_validation"]["provider_execution_started"] is False
    assert patched["no_hidden_fallback"] is True
    model_validation_events = [event for event in events["events"] if event["kind"] == "session.model_validation"]
    assert [event["payload"]["source"] for event in model_validation_events] == [
        "local_server_session_create",
        "api_session_prompt",
        "local_server_session_update",
    ]
    assert all(event["payload"]["hidden_model_fallback"] is False for event in model_validation_events)
    assert all(event["payload"]["permission_granting"] is False for event in model_validation_events)


def test_local_server_patch_updates_session_metadata_and_delete_archives(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    session = store.create_session(title="Old title", raw_model_ref="codex_cli/gpt-5.5")

    patched = _route_patch(
        f"/sessions/{session.id}",
        body={
            "title": "New title",
            "raw_model_ref": "codex_cli/gpt-5.6",
            "provider_id": "codex_cli",
            "model_id": "gpt-5.6",
        },
        store=store,
        cfg=load_config(tmp_path),
    )
    events = _route_get(
        f"/sessions/{session.id}/events",
        project_root=tmp_path,
        store=store,
        cfg=load_config(tmp_path),
        host="127.0.0.1",
        port=8765,
    )
    archived = _route_delete(f"/sessions/{session.id}", store=store)
    after_archive_events = _route_get(
        f"/sessions/{session.id}/events",
        project_root=tmp_path,
        store=store,
        cfg=load_config(tmp_path),
        host="127.0.0.1",
        port=8765,
    )

    assert patched["schema_version"] == "harness.session_update/v1"
    assert patched["session"]["title"] == "New title"
    assert patched["session"]["raw_model_ref"] == "codex_cli/gpt-5.6"
    assert patched["session"]["provider_id"] == "codex_cli"
    assert patched["title_updated"] is True
    assert patched["model_updated"] is True
    assert patched["model_validation"]["raw_model_ref"] == "codex_cli/gpt-5.6"
    assert patched["model_validation"]["executable"] is False
    assert patched["model_validation"]["blocked_reasons"] == ["model_unknown"]
    assert patched["model_validation"]["hidden_model_fallback"] is False
    assert patched["messages_mutated"] is False
    assert patched["parts_mutated"] is False
    assert patched["execution_started"] is False
    assert patched["permission_granting"] is False
    assert patched["no_hidden_fallback"] is True
    assert "session.title_updated" in [event["kind"] for event in events["events"]]
    assert "session.model_selected" in [event["kind"] for event in events["events"]]
    assert "session.model_validation" in [event["kind"] for event in events["events"]]
    assert archived["schema_version"] == "harness.session_archive/v1"
    assert archived["session"]["status"] == "archived"
    assert archived["archived"] is True
    assert archived["hard_deleted"] is False
    assert archived["messages_deleted"] is False
    assert archived["parts_deleted"] is False
    assert archived["events_deleted"] is False
    assert archived["execution_started"] is False
    assert archived["permission_granting"] is False
    assert after_archive_events["events"][-1]["kind"] == "session.archived"

    with pytest.raises(ValueError, match="No supported mutable session fields"):
        _route_patch(f"/sessions/{session.id}", body={"summary": "not through patch"}, store=store)


def test_local_server_forks_session_at_message_without_execution(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    cfg = load_config(tmp_path)
    parent = store.create_session(title="Parent session", raw_model_ref="codex_cli/gpt-5.5", agent_id="plan")
    message = store.append_session_message(parent.id, "user", "Fork from here")
    store.append_session_part(parent.id, message.id, "text", text="Fork from here")

    forked = _route_post(
        f"/sessions/{parent.id}/fork",
        body={"messageID": message.id, "title": "Child branch"},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    children = _route_get(
        f"/sessions/{parent.id}/children",
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    child_events = _route_get(
        f"/sessions/{forked['session']['id']}/events",
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )

    assert forked["schema_version"] == "harness.session_fork/v1"
    assert forked["parent_session_id"] == parent.id
    assert forked["execution_started"] is False
    assert forked["permission_granting"] is False
    assert forked["session"]["title"] == "Child branch"
    assert forked["session"]["parent_session_id"] == parent.id
    assert forked["session"]["forked_from_message_id"] == message.id
    assert forked["session"]["raw_model_ref"] == "codex_cli/gpt-5.5"
    assert forked["session"]["agent_id"] == "plan"
    assert forked["session"]["metadata"]["created_by"] == "harness_serve"
    assert children["child_session_ids"] == [forked["session"]["id"]]
    assert children["children"][0]["forked_from_message_id"] == message.id
    assert child_events["events"][-1]["kind"] == "session.forked"
    assert child_events["events"][-1]["payload"]["parent_session_id"] == parent.id

    with pytest.raises(KeyError, match="Session message not found"):
        _route_post(
            f"/sessions/{parent.id}/fork",
            body={"message_id": "msg_missing"},
            project_root=tmp_path,
            store=store,
            cfg=cfg,
            host="127.0.0.1",
            port=8765,
        )


def test_local_server_real_http_auth_post_and_sse_flow(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    server = create_local_http_server(tmp_path, host="127.0.0.1", port=0, token="test-token")
    host, port = server.server_address
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://{host}:{port}"
    try:
        with pytest.raises(HTTPError) as unauthorized:
            _http_json(f"{base_url}/health")
        assert unauthorized.value.code == 401

        options = Request(f"{base_url}/sessions", method="OPTIONS")
        with urlopen(options, timeout=5) as response:
            assert response.status == 204
            assert response.headers["Access-Control-Allow-Methods"] == "GET, POST, PUT, PATCH, DELETE, OPTIONS"
            assert response.headers["X-Harness-Permission-Granting"] == "false"

        created = _http_json(
            f"{base_url}/sessions",
            method="POST",
            token="test-token",
            body={"title": "HTTP session", "prompt": "Persist this prompt"},
        )
        session_id = created["session"]["id"]
        appended = _http_json(
            f"{base_url}/sessions/{session_id}/messages",
            method="POST",
            token="test-token",
            body={"content": "Second prompt"},
        )
        events = _http_json(f"{base_url}/sessions/{session_id}/events", token="test-token")
        sse = _http_text(f"{base_url}/sessions/{session_id}/events/stream", token="test-token")

        assert created["schema_version"] == "harness.local_server_session_create/v1"
        assert created["execution_started"] is False
        assert appended["schema_version"] == "harness.local_server_message_append/v1"
        assert appended["permission_granting"] is False
        assert [event["kind"] for event in events["events"]] == [
            "session.created",
            "session.message.appended",
            "session.part.appended",
            "session.message.appended",
            "session.part.appended",
        ]
        assert "event: harness.ready" in sse
        assert "event: session.message.appended" in sse
        assert "Second prompt" in sse
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_local_server_real_http_errors_are_shaped_and_bounded(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    server = create_local_http_server(
        tmp_path,
        host="127.0.0.1",
        port=0,
        token="test-token",
        max_body_bytes=24,
        cors_origin="http://localhost:3000",
    )
    host, port = server.server_address
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://{host}:{port}"
    try:
        with pytest.raises(HTTPError) as unauthorized_error:
            _http_json(f"{base_url}/health")
        unauthorized = _http_error_json(unauthorized_error.value)
        assert unauthorized["schema_version"] == "harness.local_server_error/v1"
        assert unauthorized["status"] == 401
        assert unauthorized["error_code"] == "unauthorized"
        assert unauthorized["permission_granting"] is False

        invalid_request = Request(f"{base_url}/sessions", data=b'{"title":', method="POST")
        invalid_request.add_header("Authorization", "Bearer test-token")
        invalid_request.add_header("Content-Type", "application/json")
        with pytest.raises(HTTPError) as invalid_error:
            urlopen(invalid_request, timeout=5)
        invalid = _http_error_json(invalid_error.value)
        assert invalid["status"] == 400
        assert invalid["error_code"] == "invalid_json"
        assert "Invalid JSON body" in invalid["error"]

        large_request = Request(f"{base_url}/sessions", data=b'{"title":"this body is too large"}', method="POST")
        large_request.add_header("Authorization", "Bearer test-token")
        large_request.add_header("Content-Type", "application/json")
        with pytest.raises(HTTPError) as large_error:
            urlopen(large_request, timeout=5)
        large = _http_error_json(large_error.value)
        assert large["status"] == 413
        assert large["error_code"] == "request_body_too_large"
        assert large_error.value.headers["Access-Control-Allow-Origin"] == "http://localhost:3000"
        assert large_error.value.headers["X-Harness-Max-Request-Body-Bytes"] == "24"

        with pytest.raises(HTTPError) as not_found_error:
            _http_json(f"{base_url}/missing", token="test-token")
        not_found = _http_error_json(not_found_error.value)
        assert not_found["status"] == 404
        assert not_found["error_code"] == "not_found"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_provider_auth_routes_require_local_server_auth(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    session = SQLiteStore(tmp_path).create_session(title="Protected model routes")
    server = create_local_http_server(tmp_path, host="127.0.0.1", port=0, token="model-token")
    host, port = server.server_address
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://{host}:{port}"

    protected_routes: list[tuple[str, str, dict[str, object] | None]] = [
        ("GET", "/providers", None),
        ("GET", "/providers/codex_cli", None),
        ("GET", "/provider/auth", None),
        ("GET", "/models", None),
        ("GET", "/models/codex_cli/gpt-5.5", None),
        ("GET", "/models/validate?model=codex_cli/gpt-5.5", None),
        ("GET", "/models/preferences", None),
        (
            "POST",
            "/provider/paid_openai_compatible/auth/api-key",
            {"api_key": "sk-unauthorized-secret", "description": "should not persist"},
        ),
        (
            "POST",
            "/provider/paid_openai_compatible/oauth/authorize",
            {"scopes": "models.read", "code_verifier": "unauthorized-verifier"},
        ),
        (
            "POST",
            f"/sessions/{session.id}/model",
            {"raw_model_ref": "codex_cli/gpt-5.5"},
        ),
        (
            "POST",
            "/models/preferences/favorite",
            {"raw_model_ref": "codex_cli/gpt-5.5", "favorite": True},
        ),
        (
            "POST",
            "/models/preferences/default",
            {"raw_model_ref": "codex_cli/gpt-5.5"},
        ),
        ("DELETE", "/provider/paid_openai_compatible/auth", None),
    ]

    def _assert_unauthorized(method: str, path: str, body: dict[str, object] | None, *, token: str | None) -> None:
        with pytest.raises(HTTPError) as error:
            _http_json(f"{base_url}{path}", method=method, token=token, body=body)
        payload = _http_error_json(error.value)
        assert error.value.code == 401
        assert payload["schema_version"] == "harness.local_server_error/v1"
        assert payload["status"] == 401
        assert payload["error_code"] == "unauthorized"
        assert payload["permission_granting"] is False
        assert "sk-unauthorized-secret" not in json.dumps(payload, sort_keys=True)

    try:
        for method, path, body in protected_routes:
            _assert_unauthorized(method, path, body, token=None)
            _assert_unauthorized(method, path, body, token="wrong-token")

        store_after_denials = SQLiteStore(tmp_path)
        assert store_after_denials.active_provider_account("paid_openai_compatible") is None
        assert store_after_denials.get_session(session.id).raw_model_ref is None
        assert store_after_denials.list_model_preferences() == []

        providers = _http_json(f"{base_url}/providers", token="model-token")
        provider = _http_json(f"{base_url}/providers/codex_cli", token="model-token")
        auth = _http_json(f"{base_url}/provider/auth", token="model-token")
        model = _http_json(f"{base_url}/models/codex_cli/gpt-5.5", token="model-token")
        unknown = _http_json(f"{base_url}/models/codex_cli/not-a-real-model", token="model-token")
        favorite = _http_json(
            f"{base_url}/models/preferences/favorite",
            method="POST",
            token="model-token",
            body={"raw_model_ref": "codex_cli/gpt-5.5", "favorite": True},
        )
        default = _http_json(
            f"{base_url}/models/preferences/default",
            method="POST",
            token="model-token",
            body={"raw_model_ref": "codex_cli/gpt-5.5"},
        )
        selected = _http_json(
            f"{base_url}/sessions/{session.id}/model",
            method="POST",
            token="model-token",
            body={"raw_model_ref": "codex_cli/gpt-5.5"},
        )
        connected = _http_json(
            f"{base_url}/provider/paid_openai_compatible/auth/api-key",
            method="POST",
            token="model-token",
            body={"api_key": "sk-authorized-secret", "description": "authorized test"},
        )
        deleted = _http_json(
            f"{base_url}/provider/paid_openai_compatible/auth",
            method="DELETE",
            token="model-token",
        )

        assert providers["schema_version"] == "harness.providers/v1"
        assert providers["credentials_included"] is False
        assert provider["provider"]["provider_id"] == "codex_cli"
        assert auth["schema_version"] == "harness.provider_auth_methods/v1"
        assert auth["credentials_included"] is False
        assert model["ok"] is True
        assert model["model"]["raw_model_ref"] == "codex_cli/gpt-5.5"
        assert unknown["ok"] is False
        assert unknown["suggestions"]
        assert favorite["preference"]["favorite"] is True
        assert default["preference"]["is_default"] is True
        assert selected["session_model_selected"] is True
        assert connected["account_created"] is True
        assert connected["credentials_included"] is False
        assert connected["credential_value_included"] is False
        assert "sk-authorized-secret" not in json.dumps(connected, sort_keys=True)
        assert deleted["account_deleted"] is True
        assert deleted["credential_removed"] is True
        store_after_success = SQLiteStore(tmp_path)
        assert store_after_success.get_session(session.id).raw_model_ref == "codex_cli/gpt-5.5"
        assert store_after_success.active_provider_account("paid_openai_compatible") is None
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_local_server_close_resets_supervised_processes(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    server = create_local_http_server(tmp_path, host="127.0.0.1", port=0, token="test-token")
    supervisor = get_process_supervisor(tmp_path)
    result_holder: dict[str, object] = {}

    def run_process() -> None:
        result_holder["result"] = supervisor.run(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            cwd=tmp_path,
            timeout_seconds=60,
            owner="test.local_server_shutdown",
        )

    thread = threading.Thread(target=run_process, daemon=True)
    thread.start()
    for _ in range(100):
        if supervisor.active_process_ids():
            break
        thread.join(timeout=0.01)
    assert supervisor.active_process_ids()

    server.server_close()
    thread.join(timeout=5)

    assert not supervisor.active_process_ids()
    assert result_holder["result"].status == "failed"


def test_harness_attach_reads_existing_server_without_project_filesystem_access(tmp_path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    session = store.create_session(title="Attach session")
    server = create_local_http_server(tmp_path, host="127.0.0.1", port=0, token="attach-token")
    host, port = server.server_address
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = runner.invoke(
            app,
            [
                "attach",
                "--server-url",
                f"http://{host}:{port}",
                "--token",
                "attach-token",
                "--output",
                "json",
            ],
        )

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["schema_version"] == "harness.local_server_attach/v1"
        assert payload["ok"] is True
        assert payload["permission_granting"] is False
        assert payload["health"]["schema_version"] == "harness.local_server/v1"
        assert payload["openapi_schema_version"] == "harness.local_server.openapi/v1"
        assert payload["session_count"] == 1
        assert payload["sessions"][0]["id"] == session.id
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    monkeypatch.delenv("HARNESS_SERVER_TOKEN", raising=False)
    missing = runner.invoke(app, ["attach", "--server-url", "http://127.0.0.1:9"])
    assert missing.exit_code != 0
    assert "Missing server token" in missing.output


def test_local_server_file_status_reports_changed_files_without_contents(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=tmp_path, check=True, capture_output=True)
    tracked = tmp_path / "app.py"
    tracked.write_text("print('one')\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=tmp_path, check=True, capture_output=True)
    tracked.write_text("print('two')\n", encoding="utf-8")
    (tmp_path / "notes.md").write_text("new notes\n", encoding="utf-8")
    (tmp_path / ".env").write_text("TOKEN=secret-token\n", encoding="utf-8")
    cfg = load_config(tmp_path)
    store = SQLiteStore(tmp_path)

    status = _route_get("/files/status", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    status_alias = _route_get("/file/status", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    vcs = _route_get("/vcs", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    vcs_status = _route_get("/vcs/status", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    vcs_diff = _route_get("/vcs/diff", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    vcs_diff_raw = _route_get("/vcs/diff/raw", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    apply = _route_post(
        "/vcs/apply",
        body={"patch": "diff --git a/app.py b/app.py\n"},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )

    assert status["schema_version"] == "harness.file_status/v1"
    assert status["available"] is True
    assert status["contents_included"] is False
    assert {file["path"] for file in status["files"]} >= {"app.py", "notes.md"}
    assert {file["path"] for file in status_alias["files"]} >= {"app.py", "notes.md"}
    assert not any(file["path"] == ".env" for file in status["files"])
    assert next(file for file in status["files"] if file["path"] == "app.py")["worktree_status"] == "M"
    assert next(file for file in status["files"] if file["path"] == "notes.md")["untracked"] is True
    assert vcs["schema_version"] == "harness.vcs/v1"
    assert vcs["available"] is True
    assert vcs["process_started"] is True
    assert vcs_status["files"] == status["files"]
    assert vcs_diff["schema_version"] == "harness.vcs_diff/v1"
    assert vcs_diff["raw"] is False
    assert "app.py" in vcs_diff["diff"]
    assert "print('two')" in vcs_diff["preview"]
    assert vcs_diff_raw["raw"] is True
    assert "diff --git" in vcs_diff_raw["diff"]
    assert apply["schema_version"] == "harness.vcs_apply_action/v1"
    assert apply["ok"] is False
    assert apply["patch_applied"] is False
    assert apply["filesystem_modified"] is False
    assert apply["permission_granting"] is False
    assert "print('two')" not in json.dumps(status)


def test_local_server_lists_worktrees_without_mutation(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "app.py").write_text("print('one')\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=tmp_path, check=True, capture_output=True)
    cfg = load_config(tmp_path)
    store = SQLiteStore(tmp_path)

    worktrees = _route_get("/worktrees", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)

    assert worktrees["schema_version"] == "harness.worktrees/v1"
    assert worktrees["available"] is True
    assert worktrees["mutation_supported"] is False
    assert worktrees["process_started"] is True
    assert worktrees["permission_granting"] is False
    assert len(worktrees["worktrees"]) == 1
    assert worktrees["worktrees"][0]["path"] == str(tmp_path)
    assert worktrees["worktrees"][0]["is_current"] is True
    assert worktrees["worktrees"][0]["mutation_supported"] is False

    create = _route_post(
        "/worktrees/create",
        body={"path": "candidate", "branch": "HEAD"},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    remove = _route_post(
        "/worktrees/remove",
        body={"path": "candidate"},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    reset = _route_post(
        "/worktrees/reset",
        body={"path": "candidate", "branch": "main"},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )

    for payload, action in [(create, "create"), (remove, "remove"), (reset, "reset")]:
        assert payload["schema_version"] == "harness.worktree_action/v1"
        assert payload["ok"] is False
        assert payload["action"] == action
        assert payload["plan"]["schema_version"] == "harness.worktree_plan/v1"
        assert payload["plan"]["managed_path"] == ".harness/worktrees/candidate"
        assert payload["plan"]["valid_target"] is True
        assert payload["plan"]["execution_supported"] is False
        assert payload["plan"]["mutation_supported"] is False
        assert payload["plan"]["approval_required"] is True
        assert payload["plan"]["required_approval"] == "managed_worktree_mutation"
        assert payload["plan"]["policy_boundary"]["kind"] == "managed_worktree"
        assert payload["plan"]["policy_boundary"]["managed_root"] == ".harness/worktrees"
        assert payload["plan"]["policy_boundary"]["active_workspace_mutation_allowed"] is False
        assert payload["plan"]["blocked_reasons"] == ["worktree_mutation_disabled"]
        assert payload["plan"]["executed"] is False
        assert payload["execution_supported"] is False
        assert payload["mutation_supported"] is False
        assert payload["approval_required"] is True
        assert payload["required_approval"] == "managed_worktree_mutation"
        assert payload["policy_boundary"]["kind"] == "managed_worktree"
        assert payload["blocked_reasons"] == ["worktree_mutation_disabled"]
        assert payload["git_mutation_started"] is False
        assert payload["filesystem_modified"] is False
        assert payload["worktree_created"] is False
        assert payload["worktree_removed"] is False
        assert payload["worktree_reset"] is False
        assert payload["process_started"] is False
        assert payload["permission_granting"] is False
    assert [step["name"] for step in create["plan"]["steps"]] == ["create_worktree"]
    assert create["plan"]["steps"][0]["command"] == [
        "git",
        "worktree",
        "add",
        "--detach",
        ".harness/worktrees/candidate",
        "HEAD",
    ]
    assert [step["name"] for step in remove["plan"]["steps"]] == ["remove_worktree"]
    assert [step["name"] for step in reset["plan"]["steps"]] == ["fetch_default_branch", "reset_worktree"]
    assert not (tmp_path / ".harness" / "worktrees" / "candidate").exists()

    outside_name = f"outside-{tmp_path.name}"
    outside = _route_post(
        "/worktrees/create",
        body={"path": f"../{outside_name}", "branch": "HEAD"},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )

    assert outside["plan"]["valid_target"] is False
    assert outside["plan"]["managed_path"] is None
    assert outside["plan"]["steps"] == []
    assert outside["plan"]["blocked_reasons"] == [
        "target_must_be_managed_worktree_name",
        "worktree_mutation_disabled",
    ]
    assert outside["git_mutation_started"] is False
    assert outside["filesystem_modified"] is False
    assert outside["process_started"] is False
    assert not (tmp_path.parent / outside_name).exists()


def test_local_server_lists_session_diff_artifact_previews_without_mutation(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    session = store.create_session(title="Diff session")
    run = store.create_run("Diff run", "codex_isolated_edit", session_id=session.id)
    store.append_store_event(
        EventStreamType.SESSION,
        "pty:pty_dev",
        "pty.created",
        {"shell": "/bin/zsh", "title": "Dev shell"},
    )
    store.append_store_event(
        EventStreamType.SESSION,
        "pty:pty_dev",
        "pty.output",
        {"preview": "dev output", "preview_bytes": 10},
        artifact_refs=["art_pty_output"],
    )
    diff_path = store.runs_dir / run.id / "isolated_unified_diff.patch"
    diff_path.parent.mkdir(parents=True, exist_ok=True)
    diff_path.write_text("--- a/app.py\n+++ b/app.py\n@@\n-old\n+new\n", encoding="utf-8")
    artifact = store.register_artifact(run.id, "isolated_unified_diff", diff_path, session_id=session.id)
    cfg = load_config(tmp_path)

    diffs = _route_get(f"/sessions/{session.id}/diffs", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    diff_alias = _route_get(f"/sessions/{session.id}/diff", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    changed = _route_get(f"/sessions/{session.id}/changed-files", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)

    assert diffs["schema_version"] == "harness.session_diffs/v1"
    assert diff_alias["schema_version"] == "harness.session_diffs/v1"
    assert diff_alias["diffs"][0]["id"] == artifact.id
    assert diff_alias["mutation_started"] is False
    assert diffs["session_id"] == session.id
    assert diffs["revert_supported"] is False
    assert diffs["unrevert_supported"] is False
    assert diffs["selected_hunk_apply_supported"] is False
    assert diffs["mutation_started"] is False
    assert diffs["permission_granting"] is False
    assert len(diffs["diffs"]) == 1
    assert diffs["diffs"][0]["id"] == artifact.id
    assert diffs["diffs"][0]["kind"] == "isolated_unified_diff"
    assert "+new" in diffs["diffs"][0]["preview"]
    assert diffs["diffs"][0]["revert_supported"] is False
    assert diffs["diffs"][0]["selected_hunk_apply_supported"] is False
    assert changed["schema_version"] == "harness.session_changed_files/v1"
    assert changed["session_id"] == session.id
    assert changed["file_count"] == 1
    assert changed["files"][0]["path"] == "app.py"
    assert changed["files"][0]["sources"] == ["diff_artifact"]
    assert changed["files"][0]["diff_artifact_ids"] == [artifact.id]
    assert changed["files"][0]["contents_included"] is False
    assert changed["contents_included"] is False
    assert changed["mutation_started"] is False
    assert changed["revert_supported"] is False
    assert changed["selected_hunk_apply_supported"] is False
    assert changed["permission_granting"] is False


def test_local_server_session_snapshots_link_messages_runs_diffs_without_revert(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    session = store.create_session(title="Snapshot session")
    run = store.create_run("Diff run", "codex_isolated_edit", session_id=session.id)
    message = store.append_session_message(session.id, "assistant", "Changed app.py", run_id=run.id)
    diff_path = store.runs_dir / run.id / "isolated_unified_diff.patch"
    diff_path.parent.mkdir(parents=True, exist_ok=True)
    diff_path.write_text("diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@\n-old\n+new\n", encoding="utf-8")
    artifact = store.register_artifact(run.id, "isolated_unified_diff", diff_path, session_id=session.id)
    explicit = store.append_session_snapshot_ref(
        session.id,
        message.id,
        "snap_explicit",
        snapshot_kind="isolated_diff_artifact",
        artifact_id=artifact.id,
        run_id=run.id,
        reversible=False,
        metadata={"note": "metadata only"},
    )
    cfg = load_config(tmp_path)

    snapshots = _route_get(
        f"/sessions/{session.id}/snapshots",
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    message_snapshots = _route_get(
        f"/sessions/{session.id}/messages/{message.id}/snapshots",
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )

    assert snapshots["schema_version"] == "harness.session_snapshots/v1"
    assert snapshots["session_id"] == session.id
    assert snapshots["snapshot_count"] == 1
    assert snapshots["explicit_snapshot_count"] == 1
    assert snapshots["derived_snapshot_count"] == 0
    snapshot = snapshots["snapshots"][0]
    assert snapshot["snapshot_id"] == "snap_explicit"
    assert snapshot["source"] == "session_part"
    assert snapshot["message_id"] == message.id
    assert snapshot["run_ids"] == [run.id]
    assert artifact.id in snapshot["artifact_ids"]
    assert snapshot["diff_artifacts"][0]["id"] == artifact.id
    assert snapshot["diff_artifacts"][0]["contents_included"] is False
    assert snapshot["diff_artifacts"][0]["sha256"] == artifact.sha256
    assert snapshot["diff_artifacts"][0]["size_bytes"] == artifact.size_bytes
    assert snapshot["diff_artifacts"][0]["content_type"] == "text/x-patch"
    assert snapshot["diff_artifacts"][0]["redaction_state"] == "not_required"
    assert snapshot["changed_paths"] == ["app.py"]
    assert snapshot["part_id"] == explicit.id
    assert snapshot["reversible"] is False
    assert snapshot["mutation_reversibility"] == "not_reversible_metadata_only"
    assert snapshot["evidence_contract"]["contents_included"] is False
    assert snapshot["evidence_contract"]["artifact_files_included"] is False
    assert snapshot["evidence_contract"]["requires_sha256"] is True
    assert snapshots["mutation_reversibility"] == "not_reversible_metadata_only"
    assert snapshots["policy_boundary"]["kind"] == "snapshot_metadata_projection"
    assert snapshots["policy_boundary"]["active_workspace_mutation_allowed"] is False
    assert snapshot["revert_supported"] is False
    assert snapshot["unrevert_supported"] is False
    assert snapshot["selected_hunk_apply_supported"] is False
    assert snapshot["mutation_started"] is False
    assert snapshot["filesystem_modified"] is False
    assert snapshot["git_mutation_started"] is False
    assert snapshot["permission_granting"] is False
    assert message_snapshots["snapshots"] == snapshots["snapshots"]


def test_local_server_session_snapshots_derive_message_effects_from_linked_run(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    session = store.create_session(title="Derived snapshot session")
    run = store.create_run("Diff run", "codex_isolated_edit", session_id=session.id)
    message = store.append_session_message(session.id, "assistant", "Changed app.py", run_id=run.id)
    diff_path = store.runs_dir / run.id / "isolated_unified_diff.patch"
    diff_path.parent.mkdir(parents=True, exist_ok=True)
    diff_path.write_text("--- a/app.py\n+++ b/app.py\n@@\n-old\n+new\n", encoding="utf-8")
    artifact = store.register_artifact(run.id, "isolated_unified_diff", diff_path, session_id=session.id)
    cfg = load_config(tmp_path)

    snapshots = _route_get(
        f"/sessions/{session.id}/snapshots",
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )

    assert snapshots["snapshot_count"] == 1
    assert snapshots["derived_snapshot_count"] == 1
    assert snapshots["explicit_snapshot_count"] == 0
    snapshot = snapshots["snapshots"][0]
    assert snapshot["snapshot_id"].startswith("snap_")
    assert snapshot["snapshot_kind"] == "message_effects_metadata"
    assert snapshot["source"] == "derived_from_message_run_artifacts"
    assert snapshot["message_id"] == message.id
    assert snapshot["run_ids"] == [run.id]
    assert artifact.id in snapshot["artifact_ids"]
    assert snapshot["changed_paths"] == ["app.py"]
    assert snapshot["reversible"] is False
    assert snapshot["mutation_reversibility"] == "not_reversible_metadata_only"
    assert snapshot["evidence_contract"]["requires_redaction_state"] is True
    assert snapshot["revert_supported"] is False
    assert snapshot["mutation_started"] is False
    assert snapshot["filesystem_modified"] is False


def test_local_server_session_revert_readiness_explains_blockers_without_mutation(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    session = store.create_session(title="Revert readiness session")
    run = store.create_run("Diff run", "codex_isolated_edit", session_id=session.id)
    message = store.append_session_message(session.id, "assistant", "Changed app.py", run_id=run.id)
    diff_path = store.runs_dir / run.id / "isolated_unified_diff.patch"
    diff_path.parent.mkdir(parents=True, exist_ok=True)
    diff_path.write_text("--- a/app.py\n+++ b/app.py\n@@\n-old\n+new\n", encoding="utf-8")
    artifact = store.register_artifact(run.id, "isolated_unified_diff", diff_path, session_id=session.id)
    cfg = load_config(tmp_path)

    readiness = _route_get(
        f"/sessions/{session.id}/revert-readiness",
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    message_readiness = _route_get(
        f"/sessions/{session.id}/messages/{message.id}/revert-readiness",
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )

    assert readiness["schema_version"] == "harness.session_revert_readiness/v1"
    assert readiness["ready"] is False
    assert readiness["mutation_reversibility"] == "not_reversible_readiness_only"
    assert readiness["policy_boundary"]["kind"] == "session_revert_readiness"
    assert readiness["policy_boundary"]["active_workspace_mutation_allowed"] is False
    assert readiness["policy_boundary"]["requires_apply_back_boundary"] is True
    assert readiness["revert_supported"] is False
    assert readiness["unrevert_supported"] is False
    assert readiness["selected_hunk_apply_supported"] is False
    assert readiness["snapshot_count"] == 1
    assert readiness["diff_artifact_ids"] == [artifact.id]
    assert readiness["changed_paths"] == ["app.py"]
    assert readiness["changed_file_count"] == 1
    assert readiness["active_conflict_count"] == 0
    assert readiness["mutation_started"] is False
    assert readiness["filesystem_modified"] is False
    assert readiness["git_mutation_started"] is False
    assert readiness["permission_granting"] is False
    assert {blocker["code"] for blocker in readiness["blockers"]} >= {
        "active_revert_policy_missing",
        "apply_back_boundary_missing",
        "snapshot_restore_not_implemented",
    }
    assert readiness["blocked_reasons"][:3] == [
        "active_revert_policy_missing",
        "apply_back_boundary_missing",
        "snapshot_restore_not_implemented",
    ]
    assert "explicit approval decision for revert/unrevert or selected-hunk apply" in readiness["required_evidence"]
    assert [step["executed"] for step in readiness["execution_plan"]] == [False] * len(readiness["execution_plan"])
    assert message_readiness["message_id"] == message.id
    assert message_readiness["diff_artifact_ids"] == readiness["diff_artifact_ids"]


def test_local_server_session_revert_actions_fail_closed_without_mutation(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    session = store.create_session(title="Mutation session")
    cfg = load_config(tmp_path)

    revert = _route_post(
        f"/sessions/{session.id}/revert",
        body={"message_id": "msg_123"},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    unrevert = _route_post(
        f"/sessions/{session.id}/unrevert",
        body={"artifact_id": "art_123"},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    apply_hunk = _route_post(
        f"/sessions/{session.id}/apply-hunk",
        body={"artifact_id": "art_123", "hunk_id": "hunk_1"},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )

    for payload, action in [(revert, "revert"), (unrevert, "unrevert"), (apply_hunk, "apply-hunk")]:
        assert payload["schema_version"] == "harness.session_mutation_action/v1"
        assert payload["ok"] is False
        assert payload["action"] == action
        assert payload["mutation_started"] is False
        assert payload["git_mutation_started"] is False
        assert payload["filesystem_modified"] is False
        assert payload["permission_granting"] is False
    assert apply_hunk["hunk_id"] == "hunk_1"


def test_local_server_dev_loop_status_summarizes_pty_worktree_and_session_without_mutation(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "app.py").write_text("print('one')\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=tmp_path, check=True, capture_output=True)
    store = SQLiteStore(tmp_path)
    session = store.create_session(title="Dev loop")
    run = store.create_run("Diff run", "codex_isolated_edit", session_id=session.id)
    store.append_store_event(
        EventStreamType.SESSION,
        "pty:pty_dev",
        "pty.created",
        {"shell": "/bin/zsh", "title": "Dev shell"},
    )
    store.append_store_event(
        EventStreamType.SESSION,
        "pty:pty_dev",
        "pty.output",
        {"preview": "dev output", "preview_bytes": 10},
        artifact_refs=["art_pty_output"],
    )
    diff_path = store.runs_dir / run.id / "isolated_unified_diff.patch"
    diff_path.parent.mkdir(parents=True, exist_ok=True)
    diff_path.write_text("--- a/app.py\n+++ b/app.py\n@@\n-one\n+two\n", encoding="utf-8")
    store.register_artifact(run.id, "isolated_unified_diff", diff_path, session_id=session.id)
    cfg = load_config(tmp_path)

    status = _route_get(
        "/dev-loop/status",
        query={"session_id": [session.id]},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )

    assert status["schema_version"] == "harness.dev_loop_status/v1"
    assert status["policy_boundary"]["kind"] == "dev_loop_status_projection"
    assert status["policy_boundary"]["terminal_process_allowed"] is False
    assert status["policy_boundary"]["worktree_creation_allowed"] is False
    assert status["policy_boundary"]["active_workspace_revert_allowed"] is False
    assert status["policy_boundary"]["selected_hunk_apply_allowed"] is False
    assert status["policy_boundary"]["git_mutation_allowed"] is False
    assert "worktree_mutation_disabled" in status["blocked_reasons"]
    assert "active_workspace_revert_disabled" in status["blocked_reasons"]
    assert status["pty"]["managed_pty_supported"] is False
    assert status["pty"]["process_started"] is False
    assert status["terminal_tabs"]["tab_count"] == 1
    assert status["terminal_tabs"]["output_event_count"] == 1
    assert status["terminal_tabs"]["artifact_ref_count"] == 1
    assert status["terminal_tabs"]["terminal_tabs_supported"] is False
    assert status["terminal_tabs"]["policy_boundary"]["kind"] == "pty_terminal_tabs_projection"
    assert status["terminal_tabs"]["policy_boundary"]["source"] == "persisted_pty_events"
    assert status["terminal_tabs"]["policy_boundary"]["terminal_control_allowed"] is False
    assert status["terminal_tabs"]["policy_boundary"]["requires_append_only_events"] is True
    assert "terminal_tab_projection_disabled" in status["terminal_tabs"]["blocked_reasons"]
    assert status["terminal_tabs"]["source"] == "persisted_pty_events"
    assert status["terminal_tabs"]["terminal_control_supported"] is False
    assert status["terminal_tabs"]["websocket_supported"] is False
    assert status["terminal_tabs"]["process_started"] is False
    assert status["terminal_tabs"]["websocket_opened"] is False
    assert status["terminal_tabs"]["live_stream_read"] is False
    assert status["terminal_tabs"]["artifact_contents_included"] is False
    assert status["terminal_tabs"]["permission_granting"] is False
    assert status["worktrees"]["available"] is True
    assert status["worktrees"]["worktree_count"] == 1
    assert status["worktrees"]["mutation_supported"] is False
    assert status["worktrees"]["creation_supported"] is False
    assert status["worktrees"]["reset_supported"] is False
    assert status["worktrees"]["remove_supported"] is False
    assert status["worktrees"]["blocked_reasons"] == ["worktree_mutation_disabled", "worktree_creation_disabled"]
    assert status["worktrees"]["policy_boundary"]["kind"] == "worktree_status_projection"
    assert status["worktrees"]["policy_boundary"]["worktree_creation_allowed"] is False
    assert status["worktrees"]["policy_boundary"]["git_mutation_allowed"] is False
    assert status["worktrees"]["filesystem_modified"] is False
    assert status["worktrees"]["git_mutation_started"] is False
    assert status["session"]["session_id"] == session.id
    assert status["session"]["diff_artifact_count"] == 1
    assert status["session"]["changed_file_count"] >= 1
    assert status["session"]["local_snapshot_available"] is True
    assert status["session"]["revert_supported"] is False
    assert status["session"]["revert_readiness_ready"] is False
    assert "active_revert_policy_missing" in status["session"]["revert_blocked_reasons"]
    assert status["session"]["revert_policy_boundary"]["kind"] == "session_revert_readiness"
    assert status["session"]["snapshot_policy_boundary"]["kind"] == "snapshot_metadata_projection"
    assert status["session"]["filesystem_modified"] is False
    assert status["session"]["git_mutation_started"] is False
    assert status["policy"]["terminal_process_started"] is False
    assert status["policy"]["terminal_websocket_opened"] is False
    assert status["policy"]["terminal_live_stream_read"] is False
    assert status["policy"]["terminal_artifact_contents_included"] is False
    assert status["policy"]["terminal_control_started"] is False
    assert status["policy"]["workspace_mutation_started"] is False
    assert "worktree_mutation_disabled" in status["policy"]["blocked_reasons"]
    assert status["policy"]["filesystem_modified"] is False
    assert status["policy"]["git_mutation_started"] is False
    assert status["permission_granting"] is False


def test_local_server_pty_projection_and_actions_fail_closed(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    cfg = load_config(tmp_path)

    sessions = _route_get("/pty/sessions", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    opencode_sessions = _route_get("/pty", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    shells = _route_get("/pty/shells", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    restoration = _route_get("/pty/restoration", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    tabs = _route_get("/pty/tabs", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    detail = _route_get("/pty/pty_123", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    connect = _route_get("/pty/pty_123/connect", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    created = _route_post(
        "/pty",
        body={"command": "bash"},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    written = _route_post(
        "/pty/sessions/pty_123/write",
        body={"data": "echo hello\n"},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    updated = _route_post(
        "/pty/pty_123",
        body={"height": 40, "width": 120},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    token = _route_post(
        "/pty/pty_123/connect-token",
        body={},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    removed = _route_delete("/pty/pty_123", store=store)

    assert sessions["schema_version"] == "harness.pty_sessions/v1"
    assert opencode_sessions["schema_version"] == "harness.pty_sessions/v1"
    assert sessions["managed_pty_supported"] is False
    assert sessions["approval_required"] is True
    assert sessions["required_approval"] == "managed_pty_control"
    assert sessions["policy_boundary"]["kind"] == "shell_pty_deferred"
    assert sessions["policy_boundary"]["shell_execution_allowed"] is False
    assert sessions["policy_boundary"]["managed_pty_allowed"] is False
    assert sessions["policy_boundary"]["model_auto_run_allowed"] is False
    assert sessions["policy_boundary"]["process_start_allowed"] is False
    assert sessions["policy_boundary"]["websocket_allowed"] is False
    assert sessions["blocked_reasons"] == ["shell_execution_disabled", "managed_pty_disabled", "model_auto_run_disabled"]
    assert sessions["sessions"] == []
    assert sessions["process_started"] is False
    assert sessions["websocket_opened"] is False
    assert sessions["filesystem_modified"] is False
    assert detail["schema_version"] == "harness.pty_session/v1"
    assert detail["pty_id"] == "pty_123"
    assert detail["found"] is False
    assert detail["policy_boundary"]["kind"] == "shell_pty_deferred"
    assert detail["policy_boundary"]["terminal_control_allowed"] is False
    assert detail["blocked_reasons"] == ["shell_execution_disabled", "managed_pty_disabled", "model_auto_run_disabled"]
    assert detail["process_started"] is False
    assert detail["websocket_opened"] is False
    assert shells["schema_version"] == "harness.pty_shells/v1"
    assert shells["probed"] is False
    assert shells["approval_required"] is True
    assert shells["policy_boundary"]["kind"] == "shell_pty_deferred"
    assert shells["policy_boundary"]["shell_probe_allowed"] is False
    assert shells["blocked_reasons"] == ["shell_execution_disabled", "shell_probe_disabled", "managed_pty_disabled", "model_auto_run_disabled"]
    assert all(shell["acceptable"] is False for shell in shells["shells"])
    assert all(shell["blocked_reasons"] == ["shell_execution_disabled", "managed_pty_disabled"] for shell in shells["shells"])
    assert restoration["schema_version"] == "harness.pty_restoration_readiness/v1"
    assert restoration["ready"] is False
    assert restoration["pty_id"] is None
    assert restoration["event_count"] == 0
    assert restoration["terminal_output_restoration_supported"] is False
    assert restoration["process_started"] is False
    assert restoration["live_stream_read"] is False
    assert restoration["permission_granting"] is False
    assert tabs["schema_version"] == "harness.pty_terminal_tabs/v1"
    assert tabs["tabs"] == []
    assert tabs["terminal_tabs_supported"] is False
    assert tabs["process_started"] is False
    assert tabs["live_stream_read"] is False
    assert tabs["permission_granting"] is False
    assert created["schema_version"] == "harness.pty_action/v1"
    assert created["ok"] is False
    assert created["action"] == "create"
    assert created["plan"]["schema_version"] == "harness.pty_plan/v1"
    assert created["plan"]["action"] == "create"
    assert created["plan"]["shell"] == "/bin/zsh"
    assert created["plan"]["command"] == "bash"
    assert created["plan"]["executed"] is False
    assert created["plan"]["execution_supported"] is False
    assert created["plan"]["approval_required"] is True
    assert created["plan"]["required_approval"] == "managed_pty_control"
    assert created["plan"]["policy_boundary"]["kind"] == "managed_pty"
    assert created["plan"]["policy_boundary"]["process_start_allowed"] is False
    assert created["plan"]["policy_boundary"]["input_write_allowed"] is False
    assert created["plan"]["policy_boundary"]["websocket_token_allowed"] is False
    assert created["plan"]["policy_boundary"]["requires_output_persistence"] is True
    assert created["plan"]["blocked_reasons"] == ["managed_pty_disabled", "pty_process_start_disabled"]
    assert created["execution_supported"] is False
    assert created["approval_required"] is True
    assert created["required_approval"] == "managed_pty_control"
    assert created["policy_boundary"]["kind"] == "managed_pty"
    assert [step["name"] for step in created["plan"]["steps"]] == [
        "create_pty_process",
        "persist_terminal_output_stream",
    ]
    assert created["process_started"] is False
    assert created["live_stream_read"] is False
    assert created["filesystem_modified"] is False
    assert created["websocket_token_issued"] is False
    assert created["permission_granting"] is False
    assert updated["action"] == "update"
    assert updated["plan"]["schema_version"] == "harness.pty_plan/v1"
    assert updated["plan"]["cols"] == 120
    assert updated["plan"]["rows"] == 40
    assert updated["blocked_reasons"] == [
        "managed_pty_disabled",
        "pty_process_start_disabled",
        "terminal_resize_disabled",
    ]
    assert updated["terminal_resized"] is False
    assert token["action"] == "connect-token"
    assert token["plan"]["steps"][0]["name"] == "issue_websocket_connect_token"
    assert token["blocked_reasons"] == [
        "managed_pty_disabled",
        "pty_process_start_disabled",
        "pty_websocket_disabled",
    ]
    assert token["websocket_token_issued"] is False
    assert connect["action"] == "connect"
    assert connect["plan"]["steps"][0]["name"] == "open_websocket_stream"
    assert connect["websocket_opened"] is False
    assert connect["process_started"] is False
    assert removed["action"] == "remove"
    assert [step["name"] for step in removed["plan"]["steps"]] == [
        "terminate_pty_process",
        "persist_terminal_close_event",
    ]
    assert removed["terminal_closed"] is False
    assert written["pty_id"] == "pty_123"
    assert written["plan"]["steps"][0]["name"] == "write_terminal_input"
    assert written["plan"]["steps"][0]["input_bytes"] == len("echo hello\n".encode("utf-8"))
    assert written["blocked_reasons"] == [
        "managed_pty_disabled",
        "pty_process_start_disabled",
        "terminal_input_write_disabled",
    ]
    assert written["input_written"] is False


def test_local_server_pty_restoration_readiness_uses_append_only_events_without_live_stream(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    cfg = load_config(tmp_path)
    store.append_store_event(EventStreamType.SESSION, "pty:pty_123", "pty.created", {"shell": "/bin/zsh", "cols": 80, "rows": 24})
    store.append_store_event(
        EventStreamType.SESSION,
        "pty:pty_123",
        "pty.output",
        {"preview": "hello\n", "preview_bytes": 6},
        artifact_refs=["art_pty_output"],
    )
    store.append_store_event(EventStreamType.SESSION, "pty:pty_123", "pty.updated", {"cols": 100, "rows": 30})

    readiness = _route_get(
        "/pty/pty_123/restoration",
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    tabs = _route_get(
        "/pty/pty_123/tab",
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )

    assert readiness["schema_version"] == "harness.pty_restoration_readiness/v1"
    assert readiness["ready"] is False
    assert readiness["pty_id"] == "pty_123"
    assert readiness["event_stream_id"] == "pty:pty_123"
    assert readiness["event_count"] == 3
    assert readiness["output_event_count"] == 1
    assert readiness["artifact_refs"] == ["art_pty_output"]
    assert readiness["output_preview_bytes"] == 6
    assert readiness["missing_events"] == ["pty.exited"]
    assert readiness["policy_boundary"]["kind"] == "pty_restoration_readiness"
    assert readiness["policy_boundary"]["live_stream_allowed"] is False
    assert readiness["policy_boundary"]["artifact_content_read_allowed"] is False
    assert readiness["policy_boundary"]["requires_append_only_events"] is True
    assert readiness["blocked_reasons"] == [
        "managed_pty_not_enabled",
        "terminal_output_restoration_not_enabled",
        "missing_required_pty_events",
    ]
    assert readiness["restoration_plan"][0] == {"step": "load_pty_lifecycle_events", "ready": True, "executed": False}
    assert readiness["restoration_plan"][1] == {"step": "load_output_artifacts", "ready": True, "executed": False}
    assert readiness["restoration_plan"][3] == {"step": "restore_terminal_dimensions", "ready": True, "executed": False}
    assert readiness["process_started"] is False
    assert readiness["live_stream_read"] is False
    assert readiness["artifact_contents_included"] is False
    assert readiness["permission_granting"] is False
    assert tabs["schema_version"] == "harness.pty_terminal_tabs/v1"
    assert tabs["tab_count"] == 1
    assert tabs["policy_boundary"]["kind"] == "pty_terminal_tabs_projection"
    assert tabs["policy_boundary"]["source"] == "persisted_pty_events"
    assert tabs["policy_boundary"]["live_stream_allowed"] is False
    assert tabs["policy_boundary"]["artifact_content_read_allowed"] is False
    assert tabs["policy_boundary"]["terminal_control_allowed"] is False
    assert tabs["policy_boundary"]["requires_append_only_events"] is True
    assert tabs["blocked_reasons"] == [
        "managed_pty_not_enabled",
        "terminal_output_restoration_not_enabled",
        "missing_required_pty_events",
        "terminal_tab_projection_disabled",
        "terminal_control_disabled",
    ]
    assert tabs["source"] == "persisted_pty_events"
    assert tabs["terminal_control_supported"] is False
    assert tabs["websocket_supported"] is False
    assert tabs["websocket_opened"] is False
    tab = tabs["tabs"][0]
    assert tab["id"] == "pty_123"
    assert tab["title"] == "/bin/zsh"
    assert tab["status"] == "unavailable"
    assert tab["cols"] == 100
    assert tab["rows"] == 30
    assert tab["scrollback_preview"] == "hello\n"
    assert tab["artifact_refs"] == ["art_pty_output"]
    assert tab["restoration_ready"] is False
    assert tab["policy_boundary"]["kind"] == "pty_terminal_tab_projection"
    assert tab["policy_boundary"]["source"] == "persisted_pty_events"
    assert tab["policy_boundary"]["terminal_control_allowed"] is False
    assert tab["policy_boundary"]["requires_append_only_events"] is True
    assert tab["policy_boundary"]["bounded_preview_only"] is True
    assert tab["blocked_reasons"] == [
        *readiness["blocked_reasons"],
        "terminal_tab_projection_disabled",
        "terminal_control_disabled",
    ]
    assert tab["source"] == "persisted_pty_events"
    assert tab["process_started"] is False
    assert tab["websocket_opened"] is False
    assert tab["live_stream_read"] is False
    assert tab["artifact_contents_included"] is False
    assert tab["permission_granting"] is False


def test_local_server_pr_helpers_fail_closed_without_network_or_git_mutation(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    cfg = load_config(tmp_path)

    checkout = _route_post(
        "/pr/checkout",
        body={"pr": "https://github.com/example/repo/pull/42"},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    run = _route_post(
        "/pr/run",
        body={"pr": "42", "adapter": "repo_planning"},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )

    for payload, action in [(checkout, "checkout"), (run, "run")]:
        assert payload["schema_version"] == "harness.pr_action/v1"
        assert payload["ok"] is False
        assert payload["action"] == action
        assert payload["plan"]["schema_version"] == "harness.pr_checkout_plan/v1"
        assert payload["plan"]["executed"] is False
        assert payload["plan"]["execution_supported"] is False
        assert payload["plan"]["approval_required"] is True
        assert payload["plan"]["required_approval"] == "pr_checkout_or_run"
        assert payload["plan"]["policy_boundary"]["kind"] == "pull_request_worktree"
        assert payload["plan"]["policy_boundary"]["managed_root"] == ".harness/pr-worktrees"
        assert payload["plan"]["policy_boundary"]["active_workspace_mutation_allowed"] is False
        assert payload["plan"]["policy_boundary"]["network_fetch_allowed"] is False
        assert payload["plan"]["policy_boundary"]["git_mutation_allowed"] is False
        assert payload["plan"]["policy_boundary"]["worktree_creation_allowed"] is False
        assert payload["execution_supported"] is False
        assert payload["approval_required"] is True
        assert payload["required_approval"] == "pr_checkout_or_run"
        assert payload["policy_boundary"]["kind"] == "pull_request_worktree"
        assert payload["network_called"] is False
        assert payload["git_mutation_started"] is False
        assert payload["worktree_created"] is False
        assert payload["checkout_started"] is False
        assert payload["adapter_started"] is False
        assert payload["process_started"] is False
        assert payload["filesystem_modified"] is False
        assert payload["permission_granting"] is False
    assert checkout["pr"] == "https://github.com/example/repo/pull/42"
    assert checkout["parsed"] == {
        "kind": "github_url",
        "owner": "example",
        "repo": "repo",
        "number": 42,
        "url": "https://github.com/example/repo/pull/42",
        "valid": True,
    }
    assert checkout["plan"]["branch"] == "harness/pr-42"
    assert checkout["plan"]["worktree_path"] == ".harness/pr-worktrees/pr-42"
    assert checkout["plan"]["fetch_ref"] == "+refs/pull/42/head:refs/remotes/origin/pr/42"
    assert [step["name"] for step in checkout["plan"]["steps"]] == ["fetch_pr_head", "create_isolated_worktree"]
    assert checkout["plan"]["blocked_reasons"] == [
        "network_fetch_disabled",
        "git_mutation_disabled",
        "worktree_creation_disabled",
    ]
    assert all(step["executed"] is False for step in checkout["plan"]["steps"])
    assert run["parsed"]["kind"] == "number"
    assert run["parsed"]["number"] == 42
    assert run["plan"]["requires_repo_resolution"] is True
    assert run["plan"]["blocked_reasons"] == [
        "network_fetch_disabled",
        "git_mutation_disabled",
        "worktree_creation_disabled",
        "adapter_execution_disabled",
        "repo_resolution_required",
    ]
    assert run["adapter"] == "repo_planning"
    assert run["plan"]["adapter"] == "repo_planning"
    assert [step["name"] for step in run["plan"]["steps"]] == ["fetch_pr_head", "create_isolated_worktree", "run_adapter"]

    invalid = _route_post(
        "/pr/checkout",
        body={"pr": "not a pr"},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    assert invalid["parsed"]["valid"] is False
    assert invalid["plan"]["valid_pr_ref"] is False
    assert invalid["plan"]["steps"] == []
    assert invalid["plan"]["blocked_reasons"][:4] == [
        "invalid_pr_ref",
        "network_fetch_disabled",
        "git_mutation_disabled",
        "worktree_creation_disabled",
    ]
    assert invalid["network_called"] is False
    assert invalid["git_mutation_started"] is False
    assert invalid["worktree_created"] is False
    assert invalid["adapter_started"] is False
    assert invalid["process_started"] is False
    assert invalid["filesystem_modified"] is False


def test_local_server_distribution_and_version_projections_are_offline(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    cfg = load_config(tmp_path)

    distribution = _route_get("/distribution/status", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    version_check = _route_get("/version/check", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)

    assert distribution["schema_version"] == "harness.distribution_status/v1"
    assert distribution["ok"] is True
    assert distribution["server_supported"] is True
    assert distribution["session_cli_supported"] is True
    assert distribution["network_called"] is False
    assert distribution["filesystem_modified"] is False
    assert distribution["subprocess_started"] is False
    assert distribution["permission_granting"] is False
    assert version_check["schema_version"] == "harness.version_check/v1"
    assert version_check["ok"] is True
    assert version_check["latest_version"] is None
    assert version_check["update_available"] is None
    assert version_check["notification_enabled"] is False
    assert version_check["network_called"] is False
    assert version_check["subprocess_started"] is False
    assert version_check["permission_granting"] is False


def test_local_server_packaging_smoke_plan_and_run_are_safe_contracts(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")
    store = SQLiteStore(tmp_path)
    cfg = load_config(tmp_path)

    smoke = _route_get("/distribution/packaging-smoke", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    run = _route_post(
        "/distribution/packaging-smoke/run",
        body={"mode": "wheel"},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )

    assert smoke["schema_version"] == "harness.packaging_smoke/v1"
    assert smoke["ok"] is True
    assert smoke["packaging_path"] == "python_wheel_first"
    assert smoke["pyproject_exists"] is True
    assert smoke["wheel_smoke_supported"] is True
    assert smoke["sdist_smoke_supported"] is False
    assert smoke["standalone_binary_smoke_supported"] is False
    assert smoke["desktop_package_smoke_supported"] is False
    assert smoke["execution_supported"] is False
    assert "harness serve --openapi --output json" in smoke["commands"]
    assert "cli_entrypoint" in smoke["covers"]
    assert "local_server_openapi" in smoke["covers"]
    assert smoke["artifact_output_supported"] is False
    assert smoke["network_called"] is False
    assert smoke["filesystem_modified"] is False
    assert smoke["subprocess_started"] is False
    assert smoke["permission_granting"] is False
    assert run["schema_version"] == "harness.packaging_smoke_action/v1"
    assert run["ok"] is False
    assert run["build_started"] is False
    assert run["install_started"] is False
    assert run["subprocess_started"] is False
    assert run["filesystem_modified"] is False
    assert run["network_called"] is False
    assert run["permission_granting"] is False


def test_local_server_objective_evidence_and_trace_projections_are_read_only(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    objective = store.create_objective("Server objective evidence")
    store.create_task(
        title="Server dry run",
        objective_id=objective.id,
        metadata={"execution_adapter": "dry_run", "task_type": "phase_1a_test"},
    )
    run_result = run_objective_autonomously(tmp_path, objective.id, autonomy_profile_id="safe-local")
    cfg = load_config(tmp_path)

    evidence = _route_get(
        f"/objectives/{objective.id}/evidence",
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    trace = _route_get(
        f"/objectives/{objective.id}/trace",
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    missing_trace = _route_get(
        "/objectives/objective_missing/trace",
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )

    assert run_result.ok is True
    assert evidence["schema_version"] == "harness.objective_evidence_verification/v1"
    assert evidence["ok"] is True
    checks = {check["id"]: check for check in evidence["checks"]}
    assert checks["event_identity"]["status"] == "pass"
    assert checks["event_hash_chain"]["status"] == "pass"
    assert checks["event_timestamps"]["status"] == "pass"
    assert checks["event_hash_chain"]["evidence"]["head_sha256"]
    assert evidence["execution_started"] is False
    assert evidence["adapter_started"] is False
    assert evidence["provider_execution_started"] is False
    assert evidence["filesystem_modified"] is False
    assert evidence["network_called"] is False
    assert evidence["artifact_contents_included"] is False
    assert evidence["permission_granting"] is False

    assert trace["schema_version"] == "harness.trace_export/v1"
    assert trace["ok"] is True
    assert trace["objective_id"] == objective.id
    assert trace["objective_run_ids"]
    root_span = next(
        span
        for resource in trace["resourceSpans"]
        for scope in resource["scopeSpans"]
        for span in scope["spans"]
        if span["name"] == "harness.objective"
    )
    root_attributes = {attribute["key"]: attribute["value"] for attribute in root_span["attributes"]}
    assert root_attributes["objective.evidence_verification_ok"] is True
    assert root_attributes["objective.evidence_hash_chain_ok"] is True
    assert root_attributes["objective.evidence_head_sha256"]
    assert trace["execution_started"] is False
    assert trace["adapter_started"] is False
    assert trace["provider_execution_started"] is False
    assert trace["filesystem_modified"] is False
    assert trace["network_called"] is False
    assert trace["artifact_contents_included"] is False
    assert trace["permission_granting"] is False

    assert missing_trace["schema_version"] == "harness.trace_export/v1"
    assert missing_trace["ok"] is False
    assert missing_trace["objective_id"] == "objective_missing"
    assert missing_trace["execution_started"] is False
    assert missing_trace["permission_granting"] is False
    assert "Objective not found" in missing_trace["errors"][0]


def test_local_server_run_trace_projection_is_read_only(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    run = store.create_run(goal="Server trace", task_type="phase_1a_test")
    store.append_event(
        run.id,
        "info",
        "server_trace_event",
        "Server trace event.",
        {"api_key": "sk-server-trace-secret-abcdefghijklmnop", "value": 1},
    )
    artifact_path = tmp_path / ".harness" / "runs" / run.id / "server-trace.txt"
    artifact_path.write_text("server trace artifact body", encoding="utf-8")
    store.register_artifact(run.id, "server_trace_artifact", artifact_path)
    cfg = load_config(tmp_path)

    trace = _route_get(
        f"/runs/{run.id}/trace",
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    missing_trace = _route_get(
        "/runs/run_missing/trace",
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )

    serialized = json.dumps(trace)
    assert trace["schema_version"] == "harness.trace_export/v1"
    assert trace["ok"] is True
    assert trace["run_id"] == run.id
    spans = [
        span
        for resource in trace["resourceSpans"]
        for scope in resource["scopeSpans"]
        for span in scope["spans"]
    ]
    span_names = {span["name"] for span in spans}
    assert "harness.run" in span_names
    assert "harness.event.server_trace_event" in span_names
    assert "harness.artifact.server_trace_artifact" in span_names
    run_span = next(span for span in spans if span["name"] == "harness.run")
    run_attributes = {attribute["key"]: attribute["value"] for attribute in run_span["attributes"]}
    event_span = next(span for span in spans if span["name"] == "harness.event.server_trace_event")
    event_attributes = {attribute["key"]: attribute["value"] for attribute in event_span["attributes"]}
    assert run_attributes["trace.producer"] == "harness.trace_export"
    assert event_attributes["event.payload"]["value"] == 1
    assert event_attributes["event.payload"]["[REDACTED_KEY]"] == "[REDACTED_SECRET]"
    assert len(event_attributes["event.payload_sha256"]) == 64
    assert event_attributes["event.payload_size_bytes"] > 0
    assert event_attributes["event.payload_keys"] == ["[REDACTED_KEY]", "value"]
    assert trace["execution_started"] is False
    assert trace["adapter_started"] is False
    assert trace["provider_execution_started"] is False
    assert trace["filesystem_modified"] is False
    assert trace["network_called"] is False
    assert trace["artifact_contents_included"] is False
    assert trace["permission_granting"] is False
    assert "server trace artifact body" not in serialized
    assert "api_key" not in serialized
    assert "sk-server-trace-secret" not in serialized

    assert missing_trace["schema_version"] == "harness.trace_export/v1"
    assert missing_trace["ok"] is False
    assert missing_trace["run_id"] == "run_missing"
    assert missing_trace["execution_started"] is False
    assert missing_trace["permission_granting"] is False
    assert "Run not found" in missing_trace["errors"][0]


def test_local_server_desktop_status_and_launch_are_safe_contracts(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    cfg = load_config(tmp_path)

    desktop = _route_get("/desktop/status", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    launch = _route_post(
        "/desktop/launch",
        body={"source": "test"},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )

    assert desktop["schema_version"] == "harness.desktop_status/v1"
    assert desktop["packaging_decision"] == "python_wheel_first"
    assert desktop["desktop_wrapper_supported"] is False
    assert desktop["desktop_app_installed"] is False
    assert desktop["launch_supported"] is False
    assert desktop["auto_update_supported"] is False
    assert desktop["requires_local_server"] is True
    assert desktop["network_called"] is False
    assert desktop["process_started"] is False
    assert desktop["filesystem_modified"] is False
    assert desktop["permission_granting"] is False
    assert launch["schema_version"] == "harness.desktop_action/v1"
    assert launch["ok"] is False
    assert launch["action"] == "launch"
    assert launch["desktop_app_launched"] is False
    assert launch["process_started"] is False
    assert launch["network_called"] is False
    assert launch["filesystem_modified"] is False
    assert launch["permission_granting"] is False


def test_local_server_tui_settings_projection_is_metadata_only(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    cfg = load_config(tmp_path)

    settings = _route_get("/settings/tui", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)

    assert settings["schema_version"] == "harness.tui_settings/v1"
    assert settings["ok"] is True
    assert settings["preferences"]["theme"] == "light"
    assert {theme["id"] for theme in settings["themes"]} == {"light", "dark", "system"}
    assert any(binding["action"] == "toggle_palette_focus" for binding in settings["keybindings"])
    assert settings["filesystem_modified"] is False
    assert settings["process_started"] is False
    assert settings["permission_granting"] is False


def test_local_server_tui_control_routes_are_safe_intents(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    cfg = load_config(tmp_path)

    next_request = _route_get("/tui/control/next", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    appended = _route_post(
        "/tui/append-prompt",
        body={"text": "Inspect this session"},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    selected = _route_post(
        "/tui/select-session",
        body={"sessionID": "session_demo"},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    response = _route_post(
        "/tui/control/response",
        body={"body": {"ok": True}},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )

    assert next_request["schema_version"] == "harness.tui_control_next/v1"
    assert next_request["queue_empty"] is True
    assert next_request["blocking"] is False
    assert next_request["control_queue_enabled"] is False
    for payload, action in [(appended, "append-prompt"), (selected, "select-session"), (response, "control.response")]:
        assert payload["schema_version"] == "harness.tui_control_action/v1"
        assert payload["action"] == action
        assert payload["queued"] is False
        assert payload["live_tui_controlled"] is False
        assert payload["process_started"] is False
        assert payload["filesystem_modified"] is False
        assert payload["permission_granting"] is False


def test_local_server_project_commands_are_discovered_but_not_executed(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    command_dir = tmp_path / ".opencode" / "command"
    command_dir.mkdir(parents=True)
    (command_dir / "review.md").write_text(
        "---\n"
        "title: Review changes\n"
        "description: Review the current branch\n"
        "variables:\n"
        "  - target\n"
        "---\n"
        "Review {{target}} without changing files.\n",
        encoding="utf-8",
    )
    store = SQLiteStore(tmp_path)
    cfg = load_config(tmp_path)

    commands = _route_get("/commands", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    command_alias = _route_get("/command", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    run = _route_post(
        "/commands/run",
        body={"name": "review", "command_id": "project:review"},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    session = store.create_session(title="Command session")
    session_command = _route_post(
        f"/sessions/{session.id}/command",
        body={"command": "review", "arguments": {"target": "HEAD"}},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )

    assert commands["schema_version"] == "harness.commands/v1"
    assert command_alias["schema_version"] == "harness.commands/v1"
    assert command_alias["commands"][0]["name"] == "review"
    assert commands["execution_supported"] is False
    assert commands["contents_included"] is False
    assert commands["commands"][0]["name"] == "review"
    assert commands["commands"][0]["slash"] == "/review"
    assert commands["commands"][0]["template_variables"] == ["target"]
    assert commands["commands"][0]["origin"] == "opencode"
    assert run["schema_version"] == "harness.command_action/v1"
    assert run["ok"] is False
    assert run["execution_started"] is False
    assert run["process_started"] is False
    assert run["network_called"] is False
    assert run["filesystem_modified"] is False
    assert run["permission_granting"] is False
    assert session_command["schema_version"] == "harness.session_command_action/v1"
    assert session_command["ok"] is False
    assert session_command["session_id"] == session.id
    assert session_command["execution_started"] is False
    assert session_command["provider_execution_started"] is False
    assert session_command["permission_granting"] is False


def test_local_server_provider_oauth_routes_fail_closed(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    cfg = load_config(tmp_path)

    provider_id = "codex_cli"
    authorized = _route_post(
        f"/provider/{provider_id}/oauth/authorize",
        body={"redirect": "http://127.0.0.1/callback"},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    callback = _route_post(
        f"/provider/{provider_id}/oauth/callback",
        body={"code": "secret-code"},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )

    assert authorized["schema_version"] == "harness.provider_oauth_action/v1"
    assert authorized["ok"] is False
    assert authorized["provider_id"] == provider_id
    assert authorized["action"] == "authorize"
    assert authorized["oauth_supported"] is False
    assert authorized["browser_opened"] is False
    assert authorized["network_called"] is False
    assert authorized["credentials_stored"] is False
    assert authorized["permission_granting"] is False
    assert authorized["no_hidden_fallback"] is True
    assert callback["action"] == "callback"
    assert callback["network_called"] is False
    assert callback["credentials_stored"] is False


def test_provider_auth_methods_projection_lists_supported_methods(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    cfg = load_config(tmp_path)

    projection = _route_get("/provider/auth", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)

    assert projection["schema_version"] == "harness.provider_auth_methods/v1"
    assert projection["credentials_included"] is False
    assert projection["provider_execution_started"] is False
    assert "oauth" in projection["auth_methods"]
    assert "paid_openai_compatible" in projection["oauth_supported_providers"]
    assert projection["oauth_support"]["paid_openai_compatible"] is True
    assert projection["credential_write_support"]["paid_openai_compatible"] is True
    providers = {provider["provider_id"]: provider for provider in projection["providers"]}
    openai_methods = {method["method"]: method for method in providers["paid_openai_compatible"]["methods"]}
    bedrock_methods = {method["method"]: method for method in providers["bedrock"]["methods"]}
    codex_methods = {method["method"]: method for method in providers["codex_cli"]["methods"]}
    assert openai_methods["api_key"]["supported"] is True
    assert openai_methods["api_key"]["secret_value_required"] is True
    assert openai_methods["env"]["supported"] is True
    assert openai_methods["env"]["default_env_var"] == "OPENAI_API_KEY"
    assert openai_methods["oauth"]["supported"] is True
    assert openai_methods["oauth"]["oauth_supported"] is True
    assert bedrock_methods["aws_profile"]["supported"] is True
    assert bedrock_methods["aws_env"]["supported"] is True
    assert codex_methods["codex_login"]["supported"] is True
    assert all(method["credential_value_included"] is False for provider in providers.values() for method in provider["methods"])


def test_server_provider_api_key_connect_redacts_secret(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    cfg = load_config(tmp_path)

    result = _route_post(
        "/provider/paid_openai_compatible/auth/api-key",
        body={"api_key": "sk-test-secret", "description": "server test"},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )

    assert result["schema_version"] == "harness.provider_auth_action/v1"
    assert result["ok"] is True
    assert result["provider_id"] == "paid_openai_compatible"
    assert result["action"] == "api_key"
    assert result["account_created"] is True
    assert result["account_activated"] is True
    assert result["credential_written"] is True
    assert result["credential_value_included"] is False
    assert result["credentials_included"] is False
    assert result["provider_execution_started"] is False
    assert result["model_execution_started"] is False
    assert result["network_accessed"] is False
    assert result["active_model_changed"] is False
    assert "sk-test-secret" not in json.dumps(result, sort_keys=True)
    account = store.active_provider_account("paid_openai_compatible")
    assert account is not None
    assert account["account_id"] == result["account_id"]
    assert "sk-test-secret" not in json.dumps(account, sort_keys=True)
    assert read_provider_account_secret(tmp_path, account) == "sk-test-secret"


def test_oauth_authorize_returns_browser_or_code_method_without_secret_leakage(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    cfg = load_config(tmp_path)

    result = _route_post(
        "/provider/paid_openai_compatible/oauth/authorize",
        body={"scopes": "models.read completions.write", "code_verifier": "secret-verifier"},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )

    assert result["schema_version"] == "harness.provider_oauth_action/v1"
    assert result["ok"] is True
    assert result["provider_id"] == "paid_openai_compatible"
    assert result["oauth_supported"] is True
    assert result["method"] == "manual_code"
    assert result["manual_code_required"] is True
    assert result["browser_opened"] is False
    assert result["network_called"] is False
    assert result["credentials_stored"] is False
    assert result["credential_value_included"] is False
    assert result["credentials_included"] is False
    assert result["pkce"]["code_challenge_method"] == "S256"
    assert result["pkce"]["code_verifier_included"] is False
    assert "secret-verifier" not in json.dumps(result, sort_keys=True)


def test_oauth_callback_stores_redacted_account(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    cfg = load_config(tmp_path)

    result = _route_post(
        "/provider/paid_openai_compatible/oauth/callback",
        body={
            "access_token": "oauth-access-secret",
            "refresh_token": "oauth-refresh-secret",
            "expires_in": 3600,
            "scopes": ["models.read"],
        },
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )

    serialized = json.dumps(result, sort_keys=True)
    assert result["schema_version"] == "harness.provider_auth_action/v1"
    assert result["ok"] is True
    assert result["action"] == "oauth_callback"
    assert result["account_created"] is True
    assert result["credential_source"] == "provider_account_oauth_secret_store"
    assert result["credential_value_included"] is False
    assert result["credentials_included"] is False
    assert result["provider_execution_started"] is False
    assert result["model_execution_started"] is False
    assert "oauth-access-secret" not in serialized
    assert "oauth-refresh-secret" not in serialized
    account = store.active_provider_account("paid_openai_compatible")
    assert account is not None
    assert account["credential_kind"] == "oauth"
    assert account["expires_at"]
    assert read_provider_oauth_tokens(tmp_path, account) == {
        "access_token": "oauth-access-secret",
        "refresh_token": "oauth-refresh-secret",
    }


def test_local_server_control_config_project_and_session_shell_routes_fail_closed(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    cfg = load_config(tmp_path)
    session = store.create_session(title="Control session")
    config_path = tmp_path / ".harness" / "config.yaml"
    config_before = config_path.read_text(encoding="utf-8")

    auth_set = _route_post(
        "/auth/openai",
        body={"type": "api", "key": "sk-test-secret"},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    auth_remove = _route_delete("/auth/openai", store=store)
    log = _route_post(
        "/log",
        body={"service": "tui", "level": "info", "message": "client-side event"},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    config_update = _route_patch(
        "/config",
        body={"theme": "dark", "provider": {"openai": {"key": "sk-test-secret"}}},
        store=store,
    )
    project_update = _route_patch(
        "/project/current",
        body={"name": "mutated", "commands": {"review": "run review"}},
        store=store,
    )
    init = _route_post(
        f"/sessions/{session.id}/init",
        body={"messageID": "msg_123"},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    shell = _route_post(
        f"/sessions/{session.id}/shell",
        body={"command": "echo should-not-run"},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    unshare = _route_delete(f"/sessions/{session.id}/share", store=store)

    assert config_path.read_text(encoding="utf-8") == config_before
    assert auth_set["schema_version"] == "harness.auth_action/v1"
    assert auth_set["ok"] is False
    assert auth_set["provider_id"] == "openai"
    assert auth_set["credentials_stored"] is False
    assert auth_set["credentials_included"] is False
    assert auth_set["network_called"] is False
    assert auth_set["permission_granting"] is False
    assert auth_set["no_hidden_fallback"] is True
    assert auth_remove["action"] == "remove"
    assert auth_remove["credentials_removed"] is False
    assert log["schema_version"] == "harness.client_log_action/v1"
    assert log["log_written"] is False
    assert log["filesystem_modified"] is False
    assert config_update["schema_version"] == "harness.config_update_action/v1"
    assert config_update["config_mutated"] is False
    assert config_update["filesystem_modified"] is False
    assert config_update["credentials_included"] is False
    assert config_update["no_hidden_fallback"] is True
    assert project_update["schema_version"] == "harness.project_action/v1"
    assert project_update["action"] == "update"
    assert project_update["project_id"] == "current"
    assert project_update["filesystem_modified"] is False
    assert project_update["process_started"] is False
    assert init["schema_version"] == "harness.session_init_action/v1"
    assert init["agents_file_written"] is False
    assert init["provider_execution_started"] is False
    assert init["no_hidden_fallback"] is True
    assert shell["schema_version"] == "harness.session_shell_action/v1"
    assert shell["permission_required"] is True
    assert shell["permission_id"]
    assert shell["approval_card"]["tool_id"] == "shell"
    assert shell["approval_card"]["command"] == "echo should-not-run"
    assert shell["approval_card"]["cwd"] == "."
    assert shell["approval_card"]["descriptor_ref"]["tool_id"] == "shell"
    assert shell["approval_card"]["descriptor_ref"]["permission_key"] == "tool.shell.execution"
    assert shell["approval_card"]["policy"]["permission_key"] == "tool.shell.execution"
    assert shell["approval_card"]["policy"]["replay_policy"] == "rerun_forbidden"
    assert shell["result"]["error_type"] == "permission_required"
    assert shell["process_started"] is False
    assert shell["command_executed"] is False
    assert shell["provider_execution_started"] is False
    assert shell["permission_granting"] is False
    assert unshare["schema_version"] == "harness.session_unshare_action/v1"
    assert unshare["share_removed"] is False
    assert unshare["network_called"] is False


def test_local_server_session_tool_route_uses_gateway_and_exposes_cwd(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("alpha\n", encoding="utf-8")
    store = SQLiteStore(tmp_path)
    cfg = load_config(tmp_path)
    session = store.create_session(title="Gateway")

    cd = _route_post(
        f"/sessions/{session.id}/tool",
        body={"tool_id": "cd", "arguments": {"path": "src", "actor": "operator"}},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    read = _route_post(
        f"/sessions/{session.id}/tools/read",
        body={"arguments": {"path": "app.py"}},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    status = _route_get(
        f"/sessions/{session.id}/status",
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
        query={},
    )

    assert cd["schema_version"] == "harness.session_tool_execution_response/v1"
    assert cd["ok"] is True
    assert cd["lifecycle"] == "direct_tool_execution"
    assert cd["save_point_emitted"] is False
    assert cd["cwd"]["cwd"] == "src"
    assert read["ok"] is True
    assert read["lifecycle"] == "direct_tool_execution"
    assert read["save_point_emitted"] is False
    assert read["result"]["preview"] == "alpha\n"
    assert status["cwd"]["cwd"] == "src"
    assert status["operator"]["phase"] == "idle"
    assert status["operator"]["project_root"] == str(tmp_path.resolve())
    assert status["operator"]["cwd"] == "src"
    assert "pwd" in status["operator"]["active_tools"]
    event_kinds = [event.kind for event in store.list_session_store_events(session.id)]
    assert "tool_call.started" in event_kinds
    assert "operator.turn.started" not in event_kinds
    assert "harness.save_point" not in event_kinds


def test_local_server_session_tool_invalid_cwd_returns_recovery_payload(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    cfg = load_config(tmp_path)
    session = store.create_session(title="Invalid cwd", metadata={"cwd": "missing-dir"})

    response = _route_post(
        f"/sessions/{session.id}/tool",
        body={"tool_id": "pwd", "arguments": {}},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )

    assert response["schema_version"] == "harness.session_tool_execution_response/v1"
    assert response["ok"] is False
    assert response["result"]["error_type"] == "invalid_cwd"
    serialized = json.dumps(response)
    assert "harness doctor --repair" in serialized
    assert "Traceback" not in serialized
    assert "no such table" not in serialized.lower()


def test_local_server_session_prompt_uses_chat_operator_loop_event_sequence(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    cfg = load_config(tmp_path)
    chat_session = store.create_session(title="Chat prompt")
    server_session = store.create_session(title="Server prompt")
    shell_session = store.create_session(title="Server prompt shell approval")

    chat_response = handle_chat_input(
        "pwd",
        tmp_path,
        ChatSessionState(session_id=chat_session.id, active_project_root=str(tmp_path)),
    )
    server_response = _route_post(
        f"/sessions/{server_session.id}/prompt",
        body={"prompt": "pwd"},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    debug_response = _route_post(
        f"/sessions/{server_session.id}/prompt",
        body={"prompt": "pwd", "debug": True},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    shell_prompt = _route_post(
        f"/sessions/{shell_session.id}/prompt",
        body={"prompt": "run the session tool tests"},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )

    assert chat_response["kind"] == "session_tool_result"
    assert server_response["schema_version"] == "harness.session_prompt_response/v1"
    assert server_response["kind"] == chat_response["kind"]
    assert server_response["lines"] == chat_response["lines"]
    assert server_response["operator_status"]["phase"] == "idle"
    assert server_response["operator_status"]["cwd"] == "."
    assert server_response["tool_results"] == [{"tool": "pwd", "ok": True, "error_type": None}]
    chat_event_kinds = _operator_prompt_event_kinds(store, chat_session.id)
    assert _operator_prompt_event_kinds(store, server_session.id)[: len(chat_event_kinds)] == chat_event_kinds
    normal_json = json.dumps(server_response)
    assert "harness.tool_result/v1" not in normal_json
    assert "Traceback" not in normal_json
    assert "no such table" not in normal_json.lower()
    assert "debug" not in server_response
    assert debug_response["debug"]["events"]
    assert debug_response["debug"]["chat_response"]["kind"] == "session_tool_result"
    assert shell_prompt["permission_required"] is True
    assert shell_prompt["permission_id"]
    assert shell_prompt["approval_card"]["command"] == "python3 -m pytest tests/test_session_tools.py -q"
    assert shell_prompt["execution_started"] is False
    assert shell_prompt["operator_status"]["phase"] == "waiting_approval"


def test_local_server_session_tools_catalog_exposes_policy_projection(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    cfg = load_config(tmp_path)
    session = store.create_session(title="Tool catalog")

    global_tools = _route_get("/tools", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    session_tools = _route_get(
        f"/sessions/{session.id}/tools",
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    shell = _route_get(
        f"/sessions/{session.id}/tools/shell",
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )

    assert global_tools["schema_version"] == "harness.session_tools/v1"
    assert global_tools["policy_projection_schema_version"] == "harness.session_tool_policy_projection/v1"
    assert session_tools["session_id"] == session.id
    assert session_tools["tools"] == global_tools["tools"]
    by_id = {tool["id"]: tool for tool in global_tools["tools"]}
    assert by_id["read"]["policy"]["enabled"] is True
    assert by_id["read"]["policy"]["maturity"] == ["implemented"]
    assert by_id["web-fetch"]["policy"]["enabled"] is False
    assert by_id["web-fetch"]["policy"]["disabled_reason"] == "Missing project configuration: web_tools.enabled, web_tools.fetch_enabled"
    assert by_id["plugin-tool"]["policy"]["enabled"] is False
    assert "disabled_by_default" in by_id["plugin-tool"]["policy"]["maturity"]
    assert shell["tools"][0]["id"] == "shell"
    assert shell["tools"][0]["policy"]["permission_key"] == "tool.shell.execution"


def test_local_server_status_projection_reports_persisted_active_turn(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    cfg = load_config(tmp_path)
    session = store.create_session(title="Persisted status turn")
    turn_state = create_turn_state_from_session(
        project_root=tmp_path,
        session=session,
        model_profile_id="codex_cli",
        backend_id="codex_cli",
        agent_id="operator",
        workbench_id=None,
        active_tools=["pwd"],
    )

    persist_turn_started(store, turn_state, prompt="pwd")
    active = _route_get(
        f"/sessions/{session.id}/status",
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    persist_turn_finished(store, turn_state)
    finished = _route_get(
        f"/sessions/{session.id}/status",
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )

    assert active["operator"]["phase"] == HarnessAgentPhase.TURN.value
    assert active["operator"]["turn_id"] == turn_state.turn_id
    assert active["operator"]["current_turn"]["turn_id"] == turn_state.turn_id
    assert finished["operator"]["phase"] == HarnessAgentPhase.IDLE.value


def test_local_server_api_session_prompt_is_append_only_compatibility_route(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    cfg = load_config(tmp_path)
    session = store.create_session(title="Compatibility prompt")

    response = _route_post(
        f"/api/session/{session.id}/prompt",
        body={"prompt": "pwd"},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )

    assert response["schema_version"] == "harness.api_session_prompt/v1"
    assert response["mode"] == "append_only"
    assert response["assistant_execution"] is False
    assert response["execution_started"] is False
    assert response["model_execution_started"] is False
    assert response["provider_execution_started"] is False
    assert response["no_hidden_fallback"] is True
    assert response["part"]["text"] == "pwd"
    assert _operator_prompt_event_kinds(store, session.id) == []


def test_local_server_session_routes_initialize_before_session_tool_execution(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    (tmp_path / "src").mkdir()
    store = SQLiteStore(tmp_path)
    cfg = load_config(tmp_path)
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("DROP TABLE IF EXISTS sessions")

    created = _route_post(
        "/sessions",
        body={"title": "Migrated server session"},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    session_id = created["session"]["id"]
    result = _route_post(
        f"/sessions/{session_id}/tool",
        body={"tool_id": "cd", "arguments": {"path": "src", "actor": "operator"}},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )

    assert created["ok"] is True
    assert result["ok"] is True
    assert result["cwd"]["cwd"] == "src"
    assert "no such table" not in json.dumps(created).lower()
    assert "no such table" not in json.dumps(result).lower()


def test_local_server_project_sync_and_experimental_workspace_actions_fail_closed(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    cfg = load_config(tmp_path)

    project_init = _route_post(
        "/project/git/init",
        body={},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    sync_start = _route_post(
        "/sync/start",
        body={"sessionID": "session_demo"},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    sync_history = _route_post(
        "/sync/history",
        body={"aggregate": 1},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    workspace_create = _route_post(
        "/experimental/workspace",
        body={"name": "remote"},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    workspace_warp = _route_post(
        "/experimental/workspace/warp",
        body={"id": None, "sessionID": "session_demo", "copyChanges": False},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )

    assert project_init["schema_version"] == "harness.project_action/v1"
    assert project_init["ok"] is False
    assert project_init["git_initialized"] is False
    assert project_init["filesystem_modified"] is False
    assert project_init["permission_granting"] is False
    assert sync_start["schema_version"] == "harness.workspace_action/v1"
    assert sync_start["sync_started"] is False
    assert sync_start["filesystem_modified"] is False
    assert sync_start["permission_granting"] is False
    assert sync_history["schema_version"] == "harness.sync_history/v1"
    assert sync_history["events"] == []
    assert sync_history["sync_started"] is False
    assert sync_history["permission_granting"] is False
    assert workspace_create["schema_version"] == "harness.workspace_action/v1"
    assert workspace_create["action"] == "experimental.create"
    assert workspace_create["filesystem_modified"] is False
    assert workspace_warp["action"] == "experimental.warp"
    assert workspace_warp["sync_started"] is False


def test_local_server_session_share_is_local_sanitized_and_hosted_fail_closed(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    cfg = load_config(tmp_path)
    session = store.create_session(title="Share session")
    message = store.append_session_message(session.id, "user", "token sk-abcdefghijklmnopqrstuvwxyz")
    store.append_session_part(session.id, message.id, "text", text="token sk-abcdefghijklmnopqrstuvwxyz")
    run = store.create_run("share run", "phase_1a_test", status="succeeded", session_id=session.id)
    artifact_path = store.initialize_run_artifacts(run.id)["final_report"]
    artifact_path.write_text("artifact body should not be included\n", encoding="utf-8")
    artifact = store.register_artifact(run.id, "final_report", artifact_path, session_id=session.id)

    share = _route_get(f"/sessions/{session.id}/share", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    hosted = _route_post(
        f"/sessions/{session.id}/share",
        body={"mode": "hosted"},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )

    assert share["schema_version"] == "harness.session_share/v1"
    assert share["ok"] is True
    assert share["share_mode"] == "local_snapshot"
    assert share["hosted_url"] is None
    assert share["include_artifacts"] is False
    assert share["artifact_files_included"] is False
    assert share["artifact_references"][0]["id"] == artifact.id
    assert share["artifact_references"][0]["contents_included"] is False
    assert share["artifact_references"][0]["file_included"] is False
    assert share["network_called"] is False
    assert share["filesystem_modified"] is False
    assert share["permission_granting"] is False
    assert share["snapshot_sha256"]
    serialized = json.dumps(share)
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in serialized
    assert "[REDACTED_SECRET]" in serialized
    assert "artifact body should not be included" not in serialized

    assert hosted["schema_version"] == "harness.session_share_action/v1"
    assert hosted["ok"] is False
    assert hosted["hosted_share_supported"] is False
    assert hosted["hosted_url"] is None
    assert hosted["network_called"] is False
    assert hosted["filesystem_modified"] is False
    assert hosted["permission_granting"] is False


def test_local_server_workspace_catalog_and_actions_are_metadata_only(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    cfg = load_config(tmp_path)

    workspaces = _route_get("/workspaces", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    clients = _route_get("/workspaces/clients", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    attach = _route_post(
        "/workspaces/attach",
        body={"workspace_id": "ws_other"},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    sync = _route_post(
        "/workspaces/sync",
        body={"workspace_id": workspaces["current_workspace_id"]},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    steal = _route_post(
        "/workspaces/steal",
        body={"workspace_id": workspaces["current_workspace_id"], "client_id": "client_other"},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    dispose = _route_post(
        "/workspaces/dispose",
        body={"workspace_id": workspaces["current_workspace_id"], "client_id": "client_other"},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )

    assert workspaces["schema_version"] == "harness.workspaces/v1"
    assert workspaces["registry_scope"] == "current_project_only"
    assert workspaces["global_registry_supported"] is False
    assert workspaces["workspace_routing_supported"] is True
    assert workspaces["remote_attach_supported"] is False
    assert workspaces["sync_supported"] is False
    assert workspaces["workspaces"][0]["path"] == str(tmp_path)
    assert workspaces["workspaces"][0]["initialized"] is True
    assert workspaces["network_called"] is False
    assert workspaces["filesystem_modified"] is False
    assert workspaces["permission_granting"] is False
    assert clients["schema_version"] == "harness.workspace_clients/v1"
    assert clients["clients"] == []
    assert clients["active_client_id"] is None
    assert clients["client_registration_supported"] is False
    assert clients["conflict_detection_supported"] is False
    assert clients["steal_supported"] is False
    assert clients["dispose_supported"] is False
    assert clients["network_called"] is False
    assert clients["filesystem_modified"] is False
    assert clients["permission_granting"] is False
    for payload, action in [(attach, "attach"), (sync, "sync"), (steal, "steal"), (dispose, "dispose")]:
        assert payload["schema_version"] == "harness.workspace_action/v1"
        assert payload["ok"] is False
        assert payload["action"] == action
        assert payload["network_called"] is False
        assert payload["filesystem_modified"] is False
        assert payload["process_started"] is False
        assert payload["attached"] is False
        assert payload["client_registered"] is False
        assert payload["client_stolen"] is False
        assert payload["disposed"] is False
        assert payload["sync_started"] is False
        assert payload["permission_granting"] is False


def test_local_server_lifecycle_mdns_and_dispose_are_safe_contracts(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    cfg = load_config(tmp_path)

    lifecycle = _route_get("/server/lifecycle", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    mdns = _route_get("/server/mdns", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    dispose = _route_post(
        "/server/dispose",
        body={"reason": "test"},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )

    assert lifecycle["schema_version"] == "harness.local_server_lifecycle/v1"
    assert lifecycle["server_url"] == "http://127.0.0.1:8765"
    assert lifecycle["dispose_supported"] is False
    assert lifecycle["remote_attach_supported"] is True
    assert lifecycle["sse_supported"] is True
    assert lifecycle["websocket_supported"] is False
    assert lifecycle["mdns_supported"] is False
    assert lifecycle["process_mutation_supported"] is False
    assert lifecycle["process_stopped"] is False
    assert lifecycle["network_called"] is False
    assert lifecycle["filesystem_modified"] is False
    assert lifecycle["permission_granting"] is False
    assert mdns["schema_version"] == "harness.local_server_mdns/v1"
    assert mdns["enabled"] is False
    assert mdns["advertised"] is False
    assert mdns["lan_discovery_supported"] is False
    assert mdns["network_broadcast_started"] is False
    assert mdns["network_called"] is False
    assert mdns["permission_granting"] is False
    assert dispose["schema_version"] == "harness.local_server_dispose/v1"
    assert dispose["ok"] is False
    assert dispose["dispose_supported"] is False
    assert dispose["process_stopped"] is False
    assert dispose["filesystem_modified"] is False
    assert dispose["permission_granting"] is False


def test_local_server_web_client_status_and_open_are_safe_contracts(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    cfg = load_config(tmp_path)

    client = _route_get("/web/client", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    opened = _route_post(
        "/web/open",
        body={"source": "test"},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )

    assert client["schema_version"] == "harness.web_client/v1"
    assert client["server_url"] == "http://127.0.0.1:8765"
    assert client["client_url"] == "http://127.0.0.1:8765/web"
    assert client["client_available"] is False
    assert client["static_assets_served"] is False
    assert client["desktop_wrapper_available"] is False
    assert client["open_supported"] is False
    assert client["network_called"] is False
    assert client["browser_opened"] is False
    assert client["process_started"] is False
    assert client["permission_granting"] is False
    assert opened["schema_version"] == "harness.web_client_action/v1"
    assert opened["ok"] is False
    assert opened["action"] == "open"
    assert opened["browser_opened"] is False
    assert opened["process_started"] is False
    assert opened["network_called"] is False
    assert opened["filesystem_modified"] is False
    assert opened["permission_granting"] is False


def test_local_server_session_replay_uses_append_only_cursor_without_execution(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    cfg = load_config(tmp_path)
    session = store.create_session(title="Replay session")
    message = store.append_session_message(session.id, "user", "Replay me")
    store.append_session_part(session.id, message.id, "text", text="Replay me")

    first = _route_get(
        f"/sessions/{session.id}/replay",
        query={"limit": ["1"]},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    second = _route_get(
        f"/sessions/{session.id}/replay",
        query={"after_seq": [str(first["next_after_seq"])]},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )

    assert first["schema_version"] == "harness.session_replay/v1"
    assert first["event_count"] == 1
    assert first["has_more"] is True
    assert first["replay_complete"] is False
    assert first["events"][0]["seq"] == 1
    assert first["next_after_seq"] == 1
    assert first["execution_started"] is False
    assert first["network_called"] is False
    assert first["filesystem_modified"] is False
    assert first["permission_granting"] is False
    assert second["schema_version"] == "harness.session_replay/v1"
    assert second["after_seq"] == 1
    assert second["event_count"] >= 2
    assert all(event["seq"] > 1 for event in second["events"])
    assert second["has_more"] is False
    assert second["replay_complete"] is True
    assert second["source"] == "append_only_event_store"


def test_local_server_session_status_and_abort_share_cli_lifecycle_contract(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    cfg = load_config(tmp_path)
    parent = store.create_session(title="Server abort parent")
    child = store.fork_session(parent.id, title="Server abort child")
    message = store.append_session_message(parent.id, "user", "Abort via server")
    store.append_session_part(parent.id, message.id, "text", text="Abort via server")

    before = _route_get(f"/sessions/{parent.id}/status", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    children = _route_get(f"/sessions/{parent.id}/children", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    aborted = _route_post(
        f"/sessions/{parent.id}/abort",
        body={"reason": "operator stopped waiting"},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    after = _route_get(f"/sessions/{parent.id}/status", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    events = _route_get(f"/sessions/{parent.id}/events", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)

    assert before["schema_version"] == "harness.session_status/v1"
    assert before["status"] == "active"
    assert before["message_count"] == 1
    assert before["child_session_ids"] == [child.id]
    assert before["planning_mode"] == {"active": False}
    assert before["terminal"] is False
    assert before["process_running"] is False
    assert before["permission_granting"] is False
    assert children["schema_version"] == "harness.session_children/v1"
    assert children["session_id"] == parent.id
    assert children["child_session_ids"] == [child.id]
    assert children["children"][0]["parent_session_id"] == parent.id
    assert children["execution_started"] is False
    assert children["permission_granting"] is False
    assert aborted["schema_version"] == "harness.session_abort/v1"
    assert aborted["session"]["status"] == "cancelled"
    assert aborted["process_stopped"] is False
    assert aborted["run_cancelled"] is False
    assert aborted["task_cancelled"] is False
    assert aborted["permission_granting"] is False
    assert after["status"] == "cancelled"
    assert after["terminal"] is True
    assert after["event_count"] == before["event_count"] + 1
    assert [event["kind"] for event in events["events"]].count("session.cancelled") == 1


def test_local_server_session_summary_updates_projection_and_event_store(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    cfg = load_config(tmp_path)
    session = store.create_session(title="Server summary")

    summarized = _route_post(
        f"/sessions/{session.id}/summary",
        body={
            "summary": "Server-side summary rollup.",
            "token_input": 220,
            "token_output": 44,
            "token_reasoning": 9,
            "token_cache_read": 11,
            "token_cache_write": 3,
            "estimated_cost_usd": "0.0456",
        },
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    summarized_alias = _route_post(
        f"/sessions/{session.id}/summarize",
        body={"summary": "Alias summary rollup."},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    status = _route_get(f"/sessions/{session.id}/status", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    events = _route_get(f"/sessions/{session.id}/events", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)

    assert summarized["schema_version"] == "harness.session_summary/v1"
    assert summarized["mutable_projection"] is True
    assert summarized["provider_execution_started"] is False
    assert summarized["permission_granting"] is False
    assert summarized["session"]["summary"] == "Server-side summary rollup."
    assert summarized["session"]["token_input"] == 220
    assert summarized["session"]["token_output"] == 44
    assert summarized["session"]["token_reasoning"] == 9
    assert summarized["session"]["token_cache_read"] == 11
    assert summarized["session"]["token_cache_write"] == 3
    assert summarized["session"]["estimated_cost_usd"] == "0.0456"
    assert summarized_alias["schema_version"] == "harness.session_summary/v1"
    assert summarized_alias["provider_execution_started"] is False
    assert summarized_alias["session"]["summary"] == "Alias summary rollup."
    assert status["summary"] == "Alias summary rollup."
    assert status["token_input"] == 220
    assert status["estimated_cost_usd"] == "0.0456"
    assert [event["kind"] for event in events["events"]].count("session.summary_updated") == 2
    with pytest.raises(ValueError, match="requires an explicit summary"):
        _route_post(
            f"/sessions/{session.id}/summarize",
            body={"providerID": "codex_cli", "modelID": "gpt-5.5"},
            project_root=tmp_path,
            store=store,
            cfg=cfg,
            host="127.0.0.1",
            port=8765,
        )


def test_local_server_message_retraction_and_part_correction_are_event_only(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    cfg = load_config(tmp_path)
    session = store.create_session(title="Server correction")
    message = store.append_session_message(session.id, "user", "Original server prompt")
    part = store.append_session_part(session.id, message.id, "text", text="Original server prompt")
    alias_message = store.append_session_message(session.id, "user", "Alias server prompt")
    alias_part = store.append_session_part(session.id, alias_message.id, "text", text="Alias server prompt")

    retracted = _route_post(
        f"/sessions/{session.id}/messages/{message.id}/retract",
        body={"reason": "superseded"},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    corrected = _route_post(
        f"/sessions/{session.id}/parts/{part.id}/correct",
        body={"corrected_text": "Corrected server prompt", "reason": "typo"},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    alias_retracted = _route_delete(f"/sessions/{session.id}/message/{alias_message.id}", store=store)
    alias_corrected = _route_patch(
        f"/sessions/{session.id}/message/{alias_message.id}/part/{alias_part.id}",
        body={"text": "Alias corrected prompt"},
        store=store,
    )
    alias_part_retracted = _route_delete(
        f"/sessions/{session.id}/message/{alias_message.id}/part/{alias_part.id}",
        store=store,
    )
    messages = _route_get(f"/sessions/{session.id}/messages", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    events = _route_get(f"/sessions/{session.id}/events", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)

    assert retracted["schema_version"] == "harness.session_message_retraction/v1"
    assert retracted["message_mutated"] is False
    assert retracted["parts_mutated"] is False
    assert retracted["permission_granting"] is False
    assert corrected["schema_version"] == "harness.session_part_correction/v1"
    assert corrected["part_mutated"] is False
    assert corrected["message_mutated"] is False
    assert corrected["permission_granting"] is False
    assert alias_retracted["schema_version"] == "harness.session_message_retraction/v1"
    assert alias_retracted["message_deleted"] is False
    assert alias_retracted["parts_deleted"] is False
    assert alias_corrected["schema_version"] == "harness.session_part_correction/v1"
    assert alias_corrected["part_mutated"] is False
    assert alias_part_retracted["schema_version"] == "harness.session_part_retraction/v1"
    assert alias_part_retracted["part_deleted"] is False
    assert alias_part_retracted["part_mutated"] is False
    assert messages["messages"][0]["content_preview"] == "Original server prompt"
    assert messages["parts"][message.id][0]["text"] == "Original server prompt"
    assert messages["messages"][1]["content_preview"] == "Alias server prompt"
    assert messages["parts"][alias_message.id][0]["text"] == "Alias server prompt"
    kinds = [event["kind"] for event in events["events"]]
    assert kinds.count("session.message.retracted") == 2
    assert kinds.count("session.part.corrected") == 2
    assert kinds.count("session.part.retracted") == 1


def test_local_server_session_todos_and_questions_are_persisted_without_execution(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    cfg = load_config(tmp_path)
    session = store.create_session(title="Server todos")

    todo = _route_post(
        f"/sessions/{session.id}/todos",
        body={"content": "Review changed files", "status": "pending", "priority": 3},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    todos = _route_get(f"/sessions/{session.id}/todos", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    todo_alias = _route_get(f"/sessions/{session.id}/todo", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    question = _route_post(
        f"/sessions/{session.id}/questions",
        body={"question": "Run tests now?", "choices": ["yes", "no"]},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    questions = _route_get(f"/sessions/{session.id}/questions", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    global_questions = _route_get("/question", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    question_reply = _route_post(
        f"/question/{question['part']['id']}/reply",
        body={"answers": [["yes"]]},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    events = _route_get(f"/sessions/{session.id}/events", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)

    assert todo["schema_version"] == "harness.session_todo/v1"
    assert todo["todo"]["content"] == "Review changed files"
    assert todo["todo"]["priority"] == 3
    assert todo["execution_started"] is False
    assert todo["permission_granting"] is False
    assert todos["schema_version"] == "harness.session_todos/v1"
    assert todos["todos"][0]["id"] == todo["todo"]["id"]
    assert todos["execution_started"] is False
    assert todos["permission_granting"] is False
    assert todo_alias["schema_version"] == "harness.session_todos/v1"
    assert todo_alias["todos"][0]["id"] == todo["todo"]["id"]
    assert todo_alias["execution_started"] is False
    assert todo_alias["permission_granting"] is False
    assert question["schema_version"] == "harness.session_question/v1"
    assert question["part"]["text"] == "Run tests now?"
    assert question["part"]["metadata"]["choices"] == ["yes", "no"]
    assert question["execution_started"] is False
    assert question["permission_granting"] is False
    assert questions["schema_version"] == "harness.session_questions/v1"
    assert questions["questions"][0]["id"] == question["part"]["id"]
    assert questions["execution_started"] is False
    assert questions["permission_granting"] is False
    assert global_questions["schema_version"] == "harness.global_questions/v1"
    assert global_questions["pending_count"] == 1
    assert global_questions["questions"][0]["id"] == question["part"]["id"]
    assert global_questions["execution_started"] is False
    assert global_questions["permission_granting"] is False
    assert question_reply["schema_version"] == "harness.session_question_reply/v1"
    assert question_reply["answers"] == [["yes"]]
    assert question_reply["part_mutated"] is False
    assert question_reply["message_mutated"] is False
    assert question_reply["execution_started"] is False
    assert question_reply["permission_granting"] is False
    kinds = [event["kind"] for event in events["events"]]
    assert "todo.updated" in kinds
    assert "question.requested" in kinds
    assert "question.resolved" in kinds


def test_local_server_resolves_mentions_and_persists_session_event(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    (tmp_path / "src").mkdir()
    (tmp_path / "README.md").write_text("readme body\n", encoding="utf-8")
    (tmp_path / "src" / "app.py").write_text("print('hello')\n", encoding="utf-8")
    (tmp_path / ".env").write_text("TOKEN=secret-token\n", encoding="utf-8")
    store = SQLiteStore(tmp_path)
    session = store.create_session(title="Mention source")
    referenced = store.create_session(title="Referenced session")
    cfg = load_config(tmp_path)

    resolved = _route_post(
        f"/sessions/{session.id}/mentions/resolve",
        body={"prompt": f"Review @file:README.md and @directory:src with @session:{referenced.id}."},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    events = _route_get(
        f"/sessions/{session.id}/events",
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )

    assert resolved is not None
    assert resolved["schema_version"] == "harness.mention_resolution/v1"
    assert resolved["contents_included"] is False
    assert resolved["permission_granting"] is False
    assert resolved["execution_started"] is False
    assert [mention["kind"] for mention in resolved["mentions"]] == ["file", "directory", "session"]
    file_mention = resolved["mentions"][0]
    directory_mention = resolved["mentions"][1]
    session_mention = resolved["mentions"][2]
    assert file_mention["path"] == "README.md"
    assert file_mention["size_bytes"] == len("readme body\n")
    assert file_mention["estimated_tokens"] > 0
    assert directory_mention["path"] == "src"
    assert directory_mention["file_count"] == 1
    assert session_mention["session_id"] == referenced.id
    assert session_mention["title"] == "Referenced session"
    assert events["events"][-1]["kind"] == "session.mentions.resolved"
    assert events["events"][-1]["payload"]["mention_count"] == 3
    assert "readme body" not in json.dumps(resolved)
    assert "print('hello')" not in json.dumps(resolved)

    with pytest.raises(ValueError, match="excluded|secret"):
        _route_post(
            f"/sessions/{session.id}/mentions/resolve",
            body={"prompt": "Read @file:.env"},
            project_root=tmp_path,
            store=store,
            cfg=cfg,
            host="127.0.0.1",
            port=8765,
        )


def test_local_server_replies_to_permission_request_with_persisted_event(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    session = store.create_session(title="Permission reply")
    permission = store.request_session_permission(
        session.id,
        tool_id="shell",
        normalized_action="run",
        normalized_target_pattern="pytest",
        boundary_kind=SessionPermissionBoundaryKind.SHELL,
        risk="medium",
        scope=SessionPermissionScope.ONCE,
    )
    cfg = load_config(tmp_path)

    replied = _route_post(
        f"/sessions/{session.id}/permissions/{permission.id}/reply",
        body={"reply": "once", "message": "operator approved this run"},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    permission_alias = store.request_session_permission(
        session.id,
        tool_id="shell",
        normalized_action="run",
        normalized_target_pattern="ruff check",
        boundary_kind=SessionPermissionBoundaryKind.SHELL,
        risk="medium",
        scope=SessionPermissionScope.ONCE,
    )
    global_permissions = _route_get("/permission", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    replied_alias = _route_post(
        f"/permission/{permission_alias.id}/reply",
        body={"reply": "reject", "message": "operator declined global alias"},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    approval_alias = store.request_session_permission(
        session.id,
        tool_id="shell",
        normalized_action="run",
        normalized_target_pattern="pytest -q",
        boundary_kind=SessionPermissionBoundaryKind.SHELL,
        risk="medium",
        scope=SessionPermissionScope.ONCE,
    )
    approved_alias = _route_post(
        f"/sessions/{session.id}/approval/{approval_alias.id}",
        body={"action": "approve", "message": "operator approved approval alias"},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    snapshot = _route_get(
        f"/sessions/{session.id}/permissions/snapshot",
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    events = _route_get(
        f"/sessions/{session.id}/events",
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )

    assert replied["schema_version"] == "harness.session_permission_reply/v1"
    assert replied["decision"] == SessionPermissionStatus.ALLOWED.value
    assert replied["permission"]["status"] == SessionPermissionStatus.ALLOWED.value
    assert replied["permission"]["scope"] == SessionPermissionScope.ONCE.value
    assert replied["scope_broadened"] is False
    assert replied["execution_started"] is False
    assert replied["tool_execution_started"] is False
    assert replied["permission_granting"] is True
    assert global_permissions["schema_version"] == "harness.global_permissions/v1"
    assert global_permissions["pending_count"] == 1
    assert global_permissions["permissions"][0]["id"] == permission_alias.id
    assert global_permissions["execution_started"] is False
    assert global_permissions["permission_granting"] is False
    assert replied_alias["schema_version"] == "harness.session_permission_reply/v1"
    assert replied_alias["decision"] == SessionPermissionStatus.DENIED.value
    assert replied_alias["permission"]["status"] == SessionPermissionStatus.DENIED.value
    assert replied_alias["permission_granting"] is False
    assert approved_alias["schema_version"] == "harness.session_permission_reply/v1"
    assert approved_alias["decision"] == SessionPermissionStatus.ALLOWED.value
    assert approved_alias["tool_execution_started"] is False
    assert snapshot["pending_count"] == 0
    assert snapshot["counts"][SessionPermissionStatus.ALLOWED.value] == 2
    assert snapshot["counts"][SessionPermissionStatus.DENIED.value] == 1
    resolved_permissions = [
        event["payload"]["permission_id"]
        for event in events["events"]
        if event["kind"] == "permission.resolved"
    ]
    assert resolved_permissions == [permission.id, permission_alias.id, approval_alias.id]


def test_local_server_approval_resume_executes_once_and_denial_records_feedback(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    cfg = load_config(tmp_path)
    session = store.create_session(title="Approval resume")
    command = f"{sys.executable} -c \"print('resumed once')\""
    body = {"command": command, "timeout_seconds": 30, "shell_executable": "/bin/sh"}

    pending = _route_post(
        f"/sessions/{session.id}/shell",
        body=body,
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    approved = _route_post(
        f"/sessions/{session.id}/approval/{pending['permission_id']}",
        body={"action": "approve", "message": "operator approved once"},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    repeated = _route_post(
        f"/sessions/{session.id}/shell",
        body=body,
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )

    assert pending["permission_required"] is True
    assert pending["process_started"] is False
    assert pending["approval_card"]["command"] == command
    assert approved["decision"] == SessionPermissionStatus.ALLOWED.value
    assert approved["tool_execution_started"] is True
    assert approved["execution_started"] is True
    assert approved["resumed_result"]["ok"] is True
    assert "Shell command executed." in approved["resumed_result"]["preview"]
    assert "resumed once" in approved["resumed_result"]["preview"]
    assert store.get_session_permission(pending["permission_id"]).status == SessionPermissionStatus.EXPIRED
    assert repeated["permission_required"] is True
    assert repeated["permission_id"] != pending["permission_id"]
    assert repeated["process_started"] is False

    deny_pending = _route_post(
        f"/sessions/{session.id}/shell",
        body={"command": "echo denied", "timeout_seconds": 30},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    denied = _route_post(
        f"/sessions/{session.id}/approval/{deny_pending['permission_id']}",
        body={"action": "deny", "message": "needs a narrower command"},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )

    assert denied["decision"] == SessionPermissionStatus.DENIED.value
    assert denied["tool_execution_started"] is False
    assert denied["denial"]["feedback"] == "needs a narrower command"
    assert "needs a narrower command" in denied["model_visible_error"]
    events = store.list_session_store_events(session.id)
    denial_event = next(event for event in events if event.kind == "harness.approval.denied")
    assert denial_event.payload["permission_id"] == deny_pending["permission_id"]
    assert denial_event.payload["feedback"] == "needs a narrower command"


def test_local_server_lists_named_references_and_resolves_reference_mentions(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "guide.md").write_text("guide body\n", encoding="utf-8")
    config_path = tmp_path / ".harness" / "config.yaml"
    config_data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config_data["references"] = {
        "guide": {"kind": "local", "path": "docs/guide.md", "description": "Local guide"},
        "upstream": {"kind": "git", "url": "https://example.com/upstream.git", "description": "Remote metadata only"},
    }
    config_path.write_text(yaml.safe_dump(config_data, sort_keys=False), encoding="utf-8")
    cfg = load_config(tmp_path)
    store = SQLiteStore(tmp_path)
    session = store.create_session(title="Reference session")

    references = _route_get("/references", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    resolved = _route_post(
        f"/sessions/{session.id}/mentions/resolve",
        body={"prompt": "Use @reference:guide and compare @reference:upstream"},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    events = _route_get(
        f"/sessions/{session.id}/events",
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )

    assert references["schema_version"] == "harness.references/v1"
    assert references["contents_included"] is False
    assert [reference["name"] for reference in references["references"]] == ["guide", "upstream"]
    guide = references["references"][0]
    upstream = references["references"][1]
    assert guide["kind"] == "local"
    assert guide["path"] == "docs/guide.md"
    assert guide["size_bytes"] == len("guide body\n")
    assert upstream["kind"] == "git"
    assert upstream["url"] == "https://example.com/upstream.git"
    assert upstream["network_required"] is True
    assert resolved is not None
    assert [mention["name"] for mention in resolved["mentions"]] == ["guide", "upstream"]
    assert resolved["mentions"][0]["contents_included"] is False
    assert events["events"][-1]["kind"] == "session.mentions.resolved"
    assert "guide body" not in json.dumps([references, resolved])

    with pytest.raises(ValueError, match="configured reference"):
        _route_post(
            f"/sessions/{session.id}/mentions/resolve",
            body={"prompt": "Use @reference:missing"},
            project_root=tmp_path,
            store=store,
            cfg=cfg,
            host="127.0.0.1",
            port=8765,
        )


def test_local_server_projects_lsp_and_formatter_config_without_starting_processes(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    config_path = tmp_path / ".harness" / "config.yaml"
    config_data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config_data["lsp"] = {
        "enabled": True,
        "servers": {
            "python": {
                "enabled": True,
                "command": ["pyright-langserver", "--stdio"],
                "file_extensions": [".py"],
            }
        },
    }
    config_data["formatter"] = {
        "enabled": True,
        "profiles": {
            "python": {
                "enabled": True,
                "command": ["black", "--quiet", "-"],
                "file_extensions": [".py"],
                "format_on_accepted_edit": True,
            }
        },
    }
    config_path.write_text(yaml.safe_dump(config_data, sort_keys=False), encoding="utf-8")
    cfg = load_config(tmp_path)
    store = SQLiteStore(tmp_path)

    diagnostics = _route_get("/lsp/diagnostics", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    formatters = _route_get("/formatters", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)

    assert diagnostics["schema_version"] == "harness.lsp_diagnostics/v1"
    assert diagnostics["enabled"] is True
    assert diagnostics["process_started"] is False
    assert diagnostics["diagnostics"] == []
    assert diagnostics["diagnostic_count"] == 0
    assert diagnostics["live_lsp_supported"] is False
    assert diagnostics["diagnostics_collection_supported"] is False
    assert diagnostics["policy_boundary"]["kind"] == "lsp_diagnostics_projection"
    assert diagnostics["policy_boundary"]["process_backed_lsp_allowed"] is False
    assert diagnostics["blocked_reasons"] == ["lsp_process_launch_disabled"]
    assert diagnostics["servers"] == [
        {
            "name": "python",
            "enabled": True,
            "configured": True,
            "file_extensions": [".py"],
            "command_configured": True,
            "launch_supported": False,
            "diagnostics_collection_supported": False,
            "blocked_reasons": ["lsp_process_launch_disabled"],
            "process_started": False,
            "diagnostics": [],
        }
    ]
    assert formatters["schema_version"] == "harness.formatters/v1"
    assert formatters["enabled"] is True
    assert formatters["process_started"] is False
    assert formatters["profiles"] == [
        {
            "name": "python",
            "enabled": True,
            "configured": True,
            "file_extensions": [".py"],
            "command_configured": True,
            "format_on_accepted_edit": True,
            "process_started": False,
        }
    ]


def test_local_server_projects_mcp_config_without_connecting(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    config_path = tmp_path / ".harness" / "config.yaml"
    config_data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config_data["mcp"] = {
        "enabled": True,
        "servers": {
            "local_docs": {
                "kind": "local",
                "enabled": True,
                "command": ["mcp-docs", "--stdio"],
                "description": "Local docs server",
                "resources": {
                    "guide": {
                        "uri": "mcp://local_docs/guide",
                        "path": "mcp-cache/guide.md",
                        "enabled": True,
                        "content_type": "text/markdown",
                        "description": "Cached docs guide",
                    }
                },
            },
            "remote_tracker": {
                "kind": "remote",
                "enabled": True,
                "url": "https://example.com/mcp",
                "description": "Remote tracker",
            },
        },
    }
    config_path.write_text(yaml.safe_dump(config_data, sort_keys=False), encoding="utf-8")
    cfg = load_config(tmp_path)
    store = SQLiteStore(tmp_path)

    status = _route_get("/mcp/status", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    status_alias = _route_get("/mcp", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    resources = _route_get("/mcp/resources", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    add = _route_post(
        "/mcp",
        body={"name": "new_server", "config": {"kind": "local"}},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    connect = _route_post(
        "/mcp/local_docs/connect",
        body={},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    auth = _route_post(
        "/mcp/remote_tracker/auth/authenticate",
        body={},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    auth_remove = _route_delete("/mcp/remote_tracker/auth", store=store)

    assert status["schema_version"] == "harness.mcp_status/v1"
    assert status_alias == status
    assert status["enabled"] is True
    assert status["connected"] is False
    assert status["tool_registration_enabled"] is False
    assert status["tool_execution_supported"] is False
    assert status["resource_reads_cached_only"] is True
    assert status["policy_boundary"]["kind"] == "mcp_metadata_projection"
    assert status["policy_boundary"]["process_launch_allowed"] is False
    assert status["policy_boundary"]["network_connection_allowed"] is False
    assert status["policy_boundary"]["tool_execution_allowed"] is False
    assert status["blocked_reasons"] == [
        "mcp_process_launch_disabled",
        "mcp_network_connection_disabled",
        "mcp_tool_execution_disabled",
    ]
    assert status["process_started"] is False
    assert status["network_called"] is False
    assert [server["name"] for server in status["servers"]] == ["local_docs", "remote_tracker"]
    local, remote = status["servers"]
    assert local["command_configured"] is True
    assert local["url_configured"] is False
    assert local["requires_network"] is False
    assert local["connected"] is False
    assert local["tool_execution_supported"] is False
    assert local["blocked_reasons"] == [
        "mcp_process_launch_disabled",
        "mcp_network_connection_disabled",
        "mcp_tool_execution_disabled",
    ]
    assert remote["command_configured"] is False
    assert remote["url_configured"] is True
    assert remote["requires_network"] is True
    assert remote["oauth_authenticated"] is False
    assert remote["permission_granting"] is False
    assert resources["schema_version"] == "harness.mcp_resources/v1"
    assert resources["enabled"] is True
    mcp_path_safety = {
        "schema_version": "harness.configured_path_safety/v1",
        "ok": True,
        "configured_path": "mcp-cache/guide.md",
        "symlink_policy": "reject_configured_path_components",
        "symlink_checked": True,
        "symlink_safe": True,
        "project_boundary_checked": True,
        "relative_path": "mcp-cache/guide.md",
        "error_type": None,
        "message": None,
        "blocked_reasons": [],
    }
    assert resources["resources"] == [
        {
            "name": "guide",
            "server": "local_docs",
            "uri": "mcp://local_docs/guide",
            "enabled": True,
            "cached": True,
            "path": "mcp-cache/guide.md",
            "path_safety": mcp_path_safety,
            "symlink_policy": "reject_configured_path_components",
            "symlink_checked": True,
            "symlink_safe": True,
            "content_type": "text/markdown",
            "description": "Cached docs guide",
            "contents_included": False,
            "evidence_status": "metadata_only",
            "resource_read_supported": False,
            "session_tool_resource_read_supported": True,
            "tool_execution_supported": False,
            "requires_permission": True,
            "policy_boundary": {
                "kind": "mcp_cached_resource_metadata",
                "server": "local_docs",
                "process_launch_allowed": False,
                "network_connection_allowed": False,
                "tool_execution_allowed": False,
                "session_tool_permission_required": True,
                "contents_included": False,
                "symlink_policy": "reject_configured_path_components",
                "symlink_safe": True,
            },
            "blocked_reasons": ["mcp_resource_read_requires_permission", "mcp_connection_disabled"],
            "connected": False,
            "process_started": False,
            "network_called": False,
            "permission_granting": False,
        }
    ]
    assert resources["resource_count"] == 1
    assert resources["unsafe_resource_count"] == 0
    assert resources["cached_only"] is True
    assert resources["contents_included"] is False
    assert resources["tool_execution_supported"] is False
    assert resources["resource_read_supported"] is False
    assert resources["session_tool_resource_read_supported"] is True
    assert resources["policy_boundary"]["kind"] == "mcp_resources_projection"
    assert resources["policy_boundary"]["requires_permission"] is True
    assert resources["policy_boundary"]["symlink_policy"] == "reject_configured_path_components"
    assert resources["blocked_reasons"] == ["mcp_connection_disabled", "mcp_tool_execution_disabled"]
    assert resources["connected"] is False
    assert resources["network_called"] is False
    for payload, action, name in [
        (add, "add", "new_server"),
        (connect, "connect", "local_docs"),
        (auth, "auth.authenticate", "remote_tracker"),
        (auth_remove, "auth.remove", "remote_tracker"),
    ]:
        assert payload["schema_version"] == "harness.mcp_action/v1"
        assert payload["ok"] is False
        assert payload["action"] == action
        assert payload["name"] == name
        assert payload["connected"] is False
        assert payload["policy_boundary"]["kind"] == "mcp_action"
        assert payload["policy_boundary"]["tool_execution_allowed"] is False
        assert payload["blocked_reasons"] == [
            "mcp_action_disabled",
            "mcp_process_launch_disabled",
            "mcp_network_connection_disabled",
        ]
        assert payload["oauth_started"] is False
        assert payload["credentials_stored"] is False
        assert payload["tool_registration_enabled"] is False
        assert payload["tool_execution_started"] is False
        assert payload["process_started"] is False
        assert payload["network_called"] is False
        assert payload["filesystem_modified"] is False
        assert payload["permission_granting"] is False


def test_local_server_mcp_resources_project_config_reports_symlink_unsafe_without_reading(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    real_cache = tmp_path / "real-mcp-cache"
    real_cache.mkdir()
    real_cache.joinpath("guide.md").write_text("# Guide\n\nDo not project this body.\n", encoding="utf-8")
    (tmp_path / "mcp-cache").symlink_to(real_cache, target_is_directory=True)
    config_path = tmp_path / ".harness" / "config.yaml"
    config_data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config_data["mcp"] = {
        "enabled": True,
        "servers": {
            "docs": {
                "kind": "local",
                "enabled": True,
                "command": ["mcp-docs", "--stdio"],
                "resources": {
                    "guide": {
                        "uri": "mcp://docs/guide",
                        "path": "mcp-cache/guide.md",
                        "enabled": True,
                        "content_type": "text/markdown",
                    }
                },
            }
        },
    }
    config_path.write_text(yaml.safe_dump(config_data, sort_keys=False), encoding="utf-8")
    cfg = load_config(tmp_path)
    store = SQLiteStore(tmp_path)

    resources = _route_get("/mcp/resources", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)

    assert resources["schema_version"] == "harness.mcp_resources/v1"
    assert resources["unsafe_resource_count"] == 1
    resource = resources["resources"][0]
    assert resource["uri"] == "mcp://docs/guide"
    assert resource["contents_included"] is False
    assert resource["session_tool_resource_read_supported"] is False
    assert resource["symlink_policy"] == "reject_configured_path_components"
    assert resource["symlink_checked"] is True
    assert resource["symlink_safe"] is False
    assert resource["path_safety"]["ok"] is False
    assert resource["path_safety"]["error_type"] == "path_security"
    assert resource["path_safety"]["blocked_reasons"] == ["configured_path_security_failed"]
    assert "Path contains symlink component: mcp-cache" in resource["path_safety"]["message"]
    assert resource["blocked_reasons"] == [
        "configured_path_security_failed",
        "mcp_resource_read_requires_permission",
        "mcp_connection_disabled",
    ]
    assert resource["policy_boundary"]["symlink_safe"] is False
    assert "Do not project this body" not in json.dumps(resources)
    assert resources["process_started"] is False
    assert resources["network_called"] is False
    assert resources["permission_granting"] is False


def test_local_server_projects_plugin_config_without_loading_plugins(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    (tmp_path / "plugins" / "reviewer").mkdir(parents=True)
    (tmp_path / "plugins" / "reviewer" / "plugin.json").write_text('{"name":"reviewer"}\n', encoding="utf-8")
    config_path = tmp_path / ".harness" / "config.yaml"
    config_data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config_data["plugins"] = {
        "enabled": True,
        "project": {
            "reviewer": {
                "enabled": True,
                "path": "plugins/reviewer",
                "spec": "./plugins/reviewer",
                "entrypoint": "plugin.json",
                "version": "0.1.0",
                "description": "Project review plugin",
                "options": {"mode": "audit"},
            },
            "remote_pack": {
                "enabled": False,
                "url": "https://example.com/plugin.git",
                "spec": "github:example/plugin",
                "description": "Remote metadata only",
            },
        },
    }
    config_path.write_text(yaml.safe_dump(config_data, sort_keys=False), encoding="utf-8")
    cfg = load_config(tmp_path)
    store = SQLiteStore(tmp_path)

    plugins = _route_get("/plugins", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)

    assert plugins["schema_version"] == "harness.plugins/v1"
    assert plugins["enabled"] is True
    assert plugins["runtime_loaded"] is False
    assert plugins["tools_registered"] is False
    assert plugins["plugin_count"] >= 2
    assert plugins["project_plugin_count"] == 2
    assert plugins["tool_execution_supported"] is False
    assert plugins["origin_review_required"] is True
    assert plugins["policy_boundary"]["kind"] == "plugin_catalog_metadata"
    assert plugins["policy_boundary"]["tool_execution_allowed"] is False
    assert plugins["blocked_reasons"] == [
        "plugin_origin_review_required",
        "plugin_runtime_load_disabled",
        "plugin_tool_execution_disabled",
    ]
    assert plugins["install_supported"] is False
    assert plugins["update_supported"] is False
    assert plugins["remove_supported"] is False
    assert plugins["filesystem_modified"] is False
    assert plugins["network_called"] is False
    project_plugins = [plugin for plugin in plugins["plugins"] if plugin["scope"] == "project"]
    assert [plugin["name"] for plugin in project_plugins] == ["remote_pack", "reviewer"]
    remote, reviewer = project_plugins
    assert remote["enabled"] is False
    assert remote["url"] == "https://example.com/plugin.git"
    assert remote["spec"] == "github:example/plugin"
    assert remote["source_kind"] == "remote"
    assert remote["origin_review_required"] is True
    assert remote["runtime_load_supported"] is False
    assert remote["tool_execution_supported"] is False
    assert remote["policy_boundary"]["kind"] == "plugin_metadata_projection"
    assert remote["policy_boundary"]["network_fetch_allowed"] is False
    assert remote["blocked_reasons"] == [
        "plugin_origin_review_required",
        "plugin_runtime_load_disabled",
        "plugin_tool_execution_disabled",
    ]
    assert remote["runtime_loaded"] is False
    assert remote["tools_registered"] is False
    assert reviewer["enabled"] is True
    assert reviewer["origin"] == "config"
    assert reviewer["source_kind"] == "local"
    assert reviewer["spec"] == "./plugins/reviewer"
    assert reviewer["entrypoint"] == "plugin.json"
    assert reviewer["options_configured"] is True
    assert reviewer["option_keys"] == ["mode"]
    assert reviewer["path"] == "plugins/reviewer"
    assert reviewer["exists"] is True
    assert reviewer["directory"] is True
    assert reviewer["manifest_path"] == "plugins/reviewer/plugin.json"
    assert reviewer["manifest_exists"] is True
    assert reviewer["version"] == "0.1.0"
    assert reviewer["tool_execution_supported"] is False
    assert reviewer["policy_boundary"]["scope"] == "project"
    assert reviewer["filesystem_modified"] is False
    assert reviewer["network_called"] is False
    assert reviewer["permission_granting"] is False


def test_plugin_provider_hook_cannot_run_network_during_catalog_projection(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    plugin_dir = tmp_path / "plugins" / "model-router"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.json").write_text(
        json.dumps(
            {
                "name": "model-router",
                "harness": {
                    "provider_hooks": [
                        {
                            "hook_id": "router_hook",
                            "provider_id": "plugin_router",
                            "display_name": "Plugin Router",
                            "protocol": "openai_chat",
                            "data_boundary": "external_router",
                            "endpoint": "https://router.example.invalid/v1",
                            "credential": {"kind": "env", "env_var": "PLUGIN_ROUTER_KEY"},
                            "models": {"router-model": {"api_id": "remote/router-model"}},
                            "safe_metadata_only": False,
                            "safety_notes": ["Manifest metadata only; runtime loader remains disabled."],
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    config_path = tmp_path / ".harness" / "config.yaml"
    config_data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config_data["plugins"] = {
        "enabled": True,
        "project": {
            "model_router": {
                "enabled": True,
                "path": "plugins/model-router",
                "spec": "./plugins/model-router",
                "entrypoint": "plugin.json",
                "description": "Model router hook fixture",
            }
        },
    }
    config_path.write_text(yaml.safe_dump(config_data, sort_keys=False), encoding="utf-8")
    cfg = load_config(tmp_path)
    store = SQLiteStore(tmp_path)

    plugins = _route_get("/plugins", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    models = _route_get("/models", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)

    project_plugin = next(plugin for plugin in plugins["plugins"] if plugin["name"] == "model_router")
    hook = project_plugin["provider_hooks"][0]

    assert plugins["provider_hook_supported"] is True
    assert plugins["provider_hook_count"] >= 1
    assert plugins["provider_registered"] is False
    assert plugins["runtime_loaded"] is False
    assert plugins["network_called"] is False
    assert project_plugin["provider_hook_count"] == 1
    assert project_plugin["provider_registered"] is False
    assert project_plugin["runtime_loaded"] is False
    assert project_plugin["network_called"] is False
    assert project_plugin["provider_hook_interface"]["metadata_only"] is True
    assert project_plugin["provider_hook_interface"]["runtime_loaded"] is False
    assert project_plugin["provider_hook_interface"]["provider_registered"] is False
    assert project_plugin["provider_hook_interface"]["model_discovery_started"] is False
    assert project_plugin["provider_hook_interface"]["network_called"] is False
    assert hook["schema_version"] == "harness.plugin_provider_hook/v1"
    assert hook["provider_id"] == "plugin_router"
    assert hook["protocol"] == "openai_chat"
    assert hook["data_boundary"] == "external_router"
    assert hook["endpoint_configured"] is True
    assert hook["credential_kind"] == "env"
    assert hook["model_count"] == 1
    assert hook["runtime_loaded"] is False
    assert hook["provider_registered"] is False
    assert hook["provider_execution_started"] is False
    assert hook["model_discovery_started"] is False
    assert hook["network_called"] is False
    assert hook["credentials_included"] is False
    assert "plugin_origin_review_required" in hook["blocked_reasons"]
    assert "plugin_runtime_load_disabled" in hook["blocked_reasons"]
    assert "plugin_router/router-model" not in {model["raw_model_ref"] for model in models["models"]}
    assert models["metadata_only"] is True
    assert models["network_accessed"] is False
    assert models["credentials_included"] is False
    serialized = json.dumps(plugins)
    assert "https://router.example.invalid/v1" not in serialized
    assert "PLUGIN_ROUTER_KEY" not in serialized


def test_plugin_provider_hook_requires_declared_runtime_contract(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    plugin_dir = tmp_path / "plugins" / "incomplete-router"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.json").write_text(
        json.dumps(
            {
                "name": "incomplete-router",
                "harness": {
                    "provider_hooks": [
                        {
                            "provider_id": "incomplete_router",
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    config_path = tmp_path / ".harness" / "config.yaml"
    config_data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config_data["plugins"] = {
        "enabled": True,
        "project": {
            "incomplete_router": {
                "enabled": True,
                "path": "plugins/incomplete-router",
                "spec": "./plugins/incomplete-router",
                "entrypoint": "plugin.json",
            }
        },
    }
    config_path.write_text(yaml.safe_dump(config_data, sort_keys=False), encoding="utf-8")
    cfg = load_config(tmp_path)
    store = SQLiteStore(tmp_path)

    plugins = _route_get("/plugins", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)

    project_plugin = next(plugin for plugin in plugins["plugins"] if plugin["name"] == "incomplete_router")
    interface = project_plugin["provider_hook_interface"]
    hook = project_plugin["provider_hooks"][0]

    assert interface["provider_hook_count"] == 1
    assert interface["metadata_only"] is True
    assert interface["runtime_loaded"] is False
    assert interface["provider_registered"] is False
    assert interface["network_called"] is False
    assert "plugin_provider_hook_invalid" in interface["blocked_reasons"]
    assert {
        "provider_hook_protocol_missing:incomplete_router:incomplete_router",
        "provider_hook_data_boundary_missing:incomplete_router:incomplete_router",
        "provider_hook_endpoint_missing:incomplete_router:incomplete_router",
        "provider_hook_credential_missing:incomplete_router:incomplete_router",
        "provider_hook_models_missing:incomplete_router:incomplete_router",
        "provider_hook_safety_notes_missing:incomplete_router:incomplete_router",
    }.issubset(set(interface["validation_errors"]))
    assert hook["provider_id"] == "incomplete_router"
    assert hook["protocol"] is None
    assert hook["data_boundary"] is None
    assert hook["endpoint_configured"] is False
    assert hook["credential_kind"] == "none"
    assert hook["model_count"] == 0
    assert hook["safety_notes"] == []
    assert "plugin_provider_hook_invalid" in hook["blocked_reasons"]
    assert hook["runtime_loaded"] is False
    assert hook["provider_registered"] is False
    assert hook["network_called"] is False
    assert plugins["provider_registered"] is False
    assert plugins["network_called"] is False


def test_plugin_provider_hook_rejects_raw_secret_values_without_leakage(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    plugin_dir = tmp_path / "plugins" / "secret-router"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.json").write_text(
        json.dumps(
            {
                "name": "secret-router",
                "harness": {
                    "provider_hooks": [
                        {
                            "provider_id": "secret_router",
                            "protocol": "openai_chat",
                            "data_boundary": "external_router",
                            "endpoint": "https://router.example.invalid/v1",
                            "credential": {"kind": "api_key", "value": "sk-plugin-raw-secret"},
                            "headers": {
                                "Authorization": {"kind": "literal", "value": "Bearer raw-header-secret"},
                                "X-Trace": {"kind": "env", "env_var": "PLUGIN_TRACE_HEADER"},
                            },
                            "models": {"router-model": {"api_id": "remote/router-model"}},
                            "safe_metadata_only": True,
                            "safety_notes": ["Manifest metadata only; runtime loader remains disabled."],
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    config_path = tmp_path / ".harness" / "config.yaml"
    config_data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config_data["plugins"] = {
        "enabled": True,
        "project": {
            "secret_router": {
                "enabled": True,
                "path": "plugins/secret-router",
                "spec": "./plugins/secret-router",
                "entrypoint": "plugin.json",
            }
        },
    }
    config_path.write_text(yaml.safe_dump(config_data, sort_keys=False), encoding="utf-8")
    cfg = load_config(tmp_path)
    store = SQLiteStore(tmp_path)

    plugins = _route_get("/plugins", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    models = _route_get("/models", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)

    project_plugin = next(plugin for plugin in plugins["plugins"] if plugin["name"] == "secret_router")
    interface = project_plugin["provider_hook_interface"]
    hook = project_plugin["provider_hooks"][0]
    serialized = json.dumps({"plugins": plugins, "models": models})

    assert {
        "provider_hook_credential_value_not_allowed:secret_router:secret_router:value",
        "provider_hook_header_must_use_env_ref:secret_router:secret_router:Authorization",
    }.issubset(set(interface["validation_errors"]))
    assert "plugin_provider_hook_invalid" in interface["blocked_reasons"]
    assert "plugin_provider_hook_invalid" in hook["blocked_reasons"]
    assert hook["credential_kind"] == "api_key"
    assert hook["runtime_loaded"] is False
    assert hook["provider_registered"] is False
    assert hook["provider_execution_started"] is False
    assert hook["model_discovery_started"] is False
    assert hook["network_called"] is False
    assert plugins["provider_registered"] is False
    assert plugins["network_called"] is False
    assert models["network_accessed"] is False
    assert "secret_router/router-model" not in {model["raw_model_ref"] for model in models["models"]}
    assert "sk-plugin-raw-secret" not in serialized
    assert "raw-header-secret" not in serialized
    assert "PLUGIN_TRACE_HEADER" not in serialized


def test_local_server_projects_skill_config_without_loading_skills(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    skill_dir = tmp_path / "skills" / "review"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Review\n\nMetadata only.\n", encoding="utf-8")
    config_path = tmp_path / ".harness" / "config.yaml"
    config_data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config_data["skills"] = {
        "enabled": True,
        "project": {
            "review": {
                "enabled": True,
                "path": "skills/review",
                "spec": "./skills/review",
                "version": "0.1.0",
                "description": "Review skill",
            }
        },
    }
    config_path.write_text(yaml.safe_dump(config_data, sort_keys=False), encoding="utf-8")
    cfg = load_config(tmp_path)
    store = SQLiteStore(tmp_path)

    skills = _route_get("/skills", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)

    assert skills["schema_version"] == "harness.skills/v1"
    assert skills["enabled"] is True
    assert skills["runtime_loaded"] is False
    assert skills["skill_body_loaded"] is False
    assert skills["tool_registered"] is False
    assert skills["load_supported"] is False
    assert skills["session_tool_load_supported"] is True
    assert skills["policy_boundary"]["session_tool_load_allowed_after_permission"] is True
    assert skills["filesystem_modified"] is False
    assert skills["network_called"] is False
    assert len([skill for skill in skills["skills"] if skill["scope"] == "project"]) == 1
    skill = [skill for skill in skills["skills"] if skill["scope"] == "project"][0]
    assert skill["name"] == "review"
    assert skill["enabled"] is True
    assert skill["origin"] == "config"
    assert skill["source_kind"] == "local"
    assert skill["spec"] == "./skills/review"
    assert skill["version"] == "0.1.0"
    assert skill["path"] == "skills/review"
    assert skill["exists"] is True
    assert skill["directory"] is True
    assert skill["skill_file_path"] == "skills/review/SKILL.md"
    assert skill["skill_file_exists"] is True
    assert skill["content_bytes"] == len("# Review\n\nMetadata only.\n".encode("utf-8"))
    assert skill["path_safety"]["schema_version"] == "harness.configured_path_safety/v1"
    assert skill["path_safety"]["ok"] is True
    assert skill["path_safety"]["relative_path"] == "skills/review"
    assert skill["skill_file_path_safety"]["ok"] is True
    assert skill["skill_file_path_safety"]["relative_path"] == "skills/review/SKILL.md"
    assert skill["symlink_policy"] == "reject_configured_path_components"
    assert skill["symlink_checked"] is True
    assert skill["symlink_safe"] is True
    assert skill["runtime_loaded"] is False
    assert skill["skill_body_loaded"] is False
    assert skill["tool_registered"] is False
    assert skill["session_tool_load_supported"] is True
    assert skill["policy_boundary"]["session_tool_load_allowed_after_permission"] is True
    assert skill["filesystem_modified"] is False
    assert skill["network_called"] is False
    assert skill["permission_granting"] is False
    assert "Metadata only" not in json.dumps(skills)


def test_local_server_projects_skill_config_reports_symlink_unsafe_without_loading_body(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    real_skill_dir = tmp_path / "real-skills" / "review"
    real_skill_dir.mkdir(parents=True)
    real_skill_dir.joinpath("SKILL.md").write_text("# Review\n\nDo not load from projection.\n", encoding="utf-8")
    (tmp_path / "skills-link").symlink_to(tmp_path / "real-skills", target_is_directory=True)
    config_path = tmp_path / ".harness" / "config.yaml"
    config_data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config_data["skills"] = {
        "enabled": True,
        "project": {
            "review": {
                "enabled": True,
                "path": "skills-link/review",
                "spec": "./skills-link/review",
                "version": "0.1.0",
                "description": "Review skill",
            }
        },
    }
    config_path.write_text(yaml.safe_dump(config_data, sort_keys=False), encoding="utf-8")
    cfg = load_config(tmp_path)
    store = SQLiteStore(tmp_path)

    skills = _route_get("/skills", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    status = _route_get("/extensions/status", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)

    assert skills["schema_version"] == "harness.skills/v1"
    assert skills["unsafe_skill_count"] == 1
    skill = [item for item in skills["skills"] if item["scope"] == "project"][0]
    assert skill["name"] == "review"
    assert skill["path"] == "skills-link/review"
    assert skill["session_tool_load_supported"] is False
    assert skill["symlink_policy"] == "reject_configured_path_components"
    assert skill["symlink_checked"] is True
    assert skill["symlink_safe"] is False
    assert skill["path_safety"]["ok"] is False
    assert skill["path_safety"]["error_type"] == "path_security"
    assert skill["path_safety"]["blocked_reasons"] == ["configured_path_security_failed"]
    assert "Path contains symlink component: skills-link" in skill["path_safety"]["message"]
    assert "configured_path_security_failed" in skill["blocked_reasons"]
    assert skill["skill_file_path"] is None
    assert skill["skill_file_exists"] is False
    assert "Do not load from projection" not in json.dumps(skills)
    assert status["skills"]["unsafe_skill_count"] == 1
    assert status["policy"]["runtime_loaded"] is False
    assert status["policy"]["network_called"] is False
    assert status["policy"]["filesystem_modified"] is False


def test_local_server_extensibility_status_summarizes_policy_without_side_effects(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    (tmp_path / "mcp-cache").mkdir()
    (tmp_path / "mcp-cache" / "guide.md").write_text("# Guide\n", encoding="utf-8")
    (tmp_path / "plugins" / "reviewer").mkdir(parents=True)
    (tmp_path / "plugins" / "reviewer" / "plugin.json").write_text('{"name":"reviewer"}\n', encoding="utf-8")
    skill_dir = tmp_path / "skills" / "review"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Review\n\nDo not load in diagnostics.\n", encoding="utf-8")
    config_path = tmp_path / ".harness" / "config.yaml"
    config_data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config_data["mcp"] = {
        "enabled": True,
        "servers": {
            "docs": {
                "kind": "local",
                "enabled": True,
                "command": ["mcp-docs", "--stdio"],
                "resources": {"guide": {"uri": "mcp://docs/guide", "path": "mcp-cache/guide.md", "enabled": True}},
            }
        },
    }
    config_data["plugins"] = {
        "enabled": True,
        "project": {"reviewer": {"enabled": True, "path": "plugins/reviewer", "spec": "./plugins/reviewer"}},
    }
    config_data["skills"] = {
        "enabled": True,
        "project": {"review": {"enabled": True, "path": "skills/review", "spec": "./skills/review"}},
    }
    config_data["web_tools"] = {
        "enabled": True,
        "fetch_enabled": True,
        "search_enabled": True,
        "approval_required": True,
        "allowed_domains": ["docs.example.com"],
        "search_endpoint_url": "http://127.0.0.1:9/search",
    }
    config_path.write_text(yaml.safe_dump(config_data, sort_keys=False), encoding="utf-8")
    cfg = load_config(tmp_path)
    store = SQLiteStore(tmp_path)

    status = _route_get("/extensions/status", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)

    assert status["schema_version"] == "harness.extensions_status/v1"
    assert status["mcp"]["enabled"] is True
    assert status["mcp"]["server_count"] == 1
    assert status["mcp"]["resource_count"] == 1
    assert status["mcp"]["unsafe_resource_count"] == 0
    assert status["mcp"]["connected"] is False
    assert status["mcp"]["process_started"] is False
    assert status["plugins"]["plugin_count"] >= 1
    assert status["plugins"]["project_plugin_count"] == 1
    assert status["plugins"]["runtime_loaded"] is False
    assert status["plugins"]["tools_registered"] is False
    assert status["plugins"]["install_supported"] is False
    assert status["skills"]["skill_count"] >= 1
    assert status["skills"]["project_skill_count"] == 1
    assert status["skills"]["unsafe_skill_count"] == 0
    assert status["skills"]["skill_body_loaded"] is False
    assert status["skills"]["load_supported"] is False
    assert status["skills"]["session_tool_load_supported"] is True
    assert status["web_tools"]["decisions"] == {"web-fetch": "approval_required", "web-search": "approval_required"}
    assert status["policy"]["network_called"] is False
    assert status["policy"]["filesystem_modified"] is False
    assert status["policy"]["runtime_loaded"] is False
    assert "Do not load in diagnostics" not in json.dumps(status)


def test_local_server_projects_web_tool_policy_without_network_call(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    config_path = tmp_path / ".harness" / "config.yaml"
    config_data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config_data["web_tools"] = {
        "enabled": True,
        "fetch_enabled": True,
        "search_enabled": False,
        "approval_required": True,
        "allowed_domains": ["docs.example.com"],
    }
    config_path.write_text(yaml.safe_dump(config_data, sort_keys=False), encoding="utf-8")
    cfg = load_config(tmp_path)
    store = SQLiteStore(tmp_path)

    web_tools = _route_get("/web/tools", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)

    assert web_tools["schema_version"] == "harness.web_tools/v1"
    assert web_tools["enabled"] is True
    assert web_tools["allowed_domains"] == ["docs.example.com"]
    assert web_tools["network_called"] is False
    assert web_tools["execution_supported"] is False
    assert web_tools["session_tool_execution_supported"] is True
    assert web_tools["permission_granting"] is False
    by_id = {tool["id"]: tool for tool in web_tools["tools"]}
    assert by_id["web-fetch"]["enabled"] is True
    assert by_id["web-fetch"]["decision"] == "approval_required"
    assert by_id["web-fetch"]["approval_required"] is True
    assert by_id["web-fetch"]["boundary_kind"] == "external_network"
    assert by_id["web-fetch"]["network_called"] is False
    assert by_id["web-fetch"]["session_tool_execution_supported"] is True
    assert by_id["web-search"]["enabled"] is False
    assert by_id["web-search"]["decision"] == "denied"
    assert by_id["web-search"]["approval_required"] is False


def test_local_server_discovers_instruction_files_without_loading_bodies(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    (tmp_path / "AGENTS.md").write_text("agent instructions\n", encoding="utf-8")
    (tmp_path / ".cursor" / "rules").mkdir(parents=True)
    (tmp_path / ".cursor" / "rules" / "python.md").write_text("python rules\n", encoding="utf-8")
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "copilot-instructions.md").write_text("copilot rules\n", encoding="utf-8")
    cfg = load_config(tmp_path)
    store = SQLiteStore(tmp_path)

    instructions = _route_get("/instructions", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)

    assert instructions["schema_version"] == "harness.instructions/v1"
    assert instructions["contents_included"] is False
    assert instructions["permission_granting"] is False
    paths = [file["path"] for file in instructions["files"]]
    assert paths == [
        "AGENTS.md",
        ".cursor/rules/python.md",
        ".github/copilot-instructions.md",
    ]
    first = instructions["files"][0]
    assert first["size_bytes"] == len("agent instructions\n")
    assert first["estimated_tokens"] > 0
    assert first["content_type"] == "text/markdown"
    assert "agent instructions" not in json.dumps(instructions)
    assert "python rules" not in json.dumps(instructions)


def test_local_server_lists_static_symbols_without_lsp_process(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    (tmp_path / "app.py").write_text(
        "class AppService:\n"
        "    pass\n\n"
        "async def build_plan():\n"
        "    return 'body must not leak'\n\n"
        "def helper():\n"
        "    return build_plan()\n",
        encoding="utf-8",
    )
    (tmp_path / "frontend.ts").write_text(
        "export function renderApp() { return 'secret body'; }\n"
        "const useWidget = () => true;\n",
        encoding="utf-8",
    )
    cfg = load_config(tmp_path)
    store = SQLiteStore(tmp_path)

    symbols = _route_get("/symbols", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    files = _route_get("/file", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    file_content = _route_get(
        "/file/content",
        query={"path": ["app.py"]},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    file_matches = _route_get(
        "/find/file",
        query={"query": ["front"]},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    text_matches = _route_get(
        "/find",
        query={"pattern": ["build_plan"]},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    symbol_alias = _route_get(
        "/find/symbol",
        query={"query": ["plan"], "path": ["app.py"]},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    filtered = _route_get(
        "/symbols",
        query={"q": ["plan"], "path": ["app.py"]},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )

    assert symbols["schema_version"] == "harness.symbols/v1"
    assert symbols["source"] == "static_scan"
    assert symbols["symbol_count"] == 5
    assert symbols["files_scanned"] == 2
    assert symbols["skipped_path_count"] == 0
    assert symbols["lsp_backed"] is False
    assert symbols["live_lsp_supported"] is False
    assert symbols["diagnostics_included"] is False
    assert symbols["policy_boundary"]["kind"] == "static_symbol_scan"
    assert symbols["policy_boundary"]["process_backed_lsp_allowed"] is False
    assert symbols["blocked_reasons"] == ["lsp_process_launch_disabled"]
    assert symbols["process_started"] is False
    assert symbols["contents_included"] is False
    assert files["schema_version"] == "harness.files/v1"
    assert {"app.py", "frontend.ts"} <= {file["path"] for file in files["files"]}
    assert files["contents_included"] is False
    assert file_content["schema_version"] == "harness.file_content/v1"
    assert file_content["path"] == "app.py"
    assert "class AppService" in file_content["preview"]
    assert file_matches["schema_version"] == "harness.find_file/v1"
    assert file_matches["matches"] == ["frontend.ts"]
    assert file_matches["contents_included"] is False
    assert text_matches["schema_version"] == "harness.find_text/v1"
    assert {match["line_number"] for match in text_matches["matches"]} == {4, 8}
    assert all(match["path"] == "app.py" for match in text_matches["matches"])
    assert symbol_alias["schema_version"] == "harness.symbols/v1"
    assert [symbol["name"] for symbol in symbol_alias["symbols"]] == ["build_plan"]
    names = {(symbol["kind"], symbol["name"], symbol["path"]) for symbol in symbols["symbols"]}
    assert ("class", "AppService", "app.py") in names
    assert ("function", "build_plan", "app.py") in names
    assert ("function", "helper", "app.py") in names
    assert ("function", "renderApp", "frontend.ts") in names
    assert ("function", "useWidget", "frontend.ts") in names
    assert [symbol["name"] for symbol in filtered["symbols"]] == ["build_plan"]
    assert "body must not leak" not in json.dumps(symbols)
    assert "secret body" not in json.dumps(symbols)


def test_local_server_prepares_metadata_only_attachments_and_persists_event(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    (tmp_path / "README.md").write_text("readme body\n", encoding="utf-8")
    large = tmp_path / "large.txt"
    large.write_bytes(b"x" * (300 * 1024))
    image = tmp_path / "image.png"
    image.write_bytes(_png_bytes(width=2, height=3))
    (tmp_path / ".env").write_text("TOKEN=secret-token\n", encoding="utf-8")
    store = SQLiteStore(tmp_path)
    session = store.create_session(title="Attachment session")
    cfg = load_config(tmp_path)

    prepared = _route_post(
        f"/sessions/{session.id}/attachments",
        body={"paths": ["README.md", "large.txt", "image.png"]},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    events = _route_get(
        f"/sessions/{session.id}/events",
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )

    assert prepared is not None
    assert prepared["schema_version"] == "harness.attachment_preparation/v1"
    assert prepared["contents_included"] is False
    assert prepared["permission_granting"] is False
    assert prepared["execution_started"] is False
    assert [attachment["path"] for attachment in prepared["attachments"]] == ["README.md", "large.txt", "image.png"]
    assert prepared["attachments"][0]["attachment_kind"] == "file_ref"
    assert prepared["attachments"][0]["content_type"] == "text/markdown"
    assert prepared["attachments"][0]["accepted"] is True
    assert prepared["attachments"][0]["requires_artifact_overflow"] is False
    assert prepared["attachments"][1]["requires_artifact_overflow"] is True
    assert prepared["attachments"][1]["accepted"] is True
    assert prepared["attachments"][2]["content_type"] == "image/png"
    assert prepared["attachments"][2]["image"] is True
    assert prepared["attachments"][2]["image_width"] == 2
    assert prepared["attachments"][2]["image_height"] == 3
    assert prepared["attachments"][2]["image_pixels"] == 6
    assert prepared["attachments"][2]["image_requires_resize"] is False
    assert prepared["attachments"][2]["max_image_pixels"] == 20_000_000
    assert events["events"][-1]["kind"] == "session.attachments.prepared"
    assert events["events"][-1]["payload"]["attachment_count"] == 3
    assert "readme body" not in json.dumps(prepared)
    assert "TOKEN=secret-token" not in json.dumps(prepared)

    with pytest.raises(ValueError, match="excluded|secret"):
        _route_post(
            f"/sessions/{session.id}/attachments",
            body={"paths": [".env"]},
            project_root=tmp_path,
            store=store,
            cfg=cfg,
            host="127.0.0.1",
            port=8765,
        )


def test_local_server_estimates_context_budget_and_persists_event(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    (tmp_path / "README.md").write_text("readme body\n", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("agent instructions\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('hello')\n", encoding="utf-8")
    store = SQLiteStore(tmp_path)
    session = store.create_session(title="Budget session")
    cfg = load_config(tmp_path)

    estimate = _route_post(
        f"/sessions/{session.id}/context/estimate",
        body={
            "prompt": "Review @file:README.md and @directory:src",
            "attachment_paths": ["README.md"],
            "include_instructions": True,
            "budget_tokens": 8,
        },
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    events = _route_get(
        f"/sessions/{session.id}/events",
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )

    assert estimate is not None
    assert estimate["schema_version"] == "harness.context_estimate/v1"
    assert estimate["contents_included"] is False
    assert estimate["permission_granting"] is False
    assert estimate["execution_started"] is False
    assert estimate["prompt_bytes"] == len("Review @file:README.md and @directory:src".encode("utf-8"))
    assert estimate["budget_tokens"] == 8
    assert estimate["within_budget"] is False
    kinds = [item["kind"] for item in estimate["items"]]
    assert kinds == ["prompt", "mention:file", "mention:directory", "attachment", "instruction"]
    assert estimate["total_estimated_tokens"] == sum(item.get("estimated_tokens", 0) for item in estimate["items"])
    assert events["events"][-1]["kind"] == "session.context.estimated"
    assert events["events"][-1]["payload"]["total_estimated_tokens"] == estimate["total_estimated_tokens"]
    assert "readme body" not in json.dumps(estimate)
    assert "agent instructions" not in json.dumps(estimate)
    assert "print('hello')" not in json.dumps(estimate)

    small = _route_post(
        f"/sessions/{session.id}/context/estimate",
        body={"prompt": "tiny", "budget_tokens": 10},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    assert small["within_budget"] is True


def test_local_server_file_content_blocks_secret_and_excluded_paths(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    (tmp_path / ".env").write_text("TOKEN=secret-token\n", encoding="utf-8")
    store = SQLiteStore(tmp_path)
    cfg = load_config(tmp_path)

    missing_path = _route_get
    try:
        missing_path("/files/content", query={}, project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    except ValueError as exc:
        assert "Missing required query parameter" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("missing file path should be rejected")

    try:
        _route_get(
            "/files/content",
            query={"path": [".env"]},
            project_root=tmp_path,
            store=store,
            cfg=cfg,
            host="127.0.0.1",
            port=8765,
        )
    except ValueError as exc:
        assert "excluded" in str(exc) or "Blocked secret-like path" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("secret-like file path should be rejected")


def _http_json(
    url: str,
    *,
    method: str = "GET",
    token: str | None = None,
    body: dict[str, object] | None = None,
) -> dict[str, object]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = Request(url, data=data, method=method)
    if token is not None:
        request.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    with urlopen(request, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def _http_text(url: str, *, token: str) -> str:
    request = Request(url)
    request.add_header("Authorization", f"Bearer {token}")
    with urlopen(request, timeout=5) as response:
        return response.read().decode("utf-8")


def _http_error_json(error: HTTPError) -> dict[str, object]:
    return json.loads(error.read().decode("utf-8"))


def _png_bytes(*, width: int, height: int) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + (b"\x00\x00\x00" * width) for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )
