import os

import pytest

from harness.backends.local_openai import (
    BackendConfigError,
    LocalOpenAICompatibleBackend,
    validate_local_base_url,
)
from harness.config import default_config


class FakeHttpClient:
    def __init__(self, post_responses=None, get_error: Exception | None = None):
        self.post_responses = list(post_responses or [])
        self.get_error = get_error
        self.get_calls = []
        self.post_calls = []

    def get_json(self, url, headers, timeout):
        self.get_calls.append((url, headers, timeout))
        if self.get_error:
            raise self.get_error
        return {"data": [{"id": "local-model"}]}

    def post_json(self, url, headers, payload, timeout):
        self.post_calls.append((url, headers, payload, timeout))
        content = self.post_responses.pop(0)
        return {"choices": [{"message": {"content": content}}]}


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
