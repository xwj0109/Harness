from __future__ import annotations

import json

from typer.testing import CliRunner

from harness.cli.main import app
from harness.memory.sqlite_store import SQLiteStore
from harness.objective_runner import run_objective_autonomously


runner = CliRunner()


def _init_project(tmp_path):
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    objective = store.create_objective("Autonomous objective")
    return store, objective


def _dry_task(
    store: SQLiteStore,
    title: str,
    objective_id: str,
    *,
    depends_on: list[str] | None = None,
    metadata: dict | None = None,
):
    return store.create_task(
        title=title,
        objective_id=objective_id,
        depends_on=depends_on,
        metadata={"execution_adapter": "dry_run", "task_type": "phase_1a_test", **dict(metadata or {})},
    )


def test_objective_runner_runs_ready_task(tmp_path) -> None:
    store, objective = _init_project(tmp_path)
    task = _dry_task(store, "Dry run", objective.id)

    result = run_objective_autonomously(tmp_path, objective.id, autonomy_profile_id="safe-local")

    assert result.ok is True
    assert result.stop_reason == "objective_succeeded"
    assert result.adapter_dispatches == 1
    assert result.step_results[0].task_id == task.id
    assert SQLiteStore(tmp_path).get_task(task.id).status.value == "succeeded"
    assert result.evidence_path.exists()
    events = [json.loads(line) for line in result.evidence_path.read_text(encoding="utf-8").splitlines()]
    assert [event["event"] for event in events] == ["started", "recovery_checked", "adapter_dispatched", "stopped"]
    dispatched = events[2]
    assert dispatched["autonomy_decision_id"].startswith("adec_")
    assert dispatched["autonomous_approval_id"].startswith("auto_")
    assert dispatched["autonomous_outcome_id"].startswith("aout_")
    assert dispatched["lease_id"] == result.step_results[0].lease_id
    assert dispatched["run_id"] == result.step_results[0].run_id
    assert dispatched["artifact_ids"]


def test_objective_runner_respects_dependencies(tmp_path) -> None:
    store, objective = _init_project(tmp_path)
    first = _dry_task(store, "First", objective.id)
    second = _dry_task(store, "Second", objective.id, depends_on=[first.id])

    result = run_objective_autonomously(tmp_path, objective.id, autonomy_profile_id="safe-local")

    assert result.ok is True
    assert result.stop_reason == "objective_succeeded"
    assert [step.task_id for step in result.step_results] == [first.id, second.id]
    fresh = SQLiteStore(tmp_path)
    assert fresh.get_task(first.id).status.value == "succeeded"
    assert fresh.get_task(second.id).status.value == "succeeded"


def test_objective_runner_requires_lease_and_links_run_evidence(tmp_path) -> None:
    store, objective = _init_project(tmp_path)
    task = _dry_task(store, "Dry run", objective.id)

    result = run_objective_autonomously(tmp_path, objective.id, autonomy_profile_id="safe-local")

    step = result.step_results[0]
    assert step.task_id == task.id
    assert step.lease_id is not None
    assert step.run_id is not None
    fresh = SQLiteStore(tmp_path)
    lease = fresh.get_task_lease(step.lease_id)
    manifest = fresh.build_run_manifest(step.run_id)
    assert lease.task_id == task.id
    assert lease.status.value == "released"
    assert manifest.task_id == task.id
    assert manifest.objective_id == objective.id
    assert manifest.autonomous_approval_id is not None
    assert manifest.autonomous_outcome_id is not None
    assert manifest.autonomy_decision_id is not None


def test_final_report_links_required_evidence(tmp_path) -> None:
    store, objective = _init_project(tmp_path)
    task = _dry_task(
        store,
        "Final coding workflow report",
        objective.id,
        metadata={
            "workflow_stage": "final_report",
            "review_role": "coding_orchestrator",
            "requires_evidence_links": "objective,task,run,artifact,policy",
        },
    )

    result = run_objective_autonomously(tmp_path, objective.id, autonomy_profile_id="safe-local")

    assert result.stop_reason == "objective_succeeded"
    step = result.step_results[0]
    fresh = SQLiteStore(tmp_path)
    artifacts = fresh.list_artifacts(step.run_id)
    report = next(artifact for artifact in artifacts if artifact.kind == "final_report")
    text = report.path.read_text(encoding="utf-8")
    assert f"- Objective id: {objective.id}" in text
    assert f"- Task id: {task.id}" in text
    assert f"- Run id: {step.run_id}" in text
    assert "- Policy sha256:" in text
    assert "- Artifact evidence: events, transcript, final_report, manifest" in text
    events = [json.loads(line) for line in result.evidence_path.read_text(encoding="utf-8").splitlines()]
    dispatched = next(event for event in events if event["event"] == "adapter_dispatched")
    assert dispatched["task_id"] == task.id
    assert dispatched["run_id"] == step.run_id
    assert dispatched["artifact_ids"]
    assert dispatched["policy_id"] == "safe-local"


def test_objective_runner_stops_on_budget(tmp_path) -> None:
    store, objective = _init_project(tmp_path)
    first = _dry_task(store, "First", objective.id)
    second = _dry_task(store, "Second", objective.id)

    result = run_objective_autonomously(tmp_path, objective.id, autonomy_profile_id="safe-local", max_steps=1)

    assert result.ok is False
    assert result.stop_reason == "adapter_dispatch_budget_exhausted"
    assert result.adapter_dispatches == 1
    statuses = result.final_task_statuses
    assert statuses[first.id] == "succeeded"
    assert statuses[second.id] == "ready"


def test_objective_runner_stops_on_policy_denial(tmp_path) -> None:
    store, objective = _init_project(tmp_path)
    task = store.create_task(
        title="Unknown adapter",
        objective_id=objective.id,
        metadata={"execution_adapter": "unknown_adapter", "task_type": "phase_1a_test"},
    )

    result = run_objective_autonomously(tmp_path, objective.id, autonomy_profile_id="safe-local")

    assert result.ok is False
    assert result.stop_reason == "denied"
    assert result.adapter_dispatches == 0
    assert result.pause_reasons[0]["task_id"] == task.id
    assert "adapter is not registered: unknown_adapter" in result.pause_reasons[0]["reasons"]
    assert SQLiteStore(tmp_path).list_runs() == []
    assert SQLiteStore(tmp_path).get_task(task.id).status.value == "leased"


def test_objective_runner_resume_does_not_duplicate_task(tmp_path) -> None:
    store, objective = _init_project(tmp_path)
    first = _dry_task(store, "First", objective.id)
    second = _dry_task(store, "Second", objective.id, depends_on=[first.id])

    first_result = run_objective_autonomously(tmp_path, objective.id, autonomy_profile_id="safe-local", max_steps=1)
    second_result = run_objective_autonomously(tmp_path, objective.id, autonomy_profile_id="safe-local")

    fresh = SQLiteStore(tmp_path)
    tasks = fresh.list_tasks(objective_id=objective.id)
    assert first_result.stop_reason == "adapter_dispatch_budget_exhausted"
    assert second_result.stop_reason == "objective_succeeded"
    assert len(tasks) == 2
    assert {task.id for task in tasks} == {first.id, second.id}
    assert all(task.status.value == "succeeded" for task in tasks)


def test_objectives_run_cli_outputs_json(tmp_path) -> None:
    store, objective = _init_project(tmp_path)
    _dry_task(store, "Dry run", objective.id)

    result = runner.invoke(
        app,
        ["objectives", "run", objective.id, "--project", str(tmp_path), "--autonomy", "safe-local", "--output", "json"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["schema_version"] == "harness.autonomous_objective_run/v1"
    assert payload["stop_reason"] == "objective_succeeded"
    assert payload["adapter_dispatches"] == 1


def test_daemon_run_autonomous_cli_runs_next_active_objective(tmp_path) -> None:
    store, objective = _init_project(tmp_path)
    _dry_task(store, "Dry run", objective.id)

    result = runner.invoke(
        app,
        ["daemon", "run-autonomous", "--project", str(tmp_path), "--autonomy", "daemon-safe", "--output", "json"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["schema_version"] == "harness.autonomous_objective_run/v1"
    assert payload["objective_id"] == objective.id
    assert payload["stop_reason"] == "objective_succeeded"
