from __future__ import annotations

import time

import pytest

from harness.local_server import _reply_to_session_permission, _route_get, _route_post
from harness.config import write_default_config
from harness.memory.sqlite_store import SQLiteStore
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


def test_session_runtime_context_overflow_compacts_and_retries_once(tmp_path) -> None:
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
            retryable=True,
        ),
        response="Compacted response.",
    )
    manager = SessionRuntimeManager.for_store(store, provider_adapter=adapter)

    manager.submit_prompt(SessionPromptRequest(session_id=session.id, content="Continue after compaction."))
    final = manager.wait(session.id, timeout=1.0)

    assert final.phase == SessionRuntimePhase.IDLE
    assert len(adapter.requests) == 2
    retry_request = adapter.requests[-1]
    assert retry_request.context["attempt"] == 2
    assert retry_request.context["context_compaction"]["schema_version"] == "harness.runtime_compaction/v1"
    assert retry_request.context["context_compaction"]["method"] == "deterministic_recent_message_summary"
    assert retry_request.messages[0].role == "system"
    assert "Earlier request" in retry_request.messages[0].content
    events = store.list_session_store_events(session.id)
    assert "harness.runtime.compaction.started" in [event.kind for event in events]
    completed = [event for event in events if event.kind == "harness.runtime.compaction.completed"][-1]
    assert completed.payload["message_count_before"] == 2
    retry = [event for event in events if event.kind == "harness.runtime.retry_scheduled"][-1]
    assert retry.payload["reason"] == "context_overflow"


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
