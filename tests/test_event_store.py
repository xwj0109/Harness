from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from harness.memory.sqlite_store import SQLiteStore
from harness.models import EventStreamType, RedactionState


def test_event_store_allocates_append_only_sequences_and_replays_after_restart(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    session = store.create_session(title="Event replay")
    message = store.append_session_message(session.id, "user", "Replay this")

    first = store.append_store_event(
        EventStreamType.SESSION,
        session.id,
        "custom.first",
        {"value": 1},
        session_id=session.id,
        message_id=message.id,
        redaction_state=RedactionState.NOT_REQUIRED,
    )
    second = store.append_store_event(
        EventStreamType.SESSION,
        session.id,
        "custom.second",
        {"value": 2},
        session_id=session.id,
        message_id=message.id,
        redaction_state=RedactionState.NOT_REQUIRED,
    )

    assert first.seq + 1 == second.seq
    restarted = SQLiteStore(tmp_path)
    events = restarted.list_store_events(EventStreamType.SESSION, session.id, after_seq=first.seq)
    assert [event.kind for event in events] == ["custom.second"]
    assert events[0].payload == {"value": 2}
    assert events[0].jsonl_envelope()["seq"] == second.seq


def test_event_store_concurrent_appends_keep_unique_monotonic_sequence(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    session = store.create_session(title="Concurrent events")

    def append(index: int) -> int:
        event = SQLiteStore(tmp_path).append_store_event(
            EventStreamType.SESSION,
            session.id,
            f"custom.concurrent.{index}",
            {"index": index},
            session_id=session.id,
            redaction_state=RedactionState.NOT_REQUIRED,
        )
        return event.seq

    with ThreadPoolExecutor(max_workers=6) as executor:
        seqs = list(executor.map(append, range(12)))

    events = SQLiteStore(tmp_path).list_store_events(EventStreamType.SESSION, session.id)
    assert sorted(seqs) == list(range(2, 14))
    assert [event.seq for event in events] == list(range(1, 14))
    assert len({event.seq for event in events}) == 13
