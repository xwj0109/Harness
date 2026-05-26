import json

from typer.testing import CliRunner

from harness.chat import ChatSessionState, handle_chat_input
from harness.cli.main import app
from harness.execution import execute_lease
from harness.memory.sqlite_store import SQLiteStore
from harness.models import TaskStatus
from harness.operator_context import build_operator_context
from harness.progress import build_orchestration_progress


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
