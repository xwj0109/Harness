from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from harness.agent_contracts import AGENT_CONTRACT_SCHEMA_VERSION, AgentContract, build_agent_contract
from harness.delegate_budgets import adapter_delegate_budget_projection
from harness.memory.sqlite_store import SQLiteStore
from harness.models import TaskRecord
from harness.paths import resolve_project_root


AGENT_HANDOFF_ENVELOPE_SCHEMA_VERSION = "harness.agent_handoff_envelope/v1"
AGENT_HANDOFF_AUTHORITY_SCHEMA_VERSION = "harness.agent_handoff_authority/v1"
AGENT_HANDOFF_TRACE_CONTEXT_SCHEMA_VERSION = "harness.agent_handoff_trace_context/v1"
AGENT_HANDOFF_INTEGRITY_SCHEMA_VERSION = "harness.agent_handoff_integrity/v1"
SESSION_CHILD_TASK_EXECUTION_ADAPTER_ID = "session_child_task"
SESSION_DELEGATE_TASK_TYPE_ID = "session_delegate"

HandoffSource = Literal["session_tool", "task_record"]


class AgentHandoffAuthority(BaseModel):
    schema_version: str = AGENT_HANDOFF_AUTHORITY_SCHEMA_VERSION
    read_only_projection: bool = True
    task_record_creation_allowed: bool = False
    adapter_execution_allowed: bool = False
    process_start_allowed: bool = False
    network_allowed: bool = False
    tool_execution_allowed: bool = False
    agent_execution_allowed: bool = False
    filesystem_mutation_allowed: bool = False
    model_context_allowed: bool = False
    credential_access_allowed: bool = False
    permission_granting: bool = False
    requires_explicit_permission: bool = True


class AgentHandoffTraceContext(BaseModel):
    schema_version: str = AGENT_HANDOFF_TRACE_CONTEXT_SCHEMA_VERSION
    trace_id: str
    span_id: str
    parent_span_id: str | None = None
    traceparent: str
    correlation_id: str
    causation_id: str | None = None
    source_run_id: str | None = None


class AgentHandoffIntegrity(BaseModel):
    schema_version: str = AGENT_HANDOFF_INTEGRITY_SCHEMA_VERSION
    payload_sha256: str
    envelope_idempotency_key: str
    task_idempotency_key: str | None = None
    contents_included: bool = False
    artifact_bodies_included: bool = False
    credential_values_included: bool = False
    reference_source_included: bool = False


class AgentHandoffEnvelope(BaseModel):
    schema_version: str = AGENT_HANDOFF_ENVELOPE_SCHEMA_VERSION
    ok: bool
    envelope_id: str
    project_root: Path
    source: HandoffSource
    source_schema_version: str | None = None
    task_id: str
    task_title: str
    task_status: str
    task_type: str | None = None
    execution_adapter: str | None = None
    objective_id: str | None = None
    workbench_id: str | None = None
    agent_id: str | None = None
    parent_session_id: str | None = None
    child_session_id: str | None = None
    task_session_id: str | None = None
    source_run_id: str | None = None
    boundary: str | None = None
    allowed_tools: list[str] = Field(default_factory=list)
    output_expectation: str | None = None
    idempotency_key: str | None = None
    required_approvals: list[str] = Field(default_factory=list)
    delegate_budget: dict[str, Any] = Field(default_factory=dict)
    replay_policy: str | None = None
    autonomy_default: str | None = None
    output_contracts: list[str] = Field(default_factory=list)
    agent_contract: AgentContract
    authority: AgentHandoffAuthority = Field(default_factory=AgentHandoffAuthority)
    trace_context: AgentHandoffTraceContext
    integrity: AgentHandoffIntegrity
    safety: dict[str, bool]
    validation_errors: list[str] = Field(default_factory=list)
    next_actions: list[str] = Field(default_factory=list)


def build_agent_handoff_envelope(project_root: Path, task: TaskRecord) -> AgentHandoffEnvelope:
    """Build a passive, typed handoff envelope from a persisted task record."""

    project_root = resolve_project_root(project_root)
    metadata = dict(task.metadata or {})
    execution_adapter = _metadata_str(metadata, "execution_adapter")
    task_type = _metadata_str(metadata, "task_type")
    descriptor = _descriptor_by_id().get(execution_adapter or "")
    parent_session_id = _metadata_str(metadata, "parent_session_id")
    child_session_id = _metadata_str(metadata, "child_session_id")
    source_run_id = _metadata_str(metadata, "source_tool_run_id")
    source: HandoffSource = "session_tool" if metadata.get("schema_version") == "harness.session_tool_task_metadata/v1" else "task_record"
    trace_context = _trace_context(task, source_run_id=source_run_id)
    agent_contract = build_agent_contract(project_root, task.agent_id, workbench_id=task.workbench_id)
    budget_projection = adapter_delegate_budget_projection(descriptor) if descriptor is not None else {}
    delegate_budget = budget_projection.get("budget", {}) if isinstance(budget_projection, dict) else {}
    payload = {
        "project_root": str(project_root),
        "source": source,
        "source_schema_version": metadata.get("schema_version"),
        "task_id": task.id,
        "task_title": task.title,
        "task_status": task.status.value,
        "task_type": task_type,
        "execution_adapter": execution_adapter,
        "objective_id": task.objective_id,
        "workbench_id": task.workbench_id,
        "agent_id": task.agent_id,
        "parent_session_id": parent_session_id,
        "child_session_id": child_session_id,
        "task_session_id": task.session_id,
        "source_run_id": source_run_id,
        "boundary": _metadata_str(metadata, "boundary"),
        "allowed_tools": _metadata_list(metadata, "allowed_tools"),
        "output_expectation": _metadata_str(metadata, "output_expectation"),
        "idempotency_key": task.idempotency_key,
        "required_approvals": list(task.required_approvals),
        "delegate_budget": delegate_budget,
        "replay_policy": descriptor.replay_policy.value if descriptor is not None else None,
        "autonomy_default": descriptor.autonomy_default if descriptor is not None else None,
        "output_contracts": list(descriptor.output_contracts) if descriptor is not None else [],
        "agent_contract": agent_contract.model_dump(mode="json"),
        "trace_context": trace_context.model_dump(mode="json"),
    }
    payload_sha = stable_json_sha256(payload)
    envelope_id = "handoff_" + stable_json_sha256(
        {
            "task_id": task.id,
            "task_idempotency_key": task.idempotency_key,
            "execution_adapter": execution_adapter,
            "task_type": task_type,
            "payload_sha256": payload_sha,
        }
    )[:16]
    validation_errors = validate_agent_handoff_task(task, descriptor=descriptor, agent_contract=agent_contract)
    integrity = AgentHandoffIntegrity(
        payload_sha256=payload_sha,
        envelope_idempotency_key=f"{task.idempotency_key or task.id}:handoff:{payload_sha[:16]}",
        task_idempotency_key=task.idempotency_key,
    )
    return AgentHandoffEnvelope(
        ok=not validation_errors,
        envelope_id=envelope_id,
        project_root=project_root,
        source=source,
        source_schema_version=str(metadata.get("schema_version")) if metadata.get("schema_version") else None,
        task_id=task.id,
        task_title=task.title,
        task_status=task.status.value,
        task_type=task_type,
        execution_adapter=execution_adapter,
        objective_id=task.objective_id,
        workbench_id=task.workbench_id,
        agent_id=task.agent_id,
        parent_session_id=parent_session_id,
        child_session_id=child_session_id,
        task_session_id=task.session_id,
        source_run_id=source_run_id,
        boundary=_metadata_str(metadata, "boundary"),
        allowed_tools=_metadata_list(metadata, "allowed_tools"),
        output_expectation=_metadata_str(metadata, "output_expectation"),
        idempotency_key=task.idempotency_key,
        required_approvals=list(task.required_approvals),
        delegate_budget=delegate_budget,
        replay_policy=payload["replay_policy"],
        autonomy_default=payload["autonomy_default"],
        output_contracts=payload["output_contracts"],
        agent_contract=agent_contract,
        trace_context=trace_context,
        integrity=integrity,
        safety={
            "read_only": True,
            "adapter_execution_started": False,
            "process_started": False,
            "network_called": False,
            "tool_execution_started": False,
            "agent_execution_started": False,
            "filesystem_modified": False,
            "credential_accessed": False,
            "permission_granting": False,
            "model_context_allowed": False,
            "artifact_bodies_read": False,
            "reference_contents_included": False,
        },
        validation_errors=validation_errors,
        next_actions=[] if not validation_errors else ["Recreate the delegated task through the governed session task tool."],
    )


def get_task_handoff_envelope(project_root: Path, task_id: str) -> AgentHandoffEnvelope:
    project_root = resolve_project_root(project_root)
    if not (project_root / ".harness" / "harness.sqlite").exists():
        raise KeyError(f"Task not found: {task_id}")
    return build_agent_handoff_envelope(project_root, SQLiteStore(project_root).get_task(task_id))


def validate_agent_handoff_task(
    task: TaskRecord,
    *,
    descriptor: Any | None = None,
    agent_contract: AgentContract | None = None,
) -> list[str]:
    metadata = dict(task.metadata or {})
    errors: list[str] = []
    if metadata.get("execution_adapter") != SESSION_CHILD_TASK_EXECUTION_ADAPTER_ID:
        errors.append("execution_adapter must be session_child_task")
    if metadata.get("task_type") != SESSION_DELEGATE_TASK_TYPE_ID:
        errors.append("task_type must be session_delegate")
    if descriptor is None:
        errors.append("registered session_child_task descriptor is missing")
    if agent_contract is None:
        errors.append("agent_contract is required")
    elif not agent_contract.ok:
        errors.append("agent_contract must resolve for delegated task")
    elif agent_contract.schema_version != AGENT_CONTRACT_SCHEMA_VERSION:
        errors.append(f"agent_contract schema_version must be {AGENT_CONTRACT_SCHEMA_VERSION}")
    if not task.idempotency_key:
        errors.append("task idempotency_key is required")
    parent_session_id = _metadata_str(metadata, "parent_session_id")
    child_session_id = _metadata_str(metadata, "child_session_id")
    if not parent_session_id:
        errors.append("parent_session_id is required")
    if not child_session_id:
        errors.append("child_session_id is required")
    if child_session_id and task.session_id != child_session_id:
        errors.append("task session_id must match child_session_id")
    if not _metadata_list(metadata, "allowed_tools"):
        errors.append("allowed_tools must be a non-empty list")
    if not _metadata_str(metadata, "boundary"):
        errors.append("boundary is required")
    if not _metadata_str(metadata, "output_expectation"):
        errors.append("output_expectation is required")
    if _metadata_bool(metadata, "execution_started") is not False:
        errors.append("execution_started must be false for a record-only handoff")
    if _metadata_bool(metadata, "hidden_process_started") is not False:
        errors.append("hidden_process_started must be false for a record-only handoff")
    if descriptor is not None:
        budget = descriptor.delegate_budget
        if descriptor.autonomy_default != "forbidden":
            errors.append("session_child_task autonomy_default must be forbidden")
        if descriptor.replay_policy.value != "not_replayable":
            errors.append("session_child_task replay_policy must be not_replayable")
        if budget.max_runtime_invocations != 0:
            errors.append("delegate budget must not allow runtime invocations")
        if budget.max_model_calls != 0:
            errors.append("delegate budget must not allow model calls")
        if budget.max_tool_calls != 0:
            errors.append("delegate budget must not allow tool calls")
        if budget.max_cpu_seconds != 0:
            errors.append("delegate budget must not allow CPU seconds")
        if budget.max_memory_mb != 0:
            errors.append("delegate budget must not allow memory")
        if budget.network_policy != "forbidden":
            errors.append("delegate budget network_policy must be forbidden")
        if budget.active_repo_write != "forbidden":
            errors.append("delegate budget active_repo_write must be forbidden")
    return errors


def handoff_metadata_patch(envelope: AgentHandoffEnvelope) -> dict[str, Any]:
    return {
        "handoff_schema_version": envelope.schema_version,
        "handoff_envelope_id": envelope.envelope_id,
        "handoff_payload_sha256": envelope.integrity.payload_sha256,
        "handoff_trace_id": envelope.trace_context.trace_id,
        "handoff_traceparent": envelope.trace_context.traceparent,
        "handoff_agent_contract_schema_version": envelope.agent_contract.schema_version,
        "handoff_agent_contract_id": envelope.agent_contract.contract_id,
        "handoff_agent_contract_sha256": envelope.agent_contract.contract_sha256,
    }


def _descriptor_by_id() -> dict[str, Any]:
    from harness.execution import list_execution_adapter_descriptors

    return {descriptor.id: descriptor for descriptor in list_execution_adapter_descriptors()}


def stable_json_sha256(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _trace_context(task: TaskRecord, *, source_run_id: str | None) -> AgentHandoffTraceContext:
    seed = {
        "task_id": task.id,
        "idempotency_key": task.idempotency_key,
        "created_at": task.created_at.isoformat(),
        "source_run_id": source_run_id,
    }
    trace_id = stable_json_sha256({"trace": seed})[:32]
    span_id = stable_json_sha256({"span": seed})[:16]
    parent_span_id = stable_json_sha256({"parent": source_run_id})[:16] if source_run_id else None
    return AgentHandoffTraceContext(
        trace_id=trace_id,
        span_id=span_id,
        parent_span_id=parent_span_id,
        traceparent=f"00-{trace_id}-{span_id}-01",
        correlation_id=f"task:{task.id}",
        causation_id=f"run:{source_run_id}" if source_run_id else None,
        source_run_id=source_run_id,
    )


def _metadata_str(metadata: dict[str, Any], key: str) -> str | None:
    value = metadata.get(key)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _metadata_list(metadata: dict[str, Any], key: str) -> list[str]:
    value = metadata.get(key)
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _metadata_bool(metadata: dict[str, Any], key: str) -> bool | None:
    value = metadata.get(key)
    return value if isinstance(value, bool) else None
