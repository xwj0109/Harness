import json
import asyncio
import sqlite3
import subprocess
import tomllib
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from harness.approvals import ApprovalStore
from harness.backends.codex_cli import CodexRunResult
from harness.chat import ChatSessionState, handle_chat_input, route_chat_intent
from harness.config import default_config
from harness.models import BackendStatus, BillingMode, DataBoundary, EventStreamType, ExecutionLocation, SessionStatus
from harness.cli.main import SUPPORTED_EXECUTION_TASK_METADATA, app
from harness.memory.sqlite_store import SQLiteStore
from harness.operator_context import build_session_pane_projection
from harness.tui import (
    THEME_DIALOG_ENTRIES,
    activate_command_palette_entry,
    build_focused_tui_view_model,
    build_tui_dashboard,
    build_tui_panes,
    build_tui_view_model,
    build_chat_welcome_message,
    build_command_palette,
    build_command_palette_panes,
    build_functionality_table,
    build_right_panel_model,
    create_harness_app,
    build_slash_commands,
    build_tui_settings_catalog,
    handle_slash_command,
    filter_command_palette,
    filter_functionality_table,
    filter_slash_commands,
    filter_tui_panes,
    render_chat_message,
    render_dashboard_text,
    render_filter_status,
    render_model_selection_dialog,
    render_functionality_table_dialog,
    render_palette_status,
    render_right_panel,
    render_right_panel_detail,
    render_right_panel_status,
    render_slash_command_suggestions,
    render_view_status,
    _render_session_rail,
)
from harness.command_catalog import build_command_catalog


runner = CliRunner()


def test_pyproject_exposes_harness_console_script() -> None:
    with open("pyproject.toml", "rb") as handle:
        pyproject = tomllib.load(handle)
    assert pyproject["project"]["scripts"]["harness"] == "harness.cli.main:app"


def test_cli_init_idempotent_and_backends(tmp_path) -> None:
    result1 = runner.invoke(app, ["init", "--project", str(tmp_path)])
    result2 = runner.invoke(app, ["init", "--project", str(tmp_path)])
    assert result1.exit_code == 0
    assert result2.exit_code == 0
    assert (tmp_path / ".harness" / "config.yaml").exists()
    assert (tmp_path / ".harness" / "harness.sqlite").exists()
    assert (tmp_path / ".harness" / "runs").exists()
    assert (tmp_path / ".harness" / "tmp").exists()
    assert (tmp_path / ".harness" / "approvals.yaml").exists()
    gitignore = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert gitignore.count("# Harness local artifacts") == 1
    assert gitignore.count(".harness/runs/") == 1
    assert gitignore.count(".harness/harness.sqlite") == 1
    assert gitignore.count(".harness/approvals.yaml") == 1
    assert gitignore.count(".harness/tmp/") == 1
    assert gitignore.count("*.egg-info/") == 1

    backends = runner.invoke(app, ["backends", "--project", str(tmp_path)])
    assert backends.exit_code == 0
    assert "codex_cli" in backends.output
    assert "local_openai_compatible" in backends.output


def test_cli_init_preserves_gitignore_and_does_not_duplicate_partial_entries(tmp_path) -> None:
    (tmp_path / ".gitignore").write_text("existing.log\n.harness/runs/\n", encoding="utf-8")
    result = runner.invoke(app, ["init", "--project", str(tmp_path)])
    assert result.exit_code == 0
    gitignore = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert "existing.log" in gitignore
    assert gitignore.count(".harness/runs/") == 1
    assert gitignore.count(".harness/harness.sqlite") == 1
    assert gitignore.count(".harness/approvals.yaml") == 1
    assert gitignore.count(".harness/tmp/") == 1
    assert gitignore.count("*.egg-info/") == 1


def test_cli_home_reports_uninitialized_project_without_mutation(tmp_path) -> None:
    result = runner.invoke(app, ["home", "--project", str(tmp_path), "--output", "json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema_version"] == "harness.home/v1"
    assert payload["ok"] is True
    assert payload["initialized"] is False
    assert payload["summary"]["tasks_total"] == 0
    assert payload["recommended_actions"][0]["id"] == "initialize_project"
    assert not (tmp_path / ".harness").exists()


def test_cli_home_text_output_is_sectioned_and_non_mutating(tmp_path) -> None:
    result = runner.invoke(app, ["home", "--project", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "Harness Home" in result.output
    assert "\nProject\n" in result.output
    assert "\nNext Actions\n" in result.output
    assert "harness init --project" in result.output
    assert "\nSafety\n" in result.output
    assert not (tmp_path / ".harness").exists()


def test_cli_autonomy_policy_inspect_json_is_read_only(tmp_path) -> None:
    result = runner.invoke(
        app,
        ["autonomy", "policy", "inspect", "--project", str(tmp_path), "--profile", "safe-local", "--output", "json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema_version"] == "harness.autonomy_policy_inspect/v1"
    assert payload["ok"] is True
    assert payload["policy"]["id"] == "safe-local"
    assert "daemon-safe" in payload["available_profiles"]
    assert not (tmp_path / ".harness").exists()


def test_cli_plain_autonomous_auto_executes_allowed_action_contract(tmp_path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0

    class FakeChatModel:
        def complete(self, _messages, _context):
            from harness.chat_model import ChatResponse

            return ChatResponse(
                content='{"type":"harness.tool_request/v1","tool":"create_objective","arguments":{"title":"CLI autonomous objective"}}'
            )

    monkeypatch.setattr("harness.chat.build_default_chat_model", lambda _project_root: FakeChatModel())

    result = runner.invoke(app, ["--project", str(tmp_path), "--plain", "--autonomous"], input="create objective\n/quit\n")

    assert result.exit_code == 0, result.output
    assert "Autonomy: safe-local" in result.output
    assert "Autonomy profile safe-local auto-approved this action contract." in result.output
    assert "Objective Created" in result.output
    objectives = SQLiteStore(tmp_path).list_objectives()
    assert [objective.title for objective in objectives] == ["CLI autonomous objective"]
    assert (tmp_path / ".harness" / "autonomy" / "approvals.jsonl").exists()


def test_cli_act_runs_read_only_loop_with_json_evidence(tmp_path, monkeypatch) -> None:
    (tmp_path / "README.md").write_text("# CLI Act\n", encoding="utf-8")

    class FakeChatModel:
        def __init__(self) -> None:
            self.calls = 0

        def complete(self, _messages, _context):
            from harness.chat_model import ChatResponse

            self.calls += 1
            if self.calls == 1:
                return ChatResponse(
                    content='{"type":"harness.tool_request/v1","tool":"read_file","arguments":{"path":"README.md"}}'
                )
            return ChatResponse(content="Evidence from README: CLI Act.")

    monkeypatch.setattr("harness.chat.build_default_chat_model", lambda _project_root: FakeChatModel())

    result = runner.invoke(
        app,
        ["act", "summarize this repo", "--project", str(tmp_path), "--autonomy", "safe-local", "--output", "json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema_version"] == "harness.autonomous_read_loop/v1"
    assert payload["ok"] is True
    assert payload["stop_reason"] == "final_answer"
    assert payload["tool_results"] == [{"tool": "read_file", "ok": True, "error_type": None}]
    assert Path(payload["evidence_path"]).exists()


def test_cli_act_can_create_and_run_local_task_graph(tmp_path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0

    class FakeChatModel:
        def __init__(self) -> None:
            self.calls = 0

        def complete(self, _messages, _context):
            from harness.chat_model import ChatResponse

            self.calls += 1
            if self.calls == 1:
                return ChatResponse(
                    content=json.dumps(
                        {
                            "type": "harness.tool_request/v1",
                            "tool": "create_task_graph",
                            "arguments": {
                                "goal": "CLI act graph",
                                "tasks": [
                                    {
                                        "title": "CLI act dry run",
                                        "execution_adapter": "dry_run",
                                        "task_type": "phase_1a_test",
                                    }
                                ],
                            },
                        }
                    )
                )
            return ChatResponse(content="Created and ran the local task graph.")

    monkeypatch.setattr("harness.chat.build_default_chat_model", lambda _project_root: FakeChatModel())

    result = runner.invoke(
        app,
        ["act", "create and run a local graph", "--project", str(tmp_path), "--autonomy", "safe-local", "--output", "json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema_version"] == "harness.autonomous_read_loop/v1"
    assert payload["ok"] is True
    assert [item["tool"] for item in payload["tool_results"]] == [
        "create_task_graph",
        "create_task_graph",
        "objectives.run",
    ]
    assert payload["tool_results"][1]["kind"] == "action_contract_executed"
    assert payload["tool_results"][2]["stop_reason"] == "objective_succeeded"
    store = SQLiteStore(tmp_path)
    assert [task.status.value for task in store.list_tasks()] == ["succeeded"]
    assert len(store.list_runs()) == 1


def test_cli_root_launches_unified_app(tmp_path, monkeypatch) -> None:
    launched = {}

    def fake_run(project_root):
        launched["project_root"] = str(project_root)

    monkeypatch.setattr("harness.cli.main._run_unified_app", fake_run)

    result = runner.invoke(app, ["--project", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert launched["project_root"] == str(tmp_path)
    assert not (tmp_path / ".harness").exists()


def test_cli_root_plain_runs_line_chat(tmp_path) -> None:
    result = runner.invoke(app, ["--project", str(tmp_path), "--plain"], input="/orchestrators\n/quit\n")

    assert result.exit_code == 0, result.output
    assert "Harness chat" in result.output
    assert "Orchestrators" in result.output
    assert not (tmp_path / ".harness").exists()


def test_cli_root_codex_like_plain_starts_action_mode(tmp_path) -> None:
    result = runner.invoke(app, ["--project", str(tmp_path), "--plain", "--codex-like"], input="/mode\n/quit\n")

    assert result.exit_code == 0, result.output
    assert "Mode: codex-like" in result.output
    assert "Current mode: codex-like" in result.output
    assert not (tmp_path / ".harness").exists()


def test_cli_root_missing_textual_returns_repair_hint_without_mutation(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("harness.cli.main._has_textual", lambda: False)

    result = runner.invoke(app, ["--project", str(tmp_path)])

    assert result.exit_code == 1
    assert "Textual is not installed." in result.output
    assert "Install Harness with its default dependencies" in result.output
    assert not (tmp_path / ".harness").exists()


def test_cli_root_help_keeps_chat_and_tui_as_hidden_aliases() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0, result.output
    assert "Local-first agent harness" in result.output
    assert "│ chat" not in result.output
    assert "│ tui " not in result.output
    assert "daemon" in result.output
    assert "tasks" in result.output


def test_cli_chat_json_reports_context_without_backend_preflight(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "harness.cli.main.CodexCliBackend",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("chat context must not preflight Codex")),
    )
    monkeypatch.setattr(
        "harness.cli.main.LocalOpenAICompatibleBackend",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("chat context must not preflight local backend")),
    )
    monkeypatch.setattr(
        "harness.cli.main.DockerImageManager",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("chat context must not touch Docker")),
    )

    result = runner.invoke(app, ["--project", str(tmp_path), "--output", "json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema_version"] == "harness.chat/v1"
    assert payload["ok"] is True
    assert payload["initialized"] is False
    assert {adapter["id"] for adapter in payload["registered_adapters"]} >= {
        "dry_run",
        "read_only_summary",
        "codex_isolated_edit",
    }
    assert not (tmp_path / ".harness").exists()


def test_cli_chat_json_context_sanitizes_task_and_memory_secrets(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    store.create_task(
        title="OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz",
        metadata={"secret": "Bearer abcdefghijklmnop"},
    )
    store.save_memory_note(
        scope_type="project",
        scope_id="default",
        summary="password: correcthorsebatterystaple",
    )

    result = runner.invoke(app, ["--project", str(tmp_path), "--output", "json"])

    assert result.exit_code == 0, result.output
    serialized = result.output
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in serialized
    assert "abcdefghijklmnop" not in serialized
    assert "correcthorsebatterystaple" not in serialized
    assert "[REDACTED_SECRET]" in serialized


def test_chat_slash_commands_and_plain_text_model_unavailable_without_mutation(tmp_path, monkeypatch) -> None:
    def fail_backend(*args, **kwargs):
        raise AssertionError("chat guidance must not construct a backend")

    def unavailable_chat_model(*args, **kwargs):
        from harness.backends.local_openai import LocalEndpointUnavailable

        raise LocalEndpointUnavailable("test chat backend unavailable")

    monkeypatch.setattr("harness.cli.main.CodexCliBackend", fail_backend)
    monkeypatch.setattr("harness.chat.build_default_chat_model", unavailable_chat_model)
    help_response = handle_chat_input("/help", tmp_path, ChatSessionState())
    plain_response = handle_chat_input("hello model", tmp_path, ChatSessionState())

    assert help_response["schema_version"] == "harness.chat_response/v1"
    assert help_response["ok"] is True
    assert "/tasks" in "\n".join(help_response["lines"])
    assert plain_response["ok"] is False
    assert plain_response["kind"] == "chat_model_unavailable"
    assert "does not fall back to paid hosted chat automatically" in "\n".join(plain_response["lines"])
    assert not (tmp_path / ".harness").exists()


def test_cli_chat_interactive_smoke_exits_without_mutation(tmp_path) -> None:
    result = runner.invoke(app, ["--project", str(tmp_path), "--plain"], input="/help\nshow tasks\n/quit\n")

    assert result.exit_code == 0, result.output
    assert "Harness chat" in result.output
    assert "Commands" in result.output
    assert "Goodbye" in result.output
    assert not (tmp_path / ".harness").exists()


def test_cli_chat_plain_text_replies_with_model_answer_without_mutation(tmp_path, monkeypatch) -> None:
    class FakeChatModel:
        def complete(self, _messages, _context):
            from harness.chat_model import ChatResponse

            return ChatResponse(content="I can inspect local Harness state and prepare explicit actions.")

    monkeypatch.setattr("harness.chat.build_default_chat_model", lambda _project_root: FakeChatModel())

    result = runner.invoke(app, ["--project", str(tmp_path), "--plain"], input="hello\n/quit\n")

    assert result.exit_code == 0, result.output
    assert "Harness: Assistant" in result.output
    assert "I can inspect local Harness state and prepare explicit actions." in result.output
    assert "Intent Routing Disabled" not in result.output
    assert not (tmp_path / ".harness").exists()


def test_cli_chat_handles_incomplete_sqlite_without_traceback(tmp_path) -> None:
    harness_dir = tmp_path / ".harness"
    harness_dir.mkdir()
    sqlite3.connect(harness_dir / "harness.sqlite").close()

    json_result = runner.invoke(app, ["--project", str(tmp_path), "--output", "json"])
    text_result = runner.invoke(app, ["--project", str(tmp_path), "--plain"], input="/tasks\n/quit\n")

    assert json_result.exit_code == 0, json_result.output
    payload = json.loads(json_result.output)
    assert payload["schema_version"] == "harness.chat/v1"
    assert payload["initialized"] is False
    assert text_result.exit_code == 0, text_result.output
    assert "Project Not Initialized" in text_result.output
    assert "Traceback" not in text_result.output


def test_chat_init_command_initializes_project_in_app(tmp_path) -> None:
    state = ChatSessionState()

    initialized = handle_chat_input("/init", tmp_path, state)
    initialized_again = handle_chat_input("initialize this project", tmp_path, state)

    assert initialized["kind"] == "project_initialized"
    assert initialized["ok"] is True
    assert initialized["already_initialized"] is False
    assert initialized_again["kind"] == "project_initialized"
    assert initialized_again["already_initialized"] is True
    assert (tmp_path / ".harness" / "config.yaml").exists()
    assert (tmp_path / ".harness" / "harness.sqlite").exists()
    assert (tmp_path / ".harness" / "approvals.yaml").exists()
    assert (tmp_path / ".gitignore").read_text(encoding="utf-8").count(".harness/harness.sqlite") == 1


def test_chat_read_only_intent_routing() -> None:
    assert route_chat_intent("show tasks")["intent"] == "show_tasks"
    assert route_chat_intent("what adapters are available?")["intent"] == "show_adapters"
    assert route_chat_intent("what is blocked?")["intent"] == "show_blocked"
    assert route_chat_intent("why is this blocked?")["intent"] == "show_blocked"
    assert route_chat_intent("security blockers")["intent"] == "show_blocked"
    assert route_chat_intent("what should I do next?")["intent"] == "recommend_next"
    assert route_chat_intent("what is the current project state?")["intent"] == "show_status"
    assert route_chat_intent("summarize this repo")["intent"] == "repo_summary"
    assert route_chat_intent("plan how to improve the CLI")["intent"] == "repo_planning"
    assert route_chat_intent("fix the failing test with codex")["intent"] == "coding_fix"
    assert route_chat_intent("initialize this project")["intent"] == "init_project"


def test_chat_model_path_emits_codex_like_progress_for_arbitrary_prompt(tmp_path) -> None:
    (tmp_path / "README.md").write_text("# Sample Project\n\nLocal-first workflows.\n", encoding="utf-8")

    class FakeChatModel:
        def stream(self, _messages, _context):
            from harness.chat_model import ChatDelta

            yield ChatDelta(content="model answer", kind="content")

    progress: list[dict] = []

    response = handle_chat_input(
        "an arbitrary prompt with no special routing",
        tmp_path,
        ChatSessionState(),
        chat_model=FakeChatModel(),
        progress_callback=progress.append,
    )

    contents = [item["content"] for item in progress]
    assert response["kind"] == "llm_chat"
    assert response["ok"] is True
    assert contents[:3] == ["Turn started", "Ran intent routing", "- intent: unsupported"]
    assert "Explored" in contents
    assert any(item.startswith("- Project:") for item in contents)
    assert any(item.startswith("- Context:") for item in contents)
    assert any(item.startswith("- Sources:") for item in contents)
    assert any(item.startswith("- Budget:") for item in contents)
    assert "context_summary" in response["context_manifest"]
    assert response["context_manifest"]["context_summary"]["selected_block_count"] == len(
        response["context_manifest"]["blocks"]
    )
    assert "Ran model turn" in contents
    assert "model answer" in contents
    assert response["lines"] == ["model answer"]


def test_chat_next_recommendation_uses_local_state(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    task = store.create_task(title="Ready task", metadata={"execution_adapter": "dry_run", "task_type": "phase_1a_test"})
    state = ChatSessionState()

    response = handle_chat_input("what should I do next?", tmp_path, state)

    assert response["kind"] == "next_recommendation"
    assert response["ok"] is True
    assert response["recommendation"]["id"] == "lease_ready_task"
    assert task.id in "\n".join(response["lines"])
    assert "harness daemon run-once" in "\n".join(response["lines"])


def test_chat_next_recommendation_surfaces_repo_planning_and_adapters(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    task = store.create_task(
        title="Plan repo change",
        metadata={"execution_adapter": "repo_planning", "task_type": "repo_planning"},
    )
    state = ChatSessionState()

    recommendation = handle_chat_input("what should I do next?", tmp_path, state)
    adapters = handle_chat_input("/adapters", tmp_path, state)

    assert recommendation["kind"] == "next_recommendation"
    assert recommendation["recommendation"]["id"] == "lease_repo_planning_task"
    assert task.id in "\n".join(recommendation["lines"])
    assert "harness daemon execute <lease_id> --project . --output json" in "\n".join(recommendation["lines"])
    assert adapters["kind"] == "adapters"
    assert "repo_planning: task_types=repo_planning" in "\n".join(adapters["lines"])


def test_cli_tui_json_probe_does_not_launch_or_mutate(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("harness.cli.main._has_textual", lambda: True)

    result = runner.invoke(app, ["tui", "--project", str(tmp_path), "--output", "json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema_version"] == "harness.tui/v1"
    assert payload["ok"] is True
    assert payload["project_root"] == str(tmp_path)
    assert payload["mode"] == "unified_app"
    assert payload["launched"] is False
    assert not (tmp_path / ".harness").exists()


def test_cli_tui_home_set_image_generates_static_art(tmp_path, monkeypatch) -> None:
    image_module = pytest.importorskip("PIL.Image")
    monkeypatch.chdir(tmp_path)
    image_path = tmp_path / "home.png"
    image = image_module.new("RGB", (10, 6), color=(240, 180, 120))
    image.save(image_path)

    result = runner.invoke(
        app,
        ["tui-home", "set-image", str(image_path), "--width", "24", "--output", "json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema_version"] == "harness.tui_home_image/v1"
    assert payload["ok"] is True
    assert payload["width"] == 24
    assert payload["stored_source"] == "assets/tui/home_source.png"
    assert payload["generated_module"] == "src/harness/tui_assets/pixel_art.py"
    assert (tmp_path / "assets" / "tui" / "home_source.png").exists()
    assert (tmp_path / "src" / "harness" / "tui_assets" / "pixel_art.py").exists()
    assert not (tmp_path / ".harness").exists()


def test_cli_tui_home_set_image_rejects_forbidden_path(tmp_path) -> None:
    forbidden = tmp_path / ".env.home.png"
    forbidden.write_text("not an image", encoding="utf-8")

    result = runner.invoke(
        app,
        ["tui-home", "set-image", str(forbidden), "--output", "json"],
    )

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload == {
        "schema_version": "harness.tui_home_image/v1",
        "ok": False,
        "errors": ["TUI home image path is forbidden by harness safety policy."],
    }
    assert not (tmp_path / ".harness").exists()


def test_tui_dashboard_reports_uninitialized_project_without_mutation(tmp_path) -> None:
    dashboard = build_tui_dashboard(tmp_path)
    panes = build_tui_panes(dashboard)
    rendered = render_dashboard_text(dashboard)

    assert dashboard["schema_version"] == "harness.tui_dashboard/v1"
    assert dashboard["ok"] is True
    assert dashboard["initialized"] is False
    assert dashboard["summary"]["tasks_total"] == 0
    assert "pixel_art" not in dashboard
    assert dashboard["agents"] == []
    assert dashboard["tasks"] == []
    assert dashboard["active_leases"] == []
    assert dashboard["daemon"]["latest_events"] == []
    assert dashboard["model_catalog"]["source"] == "default_config"
    assert [provider["provider_id"] for provider in dashboard["model_catalog"]["providers"]] == [
        "codex_cli",
        "local_openai_compatible",
        "paid_openai_compatible",
    ]
    assert any(model["raw_model_ref"] == "codex_cli/gpt-5.5" for model in dashboard["model_catalog"]["models"])
    model_dialog = render_model_selection_dialog(dashboard)
    assert "No models match." not in model_dialog
    assert "codex_cli" in model_dialog
    assert "gpt-5.5" in model_dialog
    assert dashboard["guidance"][0]["id"] == "initialize_project"
    assert [pane["id"] for pane in panes] == [
        "overview",
        "agents",
        "tasks",
        "leases",
        "daemon",
        "runs",
        "settings",
        "sessions",
        "models",
        "commands",
        "guidance",
        "safety",
    ]
    assert "Project" in rendered
    assert "Initialized: False" in rendered
    assert "Commands" in rendered
    assert "Providers: 3" in rendered
    assert "codex_cli/gpt-5.5" in rendered
    assert "Safety" in rendered
    assert "no_hidden_execution" in rendered
    assert not (tmp_path / ".harness").exists()


def test_tui_dashboard_reports_stale_project_database_without_traceback(tmp_path) -> None:
    harness_dir = tmp_path / ".harness"
    harness_dir.mkdir()
    with sqlite3.connect(harness_dir / "harness.sqlite") as conn:
        conn.execute("CREATE TABLE runs (id TEXT PRIMARY KEY)")

    dashboard = build_tui_dashboard(tmp_path)
    panes = build_tui_panes(dashboard)
    rendered = render_dashboard_text(dashboard)
    home = runner.invoke(app, ["home", "--project", str(tmp_path), "--output", "json"])

    assert dashboard["ok"] is True
    assert dashboard["initialized"] is False
    assert dashboard["state_error"]["type"] == "OperationalError"
    assert dashboard["model_catalog"]["source"] == "default_config"
    assert any(provider["provider_id"] == "codex_cli" for provider in dashboard["model_catalog"]["providers"])
    assert any(model["raw_model_ref"] == "codex_cli/gpt-5.5" for model in dashboard["model_catalog"]["models"])
    assert dashboard["guidance"] == [
        {
            "id": "repair_project_state",
            "command": f"harness init --project {tmp_path}",
            "description": "Repair or migrate local harness persistence for this project.",
        }
    ]
    assert any("repair_project_state" in line for pane in panes for line in pane["lines"])
    assert "State error: OperationalError" in rendered
    assert home.exit_code == 0, home.output
    payload = json.loads(home.output)
    assert payload["initialized"] is False
    assert payload["state_error"]["type"] == "OperationalError"
    assert payload["recommended_actions"][0]["id"] == "repair_project_state"


def test_tui_dashboard_reports_initialized_project_state(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    bundle_path = tmp_path / "tui_agent_bundle"
    scaffold = runner.invoke(
        app,
        [
            "agents",
            "scaffold",
            "tui_agent",
            "--workbench",
            "quant",
            "--kind",
            "specialist",
            "--parent",
            "quant_research",
            "--model-profile",
            "local_reasoning",
            "--tool-policy",
            "read_only",
            "--memory-scope",
            "quant",
            "--output",
            str(bundle_path),
            "--output-format",
            "json",
        ],
    )
    assert scaffold.exit_code == 0, scaffold.output
    imported = runner.invoke(app, ["agents", "import", str(bundle_path), "--project", str(tmp_path), "--output", "json"])
    assert imported.exit_code == 0, imported.output
    task = runner.invoke(
        app,
        [
            "tasks",
            "add",
            "--title",
            "tui task",
            "--agent",
            "tui_agent",
            "--workbench",
            "quant",
            "--execution-adapter",
            "read_only_summary",
            "--task-type",
            "read_only_repo_summary",
            "--project",
            str(tmp_path),
            "--output",
            "json",
        ],
    )
    assert task.exit_code == 0, task.output
    tick = runner.invoke(app, ["daemon", "run-once", "--project", str(tmp_path), "--output", "json"])
    assert tick.exit_code == 0, tick.output
    created_run = runner.invoke(
        app,
        [
            "dev",
            "create-run",
            "--goal",
            "tui dashboard run",
            "--task-type",
            "phase_1a_test",
            "--project",
            str(tmp_path),
        ],
    )
    assert created_run.exit_code == 0, created_run.output
    store = SQLiteStore(tmp_path)
    session = store.create_session(
        title="TUI session",
        agent_id="tui_agent",
        raw_model_ref="codex/gpt-test",
        ui_preferences={"theme": "dark", "terminal_font_size": 18, "keybinding_preset": "opencode-like"},
    )
    message = store.append_session_message(session.id, "user", "Inspect the active session")
    store.append_session_part(session.id, message.id, "text", text="Inspect the active session")
    store.append_store_event(
        EventStreamType.SESSION,
        session.id,
        "tui.ui_activation.applied",
        {
            "source": "slash",
            "entry_id": "ui_controls.settings",
            "activation_kind": "ui_action",
            "action": {"type": "focus_section", "section_id": "settings"},
            "ui_action_applied": True,
            "command_started": False,
            "process_started": False,
            "filesystem_modified": False,
            "permission_granting": False,
            "authority_granting": False,
        },
        session_id=session.id,
    )

    dashboard = build_tui_dashboard(tmp_path)
    panes = build_tui_panes(dashboard)
    rendered = render_dashboard_text(dashboard)

    assert dashboard["initialized"] is True
    assert dashboard["summary"]["imported_agents"] == 1
    assert dashboard["summary"]["tasks_total"] == 1
    assert dashboard["summary"]["active_leases"] == 1
    assert dashboard["summary"]["recent_runs"] == 1
    assert dashboard["summary"]["recent_sessions"] == 1
    assert dashboard["agents"][0]["agent_id"] == "tui_agent"
    assert dashboard["agents"][0]["workbench_id"] == "quant"
    assert dashboard["tasks"][0]["title"] == "tui task"
    assert dashboard["tasks"][0]["agent_id"] == "tui_agent"
    assert dashboard["tasks"][0]["workbench_id"] == "quant"
    assert dashboard["tasks"][0]["execution_adapter"] == "read_only_summary"
    assert dashboard["tasks"][0]["task_type"] == "read_only_repo_summary"
    assert dashboard["active_leases"][0]["task_id"] == dashboard["tasks"][0]["id"]
    assert dashboard["active_leases"][0]["status"] == "active"
    assert dashboard["daemon"]["latest_events"]
    assert dashboard["task_status_counts"]["leased"] == 1
    assert dashboard["recent_runs"][0]["task_type"] == "phase_1a_test"
    assert dashboard["recent_sessions"][0]["id"] == session.id
    assert dashboard["recent_sessions"][0]["title"] == "TUI session"
    assert dashboard["recent_sessions"][0]["agent_id"] == "tui_agent"
    assert dashboard["recent_sessions"][0]["ui_preferences"]["theme"] == "dark"
    assert dashboard["model_catalog"]["no_hidden_fallback"] is True
    assert dashboard["model_catalog"]["active_model"]["raw_model_ref"] == "codex/gpt-test"
    assert dashboard["model_catalog"]["active_model"]["known_catalog_entry"] is False
    assert dashboard["model_catalog"]["active_model"]["executable"] is False
    assert dashboard["model_catalog"]["active_model"]["provider_known"] is False
    assert dashboard["model_catalog"]["active_model"]["provider_enabled"] is False
    assert dashboard["model_catalog"]["active_model"]["blocked_reasons"] == ["provider_unknown", "model_unknown"]
    assert dashboard["model_catalog"]["active_model"]["provider_execution_started"] is False
    assert dashboard["model_catalog"]["active_model"]["model_execution_started"] is False
    assert dashboard["model_catalog"]["active_model"]["network_accessed"] is False
    assert dashboard["model_catalog"]["active_model"]["hidden_provider_fallback"] is False
    assert dashboard["model_catalog"]["active_model"]["hidden_model_fallback"] is False
    assert dashboard["model_catalog"]["active_model"]["permission_granting"] is False
    assert dashboard["model_catalog"]["active_model"]["authority_granting"] is False
    assert any(model["raw_model_ref"] == "codex_cli/gpt-5.5" for model in dashboard["model_catalog"]["models"])
    assert dashboard["active_session"]["id"] == session.id
    assert dashboard["active_session"]["raw_model_ref"] == "codex/gpt-test"
    assert dashboard["active_session"]["ui_preferences"]["theme"] == "dark"
    assert dashboard["active_session"]["latest_ui_activation"]["entry_id"] == "ui_controls.settings"
    assert dashboard["active_session"]["latest_ui_activation"]["process_started"] is False
    assert any("Message appended" in line for line in dashboard["active_session"]["timeline"])
    assert any("UI action applied" in line for line in dashboard["active_session"]["timeline"])
    assert any("Inspect the active session" in line for line in dashboard["active_session"]["transcript"])
    assert [pane["id"] for pane in panes] == [
        "overview",
        "agents",
        "tasks",
        "leases",
        "daemon",
        "runs",
        "settings",
        "sessions",
        "models",
        "commands",
        "guidance",
        "safety",
    ]
    settings_pane = next(pane for pane in panes if pane["id"] == "settings")
    settings_text = "\n".join(settings_pane["lines"])
    assert "Source: active session preferences" in settings_text
    assert f"Session: {session.id}" in settings_text
    assert "Policy: tui_settings_read_only" in settings_text
    assert "Evidence: read_only_settings_metadata" in settings_text
    assert "theme=dark" in settings_text
    assert "terminal_font_size=18" in settings_text
    assert "keybinding_preset=opencode-like" in settings_text
    assert "composer_mode=multiline" in settings_text
    assert "Preferences persisted: False" in settings_text
    assert "Backend settings exposed: False" in settings_text
    assert f"Persist command: harness session preferences {session.id} --project . --set key=value" in settings_text
    sessions_pane = next(pane for pane in panes if pane["id"] == "sessions")
    sessions_text = "\n".join(sessions_pane["lines"])
    assert "Latest UI action: ui_controls.settings action=focus_section source=slash" in sessions_text
    assert "UI flags: command=False process=False filesystem=False permission=False authority=False" in sessions_text
    assert any("tui_agent" in line for line in panes[1]["lines"])
    assert any("tui task" in line for line in panes[2]["lines"])
    assert any(dashboard["active_leases"][0]["id"] in line for line in panes[3]["lines"])
    assert "tui_agent workbench=quant" in rendered
    assert "tui task" in rendered
    assert "Active Leases" in rendered
    assert "Daemon" in rendered
    assert "Tasks: 1" in rendered
    assert "Active leases: 1" in rendered
    assert "Recent Runs" in rendered
    assert "Recent Sessions" in rendered
    assert "TUI session" in rendered
    assert "Models" in rendered
    assert "No hidden fallback: True" in rendered
    assert "known=False executable=False" in rendered
    assert f"Switch: harness session model {session.id} <provider/model> --project ." in rendered
    assert "Blocked: provider_unknown, model_unknown" in rendered
    assert "Hidden fallback: provider=False model=False" in rendered
    assert "codex_cli/gpt-5.5" in rendered
    assert "Timeline:" in rendered
    assert "Transcript:" in rendered
    assert "Inspect the active session" in rendered
    right_panel = render_right_panel(build_right_panel_model(dashboard, {"palette": build_command_palette()}, "", "dashboard"))
    assert "Latest:" in right_panel
    assert "Last message: user " in right_panel
    assert "Known:" not in right_panel
    assert "Executable:" not in right_panel
    assert "Blocked: provider unknown, model unknown" in right_panel
    assert "Fallback: explicit failure only" in right_panel
    assert "harness daemon status --project" in rendered
    serialized = json.dumps(dashboard)
    assert "api_key" not in serialized
    assert "OPENAI_API_KEY" not in serialized
    assert "base_url" not in serialized


def test_tui_model_selection_palette_projects_configured_models(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    session = store.create_session(title="Model picker", raw_model_ref="codex_cli/gpt-5.5")
    dashboard = build_tui_dashboard(tmp_path)
    palette = build_command_palette(model_catalog=dashboard["model_catalog"])
    filtered = filter_command_palette(palette, "select model")

    model_entries = [entry for entry in filtered["entries"] if entry["group_id"] == "model_selection"]
    assert model_entries
    assert any(entry["model_ref"] == "codex_cli/gpt-5.5" for entry in model_entries)
    local_entry = next(entry for entry in model_entries if entry["model_ref"].startswith("local_openai_compatible/"))
    local_model_ref = local_entry["model_ref"]
    assert local_entry["activation"]["kind"] == "session_model_selection"
    assert local_entry["activation"]["policy_boundary"]["provider_call_allowed"] is False
    assert local_entry["activation"]["policy_boundary"]["model_execution_allowed"] is False
    assert local_entry["activation"]["policy_boundary"]["session_metadata_mutation_allowed"] is True
    assert local_entry["activation"]["process_started"] is False
    assert local_entry["activation"]["permission_granting"] is False

    activation = activate_command_palette_entry(
        palette,
        local_entry["id"],
        {"active_section_index": 0, "collapsed_section_ids": set()},
    )
    assert activation["ok"] is True
    assert activation["activation_kind"] == "session_model_selection"
    assert activation["session_model_selection_requested"] is True
    assert activation["action"]["raw_model_ref"] == local_model_ref
    assert activation["provider_started"] is False
    assert activation["process_started"] is False
    assert activation["filesystem_modified"] is False
    assert activation["permission_granting"] is False
    assert store.get_session(session.id).raw_model_ref == "codex_cli/gpt-5.5"


def test_tui_model_picker_persists_session_model_without_provider_execution(tmp_path) -> None:
    pytest.importorskip("textual")
    from textual.widgets import Static, TextArea

    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    session = store.create_session(title="Model picker", agent_id="plan", raw_model_ref="codex_cli/gpt-5.5")
    local_model_ref = next(
        model["raw_model_ref"]
        for model in build_tui_dashboard(tmp_path)["model_catalog"]["models"]
        if model["raw_model_ref"].startswith("local_openai_compatible/")
    )

    async def run_pilot() -> None:
        app = create_harness_app(tmp_path)
        async with app.run_test(size=(130, 44)) as pilot:
            prompt = app.query_one("#prompt", TextArea)
            composer_status = app.query_one("#composer-status", Static)
            composer_footer = app.query_one("#composer-footer", Static)
            initial_messages = len(app._messages)

            assert "Model codex_cli/gpt-5.5" in str(composer_status.content)
            assert "Agent plan" in str(composer_status.content)
            assert f"Session: {session.id}" not in str(composer_status.content)
            assert "Enter send · Shift+Enter newline · Ctrl+X M models · / commands · ? shortcuts" in str(
                composer_footer.content
            )

            await pilot.press("ctrl+p")
            for char in "qwen":
                await pilot.press(char)
            await pilot.pause()
            assert app._focus_mode == "palette"
            await pilot.press("enter")
            await pilot.pause()

            assert prompt.value == ""
            assert len(app._messages) == initial_messages
            assert app._request_in_flight is False
            activation = app._latest_palette_activation
            assert activation["activation_kind"] == "session_model_selection"
            assert activation["raw_model_ref"] == local_model_ref
            assert activation["session_model_selected"] is True
            assert activation["model_validation"]["executable"] is True
            assert activation["harness_state_modified"] is True
            assert activation["session_event_persisted"] is True
            assert activation["command_started"] is False
            assert activation["provider_started"] is False
            assert activation["model_execution_started"] is False
            assert activation["process_started"] is False
            assert activation["filesystem_modified"] is False
            assert activation["permission_granting"] is False
            assert activation["authority_granting"] is False

    asyncio.run(run_pilot())

    updated = SQLiteStore(tmp_path).get_session(session.id)
    assert updated.raw_model_ref == local_model_ref
    assert updated.provider_id == "local_openai_compatible"
    assert updated.model_id
    events = SQLiteStore(tmp_path).list_session_store_events(session.id)
    selected_events = [event for event in events if event.kind == "session.model_selected"]
    validation_events = [event for event in events if event.kind == "session.model_validation"]
    assert selected_events[-1].payload["raw_model_ref"] == local_model_ref
    assert validation_events[-1].payload["source"] == "tui_model_picker"
    assert validation_events[-1].payload["provider_execution_started"] is False
    assert validation_events[-1].payload["model_execution_started"] is False
    assert validation_events[-1].payload["hidden_model_fallback"] is False
    assert validation_events[-1].payload["permission_granting"] is False


def test_tui_model_slash_command_persists_session_model_without_palette_shortcut(tmp_path) -> None:
    pytest.importorskip("textual")
    from textual.widgets import TextArea

    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    session = store.create_session(title="Model slash", agent_id="plan", raw_model_ref="codex_cli/gpt-5.5")
    local_model_ref = next(
        model["raw_model_ref"]
        for model in build_tui_dashboard(tmp_path)["model_catalog"]["models"]
        if model["raw_model_ref"].startswith("local_openai_compatible/")
    )

    async def run_pilot() -> None:
        app = create_harness_app(tmp_path)
        async with app.run_test(size=(130, 44)) as pilot:
            prompt = app.query_one("#prompt", TextArea)
            initial_messages = len(app._messages)

            prompt.value = "/model qwen"
            await pilot.press("ctrl+enter")
            await pilot.pause()

            assert prompt.value == ""
            assert len(app._messages) == initial_messages
            assert app._request_in_flight is False
            activation = app._latest_palette_activation
            assert activation["activation_kind"] == "session_model_selection"
            assert activation["source"] == "slash"
            assert activation["slash"] == "/model"
            assert activation["raw_model_ref"] == local_model_ref
            assert activation["session_model_selected"] is True
            assert activation["model_validation"]["executable"] is True
            assert activation["command_started"] is False
            assert activation["provider_started"] is False
            assert activation["model_execution_started"] is False
            assert activation["process_started"] is False
            assert activation["filesystem_modified"] is False
            assert activation["permission_granting"] is False
            assert activation["authority_granting"] is False

    asyncio.run(run_pilot())

    updated = SQLiteStore(tmp_path).get_session(session.id)
    assert updated.raw_model_ref == local_model_ref
    validation_events = [
        event
        for event in SQLiteStore(tmp_path).list_session_store_events(session.id)
        if event.kind == "session.model_validation"
    ]
    assert validation_events[-1].payload["source"] == "tui_model_picker"
    assert validation_events[-1].payload["provider_execution_started"] is False
    assert validation_events[-1].payload["permission_granting"] is False


def test_tui_models_slash_lists_numbered_models_and_selects_by_number(tmp_path) -> None:
    pytest.importorskip("textual")
    from textual.widgets import Static, TextArea

    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    session = store.create_session(title="Model numbered", agent_id="plan", raw_model_ref="codex_cli/gpt-5.5")
    unique_refs = []
    for model in build_tui_dashboard(tmp_path)["model_catalog"]["models"]:
        if model["raw_model_ref"] not in unique_refs:
            unique_refs.append(model["raw_model_ref"])
    assert len(unique_refs) >= 2
    selected_ref = unique_refs[1]

    async def run_pilot() -> None:
        app = create_harness_app(tmp_path)
        async with app.run_test(size=(130, 44)) as pilot:
            prompt = app.query_one("#prompt", TextArea)
            composer_status = app.query_one("#composer-status", Static)
            slash_status = app.query_one("#slash-status", Static)
            dialog = app.query_one("#dialog-panel", Static)
            initial_messages = len(app._messages)

            assert "Model codex_cli/gpt-5.5" in str(composer_status.content)
            assert "Agent plan" in str(composer_status.content)
            assert "Select: /model <number|name>" not in str(composer_status.content)

            prompt.value = "/model"
            await pilot.press("ctrl+enter")
            await pilot.pause()
            assert prompt.value == "/model "
            assert len(app._messages) == initial_messages
            assert "Select model" in str(dialog.content)
            assert "Search" in str(dialog.content)
            assert app._latest_palette_activation["activation_kind"] == "model_picker_help"
            assert app._latest_palette_activation["provider_started"] is False
            assert app._latest_palette_activation["process_started"] is False

            prompt.value = "/models"
            await pilot.press("ctrl+enter")
            await pilot.pause()
            assert prompt.value == ""
            assert len(app._messages) == initial_messages + 1
            assert app._messages[-1]["title"] == "Model Selection"
            assert any(f"2. " in line and selected_ref in line for line in app._messages[-1]["lines"])
            assert "Select model" in str(dialog.content)
            assert "Recent" in str(dialog.content)
            assert selected_ref.split("/", 1)[-1] in str(dialog.content)
            assert "/model <number>" in str(dialog.content)
            assert app._latest_palette_activation["activation_kind"] == "model_list"
            assert app._latest_palette_activation["provider_started"] is False
            assert app._latest_palette_activation["process_started"] is False

            prompt.value = "/model 2"
            await pilot.press("ctrl+enter")
            await pilot.pause()
            activation = app._latest_palette_activation
            assert activation["activation_kind"] == "session_model_selection"
            assert activation["raw_model_ref"] == selected_ref
            assert activation["session_model_selected"] is True
            assert activation["provider_started"] is False
            assert activation["process_started"] is False
            assert activation["permission_granting"] is False
            assert str(dialog.content) == ""

    asyncio.run(run_pilot())

    assert SQLiteStore(tmp_path).get_session(session.id).raw_model_ref == selected_ref


def test_tui_opencode_leader_key_lists_models_without_palette_focus(tmp_path) -> None:
    pytest.importorskip("textual")
    from textual.containers import Container
    from textual.widgets import Static, TextArea

    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    SQLiteStore(tmp_path).create_session(title="Leader models", agent_id="plan", raw_model_ref="codex_cli/gpt-5.5")

    async def run_pilot() -> None:
        app = create_harness_app(tmp_path)
        async with app.run_test(size=(130, 44)) as pilot:
            prompt = app.query_one("#prompt", TextArea)
            slash_status = app.query_one("#slash-status", Static)
            overlay = app.query_one("#dialog-overlay", Container)
            dialog = app.query_one("#dialog-panel", Static)
            initial_messages = len(app._messages)

            await pilot.press("ctrl+x")
            await pilot.pause()
            assert app.leader_key_active is True
            assert "Leader: m Models, s Sessions." in str(slash_status.content)
            assert app._focus_mode == "dashboard"
            assert "Commands" in str(dialog.content)
            assert "Suggested" in str(dialog.content)
            assert "Switch model" in str(dialog.content)
            assert "ctrl+x m" in str(dialog.content)
            assert overlay.size.width >= 120
            assert dialog.size.width < overlay.size.width
            assert dialog.size.width <= 84

            await pilot.press("down")
            await pilot.pause()
            assert app._dialog_selected_index == 1
            assert "Continue session" in str(dialog.content)
            await pilot.press("up")
            await pilot.pause()
            assert app._dialog_selected_index == 0

            await pilot.press("m")
            await pilot.pause()
            assert app.leader_key_active is False
            assert app._focus_mode == "dashboard"
            assert prompt.value == ""
            assert len(app._messages) == initial_messages + 1
            assert app._messages[-1]["title"] == "Model Selection"
            assert any("codex_cli/gpt-5.5" in line for line in app._messages[-1]["lines"])
            assert "Select model" in str(dialog.content)
            assert "Recent" in str(dialog.content)
            assert "gpt-5.5" in str(dialog.content)
            assert "codex_cli" in str(dialog.content)
            assert "Connect provider" in str(dialog.content)
            assert app._latest_palette_activation["activation_kind"] == "model_list"
            assert app._latest_palette_activation["source"] == "leader"
            assert app._latest_palette_activation["slash"] == "ctrl+x m"
            assert app._latest_palette_activation["provider_started"] is False
            assert app._latest_palette_activation["process_started"] is False
            assert app._latest_palette_activation["filesystem_modified"] is False
            assert app._latest_palette_activation["permission_granting"] is False

    asyncio.run(run_pilot())


def test_tui_command_table_enter_opens_selected_functionality(tmp_path) -> None:
    pytest.importorskip("textual")
    from textual.widgets import Static, TextArea

    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    SQLiteStore(tmp_path).create_session(title="Command table", agent_id="plan", raw_model_ref="codex_cli/gpt-5.5")

    async def run_pilot() -> None:
        app = create_harness_app(tmp_path)
        async with app.run_test(size=(130, 44)) as pilot:
            prompt = app.query_one("#prompt", TextArea)
            dialog = app.query_one("#dialog-panel", Static)
            initial_messages = len(app._messages)

            await pilot.press("ctrl+x")
            await pilot.pause()
            assert app._dialog_kind == "commands"
            assert "Authority" in str(dialog.content)

            await pilot.press("enter")
            await pilot.pause()
            assert app._dialog_kind == "models"
            assert prompt.value == "/model "
            assert len(app._messages) == initial_messages
            assert "Select model" in str(dialog.content)
            assert "gpt-5.5" in str(dialog.content)

    asyncio.run(run_pilot())


def test_tui_command_table_search_activates_safe_ui_function(tmp_path) -> None:
    pytest.importorskip("textual")
    from textual.widgets import Static, TextArea

    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    SQLiteStore(tmp_path).create_session(title="Command search", agent_id="plan", raw_model_ref="codex_cli/gpt-5.5")

    async def run_pilot() -> None:
        app = create_harness_app(tmp_path)
        async with app.run_test(size=(130, 44)) as pilot:
            prompt = app.query_one("#prompt", TextArea)
            dialog = app.query_one("#dialog-panel", Static)

            await pilot.press("ctrl+p")
            for char in "settings":
                await pilot.press(char)
            await pilot.pause()

            assert app.leader_key_active is False
            assert app._focus_mode == "palette"
            assert prompt.value == "settings"

            await pilot.press("enter")
            await pilot.pause()
            assert prompt.value == ""
            assert str(dialog.content) == ""
            assert app._latest_palette_activation["entry_id"] == "ui_controls.settings"
            assert app._latest_palette_activation["activation_kind"] == "ui_action"
            assert app._latest_palette_activation["process_started"] is False
            assert app._latest_palette_activation["filesystem_modified"] is False
            assert app._latest_palette_activation["permission_granting"] is False

    asyncio.run(run_pilot())


def test_tui_theme_switching_is_safe_ui_only(tmp_path) -> None:
    pytest.importorskip("textual")
    from textual.widgets import Static, TextArea

    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    SQLiteStore(tmp_path).create_session(title="Theme switch", agent_id="plan", raw_model_ref="codex_cli/gpt-5.5")

    async def run_pilot() -> None:
        app = create_harness_app(tmp_path)
        async with app.run_test(size=(130, 44)) as pilot:
            prompt = app.query_one("#prompt", TextArea)
            dialog = app.query_one("#dialog-panel", Static)

            assert app._selected_theme_id == "light"
            assert str(app.theme) == "harness-light"
            assert app.current_theme.name == "harness-light"
            assert app.has_class("-light-mode") is True
            assert app.has_class("-dark-mode") is False

            prompt.value = "/theme"
            await pilot.press("ctrl+enter")
            await pilot.pause()
            assert app._dialog_kind == "themes"
            assert "Switch theme" in str(dialog.content)
            assert "Light" in str(dialog.content)
            assert "Dark" in str(dialog.content)
            assert "System" in str(dialog.content)
            assert "runtime only" in str(dialog.content)
            assert "brighter Harness surface" in str(dialog.content)
            assert prompt.value == ""
            await pilot.press("down")
            await pilot.press("enter")
            await pilot.pause()
            assert app._selected_theme_id == "dark"
            assert str(app.theme) == "textual-dark"
            assert app.has_class("-dark-mode") is True
            assert app.has_class("-light-mode") is False
            activation = app._latest_palette_activation
            assert activation["entry_id"] == "ui_controls.theme_dark"
            assert activation["activation_kind"] == "ui_action"
            assert activation["process_started"] is False
            assert activation["filesystem_modified"] is False
            assert activation["permission_granting"] is False
            assert activation["local_state_changes"]["changed_fields"] == ["selected_theme"]

            await pilot.press("ctrl+x")
            await pilot.press("t")
            await pilot.pause()
            assert app._dialog_kind == "themes"
            assert "Switch theme" in str(dialog.content)
            await pilot.press("up")
            await pilot.press("enter")
            await pilot.pause()
            assert app._selected_theme_id == "light"
            assert str(app.theme) == "harness-light"
            assert app.current_theme.name == "harness-light"
            assert app.has_class("-light-mode") is True
            assert app.has_class("-dark-mode") is False
            assert app._latest_palette_activation["entry_id"] == "ui_controls.theme_light"

            await pilot.press("ctrl+p")
            for char in "switch theme":
                await pilot.press(char)
            await pilot.pause()
            assert app._focus_mode == "palette"
            assert prompt.value == "switch theme"
            await pilot.press("enter")
            await pilot.pause()
            assert app._dialog_kind == "themes"
            assert "Light" in str(dialog.content)
            assert "Dark" in str(dialog.content)
            await pilot.press("down")
            await pilot.press("enter")
            await pilot.pause()
            assert app._selected_theme_id == "dark"
            assert str(app.theme) == "textual-dark"
            assert app.has_class("-dark-mode") is True
            assert str(dialog.content) == ""

            await pilot.press("ctrl+x")
            await pilot.press("t")
            await pilot.pause()
            assert app._dialog_kind == "themes"
            await pilot.press("down")
            await pilot.press("enter")
            await pilot.pause()
            assert app._selected_theme_id == "system"
            assert str(app.theme) == "textual-light"
            assert app.current_theme.name == "textual-light"
            assert app.has_class("-light-mode") is True
            assert app.has_class("-dark-mode") is False
            assert app._latest_palette_activation["entry_id"] == "ui_controls.theme_system"

    asyncio.run(run_pilot())


def test_tui_model_dialog_arrows_select_highlighted_model(tmp_path) -> None:
    pytest.importorskip("textual")
    from textual.widgets import Static, TextArea

    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    session = store.create_session(title="Arrow model", agent_id="plan", raw_model_ref="codex_cli/gpt-5.5")
    unique_refs = []
    for model in build_tui_dashboard(tmp_path)["model_catalog"]["models"]:
        if model["raw_model_ref"] not in unique_refs:
            unique_refs.append(model["raw_model_ref"])
    assert len(unique_refs) >= 2
    selected_ref = unique_refs[1]

    async def run_pilot() -> None:
        app = create_harness_app(tmp_path)
        async with app.run_test(size=(130, 44)) as pilot:
            prompt = app.query_one("#prompt", TextArea)
            dialog = app.query_one("#dialog-panel", Static)
            initial_messages = len(app._messages)

            prompt.value = "/model"
            await pilot.press("ctrl+enter")
            await pilot.pause()
            assert app._dialog_kind == "models"
            assert app._dialog_selected_index == 0

            await pilot.press("down")
            await pilot.pause()
            assert app._dialog_selected_index == 1
            assert selected_ref.split("/", 1)[-1] in str(dialog.content)

            await pilot.press("enter")
            await pilot.pause()
            assert prompt.value == ""
            assert len(app._messages) == initial_messages
            assert str(dialog.content) == ""
            activation = app._latest_palette_activation
            assert activation["activation_kind"] == "session_model_selection"
            assert activation["raw_model_ref"] == selected_ref
            assert activation["provider_started"] is False
            assert activation["model_execution_started"] is False
            assert activation["process_started"] is False
            assert activation["permission_granting"] is False

    asyncio.run(run_pilot())

    assert SQLiteStore(tmp_path).get_session(session.id).raw_model_ref == selected_ref


def test_tui_filter_model_searches_sanitized_panes(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    bundle_path = tmp_path / "search_agent_bundle"
    scaffold = runner.invoke(
        app,
        [
            "agents",
            "scaffold",
            "search_agent",
            "--workbench",
            "quant",
            "--kind",
            "specialist",
            "--parent",
            "quant_research",
            "--model-profile",
            "local_reasoning",
            "--tool-policy",
            "read_only",
            "--memory-scope",
            "quant",
            "--output",
            str(bundle_path),
            "--output-format",
            "json",
        ],
    )
    assert scaffold.exit_code == 0, scaffold.output
    imported = runner.invoke(app, ["agents", "import", str(bundle_path), "--project", str(tmp_path), "--output", "json"])
    assert imported.exit_code == 0, imported.output
    task = runner.invoke(
        app,
        [
            "tasks",
            "add",
            "--title",
            "Searchable task",
            "--agent",
            "search_agent",
            "--workbench",
            "quant",
            "--execution-adapter",
            "read_only_summary",
            "--task-type",
            "read_only_repo_summary",
            "--project",
            str(tmp_path),
            "--output",
            "json",
        ],
    )
    assert task.exit_code == 0, task.output
    tick = runner.invoke(app, ["daemon", "run-once", "--project", str(tmp_path), "--output", "json"])
    assert tick.exit_code == 0, tick.output
    created_run = runner.invoke(
        app,
        [
            "dev",
            "create-run",
            "--goal",
            "searchable dashboard run",
            "--task-type",
            "phase_1a_test",
            "--project",
            str(tmp_path),
        ],
    )
    assert created_run.exit_code == 0, created_run.output

    panes = build_tui_panes(build_tui_dashboard(tmp_path))
    unfiltered = filter_tui_panes(panes, "")
    agent_filtered = filter_tui_panes(panes, "SEARCH_AGENT")
    task_filtered = filter_tui_panes(panes, "searchable task")
    lease_filtered = filter_tui_panes(panes, json.loads(tick.output)["lease"]["id"])
    run_filtered = filter_tui_panes(panes, "phase_1a_test")
    daemon_filtered = filter_tui_panes(panes, "daemon")
    command_filtered = filter_tui_panes(panes, "tasks list")
    missing_filtered = filter_tui_panes(panes, "does-not-exist")

    assert unfiltered["schema_version"] == "harness.tui_filter/v1"
    assert [pane["id"] for pane in unfiltered["panes"]] == [pane["id"] for pane in panes]
    assert render_filter_status(unfiltered).startswith("Search: none | Matches:")
    assert [pane["id"] for pane in agent_filtered["panes"]] == ["agents"]
    assert agent_filtered["panes"][0]["match_count"] == 1
    assert [pane["id"] for pane in task_filtered["panes"]] == ["tasks"]
    assert [pane["id"] for pane in lease_filtered["panes"]] == ["leases", "guidance"]
    assert [pane["id"] for pane in run_filtered["panes"]] == ["runs"]
    assert "daemon" in [pane["id"] for pane in daemon_filtered["panes"]]
    assert [pane["id"] for pane in command_filtered["panes"]] == ["commands"]
    settings = next(pane for pane in panes if pane["id"] == "settings")
    settings_text = "\n".join(settings["lines"])
    catalog = build_tui_settings_catalog()
    assert catalog["schema_version"] == "harness.tui_settings/v1"
    assert catalog["source"] == "defaults"
    assert catalog["source_label"] == "defaults"
    assert catalog["session_id"] is None
    assert catalog["preference_source"] == "defaults"
    assert catalog["evidence_status"] == "read_only_settings_metadata"
    assert catalog["policy_boundary"]["kind"] == "tui_settings_read_only"
    assert catalog["policy_boundary"]["preference_persistence_allowed"] is False
    assert catalog["policy_boundary"]["backend_settings_allowed"] is False
    assert catalog["policy_boundary"]["process_start_allowed"] is False
    assert catalog["policy_boundary"]["filesystem_mutation_allowed"] is False
    assert catalog["policy_boundary"]["permission_grant_allowed"] is False
    assert catalog["preferences"]["composer_mode"] == "multiline"
    assert catalog["preferences_persisted"] is False
    assert catalog["backend_settings_exposed"] is False
    assert catalog["authority_granting"] is False
    assert catalog["process_started"] is False
    assert catalog["filesystem_modified"] is False
    assert catalog["permission_granting"] is False
    assert catalog["persist_command"] == "harness session preferences <session-id> --project . --set key=value"
    assert {setting["key"] for setting in catalog["settings"]} == {
        "theme",
        "terminal_font_size",
        "keybinding_preset",
        "composer_mode",
    }
    assert "theme=light" in settings_text
    assert "composer_mode=multiline" in settings_text
    assert "Policy: tui_settings_read_only" in settings_text
    assert "Evidence: read_only_settings_metadata" in settings_text
    assert "ctrl+p -> toggle_palette_focus" in settings_text
    assert "composer_mode kind=choice scope=session default=multiline" in settings_text
    assert "Filesystem modified: False" in settings_text
    assert "Process started: False" in settings_text
    assert "Permission granting: False" in settings_text
    assert "Preferences persisted: False" in settings_text
    assert "Backend settings exposed: False" in settings_text
    assert missing_filtered["panes"] == []
    assert missing_filtered["total_matches"] == 0
    assert render_filter_status(missing_filtered) == "Search: does-not-exist | Matches: 0 | Panes: 0"
    serialized = json.dumps(
        {
            "panes": panes,
            "agent_filtered": agent_filtered,
            "task_filtered": task_filtered,
            "lease_filtered": lease_filtered,
            "run_filtered": run_filtered,
            "daemon_filtered": daemon_filtered,
            "command_filtered": command_filtered,
            "settings": settings,
        }
    )
    assert "api_key" not in serialized
    assert "OPENAI_API_KEY" not in serialized
    assert "base_url" not in serialized
    assert "Created Phase 1A diagnostic run." not in serialized


def test_tui_command_palette_is_grouped_searchable_and_non_executing() -> None:
    palette = build_command_palette()
    group_ids = [group["id"] for group in palette["groups"]]
    entry_ids = [entry["id"] for entry in palette["entries"]]

    assert palette["schema_version"] == "harness.tui_command_palette/v1"
    assert group_ids == [
        "orientation",
        "ui_controls",
        "model_selection",
        "agent_authoring",
        "native_agents",
        "project_agents",
        "built_in_specs",
        "objectives_tasks",
        "daemon_control",
        "registered_adapters",
        "runtime_evidence",
        "sessions",
        "packaging_smoke",
    ]
    assert len(entry_ids) == len(set(entry_ids))
    assert all(set(entry) >= {"id", "group_id", "title", "command", "description", "mutates_when_run", "safety_note", "activation"} for entry in palette["entries"])
    assert all(entry["group_id"] in group_ids for entry in palette["entries"])
    assert next(entry for entry in palette["entries"] if entry["id"] == "sessions.list")["activation"]["kind"] == "ui_action"
    assert next(entry for entry in palette["entries"] if entry["id"] == "ui_controls.expand_all")["activation"]["kind"] == "ui_action"
    assert next(entry for entry in palette["entries"] if entry["id"] == "ui_controls.settings")["activation"]["kind"] == "ui_action"
    assert next(entry for entry in palette["entries"] if entry["id"] == "sessions.continue_last")["activation"]["kind"] == "manual_command"
    safe_activation = next(entry for entry in palette["entries"] if entry["id"] == "sessions.list")["activation"]
    manual_activation = next(entry for entry in palette["entries"] if entry["id"] == "sessions.continue_last")["activation"]
    assert safe_activation["evidence_status"] == "ui_only_in_memory"
    assert safe_activation["policy_boundary"]["kind"] == "safe_ui_activation"
    assert safe_activation["policy_boundary"]["command_execution_allowed"] is False
    assert safe_activation["policy_boundary"]["provider_call_allowed"] is False
    assert safe_activation["policy_boundary"]["shell_allowed"] is False
    assert safe_activation["policy_boundary"]["adapter_dispatch_allowed"] is False
    assert safe_activation["policy_boundary"]["child_process_allowed"] is False
    assert safe_activation["policy_boundary"]["filesystem_mutation_allowed"] is False
    assert safe_activation["policy_boundary"]["permission_grant_allowed"] is False
    assert safe_activation["policy_boundary"]["authority_grant_allowed"] is False
    assert safe_activation["policy_boundary"]["session_message_allowed"] is False
    assert safe_activation["blocked_reasons"] == []
    assert safe_activation["provider_started"] is False
    assert safe_activation["shell_started"] is False
    assert safe_activation["adapter_started"] is False
    assert safe_activation["child_process_started"] is False
    assert safe_activation["authority_granting"] is False
    assert safe_activation["session_message_created"] is False
    assert manual_activation["evidence_status"] == "manual_preview_only"
    assert manual_activation["blocked_reasons"] == ["manual_command_preview_only"]
    assert manual_activation["provider_started"] is False
    assert manual_activation["shell_started"] is False
    assert manual_activation["adapter_started"] is False
    assert manual_activation["child_process_started"] is False

    all_entries = filter_command_palette(palette, "")
    daemon_entries = filter_command_palette(palette, "daemon")
    read_only_entries = filter_command_palette(palette, "execute-read-only")
    planning_entries = filter_command_palette(palette, "repo_planning")
    build_entries = filter_command_palette(palette, "build agent")
    plan_agent_entries = filter_command_palette(palette, "plan agent")
    adapter_entries = filter_command_palette(palette, "adapter")
    packaging_entries = filter_command_palette(palette, "wheel")
    missing_entries = filter_command_palette(palette, "does-not-exist")

    assert all_entries["schema_version"] == "harness.tui_command_palette_filter/v1"
    assert all_entries["total_matches"] == len(palette["entries"])
    assert any(entry["id"] == "daemon_control.run_once" for entry in daemon_entries["entries"])
    assert [entry["id"] for entry in read_only_entries["entries"]] == ["registered_adapters.execute_read_only"]
    assert any(entry["id"] == "objectives_tasks.add_repo_planning_task" for entry in planning_entries["entries"])
    assert any(entry["id"] == "native_agents.build" for entry in build_entries["entries"])
    assert any(entry["id"] == "native_agents.plan" for entry in plan_agent_entries["entries"])
    assert any(entry["id"] == "registered_adapters.execute" for entry in adapter_entries["entries"])
    assert [entry["id"] for entry in packaging_entries["entries"]] == ["packaging_smoke.wheel"]
    assert missing_entries["entries"] == []
    assert missing_entries["groups"] == []
    serialized = json.dumps(palette)
    assert "api_key" not in serialized
    assert "OPENAI_API_KEY" not in serialized
    assert "base_url" not in serialized
    assert "subprocess" not in serialized
    assert "artifact contents" not in serialized


def test_tui_command_palette_panes_show_safe_actions_and_manual_command_details() -> None:
    palette = build_command_palette()
    read_only_entries = filter_command_palette(palette, "execute-read-only")
    missing_entries = filter_command_palette(palette, "does-not-exist")

    panes = build_command_palette_panes(read_only_entries)
    missing_panes = build_command_palette_panes(missing_entries)

    assert render_palette_status(read_only_entries) == "Palette search: execute-read-only | Commands: 1 | Groups: 1"
    assert [pane["id"] for pane in panes] == [
        "command_palette",
        "command_palette_registered_adapters",
        "command_palette_selected",
    ]
    assert "Safe UI actions activate in-process; command entries remain manual previews." in panes[0]["lines"]
    assert any("registered_adapters.execute_read_only" in line for line in panes[1]["lines"])
    selected_lines = "\n".join(panes[2]["lines"])
    assert "Activation: manual_command" in selected_lines
    assert "harness daemon execute-read-only task_lease_abc123 --project . --output json" in selected_lines
    assert "Compatibility command for the bounded read-only adapter when manually run." in selected_lines
    assert "No matching command template." in missing_panes[-1]["lines"]

    serialized = json.dumps({"panes": panes, "missing": missing_panes})
    assert "api_key" not in serialized
    assert "OPENAI_API_KEY" not in serialized
    assert "base_url" not in serialized
    assert "subprocess" not in serialized
    assert "artifact contents" not in serialized


def test_tui_command_palette_activation_applies_only_safe_ui_actions() -> None:
    palette = build_command_palette()

    sessions = activate_command_palette_entry(
        palette,
        "sessions.list",
        {"focus_mode": "palette", "active_section_index": 0},
    )
    manual = activate_command_palette_entry(palette, "registered_adapters.execute_read_only")
    missing = activate_command_palette_entry(palette, "does-not-exist")
    toggle = activate_command_palette_entry(
        palette,
        "ui_controls.toggle_section",
        {"active_section_index": 4, "collapsed_section_ids": set()},
    )
    expand = activate_command_palette_entry(
        palette,
        "ui_controls.expand_all",
        {"active_section_index": 4, "collapsed_section_ids": {"queue_daemon"}},
    )
    clear = activate_command_palette_entry(
        palette,
        "ui_controls.clear_search",
        {"focus_mode": "palette", "query": "sessions"},
    )
    settings = activate_command_palette_entry(
        palette,
        "ui_controls.settings",
        {"focus_mode": "palette", "active_section_index": 0},
    )
    select_build = activate_command_palette_entry(
        palette,
        "native_agents.select_build",
        {"selected_agent_id": "plan", "focus_mode": "dashboard"},
    )
    select_plan = activate_command_palette_entry(
        palette,
        "native_agents.select_plan",
        {"selected_agent_id": "build", "focus_mode": "dashboard"},
    )

    assert sessions["schema_version"] == "harness.tui_palette_activation/v1"
    assert sessions["ok"] is True
    assert sessions["activation_kind"] == "ui_action"
    assert sessions["ui_action_applied"] is True
    assert sessions["view_state"]["focus_mode"] == "dashboard"
    assert sessions["view_state"]["active_section_id"] == "context"
    assert sessions["evidence_status"] == "ui_focus_in_memory"
    assert sessions["policy_boundary"]["kind"] == "safe_ui_activation"
    assert sessions["policy_boundary"]["command_execution_allowed"] is False
    assert sessions["policy_boundary"]["provider_call_allowed"] is False
    assert sessions["policy_boundary"]["shell_allowed"] is False
    assert sessions["policy_boundary"]["adapter_dispatch_allowed"] is False
    assert sessions["policy_boundary"]["child_process_allowed"] is False
    assert sessions["policy_boundary"]["filesystem_mutation_allowed"] is False
    assert sessions["policy_boundary"]["permission_grant_allowed"] is False
    assert sessions["policy_boundary"]["authority_grant_allowed"] is False
    assert sessions["policy_boundary"]["session_message_allowed"] is False
    assert sessions["blocked_reasons"] == []
    assert sessions["local_state_changes"] == {
        "changed_fields": ["focus_mode", "active_section_id", "active_section_index"],
        "creates_message": False,
        "starts_request": False,
        "executes_command": False,
        "mutates_filesystem": False,
        "grants_permission": False,
    }
    assert sessions["request_started"] is False
    assert sessions["command_started"] is False
    assert sessions["provider_started"] is False
    assert sessions["shell_started"] is False
    assert sessions["adapter_started"] is False
    assert sessions["child_process_started"] is False
    assert sessions["process_started"] is False
    assert sessions["filesystem_modified"] is False
    assert sessions["permission_granting"] is False
    assert sessions["authority_granting"] is False
    assert sessions["session_message_created"] is False

    assert manual["ok"] is False
    assert manual["activation_kind"] == "manual_command"
    assert manual["ui_action_applied"] is False
    assert manual["evidence_status"] == "manual_preview_only"
    assert manual["policy_boundary"]["kind"] == "safe_ui_activation"
    assert manual["blocked_reasons"] == ["manual_command_preview_only"]
    assert manual["request_started"] is False
    assert manual["command_started"] is False
    assert manual["provider_started"] is False
    assert manual["shell_started"] is False
    assert manual["adapter_started"] is False
    assert manual["child_process_started"] is False
    assert manual["process_started"] is False
    assert manual["filesystem_modified"] is False
    assert manual["permission_granting"] is False
    assert manual["authority_granting"] is False
    assert manual["session_message_created"] is False

    assert missing["ok"] is False
    assert missing["activation_kind"] == "missing"
    assert missing["evidence_status"] == "missing_entry"
    assert missing["blocked_reasons"] == ["palette_entry_not_found"]
    assert missing["request_started"] is False
    assert missing["command_started"] is False
    assert missing["provider_started"] is False
    assert missing["shell_started"] is False
    assert missing["adapter_started"] is False
    assert missing["child_process_started"] is False
    assert missing["process_started"] is False
    assert missing["filesystem_modified"] is False
    assert missing["permission_granting"] is False
    assert missing["authority_granting"] is False
    assert missing["session_message_created"] is False
    assert toggle["ok"] is True
    assert toggle["evidence_status"] == "ui_section_toggle_in_memory"
    assert toggle["view_state"]["active_section_id"] == "evidence"
    assert toggle["view_state"]["collapsed_section_ids"] == ["evidence"]
    assert toggle["local_state_changes"]["changed_fields"] == ["active_section_id", "collapsed_section_ids"]
    assert toggle["local_state_changes"]["creates_message"] is False
    assert toggle["request_started"] is False
    assert toggle["command_started"] is False
    assert toggle["filesystem_modified"] is False
    assert toggle["permission_granting"] is False
    assert expand["ok"] is True
    assert expand["evidence_status"] == "ui_sections_expanded_in_memory"
    assert expand["view_state"]["collapsed_section_ids"] == []
    assert expand["local_state_changes"]["changed_fields"] == ["collapsed_section_ids"]
    assert expand["request_started"] is False
    assert expand["command_started"] is False
    assert expand["filesystem_modified"] is False
    assert expand["permission_granting"] is False
    assert clear["ok"] is True
    assert clear["evidence_status"] == "ui_search_cleared_in_memory"
    assert clear["view_state"]["focus_mode"] == "dashboard"
    assert clear["view_state"]["query"] == ""
    assert clear["local_state_changes"]["changed_fields"] == ["focus_mode", "query"]
    assert clear["request_started"] is False
    assert clear["command_started"] is False
    assert clear["filesystem_modified"] is False
    assert clear["permission_granting"] is False
    assert settings["ok"] is True
    assert settings["evidence_status"] == "ui_focus_in_memory"
    assert settings["view_state"]["focus_mode"] == "dashboard"
    assert settings["view_state"]["active_section_id"] == "context"
    assert settings["view_state"]["requested_section_id"] == "settings"
    assert select_build["ok"] is True
    assert select_build["evidence_status"] == "ui_agent_selected_in_memory"
    assert select_build["view_state"]["selected_agent_id"] == "build"
    assert select_build["local_state_changes"]["changed_fields"] == ["selected_agent_id"]
    assert select_build["request_started"] is False
    assert select_build["command_started"] is False
    assert select_build["provider_started"] is False
    assert select_build["filesystem_modified"] is False
    assert select_build["permission_granting"] is False
    assert select_build["authority_granting"] is False
    assert select_plan["ok"] is True
    assert select_plan["view_state"]["selected_agent_id"] == "plan"


def test_tui_view_model_sections_order_and_no_match_state(tmp_path) -> None:
    dashboard = build_tui_dashboard(tmp_path)
    panes = build_tui_panes(dashboard)
    palette = build_command_palette()
    filtered = filter_tui_panes(panes, "")
    filtered_palette = filter_command_palette(palette, "")
    view = build_tui_view_model(filtered, filtered_palette)

    assert view["schema_version"] == "harness.tui_view/v1"
    assert [section["id"] for section in view["sections"]] == [
        "project_overview",
        "sessions",
        "queue_daemon",
        "agents_specs",
        "runtime_evidence",
        "settings",
        "command_palette",
        "safety",
    ]
    assert view["sections"][0]["pane_ids"] == ["overview", "models", "guidance", "commands"]
    assert view["sections"][1]["pane_ids"] == ["sessions"]
    assert view["sections"][2]["pane_ids"] == ["tasks", "leases", "daemon"]
    assert view["sections"][5]["pane_ids"] == ["settings"]
    assert view["sections"][6]["pane_ids"][0] == "command_palette"
    assert view["sections"][6]["pane_ids"][-1] == "command_palette_selected"
    assert "command_palette_ui_controls" in view["sections"][6]["pane_ids"]
    assert view["pane_order"][:8] == ["overview", "models", "guidance", "commands", "sessions", "tasks", "leases", "daemon"]
    assert view["pane_order"][-1] == "safety"
    assert view["empty_state"] is None
    assert view["focus_mode"] == "dashboard"
    assert view["collapsed_section_ids"] == []
    assert all(section["collapsed"] is False for section in view["sections"])
    assert view["search"]["dashboard_panes"] == len(panes)
    assert view["search"]["palette_matches"] == len(palette["entries"])
    assert render_view_status(view).startswith(
        "View search: none | Focus: dashboard | Collapsed: 0 | Sections: 8 | Panes:"
    )
    assert f"Palette commands: {len(palette['entries'])}" in render_view_status(view)
    assert {hint["key"] for hint in view["navigation_hints"]} == {
        "/",
        "escape",
        "tab",
        "shift+tab",
        "ctrl+p/f2",
        "ctrl+x m",
        "c",
        "shift+c",
        "ctrl+q",
        "enter",
        "shift+enter",
        "safe-actions",
    }

    missing_view = build_tui_view_model(
        filter_tui_panes(panes, "does-not-exist"),
        filter_command_palette(palette, "does-not-exist"),
    )

    assert missing_view["sections"] == []
    assert missing_view["panes"] == []
    assert missing_view["pane_order"] == []
    assert missing_view["empty_state"] == {
        "title": "No matches",
        "message": "No matching panes or command templates.",
        "query": "does-not-exist",
    }
    assert missing_view["search"] == {
        "dashboard_matches": 0,
        "dashboard_panes": 0,
        "palette_matches": 0,
        "palette_groups": 0,
    }
    assert render_view_status(missing_view) == (
        "View search: does-not-exist | Focus: dashboard | Collapsed: 0 | Sections: 0 | Panes: 0 | "
        "Dashboard matches: 0 | Palette commands: 0"
    )

    serialized = json.dumps({"view": view, "missing": missing_view})
    assert "api_key" not in serialized
    assert "OPENAI_API_KEY" not in serialized
    assert "base_url" not in serialized
    assert "subprocess" not in serialized
    assert "artifact contents" not in serialized


def test_tui_terminal_tab_panel_uses_persisted_pty_events_without_live_process(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    store.append_store_event(
        EventStreamType.SESSION,
        "pty:pty_123",
        "pty.created",
        {"shell": "/bin/zsh", "title": "Dev shell", "cols": 80, "rows": 24},
    )
    store.append_store_event(
        EventStreamType.SESSION,
        "pty:pty_123",
        "pty.output",
        {"preview": "hello from terminal\n", "preview_bytes": 20},
        artifact_refs=["art_pty_output"],
    )

    dashboard = build_tui_dashboard(tmp_path)
    panes = build_tui_panes(dashboard)
    terminal = next(pane for pane in panes if pane["id"] == "terminal")
    view = build_tui_view_model(filter_tui_panes(panes, "terminal"), filter_command_palette(build_command_palette(), "terminal"))

    assert dashboard["terminal_tabs"]["schema_version"] == "harness.tui_terminal_tabs/v1"
    assert dashboard["terminal_tabs"]["tab_count"] == 1
    assert dashboard["terminal_tabs"]["policy_boundary"]["kind"] == "tui_terminal_panel_projection"
    assert dashboard["terminal_tabs"]["policy_boundary"]["source"] == "persisted_pty_events"
    assert dashboard["terminal_tabs"]["policy_boundary"]["terminal_control_allowed"] is False
    assert dashboard["terminal_tabs"]["policy_boundary"]["requires_append_only_events"] is True
    assert "terminal_panel_projection_disabled" in dashboard["terminal_tabs"]["blocked_reasons"]
    assert dashboard["terminal_tabs"]["source"] == "persisted_pty_events"
    assert dashboard["terminal_tabs"]["terminal_control_supported"] is False
    assert dashboard["terminal_tabs"]["websocket_supported"] is False
    assert dashboard["terminal_tabs"]["process_started"] is False
    assert dashboard["terminal_tabs"]["websocket_opened"] is False
    assert dashboard["terminal_tabs"]["live_stream_read"] is False
    assert dashboard["terminal_tabs"]["artifact_contents_included"] is False
    assert dashboard["terminal_tabs"]["permission_granting"] is False
    tab = dashboard["terminal_tabs"]["tabs"][0]
    assert tab["policy_boundary"]["kind"] == "tui_terminal_tab_projection"
    assert tab["policy_boundary"]["terminal_control_allowed"] is False
    assert tab["policy_boundary"]["requires_append_only_events"] is True
    assert tab["artifact_refs"] == ["art_pty_output"]
    assert "terminal_panel_projection_disabled" in tab["blocked_reasons"]
    assert tab["websocket_opened"] is False
    assert terminal["title"] == "Terminal Tabs"
    assert any("Policy: tui_terminal_panel_projection" in line for line in terminal["lines"])
    assert any("Blocked:" in line and "terminal_panel_projection_disabled" in line for line in terminal["lines"])
    assert any("pty_123 unavailable title=Dev shell" in line for line in terminal["lines"])
    assert any("boundary=tui_terminal_tab_projection" in line for line in terminal["lines"])
    assert any("preview: hello from terminal\\n" in line for line in terminal["lines"])
    assert any("No terminal process" in line and "terminal control" in line for line in terminal["lines"])
    assert "terminal" in view["pane_order"]
    runtime_section = next(section for section in view["sections"] if section["id"] == "runtime_evidence")
    assert "terminal" in runtime_section["pane_ids"]
    serialized = json.dumps({"dashboard": dashboard, "terminal": terminal, "view": view})
    assert "api_key" not in serialized
    assert "OPENAI_API_KEY" not in serialized
    assert "base_url" not in serialized
    assert "subprocess" not in serialized
    assert "artifact contents" not in serialized


def test_tui_view_model_supports_in_memory_section_collapse(tmp_path) -> None:
    dashboard = build_tui_dashboard(tmp_path)
    panes = build_tui_panes(dashboard)
    palette = build_command_palette()

    expanded = build_focused_tui_view_model(panes, palette, "", focus_mode="dashboard")
    collapsed = build_focused_tui_view_model(
        panes,
        palette,
        "",
        focus_mode="dashboard",
        collapsed_section_ids={"queue_daemon", "not_a_section"},
    )
    restored = build_focused_tui_view_model(
        panes,
        palette,
        "",
        focus_mode="dashboard",
        collapsed_section_ids=set(),
    )

    assert expanded["collapsed_section_ids"] == []
    assert collapsed["collapsed_section_ids"] == ["queue_daemon"]
    assert [section["id"] for section in collapsed["sections"]] == [
        section["id"] for section in expanded["sections"]
    ]
    queue_section = next(section for section in collapsed["sections"] if section["id"] == "queue_daemon")
    assert queue_section["collapsed"] is True
    assert queue_section["pane_ids"] == ["tasks", "leases", "daemon"]
    assert "tasks" not in collapsed["pane_order"]
    assert "leases" not in collapsed["pane_order"]
    assert "daemon" not in collapsed["pane_order"]
    assert restored["pane_order"] == expanded["pane_order"]

    serialized = json.dumps({"expanded": expanded, "collapsed": collapsed, "restored": restored})
    assert "api_key" not in serialized
    assert "OPENAI_API_KEY" not in serialized
    assert "base_url" not in serialized
    assert "subprocess" not in serialized
    assert "artifact contents" not in serialized


def test_tui_right_panel_defaults_to_compact_live_context_without_mutation(tmp_path) -> None:
    dashboard = build_tui_dashboard(tmp_path)
    palette = build_command_palette()

    model = build_right_panel_model(
        dashboard,
        {
            "palette": palette,
            "active_section_index": 0,
            "collapsed_section_ids": set(),
            "active_orchestrator": "coding_orchestrator",
            "chat_mode": "normal",
        },
        "",
        "dashboard",
    )
    rendered = render_right_panel(model)

    assert model["schema_version"] == "harness.tui_right_panel/v1"
    assert model["cockpit_schema_version"] == "harness.right_pane_cockpit/v1"
    assert [section["id"] for section in model["sections"]] == [
        "orchestrations",
        "active_work",
        "graph",
        "attention",
        "evidence",
        "context",
        "shortcuts",
    ]
    assert model["active_section_id"] == "active_work"
    assert model["active_signal"] == "setup_needed"
    assert model["summary"]["initialized"] is False
    assert "ORCHESTRATIONS" in rendered
    assert "Active Work" in rendered
    assert "State:" in rendered
    assert "needs setup" in rendered
    assert "Model: default" in rendered
    assert "Surface: read-only cockpit" in rendered
    assert "No assigned orchestrations." in rendered
    assert "/init" in rendered
    assert "create or select an objective" in rendered
    assert "Panes:" not in rendered
    assert "IDs:" not in rendered
    assert "harness daemon execute-read-only task_lease_abc123 --project . --output json" not in rendered
    assert "no_openai_api_usage" not in rendered
    assert "Harness" in render_right_panel_status(model)
    assert "Q 0R/0A/0B" in render_right_panel_status(model)
    assert not (tmp_path / ".harness").exists()


def test_tui_right_panel_surfaces_repo_planning_adapter_and_guidance(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    store.create_task(
        title="Plan repo change",
        metadata={"execution_adapter": "repo_planning", "task_type": "repo_planning"},
    )
    dashboard = build_tui_dashboard(tmp_path)
    palette = build_command_palette()

    model = build_right_panel_model(dashboard, {"palette": palette}, "repo_planning", "dashboard")
    rendered = render_right_panel(model)

    assert "Add repo planning task" in rendered
    assert dashboard["live_activity"]["active_signal"] == "ready"
    assert "Lease the next planning task" in render_right_panel(
        build_right_panel_model(dashboard, {"palette": palette}, "", "dashboard")
    )
    assert "Dispatch the lease after review" in render_right_panel(
        build_right_panel_model(dashboard, {"palette": palette}, "", "dashboard")
    )


def test_tui_right_panel_surfaces_assistant_context_and_pending_action(tmp_path) -> None:
    dashboard = build_tui_dashboard(tmp_path)
    palette = build_command_palette()

    model = build_right_panel_model(
        dashboard,
        {
            "palette": palette,
            "active_section_id": "action",
            "active_section_index": 0,
            "collapsed_section_ids": set(),
            "chat_mode": "normal",
            "pending_action_contract": {
                "summary": "Create Harness objective: improve chat",
                "tool": "create_objective",
                "risk": "control_plane_write",
            },
            "latest_response": {
                "tool_results": [{"tool": "read_file", "ok": True}],
                "context_manifest": {
                    "blocks": [
                        {"kind": "harness_state"},
                        {"kind": "repo_tree"},
                    ]
                },
            },
        },
        "",
        "dashboard",
    )
    rendered = render_right_panel(model)

    assert model["active_section_id"] == "active_work"
    assert "Tools: read_file" in rendered
    assert "Context: harness_state, repo_tree" in rendered
    assert "State: needs confirmation" in rendered
    assert "Pending: Create Harness objective: improve chat" in rendered
    assert "Tool: create objective" in rendered
    assert "Risk: control plane write" in rendered
    assert "Next: confirm or cancel in chat" in rendered
    assert not (tmp_path / ".harness").exists()


def test_tui_right_panel_surfaces_latest_managed_action_without_pending_confirmation(tmp_path) -> None:
    dashboard = build_tui_dashboard(tmp_path)
    palette = build_command_palette()

    model = build_right_panel_model(
        dashboard,
        {
            "palette": palette,
            "active_section_id": "action",
            "active_section_index": 0,
            "collapsed_section_ids": set(),
            "chat_mode": "normal",
            "latest_response": {
                "kind": "self_managed_local_action",
                "ok": True,
                "run_id": "run_managed123",
                "report_path": str(tmp_path / ".harness" / "runs" / "run_managed123" / "final_report.md"),
            },
        },
        "",
        "dashboard",
    )
    rendered = render_right_panel(model)

    assert model["active_section_id"] == "active_work"
    assert "Latest action: succeeded" in rendered
    assert "run_managed123" not in rendered
    assert "Report: final_report.md" in rendered
    assert "Confirm: yes or /confirm" not in rendered
    assert not (tmp_path / ".harness").exists()


def test_tui_right_panel_surfaces_latest_safe_ui_activation(tmp_path) -> None:
    dashboard = build_tui_dashboard(tmp_path)
    palette = build_command_palette()
    activation = activate_command_palette_entry(
        palette,
        "ui_controls.settings",
        {"focus_mode": "palette", "active_section_index": 0},
    )

    model = build_right_panel_model(
        dashboard,
        {
            "palette": palette,
            "active_section_id": "action",
            "active_section_index": 0,
            "collapsed_section_ids": set(),
            "latest_palette_activation": activation,
        },
        "",
        "dashboard",
    )
    rendered = render_right_panel(model)

    assert model["active_section_id"] == "active_work"
    assert "Last UI: ui controls settings" in rendered
    assert "Flags:" not in rendered
    assert not (tmp_path / ".harness").exists()


def test_tui_right_panel_sessions_surface_latest_persisted_ui_activation(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    session = store.create_session(title="Session UI action")
    store.append_store_event(
        EventStreamType.SESSION,
        session.id,
        "tui.ui_activation.applied",
        {
            "source": "slash",
            "entry_id": "ui_controls.settings",
            "activation_kind": "ui_action",
            "action": {"type": "focus_section", "section_id": "settings"},
            "ui_action_applied": True,
            "command_started": False,
            "process_started": False,
            "filesystem_modified": False,
            "permission_granting": False,
            "authority_granting": False,
        },
        session_id=session.id,
    )
    dashboard = build_tui_dashboard(tmp_path)
    palette = build_command_palette()

    model = build_right_panel_model(
        dashboard,
        {"palette": palette, "active_section_id": "sessions", "active_section_index": 2, "collapsed_section_ids": set()},
        "",
        "dashboard",
    )
    rendered = render_right_panel(model)

    assert model["active_section_id"] == "context"
    assert dashboard["active_session"]["latest_ui_activation"]["entry_id"] == "ui_controls.settings"
    assert "Session: Session UI action" in rendered
    assert "UI flags:" not in rendered


def test_tui_right_panel_search_and_palette_are_progressive(tmp_path) -> None:
    dashboard = build_tui_dashboard(tmp_path)
    palette = build_command_palette()

    task_search = build_right_panel_model(dashboard, {"palette": palette}, "tasks", "dashboard")
    palette_focus = build_right_panel_model(dashboard, {"palette": palette}, "execute-read-only", "palette")
    missing = build_right_panel_model(dashboard, {"palette": palette}, "does-not-exist", "dashboard")

    assert [section["id"] for section in task_search["sections"]] == ["graph", "commands"]
    assert "Graph is projected from objectives, tasks, runs, and artifacts." in render_right_panel(task_search)
    assert "harness tasks list --project ." in render_right_panel(task_search)
    assert palette_focus["mode"] == "overview"
    assert "context" in [section["id"] for section in palette_focus["sections"]]
    assert palette_focus["sections"][-1]["id"] == "commands"
    assert "harness daemon execute-read-only task_lease_abc123 --project . --output json" in render_right_panel(palette_focus)
    assert "1 commands" in render_right_panel_status(palette_focus)
    assert missing["empty_state"]["message"] == "No matches. Try /help, tasks, runs, adapters."
    assert "No matches. Try /help, tasks, runs, adapters." in render_right_panel(missing)
    assert not (tmp_path / ".harness").exists()


def test_tui_right_panel_attention_prioritizes_pending_permission_over_ready_work(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    session = store.create_session(title="Permission session")
    store.create_task(
        title="Ready task",
        metadata={"execution_adapter": "repo_planning", "task_type": "repo_planning"},
    )
    permission = store.request_session_permission(
        session.id,
        tool_id="shell",
        normalized_action="run",
        normalized_target_pattern="pytest tests",
        boundary_kind="shell",
        risk="high",
    )

    dashboard = build_tui_dashboard(tmp_path, selected_session_id=session.id)
    model = build_right_panel_model(dashboard, {"palette": build_command_palette()}, "", "dashboard")
    rendered = render_right_panel(model)

    assert dashboard["live_activity"]["active_signal"] == "approval_required"
    assert dashboard["live_activity"]["pending_permissions"][0]["id"] == permission.id
    assert dashboard["live_activity"]["counts"]["ready"] == 1
    assert model["active_signal"] == "approval_required"
    assert "State: approval needed" in rendered
    assert "Permission: shell run" in rendered
    assert "Target: pytest tests" in rendered
    assert "Ready: 1" not in rendered.split("[bold steel_blue1]  Now[/bold steel_blue1]", 1)[0]
    assert dashboard["live_activity"]["policy_boundary"]["process_started"] is False
    assert dashboard["live_activity"]["policy_boundary"]["filesystem_modified"] is False
    assert dashboard["live_activity"]["policy_boundary"]["permission_granting"] is False


def test_tui_right_panel_attention_surfaces_active_lease_before_idle(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    task = store.create_task(
        title="Lease this",
        metadata={"execution_adapter": "repo_planning", "task_type": "repo_planning"},
    )
    leased = store.select_next_task_for_lease("test-owner")
    lease = leased["lease"]

    dashboard = build_tui_dashboard(tmp_path)
    model = build_right_panel_model(dashboard, {"palette": build_command_palette()}, "", "dashboard")
    rendered = render_right_panel(model)

    assert task.id == lease.task_id
    assert dashboard["live_activity"]["active_signal"] == "running"
    assert model["active_signal"] == "running"
    assert "State: running" in rendered
    assert "Task: Lease this" in rendered
    assert lease.id not in rendered
    assert task.id not in rendered


def test_tui_right_panel_blocked_progress_shows_stable_code_and_inspect_command(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    objective = store.create_objective("Blocked objective")
    task = store.create_task(
        title="Bad adapter",
        objective_id=objective.id,
        metadata={"execution_adapter": "missing_adapter", "task_type": "unknown"},
    )

    dashboard = build_tui_dashboard(tmp_path)
    model = build_right_panel_model(dashboard, {"palette": build_command_palette()}, "", "dashboard")
    rendered = render_right_panel(model)

    assert dashboard["live_activity"]["active_signal"] == "blocked"
    assert model["active_signal"] == "blocked"
    assert "Blocker: unknown_adapter" in rendered
    assert "Work: Bad adapter" in rendered
    assert task.id not in rendered
    assert "inspect progress details" in rendered


def test_tui_live_activity_projection_selected_session_todos_events_artifacts_and_escape(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    session = store.create_session(title="[bold]x[/bold]", agent_id="plan")
    message = store.append_session_message(session.id, "user", "inspect selected live state")
    store.append_session_part(session.id, message.id, "text", text="inspect selected live state")
    todo = store.append_session_todo(session.id, "[bold]todo[/bold]", status="in_progress", priority=3)
    permission = store.request_session_permission(
        session.id,
        tool_id="read",
        normalized_action="open",
        normalized_target_pattern="[bold]file[/bold]",
        boundary_kind="local_only",
        risk="medium",
    )
    run = store.create_run("artifact run", "phase_1a_test", session_id=session.id)
    artifact_path = tmp_path / "result.txt"
    artifact_path.write_text("artifact summary", encoding="utf-8")
    artifact = store.register_artifact(run.id, "summary", artifact_path, session_id=session.id)
    other = store.create_session(title="Most recent")

    dashboard = build_tui_dashboard(tmp_path, selected_session_id=session.id)
    model = build_right_panel_model(dashboard, {"palette": build_command_palette()}, "", "dashboard")
    rendered = render_right_panel(model)
    detail = render_right_panel_detail(model, "sessions")

    assert other.id != session.id
    assert dashboard["active_session"]["id"] == session.id
    assert dashboard["model_catalog"]["active_model"]["session_id"] == session.id
    assert dashboard["live_activity"]["schema_version"] == "harness.tui_live_activity/v1"
    assert dashboard["live_activity"]["pending_permissions"][0]["id"] == permission.id
    assert dashboard["live_activity"]["open_todos"][0]["id"] == todo.id
    assert any(event["kind"] == "todo.updated" for event in dashboard["live_activity"]["latest_events"])
    assert dashboard["live_activity"]["recent_artifacts"][0]["id"] == artifact.id
    assert dashboard["live_activity"]["counts"]["pending_permissions"] == 1
    assert dashboard["live_activity"]["counts"]["open_todos"] == 1
    assert dashboard["live_activity"]["counts"]["recent_artifacts"] == 1
    assert "Active: \\[bold]x\\[/bold]" in rendered
    assert "Target: \\[bold]file\\[/bold]" in rendered
    assert "[bold]x[/bold]" not in detail.split("Active:", 1)[-1].splitlines()[0]
    boundary = dashboard["live_activity"]["policy_boundary"]
    assert boundary["process_started"] is False
    assert boundary["filesystem_modified"] is False
    assert boundary["provider_execution_started"] is False
    assert boundary["model_execution_started"] is False
    assert boundary["shell_started"] is False
    assert boundary["docker_started"] is False
    assert boundary["permission_granting"] is False
    assert "No command, provider, shell, Docker, adapter, filesystem, or permission action is started" in detail


def test_tui_right_panel_active_section_id_collapse_and_detail_are_ui_only(tmp_path) -> None:
    dashboard = build_tui_dashboard(tmp_path)
    palette = build_command_palette()
    settings = activate_command_palette_entry(
        palette,
        "ui_controls.settings",
        {"focus_mode": "palette", "active_section_index": 0, "collapsed_section_ids": set()},
    )
    focused = build_right_panel_model(dashboard, {**settings["view_state"], "palette": palette}, "", "dashboard")
    collapsed = build_right_panel_model(
        dashboard,
        {"palette": palette, "active_section_id": "queue", "collapsed_section_ids": {"queue_daemon"}},
        "",
        "dashboard",
    )
    detail = render_right_panel_detail(focused)

    assert settings["view_state"]["active_section_id"] == "context"
    assert settings["view_state"]["requested_section_id"] == "settings"
    assert focused["active_section_id"] == "context"
    queue = next(section for section in collapsed["sections"] if section["id"] == "orchestrations")
    assert queue["collapsed"] is True
    assert collapsed["collapsed_section_ids"] == ["orchestrations"]
    assert "Context detail" in detail
    assert "Read-only persisted Harness projection" in detail
    assert "process=False" in detail
    assert settings["process_started"] is False
    assert settings["filesystem_modified"] is False
    assert settings["permission_granting"] is False
    assert settings["authority_granting"] is False
    assert not (tmp_path / ".harness").exists()


def test_tui_palette_focus_filters_palette_without_hiding_dashboard(tmp_path) -> None:
    dashboard = build_tui_dashboard(tmp_path)
    panes = build_tui_panes(dashboard)
    palette = build_command_palette()

    dashboard_focus = build_focused_tui_view_model(panes, palette, "execute-read-only", focus_mode="dashboard")
    palette_focus = build_focused_tui_view_model(panes, palette, "execute-read-only", focus_mode="palette")
    missing_palette_focus = build_focused_tui_view_model(panes, palette, "does-not-exist", focus_mode="palette")

    assert dashboard_focus["focus_mode"] == "dashboard"
    assert palette_focus["focus_mode"] == "palette"
    assert palette_focus["search"]["dashboard_panes"] == len(panes)
    assert palette_focus["search"]["dashboard_matches"] == sum(len(pane["lines"]) for pane in panes)
    assert palette_focus["search"]["palette_matches"] == 1
    assert "command_palette_selected" in palette_focus["pane_order"]
    assert any(
        pane["id"] == "command_palette_selected"
        and "harness daemon execute-read-only task_lease_abc123 --project . --output json" in "\n".join(pane["lines"])
        for pane in palette_focus["panes"]
    )

    assert missing_palette_focus["focus_mode"] == "palette"
    assert missing_palette_focus["empty_state"] is None
    assert missing_palette_focus["search"]["dashboard_panes"] == len(panes)
    assert missing_palette_focus["search"]["palette_matches"] == 0
    assert "overview" in missing_palette_focus["pane_order"]
    assert "command_palette_selected" in missing_palette_focus["pane_order"]
    selected = next(pane for pane in missing_palette_focus["panes"] if pane["id"] == "command_palette_selected")
    assert selected["lines"] == ["No matching command template."]

    serialized = json.dumps(
        {
            "dashboard_focus": dashboard_focus,
            "palette_focus": palette_focus,
            "missing_palette_focus": missing_palette_focus,
        }
    )
    assert "api_key" not in serialized
    assert "OPENAI_API_KEY" not in serialized
    assert "base_url" not in serialized
    assert "subprocess" not in serialized
    assert "artifact contents" not in serialized


def test_tui_prompt_keeps_slash_typable_and_handles_navigation_keys(tmp_path) -> None:
    pytest.importorskip("textual")
    from textual.widgets import Static, TextArea

    async def run_pilot() -> None:
        app = create_harness_app(tmp_path)
        async with app.run_test(size=(100, 40)) as pilot:
            prompt = app.query_one("#prompt", TextArea)
            slash_status = app.query_one("#slash-status", Static)
            assert app.use_command_palette is False

            await pilot.press("/")
            await pilot.pause()
            assert prompt.value == "/"
            assert "/help" in str(slash_status.content)
            assert "Print the MVP agent command sequence" in str(slash_status.content)
            assert "[bold blue]/help" in str(slash_status.content)

            await pilot.press("down")
            await pilot.pause()
            assert prompt.value == "/"
            assert "[bold blue]/home" in str(slash_status.content)

            await pilot.press("up")
            await pilot.pause()
            assert prompt.value == "/"
            assert "[bold blue]/help" in str(slash_status.content)
            assert app._request_from_prompt_submission("/") == "/help"

            for _ in range(8):
                await pilot.press("down")
            await pilot.pause()
            assert prompt.value == "/"
            assert "... 1 previous. Keep using arrows to navigate." in str(slash_status.content)
            assert "[bold blue]/scaffold" in str(slash_status.content)

            await pilot.press("e", "x", "e")
            await pilot.pause()
            assert prompt.value == "/exe"
            assert "/execute" in str(slash_status.content)
            assert "/home" not in str(slash_status.content)

            await pilot.press("down")
            await pilot.pause()
            assert prompt.value == "/exe"
            assert "[bold blue]/execute" in str(slash_status.content)
            assert app._request_from_prompt_submission("/exe") == "/execute"
            message_count = len(app._messages)

            await pilot.press("enter")
            await pilot.pause()
            assert prompt.value == "/execute"
            assert len(app._messages) == message_count

            await pilot.press("enter")
            await pilot.pause()
            rendered = "\n".join(render_chat_message(message) for message in app._messages)
            assert "/execute" in rendered
            assert len(app._messages) > message_count

            await pilot.press("escape")
            await pilot.pause()
            assert prompt.value == ""
            assert str(slash_status.content) == ""

            assert app._section_cursor_index == 1
            await pilot.press("tab")
            await pilot.pause()
            assert app._section_cursor_index == 2

            await pilot.press("shift+tab")
            await pilot.pause()
            assert app._section_cursor_index == 1

            assert app._focus_mode == "dashboard"
            await pilot.press("ctrl+p")
            await pilot.pause()
            assert app._focus_mode == "palette"
            await pilot.press("ctrl+p")
            await pilot.pause()
            assert app._focus_mode == "dashboard"
            await pilot.press("f2")
            await pilot.pause()
            assert app._focus_mode == "palette"
            await pilot.press("f2")
            await pilot.pause()
            assert app._focus_mode == "dashboard"

            await pilot.press("c")
            await pilot.pause()
            assert prompt.value == "c"
            assert app._collapsed_section_ids == set()

            await pilot.press("escape")
            await pilot.pause()
            assert prompt.value == ""

            await pilot.press("c", "o", "d", "e", "x")
            await pilot.pause()
            assert prompt.value == "codex"
            assert app._collapsed_section_ids == set()

            await pilot.press("escape")
            await pilot.pause()
            assert prompt.value == ""

            await pilot.press("e", "x", "e", "c")
            await pilot.pause()
            assert prompt.value == "exec"

            await pilot.press("escape")
            await pilot.pause()
            assert prompt.value == ""

            app._collapsed_section_ids.add("queue_daemon")
            await pilot.press("C")
            await pilot.pause()
            assert app._collapsed_section_ids == {"queue_daemon"}
            assert prompt.value == "C"

            await pilot.press("escape")
            await pilot.pause()
            assert prompt.value == ""

            app._collapsed_section_ids.add("queue_daemon")
            await pilot.press("shift+c")
            await pilot.pause()
            assert app._collapsed_section_ids == {"queue_daemon"}
            assert prompt.value == ""

            await pilot.press("escape")
            await pilot.pause()
            assert prompt.value == ""

            await pilot.press("/", "o", "r", "c", "h", "e", "s", "t", "r", "a", "t", "o", "r", "s")
            await pilot.press("enter")
            await pilot.pause()
            rendered = "\n".join(render_chat_message(message) for message in app._messages)
            assert "Orchestrators" in rendered
            assert "coding_orchestrator" in rendered

    asyncio.run(run_pilot())


def test_tui_palette_enter_activates_safe_actions_without_chat_or_process(tmp_path) -> None:
    pytest.importorskip("textual")
    from textual.widgets import Static, TextArea

    async def run_pilot() -> None:
        app = create_harness_app(tmp_path)
        async with app.run_test(size=(100, 40)) as pilot:
            prompt = app.query_one("#prompt", TextArea)
            slash_status = app.query_one("#slash-status", Static)
            initial_messages = len(app._messages)

            await pilot.press("ctrl+p")
            await pilot.press("s", "e", "s", "s", "i", "o", "n", "s")
            await pilot.pause()
            assert app._focus_mode == "palette"
            assert prompt.value == "sessions"

            await pilot.press("enter")
            await pilot.pause()
            assert app._focus_mode == "dashboard"
            assert app._section_cursor_index == 5
            assert prompt.value == ""
            assert len(app._messages) == initial_messages
            assert app._request_in_flight is False
            assert app._latest_palette_activation["entry_id"] == "sessions.list"
            assert app._latest_palette_activation["source"] == "palette_enter"
            assert app._latest_palette_activation["enter_consumed"] is True
            assert app._latest_palette_activation["chat_submitted"] is False
            assert app._latest_palette_activation["slash_suggestion_inserted"] is False
            assert app._latest_palette_activation["command_started"] is False
            assert app._latest_palette_activation["provider_started"] is False
            assert app._latest_palette_activation["shell_started"] is False
            assert app._latest_palette_activation["adapter_started"] is False
            assert app._latest_palette_activation["child_process_started"] is False
            assert app._latest_palette_activation["process_started"] is False
            assert app._latest_palette_activation["filesystem_modified"] is False
            assert app._latest_palette_activation["permission_granting"] is False
            assert app._latest_palette_activation["authority_granting"] is False
            assert app._latest_palette_activation["session_message_created"] is False

            await pilot.press("ctrl+p")
            await pilot.press("e", "x", "e", "c", "u", "t", "e", "-", "r", "e", "a", "d", "-", "o", "n", "l", "y")
            await pilot.pause()
            assert app._focus_mode == "palette"
            assert prompt.value == "execute-read-only"

            await pilot.press("enter")
            await pilot.pause()
            assert app._focus_mode == "palette"
            assert app._section_cursor_index == 5
            assert prompt.value == "execute-read-only"
            assert len(app._messages) == initial_messages
            assert app._request_in_flight is False
            assert app._latest_palette_activation["entry_id"] == "registered_adapters.execute_read_only"
            assert app._latest_palette_activation["activation_kind"] == "manual_command"
            assert app._latest_palette_activation["ui_action_applied"] is False
            assert app._latest_palette_activation["source"] == "palette_enter"
            assert app._latest_palette_activation["enter_consumed"] is True
            assert app._latest_palette_activation["chat_submitted"] is False
            assert app._latest_palette_activation["slash_suggestion_inserted"] is False
            assert app._latest_palette_activation["blocked_reasons"] == ["manual_command_preview_only"]
            assert app._latest_palette_activation["command_started"] is False
            assert app._latest_palette_activation["provider_started"] is False
            assert app._latest_palette_activation["shell_started"] is False
            assert app._latest_palette_activation["adapter_started"] is False
            assert app._latest_palette_activation["child_process_started"] is False
            assert app._latest_palette_activation["process_started"] is False
            assert app._latest_palette_activation["filesystem_modified"] is False
            assert app._latest_palette_activation["permission_granting"] is False
            assert app._latest_palette_activation["authority_granting"] is False
            assert app._latest_palette_activation["session_message_created"] is False
            assert "Manual preview only:" in str(slash_status.content)
            assert "harness daemon execute-read-only" in str(slash_status.content)

            await pilot.press("escape")
            await pilot.press("d", "o", "e", "s", "-", "n", "o", "t", "-", "e", "x", "i", "s", "t")
            await pilot.pause()
            assert app._focus_mode == "palette"
            await pilot.press("enter")
            await pilot.pause()
            assert app._focus_mode == "palette"
            assert prompt.value == "does-not-exist"
            assert len(app._messages) == initial_messages
            assert app._request_in_flight is False
            assert app._latest_palette_activation["activation_kind"] == "missing"
            assert app._latest_palette_activation["blocked_reasons"] == ["palette_entry_not_found"]
            assert app._latest_palette_activation["enter_consumed"] is True
            assert app._latest_palette_activation["chat_submitted"] is False
            assert app._latest_palette_activation["slash_suggestion_inserted"] is False
            assert app._latest_palette_activation["process_started"] is False
            assert "No matching palette action." in str(slash_status.content)

    asyncio.run(run_pilot())


def test_tui_phase2_multiline_composer_session_rail_and_history(tmp_path, monkeypatch) -> None:
    pytest.importorskip("textual")
    from textual.widgets import Static, TextArea

    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    (tmp_path / "README.md").write_text("attached context\n", encoding="utf-8")
    store = SQLiteStore(tmp_path)
    session = store.create_session(title="Composer session", agent_id="plan", raw_model_ref="codex_cli/gpt-5.5")
    message = store.append_session_message(session.id, "user", "Use attached context")
    store.append_session_part(
        session.id,
        message.id,
        "artifact_ref",
        metadata={
            "attachment_kind": "file_ref",
            "path": "README.md",
            "resolved_path": str(tmp_path / "README.md"),
        },
    )

    def fake_handle_chat_input(request, project_root, chat_state, progress_callback=None):
        assert request == "first line\nsecond line"
        return {"ok": True, "kind": "fake", "title": "Assistant", "lines": ["done"]}

    monkeypatch.setattr("harness.chat.handle_chat_input", fake_handle_chat_input)

    async def run_pilot() -> None:
        app = create_harness_app(tmp_path)
        async with app.run_test(size=(120, 44)) as pilot:
            prompt = app.query_one("#prompt", TextArea)
            composer_status = app.query_one("#composer-status", Static)
            composer_footer = app.query_one("#composer-footer", Static)
            session_detail = app.query_one("#session-pane-detail", Static)

            assert "Model codex_cli/gpt-5.5" in str(composer_status.content)
            assert "Agent plan" in str(composer_status.content)
            assert "attachments 1" in str(composer_status.content)
            assert "ctx " in str(composer_status.content)
            assert "Models: /models or ctrl+x m" not in str(composer_status.content)
            assert "Select: /model <number|name>" not in str(composer_status.content)
            assert f"Session: {session.id}" not in str(composer_status.content)
            assert "Enter send · Shift+Enter newline · Ctrl+X M models · / commands · ? shortcuts" in str(
                composer_footer.content
            )
            assert "Composer session" in str(session_detail.content)
            assert session.id not in str(session_detail.content)

            for char in "first line":
                await pilot.press(char)
            await pilot.press("shift+enter")
            for char in "second line":
                await pilot.press(char)
            await pilot.pause()
            assert prompt.value == "first line\nsecond line"

            await pilot.press("enter")
            await pilot.pause()
            assert app._prompt_history == ["first line\nsecond line"]
            assert prompt.value == ""

            await pilot.pause(0.5)
            assert app._request_in_flight is False
            await pilot.press("ctrl+up")
            await pilot.pause()
            assert prompt.value == "first line\nsecond line"
            await pilot.press("ctrl+down")
            await pilot.pause()
            assert prompt.value == "first line\nsecond line"

    asyncio.run(run_pilot())


def test_tui_composer_ctrl_m_enter_alias_submits_chat(tmp_path, monkeypatch) -> None:
    pytest.importorskip("textual")
    from textual.widgets import TextArea

    seen_requests: list[str] = []

    def fake_handle_chat_input(request, project_root, chat_state, progress_callback=None):
        seen_requests.append(request)
        return {"ok": True, "kind": "fake", "title": "Assistant", "lines": ["done"]}

    monkeypatch.setattr("harness.chat.handle_chat_input", fake_handle_chat_input)

    async def run_pilot() -> None:
        app = create_harness_app(tmp_path)
        async with app.run_test(size=(100, 40)) as pilot:
            prompt = app.query_one("#prompt", TextArea)

            for char in "hello":
                await pilot.press(char)
            await pilot.press("ctrl+m")
            await pilot.pause(0.5)

            assert seen_requests == ["hello"]
            assert app._prompt_history == ["hello"]
            assert prompt.value == ""
            assert app._request_in_flight is False

    asyncio.run(run_pilot())


def test_tui_composer_ctrl_j_inserts_newline(tmp_path, monkeypatch) -> None:
    pytest.importorskip("textual")
    from textual.widgets import TextArea

    def fake_handle_chat_input(request, project_root, chat_state, progress_callback=None):
        assert request == "alpha\nbeta"
        return {"ok": True, "kind": "fake", "title": "Assistant", "lines": ["done"]}

    monkeypatch.setattr("harness.chat.handle_chat_input", fake_handle_chat_input)

    async def run_pilot() -> None:
        app = create_harness_app(tmp_path)
        async with app.run_test(size=(120, 44)) as pilot:
            prompt = app.query_one("#prompt", TextArea)

            for char in "alpha":
                await pilot.press(char)
            await pilot.press("ctrl+j")
            for char in "beta":
                await pilot.press(char)
            await pilot.pause()

            assert prompt.value == "alpha\nbeta"
            assert app._prompt_history == []

            await pilot.press("enter")
            await pilot.pause()
            assert app._prompt_history == ["alpha\nbeta"]
            assert prompt.value == ""

            await pilot.pause(0.5)
            assert app._request_in_flight is False

    asyncio.run(run_pilot())


def test_tui_palette_enter_applies_safe_ui_control_actions(tmp_path) -> None:
    pytest.importorskip("textual")
    from textual.widgets import TextArea

    async def run_pilot() -> None:
        app = create_harness_app(tmp_path)
        async with app.run_test(size=(100, 40)) as pilot:
            prompt = app.query_one("#prompt", TextArea)
            initial_messages = len(app._messages)

            app._active_section_id = "queue"
            app._section_cursor_index = 4
            await pilot.press("ctrl+p")
            await pilot.press("t", "o", "g", "g", "l", "e", "-", "s", "e", "c", "t", "i", "o", "n")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert app._focus_mode == "palette"
            assert app._collapsed_section_ids == {"orchestrations"}
            assert prompt.value == ""
            assert len(app._messages) == initial_messages
            assert app._request_in_flight is False
            assert app._latest_palette_activation["entry_id"] == "ui_controls.toggle_section"
            assert app._latest_palette_activation["evidence_status"] == "ui_section_toggle_in_memory"
            assert app._latest_palette_activation["local_state_changes"]["changed_fields"] == ["active_section_id", "collapsed_section_ids"]
            assert app._latest_palette_activation["local_state_changes"]["creates_message"] is False
            assert app._latest_palette_activation["local_state_changes"]["starts_request"] is False
            assert app._latest_palette_activation["local_state_changes"]["executes_command"] is False
            assert app._latest_palette_activation["request_started"] is False
            assert app._latest_palette_activation["command_started"] is False
            assert app._latest_palette_activation["filesystem_modified"] is False
            assert app._latest_palette_activation["permission_granting"] is False

            await pilot.press("e", "x", "p", "a", "n", "d", "-", "a", "l", "l")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert app._focus_mode == "palette"
            assert app._collapsed_section_ids == set()
            assert prompt.value == ""
            assert len(app._messages) == initial_messages
            assert app._request_in_flight is False
            assert app._latest_palette_activation["entry_id"] == "ui_controls.expand_all"
            assert app._latest_palette_activation["evidence_status"] == "ui_sections_expanded_in_memory"
            assert app._latest_palette_activation["local_state_changes"]["changed_fields"] == ["collapsed_section_ids"]
            assert app._latest_palette_activation["request_started"] is False
            assert app._latest_palette_activation["command_started"] is False
            assert app._latest_palette_activation["filesystem_modified"] is False
            assert app._latest_palette_activation["permission_granting"] is False

            await pilot.press("f", "o", "c", "u", "s", "-", "d", "a", "s", "h", "b", "o", "a", "r", "d")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert app._focus_mode == "dashboard"
            assert prompt.value == ""
            assert len(app._messages) == initial_messages
            assert app._latest_palette_activation["entry_id"] == "ui_controls.dashboard_focus"
            assert app._latest_palette_activation["evidence_status"] == "ui_focus_in_memory"
            assert app._latest_palette_activation["local_state_changes"]["changed_fields"] == ["focus_mode"]
            assert app._latest_palette_activation["request_started"] is False
            assert app._latest_palette_activation["command_started"] is False
            assert app._latest_palette_activation["filesystem_modified"] is False
            assert app._latest_palette_activation["permission_granting"] is False

            await pilot.press("ctrl+p")
            await pilot.press("s", "e", "t", "t", "i", "n", "g", "s")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert app._focus_mode == "dashboard"
            assert app._section_cursor_index == 5
            assert prompt.value == ""
            assert len(app._messages) == initial_messages
            assert app._request_in_flight is False
            assert app._latest_palette_activation["entry_id"] == "ui_controls.settings"
            assert app._latest_palette_activation["evidence_status"] == "ui_focus_in_memory"
            assert app._latest_palette_activation["local_state_changes"]["changed_fields"] == [
                "focus_mode",
                "active_section_id",
                "active_section_index",
            ]
            assert app._latest_palette_activation["request_started"] is False
            assert app._latest_palette_activation["command_started"] is False
            assert app._latest_palette_activation["filesystem_modified"] is False
            assert app._latest_palette_activation["permission_granting"] is False

    asyncio.run(run_pilot())


def test_tui_settings_slash_command_routes_to_safe_ui_action(tmp_path) -> None:
    pytest.importorskip("textual")
    from textual.widgets import Static, TextArea

    async def run_pilot() -> None:
        app = create_harness_app(tmp_path)
        async with app.run_test(size=(100, 40)) as pilot:
            prompt = app.query_one("#prompt", TextArea)
            slash_status = app.query_one("#slash-status", Static)
            initial_messages = len(app._messages)

            await pilot.press("/", "s", "e", "t", "t", "i", "n", "g", "s")
            await pilot.pause()
            assert prompt.value == "/settings"
            assert "/settings" in str(slash_status.content)

            await pilot.press("enter")
            await pilot.pause()
            assert app._focus_mode == "dashboard"
            assert app._section_cursor_index == 5
            assert prompt.value == ""
            assert len(app._messages) == initial_messages
            assert app._request_in_flight is False
            assert app._latest_palette_activation["entry_id"] == "ui_controls.settings"
            assert app._latest_palette_activation["source"] == "slash"
            assert app._latest_palette_activation["slash"] == "/settings"
            assert app._latest_palette_activation["slash_consumed"] is True
            assert app._latest_palette_activation["chat_submitted"] is False
            assert app._latest_palette_activation["model_request_started"] is False
            assert app._latest_palette_activation["slash_suggestion_inserted"] is False
            assert app._latest_palette_activation["activation_kind"] == "ui_action"
            assert app._latest_palette_activation["ui_action_applied"] is True
            assert app._latest_palette_activation["evidence_status"] == "ui_focus_in_memory"
            assert app._latest_palette_activation["policy_boundary"]["kind"] == "safe_ui_activation"
            assert app._latest_palette_activation["policy_boundary"]["command_execution_allowed"] is False
            assert app._latest_palette_activation["policy_boundary"]["provider_call_allowed"] is False
            assert app._latest_palette_activation["policy_boundary"]["filesystem_mutation_allowed"] is False
            assert app._latest_palette_activation["policy_boundary"]["permission_grant_allowed"] is False
            assert app._latest_palette_activation["request_started"] is False
            assert app._latest_palette_activation["command_started"] is False
            assert app._latest_palette_activation["provider_started"] is False
            assert app._latest_palette_activation["shell_started"] is False
            assert app._latest_palette_activation["adapter_started"] is False
            assert app._latest_palette_activation["child_process_started"] is False
            assert app._latest_palette_activation["process_started"] is False
            assert app._latest_palette_activation["filesystem_modified"] is False
            assert app._latest_palette_activation["permission_granting"] is False
            assert app._latest_palette_activation["authority_granting"] is False
            assert app._latest_palette_activation["session_message_created"] is False

    asyncio.run(run_pilot())


def test_tui_session_rail_uses_topic_titles_and_in_memory_navigation(tmp_path) -> None:
    pytest.importorskip("textual")
    from textual.widgets import ListView, Static, TextArea

    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    first = store.create_session(title="Harness chat", agent_id="plan")
    first_message = store.append_session_message(first.id, "user", "make the session rail easier to navigate")
    store.append_session_part(first.id, first_message.id, "text", text="make the session rail easier to navigate")
    second = store.create_session(title="Harness chat", agent_id="build")
    second_message = store.append_session_message(second.id, "user", "summarize local model selection work")
    store.append_session_part(second.id, second_message.id, "text", text="summarize local model selection work")

    dashboard = build_tui_dashboard(tmp_path)
    rail = _render_session_rail(dashboard, selected_index=1)

    assert dashboard["recent_sessions"][0]["display_title"] == "Summarize local model selection work"
    assert dashboard["recent_sessions"][1]["display_title"] == "Make the session rail easier to navigate"
    assert "Summarize local model select" in rail
    assert "Make the session rail easier" in rail
    assert f" {first.id} " not in rail
    assert "Selected: Make the session rail easier to navigate" in rail

    async def run_pilot() -> None:
        app = create_harness_app(tmp_path)
        async with app.run_test(size=(120, 44)) as pilot:
            session_detail = app.query_one("#session-pane-detail", Static)
            session_list = app.query_one("#session-list", ListView)
            prompt = app.query_one("#prompt", TextArea)
            assert "Summarize local model selection work" in str(session_detail.content)
            assert second.id not in str(session_detail.content)

            app.action_session_next()
            await pilot.pause()
            assert app._chat_state.session_id == first.id
            assert "Make the session rail easier to navigate" in str(session_detail.content)
            assert first.id not in str(session_detail.content)

            app.action_session_previous()
            await pilot.pause()
            assert app._chat_state.session_id == second.id
            assert "Summarize local model selection work" in str(session_detail.content)
            assert second.id not in str(session_detail.content)

            await pilot.press("ctrl+right")
            await pilot.pause()
            assert app._chat_state.session_id == first.id
            assert "Make the session rail easier to navigate" in str(session_detail.content)
            assert first.id not in str(session_detail.content)

            await pilot.press("ctrl+x", "s")
            await pilot.pause()
            assert app._left_pane_focused is True
            assert session_list.has_focus

            await pilot.press("/")
            for char in "summarize":
                await pilot.press(char)
            await pilot.press("enter")
            await pilot.pause()
            assert app._session_query == "summarize"
            assert app._selected_session_id == second.id

            await pilot.press("n")
            await pilot.pause()
            assert app._chat_state.session_id is not None
            created_session_id = app._chat_state.session_id
            assert store.get_session(created_session_id).title == "New session"

            await pilot.press("e")
            for char in "renamed session":
                await pilot.press(char)
            await pilot.press("enter")
            await pilot.pause()
            assert store.get_session(created_session_id).title == "renamed session"

            await pilot.press("g", "down", "enter")
            await pilot.pause()
            assert store.get_session(created_session_id).agent_id == "plan"

            app.action_fork_selected_session()
            await pilot.pause()
            forked_session_id = app._selected_session_id
            assert store.get_session(forked_session_id).parent_session_id == created_session_id
            app._selected_session_id = created_session_id
            app._chat_state.session_id = created_session_id
            app._render_current_view()
            await pilot.pause()

            await pilot.press("a")
            await pilot.pause()
            assert store.get_session(created_session_id).status.value == "archived"

            app._session_filter = "archived"
            app._selected_session_id = created_session_id
            app._render_current_view()
            await pilot.pause()
            await pilot.press("r")
            await pilot.pause()
            assert store.get_session(created_session_id).status.value == "active"

            await pilot.press("d")
            await pilot.pause()
            assert app._dialog_kind == "session_delete"
            await pilot.press("escape")
            await pilot.pause()
            assert app._dialog_visible is False
            assert store.get_session(app._selected_session_id).status.value == "active"

            purged_session_id = app._selected_session_id
            await pilot.press("d")
            for char in "DELETE":
                await pilot.press(char)
            await pilot.press("enter")
            await pilot.pause()
            try:
                store.get_session(purged_session_id)
            except KeyError:
                pass
            else:
                raise AssertionError("confirmed hard delete should purge the selected session")

    asyncio.run(run_pilot())


def test_tui_session_pane_projection_filters_and_counts(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    active = store.create_session(title="Harness chat", agent_id="plan")
    active_message = store.append_session_message(active.id, "user", "inspect dynamic session pane")
    store.append_session_part(active.id, active_message.id, "text", text="inspect dynamic session pane")
    running = store.create_session(title="Run session", status=SessionStatus.RUNNING)
    waiting = store.create_session(title="Wait session", status=SessionStatus.WAITING_APPROVAL)
    archived = store.archive_session(store.create_session(title="Archived session").id)

    projection = build_session_pane_projection(tmp_path, selected_session_id=active.id, status_filter="open", query="dynamic")
    running_projection = build_session_pane_projection(tmp_path, status_filter="running")
    archived_projection = build_session_pane_projection(tmp_path, status_filter="archived")

    assert projection["schema_version"] == "harness.session_pane/v1"
    assert projection["counts"]["total"] == 4
    assert projection["counts"]["open"] == 3
    assert projection["counts"]["running"] == 2
    assert projection["counts"]["waiting_approval"] == 1
    assert projection["counts"]["archived"] == 1
    assert projection["counts"]["filtered"] == 1
    assert projection["sessions"][0]["id"] == active.id
    assert projection["sessions"][0]["display_title"] == "Inspect dynamic session pane"
    assert projection["sessions"][0]["message_count"] == 1
    assert projection["sessions"][0]["can_archive"] is True
    assert {session["id"] for session in running_projection["sessions"]} == {running.id, waiting.id}
    assert all(session["can_abort"] is True for session in running_projection["sessions"])
    assert archived_projection["sessions"][0]["id"] == archived.id
    assert archived_projection["sessions"][0]["can_restore"] is True


def test_cli_sessions_restore_and_purge_session_only(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    session = store.create_session(title="CLI purge")
    message = store.append_session_message(session.id, "user", "delete this")
    store.append_session_part(session.id, message.id, "text", text="delete this")
    run = store.create_run("cli linked run", "test", session_id=session.id)
    task = store.create_task("cli linked task", session_id=session.id)

    archived = runner.invoke(app, ["sessions", "delete", session.id, "--project", str(tmp_path), "--output", "json"])
    restored = runner.invoke(app, ["sessions", "restore", session.id, "--project", str(tmp_path), "--output", "json"])
    blocked = runner.invoke(app, ["sessions", "purge", session.id, "--project", str(tmp_path), "--output", "json"])
    assert archived.exit_code == 0, archived.output
    assert json.loads(archived.output)["behavior"] == "archive"
    assert restored.exit_code == 0, restored.output
    assert json.loads(restored.output)["session"]["status"] == "active"
    assert blocked.exit_code == 1
    assert json.loads(blocked.output)["hard_deleted"] is False
    assert store.get_session(session.id).status == SessionStatus.ACTIVE

    purged = runner.invoke(
        app,
        [
            "sessions",
            "purge",
            session.id,
            "--confirm",
            session.id,
            "--project",
            str(tmp_path),
            "--output",
            "json",
        ],
    )

    assert purged.exit_code == 0, purged.output
    purge_payload = json.loads(purged.output)
    assert purge_payload["hard_deleted"] is True
    assert purge_payload["deletion_counts"]["session_rows"] == 1
    assert store.get_run(run.id).session_id is None
    assert store.get_task(task.id).session_id is None
    try:
        store.get_session(session.id)
    except KeyError:
        pass
    else:
        raise AssertionError("purged session should not remain")


def test_tui_safe_slash_activation_persists_session_event_when_active(tmp_path) -> None:
    pytest.importorskip("textual")
    from textual.widgets import TextArea

    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    session = store.create_session(title="Activation session")

    async def run_pilot() -> None:
        app = create_harness_app(tmp_path)
        async with app.run_test(size=(100, 40)) as pilot:
            prompt = app.query_one("#prompt", TextArea)
            initial_messages = len(app._messages)

            await pilot.press("/", "s", "e", "t", "t", "i", "n", "g", "s")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()

            assert app._section_cursor_index == 5
            assert prompt.value == ""
            assert len(app._messages) == initial_messages
            assert app._request_in_flight is False
            assert app._latest_palette_activation["session_event_persisted"] is True
            assert app._latest_palette_activation["session_id"] == session.id

    asyncio.run(run_pilot())

    events = SQLiteStore(tmp_path).list_session_store_events(session.id)
    activation_events = [event for event in events if event.kind == "tui.ui_activation.applied"]
    assert len(activation_events) == 1
    payload = activation_events[0].payload
    assert payload["source"] == "slash"
    assert payload["entry_id"] == "ui_controls.settings"
    assert payload["ui_action_applied"] is True
    assert payload["command_started"] is False
    assert payload["provider_started"] is False
    assert payload["shell_started"] is False
    assert payload["adapter_started"] is False
    assert payload["child_process_started"] is False
    assert payload["process_started"] is False
    assert payload["filesystem_modified"] is False
    assert payload["permission_granting"] is False
    assert payload["authority_granting"] is False
    assert payload["session_message_created"] is False
    assert payload["evidence_status"] == "ui_only_persisted"
    assert payload["policy_boundary"]["kind"] == "safe_ui_activation"
    assert payload["policy_boundary"]["provider_call_allowed"] is False
    assert payload["policy_boundary"]["shell_allowed"] is False
    assert payload["policy_boundary"]["adapter_dispatch_allowed"] is False
    assert payload["policy_boundary"]["child_process_allowed"] is False
    assert payload["policy_boundary"]["session_message_allowed"] is False
    assert payload["policy_boundary"]["authority_grant_allowed"] is False
    assert payload["blocked_reasons"] == []


def test_tui_safe_slash_commands_focus_dashboard_sections_without_chat(tmp_path) -> None:
    pytest.importorskip("textual")
    from textual.widgets import TextArea

    async def run_pilot() -> None:
        app = create_harness_app(tmp_path)
        async with app.run_test(size=(100, 40)) as pilot:
            prompt = app.query_one("#prompt", TextArea)
            initial_messages = len(app._messages)

            for slash, section_index, entry_id in [
                ("/home", 5, "orientation.home"),
                ("/sessions", 5, "sessions.list"),
                ("/tasks", 0, "objectives_tasks.list_tasks"),
                ("/runs", 4, "runtime_evidence.runs"),
            ]:
                prompt.value = ""
                await pilot.press(*list(slash))
                await pilot.pause()
                assert prompt.value == slash

                await pilot.press("enter")
                await pilot.pause()
                assert app._focus_mode == "dashboard"
                assert app._section_cursor_index == section_index
                assert prompt.value == ""
                assert len(app._messages) == initial_messages
                assert app._request_in_flight is False
                assert app._latest_palette_activation["entry_id"] == entry_id
                assert app._latest_palette_activation["source"] == "slash"
                assert app._latest_palette_activation["slash"] == slash
                assert app._latest_palette_activation["slash_consumed"] is True
                assert app._latest_palette_activation["chat_submitted"] is False
                assert app._latest_palette_activation["model_request_started"] is False
                assert app._latest_palette_activation["slash_suggestion_inserted"] is False
                assert app._latest_palette_activation["activation_kind"] == "ui_action"
                assert app._latest_palette_activation["ui_action_applied"] is True
                assert app._latest_palette_activation["evidence_status"] == "ui_focus_in_memory"
                assert app._latest_palette_activation["local_state_changes"]["changed_fields"] == [
                    "focus_mode",
                    "active_section_id",
                    "active_section_index",
                ]
                assert app._latest_palette_activation["policy_boundary"]["kind"] == "safe_ui_activation"
                assert app._latest_palette_activation["policy_boundary"]["command_execution_allowed"] is False
                assert app._latest_palette_activation["policy_boundary"]["provider_call_allowed"] is False
                assert app._latest_palette_activation["policy_boundary"]["filesystem_mutation_allowed"] is False
                assert app._latest_palette_activation["policy_boundary"]["permission_grant_allowed"] is False
                assert app._latest_palette_activation["request_started"] is False
                assert app._latest_palette_activation["command_started"] is False
                assert app._latest_palette_activation["provider_started"] is False
                assert app._latest_palette_activation["shell_started"] is False
                assert app._latest_palette_activation["adapter_started"] is False
                assert app._latest_palette_activation["child_process_started"] is False
                assert app._latest_palette_activation["process_started"] is False
                assert app._latest_palette_activation["filesystem_modified"] is False
                assert app._latest_palette_activation["permission_granting"] is False
                assert app._latest_palette_activation["authority_granting"] is False
                assert app._latest_palette_activation["session_message_created"] is False

    asyncio.run(run_pilot())


def test_tui_safe_ui_control_slash_commands_mutate_only_local_state(tmp_path) -> None:
    pytest.importorskip("textual")
    from textual.widgets import TextArea

    async def run_pilot() -> None:
        app = create_harness_app(tmp_path)
        async with app.run_test(size=(100, 40)) as pilot:
            prompt = app.query_one("#prompt", TextArea)
            initial_messages = len(app._messages)

            def assert_safe_ui_control(slash: str, entry_id: str, changed_fields: list[str]) -> None:
                assert app._latest_palette_activation["entry_id"] == entry_id
                assert app._latest_palette_activation["source"] == "slash"
                assert app._latest_palette_activation["slash"] == slash
                assert app._latest_palette_activation["slash_consumed"] is True
                assert app._latest_palette_activation["chat_submitted"] is False
                assert app._latest_palette_activation["model_request_started"] is False
                assert app._latest_palette_activation["slash_suggestion_inserted"] is False
                assert app._latest_palette_activation["activation_kind"] == "ui_action"
                assert app._latest_palette_activation["ui_action_applied"] is True
                assert app._latest_palette_activation["local_state_changes"]["changed_fields"] == changed_fields
                assert app._latest_palette_activation["local_state_changes"]["creates_message"] is False
                assert app._latest_palette_activation["local_state_changes"]["starts_request"] is False
                assert app._latest_palette_activation["local_state_changes"]["executes_command"] is False
                assert app._latest_palette_activation["local_state_changes"]["mutates_filesystem"] is False
                assert app._latest_palette_activation["local_state_changes"]["grants_permission"] is False
                assert app._latest_palette_activation["policy_boundary"]["kind"] == "safe_ui_activation"
                assert app._latest_palette_activation["policy_boundary"]["command_execution_allowed"] is False
                assert app._latest_palette_activation["policy_boundary"]["provider_call_allowed"] is False
                assert app._latest_palette_activation["policy_boundary"]["shell_allowed"] is False
                assert app._latest_palette_activation["policy_boundary"]["adapter_dispatch_allowed"] is False
                assert app._latest_palette_activation["policy_boundary"]["child_process_allowed"] is False
                assert app._latest_palette_activation["policy_boundary"]["filesystem_mutation_allowed"] is False
                assert app._latest_palette_activation["policy_boundary"]["permission_grant_allowed"] is False
                assert app._latest_palette_activation["policy_boundary"]["authority_grant_allowed"] is False
                assert app._latest_palette_activation["request_started"] is False
                assert app._latest_palette_activation["command_started"] is False
                assert app._latest_palette_activation["provider_started"] is False
                assert app._latest_palette_activation["shell_started"] is False
                assert app._latest_palette_activation["adapter_started"] is False
                assert app._latest_palette_activation["child_process_started"] is False
                assert app._latest_palette_activation["process_started"] is False
                assert app._latest_palette_activation["filesystem_modified"] is False
                assert app._latest_palette_activation["permission_granting"] is False
                assert app._latest_palette_activation["authority_granting"] is False
                assert app._latest_palette_activation["session_message_created"] is False

            await pilot.press("/", "p", "a", "l", "e", "t", "t", "e")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert app._focus_mode == "palette"
            assert prompt.value == ""
            assert len(app._messages) == initial_messages
            assert app._request_in_flight is False
            assert_safe_ui_control("/palette", "ui_controls.palette_focus", ["focus_mode"])

            await pilot.press("/", "d", "a", "s", "h", "b", "o", "a", "r", "d")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert app._focus_mode == "dashboard"
            assert prompt.value == ""
            assert len(app._messages) == initial_messages
            assert_safe_ui_control("/dashboard", "ui_controls.dashboard_focus", ["focus_mode"])

            await pilot.press("/", "t", "o", "g", "g", "l", "e", "-", "s", "e", "c", "t", "i", "o", "n")
            await pilot.pause()
            app._active_section_id = "queue"
            app._section_cursor_index = 4
            await pilot.press("enter")
            await pilot.pause()
            assert app._collapsed_section_ids == {"orchestrations"}
            assert prompt.value == ""
            assert len(app._messages) == initial_messages
            assert_safe_ui_control("/toggle-section", "ui_controls.toggle_section", ["active_section_id", "collapsed_section_ids"])

            await pilot.press("/", "e", "x", "p", "a", "n", "d", "-", "a", "l", "l")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert app._collapsed_section_ids == set()
            assert prompt.value == ""
            assert len(app._messages) == initial_messages
            assert_safe_ui_control("/expand-all", "ui_controls.expand_all", ["collapsed_section_ids"])

            await pilot.press("/", "c", "l", "e", "a", "r")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert app._focus_mode == "dashboard"
            assert prompt.value == ""
            assert len(app._messages) == initial_messages
            assert app._request_in_flight is False
            assert_safe_ui_control("/clear", "ui_controls.clear_search", ["focus_mode", "query"])

    asyncio.run(run_pilot())


def test_tui_phase3_agent_selector_switches_build_and_plan_without_execution(tmp_path) -> None:
    pytest.importorskip("textual")
    from textual.widgets import Static, TextArea

    async def run_pilot() -> None:
        app = create_harness_app(tmp_path)
        async with app.run_test(size=(120, 42)) as pilot:
            prompt = app.query_one("#prompt", TextArea)
            composer_status = app.query_one("#composer-status", Static)
            initial_messages = len(app._messages)

            await pilot.press("ctrl+p")
            prompt.value = "select build agent"
            await pilot.press("enter")
            await pilot.pause()

            assert app._selected_agent_id == "build"
            assert app._latest_palette_activation["entry_id"] == "native_agents.select_build"
            assert app._latest_palette_activation["process_started"] is False
            assert app._latest_palette_activation["filesystem_modified"] is False
            assert app._latest_palette_activation["permission_granting"] is False
            assert app._request_in_flight is False
            assert len(app._messages) == initial_messages
            assert "Agent build" in str(composer_status.content)

            prompt.value = "select plan agent"
            await pilot.press("enter")
            await pilot.pause()

            assert app._selected_agent_id == "plan"
            assert app._latest_palette_activation["entry_id"] == "native_agents.select_plan"
            assert app._latest_palette_activation["process_started"] is False
            assert app._latest_palette_activation["provider_started"] is False
            assert app._latest_palette_activation["shell_started"] is False
            assert len(app._messages) == initial_messages
            assert "Agent plan" in str(composer_status.content)

    asyncio.run(run_pilot())


def test_tui_safe_slash_activation_failure_does_not_crash(tmp_path, monkeypatch) -> None:
    pytest.importorskip("textual")
    from textual.widgets import Static, TextArea
    import harness.tui as tui_module

    def fail_activation(*_args, **_kwargs):
        raise RuntimeError("simulated palette failure")

    monkeypatch.setattr(tui_module, "activate_command_palette_entry", fail_activation)

    async def run_pilot() -> None:
        app = create_harness_app(tmp_path)
        async with app.run_test(size=(100, 40)) as pilot:
            prompt = app.query_one("#prompt", TextArea)
            slash_status = app.query_one("#slash-status", Static)
            initial_messages = len(app._messages)

            await pilot.press("/", "s", "e", "t", "t", "i", "n", "g", "s")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()

            assert app._latest_palette_activation["ok"] is False
            assert app._latest_palette_activation["activation_kind"] == "slash_error"
            assert app._latest_palette_activation["blocked_reasons"] == ["slash_activation_error"]
            assert app._latest_palette_activation["slash"] == "/settings"
            assert app._latest_palette_activation["slash_consumed"] is True
            assert app._latest_palette_activation["chat_submitted"] is False
            assert app._latest_palette_activation["process_started"] is False
            assert app._latest_palette_activation["filesystem_modified"] is False
            assert app._latest_palette_activation["permission_granting"] is False
            assert "Slash command failed safely: simulated palette failure" in str(slash_status.content)
            assert prompt.value == "/settings"
            assert len(app._messages) == initial_messages
            assert app._request_in_flight is False

    asyncio.run(run_pilot())


def test_tui_slash_commands_cover_palette_templates_without_execution() -> None:
    palette = build_command_palette()
    slash_commands = build_slash_commands(palette)

    names = [command["name"] for command in slash_commands["commands"]]
    assert slash_commands["schema_version"] == "harness.tui_slash_commands/v1"
    assert len(names) == len(set(names))
    assert {
        "home",
        "clear",
        "palette",
        "dashboard",
        "toggle-section",
        "expand-all",
        "quickstart",
        "scaffold",
        "validate",
        "agents",
        "specs",
        "tasks",
        "settings",
        "theme",
        "dark-mode",
        "light-mode",
        "lease",
        "inspect-lease",
        "execute-read-only",
        "execute",
        "plan-task",
        "runs",
        "models",
        "model",
        "policy",
        "artifacts",
        "wheel",
    } <= set(names)
    assert all(command["slash"].startswith("/") for command in slash_commands["commands"])
    assert all(
        set(command)
        >= {
            "name",
            "slash",
            "entry_id",
            "group_id",
            "title",
            "description",
            "command",
            "mutates_when_run",
            "safety_note",
            "activation",
        }
        for command in slash_commands["commands"]
    )
    settings = next(command for command in slash_commands["commands"] if command["name"] == "settings")
    theme = next(command for command in slash_commands["commands"] if command["name"] == "theme")
    dark_mode = next(command for command in slash_commands["commands"] if command["name"] == "dark-mode")
    light_mode = next(command for command in slash_commands["commands"] if command["name"] == "light-mode")
    home = next(command for command in slash_commands["commands"] if command["name"] == "home")
    sessions = next(command for command in slash_commands["commands"] if command["name"] == "sessions")
    tasks = next(command for command in slash_commands["commands"] if command["name"] == "tasks")
    runs = next(command for command in slash_commands["commands"] if command["name"] == "runs")
    models = next(command for command in slash_commands["commands"] if command["name"] == "models")
    model = next(command for command in slash_commands["commands"] if command["name"] == "model")
    clear = next(command for command in slash_commands["commands"] if command["name"] == "clear")
    palette_focus = next(command for command in slash_commands["commands"] if command["name"] == "palette")
    dashboard_focus = next(command for command in slash_commands["commands"] if command["name"] == "dashboard")
    toggle_section = next(command for command in slash_commands["commands"] if command["name"] == "toggle-section")
    expand_all = next(command for command in slash_commands["commands"] if command["name"] == "expand-all")
    assert settings["entry_id"] == "ui_controls.settings"
    assert settings["activation"]["kind"] == "ui_action"
    assert theme["entry_id"] == "ui_controls.theme_cycle"
    assert theme["activation"]["kind"] == "ui_action"
    assert dark_mode["activation"]["kind"] == "ui_action"
    assert light_mode["activation"]["kind"] == "ui_action"
    assert home["activation"]["kind"] == "ui_action"
    assert sessions["activation"]["kind"] == "ui_action"
    assert tasks["activation"]["kind"] == "ui_action"
    assert runs["activation"]["kind"] == "ui_action"
    assert clear["activation"]["kind"] == "ui_action"
    assert palette_focus["activation"]["kind"] == "ui_action"
    assert dashboard_focus["activation"]["kind"] == "ui_action"
    assert toggle_section["activation"]["kind"] == "ui_action"
    assert expand_all["activation"]["kind"] == "ui_action"
    assert models["activation"]["kind"] == "model_list"
    assert model["activation"]["kind"] == "session_model_selection"

    read_only = filter_slash_commands(slash_commands, "/execute-read-only")
    task_matches = filter_slash_commands(slash_commands, "task")
    missing = filter_slash_commands(slash_commands, "does-not-exist")

    assert read_only["schema_version"] == "harness.tui_slash_command_filter/v1"
    assert [command["name"] for command in read_only["commands"]] == ["execute-read-only"]
    assert any(command["name"] == "tasks" for command in task_matches["commands"])
    assert missing["commands"] == []
    serialized = json.dumps(slash_commands)
    assert "api_key" not in serialized
    assert "OPENAI_API_KEY" not in serialized
    assert "base_url" not in serialized
    assert "subprocess" not in serialized


def test_tui_functionality_table_groups_commands_by_operator_workflow() -> None:
    table = build_functionality_table()
    rendered = render_functionality_table_dialog(table)
    filtered = filter_functionality_table(table, "dispatch")

    assert table["schema_version"] == "harness.tui_functionality_table/v1"
    assert [group["id"] for group in table["groups"]] == [
        "suggested",
        "session",
        "agent",
        "tasks",
        "adapters",
        "evidence",
        "provider",
        "system",
    ]
    assert any(row["title"] == "Switch model" and row["invoke"] == "ctrl+x m" for row in table["rows"])
    model_row = next(row for row in table["rows"] if row["id"] == "suggested.model")
    execute_row = next(row for row in table["rows"] if row["id"] == "adapters.execute")
    settings_row = next(row for row in table["rows"] if row["id"] == "system.settings")
    theme_row = next(row for row in table["rows"] if row["id"] == "system.theme")

    assert model_row["authority"] == "session metadata"
    assert model_row["status"] == "state"
    assert model_row["does_not"] == "call provider, execute model, hidden fallback"
    assert execute_row["authority"] == "registered dispatch"
    assert execute_row["status"] == "dispatch"
    assert settings_row["authority"] == "ui-only"
    assert settings_row["status"] == "ui"
    assert theme_row["title"] == "Switch theme"
    assert theme_row["invoke"] == "ctrl+x t"
    assert theme_row["authority"] == "ui-only"
    assert any(theme["id"] == "light" and theme["textual_theme"] == "harness-light" for theme in build_tui_settings_catalog()["themes"])
    assert any(theme["id"] == "system" and theme["textual_theme"] == "textual-light" for theme in THEME_DIALOG_ENTRIES)
    assert not any(row["id"] == "system.dark-mode" for row in table["rows"])
    assert not any(row["id"] == "system.light-mode" for row in table["rows"])
    assert "Switch to dark mode" not in rendered
    assert "Switch to light mode" not in rendered
    assert "Commands" in rendered
    assert "Suggested" in rendered
    assert "Authority" in rendered
    assert "[bold deep_sky_blue1]Commands" in rendered
    assert "[bold dark_orange3]Suggested" in rendered
    assert "[bold steel_blue1]Authority" in rendered
    assert "enter runs safe UI rows or stages command text" in rendered
    assert any(row["name"] == "execute" for row in filtered["rows"])
    serialized = json.dumps(table)
    assert "api_key" not in serialized
    assert "OPENAI_API_KEY" not in serialized
    assert "base_url" not in serialized


def test_tui_slash_command_suggestions_render_like_command_menu() -> None:
    slash_commands = build_slash_commands()

    all_suggestions = render_slash_command_suggestions(slash_commands, "/")
    execute_suggestions = render_slash_command_suggestions(slash_commands, "/execute")
    settings_suggestions = render_slash_command_suggestions(slash_commands, "/settings")
    model_suggestions = render_slash_command_suggestions(slash_commands, "/model")
    plain_text = render_slash_command_suggestions(slash_commands, "execute")
    missing = render_slash_command_suggestions(slash_commands, "/does-not-exist")
    selected_second = render_slash_command_suggestions(slash_commands, "/", selected_index=1)
    selected_after_first_page = render_slash_command_suggestions(slash_commands, "/", selected_index=8)

    assert "[bold blue]/help" in all_suggestions
    assert "[bold cyan]" not in all_suggestions
    assert "[bold blue]/home" in selected_second
    assert "... 1 previous. Keep using arrows to navigate." in selected_after_first_page
    assert "[bold blue]/scaffold" in selected_after_first_page
    assert "Print the MVP agent command sequence without running it." in all_suggestions
    assert "/execute-read-only" in execute_suggestions
    assert "/settings" in settings_suggestions
    assert "/models" in model_suggestions
    assert "/model" in model_suggestions
    assert "Select the active session model" in model_suggestions
    assert "Focus the read-only TUI settings catalog." in settings_suggestions
    assert "/home" not in execute_suggestions
    assert plain_text == ""
    assert "No slash commands match /does-not-exist" in missing


def test_tui_chat_slash_command_responses_are_templates_only() -> None:
    slash_commands = build_slash_commands()
    welcome = build_chat_welcome_message("/tmp/project")

    help_response = handle_slash_command("/help", slash_commands)
    command_response = handle_slash_command("/execute-read-only", slash_commands)
    plain_response = handle_slash_command("run something", slash_commands)
    unknown_response = handle_slash_command("/does-not-exist", slash_commands)

    assert help_response["schema_version"] == "harness.tui_chat_response/v1"
    assert "Project: /tmp/project" in render_chat_message(welcome)
    assert "o  o" not in render_chat_message(welcome)
    assert help_response["ok"] is True
    assert help_response["kind"] == "help"
    assert any("/execute-read-only" in line for line in help_response["messages"][0]["lines"])
    assert any("/models" in line for line in help_response["messages"][0]["lines"])
    assert any("/model" in line for line in help_response["messages"][0]["lines"])
    assert command_response["ok"] is True
    assert command_response["kind"] == "command_template"
    assert command_response["command"]["name"] == "execute-read-only"
    rendered = render_chat_message(command_response["messages"][0])
    assert "harness daemon execute-read-only task_lease_abc123 --project . --output json" in rendered
    assert "Mutates when run manually: True" in rendered
    assert "Compatibility command for the bounded read-only adapter when manually run." in rendered
    assert plain_response["kind"] == "plain_text_unsupported"
    assert unknown_response["kind"] == "unknown"

    serialized = json.dumps(
        {
            "help": help_response,
            "command": command_response,
            "plain": plain_response,
            "unknown": unknown_response,
        }
    )
    assert "api_key" not in serialized
    assert "OPENAI_API_KEY" not in serialized
    assert "base_url" not in serialized
    assert "subprocess" not in serialized
    assert "artifact contents" not in serialized


def test_cli_home_reports_initialized_project_dashboard_without_sensitive_output(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    task = runner.invoke(
        app,
        [
            "tasks",
            "add",
            "--title",
            "summarize repo",
            "--execution-adapter",
            "read_only_summary",
            "--task-type",
            "read_only_repo_summary",
            "--project",
            str(tmp_path),
            "--output",
            "json",
        ],
    )
    assert task.exit_code == 0, task.output
    tick = runner.invoke(app, ["daemon", "run-once", "--project", str(tmp_path), "--output", "json"])
    assert tick.exit_code == 0, tick.output
    created = runner.invoke(
        app,
        [
            "dev",
            "create-run",
            "--goal",
            "dashboard run",
            "--task-type",
            "phase_1a_test",
            "--project",
            str(tmp_path),
        ],
    )
    assert created.exit_code == 0, created.output

    result = runner.invoke(app, ["home", "--project", str(tmp_path), "--output", "json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema_version"] == "harness.home/v1"
    assert payload["ok"] is True
    assert payload["initialized"] is True
    assert payload["summary"]["tasks_total"] == 1
    assert payload["summary"]["active_leases"] == 1
    assert payload["summary"]["active_daemons"] == 1
    assert payload["summary"]["recent_runs"] == 1
    assert payload["task_status_counts"]["leased"] == 1
    assert payload["daemon"]["active_daemons"]
    assert payload["recent_runs"][0]["task_type"] == "phase_1a_test"
    serialized = json.dumps(payload)
    assert "api_key" not in serialized
    assert "OPENAI_API_KEY" not in serialized
    assert "base_url" not in serialized


def test_cli_quickstart_agent_prints_commands_without_mutation(tmp_path) -> None:
    result = runner.invoke(app, ["quickstart", "agent", "--project", str(tmp_path), "--output", "json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema_version"] == "harness.quickstart_agent/v1"
    assert payload["ok"] is True
    assert payload["initialized"] is False
    assert [step["id"] for step in payload["steps"]] == [
        "scaffold_agent",
        "validate_agent",
        "preview_agent",
        "init_project",
        "import_agent",
        "inspect_agent",
        "create_read_only_task",
        "lease_task",
        "inspect_lease",
        "execute_read_only",
    ]
    assert "harness agents scaffold my_agent" in payload["steps"][0]["command"]
    assert "harness daemon execute-read-only task_lease_..." in payload["steps"][-1]["command"]
    assert not (tmp_path / ".harness").exists()
    serialized = json.dumps(payload)
    assert "api_key" not in serialized
    assert "OPENAI_API_KEY" not in serialized
    assert "base_url" not in serialized


def test_cli_quickstart_agent_initialized_project_does_not_create_queue_state(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0

    result = runner.invoke(app, ["quickstart", "agent", "--project", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "Agent Quickstart" in result.output
    assert "\nProject\n" in result.output
    assert "\nSteps\n" in result.output
    assert "harness daemon run-once" in result.output
    assert "\nSafety\n" in result.output
    store = SQLiteStore(tmp_path)
    assert store.list_project_agents() == []
    assert store.list_tasks() == []
    assert store.list_runs() == []
    assert store.list_task_leases() == []
    assert store.list_daemons() == []


def test_cli_dev_create_run_runs_show(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    created = runner.invoke(
        app,
        [
            "dev",
            "create-run",
            "--goal",
            "test run",
            "--task-type",
            "phase_1a_test",
            "--project",
            str(tmp_path),
        ],
    )
    assert created.exit_code == 0
    run_id = created.output.split("Created run ", 1)[1].splitlines()[0]
    runs = runner.invoke(app, ["runs", "--project", str(tmp_path)])
    assert runs.exit_code == 0
    assert run_id in runs.output
    show = runner.invoke(app, ["show", run_id, "--project", str(tmp_path)])
    assert show.exit_code == 0
    assert "Final_report".lower().replace("_", "") not in show.output
    assert "final_report" in show.output
    run_dir = tmp_path / ".harness" / "runs" / run_id
    events_path = run_dir / "events.jsonl"
    transcript_path = run_dir / "transcript.jsonl"
    report_path = run_dir / "final_report.md"
    manifest_path = run_dir / "manifest.json"
    assert str(events_path) in show.output
    assert str(transcript_path) in show.output
    assert str(report_path) in show.output
    assert events_path.read_text(encoding="utf-8")
    assert transcript_path.exists()
    assert report_path.exists()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == "harness.manifest/v1.1"
    assert manifest["run_id"] == run_id
    assert manifest["run_mode"] == "dev"
    assert manifest["effective_policy"]["schema_version"] == "harness.effective_policy/v1"
    assert manifest["effective_policy_sha256"]
    assert all(artifact["schema_version"] == "harness.artifact/v1" for artifact in manifest["artifacts"])
    assert all(artifact["sha256"] for artifact in manifest["artifacts"])
    assert all(artifact["evidence_status"] in {"verified", "mismatch"} for artifact in manifest["artifacts"])
    assert {artifact["kind"] for artifact in manifest["artifacts"]} >= {
        "events",
        "transcript",
        "final_report",
        "manifest",
    }


def test_cli_runs_and_show_support_json_output(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    created = runner.invoke(
        app,
        [
            "dev",
            "create-run",
            "--goal",
            "json test run",
            "--task-type",
            "phase_1a_test",
            "--project",
            str(tmp_path),
        ],
    )
    assert created.exit_code == 0
    run_id = created.output.split("Created run ", 1)[1].splitlines()[0]

    runs = runner.invoke(app, ["runs", "--project", str(tmp_path), "--output", "json"])
    assert runs.exit_code == 0
    runs_payload = json.loads(runs.output)
    assert runs_payload["schema_version"] == "harness.runs/v1"
    assert runs_payload["runs"][0]["id"] == run_id
    assert runs_payload["runs"][0]["status"] == "completed"
    assert runs_payload["runs"][0]["task_type"] == "phase_1a_test"
    assert runs_payload["runs"][0]["backend_name"] is None

    show = runner.invoke(app, ["show", run_id, "--project", str(tmp_path), "--output", "json"])
    assert show.exit_code == 0
    show_payload = json.loads(show.output)
    assert show_payload["schema_version"] == "harness.manifest/v1.1"
    assert show_payload["run_id"] == run_id
    assert show_payload["run_mode"] == "dev"
    assert show_payload["effective_policy_sha256"]
    assert {artifact["kind"] for artifact in show_payload["artifacts"]} >= {
        "events",
        "transcript",
        "final_report",
        "manifest",
    }


def test_cli_artifacts_list_and_inspect_report_evidence_without_contents(tmp_path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    created = runner.invoke(
        app,
        [
            "dev",
            "create-run",
            "--goal",
            "artifact cli run",
            "--task-type",
            "phase_1a_test",
            "--project",
            str(tmp_path),
        ],
    )
    assert created.exit_code == 0
    run_id = created.output.split("Created run ", 1)[1].splitlines()[0]

    monkeypatch.setattr(
        "harness.cli.main.CodexCliBackend",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("artifacts must not preflight Codex")),
    )
    monkeypatch.setattr(
        "harness.cli.main.LocalOpenAICompatibleBackend",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("artifacts must not preflight local backend")),
    )

    listed = runner.invoke(app, ["artifacts", "list", run_id, "--project", str(tmp_path), "--output", "json"])

    assert listed.exit_code == 0, listed.output
    payload = json.loads(listed.output)
    assert payload["schema_version"] == "harness.artifacts/v1"
    assert payload["ok"] is True
    assert payload["run_id"] == run_id
    assert payload["artifacts"]
    artifact = payload["artifacts"][0]
    assert artifact["schema_version"] == "harness.artifact/v1"
    assert artifact["sha256"]
    assert artifact["size_bytes"] >= 0
    assert artifact["evidence_status"] == "verified"
    assert "Created Phase 1A diagnostic run." not in json.dumps(payload)

    inspected = runner.invoke(
        app,
        ["artifacts", "inspect", artifact["id"], "--project", str(tmp_path), "--output", "json"],
    )
    assert inspected.exit_code == 0, inspected.output
    inspect_payload = json.loads(inspected.output)
    assert inspect_payload["schema_version"] == "harness.artifact/v1"
    assert inspect_payload["ok"] is True
    assert inspect_payload["id"] == artifact["id"]
    assert inspect_payload["evidence_status"] == "verified"

    text = runner.invoke(app, ["artifacts", "list", run_id, "--project", str(tmp_path)])
    assert text.exit_code == 0
    assert artifact["id"] in text.output
    assert "verified" in text.output

    serialized = json.dumps(inspect_payload)
    assert "api_key" not in serialized
    assert "OPENAI_API_KEY" not in serialized
    assert "base_url" not in serialized


def test_cli_artifacts_unknown_refs_return_stable_json_errors(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0

    listed = runner.invoke(
        app,
        ["artifacts", "list", "run_missing", "--project", str(tmp_path), "--output", "json"],
    )
    inspected = runner.invoke(
        app,
        ["artifacts", "inspect", "art_missing", "--project", str(tmp_path), "--output", "json"],
    )

    assert listed.exit_code == 1
    list_payload = json.loads(listed.output)
    assert list_payload == {
        "schema_version": "harness.artifacts/v1",
        "ok": False,
        "errors": ["Run not found: run_missing"],
    }
    assert inspected.exit_code == 1
    inspect_payload = json.loads(inspected.output)
    assert inspect_payload == {
        "schema_version": "harness.artifact/v1",
        "ok": False,
        "errors": ["Artifact not found: art_missing"],
    }


def test_cli_compare_and_baseline_report_evidence_without_contents(tmp_path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    first = store.create_run(goal="first", task_type="phase_1a_test")
    second = store.create_run(goal="second", task_type="phase_1a_test")
    artifact_path = tmp_path / ".harness" / "runs" / second.id / "pytest_stdout.txt"
    artifact_path.write_text("initial output body", encoding="utf-8")
    store.register_artifact(second.id, "pytest_stdout", artifact_path)
    artifact_path.write_text("changed output body", encoding="utf-8")

    monkeypatch.setattr(
        "harness.cli.main.CodexCliBackend",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("compare must not preflight Codex")),
    )
    monkeypatch.setattr(
        "harness.cli.main.LocalOpenAICompatibleBackend",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("compare must not preflight local backend")),
    )
    monkeypatch.setattr(
        "harness.cli.main.DockerImageManager",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("compare must not touch Docker")),
    )

    compared = runner.invoke(
        app,
        ["compare", first.id, second.id, "--project", str(tmp_path), "--output", "json"],
    )
    baseline = runner.invoke(
        app,
        ["baseline", "set", first.id, "--name", "local-green", "--project", str(tmp_path), "--output", "json"],
    )
    baseline_compared = runner.invoke(
        app,
        [
            "baseline",
            "compare",
            second.id,
            "--baseline",
            "local-green",
            "--project",
            str(tmp_path),
            "--output",
            "json",
        ],
    )
    text = runner.invoke(app, ["compare", first.id, second.id, "--project", str(tmp_path)])

    assert compared.exit_code == 0, compared.output
    compare_payload = json.loads(compared.output)
    assert compare_payload["schema_version"] == "harness.compare/v1"
    assert compare_payload["ok"] is True
    assert compare_payload["run_a"] == first.id
    assert compare_payload["run_b"] == second.id
    assert compare_payload["matches"] is False
    assert "artifacts" in compare_payload["changed_sections"]
    assert compare_payload["sections"]["artifacts"]["run_b"][0]["evidence_status"] == "mismatch"

    assert baseline.exit_code == 0, baseline.output
    baseline_payload = json.loads(baseline.output)
    assert baseline_payload["schema_version"] == "harness.baseline/v1"
    assert baseline_payload["ok"] is True
    assert baseline_payload["name"] == "local-green"
    assert baseline_payload["run_id"] == first.id
    assert baseline_payload["evidence_sha256"]

    assert baseline_compared.exit_code == 0, baseline_compared.output
    baseline_compare_payload = json.loads(baseline_compared.output)
    assert baseline_compare_payload["schema_version"] == "harness.baseline_compare/v1"
    assert baseline_compare_payload["ok"] is True
    assert baseline_compare_payload["baseline"]["name"] == "local-green"
    assert baseline_compare_payload["comparison"]["schema_version"] == "harness.compare/v1"
    assert baseline_compare_payload["comparison"]["run_b"] == second.id

    assert text.exit_code == 0
    assert "Changed sections:" in text.output
    assert "artifacts" in text.output

    serialized = json.dumps(compare_payload) + json.dumps(baseline_payload) + json.dumps(baseline_compare_payload)
    assert "initial output body" not in serialized
    assert "changed output body" not in serialized
    assert "api_key" not in serialized
    assert "OPENAI_API_KEY" not in serialized
    assert "base_url" not in serialized
    assert "environment" not in serialized


def test_cli_compare_and_baseline_unknown_refs_return_stable_json_errors(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    run = store.create_run(goal="run", task_type="phase_1a_test")

    compared = runner.invoke(
        app,
        ["compare", run.id, "run_missing", "--project", str(tmp_path), "--output", "json"],
    )
    baseline_set = runner.invoke(
        app,
        [
            "baseline",
            "set",
            "run_missing",
            "--name",
            "missing",
            "--project",
            str(tmp_path),
            "--output",
            "json",
        ],
    )
    baseline_compared = runner.invoke(
        app,
        [
            "baseline",
            "compare",
            run.id,
            "--baseline",
            "missing",
            "--project",
            str(tmp_path),
            "--output",
            "json",
        ],
    )

    assert compared.exit_code == 1
    assert json.loads(compared.output) == {
        "schema_version": "harness.compare/v1",
        "ok": False,
        "errors": ["Run not found: run_missing"],
    }
    assert baseline_set.exit_code == 1
    assert json.loads(baseline_set.output) == {
        "schema_version": "harness.baseline/v1",
        "ok": False,
        "errors": ["Run not found: run_missing"],
    }
    assert baseline_compared.exit_code == 1
    assert json.loads(baseline_compared.output) == {
        "schema_version": "harness.baseline_compare/v1",
        "ok": False,
        "errors": ["Baseline not found: missing"],
    }


def test_cli_evals_safety_smoke_and_traces_export_are_evidence_only(tmp_path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    run = store.create_run(goal="trace cli", task_type="phase_1a_test")
    store.append_event(run.id, "info", "cli_trace_event", "Trace event.", {"payload": "safe"})

    monkeypatch.setattr(
        "harness.cli.main.CodexCliBackend",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("evals/traces must not preflight Codex")),
    )
    monkeypatch.setattr(
        "harness.cli.main.LocalOpenAICompatibleBackend",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("evals/traces must not preflight local backend")),
    )
    monkeypatch.setattr(
        "harness.cli.main.DockerImageManager",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("evals/traces must not touch Docker")),
    )

    evals = runner.invoke(
        app,
        ["evals", "run", "--suite", "safety-smoke", "--project", str(tmp_path), "--output", "json"],
    )
    security_layer = runner.invoke(
        app,
        ["evals", "run", "--suite", "security-layer", "--project", str(tmp_path), "--output", "json"],
    )
    trace = runner.invoke(
        app,
        ["traces", "export", run.id, "--format", "otel-json", "--project", str(tmp_path), "--output", "json"],
    )
    trace_text = runner.invoke(app, ["traces", "export", run.id, "--project", str(tmp_path)])

    assert evals.exit_code == 0, evals.output
    eval_payload = json.loads(evals.output)
    assert eval_payload["schema_version"] == "harness.evals.safety_smoke/v1"
    assert eval_payload["ok"] is True
    assert {check["id"] for check in eval_payload["checks"]} >= {
        "backend_boundaries",
        "artifact_evidence",
        "task_queue_non_execution",
    }
    assert security_layer.exit_code == 0, security_layer.output
    security_layer_payload = json.loads(security_layer.output)
    assert security_layer_payload["schema_version"] == "harness.security_layer_audit/v1"
    assert security_layer_payload["ok"] is True

    assert trace.exit_code == 0, trace.output
    trace_payload = json.loads(trace.output)
    assert trace_payload["schema_version"] == "harness.trace_export/v1"
    assert trace_payload["ok"] is True
    assert trace_payload["format"] == "otel-json"
    assert trace_payload["run_id"] == run.id
    spans = trace_payload["resourceSpans"][0]["scopeSpans"][0]["spans"]
    assert {span["name"] for span in spans} >= {"harness.run", "harness.policy", "harness.event.cli_trace_event"}

    assert trace_text.exit_code == 0
    assert "Trace:" in trace_text.output
    assert "Spans:" in trace_text.output

    serialized = json.dumps(eval_payload) + json.dumps(trace_payload) + json.dumps(security_layer_payload)
    assert "api_key" not in serialized
    assert "OPENAI_API_KEY" not in serialized
    assert "base_url" not in serialized
    assert "environment" not in serialized


def test_cli_evals_and_traces_errors_are_stable_json(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    run = store.create_run(goal="run", task_type="phase_1a_test")

    bad_suite = runner.invoke(
        app,
        ["evals", "run", "--suite", "unknown", "--project", str(tmp_path), "--output", "json"],
    )
    bad_format = runner.invoke(
        app,
        ["traces", "export", run.id, "--format", "zipkin", "--project", str(tmp_path), "--output", "json"],
    )
    missing_run = runner.invoke(
        app,
        ["traces", "export", "run_missing", "--project", str(tmp_path), "--output", "json"],
    )

    assert bad_suite.exit_code == 1
    assert json.loads(bad_suite.output) == {
        "schema_version": "harness.evals.safety_smoke/v1",
        "ok": False,
        "errors": ["Unsupported eval suite: unknown"],
    }
    assert bad_format.exit_code == 1
    assert json.loads(bad_format.output) == {
        "schema_version": "harness.trace_export/v1",
        "ok": False,
        "errors": ["Unsupported trace format: zipkin"],
    }
    assert missing_run.exit_code == 1
    assert json.loads(missing_run.output) == {
        "schema_version": "harness.trace_export/v1",
        "ok": False,
        "errors": ["Run not found: run_missing"],
    }


def test_cli_tools_list_and_inspect_are_metadata_only(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "harness.cli.main.CodexCliBackend",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("tools must not preflight Codex")),
    )
    monkeypatch.setattr(
        "harness.cli.main.LocalOpenAICompatibleBackend",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("tools must not preflight local backend")),
    )
    monkeypatch.setattr(
        "harness.cli.main.DockerImageManager",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("tools must not touch Docker")),
    )

    listed = runner.invoke(app, ["tools", "list", "--project", str(tmp_path), "--output", "json"])
    inspected = runner.invoke(app, ["tools", "inspect", "repo_read", "--project", str(tmp_path), "--output", "json"])
    text = runner.invoke(app, ["tools", "inspect", "docker_test", "--project", str(tmp_path)])

    assert listed.exit_code == 0, listed.output
    payload = json.loads(listed.output)
    assert payload["schema_version"] == "harness.tool_capabilities/v1"
    assert payload["ok"] is True
    ids = [descriptor["id"] for descriptor in payload["tools"]]
    assert ids == sorted(ids)
    assert {"repo_read", "docker_test", "policy_explain"} <= set(ids)
    assert {"generic_shell", "mcp", "a2a", "browser", "email", "calendar"}.isdisjoint(ids)

    assert inspected.exit_code == 0, inspected.output
    inspect_payload = json.loads(inspected.output)
    assert inspect_payload["schema_version"] == "harness.tool_capability/v1"
    assert inspect_payload["ok"] is True
    assert inspect_payload["id"] == "repo_read"
    assert inspect_payload["side_effect_level"] == "none"

    assert text.exit_code == 0
    assert "Tool: docker_test" in text.output
    assert "Sandbox required: True" in text.output
    assert "docker_execution" in text.output

    serialized = json.dumps(payload) + json.dumps(inspect_payload)
    assert "api_key" not in serialized
    assert "OPENAI_API_KEY" not in serialized
    assert "base_url" not in serialized
    assert "environment" not in serialized
    assert not (tmp_path / ".harness").exists()


def test_cli_tools_unknown_id_returns_stable_json_error(tmp_path) -> None:
    result = runner.invoke(
        app,
        ["tools", "inspect", "generic_shell", "--project", str(tmp_path), "--output", "json"],
    )

    assert result.exit_code == 1
    assert json.loads(result.output) == {
        "schema_version": "harness.tool_capability/v1",
        "ok": False,
        "errors": ["Tool capability not found: generic_shell"],
    }


def test_cli_mcp_list_resources_and_lifecycle_fail_closed(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    (tmp_path / "mcp-cache").mkdir()
    (tmp_path / "mcp-cache" / "guide.md").write_text("# Guide\n\nCached only.\n", encoding="utf-8")
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

    listed = runner.invoke(app, ["mcp", "list", "--project", str(tmp_path), "--output", "json"])
    status = runner.invoke(app, ["mcp", "status", "--project", str(tmp_path), "--output", "json"])
    resources = runner.invoke(app, ["mcp", "resources", "--project", str(tmp_path), "--output", "json"])
    resources_text = runner.invoke(app, ["mcp", "resources", "--project", str(tmp_path)])
    connect = runner.invoke(app, ["mcp", "connect", "remote_tracker", "--project", str(tmp_path), "--output", "json"])

    assert listed.exit_code == 0, listed.output
    list_payload = json.loads(listed.output)
    assert status.exit_code == 0, status.output
    assert json.loads(status.output) == list_payload
    assert list_payload["schema_version"] == "harness.mcp_status/v1"
    assert list_payload["enabled"] is True
    assert list_payload["connected"] is False
    assert list_payload["process_started"] is False
    assert list_payload["network_called"] is False
    assert list_payload["tool_registration_enabled"] is False
    assert list_payload["tool_execution_supported"] is False
    assert list_payload["policy_boundary"]["kind"] == "mcp_metadata_projection"
    assert list_payload["blocked_reasons"] == [
        "mcp_process_launch_disabled",
        "mcp_network_connection_disabled",
        "mcp_tool_execution_disabled",
    ]
    assert [server["name"] for server in list_payload["servers"]] == ["local_docs", "remote_tracker"]
    assert list_payload["servers"][1]["requires_network"] is True

    assert resources.exit_code == 0, resources.output
    resources_payload = json.loads(resources.output)
    assert resources_payload["schema_version"] == "harness.mcp_resources/v1"
    assert resources_payload["resources"] == [
        {
            "name": "guide",
            "server": "local_docs",
            "uri": "mcp://local_docs/guide",
            "enabled": True,
            "cached": True,
            "path": "mcp-cache/guide.md",
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
            },
            "blocked_reasons": ["mcp_resource_read_requires_permission", "mcp_connection_disabled"],
            "connected": False,
            "process_started": False,
            "network_called": False,
            "permission_granting": False,
        }
    ]
    assert resources_payload["resource_count"] == 1
    assert resources_payload["cached_only"] is True
    assert resources_payload["contents_included"] is False
    assert resources_payload["policy_boundary"]["kind"] == "mcp_resources_projection"
    assert resources_payload["network_called"] is False
    assert resources_payload["process_started"] is False
    assert resources_text.exit_code == 0, resources_text.output
    assert "mcp://local_docs/guide" in resources_text.output
    assert "mcp-cache/guide.md" in resources_text.output
    assert "MCP resources are cached-only" in resources_text.output

    assert connect.exit_code == 1
    connect_payload = json.loads(connect.output)
    assert connect_payload["schema_version"] == "harness.mcp_action/v1"
    assert connect_payload["ok"] is False
    assert connect_payload["action"] == "connect"
    assert connect_payload["server"] == "remote_tracker"
    assert connect_payload["process_started"] is False
    assert connect_payload["network_called"] is False
    assert connect_payload["tool_registration_enabled"] is False
    assert connect_payload["tool_execution_started"] is False
    assert connect_payload["filesystem_modified"] is False
    assert connect_payload["policy_boundary"]["kind"] == "mcp_action"
    assert connect_payload["blocked_reasons"] == [
        "mcp_action_disabled",
        "mcp_process_launch_disabled",
        "mcp_network_connection_disabled",
    ]
    assert connect_payload["permission_granting"] is False


def test_cli_plugins_and_skills_are_metadata_only_and_fail_closed(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    (tmp_path / "plugins" / "reviewer").mkdir(parents=True)
    (tmp_path / "plugins" / "reviewer" / "plugin.json").write_text('{"name":"reviewer"}\n', encoding="utf-8")
    skill_dir = tmp_path / "skills" / "review"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Review\n\nDo not load this body in diagnostics.\n", encoding="utf-8")
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
            }
        },
    }
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

    plugins = runner.invoke(app, ["plugins", "list", "--project", str(tmp_path), "--output", "json"])
    plugins_text = runner.invoke(app, ["plugins", "list", "--project", str(tmp_path)])
    skills = runner.invoke(app, ["skills", "list", "--project", str(tmp_path), "--output", "json"])
    skills_text = runner.invoke(app, ["skills", "list", "--project", str(tmp_path)])
    install = runner.invoke(app, ["plugins", "install", "reviewer", "--project", str(tmp_path), "--output", "json"])
    update = runner.invoke(app, ["plugins", "update", "reviewer", "--project", str(tmp_path), "--output", "json"])
    remove = runner.invoke(app, ["plugins", "remove", "reviewer", "--project", str(tmp_path), "--output", "json"])
    load = runner.invoke(app, ["skills", "load", "review", "--project", str(tmp_path), "--output", "json"])

    assert plugins.exit_code == 0, plugins.output
    plugins_payload = json.loads(plugins.output)
    assert plugins_payload["schema_version"] == "harness.plugins/v1"
    assert plugins_payload["runtime_loaded"] is False
    assert plugins_payload["tools_registered"] is False
    assert plugins_payload["tool_execution_supported"] is False
    assert plugins_payload["policy_boundary"]["kind"] == "plugin_catalog_metadata"
    assert plugins_payload["blocked_reasons"] == [
        "plugin_origin_review_required",
        "plugin_runtime_load_disabled",
        "plugin_tool_execution_disabled",
    ]
    assert plugins_payload["filesystem_modified"] is False
    assert plugins_payload["network_called"] is False
    project_plugins = [plugin for plugin in plugins_payload["plugins"] if plugin["scope"] == "project"]
    assert len(project_plugins) == 1
    assert project_plugins[0]["name"] == "reviewer"
    assert project_plugins[0]["origin"] == "config"
    assert project_plugins[0]["source_kind"] == "local"
    assert project_plugins[0]["spec"] == "./plugins/reviewer"
    assert project_plugins[0]["entrypoint"] == "plugin.json"
    assert project_plugins[0]["manifest_path"] == "plugins/reviewer/plugin.json"
    assert project_plugins[0]["manifest_exists"] is True
    assert project_plugins[0]["options_configured"] is True
    assert project_plugins[0]["option_keys"] == ["mode"]
    assert project_plugins[0]["origin_review_required"] is True
    assert project_plugins[0]["runtime_load_supported"] is False
    assert project_plugins[0]["tool_execution_supported"] is False
    assert project_plugins[0]["policy_boundary"]["kind"] == "plugin_metadata_projection"
    assert project_plugins[0]["blocked_reasons"] == [
        "plugin_origin_review_required",
        "plugin_runtime_load_disabled",
        "plugin_tool_execution_disabled",
    ]
    assert project_plugins[0]["runtime_loaded"] is False
    assert project_plugins[0]["tools_registered"] is False
    assert plugins_text.exit_code == 0, plugins_text.output
    assert "./plugins/reviewer" in plugins_text.output
    assert "plugins/reviewer/plugin.json" in plugins_text.output

    assert skills.exit_code == 0, skills.output
    skills_payload = json.loads(skills.output)
    assert skills_payload["schema_version"] == "harness.skills/v1"
    assert skills_payload["runtime_loaded"] is False
    assert skills_payload["skill_body_loaded"] is False
    assert skills_payload["tool_registered"] is False
    assert skills_payload["filesystem_modified"] is False
    assert skills_payload["network_called"] is False
    project_skills = [skill for skill in skills_payload["skills"] if skill["scope"] == "project"]
    assert len(project_skills) == 1
    assert project_skills[0]["name"] == "review"
    assert project_skills[0]["source_kind"] == "local"
    assert project_skills[0]["spec"] == "./skills/review"
    assert project_skills[0]["version"] == "0.1.0"
    assert project_skills[0]["skill_file_path"] == "skills/review/SKILL.md"
    assert project_skills[0]["skill_file_exists"] is True
    assert project_skills[0]["skill_body_loaded"] is False
    assert "Do not load this body" not in skills.output
    assert skills_text.exit_code == 0, skills_text.output
    assert "./skills/review" in skills_text.output
    assert "skills/review/SKILL.md" in skills_text.output
    assert "Do not load this body" not in skills_text.output

    for result, action in [(install, "install"), (update, "update"), (remove, "remove")]:
        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["schema_version"] == "harness.plugin_action/v1"
        assert payload["action"] == action
        assert payload["filesystem_modified"] is False
        assert payload["network_called"] is False
        assert payload["policy_boundary"]["kind"] == "plugin_action"
        assert payload["policy_boundary"]["tool_execution_allowed"] is False
        assert payload["blocked_reasons"] == [
            "plugin_action_disabled",
            "plugin_origin_review_required",
            "plugin_runtime_load_disabled",
        ]
        assert payload["runtime_loaded"] is False
        assert payload["tools_registered"] is False
        assert payload["tool_execution_started"] is False
        assert payload["install_supported"] is False
        assert payload["update_supported"] is False
        assert payload["remove_supported"] is False
        assert payload["permission_granting"] is False

    assert load.exit_code == 1
    load_payload = json.loads(load.output)
    assert load_payload["schema_version"] == "harness.skill_action/v1"
    assert load_payload["skill_body_loaded"] is False
    assert load_payload["runtime_loaded"] is False
    assert load_payload["tool_registered"] is False
    assert load_payload["permission_granting"] is False


def test_cli_extensions_status_summarizes_extensibility_without_side_effects(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    (tmp_path / "plugins" / "reviewer").mkdir(parents=True)
    (tmp_path / "plugins" / "reviewer" / "plugin.json").write_text('{"name":"reviewer"}\n', encoding="utf-8")
    skill_dir = tmp_path / "skills" / "review"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Review\n\nDo not load through status.\n", encoding="utf-8")
    config_path = tmp_path / ".harness" / "config.yaml"
    config_data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
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
        "search_enabled": False,
        "approval_required": True,
        "allowed_domains": ["docs.example.com"],
    }
    config_path.write_text(yaml.safe_dump(config_data, sort_keys=False), encoding="utf-8")

    status = runner.invoke(app, ["extensions", "status", "--project", str(tmp_path), "--output", "json"])
    text = runner.invoke(app, ["extensions", "status", "--project", str(tmp_path)])

    assert status.exit_code == 0, status.output
    payload = json.loads(status.output)
    assert payload["schema_version"] == "harness.extensions_status/v1"
    assert payload["plugins"]["plugin_count"] >= 1
    assert payload["plugins"]["project_plugin_count"] == 1
    assert payload["plugins"]["runtime_loaded"] is False
    assert payload["plugins"]["tools_registered"] is False
    assert payload["skills"]["skill_count"] >= 1
    assert payload["skills"]["project_skill_count"] == 1
    assert payload["skills"]["skill_body_loaded"] is False
    assert payload["web_tools"]["decisions"]["web-fetch"] == "approval_required"
    assert payload["web_tools"]["decisions"]["web-search"] == "denied"
    assert payload["policy"]["permission_granting"] is False
    assert payload["policy"]["network_called"] is False
    assert payload["policy"]["filesystem_modified"] is False
    assert "Do not load through status" not in status.output
    assert text.exit_code == 0, text.output
    assert "web-fetch:approval_required" in text.output
    assert "Extensibility diagnostics are metadata-only" in text.output
    assert "Do not load through status" not in text.output


def test_cli_web_tools_project_policy_and_fail_closed_execution(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    config_path = tmp_path / ".harness" / "config.yaml"
    config_data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config_data["web_tools"] = {
        "enabled": True,
        "fetch_enabled": True,
        "search_enabled": True,
        "approval_required": True,
        "allowed_domains": ["docs.example.com"],
    }
    config_path.write_text(yaml.safe_dump(config_data, sort_keys=False), encoding="utf-8")

    tools = runner.invoke(app, ["web", "tools", "--project", str(tmp_path), "--output", "json"])
    fetch = runner.invoke(
        app,
        ["web", "fetch", "https://docs.example.com/page", "--project", str(tmp_path), "--output", "json"],
    )
    search = runner.invoke(app, ["web", "search", "harness docs", "--project", str(tmp_path), "--output", "json"])

    assert tools.exit_code == 0, tools.output
    tools_payload = json.loads(tools.output)
    assert tools_payload["schema_version"] == "harness.web_tools/v1"
    assert tools_payload["enabled"] is True
    assert tools_payload["network_called"] is False
    assert tools_payload["execution_supported"] is False
    assert tools_payload["permission_granting"] is False
    by_id = {tool["id"]: tool for tool in tools_payload["tools"]}
    assert by_id["web-fetch"]["decision"] == "approval_required"
    assert by_id["web-search"]["decision"] == "approval_required"
    assert by_id["web-fetch"]["allowed_domains"] == ["docs.example.com"]

    assert fetch.exit_code == 1
    fetch_payload = json.loads(fetch.output)
    assert fetch_payload["schema_version"] == "harness.web_tool_action/v1"
    assert fetch_payload["tool"] == "web-fetch"
    assert fetch_payload["decision"] == "approval_required"
    assert fetch_payload["approval_required"] is True
    assert fetch_payload["network_called"] is False
    assert fetch_payload["execution_started"] is False
    assert fetch_payload["permission_granting"] is False

    assert search.exit_code == 1
    search_payload = json.loads(search.output)
    assert search_payload["schema_version"] == "harness.web_tool_action/v1"
    assert search_payload["tool"] == "web-search"
    assert search_payload["decision"] == "approval_required"
    assert search_payload["network_called"] is False
    assert search_payload["execution_started"] is False
    assert search_payload["permission_granting"] is False


def test_cli_worktrees_list_and_lifecycle_fail_closed(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "app.py").write_text("print('one')\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=tmp_path, check=True, capture_output=True)

    listed = runner.invoke(app, ["worktrees", "list", "--project", str(tmp_path), "--output", "json"])
    created = runner.invoke(app, ["worktrees", "create", "candidate", "--branch", "HEAD", "--project", str(tmp_path), "--output", "json"])
    removed = runner.invoke(app, ["worktrees", "remove", "candidate", "--project", str(tmp_path), "--output", "json"])
    reset = runner.invoke(app, ["worktrees", "reset", "candidate", "--branch", "main", "--project", str(tmp_path), "--output", "json"])

    assert listed.exit_code == 0, listed.output
    list_payload = json.loads(listed.output)
    assert list_payload["schema_version"] == "harness.worktrees/v1"
    assert list_payload["available"] is True
    assert list_payload["mutation_supported"] is False
    assert list_payload["permission_granting"] is False
    assert len(list_payload["worktrees"]) == 1
    assert list_payload["worktrees"][0]["path"] == str(tmp_path)
    assert list_payload["worktrees"][0]["is_current"] is True

    for result, action in [(created, "create"), (removed, "remove"), (reset, "reset")]:
        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["schema_version"] == "harness.worktree_action/v1"
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
    create_payload = json.loads(created.output)
    assert create_payload["plan"]["steps"][0]["command"] == [
        "git",
        "worktree",
        "add",
        "--detach",
        ".harness/worktrees/candidate",
        "HEAD",
    ]
    assert [step["name"] for step in json.loads(removed.output)["plan"]["steps"]] == ["remove_worktree"]
    assert [step["name"] for step in json.loads(reset.output)["plan"]["steps"]] == ["fetch_default_branch", "reset_worktree"]
    assert not (tmp_path / "candidate").exists()
    assert not (tmp_path / ".harness" / "worktrees" / "candidate").exists()

    outside = runner.invoke(
        app,
        [
            "worktrees",
            "create",
            f"../outside-{tmp_path.name}",
            "--project",
            str(tmp_path),
            "--output",
            "json",
        ],
    )
    assert outside.exit_code == 1
    outside_payload = json.loads(outside.output)
    assert outside_payload["plan"]["valid_target"] is False
    assert outside_payload["plan"]["managed_path"] is None
    assert outside_payload["plan"]["steps"] == []
    assert outside_payload["plan"]["blocked_reasons"] == [
        "target_must_be_managed_worktree_name",
        "worktree_mutation_disabled",
    ]
    assert outside_payload["git_mutation_started"] is False
    assert outside_payload["filesystem_modified"] is False
    assert outside_payload["process_started"] is False
    assert not (tmp_path.parent / f"outside-{tmp_path.name}").exists()

    text = runner.invoke(app, ["worktrees", "create", "candidate", "--project", str(tmp_path)])
    assert text.exit_code == 1
    assert ".harness/worktrees/candidate" in text.output
    assert "executed=false" in text.output


def test_cli_session_diff_lists_artifact_preview_without_mutation(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    session = store.create_session(title="Diff session")
    run = store.create_run("Diff run", "codex_isolated_edit", session_id=session.id)
    diff_path = store.runs_dir / run.id / "isolated_unified_diff.patch"
    diff_path.parent.mkdir(parents=True, exist_ok=True)
    diff_path.write_text("--- a/app.py\n+++ b/app.py\n@@\n-old\n+new\n", encoding="utf-8")
    artifact = store.register_artifact(run.id, "isolated_unified_diff", diff_path, session_id=session.id)

    result = runner.invoke(app, ["session", "diff", session.id, "--project", str(tmp_path), "--output", "json"])
    text = runner.invoke(app, ["session", "diff", session.id, "--project", str(tmp_path)])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema_version"] == "harness.session_diffs/v1"
    assert payload["session_id"] == session.id
    assert payload["revert_supported"] is False
    assert payload["unrevert_supported"] is False
    assert payload["selected_hunk_apply_supported"] is False
    assert payload["mutation_started"] is False
    assert payload["permission_granting"] is False
    assert payload["diffs"][0]["id"] == artifact.id
    assert "+new" in payload["diffs"][0]["preview"]

    assert text.exit_code == 0
    assert "Diff artifact:" in text.output
    assert "+new" in text.output
    assert "Revert, unrevert, and selected hunk apply are not enabled" in text.output


def test_cli_session_snapshots_list_message_run_diff_links_without_revert(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    session = store.create_session(title="Snapshot session")
    run = store.create_run("Diff run", "codex_isolated_edit", session_id=session.id)
    message = store.append_session_message(session.id, "assistant", "Changed app.py", run_id=run.id)
    diff_path = store.runs_dir / run.id / "isolated_unified_diff.patch"
    diff_path.parent.mkdir(parents=True, exist_ok=True)
    diff_path.write_text("diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@\n-old\n+new\n", encoding="utf-8")
    artifact = store.register_artifact(run.id, "isolated_unified_diff", diff_path, session_id=session.id)

    result = runner.invoke(app, ["session", "snapshots", session.id, "--project", str(tmp_path), "--output", "json"])
    filtered = runner.invoke(
        app,
        ["session", "snapshots", session.id, "--message", message.id, "--project", str(tmp_path), "--output", "json"],
    )
    text = runner.invoke(app, ["session", "snapshots", session.id, "--project", str(tmp_path)])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema_version"] == "harness.session_snapshots/v1"
    assert payload["snapshot_count"] == 1
    assert payload["derived_snapshot_count"] == 1
    snapshot = payload["snapshots"][0]
    assert snapshot["source"] == "derived_from_message_run_artifacts"
    assert snapshot["message_id"] == message.id
    assert snapshot["run_ids"] == [run.id]
    assert artifact.id in snapshot["artifact_ids"]
    assert snapshot["changed_paths"] == ["app.py"]
    assert snapshot["mutation_reversibility"] == "not_reversible_metadata_only"
    assert snapshot["evidence_contract"]["contents_included"] is False
    assert snapshot["evidence_contract"]["requires_sha256"] is True
    assert payload["policy_boundary"]["kind"] == "snapshot_metadata_projection"
    assert payload["policy_boundary"]["active_workspace_mutation_allowed"] is False
    assert snapshot["revert_supported"] is False
    assert snapshot["unrevert_supported"] is False
    assert snapshot["selected_hunk_apply_supported"] is False
    assert snapshot["mutation_started"] is False
    assert snapshot["filesystem_modified"] is False
    assert json.loads(filtered.output)["snapshots"] == payload["snapshots"]
    assert text.exit_code == 0, text.output
    assert "app.py" in text.output
    assert "Snapshot metadata is read-only" in text.output


def test_cli_session_revert_unrevert_and_apply_hunk_fail_closed(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    session = store.create_session(title="Mutation session")
    run = store.create_run("Diff run", "codex_isolated_edit", session_id=session.id)
    message = store.append_session_message(session.id, "assistant", "Changed app.py", run_id=run.id)
    diff_path = store.runs_dir / run.id / "isolated_unified_diff.patch"
    diff_path.parent.mkdir(parents=True, exist_ok=True)
    diff_path.write_text("--- a/app.py\n+++ b/app.py\n@@\n-old\n+new\n", encoding="utf-8")
    artifact = store.register_artifact(run.id, "isolated_unified_diff", diff_path, session_id=session.id)

    readiness = runner.invoke(
        app,
        ["session", "revert-readiness", session.id, "--message", message.id, "--project", str(tmp_path), "--output", "json"],
    )
    readiness_text = runner.invoke(app, ["session", "revert-readiness", session.id, "--project", str(tmp_path)])

    revert = runner.invoke(
        app,
        ["session", "revert", session.id, "--message", "msg_123", "--project", str(tmp_path), "--output", "json"],
    )
    unrevert = runner.invoke(
        app,
        ["session", "unrevert", session.id, "--artifact", "art_123", "--project", str(tmp_path), "--output", "json"],
    )
    apply_hunk = runner.invoke(
        app,
        [
            "session",
            "apply-hunk",
            session.id,
            "--artifact",
            "art_123",
            "--hunk",
            "hunk_1",
            "--project",
            str(tmp_path),
            "--output",
            "json",
        ],
    )

    for result, action in [(revert, "revert"), (unrevert, "unrevert"), (apply_hunk, "apply-hunk")]:
        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["schema_version"] == "harness.session_mutation_action/v1"
        assert payload["ok"] is False
        assert payload["action"] == action
        assert payload["mutation_started"] is False
        assert payload["git_mutation_started"] is False
        assert payload["filesystem_modified"] is False
        assert payload["permission_granting"] is False
    assert json.loads(apply_hunk.output)["hunk_id"] == "hunk_1"
    assert readiness.exit_code == 0, readiness.output
    readiness_payload = json.loads(readiness.output)
    assert readiness_payload["schema_version"] == "harness.session_revert_readiness/v1"
    assert readiness_payload["ready"] is False
    assert readiness_payload["mutation_reversibility"] == "not_reversible_readiness_only"
    assert readiness_payload["policy_boundary"]["kind"] == "session_revert_readiness"
    assert readiness_payload["policy_boundary"]["requires_verification_artifact"] is True
    assert readiness_payload["message_id"] == message.id
    assert readiness_payload["diff_artifact_ids"] == [artifact.id]
    assert readiness_payload["changed_paths"] == ["app.py"]
    assert readiness_payload["revert_supported"] is False
    assert readiness_payload["filesystem_modified"] is False
    assert readiness_payload["permission_granting"] is False
    assert "snapshot_restore_not_implemented" in {blocker["code"] for blocker in readiness_payload["blockers"]}
    assert "snapshot_restore_not_implemented" in readiness_payload["blocked_reasons"]
    assert readiness_text.exit_code == 0, readiness_text.output
    assert "Blockers:" in readiness_text.output
    assert "active_revert_policy_missing" in readiness_text.output


def test_cli_pty_projection_and_lifecycle_fail_closed(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0

    listed = runner.invoke(app, ["pty", "list", "--project", str(tmp_path), "--output", "json"])
    shells = runner.invoke(app, ["pty", "shells", "--project", str(tmp_path), "--output", "json"])
    restoration = runner.invoke(app, ["pty", "restoration", "--project", str(tmp_path), "--output", "json"])
    tabs = runner.invoke(app, ["pty", "tabs", "--project", str(tmp_path), "--output", "json"])
    created = runner.invoke(
        app,
        ["pty", "create", "--command", "bash", "--project", str(tmp_path), "--output", "json"],
    )
    written = runner.invoke(
        app,
        ["pty", "write", "pty_123", "--data", "echo hello", "--project", str(tmp_path), "--output", "json"],
    )
    resized = runner.invoke(
        app,
        ["pty", "resize", "pty_123", "--cols", "100", "--rows", "30", "--project", str(tmp_path), "--output", "json"],
    )
    closed = runner.invoke(app, ["pty", "close", "pty_123", "--project", str(tmp_path), "--output", "json"])

    assert listed.exit_code == 0, listed.output
    listed_payload = json.loads(listed.output)
    assert listed_payload["schema_version"] == "harness.pty_sessions/v1"
    assert listed_payload["sessions"] == []
    assert listed_payload["approval_required"] is True
    assert listed_payload["required_approval"] == "managed_pty_control"
    assert listed_payload["policy_boundary"]["kind"] == "shell_pty_deferred"
    assert listed_payload["policy_boundary"]["shell_execution_allowed"] is False
    assert listed_payload["policy_boundary"]["managed_pty_allowed"] is False
    assert listed_payload["policy_boundary"]["model_auto_run_allowed"] is False
    assert listed_payload["blocked_reasons"] == ["shell_execution_disabled", "managed_pty_disabled", "model_auto_run_disabled"]
    assert listed_payload["process_started"] is False
    assert listed_payload["websocket_opened"] is False
    assert listed_payload["filesystem_modified"] is False
    assert listed_payload["permission_granting"] is False

    assert shells.exit_code == 0, shells.output
    shells_payload = json.loads(shells.output)
    assert shells_payload["schema_version"] == "harness.pty_shells/v1"
    assert shells_payload["probed"] is False
    assert shells_payload["approval_required"] is True
    assert shells_payload["required_approval"] == "managed_pty_control"
    assert shells_payload["policy_boundary"]["kind"] == "shell_pty_deferred"
    assert shells_payload["policy_boundary"]["shell_execution_allowed"] is False
    assert shells_payload["policy_boundary"]["shell_probe_allowed"] is False
    assert shells_payload["blocked_reasons"] == ["shell_execution_disabled", "shell_probe_disabled", "managed_pty_disabled", "model_auto_run_disabled"]
    assert shells_payload["process_started"] is False
    assert shells_payload["filesystem_modified"] is False
    assert all(shell["acceptable"] is False for shell in shells_payload["shells"])
    assert all(shell["blocked_reasons"] == ["shell_execution_disabled", "managed_pty_disabled"] for shell in shells_payload["shells"])
    assert restoration.exit_code == 0, restoration.output
    restoration_payload = json.loads(restoration.output)
    assert restoration_payload["schema_version"] == "harness.pty_restoration_readiness/v1"
    assert restoration_payload["ready"] is False
    assert restoration_payload["event_count"] == 0
    assert restoration_payload["process_started"] is False
    assert restoration_payload["live_stream_read"] is False
    assert restoration_payload["permission_granting"] is False
    assert tabs.exit_code == 0, tabs.output
    tabs_payload = json.loads(tabs.output)
    assert tabs_payload["schema_version"] == "harness.pty_terminal_tabs/v1"
    assert tabs_payload["tabs"] == []
    assert tabs_payload["policy_boundary"]["kind"] == "pty_terminal_tabs_projection"
    assert tabs_payload["policy_boundary"]["requires_append_only_events"] is True
    assert tabs_payload["blocked_reasons"] == ["managed_pty_not_enabled", "terminal_tab_projection_disabled"]
    assert tabs_payload["process_started"] is False
    assert tabs_payload["websocket_opened"] is False
    assert tabs_payload["live_stream_read"] is False
    assert tabs_payload["permission_granting"] is False

    for result, action in [(created, "create"), (written, "write"), (resized, "resize"), (closed, "close")]:
        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["schema_version"] == "harness.pty_action/v1"
        assert payload["ok"] is False
        assert payload["action"] == action
        assert payload["plan"]["schema_version"] == "harness.pty_plan/v1"
        assert payload["plan"]["executed"] is False
        assert payload["plan"]["execution_supported"] is False
        assert payload["plan"]["approval_required"] is True
        assert payload["plan"]["required_approval"] == "managed_pty_control"
        assert payload["plan"]["policy_boundary"]["kind"] == "managed_pty"
        assert payload["plan"]["policy_boundary"]["process_start_allowed"] is False
        assert payload["plan"]["policy_boundary"]["live_stream_allowed"] is False
        assert payload["execution_supported"] is False
        assert payload["approval_required"] is True
        assert payload["required_approval"] == "managed_pty_control"
        assert payload["process_started"] is False
        assert payload["input_written"] is False
        assert payload["terminal_resized"] is False
        assert payload["terminal_closed"] is False
        assert payload["websocket_token_issued"] is False
        assert payload["websocket_opened"] is False
        assert payload["live_stream_read"] is False
        assert payload["filesystem_modified"] is False
        assert payload["permission_granting"] is False
    created_payload = json.loads(created.output)
    written_payload = json.loads(written.output)
    resized_payload = json.loads(resized.output)
    closed_payload = json.loads(closed.output)
    assert created_payload["plan"]["steps"][0]["name"] == "create_pty_process"
    assert created_payload["plan"]["command"] == "bash"
    assert created_payload["blocked_reasons"] == ["managed_pty_disabled", "pty_process_start_disabled"]
    assert written_payload["pty_id"] == "pty_123"
    assert written_payload["plan"]["steps"][0]["name"] == "write_terminal_input"
    assert "terminal_input_write_disabled" in written_payload["blocked_reasons"]
    assert resized_payload["plan"]["cols"] == 100
    assert resized_payload["plan"]["rows"] == 30
    assert "terminal_resize_disabled" in resized_payload["blocked_reasons"]
    assert closed_payload["plan"]["steps"][0]["name"] == "terminate_pty_process"
    assert "terminal_close_disabled" in closed_payload["blocked_reasons"]

    text = runner.invoke(app, ["pty", "create", "--command", "bash", "--project", str(tmp_path)])
    assert text.exit_code == 1
    assert "create_pty_process" in text.output
    assert "executed=false" in text.output
    assert "policy_boundary" in text.output
    assert "managed_pty_disabled" in text.output
    assert "live_stream_read=false" in text.output
    list_text = runner.invoke(app, ["pty", "list", "--project", str(tmp_path)])
    shells_text = runner.invoke(app, ["pty", "shells", "--project", str(tmp_path)])
    assert list_text.exit_code == 0, list_text.output
    assert "shell_pty_deferred" in list_text.output
    assert "model_auto_run_disabled" in list_text.output
    assert shells_text.exit_code == 0, shells_text.output
    assert "shell_pty_deferred" in shells_text.output
    assert "shell_probe_disabled" in shells_text.output

    store = SQLiteStore(tmp_path)
    store.append_store_event(EventStreamType.SESSION, "pty:pty_123", "pty.created", {"shell": "/bin/zsh"})
    store.append_store_event(
        EventStreamType.SESSION,
        "pty:pty_123",
        "pty.output",
        {"preview": "hello", "preview_bytes": 5},
        artifact_refs=["art_pty_output"],
    )
    restore_text = runner.invoke(app, ["pty", "restoration", "--pty", "pty_123", "--project", str(tmp_path)])
    tab_payload = runner.invoke(app, ["pty", "tabs", "--pty", "pty_123", "--project", str(tmp_path), "--output", "json"])
    tab_text = runner.invoke(app, ["pty", "tabs", "--pty", "pty_123", "--project", str(tmp_path)])
    assert restore_text.exit_code == 0, restore_text.output
    assert "pty_restoration_readiness" in restore_text.output
    assert "managed_pty_not_enabled" in restore_text.output
    assert "missing_events" in restore_text.output
    assert "PTY restoration readiness is diagnostic only" in restore_text.output
    assert tab_payload.exit_code == 0, tab_payload.output
    tab = json.loads(tab_payload.output)["tabs"][0]
    assert tab["id"] == "pty_123"
    assert tab["title"] == "/bin/zsh"
    assert tab["scrollback_preview"] == "hello"
    assert tab["restoration_ready"] is False
    assert tab["policy_boundary"]["kind"] == "pty_terminal_tab_projection"
    assert tab["policy_boundary"]["source"] == "persisted_pty_events"
    assert tab["policy_boundary"]["terminal_control_allowed"] is False
    assert tab["policy_boundary"]["requires_append_only_events"] is True
    assert tab["policy_boundary"]["bounded_preview_only"] is True
    assert "managed_pty_not_enabled" in tab["blocked_reasons"]
    assert "terminal_tab_projection_disabled" in tab["blocked_reasons"]
    assert "terminal_control_disabled" in tab["blocked_reasons"]
    assert tab["process_started"] is False
    assert tab["websocket_opened"] is False
    assert tab_text.exit_code == 0, tab_text.output
    assert "pty_123" in tab_text.output
    assert "pty_terminal_tabs_projection" in tab_text.output
    assert "terminal_tab_projection_disabled" in tab_text.output
    assert "Terminal tab projection is diagnostic only" in tab_text.output


def test_cli_dev_loop_status_summarizes_safe_phase_9_surface(tmp_path) -> None:
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

    status = runner.invoke(
        app,
        ["dev-loop", "status", "--session", session.id, "--project", str(tmp_path), "--output", "json"],
    )
    text = runner.invoke(app, ["dev-loop", "status", "--session", session.id, "--project", str(tmp_path)])

    assert status.exit_code == 0, status.output
    payload = json.loads(status.output)
    assert payload["schema_version"] == "harness.dev_loop_status/v1"
    assert payload["policy_boundary"]["kind"] == "dev_loop_status_projection"
    assert payload["policy_boundary"]["terminal_process_allowed"] is False
    assert payload["policy_boundary"]["worktree_creation_allowed"] is False
    assert payload["policy_boundary"]["active_workspace_revert_allowed"] is False
    assert payload["policy_boundary"]["selected_hunk_apply_allowed"] is False
    assert payload["policy_boundary"]["git_mutation_allowed"] is False
    assert "worktree_mutation_disabled" in payload["blocked_reasons"]
    assert "active_workspace_revert_disabled" in payload["blocked_reasons"]
    assert payload["pty"]["managed_pty_supported"] is False
    assert payload["pty"]["process_started"] is False
    assert payload["terminal_tabs"]["tab_count"] == 1
    assert payload["terminal_tabs"]["output_event_count"] == 1
    assert payload["terminal_tabs"]["artifact_ref_count"] == 1
    assert payload["terminal_tabs"]["terminal_tabs_supported"] is False
    assert payload["terminal_tabs"]["policy_boundary"]["kind"] == "pty_terminal_tabs_projection"
    assert payload["terminal_tabs"]["policy_boundary"]["source"] == "persisted_pty_events"
    assert payload["terminal_tabs"]["policy_boundary"]["terminal_control_allowed"] is False
    assert payload["terminal_tabs"]["policy_boundary"]["requires_append_only_events"] is True
    assert "terminal_tab_projection_disabled" in payload["terminal_tabs"]["blocked_reasons"]
    assert payload["terminal_tabs"]["source"] == "persisted_pty_events"
    assert payload["terminal_tabs"]["terminal_control_supported"] is False
    assert payload["terminal_tabs"]["websocket_supported"] is False
    assert payload["terminal_tabs"]["process_started"] is False
    assert payload["terminal_tabs"]["websocket_opened"] is False
    assert payload["terminal_tabs"]["live_stream_read"] is False
    assert payload["terminal_tabs"]["artifact_contents_included"] is False
    assert payload["terminal_tabs"]["permission_granting"] is False
    assert payload["worktrees"]["available"] is True
    assert payload["worktrees"]["mutation_supported"] is False
    assert payload["worktrees"]["creation_supported"] is False
    assert payload["worktrees"]["reset_supported"] is False
    assert payload["worktrees"]["remove_supported"] is False
    assert payload["worktrees"]["blocked_reasons"] == ["worktree_mutation_disabled", "worktree_creation_disabled"]
    assert payload["worktrees"]["policy_boundary"]["kind"] == "worktree_status_projection"
    assert payload["worktrees"]["policy_boundary"]["git_mutation_allowed"] is False
    assert payload["worktrees"]["filesystem_modified"] is False
    assert payload["worktrees"]["git_mutation_started"] is False
    assert payload["session"]["diff_artifact_count"] == 1
    assert payload["session"]["changed_file_count"] >= 1
    assert payload["session"]["local_snapshot_available"] is True
    assert payload["session"]["revert_supported"] is False
    assert payload["session"]["revert_readiness_ready"] is False
    assert "active_revert_policy_missing" in payload["session"]["revert_blocked_reasons"]
    assert payload["session"]["revert_policy_boundary"]["kind"] == "session_revert_readiness"
    assert payload["session"]["snapshot_policy_boundary"]["kind"] == "snapshot_metadata_projection"
    assert payload["session"]["filesystem_modified"] is False
    assert payload["policy"]["terminal_process_started"] is False
    assert payload["policy"]["terminal_websocket_opened"] is False
    assert payload["policy"]["terminal_live_stream_read"] is False
    assert payload["policy"]["terminal_artifact_contents_included"] is False
    assert payload["policy"]["terminal_control_started"] is False
    assert payload["policy"]["workspace_mutation_started"] is False
    assert "worktree_mutation_disabled" in payload["policy"]["blocked_reasons"]
    assert payload["policy"]["filesystem_modified"] is False
    assert payload["permission_granting"] is False
    assert text.exit_code == 0, text.output
    assert "terminal_tabs" in text.output
    assert "dev_loop_status_projection" in text.output
    assert "worktree_mutation_disabled" in text.output
    assert "tabs=1,output=1" in text.output
    assert "terminal_policy" in text.output
    assert "pty_terminal_tabs_projection" in text.output
    assert "terminal_blockers" in text.output
    assert "terminal_tab_projection_disabled" in text.output
    assert "diffs=1,files=" in text.output
    assert "Dev-loop diagnostics are metadata-only" in text.output


def test_cli_pr_checkout_and_run_fail_closed_without_network_or_git_mutation(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0

    checkout = runner.invoke(
        app,
        ["pr", "checkout", "https://github.com/example/repo/pull/42", "--project", str(tmp_path), "--output", "json"],
    )
    run = runner.invoke(
        app,
        ["pr", "run", "42", "--adapter", "repo_planning", "--project", str(tmp_path), "--output", "json"],
    )

    for result, action in [(checkout, "checkout"), (run, "run")]:
        assert result.exit_code == 1
        payload = json.loads(result.output)
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
        assert payload["execution_supported"] is False
        assert payload["approval_required"] is True
        assert payload["required_approval"] == "pr_checkout_or_run"
        assert payload["network_called"] is False
        assert payload["git_mutation_started"] is False
        assert payload["worktree_created"] is False
        assert payload["checkout_started"] is False
        assert payload["adapter_started"] is False
        assert payload["process_started"] is False
        assert payload["filesystem_modified"] is False
        assert payload["permission_granting"] is False
    checkout_payload = json.loads(checkout.output)
    run_payload = json.loads(run.output)
    assert checkout_payload["pr"] == "https://github.com/example/repo/pull/42"
    assert checkout_payload["parsed"]["owner"] == "example"
    assert checkout_payload["parsed"]["repo"] == "repo"
    assert checkout_payload["parsed"]["number"] == 42
    assert checkout_payload["plan"]["branch"] == "harness/pr-42"
    assert checkout_payload["plan"]["worktree_path"] == ".harness/pr-worktrees/pr-42"
    assert checkout_payload["plan"]["fetch_ref"] == "+refs/pull/42/head:refs/remotes/origin/pr/42"
    assert [step["name"] for step in checkout_payload["plan"]["steps"]] == ["fetch_pr_head", "create_isolated_worktree"]
    assert checkout_payload["plan"]["blocked_reasons"] == [
        "network_fetch_disabled",
        "git_mutation_disabled",
        "worktree_creation_disabled",
    ]
    assert run_payload["adapter"] == "repo_planning"
    assert run_payload["plan"]["requires_repo_resolution"] is True
    assert run_payload["plan"]["blocked_reasons"] == [
        "network_fetch_disabled",
        "git_mutation_disabled",
        "worktree_creation_disabled",
        "adapter_execution_disabled",
        "repo_resolution_required",
    ]
    assert [step["name"] for step in run_payload["plan"]["steps"]] == ["fetch_pr_head", "create_isolated_worktree", "run_adapter"]

    text = runner.invoke(app, ["pr", "checkout", "example/repo#7", "--project", str(tmp_path)])
    assert text.exit_code == 1
    assert "harness/pr-7" in text.output
    assert "executed=false" in text.output
    assert "policy_boundary" in text.output
    assert "network_called=false" in text.output

    invalid = runner.invoke(app, ["pr", "checkout", "not a pr", "--project", str(tmp_path), "--output", "json"])
    assert invalid.exit_code == 1
    invalid_payload = json.loads(invalid.output)
    assert invalid_payload["parsed"]["valid"] is False
    assert invalid_payload["plan"]["steps"] == []
    assert invalid_payload["plan"]["blocked_reasons"][:4] == [
        "invalid_pr_ref",
        "network_fetch_disabled",
        "git_mutation_disabled",
        "worktree_creation_disabled",
    ]
    assert invalid_payload["network_called"] is False
    assert invalid_payload["git_mutation_started"] is False
    assert invalid_payload["worktree_created"] is False
    assert invalid_payload["process_started"] is False
    assert invalid_payload["filesystem_modified"] is False


def test_cli_distribution_status_version_check_and_actions_are_safe(tmp_path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")
    status = runner.invoke(app, ["distribution", "status", "--project", str(tmp_path), "--output", "json"])
    packaging_smoke = runner.invoke(app, ["distribution", "packaging-smoke", "--project", str(tmp_path), "--output", "json"])
    packaging_run = runner.invoke(app, ["distribution", "packaging-smoke-run", "--project", str(tmp_path), "--output", "json"])
    version_check = runner.invoke(app, ["distribution", "version-check", "--output", "json"])
    desktop_status = runner.invoke(app, ["distribution", "desktop-status", "--output", "json"])
    desktop_launch = runner.invoke(app, ["distribution", "desktop-launch", "--output", "json"])
    install = runner.invoke(app, ["distribution", "install", "--target", "user", "--output", "json"])
    upgrade = runner.invoke(app, ["distribution", "upgrade", "--version", "0.2.0", "--output", "json"])
    uninstall = runner.invoke(app, ["distribution", "uninstall", "--confirm", "harness", "--output", "json"])

    assert status.exit_code == 0
    status_payload = json.loads(status.output)
    assert status_payload["schema_version"] == "harness.distribution_status/v1"
    assert status_payload["packaging_path"] == "python_wheel_first"
    assert status_payload["network_called"] is False
    assert status_payload["filesystem_modified"] is False
    assert status_payload["subprocess_started"] is False
    assert status_payload["permission_granting"] is False
    assert packaging_smoke.exit_code == 0, packaging_smoke.output
    packaging_payload = json.loads(packaging_smoke.output)
    assert packaging_payload["schema_version"] == "harness.packaging_smoke/v1"
    assert packaging_payload["pyproject_exists"] is True
    assert packaging_payload["wheel_smoke_supported"] is True
    assert packaging_payload["execution_supported"] is False
    assert "cli_entrypoint" in packaging_payload["covers"]
    assert packaging_payload["subprocess_started"] is False
    assert packaging_payload["filesystem_modified"] is False
    assert packaging_payload["network_called"] is False
    assert packaging_payload["permission_granting"] is False
    assert packaging_run.exit_code == 1
    packaging_run_payload = json.loads(packaging_run.output)
    assert packaging_run_payload["schema_version"] == "harness.packaging_smoke_action/v1"
    assert packaging_run_payload["ok"] is False
    assert packaging_run_payload["build_started"] is False
    assert packaging_run_payload["install_started"] is False
    assert packaging_run_payload["subprocess_started"] is False
    assert packaging_run_payload["filesystem_modified"] is False
    assert packaging_run_payload["network_called"] is False
    assert packaging_run_payload["permission_granting"] is False
    assert version_check.exit_code == 0
    version_payload = json.loads(version_check.output)
    assert version_payload["schema_version"] == "harness.version_check/v1"
    assert version_payload["latest_version"] is None
    assert version_payload["update_available"] is None
    assert version_payload["network_called"] is False
    assert version_payload["subprocess_started"] is False
    assert version_payload["permission_granting"] is False
    assert desktop_status.exit_code == 0, desktop_status.output
    desktop_payload = json.loads(desktop_status.output)
    assert desktop_payload["schema_version"] == "harness.desktop_status/v1"
    assert desktop_payload["packaging_decision"] == "python_wheel_first"
    assert desktop_payload["desktop_wrapper_supported"] is False
    assert desktop_payload["launch_supported"] is False
    assert desktop_payload["process_started"] is False
    assert desktop_payload["permission_granting"] is False
    assert desktop_launch.exit_code == 1
    launch_payload = json.loads(desktop_launch.output)
    assert launch_payload["schema_version"] == "harness.desktop_action/v1"
    assert launch_payload["ok"] is False
    assert launch_payload["desktop_app_launched"] is False
    assert launch_payload["process_started"] is False
    assert launch_payload["network_called"] is False
    assert launch_payload["filesystem_modified"] is False
    assert launch_payload["permission_granting"] is False
    for result, action in [(install, "install"), (upgrade, "upgrade"), (uninstall, "uninstall")]:
        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["schema_version"] == "harness.distribution_action/v1"
        assert payload["ok"] is False
        assert payload["action"] == action
        assert payload["network_called"] is False
        assert payload["filesystem_modified"] is False
        assert payload["subprocess_started"] is False
        assert payload["package_manager_started"] is False
        assert payload["permission_granting"] is False


def test_cli_settings_tui_and_session_preferences_are_audited(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    session = store.create_session(title="Preferences session")

    catalog = runner.invoke(app, ["settings", "tui", "--output", "json"])
    before = runner.invoke(app, ["session", "preferences", session.id, "--project", str(tmp_path), "--output", "json"])
    updated = runner.invoke(
        app,
        [
            "session",
            "preferences",
            session.id,
            "--set",
            "theme=dark",
            "--set",
            "terminal_font_size=18",
            "--project",
            str(tmp_path),
            "--output",
            "json",
        ],
    )
    after_events = SQLiteStore(tmp_path).list_store_events("session", session.id)

    assert catalog.exit_code == 0, catalog.output
    catalog_payload = json.loads(catalog.output)
    assert catalog_payload["schema_version"] == "harness.tui_settings/v1"
    assert catalog_payload["source"] == "defaults"
    assert catalog_payload["preference_source"] == "defaults"
    assert catalog_payload["preferences_persisted"] is False
    assert {setting["key"] for setting in catalog_payload["settings"]} >= {
        "theme",
        "terminal_font_size",
        "keybinding_preset",
        "composer_mode",
    }
    assert catalog_payload["filesystem_modified"] is False
    assert catalog_payload["process_started"] is False
    assert catalog_payload["permission_granting"] is False

    assert before.exit_code == 0, before.output
    before_payload = json.loads(before.output)
    assert before_payload["schema_version"] == "harness.session_preferences/v1"
    assert before_payload["settings"]["source"] == "active_session"
    assert before_payload["settings"]["session_id"] == session.id
    assert before_payload["settings"]["preference_source"] == "session_ui_preferences"
    assert before_payload["settings"]["preferences_persisted"] is False
    assert before_payload["settings"]["persist_command"] == f"harness session preferences {session.id} --project . --set key=value"
    assert before_payload["preferences"]["theme"] == "light"
    assert before_payload["updated"] is False

    assert updated.exit_code == 0, updated.output
    updated_payload = json.loads(updated.output)
    assert updated_payload["updated"] is True
    assert updated_payload["preferences"]["theme"] == "dark"
    assert updated_payload["preferences"]["terminal_font_size"] == 18
    assert updated_payload["settings"]["source"] == "active_session"
    assert updated_payload["settings"]["session_id"] == session.id
    assert updated_payload["settings"]["preferences"]["theme"] == "dark"
    assert updated_payload["settings"]["preferences_persisted"] is False
    assert updated_payload["settings"]["policy_boundary"]["preference_persistence_allowed"] is False
    assert updated_payload["permission_granting"] is False
    assert any(event.kind == "session.ui_preferences.updated" for event in after_events)
    assert SQLiteStore(tmp_path).get_session(session.id).ui_preferences["theme"] == "dark"


def test_cli_project_commands_are_listed_in_palette_and_slash_but_not_executed(tmp_path) -> None:
    command_dir = tmp_path / ".harness" / "commands"
    command_dir.mkdir(parents=True)
    (command_dir / "changelog.md").write_text(
        "---\n"
        "title: Draft changelog\n"
        "description: Prepare a changelog entry\n"
        "---\n"
        "Draft a changelog for {{range}}.\n",
        encoding="utf-8",
    )

    listed = runner.invoke(app, ["commands", "list", "--project", str(tmp_path), "--output", "json"])
    run = runner.invoke(app, ["commands", "run", "changelog", "--project", str(tmp_path), "--output", "json"])
    catalog = build_command_catalog(tmp_path)
    palette = build_command_palette(catalog["commands"])
    slash_commands = build_slash_commands(palette, catalog["commands"])

    assert listed.exit_code == 0, listed.output
    payload = json.loads(listed.output)
    assert payload["schema_version"] == "harness.commands/v1"
    assert payload["execution_supported"] is False
    assert payload["contents_included"] is False
    assert payload["commands"][0]["name"] == "changelog"
    assert payload["commands"][0]["slash"] == "/changelog"
    assert payload["commands"][0]["template_variables"] == ["range"]

    assert run.exit_code == 1
    run_payload = json.loads(run.output)
    assert run_payload["schema_version"] == "harness.command_action/v1"
    assert run_payload["ok"] is False
    assert run_payload["execution_started"] is False
    assert run_payload["process_started"] is False
    assert run_payload["network_called"] is False
    assert run_payload["filesystem_modified"] is False
    assert run_payload["permission_granting"] is False

    assert any(group["id"] == "project_commands" for group in palette["groups"])
    custom_entry = next(entry for entry in palette["entries"] if entry["id"] == "project_commands.changelog")
    assert custom_entry["custom_command"] is True
    custom_slash = next(command for command in slash_commands["commands"] if command["name"] == "changelog")
    assert custom_slash["slash"] == "/changelog"
    assert custom_slash["custom_command"] is True


def test_cli_session_share_local_snapshot_and_hosted_fail_closed(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    session = store.create_session(title="CLI share")
    message = store.append_session_message(session.id, "user", "token sk-abcdefghijklmnopqrstuvwxyz")
    store.append_session_part(session.id, message.id, "text", text="token sk-abcdefghijklmnopqrstuvwxyz")
    run = store.create_run("share run", "phase_1a_test", status="succeeded", session_id=session.id)
    artifact_path = store.initialize_run_artifacts(run.id)["final_report"]
    artifact_path.write_text("artifact body should not be included\n", encoding="utf-8")
    store.register_artifact(run.id, "final_report", artifact_path, session_id=session.id)

    local = runner.invoke(app, ["session", "share", session.id, "--project", str(tmp_path), "--output", "json"])
    hosted = runner.invoke(
        app,
        ["session", "share", session.id, "--hosted", "--project", str(tmp_path), "--output", "json"],
    )

    assert local.exit_code == 0, local.output
    payload = json.loads(local.output)
    assert payload["schema_version"] == "harness.session_share/v1"
    assert payload["share_mode"] == "local_snapshot"
    assert payload["hosted_share_supported"] is False
    assert payload["hosted_url"] is None
    assert payload["artifact_files_included"] is False
    assert payload["artifact_references"][0]["contents_included"] is False
    assert payload["artifact_references"][0]["file_included"] is False
    assert payload["network_called"] is False
    assert payload["filesystem_modified"] is False
    assert payload["permission_granting"] is False
    assert payload["snapshot_sha256"]
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in local.output
    assert "[REDACTED_SECRET]" in local.output
    assert "artifact body should not be included" not in local.output

    assert hosted.exit_code == 1
    hosted_payload = json.loads(hosted.output)
    assert hosted_payload["schema_version"] == "harness.session_share_action/v1"
    assert hosted_payload["ok"] is False
    assert hosted_payload["hosted_share_supported"] is False
    assert hosted_payload["network_called"] is False
    assert hosted_payload["filesystem_modified"] is False
    assert hosted_payload["permission_granting"] is False


def test_cli_workspaces_catalog_and_actions_are_metadata_only(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0

    listed = runner.invoke(app, ["workspaces", "list", "--project", str(tmp_path), "--output", "json"])
    current = runner.invoke(app, ["workspaces", "current", "--project", str(tmp_path), "--output", "json"])
    clients = runner.invoke(app, ["workspaces", "clients", "--project", str(tmp_path), "--output", "json"])
    attach = runner.invoke(app, ["workspaces", "attach", "ws_other", "--project", str(tmp_path), "--output", "json"])
    sync = runner.invoke(app, ["workspaces", "sync", "ws_other", "--project", str(tmp_path), "--output", "json"])
    steal = runner.invoke(app, ["workspaces", "steal", "ws_other", "--client", "client_other", "--project", str(tmp_path), "--output", "json"])
    dispose = runner.invoke(app, ["workspaces", "dispose", "ws_other", "--client", "client_other", "--project", str(tmp_path), "--output", "json"])

    assert listed.exit_code == 0, listed.output
    listed_payload = json.loads(listed.output)
    assert listed_payload["schema_version"] == "harness.workspaces/v1"
    assert listed_payload["registry_scope"] == "current_project_only"
    assert listed_payload["global_registry_supported"] is False
    assert listed_payload["remote_attach_supported"] is False
    assert listed_payload["sync_supported"] is False
    assert listed_payload["workspaces"][0]["path"] == str(tmp_path)
    assert listed_payload["workspaces"][0]["initialized"] is True
    assert listed_payload["network_called"] is False
    assert listed_payload["filesystem_modified"] is False
    assert listed_payload["process_started"] is False
    assert listed_payload["permission_granting"] is False

    assert current.exit_code == 0, current.output
    current_payload = json.loads(current.output)
    assert current_payload["schema_version"] == "harness.workspace/v1"
    assert current_payload["workspace"]["id"] == listed_payload["current_workspace_id"]

    assert clients.exit_code == 0, clients.output
    clients_payload = json.loads(clients.output)
    assert clients_payload["schema_version"] == "harness.workspace_clients/v1"
    assert clients_payload["clients"] == []
    assert clients_payload["client_registration_supported"] is False
    assert clients_payload["conflict_detection_supported"] is False
    assert clients_payload["steal_supported"] is False
    assert clients_payload["dispose_supported"] is False
    assert clients_payload["network_called"] is False
    assert clients_payload["filesystem_modified"] is False
    assert clients_payload["permission_granting"] is False

    for result, action in [(attach, "attach"), (sync, "sync"), (steal, "steal"), (dispose, "dispose")]:
        assert result.exit_code == 1
        payload = json.loads(result.output)
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


def test_cli_session_replay_supports_cursor_without_execution(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    session = store.create_session(title="Replay CLI")
    message = store.append_session_message(session.id, "user", "Replay me")
    store.append_session_part(session.id, message.id, "text", text="Replay me")

    first = runner.invoke(
        app,
        ["session", "replay", session.id, "--limit", "1", "--project", str(tmp_path), "--output", "json"],
    )
    assert first.exit_code == 0, first.output
    first_payload = json.loads(first.output)
    second = runner.invoke(
        app,
        [
            "session",
            "replay",
            session.id,
            "--after-seq",
            str(first_payload["next_after_seq"]),
            "--project",
            str(tmp_path),
            "--output",
            "json",
        ],
    )

    assert first_payload["schema_version"] == "harness.session_replay/v1"
    assert first_payload["event_count"] == 1
    assert first_payload["has_more"] is True
    assert first_payload["next_after_seq"] == 1
    assert first_payload["execution_started"] is False
    assert first_payload["network_called"] is False
    assert first_payload["filesystem_modified"] is False
    assert first_payload["permission_granting"] is False
    assert second.exit_code == 0, second.output
    second_payload = json.loads(second.output)
    assert second_payload["after_seq"] == first_payload["next_after_seq"]
    assert second_payload["event_count"] >= 2
    assert all(event["seq"] > first_payload["next_after_seq"] for event in second_payload["events"])
    assert second_payload["source"] == "append_only_event_store"
    assert second_payload["execution_started"] is False
    assert second_payload["network_called"] is False
    assert second_payload["permission_granting"] is False


def test_cli_session_status_and_inspect_surface_latest_ui_activation_and_model_validation(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    session = store.create_session(
        title="UI action CLI",
        raw_model_ref="codex_cli/not-a-real-model",
        provider_id="codex_cli",
        model_id="not-a-real-model",
    )
    store.append_store_event(
        EventStreamType.SESSION,
        session.id,
        "tui.ui_activation.applied",
        {
            "source": "slash",
            "entry_id": "ui_controls.settings",
            "activation_kind": "ui_action",
            "action": {"type": "focus_section", "section_id": "settings"},
            "ui_action_applied": True,
            "command_started": False,
            "process_started": False,
            "filesystem_modified": False,
            "permission_granting": False,
            "authority_granting": False,
        },
        session_id=session.id,
    )

    inspected = runner.invoke(app, ["session", "inspect", session.id, "--project", str(tmp_path), "--output", "json"])
    status = runner.invoke(app, ["session", "status", session.id, "--project", str(tmp_path), "--output", "json"])
    text = runner.invoke(app, ["session", "status", session.id, "--project", str(tmp_path)])

    assert inspected.exit_code == 0, inspected.output
    inspected_payload = json.loads(inspected.output)
    assert inspected_payload["schema_version"] == "harness.session/v1"
    assert inspected_payload["model_validation"]["raw_model_ref"] == "codex_cli/not-a-real-model"
    assert inspected_payload["model_validation"]["executable"] is False
    assert inspected_payload["model_validation"]["blocked_reasons"] == ["model_unknown"]
    assert inspected_payload["model_validation"]["provider_execution_started"] is False
    assert inspected_payload["model_validation"]["model_execution_started"] is False
    assert inspected_payload["model_validation"]["hidden_provider_fallback"] is False
    assert inspected_payload["model_validation"]["hidden_model_fallback"] is False
    assert inspected_payload["model_validation"]["no_hidden_fallback"] is True
    assert inspected_payload["model_validation"]["permission_granting"] is False
    assert inspected_payload["model_validation"]["authority_granting"] is False
    assert inspected_payload["latest_ui_activation"]["entry_id"] == "ui_controls.settings"
    assert inspected_payload["latest_ui_activation"]["action_type"] == "focus_section"
    assert inspected_payload["latest_ui_activation"]["evidence_status"] == "ui_only_persisted"
    assert inspected_payload["latest_ui_activation"]["policy_boundary"]["kind"] == "safe_ui_activation"
    assert inspected_payload["latest_ui_activation"]["policy_boundary"]["command_execution_allowed"] is False
    assert inspected_payload["latest_ui_activation"]["blocked_reasons"] == []
    assert inspected_payload["latest_ui_activation"]["command_started"] is False
    assert inspected_payload["latest_ui_activation"]["process_started"] is False
    assert inspected_payload["latest_ui_activation"]["filesystem_modified"] is False
    assert inspected_payload["latest_ui_activation"]["permission_granting"] is False
    assert inspected_payload["latest_ui_activation"]["authority_granting"] is False

    assert status.exit_code == 0, status.output
    status_payload = json.loads(status.output)
    assert status_payload["schema_version"] == "harness.session_status/v1"
    assert status_payload["model_validation"]["raw_model_ref"] == "codex_cli/not-a-real-model"
    assert status_payload["model_validation"]["executable"] is False
    assert status_payload["model_validation"]["blocked_reasons"] == ["model_unknown"]
    assert status_payload["model_validation"]["provider_execution_started"] is False
    assert status_payload["model_validation"]["hidden_model_fallback"] is False
    assert status_payload["model_validation"]["no_hidden_fallback"] is True
    assert status_payload["latest_ui_activation"]["entry_id"] == "ui_controls.settings"
    assert status_payload["latest_ui_activation"]["authority_granting"] is False
    assert status_payload["permission_granting"] is False

    assert text.exit_code == 0, text.output
    assert "Model: codex_cli/not-a-real-model" in text.output
    assert "Model executable: False" in text.output
    assert "Model blocked: model_unknown" in text.output
    assert "No hidden fallback: True" in text.output
    assert "Latest UI action: ui_controls.settings action=focus_section source=slash" in text.output
    assert "UI action flags: command=False process=False filesystem=False permission=False authority=False" in text.output


def test_cli_server_lifecycle_mdns_and_dispose_are_safe_contracts(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0

    lifecycle = runner.invoke(app, ["server", "lifecycle", "--project", str(tmp_path), "--output", "json"])
    mdns = runner.invoke(app, ["server", "mdns", "--output", "json"])
    dispose = runner.invoke(app, ["server", "dispose", "--output", "json"])

    assert lifecycle.exit_code == 0, lifecycle.output
    lifecycle_payload = json.loads(lifecycle.output)
    assert lifecycle_payload["schema_version"] == "harness.local_server_lifecycle/v1"
    assert lifecycle_payload["dispose_supported"] is False
    assert lifecycle_payload["remote_attach_supported"] is True
    assert lifecycle_payload["sse_supported"] is True
    assert lifecycle_payload["websocket_supported"] is False
    assert lifecycle_payload["mdns_supported"] is False
    assert lifecycle_payload["process_stopped"] is False
    assert lifecycle_payload["permission_granting"] is False

    assert mdns.exit_code == 0, mdns.output
    mdns_payload = json.loads(mdns.output)
    assert mdns_payload["schema_version"] == "harness.local_server_mdns/v1"
    assert mdns_payload["enabled"] is False
    assert mdns_payload["advertised"] is False
    assert mdns_payload["network_broadcast_started"] is False
    assert mdns_payload["network_called"] is False
    assert mdns_payload["permission_granting"] is False

    assert dispose.exit_code == 1
    dispose_payload = json.loads(dispose.output)
    assert dispose_payload["schema_version"] == "harness.local_server_dispose/v1"
    assert dispose_payload["ok"] is False
    assert dispose_payload["dispose_supported"] is False
    assert dispose_payload["process_stopped"] is False
    assert dispose_payload["filesystem_modified"] is False
    assert dispose_payload["permission_granting"] is False


def test_cli_web_client_status_and_open_are_safe_contracts() -> None:
    root_opened = runner.invoke(app, ["web", "--output", "json"])
    status = runner.invoke(app, ["web", "client", "status", "--output", "json"])
    opened = runner.invoke(app, ["web", "client", "open", "--output", "json"])

    assert root_opened.exit_code == 1
    root_payload = json.loads(root_opened.output)
    assert root_payload["schema_version"] == "harness.web_client_action/v1"
    assert root_payload["ok"] is False
    assert root_payload["action"] == "open"
    assert root_payload["browser_opened"] is False
    assert root_payload["process_started"] is False
    assert root_payload["network_called"] is False
    assert root_payload["filesystem_modified"] is False
    assert root_payload["permission_granting"] is False

    assert status.exit_code == 0, status.output
    status_payload = json.loads(status.output)
    assert status_payload["schema_version"] == "harness.web_client/v1"
    assert status_payload["client_url"] == "http://127.0.0.1:8765/web"
    assert status_payload["client_available"] is False
    assert status_payload["static_assets_served"] is False
    assert status_payload["desktop_wrapper_available"] is False
    assert status_payload["open_supported"] is False
    assert status_payload["network_called"] is False
    assert status_payload["browser_opened"] is False
    assert status_payload["process_started"] is False
    assert status_payload["permission_granting"] is False

    assert opened.exit_code == 1
    opened_payload = json.loads(opened.output)
    assert opened_payload["schema_version"] == "harness.web_client_action/v1"
    assert opened_payload["ok"] is False
    assert opened_payload["action"] == "open"
    assert opened_payload["browser_opened"] is False
    assert opened_payload["process_started"] is False
    assert opened_payload["network_called"] is False
    assert opened_payload["filesystem_modified"] is False
    assert opened_payload["permission_granting"] is False


def test_cli_phase7_mentions_attachments_and_context_estimate_are_event_backed(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    (tmp_path / "src").mkdir()
    (tmp_path / "README.md").write_text("readme body\n", encoding="utf-8")
    (tmp_path / "src" / "app.py").write_text("print('hello')\n", encoding="utf-8")
    config_path = tmp_path / ".harness" / "config.yaml"
    config_data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config_data["references"] = {
        "guide": {"kind": "local", "path": "README.md", "description": "Local guide"},
    }
    config_path.write_text(yaml.safe_dump(config_data, sort_keys=False), encoding="utf-8")
    store = SQLiteStore(tmp_path)
    session = store.create_session(title="Phase 7 CLI")
    referenced = store.create_session(title="Referenced")

    mentions = runner.invoke(
        app,
        [
            "session",
            "mentions",
            session.id,
            f"Review @file:README.md with @directory:src and @reference:guide plus @session:{referenced.id}",
            "--project",
            str(tmp_path),
            "--output",
            "json",
        ],
    )
    attachments = runner.invoke(
        app,
        ["session", "attachments", session.id, "--file", "README.md", "--project", str(tmp_path), "--output", "json"],
    )
    estimate = runner.invoke(
        app,
        [
            "session",
            "context-estimate",
            session.id,
            "Review @file:README.md",
            "--file",
            "src/app.py",
            "--budget-tokens",
            "1000",
            "--project",
            str(tmp_path),
            "--output",
            "json",
        ],
    )

    assert mentions.exit_code == 0, mentions.output
    mention_payload = json.loads(mentions.output)
    assert mention_payload["schema_version"] == "harness.mention_resolution/v1"
    assert [mention["kind"] for mention in mention_payload["mentions"]] == ["file", "directory", "reference", "session"]
    assert mention_payload["contents_included"] is False
    assert mention_payload["permission_granting"] is False
    assert "readme body" not in mentions.output
    assert "print('hello')" not in mentions.output

    assert attachments.exit_code == 0, attachments.output
    attachment_payload = json.loads(attachments.output)
    assert attachment_payload["schema_version"] == "harness.attachment_preparation/v1"
    assert attachment_payload["attachments"][0]["path"] == "README.md"
    assert attachment_payload["attachments"][0]["accepted"] is True
    assert attachment_payload["attachments"][0]["contents_included"] is False
    assert attachment_payload["permission_granting"] is False

    assert estimate.exit_code == 0, estimate.output
    estimate_payload = json.loads(estimate.output)
    assert estimate_payload["schema_version"] == "harness.context_estimate/v1"
    assert estimate_payload["total_estimated_tokens"] > 0
    assert estimate_payload["within_budget"] is True
    assert {item["kind"] for item in estimate_payload["items"]} >= {"prompt", "mention:file", "attachment"}
    assert estimate_payload["contents_included"] is False
    assert estimate_payload["permission_granting"] is False

    events = [event.kind for event in SQLiteStore(tmp_path).list_store_events(EventStreamType.SESSION, session.id)]
    assert "session.mentions.resolved" in events
    assert "session.attachments.prepared" in events
    assert "session.context.estimated" in events


def test_cli_runs_default_output_remains_text(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    created = runner.invoke(
        app,
        [
            "dev",
            "create-run",
            "--goal",
            "text test run",
            "--task-type",
            "phase_1a_test",
            "--project",
            str(tmp_path),
        ],
    )
    assert created.exit_code == 0
    runs = runner.invoke(app, ["runs", "--project", str(tmp_path)])
    assert runs.exit_code == 0
    assert not runs.output.lstrip().startswith("{")
    assert runs.output.splitlines()[0] == "run_id\tstatus\tcreated_at\ttask_type\tgoal\tbackend"
    assert "\tcompleted\t" in runs.output


def test_cli_common_text_lists_include_stable_headers(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    task = runner.invoke(app, ["tasks", "add", "--title", "Text task", "--project", str(tmp_path)])
    assert task.exit_code == 0, task.output
    daemon = runner.invoke(app, ["daemon", "run-once", "--project", str(tmp_path)])
    assert daemon.exit_code == 0, daemon.output

    tasks = runner.invoke(app, ["tasks", "list", "--project", str(tmp_path)])
    daemon_status = runner.invoke(app, ["daemon", "status", "--project", str(tmp_path)])
    agents = runner.invoke(app, ["agents", "list", "--project", str(tmp_path)])

    assert tasks.exit_code == 0, tasks.output
    assert tasks.output.splitlines()[0] == "task_id\tstatus\tpriority\ttitle"
    assert daemon_status.exit_code == 0, daemon_status.output
    assert "daemon_id\tstatus\towner\theartbeat_at" in daemon_status.output
    assert agents.exit_code == 0, agents.output
    assert agents.output.strip() == "No project agents imported."


def test_cli_common_inspect_text_outputs_are_sectioned(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    bundle_path = tmp_path / "agent_bundle"
    scaffold = runner.invoke(
        app,
        [
            "agents",
            "scaffold",
            "section_agent",
            "--workbench",
            "quant",
            "--kind",
            "specialist",
            "--parent",
            "quant_research",
            "--model-profile",
            "local_reasoning",
            "--tool-policy",
            "read_only",
            "--memory-scope",
            "quant",
            "--output",
            str(bundle_path),
            "--output-format",
            "json",
        ],
    )
    assert scaffold.exit_code == 0, scaffold.output
    imported = runner.invoke(app, ["agents", "import", str(bundle_path), "--project", str(tmp_path), "--output", "json"])
    assert imported.exit_code == 0, imported.output

    agent_text = runner.invoke(app, ["agents", "inspect", "section_agent", "--project", str(tmp_path)])
    assert agent_text.exit_code == 0, agent_text.output
    assert "\nAgent\n" in agent_text.output
    assert "\nSource\n" in agent_text.output

    created_task = runner.invoke(
        app,
        ["tasks", "add", "--title", "Section task", "--project", str(tmp_path), "--output", "json"],
    )
    assert created_task.exit_code == 0, created_task.output
    task_id = json.loads(created_task.output)["task"]["id"]
    task_text = runner.invoke(app, ["tasks", "inspect", task_id, "--project", str(tmp_path)])
    assert task_text.exit_code == 0, task_text.output
    assert "\nTask\n" in task_text.output
    assert "\nScope\n" in task_text.output
    assert "\nGates\n" in task_text.output
    assert "\nExecution\n" in task_text.output

    leased = runner.invoke(app, ["daemon", "run-once", "--project", str(tmp_path), "--output", "json"])
    assert leased.exit_code == 0, leased.output
    lease_id = json.loads(leased.output)["lease"]["id"]
    lease_text = runner.invoke(app, ["daemon", "inspect-lease", lease_id, "--project", str(tmp_path)])
    assert lease_text.exit_code == 0, lease_text.output
    assert "\nLease\n" in lease_text.output
    assert "\nLinks\n" in lease_text.output
    assert "\nEligibility\n" in lease_text.output
    assert "\nRecovery\n" in lease_text.output

    policy_text = runner.invoke(
        app,
        [
            "policy",
            "explain",
            "--subject-kind",
            "agent",
            "--subject-id",
            "repo_inspector",
            "--project",
            str(tmp_path),
        ],
    )
    assert policy_text.exit_code == 0, policy_text.output
    assert "\nPolicy\n" in policy_text.output
    assert "\nLevels\n" in policy_text.output
    assert "\nApprovals\n" in policy_text.output
    assert "\nForbidden\n" in policy_text.output

    created_run = runner.invoke(
        app,
        [
            "dev",
            "create-run",
            "--goal",
            "section artifact run",
            "--task-type",
            "phase_1a_test",
            "--project",
            str(tmp_path),
        ],
    )
    assert created_run.exit_code == 0, created_run.output
    run_id = created_run.output.split("Created run ", 1)[1].splitlines()[0]
    artifacts_json = runner.invoke(app, ["artifacts", "list", run_id, "--project", str(tmp_path), "--output", "json"])
    assert artifacts_json.exit_code == 0, artifacts_json.output
    artifact_id = json.loads(artifacts_json.output)["artifacts"][0]["id"]
    artifacts_text = runner.invoke(app, ["artifacts", "list", run_id, "--project", str(tmp_path)])
    assert artifacts_text.exit_code == 0, artifacts_text.output
    assert artifacts_text.output.splitlines()[0] == "artifact_id\tkind\tstatus\tsha256\tsize_bytes"
    artifact_text = runner.invoke(app, ["artifacts", "inspect", artifact_id, "--project", str(tmp_path)])
    assert artifact_text.exit_code == 0, artifact_text.output
    assert "\nArtifact\n" in artifact_text.output
    assert "\nEvidence\n" in artifact_text.output


def test_cli_tasks_require_initialized_project(tmp_path) -> None:
    result = runner.invoke(
        app,
        ["tasks", "add", "--title", "Uninitialized task", "--project", str(tmp_path), "--output", "json"],
    )

    assert result.exit_code != 0
    assert not (tmp_path / ".harness").exists()


def test_cli_objectives_require_initialized_project(tmp_path) -> None:
    result = runner.invoke(
        app,
        ["objectives", "add", "--title", "Uninitialized objective", "--project", str(tmp_path), "--output", "json"],
    )

    assert result.exit_code != 0
    assert not (tmp_path / ".harness").exists()


def test_cli_objectives_add_list_and_inspect_support_json_and_text(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0

    text_created = runner.invoke(
        app,
        ["objectives", "add", "--title", "Text objective", "--project", str(tmp_path)],
    )
    assert text_created.exit_code == 0
    assert "Created objective obj_" in text_created.output

    created = runner.invoke(
        app,
        [
            "objectives",
            "add",
            "--title",
            "Queue hardening",
            "--description",
            "Build objective persistence.",
            "--workbench",
            "coding",
            "--priority",
            "7",
            "--project",
            str(tmp_path),
            "--output",
            "json",
        ],
    )

    assert created.exit_code == 0
    created_payload = json.loads(created.output)
    assert created_payload["schema_version"] == "harness.objective/v1"
    assert created_payload["ok"] is True
    objective = created_payload["objective"]
    assert objective["id"].startswith("obj_")
    assert objective["status"] == "active"
    assert objective["title"] == "Queue hardening"
    assert objective["description"] == "Build objective persistence."
    assert objective["workbench_id"] == "coding"

    listed = runner.invoke(app, ["objectives", "list", "--project", str(tmp_path), "--output", "json"])
    assert listed.exit_code == 0
    listed_payload = json.loads(listed.output)
    assert listed_payload["schema_version"] == "harness.objectives/v1"
    assert [item["id"] for item in listed_payload["objectives"]][0] == objective["id"]

    text_listed = runner.invoke(app, ["objectives", "list", "--project", str(tmp_path)])
    assert text_listed.exit_code == 0
    assert f"{objective['id']}\tactive\t7\tQueue hardening" in text_listed.output

    inspected = runner.invoke(
        app,
        ["objectives", "inspect", objective["id"], "--project", str(tmp_path), "--output", "json"],
    )
    assert inspected.exit_code == 0
    assert json.loads(inspected.output)["objective"]["id"] == objective["id"]

    text_inspected = runner.invoke(app, ["objectives", "inspect", objective["id"], "--project", str(tmp_path)])
    assert text_inspected.exit_code == 0
    assert f"Objective: {objective['id']}" in text_inspected.output
    assert "Workbench: coding" in text_inspected.output


def test_cli_objectives_reject_invalid_builtin_registry_refs(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0

    invalid_workbench = runner.invoke(
        app,
        [
            "objectives",
            "add",
            "--title",
            "Bad workbench",
            "--workbench",
            "missing_workbench",
            "--project",
            str(tmp_path),
            "--output",
            "json",
        ],
    )

    assert invalid_workbench.exit_code != 0
    payload = json.loads(invalid_workbench.output)
    assert payload["schema_version"] == "harness.objective/v1"
    assert payload["ok"] is False
    assert payload["errors"] == ["Workbench not found: missing_workbench"]


def test_cli_objectives_do_not_preflight_backends_or_expose_settings(tmp_path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    monkeypatch.setattr(
        "harness.cli.main.CodexCliBackend",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("objectives must not preflight Codex")),
    )
    monkeypatch.setattr(
        "harness.cli.main.LocalOpenAICompatibleBackend",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("objectives must not preflight local backend")),
    )

    result = runner.invoke(
        app,
        ["objectives", "add", "--title", "Safe objective", "--project", str(tmp_path), "--output", "json"],
    )

    assert result.exit_code == 0
    serialized = json.dumps(json.loads(result.output))
    assert "api_key" not in serialized
    assert "OPENAI_API_KEY" not in serialized
    assert "base_url" not in serialized


def test_cli_tasks_add_list_inspect_and_status_support_json(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0

    created = runner.invoke(
        app,
        [
            "tasks",
            "add",
            "--title",
            "Inspect repo",
            "--description",
            "Read repository state.",
            "--agent",
            "repo_inspector",
            "--workbench",
            "coding",
            "--priority",
            "7",
            "--project",
            str(tmp_path),
            "--output",
            "json",
        ],
    )

    assert created.exit_code == 0
    created_payload = json.loads(created.output)
    assert created_payload["schema_version"] == "harness.task/v1"
    assert created_payload["ok"] is True
    task = created_payload["task"]
    assert task["id"].startswith("task_")
    assert task["status"] == "ready"
    assert task["idempotency_key"].startswith("task_idem_")
    assert task["title"] == "Inspect repo"
    assert task["agent_id"] == "repo_inspector"
    assert task["workbench_id"] == "coding"
    assert task["spec_source_kind"] == "builtin"

    listed = runner.invoke(app, ["tasks", "list", "--project", str(tmp_path), "--output", "json"])
    assert listed.exit_code == 0
    listed_payload = json.loads(listed.output)
    assert listed_payload["schema_version"] == "harness.tasks/v1"
    assert [item["id"] for item in listed_payload["tasks"]] == [task["id"]]

    inspected = runner.invoke(
        app,
        ["tasks", "inspect", task["id"], "--project", str(tmp_path), "--output", "json"],
    )
    assert inspected.exit_code == 0
    assert json.loads(inspected.output)["task"]["id"] == task["id"]

    updated = runner.invoke(
        app,
        ["tasks", "status", task["id"], "succeeded", "--project", str(tmp_path), "--output", "json"],
    )
    assert updated.exit_code == 0
    assert json.loads(updated.output)["task"]["status"] == "succeeded"


def test_cli_tasks_support_objective_dependencies_approvals_and_graph(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    objective_result = runner.invoke(
        app,
        [
            "objectives",
            "add",
            "--title",
            "Queue objective",
            "--workbench",
            "coding",
            "--project",
            str(tmp_path),
            "--output",
            "json",
        ],
    )
    assert objective_result.exit_code == 0
    objective_id = json.loads(objective_result.output)["objective"]["id"]

    upstream_result = runner.invoke(
        app,
        [
            "tasks",
            "add",
            "--title",
            "Upstream",
            "--objective",
            objective_id,
            "--project",
            str(tmp_path),
            "--output",
            "json",
        ],
    )
    assert upstream_result.exit_code == 0
    upstream_id = json.loads(upstream_result.output)["task"]["id"]

    downstream_result = runner.invoke(
        app,
        [
            "tasks",
            "add",
            "--title",
            "Downstream",
            "--objective",
            objective_id,
            "--depends-on",
            upstream_id,
            "--requires-approval",
            "hosted_provider",
            "--project",
            str(tmp_path),
            "--output",
            "json",
        ],
    )
    assert downstream_result.exit_code == 0
    downstream = json.loads(downstream_result.output)["task"]
    assert downstream["objective_id"] == objective_id
    assert downstream["status"] == "waiting_approval"
    assert downstream["depends_on"] == [upstream_id]
    assert downstream["required_approvals"] == ["hosted_provider"]

    listed = runner.invoke(
        app,
        ["tasks", "list", "--objective", objective_id, "--project", str(tmp_path), "--output", "json"],
    )
    assert listed.exit_code == 0
    listed_tasks = json.loads(listed.output)["tasks"]
    assert {task["id"] for task in listed_tasks} == {upstream_id, downstream["id"]}

    graph = runner.invoke(
        app,
        ["tasks", "graph", "--objective", objective_id, "--project", str(tmp_path), "--output", "json"],
    )
    assert graph.exit_code == 0
    graph_payload = json.loads(graph.output)
    assert graph_payload["schema_version"] == "harness.task_graph/v1"
    assert graph_payload["ok"] is True
    assert [objective["id"] for objective in graph_payload["objectives"]] == [objective_id]
    assert graph_payload["dependencies"][0]["upstream_task_id"] == upstream_id
    assert graph_payload["dependencies"][0]["downstream_task_id"] == downstream["id"]
    assert graph_payload["blocked_reasons"][downstream["id"]][0] == {
        "kind": "unsatisfied_dependency",
        "task_id": upstream_id,
        "status": "ready",
    }


def test_cli_tasks_reject_invalid_objective_and_dependency_refs(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    task = runner.invoke(
        app,
        ["tasks", "add", "--title", "Task", "--project", str(tmp_path), "--output", "json"],
    )
    assert task.exit_code == 0
    task_id = json.loads(task.output)["task"]["id"]

    bad_objective = runner.invoke(
        app,
        [
            "tasks",
            "add",
            "--title",
            "Bad objective",
            "--objective",
            "obj_missing",
            "--project",
            str(tmp_path),
            "--output",
            "json",
        ],
    )
    bad_dependency = runner.invoke(
        app,
        [
            "tasks",
            "add",
            "--title",
            "Bad dependency",
            "--depends-on",
            "task_missing",
            "--project",
            str(tmp_path),
            "--output",
            "json",
        ],
    )
    bad_filter = runner.invoke(
        app,
        ["tasks", "list", "--objective", "obj_missing", "--project", str(tmp_path), "--output", "json"],
    )
    cycle = runner.invoke(
        app,
        [
            "tasks",
            "add",
            "--title",
            "Cycle",
            "--depends-on",
            task_id,
            "--project",
            str(tmp_path),
            "--output",
            "json",
        ],
    )

    assert bad_objective.exit_code != 0
    assert json.loads(bad_objective.output)["errors"] == ["Objective not found: obj_missing"]
    assert bad_dependency.exit_code != 0
    assert json.loads(bad_dependency.output)["errors"] == ["Task not found: task_missing"]
    assert bad_filter.exit_code != 0
    assert json.loads(bad_filter.output)["errors"] == ["Objective not found: obj_missing"]
    assert cycle.exit_code == 0


def test_cli_tasks_cancel_and_retry_support_json(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    cancel_candidate = runner.invoke(
        app,
        ["tasks", "add", "--title", "Cancel me", "--project", str(tmp_path), "--output", "json"],
    )
    retry_candidate = runner.invoke(
        app,
        ["tasks", "add", "--title", "Retry me", "--project", str(tmp_path), "--output", "json"],
    )
    assert cancel_candidate.exit_code == 0
    assert retry_candidate.exit_code == 0
    cancel_task = json.loads(cancel_candidate.output)["task"]
    retry_task = json.loads(retry_candidate.output)["task"]
    retry_idempotency_key = retry_task["idempotency_key"]

    cancelled = runner.invoke(
        app,
        ["tasks", "cancel", cancel_task["id"], "--project", str(tmp_path), "--output", "json"],
    )
    assert cancelled.exit_code == 0
    cancelled_payload = json.loads(cancelled.output)
    assert cancelled_payload["schema_version"] == "harness.task/v1"
    assert cancelled_payload["task"]["status"] == "cancelled"

    assert (
        runner.invoke(
            app,
            ["tasks", "status", retry_task["id"], "running", "--project", str(tmp_path), "--output", "json"],
        ).exit_code
        == 0
    )
    assert (
        runner.invoke(
            app,
            ["tasks", "status", retry_task["id"], "failed", "--project", str(tmp_path), "--output", "json"],
        ).exit_code
        == 0
    )
    retried = runner.invoke(
        app,
        ["tasks", "retry", retry_task["id"], "--project", str(tmp_path), "--output", "json"],
    )
    assert retried.exit_code == 0
    retried_task = json.loads(retried.output)["task"]
    assert retried_task["status"] == "ready"
    assert retried_task["idempotency_key"] == retry_idempotency_key


def test_cli_tasks_cancel_and_retry_reject_invalid_states(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    task_result = runner.invoke(
        app,
        ["tasks", "add", "--title", "Terminal task", "--project", str(tmp_path), "--output", "json"],
    )
    assert task_result.exit_code == 0
    task_id = json.loads(task_result.output)["task"]["id"]

    retry_ready = runner.invoke(
        app,
        ["tasks", "retry", task_id, "--project", str(tmp_path), "--output", "json"],
    )
    assert retry_ready.exit_code != 0
    assert json.loads(retry_ready.output)["errors"] == ["Task retry requires failed status: ready"]

    assert (
        runner.invoke(
            app,
            ["tasks", "status", task_id, "succeeded", "--project", str(tmp_path), "--output", "json"],
        ).exit_code
        == 0
    )
    cancel_succeeded = runner.invoke(
        app,
        ["tasks", "cancel", task_id, "--project", str(tmp_path), "--output", "json"],
    )
    assert cancel_succeeded.exit_code != 0
    assert json.loads(cancel_succeeded.output)["errors"] == [
        "Invalid task transition: succeeded -> cancelled"
    ]


def test_cli_tasks_cancel_retry_require_initialized_project(tmp_path) -> None:
    for command in ["cancel", "retry"]:
        result = runner.invoke(
            app,
            ["tasks", command, "task_missing", "--project", str(tmp_path), "--output", "json"],
        )
        assert result.exit_code != 0
    assert not (tmp_path / ".harness").exists()


def test_cli_tasks_cancel_retry_do_not_preflight_backends_or_expose_settings(tmp_path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    monkeypatch.setattr(
        "harness.cli.main.CodexCliBackend",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("tasks must not preflight Codex")),
    )
    monkeypatch.setattr(
        "harness.cli.main.LocalOpenAICompatibleBackend",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("tasks must not preflight local backend")),
    )
    task_result = runner.invoke(
        app,
        ["tasks", "add", "--title", "Lifecycle task", "--project", str(tmp_path), "--output", "json"],
    )
    task_id = json.loads(task_result.output)["task"]["id"]

    cancelled = runner.invoke(
        app,
        ["tasks", "cancel", task_id, "--project", str(tmp_path), "--output", "json"],
    )

    assert cancelled.exit_code == 0
    serialized = json.dumps(json.loads(cancelled.output))
    assert "api_key" not in serialized
    assert "OPENAI_API_KEY" not in serialized
    assert "base_url" not in serialized


def test_cli_tasks_run_next_selects_task_without_creating_run_artifacts(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    low = store.create_task(title="Low", priority=0)
    high = store.create_task(title="High", priority=10)

    result = runner.invoke(app, ["tasks", "run-next", "--project", str(tmp_path), "--output", "json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["schema_version"] == "harness.task_run_next/v1"
    assert payload["ok"] is True
    assert payload["selected_task"]["id"] == high.id
    assert payload["selected_task"]["status"] == "leased"
    assert payload["attempt"]["task_id"] == high.id
    assert payload["attempt"]["status"] == "leased"
    assert payload["lease"]["task_id"] == high.id
    assert payload["lease"]["attempt_id"] == payload["attempt"]["id"]
    assert payload["lease"]["owner"] == "manual_cli"
    assert payload["lease"]["status"] == "active"
    assert store.get_task(low.id).status.value == "ready"
    assert store.list_runs() == []
    assert not any((tmp_path / ".harness" / "runs").iterdir())

    text_result = runner.invoke(app, ["tasks", "run-next", "--project", str(tmp_path)])
    assert text_result.exit_code == 0
    assert text_result.output.startswith(f"Leased task {low.id}")


def test_cli_tasks_run_next_does_not_select_active_lease_twice(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    first = SQLiteStore(tmp_path).create_task(title="First", priority=10)
    second = SQLiteStore(tmp_path).create_task(title="Second", priority=5)

    first_result = runner.invoke(app, ["tasks", "run-next", "--project", str(tmp_path), "--output", "json"])
    second_result = runner.invoke(app, ["tasks", "run-next", "--project", str(tmp_path), "--output", "json"])

    assert first_result.exit_code == 0
    assert second_result.exit_code == 0
    first_payload = json.loads(first_result.output)
    second_payload = json.loads(second_result.output)
    assert first_payload["selected_task"]["id"] == first.id
    assert second_payload["selected_task"]["id"] == second.id
    assert second_payload["selected_task"]["id"] != first_payload["selected_task"]["id"]


def test_cli_tasks_run_next_returns_null_without_runnable_task(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0

    result = runner.invoke(app, ["tasks", "run-next", "--project", str(tmp_path), "--output", "json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["schema_version"] == "harness.task_run_next/v1"
    assert payload["ok"] is True
    assert payload["selected_task"] is None
    assert payload["attempt"] is None
    assert payload["lease"] is None


def test_cli_daemon_run_once_status_and_stop_are_non_executing(tmp_path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    task = store.create_task(title="Daemon task", priority=10)
    monkeypatch.setattr(
        "harness.cli.main.CodexCliBackend",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("daemon must not preflight Codex")),
    )
    monkeypatch.setattr(
        "harness.cli.main.LocalOpenAICompatibleBackend",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("daemon must not preflight local backend")),
    )
    monkeypatch.setattr(
        "harness.cli.main.DockerImageManager",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("daemon must not touch Docker")),
    )

    tick = runner.invoke(app, ["daemon", "run-once", "--project", str(tmp_path), "--output", "json"])
    status = runner.invoke(app, ["daemon", "status", "--project", str(tmp_path), "--output", "json"])
    stopped = runner.invoke(app, ["daemon", "stop", "--project", str(tmp_path), "--output", "json"])

    assert tick.exit_code == 0, tick.output
    tick_payload = json.loads(tick.output)
    assert tick_payload["schema_version"] == "harness.daemon_tick/v1"
    assert tick_payload["ok"] is True
    assert tick_payload["decision"] == "leased_task"
    assert tick_payload["selected_task"]["id"] == task.id
    assert tick_payload["selected_task"]["status"] == "leased"
    assert tick_payload["attempt"]["run_id"] is None
    assert tick_payload["lease"]["owner"].startswith("local_daemon:")

    assert status.exit_code == 0, status.output
    status_payload = json.loads(status.output)
    assert status_payload["schema_version"] == "harness.daemon_status/v1"
    assert status_payload["ok"] is True
    assert len(status_payload["active_daemons"]) == 1
    assert {event["event_type"] for event in status_payload["latest_events"]} >= {"start", "tick"}

    assert stopped.exit_code == 0, stopped.output
    stopped_payload = json.loads(stopped.output)
    assert stopped_payload["schema_version"] == "harness.daemon_status/v1"
    assert stopped_payload["ok"] is True
    assert stopped_payload["active_daemons"] == []
    assert stopped_payload["stopped_daemons"][0]["status"] == "stopped"
    assert store.list_runs() == []
    assert not any((tmp_path / ".harness" / "runs").iterdir())

    serialized = tick.output + status.output + stopped.output
    assert "api_key" not in serialized
    assert "OPENAI_API_KEY" not in serialized
    assert "base_url" not in serialized
    assert "environment" not in serialized


def test_cli_daemon_recover_expires_lease_without_execution(tmp_path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    task = store.create_task(title="Recover task", priority=10)
    monkeypatch.setattr(
        "harness.cli.main.CodexCliBackend",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("daemon recover must not preflight Codex")),
    )
    monkeypatch.setattr(
        "harness.cli.main.LocalOpenAICompatibleBackend",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("daemon recover must not preflight local backend")),
    )
    monkeypatch.setattr(
        "harness.cli.main.DockerImageManager",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("daemon recover must not touch Docker")),
    )

    tick = runner.invoke(app, ["daemon", "run-once", "--project", str(tmp_path), "--output", "json"])
    lease_id = json.loads(tick.output)["lease"]["id"]
    with store.connect() as conn:
        conn.execute(
            "UPDATE task_leases SET expires_at = ? WHERE id = ?",
            ("2026-01-01T00:00:00+00:00", lease_id),
        )

    recovered = runner.invoke(app, ["daemon", "recover", "--project", str(tmp_path), "--output", "json"])
    recovered_again = runner.invoke(app, ["daemon", "recover", "--project", str(tmp_path), "--output", "json"])

    assert recovered.exit_code == 0, recovered.output
    payload = json.loads(recovered.output)
    assert payload["schema_version"] == "harness.daemon_recovery/v1"
    assert payload["ok"] is True
    assert payload["expired_leases"][0]["id"] == lease_id
    assert payload["expired_leases"][0]["status"] == "expired"
    assert payload["recovered_tasks"][0]["id"] == task.id
    assert payload["recovered_tasks"][0]["status"] == "ready"
    assert payload["events"][0]["event_type"] == "recover_lease"
    assert recovered_again.exit_code == 0
    again_payload = json.loads(recovered_again.output)
    assert again_payload["expired_leases"] == []
    assert again_payload["recovered_tasks"] == []
    assert store.list_runs() == []
    assert not any((tmp_path / ".harness" / "runs").iterdir())

    serialized = recovered.output + recovered_again.output
    assert "api_key" not in serialized
    assert "OPENAI_API_KEY" not in serialized
    assert "base_url" not in serialized
    assert "environment" not in serialized


def test_cli_daemon_run_once_returns_no_eligible_task(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0

    result = runner.invoke(app, ["daemon", "run-once", "--project", str(tmp_path), "--output", "json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["schema_version"] == "harness.daemon_tick/v1"
    assert payload["ok"] is True
    assert payload["decision"] == "no_eligible_task"
    assert payload["selected_task"] is None
    assert payload["attempt"] is None
    assert payload["lease"] is None


def test_cli_daemon_run_once_pauses_approval_required_tasks(tmp_path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    task = store.create_task(title="Approval", required_approvals=["hosted_provider"])
    monkeypatch.setattr(
        "harness.cli.main.CodexCliBackend",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("daemon must not preflight Codex")),
    )
    monkeypatch.setattr(
        "harness.cli.main.LocalOpenAICompatibleBackend",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("daemon must not preflight local backend")),
    )
    monkeypatch.setattr(
        "harness.cli.main.DockerImageManager",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("daemon must not touch Docker")),
    )

    tick = runner.invoke(app, ["daemon", "run-once", "--project", str(tmp_path), "--output", "json"])
    status = runner.invoke(app, ["daemon", "status", "--project", str(tmp_path), "--output", "json"])

    assert tick.exit_code == 0, tick.output
    tick_payload = json.loads(tick.output)
    assert tick_payload["schema_version"] == "harness.daemon_tick/v1"
    assert tick_payload["decision"] == "paused"
    assert tick_payload["selected_task"] is None
    assert tick_payload["attempt"] is None
    assert tick_payload["lease"] is None
    assert tick_payload["pause_reasons"][0]["task_id"] == task.id
    assert tick_payload["pause_reasons"][0]["decision"] == "waiting_approval"
    assert tick_payload["pause_reasons"][0]["required_approvals"] == ["hosted_provider"]

    assert status.exit_code == 0, status.output
    status_payload = json.loads(status.output)
    assert status_payload["schema_version"] == "harness.daemon_status/v1"
    assert status_payload["paused_tasks"][0]["task_id"] == task.id
    assert status_payload["paused_tasks"][0]["decision"] == "waiting_approval"
    assert store.get_task(task.id).status.value == "waiting_approval"
    assert store.list_runs() == []
    assert not any((tmp_path / ".harness" / "runs").iterdir())

    serialized = tick.output + status.output
    assert "api_key" not in serialized
    assert "OPENAI_API_KEY" not in serialized
    assert "base_url" not in serialized
    assert "environment" not in serialized


def test_cli_daemon_execute_dry_run_links_run_without_backends_or_docker(tmp_path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    monkeypatch.setattr(
        "harness.cli.main.CodexCliBackend",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("dry run must not preflight Codex")),
    )
    monkeypatch.setattr(
        "harness.cli.main.LocalOpenAICompatibleBackend",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("dry run must not preflight local backend")),
    )
    monkeypatch.setattr(
        "harness.cli.main.DockerImageManager",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("dry run must not touch Docker")),
    )

    task_result = runner.invoke(
        app,
        [
            "tasks",
            "add",
            "--title",
            "Dry run",
            "--execution-adapter",
            "dry_run",
            "--task-type",
            "phase_1a_test",
            "--project",
            str(tmp_path),
            "--output",
            "json",
        ],
    )
    assert task_result.exit_code == 0, task_result.output
    task_payload = json.loads(task_result.output)
    assert task_payload["task"]["metadata"] == {
        "execution_adapter": "dry_run",
        "task_type": "phase_1a_test",
    }

    tick = runner.invoke(app, ["daemon", "run-once", "--project", str(tmp_path), "--output", "json"])
    assert tick.exit_code == 0, tick.output
    tick_payload = json.loads(tick.output)
    assert tick_payload["decision"] == "leased_task"
    assert tick_payload["selected_task"]["status"] == "leased"
    assert tick_payload["attempt"]["run_id"] is None
    lease_id = tick_payload["lease"]["id"]

    executed = runner.invoke(
        app,
        ["daemon", "execute-dry-run", lease_id, "--project", str(tmp_path), "--output", "json"],
    )
    duplicate = runner.invoke(
        app,
        ["daemon", "execute-dry-run", lease_id, "--project", str(tmp_path), "--output", "json"],
    )

    assert executed.exit_code == 0, executed.output
    payload = json.loads(executed.output)
    assert payload["schema_version"] == "harness.daemon_execute_dry_run/v1"
    assert payload["ok"] is True
    assert payload["decision"] == "dry_run_no_tool_execution"
    assert payload["task"]["status"] == "succeeded"
    assert payload["attempt"]["status"] == "succeeded"
    assert payload["attempt"]["run_id"] == payload["run"]["id"]
    assert payload["lease"]["status"] == "released"
    assert payload["run"]["status"] == "completed"
    assert payload["run"]["task_type"] == "phase_1a_test"
    assert payload["manifest"]["schema_version"] == "harness.manifest/v1.1"
    assert payload["manifest"]["task_id"] == payload["task"]["id"]
    assert payload["manifest"]["backend_descriptor"] is None
    assert payload["manifest"]["backend_descriptor_sha256"] is None
    assert payload["manifest"]["context_provenance"]
    assert {record["source_kind"] for record in payload["manifest"]["context_provenance"]} >= {
        "run_goal",
        "task_metadata",
        "artifact",
    }
    assert "artifact_content_not_authority" in payload["manifest"]["untrusted_context_warnings"]
    assert {artifact["kind"] for artifact in payload["manifest"]["artifacts"]} >= {
        "events",
        "transcript",
        "final_report",
        "manifest",
    }

    assert duplicate.exit_code == 1
    duplicate_payload = json.loads(duplicate.output)
    assert duplicate_payload["schema_version"] == "harness.daemon_execute_dry_run/v1"
    assert duplicate_payload["ok"] is False
    assert duplicate_payload["errors"] == ["Dry-run execution requires active lease: released"]
    assert len(SQLiteStore(tmp_path).list_runs()) == 1

    serialized = task_result.output + tick.output + executed.output + duplicate.output
    assert "api_key" not in serialized
    assert "OPENAI_API_KEY" not in serialized
    assert "base_url" not in serialized
    assert "environment" not in serialized


def test_cli_daemon_adapters_lists_registered_descriptors_without_preflight(tmp_path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    monkeypatch.setattr(
        "harness.cli.main.CodexCliBackend",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("adapter listing must not preflight Codex")),
    )
    result = runner.invoke(app, ["daemon", "adapters", "--project", str(tmp_path), "--output", "json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema_version"] == "harness.execution_adapters/v1"
    adapter_ids = {adapter["id"] for adapter in payload["adapters"]}
    assert {"dry_run", "read_only_summary", "codex_isolated_edit", "repo_planning"} <= adapter_ids
    for adapter in payload["adapters"]:
        assert "Descriptors are documentation and validation metadata, not permission grants." in adapter["safety_notes"]


def test_cli_daemon_execute_dispatches_dry_run_through_registered_adapter(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    store.create_task(
        title="Generic dry run",
        metadata={"execution_adapter": "dry_run", "task_type": "phase_1a_test"},
    )
    leased = store.daemon_run_once("local_daemon:test:123", pid=123)
    assert leased.lease is not None

    result = runner.invoke(
        app,
        ["daemon", "execute", leased.lease.id, "--project", str(tmp_path), "--output", "json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema_version"] == "harness.daemon_execute/v1"
    assert payload["ok"] is True
    assert payload["adapter_id"] == "dry_run"
    assert payload["decision"] == "dry_run_no_tool_execution"
    assert payload["security_decision"]["schema_version"] == "harness.security_decision/v1"
    assert payload["security_decision"]["decision"] == "allow"
    assert payload["security_decision"]["adapter_id"] == "dry_run"
    assert payload["security_decision"]["task_type"] == "phase_1a_test"
    assert payload["security_decision"]["sandbox_profile_id"] == "none"
    assert payload["manifest"]["sandbox_profile"]["id"] == "none"
    assert payload["context_provenance"] == payload["manifest"]["context_provenance"]
    assert "artifact_content_not_authority" in payload["untrusted_context_warnings"]
    assert payload["task"]["status"] == "succeeded"
    assert payload["attempt"]["run_id"] == payload["run"]["id"]
    assert payload["adapter_result"]["schema_version"] == "harness.daemon_execute_dry_run/v1"


def test_cli_controls_disable_adapter_blocks_generic_execute_without_run(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    disabled = runner.invoke(
        app,
        [
            "controls",
            "disable",
            "--target-kind",
            "adapter",
            "--target-id",
            "dry_run",
            "--reason",
            "operator blocked OPENAI_API_KEY=secret",
            "--project",
            str(tmp_path),
            "--output",
            "json",
        ],
    )
    assert disabled.exit_code == 0, disabled.output
    disabled_payload = json.loads(disabled.output)
    assert disabled_payload["control"]["disabled"] is True
    assert "OPENAI_API_KEY=secret" not in disabled.output

    store = SQLiteStore(tmp_path)
    store.create_task(
        title="Generic dry run",
        metadata={"execution_adapter": "dry_run", "task_type": "phase_1a_test"},
    )
    leased = store.daemon_run_once("local_daemon:test:123", pid=123)
    assert leased.lease is not None

    inspected = runner.invoke(
        app,
        ["daemon", "inspect-lease", leased.lease.id, "--project", str(tmp_path), "--output", "json"],
    )
    result = runner.invoke(
        app,
        ["daemon", "execute", leased.lease.id, "--project", str(tmp_path), "--output", "json"],
    )

    assert inspected.exit_code == 0, inspected.output
    inspected_payload = json.loads(inspected.output)
    assert inspected_payload["security_decision"]["decision"] == "deny"
    assert inspected_payload["security_decision"]["reason_code"] == "control_disabled"

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["run"] is None
    assert payload["security_decision"]["decision"] == "deny"
    assert payload["security_decision"]["reason_code"] == "control_disabled"
    assert "OPENAI_API_KEY=secret" not in result.output
    assert store.list_runs() == []
    assert store.list_daemon_events()[0].metadata["reason_code"] == "control_disabled"


def test_cli_controls_task_type_backend_and_breaker_are_json_inspectable(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    task_type_disabled = runner.invoke(
        app,
        [
            "controls",
            "disable",
            "--target-kind",
            "task_type",
            "--target-id",
            "phase_1a_test",
            "--reason",
            "pause dry-run task type",
            "--project",
            str(tmp_path),
            "--output",
            "json",
        ],
    )
    backend_disabled = runner.invoke(
        app,
        [
            "controls",
            "disable",
            "--target-kind",
            "backend",
            "--target-id",
            "codex_cli",
            "--reason",
            "pause Codex backend",
            "--project",
            str(tmp_path),
            "--output",
            "json",
        ],
    )
    listed = runner.invoke(app, ["controls", "list", "--project", str(tmp_path), "--output", "json"])
    invalid = runner.invoke(
        app,
        [
            "controls",
            "disable",
            "--target-kind",
            "adapter",
            "--target-id",
            "missing",
            "--reason",
            "nope",
            "--project",
            str(tmp_path),
            "--output",
            "json",
        ],
    )

    assert task_type_disabled.exit_code == 0, task_type_disabled.output
    assert backend_disabled.exit_code == 0, backend_disabled.output
    assert listed.exit_code == 0, listed.output
    payload = json.loads(listed.output)
    assert payload["schema_version"] == "harness.execution_controls/v1"
    assert {(item["target_kind"], item["target_id"]) for item in payload["controls"]} >= {
        ("task_type", "phase_1a_test"),
        ("backend", "codex_cli"),
    }
    assert invalid.exit_code == 1
    invalid_payload = json.loads(invalid.output)
    assert invalid_payload["schema_version"] == "harness.execution_controls/v1"
    assert invalid_payload["ok"] is False
    assert invalid_payload["errors"] == ["Unknown registered adapter: missing"]


def test_cli_controls_breaker_opens_and_reset_closes_for_adapter(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    daemon = store.ensure_daemon(owner="test")
    for index in range(3):
        store.record_daemon_event(
            daemon.id,
            event_type="execution_adapter_rejected",
            message="Adapter execution failed.",
            metadata={
                "adapter_id": "dry_run",
                "reason_code": "adapter_execution_failed",
                "error": f"failure {index}",
            },
        )

    status = runner.invoke(app, ["controls", "breaker-status", "--project", str(tmp_path), "--output", "json"])
    assert status.exit_code == 0, status.output
    payload = json.loads(status.output)
    dry_run = next(item for item in payload["breakers"] if item["adapter_id"] == "dry_run")
    assert dry_run["status"] == "open"
    assert dry_run["failure_count"] == 3

    store.create_task(
        title="Generic dry run",
        metadata={"execution_adapter": "dry_run", "task_type": "phase_1a_test"},
    )
    leased = store.daemon_run_once("local_daemon:test:123", pid=123)
    assert leased.lease is not None
    rejected = runner.invoke(
        app,
        ["daemon", "execute", leased.lease.id, "--project", str(tmp_path), "--output", "json"],
    )
    assert rejected.exit_code == 1
    rejected_payload = json.loads(rejected.output)
    assert rejected_payload["security_decision"]["reason_code"] == "breaker_open"
    assert rejected_payload["blocked_state_explanations"][0]["code"] == "breaker_open"
    assert store.list_runs() == []

    reset = runner.invoke(
        app,
        [
            "controls",
            "breaker-reset",
            "dry_run",
            "--reason",
            "operator reviewed",
            "--project",
            str(tmp_path),
            "--output",
            "json",
        ],
    )
    assert reset.exit_code == 0, reset.output
    reset_payload = json.loads(reset.output)
    assert reset_payload["breaker"]["status"] == "closed"


def test_cli_daemon_execute_rejects_unknown_adapter_without_run(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    store.create_task(
        title="Unknown adapter",
        metadata={"execution_adapter": "unknown_adapter", "task_type": "phase_1a_test"},
    )
    leased = store.daemon_run_once("local_daemon:test:123", pid=123)
    assert leased.lease is not None

    result = runner.invoke(
        app,
        ["daemon", "execute", leased.lease.id, "--project", str(tmp_path), "--output", "json"],
    )

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["schema_version"] == "harness.daemon_execute/v1"
    assert payload["ok"] is False
    assert payload["decision"] == "execution_adapter_rejected"
    assert payload["adapter_id"] == "unknown_adapter"
    assert payload["security_decision"]["decision"] == "deny"
    assert payload["security_decision"]["reason_code"] == "unknown_adapter"
    assert payload["blocked_state_explanations"][0]["code"] == "unknown_adapter"
    assert payload["run"] is None
    assert payload["rejection_reasons"] == ["Unknown execution adapter: unknown_adapter."]
    assert store.list_runs() == []
    event = store.list_daemon_events()[0]
    assert event.event_type == "execution_adapter_rejected"
    assert event.metadata["reason_code"] == "unknown_adapter"


def test_cli_daemon_execute_rejects_unsafe_metadata_with_security_decision(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    task = store.create_task(
        title="Unsafe generic dispatch",
        metadata={"execution_adapter": "dry_run", "task_type": "phase_1a_test"},
    )
    leased = store.daemon_run_once("local_daemon:test:123", pid=123)
    assert leased.lease is not None
    with store.connect() as conn:
        conn.execute(
            "UPDATE tasks SET metadata_json = ? WHERE id = ?",
            (
                json.dumps(
                    {
                        "execution_adapter": "dry_run",
                        "task_type": "phase_1a_test",
                        "requires_external_network": True,
                    },
                    sort_keys=True,
                ),
                task.id,
            ),
        )

    result = runner.invoke(
        app,
        ["daemon", "execute", leased.lease.id, "--project", str(tmp_path), "--output", "json"],
    )

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["run"] is None
    assert payload["security_decision"]["decision"] == "deny"
    assert payload["security_decision"]["reason_code"] == "unsafe_metadata"
    assert payload["blocked_state_explanations"][0]["code"] == "unsafe_metadata"
    assert payload["security_decision"]["reasons"] == [
        "Execution rejected by task metadata: requires_external_network."
    ]
    assert store.list_runs() == []


def test_cli_daemon_execute_reports_approval_required_security_decision(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    task = store.create_task(
        title="Approval required",
        metadata={"execution_adapter": "dry_run", "task_type": "phase_1a_test"},
    )
    leased = store.daemon_run_once("local_daemon:test:123", pid=123)
    assert leased.lease is not None
    with store.connect() as conn:
        conn.execute(
            "UPDATE tasks SET required_approvals_json = ?, approval_state = ? WHERE id = ?",
            (json.dumps(["human_operator"]), "required", task.id),
        )

    inspected = runner.invoke(
        app,
        ["daemon", "inspect-lease", leased.lease.id, "--project", str(tmp_path), "--output", "json"],
    )
    assert inspected.exit_code == 0, inspected.output
    inspected_payload = json.loads(inspected.output)
    assert inspected_payload["security_decision"]["decision"] == "approval_required"
    assert inspected_payload["security_decision"]["sandbox_profile_id"] == "none"
    assert inspected_payload["security_decision"]["missing_approvals"] == ["human_operator"]
    assert inspected_payload["blocked_state_explanations"][0]["code"] == "missing_approval"

    result = runner.invoke(
        app,
        ["daemon", "execute", leased.lease.id, "--project", str(tmp_path), "--output", "json"],
    )

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["run"] is None
    assert payload["security_decision"]["decision"] == "approval_required"
    assert payload["security_decision"]["missing_approvals"] == ["human_operator"]
    assert payload["blocked_state_explanations"][0]["code"] == "missing_approval"
    assert store.list_runs() == []


def test_cli_tasks_add_accepts_repo_planning_execution_metadata(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0

    result = runner.invoke(
        app,
        [
            "tasks",
            "add",
            "--title",
            "Plan change",
            "--execution-adapter",
            "repo_planning",
            "--task-type",
            "repo_planning",
            "--project",
            str(tmp_path),
            "--output",
            "json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["task"]["metadata"] == {
        "execution_adapter": "repo_planning",
        "task_type": "repo_planning",
    }


def test_cli_daemon_execute_dispatches_repo_planning_through_registered_adapter(tmp_path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    ApprovalStore(tmp_path).add("codex_cli", "hosted_provider", ["repo_planning"], 1)

    class FakeBackend:
        def __init__(self, config):
            self.config = config
            self.name = config.name

        def preflight(self):
            return BackendStatus(
                available=True,
                metadata=self.config.metadata,
                capabilities=self.config.capabilities.model_copy(
                    update={
                        "supports_read_only_sandbox": True,
                        "supports_cd": True,
                        "supports_model_arg": True,
                        "supports_output_last_message": True,
                    }
                ),
            )

        def run_read_only(self, project_root, prompt, final_message_path):
            final_message_path.write_text("Plan only.", encoding="utf-8")
            return CodexRunResult(
                command=["codex", "exec", "--cd", str(project_root), "--sandbox", "read-only"],
                stdout="",
                stderr="",
                exit_status=0,
                json_events=[],
                final_message="Plan only.",
            )

    monkeypatch.setattr("harness.execution.CodexCliBackend", FakeBackend)
    task_result = runner.invoke(
        app,
        [
            "tasks",
            "add",
            "--title",
            "Plan change",
            "--execution-adapter",
            "repo_planning",
            "--task-type",
            "repo_planning",
            "--project",
            str(tmp_path),
            "--output",
            "json",
        ],
    )
    assert task_result.exit_code == 0, task_result.output
    tick = runner.invoke(app, ["daemon", "run-once", "--project", str(tmp_path), "--output", "json"])
    assert tick.exit_code == 0, tick.output
    lease_id = json.loads(tick.output)["lease"]["id"]

    result = runner.invoke(
        app,
        ["daemon", "execute", lease_id, "--project", str(tmp_path), "--output", "json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema_version"] == "harness.daemon_execute/v1"
    assert payload["ok"] is True
    assert payload["adapter_id"] == "repo_planning"
    assert payload["decision"] == "repo_planning_completed"
    assert payload["task"]["status"] == "succeeded"
    assert payload["attempt"]["run_id"] == payload["run"]["id"]
    assert payload["run"]["task_type"] == "repo_planning"
    assert payload["manifest"]["schema_version"] == "harness.manifest/v1.1"


def test_chat_task_draft_confirm_and_decline_flows(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    state = ChatSessionState()

    draft = handle_chat_input("create dry run task", tmp_path, state)
    assert draft["kind"] == "task_draft"
    assert draft["draft"]["execution_adapter"] == "dry_run"
    assert SQLiteStore(tmp_path).list_tasks() == []

    created = handle_chat_input("yes", tmp_path, state)
    assert created["kind"] == "task_created"
    assert len(SQLiteStore(tmp_path).list_tasks()) == 1
    assert SQLiteStore(tmp_path).list_tasks()[0].metadata == {
        "execution_adapter": "dry_run",
        "task_type": "phase_1a_test",
    }

    read_only_draft = handle_chat_input("read only summary", tmp_path, state)
    assert read_only_draft["draft"]["execution_adapter"] == "read_only_summary"
    declined = handle_chat_input("no", tmp_path, state)
    assert declined["kind"] == "declined"
    assert len(SQLiteStore(tmp_path).list_tasks()) == 1

    codex_draft = handle_chat_input("fix this bug with Codex", tmp_path, state)
    assert codex_draft["kind"] == "orchestration_draft"
    assert [task["execution_adapter"] for task in codex_draft["draft"]["tasks"]] == [
        "repo_planning",
        "codex_isolated_edit",
        "dry_run",
        "dry_run",
        "dry_run",
        "dry_run",
    ]
    assert codex_draft["draft"]["tasks"][3]["agent_id"] == "implementation_reviewer"
    assert codex_draft["draft"]["tasks"][4]["agent_id"] == "security_reviewer"
    assert "Apply-back is not automatic and remains denied by default." in codex_draft["draft"]["safety_notes"]


def test_chat_dry_run_end_to_end_dispatch_flow(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    state = ChatSessionState()

    handle_chat_input("create dry run task", tmp_path, state)
    created = handle_chat_input("/confirm", tmp_path, state)
    leased = handle_chat_input("lease the next task", tmp_path, state)
    inspected = handle_chat_input("inspect the lease", tmp_path, state)
    prepared = handle_chat_input("run the registered adapter", tmp_path, state)
    executed = handle_chat_input("yes", tmp_path, state)

    assert created["kind"] == "task_created"
    assert leased["kind"] == "lease_acquired"
    assert inspected["kind"] == "lease_inspection"
    assert prepared["kind"] == "execute_confirmation_required"
    assert executed["kind"] == "execute_result"
    assert executed["ok"] is True
    assert executed["result"]["decision"] == "dry_run_no_tool_execution"
    assert executed["result"]["task"]["status"] == "succeeded"
    assert executed["result"]["lease"]["status"] == "released"
    assert state.latest_run_id == executed["result"]["run"]["id"]
    assert any(line.startswith("adapter decision") for line in state.progress)


def test_chat_codex_like_dry_run_confirmation_runs_foreground(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    state = ChatSessionState(codex_like_mode=True)

    draft = handle_chat_input("create dry run task", tmp_path, state)
    executed = handle_chat_input("yes", tmp_path, state)

    assert draft["kind"] == "task_draft"
    assert "create this task and run it" in "\n".join(draft["lines"])
    assert executed["kind"] == "codex_like_task_result"
    assert executed["ok"] is True
    assert executed["execution"]["decision"] == "dry_run_no_tool_execution"
    store = SQLiteStore(tmp_path)
    assert len(store.list_tasks()) == 1
    assert len(store.list_runs()) == 1


def test_chat_codex_like_read_only_missing_approval_prompts_in_app_recovery(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    state = ChatSessionState(codex_like_mode=True)

    draft = handle_chat_input("summarize this repo", tmp_path, state)
    rejected = handle_chat_input("yes", tmp_path, state)
    pending_approval = state.pending_hosted_approval
    approval = handle_chat_input("yes", tmp_path, state)

    rendered = "\n".join(rejected["lines"])
    assert draft["kind"] == "task_draft"
    assert draft["draft"]["execution_adapter"] == "read_only_summary"
    assert rejected["kind"] == "codex_like_task_result"
    assert rejected["ok"] is False
    assert rejected["execution"]["decision"] == "execution_adapter_rejected"
    assert rejected["execution"]["run"] is None
    assert pending_approval is True
    assert "Hosted-boundary approval is required before Codex run creation." in rendered
    assert approval["kind"] == "hosted_approval_created"
    assert ApprovalStore(tmp_path).find_valid("codex_cli", "hosted_provider", "read_only_repo_summary") is not None
    assert ApprovalStore(tmp_path).find_valid("codex_cli", "hosted_provider", "codex_code_edit") is not None
    assert SQLiteStore(tmp_path).list_runs() == []


def test_chat_unknown_adapter_fail_closed_without_run(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    store.create_task(title="Unknown", metadata={"execution_adapter": "unknown_adapter", "task_type": "phase_1a_test"})
    leased = store.select_next_task_for_lease(owner="chat-test")
    assert leased is not None
    state = ChatSessionState(latest_lease_id=leased["lease"].id)

    prepared = handle_chat_input("run the registered adapter", tmp_path, state)

    assert prepared["kind"] == "execute_ineligible"
    assert prepared["ok"] is False
    assert store.list_runs() == []


def test_chat_duplicate_execute_is_rejected(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    state = ChatSessionState()
    handle_chat_input("create dry run task", tmp_path, state)
    handle_chat_input("yes", tmp_path, state)
    handle_chat_input("lease the next task", tmp_path, state)
    handle_chat_input("run the registered adapter", tmp_path, state)
    first = handle_chat_input("yes", tmp_path, state)
    assert first["ok"] is True

    state.pending_execute_lease_id = state.latest_lease_id
    duplicate = handle_chat_input("yes", tmp_path, state)

    assert duplicate["ok"] is False
    assert duplicate["result"]["decision"] == "execution_adapter_rejected"
    assert duplicate["result"]["run"] is None
    assert len(SQLiteStore(tmp_path).list_runs()) == 1


def test_chat_codex_missing_hosted_approval_rejects_before_run(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-secret")
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    state = ChatSessionState()

    draft = handle_chat_input("fix this bug with Codex", tmp_path, state)
    rejected = handle_chat_input("yes", tmp_path, state)

    rendered = "\n".join(rejected["lines"])
    assert draft["kind"] == "orchestration_draft"
    assert draft["draft"]["orchestrator_id"] == "coding_orchestrator"
    assert [task["execution_adapter"] for task in draft["draft"]["tasks"]] == [
        "repo_planning",
        "codex_isolated_edit",
        "dry_run",
        "dry_run",
        "dry_run",
        "dry_run",
    ]
    assert draft["draft"]["tasks"][3]["agent_id"] == "implementation_reviewer"
    assert draft["draft"]["tasks"][4]["agent_id"] == "security_reviewer"
    assert rejected["ok"] is False
    assert rejected["kind"] == "orchestration_result"
    assert rejected["orchestration"]["results"][0]["decision"] == "execution_adapter_rejected"
    assert rejected["orchestration"]["results"][0]["security_decision"]["decision"] == "approval_required"
    assert rejected["orchestration"]["results"][0]["security_decision"]["missing_approvals"] == [
        "hosted_provider_codex"
    ]
    assert rejected["orchestration"]["results"][0]["run"] is None
    assert state.pending_hosted_approval is True
    assert "Hosted-boundary approval is not apply-back approval." in rendered
    assert "sk-test-secret" not in json.dumps(rejected)
    assert SQLiteStore(tmp_path).list_runs() == []


def test_chat_orchestrator_slash_commands_are_session_local(tmp_path) -> None:
    state = ChatSessionState()

    listed = handle_chat_input("/orchestrators", tmp_path, state)
    selected = handle_chat_input("/use quant_orchestrator", tmp_path, state)
    agents = handle_chat_input("/agents", tmp_path, state)
    dashboard = handle_chat_input("/dashboard", tmp_path, state)

    assert listed["kind"] == "orchestrators"
    assert {"coding_orchestrator", "personal_orchestrator", "quant_orchestrator"} <= {
        item["id"] for item in listed["orchestrators"]
    }
    assert selected["ok"] is True
    assert state.selected_orchestrator_id == "quant_orchestrator"
    assert agents["workbench_id"] == "quant"
    assert dashboard["kind"] == "status"
    assert not (tmp_path / ".harness").exists()


def test_chat_orchestration_decline_creates_no_graph(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    state = ChatSessionState()

    draft = handle_chat_input("fix the failing test with Codex", tmp_path, state)
    declined = handle_chat_input("no", tmp_path, state)

    assert draft["kind"] == "orchestration_draft"
    assert declined["kind"] == "declined"
    store = SQLiteStore(tmp_path)
    assert store.list_objectives() == []
    assert store.list_tasks() == []


def test_chat_session_reset_clears_references_without_history_file(tmp_path) -> None:
    state = ChatSessionState(latest_task_id="task_demo", latest_lease_id="lease_demo", latest_run_id="run_demo")
    state.transcript.append({"role": "user", "content": "show tasks"})
    state.progress.append("task created: task_demo")

    reset = handle_chat_input("/reset", tmp_path, state)

    assert reset["kind"] == "reset"
    assert state.latest_task_id is None
    assert state.latest_lease_id is None
    assert state.latest_run_id is None
    assert state.transcript == []
    assert state.progress == []
    assert not (tmp_path / ".harness").exists()


def test_cli_daemon_execute_read_only_links_run_and_releases_lease(tmp_path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    (tmp_path / "README.md").write_text("# Demo\n", encoding="utf-8")
    ApprovalStore(tmp_path).add("codex_cli", "hosted_provider", ["read_only_repo_summary"], 1)

    class FakeBackend:
        def __init__(self, config):
            self.config = config
            self.name = config.name

        def preflight(self):
            return BackendStatus(
                available=True,
                metadata=self.config.metadata,
                capabilities=self.config.capabilities.model_copy(
                    update={
                        "supports_read_only_sandbox": True,
                        "supports_cd": True,
                        "supports_model_arg": True,
                        "supports_output_last_message": True,
                    }
                ),
            )

        def run_read_only(self, project_root, prompt, final_message_path):
            final_message_path.write_text("Read-only lease summary.", encoding="utf-8")
            return CodexRunResult(
                command=["codex", "exec", "--cd", str(project_root), "--model", "gpt-5.5", "-c", 'model_reasoning_effort="low"', "--sandbox", "read-only", prompt],
                stdout="",
                stderr="",
                exit_status=0,
                json_events=[],
                final_message="Read-only lease summary.",
            )

    monkeypatch.setattr("harness.daemon_adapters.CodexCliBackend", FakeBackend)
    monkeypatch.setattr(
        "harness.cli.main.LocalOpenAICompatibleBackend",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("read-only adapter must not preflight local backend")),
    )
    monkeypatch.setattr(
        "harness.cli.main.DockerImageManager",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("read-only adapter must not touch Docker")),
    )

    task_result = runner.invoke(
        app,
        [
            "tasks",
            "add",
            "--title",
            "Summarize repo",
            "--execution-adapter",
            "read_only_summary",
            "--task-type",
            "read_only_repo_summary",
            "--project",
            str(tmp_path),
            "--output",
            "json",
        ],
    )
    assert task_result.exit_code == 0, task_result.output
    task_payload = json.loads(task_result.output)
    assert task_payload["task"]["metadata"] == {
        "execution_adapter": "read_only_summary",
        "task_type": "read_only_repo_summary",
    }

    tick = runner.invoke(app, ["daemon", "run-once", "--project", str(tmp_path), "--output", "json"])
    assert tick.exit_code == 0, tick.output
    tick_payload = json.loads(tick.output)
    assert tick_payload["decision"] == "leased_task"
    assert tick_payload["attempt"]["run_id"] is None
    lease_id = tick_payload["lease"]["id"]

    before = runner.invoke(
        app,
        ["daemon", "inspect-lease", lease_id, "--project", str(tmp_path), "--output", "json"],
    )
    assert before.exit_code == 0, before.output
    before_payload = json.loads(before.output)
    assert before_payload["read_only_eligibility"]["eligible"] is True
    assert before_payload["run"] is None

    executed = runner.invoke(
        app,
        ["daemon", "execute-read-only", lease_id, "--project", str(tmp_path), "--output", "json"],
    )
    duplicate = runner.invoke(
        app,
        ["daemon", "execute-read-only", lease_id, "--project", str(tmp_path), "--output", "json"],
    )

    assert executed.exit_code == 0, executed.output
    payload = json.loads(executed.output)
    assert payload["schema_version"] == "harness.daemon_execute_read_only/v1"
    assert payload["ok"] is True
    assert payload["decision"] == "read_only_summary_completed"
    assert payload["task"]["status"] == "succeeded"
    assert payload["attempt"]["status"] == "succeeded"
    assert payload["attempt"]["run_id"] == payload["run"]["id"]
    assert payload["lease"]["status"] == "released"
    assert payload["run"]["status"] == "completed"
    assert payload["run"]["task_type"] == "read_only_repo_summary"
    assert payload["manifest"]["schema_version"] == "harness.manifest/v1.1"
    assert payload["manifest"]["task_id"] == payload["task"]["id"]
    after = runner.invoke(
        app,
        ["daemon", "inspect-lease", lease_id, "--project", str(tmp_path), "--output", "json"],
    )
    assert after.exit_code == 0, after.output
    after_payload = json.loads(after.output)
    assert after_payload["read_only_eligibility"]["eligible"] is False
    assert after_payload["run"]["id"] == payload["run"]["id"]
    assert after_payload["manifest"]["run_id"] == payload["run"]["id"]

    assert duplicate.exit_code == 1
    duplicate_payload = json.loads(duplicate.output)
    assert duplicate_payload["schema_version"] == "harness.daemon_execute_read_only/v1"
    assert duplicate_payload["ok"] is False
    assert duplicate_payload["errors"] == ["Read-only execution requires active lease: released"]
    assert len(SQLiteStore(tmp_path).list_runs()) == 1

    serialized = task_result.output + tick.output + before.output + executed.output + after.output + duplicate.output
    assert "api_key" not in serialized
    assert "OPENAI_API_KEY" not in serialized
    assert "base_url" not in serialized
    assert "http://localhost:11434" not in serialized


def test_cli_daemon_execute_read_only_preflight_failure_leaves_lease_unchanged(tmp_path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    ApprovalStore(tmp_path).add("codex_cli", "hosted_provider", ["read_only_repo_summary"], 1)

    class UnavailableBackend:
        def __init__(self, config):
            self.config = config
            self.name = config.name

        def preflight(self):
            return BackendStatus(
                available=False,
                reason="codex backend unavailable for test",
                metadata=self.config.metadata,
                capabilities=self.config.capabilities,
            )

    monkeypatch.setattr("harness.daemon_adapters.CodexCliBackend", UnavailableBackend)
    task_result = runner.invoke(
        app,
        [
            "tasks",
            "add",
            "--title",
            "Read-only preflight",
            "--execution-adapter",
            "read_only_summary",
            "--task-type",
            "read_only_repo_summary",
            "--project",
            str(tmp_path),
            "--output",
            "json",
        ],
    )
    assert task_result.exit_code == 0, task_result.output
    task_id = json.loads(task_result.output)["task"]["id"]
    tick = runner.invoke(app, ["daemon", "run-once", "--project", str(tmp_path), "--output", "json"])
    lease_id = json.loads(tick.output)["lease"]["id"]
    attempt_id = json.loads(tick.output)["attempt"]["id"]

    executed = runner.invoke(
        app,
        ["daemon", "execute-read-only", lease_id, "--project", str(tmp_path), "--output", "json"],
    )

    assert executed.exit_code == 1
    payload = json.loads(executed.output)
    assert payload["schema_version"] == "harness.daemon_execute_read_only/v1"
    assert payload["errors"] == ["codex backend unavailable for test"]
    store = SQLiteStore(tmp_path)
    assert store.list_runs() == []
    assert store.get_task(task_id).status.value == "leased"
    assert store.get_task_attempt(attempt_id).status.value == "leased"
    assert store.get_task_attempt(attempt_id).run_id is None
    assert store.get_task_lease(lease_id).status.value == "active"


def test_cli_daemon_execute_read_only_missing_hosted_approval_leaves_no_run(tmp_path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    monkeypatch.setattr(
        "harness.daemon_adapters.CodexCliBackend",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("missing approval must not preflight Codex")),
    )
    task_result = runner.invoke(
        app,
        [
            "tasks",
            "add",
            "--title",
            "Read-only missing approval",
            "--execution-adapter",
            "read_only_summary",
            "--task-type",
            "read_only_repo_summary",
            "--project",
            str(tmp_path),
            "--output",
            "json",
        ],
    )
    task_id = json.loads(task_result.output)["task"]["id"]
    tick = runner.invoke(app, ["daemon", "run-once", "--project", str(tmp_path), "--output", "json"])
    lease_id = json.loads(tick.output)["lease"]["id"]
    attempt_id = json.loads(tick.output)["attempt"]["id"]

    executed = runner.invoke(
        app,
        ["daemon", "execute-read-only", lease_id, "--project", str(tmp_path), "--output", "json"],
    )

    assert executed.exit_code == 1
    payload = json.loads(executed.output)
    assert payload["schema_version"] == "harness.daemon_execute_read_only/v1"
    assert payload["errors"] == ["Missing valid hosted-provider Codex approval for read_only_repo_summary."]
    store = SQLiteStore(tmp_path)
    assert store.list_runs() == []
    assert store.get_task(task_id).status.value == "leased"
    assert store.get_task_attempt(attempt_id).status.value == "leased"
    assert store.get_task_attempt(attempt_id).run_id is None
    assert store.get_task_lease(lease_id).status.value == "active"


def test_cli_daemon_execute_read_only_runner_failure_marks_terminal_without_duplicate_run(
    tmp_path,
    monkeypatch,
) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    ApprovalStore(tmp_path).add("codex_cli", "hosted_provider", ["read_only_repo_summary"], 1)

    class FailingBackend:
        def __init__(self, config):
            self.config = config
            self.name = config.name

        def preflight(self):
            return BackendStatus(
                available=True,
                metadata=self.config.metadata,
                capabilities=self.config.capabilities.model_copy(update={"supports_read_only_sandbox": True}),
            )

        def run_read_only(self, project_root, prompt, final_message_path):
            raise RuntimeError("codex read-only failed in test")

    monkeypatch.setattr("harness.daemon_adapters.CodexCliBackend", FailingBackend)
    task_result = runner.invoke(
        app,
        [
            "tasks",
            "add",
            "--title",
            "Read-only runner failure",
            "--execution-adapter",
            "read_only_summary",
            "--task-type",
            "read_only_repo_summary",
            "--project",
            str(tmp_path),
            "--output",
            "json",
        ],
    )
    task_id = json.loads(task_result.output)["task"]["id"]
    tick = runner.invoke(app, ["daemon", "run-once", "--project", str(tmp_path), "--output", "json"])
    lease_id = json.loads(tick.output)["lease"]["id"]
    attempt_id = json.loads(tick.output)["attempt"]["id"]

    executed = runner.invoke(
        app,
        ["daemon", "execute-read-only", lease_id, "--project", str(tmp_path), "--output", "json"],
    )
    duplicate = runner.invoke(
        app,
        ["daemon", "execute-read-only", lease_id, "--project", str(tmp_path), "--output", "json"],
    )

    assert executed.exit_code == 1
    payload = json.loads(executed.output)
    assert payload["schema_version"] == "harness.daemon_execute_read_only/v1"
    assert payload["errors"] == ["codex read-only failed in test"]
    store = SQLiteStore(tmp_path)
    runs = store.list_runs()
    assert len(runs) == 1
    assert runs[0].status == "failed"
    assert store.get_task(task_id).status.value == "failed"
    attempt = store.get_task_attempt(attempt_id)
    assert attempt.status.value == "failed"
    assert attempt.run_id == runs[0].id
    assert attempt.failure_code == "read_only_execution_failed"
    assert store.get_task_lease(lease_id).status.value == "released"
    assert duplicate.exit_code == 1
    assert len(store.list_runs()) == 1


def test_cli_daemon_execute_read_only_rejects_unsafe_backend_descriptor(tmp_path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    ApprovalStore(tmp_path).add("codex_cli", "hosted_provider", ["read_only_repo_summary"], 1)
    cfg = default_config()
    cfg.backends["codex_cli"].metadata.billing_mode = BillingMode.PAID_API
    monkeypatch.setattr("harness.daemon_adapters.load_config", lambda _project_root: cfg)
    monkeypatch.setattr(
        "harness.daemon_adapters.CodexCliBackend",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("unsafe backend must not instantiate")),
    )
    task_result = runner.invoke(
        app,
        [
            "tasks",
            "add",
            "--title",
            "Unsafe backend",
            "--execution-adapter",
            "read_only_summary",
            "--task-type",
            "read_only_repo_summary",
            "--project",
            str(tmp_path),
            "--output",
            "json",
        ],
    )
    tick = runner.invoke(app, ["daemon", "run-once", "--project", str(tmp_path), "--output", "json"])
    lease_id = json.loads(tick.output)["lease"]["id"]

    executed = runner.invoke(
        app,
        ["daemon", "execute-read-only", lease_id, "--project", str(tmp_path), "--output", "json"],
    )

    assert task_result.exit_code == 0
    assert executed.exit_code == 1
    payload = json.loads(executed.output)
    assert payload["errors"] == ["Read-only execution requires subscription codex_cli backend"]
    assert SQLiteStore(tmp_path).list_runs() == []


def test_cli_daemon_inspect_lease_before_and_after_dry_run_is_read_only(tmp_path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    monkeypatch.setattr(
        "harness.cli.main.CodexCliBackend",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("inspect must not preflight Codex")),
    )
    monkeypatch.setattr(
        "harness.cli.main.LocalOpenAICompatibleBackend",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("inspect must not preflight local backend")),
    )
    monkeypatch.setattr(
        "harness.cli.main.DockerImageManager",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("inspect must not touch Docker")),
    )
    store = SQLiteStore(tmp_path)
    store.create_task(
        title="Dry run",
        metadata={"execution_adapter": "dry_run", "task_type": "phase_1a_test"},
    )
    tick = runner.invoke(app, ["daemon", "run-once", "--project", str(tmp_path), "--output", "json"])
    lease_id = json.loads(tick.output)["lease"]["id"]

    before = runner.invoke(app, ["daemon", "inspect-lease", lease_id, "--project", str(tmp_path), "--output", "json"])
    assert before.exit_code == 0, before.output
    before_payload = json.loads(before.output)
    assert before_payload["schema_version"] == "harness.daemon_lease/v1"
    assert before_payload["dry_run_eligibility"]["eligible"] is True
    assert before_payload["security_decision"]["decision"] == "allow"
    assert before_payload["security_decision"]["adapter_id"] == "dry_run"
    assert before_payload["security_decision"]["sandbox_profile_id"] == "none"
    assert before_payload["run"] is None
    assert len(store.list_runs()) == 0

    executed = runner.invoke(
        app,
        ["daemon", "execute-dry-run", lease_id, "--project", str(tmp_path), "--output", "json"],
    )
    assert executed.exit_code == 0, executed.output
    after = runner.invoke(app, ["daemon", "inspect-lease", lease_id, "--project", str(tmp_path), "--output", "json"])
    assert after.exit_code == 0, after.output
    after_payload = json.loads(after.output)
    assert after_payload["schema_version"] == "harness.daemon_lease/v1"
    assert after_payload["lease"]["status"] == "released"
    assert after_payload["task"]["status"] == "succeeded"
    assert after_payload["attempt"]["status"] == "succeeded"
    assert after_payload["run"]["id"] == json.loads(executed.output)["run"]["id"]
    assert after_payload["manifest"]["schema_version"] == "harness.manifest/v1.1"
    assert after_payload["manifest"]["sandbox_profile"]["id"] == "none"
    assert after_payload["dry_run_eligibility"]["eligible"] is False
    assert after_payload["security_decision"]["decision"] == "deny"

    missing = runner.invoke(
        app,
        ["daemon", "inspect-lease", "missing_lease", "--project", str(tmp_path), "--output", "json"],
    )
    assert missing.exit_code == 1
    missing_payload = json.loads(missing.output)
    assert missing_payload["schema_version"] == "harness.daemon_lease/v1"
    assert missing_payload["ok"] is False
    assert missing_payload["errors"] == ["Task lease not found: missing_lease"]

    serialized = before.output + executed.output + after.output + missing.output
    assert "api_key" not in serialized
    assert "OPENAI_API_KEY" not in serialized
    assert "base_url" not in serialized
    assert "environment" not in serialized


def test_cli_daemon_recover_reports_dry_run_reconciliation_without_backends(tmp_path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    monkeypatch.setattr(
        "harness.cli.main.CodexCliBackend",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("recover must not preflight Codex")),
    )
    monkeypatch.setattr(
        "harness.cli.main.LocalOpenAICompatibleBackend",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("recover must not preflight local backend")),
    )
    monkeypatch.setattr(
        "harness.cli.main.DockerImageManager",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("recover must not touch Docker")),
    )
    store = SQLiteStore(tmp_path)
    store.create_task(
        title="Dry run",
        metadata={"execution_adapter": "dry_run", "task_type": "phase_1a_test"},
    )
    leased = store.daemon_run_once("local_daemon:test:123", pid=123)
    assert leased.lease is not None
    executed = store.execute_dry_run_lease(leased.lease.id, owner="local_daemon:test:123")
    with store.connect() as conn:
        conn.execute("UPDATE tasks SET status = ? WHERE id = ?", ("running", executed.task.id))
        conn.execute("UPDATE task_attempts SET status = ? WHERE id = ?", ("running", executed.attempt.id))
        conn.execute(
            "UPDATE task_leases SET status = ?, released_at = NULL WHERE id = ?",
            ("active", executed.lease.id),
        )

    recovered = runner.invoke(app, ["daemon", "recover", "--project", str(tmp_path), "--output", "json"])

    assert recovered.exit_code == 0, recovered.output
    payload = json.loads(recovered.output)
    assert payload["schema_version"] == "harness.daemon_recovery/v1"
    assert payload["events"][0]["event_type"] == "recover_dry_run"
    assert payload["recovered_tasks"][0]["status"] == "succeeded"
    assert len(store.list_runs()) == 1
    assert store.get_task(executed.task.id).status.value == "succeeded"

    serialized = recovered.output
    assert "api_key" not in serialized
    assert "OPENAI_API_KEY" not in serialized
    assert "base_url" not in serialized
    assert "environment" not in serialized


def test_cli_tasks_add_rejects_unsupported_execution_adapter_metadata(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0

    result = runner.invoke(
        app,
        [
            "tasks",
            "add",
            "--title",
            "Bad adapter",
            "--execution-adapter",
            "codex",
            "--task-type",
            "phase_1a_test",
            "--project",
            str(tmp_path),
            "--output",
            "json",
        ],
    )

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["schema_version"] == "harness.task/v1"
    assert payload["ok"] is False
    assert payload["errors"] == [
        "Unsupported execution metadata: supported pairs are "
        "dry_run/phase_1a_test, read_only_summary/read_only_repo_summary, "
        "codex_isolated_edit/codex_code_edit, repo_planning/repo_planning, "
        "session_read_tools/session_plan, session_read_tools/session_read_only_research, "
        "and session_read_tools/session_operator"
    ]


@pytest.mark.parametrize(("execution_adapter", "task_type"), SUPPORTED_EXECUTION_TASK_METADATA)
def test_cli_tasks_add_accepts_supported_execution_adapter_metadata(
    tmp_path,
    execution_adapter: str,
    task_type: str,
) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0

    result = runner.invoke(
        app,
        [
            "tasks",
            "add",
            "--title",
            f"Supported {execution_adapter} {task_type}",
            "--execution-adapter",
            execution_adapter,
            "--task-type",
            task_type,
            "--project",
            str(tmp_path),
            "--output",
            "json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["task"]["metadata"]["execution_adapter"] == execution_adapter
    assert payload["task"]["metadata"]["task_type"] == task_type


def test_cli_tasks_reject_invalid_builtin_registry_refs(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0

    invalid_agent = runner.invoke(
        app,
        [
            "tasks",
            "add",
            "--title",
            "Bad agent",
            "--agent",
            "missing_agent",
            "--project",
            str(tmp_path),
            "--output",
            "json",
        ],
    )
    invalid_workbench = runner.invoke(
        app,
        [
            "tasks",
            "add",
            "--title",
            "Bad workbench",
            "--workbench",
            "missing_workbench",
            "--project",
            str(tmp_path),
            "--output",
            "json",
        ],
    )

    assert invalid_agent.exit_code != 0
    assert json.loads(invalid_agent.output)["errors"] == ["Agent not found: missing_agent"]
    assert invalid_workbench.exit_code != 0
    assert json.loads(invalid_workbench.output)["errors"] == ["Workbench not found: missing_workbench"]


def test_cli_tasks_do_not_preflight_backends_or_expose_settings(tmp_path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    monkeypatch.setattr(
        "harness.cli.main.CodexCliBackend",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("tasks must not preflight Codex")),
    )
    monkeypatch.setattr(
        "harness.cli.main.LocalOpenAICompatibleBackend",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("tasks must not preflight local backend")),
    )

    result = runner.invoke(
        app,
        ["tasks", "add", "--title", "Safe task", "--project", str(tmp_path), "--output", "json"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    serialized = json.dumps(payload)
    assert "api_key" not in serialized
    assert "OPENAI_API_KEY" not in serialized
    assert "base_url" not in serialized


def test_cli_doctor_reports_initialized_project_without_mutation(tmp_path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    before = sorted(path.relative_to(tmp_path).as_posix() for path in (tmp_path / ".harness").rglob("*"))

    class FakeCodexBackend:
        def __init__(self, config):
            self.config = config

        def preflight(self):
            return BackendStatus(
                available=True,
                metadata=self.config.metadata,
                capabilities=self.config.capabilities,
            )

    class FakeLocalBackend:
        def __init__(self, config):
            self.config = config

        def preflight(self):
            return BackendStatus(
                available=True,
                metadata=self.config.metadata,
                capabilities=self.config.capabilities,
            )

    monkeypatch.setattr("harness.cli.main.CodexCliBackend", FakeCodexBackend)
    monkeypatch.setattr("harness.cli.main.LocalOpenAICompatibleBackend", FakeLocalBackend)
    monkeypatch.setattr("harness.cli.main.shutil.which", lambda name: "/usr/bin/docker" if name == "docker" else None)

    class FakeDockerVersion:
        returncode = 0
        stdout = "Docker version test\n"
        stderr = ""

    monkeypatch.setattr("harness.cli.main.subprocess.run", lambda *args, **kwargs: FakeDockerVersion())

    result = runner.invoke(app, ["doctor", "--project", str(tmp_path)])
    after = sorted(path.relative_to(tmp_path).as_posix() for path in (tmp_path / ".harness").rglob("*"))

    assert result.exit_code == 0
    assert not result.output.lstrip().startswith("{")
    assert "Overall: pass" in result.output
    assert "pass\tinitialized" in result.output
    assert "pass\tconfig_loadable" in result.output
    assert before == after


def test_cli_doctor_uninitialized_project_fails_without_creating_harness_dir(tmp_path) -> None:
    result = runner.invoke(app, ["doctor", "--project", str(tmp_path)])

    assert result.exit_code == 1
    assert "fail\tinitialized" in result.output
    assert "fail\tconfig_loadable" in result.output
    assert not (tmp_path / ".harness").exists()


def test_cli_doctor_supports_json_output_without_sensitive_backend_settings(tmp_path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0

    class FakeCodexBackend:
        def __init__(self, config):
            self.config = config

        def preflight(self):
            return BackendStatus(
                available=True,
                metadata=self.config.metadata,
                capabilities=self.config.capabilities.model_copy(update={"supports_exec": True}),
            )

    class FakeLocalBackend:
        def __init__(self, config):
            self.config = config

        def preflight(self):
            return BackendStatus(
                available=False,
                reason="local unavailable",
                metadata=self.config.metadata,
                capabilities=self.config.capabilities,
            )

    monkeypatch.setattr("harness.cli.main.CodexCliBackend", FakeCodexBackend)
    monkeypatch.setattr("harness.cli.main.LocalOpenAICompatibleBackend", FakeLocalBackend)
    monkeypatch.setattr("harness.cli.main.shutil.which", lambda name: None)

    result = runner.invoke(app, ["doctor", "--project", str(tmp_path), "--output", "json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["schema_version"] == "harness.doctor/v1"
    assert payload["project_root"] == str(tmp_path.resolve())
    assert payload["ok"] is True
    checks = {check["id"]: check for check in payload["checks"]}
    assert checks["initialized"]["status"] == "pass"
    assert checks["config_loadable"]["status"] == "pass"
    assert checks["sandbox_safety"]["status"] == "pass"
    assert checks["docker_binary"]["status"] == "warn"
    paid_backend = next(
        backend
        for backend in checks["backend_preflight"]["details"]["backends"]
        if backend["name"] == "paid_openai_compatible"
    )
    assert paid_backend["status"] == "warn"
    assert paid_backend["reason"] == "Paid backend preflight skipped; disabled by default."
    serialized = json.dumps(payload)
    assert '"settings"' not in serialized
    assert "base_url" not in serialized
    assert "api_key" not in serialized
    assert "api_key_env" not in serialized


def test_cli_doctor_reports_unwritable_artifact_directory(tmp_path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0

    class FakeCodexBackend:
        def __init__(self, config):
            self.config = config

        def preflight(self):
            return BackendStatus(
                available=True,
                metadata=self.config.metadata,
                capabilities=self.config.capabilities,
            )

    class FakeLocalBackend:
        def __init__(self, config):
            self.config = config

        def preflight(self):
            return BackendStatus(
                available=True,
                metadata=self.config.metadata,
                capabilities=self.config.capabilities,
            )

    monkeypatch.setattr("harness.cli.main.CodexCliBackend", FakeCodexBackend)
    monkeypatch.setattr("harness.cli.main.LocalOpenAICompatibleBackend", FakeLocalBackend)
    monkeypatch.setattr("harness.cli.main.shutil.which", lambda name: None)
    runs_dir = tmp_path / ".harness" / "runs"
    runs_dir.chmod(0o500)
    try:
        result = runner.invoke(app, ["doctor", "--project", str(tmp_path), "--output", "json"])
    finally:
        runs_dir.chmod(0o700)

    assert result.exit_code == 1
    payload = json.loads(result.output)
    check = next(item for item in payload["checks"] if item["id"] == "artifact_directory")
    assert check["status"] == "fail"
    assert check["message"] == "Harness artifact directory is not writable."
    assert check["details"]["issues"][0]["path"] == str(runs_dir)
    assert check["details"]["issues"][0]["error"] == "not_writable"
    assert "Traceback" not in result.output


def test_cli_doctor_release_reports_no_preflight_operator_checklist(tmp_path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    store.create_task(title="Waiting", required_approvals=["hosted_provider"])
    store.create_run(goal="failed", task_type="phase_1a_test", status="failed")

    def fail_preflight(*args, **kwargs):
        raise AssertionError("release diagnostics must not preflight backends")

    monkeypatch.setattr("harness.cli.main.CodexCliBackend", fail_preflight)
    monkeypatch.setattr("harness.cli.main.LocalOpenAICompatibleBackend", fail_preflight)
    monkeypatch.setattr("harness.cli.main.shutil.which", lambda name: f"/usr/bin/{name}")

    result = runner.invoke(app, ["doctor", "--release", "--project", str(tmp_path), "--output", "json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema_version"] == "harness.doctor/v1"
    assert payload["mode"] == "release"
    assert payload["version"] == "1.8.0"
    checks = {check["id"]: check for check in payload["checks"]}
    assert checks["codex_cli_metadata"]["details"]["preflight_performed"] is False
    assert checks["docker_metadata"]["details"]["version_check_performed"] is False
    assert {adapter["id"] for adapter in checks["registered_adapters"]["details"]["adapters"]} >= {
        "dry_run",
        "read_only_summary",
        "codex_isolated_edit",
        "repo_planning",
    }
    runtime = checks["release_runtime_state"]["details"]
    assert runtime["blocked_or_waiting_tasks"][0]["title"] == "Waiting"
    assert runtime["latest_failed_run"]["status"] == "failed"
    serialized = json.dumps(payload)
    assert "api_key" not in serialized
    assert "OPENAI_API_KEY" not in serialized
    assert "base_url" not in serialized


def test_cli_doctor_release_uninitialized_is_non_mutating_warning(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("harness.cli.main.shutil.which", lambda name: None)

    result = runner.invoke(app, ["doctor", "--release", "--project", str(tmp_path), "--output", "json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["mode"] == "release"
    checks = {check["id"]: check for check in payload["checks"]}
    assert checks["initialized"]["status"] == "warn"
    assert checks["config_loadable"]["status"] == "warn"
    assert checks["release_runtime_state"]["details"]["initialized"] is False
    assert not (tmp_path / ".harness").exists()


def test_cli_policy_explain_supports_runtime_subjects_without_preflight_or_settings(tmp_path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    run = store.create_run(goal="policy run", task_type="phase_1a_test")
    task = store.create_task(
        title="Policy task",
        required_approvals=["hosted_provider"],
        agent_id="repo_inspector",
        workbench_id="coding",
    )

    monkeypatch.setattr(
        "harness.cli.main.CodexCliBackend",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("policy explain must not preflight Codex")),
    )
    monkeypatch.setattr(
        "harness.cli.main.LocalOpenAICompatibleBackend",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("policy explain must not preflight local backend")),
    )

    for kind, subject_id in [
        ("run", run.id),
        ("task", task.id),
        ("agent", "repo_inspector"),
        ("workbench", "coding"),
        ("backend", "local_openai_compatible"),
    ]:
        result = runner.invoke(
            app,
            [
                "policy",
                "explain",
                "--subject-kind",
                kind,
                "--subject-id",
                subject_id,
                "--project",
                str(tmp_path),
                "--output",
                "json",
            ],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["schema_version"] == "harness.effective_policy/v1"
        assert payload["ok"] is True
        assert payload["subject_kind"] == kind
        assert payload["subject_id"] == subject_id
        assert payload["policy_sha256"]
        serialized = json.dumps(payload)
        assert "api_key" not in serialized
        assert "OPENAI_API_KEY" not in serialized
        assert "base_url" not in serialized


def test_cli_policy_explain_unknown_subject_returns_stable_json_error(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0

    expected_errors = {
        "run": "Run not found: run_missing",
        "task": "Task not found: task_missing",
        "agent": "Agent not found: agent_missing",
        "workbench": "Workbench not found: workbench_missing",
        "backend": "Backend not found: backend_missing",
    }
    for kind, expected_error in expected_errors.items():
        result = runner.invoke(
            app,
            [
                "policy",
                "explain",
                "--subject-kind",
                kind,
                "--subject-id",
                f"{kind}_missing",
                "--project",
                str(tmp_path),
                "--output",
                "json",
            ],
        )

        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["schema_version"] == "harness.effective_policy/v1"
        assert payload["ok"] is False
        assert payload["errors"] == [expected_error]


def test_cli_specs_registry_supports_json_output_without_runtime_leaks(tmp_path) -> None:
    result = runner.invoke(app, ["specs", "--output", "json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["schema_version"] == "harness.spec_registry/v1"
    assert {"local_reasoning", "codex_supervised"} <= set(payload["model_profiles"])
    assert {"commodities_researcher.default", "risk_reviewer.default", "job_researcher.default"} <= set(
        payload["agent_profiles"]
    )
    quant_agents = {
        "quant_orchestrator",
        "quant_researcher",
        "commodities_researcher",
        "equities_researcher",
        "volatility_researcher",
        "data_engineer",
        "backtest_engineer",
        "low_level_optimizer",
        "risk_reviewer",
        "leakage_reviewer",
        "statistical_validity_reviewer",
    }
    quant_groups = {"quant_research", "quant_development", "trading_analysis", "review"}
    coding_reviewers = {"implementation_reviewer", "security_reviewer", "factuality_reviewer"}
    assert ({"repo_inspector", "code_editor", "test_runner", "job_researcher"} | coding_reviewers | quant_agents) <= set(
        payload["agents"]
    )
    assert {"coding", "quant", "personal"} <= set(payload["workbenches"])
    serialized = json.dumps(payload)
    assert "settings" not in serialized
    assert "base_url" not in serialized
    assert "api_key" not in serialized
    assert "api_key_env" not in serialized
    assert "OPENAI_API_KEY" not in serialized
    assert not (tmp_path / ".harness").exists()


def test_cli_specs_agent_supports_json_output() -> None:
    result = runner.invoke(app, ["specs", "agent", "repo_inspector", "--output", "json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["schema_version"] == "harness.agent_spec/v1"
    assert payload["agent"]["id"] == "repo_inspector"
    assert payload["agent"]["kind"] == "specialist"
    assert payload["agent"]["model_profile"] == "codex_supervised"
    assert payload["agent"]["tool_policy"] == "read_only"
    assert payload["agent"]["memory_scope"] == "project"


def test_cli_specs_workbench_supports_json_output() -> None:
    result = runner.invoke(app, ["specs", "workbench", "coding", "--output", "json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["schema_version"] == "harness.workbench_spec/v1"
    assert payload["workbench"]["id"] == "coding"
    assert payload["workbench"]["default_model_profile"] == "codex_supervised"
    assert {
        "coding_orchestrator",
        "repo_inspector",
        "code_editor",
        "test_runner",
        "implementation_reviewer",
        "security_reviewer",
        "factuality_reviewer",
    } <= set(payload["workbench"]["allowed_agents"])


def test_cli_specs_quant_workbench_exposes_v0_6_declarative_agents_without_runtime_leaks(tmp_path) -> None:
    quant_agents = {
        "quant_orchestrator",
        "quant_researcher",
        "commodities_researcher",
        "equities_researcher",
        "volatility_researcher",
        "data_engineer",
        "backtest_engineer",
        "low_level_optimizer",
        "risk_reviewer",
        "leakage_reviewer",
        "statistical_validity_reviewer",
    }
    quant_groups = {"quant_research", "quant_development", "trading_analysis", "review"}
    workbench_result = runner.invoke(app, ["specs", "workbench", "quant", "--output", "json"])

    assert workbench_result.exit_code == 0
    workbench_payload = json.loads(workbench_result.output)
    assert workbench_payload["schema_version"] == "harness.workbench_spec/v1"
    workbench = workbench_payload["workbench"]
    assert workbench["id"] == "quant"
    assert quant_agents <= set(workbench["allowed_agents"])
    assert {
        "live_trading",
        "broker_action",
        "capital_allocation",
        "order_placement",
        "paid_api_fallback",
        "hosted_fallback",
    } <= set(workbench["forbidden_actions"])

    for agent_id in sorted(quant_agents | quant_groups):
        agent_result = runner.invoke(app, ["specs", "agent", agent_id, "--output", "json"])
        assert agent_result.exit_code == 0
        agent_payload = json.loads(agent_result.output)
        assert agent_payload["schema_version"] == "harness.agent_spec/v1"
        agent = agent_payload["agent"]
        assert agent["id"] == agent_id
        assert agent["model_profile"] == "codex_supervised"
        assert agent["tool_policy"] == "read_only"
        assert agent["memory_scope"] == "quant"

    group_result = runner.invoke(app, ["specs", "agent", "quant_research", "--output", "json"])
    assert group_result.exit_code == 0
    group_payload = json.loads(group_result.output)
    assert group_payload["agent"]["kind"] == "group"

    preview_result = runner.invoke(
        app, ["specs", "preview", "agent", "commodities_researcher", "--output", "json"]
    )
    assert preview_result.exit_code == 0
    preview_payload = json.loads(preview_result.output)
    assert preview_payload["preview"]["parent"] == "quant_research"
    assert preview_payload["preview"]["effective_agent"]["parent_chain"] == ["quant_research"]
    assert [profile["id"] for profile in preview_payload["preview"]["profiles"]] == [
        "commodities_researcher.default"
    ]

    serialized = workbench_result.output
    assert "settings" not in serialized
    assert "base_url" not in serialized
    assert "api_key" not in serialized
    assert "OPENAI_API_KEY" not in serialized
    assert not (tmp_path / ".harness").exists()


def test_cli_specs_default_outputs_remain_text() -> None:
    registry = runner.invoke(app, ["specs"])
    agent = runner.invoke(app, ["specs", "agent", "repo_inspector"])
    workbench = runner.invoke(app, ["specs", "workbench", "coding"])

    assert registry.exit_code == 0
    assert not registry.output.lstrip().startswith("{")
    assert "Built-in specs:" in registry.output
    assert "repo_inspector" in registry.output
    assert "coding" in registry.output

    assert agent.exit_code == 0
    assert not agent.output.lstrip().startswith("{")
    assert "Agent: repo_inspector" in agent.output
    assert "Kind: specialist" in agent.output

    assert workbench.exit_code == 0
    assert not workbench.output.lstrip().startswith("{")
    assert "Workbench: coding" in workbench.output
    assert (
        "Allowed agents: coding_orchestrator, repo_inspector, code_editor, test_runner, "
        "implementation_reviewer, security_reviewer, factuality_reviewer"
    ) in workbench.output


def test_cli_specs_missing_ids_fail_without_creating_harness_dir(tmp_path) -> None:
    missing_agent = runner.invoke(app, ["specs", "agent", "missing_agent"])
    missing_workbench = runner.invoke(app, ["specs", "workbench", "missing_workbench"])

    assert missing_agent.exit_code != 0
    assert "Agent not found: missing_agent" in missing_agent.output
    assert missing_workbench.exit_code != 0
    assert "Workbench not found: missing_workbench" in missing_workbench.output
    assert not (tmp_path / ".harness").exists()


def test_cli_specs_validate_valid_bundle_supports_text_and_json(tmp_path, monkeypatch) -> None:
    bundle_path = tmp_path / "specs.json"
    bundle_path.write_text(
        json.dumps(
            {
                "schema_version": "harness.spec_bundle/v1",
                "model_profiles": {
                    "local_reasoning": {
                        "id": "local_reasoning",
                        "kind": "local",
                        "backend": "local_openai_compatible",
                    }
                },
                "tool_policies": {
                    "read_only": {
                        "tools": {"repo_read": "allowed"},
                        "network": "forbidden",
                        "active_repo_write": "forbidden",
                        "hosted_boundary": "approval_required",
                    }
                },
                "memory_scopes": {"project": {"id": "project"}},
                "agents": {
                    "repo_inspector": {
                        "id": "repo_inspector",
                        "kind": "specialist",
                        "role": "Inspect repository evidence.",
                        "model_profile": "local_reasoning",
                        "tool_policy": "read_only",
                        "memory_scope": "project",
                    }
                },
                "workbenches": {
                    "coding": {
                        "id": "coding",
                        "description": "Coding workbench.",
                        "allowed_agents": ["repo_inspector"],
                        "default_model_profile": "local_reasoning",
                        "forbidden_actions": ["paid_api_fallback", "hosted_fallback"],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "harness.cli.main.load_config",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("spec validation must not load config")),
    )
    monkeypatch.setattr(
        "harness.cli.main.CodexCliBackend",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("spec validation must not preflight Codex")),
    )
    monkeypatch.setattr(
        "harness.cli.main.LocalOpenAICompatibleBackend",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("spec validation must not preflight local backend")),
    )

    text = runner.invoke(app, ["specs", "validate", str(bundle_path)])
    json_result = runner.invoke(app, ["specs", "validate", str(bundle_path), "--output", "json"])

    assert text.exit_code == 0
    assert f"Spec bundle valid: {bundle_path.resolve()}" in text.output
    assert not text.output.lstrip().startswith("{")

    assert json_result.exit_code == 0
    payload = json.loads(json_result.output)
    assert payload["schema_version"] == "harness.spec_validation/v1"
    assert payload["ok"] is True
    assert payload["path"] == str(bundle_path.resolve())
    assert payload["errors"] == []
    assert payload["registry"]["agents"]["repo_inspector"]["kind"] == "specialist"
    serialized = json.dumps(payload)
    assert "settings" not in serialized
    assert "base_url" not in serialized
    assert "api_key" not in serialized
    assert "api_key_env" not in serialized
    assert "OPENAI_API_KEY" not in serialized
    assert not (tmp_path / ".harness").exists()


def test_cli_specs_validate_invalid_bundle_supports_text_and_json(tmp_path) -> None:
    bundle_path = tmp_path / "specs.json"
    bundle_path.write_text(
        json.dumps(
            {
                "schema_version": "harness.spec_bundle/v1",
                "model_profiles": {
                    "local_reasoning": {
                        "id": "local_reasoning",
                        "kind": "local",
                        "backend": "local_openai_compatible",
                    }
                },
                "workbenches": {
                    "coding": {
                        "id": "coding",
                        "description": "Coding workbench.",
                        "allowed_agents": ["missing_agent"],
                        "default_model_profile": "local_reasoning",
                        "forbidden_actions": ["paid_api_fallback", "hosted_fallback"],
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    text = runner.invoke(app, ["specs", "validate", str(bundle_path)])
    json_result = runner.invoke(app, ["specs", "validate", str(bundle_path), "--output", "json"])

    assert text.exit_code != 0
    assert f"Spec bundle invalid: {bundle_path.resolve()}" in text.output
    assert "missing allowed agent" in text.output

    assert json_result.exit_code != 0
    payload = json.loads(json_result.output)
    assert payload["schema_version"] == "harness.spec_validation/v1"
    assert payload["ok"] is False
    assert "missing allowed agent" in payload["errors"][0]
    assert not (tmp_path / ".harness").exists()


def test_cli_specs_validate_missing_schema_version_supports_json_error(tmp_path) -> None:
    bundle_path = tmp_path / "specs.json"
    bundle_path.write_text(
        json.dumps(
            {
                "model_profiles": {
                    "local_reasoning": {
                        "id": "local_reasoning",
                        "kind": "local",
                        "backend": "local_openai_compatible",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["specs", "validate", str(bundle_path), "--output", "json"])

    assert result.exit_code != 0
    payload = json.loads(result.output)
    assert payload["schema_version"] == "harness.spec_validation/v1"
    assert payload["ok"] is False
    assert payload["errors"] == ["Spec bundle missing schema_version."]
    assert not (tmp_path / ".harness").exists()


def test_cli_specs_validate_unsupported_schema_version_supports_json_error(tmp_path) -> None:
    bundle_path = tmp_path / "specs.json"
    bundle_path.write_text(
        json.dumps(
            {
                "schema_version": "harness.spec_bundle/v0",
                "model_profiles": {
                    "local_reasoning": {
                        "id": "local_reasoning",
                        "kind": "local",
                        "backend": "local_openai_compatible",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["specs", "validate", str(bundle_path), "--output", "json"])

    assert result.exit_code != 0
    payload = json.loads(result.output)
    assert payload["schema_version"] == "harness.spec_validation/v1"
    assert payload["ok"] is False
    assert payload["errors"] == ["Unsupported spec bundle schema_version: harness.spec_bundle/v0"]
    assert not (tmp_path / ".harness").exists()


def test_cli_run_read_only_repo_summary_with_mocked_codex_backend(tmp_path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    ApprovalStore(tmp_path).add("codex_cli", "hosted_provider", ["read_only_repo_summary"], 1)

    class FakeBackend:
        def __init__(self, config):
            self.config = config
            self.name = config.name

        def preflight(self):
            return BackendStatus(
                available=True,
                metadata=self.config.metadata,
                capabilities=self.config.capabilities.model_copy(
                    update={
                        "supports_read_only_sandbox": True,
                        "supports_cd": True,
                        "supports_model_arg": True,
                        "supports_output_last_message": True,
                    }
                ),
            )

        def run_read_only(self, project_root, prompt, final_message_path):
            final_message_path.write_text("Codex subscription summary.", encoding="utf-8")
            return CodexRunResult(
                command=["codex", "exec", "--cd", str(project_root), "--model", "gpt-5.5", "-c", 'model_reasoning_effort="low"', "--sandbox", "read-only", prompt],
                stdout="",
                stderr="",
                exit_status=0,
                json_events=[],
                final_message="Codex subscription summary.",
            )

    monkeypatch.setattr("harness.cli.main.CodexCliBackend", FakeBackend)
    result = runner.invoke(
        app,
        [
            "run",
            "inspect this repo and explain the structure",
            "--project",
            str(tmp_path),
            "--task-type",
            "read_only_repo_summary",
        ],
    )
    assert result.exit_code == 0
    assert "Created run" in result.output
    assert "Codex subscription summary." in result.output
    run_id = result.output.split("Created run ", 1)[1].splitlines()[0]
    report = tmp_path / ".harness" / "runs" / run_id / "final_report.md"
    report_text = report.read_text(encoding="utf-8")
    assert "codex_cli" in report_text
    assert "gpt-5.5" in report_text
    assert "low" in report_text


def test_cli_run_codex_backend_unavailable_fails_with_guidance(tmp_path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    ApprovalStore(tmp_path).add("codex_cli", "hosted_provider", ["read_only_repo_summary"], 1)

    class FakeBackend:
        def __init__(self, config):
            self.config = config

        def preflight(self):
            return BackendStatus(
                available=False,
                reason="Codex CLI is unavailable. Run codex login.",
                metadata=self.config.metadata,
                capabilities=self.config.capabilities,
            )

    monkeypatch.setattr("harness.cli.main.CodexCliBackend", FakeBackend)
    result = runner.invoke(
        app,
        [
            "run",
            "inspect this repo",
            "--project",
            str(tmp_path),
            "--task-type",
            "read_only_repo_summary",
        ],
    )
    assert result.exit_code != 0
    assert "Codex CLI is unavailable" in result.output


def test_cli_run_read_only_uses_codex_subscription_without_local_or_paid_fallback(tmp_path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    ApprovalStore(tmp_path).add("codex_cli", "hosted_provider", ["read_only_repo_summary"], 1)

    class FakeCodexBackend:
        def __init__(self, config):
            self.config = config
            self.name = config.name

        def preflight(self):
            return BackendStatus(
                available=True,
                metadata=self.config.metadata,
                capabilities=self.config.capabilities.model_copy(
                    update={
                        "supports_read_only_sandbox": True,
                        "supports_cd": True,
                        "supports_model_arg": True,
                        "supports_output_last_message": True,
                    }
                ),
            )

        def run_read_only(self, project_root, prompt, final_message_path):
            final_message_path.write_text("Codex subscription route.", encoding="utf-8")
            return CodexRunResult(
                command=["codex", "exec", "--cd", str(project_root), "--model", "gpt-5.5", "-c", 'model_reasoning_effort="low"', "--sandbox", "read-only", prompt],
                stdout="",
                stderr="",
                exit_status=0,
                json_events=[],
                final_message="Codex subscription route.",
            )

    monkeypatch.setattr("harness.cli.main.CodexCliBackend", FakeCodexBackend)
    monkeypatch.setattr(
        "harness.cli.main.LocalOpenAICompatibleBackend",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("read-only summary must not use local backend")),
    )
    result = runner.invoke(
        app,
        [
            "run",
            "inspect this repo",
            "--project",
            str(tmp_path),
            "--task-type",
            "read_only_repo_summary",
        ],
    )
    assert result.exit_code == 0
    assert "Codex subscription route." in result.output


def test_cli_backends_preflight_reports_codex_without_paid_preflight(tmp_path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0

    class FakeCodexBackend:
        def __init__(self, config):
            self.config = config

        def preflight(self):
            return BackendStatus(
                available=True,
                metadata=self.config.metadata,
                capabilities=self.config.capabilities.model_copy(update={"supports_exec": True}),
            )

    class FakeLocalBackend:
        def __init__(self, config):
            self.config = config

        def preflight(self):
            return BackendStatus(
                available=False,
                reason="local unavailable",
                metadata=self.config.metadata,
                capabilities=self.config.capabilities,
            )

    monkeypatch.setattr("harness.cli.main.CodexCliBackend", FakeCodexBackend)
    monkeypatch.setattr("harness.cli.main.LocalOpenAICompatibleBackend", FakeLocalBackend)
    result = runner.invoke(app, ["backends", "preflight", "--project", str(tmp_path)])
    assert result.exit_code == 0
    assert "codex_cli:" in result.output
    assert "available: True" in result.output
    assert "Paid backend preflight skipped" in result.output


def test_cli_backends_support_json_output_without_settings(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0

    result = runner.invoke(app, ["backends", "--project", str(tmp_path), "--output", "json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["schema_version"] == "harness.backends/v1"
    names = {backend["name"] for backend in payload["backends"]}
    paid = next(backend for backend in payload["backends"] if backend["name"] == "paid_openai_compatible")

    assert {"codex_cli", "local_openai_compatible", "paid_openai_compatible"} <= names
    assert paid["constraints"] == ["disabled_by_default", "no_automatic_fallback", "preflight_skipped"]
    serialized = json.dumps(payload)
    assert "settings" not in serialized
    assert "base_url" not in serialized
    assert "api_key" not in serialized
    assert "api_key_env" not in serialized


def test_cli_backends_preflight_supports_json_output(tmp_path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0

    class FakeCodexBackend:
        def __init__(self, config):
            self.config = config

        def preflight(self):
            return BackendStatus(
                available=True,
                metadata=self.config.metadata,
                capabilities=self.config.capabilities.model_copy(update={"supports_exec": True}),
            )

    class FakeLocalBackend:
        def __init__(self, config):
            self.config = config

        def preflight(self):
            return BackendStatus(
                available=False,
                reason="local unavailable",
                metadata=self.config.metadata,
                capabilities=self.config.capabilities,
            )

    monkeypatch.setattr("harness.cli.main.CodexCliBackend", FakeCodexBackend)
    monkeypatch.setattr("harness.cli.main.LocalOpenAICompatibleBackend", FakeLocalBackend)
    result = runner.invoke(app, ["backends", "preflight", "--project", str(tmp_path), "--output", "json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["schema_version"] == "harness.backend_preflight/v1"
    by_name = {backend["name"]: backend for backend in payload["backends"]}

    assert by_name["codex_cli"]["available"] is True
    assert by_name["codex_cli"]["detected_capabilities"]["supports_exec"] is True
    assert by_name["local_openai_compatible"]["available"] is False
    assert by_name["local_openai_compatible"]["reason"] == "local unavailable"
    assert by_name["paid_openai_compatible"]["available"] is False
    assert by_name["paid_openai_compatible"]["reason"] == "Paid backend preflight skipped; disabled by default."


def test_cli_approvals_add_list_revoke(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    add = runner.invoke(
        app,
        [
            "approvals",
            "add",
            "--backend",
            "codex_cli",
            "--data-boundary",
            "hosted_provider",
            "--task-types",
            "repo_planning",
            "--duration-days",
            "30",
            "--project",
            str(tmp_path),
        ],
    )
    assert add.exit_code == 0
    approval_id = add.output.split("Created approval ", 1)[1].strip()
    listed = runner.invoke(app, ["approvals", "--project", str(tmp_path)])
    assert listed.exit_code == 0
    assert approval_id in listed.output
    revoked = runner.invoke(app, ["approvals", "revoke", approval_id, "--project", str(tmp_path)])
    assert revoked.exit_code == 0
    listed_after = runner.invoke(app, ["approvals", "--project", str(tmp_path)])
    assert "revoked=True" in listed_after.output


def test_cli_approvals_support_json_output(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    add = runner.invoke(
        app,
        [
            "approvals",
            "add",
            "--backend",
            "codex_cli",
            "--data-boundary",
            "hosted_provider",
            "--task-types",
            "repo_planning",
            "--duration-days",
            "30",
            "--project",
            str(tmp_path),
        ],
    )
    assert add.exit_code == 0
    approval_id = add.output.split("Created approval ", 1)[1].strip()
    assert runner.invoke(app, ["approvals", "revoke", approval_id, "--project", str(tmp_path)]).exit_code == 0

    result = runner.invoke(app, ["approvals", "--project", str(tmp_path), "--output", "json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["schema_version"] == "harness.approvals/v1"

    assert payload["approvals"][0]["id"] == approval_id
    assert payload["approvals"][0]["backend"] == "codex_cli"
    assert payload["approvals"][0]["data_boundary"] == "hosted_provider"
    assert payload["approvals"][0]["task_types"] == ["repo_planning"]
    assert payload["approvals"][0]["revoked"] is True


def test_cli_repo_planning_requires_hosted_approval(tmp_path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    result = runner.invoke(
        app,
        [
            "run",
            "plan the safest fix",
            "--project",
            str(tmp_path),
            "--task-type",
            "repo_planning",
        ],
        input="n\n",
    )
    assert result.exit_code != 0
    assert "Hosted data-boundary approval required" in result.output
    assert "backend: codex_cli" in result.output
    assert "billing mode: subscription" in result.output
    assert "execution location: mixed" in result.output
    assert "data boundary: hosted_provider" in result.output
    assert "task type: repo_planning" in result.output
    assert f"project root: {tmp_path}" in result.output
    assert "data that may be sent:" in result.output
    assert "Hosted data-boundary approval denied." in result.output


def test_cli_repo_planning_uses_valid_approval_profile(tmp_path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    approval = ApprovalStore(tmp_path).add(
        backend="codex_cli",
        data_boundary="hosted_provider",
        task_types=["repo_planning"],
        duration_days=30,
    )

    class FakeCodexBackend:
        def __init__(self, config):
            self.config = config
            self.name = config.name

        def preflight(self):
            return BackendStatus(
                available=True,
                metadata=self.config.metadata,
                capabilities=self.config.capabilities.model_copy(
                    update={"supports_exec": True, "supports_read_only_sandbox": True}
                ),
            )

        def run_read_only(self, project_root, prompt, final_message_path):
            if final_message_path:
                final_message_path.write_text("Plan from Codex.", encoding="utf-8")
            return CodexRunResult(
                command=["codex", "exec", "--sandbox", "read-only", "plan"],
                stdout="",
                stderr="",
                exit_status=0,
                json_events=[],
                final_message="Plan from Codex.",
            )

    monkeypatch.setattr("harness.cli.main.CodexCliBackend", FakeCodexBackend)
    result = runner.invoke(
        app,
        [
            "run",
            "plan the safest fix",
            "--project",
            str(tmp_path),
            "--task-type",
            "repo_planning",
        ],
    )
    assert result.exit_code == 0
    assert approval.id in result.output
    run_id = result.output.split("Created run ", 1)[1].splitlines()[0]
    report = tmp_path / ".harness" / "runs" / run_id / "final_report.md"
    assert "Plan from Codex." in report.read_text(encoding="utf-8")


def test_existing_read_only_route_uses_codex_subscription(tmp_path, monkeypatch) -> None:
    test_cli_run_read_only_repo_summary_with_mocked_codex_backend(tmp_path, monkeypatch)


def test_cli_bare_prompt_runs_codex_direct_agent(tmp_path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=tmp_path, check=True, capture_output=True)

    class FakeCodexBackend:
        def __init__(self, config):
            self.config = config
            self.name = config.name

        def preflight(self):
            return BackendStatus(available=True, metadata=self.config.metadata, capabilities=self.config.capabilities)

        def run_direct_agent(self, project_root, prompt, final_message_path, *, model=None, reasoning_effort=None):
            assert prompt == "change value"
            assert model == "codex_cli/gpt-5.5"
            assert reasoning_effort == "medium"
            (Path(project_root) / "app.py").write_text("value = 2\n", encoding="utf-8")
            final_message_path.write_text("Changed the value.", encoding="utf-8")
            self.config.settings["last_codex_approval_mode"] = "on-request via --ask-for-approval"
            self.config.settings["last_codex_sandbox_mode"] = "workspace-write"
            self.config.settings["last_apply_back_approval_required"] = False
            return (
                CodexRunResult(
                    command=["codex", "exec", "--cd", str(project_root), "--sandbox", "workspace-write", prompt],
                    stdout='{"type":"done","message":"Changed"}\n',
                    stderr="",
                    exit_status=0,
                    json_events=[{"type": "done", "message": "Changed"}],
                    final_message="Changed the value.",
                ),
                self.config.capabilities,
                "network not enforceable in fake",
            )

    monkeypatch.setattr("harness.cli.main.CodexCliBackend", FakeCodexBackend)
    result = runner.invoke(
        app,
        [
            "change value",
            "--project",
            str(tmp_path),
            "--model",
            "codex_cli/gpt-5.5",
            "--reasoning-effort",
            "medium",
            "--no-stream",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Codex foreground agent" in result.output
    assert "Changed files: app.py" in result.output
    assert "Changed the value." in result.output
    assert "value = 2" in (tmp_path / "app.py").read_text(encoding="utf-8")
    run_id = result.output.split("Created run ", 1)[1].splitlines()[0]
    report = tmp_path / ".harness" / "runs" / run_id / "final_report.md"
    report_text = report.read_text(encoding="utf-8")
    assert "Direct workspace edits: true" in report_text
    assert "Apply-back approval required: False" in report_text


def test_cli_run_defaults_to_codex_direct_agent_json(tmp_path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=tmp_path, check=True, capture_output=True)

    class FakeCodexBackend:
        def __init__(self, config):
            self.config = config
            self.name = config.name

        def preflight(self):
            return BackendStatus(available=True, metadata=self.config.metadata, capabilities=self.config.capabilities)

        def run_direct_agent(self, project_root, prompt, final_message_path, *, model=None, reasoning_effort=None):
            (Path(project_root) / "app.py").write_text("value = 3\n", encoding="utf-8")
            final_message_path.write_text("Changed through run.", encoding="utf-8")
            self.config.settings["last_codex_approval_mode"] = "on-request via --ask-for-approval"
            self.config.settings["last_codex_sandbox_mode"] = "workspace-write"
            self.config.settings["last_apply_back_approval_required"] = False
            return (
                CodexRunResult(["codex", "exec", prompt], "", "", 0, [], "Changed through run."),
                self.config.capabilities,
                "",
            )

    monkeypatch.setattr("harness.cli.main.CodexCliBackend", FakeCodexBackend)
    result = runner.invoke(app, ["run", "change value", "--project", str(tmp_path), "--output", "json", "--no-stream"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema_version"] == "harness.codex_direct_agent/v1"
    assert payload["status"] == "completed"
    assert payload["changed_files"] == ["app.py"]
    assert "value = 3" in (tmp_path / "app.py").read_text(encoding="utf-8")


def test_cli_simple_code_edit_routes_to_local_backend_only(tmp_path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    (tmp_path / "app.py").write_text('print("old")\nvalue = 1\n', encoding="utf-8")

    def forbidden_codex(*args, **kwargs):
        raise AssertionError("simple_code_edit must not instantiate or execute Codex.")

    class FakeBackend:
        def __init__(self, config):
            self.config = config
            self.name = config.name
            self.base_url = str(config.settings["base_url"]).rstrip("/")
            self.responses = [
                '{"command":"final_answer","arguments":{"answer":"No patch."}}',
            ]

        def preflight(self):
            return BackendStatus(
                available=True,
                metadata=self.config.metadata,
                capabilities=self.config.capabilities,
            )

        def complete(self, messages):
            return self.responses.pop(0)

    monkeypatch.setattr("harness.cli.main.LocalOpenAICompatibleBackend", FakeBackend)
    monkeypatch.setattr("harness.cli.main.CodexCliBackend", forbidden_codex)
    result = runner.invoke(
        app,
        [
            "run",
            "make a simple edit",
            "--project",
            str(tmp_path),
            "--task-type",
            "simple_code_edit",
        ],
    )
    assert result.exit_code == 0
    assert "No patch." in result.output


def test_cli_simple_code_edit_denied_patch_is_not_applied(tmp_path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    (tmp_path / "app.py").write_text('print("old")\nvalue = 1\n', encoding="utf-8")

    patch = """--- a/app.py
+++ b/app.py
@@ -1,2 +1,2 @@
 print("old")
-value = 1
+value = 2
"""

    class FakeBackend:
        def __init__(self, config):
            self.config = config
            self.name = config.name
            self.base_url = str(config.settings["base_url"]).rstrip("/")
            self.responses = [
                json.dumps({"command": "apply_patch", "arguments": {"patch": patch}}),
                '{"command":"final_answer","arguments":{"answer":"Denied."}}',
            ]

        def preflight(self):
            return BackendStatus(
                available=True,
                metadata=self.config.metadata,
                capabilities=self.config.capabilities,
            )

        def complete(self, messages):
            return self.responses.pop(0)

    monkeypatch.setattr("harness.cli.main.LocalOpenAICompatibleBackend", FakeBackend)
    result = runner.invoke(
        app,
        [
            "run",
            "make a simple edit",
            "--project",
            str(tmp_path),
            "--task-type",
            "simple_code_edit",
        ],
        input="d\n",
    )
    assert result.exit_code == 0
    assert "Patch approval required:" in result.output
    assert "Denied." in result.output
    assert "value = 1" in (tmp_path / "app.py").read_text(encoding="utf-8")


def test_cli_simple_code_edit_approved_patch_is_applied(tmp_path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    (tmp_path / "app.py").write_text('print("old")\nvalue = 1\n', encoding="utf-8")

    patch = """--- a/app.py
+++ b/app.py
@@ -1,2 +1,2 @@
 print("old")
-value = 1
+value = 2
""".replace("++++", "+++")

    class FakeBackend:
        def __init__(self, config):
            self.config = config
            self.name = config.name
            self.base_url = str(config.settings["base_url"]).rstrip("/")
            self.responses = [
                json.dumps({"command": "apply_patch", "arguments": {"patch": patch}}),
                '{"command":"final_answer","arguments":{"answer":"Approved."}}',
            ]

        def preflight(self):
            return BackendStatus(
                available=True,
                metadata=self.config.metadata,
                capabilities=self.config.capabilities,
            )

        def complete(self, messages):
            return self.responses.pop(0)

    monkeypatch.setattr("harness.cli.main.LocalOpenAICompatibleBackend", FakeBackend)
    result = runner.invoke(
        app,
        [
            "run",
            "make a simple edit",
            "--project",
            str(tmp_path),
            "--task-type",
            "simple_code_edit",
        ],
        input="a\n",
    )
    assert result.exit_code == 0
    assert "Patch approval required:" in result.output
    assert "Approved." in result.output
    assert "Changed files: app.py" in result.output
    assert "value = 2" in (tmp_path / "app.py").read_text(encoding="utf-8")


def test_cli_agents_scaffold_validate_and_preview_custom_bundle(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    destination = tmp_path / "agents" / "my_agent"

    scaffold = runner.invoke(
        app,
        [
            "agents",
            "scaffold",
            "my_agent",
            "--workbench",
            "quant",
            "--kind",
            "specialist",
            "--parent",
            "quant_research",
            "--model-profile",
            "local_reasoning",
            "--tool-policy",
            "read_only",
            "--memory-scope",
            "quant",
            "--role",
            "My custom read-only agent.",
            "--output",
            str(destination),
            "--output-format",
            "json",
        ],
    )

    assert scaffold.exit_code == 0, scaffold.output
    scaffold_payload = json.loads(scaffold.output)
    assert scaffold_payload["schema_version"] == "harness.agent_scaffold/v1"
    assert scaffold_payload["ok"] is True
    assert scaffold_payload["agent_id"] == "my_agent"
    assert (destination / "agent.yaml").exists()
    assert (destination / "profiles" / "default.yaml").exists()

    validation = runner.invoke(app, ["agents", "validate", str(destination), "--output", "json"])
    preview = runner.invoke(app, ["agents", "preview", str(destination), "--output", "json"])

    assert validation.exit_code == 0, validation.output
    validation_payload = json.loads(validation.output)
    assert validation_payload["schema_version"] == "harness.agent_bundle_validation/v1"
    assert validation_payload["ok"] is True
    assert validation_payload["agent_id"] == "my_agent"
    assert [profile["id"] for profile in validation_payload["profiles"]] == ["my_agent.default"]

    assert preview.exit_code == 0, preview.output
    preview_payload = json.loads(preview.output)
    assert preview_payload["schema_version"] == "harness.agent_bundle_preview/v1"
    assert preview_payload["ok"] is True
    assert preview_payload["agent"]["id"] == "my_agent"
    assert [parent["id"] for parent in preview_payload["parent_chain"]] == ["quant_research"]
    assert preview_payload["effective_agent"]["tool_policy"] == "read_only"
    assert preview_payload["workbench"]["id"] == "quant"
    assert not (tmp_path / ".harness").exists()


def test_cli_agents_generate_from_description_uses_safe_defaults(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    destination = tmp_path / "agents" / "review_planner"

    generated = runner.invoke(
        app,
        [
            "agents",
            "generate",
            "review_planner",
            "--description",
            "Review coding plans and produce concise read-only implementation guidance.",
            "--output",
            str(destination),
            "--output-format",
            "json",
        ],
    )

    assert generated.exit_code == 0, generated.output
    payload = json.loads(generated.output)
    assert payload["schema_version"] == "harness.agent_generate/v1"
    assert payload["ok"] is True
    assert payload["generated_from_description"] is True
    assert payload["provider_execution_started"] is False
    assert payload["model_execution_started"] is False
    assert payload["hidden_provider_fallback"] is False
    assert payload["permission_granting"] is False
    assert payload["authority_granting"] is False
    assert payload["defaults"] == {
        "kind": "specialist",
        "model_profile": "codex_supervised",
        "tool_policy": "read_only",
        "memory_scope": "project",
        "parent": None,
    }
    assert (destination / "agent.yaml").exists()
    agent_yaml = yaml.safe_load((destination / "agent.yaml").read_text(encoding="utf-8"))
    assert agent_yaml["agent"]["role"] == "Review coding plans and produce concise read-only implementation guidance."
    assert agent_yaml["workbench_id"] == "coding"
    assert agent_yaml["agent"]["tool_policy"] == "read_only"

    validation = runner.invoke(app, ["agents", "validate", str(destination), "--output", "json"])
    assert validation.exit_code == 0, validation.output
    assert json.loads(validation.output)["ok"] is True
    assert not (tmp_path / ".harness").exists()


def test_cli_agents_text_output_and_stable_json_errors(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    text_destination = tmp_path / "agents" / "text_agent"
    text = runner.invoke(
        app,
        [
            "agents",
            "scaffold",
            "text_agent",
            "--workbench",
            "quant",
            "--kind",
            "specialist",
            "--model-profile",
            "local_reasoning",
            "--tool-policy",
            "read_only",
            "--memory-scope",
            "quant",
            "--output",
            str(text_destination),
        ],
    )
    invalid = runner.invoke(
        app,
        [
            "agents",
            "scaffold",
            "repo_inspector",
            "--workbench",
            "quant",
            "--kind",
            "specialist",
            "--model-profile",
            "local_reasoning",
            "--tool-policy",
            "read_only",
            "--memory-scope",
            "quant",
            "--output",
            str(tmp_path / "agents" / "shadow"),
            "--output-format",
            "json",
        ],
    )
    missing = runner.invoke(app, ["agents", "validate", str(tmp_path / "missing"), "--output", "json"])
    forbidden = runner.invoke(
        app,
        [
            "agents",
            "scaffold",
            "bad_path_agent",
            "--workbench",
            "quant",
            "--kind",
            "specialist",
            "--model-profile",
            "local_reasoning",
            "--tool-policy",
            "read_only",
            "--memory-scope",
            "quant",
            "--output",
            str(tmp_path / ".harness" / "agent"),
            "--output-format",
            "json",
        ],
    )

    assert text.exit_code == 0, text.output
    assert "Agent bundle scaffolded" in text.output
    assert invalid.exit_code == 1
    invalid_payload = json.loads(invalid.output)
    assert invalid_payload["schema_version"] == "harness.agent_scaffold/v1"
    assert invalid_payload["ok"] is False
    assert invalid_payload["errors"] == ["Custom agent id shadows built-in agent: repo_inspector"]
    assert missing.exit_code == 1
    missing_payload = json.loads(missing.output)
    assert missing_payload["schema_version"] == "harness.agent_bundle_validation/v1"
    assert missing_payload["ok"] is False
    assert "does not exist" in missing_payload["errors"][0]
    assert forbidden.exit_code == 1
    forbidden_payload = json.loads(forbidden.output)
    assert forbidden_payload["schema_version"] == "harness.agent_scaffold/v1"
    assert forbidden_payload["ok"] is False
    assert forbidden_payload["errors"] == ["Agent bundle path is forbidden by harness safety policy."]
    assert not (tmp_path / ".harness").exists()


def test_cli_agents_do_not_preflight_backends_or_expose_secrets(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "harness.cli.main.load_config",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("agent authoring must not load config")),
    )
    monkeypatch.setattr(
        "harness.cli.main.CodexCliBackend",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("agent authoring must not preflight Codex")),
    )
    monkeypatch.setattr(
        "harness.cli.main.LocalOpenAICompatibleBackend",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("agent authoring must not preflight local backend")
        ),
    )
    destination = tmp_path / "agents" / "safe_agent"

    scaffold = runner.invoke(
        app,
        [
            "agents",
            "scaffold",
            "safe_agent",
            "--workbench",
            "quant",
            "--kind",
            "specialist",
            "--parent",
            "quant_research",
            "--model-profile",
            "local_reasoning",
            "--tool-policy",
            "read_only",
            "--memory-scope",
            "quant",
            "--output",
            str(destination),
            "--output-format",
            "json",
        ],
    )
    preview = runner.invoke(app, ["agents", "preview", str(destination), "--output", "json"])

    assert scaffold.exit_code == 0, scaffold.output
    assert preview.exit_code == 0, preview.output
    serialized = scaffold.output + preview.output
    assert "api_key" not in serialized
    assert "OPENAI_API_KEY" not in serialized
    assert "base_url" not in serialized
    assert not (tmp_path / ".harness").exists()


def test_cli_agents_import_list_inspect_and_task_reference_project_agent(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    destination = tmp_path / "agents" / "project_agent"
    scaffold = runner.invoke(
        app,
        [
            "agents",
            "scaffold",
            "project_agent",
            "--workbench",
            "quant",
            "--kind",
            "specialist",
            "--parent",
            "quant_research",
            "--model-profile",
            "local_reasoning",
            "--tool-policy",
            "read_only",
            "--memory-scope",
            "quant",
            "--output",
            str(destination),
            "--output-format",
            "json",
        ],
    )
    imported = runner.invoke(
        app,
        ["agents", "import", str(destination), "--project", str(tmp_path), "--output", "json"],
    )
    listed = runner.invoke(app, ["agents", "list", "--project", str(tmp_path), "--output", "json"])
    inspected = runner.invoke(
        app,
        ["agents", "inspect", "project_agent", "--project", str(tmp_path), "--output", "json"],
    )
    task = runner.invoke(
        app,
        [
            "tasks",
            "add",
            "--title",
            "Use project agent",
            "--agent",
            "project_agent",
            "--workbench",
            "quant",
            "--project",
            str(tmp_path),
            "--output",
            "json",
        ],
    )

    assert scaffold.exit_code == 0, scaffold.output
    assert imported.exit_code == 0, imported.output
    imported_payload = json.loads(imported.output)
    assert imported_payload["schema_version"] == "harness.project_agent/v1"
    assert imported_payload["ok"] is True
    assert imported_payload["agent_id"] == "project_agent"
    assert imported_payload["agent"]["id"] == "project_agent"
    assert imported_payload["profiles"][0]["id"] == "project_agent.default"
    assert imported_payload["content_sha256"]
    assert listed.exit_code == 0, listed.output
    listed_payload = json.loads(listed.output)
    assert listed_payload["schema_version"] == "harness.project_agents/v1"
    assert [agent["agent_id"] for agent in listed_payload["agents"]] == ["project_agent"]
    assert inspected.exit_code == 0, inspected.output
    inspected_payload = json.loads(inspected.output)
    assert inspected_payload["schema_version"] == "harness.project_agent/v1"
    assert inspected_payload["agent_id"] == "project_agent"
    assert task.exit_code == 0, task.output
    task_payload = json.loads(task.output)
    assert task_payload["task"]["agent_id"] == "project_agent"
    assert task_payload["task"]["spec_source_kind"] == "project"
    assert task_payload["task"]["spec_source_path"] == str(destination.resolve())


def test_cli_agents_import_rejects_duplicates_unknowns_and_mismatched_task_workbench(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    destination = tmp_path / "agents" / "project_agent"
    assert runner.invoke(
        app,
        [
            "agents",
            "scaffold",
            "project_agent",
            "--workbench",
            "quant",
            "--kind",
            "specialist",
            "--parent",
            "quant_research",
            "--model-profile",
            "local_reasoning",
            "--tool-policy",
            "read_only",
            "--memory-scope",
            "quant",
            "--output",
            str(destination),
            "--output-format",
            "json",
        ],
    ).exit_code == 0
    assert runner.invoke(app, ["agents", "import", str(destination), "--project", str(tmp_path)]).exit_code == 0

    duplicate = runner.invoke(
        app,
        ["agents", "import", str(destination), "--project", str(tmp_path), "--output", "json"],
    )
    missing = runner.invoke(
        app,
        ["agents", "inspect", "missing_agent", "--project", str(tmp_path), "--output", "json"],
    )
    mismatch = runner.invoke(
        app,
        [
            "tasks",
            "add",
            "--title",
            "Mismatch",
            "--agent",
            "project_agent",
            "--workbench",
            "coding",
            "--project",
            str(tmp_path),
            "--output",
            "json",
        ],
    )

    assert duplicate.exit_code == 1
    duplicate_payload = json.loads(duplicate.output)
    assert duplicate_payload["schema_version"] == "harness.project_agent/v1"
    assert duplicate_payload["errors"] == ["Project agent already imported: project_agent"]
    assert missing.exit_code == 1
    missing_payload = json.loads(missing.output)
    assert missing_payload["schema_version"] == "harness.project_agent/v1"
    assert missing_payload["errors"] == ["Project agent not found: missing_agent"]
    assert mismatch.exit_code == 1
    mismatch_payload = json.loads(mismatch.output)
    assert mismatch_payload["schema_version"] == "harness.task/v1"
    assert mismatch_payload["errors"] == ["Project agent project_agent belongs to workbench quant, not coding"]


def test_cli_agents_preview_imported_reports_drift_and_remove_unused_agent(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    destination = tmp_path / "agents" / "project_agent"
    assert runner.invoke(
        app,
        [
            "agents",
            "scaffold",
            "project_agent",
            "--workbench",
            "quant",
            "--kind",
            "specialist",
            "--parent",
            "quant_research",
            "--model-profile",
            "local_reasoning",
            "--tool-policy",
            "read_only",
            "--memory-scope",
            "quant",
            "--output",
            str(destination),
            "--output-format",
            "json",
        ],
    ).exit_code == 0
    assert runner.invoke(app, ["agents", "import", str(destination), "--project", str(tmp_path)]).exit_code == 0

    preview = runner.invoke(
        app,
        ["agents", "preview-imported", "project_agent", "--project", str(tmp_path), "--output", "json"],
    )
    (destination / "profiles" / "default.yaml").write_text(
        (destination / "profiles" / "default.yaml").read_text(encoding="utf-8").replace(
            "Default profile", "Changed profile"
        ),
        encoding="utf-8",
    )
    changed = runner.invoke(
        app,
        ["agents", "preview-imported", "project_agent", "--project", str(tmp_path), "--output", "json"],
    )
    removed = runner.invoke(
        app,
        ["agents", "remove", "project_agent", "--project", str(tmp_path), "--output", "json"],
    )
    inspected_after_remove = runner.invoke(
        app,
        ["agents", "inspect", "project_agent", "--project", str(tmp_path), "--output", "json"],
    )

    assert preview.exit_code == 0, preview.output
    preview_payload = json.loads(preview.output)
    assert preview_payload["schema_version"] == "harness.project_agent_preview/v1"
    assert preview_payload["ok"] is True
    assert preview_payload["agent"]["id"] == "project_agent"
    assert preview_payload["drift"]["status"] == "verified"
    assert [parent["id"] for parent in preview_payload["parent_chain"]] == ["quant_research"]
    assert changed.exit_code == 0, changed.output
    changed_payload = json.loads(changed.output)
    assert changed_payload["drift"]["status"] == "changed"
    assert removed.exit_code == 0, removed.output
    removed_payload = json.loads(removed.output)
    assert removed_payload["schema_version"] == "harness.project_agent/v1"
    assert removed_payload["ok"] is True
    assert removed_payload["removed"] is True
    assert removed_payload["agent"]["agent_id"] == "project_agent"
    assert inspected_after_remove.exit_code == 1


def test_cli_agents_remove_rejects_builtin_unknown_and_task_referenced_agents(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    destination = tmp_path / "agents" / "project_agent"
    assert runner.invoke(
        app,
        [
            "agents",
            "scaffold",
            "project_agent",
            "--workbench",
            "quant",
            "--kind",
            "specialist",
            "--parent",
            "quant_research",
            "--model-profile",
            "local_reasoning",
            "--tool-policy",
            "read_only",
            "--memory-scope",
            "quant",
            "--output",
            str(destination),
            "--output-format",
            "json",
        ],
    ).exit_code == 0
    assert runner.invoke(app, ["agents", "import", str(destination), "--project", str(tmp_path)]).exit_code == 0
    assert runner.invoke(
        app,
        [
            "tasks",
            "add",
            "--title",
            "Use project agent",
            "--agent",
            "project_agent",
            "--workbench",
            "quant",
            "--project",
            str(tmp_path),
        ],
    ).exit_code == 0

    used = runner.invoke(app, ["agents", "remove", "project_agent", "--project", str(tmp_path), "--output", "json"])
    builtin = runner.invoke(app, ["agents", "remove", "repo_inspector", "--project", str(tmp_path), "--output", "json"])
    missing = runner.invoke(app, ["agents", "remove", "missing_agent", "--project", str(tmp_path), "--output", "json"])

    assert used.exit_code == 1
    assert json.loads(used.output)["errors"] == ["Cannot remove project agent referenced by tasks: project_agent"]
    assert builtin.exit_code == 1
    assert json.loads(builtin.output)["errors"] == ["Cannot remove built-in agent: repo_inspector"]
    assert missing.exit_code == 1
    assert json.loads(missing.output)["errors"] == ["Project agent not found: missing_agent"]
