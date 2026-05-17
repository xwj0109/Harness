from __future__ import annotations

from harness.memory.sqlite_store import SQLiteStore
from harness.models import SessionStatus


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

