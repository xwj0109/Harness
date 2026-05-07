import json

from harness.config import default_config
from harness.evals import run_safety_smoke
from harness.memory.sqlite_store import SQLiteStore
from harness.traces import export_run_trace, to_otel_json


def test_safety_smoke_passes_on_clean_initialized_project(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    run = store.create_run(goal="trace run", task_type="phase_1a_test")
    store.append_event(run.id, "info", "unit_event", "Unit event.", {"value": 1})

    result = run_safety_smoke(tmp_path, default_config(), store)

    assert result.schema_version == "harness.evals.safety_smoke/v1"
    assert result.ok is True
    assert {check.id for check in result.checks} == {
        "sandbox_network_disabled",
        "backend_boundaries",
        "artifact_evidence",
        "task_queue_non_execution",
        "manifest_policy_evidence",
    }
    serialized = json.dumps(result.model_dump(mode="json"))
    assert "api_key" not in serialized
    assert "OPENAI_API_KEY" not in serialized
    assert "base_url" not in serialized


def test_safety_smoke_fails_on_artifact_drift(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    run = store.create_run(goal="drift run", task_type="phase_1a_test")
    artifact_path = tmp_path / ".harness" / "runs" / run.id / "evidence.txt"
    artifact_path.write_text("initial", encoding="utf-8")
    artifact = store.register_artifact(run.id, "evidence", artifact_path)
    artifact_path.write_text("changed", encoding="utf-8")

    result = run_safety_smoke(tmp_path, default_config(), store)
    artifact_check = next(check for check in result.checks if check.id == "artifact_evidence")

    assert result.ok is False
    assert artifact_check.status == "fail"
    assert artifact.id in artifact_check.message
    assert "changed" not in json.dumps(result.model_dump(mode="json"))


def test_trace_export_links_run_event_artifact_policy_and_backend_metadata(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    run = store.create_run(
        goal="trace run",
        task_type="read_only_repo_summary",
        backend=default_config().backends["local_openai_compatible"],
    )
    store.append_event(run.id, "info", "trace_event", "Trace event.", {"secret": "OPENAI_API_KEY=sk-secret"})
    artifact_path = tmp_path / ".harness" / "runs" / run.id / "trace.txt"
    artifact_path.write_text("trace artifact body", encoding="utf-8")
    artifact = store.register_artifact(run.id, "trace_artifact", artifact_path)

    export = export_run_trace(tmp_path, store, run.id)
    payload = to_otel_json(export)
    serialized = json.dumps(payload)
    spans = payload["resourceSpans"][0]["scopeSpans"][0]["spans"]
    span_names = {span["name"] for span in spans}

    assert payload["schema_version"] == "harness.trace_export/v1"
    assert payload["ok"] is True
    assert payload["format"] == "otel-json"
    assert payload["run_id"] == run.id
    assert len(payload["trace_id"]) == 32
    assert "harness.run" in span_names
    assert "harness.policy" in span_names
    assert "harness.backend" in span_names
    assert "harness.event.trace_event" in span_names
    assert "harness.artifact.trace_artifact" in span_names
    assert artifact.sha256 in serialized
    assert "trace artifact body" not in serialized
    assert "sk-secret" not in serialized
    assert "OPENAI_API_KEY" not in serialized
    assert "api_key" not in serialized
    assert "base_url" not in serialized
