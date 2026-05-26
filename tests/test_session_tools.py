from __future__ import annotations

import json
import subprocess
import sys
import threading
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import yaml
from typer.testing import CliRunner

from harness.cli.main import app
from harness.memory.sqlite_store import SQLiteStore
from harness.models import SessionPermissionBoundaryKind, SessionPermissionScope, SessionPermissionStatus
from harness.session_tools import (
    HARNESS_SESSION_TOOL_IDS,
    SessionToolPermissionDecisionStatus,
    SessionToolSideEffect,
    build_session_approval_card,
    decide_session_tool_permission,
    default_session_tool_descriptors,
    execute_session_tool,
    get_session_tool_descriptor,
    session_tool_catalog_projection,
)


runner = CliRunner()


class _FetchHandler(BaseHTTPRequestHandler):
    body = b""
    content_type = "text/plain; charset=utf-8"

    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", self.content_type)
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, format: str, *args: object) -> None:
        return


def _start_fetch_server(body: bytes, content_type: str) -> tuple[ThreadingHTTPServer, str]:
    class Handler(_FetchHandler):
        pass

    Handler.body = body
    Handler.content_type = content_type
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_port}/page"


def _start_mcp_search_server(body: bytes, content_type: str = "text/event-stream") -> tuple[ThreadingHTTPServer, str, list[dict]]:
    requests: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or "0")
            payload = self.rfile.read(length)
            requests.append(
                {
                    "path": self.path,
                    "body": payload.decode("utf-8", errors="replace"),
                    "accept": self.headers.get("Accept"),
                    "content_type": self.headers.get("Content-Type"),
                }
            )
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_port}/mcp", requests


def _run_git_for_test(cwd, args: list[str]) -> None:
    result = subprocess.run(["git", *args], cwd=cwd, check=False, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr or result.stdout


def test_phase_4a_session_tool_descriptors_are_low_risk_and_plan_safe() -> None:
    descriptors = default_session_tool_descriptors()
    by_id = {descriptor.id: descriptor for descriptor in descriptors}
    phase_4a_enabled = [
        descriptor
        for descriptor in descriptors
        if descriptor.enabled
        and descriptor.permission_required is False
        and descriptor.side_effect in {SessionToolSideEffect.NONE, SessionToolSideEffect.SESSION_LOCAL}
    ]

    assert {descriptor.id for descriptor in phase_4a_enabled} == {
        "read",
        "glob",
        "ls",
        "find",
        "grep",
        "git-diff",
        "pwd",
        "cd",
        "artifact-read",
        "lsp-diagnostics",
        "lsp-symbols",
        "lsp-definition",
        "lsp-references",
        "policy-explain",
        "repo-overview",
        "todo",
        "question",
        "plan-enter",
        "plan-exit",
        "task-status",
        "invalid",
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
    assert by_id["read"].side_effect == SessionToolSideEffect.NONE
    assert by_id["read"].boundary_kind == SessionPermissionBoundaryKind.LOCAL_ONLY
    assert by_id["read"].permission_required is False
    assert by_id["glob"].side_effect == SessionToolSideEffect.NONE
    assert by_id["glob"].boundary_kind == SessionPermissionBoundaryKind.LOCAL_ONLY
    assert by_id["glob"].permission_required is False
    assert by_id["ls"].enabled is True
    assert by_id["ls"].execution_supported is True
    assert by_id["ls"].planning_only is False
    assert by_id["ls"].allowed_in_plan_agent is True
    assert by_id["find"].enabled is True
    assert by_id["find"].execution_supported is True
    assert by_id["find"].planning_only is False
    assert by_id["find"].allowed_in_plan_agent is True
    assert by_id["grep"].side_effect == SessionToolSideEffect.NONE
    assert by_id["grep"].boundary_kind == SessionPermissionBoundaryKind.LOCAL_ONLY
    assert by_id["grep"].permission_required is False
    assert by_id["todo"].side_effect == SessionToolSideEffect.SESSION_LOCAL
    assert by_id["todo"].boundary_kind == SessionPermissionBoundaryKind.LOCAL_ONLY
    assert by_id["todo"].permission_required is False
    assert by_id["question"].side_effect == SessionToolSideEffect.SESSION_LOCAL
    assert by_id["question"].boundary_kind == SessionPermissionBoundaryKind.LOCAL_ONLY
    assert by_id["question"].permission_required is False
    assert by_id["invalid"].enabled is True
    assert by_id["invalid"].execution_supported is True
    assert by_id["invalid"].permission_required is False
    assert by_id["invalid"].tool_class == "session_local"
    assert by_id["plan-enter"].enabled is True
    assert by_id["plan-enter"].execution_supported is True
    assert by_id["plan-enter"].planning_only is False
    assert by_id["plan-enter"].permission_required is False
    assert by_id["plan-exit"].enabled is True
    assert by_id["plan-exit"].execution_supported is True
    assert by_id["plan-exit"].planning_only is False
    assert by_id["plan-exit"].permission_required is False
    assert by_id["task-status"].enabled is True
    assert by_id["task-status"].execution_supported is True
    assert by_id["task-status"].planning_only is False
    assert by_id["task-status"].permission_required is False
    assert by_id["lsp-diagnostics"].enabled is True
    assert by_id["lsp-diagnostics"].permission_required is False
    assert by_id["lsp-diagnostics"].allowed_in_plan_agent is True
    assert by_id["lsp-symbols"].enabled is True
    assert by_id["lsp-symbols"].permission_required is False
    assert by_id["lsp-symbols"].allowed_in_plan_agent is True
    assert by_id["lsp-definition"].enabled is True
    assert by_id["lsp-definition"].execution_supported is True
    assert by_id["lsp-definition"].permission_required is False
    assert by_id["lsp-definition"].allowed_in_plan_agent is True
    assert by_id["lsp-references"].enabled is True
    assert by_id["lsp-references"].execution_supported is True
    assert by_id["lsp-references"].permission_required is False
    assert by_id["lsp-references"].allowed_in_plan_agent is True
    assert by_id["repo-overview"].enabled is True
    assert by_id["repo-overview"].permission_required is False
    assert by_id["repo-overview"].allowed_in_plan_agent is True
    assert by_id["skill-load"].enabled is True
    assert by_id["skill-load"].permission_required is True
    assert by_id["skill-load"].allowed_in_plan_agent is False


def test_phase_4b_descriptors_are_disabled_and_permission_required() -> None:
    descriptors = default_session_tool_descriptors()
    by_id = {descriptor.id: descriptor for descriptor in descriptors}
    disabled_high_risk_ids = {
        "managed-action",
        "mcp",
        "plugin-tool",
        "pty",
    }

    assert disabled_high_risk_ids | {"patch", "direct-write", "docker-test", "shell"} <= set(by_id)
    for tool_id in disabled_high_risk_ids:
        descriptor = by_id[tool_id]
        assert descriptor.enabled is False
        assert descriptor.permission_required is True
        assert descriptor.allowed_in_plan_agent is False
        assert descriptor.risk in {"medium", "high"}
        assert "disabled by default" in " ".join(descriptor.safety_notes)
    assert by_id["shell"].boundary_kind == SessionPermissionBoundaryKind.SHELL
    assert by_id["shell"].enabled is True
    assert by_id["shell"].permission_required is True
    assert by_id["shell"].side_effect == SessionToolSideEffect.EXECUTION
    assert by_id["shell"].replay_policy.value == "rerun_forbidden"
    assert by_id["patch"].enabled is True
    assert by_id["patch"].permission_required is True
    assert by_id["patch"].side_effect == SessionToolSideEffect.MUTATION
    assert by_id["patch"].boundary_kind == SessionPermissionBoundaryKind.ACTIVE_REPO_WRITE
    assert "does not apply changes to the active workspace" in " ".join(by_id["patch"].safety_notes)
    assert "apply-back" in " ".join(by_id["patch"].safety_notes)
    assert by_id["edit"].enabled is True
    assert by_id["edit"].execution_supported is True
    assert by_id["edit"].planning_only is False
    assert by_id["edit"].permission_required is True
    assert by_id["edit"].side_effect == SessionToolSideEffect.MUTATION
    assert by_id["edit"].boundary_kind == SessionPermissionBoundaryKind.ACTIVE_REPO_WRITE
    assert by_id["write"].enabled is True
    assert by_id["write"].execution_supported is True
    assert by_id["write"].planning_only is False
    assert by_id["write"].permission_required is True
    assert by_id["write"].side_effect == SessionToolSideEffect.MUTATION
    assert by_id["write"].boundary_kind == SessionPermissionBoundaryKind.ACTIVE_REPO_WRITE
    assert by_id["direct-write"].enabled is True
    assert by_id["direct-write"].permission_required is True
    assert by_id["direct-write"].side_effect == SessionToolSideEffect.MUTATION
    assert by_id["direct-write"].boundary_kind == SessionPermissionBoundaryKind.ACTIVE_REPO_WRITE
    assert "does not write to the active workspace" in " ".join(by_id["direct-write"].safety_notes)
    assert "active workspace mutation" in " ".join(by_id["direct-write"].safety_notes)
    assert by_id["docker-test"].enabled is True
    assert by_id["docker-test"].permission_required is True
    assert by_id["docker-test"].side_effect == SessionToolSideEffect.EXECUTION
    assert by_id["docker-test"].boundary_kind == SessionPermissionBoundaryKind.SHELL
    assert by_id["web-fetch"].enabled is True
    assert by_id["web-fetch"].permission_required is True
    assert by_id["web-fetch"].side_effect == SessionToolSideEffect.NETWORK
    assert by_id["web-fetch"].boundary_kind == SessionPermissionBoundaryKind.EXTERNAL_NETWORK
    assert by_id["web-search"].enabled is True
    assert by_id["web-search"].permission_required is True
    assert by_id["web-search"].side_effect == SessionToolSideEffect.NETWORK
    assert by_id["web-search"].boundary_kind == SessionPermissionBoundaryKind.EXTERNAL_NETWORK
    assert by_id["repo-clone"].enabled is True
    assert by_id["repo-clone"].permission_required is True
    assert by_id["repo-clone"].side_effect == SessionToolSideEffect.NETWORK
    assert by_id["repo-clone"].boundary_kind == SessionPermissionBoundaryKind.EXTERNAL_NETWORK
    assert by_id["pty"].boundary_kind == SessionPermissionBoundaryKind.PTY
    assert by_id["mcp"].boundary_kind == SessionPermissionBoundaryKind.MCP
    assert by_id["mcp-resource"].boundary_kind == SessionPermissionBoundaryKind.MCP
    assert by_id["mcp-resource"].enabled is True
    assert by_id["mcp-resource"].permission_required is True
    assert by_id["mcp-resource"].side_effect == SessionToolSideEffect.NONE
    assert by_id["plugin-tool"].permission_key == "tool.plugin.execution"
    assert "exact tool name" in " ".join(by_id["mcp"].safety_notes)
    assert "version/checksum" in " ".join(by_id["mcp"].safety_notes)
    assert "exact arguments" in " ".join(by_id["plugin-tool"].safety_notes)
    assert "allowed scopes" in " ".join(by_id["plugin-tool"].safety_notes)
    assert by_id["skill-load"].side_effect == SessionToolSideEffect.SESSION_LOCAL
    assert by_id["skill-load"].enabled is True
    assert by_id["skill-load"].permission_required is True
    assert by_id["task"].enabled is True
    assert by_id["task"].execution_supported is True
    assert by_id["task"].planning_only is False
    assert by_id["task"].permission_required is True
    assert by_id["task"].side_effect == SessionToolSideEffect.EXECUTION
    assert by_id["task"].boundary_kind == SessionPermissionBoundaryKind.LOCAL_ONLY


def test_session_tool_catalog_exposes_complete_descriptor_spine() -> None:
    descriptors = default_session_tool_descriptors()
    by_id = {descriptor.id: descriptor for descriptor in descriptors}

    assert [descriptor.id for descriptor in descriptors] == HARNESS_SESSION_TOOL_IDS
    assert len(by_id) == len(HARNESS_SESSION_TOOL_IDS)
    assert {tool_id for tool_id in HARNESS_SESSION_TOOL_IDS if tool_id not in by_id} == set()

    planned_only = set()
    for tool_id in planned_only:
        descriptor = by_id[tool_id]
        assert descriptor.enabled is False
        assert descriptor.execution_supported is False
        assert descriptor.planning_only is True
        assert descriptor.disabled_reason == "not implemented yet"

    assert by_id["ls"].tool_class == "read_only_project"
    assert by_id["find"].tool_class == "read_only_project"
    assert by_id["edit"].tool_class == "active_repo_write"
    assert by_id["write"].tool_class == "active_repo_write"
    assert by_id["task"].tool_class == "execution"
    assert by_id["task-status"].tool_class == "session_local"
    assert by_id["plugin-tool"].tool_class == "extension_boundary"
    assert by_id["shell"].requires_process_supervisor is True
    assert by_id["task"].requires_runtime is True
    assert by_id["web-fetch"].feature_flag == "web_tools"
    assert "pi" in by_id["ls"].source_inspiration
    assert "opencode" in by_id["task"].source_inspiration


def test_session_tool_descriptor_lookup_and_json_round_trip() -> None:
    descriptor = get_session_tool_descriptor("grep")
    payload = descriptor.model_dump(mode="json")

    assert payload["schema_version"] == "harness.session_tool_descriptor/v1"
    assert payload["id"] == "grep"
    assert payload["permission_key"] == "tool.grep.project_files"
    assert payload["policy"]["schema_version"] == "harness.session_tool_policy_projection/v1"
    assert payload["policy"]["tool_id"] == "grep"
    assert payload["policy"]["enabled"] is True
    assert payload["policy"]["maturity"] == ["implemented"]
    assert payload["input_schema"]["properties"]["regex"]["default"] is False
    assert "shell" not in json.dumps(payload)
    assert "external_network" not in json.dumps(payload)


def test_session_tool_policy_projection_covers_representative_classes(tmp_path) -> None:
    payload = session_tool_catalog_projection(project_root=tmp_path)
    by_id = {tool["id"]: tool for tool in payload["tools"]}

    read = by_id["read"]["policy"]
    plan = by_id["plan-enter"]["policy"]
    write = by_id["write"]["policy"]
    shell = by_id["shell"]["policy"]
    web_search = by_id["web-search"]["policy"]
    mcp_resource = by_id["mcp-resource"]["policy"]
    plugin_tool = by_id["plugin-tool"]["policy"]
    task = by_id["task"]["policy"]

    assert read == {
        "schema_version": "harness.session_tool_policy_projection/v1",
        "tool_id": "read",
        "enabled": True,
        "disabled_reason": None,
        "execution_supported": True,
        "planning_only": False,
        "permission_required": False,
        "permission_key": "tool.read.project_file",
        "required_config": [],
        "required_client_capability": None,
        "required_model_capability": None,
        "boundary_kind": "local_only",
        "risk": "low",
        "replay_policy": "artifact_for_large_output",
        "policy_source": "session_tool_descriptor",
        "maturity": ["implemented"],
        "policy_reasons": [],
    }
    assert plan["boundary_kind"] == "local_only"
    assert plan["permission_required"] is False
    assert write["boundary_kind"] == "active_repo_write"
    assert write["permission_required"] is True
    assert write["risk"] == "high"
    assert shell["boundary_kind"] == "shell"
    assert shell["replay_policy"] == "rerun_forbidden"
    assert web_search["required_config"] == [
        "web_tools.enabled",
        "web_tools.search_enabled",
        "web_tools.search_provider or web_tools.search_endpoint_url",
    ]
    assert "config_missing" in web_search["maturity"]
    assert web_search["enabled"] is False
    assert mcp_resource["boundary_kind"] == "mcp"
    assert "config_missing" in mcp_resource["maturity"]
    assert plugin_tool["enabled"] is False
    assert "disabled_by_default" in plugin_tool["maturity"]
    assert plugin_tool["disabled_reason"]
    assert "adapter" in plugin_tool["disabled_reason"]
    assert task["permission_required"] is True
    assert task["required_client_capability"] == "background_tasks"
    assert task["required_model_capability"] == "tool_delegation"
    assert "client_unsupported" in task["maturity"]
    assert "model_unsupported" in task["maturity"]


def test_session_tools_cli_lists_descriptor_metadata_without_grants(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0

    result = runner.invoke(app, ["session", "tools", "--project", str(tmp_path), "--output", "json"])
    plan_only = runner.invoke(app, ["session", "tools", "--project", str(tmp_path), "--plan-only", "--output", "json"])
    inspect_one = runner.invoke(app, ["session", "tools", "--project", str(tmp_path), "--tool", "artifact-read"])
    shell = runner.invoke(app, ["session", "tools", "--project", str(tmp_path), "--tool", "shell", "--output", "json"])
    missing = runner.invoke(app, ["session", "tools", "--project", str(tmp_path), "--tool", "not-a-tool", "--output", "json"])
    confused = runner.invoke(app, ["sessions", "tools", "sess_demo", "--project", str(tmp_path), "--output", "json"])

    assert result.exit_code == 0, result.output
    assert plan_only.exit_code == 0, plan_only.output
    assert inspect_one.exit_code == 0, inspect_one.output
    assert shell.exit_code == 0, shell.output
    assert missing.exit_code == 1
    assert confused.exit_code == 1
    assert "sessions tool <session_id> <tool_id>" in json.loads(confused.output)["error"]

    payload = json.loads(result.output)
    plan_payload = json.loads(plan_only.output)
    shell_payload = json.loads(shell.output)
    assert payload["schema_version"] == "harness.session_tools/v1"
    assert payload["permission_granting"] is False
    assert {tool["id"] for tool in plan_payload["tools"]} == {
        "read",
        "glob",
        "ls",
        "find",
        "grep",
        "git-diff",
        "pwd",
        "cd",
        "artifact-read",
        "lsp-diagnostics",
        "lsp-symbols",
        "lsp-definition",
        "lsp-references",
        "policy-explain",
        "repo-overview",
        "todo",
        "question",
        "plan-enter",
        "plan-exit",
        "task-status",
        "invalid",
    }
    assert any(tool["id"] == "shell" and tool["enabled"] is True and tool["permission_required"] is True for tool in payload["tools"])
    assert any(tool["id"] == "web-search" and tool["enabled"] is True and tool["permission_required"] is True for tool in payload["tools"])
    assert any(tool["id"] == "mcp-resource" and tool["enabled"] is True and tool["permission_required"] is True for tool in payload["tools"])
    assert any(tool["id"] == "plugin-tool" and tool["enabled"] is False for tool in payload["tools"])
    assert any(tool["id"] == "skill-load" and tool["enabled"] is True and tool["permission_required"] is True for tool in payload["tools"])
    assert shell_payload["tools"][0]["id"] == "shell"
    assert shell_payload["tools"][0]["enabled"] is True
    assert shell_payload["tools"][0]["permission_required"] is True
    assert shell_payload["tools"][0]["policy"]["permission_key"] == "tool.shell.execution"
    assert shell_payload["tools"][0]["policy"]["replay_policy"] == "rerun_forbidden"
    assert any(tool["id"] == "web-fetch" and tool["enabled"] is True and tool["permission_required"] is True for tool in payload["tools"])
    web_fetch = next(tool for tool in payload["tools"] if tool["id"] == "web-fetch")
    assert "config_missing" in web_fetch["policy"]["maturity"]
    assert web_fetch["policy"]["disabled_reason"] == "Missing project configuration: web_tools.enabled, web_tools.fetch_enabled"
    assert "Descriptors are documentation and validation metadata, not permission grants." in inspect_one.output
    assert "artifact-read" in inspect_one.output


def test_unknown_session_tool_call_persists_invalid_tool_result(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    session = store.create_session(title="Invalid tool")

    result = execute_session_tool(store, tmp_path, session.id, "missing-tool", {"path": "README.md"})

    assert result.ok is False
    assert result.tool_id == "invalid"
    assert result.error_type == "invalid_tool_call"
    assert "Requested tool: missing-tool" in result.preview
    events = store.list_session_store_events(session.id)
    output = [event for event in events if event.kind == "tool_call.output"][-1]
    assert output.payload["tool_id"] == "invalid"
    assert output.payload["error_type"] == "invalid_tool_call"
    assert output.payload["ok"] is False


def test_ls_tool_lists_directory_with_cwd_filters_and_truncation(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("print('hello')\n", encoding="utf-8")
    (src / ".hidden.py").write_text("hidden\n", encoding="utf-8")
    (src / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
    (src / "pkg").mkdir()
    (src / "pkg" / "mod.py").write_text("value = 1\n", encoding="utf-8")
    store = SQLiteStore(tmp_path)
    session = store.create_session(title="List files", metadata={"cwd": "src"})

    result = execute_session_tool(store, tmp_path, session.id, "ls", {"limit": 1})

    assert result.ok is True
    assert result.tool_id == "ls"
    payload = json.loads(result.preview)
    assert payload["schema_version"] == "harness.session_tool_ls/v1"
    assert payload["target"] == "src"
    assert payload["entry_count"] == 2
    assert payload["returned_count"] == 1
    assert payload["truncated"] is True
    listed = json.dumps(payload)
    assert "src/app.py" in listed or "src/pkg" in listed
    assert ".hidden.py" not in listed
    assert ".env" not in listed
    output = [event for event in store.list_session_store_events(session.id) if event.kind == "tool_call.output"][-1]
    assert output.payload["read_only"] is True
    assert output.payload["process_started"] is False


def test_find_tool_matches_paths_without_reading_file_contents(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "session_runtime.py").write_text("needle only in file body\n", encoding="utf-8")
    (tmp_path / "src" / "other.py").write_text("session runtime appears only in body\n", encoding="utf-8")
    (tmp_path / ".hidden_runtime.py").write_text("hidden\n", encoding="utf-8")
    store = SQLiteStore(tmp_path)
    session = store.create_session(title="Find files")

    result = execute_session_tool(
        store,
        tmp_path,
        session.id,
        "find",
        {"query": "session runtime", "path": ".", "limit": 10},
    )

    assert result.ok is True
    assert result.tool_id == "find"
    payload = json.loads(result.preview)
    assert payload["schema_version"] == "harness.session_tool_find/v1"
    assert payload["content_searched"] is False
    assert payload["match_count"] == 1
    assert payload["matches"] == [{"name": "session_runtime.py", "path": "src/session_runtime.py"}]
    serialized = json.dumps(payload)
    assert "other.py" not in serialized
    assert ".hidden_runtime.py" not in serialized


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
    reloaded = SQLiteStore(tmp_path)
    events = reloaded.list_session_store_events(session.id)
    todo_event = [event for event in events if event.kind == "todo.updated"][-1]
    question_event = [event for event in events if event.kind == "question.requested"][-1]
    for event, tool_id in ((todo_event, "todo"), (question_event, "question")):
        assert event.payload["policy_boundary"] == {
            "kind": "session_local_state",
            "boundary_kind": "local_only",
            "source": f"session_{tool_id}",
        }
        assert event.payload["tool_id"] == tool_id
        assert event.payload["session_local"] is True
        assert event.payload["repository_files_modified"] is False
        assert event.payload["filesystem_modified"] is False
        assert event.payload["active_repo_modified"] is False
        assert event.payload["git_mutation_started"] is False
        assert event.payload["process_started"] is False
        assert event.payload["network_accessed"] is False
        assert event.payload["permission_granting"] is False
        assert event.payload["authority_granting"] is False
        assert event.payload["blocked_reasons"] == []
    parts = reloaded.list_session_parts(session.id)
    todo_part = [part for part in parts if part.kind.value == "todo_update"][-1]
    question_part = [part for part in parts if part.kind.value == "question"][-1]
    assert todo_part.metadata["tool_id"] == "todo"
    assert todo_part.metadata["session_local"] is True
    assert todo_part.metadata["filesystem_modified"] is False
    assert question_part.metadata["tool_id"] == "question"
    assert question_part.metadata["session_local"] is True
    assert question_part.metadata["permission_granting"] is False


def test_shell_tool_records_permission_request_without_execution(tmp_path) -> None:
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
    assert payload["result"]["error_type"] == "permission_required"
    assert payload["result"]["permission_id"]
    assert "Shell execution requires an exact normalized session-shell permission grant." in payload["result"]["preview"]

    reloaded = SQLiteStore(tmp_path)
    permission = reloaded.get_session_permission(payload["result"]["permission_id"])
    assert permission.status == SessionPermissionStatus.PENDING
    assert permission.tool_id == "shell"
    assert permission.boundary_kind == SessionPermissionBoundaryKind.SHELL
    assert reloaded.get_session(session.id).active_run_id == payload["result"]["run_id"]

    tail = runner.invoke(app, ["session", "tail", session.id, "--project", str(tmp_path), "--limit", "20"])
    assert tail.exit_code == 0, tail.output
    assert "Tool started" in tail.output
    assert "Permission checked" in tail.output
    assert "Permission requested" in tail.output
    assert "Permission resolved" not in tail.output
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
    assert plan_payload["policy_boundary"] == {
        "kind": "patch_apply_back_deferred",
        "boundary_kind": "active_repo_write",
        "source": "session_tool_patch_plan",
    }
    assert plan_payload["approval_required"] is True
    assert plan_payload["required_approval"] == "active_repo_write"
    assert plan_payload["apply_back_required"] is True
    assert plan_payload["snapshot_required"] is True
    assert plan_payload["apply_supported"] is False
    assert plan_payload["patch_apply_supported"] is False
    assert plan_payload["applies_to_active_workspace"] is False
    assert plan_payload["file_written"] is False
    assert plan_payload["filesystem_modified"] is False
    assert plan_payload["active_repo_modified"] is False
    assert plan_payload["git_mutation_started"] is False
    assert plan_payload["permission_granting"] is False
    assert plan_payload["authority_granting"] is False
    assert plan_payload["blocked_reasons"] == [
        "patch_apply_disabled",
        "requires_interactive_permission",
        "requires_snapshot_apply_back",
    ]
    assert plan.metadata["policy_boundary"] == plan_payload["policy_boundary"]
    assert plan.metadata["apply_back_required"] is True
    assert plan.metadata["filesystem_modified"] is False
    patch_artifact = next(artifact for artifact in artifacts if artifact.kind == "session_tool_patch")
    assert patch_artifact.metadata["files"] == ["app.py"]
    assert patch_artifact.metadata["applies_to_active_workspace"] is False
    assert patch_artifact.metadata["git_mutation_started"] is False
    assert SQLiteStore(tmp_path).get_session_permission(first_payload["permission_id"]).status == SessionPermissionStatus.EXPIRED

    repeated = runner.invoke(
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

    assert repeated.exit_code == 0, repeated.output
    repeated_payload = json.loads(repeated.output)["result"]
    assert repeated_payload["ok"] is False
    assert repeated_payload["error_type"] == "permission_required"
    assert repeated_payload["permission_id"] != first_payload["permission_id"]


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
    assert plan_payload["policy_boundary"] == {
        "kind": "direct_write_deferred",
        "boundary_kind": "active_repo_write",
        "source": "session_tool_direct_write_plan",
    }
    assert plan_payload["approval_required"] is True
    assert plan_payload["required_approval"] == "active_repo_write"
    assert plan_payload["blocked_path_checks"] is True
    assert plan_payload["write_supported"] is False
    assert plan_payload["direct_write_supported"] is False
    assert plan_payload["apply_supported"] is False
    assert plan_payload["file_written"] is False
    assert plan_payload["filesystem_modified"] is False
    assert plan_payload["active_repo_modified"] is False
    assert plan_payload["git_mutation_started"] is False
    assert plan_payload["permission_granting"] is False
    assert plan_payload["blocked_reasons"] == [
        "direct_write_apply_disabled",
        "requires_interactive_permission",
        "blocked_path_checks_required",
    ]
    assert plan.metadata["policy_boundary"] == plan_payload["policy_boundary"]
    assert plan.metadata["blocked_path_checks"] is True
    assert plan.metadata["filesystem_modified"] is False
    assert plan.metadata["active_repo_modified"] is False
    content_artifact = next(artifact for artifact in artifacts if artifact.kind == "session_tool_direct_write_content")
    assert content_artifact.metadata["target"] == "notes.txt"
    assert content_artifact.metadata["file_written"] is False
    assert content_artifact.metadata["filesystem_modified"] is False
    assert content_artifact.metadata["policy_boundary"] == plan_payload["policy_boundary"]
    assert SQLiteStore(tmp_path).get_session_permission(first_payload["permission_id"]).status == SessionPermissionStatus.EXPIRED

    repeated = runner.invoke(
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

    assert repeated.exit_code == 0, repeated.output
    repeated_payload = json.loads(repeated.output)["result"]
    assert repeated_payload["ok"] is False
    assert repeated_payload["error_type"] == "permission_required"
    assert repeated_payload["permission_id"] != first_payload["permission_id"]


def test_edit_tool_requires_permission_then_applies_exact_replacement(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    target = tmp_path / "app.py"
    target.write_text("value = 1\n", encoding="utf-8")
    store = SQLiteStore(tmp_path)
    session = store.create_session(title="Edit apply")
    args = {"path": "app.py", "old": "value = 1", "new": "value = 2", "expected_replacements": 1}

    first = runner.invoke(
        app,
        [
            "session",
            "tool",
            session.id,
            "edit",
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
    assert target.read_text(encoding="utf-8") == "value = 1\n"
    pending = SQLiteStore(tmp_path).get_session_permission(first_payload["permission_id"])
    assert pending.tool_id == "edit"
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
            "edit",
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
    assert "edit applied." in second_payload["preview"]
    assert target.read_text(encoding="utf-8") == "value = 2\n"
    artifacts = SQLiteStore(tmp_path).list_artifacts(second_payload["run_id"])
    kinds = {artifact.kind for artifact in artifacts}
    assert {"session_tool_edit_diff", "session_tool_edit_mutation"} <= kinds
    metadata = next(artifact for artifact in artifacts if artifact.kind == "session_tool_edit_mutation")
    metadata_payload = json.loads(metadata.path.read_text(encoding="utf-8"))
    assert metadata_payload["applied"] is True
    assert metadata_payload["target"] == "app.py"
    assert metadata_payload["operation"]["kind"] == "replace"
    assert metadata_payload["operation"]["actual_replacements"] == 1
    assert metadata_payload["filesystem_modified"] is True
    assert metadata_payload["active_repo_modified"] is True
    assert metadata_payload["git_mutation_started"] is False
    assert SQLiteStore(tmp_path).get_session_permission(first_payload["permission_id"]).status == SessionPermissionStatus.EXPIRED


def test_write_tool_supports_plan_mode_and_permissioned_apply(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    session = store.create_session(title="Write apply")

    plan = runner.invoke(
        app,
        [
            "session",
            "tool",
            session.id,
            "write",
            "--project",
            str(tmp_path),
            "--input-json",
            json.dumps({"path": "docs/new.md", "content": "# Planned\n", "mode": "plan", "create_dirs": True}),
            "--output",
            "json",
        ],
    )
    assert plan.exit_code == 0, plan.output
    plan_payload = json.loads(plan.output)["result"]
    assert plan_payload["ok"] is True
    assert "write planned." in plan_payload["preview"]
    assert not (tmp_path / "docs" / "new.md").exists()
    plan_artifacts = SQLiteStore(tmp_path).list_artifacts(plan_payload["run_id"])
    plan_metadata = next(artifact for artifact in plan_artifacts if artifact.kind == "session_tool_write_mutation")
    assert json.loads(plan_metadata.path.read_text(encoding="utf-8"))["applied"] is False

    args = {"path": "docs/new.md", "content": "# Applied\n", "create_dirs": True}
    first = runner.invoke(
        app,
        [
            "session",
            "tool",
            session.id,
            "write",
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
    assert not (tmp_path / "docs" / "new.md").exists()

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
            "write",
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
    assert "write applied." in second_payload["preview"]
    assert (tmp_path / "docs" / "new.md").read_text(encoding="utf-8") == "# Applied\n"
    artifacts = SQLiteStore(tmp_path).list_artifacts(second_payload["run_id"])
    metadata = next(artifact for artifact in artifacts if artifact.kind == "session_tool_write_mutation")
    metadata_payload = json.loads(metadata.path.read_text(encoding="utf-8"))
    assert metadata_payload["applied"] is True
    assert metadata_payload["operation"]["kind"] == "full_file_write"
    assert metadata_payload["operation"]["created"] is True
    assert metadata_payload["filesystem_modified"] is True
    assert metadata_payload["active_repo_modified"] is True


def test_fs_write_file_alias_denies_external_paths_without_permission_prompt(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    session = store.create_session(title="External write")
    external = tmp_path.parent / "outside-downloads-style.txt"

    result = runner.invoke(
        app,
        [
            "session",
            "tool",
            session.id,
            "fs.write_file",
            "--project",
            str(tmp_path),
            "--input-json",
            json.dumps({"path": str(external), "content": ""}),
            "--output",
            "json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)["result"]
    assert payload["ok"] is False
    assert payload["error_type"] == "permission_denied"
    assert payload["permission_id"]
    assert "Path escapes project root" in payload["preview"]
    assert not external.exists()
    permission = SQLiteStore(tmp_path).get_session_permission(payload["permission_id"])
    assert permission.status == SessionPermissionStatus.DENIED


def test_fs_write_file_alias_records_plan_without_active_repo_prompt(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    session = store.create_session(title="Planned write alias")
    target = tmp_path / "notes.md"

    result = runner.invoke(
        app,
        [
            "session",
            "tool",
            session.id,
            "fs.write_file",
            "--project",
            str(tmp_path),
            "--input-json",
            json.dumps({"path": "notes.md", "content": "hello\n"}),
            "--output",
            "json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)["result"]
    assert payload["ok"] is True
    assert payload["error_type"] is None
    assert payload["permission_id"] is None
    assert "write planned." in payload["preview"]
    assert not target.exists()


def test_plan_enter_exit_tools_update_session_metadata_and_events(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    session = store.create_session(title="Planning mode")

    entered = execute_session_tool(
        store,
        tmp_path,
        session.id,
        "plan-enter",
        {"reason": "inspect before editing"},
    )

    assert entered.ok is True
    assert entered.tool_id == "plan-enter"
    assert "Planning mode entered." in entered.preview
    entered_session = SQLiteStore(tmp_path).get_session(session.id)
    entered_state = entered_session.metadata["planning_mode"]
    assert entered_state["active"] is True
    assert entered_state["reason"] == "inspect before editing"
    assert entered_state["summary"] is None
    events = SQLiteStore(tmp_path).list_session_store_events(session.id)
    assert "session.planning_mode.entered" in [event.kind for event in events]

    exited = execute_session_tool(
        SQLiteStore(tmp_path),
        tmp_path,
        session.id,
        "plan-exit",
        {
            "summary": "Need a targeted edit.",
            "next_action": "apply edit after approval",
            "proposed_tools": ["edit", "grep"],
        },
    )

    assert exited.ok is True
    assert "Planning mode exited." in exited.preview
    exited_session = SQLiteStore(tmp_path).get_session(session.id)
    exited_state = exited_session.metadata["planning_mode"]
    assert exited_state["active"] is False
    assert exited_state["reason"] == "inspect before editing"
    assert exited_state["summary"] == "Need a targeted edit."
    assert exited_state["next_action"] == "apply edit after approval"
    assert exited_state["proposed_tools"] == ["edit", "grep"]
    events = SQLiteStore(tmp_path).list_session_store_events(session.id)
    assert "session.planning_mode.exited" in [event.kind for event in events]


def test_planning_mode_forces_edit_to_plan_without_mutating_unless_approved(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    target = tmp_path / "app.py"
    target.write_text("value = 1\n", encoding="utf-8")
    store = SQLiteStore(tmp_path)
    session = store.create_session(title="Planning edit")

    entered = execute_session_tool(store, tmp_path, session.id, "plan-enter", {"reason": "read only pass"})
    planned = execute_session_tool(
        SQLiteStore(tmp_path),
        tmp_path,
        session.id,
        "edit",
        {"path": "app.py", "old": "value = 1", "new": "value = 2", "expected_replacements": 1},
    )

    assert entered.ok is True
    assert planned.ok is True
    assert "edit planned." in planned.preview
    assert target.read_text(encoding="utf-8") == "value = 1\n"
    planned_artifacts = SQLiteStore(tmp_path).list_artifacts(planned.run_id)
    planned_metadata = next(artifact for artifact in planned_artifacts if artifact.kind == "session_tool_edit_mutation")
    assert json.loads(planned_metadata.path.read_text(encoding="utf-8"))["applied"] is False

    permission_store = SQLiteStore(tmp_path)
    permission = permission_store.request_session_permission(
        session.id,
        tool_id="edit",
        normalized_action="edit",
        normalized_target_pattern="app.py",
        boundary_kind=SessionPermissionBoundaryKind.ACTIVE_REPO_WRITE,
        risk="high",
        scope=SessionPermissionScope.ONCE,
        policy_reasons=["test approval"],
    )
    permission_store.resolve_session_permission(permission.id, SessionPermissionStatus.ALLOWED, reason="test approval")
    applied = execute_session_tool(
        SQLiteStore(tmp_path),
        tmp_path,
        session.id,
        "edit",
        {"path": "app.py", "old": "value = 1", "new": "value = 2", "expected_replacements": 1},
    )

    assert applied.ok is True
    assert "edit applied." in applied.preview
    assert target.read_text(encoding="utf-8") == "value = 2\n"
    assert SQLiteStore(tmp_path).get_session_permission(permission.id).status == SessionPermissionStatus.EXPIRED


def test_task_tool_requires_permission_then_persists_child_task_linkage(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    session = store.create_session(title="Delegate work")
    args = {
        "objective": "Inspect the repository layout and report likely entrypoints.",
        "allowed_tools": ["read", "glob", "grep"],
        "boundary": "read_only_project",
        "output_expectation": "Short markdown summary with file references.",
        "agent": "repo_inspector",
    }

    first = runner.invoke(
        app,
        [
            "session",
            "tool",
            session.id,
            "task",
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
    assert SQLiteStore(tmp_path).list_child_sessions(session.id) == []

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
            "task",
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
    result = json.loads(second_payload["preview"])
    assert result["schema_version"] == "harness.session_tool_task/v1"
    assert result["created"] is True
    assert result["execution_started"] is False
    assert result["process_started"] is False
    task = SQLiteStore(tmp_path).get_task(result["task_id"])
    child = SQLiteStore(tmp_path).get_session(result["child_session_id"])
    parent = SQLiteStore(tmp_path).get_session(session.id)
    assert task.session_id == child.id
    assert child.parent_session_id == session.id
    assert child.active_task_id == task.id
    assert parent.active_task_id == task.id
    assert task.metadata["parent_session_id"] == session.id
    assert task.metadata["child_session_id"] == child.id
    assert task.metadata["allowed_tools"] == ["read", "glob", "grep"]
    assert task.metadata["execution_started"] is False
    events = SQLiteStore(tmp_path).list_session_store_events(session.id)
    assert "session.task.created" in [event.kind for event in events]
    task_events = SQLiteStore(tmp_path).list_store_events("task", task.id)
    assert "task.created_by_session_tool" in [event.kind for event in task_events]
    artifacts = SQLiteStore(tmp_path).list_artifacts(second_payload["run_id"])
    assert any(artifact.kind == "session_tool_task_plan" for artifact in artifacts)
    assert SQLiteStore(tmp_path).get_session_permission(first_payload["permission_id"]).status == SessionPermissionStatus.EXPIRED


def test_task_status_tool_reads_linked_task_without_permission(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    session = store.create_session(title="Task status")
    child = store.fork_session(session.id, title="Delegated child")
    task = store.create_task(
        "Inspect status",
        description="Read status",
        agent_id="repo_inspector",
        metadata={
            "task_type": "session_delegate",
            "execution_adapter": "session_child_task",
            "parent_session_id": session.id,
            "child_session_id": child.id,
            "allowed_tools": ["read"],
        },
        session_id=child.id,
    )
    store.update_session(session.id, active_task_id=task.id)
    store.append_store_event(
        "task",
        task.id,
        "task.created_by_session_tool",
        {"task_id": task.id, "summary": "created"},
        session_id=child.id,
        task_id=task.id,
    )

    status = execute_session_tool(SQLiteStore(tmp_path), tmp_path, session.id, "task-status", {"task_id": task.id})

    assert status.ok is True
    payload = json.loads(status.preview)
    assert payload["schema_version"] == "harness.session_tool_task_status/v1"
    assert payload["task"]["id"] == task.id
    assert payload["child_session"]["id"] == child.id
    assert payload["execution_started"] is False
    assert payload["process_started"] is False
    assert payload["permission_granting"] is False
    assert "task.created_by_session_tool" in [event["kind"] for event in payload["task_events"]]


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
    permissions = SQLiteStore(tmp_path).list_session_permissions(session.id)
    assert permissions[0].status == SessionPermissionStatus.DENIED
    assert permissions[0].normalized_action == "write"
    assert permissions[0].normalized_target_pattern == ".harness/config.json"
    assert permissions[0].boundary_kind == SessionPermissionBoundaryKind.ACTIVE_REPO_WRITE
    assert permissions[0].policy_reasons == ["Blocked write path: .harness/config.json"]
    events = SQLiteStore(tmp_path).list_session_store_events(session.id)
    permission_checked = [event for event in events if event.kind == "permission.checked"][-1]
    assert permission_checked.payload["decision"] == "deny"
    assert permission_checked.payload["action"] == "write"
    assert permission_checked.payload["target"] == ".harness/config.json"
    assert permission_checked.payload["boundary_kind"] == "active_repo_write"
    assert permission_checked.payload["reasons"] == ["Blocked write path: .harness/config.json"]


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
    assert SQLiteStore(tmp_path).get_session_permission(first_payload["permission_id"]).status == SessionPermissionStatus.EXPIRED

    repeated = runner.invoke(
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

    assert repeated.exit_code == 0, repeated.output
    repeated_payload = json.loads(repeated.output)["result"]
    assert repeated_payload["ok"] is False
    assert repeated_payload["error_type"] == "permission_required"
    assert repeated_payload["permission_id"] != first_payload["permission_id"]


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


def test_skill_load_tool_requires_permission_then_loads_configured_project_skill(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    skill_dir = tmp_path / "skills" / "reviewer"
    skill_dir.mkdir(parents=True)
    skill_dir.joinpath("SKILL.md").write_text(
        "# Reviewer\n\nUse project-local review workflow.\n",
        encoding="utf-8",
    )
    config_path = tmp_path / ".harness" / "config.yaml"
    config_data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config_data["skills"] = {
        "enabled": True,
        "project": {
            "reviewer": {
                "path": "skills/reviewer",
                "spec": "./skills/reviewer",
                "version": "0.1.0",
                "enabled": True,
                "description": "Project review skill",
            }
        },
    }
    config_path.write_text(yaml.safe_dump(config_data, sort_keys=False), encoding="utf-8")
    store = SQLiteStore(tmp_path)
    session = store.create_session(title="Skill load")

    first = runner.invoke(
        app,
        [
            "session",
            "tool",
            session.id,
            "skill-load",
            "--project",
            str(tmp_path),
            "--input-json",
            json.dumps({"skill": "reviewer"}),
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
    assert pending.tool_id == "skill-load"
    assert pending.normalized_target_pattern == "reviewer"

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
            "skill-load",
            "--project",
            str(tmp_path),
            "--input-json",
            json.dumps({"skill": "reviewer"}),
            "--output",
            "json",
        ],
    )

    assert allowed.exit_code == 0, allowed.output
    assert second.exit_code == 0, second.output
    second_payload = json.loads(second.output)["result"]
    assert second_payload["ok"] is True
    assert '<skill_content name="reviewer">' in second_payload["preview"]
    assert "Use project-local review workflow." in second_payload["preview"]
    assert "Content artifact:" in second_payload["preview"]
    assert "Metadata artifact:" in second_payload["preview"]
    assert "No plugin tools were registered" in second_payload["preview"]
    assert SQLiteStore(tmp_path).get_run(second_payload["run_id"]).session_id == session.id
    artifacts = SQLiteStore(tmp_path).list_artifacts(second_payload["run_id"])
    content_artifact = next(artifact for artifact in artifacts if artifact.kind == "session_tool_skill_load_content")
    metadata_artifact = next(artifact for artifact in artifacts if artifact.kind == "session_tool_skill_load_metadata")
    metadata = json.loads(metadata_artifact.path.read_text(encoding="utf-8"))
    assert content_artifact.path.read_text(encoding="utf-8").startswith("# Reviewer")
    assert metadata["schema_version"] == "harness.session_tool_skill_load/v1"
    assert metadata["skill"] == "reviewer"
    assert metadata["description"] == "Project review skill"
    assert metadata["version"] == "0.1.0"
    assert metadata["origin"] == "project_config"
    assert metadata["source_kind"] == "project_config"
    assert metadata["path"] == "skills/reviewer"
    assert metadata["skill_file_path"] == "skills/reviewer/SKILL.md"
    assert metadata["content_artifact_id"] == content_artifact.id
    assert metadata["content_sha256"]
    assert metadata["loaded_sections"] == ["Reviewer"]
    assert metadata["allowed_scope"] == "configured_project_skill_body"
    assert metadata["runtime_loaded"] is False
    assert metadata["tool_registered"] is False
    assert metadata["plugin_tools_registered"] is False
    assert metadata["network_called"] is False
    assert metadata["filesystem_modified"] is False


def test_web_fetch_tool_requires_permission_then_fetches_and_persists_response_artifacts(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    server, url = _start_fetch_server(
        b"<html><body><h1>Harness Docs</h1><p>Fetched content.</p><script>secret()</script></body></html>",
        "text/html; charset=utf-8",
    )
    config_path = tmp_path / ".harness" / "config.yaml"
    config_data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config_data["web_tools"] = {
        "enabled": True,
        "fetch_enabled": True,
        "search_enabled": False,
        "approval_required": True,
        "allowed_domains": ["127.0.0.1"],
    }
    config_path.write_text(yaml.safe_dump(config_data, sort_keys=False), encoding="utf-8")
    store = SQLiteStore(tmp_path)
    session = store.create_session(title="Web fetch")
    arguments = {"url": url, "format": "markdown", "timeout": 5}

    try:
        first = runner.invoke(
            app,
            [
                "session",
                "tool",
                session.id,
                "web-fetch",
                "--project",
                str(tmp_path),
                "--input-json",
                json.dumps(arguments),
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
        assert pending.tool_id == "web-fetch"
        assert pending.boundary_kind == SessionPermissionBoundaryKind.EXTERNAL_NETWORK
        assert pending.normalized_target_pattern.startswith("http://127.0.0.1:")

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
                "web-fetch",
                "--project",
                str(tmp_path),
                "--input-json",
                json.dumps(arguments),
                "--output",
                "json",
            ],
        )
    finally:
        server.shutdown()
        server.server_close()

    assert allowed.exit_code == 0, allowed.output
    assert second.exit_code == 0, second.output
    second_payload = json.loads(second.output)["result"]
    assert second_payload["ok"] is True
    assert "Web fetch executed." in second_payload["preview"]
    assert "Harness Docs" in second_payload["preview"]
    assert "Fetched content." in second_payload["preview"]
    assert "secret()" not in second_payload["preview"]
    artifacts = SQLiteStore(tmp_path).list_artifacts(second_payload["run_id"])
    content_artifact = next(artifact for artifact in artifacts if artifact.kind == "session_tool_web_fetch_content")
    metadata_artifact = next(artifact for artifact in artifacts if artifact.kind == "session_tool_web_fetch_metadata")
    content = content_artifact.path.read_text(encoding="utf-8")
    metadata = json.loads(metadata_artifact.path.read_text(encoding="utf-8"))
    assert "Harness Docs" in content
    assert "Fetched content." in content
    assert "secret()" not in content
    assert metadata["schema_version"] == "harness.session_tool_web_fetch_plan/v1"
    assert metadata["url"] == url
    assert metadata["requires_network"] is True
    assert metadata["permission_boundary"]["kind"] == "external_network_fetch"
    assert metadata["permission_boundary"]["boundary_kind"] == "external_network"
    assert metadata["permission_boundary"]["approval_required"] is True
    assert metadata["permission_boundary"]["host"] == "127.0.0.1"
    assert metadata["permission_boundary"]["provider"] == "urllib"
    assert metadata["network_called"] is True
    assert metadata["fetch_executed"] is True
    assert metadata["status_code"] == 200
    assert metadata["content_artifact_id"] == content_artifact.id
    assert SQLiteStore(tmp_path).get_session_permission(first_payload["permission_id"]).status == SessionPermissionStatus.EXPIRED

    repeated = runner.invoke(
        app,
        [
            "session",
            "tool",
            session.id,
            "web-fetch",
            "--project",
            str(tmp_path),
            "--input-json",
            json.dumps(arguments),
            "--output",
            "json",
        ],
    )

    assert repeated.exit_code == 0, repeated.output
    repeated_payload = json.loads(repeated.output)["result"]
    assert repeated_payload["ok"] is False
    assert repeated_payload["error_type"] == "permission_required"
    assert repeated_payload["permission_id"] != first_payload["permission_id"]


def test_web_fetch_tool_denies_disallowed_domain_without_network(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    config_path = tmp_path / ".harness" / "config.yaml"
    config_data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config_data["web_tools"] = {
        "enabled": True,
        "fetch_enabled": True,
        "search_enabled": False,
        "approval_required": True,
        "allowed_domains": ["docs.example.com"],
    }
    config_path.write_text(yaml.safe_dump(config_data, sort_keys=False), encoding="utf-8")
    session = SQLiteStore(tmp_path).create_session(title="Web fetch denied")

    result = runner.invoke(
        app,
        [
            "session",
            "tool",
            session.id,
            "web-fetch",
            "--project",
            str(tmp_path),
            "--input-json",
            json.dumps({"url": "https://example.org/page"}),
            "--output",
            "json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)["result"]
    assert payload["ok"] is False
    assert payload["error_type"] == "permission_denied"
    assert "Web fetch host is not allowed" in payload["preview"]
    permission = SQLiteStore(tmp_path).get_session_permission(payload["permission_id"])
    assert permission.status == SessionPermissionStatus.DENIED
    assert permission.boundary_kind == SessionPermissionBoundaryKind.EXTERNAL_NETWORK


def test_mcp_resource_tool_requires_permission_then_reads_cached_resource_without_process_or_network(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    resource_path = tmp_path / "mcp-cache" / "guide.md"
    resource_path.parent.mkdir()
    resource_path.write_text("# Cached Guide\n\nUse the audited path.\n", encoding="utf-8")
    config_path = tmp_path / ".harness" / "config.yaml"
    config_data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config_data["mcp"] = {
        "enabled": True,
        "servers": {
            "docs": {
                "kind": "local",
                "enabled": True,
                "command": ["mcp-docs", "--stdio"],
                "resources": {
                    "guide": {
                        "uri": "mcp://docs/guide",
                        "path": "mcp-cache/guide.md",
                        "enabled": True,
                        "content_type": "text/markdown",
                        "description": "Cached docs guide",
                    }
                },
            }
        },
    }
    config_path.write_text(yaml.safe_dump(config_data, sort_keys=False), encoding="utf-8")
    store = SQLiteStore(tmp_path)
    session = store.create_session(title="MCP resource")
    arguments = {"server": "docs", "uri": "mcp://docs/guide"}

    first = runner.invoke(
        app,
        [
            "session",
            "tool",
            session.id,
            "mcp-resource",
            "--project",
            str(tmp_path),
            "--input-json",
            json.dumps(arguments),
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
    assert pending.tool_id == "mcp-resource"
    assert pending.boundary_kind == SessionPermissionBoundaryKind.MCP
    assert pending.normalized_target_pattern == "docs:mcp://docs/guide"

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
            "mcp-resource",
            "--project",
            str(tmp_path),
            "--input-json",
            json.dumps(arguments),
            "--output",
            "json",
        ],
    )

    assert allowed.exit_code == 0, allowed.output
    assert second.exit_code == 0, second.output
    second_payload = json.loads(second.output)["result"]
    assert second_payload["ok"] is True
    assert "MCP resource read from cache." in second_payload["preview"]
    assert "Cached Guide" in second_payload["preview"]
    artifacts = SQLiteStore(tmp_path).list_artifacts(second_payload["run_id"])
    content_artifact = next(artifact for artifact in artifacts if artifact.kind == "session_tool_mcp_resource_content")
    metadata_artifact = next(artifact for artifact in artifacts if artifact.kind == "session_tool_mcp_resource_metadata")
    metadata = json.loads(metadata_artifact.path.read_text(encoding="utf-8"))
    assert content_artifact.path.read_text(encoding="utf-8").startswith("# Cached Guide")
    assert metadata["schema_version"] == "harness.session_tool_mcp_resource/v1"
    assert metadata["server"] == "docs"
    assert metadata["uri"] == "mcp://docs/guide"
    assert metadata["content_artifact_id"] == content_artifact.id
    assert metadata["cached_only"] is True
    assert metadata["origin"] == "project_config_cached_resource"
    assert metadata["server_kind"] == "local"
    assert metadata["server_command_configured"] is True
    assert metadata["server_url_configured"] is False
    assert metadata["allowed_scope"] == "configured_cached_resource"
    assert metadata["content_sha256"]
    assert metadata["process_started"] is False
    assert metadata["network_called"] is False


def test_mcp_resource_tool_denies_unconfigured_resource_without_process_or_network(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    session = SQLiteStore(tmp_path).create_session(title="MCP resource denied")

    result = runner.invoke(
        app,
        [
            "session",
            "tool",
            session.id,
            "mcp-resource",
            "--project",
            str(tmp_path),
            "--input-json",
            json.dumps({"server": "missing", "uri": "mcp://missing/resource"}),
            "--output",
            "json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)["result"]
    assert payload["ok"] is False
    assert payload["error_type"] == "permission_denied"
    assert "Configured MCP server not found" in payload["preview"]
    permission = SQLiteStore(tmp_path).get_session_permission(payload["permission_id"])
    assert permission.status == SessionPermissionStatus.DENIED
    assert permission.tool_id == "mcp-resource"


def test_web_search_tool_requires_permission_then_executes_configured_endpoint_and_persists_artifacts(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    server, endpoint = _start_fetch_server(
        json.dumps(
            {
                "results": [
                    {
                        "title": "Harness permissions",
                        "url": "https://docs.example.com/permissions",
                        "snippet": "Session permissions are auditable.",
                    }
                ]
            }
        ).encode("utf-8"),
        "application/json; charset=utf-8",
    )
    config_path = tmp_path / ".harness" / "config.yaml"
    config_data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config_data["web_tools"] = {
        "enabled": True,
        "fetch_enabled": False,
        "search_enabled": True,
        "approval_required": True,
        "allowed_domains": ["docs.example.com"],
        "search_endpoint_url": endpoint,
    }
    config_path.write_text(yaml.safe_dump(config_data, sort_keys=False), encoding="utf-8")
    store = SQLiteStore(tmp_path)
    session = store.create_session(title="Web search")
    arguments = {
        "query": "Harness session permissions 2026",
        "num_results": 6,
        "search_type": "fast",
        "livecrawl": "fallback",
        "context_max_characters": 12000,
        "allowed_domains": ["docs.example.com"],
    }

    try:
        first = runner.invoke(
            app,
            [
                "session",
                "tool",
                session.id,
                "web-search",
                "--project",
                str(tmp_path),
                "--input-json",
                json.dumps(arguments),
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
        assert pending.tool_id == "web-search"
        assert pending.boundary_kind == SessionPermissionBoundaryKind.EXTERNAL_NETWORK
        assert pending.normalized_target_pattern == "Harness session permissions 2026"

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
                "web-search",
                "--project",
                str(tmp_path),
                "--input-json",
                json.dumps(arguments),
                "--output",
                "json",
            ],
        )
    finally:
        server.shutdown()
        server.server_close()

    assert allowed.exit_code == 0, allowed.output
    assert second.exit_code == 0, second.output
    second_payload = json.loads(second.output)["result"]
    assert second_payload["ok"] is True
    assert "Web search executed." in second_payload["preview"]
    assert "Harness permissions" in second_payload["preview"]
    artifacts = SQLiteStore(tmp_path).list_artifacts(second_payload["run_id"])
    results_artifact = next(artifact for artifact in artifacts if artifact.kind == "session_tool_web_search_results")
    metadata_artifact = next(artifact for artifact in artifacts if artifact.kind == "session_tool_web_search_metadata")
    results = results_artifact.path.read_text(encoding="utf-8")
    metadata = json.loads(metadata_artifact.path.read_text(encoding="utf-8"))
    assert "Harness permissions" in results
    assert metadata["schema_version"] == "harness.session_tool_web_search_plan/v1"
    assert metadata["query"] == "Harness session permissions 2026"
    assert metadata["num_results"] == 6
    assert metadata["search_type"] == "fast"
    assert metadata["allowed_domains"] == ["docs.example.com"]
    assert metadata["requires_network"] is True
    assert metadata["permission_boundary"]["kind"] == "external_network_search"
    assert metadata["permission_boundary"]["boundary_kind"] == "external_network"
    assert metadata["permission_boundary"]["provider"] == "configured_http"
    assert metadata["permission_boundary"]["approval_required"] is True
    assert metadata["permission_boundary"]["allowed_domains"] == ["docs.example.com"]
    assert metadata["network_called"] is True
    assert metadata["search_executed"] is True
    assert metadata["results_artifact_id"] == results_artifact.id
    assert SQLiteStore(tmp_path).get_session_permission(first_payload["permission_id"]).status == SessionPermissionStatus.EXPIRED

    repeated = runner.invoke(
        app,
        [
            "session",
            "tool",
            session.id,
            "web-search",
            "--project",
            str(tmp_path),
            "--input-json",
            json.dumps(arguments),
            "--output",
            "json",
        ],
    )

    assert repeated.exit_code == 0, repeated.output
    repeated_payload = json.loads(repeated.output)["result"]
    assert repeated_payload["ok"] is False
    assert repeated_payload["error_type"] == "permission_required"
    assert repeated_payload["permission_id"] != first_payload["permission_id"]


def test_web_search_tool_executes_exa_mcp_provider_after_exact_approval(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    mcp_payload = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "content": [
                    {
                        "type": "text",
                        "text": "Title: Italy current event\nURL: https://news.example/italy\nHighlights:\nMCP result text.",
                    }
                ]
            },
        }
    ).encode("utf-8")
    server, endpoint, requests = _start_mcp_search_server(b"event: message\ndata: " + mcp_payload + b"\n\n")
    config_path = tmp_path / ".harness" / "config.yaml"
    config_data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config_data["web_tools"] = {
        "enabled": True,
        "fetch_enabled": False,
        "search_enabled": True,
        "approval_required": True,
        "search_provider": "exa_mcp",
        "search_endpoint_url": endpoint,
    }
    config_path.write_text(yaml.safe_dump(config_data, sort_keys=False), encoding="utf-8")
    store = SQLiteStore(tmp_path)
    session = store.create_session(title="MCP web search")
    arguments = {"query": "Italy tragic news today 2026", "num_results": 2, "search_type": "fast"}

    try:
        first = runner.invoke(
            app,
            [
                "session",
                "tool",
                session.id,
                "web-search",
                "--project",
                str(tmp_path),
                "--input-json",
                json.dumps(arguments),
                "--output",
                "json",
            ],
        )
        assert first.exit_code == 0, first.output
        first_payload = json.loads(first.output)["result"]
        assert first_payload["ok"] is False
        assert first_payload["error_type"] == "permission_required"

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
                "web-search",
                "--project",
                str(tmp_path),
                "--input-json",
                json.dumps(arguments),
                "--output",
                "json",
            ],
        )
    finally:
        server.shutdown()
        server.server_close()

    assert allowed.exit_code == 0, allowed.output
    assert second.exit_code == 0, second.output
    second_payload = json.loads(second.output)["result"]
    assert second_payload["ok"] is True
    assert "Web search executed." in second_payload["preview"]
    assert "MCP result text." in second_payload["preview"]
    assert len(requests) == 1
    request_payload = json.loads(requests[0]["body"])
    assert request_payload["method"] == "tools/call"
    assert request_payload["params"]["name"] == "web_search_exa"
    assert request_payload["params"]["arguments"]["query"] == "Italy tragic news today 2026"
    assert request_payload["params"]["arguments"]["numResults"] == 2
    metadata_artifact = next(
        artifact
        for artifact in SQLiteStore(tmp_path).list_artifacts(second_payload["run_id"])
        if artifact.kind == "session_tool_web_search_metadata"
    )
    metadata = json.loads(metadata_artifact.path.read_text(encoding="utf-8"))
    assert metadata["provider"] == "exa_mcp"
    assert metadata["network_called"] is True
    assert SQLiteStore(tmp_path).get_session_permission(first_payload["permission_id"]).status == SessionPermissionStatus.EXPIRED


def test_web_search_mcp_provider_rejects_domain_filter_that_cannot_be_enforced(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    config_path = tmp_path / ".harness" / "config.yaml"
    config_data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config_data["web_tools"] = {
        "enabled": True,
        "fetch_enabled": False,
        "search_enabled": True,
        "approval_required": True,
        "allowed_domains": ["docs.example.com"],
        "search_provider": "exa_mcp",
    }
    config_path.write_text(yaml.safe_dump(config_data, sort_keys=False), encoding="utf-8")
    store = SQLiteStore(tmp_path)
    session = store.create_session(title="MCP web search")

    result = runner.invoke(
        app,
        [
            "session",
            "tool",
            session.id,
            "web-search",
            "--project",
            str(tmp_path),
            "--input-json",
            json.dumps({"query": "Harness docs"}),
            "--output",
            "json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)["result"]
    assert payload["ok"] is False
    assert payload["error_type"] == "permission_denied"
    assert "does not support Harness allowed_domains enforcement" in payload["preview"]


def test_web_search_tool_denies_unapproved_domain_filter_without_network(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    config_path = tmp_path / ".harness" / "config.yaml"
    config_data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config_data["web_tools"] = {
        "enabled": True,
        "fetch_enabled": False,
        "search_enabled": True,
        "approval_required": True,
        "allowed_domains": ["docs.example.com"],
        "search_endpoint_url": "http://127.0.0.1:9/search",
    }
    config_path.write_text(yaml.safe_dump(config_data, sort_keys=False), encoding="utf-8")
    session = SQLiteStore(tmp_path).create_session(title="Web search denied")

    result = runner.invoke(
        app,
        [
            "session",
            "tool",
            session.id,
            "web-search",
            "--project",
            str(tmp_path),
            "--input-json",
            json.dumps({"query": "Harness docs", "allowed_domains": ["example.org"]}),
            "--output",
            "json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)["result"]
    assert payload["ok"] is False
    assert payload["error_type"] == "permission_denied"
    assert "requested domain filter includes domains not allowed" in payload["preview"]
    permission = SQLiteStore(tmp_path).get_session_permission(payload["permission_id"])
    assert permission.status == SessionPermissionStatus.DENIED
    assert permission.boundary_kind == SessionPermissionBoundaryKind.EXTERNAL_NETWORK


def test_repo_overview_tool_summarizes_project_local_repository_without_network(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    (tmp_path / "package.json").write_text(
        json.dumps({"main": "src/index.js", "bin": {"harness-demo": "bin/demo.js"}, "exports": {".": "./src/index.js"}}),
        encoding="utf-8",
    )
    (tmp_path / "package-lock.json").write_text("{}", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "index.js").write_text("console.log('demo')\n", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "large.js").write_text("ignored\n", encoding="utf-8")
    store = SQLiteStore(tmp_path)
    session = store.create_session(title="Repo overview")

    result = runner.invoke(
        app,
        [
            "session",
            "tool",
            session.id,
            "repo-overview",
            "--project",
            str(tmp_path),
            "--input-json",
            json.dumps({"path": ".", "depth": 2}),
            "--output",
            "json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)["result"]
    assert payload["ok"] is True
    assert payload["permission_id"] is None
    assert "Ecosystems: Node.js, Python" in payload["preview"]
    assert "Package manager: npm" in payload["preview"]
    assert "Dependency files: package.json, package-lock.json, pyproject.toml" in payload["preview"]
    assert "main: src/index.js" in payload["preview"]
    assert "file: src/index.js" in payload["preview"]
    assert "src/" in payload["preview"]
    assert "node_modules" not in payload["preview"]
    assert '"network_called": false' in payload["preview"]


def test_repo_overview_tool_denies_external_repository_mode_until_cached(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    session = SQLiteStore(tmp_path).create_session(title="External repo overview")

    result = runner.invoke(
        app,
        [
            "session",
            "tool",
            session.id,
            "repo-overview",
            "--project",
            str(tmp_path),
            "--input-json",
            json.dumps({"repository": "anomalyco/opencode"}),
            "--output",
            "json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)["result"]
    assert payload["ok"] is False
    assert payload["error_type"] == "not_found"
    assert "Repository is not in the managed cache" in payload["preview"]
    permission = SQLiteStore(tmp_path).get_session_permission(payload["permission_id"])
    assert permission.status == SessionPermissionStatus.DENIED
    assert permission.tool_id == "repo-overview"


def test_repo_overview_tool_summarizes_cached_external_repository_without_network(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    cached = tmp_path / ".harness" / "external_repositories" / "github.com__anomalyco__opencode"
    cached.mkdir(parents=True)
    _run_git_for_test(cached, ["init"])
    _run_git_for_test(cached, ["config", "user.email", "test@example.com"])
    _run_git_for_test(cached, ["config", "user.name", "Test User"])
    (cached / "package.json").write_text(json.dumps({"main": "src/index.js"}), encoding="utf-8")
    (cached / "src").mkdir()
    (cached / "src" / "index.js").write_text("export function run() { return 'cached body'; }\n", encoding="utf-8")
    _run_git_for_test(cached, ["add", "package.json", "src/index.js"])
    _run_git_for_test(cached, ["commit", "-m", "cached"])
    store = SQLiteStore(tmp_path)
    session = store.create_session(title="Cached repo overview")

    result = runner.invoke(
        app,
        [
            "session",
            "tool",
            session.id,
            "repo-overview",
            "--project",
            str(tmp_path),
            "--input-json",
            json.dumps({"repository": "anomalyco/opencode", "depth": 2}),
            "--output",
            "json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)["result"]
    assert payload["ok"] is True
    assert payload["permission_id"] is None
    assert "Repository: github.com/anomalyco/opencode" in payload["preview"]
    assert "Ecosystems: Node.js" in payload["preview"]
    assert "main: src/index.js" in payload["preview"]
    assert '"external_cache_used": true' in payload["preview"]
    assert '"network_called": false' in payload["preview"]
    assert "cached body" not in payload["preview"]


def test_repo_clone_tool_requires_permission_then_clones_into_managed_cache_and_persists_metadata(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    source = tmp_path / "source-repo"
    source.mkdir()
    _run_git_for_test(source, ["init"])
    _run_git_for_test(source, ["config", "user.email", "test@example.com"])
    _run_git_for_test(source, ["config", "user.name", "Test User"])
    (source / "README.md").write_text("# External repo\n", encoding="utf-8")
    _run_git_for_test(source, ["add", "README.md"])
    _run_git_for_test(source, ["commit", "-m", "initial"])
    remote = source.resolve().as_uri()
    store = SQLiteStore(tmp_path)
    session = store.create_session(title="Repo clone")
    arguments = {"repository": remote, "refresh": False}

    first = runner.invoke(
        app,
        [
            "session",
            "tool",
            session.id,
            "repo-clone",
            "--project",
            str(tmp_path),
            "--input-json",
            json.dumps(arguments),
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
    assert pending.tool_id == "repo-clone"
    assert pending.boundary_kind == SessionPermissionBoundaryKind.EXTERNAL_NETWORK
    assert pending.normalized_target_pattern == "local/file/source-repo"

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
            "repo-clone",
            "--project",
            str(tmp_path),
            "--input-json",
            json.dumps(arguments),
            "--output",
            "json",
        ],
    )

    assert allowed.exit_code == 0, allowed.output
    assert second.exit_code == 0, second.output
    second_payload = json.loads(second.output)["result"]
    assert second_payload["ok"] is True
    assert "Repository ready." in second_payload["preview"]
    assert "local/file/source-repo" in second_payload["preview"]
    assert "Status: cloned" in second_payload["preview"]
    cache_path = tmp_path / ".harness" / "external_repositories" / "local__file__source-repo"
    assert (cache_path / ".git").exists()
    assert (cache_path / "README.md").read_text(encoding="utf-8") == "# External repo\n"
    artifacts = SQLiteStore(tmp_path).list_artifacts(second_payload["run_id"])
    metadata_artifact = next(artifact for artifact in artifacts if artifact.kind == "session_tool_repo_clone_metadata")
    metadata = json.loads(metadata_artifact.path.read_text(encoding="utf-8"))
    assert metadata["schema_version"] == "harness.session_tool_repo_clone_plan/v1"
    assert metadata["repository"] == "local/file/source-repo"
    assert metadata["remote"] == remote
    assert metadata["origin"] == "external_git_repository"
    assert metadata["refresh"] is False
    assert metadata["requires_network"] is True
    assert metadata["permission_boundary"]["kind"] == "managed_external_repository_cache"
    assert metadata["permission_boundary"]["boundary_kind"] == "external_network"
    assert metadata["permission_boundary"]["approval_required"] is True
    assert metadata["permission_boundary"]["active_workspace_write"] is False
    assert metadata["network_called"] is True
    assert metadata["clone_executed"] is True
    assert metadata["external_cache_used"] is True
    assert metadata["status"] == "cloned"
    assert metadata["head"]
    assert SQLiteStore(tmp_path).get_session_permission(first_payload["permission_id"]).status == SessionPermissionStatus.EXPIRED

    repeated = runner.invoke(
        app,
        [
            "session",
            "tool",
            session.id,
            "repo-clone",
            "--project",
            str(tmp_path),
            "--input-json",
            json.dumps(arguments),
            "--output",
            "json",
        ],
    )

    assert repeated.exit_code == 0, repeated.output
    repeated_payload = json.loads(repeated.output)["result"]
    assert repeated_payload["ok"] is False
    assert repeated_payload["error_type"] == "permission_required"
    assert repeated_payload["permission_id"] != first_payload["permission_id"]


def test_repo_clone_tool_denies_credential_url_without_network(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    session = SQLiteStore(tmp_path).create_session(title="Repo clone denied")

    result = runner.invoke(
        app,
        [
            "session",
            "tool",
            session.id,
            "repo-clone",
            "--project",
            str(tmp_path),
            "--input-json",
            json.dumps({"repository": "https://token@github.com/anomalyco/opencode.git"}),
            "--output",
            "json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)["result"]
    assert payload["ok"] is False
    assert payload["error_type"] == "permission_denied"
    assert "must not include credentials" in payload["preview"]
    permission = SQLiteStore(tmp_path).get_session_permission(payload["permission_id"])
    assert permission.status == SessionPermissionStatus.DENIED
    assert permission.tool_id == "repo-clone"


def test_lsp_diagnostics_tool_projects_config_without_starting_processes_or_reading_contents(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    (tmp_path / "app.py").write_text("def build_plan():\n    return 'body must not leak'\n", encoding="utf-8")
    config_path = tmp_path / ".harness" / "config.yaml"
    config_data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config_data["lsp"] = {
        "enabled": True,
        "servers": {
            "python": {
                "enabled": True,
                "command": ["pyright-langserver", "--stdio"],
                "file_extensions": [".py"],
            }
        },
    }
    config_path.write_text(yaml.safe_dump(config_data, sort_keys=False), encoding="utf-8")
    store = SQLiteStore(tmp_path)
    session = store.create_session(title="LSP diagnostics")

    result = runner.invoke(
        app,
        [
            "session",
            "tool",
            session.id,
            "lsp-diagnostics",
            "--project",
            str(tmp_path),
            "--input-json",
            json.dumps({"path": "app.py"}),
            "--output",
            "json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)["result"]
    assert payload["ok"] is True
    assert payload["permission_id"] is None
    assert "LSP enabled: true" in payload["preview"]
    assert "Matching servers: python" in payload["preview"]
    assert "Process started: false" in payload["preview"]
    assert "Diagnostics: none" in payload["preview"]
    assert '"contents_included": false' in payload["preview"]
    assert '"process_started": false' in payload["preview"]
    assert "body must not leak" not in payload["preview"]


def test_lsp_symbols_tool_lists_static_symbols_without_starting_processes_or_reading_bodies(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    (tmp_path / "app.py").write_text(
        "class Builder:\n    pass\n\n"
        "def build_plan():\n    return 'body must not leak'\n\n"
        "def skip_me():\n    return None\n",
        encoding="utf-8",
    )
    (tmp_path / "ui.ts").write_text(
        "export function renderPlan() { return 'secret body'; }\n"
        "const helper = () => true;\n",
        encoding="utf-8",
    )
    store = SQLiteStore(tmp_path)
    session = store.create_session(title="LSP symbols")

    result = runner.invoke(
        app,
        [
            "session",
            "tool",
            session.id,
            "lsp-symbols",
            "--project",
            str(tmp_path),
            "--input-json",
            json.dumps({"query": "plan", "limit": 10}),
            "--output",
            "json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)["result"]
    assert payload["ok"] is True
    assert payload["permission_id"] is None
    assert "Source: static_scan" in payload["preview"]
    assert "function build_plan app.py:4:5" in payload["preview"]
    assert "function renderPlan ui.ts:1:17" in payload["preview"]
    assert "skip_me" not in payload["preview"]
    assert "body must not leak" not in payload["preview"]
    assert "secret body" not in payload["preview"]
    assert '"contents_included": false' in payload["preview"]
    assert '"process_started": false' in payload["preview"]


def test_lsp_definition_tool_finds_static_definition_without_process_or_body_leak(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    (tmp_path / "app.py").write_text(
        "def build_plan():\n    return 'body must not leak'\n\n"
        "def caller():\n    return build_plan()\n",
        encoding="utf-8",
    )
    store = SQLiteStore(tmp_path)
    session = store.create_session(title="LSP definition")

    result = runner.invoke(
        app,
        [
            "session",
            "tool",
            session.id,
            "lsp-definition",
            "--project",
            str(tmp_path),
            "--input-json",
            json.dumps({"symbol": "build_plan", "path": "app.py"}),
            "--output",
            "json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)["result"]
    assert payload["ok"] is True
    assert payload["permission_id"] is None
    assert "Source: static_scan" in payload["preview"]
    assert "Definitions: 1" in payload["preview"]
    assert "function build_plan app.py:1:5" in payload["preview"]
    assert '"schema_version": "harness.session_tool_lsp_definition/v1"' in payload["preview"]
    assert '"process_started": false' in payload["preview"]
    assert '"contents_included": false' in payload["preview"]
    assert "body must not leak" not in payload["preview"]


def test_lsp_references_tool_finds_static_references_without_process_or_body_leak(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    (tmp_path / "app.py").write_text(
        "def build_plan():\n    return 'body must not leak'\n\n"
        "def caller():\n    return build_plan()\n",
        encoding="utf-8",
    )
    (tmp_path / "other.py").write_text("from app import build_plan\n\nvalue = build_plan()\n", encoding="utf-8")
    store = SQLiteStore(tmp_path)
    session = store.create_session(title="LSP references")

    result = runner.invoke(
        app,
        [
            "session",
            "tool",
            session.id,
            "lsp-references",
            "--project",
            str(tmp_path),
            "--input-json",
            json.dumps({"symbol": "build_plan", "path": "app.py", "limit": 10}),
            "--output",
            "json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)["result"]
    assert payload["ok"] is True
    assert payload["permission_id"] is None
    assert "Source: static_scan" in payload["preview"]
    assert "References: 4" in payload["preview"]
    assert "- app.py:1:5" in payload["preview"]
    assert "- app.py:5:12" in payload["preview"]
    assert "- other.py:1:17" in payload["preview"]
    assert "- other.py:3:9" in payload["preview"]
    assert '"schema_version": "harness.session_tool_lsp_references/v1"' in payload["preview"]
    assert '"process_started": false' in payload["preview"]
    assert '"contents_included": false' in payload["preview"]
    assert "body must not leak" not in payload["preview"]


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

    second = SQLiteStore(tmp_path).request_session_permission(
        session.id,
        tool_id="shell",
        normalized_action="run",
        normalized_target_pattern="pytest",
        boundary_kind=SessionPermissionBoundaryKind.SHELL,
        risk="medium",
        scope=SessionPermissionScope.ONCE,
    )
    replied = runner.invoke(
        app,
        [
            "session",
            "permission",
            session.id,
            "--project",
            str(tmp_path),
            "--resolve",
            second.id,
            "--reply",
            "once",
            "--reason",
            "operator approved once",
            "--output",
            "json",
        ],
    )

    assert replied.exit_code == 0, replied.output
    reply_payload = json.loads(replied.output)
    assert reply_payload["schema_version"] == "harness.session_permission_reply/v1"
    assert reply_payload["decision"] == SessionPermissionStatus.ALLOWED.value
    assert reply_payload["permission"]["status"] == SessionPermissionStatus.ALLOWED.value
    assert reply_payload["permission"]["scope"] == SessionPermissionScope.ONCE.value
    assert reply_payload["scope_broadened"] is False
    assert reply_payload["execution_started"] is False


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
    events = SQLiteStore(tmp_path).list_session_store_events(session.id)
    output_events = [event for event in events if event.kind == "tool_call.output" and event.payload.get("tool_id") in {"read", "grep", "glob"}]
    by_tool = {event.payload["tool_id"]: event.payload for event in output_events}
    for tool_id in ("read", "grep", "glob"):
        payload = by_tool[tool_id]
        assert payload["policy_boundary"] == {
            "kind": "project_read_only",
            "boundary_kind": "local_only",
            "source": "session_tool_read_glob_grep",
        }
        assert payload["project_boundary_enforced"] is True
        assert payload["context_excludes_enforced"] is True
        assert payload["secret_path_filtering"] is True
        assert payload["read_only"] is True
        assert payload["process_started"] is False
        assert payload["network_accessed"] is False
        assert payload["shell_execution_started"] is False
        assert payload["filesystem_modified"] is False
        assert payload["active_repo_modified"] is False
        assert payload["git_mutation_started"] is False
        assert payload["permission_granting"] is False
        assert payload["authority_granting"] is False
        assert payload["blocked_reasons"] == []
    parts = SQLiteStore(tmp_path).list_session_parts(session.id)
    read_parts = [part for part in parts if part.metadata.get("tool_id") in {"read", "grep", "glob"}]
    assert {part.metadata["tool_id"] for part in read_parts} == {"read", "grep", "glob"}
    assert all(part.metadata["read_only"] is True for part in read_parts)
    assert all(part.metadata["filesystem_modified"] is False for part in read_parts)
    assert all(part.metadata["permission_granting"] is False for part in read_parts)
    records = [
        event.payload["record"]
        for event in events
        if event.kind == "harness.tool_call.after" and event.payload["record"]["tool_id"] in {"read", "grep", "glob"}
    ]
    assert {record["tool_id"] for record in records} == {"read", "grep", "glob"}
    assert all(record["schema_version"] == "harness.tool_call/v1" for record in records)
    assert all(record["permission_state"] == "not_required" for record in records)
    assert all(record["status"] == "completed" for record in records)
    assert all(record["normalized_args"]["approval_target"]["project_root_fingerprint"] for record in records)


def test_session_cwd_tools_inherit_cd_and_reject_symlink_escape(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "app.py").write_text("alpha = 1\n", encoding="utf-8")
    (tmp_path / "tests" / "app.py").write_text("test alpha\n", encoding="utf-8")
    outside = tmp_path.parent / f"{tmp_path.name}_outside"
    outside.mkdir()
    (tmp_path / "src" / "escape").symlink_to(outside, target_is_directory=True)
    store = SQLiteStore(tmp_path)
    session = store.create_session(title="cwd inheritance")

    cd = runner.invoke(app, ["session", "tool", session.id, "cd", "--project", str(tmp_path), "--input-json", '{"path":"src","actor":"operator"}', "--output", "json"])
    read = runner.invoke(app, ["session", "tool", session.id, "read", "--project", str(tmp_path), "--input-json", '{"path":"app.py"}', "--output", "json"])
    grep = runner.invoke(app, ["session", "tool", session.id, "grep", "--project", str(tmp_path), "--input-json", '{"pattern":"alpha","path":".","limit":10}', "--output", "json"])
    glob = runner.invoke(app, ["session", "tool", session.id, "glob", "--project", str(tmp_path), "--input-json", '{"pattern":"*.py"}', "--output", "json"])
    explicit = runner.invoke(app, ["session", "tool", session.id, "read", "--project", str(tmp_path), "--input-json", '{"path":"app.py","cwd":"tests"}', "--output", "json"])
    pwd = runner.invoke(app, ["session", "tool", session.id, "pwd", "--project", str(tmp_path), "--output", "json"])
    escape = runner.invoke(app, ["session", "tool", session.id, "grep", "--project", str(tmp_path), "--input-json", '{"pattern":"alpha","path":"escape"}', "--output", "json"])

    assert cd.exit_code == 0, cd.output
    assert read.exit_code == 0, read.output
    assert grep.exit_code == 0, grep.output
    assert glob.exit_code == 0, glob.output
    assert explicit.exit_code == 0, explicit.output
    assert pwd.exit_code == 0, pwd.output
    assert escape.exit_code == 0, escape.output
    assert json.loads(cd.output)["ok"] is True
    assert SQLiteStore(tmp_path).get_session(session.id).metadata["cwd"] == "src"
    assert json.loads(read.output)["result"]["preview"] == "alpha = 1\n"
    assert "src/app.py:1: alpha = 1" in json.loads(grep.output)["result"]["preview"]
    assert json.loads(glob.output)["result"]["preview"] == "src/app.py"
    assert json.loads(explicit.output)["result"]["preview"] == "test alpha\n"
    assert "Session cwd: src" in json.loads(pwd.output)["result"]["preview"]
    assert json.loads(escape.output)["ok"] is False
    assert json.loads(escape.output)["result"]["error_type"] == "permission_denied"
    events = SQLiteStore(tmp_path).list_session_store_events(session.id)
    assert any(event.kind == "session.cwd_changed" and event.payload["new_cwd"] == "src" for event in events)


def test_git_diff_session_tool_uses_session_cwd_and_persists_patch_artifact(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    _run_git_for_test(tmp_path, ["init"])
    _run_git_for_test(tmp_path, ["config", "user.email", "test@example.com"])
    _run_git_for_test(tmp_path, ["config", "user.name", "Harness Test"])
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("alpha = 1\n", encoding="utf-8")
    _run_git_for_test(tmp_path, ["add", "src/app.py"])
    _run_git_for_test(tmp_path, ["commit", "-m", "initial"])
    (tmp_path / "src" / "app.py").write_text("alpha = 2\n", encoding="utf-8")
    store = SQLiteStore(tmp_path)
    session = store.create_session(title="diff", metadata={"cwd": "src"})

    result = runner.invoke(app, ["session", "tool", session.id, "git-diff", "--project", str(tmp_path), "--output", "json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert "Target: src" in payload["result"]["preview"]
    assert "-alpha = 1" in payload["result"]["preview"]
    assert "+alpha = 2" in payload["result"]["preview"]
    output_events = [
        event.payload
        for event in SQLiteStore(tmp_path).list_session_store_events(session.id)
        if event.kind == "tool_call.output" and event.payload.get("tool_id") == "git-diff"
    ]
    assert output_events[-1]["read_only"] is True
    assert output_events[-1]["git_mutation_started"] is False


def test_shell_permission_is_exact_and_simple_cd_routes_to_session_cwd(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    (tmp_path / "src").mkdir()
    store = SQLiteStore(tmp_path)
    session = store.create_session(title="shell", metadata={"cwd": "src"})

    first = runner.invoke(app, ["session", "tool", session.id, "shell", "--project", str(tmp_path), "--input-json", '{"command":"pwd","timeout_seconds":30}', "--output", "json"])
    assert first.exit_code == 0, first.output
    first_payload = json.loads(first.output)
    assert first_payload["ok"] is False
    assert first_payload["result"]["error_type"] == "permission_required"
    permission = SQLiteStore(tmp_path).get_session_permission(first_payload["result"]["permission_id"])
    assert '"timeout_seconds":30' in permission.normalized_target_pattern
    SQLiteStore(tmp_path).resolve_session_permission(permission.id, SessionPermissionStatus.ALLOWED, reason="test")

    second = runner.invoke(app, ["session", "tool", session.id, "shell", "--project", str(tmp_path), "--input-json", '{"command":"pwd","timeout_seconds":30}', "--output", "json"])
    assert second.exit_code == 0, second.output
    second_payload = json.loads(second.output)
    assert second_payload["ok"] is True
    assert "Shell command executed." in second_payload["result"]["preview"]
    assert str(tmp_path / "src") in second_payload["result"]["preview"]
    assert SQLiteStore(tmp_path).get_session_permission(permission.id).status == SessionPermissionStatus.EXPIRED

    repeated = runner.invoke(app, ["session", "tool", session.id, "shell", "--project", str(tmp_path), "--input-json", '{"command":"pwd","timeout_seconds":30}', "--output", "json"])
    assert repeated.exit_code == 0, repeated.output
    repeated_payload = json.loads(repeated.output)
    assert repeated_payload["ok"] is False
    assert repeated_payload["result"]["error_type"] == "permission_required"
    assert repeated_payload["result"]["permission_id"] != permission.id

    timeout_changed = runner.invoke(app, ["session", "tool", session.id, "shell", "--project", str(tmp_path), "--input-json", '{"command":"pwd","timeout_seconds":300}', "--output", "json"])
    assert timeout_changed.exit_code == 0, timeout_changed.output
    changed_payload = json.loads(timeout_changed.output)
    assert changed_payload["ok"] is False
    assert changed_payload["result"]["permission_id"] != permission.id

    cd = runner.invoke(app, ["session", "tool", session.id, "shell", "--project", str(tmp_path), "--input-json", '{"command":"cd .."}', "--output", "json"])
    assert cd.exit_code == 0, cd.output
    cd_payload = json.loads(cd.output)
    assert cd_payload["ok"] is True
    assert cd_payload["result"]["tool_id"] == "cd"
    assert "No process was started." in cd_payload["result"]["preview"]
    assert SQLiteStore(tmp_path).get_session(session.id).metadata["cwd"] == "."


def test_shell_approval_target_changes_with_cwd_timeout_and_executable(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    store = SQLiteStore(tmp_path)
    session = store.create_session(title="shell exact target", metadata={"cwd": "src"})

    def request(arguments: dict) -> tuple[dict, dict, dict]:
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
                json.dumps(arguments),
                "--output",
                "json",
            ],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)["result"]
        assert payload["ok"] is False
        assert payload["error_type"] == "permission_required"
        permission = SQLiteStore(tmp_path).get_session_permission(payload["permission_id"])
        card = build_session_approval_card(SQLiteStore(tmp_path), session.id, permission.id, fallback_arguments=arguments)
        return payload, json.loads(permission.normalized_target_pattern), card

    first, first_target, first_card = request({"command": "pwd", "timeout_seconds": 30})
    cwd_changed, cwd_target, cwd_card = request({"command": "pwd", "cwd": "tests", "timeout_seconds": 30})
    timeout_changed, timeout_target, timeout_card = request({"command": "pwd", "timeout_seconds": 31})
    executable_changed, executable_target, executable_card = request(
        {"command": "pwd", "timeout_seconds": 30, "shell_executable": sys.executable}
    )

    assert len(
        {
            first["permission_id"],
            cwd_changed["permission_id"],
            timeout_changed["permission_id"],
            executable_changed["permission_id"],
        }
    ) == 4
    assert first_target["normalized_cwd"] == "src"
    assert cwd_target["normalized_cwd"] == "tests"
    assert first_target["timeout_seconds"] == 30
    assert timeout_target["timeout_seconds"] == 31
    assert first_target["shell_executable"] != executable_target["shell_executable"]
    assert first_card["command"] == "pwd"
    assert first_card["cwd"] == "src"
    assert cwd_card["cwd"] == "tests"
    assert timeout_card["timeout_seconds"] == 31
    assert first_card["shell_executable"] != executable_card["shell_executable"]
    assert first_card["sandbox_profile"] == "session_tool_shell_exact"
    assert first_card["network_policy"] == "host_network_available"
    assert first_card["descriptor_ref"]["tool_id"] == "shell"
    assert first_card["descriptor_ref"]["permission_key"] == "tool.shell.execution"
    assert first_card["policy"]["permission_required"] is True
    assert first_card["policy"]["replay_policy"] == "rerun_forbidden"
    for target in (first_target, cwd_target, timeout_target, executable_target):
        assert target["project_root_fingerprint"]
        assert target["session_id"] == session.id
        assert target["tool_id"] == "shell"
        assert target["normalized_operation"] == "pwd"
        assert target["timeout"] == target["timeout_seconds"]
        assert target["env_policy"] == "minimal_inherited_path_home"
        assert target["network_policy"] == "host_network_available"
        assert target["sandbox_profile"] == "session_tool_shell_exact"
        assert target["run_mode"] == "read_only"


def test_denied_shell_call_writes_blocked_tool_call_record(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    session = SQLiteStore(tmp_path).create_session(title="shell denied")

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
            '{"command":"pwd","shell_executable":"/missing/not-a-shell"}',
            "--output",
            "json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)["result"]
    assert payload["ok"] is False
    assert payload["error_type"] == "permission_denied"
    assert "Shell executable is not executable" in payload["preview"]
    records = [
        event.payload["record"]
        for event in SQLiteStore(tmp_path).list_session_store_events(session.id)
        if event.kind == "harness.tool_call.after" and event.payload["record"]["tool_id"] == "shell"
    ]
    assert records[-1]["permission_state"] == "denied"
    assert records[-1]["status"] == "blocked"


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
    checked = [event for event in SQLiteStore(tmp_path).list_session_store_events(session.id) if event.kind == "permission.checked"][-1]
    assert checked.payload["decision"] == "deny"
    assert checked.payload["action"] == "read"
    assert checked.payload["target"] == ".env"
    assert checked.payload["boundary_kind"] == "local_only"


def test_session_read_context_excluded_path_requires_permission_with_tool_call_record(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    session = SQLiteStore(tmp_path).create_session(title="Excluded read")

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
            '{"path":".harness/config.yaml"}',
            "--output",
            "json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)["result"]
    assert payload["ok"] is False
    assert payload["error_type"] == "permission_required"
    assert payload["permission_id"]
    store = SQLiteStore(tmp_path)
    permission = store.get_session_permission(payload["permission_id"])
    assert permission.status == SessionPermissionStatus.PENDING
    assert permission.normalized_action == "read"
    records = [
        event.payload["record"]
        for event in store.list_session_store_events(session.id)
        if event.kind == "harness.tool_call.after" and event.payload["record"]["tool_id"] == "read"
    ]
    assert records[-1]["permission_state"] == "pending"
    assert records[-1]["status"] == "blocked"


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
