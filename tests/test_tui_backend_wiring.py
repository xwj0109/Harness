from __future__ import annotations

import asyncio
import threading
import time

import pytest
import yaml

from harness.core_service import HarnessAppService, HarnessHTTPAppService
from harness.config import write_default_config
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
from harness.session_events import SessionEventKind, append_session_event
from harness.session_runtime import SessionRuntimeManager
from harness.session_tools import execute_session_tool
from harness.tui import (
    build_right_panel_model,
    build_tui_transcript_projection,
    create_harness_app,
    render_model_selection_dialog,
    render_permission_card_dialog,
    render_provider_auth_method_dialog,
)


class RecordingAppService:
    def __init__(self, project_root) -> None:
        self.project_root = project_root
        self.dashboard_calls: list[str | None] = []
        self.session_pane_calls: list[dict] = []

    def dashboard(self, *, selected_session_id: str | None = None) -> dict:
        self.dashboard_calls.append(selected_session_id)
        return build_tui_dashboard(self.project_root, selected_session_id=selected_session_id)

    def session_pane(
        self,
        *,
        selected_session_id: str | None,
        status_filter: str,
        query: str,
    ) -> dict:
        self.session_pane_calls.append(
            {
                "selected_session_id": selected_session_id,
                "status_filter": status_filter,
                "query": query,
            }
        )
        return build_session_pane_projection(
            self.project_root,
            selected_session_id=selected_session_id,
            status_filter=status_filter,
            query=query,
        )


class RecordingMutationService(RecordingAppService):
    def __init__(self, project_root) -> None:
        super().__init__(project_root)
        self.store = SQLiteStore.open_initialized(project_root)
        self.created_bodies: list[dict] = []
        self.prompt_async_calls: list[dict] = []
        self.runtime_by_session: dict[str, dict] = {}

    def create_session(self, body: dict) -> dict:
        self.created_bodies.append(body)
        session = self.store.create_session(
            title=body.get("title"),
            intent=body.get("intent"),
            metadata=body.get("metadata") or {},
        )
        return {
            "schema_version": "harness.session_create/v1",
            "ok": True,
            "session": session.model_dump(mode="json"),
            "session_id": session.id,
            "permission_granting": False,
        }

    def prompt_async(self, session_id: str, body: dict) -> dict:
        self.prompt_async_calls.append({"session_id": session_id, "body": body})
        runtime_state = {
            "schema_version": "harness.session_runtime_state/v1",
            "session_id": session_id,
            "phase": "running",
            "active_prompt_id": "prompt_test",
            "active_elapsed_seconds": 16,
            "queued_prompt_count": 0,
            "queued_prompt_ids": [],
            "execution_enabled": True,
            "process_running": True,
        }
        self.runtime_by_session[session_id] = runtime_state
        message = self.store.append_session_message(session_id, "user", body["content"], agent_id=body.get("agent_id"))
        part = self.store.append_session_part(
            session_id,
            message.id,
            "text",
            text=body["content"],
            metadata={"source": "test_prompt_async"},
        )
        return {
            "schema_version": "harness.session_prompt_async/v1",
            "ok": True,
            "accepted": True,
            "session_id": session_id,
            "message": message.model_dump(mode="json"),
            "part": part.model_dump(mode="json"),
            "runtime": {
                "schema_version": "harness.session_prompt_accepted/v1",
                "ok": True,
                "accepted": True,
                "session_id": session_id,
                "prompt_id": "prompt_test",
                "queued": False,
                "queue_policy": "follow_up",
                "phase": "running",
                "execution_started": True,
                "worker_started": True,
                "runtime": runtime_state,
            },
            "prompt_id": "prompt_test",
            "execution_started": True,
            "permission_granting": False,
        }

    def runtime_status(self, session_id: str) -> dict:
        runtime_state = self.runtime_by_session.get(
            session_id,
            {
                "schema_version": "harness.session_runtime_state/v1",
                "session_id": session_id,
                "phase": "idle",
                "queued_prompt_count": 0,
                "queued_prompt_ids": [],
                "execution_enabled": True,
                "process_running": False,
            },
        )
        return {
            "schema_version": "harness.session_runtime_status/v1",
            "ok": True,
            "session_id": session_id,
            "runtime": runtime_state,
            "execution_started": False,
            "permission_granting": False,
        }

    def list_messages(self, session_id: str, *, limit: int | None = None) -> dict:
        messages = self.store.list_session_messages(session_id)
        if limit is not None:
            messages = messages[-limit:] if limit else []
        return {
            "schema_version": "harness.session_messages/v1",
            "ok": True,
            "session_id": session_id,
            "messages": [message.model_dump(mode="json") for message in messages],
            "parts": {
                message.id: [
                    part.model_dump(mode="json")
                    for part in self.store.list_session_parts(session_id, message.id)
                ]
                for message in messages
            },
            "permission_granting": False,
        }

    def list_events(self, session_id: str, *, after_seq: int | None = None, limit: int | None = None) -> dict:
        events = self.store.list_store_events(EventStreamType.SESSION, session_id, after_seq=after_seq, limit=limit)
        return {
            "schema_version": "harness.session_events/v1",
            "ok": True,
            "session_id": session_id,
            "events": [event.model_dump(mode="json") for event in events],
            "permission_granting": False,
        }


class BrokenSubscription:
    closed = False

    def next(self, timeout=None):
        raise RuntimeError("stream closed")

    def close(self) -> None:
        self.closed = True


class BrokenEventService(RecordingAppService):
    def subscribe_global_events(self):
        return BrokenSubscription()

    def subscribe_session_events(self, session_id: str, *, after_seq=None):
        return BrokenSubscription()


def test_cli_tui_server_catalogs_share_active_registry_status(tmp_path, monkeypatch) -> None:
    import json

    from typer.testing import CliRunner

    from harness.cli.main import app
    from harness.config import load_config

    write_default_config(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-shared-registry")
    store = SQLiteStore.open_initialized(tmp_path)
    store.create_provider_account(
        provider_id="paid_openai_compatible",
        credential_kind="env",
        status="configured",
        description="shared registry",
        metadata={"env_var": "OPENAI_API_KEY"},
    )
    cfg = load_config(tmp_path)
    cli = CliRunner().invoke(app, ["providers", "status", "--project", str(tmp_path), "--output", "json"])
    dashboard = build_tui_dashboard(tmp_path)
    server = _route_get("/providers", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)

    assert cli.exit_code == 0, cli.output
    cli_provider = {item["provider_id"]: item for item in json.loads(cli.output)["providers"]}["paid_openai_compatible"]
    tui_provider = {item["provider_id"]: item for item in dashboard["model_catalog"]["providers"]}["paid_openai_compatible"]
    server_provider = {item["provider_id"]: item for item in server["providers"]}["paid_openai_compatible"]

    for provider in (cli_provider, tui_provider, server_provider):
        assert provider["credential_status"] == "configured"
        assert provider["credential_source"] == "provider_account"
        assert provider["connected"] is False
    assert tui_provider["auth_methods"] == ["env:<redacted>"]
    assert tui_provider["model_count"] == server_provider["model_count"]
    assert tui_provider["refresh_status"] in {"unsupported", "not_refreshed", "fresh", "stale", "mixed"}
    assert cli_provider["credentials_included"] is False
    assert server_provider["credentials_included"] is False
    assert cli_provider["active_account_id"] == server_provider["active_account_id"]
    assert tui_provider["available_model_count"] == server_provider["available_model_count"]


def test_tui_dashboard_displays_default_source_and_active_selected_model(tmp_path) -> None:
    write_default_config(tmp_path)
    store = SQLiteStore.open_initialized(tmp_path)
    session = store.create_session(title="Default model source")
    store.set_default_model_preference(
        "codex_cli/gpt-5.5",
        provider_id="codex_cli",
        model_id="gpt-5.5",
        source="test_default_source",
    )

    dashboard = build_tui_dashboard(tmp_path, selected_session_id=session.id)
    active_model = dashboard["model_catalog"]["active_model"]
    models_by_ref = {model["raw_model_ref"]: model for model in dashboard["model_catalog"]["models"]}
    right_panel = build_right_panel_model(dashboard, {}, "", "dashboard")
    context = next(section for section in right_panel["sections"] if section["id"] == "context")

    assert active_model["raw_model_ref"] == "codex_cli/gpt-5.5"
    assert active_model["selection_source"] == "operator_preference"
    assert active_model["model_resolution"]["source"] == "operator_preference"
    assert models_by_ref["codex_cli/gpt-5.5"]["selected_model"] is True
    assert "Model source: operator preference" in context["rows"]


def test_tui_dashboard_session_pane_and_right_panel_surface_malformed_transcript_health(tmp_path) -> None:
    write_default_config(tmp_path)
    store = SQLiteStore.open_initialized(tmp_path)
    session = store.create_session(title="Malformed transcript")
    append_session_event(
        tmp_path,
        session_id=session.id,
        event_type=SessionEventKind.SESSION_STARTED,
        message="Started",
    )
    transcript_path = tmp_path / ".harness" / "sessions" / session.id / "transcript.jsonl"
    with transcript_path.open("a", encoding="utf-8") as handle:
        handle.write("not json with secret sk-abcdefghijklmnopqrstuvwxyz\n")

    dashboard = build_tui_dashboard(tmp_path, selected_session_id=session.id)
    pane = build_session_pane_projection(tmp_path, selected_session_id=session.id)
    right_panel = build_right_panel_model(dashboard, {}, "", "dashboard")
    attention = next(section for section in right_panel["sections"] if section["id"] == "attention")

    active_health = dashboard["active_session"]["transcript_health"]
    row_health = pane["sessions"][0]["transcript_health"]
    recent_health = dashboard["recent_sessions"][0]["transcript_health"]
    for health in (active_health, row_health, recent_health):
        assert health["schema_version"] == "harness.session_events_read/v1"
        assert health["ok"] is False
        assert health["parse_error_count"] == 1
        assert health["validation_error_count"] == 0
        assert health["contents_included"] is False
        assert health["permission_granting"] is False
    assert dashboard["summary"]["malformed_session_transcripts"] == 1
    assert pane["counts"]["malformed_session_transcripts"] == 1
    assert attention["rows"] == ["Session transcript: malformed | parse=1 | validation=0"]
    serialized = str([dashboard, pane, right_panel])
    assert "not json" not in serialized
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in serialized
    assert "Traceback" not in serialized


def test_tui_dashboard_model_catalog_hot_reloads_custom_models_config(tmp_path) -> None:
    write_default_config(tmp_path)
    SQLiteStore.open_initialized(tmp_path)
    custom_path = tmp_path / ".harness" / "models.yaml"

    custom_path.write_text(
        yaml.safe_dump(
            {
                "providers": {
                    "picker_router": {
                        "display_name": "Picker Router",
                        "enabled": True,
                        "data_boundary": "local_only",
                        "base_url": "http://localhost:11434/v1",
                        "protocol": "openai_chat",
                        "credential": {"kind": "static_local"},
                        "models": {"alpha": {"context_window": 4096}},
                    }
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    first = build_tui_dashboard(tmp_path)
    first_refs = {model["raw_model_ref"] for model in first["model_catalog"]["models"]}
    assert "picker_router/alpha" in first_refs

    custom_path.write_text(
        yaml.safe_dump(
            {
                "providers": {
                    "picker_router": {
                        "display_name": "Picker Router",
                        "enabled": True,
                        "data_boundary": "local_only",
                        "base_url": "http://localhost:11434/v1",
                        "protocol": "openai_chat",
                        "credential": {"kind": "static_local"},
                        "models": {"beta": {"context_window": 8192}},
                    }
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    second = build_tui_dashboard(tmp_path)
    second_refs = {model["raw_model_ref"] for model in second["model_catalog"]["models"]}

    assert "picker_router/beta" in second_refs
    assert "picker_router/alpha" not in second_refs
    assert second["model_catalog"]["source"] == "project_config"
    assert second["model_catalog"]["permission_granting"] is False
    assert second["model_catalog"]["no_hidden_fallback"] is True


def test_tui_read_projections_use_app_service(tmp_path) -> None:
    pytest.importorskip("textual")
    store = SQLiteStore.open_initialized(tmp_path)
    session = store.create_session(title="Service-backed session")
    store.append_session_message(session.id, SessionMessageRole.USER, "service backed read path")
    service = RecordingAppService(tmp_path)

    app = create_harness_app(tmp_path, app_service=service)
    app._selected_session_id = session.id
    app._session_query = "service"

    dashboard = app._dashboard_snapshot(force=True)
    projection = app._session_pane_projection()

    assert service.dashboard_calls == [None, session.id]
    assert dashboard["active_session"]["id"] == session.id
    assert service.session_pane_calls == [
        {
            "selected_session_id": session.id,
            "status_filter": "open",
            "query": "service",
        }
    ]
    assert projection["selected_session_id"] == session.id


def test_tui_create_session_action_uses_app_service(tmp_path) -> None:
    pytest.importorskip("textual")
    service = RecordingMutationService(tmp_path)

    async def run_pilot() -> None:
        app = create_harness_app(tmp_path, app_service=service)
        async with app.run_test(size=(120, 40)) as pilot:
            app.action_create_blank_session()
            await pilot.pause()

            assert service.created_bodies == [
                {
                    "title": "New session",
                    "intent": "tui_blank_session",
                    "metadata": {"created_by": "tui_session_pane", "cwd": "."},
                }
            ]
            assert app._chat_state.session_id is not None
            assert service.store.get_session(app._chat_state.session_id).title == "New session"

    asyncio.run(run_pilot())


def test_tui_left_pane_focus_switches_without_cursor_lag(tmp_path) -> None:
    pytest.importorskip("textual")
    from textual.widgets import ListView

    service = RecordingMutationService(tmp_path)
    first = service.store.create_session(title="First session", agent_id="plan")
    second = service.store.create_session(title="Second session", agent_id="build")

    async def run_pilot() -> None:
        app = create_harness_app(tmp_path, app_service=service)
        async with app.run_test(size=(120, 40)) as pilot:
            session_list = app.query_one("#session-list", ListView)
            session_list.focus()
            await pilot.pause()

            assert app._left_pane_focused is True
            assert session_list.has_focus
            assert app._selected_session_id == second.id

            service.dashboard_calls.clear()
            service.session_pane_calls.clear()
            original_render_current_view = app._render_current_view
            render_current_calls = 0

            def render_current_view() -> None:
                nonlocal render_current_calls
                render_current_calls += 1

            app._render_current_view = render_current_view
            await pilot.press("down")
            await pilot.pause()

            assert app._selected_session_id == first.id
            assert app._chat_state.session_id is None
            assert render_current_calls == 0
            assert service.dashboard_calls == []
            assert service.session_pane_calls == []

            app._render_current_view = original_render_current_view
            await pilot.press("enter")
            await pilot.pause()

            assert app._chat_state.session_id == first.id
            assert app._selected_session_id == first.id

            await pilot.press("n")
            await pilot.pause()

            assert service.created_bodies[-1]["intent"] == "tui_blank_session"
            assert app._chat_state.session_id not in {first.id, second.id}
            assert service.store.get_session(app._chat_state.session_id).title == "New session"

    asyncio.run(run_pilot())


def test_tui_session_switch_refreshes_middle_transcript(tmp_path) -> None:
    pytest.importorskip("textual")
    from textual.widgets import ListView, Static

    store = SQLiteStore.open_initialized(tmp_path)
    first = store.create_session(title="First session", agent_id="plan")
    first_user = store.append_session_message(first.id, "user", "first question")
    store.append_session_part(first.id, first_user.id, "text", text="first question")
    first_assistant = store.append_session_message(first.id, "assistant", "first transcript answer", parent_message_id=first_user.id)
    store.append_session_part(first.id, first_assistant.id, "text", text="first transcript answer")
    second = store.create_session(title="Second session", agent_id="build")
    second_user = store.append_session_message(second.id, "user", "second question")
    store.append_session_part(second.id, second_user.id, "text", text="second question")
    second_assistant = store.append_session_message(second.id, "assistant", "second transcript answer", parent_message_id=second_user.id)
    store.append_session_part(second.id, second_assistant.id, "text", text="second transcript answer")
    service = HarnessAppService(tmp_path, store=store)

    async def run_pilot() -> None:
        app = create_harness_app(tmp_path, app_service=service)
        async with app.run_test(size=(120, 40)) as pilot:
            session_list = app.query_one("#session-list", ListView)
            chat_content = app.query_one("#chat-content", Static)

            session_list.focus()
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()

            rendered = str(chat_content.content)
            assert app._chat_state.session_id == second.id
            assert "second transcript answer" in rendered
            assert "first transcript answer" not in rendered

            await pilot.press("down", "enter")
            await pilot.pause()

            rendered = str(chat_content.content)
            assert app._chat_state.session_id == first.id
            assert "first transcript answer" in rendered
            assert "second transcript answer" not in rendered

    asyncio.run(run_pilot())


def test_tui_plain_prompt_uses_runtime_service_path(tmp_path) -> None:
    pytest.importorskip("textual")
    from textual.widgets import Static, TextArea

    service = RecordingMutationService(tmp_path)

    async def run_pilot() -> None:
        app = create_harness_app(tmp_path, app_service=service)
        async with app.run_test(size=(120, 40)) as pilot:
            prompt = app.query_one("#prompt", TextArea)
            prompt.value = "hello runtime"
            app.action_submit_prompt()
            await pilot.pause(0.5)

            assert service.created_bodies[0]["intent"] == "tui_prompt_session"
            assert service.prompt_async_calls
            call = service.prompt_async_calls[0]
            assert call["session_id"] == app._chat_state.session_id
            assert call["body"]["content"] == "hello runtime"
            assert call["body"]["source"] == "tui_prompt_submit"
            assert app._latest_response["kind"] == "runtime_prompt_submitted"
            assert app._request_in_flight is False
            rendered_chat = str(app.query_one("#chat-content", Static).content)
            rendered_status = str(app.query_one("#composer-status", Static).content)
            assert "Working (16s" in rendered_chat
            assert "Prompt Submitted" not in rendered_chat
            assert "Transcript will refresh" not in rendered_chat
            assert "prompt running" not in rendered_status

    asyncio.run(run_pilot())


def test_tui_local_prompt_is_scoped_to_selected_session(tmp_path) -> None:
    pytest.importorskip("textual")
    from textual.widgets import Static, TextArea

    service = RecordingMutationService(tmp_path)
    first = service.store.create_session(title="First session")
    second = service.store.create_session(title="Second session")
    service.store.append_session_message(second.id, "user", "second existing prompt")

    async def run_pilot() -> None:
        app = create_harness_app(tmp_path, app_service=service)
        async with app.run_test(size=(120, 40)) as pilot:
            app._selected_session_id = first.id
            app._chat_state.session_id = None
            prompt = app.query_one("#prompt", TextArea)
            prompt.value = "create a file in downloads, it should be an empty .md file"
            app.action_submit_prompt()
            await pilot.pause(0.7)

            assert app._chat_state.session_id == first.id
            assert not service.created_bodies
            first_rendered = str(app.query_one("#chat-content", Static).content)
            assert "create a file in downloads" in first_rendered
            assert "Write Blocked" in first_rendered

            app._selected_session_id = second.id
            app._chat_state.session_id = second.id
            app._render_chat()
            await pilot.pause()

            second_rendered = str(app.query_one("#chat-content", Static).content)
            assert "second existing prompt" in second_rendered
            assert "create a file in downloads" not in second_rendered
            assert "Write Blocked" not in second_rendered

    asyncio.run(run_pilot())


def test_tui_runtime_completion_event_refreshes_middle_transcript(tmp_path) -> None:
    pytest.importorskip("textual")
    from textual.widgets import Static, TextArea

    service = RecordingMutationService(tmp_path)

    async def run_pilot() -> None:
        app = create_harness_app(tmp_path, app_service=service)
        async with app.run_test(size=(120, 40)) as pilot:
            prompt = app.query_one("#prompt", TextArea)
            prompt.value = "hello runtime"
            app.action_submit_prompt()
            await pilot.pause(0.5)

            session_id = app._chat_state.session_id
            assert session_id is not None
            user_message = next(
                message
                for message in service.store.list_session_messages(session_id)
                if message.role.value == "user"
            )
            assistant = service.store.append_session_message(
                session_id,
                "assistant",
                "completed runtime answer",
                parent_message_id=user_message.id,
            )
            service.store.append_session_part(
                session_id,
                assistant.id,
                "text",
                text="completed runtime answer",
                metadata={"prompt_id": "prompt_test"},
            )
            service.runtime_by_session[session_id] = {
                "schema_version": "harness.session_runtime_state/v1",
                "session_id": session_id,
                "phase": "idle",
                "queued_prompt_count": 0,
                "queued_prompt_ids": [],
                "execution_enabled": True,
                "process_running": False,
            }
            app._event_stream_status = "live"
            app._event_refresh_dirty = True
            app._dashboard_cache = None

            app._refresh_live_view()
            await pilot.pause()

            rendered = str(app.query_one("#chat-content", Static).content)
            assert "completed runtime answer" in rendered
            assert "Working (" not in rendered

    asyncio.run(run_pilot())


def test_tui_plain_prompt_runtime_provider_failure_is_visible(tmp_path) -> None:
    pytest.importorskip("textual")
    from textual.widgets import Static, TextArea

    store = SQLiteStore.open_initialized(tmp_path)
    write_default_config(tmp_path)
    runtime = SessionRuntimeManager.for_store(store)
    service = HarnessAppService(tmp_path, store=store, runtime=runtime)

    async def run_pilot() -> None:
        app = create_harness_app(tmp_path, app_service=service)
        async with app.run_test(size=(120, 40)) as pilot:
            prompt = app.query_one("#prompt", TextArea)
            prompt.value = "hello"
            app.action_submit_prompt()
            await pilot.pause(1.0)
            if service.runtime is not None:
                service.runtime.wait(app._chat_state.session_id, timeout=2.0)
            app._render_chat()
            await pilot.pause()

            rendered = str(app.query_one("#chat-content", Static).content)
            assert "Runtime failed (SessionRuntimeProviderUnavailable)" in rendered
            assert "No session runtime text provider is configured" in rendered
            assert "hidden provider fallback" in rendered

    asyncio.run(run_pilot())


def test_tui_provider_connect_persists_evidence_without_provider_execution(tmp_path, monkeypatch) -> None:
    pytest.importorskip("textual")
    from textual.widgets import Static, TextArea

    write_default_config(tmp_path)
    store = SQLiteStore.open_initialized(tmp_path)
    session = store.create_session(
        title="Provider connect evidence",
        raw_model_ref="codex_cli/gpt-5.5",
        provider_id="codex_cli",
        model_id="gpt-5.5",
    )
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env-test")
    service = HarnessAppService(tmp_path, store=store)

    async def run_pilot() -> None:
        app = create_harness_app(tmp_path, app_service=service)
        async with app.run_test(size=(120, 40)) as pilot:
            app._selected_session_id = session.id
            app._chat_state.session_id = session.id
            prompt = app.query_one("#prompt", TextArea)
            prompt.value = "/provider connect paid_openai_compatible env OPENAI_API_KEY"
            app.action_submit_prompt()
            await pilot.pause(0.5)

            activation = app._latest_palette_activation
            assert activation["ok"] is True
            assert activation["activation_kind"] == "provider_connect"
            assert activation["provider_id"] == "paid_openai_compatible"
            assert activation["account_created"] is True
            assert activation["credential_source"] == "provider_account_env"
            assert activation["credential_value_included"] is False
            assert activation["credentials_included"] is False
            assert activation["provider_execution_started"] is False
            assert activation["model_execution_started"] is False
            assert activation["network_accessed"] is False
            assert activation["active_model_changed"] is False
            assert prompt.value == ""

            account = store.active_provider_account("paid_openai_compatible")
            assert account is not None
            assert account["credential_kind"] == "env"
            assert account["metadata"]["env_var"] == "OPENAI_API_KEY"
            unchanged = store.get_session(session.id)
            assert unchanged.raw_model_ref == "codex_cli/gpt-5.5"
            assert unchanged.provider_id == "codex_cli"
            events = [event for event in store.list_session_store_events(session.id) if event.kind == "tui.ui_activation.applied"]
            assert events
            payload = events[-1].payload
            assert payload["activation_kind"] == "provider_connect"
            assert payload["account_created"] is True
            assert payload["credential_value_included"] is False
            assert payload["provider_execution_started"] is False
            rendered = str(app.query_one("#chat-content", Static).content)
            assert "Provider Connect" in rendered
            assert "Evidence: provider_connect_persisted" in rendered
            assert "Credential value included: false" in rendered
            assert "Provider execution: false" in rendered
            status = str(app.query_one("#slash-status", Static).content)
            assert "Evidence: provider_connect_persisted" in status

            prompt.value = "/provider disconnect paid_openai_compatible"
            app.action_submit_prompt()
            await pilot.pause(0.5)

            disconnect_activation = app._latest_palette_activation
            assert disconnect_activation["ok"] is True
            assert disconnect_activation["activation_kind"] == "provider_disconnect"
            assert disconnect_activation["provider_id"] == "paid_openai_compatible"
            assert disconnect_activation["account_deleted"] is True
            assert disconnect_activation["provider_execution_started"] is False
            assert disconnect_activation["model_execution_started"] is False
            assert disconnect_activation["network_accessed"] is False
            assert store.active_provider_account("paid_openai_compatible") is None
            disconnect_events = [event for event in store.list_session_store_events(session.id) if event.kind == "tui.ui_activation.applied"]
            assert disconnect_events[-1].payload["activation_kind"] == "provider_disconnect"
            rendered = str(app.query_one("#chat-content", Static).content)
            assert "Evidence: provider_disconnect_persisted" in rendered
            status = str(app.query_one("#slash-status", Static).content)
            assert "Evidence: provider_disconnect_persisted" in status

            prompt.value = "/provider refresh paid_openai_compatible"
            app.action_submit_prompt()
            await pilot.pause(0.5)

            refresh_activation = app._latest_palette_activation
            assert refresh_activation["activation_kind"] == "provider_model_refresh"
            assert refresh_activation["provider_id"] == "paid_openai_compatible"
            assert refresh_activation["ok"] is False
            assert refresh_activation["provider_execution_started"] is False
            assert refresh_activation["model_execution_started"] is False
            assert refresh_activation["credentials_included"] is False
            assert refresh_activation["blocked_reasons"]
            rendered = str(app.query_one("#chat-content", Static).content)
            assert "Evidence: provider_model_refresh_blocked" in rendered
            status = str(app.query_one("#slash-status", Static).content)
            assert "Evidence: provider_model_refresh_blocked" in status

    asyncio.run(run_pilot())


def test_model_picker_renders_provider_connect_action_and_auth_methods(tmp_path) -> None:
    write_default_config(tmp_path)
    store = SQLiteStore.open_initialized(tmp_path)
    service = HarnessAppService(tmp_path, store=store)

    dashboard = service.dashboard()
    model_picker = render_model_selection_dialog(dashboard, query="paid_openai_compatible", selected_index=1)
    auth = service.provider_auth_methods()

    assert "Select model" in model_picker
    assert "Ctrl+A" in model_picker
    assert "account" in model_picker
    assert "F9" not in model_picker
    assert "Connect a provider" not in model_picker
    provider = next(item for item in auth["providers"] if item["provider_id"] == "paid_openai_compatible")
    methods = render_provider_auth_method_dialog(provider)

    assert "Connect provider" in methods
    assert "API key" in methods
    assert "Environment variable" in methods
    assert "OAuth / manual code" in methods
    assert "does not validate credentials or run a model" in methods


def test_tui_model_picker_api_key_flow_persists_secret_and_returns_to_model_picker(tmp_path) -> None:
    pytest.importorskip("textual")
    from textual.widgets import Static, TextArea

    write_default_config(tmp_path)
    store = SQLiteStore.open_initialized(tmp_path)
    session = store.create_session(title="Provider dialog connect", raw_model_ref="codex_cli/gpt-5.5")
    service = HarnessAppService(tmp_path, store=store)

    class KeyEvent:
        key = "ctrl+a"
        character = None

        def prevent_default(self) -> None:
            pass

        def stop(self) -> None:
            pass

    async def run_pilot() -> None:
        app = create_harness_app(tmp_path, app_service=service)
        async with app.run_test(size=(120, 40)) as pilot:
            app._selected_session_id = session.id
            app._chat_state.session_id = session.id
            prompt = app.query_one("#prompt", TextArea)
            prompt.value = "/provider"
            app.action_submit_prompt()
            await pilot.pause()

            assert app._dialog_kind == "models"
            provider_alias_dialog = str(app.query_one("#dialog-panel", Static).content)
            assert "Select model" in provider_alias_dialog
            assert "Connect a provider" not in provider_alias_dialog

            prompt.value = "/model paid_openai_compatible"
            app.action_submit_prompt()
            app._show_model_dialog(query="paid_openai_compatible", selected_index=1)
            await pilot.pause()

            assert app._dialog_kind == "models"
            model_dialog = str(app.query_one("#dialog-panel", Static).content)
            assert "Select model" in model_dialog
            assert "paid_openai_compatible" in model_dialog
            assert "Connect a provider" not in model_dialog

            assert app.action_handle_model_dialog_action_key(KeyEvent()) is True
            assert app._dialog_kind == "provider_auth_methods"
            app.action_activate_dialog_selection()
            assert app._dialog_kind == "provider_api_key_prompt"

            app._dialog_text_buffer = "sk-dialog-secret"
            await pilot.press("enter")
            await pilot.pause()
            assert app._dialog_kind == "provider_connect_confirm"
            confirm_dialog = str(app.query_one("#dialog-panel", Static).content)
            assert "Store provider API key" in confirm_dialog
            assert "Key length: 16 characters" in confirm_dialog
            assert "sk-dialog-secret" not in confirm_dialog

            await pilot.press("down", "enter")
            await pilot.pause()

            activation = app._latest_palette_activation
            assert activation["ok"] is True
            assert activation["activation_kind"] == "provider_connect"
            assert activation["source"] == "model_picker"
            assert activation["provider_id"] == "paid_openai_compatible"
            assert activation["method"] == "api_key"
            assert activation["credential_written"] is True
            assert activation["credential_value_included"] is False
            assert activation["credentials_included"] is False
            assert activation["provider_execution_started"] is False
            assert activation["model_execution_started"] is False
            assert activation["network_accessed"] is False
            assert activation["active_model_changed"] is False

            account = store.active_provider_account("paid_openai_compatible")
            assert account is not None
            assert account["credential_kind"] == "api_key"
            assert app._dialog_kind == "models"
            assert prompt.value == ""
            rendered = str(app.query_one("#chat-content", Static).content)
            assert "Provider Connect" in rendered
            assert "Evidence: provider_connect_persisted" in rendered
            assert "Credential value included: false" in rendered
            assert "sk-dialog-secret" not in rendered
            status = str(app.query_one("#slash-status", Static).content)
            assert "Select a model from this provider" in status
            assert "sk-dialog-secret" not in str(app.query_one("#dialog-panel", Static).content)

            app._show_provider_disconnect_decision(
                "paid_openai_compatible",
                provider={"provider_id": "paid_openai_compatible", "display_name": "Paid OpenAI Compatible"},
            )
            await pilot.pause()
            assert app._dialog_kind == "provider_disconnect_confirm"
            disconnect_dialog = str(app.query_one("#dialog-panel", Static).content)
            assert "Disconnect" in disconnect_dialog
            assert "secret-store" in disconnect_dialog
            await pilot.press("down", "enter")
            await pilot.pause(0.5)

            disconnect_activation = app._latest_palette_activation
            assert disconnect_activation["ok"] is True
            assert disconnect_activation["activation_kind"] == "provider_disconnect"
            assert disconnect_activation["provider_id"] == "paid_openai_compatible"
            assert disconnect_activation["provider_execution_started"] is False
            assert disconnect_activation["model_execution_started"] is False
            assert store.active_provider_account("paid_openai_compatible") is None

    asyncio.run(run_pilot())


def test_tui_provider_auth_static_local_flow_creates_account_without_secret_prompt(tmp_path) -> None:
    pytest.importorskip("textual")
    from textual.widgets import Static

    write_default_config(tmp_path)
    store = SQLiteStore.open_initialized(tmp_path)
    session = store.create_session(title="Static local connect", raw_model_ref="codex_cli/gpt-5.5")
    (tmp_path / ".harness" / "models.yaml").write_text(
        """
providers:
  dialog_local:
    display_name: Dialog Local
    enabled: true
    data_boundary: local_only
    base_url: http://localhost:11434/v1
    protocol: openai_chat
    credential:
      kind: static_local
    models:
      local-model:
        display_name: Local Model
        api_id: local-model
        context_window: 8192
        status: active
""".lstrip(),
        encoding="utf-8",
    )
    service = HarnessAppService(tmp_path, store=store)

    async def run_pilot() -> None:
        app = create_harness_app(tmp_path, app_service=service)
        async with app.run_test(size=(120, 40)) as pilot:
            app._selected_session_id = session.id
            app._chat_state.session_id = session.id
            app._show_provider_auth_method_dialog("dialog_local", selected_index=2)
            await pilot.pause()

            rendered_methods = str(app.query_one("#dialog-panel", Static).content)
            assert "Static local" in rendered_methods
            app.action_activate_dialog_selection()
            await pilot.pause()
            assert app._dialog_kind == "provider_connect_confirm"
            confirm_dialog = str(app.query_one("#dialog-panel", Static).content)
            assert "Confirm provider connect" in confirm_dialog
            assert "Static local" in confirm_dialog

            await pilot.press("down", "enter")
            await pilot.pause()

            activation = app._latest_palette_activation
            assert activation["ok"] is True
            assert activation["activation_kind"] == "provider_connect"
            assert activation["provider_id"] == "dialog_local"
            assert activation["method"] == "static_local"
            assert activation["credential_written"] is False
            assert activation["credential_value_included"] is False
            assert activation["credentials_included"] is False
            assert activation["provider_execution_started"] is False
            assert activation["model_execution_started"] is False
            assert activation["network_accessed"] is False

            account = store.active_provider_account("dialog_local")
            assert account is not None
            assert account["credential_kind"] == "static_local"
            assert app._dialog_kind == "models"
            model_dialog = str(app.query_one("#dialog-panel", Static).content)
            assert "Dialog Local" in model_dialog or "local-model" in model_dialog

    asyncio.run(run_pilot())


def test_tui_model_preference_and_inspect_actions_route_through_service(tmp_path) -> None:
    pytest.importorskip("textual")
    from textual.widgets import Static, TextArea

    write_default_config(tmp_path)
    store = SQLiteStore.open_initialized(tmp_path)
    session = store.create_session(title="Model actions", raw_model_ref="codex_cli/gpt-5.5")
    service = HarnessAppService(tmp_path, store=store)

    async def run_pilot() -> None:
        app = create_harness_app(tmp_path, app_service=service)
        async with app.run_test(size=(120, 40)) as pilot:
            app._selected_session_id = session.id
            app._chat_state.session_id = session.id
            prompt = app.query_one("#prompt", TextArea)

            prompt.value = "/model codex_cli/gpt-5.5"
            app.action_submit_prompt()
            await pilot.pause(0.5)
            selection_activation = app._latest_palette_activation
            assert selection_activation["ok"] is True
            assert selection_activation["activation_kind"] == "session_model_selection"
            assert selection_activation["evidence_status"] == "session_model_selection_persisted"
            status = str(app.query_one("#slash-status", Static).content)
            assert "Evidence: session_model_selection_persisted" in status

            prompt.value = "/model favorite codex_cli/gpt-5.5"
            app.action_submit_prompt()
            await pilot.pause(0.5)
            favorite_activation = app._latest_palette_activation
            assert favorite_activation["ok"] is True
            assert favorite_activation["activation_kind"] == "model_action"
            assert favorite_activation["action_name"] == "favorite"
            assert favorite_activation["raw_model_ref"] == "codex_cli/gpt-5.5"
            assert favorite_activation["provider_execution_started"] is False
            assert favorite_activation["model_execution_started"] is False
            assert favorite_activation["network_accessed"] is False
            assert store.get_model_preference("codex_cli/gpt-5.5")["favorite"] is True

            prompt.value = "/model default codex_cli/gpt-5.5"
            app.action_submit_prompt()
            await pilot.pause(0.5)
            default_activation = app._latest_palette_activation
            assert default_activation["ok"] is True
            assert default_activation["action_name"] == "default"
            assert default_activation["filesystem_modified"] is True
            assert store.get_default_model_preference()["raw_model_ref"] == "codex_cli/gpt-5.5"

            prompt.value = "/model inspect codex_cli/gpt-5.5"
            app.action_submit_prompt()
            await pilot.pause(0.5)
            inspect_activation = app._latest_palette_activation
            assert inspect_activation["ok"] is True
            assert inspect_activation["action_name"] == "inspect"
            assert inspect_activation["filesystem_modified"] is False
            assert inspect_activation["provider_execution_started"] is False
            assert inspect_activation["model_execution_started"] is False
            rendered = str(app.query_one("#chat-content", Static).content)
            assert "Model Inspect" in rendered
            assert "Canonical: codex_cli/gpt-5.5" in rendered
            assert "Evidence: model_favorite_persisted" in rendered
            assert "Evidence: model_default_persisted" in rendered
            assert "Evidence: model_inspection_rendered" in rendered
            assert "Provider execution: false" in rendered
            status = str(app.query_one("#slash-status", Static).content)
            assert "Evidence: model_inspection_rendered" in status

    asyncio.run(run_pilot())


def test_tui_model_picker_keyboard_hints_trigger_service_actions(tmp_path) -> None:
    pytest.importorskip("textual")
    from textual.widgets import Static, TextArea

    write_default_config(tmp_path)
    store = SQLiteStore.open_initialized(tmp_path)
    session = store.create_session(title="Model action keys", raw_model_ref="codex_cli/gpt-5.5")
    service = HarnessAppService(tmp_path, store=store)

    async def run_pilot() -> None:
        app = create_harness_app(tmp_path, app_service=service)
        async with app.run_test(size=(120, 40)) as pilot:
            app._selected_session_id = session.id
            app._chat_state.session_id = session.id
            prompt = app.query_one("#prompt", TextArea)
            dialog = app.query_one("#dialog-panel", Static)

            prompt.value = "/model"
            app.action_submit_prompt()
            await pilot.pause(0.5)
            rendered_dialog = str(dialog.content)
            assert "F5[/bold steel_blue1] favorite" in rendered_dialog
            assert "F6[/bold steel_blue1] default" in rendered_dialog
            assert "F7[/bold steel_blue1] inspect" in rendered_dialog
            assert "F8[/bold steel_blue1] refresh" not in rendered_dialog

            await pilot.press("f5")
            await pilot.pause(0.5)
            assert app._latest_palette_activation["activation_kind"] == "model_action"
            assert app._latest_palette_activation["action_name"] == "favorite"
            assert store.get_model_preference("codex_cli/gpt-5.5")["favorite"] is True

            prompt.value = "/model"
            app.action_submit_prompt()
            await pilot.pause(0.5)
            await pilot.press("f6")
            await pilot.pause(0.5)
            assert app._latest_palette_activation["activation_kind"] == "model_action"
            assert app._latest_palette_activation["action_name"] == "default"
            assert store.get_default_model_preference()["raw_model_ref"] == "codex_cli/gpt-5.5"

            prompt.value = "/model"
            app.action_submit_prompt()
            await pilot.pause(0.5)
            await pilot.press("f7")
            await pilot.pause(0.5)
            assert app._latest_palette_activation["activation_kind"] == "model_action"
            assert app._latest_palette_activation["action_name"] == "inspect"
            assert app._latest_palette_activation["filesystem_modified"] is False

    asyncio.run(run_pilot())


def test_tui_model_picker_navigation_and_filtering_are_side_effect_free(tmp_path) -> None:
    pytest.importorskip("textual")
    from textual.widgets import TextArea

    write_default_config(tmp_path)
    store = SQLiteStore.open_initialized(tmp_path)
    session = store.create_session(title="Model picker navigation", raw_model_ref="codex_cli/gpt-5.5")
    service = HarnessAppService(tmp_path, store=store)

    async def run_pilot() -> None:
        app = create_harness_app(tmp_path, app_service=service)
        async with app.run_test(size=(120, 40)) as pilot:
            app._selected_session_id = session.id
            app._chat_state.session_id = session.id
            prompt = app.query_one("#prompt", TextArea)
            prompt.focus()
            prompt.value = "/model"
            app.action_submit_prompt()
            await pilot.pause(0.5)

            initial_activation = dict(app._latest_palette_activation)
            initial_messages = list(app._messages)
            initial_events = list(store.list_session_store_events(session.id))
            assert initial_activation["activation_kind"] == "model_picker_help"

            await pilot.press("down")
            await pilot.press("up")
            await pilot.pause(0.5)

            assert app._dialog_kind == "models"
            assert app._dialog_selected_index == 0
            assert app._latest_palette_activation == initial_activation
            assert app._messages == initial_messages
            assert store.list_session_store_events(session.id) == initial_events
            assert store.get_session(session.id).raw_model_ref == "codex_cli/gpt-5.5"
            with pytest.raises(KeyError):
                store.get_model_preference("codex_cli/gpt-5.5")

            prompt.cursor_position = len(prompt.value)
            await pilot.press("f")
            await pilot.pause(0.5)

            assert prompt.value.endswith("f")
            assert app._dialog_query == "f"
            assert app._latest_palette_activation == initial_activation
            assert app._messages == initial_messages
            assert store.list_session_store_events(session.id) == initial_events
            assert store.get_session(session.id).raw_model_ref == "codex_cli/gpt-5.5"
            with pytest.raises(KeyError):
                store.get_model_preference("codex_cli/gpt-5.5")
            assert app._latest_palette_activation["process_started"] is False
            assert app._latest_palette_activation["permission_granting"] is False

    asyncio.run(run_pilot())


def test_tui_session_event_refreshes_projection_without_extra_dirty_poll(tmp_path) -> None:
    pytest.importorskip("textual")
    store = SQLiteStore.open_initialized(tmp_path)
    session = store.create_session(title="Live event session")
    service = HarnessAppService(tmp_path, store=store)

    async def run_pilot() -> None:
        app = create_harness_app(tmp_path, app_service=service)
        app._selected_session_id = session.id
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            before_count = app._event_refresh_count
            event = store.append_store_event(
                EventStreamType.SESSION,
                session.id,
                "unknown.live_event",
                {"summary": "unknown events should still invalidate projections"},
                session_id=session.id,
                redaction_state=RedactionState.NOT_REQUIRED,
            )
            await pilot.pause(0.5)

            assert app._event_refresh_count > before_count
            assert app._event_refresh_dirty is False
            assert app._event_stream_status == "live"
            assert any(item["id"] == event.id for item in app._transient_session_events)

    asyncio.run(run_pilot())


def test_tui_live_refresh_timer_skips_idle_render_when_event_stream_live(tmp_path) -> None:
    pytest.importorskip("textual")
    store = SQLiteStore.open_initialized(tmp_path)
    store.create_session(title="Idle live refresh")
    service = HarnessAppService(tmp_path, store=store)

    async def run_pilot() -> None:
        app = create_harness_app(tmp_path, app_service=service)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            render_calls = 0
            right_pane_calls = 0

            def render_current_view() -> None:
                nonlocal render_calls
                render_calls += 1

            def render_right_pane_only() -> None:
                nonlocal right_pane_calls
                right_pane_calls += 1

            app._render_current_view = render_current_view
            app._render_right_pane_only = render_right_pane_only
            app._event_stream_status = "live"
            app._event_refresh_dirty = False
            app._request_in_flight = False
            app._dashboard_cache_at = time.monotonic() - 10.0

            app._refresh_live_view()

            assert render_calls == 0
            assert right_pane_calls == 0

    asyncio.run(run_pilot())


def test_tui_polling_fallback_refreshes_idle_view(tmp_path) -> None:
    pytest.importorskip("textual")
    store = SQLiteStore.open_initialized(tmp_path)
    store.create_session(title="Polling fallback refresh")
    service = RecordingAppService(tmp_path)

    async def run_pilot() -> None:
        app = create_harness_app(tmp_path, app_service=service)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            render_calls = 0

            def render_current_view() -> None:
                nonlocal render_calls
                render_calls += 1

            app._render_current_view = render_current_view
            app._event_stream_status = "polling_fallback:service_subscription_missing"
            app._event_refresh_dirty = False
            app._request_in_flight = False
            app._dashboard_cache_at = time.monotonic() - 10.0

            app._refresh_live_view()

            assert render_calls == 1

    asyncio.run(run_pilot())


def test_tui_event_replay_is_coalesced_into_one_refresh(tmp_path) -> None:
    pytest.importorskip("textual")
    store = SQLiteStore.open_initialized(tmp_path)
    session = store.create_session(title="Replay burst")
    service = HarnessAppService(tmp_path, store=store)

    async def run_pilot() -> None:
        app = create_harness_app(tmp_path, app_service=service)
        app._selected_session_id = session.id
        app._chat_state.session_id = session.id
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            render_calls = 0

            def render_current_view() -> None:
                nonlocal render_calls
                render_calls += 1

            app._render_current_view = render_current_view
            app._event_stream_status = "live"

            for index in range(12):
                event = store.append_store_event(
                    EventStreamType.SESSION,
                    session.id,
                    f"replay.event_{index}",
                    {"summary": f"event {index}"},
                    session_id=session.id,
                    redaction_state=RedactionState.NOT_REQUIRED,
                )
                app._handle_service_event("session", event)

            assert render_calls == 0
            await pilot.pause(0.35)

            assert render_calls == 1
            assert app._event_refresh_dirty is False

    asyncio.run(run_pilot())


def test_tui_plain_chat_typing_does_not_rerender_dashboard_panes(tmp_path) -> None:
    pytest.importorskip("textual")
    from textual.widgets import TextArea

    store = SQLiteStore.open_initialized(tmp_path)
    store.create_session(title="Typing should stay local")
    service = RecordingAppService(tmp_path)

    async def run_pilot() -> None:
        app = create_harness_app(tmp_path, app_service=service)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            prompt = app.query_one("#prompt", TextArea)
            service.dashboard_calls.clear()
            service.session_pane_calls.clear()
            render_current_calls = 0
            render_right_only_calls = 0
            slash_suggestion_calls = 0

            def render_current_view() -> None:
                nonlocal render_current_calls
                render_current_calls += 1

            def render_right_pane_only() -> None:
                nonlocal render_right_only_calls
                render_right_only_calls += 1

            def render_slash_suggestions(prompt_value: str) -> None:
                nonlocal slash_suggestion_calls
                slash_suggestion_calls += 1

            app._render_current_view = render_current_view
            app._render_right_pane_only = render_right_pane_only
            app._render_slash_suggestions = render_slash_suggestions

            for char in "hello":
                await pilot.press(char)
            await pilot.pause()

            assert prompt.value == "hello"
            assert render_current_calls == 0
            assert render_right_only_calls == 0
            assert slash_suggestion_calls == 0
            assert service.dashboard_calls == []
            assert service.session_pane_calls == []

    asyncio.run(run_pilot())


def test_tui_palette_typing_updates_palette_without_session_pane_refresh(tmp_path) -> None:
    pytest.importorskip("textual")
    from textual.widgets import TextArea

    store = SQLiteStore.open_initialized(tmp_path)
    store.create_session(title="Palette typing")
    service = RecordingAppService(tmp_path)

    async def run_pilot() -> None:
        app = create_harness_app(tmp_path, app_service=service)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            prompt = app.query_one("#prompt", TextArea)
            app._focus_mode = "palette"
            service.dashboard_calls.clear()
            service.session_pane_calls.clear()
            render_current_calls = 0
            render_right_only_calls = 0

            def render_current_view() -> None:
                nonlocal render_current_calls
                render_current_calls += 1

            original_render_right_pane_only = app._render_right_pane_only

            def render_right_pane_only() -> None:
                nonlocal render_right_only_calls
                render_right_only_calls += 1
                original_render_right_pane_only()

            app._render_current_view = render_current_view
            app._render_right_pane_only = render_right_pane_only

            for char in "run":
                await pilot.press(char)
            await pilot.pause()

            assert prompt.value == "run"
            assert render_current_calls == 0
            assert render_right_only_calls >= 1
            assert service.session_pane_calls == []

    asyncio.run(run_pilot())


def test_model_dialog_reuses_cached_dashboard_for_open_filter_and_navigation(tmp_path) -> None:
    pytest.importorskip("textual")
    from textual.widgets import TextArea

    write_default_config(tmp_path)
    store = SQLiteStore.open_initialized(tmp_path)
    session = store.create_session(title="Model dialog perf")
    service = RecordingAppService(tmp_path)

    async def run_pilot() -> None:
        app = create_harness_app(tmp_path, app_service=service)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            cached = service.dashboard(selected_session_id=session.id)
            app._selected_session_id = session.id
            app._chat_state.session_id = session.id
            app._dashboard_cache = cached
            app._dashboard_cache_session_id = session.id
            app._dashboard_cache_at = time.monotonic() - 60.0
            service.dashboard_calls.clear()

            app._show_model_dialog()
            app.action_move_dialog_selection(1)
            app._show_model_dialog(query="gpt", selected_index=0)

            prompt = app.query_one("#prompt", TextArea)
            prompt.value = "/model gpt"
            app._show_models_list(source="leader", slash="ctrl+x m")

            assert service.dashboard_calls == []

    asyncio.run(run_pilot())


def test_model_dialog_selection_uses_cached_dashboard_before_persist_refresh(tmp_path) -> None:
    pytest.importorskip("textual")

    write_default_config(tmp_path)
    store = SQLiteStore.open_initialized(tmp_path)
    session = store.create_session(title="Model dialog select")

    class CountingHarnessService(HarnessAppService):
        def __init__(self) -> None:
            super().__init__(tmp_path, store=store)
            self.dashboard_calls: list[str | None] = []

        def dashboard(self, *, selected_session_id: str | None = None) -> dict:
            self.dashboard_calls.append(selected_session_id)
            return super().dashboard(selected_session_id=selected_session_id)

    service = CountingHarnessService()

    async def run_pilot() -> None:
        app = create_harness_app(tmp_path, app_service=service)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            cached = service.dashboard(selected_session_id=session.id)
            app._selected_session_id = session.id
            app._chat_state.session_id = session.id
            app._dashboard_cache = cached
            app._dashboard_cache_session_id = session.id
            app._dashboard_cache_at = time.monotonic() - 60.0
            service.dashboard_calls.clear()

            app._show_model_dialog()
            app._activate_selected_model_dialog_entry()

            assert service.dashboard_calls == [session.id]

    asyncio.run(run_pilot())


def test_attached_tui_uses_http_service_for_session_creation_and_live_events(tmp_path) -> None:
    pytest.importorskip("textual")
    store = SQLiteStore.open_initialized(tmp_path)
    write_default_config(tmp_path)
    server = create_local_http_server(tmp_path, host="127.0.0.1", port=0, token="attached-tui-token")
    host, port = server.server_address
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    service = HarnessHTTPAppService.from_attach(f"http://{host}:{port}", "attached-tui-token")

    async def run_pilot() -> None:
        app = create_harness_app(tmp_path, app_service=service)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.5)
            assert app._event_stream_status == "live"

            app.action_create_blank_session()
            await pilot.pause(0.5)

            session_id = app._selected_session_id
            assert session_id is not None
            assert store.get_session(session_id).title == "New session"
            assert app._session_pane_projection()["selected_session_id"] == session_id

            app._sync_session_event_subscription()
            await pilot.pause(0.25)
            before_count = app._event_refresh_count
            event = store.append_store_event(
                EventStreamType.SESSION,
                session_id,
                "test.attached_tui_live_event",
                {"summary": "attached TUI received a live event"},
                session_id=session_id,
                redaction_state=RedactionState.NOT_REQUIRED,
            )
            await pilot.pause(0.75)

            assert app._event_refresh_count > before_count
            assert any(item["id"] == event.id for item in app._transient_session_events)

    try:
        asyncio.run(run_pilot())
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_tui_switching_sessions_closes_old_subscription_and_replays_selected_session(tmp_path) -> None:
    pytest.importorskip("textual")
    store = SQLiteStore.open_initialized(tmp_path)
    first = store.create_session(title="First session")
    second = store.create_session(title="Second session")
    replayed = store.append_store_event(
        EventStreamType.SESSION,
        second.id,
        "session.replay_check",
        {"summary": "replay selected session"},
        session_id=second.id,
        redaction_state=RedactionState.NOT_REQUIRED,
    )
    service = HarnessAppService(tmp_path, store=store)

    async def run_pilot() -> None:
        app = create_harness_app(tmp_path, app_service=service)
        app._selected_session_id = first.id
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            old_subscription = app._session_event_subscription
            assert old_subscription is not None

            app._selected_session_id = second.id
            app._chat_state.session_id = second.id
            app._sync_session_event_subscription()
            await pilot.pause(0.5)

            assert old_subscription.closed is True
            assert app._session_event_subscription_id == second.id
            assert any(item["id"] == replayed.id for item in app._transient_session_events)

    asyncio.run(run_pilot())


def test_tui_event_stream_loss_falls_back_to_polling(tmp_path) -> None:
    pytest.importorskip("textual")
    service = BrokenEventService(tmp_path)

    async def run_pilot() -> None:
        app = create_harness_app(tmp_path, app_service=service)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.5)

            assert app._event_stream_failures >= 1
            assert app._event_stream_status.startswith("polling_fallback:global:RuntimeError")

    asyncio.run(run_pilot())


def test_tui_permission_card_renders_and_enter_uses_selected_default_denial(tmp_path) -> None:
    pytest.importorskip("textual")
    store = SQLiteStore.open_initialized(tmp_path)
    session = store.create_session(title="Needs approval")
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
    service = HarnessAppService(tmp_path, store=store)

    async def run_pilot() -> None:
        app = create_harness_app(tmp_path, app_service=service)
        app._selected_session_id = session.id
        async with app.run_test(size=(120, 40)) as pilot:
            app.action_open_permission_dialog()
            await pilot.pause()
            rendered = str(app.query_one("#dialog-panel").content)
            assert "Session permission" in rendered
            assert "Tool: shell" in rendered
            assert "Operation: pytest" in rendered
            assert "Risk: medium" in rendered
            assert "Boundary: local only" in rendered
            assert "Deny" in rendered
            assert "Allow once" in rendered
            assert "Enter selects" in rendered

            await pilot.press("enter")
            await pilot.pause()

            assert store.get_session_permission(permission.id).status.value == "denied"
            assert app._dialog_visible is False
            assert app._latest_palette_activation["permission_granting"] is False

    asyncio.run(run_pilot())


def test_tui_permission_dialog_allow_deny_cancel_use_service(tmp_path) -> None:
    pytest.importorskip("textual")
    store = SQLiteStore.open_initialized(tmp_path)
    session = store.create_session(title="Permission replies")
    permissions = [
        store.request_session_permission(
            session.id,
            tool_id="shell",
            normalized_action="run",
            normalized_target_pattern=f"command-{index}",
            boundary_kind=SessionPermissionBoundaryKind.LOCAL_ONLY,
            risk="medium",
            scope=SessionPermissionScope.ONCE,
            source=SessionPermissionSource.POLICY,
        )
        for index in range(3)
    ]
    service = HarnessAppService(tmp_path, store=store)

    async def run_pilot() -> None:
        app = create_harness_app(tmp_path, app_service=service)
        app._selected_session_id = session.id
        async with app.run_test(size=(120, 40)) as pilot:
            app._dialog_context = {"permission_card": service.list_permissions(session.id)["snapshot"]["pending_approval_cards"][0]}
            app._show_dialog(render_permission_card_dialog(app._dialog_context["permission_card"]), kind="permission")
            await pilot.press("down", "enter")
            await pilot.pause()
            assert store.get_session_permission(permissions[0].id).status.value == "allowed"
            assert app._latest_palette_activation["permission_granting"] is True
            assert app._latest_palette_activation["execution_started"] is False

            app._dialog_context = {"permission_card": service.list_permissions(session.id)["snapshot"]["pending_approval_cards"][0]}
            app._show_dialog(render_permission_card_dialog(app._dialog_context["permission_card"]), kind="permission")
            await pilot.press("enter")
            await pilot.pause()
            assert store.get_session_permission(permissions[1].id).status.value == "denied"
            assert app._latest_palette_activation["permission_granting"] is False
            assert app._latest_palette_activation["execution_started"] is False

            app._dialog_context = {"permission_card": service.list_permissions(session.id)["snapshot"]["pending_approval_cards"][0]}
            app._show_dialog(render_permission_card_dialog(app._dialog_context["permission_card"]), kind="permission")
            await pilot.press("down", "down", "enter")
            await pilot.pause()
            assert store.get_session_permission(permissions[2].id).status.value == "cancelled"
            assert app._latest_palette_activation["permission_granting"] is False
            assert app._latest_palette_activation["execution_started"] is False

    asyncio.run(run_pilot())


def test_tui_transcript_projection_reconstructs_persisted_messages_after_restart(tmp_path) -> None:
    store = SQLiteStore.open_initialized(tmp_path)
    session = store.create_session(title="Durable transcript")
    user = store.append_session_message(session.id, "user", "persist this question")
    store.append_session_part(session.id, user.id, "text", text="persist this question")
    assistant = store.append_session_message(session.id, "assistant", "persisted answer", parent_message_id=user.id)
    store.append_session_part(session.id, assistant.id, "text", text="persisted answer")
    service = HarnessAppService(tmp_path, store=store)

    projection = build_tui_transcript_projection(
        service,
        session.id,
        [{"role": "assistant", "title": "Harness chat", "lines": ["Project welcome"]}],
    )

    assert projection["source"] == "persisted_session"
    assert projection["persisted_message_count"] == 2
    assert projection["messages"] == [
        {"role": "user", "title": "persist this question", "lines": []},
        {"role": "assistant", "title": "Assistant", "lines": ["persisted answer"]},
    ]


def test_tui_transcript_projection_omits_duplicate_delta_after_final_message(tmp_path) -> None:
    store = SQLiteStore.open_initialized(tmp_path)
    session = store.create_session(title="Delta transcript")
    user = store.append_session_message(session.id, "user", "run turn")
    assistant = store.append_session_message(session.id, "assistant", "final text", parent_message_id=user.id)
    part = store.append_session_part(session.id, assistant.id, "text", text="final text")
    store.append_store_event(
        EventStreamType.SESSION,
        session.id,
        "model.message_delta",
        {"prompt_id": "prompt_one", "message_id": assistant.id, "part_id": part.id, "delta": "final text"},
        session_id=session.id,
        message_id=assistant.id,
        redaction_state=RedactionState.REDACTED,
    )
    service = HarnessAppService(tmp_path, store=store)

    projection = build_tui_transcript_projection(service, session.id, [])

    rendered_text = "\n".join(
        line
        for message in projection["messages"]
        for line in [str(message.get("title") or ""), *[str(item) for item in message.get("lines", [])]]
    )
    assert rendered_text.count("final text") == 1


def test_tui_transcript_projection_omits_local_final_when_persisted_assistant_exists(tmp_path) -> None:
    store = SQLiteStore.open_initialized(tmp_path)
    session = store.create_session(title="Local duplicate transcript")
    user = store.append_session_message(session.id, "user", "hello")
    store.append_session_part(session.id, user.id, "text", text="hello")
    assistant = store.append_session_message(session.id, "assistant", "Hello from persisted runtime", parent_message_id=user.id)
    store.append_session_part(
        session.id,
        assistant.id,
        "text",
        text="Hello from persisted runtime",
        metadata={"prompt_id": "prompt_local_duplicate"},
    )
    service = HarnessAppService(tmp_path, store=store)

    projection = build_tui_transcript_projection(
        service,
        session.id,
        [
            {"role": "user", "title": "hello", "lines": []},
            {"role": "assistant", "title": "Assistant", "lines": ["Hello from persisted runtime"]},
        ],
    )

    rendered_text = "\n".join(
        [str(message.get("title") or "") for message in projection["messages"]]
        + [str(line) for message in projection["messages"] for line in message.get("lines", [])]
    )
    assert rendered_text.count("hello") == 1
    assert rendered_text.count("Hello from persisted runtime") == 1


def test_tui_transcript_projection_suppresses_completed_runtime_event_chatter(tmp_path) -> None:
    store = SQLiteStore.open_initialized(tmp_path)
    session = store.create_session(title="Completed runtime transcript")
    user = store.append_session_message(session.id, "user", "summarize")
    assistant = store.append_session_message(session.id, "assistant", "Persisted answer", parent_message_id=user.id)
    part = store.append_session_part(
        session.id,
        assistant.id,
        "text",
        text="Persisted answer",
        metadata={"prompt_id": "prompt_completed"},
    )
    for kind, payload in [
        ("model.started", {"prompt_id": "prompt_completed", "model_ref": "runtime model"}),
        ("tool_call.started", {"prompt_id": "prompt_completed", "tool_id": "web-search"}),
        ("model.message_delta", {"prompt_id": "prompt_completed", "message_id": assistant.id, "part_id": part.id, "delta": "Persisted answer"}),
        ("model.completed", {"prompt_id": "prompt_completed", "message_id": assistant.id, "part_id": part.id}),
        ("harness.turn.finished", {"prompt_id": "prompt_completed", "failed": False}),
    ]:
        store.append_store_event(
            EventStreamType.SESSION,
            session.id,
            kind,
            payload,
            session_id=session.id,
            redaction_state=RedactionState.REDACTED,
        )
    service = HarnessAppService(tmp_path, store=store)

    projection = build_tui_transcript_projection(service, session.id, [])

    rendered_text = "\n".join(
        [str(message.get("title") or "") for message in projection["messages"]]
        + [str(line) for message in projection["messages"] for line in message.get("lines", [])]
    )
    assert "Persisted answer" in rendered_text
    assert "Runtime started" not in rendered_text
    assert "Tool started" not in rendered_text
    assert "Session Events" not in rendered_text


def test_tui_transcript_projection_hides_plan_mode_toggle_chatter(tmp_path) -> None:
    write_default_config(tmp_path)
    store = SQLiteStore.open_initialized(tmp_path)
    session = store.create_session(title="Plan mode transcript")
    execute_session_tool(store, tmp_path, session.id, "plan-enter", {"reason": "shortcut"})
    execute_session_tool(
        store,
        tmp_path,
        session.id,
        "plan-exit",
        {"summary": "done", "next_action": "", "proposed_tools": []},
    )
    service = HarnessAppService(tmp_path, store=store)

    projection = build_tui_transcript_projection(service, session.id, [])

    rendered_text = "\n".join(
        [str(message.get("title") or "") for message in projection["messages"]]
        + [str(line) for message in projection["messages"] for line in message.get("lines", [])]
    )
    assert "plan-enter" not in rendered_text
    assert "plan-exit" not in rendered_text
    assert "Planning mode entered" not in rendered_text
    assert "Planning mode exited" not in rendered_text
    assert "Session Events" not in rendered_text


def test_tui_transcript_projection_orders_runtime_events_after_their_prompt(tmp_path) -> None:
    store = SQLiteStore.open_initialized(tmp_path)
    session = store.create_session(title="Ordered runtime transcript")
    old_user = store.append_session_message(session.id, "user", "hello")
    old_assistant = store.append_session_message(session.id, "assistant", "old answer", parent_message_id=old_user.id)
    store.append_session_part(session.id, old_assistant.id, "text", text="old answer", metadata={"prompt_id": "prompt_old"})
    current_user = store.append_session_message(session.id, "user", "what is in this repo")
    store.append_session_part(session.id, current_user.id, "text", text="what is in this repo")
    for kind, payload in [
        ("model.started", {"prompt_id": "prompt_current", "model_ref": "runtime model"}),
        ("tool_call.started", {"prompt_id": "prompt_current", "tool_id": "web-search"}),
        ("tool_call.output", {"prompt_id": "prompt_current", "tool_id": "web-search", "ok": False}),
        ("tool_call.finished", {"prompt_id": "prompt_current", "tool_id": "web-search", "ok": False}),
    ]:
        store.append_store_event(
            EventStreamType.SESSION,
            session.id,
            kind,
            payload,
            session_id=session.id,
            message_id=current_user.id,
            redaction_state=RedactionState.REDACTED,
        )
    service = HarnessAppService(tmp_path, store=store)

    projection = build_tui_transcript_projection(service, session.id, [])

    rendered_items = [
        str(message.get("title") or "") + "\n" + "\n".join(str(line) for line in message.get("lines", []))
        for message in projection["messages"]
    ]
    rendered = "\n---\n".join(rendered_items)
    assert rendered.index("hello") < rendered.index("old answer")
    assert rendered.index("old answer") < rendered.index("what is in this repo")
    assert "Ran model runtime model" not in rendered
    assert rendered.index("what is in this repo") < rendered.index("Ran web-search")


def test_tui_transcript_projection_renders_tool_and_permission_events(tmp_path) -> None:
    store = SQLiteStore.open_initialized(tmp_path)
    session = store.create_session(title="Event transcript")
    user = store.append_session_message(session.id, "user", "needs tool")
    store.append_store_event(
        EventStreamType.SESSION,
        session.id,
        "tool_call.started",
        {"prompt_id": "prompt_tool", "tool_id": "shell"},
        session_id=session.id,
        message_id=user.id,
        redaction_state=RedactionState.NOT_REQUIRED,
    )
    store.append_store_event(
        EventStreamType.SESSION,
        session.id,
        "harness.runtime.permission_waiting",
        {"prompt_id": "prompt_tool", "permission_id": "perm_123"},
        session_id=session.id,
        message_id=user.id,
        redaction_state=RedactionState.NOT_REQUIRED,
    )
    service = HarnessAppService(tmp_path, store=store)

    projection = build_tui_transcript_projection(service, session.id, [])

    event_lines = [line for message in projection["messages"] for line in message.get("lines", [])]
    assert "Ran shell" in event_lines
    assert "Permission waiting: perm_123" in event_lines


def test_tui_transcript_projection_groups_context_tools_as_explored(tmp_path) -> None:
    store = SQLiteStore.open_initialized(tmp_path)
    session = store.create_session(title="Context tool transcript")
    user = store.append_session_message(session.id, "user", "inspect files")
    for kind, payload in [
        ("tool_call.started", {"prompt_id": "prompt_context", "tool_id": "read", "arguments": {"path": "README.md"}}),
        ("tool_call.output", {"prompt_id": "prompt_context", "tool_id": "read", "ok": True, "preview": "# Project"}),
        ("tool_call.finished", {"prompt_id": "prompt_context", "tool_id": "read", "ok": True}),
        ("tool_call.started", {"prompt_id": "prompt_context", "tool_id": "grep", "arguments": {"pattern": "class Harness", "path": "src"}}),
        ("tool_call.started", {"prompt_id": "prompt_context", "tool_id": "ls", "arguments": {"path": "tests"}}),
        ("tool_call.started", {"prompt_id": "prompt_context", "tool_id": "shell", "arguments": {"command": "git status --short"}}),
    ]:
        store.append_store_event(
            EventStreamType.SESSION,
            session.id,
            kind,
            payload,
            session_id=session.id,
            message_id=user.id,
            redaction_state=RedactionState.REDACTED,
        )
    service = HarnessAppService(tmp_path, store=store)

    projection = build_tui_transcript_projection(service, session.id, [])

    event_lines = [line for message in projection["messages"] for line in message.get("lines", [])]
    assert event_lines[:4] == [
        "Explored",
        "- Read README.md",
        "- Search class Harness in src",
        "- List tests",
    ]
    assert "Ran git status --short" in event_lines
    assert not any(line.startswith("- Output:") for line in event_lines)


def test_tui_transcript_projection_renders_runtime_failure_events(tmp_path) -> None:
    store = SQLiteStore.open_initialized(tmp_path)
    session = store.create_session(title="Failed runtime transcript")
    user = store.append_session_message(session.id, "user", "hello")
    store.append_session_part(session.id, user.id, "text", text="hello")
    store.append_store_event(
        EventStreamType.SESSION,
        session.id,
        "model.failed",
        {
            "prompt_id": "prompt_failed",
            "error_type": "SessionRuntimeProviderUnavailable",
            "message": "No session runtime text provider is configured; refusing to use a hidden provider fallback.",
            "hidden_provider_fallback": False,
            "no_hidden_fallback": True,
        },
        session_id=session.id,
        message_id=user.id,
        redaction_state=RedactionState.REDACTED,
    )
    store.append_store_event(
        EventStreamType.SESSION,
        session.id,
        "harness.turn.finished",
        {
            "prompt_id": "prompt_failed",
            "failed": True,
            "error_type": "SessionRuntimeProviderUnavailable",
            "message": "No session runtime text provider is configured; refusing to use a hidden provider fallback.",
        },
        session_id=session.id,
        message_id=user.id,
        redaction_state=RedactionState.REDACTED,
    )
    service = HarnessAppService(tmp_path, store=store)

    projection = build_tui_transcript_projection(service, session.id, [])

    event_lines = [line for message in projection["messages"] for line in message.get("lines", [])]
    rendered = "\n".join(event_lines)
    assert "Runtime failed (SessionRuntimeProviderUnavailable)" in rendered
    assert "No session runtime text provider is configured" in rendered
    assert rendered.count("No session runtime text provider is configured") == 1


def test_tui_transcript_projection_redacts_secret_like_content_and_artifact_bodies(tmp_path) -> None:
    store = SQLiteStore.open_initialized(tmp_path)
    session = store.create_session(title="Redacted transcript")
    assistant = store.append_session_message(session.id, "assistant", "token")
    store.append_session_part(
        session.id,
        assistant.id,
        "text",
        text="OPENAI_API_KEY=sk-1234567890abcdef artifact body: should not render",
    )
    service = HarnessAppService(tmp_path, store=store)

    projection = build_tui_transcript_projection(service, session.id, [])

    rendered = "\n".join(
        [str(message.get("title") or "") for message in projection["messages"]]
        + [str(line) for message in projection["messages"] for line in message.get("lines", [])]
    )
    assert "sk-1234567890abcdef" not in rendered
    assert "should not render" not in rendered
    assert "[REDACTED_SECRET]" in rendered or "[REDACTED]" in rendered


def test_tui_transcript_projection_summarizes_raw_tool_request_json(tmp_path) -> None:
    store = SQLiteStore.open_initialized(tmp_path)
    session = store.create_session(title="Tool request transcript")
    assistant = store.append_session_message(
        session.id,
        "assistant",
        '{"type":"harness.tool_request/v1","tool":"read","reason":"Inspect README","input":{"path":"README.md"}}',
    )
    store.append_session_part(
        session.id,
        assistant.id,
        "text",
        text='{"type":"harness.tool_request/v1","tool":"read","reason":"Inspect README","input":{"path":"README.md"}}',
    )
    service = HarnessAppService(tmp_path, store=store)

    projection = build_tui_transcript_projection(service, session.id, [])

    rendered = "\n".join(
        [str(message.get("title") or "") for message in projection["messages"]]
        + [str(line) for message in projection["messages"] for line in message.get("lines", [])]
    )
    assert "Tool request: read - Inspect README" in rendered
    assert "harness.tool_request/v1" not in rendered
    assert '"input"' not in rendered
