from __future__ import annotations

import json
import ipaddress
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Iterator, Protocol

from harness.backends.streaming import BackendStreamEvent
from harness.models import BackendConfig, BackendStatus


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

    def validate_config(self) -> None:
        approved = list(self.config.settings.get("approved_lan_endpoints", []))
        validate_local_base_url(self.base_url, approved)
        if self.config.metadata.data_boundary.value != "local_only":
            raise BackendConfigError("Local backend must remain data_boundary: local_only.")
        if self.config.metadata.billing_mode.value != "local_no_api_cost":
            raise BackendConfigError("Local backend must remain billing_mode: local_no_api_cost.")

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
        payload = {
            "model": self.config.settings["model"],
            "messages": messages,
            "temperature": self.config.settings.get("temperature", 0.2),
            "max_tokens": self.config.settings.get("max_tokens", 4096),
        }
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
        yield BackendStreamEvent(type="status", text="Backend started.", payload={"streaming": False})
        try:
            content = self.complete(messages)
        except Exception as exc:
            yield BackendStreamEvent(type="error", text=str(exc), payload={"error_type": type(exc).__name__})
            return
        yield BackendStreamEvent(type="message_delta", text=content)
        yield BackendStreamEvent(type="status", text="Backend completed.", payload={"status": "completed"})

    def _client(self) -> OpenAICompatibleHttpClient:
        return self.http_client or UrllibOpenAICompatibleHttpClient()

    def _headers(self) -> dict[str, str]:
        api_key = str(self.config.settings.get("api_key", "local"))
        return {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}

    def _url(self, path: str) -> str:
        return self.base_url + path
