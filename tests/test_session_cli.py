from __future__ import annotations

import json

from typer.testing import CliRunner

from harness.cli.main import app
from harness.memory.sqlite_store import SQLiteStore


runner = CliRunner()


def test_session_cli_crud_fork_export_and_delete_archives(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    session = store.create_session(title="CLI session", raw_model_ref="codex_cli/gpt-5.5")
    message = store.append_session_message(session.id, "user", "Export me")
    store.append_session_part(session.id, message.id, "text", text="Export me")

    listed = runner.invoke(app, ["session", "list", "--project", str(tmp_path), "--output", "json"])
    got = runner.invoke(app, ["session", "get", session.id, "--project", str(tmp_path), "--output", "json"])
    forked = runner.invoke(
        app,
        ["session", "fork", session.id, "--message", message.id, "--title", "CLI fork", "--project", str(tmp_path), "--output", "json"],
    )
    exported = runner.invoke(
        app,
        ["session", "export", session.id, "--metadata-only", "--project", str(tmp_path), "--output", "json"],
    )
    deleted = runner.invoke(app, ["session", "delete", session.id, "--project", str(tmp_path), "--output", "json"])

    assert listed.exit_code == 0, listed.output
    assert got.exit_code == 0, got.output
    assert forked.exit_code == 0, forked.output
    assert exported.exit_code == 0, exported.output
    assert deleted.exit_code == 0, deleted.output
    listed_payload = json.loads(listed.output)
    got_payload = json.loads(got.output)
    forked_payload = json.loads(forked.output)
    exported_payload = json.loads(exported.output)
    deleted_payload = json.loads(deleted.output)
    assert listed_payload["schema_version"] == "harness.sessions/v1"
    assert any(item["id"] == session.id for item in listed_payload["sessions"])
    assert got_payload["session"]["id"] == session.id
    assert got_payload["model_validation"]["raw_model_ref"] == "codex_cli/gpt-5.5"
    assert forked_payload["session"]["parent_session_id"] == session.id
    assert forked_payload["session"]["forked_from_message_id"] == message.id
    assert exported_payload["metadata_only"] is True
    assert exported_payload["include_artifacts"] is False
    assert exported_payload["messages"][0]["content_preview"] == "Export me"
    assert exported_payload["parts"][0]["text"] == "Export me"
    assert deleted_payload["destructive"] is False
    assert deleted_payload["behavior"] == "archive"
    assert deleted_payload["session"]["status"] == "archived"

