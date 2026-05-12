from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Protocol

from harness.backends.codex_cli import CodexCliBackend
from harness.backends.local_openai import LocalEndpointUnavailable, LocalOpenAICompatibleBackend
from harness.config import default_config, load_config


@dataclass(frozen=True)
class ChatMessage:
    role: str
    content: str


@dataclass(frozen=True)
class ChatContext:
    project_root: str
    model_profile: str
    mode: str
    context_blocks: list[dict[str, Any]] = field(default_factory=list)
    safety_boundaries: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ChatDelta:
    content: str


@dataclass(frozen=True)
class ChatResponse:
    content: str
    raw: dict[str, Any] | None = None
    action_proposals: list[dict[str, Any]] = field(default_factory=list)


class ChatModel(Protocol):
    def stream(self, messages: list[ChatMessage], context: ChatContext) -> Iterator[ChatDelta]:
        ...

    def complete(self, messages: list[ChatMessage], context: ChatContext) -> ChatResponse:
        ...


@dataclass
class LocalOpenAIChatModel:
    backend: LocalOpenAICompatibleBackend

    def stream(self, messages: list[ChatMessage], context: ChatContext) -> Iterator[ChatDelta]:
        yield ChatDelta(content=self.complete(messages, context).content)

    def complete(self, messages: list[ChatMessage], context: ChatContext) -> ChatResponse:
        payload = [{"role": message.role, "content": message.content} for message in messages]
        return ChatResponse(content=self.backend.complete(payload))


@dataclass
class CodexCliChatModel:
    backend: CodexCliBackend
    project_root: Path

    def stream(self, messages: list[ChatMessage], context: ChatContext) -> Iterator[ChatDelta]:
        yield ChatDelta(content=self.complete(messages, context).content)

    def complete(self, messages: list[ChatMessage], context: ChatContext) -> ChatResponse:
        prompt = _codex_chat_prompt(messages, context)
        try:
            with tempfile.TemporaryDirectory(prefix="harness-codex-chat-") as tmp:
                final_message_path = Path(tmp) / "final_message.md"
                result = self.backend.run_read_only(self.project_root, prompt, final_message_path)
        except Exception as exc:
            raise LocalEndpointUnavailable(f"Codex CLI subscription chat is unavailable. Details: {exc}") from exc
        if result.exit_status != 0:
            message = (result.stderr or result.stdout or "Codex CLI returned a non-zero exit status.").strip()
            raise LocalEndpointUnavailable(f"Codex CLI subscription chat failed. Details: {message}")
        content = result.final_message or _last_assistant_message(result.json_events) or result.stdout
        return ChatResponse(content=content.strip())


def build_default_chat_model(project_root: Path) -> ChatModel:
    try:
        cfg = load_config(project_root)
    except FileNotFoundError:
        cfg = default_config()
    profile_name = cfg.chat.default_model_profile
    if profile_name == "codex_cli":
        if not cfg.chat.allow_codex_subscription_chat:
            raise LocalEndpointUnavailable("Codex CLI subscription chat is disabled in chat config.")
        return CodexCliChatModel(CodexCliBackend(cfg.backends[profile_name]), project_root)
    if profile_name == "paid_openai_compatible" and not cfg.chat.allow_hosted_chat:
        raise LocalEndpointUnavailable("Hosted chat is disabled. Configure chat.allow_hosted_chat before using it.")
    if profile_name != "local_openai_compatible":
        raise LocalEndpointUnavailable(f"Unsupported chat model profile: {profile_name}")
    return LocalOpenAIChatModel(LocalOpenAICompatibleBackend(cfg.backends[profile_name]))


def _codex_chat_prompt(messages: list[ChatMessage], context: ChatContext) -> str:
    transcript = "\n\n".join(f"{message.role.upper()}:\n{message.content}" for message in messages)
    boundaries = "\n".join(f"- {item}" for item in context.safety_boundaries)
    return (
        "You are running as the Harness chat model through Codex CLI subscription. "
        "Answer the latest user message conversationally and use the supplied Harness context. "
        "The Codex subprocess that hosts this chat is read-only, but the Harness assistant is act-capable. "
        "For read-only inspection, request a Harness read tool. For edits, tests, apply-back, task creation, "
        "approvals, security checks, or other side effects, emit exactly one harness.tool_request/v1 JSON object "
        "for the appropriate gated Harness tool instead of saying you cannot do it. Harness will validate it, "
        "show an action contract, ask for confirmation, and execute through its control plane. Do not claim a "
        "side effect is complete until Harness returns evidence.\n\n"
        f"Project root: {context.project_root}\n"
        f"Mode: {context.mode}\n"
        f"Safety boundaries:\n{boundaries}\n\n"
        f"Transcript:\n{transcript}\n"
    )


def _last_assistant_message(events: list[dict[str, Any]]) -> str | None:
    for event in reversed(events):
        message = event.get("message") or event.get("content") or event.get("text")
        if isinstance(message, str) and message.strip():
            return message
    return None
