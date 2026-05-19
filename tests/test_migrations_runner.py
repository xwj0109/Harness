from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from harness.chat import ChatSessionState, handle_chat_input
from harness.cli.main import app
from harness.memory.sqlite_store import SCHEMA_MIGRATIONS, SESSION_SCHEMA_REPAIR_MESSAGE, SQLiteStore
from harness.session_tools import execute_session_tool


runner = CliRunner()


def _migration_checksum(filename: str) -> str:
    migration_path = Path(__file__).parents[1] / "src" / "harness" / "memory" / "migrations" / filename
    return hashlib.sha256(migration_path.read_text(encoding="utf-8").encode("utf-8")).hexdigest()


def _drop_table(project_root: Path, table: str) -> None:
    with sqlite3.connect(project_root / ".harness" / "harness.sqlite") as conn:
        conn.execute(f"DROP TABLE IF EXISTS {table}")


def test_schema_migrations_apply_in_declared_order_and_are_idempotent(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    first = store.list_schema_migrations()

    store.initialize()
    second = store.list_schema_migrations()

    assert [row["id"] for row in first] == [migration[0] for migration in SCHEMA_MIGRATIONS]
    assert first == second
    assert first[0]["checksum"] == _migration_checksum(SCHEMA_MIGRATIONS[0][1])
    assert first[0]["metadata_json"]

    session = store.create_session(title="Migration smoke")
    assert store.get_session(session.id).title == "Migration smoke"


def test_schema_migration_checksum_mismatch_fails_closed(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    migration_id = SCHEMA_MIGRATIONS[0][0]

    with sqlite3.connect(store.db_path) as conn:
        conn.execute("UPDATE schema_migrations SET checksum = ? WHERE id = ?", ("bad-checksum", migration_id))

    with pytest.raises(RuntimeError, match=f"Schema migration checksum mismatch for {migration_id}"):
        SQLiteStore(tmp_path).initialize()


def test_unknown_future_schema_migration_fails_closed(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()

    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            """
            INSERT INTO schema_migrations (id, checksum, applied_at, metadata_json)
            VALUES (?, ?, ?, ?)
            """,
            ("29990101_001_future", "future-checksum", "2999-01-01T00:00:00+00:00", "{}"),
        )

    with pytest.raises(RuntimeError, match="Unknown future schema migration"):
        SQLiteStore(tmp_path).initialize()


def test_chat_pwd_migrates_old_database_missing_sessions_table(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    _drop_table(tmp_path, "sessions")

    response = handle_chat_input("/pwd", tmp_path, ChatSessionState())

    assert response["ok"] is True
    assert response["kind"] == "session_tool_result"
    assert "Session cwd: ." in "\n".join(response["lines"])
    assert "no such table" not in json.dumps(response).lower()
    assert SQLiteStore(tmp_path).inspect_required_session_schema()["ok"] is True


def test_session_tool_gateway_can_cd_after_old_database_session_schema_repair(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    (tmp_path / "src").mkdir()
    _drop_table(tmp_path, "sessions")

    store = SQLiteStore.open_initialized(tmp_path)
    session = store.create_session(title="Gateway repair")
    result = execute_session_tool(store, tmp_path, session.id, "cd", {"path": "src", "actor": "model"})

    assert result.ok is True
    assert "Changed session cwd: . -> src" in result.preview
    assert SQLiteStore(tmp_path).get_session(session.id).metadata["cwd"] == "src"


def test_doctor_reports_and_repairs_missing_required_event_table(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    _drop_table(tmp_path, "event_store")

    result = runner.invoke(app, ["doctor", "--project", str(tmp_path), "--output", "json"])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    check = next(check for check in payload["checks"] if check["id"] == "session_schema")
    assert check["status"] == "fail"
    assert check["message"] == SESSION_SCHEMA_REPAIR_MESSAGE
    assert check["details"]["missing_tables"] == ["event_store"]
    assert "no such table" not in result.output.lower()

    repaired = runner.invoke(app, ["doctor", "--repair", "--project", str(tmp_path), "--output", "json"])

    assert repaired.exit_code == 0, repaired.output
    repaired_payload = json.loads(repaired.output)
    repaired_check = next(check for check in repaired_payload["checks"] if check["id"] == "session_schema")
    assert repaired_check["status"] == "pass"
    assert repaired_check["message"] == "Harness session schema repaired."
    assert repaired_check["details"]["missing_tables"] == []
    assert SQLiteStore(tmp_path).inspect_required_session_schema()["ok"] is True


def test_doctor_reports_and_repairs_missing_sessions_table(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    _drop_table(tmp_path, "sessions")

    result = runner.invoke(app, ["doctor", "--project", str(tmp_path), "--output", "json"])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    checks = {check["id"]: check for check in payload["checks"]}
    assert checks["session_schema"]["status"] == "fail"
    assert checks["session_schema"]["details"]["missing_tables"] == ["sessions"]
    assert checks["required_session_tables"]["status"] == "fail"
    assert checks["required_session_tables"]["details"]["repairable"] is True
    assert "no such table" not in result.output.lower()

    repaired = runner.invoke(app, ["doctor", "--repair", "--project", str(tmp_path), "--output", "json"])

    assert repaired.exit_code == 0, repaired.output
    repaired_payload = json.loads(repaired.output)
    repaired_checks = {check["id"]: check for check in repaired_payload["checks"]}
    assert repaired_checks["session_schema"]["status"] == "pass"
    assert repaired_checks["required_session_tables"]["status"] == "pass"
    assert SQLiteStore(tmp_path).inspect_required_session_schema()["ok"] is True


def test_invalid_session_cwd_gets_recovery_prompt_and_doctor_repair(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    session = store.create_session(title="Invalid cwd", metadata={"cwd": "missing-dir"})

    response = handle_chat_input("/pwd", tmp_path, ChatSessionState(session_id=session.id))

    assert response["ok"] is False
    assert response["kind"] == "session_tool_result"
    response_json = json.dumps(response)
    assert "harness doctor --repair" in response_json
    assert "reset invalid session cwd values" in response_json
    assert "Traceback" not in response_json

    check = runner.invoke(app, ["doctor", "--project", str(tmp_path), "--output", "json"])
    assert check.exit_code == 1
    check_payload = json.loads(check.output)
    cwd_check = next(item for item in check_payload["checks"] if item["id"] == "session_cwd")
    assert cwd_check["status"] == "fail"
    assert cwd_check["details"]["invalid"][0]["session_id"] == session.id

    repaired = runner.invoke(app, ["doctor", "--repair", "--project", str(tmp_path), "--output", "json"])
    assert repaired.exit_code == 0, repaired.output
    repaired_payload = json.loads(repaired.output)
    repaired_cwd = next(item for item in repaired_payload["checks"] if item["id"] == "session_cwd")
    assert repaired_cwd["status"] == "pass"
    assert repaired_cwd["details"]["repaired_session_ids"] == [session.id]
    assert SQLiteStore(tmp_path).get_session(session.id).metadata["cwd"] == "."
