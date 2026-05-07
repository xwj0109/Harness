from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from harness.approvals import ApprovalStore
from harness.memory.sqlite_store import SQLiteStore
from harness.models import TraceExport, TraceSpan
from harness.security import sanitize_for_logging


def export_run_trace(project_root: Path, store: SQLiteStore, run_id: str) -> TraceExport:
    run = store.get_run(run_id)
    manifest = store.build_run_manifest(run_id)
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
                    "run.task_type": run.task_type,
                    "run.mode": manifest.run_mode.value,
                    "project.root": str(run.project_root),
                    "policy.sha256": manifest.effective_policy_sha256,
                    "backend.name": run.backend_name,
                    "backend.kind": run.backend_kind.value if run.backend_kind else None,
                    "backend.sha256": manifest.backend_descriptor_sha256,
                    "approval.id": run.approval_id,
                    "task.id": manifest.task_id,
                    "objective.id": manifest.objective_id,
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
    spans.extend(_event_spans(store, trace_id, run_span_id, run_id))
    spans.extend(_artifact_spans(manifest.artifacts, trace_id, run_span_id))
    return TraceExport(run_id=run_id, trace_id=trace_id, spans=spans)


def to_otel_json(export: TraceExport) -> dict[str, Any]:
    return {
        "schema_version": export.schema_version,
        "ok": export.ok,
        "format": export.format,
        "run_id": export.run_id,
        "trace_id": export.trace_id,
        "resourceSpans": [
            {
                "resource": {"attributes": [{"key": "service.name", "value": "harness"}]},
                "scopeSpans": [
                    {
                        "scope": {"name": "harness.trace_export", "version": export.schema_version},
                        "spans": [_span_to_otel(span) for span in export.spans],
                    }
                ],
            }
        ],
    }


def _event_spans(store: SQLiteStore, trace_id: str, parent_span_id: str, run_id: str) -> list[TraceSpan]:
    spans = []
    for event in store.list_events(run_id):
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
                        "event.payload": event.payload,
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
                    }
                ),
            )
        )
    return spans


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
