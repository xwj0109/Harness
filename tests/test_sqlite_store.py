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
