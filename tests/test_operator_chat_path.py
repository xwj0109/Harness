from __future__ import annotations

import json

from typer.testing import CliRunner

from harness.backends.codex_cli import CodexRunResult, NETWORK_NOT_ENFORCEABLE
from harness.chat import (
    ChatSessionState,
    OrchestratedCheckpointDraft,
    OrchestratedRunDraft,
    OrchestratedTaskDraft,
    _create_and_run_orchestration,
    _orchestration_draft_response,
    handle_chat_input,
    route_chat_intent,
)
from harness.cli.main import app
from harness.execution import execute_lease
from harness.memory.sqlite_store import SQLiteStore
from harness.models import BackendCapabilities, BackendStatus
from harness.objective_checkpoints import evaluate_objective_checkpoint_gate, list_objective_checkpoints
from harness.operator_context import build_operator_context
from harness.right_pane import build_right_pane_cockpit_model
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
    research = template_for_intent("research_brief", "research the architecture", tmp_path)

    assert summary.tasks[0].execution_adapter == "read_only_summary"
    assert summary.tasks[0].task_type == "read_only_repo_summary"
    assert planning.tasks[0].execution_adapter == "repo_planning"
    assert planning.tasks[0].task_type == "repo_planning"
    assert [task.execution_adapter for task in coding.tasks] == [
        "repo_planning",
        "codex_isolated_edit",
        "dry_run",
        "review_gate",
        "review_gate",
        "dry_run",
    ]
    assert coding.tasks[1].depends_on_indexes == [0]
    assert coding.tasks[3].agent_id == "implementation_reviewer"
    assert coding.tasks[3].metadata()["review_role"] == "implementation_reviewer"
    assert coding.tasks[4].agent_id == "security_reviewer"
    assert coding.tasks[4].metadata()["blocks_apply_back"] is True
    assert coding.tasks[5].metadata()["requires_evidence_links"] == "objective,task,run,artifact,policy"
    assert coding.checkpoints[0].label == "Supervisor approval for reviewed coding workflow"
    assert coding.checkpoints[0].required is True
    assert coding.checkpoints[0].metadata["gate_id"] == "checkpoint_approved"
    assert research.checkpoints[0].label == "Supervisor approval for reviewed research workflow"
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


def test_pending_task_draft_survives_session_state_restart(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    first_state = ChatSessionState()

    draft = handle_chat_input("create dry run task", tmp_path, first_state)

    assert draft["kind"] == "task_draft"
    assert first_state.session_id is not None
    store = SQLiteStore(tmp_path)
    persisted = store.get_session(first_state.session_id).metadata["pending_chat_action"]
    assert persisted["kind"] == "task_draft"

    resumed_state = ChatSessionState(session_id=first_state.session_id)
    confirmed = handle_chat_input("/confirm", tmp_path, resumed_state)

    assert confirmed["kind"] == "task_created"
    assert len(store.list_tasks()) == 1
    assert "pending_chat_action" not in store.get_session(first_state.session_id).metadata
    assert any("pending chat action restored: task_draft" in item for item in resumed_state.progress)


def test_pending_task_draft_is_visible_in_passive_operator_projections(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    state = ChatSessionState()

    draft = handle_chat_input("create dry run task", tmp_path, state)
    dashboard = build_operator_context(tmp_path, selected_session_id=state.session_id)
    right_pane = build_right_pane_cockpit_model(dashboard, {}, "", "dashboard")

    assert draft["kind"] == "task_draft"
    assert SQLiteStore(tmp_path).list_tasks() == []
    pending = dashboard["active_session"]["pending_action"]
    assert pending["kind"] == "task_draft"
    assert pending["requires_confirmation"] is True
    assert pending["process_started"] is False
    assert pending["adapter_dispatch_started"] is False
    assert pending["permission_granting"] is False
    assert dashboard["summary"]["pending_chat_actions"] == 1
    assert dashboard["session_pane"]["counts"]["pending_chat_actions"] == 1
    assert dashboard["session_pane"]["sessions"][0]["pending_action"]["kind"] == "task_draft"
    assert any("Pending action:" in row for row in right_pane["attention"])
    assert any("Next: /confirm or /decline" in row for row in right_pane["active_work"]["rows"])


def test_invalid_pending_chat_action_metadata_is_visible_and_cli_clear_is_metadata_only(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    session = store.create_session(
        title="Broken pending action",
        metadata={
            "pending_chat_action": {
                "schema_version": "harness.pending_chat_action/v1",
                "kind": "task_draft",
            }
        },
    )

    dashboard = build_operator_context(tmp_path, selected_session_id=session.id)
    right_pane = build_right_pane_cockpit_model(dashboard, {}, "", "dashboard")
    active_audit = dashboard["active_session"]["pending_action_audit"]

    assert dashboard["active_session"]["pending_action"] is None
    assert active_audit["status"] == "invalid"
    assert active_audit["recoverable"] is False
    assert active_audit["issues"][0]["code"] == "missing_task_draft"
    assert active_audit["cleanup_command"] == f"harness sessions clear-pending-action {session.id}"
    assert dashboard["summary"]["pending_chat_actions"] == 0
    assert dashboard["summary"]["invalid_pending_chat_actions"] == 1
    assert dashboard["session_pane"]["counts"]["invalid_pending_chat_actions"] == 1
    assert dashboard["live_activity"]["active_signal"] == "blocked"
    assert any("pending metadata needs cleanup" in row for row in right_pane["active_work"]["rows"])
    assert any("clear-pending-action" in row for row in right_pane["attention"])
    assert store.list_tasks() == []

    result = runner.invoke(
        app,
        ["sessions", "clear-pending-action", session.id, "--project", str(tmp_path), "--output", "json"],
    )
    payload = json.loads(result.output)

    assert result.exit_code == 0
    assert payload["cleared"] is True
    assert payload["mutation_scope"] == "session_metadata_only"
    assert payload["tasks_mutated"] is False
    assert payload["runs_mutated"] is False
    assert payload["approvals_mutated"] is False
    assert payload["artifacts_mutated"] is False
    assert "pending_chat_action" not in SQLiteStore(tmp_path).get_session(session.id).metadata
    assert SQLiteStore(tmp_path).list_tasks() == []


def test_stale_session_active_run_reference_is_visible_without_repairing(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    session = store.create_session(title="Stale active run")
    missing_run_id = "run_missing_for_operator_projection"
    with store.connect() as conn:
        conn.execute("UPDATE sessions SET active_run_id = ? WHERE id = ?", (missing_run_id, session.id))

    dashboard = build_operator_context(tmp_path, selected_session_id=session.id)
    right_pane = build_right_pane_cockpit_model(dashboard, {}, "", "dashboard")
    active_reference = dashboard["active_session"]["active_run_reference"]

    assert active_reference["schema_version"] == "harness.session_active_run_reference/v1"
    assert active_reference["status"] == "stale"
    assert active_reference["stale"] is True
    assert active_reference["repairable"] is True
    assert active_reference["missing_run_id"] == missing_run_id
    assert active_reference["repair_scope"] == "session_active_run_pointer_only"
    assert active_reference["process_started"] is False
    assert active_reference["provider_called"] is False
    assert active_reference["network_called"] is False
    assert active_reference["filesystem_modified"] is False
    assert active_reference["permission_granting"] is False
    assert dashboard["summary"]["stale_active_run_refs"] == 1
    assert dashboard["session_pane"]["counts"]["stale_active_run_refs"] == 1
    assert dashboard["live_activity"]["counts"]["stale_active_run_refs"] == 1
    assert dashboard["live_activity"]["active_signal"] == "blocked"
    assert right_pane["summary"]["stale_active_run_refs"] == 1
    assert any("session active run needs cleanup" in row for row in right_pane["active_work"]["rows"])
    assert any("harness doctor --repair" in row for row in right_pane["attention"])
    assert SQLiteStore(tmp_path).get_session(session.id).active_run_id == missing_run_id
    assert not SQLiteStore(tmp_path).list_runs()
    assert not SQLiteStore(tmp_path).list_tasks()


def test_pending_orchestration_draft_survives_session_state_restart(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    first_state = ChatSessionState()
    draft = OrchestratedRunDraft(
        objective_title="Recovered orchestration",
        objective_description="Recover pending orchestration from session metadata.",
        orchestrator_id="coding_orchestrator",
        workbench_id="coding",
        tasks=[
            OrchestratedTaskDraft(
                title="Recovered dry run",
                description="Dry-run task for pending orchestration recovery.",
                agent_id="test_runner",
                workbench_id="coding",
                execution_adapter="dry_run",
                task_type="phase_1a_test",
            )
        ],
        checkpoints=[
            OrchestratedCheckpointDraft(
                label="Recovered supervisor checkpoint",
                reason="Recovered from session metadata.",
            )
        ],
        required_approvals=[],
    )

    response = _orchestration_draft_response(tmp_path, first_state, draft)
    assert response["kind"] == "orchestration_draft"
    assert first_state.session_id is not None
    store = SQLiteStore(tmp_path)
    assert store.get_session(first_state.session_id).metadata["pending_chat_action"]["kind"] == "orchestration_draft"

    resumed_state = ChatSessionState(session_id=first_state.session_id)
    confirmed = handle_chat_input("/confirm", tmp_path, resumed_state)

    objectives = store.list_objectives()
    assert confirmed["kind"] == "orchestration_result"
    assert len(objectives) == 1
    assert len(store.list_tasks(objective_id=objectives[0].id)) == 1
    checkpoints = list_objective_checkpoints(tmp_path, objectives[0].id)
    assert len(checkpoints.checkpoints) == 1
    assert checkpoints.checkpoints[0].status == "approved"
    assert "pending_chat_action" not in store.get_session(first_state.session_id).metadata


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


def test_chat_domain_python_script_does_not_run_as_self_managed_action(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0

    response = handle_chat_input("create a python script for the black scholes pricing", tmp_path, ChatSessionState())

    assert response["kind"] == "orchestration_draft"
    assert response["ok"] is True
    assert not (tmp_path / "black_scholes_pricing.py").exists()
    rendered = "\n".join(response["lines"])
    assert "Create a bounded coding workflow" in rendered
    assert "Required approvals" in rendered


def test_coding_fix_template_creates_reviewed_task_graph_and_prepares_scoped_approval(tmp_path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0

    monkeypatch.setattr("harness.execution.CodexCliBackend", FakeCodexBackend)
    state = ChatSessionState(codex_like_mode=True)
    draft = handle_chat_input("fix the failing test with codex", tmp_path, state)
    assert draft["kind"] == "orchestration_draft"
    assert draft["draft"]["checkpoints"][0]["required"] is True
    rendered_draft = "\n".join(draft["lines"])
    assert "Supervisor checkpoints:" in rendered_draft
    assert "Supervisor approval for reviewed coding workflow" in rendered_draft
    assert [task["execution_adapter"] for task in draft["draft"]["tasks"]] == [
        "repo_planning",
        "codex_isolated_edit",
        "dry_run",
        "review_gate",
        "review_gate",
        "dry_run",
    ]
    assert [task["agent_id"] for task in draft["draft"]["tasks"]] == [
        "repo_inspector",
        "code_editor",
        "test_runner",
        "implementation_reviewer",
        "security_reviewer",
        "coding_orchestrator",
    ]
    for task in draft["draft"]["tasks"]:
        allocation = task["metadata"]["delegate_allocation"]
        selection = task["agent_selection"]
        assert selection["schema_version"] == "harness.workflow_agent_selection/v1"
        assert task["metadata"]["agent_selection_source"] == "delegate_allocation"
        assert task["metadata"]["delegate_allocation_schema_version"] == "harness.delegate_allocation/v1"
        assert allocation["schema_version"] == "harness.delegate_allocation/v1"
        assert allocation["selection_source"] == "delegate_allocation"
        assert allocation["selected_agent_id"] == task["agent_id"]
        assert allocation["requirements"]["source"] == "workflow_template"
        assert allocation["requirements"]["schema_version"] == "harness.workflow_agent_selection/v1"
        assert allocation["requirements"]["required_kind"] == selection["required_kind"]
        assert allocation["requirements"]["required_tool_policy_id"] == selection["required_tool_policy_id"]
        assert allocation["requirements"]["required_outputs"] == selection["required_outputs"]
        assert allocation["requirements"]["required_tags"] == selection["required_tags"]
        assert allocation["eligible_count"] >= 1
        assert allocation["selected_bid"]["bid_terms"]["runtime_authority_granted"] is False
        assert allocation["selected_bid"]["bid_terms"]["permission_granting"] is False
        assert allocation["safety"]["read_only"] is True
        assert allocation["safety"]["metadata_only"] is True
        assert allocation["safety"]["agent_execution_started"] is False
        assert allocation["safety"]["tool_execution_started"] is False
        assert allocation["safety"]["permission_granting"] is False

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
        "review_gate",
        "review_gate",
        "dry_run",
    ]
    assert [task.agent_id for task in tasks] == [
        "repo_inspector",
        "code_editor",
        "test_runner",
        "implementation_reviewer",
        "security_reviewer",
        "coding_orchestrator",
    ]
    for task in tasks:
        allocation = task.metadata["delegate_allocation"]
        assert task.metadata["agent_selection_source"] == "delegate_allocation"
        assert allocation["schema_version"] == "harness.delegate_allocation/v1"
        assert allocation["selected_agent_id"] == task.agent_id
        assert allocation["safety"]["provider_called"] is False
        assert allocation["safety"]["network_called"] is False
        assert allocation["safety"]["agent_execution_started"] is False
        assert allocation["safety"]["permission_granting"] is False
    assert tasks[1].depends_on == [tasks[0].id]
    assert tasks[3].depends_on == [tasks[2].id]
    assert tasks[3].metadata["review_role"] == "implementation_reviewer"
    assert tasks[4].depends_on == [tasks[3].id]
    assert tasks[4].metadata["review_role"] == "security_reviewer"
    assert tasks[4].metadata["blocks_apply_back"] is True
    assert set(tasks[5].depends_on) == {tasks[0].id, tasks[1].id, tasks[2].id, tasks[3].id, tasks[4].id}
    checkpoints = list_objective_checkpoints(tmp_path, state.latest_objective_id)
    assert len(checkpoints.checkpoints) == 1
    assert checkpoints.checkpoints[0].label == "Supervisor approval for reviewed coding workflow"
    assert checkpoints.checkpoints[0].status == "approved"
    assert checkpoints.checkpoints[0].required is True
    gate = evaluate_objective_checkpoint_gate(tmp_path, state.latest_objective_id)
    assert gate.ok is True
    assert gate.required_checkpoint_count == 1
    assert response["checkpoint_evidence"][0]["checkpoint_id"] == checkpoints.checkpoints[0].checkpoint_id
    assert state.latest_orchestration["checkpoints"][0]["status"] == "approved"
    rendered = "\n".join(response["lines"])
    assert "Approved supervisor checkpoints: 1" in rendered
    assert "Prepared scoped hosted-provider Codex approval" in rendered
    assert f"objectives={state.latest_objective_id}" in rendered
    approval = store.project_root / ".harness" / "approvals.yaml"
    assert approval.exists()


def test_foreground_orchestration_confirmation_replay_reuses_graph(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    state = ChatSessionState()
    draft = OrchestratedRunDraft(
        objective_title="Replay-safe graph",
        objective_description="Exercise orchestration idempotency without hosted providers.",
        orchestrator_id="coding_orchestrator",
        workbench_id="coding",
        tasks=[
            OrchestratedTaskDraft(
                title="Replay dry run",
                description="Dry-run task for replay safety.",
                agent_id="test_runner",
                workbench_id="coding",
                execution_adapter="dry_run",
                task_type="phase_1a_test",
            )
        ],
        checkpoints=[
            OrchestratedCheckpointDraft(
                label="Supervisor replay checkpoint",
                reason="Confirmed replay-safe foreground graph.",
            )
        ],
        required_approvals=[],
    )

    first = _create_and_run_orchestration(tmp_path, state, draft)
    second = _create_and_run_orchestration(tmp_path, state, draft)

    assert first["kind"] == "orchestration_result"
    assert second["kind"] == "orchestration_result"
    store = SQLiteStore(tmp_path)
    objectives = store.list_objectives()
    assert len(objectives) == 1
    assert state.latest_objective_id == objectives[0].id
    tasks = store.list_tasks(objective_id=objectives[0].id)
    assert len(tasks) == 1
    checkpoints = list_objective_checkpoints(tmp_path, objectives[0].id)
    assert len(checkpoints.checkpoints) == 1
    assert checkpoints.checkpoints[0].status == "approved"
    assert checkpoints.checkpoints[0].event_count == 2
    assert second["idempotency"]["objective_reused"] is True
    assert "Idempotency: reused existing objective graph" in "\n".join(second["lines"])


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
