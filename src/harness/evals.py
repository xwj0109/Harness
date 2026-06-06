from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from harness.approvals import ApprovalStore
from harness.capabilities import build_capability_catalog
from harness.config import HARNESS_DIR, HarnessConfig
from harness.execution import list_execution_adapter_descriptors
from harness.integrity import run_integrity_check
from harness.memory.sqlite_store import SQLiteStore
from harness.models import (
    DataBoundary,
    PolicyLevel,
    SafetySmokeCheck,
    SafetySmokeResult,
    SecurityCheckResult,
    SecurityFinding,
    SecurityFindingSeverity,
    SecurityFindingStatus,
    SecurityLayerAuditCheck,
    SecurityLayerAuditResult,
    TaskStatus,
)
from harness.objective_evidence import verify_objective_evidence
from harness.operator_context import build_operator_context
from harness.policy import resolve_backend_effective_policy
from harness.sandbox_profiles import get_sandbox_profile
from harness.security import sanitize_for_logging, scan_text_for_secrets
from harness.traces import export_objective_trace, export_run_trace


def run_safety_smoke(project_root: Path, config: HarnessConfig, store: SQLiteStore) -> SafetySmokeResult:
    checks = [
        _sandbox_network_check(config),
        _backend_boundary_check(config),
        _artifact_evidence_check(store),
        _task_queue_non_execution_check(store),
        _manifest_policy_check(store),
    ]
    return SafetySmokeResult(ok=all(check.status == "pass" for check in checks), checks=checks)


def run_security_check(project_root: Path, store: SQLiteStore) -> SecurityCheckResult:
    project_root = project_root.resolve()
    findings: list[SecurityFinding] = []
    findings.extend(_daemon_rejection_findings(store))
    findings.extend(_run_manifest_findings(project_root, store))
    findings.extend(_secret_metadata_findings(project_root, store))
    findings.sort(key=lambda item: (item.status.value, item.severity.value, item.check_id, item.id))
    summary = {
        "total": len(findings),
        "pass": sum(1 for finding in findings if finding.status == SecurityFindingStatus.PASS),
        "fail": sum(1 for finding in findings if finding.status == SecurityFindingStatus.FAIL),
        "high": sum(1 for finding in findings if finding.severity == SecurityFindingSeverity.HIGH),
        "warning": sum(1 for finding in findings if finding.severity == SecurityFindingSeverity.WARNING),
        "info": sum(1 for finding in findings if finding.severity == SecurityFindingSeverity.INFO),
    }
    return SecurityCheckResult(
        ok=all(finding.status == SecurityFindingStatus.PASS for finding in findings),
        project_root=project_root,
        findings=findings,
        summary=summary,
    )


def run_security_layer_audit(project_root: Path) -> SecurityLayerAuditResult:
    project_root = project_root.resolve()
    checks: list[SecurityLayerAuditCheck] = []
    initialized = (project_root / ".harness" / "harness.sqlite").exists()
    checks.append(_registered_adapter_audit_check())
    checks.append(_integrity_audit_check(project_root))
    checks.append(_operator_context_audit_check(project_root))
    if not initialized:
        checks.extend(
            [
                _audit_check(
                    "runtime_controls_inspectable",
                    "skipped",
                    "Project runtime state is not initialized; runtime controls were not inspected.",
                    {"initialized": False},
                ),
                _audit_check(
                    "runtime_manifest_evidence",
                    "skipped",
                    "Project runtime state is not initialized; run manifests were not inspected.",
                    {"initialized": False},
                ),
                _audit_check(
                    "security_detections_callable",
                    "skipped",
                    "Project runtime state is not initialized; security detections were not inspected.",
                    {"initialized": False},
                ),
                _audit_check(
                    "run_trace_payload_metadata",
                    "skipped",
                    "Project runtime state is not initialized; run trace payload metadata was not inspected.",
                    {"initialized": False},
                ),
            ]
        )
    else:
        store = SQLiteStore(project_root)
        checks.append(_runtime_controls_audit_check(store))
        checks.append(_runtime_manifest_audit_check(store))
        checks.append(_run_trace_payload_metadata_audit_check(project_root, store))
        checks.append(_security_detection_audit_check(project_root, store))
        checks.append(_memory_boundary_audit_check(store))
        checks.append(_progress_blocked_state_audit_check(project_root, store))
        checks.append(_objective_evidence_audit_check(project_root, store))
    checks.sort(key=lambda item: item.id)
    summary = {
        "total": len(checks),
        "pass": sum(1 for check in checks if check.status == "pass"),
        "fail": sum(1 for check in checks if check.status == "fail"),
        "skipped": sum(1 for check in checks if check.status == "skipped"),
    }
    return SecurityLayerAuditResult(
        ok=all(check.status != "fail" for check in checks),
        project_root=project_root,
        checks=checks,
        summary=summary,
    )


def _registered_adapter_audit_check() -> SecurityLayerAuditCheck:
    failures = []
    evidence = []
    for descriptor in list_execution_adapter_descriptors():
        profile = get_sandbox_profile(descriptor.sandbox_profile_id) if descriptor.sandbox_profile_id else None
        evidence.append(
            {
                "adapter_id": descriptor.id,
                "schema_version": descriptor.schema_version,
                "sandbox_profile_id": descriptor.sandbox_profile_id,
                "required_approvals": descriptor.required_approvals,
            }
        )
        if profile is None:
            failures.append(f"{descriptor.id}: missing or unknown sandbox profile")
    return _audit_check(
        "registered_adapters_have_sandbox_profiles",
        "pass" if not failures else "fail",
        "Registered adapters declare valid sandbox profiles." if not failures else "; ".join(failures),
        {"adapters": evidence},
    )


def _integrity_audit_check(project_root: Path) -> SecurityLayerAuditCheck:
    result = run_integrity_check(project_root)
    return _audit_check(
        "integrity_checks_pass",
        "pass" if result.ok else "fail",
        "Local integrity checks pass." if result.ok else "Local integrity checks reported failures.",
        {"summary": result.summary},
    )


def _operator_context_audit_check(project_root: Path) -> SecurityLayerAuditCheck:
    context = build_operator_context(project_root)
    capability_rows = context.get("capabilities", {}).get("capabilities", [])
    has_blocked_field = all("blocked_state_explanations" in row for row in capability_rows)
    progress_rows = context.get("progress", {}).get("tasks", [])
    progress_has_blocked_field = all("blocked_state_explanations" in row for row in progress_rows)
    ok = bool(context.get("safety_boundaries")) and has_blocked_field and progress_has_blocked_field
    return _audit_check(
        "operator_context_surfaces_security_layer",
        "pass" if ok else "fail",
        "Operator context exposes security-layer summaries."
        if ok
        else "Operator context is missing security-layer summary fields.",
        {
            "initialized": context.get("initialized"),
            "capabilities": len(capability_rows),
            "progress_tasks": len(progress_rows),
            "safety_boundaries": context.get("safety_boundaries", []),
        },
    )


def _runtime_controls_audit_check(store: SQLiteStore) -> SecurityLayerAuditCheck:
    try:
        controls = store.list_execution_controls()
        breakers = store.list_adapter_breaker_states([descriptor.id for descriptor in list_execution_adapter_descriptors()])
    except Exception as exc:
        return _audit_check(
            "runtime_controls_inspectable",
            "fail",
            "Runtime controls could not be inspected.",
            {"error": str(exc)},
        )
    return _audit_check(
        "runtime_controls_inspectable",
        "pass",
        "Runtime controls and adapter breakers are inspectable.",
        {"controls": len(controls), "breakers": len(breakers)},
    )


def _runtime_manifest_audit_check(store: SQLiteStore) -> SecurityLayerAuditCheck:
    failures = []
    evidence = []
    for run in store.list_runs():
        manifest = store.build_run_manifest(run.id)
        adapter_id = _adapter_id_for_run(store, run.task_id)
        delegate_budget = manifest.delegate_budget or {}
        delegate_budget_payload = delegate_budget.get("budget") if isinstance(delegate_budget, dict) else None
        item = {
            "run_id": run.id,
            "adapter_id": adapter_id,
            "effective_policy_sha256": manifest.effective_policy_sha256,
            "backend_descriptor_sha256": manifest.backend_descriptor_sha256,
            "sandbox_profile_id": manifest.sandbox_profile.get("id") if manifest.sandbox_profile else None,
            "delegate_budget_schema_version": delegate_budget_payload.get("schema_version")
            if isinstance(delegate_budget_payload, dict)
            else None,
            "delegate_budget_limited": delegate_budget.get("budget_limited") if isinstance(delegate_budget, dict) else None,
            "delegate_budget_gap_count": len(delegate_budget.get("gaps") or []) if isinstance(delegate_budget, dict) else None,
            "artifacts": len(manifest.artifacts),
            "context_provenance": len(manifest.context_provenance),
        }
        evidence.append(item)
        if adapter_id:
            if not manifest.effective_policy_sha256:
                failures.append(f"{run.id}: missing policy hash")
            if manifest.sandbox_profile is None:
                failures.append(f"{run.id}: missing sandbox profile evidence")
            if not isinstance(delegate_budget_payload, dict):
                failures.append(f"{run.id}: missing delegate budget evidence")
            elif delegate_budget_payload.get("schema_version") != "harness.delegate_budget/v1":
                failures.append(f"{run.id}: delegate budget evidence is not v1")
            if isinstance(delegate_budget, dict) and delegate_budget.get("gaps"):
                failures.append(f"{run.id}: delegate budget evidence has validation gaps")
            if not manifest.context_provenance:
                failures.append(f"{run.id}: missing context provenance")
            for artifact in manifest.artifacts:
                if not artifact.redaction_state:
                    failures.append(f"{run.id}:{artifact.kind}: missing redaction state")
                if artifact.provenance is None:
                    failures.append(f"{run.id}:{artifact.kind}: missing artifact provenance")
    return _audit_check(
        "runtime_manifest_evidence",
        "pass" if not failures else "fail",
        "Run manifests expose security-layer evidence." if not failures else "; ".join(failures),
        {"runs": evidence},
    )


def _run_trace_payload_metadata_audit_check(project_root: Path, store: SQLiteStore) -> SecurityLayerAuditCheck:
    runs = store.list_runs()
    if not runs:
        return _audit_check(
            "run_trace_payload_metadata",
            "skipped",
            "No runtime runs are present for trace payload metadata inspection.",
            {"runs": 0},
        )

    failures: list[str] = []
    evidence: list[dict[str, Any]] = []
    for run in runs:
        run_events = store.list_events(run.id)
        adapter_id = _adapter_id_for_run(store, run.task_id)
        lease_trace_required = _run_has_linked_lease_attempt(store, run)
        try:
            export = export_run_trace(project_root, store, run.id)
        except Exception as exc:
            failures.append(f"{run.id}: run_trace_export")
            evidence.append(
                {
                    "run_id": run.id,
                    "adapter_id": adapter_id,
                    "ok": False,
                    "event_count": len(run_events),
                    "trace_export": {"ok": False, "error": f"{exc.__class__.__name__}: {exc}"},
                }
            )
            continue

        root_attributes = export.spans[0].attributes if export.spans else {}
        run_event_payload_metadata = _trace_payload_metadata_audit(
            export.spans,
            span_name_prefix="harness.event.",
            payload_prefix="event.payload",
            require_spans=bool(run_events),
        )
        delegate_budget_attributes = _trace_span_attributes(export.spans, "harness.delegate_budget")
        lease_attributes = _trace_span_attributes(export.spans, "harness.lease")
        queue_attributes = _trace_span_attributes(export.spans, "harness.queue")
        trace_export_ok = export.ok and bool(export.spans)
        trace_export_evidence = {
            "ok": trace_export_ok,
            "trace_id": export.trace_id,
            "span_count": len(export.spans),
            "trace_provenance_id": root_attributes.get("trace.provenance_id"),
            "trace_output_sha256": root_attributes.get("trace.output_sha256"),
            "trace_producer": root_attributes.get("trace.producer"),
            "run_event_payload_metadata_ok": run_event_payload_metadata["ok"],
            "run_event_payload_metadata": run_event_payload_metadata,
            "delegate_budget_span_present": delegate_budget_attributes is not None,
            "delegate_budget_schema_version": delegate_budget_attributes.get("delegate_budget.schema_version")
            if delegate_budget_attributes
            else None,
            "delegate_budget_limited": delegate_budget_attributes.get("delegate_budget.limited")
            if delegate_budget_attributes
            else None,
            "delegate_budget_gap_count": delegate_budget_attributes.get("delegate_budget.gap_count")
            if delegate_budget_attributes
            else None,
            "lease_span_required": lease_trace_required,
            "lease_span_present": lease_attributes is not None,
            "queue_span_present": queue_attributes is not None,
        }
        evidence.append(
            {
                "run_id": run.id,
                "adapter_id": adapter_id,
                "ok": trace_export_ok and run_event_payload_metadata["ok"],
                "event_count": len(run_events),
                "trace_export": trace_export_evidence,
            }
        )
        if not trace_export_ok:
            failures.append(f"{run.id}: run_trace_export")
        if not run_event_payload_metadata["ok"]:
            failures.append(f"{run.id}: run_trace_payload_metadata")
        if adapter_id:
            if delegate_budget_attributes is None:
                failures.append(f"{run.id}: trace_delegate_budget")
            elif delegate_budget_attributes.get("delegate_budget.schema_version") != "harness.delegate_budget/v1":
                failures.append(f"{run.id}: trace_delegate_budget_schema")
            elif delegate_budget_attributes.get("delegate_budget.limited") is not True:
                failures.append(f"{run.id}: trace_delegate_budget_unlimited")
            elif delegate_budget_attributes.get("delegate_budget.gap_count") != 0:
                failures.append(f"{run.id}: trace_delegate_budget_gaps")
        if lease_trace_required:
            if lease_attributes is None:
                failures.append(f"{run.id}: trace_lease")
            if queue_attributes is None:
                failures.append(f"{run.id}: trace_queue")

    return _audit_check(
        "run_trace_payload_metadata",
        "pass" if not failures else "fail",
        "Run traces expose payload metadata, delegate budgets, and lease timing without sensitive key leaks."
        if not failures
        else "; ".join(failures),
        {"runs": evidence},
    )


def _security_detection_audit_check(project_root: Path, store: SQLiteStore) -> SecurityLayerAuditCheck:
    result = run_security_check(project_root, store)
    return _audit_check(
        "security_detections_callable",
        "pass",
        "Security detections are callable and sanitized.",
        {"ok": result.ok, "summary": result.summary},
    )


def _memory_boundary_audit_check(store: SQLiteStore) -> SecurityLayerAuditCheck:
    failures = []
    for memory in store.list_memory_records(include_forgotten=True):
        lineage = memory.lineage
        if lineage.get("permission_granting") is not False:
            failures.append(f"{memory.id}: permission_granting not false")
        if lineage.get("policy_authority") is not False:
            failures.append(f"{memory.id}: policy_authority not false")
        if lineage.get("approval_authority") is not False:
            failures.append(f"{memory.id}: approval_authority not false")
    return _audit_check(
        "memory_context_not_authority",
        "pass" if not failures else "fail",
        "Memory/context provenance cannot grant authority." if not failures else "; ".join(failures),
        {"memory_records": len(store.list_memory_records(include_forgotten=True))},
    )


def _progress_blocked_state_audit_check(project_root: Path, store: SQLiteStore) -> SecurityLayerAuditCheck:
    from harness.progress import build_orchestration_progress

    failures = []
    inspected = 0
    for objective in store.list_objectives():
        progress = build_orchestration_progress(project_root, objective.id)
        for task in progress.tasks:
            inspected += 1
            if task.blocked_reasons and not task.blocked_state_explanations:
                failures.append(f"{task.task_id}: missing blocked state explanations")
    return _audit_check(
        "progress_blocked_states_explained",
        "pass" if not failures else "fail",
        "Progress blocked states expose structured explanations." if not failures else "; ".join(failures),
        {"progress_tasks": inspected},
    )


def _objective_evidence_audit_check(project_root: Path, store: SQLiteStore) -> SecurityLayerAuditCheck:
    evidence_dir = project_root / HARNESS_DIR / "autonomy" / "objectives"
    objective_ids = {objective.id for objective in store.list_objectives()}
    if evidence_dir.exists():
        objective_ids.update(path.stem for path in evidence_dir.glob("*.jsonl"))
    objective_ids = {
        objective_id
        for objective_id in objective_ids
        if (evidence_dir / f"{objective_id}.jsonl").exists()
    }
    if not objective_ids:
        return _audit_check(
            "objective_evidence_verifiable",
            "skipped",
            "No autonomous objective JSONL evidence is present.",
            {"objective_evidence_files": 0},
        )

    failures: list[str] = []
    evidence: list[dict[str, Any]] = []
    for objective_id in sorted(objective_ids):
        verification = verify_objective_evidence(project_root, objective_id)
        failed_checks = [check for check in verification.checks if check.status == "fail"]
        trace_export_evidence = _objective_trace_export_audit_evidence(project_root, store, objective_id)
        evidence.append(
            {
                "objective_id": objective_id,
                "ok": verification.ok,
                "evidence_path": str(verification.evidence_path),
                "summary": verification.summary,
                "trace_export": trace_export_evidence,
                "failed_checks": [
                    {
                        "id": check.id,
                        "message": check.message,
                        "evidence": check.evidence,
                    }
                    for check in failed_checks
                ],
            }
        )
        if failed_checks:
            failures.append(f"{objective_id}: {', '.join(check.id for check in failed_checks)}")
        if not trace_export_evidence.get("ok"):
            failures.append(f"{objective_id}: objective_trace_export")
        if not trace_export_evidence.get("objective_event_payload_metadata_ok"):
            failures.append(f"{objective_id}: objective_trace_payload_metadata")
    return _audit_check(
        "objective_evidence_verifiable",
        "pass" if not failures else "fail",
        "Autonomous objective evidence verifies and exports as trace evidence."
        if not failures
        else "Autonomous objective evidence verification or trace export failed.",
        {"objectives": evidence},
    )


def _objective_trace_export_audit_evidence(project_root: Path, store: SQLiteStore, objective_id: str) -> dict[str, Any]:
    try:
        export = export_objective_trace(project_root, store, objective_id)
    except Exception as exc:
        return {"ok": False, "error": f"{exc.__class__.__name__}: {exc}"}
    root_attributes = export.spans[0].attributes if export.spans else {}
    objective_event_payload_metadata = _trace_payload_metadata_audit(
        export.spans,
        span_name_prefix="harness.objective_event.",
        payload_prefix="objective_event.payload",
    )
    return {
        "ok": export.ok and bool(export.spans),
        "trace_id": export.trace_id,
        "objective_run_ids": export.objective_run_ids,
        "span_count": len(export.spans),
        "objective_evidence_event_count": root_attributes.get("objective.evidence_event_count"),
        "objective_evidence_hash_chain_ok": root_attributes.get("objective.evidence_hash_chain_ok"),
        "objective_evidence_head_sha256": root_attributes.get("objective.evidence_head_sha256"),
        "trace_provenance_id": root_attributes.get("trace.provenance_id"),
        "trace_output_sha256": root_attributes.get("trace.output_sha256"),
        "trace_producer": root_attributes.get("trace.producer"),
        "objective_event_payload_metadata_ok": objective_event_payload_metadata["ok"],
        "objective_event_payload_metadata": objective_event_payload_metadata,
    }


def _trace_payload_metadata_audit(
    spans: list[Any],
    *,
    span_name_prefix: str,
    payload_prefix: str,
    require_spans: bool = True,
) -> dict[str, Any]:
    span_count = 0
    missing_payload_metadata: list[dict[str, Any]] = []
    sensitive_key_leaks: list[dict[str, Any]] = []
    hash_key = f"{payload_prefix}_sha256"
    size_key = f"{payload_prefix}_size_bytes"
    keys_key = f"{payload_prefix}_keys"
    for span in spans:
        if not str(getattr(span, "name", "")).startswith(span_name_prefix):
            continue
        span_count += 1
        attributes = getattr(span, "attributes", {}) or {}
        payload_hash = attributes.get(hash_key)
        payload_size = attributes.get(size_key)
        payload_keys = attributes.get(keys_key)
        reasons = []
        if not isinstance(payload_hash, str) or len(payload_hash) != 64:
            reasons.append(hash_key)
        if not isinstance(payload_size, int) or payload_size < 0:
            reasons.append(size_key)
        if not isinstance(payload_keys, list):
            reasons.append(keys_key)
        if reasons:
            missing_payload_metadata.append(
                {
                    "span_id": getattr(span, "span_id", None),
                    "span_name": getattr(span, "name", None),
                    "missing": reasons,
                }
            )
            continue
        for key in payload_keys:
            if _sensitive_trace_key(str(key)):
                sensitive_key_leaks.append(
                    {
                        "span_id": getattr(span, "span_id", None),
                        "span_name": getattr(span, "name", None),
                        "key": str(key),
                    }
                )
    return {
        "ok": (span_count > 0 or not require_spans) and not missing_payload_metadata and not sensitive_key_leaks,
        "span_count": span_count,
        "required": require_spans,
        "missing_payload_metadata": missing_payload_metadata,
        "sensitive_key_leaks": sensitive_key_leaks,
    }


def _trace_span_attributes(spans: list[Any], span_name: str) -> dict[str, Any] | None:
    for span in spans:
        if getattr(span, "name", None) == span_name:
            attributes = getattr(span, "attributes", {}) or {}
            return attributes if isinstance(attributes, dict) else {}
    return None


def _run_has_linked_lease_attempt(store: SQLiteStore, run) -> bool:
    if not run.task_id:
        return False
    for attempt in store.list_task_attempts(run.task_id):
        if attempt.run_id == run.id and attempt.lease_id:
            return True
    return False


def _sensitive_trace_key(key: str) -> bool:
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


def _audit_check(check_id: str, status: str, message: str, evidence: dict[str, Any]) -> SecurityLayerAuditCheck:
    return SecurityLayerAuditCheck(
        id=check_id,
        status=status,
        message=str(sanitize_for_logging(message)),
        evidence=sanitize_for_logging(evidence),
    )


def _daemon_rejection_findings(store: SQLiteStore) -> list[SecurityFinding]:
    findings: list[SecurityFinding] = []
    for event in store.list_daemon_events(limit=1000):
        if event.event_type != "execution_adapter_rejected":
            continue
        reason_code = str(event.metadata.get("reason_code") or "")
        adapter_id = _optional_str(event.metadata.get("adapter_id"))
        if reason_code == "unknown_adapter":
            findings.append(
                _finding(
                    "unknown_adapter_dispatch_attempt",
                    SecurityFindingSeverity.WARNING,
                    "Unknown registered adapter dispatch was rejected.",
                    event.created_at,
                    evidence={"daemon_event_id": event.id, "reason_code": reason_code},
                    adapter_id=adapter_id,
                    task_id=_optional_str(event.metadata.get("task_id")),
                    attempt_id=_optional_str(event.metadata.get("attempt_id")),
                    lease_id=_optional_str(event.metadata.get("lease_id")),
                    security_decision_id=_optional_str(event.metadata.get("security_decision_id")),
                    policy_sha256=_optional_str(event.metadata.get("policy_sha256")),
                )
            )
        if reason_code == "breaker_open":
            findings.append(
                _finding(
                    "breaker_open_execution_attempt",
                    SecurityFindingSeverity.WARNING,
                    "Execution was attempted while the adapter breaker was open.",
                    event.created_at,
                    evidence={"daemon_event_id": event.id, "reason_code": reason_code},
                    adapter_id=adapter_id,
                    task_id=_optional_str(event.metadata.get("task_id")),
                    attempt_id=_optional_str(event.metadata.get("attempt_id")),
                    lease_id=_optional_str(event.metadata.get("lease_id")),
                    security_decision_id=_optional_str(event.metadata.get("security_decision_id")),
                    policy_sha256=_optional_str(event.metadata.get("policy_sha256")),
                )
            )
        if reason_code in {"missing_required_approval", "unresolved_task_approvals"} and _is_codex_adapter(adapter_id):
            findings.append(
                _finding(
                    "high_risk_adapter_without_approval",
                    SecurityFindingSeverity.HIGH,
                    "High-risk registered adapter was attempted without required approval.",
                    event.created_at,
                    evidence={"daemon_event_id": event.id, "reason_code": reason_code},
                    adapter_id=adapter_id,
                    task_id=_optional_str(event.metadata.get("task_id")),
                    attempt_id=_optional_str(event.metadata.get("attempt_id")),
                    lease_id=_optional_str(event.metadata.get("lease_id")),
                    security_decision_id=_optional_str(event.metadata.get("security_decision_id")),
                    policy_sha256=_optional_str(event.metadata.get("policy_sha256")),
                )
            )
    return findings


def _run_manifest_findings(project_root: Path, store: SQLiteStore) -> list[SecurityFinding]:
    findings: list[SecurityFinding] = []
    approvals = ApprovalStore(project_root)
    for run in store.list_runs():
        manifest = store.build_run_manifest(run.id)
        adapter_id = _adapter_id_for_run(store, run.task_id)
        sandbox_profile_id = manifest.sandbox_profile.get("id") if manifest.sandbox_profile else None
        if run.data_boundary == DataBoundary.HOSTED_PROVIDER:
            approval_valid = False
            if run.approval_id:
                approval = _approval_by_id(approvals, run.approval_id)
                approval_valid = bool(
                    approval
                    and run.task_type
                    and approval.is_valid_for("codex_cli", project_root, "hosted_provider", run.task_type)
                )
            if not approval_valid:
                findings.append(
                    _finding(
                        "hosted_boundary_without_approval",
                        SecurityFindingSeverity.HIGH,
                        "Hosted-boundary run is missing valid approval evidence.",
                        run.updated_at,
                        evidence={"data_boundary": run.data_boundary.value, "approval_present": bool(run.approval_id)},
                        run_id=run.id,
                        task_id=run.task_id,
                        adapter_id=adapter_id,
                        policy_sha256=manifest.effective_policy_sha256,
                        approval_id=run.approval_id,
                        sandbox_profile_id=sandbox_profile_id,
                    )
                )
        if adapter_id and sandbox_profile_id is None:
            findings.append(
                _finding(
                    "missing_sandbox_profile_evidence",
                    SecurityFindingSeverity.WARNING,
                    "Registered-adapter run is missing sandbox profile evidence.",
                    run.updated_at,
                    evidence={"adapter_id": adapter_id, "task_type": run.task_type},
                    run_id=run.id,
                    task_id=run.task_id,
                    adapter_id=adapter_id,
                    policy_sha256=manifest.effective_policy_sha256,
                )
            )
        events = store.list_events(run.id)
        if any(event.event_type == "apply_back_applied" for event in events):
            has_diff = any(event.event_type == "isolated_diff_inspected" for event in events)
            has_approved = any(
                event.event_type == "apply_back_decision" and event.payload.get("decision") == "approved"
                for event in events
            )
            if not (has_diff and has_approved):
                findings.append(
                    _finding(
                        "apply_back_without_inspected_approval",
                        SecurityFindingSeverity.HIGH,
                        "Apply-back was applied without complete inspected-diff approval evidence.",
                        run.updated_at,
                        evidence={"has_diff_inspection": has_diff, "has_approved_decision": has_approved},
                        run_id=run.id,
                        task_id=run.task_id,
                        adapter_id=adapter_id,
                        policy_sha256=manifest.effective_policy_sha256,
                        approval_id=run.approval_id,
                        sandbox_profile_id=sandbox_profile_id,
                    )
                )
        if any(_docker_network_enabled(event.payload) for event in events) or _docker_network_enabled(
            manifest.model_dump(mode="json")
        ):
            findings.append(
                _finding(
                    "docker_network_enabled",
                    SecurityFindingSeverity.HIGH,
                    "Docker or test evidence indicates network access was enabled.",
                    run.updated_at,
                    evidence={"run_id": run.id},
                    run_id=run.id,
                    task_id=run.task_id,
                    adapter_id=adapter_id,
                    policy_sha256=manifest.effective_policy_sha256,
                    sandbox_profile_id=sandbox_profile_id,
                )
            )
    return findings


def _secret_metadata_findings(project_root: Path, store: SQLiteStore) -> list[SecurityFinding]:
    findings: list[SecurityFinding] = []
    checked: list[tuple[str, Any, dict[str, Any]]] = []
    for event in store.list_daemon_events(limit=1000):
        checked.append((f"daemon_event:{event.id}", event.metadata, {"created_at": event.created_at}))
    for memory in store.list_memory_records(include_forgotten=True):
        checked.append((f"memory:{memory.id}", {"summary": memory.summary, "lineage": memory.lineage}, {"created_at": memory.updated_at}))
    for run in store.list_runs():
        manifest = store.build_run_manifest(run.id).model_dump(mode="json")
        checked.append((f"manifest:{run.id}", manifest, {"created_at": run.updated_at, "run_id": run.id}))
        for event in store.list_events(run.id):
            checked.append((f"run_event:{event.id}", event.payload, {"created_at": event.created_at, "run_id": run.id}))
        for artifact in store.list_artifacts(run.id):
            checked.append(
                (
                    f"artifact:{artifact.id}",
                    {
                        "kind": artifact.kind,
                        "producer": artifact.producer,
                        "redaction_state": artifact.redaction_state,
                        "metadata": artifact.metadata,
                    },
                    {"created_at": artifact.created_at, "run_id": run.id},
                )
            )
    for location, payload, meta in checked:
        findings_found = scan_text_for_secrets(json.dumps(payload, sort_keys=True, default=str))
        if findings_found:
            findings.append(
                _finding(
                    "secret_like_metadata_output",
                    SecurityFindingSeverity.HIGH,
                    "Secret-like value detected in persisted metadata.",
                    meta["created_at"],
                    evidence={
                        "location": location,
                        "finding_types": sorted({finding.kind for finding in findings_found}),
                    },
                    run_id=meta.get("run_id"),
                )
            )
    return findings


def _finding(
    check_id: str,
    severity: SecurityFindingSeverity,
    message: str,
    created_at,
    *,
    evidence: dict[str, Any],
    run_id: str | None = None,
    task_id: str | None = None,
    attempt_id: str | None = None,
    lease_id: str | None = None,
    adapter_id: str | None = None,
    security_decision_id: str | None = None,
    policy_sha256: str | None = None,
    approval_id: str | None = None,
    sandbox_profile_id: str | None = None,
) -> SecurityFinding:
    stable = {
        "check_id": check_id,
        "run_id": run_id,
        "task_id": task_id,
        "attempt_id": attempt_id,
        "lease_id": lease_id,
        "adapter_id": adapter_id,
        "evidence": sanitize_for_logging(evidence),
    }
    finding_id = "secfind_" + hashlib.sha256(
        json.dumps(stable, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:16]
    return SecurityFinding(
        id=finding_id,
        check_id=check_id,
        status=SecurityFindingStatus.FAIL,
        severity=severity,
        message=str(sanitize_for_logging(message)),
        evidence=sanitize_for_logging(evidence),
        run_id=run_id,
        task_id=task_id,
        attempt_id=attempt_id,
        lease_id=lease_id,
        adapter_id=adapter_id,
        security_decision_id=security_decision_id,
        policy_sha256=policy_sha256,
        approval_id=approval_id,
        sandbox_profile_id=sandbox_profile_id,
        created_at=created_at,
    )


def _adapter_id_for_run(store: SQLiteStore, task_id: str | None) -> str | None:
    if not task_id:
        return None
    try:
        task = store.get_task(task_id)
    except KeyError:
        return None
    value = task.metadata.get("execution_adapter")
    return str(value) if isinstance(value, str) else None


def _approval_by_id(store: ApprovalStore, approval_id: str):
    for approval in store.list():
        if approval.id == approval_id:
            return approval
    return None


def _docker_network_enabled(value: Any) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).lower()
            if normalized in {"network", "allow_network", "network_enabled"} and item is True:
                return True
            if normalized in {"network_mode"} and str(item).lower() not in {"", "none", "disabled", "false"}:
                return True
            if _docker_network_enabled(item):
                return True
    if isinstance(value, list):
        return any(_docker_network_enabled(item) for item in value)
    return False


def _is_codex_adapter(adapter_id: str | None) -> bool:
    return adapter_id in {"read_only_summary", "repo_planning", "codex_isolated_edit"}


def _optional_str(value: Any) -> str | None:
    return str(value) if value is not None else None


def _sandbox_network_check(config: HarnessConfig) -> SafetySmokeCheck:
    ok = config.sandbox.network is False
    return SafetySmokeCheck(
        id="sandbox_network_disabled",
        status="pass" if ok else "fail",
        message="Sandbox network is disabled by default." if ok else "Sandbox network is enabled.",
        evidence={
            "network": config.sandbox.network,
            "workdir": config.sandbox.workdir,
            "timeout_seconds": config.sandbox.timeout_seconds,
        },
    )


def _backend_boundary_check(config: HarnessConfig) -> SafetySmokeCheck:
    evidence = []
    failures = []
    for name, backend in sorted(config.backends.items()):
        descriptor = backend.to_descriptor()
        policy = resolve_backend_effective_policy(descriptor)
        item = {
            "name": name,
            "billing_mode": descriptor.metadata.billing_mode.value,
            "execution_location": descriptor.metadata.execution_location.value,
            "data_boundary": descriptor.metadata.data_boundary.value,
            "allow_network": descriptor.metadata.allow_network,
            "levels": {key: value.value for key, value in policy.levels.items()},
            "constraints": descriptor.constraints,
        }
        evidence.append(item)
        if descriptor.metadata.billing_mode.value == "paid_api" and policy.levels["paid_provider"] != PolicyLevel.FORBIDDEN:
            failures.append(f"{name}: paid_provider is not forbidden")
        if descriptor.metadata.allow_network and policy.levels["external_network"] == PolicyLevel.ALLOWED:
            failures.append(f"{name}: external_network is allowed")
        if "settings" in json.dumps(descriptor.model_dump(mode="json")):
            failures.append(f"{name}: descriptor exposed backend settings")
    return SafetySmokeCheck(
        id="backend_boundaries",
        status="pass" if not failures else "fail",
        message="Backend descriptors preserve boundary policy." if not failures else "; ".join(failures),
        evidence={"backends": sanitize_for_logging(evidence)},
    )


def _artifact_evidence_check(store: SQLiteStore) -> SafetySmokeCheck:
    evidence = []
    failures = []
    for run in store.list_runs():
        for artifact in store.verify_artifacts(run.id):
            evidence.append(
                {
                    "run_id": run.id,
                    "artifact_id": artifact.id,
                    "kind": artifact.kind,
                    "sha256": artifact.sha256,
                    "size_bytes": artifact.size_bytes,
                    "evidence_status": artifact.evidence_status,
                }
            )
            if artifact.evidence_status in {"mismatch", "missing"}:
                failures.append(f"{artifact.id}: {artifact.evidence_status}")
    return SafetySmokeCheck(
        id="artifact_evidence",
        status="pass" if not failures else "fail",
        message="Artifact evidence is present and not drifted." if not failures else "; ".join(failures),
        evidence={"artifacts": evidence},
    )


def _task_queue_non_execution_check(store: SQLiteStore) -> SafetySmokeCheck:
    failures = []
    leased_attempts = []
    for attempt in store.list_task_attempts():
        if attempt.status == TaskStatus.LEASED:
            leased_attempts.append({"id": attempt.id, "task_id": attempt.task_id, "run_id": attempt.run_id})
            if attempt.run_id is not None:
                failures.append(f"{attempt.id}: leased queue attempt has run_id")
    return SafetySmokeCheck(
        id="task_queue_non_execution",
        status="pass" if not failures else "fail",
        message="Leased queue attempts remain non-executing." if not failures else "; ".join(failures),
        evidence={"leased_attempts": leased_attempts},
    )


def _manifest_policy_check(store: SQLiteStore) -> SafetySmokeCheck:
    failures = []
    evidence = []
    for run in store.list_runs():
        manifest = store.build_run_manifest(run.id)
        evidence.append(
            {
                "run_id": run.id,
                "schema_version": manifest.schema_version,
                "effective_policy_sha256": manifest.effective_policy_sha256,
                "backend_descriptor_sha256": manifest.backend_descriptor_sha256,
            }
        )
        if manifest.schema_version != "harness.manifest/v1.1":
            failures.append(f"{run.id}: manifest is not v1.1")
        if manifest.effective_policy is None or not manifest.effective_policy_sha256:
            failures.append(f"{run.id}: missing effective policy evidence")
    return SafetySmokeCheck(
        id="manifest_policy_evidence",
        status="pass" if not failures else "fail",
        message="Run manifests include runtime policy evidence." if not failures else "; ".join(failures),
        evidence={"runs": evidence},
    )
