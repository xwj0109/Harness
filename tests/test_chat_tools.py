from __future__ import annotations

from collections.abc import Iterator

from harness.chat import ChatSessionState, handle_chat_input
from harness.chat_model import ChatContext, ChatDelta, ChatMessage, ChatResponse
from harness.chat_tools import (
    ChatToolRequest,
    default_chat_tool_context,
    default_chat_tools,
    parse_tool_request,
    run_chat_tool,
)


class ToolLoopModel:
    def __init__(self) -> None:
        self.calls: list[list[ChatMessage]] = []

    def stream(self, messages: list[ChatMessage], context: ChatContext) -> Iterator[ChatDelta]:
        yield ChatDelta(content=self.complete(messages, context).content)

    def complete(self, messages: list[ChatMessage], context: ChatContext) -> ChatResponse:
        self.calls.append(list(messages))
        if len(self.calls) == 1:
            return ChatResponse(
                content='{"type":"harness.tool_request/v1","tool":"read_file","arguments":{"path":"README.md"}}'
            )
        return ChatResponse(content="The README says: Demo Project.")


def test_parse_tool_request_accepts_only_structured_requests() -> None:
    request = parse_tool_request('{"type":"harness.tool_request/v1","tool":"repo_tree","arguments":{}}')

    assert request is not None
    assert request.tool == "repo_tree"
    assert parse_tool_request("hello") is None
    assert parse_tool_request('{"type":"other","tool":"repo_tree","arguments":{}}') is None


def test_chat_tool_specs_include_risk_and_confirmation_metadata() -> None:
    tools = default_chat_tools()

    assert tools
    assert tools["read_file"].spec.risk == "read"
    assert tools["read_file"].spec.requires_confirmation is False
    assert tools["edit_isolated"].spec.risk == "repo_mutation"
    assert tools["edit_isolated"].spec.requires_confirmation is True
    assert tools["edit_isolated"].spec.evidence_required is True


def test_chat_tools_cover_core_harness_domain_surfaces() -> None:
    tools = set(default_chat_tools())

    assert {
        "repo_tree",
        "read_file",
        "search_repo",
        "show_diff",
        "show_capabilities",
        "list_agents",
        "show_agent",
        "list_workbenches",
        "list_model_profiles",
        "list_tool_policies",
        "list_memory_scopes",
        "show_objectives",
        "show_task_graph",
        "show_leases",
        "show_registered_adapters",
        "show_approvals",
        "show_security_summary",
        "show_sandbox_profiles",
        "show_trace",
        "show_apply_back_state",
        "explain_blocked_state",
        "create_objective",
        "create_task",
        "create_task_graph",
        "request_approval",
        "dispatch_registered_adapter",
        "edit_isolated",
        "run_tests",
        "apply_back",
        "deny_apply_back",
        "revert_pending_change",
        "remember",
        "forget_memory",
    } <= tools


def test_chat_tool_read_file_returns_allowed_file(tmp_path) -> None:
    (tmp_path / "README.md").write_text("hello", encoding="utf-8")

    result = run_chat_tool(
        ChatToolRequest("harness.tool_request/v1", "read_file", {"path": "README.md"}),
        default_chat_tool_context(tmp_path),
    )

    assert result.ok is True
    assert result.content == "hello"
    assert result.data["path"] == "README.md"


def test_chat_tool_read_file_blocks_secret(tmp_path) -> None:
    (tmp_path / ".env").write_text("TOKEN=abcdef123456", encoding="utf-8")

    result = run_chat_tool(
        ChatToolRequest("harness.tool_request/v1", "read_file", {"path": ".env"}),
        default_chat_tool_context(tmp_path),
    )

    assert result.ok is False
    assert result.error_type == "secret_path"


def test_chat_tool_unknown_tool_rejected(tmp_path) -> None:
    result = run_chat_tool(
        ChatToolRequest("harness.tool_request/v1", "shell", {"cmd": "pwd"}),
        default_chat_tool_context(tmp_path),
    )

    assert result.ok is False
    assert result.error_type == "unknown_tool"


def test_side_effect_tool_specs_are_visible_but_not_executable(tmp_path) -> None:
    tools = default_chat_tools()

    assert tools["edit_isolated"].spec.requires_confirmation is True
    assert tools["run_tests"].spec.risk == "sandboxed_execution"
    assert tools["apply_back"].spec.risk == "repo_mutation"

    result = run_chat_tool(
        ChatToolRequest(
            "harness.tool_request/v1",
            "edit_isolated",
            {"goal": "fix the failing chat tests"},
        ),
        default_chat_tool_context(tmp_path),
    )

    assert result.ok is False
    assert result.error_type == "action_contract_required"
    assert result.data["type"] == "harness.action_contract_required/v1"
    assert result.data["tool"] == "edit_isolated"
    assert result.data["requires_confirmation"] is True


def test_search_repo_does_not_search_secret_like_paths(tmp_path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("needle in safe file", encoding="utf-8")
    (tmp_path / ".env").write_text("needle in secret file", encoding="utf-8")

    result = run_chat_tool(
        ChatToolRequest("harness.tool_request/v1", "search_repo", {"query": "needle"}),
        default_chat_tool_context(tmp_path),
    )

    assert result.ok is True
    assert "src/app.py" in result.content
    assert ".env" not in result.content
    assert "secret file" not in result.content


def test_chat_loop_can_process_tool_request_then_answer(tmp_path) -> None:
    (tmp_path / "README.md").write_text("# Demo Project\n", encoding="utf-8")
    model = ToolLoopModel()

    response = handle_chat_input("what does the readme say?", tmp_path, ChatSessionState(), chat_model=model)

    assert response["kind"] == "llm_chat"
    assert response["lines"] == ["The README says: Demo Project."]
    assert response["tool_results"] == [{"tool": "read_file", "ok": True, "error_type": None}]
    assert len(model.calls) == 2
    second_call_text = "\n".join(message.content for message in model.calls[1])
    assert "harness.tool_result/v1" in second_call_text
    assert "Demo Project" in second_call_text
