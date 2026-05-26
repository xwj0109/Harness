from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


CanonicalContentKind = Literal[
    "text",
    "reasoning",
    "image_input",
    "tool_call",
    "tool_result",
    "refusal",
    "error",
    "provider_metadata",
]

CanonicalRole = Literal["system", "developer", "user", "assistant", "tool"]


class UnsupportedCanonicalPartError(ValueError):
    def __init__(self, part_kind: str, protocol: str, *, role: str | None = None) -> None:
        self.part_kind = part_kind
        self.protocol = protocol
        self.role = role
        role_suffix = f" for role {role}" if role else ""
        super().__init__(f"Unsupported canonical content part for {protocol}{role_suffix}: {part_kind}")


class CanonicalMessagePart(BaseModel):
    schema_version: str = "harness.canonical_message_part/v1"
    kind: CanonicalContentKind
    text: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    provider_native_id: str | None = None
    signature: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class CanonicalMessage(BaseModel):
    schema_version: str = "harness.canonical_message/v1"
    role: CanonicalRole
    parts: list[CanonicalMessagePart] = Field(default_factory=list)
    provider_native_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def text(cls, role: str, content: str, *, metadata: dict[str, Any] | None = None) -> "CanonicalMessage":
        return cls(
            role=_canonical_role(role),
            parts=[CanonicalMessagePart(kind="text", text=content)],
            metadata=metadata or {},
        )


class ProviderStreamCanonicalEvent(BaseModel):
    schema_version: str = "harness.provider_stream_canonical_event/v1"
    provider_event_kind: str
    part: CanonicalMessagePart | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


def canonical_messages_from_provider_request(request: Any) -> list[CanonicalMessage]:
    existing = getattr(request, "canonical_messages", None)
    if existing:
        return [item if isinstance(item, CanonicalMessage) else CanonicalMessage.model_validate(item) for item in existing]
    return [CanonicalMessage.text(message.role, message.content) for message in getattr(request, "messages", [])]


def canonical_messages_to_openai_chat_payload(messages: list[CanonicalMessage], *, protocol: str = "openai_chat") -> list[dict[str, str]]:
    payload: list[dict[str, str]] = []
    for message in messages:
        content_parts: list[str] = []
        for part in message.parts:
            if part.kind == "provider_metadata":
                continue
            if part.kind != "text":
                raise UnsupportedCanonicalPartError(part.kind, protocol, role=message.role)
            if part.text:
                content_parts.append(part.text)
        payload.append({"role": message.role, "content": "\n".join(content_parts)})
    return payload


def canonical_messages_to_text_prompt(messages: list[CanonicalMessage], *, protocol: str = "codex_cli") -> str:
    lines: list[str] = []
    for message in messages:
        content_parts: list[str] = []
        for part in message.parts:
            if part.kind == "provider_metadata":
                continue
            if part.kind != "text":
                raise UnsupportedCanonicalPartError(part.kind, protocol, role=message.role)
            if part.text:
                content_parts.append(part.text)
        lines.append(f"{message.role.upper()}:\n{chr(10).join(content_parts)}")
    return "\n\n".join(lines)


def canonical_part_from_provider_event(event: Any) -> ProviderStreamCanonicalEvent:
    raw_kind = getattr(event, "kind", "")
    kind = str(getattr(raw_kind, "value", raw_kind))
    payload = dict(getattr(event, "payload", {}) or {})
    text = getattr(event, "text", None)
    if kind == "model.message_delta":
        return ProviderStreamCanonicalEvent(
            provider_event_kind=kind,
            part=CanonicalMessagePart(kind="text", text=text or payload.get("delta")),
            metadata=payload,
        )
    if kind == "reasoning.summary_delta":
        signature = payload.get("signature") or payload.get("thought_signature")
        return ProviderStreamCanonicalEvent(
            provider_event_kind=kind,
            part=CanonicalMessagePart(kind="reasoning", text=text or payload.get("delta"), signature=signature, metadata=payload),
            metadata=payload,
        )
    if kind in {"tool_call.started", "tool_call.delta", "tool_call.completed"}:
        return ProviderStreamCanonicalEvent(
            provider_event_kind=kind,
            part=CanonicalMessagePart(
                kind="tool_call",
                data={
                    "tool_call_id": getattr(event, "tool_call_id", None) or payload.get("tool_call_id"),
                    "tool_name": getattr(event, "tool_name", None) or payload.get("tool_name"),
                    "arguments": payload.get("arguments"),
                    "arguments_delta": payload.get("arguments_delta"),
                },
                provider_native_id=getattr(event, "tool_call_id", None) or payload.get("tool_call_id"),
                metadata=payload,
            ),
            metadata=payload,
        )
    if kind == "model.failed":
        error = payload.get("error") if isinstance(payload.get("error"), dict) else payload
        return ProviderStreamCanonicalEvent(
            provider_event_kind=kind,
            part=CanonicalMessagePart(kind="error", text=str(error.get("message") or "Provider failed."), data=dict(error)),
            metadata=payload,
        )
    return ProviderStreamCanonicalEvent(provider_event_kind=kind, metadata=payload)


def _canonical_role(role: str) -> CanonicalRole:
    if role in {"system", "developer", "user", "assistant", "tool"}:
        return role  # type: ignore[return-value]
    return "user"
