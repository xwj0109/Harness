from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Protocol

from harness.backends.local_openai import BackendConfigError, LocalEndpointUnavailable
from harness.backends.streaming import BackendStreamEvent
from harness.chat_model import ChatContext, ChatMessage, ChatModel, build_default_chat_model
from harness.provider_events import (
    ProviderCapabilities,
    ProviderError,
    ProviderErrorCategory,
    ProviderEvent,
    ProviderEventKind,
    ProviderMessage,
    ProviderRequest,
    provider_error_event,
    provider_event,
)
from harness.security import sanitize_for_logging


class ProviderAdapter(Protocol):
    provider_id: str
    model_ref: str | None
    capabilities: ProviderCapabilities

    def stream(self, request: ProviderRequest) -> Iterator[ProviderEvent]:
        ...


@dataclass
class ChatModelProviderAdapter:
    chat_model: ChatModel
    provider_id: str = "chat_model"
    model_ref: str | None = None
    capabilities: ProviderCapabilities = field(default_factory=ProviderCapabilities)

    def stream(self, request: ProviderRequest) -> Iterator[ProviderEvent]:
        sequence = 1
        normalized_request = _request_with_defaults(request, provider_id=self.provider_id, model_ref=self.model_ref)
        yield provider_event(
            ProviderEventKind.MODEL_STARTED,
            sequence=sequence,
            request=normalized_request,
            payload={
                "capabilities": self.capabilities.model_dump(mode="json"),
                "hidden_provider_fallback": False,
                "no_hidden_fallback": True,
            },
        )
        sequence += 1
        try:
            response = self.chat_model.complete(
                [_chat_message(message) for message in normalized_request.messages],
                _chat_context(normalized_request),
            )
        except Exception as exc:
            yield provider_error_event(
                classify_provider_exception(exc),
                sequence=sequence,
                request=normalized_request,
            )
            return
        if response.content:
            yield provider_event(
                ProviderEventKind.MODEL_MESSAGE_DELTA,
                sequence=sequence,
                request=normalized_request,
                text=response.content,
                payload={"delta": response.content},
            )
            sequence += 1
        for tool_call in response.tool_calls:
            yield provider_event(
                ProviderEventKind.TOOL_CALL_STARTED,
                sequence=sequence,
                request=normalized_request,
                tool_call_id=tool_call.id,
                tool_name=tool_call.name,
                payload={
                    "tool_call_id": tool_call.id,
                    "tool_name": tool_call.name,
                    "arguments": sanitize_for_logging(tool_call.arguments),
                },
            )
            sequence += 1
            yield provider_event(
                ProviderEventKind.TOOL_CALL_COMPLETED,
                sequence=sequence,
                request=normalized_request,
                tool_call_id=tool_call.id,
                tool_name=tool_call.name,
                payload={
                    "tool_call_id": tool_call.id,
                    "tool_name": tool_call.name,
                    "arguments": sanitize_for_logging(tool_call.arguments),
                    "provider_native_tool_call": True,
                },
            )
            sequence += 1
        yield provider_event(
            ProviderEventKind.MODEL_COMPLETED,
            sequence=sequence,
            request=normalized_request,
            payload={"finish_reason": "stop"},
        )


@dataclass
class BackendStreamProviderAdapter:
    events: list[BackendStreamEvent]
    provider_id: str = "backend_stream"
    model_ref: str | None = None
    capabilities: ProviderCapabilities = field(default_factory=lambda: ProviderCapabilities(supports_streaming=True))

    def stream(self, request: ProviderRequest) -> Iterator[ProviderEvent]:
        normalized_request = _request_with_defaults(request, provider_id=self.provider_id, model_ref=self.model_ref)
        yield provider_event(
            ProviderEventKind.MODEL_STARTED,
            sequence=1,
            request=normalized_request,
            payload={"capabilities": self.capabilities.model_dump(mode="json")},
        )
        sequence = 2
        for event in self.events:
            yield provider_event_from_backend_stream_event(event, sequence=sequence, request=normalized_request)
            sequence += 1
        yield provider_event(ProviderEventKind.MODEL_COMPLETED, sequence=sequence, request=normalized_request)


def build_default_provider_adapter(project_root: Path) -> ProviderAdapter:
    chat_model = build_default_chat_model(project_root)
    return ChatModelProviderAdapter(
        chat_model=chat_model,
        provider_id="default_chat_model",
        model_ref=None,
        capabilities=ProviderCapabilities(supports_streaming=False, supports_native_tools=True),
    )


def provider_event_from_backend_stream_event(
    event: BackendStreamEvent,
    *,
    sequence: int,
    request: ProviderRequest | None = None,
) -> ProviderEvent:
    payload = dict(event.payload)
    if event.type == "message_delta":
        return provider_event(
            ProviderEventKind.MODEL_MESSAGE_DELTA,
            sequence=sequence,
            request=request,
            text=event.text,
            payload={**payload, "delta": event.text or payload.get("delta")},
        )
    if event.type == "reasoning_summary_delta":
        return provider_event(ProviderEventKind.REASONING_SUMMARY_DELTA, sequence=sequence, request=request, text=event.text, payload=payload)
    if event.type == "tool_call":
        tool_call_id = str(payload.get("id") or payload.get("tool_call_id") or "")
        tool_name = str(payload.get("name") or payload.get("tool") or payload.get("tool_name") or "")
        return provider_event(
            ProviderEventKind.TOOL_CALL_STARTED,
            sequence=sequence,
            request=request,
            text=event.text,
            payload=payload,
            tool_call_id=tool_call_id or None,
            tool_name=tool_name or None,
        )
    if event.type == "tool_result":
        tool_call_id = str(payload.get("id") or payload.get("tool_call_id") or "")
        tool_name = str(payload.get("name") or payload.get("tool") or payload.get("tool_name") or "")
        return provider_event(
            ProviderEventKind.TOOL_CALL_COMPLETED,
            sequence=sequence,
            request=request,
            text=event.text,
            payload=payload,
            tool_call_id=tool_call_id or None,
            tool_name=tool_name or None,
        )
    if event.type == "token_usage":
        return provider_event(ProviderEventKind.TOKEN_USAGE_UPDATED, sequence=sequence, request=request, payload=payload)
    if event.type == "error":
        error = ProviderError(
            category=ProviderErrorCategory.UNKNOWN,
            error_type=str(payload.get("error_type") or "BackendStreamError"),
            message=event.text or str(payload.get("message") or "Provider stream error."),
            retryable=False,
        )
        return provider_error_event(error, sequence=sequence, request=request)
    return provider_event(
        ProviderEventKind.MODEL_MESSAGE_DELTA if event.text else ProviderEventKind.MODEL_STARTED,
        sequence=sequence,
        request=request,
        text=event.text,
        payload={**payload, "backend_event_type": event.type},
    )


def classify_provider_exception(exc: Exception) -> ProviderError:
    message = str(sanitize_for_logging(str(exc)))
    if _looks_like_context_overflow(message):
        category = ProviderErrorCategory.CONTEXT_OVERFLOW
        retryable = True
    elif isinstance(exc, LocalEndpointUnavailable):
        category = ProviderErrorCategory.UNAVAILABLE
        retryable = True
    elif isinstance(exc, BackendConfigError):
        category = ProviderErrorCategory.CONFIGURATION
        retryable = False
    elif isinstance(exc, TimeoutError):
        category = ProviderErrorCategory.UNAVAILABLE
        retryable = True
    elif isinstance(exc, (KeyError, IndexError, TypeError, ValueError)):
        category = ProviderErrorCategory.INVALID_RESPONSE
        retryable = False
    else:
        category = ProviderErrorCategory.UNKNOWN
        retryable = False
    return ProviderError(
        category=category,
        error_type=type(exc).__name__,
        message=message,
        retryable=retryable,
        hidden_provider_fallback=False,
        no_hidden_fallback=True,
    )


def _looks_like_context_overflow(message: str) -> bool:
    lowered = message.lower()
    needles = (
        "context length",
        "context window",
        "context overflow",
        "maximum context",
        "max context",
        "too many tokens",
        "token limit",
        "tokens exceeds",
        "exceeds the model",
    )
    return any(needle in lowered for needle in needles)


def _request_with_defaults(request: ProviderRequest, *, provider_id: str, model_ref: str | None) -> ProviderRequest:
    return request.model_copy(
        update={
            "provider_id": request.provider_id or provider_id,
            "model_ref": request.model_ref or model_ref,
        },
        deep=True,
    )


def _chat_message(message: ProviderMessage) -> ChatMessage:
    return ChatMessage(role=message.role, content=message.content)


def _chat_context(request: ProviderRequest) -> ChatContext:
    context = request.context
    return ChatContext(
        project_root=str(context.get("project_root") or "."),
        model_profile=str(context.get("model_profile") or request.provider_id or "provider_adapter"),
        mode=str(context.get("mode") or "runtime"),
        context_blocks=list(context.get("context_blocks") or []),
        safety_boundaries=list(context.get("safety_boundaries") or []),
    )
