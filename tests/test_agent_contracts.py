import json
from pathlib import Path

from typer.testing import CliRunner

from harness.agent_contracts import AGENT_CONTRACT_SCHEMA_VERSION, build_agent_contract
from harness.cli.main import app


runner = CliRunner()


def test_builtin_agent_contract_is_typed_non_authoritative_and_stable(tmp_path: Path) -> None:
    contract = build_agent_contract(tmp_path, "repo_inspector", workbench_id="coding")
    again = build_agent_contract(tmp_path, "repo_inspector", workbench_id="coding")
    payload = contract.model_dump(mode="json")

    assert payload["schema_version"] == AGENT_CONTRACT_SCHEMA_VERSION
    assert payload["ok"] is True
    assert payload["contract_id"] == again.contract_id
    assert payload["contract_sha256"] == again.contract_sha256
    assert payload["agent_id"] == "repo_inspector"
    assert payload["source_kind"] == "builtin"
    assert payload["workbench_id"] == "coding"
    assert payload["kind"] == "specialist"
    assert payload["model_profile"] == "codex_supervised"
    assert payload["backend_id"] == "codex_cli"
    assert payload["tool_policy_id"] == "read_only"
    assert payload["tool_policy"]["allowed_tool_ids"] == ["artifact_read", "repo_read"]
    assert payload["tool_policy"]["network"] == "forbidden"
    assert payload["tool_policy"]["active_repo_write"] == "forbidden"
    assert payload["budget_policy"]["per_handoff_budget_required"] is True
    assert payload["budget_policy"]["agent_may_increase_budget"] is False
    assert payload["trace_policy"]["w3c_traceparent_required"] is True
    assert payload["authority"]["identity_authority"] is False
    assert payload["authority"]["orchestration_policy_authority"] is False
    assert payload["authority"]["adapter_execution_allowed"] is False
    assert payload["authority"]["tool_execution_allowed"] is False
    assert payload["authority"]["permission_granting"] is False
    assert payload["safety"]["read_only"] is True
    assert payload["safety"]["source_body_loaded"] is False


def test_agents_contract_cli_is_read_only_for_builtin_agents_without_project_init(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["agents", "contract", "repo_inspector", "--project", str(tmp_path), "--output", "json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema_version"] == AGENT_CONTRACT_SCHEMA_VERSION
    assert payload["ok"] is True
    assert payload["source_kind"] == "builtin"
    assert payload["authority"]["permission_granting"] is False
    assert not (tmp_path / ".harness").exists()


def test_agent_contract_missing_agent_fails_closed_without_project_init(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["agents", "contract", "missing_agent", "--project", str(tmp_path), "--output", "json"],
    )

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["schema_version"] == AGENT_CONTRACT_SCHEMA_VERSION
    assert payload["ok"] is False
    assert payload["source_kind"] == "unknown"
    assert payload["validation_errors"] == ["Agent not found: missing_agent"]
    assert payload["authority"]["agent_execution_allowed"] is False
    assert payload["authority"]["network_allowed"] is False
    assert payload["authority"]["permission_granting"] is False
    assert payload["safety"]["read_only"] is True
    assert not (tmp_path / ".harness").exists()


def test_agent_contract_resolves_imported_project_agent_from_stored_metadata(tmp_path: Path) -> None:
    bundle_path = tmp_path / "agents" / "custom_reader"
    init = runner.invoke(app, ["init", "--project", str(tmp_path)])
    scaffold = runner.invoke(
        app,
        [
            "agents",
            "scaffold",
            "custom_reader",
            "--workbench",
            "coding",
            "--kind",
            "specialist",
            "--model-profile",
            "codex_supervised",
            "--tool-policy",
            "read_only",
            "--memory-scope",
            "project",
            "--output",
            str(bundle_path),
            "--output-format",
            "json",
        ],
    )
    imported = runner.invoke(
        app,
        ["agents", "import", str(bundle_path), "--project", str(tmp_path), "--output", "json"],
    )
    contract = runner.invoke(
        app,
        ["agents", "contract", "custom_reader", "--project", str(tmp_path), "--output", "json"],
    )

    assert init.exit_code == 0, init.output
    assert scaffold.exit_code == 0, scaffold.output
    assert imported.exit_code == 0, imported.output
    assert contract.exit_code == 0, contract.output
    payload = json.loads(contract.output)
    imported_payload = json.loads(imported.output)
    assert payload["ok"] is True
    assert payload["source_kind"] == "project"
    assert payload["source_path"] == str(bundle_path.resolve())
    assert payload["source_content_sha256"] == imported_payload["content_sha256"]
    assert payload["tool_policy"]["network"] == "forbidden"
    assert payload["authority"]["agent_execution_allowed"] is False
