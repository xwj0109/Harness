from __future__ import annotations

import json

from typer.testing import CliRunner

from harness.cli.main import app
from harness.intent_router import route_instruction
from harness.memory.sqlite_store import SQLiteStore
from harness.models import SessionStatus
from harness.session_events import SessionEventKind, append_session_event, read_session_events


runner = CliRunner()


def test_session_create_attach_and_transcript_round_trip(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    session = store.create_session(workbench_id="coding", agent_id="repo_inspector", intent="explain_repo")
    objective = store.create_objective("Explain repo", workbench_id="coding", session_id=session.id)
    task = store.create_task("Inspect", objective_id=objective.id, session_id=session.id)
    run = store.create_run("Explain", "read_only_repo_summary", task_id=task.id, objective_id=objective.id, session_id=session.id)

    store.append_event(run.id, "info", "test", "event")
    artifact = store.register_artifact(run.id, "final_report", store.initialize_run_artifacts(run.id)["final_report"])
    append_session_event(
        tmp_path,
        session_id=session.id,
        run_id=run.id,
        task_id=task.id,
        objective_id=objective.id,
        event_type=SessionEventKind.REPORT_READY,
        message="Report ready",
    )

    loaded = store.get_session(session.id)
    assert loaded.objective_id == objective.id
    assert loaded.active_task_id == task.id
    assert loaded.active_run_id == run.id
    assert store.get_run(run.id).session_id == session.id
    assert store.get_task(task.id).session_id == session.id
    assert store.get_objective(objective.id).session_id == session.id
    assert store.list_events(run.id)[0].session_id == session.id
    assert artifact.session_id == session.id
    assert read_session_events(tmp_path, session.id)[0].event_type == SessionEventKind.REPORT_READY


def test_intent_router_routes_product_flows() -> None:
    fix = route_instruction("fix the failing tests")
    explain = route_instruction("summarize this repo")
    plan = route_instruction("plan how to improve the CLI")
    unsupported = route_instruction("deploy this to production now")

    assert fix.intent == "fix_tests"
    assert fix.agent_id == "code_editor"
    assert fix.task_type == "codex_code_edit"
    assert "apply_back_separate" in fix.required_approvals
    assert explain.intent == "explain_repo"
    assert explain.task_type == "read_only_repo_summary"
    assert plan.intent == "plan_change"
    assert plan.task_type == "repo_planning"
    assert unsupported.intent == "unsupported"


def test_route_cli_outputs_expected_contract(tmp_path) -> None:
    result = runner.invoke(app, ["route", "fix the failing tests", "--project", str(tmp_path), "--output", "json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema_version"] == "harness.route/v1"
    assert payload["route"]["intent"] == "fix_tests"
    assert "diff.patch" in payload["route"]["expected_outputs"]


def test_run_auto_creates_waiting_session_without_hosted_approval(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0

    result = runner.invoke(app, ["run", "fix the failing tests", "--project", str(tmp_path), "--output", "json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema_version"] == "harness.product_session/v1"
    assert payload["status"] == SessionStatus.WAITING_APPROVAL.value
    assert payload["route"]["intent"] == "fix_tests"
    assert payload["session"]["objective_id"]
    assert payload["session"]["active_task_id"]
    event_types = {event["event_type"] for event in payload["events"]}
    assert "intent.routed" in event_types
    assert "approval.required" in event_types
    transcript = tmp_path / ".harness" / "sessions" / payload["session"]["id"] / "transcript.jsonl"
    assert transcript.exists()
    assert "approval.required" in transcript.read_text(encoding="utf-8")


def test_run_auto_migrates_existing_initialized_database(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    with store_without_session_columns(tmp_path) as conn:
        conn.execute("DROP TABLE sessions")

    result = runner.invoke(app, ["run", "fix the failing tests", "--project", str(tmp_path), "--output", "json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == SessionStatus.WAITING_APPROVAL.value
    assert payload["session"]["id"].startswith("sess_")


def test_sessions_list_inspect_and_reject_decision(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    session = store.create_session(intent="explain_repo")
    run = store.create_run("Explain", "read_only_repo_summary", session_id=session.id)

    listed = runner.invoke(app, ["sessions", "list", "--project", str(tmp_path), "--output", "json"])
    inspected = runner.invoke(app, ["sessions", "inspect", session.id, "--project", str(tmp_path), "--output", "json"])
    rejected = runner.invoke(app, ["reject", run.id, "--project", str(tmp_path), "--output", "json"])

    assert listed.exit_code == 0, listed.output
    assert inspected.exit_code == 0, inspected.output
    assert rejected.exit_code == 0, rejected.output
    assert json.loads(listed.output)["sessions"][0]["id"] == session.id
    assert json.loads(inspected.output)["transcript_path"].endswith("transcript.jsonl")
    reject_payload = json.loads(rejected.output)
    assert reject_payload["decision"] == "rejected"
    assert (tmp_path / ".harness" / "runs" / run.id / "apply_back.json").exists()


def store_without_session_columns(tmp_path):
    import sqlite3

    return sqlite3.connect(tmp_path / ".harness" / "harness.sqlite")
