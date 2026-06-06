import json
from pathlib import Path

from typer.testing import CliRunner

from harness.cli.main import app
from harness.external_protocols import build_external_protocol_catalog, get_external_protocol_descriptor
from harness.orchestration_readiness import run_orchestration_readiness_audit


runner = CliRunner()


def _protocols(payload: dict) -> dict[str, dict]:
    return {item["id"]: item for item in payload["protocols"]}


def _checks(payload: dict) -> dict[str, dict]:
    return {item["id"]: item for item in payload["checks"]}


def test_external_protocol_catalog_is_read_only_and_fail_closed_without_init(tmp_path: Path) -> None:
    catalog = build_external_protocol_catalog(tmp_path)
    payload = catalog.model_dump(mode="json")
    protocols = _protocols(payload)

    assert payload["schema_version"] == "harness.external_protocol_catalog/v1"
    assert payload["ok"] is True
    assert payload["initialized"] is False
    assert payload["safety"]["process_started"] is False
    assert payload["safety"]["network_called"] is False
    assert payload["safety"]["tool_execution_started"] is False
    assert payload["safety"]["agent_execution_started"] is False
    assert payload["safety"]["permission_granting"] is False
    assert {"openai_chat", "openai_responses", "anthropic_messages"}.issubset(
        set(payload["registered_model_protocols"])
    )
    assert protocols["local_server_openapi"]["status"] == "metadata_only"
    assert protocols["local_session_tools"]["status"] == "implemented"
    assert protocols["mcp_tool"]["status"] == "fail_closed"
    assert protocols["mcp_tool"]["runtime_enabled"] is False
    assert protocols["mcp_tool"]["default_model_visible"] is False
    assert protocols["mcp_tool"]["telemetry_contracts"] == [
        "opentelemetry.semconv.gen_ai.mcp",
        "w3c_trace_context",
    ]
    assert protocols["mcp_tool"]["authority"]["process_start_allowed"] is False
    assert protocols["mcp_tool"]["authority"]["network_allowed"] is False
    assert protocols["mcp_tool"]["authority"]["tool_execution_allowed"] is False
    assert protocols["mcp_cached_resource"]["status"] == "cached_resource_only"
    assert protocols["a2a_remote_agent"]["status"] == "fail_closed"
    assert protocols["grpc_remote_tool"]["status"] == "fail_closed"
    assert not (tmp_path / ".harness").exists()


def test_external_protocol_cli_list_and_inspect_are_metadata_only(tmp_path: Path) -> None:
    listed = runner.invoke(app, ["protocols", "list", "--project", str(tmp_path), "--output", "json"])
    inspected = runner.invoke(
        app,
        ["protocols", "inspect", "a2a_remote_agent", "--project", str(tmp_path), "--output", "json"],
    )

    assert listed.exit_code == 0, listed.output
    payload = json.loads(listed.output)
    protocols = _protocols(payload)
    assert payload["schema_version"] == "harness.external_protocol_catalog/v1"
    assert protocols["external_openapi_tool"]["status"] == "fail_closed"
    assert protocols["mcp_tool"]["blocked_reasons"] == [
        "mcp_tool_execution_disabled",
        "mcp_process_launch_disabled",
        "mcp_network_connection_disabled",
    ]
    assert "MCP client spans" in protocols["mcp_tool"]["next_actions"][0]
    assert inspected.exit_code == 0, inspected.output
    inspected_payload = json.loads(inspected.output)
    assert inspected_payload["schema_version"] == "harness.external_protocol_descriptor/v1"
    assert inspected_payload["ok"] is True
    assert inspected_payload["id"] == "a2a_remote_agent"
    assert inspected_payload["status"] == "fail_closed"
    assert inspected_payload["authority"]["agent_execution_allowed"] is False
    assert inspected_payload["authority"]["network_allowed"] is False
    assert not (tmp_path / ".harness").exists()


def test_external_protocol_inspect_missing_fails_closed(tmp_path: Path) -> None:
    result = runner.invoke(app, ["protocols", "inspect", "missing", "--project", str(tmp_path), "--output", "json"])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["schema_version"] == "harness.external_protocol_catalog/v1"
    assert payload["ok"] is False
    assert payload["errors"] == ["External protocol not found: missing"]
    assert payload["safety"]["network_called"] is False
    assert payload["safety"]["process_started"] is False
    assert not (tmp_path / ".harness").exists()


def test_get_external_protocol_descriptor_returns_single_descriptor(tmp_path: Path) -> None:
    descriptor = get_external_protocol_descriptor(tmp_path, "local_server_openapi")

    assert descriptor.id == "local_server_openapi"
    assert descriptor.status == "metadata_only"
    assert descriptor.authority.process_start_allowed is False
    assert descriptor.authority.permission_granting is False


def test_readiness_includes_external_protocol_compatibility(tmp_path: Path) -> None:
    result = run_orchestration_readiness_audit(tmp_path, include_references=False)
    payload = result.model_dump(mode="json")
    check = _checks(payload)["external_protocol_compatibility"]

    assert check["status"] == "pass"
    assert check["evidence"]["missing_protocol_ids"] == []
    assert check["evidence"]["missing_model_protocols"] == []
    assert check["evidence"]["risky_default_model_visible_protocols"] == []
    assert check["evidence"]["unsafe_runtime_enabled_protocols"] == []
    assert check["evidence"]["unsafe_authority_protocols"] == []
    assert check["evidence"]["status_mismatches"] == []
    assert check["evidence"]["telemetry_contract_gaps"] == []
    assert check["evidence"]["telemetry_contracts"]["a2a_remote_agent"] == [
        "opentelemetry.semconv.gen_ai.agent",
        "w3c_trace_context",
    ]
    assert check["evidence"]["safety_issues"] == []
    assert check["evidence"]["summary"]["fail_closed_count"] >= 4
    assert "harness protocols list" in check["evidence"]["commands"][0]
    assert not (tmp_path / ".harness").exists()
