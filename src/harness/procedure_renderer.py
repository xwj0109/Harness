from __future__ import annotations

from typing import Any


def render_procedure_event(event: dict[str, Any]) -> str:
    event_type = str(event.get("type") or event.get("event_type") or "event")
    seq = event.get("seq")
    payload = event.get("payload") or {}
    prefix = f"{seq}. " if seq is not None else ""
    if event_type == "run.started":
        return f"{prefix}● Run started"
    if event_type == "policy.resolved":
        return f"{prefix}● Resolving policy"
    if event_type == "approval.required":
        return f"{prefix}! Approval required"
    if event_type == "workspace.prepared":
        return f"{prefix}● Preparing workspace"
    if event_type == "backend.started":
        return f"{prefix}● Model started"
    if event_type in {"model.message_delta", "model.token"}:
        return f"{prefix}{payload.get('delta') or payload.get('text') or ''}"
    if event_type == "reasoning.summary_delta":
        return f"{prefix}thinking summary: {payload.get('delta') or payload.get('text') or ''}"
    if event_type == "tool_call.started":
        return f"{prefix}● Tool call: {payload.get('tool') or payload.get('name') or 'tool'}"
    if event_type == "tool_call.output":
        return f"{prefix}● Tool output"
    if event_type == "tool_call.finished":
        return f"{prefix}● Tool finished"
    if event_type == "file.read":
        path = payload.get("path")
        return f"{prefix}● File read{f': {path}' if path else ''}"
    if event_type == "file.write":
        path = payload.get("path")
        return f"{prefix}● Editing{f': {path}' if path else ''}"
    if event_type == "diff.updated":
        added = payload.get("added")
        removed = payload.get("removed")
        if added is not None and removed is not None:
            return f"{prefix}● Diff ready (+{added} -{removed} lines)"
        return f"{prefix}● Diff ready"
    if event_type == "test.started":
        command = payload.get("command")
        return f"{prefix}● Running tests{f': {command}' if command else ''}"
    if event_type == "test.output":
        return f"{prefix}{payload.get('text') or payload.get('output') or ''}"
    if event_type == "test.finished":
        status = payload.get("status")
        return f"{prefix}● Tests finished{f': {status}' if status else ''}"
    if event_type == "token_usage.updated":
        total = payload.get("total_tokens")
        return f"{prefix}● Token usage updated{f': {total} total' if total is not None else ''}"
    if event_type == "artifact.registered":
        kind = payload.get("kind")
        return f"{prefix}● Artifact registered{f': {kind}' if kind else ''}"
    if event_type == "run.summary_created":
        return f"{prefix}● Final summary"
    if event_type == "run.finished":
        return f"{prefix}● Run finished"
    if event_type == "run.failed":
        return f"{prefix}x Run failed"
    return f"{prefix}● {event_type}"
