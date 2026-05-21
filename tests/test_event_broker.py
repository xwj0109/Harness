from __future__ import annotations

from harness.event_broker import reset_event_broker, subscribe_global_events, subscribe_store_events
from harness.memory.sqlite_store import SQLiteStore
from harness.models import EventStreamType, RedactionState


def test_event_broker_replays_persisted_stream_events(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    reset_event_broker(tmp_path)
    session = store.create_session(title="Replay through broker")
    first = store.append_store_event(
        EventStreamType.SESSION,
        session.id,
        "custom.first",
        {"value": 1},
        session_id=session.id,
        redaction_state=RedactionState.NOT_REQUIRED,
    )
    store.append_store_event(
        EventStreamType.SESSION,
        session.id,
        "custom.second",
        {"value": 2},
        session_id=session.id,
        redaction_state=RedactionState.NOT_REQUIRED,
    )

    subscription = subscribe_store_events(store, EventStreamType.SESSION, session.id, after_seq=first.seq)

    try:
        received = subscription.next(timeout=0.1)
        assert received is not None
        assert received.kind == "custom.second"
        assert subscription.next(timeout=0.01) is None
    finally:
        subscription.close()


def test_event_broker_receives_events_after_store_commit(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    reset_event_broker(tmp_path)
    session = store.create_session(title="Live through broker")
    subscription = subscribe_store_events(store, EventStreamType.SESSION, session.id, after_seq=1)

    try:
        appended = store.append_store_event(
            EventStreamType.SESSION,
            session.id,
            "custom.live",
            {"value": "after-subscribe"},
            session_id=session.id,
            redaction_state=RedactionState.NOT_REQUIRED,
        )

        received = subscription.next(timeout=0.5)
        assert received is not None
        assert received.id == appended.id
        assert received.kind == "custom.live"
        assert received.seq == appended.seq
    finally:
        subscription.close()


def test_event_broker_broadcasts_to_global_subscribers(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    reset_event_broker(tmp_path)
    session = store.create_session(title="Global broker")
    subscription = subscribe_global_events(tmp_path)

    try:
        appended = store.append_store_event(
            EventStreamType.SESSION,
            session.id,
            "custom.global",
            {"value": "broadcast"},
            session_id=session.id,
            redaction_state=RedactionState.NOT_REQUIRED,
        )

        received = subscription.next(timeout=0.5)
        assert received is not None
        assert received.id == appended.id
        assert received.stream_id == session.id
    finally:
        subscription.close()


def test_event_broker_close_unsubscribes_from_live_events(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    reset_event_broker(tmp_path)
    session = store.create_session(title="Closed broker")
    subscription = subscribe_store_events(store, EventStreamType.SESSION, session.id, after_seq=1)
    subscription.close()

    store.append_store_event(
        EventStreamType.SESSION,
        session.id,
        "custom.ignored",
        {"value": "ignored"},
        session_id=session.id,
        redaction_state=RedactionState.NOT_REQUIRED,
    )

    assert subscription.next(timeout=0.01) is None
