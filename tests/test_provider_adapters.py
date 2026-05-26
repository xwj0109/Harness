from __future__ import annotations

from collections.abc import Iterator

from harness.backends.local_openai import BackendConfigError, LocalEndpointUnavailable
from harness.backends.streaming import BackendStreamEvent
from harness.chat_model import ChatContext, ChatDelta, ChatMessage, ChatResponse, ChatToolCall
from harness.provider_adapters import (
    BackendStreamProviderAdapter,
    ChatModelProviderAdapter,
    classify_provider_exception,
    provider_event_from_backend_stream_event,
)
from harness.provider_content import CanonicalMessage, CanonicalMessagePart
from harness.provider_events import ProviderCapabilities, ProviderEventKind, ProviderMessage, ProviderRequest


class FakeChatModel:
    def __init__(self, response: ChatResponse | Exception) -> None:
        self.response = response
        self.messages: list[list[ChatMessage]] = []
        self.contexts: list[ChatContext] = []

    def stream(self, messages: list[ChatMessage], context: ChatContext) -> Iterator[ChatDelta]:
        result = self.complete(messages, context)
        yield ChatDelta(content=result.content)

    def complete(self, messages: list[ChatMessage], context: ChatContext) -> ChatResponse:
        self.messages.append(messages)
        self.contexts.append(context)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def test_chat_model_provider_adapter_emits_generic_provider_events() -> None:
    model = FakeChatModel(
        ChatResponse(
            content="I will inspect README.",
            tool_calls=[ChatToolCall(id="call_read", name="read", arguments={"path": "README.md"})],
        )
    )
    adapter = ChatModelProviderAdapter(
        model,
        provider_id="local_openai_compatible",
        model_ref="local_openai_compatible/qwen",
        capabilities=ProviderCapabilities(supports_native_tools=True, context_window_tokens=8192),
    )
    request = ProviderRequest(
        session_id="sess_123",
        turn_id="turn_123",
        prompt_id="prompt_123",
        messages=[ProviderMessage(role="user", content="Inspect README")],
        context={"project_root": "/tmp/project", "mode": "act"},
    )

    events = list(adapter.stream(request))

    assert [event.kind for event in events] == [
        ProviderEventKind.MODEL_STARTED,
        ProviderEventKind.MODEL_MESSAGE_DELTA,
        ProviderEventKind.TOOL_CALL_STARTED,
        ProviderEventKind.TOOL_CALL_COMPLETED,
        ProviderEventKind.MODEL_COMPLETED,
    ]
    assert [event.sequence for event in events] == [1, 2, 3, 4, 5]
    assert events[0].provider_id == "local_openai_compatible"
    assert events[1].payload["delta"] == "I will inspect README."
    assert events[2].tool_call_id == "call_read"
    assert events[2].payload["arguments"] == {"path": "README.md"}
    assert events[0].store_payload()["no_hidden_fallback"] is True
    assert model.messages[0][0].content == "Inspect README"
    assert model.contexts[0].project_root == "/tmp/project"


def test_chat_model_provider_adapter_classifies_unavailable_errors_without_fallback() -> None:
    adapter = ChatModelProviderAdapter(FakeChatModel(LocalEndpointUnavailable("endpoint down")), provider_id="local")
    request = ProviderRequest(messages=[ProviderMessage(role="user", content="Hello")])

    events = list(adapter.stream(request))

    assert [event.kind for event in events] == [ProviderEventKind.MODEL_STARTED, ProviderEventKind.MODEL_FAILED]
    error = events[-1].payload["error"]
    assert error["category"] == "provider_unavailable"
    assert error["retryable"] is True
    assert error["hidden_provider_fallback"] is False
    assert error["no_hidden_fallback"] is True
    assert "endpoint down" in error["message"]


def test_chat_model_provider_adapter_fails_on_unsupported_canonical_parts() -> None:
    adapter = ChatModelProviderAdapter(FakeChatModel(ChatResponse(content="unused")), provider_id="local")
    request = ProviderRequest(
        canonical_messages=[
            CanonicalMessage(role="user", parts=[CanonicalMessagePart(kind="image_input", data={"ref": "artifact://img"})])
        ]
    )

    events = list(adapter.stream(request))

    assert [event.kind for event in events] == [ProviderEventKind.MODEL_STARTED, ProviderEventKind.MODEL_FAILED]
    error = events[-1].payload["error"]
    assert error["error_type"] == "UnsupportedCanonicalPartError"
    assert "image_input" in error["message"]


def test_backend_stream_events_normalize_to_provider_events() -> None:
    request = ProviderRequest(session_id="sess", turn_id="turn", provider_id="codex_cli")

    message = provider_event_from_backend_stream_event(
        BackendStreamEvent(type="message_delta", text="delta"),
        sequence=2,
        request=request,
    )
    reasoning = provider_event_from_backend_stream_event(
        BackendStreamEvent(type="reasoning_summary_delta", text="safe summary"),
        sequence=3,
        request=request,
    )
    tool = provider_event_from_backend_stream_event(
        BackendStreamEvent(type="tool_call", text="read", payload={"id": "call_1", "tool": "read"}),
        sequence=4,
        request=request,
    )
    usage = provider_event_from_backend_stream_event(
        BackendStreamEvent(type="token_usage", payload={"prompt_tokens": 10, "completion_tokens": 12, "total_tokens": 42}),
        sequence=5,
        request=request,
    )

    assert message.kind == ProviderEventKind.MODEL_MESSAGE_DELTA
    assert message.payload["delta"] == "delta"
    assert reasoning.kind == ProviderEventKind.REASONING_SUMMARY_DELTA
    assert tool.kind == ProviderEventKind.TOOL_CALL_STARTED
    assert tool.tool_call_id == "call_1"
    assert usage.kind == ProviderEventKind.TOKEN_USAGE_UPDATED
    assert usage.payload["total_tokens"] == 42
    assert usage.payload["input_tokens"] == 10
    assert usage.payload["output_tokens"] == 12
    assert usage.payload["normalized_usage"] == {
        "input_tokens": 10,
        "output_tokens": 12,
        "reasoning_tokens": None,
        "cache_read_tokens": None,
        "cache_write_tokens": None,
        "total_tokens": 42,
    }


def test_backend_stream_provider_adapter_wraps_stream_with_model_lifecycle() -> None:
    adapter = BackendStreamProviderAdapter(
        [
            BackendStreamEvent(type="message_delta", text="Hello"),
            BackendStreamEvent(type="token_usage", payload={"input_tokens": 1, "output_tokens": 2}),
        ],
        provider_id="streaming_backend",
        model_ref="streaming/model",
    )

    events = list(adapter.stream(ProviderRequest(messages=[ProviderMessage(role="user", content="Hello")])))

    assert [event.kind for event in events] == [
        ProviderEventKind.MODEL_STARTED,
        ProviderEventKind.MODEL_MESSAGE_DELTA,
        ProviderEventKind.TOKEN_USAGE_UPDATED,
        ProviderEventKind.MODEL_COMPLETED,
    ]
    assert all(event.provider_id == "streaming_backend" for event in events)
    assert events[0].payload["capabilities"]["supports_streaming"] is True
    assert events[2].payload["normalized_usage"] == {
        "input_tokens": 1,
        "output_tokens": 2,
        "reasoning_tokens": None,
        "cache_read_tokens": None,
        "cache_write_tokens": None,
        "total_tokens": 3,
    }


def test_backend_stream_error_events_are_classified_from_error_shape() -> None:
    event = provider_event_from_backend_stream_event(
        BackendStreamEvent(type="error", text="quota exhausted", payload={"error_type": "RESOURCE_EXHAUSTED"}),
        sequence=2,
        request=ProviderRequest(messages=[ProviderMessage(role="user", content="Hi")]),
    )

    assert event.kind == ProviderEventKind.MODEL_FAILED
    error = event.payload["error"]
    assert error["category"] == "rate_limit"
    assert error["retryable"] is True


def test_provider_exception_classifier_marks_config_errors_non_retryable() -> None:
    error = classify_provider_exception(BackendConfigError("bad config"))

    assert error.category.value == "configuration"
    assert error.retryable is False
    assert error.no_hidden_fallback is True


def test_provider_exception_classifier_uses_normalized_provider_error_terms() -> None:
    error = classify_provider_exception(RuntimeError("provider overloaded, try again later"))

    assert error.category.value == "server_unavailable"
    assert error.retryable is True
