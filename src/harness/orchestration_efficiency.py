from __future__ import annotations

import inspect
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from harness.approvals import ApprovalStore
from harness.config import HARNESS_DIR
from harness.delegate_budgets import adapter_delegate_budget_projection
from harness.execution import list_execution_adapter_descriptors
from harness.memory.sqlite_store import SQLiteStore, TASK_REPLAY_RECEIPT_SCHEMA_VERSION
from harness.models import (
    ExecutionAdapterDescriptor,
    OrchestrationEfficiencyCheck,
    OrchestrationEfficiencyResult,
    OrchestrationMicrobenchmarkCase,
    OrchestrationMicrobenchmarkResult,
    SandboxActiveRepoWritePolicy,
    SandboxNetworkPolicy,
    SandboxProfileDescriptor,
    SandboxTier,
    ToolReplayPolicy,
)
from harness.objective_checkpoints import verify_objective_checkpoint_evidence
from harness.objective_evidence import verify_objective_evidence
from harness.objective_runner import (
    _evaluate_task_dispatch_autonomy,
    _record_autonomy_decision,
    run_objective_autonomously,
    run_objective_parallel,
)
from harness.paths import resolve_project_root
from harness.sandbox_profiles import get_sandbox_profile
from harness.security import sanitize_for_logging
from harness.traces import export_objective_trace, export_run_trace


ORCHESTRATION_EFFICIENCY_REFERENCE_PATTERNS = {
    "adapter_security_complexity_tradeoff": [
        "microsoft_agent_framework",
        "openai_agents",
        "google_adk",
        "gvisor",
        "firecracker",
    ],
    "bounded_critical_path_scheduler": ["langgraph", "temporal", "dapr", "microsoft_agent_framework"],
    "delegate_budget_efficiency": ["microsoft_agent_framework", "google_adk", "openai_agents", "temporal"],
    "evidence_trace_projection_cost": ["opentelemetry", "temporal", "langgraph"],
    "live_benchmark_permits": ["microsoft_agent_framework", "temporal", "openai_agents", "google_adk"],
    "microbenchmark_contracts": ["microsoft_agent_framework", "langgraph", "temporal", "opentelemetry"],
    "replay_retry_idempotency": ["temporal", "dapr", "containerd"],
}

LIVE_BENCHMARK_PERMIT_SCHEMA_VERSION = "harness.orchestration_live_benchmark_permit/v1"
LIVE_BENCHMARK_PERMITS_SCHEMA_VERSION = "harness.orchestration_live_benchmark_permits/v1"


def run_orchestration_efficiency_audit(project_root: Path) -> OrchestrationEfficiencyResult:
    """Measure orchestration complexity against security and reliability controls.

    This suite is deliberately read-only. It inspects descriptors and existing
    local evidence, plus one deterministic in-process scheduling probe. It does
    not run adapters, call models, preflight providers, touch Docker, or repair
    state.
    """

    project_root = resolve_project_root(project_root)
    checks = [
        _adapter_security_complexity_tradeoff_check(),
        _bounded_critical_path_scheduler_check(),
        _delegate_budget_efficiency_check(),
        _live_benchmark_permits_check(project_root),
        _microbenchmark_contracts_check(project_root),
        _replay_retry_idempotency_check(project_root),
        _evidence_trace_projection_cost_check(project_root),
    ]
    checks.sort(key=lambda check: check.id)
    summary = _summary(checks)
    return OrchestrationEfficiencyResult(
        ok=summary["fail"] == 0,
        project_root=project_root,
        safety=_safety_flags(),
        summary=summary,
        checks=checks,
    )


def summarize_orchestration_efficiency(audit: OrchestrationEfficiencyResult) -> dict[str, Any]:
    failing = [check.id for check in audit.checks if check.status == "fail"]
    warnings = [check.id for check in audit.checks if check.status == "warning"]
    skipped = [check.id for check in audit.checks if check.status == "skipped"]
    status = "fail" if failing else "warning" if warnings else "pass"
    return {
        "schema_version": "harness.orchestration_efficiency_summary/v1",
        "ok": audit.ok,
        "status": status,
        "summary": dict(audit.summary),
        "failing_check_ids": failing,
        "warning_check_ids": warnings,
        "skipped_check_ids": skipped,
        "check_ids": [check.id for check in audit.checks],
        "safety": dict(audit.safety),
        "next_action": f"harness evals run --suite orchestration-efficiency --project {audit.project_root} --output json",
        "command": f"harness evals run --suite orchestration-efficiency --project {audit.project_root} --output json",
    }


def summarize_orchestration_microbenchmarks(result: OrchestrationMicrobenchmarkResult) -> dict[str, Any]:
    failing = [benchmark.id for benchmark in result.benchmarks if benchmark.status == "fail"]
    warnings = [benchmark.id for benchmark in result.benchmarks if benchmark.status == "warning"]
    skipped = [benchmark.id for benchmark in result.benchmarks if benchmark.status == "skipped"]
    status = "fail" if failing else "warning" if warnings else "pass"
    return {
        "schema_version": "harness.orchestration_microbenchmarks_summary/v1",
        "ok": result.ok,
        "status": status,
        "summary": dict(result.summary),
        "failing_benchmark_ids": failing,
        "warning_benchmark_ids": warnings,
        "skipped_benchmark_ids": skipped,
        "benchmark_ids": [benchmark.id for benchmark in result.benchmarks],
        "safety": dict(result.safety),
        "next_action": (
            f"harness evals run --suite orchestration-microbenchmarks --project {result.project_root} --output json"
        ),
        "command": f"harness evals run --suite orchestration-microbenchmarks --project {result.project_root} --output json",
    }


def run_orchestration_microbenchmarks(
    project_root: Path,
    *,
    samples: int = 5,
) -> OrchestrationMicrobenchmarkResult:
    """Run bounded passive/synthetic orchestration microbenchmarks.

    This suite gives operators measured local overhead for the benchmarkable
    parts of the orchestration harness without starting adapters, sandboxes,
    providers, Docker, network calls, or filesystem mutation. Rows that require
    live provider/sandbox execution are reported as skipped with the explicit
    measurement path.
    """

    project_root = resolve_project_root(project_root)
    sample_count = max(1, min(int(samples), 25))
    sqlite_path = project_root / HARNESS_DIR / "harness.sqlite"
    initialized = sqlite_path.exists()
    benchmarks = [
        _handoff_overhead_benchmark(sample_count),
        _fanout_fanin_critical_path_benchmark(sample_count),
        _checkpoint_latency_benchmark(project_root, initialized, sample_count),
        _sandbox_startup_benchmark(project_root),
        _tool_adapter_overhead_benchmark(sample_count),
        _retry_safety_benchmark(sample_count),
        _trace_overhead_benchmark(project_root, initialized, sample_count),
        _shared_llm_contention_benchmark(project_root),
        _verification_stage_roi_benchmark(sample_count),
    ]
    summary = _microbenchmark_summary(benchmarks)
    return OrchestrationMicrobenchmarkResult(
        ok=summary["fail"] == 0,
        project_root=project_root,
        safety=_microbenchmark_safety_flags(),
        summary=summary,
        benchmarks=benchmarks,
    )


def _handoff_overhead_benchmark(samples: int) -> OrchestrationMicrobenchmarkCase:
    def measure() -> dict[str, Any]:
        descriptors = list_execution_adapter_descriptors()
        projections = [adapter_delegate_budget_projection(descriptor) for descriptor in descriptors]
        return {
            "registered_adapter_count": len(descriptors),
            "budgeted_adapter_count": len(projections),
            "runtime_invocation_ceiling": sum(
                int(projection["budget"]["max_runtime_invocations"]) for projection in projections
            ),
        }

    sample_rows, duration_stats, gaps = _time_microbenchmark_samples(samples, measure)
    duration_stats = _with_duration_guardrail("handoff_overhead", duration_stats)
    return _benchmark_case(
        "handoff_overhead",
        status="pass" if not gaps else "fail",
        measurement_mode="passive_proxy",
        message="Measured passive descriptor and delegate-budget projection overhead.",
        source_checks=["adapter_security_complexity_tradeoff", "delegate_budget_efficiency"],
        metrics=["duration_ns", "registered_adapter_count", "budgeted_adapter_count", "runtime_invocation_ceiling"],
        measurements=duration_stats,
        samples=sample_rows,
        gaps=gaps,
    )


def _fanout_fanin_critical_path_benchmark(samples: int) -> OrchestrationMicrobenchmarkCase:
    def measure() -> dict[str, Any]:
        serial = _simulate_probe_schedule(_PROBE_TASKS, max_parallel=1)
        bounded = _simulate_probe_schedule(_PROBE_TASKS, max_parallel=2)
        critical_path = _critical_path_duration(_PROBE_TASKS)
        return {
            "serial_duration_units": serial["duration_units"],
            "bounded_duration_units": bounded["duration_units"],
            "critical_path_units": critical_path,
            "bounded_max_active": bounded["max_active"],
            "speedup_over_serial": round(serial["duration_units"] / bounded["duration_units"], 3)
            if bounded["duration_units"]
            else None,
        }

    sample_rows, duration_stats, gaps = _time_microbenchmark_samples(samples, measure)
    duration_stats = _with_duration_guardrail("fanout_fanin_critical_path", duration_stats)
    latest = sample_rows[-1] if sample_rows else {}
    if latest and latest.get("bounded_duration_units", 0) < latest.get("critical_path_units", 0):
        gaps.append("bounded scheduler reported less than critical path")
    return _benchmark_case(
        "fanout_fanin_critical_path",
        status="pass" if not gaps else "fail",
        measurement_mode="synthetic_probe",
        message="Measured deterministic fan-out/fan-in scheduler probe overhead.",
        source_checks=["bounded_critical_path_scheduler"],
        metrics=[
            "duration_ns",
            "serial_duration_units",
            "bounded_duration_units",
            "critical_path_units",
            "bounded_max_active",
        ],
        measurements=duration_stats,
        samples=sample_rows,
        gaps=gaps,
    )


def _checkpoint_latency_benchmark(
    project_root: Path,
    initialized: bool,
    samples: int,
) -> OrchestrationMicrobenchmarkCase:
    if not initialized:
        return _benchmark_case(
            "checkpoint_latency",
            status="skipped",
            measurement_mode="passive_existing_evidence",
            message="No initialized Harness runtime state exists; checkpoint evidence was not measured.",
            source_checks=["supervisor_checkpoints"],
            metrics=["duration_ns", "objective_count", "checkpoint_check_count"],
            measurements={"initialized": False},
            next_actions=["Initialize project state and create objective checkpoint evidence before measuring checkpoint latency."],
        )
    store = SQLiteStore(project_root)
    objective_ids = [objective.id for objective in list(store.list_objectives())[:5]]
    if not objective_ids:
        return _benchmark_case(
            "checkpoint_latency",
            status="skipped",
            measurement_mode="passive_existing_evidence",
            message="No objectives exist; checkpoint evidence was not measured.",
            source_checks=["supervisor_checkpoints"],
            metrics=["duration_ns", "objective_count", "checkpoint_check_count"],
            measurements={"initialized": True, "objective_count": 0},
            next_actions=["Create or import an objective before measuring checkpoint latency."],
        )

    def measure() -> dict[str, Any]:
        ok_count = 0
        check_count = 0
        for objective_id in objective_ids:
            verification = verify_objective_checkpoint_evidence(project_root, objective_id)
            ok_count += 1 if verification.ok else 0
            check_count += len(verification.checks)
        return {
            "objective_count": len(objective_ids),
            "verification_ok_count": ok_count,
            "checkpoint_check_count": check_count,
        }

    sample_rows, duration_stats, gaps = _time_microbenchmark_samples(samples, measure)
    duration_stats = _with_duration_guardrail("checkpoint_latency", duration_stats)
    latest = sample_rows[-1] if sample_rows else {}
    if latest and latest.get("verification_ok_count") != latest.get("objective_count"):
        gaps.append("one or more objective checkpoint evidence verifications failed")
    return _benchmark_case(
        "checkpoint_latency",
        status="pass" if not gaps else "fail",
        measurement_mode="passive_existing_evidence",
        message="Measured read-only checkpoint evidence verification overhead.",
        source_checks=["supervisor_checkpoints"],
        metrics=["duration_ns", "objective_count", "verification_ok_count", "checkpoint_check_count"],
        measurements=duration_stats,
        samples=sample_rows,
        gaps=gaps,
    )


def _sandbox_startup_benchmark(project_root: Path) -> OrchestrationMicrobenchmarkCase:
    descriptors = list_execution_adapter_descriptors()
    isolated_workspace_adapters = [
        descriptor.id
        for descriptor in descriptors
        if descriptor.delegate_budget.filesystem_scope == "isolated_workspace"
    ]
    return _benchmark_case(
        "sandbox_startup",
        status="skipped",
        measurement_mode="explicit_live_required",
        message="Sandbox startup requires an approved live sandbox-backed adapter run.",
        source_checks=["delegate_budget_efficiency", "sandboxed_registered_adapters"],
        metrics=["isolated_workspace_adapter_count", "sandbox_contract_available"],
        measurements={
            "isolated_workspace_adapter_count": len(isolated_workspace_adapters),
            "isolated_workspace_adapters": isolated_workspace_adapters,
            "sandbox_contract_available": bool(isolated_workspace_adapters),
            "adapter_execution_started": False,
            "live_permit": _live_benchmark_permit_projection(project_root, "sandbox_startup"),
        },
        next_actions=["Run an approved codex_isolated_edit task and inspect run trace timing."],
    )


def _tool_adapter_overhead_benchmark(samples: int) -> OrchestrationMicrobenchmarkCase:
    def measure() -> dict[str, Any]:
        descriptors = list_execution_adapter_descriptors()
        allowlist_count = sum(len(descriptor.delegate_budget.tool_allowlist) for descriptor in descriptors)
        tool_budget_adapter_count = sum(
            1
            for descriptor in descriptors
            if descriptor.delegate_budget.max_tool_calls > 0 or descriptor.delegate_budget.tool_allowlist
        )
        return {
            "adapter_count": len(descriptors),
            "tool_budget_adapter_count": tool_budget_adapter_count,
            "declared_tool_allowlist_count": allowlist_count,
        }

    sample_rows, duration_stats, gaps = _time_microbenchmark_samples(samples, measure)
    duration_stats = _with_duration_guardrail("tool_adapter_overhead", duration_stats)
    return _benchmark_case(
        "tool_adapter_overhead",
        status="pass" if not gaps else "fail",
        measurement_mode="passive_proxy",
        message="Measured passive tool-budget and allowlist projection overhead.",
        source_checks=["protocol_and_tool_exposure", "delegate_budget_efficiency"],
        metrics=["duration_ns", "adapter_count", "tool_budget_adapter_count", "declared_tool_allowlist_count"],
        measurements=duration_stats,
        samples=sample_rows,
        gaps=gaps,
    )


def _retry_safety_benchmark(samples: int) -> OrchestrationMicrobenchmarkCase:
    safe_replay_policies = {ToolReplayPolicy.SAFE, ToolReplayPolicy.IDEMPOTENT_WITH_KEY}

    def measure() -> dict[str, Any]:
        descriptors = list_execution_adapter_descriptors()
        unsafe = [
            descriptor.id
            for descriptor in descriptors
            if descriptor.autonomy_default == "auto_allowed" and descriptor.replay_policy not in safe_replay_policies
        ]
        return {
            "adapter_count": len(descriptors),
            "unsafe_auto_allowed_count": len(unsafe),
            "autonomous_retry_ceiling": sum(descriptor.max_autonomous_retries for descriptor in descriptors),
        }

    sample_rows, duration_stats, gaps = _time_microbenchmark_samples(samples, measure)
    duration_stats = _with_duration_guardrail("retry_safety", duration_stats)
    latest = sample_rows[-1] if sample_rows else {}
    if latest and latest.get("unsafe_auto_allowed_count"):
        gaps.append("auto-allowed adapters include unsafe replay policy")
    return _benchmark_case(
        "retry_safety",
        status="pass" if not gaps else "fail",
        measurement_mode="passive_policy",
        message="Measured retry/replay policy validation overhead.",
        source_checks=["replay_retry_idempotency"],
        metrics=["duration_ns", "adapter_count", "unsafe_auto_allowed_count", "autonomous_retry_ceiling"],
        measurements=duration_stats,
        samples=sample_rows,
        gaps=gaps,
    )


def _trace_overhead_benchmark(
    project_root: Path,
    initialized: bool,
    samples: int,
) -> OrchestrationMicrobenchmarkCase:
    if not initialized:
        return _benchmark_case(
            "trace_overhead",
            status="skipped",
            measurement_mode="passive_existing_evidence",
            message="No initialized Harness runtime state exists; trace projection overhead was not measured.",
            source_checks=["evidence_trace_projection_cost"],
            metrics=["duration_ns", "objective_trace_spans", "run_trace_spans"],
            measurements={"initialized": False},
            next_actions=["Run an objective or import runtime evidence before measuring trace projection overhead."],
        )
    store = SQLiteStore(project_root)
    if not store.list_runs():
        return _benchmark_case(
            "trace_overhead",
            status="skipped",
            measurement_mode="passive_existing_evidence",
            message="No run evidence exists; trace projection overhead was not measured.",
            source_checks=["evidence_trace_projection_cost"],
            metrics=["duration_ns", "objective_trace_spans", "run_trace_spans"],
            measurements={"initialized": True, "run_count": 0},
            next_actions=["Run an objective before measuring trace projection overhead."],
        )

    trace_samples = min(samples, 3)

    def measure() -> dict[str, Any]:
        objective_measurements, objective_failures = _objective_evidence_trace_measurements(project_root, store)
        run_measurements, run_failures = _run_trace_measurements(project_root, store)
        return {
            "objective_evidence_files": objective_measurements["objective_evidence_files"],
            "objective_trace_spans": objective_measurements["total_trace_spans"],
            "run_count": run_measurements["run_count"],
            "run_trace_spans": run_measurements["total_trace_spans"],
            "failure_count": len(objective_failures) + len(run_failures),
            "artifact_bodies_read": False,
        }

    sample_rows, duration_stats, gaps = _time_microbenchmark_samples(trace_samples, measure)
    duration_stats = _with_duration_guardrail("trace_overhead", duration_stats)
    latest = sample_rows[-1] if sample_rows else {}
    if latest and latest.get("failure_count"):
        gaps.append("one or more trace projections failed")
    return _benchmark_case(
        "trace_overhead",
        status="pass" if not gaps else "fail",
        measurement_mode="passive_existing_evidence",
        message="Measured read-only objective/run trace projection overhead.",
        source_checks=["evidence_trace_projection_cost"],
        metrics=["duration_ns", "objective_trace_spans", "run_trace_spans", "artifact_bodies_read"],
        measurements=duration_stats,
        samples=sample_rows,
        gaps=gaps,
    )


def _shared_llm_contention_benchmark(project_root: Path) -> OrchestrationMicrobenchmarkCase:
    descriptors = list_execution_adapter_descriptors()
    budgets = [descriptor.delegate_budget for descriptor in descriptors]
    return _benchmark_case(
        "shared_llm_contention",
        status="skipped",
        measurement_mode="explicit_live_required",
        message="Shared model contention requires an approved provider-backed live benchmark.",
        source_checks=["delegate_budget_efficiency"],
        metrics=["model_call_ceiling", "runtime_invocation_ceiling", "provider_call_required_for_live_measurement"],
        measurements={
            "model_call_ceiling": sum(budget.max_model_calls for budget in budgets),
            "runtime_invocation_ceiling": sum(budget.max_runtime_invocations for budget in budgets),
            "provider_call_required_for_live_measurement": True,
            "provider_called": False,
            "live_permit": _live_benchmark_permit_projection(project_root, "shared_llm_contention"),
        },
        next_actions=["Run an approved provider-backed contention benchmark outside passive release gates."],
    )


def _verification_stage_roi_benchmark(samples: int) -> OrchestrationMicrobenchmarkCase:
    def measure() -> dict[str, Any]:
        descriptors = list_execution_adapter_descriptors()
        review_gate_adapters = [
            descriptor.id
            for descriptor in descriptors
            if "review_report" in " ".join(descriptor.terminal_evidence_required)
        ]
        return {
            "adapter_count": len(descriptors),
            "review_gate_adapter_count": len(review_gate_adapters),
            "review_gate_evidence_required": bool(review_gate_adapters),
        }

    sample_rows, duration_stats, gaps = _time_microbenchmark_samples(samples, measure)
    duration_stats = _with_duration_guardrail("verification_stage_roi", duration_stats)
    latest = sample_rows[-1] if sample_rows else {}
    if latest and not latest.get("review_gate_evidence_required"):
        gaps.append("no review-gate evidence adapter is registered")
    return _benchmark_case(
        "verification_stage_roi",
        status="pass" if not gaps else "fail",
        measurement_mode="passive_proxy",
        message="Measured passive review-gate coverage projection overhead.",
        source_checks=["applyback_governance", "replay_retry_idempotency"],
        metrics=["duration_ns", "review_gate_adapter_count", "review_gate_evidence_required"],
        measurements=duration_stats,
        samples=sample_rows,
        gaps=gaps,
    )


def _adapter_security_complexity_tradeoff_check() -> OrchestrationEfficiencyCheck:
    measurements: list[dict[str, Any]] = []
    gaps: list[str] = []
    total_complexity_score = 0
    total_security_control_score = 0
    core_service_prelease_gating_enforced = _core_service_prelease_gating_enforced()
    manual_queue_prelease_gating_enforced = _manual_queue_prelease_gating_enforced()
    adapter_rejection_finalization_enforced = _adapter_rejection_finalization_enforced()
    read_only_compatibility_rejection_finalization_enforced = (
        _read_only_compatibility_rejection_finalization_enforced()
    )
    daemon_renewal_inconsistent_lease_guard_enforced = _daemon_renewal_inconsistent_lease_guard_enforced()
    daemon_recovery_expired_lease_guard_enforced = _daemon_recovery_expired_lease_guard_enforced()
    daemon_shutdown_linked_run_guard_enforced = _daemon_shutdown_linked_run_guard_enforced()
    lease_mutation_authority_guard_enforced = _lease_mutation_authority_guard_enforced()
    adapter_boundary_failure_finalization_enforced = _adapter_boundary_failure_finalization_enforced()
    runtime_control_breaker_prelease_gating_enforced = _runtime_control_breaker_prelease_gating_enforced()

    for descriptor in list_execution_adapter_descriptors():
        profile = _sandbox_profile(descriptor)
        complexity_reasons = _adapter_complexity_reasons(descriptor, profile)
        security_controls = _adapter_security_controls(descriptor, profile)
        adapter_gaps = _adapter_security_complexity_gaps(descriptor, profile)
        total_complexity_score += len(complexity_reasons)
        total_security_control_score += len(security_controls)
        gaps.extend(f"{descriptor.id}: {gap}" for gap in adapter_gaps)
        measurements.append(
            {
                "adapter_id": descriptor.id,
                "autonomy_default": descriptor.autonomy_default,
                "replay_policy": descriptor.replay_policy.value,
                "max_autonomous_retries": descriptor.max_autonomous_retries,
                "sandbox_profile_id": descriptor.sandbox_profile_id,
                "sandbox_tier": profile.tier.value if profile is not None else None,
                "sandbox_network": profile.network.value if profile is not None else None,
                "active_repo_write": profile.active_repo_write.value if profile is not None else None,
                "required_approvals": list(descriptor.required_approvals),
                "backend_requirement_count": len(descriptor.backend_requirements),
                "sandbox_requirement_count": len(descriptor.sandbox_requirements),
                "complexity_score": len(complexity_reasons),
                "complexity_reasons": complexity_reasons,
                "security_control_score": len(security_controls),
                "security_controls": security_controls,
                "gaps": adapter_gaps,
            }
        )
    if not core_service_prelease_gating_enforced:
        gaps.append("core service can acquire foreground leases before approval, policy, or dependency eligibility checks")
    if not manual_queue_prelease_gating_enforced:
        gaps.append("manual task run-next can acquire leases before approval, policy, or dependency eligibility checks")
    if not adapter_rejection_finalization_enforced:
        gaps.append("registered-adapter no-run rejections can leave active leases or stale attempts behind")
    if not read_only_compatibility_rejection_finalization_enforced:
        gaps.append("read-only compatibility no-run rejections can leave active leases or stale attempts behind")
    if not daemon_renewal_inconsistent_lease_guard_enforced:
        gaps.append("daemon lease renewal can preserve inconsistent active leases")
    if not daemon_recovery_expired_lease_guard_enforced:
        gaps.append("daemon lease recovery can leave expired active leases or requeue linked non-terminal runs")
    if not daemon_shutdown_linked_run_guard_enforced:
        gaps.append("daemon stop/stale lease expiry can requeue linked non-terminal runs")
    if not lease_mutation_authority_guard_enforced:
        gaps.append("lease start or finish mutation can proceed without active owner authority")
    if not adapter_boundary_failure_finalization_enforced:
        gaps.append("registered-adapter boundary failures can bypass lease finalization or breaker telemetry")
    if not runtime_control_breaker_prelease_gating_enforced:
        gaps.append("runtime controls or adapter breakers can wait until dispatch after guarded lease acquisition")

    status = "pass" if not gaps else "fail"
    return _check(
        "adapter_security_complexity_tradeoff",
        status,
        "Adapter complexity is paired with explicit sandbox, approval, autonomy, and replay controls."
        if status == "pass"
        else "One or more adapters add complexity without matching security controls.",
        measurements={
            "adapter_count": len(measurements),
            "total_complexity_score": total_complexity_score,
            "total_security_control_score": total_security_control_score,
            "core_service_prelease_gating_enforced": core_service_prelease_gating_enforced,
            "manual_queue_prelease_gating_enforced": manual_queue_prelease_gating_enforced,
            "adapter_rejection_finalization_enforced": adapter_rejection_finalization_enforced,
            "read_only_compatibility_rejection_finalization_enforced": (
                read_only_compatibility_rejection_finalization_enforced
            ),
            "daemon_renewal_inconsistent_lease_guard_enforced": (
                daemon_renewal_inconsistent_lease_guard_enforced
            ),
            "daemon_recovery_expired_lease_guard_enforced": daemon_recovery_expired_lease_guard_enforced,
            "daemon_shutdown_linked_run_guard_enforced": daemon_shutdown_linked_run_guard_enforced,
            "lease_mutation_authority_guard_enforced": lease_mutation_authority_guard_enforced,
            "adapter_boundary_failure_finalization_enforced": (
                adapter_boundary_failure_finalization_enforced
            ),
            "runtime_control_breaker_prelease_gating_enforced": (
                runtime_control_breaker_prelease_gating_enforced
            ),
            "adapters": measurements,
        },
        gaps=gaps,
        next_actions=[]
        if status == "pass"
        else [
            "Add or tighten sandbox profiles, approval requirements, autonomy defaults, or replay policy before enabling the adapter."
        ],
    )


def _bounded_critical_path_scheduler_check() -> OrchestrationEfficiencyCheck:
    signature = inspect.signature(run_objective_parallel)
    max_parallel_parameter = signature.parameters.get("max_parallel")
    configured_default = max_parallel_parameter.default if max_parallel_parameter is not None else None
    max_parallel = configured_default if isinstance(configured_default, int) and configured_default >= 1 else 2
    serial = _simulate_probe_schedule(_PROBE_TASKS, max_parallel=1)
    bounded = _simulate_probe_schedule(_PROBE_TASKS, max_parallel=max_parallel)
    critical_path = _critical_path_duration(_PROBE_TASKS)
    objective_prelease_autonomy_enforced = _objective_runner_prelease_autonomy_enforced()
    scheduler_policy_evidence_enforced = _scheduler_policy_evidence_enforced()

    gaps: list[str] = []
    if max_parallel_parameter is None:
        gaps.append("run_objective_parallel is missing a max_parallel bound")
    if not isinstance(configured_default, int):
        gaps.append("run_objective_parallel max_parallel default is not an integer")
    elif configured_default < 1:
        gaps.append("run_objective_parallel max_parallel default is below 1")
    elif configured_default > 2:
        gaps.append("run_objective_parallel max_parallel default exceeds the conservative default cap of 2")
    if bounded["max_active"] > max_parallel:
        gaps.append("bounded scheduler probe exceeded max_parallel")
    if bounded["duration_units"] < critical_path:
        gaps.append("bounded scheduler probe reported less than the graph critical path")
    if bounded["duration_units"] > serial["duration_units"]:
        gaps.append("bounded scheduler probe is slower than serial execution")
    if not objective_prelease_autonomy_enforced:
        gaps.append("objective runner does not enforce autonomy and approval decisions before new lease acquisition")
    if not scheduler_policy_evidence_enforced:
        gaps.append("objective batch plans do not enforce scheduler-policy evidence against durable task and lease state")

    status = "pass" if not gaps else "fail"
    measurements = {
        "run_objective_parallel_has_max_parallel": max_parallel_parameter is not None,
        "configured_default_max_parallel": configured_default,
        "probe_task_count": len(_PROBE_TASKS),
        "probe_dependency_count": sum(len(task.depends_on) for task in _PROBE_TASKS),
        "critical_path_units": critical_path,
        "serial_duration_units": serial["duration_units"],
        "bounded_duration_units": bounded["duration_units"],
        "bounded_max_active": bounded["max_active"],
        "objective_runner_prelease_autonomy_enforced": objective_prelease_autonomy_enforced,
        "scheduler_policy_evidence_enforced": scheduler_policy_evidence_enforced,
        "speedup_over_serial": round(serial["duration_units"] / bounded["duration_units"], 3)
        if bounded["duration_units"]
        else None,
        "bounded_batches": bounded["batches"],
    }
    return _check(
        "bounded_critical_path_scheduler",
        status,
        "The objective scheduler keeps fan-out bounded while reducing a synthetic critical path."
        if status == "pass"
        else "The objective scheduler bound or critical-path probe is unsafe.",
        measurements=measurements,
        gaps=gaps,
        next_actions=[]
        if status == "pass"
        else ["Keep default objective fan-out low and preserve max_parallel as an explicit scheduler input."],
    )


def _scheduler_policy_evidence_enforced() -> bool:
    try:
        import harness.objective_evidence as objective_evidence
        from harness.objective_batch_plan import ObjectiveBatchPlan, ObjectiveBatchSelection

        helper_source = inspect.getsource(objective_evidence._append_batch_schedule_policy_issues)
    except (ImportError, OSError, TypeError, AttributeError):
        return False
    return (
        "policy_evidence" in ObjectiveBatchPlan.model_fields
        and "selection_source" in ObjectiveBatchSelection.model_fields
        and "candidate_task_ids_policy_order_mismatch" in helper_source
        and "selected_task_ids_not_policy_prefix" in helper_source
        and "resumed_lease_order_mismatch" in helper_source
        and "schedule_profile_mismatch" in helper_source
    )


def _objective_runner_prelease_autonomy_enforced() -> bool:
    try:
        sequential_source = inspect.getsource(run_objective_autonomously)
        parallel_source = inspect.getsource(run_objective_parallel)
        evaluator_source = inspect.getsource(_evaluate_task_dispatch_autonomy)
        decision_source = inspect.getsource(_record_autonomy_decision)
    except (OSError, TypeError):
        return False
    return (
        "_next_scheduled_task_candidate" in sequential_source
        and "_next_scheduled_task_candidate" in parallel_source
        and "_evaluate_task_dispatch_autonomy(project_root, policy.id, candidate, None)" in sequential_source
        and "_evaluate_task_dispatch_autonomy(project_root, policy.id, candidate, None)" in parallel_source
        and "select_guarded_task_for_lease" in sequential_source
        and "select_guarded_task_for_lease" in parallel_source
        and "_record_lease_guard_stop" in sequential_source
        and "_record_lease_guard_stop" in parallel_source
        and '"lease_id": None' in sequential_source
        and "_record_autonomy_decision(project_root, run_id, objective_id, candidate, None, decision)" in parallel_source
        and "pre_lease_autonomy_decision" in evaluator_source
        and "lease_id = lease.id if lease is not None else None" in decision_source
    )


def _core_service_prelease_gating_enforced() -> bool:
    try:
        from harness.core_service import HarnessCoreService

        run_task_source = inspect.getsource(HarnessCoreService.run_task)
        helper_source = inspect.getsource(HarnessCoreService._task_eligibility_error)
    except (ImportError, OSError, TypeError):
        return False
    return (
        "select_guarded_task_for_lease(" in run_task_source
        and "select_task_for_lease(task_id" not in run_task_source
        and "pause_reasons" in run_task_source
        and "_task_eligibility_error(eligibility)" in run_task_source
        and '"waiting_approval"' in run_task_source
        and '"control_disabled"' in helper_source
        and "target_kind" in helper_source
        and "failure_count" in helper_source
        and "missing_approvals" in helper_source
        and "required_approvals" in helper_source
    )


def _manual_queue_prelease_gating_enforced() -> bool:
    try:
        from harness.cli.main import tasks_run_next

        guarded_source = inspect.getsource(SQLiteStore.select_next_guarded_task_for_lease)
        explicit_guarded_source = inspect.getsource(SQLiteStore.select_guarded_task_for_lease)
        daemon_source = inspect.getsource(SQLiteStore.select_next_daemon_task_for_lease)
        cli_source = inspect.getsource(tasks_run_next)
    except (ImportError, OSError, TypeError):
        return False
    return (
        "daemon_task_eligibility(task, conn=conn)" in guarded_source
        and "_lease_task_in_conn" in guarded_source
        and "daemon_task_eligibility(task, conn=conn)" in explicit_guarded_source
        and "DAEMON_TASK_PAUSE_DECISIONS" in explicit_guarded_source
        and "pause_reasons.append(eligibility)" in guarded_source
        and "select_next_guarded_task_for_lease" in daemon_source
        and "select_next_guarded_task_for_lease" in cli_source
        and '"pause_reasons": pause_reasons' in cli_source
        and '"paused"' in cli_source
    )


def _adapter_rejection_finalization_enforced() -> bool:
    try:
        from harness.execution import _record_adapter_rejection

        finalizer_source = inspect.getsource(SQLiteStore.finalize_rejected_task_lease)
        recorder_source = inspect.getsource(_record_adapter_rejection)
    except (AttributeError, ImportError, OSError, TypeError):
        return False
    return (
        "TaskLeaseStatus.RELEASED" in finalizer_source
        and "TaskStatus.WAITING_APPROVAL" in finalizer_source
        and "TaskStatus.FAILED" in finalizer_source
        and "attempt.run_id is not None" in finalizer_source
        and "task.run_id is not None" in finalizer_source
        and "duplicate_run" in finalizer_source
        and "lease_owner_mismatch" in finalizer_source
        and "finalize_rejected_task_lease" in recorder_source
        and "lease_status" in recorder_source
        and "attempt_status" in recorder_source
        and "task_status" in recorder_source
    )


def _adapter_boundary_failure_finalization_enforced() -> bool:
    try:
        from harness.execution import _record_adapter_execution_failure, _record_adapter_rejection, execute_lease

        execute_source = inspect.getsource(execute_lease)
        failure_source = inspect.getsource(_record_adapter_execution_failure)
        recorder_source = inspect.getsource(_record_adapter_rejection)
    except (AttributeError, ImportError, OSError, TypeError):
        return False
    return (
        "except Exception as exc" in execute_source
        and "_record_adapter_execution_failure" in execute_source
        and "adapter_execution_failed" in failure_source
        and "finish_attempt_run" in failure_source
        and "run_status=\"failed\"" in failure_source
        and "failure_code=\"adapter_execution_failed\"" in failure_source
        and "_record_adapter_rejection" in failure_source
        and "reason_code" in recorder_source
        and "lease_status" in recorder_source
        and "attempt_status" in recorder_source
        and "task_status" in recorder_source
    )


def _runtime_control_breaker_prelease_gating_enforced() -> bool:
    try:
        eligibility_source = inspect.getsource(SQLiteStore.daemon_task_eligibility)
        guard_source = inspect.getsource(SQLiteStore._daemon_registered_adapter_pause)
        selection_source = inspect.getsource(SQLiteStore.select_next_guarded_task_for_lease)
        explicit_selection_source = inspect.getsource(SQLiteStore.select_guarded_task_for_lease)
        paused_source = inspect.getsource(SQLiteStore.daemon_paused_tasks)
    except (AttributeError, OSError, TypeError):
        return False
    return (
        "_daemon_registered_adapter_pause(task, conn=conn)" in eligibility_source
        and "runtime_control_matches_descriptor" in guard_source
        and "control_disabled" in guard_source
        and "active_execution_controls" in guard_source
        and "adapter_breaker_state" in guard_source
        and "breaker_open" in guard_source
        and "DAEMON_TASK_PAUSE_DECISIONS" in selection_source
        and "DAEMON_TASK_PAUSE_DECISIONS" in explicit_selection_source
        and "_lease_task_in_conn" in explicit_selection_source
        and "DAEMON_TASK_PAUSE_DECISIONS" in paused_source
    )


def _read_only_compatibility_rejection_finalization_enforced() -> bool:
    try:
        from harness.daemon_adapters import _record_read_only_rejection

        recorder_source = inspect.getsource(_record_read_only_rejection)
    except (ImportError, OSError, TypeError):
        return False
    return (
        "finalize_rejected_task_lease" in recorder_source
        and "execution_adapter_rejected" in recorder_source
        and "lease_status" in recorder_source
        and "attempt_status" in recorder_source
        and "task_status" in recorder_source
    )


def _daemon_renewal_inconsistent_lease_guard_enforced() -> bool:
    try:
        renewal_source = inspect.getsource(SQLiteStore.renew_daemon_leases)
        rejection_source = inspect.getsource(SQLiteStore._active_lease_renewal_rejection)
        release_source = inspect.getsource(SQLiteStore._release_inconsistent_active_lease)
    except (AttributeError, OSError, TypeError):
        return False
    return (
        "_active_lease_renewal_rejection" in renewal_source
        and "_release_inconsistent_active_lease" in renewal_source
        and "TaskStatus.LEASED" in rejection_source
        and "TaskStatus.RUNNING" in rejection_source
        and "attempt.run_id is None" in rejection_source
        and "release_inconsistent_lease" in release_source
        and "inconsistent_active_lease_released" in release_source
        and "validate_task_transition" in release_source
    )


def _daemon_recovery_expired_lease_guard_enforced() -> bool:
    try:
        recovery_source = inspect.getsource(SQLiteStore.recover_daemon_leases)
        expired_source = inspect.getsource(SQLiteStore._recover_expired_active_lease)
        inconsistent_source = inspect.getsource(SQLiteStore._expire_inconsistent_active_lease)
    except (AttributeError, OSError, TypeError):
        return False
    return (
        "_recover_expired_active_lease" in recovery_source
        and "TaskLeaseStatus.EXPIRED" in expired_source
        and "TaskLeaseStatus.RELEASED" in expired_source
        and "_reconcile_dry_run_terminal_state" in expired_source
        and "recover_execution" in expired_source
        and "nonterminal_linked_run" in expired_source
        and "missing_run" in expired_source
        and "_expire_inconsistent_active_lease" in expired_source
        and "recover_inconsistent_lease" in inconsistent_source
        and "inconsistent_expired_lease" in inconsistent_source
    )


def _daemon_shutdown_linked_run_guard_enforced() -> bool:
    try:
        expire_all_source = inspect.getsource(SQLiteStore._expire_active_daemon_leases)
        shutdown_source = inspect.getsource(SQLiteStore._expire_active_daemon_lease_for_shutdown)
    except (AttributeError, OSError, TypeError):
        return False
    return (
        "_expire_active_daemon_lease_for_shutdown" in expire_all_source
        and "attempt.run_id is not None" in shutdown_source
        and "linked_run_completed" in shutdown_source
        and "linked_run_failed" in shutdown_source
        and "nonterminal_linked_run" in shutdown_source
        and "missing_run" in shutdown_source
        and "TaskStatus.FAILED" in shutdown_source
        and "TaskLeaseStatus.EXPIRED" in shutdown_source
        and "TaskLeaseStatus.RELEASED" in shutdown_source
    )


def _lease_mutation_authority_guard_enforced() -> bool:
    try:
        authority_source = inspect.getsource(SQLiteStore._require_active_lease_authority)
        dry_run_source = inspect.getsource(SQLiteStore.execute_dry_run_lease)
        start_source = inspect.getsource(SQLiteStore.start_attempt_run)
        start_read_only_source = inspect.getsource(SQLiteStore.start_read_only_lease_run)
        finish_source = inspect.getsource(SQLiteStore.finish_attempt_run)
        finish_read_only_source = inspect.getsource(SQLiteStore.finish_read_only_lease_run)
        run_insert_source = inspect.getsource(SQLiteStore._insert_run_in_conn)
    except (AttributeError, OSError, TypeError):
        return False
    return (
        "Lease owner mismatch" in authority_source
        and "requires active lease" in authority_source
        and "_require_active_lease_authority(lease, owner, action=\"Dry-run execution\")" in dry_run_source
        and "_insert_run_in_conn" in dry_run_source
        and "Dry-run execution requires active lease owned by" in dry_run_source
        and "Dry-run finalization requires active lease owned by" in dry_run_source
        and "WHERE id = ? AND status = ? AND owner = ?" in dry_run_source
        and "validate_execution_lease_for_run(lease_id, owner=owner)" in start_source
        and "validate_read_only_lease_for_execution(lease_id, owner=owner)" in start_read_only_source
        and "_insert_run_in_conn" in start_source
        and "_insert_run_in_conn" in start_read_only_source
        and "WHERE id = ? AND status = ? AND owner = ?" in start_source
        and "WHERE id = ? AND status = ? AND owner = ?" in start_read_only_source
        and "_require_active_lease_authority(lease, owner, action=\"Execution finalization\")" in finish_source
        and "_require_active_lease_authority(lease, owner, action=\"Read-only execution finalization\")"
        in finish_read_only_source
        and "WHERE id = ? AND status = ? AND owner = ?" in finish_source
        and "WHERE id = ? AND status = ? AND owner = ?" in finish_read_only_source
        and "self.write_run_manifest(run_id)" in finish_source
        and "INSERT INTO runs" in run_insert_source
    )


def _delegate_budget_efficiency_check() -> OrchestrationEfficiencyCheck:
    projections = [adapter_delegate_budget_projection(descriptor) for descriptor in list_execution_adapter_descriptors()]
    gaps = [
        f"{projection['adapter_id']}: {gap}"
        for projection in projections
        for gap in projection.get("gaps", [])
    ]
    budgets = [projection["budget"] for projection in projections]
    max_timeout = max((int(budget["timeout_seconds"]) for budget in budgets), default=0)
    total_runtime_invocations = sum(int(budget["max_runtime_invocations"]) for budget in budgets)
    total_model_calls = sum(int(budget["max_model_calls"]) for budget in budgets)
    total_tool_calls = sum(int(budget["max_tool_calls"]) for budget in budgets)
    total_cpu_seconds = sum(int(budget["max_cpu_seconds"] or 0) for budget in budgets)
    max_memory_mb = max((int(budget["max_memory_mb"] or 0) for budget in budgets), default=0)
    cost_policy_counts: dict[str, int] = {}
    filesystem_scope_counts: dict[str, int] = {}
    for budget in budgets:
        cost_policy = str(budget["cost_policy"])
        filesystem_scope = str(budget["filesystem_scope"])
        cost_policy_counts[cost_policy] = cost_policy_counts.get(cost_policy, 0) + 1
        filesystem_scope_counts[filesystem_scope] = filesystem_scope_counts.get(filesystem_scope, 0) + 1

    status = "pass" if not gaps else "fail"
    return _check(
        "delegate_budget_efficiency",
        status,
        "Delegate budgets bound runtime calls, model/tool use, filesystem scope, cost policy, and branch fan-out."
        if status == "pass"
        else "One or more delegate budgets are missing or misaligned with the adapter boundary.",
        measurements={
            "adapter_count": len(projections),
            "budget_schema_version": "harness.delegate_budget/v1",
            "max_timeout_seconds": max_timeout,
            "total_runtime_invocation_ceiling": total_runtime_invocations,
            "total_model_call_ceiling": total_model_calls,
            "total_tool_call_ceiling": total_tool_calls,
            "total_cpu_seconds_ceiling": total_cpu_seconds,
            "max_memory_mb_ceiling": max_memory_mb,
            "cost_policy_counts": cost_policy_counts,
            "filesystem_scope_counts": filesystem_scope_counts,
            "adapters": projections,
            "provider_called": False,
            "network_called": False,
            "adapter_execution_started": False,
        },
        gaps=gaps,
        next_actions=[]
        if status == "pass"
        else ["Add explicit delegate budgets and keep sandbox, network, filesystem, and cost policies aligned."],
    )


@dataclass(frozen=True)
class _LiveBenchmarkPermitSpec:
    benchmark_id: str
    purpose: str
    required_approval_backend: str
    required_approval_data_boundary: str
    required_task_type: str
    approval_command: str
    live_measurement_path: str
    adapter_id: str | None = None
    required_autonomy_scope: str | None = None
    max_timeout_seconds: int = 0
    max_runtime_invocations: int = 0
    max_model_calls: int = 0
    max_tool_calls: int = 0
    max_parallel_branches: int = 1
    filesystem_scope: str = "harness_artifacts"
    network_policy: str = "forbidden"
    active_repo_write: str = "forbidden"
    provider_call_required: bool = False
    sandbox_start_required: bool = False
    live_runner_available: bool = False
    active_repo_mutation_allowed: bool = False
    release_blocking: bool = False
    reference_patterns: tuple[str, ...] = ()


def _live_benchmark_permits_check(project_root: Path) -> OrchestrationEfficiencyCheck:
    permits = [_live_benchmark_permit_projection(project_root, spec.benchmark_id) for spec in _live_benchmark_specs()]
    missing_contracts = [
        permit["benchmark_id"]
        for permit in permits
        if not permit.get("required_approval") or not permit.get("budget") or not permit.get("boundaries")
    ]
    status = "pass" if not missing_contracts else "fail"
    approval_required_count = sum(1 for permit in permits if permit["status"] == "approval_required")
    return _check(
        "live_benchmark_permits",
        status,
        "Live-only orchestration benchmarks have explicit approval, budget, boundary, and non-release-gate permit contracts."
        if status == "pass"
        else "One or more live-only orchestration benchmarks lack an approval or boundary permit contract.",
        measurements={
            "schema_version": LIVE_BENCHMARK_PERMITS_SCHEMA_VERSION,
            "permit_count": len(permits),
            "approval_ready_count": sum(1 for permit in permits if permit["approval_ready"] is True),
            "approval_required_count": approval_required_count,
            "live_runner_available_count": sum(1 for permit in permits if permit["live_execution"]["runner_available"]),
            "release_blocking_count": sum(1 for permit in permits if permit["release_blocking"]),
            "automated_execution_allowed_count": sum(
                1 for permit in permits if permit["live_execution"]["automated_execution_allowed"]
            ),
            "adapter_execution_started": False,
            "provider_called": False,
            "network_called": False,
            "filesystem_modified": False,
            "permission_granting": False,
            "artifact_bodies_read": False,
            "permits": permits,
        },
        gaps=[f"{benchmark_id}: missing live benchmark permit contract fields" for benchmark_id in missing_contracts],
        next_actions=[
            permit["required_approval"]["approval_command"]
            for permit in permits
            if permit["status"] == "approval_required"
        ][:3],
    )


def _live_benchmark_permit_projection(project_root: Path, benchmark_id: str) -> dict[str, Any]:
    spec = _live_benchmark_spec(benchmark_id)
    approval = ApprovalStore(project_root).find_valid(
        spec.required_approval_backend,
        spec.required_approval_data_boundary,
        spec.required_task_type,
        adapter_id=spec.adapter_id,
        autonomy_scope=spec.required_autonomy_scope,
        strict_scope=spec.required_autonomy_scope is not None,
    )
    approval_ready = approval is not None
    return {
        "schema_version": LIVE_BENCHMARK_PERMIT_SCHEMA_VERSION,
        "benchmark_id": spec.benchmark_id,
        "status": "approval_ready" if approval_ready else "approval_required",
        "approval_ready": approval_ready,
        "purpose": spec.purpose,
        "reference_patterns": list(spec.reference_patterns),
        "required_approval": {
            "backend": spec.required_approval_backend,
            "data_boundary": spec.required_approval_data_boundary,
            "task_type": spec.required_task_type,
            "adapter_id": spec.adapter_id,
            "autonomy_scope": spec.required_autonomy_scope,
            "strict_scope": spec.required_autonomy_scope is not None,
            "approval_id": approval.id if approval is not None else None,
            "approval_expires_at": approval.expires_at.isoformat() if approval is not None else None,
            "approval_reason_present": bool(approval.reason) if approval is not None else False,
            "approval_command": spec.approval_command,
        },
        "budget": {
            "max_timeout_seconds": spec.max_timeout_seconds,
            "max_runtime_invocations": spec.max_runtime_invocations,
            "max_model_calls": spec.max_model_calls,
            "max_tool_calls": spec.max_tool_calls,
            "max_parallel_branches": spec.max_parallel_branches,
        },
        "boundaries": {
            "filesystem_scope": spec.filesystem_scope,
            "network_policy": spec.network_policy,
            "active_repo_write": spec.active_repo_write,
            "active_repo_mutation_allowed": spec.active_repo_mutation_allowed,
            "provider_call_required": spec.provider_call_required,
            "sandbox_start_required": spec.sandbox_start_required,
        },
        "live_execution": {
            "runner_available": spec.live_runner_available,
            "automated_execution_allowed": False,
            "adapter_execution_started": False,
            "provider_called": False,
            "network_called": False,
            "filesystem_modified": False,
            "permission_granting": False,
            "live_measurement_path": spec.live_measurement_path,
            "passive_suite_behavior": "skipped_explicit_live_required",
        },
        "release_blocking": spec.release_blocking,
        "authority": {
            "read_only_projection": True,
            "approval_granting": False,
            "permission_granting": False,
            "provider_execution": False,
            "adapter_execution": False,
            "network_execution": False,
            "filesystem_mutation": False,
        },
    }


def _live_benchmark_spec(benchmark_id: str) -> _LiveBenchmarkPermitSpec:
    for spec in _live_benchmark_specs():
        if spec.benchmark_id == benchmark_id:
            return spec
    raise KeyError(f"Unknown live benchmark id: {benchmark_id}")


def _live_benchmark_specs() -> tuple[_LiveBenchmarkPermitSpec, ...]:
    descriptors = {descriptor.id: descriptor for descriptor in list_execution_adapter_descriptors()}
    isolated_edit = descriptors.get("codex_isolated_edit")
    isolated_budget = isolated_edit.delegate_budget if isolated_edit is not None else None
    return (
        _LiveBenchmarkPermitSpec(
            benchmark_id="sandbox_startup",
            purpose="Measure startup latency for an approved isolated-workspace Codex adapter run.",
            required_approval_backend="codex_cli",
            required_approval_data_boundary="hosted_provider",
            required_task_type="codex_code_edit",
            adapter_id="codex_isolated_edit",
            required_autonomy_scope="supervised-codex",
            max_timeout_seconds=isolated_budget.timeout_seconds if isolated_budget is not None else 1800,
            max_runtime_invocations=isolated_budget.max_runtime_invocations if isolated_budget is not None else 1,
            max_model_calls=isolated_budget.max_model_calls if isolated_budget is not None else 0,
            max_tool_calls=isolated_budget.max_tool_calls if isolated_budget is not None else 0,
            max_parallel_branches=isolated_budget.max_parallel_branches if isolated_budget is not None else 1,
            filesystem_scope=isolated_budget.filesystem_scope if isolated_budget is not None else "isolated_workspace",
            network_policy=isolated_budget.network_policy.value if isolated_budget is not None else "forbidden",
            active_repo_write=isolated_budget.active_repo_write.value
            if isolated_budget is not None
            else "approval_required",
            provider_call_required=True,
            sandbox_start_required=True,
            active_repo_mutation_allowed=False,
            reference_patterns=("microsoft_agent_framework", "openai_agents", "firecracker", "gvisor"),
            live_measurement_path="approved codex_isolated_edit task plus run trace timing inspection",
            approval_command=(
                "harness approvals add --backend codex_cli --data-boundary hosted_provider "
                "--task-types codex_code_edit --allowed-adapters codex_isolated_edit "
                "--autonomy-scope supervised-codex --max-runs 1 --duration-hours 1"
            ),
        ),
        _LiveBenchmarkPermitSpec(
            benchmark_id="shared_llm_contention",
            purpose="Measure provider-pool queueing with a bounded, approved model-contention benchmark.",
            required_approval_backend="harness_evals",
            required_approval_data_boundary="provider_model_pool",
            required_task_type="shared_llm_contention_benchmark",
            required_autonomy_scope="benchmark-only",
            max_timeout_seconds=120,
            max_runtime_invocations=1,
            max_model_calls=4,
            max_tool_calls=0,
            max_parallel_branches=2,
            filesystem_scope="harness_artifacts",
            network_policy="provider_only",
            active_repo_write="forbidden",
            provider_call_required=True,
            sandbox_start_required=False,
            active_repo_mutation_allowed=False,
            reference_patterns=("microsoft_agent_framework", "temporal", "openai_agents", "opentelemetry"),
            live_measurement_path="approved provider-backed contention benchmark outside passive release gates",
            approval_command=(
                "harness approvals add --backend harness_evals --data-boundary provider_model_pool "
                "--task-types shared_llm_contention_benchmark --autonomy-scope benchmark-only "
                "--max-runs 1 --duration-hours 1"
            ),
        ),
    )


def _microbenchmark_contracts_check(project_root: Path) -> OrchestrationEfficiencyCheck:
    """Expose the reference-recommended benchmark matrix without live execution.

    The research recommendation is broader than a single scheduler probe. This
    check keeps the benchmark contract visible in release output while staying
    passive: it records synthetic/passive proxies and clearly marks benchmarks
    that require a later explicit live run.
    """

    descriptors = list_execution_adapter_descriptors()
    budgets = [descriptor.delegate_budget for descriptor in descriptors]
    bounded = _simulate_probe_schedule(_PROBE_TASKS, max_parallel=2)
    serial = _simulate_probe_schedule(_PROBE_TASKS, max_parallel=1)
    critical_path = _critical_path_duration(_PROBE_TASKS)
    evidence_dir = project_root / HARNESS_DIR / "autonomy" / "objectives"
    evidence_files = list(evidence_dir.glob("*.jsonl")) if evidence_dir.exists() else []
    objective_evidence_files = len(
        [path for path in evidence_files if not path.name.endswith(".checkpoints.jsonl")]
    )
    checkpoint_files = len([path for path in evidence_files if path.name.endswith(".checkpoints.jsonl")])
    isolated_workspace_adapters = [
        descriptor.id
        for descriptor in descriptors
        if descriptor.delegate_budget.filesystem_scope == "isolated_workspace"
    ]
    review_gate_adapters = [
        descriptor.id
        for descriptor in descriptors
        if "review_report" in " ".join(descriptor.terminal_evidence_required)
    ]
    tool_budget_adapters = [
        descriptor.id
        for descriptor in descriptors
        if descriptor.delegate_budget.max_tool_calls > 0 or descriptor.delegate_budget.tool_allowlist
    ]
    benchmark_rows = [
        _microbenchmark_contract(
            "handoff_overhead",
            measurement_mode="passive_proxy",
            source_checks=["adapter_security_complexity_tradeoff", "delegate_budget_efficiency"],
            metrics=[
                "registered_adapter_count",
                "budgeted_adapter_count",
                "runtime_invocation_ceiling",
            ],
            passive_measurements={
                "registered_adapter_count": len(descriptors),
                "budgeted_adapter_count": len(budgets),
                "runtime_invocation_ceiling": sum(budget.max_runtime_invocations for budget in budgets),
            },
        ),
        _microbenchmark_contract(
            "fanout_fanin_critical_path",
            measurement_mode="synthetic_probe",
            source_checks=["bounded_critical_path_scheduler"],
            metrics=[
                "serial_duration_units",
                "bounded_duration_units",
                "critical_path_units",
                "bounded_max_active",
            ],
            passive_measurements={
                "serial_duration_units": serial["duration_units"],
                "bounded_duration_units": bounded["duration_units"],
                "critical_path_units": critical_path,
                "bounded_max_active": bounded["max_active"],
            },
        ),
        _microbenchmark_contract(
            "checkpoint_latency",
            measurement_mode="passive_existing_evidence",
            source_checks=["supervisor_checkpoints", "evidence_trace_projection_cost"],
            metrics=["checkpoint_file_count", "objective_evidence_file_count", "verify_command_available"],
            passive_measurements={
                "checkpoint_file_count": checkpoint_files,
                "objective_evidence_file_count": objective_evidence_files,
                "verify_command_available": True,
            },
            explicit_command="harness objectives checkpoints verify <objective_id> --project . --output json",
        ),
        _microbenchmark_contract(
            "sandbox_startup",
            measurement_mode="explicit_live_required",
            source_checks=["delegate_budget_efficiency", "sandboxed_registered_adapters"],
            metrics=["isolated_workspace_adapter_count", "sandbox_contract_available"],
            passive_measurements={
                "isolated_workspace_adapter_count": len(isolated_workspace_adapters),
                "isolated_workspace_adapters": isolated_workspace_adapters,
                "sandbox_contract_available": bool(isolated_workspace_adapters),
            },
            explicit_measurement_path="approved codex_isolated_edit task plus run trace timing inspection",
            live_permit=_live_benchmark_permit_projection(project_root, "sandbox_startup"),
        ),
        _microbenchmark_contract(
            "tool_adapter_overhead",
            measurement_mode="passive_proxy",
            source_checks=["protocol_and_tool_exposure", "delegate_budget_efficiency"],
            metrics=["tool_budget_adapter_count", "declared_tool_allowlist_count"],
            passive_measurements={
                "tool_budget_adapter_count": len(tool_budget_adapters),
                "declared_tool_allowlist_count": sum(len(budget.tool_allowlist) for budget in budgets),
            },
        ),
        _microbenchmark_contract(
            "retry_safety",
            measurement_mode="passive_policy",
            source_checks=["replay_retry_idempotency"],
            metrics=["replay_policy_coverage", "autonomous_retry_ceiling"],
            passive_measurements={
                "replay_policy_coverage": len(descriptors),
                "autonomous_retry_ceiling": sum(descriptor.max_autonomous_retries for descriptor in descriptors),
            },
        ),
        _microbenchmark_contract(
            "trace_overhead",
            measurement_mode="passive_existing_evidence",
            source_checks=["evidence_trace_projection_cost"],
            metrics=["objective_trace_projection", "run_trace_projection", "artifact_bodies_read"],
            passive_measurements={
                "objective_evidence_file_count": objective_evidence_files,
                "artifact_bodies_read": False,
            },
        ),
        _microbenchmark_contract(
            "shared_llm_contention",
            measurement_mode="explicit_live_required",
            source_checks=["delegate_budget_efficiency"],
            metrics=["model_call_ceiling", "runtime_invocation_ceiling", "provider_call_required_for_live_measurement"],
            passive_measurements={
                "model_call_ceiling": sum(budget.max_model_calls for budget in budgets),
                "runtime_invocation_ceiling": sum(budget.max_runtime_invocations for budget in budgets),
                "provider_call_required_for_live_measurement": True,
            },
            explicit_measurement_path="approved provider-backed contention benchmark outside passive release gates",
            live_permit=_live_benchmark_permit_projection(project_root, "shared_llm_contention"),
        ),
        _microbenchmark_contract(
            "verification_stage_roi",
            measurement_mode="passive_proxy",
            source_checks=["applyback_governance", "replay_retry_idempotency"],
            metrics=["review_gate_adapter_count", "review_gate_evidence_required"],
            passive_measurements={
                "review_gate_adapter_count": len(review_gate_adapters),
                "review_gate_adapters": review_gate_adapters,
                "review_gate_evidence_required": bool(review_gate_adapters),
            },
        ),
    ]
    missing_contracts = [
        row["id"]
        for row in benchmark_rows
        if not row.get("metrics") or not row.get("source_checks") or not row.get("measurement_mode")
    ]
    status = "pass" if not missing_contracts else "fail"
    return _check(
        "microbenchmark_contracts",
        status,
        "Reference-recommended orchestration microbenchmarks are represented as passive, synthetic, or explicit live contracts."
        if status == "pass"
        else "One or more orchestration microbenchmarks lack a measurement contract.",
        measurements={
            "schema_version": "harness.orchestration_microbenchmark_contracts/v1",
            "benchmark_count": len(benchmark_rows),
            "passive_or_synthetic_count": sum(
                1 for row in benchmark_rows if row["measurement_mode"] != "explicit_live_required"
            ),
            "explicit_live_required_count": sum(
                1 for row in benchmark_rows if row["measurement_mode"] == "explicit_live_required"
            ),
            "adapter_execution_started": False,
            "provider_called": False,
            "network_called": False,
            "filesystem_modified": False,
            "artifact_bodies_read": False,
            "benchmarks": benchmark_rows,
        },
        gaps=[f"{benchmark_id}: missing benchmark contract fields" for benchmark_id in missing_contracts],
        next_actions=[]
        if status == "pass"
        else ["Add source checks, metrics, and a passive/synthetic/explicit-live measurement mode for each benchmark."],
    )


def _replay_retry_idempotency_check(project_root: Path) -> OrchestrationEfficiencyCheck:
    rows: list[dict[str, Any]] = []
    gaps: list[str] = []
    safe_replay_policies = {ToolReplayPolicy.SAFE, ToolReplayPolicy.IDEMPOTENT_WITH_KEY}
    retry_path_policy_enforced = _retry_path_policy_enforced()
    task_replay_receipts_enforced = _task_replay_receipts_enforced()
    daemon_prelease_descriptor_approval_enforced = _daemon_prelease_descriptor_approval_enforced()
    existing_receipts = _existing_task_replay_receipt_measurements(project_root)
    if not retry_path_policy_enforced:
        gaps.append("SQLiteStore.retry_task does not enforce registered adapter replay policy")
    if not task_replay_receipts_enforced:
        gaps.append("task retry and attempt leasing do not emit durable replay receipts")
    if not daemon_prelease_descriptor_approval_enforced:
        gaps.append("SQLiteStore.daemon_task_eligibility does not pause descriptor-required approvals before leasing")
    gaps.extend(existing_receipts["gaps"])
    for descriptor in list_execution_adapter_descriptors():
        row_gaps: list[str] = []
        if descriptor.max_autonomous_retries < 0:
            row_gaps.append("max_autonomous_retries is negative")
        if descriptor.max_autonomous_retries > 0 and descriptor.replay_policy not in safe_replay_policies:
            row_gaps.append("autonomous retries require a safe or idempotent replay policy")
        if descriptor.autonomy_default == "auto_allowed" and descriptor.replay_policy not in safe_replay_policies:
            row_gaps.append("auto-allowed adapters require safe or idempotent replay")
        if descriptor.replay_policy == ToolReplayPolicy.NOT_REPLAYABLE and descriptor.autonomy_default != "forbidden":
            row_gaps.append("not_replayable adapters must not be autonomously dispatchable")
        gaps.extend(f"{descriptor.id}: {gap}" for gap in row_gaps)
        rows.append(
            {
                "adapter_id": descriptor.id,
                "autonomy_default": descriptor.autonomy_default,
                "replay_policy": descriptor.replay_policy.value,
                "max_autonomous_retries": descriptor.max_autonomous_retries,
                "retry_safe": not row_gaps,
                "gaps": row_gaps,
            }
        )
    status = "pass" if not gaps else "fail"
    return _check(
        "replay_retry_idempotency",
        status,
        "Replay and retry policy prevents duplicate side effects under redelivery or retry."
        if status == "pass"
        else "Replay or retry policy permits duplicate side-effect risk.",
        measurements={
            "adapter_count": len(rows),
            "autonomous_retry_adapter_count": sum(1 for row in rows if int(row["max_autonomous_retries"]) > 0),
            "auto_allowed_adapter_count": sum(1 for row in rows if row["autonomy_default"] == "auto_allowed"),
            "task_retry_replay_policy_enforced": retry_path_policy_enforced,
            "task_replay_receipts_enforced": task_replay_receipts_enforced,
            "existing_task_replay_receipts": existing_receipts,
            "daemon_prelease_descriptor_approval_enforced": daemon_prelease_descriptor_approval_enforced,
            "adapters": rows,
        },
        gaps=gaps,
        next_actions=[]
        if status == "pass"
        else ["Use idempotency keys for autonomous retries or require fresh approval before replay."],
    )


def _retry_path_policy_enforced() -> bool:
    try:
        retry_source = inspect.getsource(SQLiteStore.retry_task)
        guard_source = inspect.getsource(SQLiteStore._task_retry_replay_rejection)
    except (OSError, TypeError):
        return False
    return (
        "_task_retry_replay_rejection" in retry_source
        and "ToolReplayPolicy.NOT_REPLAYABLE" in guard_source
        and "ToolReplayPolicy.REQUIRES_FRESH_APPROVAL" in guard_source
    )


def _task_replay_receipts_enforced() -> bool:
    try:
        retry_source = inspect.getsource(SQLiteStore.retry_task)
        lease_source = inspect.getsource(SQLiteStore._lease_task_in_conn)
        retry_receipt_source = inspect.getsource(SQLiteStore._task_retry_replay_receipt)
        attempt_receipt_source = inspect.getsource(SQLiteStore._attempt_replay_receipt)
    except (OSError, TypeError, AttributeError):
        return False
    return (
        TASK_REPLAY_RECEIPT_SCHEMA_VERSION == "harness.task_replay_receipt/v1"
        and "task_retry_authorized" in retry_source
        and "_task_retry_replay_receipt" in retry_source
        and '"replay_receipt": replay_receipt' in retry_source
        and "_attempt_replay_receipt" in lease_source
        and "active_lease_exclusion_before_attempt_insert" in retry_receipt_source
        and "active_lease_exclusion_before_attempt_insert" in attempt_receipt_source
        and "prior_attempt_count" in retry_receipt_source
        and "prior_attempt_count" in attempt_receipt_source
    )


def _existing_task_replay_receipt_measurements(project_root: Path) -> dict[str, Any]:
    sqlite_path = project_root / HARNESS_DIR / "harness.sqlite"
    if not sqlite_path.exists():
        return {
            "initialized": False,
            "attempt_count": 0,
            "receipt_count": 0,
            "legacy_missing_receipt_count": 0,
            "gaps": [],
        }
    store = SQLiteStore(project_root)
    attempts = store.list_task_attempts()
    gaps: list[str] = []
    receipt_count = 0
    legacy_missing = 0
    for attempt in attempts:
        receipt = attempt.metadata.get("replay_receipt")
        if receipt is None:
            legacy_missing += 1
            continue
        receipt_count += 1
        if not isinstance(receipt, dict):
            gaps.append(f"{attempt.id}: replay_receipt_not_object")
            continue
        expected = {
            "schema_version": TASK_REPLAY_RECEIPT_SCHEMA_VERSION,
            "receipt_kind": "attempt_replay_guard",
            "task_id": attempt.task_id,
            "attempt_number": attempt.attempt_number,
            "prior_attempt_count": max(0, attempt.attempt_number - 1),
        }
        for field, expected_value in expected.items():
            if receipt.get(field) != expected_value:
                gaps.append(f"{attempt.id}: {field}_mismatch")
        attempt_idempotency_key = attempt.metadata.get("attempt_idempotency_key")
        if receipt.get("attempt_idempotency_key") != attempt_idempotency_key:
            gaps.append(f"{attempt.id}: attempt_idempotency_key_mismatch")
        if receipt.get("active_lease_duplicate_guard") != "active_lease_exclusion_before_attempt_insert":
            gaps.append(f"{attempt.id}: active_lease_duplicate_guard_mismatch")
    return {
        "initialized": True,
        "attempt_count": len(attempts),
        "receipt_count": receipt_count,
        "legacy_missing_receipt_count": legacy_missing,
        "gaps": gaps,
    }


def _daemon_prelease_descriptor_approval_enforced() -> bool:
    try:
        eligibility_source = inspect.getsource(SQLiteStore.daemon_task_eligibility)
        guard_source = inspect.getsource(SQLiteStore._daemon_registered_adapter_pause)
        approval_source = inspect.getsource(SQLiteStore._missing_registered_adapter_approvals)
    except (OSError, TypeError, AttributeError):
        return False
    return (
        "_daemon_registered_adapter_pause" in eligibility_source
        and "waiting_approval" in guard_source
        and "approval_source" in guard_source
        and "execution_adapter_descriptor" in guard_source
        and "ApprovalStore" in approval_source
        and "adapter_id=descriptor.id" in approval_source
    )


def _evidence_trace_projection_cost_check(project_root: Path) -> OrchestrationEfficiencyCheck:
    sqlite_path = project_root / HARNESS_DIR / "harness.sqlite"
    if not sqlite_path.exists():
        return _check(
            "evidence_trace_projection_cost",
            "skipped",
            "No initialized Harness runtime state exists; runtime evidence and trace cost were not measured.",
            measurements={"initialized": False, "sqlite_path": str(sqlite_path)},
        )

    store = SQLiteStore(project_root)
    objective_measurements, objective_failures = _objective_evidence_trace_measurements(project_root, store)
    run_measurements, run_failures = _run_trace_measurements(project_root, store)
    failures = objective_failures + run_failures
    status = "pass" if not failures else "fail"
    return _check(
        "evidence_trace_projection_cost",
        status,
        "Existing objective and run evidence exports as bounded metadata-only trace projections."
        if status == "pass"
        else "Runtime evidence or trace projection measurement failed.",
        measurements={
            "initialized": True,
            "objectives": objective_measurements,
            "runs": run_measurements,
            "artifact_bodies_read": False,
            "adapter_execution_started": False,
            "provider_called": False,
            "network_called": False,
        },
        gaps=failures,
        next_actions=[]
        if status == "pass"
        else ["Run the named evidence or trace export command for the failing objective or run id."],
    )


def _objective_evidence_trace_measurements(
    project_root: Path,
    store: SQLiteStore,
) -> tuple[dict[str, Any], list[str]]:
    evidence_dir = project_root / HARNESS_DIR / "autonomy" / "objectives"
    objective_ids = {objective.id for objective in store.list_objectives()}
    if evidence_dir.exists():
        objective_ids.update(path.stem for path in evidence_dir.glob("*.jsonl"))
    objective_ids = sorted(
        objective_id for objective_id in objective_ids if (evidence_dir / f"{objective_id}.jsonl").exists()
    )
    failures: list[str] = []
    rows: list[dict[str, Any]] = []
    total_events = 0
    total_trace_spans = 0
    total_bytes = 0
    for objective_id in objective_ids:
        evidence_path = evidence_dir / f"{objective_id}.jsonl"
        line_count, size_bytes = _jsonl_measurement(evidence_path)
        total_events += line_count
        total_bytes += size_bytes
        try:
            verification = verify_objective_evidence(project_root, objective_id)
        except Exception as exc:
            failures.append(f"{objective_id}: objective_evidence_verify_error:{exc.__class__.__name__}")
            rows.append(
                {
                    "objective_id": objective_id,
                    "evidence_events": line_count,
                    "evidence_size_bytes": size_bytes,
                    "verification_ok": False,
                    "trace_ok": False,
                    "trace_spans": 0,
                }
            )
            continue
        try:
            trace = export_objective_trace(project_root, store, objective_id)
        except Exception as exc:
            failures.append(f"{objective_id}: objective_trace_export_error:{exc.__class__.__name__}")
            trace_ok = False
            trace_spans = 0
        else:
            trace_ok = trace.ok
            trace_spans = len(trace.spans)
            total_trace_spans += trace_spans
            if not trace.ok:
                failures.append(f"{objective_id}: objective_trace_export_not_ok")
        if not verification.ok:
            failures.append(f"{objective_id}: objective_evidence_not_ok")
        rows.append(
            {
                "objective_id": objective_id,
                "evidence_events": line_count,
                "evidence_size_bytes": size_bytes,
                "verification_ok": verification.ok,
                "verification_summary": verification.summary,
                "trace_ok": trace_ok,
                "trace_spans": trace_spans,
            }
        )
    return (
        {
            "objective_evidence_files": len(objective_ids),
            "total_evidence_events": total_events,
            "total_evidence_size_bytes": total_bytes,
            "total_trace_spans": total_trace_spans,
            "span_per_event_ratio": round(total_trace_spans / total_events, 3) if total_events else None,
            "items": rows,
        },
        failures,
    )


def _run_trace_measurements(project_root: Path, store: SQLiteStore) -> tuple[dict[str, Any], list[str]]:
    failures: list[str] = []
    rows: list[dict[str, Any]] = []
    total_events = 0
    total_trace_spans = 0
    for run in store.list_runs():
        event_count = len(store.list_events(run.id))
        total_events += event_count
        try:
            trace = export_run_trace(project_root, store, run.id)
        except Exception as exc:
            failures.append(f"{run.id}: run_trace_export_error:{exc.__class__.__name__}")
            rows.append(
                {
                    "run_id": run.id,
                    "status": run.status,
                    "events": event_count,
                    "trace_ok": False,
                    "trace_spans": 0,
                }
            )
            continue
        total_trace_spans += len(trace.spans)
        if not trace.ok:
            failures.append(f"{run.id}: run_trace_export_not_ok")
        rows.append(
            {
                "run_id": run.id,
                "status": run.status,
                "events": event_count,
                "trace_ok": trace.ok,
                "trace_spans": len(trace.spans),
            }
        )
    return (
        {
            "run_count": len(rows),
            "total_run_events": total_events,
            "total_trace_spans": total_trace_spans,
            "span_per_event_ratio": round(total_trace_spans / total_events, 3) if total_events else None,
        },
        failures,
    )


def _adapter_security_complexity_gaps(
    descriptor: ExecutionAdapterDescriptor,
    profile: SandboxProfileDescriptor | None,
) -> list[str]:
    gaps: list[str] = []
    if profile is None:
        gaps.append("missing sandbox profile")
        return gaps
    if profile.network == SandboxNetworkPolicy.ALLOWED:
        gaps.append("sandbox network policy is allowed")
    if profile.active_repo_write == SandboxActiveRepoWritePolicy.APPROVAL_REQUIRED and not descriptor.required_approvals:
        gaps.append("active repository write boundary requires adapter approval evidence")
    if _external_boundary_required(descriptor) and not descriptor.required_approvals:
        gaps.append("external or hosted backend requirement lacks required approval")
    if descriptor.autonomy_default == "auto_allowed" and descriptor.replay_policy not in {
        ToolReplayPolicy.SAFE,
        ToolReplayPolicy.IDEMPOTENT_WITH_KEY,
    }:
        gaps.append("auto-allowed adapter is not replay safe")
    if descriptor.sandbox_profile_id in {None, ""}:
        gaps.append("adapter does not name a sandbox profile")
    return gaps


def _adapter_complexity_reasons(
    descriptor: ExecutionAdapterDescriptor,
    profile: SandboxProfileDescriptor | None,
) -> list[str]:
    reasons = []
    if descriptor.backend_requirements:
        reasons.append("backend_requirement")
    if descriptor.sandbox_requirements:
        reasons.append("sandbox_requirement")
    if descriptor.required_approvals:
        reasons.append("approval_boundary")
    if descriptor.autonomy_default != "auto_allowed":
        reasons.append(f"autonomy_{descriptor.autonomy_default}")
    if descriptor.replay_policy != ToolReplayPolicy.IDEMPOTENT_WITH_KEY:
        reasons.append(f"replay_{descriptor.replay_policy.value}")
    if profile is None:
        reasons.append("missing_sandbox_profile")
    else:
        if profile.tier not in {SandboxTier.NONE, SandboxTier.READ_ONLY}:
            reasons.append(f"sandbox_{profile.tier.value}")
        if profile.active_repo_write != SandboxActiveRepoWritePolicy.FORBIDDEN:
            reasons.append("active_repo_write_boundary")
        if profile.network != SandboxNetworkPolicy.FORBIDDEN:
            reasons.append("network_boundary")
    if _external_boundary_required(descriptor):
        reasons.append("external_or_hosted_boundary")
    return reasons


def _adapter_security_controls(
    descriptor: ExecutionAdapterDescriptor,
    profile: SandboxProfileDescriptor | None,
) -> list[str]:
    controls = []
    if profile is not None:
        controls.append("sandbox_profile_declared")
        if profile.network == SandboxNetworkPolicy.FORBIDDEN:
            controls.append("network_forbidden")
        if profile.active_repo_write == SandboxActiveRepoWritePolicy.FORBIDDEN:
            controls.append("active_repo_write_forbidden")
        elif profile.active_repo_write == SandboxActiveRepoWritePolicy.APPROVAL_REQUIRED:
            controls.append("active_repo_write_approval_required")
    if descriptor.required_approvals:
        controls.append("required_approval")
    if descriptor.autonomy_default in {"approval_required", "forbidden"}:
        controls.append(f"autonomy_{descriptor.autonomy_default}")
    if descriptor.replay_policy in {ToolReplayPolicy.SAFE, ToolReplayPolicy.IDEMPOTENT_WITH_KEY}:
        controls.append("replay_safe")
    elif descriptor.replay_policy == ToolReplayPolicy.REQUIRES_FRESH_APPROVAL:
        controls.append("fresh_approval_replay")
    elif descriptor.replay_policy == ToolReplayPolicy.NOT_REPLAYABLE:
        controls.append("not_replayable")
    if descriptor.max_autonomous_retries == 0:
        controls.append("autonomous_retries_disabled")
    return controls


def _external_boundary_required(descriptor: ExecutionAdapterDescriptor) -> bool:
    text = " ".join(descriptor.backend_requirements + descriptor.sandbox_requirements).lower()
    return any(
        marker in text
        for marker in (
            "hosted",
            "paid",
            "external_network",
            "external network",
            "subscription",
            "mixed",
        )
    )


def _sandbox_profile(descriptor: ExecutionAdapterDescriptor) -> SandboxProfileDescriptor | None:
    if not descriptor.sandbox_profile_id:
        return None
    try:
        return get_sandbox_profile(descriptor.sandbox_profile_id)
    except KeyError:
        return None


@dataclass(frozen=True)
class _ProbeTask:
    id: str
    duration_units: int
    depends_on: tuple[str, ...] = ()


_PROBE_TASKS = (
    _ProbeTask("plan", 3),
    _ProbeTask("repo_context", 2, ("plan",)),
    _ProbeTask("tool_contracts", 2, ("plan",)),
    _ProbeTask("implementation", 6, ("repo_context", "tool_contracts")),
    _ProbeTask("security_review", 3, ("implementation",)),
    _ProbeTask("docs", 2, ("implementation",)),
    _ProbeTask("final_synthesis", 2, ("security_review", "docs")),
)


def _simulate_probe_schedule(tasks: Iterable[_ProbeTask], *, max_parallel: int) -> dict[str, Any]:
    tasks_by_id = {task.id: task for task in tasks}
    critical_remaining = _critical_path_by_task(tasks_by_id)
    completed: set[str] = set()
    started: set[str] = set()
    in_flight: list[tuple[int, str]] = []
    now = 0
    batches: list[dict[str, Any]] = []
    max_active = 0

    while len(completed) < len(tasks_by_id):
        ready = [
            task
            for task in tasks_by_id.values()
            if task.id not in started and all(parent in completed for parent in task.depends_on)
        ]
        ready.sort(key=lambda task: (-critical_remaining[task.id], task.id))
        selected: list[str] = []
        while ready and len(in_flight) < max_parallel:
            task = ready.pop(0)
            selected.append(task.id)
            started.add(task.id)
            in_flight.append((now + task.duration_units, task.id))
        if selected:
            batches.append({"at": now, "selected": selected, "active_after_start": len(in_flight)})
            max_active = max(max_active, len(in_flight))
        if not in_flight:
            return {
                "ok": False,
                "duration_units": now,
                "max_active": max_active,
                "batches": batches,
                "error": "cycle_or_unschedulable_probe",
            }
        next_time = min(end for end, _task_id in in_flight)
        now = next_time
        completed_now = sorted(task_id for end, task_id in in_flight if end == next_time)
        completed.update(completed_now)
        in_flight = [(end, task_id) for end, task_id in in_flight if end != next_time]

    return {"ok": True, "duration_units": now, "max_active": max_active, "batches": batches}


def _critical_path_duration(tasks: Iterable[_ProbeTask]) -> int:
    tasks_by_id = {task.id: task for task in tasks}
    by_task = _critical_path_by_task(tasks_by_id)
    return max(by_task.values()) if by_task else 0


def _critical_path_by_task(tasks_by_id: dict[str, _ProbeTask]) -> dict[str, int]:
    children: dict[str, list[str]] = {task_id: [] for task_id in tasks_by_id}
    for task in tasks_by_id.values():
        for parent in task.depends_on:
            children.setdefault(parent, []).append(task.id)

    memo: dict[str, int] = {}

    def visit(task_id: str) -> int:
        if task_id in memo:
            return memo[task_id]
        task = tasks_by_id[task_id]
        child_duration = max((visit(child) for child in children.get(task_id, [])), default=0)
        memo[task_id] = task.duration_units + child_duration
        return memo[task_id]

    for task_id in tasks_by_id:
        visit(task_id)
    return memo


def _jsonl_measurement(path: Path) -> tuple[int, int]:
    try:
        contents = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return (0, 0)
    line_count = sum(1 for line in contents.splitlines() if line.strip())
    return (line_count, len(contents.encode("utf-8")))


def _summary(checks: list[OrchestrationEfficiencyCheck]) -> dict[str, int]:
    return {
        "total": len(checks),
        "pass": sum(1 for check in checks if check.status == "pass"),
        "warning": sum(1 for check in checks if check.status == "warning"),
        "fail": sum(1 for check in checks if check.status == "fail"),
        "skipped": sum(1 for check in checks if check.status == "skipped"),
    }


def _safety_flags() -> dict[str, bool]:
    return {
        "read_only": True,
        "synthetic_probe_only": True,
        "adapter_execution_started": False,
        "filesystem_modified": False,
        "filesystem_mutation_allowed": False,
        "network_called": False,
        "permission_granting": False,
        "provider_called": False,
        "artifact_bodies_read": False,
        "reference_code_imported": False,
        "reference_contents_included": False,
    }


def _microbenchmark_contract(
    benchmark_id: str,
    *,
    measurement_mode: str,
    source_checks: list[str],
    metrics: list[str],
    passive_measurements: dict[str, Any],
    explicit_command: str | None = None,
    explicit_measurement_path: str | None = None,
    live_permit: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": benchmark_id,
        "measurement_mode": measurement_mode,
        "source_checks": source_checks,
        "metrics": metrics,
        "passive_measurements": passive_measurements,
        "explicit_command": explicit_command,
        "explicit_measurement_path": explicit_measurement_path,
        "live_permit": live_permit,
        "live_execution_required": measurement_mode == "explicit_live_required",
        "adapter_execution_started": False,
        "provider_called": False,
        "network_called": False,
        "filesystem_modified": False,
        "artifact_bodies_read": False,
    }


def _benchmark_case(
    benchmark_id: str,
    *,
    status: str,
    measurement_mode: str,
    message: str,
    source_checks: list[str],
    metrics: list[str],
    measurements: dict[str, Any],
    samples: list[dict[str, Any]] | None = None,
    gaps: list[str] | None = None,
    next_actions: list[str] | None = None,
) -> OrchestrationMicrobenchmarkCase:
    return OrchestrationMicrobenchmarkCase(
        id=benchmark_id,
        status=status,
        measurement_mode=measurement_mode,
        message=str(sanitize_for_logging(message)),
        source_checks=source_checks,
        metrics=metrics,
        measurements=sanitize_for_logging(measurements),
        samples=sanitize_for_logging(samples or []),
        gaps=[str(sanitize_for_logging(gap)) for gap in (gaps or [])],
        next_actions=[str(sanitize_for_logging(action)) for action in (next_actions or [])],
    )


def _time_microbenchmark_samples(
    sample_count: int,
    measure: Any,
) -> tuple[list[dict[str, Any]], dict[str, Any], list[str]]:
    samples: list[dict[str, Any]] = []
    gaps: list[str] = []
    for index in range(sample_count):
        started = time.perf_counter_ns()
        try:
            measurement = measure()
        except Exception as exc:
            duration_ns = time.perf_counter_ns() - started
            gaps.append(f"sample {index + 1}: {exc.__class__.__name__}")
            samples.append(
                {
                    "sample": index + 1,
                    "duration_ns": duration_ns,
                    "ok": False,
                    "error": exc.__class__.__name__,
                }
            )
            continue
        duration_ns = time.perf_counter_ns() - started
        samples.append(
            {
                "sample": index + 1,
                "duration_ns": duration_ns,
                "ok": True,
                **measurement,
            }
        )
    return samples, _duration_stats(samples), gaps


def _duration_stats(samples: list[dict[str, Any]]) -> dict[str, Any]:
    durations = sorted(int(sample.get("duration_ns", 0)) for sample in samples if sample.get("ok") is True)
    if not durations:
        return {
            "sample_count": len(samples),
            "successful_sample_count": 0,
            "min_duration_ns": None,
            "mean_duration_ns": None,
            "p95_duration_ns": None,
            "max_duration_ns": None,
        }
    p95_index = min(len(durations) - 1, max(0, math.ceil(len(durations) * 0.95) - 1))
    return {
        "sample_count": len(samples),
        "successful_sample_count": len(durations),
        "min_duration_ns": durations[0],
        "mean_duration_ns": round(sum(durations) / len(durations), 1),
        "p95_duration_ns": durations[p95_index],
        "max_duration_ns": durations[-1],
    }


_MICROBENCHMARK_DURATION_GUARDRAILS_NS: dict[str, dict[str, int]] = {
    "handoff_overhead": {"mean": 10_000_000, "p95": 25_000_000},
    "fanout_fanin_critical_path": {"mean": 10_000_000, "p95": 25_000_000},
    "checkpoint_latency": {"mean": 100_000_000, "p95": 250_000_000},
    "tool_adapter_overhead": {"mean": 10_000_000, "p95": 25_000_000},
    "retry_safety": {"mean": 10_000_000, "p95": 25_000_000},
    "trace_overhead": {"mean": 2_000_000_000, "p95": 5_000_000_000},
    "verification_stage_roi": {"mean": 10_000_000, "p95": 25_000_000},
}


def _with_duration_guardrail(benchmark_id: str, measurements: dict[str, Any]) -> dict[str, Any]:
    guardrail = _MICROBENCHMARK_DURATION_GUARDRAILS_NS.get(benchmark_id)
    if guardrail is None:
        return {
            **measurements,
            "duration_guardrail": {
                "schema_version": "harness.orchestration_microbenchmark_guardrail/v1",
                "mode": "not_configured",
                "status": "not_applicable",
            },
        }
    mean_duration = measurements.get("mean_duration_ns")
    p95_duration = measurements.get("p95_duration_ns")
    mean_ok = mean_duration is not None and float(mean_duration) <= guardrail["mean"]
    p95_ok = p95_duration is not None and int(p95_duration) <= guardrail["p95"]
    return {
        **measurements,
        "duration_guardrail": {
            "schema_version": "harness.orchestration_microbenchmark_guardrail/v1",
            "mode": "informational_local_threshold",
            "status": "pass" if mean_ok and p95_ok else "warning",
            "max_mean_duration_ns": guardrail["mean"],
            "max_p95_duration_ns": guardrail["p95"],
            "mean_within_guardrail": mean_ok,
            "p95_within_guardrail": p95_ok,
            "release_blocking": False,
        },
    }


def _microbenchmark_summary(benchmarks: list[OrchestrationMicrobenchmarkCase]) -> dict[str, int]:
    return {
        "total": len(benchmarks),
        "pass": sum(1 for benchmark in benchmarks if benchmark.status == "pass"),
        "warning": sum(1 for benchmark in benchmarks if benchmark.status == "warning"),
        "fail": sum(1 for benchmark in benchmarks if benchmark.status == "fail"),
        "skipped": sum(1 for benchmark in benchmarks if benchmark.status == "skipped"),
    }


def _microbenchmark_safety_flags() -> dict[str, bool]:
    return {
        "read_only": True,
        "synthetic_probe_only": True,
        "adapter_execution_started": False,
        "filesystem_modified": False,
        "filesystem_mutation_allowed": False,
        "network_called": False,
        "permission_granting": False,
        "provider_called": False,
        "artifact_bodies_read": False,
        "reference_code_imported": False,
        "reference_contents_included": False,
    }


def _check(
    check_id: str,
    status: str,
    message: str,
    *,
    measurements: dict[str, Any],
    gaps: list[str] | None = None,
    next_actions: list[str] | None = None,
) -> OrchestrationEfficiencyCheck:
    return OrchestrationEfficiencyCheck(
        id=check_id,
        status=status,
        message=str(sanitize_for_logging(message)),
        reference_patterns=list(ORCHESTRATION_EFFICIENCY_REFERENCE_PATTERNS.get(check_id, [])),
        measurements=sanitize_for_logging(measurements),
        gaps=[str(sanitize_for_logging(gap)) for gap in (gaps or [])],
        next_actions=[str(sanitize_for_logging(action)) for action in (next_actions or [])],
    )
