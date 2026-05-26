from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from harness.approvals import ApprovalStore
from harness.config import load_config
from harness.context_budget import budget_report, budgeter_for_project
from harness.memory.sqlite_store import SQLiteStore
from harness.model_catalog import validate_model_selection
from harness.model_registry import ModelDescriptor, ProviderDescriptor, ResolvedModelSelection, SessionModelResolution, resolve_model_for_session
from harness.models import EventStreamType, RedactionState, SessionMessageRole, SessionPartKind, SessionStatus
from harness.provider_adapters import ProviderAdapter
from harness.provider_auth import ProviderCredentialResolutionError, ResolvedProviderCredential, resolve_provider_credential
from harness.provider_events import (
    ProviderError,
    ProviderErrorCategory,
    ProviderEvent,
    ProviderEventKind,
    ProviderMessage,
    ProviderRequest,
    provider_error_event,
    provider_event,
    provider_error_retryable_for,
    provider_retry_after_seconds_for,
)
from harness.protocol_adapters import ProtocolAdapter, ProtocolAdapterRegistry, protocol_adapter_missing_error
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
    POLICY_BLOCKED = "policy_blocked"
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
    active_prompt_id: str | None = None
    active_run_id: str | None = None
    active_task_id: str | None = None
    active_started_at: datetime | None = None
    active_elapsed_seconds: int | None = None
    waiting_permission_id: str | None = None
    active_process_ids: list[str] = Field(default_factory=list)
    queued_prompt_count: int = 0
    queued_prompt_ids: list[str] = Field(default_factory=list)
    blocked_reason: str | None = None
    blocked_error_type: str | None = None
    blocked_error_category: str | None = None
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
    policy_blocked: bool = False
    provider_execution_started: bool = False


@dataclass(frozen=True)
class _ResolvedProtocolAdapter:
    adapter: ProtocolAdapter
    provider: ProviderDescriptor
    model: ModelDescriptor
    selection: ResolvedModelSelection
    model_resolution: SessionModelResolution
    credential: ResolvedProviderCredential


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
        self.active_started_at: datetime | None = None
        self.waiting_permission_id: str | None = None
        self.active_process_ids: list[str] = []
        self.queued_prompts: list[QueuedPrompt] = []
        self.active_worker_thread: threading.Thread | None = None
        self.blocked_reason: str | None = None
        self.blocked_error_type: str | None = None
        self.blocked_error_category: str | None = None
        self.updated_at = _utc_now()

    def state(
        self,
        *,
        last_event_seq: int | None,
        terminal: bool = False,
        execution_enabled: bool = True,
    ) -> SessionRuntimeState:
        phase = SessionRuntimePhase.CLOSED if terminal else self.phase
        active_started_at = None if terminal else self.active_started_at
        worker_running = (
            self.active_worker_thread is not None
            and self.active_worker_thread.is_alive()
            and phase in {SessionRuntimePhase.RUNNING, SessionRuntimePhase.ABORTING}
        )
        return SessionRuntimeState(
            session_id=self.session_id,
            phase=phase,
            active_turn_id=None if terminal else self.active_turn_id,
            active_prompt_id=None if terminal else self.active_prompt_id,
            active_run_id=None if terminal else self.active_run_id,
            active_task_id=None if terminal else self.active_task_id,
            active_started_at=active_started_at,
            active_elapsed_seconds=_elapsed_seconds(active_started_at) if active_started_at is not None else None,
            waiting_permission_id=None if terminal else self.waiting_permission_id,
            active_process_ids=[] if terminal else list(self.active_process_ids),
            queued_prompt_count=0 if terminal else len(self.queued_prompts),
            queued_prompt_ids=[] if terminal else [prompt.prompt_id for prompt in self.queued_prompts],
            blocked_reason=None if terminal else self.blocked_reason,
            blocked_error_type=None if terminal else self.blocked_error_type,
            blocked_error_category=None if terminal else self.blocked_error_category,
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
        protocol_adapter_registry: ProtocolAdapterRegistry | None = None,
        execution_enabled: bool = True,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.store = store or SQLiteStore.open_initialized(self.project_root)
        self._project_state = _project_state(self.project_root)
        self.text_provider = text_provider
        self.provider_adapter = provider_adapter
        self.protocol_adapter_registry = protocol_adapter_registry
        self.execution_enabled = execution_enabled

    @classmethod
    def for_store(
        cls,
        store: SQLiteStore,
        *,
        text_provider: SessionRuntimeTextProvider | None = None,
        provider_adapter: ProviderAdapter | None = None,
        protocol_adapter_registry: ProtocolAdapterRegistry | None = None,
        execution_enabled: bool = True,
    ) -> "SessionRuntimeManager":
        return cls(
            store.project_root,
            store,
            text_provider=text_provider,
            provider_adapter=provider_adapter,
            protocol_adapter_registry=protocol_adapter_registry,
            execution_enabled=execution_enabled,
        )

    def status(self, session_id: str) -> SessionRuntimeState:
        session = self.store.get_session(session_id)
        slot = self._project_state.slot(session_id)
        terminal = _is_terminal_status(session.status)
        with slot.condition:
            if not terminal and session.status == SessionStatus.WAITING_APPROVAL:
                slot.phase = SessionRuntimePhase.WAITING_PERMISSION
                _clear_runtime_blocked_state(slot)
                slot.updated_at = _utc_now()
            elif not terminal and session.status == SessionStatus.RUNNING and slot.phase == SessionRuntimePhase.IDLE:
                slot.phase = SessionRuntimePhase.RUNNING
                _clear_runtime_blocked_state(slot)
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
            if slot.phase in {SessionRuntimePhase.IDLE, SessionRuntimePhase.FAILED, SessionRuntimePhase.POLICY_BLOCKED}:
                slot.phase = SessionRuntimePhase.QUEUED
                _clear_runtime_blocked_state(slot)
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
            slot.active_prompt_id = None
            slot.active_run_id = run_id
            slot.active_task_id = task_id
            slot.active_started_at = _utc_now()
            slot.waiting_permission_id = None
            _clear_runtime_blocked_state(slot)
            slot.updated_at = _utc_now()
            slot.condition.notify_all()
            return slot.state(last_event_seq=self._last_event_seq(session_id), execution_enabled=self.execution_enabled)

    def wait_for_permission(self, session_id: str, permission_id: str) -> SessionRuntimeState:
        self.store.get_session(session_id)
        slot = self._project_state.slot(session_id)
        with slot.condition:
            slot.phase = SessionRuntimePhase.WAITING_PERMISSION
            slot.waiting_permission_id = permission_id
            _clear_runtime_blocked_state(slot)
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
            slot.active_started_at = None
            slot.waiting_permission_id = None
            slot.phase = SessionRuntimePhase.FAILED if failed else (
                SessionRuntimePhase.QUEUED if slot.queued_prompts else SessionRuntimePhase.IDLE
            )
            _clear_runtime_blocked_state(slot)
            slot.updated_at = _utc_now()
            slot.condition.notify_all()
            return slot.state(last_event_seq=self._last_event_seq(session_id), execution_enabled=self.execution_enabled)

    def abort(self, session_id: str, *, reason: str | None = None) -> SessionRuntimeState:
        self.store.get_session(session_id)
        slot = self._project_state.slot(session_id)
        with slot.condition:
            slot.phase = SessionRuntimePhase.ABORTING
            _clear_runtime_blocked_state(slot)
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
                slot.active_started_at = None
                slot.waiting_permission_id = None
                slot.phase = SessionRuntimePhase.QUEUED if slot.queued_prompts else SessionRuntimePhase.IDLE
                _clear_runtime_blocked_state(slot)
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
        slot.active_started_at = _utc_now()
        slot.waiting_permission_id = None
        _clear_runtime_blocked_state(slot)
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
        policy_blocked_error: ProviderError | None = None
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
            provider_runtime_available = self.provider_adapter is not None or self.protocol_adapter_registry is not None
            if provider_runtime_available:
                stream_result = self._run_provider_with_recovery(prompt, turn_id)
                if stream_result.failed:
                    failed = True
                    partial_response = _partial_response_evidence(stream_result.response_text)
                    if partial_response is not None:
                        self._record_runtime_partial_response(prompt, turn_id, partial_response)
                    if stream_result.policy_blocked:
                        policy_blocked_error = stream_result.error
                        self._record_runtime_policy_blocked(
                            prompt,
                            turn_id,
                            stream_result.error,
                            provider_execution_started=stream_result.provider_execution_started,
                        )
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
                            "partial_response": partial_response,
                        },
                        session_id=session_id,
                        redaction_state=RedactionState.REDACTED if partial_response is not None else RedactionState.NOT_REQUIRED,
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
            if provider_runtime_available and not response_text:
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
            self._finish_worker_turn(
                session_id,
                turn_id,
                failed=failed,
                suspended=suspended,
                policy_blocked_error=policy_blocked_error,
            )

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
                delay = _retry_delay_seconds(prompt, result.error)
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
        if self.provider_adapter is None and self.protocol_adapter_registry is None:
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
                "abort_checker": lambda session_id=prompt.session_id: self._runtime_aborting(session_id),
            },
            metadata={
                "source": "session_runtime",
                "message_id": prompt.message_id,
                "part_id": prompt.part_id,
                "attempt": attempt,
                "compacted": compaction is not None,
                "requested_model_ref": prompt.model_ref,
            },
        )
        chunks: list[str] = []
        failed = False
        error: ProviderError | None = None
        context_overflow = False
        waiting_permission_id: str | None = None
        provider_execution_started = False
        stream = self._provider_event_stream(prompt, request)
        for event in stream:
            self._append_provider_event(prompt, event)
            if event.kind == ProviderEventKind.MODEL_STARTED:
                provider_execution_started = True
            elif event.kind == ProviderEventKind.MODEL_MESSAGE_DELTA:
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
            elif event.kind == ProviderEventKind.MODEL_ABORTED:
                failed = True
                error = _provider_error_from_event(event)
        return _ProviderStreamResult(
            response_text="".join(chunks).strip(),
            failed=failed,
            waiting_permission_id=waiting_permission_id,
            error=error,
            context_overflow=context_overflow,
            policy_blocked=_provider_error_is_policy_block(error),
            provider_execution_started=provider_execution_started,
        )

    def _provider_event_stream(self, prompt: QueuedPrompt, request: ProviderRequest) -> Iterator[ProviderEvent]:
        if self.provider_adapter is not None:
            yield from self.provider_adapter.stream(request)
            return
        resolved = self._resolve_protocol_adapter(prompt)
        if isinstance(resolved, ProviderError):
            yield provider_event(
                ProviderEventKind.MODEL_FAILED,
                sequence=1,
                request=request,
                payload={
                    "error": resolved.model_dump(mode="json"),
                    "provider_execution_started": False,
                    "model_execution_started": False,
                    "network_accessed": False,
                },
            )
            return
        resolved_request = request.model_copy(
            update={
                "provider_id": request.provider_id or resolved.provider.provider_id,
                "model_ref": request.model_ref or resolved.model.raw_model_ref,
                "metadata": {
                    **request.metadata,
                    "canonical_model_ref": resolved.selection.canonical_model_ref,
                    "alias_used": resolved.selection.alias_used,
                    "resolved_provider_id": resolved.provider.provider_id,
                    "resolved_model_ref": resolved.model.raw_model_ref,
                    "resolved_model_id": resolved.model.model_id,
                    "model_selection_source": resolved.model_resolution.source.value if resolved.model_resolution.source is not None else None,
                    "model_resolution": resolved.model_resolution.model_dump(mode="json"),
                    "protocol": resolved.model.protocol,
                    "model_descriptor_source": resolved.model.source,
                    "resolved_provider_options": resolved.selection.resolved_provider_options,
                    "resolved_model_options": resolved.selection.resolved_model_options,
                    "requested_reasoning_effort": resolved.selection.requested_reasoning_effort,
                    "resolved_reasoning_effort": resolved.selection.resolved_reasoning_effort,
                    "reasoning_resolution": resolved.selection.reasoning_resolution,
                    "provider_credential": resolved.credential.model_dump(mode="json"),
                    "provider_credential_evidence": resolved.credential.redacted_evidence(),
                },
            },
            deep=True,
        )
        yield from resolved.adapter.stream(resolved.provider, resolved.model, resolved_request)

    def _resolve_protocol_adapter(self, prompt: QueuedPrompt) -> _ResolvedProtocolAdapter | ProviderError:
        if self.protocol_adapter_registry is None:
            return ProviderError(
                category=ProviderErrorCategory.CONFIGURATION,
                error_type="ProtocolAdapterRegistryMissing",
                message="No protocol adapter registry is configured for session runtime provider execution.",
                retryable=False,
                hidden_provider_fallback=False,
                no_hidden_fallback=True,
            )
        try:
            cfg = load_config(self.project_root)
            model_resolution = resolve_model_for_session(
                cfg,
                self.store,
                prompt.session_id,
                requested_ref=prompt.model_ref,
            )
            self.store.append_store_event(
                EventStreamType.SESSION,
                prompt.session_id,
                "session.model_resolution",
                {
                    **model_resolution.model_dump(mode="json"),
                    "source": model_resolution.source.value if model_resolution.source is not None else None,
                    "prompt_id": prompt.prompt_id,
                    "message_id": prompt.message_id,
                    "provider_execution_started": False,
                    "model_execution_started": False,
                    "hidden_provider_fallback": False,
                    "hidden_model_fallback": False,
                    "no_hidden_fallback": True,
                },
                session_id=prompt.session_id,
                message_id=prompt.message_id,
                redaction_state=RedactionState.NOT_REQUIRED,
            )
            if model_resolution.resolved_model_selection is None or model_resolution.raw_model_ref is None:
                return ProviderError(
                    category=ProviderErrorCategory.CONFIGURATION,
                    error_type="ModelResolutionFailed",
                    message=f"Model resolution failed: {', '.join(model_resolution.blocked_reasons or ['model_ref_missing'])}",
                    retryable=False,
                    hidden_provider_fallback=False,
                    no_hidden_fallback=True,
                )
            validation = validate_model_selection(
                cfg,
                model_resolution.raw_model_ref,
                provider_accounts=self.store.list_provider_accounts(),
            )
        except Exception as exc:
            return ProviderError(
                category=ProviderErrorCategory.CONFIGURATION,
                error_type=type(exc).__name__,
                message=str(sanitize_for_logging(str(exc))),
                retryable=False,
                hidden_provider_fallback=False,
                no_hidden_fallback=True,
            )
        validation_payload = {
            **validation.model_dump(mode="json"),
            "model_resolution": model_resolution.model_dump(mode="json"),
            "model_selection_source": model_resolution.source.value if model_resolution.source is not None else None,
        }
        runtime_modality_error: ProviderError | None = None
        runtime_tool_error: ProviderError | None = None
        runtime_context_error: ProviderError | None = None
        runtime_token_policy_error: ProviderError | None = None
        runtime_hosted_policy_error: ProviderError | None = None
        runtime_paid_policy_error: ProviderError | None = None
        runtime_data_boundary_policy_error: ProviderError | None = None
        runtime_policy_error: ProviderError | None = None
        if validation.resolved_model_selection is not None:
            runtime_input_modalities = self._requested_input_modalities(prompt)
            supported_input_modalities = {
                str(modality).casefold()
                for modality in validation.resolved_model_selection.model.input_modalities
            }
            unsupported_input_modalities = sorted(
                modality for modality in runtime_input_modalities if modality not in supported_input_modalities
            )
            validation_payload["runtime_input_modalities"] = runtime_input_modalities
            validation_payload["supported_input_modalities"] = sorted(supported_input_modalities)
            validation_payload["unsupported_input_modalities"] = unsupported_input_modalities
            if unsupported_input_modalities:
                blocked_reasons = list(validation_payload.get("blocked_reasons") or [])
                if "input_modality_unsupported" not in blocked_reasons:
                    blocked_reasons.append("input_modality_unsupported")
                validation_payload["blocked_reasons"] = blocked_reasons
                validation_payload["executable"] = False
                validation_payload["runtime_blocked"] = True
                runtime_modality_error = ProviderError(
                    category=ProviderErrorCategory.INVALID_REQUEST,
                    error_type="InputModalityUnsupported",
                    message=(
                        "Input modalities unsupported for "
                        f"{validation.resolved_model_selection.model.raw_model_ref}: "
                        f"{', '.join(unsupported_input_modalities)}. "
                        f"Supported input modalities: {', '.join(sorted(supported_input_modalities)) or 'none'}."
                    ),
                    retryable=False,
                    hidden_provider_fallback=False,
                    no_hidden_fallback=True,
                )
            requested_tools = self._requested_provider_tools(prompt)
            tools_requested = bool(requested_tools) or _metadata_truthy(prompt.metadata.get("requires_tools"))
            validation_payload["runtime_tools_requested"] = tools_requested
            validation_payload["runtime_requested_tools"] = requested_tools
            validation_payload["model_tool_support"] = bool(validation.resolved_model_selection.model.tool_support)
            if tools_requested and not validation.resolved_model_selection.model.tool_support:
                blocked_reasons = list(validation_payload.get("blocked_reasons") or [])
                if "tool_support_unsupported" not in blocked_reasons:
                    blocked_reasons.append("tool_support_unsupported")
                validation_payload["blocked_reasons"] = blocked_reasons
                validation_payload["executable"] = False
                validation_payload["runtime_blocked"] = True
                tool_list = ", ".join(requested_tools) if requested_tools else "provider-native tools"
                runtime_tool_error = ProviderError(
                    category=ProviderErrorCategory.INVALID_REQUEST,
                    error_type="ToolSupportUnsupported",
                    message=(
                        "Provider-native tools were requested for "
                        f"{validation.resolved_model_selection.model.raw_model_ref}, but the selected model "
                        f"does not advertise tool support. Requested tools: {tool_list}."
                    ),
                    retryable=False,
                    hidden_provider_fallback=False,
                    no_hidden_fallback=True,
                )
            context_budget_payload = self._runtime_context_budget_payload(prompt, validation.resolved_model_selection)
            if context_budget_payload is not None:
                validation_payload["runtime_context_budget"] = context_budget_payload
                if context_budget_payload["within_budget"] is False:
                    blocked_reasons = list(validation_payload.get("blocked_reasons") or [])
                    if "context_limit_exceeded" not in blocked_reasons:
                        blocked_reasons.append("context_limit_exceeded")
                    validation_payload["blocked_reasons"] = blocked_reasons
                    validation_payload["executable"] = False
                    validation_payload["runtime_blocked"] = True
                    runtime_context_error = ProviderError(
                        category=ProviderErrorCategory.CONTEXT_OVERFLOW,
                        error_type="ContextBudgetExceeded",
                        message=(
                            "Estimated input context exceeds "
                            f"{validation.resolved_model_selection.model.raw_model_ref} budget: "
                            f"{context_budget_payload['used_input_tokens']} estimated input tokens > "
                            f"{context_budget_payload['max_input_tokens']} allowed input tokens."
                        ),
                        retryable=False,
                        hidden_provider_fallback=False,
                        no_hidden_fallback=True,
                    )
            hosted_policy_payload = (
                self._runtime_hosted_provider_policy_payload(prompt, validation.resolved_model_selection)
                if _model_validation_allows_runtime_policy(validation_payload)
                and runtime_modality_error is None
                and runtime_tool_error is None
                and runtime_context_error is None
                else None
            )
            if hosted_policy_payload is not None:
                validation_payload["runtime_hosted_provider_policy"] = hosted_policy_payload
                if hosted_policy_payload["approved"] is False:
                    blocked_reasons = list(validation_payload.get("blocked_reasons") or [])
                    if "hosted_provider_approval_required" not in blocked_reasons:
                        blocked_reasons.append("hosted_provider_approval_required")
                    validation_payload["blocked_reasons"] = blocked_reasons
                    validation_payload["executable"] = False
                    validation_payload["runtime_blocked"] = True
                    runtime_hosted_policy_error = ProviderError(
                        category=ProviderErrorCategory.PROVIDER_POLICY_BLOCK,
                        error_type="HostedProviderApprovalRequired",
                        message=(
                            "Hosted provider execution requires a valid hosted_provider approval "
                            f"for provider {hosted_policy_payload['provider_id']} and task type "
                            f"{hosted_policy_payload['task_type']}."
                        ),
                        retryable=False,
                        hidden_provider_fallback=False,
                        no_hidden_fallback=True,
                    )
            paid_policy_payload = (
                self._runtime_paid_provider_policy_payload(prompt, validation.resolved_model_selection)
                if _model_validation_allows_runtime_policy(validation_payload)
                and runtime_modality_error is None
                and runtime_tool_error is None
                and runtime_context_error is None
                and runtime_hosted_policy_error is None
                else None
            )
            if paid_policy_payload is not None:
                validation_payload["runtime_paid_provider_policy"] = paid_policy_payload
                if paid_policy_payload["approved"] is False:
                    blocked_reasons = list(validation_payload.get("blocked_reasons") or [])
                    if "paid_provider_approval_required" not in blocked_reasons:
                        blocked_reasons.append("paid_provider_approval_required")
                    validation_payload["blocked_reasons"] = blocked_reasons
                    validation_payload["executable"] = False
                    validation_payload["runtime_blocked"] = True
                    runtime_paid_policy_error = ProviderError(
                        category=ProviderErrorCategory.PROVIDER_POLICY_BLOCK,
                        error_type="PaidProviderApprovalRequired",
                        message=(
                            "Paid API provider execution requires a valid paid_provider approval "
                            f"for provider {paid_policy_payload['provider_id']} and task type "
                            f"{paid_policy_payload['task_type']}."
                        ),
                        retryable=False,
                        hidden_provider_fallback=False,
                        no_hidden_fallback=True,
                    )
            data_boundary_policy_payload = (
                self._runtime_data_boundary_policy_payload(prompt, validation.resolved_model_selection)
                if _model_validation_allows_runtime_policy(validation_payload)
                and runtime_modality_error is None
                and runtime_tool_error is None
                and runtime_context_error is None
                and runtime_hosted_policy_error is None
                and runtime_paid_policy_error is None
                else None
            )
            if data_boundary_policy_payload is not None:
                validation_payload["runtime_data_boundary_policy"] = data_boundary_policy_payload
                if data_boundary_policy_payload["approved"] is False:
                    blocked_reasons = list(validation_payload.get("blocked_reasons") or [])
                    if "data_boundary_approval_required" not in blocked_reasons:
                        blocked_reasons.append("data_boundary_approval_required")
                    validation_payload["blocked_reasons"] = blocked_reasons
                    validation_payload["executable"] = False
                    validation_payload["runtime_blocked"] = True
                    runtime_data_boundary_policy_error = ProviderError(
                        category=ProviderErrorCategory.PROVIDER_POLICY_BLOCK,
                        error_type="DataBoundaryApprovalRequired",
                        message=(
                            "Provider execution across data boundary "
                            f"{data_boundary_policy_payload['data_boundary']} requires a valid approval "
                            f"for provider {data_boundary_policy_payload['provider_id']} and task type "
                            f"{data_boundary_policy_payload['task_type']}."
                        ),
                        retryable=False,
                        hidden_provider_fallback=False,
                        no_hidden_fallback=True,
                    )
            token_policy_payload = self._runtime_token_policy_payload(
                prompt,
                validation.resolved_model_selection,
                context_budget_payload=context_budget_payload,
            )
            if token_policy_payload is not None:
                validation_payload["runtime_token_policy"] = token_policy_payload
                if token_policy_payload["within_budget"] is False:
                    blocked_reasons = list(validation_payload.get("blocked_reasons") or [])
                    if "max_tokens_per_turn_exceeded" not in blocked_reasons:
                        blocked_reasons.append("max_tokens_per_turn_exceeded")
                    validation_payload["blocked_reasons"] = blocked_reasons
                    validation_payload["executable"] = False
                    validation_payload["runtime_blocked"] = True
                    runtime_token_policy_error = ProviderError(
                        category=ProviderErrorCategory.PROVIDER_POLICY_BLOCK,
                        error_type="MaxTokensPerTurnExceeded",
                        message=(
                            "Estimated turn tokens would exceed max_tokens_per_turn policy: "
                            f"{token_policy_payload['estimated_total_tokens']} > "
                            f"{token_policy_payload['max_tokens_per_turn']}."
                        ),
                        retryable=False,
                        hidden_provider_fallback=False,
                        no_hidden_fallback=True,
                    )
            cost_policy_payload = self._runtime_cost_policy_payload(
                prompt,
                validation.resolved_model_selection,
                context_budget_payload=context_budget_payload,
            )
            if cost_policy_payload is not None:
                validation_payload["runtime_cost_policy"] = cost_policy_payload
                if cost_policy_payload["within_budget"] is False:
                    blocked_reasons = list(validation_payload.get("blocked_reasons") or [])
                    if "max_cost_per_run_exceeded" not in blocked_reasons:
                        blocked_reasons.append("max_cost_per_run_exceeded")
                    validation_payload["blocked_reasons"] = blocked_reasons
                    validation_payload["executable"] = False
                    validation_payload["runtime_blocked"] = True
                    runtime_policy_error = ProviderError(
                        category=ProviderErrorCategory.PROVIDER_POLICY_BLOCK,
                        error_type="MaxCostPerRunExceeded",
                        message=(
                            "Estimated run cost would exceed max_cost_usd policy: "
                            f"{cost_policy_payload['projected_total_cost_usd']} > "
                            f"{cost_policy_payload['max_cost_usd']}."
                        ),
                        retryable=False,
                        hidden_provider_fallback=False,
                        no_hidden_fallback=True,
                    )
        credential: ResolvedProviderCredential | None = None
        credential_error: ProviderCredentialResolutionError | None = None
        credential_resolution_unblocked_model = False
        if (
            runtime_modality_error is None
            and runtime_tool_error is None
            and runtime_context_error is None
            and runtime_token_policy_error is None
            and runtime_hosted_policy_error is None
            and runtime_paid_policy_error is None
            and runtime_data_boundary_policy_error is None
            and runtime_policy_error is None
            and validation.resolved_model_selection is not None
            and (
                validation.executable
                or any(reason.startswith("credential_") for reason in validation.blocked_reasons)
            )
        ):
            try:
                credential = resolve_provider_credential(
                    cfg,
                    validation.resolved_model_selection.provider,
                    self.store,
                    allow_secret_material=True,
                )
                validation_payload["provider_credential"] = credential.redacted_evidence()
                if _credential_resolution_unblocks_model(validation_payload, credential):
                    credential_resolution_unblocked_model = True
            except ProviderCredentialResolutionError as exc:
                credential_error = exc
                validation_payload["provider_credential"] = {
                    "schema_version": "harness.resolved_provider_credential/v1",
                    "provider_id": validation.provider_id,
                    "status": "missing",
                    "source": "runtime",
                    "blocked_reasons": exc.blocked_reasons,
                    "credential_value_included": False,
                    "credentials_included": False,
                    "network_accessed": False,
                    "credential_written": False,
                    "no_hidden_fallback": True,
                }
        self.store.append_store_event(
            EventStreamType.SESSION,
            prompt.session_id,
            "session.model_validation",
            {
                **validation_payload,
                "source": "session_runtime",
                "provider_execution_started": False,
                "model_execution_started": False,
                "hidden_provider_fallback": False,
                "hidden_model_fallback": False,
                "no_hidden_fallback": True,
            },
            session_id=prompt.session_id,
            redaction_state=RedactionState.NOT_REQUIRED,
        )
        if runtime_modality_error is not None:
            return runtime_modality_error
        if runtime_tool_error is not None:
            return runtime_tool_error
        if runtime_context_error is not None:
            return runtime_context_error
        if runtime_token_policy_error is not None:
            return runtime_token_policy_error
        if runtime_hosted_policy_error is not None:
            return runtime_hosted_policy_error
        if runtime_paid_policy_error is not None:
            return runtime_paid_policy_error
        if runtime_data_boundary_policy_error is not None:
            return runtime_data_boundary_policy_error
        if runtime_policy_error is not None:
            return runtime_policy_error
        if credential_error is not None:
            return ProviderError(
                category=ProviderErrorCategory.CONFIGURATION,
                error_type=type(credential_error).__name__,
                message=credential_error.to_provider_error_message(),
                retryable=False,
                hidden_provider_fallback=False,
                no_hidden_fallback=True,
            )
        effective_executable = validation.executable or credential_resolution_unblocked_model
        if not effective_executable or validation.resolved_model_selection is None:
            return ProviderError(
                category=ProviderErrorCategory.CONFIGURATION,
                error_type="ModelSelectionValidationFailed",
                message=f"Model selection failed: {', '.join(validation_payload.get('blocked_reasons') or validation.blocked_reasons)}",
                retryable=False,
                hidden_provider_fallback=False,
                no_hidden_fallback=True,
            )
        protocol = validation.resolved_model_selection.model.protocol
        if not self.protocol_adapter_registry.has(protocol):
            return protocol_adapter_missing_error(protocol)
        return _ResolvedProtocolAdapter(
            adapter=self.protocol_adapter_registry.get(protocol),
            provider=validation.resolved_model_selection.provider,
            model=validation.resolved_model_selection.model,
            selection=validation.resolved_model_selection,
            model_resolution=model_resolution,
            credential=credential,
        )

    def _requested_input_modalities(self, prompt: QueuedPrompt) -> list[str]:
        modalities: set[str] = set()
        if prompt.content.strip():
            modalities.add("text")
        modalities.update(_input_modalities_from_metadata(prompt.metadata))
        parts = []
        if prompt.message_id is not None:
            parts = self.store.list_session_parts(prompt.session_id, prompt.message_id)
            if prompt.part_id is not None:
                parts = [part for part in parts if part.id == prompt.part_id]
        elif prompt.part_id is not None:
            parts = [part for part in self.store.list_session_parts(prompt.session_id) if part.id == prompt.part_id]
        for part in parts:
                modalities.update(_input_modalities_from_session_part(part))
        return sorted(modalities or {"text"})

    def _requested_provider_tools(self, prompt: QueuedPrompt) -> list[str]:
        tools = _requested_tools_from_metadata(prompt.metadata)
        parts = []
        if prompt.message_id is not None:
            parts = self.store.list_session_parts(prompt.session_id, prompt.message_id)
            if prompt.part_id is not None:
                parts = [part for part in parts if part.id == prompt.part_id]
        elif prompt.part_id is not None:
            parts = [part for part in self.store.list_session_parts(prompt.session_id) if part.id == prompt.part_id]
        for part in parts:
            metadata = getattr(part, "metadata", None)
            if isinstance(metadata, dict):
                tools.update(_requested_tools_from_metadata(metadata))
        return sorted(tools)

    def _runtime_context_budget_payload(
        self,
        prompt: QueuedPrompt,
        selection: ResolvedModelSelection,
    ) -> dict[str, Any] | None:
        context_limit = selection.model.context_limit
        if context_limit is None:
            return None
        reserved_output_tokens = _requested_output_tokens(selection)
        max_input_tokens = max(0, int(context_limit) - reserved_output_tokens)
        budgeter = budgeter_for_project(self.project_root, model_profile=selection.canonical_model_ref)
        context_text = self._context_budget_text(prompt)
        used_input_tokens = budgeter.count(context_text)
        report = budget_report(
            budgeter,
            model_profile=selection.canonical_model_ref,
            max_input_tokens=max_input_tokens,
            used_input_tokens=used_input_tokens,
        ).to_payload()
        return {
            **report,
            "context_limit": int(context_limit),
            "reserved_output_tokens": reserved_output_tokens,
            "within_budget": used_input_tokens <= max_input_tokens,
            "source": "session_runtime_preflight",
        }

    def _context_budget_text(self, prompt: QueuedPrompt) -> str:
        parts = [prompt.content]
        for part in self._referenced_session_parts(prompt):
            if part.text:
                parts.append(part.text)
        return "\n\n".join(part for part in parts if part)

    def _referenced_session_parts(self, prompt: QueuedPrompt) -> list[Any]:
        if prompt.message_id is not None:
            parts = self.store.list_session_parts(prompt.session_id, prompt.message_id)
            if prompt.part_id is not None:
                parts = [part for part in parts if part.id == prompt.part_id]
            return parts
        if prompt.part_id is not None:
            return [part for part in self.store.list_session_parts(prompt.session_id) if part.id == prompt.part_id]
        return []

    def _runtime_cost_policy_payload(
        self,
        prompt: QueuedPrompt,
        selection: ResolvedModelSelection,
        *,
        context_budget_payload: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        max_cost_usd = _metadata_decimal(prompt.metadata, "max_cost_usd", "max_run_cost_usd")
        if max_cost_usd is None:
            policy = prompt.metadata.get("policy")
            max_cost_usd = _metadata_decimal(policy, "max_cost_usd", "max_run_cost_usd")
        if max_cost_usd is None:
            policy = prompt.metadata.get("runtime_policy")
            max_cost_usd = _metadata_decimal(policy, "max_cost_usd", "max_run_cost_usd")
        if max_cost_usd is None:
            return None
        input_tokens = (
            _safe_positive_int((context_budget_payload or {}).get("used_input_tokens"))
            if context_budget_payload is not None
            else None
        )
        if input_tokens is None:
            budgeter = budgeter_for_project(self.project_root, model_profile=selection.canonical_model_ref)
            input_tokens = budgeter.count(self._context_budget_text(prompt))
        output_tokens = _requested_output_tokens(selection)
        estimated_turn_cost = _estimated_policy_cost_usd(selection, input_tokens=input_tokens, output_tokens=output_tokens)
        current_cost = self.store.get_session(prompt.session_id).estimated_cost_usd or Decimal("0")
        payload: dict[str, Any] = {
            "schema_version": "harness.runtime_cost_policy/v1",
            "source": "session_runtime_preflight",
            "max_cost_usd": str(max_cost_usd),
            "current_cost_usd": str(current_cost),
            "estimated_input_tokens": input_tokens,
            "estimated_output_tokens": output_tokens,
            "estimated_turn_cost_usd": str(estimated_turn_cost) if estimated_turn_cost is not None else None,
            "projected_total_cost_usd": str(current_cost + estimated_turn_cost) if estimated_turn_cost is not None else None,
            "pricing_available": estimated_turn_cost is not None,
            "within_budget": None,
        }
        if estimated_turn_cost is not None:
            payload["within_budget"] = current_cost + estimated_turn_cost <= max_cost_usd
        return payload

    def _runtime_hosted_provider_policy_payload(
        self,
        prompt: QueuedPrompt,
        selection: ResolvedModelSelection,
    ) -> dict[str, Any] | None:
        data_boundary = str(selection.provider.metadata.data_boundary.value)
        if data_boundary != "hosted_provider":
            return None
        task_type = _runtime_policy_task_type(prompt)
        approval = ApprovalStore(self.project_root).find_valid(
            selection.provider.provider_id,
            data_boundary,
            task_type,
            adapter_id=selection.model.protocol,
            workbench_id=_metadata_text(prompt.metadata, "workbench_id"),
            objective_id=_metadata_text(prompt.metadata, "objective_id"),
        )
        return {
            "schema_version": "harness.runtime_hosted_provider_policy/v1",
            "source": "session_runtime_preflight",
            "required_approval": "hosted_provider",
            "provider_id": selection.provider.provider_id,
            "model_ref": selection.model.raw_model_ref,
            "data_boundary": data_boundary,
            "task_type": task_type,
            "approved": approval is not None,
            "approval_id": approval.id if approval is not None else None,
            "approval_expires_at": approval.expires_at.isoformat() if approval is not None else None,
            "provider_execution_started": False,
            "network_accessed": False,
        }

    def _runtime_paid_provider_policy_payload(
        self,
        prompt: QueuedPrompt,
        selection: ResolvedModelSelection,
    ) -> dict[str, Any] | None:
        billing_mode = str(selection.provider.metadata.billing_mode.value)
        if billing_mode != "paid_api":
            return None
        task_type = _runtime_policy_task_type(prompt)
        approval = ApprovalStore(self.project_root).find_valid(
            selection.provider.provider_id,
            "paid_provider",
            task_type,
            adapter_id=selection.model.protocol,
            workbench_id=_metadata_text(prompt.metadata, "workbench_id"),
            objective_id=_metadata_text(prompt.metadata, "objective_id"),
        )
        return {
            "schema_version": "harness.runtime_paid_provider_policy/v1",
            "source": "session_runtime_preflight",
            "required_approval": "paid_provider",
            "provider_id": selection.provider.provider_id,
            "model_ref": selection.model.raw_model_ref,
            "billing_mode": billing_mode,
            "task_type": task_type,
            "approved": approval is not None,
            "approval_id": approval.id if approval is not None else None,
            "approval_expires_at": approval.expires_at.isoformat() if approval is not None else None,
            "provider_execution_started": False,
            "network_accessed": False,
        }

    def _runtime_data_boundary_policy_payload(
        self,
        prompt: QueuedPrompt,
        selection: ResolvedModelSelection,
    ) -> dict[str, Any] | None:
        data_boundary = str(selection.provider.metadata.data_boundary.value)
        if data_boundary in {"local_only", "hosted_provider"}:
            return None
        task_type = _runtime_policy_task_type(prompt)
        approval = ApprovalStore(self.project_root).find_valid(
            selection.provider.provider_id,
            data_boundary,
            task_type,
            adapter_id=selection.model.protocol,
            workbench_id=_metadata_text(prompt.metadata, "workbench_id"),
            objective_id=_metadata_text(prompt.metadata, "objective_id"),
        )
        return {
            "schema_version": "harness.runtime_data_boundary_policy/v1",
            "source": "session_runtime_preflight",
            "required_approval": f"data_boundary:{data_boundary}",
            "provider_id": selection.provider.provider_id,
            "model_ref": selection.model.raw_model_ref,
            "data_boundary": data_boundary,
            "task_type": task_type,
            "approved": approval is not None,
            "approval_id": approval.id if approval is not None else None,
            "approval_expires_at": approval.expires_at.isoformat() if approval is not None else None,
            "provider_execution_started": False,
            "network_accessed": False,
        }

    def _runtime_token_policy_payload(
        self,
        prompt: QueuedPrompt,
        selection: ResolvedModelSelection,
        *,
        context_budget_payload: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        max_tokens = _metadata_positive_int(prompt.metadata, "max_tokens_per_turn", "max_turn_tokens")
        if max_tokens is None:
            policy = prompt.metadata.get("policy")
            max_tokens = _metadata_positive_int(policy, "max_tokens_per_turn", "max_turn_tokens")
        if max_tokens is None:
            policy = prompt.metadata.get("runtime_policy")
            max_tokens = _metadata_positive_int(policy, "max_tokens_per_turn", "max_turn_tokens")
        if max_tokens is None:
            return None
        input_tokens = (
            _safe_positive_int((context_budget_payload or {}).get("used_input_tokens"))
            if context_budget_payload is not None
            else None
        )
        if input_tokens is None:
            budgeter = budgeter_for_project(self.project_root, model_profile=selection.canonical_model_ref)
            input_tokens = budgeter.count(self._context_budget_text(prompt))
        output_tokens = _requested_output_tokens(selection)
        estimated_total_tokens = input_tokens + output_tokens
        return {
            "schema_version": "harness.runtime_token_policy/v1",
            "source": "session_runtime_preflight",
            "max_tokens_per_turn": max_tokens,
            "estimated_input_tokens": input_tokens,
            "estimated_output_tokens": output_tokens,
            "estimated_total_tokens": estimated_total_tokens,
            "within_budget": estimated_total_tokens <= max_tokens,
        }

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
                "retry_after_seconds": error.retry_after_seconds if error is not None else None,
                "category": error.category.value if error is not None else None,
                "error_type": error.error_type if error is not None else None,
                "retryable": error.retryable if error is not None else None,
                "hidden_provider_fallback": False,
                "no_hidden_fallback": True,
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
        payload = event.store_payload()
        self.store.append_store_event(
            EventStreamType.SESSION,
            prompt.session_id,
            event.store_kind(),
            payload,
            session_id=prompt.session_id,
            message_id=prompt.message_id,
            redaction_state=_provider_event_redaction(event),
        )
        if event.kind == ProviderEventKind.TOKEN_USAGE_UPDATED:
            self._update_session_token_rollup(prompt.session_id, payload)

    def _update_session_token_rollup(self, session_id: str, payload: dict[str, Any]) -> None:
        normalized = payload.get("normalized_usage")
        usage = normalized if isinstance(normalized, dict) else payload
        input_tokens = _usage_int(usage, "input_tokens")
        output_tokens = _usage_int(usage, "output_tokens")
        reasoning_tokens = _usage_int(usage, "reasoning_tokens")
        cache_read_tokens = _usage_int(usage, "cache_read_tokens", "cached_input_tokens")
        cache_write_tokens = _usage_int(usage, "cache_write_tokens")
        estimated_cost_usd = _usage_estimated_cost_usd(payload)
        if all(
            value is None
            for value in (
                input_tokens,
                output_tokens,
                reasoning_tokens,
                cache_read_tokens,
                cache_write_tokens,
                estimated_cost_usd,
            )
        ):
            return
        current = self.store.get_session(session_id)
        self.store.update_session_summary(
            session_id,
            token_input=current.token_input + input_tokens if input_tokens is not None else None,
            token_output=current.token_output + output_tokens if output_tokens is not None else None,
            token_reasoning=current.token_reasoning + reasoning_tokens if reasoning_tokens is not None else None,
            token_cache_read=current.token_cache_read + cache_read_tokens if cache_read_tokens is not None else None,
            token_cache_write=current.token_cache_write + cache_write_tokens if cache_write_tokens is not None else None,
            estimated_cost_usd=(
                (current.estimated_cost_usd or Decimal("0")) + estimated_cost_usd
                if estimated_cost_usd is not None
                else None
            ),
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

    def _record_runtime_partial_response(
        self,
        prompt: QueuedPrompt,
        turn_id: str,
        partial_response: dict[str, Any],
    ) -> None:
        self.store.append_store_event(
            EventStreamType.SESSION,
            prompt.session_id,
            "harness.runtime.partial_response",
            {
                **partial_response,
                "turn_id": turn_id,
                "prompt_id": prompt.prompt_id,
                "failed": True,
                "assistant_message_persisted": False,
                "summary": "partial provider response preserved after stream failure",
            },
            session_id=prompt.session_id,
            message_id=prompt.message_id,
            redaction_state=RedactionState.REDACTED,
        )

    def _record_runtime_policy_blocked(
        self,
        prompt: QueuedPrompt,
        turn_id: str,
        error: ProviderError | None,
        *,
        provider_execution_started: bool,
    ) -> None:
        self.store.append_store_event(
            EventStreamType.SESSION,
            prompt.session_id,
            "harness.runtime.policy_blocked",
            {
                "turn_id": turn_id,
                "prompt_id": prompt.prompt_id,
                "provider_error": error.model_dump(mode="json") if error is not None else None,
                "blocked_error_type": error.error_type if error is not None else None,
                "blocked_error_category": error.category.value if error is not None else None,
                "blocked_reason": sanitize_for_logging(error.message) if error is not None else "Provider policy blocked execution.",
                "provider_execution_started": provider_execution_started,
                "network_accessed": provider_execution_started,
                "summary": "runtime blocked by provider policy",
            },
            session_id=prompt.session_id,
            message_id=prompt.message_id,
            redaction_state=RedactionState.REDACTED if error is not None else RedactionState.NOT_REQUIRED,
        )

    def _finish_worker_turn(
        self,
        session_id: str,
        turn_id: str,
        *,
        failed: bool,
        suspended: bool = False,
        policy_blocked_error: ProviderError | None = None,
    ) -> None:
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
                slot.active_started_at = None
                slot.waiting_permission_id = None
                slot.active_worker_thread = None
                if slot.queued_prompts:
                    slot.phase = SessionRuntimePhase.QUEUED
                    _clear_runtime_blocked_state(slot)
                    thread_to_start = self._start_next_prompt_locked(slot)
                elif policy_blocked_error is not None:
                    slot.phase = SessionRuntimePhase.POLICY_BLOCKED
                    _set_runtime_blocked_state(slot, policy_blocked_error)
                else:
                    slot.phase = SessionRuntimePhase.FAILED if failed else SessionRuntimePhase.IDLE
                    if not failed:
                        _clear_runtime_blocked_state(slot)
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
                slot.active_started_at = None
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
    return slot.phase in {
        SessionRuntimePhase.IDLE,
        SessionRuntimePhase.FAILED,
        SessionRuntimePhase.POLICY_BLOCKED,
        SessionRuntimePhase.CLOSED,
    } and not slot.queued_prompts


def _set_runtime_blocked_state(slot: _RuntimeSessionSlot, error: ProviderError) -> None:
    slot.blocked_reason = str(sanitize_for_logging(error.message))
    slot.blocked_error_type = error.error_type
    slot.blocked_error_category = error.category.value


def _clear_runtime_blocked_state(slot: _RuntimeSessionSlot) -> None:
    slot.blocked_reason = None
    slot.blocked_error_type = None
    slot.blocked_error_category = None


def _elapsed_seconds(started_at: datetime | None) -> int | None:
    if started_at is None:
        return None
    started = started_at if started_at.tzinfo is not None else started_at.replace(tzinfo=timezone.utc)
    return max(0, int((_utc_now() - started).total_seconds()))


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


def _usage_int(payload: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = payload.get(key)
        if value is None or isinstance(value, bool):
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _usage_estimated_cost_usd(payload: dict[str, Any]) -> Decimal | None:
    direct = _usage_decimal(payload.get("estimated_cost_usd"))
    if direct is not None:
        return direct
    estimated = payload.get("estimated_cost")
    if isinstance(estimated, dict) and estimated.get("currency") == "USD":
        return _usage_decimal(estimated.get("total"))
    return None


def _usage_decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _input_modalities_from_session_part(part: Any) -> set[str]:
    modalities: set[str] = set()
    kind = str(getattr(part, "kind", "") or "")
    if "." in kind:
        kind = kind.rsplit(".", 1)[-1]
    if kind in {
        SessionPartKind.TEXT.value,
        SessionPartKind.QUESTION.value,
        SessionPartKind.TODO_UPDATE.value,
        SessionPartKind.SUMMARY.value,
    } and getattr(part, "text", None):
        modalities.add("text")
    metadata = getattr(part, "metadata", None)
    if isinstance(metadata, dict):
        modalities.update(_input_modalities_from_metadata(metadata))
    return modalities


def _input_modalities_from_metadata(metadata: Any) -> set[str]:
    modalities: set[str] = set()
    if not isinstance(metadata, dict):
        return modalities
    for key in ("input_modalities", "requested_input_modalities", "modalities"):
        modalities.update(_normalized_modality_values(metadata.get(key)))
    for key in ("modality", "input_modality"):
        modalities.update(_normalized_modality_values(metadata.get(key)))
    kind = str(metadata.get("kind") or metadata.get("content_kind") or "").casefold()
    if kind == "image_input":
        modalities.add("image")
    elif kind in {"text", "input_text"}:
        modalities.add("text")
    for key in ("media_type", "mime_type", "content_type"):
        modality = _modality_from_mime_type(metadata.get(key))
        if modality is not None:
            modalities.add(modality)
    for key in ("attachments", "parts", "canonical_messages"):
        value = metadata.get(key)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    modalities.update(_input_modalities_from_metadata(item))
    return modalities


def _normalized_modality_values(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, str):
        candidates = [value]
    elif isinstance(value, list | tuple | set):
        candidates = [str(item) for item in value if item is not None]
    else:
        candidates = [str(value)]
    return {candidate.strip().casefold() for candidate in candidates if candidate.strip()}


def _modality_from_mime_type(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    mime_type = value.strip().casefold()
    if mime_type.startswith("image/"):
        return "image"
    if mime_type.startswith("audio/"):
        return "audio"
    if mime_type.startswith("video/"):
        return "video"
    if mime_type in {"application/pdf", "text/csv"} or mime_type.startswith("text/"):
        return "text"
    return None


def _requested_tools_from_metadata(metadata: Any) -> set[str]:
    tools: set[str] = set()
    if not isinstance(metadata, dict):
        return tools
    for key in ("active_tools", "allowed_tools", "requested_tools", "provider_tools", "native_tools"):
        tools.update(_tool_names(metadata.get(key)))
    for key in ("tools", "tool_schemas"):
        tools.update(_tool_names(metadata.get(key)))
    tool_choice = metadata.get("tool_choice")
    if isinstance(tool_choice, str) and tool_choice.strip().casefold() not in {"", "none", "auto"}:
        tools.add(tool_choice.strip())
    elif isinstance(tool_choice, dict):
        tools.update(_tool_names(tool_choice))
        if not tools and tool_choice:
            tools.add("tool_choice")
    for key in ("attachments", "parts", "canonical_messages"):
        value = metadata.get(key)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    tools.update(_requested_tools_from_metadata(item))
    return tools


def _tool_names(value: Any) -> set[str]:
    names: set[str] = set()
    if value is None:
        return names
    if isinstance(value, str):
        text = value.strip()
        return {text} if text else set()
    if isinstance(value, dict):
        for key in ("name", "id", "tool", "tool_name"):
            name = value.get(key)
            if isinstance(name, str) and name.strip():
                names.add(name.strip())
        if names:
            return names
        for key in value:
            if isinstance(key, str) and key.strip():
                names.add(key.strip())
        return names
    if isinstance(value, list | tuple | set):
        for item in value:
            names.update(_tool_names(item))
    return names


def _metadata_truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "on"}
    return bool(value)


def _requested_output_tokens(selection: ResolvedModelSelection) -> int:
    for key in ("max_output_tokens", "max_completion_tokens", "max_tokens"):
        value = _safe_positive_int(selection.resolved_model_options.get(key))
        if value is not None:
            return value
    return _safe_positive_int(selection.model.max_output_tokens) or 0


def _estimated_policy_cost_usd(
    selection: ResolvedModelSelection,
    *,
    input_tokens: int,
    output_tokens: int,
) -> Decimal | None:
    cost = selection.model.cost
    if not isinstance(cost, dict):
        return None
    input_rate = _metadata_decimal(cost, "input_per_1m", "input_per_million")
    output_rate = _metadata_decimal(cost, "output_per_1m", "output_per_million")
    if input_rate is None and output_rate is None:
        return None
    total = Decimal("0")
    if input_rate is not None:
        total += Decimal(input_tokens) * input_rate / Decimal(1_000_000)
    if output_rate is not None:
        total += Decimal(output_tokens) * output_rate / Decimal(1_000_000)
    return total


def _metadata_decimal(metadata: Any, *keys: str) -> Decimal | None:
    if not isinstance(metadata, dict):
        return None
    for key in keys:
        value = metadata.get(key)
        if value is None or isinstance(value, bool):
            continue
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, ValueError):
            continue
        if parsed >= 0:
            return parsed
    return None


def _metadata_positive_int(metadata: Any, *keys: str) -> int | None:
    if not isinstance(metadata, dict):
        return None
    for key in keys:
        value = _safe_positive_int(metadata.get(key))
        if value is not None:
            return value
    return None


def _metadata_text(metadata: Any, key: str) -> str | None:
    if not isinstance(metadata, dict):
        return None
    value = metadata.get(key)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _runtime_policy_task_type(prompt: QueuedPrompt) -> str:
    for key in ("task_type", "run_task_type", "approval_task_type"):
        value = _metadata_text(prompt.metadata, key)
        if value is not None:
            return value
    return "session_provider_execution"


def _model_validation_allows_runtime_policy(validation_payload: dict[str, Any]) -> bool:
    if validation_payload.get("executable") is True:
        return True
    blocked = validation_payload.get("blocked_reasons")
    if not isinstance(blocked, list) or not blocked:
        return False
    return all(str(reason).startswith("credential_") for reason in blocked)


def _credential_resolution_unblocks_model(
    validation_payload: dict[str, Any],
    credential: ResolvedProviderCredential,
) -> bool:
    blocked = validation_payload.get("blocked_reasons")
    if not isinstance(blocked, list) or not blocked:
        return False
    if not all(str(reason).startswith("credential_") for reason in blocked):
        return False
    if credential.status not in {"configured", "not_required"}:
        return False
    validation_payload["blocked_reasons"] = []
    validation_payload["executable"] = True
    validation_payload["runtime_credential_resolution"] = {
        "schema_version": "harness.runtime_credential_resolution/v1",
        "source": credential.source,
        "provider_id": credential.provider_id,
        "credential_kind": credential.credential_kind,
        "status": credential.status,
        "unblocked_model_selection": True,
        "previous_blocked_reasons": [str(reason) for reason in blocked],
        "credential_value_included": False,
        "credentials_included": False,
        "network_accessed": False,
        "provider_execution_started": False,
        "model_execution_started": False,
        "no_hidden_fallback": True,
    }
    return True


def _safe_positive_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


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
        if "retryable" not in raw and raw.get("category") is not None:
            raw = {
                **raw,
                "retryable": provider_error_retryable_for(
                    str(raw.get("category")),
                    raw.get("error_type"),
                    raw.get("message"),
                ),
            }
        if "retry_after_seconds" not in raw:
            retry_after_seconds = provider_retry_after_seconds_for(raw)
            if retry_after_seconds is not None:
                raw = {**raw, "retry_after_seconds": retry_after_seconds}
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


def _provider_error_is_policy_block(error: ProviderError | None) -> bool:
    return error is not None and error.category == ProviderErrorCategory.PROVIDER_POLICY_BLOCK


def _partial_response_evidence(response_text: str) -> dict[str, Any] | None:
    text = str(response_text or "")
    if not text.strip():
        return None
    sanitized = str(sanitize_for_logging(text))
    return {
        "schema_version": "harness.runtime_partial_response/v1",
        "present": True,
        "text_preview": _preview(sanitized, limit=500),
        "char_count": len(sanitized),
        "line_count": len(sanitized.splitlines()) or 1,
        "redacted": True,
        "failed": True,
        "assistant_message_persisted": False,
        "provider_execution_started": True,
        "model_execution_started": True,
        "no_hidden_fallback": True,
    }


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


def _retry_delay_seconds(prompt: QueuedPrompt, error: ProviderError | None = None) -> float:
    if (
        error is not None
        and error.category == ProviderErrorCategory.RATE_LIMIT
        and error.retry_after_seconds is not None
    ):
        return max(0.0, float(error.retry_after_seconds))
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
