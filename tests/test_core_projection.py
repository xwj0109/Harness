from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from harness.cli.main import app
from harness.core_projection import (
    build_core_blocked_state_projection,
    build_core_run_projection,
    list_core_run_events,
)
from harness.core_service import HarnessCoreService
from harness.memory.sqlite_store import SQLiteStore


runner = CliRunner()


def test_core_projection_inspects_dry_run_execution(tmp_path) -> None:
    result = HarnessCoreService().start_goal("smoke test core loop", mode="dry_run", project_root=tmp_path)

    projection = build_core_run_projection(tmp_path, result.run_id)

    assert projection.schema_version == "harness.core_run_projection/v1"
    assert projection.ok is True
    assert projection.run_id == result.run_id
    assert projection.task_id == result.task_id
    assert projection.objective_id == result.objective_id
    assert projection.lease_id == result.lease_id
    assert projection.adapter_id == "dry_run"
    assert projection.task_type == "phase_1a_test"
    assert projection.status == "completed"
    assert projection.decision == "dry_run_no_tool_execution"
    assert projection.manifest == str(result.manifest)
    assert set(projection.artifact_ids)
    assert projection.approval_id is None
    assert projection.policy_sha256
    assert projection.errors == []
    assert projection.blocked_reasons == []
    assert any(command.startswith("harness core inspect-run ") for command in projection.next_commands)
    assert projection.task is not None
    assert projection.task.task_id == result.task_id

    events = list_core_run_events(tmp_path, result.run_id)
    assert events
    assert events[0].schema_version == "harness.core_event_projection/v1"
    assert events[0].run_id == result.run_id


def test_core_blocked_repo_planning_projection_has_reasons_and_no_run_id(tmp_path) -> None:
    result = HarnessCoreService().start_goal("plan a small change", mode="repo_planning", project_root=tmp_path)

    assert result.run_id is None
    projection = build_core_blocked_state_projection(tmp_path, result.task_id)

    assert projection.schema_version == "harness.core_blocked_state_projection/v1"
    assert projection.ok is False
    assert projection.run_id is None
    assert projection.task_id == result.task_id
    assert projection.lease_id == result.lease_id
    assert projection.adapter_id == "repo_planning"
    assert projection.task_type == "repo_planning"
    assert projection.decision == "execution_adapter_rejected"
    assert projection.policy_sha256
    assert any("hosted_provider_codex" in reason for reason in projection.blocked_reasons)
    assert projection.next_commands


def test_core_projection_does_not_read_artifact_bodies(tmp_path, monkeypatch) -> None:
    result = HarnessCoreService().start_goal("smoke test core loop", mode="dry_run", project_root=tmp_path)
    body_path = tmp_path / ".harness" / "runs" / result.run_id / "body_should_not_be_read.txt"
    body_path.write_text("SUPER_SECRET_BODY_SHOULD_NOT_APPEAR", encoding="utf-8")
    SQLiteStore(tmp_path).register_artifact(
        result.run_id,
        kind="body_probe",
        path=body_path,
        metadata={"purpose": "prove projection does not read artifact bodies"},
        producer="test",
        redaction_state="redacted",
    )
    original_read_text = Path.read_text

    def guarded_read_text(self, *args, **kwargs):
        if self == body_path:
            raise AssertionError("projection must not read artifact bodies")
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read_text)

    projection = build_core_run_projection(tmp_path, result.run_id)
    serialized = json.dumps(projection.model_dump(mode="json"), sort_keys=True)

    assert "body_probe" in serialized
    assert "SUPER_SECRET_BODY_SHOULD_NOT_APPEAR" not in serialized


def test_core_projection_sanitizes_event_and_artifact_metadata_secrets(tmp_path) -> None:
    result = HarnessCoreService().start_goal("smoke test core loop", mode="dry_run", project_root=tmp_path)
    store = SQLiteStore(tmp_path)
    secret = "sk-abcdefghijklmnopqrstuvwxyz"
    metadata_path = tmp_path / ".harness" / "runs" / result.run_id / "metadata_probe.txt"
    metadata_path.write_text("metadata body", encoding="utf-8")
    store.register_artifact(
        result.run_id,
        kind="metadata_probe",
        path=metadata_path,
        metadata={"token": secret},
        producer="test",
        redaction_state="redacted",
    )
    store.append_event(
        result.run_id,
        "info",
        "secret_probe",
        f"token {secret}",
        {"token": secret},
    )

    projection_json = json.dumps(build_core_run_projection(tmp_path, result.run_id).model_dump(mode="json"), sort_keys=True)
    events_json = json.dumps([event.model_dump(mode="json") for event in list_core_run_events(tmp_path, result.run_id)], sort_keys=True)

    assert secret not in projection_json
    assert secret not in events_json
    assert "[REDACTED_SECRET]" in projection_json
    assert "[REDACTED_SECRET]" in events_json


def test_core_projection_missing_run_cli_fails_closed_with_structured_error(tmp_path) -> None:
    SQLiteStore(tmp_path).initialize()
    result = runner.invoke(
        app,
        ["core", "inspect-run", "run_missing", "--project", str(tmp_path), "--output", "json"],
    )

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload == {
        "schema_version": "harness.core_projection_error/v1",
        "ok": False,
        "run_id": "run_missing",
        "project_root": str(tmp_path.resolve()),
        "error": "Run not found: run_missing",
    }


def test_core_inspect_run_cli_json_shape_is_deterministic(tmp_path) -> None:
    run_result = runner.invoke(
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
    assert run_result.exit_code == 0, run_result.output
    run_payload = json.loads(run_result.output)

    inspect_result = runner.invoke(
        app,
        [
            "core",
            "inspect-run",
            run_payload["run_id"],
            "--project",
            str(tmp_path),
            "--output",
            "json",
        ],
    )

    assert inspect_result.exit_code == 0, inspect_result.output
    payload = json.loads(inspect_result.output)
    assert payload["schema_version"] == "harness.core_run_projection/v1"
    assert list(payload) == sorted(payload)
    for key in (
        "schema_version",
        "ok",
        "run_id",
        "task_id",
        "objective_id",
        "lease_id",
        "adapter_id",
        "task_type",
        "status",
        "decision",
        "manifest",
        "artifact_ids",
        "approval_id",
        "policy_sha256",
        "errors",
        "blocked_reasons",
        "next_commands",
    ):
        assert key in payload
    assert payload["run_id"] == run_payload["run_id"]
    assert payload["task_id"] == run_payload["task_id"]
    assert payload["lease_id"] == run_payload["lease_id"]
    assert payload["adapter_id"] == "dry_run"
    assert payload["decision"] == "dry_run_no_tool_execution"
