from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from harness.memory.sqlite_store import SQLiteStore
from harness.models import EventStreamType, RedactionState, SessionMessageRole, SessionPartKind, SessionStatus
from harness.provider_adapters import ProviderAdapter
from harness.provider_events import ProviderError, ProviderErrorCategory, ProviderEvent, ProviderEventKind, ProviderMessage, ProviderRequest
from harness.security import sanitize_for_logging


RUNTIME_SCHEMA_VERSION = "harness.session_runtime_state/v1"
PROMPT_ACCEPTED_SCHEMA_VERSION = "harness.session_prompt_accepted/v1"
PROMPT_EXECUTION_SCHEMA_VERSION = "harness.session_prompt_execution/v1"
RUNTIME_COMPACTION_SCHEMA_VERSION = "harness.runtime_compaction/v1"

MAX_TRANSIENT_RETRIES = 1
MAX_CONTEXT_OVERFLOW_COMPACTIONS = 1
DEFAULT_RETRY_DELAY_SECONDS = 0.05


class SessionRuntimePhase(str, Enum):
    IDLE = "idle"
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_PERMISSION = "waiting_permission"
    COMPACTING = "compacting"
    RETRY_WAIT = "retry_wait"
    ABORTING = "aborting"
    FAILED = "failed"
    CLOSED = "closed"


class SessionPromptQueuePolicy(str, Enum):
    REJECT_IF_BUSY = "reject_if_busy"
    STEER = "steer"
    FOLLOW_UP = "follow_up"
    NEXT_TURN = "next_turn"


class SessionPromptRequest(BaseModel):
    schema_version: str = "harness.session_prompt_request/v1"
    session_id: str
    content: str
    mode: Literal["sync", "async"] = "async"
    queue_policy: SessionPromptQueuePolicy = SessionPromptQueuePolicy.FOLLOW_UP
    agent_id: str | None = None
    model_ref: str | None = None
    message_id: str | None = None
    part_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class QueuedPrompt(BaseModel):
    schema_version: str = "harness.queued_prompt/v1"
    prompt_id: str
    session_id: str
    content: str
    content_preview: str
    mode: str
    queue_policy: str
    agent_id: str | None = None
    model_ref: str | None = None
    message_id: str | None = None
    part_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class SessionRuntimeState(BaseModel):
    schema_version: str = RUNTIME_SCHEMA_VERSION
    session_id: str
    phase: SessionRuntimePhase
    active_turn_id: str | None = None
    active_run_id: str | None = None
    active_task_id: str | None = None
    waiting_permission_id: str | None = None
    active_process_ids: list[str] = Field(default_factory=list)
    queued_prompt_count: int = 0
    queued_prompt_ids: list[str] = Field(default_factory=list)
    last_event_seq: int | None = None
    updated_at: datetime
    execution_enabled: bool = False
    process_running: bool = False


class SessionPromptAccepted(BaseModel):
    schema_version: str = PROMPT_ACCEPTED_SCHEMA_VERSION
    ok: bool
    accepted: bool
    session_id: str
    prompt_id: str | None = None
    queued: bool = False
    queue_policy: str
    phase: SessionRuntimePhase
    reason: str | None = None
    execution_started: bool = False
    worker_started: bool = False
    runtime: SessionRuntimeState


class SessionPromptExecution(BaseModel):
    schema_version: str = PROMPT_EXECUTION_SCHEMA_VERSION
    session_id: str
    prompt_id: str
    turn_id: str
    content: str
    agent_id: str | None = None
    model_ref: str | None = None
    message_id: str | None = None
    part_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class SessionRuntimeTextProvider(Protocol):
    def complete(self, execution: SessionPromptExecution) -> str:
        ...


class SessionRuntimeBusyError(RuntimeError):
    pass


class SessionRuntimeProviderUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class _ProviderStreamResult:
    response_text: str
    failed: bool = False
    waiting_permission_id: str | None = None
    error: ProviderError | None = None
    context_overflow: bool = False


class _RuntimeSessionSlot:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.lock = threading.RLock()
        self.condition = threading.Condition(self.lock)
        self.phase = SessionRuntimePhase.IDLE
        self.active_turn_id: str | None = None
        self.active_prompt_id: str | None = None
        self.active_run_id: str | None = None
        self.active_task_id: str | None = None
        self.waiting_permission_id: str | None = None
        self.active_process_ids: list[str] = []
        self.queued_prompts: list[QueuedPrompt] = []
        self.active_worker_thread: threading.Thread | None = None
        self.updated_at = _utc_now()

    def state(
        self,
        *,
        last_event_seq: int | None,
        terminal: bool = False,
        execution_enabled: bool = True,
    ) -> SessionRuntimeState:
        phase = SessionRuntimePhase.CLOSED if terminal else self.phase
        worker_running = (
            self.active_worker_thread is not None
            and self.active_worker_thread.is_alive()
            and phase in {SessionRuntimePhase.RUNNING, SessionRuntimePhase.ABORTING}
        )
        return SessionRuntimeState(
            session_id=self.session_id,
            phase=phase,
            active_turn_id=None if terminal else self.active_turn_id,
            active_run_id=None if terminal else self.active_run_id,
            active_task_id=None if terminal else self.active_task_id,
            waiting_permission_id=None if terminal else self.waiting_permission_id,
            active_process_ids=[] if terminal else list(self.active_process_ids),
            queued_prompt_count=0 if terminal else len(self.queued_prompts),
            queued_prompt_ids=[] if terminal else [prompt.prompt_id for prompt in self.queued_prompts],
            last_event_seq=last_event_seq,
            updated_at=self.updated_at,
            execution_enabled=execution_enabled,
            process_running=False if terminal else bool(self.active_process_ids) or worker_running,
        )


class _RuntimeProjectState:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.sessions: dict[str, _RuntimeSessionSlot] = {}

    def slot(self, session_id: str) -> _RuntimeSessionSlot:
        with self.lock:
            existing = self.sessions.get(session_id)
            if existing is not None:
                return existing
            created = _RuntimeSessionSlot(session_id)
            self.sessions[session_id] = created
            return created


_PROJECT_STATES: dict[str, _RuntimeProjectState] = {}
_PROJECT_STATES_LOCK = threading.RLock()


class SessionRuntimeManager:
    """Process-local owner for live session runtime state."""

    def __init__(
        self,
        project_root: Path,
        store: SQLiteStore | None = None,
        *,
        text_provider: SessionRuntimeTextProvider | None = None,
        provider_adapter: ProviderAdapter | None = None,
        execution_enabled: bool = True,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.store = store or SQLiteStore.open_initialized(self.project_root)
        self._project_state = _project_state(self.project_root)
        self.text_provider = text_provider
        self.provider_adapter = provider_adapter
        self.execution_enabled = execution_enabled

    @classmethod
    def for_store(
        cls,
        store: SQLiteStore,
        *,
        text_provider: SessionRuntimeTextProvider | None = None,
        provider_adapter: ProviderAdapter | None = None,
        execution_enabled: bool = True,
    ) -> "SessionRuntimeManager":
        return cls(
            store.project_root,
            store,
            text_provider=text_provider,
            provider_adapter=provider_adapter,
            execution_enabled=execution_enabled,
        )

    def status(self, session_id: str) -> SessionRuntimeState:
        session = self.store.get_session(session_id)
        slot = self._project_state.slot(session_id)
        terminal = _is_terminal_status(session.status)
        with slot.condition:
            if not terminal and session.status == SessionStatus.WAITING_APPROVAL:
                slot.phase = SessionRuntimePhase.WAITING_PERMISSION
                slot.updated_at = _utc_now()
            elif not terminal and session.status == SessionStatus.RUNNING and slot.phase == SessionRuntimePhase.IDLE:
                slot.phase = SessionRuntimePhase.RUNNING
                slot.updated_at = _utc_now()
            return slot.state(
                last_event_seq=self._last_event_seq(session_id),
                terminal=terminal,
                execution_enabled=self.execution_enabled,
            )

    def submit_prompt(self, request: SessionPromptRequest) -> SessionPromptAccepted:
        session = self.store.get_session(request.session_id)
        slot = self._project_state.slot(request.session_id)
        terminal = _is_terminal_status(session.status)
        thread_to_start: threading.Thread | None = None
        execution_started = False
        with slot.condition:
            if terminal:
                state = slot.state(
                    last_event_seq=self._last_event_seq(request.session_id),
                    terminal=True,
                    execution_enabled=self.execution_enabled,
                )
                return SessionPromptAccepted(
                    ok=False,
                    accepted=False,
                    session_id=request.session_id,
                    queue_policy=request.queue_policy.value,
                    phase=state.phase,
                    reason=f"Session is terminal: {session.status.value}.",
                    runtime=state,
                )
            busy = _runtime_busy(slot)
            if busy and request.queue_policy == SessionPromptQueuePolicy.REJECT_IF_BUSY:
                state = slot.state(last_event_seq=self._last_event_seq(request.session_id), execution_enabled=self.execution_enabled)
                return SessionPromptAccepted(
                    ok=False,
                    accepted=False,
                    session_id=request.session_id,
                    queue_policy=request.queue_policy.value,
                    phase=state.phase,
                    reason=f"Session runtime is busy: {slot.phase.value}.",
                    runtime=state,
                )
            prompt = QueuedPrompt(
                prompt_id=f"prompt_{uuid.uuid4().hex[:12]}",
                session_id=request.session_id,
                content=str(sanitize_for_logging(request.content)),
                content_preview=_preview(request.content),
                mode=request.mode,
                queue_policy=request.queue_policy.value,
                agent_id=request.agent_id,
                model_ref=request.model_ref,
                message_id=request.message_id,
                part_id=request.part_id,
                metadata=request.metadata,
                created_at=_utc_now(),
            )
            slot.queued_prompts.append(prompt)
            if slot.phase in {SessionRuntimePhase.IDLE, SessionRuntimePhase.FAILED}:
                slot.phase = SessionRuntimePhase.QUEUED
            slot.updated_at = _utc_now()
            self._record_prompt_queued(request, prompt)
            if self.execution_enabled and not busy:
                thread_to_start = self._start_next_prompt_locked(slot)
                execution_started = thread_to_start is not None
            state = slot.state(last_event_seq=self._last_event_seq(request.session_id), execution_enabled=self.execution_enabled)
            slot.condition.notify_all()
        if thread_to_start is not None:
            thread_to_start.start()
        queued = prompt.prompt_id in state.queued_prompt_ids
        if execution_started:
            reason = "Prompt accepted and execution worker started."
        elif self.execution_enabled:
            reason = "Prompt accepted into runtime queue."
        else:
            reason = "Prompt accepted into runtime queue; execution worker is disabled."
        return SessionPromptAccepted(
            ok=True,
            accepted=True,
            session_id=request.session_id,
            prompt_id=prompt.prompt_id,
            queued=queued,
            queue_policy=request.queue_policy.value,
            phase=state.phase,
            reason=reason,
            execution_started=execution_started,
            worker_started=execution_started,
            runtime=state,
        )

    def wait(self, session_id: str, *, timeout: float | None = None) -> SessionRuntimeState:
        self.store.get_session(session_id)
        slot = self._project_state.slot(session_id)
        deadline = None if timeout is None else time.monotonic() + max(timeout, 0.0)
        with slot.condition:
            while True:
                session = self.store.get_session(session_id)
                terminal = _is_terminal_status(session.status)
                if terminal or _runtime_wait_complete(slot):
                    return slot.state(
                        last_event_seq=self._last_event_seq(session_id),
                        terminal=terminal,
                        execution_enabled=self.execution_enabled,
                    )
                if deadline is None:
                    slot.condition.wait()
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return slot.state(last_event_seq=self._last_event_seq(session_id), execution_enabled=self.execution_enabled)
                slot.condition.wait(remaining)

    def begin_turn(
        self,
        session_id: str,
        *,
        turn_id: str | None = None,
        run_id: str | None = None,
        task_id: str | None = None,
    ) -> SessionRuntimeState:
        self.store.get_session(session_id)
        slot = self._project_state.slot(session_id)
        with slot.condition:
            if _runtime_busy(slot):
                raise SessionRuntimeBusyError(f"Session runtime is busy: {slot.phase.value}.")
            slot.phase = SessionRuntimePhase.RUNNING
            slot.active_turn_id = turn_id or f"turn_{uuid.uuid4().hex[:12]}"
            slot.active_run_id = run_id
            slot.active_task_id = task_id
            slot.waiting_permission_id = None
            slot.updated_at = _utc_now()
            slot.condition.notify_all()
            return slot.state(last_event_seq=self._last_event_seq(session_id), execution_enabled=self.execution_enabled)

    def wait_for_permission(self, session_id: str, permission_id: str) -> SessionRuntimeState:
        self.store.get_session(session_id)
        slot = self._project_state.slot(session_id)
        with slot.condition:
            slot.phase = SessionRuntimePhase.WAITING_PERMISSION
            slot.waiting_permission_id = permission_id
            slot.updated_at = _utc_now()
            slot.condition.notify_all()
            return slot.state(last_event_seq=self._last_event_seq(session_id), execution_enabled=self.execution_enabled)

    def finish_turn(self, session_id: str, *, failed: bool = False) -> SessionRuntimeState:
        self.store.get_session(session_id)
        slot = self._project_state.slot(session_id)
        with slot.condition:
            slot.active_turn_id = None
            slot.active_prompt_id = None
            slot.active_run_id = None
            slot.active_task_id = None
            slot.waiting_permission_id = None
            slot.phase = SessionRuntimePhase.FAILED if failed else (
                SessionRuntimePhase.QUEUED if slot.queued_prompts else SessionRuntimePhase.IDLE
            )
            slot.updated_at = _utc_now()
            slot.condition.notify_all()
            return slot.state(last_event_seq=self._last_event_seq(session_id), execution_enabled=self.execution_enabled)

    def abort(self, session_id: str, *, reason: str | None = None) -> SessionRuntimeState:
        self.store.get_session(session_id)
        slot = self._project_state.slot(session_id)
        with slot.condition:
            slot.phase = SessionRuntimePhase.ABORTING
            slot.updated_at = _utc_now()
            self.store.append_store_event(
                EventStreamType.SESSION,
                session_id,
                "harness.runtime.abort_requested",
                {
                    "reason": sanitize_for_logging(reason) if reason else None,
                    "process_stopped": False,
                    "execution_enabled": self.execution_enabled,
                    "summary": "runtime abort requested",
                },
                session_id=session_id,
                redaction_state=RedactionState.REDACTED if reason else RedactionState.NOT_REQUIRED,
            )
            slot.condition.notify_all()
            return slot.state(last_event_seq=self._last_event_seq(session_id), execution_enabled=self.execution_enabled)

    def permission_resolved(
        self,
        session_id: str,
        permission_id: str,
        *,
        decision: str,
        resumed: bool = False,
    ) -> SessionRuntimeState:
        self.store.get_session(session_id)
        slot = self._project_state.slot(session_id)
        with slot.condition:
            was_waiting = slot.phase == SessionRuntimePhase.WAITING_PERMISSION and slot.waiting_permission_id == permission_id
            turn_id = slot.active_turn_id
            prompt_id = slot.active_prompt_id
            if was_waiting:
                slot.active_turn_id = None
                slot.active_prompt_id = None
                slot.active_run_id = None
                slot.active_task_id = None
                slot.waiting_permission_id = None
                slot.phase = SessionRuntimePhase.QUEUED if slot.queued_prompts else SessionRuntimePhase.IDLE
                slot.updated_at = _utc_now()
                try:
                    self.store.update_session(session_id, status=SessionStatus.ACTIVE)
                except Exception:
                    pass
                self.store.append_store_event(
                    EventStreamType.SESSION,
                    session_id,
                    "harness.runtime.permission_resolved",
                    {
                        "turn_id": turn_id,
                        "prompt_id": prompt_id,
                        "permission_id": permission_id,
                        "decision": decision,
                        "resumed": resumed,
                        "summary": f"permission {decision}",
                    },
                    session_id=session_id,
                    redaction_state=RedactionState.NOT_REQUIRED,
                )
                self.store.append_store_event(
                    EventStreamType.SESSION,
                    session_id,
                    "harness.turn.finished",
                    {
                        "turn_id": turn_id,
                        "prompt_id": prompt_id,
                        "permission_id": permission_id,
                        "failed": decision not in {"allowed", "allow"},
                        "resumed": resumed,
                    },
                    session_id=session_id,
                    redaction_state=RedactionState.NOT_REQUIRED,
                )
            slot.condition.notify_all()
            return slot.state(last_event_seq=self._last_event_seq(session_id), execution_enabled=self.execution_enabled)

    def _record_prompt_queued(self, request: SessionPromptRequest, prompt: QueuedPrompt) -> None:
        self.store.append_store_event(
            EventStreamType.SESSION,
            request.session_id,
            "harness.runtime.prompt_queued",
            {
                "prompt": prompt.model_dump(mode="json"),
                "mode": request.mode,
                "queue_policy": request.queue_policy.value,
                "execution_started": False,
                "worker_started": False,
                "summary": "prompt accepted into runtime queue",
            },
            session_id=request.session_id,
            redaction_state=RedactionState.REDACTED,
        )

    def _start_next_prompt_locked(self, slot: _RuntimeSessionSlot) -> threading.Thread | None:
        if not slot.queued_prompts or _runtime_busy(slot):
            return None
        prompt = slot.queued_prompts.pop(0)
        turn_id = f"turn_{uuid.uuid4().hex[:12]}"
        slot.phase = SessionRuntimePhase.RUNNING
        slot.active_turn_id = turn_id
        slot.active_prompt_id = prompt.prompt_id
        slot.active_run_id = None
        slot.active_task_id = None
        slot.waiting_permission_id = None
        slot.updated_at = _utc_now()
        thread = threading.Thread(
            target=self._run_prompt_turn,
            name=f"harness-session-runtime-{slot.session_id}-{turn_id}",
            args=(prompt, turn_id),
            daemon=True,
        )
        slot.active_worker_thread = thread
        slot.condition.notify_all()
        return thread

    def _run_prompt_turn(self, prompt: QueuedPrompt, turn_id: str) -> None:
        failed = False
        suspended = False
        session_id = prompt.session_id
        try:
            self.store.update_session(session_id, status=SessionStatus.RUNNING)
            self.store.append_store_event(
                EventStreamType.SESSION,
                session_id,
                "harness.turn.started",
                {
                    "turn_id": turn_id,
                    "prompt_id": prompt.prompt_id,
                    "message_id": prompt.message_id,
                    "part_id": prompt.part_id,
                    "execution_enabled": True,
                    "provider_execution_started": True,
                    "model_execution_started": True,
                    "permission_granting": False,
                },
                session_id=session_id,
                message_id=prompt.message_id,
                redaction_state=RedactionState.NOT_REQUIRED,
            )
            if self.provider_adapter is not None:
                stream_result = self._run_provider_with_recovery(prompt, turn_id)
                if stream_result.failed:
                    failed = True
                    self.store.append_store_event(
                        EventStreamType.SESSION,
                        session_id,
                        "harness.turn.finished",
                        {
                            "turn_id": turn_id,
                            "prompt_id": prompt.prompt_id,
                            "failed": True,
                            "provider_error": stream_result.error.model_dump(mode="json") if stream_result.error else None,
                            "context_overflow": stream_result.context_overflow,
                        },
                        session_id=session_id,
                        redaction_state=RedactionState.NOT_REQUIRED,
                    )
                    self.store.update_session(session_id, status=SessionStatus.ACTIVE)
                    return
                if stream_result.waiting_permission_id:
                    suspended = True
                    slot = self._project_state.slot(session_id)
                    with slot.condition:
                        if slot.active_turn_id == turn_id:
                            slot.phase = SessionRuntimePhase.WAITING_PERMISSION
                            slot.waiting_permission_id = stream_result.waiting_permission_id
                            slot.updated_at = _utc_now()
                            slot.condition.notify_all()
                    self.store.update_session(session_id, status=SessionStatus.WAITING_APPROVAL)
                    self.store.append_store_event(
                        EventStreamType.SESSION,
                        session_id,
                        "harness.runtime.permission_waiting",
                        {
                            "turn_id": turn_id,
                            "prompt_id": prompt.prompt_id,
                            "permission_id": stream_result.waiting_permission_id,
                            "summary": "runtime waiting on permission",
                        },
                        session_id=session_id,
                        message_id=prompt.message_id,
                        redaction_state=RedactionState.NOT_REQUIRED,
                    )
                    return
                response_text = stream_result.response_text
            else:
                self.store.append_store_event(
                    EventStreamType.SESSION,
                    session_id,
                    "model.started",
                    {
                        "turn_id": turn_id,
                        "prompt_id": prompt.prompt_id,
                        "model_ref": prompt.model_ref,
                        "provider_execution_started": True,
                        "model_execution_started": True,
                        "hidden_provider_fallback": False,
                        "no_hidden_fallback": True,
                    },
                    session_id=session_id,
                    message_id=prompt.message_id,
                    redaction_state=RedactionState.NOT_REQUIRED,
                )
                response_text = self._complete_prompt(prompt, turn_id)
            if self.provider_adapter is not None and not response_text:
                self.store.append_store_event(
                    EventStreamType.SESSION,
                    session_id,
                    "harness.turn.finished",
                    {"turn_id": turn_id, "prompt_id": prompt.prompt_id, "failed": False},
                    session_id=session_id,
                    redaction_state=RedactionState.NOT_REQUIRED,
                )
                self.store.update_session(session_id, status=SessionStatus.ACTIVE)
                return
            assistant = self.store.append_session_message(
                session_id,
                SessionMessageRole.ASSISTANT,
                response_text,
                parent_message_id=prompt.message_id,
                agent_id=prompt.agent_id,
            )
            part = self.store.append_session_part(
                session_id,
                assistant.id,
                SessionPartKind.TEXT,
                text=response_text,
                metadata={"source": "session_runtime", "turn_id": turn_id, "prompt_id": prompt.prompt_id},
                redaction_state=RedactionState.REDACTED,
            )
            if self.provider_adapter is None:
                self.store.append_store_event(
                    EventStreamType.SESSION,
                    session_id,
                    "model.message_delta",
                    {
                        "turn_id": turn_id,
                        "prompt_id": prompt.prompt_id,
                        "message_id": assistant.id,
                        "part_id": part.id,
                        "delta": response_text,
                    },
                    session_id=session_id,
                    message_id=assistant.id,
                    redaction_state=RedactionState.REDACTED,
                )
                self.store.append_store_event(
                    EventStreamType.SESSION,
                    session_id,
                    "model.completed",
                    {
                        "turn_id": turn_id,
                        "prompt_id": prompt.prompt_id,
                        "message_id": assistant.id,
                        "part_id": part.id,
                    },
                    session_id=session_id,
                    message_id=assistant.id,
                    redaction_state=RedactionState.NOT_REQUIRED,
                )
            self.store.append_store_event(
                EventStreamType.SESSION,
                session_id,
                "harness.turn.finished",
                {"turn_id": turn_id, "prompt_id": prompt.prompt_id, "failed": False},
                session_id=session_id,
                redaction_state=RedactionState.NOT_REQUIRED,
            )
            self.store.update_session(session_id, status=SessionStatus.ACTIVE)
        except Exception as exc:
            failed = True
            self._record_turn_failed(prompt, turn_id, exc)
            try:
                self.store.update_session(session_id, status=SessionStatus.ACTIVE)
            except Exception:
                pass
        finally:
            self._finish_worker_turn(session_id, turn_id, failed=failed, suspended=suspended)

    def _complete_prompt(self, prompt: QueuedPrompt, turn_id: str) -> str:
        if self.text_provider is None:
            raise SessionRuntimeProviderUnavailable(
                "No session runtime text provider is configured; refusing to use a hidden provider fallback."
            )
        execution = SessionPromptExecution(
            session_id=prompt.session_id,
            prompt_id=prompt.prompt_id,
            turn_id=turn_id,
            content=prompt.content,
            agent_id=prompt.agent_id,
            model_ref=prompt.model_ref,
            message_id=prompt.message_id,
            part_id=prompt.part_id,
            metadata={"source": "session_runtime"},
        )
        return str(self.text_provider.complete(execution))

    def _run_provider_with_recovery(self, prompt: QueuedPrompt, turn_id: str) -> _ProviderStreamResult:
        compaction: dict[str, Any] | None = None
        transient_retries = 0
        compactions = 0
        attempt = 1
        while True:
            result = self._stream_provider_prompt(prompt, turn_id, attempt=attempt, compaction=compaction)
            if not result.failed or result.waiting_permission_id:
                return result
            if result.context_overflow and compactions < MAX_CONTEXT_OVERFLOW_COMPACTIONS:
                compactions += 1
                compaction = self._compact_runtime_context(prompt, turn_id, attempt=attempt, error=result.error)
                if self._runtime_aborting(prompt.session_id):
                    return _ProviderStreamResult(response_text="", failed=True, error=result.error, context_overflow=True)
                self._record_retry_scheduled(
                    prompt,
                    turn_id,
                    attempt=attempt,
                    next_attempt=attempt + 1,
                    reason="context_overflow",
                    error=result.error,
                    delay_seconds=0.0,
                )
                attempt += 1
                continue
            if result.error is not None and result.error.retryable and transient_retries < MAX_TRANSIENT_RETRIES:
                transient_retries += 1
                delay = _retry_delay_seconds(prompt)
                if not self._wait_for_retry(prompt, turn_id, attempt=attempt, delay_seconds=delay, error=result.error):
                    return _ProviderStreamResult(response_text="", failed=True, error=result.error, context_overflow=result.context_overflow)
                attempt += 1
                continue
            return result

    def _stream_provider_prompt(
        self,
        prompt: QueuedPrompt,
        turn_id: str,
        *,
        attempt: int,
        compaction: dict[str, Any] | None = None,
    ) -> _ProviderStreamResult:
        if self.provider_adapter is None:
            return _ProviderStreamResult(response_text="", failed=True)
        messages = [ProviderMessage(role=SessionMessageRole.USER.value, content=prompt.content)]
        if compaction is not None:
            summary = str(compaction.get("summary") or "").strip()
            if summary:
                messages.insert(0, ProviderMessage(role="system", content=f"Conversation context was compacted before retry:\n{summary}"))
        request = ProviderRequest(
            session_id=prompt.session_id,
            turn_id=turn_id,
            prompt_id=prompt.prompt_id,
            model_ref=prompt.model_ref,
            messages=messages,
            context={
                "project_root": str(self.project_root),
                "mode": prompt.mode,
                "attempt": attempt,
                "context_compaction": compaction,
            },
            metadata={
                "source": "session_runtime",
                "message_id": prompt.message_id,
                "part_id": prompt.part_id,
                "attempt": attempt,
                "compacted": compaction is not None,
            },
        )
        chunks: list[str] = []
        failed = False
        error: ProviderError | None = None
        context_overflow = False
        waiting_permission_id: str | None = None
        for event in self.provider_adapter.stream(request):
            self._append_provider_event(prompt, event)
            if event.kind == ProviderEventKind.MODEL_MESSAGE_DELTA:
                delta = event.text or event.payload.get("delta")
                if isinstance(delta, str):
                    chunks.append(delta)
            elif event.kind == ProviderEventKind.TOOL_CALL_COMPLETED:
                permission_id = self._execute_provider_tool_call(prompt, turn_id, event)
                if permission_id:
                    waiting_permission_id = permission_id
                    break
            elif event.kind == ProviderEventKind.MODEL_FAILED:
                failed = True
                error = _provider_error_from_event(event)
                context_overflow = _provider_error_is_context_overflow(error)
        return _ProviderStreamResult(
            response_text="".join(chunks).strip(),
            failed=failed,
            waiting_permission_id=waiting_permission_id,
            error=error,
            context_overflow=context_overflow,
        )

    def _wait_for_retry(
        self,
        prompt: QueuedPrompt,
        turn_id: str,
        *,
        attempt: int,
        delay_seconds: float,
        error: ProviderError | None,
    ) -> bool:
        self._record_retry_scheduled(
            prompt,
            turn_id,
            attempt=attempt,
            next_attempt=attempt + 1,
            reason="retryable_provider_error",
            error=error,
            delay_seconds=delay_seconds,
        )
        slot = self._project_state.slot(prompt.session_id)
        deadline = time.monotonic() + max(delay_seconds, 0.0)
        with slot.condition:
            while True:
                if slot.phase == SessionRuntimePhase.ABORTING:
                    self.store.append_store_event(
                        EventStreamType.SESSION,
                        prompt.session_id,
                        "harness.runtime.retry_aborted",
                        {
                            "turn_id": turn_id,
                            "prompt_id": prompt.prompt_id,
                            "attempt": attempt,
                            "reason": "runtime abort requested",
                        },
                        session_id=prompt.session_id,
                        message_id=prompt.message_id,
                        redaction_state=RedactionState.NOT_REQUIRED,
                    )
                    return False
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    slot.phase = SessionRuntimePhase.RUNNING
                    slot.updated_at = _utc_now()
                    slot.condition.notify_all()
                    return True
                slot.condition.wait(min(remaining, 0.05))

    def _record_retry_scheduled(
        self,
        prompt: QueuedPrompt,
        turn_id: str,
        *,
        attempt: int,
        next_attempt: int,
        reason: str,
        error: ProviderError | None,
        delay_seconds: float,
    ) -> None:
        slot = self._project_state.slot(prompt.session_id)
        with slot.condition:
            if slot.active_turn_id == turn_id:
                slot.phase = SessionRuntimePhase.RETRY_WAIT if delay_seconds > 0 else SessionRuntimePhase.RUNNING
                slot.updated_at = _utc_now()
                slot.condition.notify_all()
        self.store.append_store_event(
            EventStreamType.SESSION,
            prompt.session_id,
            "harness.runtime.retry_scheduled",
            {
                "turn_id": turn_id,
                "prompt_id": prompt.prompt_id,
                "attempt": attempt,
                "next_attempt": next_attempt,
                "reason": reason,
                "delay_seconds": delay_seconds,
                "provider_error": error.model_dump(mode="json") if error else None,
                "summary": f"retry scheduled after {reason}",
            },
            session_id=prompt.session_id,
            message_id=prompt.message_id,
            redaction_state=RedactionState.REDACTED if error else RedactionState.NOT_REQUIRED,
        )

    def _compact_runtime_context(
        self,
        prompt: QueuedPrompt,
        turn_id: str,
        *,
        attempt: int,
        error: ProviderError | None,
    ) -> dict[str, Any]:
        slot = self._project_state.slot(prompt.session_id)
        with slot.condition:
            if slot.active_turn_id == turn_id:
                slot.phase = SessionRuntimePhase.COMPACTING
                slot.updated_at = _utc_now()
                slot.condition.notify_all()
        self.store.append_store_event(
            EventStreamType.SESSION,
            prompt.session_id,
            "harness.runtime.compaction.started",
            {
                "turn_id": turn_id,
                "prompt_id": prompt.prompt_id,
                "attempt": attempt,
                "reason": "context_overflow",
                "provider_error": error.model_dump(mode="json") if error else None,
                "hidden_provider_fallback": False,
                "no_hidden_fallback": True,
            },
            session_id=prompt.session_id,
            message_id=prompt.message_id,
            redaction_state=RedactionState.REDACTED if error else RedactionState.NOT_REQUIRED,
        )
        compaction = _build_runtime_compaction(self.store, prompt)
        self.store.append_store_event(
            EventStreamType.SESSION,
            prompt.session_id,
            "harness.runtime.compaction.completed",
            {
                **compaction,
                "turn_id": turn_id,
                "prompt_id": prompt.prompt_id,
                "attempt": attempt,
                "reason": "context_overflow",
                "summary": compaction["summary"],
            },
            session_id=prompt.session_id,
            message_id=prompt.message_id,
            redaction_state=RedactionState.REDACTED,
        )
        with slot.condition:
            if slot.active_turn_id == turn_id and slot.phase != SessionRuntimePhase.ABORTING:
                slot.phase = SessionRuntimePhase.RUNNING
                slot.updated_at = _utc_now()
                slot.condition.notify_all()
        return compaction

    def _runtime_aborting(self, session_id: str) -> bool:
        slot = self._project_state.slot(session_id)
        with slot.condition:
            return slot.phase == SessionRuntimePhase.ABORTING

    def _append_provider_event(self, prompt: QueuedPrompt, event: ProviderEvent) -> None:
        self.store.append_store_event(
            EventStreamType.SESSION,
            prompt.session_id,
            event.store_kind(),
            event.store_payload(),
            session_id=prompt.session_id,
            message_id=prompt.message_id,
            redaction_state=_provider_event_redaction(event),
        )

    def _execute_provider_tool_call(self, prompt: QueuedPrompt, turn_id: str, event: ProviderEvent) -> str | None:
        tool_id = _provider_tool_name(event)
        arguments = _provider_tool_arguments(event)
        tool_call_id = event.tool_call_id or str(event.payload.get("tool_call_id") or "")
        if not tool_id:
            self._persist_provider_tool_error(
                prompt,
                event,
                preview="Provider emitted a tool call without a tool name.",
                error_type="invalid_tool_call",
            )
            return None
        try:
            from harness.models import RunMode
            from harness.session_tools import execute_session_tool

            result = execute_session_tool(
                self.store,
                self.project_root,
                prompt.session_id,
                tool_id,
                arguments,
                tool_call_id=tool_call_id or None,
                turn_id=turn_id,
                run_mode=RunMode.READ_ONLY,
            )
            if result.error_type == "permission_required":
                return result.permission_id
            return None
        except Exception as exc:
            self._persist_provider_tool_error(
                prompt,
                event,
                preview=str(sanitize_for_logging(str(exc))),
                error_type="unknown_tool" if isinstance(exc, KeyError) else "invalid_tool_call",
            )
            return None

    def _persist_provider_tool_error(
        self,
        prompt: QueuedPrompt,
        event: ProviderEvent,
        *,
        preview: str,
        error_type: str,
    ) -> None:
        tool_id = _provider_tool_name(event) or "unknown"
        tool_call_id = event.tool_call_id or str(event.payload.get("tool_call_id") or "")
        output = f"Tool call failed: {preview}"
        message = self.store.append_session_message(prompt.session_id, SessionMessageRole.TOOL, output)
        self.store.append_session_part(
            prompt.session_id,
            message.id,
            SessionPartKind.TOOL_RESULT,
            text=output,
            metadata={
                "tool_id": tool_id,
                "tool_call_id": tool_call_id or None,
                "ok": False,
                "error_type": error_type,
                "model_visible": True,
                "provider_native_tool_call": True,
                "permission_granting": False,
            },
            redaction_state=RedactionState.REDACTED,
        )
        payload = {
            "tool_id": tool_id,
            "tool_call_id": tool_call_id or None,
            "ok": False,
            "preview": output,
            "error_type": error_type,
            "provider_event": event.model_dump(mode="json"),
            "summary": output[:240],
        }
        self.store.append_store_event(
            EventStreamType.SESSION,
            prompt.session_id,
            "tool_call.output",
            payload,
            session_id=prompt.session_id,
            message_id=message.id,
            redaction_state=RedactionState.REDACTED,
        )
        self.store.append_store_event(
            EventStreamType.SESSION,
            prompt.session_id,
            "tool_call.finished",
            {"tool_id": tool_id, "tool_call_id": tool_call_id or None, "ok": False, "status": "failed", "summary": "failed"},
            session_id=prompt.session_id,
            message_id=message.id,
            redaction_state=RedactionState.NOT_REQUIRED,
        )

    def _record_turn_failed(self, prompt: QueuedPrompt, turn_id: str, exc: Exception) -> None:
        payload = {
            "turn_id": turn_id,
            "prompt_id": prompt.prompt_id,
            "error_type": type(exc).__name__,
            "message": sanitize_for_logging(str(exc)),
            "hidden_provider_fallback": False,
            "no_hidden_fallback": True,
        }
        self.store.append_store_event(
            EventStreamType.SESSION,
            prompt.session_id,
            "model.failed",
            payload,
            session_id=prompt.session_id,
            message_id=prompt.message_id,
            redaction_state=RedactionState.REDACTED,
        )
        self.store.append_store_event(
            EventStreamType.SESSION,
            prompt.session_id,
            "harness.turn.finished",
            {**payload, "failed": True},
            session_id=prompt.session_id,
            message_id=prompt.message_id,
            redaction_state=RedactionState.REDACTED,
        )

    def _finish_worker_turn(self, session_id: str, turn_id: str, *, failed: bool, suspended: bool = False) -> None:
        slot = self._project_state.slot(session_id)
        thread_to_start: threading.Thread | None = None
        with slot.condition:
            if slot.active_turn_id == turn_id:
                if suspended:
                    slot.phase = SessionRuntimePhase.WAITING_PERMISSION
                    slot.active_worker_thread = None
                    slot.updated_at = _utc_now()
                    slot.condition.notify_all()
                    return
                slot.active_turn_id = None
                slot.active_prompt_id = None
                slot.active_run_id = None
                slot.active_task_id = None
                slot.waiting_permission_id = None
                slot.active_worker_thread = None
                if slot.queued_prompts:
                    slot.phase = SessionRuntimePhase.QUEUED
                    thread_to_start = self._start_next_prompt_locked(slot)
                else:
                    slot.phase = SessionRuntimePhase.FAILED if failed else SessionRuntimePhase.IDLE
                slot.updated_at = _utc_now()
                slot.condition.notify_all()
        if thread_to_start is not None:
            thread_to_start.start()

    def _last_event_seq(self, session_id: str) -> int | None:
        events = self.store.list_session_store_events(session_id)
        return events[-1].seq if events else None


def _project_state(project_root: Path) -> _RuntimeProjectState:
    key = str(Path(project_root).resolve())
    with _PROJECT_STATES_LOCK:
        existing = _PROJECT_STATES.get(key)
        if existing is not None:
            return existing
        created = _RuntimeProjectState()
        _PROJECT_STATES[key] = created
        return created


def reset_session_runtime_state(project_root: Path | str | None = None) -> None:
    with _PROJECT_STATES_LOCK:
        if project_root is None:
            states = list(_PROJECT_STATES.values())
            _PROJECT_STATES.clear()
        else:
            state = _PROJECT_STATES.pop(str(Path(project_root).resolve()), None)
            states = [state] if state is not None else []
    for state in states:
        with state.lock:
            slots = list(state.sessions.values())
            state.sessions.clear()
        for slot in slots:
            with slot.condition:
                slot.phase = SessionRuntimePhase.CLOSED
                slot.active_turn_id = None
                slot.active_prompt_id = None
                slot.active_run_id = None
                slot.active_task_id = None
                slot.waiting_permission_id = None
                slot.active_process_ids.clear()
                slot.queued_prompts.clear()
                slot.updated_at = _utc_now()
                slot.condition.notify_all()


def _runtime_busy(slot: _RuntimeSessionSlot) -> bool:
    return slot.phase in {
        SessionRuntimePhase.RUNNING,
        SessionRuntimePhase.WAITING_PERMISSION,
        SessionRuntimePhase.COMPACTING,
        SessionRuntimePhase.RETRY_WAIT,
        SessionRuntimePhase.ABORTING,
    }


def _runtime_wait_complete(slot: _RuntimeSessionSlot) -> bool:
    return slot.phase in {SessionRuntimePhase.IDLE, SessionRuntimePhase.FAILED, SessionRuntimePhase.CLOSED} and not slot.queued_prompts


def _is_terminal_status(status: SessionStatus) -> bool:
    return status in {
        SessionStatus.COMPLETED,
        SessionStatus.FAILED,
        SessionStatus.CANCELLED,
        SessionStatus.ARCHIVED,
    }


def _provider_event_redaction(event: ProviderEvent) -> RedactionState:
    if event.kind in {
        ProviderEventKind.MODEL_MESSAGE_DELTA,
        ProviderEventKind.REASONING_SUMMARY_DELTA,
        ProviderEventKind.MODEL_FAILED,
    }:
        return RedactionState.REDACTED
    return RedactionState.NOT_REQUIRED


def _provider_tool_name(event: ProviderEvent) -> str | None:
    name = event.tool_name or event.payload.get("tool_name") or event.payload.get("tool") or event.payload.get("name")
    if name is None:
        return None
    text = str(sanitize_for_logging(name)).strip()
    return text or None


def _provider_tool_arguments(event: ProviderEvent) -> dict[str, Any]:
    for key in ("arguments", "args", "input"):
        value = event.payload.get(key)
        if isinstance(value, dict):
            return sanitize_for_logging(value)
    return {}


def _provider_error_from_event(event: ProviderEvent) -> ProviderError | None:
    raw = event.payload.get("error")
    if isinstance(raw, ProviderError):
        return raw
    if isinstance(raw, dict):
        try:
            return ProviderError.model_validate(raw)
        except Exception:
            return ProviderError(
                category=ProviderErrorCategory.UNKNOWN,
                error_type=str(raw.get("error_type") or "ProviderError"),
                message=str(sanitize_for_logging(raw.get("message") or "Provider failed.")),
                retryable=bool(raw.get("retryable")),
            )
    message = event.text or event.payload.get("message") or "Provider failed."
    return ProviderError(
        category=ProviderErrorCategory.UNKNOWN,
        error_type=str(event.payload.get("error_type") or "ProviderError"),
        message=str(sanitize_for_logging(message)),
        retryable=bool(event.payload.get("retryable")),
    )


def _provider_error_is_context_overflow(error: ProviderError | None) -> bool:
    if error is None:
        return False
    if error.category == ProviderErrorCategory.CONTEXT_OVERFLOW:
        return True
    message = f"{error.error_type} {error.message}".lower()
    needles = (
        "context length",
        "context window",
        "context overflow",
        "maximum context",
        "max context",
        "too many tokens",
        "token limit",
        "tokens exceeds",
        "exceeds the model",
    )
    return any(needle in message for needle in needles)


def _build_runtime_compaction(store: SQLiteStore, prompt: QueuedPrompt) -> dict[str, Any]:
    messages = store.list_session_messages(prompt.session_id)
    retained = messages[-8:]
    summary_lines: list[str] = []
    for message in retained:
        preview = _preview(message.content_preview, limit=180)
        if preview:
            summary_lines.append(f"{message.role.value}: {preview}")
    prompt_preview = _preview(prompt.content, limit=180)
    if prompt_preview:
        summary_lines.append(f"user: {prompt_preview}")
    summary = "\n".join(summary_lines) if summary_lines else "No prior message context was available to compact."
    return {
        "schema_version": RUNTIME_COMPACTION_SCHEMA_VERSION,
        "method": "deterministic_recent_message_summary",
        "message_count_before": len(messages),
        "retained_message_ids": [message.id for message in retained],
        "dropped_message_count": max(0, len(messages) - len(retained)),
        "summary": summary,
        "hidden_provider_fallback": False,
        "no_hidden_fallback": True,
        "permission_granting": False,
    }


def _retry_delay_seconds(prompt: QueuedPrompt) -> float:
    raw = prompt.metadata.get("runtime_retry_delay_seconds")
    if raw is None:
        return DEFAULT_RETRY_DELAY_SECONDS
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return DEFAULT_RETRY_DELAY_SECONDS


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _preview(content: str, limit: int = 240) -> str:
    text = " ".join(str(sanitize_for_logging(content)).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."
