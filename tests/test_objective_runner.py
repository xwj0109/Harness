from __future__ import annotations

from datetime import datetime
import hashlib
import json

import pytest
from typer.testing import CliRunner

from harness.cli.main import app
from harness.config import HARNESS_DIR
from harness.execution import execute_lease
import harness.objective_runner as objective_runner_module
from harness.memory.sqlite_store import SQLiteStore
from harness.objective_batch_plan import OBJECTIVE_BATCH_PLAN_SCHEMA_VERSION, ObjectiveBatchPlan
from harness.objective_checkpoints import (
    create_objective_checkpoint,
    evaluate_objective_checkpoint_gate,
    list_objective_checkpoints,
    resolve_objective_checkpoint,
    verify_objective_checkpoint_evidence,
)
from harness.objective_evidence import verify_objective_evidence
from harness.objective_evidence import read_objective_evidence_events
from harness.objective_evidence_reconciliation import reconcile_objective_evidence
from harness.objective_runner import run_objective_autonomously, run_objective_parallel
from harness.orchestration_readiness import run_orchestration_readiness_audit
from harness.models import KillSwitchTargetKind, ObjectiveStatus


runner = CliRunner()


def _objective_event_sha256_for_test(event: dict) -> str:
    stable = {key: value for key, value in event.items() if key != "event_sha256"}
    return hashlib.sha256(json.dumps(stable, sort_keys=True, default=str).encode("utf-8")).hexdigest()


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
    priority: int = 0,
):
    return store.create_task(
        title=title,
        priority=priority,
        objective_id=objective_id,
        depends_on=depends_on,
        metadata={"execution_adapter": "dry_run", "task_type": "phase_1a_test", **dict(metadata or {})},
    )


def _read_jsonl_records(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _write_jsonl_records(path, records) -> None:
    path.write_text("\n".join(json.dumps(record, sort_keys=True) for record in records) + "\n", encoding="utf-8")


def _batch_plan_payload(event: dict) -> dict:
    return {key: event[key] for key in ObjectiveBatchPlan.model_fields if key in event}


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
    assert [event["event_index"] for event in events] == [1, 2, 3, 4]
    assert len({event["objective_event_id"] for event in events}) == len(events)
    assert events[0]["previous_event_sha256"] is None
    assert all(event["event_sha256"] for event in events)
    assert [event["previous_event_sha256"] for event in events[1:]] == [event["event_sha256"] for event in events[:-1]]
    for event in events:
        assert event["objective_id"] == objective.id
        assert event["objective_event_id"].startswith("oevt_")
        assert datetime.fromisoformat(event["created_at"]).tzinfo is not None
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


def test_parallel_objective_runner_dispatches_ready_tasks_in_bounded_batches(tmp_path) -> None:
    store, objective = _init_project(tmp_path)
    first = _dry_task(store, "First", objective.id, priority=30)
    second = _dry_task(store, "Second", objective.id, priority=20)
    third = _dry_task(store, "Third", objective.id, priority=10)

    result = run_objective_parallel(
        tmp_path,
        objective.id,
        autonomy_profile_id="safe-local",
        max_parallel=2,
    )

    assert result.ok is True
    assert result.scheduler_mode == "bounded_parallel"
    assert result.max_parallel == 2
    assert result.stop_reason == "objective_succeeded"
    assert result.adapter_dispatches == 3
    assert result.batches == 2
    assert [(step.task_id, step.batch) for step in result.step_results] == [
        (first.id, 1),
        (second.id, 1),
        (third.id, 2),
    ]
    fresh = SQLiteStore(tmp_path)
    assert {fresh.get_task(task.id).status.value for task in [first, second, third]} == {"succeeded"}
    events = [json.loads(line) for line in result.evidence_path.read_text(encoding="utf-8").splitlines()]
    assert [event["event"] for event in events] == [
        "started",
        "recovery_checked",
        "batch_planned",
        "batch_started",
        "adapter_dispatched",
        "adapter_dispatched",
        "batch_completed",
        "batch_planned",
        "batch_started",
        "adapter_dispatched",
        "batch_completed",
        "stopped",
    ]
    first_plan = events[2]
    assert first_plan["plan_schema_version"] == OBJECTIVE_BATCH_PLAN_SCHEMA_VERSION
    first_plan_model = ObjectiveBatchPlan.model_validate(_batch_plan_payload(first_plan))
    assert first_plan_model.scheduler_policy == "priority_then_critical_path"
    assert first_plan_model.policy_evidence.policy_id == "priority_then_critical_path"
    assert first_plan["policy_evidence"]["sort_keys"] == [
        "priority_desc",
        "critical_path_depth_desc",
        "downstream_task_count_desc",
        "created_at_asc",
        "task_id_asc",
    ]
    assert first_plan_model.selected[0].schedule_profile.task_id == first.id
    assert first_plan["candidate_task_ids"] == [first.id, second.id, third.id]
    assert first_plan["selected_task_ids"] == [first.id, second.id]
    assert [item["selection_source"] for item in first_plan["selected"]] == [
        "new_guarded_lease",
        "new_guarded_lease",
    ]
    assert first_plan["selected"][0]["autonomy_decision_id"].startswith("adec_")
    assert events[3]["task_ids"] == [first.id, second.id]
    first_completed = events[6]
    assert first_completed["batch_dispatches"] == 2
    assert first_completed["cumulative_adapter_dispatches"] == 2
    assert first_completed["adapter_dispatches"] == 2
    assert first_completed["execution_errors"] == 0
    second_plan = events[7]
    assert second_plan["candidate_task_ids"] == [third.id]
    assert second_plan["selected_task_ids"] == [third.id]
    assert events[8]["task_ids"] == [third.id]
    second_completed = events[10]
    assert second_completed["batch_dispatches"] == 1
    assert second_completed["cumulative_adapter_dispatches"] == 3
    assert second_completed["adapter_dispatches"] == 3
    assert second_completed["execution_errors"] == 0
    verification = verify_objective_evidence(tmp_path, objective.id)
    checks = {check.id: check for check in verification.checks}
    assert verification.ok is True
    assert checks["event_payload_schema"].status == "pass"
    assert checks["dispatch_links"].status == "pass"
    assert checks["batch_plan_links"].status == "pass"
    assert checks["batch_lifecycle"].status == "pass"
    assert checks["event_hash_chain"].status == "pass"


def test_parallel_objective_runner_respects_join_dependencies(tmp_path) -> None:
    store, objective = _init_project(tmp_path)
    first = _dry_task(store, "First", objective.id, priority=30)
    second = _dry_task(store, "Second", objective.id, priority=20)
    join = _dry_task(store, "Join", objective.id, depends_on=[first.id, second.id], priority=10)

    result = run_objective_parallel(
        tmp_path,
        objective.id,
        autonomy_profile_id="safe-local",
        max_parallel=2,
    )

    assert result.stop_reason == "objective_succeeded"
    assert [(step.task_id, step.batch) for step in result.step_results] == [
        (first.id, 1),
        (second.id, 1),
        (join.id, 2),
    ]
    events = [json.loads(line) for line in result.evidence_path.read_text(encoding="utf-8").splitlines()]
    plans = [event for event in events if event["event"] == "batch_planned"]
    assert len(plans) == 2
    first_join_snapshot = next(snapshot for snapshot in plans[0]["dependency_snapshots"] if snapshot["task_id"] == join.id)
    assert first_join_snapshot["dependency_statuses"] == {
        first.id: "leased",
        second.id: "leased",
    }
    assert set(first_join_snapshot["unresolved_dependency_ids"]) == {first.id, second.id}
    second_join_snapshot = next(snapshot for snapshot in plans[1]["dependency_snapshots"] if snapshot["task_id"] == join.id)
    assert second_join_snapshot["dependency_statuses"] == {
        first.id: "succeeded",
        second.id: "succeeded",
    }
    assert second_join_snapshot["unresolved_dependency_ids"] == []


def test_select_task_for_lease_targets_specific_ready_task_without_skipping_dependencies(tmp_path) -> None:
    store, objective = _init_project(tmp_path)
    dependency = _dry_task(store, "Dependency", objective.id)
    blocked = _dry_task(store, "Blocked", objective.id, depends_on=[dependency.id], priority=100)
    ready = _dry_task(store, "Ready", objective.id, priority=1)

    assert store.select_task_for_lease(blocked.id, owner="scheduler", objective_id=objective.id) is None
    selection = store.select_task_for_lease(ready.id, owner="scheduler", objective_id=objective.id)

    assert selection is not None
    assert selection["task"].id == ready.id
    assert SQLiteStore(tmp_path).get_task(blocked.id).status.value == "blocked"


def test_parallel_objective_runner_prioritizes_tied_ready_tasks_by_critical_path(tmp_path) -> None:
    store, objective = _init_project(tmp_path)
    independent = _dry_task(store, "Older independent work", objective.id)
    root = _dry_task(store, "Newer chain root", objective.id)
    middle = _dry_task(store, "Middle chain work", objective.id, depends_on=[root.id])

    result = run_objective_parallel(
        tmp_path,
        objective.id,
        autonomy_profile_id="safe-local",
        max_parallel=1,
    )

    assert result.stop_reason == "objective_succeeded"
    assert result.step_results[0].task_id == root.id
    events = [json.loads(line) for line in result.evidence_path.read_text(encoding="utf-8").splitlines()]
    first_plan = next(event for event in events if event["event"] == "batch_planned")
    ObjectiveBatchPlan.model_validate(_batch_plan_payload(first_plan))
    assert first_plan["scheduler_policy"] == "priority_then_critical_path"
    assert first_plan["candidate_task_ids"][:2] == [root.id, independent.id]
    assert first_plan["selected_task_ids"] == [root.id]
    assert first_plan["schedule_profiles"][root.id]["critical_path_depth"] == 1
    assert first_plan["schedule_profiles"][root.id]["downstream_task_count"] == 1
    assert first_plan["schedule_profiles"][independent.id]["critical_path_depth"] == 0
    assert first_plan["selected"][0]["schedule_profile"]["critical_path_depth"] == 1
    assert middle.id in result.final_task_statuses


def test_objective_evidence_verifier_fails_tampered_artifact_link(tmp_path) -> None:
    store, objective = _init_project(tmp_path)
    _dry_task(store, "Dry run", objective.id)

    result = run_objective_autonomously(tmp_path, objective.id, autonomy_profile_id="safe-local")
    lines = []
    for raw_line in result.evidence_path.read_text(encoding="utf-8").splitlines():
        event = json.loads(raw_line)
        if event["event"] == "adapter_dispatched":
            event["artifact_ids"] = ["art_missing"]
        lines.append(json.dumps(event, sort_keys=True))
    result.evidence_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    verification = verify_objective_evidence(tmp_path, objective.id)

    assert verification.ok is False
    dispatch_check = next(check for check in verification.checks if check.id == "dispatch_links")
    assert dispatch_check.status == "fail"
    assert dispatch_check.evidence["issues"][0]["reason"] == "artifact_missing"


def test_objective_evidence_verifier_fails_tampered_dispatch_run_status_and_decision(tmp_path) -> None:
    store, objective = _init_project(tmp_path)
    _dry_task(store, "Dry run", objective.id)

    result = run_objective_autonomously(tmp_path, objective.id, autonomy_profile_id="safe-local")
    dispatched = next(
        json.loads(line)
        for line in result.evidence_path.read_text(encoding="utf-8").splitlines()
        if json.loads(line)["event"] == "adapter_dispatched"
    )
    fresh = SQLiteStore(tmp_path)
    fresh.update_run_status(dispatched["run_id"], "failed")
    lease = fresh.get_task_lease(dispatched["lease_id"])
    with fresh.connect() as conn:
        conn.execute(
            "UPDATE task_leases SET metadata_json = ? WHERE id = ?",
            (json.dumps({**lease.metadata, "decision": "tampered_decision"}, sort_keys=True), lease.id),
        )

    verification = verify_objective_evidence(tmp_path, objective.id)
    dispatch_check = next(check for check in verification.checks if check.id == "dispatch_links")
    reasons = [issue["reason"] for issue in dispatch_check.evidence["issues"]]

    assert verification.ok is False
    assert dispatch_check.status == "fail"
    assert "run_status_ok_mismatch" in reasons
    assert "lease_decision_mismatch" in reasons


def test_objective_evidence_verifier_fails_tampered_stopped_summary(tmp_path) -> None:
    store, objective = _init_project(tmp_path)
    _dry_task(store, "Dry run", objective.id)

    result = run_objective_autonomously(tmp_path, objective.id, autonomy_profile_id="safe-local")
    lines = []
    for raw_line in result.evidence_path.read_text(encoding="utf-8").splitlines():
        event = json.loads(raw_line)
        if event["event"] == "stopped":
            event["adapter_dispatches"] = 0
        lines.append(json.dumps(event, sort_keys=True))
    result.evidence_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    verification = verify_objective_evidence(tmp_path, objective.id)

    assert verification.ok is False
    summary_check = next(check for check in verification.checks if check.id == "stopped_summary")
    assert summary_check.status == "fail"
    assert summary_check.evidence["issues"][0]["reason"] == "adapter_dispatches_mismatch"


def test_objective_evidence_verifier_fails_tampered_event_timestamp(tmp_path) -> None:
    store, objective = _init_project(tmp_path)
    _dry_task(store, "Dry run", objective.id)

    result = run_objective_autonomously(tmp_path, objective.id, autonomy_profile_id="safe-local")
    lines = []
    for raw_line in result.evidence_path.read_text(encoding="utf-8").splitlines():
        event = json.loads(raw_line)
        if event["event"] == "adapter_dispatched":
            event["created_at"] = "not-a-timestamp"
        lines.append(json.dumps(event, sort_keys=True))
    result.evidence_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    verification = verify_objective_evidence(tmp_path, objective.id)

    assert verification.ok is False
    timestamp_check = next(check for check in verification.checks if check.id == "event_timestamps")
    assert timestamp_check.status == "fail"
    assert timestamp_check.evidence["invalid"][0]["reason"] == "created_at_malformed"


def test_objective_evidence_verifier_fails_tampered_event_hash(tmp_path) -> None:
    store, objective = _init_project(tmp_path)
    _dry_task(store, "Dry run", objective.id)

    result = run_objective_autonomously(tmp_path, objective.id, autonomy_profile_id="safe-local")
    lines = []
    for raw_line in result.evidence_path.read_text(encoding="utf-8").splitlines():
        event = json.loads(raw_line)
        if event["event"] == "started":
            event["autonomy_profile_id"] = "tampered-profile"
        lines.append(json.dumps(event, sort_keys=True))
    result.evidence_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    verification = verify_objective_evidence(tmp_path, objective.id)

    assert verification.ok is False
    hash_check = next(check for check in verification.checks if check.id == "event_hash_chain")
    assert hash_check.status == "fail"
    assert hash_check.evidence["issues"][0]["reason"] == "event_sha256_mismatch"


def test_objective_evidence_verifier_fails_tampered_previous_event_hash(tmp_path) -> None:
    store, objective = _init_project(tmp_path)
    _dry_task(store, "Dry run", objective.id)

    result = run_objective_autonomously(tmp_path, objective.id, autonomy_profile_id="safe-local")
    events = [json.loads(line) for line in result.evidence_path.read_text(encoding="utf-8").splitlines()]
    events[1]["previous_event_sha256"] = "0" * 64
    result.evidence_path.write_text("\n".join(json.dumps(event, sort_keys=True) for event in events) + "\n", encoding="utf-8")

    verification = verify_objective_evidence(tmp_path, objective.id)

    assert verification.ok is False
    hash_check = next(check for check in verification.checks if check.id == "event_hash_chain")
    assert hash_check.status == "fail"
    reasons = [issue["reason"] for issue in hash_check.evidence["issues"]]
    assert "previous_event_sha256_mismatch" in reasons
    assert "event_sha256_mismatch" in reasons


def test_objective_evidence_verifier_hashes_raw_records_and_public_reader_sanitizes(tmp_path) -> None:
    store, objective = _init_project(tmp_path)
    _dry_task(store, "Dry run", objective.id)

    result = run_objective_autonomously(tmp_path, objective.id, autonomy_profile_id="safe-local")
    events = [json.loads(line) for line in result.evidence_path.read_text(encoding="utf-8").splitlines()]
    events[0]["diagnostic"] = "Bearer abcdefghijklmnop"
    previous_hash = None
    for event in events:
        event["previous_event_sha256"] = previous_hash
        event["event_sha256"] = _objective_event_sha256_for_test(event)
        previous_hash = event["event_sha256"]
    result.evidence_path.write_text("\n".join(json.dumps(event, sort_keys=True) for event in events) + "\n", encoding="utf-8")

    verification = verify_objective_evidence(tmp_path, objective.id)
    public_events, parse_errors = read_objective_evidence_events(result.evidence_path)

    assert verification.ok is True
    assert parse_errors == []
    assert public_events[0][1]["diagnostic"] == "[REDACTED_SECRET]"


def test_objective_evidence_verifier_redacts_secret_like_failed_diagnostics(tmp_path) -> None:
    store, objective = _init_project(tmp_path)
    _dry_task(store, "Dry run", objective.id)

    result = run_objective_autonomously(tmp_path, objective.id, autonomy_profile_id="safe-local")
    events = [json.loads(line) for line in result.evidence_path.read_text(encoding="utf-8").splitlines()]
    events[0]["schema_version"] = "Bearer abcdefghijklmnop"
    result.evidence_path.write_text("\n".join(json.dumps(event, sort_keys=True) for event in events) + "\n", encoding="utf-8")

    verification = verify_objective_evidence(tmp_path, objective.id)
    schema_check = next(check for check in verification.checks if check.id == "event_schema")
    rendered_evidence = json.dumps(schema_check.evidence, sort_keys=True)

    assert verification.ok is False
    assert schema_check.status == "fail"
    assert "[REDACTED_SECRET]" in rendered_evidence
    assert "abcdefghijklmnop" not in rendered_evidence


def test_objective_evidence_verifier_fails_duplicate_referenced_autonomy_decision(tmp_path) -> None:
    store, objective = _init_project(tmp_path)
    _dry_task(store, "Dry run", objective.id)

    result = run_objective_autonomously(tmp_path, objective.id, autonomy_profile_id="safe-local")
    decision_path = tmp_path / HARNESS_DIR / "autonomy" / "decisions.jsonl"
    decision_line = decision_path.read_text(encoding="utf-8").splitlines()[0]
    with decision_path.open("a", encoding="utf-8") as handle:
        handle.write(decision_line + "\n")

    verification = verify_objective_evidence(tmp_path, objective.id)
    dispatch_check = next(check for check in verification.checks if check.id == "dispatch_links")
    reasons = [issue["reason"] for issue in dispatch_check.evidence["issues"]]

    assert verification.ok is False
    assert dispatch_check.status == "fail"
    assert "autonomy_decision_id_duplicate_record_id" in reasons


def test_objective_evidence_verifier_fails_malformed_referenced_autonomy_decision_store(tmp_path) -> None:
    store, objective = _init_project(tmp_path)
    _dry_task(store, "Dry run", objective.id)

    run_objective_autonomously(tmp_path, objective.id, autonomy_profile_id="safe-local")
    decision_path = tmp_path / HARNESS_DIR / "autonomy" / "decisions.jsonl"
    with decision_path.open("a", encoding="utf-8") as handle:
        handle.write("not json\n")

    verification = verify_objective_evidence(tmp_path, objective.id)
    dispatch_check = next(check for check in verification.checks if check.id == "dispatch_links")
    reasons = [issue["reason"] for issue in dispatch_check.evidence["issues"]]

    assert verification.ok is False
    assert dispatch_check.status == "fail"
    assert "autonomy_decision_id_store_malformed" in reasons


def test_objective_evidence_verifier_fails_duplicate_event_id(tmp_path) -> None:
    store, objective = _init_project(tmp_path)
    _dry_task(store, "Dry run", objective.id)

    result = run_objective_autonomously(tmp_path, objective.id, autonomy_profile_id="safe-local")
    events = [json.loads(line) for line in result.evidence_path.read_text(encoding="utf-8").splitlines()]
    events[-1]["objective_event_id"] = events[0]["objective_event_id"]
    result.evidence_path.write_text("\n".join(json.dumps(event, sort_keys=True) for event in events) + "\n", encoding="utf-8")

    verification = verify_objective_evidence(tmp_path, objective.id)

    assert verification.ok is False
    identity_check = next(check for check in verification.checks if check.id == "event_identity")
    assert identity_check.status == "fail"
    assert identity_check.evidence["issues"][0]["reason"] == "objective_event_id_duplicate"


def test_objective_evidence_verifier_fails_out_of_sequence_event_index(tmp_path) -> None:
    store, objective = _init_project(tmp_path)
    _dry_task(store, "Dry run", objective.id)

    result = run_objective_autonomously(tmp_path, objective.id, autonomy_profile_id="safe-local")
    events = [json.loads(line) for line in result.evidence_path.read_text(encoding="utf-8").splitlines()]
    events[-1]["event_index"] = events[1]["event_index"]
    result.evidence_path.write_text("\n".join(json.dumps(event, sort_keys=True) for event in events) + "\n", encoding="utf-8")

    verification = verify_objective_evidence(tmp_path, objective.id)

    assert verification.ok is False
    identity_check = next(check for check in verification.checks if check.id == "event_identity")
    assert identity_check.status == "fail"
    reasons = [issue["reason"] for issue in identity_check.evidence["issues"]]
    assert "event_index_duplicate" in reasons
    assert "event_index_out_of_sequence" in reasons


def test_objective_evidence_verifier_fails_missing_event_objective_id(tmp_path) -> None:
    store, objective = _init_project(tmp_path)
    _dry_task(store, "Dry run", objective.id)

    result = run_objective_autonomously(tmp_path, objective.id, autonomy_profile_id="safe-local")
    lines = []
    for raw_line in result.evidence_path.read_text(encoding="utf-8").splitlines():
        event = json.loads(raw_line)
        if event["event"] == "recovery_checked":
            del event["objective_id"]
        lines.append(json.dumps(event, sort_keys=True))
    result.evidence_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    verification = verify_objective_evidence(tmp_path, objective.id)

    assert verification.ok is False
    schema_check = next(check for check in verification.checks if check.id == "event_schema")
    assert schema_check.status == "fail"
    assert schema_check.evidence["invalid"][0]["reason"] == "objective_id"


def test_objective_evidence_verifier_fails_tampered_event_payload_schema(tmp_path) -> None:
    store, objective = _init_project(tmp_path)
    _dry_task(store, "Dry run", objective.id)

    result = run_objective_autonomously(tmp_path, objective.id, autonomy_profile_id="safe-local")
    lines = []
    for raw_line in result.evidence_path.read_text(encoding="utf-8").splitlines():
        event = json.loads(raw_line)
        if event["event"] == "adapter_dispatched":
            del event["lease_id"]
        lines.append(json.dumps(event, sort_keys=True))
    result.evidence_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    verification = verify_objective_evidence(tmp_path, objective.id)

    assert verification.ok is False
    payload_schema = next(check for check in verification.checks if check.id == "event_payload_schema")
    assert payload_schema.status == "fail"
    assert payload_schema.evidence["issues"][0] == {
        "line": 3,
        "event": "adapter_dispatched",
        "field": "lease_id",
        "reason": "expected_string",
    }


def test_objective_evidence_verifier_fails_unknown_event_type(tmp_path) -> None:
    store, objective = _init_project(tmp_path)
    _dry_task(store, "Dry run", objective.id)

    result = run_objective_autonomously(tmp_path, objective.id, autonomy_profile_id="safe-local")
    lines = []
    for raw_line in result.evidence_path.read_text(encoding="utf-8").splitlines():
        event = json.loads(raw_line)
        if event["event"] == "recovery_checked":
            event["event"] = "unexpected_event"
        lines.append(json.dumps(event, sort_keys=True))
    result.evidence_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    verification = verify_objective_evidence(tmp_path, objective.id)

    assert verification.ok is False
    payload_schema = next(check for check in verification.checks if check.id == "event_payload_schema")
    assert payload_schema.status == "fail"
    assert payload_schema.evidence["issues"][0] == {
        "line": 2,
        "event": "unexpected_event",
        "reason": "unknown_event_type",
    }


def test_objective_evidence_verifier_fails_out_of_order_event_timestamp(tmp_path) -> None:
    store, objective = _init_project(tmp_path)
    _dry_task(store, "Dry run", objective.id)

    result = run_objective_autonomously(tmp_path, objective.id, autonomy_profile_id="safe-local")
    lines = []
    for raw_line in result.evidence_path.read_text(encoding="utf-8").splitlines():
        event = json.loads(raw_line)
        if event["event"] == "stopped":
            event["created_at"] = "2000-01-01T00:00:00+00:00"
        lines.append(json.dumps(event, sort_keys=True))
    result.evidence_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    verification = verify_objective_evidence(tmp_path, objective.id)

    assert verification.ok is False
    timestamp_check = next(check for check in verification.checks if check.id == "event_timestamps")
    assert timestamp_check.status == "fail"
    assert timestamp_check.evidence["invalid"][0]["reason"] == "created_at_out_of_order"


def test_objective_evidence_verifier_fails_tampered_missing_stopped_step(tmp_path) -> None:
    store, objective = _init_project(tmp_path)
    _dry_task(store, "Dry run", objective.id)

    result = run_objective_autonomously(tmp_path, objective.id, autonomy_profile_id="safe-local")
    lines = []
    for raw_line in result.evidence_path.read_text(encoding="utf-8").splitlines():
        event = json.loads(raw_line)
        if event["event"] == "stopped":
            event["steps"] = 0
            event["step_results"] = []
        lines.append(json.dumps(event, sort_keys=True))
    result.evidence_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    verification = verify_objective_evidence(tmp_path, objective.id)

    assert verification.ok is False
    summary_check = next(check for check in verification.checks if check.id == "stopped_summary")
    assert summary_check.status == "fail"
    reasons = [issue["reason"] for issue in summary_check.evidence["issues"]]
    assert "step_event_count_mismatch" in reasons
    assert "expected_step_missing" in reasons


def test_objective_evidence_verifier_fails_tampered_stopped_step_task_type(tmp_path) -> None:
    store, objective = _init_project(tmp_path)
    _dry_task(store, "Dry run", objective.id)

    result = run_objective_autonomously(tmp_path, objective.id, autonomy_profile_id="safe-local")
    lines = []
    for raw_line in result.evidence_path.read_text(encoding="utf-8").splitlines():
        event = json.loads(raw_line)
        if event["event"] == "stopped":
            event["step_results"][0]["task_type"] = "tampered_task_type"
        lines.append(json.dumps(event, sort_keys=True))
    result.evidence_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    verification = verify_objective_evidence(tmp_path, objective.id)

    assert verification.ok is False
    summary_check = next(check for check in verification.checks if check.id == "stopped_summary")
    assert summary_check.status == "fail"
    assert any(
        issue["reason"] == "step_task_type_mismatch" and issue["actual"] == "tampered_task_type"
        for issue in summary_check.evidence["issues"]
    )


def test_objective_evidence_verifier_fails_tampered_batch_started_tasks(tmp_path) -> None:
    store, objective = _init_project(tmp_path)
    first = _dry_task(store, "First", objective.id, priority=20)
    second = _dry_task(store, "Second", objective.id, priority=10)

    result = run_objective_parallel(
        tmp_path,
        objective.id,
        autonomy_profile_id="safe-local",
        max_parallel=2,
    )
    lines = []
    for raw_line in result.evidence_path.read_text(encoding="utf-8").splitlines():
        event = json.loads(raw_line)
        if event["event"] == "batch_started":
            event["task_ids"] = [second.id, first.id]
        lines.append(json.dumps(event, sort_keys=True))
    result.evidence_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    verification = verify_objective_evidence(tmp_path, objective.id)

    assert verification.ok is False
    batch_check = next(check for check in verification.checks if check.id == "batch_lifecycle")
    assert batch_check.status == "fail"
    assert batch_check.evidence["issues"][0]["reason"] == "batch_started_task_ids_mismatch"


def test_objective_evidence_verifier_fails_tampered_batch_plan_decision_payload(tmp_path) -> None:
    store, objective = _init_project(tmp_path)
    _dry_task(store, "First", objective.id, priority=20)
    _dry_task(store, "Second", objective.id, priority=10)

    result = run_objective_parallel(
        tmp_path,
        objective.id,
        autonomy_profile_id="safe-local",
        max_parallel=2,
    )
    batch_plan = next(
        json.loads(line)
        for line in result.evidence_path.read_text(encoding="utf-8").splitlines()
        if json.loads(line)["event"] == "batch_planned"
    )
    decision_id = batch_plan["selected"][0]["autonomy_decision_id"]
    decisions_path = tmp_path / HARNESS_DIR / "autonomy" / "decisions.jsonl"
    decisions = _read_jsonl_records(decisions_path)
    for decision in decisions:
        if decision["record_id"] == decision_id:
            decision["objective_run_id"] = "orun_tampered"
            decision["tool_name"] = "unknown_dispatch_tool"
            decision["adapter_id"] = "unknown_adapter"
            decision["task_type"] = "tampered_task_type"
            decision["status"] = "denied"
    _write_jsonl_records(decisions_path, decisions)

    verification = verify_objective_evidence(tmp_path, objective.id)
    batch_check = next(check for check in verification.checks if check.id == "batch_plan_links")
    reasons = [issue["reason"] for issue in batch_check.evidence["issues"]]

    assert verification.ok is False
    assert batch_check.status == "fail"
    assert "selected_autonomy_decision_objective_run_id_mismatch" in reasons
    assert "selected_autonomy_decision_tool_name_mismatch" in reasons
    assert "selected_autonomy_decision_adapter_id_mismatch" in reasons
    assert "selected_autonomy_decision_task_type_mismatch" in reasons
    assert "selected_autonomy_decision_status_mismatch" in reasons


def test_objective_evidence_verifier_fails_tampered_batch_plan_scheduler_order(tmp_path) -> None:
    store, objective = _init_project(tmp_path)
    independent = _dry_task(store, "Older independent work", objective.id)
    root = _dry_task(store, "Newer chain root", objective.id)
    _dry_task(store, "Middle chain work", objective.id, depends_on=[root.id])

    result = run_objective_parallel(
        tmp_path,
        objective.id,
        autonomy_profile_id="safe-local",
        max_parallel=1,
    )
    lines = []
    tampered = False
    for raw_line in result.evidence_path.read_text(encoding="utf-8").splitlines():
        event = json.loads(raw_line)
        if event["event"] == "batch_planned" and not tampered:
            assert event["candidate_task_ids"][:2] == [root.id, independent.id]
            event["candidate_task_ids"] = [independent.id, root.id, *event["candidate_task_ids"][2:]]
            tampered = True
        lines.append(json.dumps(event, sort_keys=True))
    result.evidence_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    verification = verify_objective_evidence(tmp_path, objective.id)
    batch_check = next(check for check in verification.checks if check.id == "batch_plan_links")
    reasons = [issue["reason"] for issue in batch_check.evidence["issues"]]

    assert tampered is True
    assert verification.ok is False
    assert batch_check.status == "fail"
    assert "candidate_task_ids_policy_order_mismatch" in reasons
    assert "selected_task_ids_not_policy_prefix" in reasons


def test_objective_evidence_verifier_fails_selected_batch_pair_without_terminal_event(tmp_path) -> None:
    store, objective = _init_project(tmp_path)
    _dry_task(store, "First", objective.id, priority=20)
    _dry_task(store, "Second", objective.id, priority=10)

    result = run_objective_parallel(
        tmp_path,
        objective.id,
        autonomy_profile_id="safe-local",
        max_parallel=2,
    )
    removed_dispatch = False
    lines = []
    for raw_line in result.evidence_path.read_text(encoding="utf-8").splitlines():
        event = json.loads(raw_line)
        if event["event"] == "adapter_dispatched" and not removed_dispatch:
            removed_dispatch = True
            continue
        lines.append(json.dumps(event, sort_keys=True))
    result.evidence_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    verification = verify_objective_evidence(tmp_path, objective.id)
    batch_check = next(check for check in verification.checks if check.id == "batch_lifecycle")
    reasons = [issue["reason"] for issue in batch_check.evidence["issues"]]

    assert removed_dispatch is True
    assert verification.ok is False
    assert batch_check.status == "fail"
    assert "selected_pair_missing_terminal_event" in reasons


def test_objective_evidence_verifier_fails_tampered_batch_completion_dispatch_count(tmp_path) -> None:
    store, objective = _init_project(tmp_path)
    _dry_task(store, "First", objective.id, priority=20)
    _dry_task(store, "Second", objective.id, priority=10)

    result = run_objective_parallel(
        tmp_path,
        objective.id,
        autonomy_profile_id="safe-local",
        max_parallel=2,
    )
    lines = []
    for raw_line in result.evidence_path.read_text(encoding="utf-8").splitlines():
        event = json.loads(raw_line)
        if event["event"] == "batch_completed":
            event["adapter_dispatches"] = 1
        lines.append(json.dumps(event, sort_keys=True))
    result.evidence_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    verification = verify_objective_evidence(tmp_path, objective.id)

    assert verification.ok is False
    batch_check = next(check for check in verification.checks if check.id == "batch_lifecycle")
    assert batch_check.status == "fail"
    assert batch_check.evidence["issues"][0]["reason"] == "batch_completed_adapter_dispatches_mismatch"


def test_objective_evidence_verifier_fails_tampered_batch_completion_explicit_counts(tmp_path) -> None:
    store, objective = _init_project(tmp_path)
    _dry_task(store, "First", objective.id, priority=20)
    _dry_task(store, "Second", objective.id, priority=10)

    result = run_objective_parallel(
        tmp_path,
        objective.id,
        autonomy_profile_id="safe-local",
        max_parallel=2,
    )
    lines = []
    for raw_line in result.evidence_path.read_text(encoding="utf-8").splitlines():
        event = json.loads(raw_line)
        if event["event"] == "batch_completed":
            event["batch_dispatches"] = 1
            event["cumulative_adapter_dispatches"] = 1
            event["execution_errors"] = 1
        lines.append(json.dumps(event, sort_keys=True))
    result.evidence_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    verification = verify_objective_evidence(tmp_path, objective.id)

    assert verification.ok is False
    batch_check = next(check for check in verification.checks if check.id == "batch_lifecycle")
    assert batch_check.status == "fail"
    reasons = [issue["reason"] for issue in batch_check.evidence["issues"]]
    assert "batch_completed_batch_dispatches_mismatch" in reasons
    assert "batch_completed_cumulative_adapter_dispatches_mismatch" in reasons
    assert "batch_completed_execution_errors_mismatch" in reasons


def test_parallel_objective_runner_records_worker_exception_as_linked_outcome(tmp_path, monkeypatch) -> None:
    store, objective = _init_project(tmp_path)
    task = _dry_task(store, "Worker exception", objective.id)

    def boom(project_root, lease_id, owner="autonomous_objective_runner"):
        raise ValueError("simulated adapter boundary failure")

    monkeypatch.setattr("harness.objective_runner.execute_lease", boom)

    result = run_objective_parallel(
        tmp_path,
        objective.id,
        autonomy_profile_id="safe-local",
        max_parallel=1,
    )

    assert result.ok is False
    assert result.stop_reason == "execution_error"
    assert result.adapter_dispatches == 0
    assert result.consecutive_failures == 1
    assert result.errors == ["simulated adapter boundary failure"]
    assert result.step_results[0].task_id == task.id
    assert result.step_results[0].stop_reason == "execution_error"
    events = [json.loads(line) for line in result.evidence_path.read_text(encoding="utf-8").splitlines()]
    execution_error = next(event for event in events if event["event"] == "execution_error")
    assert execution_error["task_id"] == task.id
    assert execution_error["adapter_id"] == "dry_run"
    assert execution_error["policy_id"] == "safe-local"
    assert execution_error["autonomy_decision_id"].startswith("adec_")
    assert execution_error["autonomous_approval_id"].startswith("auto_")
    assert execution_error["autonomous_outcome_id"].startswith("aout_")
    assert execution_error["error"] == "simulated adapter boundary failure"
    completed = next(event for event in events if event["event"] == "batch_completed")
    assert completed["batch_dispatches"] == 0
    assert completed["execution_errors"] == 1
    outcomes_path = tmp_path / HARNESS_DIR / "autonomy" / "outcomes.jsonl"
    outcomes = [json.loads(line) for line in outcomes_path.read_text(encoding="utf-8").splitlines()]
    outcome = next(record for record in outcomes if record["record_id"] == execution_error["autonomous_outcome_id"])
    assert outcome["ok"] is False
    assert outcome["run_id"] is None
    assert outcome["artifact_ids"] == []
    assert outcome["error"] == "simulated adapter boundary failure"
    verification = verify_objective_evidence(tmp_path, objective.id)
    checks = {check.id: check for check in verification.checks}
    assert verification.ok is True
    assert checks["event_payload_schema"].status == "pass"
    assert checks["dispatch_links"].status == "pass"
    assert checks["dispatch_links"].evidence["execution_error_count"] == 1
    assert checks["batch_lifecycle"].status == "pass"


def test_objective_evidence_verifier_fails_tampered_dispatch_outcome_payload(tmp_path) -> None:
    store, objective = _init_project(tmp_path)
    _dry_task(store, "Dry run", objective.id)

    result = run_objective_autonomously(tmp_path, objective.id, autonomy_profile_id="safe-local")
    dispatched = next(
        json.loads(line)
        for line in result.evidence_path.read_text(encoding="utf-8").splitlines()
        if json.loads(line)["event"] == "adapter_dispatched"
    )
    outcomes_path = tmp_path / HARNESS_DIR / "autonomy" / "outcomes.jsonl"
    outcomes = [json.loads(line) for line in outcomes_path.read_text(encoding="utf-8").splitlines()]
    for outcome in outcomes:
        if outcome["record_id"] == dispatched["autonomous_outcome_id"]:
            outcome["artifact_ids"] = []
    outcomes_path.write_text("\n".join(json.dumps(outcome, sort_keys=True) for outcome in outcomes) + "\n", encoding="utf-8")

    verification = verify_objective_evidence(tmp_path, objective.id)
    dispatch_check = next(check for check in verification.checks if check.id == "dispatch_links")
    reasons = [issue["reason"] for issue in dispatch_check.evidence["issues"]]

    assert verification.ok is False
    assert dispatch_check.status == "fail"
    assert "autonomous_outcome_id_artifact_ids_mismatch" in reasons


def test_objective_evidence_verifier_fails_tampered_execution_error_outcome_payload(
    tmp_path,
    monkeypatch,
) -> None:
    store, objective = _init_project(tmp_path)
    _dry_task(store, "Worker exception", objective.id)

    def boom(project_root, lease_id, owner="autonomous_objective_runner"):
        raise ValueError("simulated adapter boundary failure")

    monkeypatch.setattr("harness.objective_runner.execute_lease", boom)
    result = run_objective_parallel(tmp_path, objective.id, autonomy_profile_id="safe-local", max_parallel=1)
    execution_error = next(
        json.loads(line)
        for line in result.evidence_path.read_text(encoding="utf-8").splitlines()
        if json.loads(line)["event"] == "execution_error"
    )
    outcomes_path = tmp_path / HARNESS_DIR / "autonomy" / "outcomes.jsonl"
    outcomes = [json.loads(line) for line in outcomes_path.read_text(encoding="utf-8").splitlines()]
    for outcome in outcomes:
        if outcome["record_id"] == execution_error["autonomous_outcome_id"]:
            outcome["ok"] = True
            outcome["error"] = "tampered"
    outcomes_path.write_text("\n".join(json.dumps(outcome, sort_keys=True) for outcome in outcomes) + "\n", encoding="utf-8")

    verification = verify_objective_evidence(tmp_path, objective.id)
    dispatch_check = next(check for check in verification.checks if check.id == "dispatch_links")
    reasons = [issue["reason"] for issue in dispatch_check.evidence["issues"]]

    assert verification.ok is False
    assert dispatch_check.status == "fail"
    assert "autonomous_outcome_id_ok_mismatch" in reasons
    assert "autonomous_outcome_id_error_mismatch" in reasons


def test_objective_evidence_verifier_fails_tampered_approval_authority_payload(tmp_path) -> None:
    store, objective = _init_project(tmp_path)
    _dry_task(store, "Dry run", objective.id)

    result = run_objective_autonomously(tmp_path, objective.id, autonomy_profile_id="safe-local")
    dispatched = next(
        json.loads(line)
        for line in result.evidence_path.read_text(encoding="utf-8").splitlines()
        if json.loads(line)["event"] == "adapter_dispatched"
    )
    approvals_path = tmp_path / HARNESS_DIR / "autonomy" / "approvals.jsonl"
    approvals = _read_jsonl_records(approvals_path)
    for approval in approvals:
        if approval["id"] == dispatched["autonomous_approval_id"]:
            approval["decision_status"] = "approval_required"
            approval["tool_name"] = "unknown_dispatch_tool"
            approval["task_type"] = "tampered_task_type"
    _write_jsonl_records(approvals_path, approvals)

    verification = verify_objective_evidence(tmp_path, objective.id)
    dispatch_check = next(check for check in verification.checks if check.id == "dispatch_links")
    reasons = [issue["reason"] for issue in dispatch_check.evidence["issues"]]

    assert verification.ok is False
    assert dispatch_check.status == "fail"
    assert "autonomous_approval_id_decision_status_mismatch" in reasons
    assert "autonomous_approval_id_tool_name_mismatch" in reasons
    assert "autonomous_approval_id_task_type_mismatch" in reasons


def test_objective_evidence_verifier_fails_tampered_outcome_authority_payload(tmp_path) -> None:
    store, objective = _init_project(tmp_path)
    _dry_task(store, "Dry run", objective.id)

    result = run_objective_autonomously(tmp_path, objective.id, autonomy_profile_id="safe-local")
    dispatched = next(
        json.loads(line)
        for line in result.evidence_path.read_text(encoding="utf-8").splitlines()
        if json.loads(line)["event"] == "adapter_dispatched"
    )
    outcomes_path = tmp_path / HARNESS_DIR / "autonomy" / "outcomes.jsonl"
    outcomes = _read_jsonl_records(outcomes_path)
    for outcome in outcomes:
        if outcome["record_id"] == dispatched["autonomous_outcome_id"]:
            outcome["decision_status"] = "denied"
            outcome["tool_name"] = "unknown_dispatch_tool"
            outcome["task_type"] = "tampered_task_type"
    _write_jsonl_records(outcomes_path, outcomes)

    verification = verify_objective_evidence(tmp_path, objective.id)
    dispatch_check = next(check for check in verification.checks if check.id == "dispatch_links")
    reasons = [issue["reason"] for issue in dispatch_check.evidence["issues"]]

    assert verification.ok is False
    assert dispatch_check.status == "fail"
    assert "autonomous_outcome_id_decision_status_mismatch" in reasons
    assert "autonomous_outcome_id_tool_name_mismatch" in reasons
    assert "autonomous_outcome_id_task_type_mismatch" in reasons


def test_parallel_objective_runner_stops_on_policy_pause_before_dispatch(tmp_path) -> None:
    store, objective = _init_project(tmp_path)
    denied = store.create_task(
        title="Hosted adapter",
        priority=100,
        objective_id=objective.id,
        metadata={"execution_adapter": "read_only_summary", "task_type": "read_only_repo_summary"},
    )
    safe = _dry_task(store, "Safe", objective.id, priority=10)

    result = run_objective_parallel(
        tmp_path,
        objective.id,
        autonomy_profile_id="safe-local",
        max_parallel=2,
    )

    assert result.ok is False
    assert result.stop_reason == "approval_required"
    assert result.adapter_dispatches == 0
    assert result.pause_reasons[0]["task_id"] == denied.id
    assert result.pause_reasons[0]["lease_id"] is None
    assert result.step_results[0].lease_id is None
    fresh = SQLiteStore(tmp_path)
    assert fresh.get_task(denied.id).status.value == "ready"
    assert fresh.list_task_attempts(denied.id) == []
    assert fresh.list_task_leases(denied.id) == []
    assert fresh.get_task(safe.id).status.value == "ready"
    assert fresh.list_task_attempts(safe.id) == []


def test_registered_dispatch_rejects_foreign_lease_owner(tmp_path) -> None:
    store, objective = _init_project(tmp_path)
    task = _dry_task(store, "Foreign lease", objective.id)
    selection = store.select_next_task_for_lease(owner="other_runner")
    lease = selection["lease"]

    result = execute_lease(tmp_path, lease.id, owner="objective_runner")

    assert result.ok is False
    assert result.security_decision is not None
    assert result.security_decision.reason_code == "lease_owner_mismatch"
    assert "Lease owner mismatch" in result.security_decision.reasons[0]
    fresh = SQLiteStore(tmp_path)
    assert fresh.list_runs() == []
    assert fresh.get_task(task.id).status.value == "leased"
    assert fresh.get_task_lease(lease.id).owner == "other_runner"


def test_objective_runner_pauses_on_foreign_active_lease_without_dispatch(tmp_path) -> None:
    store, objective = _init_project(tmp_path)
    task = _dry_task(store, "Foreign active lease", objective.id)
    selection = store.select_next_task_for_lease(owner="other_runner")
    lease = selection["lease"]

    result = run_objective_autonomously(
        tmp_path,
        objective.id,
        autonomy_profile_id="safe-local",
        owner="objective_runner",
    )

    assert result.ok is False
    assert result.stop_reason == "blocked_or_no_ready_task"
    assert result.adapter_dispatches == 0
    assert result.pause_reasons == [
        {
            "task_id": task.id,
            "status": "leased",
            "decision": "active_lease",
            "reason": "Task has an active lease owned by another runner.",
            "active_lease_ids": [lease.id],
            "active_lease_owners": ["other_runner"],
        }
    ]
    fresh = SQLiteStore(tmp_path)
    assert fresh.list_runs() == []
    assert fresh.get_task(task.id).status.value == "leased"
    assert fresh.get_task_lease(lease.id).owner == "other_runner"


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


def test_objective_runner_stops_on_policy_pause(tmp_path) -> None:
    store, objective = _init_project(tmp_path)
    task = store.create_task(
        title="Hosted adapter",
        objective_id=objective.id,
        metadata={"execution_adapter": "read_only_summary", "task_type": "read_only_repo_summary"},
    )

    result = run_objective_autonomously(tmp_path, objective.id, autonomy_profile_id="safe-local")

    assert result.ok is False
    assert result.stop_reason == "approval_required"
    assert result.adapter_dispatches == 0
    assert result.pause_reasons[0]["task_id"] == task.id
    assert result.pause_reasons[0]["lease_id"] is None
    assert result.step_results[0].lease_id is None
    assert "boundary is not auto-allowed by autonomy profile: hosted_provider_codex" in result.pause_reasons[0]["reasons"]
    stopped_event = next(
        json.loads(line)
        for line in result.evidence_path.read_text(encoding="utf-8").splitlines()
        if json.loads(line)["event"] == "autonomy_stopped"
    )
    fresh = SQLiteStore(tmp_path)
    assert stopped_event["lease_id"] is None
    assert fresh.list_runs() == []
    assert fresh.get_task(task.id).status.value == "ready"
    assert fresh.list_task_attempts(task.id) == []
    assert fresh.list_task_leases(task.id) == []


def test_objective_evidence_verifier_fails_missing_stopped_decision_record(tmp_path) -> None:
    store, objective = _init_project(tmp_path)
    store.create_task(
        title="Hosted adapter",
        objective_id=objective.id,
        metadata={"execution_adapter": "read_only_summary", "task_type": "read_only_repo_summary"},
    )

    result = run_objective_autonomously(tmp_path, objective.id, autonomy_profile_id="safe-local")
    stopped_event = next(
        json.loads(line)
        for line in result.evidence_path.read_text(encoding="utf-8").splitlines()
        if json.loads(line)["event"] == "autonomy_stopped"
    )
    decisions_path = tmp_path / HARNESS_DIR / "autonomy" / "decisions.jsonl"
    decisions = [
        decision
        for decision in _read_jsonl_records(decisions_path)
        if decision["record_id"] != stopped_event["autonomy_decision_id"]
    ]
    _write_jsonl_records(decisions_path, decisions)

    verification = verify_objective_evidence(tmp_path, objective.id)
    summary_check = next(check for check in verification.checks if check.id == "stopped_summary")
    reasons = [issue["reason"] for issue in summary_check.evidence["issues"]]

    assert verification.ok is False
    assert summary_check.status == "fail"
    assert "autonomy_decision_id_not_found" in reasons


def test_objective_evidence_verifier_fails_tampered_stopped_decision_payload(tmp_path) -> None:
    store, objective = _init_project(tmp_path)
    store.create_task(
        title="Hosted adapter",
        objective_id=objective.id,
        metadata={"execution_adapter": "read_only_summary", "task_type": "read_only_repo_summary"},
    )

    result = run_objective_autonomously(tmp_path, objective.id, autonomy_profile_id="safe-local")
    stopped_event = next(
        json.loads(line)
        for line in result.evidence_path.read_text(encoding="utf-8").splitlines()
        if json.loads(line)["event"] == "autonomy_stopped"
    )
    decisions_path = tmp_path / HARNESS_DIR / "autonomy" / "decisions.jsonl"
    decisions = _read_jsonl_records(decisions_path)
    for decision in decisions:
        if decision["record_id"] == stopped_event["autonomy_decision_id"]:
            decision["status"] = "auto_allowed"
            decision["tool_name"] = "unknown_dispatch_tool"
            decision["task_type"] = "tampered_task_type"
    _write_jsonl_records(decisions_path, decisions)

    verification = verify_objective_evidence(tmp_path, objective.id)
    summary_check = next(check for check in verification.checks if check.id == "stopped_summary")
    reasons = [issue["reason"] for issue in summary_check.evidence["issues"]]

    assert verification.ok is False
    assert summary_check.status == "fail"
    assert "autonomy_decision_id_embedded_status_mismatch" in reasons
    assert "autonomy_decision_id_tool_name_mismatch" in reasons
    assert "autonomy_decision_id_embedded_task_type_mismatch" in reasons
    assert "step_decision_status_mismatch" in reasons


def test_objective_runner_backend_control_blocks_codex_adapter_before_dispatch(tmp_path) -> None:
    store, objective = _init_project(tmp_path)
    task = store.create_task(
        title="Repo planning",
        objective_id=objective.id,
        metadata={"execution_adapter": "repo_planning", "task_type": "repo_planning"},
    )
    store.disable_execution_control(
        KillSwitchTargetKind.BACKEND,
        "codex_cli",
        reason="pause hosted Codex backend",
        actor="test",
    )

    result = run_objective_autonomously(tmp_path, objective.id, autonomy_profile_id="supervised-codex")

    assert result.ok is False
    assert result.stop_reason == "denied"
    assert result.adapter_dispatches == 0
    assert result.pause_reasons[0]["task_id"] == task.id
    assert result.pause_reasons[0]["lease_id"] is None
    assert result.pause_reasons[0]["adapter_id"] == "repo_planning"
    assert "runtime kill switch is active" in result.pause_reasons[0]["reasons"]
    fresh = SQLiteStore(tmp_path)
    assert fresh.list_runs() == []
    assert fresh.get_task(task.id).status.value == "ready"
    assert fresh.list_task_attempts(task.id) == []
    assert fresh.list_task_leases(task.id) == []


def test_objective_runner_records_lease_guard_pause_after_stale_prelease_control(monkeypatch, tmp_path) -> None:
    store, objective = _init_project(tmp_path)
    task = _dry_task(store, "Paused after prelease decision", objective.id)
    store.disable_execution_control(
        KillSwitchTargetKind.ADAPTER,
        "dry_run",
        reason="operator pause",
        actor="test",
    )
    monkeypatch.setattr(objective_runner_module, "_kill_switch_active", lambda *_args, **_kwargs: False)

    result = run_objective_autonomously(tmp_path, objective.id, autonomy_profile_id="safe-local")

    assert result.ok is False
    assert result.stop_reason == "control_disabled"
    assert result.adapter_dispatches == 0
    assert result.pause_reasons[0]["task_id"] == task.id
    assert result.pause_reasons[0]["decision"] == "control_disabled"
    assert result.step_results[0].task_id == task.id
    assert result.step_results[0].lease_id is None
    assert result.step_results[0].decision_status == "auto_allowed"
    assert result.step_results[0].stop_reason == "control_disabled"
    events = _read_jsonl_records(result.evidence_path)
    guard_event = next(event for event in events if event["event"] == "lease_guard_stopped")
    assert guard_event["task_id"] == task.id
    assert guard_event["lease_id"] is None
    assert guard_event["stop_reason"] == "control_disabled"
    assert guard_event["guard_pause_reasons"][0]["target_kind"] == "adapter"
    assert verify_objective_evidence(tmp_path, objective.id).ok is True
    fresh = SQLiteStore(tmp_path)
    assert fresh.list_runs() == []
    assert fresh.get_task(task.id).status.value == "ready"
    assert fresh.list_task_attempts(task.id) == []
    assert fresh.list_task_leases(task.id) == []


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


def test_objective_cancel_cli_blocks_later_dispatch_without_leases(tmp_path) -> None:
    store, objective = _init_project(tmp_path)
    task = _dry_task(store, "Ready dry run", objective.id)

    cancelled = runner.invoke(
        app,
        [
            "objectives",
            "cancel",
            objective.id,
            "--reason",
            "operator pause",
            "--actor",
            "tester",
            "--project",
            str(tmp_path),
            "--output",
            "json",
        ],
    )
    result = run_objective_autonomously(tmp_path, objective.id, autonomy_profile_id="safe-local")
    events, parse_errors = read_objective_evidence_events(result.evidence_path)

    assert cancelled.exit_code == 0, cancelled.output
    payload = json.loads(cancelled.output)
    assert payload["schema_version"] == "harness.objective_lifecycle/v1"
    assert payload["objective"]["status"] == "cancelled"
    assert payload["operator_authority"]["execution_allowed"] is False
    assert payload["objective"]["metadata"]["last_lifecycle_event"]["actor"] == "tester"
    assert result.ok is False
    assert result.stop_reason == "objective_inactive"
    assert result.steps == 0
    assert result.pause_reasons[0]["objective_status"] == "cancelled"
    assert parse_errors == []
    assert [event["event"] for _, event in events] == ["started", "stopped"]
    assert events[-1][1]["stop_reason"] == "objective_inactive"
    fresh = SQLiteStore(tmp_path)
    assert fresh.get_task(task.id).status.value == "ready"
    assert fresh.list_task_attempts(task.id) == []
    assert fresh.list_task_leases(task.id) == []
    assert fresh.list_runs() == []


def test_objective_suspend_resume_cli_blocks_then_allows_dispatch(tmp_path) -> None:
    store, objective = _init_project(tmp_path)
    task = _dry_task(store, "Ready dry run", objective.id)

    suspended = runner.invoke(
        app,
        [
            "objectives",
            "suspend",
            objective.id,
            "--reason",
            "operator pause",
            "--actor",
            "tester",
            "--project",
            str(tmp_path),
            "--output",
            "json",
        ],
    )
    blocked = run_objective_autonomously(tmp_path, objective.id, autonomy_profile_id="safe-local")

    assert suspended.exit_code == 0, suspended.output
    suspended_payload = json.loads(suspended.output)
    assert suspended_payload["schema_version"] == "harness.objective_lifecycle/v1"
    assert suspended_payload["objective"]["status"] == "suspended"
    assert suspended_payload["operator_authority"]["execution_allowed"] is False
    assert blocked.ok is False
    assert blocked.stop_reason == "objective_inactive"
    assert blocked.adapter_dispatches == 0
    assert blocked.pause_reasons[0]["objective_status"] == "suspended"
    fresh = SQLiteStore(tmp_path)
    assert fresh.get_task(task.id).status.value == "ready"
    assert fresh.list_task_attempts(task.id) == []
    assert fresh.list_task_leases(task.id) == []
    assert fresh.list_runs() == []

    resumed = runner.invoke(
        app,
        [
            "objectives",
            "resume",
            objective.id,
            "--reason",
            "operator resumed",
            "--actor",
            "tester",
            "--project",
            str(tmp_path),
            "--output",
            "json",
        ],
    )
    dispatched = run_objective_autonomously(tmp_path, objective.id, autonomy_profile_id="safe-local")

    assert resumed.exit_code == 0, resumed.output
    resumed_payload = json.loads(resumed.output)
    assert resumed_payload["objective"]["status"] == "active"
    assert resumed_payload["objective"]["metadata"]["last_lifecycle_event"]["from_status"] == "suspended"
    assert dispatched.ok is True
    assert dispatched.stop_reason == "objective_succeeded"
    assert dispatched.adapter_dispatches == 1
    final = SQLiteStore(tmp_path)
    assert final.get_task(task.id).status.value == "succeeded"
    assert len(final.list_task_attempts(task.id)) == 1
    assert len(final.list_task_leases(task.id)) == 1
    assert len(final.list_runs()) == 1


def test_objective_add_draft_start_cli_blocks_then_allows_dispatch(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    added = runner.invoke(
        app,
        [
            "objectives",
            "add",
            "--title",
            "Draft autonomous objective",
            "--draft",
            "--project",
            str(tmp_path),
            "--output",
            "json",
        ],
    )

    assert added.exit_code == 0, added.output
    added_payload = json.loads(added.output)
    assert added_payload["schema_version"] == "harness.objective/v1"
    assert added_payload["objective"]["status"] == "created"
    objective_id = added_payload["objective"]["id"]
    store = SQLiteStore(tmp_path)
    task = _dry_task(store, "Ready dry run", objective_id)
    blocked = run_objective_autonomously(tmp_path, objective_id, autonomy_profile_id="safe-local")

    assert blocked.ok is False
    assert blocked.stop_reason == "objective_inactive"
    assert blocked.adapter_dispatches == 0
    assert blocked.pause_reasons[0]["objective_status"] == "created"
    assert SQLiteStore(tmp_path).get_task(task.id).status.value == "ready"
    assert SQLiteStore(tmp_path).list_task_attempts(task.id) == []
    assert SQLiteStore(tmp_path).list_task_leases(task.id) == []
    assert SQLiteStore(tmp_path).list_runs() == []

    started = runner.invoke(
        app,
        [
            "objectives",
            "start",
            objective_id,
            "--reason",
            "ready to dispatch",
            "--actor",
            "tester",
            "--project",
            str(tmp_path),
            "--output",
            "json",
        ],
    )
    dispatched = run_objective_autonomously(tmp_path, objective_id, autonomy_profile_id="safe-local")

    assert started.exit_code == 0, started.output
    started_payload = json.loads(started.output)
    assert started_payload["schema_version"] == "harness.objective_lifecycle/v1"
    assert started_payload["objective"]["status"] == "active"
    assert started_payload["objective"]["metadata"]["last_lifecycle_event"]["from_status"] == "created"
    assert started_payload["objective"]["metadata"]["last_lifecycle_event"]["to_status"] == "active"
    assert started_payload["operator_authority"]["execution_allowed"] is False
    assert dispatched.ok is True
    assert dispatched.stop_reason == "objective_succeeded"
    assert dispatched.adapter_dispatches == 1
    final = SQLiteStore(tmp_path)
    assert final.get_task(task.id).status.value == "succeeded"
    assert len(final.list_task_attempts(task.id)) == 1
    assert len(final.list_task_leases(task.id)) == 1
    assert len(final.list_runs()) == 1


def test_objective_run_cli_timeout_records_terminal_state_without_leases(tmp_path) -> None:
    store, objective = _init_project(tmp_path)
    task = _dry_task(store, "Ready dry run", objective.id)

    timed_out = runner.invoke(
        app,
        [
            "objectives",
            "run",
            objective.id,
            "--timeout-seconds",
            "0",
            "--project",
            str(tmp_path),
            "--output",
            "json",
        ],
    )

    assert timed_out.exit_code == 0, timed_out.output
    payload = json.loads(timed_out.output)
    assert payload["schema_version"] == "harness.autonomous_objective_run/v1"
    assert payload["ok"] is False
    assert payload["stop_reason"] == "timed_out"
    assert payload["adapter_dispatches"] == 0
    assert payload["steps"] == 0
    assert payload["pause_reasons"][0]["decision"] == "timed_out"
    fresh = SQLiteStore(tmp_path)
    assert fresh.get_objective(objective.id).status.value == "timed_out"
    assert fresh.get_task(task.id).status.value == "ready"
    assert fresh.list_task_attempts(task.id) == []
    assert fresh.list_task_leases(task.id) == []
    assert fresh.list_runs() == []
    events = _read_jsonl_records(tmp_path / HARNESS_DIR / "autonomy" / "objectives" / f"{objective.id}.jsonl")
    assert [event["event"] for event in events] == ["started", "stopped"]
    assert events[-1]["stop_reason"] == "timed_out"
    assert verify_objective_evidence(tmp_path, objective.id).ok is True


def test_objective_retry_cli_reactivates_timed_out_objective_without_dispatch(tmp_path) -> None:
    store, objective = _init_project(tmp_path)
    task = _dry_task(store, "Ready dry run", objective.id)
    store.update_objective_status(objective.id, ObjectiveStatus.TIMED_OUT, reason="deadline")

    retried = runner.invoke(
        app,
        [
            "objectives",
            "retry",
            objective.id,
            "--reason",
            "operator retry",
            "--actor",
            "tester",
            "--project",
            str(tmp_path),
            "--output",
            "json",
        ],
    )

    assert retried.exit_code == 0, retried.output
    payload = json.loads(retried.output)
    assert payload["schema_version"] == "harness.objective_retry/v1"
    assert payload["ok"] is True
    assert payload["objective"]["status"] == "active"
    assert payload["retried_task_count"] == 0
    assert payload["retried_task_ids"] == []
    assert payload["operator_authority"]["execution_allowed"] is False
    fresh = SQLiteStore(tmp_path)
    assert fresh.get_objective(objective.id).status.value == "active"
    assert fresh.get_task(task.id).status.value == "ready"
    assert fresh.list_task_attempts(task.id) == []
    assert fresh.list_task_leases(task.id) == []
    assert fresh.list_runs() == []


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


def test_objective_checkpoint_blocks_dispatch_until_approved(tmp_path) -> None:
    store, objective = _init_project(tmp_path)
    task = _dry_task(store, "Dry run", objective.id)
    checkpoint = create_objective_checkpoint(
        tmp_path,
        objective.id,
        label="Human review",
        reason="Review plan before dispatch",
    )
    assert SQLiteStore(tmp_path).get_objective(objective.id).status.value == "waiting_approval"

    blocked = run_objective_autonomously(tmp_path, objective.id, autonomy_profile_id="safe-local")
    gate = evaluate_objective_checkpoint_gate(tmp_path, objective.id)

    assert blocked.ok is False
    assert blocked.stop_reason == "checkpoint_blocked"
    assert blocked.adapter_dispatches == 0
    assert blocked.pause_reasons[0]["pending_checkpoint_ids"] == [checkpoint.checkpoint_id]
    assert gate.ok is False
    assert gate.pending_checkpoint_ids == [checkpoint.checkpoint_id]
    fresh = SQLiteStore(tmp_path)
    assert fresh.get_task(task.id).status.value == "ready"
    assert fresh.list_runs() == []
    assert fresh.list_task_leases() == []
    events = [json.loads(line) for line in blocked.evidence_path.read_text(encoding="utf-8").splitlines()]
    assert [event["event"] for event in events] == ["started", "checkpoint_blocked", "stopped"]
    assert verify_objective_evidence(tmp_path, objective.id).ok is True

    resolved = resolve_objective_checkpoint(
        tmp_path,
        objective.id,
        checkpoint.checkpoint_id,
        verdict="approved",
        approval_id="approval_manual_review",
        reason="Reviewed and approved",
    )
    assert resolved.status == "approved"
    assert SQLiteStore(tmp_path).get_objective(objective.id).status.value == "active"
    allowed = run_objective_autonomously(tmp_path, objective.id, autonomy_profile_id="safe-local")

    assert allowed.ok is True
    assert allowed.stop_reason == "objective_succeeded"
    assert allowed.adapter_dispatches == 1
    assert SQLiteStore(tmp_path).get_task(task.id).status.value == "succeeded"


def test_objective_checkpoints_cli_create_gate_and_approve(tmp_path) -> None:
    store, objective = _init_project(tmp_path)
    _dry_task(store, "Dry run", objective.id)

    created = runner.invoke(
        app,
        [
            "objectives",
            "checkpoints",
            "create",
            objective.id,
            "--label",
            "Manual checkpoint",
            "--reason",
            "review before run",
            "--project",
            str(tmp_path),
            "--output",
            "json",
        ],
    )

    assert created.exit_code == 0, created.output
    checkpoint = json.loads(created.output)["checkpoint"]
    assert checkpoint["schema_version"] == "harness.objective_checkpoint/v1"
    assert checkpoint["status"] == "pending"
    assert checkpoint["required"] is True
    assert SQLiteStore(tmp_path).get_objective(objective.id).status.value == "waiting_approval"

    blocked_gate = runner.invoke(
        app,
        ["objectives", "checkpoints", "gate", objective.id, "--project", str(tmp_path), "--output", "json"],
    )
    listed = runner.invoke(
        app,
        ["objectives", "checkpoints", "list", objective.id, "--project", str(tmp_path), "--output", "json"],
    )
    approved = runner.invoke(
        app,
        [
            "objectives",
            "checkpoints",
            "approve",
            objective.id,
            checkpoint["checkpoint_id"],
            "--approval-id",
            "approval_cli",
            "--reason",
            "approved",
            "--project",
            str(tmp_path),
            "--output",
            "json",
        ],
    )
    passing_gate = runner.invoke(
        app,
        ["objectives", "checkpoints", "gate", objective.id, "--project", str(tmp_path), "--output", "json"],
    )

    assert blocked_gate.exit_code == 1
    assert json.loads(blocked_gate.output)["schema_version"] == "harness.objective_checkpoint_gate/v1"
    assert listed.exit_code == 0, listed.output
    assert json.loads(listed.output)["schema_version"] == "harness.objective_checkpoints/v1"
    assert approved.exit_code == 0, approved.output
    assert json.loads(approved.output)["checkpoint"]["status"] == "approved"
    assert SQLiteStore(tmp_path).get_objective(objective.id).status.value == "active"
    assert passing_gate.exit_code == 0, passing_gate.output
    assert json.loads(passing_gate.output)["ok"] is True


def test_objective_checkpoint_evidence_tamper_blocks_gate_and_writes(tmp_path) -> None:
    store, objective = _init_project(tmp_path)
    _dry_task(store, "Dry run", objective.id)
    checkpoint = create_objective_checkpoint(
        tmp_path,
        objective.id,
        label="Manual checkpoint",
        reason="review before run",
    )
    resolve_objective_checkpoint(
        tmp_path,
        objective.id,
        checkpoint.checkpoint_id,
        verdict="approved",
        approval_id="approval_cli",
        reason="approved",
    )
    assert verify_objective_checkpoint_evidence(tmp_path, objective.id).ok is True

    evidence_path = tmp_path / HARNESS_DIR / "autonomy" / "objectives" / f"{objective.id}.checkpoints.jsonl"
    lines = [json.loads(line) for line in evidence_path.read_text(encoding="utf-8").splitlines()]
    lines[0]["label"] = "tampered checkpoint"
    evidence_path.write_text("\n".join(json.dumps(line, sort_keys=True) for line in lines) + "\n", encoding="utf-8")

    verification = verify_objective_checkpoint_evidence(tmp_path, objective.id)
    failed_check_ids = [check.id for check in verification.checks if check.status == "fail"]
    projection = list_objective_checkpoints(tmp_path, objective.id)
    gate = evaluate_objective_checkpoint_gate(tmp_path, objective.id)
    verify_cli = runner.invoke(
        app,
        ["objectives", "checkpoints", "verify", objective.id, "--project", str(tmp_path), "--output", "json"],
    )

    assert verification.ok is False
    assert "event_hash_chain" in failed_check_ids
    assert projection.ok is False
    assert projection.evidence_ok is False
    assert gate.ok is False
    assert gate.status == "blocked"
    assert gate.evidence_ok is False
    assert "event_hash_chain" in gate.evidence_failed_check_ids
    assert any("checkpoint evidence verification failed" in reason for reason in gate.reasons)
    assert verify_cli.exit_code == 1
    assert json.loads(verify_cli.output)["ok"] is False
    with pytest.raises(ValueError, match="checkpoint evidence verification failed"):
        create_objective_checkpoint(tmp_path, objective.id, label="Second checkpoint")


def test_objectives_verify_evidence_cli_outputs_json(tmp_path) -> None:
    store, objective = _init_project(tmp_path)
    _dry_task(store, "Dry run", objective.id)
    run_result = runner.invoke(
        app,
        ["objectives", "run", objective.id, "--project", str(tmp_path), "--autonomy", "safe-local", "--output", "json"],
    )
    assert run_result.exit_code == 0

    result = runner.invoke(
        app,
        ["objectives", "verify-evidence", objective.id, "--project", str(tmp_path), "--output", "json"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["schema_version"] == "harness.objective_evidence_verification/v1"
    assert payload["ok"] is True
    checks = {check["id"]: check for check in payload["checks"]}
    assert checks["event_payload_schema"]["status"] == "pass"
    assert checks["dispatch_links"]["status"] == "pass"
    assert checks["batch_plan_links"]["status"] == "pass"
    assert checks["batch_lifecycle"]["status"] == "pass"


def test_objective_evidence_reconciliation_records_existing_run_links(tmp_path) -> None:
    store, objective = _init_project(tmp_path)
    task = _dry_task(store, "Historical dry run", objective.id)
    run = store.create_run(
        "Historical run evidence",
        "phase_1a_test",
        status="succeeded",
        task_id=task.id,
        objective_id=objective.id,
    )
    paths = store.initialize_run_artifacts(run.id)
    paths["final_report"].write_text("historical report\n", encoding="utf-8")
    artifact = store.register_artifact(run.id, "final_report", paths["final_report"])
    evidence_path = tmp_path / ".harness" / "autonomy" / "objectives" / f"{objective.id}.jsonl"
    before = run_orchestration_readiness_audit(tmp_path, include_references=False)
    before_checks = {check.id: check for check in before.checks}

    dry_run = reconcile_objective_evidence(tmp_path, objective.id, dry_run=True)
    assert dry_run.status == "would_reconcile"
    assert dry_run.filesystem_modified is False
    assert dry_run.events_written == 3
    assert not evidence_path.exists()

    reconciled = reconcile_objective_evidence(tmp_path, objective.id, actor="test")
    verification = verify_objective_evidence(tmp_path, objective.id)
    after = run_orchestration_readiness_audit(tmp_path, include_references=False)
    after_checks = {check.id: check for check in after.checks}

    assert before_checks["append_only_objective_evidence"].status == "warning"
    assert before_checks["append_only_objective_evidence"].evidence["objectives_missing_evidence"] == [objective.id]
    assert evidence_path.exists()
    assert reconciled.ok is True
    assert reconciled.status == "reconciled"
    assert reconciled.run_ids == [run.id]
    assert reconciled.events_written == 3
    assert reconciled.filesystem_modified is True
    assert reconciled.existing_runs_mutated is False
    assert reconciled.tasks_mutated is False
    assert reconciled.artifacts_mutated is False
    assert reconciled.process_started is False
    assert reconciled.provider_called is False
    assert reconciled.network_called is False
    assert reconciled.permission_granting is False
    events = [json.loads(line) for line in evidence_path.read_text(encoding="utf-8").splitlines()]
    assert [event["event"] for event in events] == ["started", "reconciled_existing_run", "stopped"]
    assert events[1]["run_id"] == run.id
    assert events[1]["task_id"] == task.id
    assert events[1]["artifact_ids"] == [artifact.id]
    assert events[1]["run_event_count"] == 0
    assert events[2]["stop_reason"] == "reconciled_existing_evidence"
    assert events[2]["adapter_dispatches"] == 0
    assert events[2]["step_results"] == []
    assert verification.ok is True
    checks = {check.id: check for check in verification.checks}
    assert checks["event_payload_schema"].status == "pass"
    assert checks["reconciled_run_links"].status == "pass"
    assert checks["dispatch_links"].evidence["dispatch_count"] == 0
    assert after_checks["append_only_objective_evidence"].status == "pass"
    assert after_checks["append_only_objective_evidence"].evidence["objectives_missing_evidence"] == []


def test_objective_evidence_reconciliation_verifier_rejects_tampered_stopped_summary(tmp_path) -> None:
    store, objective = _init_project(tmp_path)
    task = _dry_task(store, "Historical dry run", objective.id)
    run = store.create_run(
        "Historical run evidence",
        "phase_1a_test",
        status="succeeded",
        task_id=task.id,
        objective_id=objective.id,
    )
    evidence_path = tmp_path / ".harness" / "autonomy" / "objectives" / f"{objective.id}.jsonl"

    result = reconcile_objective_evidence(tmp_path, objective.id)
    assert result.ok is True

    events = [json.loads(line) for line in evidence_path.read_text(encoding="utf-8").splitlines()]
    assert events[1]["run_id"] == run.id
    events[-1]["reconciled_run_ids"] = []
    events[-1]["reconciled_run_count"] = 0
    evidence_path.write_text("\n".join(json.dumps(event, sort_keys=True) for event in events) + "\n", encoding="utf-8")

    verification = verify_objective_evidence(tmp_path, objective.id)
    checks = {check.id: check for check in verification.checks}
    reasons = [issue["reason"] for issue in checks["stopped_summary"].evidence["issues"]]

    assert verification.ok is False
    assert checks["stopped_summary"].status == "fail"
    assert "reconciled_run_ids_mismatch" in reasons
    assert "reconciled_run_count_mismatch" in reasons


def test_objectives_reconcile_evidence_cli_outputs_json(tmp_path) -> None:
    store, objective = _init_project(tmp_path)
    task = _dry_task(store, "Historical dry run", objective.id)
    run = store.create_run(
        "Historical run evidence",
        "phase_1a_test",
        status="succeeded",
        task_id=task.id,
        objective_id=objective.id,
    )
    evidence_path = tmp_path / ".harness" / "autonomy" / "objectives" / f"{objective.id}.jsonl"

    preview = runner.invoke(
        app,
        ["objectives", "reconcile-evidence", objective.id, "--project", str(tmp_path), "--dry-run", "--output", "json"],
    )

    assert preview.exit_code == 0, preview.output
    preview_payload = json.loads(preview.output)
    assert preview_payload["status"] == "would_reconcile"
    assert preview_payload["filesystem_modified"] is False
    assert not evidence_path.exists()

    written = runner.invoke(
        app,
        ["objectives", "reconcile-evidence", objective.id, "--project", str(tmp_path), "--output", "json"],
    )
    existing = runner.invoke(
        app,
        ["objectives", "reconcile-evidence", objective.id, "--project", str(tmp_path), "--output", "json"],
    )

    assert written.exit_code == 0, written.output
    payload = json.loads(written.output)
    assert payload["schema_version"] == "harness.objective_evidence_reconciliation/v1"
    assert payload["ok"] is True
    assert payload["status"] == "reconciled"
    assert payload["run_ids"] == [run.id]
    assert payload["events_written"] == 3
    assert payload["verification_ok"] is True
    assert payload["filesystem_modified"] is True
    assert payload["tasks_mutated"] is False
    assert payload["run_records_mutated"] is False
    assert payload["permission_granting"] is False
    assert existing.exit_code == 0, existing.output
    existing_payload = json.loads(existing.output)
    assert existing_payload["status"] == "already_exists"
    assert existing_payload["events_written"] == 0
    assert existing_payload["verification_ok"] is True


def test_objectives_verify_evidence_cli_text_surfaces_event_chain(tmp_path) -> None:
    store, objective = _init_project(tmp_path)
    _dry_task(store, "Dry run", objective.id)
    run_result = runner.invoke(
        app,
        ["objectives", "run", objective.id, "--project", str(tmp_path), "--autonomy", "safe-local", "--output", "json"],
    )
    assert run_result.exit_code == 0

    result = runner.invoke(app, ["objectives", "verify-evidence", objective.id, "--project", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "Objective Evidence Verification" in result.output
    assert "Overall: pass" in result.output
    assert "Events: 4" in result.output
    assert "Evidence head sha256:" in result.output
    assert "Event payload schema: pass" in result.output
    assert "Event identity: pass" in result.output
    assert "Event hash chain: pass" in result.output
    assert "Event timestamps: pass" in result.output


def test_objectives_verify_evidence_cli_text_surfaces_event_chain_failure(tmp_path) -> None:
    store, objective = _init_project(tmp_path)
    _dry_task(store, "Dry run", objective.id)
    result = run_objective_autonomously(tmp_path, objective.id, autonomy_profile_id="safe-local")
    lines = []
    for raw_line in result.evidence_path.read_text(encoding="utf-8").splitlines():
        event = json.loads(raw_line)
        if event["event"] == "adapter_dispatched":
            event["artifact_ids"] = ["art_missing"]
        lines.append(json.dumps(event, sort_keys=True))
    result.evidence_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    cli_result = runner.invoke(app, ["objectives", "verify-evidence", objective.id, "--project", str(tmp_path)])

    assert cli_result.exit_code == 1
    assert "Overall: fail" in cli_result.output
    assert "Event hash chain: fail" in cli_result.output
    assert "fail\tevent_hash_chain" in cli_result.output
    assert "fail\tdispatch_links" in cli_result.output


def test_objectives_run_cli_outputs_parallel_json(tmp_path) -> None:
    store, objective = _init_project(tmp_path)
    _dry_task(store, "First", objective.id, priority=30)
    _dry_task(store, "Second", objective.id, priority=20)

    result = runner.invoke(
        app,
        [
            "objectives",
            "run",
            objective.id,
            "--project",
            str(tmp_path),
            "--autonomy",
            "safe-local",
            "--max-parallel",
            "2",
            "--output",
            "json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["schema_version"] == "harness.autonomous_objective_run/v1"
    assert payload["scheduler_mode"] == "bounded_parallel"
    assert payload["max_parallel"] == 2
    assert payload["batches"] == 1
    assert payload["adapter_dispatches"] == 2


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


def test_daemon_run_autonomous_cli_accepts_parallel_scheduler(tmp_path) -> None:
    store, objective = _init_project(tmp_path)
    _dry_task(store, "First", objective.id, priority=30)
    _dry_task(store, "Second", objective.id, priority=20)

    result = runner.invoke(
        app,
        [
            "daemon",
            "run-autonomous",
            "--project",
            str(tmp_path),
            "--autonomy",
            "safe-local",
            "--max-parallel",
            "2",
            "--output",
            "json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["objective_id"] == objective.id
    assert payload["scheduler_mode"] == "bounded_parallel"
    assert payload["max_parallel"] == 2
    assert payload["adapter_dispatches"] == 2
