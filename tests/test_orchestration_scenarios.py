import json
from pathlib import Path

from typer.testing import CliRunner

from harness.cli.main import app
from harness.config import default_config
from harness.local_server import _route_get
from harness.memory.sqlite_store import SQLiteStore
from harness.orchestration_scenarios import build_orchestration_scenario_catalog


runner = CliRunner()


def _cases(payload: dict) -> dict[str, dict]:
    return {case["id"]: case for case in payload["cases"]}


def test_orchestration_scenario_catalog_is_passive_and_layered_without_init(tmp_path: Path) -> None:
    catalog = build_orchestration_scenario_catalog(tmp_path)
    payload = catalog.model_dump(mode="json")
    cases = _cases(payload)

    assert payload["schema_version"] == "harness.orchestration_scenario_catalog/v1"
    assert payload["ok"] is True
    assert payload["initialized"] is False
    assert payload["summary"]["missing_required_case_count"] == 0
    assert payload["summary"]["missing_required_layer_count"] == 0
    assert payload["summary"]["fail"] == 0
    assert set(payload["required_layers"]) == {"unit", "contract", "replay", "scenario", "security", "benchmark"}
    assert set(payload["required_case_ids"]).issubset(cases)
    assert {case["layer"] for case in payload["cases"]} >= set(payload["required_layers"])
    assert payload["safety"]["read_only"] is True
    assert payload["safety"]["metadata_only"] is True
    assert payload["safety"]["synthetic_probe_only"] is True
    assert payload["safety"]["reference_code_imported"] is False
    assert payload["safety"]["reference_contents_included"] is False
    assert payload["safety"]["provider_called"] is False
    assert payload["safety"]["network_called"] is False
    assert payload["safety"]["adapter_execution_started"] is False
    assert payload["safety"]["tool_execution_started"] is False
    assert payload["safety"]["agent_execution_started"] is False
    assert payload["safety"]["filesystem_modified"] is False
    assert payload["safety"]["artifact_bodies_read"] is False
    assert payload["safety"]["model_context_allowed"] is False
    assert payload["safety"]["live_benchmark_execution_allowed"] is False
    assert payload["safety"]["approval_store_instantiated"] is False

    assert cases["duplicate_dispatch_redelivery"]["detected_signals"] == ["duplicate_side_effect_dispatch"]
    assert cases["slow_branch_barrier"]["detected_signals"] == ["batch_completed_missing_terminal_task"]
    assert cases["approval_reject_pause"]["detected_signals"] == ["dispatch_after_blocking_event"]
    assert cases["missing_terminal_event"]["detected_signals"] == ["missing_stopped_event"]
    assert "checkpoint_blocked_stop_reason_mismatch" in cases["checkpoint_reject_stop"]["detected_signals"]
    assert "checkpoint_gate_rejected_branch_present" in cases["checkpoint_reject_stop"]["detected_signals"]
    assert cases["unsafe_memory_to_hosted_model"]["evidence"]["decision"]["allowed"] is False
    assert "memory_not_authority" in cases["unsafe_memory_to_hosted_model"]["detected_signals"]
    assert cases["remote_protocol_fail_closed"]["evidence"]["risky_protocol_ids"] == []
    assert set(cases["remote_protocol_fail_closed"]["evidence"]["fail_closed_protocol_ids"]) >= {
        "mcp_tool",
        "external_openapi_tool",
        "a2a_remote_agent",
        "grpc_remote_tool",
    }
    assert cases["retry_requires_idempotency"]["evidence"]["auto_allowed_unsafe_replay_adapter_ids"] == []
    assert cases["retry_requires_idempotency"]["evidence"]["not_replayable_not_forbidden_adapter_ids"] == []
    assert "fresh_approval_side_effects_gated" in cases["retry_requires_idempotency"]["detected_signals"]
    assert "live_benchmark_explicit_approval" in cases["live_benchmark_explicit_permit"]["detected_signals"]
    assert cases["live_benchmark_explicit_permit"]["evidence"]["approval_store_instantiated"] is False
    assert not (tmp_path / ".harness").exists()


def test_orchestration_scenario_cli_eval_and_local_server_are_metadata_only(tmp_path: Path) -> None:
    cli = runner.invoke(app, ["orchestration", "scenarios", "--project", str(tmp_path), "--output", "json"])
    suite = runner.invoke(
        app,
        ["evals", "run", "--suite", "orchestration-scenarios", "--project", str(tmp_path), "--output", "json"],
    )
    route = _route_get(
        "/orchestration/scenarios",
        project_root=tmp_path,
        store=SQLiteStore(tmp_path),
        cfg=default_config(),
        host="127.0.0.1",
        port=8765,
        query={},
    )

    assert cli.exit_code == 0, cli.output
    cli_payload = json.loads(cli.output)
    assert cli_payload["schema_version"] == "harness.orchestration_scenario_catalog/v1"
    assert cli_payload["summary"]["fail"] == 0
    assert cli_payload["safety"]["provider_called"] is False
    assert cli_payload["safety"]["live_benchmark_execution_allowed"] is False
    assert suite.exit_code == 0, suite.output
    suite_payload = json.loads(suite.output)
    assert suite_payload["schema_version"] == "harness.orchestration_scenario_catalog/v1"
    assert route is not None
    assert route["schema_version"] == "harness.orchestration_scenario_catalog/v1"
    assert route["summary_projection"]["schema_version"] == "harness.orchestration_scenario_summary/v1"
    assert route["summary_projection"]["case_ids"] == [case["id"] for case in route["cases"]]
    assert route["safety"]["network_called"] is False
    assert route["safety"]["filesystem_modified"] is False
    assert route["safety"]["permission_granting"] is False
    assert route["safety"]["approval_store_instantiated"] is False
    assert not (tmp_path / ".harness").exists()
