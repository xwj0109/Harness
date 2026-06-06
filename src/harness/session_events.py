from __future__ import annotations

from enum import Enum
import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from harness.events import append_jsonl
from harness.memory.sqlite_store import now_iso, parse_dt
from harness.security import sanitize_for_logging


SESSION_EVENT_SCHEMA_VERSION = "harness.session_event/v1"
SESSION_EVENTS_READ_SCHEMA_VERSION = "harness.session_events_read/v1"


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


class SessionEventsReadResult(BaseModel):
    schema_version: str = SESSION_EVENTS_READ_SCHEMA_VERSION
    ok: bool
    project_root: Path
    session_id: str
    transcript_path: Path
    transcript_exists: bool
    events: list[SessionEvent] = Field(default_factory=list)
    event_count: int = 0
    parse_errors: list[dict[str, Any]] = Field(default_factory=list)
    validation_errors: list[dict[str, Any]] = Field(default_factory=list)
    contents_included: bool = False
    execution_allowed: bool = False
    model_context_allowed: bool = False
    network_required: bool = False
    mutation_allowed: bool = False
    permission_granting: bool = False


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
    result = read_session_events_with_diagnostics(project_root, session_id)
    if not result.ok:
        raise ValueError(f"Session transcript is malformed: {session_id}")
    return result.events


def session_events_read_health_payload(result: SessionEventsReadResult) -> dict[str, Any]:
    return {
        "schema_version": result.schema_version,
        "ok": result.ok,
        "session_id": result.session_id,
        "transcript_path": str(result.transcript_path),
        "transcript_exists": result.transcript_exists,
        "event_count": result.event_count,
        "parse_error_count": len(result.parse_errors),
        "validation_error_count": len(result.validation_errors),
        "parse_errors": list(result.parse_errors),
        "validation_errors": list(result.validation_errors),
        "contents_included": result.contents_included,
        "execution_allowed": result.execution_allowed,
        "model_context_allowed": result.model_context_allowed,
        "network_required": result.network_required,
        "mutation_allowed": result.mutation_allowed,
        "permission_granting": result.permission_granting,
    }


def read_session_events_with_diagnostics(project_root: Path, session_id: str) -> SessionEventsReadResult:
    path = session_transcript_path(project_root, session_id)
    events: list[SessionEvent] = []
    parse_errors: list[dict[str, Any]] = []
    validation_errors: list[dict[str, Any]] = []
    if not path.exists():
        return SessionEventsReadResult(
            ok=True,
            project_root=project_root,
            session_id=session_id,
            transcript_path=path,
            transcript_exists=False,
            events=[],
            event_count=0,
        )
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            parse_errors.append({"line": line_number, "error": f"{exc.__class__.__name__}: {exc.msg}"})
            continue
        if not isinstance(payload, dict):
            validation_errors.append({"line": line_number, "error": "JSONL record is not an object."})
            continue
        try:
            events.append(SessionEvent.model_validate(payload))
        except Exception as exc:
            validation_errors.append(
                {
                    "line": line_number,
                    "error": exc.__class__.__name__,
                    "message": "Session transcript event failed schema validation.",
                }
            )
    return SessionEventsReadResult(
        ok=not parse_errors and not validation_errors,
        project_root=project_root,
        session_id=session_id,
        transcript_path=path,
        transcript_exists=True,
        events=events,
        event_count=len(events),
        parse_errors=parse_errors,
        validation_errors=validation_errors,
    )
