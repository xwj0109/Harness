from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from harness.events import append_jsonl
from harness.memory.sqlite_store import now_iso, parse_dt
from harness.security import sanitize_for_logging


SESSION_EVENT_SCHEMA_VERSION = "harness.session_event/v1"


class SessionEventKind(str, Enum):
    SESSION_STARTED = "session.started"
    INTENT_ROUTED = "intent.routed"
    THOUGHT_SUMMARY = "thought.summary"
    TOOL_CALL = "tool.call"
    FILE_READ = "file.read"
    FILE_EDIT = "file.edit"
    TEST_STARTED = "test.started"
    TEST_FINISHED = "test.finished"
    APPROVAL_REQUIRED = "approval.required"
    APPROVAL_DECIDED = "approval.decided"
    POLICY_DECISION = "policy.decision"
    ARTIFACT_REGISTERED = "artifact.registered"
    REPORT_READY = "report.ready"
    APPLY_REQUIRED = "apply.required"
    APPLY_DECIDED = "apply.decided"
    SESSION_COMPLETED = "session.completed"
    SESSION_FAILED = "session.failed"


class SessionEvent(BaseModel):
    schema_version: str = SESSION_EVENT_SCHEMA_VERSION
    session_id: str
    run_id: str | None = None
    task_id: str | None = None
    objective_id: str | None = None
    event_type: SessionEventKind
    level: Literal["info", "warning", "error"] = "info"
    message: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: str


def session_transcript_path(project_root: Path, session_id: str) -> Path:
    return project_root / ".harness" / "sessions" / session_id / "transcript.jsonl"


def append_session_event(
    project_root: Path,
    *,
    session_id: str,
    event_type: SessionEventKind,
    message: str,
    level: Literal["info", "warning", "error"] = "info",
    payload: dict[str, Any] | None = None,
    run_id: str | None = None,
    task_id: str | None = None,
    objective_id: str | None = None,
) -> SessionEvent:
    event = SessionEvent(
        session_id=session_id,
        run_id=run_id,
        task_id=task_id,
        objective_id=objective_id,
        event_type=event_type,
        level=level,
        message=str(sanitize_for_logging(message)),
        payload=dict(sanitize_for_logging(payload or {})),
        created_at=now_iso(),
    )
    append_jsonl(session_transcript_path(project_root, session_id), event.model_dump(mode="json"))
    return event


def render_session_event(event: SessionEvent | dict[str, Any]) -> str:
    if isinstance(event, dict):
        event = SessionEvent.model_validate(event)
    prefix = {
        "info": "●",
        "warning": "!",
        "error": "x",
    }.get(event.level, "●")
    return f"{prefix} {event.message}"


def read_session_events(project_root: Path, session_id: str) -> list[SessionEvent]:
    path = session_transcript_path(project_root, session_id)
    if not path.exists():
        return []
    import json

    events: list[SessionEvent] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            events.append(SessionEvent.model_validate(json.loads(line)))
    return events

