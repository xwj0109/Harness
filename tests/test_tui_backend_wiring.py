from __future__ import annotations

import asyncio

import pytest

from harness.memory.sqlite_store import SQLiteStore
from harness.models import SessionMessageRole
from harness.operator_context import build_session_pane_projection, build_tui_dashboard
from harness.tui import create_harness_app


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
                "runtime": {"phase": "running"},
            },
            "prompt_id": "prompt_test",
            "execution_started": True,
            "permission_granting": False,
        }


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


def test_tui_plain_prompt_uses_runtime_service_path(tmp_path) -> None:
    pytest.importorskip("textual")
    from textual.widgets import TextArea

    service = RecordingMutationService(tmp_path)

    async def run_pilot() -> None:
        app = create_harness_app(tmp_path, app_service=service)
        async with app.run_test(size=(120, 40)) as pilot:
            prompt = app.query_one("#prompt", TextArea)
            prompt.value = "hello runtime"
            app.action_submit_prompt()
            await pilot.pause()
            await pilot.pause()

            assert service.created_bodies[0]["intent"] == "tui_prompt_session"
            assert service.prompt_async_calls
            call = service.prompt_async_calls[0]
            assert call["session_id"] == app._chat_state.session_id
            assert call["body"]["content"] == "hello runtime"
            assert call["body"]["source"] == "tui_prompt_submit"
            assert app._latest_response["kind"] == "runtime_prompt_submitted"
            assert app._request_in_flight is False

    asyncio.run(run_pilot())
