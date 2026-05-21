import json
from pathlib import Path

from typer.testing import CliRunner

from harness.cli.main import app
from harness.config import load_config, write_default_config
from harness.core_service import HarnessAppService, HarnessCoreService
from harness.local_server import _route_get
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
from harness.session_runtime import SessionPromptExecution, SessionRuntimeManager


runner = CliRunner()


class EchoRuntimeProvider:
    def complete(self, execution: SessionPromptExecution) -> str:
        return f"echo: {execution.content}"


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
