from __future__ import annotations

from harness.memory.sqlite_store import SQLiteStore
from harness.models import EventStreamType, SessionStatus


def test_session_store_crud_fork_archive_delete_and_parts(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()

    session = store.create_session(title="Store session", raw_model_ref="codex_cli/gpt-5.5", agent_id="plan")
    message = store.append_session_message(session.id, "user", "Plan the migration")
    part = store.append_session_part(session.id, message.id, "text", text="Plan the migration")
    child = store.fork_session(session.id, message_id=message.id, title="Forked plan")
    archived = store.archive_session(session.id)
    deleted = store.delete_session(child.id)

    assert store.get_session(session.id).status == SessionStatus.ARCHIVED
    assert archived.status == SessionStatus.ARCHIVED
    assert deleted.status == SessionStatus.ARCHIVED
    assert child.parent_session_id == session.id
    assert child.forked_from_message_id == message.id
    assert store.list_session_messages(session.id)[0].content_preview == "Plan the migration"
    assert store.list_session_parts(session.id)[0].id == part.id
    assert [event.kind for event in store.list_session_store_events(session.id)][:3] == [
        "session.created",
        "session.message.appended",
        "session.part.appended",
    ]
    assert store.latest_session() is None


def test_session_store_restore_and_hard_delete_session_only(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()

    session = store.create_session(title="Delete me")
    message = store.append_session_message(session.id, "user", "Keep independent evidence")
    store.append_session_part(session.id, message.id, "text", text="Keep independent evidence")
    store.append_session_todo(session.id, "Follow up")
    permission = store.request_session_permission(
        session.id,
        tool_id="read",
        normalized_action="read",
        normalized_target_pattern="README.md",
        boundary_kind="local_only",
        risk="low",
        scope="session",
        source="policy",
    )
    child = store.fork_session(session.id, message_id=message.id, title="Child")
    run = store.create_run("linked run", "test", session_id=session.id)
    store.append_event(run.id, "info", "progress", "linked event", session_id=session.id)
    run_store_event = store.append_store_event(
        EventStreamType.RUN,
        run.id,
        "run.linked",
        {"summary": "run event linked to session"},
        session_id=session.id,
        run_id=run.id,
    )
    artifact_path = tmp_path / ".harness" / "runs" / run.id / "artifact.txt"
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text("artifact", encoding="utf-8")
    artifact = store.register_artifact(run.id, "text", artifact_path, session_id=session.id)
    task = store.create_task("linked task", session_id=session.id)
    objective = store.create_objective("linked objective", session_id=session.id)
    session_dir = tmp_path / ".harness" / "sessions" / session.id
    assert session_dir.exists()
    assert permission.session_id == session.id

    archived = store.archive_session(session.id)
    restored = store.restore_session(session.id)

    assert archived.status == SessionStatus.ARCHIVED
    assert restored.status == SessionStatus.ACTIVE
    assert any(event.kind == "session.restored" for event in store.list_session_store_events(session.id))

    counts = store.hard_delete_session(session.id)

    assert counts["session_rows"] == 1
    assert counts["session_messages"] == 2
    assert counts["session_parts"] == 2
    assert counts["session_todos"] == 1
    assert counts["session_permissions"] == 1
    assert counts["session_directory_removed"] is True
    assert not session_dir.exists()
    assert store.get_run(run.id).session_id is None
    assert store.get_task(task.id).session_id is None
    assert store.get_objective(objective.id).session_id is None
    assert store.get_artifact(artifact.id).session_id is None
    assert store.list_events(run.id)[0].session_id is None
    reloaded_run_store_event = next(event for event in store.list_store_events(EventStreamType.RUN, run.id) if event.id == run_store_event.id)
    assert reloaded_run_store_event.session_id is None
    reloaded_child = store.get_session(child.id)
    assert reloaded_child.parent_session_id is None
    assert reloaded_child.forked_from_message_id is None

    try:
        store.get_session(session.id)
    except KeyError:
        pass
    else:
        raise AssertionError("hard deleted session should not be loadable")
