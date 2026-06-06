from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from harness.approvals import ApprovalStore
from harness.cli.main import app
from harness.config import default_config
from harness.local_server import _route_get
from harness.memory.sqlite_store import SQLiteStore
from harness.objective_runner import run_objective_parallel
from harness.orchestration_efficiency import run_orchestration_efficiency_audit, run_orchestration_microbenchmarks
from harness.orchestration_synthesis import run_orchestration_synthesis
from harness.operator_context import build_tui_dashboard
from harness.tui import build_command_palette, build_right_panel_model, render_right_panel


runner = CliRunner()


def _checks(payload: dict) -> dict[str, dict]:
    return {check["id"]: check for check in payload["checks"]}


def test_orchestration_efficiency_uninitialized_project_is_read_only(tmp_path: Path) -> None:
    result = run_orchestration_efficiency_audit(tmp_path)
    payload = result.model_dump(mode="json")
    checks = _checks(payload)

    assert payload["schema_version"] == "harness.orchestration_efficiency/v1"
    assert payload["ok"] is True
    assert payload["suite"] == "orchestration-efficiency"
    assert payload["safety"]["read_only"] is True
    assert payload["safety"]["provider_called"] is False
    assert payload["safety"]["network_called"] is False
    assert payload["safety"]["adapter_execution_started"] is False
    assert payload["safety"]["filesystem_modified"] is False
    assert payload["safety"]["permission_granting"] is False
    assert checks["adapter_security_complexity_tradeoff"]["status"] == "pass"
    assert (
        checks["adapter_security_complexity_tradeoff"]["measurements"]["core_service_prelease_gating_enforced"]
        is True
    )
    assert (
        checks["adapter_security_complexity_tradeoff"]["measurements"]["manual_queue_prelease_gating_enforced"]
        is True
    )
    assert (
        checks["adapter_security_complexity_tradeoff"]["measurements"]["adapter_rejection_finalization_enforced"]
        is True
    )
    assert (
        checks["adapter_security_complexity_tradeoff"]["measurements"][
            "read_only_compatibility_rejection_finalization_enforced"
        ]
        is True
    )
    assert (
        checks["adapter_security_complexity_tradeoff"]["measurements"][
            "daemon_renewal_inconsistent_lease_guard_enforced"
        ]
        is True
    )
    assert (
        checks["adapter_security_complexity_tradeoff"]["measurements"][
            "daemon_recovery_expired_lease_guard_enforced"
        ]
        is True
    )
    assert (
        checks["adapter_security_complexity_tradeoff"]["measurements"][
            "daemon_shutdown_linked_run_guard_enforced"
        ]
        is True
    )
    assert (
        checks["adapter_security_complexity_tradeoff"]["measurements"][
            "lease_mutation_authority_guard_enforced"
        ]
        is True
    )
    assert (
        checks["adapter_security_complexity_tradeoff"]["measurements"][
            "adapter_boundary_failure_finalization_enforced"
        ]
        is True
    )
    assert (
        checks["adapter_security_complexity_tradeoff"]["measurements"][
            "runtime_control_breaker_prelease_gating_enforced"
        ]
        is True
    )
    assert checks["bounded_critical_path_scheduler"]["status"] == "pass"
    assert (
        checks["bounded_critical_path_scheduler"]["measurements"]["objective_runner_prelease_autonomy_enforced"]
        is True
    )
    assert (
        checks["bounded_critical_path_scheduler"]["measurements"]["scheduler_policy_evidence_enforced"]
        is True
    )
    assert checks["delegate_budget_efficiency"]["status"] == "pass"
    assert checks["delegate_budget_efficiency"]["measurements"]["budget_schema_version"] == "harness.delegate_budget/v1"
    assert checks["delegate_budget_efficiency"]["measurements"]["total_runtime_invocation_ceiling"] > 0
    assert checks["delegate_budget_efficiency"]["measurements"]["total_cpu_seconds_ceiling"] > 0
    assert checks["delegate_budget_efficiency"]["measurements"]["max_memory_mb_ceiling"] > 0
    assert all(
        adapter["budget"]["max_parallel_branches"] == 1
        for adapter in checks["delegate_budget_efficiency"]["measurements"]["adapters"]
    )
    assert all(
        adapter["budget"]["max_cpu_seconds"] is not None
        for adapter in checks["delegate_budget_efficiency"]["measurements"]["adapters"]
    )
    assert all(
        adapter["budget"]["max_memory_mb"] is not None
        for adapter in checks["delegate_budget_efficiency"]["measurements"]["adapters"]
    )
    assert checks["live_benchmark_permits"]["status"] == "pass"
    live_permits = checks["live_benchmark_permits"]["measurements"]
    assert live_permits["schema_version"] == "harness.orchestration_live_benchmark_permits/v1"
    assert live_permits["permit_count"] == 2
    assert live_permits["approval_required_count"] == 2
    assert live_permits["release_blocking_count"] == 0
    assert live_permits["automated_execution_allowed_count"] == 0
    assert live_permits["provider_called"] is False
    assert checks["microbenchmark_contracts"]["status"] == "pass"
    microbenchmarks = checks["microbenchmark_contracts"]["measurements"]
    assert microbenchmarks["schema_version"] == "harness.orchestration_microbenchmark_contracts/v1"
    assert microbenchmarks["benchmark_count"] == 9
    assert microbenchmarks["passive_or_synthetic_count"] >= 7
    assert microbenchmarks["explicit_live_required_count"] == 2
    assert {row["id"] for row in microbenchmarks["benchmarks"]} == {
        "handoff_overhead",
        "fanout_fanin_critical_path",
        "checkpoint_latency",
        "sandbox_startup",
        "tool_adapter_overhead",
        "retry_safety",
        "trace_overhead",
        "shared_llm_contention",
        "verification_stage_roi",
    }
    assert all(row["adapter_execution_started"] is False for row in microbenchmarks["benchmarks"])
    explicit_live_rows = [row for row in microbenchmarks["benchmarks"] if row["measurement_mode"] == "explicit_live_required"]
    assert all(row["live_permit"]["schema_version"] == "harness.orchestration_live_benchmark_permit/v1" for row in explicit_live_rows)
    assert all(row["live_permit"]["release_blocking"] is False for row in explicit_live_rows)
    assert checks["replay_retry_idempotency"]["status"] == "pass"
    assert checks["replay_retry_idempotency"]["measurements"]["task_retry_replay_policy_enforced"] is True
    assert checks["replay_retry_idempotency"]["measurements"]["task_replay_receipts_enforced"] is True
    assert (
        checks["replay_retry_idempotency"]["measurements"]["existing_task_replay_receipts"]["initialized"]
        is False
    )
    assert (
        checks["replay_retry_idempotency"]["measurements"]["daemon_prelease_descriptor_approval_enforced"]
        is True
    )
    assert checks["evidence_trace_projection_cost"]["status"] == "skipped"
    assert not (tmp_path / ".harness").exists()


def test_orchestration_efficiency_cli_json_does_not_initialize_project(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["evals", "run", "--suite", "orchestration-efficiency", "--project", str(tmp_path), "--output", "json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    checks = _checks(payload)
    assert payload["schema_version"] == "harness.orchestration_efficiency/v1"
    assert payload["safety"]["artifact_bodies_read"] is False
    assert checks["bounded_critical_path_scheduler"]["measurements"]["configured_default_max_parallel"] == 2
    assert checks["bounded_critical_path_scheduler"]["measurements"]["bounded_max_active"] <= 2
    assert checks["adapter_security_complexity_tradeoff"]["measurements"]["adapter_count"] > 0
    assert checks["microbenchmark_contracts"]["measurements"]["provider_called"] is False
    assert checks["microbenchmark_contracts"]["measurements"]["network_called"] is False
    assert checks["live_benchmark_permits"]["measurements"]["provider_called"] is False
    assert checks["live_benchmark_permits"]["measurements"]["automated_execution_allowed_count"] == 0
    assert checks["delegate_budget_efficiency"]["measurements"]["adapter_count"] == checks[
        "adapter_security_complexity_tradeoff"
    ]["measurements"]["adapter_count"]
    assert not (tmp_path / ".harness").exists()


def test_orchestration_microbenchmarks_uninitialized_project_is_read_only(tmp_path: Path) -> None:
    result = run_orchestration_microbenchmarks(tmp_path, samples=3)
    payload = result.model_dump(mode="json")
    benchmarks = {benchmark["id"]: benchmark for benchmark in payload["benchmarks"]}

    assert payload["schema_version"] == "harness.orchestration_microbenchmarks/v1"
    assert payload["ok"] is True
    assert payload["suite"] == "orchestration-microbenchmarks"
    assert payload["summary"]["total"] == 9
    assert payload["summary"]["fail"] == 0
    assert payload["summary"]["pass"] >= 5
    assert payload["summary"]["skipped"] >= 2
    assert payload["safety"]["read_only"] is True
    assert payload["safety"]["provider_called"] is False
    assert payload["safety"]["network_called"] is False
    assert payload["safety"]["adapter_execution_started"] is False
    assert payload["safety"]["filesystem_modified"] is False
    assert payload["safety"]["permission_granting"] is False
    assert payload["safety"]["artifact_bodies_read"] is False
    assert benchmarks["handoff_overhead"]["status"] == "pass"
    assert benchmarks["handoff_overhead"]["measurements"]["sample_count"] == 3
    assert benchmarks["handoff_overhead"]["measurements"]["successful_sample_count"] == 3
    handoff_guardrail = benchmarks["handoff_overhead"]["measurements"]["duration_guardrail"]
    assert handoff_guardrail["schema_version"] == "harness.orchestration_microbenchmark_guardrail/v1"
    assert handoff_guardrail["mode"] == "informational_local_threshold"
    assert handoff_guardrail["release_blocking"] is False
    assert benchmarks["fanout_fanin_critical_path"]["samples"][-1]["bounded_max_active"] <= 2
    assert benchmarks["checkpoint_latency"]["status"] == "skipped"
    assert benchmarks["checkpoint_latency"]["measurements"]["initialized"] is False
    assert benchmarks["sandbox_startup"]["status"] == "skipped"
    assert benchmarks["sandbox_startup"]["measurement_mode"] == "explicit_live_required"
    sandbox_permit = benchmarks["sandbox_startup"]["measurements"]["live_permit"]
    assert sandbox_permit["schema_version"] == "harness.orchestration_live_benchmark_permit/v1"
    assert sandbox_permit["status"] == "approval_required"
    assert sandbox_permit["live_execution"]["automated_execution_allowed"] is False
    assert sandbox_permit["live_execution"]["adapter_execution_started"] is False
    assert sandbox_permit["boundaries"]["active_repo_mutation_allowed"] is False
    assert sandbox_permit["release_blocking"] is False
    assert benchmarks["shared_llm_contention"]["status"] == "skipped"
    shared_permit = benchmarks["shared_llm_contention"]["measurements"]["live_permit"]
    assert shared_permit["required_approval"]["backend"] == "harness_evals"
    assert shared_permit["boundaries"]["provider_call_required"] is True
    assert shared_permit["live_execution"]["provider_called"] is False
    assert benchmarks["trace_overhead"]["status"] == "skipped"
    assert not (tmp_path / ".harness").exists()


def test_orchestration_microbenchmarks_cli_json_does_not_initialize_project(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["evals", "run", "--suite", "orchestration-microbenchmarks", "--project", str(tmp_path), "--output", "json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    benchmarks = {benchmark["id"]: benchmark for benchmark in payload["benchmarks"]}
    assert payload["schema_version"] == "harness.orchestration_microbenchmarks/v1"
    assert payload["summary"]["fail"] == 0
    assert benchmarks["handoff_overhead"]["measurements"]["successful_sample_count"] > 0
    assert benchmarks["tool_adapter_overhead"]["status"] == "pass"
    assert benchmarks["tool_adapter_overhead"]["measurements"]["duration_guardrail"]["release_blocking"] is False
    assert benchmarks["retry_safety"]["status"] == "pass"
    assert payload["safety"]["provider_called"] is False
    assert payload["safety"]["network_called"] is False
    assert payload["safety"]["adapter_execution_started"] is False
    assert payload["safety"]["filesystem_modified"] is False
    assert payload["safety"]["permission_granting"] is False
    assert payload["safety"]["artifact_bodies_read"] is False
    assert not (tmp_path / ".harness").exists()


def test_orchestration_live_benchmark_permit_detects_scoped_approval(tmp_path: Path) -> None:
    approval = ApprovalStore(tmp_path).add(
        backend="codex_cli",
        data_boundary="hosted_provider",
        task_types=["codex_code_edit"],
        duration_hours=1,
        allowed_adapters=["codex_isolated_edit"],
        max_runs=1,
        autonomy_scope="supervised-codex",
    )

    result = run_orchestration_microbenchmarks(tmp_path, samples=1)
    payload = result.model_dump(mode="json")
    benchmarks = {benchmark["id"]: benchmark for benchmark in payload["benchmarks"]}
    permit = benchmarks["sandbox_startup"]["measurements"]["live_permit"]

    assert permit["status"] == "approval_ready"
    assert permit["approval_ready"] is True
    assert permit["required_approval"]["approval_id"] == approval.id
    assert permit["live_execution"]["runner_available"] is False
    assert permit["live_execution"]["automated_execution_allowed"] is False
    assert permit["live_execution"]["adapter_execution_started"] is False
    assert payload["safety"]["provider_called"] is False
    assert payload["safety"]["filesystem_modified"] is False
    assert not (tmp_path / ".harness" / "harness.sqlite").exists()


def test_orchestration_efficiency_local_server_route_is_passive(tmp_path: Path) -> None:
    payload = _route_get(
        "/orchestration/efficiency",
        project_root=tmp_path,
        store=SQLiteStore(tmp_path),
        cfg=default_config(),
        host="127.0.0.1",
        port=8765,
        query={},
    )

    assert payload is not None
    assert payload["schema_version"] == "harness.orchestration_efficiency/v1"
    assert payload["summary_projection"]["schema_version"] == "harness.orchestration_efficiency_summary/v1"
    assert payload["safety"]["read_only"] is True
    assert payload["safety"]["provider_called"] is False
    assert payload["safety"]["network_called"] is False
    assert payload["safety"]["adapter_execution_started"] is False
    assert payload["safety"]["filesystem_modified"] is False
    assert payload["safety"]["permission_granting"] is False
    assert payload["safety"]["artifact_bodies_read"] is False
    assert not (tmp_path / ".harness").exists()


def test_orchestration_microbenchmarks_local_server_route_is_passive(tmp_path: Path) -> None:
    payload = _route_get(
        "/orchestration/microbenchmarks",
        project_root=tmp_path,
        store=SQLiteStore(tmp_path),
        cfg=default_config(),
        host="127.0.0.1",
        port=8765,
        query={},
    )

    assert payload is not None
    benchmarks = {benchmark["id"]: benchmark for benchmark in payload["benchmarks"]}
    assert payload["schema_version"] == "harness.orchestration_microbenchmarks/v1"
    assert payload["summary_projection"]["schema_version"] == "harness.orchestration_microbenchmarks_summary/v1"
    assert payload["summary"]["fail"] == 0
    assert payload["safety"]["read_only"] is True
    assert payload["safety"]["provider_called"] is False
    assert payload["safety"]["network_called"] is False
    assert payload["safety"]["adapter_execution_started"] is False
    assert payload["safety"]["filesystem_modified"] is False
    assert payload["safety"]["permission_granting"] is False
    assert payload["safety"]["artifact_bodies_read"] is False
    assert benchmarks["sandbox_startup"]["status"] == "skipped"
    assert benchmarks["sandbox_startup"]["measurements"]["live_permit"]["release_blocking"] is False
    assert benchmarks["shared_llm_contention"]["status"] == "skipped"
    assert benchmarks["shared_llm_contention"]["measurements"]["live_permit"]["live_execution"][
        "automated_execution_allowed"
    ] is False
    assert not (tmp_path / ".harness").exists()


def test_orchestration_synthesis_uninitialized_project_is_read_only(tmp_path: Path) -> None:
    result = run_orchestration_synthesis(tmp_path, include_references=False)
    payload = result.model_dump(mode="json")

    assert payload["schema_version"] == "harness.orchestration_synthesis/v1"
    assert payload["ok"] is True
    assert payload["summary"]["readiness_status"] == "pass"
    assert payload["summary"]["efficiency_status"] == "pass"
    assert payload["summary"]["microbenchmark_status"] == "pass"
    assert payload["summary"]["reference_status"] == "disabled"
    assert payload["source_reports"]["reference_repositories"]["included"] is False
    assert payload["adopted_reference_patterns"]
    assert payload["deliberate_non_adoptions"]
    adopted = {row["pattern"]: row for row in payload["adopted_reference_patterns"]}
    assert adopted["external_protocol_interoperability"]["status"] == "pass"
    assert adopted["external_protocol_interoperability"]["readiness_statuses"]["external_protocol_compatibility"] == "pass"
    assert "external protocol compatibility catalog" in adopted["external_protocol_interoperability"]["harness_surfaces"]
    assert adopted["durable_workflow_and_state_graph"]["readiness_statuses"]["replay_drift_detection"] == "pass"
    assert adopted["durable_workflow_and_state_graph"]["readiness_statuses"]["workflow_coordination_contracts"] == "pass"
    assert adopted["durable_workflow_and_state_graph"]["readiness_statuses"][
        "orchestration_scenario_conformance"
    ] == "pass"
    assert "workflow coordination catalog" in adopted["durable_workflow_and_state_graph"]["harness_surfaces"]
    assert "orchestration scenario conformance catalog" in adopted["durable_workflow_and_state_graph"][
        "harness_surfaces"
    ]
    assert "microsoft-agent-framework" in adopted["durable_workflow_and_state_graph"]["reference_inputs"]
    assert "orchestration replay drift audit" in adopted["durable_workflow_and_state_graph"]["harness_surfaces"]
    deliberate_non_adoptions = {row["id"] for row in payload["deliberate_non_adoptions"]}
    assert "no_fail_open_remote_protocol_execution" in deliberate_non_adoptions
    assert "no_replay_side_effect_execution" in deliberate_non_adoptions
    assert adopted["policy_boundary_and_applyback"]["readiness_statuses"]["agentic_security_controls"] == "pass"
    assert "agentic security controls" in adopted["policy_boundary_and_applyback"]["harness_surfaces"]
    assert payload["security_complexity_posture"]["live_benchmarks_automatic"] is False
    assert payload["security_complexity_posture"]["live_benchmarks_release_blocking"] is False
    assert payload["safety"]["read_only"] is True
    assert payload["safety"]["reference_code_imported"] is False
    assert payload["safety"]["reference_contents_included"] is False
    assert payload["safety"]["provider_called"] is False
    assert payload["safety"]["network_called"] is False
    assert payload["safety"]["adapter_execution_started"] is False
    assert payload["safety"]["filesystem_modified"] is False
    assert payload["safety"]["permission_granting"] is False
    assert payload["safety"]["artifact_bodies_read"] is False
    assert payload["safety"]["live_benchmark_execution_allowed"] is False
    assert not (tmp_path / ".harness").exists()


def test_orchestration_synthesis_cli_and_eval_json_are_passive(tmp_path: Path) -> None:
    cli_result = runner.invoke(
        app,
        ["orchestration", "synthesis", "--project", str(tmp_path), "--no-references", "--output", "json"],
    )
    eval_result = runner.invoke(
        app,
        ["evals", "run", "--suite", "orchestration-synthesis", "--project", str(tmp_path), "--output", "json"],
    )

    assert cli_result.exit_code == 0, cli_result.output
    assert eval_result.exit_code == 0, eval_result.output
    cli_payload = json.loads(cli_result.output)
    eval_payload = json.loads(eval_result.output)
    assert cli_payload["schema_version"] == "harness.orchestration_synthesis/v1"
    assert eval_payload["schema_version"] == "harness.orchestration_synthesis/v1"
    assert cli_payload["summary"]["reference_status"] == "disabled"
    assert "external_protocol_interoperability" in {
        row["pattern"] for row in cli_payload["adopted_reference_patterns"]
    }
    assert "no_fail_open_remote_protocol_execution" in {
        row["id"] for row in cli_payload["deliberate_non_adoptions"]
    }
    assert eval_payload["summary"]["readiness_status"] == "warning"
    assert eval_payload["summary"]["efficiency_status"] == "pass"
    assert eval_payload["summary"]["reference_status"] in {"pass", "warning"}
    assert eval_payload["safety"]["reference_code_imported"] is False
    assert eval_payload["safety"]["reference_contents_included"] is False
    assert eval_payload["safety"]["provider_called"] is False
    assert eval_payload["safety"]["adapter_execution_started"] is False
    assert not (tmp_path / ".harness").exists()


def test_orchestration_synthesis_local_server_route_is_passive(tmp_path: Path) -> None:
    payload = _route_get(
        "/orchestration/synthesis",
        project_root=tmp_path,
        store=SQLiteStore(tmp_path),
        cfg=default_config(),
        host="127.0.0.1",
        port=8765,
        query={"include_references": ["false"]},
    )

    assert payload is not None
    assert payload["schema_version"] == "harness.orchestration_synthesis/v1"
    assert payload["summary"]["reference_status"] == "disabled"
    assert payload["summary_projection"]["schema_version"] == "harness.orchestration_synthesis_summary/v1"
    assert payload["summary_projection"]["summary"]["reference_status"] == "disabled"
    assert payload["summary_projection"]["source_report_statuses"]["readiness"] == "pass"
    assert payload["summary_projection"]["source_report_statuses"]["efficiency"] == "pass"
    assert payload["summary_projection"]["source_report_statuses"]["microbenchmarks"] == "pass"
    assert payload["summary_projection"]["security_complexity_posture"]["live_benchmarks_release_blocking"] is False
    assert payload["summary_projection"]["safety"]["reference_metadata_included"] is False
    assert payload["summary_projection"]["safety"]["provider_called"] is False
    assert payload["summary_projection"]["safety"]["network_called"] is False
    assert payload["summary_projection"]["safety"]["adapter_execution_started"] is False
    assert payload["summary_projection"]["safety"]["filesystem_modified"] is False
    assert payload["summary_projection"]["safety"]["permission_granting"] is False
    assert payload["summary_projection"]["safety"]["artifact_bodies_read"] is False
    assert payload["source_reports"]["readiness"]["schema_version"] == "harness.orchestration_readiness_summary/v1"
    assert payload["source_reports"]["efficiency"]["schema_version"] == "harness.orchestration_efficiency_summary/v1"
    assert (
        payload["source_reports"]["microbenchmarks"]["schema_version"]
        == "harness.orchestration_microbenchmarks_summary/v1"
    )
    assert payload["safety"]["read_only"] is True
    assert payload["safety"]["network_called"] is False
    assert payload["safety"]["adapter_execution_started"] is False
    assert payload["safety"]["filesystem_modified"] is False
    assert payload["safety"]["permission_granting"] is False
    assert not (tmp_path / ".harness").exists()


def test_tui_dashboard_surfaces_efficiency_and_microbenchmarks_without_initializing_project(tmp_path: Path) -> None:
    dashboard = build_tui_dashboard(tmp_path)
    model = build_right_panel_model(dashboard, {"palette": build_command_palette()}, "", "dashboard")
    rendered = render_right_panel(model)

    assert dashboard["orchestration_efficiency"]["schema_version"] == "harness.orchestration_efficiency_summary/v1"
    assert dashboard["orchestration_efficiency"]["safety"]["provider_called"] is False
    assert dashboard["orchestration_efficiency"]["safety"]["artifact_bodies_read"] is False
    assert (
        dashboard["orchestration_microbenchmarks"]["schema_version"]
        == "harness.orchestration_microbenchmarks_summary/v1"
    )
    assert dashboard["orchestration_microbenchmarks"]["source"] == "operator_context_bounded_passive_sample"
    assert dashboard["orchestration_microbenchmarks"]["safety"]["provider_called"] is False
    assert dashboard["orchestration_microbenchmarks"]["safety"]["network_called"] is False
    assert dashboard["orchestration_microbenchmarks"]["safety"]["adapter_execution_started"] is False
    assert dashboard["orchestration_microbenchmarks"]["safety"]["filesystem_modified"] is False
    assert dashboard["orchestration_microbenchmarks"]["safety"]["permission_granting"] is False
    assert dashboard["orchestration_microbenchmarks"]["safety"]["artifact_bodies_read"] is False
    assert dashboard["orchestration_synthesis"]["schema_version"] == "harness.orchestration_synthesis_summary/v1"
    assert dashboard["orchestration_synthesis"]["source"] == "operator_context_no_reference_synthesis"
    assert dashboard["orchestration_synthesis"]["summary"]["reference_status"] == "disabled"
    assert dashboard["orchestration_synthesis"]["security_complexity_posture"]["live_benchmarks_release_blocking"] is False
    assert dashboard["orchestration_synthesis"]["safety"]["reference_metadata_included"] is False
    assert dashboard["orchestration_synthesis"]["safety"]["reference_code_imported"] is False
    assert dashboard["orchestration_synthesis"]["safety"]["reference_contents_included"] is False
    assert dashboard["orchestration_synthesis"]["safety"]["provider_called"] is False
    assert dashboard["orchestration_synthesis"]["safety"]["network_called"] is False
    assert dashboard["orchestration_synthesis"]["safety"]["adapter_execution_started"] is False
    assert dashboard["orchestration_synthesis"]["safety"]["filesystem_modified"] is False
    assert dashboard["orchestration_synthesis"]["safety"]["permission_granting"] is False
    assert dashboard["orchestration_synthesis"]["safety"]["artifact_bodies_read"] is False
    assert dashboard["orchestration_synthesis"]["safety"]["mutation_allowed"] is False
    assert dashboard["orchestration_synthesis"]["safety"]["model_context_allowed"] is False
    assert "Efficiency:" in rendered
    assert "Efficiency audit: harness evals run --suite orchestration-efficiency --project . --output json" in rendered
    assert "Microbenchmarks:" in rendered
    assert "Microbenchmarks: harness evals run --suite orchestration-microbenchmarks --project . --output json" in rendered
    assert "Synthesis:" in rendered
    assert "Synthesis: harness evals run --suite orchestration-synthesis --project . --output json" in rendered
    assert not (tmp_path / ".harness").exists()


def test_orchestration_efficiency_measures_existing_objective_and_run_evidence(tmp_path: Path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    objective = store.create_objective("Efficiency measurement objective")
    store.create_task(
        "Dry run task",
        objective_id=objective.id,
        metadata={"execution_adapter": "dry_run", "task_type": "phase_1a_test"},
    )

    run_result = run_objective_parallel(tmp_path, objective.id, max_steps=1, max_parallel=2)
    assert run_result.ok is True

    result = run_orchestration_efficiency_audit(tmp_path)
    payload = result.model_dump(mode="json")
    checks = _checks(payload)
    evidence = checks["evidence_trace_projection_cost"]
    replay_receipts = checks["replay_retry_idempotency"]["measurements"]["existing_task_replay_receipts"]

    assert payload["ok"] is True
    assert replay_receipts["initialized"] is True
    assert replay_receipts["attempt_count"] == 1
    assert replay_receipts["receipt_count"] == 1
    assert replay_receipts["legacy_missing_receipt_count"] == 0
    assert replay_receipts["gaps"] == []
    assert evidence["status"] == "pass"
    assert evidence["measurements"]["objectives"]["objective_evidence_files"] == 1
    assert evidence["measurements"]["objectives"]["total_evidence_events"] > 0
    assert evidence["measurements"]["objectives"]["total_trace_spans"] > 0
    assert evidence["measurements"]["runs"]["run_count"] == 1
    assert evidence["measurements"]["runs"]["total_trace_spans"] > 0
    assert evidence["measurements"]["artifact_bodies_read"] is False
