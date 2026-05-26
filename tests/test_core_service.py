import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from typer.testing import CliRunner

from harness import model_discovery
from harness.approvals import ApprovalStore
from harness.cli.main import app
from harness.config import load_config, write_default_config
from harness.core_service import HarnessAppService, HarnessCoreService, HarnessHTTPAppService, HarnessHTTPServiceError
from harness.local_server import _route_get, create_local_http_server
from harness.memory.sqlite_store import SQLiteStore
from harness.models import (
    EventStreamType,
    RedactionState,
    SessionMessageRole,
    SessionPermissionBoundaryKind,
    SessionPermissionScope,
    SessionPermissionSource,
)
from harness.operator_context import build_session_pane_projection, build_tui_dashboard
from harness.protocol_adapters import OpenAIChatProtocolAdapter, ProtocolAdapterRegistry
from harness.provider_events import ProviderCapabilities, ProviderEventKind, ProviderRequest, provider_event
from harness.session_runtime import SessionPromptExecution, SessionRuntimeManager


runner = CliRunner()


class EchoRuntimeProvider:
    def complete(self, execution: SessionPromptExecution) -> str:
        return f"echo: {execution.content}"


class DefaultProviderAdapter:
    provider_id = "default_test_provider"
    model_ref = "default/test"
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
            text="default provider response",
            payload={"delta": "default provider response"},
        )
        yield provider_event(ProviderEventKind.MODEL_COMPLETED, sequence=3, request=request, provider_id=self.provider_id, model_ref=self.model_ref)


class AppLocalDiscoveryHttpClient:
    def __init__(self) -> None:
        self.gets: list[dict] = []

    def get_json(self, url: str, headers: dict[str, str], timeout: float) -> dict:
        self.gets.append({"url": url, "headers": headers, "timeout": timeout})
        return {"data": [{"id": "app-model"}]}

    def post_json(self, url: str, headers: dict[str, str], payload: dict, timeout: float) -> dict:
        raise AssertionError("local model discovery must not execute a provider prompt")


class AppHostedOpenAIHttpClient:
    def __init__(self) -> None:
        self.streams: list[dict] = []

    def get_json(self, url: str, headers: dict[str, str], timeout: float) -> dict:
        raise AssertionError("hosted app execution test must not perform discovery")

    def post_json(self, url: str, headers: dict[str, str], payload: dict, timeout: float) -> dict:
        raise AssertionError("hosted app execution test uses streaming chat completions")

    def stream_sse_json(self, url: str, headers: dict[str, str], payload: dict, timeout: float):
        self.streams.append({"url": url, "headers": headers, "payload": payload, "timeout": timeout})
        yield {"choices": [{"delta": {"content": "hosted app response"}, "finish_reason": None}]}
        yield {
            "choices": [{"delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
        }


class AppLocalProtocolAdapter:
    protocol = "openai_chat"

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def stream(self, provider, model, request):
        self.calls.append((provider, model, request))
        yield provider_event(
            ProviderEventKind.MODEL_STARTED,
            sequence=1,
            request=request,
            provider_id=provider.provider_id,
            model_ref=model.raw_model_ref,
        )
        yield provider_event(
            ProviderEventKind.MODEL_MESSAGE_DELTA,
            sequence=2,
            request=request,
            provider_id=provider.provider_id,
            model_ref=model.raw_model_ref,
            text="local app response",
            payload={"delta": "local app response"},
        )
        yield provider_event(
            ProviderEventKind.MODEL_COMPLETED,
            sequence=3,
            request=request,
            provider_id=provider.provider_id,
            model_ref=model.raw_model_ref,
        )


def test_app_service_uninitialized_read_projections_do_not_create_state(tmp_path) -> None:
    service = HarnessAppService(tmp_path)

    health = service.health()
    dashboard = service.dashboard()
    sessions = service.list_sessions()
    pane = service.session_pane(selected_session_id=None, status_filter="open", query="")

    assert health["schema_version"] == "harness.app_service/v1"
    assert health["initialized"] is False
    assert dashboard == build_tui_dashboard(tmp_path)
    assert dashboard["initialized"] is False
    assert sessions["ok"] is False
    assert sessions["error_code"] == "project_uninitialized"
    assert sessions["sessions"] == []
    assert pane["ok"] is False
    assert not (tmp_path / ".harness").exists()


def test_app_service_dashboard_and_session_pane_match_existing_projection_builders(tmp_path) -> None:
    store = SQLiteStore.open_initialized(tmp_path)
    session = store.create_session(title="Inspect dynamic app service", intent="test")
    store.append_session_message(session.id, SessionMessageRole.USER, "Inspect dynamic app service")
    service = HarnessAppService(tmp_path, store=store)

    service_dashboard = service.dashboard(selected_session_id=session.id)
    direct_dashboard = build_tui_dashboard(
        tmp_path,
        selected_session_id=session.id,
    )
    assert service_dashboard["schema_version"] == direct_dashboard["schema_version"]
    assert service_dashboard["active_session"]["id"] == session.id
    assert service_dashboard["recent_sessions"][0]["id"] == session.id
    assert service_dashboard["live_activity"]["policy_boundary"] == direct_dashboard["live_activity"]["policy_boundary"]
    assert service.session_pane(
        selected_session_id=session.id,
        status_filter="open",
        query="dynamic",
    ) == build_session_pane_projection(
        tmp_path,
        selected_session_id=session.id,
        status_filter="open",
        query="dynamic",
    )


def test_app_service_session_messages_events_and_status_match_local_server_payloads(tmp_path) -> None:
    store = SQLiteStore.open_initialized(tmp_path)
    write_default_config(tmp_path)
    session = store.create_session(title="Backend TUI wiring")
    message = store.append_session_message(session.id, SessionMessageRole.USER, "Wire backend to TUI")
    event = store.append_store_event(
        EventStreamType.SESSION,
        session.id,
        "test.event",
        {"summary": "event for service parity"},
        session_id=session.id,
        message_id=message.id,
        redaction_state=RedactionState.NOT_REQUIRED,
    )
    cfg = load_config(tmp_path)
    service = HarnessAppService(tmp_path, store=store)

    route_messages = _route_get(
        f"/sessions/{session.id}/messages",
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    route_events = _route_get(
        f"/sessions/{session.id}/events",
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    route_status = _route_get(
        f"/sessions/{session.id}/status",
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )

    service_messages = service.list_messages(session.id)
    service_events = service.list_events(session.id)
    service_status = service.session_status(session.id)

    assert service_messages["schema_version"] == route_messages["schema_version"]
    assert service_messages["messages"] == route_messages["messages"]
    assert service_messages["parts"] == route_messages["parts"]
    assert service_events["schema_version"] == route_events["schema_version"]
    assert service_events["events"] == route_events["events"]
    assert any(item["id"] == event.id for item in service_events["events"])
    assert service_status["schema_version"] == route_status["schema_version"]
    assert service_status["session_id"] == route_status["session_id"]
    assert service_status["runtime"] == route_status["runtime"]
    assert service_status["permission_granting"] is False


def test_http_app_service_attaches_to_server_reads_sessions_and_streams_events(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    session = store.create_session(title="Attached TUI service")
    store.append_session_message(session.id, SessionMessageRole.USER, "attached transcript")
    server = create_local_http_server(tmp_path, host="127.0.0.1", port=0, token="attach-token")
    host, port = server.server_address
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    service = HarnessHTTPAppService.from_attach(f"http://{host}:{port}", "attach-token")
    subscription = None

    try:
        health = service.health()
        dashboard = service.dashboard(selected_session_id=session.id)
        pane = service.session_pane(selected_session_id=session.id, status_filter="open", query="attached")
        sessions = service.list_sessions()
        messages = service.list_messages(session.id)
        subscription = service.subscribe_session_events(session.id)
        store.append_store_event(
            EventStreamType.SESSION,
            session.id,
            "test.http_attached_stream",
            {"summary": "attached TUI streamed this event"},
            session_id=session.id,
            redaction_state=RedactionState.NOT_REQUIRED,
        )

        delivered = None
        for _ in range(20):
            candidate = subscription.next(timeout=0.25)
            if candidate is None:
                continue
            if candidate.kind == "test.http_attached_stream":
                delivered = candidate
                break

        assert health["schema_version"] == "harness.global_health/v1"
        assert dashboard["schema_version"] == "harness.tui_dashboard/v1"
        assert dashboard["active_session"]["id"] == session.id
        assert pane["schema_version"] == "harness.session_pane/v1"
        assert pane["selected_session_id"] == session.id
        assert sessions["sessions"][0]["id"] == session.id
        assert messages["messages"][0]["id"]
        assert delivered is not None
        assert delivered.session_id == session.id
    finally:
        if subscription is not None:
            subscription.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_http_app_service_attach_fails_closed_for_missing_and_bad_tokens(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    server = create_local_http_server(tmp_path, host="127.0.0.1", port=0, token="attach-token")
    host, port = server.server_address
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        try:
            HarnessHTTPAppService.from_attach(f"http://{host}:{port}", None)
        except ValueError as exc:
            assert "Missing server token" in str(exc)
        else:
            raise AssertionError("missing token should fail before attached reads")

        try:
            HarnessHTTPAppService.from_attach(f"http://{host}:{port}", "wrong-token")
        except HarnessHTTPServiceError as exc:
            assert exc.status == 401
            assert exc.error_code == "unauthorized"
        else:
            raise AssertionError("bad token should fail health probe")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_app_service_permission_question_settings_and_event_subscription(tmp_path) -> None:
    store = SQLiteStore.open_initialized(tmp_path)
    session = store.create_session(title="Permission cards")
    permission = store.request_session_permission(
        session.id,
        tool_id="shell",
        normalized_action="run",
        normalized_target_pattern="pytest",
        boundary_kind=SessionPermissionBoundaryKind.LOCAL_ONLY,
        risk="medium",
        scope=SessionPermissionScope.ONCE,
        source=SessionPermissionSource.POLICY,
        policy_reasons=["operator approval required"],
    )
    question = store.append_session_question(session.id, "Which test should run?", choices=["unit", "smoke"])
    service = HarnessAppService(tmp_path, store=store)

    global_permissions = service.list_permissions()
    session_permissions = service.list_permissions(session.id)
    questions = service.list_questions(session.id)
    settings = service.settings_tui(session.id)
    existing_events = store.list_store_events(EventStreamType.SESSION, session.id)
    after_seq = max((event.seq for event in existing_events), default=0)
    subscription = service.subscribe_session_events(session.id, after_seq=after_seq)
    store.append_store_event(
        EventStreamType.SESSION,
        session.id,
        "test.subscription",
        {"summary": "subscription replay and live event"},
        session_id=session.id,
        redaction_state=RedactionState.NOT_REQUIRED,
    )
    delivered = subscription.next(timeout=0.2)
    subscription.close()

    assert global_permissions["pending_count"] == 1
    assert global_permissions["permissions"][0]["id"] == permission.id
    assert global_permissions["permissions"][0]["approval_card"]
    assert session_permissions["snapshot"]["blocked_on_permission"] is True
    assert session_permissions["snapshot"]["pending_permission_ids"] == [permission.id]
    assert questions["questions"][0]["id"] == question.id
    assert settings["schema_version"] == "harness.tui_settings/v1"
    assert settings["permission_granting"] is False
    assert delivered is not None
    assert delivered.kind == "test.subscription"


def test_app_service_permission_reply_resolves_store_and_runtime(tmp_path) -> None:
    store = SQLiteStore.open_initialized(tmp_path)
    session = store.create_session(title="Permission reply")
    permission = store.request_session_permission(
        session.id,
        tool_id="shell",
        normalized_action="run",
        normalized_target_pattern='{"command": "pytest"}',
        boundary_kind=SessionPermissionBoundaryKind.LOCAL_ONLY,
        risk="medium",
        scope=SessionPermissionScope.ONCE,
        source=SessionPermissionSource.POLICY,
        policy_reasons=["operator approval required"],
    )
    runtime = SessionRuntimeManager.for_store(store)
    runtime.wait_for_permission(session.id, permission.id)
    service = HarnessAppService(tmp_path, store=store, runtime=runtime)

    reply = service.reply_permission(session.id, permission.id, {"reply": "once", "reason": "approved in TUI"})

    assert reply["schema_version"] == "harness.session_permission_reply/v1"
    assert reply["decision"] == "allowed"
    assert reply["permission_granting"] is True
    assert reply["execution_started"] is False
    assert reply["tool_execution_started"] is False
    assert reply["snapshot"]["pending_count"] == 0
    assert store.get_session_permission(permission.id).status.value == "allowed"

    event_kinds = [event.kind for event in store.list_session_store_events(session.id)]
    assert "permission.resolved" in event_kinds
    assert "harness.runtime.permission_resolved" in event_kinds
    assert "harness.turn.finished" in event_kinds


def test_app_service_permission_deny_and_cancel_do_not_resume_as_success(tmp_path) -> None:
    store = SQLiteStore.open_initialized(tmp_path)
    first = store.create_session(title="Permission deny")
    denied = store.request_session_permission(
        first.id,
        tool_id="shell",
        normalized_action="run",
        normalized_target_pattern="pytest",
        boundary_kind=SessionPermissionBoundaryKind.LOCAL_ONLY,
        risk="medium",
        scope=SessionPermissionScope.ONCE,
        source=SessionPermissionSource.POLICY,
    )
    service = HarnessAppService(tmp_path, store=store)

    deny_reply = service.reply_permission(first.id, denied.id, {"reply": "deny"})
    assert deny_reply["decision"] == "denied"
    assert deny_reply["permission_granting"] is False
    assert deny_reply["execution_started"] is False
    assert store.get_session_permission(denied.id).status.value == "denied"

    second = store.create_session(title="Permission cancel")
    cancelled = store.request_session_permission(
        second.id,
        tool_id="shell",
        normalized_action="run",
        normalized_target_pattern="pytest",
        boundary_kind=SessionPermissionBoundaryKind.LOCAL_ONLY,
        risk="medium",
        scope=SessionPermissionScope.ONCE,
        source=SessionPermissionSource.POLICY,
    )
    cancel_reply = service.reply_permission(second.id, cancelled.id, {"reply": "cancel"})
    assert cancel_reply["decision"] == "cancelled"
    assert cancel_reply["permission_granting"] is False
    assert cancel_reply["execution_started"] is False
    assert store.get_session_permission(cancelled.id).status.value == "cancelled"


def test_app_service_session_mutations_record_events_and_flags(tmp_path) -> None:
    store = SQLiteStore.open_initialized(tmp_path)
    write_default_config(tmp_path)
    service = HarnessAppService(tmp_path, store=store)

    created = service.create_session(
        {
            "title": "New service session",
            "intent": "test_session_mutations",
            "metadata": {"cwd": "."},
        }
    )
    session_id = created["session_id"]
    renamed = service.update_session_title(session_id, "Renamed service session")
    agent = service.update_session_agent(session_id, "plan", source="test")
    selected = service.update_session_model_selection(session_id, "codex_cli/gpt-5.5", source="test")
    invalid = service.update_session_model_selection(session_id, "codex_cli/not-a-real-model", source="test")
    forked = service.fork_session(session_id, {"title": "Forked service session"})
    archived = service.archive_session(session_id)
    restored = service.restore_session(session_id)
    aborted = service.abort_session(session_id, {"reason": "test abort"})

    assert created["session_created"] is True
    assert created["permission_granting"] is False
    assert renamed["title_updated"] is True
    assert store.get_session(session_id).agent_id == "plan"
    assert agent["agent_updated"] is True
    assert selected["session_model_selected"] is True
    assert selected["provider_execution_started"] is False
    assert selected["hidden_model_fallback"] is False
    assert invalid["ok"] is False
    assert invalid["session_model_selected"] is False
    assert invalid["blocked_reasons"] == ["model_unknown"]
    assert invalid["suggestions"]
    assert invalid["model_validation"]["suggestions"] == invalid["suggestions"]
    assert all(item["suggestion_only"] is True for item in invalid["suggestions"])
    assert all(item["selected_model"] is False for item in invalid["suggestions"])
    assert all(item["provider_execution_started"] is False for item in invalid["suggestions"])
    assert forked["child"]["parent_session_id"] == session_id
    assert archived["archived"] is True
    assert restored["restored"] is True
    assert aborted["process_stopped"] is False
    assert aborted["permission_granting"] is False

    event_kinds = [event.kind for event in store.list_session_store_events(session_id)]
    assert "session.created" in event_kinds
    assert "session.title_updated" in event_kinds
    assert "agent.selected" in event_kinds
    assert event_kinds.count("session.model_validation") == 2
    assert "session.archived" in event_kinds
    assert "session.restored" in event_kinds
    assert "session.cancelled" in event_kinds

    deleted = service.hard_delete_session(forked["child_session_id"])
    assert deleted["hard_deleted"] is True
    assert deleted["counts"]["session_rows"] == 1
    assert deleted["active_repo_modified"] is False


def test_app_service_exposes_stable_model_provider_api_surface(tmp_path) -> None:
    store = SQLiteStore.open_initialized(tmp_path)
    write_default_config(tmp_path)
    service = HarnessAppService(tmp_path, store=store)

    providers = service.list_providers()
    provider = service.provider_detail("codex_cli")
    models = service.list_models()
    model = service.model_detail("codex_cli", "gpt-5.5")
    validation = service.validate_model("codex_cli/gpt-5.5")
    favorite = service.set_model_favorite("codex_cli/gpt-5.5", True)
    default = service.set_default_model_preference("codex_cli/gpt-5.5")
    preferences = service.model_preferences()
    providers_after_default = service.list_providers()
    models_after_default = service.list_models()
    auth_methods = service.provider_auth_methods()

    assert providers["schema_version"] == "harness.providers/v1"
    assert provider["provider"]["provider_id"] == "codex_cli"
    assert models["schema_version"] == "harness.models/v1"
    assert model["schema_version"] == "harness.model_detail/v1"
    assert model["raw_model_ref"] == "codex_cli/gpt-5.5"
    assert validation["validation"]["executable"] is True
    assert favorite["preference"]["favorite"] is True
    assert default["preference"]["is_default"] is True
    assert preferences["default_preference"]["raw_model_ref"] == "codex_cli/gpt-5.5"
    assert preferences["provider_execution_started"] is False
    assert preferences["model_execution_started"] is False
    assert model["provider_execution_started"] is False
    assert model["model_execution_started"] is False
    assert providers_after_default["default"]["provider_id"] == "codex_cli"
    assert providers_after_default["distinctions"]["default"] == "codex_cli"
    assert "codex_cli" in providers_after_default["distinctions"]["connected"]
    assert all(item["is_connected"] for item in providers_after_default["connected"])
    assert all(item["is_blocked"] for item in providers_after_default["blocked"])
    assert providers_after_default["oauth_support"]["paid_openai_compatible"] is True
    assert "oauth" in providers_after_default["methods_by_provider"]["paid_openai_compatible"]
    assert models_after_default["default"]["raw_model_ref"] == "codex_cli/gpt-5.5"
    assert models_after_default["distinctions"]["default"] == "codex_cli/gpt-5.5"
    assert "codex_cli/gpt-5.5" in models_after_default["distinctions"]["connected"]
    assert all(item["is_connected"] for item in models_after_default["connected"])
    assert all(item["is_blocked"] for item in models_after_default["blocked"])
    assert models_after_default["default"]["provider_oauth_supported"] is False
    assert "codex_login" in models_after_default["default"]["provider_auth_methods"]
    assert auth_methods["oauth_support"]["paid_openai_compatible"] is True
    assert "oauth" in auth_methods["auth_methods"]


def test_app_service_local_provider_can_refresh_select_and_execute(tmp_path, monkeypatch) -> None:
    store = SQLiteStore.open_initialized(tmp_path)
    write_default_config(tmp_path)
    (tmp_path / ".harness" / "models.yaml").write_text(
        """
providers:
  app_local:
    display_name: App Local
    enabled: true
    data_boundary: local_only
    base_url: http://localhost:11434/v1
    protocol: openai_chat
    credential:
      kind: static_local
    models:
      app-model:
        display_name: App Model
        api_id: app-model
        context_window: 8192
        max_output_tokens: 1024
        input_modalities: [text]
        output_modalities: [text]
        tool_support: true
        status: active
""".lstrip(),
        encoding="utf-8",
    )
    discovery_client = AppLocalDiscoveryHttpClient()
    monkeypatch.setattr(model_discovery, "UrllibOpenAICompatibleHttpClient", lambda: discovery_client)
    protocol_adapter = AppLocalProtocolAdapter()
    registry = ProtocolAdapterRegistry()
    registry.register(protocol_adapter)
    runtime = SessionRuntimeManager.for_store(store, protocol_adapter_registry=registry)
    service = HarnessAppService(tmp_path, store=store, runtime=runtime)
    created = service.create_session({"title": "Local provider app path"})

    refreshed = service.refresh_provider_models("app_local")
    models = service.list_models()
    selected = service.update_session_model_selection(created["session_id"], "app_local/app-model", source="test_app_local")
    response = service.prompt_async(
        created["session_id"],
        {
            "content": "Use the local provider.",
            "source": "test_app_local_prompt",
        },
    )
    final = runtime.wait(created["session_id"], timeout=2.0)

    assert refreshed["ok"] is True
    assert refreshed["provider_id"] == "app_local"
    assert refreshed["source"] == "discovered"
    assert refreshed["network_accessed"] is True
    assert refreshed["credentials_included"] is False
    assert refreshed["provider_execution_started"] is False
    assert refreshed["model_execution_started"] is False
    assert refreshed["models"][0]["raw_model_ref"] == "app_local/app-model"
    assert discovery_client.gets == [
        {
            "url": "http://localhost:11434/v1/models",
            "headers": {"Content-Type": "application/json"},
            "timeout": 300.0,
        }
    ]
    cached_discovered = [
        model
        for model in models["models"]
        if model["raw_model_ref"] == "app_local/app-model" and model["source"] == "discovered"
    ]
    assert len(cached_discovered) == 1
    assert cached_discovered[0]["discovery_metadata"]["cache_status"] == "fresh"
    assert "app_local/app-model" in models["distinctions"]["connected"]
    assert selected["ok"] is True
    assert selected["session_model_selected"] is True
    assert selected["model_validation"]["provider_id"] == "app_local"
    assert selected["model_validation"]["model_id"] == "app-model"
    assert selected["model_validation"]["executable"] is True
    assert selected["provider_execution_started"] is False
    assert response["accepted"] is True
    assert response["execution_started"] is True
    assert final.phase.value == "idle"
    provider, model, request = protocol_adapter.calls[0]
    assert provider.provider_id == "app_local"
    assert model.raw_model_ref == "app_local/app-model"
    assert request.model_ref == "app_local/app-model"
    assert request.metadata["canonical_model_ref"] == "app_local/app-model"
    assert request.metadata["protocol"] == "openai_chat"
    messages = store.list_session_messages(created["session_id"])
    assert [message.role.value for message in messages] == ["user", "assistant"]
    assert messages[-1].content_preview == "local app response"
    event_kinds = [event.kind for event in store.list_session_store_events(created["session_id"])]
    assert "session.model_resolution" in event_kinds
    assert "session.model_validation" in event_kinds
    assert "model.started" in event_kinds
    assert "model.completed" in event_kinds


def test_app_service_hosted_api_key_provider_can_connect_select_and_execute_with_approval(tmp_path) -> None:
    store = SQLiteStore.open_initialized(tmp_path)
    write_default_config(tmp_path)
    (tmp_path / ".harness" / "models.yaml").write_text(
        """
providers:
  app_hosted:
    display_name: App Hosted
    enabled: true
    approved: true
    data_boundary: hosted_provider
    base_url: https://api.example.com/v1
    protocol: openai_chat
    credential:
      kind: api_key
    models:
      hosted-model:
        display_name: Hosted Model
        api_id: hosted-model
        context_window: 8192
        max_output_tokens: 1024
        input_modalities: [text]
        output_modalities: [text]
        tool_support: true
        status: active
""".lstrip(),
        encoding="utf-8",
    )
    http_client = AppHostedOpenAIHttpClient()
    registry = ProtocolAdapterRegistry()
    registry.register(OpenAIChatProtocolAdapter(http_client=http_client))
    runtime = SessionRuntimeManager.for_store(store, protocol_adapter_registry=registry)
    service = HarnessAppService(tmp_path, store=store, runtime=runtime)

    connected = service.connect_provider_api_key("app_hosted", "sk-hosted-app-secret", description="test hosted app")
    hosted_approval = ApprovalStore(tmp_path).add("app_hosted", "hosted_provider", ["session_provider_execution"], 1)
    paid_approval = ApprovalStore(tmp_path).add("app_hosted", "paid_provider", ["session_provider_execution"], 1)
    created = service.create_session({"title": "Hosted provider app path"})
    selected = service.update_session_model_selection(created["session_id"], "app_hosted/hosted-model", source="test_app_hosted")
    response = service.prompt_async(
        created["session_id"],
        {
            "content": "Use the hosted provider.",
            "source": "test_app_hosted_prompt",
        },
    )
    final = runtime.wait(created["session_id"], timeout=2.0)

    assert connected["ok"] is True
    assert connected["provider_id"] == "app_hosted"
    assert connected["credential_written"] is True
    assert connected["credential_value_included"] is False
    assert connected["credentials_included"] is False
    assert connected["account"]["credential_kind"] == "api_key"
    assert connected["account"]["status"] == "configured"
    assert "sk-hosted-app-secret" not in json.dumps(connected)
    assert selected["ok"] is True
    assert selected["session_model_selected"] is True
    assert selected["model_validation"]["provider_id"] == "app_hosted"
    assert selected["model_validation"]["model_id"] == "hosted-model"
    assert selected["model_validation"]["executable"] is True
    assert response["accepted"] is True
    assert response["execution_started"] is True
    assert final.phase.value == "idle"
    assert http_client.streams == [
        {
            "url": "https://api.example.com/v1/chat/completions",
            "headers": {"Content-Type": "application/json", "Authorization": "Bearer sk-hosted-app-secret"},
            "payload": {
                "model": "hosted-model",
                "messages": [{"role": "user", "content": "Use the hosted provider."}],
                "temperature": 0.2,
                "max_tokens": 4096,
                "stream": True,
            },
            "timeout": 300.0,
        }
    ]
    events = store.list_session_store_events(created["session_id"])
    validation = [event for event in events if event.kind == "session.model_validation"][-1]
    assert validation.payload["runtime_hosted_provider_policy"]["approval_id"] == hosted_approval.id
    assert validation.payload["runtime_hosted_provider_policy"]["approved"] is True
    assert validation.payload["runtime_hosted_provider_policy"]["provider_execution_started"] is False
    assert validation.payload["runtime_hosted_provider_policy"]["network_accessed"] is False
    assert validation.payload["runtime_paid_provider_policy"]["approval_id"] == paid_approval.id
    assert validation.payload["provider_credential"]["credential_kind"] == "api_key"
    assert validation.payload["provider_credential"]["credentials_included"] is False
    started = [event for event in events if event.kind == "model.started"][-1]
    assert started.payload["provider_id"] == "app_hosted"
    assert started.payload["model_ref"] == "app_hosted/hosted-model"
    assert started.payload["provider_credential"]["credentials_included"] is False
    messages = store.list_session_messages(created["session_id"])
    assert [message.role.value for message in messages] == ["user", "assistant"]
    assert messages[-1].content_preview == "hosted app response"
    assert "sk-hosted-app-secret" not in json.dumps([event.payload for event in events])


def test_app_service_missing_hosted_credential_blocks_before_network(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("APP_HOSTED_API_KEY", raising=False)
    store = SQLiteStore.open_initialized(tmp_path)
    write_default_config(tmp_path)
    (tmp_path / ".harness" / "models.yaml").write_text(
        """
providers:
  app_hosted:
    display_name: App Hosted
    enabled: true
    approved: true
    data_boundary: hosted_provider
    base_url: https://api.example.com/v1
    protocol: openai_chat
    credential:
      kind: env
      env_var: APP_HOSTED_API_KEY
    models:
      hosted-model:
        display_name: Hosted Model
        api_id: hosted-model
        context_window: 8192
        max_output_tokens: 1024
        input_modalities: [text]
        output_modalities: [text]
        tool_support: true
        status: active
""".lstrip(),
        encoding="utf-8",
    )
    http_client = AppHostedOpenAIHttpClient()
    registry = ProtocolAdapterRegistry()
    registry.register(OpenAIChatProtocolAdapter(http_client=http_client))
    runtime = SessionRuntimeManager.for_store(store, protocol_adapter_registry=registry)
    service = HarnessAppService(tmp_path, store=store, runtime=runtime)

    hosted_approval = ApprovalStore(tmp_path).add("app_hosted", "hosted_provider", ["session_provider_execution"], 1)
    paid_approval = ApprovalStore(tmp_path).add("app_hosted", "paid_provider", ["session_provider_execution"], 1)
    created = service.create_session({"title": "Missing hosted credential"})
    selected = service.update_session_model_selection(created["session_id"], "app_hosted/hosted-model", source="test_missing_credential")
    response = service.prompt_async(
        created["session_id"],
        {"content": "Do not call the provider.", "raw_model_ref": "app_hosted/hosted-model"},
    )
    final = runtime.wait(created["session_id"], timeout=2.0)

    assert selected["ok"] is False
    assert selected["session_model_selected"] is False
    assert selected["model_validation"]["executable"] is False
    assert selected["model_validation"]["blocked_reasons"] == ["credential_missing"]
    assert response["accepted"] is True
    assert final.phase.value == "failed"
    assert http_client.streams == []
    events = store.list_session_store_events(created["session_id"])
    assert "model.started" not in [event.kind for event in events]
    validation = [event for event in events if event.kind == "session.model_validation"][-1]
    assert validation.payload["runtime_hosted_provider_policy"]["approval_id"] == hosted_approval.id
    assert validation.payload["runtime_paid_provider_policy"]["approval_id"] == paid_approval.id
    assert validation.payload["provider_credential"]["blocked_reasons"] == ["credential_missing"]
    assert validation.payload["provider_credential"]["network_accessed"] is False
    failed = [event for event in events if event.kind == "model.failed"][-1]
    assert failed.payload["provider_execution_started"] is False
    assert failed.payload["model_execution_started"] is False
    assert failed.payload["network_accessed"] is False
    assert failed.payload["error"]["error_type"] == "ProviderCredentialResolutionError"


def test_app_service_oauth_provider_can_connect_refresh_select_and_execute_with_redacted_evidence(tmp_path, monkeypatch) -> None:
    store = SQLiteStore.open_initialized(tmp_path)
    write_default_config(tmp_path)
    (tmp_path / ".harness" / "models.yaml").write_text(
        """
providers:
  paid_openai_compatible:
    override: true
    display_name: OAuth Hosted
    enabled: true
    approved: true
    data_boundary: hosted_provider
    base_url: https://api.example.com/v1
    protocol: openai_chat
    credential:
      kind: oauth
    models:
      oauth-model:
        display_name: OAuth Model
        api_id: oauth-model
        context_window: 8192
        max_output_tokens: 1024
        input_modalities: [text]
        output_modalities: [text]
        tool_support: true
        status: active
""".lstrip(),
        encoding="utf-8",
    )
    old_access_token = "oauth-old-access-secret"
    refresh_token = "oauth-refresh-secret"
    new_access_token = "oauth-new-access-secret"
    new_refresh_token = "oauth-new-refresh-secret"
    expired = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    http_client = AppHostedOpenAIHttpClient()
    registry = ProtocolAdapterRegistry()
    registry.register(OpenAIChatProtocolAdapter(http_client=http_client))
    runtime = SessionRuntimeManager.for_store(store, protocol_adapter_registry=registry)
    service = HarnessAppService(tmp_path, store=store, runtime=runtime)
    refresh_calls: list[dict] = []

    def fake_refresh(project_root, provider_id, account, token):
        refresh_calls.append(
            {
                "project_root": project_root,
                "provider_id": provider_id,
                "account_id": account["account_id"],
                "refresh_token": token,
            }
        )
        return {
            "access_token": new_access_token,
            "refresh_token": new_refresh_token,
            "expires_at": future,
            "network_accessed": True,
        }

    monkeypatch.setattr("harness.provider_auth.refresh_provider_oauth_account", fake_refresh)

    authorized = service.provider_oauth_authorize("paid_openai_compatible", {"scopes": ["models.read"]})
    callback = service.provider_oauth_callback(
        "paid_openai_compatible",
        {
            "access_token": old_access_token,
            "refresh_token": refresh_token,
            "expires_in": 3600,
            "scopes": ["models.read"],
            "description": "test oauth app",
        },
    )
    created = service.create_session({"title": "OAuth provider app path"})
    selected = service.update_session_model_selection(created["session_id"], "paid_openai_compatible/oauth-model", source="test_app_oauth")
    with store.connect() as conn:
        conn.execute(
            "UPDATE provider_accounts SET expires_at = ? WHERE account_id = ?",
            (expired, callback["account_id"]),
        )
    hosted_approval = ApprovalStore(tmp_path).add("paid_openai_compatible", "hosted_provider", ["session_provider_execution"], 1)
    paid_approval = ApprovalStore(tmp_path).add("paid_openai_compatible", "paid_provider", ["session_provider_execution"], 1)
    response = service.prompt_async(
        created["session_id"],
        {
            "content": "Use the OAuth provider.",
            "source": "test_app_oauth_prompt",
        },
    )
    final = runtime.wait(created["session_id"], timeout=2.0)

    assert authorized["ok"] is True
    assert authorized["oauth_supported"] is True
    assert authorized["credential_value_included"] is False
    assert authorized["credentials_included"] is False
    assert authorized["network_called"] is False
    assert callback["ok"] is True
    assert callback["action"] == "oauth_callback"
    assert callback["credential_written"] is True
    assert callback["credential_value_included"] is False
    assert callback["credentials_included"] is False
    assert callback["account"]["credential_kind"] == "oauth"
    assert old_access_token not in json.dumps(callback)
    assert refresh_token not in json.dumps(callback)
    assert selected["ok"] is True
    assert selected["session_model_selected"] is True
    assert selected["model_validation"]["provider_id"] == "paid_openai_compatible"
    assert selected["model_validation"]["model_id"] == "oauth-model"
    assert response["accepted"] is True
    assert response["execution_started"] is True
    assert final.phase.value == "idle"
    assert refresh_calls == [
        {
            "project_root": tmp_path,
            "provider_id": "paid_openai_compatible",
            "account_id": callback["account_id"],
            "refresh_token": refresh_token,
        }
    ]
    assert http_client.streams == [
        {
            "url": "https://api.example.com/v1/chat/completions",
            "headers": {"Content-Type": "application/json", "Authorization": f"Bearer {new_access_token}"},
            "payload": {
                "model": "oauth-model",
                "messages": [{"role": "user", "content": "Use the OAuth provider."}],
                "temperature": 0.2,
                "max_tokens": 4096,
                "stream": True,
            },
            "timeout": 300.0,
        }
    ]
    events = store.list_session_store_events(created["session_id"])
    validation = [event for event in events if event.kind == "session.model_validation"][-1]
    assert validation.payload["runtime_hosted_provider_policy"]["approval_id"] == hosted_approval.id
    assert validation.payload["runtime_paid_provider_policy"]["approval_id"] == paid_approval.id
    assert validation.payload["provider_credential"]["credential_kind"] == "oauth"
    assert validation.payload["provider_credential"]["source"] == "provider_account_oauth_refreshed"
    assert validation.payload["provider_credential"]["credentials_included"] is False
    refresh_events = store.list_store_events("orchestration", "provider_accounts")
    assert any(event.kind == "provider.oauth_token_refreshed" and event.payload["network_accessed"] is True for event in refresh_events)
    messages = store.list_session_messages(created["session_id"])
    assert [message.role.value for message in messages] == ["user", "assistant"]
    assert messages[-1].content_preview == "hosted app response"
    serialized_events = json.dumps([event.payload for event in events + refresh_events])
    assert old_access_token not in serialized_events
    assert refresh_token not in serialized_events
    assert new_access_token not in serialized_events
    assert new_refresh_token not in serialized_events


def test_provider_model_catalog_state_matches_cli_server_tui_and_runtime(tmp_path, monkeypatch) -> None:
    store = SQLiteStore.open_initialized(tmp_path)
    write_default_config(tmp_path)
    (tmp_path / ".harness" / "models.yaml").write_text(
        """
providers:
  parity_local:
    display_name: Parity Local
    enabled: true
    data_boundary: local_only
    base_url: http://localhost:11434/v1
    protocol: openai_chat
    credential:
      kind: static_local
    models:
      parity-model:
        display_name: Parity Model
        api_id: parity-model
        context_window: 8192
        max_output_tokens: 1024
        input_modalities: [text]
        output_modalities: [text]
        tool_support: true
        status: active
""".lstrip(),
        encoding="utf-8",
    )
    discovery_client = AppLocalDiscoveryHttpClient()
    monkeypatch.setattr(model_discovery, "UrllibOpenAICompatibleHttpClient", lambda: discovery_client)
    protocol_adapter = AppLocalProtocolAdapter()
    registry = ProtocolAdapterRegistry()
    registry.register(protocol_adapter)
    runtime = SessionRuntimeManager.for_store(store, protocol_adapter_registry=registry)
    service = HarnessAppService(tmp_path, store=store, runtime=runtime)
    cfg = load_config(tmp_path)

    service.refresh_provider_models("parity_local")
    created = service.create_session({"title": "Catalog parity"})
    service.set_model_favorite("parity_local/parity-model", True)
    service.set_default_model_preference("parity_local/parity-model")
    selected = service.update_session_model_selection(created["session_id"], "parity_local/parity-model", source="test_catalog_parity")
    response = service.prompt_async(
        created["session_id"],
        {"content": "Use the parity provider.", "source": "test_catalog_parity_prompt"},
    )
    final = runtime.wait(created["session_id"], timeout=2.0)

    cli_providers = json.loads(
        runner.invoke(app, ["providers", "list", "--project", str(tmp_path), "--output", "json"]).output
    )
    cli_models = json.loads(runner.invoke(app, ["models", "list", "--project", str(tmp_path), "--output", "json"]).output)
    server_providers = _route_get("/providers", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    server_models = _route_get("/models", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    service_providers = service.list_providers()
    service_models = service.list_models()
    dashboard = build_tui_dashboard(tmp_path, selected_session_id=created["session_id"])

    def provider_state(payload: dict) -> dict:
        return {
            item["provider_id"]: {
                "enabled": item["enabled"],
                "is_connected": item["is_connected"],
                "is_blocked": item["is_blocked"],
                "credential_status": item["credential_status"],
                "auth_methods": item["auth_methods"],
                "oauth_supported": item["oauth_supported"],
                "active_credential_kind": item["active_credential_kind"],
                "account_count": item["account_count"],
            }
            for item in payload["all"]
        }

    def model_state(payload: dict) -> dict:
        return {
            item["raw_model_ref"]: {
                "provider_id": item["provider_id"],
                "model_id": item["model_id"],
                "source": item["source"],
                "provider_connected": item["provider_connected"],
                "is_connected": item["is_connected"],
                "is_blocked": item["is_blocked"],
                "favorite": item["favorite"],
                "is_default": item["is_default"],
                "blocked_reasons": item["blocked_reasons"],
                "provider_credential_status": item["provider_credential_status"],
                "provider_active_credential_kind": item["provider_active_credential_kind"],
            }
            for item in payload["all"]
        }

    assert selected["ok"] is True
    assert response["accepted"] is True
    assert final.phase.value == "idle"
    assert provider_state(cli_providers) == provider_state(server_providers) == provider_state(service_providers)
    assert model_state(cli_models) == model_state(server_models) == model_state(service_models)
    assert cli_models["distinctions"] == server_models["distinctions"] == service_models["distinctions"]
    assert cli_providers["distinctions"] == server_providers["distinctions"] == service_providers["distinctions"]

    service_model = model_state(service_models)["parity_local/parity-model"]
    tui_model = next(model for model in dashboard["model_catalog"]["models"] if model["raw_model_ref"] == "parity_local/parity-model")
    assert dashboard["model_catalog"]["active_model"]["raw_model_ref"] == "parity_local/parity-model"
    assert tui_model["provider_connected"] == service_model["provider_connected"]
    assert tui_model["blocked_reasons"] == service_model["blocked_reasons"]
    assert tui_model["favorite"] == service_model["favorite"]
    assert tui_model["selected_model"] is True

    events = store.list_session_store_events(created["session_id"])
    resolution = [event for event in events if event.kind == "session.model_resolution"][-1].payload
    validation = [event for event in events if event.kind == "session.model_validation"][-1].payload
    assert resolution["raw_model_ref"] == service_model["provider_id"] + "/" + service_model["model_id"]
    assert resolution["canonical_model_ref"] == "parity_local/parity-model"
    assert validation["matched_model"]["raw_model_ref"] == "parity_local/parity-model"
    assert validation["matched_model"]["provider_connected"] == service_model["provider_connected"]
    assert validation["matched_model"]["blocked_reasons"] == service_model["blocked_reasons"]
    assert validation["provider_credential"]["status"] == service_model["provider_credential_status"]
    assert validation["provider_credential"]["credential_kind"] == "static_local"
    assert protocol_adapter.calls[0][0].provider_id == "parity_local"
    assert protocol_adapter.calls[0][1].raw_model_ref == "parity_local/parity-model"


def test_connect_provider_does_not_change_active_model(tmp_path, monkeypatch) -> None:
    store = SQLiteStore.open_initialized(tmp_path)
    write_default_config(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env-test")
    session = store.create_session(
        title="Active model stays put",
        raw_model_ref="codex_cli/gpt-5.5",
        provider_id="codex_cli",
        model_id="gpt-5.5",
    )
    service = HarnessAppService(tmp_path, store=store)

    result = service.connect_provider_env("paid_openai_compatible", "OPENAI_API_KEY", description="test")
    unchanged = store.get_session(session.id)

    assert result["ok"] is True
    assert result["account_created"] is True
    assert result["active_model_changed"] is False
    assert result["provider_execution_started"] is False
    assert result["model_execution_started"] is False
    assert unchanged.raw_model_ref == "codex_cli/gpt-5.5"
    assert unchanged.provider_id == "codex_cli"
    assert unchanged.model_id == "gpt-5.5"


def test_app_service_prompt_async_persists_user_message_and_starts_runtime(tmp_path) -> None:
    store = SQLiteStore.open_initialized(tmp_path)
    session = store.create_session(title="Runtime submit", raw_model_ref="codex_cli/gpt-5.5")
    runtime = SessionRuntimeManager.for_store(store, text_provider=EchoRuntimeProvider())
    service = HarnessAppService(tmp_path, store=store, runtime=runtime)

    response = service.prompt_async(
        session.id,
        {
            "content": "Explain the current state.",
            "agent_id": "plan",
            "raw_model_ref": "codex_cli/gpt-5.5",
            "source": "test_prompt_async",
        },
    )
    runtime.wait(session.id, timeout=2.0)

    assert response["schema_version"] == "harness.session_prompt_async/v1"
    assert response["accepted"] is True
    assert response["message"]["role"] == "user"
    assert response["part"]["text"] == "Explain the current state."
    assert response["runtime"]["accepted"] is True
    assert response["execution_started"] is True

    messages = store.list_session_messages(session.id)
    assert [message.role.value for message in messages] == ["user", "assistant"]
    assert messages[-1].content_preview == "echo: Explain the current state."
    event_kinds = [event.kind for event in store.list_session_store_events(session.id)]
    assert "harness.runtime.prompt_queued" in event_kinds
    assert "harness.turn.started" in event_kinds
    assert "model.message_delta" in event_kinds
    assert "harness.turn.finished" in event_kinds


def test_app_service_prompt_async_uses_configured_default_provider_adapter(tmp_path, monkeypatch) -> None:
    store = SQLiteStore.open_initialized(tmp_path)
    write_default_config(tmp_path)
    session = store.create_session(title="Default runtime provider")
    adapter = DefaultProviderAdapter()
    monkeypatch.setattr("harness.core_service.build_default_provider_adapter", lambda project_root: adapter)
    service = HarnessAppService(tmp_path, store=store)

    response = service.prompt_async(session.id, {"content": "Use the configured provider."})
    assert response["accepted"] is True
    assert response["execution_started"] is True
    assert service.runtime is not None
    service.runtime.wait(session.id, timeout=2.0)

    messages = store.list_session_messages(session.id)
    assert [message.role.value for message in messages] == ["user", "assistant"]
    assert messages[-1].content_preview == "default provider response"
    assert adapter.requests[0].messages[0].content == "Use the configured provider."
    assert [event.kind for event in store.list_session_store_events(session.id)].count("model.failed") == 0


def test_app_service_prompt_async_busy_session_queues_follow_up(tmp_path) -> None:
    store = SQLiteStore.open_initialized(tmp_path)
    session = store.create_session(title="Busy runtime")
    runtime = SessionRuntimeManager.for_store(store, text_provider=EchoRuntimeProvider())
    runtime.begin_turn(session.id, turn_id="turn_busy")
    service = HarnessAppService(tmp_path, store=store, runtime=runtime)

    response = service.prompt_async(session.id, {"content": "Queue this behind the running turn."})
    state = runtime.status(session.id)

    assert response["accepted"] is True
    assert response["runtime"]["queue_policy"] == "follow_up"
    assert response["runtime"]["queued"] is True
    assert response["execution_started"] is False
    assert state.queued_prompt_count == 1

    runtime.finish_turn(session.id)


def test_app_service_prompt_async_rejects_terminal_session_without_message_mutation(tmp_path) -> None:
    store = SQLiteStore.open_initialized(tmp_path)
    session = store.create_session(title="Terminal runtime")
    store.cancel_session(session.id, reason="test terminal")
    service = HarnessAppService(tmp_path, store=store)

    response = service.prompt_async(session.id, {"content": "Should not enqueue."})

    assert response["ok"] is False
    assert response["accepted"] is False
    assert response["error_code"] == "session_terminal"
    assert response["messages_mutated"] is False
    assert response["execution_started"] is False
    assert store.list_session_messages(session.id) == []


def test_core_service_dry_run_creates_task_lease_run_manifest_and_events(tmp_path) -> None:
    result = HarnessCoreService().start_goal(
        "smoke test core loop",
        mode="dry_run",
        project_root=tmp_path,
    )

    assert result.schema_version == "harness.core_run/v1"
    assert result.ok is True
    assert result.mode == "dry_run"
    assert result.decision == "dry_run_no_tool_execution"
    assert result.task_id
    assert result.lease_id
    assert result.run_id
    assert result.adapter_id == "dry_run"
    assert result.manifest is not None
    assert result.manifest.exists()
    assert result.errors == []
    assert result.summary is not None
    assert result.summary.event_count >= 1
    assert {"events", "transcript", "final_report", "manifest"} <= set(result.summary.artifact_kinds)
    assert any(command.startswith("harness core inspect-task ") for command in result.next_commands)

    store = SQLiteStore(tmp_path)
    task = store.get_task(result.task_id)
    lease = store.get_task_lease(result.lease_id)
    run = store.get_run(result.run_id)
    manifest = store.build_run_manifest(result.run_id)

    assert task.status.value == "succeeded"
    assert lease.status.value == "released"
    assert run.status == "completed"
    assert manifest.task_id == result.task_id
    assert manifest.run_id == result.run_id
    assert store.list_events(result.run_id)


def test_core_service_unsupported_mode_fails_closed_without_project_state(tmp_path) -> None:
    result = HarnessCoreService().start_goal(
        "try unsafe mode",
        mode="ambient_shell",
        project_root=tmp_path,
    )

    assert result.ok is False
    assert result.decision == "unsupported_mode"
    assert result.task_id is None
    assert result.lease_id is None
    assert result.run_id is None
    assert result.manifest is None
    assert "Unsupported core mode" in result.errors[0]
    assert not (tmp_path / ".harness").exists()


def test_core_service_repo_planning_without_hosted_approval_is_blocked(tmp_path) -> None:
    result = HarnessCoreService().start_goal(
        "plan a small change",
        mode="repo_planning",
        project_root=tmp_path,
    )

    assert result.ok is False
    assert result.mode == "repo_planning"
    assert result.decision == "execution_adapter_rejected"
    assert result.task_id
    assert result.lease_id
    assert result.run_id is None
    assert result.manifest is None
    assert result.adapter_id == "repo_planning"
    assert any("hosted_provider_codex" in error for error in result.errors)
    assert any(command.startswith("harness core inspect-task ") for command in result.next_commands)
    assert any(command.startswith("harness daemon inspect-lease ") for command in result.next_commands)
    assert SQLiteStore(tmp_path).list_runs() == []


def test_core_service_isolated_edit_without_hosted_approval_is_blocked(tmp_path) -> None:
    result = HarnessCoreService().start_goal(
        "edit a file",
        mode="codex_isolated_edit",
        project_root=tmp_path,
    )

    assert result.ok is False
    assert result.mode == "codex_isolated_edit"
    assert result.decision == "execution_adapter_rejected"
    assert result.task_id
    assert result.lease_id
    assert result.run_id is None
    assert result.manifest is None
    assert result.adapter_id == "codex_isolated_edit"
    assert any("hosted_provider_codex" in error for error in result.errors)
    assert any(command.startswith("harness core inspect-task ") for command in result.next_commands)
    assert any(command.startswith("harness daemon inspect-lease ") for command in result.next_commands)
    assert SQLiteStore(tmp_path).list_runs() == []


def test_core_service_final_summary_references_core_identifiers_and_errors(tmp_path) -> None:
    result = HarnessCoreService().start_goal(
        "smoke test core summary",
        mode="dry_run",
        project_root=tmp_path,
    )

    assert result.summary is not None
    text = result.summary.summary_text
    assert result.run_id in text
    assert result.task_id in text
    assert result.lease_id in text
    assert "adapter_id=dry_run" in text
    assert "decision=dry_run_no_tool_execution" in text
    assert str(result.manifest) in text
    assert "errors=none" in text

    blocked = HarnessCoreService().start_goal(
        "blocked summary",
        mode="repo_planning",
        project_root=tmp_path,
    )

    assert blocked.summary is not None
    blocked_text = blocked.summary.summary_text
    assert blocked.task_id in blocked_text
    assert blocked.lease_id in blocked_text
    assert "run_id=none" in blocked_text
    assert "adapter_id=repo_planning" in blocked_text
    assert "hosted_provider_codex" in blocked_text


def test_core_service_cli_json_matches_result_shape(tmp_path) -> None:
    result = runner.invoke(
        app,
        [
            "core",
            "run",
            "smoke test core loop",
            "--mode",
            "dry_run",
            "--project",
            str(tmp_path),
            "--output",
            "json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema_version"] == "harness.core_run/v1"
    for key in (
        "ok",
        "mode",
        "decision",
        "task_id",
        "lease_id",
        "run_id",
        "adapter_id",
        "manifest",
        "errors",
        "next_commands",
    ):
        assert key in payload
    assert payload["ok"] is True
    assert payload["mode"] == "dry_run"
    assert payload["decision"] == "dry_run_no_tool_execution"
    assert payload["adapter_id"] == "dry_run"
    assert payload["errors"] == []
    assert Path(payload["manifest"]).exists()
    assert payload["summary"]["run_id"] == payload["run_id"]
    assert payload["summary"]["task_id"] == payload["task_id"]
