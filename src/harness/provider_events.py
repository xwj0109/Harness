from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from harness.provider_content import CanonicalMessage
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
    AUTH = "auth"
    RATE_LIMIT = "rate_limit"
    UNAVAILABLE = "provider_unavailable"
    CONFIGURATION = "configuration"
    CONTEXT_OVERFLOW = "context_overflow"
    ABORTED = "aborted"
    INVALID_REQUEST = "invalid_request"
    INVALID_RESPONSE = "invalid_response"
    SERVER_UNAVAILABLE = "server_unavailable"
    PROVIDER_POLICY_BLOCK = "provider_policy_block"
    UNKNOWN = "unknown"


_ERROR_CATEGORY_TOKENS: tuple[tuple[ProviderErrorCategory, tuple[str, ...]], ...] = (
    (
        ProviderErrorCategory.AUTH,
        (
            "auth",
            "authentication",
            "unauthorized",
            "permission_denied",
            "forbidden",
            "invalid_api_key",
            "access_denied",
            "accessdenied",
            "expired_token",
            "signaturedoesnotmatch",
        ),
    ),
    (
        ProviderErrorCategory.RATE_LIMIT,
        ("rate", "quota", "resource_exhausted", "throttl", "too_many_requests", "too many requests", "429"),
    ),
    (
        ProviderErrorCategory.CONTEXT_OVERFLOW,
        (
            "context_overflow",
            "context length",
            "context_length",
            "context window",
            "maximum context",
            "max context",
            "token_limit",
            "too many tokens",
            "tokens exceeds",
        ),
    ),
    (
        ProviderErrorCategory.INVALID_REQUEST,
        ("invalid", "bad_request", "validation", "malformed", "schema", "parse"),
    ),
    (
        ProviderErrorCategory.SERVER_UNAVAILABLE,
        (
            "unavailable",
            "overload",
            "overloaded",
            "server",
            "internal",
            "timeout",
            "timed out",
            "service_unavailable",
            "500",
            "502",
            "503",
            "504",
        ),
    ),
    (
        ProviderErrorCategory.UNAVAILABLE,
        (
            "provider_unavailable",
            "connection refused",
            "connection reset",
            "network unreachable",
            "name resolution",
            "dns",
            "econnreset",
            "local endpoint",
        ),
    ),
    (
        ProviderErrorCategory.PROVIDER_POLICY_BLOCK,
        ("safety", "policy", "blocked", "content_filter", "moderation", "recitation", "prohibited_content", "spii"),
    ),
)


def provider_error_category_for(*values: Any, default: ProviderErrorCategory = ProviderErrorCategory.UNKNOWN) -> ProviderErrorCategory:
    normalized = " ".join(str(value) for value in values if value is not None).casefold()
    if not normalized:
        return default
    for category, tokens in _ERROR_CATEGORY_TOKENS:
        if any(token in normalized for token in tokens):
            return category
    return default


_RETRYABLE_ERROR_CATEGORIES = frozenset(
    {
        ProviderErrorCategory.RATE_LIMIT,
        ProviderErrorCategory.UNAVAILABLE,
        ProviderErrorCategory.CONTEXT_OVERFLOW,
        ProviderErrorCategory.SERVER_UNAVAILABLE,
    }
)

_NON_RETRYABLE_ERROR_TOKENS = (
    "authentication",
    "authorization",
    "unauthorized",
    "forbidden",
    "invalid_api_key",
    "permission_denied",
    "access_denied",
    "billing",
    "payment",
    "insufficient_quota",
    "quota_exceeded",
    "hard limit",
    "model_not_found",
    "not_found",
    "unsupported",
)


def provider_error_retryable_for(category: ProviderErrorCategory | str, *values: Any) -> bool:
    if isinstance(category, ProviderErrorCategory):
        normalized_category = category
    else:
        try:
            normalized_category = ProviderErrorCategory(str(category))
        except ValueError:
            normalized_category = ProviderErrorCategory.UNKNOWN
    normalized = " ".join(str(value) for value in values if value is not None).casefold()
    if normalized and any(token in normalized for token in _NON_RETRYABLE_ERROR_TOKENS):
        return False
    return normalized_category in _RETRYABLE_ERROR_CATEGORIES


_RETRY_AFTER_KEYS = {
    "retry-after",
    "retry_after",
    "retryafter",
    "retry-after-seconds",
    "retry_after_seconds",
    "retryafterseconds",
    "retry-delay",
    "retry_delay",
    "retrydelay",
    "reset-after",
    "reset_after",
    "resetafter",
}

_RETRY_AFTER_MS_KEYS = {
    "retry-after-ms",
    "retry_after_ms",
    "retryafterms",
    "retry-delay-ms",
    "retry_delay_ms",
    "retrydelayms",
}


def provider_retry_after_seconds_for(*values: Any) -> float | None:
    for value in values:
        parsed = _retry_after_seconds_from_value(value)
        if parsed is not None:
            return parsed
    return None


def _retry_after_seconds_from_value(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        return max(0.0, float(value))
    if isinstance(value, str):
        return _retry_after_seconds_from_text(value)
    if isinstance(value, dict):
        for key, item in value.items():
            normalized_key = str(key).strip().casefold()
            if normalized_key in _RETRY_AFTER_MS_KEYS:
                parsed = _retry_after_seconds_from_value(item)
                if parsed is not None:
                    return parsed / 1000.0
            if normalized_key in _RETRY_AFTER_KEYS:
                parsed = _retry_after_seconds_from_value(item)
                if parsed is not None:
                    return parsed
        for item in value.values():
            parsed = _retry_after_seconds_from_value(item)
            if parsed is not None:
                return parsed
    if isinstance(value, list | tuple):
        for item in value:
            parsed = _retry_after_seconds_from_value(item)
            if parsed is not None:
                return parsed
    return None


def _retry_after_seconds_from_text(value: str) -> float | None:
    text = value.strip()
    if not text:
        return None
    try:
        return max(0.0, float(text[:-1] if text.casefold().endswith("s") else text))
    except ValueError:
        pass
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0.0, (parsed - datetime.now(timezone.utc)).total_seconds())


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
    canonical_messages: list[CanonicalMessage] = Field(default_factory=list)
    context: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ProviderError(BaseModel):
    schema_version: str = PROVIDER_ERROR_SCHEMA_VERSION
    category: ProviderErrorCategory
    error_type: str
    message: str
    retryable: bool = False
    retry_after_seconds: float | None = None
    hidden_provider_fallback: bool = False
    no_hidden_fallback: bool = True


class ProviderStreamAbortError(RuntimeError):
    pass


class ProviderStreamTimeoutError(TimeoutError):
    pass


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


def normalized_token_usage(payload: dict[str, Any]) -> dict[str, int | None]:
    input_tokens = _usage_int(payload, "input_tokens", "prompt_tokens", "inputTokens", "promptTokenCount")
    output_tokens = _usage_int(payload, "output_tokens", "completion_tokens", "outputTokens", "candidatesTokenCount")
    reasoning_tokens = _usage_int(payload, "reasoning_tokens", "reasoningTokens", "thoughtsTokenCount")
    total_tokens = _usage_int(payload, "total_tokens", "totalTokens", "totalTokenCount")
    input_details = payload.get("input_tokens_details") if isinstance(payload.get("input_tokens_details"), dict) else {}
    output_details = payload.get("output_tokens_details") if isinstance(payload.get("output_tokens_details"), dict) else {}
    cache_read_tokens = _usage_int(
        payload,
        "cache_read_tokens",
        "cached_input_tokens",
        "cache_read_input_tokens",
        "cached_tokens",
        "cacheReadInputTokens",
        "cacheReadTokens",
    )
    if cache_read_tokens is None:
        cache_read_tokens = _usage_int(input_details, "cached_tokens", "cache_read_tokens", "cache_read_input_tokens")
    cache_write_tokens = _usage_int(
        payload,
        "cache_write_tokens",
        "cache_creation_input_tokens",
        "cacheCreationInputTokens",
        "cacheWriteInputTokens",
        "cacheWriteTokens",
    )
    if cache_write_tokens is None:
        cache_write_tokens = _usage_int(input_details, "cache_creation_tokens", "cache_write_tokens", "cache_creation_input_tokens")
    if reasoning_tokens is None:
        reasoning_tokens = _usage_int(output_details, "reasoning_tokens", "reasoning_tokens_details", "reasoningTokens")
    if total_tokens is None and (input_tokens is not None or output_tokens is not None or reasoning_tokens is not None):
        total_tokens = (input_tokens or 0) + (output_tokens or 0)
        if output_tokens is None:
            total_tokens += reasoning_tokens or 0
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "reasoning_tokens": reasoning_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_write_tokens": cache_write_tokens,
        "total_tokens": total_tokens,
    }


def normalize_token_usage_payload(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = normalized_token_usage(payload)
    usage_payload = {**payload, "normalized_usage": normalized}
    for key, value in normalized.items():
        if value is not None:
            usage_payload.setdefault(key, value)
    return usage_payload


def _usage_int(payload: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
    return None


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
