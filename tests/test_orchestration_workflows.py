import json
from pathlib import Path

from typer.testing import CliRunner

from harness.cli.main import app
from harness.config import default_config
from harness.local_server import _route_get
from harness.memory.sqlite_store import SQLiteStore
from harness.orchestration_workflows import build_workflow_coordination_catalog


runner = CliRunner()


def _patterns(payload: dict) -> dict[str, dict]:
    return {pattern["id"]: pattern for pattern in payload["patterns"]}


def _state_classes(payload: dict) -> dict[str, dict]:
    return {state_class["id"]: state_class for state_class in payload["state_classes"]}


def test_workflow_coordination_catalog_is_passive_and_complete_without_init(tmp_path: Path) -> None:
    catalog = build_workflow_coordination_catalog(tmp_path)
    payload = catalog.model_dump(mode="json")
    patterns = _patterns(payload)
    state_classes = _state_classes(payload)

    assert payload["schema_version"] == "harness.workflow_coordination_catalog/v1"
    assert payload["ok"] is True
    assert payload["initialized"] is False
    assert payload["summary"]["missing_required_pattern_count"] == 0
    assert payload["summary"]["missing_required_state_class_count"] == 0
    assert payload["summary"]["fail"] == 0
    assert payload["safety"]["read_only"] is True
    assert payload["safety"]["metadata_only"] is True
    assert payload["safety"]["reference_code_imported"] is False
    assert payload["safety"]["reference_contents_included"] is False
    assert payload["safety"]["provider_called"] is False
    assert payload["safety"]["network_called"] is False
    assert payload["safety"]["adapter_execution_started"] is False
    assert payload["safety"]["tool_execution_started"] is False
    assert payload["safety"]["agent_execution_started"] is False
    assert payload["safety"]["filesystem_modified"] is False
    assert payload["safety"]["permission_granting"] is False
    assert payload["safety"]["artifact_bodies_read"] is False
    assert payload["safety"]["model_context_allowed"] is False
    assert set(payload["required_pattern_ids"]).issubset(patterns)
    assert set(payload["required_state_class_ids"]) == {"session_state", "workflow_state", "memory_state", "artifact_state"}
    assert state_classes["memory_state"]["model_context_allowed_by_default"] is False
    assert "approval" in " ".join(state_classes["memory_state"]["authority_notes"]).lower()
    assert patterns["durable_supervisor"]["status"] == "pass"
    assert patterns["durable_supervisor"]["execution_mode"] == "local_control_plane"
    assert "microsoft_agent_framework" in patterns["durable_supervisor"]["reference_patterns"]
    assert patterns["bounded_parallel_fanout"]["evidence"]["has_max_parallel_bound"] is True
    assert patterns["bounded_parallel_fanout"]["evidence"]["slow_branch_replay_detected"] is True
    assert "batch_completed_missing_terminal_task" in patterns["append_only_replay"]["evidence"]["replay_issue_codes"]
    assert patterns["typed_agent_handoff"]["execution_mode"] == "record_only_handoff"
    assert patterns["human_approval_pause"]["execution_mode"] == "durable_hitl_gate"
    assert patterns["external_protocol_boundary"]["evidence"]["risky_protocol_ids"] == []
    assert patterns["memory_context_boundary"]["evidence"]["hosted_memory_allowed"] is False
    assert "memory_not_authority" in patterns["memory_context_boundary"]["evidence"]["warnings"]
    assert not (tmp_path / ".harness").exists()


def test_workflow_coordination_cli_and_local_server_are_metadata_only(tmp_path: Path) -> None:
    cli = runner.invoke(app, ["orchestration", "workflows", "--project", str(tmp_path), "--output", "json"])
    suite = runner.invoke(app, ["evals", "run", "--suite", "orchestration-workflows", "--project", str(tmp_path), "--output", "json"])
    route = _route_get(
        "/orchestration/workflows",
        project_root=tmp_path,
        store=SQLiteStore(tmp_path),
        cfg=default_config(),
        host="127.0.0.1",
        port=8765,
        query={},
    )

    assert cli.exit_code == 0, cli.output
    cli_payload = json.loads(cli.output)
    assert cli_payload["schema_version"] == "harness.workflow_coordination_catalog/v1"
    assert cli_payload["safety"]["reference_code_imported"] is False
    assert cli_payload["safety"]["provider_called"] is False
    assert suite.exit_code == 0, suite.output
    suite_payload = json.loads(suite.output)
    assert suite_payload["schema_version"] == "harness.workflow_coordination_catalog/v1"
    assert route is not None
    assert route["schema_version"] == "harness.workflow_coordination_catalog/v1"
    assert route["summary_projection"]["schema_version"] == "harness.workflow_coordination_summary/v1"
    assert route["summary_projection"]["pattern_ids"] == [pattern["id"] for pattern in route["patterns"]]
    assert route["safety"]["network_called"] is False
    assert route["safety"]["filesystem_modified"] is False
    assert route["safety"]["permission_granting"] is False
    assert not (tmp_path / ".harness").exists()
