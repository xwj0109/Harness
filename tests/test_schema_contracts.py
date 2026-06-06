import json
from pathlib import Path

from typer.testing import CliRunner

from harness.cli.main import app
from harness.schema_contracts import build_schema_contract_catalog, get_schema_contract_descriptor


runner = CliRunner()


def _schemas(payload: dict) -> dict[str, dict]:
    return {item["id"]: item for item in payload["schemas"]}


def test_schema_contract_catalog_is_read_only_and_complete_without_init(tmp_path: Path) -> None:
    catalog = build_schema_contract_catalog(tmp_path)
    payload = catalog.model_dump(mode="json")
    schemas = _schemas(payload)

    assert payload["schema_version"] == "harness.schema_contract_catalog/v1"
    assert payload["ok"] is True
    assert payload["initialized"] is False
    assert payload["summary"]["critical_present_count"] == payload["summary"]["critical_schema_count"]
    assert payload["summary"]["duplicate_schema_id_count"] == 0
    assert payload["summary"]["versioned_schema_count"] == payload["summary"]["schema_count"]
    assert payload["summary"]["authority_safe_schema_count"] == payload["summary"]["schema_count"]
    assert payload["safety"]["read_only"] is True
    assert payload["safety"]["schema_validation_only"] is True
    assert payload["safety"]["network_called"] is False
    assert payload["safety"]["tool_execution_started"] is False
    assert payload["safety"]["agent_execution_started"] is False
    assert payload["safety"]["permission_granting"] is False
    assert payload["safety"]["artifact_bodies_read"] is False
    assert schemas["agent_contract"]["current_schema_version"] == "harness.agent_contract/v1"
    assert schemas["agent_discovery_catalog"]["current_schema_version"] == "harness.agent_discovery_catalog/v1"
    assert schemas["agent_discovery_catalog"]["compatibility_policy"] == "additive_only"
    assert schemas["agent_discovery_catalog"]["authority"]["execution_authority"] is False
    assert schemas["agent_discovery_catalog"]["authority"]["permission_granting"] is False
    assert schemas["agent_handoff_envelope"]["compatibility_policy"] == "breaking_requires_new_version"
    assert schemas["task_replay_receipt"]["current_schema_version"] == "harness.task_replay_receipt/v1"
    assert schemas["task_replay_receipt"]["compatibility_policy"] == "additive_only"
    assert schemas["task_replay_receipt"]["authority"]["execution_authority"] is False
    assert schemas["task_replay_receipt"]["authority"]["permission_granting"] is False
    assert schemas["orchestration_replay_audit"]["current_schema_version"] == "harness.orchestration_replay_audit/v1"
    assert schemas["orchestration_scenario_catalog"]["current_schema_version"] == "harness.orchestration_scenario_catalog/v1"
    assert schemas["orchestration_scenario_catalog"]["authority"]["execution_authority"] is False
    assert schemas["workflow_template"]["current_schema_version"] == "harness.workflow_template/v1"
    assert schemas["workflow_template"]["compatibility_policy"] == "additive_only"
    assert schemas["workflow_template"]["authority"]["execution_authority"] is False
    assert schemas["workflow_agent_selection"]["current_schema_version"] == "harness.workflow_agent_selection/v1"
    assert schemas["workflow_agent_selection"]["compatibility_policy"] == "additive_only"
    assert schemas["workflow_agent_selection"]["authority"]["permission_granting"] is False
    assert schemas["workflow_coordination_catalog"]["current_schema_version"] == "harness.workflow_coordination_catalog/v1"
    assert schemas["workflow_coordination_catalog"]["authority"]["execution_authority"] is False
    assert schemas["objective_batch_plan"]["current_schema_version"] == "harness.objective_batch_plan/v1"
    assert schemas["objective_evidence_chain"]["compatibility_policy"] == "append_only_hash_chained"
    assert schemas["sandbox_profile_catalog"]["current_schema_version"] == "harness.sandbox_profiles/v1"
    assert schemas["sandbox_profile_catalog"]["authority"]["execution_authority"] is False
    assert schemas["sandbox_profile"]["current_schema_version"] == "harness.sandbox_profile/v1"
    assert schemas["sandbox_profile"]["authority"]["process_start_allowed"] is False
    assert schemas["local_server_openapi"]["compatibility_policy"] == "metadata_projection_only"
    assert schemas["agent_contract"]["authority"]["execution_authority"] is False
    assert schemas["agent_contract"]["authority"]["permission_granting"] is False
    assert not (tmp_path / ".harness").exists()


def test_schema_contract_cli_list_and_inspect_are_metadata_only(tmp_path: Path) -> None:
    listed = runner.invoke(app, ["schemas", "list", "--project", str(tmp_path), "--output", "json"])
    inspected = runner.invoke(
        app,
        ["schemas", "inspect", "agent_handoff_envelope", "--project", str(tmp_path), "--output", "json"],
    )

    assert listed.exit_code == 0, listed.output
    payload = json.loads(listed.output)
    schemas = _schemas(payload)
    assert payload["schema_version"] == "harness.schema_contract_catalog/v1"
    assert "agent_discovery_catalog" in schemas
    assert "delegate_budget" in schemas
    assert "task_replay_receipt" in schemas
    assert schemas["external_protocol_catalog"]["current_schema_version"] == "harness.external_protocol_catalog/v1"
    assert "opentelemetry" in schemas["external_protocol_catalog"]["reference_patterns"]
    assert schemas["orchestration_replay_audit"]["compatibility_policy"] == "additive_only"
    assert schemas["orchestration_scenario_catalog"]["compatibility_policy"] == "additive_only"
    assert schemas["workflow_template"]["compatibility_policy"] == "additive_only"
    assert schemas["workflow_agent_selection"]["current_schema_version"] == "harness.workflow_agent_selection/v1"
    assert schemas["workflow_coordination_catalog"]["compatibility_policy"] == "additive_only"
    assert schemas["objective_batch_plan"]["compatibility_policy"] == "additive_only"
    assert schemas["sandbox_profile_catalog"]["compatibility_policy"] == "additive_only"
    assert schemas["sandbox_profile"]["compatibility_policy"] == "additive_only"
    assert inspected.exit_code == 0, inspected.output
    inspected_payload = json.loads(inspected.output)
    assert inspected_payload["schema_version"] == "harness.schema_contract_descriptor/v1"
    assert inspected_payload["ok"] is True
    assert inspected_payload["id"] == "agent_handoff_envelope"
    assert inspected_payload["current_schema_version"] == "harness.agent_handoff_envelope/v1"
    assert inspected_payload["authority"]["agent_execution_allowed"] is False
    assert inspected_payload["authority"]["model_context_allowed"] is False
    assert not (tmp_path / ".harness").exists()


def test_schema_contract_inspect_missing_fails_closed(tmp_path: Path) -> None:
    result = runner.invoke(app, ["schemas", "inspect", "missing", "--project", str(tmp_path), "--output", "json"])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["schema_version"] == "harness.schema_contract_catalog/v1"
    assert payload["ok"] is False
    assert payload["errors"] == ["Schema contract not found: missing"]
    assert payload["safety"]["network_called"] is False
    assert payload["safety"]["process_started"] is False
    assert payload["safety"]["permission_granting"] is False
    assert not (tmp_path / ".harness").exists()


def test_get_schema_contract_descriptor_returns_single_descriptor(tmp_path: Path) -> None:
    descriptor = get_schema_contract_descriptor(tmp_path, "trace_export")

    assert descriptor.id == "trace_export"
    assert descriptor.current_schema_version == "harness.trace_export/v1"
    assert descriptor.compatibility_policy == "additive_only"
    assert any("GenAI/MCP-compatible" in note for note in descriptor.upgrade_notes)
    assert descriptor.authority.process_start_allowed is False
    assert descriptor.authority.permission_granting is False
