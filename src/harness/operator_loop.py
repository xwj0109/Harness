from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from harness.chat_model import ChatContext, ChatMessage, ChatModel, ChatResponse, ChatToolCall, ChatToolSchema
from harness.memory.sqlite_store import SQLiteStore
from harness.models import (
    EventStreamType,
    RedactionState,
    RunMode,
    SessionMessageRole,
    SessionPartKind,
    SessionPermissionStatus,
    SessionSpec,
)
from harness.operator_models import (
    HarnessAgentPhase,
    HarnessOperatorQueueKind,
    HarnessOperatorStatus,
    HarnessSavePoint,
    HarnessTurnState,
)
from harness.policy import stable_json_sha256
from harness.security import sanitize_for_logging
from harness.session_cwd import session_cwd_from_metadata
from harness.session_tools import (
    SessionToolDescriptor,
    SessionToolExecutionResult,
    build_session_approval_card,
    default_session_tool_descriptors,
    execute_session_tool,
    get_session_tool_descriptor,
)


MAX_TOOL_STEPS_PER_TURN = 12
MAX_SAME_TOOL_SAME_ARGS = 3
MAX_PROJECT_SWITCHES_PER_TURN = 1
MAX_SHELL_REQUESTS_PER_TURN = 3
MAX_WALL_CLOCK_SECONDS_PER_TURN = 600
MAX_PERSISTED_TURN_PHASE_AGE_SECONDS = 600


class HarnessOperatorBusyError(RuntimeError):
    pass


@dataclass
class HarnessOperatorRuntime:
    phase: HarnessAgentPhase = HarnessAgentPhase.IDLE
    active_turn_state: HarnessTurnState | None = None
    last_turn_state: HarnessTurnState | None = None
    waiting_approval_id: str | None = None
    steer_queue: list[str] = field(default_factory=list)
    follow_up_queue: list[str] = field(default_factory=list)
    next_turn_queue: list[str] = field(default_factory=list)

    def start_turn(self, turn_state: HarnessTurnState) -> None:
        if self.phase != HarnessAgentPhase.IDLE:
            raise HarnessOperatorBusyError(f"Harness operator is busy: {self.phase.value}")
        self.phase = HarnessAgentPhase.TURN
        self.active_turn_state = turn_state
        self.waiting_approval_id = None

    def resume_waiting_turn(self) -> None:
        if self.phase == HarnessAgentPhase.WAITING_APPROVAL:
            self.phase = HarnessAgentPhase.TURN
            self.waiting_approval_id = None

    def wait_for_approval(self, approval_id: str | None) -> None:
        self.phase = HarnessAgentPhase.WAITING_APPROVAL
        self.waiting_approval_id = approval_id

    def finish(self) -> None:
        if self.active_turn_state is not None:
            self.last_turn_state = self.active_turn_state
        self.active_turn_state = None
        self.phase = HarnessAgentPhase.IDLE
        self.waiting_approval_id = None

    def abort(self) -> None:
        self.finish()

    def wait_for_idle(self) -> bool:
        return self.phase == HarnessAgentPhase.IDLE

    def enqueue(self, queue: HarnessOperatorQueueKind | str, content: str) -> None:
        text = content.strip()
        if not text:
            return
        queue_value = HarnessOperatorQueueKind(queue.value if isinstance(queue, HarnessOperatorQueueKind) else queue)
        if queue_value == HarnessOperatorQueueKind.STEER:
            self.steer_queue.append(text)
        elif queue_value == HarnessOperatorQueueKind.FOLLOW_UP:
            self.follow_up_queue.append(text)
        elif queue_value == HarnessOperatorQueueKind.NEXT_TURN:
            self.next_turn_queue.append(text)

    def drain_save_point_queues(self) -> dict[str, list[str]]:
        drained = {
            HarnessOperatorQueueKind.STEER.value: list(self.steer_queue),
            HarnessOperatorQueueKind.FOLLOW_UP.value: [],
            HarnessOperatorQueueKind.NEXT_TURN.value: [],
        }
        self.steer_queue.clear()
        return drained

    def status(
        self,
        *,
        project_root: Path,
        cwd: str,
        active_tools: list[str],
    ) -> HarnessOperatorStatus:
        turn_state = self.active_turn_state or self.last_turn_state
        return HarnessOperatorStatus(
            phase=self.phase,
            project_root=str(project_root),
            cwd=cwd,
            active_tools=list(active_tools),
            turn_id=turn_state.turn_id if turn_state is not None else None,
            session_id=turn_state.session_id if turn_state is not None else None,
            waiting_approval_id=self.waiting_approval_id,
            current_turn=turn_state,
        )


def create_turn_state_from_session(
    *,
    project_root: Path,
    session: SessionSpec,
    model_profile_id: str,
    backend_id: str,
    agent_id: str,
    workbench_id: str | None,
    active_tools: list[str],
    run_mode: RunMode = RunMode.READ_ONLY,
    context_pack_sha256: str | None = None,
    stream_options: dict[str, Any] | None = None,
) -> HarnessTurnState:
    cwd = session_cwd_from_metadata(session.metadata)
    policy_sha256 = stable_json_sha256(
        {
            "surface": "operator_chat",
            "run_mode": run_mode.value,
            "session_id": session.id,
            "project_root": str(project_root.resolve()),
            "active_tools": sorted(active_tools),
        }
    )
    return HarnessTurnState(
        turn_id=f"turn_{uuid.uuid4().hex[:12]}",
        session_id=session.id,
        project_root=str(project_root.resolve()),
        cwd=cwd,
        model_profile_id=model_profile_id,
        backend_id=backend_id,
        agent_id=agent_id,
        workbench_id=workbench_id,
        run_mode=run_mode,
        active_tools=list(active_tools),
        effective_policy_sha256=policy_sha256,
        context_pack_sha256=context_pack_sha256,
        stream_options=dict(stream_options or {}),
    )


@dataclass(frozen=True)
class HarnessAgentLoopLimits:
    max_tool_steps_per_turn: int = MAX_TOOL_STEPS_PER_TURN
    max_same_tool_same_args: int = MAX_SAME_TOOL_SAME_ARGS
    max_project_switches_per_turn: int = MAX_PROJECT_SWITCHES_PER_TURN
    max_shell_requests_per_turn: int = MAX_SHELL_REQUESTS_PER_TURN
    max_wall_clock_seconds_per_turn: int = MAX_WALL_CLOCK_SECONDS_PER_TURN


@dataclass(frozen=True)
class HarnessAgentLoopToolResult:
    tool_call_id: str
    tool_id: str
    ok: bool
    content: str
    error_type: str | None = None
    run_id: str | None = None
    artifact_id: str | None = None
    permission_id: str | None = None
    executed: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_model_message(self) -> str:
        return json.dumps(
            {
                "type": "harness.tool_result/v1",
                "tool_call_id": self.tool_call_id,
                "tool": self.tool_id,
                "ok": self.ok,
                "content": self.content,
                "artifact_id": self.artifact_id,
                "error_type": self.error_type,
                "run_id": self.run_id,
                "permission_id": self.permission_id,
                "metadata": self.metadata,
            },
            sort_keys=True,
            default=str,
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "tool_call_id": self.tool_call_id,
            "tool": self.tool_id,
            "ok": self.ok,
            "content": self.content,
            "artifact_id": self.artifact_id,
            "error_type": self.error_type,
            "run_id": self.run_id,
            "permission_id": self.permission_id,
            "executed": self.executed,
            "metadata": self.metadata,
        }


@dataclass(frozen=True)
class HarnessAgentLoopResult:
    status: str
    final_output: str
    messages: list[ChatMessage]
    tool_results: list[HarnessAgentLoopToolResult] = field(default_factory=list)
    pending_tool_call: dict[str, Any] | None = None
    permission_result: SessionToolExecutionResult | None = None
    save_points: list[HarnessSavePoint] = field(default_factory=list)
    turn_state: HarnessTurnState | None = None
    stop_reason: str = "final"

    @property
    def ok(self) -> bool:
        return self.status == "final"


class HarnessAgentLoop:
    def __init__(
        self,
        *,
        store: SQLiteStore,
        project_root: Path,
        session_id: str,
        model: ChatModel,
        chat_context: ChatContext,
        messages: list[ChatMessage],
        turn_state: HarnessTurnState,
        limits: HarnessAgentLoopLimits | None = None,
        progress_callback: Any | None = None,
        queue_drain_callback: Any | None = None,
    ) -> None:
        self.store = store
        self.project_root = project_root
        self.session_id = session_id
        self.model = model
        self.chat_context = chat_context
        self.messages = list(messages)
        self.turn_state = turn_state
        self.limits = limits or HarnessAgentLoopLimits()
        self.progress_callback = progress_callback
        self.queue_drain_callback = queue_drain_callback
        self.tool_results: list[HarnessAgentLoopToolResult] = []
        self.save_points: list[HarnessSavePoint] = []
        self._tool_repeat_counts: dict[str, int] = {}
        self._tool_steps = 0
        self._project_switches = 0
        self._shell_requests = 0
        self._start_time = time.monotonic()
        self._last_flushed_event_count = len(self.store.list_session_store_events(self.session_id))
        self._last_flushed_artifact_count = _session_artifact_count(self.store, self.session_id)

    def run(self, user_text: str) -> HarnessAgentLoopResult:
        self._persist_user_message(user_text)
        tools = native_session_tool_schemas(active_tool_ids=self.turn_state.active_tools)
        self._emit("procedure", "Ran provider-native model turn")
        while True:
            if self._wall_clock_exhausted():
                result = self._guard_result("wall_clock_exhausted", "Model tool loop exceeded the wall-clock limit.")
                self._save_point(continue_loop=False)
                return self._result_with_save_point_state(result)
            response = self._complete_with_tools(tools)
            if not response.tool_calls:
                final_output = str(sanitize_for_logging(response.content)).strip()
                if not final_output:
                    final_output = "The local chat model returned an empty response."
                self._persist_assistant_message(final_output)
                self._save_point(continue_loop=False)
                return HarnessAgentLoopResult(
                    status="final",
                    final_output=final_output,
                    messages=list(self.messages),
                    tool_results=list(self.tool_results),
                    save_points=list(self.save_points),
                    turn_state=self.turn_state,
                    stop_reason="final",
                )

            self.messages.append(ChatMessage(role="assistant", content=str(sanitize_for_logging(response.content))))
            self._persist_assistant_tool_calls(response)
            stop_result: HarnessAgentLoopResult | None = None
            for tool_call in response.tool_calls:
                guarded = self._guard_tool_call(tool_call)
                if guarded is not None:
                    self.tool_results.append(guarded)
                    self.messages.append(ChatMessage(role="tool", content=guarded.to_model_message()))
                    stop_result = HarnessAgentLoopResult(
                        status="guard_triggered",
                        final_output=guarded.content,
                        messages=list(self.messages),
                        tool_results=list(self.tool_results),
                        save_points=list(self.save_points),
                        turn_state=self.turn_state,
                        stop_reason=guarded.error_type or "guard_triggered",
                    )
                    break
                result = self._execute_tool_call(tool_call)
                self.tool_results.append(result)
                self.messages.append(ChatMessage(role="tool", content=result.to_model_message()))
                if result.error_type in {
                    "tool_step_limit_exhausted",
                    "shell_request_limit_exhausted",
                    "project_switch_limit_exhausted",
                }:
                    stop_result = HarnessAgentLoopResult(
                        status="guard_triggered",
                        final_output=result.content,
                        messages=list(self.messages),
                        tool_results=list(self.tool_results),
                        save_points=list(self.save_points),
                        turn_state=self.turn_state,
                        stop_reason=result.error_type,
                    )
                    break
                if result.permission_id and result.error_type == "permission_required":
                    stop_result = HarnessAgentLoopResult(
                        status="approval_required",
                        final_output=result.content,
                        messages=list(self.messages),
                        tool_results=list(self.tool_results),
                        pending_tool_call={
                            "project_root": str(self.project_root),
                            "session_id": self.session_id,
                            "tool_id": result.tool_id,
                            "arguments": sanitize_for_logging(tool_call.arguments),
                            "permission_id": result.permission_id,
                        },
                        permission_result=self._permission_execution_result(result),
                        save_points=list(self.save_points),
                        turn_state=self.turn_state,
                        stop_reason="approval_required",
                    )
                    break
                if result.error_type in {"permission_denied", "tool_error", "path_security", "secret_path"}:
                    stop_result = HarnessAgentLoopResult(
                        status="failed",
                        final_output=result.content,
                        messages=list(self.messages),
                        tool_results=list(self.tool_results),
                        save_points=list(self.save_points),
                        turn_state=self.turn_state,
                        stop_reason=result.error_type or "failed",
                    )
                    break

            next_turn_state = self._save_point(continue_loop=stop_result is None)
            if stop_result is not None:
                return self._result_with_save_point_state(stop_result)
            if next_turn_state is not None:
                self.turn_state = next_turn_state
                persist_turn_started(self.store, self.turn_state, prompt="continued after save point")
                self._apply_drained_queue_messages()
                tools = native_session_tool_schemas(active_tool_ids=self.turn_state.active_tools)

    def _result_with_save_point_state(self, result: HarnessAgentLoopResult) -> HarnessAgentLoopResult:
        return HarnessAgentLoopResult(
            status=result.status,
            final_output=result.final_output,
            messages=list(result.messages),
            tool_results=list(result.tool_results),
            pending_tool_call=result.pending_tool_call,
            permission_result=result.permission_result,
            save_points=list(self.save_points),
            turn_state=self.turn_state,
            stop_reason=result.stop_reason,
        )

    def _save_point(self, *, continue_loop: bool) -> HarnessTurnState | None:
        next_turn_state = self._refresh_next_turn_state()
        current_event_count = len(self.store.list_session_store_events(self.session_id))
        current_artifact_count = _session_artifact_count(self.store, self.session_id)
        flushed_event_count = max(0, current_event_count - self._last_flushed_event_count)
        flushed_artifact_count = max(0, current_artifact_count - self._last_flushed_artifact_count)
        save_point = persist_save_point(
            self.store,
            self.turn_state,
            next_turn_state=next_turn_state,
            flushed_event_count=flushed_event_count,
            flushed_artifact_count=flushed_artifact_count,
        )
        self.save_points.append(save_point)
        self._last_flushed_event_count = current_event_count + 1
        self._last_flushed_artifact_count = current_artifact_count
        self._emit("procedure", "Saved operator turn state")
        return next_turn_state if continue_loop else None

    def _refresh_next_turn_state(self) -> HarnessTurnState:
        session = self.store.get_session(self.session_id)
        return create_turn_state_from_session(
            project_root=self.project_root,
            session=session,
            model_profile_id=self.turn_state.model_profile_id,
            backend_id=self.turn_state.backend_id,
            agent_id=session.agent_id or self.turn_state.agent_id,
            workbench_id=session.workbench_id or self.turn_state.workbench_id,
            active_tools=_active_session_tool_ids(),
            run_mode=self.turn_state.run_mode,
            context_pack_sha256=self.turn_state.context_pack_sha256,
            stream_options=dict(self.turn_state.stream_options),
        )

    def _apply_drained_queue_messages(self) -> None:
        if self.queue_drain_callback is None:
            return
        drained = self.queue_drain_callback()
        if not isinstance(drained, dict):
            return
        messages = drained.get(HarnessOperatorQueueKind.STEER.value) or []
        if not isinstance(messages, list):
            return
        for content in messages:
            text = str(sanitize_for_logging(content)).strip()
            if not text:
                continue
            model_content = f"Harness operator steer message:\n{text}"
            self.messages.append(ChatMessage(role="user", content=model_content))
            message = self.store.append_session_message(
                self.session_id,
                SessionMessageRole.USER,
                text,
                agent_id=self.turn_state.agent_id,
            )
            self.store.append_session_part(
                self.session_id,
                message.id,
                SessionPartKind.TEXT,
                text=text,
                metadata={
                    "turn_id": self.turn_state.turn_id,
                    "source": "harness_operator_queue:steer",
                },
                redaction_state=RedactionState.REDACTED,
            )

    def _complete_with_tools(self, tools: list[ChatToolSchema]) -> ChatResponse:
        complete_with_tools = getattr(self.model, "complete_with_tools", None)
        if not callable(complete_with_tools):
            raise TypeError("Chat model does not support provider-native tool calls.")
        return complete_with_tools(self.messages, self.chat_context, tools)

    def _execute_tool_call(self, tool_call: ChatToolCall) -> HarnessAgentLoopToolResult:
        self._tool_steps += 1
        if self._tool_steps > self.limits.max_tool_steps_per_turn:
            return self._persist_model_visible_tool_error(
                tool_call,
                "tool_step_limit_exhausted",
                "Model tool loop exceeded the tool-step limit.",
                event_kind="harness.agent_loop_guard.triggered",
            )
        try:
            descriptor = get_session_tool_descriptor(tool_call.name)
        except KeyError:
            return self._persist_model_visible_tool_error(
                tool_call,
                "unknown_tool",
                f"Unknown session tool: {tool_call.name}",
                event_kind="harness.agent_loop.tool_error",
            )
        if self.turn_state.active_tools and tool_call.name not in set(self.turn_state.active_tools):
            return self._persist_model_visible_tool_error(
                tool_call,
                "active_tool_not_available",
                f"Session tool is not active for this turn: {tool_call.name}",
                event_kind="harness.agent_loop.tool_error",
            )
        validation_error = _validate_native_tool_arguments(descriptor, tool_call.arguments)
        if validation_error is not None:
            return self._persist_model_visible_tool_error(
                tool_call,
                "schema_validation_failed",
                validation_error,
                event_kind="harness.agent_loop.tool_error",
            )
        if tool_call.name == "shell":
            self._shell_requests += 1
            if self._shell_requests > self.limits.max_shell_requests_per_turn:
                return self._persist_model_visible_tool_error(
                    tool_call,
                    "shell_request_limit_exhausted",
                    "Model tool loop exceeded the shell request limit.",
                    event_kind="harness.agent_loop_guard.triggered",
                )
        if tool_call.name in {"project-switch", "workspace-switch"}:
            self._project_switches += 1
            if self._project_switches > self.limits.max_project_switches_per_turn:
                return self._persist_model_visible_tool_error(
                    tool_call,
                    "project_switch_limit_exhausted",
                    "Model tool loop exceeded the project switch limit.",
                    event_kind="harness.agent_loop_guard.triggered",
                )

        try:
            result = execute_session_tool(
                self.store,
                self.project_root,
                self.session_id,
                tool_call.name,
                tool_call.arguments,
                tool_call_id=tool_call.id,
                turn_id=self.turn_state.turn_id,
                run_mode=self.turn_state.run_mode,
            )
        except Exception as exc:
            return self._persist_model_visible_tool_error(
                tool_call,
                "tool_error",
                str(sanitize_for_logging(str(exc))),
                event_kind="harness.agent_loop.tool_error",
            )
        return HarnessAgentLoopToolResult(
            tool_call_id=tool_call.id,
            tool_id=result.tool_id,
            ok=result.ok,
            content=_model_visible_tool_result_content(result),
            error_type=result.error_type,
            run_id=result.run_id,
            artifact_id=result.artifact_id,
            permission_id=result.permission_id,
            executed=result.error_type != "permission_required",
            metadata={
                "schema_version": result.schema_version,
                "truncated": result.truncated,
            },
        )

    def _guard_tool_call(self, tool_call: ChatToolCall) -> HarnessAgentLoopToolResult | None:
        key = _normalized_tool_call_key(tool_call.name, tool_call.arguments)
        count = self._tool_repeat_counts.get(key, 0) + 1
        self._tool_repeat_counts[key] = count
        if count < self.limits.max_same_tool_same_args:
            return None
        return self._persist_model_visible_tool_error(
            tool_call,
            "same_tool_same_args_guard",
            f"Stopped repeated tool call: {tool_call.name} with the same normalized arguments.",
            event_kind="harness.agent_loop_guard.triggered",
            metadata={"repeat_count": count, "max_repeat_count": self.limits.max_same_tool_same_args},
        )

    def _guard_result(self, error_type: str, message: str) -> HarnessAgentLoopResult:
        self.store.append_store_event(
            EventStreamType.SESSION,
            self.session_id,
            "harness.agent_loop_guard.triggered",
            {
                "turn_id": self.turn_state.turn_id,
                "error_type": error_type,
                "summary": message,
            },
            session_id=self.session_id,
            redaction_state=RedactionState.REDACTED,
        )
        return HarnessAgentLoopResult(
            status="guard_triggered",
            final_output=message,
            messages=list(self.messages),
            tool_results=list(self.tool_results),
            stop_reason=error_type,
        )

    def _persist_user_message(self, user_text: str) -> None:
        message = self.store.append_session_message(
            self.session_id,
            SessionMessageRole.USER,
            user_text,
            agent_id=self.turn_state.agent_id,
        )
        self.store.append_session_part(
            self.session_id,
            message.id,
            SessionPartKind.TEXT,
            text=user_text,
            metadata={"turn_id": self.turn_state.turn_id, "source": "harness_agent_loop"},
            redaction_state=RedactionState.REDACTED,
        )

    def _persist_assistant_message(self, content: str) -> None:
        message = self.store.append_session_message(
            self.session_id,
            SessionMessageRole.ASSISTANT,
            content,
            agent_id=self.turn_state.agent_id,
        )
        self.store.append_session_part(
            self.session_id,
            message.id,
            SessionPartKind.TEXT,
            text=content,
            metadata={"turn_id": self.turn_state.turn_id, "source": "harness_agent_loop"},
            redaction_state=RedactionState.REDACTED,
        )

    def _persist_assistant_tool_calls(self, response: ChatResponse) -> None:
        preview = str(sanitize_for_logging(response.content or "Tool call requested."))
        message = self.store.append_session_message(
            self.session_id,
            SessionMessageRole.ASSISTANT,
            preview,
            agent_id=self.turn_state.agent_id,
        )
        if response.content.strip():
            self.store.append_session_part(
                self.session_id,
                message.id,
                SessionPartKind.TEXT,
                text=response.content,
                metadata={"turn_id": self.turn_state.turn_id, "source": "harness_agent_loop"},
                redaction_state=RedactionState.REDACTED,
            )
        for tool_call in response.tool_calls:
            self.store.append_session_part(
                self.session_id,
                message.id,
                SessionPartKind.TOOL_CALL,
                text=json.dumps(
                    {
                        "tool_call_id": tool_call.id,
                        "tool": tool_call.name,
                        "arguments": sanitize_for_logging(tool_call.arguments),
                    },
                    sort_keys=True,
                    default=str,
                ),
                metadata={
                    "turn_id": self.turn_state.turn_id,
                    "tool_call_id": tool_call.id,
                    "tool_id": tool_call.name,
                    "provider_native": True,
                },
                redaction_state=RedactionState.REDACTED,
            )

    def _persist_model_visible_tool_error(
        self,
        tool_call: ChatToolCall,
        error_type: str,
        message: str,
        *,
        event_kind: str,
        metadata: dict[str, Any] | None = None,
    ) -> HarnessAgentLoopToolResult:
        metadata = dict(metadata or {})
        payload = {
            "turn_id": self.turn_state.turn_id,
            "tool_call_id": tool_call.id,
            "tool_id": tool_call.name,
            "error_type": error_type,
            "arguments": sanitize_for_logging(tool_call.arguments),
            "summary": message[:240],
            **metadata,
        }
        self.store.append_store_event(
            EventStreamType.SESSION,
            self.session_id,
            event_kind,
            payload,
            session_id=self.session_id,
            redaction_state=RedactionState.REDACTED,
        )
        tool_message = self.store.append_session_message(
            self.session_id,
            SessionMessageRole.TOOL,
            message,
        )
        self.store.append_session_part(
            self.session_id,
            tool_message.id,
            SessionPartKind.TOOL_RESULT,
            text=message,
            metadata={
                "turn_id": self.turn_state.turn_id,
                "tool_call_id": tool_call.id,
                "tool_id": tool_call.name,
                "ok": False,
                "error_type": error_type,
                **metadata,
            },
            redaction_state=RedactionState.REDACTED,
        )
        return HarnessAgentLoopToolResult(
            tool_call_id=tool_call.id,
            tool_id=tool_call.name,
            ok=False,
            content=message,
            error_type=error_type,
            executed=False,
            metadata=metadata,
        )

    def _permission_execution_result(self, result: HarnessAgentLoopToolResult) -> SessionToolExecutionResult:
        return SessionToolExecutionResult(
            ok=False,
            session_id=self.session_id,
            run_id=result.run_id or "",
            tool_id=result.tool_id,
            preview=result.content,
            artifact_id=result.artifact_id,
            truncated=bool(result.metadata.get("truncated")),
            error_type=result.error_type,
            permission_id=result.permission_id,
        )

    def _wall_clock_exhausted(self) -> bool:
        return time.monotonic() - self._start_time > self.limits.max_wall_clock_seconds_per_turn

    def _emit(self, kind: str, content: str) -> None:
        if self.progress_callback is not None:
            self.progress_callback({"kind": kind, "content": content})


def model_supports_native_tools(model: ChatModel) -> bool:
    return callable(getattr(model, "complete_with_tools", None))


def native_session_tool_schemas(*, active_tool_ids: list[str] | None = None) -> list[ChatToolSchema]:
    active = set(active_tool_ids or [])
    schemas: list[ChatToolSchema] = []
    for descriptor in default_session_tool_descriptors():
        if not descriptor.enabled:
            continue
        if active and descriptor.id not in active:
            continue
        schemas.append(
            ChatToolSchema(
                name=descriptor.id,
                description=descriptor.description,
                input_schema=dict(descriptor.input_schema),
            )
        )
    return schemas


def _active_session_tool_ids() -> list[str]:
    return sorted(descriptor.id for descriptor in default_session_tool_descriptors() if descriptor.enabled)


def _model_visible_tool_result_content(result: SessionToolExecutionResult) -> str:
    status = "success" if result.ok else result.error_type or "failed"
    payload = {
        "status": status,
        "preview": result.preview,
        "artifact_id": result.artifact_id,
        "truncated": result.truncated,
    }
    return json.dumps(payload, sort_keys=True, default=str)


def _normalized_tool_call_key(tool_name: str, arguments: dict[str, Any]) -> str:
    return json.dumps(
        {"tool": tool_name, "arguments": sanitize_for_logging(arguments)},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _validate_native_tool_arguments(descriptor: SessionToolDescriptor, arguments: dict[str, Any]) -> str | None:
    if not isinstance(arguments, dict):
        return f"Tool arguments for {descriptor.id} must be an object."
    schema = descriptor.input_schema or {}
    required = schema.get("required") or []
    if isinstance(required, list):
        for key in required:
            if isinstance(key, str) and key not in arguments:
                return f"Missing required argument for {descriptor.id}: {key}"
    if schema.get("additionalProperties") is False:
        properties = schema.get("properties") or {}
        if isinstance(properties, dict):
            extra = sorted(key for key in arguments if key not in properties)
            if extra:
                return f"Unexpected argument for {descriptor.id}: {extra[0]}"
    properties = schema.get("properties") or {}
    if isinstance(properties, dict):
        for key, value in arguments.items():
            property_schema = properties.get(key)
            if isinstance(property_schema, dict) and not _json_schema_value_matches(value, property_schema):
                return f"Invalid argument type for {descriptor.id}.{key}"
    return None


def _json_schema_value_matches(value: Any, schema: dict[str, Any]) -> bool:
    if "oneOf" in schema and isinstance(schema["oneOf"], list):
        return any(isinstance(candidate, dict) and _json_schema_value_matches(value, candidate) for candidate in schema["oneOf"])
    expected = schema.get("type")
    if isinstance(expected, list):
        return any(_json_type_matches(value, item) for item in expected if isinstance(item, str))
    if isinstance(expected, str):
        return _json_type_matches(value, expected)
    return True


def _json_type_matches(value: Any, expected: str) -> bool:
    if expected == "null":
        return value is None
    if expected == "string":
        return isinstance(value, str)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    return True


def persist_turn_started(
    store: SQLiteStore,
    turn_state: HarnessTurnState,
    *,
    prompt: str,
) -> None:
    store.append_store_event(
        "session",
        turn_state.session_id,
        "operator.turn.started",
        {
            "phase": HarnessAgentPhase.TURN.value,
            "turn_state": turn_state.model_dump(mode="json"),
            "prompt_preview": prompt[:240],
            "summary": "operator turn started",
            "provider_execution_started": False,
            "model_execution_started": False,
            "permission_granting": False,
            "authority_granting": False,
        },
        session_id=turn_state.session_id,
        redaction_state="redacted",
    )


def persist_operator_phase(
    store: SQLiteStore,
    turn_state: HarnessTurnState,
    *,
    phase: HarnessAgentPhase,
    waiting_approval_id: str | None = None,
    reason: str | None = None,
) -> None:
    store.append_store_event(
        EventStreamType.SESSION,
        turn_state.session_id,
        f"operator.turn.{phase.value}",
        {
            "phase": phase.value,
            "turn_state": turn_state.model_dump(mode="json"),
            "waiting_approval_id": waiting_approval_id,
            "reason": reason,
            "summary": f"operator phase {phase.value}",
            "provider_execution_started": False,
            "model_execution_started": False,
            "permission_granting": False,
            "authority_granting": False,
        },
        session_id=turn_state.session_id,
        redaction_state=RedactionState.REDACTED,
    )


def persist_turn_waiting_approval(
    store: SQLiteStore,
    turn_state: HarnessTurnState,
    *,
    waiting_approval_id: str | None,
) -> None:
    persist_operator_phase(
        store,
        turn_state,
        phase=HarnessAgentPhase.WAITING_APPROVAL,
        waiting_approval_id=waiting_approval_id,
        reason="approval_required",
    )


def persist_turn_finished(store: SQLiteStore, turn_state: HarnessTurnState, *, reason: str = "completed") -> None:
    persist_operator_phase(store, turn_state, phase=HarnessAgentPhase.IDLE, reason=reason)


def persist_turn_aborted(store: SQLiteStore, turn_state: HarnessTurnState, *, reason: str = "aborted") -> None:
    persist_operator_phase(store, turn_state, phase=HarnessAgentPhase.IDLE, reason=reason)


def persist_save_point(
    store: SQLiteStore,
    turn_state: HarnessTurnState,
    *,
    next_turn_state: HarnessTurnState | None,
    flushed_event_count: int,
    flushed_artifact_count: int,
) -> HarnessSavePoint:
    next_turn_state_payload = next_turn_state.model_dump(mode="json") if next_turn_state is not None else None
    next_turn_state_sha256 = stable_json_sha256(next_turn_state_payload) if next_turn_state_payload is not None else None
    save_point = HarnessSavePoint(
        save_point_id=f"sp_{uuid.uuid4().hex[:12]}",
        turn_id=turn_state.turn_id,
        session_id=turn_state.session_id,
        flushed_event_count=flushed_event_count,
        flushed_artifact_count=flushed_artifact_count,
        next_turn_state_sha256=next_turn_state_sha256,
    )
    store.append_store_event(
        EventStreamType.SESSION,
        turn_state.session_id,
        "harness.save_point",
        {
            "save_point": save_point.model_dump(mode="json"),
            "turn_state": turn_state.model_dump(mode="json"),
            "next_turn_state": next_turn_state_payload,
            "summary": "operator save point",
            "flushed_event_count": flushed_event_count,
            "flushed_artifact_count": flushed_artifact_count,
        },
        session_id=turn_state.session_id,
        redaction_state=RedactionState.REDACTED,
    )
    return save_point


def session_operator_status_projection(
    store: SQLiteStore,
    session_id: str,
    *,
    project_root: Path,
    cwd: str,
    active_tools: list[str],
) -> dict[str, Any]:
    pending = store.list_session_permissions(session_id, SessionPermissionStatus.PENDING)
    events = store.list_session_store_events(session_id)
    operator_phase_events = [
        event
        for event in events
        if event.kind in {
            "operator.turn.started",
            "operator.turn.idle",
            "operator.turn.waiting_approval",
            "operator.turn.retry",
            "operator.turn.compaction",
            "operator.turn.project_switch",
        }
    ]
    latest_phase_event = (
        max(operator_phase_events, key=lambda event: (event.created_at, event.seq, event.id))
        if operator_phase_events
        else None
    )
    latest_turn = next((event for event in reversed(events) if event.kind == "operator.turn.started"), None)
    save_point_events = [event for event in events if event.kind == "harness.save_point"]
    latest_save_point = (
        max(save_point_events, key=lambda event: (event.created_at, event.seq, event.id)) if save_point_events else None
    )
    turn_state = None
    if latest_turn is not None:
        payload = latest_turn.payload if isinstance(latest_turn.payload, dict) else {}
        candidate = payload.get("turn_state")
        if isinstance(candidate, dict):
            turn_state = candidate
    latest_save_point_payload = None
    if latest_save_point is not None:
        payload = latest_save_point.payload if isinstance(latest_save_point.payload, dict) else {}
        candidate = payload.get("save_point")
        if isinstance(candidate, dict):
            latest_save_point_payload = {
                **candidate,
                "event_id": latest_save_point.id,
                "seq": latest_save_point.seq,
            }
    approval_card = None
    if pending:
        try:
            approval_card = build_session_approval_card(store, session_id, pending[-1].id)
        except Exception:
            approval_card = None
    phase = HarnessAgentPhase.WAITING_APPROVAL.value if pending else HarnessAgentPhase.IDLE.value
    interrupted = False
    if not pending and latest_phase_event is not None:
        payload = latest_phase_event.payload if isinstance(latest_phase_event.payload, dict) else {}
        candidate_phase = str(payload.get("phase") or "")
        if candidate_phase == HarnessAgentPhase.TURN.value:
            age = _event_age_seconds(latest_phase_event.created_at)
            if age <= MAX_PERSISTED_TURN_PHASE_AGE_SECONDS:
                phase = HarnessAgentPhase.TURN.value
            else:
                interrupted = True
        elif candidate_phase in {
            HarnessAgentPhase.RETRY.value,
            HarnessAgentPhase.COMPACTION.value,
            HarnessAgentPhase.PROJECT_SWITCH.value,
        }:
            age = _event_age_seconds(latest_phase_event.created_at)
            if age <= MAX_PERSISTED_TURN_PHASE_AGE_SECONDS:
                phase = candidate_phase
            else:
                interrupted = True
    return {
        "schema_version": "harness.operator_status/v1",
        "phase": phase,
        "project_root": str(project_root),
        "cwd": cwd,
        "active_tools": list(active_tools),
        "turn_id": (turn_state or {}).get("turn_id"),
        "session_id": session_id,
        "waiting_approval_id": pending[-1].id if pending else None,
        "approval_card": approval_card,
        "current_turn": turn_state,
        "latest_save_point": latest_save_point_payload,
        "interrupted": interrupted,
        "live_runtime_known": False,
    }


def _event_age_seconds(created_at: datetime) -> float:
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - created_at.astimezone(timezone.utc)).total_seconds()


def _session_artifact_count(store: SQLiteStore, session_id: str) -> int:
    artifact_ids: set[str] = set()
    for event in store.list_session_store_events(session_id):
        if event.artifact_id:
            artifact_ids.add(event.artifact_id)
        for artifact_ref in event.artifact_refs:
            if isinstance(artifact_ref, str) and artifact_ref:
                artifact_ids.add(artifact_ref)
    return len(artifact_ids)
