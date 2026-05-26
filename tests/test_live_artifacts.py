import json

from typer.testing import CliRunner

from harness.cli.main import app
from harness.live_artifacts import write_live_run_artifacts
from harness.memory.sqlite_store import SQLiteStore
from harness.models import RunEventType, TokenUsageSnapshot

runner = CliRunner()


def _live_run(store: SQLiteStore) -> str:
    run = store.create_run(goal="Fix parser fallback handling", task_type="codex_code_edit", status="completed")
    store.append_run_event(run.id, RunEventType.RUN_STARTED, {"agent": "code_editor"}, message="Run started.")
    store.append_run_event(
        run.id,
        RunEventType.REASONING_SUMMARY_DELTA,
        {"delta": "Inspect failing parser tests, then patch the smallest affected module."},
        message="Reasoning summary updated.",
    )
    store.append_run_event(run.id, RunEventType.FILE_WRITE, {"path": "src/parser.py"}, message="File modified.")
    store.append_run_event(run.id, RunEventType.DIFF_UPDATED, {"added": 12, "removed": 4}, message="Diff updated.")
    store.append_run_event(run.id, RunEventType.TEST_STARTED, {"command": "pytest -q"}, message="Tests started.")
    store.append_run_event(run.id, RunEventType.TEST_FINISHED, {"status": "passed"}, message="Tests passed.")
    store.append_token_usage_event(
        run.id,
        TokenUsageSnapshot(
            input_tokens=100,
            output_tokens=50,
            reasoning_tokens=20,
            cache_read_tokens=12,
            cache_write_tokens=3,
            total_tokens=170,
        ),
    )
    store.append_run_event(run.id, RunEventType.RUN_FINISHED, {"status": "completed"}, message="Run finished.")
    return run.id


def test_live_run_artifacts_are_written_registered_and_manifested(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    run_id = _live_run(store)

    paths = write_live_run_artifacts(store, run_id)

    assert paths["transcript"].exists()
    assert paths["procedure"].exists()
    assert paths["final_report"].exists()
    assert paths["token_usage"].exists()

    transcript_lines = [json.loads(line) for line in paths["transcript"].read_text(encoding="utf-8").splitlines()]
    assert transcript_lines[0]["schema_version"] == "harness.transcript/v1"
    assert transcript_lines[0]["type"] == "run.started"
    assert all("sk-" not in json.dumps(line) for line in transcript_lines)

    procedure = paths["procedure"].read_text(encoding="utf-8")
    assert "# Live Procedure" in procedure
    assert "● Diff ready (+12 -4 lines)" in procedure
    assert "● Tests finished: passed" in procedure

    usage = json.loads(paths["token_usage"].read_text(encoding="utf-8"))
    assert usage == {
        "cache_read_tokens": 12,
        "cache_write_tokens": 3,
        "input_tokens": 100,
        "output_tokens": 50,
        "reasoning_tokens": 20,
        "total_tokens": 170,
    }

    report = paths["final_report"].read_text(encoding="utf-8")
    assert "# Run Summary" in report
    assert "## Reasoning summary" in report
    assert "Inspect failing parser tests" in report
    assert "src/parser.py" in report
    assert "reasoning_tokens" in report
    assert "raw chain-of-thought" not in report.lower()

    artifacts = store.list_artifacts(run_id)
    kinds = {artifact.kind for artifact in artifacts}
    assert {"events", "transcript", "procedure", "final_report", "token_usage", "manifest"} <= kinds
    assert all(artifact.sha256 for artifact in artifacts)
    assert all(artifact.size_bytes is not None for artifact in artifacts)

    manifest = store.build_run_manifest(run_id)
    assert {"events", "transcript", "procedure", "final_report", "token_usage", "manifest"} <= {
        artifact.kind for artifact in manifest.artifacts
    }


def test_cli_transcript_and_summary_commands_render_live_artifacts(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    run_id = _live_run(store)

    transcript = runner.invoke(app, ["transcript", run_id, "--project", str(tmp_path)])
    assert transcript.exit_code == 0, transcript.output
    assert "# Live Procedure" in transcript.output
    assert "● Run finished" in transcript.output

    transcript_jsonl = runner.invoke(app, ["transcript", run_id, "--project", str(tmp_path), "--format", "jsonl"])
    assert transcript_jsonl.exit_code == 0, transcript_jsonl.output
    assert json.loads(transcript_jsonl.output.splitlines()[0])["schema_version"] == "harness.transcript/v1"

    summary = runner.invoke(app, ["summary", run_id, "--project", str(tmp_path)])
    assert summary.exit_code == 0, summary.output
    assert "# Run Summary" in summary.output
    assert "## Token usage" in summary.output
