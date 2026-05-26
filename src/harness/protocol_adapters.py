from __future__ import annotations

import configparser
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import inspect
import json
import os
from pathlib import Path
import time
import urllib.parse
from typing import Any, Callable, Iterator, Protocol

from harness.backends.codex_cli import CodexCliBackend
from harness.backends.local_openai import LocalOpenAICompatibleBackend, OpenAICompatibleHttpClient, UrllibOpenAICompatibleHttpClient
from harness.model_registry import ModelDescriptor, ModelProtocol, ProviderDescriptor
from harness.model_protocols import ALLOWED_MODEL_PROTOCOLS
from harness.models import BackendConfig, BackendKind
from harness.provider_adapters import classify_provider_exception, provider_event_from_backend_stream_event
from harness.provider_auth import ProviderCredentialResolutionError
from harness.provider_content import (
    CanonicalMessage,
    canonical_messages_from_provider_request,
    canonical_messages_to_openai_chat_payload,
    canonical_messages_to_text_prompt,
    UnsupportedCanonicalPartError,
)
from harness.provider_events import (
    ProviderError,
    ProviderErrorCategory,
    ProviderEvent,
    ProviderEventKind,
    ProviderRequest,
    ProviderStreamAbortError,
    ProviderStreamTimeoutError,
    normalize_token_usage_payload,
    normalized_token_usage,
    provider_error_category_for,
    provider_error_retryable_for,
    provider_retry_after_seconds_for,
    provider_error_event,
    provider_event,
)


class ProtocolAdapter(Protocol):
    protocol: ModelProtocol

    def stream(
        self,
        provider: ProviderDescriptor,
        model: ModelDescriptor,
        request: ProviderRequest,
    ) -> Iterator[ProviderEvent]:
        ...


class ProtocolAdapterRegistry:
    def __init__(self, *, allowed_protocols: set[str] | frozenset[str] | None = None) -> None:
        self._adapters: dict[str, ProtocolAdapter] = {}
        self._allowed_protocols = frozenset(allowed_protocols or ALLOWED_MODEL_PROTOCOLS)

    def register(self, adapter: ProtocolAdapter) -> None:
        protocol = getattr(adapter, "protocol", None)
        if not isinstance(protocol, str) or not protocol.strip():
            raise ProtocolAdapterRegistrationError(str(protocol), sorted(self._allowed_protocols), reason="protocol_missing")
        clean_protocol = protocol.strip()
        if clean_protocol not in self._allowed_protocols:
            raise ProtocolAdapterRegistrationError(clean_protocol, sorted(self._allowed_protocols), reason="protocol_not_allowlisted")
        self._adapters[clean_protocol] = adapter

    def get(self, protocol: str) -> ProtocolAdapter:
        try:
            return self._adapters[protocol]
        except KeyError as exc:
            raise ProtocolAdapterNotFound(protocol) from exc

    def has(self, protocol: str) -> bool:
        return protocol in self._adapters

    def list_protocols(self) -> list[str]:
        return sorted(self._adapters)

    def list_allowed_protocols(self) -> list[str]:
        return sorted(self._allowed_protocols)


class ProtocolAdapterNotFound(KeyError):
    def __init__(self, protocol: str) -> None:
        self.protocol = protocol
        super().__init__(f"No protocol adapter registered for protocol: {protocol}")


class ProtocolAdapterRegistrationError(ValueError):
    def __init__(self, protocol: str, allowed_protocols: list[str], *, reason: str) -> None:
        self.protocol = protocol
        self.allowed_protocols = allowed_protocols
        self.reason = reason
        super().__init__(f"Protocol adapter registration rejected for protocol {protocol!r}: {reason}")


@dataclass
class _StreamControl:
    request: ProviderRequest
    deadline_monotonic: float | None = None

    @property
    def abort_checker(self):
        return lambda: _request_abort_requested(self.request)

    def check(self) -> None:
        if _request_abort_requested(self.request):
            raise ProviderStreamAbortError("Provider stream aborted.")
        if self.deadline_monotonic is not None and time.monotonic() >= self.deadline_monotonic:
            raise ProviderStreamTimeoutError("Provider stream deadline exceeded.")

    def transport_timeout(self, configured_timeout: float) -> float:
        self.check()
        if self.deadline_monotonic is None:
            return float(configured_timeout)
        remaining = max(self.deadline_monotonic - time.monotonic(), 0.001)
        return min(float(configured_timeout), remaining)


def _stream_control(request: ProviderRequest) -> _StreamControl:
    return _StreamControl(request=request, deadline_monotonic=_request_deadline_monotonic(request))


def _request_abort_requested(request: ProviderRequest) -> bool:
    for source in (request.context, request.metadata):
        checker = source.get("abort_checker")
        if callable(checker) and checker():
            return True
        signal = source.get("abort_signal")
        is_set = getattr(signal, "is_set", None)
        if callable(is_set) and is_set():
            return True
        if source.get("aborted") is True or source.get("abort_requested") is True:
            return True
    return False


def _request_deadline_monotonic(request: ProviderRequest) -> float | None:
    for source in (request.context, request.metadata):
        value = source.get("stream_deadline_monotonic") or source.get("deadline_monotonic")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    for source in (request.context, request.metadata):
        value = source.get("stream_timeout_seconds") or source.get("overall_timeout_seconds")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return time.monotonic() + max(float(value), 0.0)
    return None


def _client_stream_sse_json(
    client: OpenAICompatibleHttpClient,
    url: str,
    *,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout: float,
    abort_checker,
) -> Iterator[dict[str, Any]]:
    if callable(abort_checker) and abort_checker():
        raise ProviderStreamAbortError("Provider stream aborted.")
    method = client.stream_sse_json
    try:
        supports_abort = "abort_checker" in inspect.signature(method).parameters
    except (TypeError, ValueError):
        supports_abort = False
    if supports_abort:
        stream = method(url, headers=headers, payload=payload, timeout=timeout, abort_checker=abort_checker)
    else:
        stream = method(url, headers=headers, payload=payload, timeout=timeout)
    for chunk in stream:
        if callable(abort_checker) and abort_checker():
            raise ProviderStreamAbortError("Provider stream aborted.")
        yield chunk
        if callable(abort_checker) and abort_checker():
            raise ProviderStreamAbortError("Provider stream aborted.")


def _provider_abort_event(
    exc: ProviderStreamAbortError,
    *,
    sequence: int,
    request: ProviderRequest,
    model: ModelDescriptor,
) -> ProviderEvent:
    error = classify_provider_exception(exc)
    return provider_event(
        ProviderEventKind.MODEL_ABORTED,
        sequence=sequence,
        request=request,
        payload={**_execution_payload(model), "aborted": True, "error": error.model_dump(mode="json")},
    )


def _current_sequence(scope: dict[str, Any]) -> int:
    value = scope.get("sequence")
    if isinstance(value, int) and value > 0:
        return value
    return 2


class OpenAIChatProtocolAdapter:
    protocol: ModelProtocol = "openai_chat"

    def __init__(self, *, http_client: OpenAICompatibleHttpClient | None = None) -> None:
        self.http_client = http_client

    def stream(
        self,
        provider: ProviderDescriptor,
        model: ModelDescriptor,
        request: ProviderRequest,
    ) -> Iterator[ProviderEvent]:
        normalized = _request_with_selection(request, provider=provider, model=model)
        yield provider_event(
            ProviderEventKind.MODEL_STARTED,
            sequence=1,
            request=normalized,
            payload=_started_payload(provider, model, normalized),
        )
        control = _stream_control(normalized)
        try:
            control.check()
            backend = LocalOpenAICompatibleBackend(
                _openai_chat_backend_config(provider, model, normalized),
                http_client=self.http_client,
            )
            sequence = 2
            completed = False
            for event in backend.stream_complete_backend_events(
                canonical_messages_to_openai_chat_payload(canonical_messages_from_provider_request(normalized))
            ):
                control.check()
                if event.type == "status":
                    continue
                normalized_event = provider_event_from_backend_stream_event(event, sequence=sequence, request=normalized)
                payload = {**_execution_payload(model), **normalized_event.payload}
                if normalized_event.kind == ProviderEventKind.TOKEN_USAGE_UPDATED:
                    payload = _usage_event_payload(model, payload)
                yield normalized_event.model_copy(
                    update={"payload": payload},
                    deep=True,
                )
                if normalized_event.kind == ProviderEventKind.MODEL_FAILED:
                    return
                if normalized_event.kind == ProviderEventKind.MODEL_COMPLETED:
                    completed = True
                sequence += 1
                control.check()
        except ProviderStreamAbortError as exc:
            yield _provider_abort_event(exc, sequence=_current_sequence(locals()), request=normalized, model=model)
            return
        except ProviderStreamTimeoutError as exc:
            yield provider_error_event(classify_provider_exception(exc), sequence=_current_sequence(locals()), request=normalized)
            return
        except Exception as exc:
            yield provider_error_event(
                classify_provider_exception(exc),
                sequence=2,
                request=normalized,
            )
            return
        if not completed:
            yield provider_event(
                ProviderEventKind.MODEL_COMPLETED,
                sequence=sequence,
                request=normalized,
                payload={**_execution_payload(model), "finish_reason": "stop"},
            )


class CodexCliProtocolAdapter:
    protocol: ModelProtocol = "codex_cli"

    def __init__(self, *, backend_factory: Callable[[BackendConfig], CodexCliBackend] = CodexCliBackend) -> None:
        self.backend_factory = backend_factory

    def stream(
        self,
        provider: ProviderDescriptor,
        model: ModelDescriptor,
        request: ProviderRequest,
    ) -> Iterator[ProviderEvent]:
        normalized = _request_with_selection(request, provider=provider, model=model)
        yield provider_event(
            ProviderEventKind.MODEL_STARTED,
            sequence=1,
            request=normalized,
            payload=_started_payload(provider, model, normalized),
        )
        project_root = Path(str(normalized.context.get("project_root") or "."))
        final_message_path = _optional_path(normalized.context.get("final_message_path"))
        prompt = _request_prompt(normalized)
        control = _stream_control(normalized)
        try:
            control.check()
            backend = self.backend_factory(_codex_backend_config(provider, model, normalized))
            sequence = 2
            for event in backend.stream_read_only_backend_events(project_root, prompt, final_message_path):
                control.check()
                if event.type == "status":
                    continue
                normalized_event = provider_event_from_backend_stream_event(event, sequence=sequence, request=normalized)
                yield normalized_event.model_copy(
                    update={"payload": {**_execution_payload(model), **normalized_event.payload}},
                    deep=True,
                )
                if normalized_event.kind == ProviderEventKind.MODEL_FAILED:
                    return
                sequence += 1
                control.check()
        except ProviderStreamAbortError as exc:
            yield _provider_abort_event(exc, sequence=_current_sequence(locals()), request=normalized, model=model)
            return
        except ProviderStreamTimeoutError as exc:
            yield provider_error_event(classify_provider_exception(exc), sequence=_current_sequence(locals()), request=normalized)
            return
        except Exception as exc:
            yield provider_error_event(
                classify_provider_exception(exc),
                sequence=2,
                request=normalized,
            )
            return
        yield provider_event(
            ProviderEventKind.MODEL_COMPLETED,
            sequence=sequence,
            request=normalized,
            payload={**_execution_payload(model), "finish_reason": "stop"},
        )


class OpenAIResponsesProtocolAdapter:
    protocol: ModelProtocol = "openai_responses"

    def __init__(self, *, http_client: OpenAICompatibleHttpClient | None = None) -> None:
        self.http_client = http_client

    def stream(
        self,
        provider: ProviderDescriptor,
        model: ModelDescriptor,
        request: ProviderRequest,
    ) -> Iterator[ProviderEvent]:
        normalized = _request_with_selection(request, provider=provider, model=model)
        yield provider_event(
            ProviderEventKind.MODEL_STARTED,
            sequence=1,
            request=normalized,
            payload=_started_payload(provider, model, normalized),
        )
        control = _stream_control(normalized)
        try:
            control.check()
            payload = self._payload(provider, model, normalized)
            sequence = 2
            completed = False
            state: dict[str, Any] = {}
            client = self.http_client or _default_openai_http_client()
            timeout = float(payload.pop("_timeout_seconds", 30))
            for chunk in _client_stream_sse_json(
                client,
                _join_url(model.endpoint or provider.endpoint, "/responses"),
                headers=_openai_responses_headers(provider, normalized),
                payload=payload,
                timeout=control.transport_timeout(timeout),
                abort_checker=control.abort_checker,
            ):
                control.check()
                for event in _openai_responses_events(chunk, state, sequence, normalized, model):
                    yield event
                    if event.kind == ProviderEventKind.MODEL_FAILED:
                        return
                    if event.kind == ProviderEventKind.MODEL_COMPLETED:
                        completed = True
                    sequence += 1
                    control.check()
        except ProviderStreamAbortError as exc:
            yield _provider_abort_event(exc, sequence=_current_sequence(locals()), request=normalized, model=model)
            return
        except ProviderStreamTimeoutError as exc:
            yield provider_error_event(classify_provider_exception(exc), sequence=_current_sequence(locals()), request=normalized)
            return
        except Exception as exc:
            yield provider_error_event(
                classify_provider_exception(exc),
                sequence=2,
                request=normalized,
            )
            return
        if not completed:
            yield provider_event(
                ProviderEventKind.MODEL_COMPLETED,
                sequence=sequence,
                request=normalized,
                payload={**_execution_payload(model), "finish_reason": "stop"},
            )


    def _payload(self, provider: ProviderDescriptor, model: ModelDescriptor, request: ProviderRequest) -> dict[str, Any]:
        return _openai_responses_payload(provider, model, request)


class OpenAICodexResponsesProtocolAdapter(OpenAIResponsesProtocolAdapter):
    protocol: ModelProtocol = "openai_codex_responses"

    def _payload(self, provider: ProviderDescriptor, model: ModelDescriptor, request: ProviderRequest) -> dict[str, Any]:
        return _openai_codex_responses_payload(provider, model, request)


class AnthropicMessagesProtocolAdapter:
    protocol: ModelProtocol = "anthropic_messages"

    def __init__(self, *, http_client: OpenAICompatibleHttpClient | None = None) -> None:
        self.http_client = http_client

    def stream(
        self,
        provider: ProviderDescriptor,
        model: ModelDescriptor,
        request: ProviderRequest,
    ) -> Iterator[ProviderEvent]:
        normalized = _request_with_selection(request, provider=provider, model=model)
        yield provider_event(
            ProviderEventKind.MODEL_STARTED,
            sequence=1,
            request=normalized,
            payload=_started_payload(provider, model, normalized),
        )
        control = _stream_control(normalized)
        try:
            control.check()
            payload = _anthropic_messages_payload(provider, model, normalized)
            sequence = 2
            completed = False
            state: dict[str, Any] = {"blocks": {}}
            client = self.http_client or _default_openai_http_client()
            timeout = float(payload.pop("_timeout_seconds", 30))
            for chunk in _client_stream_sse_json(
                client,
                _join_url(model.endpoint or provider.endpoint, "/messages"),
                headers=_anthropic_messages_headers(provider, normalized),
                payload=payload,
                timeout=control.transport_timeout(timeout),
                abort_checker=control.abort_checker,
            ):
                control.check()
                for event in _anthropic_messages_events(chunk, state, sequence, normalized, model):
                    yield event
                    if event.kind == ProviderEventKind.MODEL_FAILED:
                        return
                    if event.kind == ProviderEventKind.MODEL_COMPLETED:
                        completed = True
                    sequence += 1
                    control.check()
        except ProviderStreamAbortError as exc:
            yield _provider_abort_event(exc, sequence=_current_sequence(locals()), request=normalized, model=model)
            return
        except ProviderStreamTimeoutError as exc:
            yield provider_error_event(classify_provider_exception(exc), sequence=_current_sequence(locals()), request=normalized)
            return
        except Exception as exc:
            yield provider_error_event(
                classify_provider_exception(exc),
                sequence=2,
                request=normalized,
            )
            return
        if not completed:
            yield provider_event(
                ProviderEventKind.MODEL_COMPLETED,
                sequence=sequence,
                request=normalized,
                payload={**_execution_payload(model), "finish_reason": "stop"},
            )


class GoogleGenerativeProtocolAdapter:
    protocol: ModelProtocol = "google_generative"

    def __init__(self, *, http_client: OpenAICompatibleHttpClient | None = None) -> None:
        self.http_client = http_client

    def stream(
        self,
        provider: ProviderDescriptor,
        model: ModelDescriptor,
        request: ProviderRequest,
    ) -> Iterator[ProviderEvent]:
        normalized = _request_with_selection(request, provider=provider, model=model)
        yield provider_event(
            ProviderEventKind.MODEL_STARTED,
            sequence=1,
            request=normalized,
            payload=_started_payload(provider, model, normalized),
        )
        control = _stream_control(normalized)
        try:
            control.check()
            payload = _google_generative_payload(provider, model, normalized)
            sequence = 2
            completed = False
            state: dict[str, Any] = {"tool_index": 0}
            client = self.http_client or _default_openai_http_client()
            timeout = float(payload.pop("_timeout_seconds", 30))
            for chunk in _client_stream_sse_json(
                client,
                _google_generative_stream_url(model.endpoint or provider.endpoint, model.api_id or model.model_id),
                headers=_google_generative_headers(provider, normalized),
                payload=payload,
                timeout=control.transport_timeout(timeout),
                abort_checker=control.abort_checker,
            ):
                control.check()
                for event in _google_generative_events(chunk, state, sequence, normalized, model):
                    yield event
                    if event.kind == ProviderEventKind.MODEL_FAILED:
                        return
                    if event.kind == ProviderEventKind.MODEL_COMPLETED:
                        completed = True
                    sequence += 1
                    control.check()
        except ProviderStreamAbortError as exc:
            yield _provider_abort_event(exc, sequence=_current_sequence(locals()), request=normalized, model=model)
            return
        except ProviderStreamTimeoutError as exc:
            yield provider_error_event(classify_provider_exception(exc), sequence=_current_sequence(locals()), request=normalized)
            return
        except Exception as exc:
            yield provider_error_event(
                classify_provider_exception(exc),
                sequence=2,
                request=normalized,
            )
            return
        if not completed:
            yield provider_event(
                ProviderEventKind.MODEL_COMPLETED,
                sequence=sequence,
                request=normalized,
                payload={**_execution_payload(model), "finish_reason": "stop"},
            )


class BedrockConverseProtocolAdapter:
    protocol: ModelProtocol = "bedrock_converse"

    def __init__(self, *, http_client: OpenAICompatibleHttpClient | None = None) -> None:
        self.http_client = http_client

    def stream(
        self,
        provider: ProviderDescriptor,
        model: ModelDescriptor,
        request: ProviderRequest,
    ) -> Iterator[ProviderEvent]:
        normalized = _request_with_selection(request, provider=provider, model=model)
        yield provider_event(
            ProviderEventKind.MODEL_STARTED,
            sequence=1,
            request=normalized,
            payload=_started_payload(provider, model, normalized),
        )
        control = _stream_control(normalized)
        try:
            control.check()
            payload = _bedrock_converse_payload(provider, model, normalized)
            sequence = 2
            completed = False
            state: dict[str, Any] = {"blocks": {}, "tool_index": 0}
            client = self.http_client or _default_openai_http_client()
            timeout = float(payload.pop("_timeout_seconds", 30))
            url = _bedrock_converse_stream_url(_bedrock_converse_base_url(provider, model, normalized), model.api_id or model.model_id)
            headers = _bedrock_converse_headers(provider, model, normalized, url=url, payload=payload)
            for chunk in _client_stream_sse_json(client, url, headers=headers, payload=payload, timeout=control.transport_timeout(timeout), abort_checker=control.abort_checker):
                control.check()
                for event in _bedrock_converse_events(chunk, state, sequence, normalized, model):
                    yield event
                    if event.kind == ProviderEventKind.MODEL_FAILED:
                        return
                    if event.kind == ProviderEventKind.MODEL_COMPLETED:
                        completed = True
                    sequence += 1
                    control.check()
        except ProviderStreamAbortError as exc:
            yield _provider_abort_event(exc, sequence=_current_sequence(locals()), request=normalized, model=model)
            return
        except ProviderStreamTimeoutError as exc:
            yield provider_error_event(classify_provider_exception(exc), sequence=_current_sequence(locals()), request=normalized)
            return
        except Exception as exc:
            yield provider_error_event(
                classify_provider_exception(exc),
                sequence=2,
                request=normalized,
            )
            return
        if not completed:
            yield provider_event(
                ProviderEventKind.MODEL_COMPLETED,
                sequence=sequence,
                request=normalized,
                payload={**_execution_payload(model), "finish_reason": "stop"},
            )


def build_default_protocol_adapter_registry() -> ProtocolAdapterRegistry:
    registry = ProtocolAdapterRegistry()
    registry.register(CodexCliProtocolAdapter())
    registry.register(OpenAIChatProtocolAdapter())
    registry.register(OpenAIResponsesProtocolAdapter())
    registry.register(OpenAICodexResponsesProtocolAdapter())
    registry.register(AnthropicMessagesProtocolAdapter())
    registry.register(GoogleGenerativeProtocolAdapter())
    registry.register(BedrockConverseProtocolAdapter())
    return registry


def protocol_adapter_missing_error(protocol: str) -> ProviderError:
    return ProviderError(
        category=ProviderErrorCategory.CONFIGURATION,
        error_type="ProtocolAdapterNotFound",
        message=f"No protocol adapter registered for protocol: {protocol}",
        retryable=False,
        hidden_provider_fallback=False,
        no_hidden_fallback=True,
    )


def _default_openai_http_client() -> OpenAICompatibleHttpClient:
    return UrllibOpenAICompatibleHttpClient()


def _openai_responses_payload(provider: ProviderDescriptor, model: ModelDescriptor, request: ProviderRequest) -> dict[str, Any]:
    provider_options, model_options = _resolved_request_options(request, provider, model)
    payload: dict[str, Any] = {
        "model": model.api_id or model.model_id,
        "input": _canonical_messages_to_openai_responses_input(canonical_messages_from_provider_request(request)),
        "stream": True,
    }
    if model_options.get("temperature") is not None or provider_options.get("temperature") is not None:
        payload["temperature"] = model_options.get("temperature", provider_options.get("temperature"))
    max_tokens = model_options.get("max_output_tokens", model_options.get("max_tokens", provider_options.get("max_tokens")))
    if max_tokens is not None:
        payload["max_output_tokens"] = max_tokens
    effort = model_options.get("model_reasoning_effort", provider_options.get("model_reasoning_effort"))
    if effort is not None and model.reasoning_support == "effort":
        payload["reasoning"] = {"effort": effort}
    payload["_timeout_seconds"] = provider_options.get("timeout_seconds", 30)
    return payload


def _openai_codex_responses_payload(provider: ProviderDescriptor, model: ModelDescriptor, request: ProviderRequest) -> dict[str, Any]:
    provider_options, model_options = _resolved_request_options(request, provider, model)
    payload = _openai_responses_payload(provider, model, request)
    metadata = {
        **_string_metadata(provider_options.get("metadata")),
        **_string_metadata(model_options.get("metadata")),
        "harness_protocol": "openai_codex_responses",
    }
    codex_metadata = _codex_responses_metadata(provider_options, model_options)
    if codex_metadata:
        metadata.update(codex_metadata)
    payload["metadata"] = metadata
    return payload


def _codex_responses_metadata(provider_options: dict[str, Any], model_options: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    effort = model_options.get("model_reasoning_effort", provider_options.get("model_reasoning_effort"))
    if effort is not None:
        result["harness_reasoning_effort"] = str(effort)
    approval = (
        model_options.get("codex_approval_policy")
        or model_options.get("approval_policy")
        or model_options.get("ask_for_approval")
        or provider_options.get("codex_approval_policy")
        or provider_options.get("approval_policy")
        or provider_options.get("ask_for_approval")
    )
    if approval is not None:
        result["harness_approval_policy"] = str(approval)
    sandbox = model_options.get("codex_sandbox") or model_options.get("sandbox") or provider_options.get("codex_sandbox") or provider_options.get("sandbox")
    if sandbox is not None:
        result["harness_sandbox"] = str(sandbox)
    tool_policy = model_options.get("codex_tool_policy") or model_options.get("tool_policy") or provider_options.get("codex_tool_policy") or provider_options.get("tool_policy")
    if tool_policy is not None:
        result["harness_tool_policy"] = str(tool_policy)
    return result


def _string_metadata(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(key): str(item) for key, item in value.items() if key and item is not None}


def _canonical_messages_to_openai_responses_input(messages) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for message in messages:
        content: list[dict[str, Any]] = []
        for part in message.parts:
            if part.kind == "provider_metadata":
                continue
            if part.kind != "text":
                from harness.provider_content import UnsupportedCanonicalPartError

                raise UnsupportedCanonicalPartError(part.kind, "openai_responses", role=message.role)
            if part.text:
                content.append({"type": "input_text", "text": part.text})
        items.append({"role": message.role, "content": content})
    return items


def _openai_responses_headers(provider: ProviderDescriptor, request: ProviderRequest) -> dict[str, str]:
    headers = _credential_extra_headers(request)
    token = _credential_api_key(provider, request)
    return {**headers, "Content-Type": "application/json", "Authorization": f"Bearer {token}"}


def _anthropic_messages_payload(provider: ProviderDescriptor, model: ModelDescriptor, request: ProviderRequest) -> dict[str, Any]:
    provider_options, model_options = _resolved_request_options(request, provider, model)
    cache_control = _anthropic_cache_control(model, model_options=model_options)
    lowered = _canonical_messages_to_anthropic_messages(
        canonical_messages_from_provider_request(request),
        model=model,
        cache_control=cache_control,
    )
    max_tokens = model_options.get("max_tokens", provider_options.get("max_tokens", model.max_output_tokens or 4096))
    payload: dict[str, Any] = {
        "model": model.api_id or model.model_id,
        "messages": lowered["messages"],
        "max_tokens": max_tokens,
        "stream": True,
    }
    if lowered["system"]:
        payload["system"] = lowered["system"]
    if model_options.get("temperature") is not None or provider_options.get("temperature") is not None:
        payload["temperature"] = model_options.get("temperature", provider_options.get("temperature"))
    thinking_budget = (
        model_options.get("thinking_budget_tokens")
        or provider_options.get("thinking_budget_tokens")
        or model.model_options.get("thinking_budget_tokens")
        or model.provider_options.get("thinking_budget_tokens")
    )
    if thinking_budget is not None:
        payload["thinking"] = {"type": "enabled", "budget_tokens": thinking_budget}
    payload["_timeout_seconds"] = provider_options.get("timeout_seconds", 30)
    return payload


def _canonical_messages_to_anthropic_messages(
    messages: list[CanonicalMessage],
    *,
    model: ModelDescriptor,
    cache_control: dict[str, str] | None = None,
) -> dict[str, Any]:
    lowered: list[dict[str, Any]] = []
    system_parts: list[str] = []
    for message in messages:
        content: list[dict[str, Any]] = []
        for part in message.parts:
            if part.kind == "provider_metadata":
                continue
            if message.role in {"system", "developer"}:
                if part.kind != "text":
                    raise UnsupportedCanonicalPartError(part.kind, "anthropic_messages", role=message.role)
                if part.text:
                    system_parts.append(part.text)
                continue
            if message.role == "assistant":
                if part.kind == "text" and part.text:
                    content.append({"type": "text", "text": part.text})
                    continue
                if part.kind == "reasoning" and part.text:
                    block = {"type": "thinking", "thinking": part.text}
                    signature = part.signature or part.metadata.get("signature")
                    if signature:
                        block["signature"] = signature
                    content.append(block)
                    continue
                if part.kind == "tool_call":
                    call_id = str(part.provider_native_id or part.data.get("tool_call_id") or "")
                    tool_name = str(part.data.get("tool_name") or part.data.get("name") or "")
                    content.append(
                        {
                            "type": "tool_use",
                            "id": call_id,
                            "name": tool_name,
                            "input": part.data.get("arguments", part.data.get("input", {})),
                        }
                    )
                    continue
                raise UnsupportedCanonicalPartError(part.kind, "anthropic_messages", role=message.role)
            if message.role == "tool" or part.kind == "tool_result":
                if part.kind not in {"text", "tool_result"}:
                    raise UnsupportedCanonicalPartError(part.kind, "anthropic_messages", role=message.role)
                tool_use_id = str(part.provider_native_id or part.data.get("tool_call_id") or message.metadata.get("tool_call_id") or "")
                content.append({"type": "tool_result", "tool_use_id": tool_use_id, "content": part.text or str(part.data.get("output") or "")})
                continue
            if message.role == "user":
                if part.kind != "text":
                    raise UnsupportedCanonicalPartError(part.kind, "anthropic_messages", role=message.role)
                if part.text:
                    content.append({"type": "text", "text": part.text})
                continue
            raise UnsupportedCanonicalPartError(part.kind, "anthropic_messages", role=message.role)
        if content:
            if cache_control:
                content[-1] = {**content[-1], "cache_control": cache_control}
            lowered.append({"role": "assistant" if message.role == "assistant" else "user", "content": content})
    return {"system": "\n\n".join(system_parts), "messages": lowered}


def _anthropic_cache_control(model: ModelDescriptor, *, model_options: dict[str, Any] | None = None) -> dict[str, str] | None:
    options = model_options or {}
    value = (
        options.get("cache_retention")
        or options.get("cache_control")
        or model.model_options.get("cache_control")
        or model.provider_options.get("cache_control")
    )
    if value == "ephemeral":
        return {"type": "ephemeral"}
    if value in {"1h", "long"}:
        return {"type": "ephemeral", "ttl": "1h"}
    if value == "5m":
        return {"type": "ephemeral", "ttl": "5m"}
    if isinstance(value, dict) and value.get("type") == "ephemeral":
        result = {"type": "ephemeral"}
        ttl = value.get("ttl")
        if ttl in {"5m", "1h"}:
            result["ttl"] = str(ttl)
        return result
    return None


def _anthropic_messages_headers(provider: ProviderDescriptor, request: ProviderRequest) -> dict[str, str]:
    headers = _credential_extra_headers(request)
    token = _credential_api_key(provider, request)
    betas = _anthropic_beta_headers(request)
    return {
        **headers,
        "Content-Type": "application/json",
        "x-api-key": token,
        "anthropic-version": "2023-06-01",
        **({"anthropic-beta": ",".join(betas)} if betas else {}),
    }


def _anthropic_beta_headers(request: ProviderRequest) -> list[str]:
    request_provider_options = request.metadata.get("resolved_provider_options")
    request_model_options = request.metadata.get("resolved_model_options")
    provider_options = dict(request_provider_options) if isinstance(request_provider_options, dict) else {}
    model_options = dict(request_model_options) if isinstance(request_model_options, dict) else {}
    values: list[str] = []
    for options in (provider_options, model_options):
        for key in ("anthropic_beta", "anthropic_beta_headers", "anthropic_betas", "beta_headers"):
            value = options.get(key)
            if isinstance(value, str) and value.strip():
                values.extend(item.strip() for item in value.split(",") if item.strip())
            elif isinstance(value, list):
                values.extend(str(item).strip() for item in value if str(item).strip())
    deduped: list[str] = []
    for value in values:
        if value not in deduped:
            deduped.append(value)
    return deduped


def _google_generative_payload(provider: ProviderDescriptor, model: ModelDescriptor, request: ProviderRequest) -> dict[str, Any]:
    provider_options, model_options = _resolved_request_options(request, provider, model)
    lowered = _canonical_messages_to_google_contents(canonical_messages_from_provider_request(request))
    generation_config: dict[str, Any] = {}
    if model_options.get("temperature") is not None or provider_options.get("temperature") is not None:
        generation_config["temperature"] = model_options.get("temperature", provider_options.get("temperature"))
    max_tokens = model_options.get("max_tokens", provider_options.get("max_tokens", model.max_output_tokens))
    if max_tokens is not None:
        generation_config["maxOutputTokens"] = max_tokens
    thinking_budget = (
        model_options.get("thinking_budget_tokens")
        or provider_options.get("thinking_budget_tokens")
        or model.model_options.get("thinking_budget_tokens")
        or model.provider_options.get("thinking_budget_tokens")
    )
    if thinking_budget is not None:
        generation_config["thinkingConfig"] = {"thinkingBudget": thinking_budget}
    payload: dict[str, Any] = {"contents": lowered["contents"]}
    if lowered["system"]:
        payload["systemInstruction"] = {"parts": [{"text": lowered["system"]}]}
    if generation_config:
        payload["generationConfig"] = generation_config
    payload["_timeout_seconds"] = provider_options.get("timeout_seconds", 30)
    return payload


def _canonical_messages_to_google_contents(messages: list[CanonicalMessage]) -> dict[str, Any]:
    contents: list[dict[str, Any]] = []
    system_parts: list[str] = []
    for message in messages:
        parts: list[dict[str, Any]] = []
        for part in message.parts:
            if part.kind == "provider_metadata":
                continue
            if message.role in {"system", "developer"}:
                if part.kind != "text":
                    raise UnsupportedCanonicalPartError(part.kind, "google_generative", role=message.role)
                if part.text:
                    system_parts.append(part.text)
                continue
            if part.kind == "text":
                if part.text:
                    parts.append({"text": part.text})
                continue
            if part.kind == "reasoning" and message.role == "assistant":
                item = {"text": part.text or "", "thought": True}
                signature = part.signature or part.metadata.get("thought_signature") or part.metadata.get("signature")
                if signature:
                    item["thoughtSignature"] = signature
                parts.append(item)
                continue
            if part.kind == "image_input" and message.role == "user":
                media_type = str(part.data.get("media_type") or part.data.get("mime_type") or "")
                data = part.data.get("data") or part.data.get("base64")
                uri = part.data.get("uri") or part.data.get("ref")
                if media_type and isinstance(data, str) and data:
                    parts.append({"inlineData": {"mimeType": media_type, "data": data}})
                    continue
                if media_type and isinstance(uri, str) and uri:
                    parts.append({"fileData": {"mimeType": media_type, "fileUri": uri}})
                    continue
                raise UnsupportedCanonicalPartError(part.kind, "google_generative", role=message.role)
            if part.kind == "tool_call" and message.role == "assistant":
                tool_name = str(part.data.get("tool_name") or part.data.get("name") or "")
                parts.append({"functionCall": {"name": tool_name, "args": part.data.get("arguments", part.data.get("input", {}))}})
                continue
            if part.kind == "tool_result" or message.role == "tool":
                if part.kind not in {"tool_result", "text"}:
                    raise UnsupportedCanonicalPartError(part.kind, "google_generative", role=message.role)
                tool_name = str(part.data.get("tool_name") or part.data.get("name") or message.metadata.get("tool_name") or "")
                response = part.data.get("response")
                if response is None:
                    response = {"output": part.text or part.data.get("output") or ""}
                parts.append({"functionResponse": {"name": tool_name, "response": response}})
                continue
            raise UnsupportedCanonicalPartError(part.kind, "google_generative", role=message.role)
        if parts:
            contents.append({"role": "model" if message.role == "assistant" else "user", "parts": parts})
    return {"system": "\n\n".join(system_parts), "contents": contents}


def _google_generative_headers(provider: ProviderDescriptor, request: ProviderRequest) -> dict[str, str]:
    headers = _credential_extra_headers(request)
    credential = _provider_credential_from_request(request)
    token = _credential_api_key(provider, request)
    auth_kind = str(credential.get("credential_kind") or credential.get("kind") or "").casefold()
    auth_source = str(credential.get("source") or "").casefold()
    if auth_kind in {"oauth", "bearer"} or "oauth" in auth_source:
        return {**headers, "Content-Type": "application/json", "Authorization": f"Bearer {token}"}
    return {**headers, "Content-Type": "application/json", "x-goog-api-key": token}


def _google_generative_stream_url(base_url: str | None, model_id: str) -> str:
    if not base_url:
        raise ValueError("Google Generative provider endpoint is missing.")
    quoted = urllib.parse.quote(model_id, safe="")
    return urllib.parse.urljoin(base_url.rstrip("/") + "/", f"models/{quoted}:streamGenerateContent?alt=sse")


def _bedrock_converse_payload(provider: ProviderDescriptor, model: ModelDescriptor, request: ProviderRequest) -> dict[str, Any]:
    provider_options, model_options = _resolved_request_options(request, provider, model)
    lowered = _canonical_messages_to_bedrock_messages(canonical_messages_from_provider_request(request))
    inference: dict[str, Any] = {}
    if model_options.get("temperature") is not None or provider_options.get("temperature") is not None:
        inference["temperature"] = model_options.get("temperature", provider_options.get("temperature"))
    max_tokens = model_options.get("max_tokens", provider_options.get("max_tokens", model.max_output_tokens))
    if max_tokens is not None:
        inference["maxTokens"] = max_tokens
    payload: dict[str, Any] = {"messages": lowered["messages"]}
    if lowered["system"]:
        payload["system"] = [{"text": lowered["system"]}]
    if inference:
        payload["inferenceConfig"] = inference
    payload["_timeout_seconds"] = provider_options.get("timeout_seconds", 30)
    return payload


def _canonical_messages_to_bedrock_messages(messages: list[CanonicalMessage]) -> dict[str, Any]:
    lowered: list[dict[str, Any]] = []
    system_parts: list[str] = []
    for message in messages:
        content: list[dict[str, Any]] = []
        for part in message.parts:
            if part.kind == "provider_metadata":
                continue
            if message.role in {"system", "developer"}:
                if part.kind != "text":
                    raise UnsupportedCanonicalPartError(part.kind, "bedrock_converse", role=message.role)
                if part.text:
                    system_parts.append(part.text)
                continue
            if part.kind == "text":
                if part.text:
                    content.append({"text": part.text})
                continue
            if part.kind == "image_input" and message.role == "user":
                media_type = str(part.data.get("media_type") or part.data.get("mime_type") or "")
                data = part.data.get("data") or part.data.get("bytes")
                if media_type and data:
                    image_format = media_type.rsplit("/", 1)[-1]
                    content.append({"image": {"format": image_format, "source": {"bytes": data}}})
                    continue
                raise UnsupportedCanonicalPartError(part.kind, "bedrock_converse", role=message.role)
            if part.kind == "tool_call" and message.role == "assistant":
                tool_use_id = str(part.provider_native_id or part.data.get("tool_call_id") or "")
                name = str(part.data.get("tool_name") or part.data.get("name") or "")
                content.append({"toolUse": {"toolUseId": tool_use_id, "name": name, "input": part.data.get("arguments", part.data.get("input", {}))}})
                continue
            if part.kind == "tool_result" or message.role == "tool":
                if part.kind not in {"tool_result", "text"}:
                    raise UnsupportedCanonicalPartError(part.kind, "bedrock_converse", role=message.role)
                tool_use_id = str(part.provider_native_id or part.data.get("tool_call_id") or message.metadata.get("tool_call_id") or "")
                content.append({"toolResult": {"toolUseId": tool_use_id, "content": [{"text": part.text or str(part.data.get("output") or "")}]}})
                continue
            raise UnsupportedCanonicalPartError(part.kind, "bedrock_converse", role=message.role)
        if content:
            lowered.append({"role": "assistant" if message.role == "assistant" else "user", "content": content})
    return {"system": "\n\n".join(system_parts), "messages": lowered}


def _bedrock_converse_headers(
    provider: ProviderDescriptor,
    model: ModelDescriptor,
    request: ProviderRequest,
    *,
    url: str,
    payload: dict[str, Any],
) -> dict[str, str]:
    region = _bedrock_aws_region(provider, model, request)
    credential = _provider_credential_from_request(request)
    source = str(credential.get("source") or (provider.credential.source if provider.credential else "none"))
    base_headers = {
        **_credential_extra_headers(request),
        "Content-Type": "application/json",
        "X-Harness-AWS-Credential-Source": source,
        "X-Harness-AWS-Region": str(region or "unknown"),
    }
    auth = _bedrock_auth_material(provider, model, request)
    if auth.get("bearer_token"):
        return {**base_headers, "Authorization": f"Bearer {auth['bearer_token']}"}
    return _bedrock_sigv4_headers(base_headers, url=url, payload=payload, region=str(region or "us-east-1"), auth=auth)


def _bedrock_converse_base_url(provider: ProviderDescriptor, model: ModelDescriptor, request: ProviderRequest) -> str | None:
    base_url = model.endpoint or provider.endpoint
    region = _bedrock_aws_region(provider, model, request)
    if not base_url or not region:
        return base_url
    parsed = urllib.parse.urlparse(base_url)
    host_parts = parsed.netloc.split(".")
    if len(host_parts) >= 4 and host_parts[0] == "bedrock-runtime" and host_parts[2] == "amazonaws":
        host_parts[1] = str(region)
        return urllib.parse.urlunparse(parsed._replace(netloc=".".join(host_parts)))
    return base_url


def _bedrock_aws_region(provider: ProviderDescriptor, model: ModelDescriptor, request: ProviderRequest) -> str:
    provider_options, model_options = _resolved_request_options(request, provider, model)
    region = (
        model_options.get("aws_region")
        or provider_options.get("aws_region")
        or model.provider_options.get("aws_region")
        or provider.protocol_defaults.get("aws_region")
        or _region_from_bedrock_endpoint(model.endpoint or provider.endpoint)
        or "us-east-1"
    )
    return str(region)


def _region_from_bedrock_endpoint(endpoint: str | None) -> str | None:
    if not endpoint:
        return None
    host = urllib.parse.urlparse(endpoint).netloc
    parts = host.split(".")
    if len(parts) >= 3 and parts[0] == "bedrock-runtime":
        return parts[1]
    return None


def _bedrock_auth_material(provider: ProviderDescriptor, model: ModelDescriptor, request: ProviderRequest) -> dict[str, str]:
    credential = _provider_credential_from_request(request)
    kind = str(credential.get("credential_kind") or credential.get("kind") or "").casefold()
    source = str(credential.get("source") or "").casefold()
    api_key = credential.get("api_key")
    if kind in {"oauth", "bearer", "api_key"} and isinstance(api_key, str) and api_key:
        return {"bearer_token": api_key}
    access_key_id = credential.get("access_key_id") or credential.get("aws_access_key_id")
    secret_access_key = credential.get("secret_access_key") or credential.get("aws_secret_access_key")
    session_token = credential.get("session_token") or credential.get("aws_session_token")
    if isinstance(access_key_id, str) and access_key_id and isinstance(secret_access_key, str) and secret_access_key:
        material = {"access_key_id": access_key_id, "secret_access_key": secret_access_key}
        if isinstance(session_token, str) and session_token:
            material["session_token"] = session_token
        return material
    if kind == "aws_env" or source == "aws_env":
        return _bedrock_auth_material_from_env(provider.provider_id)
    if kind == "aws_profile" or source in {"aws_profile", "provider_account"} or provider.provider_id == "bedrock":
        provider_options, _model_options = _resolved_request_options(request, provider, model)
        profile = str(credential.get("profile") or provider_options.get("aws_profile") or os.environ.get(str(credential.get("env_var") or "AWS_PROFILE")) or "default")
        return _bedrock_auth_material_from_profile(provider.provider_id, profile)
    raise ProviderCredentialResolutionError(provider.provider_id, "credential_missing")


def _bedrock_auth_material_from_env(provider_id: str) -> dict[str, str]:
    access_key_id = os.environ.get("AWS_ACCESS_KEY_ID")
    secret_access_key = os.environ.get("AWS_SECRET_ACCESS_KEY")
    if not access_key_id or not secret_access_key:
        raise ProviderCredentialResolutionError(provider_id, "credential_missing")
    material = {"access_key_id": access_key_id, "secret_access_key": secret_access_key}
    session_token = os.environ.get("AWS_SESSION_TOKEN")
    if session_token:
        material["session_token"] = session_token
    return material


def _bedrock_auth_material_from_profile(provider_id: str, profile: str) -> dict[str, str]:
    parser = configparser.ConfigParser()
    credentials_file = Path(os.environ.get("AWS_SHARED_CREDENTIALS_FILE") or Path.home() / ".aws" / "credentials")
    config_file = Path(os.environ.get("AWS_CONFIG_FILE") or Path.home() / ".aws" / "config")
    parser.read([str(credentials_file), str(config_file)])
    section_names = [profile, f"profile {profile}"] if profile != "default" else ["default", "profile default"]
    for section in section_names:
        if not parser.has_section(section):
            continue
        access_key_id = parser.get(section, "aws_access_key_id", fallback="").strip()
        secret_access_key = parser.get(section, "aws_secret_access_key", fallback="").strip()
        if access_key_id and secret_access_key:
            material = {"access_key_id": access_key_id, "secret_access_key": secret_access_key}
            session_token = parser.get(section, "aws_session_token", fallback="").strip()
            if session_token:
                material["session_token"] = session_token
            return material
    raise ProviderCredentialResolutionError(provider_id, "credential_missing")


def _bedrock_sigv4_headers(
    headers: dict[str, str],
    *,
    url: str,
    payload: dict[str, Any],
    region: str,
    auth: dict[str, str],
) -> dict[str, str]:
    access_key_id = auth.get("access_key_id")
    secret_access_key = auth.get("secret_access_key")
    if not access_key_id or not secret_access_key:
        raise ProviderCredentialResolutionError("bedrock", "credential_missing")
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc
    now = datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    body = json.dumps(payload).encode("utf-8")
    payload_hash = hashlib.sha256(body).hexdigest()
    signed_header_values = {
        "content-type": headers.get("Content-Type", "application/json"),
        "host": host,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amz_date,
    }
    if auth.get("session_token"):
        signed_header_values["x-amz-security-token"] = auth["session_token"]
    canonical_headers = "".join(f"{key}:{signed_header_values[key]}\n" for key in sorted(signed_header_values))
    signed_headers = ";".join(sorted(signed_header_values))
    canonical_query = _canonical_query(parsed.query)
    canonical_request = "\n".join(
        [
            "POST",
            parsed.path or "/",
            canonical_query,
            canonical_headers,
            signed_headers,
            payload_hash,
        ]
    )
    credential_scope = f"{date_stamp}/{region}/bedrock/aws4_request"
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            credential_scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ]
    )
    signing_key = _aws_sigv4_signing_key(secret_access_key, date_stamp, region, "bedrock")
    signature = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    authorization = (
        "AWS4-HMAC-SHA256 "
        f"Credential={access_key_id}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, "
        f"Signature={signature}"
    )
    signed = {
        **headers,
        "Host": host,
        "X-Amz-Date": amz_date,
        "X-Amz-Content-Sha256": payload_hash,
        "Authorization": authorization,
    }
    if auth.get("session_token"):
        signed["X-Amz-Security-Token"] = auth["session_token"]
    return signed


def _canonical_query(query: str) -> str:
    pairs = urllib.parse.parse_qsl(query, keep_blank_values=True)
    return "&".join(
        f"{urllib.parse.quote(key, safe='-_.~')}={urllib.parse.quote(value, safe='-_.~')}"
        for key, value in sorted(pairs)
    )


def _aws_sigv4_signing_key(secret_access_key: str, date_stamp: str, region: str, service: str) -> bytes:
    date_key = hmac.new(("AWS4" + secret_access_key).encode("utf-8"), date_stamp.encode("utf-8"), hashlib.sha256).digest()
    region_key = hmac.new(date_key, region.encode("utf-8"), hashlib.sha256).digest()
    service_key = hmac.new(region_key, service.encode("utf-8"), hashlib.sha256).digest()
    return hmac.new(service_key, b"aws4_request", hashlib.sha256).digest()


def _bedrock_converse_stream_url(base_url: str | None, model_id: str) -> str:
    if not base_url:
        raise ValueError("Bedrock provider endpoint is missing.")
    quoted = urllib.parse.quote(model_id, safe="")
    return urllib.parse.urljoin(base_url.rstrip("/") + "/", f"model/{quoted}/converse-stream")


def _join_url(base_url: str | None, path: str) -> str:
    if not base_url:
        raise ValueError("Responses provider endpoint is missing.")
    return urllib.parse.urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))


def _usage_event_payload(model: ModelDescriptor, payload: dict[str, Any]) -> dict[str, Any]:
    normalized = normalized_token_usage(payload)
    usage_payload = normalize_token_usage_payload(payload)
    usage_payload.pop("cost", None)
    usage_payload.pop("cost_usd", None)
    reported_cost = _provider_reported_cost(payload)
    if reported_cost is not None:
        usage_payload["provider_reported_cost"] = reported_cost
    estimated_cost = _estimated_usage_cost(model, normalized)
    if estimated_cost is not None:
        usage_payload["estimated_cost"] = estimated_cost
        usage_payload["estimated_cost_usd"] = estimated_cost["total"] if estimated_cost.get("currency") == "USD" else None
    return usage_payload


def _normalized_usage(payload: dict[str, Any]) -> dict[str, int | None]:
    return normalized_token_usage(payload)


def _provider_reported_cost(payload: dict[str, Any]) -> dict[str, Any] | None:
    if payload.get("provider_reported_cost") is not None:
        return _provider_reported_cost_payload(payload["provider_reported_cost"], source="provider_reported_cost")
    if payload.get("cost") is not None:
        return _provider_reported_cost_payload(payload["cost"], source="provider_usage_cost")
    if payload.get("cost_usd") is not None:
        return _provider_reported_cost_payload(
            payload["cost_usd"],
            source="provider_usage_cost_usd",
            currency="USD",
        )
    return None


def _provider_reported_cost_payload(value: Any, *, source: str, currency: str | None = None) -> dict[str, Any] | None:
    if isinstance(value, dict):
        payload = dict(value)
        total = _cost_number(payload.get("total") if payload.get("total") is not None else payload.get("cost_usd"))
        if total is not None:
            payload["total"] = total
        payload.setdefault("currency", currency or str(payload.get("currency") or "USD"))
        payload["estimated"] = False
        payload["source"] = source
        return payload
    total = _cost_number(value)
    if total is None:
        return None
    return {
        "currency": currency or "USD",
        "total": total,
        "estimated": False,
        "source": source,
    }


def _estimated_usage_cost(model: ModelDescriptor, usage: dict[str, int | None]) -> dict[str, Any] | None:
    if not isinstance(model.cost, dict):
        return None
    input_rate = _cost_rate(model.cost, "input_per_1m", "input_per_million")
    output_rate = _cost_rate(model.cost, "output_per_1m", "output_per_million")
    cache_read_rate = _cost_rate(model.cost, "cache_read_per_1m", "cache_read_per_million")
    cache_write_rate = _cost_rate(model.cost, "cache_write_per_1m", "cache_write_per_million")
    parts = {
        "input": _cost_part(usage.get("input_tokens"), input_rate),
        "output": _cost_part(usage.get("output_tokens"), output_rate),
        "cache_read": _cost_part(usage.get("cache_read_tokens"), cache_read_rate),
        "cache_write": _cost_part(usage.get("cache_write_tokens"), cache_write_rate),
    }
    if all(value is None for value in parts.values()):
        return None
    total = round(sum(value or 0.0 for value in parts.values()), 12)
    return {
        "currency": str(model.cost.get("currency") or "USD"),
        "total": total,
        "estimated": True,
        "source": "model_descriptor_pricing",
        "pricing_unit": "per_1m_tokens",
        **parts,
    }


def _cost_rate(cost: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = cost.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                continue
    return None


def _cost_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _cost_part(tokens: int | None, rate_per_million: float | None) -> float | None:
    if tokens is None or rate_per_million is None:
        return None
    return round((tokens / 1_000_000) * rate_per_million, 12)


def _openai_responses_events(
    chunk: dict[str, Any],
    state: dict[str, Any],
    sequence: int,
    request: ProviderRequest,
    model: ModelDescriptor,
) -> Iterator[ProviderEvent]:
    event_type = str(chunk.get("type") or "")
    response = chunk.get("response") if isinstance(chunk.get("response"), dict) else {}
    response_id = chunk.get("response_id") or chunk.get("id") or response.get("id") or state.get("response_id")
    if response_id:
        state["response_id"] = response_id
    for key in ("status", "background", "error", "incomplete_details", "metadata"):
        if key in response:
            state[key] = response.get(key)
    base = _openai_response_base_payload(model, state)
    if event_type in {"response.created", "response.in_progress"}:
        return
    if event_type in {"response.output_text.delta", "response.refusal.delta"}:
        delta = str(chunk.get("delta") or "")
        if delta:
            yield provider_event(
                ProviderEventKind.MODEL_MESSAGE_DELTA,
                sequence=sequence,
                request=request,
                text=delta,
                payload={**base, "delta": delta, **({"refusal": True} if event_type == "response.refusal.delta" else {})},
            )
        return
    if event_type in {"response.reasoning_summary_text.delta", "response.reasoning_text.delta", "response.reasoning.delta"}:
        delta = str(chunk.get("delta") or "")
        if delta:
            yield provider_event(ProviderEventKind.REASONING_SUMMARY_DELTA, sequence=sequence, request=request, text=delta, payload={**base, "delta": delta, "signature": chunk.get("signature")})
        return
    if event_type == "response.function_call_arguments.delta":
        call_id = str(chunk.get("call_id") or chunk.get("item_id") or "")
        state.setdefault("function_arguments", {})[call_id] = str(state.setdefault("function_arguments", {}).get(call_id) or "") + str(chunk.get("delta") or "")
        yield provider_event(
            ProviderEventKind.TOOL_CALL_DELTA,
            sequence=sequence,
            request=request,
            payload={**base, "tool_call_id": call_id or None, "arguments_delta": chunk.get("delta")},
            tool_call_id=call_id or None,
        )
        return
    if event_type == "response.output_item.done":
        item = chunk.get("item") if isinstance(chunk.get("item"), dict) else {}
        if item.get("type") in {"function_call", "tool_call"}:
            call_id = str(item.get("call_id") or item.get("id") or "")
            name = str(item.get("name") or "")
            arguments = item.get("arguments")
            if arguments is None:
                arguments = state.setdefault("function_arguments", {}).get(call_id)
            yield provider_event(
                ProviderEventKind.TOOL_CALL_COMPLETED,
                sequence=sequence,
                request=request,
                payload={**base, "tool_call_id": call_id or None, "tool_name": name or None, "arguments": arguments},
                tool_call_id=call_id or None,
                tool_name=name or None,
            )
        return
    if event_type == "response.completed":
        usage = response.get("usage") if isinstance(response.get("usage"), dict) else chunk.get("usage")
        if isinstance(usage, dict):
            yield provider_event(ProviderEventKind.TOKEN_USAGE_UPDATED, sequence=sequence, request=request, payload=_usage_event_payload(model, {**base, **usage}))
            sequence += 1
        yield provider_event(ProviderEventKind.MODEL_COMPLETED, sequence=sequence, request=request, payload={**base, "finish_reason": "stop"})
        return
    if event_type in {"response.failed", "error"}:
        error = chunk.get("error") if isinstance(chunk.get("error"), dict) else response.get("error") if isinstance(response.get("error"), dict) else {}
        yield provider_error_event(
            _provider_error_from_payload(error, default_type="OpenAIResponsesError", default_message="OpenAI Responses stream failed."),
            sequence=sequence,
            request=request,
        )


def _openai_response_base_payload(model: ModelDescriptor, state: dict[str, Any]) -> dict[str, Any]:
    base = {**_execution_payload(model), "response_id": state.get("response_id")}
    for key in ("status", "background", "error", "incomplete_details", "metadata"):
        if state.get(key) is not None:
            base[key] = state.get(key)
    return base


def _provider_error_from_payload(
    error: dict[str, Any],
    *,
    default_type: str,
    default_message: str,
) -> ProviderError:
    error_type = str(error.get("type") or error.get("code") or error.get("status") or default_type)
    message = str(error.get("message") or default_message)
    category = provider_error_category_for(error_type, message, error.get("details"))
    return ProviderError(
        category=category,
        error_type=error_type,
        message=message,
        retryable=provider_error_retryable_for(category, error_type, message, error.get("details")),
        retry_after_seconds=provider_retry_after_seconds_for(error),
        hidden_provider_fallback=False,
        no_hidden_fallback=True,
    )


def _google_generative_events(
    chunk: dict[str, Any],
    state: dict[str, Any],
    sequence: int,
    request: ProviderRequest,
    model: ModelDescriptor,
) -> Iterator[ProviderEvent]:
    base = {**_execution_payload(model)}
    if isinstance(chunk.get("error"), dict):
        error = chunk["error"]
        yield provider_error_event(
            _provider_error_from_payload(
                error,
                default_type="GoogleGenerativeError",
                default_message="Google Generative stream failed.",
            ),
            sequence=sequence,
            request=request,
        )
        return
    prompt_feedback = chunk.get("promptFeedback") if isinstance(chunk.get("promptFeedback"), dict) else None
    if prompt_feedback and prompt_feedback.get("blockReason"):
        yield provider_error_event(
            _google_policy_block_error(prompt_feedback.get("blockReason"), prompt_feedback),
            sequence=sequence,
            request=request,
        )
        return
    usage = chunk.get("usageMetadata") if isinstance(chunk.get("usageMetadata"), dict) else None
    if usage:
        yield provider_event(ProviderEventKind.TOKEN_USAGE_UPDATED, sequence=sequence, request=request, payload=_usage_event_payload(model, {**base, **usage}))
        sequence += 1
    candidates = chunk.get("candidates") if isinstance(chunk.get("candidates"), list) else []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        finish_reason = candidate.get("finishReason")
        if _google_finish_reason_is_policy_block(finish_reason):
            yield provider_error_event(
                _google_policy_block_error(finish_reason, candidate),
                sequence=sequence,
                request=request,
            )
            return
        content = candidate.get("content") if isinstance(candidate.get("content"), dict) else {}
        parts = content.get("parts") if isinstance(content.get("parts"), list) else []
        for part in parts:
            if not isinstance(part, dict):
                continue
            if isinstance(part.get("text"), str) and part.get("text"):
                text = str(part["text"])
                thought_signature = part.get("thoughtSignature")
                if part.get("thought") is True:
                    yield provider_event(
                        ProviderEventKind.REASONING_SUMMARY_DELTA,
                        sequence=sequence,
                        request=request,
                        text=text,
                        payload={**base, "delta": text, "thought_signature": thought_signature, "signature": thought_signature},
                    )
                else:
                    yield provider_event(ProviderEventKind.MODEL_MESSAGE_DELTA, sequence=sequence, request=request, text=text, payload={**base, "delta": text})
                sequence += 1
                continue
            function_call = part.get("functionCall") if isinstance(part.get("functionCall"), dict) else None
            if function_call is not None:
                state["tool_index"] = int(state.get("tool_index") or 0) + 1
                name = str(function_call.get("name") or "")
                call_id = str(function_call.get("id") or f"google_tool_{state['tool_index']}")
                yield provider_event(
                    ProviderEventKind.TOOL_CALL_COMPLETED,
                    sequence=sequence,
                    request=request,
                    payload={**base, "tool_call_id": call_id, "tool_name": name or None, "arguments": function_call.get("args", {})},
                    tool_call_id=call_id,
                    tool_name=name or None,
                )
                sequence += 1
        if finish_reason:
            yield provider_event(
                ProviderEventKind.MODEL_COMPLETED,
                sequence=sequence,
                request=request,
                payload={**base, "finish_reason": str(finish_reason).lower()},
            )


def _google_finish_reason_is_policy_block(finish_reason: Any) -> bool:
    normalized = str(finish_reason or "").casefold()
    return normalized in {"safety", "recitation", "prohibited_content", "spii", "blocklist", "image_safety"}


def _google_policy_block_error(reason: Any, details: dict[str, Any]) -> ProviderError:
    reason_text = str(reason or "blocked")
    return _provider_error_from_payload(
        {
            "status": f"GOOGLE_GENERATIVE_{reason_text}",
            "message": f"Google Generative blocked the response: {reason_text}",
            "details": details,
        },
        default_type="GoogleGenerativePolicyBlock",
        default_message="Google Generative blocked the response.",
    )


def _bedrock_converse_events(
    chunk: dict[str, Any],
    state: dict[str, Any],
    sequence: int,
    request: ProviderRequest,
    model: ModelDescriptor,
) -> Iterator[ProviderEvent]:
    base = {**_execution_payload(model)}
    if isinstance(chunk.get("error"), dict):
        error = chunk["error"]
        yield provider_error_event(
            _provider_error_from_payload(
                error,
                default_type="BedrockConverseError",
                default_message="Bedrock Converse stream failed.",
            ),
            sequence=sequence,
            request=request,
        )
        return
    if isinstance(chunk.get("messageStart"), dict):
        state["role"] = chunk["messageStart"].get("role")
        return
    if isinstance(chunk.get("contentBlockStart"), dict):
        item = chunk["contentBlockStart"]
        index = int(item.get("contentBlockIndex") or item.get("index") or 0)
        start = item.get("start") if isinstance(item.get("start"), dict) else {}
        tool = start.get("toolUse") if isinstance(start.get("toolUse"), dict) else None
        if tool is not None:
            block = {"type": "toolUse", **tool, "input_delta": ""}
            state.setdefault("blocks", {})[index] = block
            tool_id = str(tool.get("toolUseId") or "")
            name = str(tool.get("name") or "")
            yield provider_event(
                ProviderEventKind.TOOL_CALL_STARTED,
                sequence=sequence,
                request=request,
                payload={**base, "tool_call_id": tool_id or None, "tool_name": name or None, "arguments": tool.get("input", {})},
                tool_call_id=tool_id or None,
                tool_name=name or None,
            )
        return
    if isinstance(chunk.get("contentBlockDelta"), dict):
        item = chunk["contentBlockDelta"]
        index = int(item.get("contentBlockIndex") or item.get("index") or 0)
        delta = item.get("delta") if isinstance(item.get("delta"), dict) else {}
        if isinstance(delta.get("text"), str) and delta.get("text"):
            text = str(delta["text"])
            yield provider_event(ProviderEventKind.MODEL_MESSAGE_DELTA, sequence=sequence, request=request, text=text, payload={**base, "delta": text})
            return
        tool_delta = delta.get("toolUse") if isinstance(delta.get("toolUse"), dict) else None
        if tool_delta is not None:
            block = state.setdefault("blocks", {}).setdefault(index, {"type": "toolUse", "input_delta": ""})
            partial = str(tool_delta.get("input") or "")
            block["input_delta"] = str(block.get("input_delta") or "") + partial
            tool_id = str(block.get("toolUseId") or tool_delta.get("toolUseId") or "")
            name = str(block.get("name") or tool_delta.get("name") or "")
            yield provider_event(
                ProviderEventKind.TOOL_CALL_DELTA,
                sequence=sequence,
                request=request,
                payload={**base, "tool_call_id": tool_id or None, "tool_name": name or None, "arguments_delta": partial},
                tool_call_id=tool_id or None,
                tool_name=name or None,
            )
        return
    if isinstance(chunk.get("contentBlockStop"), dict):
        item = chunk["contentBlockStop"]
        index = int(item.get("contentBlockIndex") or item.get("index") or 0)
        block = state.setdefault("blocks", {}).get(index, {})
        if block.get("type") == "toolUse":
            tool_id = str(block.get("toolUseId") or "")
            name = str(block.get("name") or "")
            arguments = block.get("input_delta") or block.get("input")
            yield provider_event(
                ProviderEventKind.TOOL_CALL_COMPLETED,
                sequence=sequence,
                request=request,
                payload={**base, "tool_call_id": tool_id or None, "tool_name": name or None, "arguments": arguments},
                tool_call_id=tool_id or None,
                tool_name=name or None,
            )
        return
    if isinstance(chunk.get("metadata"), dict):
        metadata = chunk["metadata"]
        usage = metadata.get("usage") if isinstance(metadata.get("usage"), dict) else {}
        metrics = metadata.get("metrics") if isinstance(metadata.get("metrics"), dict) else {}
        if usage or metrics:
            yield provider_event(ProviderEventKind.TOKEN_USAGE_UPDATED, sequence=sequence, request=request, payload=_usage_event_payload(model, {**base, **usage, "metrics": metrics}))
        return
    if isinstance(chunk.get("messageStop"), dict):
        stop = chunk["messageStop"].get("stopReason")
        yield provider_event(
            ProviderEventKind.MODEL_COMPLETED,
            sequence=sequence,
            request=request,
            payload={**base, "finish_reason": str(stop or "stop")},
        )


def _anthropic_messages_events(
    chunk: dict[str, Any],
    state: dict[str, Any],
    sequence: int,
    request: ProviderRequest,
    model: ModelDescriptor,
) -> Iterator[ProviderEvent]:
    event_type = str(chunk.get("type") or "")
    base = {**_execution_payload(model)}
    blocks = state.setdefault("blocks", {})
    if event_type == "message_start":
        message = chunk.get("message") if isinstance(chunk.get("message"), dict) else {}
        if message.get("id"):
            state["message_id"] = message.get("id")
        usage = message.get("usage") if isinstance(message.get("usage"), dict) else None
        if usage:
            yield provider_event(
                ProviderEventKind.TOKEN_USAGE_UPDATED,
                sequence=sequence,
                request=request,
                payload=_usage_event_payload(model, {**base, "message_id": state.get("message_id"), **usage}),
            )
        return
    if event_type == "content_block_start":
        index = int(chunk.get("index") or 0)
        block = dict(chunk.get("content_block") if isinstance(chunk.get("content_block"), dict) else {})
        block.setdefault("arguments_delta", "")
        blocks[index] = block
        if block.get("type") == "tool_use":
            call_id = str(block.get("id") or "")
            name = str(block.get("name") or "")
            yield provider_event(
                ProviderEventKind.TOOL_CALL_STARTED,
                sequence=sequence,
                request=request,
                payload={**base, "message_id": state.get("message_id"), "tool_call_id": call_id or None, "tool_name": name or None, "arguments": block.get("input")},
                tool_call_id=call_id or None,
                tool_name=name or None,
            )
        return
    if event_type == "content_block_delta":
        index = int(chunk.get("index") or 0)
        block = blocks.setdefault(index, {})
        delta = chunk.get("delta") if isinstance(chunk.get("delta"), dict) else {}
        delta_type = str(delta.get("type") or "")
        if delta_type == "text_delta":
            text = str(delta.get("text") or "")
            if text:
                yield provider_event(ProviderEventKind.MODEL_MESSAGE_DELTA, sequence=sequence, request=request, text=text, payload={**base, "message_id": state.get("message_id"), "delta": text})
            return
        if delta_type == "thinking_delta":
            text = str(delta.get("thinking") or "")
            if text:
                yield provider_event(ProviderEventKind.REASONING_SUMMARY_DELTA, sequence=sequence, request=request, text=text, payload={**base, "message_id": state.get("message_id"), "delta": text, "signature": block.get("signature")})
            return
        if delta_type == "signature_delta":
            block["signature"] = delta.get("signature")
            return
        if delta_type == "input_json_delta":
            partial = str(delta.get("partial_json") or "")
            block["arguments_delta"] = str(block.get("arguments_delta") or "") + partial
            call_id = str(block.get("id") or "")
            name = str(block.get("name") or "")
            yield provider_event(
                ProviderEventKind.TOOL_CALL_DELTA,
                sequence=sequence,
                request=request,
                payload={**base, "message_id": state.get("message_id"), "tool_call_id": call_id or None, "tool_name": name or None, "arguments_delta": partial},
                tool_call_id=call_id or None,
                tool_name=name or None,
            )
            return
        return
    if event_type == "content_block_stop":
        index = int(chunk.get("index") or 0)
        block = blocks.get(index, {})
        if block.get("type") == "tool_use":
            call_id = str(block.get("id") or "")
            name = str(block.get("name") or "")
            arguments = block.get("arguments_delta") or block.get("input")
            yield provider_event(
                ProviderEventKind.TOOL_CALL_COMPLETED,
                sequence=sequence,
                request=request,
                payload={**base, "message_id": state.get("message_id"), "tool_call_id": call_id or None, "tool_name": name or None, "arguments": arguments},
                tool_call_id=call_id or None,
                tool_name=name or None,
            )
        return
    if event_type == "message_delta":
        delta = chunk.get("delta") if isinstance(chunk.get("delta"), dict) else {}
        if delta.get("stop_reason"):
            state["stop_reason"] = delta.get("stop_reason")
        usage = chunk.get("usage") if isinstance(chunk.get("usage"), dict) else {}
        if usage:
            yield provider_event(
                ProviderEventKind.TOKEN_USAGE_UPDATED,
                sequence=sequence,
                request=request,
                payload=_usage_event_payload(model, {**base, "message_id": state.get("message_id"), **usage}),
            )
        return
    if event_type == "message_stop":
        yield provider_event(
            ProviderEventKind.MODEL_COMPLETED,
            sequence=sequence,
            request=request,
            payload={**base, "message_id": state.get("message_id"), "finish_reason": state.get("stop_reason") or "stop"},
        )
        return
    if event_type == "error":
        error = chunk.get("error") if isinstance(chunk.get("error"), dict) else {}
        yield provider_error_event(
            _provider_error_from_payload(
                error,
                default_type="AnthropicMessagesError",
                default_message="Anthropic Messages stream failed.",
            ),
            sequence=sequence,
            request=request,
        )


def _openai_chat_backend_config(
    provider: ProviderDescriptor,
    model: ModelDescriptor,
    request: ProviderRequest,
) -> BackendConfig:
    provider_options, model_options = _resolved_request_options(request, provider, model)
    headers = {
        **_safe_headers(provider_options.get("headers")),
        **_safe_headers(model_options.get("headers")),
        **_credential_extra_headers(request),
    }
    reasoning = _openai_chat_reasoning_payload(provider_options, model_options, model)
    settings = {
        **provider_options,
        **model_options,
        "base_url": model.endpoint or provider.endpoint,
        "api_key": _credential_api_key(provider, request),
        "headers": headers,
        "abort_checker": _stream_control(request).abort_checker,
        **reasoning,
        "model": model.api_id or model.model_id,
    }
    settings["timeout_seconds"] = _stream_control(request).transport_timeout(float(settings.get("timeout_seconds", 30)))
    return BackendConfig(
        name=provider.backend_id or provider.provider_id,
        kind=BackendKind.NATIVE_MODEL,
        metadata=provider.metadata,
        capabilities=provider.capabilities,
        settings={key: value for key, value in settings.items() if value is not None},
    )


def _safe_headers(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(key): str(item) for key, item in value.items() if key and item is not None}


def _openai_chat_reasoning_payload(
    provider_options: dict[str, Any],
    model_options: dict[str, Any],
    model: ModelDescriptor,
) -> dict[str, Any]:
    if isinstance(model_options.get("reasoning"), dict):
        return {"reasoning": dict(model_options["reasoning"])}
    if isinstance(provider_options.get("reasoning"), dict):
        return {"reasoning": dict(provider_options["reasoning"])}
    effort = model_options.get("reasoning_effort") or provider_options.get("reasoning_effort")
    if effort is not None:
        return {"reasoning_effort": effort}
    effort = model_options.get("model_reasoning_effort") or provider_options.get("model_reasoning_effort")
    if effort is not None and (
        model_options.get("openrouter_reasoning")
        or provider_options.get("openrouter_reasoning")
        or str((model.provider_options or {}).get("compatibility") or "").casefold() == "openrouter"
    ):
        return {"reasoning": {"effort": effort}}
    return {}


def _codex_backend_config(
    provider: ProviderDescriptor,
    model: ModelDescriptor,
    request: ProviderRequest,
) -> BackendConfig:
    provider_options, model_options = _resolved_request_options(request, provider, model)
    settings = {
        **provider_options,
        **model_options,
        "command": provider_options.get("command") or "codex",
        "model": model.api_id or model.model_id,
        "abort_checker": _stream_control(request).abort_checker,
        "skip_git_repo_check": True,
    }
    settings["timeout_seconds"] = _stream_control(request).transport_timeout(float(settings.get("timeout_seconds", 900)))
    return BackendConfig(
        name=provider.backend_id or provider.provider_id,
        kind=BackendKind.EXTERNAL_AGENT,
        metadata=provider.metadata,
        capabilities=provider.capabilities,
        settings={key: value for key, value in settings.items() if value is not None},
    )


def _request_with_selection(
    request: ProviderRequest,
    *,
    provider: ProviderDescriptor,
    model: ModelDescriptor,
) -> ProviderRequest:
    metadata = {
        **request.metadata,
        "canonical_model_ref": model.raw_model_ref,
        "protocol": model.protocol,
        "model_descriptor_source": model.source,
    }
    return request.model_copy(
        update={
            "provider_id": request.provider_id or provider.provider_id,
            "model_ref": request.model_ref or model.raw_model_ref,
            "metadata": metadata,
        },
        deep=True,
    )


def _resolved_request_options(
    request: ProviderRequest,
    provider: ProviderDescriptor,
    model: ModelDescriptor,
) -> tuple[dict, dict]:
    request_provider_options = request.metadata.get("resolved_provider_options")
    request_model_options = request.metadata.get("resolved_model_options")
    provider_options = (
        dict(request_provider_options)
        if isinstance(request_provider_options, dict)
        else dict(provider.protocol_defaults)
    )
    model_options = (
        dict(request_model_options)
        if isinstance(request_model_options, dict)
        else dict(model.model_options)
    )
    return provider_options, model_options


def _provider_credential_from_request(request: ProviderRequest) -> dict[str, Any]:
    credential = request.metadata.get("provider_credential")
    return dict(credential) if isinstance(credential, dict) else {}


def _provider_credential_metadata_present(request: ProviderRequest) -> bool:
    return "provider_credential" in request.metadata


def _credential_extra_headers(request: ProviderRequest) -> dict[str, str]:
    credential = _provider_credential_from_request(request)
    headers = credential.get("headers")
    if not isinstance(headers, dict):
        return {}
    return {str(key): str(value) for key, value in headers.items() if value is not None}


def _credential_api_key(provider: ProviderDescriptor, request: ProviderRequest) -> str:
    credential = _provider_credential_from_request(request)
    api_key = credential.get("api_key")
    if isinstance(api_key, str) and api_key:
        return api_key
    status = str(credential.get("status") or "")
    if status in {"missing", "expired", "refresh_required", "unsupported"}:
        reason = {
            "missing": "credential_missing",
            "expired": "credential_expired",
            "refresh_required": "credential_refresh_required",
            "unsupported": "credential_kind_unsupported",
        }[status]
        raise ProviderCredentialResolutionError(provider.provider_id, reason)
    if _provider_credential_metadata_present(request):
        raise ProviderCredentialResolutionError(provider.provider_id, "credential_missing")
    if provider.credential is not None and provider.credential.env_var:
        token = os.environ.get(provider.credential.env_var)
        if token:
            return token
        raise ProviderCredentialResolutionError(provider.provider_id, "credential_missing")
    if _provider_is_local(provider):
        return "local"
    raise ProviderCredentialResolutionError(provider.provider_id, "credential_missing")


def _provider_is_local(provider: ProviderDescriptor) -> bool:
    boundary = getattr(provider.metadata.data_boundary, "value", provider.metadata.data_boundary)
    return str(boundary) == "local_only"


def _redacted_credential_evidence(request: ProviderRequest) -> dict[str, Any] | None:
    evidence = request.metadata.get("provider_credential_evidence")
    if isinstance(evidence, dict):
        return dict(evidence)
    credential = _provider_credential_from_request(request)
    if not credential:
        return None
    return {
        "schema_version": credential.get("schema_version") or "harness.resolved_provider_credential/v1",
        "provider_id": credential.get("provider_id"),
        "credential_kind": credential.get("credential_kind"),
        "status": credential.get("status"),
        "source": credential.get("source"),
        "env_var": credential.get("env_var"),
        "account_id": credential.get("account_id"),
        "expires_at": credential.get("expires_at"),
        "header_names": sorted((credential.get("headers") or {}).keys()) if isinstance(credential.get("headers"), dict) else [],
        "redaction_state": "redacted",
        "credential_value_included": False,
        "credentials_included": False,
        "network_accessed": False,
        "credential_written": False,
        "no_hidden_fallback": True,
    }


def _started_payload(provider: ProviderDescriptor, model: ModelDescriptor, request: ProviderRequest | None = None) -> dict:
    credential_evidence = _redacted_credential_evidence(request) if request is not None else None
    model_resolution_evidence = _model_resolution_evidence(request) if request is not None else {}
    return {
        "provider_id": provider.provider_id,
        "model_id": model.model_id,
        "canonical_model_ref": model.raw_model_ref,
        "protocol": model.protocol,
        "model_descriptor_source": model.source,
        "provider_execution_started": True,
        "model_execution_started": True,
        "hidden_provider_fallback": False,
        "hidden_model_fallback": False,
        "no_hidden_fallback": True,
        **({"provider_credential": credential_evidence} if credential_evidence else {}),
        **model_resolution_evidence,
    }


def _model_resolution_evidence(request: ProviderRequest) -> dict[str, Any]:
    evidence: dict[str, Any] = {}
    source = request.metadata.get("model_selection_source")
    if isinstance(source, str) and source:
        evidence["model_selection_source"] = source
    resolution = request.metadata.get("model_resolution")
    if isinstance(resolution, dict):
        evidence["model_resolution"] = dict(resolution)
    return evidence


def _execution_payload(model: ModelDescriptor) -> dict:
    return {
        "canonical_model_ref": model.raw_model_ref,
        "protocol": model.protocol,
        "hidden_provider_fallback": False,
        "hidden_model_fallback": False,
        "no_hidden_fallback": True,
    }


def _request_prompt(request: ProviderRequest) -> str:
    prompt = request.metadata.get("prompt")
    if isinstance(prompt, str) and prompt.strip():
        return prompt
    return canonical_messages_to_text_prompt(canonical_messages_from_provider_request(request))


def _optional_path(value: object) -> Path | None:
    if isinstance(value, str) and value.strip():
        return Path(value)
    return None
