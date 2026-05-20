from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from harness.cli.main import app
from harness.core_projection import (
    build_core_evidence_bundle,
    build_core_run_events_projection,
    build_core_blocked_state_projection,
    build_core_run_projection,
    build_core_task_projection,
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
    assert any(command.startswith("harness core inspect-evidence --run ") for command in projection.next_commands)
    assert any(command.startswith("harness core inspect-evidence --task ") for command in projection.next_commands)
    assert any(command.startswith("harness core inspect-run ") for command in projection.next_commands)
    assert any(command.startswith("harness core inspect-events ") for command in projection.next_commands)
    assert projection.task is not None
    assert projection.task.task_id == result.task_id

    events = list_core_run_events(tmp_path, result.run_id)
    assert events
    assert events[0].schema_version == "harness.core_event_projection/v1"
    assert events[0].run_id == result.run_id
    assert events[0].kind == events[0].event_type

    events_projection = build_core_run_events_projection(tmp_path, result.run_id)
    assert events_projection.schema_version == "harness.core_run_events_projection/v1"
    assert events_projection.ok is True
    assert events_projection.run_id == result.run_id
    assert events_projection.event_count == len(events)
    assert events_projection.events == events


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
    assert any(command.startswith("harness core inspect-evidence --task ") for command in projection.next_commands)
    assert projection.next_commands


def test_core_inspect_task_cli_inspects_blocked_repo_planning(tmp_path) -> None:
    result = HarnessCoreService().start_goal("plan a small change", mode="repo_planning", project_root=tmp_path)

    inspect = runner.invoke(
        app,
        ["core", "inspect-task", result.task_id, "--project", str(tmp_path), "--output", "json"],
    )

    assert inspect.exit_code == 1
    payload = json.loads(inspect.output)
    assert payload["schema_version"] == "harness.core_blocked_state_projection/v1"
    assert payload["ok"] is False
    assert payload["run_id"] is None
    assert payload["task_id"] == result.task_id
    assert payload["lease_id"] == result.lease_id
    assert payload["adapter_id"] == "repo_planning"
    assert payload["task_type"] == "repo_planning"
    assert payload["decision"] == "execution_adapter_rejected"
    assert payload["policy_sha256"]
    assert any("hosted_provider_codex" in reason for reason in payload["blocked_reasons"])
    assert any(command.startswith("harness core inspect-task ") for command in payload["next_commands"])
    assert any(command.startswith("harness daemon inspect-lease ") for command in payload["next_commands"])


def test_core_inspect_task_cli_inspects_blocked_codex_isolated_edit(tmp_path) -> None:
    result = HarnessCoreService().start_goal("make a small change", mode="codex_isolated_edit", project_root=tmp_path)

    inspect = runner.invoke(
        app,
        ["core", "inspect-task", result.task_id, "--project", str(tmp_path), "--output", "json"],
    )

    assert inspect.exit_code == 1
    payload = json.loads(inspect.output)
    assert payload["schema_version"] == "harness.core_blocked_state_projection/v1"
    assert payload["ok"] is False
    assert payload["run_id"] is None
    assert payload["task_id"] == result.task_id
    assert payload["lease_id"] == result.lease_id
    assert payload["adapter_id"] == "codex_isolated_edit"
    assert payload["task_type"] == "codex_code_edit"
    assert payload["decision"] == "execution_adapter_rejected"
    assert any("hosted_provider_codex" in reason for reason in payload["blocked_reasons"])


def test_core_inspect_task_cli_returns_completed_task_projection_for_dry_run(tmp_path) -> None:
    result = HarnessCoreService().start_goal("smoke test core loop", mode="dry_run", project_root=tmp_path)

    inspect = runner.invoke(
        app,
        ["core", "inspect-task", result.task_id, "--project", str(tmp_path), "--output", "json"],
    )

    assert inspect.exit_code == 0, inspect.output
    payload = json.loads(inspect.output)
    assert payload["schema_version"] == "harness.core_task_projection/v1"
    assert payload["ok"] is True
    assert payload["task_id"] == result.task_id
    assert payload["run_id"] == result.run_id
    assert payload["lease_id"] == result.lease_id
    assert payload["adapter_id"] == "dry_run"
    assert payload["task_type"] == "phase_1a_test"
    assert payload["status"] == "succeeded"
    assert payload["decision"] == "dry_run_no_tool_execution"
    assert payload["manifest"] == str(result.manifest)
    assert payload["artifact_ids"]
    assert payload["blocked_reasons"] == []
    assert payload["errors"] == []
    assert any(command.startswith("harness core inspect-evidence --task ") for command in payload["next_commands"])


def test_core_inspect_task_cli_missing_task_fails_closed_with_structured_json(tmp_path) -> None:
    SQLiteStore(tmp_path).initialize()

    inspect = runner.invoke(
        app,
        ["core", "inspect-task", "task_missing", "--project", str(tmp_path), "--output", "json"],
    )

    assert inspect.exit_code == 1
    payload = json.loads(inspect.output)
    assert payload == {
        "schema_version": "harness.core_projection_error/v1",
        "ok": False,
        "task_id": "task_missing",
        "project_root": str(tmp_path.resolve()),
        "error": "Task not found: task_missing",
    }


def test_core_inspect_task_cli_unblocked_no_run_task_fails_closed(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    task = store.create_task("ready task", metadata={"execution_adapter": "dry_run", "task_type": "phase_1a_test"})

    inspect = runner.invoke(
        app,
        ["core", "inspect-task", task.id, "--project", str(tmp_path), "--output", "json"],
    )

    assert inspect.exit_code == 1
    payload = json.loads(inspect.output)
    assert payload == {
        "schema_version": "harness.core_projection_error/v1",
        "ok": False,
        "task_id": task.id,
        "project_root": str(tmp_path.resolve()),
        "error": f"Task has no blocked state and no run evidence: {task.id}",
    }


def test_core_inspect_task_cli_does_not_initialize_project_state(tmp_path) -> None:
    inspect = runner.invoke(
        app,
        ["core", "inspect-task", "task_missing", "--project", str(tmp_path), "--output", "json"],
    )

    assert inspect.exit_code == 1
    payload = json.loads(inspect.output)
    assert payload["schema_version"] == "harness.core_projection_error/v1"
    assert payload["ok"] is False
    assert payload["task_id"] == "task_missing"
    assert "Project state not initialized" in payload["error"]
    assert not (tmp_path / ".harness").exists()


def test_core_inspect_task_cli_does_not_read_artifact_bodies(tmp_path, monkeypatch) -> None:
    result = HarnessCoreService().start_goal("smoke test core loop", mode="dry_run", project_root=tmp_path)
    body_path = tmp_path / ".harness" / "runs" / result.run_id / "task_body_should_not_be_read.txt"
    body_path.write_text("TASK_BODY_SECRET_SHOULD_NOT_APPEAR", encoding="utf-8")
    SQLiteStore(tmp_path).register_artifact(
        result.run_id,
        kind="task_body_probe",
        path=body_path,
        metadata={"purpose": "prove task projection does not read artifact bodies"},
        producer="test",
        redaction_state="redacted",
    )
    original_read_text = Path.read_text

    def guarded_read_text(self, *args, **kwargs):
        if self == body_path:
            raise AssertionError("task projection must not read artifact bodies")
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read_text)

    inspect = runner.invoke(
        app,
        ["core", "inspect-task", result.task_id, "--project", str(tmp_path), "--output", "json"],
    )

    assert inspect.exit_code == 0, inspect.output
    payload = json.loads(inspect.output)
    serialized = json.dumps(payload, sort_keys=True)
    assert "task_body_probe" not in serialized
    assert "TASK_BODY_SECRET_SHOULD_NOT_APPEAR" not in serialized
    assert payload["artifact_ids"]


def test_core_inspect_task_cli_json_shape_is_deterministic(tmp_path) -> None:
    result = HarnessCoreService().start_goal("plan a small change", mode="repo_planning", project_root=tmp_path)

    inspect = runner.invoke(
        app,
        ["core", "inspect-task", result.task_id, "--project", str(tmp_path), "--output", "json"],
    )

    assert inspect.exit_code == 1
    payload = json.loads(inspect.output)
    assert list(payload) == sorted(payload)
    for key in (
        "schema_version",
        "ok",
        "task_id",
        "objective_id",
        "task_type",
        "adapter_id",
        "status",
        "decision",
        "lease_id",
        "run_id",
        "approval_id",
        "policy_sha256",
        "blocked_reasons",
        "errors",
        "next_commands",
    ):
        assert key in payload


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


def test_core_inspect_events_cli_returns_deterministic_json(tmp_path) -> None:
    result = HarnessCoreService().start_goal("smoke test core loop", mode="dry_run", project_root=tmp_path)

    inspect = runner.invoke(
        app,
        ["core", "inspect-events", result.run_id, "--project", str(tmp_path), "--output", "json"],
    )

    assert inspect.exit_code == 0, inspect.output
    payload = json.loads(inspect.output)
    assert payload["schema_version"] == "harness.core_run_events_projection/v1"
    assert list(payload) == sorted(payload)
    assert payload["ok"] is True
    assert payload["run_id"] == result.run_id
    assert payload["project_root"] == str(tmp_path.resolve())
    assert payload["event_count"] == len(payload["events"])
    assert payload["event_count"] >= 1
    assert payload["errors"] == []
    assert any(command.startswith("harness core inspect-evidence --run ") for command in payload["next_commands"])
    assert any(command.startswith("harness core inspect-run ") for command in payload["next_commands"])
    event = payload["events"][0]
    for key in (
        "schema_version",
        "event_id",
        "run_id",
        "task_id",
        "kind",
        "event_type",
        "message",
        "created_at",
        "redaction_state",
        "metadata",
    ):
        assert key in event
    assert event["schema_version"] == "harness.core_event_projection/v1"
    assert event["run_id"] == result.run_id
    assert event["kind"] == event["event_type"]


def test_core_inspect_events_cli_missing_run_fails_closed_with_structured_json(tmp_path) -> None:
    SQLiteStore(tmp_path).initialize()

    inspect = runner.invoke(
        app,
        ["core", "inspect-events", "run_missing", "--project", str(tmp_path), "--output", "json"],
    )

    assert inspect.exit_code == 1
    payload = json.loads(inspect.output)
    assert payload == {
        "schema_version": "harness.core_projection_error/v1",
        "ok": False,
        "run_id": "run_missing",
        "project_root": str(tmp_path.resolve()),
        "errors": ["Run not found: run_missing"],
    }


def test_core_inspect_events_cli_missing_project_state_does_not_initialize(tmp_path) -> None:
    inspect = runner.invoke(
        app,
        ["core", "inspect-events", "run_missing", "--project", str(tmp_path), "--output", "json"],
    )

    assert inspect.exit_code == 1
    payload = json.loads(inspect.output)
    assert payload["schema_version"] == "harness.core_projection_error/v1"
    assert payload["ok"] is False
    assert payload["run_id"] == "run_missing"
    assert "Project state not initialized" in payload["errors"][0]
    assert not (tmp_path / ".harness").exists()


def test_core_inspect_events_cli_does_not_read_artifact_bodies(tmp_path, monkeypatch) -> None:
    result = HarnessCoreService().start_goal("smoke test core loop", mode="dry_run", project_root=tmp_path)
    body_path = tmp_path / ".harness" / "runs" / result.run_id / "event_body_should_not_be_read.txt"
    body_path.write_text("EVENT_BODY_SECRET_SHOULD_NOT_APPEAR", encoding="utf-8")
    SQLiteStore(tmp_path).register_artifact(
        result.run_id,
        kind="event_body_probe",
        path=body_path,
        metadata={"purpose": "prove event projection does not read artifact bodies"},
        producer="test",
        redaction_state="redacted",
    )
    original_read_text = Path.read_text

    def guarded_read_text(self, *args, **kwargs):
        if self == body_path:
            raise AssertionError("event projection must not read artifact bodies")
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read_text)

    inspect = runner.invoke(
        app,
        ["core", "inspect-events", result.run_id, "--project", str(tmp_path), "--output", "json"],
    )

    assert inspect.exit_code == 0, inspect.output
    serialized = json.dumps(json.loads(inspect.output), sort_keys=True)
    assert "event_body_probe" not in serialized
    assert "EVENT_BODY_SECRET_SHOULD_NOT_APPEAR" not in serialized


def test_core_inspect_events_cli_redacts_secret_like_metadata(tmp_path) -> None:
    result = HarnessCoreService().start_goal("smoke test core loop", mode="dry_run", project_root=tmp_path)
    secret = "sk-abcdefghijklmnopqrstuvwxyz"
    SQLiteStore(tmp_path).append_event(
        result.run_id,
        "info",
        "secret_event_probe",
        f"token {secret}",
        {"token": secret},
    )

    inspect = runner.invoke(
        app,
        ["core", "inspect-events", result.run_id, "--project", str(tmp_path), "--output", "json"],
    )

    assert inspect.exit_code == 0, inspect.output
    serialized = json.dumps(json.loads(inspect.output), sort_keys=True)
    assert secret not in serialized
    assert "[REDACTED_SECRET]" in serialized


def test_core_evidence_bundle_dry_run_by_run_id(tmp_path) -> None:
    result = HarnessCoreService().start_goal("smoke test core loop", mode="dry_run", project_root=tmp_path)

    bundle = build_core_evidence_bundle(tmp_path, run_id=result.run_id)

    assert bundle.schema_version == "harness.core_evidence_bundle_projection/v1"
    assert bundle.ok is True
    assert bundle.project_root == tmp_path.resolve()
    assert bundle.run_id == result.run_id
    assert bundle.task_id == result.task_id
    assert bundle.mode == "dry_run"
    assert bundle.decision == "dry_run_no_tool_execution"
    assert bundle.status == "completed"
    assert bundle.run is not None
    assert bundle.run.run_id == result.run_id
    assert bundle.task is not None
    assert bundle.task.task_id == result.task_id
    assert bundle.events is not None
    assert bundle.events.event_count >= 1
    assert bundle.artifacts
    assert bundle.manifest == str(result.manifest)
    assert bundle.errors == []
    assert any(command.startswith("harness core inspect-evidence --run ") for command in bundle.next_commands)


def test_core_evidence_bundle_dry_run_by_task_id(tmp_path) -> None:
    result = HarnessCoreService().start_goal("smoke test core loop", mode="dry_run", project_root=tmp_path)

    inspect = runner.invoke(
        app,
        ["core", "inspect-evidence", "--task", result.task_id, "--project", str(tmp_path), "--output", "json"],
    )

    assert inspect.exit_code == 0, inspect.output
    payload = json.loads(inspect.output)
    assert payload["schema_version"] == "harness.core_evidence_bundle_projection/v1"
    assert payload["ok"] is True
    assert payload["run_id"] == result.run_id
    assert payload["task_id"] == result.task_id
    assert payload["mode"] == "dry_run"
    assert payload["decision"] == "dry_run_no_tool_execution"
    assert payload["run"]["run_id"] == result.run_id
    assert payload["task"]["task_id"] == result.task_id
    assert payload["events"]["event_count"] >= 1
    assert payload["artifacts"]
    assert payload["manifest"] == str(result.manifest)


def test_core_evidence_bundle_blocked_repo_planning_by_task_id(tmp_path) -> None:
    result = HarnessCoreService().start_goal("plan a small change", mode="repo_planning", project_root=tmp_path)

    inspect = runner.invoke(
        app,
        ["core", "inspect-evidence", "--task", result.task_id, "--project", str(tmp_path), "--output", "json"],
    )

    assert inspect.exit_code == 1
    payload = json.loads(inspect.output)
    assert payload["schema_version"] == "harness.core_evidence_bundle_projection/v1"
    assert payload["ok"] is False
    assert payload["run_id"] is None
    assert payload["task_id"] == result.task_id
    assert payload["mode"] == "repo_planning"
    assert payload["decision"] == "execution_adapter_rejected"
    assert payload["run"] is None
    assert payload["task"]["task_id"] == result.task_id
    assert payload["blocked_state"]["task_id"] == result.task_id
    assert payload["events"] is None
    assert payload["artifacts"] == []
    assert payload["manifest"] is None
    assert any("hosted_provider_codex" in reason for reason in payload["blocked_state"]["blocked_reasons"])


def test_core_evidence_bundle_blocked_codex_isolated_edit_by_task_id(tmp_path) -> None:
    result = HarnessCoreService().start_goal("make a small change", mode="codex_isolated_edit", project_root=tmp_path)

    inspect = runner.invoke(
        app,
        ["core", "inspect-evidence", "--task", result.task_id, "--project", str(tmp_path), "--output", "json"],
    )

    assert inspect.exit_code == 1
    payload = json.loads(inspect.output)
    assert payload["schema_version"] == "harness.core_evidence_bundle_projection/v1"
    assert payload["ok"] is False
    assert payload["run_id"] is None
    assert payload["task_id"] == result.task_id
    assert payload["mode"] == "codex_isolated_edit"
    assert payload["decision"] == "execution_adapter_rejected"
    assert payload["blocked_state"]["adapter_id"] == "codex_isolated_edit"
    assert any("hosted_provider_codex" in reason for reason in payload["blocked_state"]["blocked_reasons"])


def test_core_inspect_evidence_missing_run_fails_closed_with_structured_json(tmp_path) -> None:
    SQLiteStore(tmp_path).initialize()

    inspect = runner.invoke(
        app,
        ["core", "inspect-evidence", "--run", "run_missing", "--project", str(tmp_path), "--output", "json"],
    )

    assert inspect.exit_code == 1
    payload = json.loads(inspect.output)
    assert payload == {
        "schema_version": "harness.core_projection_error/v1",
        "ok": False,
        "project_root": str(tmp_path.resolve()),
        "run_id": "run_missing",
        "task_id": None,
        "error": "Run not found: run_missing",
        "errors": ["Run not found: run_missing"],
    }


def test_core_inspect_evidence_missing_task_fails_closed_with_structured_json(tmp_path) -> None:
    SQLiteStore(tmp_path).initialize()

    inspect = runner.invoke(
        app,
        ["core", "inspect-evidence", "--task", "task_missing", "--project", str(tmp_path), "--output", "json"],
    )

    assert inspect.exit_code == 1
    payload = json.loads(inspect.output)
    assert payload == {
        "schema_version": "harness.core_projection_error/v1",
        "ok": False,
        "project_root": str(tmp_path.resolve()),
        "run_id": None,
        "task_id": "task_missing",
        "error": "Task not found: task_missing",
        "errors": ["Task not found: task_missing"],
    }


def test_core_inspect_evidence_rejects_both_run_and_task(tmp_path) -> None:
    SQLiteStore(tmp_path).initialize()

    inspect = runner.invoke(
        app,
        [
            "core",
            "inspect-evidence",
            "--run",
            "run_one",
            "--task",
            "task_one",
            "--project",
            str(tmp_path),
            "--output",
            "json",
        ],
    )

    assert inspect.exit_code == 1
    payload = json.loads(inspect.output)
    assert payload["schema_version"] == "harness.core_projection_error/v1"
    assert payload["ok"] is False
    assert payload["run_id"] == "run_one"
    assert payload["task_id"] == "task_one"
    assert payload["error"] == "Exactly one of run_id or task_id is required."


def test_core_inspect_evidence_rejects_neither_run_nor_task(tmp_path) -> None:
    SQLiteStore(tmp_path).initialize()

    inspect = runner.invoke(
        app,
        ["core", "inspect-evidence", "--project", str(tmp_path), "--output", "json"],
    )

    assert inspect.exit_code == 1
    payload = json.loads(inspect.output)
    assert payload["schema_version"] == "harness.core_projection_error/v1"
    assert payload["ok"] is False
    assert payload["run_id"] is None
    assert payload["task_id"] is None
    assert payload["error"] == "Exactly one of run_id or task_id is required."


def test_core_inspect_evidence_missing_project_state_does_not_initialize(tmp_path) -> None:
    inspect = runner.invoke(
        app,
        ["core", "inspect-evidence", "--run", "run_missing", "--project", str(tmp_path), "--output", "json"],
    )

    assert inspect.exit_code == 1
    payload = json.loads(inspect.output)
    assert payload["schema_version"] == "harness.core_projection_error/v1"
    assert payload["ok"] is False
    assert "Project state not initialized" in payload["error"]
    assert not (tmp_path / ".harness").exists()


def test_core_inspect_evidence_does_not_read_artifact_bodies(tmp_path, monkeypatch) -> None:
    result = HarnessCoreService().start_goal("smoke test core loop", mode="dry_run", project_root=tmp_path)
    body_path = tmp_path / ".harness" / "runs" / result.run_id / "bundle_body_should_not_be_read.txt"
    body_path.write_text("BUNDLE_BODY_SECRET_SHOULD_NOT_APPEAR", encoding="utf-8")
    SQLiteStore(tmp_path).register_artifact(
        result.run_id,
        kind="bundle_body_probe",
        path=body_path,
        metadata={"purpose": "prove bundle projection does not read artifact bodies"},
        producer="test",
        redaction_state="redacted",
    )
    original_read_text = Path.read_text

    def guarded_read_text(self, *args, **kwargs):
        if self == body_path:
            raise AssertionError("bundle projection must not read artifact bodies")
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read_text)

    inspect = runner.invoke(
        app,
        ["core", "inspect-evidence", "--run", result.run_id, "--project", str(tmp_path), "--output", "json"],
    )

    assert inspect.exit_code == 0, inspect.output
    serialized = json.dumps(json.loads(inspect.output), sort_keys=True)
    assert "bundle_body_probe" in serialized
    assert "BUNDLE_BODY_SECRET_SHOULD_NOT_APPEAR" not in serialized


def test_core_inspect_evidence_redacts_secret_like_metadata(tmp_path) -> None:
    result = HarnessCoreService().start_goal("smoke test core loop", mode="dry_run", project_root=tmp_path)
    secret = "sk-abcdefghijklmnopqrstuvwxyz"
    metadata_path = tmp_path / ".harness" / "runs" / result.run_id / "bundle_metadata_probe.txt"
    metadata_path.write_text("metadata body", encoding="utf-8")
    store = SQLiteStore(tmp_path)
    store.register_artifact(
        result.run_id,
        kind="bundle_metadata_probe",
        path=metadata_path,
        metadata={"token": secret},
        producer="test",
        redaction_state="redacted",
    )
    store.append_event(result.run_id, "info", "bundle_secret_probe", f"token {secret}", {"token": secret})

    inspect = runner.invoke(
        app,
        ["core", "inspect-evidence", "--run", result.run_id, "--project", str(tmp_path), "--output", "json"],
    )

    assert inspect.exit_code == 0, inspect.output
    serialized = json.dumps(json.loads(inspect.output), sort_keys=True)
    assert secret not in serialized
    assert "[REDACTED_SECRET]" in serialized


def test_core_inspect_evidence_cli_json_shape_is_deterministic(tmp_path) -> None:
    result = HarnessCoreService().start_goal("smoke test core loop", mode="dry_run", project_root=tmp_path)

    inspect = runner.invoke(
        app,
        ["core", "inspect-evidence", "--run", result.run_id, "--project", str(tmp_path), "--output", "json"],
    )

    assert inspect.exit_code == 0, inspect.output
    payload = json.loads(inspect.output)
    assert payload["schema_version"] == "harness.core_evidence_bundle_projection/v1"
    assert list(payload) == sorted(payload)
    for key in (
        "schema_version",
        "ok",
        "project_root",
        "run_id",
        "task_id",
        "mode",
        "decision",
        "status",
        "run",
        "task",
        "blocked_state",
        "events",
        "artifacts",
        "manifest",
        "errors",
        "next_commands",
    ):
        assert key in payload


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


def test_core_task_projection_helper_returns_completed_dry_run_task(tmp_path) -> None:
    result = HarnessCoreService().start_goal("smoke test core loop", mode="dry_run", project_root=tmp_path)

    projection = build_core_task_projection(tmp_path, result.task_id)

    assert projection.schema_version == "harness.core_task_projection/v1"
    assert projection.ok is True
    assert projection.task_id == result.task_id
    assert projection.run_id == result.run_id
    assert projection.lease_id == result.lease_id
    assert projection.decision == "dry_run_no_tool_execution"
