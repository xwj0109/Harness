import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone

from rich.console import Console

from harness.memory.sqlite_store import SQLiteStore
from harness.models import RunEventType, SessionPermissionBoundaryKind, SessionPermissionScope, TokenUsageSnapshot
from harness.config import write_default_config
from harness.tui import (
    _append_streaming_content,
    _chat_response_to_tui_message,
    _merge_codex_stream_and_final_lines,
    _model_selection_dialog_entries,
    _runtime_elapsed_seconds,
    _transcript_event_messages,
    _update_markup_static,
    _render_composer_status,
    _model_catalog_pane_rows,
    build_tui_dashboard,
    build_codex_mode_model,
    build_right_panel_model,
    create_harness_app,
    render_codex_like_transcript,
    render_codex_mode,
    render_model_selection_dialog,
    render_right_panel,
)


def test_codex_mode_model_renders_four_event_backed_panes(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    run = store.create_run(goal="fix parser", task_type="codex_code_edit", status="completed", task_id=None)
    store.append_run_event(run.id, RunEventType.RUN_STARTED, {"agent": "code_editor"})
    store.append_run_event(run.id, RunEventType.POLICY_RESOLVED, {"active_repo": "approval_required"})
    store.append_run_event(run.id, RunEventType.BACKEND_STARTED, {"streaming": True})
    store.append_run_event(
        run.id,
        RunEventType.REASONING_SUMMARY_DELTA,
        {"delta": "Inspect tests, patch parser fallback."},
    )
    store.append_run_event(run.id, RunEventType.MODEL_MESSAGE_DELTA, {"delta": "I will inspect the tests first."})
    store.append_run_event(run.id, RunEventType.TOOL_CALL_STARTED, {"tool": "repo_read"})
    store.append_run_event(run.id, RunEventType.TOOL_CALL_FINISHED, {"tool": "repo_read"})
    store.append_run_event(run.id, RunEventType.FILE_WRITE, {"path": "src/parser.py"})
    store.append_run_event(run.id, RunEventType.TEST_STARTED, {"command": "pytest -q"})
    store.append_run_event(run.id, RunEventType.TEST_FINISHED, {"status": "passed"})
    store.append_token_usage_event(run.id, TokenUsageSnapshot(input_tokens=10, output_tokens=5, reasoning_tokens=3, total_tokens=18))
    store.append_run_event(run.id, RunEventType.RUN_FINISHED, {"status": "completed"})

    model = build_codex_mode_model(tmp_path, run.id)

    assert model["schema_version"] == "harness.tui_codex_mode/v1"
    assert model["run_id"] == run.id
    assert model["state"] == "Succeeded"
    assert [pane["id"] for pane in model["panes"]] == [
        "live_procedure",
        "model_output",
        "artifacts",
        "controls",
    ]
    assert any("● Tool call: repo_read" in line for line in model["panes"][0]["lines"])
    assert any("thinking summary: Inspect tests" in line for line in model["panes"][1]["lines"])
    assert "reasoning_count=3" in model["header"][-1]
    rendered = render_codex_mode(model)
    assert "Codex Mode" in rendered
    assert "Live Procedure" in rendered
    assert "Model Output" in rendered
    assert "Artifacts" in rendered
    assert "Controls" in rendered


def test_codex_mode_model_handles_no_runs(tmp_path) -> None:
    SQLiteStore(tmp_path).initialize()

    model = build_codex_mode_model(tmp_path)

    assert model["ok"] is True
    assert model["run_id"] is None
    assert model["state"] == "Queued"
    assert "No persisted run events yet." in model["panes"][0]["lines"]


def test_tui_dashboard_and_composer_include_operator_phase_cwd_and_approval(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    session = store.create_session(title="Operator status", metadata={"cwd": "src"})
    permission = store.request_session_permission(
        session.id,
        tool_id="shell",
        normalized_action="run",
        normalized_target_pattern=json.dumps(
            {
                "normalized_cwd": "src",
                "command": "pytest -q",
                "normalized_command": "pytest -q",
                "timeout_seconds": 120,
                "sandbox_profile": "session_tool_shell_exact",
                "network_policy": "host_network_available",
            },
            sort_keys=True,
        ),
        boundary_kind=SessionPermissionBoundaryKind.SHELL,
        risk="medium",
        scope=SessionPermissionScope.ONCE,
    )

    dashboard = build_tui_dashboard(tmp_path)
    operator = dashboard["active_session"]["operator"]
    composer = _render_composer_status(dashboard)

    assert operator["phase"] == "waiting_approval"
    assert operator["cwd"] == "src"
    assert operator["waiting_approval_id"] == permission.id
    assert "cwd=src" in composer
    assert "Operator: waiting_approval" in composer
    assert permission.id in composer
    assert "command=pytest -q" in composer
    assert operator["approval_card"]["command"] == "pytest -q"


def test_tui_composer_status_tracks_context_window_without_prompt_elapsed() -> None:
    dashboard = {
        "active_session": {
            "agent_id": "plan",
            "composer_context": {"total_estimated_tokens": 2048, "attachment_count": 1},
            "runtime": {"phase": "running", "active_elapsed_seconds": 65},
        },
        "model_catalog": {
            "active_model": {
                "raw_model_ref": "codex_cli/gpt-test",
                "context_limit": 8192,
            }
        },
    }

    rendered = _render_composer_status(dashboard)

    assert "ctx 25% used" in rendered
    assert "2k/8.2k" not in rendered
    assert "attachments 1" in rendered
    assert "prompt running" not in rendered

    unknown_limit = _render_composer_status(
        {
            "active_session": {
                "agent_id": "plan",
                "composer_context": {"total_estimated_tokens": 15, "attachment_count": 0},
            },
            "model_catalog": {"active_model": {"raw_model_ref": "default", "context_limit": None}},
        }
    )
    assert "ctx unknown" in unknown_limit
    assert "tokens" not in unknown_limit
    assert "ctx 15/?" not in unknown_limit


def test_model_picker_renders_protocol_alias_boundary_and_blocked_state() -> None:
    dashboard = {
        "model_catalog": {
            "no_hidden_fallback": True,
            "providers": [
                {
                    "provider_id": "codex",
                    "enabled": True,
                    "credential_status": "configured",
                    "data_boundary": "hosted_provider",
                },
                {
                    "provider_id": "openai",
                    "enabled": False,
                    "credential_status": "missing",
                    "data_boundary": "hosted_provider",
                },
                {
                    "provider_id": "local",
                    "enabled": True,
                    "credential_status": "configured",
                    "data_boundary": "local_only",
                    "endpoint": "http://localhost:11434/v1",
                },
            ],
            "models": [
                {
                    "provider_id": "codex",
                    "model_id": "gpt-5.5",
                    "raw_model_ref": "codex/gpt-5.5",
                    "canonical_model_ref": "codex_cli/gpt-5.5",
                    "protocol": "codex_cli",
                    "status": "active",
                    "source": "alias",
                    "context_limit": 8192,
                    "max_output_tokens": 4096,
                    "reasoning_support": "effort",
                    "variant_list": ["low", "high"],
                    "executable": True,
                    "blocked_reasons": [],
                },
                {
                    "provider_id": "openai",
                    "model_id": "gpt-5.3-codex",
                    "raw_model_ref": "openai/gpt-5.3-codex",
                    "canonical_model_ref": "paid_openai_compatible/gpt-5.3-codex",
                    "protocol": "openai_chat",
                    "status": "disabled",
                    "source": "alias",
                    "context_limit": 8192,
                    "reasoning_support": "effort",
                    "executable": False,
                    "blocked_reasons": ["provider_disabled"],
                },
                {
                    "provider_id": "local",
                    "model_id": "qwen3-coder",
                    "raw_model_ref": "local/qwen3-coder",
                    "canonical_model_ref": "local_openai_compatible/qwen3-coder:30b",
                    "protocol": "openai_chat",
                    "status": "active",
                    "source": "alias",
                    "context_limit": 32768,
                    "reasoning_support": "unknown",
                    "executable": True,
                    "blocked_reasons": [],
                },
            ],
            "active_model": {
                "session_id": "session_1",
                "raw_model_ref": "codex/gpt-5.5",
                "canonical_model_ref": "codex_cli/gpt-5.5",
                "alias_used": "codex/gpt-5.5",
                "protocol": "codex_cli",
                "known_catalog_entry": True,
                "executable": True,
                "provider_enabled": True,
                "blocked_reasons": [],
            },
        }
    }

    pane_rows = _model_catalog_pane_rows(dashboard)
    dialog = render_model_selection_dialog(dashboard)

    assert any("Canonical: codex_cli/gpt-5.5" in row for row in pane_rows)
    assert any("Protocol: codex_cli" in row for row in pane_rows)
    assert any("exec=False" in row for row in pane_rows)
    assert "gpt-5.5  ctx 8.2k" in dialog
    assert "reason effort" in dialog
    assert "blocked: provider_disabled" in dialog
    assert "endpoint=http://localhost:11434/v1" not in dialog
    assert "Model" in dialog
    assert "Ref:" in dialog
    assert "codex_cli/gpt-5.5" in dialog
    assert "Protocol:" in dialog
    assert "Limits:" in dialog
    assert "Reasoning:" in dialog
    assert "Tools:" in dialog
    assert "Boundary:" in dialog
    assert "Blocked:" in dialog
    assert "Variants:" not in dialog
    assert "Modalities:" not in dialog
    assert "Cost:" not in dialog
    assert "[bold steel_blue1]F5[/bold steel_blue1] favorite" in dialog
    assert "[bold steel_blue1]F6[/bold steel_blue1] default" in dialog
    assert "[bold steel_blue1]F7[/bold steel_blue1] inspect" in dialog
    assert "[bold steel_blue1]Ctrl+A[/bold steel_blue1] account" in dialog
    assert "[bold steel_blue1]F10[/bold steel_blue1] disconnect" in dialog
    assert "Connect provider" not in dialog


def test_model_picker_fuzzy_search_details_and_virtualized_rows() -> None:
    models = [
        {
            "provider_id": "anthropic",
            "model_id": f"claude-sonnet-{index}-with-a-very-long-display-name",
            "raw_model_ref": f"anthropic/claude-sonnet-{index}",
            "canonical_model_ref": f"anthropic/claude-sonnet-{index}",
            "protocol": "anthropic_messages",
            "status": "active",
            "source": "backend_config",
            "context_limit": 200000,
            "max_output_tokens": 8192,
            "modalities": ["text", "image"],
            "tool_support": True,
            "reasoning_support": "tokens",
            "cost": {"input_per_1m": 3.0, "output_per_1m": 15.0},
            "executable": True,
            "blocked_reasons": [],
        }
        for index in range(30)
    ]
    dashboard = {
        "model_catalog": {
            "providers": [
                {
                    "provider_id": "anthropic",
                    "display_name": "Anthropic",
                    "enabled": True,
                    "credential_status": "configured",
                    "data_boundary": "hosted_provider",
                }
            ],
            "models": models,
            "active_model": {},
        }
    }

    rendered = render_model_selection_dialog(dashboard, query="anthropic sonnet 25", selected_index=0, width=60)

    assert "claude-sonnet-25" in rendered
    assert "Model" in rendered
    assert "Limits:" in rendered
    assert "Tools:" in rendered
    assert "Max output:" not in rendered
    assert "Modalities:" not in rendered
    assert "Cost:" not in rendered
    assert "harness models validate anthropic/claude" not in rendered
    assert "claude-sonnet-2-with-a-very-long-display-name" not in rendered


def test_model_picker_sections_current_favorites_recent_connected_local_hosted_blocked() -> None:
    dashboard = {
        "model_catalog": {
            "providers": [
                {
                    "provider_id": "active",
                    "display_name": "Active Provider",
                    "enabled": True,
                    "credential_status": "configured",
                    "data_boundary": "hosted_provider",
                },
                {
                    "provider_id": "favorite",
                    "display_name": "Favorite Provider",
                    "enabled": True,
                    "credential_status": "configured",
                    "data_boundary": "hosted_provider",
                },
                {
                    "provider_id": "recent",
                    "display_name": "Recent Provider",
                    "enabled": True,
                    "credential_status": "configured",
                    "data_boundary": "hosted_provider",
                },
                {
                    "provider_id": "connected",
                    "display_name": "Connected Provider",
                    "enabled": True,
                    "credential_status": "configured",
                    "data_boundary": "hosted_provider",
                },
                {
                    "provider_id": "local",
                    "display_name": "Local Provider",
                    "enabled": True,
                    "credential_status": "configured",
                    "data_boundary": "local_only",
                },
                {
                    "provider_id": "hosted",
                    "display_name": "Hosted Provider",
                    "enabled": True,
                    "credential_status": "missing",
                    "data_boundary": "hosted_provider",
                },
                {
                    "provider_id": "blocked",
                    "display_name": "Blocked Provider",
                    "enabled": False,
                    "credential_status": "missing",
                    "data_boundary": "hosted_provider",
                },
            ],
            "models": [
                {
                    "provider_id": "hosted",
                    "model_id": "plain-hosted",
                    "raw_model_ref": "hosted/plain-hosted",
                    "context_limit": 8192,
                    "max_output_tokens": 2048,
                    "reasoning_support": "unknown",
                    "executable": True,
                    "blocked_reasons": [],
                },
                {
                    "provider_id": "blocked",
                    "model_id": "blocked-model",
                    "raw_model_ref": "blocked/blocked-model",
                    "context_limit": 8192,
                    "max_output_tokens": 2048,
                    "reasoning_support": "unknown",
                    "executable": False,
                    "blocked_reasons": ["provider_disabled"],
                },
                {
                    "provider_id": "local",
                    "model_id": "local-model",
                    "raw_model_ref": "local/local-model",
                    "context_limit": 8192,
                    "max_output_tokens": 2048,
                    "reasoning_support": "unknown",
                    "executable": True,
                    "blocked_reasons": [],
                },
                {
                    "provider_id": "connected",
                    "model_id": "connected-model",
                    "raw_model_ref": "connected/connected-model",
                    "context_limit": 8192,
                    "max_output_tokens": 2048,
                    "reasoning_support": "unknown",
                    "executable": True,
                    "blocked_reasons": [],
                },
                {
                    "provider_id": "recent",
                    "model_id": "recent-model",
                    "raw_model_ref": "recent/recent-model",
                    "context_limit": 8192,
                    "max_output_tokens": 2048,
                    "reasoning_support": "unknown",
                    "executable": True,
                    "blocked_reasons": [],
                    "selection_count": 3,
                    "last_selected_at": "2026-05-23T10:00:00Z",
                },
                {
                    "provider_id": "favorite",
                    "model_id": "favorite-model",
                    "raw_model_ref": "favorite/favorite-model",
                    "context_limit": 8192,
                    "max_output_tokens": 2048,
                    "reasoning_support": "unknown",
                    "executable": True,
                    "blocked_reasons": [],
                    "favorite": True,
                },
                {
                    "provider_id": "active",
                    "model_id": "active-model",
                    "raw_model_ref": "active/active-model",
                    "context_limit": 8192,
                    "max_output_tokens": 2048,
                    "reasoning_support": "unknown",
                    "executable": True,
                    "blocked_reasons": [],
                },
            ],
            "active_model": {"raw_model_ref": "active/active-model", "provider_id": "active"},
        }
    }

    entries = _model_selection_dialog_entries(dashboard)

    assert [entry["raw_model_ref"] for entry in entries] == [
        "active/active-model",
        "favorite/favorite-model",
        "recent/recent-model",
        "connected/connected-model",
        "local/local-model",
        "hosted/plain-hosted",
        "blocked/blocked-model",
    ]
    assert [entry["_picker_section_title"] for entry in entries] == [
        "Current session model",
        "Favorites",
        "Recents",
        "Connected providers",
        "Local providers",
        "Hosted providers",
        "Disabled or blocked providers",
    ]
    rendered = render_model_selection_dialog(dashboard, selected_index=0, width=90)
    assert rendered.index("Current session model") < rendered.index("Favorites")
    assert rendered.index("Favorites") < rendered.index("Recents")
    assert rendered.index("Recents") < rendered.index("Connected providers")
    assert rendered.index("Connected providers") < rendered.index("Local providers")
    assert rendered.index("Local providers") < rendered.index("Hosted providers")
    assert rendered.index("Hosted providers") < rendered.index("Disabled or blocked providers")


def test_model_picker_shows_provider_connect_action_for_missing_credentials() -> None:
    dashboard = {
        "model_catalog": {
            "providers": [
                {
                    "provider_id": "anthropic",
                    "display_name": "Anthropic",
                    "enabled": True,
                    "credential_status": "missing",
                    "data_boundary": "hosted_provider",
                    "methods": [
                        {
                            "method": "env",
                            "supported": True,
                            "default_env_var": "ANTHROPIC_API_KEY",
                        }
                    ],
                }
            ],
            "models": [
                {
                    "provider_id": "anthropic",
                    "model_id": "claude-sonnet-4",
                    "raw_model_ref": "anthropic/claude-sonnet-4",
                    "canonical_model_ref": "anthropic/claude-sonnet-4",
                    "protocol": "anthropic_messages",
                    "status": "active",
                    "source": "backend_config",
                    "context_limit": 200000,
                    "max_output_tokens": 8192,
                    "reasoning_support": "tokens",
                    "executable": False,
                    "blocked_reasons": ["credential_missing"],
                }
            ],
            "active_model": {},
        }
    }

    rendered = render_model_selection_dialog(dashboard, selected_index=0, width=72)

    assert "connect credentials" in rendered
    assert "Model" in rendered
    assert "Boundary:" in rendered
    assert "[bold steel_blue1]Ctrl+A[/bold steel_blue1] account" in rendered
    assert "[bold steel_blue1]F9[/bold steel_blue1] connect" not in rendered


def test_model_picker_does_not_render_unavailable_provider_shortcuts() -> None:
    dashboard = {
        "model_catalog": {
            "providers": [
                {
                    "provider_id": "static_catalog",
                    "display_name": "Static Catalog",
                    "enabled": True,
                    "connected": False,
                    "credential_status": "missing",
                    "auth_methods": [],
                    "refresh_supported": False,
                    "refresh_status": "unsupported",
                    "data_boundary": "hosted_provider",
                }
            ],
            "models": [
                {
                    "provider_id": "static_catalog",
                    "model_id": "preview-model",
                    "raw_model_ref": "static_catalog/preview-model",
                    "canonical_model_ref": "static_catalog/preview-model",
                    "protocol": "openai_chat",
                    "status": "active",
                    "source": "static_catalog",
                    "context_limit": 8192,
                    "max_output_tokens": 1024,
                    "reasoning_support": "unknown",
                    "executable": False,
                    "blocked_reasons": ["credential_missing"],
                    "refresh_supported": False,
                }
            ],
            "active_model": {},
        }
    }

    rendered = render_model_selection_dialog(dashboard, selected_index=0, width=88)

    assert "[bold steel_blue1]F5[/bold steel_blue1] favorite" in rendered
    assert "[bold steel_blue1]F6[/bold steel_blue1] default" in rendered
    assert "[bold steel_blue1]F7[/bold steel_blue1] inspect" in rendered
    assert "[bold steel_blue1]F8[/bold steel_blue1] refresh" not in rendered
    assert "[bold steel_blue1]Ctrl+A[/bold steel_blue1] account" not in rendered
    assert "[bold steel_blue1]F9[/bold steel_blue1] connect" not in rendered
    assert "[bold steel_blue1]F10[/bold steel_blue1] disconnect" not in rendered


def test_model_picker_provider_detail_panel_shows_management_state() -> None:
    dashboard = {
        "model_catalog": {
            "providers": [
                {
                    "provider_id": "anthropic",
                    "display_name": "Anthropic",
                    "enabled": True,
                    "connected": False,
                    "credential_status": "missing",
                    "credential_source": "env",
                    "auth_methods": ["env:ANTHROPIC_API_KEY", "oauth"],
                    "model_count": 2,
                    "available_model_count": 1,
                    "refresh_supported": True,
                    "refresh_status": "stale",
                    "data_boundary": "hosted_provider",
                    "methods": [
                        {
                            "method": "env",
                            "supported": True,
                            "default_env_var": "ANTHROPIC_API_KEY",
                        }
                    ],
                }
            ],
            "models": [
                {
                    "provider_id": "anthropic",
                    "model_id": "claude-sonnet-4",
                    "raw_model_ref": "anthropic/claude-sonnet-4",
                    "canonical_model_ref": "anthropic/claude-sonnet-4",
                    "protocol": "anthropic_messages",
                    "source": "backend_config",
                    "context_limit": 200000,
                    "max_output_tokens": 8192,
                    "reasoning_support": "tokens",
                    "executable": False,
                    "blocked_reasons": ["credential_missing"],
                    "cache_status": "stale",
                    "refresh_supported": True,
                },
                {
                    "provider_id": "anthropic",
                    "model_id": "claude-haiku",
                    "raw_model_ref": "anthropic/claude-haiku",
                    "canonical_model_ref": "anthropic/claude-haiku",
                    "protocol": "anthropic_messages",
                    "source": "backend_config",
                    "context_limit": 200000,
                    "max_output_tokens": 8192,
                    "reasoning_support": "tokens",
                    "available_model": True,
                    "executable": True,
                    "blocked_reasons": [],
                    "cache_status": "stale",
                    "refresh_supported": True,
                },
            ],
            "active_model": {},
        }
    }

    rendered = render_model_selection_dialog(dashboard, selected_index=0, width=88)

    assert "Provider" in rendered
    assert "Provider:" in rendered
    assert "Anthropic (anthropic)" in rendered
    assert "Status:" in rendered
    assert "connected=no, credentials=missing, enabled=yes" in rendered
    assert "Auth:" in rendered
    assert "env:ANTHROPIC_API_KEY, oauth" in rendered
    assert "Models:" in rendered
    assert "1/2 available" in rendered
    assert "Refresh:" in rendered
    assert "stale" in rendered
    assert "Connect:" not in rendered
    assert "[bold steel_blue1]Ctrl+A[/bold steel_blue1] account" in rendered
    assert "Disconnect:" not in rendered


def test_model_picker_virtualizes_large_unfiltered_catalog() -> None:
    dashboard = {
        "model_catalog": {
            "providers": [{"provider_id": "local", "enabled": True, "credential_status": "configured", "data_boundary": "local_only"}],
            "models": [
                {
                    "provider_id": "local",
                    "model_id": f"model-{index}",
                    "raw_model_ref": f"local/model-{index}",
                    "context_limit": 4096,
                    "max_output_tokens": 1024,
                    "reasoning_support": "unknown",
                    "executable": True,
                    "blocked_reasons": [],
                }
                for index in range(40)
            ],
            "active_model": {},
        }
    }

    rendered = render_model_selection_dialog(dashboard, selected_index=25)

    assert "Showing" in rendered
    assert "model-25" in rendered
    assert "model-0  ctx" not in rendered


def test_model_dialog_reuses_cached_dashboard_when_moving_selection(tmp_path) -> None:
    write_default_config(tmp_path)
    store = SQLiteStore.open_initialized(tmp_path)
    session = store.create_session(title="Model dialog")

    class CountingService:
        def __init__(self) -> None:
            self.calls = 0

        def dashboard(self, *, selected_session_id=None):
            self.calls += 1
            return build_tui_dashboard(tmp_path, selected_session_id=selected_session_id)

        def runtime_status(self, session_id: str):
            return {"phase": "idle"}

    service = CountingService()
    dashboard = service.dashboard(selected_session_id=session.id)

    # Bind the production method shape closely enough to prove dialog movement no longer calls service.dashboard.
    from harness.tui import create_harness_app

    real_app = create_harness_app(tmp_path, app_service=service)
    real_app._selected_session_id = session.id
    real_app._chat_state.session_id = session.id
    real_app._dashboard_cache = dashboard
    real_app._dashboard_cache_session_id = session.id
    import time

    real_app._dashboard_cache_at = time.monotonic()
    calls_before_snapshot = service.calls
    rendered = render_model_selection_dialog(real_app._dashboard_snapshot(), selected_index=1)

    assert "Select model" in rendered
    assert service.calls == calls_before_snapshot


def test_tui_right_panel_context_tracks_window_without_runtime_timer() -> None:
    dashboard = {
        "project_root": "/tmp/project",
        "branch": "main",
        "initialized": True,
        "summary": {"tasks_total": 0, "active_leases": 0, "recent_runs": 0},
        "task_status_counts": {},
        "active_session": {
            "id": "session_1",
            "title": "Runtime session",
            "display_title": "Runtime session",
            "composer_context": {"total_estimated_tokens": 1024, "attachment_count": 0},
            "runtime": {
                "phase": "running",
                "active_prompt_id": "prompt_abc",
                "active_elapsed_seconds": 9,
            },
        },
        "model_catalog": {
            "active_model": {
                "raw_model_ref": "codex_cli/gpt-test",
                "context_limit": 4096,
            }
        },
        "live_activity": {"active_signal": "running", "counts": {}},
    }

    model = build_right_panel_model(dashboard, {"active_section_id": "context"}, "", "dashboard")
    rendered = render_right_panel(model)

    assert "Context window: 25% used" in rendered
    assert "tokens" not in rendered
    assert "Prompt runtime:" not in rendered


def test_tui_runtime_elapsed_prefers_started_at_for_live_ticking() -> None:
    started_at = (datetime.now(timezone.utc) - timedelta(seconds=2)).isoformat()

    elapsed = _runtime_elapsed_seconds({"active_started_at": started_at, "active_elapsed_seconds": 99})

    assert elapsed is not None
    assert 1 <= elapsed < 99


def test_codex_mode_model_repairs_missing_session_schema_without_raw_sqlite(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("DROP TABLE IF EXISTS sessions")

    model = build_codex_mode_model(tmp_path)

    assert model["ok"] is True
    assert "no such table" not in json.dumps(model).lower()
    assert SQLiteStore(tmp_path).inspect_required_session_schema()["ok"] is True


def test_tui_chat_response_renderer_hides_raw_payloads_unless_debug() -> None:
    response = {
        "title": "Assistant",
        "lines": ["Done."],
        "tool_results": [{"tool": "grep", "ok": True, "content": "{\"type\":\"harness.tool_result/v1\"}"}],
        "event_sequence": ["operator.turn.started", "tool_call.output"],
        "debug": {"exception_type": "ValueError", "traceback": "Traceback (most recent call last): ..."},
    }

    normal = _chat_response_to_tui_message(response)
    debug = _chat_response_to_tui_message(response, debug=True)

    assert "harness.tool_result/v1" not in json.dumps(normal)
    assert "Traceback" not in json.dumps(normal)
    debug_json = json.dumps(debug)
    assert "operator.turn.started" in debug_json
    assert "exception: ValueError" in debug_json
    assert "traceback captured" in debug_json

    approval = _chat_response_to_tui_message(
        {
            "kind": "session_tool_permission_required",
            "title": "Permission Required",
            "lines": [],
            "approval_card": {
                "approval_id": "perm_123",
                "tool_id": "shell",
                "cwd": ".",
                "command": "pytest -q",
            },
        }
    )
    assert "approval: perm_123" in approval["lines"]
    assert "command: pytest -q" in approval["lines"]


def test_codex_like_transcript_matches_prompt_entered_layout() -> None:
    rendered = render_codex_like_transcript(
        [
            {"role": "assistant", "title": "Harness chat", "lines": ["Project: /tmp/project"]},
            {"role": "user", "title": "first arbitrary prompt", "lines": []},
            {"role": "assistant", "title": "Assistant", "lines": ["First arbitrary response."]},
            {"role": "user", "title": "arbitrary request", "lines": []},
            {
                "role": "assistant",
                "title": "Assistant Streaming",
                "lines": [
                    "Turn started",
                    "Ran intent routing",
                    "- intent: unsupported",
                    "Explored",
                    "- Context: 2 selected blocks, 1 pinned, 1 retrieved",
                    "- Sources: harness policy, repo tree",
                    "- Budget: 120 / 1,000 tokens",
                    "- Warnings: approximate_token_budget_only",
                ],
            },
        ],
        working_seconds=3,
    )

    assert "[bold]Tip:[/bold] GPT-5.5 is now available in Codex." in rendered
    assert "[on #eeeeee][dim]›[/dim] first arbitrary prompt[/]" in rendered
    assert "\nFirst arbitrary response.\n" in rendered
    assert "[on #eeeeee][dim]›[/dim] arbitrary request[/]" in rendered
    assert "[green]●[/green] [bold]Ran[/bold] [dim]intent[/dim] [dim]routing[/dim]" in rendered
    assert "[dim]•[/dim] [bold]Explored[/bold]" in rendered
    assert "[dark_cyan]Context:[/dark_cyan] [dim]2 selected blocks, 1 pinned, 1 retrieved[/dim]" in rendered
    assert "[dark_cyan]Sources:[/dark_cyan] [dim]harness policy, repo tree[/dim]" in rendered
    assert "[dark_cyan]Budget:[/dark_cyan] [dim]120 / 1,000 tokens[/dim]" in rendered
    assert "[dark_cyan]Warnings:[/dark_cyan] [dim]approximate_token_budget_only[/dim]" in rendered
    assert "[dim]○[/dim] [dim]Working (3s • esc to interrupt)[/dim]" in rendered
    assert "Harness chat" not in rendered
    assert "[cyan]" not in rendered


def test_codex_like_streaming_content_accumulates_like_codex_prose() -> None:
    lines: list[str] = []
    lines = _append_streaming_content(lines, "First")
    lines = _append_streaming_content(lines, " streamed")
    lines = _append_streaming_content(lines, " sentence.\nSecond sentence.")
    lines.append("Ran model turn")
    lines = _append_streaming_content(lines, "Final")
    lines = _append_streaming_content(lines, " streamed answer.")

    rendered = render_codex_like_transcript(
        [
            {"role": "user", "title": "arbitrary request", "lines": []},
            {"role": "assistant", "title": "Assistant Streaming", "lines": lines},
        ],
        working_seconds=9,
    )

    assert "\nFirst streamed sentence.\n" in rendered
    assert "\nSecond sentence.\n" in rendered
    assert "[green]●[/green] [bold]Ran[/bold] [dim]model[/dim] [dim]turn[/dim]" in rendered
    assert "[dim]────────────────" in rendered
    assert "\nFinal streamed answer." in rendered
    assert "First\n•  streamed" not in rendered


def test_codex_like_streaming_content_does_not_append_after_turn_completed() -> None:
    lines = ["Turn completed"]
    lines = _append_streaming_content(lines, "This repo is Agent Harness.")

    rendered = render_codex_like_transcript(
        [{"role": "assistant", "title": "Assistant Streaming", "lines": lines}]
    )

    assert lines == ["Turn completed", "This repo is Agent Harness."]
    assert "Turn completedThis repo" not in rendered
    assert "Turn completed\n\nThis repo is [bold]Agent Harness[/bold]." in rendered


def test_codex_like_transcript_escapes_literal_rich_closing_tags() -> None:
    rendered = render_codex_like_transcript(
        [
            {"role": "user", "title": "trigger markup error", "lines": []},
            {
                "role": "assistant",
                "title": "Chat Error",
                "lines": [
                    "closing tag '[/cyan]' does not match any open tag",
                    "closing tag '[/bold]' does not match any open tag",
                ],
            },
        ]
    )

    Console().render_str(rendered)
    assert r"\[/cyan]" in rendered
    assert r"\[/bold]" in rendered


def test_markup_static_update_falls_back_to_plain_text_on_bad_markup() -> None:
    class FakeWidget:
        content = None

        def update(self, content) -> None:
            self.content = content

    widget = FakeWidget()

    _update_markup_static(widget, "closing tag [/bold] does not match any open tag")

    assert "closing tag [/bold] does not match any open tag" in str(widget.content)


def test_markup_static_update_parses_valid_rich_markup() -> None:
    from rich.text import Text

    class FakeWidget:
        content = None

        def update(self, content) -> None:
            self.content = content

    widget = FakeWidget()

    _update_markup_static(widget, "[bold]Tip:[/bold] hello")

    assert isinstance(widget.content, Text)
    assert str(widget.content) == "Tip: hello"
    assert widget.content.spans


def test_codex_like_finish_keeps_live_steps_before_final_answer() -> None:
    merged = _merge_codex_stream_and_final_lines(
        [
            "Turn started",
            "Ran intent routing",
            "- intent: unsupported",
            "Explored",
            "- Context: 2 selected blocks, 1 pinned, 1 retrieved",
        ],
        [
            "Final answer line one.",
            "Final answer line two.",
        ],
    )
    rendered = render_codex_like_transcript(
        [
            {"role": "user", "title": "arbitrary request", "lines": []},
            {"role": "assistant", "title": "Assistant Streaming", "lines": merged},
        ]
    )

    assert "\nTurn started\n" in rendered
    assert "[green]●[/green] [bold]Ran[/bold] [dim]intent[/dim] [dim]routing[/dim]" in rendered
    assert "[dim]•[/dim] [bold]Explored[/bold]" in rendered
    assert "[dim]────────────────" in rendered
    assert "\nFinal answer line one.\n" in rendered


def test_codex_like_self_managed_action_keeps_progress_before_done_title() -> None:
    final = _chat_response_to_tui_message(
        {
            "kind": "self_managed_local_action",
            "title": "Done",
            "lines": ["Created `simple_script.py`.", "Run: run_123"],
        }
    )
    merged = _merge_codex_stream_and_final_lines(["Turn started"], final["lines"])
    rendered = render_codex_like_transcript(
        [{"role": "assistant", "title": final["title"], "lines": merged}],
        separator_width=24,
    )

    assert final["title"] == "Assistant"
    assert rendered.index("Turn started") < rendered.index("Done")
    assert rendered.index("Done") < rendered.index("Created")


def test_transcript_events_without_seq_use_timestamp_order() -> None:
    messages = _transcript_event_messages(
        [
            {
                "id": "event_z",
                "kind": "model.message_delta",
                "occurred_at": "2026-05-26T12:00:02+00:00",
                "payload": {"prompt_id": "prompt_1", "delta": "Second."},
            },
            {
                "id": "event_a",
                "kind": "model.message_delta",
                "occurred_at": "2026-05-26T12:00:01+00:00",
                "payload": {"prompt_id": "prompt_1", "delta": "First."},
            },
        ],
        [],
    )

    assert messages[0]["lines"] == ["First.", "Second."]


def test_codex_like_transcript_uses_connectors_only_for_procedure_children() -> None:
    rendered = render_codex_like_transcript(
        [
            {
                "role": "assistant",
                "title": "Assistant Streaming",
                "lines": [
                    "Ran pwd",
                    "- /Users/oscarxue/Documents/harness",
                    "Explored",
                    "- Read README.md",
                    "This repo is Agent Harness.",
                    "- CLI/TUI surface",
                ],
            },
        ],
        separator_width=24,
    )

    assert "[green]●[/green] [bold]Ran[/bold] [dim]pwd[/dim]" in rendered
    assert "[dim]└[/dim] [dim][dark_cyan]/Users/oscarxue/Documents/harness[/dark_cyan][/dim]" in rendered
    assert "[dim]└[/dim] [dark_cyan]Read[/dark_cyan] [bold]README[/bold].md" in rendered
    assert "[dim]────────────────────────[/dim]" in rendered
    assert "\nThis repo is [bold]Agent Harness[/bold].\n" in rendered
    assert "\nThis repo is [bold]Agent Harness[/bold].\n\n[dim]•[/dim]" in rendered
    assert "[dim]•[/dim] CLI[dark_cyan]/TUI[/dark_cyan] surface" in rendered
    assert "[dim]└[/dim] [dark_cyan]CLI" not in rendered


def test_codex_like_transcript_renders_reasoning_between_tool_calls() -> None:
    rendered = render_codex_like_transcript(
        [
            {
                "role": "assistant",
                "title": "Assistant Streaming",
                "lines": [
                    "Ran model turn",
                    "Reasoning: I need to inspect the README before answering.",
                    "Ran read_file",
                    "- read_file: ok",
                    "Ran model turn",
                    "Reasoning: The README gives enough project context.",
                    "Final answer.",
                ],
            },
        ],
        separator_width=24,
    )

    assert "[green]●[/green] [bold]Ran[/bold] [dim]model[/dim] [dim]turn[/dim]" in rendered
    assert "[dim]•[/dim] [dim]I need to inspect the [bold]README[/bold] before answering.[/dim]" in rendered
    assert "[green]●[/green] [bold]Ran[/bold] [dim]read_file[/dim]" in rendered
    assert "[dim]└[/dim] [dark_cyan]read_file:[/dark_cyan] [dim]ok[/dim]" in rendered
    assert "[dim]•[/dim] [dim]The [bold]README[/bold] gives enough project context.[/dim]" in rendered
    assert "[dim]────────────────────────[/dim]\nFinal answer." in rendered


def test_codex_like_transcript_spaces_and_emphasizes_prose_paragraphs() -> None:
    rendered = render_codex_like_transcript(
        [
            {
                "role": "assistant",
                "title": "Assistant",
                "lines": [
                    "This repo is Agent Harness.",
                    "In plain terms: it provides a supervised control plane.",
                    "Key files:",
                    "- README overview",
                    "- CLI entrypoint",
                ],
            },
        ]
    )

    assert "This repo is [bold]Agent Harness[/bold].\n\n[bold]In plain terms:[/bold]" in rendered
    assert "[bold]supervised control plane[/bold]" in rendered
    assert "[bold]Key files:[/bold]\n[dim]•[/dim] [bold]README[/bold] overview" in rendered
    assert "[bold]README[/bold] overview\n[dim]•[/dim] [bold]CLI[/bold] entrypoint" in rendered


def test_codex_like_app_keeps_transcript_and_context_together(tmp_path) -> None:
    async def run_pilot() -> None:
        app = create_harness_app(tmp_path, codex_like=True)
        async with app.run_test(size=(120, 40)):
            chat = app.query_one("#chat-content")
            side = app.query_one("#pane-container")
            status = app.query_one("#search-status")

            assert "Tip: GPT-5.5 is now available in Codex." in str(chat.content)
            assert "[bold]Tip:[/bold]" not in str(chat.content)
            assert "Harness chat" not in str(chat.content)
            assert "Assistant" in str(side.content)
            assert "Mode: live" in str(side.content)
            assert "Harness" in str(status.content)
            assert "Q 0R/0A/0B" in str(status.content)

    asyncio.run(run_pilot())
