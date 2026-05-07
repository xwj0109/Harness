import json
import sqlite3

from harness.config import default_config
from harness.memory.sqlite_store import SQLiteStore
from harness.models import ObjectiveStatus, TaskStatus


def test_store_create_run_event_artifact_and_report(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    run = store.create_run(goal="test run", task_type="phase_1a_test")
    event = store.append_event(run.id, "info", "test", "hello", {"a": 1})
    paths = store.initialize_run_artifacts(run.id)
    artifact = store.register_artifact(run.id, "events", paths["events"])
    report = store.generate_final_report(run.id)

    assert store.get_run(run.id).goal == "test run"
    assert len(store.list_runs()) == 1
    assert store.list_events(run.id)[0].id == event.id
    assert store.list_artifacts(run.id)[0].id == artifact.id
    assert paths["events"].read_text(encoding="utf-8")
    assert report.exists()
    assert "Run " in report.read_text(encoding="utf-8")


def test_store_writes_and_refreshes_run_manifest(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    run = store.create_run(goal="test run", task_type="phase_1a_test")
    manifest_path = tmp_path / ".harness" / "runs" / run.id / "manifest.json"

    initial = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert initial["schema_version"] == "harness.manifest/v1"
    assert initial["run_id"] == run.id
    assert initial["goal"] == "test run"
    assert initial["task_type"] == "phase_1a_test"
    assert initial["run_mode"] == "dev"
    assert initial["status"] == "created"
    assert initial["project_root"] == str(tmp_path.resolve())
    assert initial["approval_id"] is None
    assert initial["backend_descriptor"] is None
    assert initial["artifacts"] == []

    paths = store.initialize_run_artifacts(run.id)
    store.register_artifact(run.id, "events", paths["events"], {"required": True})
    with_artifact = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert with_artifact["artifacts"] == [
        {
            "kind": "events",
            "path": str(paths["events"]),
            "created_at": with_artifact["artifacts"][0]["created_at"],
            "metadata": {"required": True},
        }
    ]

    store.update_run_status(run.id, "completed")
    completed = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert completed["status"] == "completed"
    assert completed["updated_at"] >= initial["updated_at"]


def test_store_manifest_includes_backend_descriptor_without_settings(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    backend = default_config().backends["local_openai_compatible"]
    run = store.create_run(
        goal="test run",
        task_type="read_only_repo_summary",
        backend=backend,
        approval_id="approval_123",
    )

    manifest_path = tmp_path / ".harness" / "runs" / run.id / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    descriptor = manifest["backend_descriptor"]

    assert manifest["run_mode"] == "read_only"
    assert manifest["approval_id"] == "approval_123"
    assert descriptor["name"] == "local_openai_compatible"
    assert descriptor["kind"] == "native_model"
    assert descriptor["metadata"]["data_boundary"] == "local_only"
    assert descriptor["capabilities"]["json_mode"] is True
    assert "settings" not in descriptor
    assert "base_url" not in json.dumps(descriptor)


def test_store_redacts_secret_like_event_payloads(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    run = store.create_run(goal="test run", task_type="phase_1a_test")
    secret = "OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz"
    store.append_event(run.id, "info", "secret_test", "payload redaction", {"secret": secret})
    events_jsonl = (tmp_path / ".harness" / "runs" / run.id / "events.jsonl").read_text(
        encoding="utf-8"
    )
    events = store.list_events(run.id)
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in events_jsonl
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in str(events[0].payload)
    assert "[REDACTED_SECRET]" in events_jsonl


def test_store_initializes_tasks_table_without_breaking_runs(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()

    with sqlite3.connect(tmp_path / ".harness" / "harness.sqlite") as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        task_columns = {row[1] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()}

    assert {
        "runs",
        "events",
        "artifacts",
        "backend_snapshots",
        "tasks",
        "objectives",
        "task_dependencies",
        "task_attempts",
        "task_leases",
        "task_transitions",
    } <= tables
    assert {
        "objective_id",
        "idempotency_key",
        "required_approvals_json",
        "approval_state",
    } <= task_columns
    run = store.create_run(goal="test run", task_type="phase_1a_test")
    assert store.get_run(run.id).id == run.id


def test_store_creates_lists_filters_and_updates_tasks(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()

    low = store.create_task(title="Low priority", priority=0, agent_id="repo_inspector")
    high = store.create_task(title="High priority", description="Important", priority=5, workbench_id="coding")

    assert store.get_task(high.id).description == "Important"
    assert store.get_task(high.id).status == TaskStatus.READY
    assert store.get_task(high.id).idempotency_key is not None
    assert store.get_task(high.id).idempotency_key.startswith("task_idem_")
    assert [task.id for task in store.list_tasks()] == [high.id, low.id]
    assert [task.id for task in store.list_tasks(status="ready")] == [high.id, low.id]
    assert [task.id for task in store.list_tasks(status="queued")] == [high.id, low.id]

    succeeded = store.update_task_status(low.id, TaskStatus.SUCCEEDED)
    assert succeeded.status == TaskStatus.SUCCEEDED
    assert [task.id for task in store.list_tasks(status="ready")] == [high.id]
    assert [task.id for task in store.list_tasks(status="queued")] == [high.id]


def test_store_creates_lists_and_gets_objectives(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()

    low = store.create_objective(title="Low priority", priority=0, workbench_id="coding")
    high = store.create_objective(
        title="High priority",
        description="Important",
        priority=5,
        workbench_id="quant",
        metadata={"secret": "OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz"},
    )

    loaded = store.get_objective(high.id)
    assert loaded.id == high.id
    assert loaded.title == "High priority"
    assert loaded.description == "Important"
    assert loaded.status == ObjectiveStatus.ACTIVE
    assert loaded.workbench_id == "quant"
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in json.dumps(loaded.metadata)
    assert "[REDACTED_SECRET]" in json.dumps(loaded.metadata)
    assert [objective.id for objective in store.list_objectives()] == [high.id, low.id]

    try:
        store.get_objective("obj_missing")
    except KeyError as exc:
        assert str(exc).strip("'") == "Objective not found: obj_missing"
    else:
        raise AssertionError("missing objective should raise")


def test_store_normalizes_legacy_task_statuses(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    timestamp = "2026-01-01T00:00:00+00:00"

    with store.connect() as conn:
        for task_id, status in [
            ("task_queued", "queued"),
            ("task_completed", "completed"),
            ("task_canceled", "canceled"),
        ]:
            conn.execute(
                """
                INSERT INTO tasks (
                  id, title, description, status, project_root, created_at, updated_at,
                  priority, depends_on_json, metadata_json, idempotency_key,
                  required_approvals_json
                ) VALUES (?, ?, '', ?, ?, ?, ?, 0, '[]', '{}', ?, '[]')
                """,
                (task_id, task_id, status, str(tmp_path), timestamp, timestamp, f"idem_{task_id}"),
            )

    assert store.get_task("task_queued").status == TaskStatus.READY
    assert store.get_task("task_completed").status == TaskStatus.SUCCEEDED
    assert store.get_task("task_canceled").status == TaskStatus.CANCELLED
    assert [task.id for task in store.list_tasks(status="queued")] == ["task_queued"]
    assert [task.id for task in store.list_tasks(status="completed")] == ["task_completed"]
    assert [task.id for task in store.list_tasks(status="canceled")] == ["task_canceled"]


def test_store_task_transitions_are_validated_and_recorded(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    task = store.create_task(title="Task")

    created_transitions = store.list_task_transitions(task.id)
    assert len(created_transitions) == 1
    assert created_transitions[0].from_status is None
    assert created_transitions[0].to_status == TaskStatus.READY
    assert created_transitions[0].reason == "task_created"

    updated = store.update_task_status(task.id, TaskStatus.SUCCEEDED)
    assert updated.status == TaskStatus.SUCCEEDED
    transitions = store.list_task_transitions(task.id)
    assert len(transitions) == 2
    assert transitions[1].from_status == TaskStatus.READY
    assert transitions[1].to_status == TaskStatus.SUCCEEDED
    assert transitions[1].reason == "status_updated"

    try:
        store.update_task_status(task.id, TaskStatus.READY)
    except ValueError as exc:
        assert "Invalid task transition: succeeded -> ready" in str(exc)
    else:
        raise AssertionError("invalid transition should raise")


def test_store_task_errors_are_stable_for_unknown_or_invalid_status(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    task = store.create_task(title="Task")

    try:
        store.get_task("task_missing")
    except KeyError as exc:
        assert str(exc).strip("'") == "Task not found: task_missing"
    else:
        raise AssertionError("missing task should raise")

    try:
        store.update_task_status(task.id, "invalid")
    except ValueError as exc:
        assert "invalid" in str(exc)
    else:
        raise AssertionError("invalid status should raise")


def test_store_select_next_task_uses_priority_and_dependencies(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    dependency = store.create_task(title="Dependency", priority=10)
    blocked_by_dependency = store.create_task(
        title="Blocked by dependency",
        priority=100,
        depends_on=[dependency.id],
    )
    runnable = store.create_task(title="Runnable", priority=1)
    cancelled = store.create_task(title="Canceled", priority=200)
    store.update_task_status(cancelled.id, TaskStatus.CANCELLED)

    selected = store.select_next_task()

    assert selected is not None
    assert selected.id == dependency.id
    assert selected.status == TaskStatus.RUNNING
    assert store.get_task(blocked_by_dependency.id).status == TaskStatus.READY

    store.update_task_status(dependency.id, TaskStatus.SUCCEEDED)
    selected_after_dependency = store.select_next_task()

    assert selected_after_dependency is not None
    assert selected_after_dependency.id == blocked_by_dependency.id
    assert store.get_task(runnable.id).status == TaskStatus.READY
