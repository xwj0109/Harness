from __future__ import annotations

from pathlib import Path
from typing import Any

from harness.governance.reference_repositories import build_reference_repositories_audit
from harness.models import OrchestrationSynthesisReport
from harness.orchestration_efficiency import (
    run_orchestration_efficiency_audit,
    run_orchestration_microbenchmarks,
    summarize_orchestration_efficiency,
    summarize_orchestration_microbenchmarks,
)
from harness.orchestration_readiness import run_orchestration_readiness_audit, summarize_orchestration_readiness
from harness.paths import resolve_project_root


ORCHESTRATION_SYNTHESIS_SUMMARY_SCHEMA_VERSION = "harness.orchestration_synthesis_summary/v1"

ADOPTION_MATRIX: tuple[dict[str, Any], ...] = (
    {
        "pattern": "agent_runtime_and_tool_contracts",
        "reference_inputs": ["microsoft-agent-framework", "openai-agents-python", "openai-agents-js", "google-adk-python"],
        "harness_surfaces": [
            "registered execution adapters",
            "session tool descriptors",
            "model-visible tool exposure policy",
            "typed session_child_task delegation",
            "canonical agent identity contracts",
            "schema compatibility contracts",
        ],
        "readiness_checks": [
            "typed_task_delegation",
            "protocol_and_tool_exposure",
            "budget_limited_delegation",
            "schema_compatibility_contracts",
        ],
        "efficiency_checks": ["adapter_security_complexity_tradeoff", "delegate_budget_efficiency"],
        "decision": "adopt_contracts_not_runtime",
        "rationale": "Use typed tool and delegation contracts while keeping Harness policy, approval, and evidence stores authoritative.",
    },
    {
        "pattern": "external_protocol_interoperability",
        "reference_inputs": ["modelcontextprotocol", "A2A", "microsoft-agent-framework", "openai-agents-python", "google-adk-python"],
        "harness_surfaces": [
            "external protocol compatibility catalog",
            "model provider protocol adapter registry",
            "local server OpenAPI metadata",
            "cached MCP resource boundary",
            "fail-closed remote protocol descriptors",
        ],
        "readiness_checks": ["external_protocol_compatibility", "protocol_and_tool_exposure"],
        "efficiency_checks": ["adapter_security_complexity_tradeoff", "delegate_budget_efficiency"],
        "decision": "adopt_compatibility_catalog_not_remote_execution",
        "rationale": "Track MCP, OpenAPI, A2A, and gRPC compatibility explicitly while leaving remote execution disabled until authority, identity, replay, and trace boundaries are implemented.",
    },
    {
        "pattern": "durable_workflow_and_state_graph",
        "reference_inputs": ["microsoft-agent-framework", "temporal-sdk-python", "langgraph", "dapr-agents"],
        "harness_surfaces": [
            "objectives",
            "tasks",
            "task dependencies",
            "leases",
            "append-only objective evidence",
            "supervisor checkpoints",
            "workflow coordination catalog",
            "orchestration replay drift audit",
            "orchestration scenario conformance catalog",
        ],
        "readiness_checks": [
            "durable_supervisor_state",
            "workflow_coordination_contracts",
            "bounded_parallel_scheduler",
            "append_only_objective_evidence",
            "supervisor_checkpoints",
            "replay_drift_detection",
            "orchestration_scenario_conformance",
        ],
        "efficiency_checks": ["bounded_critical_path_scheduler", "replay_retry_idempotency"],
        "decision": "adopt_durable_metadata_and_replay_discipline",
        "rationale": "Prefer inspectable local state, deterministic scheduling, and append-only evidence over opaque in-memory orchestration.",
    },
    {
        "pattern": "observability_and_progress",
        "reference_inputs": ["opentelemetry-semantic-conventions", "microsoft-agent-framework", "temporal-sdk-python"],
        "harness_surfaces": [
            "progress projections",
            "run trace export",
            "objective trace export",
            "orchestration microbenchmark summaries",
        ],
        "readiness_checks": ["progress_observability", "otel_trace_export"],
        "efficiency_checks": ["evidence_trace_projection_cost", "microbenchmark_contracts"],
        "decision": "adopt_metadata_traces_without_bodies",
        "rationale": "Expose operator-visible progress and trace metadata without reading artifact or transcript bodies.",
    },
    {
        "pattern": "policy_boundary_and_applyback",
        "reference_inputs": ["microsoft-agent-framework", "modelcontextprotocol", "openai-agents-python", "temporal-sdk-python"],
        "harness_surfaces": [
            "approvals",
            "runtime controls",
            "adapter breakers",
            "apply-back governance gates",
            "pending chat action recovery",
            "agentic security controls",
            "orchestration scenario conformance",
        ],
        "readiness_checks": [
            "runtime_controls_and_breakers",
            "applyback_governance",
            "pending_chat_action_recovery",
            "agentic_security_controls",
            "orchestration_scenario_conformance",
        ],
        "efficiency_checks": ["adapter_security_complexity_tradeoff", "replay_retry_idempotency"],
        "decision": "adopt_explicit_authority_boundaries",
        "rationale": "Keep proposal, approval, execution, and apply-back authority separated to reduce hidden side effects.",
    },
    {
        "pattern": "sandbox_and_low_level_isolation",
        "reference_inputs": ["bubblewrap", "firecracker", "gvisor", "kata-containers", "nsjail", "runc", "containerd"],
        "harness_surfaces": [
            "sandbox profiles",
            "delegate filesystem/network budgets",
            "adapter metadata",
            "live benchmark permits",
        ],
        "readiness_checks": ["sandboxed_registered_adapters", "budget_limited_delegation"],
        "efficiency_checks": ["adapter_security_complexity_tradeoff", "delegate_budget_efficiency", "live_benchmark_permits"],
        "decision": "adopt_boundary_model_not_runtime_dependency",
        "rationale": "Use isolation projects as threat-model references while keeping runtime support explicit, declared, and approval-gated.",
    },
)


DELIBERATE_NON_ADOPTIONS: tuple[dict[str, Any], ...] = (
    {
        "id": "no_reference_source_import",
        "decision": "Do not import, vendor, or execute pulled reference source as part of Harness orchestration.",
        "reason": "Reference repositories are useful design evidence, but runtime authority must stay in Harness-reviewed code.",
        "evidence": ["reference_repository_hygiene", "orchestration_readiness.safety.reference_code_imported=false"],
    },
    {
        "id": "no_ambient_tool_or_provider_execution",
        "decision": "Do not adopt ambient framework defaults that can call tools or providers without Harness approval state.",
        "reason": "Security depends on explicit model-visible tool policy, provider/data-boundary approvals, and adapter dispatch records.",
        "evidence": ["protocol_and_tool_exposure", "adapter_security_complexity_tradeoff"],
    },
    {
        "id": "no_fail_open_remote_protocol_execution",
        "decision": "Do not enable MCP tool execution, external OpenAPI imports, A2A agents, or gRPC remote tools as generic runtime paths.",
        "reason": "Remote protocol compatibility is useful only when identity, host policy, permission evidence, retry semantics, and trace propagation are explicit.",
        "evidence": ["external_protocol_compatibility", "harness.external_protocol_catalog/v1"],
    },
    {
        "id": "no_hidden_applyback",
        "decision": "Do not let delegated or leased work mutate the active repository directly.",
        "reason": "Active repository changes remain behind inspected-diff apply-back governance.",
        "evidence": ["applyback_governance", "delegate_budget_efficiency"],
    },
    {
        "id": "no_release_blocking_live_benchmarks",
        "decision": "Do not turn sandbox/provider live benchmarks into automatic release gates.",
        "reason": "Provider contention and sandbox startup require explicit operator approval and are too environment-sensitive for passive gates.",
        "evidence": ["live_benchmark_permits", "orchestration_microbenchmarks.skipped_explicit_live_required"],
    },
    {
        "id": "no_replay_side_effect_execution",
        "decision": "Do not replay captured orchestration logs by re-executing adapters, tools, providers, or repository mutations.",
        "reason": "Replay is for semantic drift detection; side effects remain behind fresh leases, approvals, and runtime policy.",
        "evidence": ["replay_drift_detection", "harness.orchestration_replay_audit/v1"],
    },
)


def run_orchestration_synthesis(
    project_root: Path,
    *,
    reference_root: Path | None = None,
    include_references: bool = True,
) -> OrchestrationSynthesisReport:
    project_root = resolve_project_root(project_root)
    readiness = run_orchestration_readiness_audit(
        project_root,
        reference_root=reference_root,
        include_references=include_references,
    )
    efficiency = run_orchestration_efficiency_audit(project_root)
    microbenchmarks = run_orchestration_microbenchmarks(project_root, samples=1)
    reference_audit_payload: dict[str, Any] | None = None
    if include_references:
        reference_audit_payload = build_reference_repositories_audit(
            project_root,
            reference_root=reference_root,
        ).to_dict()

    readiness_summary = summarize_orchestration_readiness(readiness)
    efficiency_summary = summarize_orchestration_efficiency(efficiency)
    microbenchmark_summary = summarize_orchestration_microbenchmarks(microbenchmarks)
    adopted = [
        _adoption_projection(row, readiness_statuses=_status_map(readiness.checks), efficiency_statuses=_status_map(efficiency.checks))
        for row in ADOPTION_MATRIX
    ]
    reference_summary = (reference_audit_payload or {}).get("summary", {})
    ok = readiness.ok and efficiency.ok and microbenchmarks.ok
    return OrchestrationSynthesisReport(
        ok=ok,
        project_root=project_root,
        reference_root=readiness.reference_root,
        summary={
            "status": "pass" if ok else "fail",
            "readiness_status": readiness_summary["status"],
            "efficiency_status": efficiency_summary["status"],
            "microbenchmark_status": microbenchmark_summary["status"],
            "reference_status": "disabled"
            if reference_audit_payload is None
            else "pass"
            if reference_audit_payload.get("ok") is True
            else "warning",
            "adopted_pattern_count": len(adopted),
            "deliberate_non_adoption_count": len(DELIBERATE_NON_ADOPTIONS),
            "reference_repository_count": reference_summary.get("repository_count", 0),
            "missing_expected_repository_count": reference_summary.get("missing_expected_repository_count", 0),
            "missing_required_reference_pattern_count": reference_summary.get("missing_required_reference_pattern_count", 0),
            "lfs_unmaterialized_file_count": reference_summary.get("lfs_unmaterialized_file_count", 0),
            "readiness": dict(readiness.summary),
            "efficiency": dict(efficiency.summary),
            "microbenchmarks": dict(microbenchmarks.summary),
        },
        source_reports={
            "readiness": readiness_summary,
            "efficiency": efficiency_summary,
            "microbenchmarks": microbenchmark_summary,
            "reference_repositories": _reference_source_projection(reference_audit_payload),
        },
        adopted_reference_patterns=adopted,
        deliberate_non_adoptions=[dict(item) for item in DELIBERATE_NON_ADOPTIONS],
        security_complexity_posture=_security_complexity_posture(
            readiness_summary,
            efficiency_summary,
            microbenchmark_summary,
            reference_audit_payload,
        ),
        operator_commands=[
            f"harness orchestration audit --project {project_root} --reference-root {readiness.reference_root or '<reference-root>'} --output json",
            f"harness evals run --suite orchestration-efficiency --project {project_root} --output json",
            f"harness evals run --suite orchestration-microbenchmarks --project {project_root} --output json",
            f"harness governance references-audit --project {project_root} --root {readiness.reference_root or '<reference-root>'} --output json",
        ],
        safety=_safety_projection(readiness.safety, efficiency.safety, microbenchmarks.safety, include_references=include_references),
    )


def summarize_orchestration_synthesis(report: OrchestrationSynthesisReport) -> dict[str, Any]:
    source_report_statuses = _source_report_statuses(report.source_reports)
    return {
        "schema_version": ORCHESTRATION_SYNTHESIS_SUMMARY_SCHEMA_VERSION,
        "ok": report.ok,
        "status": str(report.summary.get("status") or ("pass" if report.ok else "fail")),
        "summary": dict(report.summary),
        "source_report_statuses": source_report_statuses,
        "failing_source_report_ids": [
            source_id for source_id, status in source_report_statuses.items() if status == "fail"
        ],
        "warning_source_report_ids": [
            source_id for source_id, status in source_report_statuses.items() if status == "warning"
        ],
        "adopted_reference_pattern_ids": [
            str(item.get("pattern")) for item in report.adopted_reference_patterns if item.get("pattern")
        ],
        "adopted_reference_pattern_count": len(report.adopted_reference_patterns),
        "deliberate_non_adoption_ids": [
            str(item.get("id")) for item in report.deliberate_non_adoptions if item.get("id")
        ],
        "deliberate_non_adoption_count": len(report.deliberate_non_adoptions),
        "security_complexity_posture": dict(report.security_complexity_posture),
        "safety": dict(report.safety),
        "reference_root": str(report.reference_root) if report.reference_root is not None else None,
        "next_action": f"harness evals run --suite orchestration-synthesis --project {report.project_root} --output json",
        "command": f"harness evals run --suite orchestration-synthesis --project {report.project_root} --output json",
    }


def summarize_orchestration_synthesis_sources(
    project_root: Path,
    *,
    readiness_summary: dict[str, Any],
    efficiency_summary: dict[str, Any],
    microbenchmark_summary: dict[str, Any],
    include_references: bool = False,
) -> dict[str, Any]:
    project_root = resolve_project_root(project_root)
    source_report_statuses = {
        "readiness": str(readiness_summary.get("status") or "unknown"),
        "efficiency": str(efficiency_summary.get("status") or "unknown"),
        "microbenchmarks": str(microbenchmark_summary.get("status") or "unknown"),
    }
    failing = [source_id for source_id, status in source_report_statuses.items() if status == "fail"]
    warnings = [source_id for source_id, status in source_report_statuses.items() if status == "warning"]
    status = "fail" if failing else "warning" if warnings else "pass"
    safety = _safety_projection(
        readiness_summary.get("safety") or {},
        efficiency_summary.get("safety") or {},
        microbenchmark_summary.get("safety") or {},
        include_references=include_references,
    )
    posture = _security_complexity_posture(
        readiness_summary,
        efficiency_summary,
        microbenchmark_summary,
        None,
    )
    summary = {
        "status": status,
        "readiness_status": source_report_statuses["readiness"],
        "efficiency_status": source_report_statuses["efficiency"],
        "microbenchmark_status": source_report_statuses["microbenchmarks"],
        "reference_status": "disabled" if not include_references else "summary_only",
        "adopted_pattern_count": len(ADOPTION_MATRIX),
        "deliberate_non_adoption_count": len(DELIBERATE_NON_ADOPTIONS),
        "readiness": dict(readiness_summary.get("summary") or {}),
        "efficiency": dict(efficiency_summary.get("summary") or {}),
        "microbenchmarks": dict(microbenchmark_summary.get("summary") or {}),
    }
    return {
        "schema_version": ORCHESTRATION_SYNTHESIS_SUMMARY_SCHEMA_VERSION,
        "ok": not failing,
        "status": status,
        "summary": summary,
        "source_report_statuses": source_report_statuses,
        "failing_source_report_ids": failing,
        "warning_source_report_ids": warnings,
        "adopted_reference_pattern_ids": [str(item["pattern"]) for item in ADOPTION_MATRIX],
        "adopted_reference_pattern_count": len(ADOPTION_MATRIX),
        "deliberate_non_adoption_ids": [str(item["id"]) for item in DELIBERATE_NON_ADOPTIONS],
        "deliberate_non_adoption_count": len(DELIBERATE_NON_ADOPTIONS),
        "security_complexity_posture": posture,
        "safety": safety,
        "reference_root": None,
        "next_action": f"harness evals run --suite orchestration-synthesis --project {project_root} --output json",
        "command": f"harness evals run --suite orchestration-synthesis --project {project_root} --output json",
    }


def _adoption_projection(
    row: dict[str, Any],
    *,
    readiness_statuses: dict[str, str],
    efficiency_statuses: dict[str, str],
) -> dict[str, Any]:
    readiness = {check_id: readiness_statuses.get(check_id, "missing") for check_id in row["readiness_checks"]}
    efficiency = {check_id: efficiency_statuses.get(check_id, "missing") for check_id in row["efficiency_checks"]}
    statuses = [*readiness.values(), *efficiency.values()]
    status = "fail" if "fail" in statuses or "missing" in statuses else "warning" if "warning" in statuses else "pass"
    return {
        **row,
        "status": status,
        "readiness_statuses": readiness,
        "efficiency_statuses": efficiency,
        "reference_contents_included": False,
        "reference_code_imported": False,
        "runtime_authority_granted": False,
    }


def _status_map(items: list[Any]) -> dict[str, str]:
    return {str(item.id): str(item.status) for item in items}


def _reference_source_projection(payload: dict[str, Any] | None) -> dict[str, Any]:
    if payload is None:
        return {
            "schema_version": "harness.reference_repositories_audit/v1",
            "included": False,
            "ok": None,
            "summary": {},
            "authority": {
                "contents_included": False,
                "model_context_allowed": False,
                "execution_allowed": False,
                "mutation_allowed": False,
                "permission_granting": False,
            },
        }
    return {
        "schema_version": payload.get("schema_version"),
        "included": True,
        "ok": payload.get("ok"),
        "reference_root": payload.get("reference_root"),
        "summary": payload.get("summary") or {},
        "authority": payload.get("authority") or {},
        "expected_repository_names": payload.get("expected_repository_names") or [],
        "missing_expected_repository_names": payload.get("missing_expected_repository_names") or [],
        "missing_required_reference_patterns": payload.get("missing_required_reference_patterns") or [],
        "warnings": payload.get("warnings") or [],
    }


def _source_report_statuses(source_reports: dict[str, Any]) -> dict[str, str]:
    statuses: dict[str, str] = {}
    for source_id, source in source_reports.items():
        if not isinstance(source, dict):
            continue
        status = source.get("status")
        if status is not None:
            statuses[source_id] = str(status)
    return statuses


def _security_complexity_posture(
    readiness_summary: dict[str, Any],
    efficiency_summary: dict[str, Any],
    microbenchmark_summary: dict[str, Any],
    reference_audit_payload: dict[str, Any] | None,
) -> dict[str, Any]:
    reference_ok = reference_audit_payload is None or reference_audit_payload.get("ok") is True
    release_blocking_failures = [
        name
        for name, summary in (
            ("readiness", readiness_summary),
            ("efficiency", efficiency_summary),
            ("microbenchmarks", microbenchmark_summary),
        )
        if summary.get("status") == "fail"
    ]
    return {
        "posture": "balanced" if not release_blocking_failures and reference_ok else "needs_review",
        "release_blocking_failures": release_blocking_failures,
        "reference_hygiene_ok": reference_ok,
        "live_benchmarks_automatic": False,
        "live_benchmarks_release_blocking": False,
        "security_tradeoff": "prefer explicit contracts, local evidence, bounded delegation, and fail-closed policy over framework breadth",
        "complexity_tradeoff": "reuse reference patterns as design constraints while avoiding runtime coupling to external frameworks",
    }


def _safety_projection(*sources: dict[str, bool], include_references: bool) -> dict[str, bool]:
    return {
        "read_only": all(source.get("read_only") is True for source in sources),
        "reference_metadata_included": include_references,
        "reference_code_imported": any(source.get("reference_code_imported") is True for source in sources),
        "reference_contents_included": any(source.get("reference_contents_included") is True for source in sources),
        "provider_called": any(source.get("provider_called") is True for source in sources),
        "network_called": any(source.get("network_called") is True for source in sources),
        "adapter_execution_started": any(source.get("adapter_execution_started") is True for source in sources),
        "filesystem_modified": any(source.get("filesystem_modified") is True for source in sources),
        "permission_granting": any(source.get("permission_granting") is True for source in sources),
        "artifact_bodies_read": any(source.get("artifact_bodies_read") is True for source in sources),
        "live_benchmark_execution_allowed": False,
        "mutation_allowed": False,
        "model_context_allowed": False,
    }
