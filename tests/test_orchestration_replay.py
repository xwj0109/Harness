import json
from pathlib import Path

from typer.testing import CliRunner

from harness.cli.main import app
from harness.memory.sqlite_store import SQLiteStore
from harness.objective_runner import run_objective_autonomously
from harness.orchestration_replay import run_orchestration_replay_audit


runner = CliRunner()


def _cases(payload: dict) -> dict[str, dict]:
    return {case["id"]: case for case in payload["cases"]}


def test_orchestration_replay_uninitialized_project_runs_synthetic_only(tmp_path: Path) -> None:
    audit = run_orchestration_replay_audit(tmp_path)
    payload = audit.model_dump(mode="json")
    cases = _cases(payload)

    assert payload["schema_version"] == "harness.orchestration_replay_audit/v1"
    assert payload["ok"] is True
    assert payload["initialized"] is False
    assert payload["summary"]["fail"] == 0
    assert payload["summary"]["synthetic"] == 5
    assert payload["summary"]["captured"] == 1
    assert cases["captured_objective_evidence_replay"]["status"] == "skipped"
    assert cases["synthetic_happy_checkpoint_stop"]["status"] == "pass"
    assert cases["synthetic_duplicate_dispatch_detection"]["detected_issue_codes"] == [
        "duplicate_side_effect_dispatch"
    ]
    assert cases["synthetic_slow_branch_barrier_detection"]["detected_issue_codes"] == [
        "batch_completed_missing_terminal_task"
    ]
    assert cases["synthetic_approval_reject_detection"]["detected_issue_codes"] == [
        "dispatch_after_blocking_event"
    ]
    assert cases["synthetic_missing_terminal_detection"]["detected_issue_codes"] == ["missing_stopped_event"]
    assert payload["safety"]["read_only"] is True
    assert payload["safety"]["provider_called"] is False
    assert payload["safety"]["network_called"] is False
    assert payload["safety"]["adapter_execution_started"] is False
    assert payload["safety"]["filesystem_modified"] is False
    assert payload["safety"]["permission_granting"] is False
    assert payload["safety"]["artifact_bodies_read"] is False
    assert not (tmp_path / ".harness").exists()


def test_orchestration_replay_cli_and_eval_are_passive(tmp_path: Path) -> None:
    cli_result = runner.invoke(app, ["orchestration", "replay", "--project", str(tmp_path), "--output", "json"])
    eval_result = runner.invoke(
        app,
        ["evals", "run", "--suite", "orchestration-replay", "--project", str(tmp_path), "--output", "json"],
    )

    assert cli_result.exit_code == 0, cli_result.output
    assert eval_result.exit_code == 0, eval_result.output
    cli_payload = json.loads(cli_result.output)
    eval_payload = json.loads(eval_result.output)
    assert cli_payload["schema_version"] == "harness.orchestration_replay_audit/v1"
    assert eval_payload["schema_version"] == "harness.orchestration_replay_audit/v1"
    assert cli_payload["summary"]["fail"] == 0
    assert eval_payload["summary"]["fail"] == 0
    assert cli_payload["safety"]["adapter_execution_started"] is False
    assert eval_payload["safety"]["artifact_bodies_read"] is False
    assert not (tmp_path / ".harness").exists()


def test_orchestration_replay_checks_captured_objective_evidence(tmp_path: Path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    objective = store.create_objective("Replay captured objective")
    store.create_task(
        "Dry task",
        objective_id=objective.id,
        metadata={"execution_adapter": "dry_run", "task_type": "phase_1a_test"},
    )
    result = run_objective_autonomously(tmp_path, objective.id, autonomy_profile_id="safe-local")
    assert result.evidence_path is not None
    assert result.evidence_path.exists()

    audit = run_orchestration_replay_audit(tmp_path)
    payload = audit.model_dump(mode="json")
    captured = _cases(payload)["captured_objective_evidence_replay"]

    assert payload["ok"] is True
    assert captured["status"] == "pass"
    assert captured["event_count"] > 0
    assert captured["replay_summary"]["objective_count"] == 1
    assert captured["replay_summary"]["failed_objective_count"] == 0
    assert captured["evidence"]["checked_objectives"][0]["objective_id"] == objective.id
    assert captured["evidence"]["checked_objectives"][0]["verification_ok"] is True
    assert captured["evidence"]["checked_objectives"][0]["replay_issue_codes"] == []
    assert captured["evidence"]["artifact_bodies_read"] is False
