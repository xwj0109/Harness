from __future__ import annotations

import json
import subprocess
from pathlib import Path

from typer.testing import CliRunner

from harness.cli.main import app
from harness.config import HARNESS_DIR
from harness.config import default_config
from harness.governance.reference_repositories import CURATED_REFERENCE_REPOSITORIES, REQUIRED_REFERENCE_PATTERNS
from harness.local_server import _route_get
from harness.memory.sqlite_store import SQLiteStore
from harness.objective_checkpoints import create_objective_checkpoint, resolve_objective_checkpoint
from harness.orchestration_readiness import run_orchestration_readiness_audit
from harness.operator_context import build_tui_dashboard
from harness.tui import build_command_palette, build_right_panel_model, render_right_panel


runner = CliRunner()


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _init_git_repo(root: Path, name: str) -> Path:
    repo = root / name
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True, text=True)
    (repo / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "-c", "user.name=Harness Test", "-c", "user.email=harness@example.invalid", "commit", "-m", "init")
    return repo


def _checks(payload: dict) -> dict[str, dict]:
    return {check["id"]: check for check in payload["checks"]}


def test_orchestration_readiness_uninitialized_project_is_read_only(tmp_path: Path) -> None:
    result = run_orchestration_readiness_audit(tmp_path, include_references=False)
    payload = result.model_dump(mode="json")
    checks = _checks(payload)

    assert payload["schema_version"] == "harness.orchestration_readiness_audit/v1"
    assert payload["ok"] is True
    assert payload["initialized"] is False
    assert payload["safety"]["read_only"] is True
    assert payload["safety"]["reference_code_imported"] is False
    assert payload["safety"]["reference_contents_included"] is False
    assert payload["safety"]["provider_called"] is False
    assert payload["safety"]["network_called"] is False
    assert payload["safety"]["adapter_execution_started"] is False
    assert payload["safety"]["filesystem_modified"] is False
    assert payload["safety"]["permission_granting"] is False
    assert checks["durable_supervisor_state"]["status"] == "pass"
    assert checks["typed_task_delegation"]["status"] == "pass"
    assert checks["typed_task_delegation"]["evidence"]["handoff_schema_version"] == "harness.agent_handoff_envelope/v1"
    assert checks["typed_task_delegation"]["evidence"]["handoff_ok"] is True
    assert checks["typed_task_delegation"]["evidence"]["handoff_validation_errors"] == []
    assert checks["typed_task_delegation"]["evidence"]["unsafe_handoff_authority"] == []
    assert checks["typed_task_delegation"]["evidence"]["agent_contract_schema_version"] == "harness.agent_contract/v1"
    assert checks["typed_task_delegation"]["evidence"]["agent_contract_ok"] is True
    assert checks["typed_task_delegation"]["evidence"]["agent_contract_source_kind"] == "builtin"
    assert checks["typed_task_delegation"]["evidence"]["agent_contract_tool_policy"]["tool_policy_id"] == "read_only"
    assert checks["typed_task_delegation"]["evidence"]["agent_contract_budget_policy"]["per_handoff_budget_required"] is True
    assert checks["typed_task_delegation"]["evidence"]["agent_contract_trace_policy"]["w3c_traceparent_required"] is True
    assert checks["typed_task_delegation"]["evidence"]["agent_contract_validation_errors"] == []
    assert "harness.agent_handoff_envelope/v1" in checks["typed_task_delegation"]["evidence"]["output_contracts"]
    assert checks["agent_discovery_and_allocation"]["status"] == "pass"
    agent_discovery = checks["agent_discovery_and_allocation"]["evidence"]
    assert agent_discovery["catalog_schema_version"] == "harness.agent_discovery_catalog/v1"
    assert agent_discovery["allocation_schema_version"] == "harness.delegate_allocation/v1"
    assert agent_discovery["catalog_ok"] is True
    assert agent_discovery["allocation_ok"] is True
    assert agent_discovery["selected_agent_ids"] == ["security_reviewer"]
    assert agent_discovery["sample_allocation_selected_agent_ids"] == ["security_reviewer"]
    assert agent_discovery["eligible_count"] >= 1
    assert "security_reviewer" in agent_discovery["agent_ids"]
    assert agent_discovery["announcement"]["authority"]["task_record_creation_allowed"] is False
    assert agent_discovery["announcement"]["authority"]["agent_execution_allowed"] is False
    assert agent_discovery["announcement"]["authority"]["permission_granting"] is False
    assert agent_discovery["selected_bids"][0]["agent_id"] == "security_reviewer"
    assert agent_discovery["selected_bids"][0]["bid_terms"]["runtime_authority_granted"] is False
    assert agent_discovery["catalog_safety_issues"] == []
    assert agent_discovery["allocation_safety_issues"] == []
    assert agent_discovery["card_safety_issues"] == []
    assert agent_discovery["bid_safety_issues"] == []
    assert agent_discovery["unsafe_announcement_authority"] == []
    assert agent_discovery["unsafe_card_authority"] == []
    assert agent_discovery["catalog_safety"]["agent_execution_started"] is False
    assert agent_discovery["allocation_safety"]["permission_granting"] is False
    assert checks["budget_limited_delegation"]["status"] == "pass"
    assert checks["budget_limited_delegation"]["evidence"]["budget_schema_version"] == "harness.delegate_budget/v1"
    assert all(adapter["budget_limited"] for adapter in checks["budget_limited_delegation"]["evidence"]["adapters"])
    assert checks["supervisor_checkpoints"]["status"] == "pass"
    assert checks["external_protocol_compatibility"]["status"] == "pass"
    assert checks["external_protocol_compatibility"]["evidence"]["missing_protocol_ids"] == []
    assert checks["external_protocol_compatibility"]["evidence"]["missing_model_protocols"] == []
    assert checks["external_protocol_compatibility"]["evidence"]["risky_default_model_visible_protocols"] == []
    assert checks["external_protocol_compatibility"]["evidence"]["unsafe_runtime_enabled_protocols"] == []
    assert checks["external_protocol_compatibility"]["evidence"]["unsafe_authority_protocols"] == []
    assert checks["external_protocol_compatibility"]["evidence"]["telemetry_contract_gaps"] == []
    assert checks["external_protocol_compatibility"]["evidence"]["telemetry_contracts"]["mcp_tool"] == [
        "opentelemetry.semconv.gen_ai.mcp",
        "w3c_trace_context",
    ]
    assert checks["schema_compatibility_contracts"]["status"] == "pass"
    assert checks["schema_compatibility_contracts"]["evidence"]["missing_critical_schema_ids"] == []
    assert checks["schema_compatibility_contracts"]["evidence"]["duplicate_schema_ids"] == []
    assert checks["schema_compatibility_contracts"]["evidence"]["unversioned_schema_ids"] == []
    assert checks["schema_compatibility_contracts"]["evidence"]["unsafe_authority_schema_ids"] == []
    assert checks["schema_compatibility_contracts"]["evidence"]["incomplete_contract_ids"] == []
    assert checks["schema_compatibility_contracts"]["evidence"]["policy_mismatches"] == []
    assert checks["schema_compatibility_contracts"]["evidence"]["version_mismatches"] == []
    assert checks["schema_compatibility_contracts"]["evidence"]["safety_issues"] == []
    assert "agent_discovery_catalog" in checks["schema_compatibility_contracts"]["evidence"]["schema_ids"]
    assert "agent_handoff_envelope" in checks["schema_compatibility_contracts"]["evidence"]["schema_ids"]
    assert "task_replay_receipt" in checks["schema_compatibility_contracts"]["evidence"]["schema_ids"]
    assert "objective_batch_plan" in checks["schema_compatibility_contracts"]["evidence"]["schema_ids"]
    assert "workflow_template" in checks["schema_compatibility_contracts"]["evidence"]["schema_ids"]
    assert "workflow_agent_selection" in checks["schema_compatibility_contracts"]["evidence"]["schema_ids"]
    assert "workflow_coordination_catalog" in checks["schema_compatibility_contracts"]["evidence"]["schema_ids"]
    assert "sandbox_profile_catalog" in checks["schema_compatibility_contracts"]["evidence"]["schema_ids"]
    assert "sandbox_profile" in checks["schema_compatibility_contracts"]["evidence"]["schema_ids"]
    assert "orchestration_scenario_catalog" in checks["schema_compatibility_contracts"]["evidence"]["schema_ids"]
    assert checks["workflow_coordination_contracts"]["status"] == "pass"
    workflow_contracts = checks["workflow_coordination_contracts"]["evidence"]
    assert workflow_contracts["schema_version"] == "harness.workflow_coordination_catalog/v1"
    assert workflow_contracts["missing_required_pattern_ids"] == []
    assert workflow_contracts["missing_required_state_class_ids"] == []
    assert workflow_contracts["failed_pattern_ids"] == []
    assert workflow_contracts["safety_issues"] == []
    assert set(workflow_contracts["state_class_ids"]) == {
        "session_state",
        "workflow_state",
        "memory_state",
        "artifact_state",
    }
    assert "bounded_parallel_fanout" in workflow_contracts["pattern_ids"]
    assert "typed_agent_handoff" in workflow_contracts["pattern_ids"]
    assert checks["orchestration_scenario_conformance"]["status"] == "pass"
    scenario_conformance = checks["orchestration_scenario_conformance"]["evidence"]
    assert scenario_conformance["schema_version"] == "harness.orchestration_scenario_catalog/v1"
    assert scenario_conformance["missing_required_case_ids"] == []
    assert scenario_conformance["missing_required_layers"] == []
    assert scenario_conformance["failed_case_ids"] == []
    assert scenario_conformance["safety_issues"] == []
    assert set(scenario_conformance["layer_ids"]) == {"unit", "contract", "replay", "scenario", "security", "benchmark"}
    assert "slow_branch_barrier" in scenario_conformance["case_ids"]
    assert "unsafe_memory_to_hosted_model" in scenario_conformance["case_ids"]
    assert checks["agentic_security_controls"]["status"] == "pass"
    agentic_security = checks["agentic_security_controls"]["evidence"]
    assert agentic_security["risk_count"] == 3
    assert agentic_security["passed_risk_count"] == 3
    assert {row["risk_id"] for row in agentic_security["risk_controls"]} == {
        "memory_poisoning",
        "insecure_inter_agent_communication",
        "cascading_failures",
    }
    assert agentic_security["context_policy_decisions"]["local_memory"]["allowed"] is True
    assert "memory_not_authority" in agentic_security["context_policy_decisions"]["local_memory"]["warnings"]
    assert agentic_security["context_policy_decisions"]["hosted_memory"]["allowed"] is False
    assert agentic_security["context_policy_decisions"]["remote_memory"]["allowed"] is False
    assert agentic_security["context_policy_decisions"]["secret_context"]["allowed"] is False
    assert agentic_security["handoff"]["unsafe_handoff_authority"] == []
    assert agentic_security["handoff"]["traceparent_present"] is True
    assert agentic_security["handoff"]["payload_sha256_present"] is True
    assert agentic_security["protocols"]["risky_protocols"] == []
    assert agentic_security["cascading_failure"]["auto_allowed_unsafe_replay_adapter_ids"] == []
    assert "duplicate_side_effect_dispatch" in agentic_security["cascading_failure"]["detected_replay_issue_codes"]
    assert agentic_security["safety"]["provider_called"] is False
    assert agentic_security["safety"]["permission_granting"] is False
    assert checks["objective_lifecycle_controls"]["status"] == "pass"
    assert checks["objective_lifecycle_controls"]["evidence"]["store_create_draft_objective"] is True
    assert checks["objective_lifecycle_controls"]["evidence"]["runner_blocks_inactive_objectives"] is True
    assert checks["objective_lifecycle_controls"]["evidence"]["waiting_approval_status"] is True
    assert checks["objective_lifecycle_controls"]["evidence"]["retrying_status"] is True
    assert checks["objective_lifecycle_controls"]["evidence"]["store_retry_method"] is True
    assert checks["objective_lifecycle_controls"]["evidence"]["runner_marks_checkpoint_waiting_approval"] is True
    assert checks["objective_lifecycle_controls"]["evidence"]["progress_terminalizes_inactive_objectives"] is True
    assert checks["objective_lifecycle_controls"]["evidence"]["progress_blocks_created_objectives"] is True
    assert checks["objective_lifecycle_controls"]["evidence"]["progress_blocks_suspended_objectives"] is True
    assert checks["objective_lifecycle_controls"]["evidence"]["progress_blocks_waiting_approval_objectives"] is True
    assert checks["objective_lifecycle_controls"]["evidence"]["progress_blocks_retrying_objectives"] is True
    assert checks["objective_lifecycle_controls"]["evidence"]["cli_add_draft_option"] is True
    assert checks["objective_lifecycle_controls"]["evidence"]["cli_start_command"] is True
    assert checks["objective_lifecycle_controls"]["evidence"]["cli_cancel_command"] is True
    assert checks["objective_lifecycle_controls"]["evidence"]["cli_complete_command"] is True
    assert checks["objective_lifecycle_controls"]["evidence"]["cli_suspend_command"] is True
    assert checks["objective_lifecycle_controls"]["evidence"]["cli_resume_command"] is True
    assert checks["objective_lifecycle_controls"]["evidence"]["cli_timeout_command"] is True
    assert checks["objective_lifecycle_controls"]["evidence"]["cli_retry_command"] is True
    assert checks["replay_drift_detection"]["status"] == "pass"
    assert checks["replay_drift_detection"]["evidence"]["summary"]["fail"] == 0
    assert checks["replay_drift_detection"]["evidence"]["summary"]["synthetic"] == 5
    assert checks["replay_drift_detection"]["evidence"]["skipped_case_ids"] == ["captured_objective_evidence_replay"]
    assert "duplicate_side_effect_dispatch" in checks["replay_drift_detection"]["evidence"]["detected_issue_codes"]
    assert checks["replay_drift_detection"]["evidence"]["safety"]["artifact_bodies_read"] is False
    assert checks["pending_chat_action_recovery"]["status"] == "pass"
    assert checks["pending_chat_action_recovery"]["evidence"]["current_sessions"]["initialized"] is False
    assert checks["pending_chat_action_recovery"]["evidence"]["synthetic"]["invalid_status"] == "invalid"
    assert checks["pending_chat_action_recovery"]["evidence"]["synthetic"]["stale_status"] == "stale"
    assert checks["pending_chat_action_recovery"]["evidence"]["permission_granting"] is False
    assert checks["runtime_controls_and_breakers"]["status"] == "skipped"
    assert checks["reference_repository_hygiene"]["status"] == "skipped"
    assert not (tmp_path / ".harness").exists()


def test_orchestration_readiness_cli_json_does_not_initialize_project(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["orchestration", "audit", "--project", str(tmp_path), "--no-references", "--output", "json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema_version"] == "harness.orchestration_readiness_audit/v1"
    assert payload["safety"]["filesystem_modified"] is False
    assert payload["checks"][0]["schema_version"] == "harness.orchestration_readiness_check/v1"
    assert not (tmp_path / ".harness").exists()


def test_orchestration_readiness_fails_corrupt_checkpoint_evidence(tmp_path: Path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    objective = store.create_objective("Checkpoint integrity objective")
    checkpoint = create_objective_checkpoint(
        tmp_path,
        objective.id,
        label="Supervisor checkpoint",
        reason="review before dispatch",
    )
    resolve_objective_checkpoint(
        tmp_path,
        objective.id,
        checkpoint.checkpoint_id,
        verdict="approved",
        approval_id="approval_readiness",
    )
    evidence_path = tmp_path / HARNESS_DIR / "autonomy" / "objectives" / f"{objective.id}.checkpoints.jsonl"
    lines = [json.loads(line) for line in evidence_path.read_text(encoding="utf-8").splitlines()]
    lines[0]["label"] = "tampered checkpoint"
    evidence_path.write_text("\n".join(json.dumps(line, sort_keys=True) for line in lines) + "\n", encoding="utf-8")

    result = run_orchestration_readiness_audit(tmp_path, include_references=False)
    supervisor = _checks(result.model_dump(mode="json"))["supervisor_checkpoints"]

    assert result.ok is False
    assert supervisor["status"] == "fail"
    assert supervisor["evidence"]["checkpoint_evidence_verified"][0]["ok"] is False
    assert "event_hash_chain" in supervisor["evidence"]["checkpoint_evidence_verified"][0]["failed_check_ids"]
    assert "harness objectives checkpoints verify" in supervisor["next_actions"][0]


def test_orchestration_readiness_local_server_route_is_passive(tmp_path: Path) -> None:
    payload = _route_get(
        "/orchestration/readiness",
        project_root=tmp_path,
        store=SQLiteStore(tmp_path),
        cfg=default_config(),
        host="127.0.0.1",
        port=8765,
        query={"include_references": ["false"]},
    )

    assert payload is not None
    assert payload["schema_version"] == "harness.orchestration_readiness_audit/v1"
    assert payload["summary_projection"]["schema_version"] == "harness.orchestration_readiness_summary/v1"
    assert payload["safety"]["reference_code_imported"] is False
    assert payload["safety"]["reference_contents_included"] is False
    assert payload["safety"]["provider_called"] is False
    assert payload["safety"]["network_called"] is False
    assert payload["safety"]["adapter_execution_started"] is False
    assert payload["safety"]["filesystem_modified"] is False
    assert payload["safety"]["permission_granting"] is False
    assert not (tmp_path / ".harness").exists()


def test_tui_dashboard_surfaces_readiness_without_initializing_project(tmp_path: Path) -> None:
    dashboard = build_tui_dashboard(tmp_path)
    model = build_right_panel_model(dashboard, {"palette": build_command_palette()}, "", "dashboard")
    rendered = render_right_panel(model)

    assert dashboard["orchestration_readiness"]["schema_version"] == "harness.orchestration_readiness_summary/v1"
    assert dashboard["orchestration_readiness"]["source"] == "operator_context_bounded_passive_readiness_sample"
    assert dashboard["orchestration_readiness"]["summary"]["deep_audit_required"] is True
    assert dashboard["orchestration_readiness"]["safety"]["provider_called"] is False
    assert "Readiness:" in rendered
    assert "Audit: harness orchestration audit --project . --output json" in rendered
    assert not (tmp_path / ".harness").exists()


def test_orchestration_readiness_reference_hygiene_uses_metadata_only(tmp_path: Path) -> None:
    refs_root = tmp_path / "refs"
    _init_git_repo(refs_root, "microsoft-agent-framework")

    result = run_orchestration_readiness_audit(tmp_path, reference_root=refs_root)
    payload = result.model_dump(mode="json")
    reference = _checks(payload)["reference_repository_hygiene"]

    assert result.ok is True
    assert reference["status"] == "warning"
    assert reference["evidence"]["summary"]["repository_count"] == 1
    assert reference["evidence"]["summary"]["expected_repository_count"] == len(CURATED_REFERENCE_REPOSITORIES)
    assert reference["evidence"]["summary"]["missing_expected_repository_count"] == (
        len(CURATED_REFERENCE_REPOSITORIES) - 1
    )
    assert reference["evidence"]["summary"]["missing_required_reference_pattern_count"] > 0
    assert set(reference["evidence"]["required_reference_patterns"]) == set(REQUIRED_REFERENCE_PATTERNS)
    assert "microsoft-agent-framework" in reference["evidence"]["reference_pattern_coverage"]["agent_runtime"]
    assert "low_level_isolation" in reference["evidence"]["missing_required_reference_patterns"]
    assert "microsoft-agent-framework" not in reference["evidence"]["missing_expected_repository_names"]
    assert reference["evidence"]["summary"]["contents_included"] is False
    assert reference["evidence"]["summary"]["execution_allowed"] is False
    assert reference["evidence"]["summary"]["model_context_allowed"] is False
    assert reference["evidence"]["authority"]["mutation_allowed"] is False
    assert reference["evidence"]["repository_names"] == ["microsoft-agent-framework"]


def test_orchestration_readiness_initialized_project_checks_runtime_surfaces(tmp_path: Path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    objective = store.create_objective("Readiness objective")
    store.create_task(
        "Dry task",
        objective_id=objective.id,
        metadata={"execution_adapter": "dry_run", "task_type": "phase_1a_test"},
    )

    result = run_orchestration_readiness_audit(tmp_path, include_references=False)
    payload = result.model_dump(mode="json")
    checks = _checks(payload)

    assert payload["ok"] is True
    assert payload["initialized"] is True
    assert checks["durable_supervisor_state"]["evidence"]["objectives"] == 1
    assert checks["runtime_controls_and_breakers"]["status"] == "pass"
    assert checks["progress_observability"]["status"] == "pass"
    assert checks["append_only_objective_evidence"]["status"] == "pass"
    assert checks["append_only_objective_evidence"]["evidence"]["objectives_with_run_evidence"] == 0
    assert checks["append_only_objective_evidence"]["evidence"]["objectives_missing_evidence"] == []
    assert checks["external_protocol_compatibility"]["status"] == "pass"
    assert checks["external_protocol_compatibility"]["evidence"]["summary"]["fail_closed_count"] >= 4
    assert checks["external_protocol_compatibility"]["evidence"]["telemetry_contract_gaps"] == []
    assert checks["replay_drift_detection"]["status"] == "pass"
    assert checks["replay_drift_detection"]["evidence"]["summary"]["fail"] == 0
    assert checks["replay_drift_detection"]["evidence"]["safety"]["adapter_execution_started"] is False
    assert checks["schema_compatibility_contracts"]["status"] == "pass"
    assert checks["schema_compatibility_contracts"]["evidence"]["summary"]["critical_present_count"] == checks[
        "schema_compatibility_contracts"
    ]["evidence"]["summary"]["critical_schema_count"]
    assert checks["workflow_coordination_contracts"]["status"] == "pass"
    assert checks["workflow_coordination_contracts"]["evidence"]["safety"]["filesystem_modified"] is False
    assert checks["orchestration_scenario_conformance"]["status"] == "pass"
    assert checks["orchestration_scenario_conformance"]["evidence"]["safety"]["filesystem_modified"] is False
    assert checks["agentic_security_controls"]["status"] == "pass"
    assert checks["agentic_security_controls"]["evidence"]["dependent_check_statuses"]["runtime_controls_and_breakers"] == "pass"
    assert checks["protocol_and_tool_exposure"]["status"] == "pass"
    assert "invalid" not in checks["protocol_and_tool_exposure"]["evidence"]["model_visible_tool_ids"]
    assert checks["protocol_and_tool_exposure"]["evidence"]["exposed_boundary_tools"] == []
    assert checks["protocol_and_tool_exposure"]["evidence"]["loose_model_visible_schemas"] == []
    assert checks["protocol_and_tool_exposure"]["evidence"]["session_read_tools_default_tool_ids"] == [
        "artifact-read",
        "glob",
        "grep",
        "read",
    ]
    assert checks["protocol_and_tool_exposure"]["evidence"]["session_read_tools_default_extras"] == []
    assert checks["protocol_and_tool_exposure"]["evidence"]["session_read_tools_default_missing"] == []
    assert checks["protocol_and_tool_exposure"]["evidence"]["session_read_tools_default_not_model_visible"] == []


def test_orchestration_readiness_runtime_controls_map_to_registered_descriptors(tmp_path: Path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    store.disable_execution_control(
        "backend",
        "codex_cli",
        reason="pause Codex backend",
        actor="test",
    )

    result = run_orchestration_readiness_audit(tmp_path, include_references=False)
    runtime = _checks(result.model_dump(mode="json"))["runtime_controls_and_breakers"]

    assert result.ok is True
    assert runtime["status"] == "pass"
    assert runtime["evidence"]["active_controls"] == 1
    assert runtime["evidence"]["active_control_targets"] == [
        {"target_kind": "backend", "target_id": "codex_cli", "reason": "pause Codex backend"}
    ]
    assert runtime["evidence"]["unmatched_active_controls"] == []
    assert runtime["evidence"]["open_breakers"] == 0


def test_orchestration_readiness_warns_on_run_linked_objective_missing_jsonl_evidence(tmp_path: Path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    objective = store.create_objective("Run-linked objective")
    run = store.create_run("Existing run evidence", "phase_1a_test", status="succeeded", objective_id=objective.id)

    result = run_orchestration_readiness_audit(tmp_path, include_references=False)
    payload = result.model_dump(mode="json")
    checks = _checks(payload)
    evidence = checks["append_only_objective_evidence"]["evidence"]

    assert payload["ok"] is True
    assert payload["summary"]["warning"] == 1
    assert checks["append_only_objective_evidence"]["status"] == "warning"
    assert checks["append_only_objective_evidence"]["gaps"] == [
        f"Run-linked objectives missing objective evidence: {objective.id}"
    ]
    assert "objective runner" in checks["append_only_objective_evidence"]["next_actions"][0]
    assert checks["append_only_objective_evidence"]["next_actions"][1].endswith(
        f"reconcile-evidence {objective.id} --project {tmp_path} --dry-run --output json"
    )
    assert evidence["objective_count"] == 1
    assert evidence["objectives_with_run_evidence"] == 1
    assert evidence["objectives_with_evidence"] == 0
    assert evidence["objectives_missing_evidence"] == [objective.id]
    assert evidence["objectives_missing_evidence_count"] == 1
    assert evidence["reconciliation_dry_run_commands"] == [
        f"harness objectives reconcile-evidence {objective.id} --project {tmp_path} --dry-run --output json"
    ]
    assert (tmp_path / ".harness" / "runs" / run.id).exists()
    assert not (tmp_path / ".harness" / "autonomy" / "objectives" / f"{objective.id}.jsonl").exists()


def test_orchestration_readiness_warns_on_invalid_pending_action_metadata_without_cleanup(tmp_path: Path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    session = store.create_session(
        title="Invalid pending action",
        metadata={
            "pending_chat_action": {
                "schema_version": "harness.pending_chat_action/v1",
                "kind": "task_draft",
            }
        },
    )

    result = run_orchestration_readiness_audit(tmp_path, include_references=False)
    payload = result.model_dump(mode="json")
    check = _checks(payload)["pending_chat_action_recovery"]

    assert payload["ok"] is True
    assert check["status"] == "warning"
    assert check["evidence"]["current_sessions"]["invalid_count"] == 1
    assert check["evidence"]["current_sessions"]["warning_session_ids"] == [session.id]
    assert check["evidence"]["synthetic"]["cleanup_command"] == "harness sessions clear-pending-action sess_readiness"
    assert check["evidence"]["synthetic"]["cleanup_route"] == "DELETE /sessions/sess_readiness/pending-action"
    assert check["evidence"]["cleanup_mutation_scope"] == "session_metadata_only"
    assert check["evidence"]["execution_started"] is False
    assert check["evidence"]["provider_called"] is False
    assert check["evidence"]["network_called"] is False
    assert check["evidence"]["filesystem_modified"] is False
    assert check["evidence"]["permission_granting"] is False
    assert "pending_chat_action" in store.get_session(session.id).metadata
    assert not store.list_tasks()
