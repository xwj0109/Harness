import json
import asyncio
import sqlite3
import subprocess
import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from harness.approvals import ApprovalStore
from harness.backends.codex_cli import CodexRunResult
from harness.chat import ChatSessionState, handle_chat_input, route_chat_intent
from harness.config import default_config
from harness.models import BackendStatus, BillingMode, DataBoundary, ExecutionLocation
from harness.cli.main import app
from harness.memory.sqlite_store import SQLiteStore
from harness.tui import (
    build_focused_tui_view_model,
    build_tui_dashboard,
    build_tui_panes,
    build_tui_view_model,
    build_chat_welcome_message,
    build_command_palette,
    build_command_palette_panes,
    build_right_panel_model,
    create_harness_app,
    build_slash_commands,
    handle_slash_command,
    filter_command_palette,
    filter_slash_commands,
    filter_tui_panes,
    render_chat_message,
    render_dashboard_text,
    render_filter_status,
    render_palette_status,
    render_right_panel,
    render_right_panel_status,
    render_view_status,
)


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
    assert dashboard["guidance"][0]["id"] == "initialize_project"
    assert [pane["id"] for pane in panes] == [
        "overview",
        "agents",
        "tasks",
        "leases",
        "daemon",
        "runs",
        "commands",
        "guidance",
        "safety",
    ]
    assert "Project" in rendered
    assert "Initialized: False" in rendered
    assert "Commands" in rendered
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

    dashboard = build_tui_dashboard(tmp_path)
    panes = build_tui_panes(dashboard)
    rendered = render_dashboard_text(dashboard)

    assert dashboard["initialized"] is True
    assert dashboard["summary"]["imported_agents"] == 1
    assert dashboard["summary"]["tasks_total"] == 1
    assert dashboard["summary"]["active_leases"] == 1
    assert dashboard["summary"]["recent_runs"] == 1
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
    assert [pane["id"] for pane in panes] == [
        "overview",
        "agents",
        "tasks",
        "leases",
        "daemon",
        "runs",
        "commands",
        "guidance",
        "safety",
    ]
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
    assert "harness daemon status --project" in rendered
    serialized = json.dumps(dashboard)
    assert "api_key" not in serialized
    assert "OPENAI_API_KEY" not in serialized
    assert "base_url" not in serialized


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
        "agent_authoring",
        "project_agents",
        "built_in_specs",
        "objectives_tasks",
        "daemon_control",
        "registered_adapters",
        "runtime_evidence",
        "packaging_smoke",
    ]
    assert len(entry_ids) == len(set(entry_ids))
    assert all(set(entry) >= {"id", "group_id", "title", "command", "description", "mutates_when_run", "safety_note"} for entry in palette["entries"])
    assert all(entry["group_id"] in group_ids for entry in palette["entries"])

    all_entries = filter_command_palette(palette, "")
    daemon_entries = filter_command_palette(palette, "daemon")
    read_only_entries = filter_command_palette(palette, "execute-read-only")
    planning_entries = filter_command_palette(palette, "repo_planning")
    adapter_entries = filter_command_palette(palette, "adapter")
    packaging_entries = filter_command_palette(palette, "wheel")
    missing_entries = filter_command_palette(palette, "does-not-exist")

    assert all_entries["schema_version"] == "harness.tui_command_palette_filter/v1"
    assert all_entries["total_matches"] == len(palette["entries"])
    assert any(entry["id"] == "daemon_control.run_once" for entry in daemon_entries["entries"])
    assert [entry["id"] for entry in read_only_entries["entries"]] == ["registered_adapters.execute_read_only"]
    assert any(entry["id"] == "objectives_tasks.add_repo_planning_task" for entry in planning_entries["entries"])
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


def test_tui_command_palette_panes_show_copy_only_command_details() -> None:
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
    assert "Copy-only command templates." in panes[0]["lines"]
    assert any("registered_adapters.execute_read_only" in line for line in panes[1]["lines"])
    selected_lines = "\n".join(panes[2]["lines"])
    assert "harness daemon execute-read-only task_lease_abc123 --project . --output json" in selected_lines
    assert "Compatibility command for the bounded read-only adapter when manually run." in selected_lines
    assert "No matching command template." in missing_panes[-1]["lines"]

    serialized = json.dumps({"panes": panes, "missing": missing_panes})
    assert "api_key" not in serialized
    assert "OPENAI_API_KEY" not in serialized
    assert "base_url" not in serialized
    assert "subprocess" not in serialized
    assert "artifact contents" not in serialized


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
        "queue_daemon",
        "agents_specs",
        "runtime_evidence",
        "command_palette",
        "safety",
    ]
    assert view["sections"][0]["pane_ids"] == ["overview", "guidance", "commands"]
    assert view["sections"][1]["pane_ids"] == ["tasks", "leases", "daemon"]
    assert view["sections"][4]["pane_ids"][0] == "command_palette"
    assert view["sections"][4]["pane_ids"][-1] == "command_palette_selected"
    assert view["pane_order"][:6] == ["overview", "guidance", "commands", "tasks", "leases", "daemon"]
    assert view["pane_order"][-1] == "safety"
    assert view["empty_state"] is None
    assert view["focus_mode"] == "dashboard"
    assert view["collapsed_section_ids"] == []
    assert all(section["collapsed"] is False for section in view["sections"])
    assert view["search"]["dashboard_panes"] == len(panes)
    assert view["search"]["palette_matches"] == len(palette["entries"])
    assert render_view_status(view).startswith(
        "View search: none | Focus: dashboard | Collapsed: 0 | Sections: 6 | Panes:"
    )
    assert f"Palette commands: {len(palette['entries'])}" in render_view_status(view)
    assert {hint["key"] for hint in view["navigation_hints"]} == {
        "/",
        "escape",
        "tab",
        "shift+tab",
        "ctrl+p/f2",
        "c",
        "shift+c",
        "ctrl+q",
        "enter",
        "copy-only",
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
    assert [section["id"] for section in model["sections"]] == [
        "assistant",
        "action",
        "project",
        "now",
        "queue",
        "recent",
        "adapters",
        "progress",
        "next",
    ]
    assert model["active_section_id"] == "assistant"
    assert model["summary"]["initialized"] is False
    assert "Assistant" in rendered
    assert "Model: codex_cli" in rendered
    assert "Side effects: action contracts" in rendered
    assert "No pending action." in rendered
    assert "/init" in rendered
    assert "summarize this repo" in rendered
    assert "State:" not in rendered
    assert "Panes:" not in rendered
    assert "IDs:" not in rendered
    assert "harness daemon execute-read-only task_lease_abc123 --project . --output json" not in rendered
    assert "no_openai_api_usage" not in rendered
    assert render_right_panel_status(model) == "Context | assistant | ready"
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

    assert "repo_planning -> repo_planning" in rendered
    assert "harness daemon run-once --project . --output json" in render_right_panel(
        build_right_panel_model(dashboard, {"palette": palette}, "", "dashboard")
    )
    assert "harness daemon execute <lease_id> --project . --output json" in render_right_panel(
        build_right_panel_model(dashboard, {"palette": palette}, "", "dashboard")
    )


def test_tui_right_panel_surfaces_assistant_context_and_pending_action(tmp_path) -> None:
    dashboard = build_tui_dashboard(tmp_path)
    palette = build_command_palette()

    model = build_right_panel_model(
        dashboard,
        {
            "palette": palette,
            "active_section_index": 1,
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

    assert model["active_section_id"] == "action"
    assert "Tools: read_file" in rendered
    assert "Context: harness_state, repo_tree" in rendered
    assert "Pending: Create Harness objective: improve chat" in rendered
    assert "Tool: create_objective" in rendered
    assert "Risk: control_plane_write" in rendered
    assert "Confirm: yes or /confirm" in rendered
    assert not (tmp_path / ".harness").exists()


def test_tui_right_panel_search_and_palette_are_progressive(tmp_path) -> None:
    dashboard = build_tui_dashboard(tmp_path)
    palette = build_command_palette()

    task_search = build_right_panel_model(dashboard, {"palette": palette}, "tasks", "dashboard")
    palette_focus = build_right_panel_model(dashboard, {"palette": palette}, "execute-read-only", "palette")
    missing = build_right_panel_model(dashboard, {"palette": palette}, "does-not-exist", "dashboard")

    assert [section["id"] for section in task_search["sections"]] == ["queue", "commands"]
    assert "Queue" in render_right_panel(task_search)
    assert "harness tasks list --project ." in render_right_panel(task_search)
    assert palette_focus["mode"] == "palette"
    assert "project" in [section["id"] for section in palette_focus["sections"]]
    assert palette_focus["sections"][-1]["id"] == "commands"
    assert "harness daemon execute-read-only task_lease_abc123 --project . --output json" in render_right_panel(palette_focus)
    assert render_right_panel_status(palette_focus).startswith("Context | palette | 1 commands")
    assert missing["empty_state"]["message"] == "No matches. Try /help, tasks, runs, adapters."
    assert render_right_panel(missing) == "No matches. Try /help, tasks, runs, adapters."
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
    from textual.widgets import Input

    async def run_pilot() -> None:
        app = create_harness_app(tmp_path)
        async with app.run_test(size=(100, 40)) as pilot:
            prompt = app.query_one("#prompt", Input)
            assert app.use_command_palette is False

            await pilot.press("/")
            await pilot.pause()
            assert prompt.value == "/"

            await pilot.press("escape")
            await pilot.pause()
            assert prompt.value == ""

            assert app._section_cursor_index == 0
            await pilot.press("tab")
            await pilot.pause()
            assert app._section_cursor_index == 1

            await pilot.press("shift+tab")
            await pilot.pause()
            assert app._section_cursor_index == 0

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


def test_tui_slash_commands_cover_palette_templates_without_execution() -> None:
    palette = build_command_palette()
    slash_commands = build_slash_commands(palette)

    names = [command["name"] for command in slash_commands["commands"]]
    assert slash_commands["schema_version"] == "harness.tui_slash_commands/v1"
    assert len(names) == len(set(names))
    assert {
        "home",
        "quickstart",
        "scaffold",
        "validate",
        "agents",
        "specs",
        "tasks",
        "lease",
        "inspect-lease",
        "execute-read-only",
        "execute",
        "plan-task",
        "runs",
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
        }
        for command in slash_commands["commands"]
    )

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
        "codex_isolated_edit/codex_code_edit, and repo_planning/repo_planning"
    ]


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
            assert model == "gpt-test"
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
            "gpt-test",
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
