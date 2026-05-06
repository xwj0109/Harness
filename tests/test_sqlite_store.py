import json

from harness.config import default_config
from harness.memory.sqlite_store import SQLiteStore


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
