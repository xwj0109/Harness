from __future__ import annotations

from harness.memory.sqlite_store import SQLiteStore
from harness.models import (
    SessionMessageRecord,
    SessionPartRecord,
    SessionPermissionRequest,
    SessionSpec,
    StoredEventRecord,
)


def test_session_records_round_trip_through_json_models(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    session = store.create_session(title="Model round trip", raw_model_ref="codex_cli/gpt-5.5")
    message = store.append_session_message(session.id, "user", "Persist records")
    part = store.append_session_part(session.id, message.id, "text", text="Persist records")
    permission = store.request_session_permission(
        session.id,
        tool_id="read",
        normalized_action="read",
        normalized_target_pattern="README.md",
        boundary_kind="local_only",
        risk="low",
    )
    event = store.append_store_event("session", session.id, "custom.event", {"ok": True}, session_id=session.id)

    assert SessionSpec.model_validate_json(session.model_dump_json()).id == session.id
    assert SessionMessageRecord.model_validate_json(message.model_dump_json()).id == message.id
    assert SessionPartRecord.model_validate_json(part.model_dump_json()).id == part.id
    assert SessionPermissionRequest.model_validate_json(permission.model_dump_json()).id == permission.id
    assert StoredEventRecord.model_validate_json(event.model_dump_json()).id == event.id

