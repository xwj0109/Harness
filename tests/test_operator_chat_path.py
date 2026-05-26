from __future__ import annotations

import json

from typer.testing import CliRunner

from harness.backends.codex_cli import CodexRunResult, NETWORK_NOT_ENFORCEABLE
from harness.chat import ChatSessionState, handle_chat_input, route_chat_intent
from harness.cli.main import app
from harness.execution import execute_lease
from harness.memory.sqlite_store import SQLiteStore
from harness.models import BackendCapabilities, BackendStatus
from harness.workflow_templates import template_for_intent


runner = CliRunner()


class FakeCodexBackend:
    def __init__(self, config) -> None:
        self.config = config
        self.name = config.name

    def preflight(self):
        return BackendStatus(
            available=True,
            metadata=self.config.metadata,
            capabilities=BackendCapabilities(
                supports_exec=True,
                supports_cd=True,
                supports_read_only_sandbox=True,
                supports_workspace_write_sandbox=True,
                supports_json_events=True,
                supports_output_last_message=True,
            ),
        )

    def run_read_only(self, project_root, prompt, final_message_path):
        if final_message_path:
            final_message_path.write_text("implementation plan", encoding="utf-8")
        return CodexRunResult(
            ["codex", "exec", "--cd", str(project_root), "--sandbox", "read-only"],
            "",
            "",
            0,
            [],
            "implementation plan",
        )

    def run_edit(self, isolated_workspace, prompt, final_message_path):
        if final_message_path:
            final_message_path.write_text("no changes needed", encoding="utf-8")
        return (
            CodexRunResult(
                ["codex", "exec", "--cd", str(isolated_workspace), "--sandbox", "workspace-write"],
                "",
                "",
                0,
                [],
                "no changes needed",
            ),
            self.preflight().capabilities,
            NETWORK_NOT_ENFORCEABLE,
        )


def test_workflow_templates_describe_initial_operator_paths(tmp_path) -> None:
    summary = template_for_intent("repo_summary", "summarize this repo", tmp_path)
    planning = template_for_intent("repo_planning", "plan how to improve the CLI", tmp_path)
    coding = template_for_intent("coding_fix", "fix the failing test with codex", tmp_path)

    assert summary.tasks[0].execution_adapter == "read_only_summary"
    assert summary.tasks[0].task_type == "read_only_repo_summary"
    assert planning.tasks[0].execution_adapter == "repo_planning"
    assert planning.tasks[0].task_type == "repo_planning"
    assert [task.execution_adapter for task in coding.tasks] == [
        "repo_planning",
        "codex_isolated_edit",
        "dry_run",
        "dry_run",
        "dry_run",
        "dry_run",
    ]
    assert coding.tasks[1].depends_on_indexes == [0]
    assert coding.tasks[3].agent_id == "implementation_reviewer"
    assert coding.tasks[3].metadata()["review_role"] == "implementation_reviewer"
    assert coding.tasks[4].agent_id == "security_reviewer"
    assert coding.tasks[4].metadata()["blocks_apply_back"] is True
    assert coding.tasks[5].metadata()["requires_evidence_links"] == "objective,task,run,artifact,policy"
    assert "hosted_provider_codex" in coding.required_approvals


def test_natural_operator_intents_route_to_first_class_paths() -> None:
    assert route_chat_intent("summarize this repo")["intent"] == "repo_summary"
    assert route_chat_intent("plan how to improve the CLI")["intent"] == "repo_planning"
    assert route_chat_intent("fix the failing test with codex")["intent"] == "coding_fix"
    assert route_chat_intent("create a python script for black scholes pricing")["intent"] == "coding_fix"
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


def test_chat_self_managed_local_action_returns_policy_and_file_evidence(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0

    response = handle_chat_input("create scratch.md with hello world", tmp_path, ChatSessionState())

    assert response["kind"] == "self_managed_local_action"
    assert response["ok"] is True
    assert (tmp_path / "scratch.md").read_text(encoding="utf-8") == "hello world\n"
    rendered = "\n".join(response["lines"])
    assert "Created: scratch.md" in rendered
    assert "Policy: auto_allowed; sandbox=safe; executor=write_file" in rendered
    assert "no provider, shell, Docker, network, permission grant, or human approval prompt" in rendered
    assert response["decision"]["status"] == "auto_allowed"
    assert response["decision"]["sandbox_assessment"]["status"] == "safe"


def test_chat_black_scholes_python_script_runs_as_self_managed_action(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0

    response = handle_chat_input("create a python script for the black scholes pricing", tmp_path, ChatSessionState())

    script = tmp_path / "black_scholes_pricing.py"
    assert response["kind"] == "self_managed_local_action"
    assert response["ok"] is True
    assert script.exists()
    assert "def black_scholes_price(" in script.read_text(encoding="utf-8")
    rendered = "\n".join(response["lines"])
    assert "Created: black_scholes_pricing.py" in rendered
    assert "Policy: auto_allowed; sandbox=safe; executor=create_file_with_content" in rendered
    assert "human approval prompt" in rendered


def test_coding_fix_template_creates_reviewed_task_graph_and_prepares_scoped_approval(tmp_path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0

    monkeypatch.setattr("harness.execution.CodexCliBackend", FakeCodexBackend)
    state = ChatSessionState(codex_like_mode=True)
    draft = handle_chat_input("fix the failing test with codex", tmp_path, state)
    assert draft["kind"] == "orchestration_draft"
    assert [task["execution_adapter"] for task in draft["draft"]["tasks"]] == [
        "repo_planning",
        "codex_isolated_edit",
        "dry_run",
        "dry_run",
        "dry_run",
        "dry_run",
    ]

    response = handle_chat_input("yes", tmp_path, state)

    store = SQLiteStore(tmp_path)
    assert response["kind"] == "orchestration_result"
    assert response["ok"] is True
    assert state.latest_objective_id is not None
    tasks = store.list_tasks(objective_id=state.latest_objective_id)
    assert [task.metadata["execution_adapter"] for task in tasks] == [
        "repo_planning",
        "codex_isolated_edit",
        "dry_run",
        "dry_run",
        "dry_run",
        "dry_run",
    ]
    assert tasks[1].depends_on == [tasks[0].id]
    assert tasks[3].depends_on == [tasks[2].id]
    assert tasks[3].metadata["review_role"] == "implementation_reviewer"
    assert tasks[4].depends_on == [tasks[3].id]
    assert tasks[4].metadata["review_role"] == "security_reviewer"
    assert tasks[4].metadata["blocks_apply_back"] is True
    assert set(tasks[5].depends_on) == {tasks[0].id, tasks[1].id, tasks[2].id, tasks[3].id, tasks[4].id}
    rendered = "\n".join(response["lines"])
    assert "Prepared scoped hosted-provider Codex approval" in rendered
    assert f"objectives={state.latest_objective_id}" in rendered
    approval = store.project_root / ".harness" / "approvals.yaml"
    assert approval.exists()


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
