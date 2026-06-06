from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from harness.config import HARNESS_DIR
from harness.context_policy import decide_context_transmission
from harness.execution import list_execution_adapter_descriptors
from harness.external_protocols import build_external_protocol_catalog
from harness.models import ToolReplayPolicy
from harness.objective_checkpoints import evaluate_objective_checkpoint_gate
from harness.orchestration_efficiency import (
    _LiveBenchmarkPermitSpec,
    _live_benchmark_permit_projection,
    _live_benchmark_permits_check,
    _live_benchmark_specs,
)
from harness.orchestration_replay import replay_objective_event_log, run_orchestration_replay_audit
from harness.paths import resolve_project_root
from harness.security import sanitize_for_logging


ORCHESTRATION_SCENARIO_CATALOG_SCHEMA_VERSION = "harness.orchestration_scenario_catalog/v1"
ORCHESTRATION_SCENARIO_CASE_SCHEMA_VERSION = "harness.orchestration_scenario_case/v1"
ORCHESTRATION_SCENARIO_SUMMARY_SCHEMA_VERSION = "harness.orchestration_scenario_summary/v1"

ScenarioStatus = Literal["pass", "warning", "fail"]
ScenarioLayer = Literal["unit", "contract", "replay", "scenario", "security", "benchmark"]

REQUIRED_SCENARIO_CASE_IDS: tuple[str, ...] = (
    "duplicate_dispatch_redelivery",
    "slow_branch_barrier",
    "approval_reject_pause",
    "checkpoint_reject_stop",
    "missing_terminal_event",
    "unsafe_memory_to_hosted_model",
    "remote_protocol_fail_closed",
    "retry_requires_idempotency",
    "live_benchmark_explicit_permit",
)

REQUIRED_SCENARIO_LAYERS: tuple[ScenarioLayer, ...] = (
    "unit",
    "contract",
    "replay",
    "scenario",
    "security",
    "benchmark",
)


class OrchestrationScenarioCase(BaseModel):
    schema_version: str = ORCHESTRATION_SCENARIO_CASE_SCHEMA_VERSION
    id: str
    title: str
    status: ScenarioStatus
    layer: ScenarioLayer
    failure_mode: str
    execution_mode: str = "passive_probe"
    source_surfaces: list[str] = Field(default_factory=list)
    reference_patterns: list[str] = Field(default_factory=list)
    expected_signals: list[str] = Field(default_factory=list)
    detected_signals: list[str] = Field(default_factory=list)
    evidence: dict[str, Any] = Field(default_factory=dict)
    gaps: list[str] = Field(default_factory=list)
    next_actions: list[str] = Field(default_factory=list)


class OrchestrationScenarioCatalog(BaseModel):
    schema_version: str = ORCHESTRATION_SCENARIO_CATALOG_SCHEMA_VERSION
    ok: bool
    project_root: Path
    initialized: bool
    required_case_ids: list[str]
    required_layers: list[str]
    cases: list[OrchestrationScenarioCase]
    summary: dict[str, int]
    safety: dict[str, bool]


def build_orchestration_scenario_catalog(project_root: Path) -> OrchestrationScenarioCatalog:
    """Return the passive orchestration failure-mode scenario matrix."""

    root = resolve_project_root(project_root)
    initialized = (root / HARNESS_DIR / "harness.sqlite").exists()
    cases = _scenario_cases(root)
    case_ids = {case.id for case in cases}
    layers = {case.layer for case in cases if case.status != "fail"}
    missing_case_ids = [case_id for case_id in REQUIRED_SCENARIO_CASE_IDS if case_id not in case_ids]
    missing_layers = [layer for layer in REQUIRED_SCENARIO_LAYERS if layer not in layers]
    failing_case_ids = [case.id for case in cases if case.status == "fail"]
    safety = _safety_flags()
    ok = not missing_case_ids and not missing_layers and not failing_case_ids and _safety_is_passive(safety)
    return OrchestrationScenarioCatalog(
        ok=ok,
        project_root=root,
        initialized=initialized,
        required_case_ids=list(REQUIRED_SCENARIO_CASE_IDS),
        required_layers=list(REQUIRED_SCENARIO_LAYERS),
        cases=cases,
        summary={
            "case_count": len(cases),
            "required_case_count": len(REQUIRED_SCENARIO_CASE_IDS),
            "required_case_present_count": len(REQUIRED_SCENARIO_CASE_IDS) - len(missing_case_ids),
            "required_layer_count": len(REQUIRED_SCENARIO_LAYERS),
            "required_layer_present_count": len(REQUIRED_SCENARIO_LAYERS) - len(missing_layers),
            "pass": sum(1 for case in cases if case.status == "pass"),
            "warning": sum(1 for case in cases if case.status == "warning"),
            "fail": len(failing_case_ids),
            "missing_required_case_count": len(missing_case_ids),
            "missing_required_layer_count": len(missing_layers),
        },
        safety=safety,
    )


def summarize_orchestration_scenarios(catalog: OrchestrationScenarioCatalog) -> dict[str, Any]:
    failing = [case.id for case in catalog.cases if case.status == "fail"]
    warnings = [case.id for case in catalog.cases if case.status == "warning"]
    status = "fail" if failing else "warning" if warnings else "pass"
    return {
        "schema_version": ORCHESTRATION_SCENARIO_SUMMARY_SCHEMA_VERSION,
        "ok": catalog.ok,
        "status": status,
        "initialized": catalog.initialized,
        "summary": dict(catalog.summary),
        "failing_case_ids": failing,
        "warning_case_ids": warnings,
        "case_ids": [case.id for case in catalog.cases],
        "layers": sorted({case.layer for case in catalog.cases}),
        "safety": dict(catalog.safety),
        "command": f"harness orchestration scenarios --project {catalog.project_root} --output json",
    }


def _scenario_cases(project_root: Path) -> list[OrchestrationScenarioCase]:
    replay_audit = run_orchestration_replay_audit(project_root)
    replay_cases = {case.id: case for case in replay_audit.cases}
    return [
        _replay_scenario_case(
            case_id="duplicate_dispatch_redelivery",
            title="Duplicate dispatch and redelivery detection",
            replay_case=replay_cases.get("synthetic_duplicate_dispatch_detection"),
            layer="replay",
            failure_mode="A side-effecting task is dispatched twice with the same task, lease, and run identity.",
            expected_signals=["duplicate_side_effect_dispatch"],
            reference_patterns=["temporal", "dapr", "microsoft_agent_framework"],
            source_surfaces=["orchestration replay reducer", "objective evidence event ids", "adapter dispatch evidence"],
        ),
        _replay_scenario_case(
            case_id="slow_branch_barrier",
            title="Slow branch fan-in barrier detection",
            replay_case=replay_cases.get("synthetic_slow_branch_barrier_detection"),
            layer="scenario",
            failure_mode="A batch completes before every selected branch reaches terminal evidence.",
            expected_signals=["batch_completed_missing_terminal_task"],
            reference_patterns=["langgraph", "temporal", "microsoft_agent_framework", "kairos", "lamas"],
            source_surfaces=["objective batch plans", "batch_started evidence", "batch_completed evidence"],
        ),
        _replay_scenario_case(
            case_id="approval_reject_pause",
            title="Approval reject pause enforcement",
            replay_case=replay_cases.get("synthetic_approval_reject_detection"),
            layer="scenario",
            failure_mode="Work is dispatched after an approval or autonomy boundary blocks progress.",
            expected_signals=["dispatch_after_blocking_event"],
            reference_patterns=["openai_agents", "microsoft_agent_framework", "temporal"],
            source_surfaces=["autonomy stop events", "approval records", "adapter dispatch evidence"],
        ),
        _checkpoint_reject_scenario_case(),
        _replay_scenario_case(
            case_id="missing_terminal_event",
            title="Missing terminal event detection",
            replay_case=replay_cases.get("synthetic_missing_terminal_detection"),
            layer="replay",
            failure_mode="An objective event log starts work but never records a terminal stopped event.",
            expected_signals=["missing_stopped_event"],
            reference_patterns=["temporal", "opentelemetry", "langgraph"],
            source_surfaces=["objective JSONL evidence", "orchestration replay reducer"],
        ),
        _memory_security_scenario_case(),
        _remote_protocol_scenario_case(project_root),
        _retry_idempotency_scenario_case(),
        _live_benchmark_permit_scenario_case(),
    ]


def _replay_scenario_case(
    *,
    case_id: str,
    title: str,
    replay_case: Any | None,
    layer: ScenarioLayer,
    failure_mode: str,
    expected_signals: list[str],
    reference_patterns: list[str],
    source_surfaces: list[str],
) -> OrchestrationScenarioCase:
    detected = list(getattr(replay_case, "detected_issue_codes", []) or [])
    missing = _missing_expected(expected_signals, detected)
    return OrchestrationScenarioCase(
        id=case_id,
        title=title,
        status="pass" if replay_case is not None and not missing else "fail",
        layer=layer,
        failure_mode=failure_mode,
        source_surfaces=source_surfaces,
        reference_patterns=reference_patterns,
        expected_signals=expected_signals,
        detected_signals=detected,
        evidence={
            "replay_case_id": getattr(replay_case, "id", None),
            "replay_case_status": getattr(replay_case, "status", None),
            "event_count": getattr(replay_case, "event_count", 0),
            "replay_summary": getattr(replay_case, "replay_summary", {}),
            "missing_expected_signals": missing,
        },
        gaps=[] if replay_case is not None and not missing else [f"{case_id}: scenario signal coverage drifted."],
        next_actions=[]
        if replay_case is not None and not missing
        else ["Inspect orchestration replay synthetic cases before changing failure-mode semantics."],
    )


def _checkpoint_reject_scenario_case() -> OrchestrationScenarioCase:
    replay = replay_objective_event_log(_checkpoint_reject_stop_events())
    checkpoint_gate_source = inspect.getsource(evaluate_objective_checkpoint_gate)
    detected = list(replay.get("issue_codes") or [])
    if "rejected_checkpoint_ids" in checkpoint_gate_source and "rejected" in checkpoint_gate_source:
        detected.append("checkpoint_gate_rejected_branch_present")
    expected = ["checkpoint_blocked_stop_reason_mismatch", "checkpoint_gate_rejected_branch_present"]
    missing = _missing_expected(expected, detected)
    return OrchestrationScenarioCase(
        id="checkpoint_reject_stop",
        title="Checkpoint reject stop enforcement",
        status="pass" if not missing else "fail",
        layer="unit",
        failure_mode="A required checkpoint is rejected or blocked, but objective progress is summarized as complete.",
        source_surfaces=["objective checkpoint gate", "orchestration replay reducer", "objective checkpoint evidence"],
        reference_patterns=["temporal", "microsoft_agent_framework", "langgraph"],
        expected_signals=expected,
        detected_signals=detected,
        evidence={
            "replay_summary": _compact_replay_summary(replay),
            "checkpoint_gate_handles_rejected_ids": "rejected_checkpoint_ids" in checkpoint_gate_source,
            "checkpoint_gate_source_read": True,
            "missing_expected_signals": missing,
        },
        gaps=[] if not missing else ["Checkpoint rejection or stop-reason mismatch coverage drifted."],
        next_actions=[] if not missing else ["Inspect objective checkpoint gate and replay stop-reason checks."],
    )


def _memory_security_scenario_case() -> OrchestrationScenarioCase:
    decision = decide_context_transmission(
        "hosted_model",
        source_kind="memory_record",
        trust_level="memory",
    )
    detected = [decision.code, *decision.warnings]
    if decision.provider_call_allowed is False:
        detected.append("provider_call_not_allowed")
    if decision.permission_granting is False:
        detected.append("permission_not_granting")
    expected = [
        "context_hosted_transmission_denied",
        "memory_not_authority",
        "provider_call_not_allowed",
        "permission_not_granting",
    ]
    missing = _missing_expected(expected, detected)
    return OrchestrationScenarioCase(
        id="unsafe_memory_to_hosted_model",
        title="Unsafe memory-to-hosted-model propagation denial",
        status="pass" if decision.allowed is False and not missing else "fail",
        layer="security",
        failure_mode="Long-term memory is treated as authority or propagated to a hosted model without a governed path.",
        source_surfaces=["context policy", "memory state class", "agentic security controls"],
        reference_patterns=["owasp_agentic", "openai_agents", "microsoft_agent_framework"],
        expected_signals=expected,
        detected_signals=detected,
        evidence={
            "decision": decision.to_payload(),
            "missing_expected_signals": missing,
        },
        gaps=[] if decision.allowed is False and not missing else ["Hosted memory context policy no longer fails closed."],
        next_actions=[] if decision.allowed is False and not missing else ["Restore memory_not_authority hosted context denial."],
    )


def _remote_protocol_scenario_case(project_root: Path) -> OrchestrationScenarioCase:
    catalog = build_external_protocol_catalog(project_root)
    remote_protocols = [
        descriptor
        for descriptor in catalog.protocols
        if descriptor.category in {"extension", "agent_to_agent", "rpc"} or descriptor.boundary_kind == "external_network"
    ]
    risky = [
        descriptor.id
        for descriptor in remote_protocols
        if descriptor.default_model_visible
        or descriptor.authority.process_start_allowed
        or descriptor.authority.network_allowed
        or descriptor.authority.agent_execution_allowed
        or descriptor.authority.model_context_allowed
        or descriptor.authority.credential_access_allowed
        or descriptor.authority.permission_granting
        or (
            descriptor.status == "fail_closed"
            and (descriptor.runtime_enabled or descriptor.authority.tool_execution_allowed)
        )
    ]
    fail_closed_ids = [descriptor.id for descriptor in remote_protocols if descriptor.status == "fail_closed"]
    detected: list[str] = []
    if fail_closed_ids:
        detected.append("fail_closed_remote_protocols_present")
    if not risky:
        detected.append("no_risky_remote_protocols")
    if all(descriptor.default_model_visible is False for descriptor in remote_protocols):
        detected.append("remote_protocols_not_model_visible")
    expected = ["fail_closed_remote_protocols_present", "no_risky_remote_protocols", "remote_protocols_not_model_visible"]
    missing = _missing_expected(expected, detected)
    return OrchestrationScenarioCase(
        id="remote_protocol_fail_closed",
        title="Remote protocol fail-closed boundary",
        status="pass" if catalog.ok and not missing else "fail",
        layer="contract",
        failure_mode="MCP, A2A, external OpenAPI, or gRPC surfaces become model-visible or executable by default.",
        source_surfaces=["external protocol catalog", "session tool exposure policy", "local server OpenAPI"],
        reference_patterns=["modelcontextprotocol", "A2A", "grpc", "microsoft_agent_framework"],
        expected_signals=expected,
        detected_signals=detected,
        evidence={
            "catalog_schema_version": catalog.schema_version,
            "remote_protocol_ids": [descriptor.id for descriptor in remote_protocols],
            "fail_closed_protocol_ids": fail_closed_ids,
            "risky_protocol_ids": risky,
            "missing_expected_signals": missing,
        },
        gaps=[] if catalog.ok and not missing else ["One or more remote protocol descriptors are risky by default."],
        next_actions=[] if catalog.ok and not missing else ["Restore fail-closed protocol descriptors before enabling remote execution."],
    )


def _retry_idempotency_scenario_case() -> OrchestrationScenarioCase:
    descriptors = list_execution_adapter_descriptors()
    safe_replay_policies = {ToolReplayPolicy.SAFE, ToolReplayPolicy.IDEMPOTENT_WITH_KEY}
    auto_allowed_unsafe = [
        descriptor.id
        for descriptor in descriptors
        if descriptor.autonomy_default == "auto_allowed" and descriptor.replay_policy not in safe_replay_policies
    ]
    not_replayable_not_forbidden = [
        descriptor.id
        for descriptor in descriptors
        if descriptor.replay_policy == ToolReplayPolicy.NOT_REPLAYABLE and descriptor.autonomy_default != "forbidden"
    ]
    fresh_approval_gated = [
        descriptor.id
        for descriptor in descriptors
        if descriptor.replay_policy == ToolReplayPolicy.REQUIRES_FRESH_APPROVAL
        and descriptor.autonomy_default == "approval_required"
    ]
    detected: list[str] = []
    if not auto_allowed_unsafe:
        detected.append("auto_allowed_replay_safe")
    if not not_replayable_not_forbidden:
        detected.append("not_replayable_forbidden")
    if fresh_approval_gated:
        detected.append("fresh_approval_side_effects_gated")
    expected = ["auto_allowed_replay_safe", "not_replayable_forbidden", "fresh_approval_side_effects_gated"]
    missing = _missing_expected(expected, detected)
    return OrchestrationScenarioCase(
        id="retry_requires_idempotency",
        title="Retry and replay idempotency contract",
        status="pass" if not missing else "fail",
        layer="contract",
        failure_mode="Retries or redeliveries are allowed for side-effecting adapters without idempotency keys or fresh approval.",
        source_surfaces=["execution adapter descriptors", "delegate budgets", "replay retry idempotency audit"],
        reference_patterns=["temporal", "dapr", "containerd", "microsoft_agent_framework"],
        expected_signals=expected,
        detected_signals=detected,
        evidence={
            "adapter_count": len(descriptors),
            "auto_allowed_unsafe_replay_adapter_ids": auto_allowed_unsafe,
            "not_replayable_not_forbidden_adapter_ids": not_replayable_not_forbidden,
            "fresh_approval_adapter_ids": fresh_approval_gated,
            "adapter_replay_policies": [
                {
                    "id": descriptor.id,
                    "replay_policy": descriptor.replay_policy.value,
                    "autonomy_default": descriptor.autonomy_default,
                }
                for descriptor in descriptors
            ],
            "missing_expected_signals": missing,
        },
        gaps=[] if not missing else ["Adapter retry/replay policy permits duplicate side-effect risk."],
        next_actions=[] if not missing else ["Require idempotency keys, fresh approval, or forbidden autonomy for unsafe adapters."],
    )


def _live_benchmark_permit_scenario_case() -> OrchestrationScenarioCase:
    permit_source = inspect.getsource(_live_benchmark_permit_projection)
    check_source = inspect.getsource(_live_benchmark_permits_check)
    specs_source = inspect.getsource(_live_benchmark_specs)
    detected: list[str] = []
    if "approval_required" in permit_source and "required_approval" in permit_source:
        detected.append("live_benchmark_explicit_approval")
    if '"automated_execution_allowed": False' in permit_source:
        detected.append("automated_live_execution_disabled")
    if "release_blocking: bool = False" in inspect.getsource(_LiveBenchmarkPermitSpec):
        detected.append("live_benchmark_not_release_blocking_default")
    if "release_blocking_count" in check_source and "approval_ready_count" in check_source:
        detected.append("live_benchmark_release_gate_reports_permits")
    expected = [
        "live_benchmark_explicit_approval",
        "automated_live_execution_disabled",
        "live_benchmark_not_release_blocking_default",
        "live_benchmark_release_gate_reports_permits",
    ]
    missing = _missing_expected(expected, detected)
    return OrchestrationScenarioCase(
        id="live_benchmark_explicit_permit",
        title="Live benchmark explicit permit contract",
        status="pass" if not missing else "fail",
        layer="benchmark",
        failure_mode="Provider, sandbox, or live contention benchmarks become automatic release gates or execute without approval.",
        source_surfaces=["orchestration efficiency live benchmark permits", "microbenchmark contracts", "approval store"],
        reference_patterns=["microsoft_agent_framework", "temporal", "openai_agents", "google_adk"],
        expected_signals=expected,
        detected_signals=detected,
        evidence={
            "permit_projection_source_read": True,
            "permit_check_source_read": True,
            "live_benchmark_spec_source_read": bool(specs_source),
            "approval_store_instantiated": False,
            "missing_expected_signals": missing,
        },
        gaps=[] if not missing else ["Live benchmark permit contract no longer proves approval-only execution."],
        next_actions=[] if not missing else ["Restore explicit approval and non-automatic live benchmark permit semantics."],
    )


def _checkpoint_reject_stop_events() -> list[dict[str, Any]]:
    return [
        _objective_event(1, "started"),
        _objective_event(
            2,
            "checkpoint_blocked",
            gate_id="checkpoint_approved",
            gate_status="blocked",
            pending_checkpoint_ids=[],
            rejected_checkpoint_ids=["ockpt_rejected"],
            reasons=["required objective checkpoints rejected: ockpt_rejected"],
            required_checkpoint_count=1,
        ),
        _objective_event(
            3,
            "stopped",
            ok=True,
            stop_reason="complete",
            adapter_dispatches=0,
            batches=0,
        ),
    ]


def _objective_event(index: int, event: str, **payload: Any) -> dict[str, Any]:
    base = {
        "schema_version": "harness.autonomous_objective_event/v1",
        "objective_id": "obj_scenario",
        "objective_run_id": "orun_scenario",
        "objective_event_id": f"oevt_scenario_{index}",
        "event_index": index,
        "previous_event_sha256": None if index == 1 else f"prev_{index - 1}",
        "event_sha256": f"sha_{index}",
        "event": event,
    }
    base.update(payload)
    return base


def _compact_replay_summary(replay: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_count": replay.get("event_count"),
        "event_type_counts": replay.get("event_type_counts") or {},
        "issue_count": replay.get("issue_count"),
        "issue_codes": replay.get("issue_codes") or [],
        "checkpoint_blocked": replay.get("checkpoint_blocked"),
        "stop_reason": replay.get("stop_reason"),
    }


def _missing_expected(expected: list[str], detected: list[str]) -> list[str]:
    return sorted(signal for signal in expected if signal not in set(detected))


def _summary_rows(cases: list[OrchestrationScenarioCase]) -> list[dict[str, Any]]:
    return [
        {
            "id": case.id,
            "status": case.status,
            "layer": case.layer,
            "failure_mode": case.failure_mode,
            "detected_signals": list(case.detected_signals),
        }
        for case in cases
    ]


def _safety_flags() -> dict[str, bool]:
    return {
        "read_only": True,
        "metadata_only": True,
        "synthetic_probe_only": True,
        "reference_code_imported": False,
        "reference_contents_included": False,
        "provider_called": False,
        "network_called": False,
        "adapter_execution_started": False,
        "tool_execution_started": False,
        "agent_execution_started": False,
        "process_started": False,
        "filesystem_modified": False,
        "permission_granting": False,
        "credential_accessed": False,
        "artifact_bodies_read": False,
        "model_context_allowed": False,
        "live_benchmark_execution_allowed": False,
        "approval_store_instantiated": False,
    }


def _safety_is_passive(safety: dict[str, bool]) -> bool:
    return safety.get("read_only") is True and all(
        safety.get(key) is False
        for key in (
            "reference_code_imported",
            "reference_contents_included",
            "provider_called",
            "network_called",
            "adapter_execution_started",
            "tool_execution_started",
            "agent_execution_started",
            "process_started",
            "filesystem_modified",
            "permission_granting",
            "credential_accessed",
            "artifact_bodies_read",
            "model_context_allowed",
            "live_benchmark_execution_allowed",
            "approval_store_instantiated",
        )
    )


def compact_scenario_rows(catalog: OrchestrationScenarioCatalog) -> list[dict[str, Any]]:
    return sanitize_for_logging(_summary_rows(catalog.cases))
