from __future__ import annotations

from collections.abc import Iterator

from harness.chat import ChatSessionState, handle_chat_input
from harness.chat_model import ChatContext, ChatDelta, ChatMessage, ChatResponse
from harness.chat_model import CodexCliChatModel
from harness.backends.local_openai import LocalEndpointUnavailable
from harness.config import default_config


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
    assert {block["kind"] for block in response["context_manifest"]["blocks"]} >= {
        "harness_vocabulary",
        "harness_state",
        "builtin_harness_domain",
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


def test_slash_commands_do_not_call_chat_model(tmp_path) -> None:
    class FailingChatModel(FakeChatModel):
        def complete(self, messages: list[ChatMessage], context: ChatContext) -> ChatResponse:
            raise AssertionError("slash commands should remain deterministic")

    response = handle_chat_input("/help", tmp_path, ChatSessionState(), chat_model=FailingChatModel())

    assert response["kind"] == "help"
    assert response["ok"] is True


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


def test_mutation_request_falls_back_to_action_contract_when_model_only_refuses(tmp_path) -> None:
    model = FakeChatModel("I can't create the file directly from this read-only chat turn.")
    state = ChatSessionState()

    response = handle_chat_input("create an empty file in the repository", tmp_path, state, chat_model=model)

    assert response["kind"] == "action_contract"
    assert response["contract"]["tool"] == "edit_isolated"
    assert response["contract"]["normalized_arguments"]["goal"] == "create an empty file in the repository"
    assert state.pending_action_contract is not None
    assert not (tmp_path / ".harness").exists()


def test_test_request_falls_back_to_run_tests_contract_when_model_only_refuses(tmp_path) -> None:
    model = FakeChatModel("I cannot run tests directly from read-only chat.")
    state = ChatSessionState()

    response = handle_chat_input("run the tests", tmp_path, state, chat_model=model)

    assert response["kind"] == "action_contract"
    assert response["contract"]["tool"] == "run_tests"
    assert response["contract"]["normalized_arguments"]["suggested_command"] == "pytest -q"
    assert state.pending_action_contract is not None
    assert not (tmp_path / ".harness").exists()
