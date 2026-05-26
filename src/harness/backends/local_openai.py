from __future__ import annotations

import json
import ipaddress
import inspect
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Iterator, Protocol

from harness.backends.streaming import BackendStreamEvent
from harness.models import BackendConfig, BackendStatus
from harness.provider_events import ProviderStreamAbortError


class BackendConfigError(ValueError):
    pass


class LocalEndpointUnavailable(RuntimeError):
    pass


LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}


def is_local_base_url(base_url: str, approved_lan_endpoints: list[str] | None = None) -> bool:
    parsed = urllib.parse.urlparse(base_url)
    host = parsed.hostname
    if not host:
        return False
    if host in {"localhost", "127.0.0.1", "::1"}:
        return True
    approved_lan_endpoints = approved_lan_endpoints or []
    if base_url.rstrip("/") in {item.rstrip("/") for item in approved_lan_endpoints}:
        return _host_is_loopback_or_lan(host)
    try:
        ip = socket.gethostbyname(host)
    except OSError:
        return False
    return ip.startswith("127.")


def validate_local_base_url(base_url: str, approved_lan_endpoints: list[str] | None = None) -> None:
    parsed = urllib.parse.urlparse(base_url)
    if parsed.scheme not in {"http", "https"}:
        raise BackendConfigError("Local backend base_url must use http or https.")
    if not is_local_base_url(base_url, approved_lan_endpoints):
        raise BackendConfigError(
            "local_openai_compatible base_url is not local. Use localhost, 127.0.0.1, "
            "[::1], or an explicitly approved LAN endpoint. Hosted routers are not allowed "
            "under the local backend."
        )


def _host_is_loopback_or_lan(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_loopback or ip.is_private or ip.is_link_local
    except ValueError:
        pass
    try:
        resolved = socket.gethostbyname(host)
    except OSError:
        return False
    ip = ipaddress.ip_address(resolved)
    return ip.is_loopback or ip.is_private or ip.is_link_local


class OpenAICompatibleHttpClient(Protocol):
    def get_json(self, url: str, headers: dict[str, str], timeout: float) -> dict[str, Any]:
        ...

    def post_json(
        self,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout: float,
    ) -> dict[str, Any]:
        ...

    def stream_sse_json(
        self,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout: float,
        abort_checker: Any | None = None,
    ) -> Iterator[dict[str, Any]]:
        ...


class UrllibOpenAICompatibleHttpClient:
    def get_json(self, url: str, headers: dict[str, str], timeout: float) -> dict[str, Any]:
        request = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def post_json(
        self,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout: float,
    ) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def stream_sse_json(
        self,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout: float,
        abort_checker: Any | None = None,
    ) -> Iterator[dict[str, Any]]:
        _raise_if_aborted(abort_checker)
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(url, data=body, headers={**headers, "Accept": "text/event-stream"}, method="POST")
        with urllib.request.urlopen(request, timeout=timeout) as response:
            for raw_line in response:
                _raise_if_aborted(abort_checker)
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line or line.startswith(":") or not line.startswith("data:"):
                    continue
                data = line.removeprefix("data:").strip()
                if data == "[DONE]":
                    break
                yield json.loads(data)
                _raise_if_aborted(abort_checker)


@dataclass
class LocalOpenAICompatibleBackend:
    config: BackendConfig
    http_client: OpenAICompatibleHttpClient | None = None

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def base_url(self) -> str:
        return str(self.config.settings["base_url"]).rstrip("/")

    @property
    def timeout_seconds(self) -> float:
        return float(self.config.settings.get("timeout_seconds", 30))

    @property
    def abort_checker(self):
        checker = self.config.settings.get("abort_checker")
        return checker if callable(checker) else None

    def validate_config(self) -> None:
        data_boundary = self.config.metadata.data_boundary.value
        billing_mode = self.config.metadata.billing_mode.value
        if data_boundary == "local_only":
            approved = list(self.config.settings.get("approved_lan_endpoints", []))
            validate_local_base_url(self.base_url, approved)
            if billing_mode != "local_no_api_cost":
                raise BackendConfigError("Local backend must remain billing_mode: local_no_api_cost.")
            return
        if data_boundary == "hosted_provider":
            if billing_mode != "paid_api":
                raise BackendConfigError("Hosted OpenAI-compatible backend must remain billing_mode: paid_api.")
            return
        raise BackendConfigError("OpenAI-compatible backend must use data_boundary: local_only or hosted_provider.")

    def preflight(self) -> BackendStatus:
        try:
            self.validate_config()
        except BackendConfigError as exc:
            return BackendStatus(
                available=False,
                reason=str(exc),
                metadata=self.config.metadata,
                capabilities=self.config.capabilities,
            )
        try:
            self._client().get_json(
                self._url("/models"),
                headers=self._headers(),
                timeout=min(self.timeout_seconds, 10),
            )
        except (OSError, TimeoutError, urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
            return BackendStatus(
                available=False,
                reason=(
                    "Local OpenAI-compatible endpoint is unavailable. Start a local server "
                    "such as Ollama (`ollama serve`), LM Studio Developer server, or vLLM, "
                    f"then retry. Details: {exc}"
                ),
                metadata=self.config.metadata,
                capabilities=self.config.capabilities,
            )
        return BackendStatus(
            available=True,
            reason=None,
            metadata=self.config.metadata,
            capabilities=self.config.capabilities,
        )

    def complete(self, messages: list[dict[str, str]]) -> str:
        self.validate_config()
        payload = self._chat_payload(messages)
        try:
            response = self._client().post_json(
                self._url("/chat/completions"),
                headers=self._headers(),
                payload=payload,
                timeout=self.timeout_seconds,
            )
        except (
            OSError,
            TimeoutError,
            urllib.error.URLError,
            urllib.error.HTTPError,
            json.JSONDecodeError,
            KeyError,
            IndexError,
            TypeError,
        ) as exc:
            raise LocalEndpointUnavailable(
                "Local OpenAI-compatible endpoint is unavailable or returned an invalid response. Start Ollama, LM Studio, "
                f"or vLLM and retry. Details: {exc}"
            ) from exc
        return str(response["choices"][0]["message"]["content"])

    def stream_complete_backend_events(self, messages: list[dict[str, str]]) -> Iterator[BackendStreamEvent]:
        try:
            self.validate_config()
        except ProviderStreamAbortError:
            raise
        except Exception as exc:
            yield BackendStreamEvent(type="error", text=str(exc), payload={"error_type": type(exc).__name__})
            return
        if self.config.settings.get("stream") is False:
            yield from self._non_streaming_backend_events(messages)
            return
        yield BackendStreamEvent(type="status", text="Backend started.", payload={"streaming": True})
        payload = self._chat_payload(messages, stream=True)
        try:
            _raise_if_aborted(self.abort_checker)
            for chunk in _stream_sse_json(
                self._client(),
                self._url("/chat/completions"),
                headers=self._headers(),
                payload=payload,
                timeout=self.timeout_seconds,
                abort_checker=self.abort_checker,
            ):
                _raise_if_aborted(self.abort_checker)
                yield from _backend_events_from_openai_chat_chunk(chunk)
                _raise_if_aborted(self.abort_checker)
        except ProviderStreamAbortError:
            raise
        except Exception as exc:
            yield BackendStreamEvent(type="error", text=str(exc), payload={"error_type": type(exc).__name__})
            return
        yield BackendStreamEvent(type="status", text="Backend completed.", payload={"status": "completed"})

    def _non_streaming_backend_events(self, messages: list[dict[str, str]]) -> Iterator[BackendStreamEvent]:
        yield BackendStreamEvent(type="status", text="Backend started.", payload={"streaming": False})
        try:
            content = self.complete(messages)
        except Exception as exc:
            yield BackendStreamEvent(type="error", text=str(exc), payload={"error_type": type(exc).__name__})
            return
        yield BackendStreamEvent(type="message_delta", text=content)
        yield BackendStreamEvent(type="status", text="Backend completed.", payload={"status": "completed"})

    def _chat_payload(self, messages: list[dict[str, str]], *, stream: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.config.settings["model"],
            "messages": messages,
            "temperature": self.config.settings.get("temperature", 0.2),
        }
        if self.config.settings.get("max_completion_tokens") is not None:
            payload["max_completion_tokens"] = self.config.settings["max_completion_tokens"]
        else:
            payload["max_tokens"] = self.config.settings.get("max_tokens", 4096)
        for key in ("reasoning", "reasoning_effort"):
            if self.config.settings.get(key) is not None:
                payload[key] = self.config.settings[key]
        if stream:
            payload["stream"] = True
        return payload

    def _client(self) -> OpenAICompatibleHttpClient:
        return self.http_client or UrllibOpenAICompatibleHttpClient()

    def _headers(self) -> dict[str, str]:
        api_key = str(self.config.settings.get("api_key", "local"))
        configured_headers = self.config.settings.get("headers")
        headers = {str(key): str(value) for key, value in configured_headers.items()} if isinstance(configured_headers, dict) else {}
        return {**headers, "Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}

    def _url(self, path: str) -> str:
        return self.base_url + path


def _raise_if_aborted(abort_checker: Any | None) -> None:
    if callable(abort_checker) and abort_checker():
        raise ProviderStreamAbortError("Provider stream aborted.")


def _stream_sse_json(
    client: OpenAICompatibleHttpClient,
    url: str,
    *,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout: float,
    abort_checker: Any | None = None,
) -> Iterator[dict[str, Any]]:
    _raise_if_aborted(abort_checker)
    method = client.stream_sse_json
    try:
        supports_abort = "abort_checker" in inspect.signature(method).parameters
    except (TypeError, ValueError):
        supports_abort = False
    if supports_abort:
        yield from method(url, headers=headers, payload=payload, timeout=timeout, abort_checker=abort_checker)
        return
    for chunk in method(url, headers=headers, payload=payload, timeout=timeout):
        _raise_if_aborted(abort_checker)
        yield chunk
        _raise_if_aborted(abort_checker)

def _backend_events_from_openai_chat_chunk(chunk: dict[str, Any]) -> Iterator[BackendStreamEvent]:
    if isinstance(chunk.get("error"), dict):
        error = chunk["error"]
        yield BackendStreamEvent(type="error", text=str(error.get("message") or "Provider stream error."), payload={"error": error, "error_type": str(error.get("type") or "OpenAIStreamError")})
        return
    usage = chunk.get("usage")
    if isinstance(usage, dict):
        yield BackendStreamEvent(type="token_usage", payload=usage)
    choices = chunk.get("choices")
    if not isinstance(choices, list):
        return
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
        content = delta.get("content")
        if isinstance(content, str) and content:
            yield BackendStreamEvent(type="message_delta", text=content, payload={"delta": content})
        for tool_call in _tool_call_deltas(delta):
            yield BackendStreamEvent(type="tool_call_delta", text=tool_call.get("name"), payload=tool_call)
        finish_reason = choice.get("finish_reason")
        if finish_reason:
            yield BackendStreamEvent(type="finish_reason", text=str(finish_reason), payload={"finish_reason": str(finish_reason)})


def _tool_call_deltas(delta: dict[str, Any]) -> Iterator[dict[str, Any]]:
    function_call = delta.get("function_call") if isinstance(delta.get("function_call"), dict) else None
    if function_call is not None:
        yield {
            "tool_call_id": delta.get("id"),
            "index": 0,
            "type": "function",
            "tool_name": function_call.get("name"),
            "arguments_delta": function_call.get("arguments"),
        }
    tool_calls = delta.get("tool_calls")
    if not isinstance(tool_calls, list):
        return
    for item in tool_calls:
        if not isinstance(item, dict):
            continue
        function = item.get("function") if isinstance(item.get("function"), dict) else {}
        yield {
            "tool_call_id": item.get("id"),
            "index": item.get("index"),
            "type": item.get("type"),
            "tool_name": function.get("name"),
            "arguments_delta": function.get("arguments"),
        }
