import json

from typer.testing import CliRunner

from harness.chat import ChatSessionState, handle_chat_input
from harness.cli.main import app
from harness.execution import execute_lease
from harness.memory.sqlite_store import SQLiteStore
from harness.models import KillSwitchTargetKind, ObjectiveStatus, TaskStatus
from harness.objective_checkpoints import create_objective_checkpoint
import harness.objective_runner as objective_runner_module
from harness.objective_runner import run_objective_autonomously
from harness.operator_context import build_operator_context
from harness.progress import build_orchestration_progress
from harness.tui import _right_panel_progress_rows


runner = CliRunner()


def _store(tmp_path) -> SQLiteStore:
    store = SQLiteStore(tmp_path)
    store.initialize()
    return store


def _objective(store: SQLiteStore):
    return store.create_objective("Progress objective", metadata={"orchestrator_id": "coding_orchestrator"})


def _dry_run_task(store: SQLiteStore, objective_id: str, title: str = "Dry run task"):
    return store.create_task(
        title=title,
        objective_id=objective_id,
        metadata={"execution_adapter": "dry_run", "task_type": "phase_1a_test"},
    )


def test_progress_objective_with_no_tasks_is_idle(tmp_path) -> None:
    store = _store(tmp_path)
    objective = _objective(store)

    progress = build_orchestration_progress(tmp_path, objective.id)

    assert progress.schema_version == "harness.orchestration_progress/v1"
    assert progress.ok is True
    assert progress.mode.value == "idle"
    assert progress.tasks == []
    assert progress.next_action == f"Create tasks for objective {objective.id}."


def test_progress_ready_task_has_run_once_next_action(tmp_path) -> None:
    store = _store(tmp_path)
    objective = _objective(store)
    task = _dry_run_task(store, objective.id)

    progress = build_orchestration_progress(tmp_path, objective.id)

    assert progress.mode.value == "ready"
    assert "harness daemon run-once" in progress.next_action
    assert progress.tasks[0].task_id == task.id
    assert progress.tasks[0].status.value == "ready"
    assert progress.tasks[0].blocked_reasons == []


def test_progress_blocks_created_objective_with_start_action(tmp_path) -> None:
    store = _store(tmp_path)
    objective = store.create_objective(
        "Draft progress objective",
        metadata={"orchestrator_id": "coding_orchestrator"},
        status=ObjectiveStatus.CREATED,
    )
    task = _dry_run_task(store, objective.id)

    progress = build_orchestration_progress(tmp_path, objective.id)
    text = runner.invoke(app, ["progress", "--objective", objective.id, "--project", str(tmp_path)])

    assert progress.objective_status == ObjectiveStatus.CREATED
    assert progress.mode.value == "blocked"
    assert progress.tasks[0].task_id == task.id
    assert progress.tasks[0].status == TaskStatus.READY
    assert any("objective_created" in reason for reason in progress.blocked_reasons)
    assert any("objective_created" in reason for reason in progress.tasks[0].blocked_reasons)
    assert "harness objectives start" in progress.next_action
    assert "harness objectives start" in (progress.tasks[0].next_action or "")
    assert "harness daemon run-once" not in progress.next_action
    assert "harness daemon run-once" not in (progress.tasks[0].next_action or "")
    assert not any("harness daemon run-once" in command for command in progress.equivalent_commands)
    assert any("harness objectives start" in command for command in progress.equivalent_commands)
    assert text.exit_code == 0, text.output
    assert "Mode: blocked" in text.output


def test_progress_terminalizes_cancelled_objective_with_ready_tasks(tmp_path) -> None:
    store = _store(tmp_path)
    objective = _objective(store)
    task = _dry_run_task(store, objective.id)
    store.update_objective_status(objective.id, ObjectiveStatus.CANCELLED, reason="operator cancelled", actor="test")

    progress = build_orchestration_progress(tmp_path, objective.id)
    text = runner.invoke(app, ["progress", "--objective", objective.id, "--project", str(tmp_path)])

    assert progress.objective_status == ObjectiveStatus.CANCELLED
    assert progress.mode.value == "terminal"
    assert progress.tasks[0].task_id == task.id
    assert progress.tasks[0].status == TaskStatus.READY
    assert "harness objectives inspect" in progress.next_action
    assert "harness daemon run-once" not in progress.next_action
    assert "harness daemon run-once" not in (progress.tasks[0].next_action or "")
    assert not any("harness daemon run-once" in command for command in progress.equivalent_commands)
    assert text.exit_code == 0, text.output
    assert "Mode: terminal" in text.output


def test_progress_terminalizes_timed_out_objective_with_ready_tasks(tmp_path) -> None:
    store = _store(tmp_path)
    objective = _objective(store)
    task = _dry_run_task(store, objective.id)
    store.update_objective_status(objective.id, ObjectiveStatus.TIMED_OUT, reason="deadline exceeded", actor="test")

    progress = build_orchestration_progress(tmp_path, objective.id)

    assert progress.objective_status == ObjectiveStatus.TIMED_OUT
    assert progress.mode.value == "terminal"
    assert progress.tasks[0].task_id == task.id
    assert progress.tasks[0].status == TaskStatus.READY
    assert "harness objectives retry" in progress.next_action
    assert "harness objectives retry" in (progress.tasks[0].next_action or "")
    assert "harness daemon run-once" not in progress.next_action
    assert "harness daemon run-once" not in (progress.tasks[0].next_action or "")
    assert not any("harness daemon run-once" in command for command in progress.equivalent_commands)
    assert any("harness objectives retry" in command for command in progress.equivalent_commands)
    assert any("objective_timed_out" in reason for reason in progress.tasks[0].blocked_reasons)


def test_progress_blocks_retrying_objective_without_dispatch_action(tmp_path) -> None:
    store = _store(tmp_path)
    objective = _objective(store)
    task = _dry_run_task(store, objective.id)
    store.update_objective_status(objective.id, ObjectiveStatus.RETRYING, reason="retry in progress", actor="test")

    progress = build_orchestration_progress(tmp_path, objective.id)

    assert progress.objective_status == ObjectiveStatus.RETRYING
    assert progress.mode.value == "blocked"
    assert progress.tasks[0].task_id == task.id
    assert any("objective_retrying" in reason for reason in progress.blocked_reasons)
    assert any("objective_retrying" in reason for reason in progress.tasks[0].blocked_reasons)
    assert "harness objectives resume" in progress.next_action
    assert "harness daemon run-once" not in progress.next_action
    assert "harness daemon run-once" not in (progress.tasks[0].next_action or "")
    assert not any("harness daemon run-once" in command for command in progress.equivalent_commands)


def test_progress_blocks_suspended_objective_with_resume_action(tmp_path) -> None:
    store = _store(tmp_path)
    objective = _objective(store)
    task = _dry_run_task(store, objective.id)
    store.update_objective_status(objective.id, ObjectiveStatus.SUSPENDED, reason="operator paused", actor="test")

    progress = build_orchestration_progress(tmp_path, objective.id)
    text = runner.invoke(app, ["progress", "--objective", objective.id, "--project", str(tmp_path)])

    assert progress.objective_status == ObjectiveStatus.SUSPENDED
    assert progress.mode.value == "blocked"
    assert progress.tasks[0].task_id == task.id
    assert progress.tasks[0].status == TaskStatus.READY
    assert any("objective_suspended" in reason for reason in progress.blocked_reasons)
    assert any("objective_suspended" in reason for reason in progress.tasks[0].blocked_reasons)
    assert "harness objectives resume" in progress.next_action
    assert "harness objectives resume" in (progress.tasks[0].next_action or "")
    assert "harness daemon run-once" not in progress.next_action
    assert "harness daemon run-once" not in (progress.tasks[0].next_action or "")
    assert not any("harness daemon run-once" in command for command in progress.equivalent_commands)
    assert any("harness objectives resume" in command for command in progress.equivalent_commands)
    assert text.exit_code == 0, text.output
    assert "Mode: blocked" in text.output


def test_progress_surfaces_disabled_adapter_before_daemon_lease(tmp_path) -> None:
    store = _store(tmp_path)
    objective = _objective(store)
    task = _dry_run_task(store, objective.id)
    store.disable_execution_control(
        KillSwitchTargetKind.ADAPTER,
        "dry_run",
        reason="operator pause",
        actor="test",
    )
    before = (len(store.list_task_attempts(task.id)), len(store.list_task_leases(task.id)), len(store.list_runs()))

    progress = build_orchestration_progress(tmp_path, objective.id)
    text = runner.invoke(app, ["progress", "--objective", objective.id, "--project", str(tmp_path)])
    json_result = runner.invoke(
        app,
        ["progress", "--objective", objective.id, "--project", str(tmp_path), "--output", "json"],
    )
    context = build_operator_context(tmp_path)
    rows = _right_panel_progress_rows(context)
    after = (len(store.list_task_attempts(task.id)), len(store.list_task_leases(task.id)), len(store.list_runs()))

    assert progress.mode.value == "blocked"
    row = progress.tasks[0]
    assert row.task_id == task.id
    assert row.status == TaskStatus.READY
    assert row.blocked_reasons == ["control_disabled: adapter:dry_run. operator pause"]
    assert row.blocked_state_explanations[0].code.value == "disabled_adapter"
    assert row.blocked_state_explanations[0].inspect_command == (
        f"harness tasks inspect {task.id} --project {tmp_path} --output json"
    )
    assert "harness controls list" in row.next_action
    assert "harness tasks inspect" in row.next_action
    assert "harness controls list" in progress.next_action
    assert text.exit_code == 0, text.output
    assert "disabled_adapter" in text.output
    assert "control_disabled: adapter:dry_run. operator pause" in text.output
    assert json_result.exit_code == 0, json_result.output
    payload = json.loads(json_result.output)
    assert payload["mode"] == "blocked"
    assert payload["tasks"][0]["blocked_state_explanations"][0]["code"] == "disabled_adapter"
    assert context["progress"]["mode"] == "blocked"
    assert context["progress"]["tasks"][0]["blocked_state_explanations"][0]["code"] == "disabled_adapter"
    assert any("disabled_adapter" in row for row in rows)
    assert after == before


def test_progress_surfaces_open_adapter_breaker_before_daemon_lease(tmp_path) -> None:
    store = _store(tmp_path)
    objective = _objective(store)
    task = _dry_run_task(store, objective.id)
    daemon = store.ensure_daemon(owner="test")
    for index in range(3):
        store.record_daemon_event(
            daemon.id,
            event_type="execution_adapter_rejected",
            message="Adapter execution failed.",
            metadata={
                "adapter_id": "dry_run",
                "reason_code": "adapter_execution_failed",
                "error": f"failure {index}",
            },
        )
    before = (len(store.list_task_attempts(task.id)), len(store.list_task_leases(task.id)), len(store.list_runs()))

    progress = build_orchestration_progress(tmp_path, objective.id)
    text = runner.invoke(app, ["progress", "--objective", objective.id, "--project", str(tmp_path)])
    after = (len(store.list_task_attempts(task.id)), len(store.list_task_leases(task.id)), len(store.list_runs()))

    assert progress.mode.value == "blocked"
    row = progress.tasks[0]
    assert row.task_id == task.id
    assert row.status == TaskStatus.READY
    assert row.blocked_reasons == ["breaker_open: dry_run 3/3 failures in 900 seconds"]
    assert row.blocked_state_explanations[0].code.value == "breaker_open"
    assert "harness controls breaker-status" in row.next_action
    assert "harness controls breaker-status" in progress.next_action
    assert text.exit_code == 0, text.output
    assert "breaker_open" in text.output
    assert after == before


def test_progress_checkpoint_gate_blocks_ready_objective(tmp_path) -> None:
    store = _store(tmp_path)
    objective = _objective(store)
    _dry_run_task(store, objective.id)
    checkpoint = create_objective_checkpoint(
        tmp_path,
        objective.id,
        label="Supervisor checkpoint",
        reason="review before dispatch",
    )

    progress = build_orchestration_progress(tmp_path, objective.id)
    text = runner.invoke(app, ["progress", "--objective", objective.id, "--project", str(tmp_path)])
    json_result = runner.invoke(
        app,
        ["progress", "--objective", objective.id, "--project", str(tmp_path), "--output", "json"],
    )
    context = build_operator_context(tmp_path)

    assert progress.objective_status == ObjectiveStatus.WAITING_APPROVAL
    assert progress.mode.value == "blocked"
    assert progress.checkpoints is not None
    assert progress.checkpoints["status"] == "blocked"
    assert progress.checkpoints["pending_checkpoint_ids"] == [checkpoint.checkpoint_id]
    assert any("required objective checkpoints pending" in reason for reason in progress.blocked_reasons)
    assert any("objective_waiting_approval" in reason for reason in progress.tasks[0].blocked_reasons)
    assert "harness objectives checkpoints gate" in progress.next_action
    assert "harness daemon run-once" not in progress.next_action
    assert "harness daemon run-once" not in (progress.tasks[0].next_action or "")
    assert not any("harness daemon run-once" in command for command in progress.equivalent_commands)
    assert text.exit_code == 0, text.output
    assert "Checkpoint gate: blocked" in text.output
    assert json_result.exit_code == 0, json_result.output
    assert json.loads(json_result.output)["checkpoints"]["status"] == "blocked"
    assert context["progress"]["checkpoints"]["status"] == "blocked"


def test_progress_active_lease_reports_inspect_and_execute_commands(tmp_path) -> None:
    store = _store(tmp_path)
    objective = _objective(store)
    task = _dry_run_task(store, objective.id)
    selected = store.select_next_task_for_lease(owner="progress-test")
    lease = selected["lease"]

    progress = build_orchestration_progress(tmp_path, objective.id)

    assert progress.mode.value == "leased"
    assert progress.active_lease_ids == [lease.id]
    assert "harness daemon inspect-lease" in progress.next_action
    assert "harness daemon execute" in progress.next_action
    row = progress.tasks[0]
    assert row.task_id == task.id
    assert row.lease_id == lease.id
    assert row.attempt_id == selected["attempt"].id


def test_progress_dependency_blocker_is_reported(tmp_path) -> None:
    store = _store(tmp_path)
    objective = _objective(store)
    upstream = _dry_run_task(store, objective.id, "Upstream")
    downstream = store.create_task(
        title="Downstream",
        objective_id=objective.id,
        depends_on=[upstream.id],
        metadata={"execution_adapter": "dry_run", "task_type": "phase_1a_test"},
    )

    progress = build_orchestration_progress(tmp_path, objective.id)

    assert progress.mode.value == "blocked"
    blocked = next(task for task in progress.tasks if task.task_id == downstream.id)
    assert any(upstream.id in reason for reason in blocked.blocked_reasons)
    assert any("dependency" in reason for reason in progress.blocked_reasons)


def test_progress_missing_hosted_approval_blocks_without_creating_run(tmp_path) -> None:
    store = _store(tmp_path)
    objective = _objective(store)
    task = store.create_task(
        title="Read-only summary",
        objective_id=objective.id,
        metadata={"execution_adapter": "read_only_summary", "task_type": "read_only_repo_summary"},
    )
    run_count_before = len(store.list_runs())

    progress = build_orchestration_progress(tmp_path, objective.id)

    assert progress.mode.value == "blocked"
    row = progress.tasks[0]
    assert row.task_id == task.id
    assert any("hosted_provider_codex" in reason for reason in row.blocked_reasons)
    assert len(store.list_runs()) == run_count_before


def test_progress_terminal_succeeded_task_reports_linked_run(tmp_path) -> None:
    store = _store(tmp_path)
    objective = _objective(store)
    _dry_run_task(store, objective.id)
    lease = store.select_next_task_for_lease(owner="progress-test")["lease"]
    result = execute_lease(tmp_path, lease.id, owner="progress-test")
    assert result.ok is True

    progress = build_orchestration_progress(tmp_path, objective.id)

    assert progress.mode.value == "terminal"
    assert progress.tasks[0].status.value == "succeeded"
    assert progress.tasks[0].run_id == result.run.id
    assert progress.tasks[0].terminal_decision == f"linked_run:{result.run.id}"


def test_progress_terminal_objective_evidence_commands_when_jsonl_exists(tmp_path) -> None:
    store = _store(tmp_path)
    objective = _objective(store)
    _dry_run_task(store, objective.id)
    result = run_objective_autonomously(tmp_path, objective.id, autonomy_profile_id="safe-local")
    assert result.evidence_path.exists()

    progress = build_orchestration_progress(tmp_path, objective.id)

    verify_command = f"harness objectives verify-evidence {objective.id} --project {tmp_path} --output json"
    trace_command = f"harness traces export-objective {objective.id} --format otel-json --project {tmp_path} --output json"
    assert progress.mode.value == "terminal"
    assert progress.objective_evidence is not None
    assert progress.objective_evidence["ok"] is True
    assert progress.objective_evidence["event_count"] >= 4
    assert progress.objective_evidence["head_sha256"]
    assert progress.objective_evidence["check_statuses"]["event_payload_schema"] == "pass"
    assert progress.objective_evidence["check_statuses"]["event_hash_chain"] == "pass"
    assert verify_command in progress.next_action
    assert trace_command in progress.next_action
    assert verify_command in progress.equivalent_commands
    assert trace_command in progress.equivalent_commands

    text = runner.invoke(app, ["progress", "--objective", objective.id, "--project", str(tmp_path)])
    json_result = runner.invoke(
        app,
        ["progress", "--objective", objective.id, "--project", str(tmp_path), "--output", "json"],
    )
    context = build_operator_context(tmp_path)

    assert text.exit_code == 0, text.output
    assert "Objective evidence: pass" in text.output
    assert "Evidence events:" in text.output
    assert "harness objectives verify-evidence" in text.output
    assert "harness traces export-objective" in text.output
    assert json_result.exit_code == 0, json_result.output
    assert json.loads(json_result.output)["objective_evidence"]["ok"] is True
    assert context["progress"]["objective_evidence"]["ok"] is True


def test_progress_surfaces_lease_guard_stopped_objective_evidence(monkeypatch, tmp_path) -> None:
    store = _store(tmp_path)
    objective = _objective(store)
    task = _dry_run_task(store, objective.id, "Guarded dry run")
    store.disable_execution_control(
        KillSwitchTargetKind.ADAPTER,
        "dry_run",
        reason="operator pause",
        actor="test",
    )
    monkeypatch.setattr(objective_runner_module, "_kill_switch_active", lambda *_args, **_kwargs: False)

    result = run_objective_autonomously(tmp_path, objective.id, autonomy_profile_id="safe-local")
    progress = build_orchestration_progress(tmp_path, objective.id)
    text = runner.invoke(app, ["progress", "--objective", objective.id, "--project", str(tmp_path)])
    json_result = runner.invoke(
        app,
        ["progress", "--objective", objective.id, "--project", str(tmp_path), "--output", "json"],
    )
    context = build_operator_context(tmp_path)

    assert result.ok is False
    assert result.stop_reason == "control_disabled"
    assert progress.objective_evidence is not None
    assert progress.objective_evidence["ok"] is True
    assert progress.objective_evidence["event_type_counts"]["lease_guard_stopped"] == 1
    assert progress.objective_evidence["event_type_counts"].get("adapter_dispatched", 0) == 0
    assert progress.objective_evidence["lease_guard_stop_count"] == 1
    assert progress.objective_evidence["last_lease_guard_stop"]["task_id"] == task.id
    assert progress.objective_evidence["last_lease_guard_stop"]["lease_id"] is None
    assert progress.objective_evidence["last_lease_guard_stop"]["adapter_id"] == "dry_run"
    assert progress.objective_evidence["last_lease_guard_stop"]["stop_reason"] == "control_disabled"
    assert progress.objective_evidence["last_lease_guard_stop"]["guard_decision"] == "control_disabled"
    assert progress.objective_evidence["last_event_type"] == "stopped"
    assert text.exit_code == 0, text.output
    assert "Evidence event types:" in text.output
    assert "lease_guard_stopped=1" in text.output
    assert "Lease guard stop: control_disabled | adapter=dry_run" in text.output
    assert json_result.exit_code == 0, json_result.output
    assert json.loads(json_result.output)["objective_evidence"]["last_lease_guard_stop"]["task_id"] == task.id
    assert context["progress"]["objective_evidence"]["lease_guard_stop_count"] == 1
    fresh = SQLiteStore(tmp_path)
    assert fresh.list_runs() == []
    assert fresh.list_task_attempts(task.id) == []
    assert fresh.list_task_leases(task.id) == []


def test_progress_reports_tampered_objective_evidence_status_without_repair(tmp_path) -> None:
    store = _store(tmp_path)
    objective = _objective(store)
    _dry_run_task(store, objective.id)
    result = run_objective_autonomously(tmp_path, objective.id, autonomy_profile_id="safe-local")
    before = (len(store.list_tasks()), len(store.list_task_leases()), len(store.list_runs()))
    lines = []
    for raw_line in result.evidence_path.read_text(encoding="utf-8").splitlines():
        event = json.loads(raw_line)
        if event["event"] == "adapter_dispatched":
            event["artifact_ids"] = ["art_missing"]
        lines.append(json.dumps(event, sort_keys=True))
    result.evidence_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    progress = build_orchestration_progress(tmp_path, objective.id)
    text = runner.invoke(app, ["progress", "--objective", objective.id, "--project", str(tmp_path)])
    after = (len(store.list_tasks()), len(store.list_task_leases()), len(store.list_runs()))

    assert progress.objective_evidence is not None
    assert progress.objective_evidence["ok"] is False
    assert progress.objective_evidence["check_statuses"]["event_hash_chain"] == "fail"
    assert text.exit_code == 0, text.output
    assert "Objective evidence: fail" in text.output
    assert after == before


def test_progress_cli_json_and_unknown_objective_error(tmp_path) -> None:
    store = _store(tmp_path)
    objective = _objective(store)
    _dry_run_task(store, objective.id)

    result = runner.invoke(app, ["progress", "--objective", objective.id, "--project", str(tmp_path), "--output", "json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema_version"] == "harness.orchestration_progress/v1"
    assert payload["ok"] is True
    assert payload["objective_id"] == objective.id
    assert payload["mode"] == "ready"
    text = runner.invoke(app, ["progress", "--objective", objective.id, "--project", str(tmp_path)])
    assert text.exit_code == 0, text.output
    assert "task_id\tstatus\ttitle\tadapter" in text.output
    assert "Inspect" in text.output
    assert '"schema_version"' not in text.output

    missing = runner.invoke(app, ["progress", "--objective", "obj_missing", "--project", str(tmp_path), "--output", "json"])
    assert missing.exit_code != 0
    error = json.loads(missing.output)
    assert error["schema_version"] == "harness.orchestration_progress/v1"
    assert error["ok"] is False
    assert error["objective_id"] == "obj_missing"


def test_progress_command_is_read_only(tmp_path, monkeypatch) -> None:
    store = _store(tmp_path)
    objective = _objective(store)
    _dry_run_task(store, objective.id)
    before = (len(store.list_tasks()), len(store.list_task_leases()), len(store.list_runs()))

    monkeypatch.setattr(
        "harness.cli.main.CodexCliBackend",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("progress must not preflight Codex")),
    )
    monkeypatch.setattr(
        "harness.cli.main.LocalOpenAICompatibleBackend",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("progress must not preflight local backend")),
    )
    monkeypatch.setattr(
        "harness.cli.main.DockerImageManager",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("progress must not preflight Docker")),
    )

    result = runner.invoke(app, ["progress", "--objective", objective.id, "--project", str(tmp_path), "--output", "json"])

    assert result.exit_code == 0, result.output
    after = (len(store.list_tasks()), len(store.list_task_leases()), len(store.list_runs()))
    assert after == before


def test_progress_chat_aliases_render_objective_payload_without_backend_preflight(tmp_path, monkeypatch) -> None:
    store = _store(tmp_path)
    objective = _objective(store)
    _dry_run_task(store, objective.id)
    state = ChatSessionState(latest_objective_id=objective.id)
    before = (len(store.list_tasks()), len(store.list_task_leases()), len(store.list_runs()))
    monkeypatch.setattr(
        "harness.chat.execute_lease",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("progress chat must not execute")),
    )

    slash = handle_chat_input("/progress", tmp_path, state)
    natural = handle_chat_input("where are we", tmp_path, state)

    assert slash["ok"] is True
    assert slash["progress"]["schema_version"] == "harness.orchestration_progress/v1"
    assert slash["progress"]["objective_id"] == objective.id
    assert natural["ok"] is True
    assert natural["progress"]["mode"] == slash["progress"]["mode"]
    after = (len(store.list_tasks()), len(store.list_task_leases()), len(store.list_runs()))
    assert after == before


def test_operator_context_includes_progress_summary_without_backend_preflight(tmp_path, monkeypatch) -> None:
    store = _store(tmp_path)
    objective = _objective(store)
    task = _dry_run_task(store, objective.id)
    monkeypatch.setattr(
        "harness.operator_context.subprocess.run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(FileNotFoundError()),
    )

    context = build_operator_context(tmp_path)

    assert context["progress"]["schema_version"] == "harness.orchestration_progress_summary/v1"
    assert context["progress"]["objective_id"] == objective.id
    assert context["progress"]["mode"] == "ready"
    assert context["progress"]["tasks"][0]["task_id"] == task.id
