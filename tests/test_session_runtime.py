from __future__ import annotations

import time
from decimal import Decimal

import pytest
import yaml

from harness.approvals import ApprovalStore
from harness.config import write_default_config
from harness.local_server import _reply_to_session_permission, _route_get, _route_post
from harness.memory.sqlite_store import SQLiteStore
from harness.models import SessionMessageRole, SessionPartKind
from harness.protocol_adapters import OpenAIChatProtocolAdapter, OpenAIResponsesProtocolAdapter, ProtocolAdapterRegistry
from harness.provider_events import (
    ProviderCapabilities,
    ProviderError,
    ProviderErrorCategory,
    ProviderEventKind,
    ProviderMessage,
    ProviderRequest,
    provider_error_event,
    provider_event,
)
from harness.session_runtime import (
    SessionPromptQueuePolicy,
    SessionPromptExecution,
    SessionPromptRequest,
    SessionRuntimeBusyError,
    SessionRuntimeManager,
    SessionRuntimePhase,
)


class StaticTextProvider:
    def __init__(self, response: str) -> None:
        self.response = response
        self.executions: list[SessionPromptExecution] = []

    def complete(self, execution: SessionPromptExecution) -> str:
        self.executions.append(execution)
        return self.response


class StaticProviderAdapter:
    provider_id = "static_provider"
    model_ref = "static/model"
    capabilities = ProviderCapabilities(supports_streaming=True)

    def __init__(self) -> None:
        self.requests: list[ProviderRequest] = []

    def stream(self, request: ProviderRequest):
        self.requests.append(request)
        yield provider_event(ProviderEventKind.MODEL_STARTED, sequence=1, request=request, provider_id=self.provider_id, model_ref=self.model_ref)
        yield provider_event(
            ProviderEventKind.MODEL_MESSAGE_DELTA,
            sequence=2,
            request=request,
            provider_id=self.provider_id,
            model_ref=self.model_ref,
            text="Adapter response.",
            payload={"delta": "Adapter response."},
        )
        yield provider_event(ProviderEventKind.MODEL_COMPLETED, sequence=3, request=request, provider_id=self.provider_id, model_ref=self.model_ref)


class AbortAwareProviderAdapter:
    provider_id = "abort_provider"
    model_ref = "abort/model"
    capabilities = ProviderCapabilities(supports_streaming=True, supports_abort=True)

    def __init__(self) -> None:
        self.requests: list[ProviderRequest] = []

    def stream(self, request: ProviderRequest):
        self.requests.append(request)
        yield provider_event(ProviderEventKind.MODEL_STARTED, sequence=1, request=request, provider_id=self.provider_id, model_ref=self.model_ref)
        deadline = time.monotonic() + 2.0
        abort_checker = request.context.get("abort_checker")
        while time.monotonic() < deadline:
            if callable(abort_checker) and abort_checker():
                error = ProviderError(
                    category=ProviderErrorCategory.ABORTED,
                    error_type="ProviderStreamAbortError",
                    message="Provider stream aborted.",
                    retryable=False,
                )
                yield provider_event(
                    ProviderEventKind.MODEL_ABORTED,
                    sequence=2,
                    request=request,
                    provider_id=self.provider_id,
                    model_ref=self.model_ref,
                    payload={"aborted": True, "error": error.model_dump(mode="json")},
                )
                return
            time.sleep(0.01)
        yield provider_event(
            ProviderEventKind.MODEL_MESSAGE_DELTA,
            sequence=2,
            request=request,
            provider_id=self.provider_id,
            model_ref=self.model_ref,
            text="Not aborted.",
            payload={"delta": "Not aborted."},
        )
        yield provider_event(ProviderEventKind.MODEL_COMPLETED, sequence=3, request=request, provider_id=self.provider_id, model_ref=self.model_ref)


class ToolCallingProviderAdapter:
    provider_id = "tool_provider"
    model_ref = "tool/model"
    capabilities = ProviderCapabilities(supports_streaming=True, supports_native_tools=True)

    def __init__(self, *, tool_name: str, arguments: dict) -> None:
        self.tool_name = tool_name
        self.arguments = arguments

    def stream(self, request: ProviderRequest):
        yield provider_event(ProviderEventKind.MODEL_STARTED, sequence=1, request=request, provider_id=self.provider_id, model_ref=self.model_ref)
        yield provider_event(
            ProviderEventKind.TOOL_CALL_STARTED,
            sequence=2,
            request=request,
            provider_id=self.provider_id,
            model_ref=self.model_ref,
            tool_call_id="call_provider_tool",
            tool_name=self.tool_name,
            payload={"tool_call_id": "call_provider_tool", "tool_name": self.tool_name, "arguments": self.arguments},
        )
        yield provider_event(
            ProviderEventKind.TOOL_CALL_COMPLETED,
            sequence=3,
            request=request,
            provider_id=self.provider_id,
            model_ref=self.model_ref,
            tool_call_id="call_provider_tool",
            tool_name=self.tool_name,
            payload={"tool_call_id": "call_provider_tool", "tool_name": self.tool_name, "arguments": self.arguments},
        )
        yield provider_event(ProviderEventKind.MODEL_COMPLETED, sequence=4, request=request, provider_id=self.provider_id, model_ref=self.model_ref)


class FailsThenSucceedsProviderAdapter:
    provider_id = "retry_provider"
    model_ref = "retry/model"
    capabilities = ProviderCapabilities(supports_streaming=True)

    def __init__(self, error: ProviderError, *, response: str = "Recovered response.") -> None:
        self.error = error
        self.response = response
        self.requests: list[ProviderRequest] = []

    def stream(self, request: ProviderRequest):
        self.requests.append(request)
        yield provider_event(ProviderEventKind.MODEL_STARTED, sequence=1, request=request, provider_id=self.provider_id, model_ref=self.model_ref)
        if len(self.requests) == 1:
            yield provider_error_event(self.error, sequence=2, request=request, provider_id=self.provider_id, model_ref=self.model_ref)
            return
        yield provider_event(
            ProviderEventKind.MODEL_MESSAGE_DELTA,
            sequence=2,
            request=request,
            provider_id=self.provider_id,
            model_ref=self.model_ref,
            text=self.response,
            payload={"delta": self.response},
        )
        yield provider_event(ProviderEventKind.MODEL_COMPLETED, sequence=3, request=request, provider_id=self.provider_id, model_ref=self.model_ref)


class AlwaysFailsProviderAdapter:
    provider_id = "failing_provider"
    model_ref = "failing/model"
    capabilities = ProviderCapabilities(supports_streaming=True)

    def __init__(self, error: ProviderError) -> None:
        self.error = error
        self.requests: list[ProviderRequest] = []

    def stream(self, request: ProviderRequest):
        self.requests.append(request)
        yield provider_event(ProviderEventKind.MODEL_STARTED, sequence=1, request=request, provider_id=self.provider_id, model_ref=self.model_ref)
        yield provider_error_event(self.error, sequence=2, request=request, provider_id=self.provider_id, model_ref=self.model_ref)


class PartialThenFailsProviderAdapter:
    provider_id = "partial_provider"
    model_ref = "partial/model"
    capabilities = ProviderCapabilities(supports_streaming=True)

    def __init__(self, error: ProviderError, *, partial_text: str) -> None:
        self.error = error
        self.partial_text = partial_text
        self.requests: list[ProviderRequest] = []

    def stream(self, request: ProviderRequest):
        self.requests.append(request)
        yield provider_event(ProviderEventKind.MODEL_STARTED, sequence=1, request=request, provider_id=self.provider_id, model_ref=self.model_ref)
        yield provider_event(
            ProviderEventKind.MODEL_MESSAGE_DELTA,
            sequence=2,
            request=request,
            provider_id=self.provider_id,
            model_ref=self.model_ref,
            text=self.partial_text,
            payload={"delta": self.partial_text},
        )
        yield provider_error_event(self.error, sequence=3, request=request, provider_id=self.provider_id, model_ref=self.model_ref)


class RuntimeProtocolAdapter:
    protocol = "codex_cli"

    def __init__(self) -> None:
        self.calls = []

    def stream(self, provider, model, request):
        self.calls.append((provider, model, request))
        yield provider_event(ProviderEventKind.MODEL_STARTED, sequence=1, request=request)
        yield provider_event(
            ProviderEventKind.MODEL_MESSAGE_DELTA,
            sequence=2,
            request=request,
            text="Descriptor response.",
            payload={"delta": "Descriptor response."},
        )
        yield provider_event(ProviderEventKind.MODEL_COMPLETED, sequence=3, request=request)


class TrackingProtocolAdapterRegistry(ProtocolAdapterRegistry):
    def __init__(self) -> None:
        super().__init__()
        self.has_calls: list[str] = []
        self.get_calls: list[str] = []

    def has(self, protocol: str) -> bool:
        self.has_calls.append(protocol)
        return super().has(protocol)

    def get(self, protocol: str):
        self.get_calls.append(protocol)
        return super().get(protocol)


class RuntimeRetryProtocolAdapter:
    protocol = "codex_cli"

    def __init__(self, error: ProviderError) -> None:
        self.error = error
        self.calls = []

    def stream(self, provider, model, request):
        self.calls.append((provider, model, request))
        yield provider_event(ProviderEventKind.MODEL_STARTED, sequence=1, request=request)
        if len(self.calls) == 1:
            yield provider_error_event(self.error, sequence=2, request=request)
            return
        yield provider_event(
            ProviderEventKind.MODEL_MESSAGE_DELTA,
            sequence=2,
            request=request,
            text="Retried descriptor response.",
            payload={"delta": "Retried descriptor response."},
        )
        yield provider_event(ProviderEventKind.MODEL_COMPLETED, sequence=3, request=request)


class RuntimeFakeResponsesHttpClient:
    def __init__(self) -> None:
        self.streams = []

    def stream_sse_json(self, url: str, headers: dict, payload: dict, timeout: float):
        self.streams.append({"url": url, "headers": headers, "payload": payload, "timeout": timeout})
        yield {"type": "response.created", "response": {"id": "resp_runtime"}}
        yield {"type": "response.reasoning_summary_text.delta", "delta": "Checked routing."}
        yield {
            "type": "response.function_call_arguments.delta",
            "item_id": "call_runtime",
            "delta": '{"path":"README.md"}',
        }
        yield {"type": "response.output_text.delta", "delta": "Runtime Responses answer."}
        yield {
            "type": "response.completed",
            "response": {
                "id": "resp_runtime",
                "status": "completed",
                "usage": {
                    "input_tokens": 3,
                    "output_tokens": 4,
                    "total_tokens": 7,
                    "input_tokens_details": {"cached_tokens": 2},
                    "cache_creation_input_tokens": 1,
                },
            },
        }


def test_session_runtime_executes_prompt_with_text_provider(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    session = store.create_session(title="Runtime queue")
    provider = StaticTextProvider("Repository inspected.")
    manager = SessionRuntimeManager.for_store(store, text_provider=provider)

    accepted = manager.submit_prompt(
        SessionPromptRequest(
            session_id=session.id,
            content="Inspect the repository first.",
            queue_policy=SessionPromptQueuePolicy.FOLLOW_UP,
        )
    )

    assert accepted.ok is True
    assert accepted.accepted is True
    assert accepted.queued is False
    assert accepted.execution_started is True
    assert accepted.worker_started is True
    assert accepted.runtime.phase == SessionRuntimePhase.RUNNING

    final = manager.wait(session.id, timeout=1.0)

    assert final.phase == SessionRuntimePhase.IDLE
    assert final.queued_prompt_count == 0
    assert provider.executions[0].content == "Inspect the repository first."
    messages = store.list_session_messages(session.id)
    assert [message.role.value for message in messages] == ["assistant"]
    assert messages[0].content_preview == "Repository inspected."

    events = store.list_session_store_events(session.id)
    kinds = [event.kind for event in events]
    assert "harness.runtime.prompt_queued" in kinds
    assert "harness.turn.started" in kinds
    assert "model.started" in kinds
    assert "model.message_delta" in kinds
    assert "model.completed" in kinds
    assert kinds[-1] == "harness.turn.finished"


def test_session_runtime_resolves_model_descriptor_and_uses_protocol_adapter(tmp_path) -> None:
    write_default_config(tmp_path)
    store = SQLiteStore.open_initialized(tmp_path)
    ApprovalStore(tmp_path).add("codex_cli", "hosted_provider", ["session_provider_execution"], 1)
    session = store.create_session(title="Runtime descriptor", raw_model_ref="codex_cli/gpt-5.5")
    adapter = RuntimeProtocolAdapter()
    registry = ProtocolAdapterRegistry()
    registry.register(adapter)
    manager = SessionRuntimeManager.for_store(store, protocol_adapter_registry=registry)

    accepted = manager.submit_prompt(
        SessionPromptRequest(
            session_id=session.id,
            content="Use descriptor routing.",
            model_ref="codex_cli/gpt-5.5",
            queue_policy=SessionPromptQueuePolicy.FOLLOW_UP,
        )
    )
    final = manager.wait(session.id, timeout=1.0)

    assert accepted.execution_started is True
    assert final.phase == SessionRuntimePhase.IDLE
    provider, model, request = adapter.calls[0]
    assert provider.provider_id == "codex_cli"
    assert model.raw_model_ref == "codex_cli/gpt-5.5"
    assert model.protocol == "codex_cli"
    assert request.model_ref == "codex_cli/gpt-5.5"
    assert request.metadata["canonical_model_ref"] == "codex_cli/gpt-5.5"
    assert request.metadata["protocol"] == "codex_cli"
    assert request.metadata["resolved_provider_options"]["command"] == "codex"
    assert request.metadata["resolved_model_options"]["model_reasoning_effort"] == "low"
    assert request.metadata["requested_reasoning_effort"] == "low"
    assert request.metadata["resolved_reasoning_effort"] == "low"
    assert request.metadata["reasoning_resolution"] == "exact"

    messages = store.list_session_messages(session.id)
    assert messages[-1].role.value == "assistant"
    assert messages[-1].content_preview == "Descriptor response."
    events = store.list_session_store_events(session.id)
    validation = [event for event in events if event.kind == "session.model_validation"][-1]
    assert validation.payload["source"] == "session_runtime"
    assert validation.payload["canonical_model_ref"] == "codex_cli/gpt-5.5"
    assert validation.payload["protocol"] == "codex_cli"
    assert validation.payload["provider_execution_started"] is False
    delta = [event for event in events if event.kind == "model.message_delta"][-1]
    assert delta.payload["delta"] == "Descriptor response."


def test_session_runtime_uses_session_selected_model_with_resolution_event(tmp_path) -> None:
    write_default_config(tmp_path)
    store = SQLiteStore.open_initialized(tmp_path)
    ApprovalStore(tmp_path).add("codex_cli", "hosted_provider", ["session_provider_execution"], 1)
    session = store.create_session(title="Runtime selected model", raw_model_ref="codex_cli/gpt-5.5")
    adapter = RuntimeProtocolAdapter()
    registry = ProtocolAdapterRegistry()
    registry.register(adapter)
    manager = SessionRuntimeManager.for_store(store, protocol_adapter_registry=registry)

    accepted = manager.submit_prompt(
        SessionPromptRequest(
            session_id=session.id,
            content="Use the selected session model.",
            queue_policy=SessionPromptQueuePolicy.FOLLOW_UP,
        )
    )
    final = manager.wait(session.id, timeout=1.0)

    assert accepted.execution_started is True
    assert final.phase == SessionRuntimePhase.IDLE
    provider, model, request = adapter.calls[0]
    assert provider.provider_id == "codex_cli"
    assert model.raw_model_ref == "codex_cli/gpt-5.5"
    assert request.model_ref == "codex_cli/gpt-5.5"
    assert request.metadata["model_selection_source"] == "session_override"
    events = store.list_session_store_events(session.id)
    resolution = [event for event in events if event.kind == "session.model_resolution"][-1]
    validation = [event for event in events if event.kind == "session.model_validation"][-1]
    assert resolution.payload["source"] == "session_override"
    assert resolution.payload["raw_model_ref"] == "codex_cli/gpt-5.5"
    assert resolution.payload["canonical_model_ref"] == "codex_cli/gpt-5.5"
    assert resolution.payload["blocked_reasons"] == []
    assert validation.payload["model_selection_source"] == "session_override"
    assert [event.kind for event in events].index("session.model_resolution") < [event.kind for event in events].index("session.model_validation")


def test_workspace_default_model_resolution_is_audited(tmp_path) -> None:
    write_default_config(tmp_path)
    config_path = tmp_path / ".harness" / "config.yaml"
    config_data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config_data.setdefault("chat", {})["default_model_profile"] = "codex_cli/gpt-5.5"
    config_path.write_text(yaml.safe_dump(config_data, sort_keys=False), encoding="utf-8")
    store = SQLiteStore.open_initialized(tmp_path)
    ApprovalStore(tmp_path).add("codex_cli", "hosted_provider", ["session_provider_execution"], 1)
    session = store.create_session(title="Runtime workspace default")
    adapter = RuntimeProtocolAdapter()
    registry = ProtocolAdapterRegistry()
    registry.register(adapter)
    manager = SessionRuntimeManager.for_store(store, protocol_adapter_registry=registry)

    manager.submit_prompt(SessionPromptRequest(session_id=session.id, content="Use workspace default."))
    final = manager.wait(session.id, timeout=1.0)

    assert final.phase == SessionRuntimePhase.IDLE
    _, model, request = adapter.calls[0]
    assert model.raw_model_ref == "codex_cli/gpt-5.5"
    assert request.metadata["model_selection_source"] == "workspace_default"
    resolution = [event for event in store.list_session_store_events(session.id) if event.kind == "session.model_resolution"][-1]
    assert resolution.payload["source"] == "workspace_default"
    assert resolution.payload["raw_model_ref"] == "codex_cli/gpt-5.5"
    assert resolution.payload["hidden_provider_fallback"] is False
    assert resolution.payload["no_hidden_fallback"] is True


def test_workspace_default_model_resolution_is_visible_on_model_started_event(tmp_path) -> None:
    write_default_config(tmp_path)
    config_path = tmp_path / ".harness" / "config.yaml"
    config_data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config_data.setdefault("chat", {})["default_model_profile"] = "responses_local/resp-model"
    config_path.write_text(yaml.safe_dump(config_data, sort_keys=False), encoding="utf-8")
    (tmp_path / ".harness" / "models.yaml").write_text(
        """
providers:
  responses_local:
    display_name: Responses Local
    enabled: true
    data_boundary: local_only
    base_url: http://localhost:11434/v1
    protocol: openai_responses
    credential:
      kind: static_local
    models:
      resp-model:
        display_name: Responses Model
        api_id: resp-model
        context_window: 8192
        max_output_tokens: 1024
        input_modalities: [text]
        output_modalities: [text]
        status: active
""".lstrip(),
        encoding="utf-8",
    )
    store = SQLiteStore.open_initialized(tmp_path)
    session = store.create_session(title="Runtime visible workspace default")
    client = RuntimeFakeResponsesHttpClient()
    registry = ProtocolAdapterRegistry()
    registry.register(OpenAIResponsesProtocolAdapter(http_client=client))
    manager = SessionRuntimeManager.for_store(store, protocol_adapter_registry=registry)

    manager.submit_prompt(SessionPromptRequest(session_id=session.id, content="Use visible workspace default."))
    final = manager.wait(session.id, timeout=1.0)

    assert final.phase == SessionRuntimePhase.IDLE
    events = store.list_session_store_events(session.id)
    resolution = [event for event in events if event.kind == "session.model_resolution"][-1].payload
    started = [event for event in events if event.kind == "model.started"][-1].payload
    assert resolution["source"] == "workspace_default"
    assert resolution["raw_model_ref"] == "responses_local/resp-model"
    assert resolution["canonical_model_ref"] == "responses_local/resp-model"
    assert started["model_selection_source"] == "workspace_default"
    assert started["model_resolution"]["source"] == "workspace_default"
    assert started["model_resolution"]["raw_model_ref"] == "responses_local/resp-model"
    assert started["model_resolution"]["canonical_model_ref"] == "responses_local/resp-model"
    assert started["model_resolution"]["hidden_provider_fallback"] is False
    assert started["model_resolution"]["hidden_model_fallback"] is False
    assert started["model_resolution"]["no_hidden_fallback"] is True
    assert started["provider_execution_started"] is True
    assert client.streams[0]["url"] == "http://localhost:11434/v1/responses"


def test_missing_default_model_blocks_without_hidden_fallback(tmp_path) -> None:
    write_default_config(tmp_path)
    config_path = tmp_path / ".harness" / "config.yaml"
    config_data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config_data.setdefault("chat", {})["default_model_profile"] = ""
    config_path.write_text(yaml.safe_dump(config_data, sort_keys=False), encoding="utf-8")
    store = SQLiteStore.open_initialized(tmp_path)
    session = store.create_session(title="Runtime missing default")
    adapter = RuntimeProtocolAdapter()
    registry = ProtocolAdapterRegistry()
    registry.register(adapter)
    manager = SessionRuntimeManager.for_store(store, protocol_adapter_registry=registry)

    manager.submit_prompt(SessionPromptRequest(session_id=session.id, content="No default."))
    final = manager.wait(session.id, timeout=1.0)

    assert final.phase == SessionRuntimePhase.FAILED
    assert adapter.calls == []
    events = store.list_session_store_events(session.id)
    resolution = [event for event in events if event.kind == "session.model_resolution"][-1]
    assert resolution.payload["source"] is None
    assert resolution.payload["blocked_reasons"] == ["model_ref_missing"]
    assert resolution.payload["hidden_provider_fallback"] is False
    assert resolution.payload["hidden_model_fallback"] is False
    assert resolution.payload["no_hidden_fallback"] is True
    assert "session.model_validation" not in [event.kind for event in events]
    failed = [event for event in events if event.kind == "model.failed"][-1]
    assert failed.payload["error"]["error_type"] == "ModelResolutionFailed"


def test_default_preference_must_validate_before_execution(tmp_path) -> None:
    write_default_config(tmp_path)
    config_path = tmp_path / ".harness" / "config.yaml"
    config_data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config_data.setdefault("chat", {})["default_model_profile"] = ""
    config_path.write_text(yaml.safe_dump(config_data, sort_keys=False), encoding="utf-8")
    store = SQLiteStore.open_initialized(tmp_path)
    store.set_default_model_preference(
        "paid_openai_compatible/gpt-5.3-codex",
        provider_id="paid_openai_compatible",
        model_id="gpt-5.3-codex",
        source="test_default_preference",
    )
    session = store.create_session(title="Runtime invalid preference")
    adapter = RuntimeProtocolAdapter()
    registry = TrackingProtocolAdapterRegistry()
    registry.register(adapter)
    manager = SessionRuntimeManager.for_store(store, protocol_adapter_registry=registry)

    manager.submit_prompt(SessionPromptRequest(session_id=session.id, content="Validate preference."))
    final = manager.wait(session.id, timeout=1.0)

    assert final.phase == SessionRuntimePhase.FAILED
    assert adapter.calls == []
    assert registry.has_calls == []
    assert registry.get_calls == []
    events = store.list_session_store_events(session.id)
    resolution = [event for event in events if event.kind == "session.model_resolution"][-1]
    validation = [event for event in events if event.kind == "session.model_validation"][-1]
    assert resolution.payload["source"] == "operator_preference"
    assert resolution.payload["raw_model_ref"] == "paid_openai_compatible/gpt-5.3-codex"
    assert resolution.payload["provider_execution_started"] is False
    assert resolution.payload["model_execution_started"] is False
    assert resolution.payload["network_accessed"] is False
    assert validation.payload["model_selection_source"] == "operator_preference"
    assert validation.payload["blocked_reasons"] == ["provider_disabled"]
    assert validation.payload["provider_execution_started"] is False
    assert validation.payload["model_execution_started"] is False
    assert validation.payload["network_accessed"] is False
    failed = [event for event in events if event.kind == "model.failed"][-1]
    assert failed.payload["provider_execution_started"] is False
    assert failed.payload["model_execution_started"] is False
    assert failed.payload["network_accessed"] is False
    assert failed.payload["error"]["error_type"] == "ModelSelectionValidationFailed"


def test_session_runtime_executes_custom_openai_responses_provider(tmp_path) -> None:
    write_default_config(tmp_path)
    custom_path = tmp_path / ".harness" / "models.yaml"
    custom_path.write_text(
        """
providers:
  responses_local:
    display_name: Responses Local
    enabled: true
    data_boundary: local_only
    base_url: http://localhost:11434/v1
    protocol: openai_responses
    credential:
      kind: static_local
    models:
      resp-model:
        display_name: Responses Model
        api_id: resp-model
        context_window: 8192
        max_output_tokens: 1024
        input_modalities: [text]
        output_modalities: [text]
        tool_support: true
        reasoning_support: effort
        cost:
          input_per_1m: 1.0
          output_per_1m: 2.0
        status: active
""".lstrip(),
        encoding="utf-8",
    )
    store = SQLiteStore.open_initialized(tmp_path)
    session = store.create_session(title="Runtime responses", raw_model_ref="responses_local/resp-model")
    client = RuntimeFakeResponsesHttpClient()
    registry = ProtocolAdapterRegistry()
    registry.register(OpenAIResponsesProtocolAdapter(http_client=client))
    manager = SessionRuntimeManager.for_store(store, protocol_adapter_registry=registry)

    accepted = manager.submit_prompt(
        SessionPromptRequest(
            session_id=session.id,
            content="Use the Responses adapter.",
            model_ref="responses_local/resp-model",
            queue_policy=SessionPromptQueuePolicy.FOLLOW_UP,
        )
    )
    final = manager.wait(session.id, timeout=1.0)

    assert accepted.execution_started is True
    assert final.phase == SessionRuntimePhase.IDLE
    assert client.streams == [
        {
            "url": "http://localhost:11434/v1/responses",
            "headers": {"Content-Type": "application/json", "Authorization": "Bearer local"},
            "payload": {
                "model": "resp-model",
                "input": [
                    {
                        "role": "user",
                        "content": [{"type": "input_text", "text": "Use the Responses adapter."}],
                    }
                ],
                "stream": True,
            },
            "timeout": 300.0,
        }
    ]
    messages = store.list_session_messages(session.id)
    assert messages[-1].role.value == "assistant"
    assert messages[-1].content_preview == "Runtime Responses answer."
    events = store.list_session_store_events(session.id)
    validation = [event for event in events if event.kind == "session.model_validation"][-1]
    assert validation.payload["canonical_model_ref"] == "responses_local/resp-model"
    assert validation.payload["protocol"] == "openai_responses"
    assert validation.payload["provider_execution_started"] is False
    reasoning = [event for event in events if event.kind == "reasoning.summary_delta"][-1]
    assert reasoning.payload["delta"] == "Checked routing."
    assert reasoning.payload["response_id"] == "resp_runtime"
    tool_delta = [event for event in events if event.kind == "tool_call.delta"][-1]
    assert tool_delta.payload["tool_call_id"] == "call_runtime"
    assert tool_delta.payload["arguments_delta"] == '{"path":"README.md"}'
    usage = [event for event in events if event.kind == "token_usage.updated"][-1]
    assert usage.payload["input_tokens"] == 3
    assert usage.payload["normalized_usage"] == {
        "input_tokens": 3,
        "output_tokens": 4,
        "reasoning_tokens": None,
        "cache_read_tokens": 2,
        "cache_write_tokens": 1,
        "total_tokens": 7,
    }
    assert usage.payload["estimated_cost"]["total"] == 0.000011
    assert usage.payload["estimated_cost"]["estimated"] is True
    assert usage.payload["estimated_cost"]["source"] == "model_descriptor_pricing"
    assert usage.payload["estimated_cost_usd"] == 0.000011
    updated_session = store.get_session(session.id)
    assert updated_session.token_input == 3
    assert updated_session.token_output == 4
    assert updated_session.token_cache_read == 2
    assert updated_session.token_cache_write == 1
    assert updated_session.estimated_cost_usd == Decimal("0.000011")
    completed = [event for event in events if event.kind == "model.completed" and event.payload.get("response_id")][-1]
    assert completed.payload["response_id"] == "resp_runtime"


def test_image_input_blocks_for_text_only_model_before_network(tmp_path) -> None:
    write_default_config(tmp_path)
    (tmp_path / ".harness" / "models.yaml").write_text(
        """
providers:
  responses_local:
    display_name: Responses Local
    enabled: true
    data_boundary: local_only
    base_url: http://localhost:11434/v1
    protocol: openai_responses
    credential:
      kind: static_local
    models:
      text-only:
        api_id: text-only
        input_modalities: [text]
        output_modalities: [text]
        status: active
""".lstrip(),
        encoding="utf-8",
    )
    store = SQLiteStore.open_initialized(tmp_path)
    session = store.create_session(title="Runtime image block", raw_model_ref="responses_local/text-only")
    message = store.append_session_message(session.id, SessionMessageRole.USER, "Describe this image.")
    image_part = store.append_session_part(
        session.id,
        message.id,
        SessionPartKind.ARTIFACT_REF,
        artifact_id="art_image",
        metadata={"modality": "image", "media_type": "image/png"},
    )
    client = RuntimeFakeResponsesHttpClient()
    registry = ProtocolAdapterRegistry()
    registry.register(OpenAIResponsesProtocolAdapter(http_client=client))
    manager = SessionRuntimeManager.for_store(store, protocol_adapter_registry=registry)

    manager.submit_prompt(
        SessionPromptRequest(
            session_id=session.id,
            content="Describe this image.",
            model_ref="responses_local/text-only",
            message_id=message.id,
            part_id=image_part.id,
        )
    )
    final = manager.wait(session.id, timeout=1.0)

    assert final.phase == SessionRuntimePhase.FAILED
    assert client.streams == []
    events = store.list_session_store_events(session.id)
    assert "model.started" not in [event.kind for event in events]
    validation = [event for event in events if event.kind == "session.model_validation"][-1]
    assert validation.payload["runtime_input_modalities"] == ["image", "text"]
    assert validation.payload["supported_input_modalities"] == ["text"]
    assert validation.payload["unsupported_input_modalities"] == ["image"]
    assert validation.payload["blocked_reasons"] == ["input_modality_unsupported"]
    assert validation.payload["executable"] is False
    failed = [event for event in events if event.kind == "model.failed"][-1]
    assert failed.payload["error"]["category"] == "invalid_request"
    assert failed.payload["error"]["error_type"] == "InputModalityUnsupported"
    assert "image" in failed.payload["error"]["message"]


def test_tool_request_blocks_when_model_tool_support_false(tmp_path) -> None:
    write_default_config(tmp_path)
    (tmp_path / ".harness" / "models.yaml").write_text(
        """
providers:
  responses_local:
    display_name: Responses Local
    enabled: true
    data_boundary: local_only
    base_url: http://localhost:11434/v1
    protocol: openai_responses
    credential:
      kind: static_local
    models:
      text-only:
        api_id: text-only
        input_modalities: [text]
        output_modalities: [text]
        tool_support: false
        status: active
""".lstrip(),
        encoding="utf-8",
    )
    store = SQLiteStore.open_initialized(tmp_path)
    session = store.create_session(title="Runtime tool block", raw_model_ref="responses_local/text-only")
    client = RuntimeFakeResponsesHttpClient()
    registry = ProtocolAdapterRegistry()
    registry.register(OpenAIResponsesProtocolAdapter(http_client=client))
    manager = SessionRuntimeManager.for_store(store, protocol_adapter_registry=registry)

    manager.submit_prompt(
        SessionPromptRequest(
            session_id=session.id,
            content="Read README with a tool.",
            model_ref="responses_local/text-only",
            metadata={"requires_tools": True, "active_tools": ["read"]},
        )
    )
    final = manager.wait(session.id, timeout=1.0)

    assert final.phase == SessionRuntimePhase.FAILED
    assert client.streams == []
    events = store.list_session_store_events(session.id)
    assert "model.started" not in [event.kind for event in events]
    validation = [event for event in events if event.kind == "session.model_validation"][-1]
    assert validation.payload["runtime_tools_requested"] is True
    assert validation.payload["runtime_requested_tools"] == ["read"]
    assert validation.payload["model_tool_support"] is False
    assert validation.payload["blocked_reasons"] == ["tool_support_unsupported"]
    assert validation.payload["executable"] is False
    failed = [event for event in events if event.kind == "model.failed"][-1]
    assert failed.payload["error"]["category"] == "invalid_request"
    assert failed.payload["error"]["error_type"] == "ToolSupportUnsupported"
    assert "read" in failed.payload["error"]["message"]


def test_context_budget_blocks_before_provider_call_when_prompt_exceeds_model_limit(tmp_path) -> None:
    write_default_config(tmp_path)
    (tmp_path / ".harness" / "models.yaml").write_text(
        """
providers:
  responses_local:
    display_name: Responses Local
    enabled: true
    data_boundary: local_only
    base_url: http://localhost:11434/v1
    protocol: openai_responses
    credential:
      kind: static_local
    models:
      tiny-context:
        api_id: tiny-context
        context_limit: 8
        max_output_tokens: 2
        input_modalities: [text]
        output_modalities: [text]
        status: active
""".lstrip(),
        encoding="utf-8",
    )
    store = SQLiteStore.open_initialized(tmp_path)
    session = store.create_session(title="Runtime context block", raw_model_ref="responses_local/tiny-context")
    client = RuntimeFakeResponsesHttpClient()
    registry = ProtocolAdapterRegistry()
    registry.register(OpenAIResponsesProtocolAdapter(http_client=client))
    manager = SessionRuntimeManager.for_store(store, protocol_adapter_registry=registry)

    manager.submit_prompt(
        SessionPromptRequest(
            session_id=session.id,
            content="This prompt is intentionally longer than the tiny context window.",
            model_ref="responses_local/tiny-context",
        )
    )
    final = manager.wait(session.id, timeout=1.0)

    assert final.phase == SessionRuntimePhase.FAILED
    assert client.streams == []
    events = store.list_session_store_events(session.id)
    assert "model.started" not in [event.kind for event in events]
    validation = [event for event in events if event.kind == "session.model_validation"][-1]
    budget = validation.payload["runtime_context_budget"]
    assert budget["context_limit"] == 8
    assert budget["reserved_output_tokens"] == 2
    assert budget["max_input_tokens"] == 6
    assert budget["used_input_tokens"] > budget["max_input_tokens"]
    assert budget["within_budget"] is False
    assert validation.payload["blocked_reasons"] == ["context_limit_exceeded"]
    assert validation.payload["executable"] is False
    failed = [event for event in events if event.kind == "model.failed"][-1]
    assert failed.payload["error"]["category"] == "context_overflow"
    assert failed.payload["error"]["error_type"] == "ContextBudgetExceeded"


def test_max_cost_policy_blocks_before_provider_call_when_estimate_exceeds_limit(tmp_path) -> None:
    write_default_config(tmp_path)
    (tmp_path / ".harness" / "models.yaml").write_text(
        """
providers:
  responses_local:
    display_name: Responses Local
    enabled: true
    data_boundary: local_only
    base_url: http://localhost:11434/v1
    protocol: openai_responses
    credential:
      kind: static_local
    models:
      costly:
        api_id: costly
        context_limit: 1024
        max_output_tokens: 1
        input_modalities: [text]
        output_modalities: [text]
        cost:
          input_per_1m: 500000
          output_per_1m: 0
        status: active
""".lstrip(),
        encoding="utf-8",
    )
    store = SQLiteStore.open_initialized(tmp_path)
    session = store.create_session(title="Runtime cost block", raw_model_ref="responses_local/costly")
    client = RuntimeFakeResponsesHttpClient()
    registry = ProtocolAdapterRegistry()
    registry.register(OpenAIResponsesProtocolAdapter(http_client=client))
    manager = SessionRuntimeManager.for_store(store, protocol_adapter_registry=registry)

    manager.submit_prompt(
        SessionPromptRequest(
            session_id=session.id,
            content="This prompt has enough characters to cost more than one dollar.",
            model_ref="responses_local/costly",
            metadata={"max_cost_usd": "1.00"},
        )
    )
    final = manager.wait(session.id, timeout=1.0)

    assert final.phase == SessionRuntimePhase.POLICY_BLOCKED
    assert final.blocked_error_category == "provider_policy_block"
    assert final.blocked_error_type == "MaxCostPerRunExceeded"
    assert "Estimated run cost" in (final.blocked_reason or "")
    assert client.streams == []
    events = store.list_session_store_events(session.id)
    assert "model.started" not in [event.kind for event in events]
    validation = [event for event in events if event.kind == "session.model_validation"][-1]
    policy = validation.payload["runtime_cost_policy"]
    assert policy["max_cost_usd"] == "1.00"
    assert policy["pricing_available"] is True
    assert policy["within_budget"] is False
    assert Decimal(policy["projected_total_cost_usd"]) > Decimal(policy["max_cost_usd"])
    assert validation.payload["blocked_reasons"] == ["max_cost_per_run_exceeded"]
    assert validation.payload["executable"] is False
    failed = [event for event in events if event.kind == "model.failed"][-1]
    assert failed.payload["error"]["category"] == "provider_policy_block"
    assert failed.payload["error"]["error_type"] == "MaxCostPerRunExceeded"
    blocked = [event for event in events if event.kind == "harness.runtime.policy_blocked"][-1]
    assert blocked.payload["blocked_error_type"] == "MaxCostPerRunExceeded"
    assert blocked.payload["blocked_error_category"] == "provider_policy_block"
    assert blocked.payload["provider_execution_started"] is False
    assert blocked.payload["network_accessed"] is False


def test_max_tokens_policy_blocks_before_provider_call_when_estimate_exceeds_limit(tmp_path) -> None:
    write_default_config(tmp_path)
    (tmp_path / ".harness" / "models.yaml").write_text(
        """
providers:
  responses_local:
    display_name: Responses Local
    enabled: true
    data_boundary: local_only
    base_url: http://localhost:11434/v1
    protocol: openai_responses
    credential:
      kind: static_local
    models:
      token-capped:
        api_id: token-capped
        context_limit: 1024
        max_output_tokens: 4
        input_modalities: [text]
        output_modalities: [text]
        status: active
""".lstrip(),
        encoding="utf-8",
    )
    store = SQLiteStore.open_initialized(tmp_path)
    session = store.create_session(title="Runtime token policy block", raw_model_ref="responses_local/token-capped")
    client = RuntimeFakeResponsesHttpClient()
    registry = ProtocolAdapterRegistry()
    registry.register(OpenAIResponsesProtocolAdapter(http_client=client))
    manager = SessionRuntimeManager.for_store(store, protocol_adapter_registry=registry)

    manager.submit_prompt(
        SessionPromptRequest(
            session_id=session.id,
            content="This prompt is long enough to exceed a very small turn token policy.",
            model_ref="responses_local/token-capped",
            metadata={"max_tokens_per_turn": 8},
        )
    )
    final = manager.wait(session.id, timeout=1.0)

    assert final.phase == SessionRuntimePhase.POLICY_BLOCKED
    assert client.streams == []
    events = store.list_session_store_events(session.id)
    assert "model.started" not in [event.kind for event in events]
    validation = [event for event in events if event.kind == "session.model_validation"][-1]
    policy = validation.payload["runtime_token_policy"]
    assert policy["max_tokens_per_turn"] == 8
    assert policy["estimated_output_tokens"] == 4
    assert policy["estimated_total_tokens"] > policy["max_tokens_per_turn"]
    assert policy["within_budget"] is False
    assert validation.payload["blocked_reasons"] == ["max_tokens_per_turn_exceeded"]
    assert validation.payload["executable"] is False
    failed = [event for event in events if event.kind == "model.failed"][-1]
    assert failed.payload["error"]["category"] == "provider_policy_block"
    assert failed.payload["error"]["error_type"] == "MaxTokensPerTurnExceeded"


def test_hosted_provider_requires_approval_before_credentials_or_network(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TEAM_OPENAI_API_KEY", "sk-runtime-secret")
    write_default_config(tmp_path)
    (tmp_path / ".harness" / "models.yaml").write_text(
        """
providers:
  hosted_responses:
    display_name: Hosted Responses
    enabled: true
    approved: true
    data_boundary: hosted_provider
    base_url: https://api.example.com/v1
    protocol: openai_responses
    credential:
      kind: env
      env_var: TEAM_OPENAI_API_KEY
    models:
      resp-model:
        api_id: resp-model
        input_modalities: [text]
        output_modalities: [text]
        status: active
""".lstrip(),
        encoding="utf-8",
    )
    store = SQLiteStore.open_initialized(tmp_path)
    session = store.create_session(title="Hosted approval missing", raw_model_ref="hosted_responses/resp-model")
    client = RuntimeFakeResponsesHttpClient()
    registry = ProtocolAdapterRegistry()
    registry.register(OpenAIResponsesProtocolAdapter(http_client=client))
    manager = SessionRuntimeManager.for_store(store, protocol_adapter_registry=registry)

    manager.submit_prompt(
        SessionPromptRequest(session_id=session.id, content="No hosted approval.", model_ref="hosted_responses/resp-model")
    )
    final = manager.wait(session.id, timeout=1.0)

    assert final.phase == SessionRuntimePhase.POLICY_BLOCKED
    assert client.streams == []
    events = store.list_session_store_events(session.id)
    assert "model.started" not in [event.kind for event in events]
    validation = [event for event in events if event.kind == "session.model_validation"][-1]
    policy = validation.payload["runtime_hosted_provider_policy"]
    assert policy["provider_id"] == "hosted_responses"
    assert policy["data_boundary"] == "hosted_provider"
    assert policy["task_type"] == "session_provider_execution"
    assert policy["approved"] is False
    assert policy["approval_id"] is None
    assert "provider_credential" not in validation.payload
    assert validation.payload["blocked_reasons"] == ["hosted_provider_approval_required"]
    failed = [event for event in events if event.kind == "model.failed"][-1]
    assert failed.payload["error"]["category"] == "provider_policy_block"
    assert failed.payload["error"]["error_type"] == "HostedProviderApprovalRequired"


def test_paid_provider_requires_approval_after_hosted_approval_before_credentials_or_network(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TEAM_OPENAI_API_KEY", "sk-runtime-secret")
    write_default_config(tmp_path)
    (tmp_path / ".harness" / "models.yaml").write_text(
        """
providers:
  hosted_responses:
    display_name: Hosted Responses
    enabled: true
    approved: true
    data_boundary: hosted_provider
    base_url: https://api.example.com/v1
    protocol: openai_responses
    credential:
      kind: env
      env_var: TEAM_OPENAI_API_KEY
    models:
      resp-model:
        api_id: resp-model
        input_modalities: [text]
        output_modalities: [text]
        status: active
""".lstrip(),
        encoding="utf-8",
    )
    store = SQLiteStore.open_initialized(tmp_path)
    hosted_approval = ApprovalStore(tmp_path).add(
        "hosted_responses",
        "hosted_provider",
        ["session_provider_execution"],
        1,
    )
    session = store.create_session(title="Paid approval missing", raw_model_ref="hosted_responses/resp-model")
    client = RuntimeFakeResponsesHttpClient()
    registry = ProtocolAdapterRegistry()
    registry.register(OpenAIResponsesProtocolAdapter(http_client=client))
    manager = SessionRuntimeManager.for_store(store, protocol_adapter_registry=registry)

    manager.submit_prompt(
        SessionPromptRequest(session_id=session.id, content="No paid approval.", model_ref="hosted_responses/resp-model")
    )
    final = manager.wait(session.id, timeout=1.0)

    assert final.phase == SessionRuntimePhase.POLICY_BLOCKED
    assert client.streams == []
    events = store.list_session_store_events(session.id)
    assert "model.started" not in [event.kind for event in events]
    validation = [event for event in events if event.kind == "session.model_validation"][-1]
    assert validation.payload["runtime_hosted_provider_policy"]["approval_id"] == hosted_approval.id
    policy = validation.payload["runtime_paid_provider_policy"]
    assert policy["provider_id"] == "hosted_responses"
    assert policy["billing_mode"] == "paid_api"
    assert policy["task_type"] == "session_provider_execution"
    assert policy["approved"] is False
    assert policy["approval_id"] is None
    assert "provider_credential" not in validation.payload
    assert validation.payload["blocked_reasons"] == ["paid_provider_approval_required"]
    failed = [event for event in events if event.kind == "model.failed"][-1]
    assert failed.payload["error"]["category"] == "provider_policy_block"
    assert failed.payload["error"]["error_type"] == "PaidProviderApprovalRequired"


def test_data_boundary_requires_approval_before_credentials_or_network(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TEAM_ROUTER_API_KEY", "sk-runtime-secret")
    write_default_config(tmp_path)
    (tmp_path / ".harness" / "models.yaml").write_text(
        """
providers:
  router_responses:
    display_name: Router Responses
    enabled: true
    approved: true
    data_boundary: external_router
    base_url: https://router.example.com/v1
    protocol: openai_responses
    credential:
      kind: env
      env_var: TEAM_ROUTER_API_KEY
    models:
      resp-model:
        api_id: resp-model
        input_modalities: [text]
        output_modalities: [text]
        status: active
""".lstrip(),
        encoding="utf-8",
    )
    store = SQLiteStore.open_initialized(tmp_path)
    paid_approval = ApprovalStore(tmp_path).add("router_responses", "paid_provider", ["session_provider_execution"], 1)
    session = store.create_session(title="Data boundary approval missing", raw_model_ref="router_responses/resp-model")
    client = RuntimeFakeResponsesHttpClient()
    registry = ProtocolAdapterRegistry()
    registry.register(OpenAIResponsesProtocolAdapter(http_client=client))
    manager = SessionRuntimeManager.for_store(store, protocol_adapter_registry=registry)

    manager.submit_prompt(
        SessionPromptRequest(
            session_id=session.id,
            content="No external router approval.",
            model_ref="router_responses/resp-model",
        )
    )
    final = manager.wait(session.id, timeout=1.0)

    assert final.phase == SessionRuntimePhase.POLICY_BLOCKED
    assert client.streams == []
    events = store.list_session_store_events(session.id)
    assert "model.started" not in [event.kind for event in events]
    validation = [event for event in events if event.kind == "session.model_validation"][-1]
    assert "runtime_hosted_provider_policy" not in validation.payload
    assert validation.payload["runtime_paid_provider_policy"]["approval_id"] == paid_approval.id
    policy = validation.payload["runtime_data_boundary_policy"]
    assert policy["required_approval"] == "data_boundary:external_router"
    assert policy["provider_id"] == "router_responses"
    assert policy["data_boundary"] == "external_router"
    assert policy["task_type"] == "session_provider_execution"
    assert policy["approved"] is False
    assert policy["approval_id"] is None
    assert "provider_credential" not in validation.payload
    assert validation.payload["blocked_reasons"] == ["data_boundary_approval_required"]
    failed = [event for event in events if event.kind == "model.failed"][-1]
    assert failed.payload["error"]["category"] == "provider_policy_block"
    assert failed.payload["error"]["error_type"] == "DataBoundaryApprovalRequired"


def test_runtime_blocks_missing_env_credential_before_network(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("TEAM_OPENAI_API_KEY", raising=False)
    write_default_config(tmp_path)
    (tmp_path / ".harness" / "models.yaml").write_text(
        """
providers:
  hosted_responses:
    display_name: Hosted Responses
    enabled: true
    approved: true
    data_boundary: hosted_provider
    base_url: https://api.example.com/v1
    protocol: openai_responses
    credential:
      kind: env
      env_var: TEAM_OPENAI_API_KEY
    models:
      resp-model:
        api_id: resp-model
        input_modalities: [text]
        output_modalities: [text]
        status: active
""".lstrip(),
        encoding="utf-8",
    )
    store = SQLiteStore.open_initialized(tmp_path)
    approval = ApprovalStore(tmp_path).add("hosted_responses", "hosted_provider", ["session_provider_execution"], 1)
    paid_approval = ApprovalStore(tmp_path).add("hosted_responses", "paid_provider", ["session_provider_execution"], 1)
    session = store.create_session(title="Missing credential", raw_model_ref="hosted_responses/resp-model")
    client = RuntimeFakeResponsesHttpClient()
    registry = ProtocolAdapterRegistry()
    registry.register(OpenAIResponsesProtocolAdapter(http_client=client))
    manager = SessionRuntimeManager.for_store(store, protocol_adapter_registry=registry)

    manager.submit_prompt(SessionPromptRequest(session_id=session.id, content="No network.", model_ref="hosted_responses/resp-model"))
    final = manager.wait(session.id, timeout=1.0)

    assert final.phase == SessionRuntimePhase.FAILED
    assert client.streams == []
    events = store.list_session_store_events(session.id)
    assert "model.started" not in [event.kind for event in events]
    validation = [event for event in events if event.kind == "session.model_validation"][-1]
    assert validation.payload["runtime_hosted_provider_policy"]["approval_id"] == approval.id
    assert validation.payload["runtime_paid_provider_policy"]["approval_id"] == paid_approval.id
    assert validation.payload["provider_execution_started"] is False
    assert validation.payload["model_execution_started"] is False
    assert validation.payload["provider_credential"]["network_accessed"] is False
    assert validation.payload["provider_credential"]["blocked_reasons"] == ["credential_missing"]
    failed = [event for event in events if event.kind == "model.failed"][-1]
    assert failed.payload["provider_execution_started"] is False
    assert failed.payload["model_execution_started"] is False
    assert failed.payload["network_accessed"] is False
    assert failed.payload["error"]["error_type"] == "ProviderCredentialResolutionError"
    assert failed.payload["error"]["category"] == "configuration"


def test_runtime_uses_env_credential_without_persisting_value(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TEAM_OPENAI_API_KEY", "sk-runtime-secret")
    write_default_config(tmp_path)
    (tmp_path / ".harness" / "models.yaml").write_text(
        """
providers:
  hosted_responses:
    display_name: Hosted Responses
    enabled: true
    approved: true
    data_boundary: hosted_provider
    base_url: https://api.example.com/v1
    protocol: openai_responses
    credential:
      kind: env
      env_var: TEAM_OPENAI_API_KEY
    models:
      resp-model:
        api_id: resp-model
        input_modalities: [text]
        output_modalities: [text]
        status: active
""".lstrip(),
        encoding="utf-8",
    )
    store = SQLiteStore.open_initialized(tmp_path)
    approval = ApprovalStore(tmp_path).add("hosted_responses", "hosted_provider", ["session_provider_execution"], 1)
    paid_approval = ApprovalStore(tmp_path).add("hosted_responses", "paid_provider", ["session_provider_execution"], 1)
    session = store.create_session(title="Env credential", raw_model_ref="hosted_responses/resp-model")
    client = RuntimeFakeResponsesHttpClient()
    registry = ProtocolAdapterRegistry()
    registry.register(OpenAIResponsesProtocolAdapter(http_client=client))
    manager = SessionRuntimeManager.for_store(store, protocol_adapter_registry=registry)

    manager.submit_prompt(SessionPromptRequest(session_id=session.id, content="Use env.", model_ref="hosted_responses/resp-model"))
    final = manager.wait(session.id, timeout=1.0)

    assert final.phase == SessionRuntimePhase.IDLE
    assert client.streams[0]["headers"]["Authorization"] == "Bearer sk-runtime-secret"
    events = store.list_session_store_events(session.id)
    serialized_events = str([event.payload for event in events])
    assert "sk-runtime-secret" not in serialized_events
    validation = [event for event in events if event.kind == "session.model_validation"][-1]
    assert validation.payload["runtime_hosted_provider_policy"]["approval_id"] == approval.id
    assert validation.payload["runtime_paid_provider_policy"]["approval_id"] == paid_approval.id
    assert validation.payload["provider_credential"]["env_var"] == "TEAM_OPENAI_API_KEY"
    assert validation.payload["provider_credential"]["credentials_included"] is False
    started = [event for event in events if event.kind == "model.started"][-1]
    assert started.payload["provider_credential"]["env_var"] == "TEAM_OPENAI_API_KEY"
    assert started.payload["provider_credential"]["credentials_included"] is False


def test_custom_provider_header_env_refs_are_resolved_only_at_runtime(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TEAM_HEADER_TOKEN", "team-header-secret")
    write_default_config(tmp_path)
    (tmp_path / ".harness" / "models.yaml").write_text(
        """
providers:
  header_local:
    display_name: Header Local
    enabled: true
    data_boundary: local_only
    base_url: http://localhost:11434/v1
    protocol: openai_responses
    credential:
      kind: static_local
    headers:
      X-Team-Token:
        kind: env
        env_var: TEAM_HEADER_TOKEN
    models:
      resp-model:
        api_id: resp-model
        input_modalities: [text]
        output_modalities: [text]
        status: active
""".lstrip(),
        encoding="utf-8",
    )
    store = SQLiteStore.open_initialized(tmp_path)
    session = store.create_session(title="Header credential", raw_model_ref="header_local/resp-model")
    client = RuntimeFakeResponsesHttpClient()
    registry = ProtocolAdapterRegistry()
    registry.register(OpenAIResponsesProtocolAdapter(http_client=client))
    manager = SessionRuntimeManager.for_store(store, protocol_adapter_registry=registry)

    manager.submit_prompt(SessionPromptRequest(session_id=session.id, content="Use header.", model_ref="header_local/resp-model"))
    final = manager.wait(session.id, timeout=1.0)

    assert final.phase == SessionRuntimePhase.IDLE
    assert client.streams[0]["headers"]["X-Team-Token"] == "team-header-secret"
    events = store.list_session_store_events(session.id)
    serialized_events = str([event.payload for event in events])
    assert "team-header-secret" not in serialized_events
    validation = [event for event in events if event.kind == "session.model_validation"][-1]
    assert validation.payload["provider_credential"]["header_names"] == ["X-Team-Token"]


def test_session_runtime_records_alias_resolution_in_protocol_request_metadata(tmp_path) -> None:
    write_default_config(tmp_path)
    store = SQLiteStore.open_initialized(tmp_path)
    ApprovalStore(tmp_path).add("codex_cli", "hosted_provider", ["session_provider_execution"], 1)
    session = store.create_session(title="Runtime alias descriptor", raw_model_ref="codex/gpt-5.5")
    adapter = RuntimeProtocolAdapter()
    registry = ProtocolAdapterRegistry()
    registry.register(adapter)
    manager = SessionRuntimeManager.for_store(store, protocol_adapter_registry=registry)

    accepted = manager.submit_prompt(
        SessionPromptRequest(
            session_id=session.id,
            content="Use alias routing.",
            model_ref="codex/gpt-5.5",
            queue_policy=SessionPromptQueuePolicy.FOLLOW_UP,
        )
    )
    final = manager.wait(session.id, timeout=1.0)

    assert accepted.execution_started is True
    assert final.phase == SessionRuntimePhase.IDLE
    provider, model, request = adapter.calls[0]
    assert provider.provider_id == "codex_cli"
    assert model.raw_model_ref == "codex_cli/gpt-5.5"
    assert request.model_ref == "codex/gpt-5.5"
    assert request.metadata["canonical_model_ref"] == "codex_cli/gpt-5.5"
    assert request.metadata["alias_used"] == "codex/gpt-5.5"
    events = store.list_session_store_events(session.id)
    validation = [event for event in events if event.kind == "session.model_validation"][-1]
    assert validation.payload["source"] == "session_runtime"
    assert validation.payload["raw_model_ref"] == "codex/gpt-5.5"
    assert validation.payload["canonical_model_ref"] == "codex_cli/gpt-5.5"
    assert validation.payload["alias_used"] == "codex/gpt-5.5"


def test_retry_preserves_provider_model_and_variant(tmp_path) -> None:
    write_default_config(tmp_path)
    store = SQLiteStore.open_initialized(tmp_path)
    ApprovalStore(tmp_path).add("codex_cli", "hosted_provider", ["session_provider_execution"], 1)
    session = store.create_session(title="Runtime retry descriptor", raw_model_ref="codex/gpt-5.5")
    adapter = RuntimeRetryProtocolAdapter(
        ProviderError(
            category=ProviderErrorCategory.UNAVAILABLE,
            error_type="ProviderUnavailable",
            message="temporary provider outage",
            retryable=True,
        )
    )
    registry = ProtocolAdapterRegistry()
    registry.register(adapter)
    manager = SessionRuntimeManager.for_store(store, protocol_adapter_registry=registry)

    manager.submit_prompt(
        SessionPromptRequest(
            session_id=session.id,
            content="Retry without changing model.",
            model_ref="codex/gpt-5.5",
            metadata={"runtime_retry_delay_seconds": 0},
        )
    )
    final = manager.wait(session.id, timeout=1.0)

    assert final.phase == SessionRuntimePhase.IDLE
    assert len(adapter.calls) == 2
    first_provider, first_model, first_request = adapter.calls[0]
    second_provider, second_model, second_request = adapter.calls[1]
    assert first_provider.provider_id == second_provider.provider_id == "codex_cli"
    assert first_model.raw_model_ref == second_model.raw_model_ref == "codex_cli/gpt-5.5"
    assert first_model.model_id == second_model.model_id
    assert first_model.variants == second_model.variants
    assert first_request.model_ref == second_request.model_ref == "codex/gpt-5.5"
    stable_metadata_keys = [
        "canonical_model_ref",
        "alias_used",
        "resolved_provider_id",
        "resolved_model_ref",
        "resolved_model_id",
        "protocol",
        "requested_reasoning_effort",
        "resolved_reasoning_effort",
        "reasoning_resolution",
        "model_selection_source",
        "resolved_provider_options",
        "resolved_model_options",
    ]
    for key in stable_metadata_keys:
        assert first_request.metadata[key] == second_request.metadata[key], key
    assert first_request.metadata["attempt"] == 1
    assert second_request.metadata["attempt"] == 2
    events = store.list_session_store_events(session.id)
    retries = [event for event in events if event.kind == "harness.runtime.retry_scheduled"]
    assert len(retries) == 1
    assert retries[0].payload["reason"] == "retryable_provider_error"
    messages = store.list_session_messages(session.id)
    assert messages[-1].content_preview == "Retried descriptor response."


def test_session_runtime_missing_protocol_adapter_fails_without_model_start(tmp_path) -> None:
    write_default_config(tmp_path)
    store = SQLiteStore.open_initialized(tmp_path)
    ApprovalStore(tmp_path).add("codex_cli", "hosted_provider", ["session_provider_execution"], 1)
    session = store.create_session(title="Runtime missing adapter", raw_model_ref="codex_cli/gpt-5.5")
    manager = SessionRuntimeManager.for_store(store, protocol_adapter_registry=ProtocolAdapterRegistry())

    manager.submit_prompt(
        SessionPromptRequest(
            session_id=session.id,
            content="No adapter should run.",
            model_ref="codex_cli/gpt-5.5",
        )
    )
    final = manager.wait(session.id, timeout=1.0)

    assert final.phase == SessionRuntimePhase.FAILED
    events = store.list_session_store_events(session.id)
    assert "model.started" not in [event.kind for event in events]
    failed = [event for event in events if event.kind == "model.failed"][-1]
    assert failed.payload["error"]["error_type"] == "ProtocolAdapterNotFound"
    assert failed.payload["error"]["category"] == "configuration"
    assert failed.payload["error"]["hidden_provider_fallback"] is False
    assert failed.payload["error"]["no_hidden_fallback"] is True
    validation = [event for event in events if event.kind == "session.model_validation"][-1]
    assert validation.payload["executable"] is True
    assert validation.payload["provider_execution_started"] is False


def test_session_runtime_unknown_model_fails_before_protocol_adapter_execution(tmp_path) -> None:
    write_default_config(tmp_path)
    store = SQLiteStore.open_initialized(tmp_path)
    session = store.create_session(title="Runtime unknown model", raw_model_ref="codex_cli/not-real")
    adapter = RuntimeProtocolAdapter()
    registry = TrackingProtocolAdapterRegistry()
    registry.register(adapter)
    manager = SessionRuntimeManager.for_store(store, protocol_adapter_registry=registry)

    manager.submit_prompt(
        SessionPromptRequest(
            session_id=session.id,
            content="Unknown model.",
            model_ref="codex_cli/not-real",
        )
    )
    final = manager.wait(session.id, timeout=1.0)

    assert final.phase == SessionRuntimePhase.FAILED
    assert adapter.calls == []
    assert registry.has_calls == []
    assert registry.get_calls == []
    events = store.list_session_store_events(session.id)
    resolution = [event for event in events if event.kind == "session.model_resolution"][-1]
    assert resolution.payload["source"] == "command_arg"
    assert resolution.payload["blocked_reasons"] == ["model_unknown"]
    assert resolution.payload["provider_execution_started"] is False
    assert resolution.payload["model_execution_started"] is False
    assert resolution.payload["network_accessed"] is False
    assert "session.model_validation" not in [event.kind for event in events]
    failed = [event for event in events if event.kind == "model.failed"][-1]
    assert failed.payload["provider_execution_started"] is False
    assert failed.payload["model_execution_started"] is False
    assert failed.payload["network_accessed"] is False
    assert failed.payload["error"]["error_type"] == "ModelResolutionFailed"
    assert "model_unknown" in failed.payload["error"]["message"]


def test_session_runtime_unknown_provider_fails_before_protocol_adapter_lookup(tmp_path) -> None:
    write_default_config(tmp_path)
    store = SQLiteStore.open_initialized(tmp_path)
    session = store.create_session(title="Runtime unknown provider", raw_model_ref="missing_provider/not-real")
    adapter = RuntimeProtocolAdapter()
    registry = TrackingProtocolAdapterRegistry()
    registry.register(adapter)
    manager = SessionRuntimeManager.for_store(store, protocol_adapter_registry=registry)

    manager.submit_prompt(
        SessionPromptRequest(
            session_id=session.id,
            content="Unknown provider.",
            model_ref="missing_provider/not-real",
        )
    )
    final = manager.wait(session.id, timeout=1.0)

    assert final.phase == SessionRuntimePhase.FAILED
    assert adapter.calls == []
    assert registry.has_calls == []
    assert registry.get_calls == []
    events = store.list_session_store_events(session.id)
    resolution = [event for event in events if event.kind == "session.model_resolution"][-1]
    assert resolution.payload["source"] == "command_arg"
    assert resolution.payload["blocked_reasons"] == ["provider_unknown", "model_unknown"]
    assert resolution.payload["provider_execution_started"] is False
    assert resolution.payload["model_execution_started"] is False
    assert resolution.payload["network_accessed"] is False
    assert "session.model_validation" not in [event.kind for event in events]
    failed = [event for event in events if event.kind == "model.failed"][-1]
    assert failed.payload["provider_execution_started"] is False
    assert failed.payload["model_execution_started"] is False
    assert failed.payload["network_accessed"] is False
    assert failed.payload["error"]["error_type"] == "ModelResolutionFailed"
    assert "provider_unknown" in failed.payload["error"]["message"]


def test_session_runtime_disabled_model_fails_before_protocol_adapter_lookup(tmp_path) -> None:
    write_default_config(tmp_path)
    (tmp_path / ".harness" / "models.yaml").write_text(
        """
providers:
  router_team:
    display_name: Router Team
    enabled: true
    data_boundary: local_only
    base_url: http://localhost:11434/v1
    protocol: openai_chat
    credential:
      kind: static_local
    disabled_models: [beta]
    models:
      beta:
        api_id: beta
        context_window: 8192
        max_output_tokens: 1024
        input_modalities: [text]
        output_modalities: [text]
        tool_support: true
""".lstrip(),
        encoding="utf-8",
    )
    store = SQLiteStore.open_initialized(tmp_path)
    session = store.create_session(title="Runtime disabled model", raw_model_ref="router_team/beta")
    registry = TrackingProtocolAdapterRegistry()
    registry.register(OpenAIChatProtocolAdapter(http_client=RuntimeFakeResponsesHttpClient()))
    manager = SessionRuntimeManager.for_store(store, protocol_adapter_registry=registry)

    manager.submit_prompt(
        SessionPromptRequest(
            session_id=session.id,
            content="Disabled model.",
            model_ref="router_team/beta",
        )
    )
    final = manager.wait(session.id, timeout=1.0)

    assert final.phase == SessionRuntimePhase.FAILED
    assert registry.has_calls == []
    assert registry.get_calls == []
    events = store.list_session_store_events(session.id)
    assert "model.started" not in [event.kind for event in events]
    resolution = [event for event in events if event.kind == "session.model_resolution"][-1]
    validation = [event for event in events if event.kind == "session.model_validation"][-1]
    assert resolution.payload["source"] == "command_arg"
    assert resolution.payload["raw_model_ref"] == "router_team/beta"
    assert resolution.payload["provider_execution_started"] is False
    assert resolution.payload["model_execution_started"] is False
    assert resolution.payload["network_accessed"] is False
    assert validation.payload["blocked_reasons"] == ["model_disabled"]
    assert validation.payload["provider_execution_started"] is False
    assert validation.payload["model_execution_started"] is False
    assert validation.payload["network_accessed"] is False
    assert "provider_credential" not in validation.payload
    failed = [event for event in events if event.kind == "model.failed"][-1]
    assert failed.payload["provider_execution_started"] is False
    assert failed.payload["model_execution_started"] is False
    assert failed.payload["network_accessed"] is False
    assert failed.payload["error"]["error_type"] == "ModelSelectionValidationFailed"
    assert "model_disabled" in failed.payload["error"]["message"]


def test_session_runtime_persists_generic_provider_adapter_events(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    session = store.create_session(title="Runtime provider adapter")
    adapter = StaticProviderAdapter()
    manager = SessionRuntimeManager.for_store(store, provider_adapter=adapter)

    accepted = manager.submit_prompt(
        SessionPromptRequest(
            session_id=session.id,
            content="Use the provider adapter.",
            message_id=None,
            queue_policy=SessionPromptQueuePolicy.FOLLOW_UP,
        )
    )
    final = manager.wait(session.id, timeout=1.0)

    assert accepted.execution_started is True
    assert final.phase == SessionRuntimePhase.IDLE
    assert adapter.requests[0].messages == [ProviderMessage(role="user", content="Use the provider adapter.")]
    messages = store.list_session_messages(session.id)
    assert messages[-1].role.value == "assistant"
    assert messages[-1].content_preview == "Adapter response."
    kinds = [event.kind for event in store.list_session_store_events(session.id)]
    assert kinds.count("model.started") == 1
    assert kinds.count("model.message_delta") == 1
    assert kinds.count("model.completed") == 1
    provider_delta = next(event for event in store.list_session_store_events(session.id) if event.kind == "model.message_delta")
    assert provider_delta.payload["provider_id"] == "static_provider"
    assert provider_delta.payload["model_ref"] == "static/model"


def test_session_runtime_provider_request_includes_prior_messages_for_follow_up(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    session = store.create_session(title="Runtime follow-up")
    first_user = store.append_session_message(session.id, SessionMessageRole.USER, "Where is the Python code located?")
    store.append_session_message(
        session.id,
        SessionMessageRole.ASSISTANT,
        '{"type":"harness.tool_request/v1","tool":"read","reason":"Find Python source files."}',
        parent_message_id=first_user.id,
    )
    follow_up = store.append_session_message(session.id, SessionMessageRole.USER, "yes")
    adapter = StaticProviderAdapter()
    manager = SessionRuntimeManager.for_store(store, provider_adapter=adapter)

    accepted = manager.submit_prompt(
        SessionPromptRequest(
            session_id=session.id,
            content="yes",
            message_id=follow_up.id,
            queue_policy=SessionPromptQueuePolicy.FOLLOW_UP,
        )
    )
    final = manager.wait(session.id, timeout=1.0)

    assert accepted.execution_started is True
    assert final.phase == SessionRuntimePhase.IDLE
    assert [message.role for message in adapter.requests[0].messages] == ["user", "assistant", "user"]
    assert [message.content for message in adapter.requests[0].messages] == [
        "Where is the Python code located?",
        '{"type":"harness.tool_request/v1","tool":"read","reason":"Find Python source files."}',
        "yes",
    ]


def test_session_runtime_propagates_abort_checker_to_provider_stream(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    session = store.create_session(title="Runtime stream abort")
    adapter = AbortAwareProviderAdapter()
    manager = SessionRuntimeManager.for_store(store, provider_adapter=adapter)

    manager.submit_prompt(SessionPromptRequest(session_id=session.id, content="Abort this stream."))
    deadline = time.monotonic() + 1.0
    while not adapter.requests and time.monotonic() < deadline:
        time.sleep(0.01)
    manager.abort(session.id, reason="operator stopped provider stream")
    final = manager.wait(session.id, timeout=2.0)

    assert adapter.requests
    assert callable(adapter.requests[0].context["abort_checker"])
    assert final.phase == SessionRuntimePhase.FAILED
    events = store.list_session_store_events(session.id)
    assert "model.aborted" in [event.kind for event in events]
    aborted = next(event for event in events if event.kind == "model.aborted")
    assert aborted.payload["error"]["category"] == "aborted"
    finished = [event for event in events if event.kind == "harness.turn.finished"][-1]
    assert finished.payload["failed"] is True
    assert finished.payload["provider_error"]["category"] == "aborted"


def test_session_runtime_retries_retryable_provider_failure_once(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    session = store.create_session(title="Runtime retry")
    adapter = FailsThenSucceedsProviderAdapter(
        ProviderError(
            category=ProviderErrorCategory.UNAVAILABLE,
            error_type="TimeoutError",
            message="temporary provider timeout",
            retryable=True,
        )
    )
    manager = SessionRuntimeManager.for_store(store, provider_adapter=adapter)

    manager.submit_prompt(
        SessionPromptRequest(
            session_id=session.id,
            content="Retry this provider call.",
            metadata={"runtime_retry_delay_seconds": 0},
        )
    )
    final = manager.wait(session.id, timeout=1.0)

    assert final.phase == SessionRuntimePhase.IDLE
    assert len(adapter.requests) == 2
    assert [request.context["attempt"] for request in adapter.requests] == [1, 2]
    messages = store.list_session_messages(session.id)
    assert messages[-1].content_preview == "Recovered response."
    events = store.list_session_store_events(session.id)
    retry = [event for event in events if event.kind == "harness.runtime.retry_scheduled"][-1]
    assert retry.payload["reason"] == "retryable_provider_error"
    assert retry.payload["attempt"] == 1
    assert retry.payload["next_attempt"] == 2
    assert retry.payload["delay_seconds"] == 0.0
    assert retry.payload["category"] == "provider_unavailable"
    assert retry.payload["error_type"] == "TimeoutError"
    assert retry.payload["retryable"] is True
    assert retry.payload["hidden_provider_fallback"] is False
    assert retry.payload["no_hidden_fallback"] is True


def test_session_runtime_uses_rate_limit_retry_after_hint(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    session = store.create_session(title="Runtime rate limit retry hint")
    adapter = FailsThenSucceedsProviderAdapter(
        ProviderError(
            category=ProviderErrorCategory.RATE_LIMIT,
            error_type="RateLimitExceeded",
            message="provider asked us to slow down",
            retryable=True,
            retry_after_seconds=0.01,
        )
    )
    manager = SessionRuntimeManager.for_store(store, provider_adapter=adapter)

    manager.submit_prompt(
        SessionPromptRequest(
            session_id=session.id,
            content="Retry this provider call using its hint.",
            metadata={"runtime_retry_delay_seconds": 5},
        )
    )
    final = manager.wait(session.id, timeout=1.0)

    assert final.phase == SessionRuntimePhase.IDLE
    assert len(adapter.requests) == 2
    events = store.list_session_store_events(session.id)
    retry = [event for event in events if event.kind == "harness.runtime.retry_scheduled"][-1]
    assert retry.payload["reason"] == "retryable_provider_error"
    assert retry.payload["delay_seconds"] == 0.01
    assert retry.payload["retry_after_seconds"] == 0.01
    assert retry.payload["category"] == "rate_limit"
    assert retry.payload["retryable"] is True
    assert retry.payload["no_hidden_fallback"] is True
    assert retry.payload["provider_error"]["category"] == "rate_limit"
    assert retry.payload["provider_error"]["retry_after_seconds"] == 0.01


def test_session_runtime_non_retryable_provider_failure_fails_visibly(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    session = store.create_session(title="Runtime no retry")
    adapter = AlwaysFailsProviderAdapter(
        ProviderError(
            category=ProviderErrorCategory.CONFIGURATION,
            error_type="BackendConfigError",
            message="missing model configuration",
            retryable=False,
        )
    )
    manager = SessionRuntimeManager.for_store(store, provider_adapter=adapter)

    manager.submit_prompt(SessionPromptRequest(session_id=session.id, content="Do not retry this provider call."))
    final = manager.wait(session.id, timeout=1.0)

    assert final.phase == SessionRuntimePhase.FAILED
    assert len(adapter.requests) == 1
    messages = store.list_session_messages(session.id)
    assert messages == []
    events = store.list_session_store_events(session.id)
    assert [event.kind for event in events].count("model.failed") == 1
    assert not [event for event in events if event.kind == "harness.runtime.retry_scheduled"]
    finished = [event for event in events if event.kind == "harness.turn.finished"][-1]
    assert finished.payload["failed"] is True
    assert finished.payload["provider_error"]["category"] == "configuration"


def test_partial_stream_failure_preserves_partial_text_and_error(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    session = store.create_session(title="Runtime partial failure")
    adapter = PartialThenFailsProviderAdapter(
        ProviderError(
            category=ProviderErrorCategory.INVALID_RESPONSE,
            error_type="MalformedProviderChunk",
            message="provider stream failed after partial output",
            retryable=False,
        ),
        partial_text="Partial answer before failure.",
    )
    manager = SessionRuntimeManager.for_store(store, provider_adapter=adapter)

    manager.submit_prompt(SessionPromptRequest(session_id=session.id, content="Fail after partial output."))
    final = manager.wait(session.id, timeout=1.0)

    assert final.phase == SessionRuntimePhase.FAILED
    assert len(adapter.requests) == 1
    assert store.list_session_messages(session.id) == []
    events = store.list_session_store_events(session.id)
    delta = [event for event in events if event.kind == "model.message_delta"][-1]
    assert delta.payload["delta"] == "Partial answer before failure."
    partial = [event for event in events if event.kind == "harness.runtime.partial_response"][-1]
    assert partial.payload["present"] is True
    assert partial.payload["text_preview"] == "Partial answer before failure."
    assert partial.payload["char_count"] == len("Partial answer before failure.")
    assert partial.payload["assistant_message_persisted"] is False
    assert partial.payload["failed"] is True
    failed = [event for event in events if event.kind == "model.failed"][-1]
    assert failed.payload["error"]["error_type"] == "MalformedProviderChunk"
    finished = [event for event in events if event.kind == "harness.turn.finished"][-1]
    assert finished.payload["failed"] is True
    assert finished.payload["provider_error"]["error_type"] == "MalformedProviderChunk"
    assert finished.payload["partial_response"]["text_preview"] == "Partial answer before failure."
    assert finished.payload["partial_response"]["assistant_message_persisted"] is False


def test_session_runtime_context_overflow_compacts_and_retries_once_even_when_non_retryable(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    session = store.create_session(title="Runtime compaction")
    store.append_session_message(session.id, "user", "Earlier request about a parser failure.")
    store.append_session_message(session.id, "assistant", "Earlier answer with implementation details.")
    adapter = FailsThenSucceedsProviderAdapter(
        ProviderError(
            category=ProviderErrorCategory.CONTEXT_OVERFLOW,
            error_type="ContextLengthExceeded",
            message="maximum context window exceeded",
            retryable=False,
        ),
        response="Compacted response.",
    )
    manager = SessionRuntimeManager.for_store(store, provider_adapter=adapter)

    manager.submit_prompt(SessionPromptRequest(session_id=session.id, content="Continue after compaction."))
    final = manager.wait(session.id, timeout=1.0)

    assert final.phase == SessionRuntimePhase.IDLE
    assert len(adapter.requests) == 2
    first_request = adapter.requests[0]
    retry_request = adapter.requests[-1]
    assert first_request.provider_id == retry_request.provider_id
    assert first_request.model_ref == retry_request.model_ref
    assert retry_request.context["attempt"] == 2
    assert retry_request.context["context_compaction"]["schema_version"] == "harness.runtime_compaction/v1"
    assert retry_request.context["context_compaction"]["method"] == "deterministic_recent_message_summary"
    assert retry_request.messages[0].role == "system"
    assert "Earlier request" in retry_request.messages[0].content
    events = store.list_session_store_events(session.id)
    assert "harness.runtime.compaction.started" in [event.kind for event in events]
    provider_events = [
        event
        for event in events
        if event.kind in {"model.started", "model.failed", "model.completed"}
    ]
    assert provider_events
    assert {event.payload["provider_id"] for event in provider_events} == {adapter.provider_id}
    assert {event.payload["model_ref"] for event in provider_events} == {adapter.model_ref}
    started = [event for event in events if event.kind == "harness.runtime.compaction.started"][-1]
    assert started.payload["provider_error"]["retryable"] is False
    completed = [event for event in events if event.kind == "harness.runtime.compaction.completed"][-1]
    assert completed.payload["message_count_before"] == 2
    retry = [event for event in events if event.kind == "harness.runtime.retry_scheduled"][-1]
    assert retry.payload["reason"] == "context_overflow"
    assert retry.payload["attempt"] == 1
    assert retry.payload["next_attempt"] == 2
    assert retry.payload["delay_seconds"] == 0.0
    assert retry.payload["category"] == "context_overflow"
    assert retry.payload["retryable"] is False
    assert retry.payload["hidden_provider_fallback"] is False
    assert retry.payload["no_hidden_fallback"] is True
    assert retry.payload["provider_error"]["category"] == "context_overflow"
    assert retry.payload["provider_error"]["retryable"] is False
    assert not [event for event in events if event.kind == "harness.runtime.retry_scheduled" and event.payload["reason"] == "retryable_provider_error"]


def test_session_runtime_abort_during_retry_wait_stops_cleanly(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    session = store.create_session(title="Runtime abort retry")
    adapter = AlwaysFailsProviderAdapter(
        ProviderError(
            category=ProviderErrorCategory.UNAVAILABLE,
            error_type="TimeoutError",
            message="temporary provider timeout",
            retryable=True,
        )
    )
    manager = SessionRuntimeManager.for_store(store, provider_adapter=adapter)

    manager.submit_prompt(
        SessionPromptRequest(
            session_id=session.id,
            content="Abort this retry.",
            metadata={"runtime_retry_delay_seconds": 5},
        )
    )
    deadline = 1.0
    while deadline > 0:
        if manager.status(session.id).phase == SessionRuntimePhase.RETRY_WAIT:
            break
        time.sleep(0.01)
        deadline -= 0.01
    assert manager.status(session.id).phase == SessionRuntimePhase.RETRY_WAIT

    aborted = manager.abort(session.id, reason="operator stopped retry")
    final = manager.wait(session.id, timeout=1.0)

    assert aborted.phase == SessionRuntimePhase.ABORTING
    assert final.phase == SessionRuntimePhase.FAILED
    assert len(adapter.requests) == 1
    events = store.list_session_store_events(session.id)
    assert "harness.runtime.retry_aborted" in [event.kind for event in events]


def test_session_runtime_routes_provider_tool_calls_through_session_gateway(tmp_path) -> None:
    (tmp_path / "README.md").write_text("# Project\nTool gateway content.\n", encoding="utf-8")
    write_default_config(tmp_path)
    store = SQLiteStore(tmp_path)
    store.initialize()
    session = store.create_session(title="Runtime provider tool")
    adapter = ToolCallingProviderAdapter(tool_name="read", arguments={"path": "README.md"})
    manager = SessionRuntimeManager.for_store(store, provider_adapter=adapter)

    manager.submit_prompt(SessionPromptRequest(session_id=session.id, content="Read README."))
    final = manager.wait(session.id, timeout=1.0)

    assert final.phase == SessionRuntimePhase.IDLE
    messages = store.list_session_messages(session.id)
    tool_messages = [message for message in messages if message.role.value == "tool"]
    assert tool_messages
    assert "Tool gateway content." in tool_messages[-1].content_preview
    events = store.list_session_store_events(session.id)
    output = [event for event in events if event.kind == "tool_call.output" and event.payload.get("tool_id") == "read"][-1]
    assert output.payload["ok"] is True
    assert output.payload["read_only"] is True
    after = [event for event in events if event.kind == "harness.tool_call.after" and event.payload["record"]["tool_id"] == "read"][-1]
    assert after.payload["record"]["tool_call_id"] == "call_provider_tool"


def test_session_runtime_provider_unknown_tool_becomes_model_visible_tool_error(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    write_default_config(tmp_path)
    store.initialize()
    session = store.create_session(title="Runtime unknown provider tool")
    adapter = ToolCallingProviderAdapter(tool_name="missing-tool", arguments={})
    manager = SessionRuntimeManager.for_store(store, provider_adapter=adapter)

    manager.submit_prompt(SessionPromptRequest(session_id=session.id, content="Use missing tool."))
    final = manager.wait(session.id, timeout=1.0)

    assert final.phase == SessionRuntimePhase.IDLE
    messages = store.list_session_messages(session.id)
    tool_messages = [message for message in messages if message.role.value == "tool"]
    assert tool_messages
    assert "Invalid tool call" in tool_messages[-1].content_preview
    assert "Requested tool: missing-tool" in tool_messages[-1].content_preview
    output = [event for event in store.list_session_store_events(session.id) if event.kind == "tool_call.output"][-1]
    assert output.payload["ok"] is False
    assert output.payload["error_type"] == "invalid_tool_call"
    assert output.payload["tool_id"] == "invalid"


def test_session_runtime_suspends_provider_tool_until_permission_reply_resumes(tmp_path) -> None:
    write_default_config(tmp_path)
    store = SQLiteStore(tmp_path)
    store.initialize()
    session = store.create_session(title="Runtime permission suspend")
    adapter = ToolCallingProviderAdapter(
        tool_name="shell",
        arguments={"command": "printf approved", "timeout_seconds": 5, "shell_executable": "/bin/sh"},
    )
    manager = SessionRuntimeManager.for_store(store, provider_adapter=adapter)

    manager.submit_prompt(SessionPromptRequest(session_id=session.id, content="Run approved shell."))
    waiting = manager.wait(session.id, timeout=1.0)

    assert waiting.phase == SessionRuntimePhase.WAITING_PERMISSION
    assert waiting.waiting_permission_id
    assert store.get_session(session.id).status.value == "waiting_approval"
    permission = store.get_session_permission(waiting.waiting_permission_id)
    assert permission.tool_id == "shell"

    reply = _reply_to_session_permission(
        store,
        session.id,
        permission.id,
        {"reply": "once"},
        project_root=tmp_path,
        resume=True,
    )

    assert reply["decision"] == "allowed"
    assert reply["resumed_result"]["ok"] is True
    assert reply["runtime"]["phase"] == "idle"
    assert store.get_session(session.id).status.value == "active"
    output_events = [
        event
        for event in store.list_session_store_events(session.id)
        if event.kind == "tool_call.output" and event.payload.get("tool_id") == "shell"
    ]
    assert output_events[0].payload["error_type"] == "permission_required"
    assert output_events[-1].payload["ok"] is True
    assert "approved" in output_events[-1].payload["preview"]
    events = store.list_session_store_events(session.id)
    runtime_events = [event.kind for event in events]
    assert "harness.runtime.permission_waiting" in runtime_events
    assert "harness.runtime.permission_resolved" in runtime_events
    process_events = [event for event in events if event.kind in {"harness.process.started", "harness.process.finished"}]
    assert [event.kind for event in process_events] == ["harness.process.started", "harness.process.finished"]
    assert process_events[0].payload["process"]["process_id"] == process_events[1].payload["process"]["process_id"]
    assert process_events[1].payload["process"]["status"] == "completed"


def test_session_runtime_denied_provider_permission_writes_tool_error_and_clears_wait(tmp_path) -> None:
    write_default_config(tmp_path)
    store = SQLiteStore(tmp_path)
    store.initialize()
    session = store.create_session(title="Runtime permission denial")
    adapter = ToolCallingProviderAdapter(
        tool_name="shell",
        arguments={"command": "printf denied", "timeout_seconds": 5, "shell_executable": "/bin/sh"},
    )
    manager = SessionRuntimeManager.for_store(store, provider_adapter=adapter)

    manager.submit_prompt(SessionPromptRequest(session_id=session.id, content="Run denied shell."))
    waiting = manager.wait(session.id, timeout=1.0)
    reply = _reply_to_session_permission(
        store,
        session.id,
        str(waiting.waiting_permission_id),
        {"reply": "reject", "reason": "not needed"},
        project_root=tmp_path,
        resume=True,
    )

    assert reply["decision"] == "denied"
    assert reply["runtime"]["phase"] == "idle"
    assert reply["model_visible_error"]
    tool_messages = [message for message in store.list_session_messages(session.id) if message.role.value == "tool"]
    assert "Tool call denied by operator." in tool_messages[-1].content_preview
    assert "Tool: shell" in tool_messages[-1].content_preview
    resolved = [event for event in store.list_session_store_events(session.id) if event.kind == "harness.runtime.permission_resolved"][-1]
    assert resolved.payload["decision"] == "denied"
    assert resolved.payload["resumed"] is False


def test_session_runtime_rejects_busy_prompt_when_policy_requires_reject(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    session = store.create_session(title="Runtime busy")
    manager = SessionRuntimeManager.for_store(store)
    running = manager.begin_turn(session.id, turn_id="turn_test")

    rejected = manager.submit_prompt(
        SessionPromptRequest(
            session_id=session.id,
            content="Do not queue this.",
            queue_policy=SessionPromptQueuePolicy.REJECT_IF_BUSY,
        )
    )

    assert running.phase == SessionRuntimePhase.RUNNING
    assert rejected.ok is False
    assert rejected.accepted is False
    assert rejected.queued is False
    assert rejected.phase == SessionRuntimePhase.RUNNING
    assert "busy" in str(rejected.reason)
    with pytest.raises(SessionRuntimeBusyError):
        manager.begin_turn(session.id, turn_id="turn_second")


def test_session_runtime_status_projection_closes_terminal_sessions(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    session = store.create_session(title="Runtime terminal")
    manager = SessionRuntimeManager.for_store(store, execution_enabled=False)
    manager.submit_prompt(SessionPromptRequest(session_id=session.id, content="Queued before cancel."))
    store.cancel_session(session.id, reason="No longer needed.")

    state = manager.status(session.id)

    assert state.phase == SessionRuntimePhase.CLOSED
    assert state.queued_prompt_count == 0
    assert state.process_running is False


def test_session_runtime_status_tracks_active_prompt_elapsed_time(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    session = store.create_session(title="Runtime elapsed")
    manager = SessionRuntimeManager.for_store(store)

    running = manager.begin_turn(session.id, turn_id="turn_elapsed")

    assert running.phase == SessionRuntimePhase.RUNNING
    assert running.active_turn_id == "turn_elapsed"
    assert running.active_started_at is not None
    assert running.active_elapsed_seconds is not None
    assert running.active_elapsed_seconds >= 0

    finished = manager.finish_turn(session.id)

    assert finished.phase == SessionRuntimePhase.IDLE
    assert finished.active_started_at is None
    assert finished.active_elapsed_seconds is None


def test_local_server_status_includes_runtime_projection(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    cfg = object()
    session = store.create_session(title="Server runtime")

    prompt = _route_post(
        f"/sessions/{session.id}/prompt_async",
        body={"content": "Queue this for the runtime."},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    status = _route_get(
        f"/sessions/{session.id}/status",
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )

    assert prompt["runtime"]["schema_version"] == "harness.session_prompt_accepted/v1"
    assert prompt["runtime"]["runtime"]["phase"] == "running"
    assert prompt["execution_started"] is True
    waited = _route_post(
        f"/api/session/{session.id}/wait",
        body={"timeout": 1},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    status = _route_get(
        f"/sessions/{session.id}/status",
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    assert waited["runtime"]["phase"] == "failed"
    assert status["runtime"]["schema_version"] == "harness.session_runtime_state/v1"
    assert status["runtime"]["phase"] == "failed"
    assert status["runtime"]["queued_prompt_count"] == 0
    assert status["process_running"] is False
