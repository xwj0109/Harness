from __future__ import annotations

import hashlib
import json
from typing import Any

from harness.memory.sqlite_store import SQLiteStore
from harness.security import sanitize_for_logging


SESSION_SHARE_SCHEMA_VERSION = "harness.session_share/v1"
SESSION_SHARE_ACTION_SCHEMA_VERSION = "harness.session_share_action/v1"


def build_local_session_share_snapshot(
    store: SQLiteStore,
    session_id: str,
    *,
    sanitize: bool = True,
) -> dict[str, Any]:
    session = store.get_session(session_id)
    messages = store.list_session_messages(session.id)
    parts = store.list_session_parts(session.id)
    events = store.list_session_store_events(session.id)
    artifacts = _session_artifact_references(store, session.id)
    snapshot = {
        "schema_version": SESSION_SHARE_SCHEMA_VERSION,
        "ok": True,
        "share_mode": "local_snapshot",
        "hosted_share_supported": False,
        "hosted_url": None,
        "sanitize": sanitize,
        "include_artifacts": False,
        "artifact_files_included": False,
        "session": session.model_dump(mode="json"),
        "messages": [message.model_dump(mode="json") for message in messages],
        "parts": [part.model_dump(mode="json") for part in parts],
        "events": [event.model_dump(mode="json") for event in events],
        "artifact_references": artifacts,
        "counts": {
            "messages": len(messages),
            "parts": len(parts),
            "events": len(events),
            "artifact_references": len(artifacts),
        },
        "network_called": False,
        "filesystem_modified": False,
        "permission_granting": False,
    }
    if sanitize:
        snapshot = sanitize_for_logging(snapshot)
    encoded = json.dumps(snapshot, sort_keys=True, default=str).encode("utf-8")
    snapshot["snapshot_sha256"] = hashlib.sha256(encoded).hexdigest()
    return snapshot


def hosted_share_unsupported(session_id: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "schema_version": SESSION_SHARE_ACTION_SCHEMA_VERSION,
        "ok": False,
        "session_id": session_id,
        "requested": sanitize_for_logging(body or {}),
        "error": "Hosted session sharing is not implemented yet; refusing to upload session data or contact a share service.",
        "hosted_share_supported": False,
        "hosted_url": None,
        "network_called": False,
        "filesystem_modified": False,
        "permission_granting": False,
    }


def _session_artifact_references(store: SQLiteStore, session_id: str) -> list[dict[str, Any]]:
    references: list[dict[str, Any]] = []
    for run in store.list_runs():
        if run.session_id != session_id:
            continue
        for artifact in store.list_artifacts(run.id):
            payload = artifact.model_dump(mode="json")
            payload["contents_included"] = False
            payload["file_included"] = False
            references.append(payload)
    return references
