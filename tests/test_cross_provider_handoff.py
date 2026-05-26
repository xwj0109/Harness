from __future__ import annotations

from typing import Any

import pytest

from harness.config import default_config
from harness.model_registry import build_model_descriptors, build_provider_descriptors
from harness.protocol_adapters import (
    AnthropicMessagesProtocolAdapter,
    BedrockConverseProtocolAdapter,
    GoogleGenerativeProtocolAdapter,
    OpenAIChatProtocolAdapter,
    OpenAIResponsesProtocolAdapter,
)
from harness.provider_content import CanonicalMessage, CanonicalMessagePart, canonical_part_from_provider_event
from harness.provider_events import ProviderEvent, ProviderEventKind, ProviderRequest


class HandoffFakeHttpClient:
    def __init__(self, stream_chunks: list[dict[str, Any]]) -> None:
        self.streams: list[dict[str, Any]] = []
        self.stream_chunks = stream_chunks

    def get_json(self, url: str, headers: dict[str, str], timeout: float) -> dict[str, Any]:
        raise AssertionError("handoff tests must not perform discovery")

    def post_json(self, url: str, headers: dict[str, str], payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        raise AssertionError("handoff tests must use streaming adapters only")

    def stream_sse_json(self, url: str, headers: dict[str, str], payload: dict[str, Any], timeout: float):
        self.streams.append({"url": url, "headers": headers, "payload": payload, "timeout": timeout})
        yield from self.stream_chunks


def _provider(provider_id: str):
    return next(provider for provider in build_provider_descriptors(default_config()) if provider.provider_id == provider_id)


def _model(provider_id: str):
    return next(
        model
        for model in build_model_descriptors(default_config())
        if model.provider_id == provider_id and model.source == "backend_config"
    )


def _local_provider(provider_id: str, endpoint: str):
    return _provider("local_openai_compatible").model_copy(update={"provider_id": provider_id, "endpoint": endpoint})


def _local_model(provider_id: str, protocol: str, *, api_id: str = "handoff-model", endpoint: str = "http://localhost:11434/v1"):
    return _model("local_openai_compatible").model_copy(
        update={
            "provider_id": provider_id,
            "model_id": api_id,
            "raw_model_ref": f"{provider_id}/{api_id}",
            "api_id": api_id,
            "protocol": protocol,
            "endpoint": endpoint,
            "tool_support": True,
            "reasoning_support": "native",
        }
    )


def _assistant_context_from_events(events: list[ProviderEvent]) -> list[CanonicalMessage]:
    parts: list[CanonicalMessagePart] = []
    for event in events:
        if event.kind not in {
            ProviderEventKind.MODEL_MESSAGE_DELTA,
            ProviderEventKind.REASONING_SUMMARY_DELTA,
            ProviderEventKind.TOOL_CALL_COMPLETED,
        }:
            continue
        canonical = canonical_part_from_provider_event(event)
        if canonical.part is not None:
            parts.append(canonical.part)
    return [CanonicalMessage(role="assistant", parts=parts)] if parts else []


def _stream_openai_chat_text() -> list[ProviderEvent]:
    client = HandoffFakeHttpClient(
        [
            {"choices": [{"delta": {"content": "chat text"}, "finish_reason": None}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        ]
    )
    adapter = OpenAIChatProtocolAdapter(http_client=client)
    provider = _provider("local_openai_compatible")
    model = _model("local_openai_compatible")
    return list(adapter.stream(provider, model, ProviderRequest(messages=[])))


def _stream_anthropic_text() -> list[ProviderEvent]:
    client = HandoffFakeHttpClient(
        [
            {"type": "message_start", "message": {"id": "msg_handoff"}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "anthropic text"}},
            {"type": "message_stop"},
        ]
    )
    adapter = AnthropicMessagesProtocolAdapter(http_client=client)
    provider = _local_provider("anthropic_handoff", "http://localhost:8010/v1")
    model = _local_model("anthropic_handoff", "anthropic_messages", endpoint="http://localhost:8010/v1")
    return list(adapter.stream(provider, model, ProviderRequest(messages=[])))


def _stream_openai_responses_text() -> list[ProviderEvent]:
    client = HandoffFakeHttpClient(
        [
            {"type": "response.created", "response": {"id": "resp_handoff"}},
            {"type": "response.output_text.delta", "delta": "responses text", "response_id": "resp_handoff"},
            {"type": "response.completed", "response": {"id": "resp_handoff"}},
        ]
    )
    adapter = OpenAIResponsesProtocolAdapter(http_client=client)
    provider = _provider("local_openai_compatible")
    model = _local_model("responses_handoff", "openai_responses")
    return list(adapter.stream(provider, model, ProviderRequest(messages=[])))


def _stream_google_text() -> list[ProviderEvent]:
    client = HandoffFakeHttpClient(
        [
            {"candidates": [{"content": {"parts": [{"text": "google text"}]}}]},
            {"candidates": [{"finishReason": "STOP"}]},
        ]
    )
    adapter = GoogleGenerativeProtocolAdapter(http_client=client)
    provider = _local_provider("google_handoff", "https://generativelanguage.googleapis.com/v1beta")
    model = _local_model("google_handoff", "google_generative", endpoint="https://generativelanguage.googleapis.com/v1beta")
    return list(adapter.stream(provider, model, ProviderRequest(messages=[])))


def test_openai_chat_context_replays_into_anthropic_serializer_offline() -> None:
    context = _assistant_context_from_events(_stream_openai_chat_text())
    client = HandoffFakeHttpClient([{"type": "message_stop"}])
    provider = _local_provider("anthropic_handoff", "http://localhost:8010/v1")
    model = _local_model("anthropic_handoff", "anthropic_messages", endpoint="http://localhost:8010/v1")

    events = list(AnthropicMessagesProtocolAdapter(http_client=client).stream(provider, model, ProviderRequest(canonical_messages=context)))

    assert events[-1].kind == ProviderEventKind.MODEL_COMPLETED
    assert client.streams[0]["payload"]["messages"][0] == {
        "role": "assistant",
        "content": [{"type": "text", "text": "chat text"}],
    }


def test_anthropic_context_replays_into_openai_responses_serializer_offline() -> None:
    context = _assistant_context_from_events(_stream_anthropic_text())
    client = HandoffFakeHttpClient([{"type": "response.completed", "response": {"id": "resp_target"}}])
    provider = _provider("local_openai_compatible")
    model = _local_model("responses_handoff", "openai_responses")

    events = list(OpenAIResponsesProtocolAdapter(http_client=client).stream(provider, model, ProviderRequest(canonical_messages=context)))

    assert events[-1].kind == ProviderEventKind.MODEL_COMPLETED
    assert client.streams[0]["payload"]["input"] == [
        {"role": "assistant", "content": [{"type": "input_text", "text": "anthropic text"}]}
    ]


def test_openai_responses_context_replays_into_google_serializer_offline() -> None:
    context = _assistant_context_from_events(_stream_openai_responses_text())
    client = HandoffFakeHttpClient([{"candidates": [{"finishReason": "STOP"}]}])
    provider = _local_provider("google_handoff", "https://generativelanguage.googleapis.com/v1beta")
    model = _local_model("google_handoff", "google_generative", endpoint="https://generativelanguage.googleapis.com/v1beta")

    events = list(GoogleGenerativeProtocolAdapter(http_client=client).stream(provider, model, ProviderRequest(canonical_messages=context)))

    assert events[-1].kind == ProviderEventKind.MODEL_COMPLETED
    assert client.streams[0]["payload"]["contents"] == [{"role": "model", "parts": [{"text": "responses text"}]}]


def test_google_context_replays_into_openai_chat_serializer_offline() -> None:
    context = _assistant_context_from_events(_stream_google_text())
    client = HandoffFakeHttpClient([{"choices": [{"delta": {}, "finish_reason": "stop"}]}])
    provider = _provider("local_openai_compatible")
    model = _model("local_openai_compatible")

    events = list(OpenAIChatProtocolAdapter(http_client=client).stream(provider, model, ProviderRequest(canonical_messages=context)))

    assert events[-1].kind == ProviderEventKind.MODEL_COMPLETED
    assert client.streams[0]["payload"]["messages"] == [{"role": "assistant", "content": "google text"}]


def test_google_reasoning_signature_replays_into_anthropic_serializer_without_dropping_signature() -> None:
    source_client = HandoffFakeHttpClient(
        [
            {
                "candidates": [
                    {"content": {"parts": [{"text": "signed thought", "thought": True, "thoughtSignature": "sig_google"}]}}
                ]
            },
            {"candidates": [{"finishReason": "STOP"}]},
        ]
    )
    source_provider = _local_provider("google_handoff", "https://generativelanguage.googleapis.com/v1beta")
    source_model = _local_model("google_handoff", "google_generative", endpoint="https://generativelanguage.googleapis.com/v1beta")
    context = _assistant_context_from_events(
        list(GoogleGenerativeProtocolAdapter(http_client=source_client).stream(source_provider, source_model, ProviderRequest(messages=[])))
    )
    assert context[0].parts[0].signature == "sig_google"
    target_client = HandoffFakeHttpClient([{"type": "message_stop"}])
    target_provider = _local_provider("anthropic_handoff", "http://localhost:8010/v1")
    target_model = _local_model("anthropic_handoff", "anthropic_messages", endpoint="http://localhost:8010/v1")

    events = list(AnthropicMessagesProtocolAdapter(http_client=target_client).stream(target_provider, target_model, ProviderRequest(canonical_messages=context)))

    assert events[-1].kind == ProviderEventKind.MODEL_COMPLETED
    assert target_client.streams[0]["payload"]["messages"][0]["content"] == [
        {"type": "thinking", "thinking": "signed thought", "signature": "sig_google"}
    ]


def test_anthropic_tool_call_id_replays_into_bedrock_serializer_without_dropping_id() -> None:
    source_client = HandoffFakeHttpClient(
        [
            {"type": "message_start", "message": {"id": "msg_handoff"}},
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "tool_use", "id": "toolu_handoff", "name": "read", "input": {"path": "README.md"}},
            },
            {"type": "content_block_stop", "index": 0},
            {"type": "message_stop"},
        ]
    )
    source_provider = _local_provider("anthropic_handoff", "http://localhost:8010/v1")
    source_model = _local_model("anthropic_handoff", "anthropic_messages", endpoint="http://localhost:8010/v1")
    context = _assistant_context_from_events(
        list(AnthropicMessagesProtocolAdapter(http_client=source_client).stream(source_provider, source_model, ProviderRequest(messages=[])))
    )
    assert context[0].parts[0].provider_native_id == "toolu_handoff"
    target_client = HandoffFakeHttpClient([{"messageStop": {"stopReason": "end_turn"}}])
    target_provider = _provider("bedrock")
    target_model = _model("bedrock")

    request = ProviderRequest(
        canonical_messages=context,
        metadata={"provider_credential": {"credential_kind": "api_key", "status": "configured", "api_key": "bedrock-bearer-token"}},
    )

    events = list(BedrockConverseProtocolAdapter(http_client=target_client).stream(target_provider, target_model, request))

    assert events[-1].kind == ProviderEventKind.MODEL_COMPLETED
    assert target_client.streams[0]["payload"]["messages"][0]["content"] == [
        {"toolUse": {"toolUseId": "toolu_handoff", "name": "read", "input": {"path": "README.md"}}}
    ]


def test_unsupported_handoff_part_fails_with_adapter_and_part_name() -> None:
    client = HandoffFakeHttpClient([{"choices": [{"delta": {}, "finish_reason": "stop"}]}])
    provider = _provider("local_openai_compatible")
    model = _model("local_openai_compatible")
    request = ProviderRequest(
        canonical_messages=[
            CanonicalMessage(
                role="user",
                parts=[CanonicalMessagePart(kind="image_input", data={"media_type": "image/png", "data": "aW1n"})],
            )
        ]
    )

    events = list(OpenAIChatProtocolAdapter(http_client=client).stream(provider, model, request))

    assert [event.kind for event in events] == [ProviderEventKind.MODEL_STARTED, ProviderEventKind.MODEL_FAILED]
    error = events[-1].payload["error"]
    assert error["error_type"] == "UnsupportedCanonicalPartError"
    assert "openai_chat" in error["message"]
    assert "image_input" in error["message"]
    assert client.streams == []
