from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

from harness.events import json_default
from harness.memory.sqlite_store import SQLiteStore
from harness.models import SessionMessageRecord, SessionPartRecord, StoredEventRecord
from harness.security import sanitize_for_logging


TimelineFormat = Literal["text", "jsonl"]


@dataclass(frozen=True)
class SessionTranscriptEntry:
    message: SessionMessageRecord
    parts: list[SessionPartRecord]

    def json_envelope(self) -> dict[str, Any]:
        return {
            "schema_version": "harness.session_transcript_entry/v1",
            "message": self.message.model_dump(mode="json"),
            "parts": [part.model_dump(mode="json") for part in self.parts],
        }


def list_session_timeline(store: SQLiteStore, session_id: str, *, limit: int | None = None) -> list[StoredEventRecord]:
    events = store.list_session_store_events(session_id)
    if limit is not None and limit >= 0:
        return events[-limit:]
    return events


def list_session_transcript(store: SQLiteStore, session_id: str) -> list[SessionTranscriptEntry]:
    messages = store.list_session_messages(session_id)
    parts = store.list_session_parts(session_id)
    parts_by_message: dict[str, list[SessionPartRecord]] = {}
    for part in parts:
        parts_by_message.setdefault(part.message_id, []).append(part)
    return [SessionTranscriptEntry(message=message, parts=parts_by_message.get(message.id, [])) for message in messages]


def render_timeline_event(event: StoredEventRecord) -> str:
    label = _event_label(event.kind)
    suffixes = []
    if event.message_id:
        suffixes.append(f"message={event.message_id}")
    if event.run_id:
        suffixes.append(f"run={event.run_id}")
    if event.task_id:
        suffixes.append(f"task={event.task_id}")
    if event.artifact_id:
        suffixes.append(f"artifact={event.artifact_id}")
    detail = _event_detail(event)
    line = f"{event.seq:04d} {label}"
    if detail:
        line += f": {detail}"
    if suffixes:
        line += f" ({', '.join(suffixes)})"
    return line


def render_transcript_entry(entry: SessionTranscriptEntry) -> str:
    role = entry.message.role.value
    header = f"{role} {entry.message.id}"
    if entry.message.run_id:
        header += f" run={entry.message.run_id}"
    if entry.message.mutation_reversibility.value != "none":
        header += f" reversibility={entry.message.mutation_reversibility.value}"
    body_lines = []
    for part in entry.parts:
        rendered = _render_part(part)
        if rendered:
            body_lines.append(rendered)
    if not body_lines and entry.message.content_preview:
        body_lines.append(str(sanitize_for_logging(entry.message.content_preview)))
    body = "\n".join(body_lines) if body_lines else "(no persisted parts)"
    return f"{header}\n{body}"


def timeline_event_jsonl(event: StoredEventRecord) -> str:
    return json.dumps(event.jsonl_envelope(), default=json_default, sort_keys=True)


def transcript_entry_jsonl(entry: SessionTranscriptEntry) -> str:
    return json.dumps(entry.json_envelope(), default=json_default, sort_keys=True)


def _event_label(kind: str) -> str:
    labels = {
        "session.created": "Session created",
        "session.archived": "Session archived",
        "session.forked": "Session forked",
        "session.message.appended": "Message appended",
        "session.part.appended": "Part appended",
        "session.snapshot.recorded": "Snapshot recorded",
        "session.model_selected": "Model selected",
        "session.title_updated": "Title updated",
        "run.started": "Run started",
        "run.progress": "Run progress",
        "model.message_delta": "Model update",
        "tool_call.started": "Tool started",
        "tool_call.output": "Tool output",
        "tool_call.finished": "Tool finished",
        "run.finished": "Run finished",
        "run.failed": "Run failed",
        "artifact.registered": "Artifact registered",
        "token_usage.updated": "Token usage updated",
        "permission.requested": "Permission requested",
        "permission.resolved": "Permission resolved",
        "permission.checked": "Permission checked",
        "todo.updated": "Todo updated",
        "question.requested": "Question requested",
        "agent.selected": "Agent selected",
        "run.blocked": "Run blocked",
    }
    return labels.get(kind, kind)


def _event_detail(event: StoredEventRecord) -> str:
    payload = event.payload or {}
    if event.kind == "session.message.appended":
        role = payload.get("role")
        preview = str(payload.get("content_preview") or "").strip()
        if role and preview:
            return f"{role}: {preview[:160]}"
        if role:
            return str(role)
    if event.kind == "session.part.appended":
        kind = payload.get("kind")
        ordinal = payload.get("ordinal")
        if kind and ordinal:
            return f"{kind} #{ordinal}"
        if kind:
            return str(kind)
    if event.kind == "session.model_selected":
        return str(payload.get("raw_model_ref") or payload.get("model_id") or "")
    if event.kind == "session.forked":
        return str(payload.get("parent_session_id") or "")
    if event.kind == "session.snapshot.recorded":
        snapshot_id = payload.get("snapshot_id") or ""
        snapshot_kind = payload.get("snapshot_kind") or "snapshot"
        reversible = payload.get("reversible")
        return f"{snapshot_kind} {snapshot_id} reversible={reversible}"
    summary = payload.get("summary")
    if summary:
        return str(sanitize_for_logging(summary))[:160]
    message = payload.get("message")
    if message:
        return str(sanitize_for_logging(message))[:160]
    status = payload.get("status")
    if status:
        return str(status)
    return ""


def _render_part(part: SessionPartRecord) -> str:
    if part.text:
        if part.kind.value == "todo_update":
            status = part.metadata.get("status") if part.metadata else None
            return f"[todo {status or 'updated'}] {sanitize_for_logging(part.text)}"
        if part.kind.value == "question":
            choices = part.metadata.get("choices") if part.metadata else None
            suffix = f" choices={json.dumps(sanitize_for_logging(choices), default=json_default, sort_keys=True)}" if choices else ""
            return f"[question] {sanitize_for_logging(part.text)}{suffix}"
        return str(sanitize_for_logging(part.text))
    if part.artifact_id:
        if part.kind.value == "snapshot_ref":
            snapshot_id = part.metadata.get("snapshot_id") if part.metadata else None
            reversible = part.metadata.get("reversible") if part.metadata else None
            return f"[snapshot] {snapshot_id or part.artifact_id} reversible={reversible} artifact={part.artifact_id}"
        return f"[{part.kind.value}] artifact={part.artifact_id}"
    if part.run_id:
        return f"[{part.kind.value}] run={part.run_id}"
    if part.metadata:
        metadata = sanitize_for_logging(part.metadata)
        if part.kind.value == "snapshot_ref":
            return f"[snapshot] {metadata.get('snapshot_id')} reversible={metadata.get('reversible')}"
        if metadata.get("attachment_kind") == "file_ref":
            return f"[file] {metadata.get('path')}"
        return f"[{part.kind.value}] {json.dumps(metadata, default=json_default, sort_keys=True)}"
    return f"[{part.kind.value}]"
