from __future__ import annotations

import json
import subprocess
from pathlib import Path

from typer.testing import CliRunner

from harness.cli.main import app
from harness.backends.codex_cli import CodexRunResult
from harness.intent_router import route_instruction
from harness.memory.sqlite_store import SQLiteStore
from harness.models import (
    EventStreamType,
    SessionMessageRole,
    SessionMutationReversibility,
    SessionPartKind,
    SessionStatus,
)
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


def test_session_spine_persists_messages_parts_events_and_archive_export(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    session = store.create_session(title="Spine", intent="plan_change", raw_model_ref="provider/model")
    message = store.append_session_message(session.id, SessionMessageRole.USER, "Plan this change")
    part = store.append_session_part(session.id, message.id, SessionPartKind.TEXT, text="Plan this change")
    run = store.create_run("Direct edit", "codex_code_edit", session_id=session.id)
    assistant = store.append_session_message(
        session.id,
        SessionMessageRole.ASSISTANT,
        "Changed active workspace",
        run_id=run.id,
        mutation_reversibility=SessionMutationReversibility.NOT_REVERSIBLE_ACTIVE_WORKSPACE,
    )

    events = store.list_store_events(EventStreamType.SESSION, session.id)
    assert [event.kind for event in events][:3] == [
        "session.created",
        "session.message.appended",
        "session.part.appended",
    ]
    assert store.list_session_messages(session.id)[0].id == message.id
    assert store.list_session_parts(session.id)[0].id == part.id
    assert store.list_session_messages(session.id)[1].id == assistant.id
    assert store.list_session_messages(session.id)[1].mutation_reversibility == (
        SessionMutationReversibility.NOT_REVERSIBLE_ACTIVE_WORKSPACE
    )

    archived = runner.invoke(app, ["session", "archive", session.id, "--project", str(tmp_path), "--output", "json"])
    exported = runner.invoke(app, ["session", "export", session.id, "--project", str(tmp_path), "--output", "json"])

    assert archived.exit_code == 0, archived.output
    assert exported.exit_code == 0, exported.output
    archived_payload = json.loads(archived.output)
    export_payload = json.loads(exported.output)
    assert archived_payload["session"]["status"] == SessionStatus.ARCHIVED.value
    assert export_payload["include_artifacts"] is False
    assert [exported_message["id"] for exported_message in export_payload["messages"]] == [message.id, assistant.id]
    assert "session.archived" in {event["kind"] for event in export_payload["events"]}


def test_session_fork_cli_creates_child_session(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    parent = store.create_session(title="Parent")
    message = store.append_session_message(parent.id, SessionMessageRole.USER, "Fork here")

    result = runner.invoke(
        app,
        [
            "session",
            "fork",
            parent.id,
            "--message",
            message.id,
            "--title",
            "Child",
            "--project",
            str(tmp_path),
            "--output",
            "json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    child = payload["session"]
    assert child["parent_session_id"] == parent.id
    assert child["forked_from_message_id"] == message.id
    assert child["title"] == "Child"
    assert "session.forked" in {event.kind for event in store.list_store_events(EventStreamType.SESSION, child["id"])}


def test_session_tail_and_transcript_render_from_persisted_events(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    session = store.create_session(title="Timeline")
    message = store.append_session_message(session.id, SessionMessageRole.USER, "Inspect this file")
    store.append_session_part(session.id, message.id, SessionPartKind.TEXT, text="Inspect this file")
    assistant = store.append_session_message(session.id, SessionMessageRole.ASSISTANT, "Done")
    store.append_session_part(session.id, assistant.id, SessionPartKind.SUMMARY, text="Done")

    tail = runner.invoke(app, ["session", "tail", session.id, "--project", str(tmp_path)])
    tail_jsonl = runner.invoke(app, ["session", "tail", session.id, "--project", str(tmp_path), "--jsonl"])
    transcript = runner.invoke(app, ["session", "transcript", session.id, "--project", str(tmp_path)])
    transcript_jsonl = runner.invoke(
        app,
        ["session", "transcript", session.id, "--project", str(tmp_path), "--format", "jsonl"],
    )

    assert tail.exit_code == 0, tail.output
    assert tail_jsonl.exit_code == 0, tail_jsonl.output
    assert transcript.exit_code == 0, transcript.output
    assert transcript_jsonl.exit_code == 0, transcript_jsonl.output
    assert "Session created" in tail.output
    assert "Message appended" in tail.output
    first_event = json.loads(tail_jsonl.output.splitlines()[0])
    assert first_event["schema_version"] == "harness.event/v2"
    assert "\x1b[" not in tail_jsonl.output
    assert f"user {message.id}" in transcript.output
    assert "Inspect this file" in transcript.output
    first_entry = json.loads(transcript_jsonl.output.splitlines()[0])
    assert first_entry["schema_version"] == "harness.session_transcript_entry/v1"
    assert first_entry["message"]["id"] == message.id
    assert "\x1b[" not in transcript_jsonl.output


def test_foreground_prompt_creates_session_and_records_direct_run_messages(tmp_path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=tmp_path, check=True, capture_output=True)

    class FakeCodexBackend:
        def __init__(self, config):
            self.config = config
            self.name = config.name

        def preflight(self):
            from harness.models import BackendStatus

            return BackendStatus(available=True, metadata=self.config.metadata, capabilities=self.config.capabilities)

        def run_direct_agent(self, project_root, prompt, final_message_path, *, model=None, reasoning_effort=None):
            assert prompt == "change value"
            assert model == "codex/gpt-test@fast"
            (Path(project_root) / "app.py").write_text("value = 2\n", encoding="utf-8")
            final_message_path.write_text("Changed the value.", encoding="utf-8")
            self.config.settings["last_codex_approval_mode"] = "on-request via --ask-for-approval"
            self.config.settings["last_codex_sandbox_mode"] = "workspace-write"
            self.config.settings["last_apply_back_approval_required"] = False
            return (
                CodexRunResult(["codex", "exec", prompt], "", "", 0, [], "Changed the value."),
                self.config.capabilities,
                "",
            )

    monkeypatch.setattr("harness.cli.main.CodexCliBackend", FakeCodexBackend)
    result = runner.invoke(
        app,
        [
            "change value",
            "--project",
            str(tmp_path),
            "--title",
            "Direct session",
            "--agent",
            "build",
            "--mode",
            "direct",
            "--model",
            "codex/gpt-test@fast",
            "--file",
            "app.py",
            "--no-stream",
            "--output",
            "json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    session_id = payload["session_id"]
    store = SQLiteStore(tmp_path)
    session = store.get_session(session_id)
    messages = store.list_session_messages(session_id)
    parts = store.list_session_parts(session_id)
    assert payload["run_id"] == session.active_run_id
    assert session.title == "Direct session"
    assert session.agent_id == "build"
    assert session.provider_id == "codex"
    assert session.model_id == "gpt-test"
    assert session.model_variant == "fast"
    assert [message.role for message in messages] == [SessionMessageRole.USER, SessionMessageRole.ASSISTANT]
    assert messages[1].mutation_reversibility == SessionMutationReversibility.NOT_REVERSIBLE_ACTIVE_WORKSPACE
    assert any(part.metadata.get("attachment_kind") == "file_ref" for part in parts)
    artifact_parts = [part for part in parts if part.kind == SessionPartKind.ARTIFACT_REF and part.artifact_id]
    assert artifact_parts
    assert any(part.metadata.get("kind") == "codex_final_message" for part in artifact_parts)
    session_event_kinds = {event.kind for event in store.list_session_store_events(session_id)}
    assert "artifact.registered" in session_event_kinds
    transcript = runner.invoke(app, ["session", "transcript", session_id, "--project", str(tmp_path)])
    assert transcript.exit_code == 0, transcript.output
    assert "artifact=" in transcript.output


def test_foreground_build_agent_creates_isolated_task_by_default(tmp_path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0

    class ExplodingCodexBackend:
        def __init__(self, config):
            raise AssertionError("build agent should not use direct active-workspace Codex by default")

    monkeypatch.setattr("harness.cli.main.CodexCliBackend", ExplodingCodexBackend)
    result = runner.invoke(
        app,
        [
            "change value",
            "--project",
            str(tmp_path),
            "--agent",
            "build",
            "--title",
            "Build safely",
            "--output",
            "json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    session = payload["session"]
    task = payload["task"]
    assert payload["schema_version"] == "harness.native_agent_session/v1"
    assert session["agent_id"] == "build"
    assert session["status"] == SessionStatus.WAITING_APPROVAL.value
    assert session["active_task_id"] == task["id"]
    assert task["agent_id"] == "build"
    assert task["metadata"]["execution_adapter"] == "codex_isolated_edit"
    assert task["metadata"]["task_type"] == "codex_code_edit"
    assert task["metadata"]["direct_active_workspace"] is False
    assert task["required_approvals"] == ["hosted_provider_codex"]

    store = SQLiteStore(tmp_path)
    messages = store.list_session_messages(session["id"])
    assert [message.role for message in messages] == [SessionMessageRole.USER]
    event_kinds = [event.kind for event in store.list_session_store_events(session["id"])]
    assert "agent.selected" in event_kinds
    assert "run.blocked" in event_kinds


def test_foreground_plan_mention_creates_read_only_session_task(tmp_path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0

    class ExplodingCodexBackend:
        def __init__(self, config):
            raise AssertionError("plan agent should not call Codex directly")

    monkeypatch.setattr("harness.cli.main.CodexCliBackend", ExplodingCodexBackend)
    result = runner.invoke(app, ["@plan inspect auth flow", "--project", str(tmp_path), "--output", "json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    session = payload["session"]
    task = payload["task"]
    assert session["agent_id"] == "plan"
    assert session["status"] == SessionStatus.IDLE.value
    assert task["metadata"]["execution_adapter"] == "session_read_tools"
    assert task["metadata"]["task_type"] == "session_plan"
    assert task["metadata"]["active_repo_write"] == "forbidden"
    assert task["metadata"]["external_network"] == "forbidden"
    assert task["metadata"]["allowed_tools"] == ["read", "glob", "grep", "artifact-read"]
    assert task["required_approvals"] == []

    store = SQLiteStore(tmp_path)
    messages = store.list_session_messages(session["id"])
    assert messages[0].content_preview == "inspect auth flow"


def test_foreground_prompt_continue_uses_latest_session(tmp_path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=tmp_path, check=True, capture_output=True)
    session = SQLiteStore(tmp_path).create_session(title="Continue me")

    class FakeCodexBackend:
        def __init__(self, config):
            self.config = config
            self.name = config.name

        def preflight(self):
            from harness.models import BackendStatus

            return BackendStatus(available=True, metadata=self.config.metadata, capabilities=self.config.capabilities)

        def run_direct_agent(self, project_root, prompt, final_message_path, *, model=None, reasoning_effort=None):
            final_message_path.write_text("Continued.", encoding="utf-8")
            self.config.settings["last_codex_approval_mode"] = "on-request via --ask-for-approval"
            self.config.settings["last_codex_sandbox_mode"] = "workspace-write"
            self.config.settings["last_apply_back_approval_required"] = False
            return (CodexRunResult(["codex", "exec", prompt], "", "", 0, [], "Continued."), self.config.capabilities, "")

    monkeypatch.setattr("harness.cli.main.CodexCliBackend", FakeCodexBackend)
    result = runner.invoke(app, ["continue work", "--project", str(tmp_path), "--continue", "--no-stream", "--output", "json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["session_id"] == session.id
    assert len(SQLiteStore(tmp_path).list_session_messages(session.id)) == 2


def test_foreground_stream_progress_is_persisted_before_display(tmp_path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=tmp_path, check=True, capture_output=True)

    class FakeCodexBackend:
        def __init__(self, config):
            self.config = config
            self.name = config.name

        def preflight(self):
            from harness.models import BackendStatus

            return BackendStatus(available=True, metadata=self.config.metadata, capabilities=self.config.capabilities)

        def stream_direct_agent(self, project_root, prompt, final_message_path, *, model=None, reasoning_effort=None):
            yield {"type": "event", "event": {"type": "message", "message": "Streaming progress"}, "line": "{}\n"}
            final_message_path.write_text("Streamed final.", encoding="utf-8")
            self.config.settings["last_codex_approval_mode"] = "on-request via --ask-for-approval"
            self.config.settings["last_codex_sandbox_mode"] = "workspace-write"
            self.config.settings["last_apply_back_approval_required"] = False
            yield {
                "type": "completed",
                "capabilities": self.config.capabilities,
                "network_status": "",
                "result": CodexRunResult(["codex", "exec", prompt], "", "", 0, [{"type": "message"}], "Streamed final."),
            }

    monkeypatch.setattr("harness.cli.main.CodexCliBackend", FakeCodexBackend)
    result = runner.invoke(app, ["stream progress", "--project", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "codex: message: Streaming progress" in result.output
    session_id = result.output.split("Session: ", 1)[1].splitlines()[0]
    store = SQLiteStore(tmp_path)
    session_events = store.list_session_store_events(session_id)
    kinds = [event.kind for event in session_events]
    assert "model.message_delta" in kinds
    assert "run.started" in kinds
    assert "run.finished" in kinds
    assert "artifact.registered" in kinds
    assert any(event.payload.get("summary") == "message: Streaming progress" for event in session_events)
    tail = runner.invoke(app, ["session", "tail", session_id, "--project", str(tmp_path)])
    assert tail.exit_code == 0, tail.output
    assert "Model update: message: Streaming progress" in tail.output


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
