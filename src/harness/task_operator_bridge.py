from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from harness.chat_model import ChatContext, ChatMessage, ChatModel, build_default_chat_model
from harness.config import load_config
from harness.memory.sqlite_store import DEFAULT_TASK_LEASE_OWNER, SQLiteStore, now_iso
from harness.models import (
    DaemonExecuteResult,
    EventStreamType,
    RedactionState,
    RunMode,
    SessionPermissionStatus,
    SessionStatus,
    TaskAttempt,
    TaskLease,
    TaskLeaseStatus,
    TaskRecord,
    TaskStatus,
)
from harness.operator_loop import (
    HarnessAgentLoop,
    HarnessAgentLoopResult,
    create_turn_state_from_session,
    model_supports_native_tools,
    persist_turn_started,
)
from harness.operator_models import HarnessTurnState
from harness.policy import effective_policy_sha256, resolve_task_effective_policy
from harness.security import sanitize_for_logging
from harness.session_tools import SessionToolExecutionResult, default_session_tool_descriptors, execute_session_tool


SESSION_OPERATOR_EXECUTION_ADAPTER = "session_read_tools"
SESSION_OPERATOR_TASK_TYPES = {"session_plan", "session_read_only_research", "session_operator"}
SESSION_OPERATOR_DEFAULT_ALLOWED_TOOLS = ["read", "glob", "grep", "artifact-read"]


def execute_operator_task_lease(
    project_root: Path,
    lease_id: str,
    *,
    owner: str = DEFAULT_TASK_LEASE_OWNER,
    model: ChatModel | None = None,
) -> DaemonExecuteResult:
    store = SQLiteStore.open_initialized(project_root)
    lease, attempt, task = store.validate_execution_lease_for_run(lease_id)
    try:
        _validate_operator_task(task)
    except ValueError as exc:
        reason = str(sanitize_for_logging(str(exc)))
        _record_operator_task_rejection(store, lease, attempt, task, reason, owner=owner)
        return _operator_daemon_result(
            store,
            lease=lease,
            attempt=attempt,
            task=task,
            decision="operator_task_blocked_policy",
            ok=False,
            errors=[reason],
        )

    session = _ensure_operator_task_session(store, task)
    run = store.start_attempt_run(
        lease.id,
        task_type=str(task.metadata.get("task_type") or "session_operator"),
        backend=None,
        approval_id=None,
        owner=owner,
    )
    session = store.attach_session_to_run(session.id, run.id)
    session = store.update_session(
        session.id,
        active_task_id=task.id,
        active_run_id=run.id,
        status=SessionStatus.RUNNING,
    )
    prompt = _task_operator_prompt(task)
    policy_sha = effective_policy_sha256(resolve_task_effective_policy(task))
    try:
        chat_model = model or build_default_chat_model(store.project_root)
        if not model_supports_native_tools(chat_model):
            raise ValueError("Task operator execution requires a provider-native tool-capable model.")
        turn_state = _create_task_turn_state(store, task, session)
        persist_turn_started(store, turn_state, prompt=prompt)
        loop = HarnessAgentLoop(
            store=store,
            project_root=store.project_root,
            session_id=session.id,
            model=chat_model,
            chat_context=_task_chat_context(store.project_root, task),
            messages=_task_initial_messages(task),
            turn_state=turn_state,
        )
        loop_result = loop.run(prompt)
    except Exception as exc:
        reason = str(sanitize_for_logging(str(exc)))
        final_artifacts = _persist_operator_task_artifacts(
            store,
            run.id,
            session.id,
            task=task,
            attempt=attempt,
            lease=lease,
            status="failed",
            final_output=reason,
            tool_results=[],
            approval_ids=[],
            turn_id=None,
        )
        store.finish_attempt_run(
            lease.id,
            run_id=run.id,
            owner=owner,
            success=False,
            decision="operator_task_failed",
            run_status="failed",
            failure_code="operator_task_failed",
            failure_message=reason,
        )
        _merge_attempt_metadata(
            store,
            attempt.id,
            {
                "operator_task": True,
                "failure_code": "operator_task_failed",
                "failure_message": reason,
                "artifact_ids": final_artifacts,
            },
        )
        store.update_session(
            session.id,
            active_task_id=task.id,
            active_run_id=run.id,
            status=SessionStatus.FAILED,
        )
        _record_operator_task_event(
            store,
            "harness.task_operator.failed",
            task=task,
            attempt=attempt,
            lease=lease,
            run_id=run.id,
            session_id=session.id,
            payload={"decision": "operator_task_failed", "error": reason, "artifact_ids": final_artifacts},
        )
        store.write_run_manifest(run.id)
        return _operator_daemon_result(
            store,
            lease=store.get_task_lease(lease.id),
            attempt=store.get_task_attempt(attempt.id),
            task=store.get_task(task.id),
            run_id=run.id,
            decision="operator_task_failed",
            ok=False,
            errors=[reason],
        )

    if loop_result.status == "approval_required":
        return _pause_operator_task_for_approval(
            store,
            lease=lease,
            attempt=attempt,
            task=task,
            run_id=run.id,
            session_id=session.id,
            loop_result=loop_result,
            owner=owner,
            policy_sha=policy_sha,
        )

    success = loop_result.ok
    decision = "operator_task_completed" if success else "operator_task_failed"
    final_artifacts = _persist_operator_task_artifacts(
        store,
        run.id,
        session.id,
        task=task,
        attempt=attempt,
        lease=lease,
        status=loop_result.status,
        final_output=loop_result.final_output,
        tool_results=[item.to_payload() for item in loop_result.tool_results],
        approval_ids=_approval_ids(loop_result),
        turn_id=loop_result.turn_state.turn_id if loop_result.turn_state is not None else None,
    )
    missing_artifacts = _missing_required_artifacts(store, run.id, task)
    if missing_artifacts:
        success = False
        decision = "operator_task_missing_expected_artifact"
    store.finish_attempt_run(
        lease.id,
        run_id=run.id,
        owner=owner,
        success=success,
        decision=decision,
        run_status="completed" if success else "failed",
        failure_code=None if success else decision,
        failure_message=None if success else _failure_message(loop_result, missing_artifacts),
    )
    _merge_attempt_metadata(
        store,
        attempt.id,
        {
            "operator_task": True,
            "turn_id": loop_result.turn_state.turn_id if loop_result.turn_state is not None else None,
            "run_id": run.id,
            "artifact_ids": final_artifacts,
            "approval_ids": _approval_ids(loop_result),
            "tool_run_ids": _tool_run_ids(loop_result),
            "status": decision,
            "missing_required_artifacts": missing_artifacts,
        },
    )
    store.update_session(
        session.id,
        active_task_id=task.id,
        active_run_id=run.id,
        status=SessionStatus.IDLE if success else SessionStatus.FAILED,
    )
    _record_operator_task_event(
        store,
        "harness.task_operator.completed" if success else "harness.task_operator.failed",
        task=task,
        attempt=attempt,
        lease=lease,
        run_id=run.id,
        session_id=session.id,
        payload={
            "decision": decision,
            "turn_id": loop_result.turn_state.turn_id if loop_result.turn_state is not None else None,
            "artifact_ids": final_artifacts,
            "approval_ids": _approval_ids(loop_result),
            "tool_run_ids": _tool_run_ids(loop_result),
            "missing_required_artifacts": missing_artifacts,
        },
    )
    store.write_run_manifest(run.id)
    return _operator_daemon_result(
        store,
        lease=store.get_task_lease(lease.id),
        attempt=store.get_task_attempt(attempt.id),
        task=store.get_task(task.id),
        run_id=run.id,
        decision=decision,
        ok=success,
        errors=[] if success else [_failure_message(loop_result, missing_artifacts)],
        adapter_result=_loop_result_payload(loop_result, final_artifacts=final_artifacts),
    )


def resume_operator_task_permission(
    project_root: Path,
    session_id: str,
    permission_id: str,
    *,
    resumed_result: dict[str, Any] | SessionToolExecutionResult | None,
    owner: str = DEFAULT_TASK_LEASE_OWNER,
) -> dict[str, Any] | None:
    store = SQLiteStore.open_initialized(project_root)
    pending = _pending_operator_task_for_permission(store, session_id, permission_id)
    if pending is None:
        return None
    task = store.get_task(str(pending["task_id"]))
    attempt = store.get_task_attempt(str(pending["attempt_id"]))
    lease = store.get_task_lease(str(pending["lease_id"]))
    run_id = str(pending["run_id"])
    result_payload = resumed_result.model_dump(mode="json") if isinstance(resumed_result, SessionToolExecutionResult) else dict(resumed_result or {})
    ok = bool(result_payload.get("ok"))
    final_output = str(result_payload.get("preview") or result_payload.get("error_type") or "Approval resumed.")
    tool_results = [result_payload] if result_payload else []
    final_artifacts = _persist_operator_task_artifacts(
        store,
        run_id,
        session_id,
        task=task,
        attempt=attempt,
        lease=lease,
        status="approval_resumed",
        final_output=final_output,
        tool_results=tool_results,
        approval_ids=[permission_id],
        turn_id=_optional_str(pending.get("turn_id")),
    )
    missing_artifacts = _missing_required_artifacts(store, run_id, task)
    success = ok and not missing_artifacts
    decision = "operator_task_completed" if success else "operator_task_failed"
    store.finish_attempt_run(
        lease.id,
        run_id=run_id,
        owner=owner,
        success=success,
        decision=decision,
        run_status="completed" if success else "failed",
        failure_code=None if success else "operator_task_resume_failed",
        failure_message=None if success else final_output,
    )
    _merge_attempt_metadata(
        store,
        attempt.id,
        {
            "operator_task": True,
            "resumed_permission_id": permission_id,
            "artifact_ids": final_artifacts,
            "approval_ids": [permission_id],
            "tool_run_ids": [result_payload.get("run_id")] if result_payload.get("run_id") else [],
            "status": decision,
            "missing_required_artifacts": missing_artifacts,
        },
    )
    store.update_session(
        session_id,
        active_task_id=task.id,
        active_run_id=run_id,
        status=SessionStatus.IDLE if success else SessionStatus.FAILED,
    )
    _record_operator_task_event(
        store,
        "harness.task_operator.resumed",
        task=task,
        attempt=attempt,
        lease=lease,
        run_id=run_id,
        session_id=session_id,
        payload={
            "decision": decision,
            "permission_id": permission_id,
            "resumed_result": result_payload,
            "artifact_ids": final_artifacts,
            "missing_required_artifacts": missing_artifacts,
        },
    )
    store.write_run_manifest(run_id)
    return {
        "schema_version": "harness.task_operator_resume/v1",
        "ok": success,
        "decision": decision,
        "task_id": task.id,
        "attempt_id": attempt.id,
        "lease_id": lease.id,
        "run_id": run_id,
        "session_id": session_id,
        "permission_id": permission_id,
        "artifact_ids": final_artifacts,
        "missing_required_artifacts": missing_artifacts,
    }


def apply_operator_task_permission_resolution(
    project_root: Path,
    session_id: str,
    permission_id: str,
    *,
    status: SessionPermissionStatus | str,
    resumed_result: dict[str, Any] | SessionToolExecutionResult | None = None,
    feedback: str | None = None,
    owner: str = DEFAULT_TASK_LEASE_OWNER,
) -> dict[str, Any] | None:
    """Update a task-operator attempt for a resolved permission, if one is linked.

    Session-tool permission replies are also used outside task execution. This helper
    deliberately no-ops when the permission is not linked to a paused operator task.
    """
    status_value = SessionPermissionStatus(status.value if isinstance(status, SessionPermissionStatus) else status)
    if status_value == SessionPermissionStatus.DENIED:
        return deny_operator_task_permission(
            project_root,
            session_id,
            permission_id,
            feedback=feedback,
            owner=owner,
        )
    if status_value == SessionPermissionStatus.ALLOWED and resumed_result is not None:
        return resume_operator_task_permission(
            project_root,
            session_id,
            permission_id,
            resumed_result=resumed_result,
            owner=owner,
        )
    return None


def deny_operator_task_permission(
    project_root: Path,
    session_id: str,
    permission_id: str,
    *,
    feedback: str | None = None,
    owner: str = DEFAULT_TASK_LEASE_OWNER,
) -> dict[str, Any] | None:
    store = SQLiteStore.open_initialized(project_root)
    pending = _pending_operator_task_for_permission(store, session_id, permission_id)
    if pending is None:
        return None
    task = store.get_task(str(pending["task_id"]))
    attempt = store.get_task_attempt(str(pending["attempt_id"]))
    lease = store.get_task_lease(str(pending["lease_id"]))
    run_id = str(pending["run_id"])
    message = feedback or "Operator denied the approval required by this task."
    final_artifacts = _persist_operator_task_artifacts(
        store,
        run_id,
        session_id,
        task=task,
        attempt=attempt,
        lease=lease,
        status="approval_denied",
        final_output=message,
        tool_results=[],
        approval_ids=[permission_id],
        turn_id=_optional_str(pending.get("turn_id")),
    )
    store.finish_attempt_run(
        lease.id,
        run_id=run_id,
        owner=owner,
        success=False,
        decision="operator_task_approval_denied",
        run_status="failed",
        failure_code="approval_denied",
        failure_message=message,
    )
    _merge_attempt_metadata(
        store,
        attempt.id,
        {
            "operator_task": True,
            "denied_permission_id": permission_id,
            "artifact_ids": final_artifacts,
            "approval_ids": [permission_id],
            "status": "operator_task_approval_denied",
        },
    )
    store.update_session(
        session_id,
        active_task_id=task.id,
        active_run_id=run_id,
        status=SessionStatus.FAILED,
    )
    _record_operator_task_event(
        store,
        "harness.task_operator.denied",
        task=task,
        attempt=attempt,
        lease=lease,
        run_id=run_id,
        session_id=session_id,
        payload={"decision": "operator_task_approval_denied", "permission_id": permission_id, "feedback": message},
    )
    store.write_run_manifest(run_id)
    return {
        "schema_version": "harness.task_operator_resume/v1",
        "ok": False,
        "decision": "operator_task_approval_denied",
        "task_id": task.id,
        "attempt_id": attempt.id,
        "lease_id": lease.id,
        "run_id": run_id,
        "session_id": session_id,
        "permission_id": permission_id,
        "artifact_ids": final_artifacts,
    }


def _validate_operator_task(task: TaskRecord) -> None:
    if task.metadata.get("execution_adapter") != SESSION_OPERATOR_EXECUTION_ADAPTER:
        raise ValueError(f"Operator task execution requires execution_adapter={SESSION_OPERATOR_EXECUTION_ADAPTER}")
    task_type = str(task.metadata.get("task_type") or "")
    if task_type not in SESSION_OPERATOR_TASK_TYPES:
        raise ValueError(f"Operator task execution does not support task_type={task_type or 'none'}")
    forbidden = [
        key
        for key in (
            "daemon_policy_forbidden",
            "requires_active_repo_write",
            "requires_external_network",
            "requires_docker",
            "requires_paid_provider",
            "requires_hosted_boundary",
        )
        if bool(task.metadata.get(key))
    ]
    if forbidden:
        raise ValueError(f"Operator task execution rejected by task metadata: {', '.join(sorted(forbidden))}.")


def _ensure_operator_task_session(store: SQLiteStore, task: TaskRecord):
    if task.session_id:
        session = store.get_session(task.session_id)
        return store.update_session(
            session.id,
            active_task_id=task.id,
            workbench_id=task.workbench_id,
            agent_id=task.agent_id,
            intent=str(task.metadata.get("task_type") or "session_operator"),
        )
    session = store.create_session(
        title=task.title,
        objective_id=task.objective_id,
        active_task_id=task.id,
        workbench_id=task.workbench_id,
        agent_id=task.agent_id or str(task.metadata.get("agent_alias") or "operator"),
        intent=str(task.metadata.get("task_type") or "session_operator"),
        status=SessionStatus.ACTIVE,
        metadata={"cwd": ".", "created_for_task_id": task.id},
    )
    store.attach_session_to_task(session.id, task.id)
    return store.get_session(session.id)


def _task_operator_prompt(task: TaskRecord) -> str:
    explicit = task.metadata.get("operator_prompt")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    if task.description.strip():
        return f"{task.title}\n\n{task.description}"
    return task.title


def _create_task_turn_state(store: SQLiteStore, task: TaskRecord, session) -> HarnessTurnState:
    cfg = load_config(store.project_root)
    return create_turn_state_from_session(
        project_root=store.project_root,
        session=session,
        model_profile_id=cfg.chat.default_model_profile,
        backend_id=cfg.chat.default_model_profile,
        agent_id=session.agent_id or task.agent_id or "operator",
        workbench_id=session.workbench_id or task.workbench_id,
        active_tools=_active_tools_for_task(task),
        run_mode=_run_mode_for_operator_task(task),
        stream_options={"stream": cfg.chat.stream, "task_id": task.id},
    )


def _active_tools_for_task(task: TaskRecord) -> list[str]:
    available = {descriptor.id for descriptor in default_session_tool_descriptors() if descriptor.enabled}
    raw = task.metadata.get("allowed_tools")
    if isinstance(raw, list) and raw:
        return sorted(str(item) for item in raw if isinstance(item, str) and item in available)
    return sorted(available)


def _run_mode_for_operator_task(task: TaskRecord) -> RunMode:
    value = task.metadata.get("run_mode")
    if isinstance(value, str):
        try:
            return RunMode(value)
        except ValueError:
            pass
    return RunMode.READ_ONLY


def _task_chat_context(project_root: Path, task: TaskRecord) -> ChatContext:
    cfg = load_config(project_root)
    return ChatContext(
        project_root=str(project_root),
        model_profile=cfg.chat.default_model_profile,
        mode="task_operator",
        context_blocks=[
            {
                "kind": "task",
                "role": "request_context",
                "task_id": task.id,
                "task_type": task.metadata.get("task_type"),
                "execution_adapter": task.metadata.get("execution_adapter"),
            }
        ],
        safety_boundaries=[
            "task_queue_execution",
            "session_tool_gateway",
            "exact_approval_required_for_shell_and_tests",
            "active_repo_mutation_forbidden_without_apply_back",
        ],
    )


def _task_initial_messages(task: TaskRecord) -> list[ChatMessage]:
    return [
        ChatMessage(
            role="system",
            content=(
                "You are the Harness operator agent running one leased task. "
                "Use only Harness session tools. Stop when done, blocked, or approval is needed. "
                "Do not claim completion unless tool evidence supports it."
            ),
        ),
        ChatMessage(
            role="system",
            content=(
                "Task context: "
                + json.dumps(
                    sanitize_for_logging(
                        {
                            "task_id": task.id,
                            "title": task.title,
                            "task_type": task.metadata.get("task_type"),
                            "required_artifact_kinds": _required_artifact_kinds(task),
                        }
                    ),
                    sort_keys=True,
                    default=str,
                )
            ),
        ),
    ]


def _pause_operator_task_for_approval(
    store: SQLiteStore,
    *,
    lease: TaskLease,
    attempt: TaskAttempt,
    task: TaskRecord,
    run_id: str,
    session_id: str,
    loop_result: HarnessAgentLoopResult,
    owner: str,
    policy_sha: str,
) -> DaemonExecuteResult:
    permission_id = loop_result.permission_result.permission_id if loop_result.permission_result is not None else None
    pending = sanitize_for_logging(loop_result.pending_tool_call or {})
    store.update_run_status(run_id, "waiting_approval")
    store.update_task_status(task.id, TaskStatus.WAITING_APPROVAL, run_id=run_id)
    _set_attempt_waiting_approval(
        store,
        attempt.id,
        metadata={
            "operator_task": True,
            "turn_id": loop_result.turn_state.turn_id if loop_result.turn_state is not None else None,
            "run_id": run_id,
            "approval_ids": [permission_id] if permission_id else [],
            "pending_tool_call": pending,
            "policy_sha256": policy_sha,
        },
    )
    _merge_lease_metadata(
        store,
        lease.id,
        {
            "operator_task": True,
            "run_id": run_id,
            "waiting_approval_id": permission_id,
            "pending_tool_call": pending,
        },
    )
    store.update_session(
        session_id,
        active_task_id=task.id,
        active_run_id=run_id,
        status=SessionStatus.WAITING_APPROVAL,
    )
    _record_operator_task_event(
        store,
        "harness.task_operator.waiting_approval",
        task=task,
        attempt=attempt,
        lease=lease,
        run_id=run_id,
        session_id=session_id,
        payload={
            "decision": "operator_task_waiting_approval",
            "turn_id": loop_result.turn_state.turn_id if loop_result.turn_state is not None else None,
            "permission_id": permission_id,
            "pending_tool_call": pending,
            "policy_sha256": policy_sha,
        },
    )
    store.write_run_manifest(run_id)
    return _operator_daemon_result(
        store,
        lease=store.get_task_lease(lease.id),
        attempt=store.get_task_attempt(attempt.id),
        task=store.get_task(task.id),
        run_id=run_id,
        decision="operator_task_waiting_approval",
        ok=False,
        approval_id=permission_id,
        errors=[],
        adapter_result=_loop_result_payload(loop_result, final_artifacts=[]),
    )


def _persist_operator_task_artifacts(
    store: SQLiteStore,
    run_id: str,
    session_id: str,
    *,
    task: TaskRecord,
    attempt: TaskAttempt,
    lease: TaskLease,
    status: str,
    final_output: str,
    tool_results: list[dict[str, Any]],
    approval_ids: list[str],
    turn_id: str | None,
) -> list[str]:
    paths = store.initialize_run_artifacts(run_id)
    final_report = paths["final_report"]
    final_report.write_text(
        "\n".join(
            [
                f"# Harness Operator Task {task.id}",
                "",
                f"- Status: {status}",
                f"- Attempt: {attempt.id}",
                f"- Lease: {lease.id}",
                f"- Run: {run_id}",
                f"- Turn: {turn_id or 'none'}",
                f"- Approvals: {', '.join(approval_ids) if approval_ids else 'none'}",
                "",
                "## Final Output",
                "",
                final_output or "(empty)",
                "",
                "## Tool Results",
                "",
                json.dumps(sanitize_for_logging(tool_results), indent=2, sort_keys=True, default=str),
                "",
            ]
        ),
        encoding="utf-8",
    )
    tool_index = store.runs_dir / run_id / "operator_tool_results.json"
    tool_index.write_text(
        json.dumps(
            sanitize_for_logging(
                {
                    "schema_version": "harness.operator_task_tool_results/v1",
                    "task_id": task.id,
                    "attempt_id": attempt.id,
                    "lease_id": lease.id,
                    "run_id": run_id,
                    "session_id": session_id,
                    "turn_id": turn_id,
                    "approval_ids": approval_ids,
                    "tool_results": tool_results,
                }
            ),
            indent=2,
            sort_keys=True,
            default=str,
        )
        + "\n",
        encoding="utf-8",
    )
    artifact_ids: list[str] = []
    for kind, path in (("final_report", final_report), ("operator_tool_result_index", tool_index)):
        existing = [artifact for artifact in store.list_artifacts(run_id) if artifact.kind == kind]
        if existing:
            artifact_ids.extend(artifact.id for artifact in existing)
            continue
        artifact = store.register_artifact(
            run_id,
            kind=kind,
            path=path,
            producer="harness_task_operator",
            redaction_state=RedactionState.REDACTED.value,
            session_id=session_id,
            metadata={
                "task_id": task.id,
                "attempt_id": attempt.id,
                "lease_id": lease.id,
                "turn_id": turn_id,
                "approval_ids": approval_ids,
            },
        )
        artifact_ids.append(artifact.id)
    store.append_event(
        run_id,
        "info",
        "operator_task_artifacts_registered",
        "Operator task artifacts registered.",
        {"artifact_ids": artifact_ids, "tool_result_count": len(tool_results), "approval_ids": approval_ids},
        session_id=session_id,
    )
    return artifact_ids


def _missing_required_artifacts(store: SQLiteStore, run_id: str, task: TaskRecord) -> list[str]:
    required = _required_artifact_kinds(task)
    if not required:
        return []
    produced = {artifact.kind for artifact in store.list_artifacts(run_id)}
    return sorted(kind for kind in required if kind not in produced)


def _required_artifact_kinds(task: TaskRecord) -> list[str]:
    raw = task.metadata.get("required_artifact_kinds")
    if raw is None:
        raw = task.metadata.get("expected_artifacts")
    if raw is None:
        raw = task.metadata.get("required_outputs")
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        return sorted({str(item) for item in raw if isinstance(item, str) and item.strip()})
    return []


def _pending_operator_task_for_permission(store: SQLiteStore, session_id: str, permission_id: str) -> dict[str, Any] | None:
    events = [
        event
        for event in store.list_session_store_events(session_id)
        if event.kind == "harness.task_operator.waiting_approval" and isinstance(event.payload, dict)
    ]
    for event in reversed(events):
        payload = event.payload
        if payload.get("permission_id") == permission_id:
            return {
                "task_id": event.task_id or payload.get("task_id"),
                "attempt_id": payload.get("attempt_id"),
                "lease_id": payload.get("lease_id"),
                "run_id": event.run_id or payload.get("run_id"),
                "turn_id": payload.get("turn_id"),
                "permission_id": permission_id,
            }
    return None


def _record_operator_task_event(
    store: SQLiteStore,
    kind: str,
    *,
    task: TaskRecord,
    attempt: TaskAttempt,
    lease: TaskLease,
    run_id: str,
    session_id: str,
    payload: dict[str, Any],
) -> None:
    base = {
        "task_id": task.id,
        "attempt_id": attempt.id,
        "lease_id": lease.id,
        "run_id": run_id,
        "session_id": session_id,
        "summary": kind,
    }
    base.update(payload)
    store.append_store_event(
        EventStreamType.SESSION,
        session_id,
        kind,
        sanitize_for_logging(base),
        session_id=session_id,
        run_id=run_id,
        task_id=task.id,
        redaction_state=RedactionState.REDACTED,
    )
    store.append_event(
        run_id,
        "info",
        kind,
        kind,
        sanitize_for_logging(base),
        session_id=session_id,
    )


def _record_operator_task_rejection(
    store: SQLiteStore,
    lease: TaskLease,
    attempt: TaskAttempt,
    task: TaskRecord,
    reason: str,
    *,
    owner: str,
) -> None:
    daemon = store.ensure_daemon(owner=owner)
    store.record_daemon_event(
        daemon.id,
        event_type="execution_adapter_rejected",
        message="Session operator task adapter rejected task metadata before run creation.",
        metadata={
            "lease_id": lease.id,
            "task_id": task.id,
            "attempt_id": attempt.id,
            "adapter_id": SESSION_OPERATOR_EXECUTION_ADAPTER,
            "reason_code": "unsafe_metadata",
            "rejection_reasons": [reason],
        },
    )


def _set_attempt_waiting_approval(store: SQLiteStore, attempt_id: str, *, metadata: dict[str, Any]) -> None:
    timestamp = now_iso()
    with store.connect() as conn:
        row = conn.execute("SELECT metadata_json FROM task_attempts WHERE id = ?", (attempt_id,)).fetchone()
        if row is None:
            raise KeyError(f"Task attempt not found: {attempt_id}")
        current = json.loads(row["metadata_json"] or "{}")
        current.update(sanitize_for_logging(metadata))
        conn.execute(
            """
            UPDATE task_attempts
            SET status = ?, metadata_json = ?
            WHERE id = ?
            """,
            (TaskStatus.WAITING_APPROVAL.value, json.dumps(current, sort_keys=True, default=str), attempt_id),
        )


def _merge_attempt_metadata(store: SQLiteStore, attempt_id: str, updates: dict[str, Any]) -> None:
    with store.connect() as conn:
        row = conn.execute("SELECT metadata_json FROM task_attempts WHERE id = ?", (attempt_id,)).fetchone()
        if row is None:
            raise KeyError(f"Task attempt not found: {attempt_id}")
        current = json.loads(row["metadata_json"] or "{}")
        current.update(sanitize_for_logging(updates))
        conn.execute(
            "UPDATE task_attempts SET metadata_json = ? WHERE id = ?",
            (json.dumps(current, sort_keys=True, default=str), attempt_id),
        )


def _merge_lease_metadata(store: SQLiteStore, lease_id: str, updates: dict[str, Any]) -> None:
    with store.connect() as conn:
        row = conn.execute("SELECT metadata_json FROM task_leases WHERE id = ?", (lease_id,)).fetchone()
        if row is None:
            raise KeyError(f"Task lease not found: {lease_id}")
        current = json.loads(row["metadata_json"] or "{}")
        current.update(sanitize_for_logging(updates))
        conn.execute(
            "UPDATE task_leases SET metadata_json = ? WHERE id = ?",
            (json.dumps(current, sort_keys=True, default=str), lease_id),
        )


def _operator_daemon_result(
    store: SQLiteStore,
    *,
    lease: TaskLease,
    attempt: TaskAttempt,
    task: TaskRecord,
    decision: str,
    ok: bool,
    run_id: str | None = None,
    approval_id: str | None = None,
    errors: list[str] | None = None,
    adapter_result: dict[str, Any] | None = None,
) -> DaemonExecuteResult:
    run = store.get_run(run_id) if run_id is not None else None
    manifest = store.build_run_manifest(run_id) if run_id is not None else None
    return DaemonExecuteResult(
        ok=ok,
        decision=decision,
        adapter_id=SESSION_OPERATOR_EXECUTION_ADAPTER,
        project_root=store.project_root,
        task=task,
        attempt=attempt,
        lease=lease,
        run=run,
        manifest=manifest,
        policy_sha256=effective_policy_sha256(resolve_task_effective_policy(task)),
        approval_id=approval_id,
        errors=list(errors or []),
        adapter_result=adapter_result or {},
    )


def _loop_result_payload(loop_result: HarnessAgentLoopResult, *, final_artifacts: list[str]) -> dict[str, Any]:
    return sanitize_for_logging(
        {
            "status": loop_result.status,
            "stop_reason": loop_result.stop_reason,
            "turn_id": loop_result.turn_state.turn_id if loop_result.turn_state is not None else None,
            "tool_results": [item.to_payload() for item in loop_result.tool_results],
            "save_points": [item.model_dump(mode="json") for item in loop_result.save_points],
            "pending_tool_call": loop_result.pending_tool_call,
            "artifact_ids": final_artifacts,
        }
    )


def _tool_run_ids(loop_result: HarnessAgentLoopResult) -> list[str]:
    return sorted({item.run_id for item in loop_result.tool_results if item.run_id})


def _approval_ids(loop_result: HarnessAgentLoopResult) -> list[str]:
    ids = [item.permission_id for item in loop_result.tool_results if item.permission_id]
    if loop_result.permission_result is not None and loop_result.permission_result.permission_id:
        ids.append(loop_result.permission_result.permission_id)
    return sorted(set(ids))


def _failure_message(loop_result: HarnessAgentLoopResult, missing_artifacts: list[str]) -> str:
    if missing_artifacts:
        return f"Missing required task artifacts: {', '.join(missing_artifacts)}"
    return loop_result.final_output or loop_result.stop_reason or "Operator task failed."


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
