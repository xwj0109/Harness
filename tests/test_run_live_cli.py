import json

from typer.testing import CliRunner

from harness.cli.main import app
from harness.memory.sqlite_store import SQLiteStore

runner = CliRunner()


def test_run_live_task_file_creates_waiting_approval_live_run(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    task_file = tmp_path / "tasks" / "small_fix.md"
    task_file.parent.mkdir()
    task_file.write_text("Fix parser fallback handling.\n", encoding="utf-8")

    result = runner.invoke(
        app,
        ["run-live", "--task-file", "tasks/small_fix.md", "--agent", "code_editor", "--project", str(tmp_path)],
    )

    assert result.exit_code == 0, result.output
    assert "Run:" in result.output
    assert "Status: waiting_approval" in result.output
    assert "Approval required" in result.output
    run_id = result.output.split("Run: ", 1)[1].splitlines()[0]
    run_dir = tmp_path / ".harness" / "runs" / run_id
    for filename in ("events.jsonl", "transcript.jsonl", "procedure.md", "final_report.md", "manifest.json", "token_usage.json"):
        assert (run_dir / filename).exists()
    store = SQLiteStore(tmp_path)
    events = store.list_events(run_id)
    assert [event.seq for event in events] == [1, 2, 3]
    assert [event.event_type for event in events] == ["run.started", "policy.resolved", "approval.required"]
    assert store.get_run(run_id).status == "waiting_approval"


def test_run_live_jsonl_and_none_stream_formats(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    task_file = tmp_path / "fix.md"
    task_file.write_text("Fix it.\n", encoding="utf-8")

    jsonl = runner.invoke(app, ["run-live", "--task-file", str(task_file), "--project", str(tmp_path), "--stream", "jsonl"])
    assert jsonl.exit_code == 0, jsonl.output
    lines = [json.loads(line) for line in jsonl.output.splitlines()]
    assert [line["type"] for line in lines] == ["run.started", "policy.resolved", "approval.required"]

    none = runner.invoke(app, ["run-live", "--task-file", str(task_file), "--project", str(tmp_path), "--stream", "none"])
    assert none.exit_code == 0, none.output
    assert "Run:" in none.output
    assert "●" not in none.output


def test_tasks_run_live_binds_task_id_and_outputs_json(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    created = runner.invoke(
        app,
        [
            "tasks",
            "add",
            "--title",
            "Small fix",
            "--description",
            "Patch parser fallback.",
            "--execution-adapter",
            "codex_isolated_edit",
            "--task-type",
            "codex_code_edit",
            "--project",
            str(tmp_path),
            "--output",
            "json",
        ],
    )
    assert created.exit_code == 0, created.output
    task_id = json.loads(created.output)["task"]["id"]

    result = runner.invoke(
        app,
        ["tasks", "run", task_id, "--live", "--project", str(tmp_path), "--stream", "none", "--output", "json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema_version"] == "harness.task_run/v1"
    assert payload["ok"] is True
    run = SQLiteStore(tmp_path).get_run(payload["run_id"])
    assert run.task_id == task_id
    assert run.status == "waiting_approval"
