import os

import pytest

from harness.backends.local_openai import (
    BackendConfigError,
    LocalOpenAICompatibleBackend,
    _stream_sse_json,
    validate_local_base_url,
)
from harness.config import default_config
from harness.provider_events import ProviderStreamAbortError


class FakeHttpClient:
    def __init__(self, post_responses=None, get_error: Exception | None = None, stream_chunks=None):
        self.post_responses = list(post_responses or [])
        self.get_error = get_error
        self.stream_chunks = list(stream_chunks or [])
        self.get_calls = []
        self.post_calls = []
        self.stream_calls = []

    def get_json(self, url, headers, timeout):
        self.get_calls.append((url, headers, timeout))
        if self.get_error:
            raise self.get_error
        return {"data": [{"id": "local-model"}]}

    def post_json(self, url, headers, payload, timeout):
        self.post_calls.append((url, headers, payload, timeout))
        content = self.post_responses.pop(0)
        return {"choices": [{"message": {"content": content}}]}

    def stream_sse_json(self, url, headers, payload, timeout):
        self.stream_calls.append((url, headers, payload, timeout))
        yield from self.stream_chunks


def local_backend_config():
    return default_config().backends["local_openai_compatible"]


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:11434/v1",
        "http://127.0.0.1:8000/v1",
        "http://[::1]:1234/v1",
    ],
)
def test_local_only_url_validation_accepts_loopback(url) -> None:
    validate_local_base_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://api.openai.com/v1",
        "https://litellm.example.com/v1",
    ],
)
def test_local_only_url_validation_rejects_hosted_urls(url) -> None:
    with pytest.raises(BackendConfigError):
        validate_local_base_url(url)


def test_local_only_url_validation_allows_explicit_approved_lan_endpoint() -> None:
    validate_local_base_url(
        "http://192.168.1.20:8000/v1",
        approved_lan_endpoints=["http://192.168.1.20:8000/v1"],
    )


def test_local_only_url_validation_rejects_approved_hosted_endpoint() -> None:
    with pytest.raises(BackendConfigError):
        validate_local_base_url(
            "https://api.openai.com/v1",
            approved_lan_endpoints=["https://api.openai.com/v1"],
        )


def test_local_backend_preflight_available() -> None:
    backend = LocalOpenAICompatibleBackend(local_backend_config(), FakeHttpClient())
    status = backend.preflight()
    assert status.available
    assert status.metadata.data_boundary.value == "local_only"


def test_local_backend_preflight_unavailable() -> None:
    backend = LocalOpenAICompatibleBackend(
        local_backend_config(),
        FakeHttpClient(get_error=OSError("connection refused")),
    )
    status = backend.preflight()
    assert not status.available
    assert "Local OpenAI-compatible endpoint is unavailable" in status.reason


def test_local_backend_does_not_require_openai_api_key(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    backend = LocalOpenAICompatibleBackend(
        local_backend_config(),
        FakeHttpClient(post_responses=['{"command":"final_answer","arguments":{"answer":"ok"}}']),
    )
    assert backend.complete([{"role": "user", "content": "hi"}])
    assert "OPENAI_API_KEY" not in os.environ


def test_local_backend_rejects_non_local_config() -> None:
    cfg = local_backend_config().model_copy(deep=True)
    cfg.settings["base_url"] = "https://api.openai.com/v1"
    backend = LocalOpenAICompatibleBackend(cfg, FakeHttpClient())
    status = backend.preflight()
    assert not status.available
    assert "not local" in status.reason


def test_local_backend_streams_openai_compatible_sse_chunks() -> None:
    client = FakeHttpClient(
        stream_chunks=[
            {"choices": [{"delta": {"content": "Hel"}, "finish_reason": None}]},
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {"name": "read", "arguments": '{"path"'},
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            },
            {"choices": [{"delta": {"content": "lo"}, "finish_reason": "stop"}], "usage": {"total_tokens": 3}},
        ]
    )
    backend = LocalOpenAICompatibleBackend(local_backend_config(), client)

    events = list(backend.stream_complete_backend_events([{"role": "user", "content": "hi"}]))

    assert [event.type for event in events] == [
        "status",
        "message_delta",
        "tool_call_delta",
        "token_usage",
        "message_delta",
        "finish_reason",
        "status",
    ]
    assert events[1].text == "Hel"
    assert events[2].payload["tool_call_id"] == "call_1"
    assert events[2].payload["tool_name"] == "read"
    assert events[3].payload["total_tokens"] == 3
    assert events[4].text == "lo"
    assert events[5].payload["finish_reason"] == "stop"
    assert client.stream_calls[0][2]["stream"] is True


def test_local_openai_stream_helper_aborts_legacy_client_between_chunks() -> None:
    client = FakeHttpClient(stream_chunks=[{"first": True}, {"second": True}])
    checks = {"count": 0}

    def abort_checker() -> bool:
        checks["count"] += 1
        return checks["count"] >= 3

    stream = _stream_sse_json(
        client,
        "http://localhost:11434/v1/chat/completions",
        headers={},
        payload={},
        timeout=1,
        abort_checker=abort_checker,
    )

    assert next(stream) == {"first": True}
    with pytest.raises(ProviderStreamAbortError):
        next(stream)
    assert client.stream_calls


def test_local_backend_non_streaming_fallback_is_explicit() -> None:
    cfg = local_backend_config().model_copy(deep=True)
    cfg.settings["stream"] = False
    client = FakeHttpClient(post_responses=["fallback answer"])
    backend = LocalOpenAICompatibleBackend(cfg, client)

    events = list(backend.stream_complete_backend_events([{"role": "user", "content": "hi"}]))

    assert [event.type for event in events] == ["status", "message_delta", "status"]
    assert events[0].payload["streaming"] is False
    assert events[1].text == "fallback answer"
    assert client.stream_calls == []
    assert client.post_calls[0][2].get("stream") is None
