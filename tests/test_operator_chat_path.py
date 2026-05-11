from __future__ import annotations

import json

from typer.testing import CliRunner

from harness.chat import ChatSessionState, handle_chat_input, route_chat_intent
from harness.cli.main import app
from harness.execution import execute_lease
from harness.memory.sqlite_store import SQLiteStore
from harness.workflow_templates import template_for_intent


runner = CliRunner()


def test_workflow_templates_describe_initial_operator_paths(tmp_path) -> None:
    summary = template_for_intent("repo_summary", "summarize this repo", tmp_path)
    planning = template_for_intent("repo_planning", "plan how to improve the CLI", tmp_path)
    coding = template_for_intent("coding_fix", "fix the failing test with codex", tmp_path)

    assert summary.tasks[0].execution_adapter == "read_only_summary"
    assert summary.tasks[0].task_type == "read_only_repo_summary"
    assert planning.tasks[0].execution_adapter == "repo_planning"
    assert planning.tasks[0].task_type == "repo_planning"
    assert [task.execution_adapter for task in coding.tasks] == ["repo_planning", "codex_isolated_edit"]
    assert coding.tasks[1].depends_on_indexes == [0]
    assert "hosted_provider_codex" in coding.required_approvals


def test_natural_operator_intents_route_to_first_class_paths() -> None:
    assert route_chat_intent("summarize this repo")["intent"] == "repo_summary"
    assert route_chat_intent("plan how to improve the CLI")["intent"] == "repo_planning"
    assert route_chat_intent("fix the failing test with codex")["intent"] == "coding_fix"
    assert route_chat_intent("show recent runs")["intent"] == "show_runs"
    assert route_chat_intent("review the last result")["intent"] == "show_last_result"
    assert route_chat_intent("continue")["intent"] == "continue_workflow"
    assert route_chat_intent("stop")["intent"] == "stop_workflow"


def test_repo_summary_draft_is_visible_and_non_mutating_before_confirmation(tmp_path) -> None:
    state = ChatSessionState(codex_like_mode=True)

    response = handle_chat_input("summarize this repo", tmp_path, state)

    assert response["kind"] == "task_draft"
    assert response["draft"]["interpreted_intent"] == "repo_summary"
    assert response["draft"]["execution_adapter"] == "read_only_summary"
    assert response["draft"]["task_type"] == "read_only_repo_summary"
    rendered = "\n".join(response["lines"])
    assert "Interpreted intent: repo_summary" in rendered
    assert "Safety boundary:" in rendered
    assert "Chat does not call Codex" in rendered
    assert not (tmp_path / ".harness").exists()


def test_repo_planning_draft_uses_registered_adapter_contract(tmp_path) -> None:
    response = handle_chat_input("plan how to add a plugin system", tmp_path, ChatSessionState())

    assert response["kind"] == "task_draft"
    assert response["draft"]["interpreted_intent"] == "repo_planning"
    assert response["draft"]["execution_adapter"] == "repo_planning"
    assert response["draft"]["task_type"] == "repo_planning"
    assert response["draft"]["required_approvals"] == ["hosted_provider_codex"]


def test_coding_fix_template_creates_two_task_graph_and_blocks_on_missing_approval(tmp_path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0

    def fail_codex(*_args, **_kwargs):
        raise AssertionError("missing approval must block before provider construction")

    monkeypatch.setattr("harness.execution.CodexCliBackend", fail_codex)
    state = ChatSessionState(codex_like_mode=True)
    draft = handle_chat_input("fix the failing test with codex", tmp_path, state)
    assert draft["kind"] == "orchestration_draft"
    assert [task["execution_adapter"] for task in draft["draft"]["tasks"]] == ["repo_planning", "codex_isolated_edit"]

    response = handle_chat_input("yes", tmp_path, state)

    store = SQLiteStore(tmp_path)
    assert response["kind"] == "orchestration_result"
    assert response["ok"] is False
    assert state.latest_objective_id is not None
    tasks = store.list_tasks(objective_id=state.latest_objective_id)
    assert [task.metadata["execution_adapter"] for task in tasks] == ["repo_planning", "codex_isolated_edit"]
    assert tasks[1].depends_on == [tasks[0].id]
    rendered = "\n".join(response["lines"])
    assert "Hosted-boundary approval is required before Codex run creation." in rendered
    assert "harness approvals add --backend codex_cli --data-boundary hosted_provider" in rendered


def test_codex_like_dry_run_creates_leases_dispatches_and_renders_evidence(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    state = ChatSessionState(codex_like_mode=True)

    draft = handle_chat_input("create dry run task", tmp_path, state)
    response = handle_chat_input("yes", tmp_path, state)

    assert draft["kind"] == "task_draft"
    assert response["kind"] == "codex_like_task_result"
    assert response["ok"] is True
    rendered = "\n".join(response["lines"])
    assert "Task: succeeded" in rendered
    assert "Adapter: dry_run" in rendered
    assert "Run: run_" in rendered
    assert "Artifacts:" in rendered
    assert "Next:" in rendered
    assert "harness show run_" in rendered


def test_manual_task_run_once_and_registered_dispatch_return_evidence_summary(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    task = store.create_task(
        title="Manual dry run",
        metadata={"execution_adapter": "dry_run", "task_type": "phase_1a_test"},
    )

    tick = store.daemon_run_once(owner="test", pid=None)
    result = execute_lease(tmp_path, tick.lease.id, owner="test")

    assert tick.selected_task.id == task.id
    assert result.ok is True
    assert result.adapter_id == "dry_run"
    assert result.run is not None
    assert result.manifest is not None
    assert {artifact.kind for artifact in result.manifest.artifacts} >= {"final_report", "events", "manifest"}


def test_root_json_context_exposes_operator_surface_without_mutation(tmp_path) -> None:
    result = runner.invoke(app, ["--project", str(tmp_path), "--output", "json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema_version"] == "harness.chat/v1"
    assert payload["ok"] is True
    assert payload["initialized"] is False
    assert {adapter["id"] for adapter in payload["registered_adapters"]} >= {
        "read_only_summary",
        "repo_planning",
        "codex_isolated_edit",
    }
    assert "no_generic_shell" in payload["safety_boundaries"]
    assert not (tmp_path / ".harness").exists()
