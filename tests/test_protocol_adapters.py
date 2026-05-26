from __future__ import annotations

from pathlib import Path
import time
from typing import Any

import pytest

from harness.backends.streaming import BackendStreamEvent
from harness.config import default_config
from harness.model_registry import build_model_descriptors, build_provider_descriptors
from harness.protocol_adapters import (
    AnthropicMessagesProtocolAdapter,
    BedrockConverseProtocolAdapter,
    CodexCliProtocolAdapter,
    GoogleGenerativeProtocolAdapter,
    OpenAICodexResponsesProtocolAdapter,
    OpenAIChatProtocolAdapter,
    OpenAIResponsesProtocolAdapter,
    ProtocolAdapterNotFound,
    ProtocolAdapterRegistrationError,
    ProtocolAdapterRegistry,
    build_default_protocol_adapter_registry,
    protocol_adapter_missing_error,
)
from harness.provider_adapters import provider_event_from_backend_stream_event
from harness.provider_content import (
    CanonicalMessage,
    CanonicalMessagePart,
    UnsupportedCanonicalPartError,
    canonical_messages_from_provider_request,
    canonical_messages_to_openai_chat_payload,
    canonical_part_from_provider_event,
)
from harness.provider_events import (
    ProviderErrorCategory,
    ProviderEventKind,
    ProviderMessage,
    ProviderRequest,
    normalized_token_usage,
    provider_error_retryable_for,
    provider_retry_after_seconds_for,
)


class FakeOpenAIHttpClient:
    def __init__(self, stream_chunks: list[dict[str, Any]] | None = None) -> None:
        self.posts: list[dict[str, Any]] = []
        self.streams: list[dict[str, Any]] = []
        self.stream_chunks = stream_chunks or [
            {"choices": [{"delta": {"content": "local answer"}, "finish_reason": None}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 1, "completion_tokens": 2}},
        ]

    def get_json(self, url: str, headers: dict[str, str], timeout: float) -> dict[str, Any]:
        raise AssertionError("complete path should not perform model discovery")

    def post_json(
        self,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout: float,
    ) -> dict[str, Any]:
        self.posts.append({"url": url, "headers": headers, "payload": payload, "timeout": timeout})
        return {"choices": [{"message": {"content": "local answer"}}]}

    def stream_sse_json(
        self,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout: float,
    ):
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


def test_default_protocol_adapter_registry_exposes_initial_protocols() -> None:
    registry = build_default_protocol_adapter_registry()

    assert registry.has("anthropic_messages") is True
    assert registry.has("bedrock_converse") is True
    assert registry.has("codex_cli") is True
    assert registry.has("google_generative") is True
    assert registry.has("openai_chat") is True
    assert registry.has("openai_responses") is True
    assert registry.has("openai_codex_responses") is True
    assert registry.list_protocols() == [
        "anthropic_messages",
        "bedrock_converse",
        "codex_cli",
        "google_generative",
        "openai_chat",
        "openai_codex_responses",
        "openai_responses",
    ]
    assert registry.get("anthropic_messages").protocol == "anthropic_messages"
    assert registry.get("bedrock_converse").protocol == "bedrock_converse"
    assert registry.get("codex_cli").protocol == "codex_cli"
    assert registry.get("google_generative").protocol == "google_generative"
    assert registry.get("openai_chat").protocol == "openai_chat"
    assert registry.get("openai_responses").protocol == "openai_responses"
    assert registry.get("openai_codex_responses").protocol == "openai_codex_responses"
    assert registry.list_allowed_protocols() == [
        "anthropic_messages",
        "bedrock_converse",
        "codex_cli",
        "google_generative",
        "openai_chat",
        "openai_codex_responses",
        "openai_responses",
    ]
    with pytest.raises(ProtocolAdapterNotFound) as exc:
        registry.get("not_registered")
    assert exc.value.protocol == "not_registered"


def test_protocol_adapter_registry_can_register_custom_adapter() -> None:
    class CustomAdapter:
        protocol = "openai_responses"

        def stream(self, provider, model, request):
            yield from ()

    registry = ProtocolAdapterRegistry()
    registry.register(CustomAdapter())

    assert registry.has("openai_responses") is True
    assert registry.get("openai_responses").protocol == "openai_responses"
    assert registry.list_protocols() == ["openai_responses"]


def test_protocol_adapter_registry_rejects_protocols_outside_allowlist() -> None:
    class PluginAdapter:
        protocol = "plugin_runtime_protocol"

        def stream(self, provider, model, request):
            yield from ()

    registry = ProtocolAdapterRegistry()

    with pytest.raises(ProtocolAdapterRegistrationError) as exc:
        registry.register(PluginAdapter())

    assert exc.value.protocol == "plugin_runtime_protocol"
    assert exc.value.reason == "protocol_not_allowlisted"
    assert "openai_chat" in exc.value.allowed_protocols
    assert registry.has("plugin_runtime_protocol") is False
    assert registry.list_protocols() == []


def test_protocol_adapter_registry_rejects_missing_protocol() -> None:
    class MissingProtocolAdapter:
        protocol = ""

        def stream(self, provider, model, request):
            yield from ()

    registry = ProtocolAdapterRegistry()

    with pytest.raises(ProtocolAdapterRegistrationError) as exc:
        registry.register(MissingProtocolAdapter())

    assert exc.value.protocol == ""
    assert exc.value.reason == "protocol_missing"
    assert registry.list_protocols() == []


def test_streaming_protocol_adapters_emit_aborted_before_network() -> None:
    local_provider = _provider("local_openai_compatible")
    local_model = _model("local_openai_compatible")
    hosted_provider = local_provider.model_copy(update={"provider_id": "hosted", "endpoint": "https://api.example.test/v1"})
    cases = [
        (OpenAIChatProtocolAdapter(http_client=FakeOpenAIHttpClient()), local_provider, local_model.model_copy(update={"protocol": "openai_chat"})),
        (OpenAIResponsesProtocolAdapter(http_client=FakeOpenAIHttpClient()), hosted_provider, local_model.model_copy(update={"protocol": "openai_responses"})),
        (OpenAICodexResponsesProtocolAdapter(http_client=FakeOpenAIHttpClient()), hosted_provider, local_model.model_copy(update={"protocol": "openai_codex_responses"})),
        (AnthropicMessagesProtocolAdapter(http_client=FakeOpenAIHttpClient()), hosted_provider, local_model.model_copy(update={"protocol": "anthropic_messages"})),
        (GoogleGenerativeProtocolAdapter(http_client=FakeOpenAIHttpClient()), hosted_provider, local_model.model_copy(update={"protocol": "google_generative"})),
        (BedrockConverseProtocolAdapter(http_client=FakeOpenAIHttpClient()), _provider("bedrock"), _model("bedrock")),
    ]

    for adapter, provider, model in cases:
        client = adapter.http_client
        events = list(adapter.stream(provider, model, ProviderRequest(messages=[ProviderMessage(role="user", content="Hi")], context={"abort_requested": True})))

        assert [event.kind for event in events] == [ProviderEventKind.MODEL_STARTED, ProviderEventKind.MODEL_ABORTED]
        assert events[-1].payload["aborted"] is True
        assert events[-1].payload["error"]["category"] == "aborted"
        assert client.streams == []


def test_streaming_protocol_adapter_aborts_during_http_stream() -> None:
    provider = _provider("local_openai_compatible").model_copy(update={"endpoint": "https://api.example.test/v1"})
    model = _model("local_openai_compatible").model_copy(update={"protocol": "openai_responses"})
    client = FakeOpenAIHttpClient(
        stream_chunks=[
            {"type": "response.created", "response": {"id": "resp_abort"}},
            {"type": "response.output_text.delta", "delta": "should not arrive"},
        ]
    )
    checks = {"count": 0}

    def abort_checker() -> bool:
        checks["count"] += 1
        return checks["count"] >= 4

    events = list(
        OpenAIResponsesProtocolAdapter(http_client=client).stream(
            provider,
            model,
            ProviderRequest(
                messages=[ProviderMessage(role="user", content="Hi")],
                context={"abort_checker": abort_checker},
            ),
        )
    )

    assert client.streams
    assert [event.kind for event in events] == [ProviderEventKind.MODEL_STARTED, ProviderEventKind.MODEL_ABORTED]
    assert events[-1].payload["error"]["category"] == "aborted"
    assert events[-1].payload["aborted"] is True


def test_codex_cli_protocol_adapter_emits_aborted_before_backend_starts() -> None:
    created = []

    class FakeCodexBackend:
        def __init__(self, config) -> None:
            created.append(config)

        def stream_read_only_backend_events(self, project_root, prompt, final_message_path):
            raise AssertionError("aborted request should not start Codex backend streaming")

    adapter = CodexCliProtocolAdapter(backend_factory=FakeCodexBackend)

    events = list(adapter.stream(_provider("codex_cli"), _model("codex_cli"), ProviderRequest(messages=[ProviderMessage(role="user", content="Hi")], context={"abort_requested": True})))

    assert [event.kind for event in events] == [ProviderEventKind.MODEL_STARTED, ProviderEventKind.MODEL_ABORTED]
    assert events[-1].payload["error"]["category"] == "aborted"
    assert created == []


def test_streaming_protocol_adapter_deadline_controls_transport_timeout() -> None:
    provider = _provider("local_openai_compatible").model_copy(update={"endpoint": "https://api.example.test/v1"})
    model = _model("local_openai_compatible").model_copy(update={"protocol": "openai_responses"})
    client = FakeOpenAIHttpClient(stream_chunks=[{"type": "response.completed", "response": {"usage": {"input_tokens": 1}}}])
    request = ProviderRequest(
        messages=[ProviderMessage(role="user", content="Hi")],
        context={"stream_timeout_seconds": 0.25},
        metadata={"resolved_provider_options": {"timeout_seconds": 42}},
    )

    events = list(OpenAIResponsesProtocolAdapter(http_client=client).stream(provider, model, request))

    assert events[-1].kind == ProviderEventKind.MODEL_COMPLETED
    assert 0 < client.streams[0]["timeout"] <= 0.25


def test_streaming_protocol_adapter_expired_deadline_fails_before_network() -> None:
    provider = _provider("local_openai_compatible").model_copy(update={"endpoint": "https://api.example.test/v1"})
    model = _model("local_openai_compatible").model_copy(update={"protocol": "openai_responses"})
    client = FakeOpenAIHttpClient()
    request = ProviderRequest(messages=[ProviderMessage(role="user", content="Hi")], context={"stream_deadline_monotonic": time.monotonic() - 1})

    events = list(OpenAIResponsesProtocolAdapter(http_client=client).stream(provider, model, request))

    assert [event.kind for event in events] == [ProviderEventKind.MODEL_STARTED, ProviderEventKind.MODEL_FAILED]
    error = events[-1].payload["error"]
    assert error["error_type"] == "ProviderStreamTimeoutError"
    assert error["category"] == "provider_unavailable"
    assert error["retryable"] is True
    assert client.streams == []


def test_openai_responses_normalizes_provider_error_categories() -> None:
    provider = _provider("local_openai_compatible").model_copy(update={"endpoint": "https://api.example.test/v1"})
    model = _model("local_openai_compatible").model_copy(update={"protocol": "openai_responses"})
    cases = [
        ({"type": "authentication_error", "message": "invalid api key"}, "auth", False),
        ({"type": "rate_limit_exceeded", "message": "too many requests", "retry_after": "2.5"}, "rate_limit", True),
        ({"type": "insufficient_quota", "message": "quota exceeded; check billing"}, "rate_limit", False),
        ({"type": "context_length_exceeded", "message": "maximum context length exceeded"}, "context_overflow", True),
        ({"type": "invalid_request_error", "message": "bad request"}, "invalid_request", False),
        ({"type": "server_error", "message": "internal server error"}, "server_unavailable", True),
        ({"type": "content_policy_violation", "message": "blocked by safety policy"}, "provider_policy_block", False),
    ]

    for provider_error, category, retryable in cases:
        client = FakeOpenAIHttpClient(
            stream_chunks=[
                {
                    "type": "response.failed",
                    "response": {"status": "failed", "error": provider_error},
                }
            ]
        )

        events = list(OpenAIResponsesProtocolAdapter(http_client=client).stream(provider, model, ProviderRequest(messages=[ProviderMessage(role="user", content="Hi")])))

        assert [event.kind for event in events] == [ProviderEventKind.MODEL_STARTED, ProviderEventKind.MODEL_FAILED]
        error = events[-1].payload["error"]
        assert error["category"] == category
        assert error["retryable"] is retryable
        if provider_error.get("retry_after") is not None:
            assert error["retry_after_seconds"] == 2.5


def test_provider_error_retryable_classification_is_category_driven() -> None:
    assert provider_error_retryable_for(ProviderErrorCategory.RATE_LIMIT, "rate_limit_exceeded") is True
    assert provider_error_retryable_for(ProviderErrorCategory.SERVER_UNAVAILABLE, "503") is True
    assert provider_error_retryable_for(ProviderErrorCategory.UNAVAILABLE, "connection refused") is True
    assert provider_error_retryable_for(ProviderErrorCategory.CONTEXT_OVERFLOW, "context length exceeded") is True
    assert provider_error_retryable_for(ProviderErrorCategory.AUTH, "authentication_error") is False
    assert provider_error_retryable_for(ProviderErrorCategory.INVALID_REQUEST, "bad request") is False
    assert provider_error_retryable_for(ProviderErrorCategory.PROVIDER_POLICY_BLOCK, "safety") is False
    assert provider_error_retryable_for(ProviderErrorCategory.RATE_LIMIT, "insufficient_quota", "billing required") is False
    assert provider_retry_after_seconds_for({"headers": {"Retry-After": "3"}}) == 3.0
    assert provider_retry_after_seconds_for({"details": [{"retryDelay": "2s"}]}) == 2.0


def test_openai_chat_protocol_adapter_streams_local_backend_without_discovery() -> None:
    provider = _provider("local_openai_compatible")
    model = _model("local_openai_compatible")
    client = FakeOpenAIHttpClient()
    adapter = OpenAIChatProtocolAdapter(http_client=client)
    request = ProviderRequest(
        session_id="sess",
        turn_id="turn",
        prompt_id="prompt",
        messages=[ProviderMessage(role="user", content="Hello")],
    )

    events = list(adapter.stream(provider, model, request))

    assert [event.kind for event in events] == [
        ProviderEventKind.MODEL_STARTED,
        ProviderEventKind.MODEL_MESSAGE_DELTA,
        ProviderEventKind.TOKEN_USAGE_UPDATED,
        ProviderEventKind.MODEL_COMPLETED,
    ]
    assert [event.sequence for event in events] == [1, 2, 3, 4]
    assert all(event.provider_id == "local_openai_compatible" for event in events)
    assert all(event.model_ref == "local_openai_compatible/qwen3-coder:30b" for event in events)
    assert events[0].payload["protocol"] == "openai_chat"
    assert events[0].payload["canonical_model_ref"] == "local_openai_compatible/qwen3-coder:30b"
    assert events[0].payload["provider_execution_started"] is True
    assert events[0].payload["model_execution_started"] is True
    assert events[0].payload["hidden_provider_fallback"] is False
    assert events[0].payload["hidden_model_fallback"] is False
    assert events[1].text == "local answer"
    assert events[1].payload["delta"] == "local answer"
    assert events[1].payload["protocol"] == "openai_chat"
    assert events[2].payload["prompt_tokens"] == 1
    assert events[3].payload["finish_reason"] == "stop"
    assert client.posts == []
    assert client.streams == [
        {
            "url": "http://localhost:11434/v1/chat/completions",
            "headers": {"Content-Type": "application/json", "Authorization": "Bearer local"},
            "payload": {
                "model": "qwen3-coder:30b",
                "messages": [{"role": "user", "content": "Hello"}],
                "temperature": 0.2,
                "max_tokens": 4096,
                "stream": True,
            },
            "timeout": 300.0,
        }
    ]


def test_openai_chat_protocol_adapter_uses_resolved_variant_options() -> None:
    provider = _provider("local_openai_compatible")
    model = _model("local_openai_compatible")
    client = FakeOpenAIHttpClient()
    adapter = OpenAIChatProtocolAdapter(http_client=client)
    request = ProviderRequest(
        session_id="sess",
        turn_id="turn",
        prompt_id="prompt",
        messages=[ProviderMessage(role="user", content="Hello")],
        metadata={
            "resolved_provider_options": {"temperature": 0.2, "max_tokens": 4096, "timeout_seconds": 30},
            "resolved_model_options": {"temperature": 0.0, "max_tokens": 2048},
        },
    )

    events = list(adapter.stream(provider, model, request))

    assert [event.kind for event in events] == [
        ProviderEventKind.MODEL_STARTED,
        ProviderEventKind.MODEL_MESSAGE_DELTA,
        ProviderEventKind.TOKEN_USAGE_UPDATED,
        ProviderEventKind.MODEL_COMPLETED,
    ]
    assert client.streams[0]["timeout"] == 30.0
    assert client.streams[0]["payload"]["temperature"] == 0.0
    assert client.streams[0]["payload"]["max_tokens"] == 2048


def test_openai_chat_protocol_adapter_merges_headers_completion_tokens_and_reasoning() -> None:
    provider = _provider("local_openai_compatible")
    model = _model("local_openai_compatible").model_copy(
        update={"provider_options": {"compatibility": "openrouter"}}
    )
    client = FakeOpenAIHttpClient()
    adapter = OpenAIChatProtocolAdapter(http_client=client)
    request = ProviderRequest(
        session_id="sess",
        turn_id="turn",
        prompt_id="prompt",
        messages=[ProviderMessage(role="user", content="Hello")],
        metadata={
            "provider_credential": {"status": "configured", "api_key": "sk-runtime", "headers": {"X-Credential": "runtime"}},
            "resolved_provider_options": {
                "temperature": 0.2,
                "max_tokens": 4096,
                "timeout_seconds": 30,
                "headers": {"HTTP-Referer": "https://harness.local", "X-Provider": "provider"},
            },
            "resolved_model_options": {
                "max_completion_tokens": 512,
                "model_reasoning_effort": "high",
                "openrouter_reasoning": True,
                "headers": {"X-Provider": "model", "X-Model": "model"},
            },
        },
    )

    events = list(adapter.stream(provider, model, request))

    assert events[-1].kind == ProviderEventKind.MODEL_COMPLETED
    assert client.streams[0]["headers"] == {
        "HTTP-Referer": "https://harness.local",
        "X-Provider": "model",
        "X-Model": "model",
        "X-Credential": "runtime",
        "Content-Type": "application/json",
        "Authorization": "Bearer sk-runtime",
    }
    assert client.streams[0]["payload"] == {
        "model": "qwen3-coder:30b",
        "messages": [{"role": "user", "content": "Hello"}],
        "temperature": 0.2,
        "max_completion_tokens": 512,
        "reasoning": {"effort": "high"},
        "stream": True,
    }


def test_openai_chat_protocol_adapter_normalizes_tool_call_deltas_and_usage() -> None:
    provider = _provider("local_openai_compatible")
    model = _model("local_openai_compatible")
    client = FakeOpenAIHttpClient(
        stream_chunks=[
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {"name": "read", "arguments": '{"path":"README.md"}'},
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            },
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}], "usage": {"total_tokens": 5}},
        ]
    )
    adapter = OpenAIChatProtocolAdapter(http_client=client)

    events = list(adapter.stream(provider, model, ProviderRequest(messages=[ProviderMessage(role="user", content="Hello")])))

    assert [event.kind for event in events] == [
        ProviderEventKind.MODEL_STARTED,
        ProviderEventKind.TOOL_CALL_DELTA,
        ProviderEventKind.TOKEN_USAGE_UPDATED,
        ProviderEventKind.MODEL_COMPLETED,
    ]
    assert events[1].tool_call_id == "call_1"
    assert events[1].tool_name == "read"
    assert events[1].payload["arguments_delta"] == '{"path":"README.md"}'
    assert events[2].payload["total_tokens"] == 5
    assert events[3].payload["finish_reason"] == "tool_calls"


def test_usage_normalization_all_protocols() -> None:
    cases = [
        (
            {"prompt_tokens": 3, "completion_tokens": 4},
            {"input_tokens": 3, "output_tokens": 4, "reasoning_tokens": None, "cache_read_tokens": None, "cache_write_tokens": None, "total_tokens": 7},
        ),
        (
            {"input_tokens": 3, "output_tokens": 4, "input_tokens_details": {"cached_tokens": 2}, "output_tokens_details": {"reasoning_tokens": 1}},
            {"input_tokens": 3, "output_tokens": 4, "reasoning_tokens": 1, "cache_read_tokens": 2, "cache_write_tokens": None, "total_tokens": 7},
        ),
        (
            {"input_tokens": 8, "output_tokens": 11, "cache_creation_input_tokens": 5},
            {"input_tokens": 8, "output_tokens": 11, "reasoning_tokens": None, "cache_read_tokens": None, "cache_write_tokens": 5, "total_tokens": 19},
        ),
        (
            {"promptTokenCount": 5, "candidatesTokenCount": 7, "thoughtsTokenCount": 2, "totalTokenCount": 14},
            {"input_tokens": 5, "output_tokens": 7, "reasoning_tokens": 2, "cache_read_tokens": None, "cache_write_tokens": None, "total_tokens": 14},
        ),
        (
            {"inputTokens": 6, "outputTokens": 7, "cacheReadInputTokens": 4, "cacheWriteInputTokens": 3, "totalTokens": 20},
            {"input_tokens": 6, "output_tokens": 7, "reasoning_tokens": None, "cache_read_tokens": 4, "cache_write_tokens": 3, "total_tokens": 20},
        ),
    ]

    for payload, expected in cases:
        assert normalized_token_usage(payload) == expected


def test_estimated_cost_is_marked_estimated() -> None:
    provider = _provider("local_openai_compatible")
    model = _model("local_openai_compatible").model_copy(
        update={
            "protocol": "openai_responses",
            "api_id": "gpt-responses",
            "cost": {"currency": "USD", "input_per_1m": 1.0, "output_per_1m": 2.0, "cache_read_per_1m": 0.25},
        }
    )
    client = FakeOpenAIHttpClient(
        stream_chunks=[
            {
                "type": "response.completed",
                "response": {
                    "id": "resp_cost",
                    "usage": {
                        "input_tokens": 4,
                        "output_tokens": 5,
                        "input_tokens_details": {"cached_tokens": 2},
                    },
                },
            },
        ]
    )
    adapter = OpenAIResponsesProtocolAdapter(http_client=client)

    events = list(adapter.stream(provider, model, ProviderRequest(messages=[ProviderMessage(role="user", content="Hello")])))

    usage = next(event.payload for event in events if event.kind == ProviderEventKind.TOKEN_USAGE_UPDATED)
    assert usage["estimated_cost"] == {
        "currency": "USD",
        "total": 0.0000145,
        "estimated": True,
        "source": "model_descriptor_pricing",
        "pricing_unit": "per_1m_tokens",
        "input": 0.000004,
        "output": 0.00001,
        "cache_read": 0.0000005,
        "cache_write": None,
    }
    assert usage["estimated_cost_usd"] == 0.0000145
    assert "provider_reported_cost" not in usage


def test_provider_reported_cost_is_recorded_separately() -> None:
    provider = _provider("local_openai_compatible")
    model = _model("local_openai_compatible").model_copy(
        update={
            "protocol": "openai_responses",
            "api_id": "gpt-responses",
            "cost": {"currency": "USD", "input_per_1m": 1.0, "output_per_1m": 2.0},
        }
    )
    client = FakeOpenAIHttpClient(
        stream_chunks=[
            {
                "type": "response.completed",
                "response": {
                    "id": "resp_cost",
                    "usage": {
                        "input_tokens": 4,
                        "output_tokens": 5,
                        "cost": {"currency": "USD", "total": "0.006", "line_items": [{"kind": "provider"}]},
                    },
                },
            },
        ]
    )
    adapter = OpenAIResponsesProtocolAdapter(http_client=client)

    events = list(adapter.stream(provider, model, ProviderRequest(messages=[ProviderMessage(role="user", content="Hello")])))

    usage = next(event.payload for event in events if event.kind == ProviderEventKind.TOKEN_USAGE_UPDATED)
    assert usage["provider_reported_cost"] == {
        "currency": "USD",
        "total": 0.006,
        "line_items": [{"kind": "provider"}],
        "estimated": False,
        "source": "provider_usage_cost",
    }
    assert usage["estimated_cost"] == {
        "currency": "USD",
        "total": 0.000014,
        "estimated": True,
        "source": "model_descriptor_pricing",
        "pricing_unit": "per_1m_tokens",
        "input": 0.000004,
        "output": 0.00001,
        "cache_read": None,
        "cache_write": None,
    }
    assert usage["estimated_cost_usd"] == 0.000014
    assert "cost" not in usage
    assert usage["provider_reported_cost"]["total"] != usage["estimated_cost"]["total"]


def test_openai_chat_protocol_adapter_normalizes_legacy_function_call_delta() -> None:
    provider = _provider("local_openai_compatible")
    model = _model("local_openai_compatible")
    client = FakeOpenAIHttpClient(
        stream_chunks=[
            {
                "choices": [
                    {
                        "delta": {"function_call": {"name": "read", "arguments": '{"path":"README.md"}'}},
                        "finish_reason": None,
                    }
                ]
            },
            {"choices": [{"delta": {}, "finish_reason": "function_call"}]},
        ]
    )
    adapter = OpenAIChatProtocolAdapter(http_client=client)

    events = list(adapter.stream(provider, model, ProviderRequest(messages=[ProviderMessage(role="user", content="Hello")])))

    assert [event.kind for event in events] == [
        ProviderEventKind.MODEL_STARTED,
        ProviderEventKind.TOOL_CALL_DELTA,
        ProviderEventKind.MODEL_COMPLETED,
    ]
    assert events[1].tool_name == "read"
    assert events[1].payload["arguments_delta"] == '{"path":"README.md"}'
    assert events[2].payload["finish_reason"] == "function_call"


def test_canonical_messages_convert_to_openai_chat_payload_and_preserve_metadata() -> None:
    request = ProviderRequest(
        canonical_messages=[
            CanonicalMessage(
                role="user",
                parts=[
                    CanonicalMessagePart(kind="provider_metadata", provider_native_id="resp_123", metadata={"opaque": True}),
                    CanonicalMessagePart(kind="text", text="Hello"),
                ],
                provider_native_id="msg_123",
            )
        ]
    )

    canonical = canonical_messages_from_provider_request(request)
    payload = canonical_messages_to_openai_chat_payload(canonical)

    assert canonical[0].provider_native_id == "msg_123"
    assert canonical[0].parts[0].provider_native_id == "resp_123"
    assert payload == [{"role": "user", "content": "Hello"}]


def test_openai_chat_protocol_adapter_fails_visibly_for_unsupported_canonical_parts() -> None:
    provider = _provider("local_openai_compatible")
    model = _model("local_openai_compatible")
    adapter = OpenAIChatProtocolAdapter(http_client=FakeOpenAIHttpClient())
    request = ProviderRequest(
        canonical_messages=[
            CanonicalMessage(
                role="user",
                parts=[CanonicalMessagePart(kind="image_input", data={"ref": "artifact://img"})],
            )
        ]
    )

    events = list(adapter.stream(provider, model, request))

    assert [event.kind for event in events] == [ProviderEventKind.MODEL_STARTED, ProviderEventKind.MODEL_FAILED]
    error = events[-1].payload["error"]
    assert error["category"] == "invalid_response"
    assert error["error_type"] == "UnsupportedCanonicalPartError"
    assert "image_input" in error["message"]
    assert error["no_hidden_fallback"] is True


def test_canonical_stream_event_mapping_preserves_tool_and_reasoning_parts() -> None:
    request = ProviderRequest(provider_id="local", model_ref="local/model")
    reasoning_event = provider_event_from_backend_stream_event(
        BackendStreamEvent(type="reasoning_summary_delta", text="thinking", payload={"signature": "sig_123"}),
        sequence=1,
        request=request,
    )
    tool_event = provider_event_from_backend_stream_event(
        BackendStreamEvent(type="tool_call", payload={"id": "call_1", "tool": "read", "arguments": {"path": "README.md"}}),
        sequence=2,
        request=request,
    )

    reasoning = canonical_part_from_provider_event(reasoning_event)
    tool = canonical_part_from_provider_event(tool_event)

    assert reasoning.part is not None
    assert reasoning.part.kind == "reasoning"
    assert reasoning.part.text == "thinking"
    assert reasoning.part.signature == "sig_123"
    assert reasoning.part.metadata["signature"] == "sig_123"
    assert tool.part is not None
    assert tool.part.kind == "tool_call"
    assert tool.part.provider_native_id == "call_1"
    assert tool.part.data["tool_name"] == "read"


def test_canonical_openai_payload_raises_direct_unsupported_part_error() -> None:
    with pytest.raises(UnsupportedCanonicalPartError) as exc:
        canonical_messages_to_openai_chat_payload(
            [
                CanonicalMessage(
                    role="tool",
                    parts=[CanonicalMessagePart(kind="tool_result", text="done", provider_native_id="call_1")],
                )
            ]
        )

    assert exc.value.part_kind == "tool_result"
    assert exc.value.protocol == "openai_chat"


def test_openai_chat_protocol_adapter_classifies_backend_errors_without_fallback() -> None:
    provider = _provider("local_openai_compatible")
    model = _model("local_openai_compatible").model_copy(update={"endpoint": "https://api.example.test/v1"})
    adapter = OpenAIChatProtocolAdapter(http_client=FakeOpenAIHttpClient())

    request = ProviderRequest(messages=[ProviderMessage(role="user", content="Hi")])

    events = list(adapter.stream(provider, model, request))

    assert [event.kind for event in events] == [ProviderEventKind.MODEL_STARTED, ProviderEventKind.MODEL_FAILED]
    error = events[-1].payload["error"]
    assert error["category"] == "configuration"
    assert error["hidden_provider_fallback"] is False
    assert error["no_hidden_fallback"] is True


def test_openai_responses_protocol_adapter_streams_text_reasoning_tool_and_usage() -> None:
    provider = _provider("local_openai_compatible")
    model = _model("local_openai_compatible").model_copy(
        update={
            "protocol": "openai_responses",
            "api_id": "gpt-responses",
            "reasoning_support": "effort",
            "cost": {"input_per_1m": 1.0, "output_per_1m": 2.0, "cache_read_per_1m": 0.25},
        }
    )
    client = FakeOpenAIHttpClient(
        stream_chunks=[
            {"type": "response.created", "response": {"id": "resp_123"}},
            {"type": "response.reasoning_summary_text.delta", "delta": "thinking", "response_id": "resp_123"},
            {"type": "response.output_text.delta", "delta": "Hello", "response_id": "resp_123"},
            {
                "type": "response.function_call_arguments.delta",
                "call_id": "call_1",
                "delta": '{"path":"README.md"}',
                "response_id": "resp_123",
            },
            {
                "type": "response.output_item.done",
                "item": {"type": "function_call", "call_id": "call_1", "name": "read", "arguments": '{"path":"README.md"}'},
                "response_id": "resp_123",
            },
            {
                "type": "response.completed",
                "response": {
                    "id": "resp_123",
                    "usage": {
                        "input_tokens": 4,
                        "output_tokens": 5,
                        "input_tokens_details": {"cached_tokens": 2},
                    },
                },
            },
        ]
    )
    adapter = OpenAIResponsesProtocolAdapter(http_client=client)
    request = ProviderRequest(
        messages=[ProviderMessage(role="user", content="Hello")],
        metadata={
            "resolved_provider_options": {"timeout_seconds": 42},
            "resolved_model_options": {"max_tokens": 123, "model_reasoning_effort": "medium"},
        },
    )

    events = list(adapter.stream(provider, model, request))

    assert [event.kind for event in events] == [
        ProviderEventKind.MODEL_STARTED,
        ProviderEventKind.REASONING_SUMMARY_DELTA,
        ProviderEventKind.MODEL_MESSAGE_DELTA,
        ProviderEventKind.TOOL_CALL_DELTA,
        ProviderEventKind.TOOL_CALL_COMPLETED,
        ProviderEventKind.TOKEN_USAGE_UPDATED,
        ProviderEventKind.MODEL_COMPLETED,
    ]
    assert events[1].payload["response_id"] == "resp_123"
    assert events[1].text == "thinking"
    assert events[2].text == "Hello"
    assert events[3].tool_call_id == "call_1"
    assert events[3].payload["arguments_delta"] == '{"path":"README.md"}'
    assert events[4].tool_name == "read"
    assert events[5].payload["input_tokens"] == 4
    assert events[5].payload["normalized_usage"] == {
        "input_tokens": 4,
        "output_tokens": 5,
        "reasoning_tokens": None,
        "cache_read_tokens": 2,
        "cache_write_tokens": None,
        "total_tokens": 9,
    }
    assert events[5].payload["estimated_cost"] == {
        "currency": "USD",
        "total": 0.0000145,
        "estimated": True,
        "source": "model_descriptor_pricing",
        "pricing_unit": "per_1m_tokens",
        "input": 0.000004,
        "output": 0.00001,
        "cache_read": 0.0000005,
        "cache_write": None,
    }
    assert events[5].payload["estimated_cost_usd"] == 0.0000145
    assert events[6].payload["finish_reason"] == "stop"
    assert client.streams == [
        {
            "url": "http://localhost:11434/v1/responses",
            "headers": {"Content-Type": "application/json", "Authorization": "Bearer local"},
            "payload": {
                "model": "gpt-responses",
                "input": [{"role": "user", "content": [{"type": "input_text", "text": "Hello"}]}],
                "stream": True,
                "max_output_tokens": 123,
                "reasoning": {"effort": "medium"},
            },
            "timeout": 42.0,
        }
    ]


def test_openai_responses_function_call_arguments_accumulate() -> None:
    provider = _provider("local_openai_compatible")
    model = _model("local_openai_compatible").model_copy(
        update={"protocol": "openai_responses", "api_id": "gpt-responses"}
    )
    client = FakeOpenAIHttpClient(
        stream_chunks=[
            {
                "type": "response.created",
                "response": {"id": "resp_456", "status": "in_progress", "background": False},
            },
            {
                "type": "response.function_call_arguments.delta",
                "item_id": "call_1",
                "delta": '{"path"',
                "response_id": "resp_456",
            },
            {
                "type": "response.function_call_arguments.delta",
                "item_id": "call_1",
                "delta": ':"README.md"}',
                "response_id": "resp_456",
            },
            {
                "type": "response.output_item.done",
                "item": {"type": "function_call", "id": "call_1", "name": "read"},
                "response_id": "resp_456",
            },
            {
                "type": "response.completed",
                "response": {"id": "resp_456", "status": "completed", "usage": {"input_tokens": 1, "output_tokens": 2}},
            },
        ]
    )
    adapter = OpenAIResponsesProtocolAdapter(http_client=client)

    events = list(adapter.stream(provider, model, ProviderRequest(messages=[ProviderMessage(role="user", content="Read")])) )

    assert [event.kind for event in events] == [
        ProviderEventKind.MODEL_STARTED,
        ProviderEventKind.TOOL_CALL_DELTA,
        ProviderEventKind.TOOL_CALL_DELTA,
        ProviderEventKind.TOOL_CALL_COMPLETED,
        ProviderEventKind.TOKEN_USAGE_UPDATED,
        ProviderEventKind.MODEL_COMPLETED,
    ]
    assert events[1].payload["response_id"] == "resp_456"
    assert events[1].payload["status"] == "in_progress"
    assert events[1].payload["background"] is False
    assert events[3].tool_call_id == "call_1"
    assert events[3].tool_name == "read"
    assert events[3].payload["arguments"] == '{"path":"README.md"}'
    assert events[4].payload["response_id"] == "resp_456"
    assert events[4].payload["status"] == "completed"
    assert events[5].payload["finish_reason"] == "stop"


def test_openai_responses_refusal_and_error_details_are_preserved() -> None:
    provider = _provider("local_openai_compatible")
    model = _model("local_openai_compatible").model_copy(
        update={"protocol": "openai_responses", "api_id": "gpt-responses"}
    )
    refusal_client = FakeOpenAIHttpClient(
        stream_chunks=[
            {"type": "response.created", "response": {"id": "resp_refusal", "status": "in_progress"}},
            {"type": "response.refusal.delta", "delta": "I cannot help.", "response_id": "resp_refusal"},
            {
                "type": "response.completed",
                "response": {
                    "id": "resp_refusal",
                    "status": "completed",
                    "incomplete_details": {"reason": "content_filter"},
                },
            },
        ]
    )
    adapter = OpenAIResponsesProtocolAdapter(http_client=refusal_client)

    refusal_events = list(adapter.stream(provider, model, ProviderRequest(messages=[ProviderMessage(role="user", content="No")])) )

    assert refusal_events[1].kind == ProviderEventKind.MODEL_MESSAGE_DELTA
    assert refusal_events[1].payload["refusal"] is True
    assert refusal_events[1].payload["response_id"] == "resp_refusal"
    assert refusal_events[-1].payload["incomplete_details"] == {"reason": "content_filter"}

    error_client = FakeOpenAIHttpClient(
        stream_chunks=[
            {
                "type": "response.failed",
                "response": {
                    "id": "resp_failed",
                    "status": "failed",
                    "error": {"type": "rate_limit_exceeded", "message": "slow down"},
                },
            }
        ]
    )
    adapter = OpenAIResponsesProtocolAdapter(http_client=error_client)

    error_events = list(adapter.stream(provider, model, ProviderRequest(messages=[ProviderMessage(role="user", content="Hi")])) )

    assert [event.kind for event in error_events] == [ProviderEventKind.MODEL_STARTED, ProviderEventKind.MODEL_FAILED]
    error = error_events[-1].payload["error"]
    assert error["category"] == "rate_limit"
    assert error["error_type"] == "rate_limit_exceeded"
    assert error["message"] == "slow down"
    assert error["retryable"] is True


def test_openai_codex_responses_protocol_adapter_uses_codex_protocol_id(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-runtime")
    provider = _provider("paid_openai_compatible")
    model = _model("paid_openai_compatible")
    assert model.protocol == "openai_codex_responses"
    client = FakeOpenAIHttpClient(
        stream_chunks=[
            {"type": "response.output_text.delta", "delta": "codex"},
            {"type": "response.completed", "response": {"id": "resp_codex"}},
        ]
    )
    adapter = OpenAICodexResponsesProtocolAdapter(http_client=client)

    events = list(adapter.stream(provider, model, ProviderRequest(messages=[ProviderMessage(role="user", content="Inspect")])))

    assert events[0].payload["protocol"] == "openai_codex_responses"
    assert events[1].payload["protocol"] == "openai_codex_responses"
    assert events[1].text == "codex"
    assert client.streams[0]["url"] == "https://api.openai.com/v1/responses"
    assert client.streams[0]["headers"]["Authorization"] == "Bearer sk-test-runtime"


def test_openai_codex_responses_protocol_adapter_separates_codex_metadata(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-runtime")
    provider = _provider("paid_openai_compatible")
    model = _model("paid_openai_compatible")
    client = FakeOpenAIHttpClient(
        stream_chunks=[
            {"type": "response.completed", "response": {"id": "resp_codex_metadata"}},
        ]
    )
    adapter = OpenAICodexResponsesProtocolAdapter(http_client=client)
    request = ProviderRequest(
        messages=[ProviderMessage(role="user", content="Inspect")],
        metadata={
            "resolved_provider_options": {
                "timeout_seconds": 25,
                "metadata": {"route": "codex"},
                "codex_approval_policy": "on-request",
            },
            "resolved_model_options": {
                "max_tokens": 2048,
                "model_reasoning_effort": "high",
                "codex_sandbox": "workspace-write",
                "codex_tool_policy": "read-only",
                "metadata": {"task": "repo-inspection"},
            },
        },
    )

    events = list(adapter.stream(provider, model, request))

    assert [event.kind for event in events] == [ProviderEventKind.MODEL_STARTED, ProviderEventKind.MODEL_COMPLETED]
    assert client.streams[0]["payload"] == {
        "model": "gpt-5.3-codex",
        "input": [{"role": "user", "content": [{"type": "input_text", "text": "Inspect"}]}],
        "stream": True,
        "max_output_tokens": 2048,
        "reasoning": {"effort": "high"},
        "metadata": {
            "route": "codex",
            "task": "repo-inspection",
            "harness_protocol": "openai_codex_responses",
            "harness_reasoning_effort": "high",
            "harness_approval_policy": "on-request",
            "harness_sandbox": "workspace-write",
            "harness_tool_policy": "read-only",
        },
    }
    assert "codex_approval_policy" not in client.streams[0]["payload"]
    assert "codex_sandbox" not in client.streams[0]["payload"]
    assert client.streams[0]["timeout"] == 25.0


def test_openai_chat_adapter_does_not_use_local_placeholder_for_hosted_provider(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    provider = _provider("paid_openai_compatible").model_copy(update={"enabled": True})
    model = _model("paid_openai_compatible").model_copy(update={"protocol": "openai_chat"})
    client = FakeOpenAIHttpClient()
    adapter = OpenAIChatProtocolAdapter(http_client=client)

    request = ProviderRequest(messages=[ProviderMessage(role="user", content="Hi")])

    events = list(adapter.stream(provider, model, request))

    assert [event.kind for event in events] == [ProviderEventKind.MODEL_STARTED, ProviderEventKind.MODEL_FAILED]
    error = events[-1].payload["error"]
    assert error["category"] == "configuration"
    assert error["error_type"] == "ProviderCredentialResolutionError"
    assert "credential_missing" in error["message"] or "credential" in error["message"]
    assert client.streams == []
    assert client.posts == []


def test_openai_chat_adapter_trusts_runtime_credential_metadata_without_local_fallback() -> None:
    provider = _provider("local_openai_compatible")
    model = _model("local_openai_compatible")
    client = FakeOpenAIHttpClient()
    adapter = OpenAIChatProtocolAdapter(http_client=client)
    request = ProviderRequest(
        messages=[ProviderMessage(role="user", content="Hi")],
        metadata={
            "provider_credential": {
                "schema_version": "harness.resolved_provider_credential/v1",
                "provider_id": "local_openai_compatible",
                "credential_kind": "static_local",
                "status": "configured",
                "source": "static_local",
                "credentials_included": False,
            }
        },
    )

    events = list(adapter.stream(provider, model, request))

    assert [event.kind for event in events] == [ProviderEventKind.MODEL_STARTED, ProviderEventKind.MODEL_FAILED]
    error = events[-1].payload["error"]
    assert error["category"] == "configuration"
    assert error["error_type"] == "ProviderCredentialResolutionError"
    assert "credential_missing" in error["message"] or "credential" in error["message"]
    assert client.streams == []
    assert client.posts == []


def test_anthropic_messages_protocol_adapter_streams_text_thinking_tool_and_usage() -> None:
    provider = _provider("local_openai_compatible").model_copy(
        update={"provider_id": "anthropic_local", "endpoint": "http://localhost:8010/v1"}
    )
    model = _model("local_openai_compatible").model_copy(
        update={
            "provider_id": "anthropic_local",
            "model_id": "claude-test",
            "raw_model_ref": "anthropic_local/claude-test",
            "api_id": "claude-test",
            "protocol": "anthropic_messages",
            "endpoint": "http://localhost:8010/v1",
            "reasoning_support": "tokens",
            "tool_support": True,
            "max_output_tokens": 1024,
        }
    )
    client = FakeOpenAIHttpClient(
        stream_chunks=[
            {"type": "message_start", "message": {"id": "msg_123", "usage": {"input_tokens": 8}}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking"}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "thinking"}},
            {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "Hello"}},
            {
                "type": "content_block_start",
                "index": 2,
                "content_block": {"type": "tool_use", "id": "toolu_1", "name": "read", "input": {}},
            },
            {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": '{"path":"README.md"}'}},
            {"type": "content_block_stop", "index": 2},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 11}},
            {"type": "message_stop"},
        ]
    )
    adapter = AnthropicMessagesProtocolAdapter(http_client=client)
    request = ProviderRequest(
        canonical_messages=[
            CanonicalMessage(role="system", parts=[CanonicalMessagePart(kind="text", text="Be concise.")]),
            CanonicalMessage(role="user", parts=[CanonicalMessagePart(kind="text", text="Hello")]),
            CanonicalMessage(
                role="tool",
                parts=[CanonicalMessagePart(kind="tool_result", text="file body", provider_native_id="toolu_prev")],
            ),
        ],
        metadata={"resolved_provider_options": {"timeout_seconds": 12}, "resolved_model_options": {"max_tokens": 256, "cache_retention": "ephemeral"}},
    )

    events = list(adapter.stream(provider, model, request))

    assert [event.kind for event in events] == [
        ProviderEventKind.MODEL_STARTED,
        ProviderEventKind.TOKEN_USAGE_UPDATED,
        ProviderEventKind.REASONING_SUMMARY_DELTA,
        ProviderEventKind.MODEL_MESSAGE_DELTA,
        ProviderEventKind.TOOL_CALL_STARTED,
        ProviderEventKind.TOOL_CALL_DELTA,
        ProviderEventKind.TOOL_CALL_COMPLETED,
        ProviderEventKind.TOKEN_USAGE_UPDATED,
        ProviderEventKind.MODEL_COMPLETED,
    ]
    assert events[1].payload["message_id"] == "msg_123"
    assert events[1].payload["input_tokens"] == 8
    assert events[2].text == "thinking"
    assert events[3].text == "Hello"
    assert events[4].tool_call_id == "toolu_1"
    assert events[4].tool_name == "read"
    assert events[5].payload["arguments_delta"] == '{"path":"README.md"}'
    assert events[6].payload["arguments"] == '{"path":"README.md"}'
    assert events[7].payload["output_tokens"] == 11
    assert events[8].payload["finish_reason"] == "tool_use"
    assert client.streams == [
        {
            "url": "http://localhost:8010/v1/messages",
            "headers": {"Content-Type": "application/json", "x-api-key": "local", "anthropic-version": "2023-06-01"},
            "payload": {
                "model": "claude-test",
                "messages": [
                    {"role": "user", "content": [{"type": "text", "text": "Hello", "cache_control": {"type": "ephemeral"}}]},
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "toolu_prev",
                                "content": "file body",
                                "cache_control": {"type": "ephemeral"},
                            }
                        ],
                    },
                ],
                "max_tokens": 256,
                "stream": True,
                "system": "Be concise.",
            },
            "timeout": 12.0,
        }
    ]


def test_anthropic_beta_headers_from_model_metadata() -> None:
    provider = _provider("local_openai_compatible").model_copy(
        update={"provider_id": "anthropic_local", "endpoint": "http://localhost:8010/v1"}
    )
    model = _model("local_openai_compatible").model_copy(
        update={
            "provider_id": "anthropic_local",
            "model_id": "claude-test",
            "raw_model_ref": "anthropic_local/claude-test",
            "api_id": "claude-test",
            "protocol": "anthropic_messages",
            "endpoint": "http://localhost:8010/v1",
            "reasoning_support": "tokens",
        }
    )
    client = FakeOpenAIHttpClient(
        stream_chunks=[
            {"type": "message_stop"},
        ]
    )
    adapter = AnthropicMessagesProtocolAdapter(http_client=client)
    request = ProviderRequest(
        messages=[ProviderMessage(role="user", content="Hello")],
        metadata={
            "resolved_provider_options": {
                "timeout_seconds": 12,
                "anthropic_beta": "tools-2024-04-04",
            },
            "resolved_model_options": {
                "anthropic_beta_headers": ["prompt-caching-2024-07-31", "fine-grained-tool-streaming-2025-05-14"],
                "thinking_budget_tokens": 1024,
                "cache_retention": "1h",
            },
        },
    )

    events = list(adapter.stream(provider, model, request))

    assert [event.kind for event in events] == [ProviderEventKind.MODEL_STARTED, ProviderEventKind.MODEL_COMPLETED]
    assert client.streams[0]["headers"] == {
        "Content-Type": "application/json",
        "x-api-key": "local",
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "tools-2024-04-04,prompt-caching-2024-07-31,fine-grained-tool-streaming-2025-05-14",
    }
    assert client.streams[0]["payload"] == {
        "model": "claude-test",
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "Hello",
                        "cache_control": {"type": "ephemeral", "ttl": "1h"},
                    }
                ],
            }
        ],
        "max_tokens": 4096,
        "stream": True,
        "thinking": {"type": "enabled", "budget_tokens": 1024},
    }


def test_anthropic_messages_protocol_adapter_reports_stream_error() -> None:
    provider = _provider("local_openai_compatible").model_copy(update={"endpoint": "http://localhost:8010/v1"})
    model = _model("local_openai_compatible").model_copy(update={"protocol": "anthropic_messages"})
    client = FakeOpenAIHttpClient(
        stream_chunks=[
            {"type": "error", "error": {"type": "overloaded_error", "message": "try again later"}},
        ]
    )
    adapter = AnthropicMessagesProtocolAdapter(http_client=client)

    events = list(adapter.stream(provider, model, ProviderRequest(messages=[ProviderMessage(role="user", content="Hi")])))

    assert [event.kind for event in events] == [ProviderEventKind.MODEL_STARTED, ProviderEventKind.MODEL_FAILED]
    error = events[-1].payload["error"]
    assert error["category"] == "server_unavailable"
    assert error["error_type"] == "overloaded_error"
    assert error["message"] == "try again later"
    assert error["retryable"] is True
    assert error["no_hidden_fallback"] is True


def test_anthropic_messages_protocol_adapter_fails_visibly_for_unsupported_parts() -> None:
    provider = _provider("local_openai_compatible").model_copy(update={"endpoint": "http://localhost:8010/v1"})
    model = _model("local_openai_compatible").model_copy(update={"protocol": "anthropic_messages"})
    adapter = AnthropicMessagesProtocolAdapter(http_client=FakeOpenAIHttpClient())
    request = ProviderRequest(
        canonical_messages=[
            CanonicalMessage(
                role="user",
                parts=[CanonicalMessagePart(kind="image_input", data={"ref": "artifact://img"})],
            )
        ]
    )

    events = list(adapter.stream(provider, model, request))

    assert [event.kind for event in events] == [ProviderEventKind.MODEL_STARTED, ProviderEventKind.MODEL_FAILED]
    error = events[-1].payload["error"]
    assert error["category"] == "invalid_response"
    assert error["error_type"] == "UnsupportedCanonicalPartError"
    assert "image_input" in error["message"]
    assert error["no_hidden_fallback"] is True


def test_google_generative_protocol_adapter_streams_text_thought_tool_and_usage() -> None:
    provider = _provider("local_openai_compatible").model_copy(
        update={"provider_id": "google", "endpoint": "https://generativelanguage.googleapis.com/v1beta"}
    )
    model = _model("local_openai_compatible").model_copy(
        update={
            "provider_id": "google",
            "model_id": "gemini-test",
            "raw_model_ref": "google/gemini-test",
            "api_id": "gemini-test",
            "protocol": "google_generative",
            "endpoint": "https://generativelanguage.googleapis.com/v1beta",
            "reasoning_support": "tokens",
            "tool_support": True,
            "model_options": {"thinking_budget_tokens": 128},
            "max_output_tokens": 1024,
        }
    )
    client = FakeOpenAIHttpClient(
        stream_chunks=[
            {
                "usageMetadata": {"promptTokenCount": 5},
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"text": "thinking", "thought": True, "thoughtSignature": "sig_thought_123"},
                                {"text": "Hello"},
                                {"functionCall": {"name": "read", "args": {"path": "README.md"}}},
                            ]
                        }
                    }
                ],
            },
            {"usageMetadata": {"candidatesTokenCount": 7}, "candidates": [{"finishReason": "STOP"}]},
        ]
    )
    adapter = GoogleGenerativeProtocolAdapter(http_client=client)
    request = ProviderRequest(
        canonical_messages=[
            CanonicalMessage(role="system", parts=[CanonicalMessagePart(kind="text", text="Be concise.")]),
            CanonicalMessage(
                role="user",
                parts=[
                    CanonicalMessagePart(kind="text", text="Hello"),
                    CanonicalMessagePart(kind="image_input", data={"media_type": "image/png", "data": "aW1n"}),
                ],
            ),
            CanonicalMessage(
                role="assistant",
                parts=[
                    CanonicalMessagePart(kind="reasoning", text="prior thought", signature="sig_prev"),
                    CanonicalMessagePart(kind="tool_call", provider_native_id="call_prev", data={"tool_name": "read", "arguments": {"path": "README.md"}}),
                ],
            ),
            CanonicalMessage(
                role="tool",
                parts=[CanonicalMessagePart(kind="tool_result", text="file body", data={"tool_name": "read"})],
            ),
        ],
        metadata={"resolved_provider_options": {"timeout_seconds": 15}, "resolved_model_options": {"max_tokens": 256}},
    )

    events = list(adapter.stream(provider, model, request))

    assert [event.kind for event in events] == [
        ProviderEventKind.MODEL_STARTED,
        ProviderEventKind.TOKEN_USAGE_UPDATED,
        ProviderEventKind.REASONING_SUMMARY_DELTA,
        ProviderEventKind.MODEL_MESSAGE_DELTA,
        ProviderEventKind.TOOL_CALL_COMPLETED,
        ProviderEventKind.TOKEN_USAGE_UPDATED,
        ProviderEventKind.MODEL_COMPLETED,
    ]
    assert events[1].payload["promptTokenCount"] == 5
    assert events[2].text == "thinking"
    assert events[2].payload["thought_signature"] == "sig_thought_123"
    assert events[2].payload["signature"] == "sig_thought_123"
    assert events[3].text == "Hello"
    assert events[4].tool_call_id == "google_tool_1"
    assert events[4].tool_name == "read"
    assert events[4].payload["arguments"] == {"path": "README.md"}
    assert events[5].payload["candidatesTokenCount"] == 7
    assert events[5].payload["normalized_usage"]["output_tokens"] == 7
    assert events[5].payload["normalized_usage"]["total_tokens"] == 7
    assert events[6].payload["finish_reason"] == "stop"
    assert client.streams == [
        {
            "url": "https://generativelanguage.googleapis.com/v1beta/models/gemini-test:streamGenerateContent?alt=sse",
            "headers": {"Content-Type": "application/json", "x-goog-api-key": "local"},
            "payload": {
                "contents": [
                    {
                        "role": "user",
                        "parts": [
                            {"text": "Hello"},
                            {"inlineData": {"mimeType": "image/png", "data": "aW1n"}},
                        ],
                    },
                    {
                        "role": "model",
                        "parts": [
                            {"text": "prior thought", "thought": True, "thoughtSignature": "sig_prev"},
                            {"functionCall": {"name": "read", "args": {"path": "README.md"}}},
                        ],
                    },
                    {
                        "role": "user",
                        "parts": [{"functionResponse": {"name": "read", "response": {"output": "file body"}}}],
                    },
                ],
                "systemInstruction": {"parts": [{"text": "Be concise."}]},
                "generationConfig": {"maxOutputTokens": 256, "thinkingConfig": {"thinkingBudget": 128}},
            },
            "timeout": 15.0,
        }
    ]


def test_google_generative_protocol_adapter_uses_oauth_bearer_and_file_data() -> None:
    provider = _provider("local_openai_compatible").model_copy(
        update={"provider_id": "google", "endpoint": "https://generativelanguage.googleapis.com/v1beta"}
    )
    model = _model("local_openai_compatible").model_copy(
        update={
            "provider_id": "google",
            "model_id": "gemini-test",
            "raw_model_ref": "google/gemini-test",
            "api_id": "gemini-test",
            "protocol": "google_generative",
            "endpoint": "https://generativelanguage.googleapis.com/v1beta",
        }
    )
    client = FakeOpenAIHttpClient(stream_chunks=[{"candidates": [{"finishReason": "STOP"}]}])
    adapter = GoogleGenerativeProtocolAdapter(http_client=client)
    request = ProviderRequest(
        canonical_messages=[
            CanonicalMessage(
                role="user",
                parts=[
                    CanonicalMessagePart(kind="text", text="Describe this."),
                    CanonicalMessagePart(kind="image_input", data={"media_type": "image/png", "uri": "gs://bucket/image.png"}),
                ],
            )
        ],
        metadata={
            "provider_credential": {
                "credential_kind": "oauth",
                "status": "configured",
                "source": "account:google-oauth",
                "api_key": "ya29.runtime-token",
                "headers": {"X-Goog-User-Project": "harness-project"},
            },
            "resolved_provider_options": {"timeout_seconds": 20},
            "resolved_model_options": {"thinking_budget_tokens": 64},
        },
    )

    events = list(adapter.stream(provider, model, request))

    assert events[-1].kind == ProviderEventKind.MODEL_COMPLETED
    assert client.streams == [
        {
            "url": "https://generativelanguage.googleapis.com/v1beta/models/gemini-test:streamGenerateContent?alt=sse",
            "headers": {
                "X-Goog-User-Project": "harness-project",
                "Content-Type": "application/json",
                "Authorization": "Bearer ya29.runtime-token",
            },
            "payload": {
                "contents": [
                    {
                        "role": "user",
                        "parts": [
                            {"text": "Describe this."},
                            {"fileData": {"mimeType": "image/png", "fileUri": "gs://bucket/image.png"}},
                        ],
                    }
                ],
                "generationConfig": {"thinkingConfig": {"thinkingBudget": 64}},
            },
            "timeout": 20.0,
        }
    ]


def test_google_generative_protocol_adapter_reports_stream_error() -> None:
    provider = _provider("local_openai_compatible").model_copy(update={"endpoint": "https://generativelanguage.googleapis.com/v1beta"})
    model = _model("local_openai_compatible").model_copy(update={"protocol": "google_generative", "api_id": "gemini-test"})
    client = FakeOpenAIHttpClient(stream_chunks=[{"error": {"status": "RESOURCE_EXHAUSTED", "message": "quota exhausted"}}])
    adapter = GoogleGenerativeProtocolAdapter(http_client=client)

    events = list(adapter.stream(provider, model, ProviderRequest(messages=[ProviderMessage(role="user", content="Hi")])))

    assert [event.kind for event in events] == [ProviderEventKind.MODEL_STARTED, ProviderEventKind.MODEL_FAILED]
    error = events[-1].payload["error"]
    assert error["category"] == "rate_limit"
    assert error["error_type"] == "RESOURCE_EXHAUSTED"
    assert error["message"] == "quota exhausted"
    assert error["retryable"] is True
    assert error["no_hidden_fallback"] is True


def test_google_safety_block_maps_to_provider_policy_error() -> None:
    provider = _provider("local_openai_compatible").model_copy(update={"endpoint": "https://generativelanguage.googleapis.com/v1beta"})
    model = _model("local_openai_compatible").model_copy(update={"protocol": "google_generative", "api_id": "gemini-test"})
    client = FakeOpenAIHttpClient(
        stream_chunks=[
            {
                "candidates": [
                    {
                        "finishReason": "SAFETY",
                        "safetyRatings": [{"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "blocked": True}],
                    }
                ]
            }
        ]
    )
    adapter = GoogleGenerativeProtocolAdapter(http_client=client)

    events = list(adapter.stream(provider, model, ProviderRequest(messages=[ProviderMessage(role="user", content="Hi")])))

    assert [event.kind for event in events] == [ProviderEventKind.MODEL_STARTED, ProviderEventKind.MODEL_FAILED]
    error = events[-1].payload["error"]
    assert error["category"] == "provider_policy_block"
    assert error["error_type"] == "GOOGLE_GENERATIVE_SAFETY"
    assert error["message"] == "Google Generative blocked the response: SAFETY"
    assert error["retryable"] is False
    assert error["no_hidden_fallback"] is True


def test_google_generative_protocol_adapter_fails_visibly_for_unsupported_parts() -> None:
    provider = _provider("local_openai_compatible").model_copy(update={"endpoint": "https://generativelanguage.googleapis.com/v1beta"})
    model = _model("local_openai_compatible").model_copy(update={"protocol": "google_generative", "api_id": "gemini-test"})
    adapter = GoogleGenerativeProtocolAdapter(http_client=FakeOpenAIHttpClient())
    request = ProviderRequest(
        canonical_messages=[
            CanonicalMessage(
                role="user",
                parts=[CanonicalMessagePart(kind="image_input", data={"ref": "artifact://img"})],
            )
        ]
    )

    events = list(adapter.stream(provider, model, request))

    assert [event.kind for event in events] == [ProviderEventKind.MODEL_STARTED, ProviderEventKind.MODEL_FAILED]
    error = events[-1].payload["error"]
    assert error["category"] == "invalid_response"
    assert error["error_type"] == "UnsupportedCanonicalPartError"
    assert "image_input" in error["message"]
    assert error["no_hidden_fallback"] is True


def test_bedrock_converse_protocol_adapter_streams_text_tool_and_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _provider("bedrock")
    model = _model("bedrock")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAENV")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "env-secret")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "env-session")
    client = FakeOpenAIHttpClient(
        stream_chunks=[
            {"messageStart": {"role": "assistant"}},
            {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "Hello"}}},
            {
                "contentBlockStart": {
                    "contentBlockIndex": 1,
                    "start": {"toolUse": {"toolUseId": "tooluse_1", "name": "read", "input": {}}},
                }
            },
            {"contentBlockDelta": {"contentBlockIndex": 1, "delta": {"toolUse": {"input": '{"path":"README.md"}'}}}},
            {"contentBlockStop": {"contentBlockIndex": 1}},
            {"metadata": {"usage": {"inputTokens": 6, "outputTokens": 7, "totalTokens": 13}, "metrics": {"latencyMs": 25}}},
            {"messageStop": {"stopReason": "tool_use"}},
        ]
    )
    adapter = BedrockConverseProtocolAdapter(http_client=client)
    request = ProviderRequest(
        canonical_messages=[
            CanonicalMessage(role="system", parts=[CanonicalMessagePart(kind="text", text="Be concise.")]),
            CanonicalMessage(
                role="user",
                parts=[
                    CanonicalMessagePart(kind="text", text="Hello"),
                    CanonicalMessagePart(kind="image_input", data={"media_type": "image/png", "data": "aW1n"}),
                ],
            ),
            CanonicalMessage(
                role="assistant",
                parts=[CanonicalMessagePart(kind="tool_call", provider_native_id="tooluse_prev", data={"tool_name": "read", "arguments": {"path": "README.md"}})],
            ),
            CanonicalMessage(
                role="tool",
                parts=[CanonicalMessagePart(kind="tool_result", text="file body", provider_native_id="tooluse_prev")],
            ),
        ],
        metadata={
            "provider_credential": {"credential_kind": "aws_env", "status": "configured", "source": "aws_env"},
            "resolved_provider_options": {"timeout_seconds": 11, "aws_region": "us-east-1"},
            "resolved_model_options": {"max_tokens": 256},
        },
    )

    events = list(adapter.stream(provider, model, request))

    assert [event.kind for event in events] == [
        ProviderEventKind.MODEL_STARTED,
        ProviderEventKind.MODEL_MESSAGE_DELTA,
        ProviderEventKind.TOOL_CALL_STARTED,
        ProviderEventKind.TOOL_CALL_DELTA,
        ProviderEventKind.TOOL_CALL_COMPLETED,
        ProviderEventKind.TOKEN_USAGE_UPDATED,
        ProviderEventKind.MODEL_COMPLETED,
    ]
    assert events[1].text == "Hello"
    assert events[2].tool_call_id == "tooluse_1"
    assert events[2].tool_name == "read"
    assert events[3].payload["arguments_delta"] == '{"path":"README.md"}'
    assert events[4].payload["arguments"] == '{"path":"README.md"}'
    assert events[5].payload["inputTokens"] == 6
    assert events[5].payload["outputTokens"] == 7
    assert events[5].payload["normalized_usage"] == {
        "input_tokens": 6,
        "output_tokens": 7,
        "reasoning_tokens": None,
        "cache_read_tokens": None,
        "cache_write_tokens": None,
        "total_tokens": 13,
    }
    assert events[5].payload["metrics"] == {"latencyMs": 25}
    assert events[6].payload["finish_reason"] == "tool_use"
    assert len(client.streams) == 1
    stream = client.streams[0]
    assert stream["url"] == "https://bedrock-runtime.us-east-1.amazonaws.com/model/anthropic.claude-3-5-sonnet-20241022-v2%3A0/converse-stream"
    assert stream["headers"]["Content-Type"] == "application/json"
    assert stream["headers"]["Host"] == "bedrock-runtime.us-east-1.amazonaws.com"
    assert stream["headers"]["X-Harness-AWS-Credential-Source"] == "aws_env"
    assert stream["headers"]["X-Harness-AWS-Region"] == "us-east-1"
    assert stream["headers"]["X-Amz-Content-Sha256"]
    assert stream["headers"]["X-Amz-Date"].endswith("Z")
    assert stream["headers"]["X-Amz-Security-Token"] == "env-session"
    assert "Credential=AKIAENV/" in stream["headers"]["Authorization"]
    assert "/us-east-1/bedrock/aws4_request" in stream["headers"]["Authorization"]
    assert "SignedHeaders=content-type;host;x-amz-content-sha256;x-amz-date;x-amz-security-token" in stream["headers"]["Authorization"]
    assert "env-secret" not in str(stream["headers"])
    assert stream["payload"] == {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"text": "Hello"},
                    {"image": {"format": "png", "source": {"bytes": "aW1n"}}},
                ],
            },
            {
                "role": "assistant",
                "content": [
                    {
                        "toolUse": {
                            "toolUseId": "tooluse_prev",
                            "name": "read",
                            "input": {"path": "README.md"},
                        }
                    }
                ],
            },
            {
                "role": "user",
                "content": [{"toolResult": {"toolUseId": "tooluse_prev", "content": [{"text": "file body"}]}}],
            },
        ],
        "system": [{"text": "Be concise."}],
        "inferenceConfig": {"maxTokens": 256},
    }
    assert stream["timeout"] == 11.0


def test_bedrock_converse_protocol_adapter_resolves_profile_region_and_bearer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credentials_file = tmp_path / "credentials"
    credentials_file.write_text(
        "[harness]\n"
        "aws_access_key_id = AKIAPROFILE\n"
        "aws_secret_access_key = profile-secret\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(credentials_file))
    provider = _provider("bedrock")
    model = _model("bedrock")
    profile_client = FakeOpenAIHttpClient(stream_chunks=[{"messageStop": {"stopReason": "end_turn"}}])
    profile_request = ProviderRequest(
        messages=[ProviderMessage(role="user", content="Hi")],
        metadata={
            "provider_credential": {
                "credential_kind": "aws_profile",
                "status": "configured",
                "source": "aws_profile",
                "profile": "harness",
            },
            "resolved_provider_options": {"aws_region": "us-west-2"},
        },
    )

    profile_events = list(BedrockConverseProtocolAdapter(http_client=profile_client).stream(provider, model, profile_request))

    assert profile_events[-1].kind == ProviderEventKind.MODEL_COMPLETED
    assert profile_client.streams[0]["url"].startswith("https://bedrock-runtime.us-west-2.amazonaws.com/")
    assert profile_client.streams[0]["headers"]["Host"] == "bedrock-runtime.us-west-2.amazonaws.com"
    assert "Credential=AKIAPROFILE/" in profile_client.streams[0]["headers"]["Authorization"]
    assert "/us-west-2/bedrock/aws4_request" in profile_client.streams[0]["headers"]["Authorization"]
    assert "profile-secret" not in str(profile_client.streams[0]["headers"])

    bearer_client = FakeOpenAIHttpClient(stream_chunks=[{"messageStop": {"stopReason": "end_turn"}}])
    bearer_request = ProviderRequest(
        messages=[ProviderMessage(role="user", content="Hi")],
        metadata={
            "provider_credential": {
                "credential_kind": "api_key",
                "status": "configured",
                "source": "provider_account_secret_store",
                "api_key": "bedrock-bearer-token",
            },
            "resolved_provider_options": {"aws_region": "us-east-2"},
        },
    )

    bearer_events = list(BedrockConverseProtocolAdapter(http_client=bearer_client).stream(provider, model, bearer_request))

    assert bearer_events[-1].kind == ProviderEventKind.MODEL_COMPLETED
    assert bearer_client.streams[0]["url"].startswith("https://bedrock-runtime.us-east-2.amazonaws.com/")
    assert bearer_client.streams[0]["headers"]["Authorization"] == "Bearer bedrock-bearer-token"
    assert "X-Amz-Date" not in bearer_client.streams[0]["headers"]


def test_bedrock_converse_protocol_adapter_reports_stream_error() -> None:
    provider = _provider("bedrock")
    model = _model("bedrock")
    client = FakeOpenAIHttpClient(stream_chunks=[{"error": {"type": "ThrottlingException", "message": "rate exceeded"}}])
    adapter = BedrockConverseProtocolAdapter(http_client=client)
    request = ProviderRequest(
        messages=[ProviderMessage(role="user", content="Hi")],
        metadata={"provider_credential": {"credential_kind": "api_key", "status": "configured", "api_key": "bedrock-bearer-token"}},
    )

    events = list(adapter.stream(provider, model, request))

    assert [event.kind for event in events] == [ProviderEventKind.MODEL_STARTED, ProviderEventKind.MODEL_FAILED]
    error = events[-1].payload["error"]
    assert error["category"] == "rate_limit"
    assert error["error_type"] == "ThrottlingException"
    assert error["message"] == "rate exceeded"
    assert error["retryable"] is True
    assert error["no_hidden_fallback"] is True


def test_bedrock_converse_protocol_adapter_fails_visibly_for_unsupported_parts() -> None:
    provider = _provider("bedrock")
    model = _model("bedrock")
    adapter = BedrockConverseProtocolAdapter(http_client=FakeOpenAIHttpClient())
    request = ProviderRequest(
        canonical_messages=[
            CanonicalMessage(role="assistant", parts=[CanonicalMessagePart(kind="reasoning", text="hidden thought")])
        ]
    )

    events = list(adapter.stream(provider, model, request))

    assert [event.kind for event in events] == [ProviderEventKind.MODEL_STARTED, ProviderEventKind.MODEL_FAILED]
    error = events[-1].payload["error"]
    assert error["category"] == "invalid_response"
    assert error["error_type"] == "UnsupportedCanonicalPartError"
    assert "reasoning" in error["message"]
    assert error["no_hidden_fallback"] is True


def test_codex_cli_protocol_adapter_wraps_codex_backend_events() -> None:
    provider = _provider("codex_cli")
    model = _model("codex_cli")
    created = []

    class FakeCodexBackend:
        def __init__(self, config) -> None:
            self.config = config
            created.append(self)

        def stream_read_only_backend_events(
            self,
            project_root: Path,
            prompt: str,
            final_message_path: Path | None,
        ):
            self.project_root = project_root
            self.prompt = prompt
            self.final_message_path = final_message_path
            yield BackendStreamEvent(type="status", text="started")
            yield BackendStreamEvent(type="message_delta", text="codex answer")
            yield BackendStreamEvent(type="token_usage", payload={"total_tokens": 3})

    adapter = CodexCliProtocolAdapter(backend_factory=FakeCodexBackend)
    request = ProviderRequest(
        session_id="sess",
        turn_id="turn",
        prompt_id="prompt",
        messages=[ProviderMessage(role="user", content="Inspect")],
        context={"project_root": "/tmp/project", "final_message_path": "/tmp/final.md"},
    )

    events = list(adapter.stream(provider, model, request))

    assert [event.kind for event in events] == [
        ProviderEventKind.MODEL_STARTED,
        ProviderEventKind.MODEL_MESSAGE_DELTA,
        ProviderEventKind.TOKEN_USAGE_UPDATED,
        ProviderEventKind.MODEL_COMPLETED,
    ]
    assert [event.sequence for event in events] == [1, 2, 3, 4]
    assert all(event.provider_id == "codex_cli" for event in events)
    assert all(event.model_ref == "codex_cli/gpt-5.5" for event in events)
    assert events[1].text == "codex answer"
    assert events[1].payload["canonical_model_ref"] == "codex_cli/gpt-5.5"
    assert events[1].payload["protocol"] == "codex_cli"
    assert events[2].payload["total_tokens"] == 3
    assert events[2].payload["hidden_model_fallback"] is False
    assert events[3].payload["finish_reason"] == "stop"
    assert created[0].config.settings["command"] == "codex"
    assert created[0].config.settings["model"] == "gpt-5.5"
    assert created[0].config.settings["model_reasoning_effort"] == "low"
    assert created[0].project_root == Path("/tmp/project")
    assert created[0].prompt == "USER:\nInspect"
    assert created[0].final_message_path == Path("/tmp/final.md")


def test_codex_cli_protocol_adapter_wraps_backend_tool_events() -> None:
    provider = _provider("codex_cli")
    model = _model("codex_cli")

    class FakeCodexBackend:
        def __init__(self, config) -> None:
            self.config = config

        def stream_read_only_backend_events(
            self,
            project_root: Path,
            prompt: str,
            final_message_path: Path | None,
        ):
            yield BackendStreamEvent(type="tool_call", text="read", payload={"id": "call_1", "tool": "read"})
            yield BackendStreamEvent(
                type="tool_call_delta",
                text='{"path":"README.md"}',
                payload={"id": "call_1", "tool": "read", "arguments_delta": '{"path":"README.md"}'},
            )
            yield BackendStreamEvent(
                type="tool_result",
                text="file body",
                payload={"id": "call_1", "tool": "read", "arguments": '{"path":"README.md"}'},
            )

    adapter = CodexCliProtocolAdapter(backend_factory=FakeCodexBackend)
    request = ProviderRequest(messages=[ProviderMessage(role="user", content="Inspect")])

    events = list(adapter.stream(provider, model, request))

    assert [event.kind for event in events] == [
        ProviderEventKind.MODEL_STARTED,
        ProviderEventKind.TOOL_CALL_STARTED,
        ProviderEventKind.TOOL_CALL_DELTA,
        ProviderEventKind.TOOL_CALL_COMPLETED,
        ProviderEventKind.MODEL_COMPLETED,
    ]
    assert events[1].tool_call_id == "call_1"
    assert events[1].tool_name == "read"
    assert events[1].payload["canonical_model_ref"] == "codex_cli/gpt-5.5"
    assert events[2].payload["arguments_delta"] == '{"path":"README.md"}'
    assert events[3].payload["arguments"] == '{"path":"README.md"}'
    assert events[4].payload["finish_reason"] == "stop"


def test_codex_cli_protocol_adapter_stops_on_backend_stream_error() -> None:
    provider = _provider("codex_cli")
    model = _model("codex_cli")

    class FakeCodexBackend:
        def __init__(self, config) -> None:
            self.config = config

        def stream_read_only_backend_events(
            self,
            project_root: Path,
            prompt: str,
            final_message_path: Path | None,
        ):
            yield BackendStreamEvent(
                type="error",
                text="provider overloaded, try again later",
                payload={"error_type": "ProviderOverloaded"},
            )
            yield BackendStreamEvent(type="message_delta", text="should not appear")

    adapter = CodexCliProtocolAdapter(backend_factory=FakeCodexBackend)
    request = ProviderRequest(messages=[ProviderMessage(role="user", content="Inspect")])

    events = list(adapter.stream(provider, model, request))

    assert [event.kind for event in events] == [ProviderEventKind.MODEL_STARTED, ProviderEventKind.MODEL_FAILED]
    error = events[-1].payload["error"]
    assert error["category"] == "server_unavailable"
    assert error["error_type"] == "ProviderOverloaded"
    assert error["message"] == "provider overloaded, try again later"
    assert error["retryable"] is True
    assert error["no_hidden_fallback"] is True


def test_codex_cli_protocol_adapter_uses_resolved_reasoning_options() -> None:
    provider = _provider("codex_cli")
    model = _model("codex_cli")
    created = []

    class FakeCodexBackend:
        def __init__(self, config) -> None:
            self.config = config
            created.append(self)

        def stream_read_only_backend_events(
            self,
            project_root: Path,
            prompt: str,
            final_message_path: Path | None,
        ):
            yield BackendStreamEvent(type="message_delta", text="codex answer")

    adapter = CodexCliProtocolAdapter(backend_factory=FakeCodexBackend)
    request = ProviderRequest(
        messages=[ProviderMessage(role="user", content="Inspect")],
        metadata={
            "resolved_provider_options": {"command": "codex", "model_reasoning_effort": "low"},
            "resolved_model_options": {"model_reasoning_effort": "high"},
        },
    )

    events = list(adapter.stream(provider, model, request))

    assert [event.kind for event in events] == [
        ProviderEventKind.MODEL_STARTED,
        ProviderEventKind.MODEL_MESSAGE_DELTA,
        ProviderEventKind.MODEL_COMPLETED,
    ]
    assert created[0].config.settings["model_reasoning_effort"] == "high"


def test_protocol_adapter_missing_error_is_non_retryable_configuration_error() -> None:
    error = protocol_adapter_missing_error("anthropic_messages")

    assert error.category.value == "configuration"
    assert error.error_type == "ProtocolAdapterNotFound"
    assert error.retryable is False
    assert error.hidden_provider_fallback is False
    assert error.no_hidden_fallback is True
    assert "anthropic_messages" in error.message
