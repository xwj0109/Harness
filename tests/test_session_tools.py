from __future__ import annotations

import json
from datetime import timedelta

from typer.testing import CliRunner

from harness.cli.main import app
from harness.memory.sqlite_store import SQLiteStore
from harness.models import SessionPermissionBoundaryKind, SessionPermissionScope, SessionPermissionStatus
from harness.session_tools import (
    SessionToolPermissionDecisionStatus,
    SessionToolSideEffect,
    decide_session_tool_permission,
    default_session_tool_descriptors,
    get_session_tool_descriptor,
)


runner = CliRunner()


def test_phase_4a_session_tool_descriptors_are_low_risk_and_plan_safe() -> None:
    descriptors = default_session_tool_descriptors()
    by_id = {descriptor.id: descriptor for descriptor in descriptors}
    phase_4a_enabled = [
        descriptor
        for descriptor in descriptors
        if descriptor.enabled and descriptor.side_effect in {SessionToolSideEffect.NONE, SessionToolSideEffect.SESSION_LOCAL}
    ]

    assert {descriptor.id for descriptor in phase_4a_enabled} == {
        "read",
        "glob",
        "grep",
        "artifact-read",
        "policy-explain",
        "todo",
        "question",
    }
    assert all(descriptor.allowed_in_plan_agent for descriptor in phase_4a_enabled)
    assert all(descriptor.boundary_kind == SessionPermissionBoundaryKind.LOCAL_ONLY for descriptor in phase_4a_enabled)
    assert all(descriptor.permission_required is False for descriptor in phase_4a_enabled)
    assert all(descriptor.inline_preview_limit_bytes == 16 * 1024 for descriptor in descriptors)
    assert all(descriptor.event_payload_limit_bytes == 64 * 1024 for descriptor in descriptors)
    assert all(
        "Descriptors are documentation and validation metadata, not permission grants." in descriptor.safety_notes
        for descriptor in descriptors
    )

    assert by_id["read"].input_schema["required"] == ["path"]
    assert by_id["todo"].side_effect == SessionToolSideEffect.SESSION_LOCAL
    assert by_id["question"].side_effect == SessionToolSideEffect.SESSION_LOCAL


def test_phase_4b_descriptors_are_disabled_and_permission_required() -> None:
    descriptors = default_session_tool_descriptors()
    by_id = {descriptor.id: descriptor for descriptor in descriptors}
    disabled_high_risk_ids = {
        "managed-action",
        "shell",
        "web-fetch",
        "web-search",
        "mcp",
        "mcp-resource",
        "plugin-tool",
        "skill-load",
        "pty",
        "repo-clone",
    }

    assert disabled_high_risk_ids | {"patch", "direct-write", "docker-test"} <= set(by_id)
    for tool_id in disabled_high_risk_ids:
        descriptor = by_id[tool_id]
        assert descriptor.enabled is False
        assert descriptor.permission_required is True
        assert descriptor.allowed_in_plan_agent is False
        assert descriptor.risk in {"medium", "high"}
        assert "disabled by default" in " ".join(descriptor.safety_notes)
    assert by_id["shell"].boundary_kind == SessionPermissionBoundaryKind.SHELL
    assert by_id["patch"].enabled is True
    assert by_id["patch"].permission_required is True
    assert by_id["patch"].side_effect == SessionToolSideEffect.MUTATION
    assert by_id["patch"].boundary_kind == SessionPermissionBoundaryKind.ACTIVE_REPO_WRITE
    assert by_id["direct-write"].enabled is True
    assert by_id["direct-write"].permission_required is True
    assert by_id["direct-write"].side_effect == SessionToolSideEffect.MUTATION
    assert by_id["direct-write"].boundary_kind == SessionPermissionBoundaryKind.ACTIVE_REPO_WRITE
    assert by_id["docker-test"].enabled is True
    assert by_id["docker-test"].permission_required is True
    assert by_id["docker-test"].side_effect == SessionToolSideEffect.EXECUTION
    assert by_id["docker-test"].boundary_kind == SessionPermissionBoundaryKind.SHELL
    assert by_id["pty"].boundary_kind == SessionPermissionBoundaryKind.PTY
    assert by_id["mcp"].boundary_kind == SessionPermissionBoundaryKind.MCP
    assert by_id["mcp-resource"].boundary_kind == SessionPermissionBoundaryKind.MCP
    assert by_id["web-fetch"].boundary_kind == SessionPermissionBoundaryKind.EXTERNAL_NETWORK
    assert by_id["web-search"].boundary_kind == SessionPermissionBoundaryKind.EXTERNAL_NETWORK
    assert by_id["plugin-tool"].permission_key == "tool.plugin.execution"
    assert by_id["skill-load"].side_effect == SessionToolSideEffect.SESSION_LOCAL
    assert by_id["skill-load"].permission_required is True


def test_session_tool_descriptor_lookup_and_json_round_trip() -> None:
    descriptor = get_session_tool_descriptor("grep")
    payload = descriptor.model_dump(mode="json")

    assert payload["schema_version"] == "harness.session_tool_descriptor/v1"
    assert payload["id"] == "grep"
    assert payload["permission_key"] == "tool.grep.project_files"
    assert payload["input_schema"]["properties"]["regex"]["default"] is False
    assert "shell" not in json.dumps(payload)
    assert "external_network" not in json.dumps(payload)


def test_session_tools_cli_lists_descriptor_metadata_without_grants(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0

    result = runner.invoke(app, ["session", "tools", "--project", str(tmp_path), "--output", "json"])
    plan_only = runner.invoke(app, ["session", "tools", "--project", str(tmp_path), "--plan-only", "--output", "json"])
    inspect_one = runner.invoke(app, ["session", "tools", "--project", str(tmp_path), "--tool", "artifact-read"])
    shell = runner.invoke(app, ["session", "tools", "--project", str(tmp_path), "--tool", "shell", "--output", "json"])
    missing = runner.invoke(app, ["session", "tools", "--project", str(tmp_path), "--tool", "not-a-tool", "--output", "json"])

    assert result.exit_code == 0, result.output
    assert plan_only.exit_code == 0, plan_only.output
    assert inspect_one.exit_code == 0, inspect_one.output
    assert shell.exit_code == 0, shell.output
    assert missing.exit_code == 1

    payload = json.loads(result.output)
    plan_payload = json.loads(plan_only.output)
    shell_payload = json.loads(shell.output)
    assert payload["schema_version"] == "harness.session_tools/v1"
    assert payload["permission_granting"] is False
    assert {tool["id"] for tool in plan_payload["tools"]} == {
        "read",
        "glob",
        "grep",
        "artifact-read",
        "policy-explain",
        "todo",
        "question",
    }
    assert any(tool["id"] == "shell" and tool["enabled"] is False for tool in payload["tools"])
    assert any(tool["id"] == "web-search" and tool["enabled"] is False for tool in payload["tools"])
    assert any(tool["id"] == "mcp-resource" and tool["enabled"] is False for tool in payload["tools"])
    assert any(tool["id"] == "plugin-tool" and tool["enabled"] is False for tool in payload["tools"])
    assert any(tool["id"] == "skill-load" and tool["enabled"] is False for tool in payload["tools"])
    assert shell_payload["tools"][0]["id"] == "shell"
    assert shell_payload["tools"][0]["enabled"] is False
    assert "Descriptors are documentation and validation metadata, not permission grants." in inspect_one.output
    assert "artifact-read" in inspect_one.output


def test_session_todo_and_question_cli_persist_transcript_parts_and_events(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    session = store.create_session(title="Tools")
    raw_secret = "sk-abcdefghijklmnopqrstuvwxyz"

    todo_result = runner.invoke(
        app,
        [
            "session",
            "todo",
            session.id,
            "--project",
            str(tmp_path),
            "--content",
            f"Review auth token {raw_secret}",
            "--status",
            "in-progress",
            "--priority",
            "5",
            "--output",
            "json",
        ],
    )
    question_result = runner.invoke(
        app,
        [
            "session",
            "question",
            session.id,
            "--project",
            str(tmp_path),
            "--question",
            f"Which path should use {raw_secret}?",
            "--choice",
            "src/auth.py",
            "--choice",
            "tests/auth_test.py",
            "--output",
            "json",
        ],
    )
    list_result = runner.invoke(app, ["session", "todo", session.id, "--project", str(tmp_path), "--list", "--output", "json"])
    transcript = runner.invoke(app, ["session", "transcript", session.id, "--project", str(tmp_path)])
    tail = runner.invoke(app, ["session", "tail", session.id, "--project", str(tmp_path)])

    assert todo_result.exit_code == 0, todo_result.output
    assert question_result.exit_code == 0, question_result.output
    assert list_result.exit_code == 0, list_result.output
    assert transcript.exit_code == 0, transcript.output
    assert tail.exit_code == 0, tail.output

    todo_payload = json.loads(todo_result.output)
    question_payload = json.loads(question_result.output)
    list_payload = json.loads(list_result.output)
    assert todo_payload["todo"]["status"] == "in_progress"
    assert todo_payload["todo"]["priority"] == 5
    assert question_payload["part"]["kind"] == "question"
    assert list_payload["todos"][0]["id"] == todo_payload["todo"]["id"]

    combined = "\n".join([todo_result.output, question_result.output, list_result.output, transcript.output, tail.output])
    assert raw_secret not in combined
    assert "[REDACTED_SECRET]" in combined
    assert "[todo in_progress]" in transcript.output
    assert "[question]" in transcript.output
    assert "Todo updated" in tail.output
    assert "Question requested" in tail.output


def test_disabled_phase_4b_tool_records_denied_evidence_without_execution(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    session = store.create_session(title="Disabled shell")

    result = runner.invoke(
        app,
        [
            "session",
            "tool",
            session.id,
            "shell",
            "--project",
            str(tmp_path),
            "--input-json",
            '{"command":"echo blocked"}',
            "--output",
            "json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["result"]["tool_id"] == "shell"
    assert payload["result"]["error_type"] == "permission_denied"
    assert payload["result"]["permission_id"]
    assert "Session tool is disabled by policy: shell" in payload["result"]["preview"]

    reloaded = SQLiteStore(tmp_path)
    permission = reloaded.get_session_permission(payload["result"]["permission_id"])
    assert permission.status == SessionPermissionStatus.DENIED
    assert permission.tool_id == "shell"
    assert permission.boundary_kind == SessionPermissionBoundaryKind.SHELL
    assert reloaded.get_session(session.id).active_run_id == payload["result"]["run_id"]

    tail = runner.invoke(app, ["session", "tail", session.id, "--project", str(tmp_path), "--limit", "20"])
    assert tail.exit_code == 0, tail.output
    assert "Tool started" in tail.output
    assert "Permission checked" in tail.output
    assert "Permission requested" in tail.output
    assert "Permission resolved" in tail.output
    assert "Tool output" in tail.output


def test_patch_tool_requires_permission_then_persists_plan_artifacts_without_applying(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")
    store = SQLiteStore(tmp_path)
    session = store.create_session(title="Patch plan")
    patch = """--- a/app.py
+++ b/app.py
@@ -1 +1 @@
-value = 1
+value = 2
"""

    first = runner.invoke(
        app,
        [
            "session",
            "tool",
            session.id,
            "patch",
            "--project",
            str(tmp_path),
            "--input-json",
            json.dumps({"patch": patch}),
            "--output",
            "json",
        ],
    )

    assert first.exit_code == 0, first.output
    first_payload = json.loads(first.output)["result"]
    assert first_payload["ok"] is False
    assert first_payload["error_type"] == "permission_required"
    assert first_payload["permission_id"]
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "value = 1\n"
    pending = SQLiteStore(tmp_path).get_session_permission(first_payload["permission_id"])
    assert pending.status == SessionPermissionStatus.PENDING
    assert pending.normalized_target_pattern == "app.py"

    allowed = runner.invoke(
        app,
        [
            "session",
            "permission",
            session.id,
            "--project",
            str(tmp_path),
            "--resolve",
            first_payload["permission_id"],
            "--decision",
            "allowed",
            "--output",
            "json",
        ],
    )
    second = runner.invoke(
        app,
        [
            "session",
            "tool",
            session.id,
            "patch",
            "--project",
            str(tmp_path),
            "--input-json",
            json.dumps({"patch": patch}),
            "--output",
            "json",
        ],
    )

    assert allowed.exit_code == 0, allowed.output
    assert second.exit_code == 0, second.output
    second_payload = json.loads(second.output)["result"]
    assert second_payload["ok"] is True
    assert "Patch validated but not applied." in second_payload["preview"]
    assert "Files: app.py" in second_payload["preview"]
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "value = 1\n"
    artifacts = SQLiteStore(tmp_path).list_artifacts(second_payload["run_id"])
    kinds = {artifact.kind for artifact in artifacts}
    assert {"session_tool_patch", "session_tool_patch_plan"} <= kinds
    plan = next(artifact for artifact in artifacts if artifact.kind == "session_tool_patch_plan")
    plan_payload = json.loads(plan.path.read_text(encoding="utf-8"))
    assert plan_payload["applied"] is False
    assert plan_payload["files"] == ["app.py"]


def test_direct_write_tool_requires_permission_then_persists_plan_artifacts_without_writing(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    session = store.create_session(title="Direct write plan")

    first = runner.invoke(
        app,
        [
            "session",
            "tool",
            session.id,
            "direct-write",
            "--project",
            str(tmp_path),
            "--input-json",
            json.dumps({"path": "notes.txt", "content": "proposed content\n"}),
            "--output",
            "json",
        ],
    )

    assert first.exit_code == 0, first.output
    first_payload = json.loads(first.output)["result"]
    assert first_payload["ok"] is False
    assert first_payload["error_type"] == "permission_required"
    assert first_payload["permission_id"]
    assert not (tmp_path / "notes.txt").exists()
    pending = SQLiteStore(tmp_path).get_session_permission(first_payload["permission_id"])
    assert pending.status == SessionPermissionStatus.PENDING
    assert pending.normalized_target_pattern == "notes.txt"

    allowed = runner.invoke(
        app,
        [
            "session",
            "permission",
            session.id,
            "--project",
            str(tmp_path),
            "--resolve",
            first_payload["permission_id"],
            "--decision",
            "allowed",
            "--output",
            "json",
        ],
    )
    second = runner.invoke(
        app,
        [
            "session",
            "tool",
            session.id,
            "direct-write",
            "--project",
            str(tmp_path),
            "--input-json",
            json.dumps({"path": "notes.txt", "content": "proposed content\n"}),
            "--output",
            "json",
        ],
    )

    assert allowed.exit_code == 0, allowed.output
    assert second.exit_code == 0, second.output
    second_payload = json.loads(second.output)["result"]
    assert second_payload["ok"] is True
    assert "Direct write validated but not applied." in second_payload["preview"]
    assert not (tmp_path / "notes.txt").exists()
    artifacts = SQLiteStore(tmp_path).list_artifacts(second_payload["run_id"])
    kinds = {artifact.kind for artifact in artifacts}
    assert {"session_tool_direct_write_content", "session_tool_direct_write_plan"} <= kinds
    plan = next(artifact for artifact in artifacts if artifact.kind == "session_tool_direct_write_plan")
    plan_payload = json.loads(plan.path.read_text(encoding="utf-8"))
    assert plan_payload["applied"] is False
    assert plan_payload["target"] == "notes.txt"


def test_direct_write_tool_denies_blocked_path_without_creating_file(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    session = store.create_session(title="Blocked direct write")

    result = runner.invoke(
        app,
        [
            "session",
            "tool",
            session.id,
            "direct-write",
            "--project",
            str(tmp_path),
            "--input-json",
            json.dumps({"path": ".harness/config.json", "content": "{}"}),
            "--output",
            "json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)["result"]
    assert payload["ok"] is False
    assert payload["error_type"] == "permission_denied"
    assert payload["permission_id"]
    assert not (tmp_path / ".harness" / "config.json").exists()
    assert SQLiteStore(tmp_path).list_session_permissions(session.id)[0].status == SessionPermissionStatus.DENIED


def test_docker_test_tool_requires_permission_then_persists_plan_without_execution(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    (tmp_path / "tests").mkdir()
    store = SQLiteStore(tmp_path)
    session = store.create_session(title="Docker test plan")
    args = {"command": ["pytest", "-q"], "cwd": "tests"}

    first = runner.invoke(
        app,
        [
            "session",
            "tool",
            session.id,
            "docker-test",
            "--project",
            str(tmp_path),
            "--input-json",
            json.dumps(args),
            "--output",
            "json",
        ],
    )

    assert first.exit_code == 0, first.output
    first_payload = json.loads(first.output)["result"]
    assert first_payload["ok"] is False
    assert first_payload["error_type"] == "permission_required"
    assert first_payload["permission_id"]
    pending = SQLiteStore(tmp_path).get_session_permission(first_payload["permission_id"])
    assert pending.status == SessionPermissionStatus.PENDING
    assert pending.normalized_target_pattern == "tests:pytest -q"

    allowed = runner.invoke(
        app,
        [
            "session",
            "permission",
            session.id,
            "--project",
            str(tmp_path),
            "--resolve",
            first_payload["permission_id"],
            "--decision",
            "allowed",
            "--output",
            "json",
        ],
    )
    second = runner.invoke(
        app,
        [
            "session",
            "tool",
            session.id,
            "docker-test",
            "--project",
            str(tmp_path),
            "--input-json",
            json.dumps(args),
            "--output",
            "json",
        ],
    )

    assert allowed.exit_code == 0, allowed.output
    assert second.exit_code == 0, second.output
    second_payload = json.loads(second.output)["result"]
    assert second_payload["ok"] is True
    assert "Docker test validated but not executed." in second_payload["preview"]
    artifacts = SQLiteStore(tmp_path).list_artifacts(second_payload["run_id"])
    plan = next(artifact for artifact in artifacts if artifact.kind == "session_tool_docker_test_plan")
    plan_payload = json.loads(plan.path.read_text(encoding="utf-8"))
    assert plan_payload["executed"] is False
    assert plan_payload["command"] == ["pytest", "-q"]
    assert plan_payload["cwd"] == "tests"


def test_docker_test_tool_denies_shell_string_command(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    session = store.create_session(title="Docker denied")

    result = runner.invoke(
        app,
        [
            "session",
            "tool",
            session.id,
            "docker-test",
            "--project",
            str(tmp_path),
            "--input-json",
            json.dumps({"command": "pytest -q"}),
            "--output",
            "json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)["result"]
    assert payload["ok"] is False
    assert payload["error_type"] == "permission_denied"
    assert payload["permission_id"]
    assert "Shell-string test commands are not allowed" in payload["preview"]
    assert SQLiteStore(tmp_path).list_session_permissions(session.id)[0].status == SessionPermissionStatus.DENIED


def test_session_todo_rejects_invalid_status(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    session = SQLiteStore(tmp_path).create_session(title="Invalid")

    result = runner.invoke(
        app,
        [
            "session",
            "todo",
            session.id,
            "--project",
            str(tmp_path),
            "--content",
            "Bad state",
            "--status",
            "running_shell",
            "--output",
            "json",
        ],
    )

    assert result.exit_code == 1
    assert "Unsupported session todo status" in result.output


def test_session_permission_request_and_resolution_are_persisted_with_expiry(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    session = store.create_session(title="Permissions")
    raw_secret = "sk-abcdefghijklmnopqrstuvwxyz"

    requested = runner.invoke(
        app,
        [
            "session",
            "permission",
            session.id,
            "--project",
            str(tmp_path),
            "--request",
            "--tool",
            "read",
            "--action",
            "read",
            "--target",
            f"src/auth.py {raw_secret}",
            "--boundary",
            "local_only",
            "--risk",
            "low",
            "--scope",
            "once",
            "--reason",
            f"needs inspection {raw_secret}",
            "--output",
            "json",
        ],
    )

    assert requested.exit_code == 0, requested.output
    payload = json.loads(requested.output)
    permission = payload["permission"]
    assert permission["status"] == SessionPermissionStatus.PENDING.value
    assert permission["scope"] == SessionPermissionScope.ONCE.value
    assert permission["boundary_kind"] == SessionPermissionBoundaryKind.LOCAL_ONLY.value
    assert raw_secret not in requested.output
    assert "[REDACTED_SECRET]" in requested.output

    loaded = SQLiteStore(tmp_path).get_session_permission(permission["id"])
    assert loaded.expires_at - loaded.requested_at <= timedelta(minutes=16)
    assert loaded.expires_at > loaded.requested_at

    listed = runner.invoke(
        app,
        ["session", "permission", session.id, "--project", str(tmp_path), "--status", "pending", "--output", "json"],
    )
    resolved = runner.invoke(
        app,
        [
            "session",
            "permission",
            session.id,
            "--project",
            str(tmp_path),
            "--resolve",
            permission["id"],
            "--decision",
            "denied",
            "--reason",
            "operator declined",
            "--output",
            "json",
        ],
    )
    tail = runner.invoke(app, ["session", "tail", session.id, "--project", str(tmp_path)])

    assert listed.exit_code == 0, listed.output
    assert resolved.exit_code == 0, resolved.output
    assert tail.exit_code == 0, tail.output
    assert json.loads(listed.output)["permissions"][0]["id"] == permission["id"]
    assert json.loads(resolved.output)["permission"]["status"] == SessionPermissionStatus.DENIED.value
    assert "Permission requested" in tail.output
    assert "Permission resolved" in tail.output
    assert raw_secret not in tail.output


def test_session_permission_session_scope_caps_at_24_hours(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    session = store.create_session(title="Session scoped")

    permission = store.request_session_permission(
        session.id,
        tool_id="grep",
        normalized_action="search",
        normalized_target_pattern="src/**",
        boundary_kind=SessionPermissionBoundaryKind.LOCAL_ONLY,
        risk="low",
        scope=SessionPermissionScope.SESSION,
    )

    assert permission.expires_at - permission.requested_at <= timedelta(hours=24, seconds=1)
    assert permission.expires_at - permission.requested_at > timedelta(hours=23, minutes=59)


def test_session_read_grep_and_glob_tools_persist_events_and_transcript(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("alpha = 1\nbeta = 2\n", encoding="utf-8")
    (tmp_path / "src" / "notes.md").write_text("alpha docs\n", encoding="utf-8")
    session = SQLiteStore(tmp_path).create_session(title="Read tools")

    read_result = runner.invoke(
        app,
        [
            "session",
            "tool",
            session.id,
            "read",
            "--project",
            str(tmp_path),
            "--input-json",
            '{"path":"src/app.py"}',
            "--output",
            "json",
        ],
    )
    grep_result = runner.invoke(
        app,
        [
            "session",
            "tool",
            session.id,
            "grep",
            "--project",
            str(tmp_path),
            "--input-json",
            '{"pattern":"alpha","path":"src","limit":10}',
            "--output",
            "json",
        ],
    )
    glob_result = runner.invoke(
        app,
        [
            "session",
            "tool",
            session.id,
            "glob",
            "--project",
            str(tmp_path),
            "--input-json",
            '{"pattern":"src/*.py"}',
            "--output",
            "json",
        ],
    )
    transcript = runner.invoke(app, ["session", "transcript", session.id, "--project", str(tmp_path)])
    tail = runner.invoke(app, ["session", "tail", session.id, "--project", str(tmp_path)])

    assert read_result.exit_code == 0, read_result.output
    assert grep_result.exit_code == 0, grep_result.output
    assert glob_result.exit_code == 0, glob_result.output
    assert transcript.exit_code == 0, transcript.output
    assert tail.exit_code == 0, tail.output
    assert json.loads(read_result.output)["result"]["preview"] == "alpha = 1\nbeta = 2\n"
    assert "src/app.py:1: alpha = 1" in json.loads(grep_result.output)["result"]["preview"]
    assert json.loads(glob_result.output)["result"]["preview"] == "src/app.py"
    assert "Permission checked" in tail.output
    assert "Tool started" in tail.output
    assert "Tool output" in tail.output
    assert "Tool finished" in tail.output
    assert "alpha = 1" in transcript.output
    assert SQLiteStore(tmp_path).get_run(json.loads(read_result.output)["result"]["run_id"]).session_id == session.id


def test_session_read_tool_denies_secret_or_outside_path_with_permission_evidence(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    (tmp_path / ".env").write_text("TOKEN=secret-value\n", encoding="utf-8")
    session = SQLiteStore(tmp_path).create_session(title="Denied read")

    result = runner.invoke(
        app,
        [
            "session",
            "tool",
            session.id,
            "read",
            "--project",
            str(tmp_path),
            "--input-json",
            '{"path":".env"}',
            "--output",
            "json",
        ],
    )
    tail = runner.invoke(app, ["session", "tail", session.id, "--project", str(tmp_path)])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)["result"]
    assert payload["ok"] is False
    assert payload["error_type"] == "permission_denied"
    assert payload["permission_id"]
    permissions = SQLiteStore(tmp_path).list_session_permissions(session.id)
    assert permissions[0].status == SessionPermissionStatus.DENIED
    assert "Permission requested" in tail.output
    assert "Permission resolved" in tail.output
    assert "Permission checked" in tail.output
    assert "Tool output" in tail.output


def test_session_tool_permission_decision_is_path_sensitive(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("ok\n", encoding="utf-8")
    (tmp_path / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
    store = SQLiteStore(tmp_path)
    session = store.create_session(title="Permission decision")

    allowed = decide_session_tool_permission(store, tmp_path, session.id, "read", {"path": "src/app.py"})
    secret = decide_session_tool_permission(store, tmp_path, session.id, "read", {"path": ".env"})
    outside = decide_session_tool_permission(store, tmp_path, session.id, "read", {"path": "../outside.txt"})

    assert allowed.status == SessionToolPermissionDecisionStatus.ALLOW
    assert allowed.action == "read"
    assert allowed.target == "src/app.py"
    assert secret.status == SessionToolPermissionDecisionStatus.DENY
    assert "secret" in " ".join(secret.reasons).lower()
    assert outside.status == SessionToolPermissionDecisionStatus.DENY
    assert "escapes project root" in " ".join(outside.reasons)


def test_session_tool_permission_can_unblock_excluded_project_read(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "generated.txt").write_text("generated artifact\n", encoding="utf-8")
    store = SQLiteStore(tmp_path)
    session = store.create_session(title="Ask permission")

    first = runner.invoke(
        app,
        [
            "session",
            "tool",
            session.id,
            "read",
            "--project",
            str(tmp_path),
            "--input-json",
            '{"path":"build/generated.txt"}',
            "--output",
            "json",
        ],
    )

    assert first.exit_code == 0, first.output
    first_payload = json.loads(first.output)["result"]
    assert first_payload["ok"] is False
    assert first_payload["error_type"] == "permission_required"
    assert first_payload["permission_id"]
    pending = SQLiteStore(tmp_path).get_session_permission(first_payload["permission_id"])
    assert pending.status == SessionPermissionStatus.PENDING
    assert pending.normalized_target_pattern == "build/generated.txt"

    allowed = runner.invoke(
        app,
        [
            "session",
            "permission",
            session.id,
            "--project",
            str(tmp_path),
            "--resolve",
            first_payload["permission_id"],
            "--decision",
            "allowed",
            "--output",
            "json",
        ],
    )
    second = runner.invoke(
        app,
        [
            "session",
            "tool",
            session.id,
            "read",
            "--project",
            str(tmp_path),
            "--input-json",
            '{"path":"build/generated.txt"}',
            "--output",
            "json",
        ],
    )
    tail = runner.invoke(app, ["session", "tail", session.id, "--project", str(tmp_path)])

    assert allowed.exit_code == 0, allowed.output
    assert second.exit_code == 0, second.output
    second_payload = json.loads(second.output)["result"]
    assert second_payload["ok"] is True
    assert second_payload["preview"] == "generated artifact\n"
    assert "Permission checked" in tail.output
    assert "read allow" in tail.output


def test_session_read_tool_truncates_large_output_to_artifact(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    (tmp_path / "large.txt").write_text("x" * (20 * 1024), encoding="utf-8")
    session = SQLiteStore(tmp_path).create_session(title="Large read")

    result = runner.invoke(
        app,
        [
            "session",
            "tool",
            session.id,
            "read",
            "--project",
            str(tmp_path),
            "--input-json",
            '{"path":"large.txt"}',
            "--output",
            "json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)["result"]
    assert payload["ok"] is True
    assert payload["truncated"] is True
    assert payload["artifact_id"]
    artifact = SQLiteStore(tmp_path).get_artifact(payload["artifact_id"])
    assert artifact.kind == "session_tool_output"
    assert artifact.session_id == session.id
    assert artifact.size_bytes == 20 * 1024


def test_session_artifact_read_tool_requires_session_link_and_replays_preview(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    session = store.create_session(title="Artifact read")
    run = store.create_run("artifact source", "session_tool_call", session_id=session.id)
    artifact_path = store.runs_dir / run.id / "note.txt"
    artifact_path.write_text("artifact preview\n", encoding="utf-8")
    artifact = store.register_artifact(
        run.id,
        "note",
        artifact_path,
        producer="test",
        redaction_state="redacted",
        session_id=session.id,
    )

    result = runner.invoke(
        app,
        [
            "session",
            "tool",
            session.id,
            "artifact-read",
            "--project",
            str(tmp_path),
            "--input-json",
            json.dumps({"artifact_id": artifact.id, "max_bytes": 200}),
            "--output",
            "json",
        ],
    )
    transcript = runner.invoke(app, ["session", "transcript", session.id, "--project", str(tmp_path)])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)["result"]
    assert payload["ok"] is True
    assert '"artifact_id":' in payload["preview"]
    assert "artifact preview" in payload["preview"]
    assert transcript.exit_code == 0, transcript.output
    assert "artifact preview" in transcript.output


def test_session_artifact_read_denies_artifact_from_other_session(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    source = store.create_session(title="Source")
    target = store.create_session(title="Target")
    run = store.create_run("artifact source", "session_tool_call", session_id=source.id)
    artifact_path = store.runs_dir / run.id / "note.txt"
    artifact_path.write_text("private artifact\n", encoding="utf-8")
    artifact = store.register_artifact(run.id, "note", artifact_path, session_id=source.id)

    result = runner.invoke(
        app,
        [
            "session",
            "tool",
            target.id,
            "artifact-read",
            "--project",
            str(tmp_path),
            "--input-json",
            json.dumps({"artifact_id": artifact.id}),
            "--output",
            "json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)["result"]
    assert payload["ok"] is False
    assert payload["error_type"] == "permission_denied"
    assert payload["permission_id"]
    assert SQLiteStore(tmp_path).list_session_permissions(target.id)[0].status == SessionPermissionStatus.DENIED


def test_session_policy_explain_tool_reports_session_and_task_policy(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    session = store.create_session(title="Policy", agent_id="code_editor")
    task = store.create_task(
        "Plan",
        description="Read only",
        agent_id="repo_inspector",
        metadata={"execution_adapter": "read_only_summary", "task_type": "read_only_repo_summary"},
        session_id=session.id,
    )
    store.attach_session_to_task(session.id, task.id)

    session_result = runner.invoke(
        app,
        [
            "session",
            "tool",
            session.id,
            "policy-explain",
            "--project",
            str(tmp_path),
            "--input-json",
            '{"subject_kind":"session"}',
            "--output",
            "json",
        ],
    )
    task_result = runner.invoke(
        app,
        [
            "session",
            "tool",
            session.id,
            "policy-explain",
            "--project",
            str(tmp_path),
            "--input-json",
            json.dumps({"subject_kind": "task", "subject_id": task.id}),
            "--output",
            "json",
        ],
    )

    assert session_result.exit_code == 0, session_result.output
    assert task_result.exit_code == 0, task_result.output
    session_preview = json.loads(session_result.output)["result"]["preview"]
    task_preview = json.loads(task_result.output)["result"]["preview"]
    assert "Sessions are operator-facing continuity" in session_preview
    assert '"policy_sha256":' in task_preview
    assert '"subject_kind": "task"' in task_preview
