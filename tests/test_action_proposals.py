from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from harness.action_proposals import contract_from_tool_request
from harness.approvals import ApprovalStore
from harness.backends.codex_cli import CodexCliBackend, CodexRunResult, NETWORK_NOT_ENFORCEABLE
from harness.chat import ChatSessionState, handle_chat_input, run_autonomous_read_loop
from harness.chat_model import ChatContext, ChatDelta, ChatMessage, ChatResponse
from harness.chat_tools import ChatToolRequest
from harness.cli.main import app
from harness.execution import list_execution_adapter_descriptors
from harness.memory.sqlite_store import SQLiteStore
from harness.models import BackendCapabilities, BackendStatus
from harness.autonomy import AutonomyBudget, get_builtin_autonomy_policy
from typer.testing import CliRunner


runner = CliRunner()


class SideEffectRequestModel:
    def __init__(self, request_json: str) -> None:
        self.request_json = request_json

    def stream(self, messages: list[ChatMessage], context: ChatContext) -> Iterator[ChatDelta]:
        yield ChatDelta(content=self.complete(messages, context).content)

    def complete(self, messages: list[ChatMessage], context: ChatContext) -> ChatResponse:
        return ChatResponse(content=self.request_json)


class SequenceModel:
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.calls = 0
        self.contexts: list[ChatContext] = []

    def stream(self, messages: list[ChatMessage], context: ChatContext) -> Iterator[ChatDelta]:
        yield ChatDelta(content=self.complete(messages, context).content)

    def complete(self, messages: list[ChatMessage], context: ChatContext) -> ChatResponse:
        self.contexts.append(context)
        index = min(self.calls, len(self.responses) - 1)
        self.calls += 1
        return ChatResponse(content=self.responses[index])


class FakeCodexBackend(CodexCliBackend):
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


def test_action_proposal_validates_known_adapter() -> None:
    contract = contract_from_tool_request(
        ChatToolRequest(
            "harness.tool_request/v1",
            "create_task",
            {"title": "Dry run task", "execution_adapter": "dry_run", "task_type": "phase_1a_test"},
        )
    )

    assert contract.tool == "create_task"
    assert contract.normalized_arguments["execution_adapter"] == "dry_run"
    assert contract.normalized_arguments["task_type"] == "phase_1a_test"
    assert contract.requires_confirmation is True


def test_action_proposal_rejects_unknown_adapter() -> None:
    with pytest.raises(ValueError, match="Unknown execution adapter"):
        contract_from_tool_request(
            ChatToolRequest(
                "harness.tool_request/v1",
                "create_task",
                {"title": "Bad task", "execution_adapter": "shell"},
            )
        )


def test_action_proposal_cannot_broaden_permissions() -> None:
    with pytest.raises(ValueError, match="not supported"):
        contract_from_tool_request(
            ChatToolRequest(
                "harness.tool_request/v1",
                "create_task",
                {"title": "Bad type", "execution_adapter": "dry_run", "task_type": "codex_code_edit"},
            )
        )


def test_action_contract_recomputes_required_approvals() -> None:
    contract = contract_from_tool_request(
        ChatToolRequest(
            "harness.tool_request/v1",
            "edit_isolated",
            {"goal": "fix chat tools", "required_approvals": []},
        )
    )

    assert "hosted_provider_codex" in contract.required_approvals
    assert "apply_back_separate" in contract.required_confirmations


def test_side_effect_tool_request_becomes_action_contract_without_execution(tmp_path) -> None:
    model = SideEffectRequestModel(
        '{"type":"harness.tool_request/v1","tool":"create_objective","arguments":{"title":"Improve chat"}}'
    )
    state = ChatSessionState()

    response = handle_chat_input("create an objective for this", tmp_path, state, chat_model=model)

    assert response["kind"] == "action_contract"
    assert response["contract"]["tool"] == "create_objective"
    assert state.pending_action_contract is not None
    assert not (tmp_path / ".harness").exists()


def test_confirmed_action_contract_creates_objective_record(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    model = SideEffectRequestModel(
        '{"type":"harness.tool_request/v1","tool":"create_objective","arguments":{"title":"Improve chat"}}'
    )
    state = ChatSessionState()

    draft = handle_chat_input("create an objective for this", tmp_path, state, chat_model=model)
    response = handle_chat_input("yes", tmp_path, state)

    assert draft["kind"] == "action_contract"
    assert response["kind"] == "action_contract_executed"
    store = SQLiteStore(tmp_path)
    objectives = store.list_objectives()
    assert [objective.title for objective in objectives] == ["Improve chat"]


def test_confirmed_action_contract_creates_task_record(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    model = SideEffectRequestModel(
        '{"type":"harness.tool_request/v1","tool":"create_task","arguments":{"title":"Dry run","execution_adapter":"dry_run","task_type":"phase_1a_test"}}'
    )
    state = ChatSessionState()

    draft = handle_chat_input("please add a queue item for validation", tmp_path, state, chat_model=model)
    response = handle_chat_input("yes", tmp_path, state)

    assert draft["kind"] == "action_contract"
    assert response["kind"] == "action_contract_executed"
    tasks = SQLiteStore(tmp_path).list_tasks()
    assert len(tasks) == 1
    assert tasks[0].title == "Dry run"
    assert tasks[0].metadata["execution_adapter"] == "dry_run"


def test_manual_profile_preserves_pending_contract_confirmation(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    model = SideEffectRequestModel(
        '{"type":"harness.tool_request/v1","tool":"create_objective","arguments":{"title":"Manual objective"}}'
    )
    state = ChatSessionState(autonomy_profile_id="manual")

    response = handle_chat_input("create an objective for this", tmp_path, state, chat_model=model)

    assert response["kind"] == "action_contract"
    assert response["autonomy_decision"]["status"] == "approval_required"
    assert state.pending_action_contract is not None
    assert SQLiteStore(tmp_path).list_objectives() == []


def test_safe_local_auto_executes_allowed_control_plane_contract(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    model = SideEffectRequestModel(
        '{"type":"harness.tool_request/v1","tool":"create_objective","arguments":{"title":"Autonomous objective"}}'
    )
    state = ChatSessionState(autonomy_profile_id="safe-local")

    response = handle_chat_input("create an objective for this", tmp_path, state, chat_model=model)

    assert response["kind"] == "action_contract_executed"
    assert response["autonomy_decision"]["status"] == "auto_allowed"
    assert response["autonomous_approval"]["policy_id"] == "safe-local"
    assert state.pending_action_contract is None
    objectives = SQLiteStore(tmp_path).list_objectives()
    assert [objective.title for objective in objectives] == ["Autonomous objective"]


def test_denied_contract_is_not_executed_under_safe_local(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    model = SideEffectRequestModel(
        '{"type":"harness.tool_request/v1","tool":"edit_isolated","arguments":{"goal":"fix chat tool routing"}}'
    )
    state = ChatSessionState(autonomy_profile_id="safe-local")

    response = handle_chat_input("please improve this", tmp_path, state, chat_model=model)

    assert response["kind"] == "action_contract_denied"
    assert response["autonomy_decision"]["status"] == "denied"
    assert state.pending_action_contract is None
    assert SQLiteStore(tmp_path).list_objectives() == []
    decision_path = tmp_path / ".harness" / "autonomy" / "decisions.jsonl"
    decisions = [json.loads(line) for line in decision_path.read_text(encoding="utf-8").splitlines()]
    assert decisions[0]["status"] == "denied"
    assert decisions[0]["tool_name"] == "edit_isolated"


def test_approval_required_contract_remains_pending_under_supervised_codex(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    model = SideEffectRequestModel(
        '{"type":"harness.tool_request/v1","tool":"edit_isolated","arguments":{"goal":"fix chat tool routing"}}'
    )
    state = ChatSessionState(autonomy_profile_id="supervised-codex")

    response = handle_chat_input("please improve this", tmp_path, state, chat_model=model)

    assert response["kind"] == "action_contract"
    assert response["autonomy_decision"]["status"] == "approval_required"
    assert state.pending_action_contract is not None
    assert SQLiteStore(tmp_path).list_objectives() == []


def test_autonomous_approval_record_is_written(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    model = SideEffectRequestModel(
        '{"type":"harness.tool_request/v1","tool":"create_task","arguments":{"title":"Dry run","execution_adapter":"dry_run","task_type":"phase_1a_test"}}'
    )
    state = ChatSessionState(autonomy_profile_id="safe-local")

    response = handle_chat_input("please add a queue item for validation", tmp_path, state, chat_model=model)

    assert response["kind"] == "action_contract_executed"
    assert response["autonomy_decision_evidence"]["status"] == "auto_allowed"
    approval_path = tmp_path / ".harness" / "autonomy" / "approvals.jsonl"
    records = [json.loads(line) for line in approval_path.read_text(encoding="utf-8").splitlines()]
    assert len(records) == 1
    assert records[0]["schema_version"] == "harness.autonomous_approval/v1"
    assert records[0]["policy_id"] == "safe-local"
    assert records[0]["tool_name"] == "create_task"
    assert records[0]["task_type"] == "phase_1a_test"
    outcomes = [
        json.loads(line)
        for line in (tmp_path / ".harness" / "autonomy" / "outcomes.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert outcomes[0]["schema_version"] == "harness.autonomous_outcome/v1"
    assert outcomes[0]["task_id"] == response["task"]["id"]


def test_safe_local_auto_creates_task_graph(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    request = {
        "type": "harness.tool_request/v1",
        "tool": "create_task_graph",
        "arguments": {
            "goal": "validate local autonomy",
            "tasks": [
                {
                    "title": "Dry run one",
                    "execution_adapter": "dry_run",
                    "task_type": "phase_1a_test",
                },
                {
                    "title": "Dry run two",
                    "execution_adapter": "dry_run",
                    "task_type": "phase_1a_test",
                    "depends_on_indexes": [0],
                },
            ],
        },
    }
    state = ChatSessionState(autonomy_profile_id="safe-local")

    response = handle_chat_input("turn this into a workflow", tmp_path, state, chat_model=SideEffectRequestModel(json.dumps(request)))

    assert response["kind"] == "action_contract_executed"
    assert response["autonomy_decision"]["status"] == "auto_allowed"
    assert len(response["tasks"]) == 2
    tasks = SQLiteStore(tmp_path).list_tasks(objective_id=response["objective"]["id"])
    assert [task.title for task in tasks] == ["Dry run one", "Dry run two"]
    assert tasks[1].depends_on == [tasks[0].id]


def test_idempotent_task_creation_not_duplicated(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    model = SideEffectRequestModel(
        '{"type":"harness.tool_request/v1","tool":"create_task","arguments":{"title":"Dry run","execution_adapter":"dry_run","task_type":"phase_1a_test"}}'
    )
    first_state = ChatSessionState(autonomy_profile_id="safe-local")
    second_state = ChatSessionState(autonomy_profile_id="safe-local")

    first = handle_chat_input("please add a queue item for validation", tmp_path, first_state, chat_model=model)
    second = handle_chat_input("please add a queue item for validation", tmp_path, second_state, chat_model=model)

    assert first["kind"] == "action_contract_executed"
    assert second["kind"] == "action_contract_executed"
    tasks = SQLiteStore(tmp_path).list_tasks()
    assert len(tasks) == 1
    assert first["task"]["id"] == second["task"]["id"] == tasks[0].id
    assert tasks[0].idempotency_key is not None


def test_memory_write_requires_scope_and_hash(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    model = SideEffectRequestModel(
        '{"type":"harness.tool_request/v1","tool":"remember","arguments":{"summary":"Prefer local autonomy evidence."}}'
    )
    state = ChatSessionState(autonomy_profile_id="safe-local")

    response = handle_chat_input("remember this", tmp_path, state, chat_model=model)

    assert response["kind"] == "memory_saved"
    assert response["autonomy_decision"]["status"] == "auto_allowed"
    memory = response["memory"]
    assert memory["scope_type"] == "project"
    assert memory["scope_id"] == str(tmp_path)
    assert memory["source_id"] == memory["id"]
    assert memory["sha256"]
    assert memory["redaction_state"] == "not_required"
    assert memory["lineage"]["permission_granting"] is False
    outcomes = [
        json.loads(line)
        for line in (tmp_path / ".harness" / "autonomy" / "outcomes.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert outcomes[0]["memory_id"] == memory["id"]


def test_active_repo_mutation_still_blocked_under_safe_local(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    state = ChatSessionState(autonomy_profile_id="safe-local")
    model = SideEffectRequestModel(
        '{"type":"harness.tool_request/v1","tool":"apply_back","arguments":{"goal":"apply active repo changes"}}'
    )

    response = handle_chat_input("perform the requested operation", tmp_path, state, chat_model=model)

    assert response["kind"] == "action_contract_denied"
    assert response["autonomy_decision"]["status"] == "denied"
    assert state.pending_action_contract is None


def test_confirmed_edit_isolated_contract_prepares_approval_before_orchestration(tmp_path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0

    monkeypatch.setattr("harness.execution.CodexCliBackend", FakeCodexBackend)
    model = SideEffectRequestModel(
        '{"type":"harness.tool_request/v1","tool":"edit_isolated","arguments":{"goal":"fix chat tool routing"}}'
    )
    state = ChatSessionState()

    draft = handle_chat_input("please improve this", tmp_path, state, chat_model=model)
    response = handle_chat_input("yes", tmp_path, state)

    assert draft["kind"] == "action_contract"
    assert response["kind"] == "orchestration_result"
    assert response["contract"]["tool"] == "edit_isolated"
    assert "Prepared required hosted-provider Codex approval" in "\n".join(response["lines"])
    tasks = SQLiteStore(tmp_path).list_tasks(objective_id=state.latest_objective_id)
    assert [task.metadata["execution_adapter"] for task in tasks] == [
        "repo_planning",
        "codex_isolated_edit",
        "dry_run",
        "dry_run",
        "dry_run",
        "dry_run",
    ]
    assert tasks[3].agent_id == "implementation_reviewer"
    assert tasks[4].agent_id == "security_reviewer"
    assert tasks[0].status.value != "waiting_approval"


def test_confirmed_dispatch_contract_executes_latest_lease(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    task = store.create_task(
        title="Dry run",
        metadata={"execution_adapter": "dry_run", "task_type": "phase_1a_test"},
    )
    leased = store.daemon_run_once(owner="test", pid=None)
    state = ChatSessionState(latest_lease_id=leased.lease.id)
    model = SideEffectRequestModel(
        '{"type":"harness.tool_request/v1","tool":"dispatch_registered_adapter","arguments":{}}'
    )

    draft = handle_chat_input("continue the lease", tmp_path, state, chat_model=model)
    response = handle_chat_input("yes", tmp_path, state)

    assert draft["kind"] == "action_contract"
    assert response["kind"] == "execute_result"
    assert response["contract"]["tool"] == "dispatch_registered_adapter"
    assert SQLiteStore(tmp_path).get_task(task.id).status.value == "succeeded"


def test_adapter_descriptors_expose_autonomy_metadata() -> None:
    descriptors = {descriptor.id: descriptor for descriptor in list_execution_adapter_descriptors()}

    assert descriptors["dry_run"].autonomy_default == "auto_allowed"
    assert "safe-local" in descriptors["dry_run"].required_autonomy_scopes
    assert descriptors["dry_run"].terminal_evidence_required == ["task", "lease", "run", "manifest", "policy_sha256"]
    assert descriptors["repo_planning"].autonomy_default == "approval_required"
    assert descriptors["repo_planning"].required_autonomy_scopes == ["supervised-codex"]
    assert descriptors["codex_isolated_edit"].sandbox_profile_id == "isolated_workspace_codex"
    assert "diff_artifact" in descriptors["codex_isolated_edit"].terminal_evidence_required


def test_safe_local_auto_dispatches_dry_run_adapter_contract(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    task = store.create_task(
        title="Dry run",
        metadata={"execution_adapter": "dry_run", "task_type": "phase_1a_test"},
    )
    leased = store.daemon_run_once(owner="test", pid=None)
    state = ChatSessionState(latest_lease_id=leased.lease.id, autonomy_profile_id="safe-local")
    model = SideEffectRequestModel(
        '{"type":"harness.tool_request/v1","tool":"dispatch_registered_adapter","arguments":{}}'
    )

    response = handle_chat_input("continue the lease", tmp_path, state, chat_model=model)

    assert response["kind"] == "execute_result"
    assert response["autonomy_decision"]["status"] == "auto_allowed"
    assert response["autonomous_approval"]["adapter_id"] == "dry_run"
    assert state.pending_action_contract is None
    assert SQLiteStore(tmp_path).get_task(task.id).status.value == "succeeded"
    assert response["autonomous_outcome"]["adapter_id"] == "dry_run"
    assert response["autonomous_outcome"]["run_id"] == response["result"]["run"]["id"]
    manifest = SQLiteStore(tmp_path).build_run_manifest(response["result"]["run"]["id"])
    assert manifest.autonomy_decision_id == response["autonomy_decision_evidence"]["record_id"]
    assert manifest.autonomous_approval_id == response["autonomous_approval"]["id"]
    assert manifest.autonomous_outcome_id == response["autonomous_outcome"]["record_id"]


def test_autonomous_dispatch_denies_unknown_adapter(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    state = ChatSessionState(autonomy_profile_id="supervised-codex")
    model = SideEffectRequestModel(
        '{"type":"harness.tool_request/v1","tool":"dispatch_registered_adapter","arguments":{"adapter_id":"unknown_adapter","task_type":"phase_1a_test"}}'
    )

    response = handle_chat_input("continue now", tmp_path, state, chat_model=model)

    assert response["kind"] == "action_contract_denied"
    assert response["autonomy_decision"]["status"] == "denied"
    assert "adapter is not registered: unknown_adapter" in response["autonomy_decision"]["reasons"]
    assert SQLiteStore(tmp_path).list_runs() == []


def test_repo_planning_autonomous_dispatch_requires_hosted_approval(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    state = ChatSessionState(autonomy_profile_id="supervised-codex")
    model = SideEffectRequestModel(
        '{"type":"harness.tool_request/v1","tool":"dispatch_registered_adapter","arguments":{"adapter_id":"repo_planning","task_type":"repo_planning"}}'
    )

    response = handle_chat_input("continue now", tmp_path, state, chat_model=model)

    assert response["kind"] == "action_contract"
    assert response["autonomy_decision"]["status"] == "approval_required"
    assert state.pending_action_contract is not None
    assert SQLiteStore(tmp_path).list_runs() == []


def test_repo_planning_autonomous_dispatch_rejects_legacy_hosted_approval(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    objective = store.create_objective("Planning objective")
    ApprovalStore(tmp_path).add(
        backend="codex_cli",
        data_boundary="hosted_provider",
        task_types=["repo_planning"],
        duration_hours=8,
        reason="legacy hosted approval without autonomy scope",
    )
    task = store.create_task(
        title="Plan",
        objective_id=objective.id,
        metadata={"execution_adapter": "repo_planning", "task_type": "repo_planning"},
    )
    leased = store.daemon_run_once(owner="test", pid=None)
    state = ChatSessionState(latest_lease_id=leased.lease.id, autonomy_profile_id="supervised-codex")
    model = SideEffectRequestModel(
        '{"type":"harness.tool_request/v1","tool":"dispatch_registered_adapter","arguments":{}}'
    )

    response = handle_chat_input("continue now", tmp_path, state, chat_model=model)

    assert response["kind"] == "action_contract"
    assert response["autonomy_decision"]["status"] == "approval_required"
    assert state.pending_action_contract is not None
    assert SQLiteStore(tmp_path).get_task(task.id).status.value == "leased"
    assert SQLiteStore(tmp_path).list_runs() == []


def test_repo_planning_autonomous_dispatch_runs_with_scoped_approval(tmp_path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    monkeypatch.setattr("harness.execution.CodexCliBackend", FakeCodexBackend)
    store = SQLiteStore(tmp_path)
    objective = store.create_objective("Planning objective")
    ApprovalStore(tmp_path).add(
        backend="codex_cli",
        data_boundary="hosted_provider",
        task_types=["repo_planning"],
        duration_hours=8,
        allowed_adapters=["repo_planning"],
        allowed_objective_ids=[objective.id],
        autonomy_scope="supervised-codex",
        reason="test scoped autonomy approval",
    )
    task = store.create_task(
        title="Plan",
        objective_id=objective.id,
        metadata={"execution_adapter": "repo_planning", "task_type": "repo_planning"},
    )
    leased = store.daemon_run_once(owner="test", pid=None)
    state = ChatSessionState(latest_lease_id=leased.lease.id, autonomy_profile_id="supervised-codex")
    model = SideEffectRequestModel(
        '{"type":"harness.tool_request/v1","tool":"dispatch_registered_adapter","arguments":{}}'
    )

    response = handle_chat_input("continue now", tmp_path, state, chat_model=model)

    assert response["kind"] == "execute_result"
    assert response["autonomy_decision"]["status"] == "auto_allowed"
    assert response["autonomous_approval"]["adapter_id"] == "repo_planning"
    assert SQLiteStore(tmp_path).get_task(task.id).status.value == "succeeded"
    assert response["result"]["approval_id"]


def test_adapter_breaker_blocks_autonomous_dispatch(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    task = store.create_task(
        title="Dry run",
        metadata={"execution_adapter": "dry_run", "task_type": "phase_1a_test"},
    )
    leased = store.daemon_run_once(owner="test", pid=None)
    daemon = store.ensure_daemon(owner="test")
    for index in range(3):
        store.record_daemon_event(
            daemon.id,
            event_type="execution_adapter_rejected",
            message="Adapter failed.",
            metadata={
                "adapter_id": "dry_run",
                "reason_code": "adapter_execution_failed",
                "error": f"failure {index}",
            },
        )
    state = ChatSessionState(latest_lease_id=leased.lease.id, autonomy_profile_id="safe-local")
    model = SideEffectRequestModel(
        '{"type":"harness.tool_request/v1","tool":"dispatch_registered_adapter","arguments":{}}'
    )

    response = handle_chat_input("continue the lease", tmp_path, state, chat_model=model)

    assert response["kind"] == "action_contract_denied"
    assert response["autonomy_decision"]["status"] == "denied"
    assert response["autonomy_decision"]["reasons"] == ["adapter breaker is open", "adapter autonomy default is auto_allowed: dry_run"]
    assert SQLiteStore(tmp_path).get_task(task.id).status.value == "leased"


def test_confirmed_run_tests_contract_invokes_test_runner(tmp_path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    calls = []

    class FakeDockerTestRunner:
        def __init__(self, project_root, cfg, store, approval_provider):
            self.project_root = project_root
            self.approval_provider = approval_provider

        def run(self, command):
            calls.append(command)
            decision = self.approval_provider.decide("pytest")
            return {
                "run_id": "run_test123",
                "status": "tests_passed",
                "approval_decision": decision.decision,
                "artifacts": {"final_report": "report.md"},
            }

    monkeypatch.setattr("harness.chat.DockerTestRunner", FakeDockerTestRunner)
    model = SideEffectRequestModel(
        '{"type":"harness.tool_request/v1","tool":"run_tests","arguments":{"suggested_command":"pytest tests/test_chat_tools.py"}}'
    )
    state = ChatSessionState()

    draft = handle_chat_input("run the focused tests", tmp_path, state, chat_model=model)
    response = handle_chat_input("yes", tmp_path, state)

    assert draft["kind"] == "action_contract"
    assert response["kind"] == "action_contract_executed"
    assert response["test_result"]["status"] == "tests_passed"
    assert calls == [["pytest", "tests/test_chat_tools.py"]]


def test_act_mode_allows_readonly_tool_loop(tmp_path) -> None:
    (tmp_path / "README.md").write_text("# Demo\n\nHarness project.\n", encoding="utf-8")
    model = SequenceModel(
        [
            '{"type":"harness.tool_request/v1","tool":"read_file","arguments":{"path":"README.md"}}',
            "README says this is a Harness project.",
        ]
    )

    response = handle_chat_input("/act explain the readme", tmp_path, ChatSessionState(), chat_model=model)

    assert response["kind"] == "llm_chat"
    assert response["mode"] == "act"
    assert response["tool_results"] == [{"tool": "read_file", "ok": True, "error_type": None}]
    assert model.contexts[0].mode == "act"
    assert model.calls == 2


def test_act_mode_can_request_side_effect_harness_tools_without_execution(tmp_path) -> None:
    model = SideEffectRequestModel(
        '{"type":"harness.tool_request/v1","tool":"edit_isolated","arguments":{"goal":"fix failing test"}}'
    )
    state = ChatSessionState()

    response = handle_chat_input("/act fix the failing test", tmp_path, state, chat_model=model)

    assert response["kind"] == "action_contract"
    assert response["contract"]["tool"] == "edit_isolated"
    assert state.pending_action_contract is not None
    assert not (tmp_path / ".harness").exists()


def test_autonomous_read_loop_uses_read_tools_until_answer(tmp_path) -> None:
    (tmp_path / "README.md").write_text("# Demo Project\n", encoding="utf-8")
    model = SequenceModel(
        [
            '{"type":"harness.tool_request/v1","tool":"read_file","arguments":{"path":"README.md"}}',
            "Evidence from README: Demo Project.",
        ]
    )

    result = run_autonomous_read_loop("summarize this repo", tmp_path, chat_model=model)

    assert result["schema_version"] == "harness.autonomous_read_loop/v1"
    assert result["ok"] is True
    assert result["stop_reason"] == "final_answer"
    assert result["final_answer"] == "Evidence from README: Demo Project."
    assert result["tool_results"] == [{"tool": "read_file", "ok": True, "error_type": None}]
    assert len(model.contexts) == 2
    assert model.contexts[0].mode == "act"


def test_autonomous_read_loop_stops_on_tool_budget(tmp_path, monkeypatch) -> None:
    (tmp_path / "README.md").write_text("# Demo Project\n", encoding="utf-8")
    policy = get_builtin_autonomy_policy("safe-local").model_copy(
        update={"budget": AutonomyBudget(max_model_turns=4, max_tool_calls=1, max_consecutive_failures=2)}
    )
    monkeypatch.setattr("harness.chat.get_builtin_autonomy_policy", lambda _profile: policy)
    model = SequenceModel(
        [
            '{"type":"harness.tool_request/v1","tool":"read_file","arguments":{"path":"README.md"}}',
            '{"type":"harness.tool_request/v1","tool":"repo_tree","arguments":{}}',
        ]
    )

    result = run_autonomous_read_loop("inspect twice", tmp_path, chat_model=model)

    assert result["ok"] is True
    assert result["stop_reason"] == "tool_budget_exhausted"
    assert result["tool_calls"] == 1


def test_autonomous_read_loop_rejects_side_effect_tool(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    model = SequenceModel(
        ['{"type":"harness.tool_request/v1","tool":"create_objective","arguments":{"title":"Nope"}}']
    )

    result = run_autonomous_read_loop("create an objective", tmp_path, chat_model=model)

    assert result["ok"] is False
    assert result["stop_reason"] == "side_effect_tool_rejected"
    assert result["tool_results"] == [{"tool": "create_objective", "ok": False, "error_type": "action_contract_required"}]
    assert SQLiteStore(tmp_path).list_objectives() == []


def test_autonomous_act_loop_can_create_and_run_local_task_graph(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    model = SequenceModel(
        [
            json.dumps(
                {
                    "type": "harness.tool_request/v1",
                    "tool": "create_task_graph",
                    "arguments": {
                        "goal": "local act graph",
                        "tasks": [
                            {
                                "title": "Act dry run",
                                "execution_adapter": "dry_run",
                                "task_type": "phase_1a_test",
                            }
                        ],
                    },
                }
            ),
            "Created the objective, ran the local task, and collected evidence.",
        ]
    )

    result = run_autonomous_read_loop(
        "create and run a local task graph",
        tmp_path,
        chat_model=model,
        allow_action_contracts=True,
        auto_run_created_objective=True,
    )

    assert result["ok"] is True
    assert result["stop_reason"] == "final_answer"
    assert [item["tool"] for item in result["tool_results"]] == [
        "create_task_graph",
        "create_task_graph",
        "objectives.run",
    ]
    assert result["tool_results"][1]["kind"] == "action_contract_executed"
    assert result["tool_results"][2]["stop_reason"] == "objective_succeeded"
    store = SQLiteStore(tmp_path)
    tasks = store.list_tasks()
    assert len(tasks) == 1
    assert tasks[0].status.value == "succeeded"
    assert len(store.list_runs()) == 1


def test_autonomous_read_loop_records_jsonl_evidence(tmp_path) -> None:
    (tmp_path / "README.md").write_text("# Demo Project\n", encoding="utf-8")
    model = SequenceModel(
        [
            '{"type":"harness.tool_request/v1","tool":"read_file","arguments":{"path":"README.md"}}',
            "Done from evidence.",
        ]
    )

    result = run_autonomous_read_loop("summarize", tmp_path, chat_model=model)

    events = [json.loads(line) for line in Path(result["evidence_path"]).read_text(encoding="utf-8").splitlines()]
    assert [event["event"] for event in events] == ["started", "model_turn", "tool_observation", "model_turn", "stopped"]
    assert events[2]["observation"]["tool"] == "read_file"
    assert events[-1]["stop_reason"] == "final_answer"


def test_autonomous_read_loop_handles_invalid_tool_request(tmp_path) -> None:
    model = SequenceModel(['{"type":"harness.tool_request/v1","tool":"missing_tool","arguments":{}}'])

    result = run_autonomous_read_loop("use an unknown tool", tmp_path, chat_model=model)

    assert result["ok"] is False
    assert result["stop_reason"] == "tool_failure_budget_exhausted"
    assert result["tool_results"][0] == {"tool": "missing_tool", "ok": False, "error_type": "unknown_tool"}


def test_test_slash_command_creates_sandbox_action_contract(tmp_path) -> None:
    state = ChatSessionState()

    response = handle_chat_input("/test pytest tests/test_chat_tools.py", tmp_path, state)

    assert response["kind"] == "action_contract"
    assert response["contract"]["tool"] == "run_tests"
    assert response["contract"]["normalized_arguments"]["suggested_command"] == "pytest tests/test_chat_tools.py"
    assert response["contract"]["required_approvals"] == ["docker_execution"]
    assert state.pending_action_contract is not None
    assert not (tmp_path / ".harness").exists()


def test_llm_can_select_known_workflow_template(tmp_path) -> None:
    contract = contract_from_tool_request(
        ChatToolRequest(
            "harness.tool_request/v1",
            "create_task_graph",
            {"goal": "fix the failing test", "template_id": "coding_fix"},
        ),
        project_root=tmp_path,
    )

    assert contract.tool == "create_task_graph"
    assert contract.normalized_arguments["template_id"] == "coding_fix"
    assert [task["execution_adapter"] for task in contract.normalized_arguments["tasks"]] == [
        "repo_planning",
        "codex_isolated_edit",
        "dry_run",
        "dry_run",
        "dry_run",
        "dry_run",
    ]
    assert contract.normalized_arguments["tasks"][3]["agent_id"] == "implementation_reviewer"
    assert contract.normalized_arguments["tasks"][3]["metadata"]["review_role"] == "implementation_reviewer"
    assert contract.normalized_arguments["tasks"][4]["agent_id"] == "security_reviewer"
    assert contract.normalized_arguments["tasks"][4]["metadata"]["blocks_apply_back"] is True
    assert contract.execution_plan[0] == {"step": "select_workflow_template", "template_id": "coding_fix"}


def test_unknown_workflow_template_rejected(tmp_path) -> None:
    with pytest.raises(ValueError, match="Unknown workflow template intent"):
        contract_from_tool_request(
            ChatToolRequest(
                "harness.tool_request/v1",
                "create_task_graph",
                {"goal": "do arbitrary work", "template_id": "unrestricted_shell"},
            ),
            project_root=tmp_path,
        )


def test_template_output_policy_checked_before_confirm(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    model = SideEffectRequestModel(
        '{"type":"harness.tool_request/v1","tool":"create_task_graph","arguments":{"goal":"fix failing test","template_id":"coding_fix"}}'
    )
    state = ChatSessionState()

    draft = handle_chat_input("turn this into a workflow", tmp_path, state, chat_model=model)
    response = handle_chat_input("yes", tmp_path, state)

    assert draft["kind"] == "action_contract"
    assert response["kind"] == "action_contract_executed"
    tasks = SQLiteStore(tmp_path).list_tasks(objective_id=state.latest_objective_id)
    assert [task.metadata["execution_adapter"] for task in tasks] == [
        "repo_planning",
        "codex_isolated_edit",
        "dry_run",
        "dry_run",
        "dry_run",
        "dry_run",
    ]
    assert tasks[1].depends_on == [tasks[0].id]
    assert tasks[3].metadata["review_role"] == "implementation_reviewer"
    assert tasks[4].metadata["review_role"] == "security_reviewer"
    assert tasks[5].metadata["requires_evidence_links"] == "objective,task,run,artifact,policy"
