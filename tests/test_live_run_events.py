import json
from decimal import Decimal

from typer.testing import CliRunner

from harness.cli.main import app
from harness.memory.sqlite_store import SQLiteStore
from harness.models import EventVisibility, RedactionState, RunEventType, TokenUsageSnapshot

runner = CliRunner()


def test_live_run_events_have_stable_envelope_and_strict_sequence(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    run = store.create_run(goal="live run", task_type="codex_code_edit", status="running")

    first = store.append_run_event(
        run.id,
        RunEventType.RUN_STARTED,
        {"agent": "code_editor"},
        message="Run started.",
        trace_id="trace_test",
    )
    second = store.append_run_event(
        run.id,
        RunEventType.TOOL_CALL_STARTED,
        {"tool": "repo_read", "input_preview": {"path": "src/parser.py"}},
        message="Tool call started.",
        trace_id="trace_test",
    )

    assert first.seq == 1
    assert second.seq == 2

    events = store.list_events(run.id)
    assert [event.seq for event in events] == [1, 2]
    assert [event.event_type for event in events] == [
        RunEventType.RUN_STARTED.value,
        RunEventType.TOOL_CALL_STARTED.value,
    ]

    jsonl_path = tmp_path / ".harness" / "runs" / run.id / "events.jsonl"
    lines = [json.loads(line) for line in jsonl_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert [line["seq"] for line in lines] == [1, 2]
    assert lines[0]["schema_version"] == "harness.event/v1"
    assert lines[0]["event_id"].startswith("evt_")
    assert lines[0]["run_id"] == run.id
    assert lines[0]["task_id"] is None
    assert lines[0]["trace_id"] == "trace_test"
    assert lines[0]["type"] == "run.started"
    assert lines[0]["visibility"] == "user_visible"
    assert lines[0]["redaction_state"] == "redacted"
    assert lines[0]["payload"] == {"agent": "code_editor"}


def test_token_usage_event_persists_reasoning_tokens_as_count_only(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    run = store.create_run(goal="usage", task_type="codex_code_edit", status="running")

    usage = TokenUsageSnapshot(
        input_tokens=100,
        output_tokens=25,
        reasoning_tokens=9,
        cached_input_tokens=10,
        cache_write_tokens=4,
        total_tokens=134,
        estimated_cost_usd=Decimal("0.0123"),
    )
    event = store.append_token_usage_event(run.id, usage)

    assert event.event_type == RunEventType.TOKEN_USAGE_UPDATED.value
    assert event.redaction_state == RedactionState.NOT_REQUIRED
    assert event.visibility == EventVisibility.USER_VISIBLE
    assert event.payload == {
        "cached_input_tokens": 10,
        "cache_write_tokens": 4,
        "estimated_cost_usd": "0.0123",
        "input_tokens": 100,
        "output_tokens": 25,
        "reasoning_tokens": 9,
        "total_tokens": 134,
    }

    jsonl_path = tmp_path / ".harness" / "runs" / run.id / "events.jsonl"
    payload = json.loads(jsonl_path.read_text(encoding="utf-8").splitlines()[0])
    assert payload["type"] == "token_usage.updated"
    assert payload["redaction_state"] == "not_required"
    assert "reasoning" not in json.dumps(payload["payload"]).replace("reasoning_tokens", "")


def test_cli_events_and_runs_tail_replay_persisted_jsonl(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    run = store.create_run(goal="tail", task_type="codex_code_edit", status="completed")
    store.append_run_event(run.id, RunEventType.RUN_STARTED, {"agent": "code_editor"}, message="Run started.")
    store.append_run_event(run.id, RunEventType.RUN_FINISHED, {"status": "completed"}, message="Run finished.")

    events = runner.invoke(app, ["events", run.id, "--project", str(tmp_path), "--jsonl"])
    assert events.exit_code == 0, events.output
    event_lines = [json.loads(line) for line in events.output.splitlines()]
    assert [line["type"] for line in event_lines] == ["run.started", "run.finished"]
    assert [line["seq"] for line in event_lines] == [1, 2]

    tail_jsonl = runner.invoke(app, ["runs", "tail", run.id, "--project", str(tmp_path), "--jsonl"])
    assert tail_jsonl.exit_code == 0, tail_jsonl.output
    assert tail_jsonl.output == events.output

    tail_human = runner.invoke(app, ["runs", "tail", run.id, "--project", str(tmp_path)])
    assert tail_human.exit_code == 0, tail_human.output
    assert "1. ● Run started" in tail_human.output
    assert "2. ● Run finished" in tail_human.output
