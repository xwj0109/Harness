from __future__ import annotations

import subprocess
from collections.abc import Iterator

import harness.operator_loop as operator_loop_module
import harness.session_tools as session_tools_module
import yaml
from harness.chat import ChatSessionState, handle_chat_input
from harness.chat_model import ChatContext, ChatDelta, ChatMessage, ChatResponse, ChatToolCall, ChatToolSchema
from harness.chat_model import CodexCliChatModel
from harness.backends.local_openai import LocalEndpointUnavailable
from harness.config import default_config
from harness.memory.sqlite_store import SQLiteStore
from harness.models import SessionPermissionStatus
from harness.operator_loop import (
    create_turn_state_from_session,
    persist_turn_aborted,
    persist_turn_finished,
    persist_turn_started,
    session_operator_status_projection,
)
from harness.operator_models import HarnessAgentPhase


class FakeChatModel:
    def __init__(self, content: str = "I can help with that.") -> None:
        self.content = content
        self.messages: list[ChatMessage] = []
        self.context: ChatContext | None = None

    def stream(self, messages: list[ChatMessage], context: ChatContext) -> Iterator[ChatDelta]:
        yield ChatDelta(content=self.complete(messages, context).content)

    def complete(self, messages: list[ChatMessage], context: ChatContext) -> ChatResponse:
        self.messages = messages
        self.context = context
        return ChatResponse(content=self.content)


class FakeNativeToolModel:
    def __init__(self, responses: list[ChatResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    def complete_with_tools(
        self,
        messages: list[ChatMessage],
        context: ChatContext,
        tools: list[ChatToolSchema],
    ) -> ChatResponse:
        self.calls.append({"messages": list(messages), "context": context, "tools": list(tools)})
        if not self.responses:
            return ChatResponse(content="Done.")
        return self.responses.pop(0)

    def stream(self, _messages: list[ChatMessage], _context: ChatContext) -> Iterator[ChatDelta]:
        raise AssertionError("native tool model should use complete_with_tools")

    def complete(self, _messages: list[ChatMessage], _context: ChatContext) -> ChatResponse:
        raise AssertionError("native tool model should use complete_with_tools")


def _init_project(project_root) -> None:
    from typer.testing import CliRunner

    from harness.cli.main import app

    result = CliRunner().invoke(app, ["init", "--project", str(project_root)])
    assert result.exit_code == 0, result.output


def _run_git(cwd, args: list[str]) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, text=True, capture_output=True)


def _save_point_events(store: SQLiteStore, session_id: str):
    return [event for event in store.list_session_store_events(session_id) if event.kind == "harness.save_point"]


def test_default_config_includes_local_only_chat() -> None:
    cfg = default_config()

    assert cfg.chat.default_model_profile == "codex_cli"
    assert cfg.chat.mode == "subscription"
    assert cfg.chat.stream is True
    assert cfg.chat.allow_hosted_chat is False
    assert cfg.chat.allow_codex_subscription_chat is True


def test_freeform_chat_uses_chat_model_without_mutation(tmp_path) -> None:
    model = FakeChatModel("This is a model-backed answer.")
    state = ChatSessionState()

    response = handle_chat_input("explain how this project works", tmp_path, state, chat_model=model)

    assert response["kind"] == "llm_chat"
    assert response["ok"] is True
    assert response["lines"] == ["This is a model-backed answer."]
    assert response["model_profile"] == "codex_cli"
    assert response["hosted_fallback"] is False
    assert response["context_manifest"]["blocks"]
    assert response["context_manifest"]["role_summary"]["pinned"] >= 4
    assert response["context_manifest"]["context_provenance"]
    assert response["context_manifest"]["untrusted_context_warnings"]
    assert all("content" not in record for record in response["context_manifest"]["context_provenance"])
    assert all(record["lineage"]["permission_granting"] is False for record in response["context_manifest"]["context_provenance"])
    assert {block["role"] for block in response["context_manifest"]["blocks"]} >= {"pinned", "retrieved"}
    assert {block["kind"] for block in response["context_manifest"]["blocks"]} >= {
        "harness_vocabulary",
        "harness_state",
        "builtin_harness_domain",
        "request_context",
    }
    assert model.context is not None
    assert model.context.mode == "normal"
    assert {block["kind"] for block in model.context.context_blocks} >= {
        "harness_vocabulary",
        "harness_state",
        "builtin_harness_domain",
    }
    assert model.messages[-1] == ChatMessage(role="user", content="explain how this project works")
    assert not (tmp_path / ".harness").exists()


def test_active_plan_mode_uses_model_for_prompt_specific_plan(tmp_path) -> None:
    _init_project(tmp_path)
    state = ChatSessionState()
    model = FakeChatModel(
        "\n".join(
            [
                "Plan:",
                "1. Define a Markowitz portfolio script with expected returns, covariance, risk-free rate, and constraints.",
                "2. Implement efficient frontier and max Sharpe calculations.",
                "3. Add focused tests for weight normalization, covariance shape checks, and optimizer output.",
            ]
        )
    )

    entered = handle_chat_input("/plan-mode on inspect before editing", tmp_path, state)
    response = handle_chat_input(
        "build a plan for a script that implements markowitz portfolio theory in python",
        tmp_path,
        state,
        chat_model=model,
    )

    assert entered["ok"] is True
    assert response["kind"] == "plan_mode_plan"
    assert response["captured_intent"] == "coding_fix"
    assert response["creates_pending_action"] is False
    assert response["mode"] == "plan"
    assert model.context is not None
    assert model.context.mode == "plan"
    rendered = "\n".join(response["lines"])
    assert "Markowitz portfolio script" in rendered
    assert "efficient frontier" in rendered
    assert "Clarify the desired outcome" not in rendered


def test_active_plan_mode_turns_side_effect_tool_request_into_model_visible_boundary(tmp_path) -> None:
    class ToolThenPlanModel:
        def __init__(self) -> None:
            self.calls: list[list[ChatMessage]] = []
            self.contexts: list[ChatContext] = []

        def stream(self, messages: list[ChatMessage], context: ChatContext) -> Iterator[ChatDelta]:
            yield ChatDelta(content=self.complete(messages, context).content)

        def complete(self, messages: list[ChatMessage], context: ChatContext) -> ChatResponse:
            self.calls.append(list(messages))
            self.contexts.append(context)
            if len(self.calls) == 1:
                return ChatResponse(
                    content='{"type":"harness.tool_request/v1","tool":"edit_isolated","arguments":{"goal":"add importer"}}'
                )
            return ChatResponse(content="Plan:\n1. Inspect importer entry points.\n2. Propose an isolated edit later.")

    _init_project(tmp_path)
    state = ChatSessionState()
    model = ToolThenPlanModel()

    entered = handle_chat_input("/plan-mode on inspect before editing", tmp_path, state)
    response = handle_chat_input("build a plan to add a command line csv importer", tmp_path, state, chat_model=model)
    confirm = handle_chat_input("/confirm", tmp_path, state)

    rendered = "\n".join(response["lines"])
    assert entered["ok"] is True
    assert response["kind"] == "plan_mode_plan"
    assert response["captured_intent"] == "coding_fix"
    assert response["creates_pending_action"] is False
    assert response["provider_execution_started"] is False
    assert response["adapter_dispatch_started"] is False
    assert response["tool_results"] == [{"tool": "edit_isolated", "ok": False, "error_type": "plan_mode_boundary"}]
    assert len(model.calls) == 2
    assert all(context.mode == "plan" for context in model.contexts)
    assert any("Harness plan-mode boundary" in message.content for message in model.calls[1])
    assert "Inspect importer entry points" in rendered
    assert "Type yes" not in rendered
    assert "Required approvals" not in rendered
    assert state.pending_draft is None
    assert state.pending_orchestration is None
    assert state.pending_execute_lease_id is None
    assert state.pending_action_contract is None
    assert state.pending_session_tool_call is None
    assert state.pending_hosted_approval is False
    assert confirm["kind"] == "nothing_to_confirm"


def test_active_plan_mode_native_tool_loop_uses_plan_agent_tools_without_approvals(tmp_path) -> None:
    _init_project(tmp_path)
    state = ChatSessionState()
    model = FakeNativeToolModel(
        [
            ChatResponse(
                content="I will try to run shell.",
                tool_calls=[
                    ChatToolCall(
                        id="call_shell",
                        name="shell",
                        arguments={"command": "python3 -m pytest", "timeout_seconds": 120},
                    )
                ],
            ),
            ChatResponse(content="Plan:\n1. Inspect tests.\n2. Treat shell execution as a later governed step."),
        ]
    )

    entered = handle_chat_input("/plan-mode on inspect before editing", tmp_path, state)
    response = handle_chat_input("build a plan for the test command", tmp_path, state, chat_model=model)

    first_tool_names = {tool.name for tool in model.calls[0]["tools"]}
    assert entered["ok"] is True
    assert response["kind"] == "plan_mode_plan"
    assert response["mode"] == "plan"
    assert "shell" not in first_tool_names
    assert {"read", "grep", "glob"}.issubset(first_tool_names)
    assert response["tool_results"][0]["tool"] == "shell"
    assert response["tool_results"][0]["error_type"] == "plan_mode_tool_not_available"
    assert state.pending_session_tool_call is None
    assert SQLiteStore(tmp_path).list_session_permissions(state.session_id) == []


def test_slash_commands_do_not_call_chat_model(tmp_path) -> None:
    class FailingChatModel(FakeChatModel):
        def complete(self, messages: list[ChatMessage], context: ChatContext) -> ChatResponse:
            raise AssertionError("slash commands should remain deterministic")

    response = handle_chat_input("/help", tmp_path, ChatSessionState(), chat_model=FailingChatModel())

    assert response["kind"] == "help"
    assert response["ok"] is True


def test_natural_language_router_handles_project_and_cwd_navigation(tmp_path) -> None:
    _init_project(tmp_path)
    (tmp_path / "src" / "harness").mkdir(parents=True)
    state = ChatSessionState()

    switched = handle_chat_input(f"move to {tmp_path}", tmp_path, state)
    cd = handle_chat_input("move to src/harness", tmp_path, state)
    cd_save_points = _save_point_events(SQLiteStore(tmp_path), state.session_id)
    root = handle_chat_input("go back to repo root", tmp_path, state)

    assert switched["kind"] == "project_switched"
    assert switched["ok"] is True
    assert state.active_project_root == str(tmp_path.resolve())
    assert cd["kind"] == "session_tool_result"
    assert "Changed session cwd: . -> src/harness" in "\n".join(cd["lines"])
    assert cd["operator_status"]["cwd"] == "src/harness"
    assert cd_save_points
    assert root["kind"] == "session_tool_result"
    assert "Changed session cwd: src/harness -> ." in "\n".join(root["lines"])
    assert root["operator_status"]["cwd"] == "."
    assert len(_save_point_events(SQLiteStore(tmp_path), state.session_id)) >= len(cd_save_points) + 1
    assert SQLiteStore(tmp_path).get_session(state.session_id).metadata["cwd"] == "."


def test_natural_language_router_rejects_outside_path_without_switching(tmp_path) -> None:
    _init_project(tmp_path)
    outside = tmp_path.parent / f"{tmp_path.name}_outside"
    outside.mkdir()
    state = ChatSessionState()

    response = handle_chat_input(f"move to {outside}", tmp_path, state)

    assert response["kind"] == "project_switch_boundary"
    assert response["ok"] is False
    assert "outside the active project" in "\n".join(response["lines"])
    assert state.active_project_root == str(tmp_path.resolve())


def test_natural_language_router_routes_diff_and_search_to_session_tools(tmp_path) -> None:
    _init_project(tmp_path)
    _run_git(tmp_path, ["init"])
    _run_git(tmp_path, ["config", "user.email", "test@example.com"])
    _run_git(tmp_path, ["config", "user.name", "Harness Test"])
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "approval.py").write_text("shell approval is implemented here\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("old\n", encoding="utf-8")
    _run_git(tmp_path, ["add", "README.md", "src/approval.py"])
    _run_git(tmp_path, ["commit", "-m", "initial"])
    (tmp_path / "README.md").write_text("new\n", encoding="utf-8")
    state = ChatSessionState()

    diff = handle_chat_input("show me what changed", tmp_path, state)
    diff_save_points = _save_point_events(SQLiteStore(tmp_path), state.session_id)
    search = handle_chat_input("find where shell approval is implemented", tmp_path, state)

    assert diff["kind"] == "session_tool_result"
    assert diff["result"]["tool_id"] == "git-diff"
    assert "-old" in "\n".join(diff["lines"])
    assert "+new" in "\n".join(diff["lines"])
    assert diff_save_points
    assert diff_save_points[-1].payload["turn_state"]["turn_id"] == diff["operator_status"]["turn_id"]
    assert search["kind"] == "session_tool_result"
    assert search["result"]["tool_id"] == "grep"
    assert "src/approval.py:1: shell approval is implemented here" in "\n".join(search["lines"])
    assert search["result"]["tool_id"] != "shell"
    assert len(_save_point_events(SQLiteStore(tmp_path), state.session_id)) >= len(diff_save_points) + 1


def test_natural_language_router_routes_pwd_and_read_to_session_tools(tmp_path) -> None:
    _init_project(tmp_path)
    (tmp_path / "README.md").write_text("Harness notes\n", encoding="utf-8")
    state = ChatSessionState()

    pwd = handle_chat_input("pwd", tmp_path, state)
    read = handle_chat_input("read README.md", tmp_path, state)

    assert pwd["kind"] == "session_tool_result"
    assert pwd["result"]["tool_id"] == "pwd"
    assert "Session cwd: ." in "\n".join(pwd["lines"])
    assert read["kind"] == "session_tool_result"
    assert read["result"]["tool_id"] == "read"
    assert read["lines"] == ["Harness notes"]
    assert len(_save_point_events(SQLiteStore(tmp_path), state.session_id)) >= 2


def test_operator_turn_records_event_and_returns_idle_after_success(tmp_path) -> None:
    _init_project(tmp_path)
    state = ChatSessionState()

    response = handle_chat_input("pwd", tmp_path, state)

    assert response["kind"] == "session_tool_result"
    assert response["operator_status"]["phase"] == HarnessAgentPhase.IDLE.value
    assert response["operator_status"]["project_root"] == str(tmp_path.resolve())
    assert response["operator_status"]["cwd"] == "."
    assert state.operator_runtime.phase == HarnessAgentPhase.IDLE
    events = SQLiteStore(tmp_path).list_session_store_events(state.session_id)
    turn_events = [event for event in events if event.kind == "operator.turn.started"]
    assert turn_events
    assert turn_events[-1].payload["turn_state"]["session_id"] == state.session_id
    assert turn_events[-1].payload["turn_state"]["active_tools"]


def test_operator_turn_returns_idle_after_routed_failure(tmp_path) -> None:
    _init_project(tmp_path)
    state = ChatSessionState()

    response = handle_chat_input("read missing.md", tmp_path, state)

    assert response["kind"] == "session_tool_result"
    assert response["ok"] is False
    assert response["operator_status"]["phase"] == HarnessAgentPhase.IDLE.value
    assert state.operator_runtime.phase == HarnessAgentPhase.IDLE


def test_natural_language_router_routes_named_test_run_to_shell_approval(tmp_path) -> None:
    _init_project(tmp_path)
    state = ChatSessionState()
    progress: list[dict] = []

    response = handle_chat_input(
        "run the session tool tests",
        tmp_path,
        state,
        chat_model=FakeChatModel("should not run"),
        progress_callback=progress.append,
    )

    assert response["kind"] == "session_tool_permission_required"
    assert response["ok"] is False
    assert response["tool_request"]["tool"] == "shell"
    assert response["tool_request"]["arguments"]["command"] == "python3 -m pytest tests/test_session_tools.py -q"
    assert response["approval_card"]["tool_id"] == "shell"
    assert response["approval_card"]["command"] == "python3 -m pytest tests/test_session_tools.py -q"
    assert response["approval_card"]["cwd"] == "."
    assert response["approval_card"]["timeout_seconds"] == 120
    assert response["approval_card"]["sandbox_profile"] == "session_tool_shell_exact"
    assert response["approval_card"]["network_policy"] == "host_network_available"
    rendered = "\n".join(response["lines"])
    assert "approval:" in rendered
    assert "sandbox: session_tool_shell_exact" in rendered
    assert state.pending_session_tool_call is not None
    assert state.pending_session_tool_call["tool_id"] == "shell"
    assert "Shell command executed." not in "\n".join(response["lines"])
    assert response["operator_status"]["phase"] == HarnessAgentPhase.WAITING_APPROVAL.value
    assert response["operator_status"]["waiting_approval_id"]
    assert state.operator_runtime.phase == HarnessAgentPhase.WAITING_APPROVAL
    save_points = _save_point_events(SQLiteStore(tmp_path), state.session_id)
    assert save_points
    assert save_points[-1].payload["turn_state"]["turn_id"] == response["operator_status"]["turn_id"]
    contents = [item["content"] for item in progress]
    assert "- intent: run_tests" in contents
    assert "- intent: unsupported" not in contents


def test_operator_decline_clears_waiting_approval_and_returns_idle(tmp_path) -> None:
    _init_project(tmp_path)
    state = ChatSessionState()

    approval = handle_chat_input("run the session tool tests", tmp_path, state)
    declined = handle_chat_input("no", tmp_path, state)

    assert approval["operator_status"]["phase"] == HarnessAgentPhase.WAITING_APPROVAL.value
    assert declined["kind"] == "declined"
    assert declined["operator_status"]["phase"] == HarnessAgentPhase.IDLE.value
    assert state.pending_session_tool_call is None
    assert state.operator_runtime.phase == HarnessAgentPhase.IDLE
    store = SQLiteStore(tmp_path)
    assert store.get_session_permission(approval["permission_id"]).status == SessionPermissionStatus.DENIED
    projection = session_operator_status_projection(
        store,
        state.session_id,
        project_root=tmp_path.resolve(),
        cwd=".",
        active_tools=["shell"],
    )
    assert projection["phase"] == HarnessAgentPhase.IDLE.value
    assert projection["waiting_approval_id"] is None


def test_operator_decline_with_feedback_records_model_visible_tool_error(tmp_path) -> None:
    _init_project(tmp_path)
    state = ChatSessionState()

    approval = handle_chat_input("run the session tool tests", tmp_path, state)
    declined = handle_chat_input("/decline tests are too expensive right now", tmp_path, state)

    assert declined["kind"] == "declined"
    assert declined["operator_status"]["phase"] == HarnessAgentPhase.IDLE.value
    assert declined["denial"]["feedback"] == "tests are too expensive right now"
    assert "Model-visible tool error recorded." in declined["lines"]
    store = SQLiteStore(tmp_path)
    assert store.get_session_permission(approval["permission_id"]).status == SessionPermissionStatus.DENIED
    events = store.list_session_store_events(state.session_id)
    denial_event = next(event for event in events if event.kind == "harness.approval.denied")
    assert denial_event.payload["feedback"] == "tests are too expensive right now"
    assert denial_event.payload["permission_id"] == approval["permission_id"]
    assert "Tool call denied by operator." in denial_event.payload["model_visible_error"]


def test_operator_busy_rejects_second_structural_prompt(tmp_path) -> None:
    _init_project(tmp_path)
    store = SQLiteStore.open_initialized(tmp_path)
    session = store.create_session(title="busy")
    turn_state = create_turn_state_from_session(
        project_root=tmp_path,
        session=session,
        model_profile_id="codex_cli",
        backend_id="codex_cli",
        agent_id="operator",
        workbench_id=None,
        active_tools=["pwd"],
    )
    state = ChatSessionState(session_id=session.id)
    state.operator_runtime.start_turn(turn_state)

    response = handle_chat_input("pwd", tmp_path, state)

    assert response["kind"] == "operator_busy"
    assert response["ok"] is False
    assert response["operator_status"]["phase"] == HarnessAgentPhase.TURN.value
    state.operator_runtime.finish()


def test_persisted_operator_status_reports_active_turn_until_terminal_event(tmp_path) -> None:
    _init_project(tmp_path)
    store = SQLiteStore.open_initialized(tmp_path)
    session = store.create_session(title="persisted active turn")
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
    active = session_operator_status_projection(
        store,
        session.id,
        project_root=tmp_path.resolve(),
        cwd=".",
        active_tools=["pwd"],
    )
    persist_turn_finished(store, turn_state)
    finished = session_operator_status_projection(
        store,
        session.id,
        project_root=tmp_path.resolve(),
        cwd=".",
        active_tools=["pwd"],
    )
    persist_turn_started(store, turn_state, prompt="pwd")
    persist_turn_aborted(store, turn_state, reason="test_abort")
    aborted = session_operator_status_projection(
        store,
        session.id,
        project_root=tmp_path.resolve(),
        cwd=".",
        active_tools=["pwd"],
    )

    assert active["phase"] == HarnessAgentPhase.TURN.value
    assert active["turn_id"] == turn_state.turn_id
    assert finished["phase"] == HarnessAgentPhase.IDLE.value
    assert aborted["phase"] == HarnessAgentPhase.IDLE.value


def test_provider_native_agent_loop_emits_save_point_after_assistant_only_response(tmp_path) -> None:
    _init_project(tmp_path)
    model = FakeNativeToolModel([ChatResponse(content="No tools are needed.")])
    state = ChatSessionState()

    response = handle_chat_input("answer with native model", tmp_path, state, chat_model=model)

    assert response["kind"] == "llm_chat"
    assert response["native_tool_loop"] is True
    store = SQLiteStore(tmp_path)
    save_points = _save_point_events(store, state.session_id)
    assert len(save_points) == 1
    save_point = save_points[0].payload["save_point"]
    assert save_point["schema_version"] == "harness.save_point/v1"
    assert save_point["flushed_event_count"] > 0
    assert save_point["flushed_artifact_count"] >= 0
    assert save_point["next_turn_state_sha256"]
    projection = session_operator_status_projection(
        store,
        state.session_id,
        project_root=tmp_path.resolve(),
        cwd=".",
        active_tools=["pwd"],
    )
    assert projection["latest_save_point"]["save_point_id"] == save_point["save_point_id"]


def test_provider_native_agent_loop_calls_grep_then_read_then_final(tmp_path) -> None:
    _init_project(tmp_path)
    (tmp_path / "README.md").write_text("Harness notes\nneedle appears here\n", encoding="utf-8")
    model = FakeNativeToolModel(
        [
            ChatResponse(
                content="I will search first.",
                tool_calls=[
                    ChatToolCall(
                        id="call_grep",
                        name="grep",
                        arguments={"pattern": "needle", "path": ".", "regex": False, "limit": 20},
                    )
                ],
            ),
            ChatResponse(
                content="I found the file; I will read it.",
                tool_calls=[ChatToolCall(id="call_read", name="read", arguments={"path": "README.md"})],
            ),
            ChatResponse(content="The README mentions Harness notes and the needle line."),
        ]
    )
    state = ChatSessionState()

    response = handle_chat_input("inspect the repository with tools", tmp_path, state, chat_model=model)

    assert response["kind"] == "llm_chat"
    assert response["native_tool_loop"] is True
    assert response["lines"] == ["The README mentions Harness notes and the needle line."]
    assert [item["tool"] for item in response["tool_results"]] == ["grep", "read"]
    assert all(item["ok"] for item in response["tool_results"])
    assert any(tool.name == "grep" and tool.input_schema["required"] == ["pattern"] for tool in model.calls[0]["tools"])
    assert any('"tool": "grep"' in message.content for message in model.calls[1]["messages"] if message.role == "tool")
    assert any('"tool": "read"' in message.content for message in model.calls[2]["messages"] if message.role == "tool")
    store = SQLiteStore(tmp_path)
    parts = store.list_session_parts(state.session_id)
    assert any(part.kind.value == "tool_call" and part.metadata.get("provider_native") is True for part in parts)
    save_points = _save_point_events(store, state.session_id)
    assert len(save_points) == 3
    assert save_points[0].payload["save_point"]["flushed_event_count"] > 0
    assert save_points[0].payload["save_point"]["flushed_artifact_count"] >= 0
    assert save_points[0].payload["next_turn_state"]["session_id"] == state.session_id


def test_provider_native_agent_loop_refreshes_cwd_snapshot_after_save_point(tmp_path) -> None:
    _init_project(tmp_path)
    (tmp_path / "src").mkdir()
    model = FakeNativeToolModel(
        [
            ChatResponse(
                content="I will move the session cwd.",
                tool_calls=[ChatToolCall(id="call_cd", name="cd", arguments={"path": "src", "actor": "model"})],
            ),
            ChatResponse(content="The next request sees the refreshed cwd."),
        ]
    )
    state = ChatSessionState()

    response = handle_chat_input("move into src and continue", tmp_path, state, chat_model=model)

    assert response["kind"] == "llm_chat"
    assert len(model.calls) == 2
    store = SQLiteStore(tmp_path)
    save_points = _save_point_events(store, state.session_id)
    assert save_points[0].payload["turn_state"]["cwd"] == "."
    assert save_points[0].payload["next_turn_state"]["cwd"] == "src"
    turn_events = [event for event in store.list_session_store_events(state.session_id) if event.kind == "operator.turn.started"]
    assert any(event.payload["turn_state"]["cwd"] == "src" for event in turn_events)


def test_provider_native_agent_loop_refreshes_active_tools_after_save_point(tmp_path, monkeypatch) -> None:
    _init_project(tmp_path)
    original_descriptors = operator_loop_module.default_session_tool_descriptors()
    restricted = {"enabled": False}

    def descriptors_for_loop():
        if restricted["enabled"]:
            return [descriptor for descriptor in original_descriptors if descriptor.id == "pwd"]
        return original_descriptors

    class RestrictingNativeToolModel(FakeNativeToolModel):
        def complete_with_tools(
            self,
            messages: list[ChatMessage],
            context: ChatContext,
            tools: list[ChatToolSchema],
        ) -> ChatResponse:
            response = super().complete_with_tools(messages, context, tools)
            if len(self.calls) == 1:
                restricted["enabled"] = True
            return response

    monkeypatch.setattr(operator_loop_module, "default_session_tool_descriptors", descriptors_for_loop)
    monkeypatch.setattr(session_tools_module, "default_session_tool_descriptors", descriptors_for_loop)
    model = RestrictingNativeToolModel(
        [
            ChatResponse(content="I will inspect pwd.", tool_calls=[ChatToolCall(id="call_pwd", name="pwd", arguments={})]),
            ChatResponse(content="The next request has refreshed tools."),
        ]
    )
    state = ChatSessionState()

    response = handle_chat_input("inspect with refreshed tools", tmp_path, state, chat_model=model)

    assert response["kind"] == "llm_chat"
    assert "grep" in {tool.name for tool in model.calls[0]["tools"]}
    assert {tool.name for tool in model.calls[1]["tools"]} == {"pwd"}
    save_points = _save_point_events(SQLiteStore(tmp_path), state.session_id)
    assert save_points[0].payload["next_turn_state"]["active_tools"] == ["pwd"]


def test_provider_native_agent_loop_drains_queued_steer_before_next_model_request(tmp_path) -> None:
    _init_project(tmp_path)
    model = FakeNativeToolModel(
        [
            ChatResponse(content="I will check pwd.", tool_calls=[ChatToolCall(id="call_pwd", name="pwd", arguments={})]),
            ChatResponse(content="I considered the queued steering."),
        ]
    )
    state = ChatSessionState()
    state.operator_runtime.enqueue("steer", "Use a concise final answer.")

    response = handle_chat_input("inspect and then answer", tmp_path, state, chat_model=model)

    assert response["kind"] == "llm_chat"
    assert any("Harness operator steer message" in message.content for message in model.calls[1]["messages"])
    parts = SQLiteStore(tmp_path).list_session_parts(state.session_id)
    assert any(part.metadata.get("source") == "harness_operator_queue:steer" for part in parts)


def test_provider_native_agent_loop_calls_git_diff_then_final(tmp_path) -> None:
    _init_project(tmp_path)
    _run_git(tmp_path, ["init"])
    _run_git(tmp_path, ["config", "user.email", "test@example.com"])
    _run_git(tmp_path, ["config", "user.name", "Harness Test"])
    (tmp_path / "README.md").write_text("old\n", encoding="utf-8")
    _run_git(tmp_path, ["add", "README.md"])
    _run_git(tmp_path, ["commit", "-m", "initial"])
    (tmp_path / "README.md").write_text("new\n", encoding="utf-8")
    model = FakeNativeToolModel(
        [
            ChatResponse(
                content="I will inspect the diff.",
                tool_calls=[ChatToolCall(id="call_diff", name="git-diff", arguments={})],
            ),
            ChatResponse(content="README.md changed from old to new."),
        ]
    )

    response = handle_chat_input("inspect current repository changes", tmp_path, ChatSessionState(), chat_model=model)

    assert response["kind"] == "llm_chat"
    assert response["native_tool_loop"] is True
    assert [item["tool"] for item in response["tool_results"]] == ["git-diff"]
    assert response["tool_results"][0]["ok"] is True
    assert "-old" in response["tool_results"][0]["content"]
    assert "+new" in response["tool_results"][0]["content"]


def test_provider_native_agent_loop_returns_model_visible_unknown_tool_error(tmp_path) -> None:
    _init_project(tmp_path)
    model = FakeNativeToolModel(
        [
            ChatResponse(
                content="I will call an unknown tool.",
                tool_calls=[ChatToolCall(id="call_unknown", name="unknown-tool", arguments={})],
            ),
            ChatResponse(content="I recovered after the tool error."),
        ]
    )
    state = ChatSessionState()

    response = handle_chat_input("inspect with a native tool", tmp_path, state, chat_model=model)

    assert response["kind"] == "llm_chat"
    assert response["lines"] == ["I recovered after the tool error."]
    assert response["tool_results"][0]["ok"] is False
    assert response["tool_results"][0]["error_type"] == "unknown_tool"
    assert any("Unknown session tool" in message.content for message in model.calls[1]["messages"] if message.role == "tool")
    events = SQLiteStore(tmp_path).list_session_store_events(state.session_id)
    assert any(event.kind == "harness.agent_loop.tool_error" for event in events)


def test_provider_native_agent_loop_repeat_guard_skips_guarded_call(tmp_path) -> None:
    _init_project(tmp_path)
    (tmp_path / "README.md").write_text("needle\n", encoding="utf-8")
    repeated_call = ChatToolCall(
        id="call_repeated",
        name="grep",
        arguments={"pattern": "needle", "path": ".", "regex": False, "limit": 20},
    )
    model = FakeNativeToolModel(
        [
            ChatResponse(content="Search once.", tool_calls=[repeated_call]),
            ChatResponse(content="Search twice.", tool_calls=[repeated_call]),
            ChatResponse(content="Search again.", tool_calls=[repeated_call]),
            ChatResponse(content="This should not be reached."),
        ]
    )
    state = ChatSessionState()

    response = handle_chat_input("inspect with repeated native tools", tmp_path, state, chat_model=model)

    assert response["kind"] == "agent_loop_guard_triggered"
    assert response["ok"] is False
    assert response["stop_reason"] == "same_tool_same_args_guard"
    assert len(model.calls) == 3
    events = SQLiteStore(tmp_path).list_session_store_events(state.session_id)
    assert sum(1 for event in events if event.kind == "tool_call.output" and event.payload.get("tool_id") == "grep") == 2
    assert any(event.kind == "harness.agent_loop_guard.triggered" for event in events)


def test_provider_native_agent_loop_unexposed_shell_request_fails_closed(tmp_path) -> None:
    _init_project(tmp_path)
    model = FakeNativeToolModel(
        [
            ChatResponse(
                content="I will request a shell command.",
                tool_calls=[
                    ChatToolCall(
                        id="call_shell",
                        name="shell",
                        arguments={"command": "python3 -m pytest tests/test_session_tools.py -q", "timeout_seconds": 120},
                    )
                ],
            ),
            ChatResponse(content="This should not run until approval is resolved."),
        ]
    )
    state = ChatSessionState()

    response = handle_chat_input("native execution approval probe", tmp_path, state, chat_model=model)

    assert response["kind"] == "llm_chat"
    assert response["ok"] is True
    assert state.pending_session_tool_call is None
    assert response["tool_results"][0]["tool"] == "shell"
    assert response["tool_results"][0]["ok"] is False
    assert response["tool_results"][0]["error_type"] == "active_tool_not_available"
    assert len(model.calls) == 2
    assert all("shell" not in {tool.name for tool in call["tools"]} for call in model.calls)
    assert "Shell command executed." not in "\n".join(response["lines"])
    events = SQLiteStore(tmp_path).list_session_store_events(state.session_id)
    tool_errors = [event for event in events if event.kind == "harness.agent_loop.tool_error"]
    assert tool_errors[-1].payload["tool_id"] == "shell"
    assert tool_errors[-1].payload["error_type"] == "active_tool_not_available"


def test_chat_model_unavailable_does_not_initialize_or_fallback(tmp_path, monkeypatch) -> None:
    def unavailable(_project_root):
        raise LocalEndpointUnavailable("test backend unavailable")

    monkeypatch.setattr("harness.chat.build_default_chat_model", unavailable)
    response = handle_chat_input("hello model", tmp_path, ChatSessionState())

    assert response["kind"] == "chat_model_unavailable"
    assert response["ok"] is False
    assert response["hosted_fallback"] is False
    assert "does not fall back to paid hosted chat automatically" in "\n".join(response["lines"])
    assert not (tmp_path / ".harness").exists()


def test_codex_cli_chat_model_uses_read_only_subscription_backend(tmp_path) -> None:
    class FakeCodexBackend:
        def __init__(self) -> None:
            self.prompt = ""
            self.project_root = None

        def run_read_only(self, project_root, prompt, final_message_path):
            self.project_root = project_root
            self.prompt = prompt
            final_message_path.write_text("Codex subscription answer.", encoding="utf-8")

            class Result:
                exit_status = 0
                stderr = ""
                stdout = ""
                json_events = []
                final_message = "Codex subscription answer."

            return Result()

    backend = FakeCodexBackend()
    model = CodexCliChatModel(backend, tmp_path)  # type: ignore[arg-type]
    response = model.complete(
        [ChatMessage(role="user", content="explain orchestration")],
        ChatContext(project_root=str(tmp_path), model_profile="codex_cli", mode="normal"),
    )

    assert response.content == "Codex subscription answer."
    assert backend.project_root == tmp_path
    assert "Harness assistant is act-capable" in backend.prompt
    assert "harness.tool_request/v1" in backend.prompt
    assert "explain orchestration" in backend.prompt


def test_codex_cli_chat_model_streams_reasoning_before_final_answer(tmp_path) -> None:
    class FakeCodexBackend:
        def stream_read_only(self, project_root, prompt, final_message_path):
            self.project_root = project_root
            self.prompt = prompt
            yield {
                "type": "event",
                "event": {
                    "type": "agent_reasoning",
                    "summary": [{"text": "Inspecting the Harness context."}],
                },
            }
            final_message_path.write_text("Final answer.", encoding="utf-8")

            class Result:
                exit_status = 0
                stderr = ""
                stdout = ""
                json_events = []
                final_message = "Final answer."

            yield {"type": "completed", "result": Result()}

    backend = FakeCodexBackend()
    model = CodexCliChatModel(backend, tmp_path)  # type: ignore[arg-type]

    deltas = list(
        model.stream(
            [ChatMessage(role="user", content="explain orchestration")],
            ChatContext(project_root=str(tmp_path), model_profile="codex_cli", mode="normal"),
        )
    )

    assert [delta.kind for delta in deltas] == ["reasoning", "content"]
    assert deltas[0].content == "Reasoning: Inspecting the Harness context."
    assert deltas[1].content == "Final answer."
    assert backend.project_root == tmp_path
    assert "explain orchestration" in backend.prompt


def test_chat_model_path_emits_reasoning_between_tool_calls(tmp_path) -> None:
    (tmp_path / "README.md").write_text("# Harness\n\nLocal control plane.\n", encoding="utf-8")

    class ToolThenAnswerModel:
        def __init__(self) -> None:
            self.turn = 0

        def stream(self, _messages, _context):
            self.turn += 1
            if self.turn == 1:
                yield ChatDelta(
                    content='{"type":"harness.tool_request/v1","tool":"read_file","arguments":{"path":"README.md"}}',
                    kind="content",
                )
                return
            yield ChatDelta(content="The README describes Harness.", kind="content")

        def complete(self, _messages, _context):
            raise AssertionError("streaming path should be used when progress is requested")

    progress: list[dict] = []
    response = handle_chat_input(
        "explain this repository",
        tmp_path,
        ChatSessionState(),
        chat_model=ToolThenAnswerModel(),
        progress_callback=progress.append,
    )

    contents = [item["content"] for item in progress]
    assert response["kind"] == "llm_chat"
    assert response["lines"] == ["The README describes Harness."]
    assert "Reasoning: requesting read_file." in contents
    assert "Ran read_file" in contents
    assert "- read_file: ok" in contents
    assert not any("harness.tool_request/v1" in content for content in contents)
    assert contents.index("Reasoning: requesting read_file.") < contents.index("Ran read_file")


def test_model_requested_web_search_uses_user_prompt_as_missing_query_and_pauses_for_approval(tmp_path) -> None:
    _init_project(tmp_path)
    config_path = tmp_path / ".harness" / "config.yaml"
    config_data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config_data["web_tools"] = {
        "enabled": True,
        "fetch_enabled": False,
        "search_enabled": True,
        "approval_required": True,
        "search_endpoint_url": "http://127.0.0.1:9/search",
    }
    config_path.write_text(yaml.safe_dump(config_data, sort_keys=False), encoding="utf-8")

    class WebSearchWithoutArgsModel:
        def complete(self, _messages, _context):
            return ChatResponse(content='{"type":"harness.tool_request/v1","tool":"web-search","arguments":{}}')

    prompt = "what happened this weekend in italy tragic news"
    response = handle_chat_input(prompt, tmp_path, ChatSessionState(), chat_model=WebSearchWithoutArgsModel())

    assert response["kind"] == "session_tool_permission_required"
    assert response["tool_request"]["tool"] == "web-search"
    assert response["tool_request"]["arguments"]["query"] == prompt
    assert response["approval_card"]["tool_id"] == "web-search"
    assert response["approval_card"]["operation"] == "web-search"
    assert response["permission_id"]
    assert response["operator_status"]["phase"] == HarnessAgentPhase.WAITING_APPROVAL.value
    assert "Missing required argument" not in "\n".join(response["lines"])


def test_model_requested_web_search_normalizes_common_query_aliases(tmp_path) -> None:
    _init_project(tmp_path)
    config_path = tmp_path / ".harness" / "config.yaml"
    config_data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config_data["web_tools"] = {
        "enabled": True,
        "fetch_enabled": False,
        "search_enabled": True,
        "approval_required": True,
        "search_endpoint_url": "http://127.0.0.1:9/search",
    }
    config_path.write_text(yaml.safe_dump(config_data, sort_keys=False), encoding="utf-8")

    class WebSearchAliasModel:
        def complete(self, _messages, _context):
            return ChatResponse(content='{"type":"harness.tool_request/v1","tool":"web-search","arguments":{"q":"italy weekend tragic news"}}')

    response = handle_chat_input("look it up", tmp_path, ChatSessionState(), chat_model=WebSearchAliasModel())

    assert response["kind"] == "session_tool_permission_required"
    assert response["tool_request"]["arguments"]["query"] == "italy weekend tragic news"
    assert "q" not in response["tool_request"]["arguments"]


def test_model_requested_web_search_disabled_policy_returns_actionable_config_guidance(tmp_path) -> None:
    _init_project(tmp_path)

    class WebSearchModel:
        def complete(self, _messages, _context):
            return ChatResponse(content='{"type":"harness.tool_request/v1","tool":"web-search","arguments":{}}')

    prompt = "what happened today in italy tragic news"
    response = handle_chat_input(prompt, tmp_path, ChatSessionState(), chat_model=WebSearchModel())

    rendered = "\n".join(response["lines"])
    assert response["kind"] == "session_tool_blocked"
    assert response["tool_request"]["tool"] == "web-search"
    assert response["tool_request"]["arguments"]["query"] == prompt
    assert "Web search is disabled by project web_tools policy." in rendered
    assert "web_tools.enabled: true" in rendered
    assert "web_tools.search_enabled: true" in rendered
    assert "web_tools.search_provider: exa_mcp" in rendered
    assert "configured_http" in rendered
    assert "Missing required argument" not in rendered


def test_mutation_request_falls_back_to_action_contract_when_model_only_refuses(tmp_path) -> None:
    model = FakeChatModel("I can't create the file directly from this read-only chat turn.")
    state = ChatSessionState()

    response = handle_chat_input("create an empty file in the repository", tmp_path, state, chat_model=model)

    assert response["kind"] == "action_contract"
    assert response["contract"]["tool"] == "edit_isolated"
    assert response["contract"]["normalized_arguments"]["goal"] == "create an empty file in the repository"
    assert state.pending_action_contract is not None
    assert not (tmp_path / ".harness").exists()


def test_test_request_routes_to_shell_permission_without_model_execution(tmp_path) -> None:
    _init_project(tmp_path)

    class FailingChatModel(FakeChatModel):
        def complete(self, messages: list[ChatMessage], context: ChatContext) -> ChatResponse:
            raise AssertionError("test run phrases should be routed before the model")

    model = FailingChatModel("I cannot run tests directly from read-only chat.")
    state = ChatSessionState()

    response = handle_chat_input("run the tests", tmp_path, state, chat_model=model)

    assert response["kind"] == "session_tool_permission_required"
    assert response["tool_request"]["tool"] == "shell"
    assert response["tool_request"]["arguments"]["command"] == "python3 -m pytest -q"
    assert state.pending_session_tool_call is not None
