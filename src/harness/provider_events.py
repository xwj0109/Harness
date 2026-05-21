from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from harness.security import sanitize_for_logging


PROVIDER_EVENT_SCHEMA_VERSION = "harness.provider_event/v1"
PROVIDER_CAPABILITIES_SCHEMA_VERSION = "harness.provider_capabilities/v1"
PROVIDER_REQUEST_SCHEMA_VERSION = "harness.provider_request/v1"
PROVIDER_ERROR_SCHEMA_VERSION = "harness.provider_error/v1"


class ProviderEventKind(str, Enum):
    MODEL_STARTED = "model.started"
    MODEL_MESSAGE_DELTA = "model.message_delta"
    REASONING_SUMMARY_DELTA = "reasoning.summary_delta"
    TOOL_CALL_STARTED = "tool_call.started"
    TOOL_CALL_DELTA = "tool_call.delta"
    TOOL_CALL_COMPLETED = "tool_call.completed"
    TOKEN_USAGE_UPDATED = "token_usage.updated"
    MODEL_COMPLETED = "model.completed"
    MODEL_FAILED = "model.failed"
    MODEL_ABORTED = "model.aborted"


class ProviderErrorCategory(str, Enum):
    UNAVAILABLE = "provider_unavailable"
    CONFIGURATION = "configuration"
    CONTEXT_OVERFLOW = "context_overflow"
    ABORTED = "aborted"
    INVALID_RESPONSE = "invalid_response"
    UNKNOWN = "unknown"


class ProviderCapabilities(BaseModel):
    schema_version: str = PROVIDER_CAPABILITIES_SCHEMA_VERSION
    supports_streaming: bool = False
    supports_native_tools: bool = False
    supports_abort: bool = False
    supports_token_usage: bool = False
    supports_reasoning_summary: bool = False
    supports_images: bool = False
    context_window_tokens: int | None = None


class ProviderMessage(BaseModel):
    role: str
    content: str


class ProviderRequest(BaseModel):
    schema_version: str = PROVIDER_REQUEST_SCHEMA_VERSION
    session_id: str | None = None
    turn_id: str | None = None
    prompt_id: str | None = None
    provider_id: str | None = None
    model_ref: str | None = None
    messages: list[ProviderMessage] = Field(default_factory=list)
    context: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ProviderError(BaseModel):
    schema_version: str = PROVIDER_ERROR_SCHEMA_VERSION
    category: ProviderErrorCategory
    error_type: str
    message: str
    retryable: bool = False
    hidden_provider_fallback: bool = False
    no_hidden_fallback: bool = True


class ProviderEvent(BaseModel):
    schema_version: str = PROVIDER_EVENT_SCHEMA_VERSION
    kind: ProviderEventKind
    sequence: int
    provider_id: str | None = None
    model_ref: str | None = None
    session_id: str | None = None
    turn_id: str | None = None
    prompt_id: str | None = None
    message_id: str | None = None
    part_id: str | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    text: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def store_kind(self) -> str:
        return self.kind.value

    def store_payload(self) -> dict[str, Any]:
        payload = dict(self.payload)
        if self.text is not None:
            payload.setdefault("text", self.text)
        if self.tool_call_id is not None:
            payload.setdefault("tool_call_id", self.tool_call_id)
        if self.tool_name is not None:
            payload.setdefault("tool_name", self.tool_name)
        payload.setdefault("provider_id", self.provider_id)
        payload.setdefault("model_ref", self.model_ref)
        payload.setdefault("turn_id", self.turn_id)
        payload.setdefault("prompt_id", self.prompt_id)
        payload.setdefault("hidden_provider_fallback", False)
        payload.setdefault("no_hidden_fallback", True)
        return sanitize_for_logging(payload)


def provider_event(
    kind: ProviderEventKind | str,
    *,
    sequence: int,
    request: ProviderRequest | None = None,
    provider_id: str | None = None,
    model_ref: str | None = None,
    text: str | None = None,
    payload: dict[str, Any] | None = None,
    message_id: str | None = None,
    part_id: str | None = None,
    tool_call_id: str | None = None,
    tool_name: str | None = None,
) -> ProviderEvent:
    kind_value = ProviderEventKind(kind.value if isinstance(kind, ProviderEventKind) else kind)
    return ProviderEvent(
        kind=kind_value,
        sequence=sequence,
        provider_id=provider_id if provider_id is not None else (request.provider_id if request else None),
        model_ref=model_ref if model_ref is not None else (request.model_ref if request else None),
        session_id=request.session_id if request else None,
        turn_id=request.turn_id if request else None,
        prompt_id=request.prompt_id if request else None,
        message_id=message_id,
        part_id=part_id,
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        text=sanitize_for_logging(text) if text is not None else None,
        payload=sanitize_for_logging(payload or {}),
    )


def provider_error_event(
    error: ProviderError,
    *,
    sequence: int,
    request: ProviderRequest | None = None,
    provider_id: str | None = None,
    model_ref: str | None = None,
) -> ProviderEvent:
    return provider_event(
        ProviderEventKind.MODEL_FAILED,
        sequence=sequence,
        request=request,
        provider_id=provider_id,
        model_ref=model_ref,
        payload={"error": error.model_dump(mode="json")},
    )
