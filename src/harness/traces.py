from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path
from typing import Any

from harness.approvals import ApprovalStore
from harness.config import HARNESS_DIR
from harness.execution import evaluate_registered_adapter_security_decision
from harness.integrity import stable_json_sha256, trace_export_provenance
from harness.memory.sqlite_store import SQLiteStore
from harness.models import TRACE_CONTEXT_PROPAGATION, TRACE_SEMANTIC_CONVENTIONS, TraceExport, TraceSpan
from harness.objective_evidence import read_objective_evidence_events, verify_objective_evidence
from harness.security import sanitize_for_logging


def export_run_trace(project_root: Path, store: SQLiteStore, run_id: str) -> TraceExport:
    run = store.get_run(run_id)
    manifest = store.build_run_manifest(run_id)
    task = store.get_task(run.task_id) if run.task_id else None
    attempt, lease = _run_attempt_and_lease(store, run)
    security_decision = _security_decision_for_run(project_root, store, run)
    trace_id = manifest.trace_id or _trace_id(run_id)
    run_span_id = _span_id("run", run_id)
    spans = [
        TraceSpan(
            trace_id=trace_id,
            span_id=run_span_id,
            name="harness.run",
            start_time=run.created_at,
            end_time=run.updated_at,
            attributes=sanitize_for_logging(
                {
                    "run.id": run.id,
                    "run.status": run.status,
                    "run.final_outcome": run.status,
                    "run.task_type": run.task_type,
                    "run.mode": manifest.run_mode.value,
                    "project.root": str(run.project_root),
                    "security.decision_id": security_decision.id if security_decision else None,
                    "policy.sha256": manifest.effective_policy_sha256,
                    "adapter.id": task.metadata.get("execution_adapter") if task else None,
                    "task.type": run.task_type,
                    "attempt.id": attempt.id if attempt is not None else None,
                    "lease.id": lease.id if lease is not None else None,
                    "backend.name": run.backend_name,
                    "backend.kind": run.backend_kind.value if run.backend_kind else None,
                    "backend.sha256": manifest.backend_descriptor_sha256,
                    "approval.id": run.approval_id,
                    "task.id": manifest.task_id,
                    "objective.id": manifest.objective_id,
                    "session.id": run.session_id,
                    "context.warning_codes": manifest.untrusted_context_warnings,
                    "context.provenance_ids": [record.id for record in manifest.context_provenance],
                    **_delegate_budget_summary_attributes(manifest.delegate_budget),
                }
            ),
        ),
        TraceSpan(
            trace_id=trace_id,
            span_id=_span_id("policy", run_id),
            parent_span_id=run_span_id,
            name="harness.policy",
            start_time=run.created_at,
            end_time=run.updated_at,
            attributes=sanitize_for_logging(
                {
                    "policy.schema_version": manifest.effective_policy.schema_version
                    if manifest.effective_policy
                    else None,
                    "policy.sha256": manifest.effective_policy_sha256,
                    "policy.required_approvals": manifest.effective_policy.required_approvals
                    if manifest.effective_policy
                    else [],
                    "policy.forbidden_reasons": manifest.effective_policy.forbidden_reasons
                    if manifest.effective_policy
                    else [],
                }
            ),
        ),
    ]
    if manifest.backend_descriptor is not None:
        spans.append(
            TraceSpan(
                trace_id=trace_id,
                span_id=_span_id("backend", run_id),
                parent_span_id=run_span_id,
                name="harness.backend",
                start_time=run.created_at,
                end_time=run.updated_at,
                attributes=sanitize_for_logging(
                    {
                        "backend.name": manifest.backend_descriptor.name,
                        "backend.kind": manifest.backend_descriptor.kind.value,
                        "backend.billing_mode": manifest.backend_descriptor.metadata.billing_mode.value,
                        "backend.execution_location": manifest.backend_descriptor.metadata.execution_location.value,
                        "backend.data_boundary": manifest.backend_descriptor.metadata.data_boundary.value,
                        "backend.allow_network": manifest.backend_descriptor.metadata.allow_network,
                        "backend.sha256": manifest.backend_descriptor_sha256,
                    }
                ),
            )
        )
    if run.approval_id:
        approval = _find_approval(project_root, run.approval_id)
        spans.append(
            TraceSpan(
                trace_id=trace_id,
                span_id=_span_id("approval", run.approval_id),
                parent_span_id=run_span_id,
                name="harness.approval",
                start_time=approval.created_at if approval else run.created_at,
                end_time=approval.expires_at if approval else run.updated_at,
                attributes=sanitize_for_logging(
                    approval.model_dump(mode="json")
                    if approval
                    else {"approval.id": run.approval_id, "approval.status": "not_found"}
                ),
            )
        )
    if manifest.sandbox_profile is not None:
        spans.append(
            TraceSpan(
                trace_id=trace_id,
                span_id=_span_id("sandbox", run_id),
                parent_span_id=run_span_id,
                name="harness.sandbox",
                start_time=run.created_at,
                end_time=run.updated_at,
                attributes=sanitize_for_logging(
                    {
                        "sandbox.profile_id": manifest.sandbox_profile.get("id"),
                        "sandbox.tier": manifest.sandbox_profile.get("tier"),
                        "sandbox.network": manifest.sandbox_profile.get("network"),
                        "sandbox.active_repo_write": manifest.sandbox_profile.get("active_repo_write"),
                        "sandbox.host_filesystem": manifest.sandbox_profile.get("host_filesystem"),
                    }
                ),
            )
        )
    if manifest.delegate_budget is not None:
        spans.append(
            TraceSpan(
                trace_id=trace_id,
                span_id=_span_id("delegate_budget", run_id),
                parent_span_id=run_span_id,
                name="harness.delegate_budget",
                start_time=run.created_at,
                end_time=run.updated_at,
                attributes=sanitize_for_logging(_delegate_budget_trace_attributes(manifest.delegate_budget)),
            )
        )
    if task is not None and attempt is not None and lease is not None:
        spans.append(
            TraceSpan(
                trace_id=trace_id,
                span_id=_span_id("queue", lease.id),
                parent_span_id=run_span_id,
                name="harness.queue",
                start_time=task.created_at,
                end_time=lease.acquired_at,
                attributes=sanitize_for_logging(
                    {
                        "task.id": task.id,
                        "task.status": task.status.value,
                        "task.priority": task.priority,
                        "attempt.id": attempt.id,
                        "attempt.number": attempt.attempt_number,
                        "lease.id": lease.id,
                        "queue.wait_ms": _duration_ms(task.created_at, lease.acquired_at),
                    }
                ),
            )
        )
        spans.append(
            TraceSpan(
                trace_id=trace_id,
                span_id=_span_id("lease", lease.id),
                parent_span_id=run_span_id,
                name="harness.lease",
                start_time=lease.acquired_at,
                end_time=lease.released_at or run.updated_at,
                attributes=sanitize_for_logging(
                    {
                        "lease.id": lease.id,
                        "lease.owner": lease.owner,
                        "lease.status": lease.status.value,
                        "lease.task_id": lease.task_id,
                        "lease.attempt_id": lease.attempt_id,
                        "lease.acquired_at": lease.acquired_at.isoformat(),
                        "lease.expires_at": lease.expires_at.isoformat(),
                        "lease.heartbeat_at": lease.heartbeat_at.isoformat() if lease.heartbeat_at else None,
                        "lease.released_at": lease.released_at.isoformat() if lease.released_at else None,
                        "lease.ttl_ms": _duration_ms(lease.acquired_at, lease.expires_at),
                        "lease.runtime_ms": _duration_ms(lease.acquired_at, lease.released_at or run.updated_at),
                        "attempt.id": attempt.id,
                        "attempt.status": attempt.status.value,
                        "attempt.run_id": attempt.run_id,
                    }
                ),
            )
        )
    spans.extend(_event_spans(store, trace_id, run_span_id, run_id))
    spans.extend(_artifact_spans(manifest.artifacts, trace_id, run_span_id))
    spans.extend(_context_spans(manifest.context_provenance, trace_id, run_span_id, run.created_at, run.updated_at))
    _apply_semantic_convention_attributes(spans, export_kind="run")
    span_hash = stable_json_sha256([span.model_dump(mode="json") for span in spans])
    provenance = trace_export_provenance(run_id, trace_id, span_hash)
    spans[0].attributes = sanitize_for_logging(
        {
            **spans[0].attributes,
            "trace.provenance_id": provenance["id"],
            "trace.output_sha256": span_hash,
            "trace.producer": provenance["producer"],
        }
    )
    return TraceExport(run_id=run_id, trace_id=trace_id, spans=spans)


def export_objective_trace(project_root: Path, store: SQLiteStore, objective_id: str) -> TraceExport:
    project_root = project_root.resolve()
    objective = store.get_objective(objective_id)
    evidence_path = project_root / HARNESS_DIR / "autonomy" / "objectives" / f"{objective_id}.jsonl"
    if not evidence_path.exists():
        raise KeyError(f"Objective evidence not found: {objective_id}")
    events, parse_errors = read_objective_evidence_events(evidence_path)
    if parse_errors:
        raise ValueError(f"Objective evidence is malformed: {objective_id}")

    trace_id = _trace_id(f"objective:{objective_id}")
    root_span_id = _span_id("objective", objective_id)
    objective_run_ids = sorted(
        {
            str(event.get("objective_run_id"))
            for _, event in events
            if isinstance(event.get("objective_run_id"), str) and event.get("objective_run_id")
        }
    )
    verification = verify_objective_evidence(project_root, objective_id)
    hash_chain_check = next((check for check in verification.checks if check.id == "event_hash_chain"), None)
    event_spans: list[TraceSpan] = []
    for objective_run_id in objective_run_ids:
        run_events = [(line, event) for line, event in events if event.get("objective_run_id") == objective_run_id]
        event_times = [_objective_event_time(store, objective, event) for _, event in run_events]
        run_start = min(event_times) if event_times else objective.created_at
        run_end = max(event_times) if event_times else objective.updated_at
        run_span_id = _span_id("objective_run", objective_run_id)
        event_spans.append(
            TraceSpan(
                trace_id=trace_id,
                span_id=run_span_id,
                parent_span_id=root_span_id,
                name="harness.objective_run",
                start_time=run_start,
                end_time=run_end,
                attributes=sanitize_for_logging(
                    {
                        "objective.id": objective_id,
                        "objective_run.id": objective_run_id,
                        "objective_run.event_count": len(run_events),
                        "objective_run.stop_reason": _objective_run_stop_reason(run_events),
                    }
                ),
            )
        )
        for line_number, event in run_events:
            event_time = _objective_event_time(store, objective, event)
            event_name = str(event.get("event") or "unknown")
            event_identity = _objective_event_identity(objective_run_id, line_number, event_name, event)
            event_payload = _trace_payload(event)
            event_spans.append(
                TraceSpan(
                    trace_id=trace_id,
                    span_id=_span_id("objective_event", event_identity),
                    parent_span_id=run_span_id,
                    name=f"harness.objective_event.{event_name}",
                    start_time=event_time,
                    end_time=event_time,
                attributes=sanitize_for_logging(
                    {
                        "objective.id": objective_id,
                        "objective_run.id": objective_run_id,
                        "objective_event.id": event.get("objective_event_id"),
                            "objective_event.index": event.get("event_index"),
                            "objective_event.line": line_number,
                            "objective_event.type": event_name,
                            "objective_event.payload": event_payload,
                            **_payload_metadata("objective_event.payload", event_payload),
                        }
                    ),
                )
            )

    child_times = [span.start_time for span in event_spans] + [span.end_time for span in event_spans]
    root_start = min(child_times) if child_times else objective.created_at
    root_end = max(child_times) if child_times else objective.updated_at
    root_span = TraceSpan(
        trace_id=trace_id,
        span_id=root_span_id,
        name="harness.objective",
        start_time=root_start,
        end_time=root_end,
        attributes=sanitize_for_logging(
            {
                "objective.id": objective.id,
                "objective.status": objective.status.value,
                "objective.title": objective.title,
                "objective.evidence_path": str(evidence_path),
                "objective.evidence_verification_ok": verification.ok,
                "objective.evidence_verification_summary": verification.summary,
                "objective.evidence_event_count": len(events),
                "objective.evidence_hash_chain_ok": hash_chain_check.status == "pass" if hash_chain_check else False,
                "objective.evidence_head_sha256": hash_chain_check.evidence.get("head_sha256")
                if hash_chain_check
                else None,
                "objective.objective_run_ids": objective_run_ids,
            }
        ),
    )
    spans = [root_span, *event_spans]
    _apply_semantic_convention_attributes(spans, export_kind="objective")
    span_hash = stable_json_sha256([span.model_dump(mode="json") for span in spans])
    provenance = trace_export_provenance(f"objective:{objective_id}", trace_id, span_hash)
    spans[0].attributes = sanitize_for_logging(
        {
            **spans[0].attributes,
            "trace.provenance_id": provenance["id"],
            "trace.output_sha256": span_hash,
            "trace.producer": provenance["producer"],
        }
    )
    return TraceExport(
        ok=verification.ok,
        objective_id=objective_id,
        objective_run_ids=objective_run_ids,
        trace_id=trace_id,
        spans=spans,
    )


def to_otel_json(export: TraceExport) -> dict[str, Any]:
    payload = {
        "schema_version": export.schema_version,
        "ok": export.ok,
        "format": export.format,
        "semantic_conventions": list(export.semantic_conventions),
        "trace_context": dict(export.trace_context),
        "trace_id": export.trace_id,
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": "harness"},
                        {"key": "service.version", "value": export.schema_version},
                        {"key": "harness.trace.semantic_conventions", "value": list(export.semantic_conventions)},
                    ]
                },
                "scopeSpans": [
                    {
                        "scope": {
                            "name": "harness.trace_export",
                            "version": export.schema_version,
                            "attributes": [
                                {"key": "harness.trace.w3c_trace_context", "value": True},
                                {
                                    "key": "harness.trace.external_protocol_propagation_required",
                                    "value": True,
                                },
                            ],
                        },
                        "spans": [_span_to_otel(span) for span in export.spans],
                    }
                ],
            }
        ],
    }
    if export.run_id is not None:
        payload["run_id"] = export.run_id
    if export.objective_id is not None:
        payload["objective_id"] = export.objective_id
        payload["objective_run_ids"] = export.objective_run_ids
    return payload


def _event_spans(store: SQLiteStore, trace_id: str, parent_span_id: str, run_id: str) -> list[TraceSpan]:
    spans = []
    for event in store.list_events(run_id):
        event_payload = _trace_payload(event.payload)
        spans.append(
            TraceSpan(
                trace_id=trace_id,
                span_id=_span_id("event", event.id),
                parent_span_id=parent_span_id,
                name=f"harness.event.{event.event_type}",
                start_time=event.created_at,
                end_time=event.created_at,
                attributes=sanitize_for_logging(
                    {
                        "event.id": event.id,
                        "event.level": event.level,
                        "event.type": event.event_type,
                        "event.message": event.message,
                        "event.payload": event_payload,
                        "event.redaction_state": event.redaction_state.value,
                        **_payload_metadata("event.payload", event_payload),
                    }
                ),
            )
        )
    return spans


def _artifact_spans(artifacts, trace_id: str, parent_span_id: str) -> list[TraceSpan]:
    spans = []
    for artifact in artifacts:
        spans.append(
            TraceSpan(
                trace_id=trace_id,
                span_id=_span_id("artifact", artifact.id or f"{artifact.run_id}:{artifact.kind}"),
                parent_span_id=parent_span_id,
                name=f"harness.artifact.{artifact.kind}",
                start_time=artifact.created_at,
                end_time=artifact.created_at,
                attributes=sanitize_for_logging(
                    {
                        "artifact.id": artifact.id,
                        "artifact.run_id": artifact.run_id,
                        "artifact.kind": artifact.kind,
                        "artifact.path": str(artifact.path),
                        "artifact.sha256": artifact.sha256,
                        "artifact.size_bytes": artifact.size_bytes,
                        "artifact.producer": artifact.producer,
                        "artifact.redaction_state": artifact.redaction_state,
                        "artifact.evidence_status": artifact.evidence_status,
                        "artifact.provenance_id": artifact.provenance.id if artifact.provenance else None,
                        "artifact.provenance_source_kind": artifact.provenance.source_kind
                        if artifact.provenance
                        else None,
                        "artifact.provenance_output_sha256": artifact.provenance.output_sha256
                        if artifact.provenance
                        else None,
                    }
                ),
            )
        )
    return spans


def _run_attempt_and_lease(store: SQLiteStore, run) -> tuple[Any | None, Any | None]:
    if not run.task_id:
        return None, None
    attempts = [attempt for attempt in store.list_task_attempts(run.task_id) if attempt.run_id == run.id]
    if not attempts:
        return None, None
    attempt = attempts[0]
    if not attempt.lease_id:
        return attempt, None
    try:
        return attempt, store.get_task_lease(attempt.lease_id)
    except KeyError:
        return attempt, None


def _security_decision_for_run(project_root: Path, store: SQLiteStore, run) -> Any | None:
    if not run.task_id:
        return None
    try:
        task = store.get_task(run.task_id)
    except KeyError:
        return None
    attempt, lease = _run_attempt_and_lease(store, run)
    if attempt is None or lease is None:
        return None
    return evaluate_registered_adapter_security_decision(project_root, lease, task, attempt, owner=lease.owner)


def _delegate_budget_summary_attributes(delegate_budget: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(delegate_budget, dict):
        return {
            "delegate_budget.schema_version": None,
            "delegate_budget.limited": None,
            "delegate_budget.gap_count": None,
        }
    budget = delegate_budget.get("budget")
    return {
        "delegate_budget.schema_version": budget.get("schema_version") if isinstance(budget, dict) else None,
        "delegate_budget.limited": delegate_budget.get("budget_limited"),
        "delegate_budget.gap_count": len(delegate_budget.get("gaps") or []),
    }


def _delegate_budget_trace_attributes(delegate_budget: dict[str, Any]) -> dict[str, Any]:
    budget = delegate_budget.get("budget") if isinstance(delegate_budget, dict) else None
    budget = budget if isinstance(budget, dict) else {}
    return {
        "delegate_budget.adapter_id": delegate_budget.get("adapter_id"),
        "delegate_budget.schema_version": budget.get("schema_version"),
        "delegate_budget.limited": delegate_budget.get("budget_limited"),
        "delegate_budget.gap_count": len(delegate_budget.get("gaps") or []),
        "delegate_budget.gaps": list(delegate_budget.get("gaps") or []),
        "delegate_budget.sandbox_profile_id": delegate_budget.get("sandbox_profile_id"),
        "delegate_budget.sandbox_tier": delegate_budget.get("sandbox_tier"),
        "delegate_budget.sandbox_network": delegate_budget.get("sandbox_network"),
        "delegate_budget.sandbox_active_repo_write": delegate_budget.get("sandbox_active_repo_write"),
        "delegate_budget.network_policy": _enum_or_value(budget.get("network_policy")),
        "delegate_budget.active_repo_write": _enum_or_value(budget.get("active_repo_write")),
        "delegate_budget.filesystem_scope": budget.get("filesystem_scope"),
        "delegate_budget.cost_policy": budget.get("cost_policy"),
        "delegate_budget.timeout_seconds": budget.get("timeout_seconds"),
        "delegate_budget.max_cpu_seconds": budget.get("max_cpu_seconds"),
        "delegate_budget.max_memory_mb": budget.get("max_memory_mb"),
        "delegate_budget.max_runtime_invocations": budget.get("max_runtime_invocations"),
        "delegate_budget.max_model_calls": budget.get("max_model_calls"),
        "delegate_budget.max_tool_calls": budget.get("max_tool_calls"),
        "delegate_budget.max_parallel_branches": budget.get("max_parallel_branches"),
        "delegate_budget.max_input_tokens": budget.get("max_input_tokens"),
        "delegate_budget.max_output_tokens": budget.get("max_output_tokens"),
        "delegate_budget.max_cost_usd": budget.get("max_cost_usd"),
        "delegate_budget.tool_allowlist": list(budget.get("tool_allowlist") or []),
    }


def _apply_semantic_convention_attributes(spans: list[TraceSpan], *, export_kind: str) -> None:
    """Attach stable OTel GenAI compatibility metadata before provenance hashing."""

    for span in spans:
        attributes = dict(span.attributes)
        attributes.setdefault("harness.trace.export_kind", export_kind)
        attributes.setdefault("harness.trace.semantic_conventions", list(TRACE_SEMANTIC_CONVENTIONS))
        attributes.setdefault("harness.trace.w3c_trace_context", TRACE_CONTEXT_PROPAGATION["w3c_trace_context"])
        attributes.setdefault(
            "harness.trace.external_protocol_propagation_required",
            TRACE_CONTEXT_PROPAGATION["external_protocol_propagation_required"],
        )
        operation = _genai_operation_name(span.name)
        if operation is not None:
            attributes.setdefault("gen_ai.operation.name", operation)
            attributes.setdefault("gen_ai.system", "harness")
        if span.name == "harness.run":
            attributes.setdefault("gen_ai.agent.id", attributes.get("adapter.id") or attributes.get("task.type") or "harness")
            attributes.setdefault("gen_ai.agent.name", attributes.get("task.type") or "harness.run")
            if attributes.get("session.id") is None and attributes.get("objective.id") is not None:
                attributes.setdefault("gen_ai.conversation.id", attributes.get("objective.id"))
            elif attributes.get("session.id") is not None:
                attributes.setdefault("gen_ai.conversation.id", attributes.get("session.id"))
        elif span.name == "harness.objective":
            attributes.setdefault("gen_ai.agent.id", "harness.objective_runner")
            attributes.setdefault("gen_ai.agent.name", "harness.objective")
            attributes.setdefault("gen_ai.conversation.id", attributes.get("objective.id"))
            attributes.setdefault("workflow.id", attributes.get("objective.id"))
            attributes.setdefault("workflow.name", attributes.get("objective.title"))
        elif span.name == "harness.objective_run":
            attributes.setdefault("gen_ai.agent.id", "harness.objective_runner")
            attributes.setdefault("gen_ai.agent.name", "harness.objective_run")
            attributes.setdefault("gen_ai.conversation.id", attributes.get("objective.id"))
            attributes.setdefault("workflow.id", attributes.get("objective.id"))
        elif span.name.startswith("harness.objective_event.adapter_dispatched"):
            payload = attributes.get("objective_event.payload")
            if isinstance(payload, dict):
                adapter_id = payload.get("adapter_id") or payload.get("execution_adapter")
                if adapter_id is not None:
                    attributes.setdefault("gen_ai.tool.name", str(adapter_id))
                    attributes.setdefault("gen_ai.tool.type", "harness_execution_adapter")
        span.attributes = sanitize_for_logging(attributes)


def _genai_operation_name(span_name: str) -> str | None:
    if span_name == "harness.run":
        return "invoke_agent"
    if span_name in {"harness.backend"}:
        return "chat"
    if span_name in {"harness.objective", "harness.objective_run"}:
        return "invoke_agent"
    if span_name.startswith("harness.objective_event.adapter_dispatched"):
        return "execute_tool"
    return None


def _enum_or_value(value: Any) -> Any:
    return getattr(value, "value", value)


def _duration_ms(start, end) -> int | None:
    if start is None or end is None:
        return None
    try:
        return max(0, int((end - start).total_seconds() * 1000))
    except Exception:
        return None


def _context_spans(records, trace_id: str, parent_span_id: str, start_time, end_time) -> list[TraceSpan]:
    spans = []
    for record in records:
        spans.append(
            TraceSpan(
                trace_id=trace_id,
                span_id=_span_id("context", record.id),
                parent_span_id=parent_span_id,
                name=f"harness.context.{record.source_kind.value}",
                start_time=start_time,
                end_time=end_time,
                attributes=sanitize_for_logging(
                    {
                        "context.id": record.id,
                        "context.source_kind": record.source_kind.value,
                        "context.trust_level": record.trust_level.value,
                        "context.label": record.label,
                        "context.source_id": record.source_id,
                        "context.artifact_id": record.artifact_id,
                        "context.memory_id": record.memory_id,
                        "context.sha256": record.sha256,
                        "context.redaction_state": record.redaction_state,
                        "context.warning_codes": record.warnings,
                    }
                ),
            )
        )
    return spans


def _payload_metadata(prefix: str, payload: Any) -> dict[str, Any]:
    sanitized = _trace_payload(payload)
    serialized = _stable_payload_json(sanitized)
    keys = sorted(str(key) for key in sanitized) if isinstance(sanitized, dict) else []
    return {
        f"{prefix}_sha256": hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
        f"{prefix}_size_bytes": len(serialized.encode("utf-8")),
        f"{prefix}_keys": keys,
    }


def _stable_payload_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, default=str)


def _trace_payload(payload: Any) -> Any:
    return _redact_sensitive_keys(sanitize_for_logging(payload))


def _redact_sensitive_keys(payload: Any) -> Any:
    if isinstance(payload, dict):
        redacted: dict[str, Any] = {}
        redacted_count = 0
        for key, value in payload.items():
            key_text = str(key)
            if _is_sensitive_key(key_text):
                redacted_count += 1
                safe_key = "[REDACTED_KEY]" if redacted_count == 1 else f"[REDACTED_KEY_{redacted_count}]"
            else:
                safe_key = key_text
            redacted[safe_key] = _redact_sensitive_keys(value)
        return redacted
    if isinstance(payload, list):
        return [_redact_sensitive_keys(item) for item in payload]
    if isinstance(payload, tuple):
        return tuple(_redact_sensitive_keys(item) for item in payload)
    return payload


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(
        marker in lowered
        for marker in (
            "api_key",
            "apikey",
            "authorization",
            "credential",
            "password",
            "passwd",
            "secret",
            "token",
        )
    )


def _objective_event_time(store: SQLiteStore, objective, event: dict[str, Any]):
    event_time = _event_created_at(event)
    if event_time is not None:
        return event_time
    run_id = event.get("run_id")
    if isinstance(run_id, str) and run_id:
        try:
            run = store.get_run(run_id)
            return run.updated_at if event.get("event") == "adapter_dispatched" else run.created_at
        except KeyError:
            pass
    lease_id = event.get("lease_id")
    if isinstance(lease_id, str) and lease_id:
        try:
            return store.get_task_lease(lease_id).acquired_at
        except KeyError:
            pass
    task_ids = event.get("task_ids")
    if isinstance(task_ids, list):
        times = []
        for task_id in task_ids:
            if isinstance(task_id, str):
                try:
                    task = store.get_task(task_id)
                    times.append(task.updated_at)
                except KeyError:
                    continue
        if times:
            return max(times)
    if event.get("event") == "stopped":
        return objective.updated_at
    return objective.created_at


def _event_created_at(event: dict[str, Any]) -> datetime | None:
    value = event.get("created_at")
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _objective_run_stop_reason(run_events: list[tuple[int, dict[str, Any]]]) -> str | None:
    for _, event in reversed(run_events):
        if event.get("event") == "stopped":
            stop_reason = event.get("stop_reason")
            return str(stop_reason) if stop_reason is not None else None
    return None


def _objective_event_identity(objective_run_id: str, line_number: int, event_name: str, event: dict[str, Any]) -> str:
    event_id = event.get("objective_event_id")
    if isinstance(event_id, str) and event_id:
        return f"{objective_run_id}:{event_id}:{event_name}"
    return f"{objective_run_id}:{line_number}:{event_name}"


def _span_to_otel(span: TraceSpan) -> dict[str, Any]:
    payload = {
        "traceId": span.trace_id,
        "spanId": span.span_id,
        "name": span.name,
        "kind": span.kind,
        "startTimeUnixNano": _unix_nanos(span.start_time),
        "endTimeUnixNano": _unix_nanos(span.end_time),
        "attributes": [
            {"key": key, "value": value}
            for key, value in sorted(sanitize_for_logging(span.attributes).items())
        ],
    }
    if span.parent_span_id is not None:
        payload["parentSpanId"] = span.parent_span_id
    return payload


def _unix_nanos(value) -> str:
    return str(int(value.timestamp() * 1_000_000_000))


def _trace_id(run_id: str) -> str:
    return hashlib.sha256(f"trace:{run_id}".encode("utf-8")).hexdigest()[:32]


def _span_id(kind: str, value: str) -> str:
    return hashlib.sha256(f"{kind}:{value}".encode("utf-8")).hexdigest()[:16]


def _find_approval(project_root: Path, approval_id: str):
    for approval in ApprovalStore(project_root).list():
        if approval.id == approval_id:
            return approval
    return None
