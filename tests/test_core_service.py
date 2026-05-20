import json
from pathlib import Path

from typer.testing import CliRunner

from harness.cli.main import app
from harness.core_service import HarnessCoreService
from harness.memory.sqlite_store import SQLiteStore


runner = CliRunner()


def test_core_service_dry_run_creates_task_lease_run_manifest_and_events(tmp_path) -> None:
    result = HarnessCoreService().start_goal(
        "smoke test core loop",
        mode="dry_run",
        project_root=tmp_path,
    )

    assert result.schema_version == "harness.core_run/v1"
    assert result.ok is True
    assert result.mode == "dry_run"
    assert result.decision == "dry_run_no_tool_execution"
    assert result.task_id
    assert result.lease_id
    assert result.run_id
    assert result.adapter_id == "dry_run"
    assert result.manifest is not None
    assert result.manifest.exists()
    assert result.errors == []
    assert result.summary is not None
    assert result.summary.event_count >= 1
    assert {"events", "transcript", "final_report", "manifest"} <= set(result.summary.artifact_kinds)

    store = SQLiteStore(tmp_path)
    task = store.get_task(result.task_id)
    lease = store.get_task_lease(result.lease_id)
    run = store.get_run(result.run_id)
    manifest = store.build_run_manifest(result.run_id)

    assert task.status.value == "succeeded"
    assert lease.status.value == "released"
    assert run.status == "completed"
    assert manifest.task_id == result.task_id
    assert manifest.run_id == result.run_id
    assert store.list_events(result.run_id)


def test_core_service_unsupported_mode_fails_closed_without_project_state(tmp_path) -> None:
    result = HarnessCoreService().start_goal(
        "try unsafe mode",
        mode="ambient_shell",
        project_root=tmp_path,
    )

    assert result.ok is False
    assert result.decision == "unsupported_mode"
    assert result.task_id is None
    assert result.lease_id is None
    assert result.run_id is None
    assert result.manifest is None
    assert "Unsupported core mode" in result.errors[0]
    assert not (tmp_path / ".harness").exists()


def test_core_service_repo_planning_without_hosted_approval_is_blocked(tmp_path) -> None:
    result = HarnessCoreService().start_goal(
        "plan a small change",
        mode="repo_planning",
        project_root=tmp_path,
    )

    assert result.ok is False
    assert result.mode == "repo_planning"
    assert result.decision == "execution_adapter_rejected"
    assert result.task_id
    assert result.lease_id
    assert result.run_id is None
    assert result.manifest is None
    assert result.adapter_id == "repo_planning"
    assert any("hosted_provider_codex" in error for error in result.errors)
    assert SQLiteStore(tmp_path).list_runs() == []


def test_core_service_isolated_edit_without_hosted_approval_is_blocked(tmp_path) -> None:
    result = HarnessCoreService().start_goal(
        "edit a file",
        mode="codex_isolated_edit",
        project_root=tmp_path,
    )

    assert result.ok is False
    assert result.mode == "codex_isolated_edit"
    assert result.decision == "execution_adapter_rejected"
    assert result.task_id
    assert result.lease_id
    assert result.run_id is None
    assert result.manifest is None
    assert result.adapter_id == "codex_isolated_edit"
    assert any("hosted_provider_codex" in error for error in result.errors)
    assert SQLiteStore(tmp_path).list_runs() == []


def test_core_service_final_summary_references_core_identifiers_and_errors(tmp_path) -> None:
    result = HarnessCoreService().start_goal(
        "smoke test core summary",
        mode="dry_run",
        project_root=tmp_path,
    )

    assert result.summary is not None
    text = result.summary.summary_text
    assert result.run_id in text
    assert result.task_id in text
    assert result.lease_id in text
    assert "adapter_id=dry_run" in text
    assert "decision=dry_run_no_tool_execution" in text
    assert str(result.manifest) in text
    assert "errors=none" in text

    blocked = HarnessCoreService().start_goal(
        "blocked summary",
        mode="repo_planning",
        project_root=tmp_path,
    )

    assert blocked.summary is not None
    blocked_text = blocked.summary.summary_text
    assert blocked.task_id in blocked_text
    assert blocked.lease_id in blocked_text
    assert "run_id=none" in blocked_text
    assert "adapter_id=repo_planning" in blocked_text
    assert "hosted_provider_codex" in blocked_text


def test_core_service_cli_json_matches_result_shape(tmp_path) -> None:
    result = runner.invoke(
        app,
        [
            "core",
            "run",
            "smoke test core loop",
            "--mode",
            "dry_run",
            "--project",
            str(tmp_path),
            "--output",
            "json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema_version"] == "harness.core_run/v1"
    for key in (
        "ok",
        "mode",
        "decision",
        "task_id",
        "lease_id",
        "run_id",
        "adapter_id",
        "manifest",
        "errors",
        "next_commands",
    ):
        assert key in payload
    assert payload["ok"] is True
    assert payload["mode"] == "dry_run"
    assert payload["decision"] == "dry_run_no_tool_execution"
    assert payload["adapter_id"] == "dry_run"
    assert payload["errors"] == []
    assert Path(payload["manifest"]).exists()
    assert payload["summary"]["run_id"] == payload["run_id"]
    assert payload["summary"]["task_id"] == payload["task_id"]
