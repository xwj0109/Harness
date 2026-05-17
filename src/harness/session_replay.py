from __future__ import annotations

from typing import Any

from harness.memory.sqlite_store import SQLiteStore
from harness.security import sanitize_for_logging


SESSION_REPLAY_SCHEMA_VERSION = "harness.session_replay/v1"


def build_session_replay_projection(
    store: SQLiteStore,
    session_id: str,
    *,
    after_seq: int | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    session = store.get_session(session_id)
    fetch_limit = limit + 1 if limit is not None and limit >= 0 else None
    fetched = store.list_session_store_events(session.id, after_seq=after_seq, limit=fetch_limit)
    has_more = bool(fetch_limit is not None and len(fetched) > limit)
    events = fetched[:limit] if has_more and limit is not None else fetched
    latest_seq = events[-1].seq if events else after_seq
    return sanitize_for_logging(
        {
            "schema_version": SESSION_REPLAY_SCHEMA_VERSION,
            "ok": True,
            "session_id": session.id,
            "session": session.model_dump(mode="json"),
            "after_seq": after_seq,
            "limit": limit,
            "events": [event.model_dump(mode="json") for event in events],
            "event_count": len(events),
            "next_after_seq": latest_seq,
            "has_more": has_more,
            "replay_complete": not has_more,
            "transport": "snapshot",
            "source": "append_only_event_store",
            "execution_started": False,
            "network_called": False,
            "filesystem_modified": False,
            "permission_granting": False,
        }
    )
