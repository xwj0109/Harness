from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from typer.testing import CliRunner

from harness.cli.main import app
from harness.chat import ChatSessionState, handle_chat_input
from harness.chat_model import ChatContext, ChatResponse, ChatToolCall, ChatToolSchema
from harness.backends.codex_cli import CodexRunResult
from harness.execution import execute_lease
from harness.intent_router import route_instruction
from harness.local_server import _reply_to_session_permission
from harness.memory.sqlite_store import SQLiteStore
from harness.models import (
    EventStreamType,
    SessionMessageRole,
    SessionMutationReversibility,
    SessionPartKind,
    SessionStatus,
)
from harness.session_tools import pending_session_tool_call_from_permission
from harness.session_events import SessionEventKind, append_session_event, read_session_events


runner = CliRunner()


class FakeNativeToolTaskModel:
    def __init__(self, responses: list[ChatResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    def complete_with_tools(
        self,
        messages: list,
        context: ChatContext,
        tools: list[ChatToolSchema],
    ) -> ChatResponse:
        self.calls.append({"messages": list(messages), "context": context, "tools": list(tools)})
        if not self.responses:
            return ChatResponse(content="Done.")
        return self.responses.pop(0)

    def stream(self, _messages, _context):
        raise AssertionError("task operator should use provider-native tool calls")

    def complete(self, _messages, _context):
        raise AssertionError("task operator should use provider-native tool calls")


def _operator_task(store: SQLiteStore, title: str, *, metadata: dict | None = None):
    base = {
        "execution_adapter": "session_read_tools",
        "task_type": "session_plan",
        "allowed_tools": ["read", "glob", "grep", "git-diff", "pwd"],
        "required_artifact_kinds": ["final_report"],
    }
    if metadata:
        base.update(metadata)
    return store.create_task(title, metadata=base)


def test_session_create_attach_and_transcript_round_trip(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    session = store.create_session(workbench_id="coding", agent_id="repo_inspector", intent="explain_repo")
    objective = store.create_objective("Explain repo", workbench_id="coding", session_id=session.id)
    task = store.create_task("Inspect", objective_id=objective.id, session_id=session.id)
    run = store.create_run("Explain", "read_only_repo_summary", task_id=task.id, objective_id=objective.id, session_id=session.id)

    store.append_event(run.id, "info", "test", "event")
    artifact = store.register_artifact(run.id, "final_report", store.initialize_run_artifacts(run.id)["final_report"])
    append_session_event(
        tmp_path,
        session_id=session.id,
        run_id=run.id,
        task_id=task.id,
        objective_id=objective.id,
        event_type=SessionEventKind.REPORT_READY,
        message="Report ready",
    )

    loaded = store.get_session(session.id)
    assert loaded.objective_id == objective.id
    assert loaded.active_task_id == task.id
    assert loaded.active_run_id == run.id
    assert store.get_run(run.id).session_id == session.id
    assert store.get_task(task.id).session_id == session.id
    assert store.get_objective(objective.id).session_id == session.id
    assert store.list_events(run.id)[0].session_id == session.id
    assert artifact.session_id == session.id
    assert read_session_events(tmp_path, session.id)[0].event_type == SessionEventKind.REPORT_READY


def test_task_runs_read_only_operator_loop_and_records_linkage(tmp_path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    (tmp_path / "README.md").write_text("Harness task bridge\n", encoding="utf-8")
    store = SQLiteStore(tmp_path)
    task = _operator_task(store, "Inspect README with session tools", metadata={"allowed_tools": ["read"]})
    selection = store.select_next_task_for_lease()
    assert selection is not None
    model = FakeNativeToolTaskModel(
        [
            ChatResponse(
                content="I will read the file.",
                tool_calls=[ChatToolCall(id="call_read", name="read", arguments={"path": "README.md"})],
            ),
            ChatResponse(content="README inspected."),
        ]
    )
    monkeypatch.setattr("harness.task_operator_bridge.build_default_chat_model", lambda _project_root: model)

    result = execute_lease(tmp_path, selection["lease"].id)

    assert result.ok is True
    assert result.decision == "operator_task_completed"
    assert result.adapter_id == "session_read_tools"
    assert result.task.status.value == "succeeded"
    assert result.attempt.run_id == result.run.id
    assert result.attempt.metadata["task_idempotency_key"] == task.idempotency_key
    assert result.attempt.metadata["attempt_idempotency_key"].endswith(":attempt:1")
    assert result.run.task_id == task.id
    assert result.manifest.task_id == task.id
    assert {artifact.kind for artifact in result.manifest.artifacts} >= {"final_report", "operator_tool_result_index"}
    assert result.adapter_result["turn_id"]
    assert result.adapter_result["tool_results"][0]["tool"] == "read"
    session = store.get_session(result.run.session_id)
    assert session.active_task_id == task.id
    assert session.active_run_id == result.run.id
    events = store.list_session_store_events(session.id)
    assert any(event.kind == "operator.turn.started" for event in events)
    assert any(event.kind == "harness.task_operator.completed" for event in events)


def test_task_operator_loop_pauses_for_shell_approval_and_resumes_once(tmp_path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    command = f"{sys.executable} -c \"print('task approval ran')\""
    task = _operator_task(
        store,
        "Run approved task command",
        metadata={"allowed_tools": ["shell"], "required_artifact_kinds": ["final_report"]},
    )
    selection = store.select_next_task_for_lease()
    assert selection is not None
    model = FakeNativeToolTaskModel(
        [
            ChatResponse(
                content="I need shell approval.",
                tool_calls=[
                    ChatToolCall(
                        id="call_shell",
                        name="shell",
                        arguments={"command": command, "timeout_seconds": 30, "shell_executable": "/bin/sh"},
                    )
                ],
            )
        ]
    )
    monkeypatch.setattr("harness.task_operator_bridge.build_default_chat_model", lambda _project_root: model)

    paused = execute_lease(tmp_path, selection["lease"].id)

    assert paused.ok is False
    assert paused.decision == "operator_task_waiting_approval"
    assert paused.approval_id
    assert paused.task.status.value == "waiting_approval"
    assert paused.attempt.status.value == "waiting_approval"
    assert paused.lease.status.value == "active"
    assert "Shell command executed." not in json.dumps(paused.model_dump(mode="json"))

    reply = _reply_to_session_permission(
        store,
        paused.run.session_id,
        paused.approval_id,
        {"decision": "allow", "message": "approve this exact command"},
        project_root=tmp_path,
        resume=True,
    )

    assert reply["decision"] == "allowed"
    assert reply["tool_execution_started"] is True
    assert reply["task_operator_resume"]["decision"] == "operator_task_completed"
    assert reply["task_operator_resume"]["task_id"] == task.id
    assert store.get_task(task.id).status.value == "succeeded"
    assert store.get_task_attempt(paused.attempt.id).status.value == "succeeded"
    assert store.get_task_lease(paused.lease.id).status.value == "released"
    assert store.get_session_permission(paused.approval_id).status.value == "expired"
    events = store.list_session_store_events(paused.run.session_id)
    shell_outputs = [
        event
        for event in events
        if event.kind == "tool_call.output" and isinstance(event.payload, dict) and event.payload.get("tool_id") == "shell"
    ]
    assert len(shell_outputs) == 2
    assert sum("Shell command executed." in event.payload.get("preview", "") for event in shell_outputs) == 1


def test_task_operator_denial_fails_waiting_task(tmp_path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    task = _operator_task(store, "Deny unsafe command", metadata={"allowed_tools": ["shell"]})
    selection = store.select_next_task_for_lease()
    assert selection is not None
    model = FakeNativeToolTaskModel(
        [
            ChatResponse(
                content="I need shell approval.",
                tool_calls=[
                    ChatToolCall(
                        id="call_shell",
                        name="shell",
                        arguments={"command": "echo denied", "timeout_seconds": 30},
                    )
                ],
            )
        ]
    )
    monkeypatch.setattr("harness.task_operator_bridge.build_default_chat_model", lambda _project_root: model)
    paused = execute_lease(tmp_path, selection["lease"].id)

    reply = _reply_to_session_permission(
        store,
        paused.run.session_id,
        paused.approval_id,
        {"decision": "deny", "message": "needs a narrower command"},
        project_root=tmp_path,
        resume=True,
    )

    assert reply["decision"] == "denied"
    assert reply["tool_execution_started"] is False
    assert reply["task_operator_resume"]["decision"] == "operator_task_approval_denied"
    assert store.get_task(task.id).status.value == "failed"
    attempt = store.get_task_attempt(paused.attempt.id)
    assert attempt.status.value == "failed"
    assert attempt.failure_code == "approval_denied"


def test_task_operator_chat_approval_resumes_waiting_task_once(tmp_path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    command = f"{sys.executable} -c \"print('chat task approval ran')\""
    task = _operator_task(
        store,
        "Run chat approved task command",
        metadata={"allowed_tools": ["shell"], "required_artifact_kinds": ["final_report"]},
    )
    selection = store.select_next_task_for_lease()
    assert selection is not None
    model = FakeNativeToolTaskModel(
        [
            ChatResponse(
                content="I need shell approval.",
                tool_calls=[
                    ChatToolCall(
                        id="call_shell",
                        name="shell",
                        arguments={"command": command, "timeout_seconds": 30, "shell_executable": "/bin/sh"},
                    )
                ],
            )
        ]
    )
    monkeypatch.setattr("harness.task_operator_bridge.build_default_chat_model", lambda _project_root: model)
    paused = execute_lease(tmp_path, selection["lease"].id)
    pending = pending_session_tool_call_from_permission(store, paused.run.session_id, paused.approval_id)
    assert pending is not None

    state = ChatSessionState(session_id=paused.run.session_id, active_project_root=str(tmp_path))
    pending["project_root"] = str(tmp_path)
    state.pending_session_tool_call = pending
    response = handle_chat_input("yes", tmp_path, state)

    assert response["kind"] == "session_tool_result"
    assert response["ok"] is True
    assert response["task_operator_resume"]["decision"] == "operator_task_completed"
    assert response["task_operator_resume"]["task_id"] == task.id
    assert store.get_task(task.id).status.value == "succeeded"
    assert store.get_task_attempt(paused.attempt.id).status.value == "succeeded"
    assert store.get_task_lease(paused.lease.id).status.value == "released"
    assert store.get_session_permission(paused.approval_id).status.value == "expired"
    shell_outputs = [
        event
        for event in store.list_session_store_events(paused.run.session_id)
        if event.kind == "tool_call.output" and isinstance(event.payload, dict) and event.payload.get("tool_id") == "shell"
    ]
    assert len(shell_outputs) == 2
    assert sum("Shell command executed." in event.payload.get("preview", "") for event in shell_outputs) == 1


def test_task_operator_chat_denial_fails_waiting_task(tmp_path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    task = _operator_task(store, "Deny from chat", metadata={"allowed_tools": ["shell"]})
    selection = store.select_next_task_for_lease()
    assert selection is not None
    model = FakeNativeToolTaskModel(
        [
            ChatResponse(
                content="I need shell approval.",
                tool_calls=[ChatToolCall(id="call_shell", name="shell", arguments={"command": "echo denied", "timeout_seconds": 30})],
            )
        ]
    )
    monkeypatch.setattr("harness.task_operator_bridge.build_default_chat_model", lambda _project_root: model)
    paused = execute_lease(tmp_path, selection["lease"].id)
    pending = pending_session_tool_call_from_permission(store, paused.run.session_id, paused.approval_id)
    assert pending is not None

    state = ChatSessionState(session_id=paused.run.session_id, active_project_root=str(tmp_path))
    pending["project_root"] = str(tmp_path)
    state.pending_session_tool_call = pending
    response = handle_chat_input("no needs a narrower command", tmp_path, state)

    assert response["kind"] == "declined"
    assert response["denial"]["task_operator_resume"]["decision"] == "operator_task_approval_denied"
    assert store.get_task(task.id).status.value == "failed"
    attempt = store.get_task_attempt(paused.attempt.id)
    assert attempt.status.value == "failed"
    assert attempt.failure_code == "approval_denied"
    shell_outputs = [
        event
        for event in store.list_session_store_events(paused.run.session_id)
        if event.kind == "tool_call.output" and isinstance(event.payload, dict) and event.payload.get("tool_id") == "shell"
    ]
    assert sum("Shell command executed." in event.payload.get("preview", "") for event in shell_outputs) == 0


def test_task_operator_missing_expected_artifact_fails_but_preserves_retry_evidence(tmp_path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    task = _operator_task(
        store,
        "Produce impossible artifact",
        metadata={"allowed_tools": ["pwd"], "required_artifact_kinds": ["nonexistent_artifact"]},
    )
    first_selection = store.select_next_task_for_lease()
    assert first_selection is not None
    model = FakeNativeToolTaskModel(
        [
            ChatResponse(content="I will check pwd.", tool_calls=[ChatToolCall(id="call_pwd", name="pwd", arguments={})]),
            ChatResponse(content="Done without the required artifact."),
        ]
    )
    monkeypatch.setattr("harness.task_operator_bridge.build_default_chat_model", lambda _project_root: model)

    failed = execute_lease(tmp_path, first_selection["lease"].id)

    assert failed.ok is False
    assert failed.decision == "operator_task_missing_expected_artifact"
    assert store.get_task(task.id).status.value == "failed"
    assert {artifact.kind for artifact in store.list_artifacts(failed.run.id)} >= {"final_report", "operator_tool_result_index"}
    first_attempt = store.get_task_attempt(failed.attempt.id)
    assert first_attempt.failure_code == "operator_task_missing_expected_artifact"

    retried = store.retry_task(task.id)
    second_selection = store.select_next_task_for_lease()

    assert retried.status.value == "ready"
    assert second_selection is not None
    assert second_selection["attempt"].attempt_number == 2
    assert second_selection["attempt"].metadata["task_idempotency_key"] == task.idempotency_key
    assert second_selection["attempt"].metadata["attempt_idempotency_key"].endswith(":attempt:2")
    assert store.list_artifacts(failed.run.id)


def test_duplicate_run_next_does_not_lease_same_operator_task_twice(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    task = _operator_task(store, "Lease once")

    first = store.daemon_run_once(owner="test-daemon", pid=123)
    second = store.daemon_run_once(owner="test-daemon", pid=123)

    assert first.decision == "leased_task"
    assert first.selected_task.id == task.id
    assert second.decision == "renewed_lease"
    assert second.selected_task is None
    assert second.lease.id == first.lease.id
    assert len(store.list_task_attempts(task.id)) == 1


def test_session_spine_persists_messages_parts_events_and_archive_export(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    session = store.create_session(title="Spine", intent="plan_change", raw_model_ref="provider/model")
    message = store.append_session_message(session.id, SessionMessageRole.USER, "Plan this change")
    part = store.append_session_part(session.id, message.id, SessionPartKind.TEXT, text="Plan this change")
    run = store.create_run("Direct edit", "codex_code_edit", session_id=session.id)
    assistant = store.append_session_message(
        session.id,
        SessionMessageRole.ASSISTANT,
        "Changed active workspace",
        run_id=run.id,
        mutation_reversibility=SessionMutationReversibility.NOT_REVERSIBLE_ACTIVE_WORKSPACE,
    )

    events = store.list_store_events(EventStreamType.SESSION, session.id)
    assert [event.kind for event in events][:3] == [
        "session.created",
        "session.message.appended",
        "session.part.appended",
    ]
    created_event = events[0]
    assert created_event.payload["raw_model_ref"] == "provider/model"
    assert created_event.payload["provider_id"] is None
    assert created_event.payload["model_id"] is None
    assert created_event.payload["model_variant"] is None
    assert created_event.payload["model_selection_source"] == "session_create"
    assert created_event.payload["model_override_persisted"] is True
    assert created_event.payload["provider_execution_started"] is False
    assert created_event.payload["model_execution_started"] is False
    assert created_event.payload["hidden_provider_fallback"] is False
    assert created_event.payload["hidden_model_fallback"] is False
    assert created_event.payload["no_hidden_fallback"] is True
    assert created_event.payload["permission_granting"] is False
    assert created_event.payload["authority_granting"] is False
    assert store.list_session_messages(session.id)[0].id == message.id
    assert store.list_session_parts(session.id)[0].id == part.id
    assert store.list_session_messages(session.id)[1].id == assistant.id
    assert store.list_session_messages(session.id)[1].mutation_reversibility == (
        SessionMutationReversibility.NOT_REVERSIBLE_ACTIVE_WORKSPACE
    )

    archived = runner.invoke(app, ["session", "archive", session.id, "--project", str(tmp_path), "--output", "json"])
    exported = runner.invoke(app, ["session", "export", session.id, "--project", str(tmp_path), "--output", "json"])

    assert archived.exit_code == 0, archived.output
    assert exported.exit_code == 0, exported.output
    archived_payload = json.loads(archived.output)
    export_payload = json.loads(exported.output)
    assert archived_payload["session"]["status"] == SessionStatus.ARCHIVED.value
    assert export_payload["include_artifacts"] is False
    assert [exported_message["id"] for exported_message in export_payload["messages"]] == [message.id, assistant.id]
    assert "session.archived" in {event["kind"] for event in export_payload["events"]}


def test_session_fork_cli_creates_child_session(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    parent = store.create_session(title="Parent")
    message = store.append_session_message(parent.id, SessionMessageRole.USER, "Fork here")

    result = runner.invoke(
        app,
        [
            "session",
            "fork",
            parent.id,
            "--message",
            message.id,
            "--title",
            "Child",
            "--project",
            str(tmp_path),
            "--output",
            "json",
        ],
    )
    children = runner.invoke(app, ["session", "children", parent.id, "--project", str(tmp_path), "--output", "json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    child = payload["session"]
    assert child["parent_session_id"] == parent.id
    assert child["forked_from_message_id"] == message.id
    assert child["title"] == "Child"
    assert "session.forked" in {event.kind for event in store.list_store_events(EventStreamType.SESSION, child["id"])}
    assert children.exit_code == 0, children.output
    children_payload = json.loads(children.output)
    assert children_payload["schema_version"] == "harness.session_children/v1"
    assert children_payload["session_id"] == parent.id
    assert children_payload["child_session_ids"] == [child["id"]]
    assert children_payload["children"][0]["parent_session_id"] == parent.id
    assert children_payload["execution_started"] is False
    assert children_payload["permission_granting"] is False


def test_session_status_and_abort_are_append_only_metadata_contracts(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    parent = store.create_session(title="Abort parent")
    child = store.fork_session(parent.id, title="Abort child")
    message = store.append_session_message(parent.id, SessionMessageRole.USER, "Abort this")
    store.append_session_part(parent.id, message.id, SessionPartKind.TEXT, text="Abort this")

    before = runner.invoke(app, ["session", "status", parent.id, "--project", str(tmp_path), "--output", "json"])
    aborted = runner.invoke(
        app,
        ["session", "abort", parent.id, "--reason", "operator stopped waiting", "--project", str(tmp_path), "--output", "json"],
    )
    after = runner.invoke(app, ["session", "status", parent.id, "--project", str(tmp_path), "--output", "json"])
    tail = runner.invoke(app, ["events", parent.id, "--project", str(tmp_path)])

    assert before.exit_code == 0, before.output
    before_payload = json.loads(before.output)
    assert before_payload["schema_version"] == "harness.session_status/v1"
    assert before_payload["status"] == SessionStatus.ACTIVE.value
    assert before_payload["message_count"] == 1
    assert before_payload["child_session_ids"] == [child.id]
    assert before_payload["terminal"] is False
    assert before_payload["process_running"] is False
    assert before_payload["permission_granting"] is False

    assert aborted.exit_code == 0, aborted.output
    abort_payload = json.loads(aborted.output)
    assert abort_payload["schema_version"] == "harness.session_abort/v1"
    assert abort_payload["session"]["status"] == SessionStatus.CANCELLED.value
    assert abort_payload["process_stopped"] is False
    assert abort_payload["run_cancelled"] is False
    assert abort_payload["task_cancelled"] is False
    assert abort_payload["permission_granting"] is False

    assert after.exit_code == 0, after.output
    after_payload = json.loads(after.output)
    assert after_payload["status"] == SessionStatus.CANCELLED.value
    assert after_payload["terminal"] is True
    assert after_payload["event_count"] == before_payload["event_count"] + 1
    assert tail.exit_code == 0, tail.output
    assert "session.cancelled" in tail.output or "Session cancelled" in tail.output
    events = store.list_session_store_events(parent.id)
    assert [event.kind for event in events].count("session.cancelled") == 1


def test_session_summary_and_token_rollup_are_mutable_projection_events(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    session = store.create_session(title="Summary rollup")

    summarized = runner.invoke(
        app,
        [
            "session",
            "summarize",
            session.id,
            "--summary",
            "Investigated the failing parser path.",
            "--input-tokens",
            "120",
            "--output-tokens",
            "35",
            "--reasoning-tokens",
            "7",
            "--cache-read-tokens",
            "5",
            "--cache-write-tokens",
            "2",
            "--estimated-cost-usd",
            "0.0123",
            "--project",
            str(tmp_path),
            "--output",
            "json",
        ],
    )
    status = runner.invoke(app, ["session", "status", session.id, "--project", str(tmp_path), "--output", "json"])
    tail = runner.invoke(app, ["events", session.id, "--project", str(tmp_path)])

    assert summarized.exit_code == 0, summarized.output
    payload = json.loads(summarized.output)
    assert payload["schema_version"] == "harness.session_summary/v1"
    assert payload["mutable_projection"] is True
    assert payload["permission_granting"] is False
    updated = payload["session"]
    assert updated["summary"] == "Investigated the failing parser path."
    assert updated["token_input"] == 120
    assert updated["token_output"] == 35
    assert updated["token_reasoning"] == 7
    assert updated["token_cache_read"] == 5
    assert updated["token_cache_write"] == 2
    assert updated["estimated_cost_usd"] == "0.0123"
    assert status.exit_code == 0, status.output
    status_payload = json.loads(status.output)
    assert status_payload["summary"] == "Investigated the failing parser path."
    assert status_payload["token_input"] == 120
    assert status_payload["estimated_cost_usd"] == "0.0123"
    assert tail.exit_code == 0, tail.output
    assert "session.summary_updated" in tail.output or "Summary updated" in tail.output
    events = store.list_session_store_events(session.id)
    assert [event.kind for event in events].count("session.summary_updated") == 1


def test_session_message_retraction_and_part_correction_are_event_only(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    session = store.create_session(title="Correction session")
    message = store.append_session_message(session.id, SessionMessageRole.USER, "Original prompt")
    part = store.append_session_part(session.id, message.id, SessionPartKind.TEXT, text="Original prompt")

    retracted = runner.invoke(
        app,
        [
            "session",
            "retract-message",
            session.id,
            message.id,
            "--reason",
            "superseded",
            "--project",
            str(tmp_path),
            "--output",
            "json",
        ],
    )
    corrected = runner.invoke(
        app,
        [
            "session",
            "correct-part",
            session.id,
            part.id,
            "--text",
            "Corrected prompt",
            "--reason",
            "typo",
            "--project",
            str(tmp_path),
            "--output",
            "json",
        ],
    )
    transcript = runner.invoke(app, ["session", "transcript", session.id, "--project", str(tmp_path), "--format", "jsonl"])
    tail = runner.invoke(app, ["events", session.id, "--project", str(tmp_path)])

    assert retracted.exit_code == 0, retracted.output
    retracted_payload = json.loads(retracted.output)
    assert retracted_payload["schema_version"] == "harness.session_message_retraction/v1"
    assert retracted_payload["message_mutated"] is False
    assert retracted_payload["parts_mutated"] is False
    assert retracted_payload["permission_granting"] is False
    assert corrected.exit_code == 0, corrected.output
    corrected_payload = json.loads(corrected.output)
    assert corrected_payload["schema_version"] == "harness.session_part_correction/v1"
    assert corrected_payload["part_mutated"] is False
    assert corrected_payload["message_mutated"] is False
    assert corrected_payload["permission_granting"] is False
    assert transcript.exit_code == 0, transcript.output
    transcript_payload = json.loads(transcript.output.splitlines()[0])
    assert transcript_payload["message"]["content_preview"] == "Original prompt"
    assert transcript_payload["parts"][0]["text"] == "Original prompt"
    assert tail.exit_code == 0, tail.output
    assert "Message retracted" in tail.output
    assert "Part corrected" in tail.output
    events = store.list_session_store_events(session.id)
    assert [event.kind for event in events].count("session.message.retracted") == 1
    assert [event.kind for event in events].count("session.part.corrected") == 1


def test_session_changed_files_cli_summarizes_diff_artifacts_without_mutation(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    session = store.create_session(title="Changed files")
    run = store.create_run("Diff run", "codex_isolated_edit", session_id=session.id)
    diff_path = store.runs_dir / run.id / "isolated_unified_diff.patch"
    diff_path.parent.mkdir(parents=True, exist_ok=True)
    diff_path.write_text("diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@\n-old\n+new\n", encoding="utf-8")
    artifact = store.register_artifact(run.id, "isolated_unified_diff", diff_path, session_id=session.id)

    result = runner.invoke(app, ["session", "changed-files", session.id, "--project", str(tmp_path), "--output", "json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema_version"] == "harness.session_changed_files/v1"
    assert payload["session_id"] == session.id
    assert payload["file_count"] == 1
    assert payload["files"][0]["path"] == "app.py"
    assert payload["files"][0]["sources"] == ["diff_artifact"]
    assert payload["files"][0]["diff_artifact_ids"] == [artifact.id]
    assert payload["files"][0]["contents_included"] is False
    assert payload["contents_included"] is False
    assert payload["mutation_started"] is False
    assert payload["revert_supported"] is False
    assert payload["selected_hunk_apply_supported"] is False
    assert payload["permission_granting"] is False


def test_session_tail_and_transcript_render_from_persisted_events(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    session = store.create_session(title="Timeline")
    message = store.append_session_message(session.id, SessionMessageRole.USER, "Inspect this file")
    store.append_session_part(session.id, message.id, SessionPartKind.TEXT, text="Inspect this file")
    assistant = store.append_session_message(session.id, SessionMessageRole.ASSISTANT, "Done")
    store.append_session_part(session.id, assistant.id, SessionPartKind.SUMMARY, text="Done")

    tail = runner.invoke(app, ["session", "tail", session.id, "--project", str(tmp_path)])
    tail_jsonl = runner.invoke(app, ["session", "tail", session.id, "--project", str(tmp_path), "--jsonl"])
    generic_events = runner.invoke(app, ["events", session.id, "--project", str(tmp_path)])
    generic_events_jsonl = runner.invoke(app, ["events", session.id, "--project", str(tmp_path), "--jsonl"])
    transcript = runner.invoke(app, ["session", "transcript", session.id, "--project", str(tmp_path)])
    transcript_jsonl = runner.invoke(
        app,
        ["session", "transcript", session.id, "--project", str(tmp_path), "--format", "jsonl"],
    )

    assert tail.exit_code == 0, tail.output
    assert tail_jsonl.exit_code == 0, tail_jsonl.output
    assert generic_events.exit_code == 0, generic_events.output
    assert generic_events_jsonl.exit_code == 0, generic_events_jsonl.output
    assert transcript.exit_code == 0, transcript.output
    assert transcript_jsonl.exit_code == 0, transcript_jsonl.output
    assert "Session created" in tail.output
    assert "Message appended" in tail.output
    assert generic_events.output == tail.output
    assert generic_events_jsonl.output == tail_jsonl.output
    first_event = json.loads(tail_jsonl.output.splitlines()[0])
    assert first_event["schema_version"] == "harness.event/v2"
    assert "\x1b[" not in tail_jsonl.output
    assert f"user {message.id}" in transcript.output
    assert "Inspect this file" in transcript.output
    first_entry = json.loads(transcript_jsonl.output.splitlines()[0])
    assert first_entry["schema_version"] == "harness.session_transcript_entry/v1"
    assert first_entry["message"]["id"] == message.id
    assert "\x1b[" not in transcript_jsonl.output


def test_foreground_prompt_creates_session_and_records_direct_run_messages(tmp_path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=tmp_path, check=True, capture_output=True)

    class FakeCodexBackend:
        def __init__(self, config):
            self.config = config
            self.name = config.name

        def preflight(self):
            from harness.models import BackendStatus

            return BackendStatus(available=True, metadata=self.config.metadata, capabilities=self.config.capabilities)

        def run_direct_agent(self, project_root, prompt, final_message_path, *, model=None, reasoning_effort=None):
            assert prompt == "change value"
            assert model == "codex_cli/gpt-5.5"
            (Path(project_root) / "app.py").write_text("value = 2\n", encoding="utf-8")
            final_message_path.write_text("Changed the value.", encoding="utf-8")
            self.config.settings["last_codex_approval_mode"] = "on-request via --ask-for-approval"
            self.config.settings["last_codex_sandbox_mode"] = "workspace-write"
            self.config.settings["last_apply_back_approval_required"] = False
            return (
                CodexRunResult(["codex", "exec", prompt], "", "", 0, [], "Changed the value."),
                self.config.capabilities,
                "",
            )

    monkeypatch.setattr("harness.cli.main.CodexCliBackend", FakeCodexBackend)
    result = runner.invoke(
        app,
        [
            "change value",
            "--project",
            str(tmp_path),
            "--title",
            "Direct session",
            "--agent",
            "build",
            "--mode",
            "direct",
            "--model",
            "codex_cli/gpt-5.5",
            "--file",
            "app.py",
            "--no-stream",
            "--output",
            "json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    session_id = payload["session_id"]
    store = SQLiteStore(tmp_path)
    session = store.get_session(session_id)
    messages = store.list_session_messages(session_id)
    parts = store.list_session_parts(session_id)
    assert payload["run_id"] == session.active_run_id
    assert session.title == "Direct session"
    assert session.agent_id == "build"
    assert session.provider_id == "codex_cli"
    assert session.model_id == "gpt-5.5"
    assert session.model_variant is None
    model_events = [event for event in store.list_session_store_events(session_id) if event.kind in {"session.created", "session.model_selected"}]
    assert model_events[0].payload["raw_model_ref"] == "codex_cli/gpt-5.5"
    assert model_events[0].payload["provider_id"] == "codex_cli"
    assert model_events[0].payload["model_id"] == "gpt-5.5"
    assert model_events[0].payload["model_variant"] is None
    assert model_events[0].payload["model_selection_source"] == "session_create"
    assert all(event.payload["no_hidden_fallback"] is True for event in model_events)
    assert all(event.payload["provider_execution_started"] is False for event in model_events)
    assert all(event.payload["model_execution_started"] is False for event in model_events)
    assert all(event.payload["hidden_provider_fallback"] is False for event in model_events)
    assert all(event.payload["hidden_model_fallback"] is False for event in model_events)
    validation_events = [event for event in store.list_session_store_events(session_id) if event.kind == "session.model_validation"]
    assert validation_events
    assert validation_events[-1].payload["raw_model_ref"] == "codex_cli/gpt-5.5"
    assert validation_events[-1].payload["executable"] is True
    assert validation_events[-1].payload["known_catalog_entry"] is True
    assert validation_events[-1].payload["provider_execution_started"] is False
    assert validation_events[-1].payload["hidden_provider_fallback"] is False
    assert validation_events[-1].payload["no_hidden_fallback"] is True
    assert [message.role for message in messages] == [SessionMessageRole.USER, SessionMessageRole.ASSISTANT]
    assert messages[1].mutation_reversibility == SessionMutationReversibility.NOT_REVERSIBLE_ACTIVE_WORKSPACE
    assert any(part.metadata.get("attachment_kind") == "file_ref" for part in parts)
    artifact_parts = [part for part in parts if part.kind == SessionPartKind.ARTIFACT_REF and part.artifact_id]
    assert artifact_parts
    assert any(part.metadata.get("kind") == "codex_final_message" for part in artifact_parts)
    session_event_kinds = {event.kind for event in store.list_session_store_events(session_id)}
    assert "artifact.registered" in session_event_kinds
    transcript = runner.invoke(app, ["session", "transcript", session_id, "--project", str(tmp_path)])
    assert transcript.exit_code == 0, transcript.output
    assert "artifact=" in transcript.output


def test_foreground_prompt_unknown_model_fails_closed_after_session_persistence(tmp_path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0

    class FakeCodexBackend:
        def __init__(self, config):
            raise AssertionError("backend must not be constructed after failed model validation")

    monkeypatch.setattr("harness.cli.main.CodexCliBackend", FakeCodexBackend)
    result = runner.invoke(
        app,
        [
            "change value",
            "--project",
            str(tmp_path),
            "--mode",
            "direct",
            "--model",
            "codex_cli/not-a-real-model",
            "--no-stream",
            "--output",
            "json",
        ],
    )

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["status"] == "model_validation_failed"
    assert payload["run_id"] is None
    assert payload["provider_execution_started"] is False
    assert payload["model_execution_started"] is False
    assert payload["hidden_provider_fallback"] is False
    assert payload["hidden_model_fallback"] is False
    assert payload["no_hidden_fallback"] is True
    assert payload["model_validation"]["blocked_reasons"] == ["model_unknown"]
    store = SQLiteStore(tmp_path)
    session = store.get_session(payload["session_id"])
    assert session.raw_model_ref == "codex_cli/not-a-real-model"
    assert session.provider_id == "codex_cli"
    assert session.model_id == "not-a-real-model"
    assert session.active_run_id is None
    events = store.list_session_store_events(session.id)
    validation = [event for event in events if event.kind == "session.model_validation"][-1]
    assert validation.payload["executable"] is False
    assert validation.payload["provider_execution_started"] is False
    assert validation.payload["blocked_reasons"] == ["model_unknown"]
    assert any(part.metadata.get("status") == "model_validation_failed" for part in store.list_session_parts(session.id))


def test_foreground_build_agent_creates_isolated_task_by_default(tmp_path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0

    class ExplodingCodexBackend:
        def __init__(self, config):
            raise AssertionError("build agent should not use direct active-workspace Codex by default")

    monkeypatch.setattr("harness.cli.main.CodexCliBackend", ExplodingCodexBackend)
    result = runner.invoke(
        app,
        [
            "change value",
            "--project",
            str(tmp_path),
            "--agent",
            "build",
            "--model",
            "codex_cli/gpt-5.5",
            "--title",
            "Build safely",
            "--output",
            "json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    session = payload["session"]
    task = payload["task"]
    assert payload["schema_version"] == "harness.native_agent_session/v1"
    assert session["agent_id"] == "build"
    assert session["status"] == SessionStatus.WAITING_APPROVAL.value
    assert session["active_task_id"] == task["id"]
    assert task["agent_id"] == "build"
    assert task["metadata"]["execution_adapter"] == "codex_isolated_edit"
    assert task["metadata"]["task_type"] == "codex_code_edit"
    assert task["metadata"]["direct_active_workspace"] is False
    assert task["required_approvals"] == ["hosted_provider_codex"]
    store = SQLiteStore(tmp_path)
    validation = [event for event in store.list_session_store_events(session["id"]) if event.kind == "session.model_validation"][-1]
    assert validation.payload["raw_model_ref"] == "codex_cli/gpt-5.5"
    assert validation.payload["executable"] is True
    assert validation.payload["known_catalog_entry"] is True
    assert validation.payload["provider_execution_started"] is False
    assert validation.payload["hidden_provider_fallback"] is False
    assert validation.payload["no_hidden_fallback"] is True
    messages = store.list_session_messages(session["id"])
    assert [message.role for message in messages] == [SessionMessageRole.USER]
    event_kinds = [event.kind for event in store.list_session_store_events(session["id"])]
    assert "agent.selected" in event_kinds
    assert "run.blocked" in event_kinds


def test_foreground_native_agent_unknown_model_fails_closed_before_task_creation(tmp_path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0

    class ExplodingCodexBackend:
        def __init__(self, config):
            raise AssertionError("native agent model validation must not construct Codex backend")

    monkeypatch.setattr("harness.cli.main.CodexCliBackend", ExplodingCodexBackend)
    result = runner.invoke(
        app,
        [
            "change value",
            "--project",
            str(tmp_path),
            "--agent",
            "build",
            "--model",
            "codex_cli/not-a-real-model",
            "--output",
            "json",
        ],
    )

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["schema_version"] == "harness.native_agent_session/v1"
    assert payload["ok"] is False
    assert payload["status"] == "model_validation_failed"
    assert payload["task"] is None
    assert payload["model_validation"]["blocked_reasons"] == ["model_unknown"]
    assert payload["provider_execution_started"] is False
    assert payload["model_execution_started"] is False
    assert payload["hidden_provider_fallback"] is False
    assert payload["hidden_model_fallback"] is False
    assert payload["no_hidden_fallback"] is True
    store = SQLiteStore(tmp_path)
    session = store.get_session(payload["session"]["id"])
    assert session.raw_model_ref == "codex_cli/not-a-real-model"
    assert session.provider_id == "codex_cli"
    assert session.model_id == "not-a-real-model"
    assert session.active_task_id is None
    assert store.list_tasks() == []
    validation = [event for event in store.list_session_store_events(session.id) if event.kind == "session.model_validation"][-1]
    assert validation.payload["executable"] is False
    assert validation.payload["provider_execution_started"] is False
    assert validation.payload["blocked_reasons"] == ["model_unknown"]

    messages = store.list_session_messages(session.id)
    assert [message.role for message in messages] == [SessionMessageRole.USER]
    event_kinds = [event.kind for event in store.list_session_store_events(session.id)]
    assert "agent.selected" not in event_kinds
    assert "run.blocked" not in event_kinds


def test_foreground_plan_mention_creates_read_only_session_task(tmp_path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0

    class ExplodingCodexBackend:
        def __init__(self, config):
            raise AssertionError("plan agent should not call Codex directly")

    monkeypatch.setattr("harness.cli.main.CodexCliBackend", ExplodingCodexBackend)
    result = runner.invoke(app, ["@plan inspect auth flow", "--project", str(tmp_path), "--output", "json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    session = payload["session"]
    task = payload["task"]
    assert session["agent_id"] == "plan"
    assert session["status"] == SessionStatus.IDLE.value
    assert task["metadata"]["execution_adapter"] == "session_read_tools"
    assert task["metadata"]["task_type"] == "session_plan"
    assert task["metadata"]["active_repo_write"] == "forbidden"
    assert task["metadata"]["external_network"] == "forbidden"
    assert task["metadata"]["allowed_tools"] == ["read", "glob", "grep", "artifact-read"]
    assert task["required_approvals"] == []

    store = SQLiteStore(tmp_path)
    messages = store.list_session_messages(session["id"])
    assert messages[0].content_preview == "inspect auth flow"


def test_foreground_general_mention_creates_child_subagent_artifact_summary(tmp_path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0

    class ExplodingCodexBackend:
        def __init__(self, config):
            raise AssertionError("general subagent should not call Codex directly")

    monkeypatch.setattr("harness.cli.main.CodexCliBackend", ExplodingCodexBackend)
    result = runner.invoke(app, ["@general investigate auth flow", "--project", str(tmp_path), "--output", "json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    parent_session = payload["parent_session"]
    child_session = payload["session"]
    task = payload["task"]
    run = payload["run"]
    artifact = payload["artifact"]
    assert payload["schema_version"] == "harness.native_agent_session/v1"
    assert payload["subagent_branch"] is True
    assert payload["provider_execution_started"] is False
    assert payload["shell_started"] is False
    assert payload["network_started"] is False
    assert payload["active_repo_write"] == "forbidden"
    assert child_session["parent_session_id"] == parent_session["id"]
    assert child_session["forked_from_message_id"] is not None
    assert child_session["agent_id"] == "general"
    assert child_session["active_run_id"] == run["id"]
    assert task["agent_id"] == "general"
    assert task["metadata"]["execution_adapter"] == "session_read_tools"
    assert task["metadata"]["active_repo_write"] == "forbidden"
    assert task["metadata"]["external_network"] == "forbidden"
    assert task["metadata"]["allowed_tools"] == ["read", "glob", "grep", "artifact-read"]
    assert artifact["kind"] == "subagent_summary"
    assert artifact["producer"] == "harness_native_agent_alias"
    assert Path(artifact["path"]).exists()
    assert "No shell, network, provider execution" in Path(artifact["path"]).read_text(encoding="utf-8")

    store = SQLiteStore(tmp_path)
    parent_events = [event.kind for event in store.list_session_store_events(parent_session["id"])]
    child_events = [event.kind for event in store.list_session_store_events(child_session["id"])]
    assert "subagent.spawned" in parent_events
    assert "agent.selected" in child_events
    assert "subagent.completed" in child_events
    child_messages = store.list_session_messages(child_session["id"])
    assert [message.role for message in child_messages] == [SessionMessageRole.USER, SessionMessageRole.ASSISTANT]
    assistant_parts = store.list_session_parts(child_session["id"], message_id=child_messages[-1].id)
    assert any(part.kind == SessionPartKind.ARTIFACT_REF for part in assistant_parts)


def test_foreground_prompt_continue_uses_latest_session(tmp_path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=tmp_path, check=True, capture_output=True)
    session = SQLiteStore(tmp_path).create_session(title="Continue me")

    class FakeCodexBackend:
        def __init__(self, config):
            self.config = config
            self.name = config.name

        def preflight(self):
            from harness.models import BackendStatus

            return BackendStatus(available=True, metadata=self.config.metadata, capabilities=self.config.capabilities)

        def run_direct_agent(self, project_root, prompt, final_message_path, *, model=None, reasoning_effort=None):
            final_message_path.write_text("Continued.", encoding="utf-8")
            self.config.settings["last_codex_approval_mode"] = "on-request via --ask-for-approval"
            self.config.settings["last_codex_sandbox_mode"] = "workspace-write"
            self.config.settings["last_apply_back_approval_required"] = False
            return (CodexRunResult(["codex", "exec", prompt], "", "", 0, [], "Continued."), self.config.capabilities, "")

    monkeypatch.setattr("harness.cli.main.CodexCliBackend", FakeCodexBackend)
    result = runner.invoke(app, ["continue work", "--project", str(tmp_path), "--continue", "--no-stream", "--output", "json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["session_id"] == session.id
    assert len(SQLiteStore(tmp_path).list_session_messages(session.id)) == 2


def test_foreground_stream_progress_is_persisted_before_display(tmp_path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=tmp_path, check=True, capture_output=True)

    class FakeCodexBackend:
        def __init__(self, config):
            self.config = config
            self.name = config.name

        def preflight(self):
            from harness.models import BackendStatus

            return BackendStatus(available=True, metadata=self.config.metadata, capabilities=self.config.capabilities)

        def stream_direct_agent(self, project_root, prompt, final_message_path, *, model=None, reasoning_effort=None):
            yield {"type": "event", "event": {"type": "message", "message": "Streaming progress"}, "line": "{}\n"}
            final_message_path.write_text("Streamed final.", encoding="utf-8")
            self.config.settings["last_codex_approval_mode"] = "on-request via --ask-for-approval"
            self.config.settings["last_codex_sandbox_mode"] = "workspace-write"
            self.config.settings["last_apply_back_approval_required"] = False
            yield {
                "type": "completed",
                "capabilities": self.config.capabilities,
                "network_status": "",
                "result": CodexRunResult(["codex", "exec", prompt], "", "", 0, [{"type": "message"}], "Streamed final."),
            }

    monkeypatch.setattr("harness.cli.main.CodexCliBackend", FakeCodexBackend)
    result = runner.invoke(app, ["stream progress", "--project", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "codex: message: Streaming progress" in result.output
    session_id = result.output.split("Session: ", 1)[1].splitlines()[0]
    store = SQLiteStore(tmp_path)
    session_events = store.list_session_store_events(session_id)
    kinds = [event.kind for event in session_events]
    assert "model.message_delta" in kinds
    assert "run.started" in kinds
    assert "run.finished" in kinds
    assert "artifact.registered" in kinds
    assert any(event.payload.get("summary") == "message: Streaming progress" for event in session_events)
    tail = runner.invoke(app, ["session", "tail", session_id, "--project", str(tmp_path)])
    assert tail.exit_code == 0, tail.output
    assert "Model update: message: Streaming progress" in tail.output


def test_intent_router_routes_product_flows() -> None:
    fix = route_instruction("fix the failing tests")
    explain = route_instruction("summarize this repo")
    plan = route_instruction("plan how to improve the CLI")
    unsupported = route_instruction("deploy this to production now")

    assert fix.intent == "fix_tests"
    assert fix.agent_id == "code_editor"
    assert fix.task_type == "codex_code_edit"
    assert "apply_back_separate" in fix.required_approvals
    assert explain.intent == "explain_repo"
    assert explain.task_type == "read_only_repo_summary"
    assert plan.intent == "plan_change"
    assert plan.task_type == "repo_planning"
    assert unsupported.intent == "unsupported"


def test_route_cli_outputs_expected_contract(tmp_path) -> None:
    result = runner.invoke(app, ["route", "fix the failing tests", "--project", str(tmp_path), "--output", "json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema_version"] == "harness.route/v1"
    assert payload["route"]["intent"] == "fix_tests"
    assert "diff.patch" in payload["route"]["expected_outputs"]


def test_run_auto_creates_waiting_session_without_hosted_approval(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0

    result = runner.invoke(app, ["run", "fix the failing tests", "--project", str(tmp_path), "--output", "json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema_version"] == "harness.product_session/v1"
    assert payload["status"] == SessionStatus.WAITING_APPROVAL.value
    assert payload["route"]["intent"] == "fix_tests"
    assert payload["session"]["objective_id"]
    assert payload["session"]["active_task_id"]
    event_types = {event["event_type"] for event in payload["events"]}
    assert "intent.routed" in event_types
    assert "approval.required" in event_types
    transcript = tmp_path / ".harness" / "sessions" / payload["session"]["id"] / "transcript.jsonl"
    assert transcript.exists()
    assert "approval.required" in transcript.read_text(encoding="utf-8")


def test_run_auto_migrates_existing_initialized_database(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    with store_without_session_columns(tmp_path) as conn:
        conn.execute("DROP TABLE sessions")

    result = runner.invoke(app, ["run", "fix the failing tests", "--project", str(tmp_path), "--output", "json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == SessionStatus.WAITING_APPROVAL.value
    assert payload["session"]["id"].startswith("sess_")


def test_sessions_list_inspect_and_reject_decision(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    session = store.create_session(intent="explain_repo")
    run = store.create_run("Explain", "read_only_repo_summary", session_id=session.id)

    listed = runner.invoke(app, ["sessions", "list", "--project", str(tmp_path), "--output", "json"])
    inspected = runner.invoke(app, ["sessions", "inspect", session.id, "--project", str(tmp_path), "--output", "json"])
    rejected = runner.invoke(app, ["reject", run.id, "--project", str(tmp_path), "--output", "json"])

    assert listed.exit_code == 0, listed.output
    assert inspected.exit_code == 0, inspected.output
    assert rejected.exit_code == 0, rejected.output
    assert json.loads(listed.output)["sessions"][0]["id"] == session.id
    assert json.loads(inspected.output)["transcript_path"].endswith("transcript.jsonl")
    reject_payload = json.loads(rejected.output)
    assert reject_payload["decision"] == "rejected"
    assert (tmp_path / ".harness" / "runs" / run.id / "apply_back.json").exists()


def store_without_session_columns(tmp_path):
    import sqlite3

    return sqlite3.connect(tmp_path / ".harness" / "harness.sqlite")
