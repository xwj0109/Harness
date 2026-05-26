from __future__ import annotations

from typing import Any, Iterator, Literal

from pydantic import BaseModel, Field

from harness.security import sanitize_for_logging


class BackendStreamEvent(BaseModel):
    type: Literal[
        "message_delta",
        "reasoning_summary_delta",
        "tool_call",
        "tool_call_delta",
        "tool_result",
        "token_usage",
        "finish_reason",
        "status",
        "error",
    ]
    text: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


def classify_codex_stream_item(item: dict[str, Any]) -> BackendStreamEvent:
    item = sanitize_for_logging(item)
    item_type = str(item.get("type") or "")
    event = item.get("event") if isinstance(item.get("event"), dict) else {}
    event_type = str(event.get("type") or item_type)
    text = _first_text(event, item)

    if item_type == "stdout":
        return BackendStreamEvent(type="message_delta", text=str(item.get("line") or ""), payload={"source": "stdout"})
    if item_type == "completed":
        result = item.get("result")
        final_message = getattr(result, "final_message", None)
        return BackendStreamEvent(
            type="status",
            text=final_message or "Backend completed.",
            payload={"status": "completed"},
        )
    if item_type == "event":
        lowered = event_type.lower()
        if "token" in lowered or "usage" in lowered:
            return BackendStreamEvent(type="token_usage", payload=dict(event))
        if "reasoning" in lowered and "summary" in lowered:
            return BackendStreamEvent(type="reasoning_summary_delta", text=text, payload=dict(event))
        if "tool" in lowered and ("result" in lowered or "output" in lowered):
            return BackendStreamEvent(type="tool_result", text=text, payload=dict(event))
        if "tool" in lowered:
            return BackendStreamEvent(type="tool_call", text=text, payload=dict(event))
        if text:
            return BackendStreamEvent(type="message_delta", text=text, payload=dict(event))
        return BackendStreamEvent(type="status", payload=dict(event))
    if item_type == "error":
        return BackendStreamEvent(type="error", text=text or str(item.get("message") or "Backend error."), payload=dict(item))
    return BackendStreamEvent(type="status", text=text, payload=dict(item))


class MockStreamingBackend:
    def __init__(self, events: list[BackendStreamEvent] | None = None) -> None:
        self.events = events or [
            BackendStreamEvent(type="status", text="Backend started.", payload={"status": "started"}),
            BackendStreamEvent(type="message_delta", text="I will inspect the failing tests first."),
            BackendStreamEvent(type="tool_call", text="repo_read", payload={"tool": "repo_read"}),
            BackendStreamEvent(type="tool_result", text="found failing assertion", payload={"tool": "repo_read"}),
            BackendStreamEvent(type="token_usage", payload={"input_tokens": 10, "output_tokens": 12, "total_tokens": 22}),
            BackendStreamEvent(type="status", text="Backend completed.", payload={"status": "completed"}),
        ]

    def stream(self) -> Iterator[BackendStreamEvent]:
        yield from self.events


def _first_text(*items: dict[str, Any]) -> str | None:
    for item in items:
        for key in ("text", "delta", "message", "content", "line"):
            value = item.get(key)
            if isinstance(value, str) and value:
                return str(sanitize_for_logging(value))
    return None
