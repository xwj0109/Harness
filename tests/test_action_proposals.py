from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from harness.action_proposals import contract_from_tool_request
from harness.backends.codex_cli import CodexCliBackend, CodexRunResult, NETWORK_NOT_ENFORCEABLE
from harness.chat import ChatSessionState, handle_chat_input
from harness.chat_model import ChatContext, ChatDelta, ChatMessage, ChatResponse
from harness.chat_tools import ChatToolRequest
from harness.cli.main import app
from harness.memory.sqlite_store import SQLiteStore
from harness.models import BackendCapabilities, BackendStatus
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
    assert [task.metadata["execution_adapter"] for task in tasks] == ["repo_planning", "codex_isolated_edit"]
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
    ]
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
    assert [task.metadata["execution_adapter"] for task in tasks] == ["repo_planning", "codex_isolated_edit"]
    assert tasks[1].depends_on == [tasks[0].id]
