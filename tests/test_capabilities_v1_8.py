import json

from typer.testing import CliRunner

from harness.capabilities import build_capability_catalog
from harness.chat import ChatSessionState, handle_chat_input
from harness.cli.main import app


runner = CliRunner()


def test_build_capability_catalog_matches_registered_adapters(tmp_path) -> None:
    catalog = build_capability_catalog(tmp_path)
    by_id = {capability.id: capability for capability in catalog.capabilities}

    assert catalog.schema_version == "harness.capability_catalog/v1"
    assert set(by_id) >= {"dry_run", "read_only_summary", "codex_isolated_edit", "repo_planning"}
    assert by_id["dry_run"].execution_adapter == "dry_run"
    assert by_id["dry_run"].supported_task_types == ["phase_1a_test"]
    assert by_id["repo_planning"].supported_task_types == ["repo_planning"]
    assert by_id["read_only_summary"].readiness == "requires_approval_before_execution"
    assert by_id["read_only_summary"].sandbox_profile["id"] == "read_only_codex"
    assert by_id["codex_isolated_edit"].required_approvals == ["hosted_provider_codex"]
    assert by_id["codex_isolated_edit"].sandbox_profile["id"] == "isolated_workspace_codex"


def test_capabilities_cli_list_and_inspect_are_read_only_without_init(tmp_path, monkeypatch) -> None:
    def fail_backend(*_args, **_kwargs):
        raise AssertionError("capability catalog must not preflight backends")

    monkeypatch.setattr("harness.cli.main.CodexCliBackend", fail_backend)
    monkeypatch.setattr("harness.cli.main.LocalOpenAICompatibleBackend", fail_backend)
    monkeypatch.setattr("harness.cli.main.DockerImageManager", fail_backend)

    listed = runner.invoke(app, ["capabilities", "list", "--project", str(tmp_path), "--output", "json"])
    inspected = runner.invoke(
        app,
        ["capabilities", "inspect", "dry_run", "--project", str(tmp_path), "--output", "json"],
    )

    assert listed.exit_code == 0, listed.output
    payload = json.loads(listed.output)
    assert payload["schema_version"] == "harness.capability_catalog/v1"
    assert payload["ok"] is True
    assert {capability["id"] for capability in payload["capabilities"]} >= {
        "dry_run",
        "read_only_summary",
        "codex_isolated_edit",
        "repo_planning",
    }
    assert inspected.exit_code == 0, inspected.output
    inspected_payload = json.loads(inspected.output)
    assert inspected_payload["schema_version"] == "harness.capability/v1"
    assert inspected_payload["ok"] is True
    assert inspected_payload["id"] == "dry_run"
    assert inspected_payload["supported_task_types"] == ["phase_1a_test"]
    assert inspected_payload["sandbox_profile"]["id"] == "none"
    assert not (tmp_path / ".harness").exists()


def test_capabilities_cli_inspect_missing_fails_closed(tmp_path) -> None:
    result = runner.invoke(
        app,
        ["capabilities", "inspect", "missing", "--project", str(tmp_path), "--output", "json"],
    )

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["schema_version"] == "harness.capability_catalog/v1"
    assert payload["ok"] is False
    assert payload["errors"] == ["Capability not found: missing"]
    assert not (tmp_path / ".harness").exists()


def test_chat_context_includes_capabilities_without_backend_preflight(tmp_path, monkeypatch) -> None:
    def fail_backend(*_args, **_kwargs):
        raise AssertionError("chat context must not preflight backends for capabilities")

    monkeypatch.setattr("harness.cli.main.CodexCliBackend", fail_backend)
    monkeypatch.setattr("harness.cli.main.LocalOpenAICompatibleBackend", fail_backend)
    monkeypatch.setattr("harness.cli.main.DockerImageManager", fail_backend)

    result = runner.invoke(app, ["--project", str(tmp_path), "--output", "json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema_version"] == "harness.chat/v1"
    assert "registered_adapters" in payload
    assert payload["capabilities"]["schema_version"] == "harness.capability_catalog/v1"
    assert {capability["id"] for capability in payload["capabilities"]["capabilities"]} >= {
        "dry_run",
        "read_only_summary",
        "codex_isolated_edit",
        "repo_planning",
    }
    assert not (tmp_path / ".harness").exists()


def test_chat_capabilities_aliases_are_deterministic_and_read_only(tmp_path) -> None:
    state = ChatSessionState()

    slash = handle_chat_input("/capabilities", tmp_path, state)
    natural = handle_chat_input("what can Harness do here?", tmp_path, state)
    approvals = handle_chat_input("which actions need approval?", tmp_path, state)

    assert slash["kind"] == "capabilities"
    assert natural["kind"] == "capabilities"
    assert approvals["kind"] == "capabilities"
    rendered = "\n".join(natural["lines"])
    assert "dry_run: task_types=phase_1a_test" in rendered
    assert "read_only_summary: task_types=read_only_repo_summary" in rendered
    assert "approvals=hosted_provider_codex" in "\n".join(approvals["lines"])
    assert not (tmp_path / ".harness").exists()


def test_capabilities_mark_disabled_adapter_unavailable_after_init(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    disabled = runner.invoke(
        app,
        [
            "controls",
            "disable",
            "--target-kind",
            "adapter",
            "--target-id",
            "dry_run",
            "--reason",
            "operator pause",
            "--project",
            str(tmp_path),
            "--output",
            "json",
        ],
    )
    assert disabled.exit_code == 0, disabled.output

    catalog = build_capability_catalog(tmp_path)
    dry_run = {capability.id: capability for capability in catalog.capabilities}["dry_run"]

    assert dry_run.readiness == "unavailable"
    assert dry_run.readiness_reasons == ["control_disabled: adapter:dry_run. operator pause"]
    assert dry_run.blocked_state_explanations[0].code.value == "disabled_adapter"


def test_chat_context_surfaces_runtime_controls_without_execution(tmp_path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    assert runner.invoke(
        app,
        [
            "controls",
            "disable",
            "--target-kind",
            "hosted_boundary",
            "--target-id",
            "*",
            "--reason",
            "pause hosted boundary",
            "--project",
            str(tmp_path),
            "--output",
            "json",
        ],
    ).exit_code == 0

    def fail_backend(*_args, **_kwargs):
        raise AssertionError("chat context must not preflight backends")

    monkeypatch.setattr("harness.cli.main.CodexCliBackend", fail_backend)
    result = runner.invoke(app, ["--project", str(tmp_path), "--output", "json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["runtime_controls"]["schema_version"] == "harness.execution_controls_summary/v1"
    assert payload["runtime_controls"]["controls"][0]["target_kind"] == "hosted_boundary"
    capability_by_id = {item["id"]: item for item in payload["capabilities"]["capabilities"]}
    assert capability_by_id["read_only_summary"]["readiness"] == "unavailable"
    assert capability_by_id["read_only_summary"]["blocked_state_explanations"][0]["code"] == "disabled_adapter"
