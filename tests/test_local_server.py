from __future__ import annotations

import json
import subprocess
import struct
import threading
import zlib
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
import yaml
from typer.testing import CliRunner

from harness.cli.main import app
from harness.config import load_config
from harness.local_server import (
    _authorized,
    _route_get,
    _route_post,
    build_openapi_spec,
    build_session_sse_stream,
    create_local_http_server,
)
from harness.memory.sqlite_store import SQLiteStore


runner = CliRunner()


def test_openapi_spec_exposes_phase_6_read_only_endpoints() -> None:
    spec = build_openapi_spec(server_url="http://127.0.0.1:9999")

    assert spec["openapi"] == "3.1.0"
    assert spec["info"]["x-harness-schema-version"] == "harness.local_server.openapi/v1"
    assert spec["x-harness"]["permission_granting"] is False
    assert spec["x-harness"]["no_hidden_fallback"] is True
    assert spec["x-harness"]["authority"] == "local_persistence_no_execution"
    assert spec["components"]["securitySchemes"]["bearerAuth"]["scheme"] == "bearer"
    assert {
        "/health",
        "/providers",
        "/models",
        "/config",
        "/agents",
        "/artifacts",
        "/files",
        "/files/content",
        "/files/status",
        "/references",
        "/instructions",
        "/symbols",
        "/lsp/diagnostics",
        "/formatters",
        "/mcp/status",
        "/mcp/resources",
        "/plugins",
        "/skills",
        "/web/tools",
        "/worktrees",
        "/pty/sessions",
        "/pty/shells",
        "/distribution/status",
        "/version/check",
        "/pr/checkout",
        "/pr/run",
        "/sessions",
        "/sessions/{session_id}",
        "/sessions/{session_id}/events",
        "/sessions/{session_id}/messages",
        "/sessions/{session_id}/permissions",
        "/sessions/{session_id}/diffs",
        "/sessions/{session_id}/revert",
        "/sessions/{session_id}/unrevert",
        "/sessions/{session_id}/apply-hunk",
        "/sessions/{session_id}/mentions/resolve",
        "/sessions/{session_id}/attachments",
        "/sessions/{session_id}/context/estimate",
        "/sessions/{session_id}/events/stream",
        "/openapi.json",
    } <= set(spec["paths"])
    assert "post" in spec["paths"]["/sessions"]
    assert "post" in spec["paths"]["/sessions/{session_id}/messages"]
    assert spec["paths"]["/sessions/{session_id}/events"]["get"]["security"] == [{"bearerAuth": []}]
    assert (
        spec["paths"]["/sessions/{session_id}/events/stream"]["get"]["responses"]["200"]["content"]
        == {"text/event-stream": {"schema": {"type": "string"}}}
    )


def test_harness_serve_openapi_cli_outputs_checked_contract(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0

    result = runner.invoke(app, ["serve", "--project", str(tmp_path), "--openapi", "--output", "json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["info"]["x-harness-schema-version"] == "harness.local_server.openapi/v1"
    assert payload["servers"][0]["url"] == "http://127.0.0.1:8765"
    assert "bearerAuth" in payload["components"]["securitySchemes"]


def test_local_server_routes_require_token_and_return_store_projections(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    (tmp_path / "README.md").write_text("hello server\nsk-abcdefghijklmnopqrstuvwxyz\n", encoding="utf-8")
    (tmp_path / ".env").write_text("TOKEN=secret-token\n", encoding="utf-8")
    store = SQLiteStore(tmp_path)
    session = store.create_session(title="Server session", raw_model_ref="codex_cli/gpt-5.5")
    message = store.append_session_message(session.id, "user", "Replay this")
    store.append_session_part(session.id, message.id, "text", text="Replay this")
    permission = store.request_session_permission(
        session.id,
        tool_id="read",
        normalized_action="read",
        normalized_target_pattern="README.md",
        boundary_kind="local_only",
        risk="low",
    )
    run = store.create_run("server artifact", "phase_1a_test", status="succeeded", session_id=session.id)
    artifact_path = store.initialize_run_artifacts(run.id)["final_report"]
    artifact_path.write_text("server artifact body\n", encoding="utf-8")
    artifact = store.register_artifact(run.id, "final_report", artifact_path, session_id=session.id)
    cfg = load_config(tmp_path)

    assert _authorized("Bearer local-token", "local-token") is True
    assert _authorized("Bearer wrong", "local-token") is False
    health = _route_get("/health", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    config = _route_get("/config", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    agents = _route_get("/agents", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    artifacts = _route_get("/artifacts", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    files = _route_get("/files", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    file_content = _route_get(
        "/files/content",
        query={"path": ["README.md"]},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    file_status = _route_get("/files/status", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    references = _route_get("/references", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    instructions = _route_get("/instructions", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    symbols = _route_get("/symbols", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    diagnostics = _route_get("/lsp/diagnostics", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    formatters = _route_get("/formatters", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    mcp_status = _route_get("/mcp/status", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    mcp_resources = _route_get("/mcp/resources", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    plugins = _route_get("/plugins", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    skills = _route_get("/skills", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    web_tools = _route_get("/web/tools", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    worktrees = _route_get("/worktrees", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    pty_sessions = _route_get("/pty/sessions", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    pty_shells = _route_get("/pty/shells", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    distribution = _route_get("/distribution/status", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    version_check = _route_get("/version/check", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    providers = _route_get("/providers", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    models = _route_get("/models", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    sessions = _route_get("/sessions", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    inspected = _route_get(f"/sessions/{session.id}", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    events = _route_get(f"/sessions/{session.id}/events", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    messages = _route_get(f"/sessions/{session.id}/messages", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    permissions = _route_get(
        f"/sessions/{session.id}/permissions",
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    diffs = _route_get(f"/sessions/{session.id}/diffs", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    stream_projection = _route_get(
        f"/sessions/{session.id}/events/stream",
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    sse = build_session_sse_stream(store, f"/sessions/{session.id}/events/stream")

    assert health["schema_version"] == "harness.local_server/v1"
    assert config["schema_version"] == "harness.config_projection/v1"
    assert config["permission_granting"] is False
    assert agents["schema_version"] == "harness.project_agents/v1"
    assert artifacts["schema_version"] == "harness.artifacts/v1"
    assert artifacts["contents_included"] is False
    assert artifacts["artifacts"][0]["id"] == artifact.id
    assert artifacts["artifacts"][0]["contents_included"] is False
    assert files["schema_version"] == "harness.files/v1"
    assert files["contents_included"] is False
    assert any(file["path"] == "README.md" for file in files["files"])
    assert not any(file["path"] == ".env" for file in files["files"])
    assert file_content["schema_version"] == "harness.file_content/v1"
    assert file_content["path"] == "README.md"
    assert "hello server" in file_content["preview"]
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in file_content["preview"]
    assert "[REDACTED_SECRET]" in file_content["preview"]
    assert file_status["schema_version"] == "harness.file_status/v1"
    assert file_status["contents_included"] is False
    assert file_status["permission_granting"] is False
    assert references["schema_version"] == "harness.references/v1"
    assert references["contents_included"] is False
    assert references["permission_granting"] is False
    assert instructions["schema_version"] == "harness.instructions/v1"
    assert instructions["contents_included"] is False
    assert instructions["permission_granting"] is False
    assert symbols["schema_version"] == "harness.symbols/v1"
    assert symbols["lsp_backed"] is False
    assert symbols["process_started"] is False
    assert symbols["contents_included"] is False
    assert symbols["permission_granting"] is False
    assert diagnostics["schema_version"] == "harness.lsp_diagnostics/v1"
    assert diagnostics["enabled"] is False
    assert diagnostics["process_started"] is False
    assert diagnostics["permission_granting"] is False
    assert formatters["schema_version"] == "harness.formatters/v1"
    assert formatters["enabled"] is False
    assert formatters["process_started"] is False
    assert formatters["permission_granting"] is False
    assert mcp_status["schema_version"] == "harness.mcp_status/v1"
    assert mcp_status["enabled"] is False
    assert mcp_status["connected"] is False
    assert mcp_status["process_started"] is False
    assert mcp_status["network_called"] is False
    assert mcp_status["permission_granting"] is False
    assert mcp_resources["schema_version"] == "harness.mcp_resources/v1"
    assert mcp_resources["resources"] == []
    assert mcp_resources["cached_only"] is True
    assert mcp_resources["permission_granting"] is False
    assert plugins["schema_version"] == "harness.plugins/v1"
    assert plugins["enabled"] is False
    assert plugins["runtime_loaded"] is False
    assert plugins["tools_registered"] is False
    assert plugins["permission_granting"] is False
    assert skills["schema_version"] == "harness.skills/v1"
    assert skills["enabled"] is False
    assert skills["runtime_loaded"] is False
    assert skills["tool_registered"] is False
    assert skills["permission_granting"] is False
    assert web_tools["schema_version"] == "harness.web_tools/v1"
    assert web_tools["enabled"] is False
    assert web_tools["network_called"] is False
    assert web_tools["execution_supported"] is False
    assert web_tools["permission_granting"] is False
    assert {tool["id"] for tool in web_tools["tools"]} == {"web-fetch", "web-search"}
    assert all(tool["decision"] == "denied" for tool in web_tools["tools"])
    assert worktrees["schema_version"] == "harness.worktrees/v1"
    assert worktrees["available"] is False
    assert worktrees["mutation_supported"] is False
    assert worktrees["permission_granting"] is False
    assert pty_sessions["schema_version"] == "harness.pty_sessions/v1"
    assert pty_sessions["sessions"] == []
    assert pty_sessions["process_started"] is False
    assert pty_sessions["permission_granting"] is False
    assert pty_shells["schema_version"] == "harness.pty_shells/v1"
    assert pty_shells["probed"] is False
    assert pty_shells["process_started"] is False
    assert pty_shells["permission_granting"] is False
    assert distribution["schema_version"] == "harness.distribution_status/v1"
    assert distribution["packaging_path"] == "python_wheel_first"
    assert distribution["network_called"] is False
    assert distribution["filesystem_modified"] is False
    assert distribution["subprocess_started"] is False
    assert distribution["permission_granting"] is False
    assert version_check["schema_version"] == "harness.version_check/v1"
    assert version_check["network_called"] is False
    assert version_check["subprocess_started"] is False
    assert version_check["permission_granting"] is False
    assert providers["schema_version"] == "harness.providers/v1"
    assert providers["permission_granting"] is False
    assert models["no_hidden_fallback"] is True
    assert sessions["sessions"][0]["id"] == session.id
    assert inspected["session"]["id"] == session.id
    assert events["session_id"] == session.id
    assert messages["messages"][0]["id"] == message.id
    assert messages["parts"][message.id][0]["text"] == "Replay this"
    assert permissions["permissions"][0]["id"] == permission.id
    assert permissions["permission_granting"] is False
    assert diffs["schema_version"] == "harness.session_diffs/v1"
    assert diffs["revert_supported"] is False
    assert diffs["mutation_started"] is False
    assert diffs["permission_granting"] is False
    assert stream_projection["transport"] == "sse"
    assert stream_projection["permission_granting"] is False
    assert any(event["kind"] == "session.message.appended" for event in events["events"])
    assert "event: harness.ready" in sse
    assert f'"session_id": "{session.id}"' in sse
    assert "event: session.message.appended" in sse
    assert "\x1b[" not in sse
    serialized = json.dumps([config, agents, artifacts, files, file_content, file_status, references, instructions, symbols, diagnostics, formatters, mcp_status, mcp_resources, plugins, skills, web_tools, worktrees, pty_sessions, pty_shells, distribution, version_check, providers, models, sessions, inspected, events, messages, permissions, diffs])
    assert "api_key" not in serialized
    assert "ollama" not in serialized
    assert "server artifact body" not in serialized


def test_local_server_post_persists_session_prompt_without_execution(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    cfg = load_config(tmp_path)

    created = _route_post(
        "/sessions",
        body={
            "title": "API session",
            "prompt": "Plan a safe implementation",
            "raw_model_ref": "codex_cli/gpt-5.5",
            "agent_id": "plan",
        },
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )

    assert created is not None
    assert created["schema_version"] == "harness.local_server_session_create/v1"
    assert created["execution_started"] is False
    assert created["permission_granting"] is False
    assert created["no_hidden_fallback"] is True
    assert created["session"]["title"] == "API session"
    assert created["session"]["raw_model_ref"] == "codex_cli/gpt-5.5"
    assert created["message"]["role"] == "user"
    assert created["message"]["agent_id"] == "plan"
    assert created["message"]["content_preview"] == "Plan a safe implementation"
    assert created["part"]["kind"] == "text"
    assert created["part"]["text"] == "Plan a safe implementation"

    session_id = created["session"]["id"]
    appended = _route_post(
        f"/sessions/{session_id}/messages",
        body={"content": "Add tests first"},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    events = _route_get(
        f"/sessions/{session_id}/events",
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    messages = _route_get(
        f"/sessions/{session_id}/messages",
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    sse = build_session_sse_stream(store, f"/sessions/{session_id}/events/stream")

    assert appended is not None
    assert appended["schema_version"] == "harness.local_server_message_append/v1"
    assert appended["execution_started"] is False
    assert appended["message"]["role"] == "user"
    assert [message["content_preview"] for message in messages["messages"]] == [
        "Plan a safe implementation",
        "Add tests first",
    ]
    assert [event["kind"] for event in events["events"]] == [
        "session.created",
        "session.message.appended",
        "session.part.appended",
        "session.message.appended",
        "session.part.appended",
    ]
    assert "event: session.message.appended" in sse
    assert "Add tests first" in sse

    with pytest.raises(ValueError, match="Only user messages"):
        _route_post(
            f"/sessions/{session_id}/messages",
            body={"role": "assistant", "content": "No execution path here"},
            project_root=tmp_path,
            store=store,
            cfg=cfg,
            host="127.0.0.1",
            port=8765,
        )


def test_local_server_real_http_auth_post_and_sse_flow(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    server = create_local_http_server(tmp_path, host="127.0.0.1", port=0, token="test-token")
    host, port = server.server_address
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://{host}:{port}"
    try:
        with pytest.raises(HTTPError) as unauthorized:
            _http_json(f"{base_url}/health")
        assert unauthorized.value.code == 401

        options = Request(f"{base_url}/sessions", method="OPTIONS")
        with urlopen(options, timeout=5) as response:
            assert response.status == 204
            assert response.headers["Access-Control-Allow-Methods"] == "GET, POST, OPTIONS"
            assert response.headers["X-Harness-Permission-Granting"] == "false"

        created = _http_json(
            f"{base_url}/sessions",
            method="POST",
            token="test-token",
            body={"title": "HTTP session", "prompt": "Persist this prompt"},
        )
        session_id = created["session"]["id"]
        appended = _http_json(
            f"{base_url}/sessions/{session_id}/messages",
            method="POST",
            token="test-token",
            body={"content": "Second prompt"},
        )
        events = _http_json(f"{base_url}/sessions/{session_id}/events", token="test-token")
        sse = _http_text(f"{base_url}/sessions/{session_id}/events/stream", token="test-token")

        assert created["schema_version"] == "harness.local_server_session_create/v1"
        assert created["execution_started"] is False
        assert appended["schema_version"] == "harness.local_server_message_append/v1"
        assert appended["permission_granting"] is False
        assert [event["kind"] for event in events["events"]] == [
            "session.created",
            "session.message.appended",
            "session.part.appended",
            "session.message.appended",
            "session.part.appended",
        ]
        assert "event: harness.ready" in sse
        assert "event: session.message.appended" in sse
        assert "Second prompt" in sse
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_harness_attach_reads_existing_server_without_project_filesystem_access(tmp_path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    session = store.create_session(title="Attach session")
    server = create_local_http_server(tmp_path, host="127.0.0.1", port=0, token="attach-token")
    host, port = server.server_address
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = runner.invoke(
            app,
            [
                "attach",
                "--server-url",
                f"http://{host}:{port}",
                "--token",
                "attach-token",
                "--output",
                "json",
            ],
        )

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["schema_version"] == "harness.local_server_attach/v1"
        assert payload["ok"] is True
        assert payload["permission_granting"] is False
        assert payload["health"]["schema_version"] == "harness.local_server/v1"
        assert payload["openapi_schema_version"] == "harness.local_server.openapi/v1"
        assert payload["session_count"] == 1
        assert payload["sessions"][0]["id"] == session.id
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    monkeypatch.delenv("HARNESS_SERVER_TOKEN", raising=False)
    missing = runner.invoke(app, ["attach", "--server-url", "http://127.0.0.1:9"])
    assert missing.exit_code != 0
    assert "Missing server token" in missing.output


def test_local_server_file_status_reports_changed_files_without_contents(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=tmp_path, check=True, capture_output=True)
    tracked = tmp_path / "app.py"
    tracked.write_text("print('one')\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=tmp_path, check=True, capture_output=True)
    tracked.write_text("print('two')\n", encoding="utf-8")
    (tmp_path / "notes.md").write_text("new notes\n", encoding="utf-8")
    (tmp_path / ".env").write_text("TOKEN=secret-token\n", encoding="utf-8")
    cfg = load_config(tmp_path)
    store = SQLiteStore(tmp_path)

    status = _route_get("/files/status", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)

    assert status["schema_version"] == "harness.file_status/v1"
    assert status["available"] is True
    assert status["contents_included"] is False
    assert {file["path"] for file in status["files"]} >= {"app.py", "notes.md"}
    assert not any(file["path"] == ".env" for file in status["files"])
    assert next(file for file in status["files"] if file["path"] == "app.py")["worktree_status"] == "M"
    assert next(file for file in status["files"] if file["path"] == "notes.md")["untracked"] is True
    assert "print('two')" not in json.dumps(status)


def test_local_server_lists_worktrees_without_mutation(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "app.py").write_text("print('one')\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=tmp_path, check=True, capture_output=True)
    cfg = load_config(tmp_path)
    store = SQLiteStore(tmp_path)

    worktrees = _route_get("/worktrees", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)

    assert worktrees["schema_version"] == "harness.worktrees/v1"
    assert worktrees["available"] is True
    assert worktrees["mutation_supported"] is False
    assert worktrees["process_started"] is True
    assert worktrees["permission_granting"] is False
    assert len(worktrees["worktrees"]) == 1
    assert worktrees["worktrees"][0]["path"] == str(tmp_path)
    assert worktrees["worktrees"][0]["is_current"] is True
    assert worktrees["worktrees"][0]["mutation_supported"] is False


def test_local_server_lists_session_diff_artifact_previews_without_mutation(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    session = store.create_session(title="Diff session")
    run = store.create_run("Diff run", "codex_isolated_edit", session_id=session.id)
    diff_path = store.runs_dir / run.id / "isolated_unified_diff.patch"
    diff_path.parent.mkdir(parents=True, exist_ok=True)
    diff_path.write_text("--- a/app.py\n+++ b/app.py\n@@\n-old\n+new\n", encoding="utf-8")
    artifact = store.register_artifact(run.id, "isolated_unified_diff", diff_path, session_id=session.id)
    cfg = load_config(tmp_path)

    diffs = _route_get(f"/sessions/{session.id}/diffs", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)

    assert diffs["schema_version"] == "harness.session_diffs/v1"
    assert diffs["session_id"] == session.id
    assert diffs["revert_supported"] is False
    assert diffs["unrevert_supported"] is False
    assert diffs["selected_hunk_apply_supported"] is False
    assert diffs["mutation_started"] is False
    assert diffs["permission_granting"] is False
    assert len(diffs["diffs"]) == 1
    assert diffs["diffs"][0]["id"] == artifact.id
    assert diffs["diffs"][0]["kind"] == "isolated_unified_diff"
    assert "+new" in diffs["diffs"][0]["preview"]
    assert diffs["diffs"][0]["revert_supported"] is False
    assert diffs["diffs"][0]["selected_hunk_apply_supported"] is False


def test_local_server_session_revert_actions_fail_closed_without_mutation(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    session = store.create_session(title="Mutation session")
    cfg = load_config(tmp_path)

    revert = _route_post(
        f"/sessions/{session.id}/revert",
        body={"message_id": "msg_123"},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    unrevert = _route_post(
        f"/sessions/{session.id}/unrevert",
        body={"artifact_id": "art_123"},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    apply_hunk = _route_post(
        f"/sessions/{session.id}/apply-hunk",
        body={"artifact_id": "art_123", "hunk_id": "hunk_1"},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )

    for payload, action in [(revert, "revert"), (unrevert, "unrevert"), (apply_hunk, "apply-hunk")]:
        assert payload["schema_version"] == "harness.session_mutation_action/v1"
        assert payload["ok"] is False
        assert payload["action"] == action
        assert payload["mutation_started"] is False
        assert payload["git_mutation_started"] is False
        assert payload["filesystem_modified"] is False
        assert payload["permission_granting"] is False
    assert apply_hunk["hunk_id"] == "hunk_1"


def test_local_server_pty_projection_and_actions_fail_closed(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    cfg = load_config(tmp_path)

    sessions = _route_get("/pty/sessions", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    shells = _route_get("/pty/shells", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    created = _route_post(
        "/pty/sessions",
        body={"command": "bash"},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    written = _route_post(
        "/pty/sessions/pty_123/write",
        body={"data": "echo hello\n"},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )

    assert sessions["schema_version"] == "harness.pty_sessions/v1"
    assert sessions["managed_pty_supported"] is False
    assert sessions["sessions"] == []
    assert sessions["process_started"] is False
    assert shells["schema_version"] == "harness.pty_shells/v1"
    assert shells["probed"] is False
    assert all(shell["acceptable"] is False for shell in shells["shells"])
    assert created["schema_version"] == "harness.pty_action/v1"
    assert created["ok"] is False
    assert created["action"] == "create"
    assert created["process_started"] is False
    assert created["websocket_token_issued"] is False
    assert created["permission_granting"] is False
    assert written["pty_id"] == "pty_123"
    assert written["input_written"] is False


def test_local_server_pr_helpers_fail_closed_without_network_or_git_mutation(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    cfg = load_config(tmp_path)

    checkout = _route_post(
        "/pr/checkout",
        body={"pr": "https://github.com/example/repo/pull/42"},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    run = _route_post(
        "/pr/run",
        body={"pr": "42", "adapter": "repo_planning"},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )

    for payload, action in [(checkout, "checkout"), (run, "run")]:
        assert payload["schema_version"] == "harness.pr_action/v1"
        assert payload["ok"] is False
        assert payload["action"] == action
        assert payload["network_called"] is False
        assert payload["git_mutation_started"] is False
        assert payload["worktree_created"] is False
        assert payload["checkout_started"] is False
        assert payload["adapter_started"] is False
        assert payload["permission_granting"] is False
    assert checkout["pr"] == "https://github.com/example/repo/pull/42"
    assert run["adapter"] == "repo_planning"


def test_local_server_distribution_and_version_projections_are_offline(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    cfg = load_config(tmp_path)

    distribution = _route_get("/distribution/status", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    version_check = _route_get("/version/check", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)

    assert distribution["schema_version"] == "harness.distribution_status/v1"
    assert distribution["ok"] is True
    assert distribution["server_supported"] is True
    assert distribution["session_cli_supported"] is True
    assert distribution["network_called"] is False
    assert distribution["filesystem_modified"] is False
    assert distribution["subprocess_started"] is False
    assert distribution["permission_granting"] is False
    assert version_check["schema_version"] == "harness.version_check/v1"
    assert version_check["ok"] is True
    assert version_check["latest_version"] is None
    assert version_check["update_available"] is None
    assert version_check["notification_enabled"] is False
    assert version_check["network_called"] is False
    assert version_check["subprocess_started"] is False
    assert version_check["permission_granting"] is False


def test_local_server_resolves_mentions_and_persists_session_event(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    (tmp_path / "src").mkdir()
    (tmp_path / "README.md").write_text("readme body\n", encoding="utf-8")
    (tmp_path / "src" / "app.py").write_text("print('hello')\n", encoding="utf-8")
    (tmp_path / ".env").write_text("TOKEN=secret-token\n", encoding="utf-8")
    store = SQLiteStore(tmp_path)
    session = store.create_session(title="Mention source")
    referenced = store.create_session(title="Referenced session")
    cfg = load_config(tmp_path)

    resolved = _route_post(
        f"/sessions/{session.id}/mentions/resolve",
        body={"prompt": f"Review @file:README.md and @directory:src with @session:{referenced.id}."},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    events = _route_get(
        f"/sessions/{session.id}/events",
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )

    assert resolved is not None
    assert resolved["schema_version"] == "harness.mention_resolution/v1"
    assert resolved["contents_included"] is False
    assert resolved["permission_granting"] is False
    assert resolved["execution_started"] is False
    assert [mention["kind"] for mention in resolved["mentions"]] == ["file", "directory", "session"]
    file_mention = resolved["mentions"][0]
    directory_mention = resolved["mentions"][1]
    session_mention = resolved["mentions"][2]
    assert file_mention["path"] == "README.md"
    assert file_mention["size_bytes"] == len("readme body\n")
    assert file_mention["estimated_tokens"] > 0
    assert directory_mention["path"] == "src"
    assert directory_mention["file_count"] == 1
    assert session_mention["session_id"] == referenced.id
    assert session_mention["title"] == "Referenced session"
    assert events["events"][-1]["kind"] == "session.mentions.resolved"
    assert events["events"][-1]["payload"]["mention_count"] == 3
    assert "readme body" not in json.dumps(resolved)
    assert "print('hello')" not in json.dumps(resolved)

    with pytest.raises(ValueError, match="excluded|secret"):
        _route_post(
            f"/sessions/{session.id}/mentions/resolve",
            body={"prompt": "Read @file:.env"},
            project_root=tmp_path,
            store=store,
            cfg=cfg,
            host="127.0.0.1",
            port=8765,
        )


def test_local_server_lists_named_references_and_resolves_reference_mentions(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "guide.md").write_text("guide body\n", encoding="utf-8")
    config_path = tmp_path / ".harness" / "config.yaml"
    config_data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config_data["references"] = {
        "guide": {"kind": "local", "path": "docs/guide.md", "description": "Local guide"},
        "upstream": {"kind": "git", "url": "https://example.com/upstream.git", "description": "Remote metadata only"},
    }
    config_path.write_text(yaml.safe_dump(config_data, sort_keys=False), encoding="utf-8")
    cfg = load_config(tmp_path)
    store = SQLiteStore(tmp_path)
    session = store.create_session(title="Reference session")

    references = _route_get("/references", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    resolved = _route_post(
        f"/sessions/{session.id}/mentions/resolve",
        body={"prompt": "Use @reference:guide and compare @reference:upstream"},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    events = _route_get(
        f"/sessions/{session.id}/events",
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )

    assert references["schema_version"] == "harness.references/v1"
    assert references["contents_included"] is False
    assert [reference["name"] for reference in references["references"]] == ["guide", "upstream"]
    guide = references["references"][0]
    upstream = references["references"][1]
    assert guide["kind"] == "local"
    assert guide["path"] == "docs/guide.md"
    assert guide["size_bytes"] == len("guide body\n")
    assert upstream["kind"] == "git"
    assert upstream["url"] == "https://example.com/upstream.git"
    assert upstream["network_required"] is True
    assert resolved is not None
    assert [mention["name"] for mention in resolved["mentions"]] == ["guide", "upstream"]
    assert resolved["mentions"][0]["contents_included"] is False
    assert events["events"][-1]["kind"] == "session.mentions.resolved"
    assert "guide body" not in json.dumps([references, resolved])

    with pytest.raises(ValueError, match="configured reference"):
        _route_post(
            f"/sessions/{session.id}/mentions/resolve",
            body={"prompt": "Use @reference:missing"},
            project_root=tmp_path,
            store=store,
            cfg=cfg,
            host="127.0.0.1",
            port=8765,
        )


def test_local_server_projects_lsp_and_formatter_config_without_starting_processes(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
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
    config_data["formatter"] = {
        "enabled": True,
        "profiles": {
            "python": {
                "enabled": True,
                "command": ["black", "--quiet", "-"],
                "file_extensions": [".py"],
                "format_on_accepted_edit": True,
            }
        },
    }
    config_path.write_text(yaml.safe_dump(config_data, sort_keys=False), encoding="utf-8")
    cfg = load_config(tmp_path)
    store = SQLiteStore(tmp_path)

    diagnostics = _route_get("/lsp/diagnostics", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    formatters = _route_get("/formatters", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)

    assert diagnostics["schema_version"] == "harness.lsp_diagnostics/v1"
    assert diagnostics["enabled"] is True
    assert diagnostics["process_started"] is False
    assert diagnostics["diagnostics"] == []
    assert diagnostics["servers"] == [
        {
            "name": "python",
            "enabled": True,
            "configured": True,
            "file_extensions": [".py"],
            "command_configured": True,
            "process_started": False,
            "diagnostics": [],
        }
    ]
    assert formatters["schema_version"] == "harness.formatters/v1"
    assert formatters["enabled"] is True
    assert formatters["process_started"] is False
    assert formatters["profiles"] == [
        {
            "name": "python",
            "enabled": True,
            "configured": True,
            "file_extensions": [".py"],
            "command_configured": True,
            "format_on_accepted_edit": True,
            "process_started": False,
        }
    ]


def test_local_server_projects_mcp_config_without_connecting(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    config_path = tmp_path / ".harness" / "config.yaml"
    config_data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config_data["mcp"] = {
        "enabled": True,
        "servers": {
            "local_docs": {
                "kind": "local",
                "enabled": True,
                "command": ["mcp-docs", "--stdio"],
                "description": "Local docs server",
            },
            "remote_tracker": {
                "kind": "remote",
                "enabled": True,
                "url": "https://example.com/mcp",
                "description": "Remote tracker",
            },
        },
    }
    config_path.write_text(yaml.safe_dump(config_data, sort_keys=False), encoding="utf-8")
    cfg = load_config(tmp_path)
    store = SQLiteStore(tmp_path)

    status = _route_get("/mcp/status", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    resources = _route_get("/mcp/resources", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)

    assert status["schema_version"] == "harness.mcp_status/v1"
    assert status["enabled"] is True
    assert status["connected"] is False
    assert status["tool_registration_enabled"] is False
    assert status["process_started"] is False
    assert status["network_called"] is False
    assert [server["name"] for server in status["servers"]] == ["local_docs", "remote_tracker"]
    local, remote = status["servers"]
    assert local["command_configured"] is True
    assert local["url_configured"] is False
    assert local["requires_network"] is False
    assert local["connected"] is False
    assert remote["command_configured"] is False
    assert remote["url_configured"] is True
    assert remote["requires_network"] is True
    assert remote["oauth_authenticated"] is False
    assert remote["permission_granting"] is False
    assert resources["schema_version"] == "harness.mcp_resources/v1"
    assert resources["enabled"] is True
    assert resources["resources"] == []
    assert resources["cached_only"] is True
    assert resources["connected"] is False
    assert resources["network_called"] is False


def test_local_server_projects_plugin_config_without_loading_plugins(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    (tmp_path / "plugins" / "reviewer").mkdir(parents=True)
    config_path = tmp_path / ".harness" / "config.yaml"
    config_data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config_data["plugins"] = {
        "enabled": True,
        "project": {
            "reviewer": {
                "enabled": True,
                "path": "plugins/reviewer",
                "version": "0.1.0",
                "description": "Project review plugin",
            },
            "remote_pack": {
                "enabled": False,
                "url": "https://example.com/plugin.git",
                "description": "Remote metadata only",
            },
        },
    }
    config_path.write_text(yaml.safe_dump(config_data, sort_keys=False), encoding="utf-8")
    cfg = load_config(tmp_path)
    store = SQLiteStore(tmp_path)

    plugins = _route_get("/plugins", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)

    assert plugins["schema_version"] == "harness.plugins/v1"
    assert plugins["enabled"] is True
    assert plugins["runtime_loaded"] is False
    assert plugins["tools_registered"] is False
    assert plugins["install_supported"] is False
    assert plugins["update_supported"] is False
    assert plugins["remove_supported"] is False
    project_plugins = [plugin for plugin in plugins["plugins"] if plugin["scope"] == "project"]
    assert [plugin["name"] for plugin in project_plugins] == ["remote_pack", "reviewer"]
    remote, reviewer = project_plugins
    assert remote["enabled"] is False
    assert remote["url"] == "https://example.com/plugin.git"
    assert remote["runtime_loaded"] is False
    assert remote["tools_registered"] is False
    assert reviewer["enabled"] is True
    assert reviewer["origin"] == "config"
    assert reviewer["path"] == "plugins/reviewer"
    assert reviewer["exists"] is True
    assert reviewer["directory"] is True
    assert reviewer["version"] == "0.1.0"
    assert reviewer["permission_granting"] is False


def test_local_server_projects_skill_config_without_loading_skills(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    skill_dir = tmp_path / "skills" / "review"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Review\n\nMetadata only.\n", encoding="utf-8")
    config_path = tmp_path / ".harness" / "config.yaml"
    config_data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config_data["skills"] = {
        "enabled": True,
        "project": {
            "review": {
                "enabled": True,
                "path": "skills/review",
                "description": "Review skill",
            }
        },
    }
    config_path.write_text(yaml.safe_dump(config_data, sort_keys=False), encoding="utf-8")
    cfg = load_config(tmp_path)
    store = SQLiteStore(tmp_path)

    skills = _route_get("/skills", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)

    assert skills["schema_version"] == "harness.skills/v1"
    assert skills["enabled"] is True
    assert skills["runtime_loaded"] is False
    assert skills["tool_registered"] is False
    assert skills["load_supported"] is False
    assert len([skill for skill in skills["skills"] if skill["scope"] == "project"]) == 1
    skill = [skill for skill in skills["skills"] if skill["scope"] == "project"][0]
    assert skill["name"] == "review"
    assert skill["enabled"] is True
    assert skill["origin"] == "config"
    assert skill["path"] == "skills/review"
    assert skill["exists"] is True
    assert skill["directory"] is True
    assert skill["skill_file_exists"] is True
    assert skill["runtime_loaded"] is False
    assert skill["tool_registered"] is False
    assert skill["permission_granting"] is False
    assert "Metadata only" not in json.dumps(skills)


def test_local_server_projects_web_tool_policy_without_network_call(tmp_path) -> None:
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
    cfg = load_config(tmp_path)
    store = SQLiteStore(tmp_path)

    web_tools = _route_get("/web/tools", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)

    assert web_tools["schema_version"] == "harness.web_tools/v1"
    assert web_tools["enabled"] is True
    assert web_tools["allowed_domains"] == ["docs.example.com"]
    assert web_tools["network_called"] is False
    assert web_tools["execution_supported"] is False
    assert web_tools["permission_granting"] is False
    by_id = {tool["id"]: tool for tool in web_tools["tools"]}
    assert by_id["web-fetch"]["enabled"] is True
    assert by_id["web-fetch"]["decision"] == "approval_required"
    assert by_id["web-fetch"]["approval_required"] is True
    assert by_id["web-fetch"]["boundary_kind"] == "external_network"
    assert by_id["web-fetch"]["network_called"] is False
    assert by_id["web-search"]["enabled"] is False
    assert by_id["web-search"]["decision"] == "denied"
    assert by_id["web-search"]["approval_required"] is False


def test_local_server_discovers_instruction_files_without_loading_bodies(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    (tmp_path / "AGENTS.md").write_text("agent instructions\n", encoding="utf-8")
    (tmp_path / ".cursor" / "rules").mkdir(parents=True)
    (tmp_path / ".cursor" / "rules" / "python.md").write_text("python rules\n", encoding="utf-8")
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "copilot-instructions.md").write_text("copilot rules\n", encoding="utf-8")
    cfg = load_config(tmp_path)
    store = SQLiteStore(tmp_path)

    instructions = _route_get("/instructions", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)

    assert instructions["schema_version"] == "harness.instructions/v1"
    assert instructions["contents_included"] is False
    assert instructions["permission_granting"] is False
    paths = [file["path"] for file in instructions["files"]]
    assert paths == [
        "AGENTS.md",
        ".cursor/rules/python.md",
        ".github/copilot-instructions.md",
    ]
    first = instructions["files"][0]
    assert first["size_bytes"] == len("agent instructions\n")
    assert first["estimated_tokens"] > 0
    assert first["content_type"] == "text/markdown"
    assert "agent instructions" not in json.dumps(instructions)
    assert "python rules" not in json.dumps(instructions)


def test_local_server_lists_static_symbols_without_lsp_process(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    (tmp_path / "app.py").write_text(
        "class AppService:\n"
        "    pass\n\n"
        "async def build_plan():\n"
        "    return 'body must not leak'\n\n"
        "def helper():\n"
        "    return build_plan()\n",
        encoding="utf-8",
    )
    (tmp_path / "frontend.ts").write_text(
        "export function renderApp() { return 'secret body'; }\n"
        "const useWidget = () => true;\n",
        encoding="utf-8",
    )
    cfg = load_config(tmp_path)
    store = SQLiteStore(tmp_path)

    symbols = _route_get("/symbols", project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    filtered = _route_get(
        "/symbols",
        query={"q": ["plan"], "path": ["app.py"]},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )

    assert symbols["schema_version"] == "harness.symbols/v1"
    assert symbols["source"] == "static_scan"
    assert symbols["lsp_backed"] is False
    assert symbols["process_started"] is False
    assert symbols["contents_included"] is False
    names = {(symbol["kind"], symbol["name"], symbol["path"]) for symbol in symbols["symbols"]}
    assert ("class", "AppService", "app.py") in names
    assert ("function", "build_plan", "app.py") in names
    assert ("function", "helper", "app.py") in names
    assert ("function", "renderApp", "frontend.ts") in names
    assert ("function", "useWidget", "frontend.ts") in names
    assert [symbol["name"] for symbol in filtered["symbols"]] == ["build_plan"]
    assert "body must not leak" not in json.dumps(symbols)
    assert "secret body" not in json.dumps(symbols)


def test_local_server_prepares_metadata_only_attachments_and_persists_event(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    (tmp_path / "README.md").write_text("readme body\n", encoding="utf-8")
    large = tmp_path / "large.txt"
    large.write_bytes(b"x" * (300 * 1024))
    image = tmp_path / "image.png"
    image.write_bytes(_png_bytes(width=2, height=3))
    (tmp_path / ".env").write_text("TOKEN=secret-token\n", encoding="utf-8")
    store = SQLiteStore(tmp_path)
    session = store.create_session(title="Attachment session")
    cfg = load_config(tmp_path)

    prepared = _route_post(
        f"/sessions/{session.id}/attachments",
        body={"paths": ["README.md", "large.txt", "image.png"]},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    events = _route_get(
        f"/sessions/{session.id}/events",
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )

    assert prepared is not None
    assert prepared["schema_version"] == "harness.attachment_preparation/v1"
    assert prepared["contents_included"] is False
    assert prepared["permission_granting"] is False
    assert prepared["execution_started"] is False
    assert [attachment["path"] for attachment in prepared["attachments"]] == ["README.md", "large.txt", "image.png"]
    assert prepared["attachments"][0]["attachment_kind"] == "file_ref"
    assert prepared["attachments"][0]["content_type"] == "text/markdown"
    assert prepared["attachments"][0]["accepted"] is True
    assert prepared["attachments"][0]["requires_artifact_overflow"] is False
    assert prepared["attachments"][1]["requires_artifact_overflow"] is True
    assert prepared["attachments"][1]["accepted"] is True
    assert prepared["attachments"][2]["content_type"] == "image/png"
    assert prepared["attachments"][2]["image"] is True
    assert prepared["attachments"][2]["image_width"] == 2
    assert prepared["attachments"][2]["image_height"] == 3
    assert prepared["attachments"][2]["image_pixels"] == 6
    assert prepared["attachments"][2]["image_requires_resize"] is False
    assert prepared["attachments"][2]["max_image_pixels"] == 20_000_000
    assert events["events"][-1]["kind"] == "session.attachments.prepared"
    assert events["events"][-1]["payload"]["attachment_count"] == 3
    assert "readme body" not in json.dumps(prepared)
    assert "TOKEN=secret-token" not in json.dumps(prepared)

    with pytest.raises(ValueError, match="excluded|secret"):
        _route_post(
            f"/sessions/{session.id}/attachments",
            body={"paths": [".env"]},
            project_root=tmp_path,
            store=store,
            cfg=cfg,
            host="127.0.0.1",
            port=8765,
        )


def test_local_server_estimates_context_budget_and_persists_event(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    (tmp_path / "README.md").write_text("readme body\n", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("agent instructions\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('hello')\n", encoding="utf-8")
    store = SQLiteStore(tmp_path)
    session = store.create_session(title="Budget session")
    cfg = load_config(tmp_path)

    estimate = _route_post(
        f"/sessions/{session.id}/context/estimate",
        body={
            "prompt": "Review @file:README.md and @directory:src",
            "attachment_paths": ["README.md"],
            "include_instructions": True,
            "budget_tokens": 8,
        },
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    events = _route_get(
        f"/sessions/{session.id}/events",
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )

    assert estimate is not None
    assert estimate["schema_version"] == "harness.context_estimate/v1"
    assert estimate["contents_included"] is False
    assert estimate["permission_granting"] is False
    assert estimate["execution_started"] is False
    assert estimate["prompt_bytes"] == len("Review @file:README.md and @directory:src".encode("utf-8"))
    assert estimate["budget_tokens"] == 8
    assert estimate["within_budget"] is False
    kinds = [item["kind"] for item in estimate["items"]]
    assert kinds == ["prompt", "mention:file", "mention:directory", "attachment", "instruction"]
    assert estimate["total_estimated_tokens"] == sum(item.get("estimated_tokens", 0) for item in estimate["items"])
    assert events["events"][-1]["kind"] == "session.context.estimated"
    assert events["events"][-1]["payload"]["total_estimated_tokens"] == estimate["total_estimated_tokens"]
    assert "readme body" not in json.dumps(estimate)
    assert "agent instructions" not in json.dumps(estimate)
    assert "print('hello')" not in json.dumps(estimate)

    small = _route_post(
        f"/sessions/{session.id}/context/estimate",
        body={"prompt": "tiny", "budget_tokens": 10},
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
    )
    assert small["within_budget"] is True


def test_local_server_file_content_blocks_secret_and_excluded_paths(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    (tmp_path / ".env").write_text("TOKEN=secret-token\n", encoding="utf-8")
    store = SQLiteStore(tmp_path)
    cfg = load_config(tmp_path)

    missing_path = _route_get
    try:
        missing_path("/files/content", query={}, project_root=tmp_path, store=store, cfg=cfg, host="127.0.0.1", port=8765)
    except ValueError as exc:
        assert "Missing required query parameter" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("missing file path should be rejected")

    try:
        _route_get(
            "/files/content",
            query={"path": [".env"]},
            project_root=tmp_path,
            store=store,
            cfg=cfg,
            host="127.0.0.1",
            port=8765,
        )
    except ValueError as exc:
        assert "excluded" in str(exc) or "Blocked secret-like path" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("secret-like file path should be rejected")


def _http_json(
    url: str,
    *,
    method: str = "GET",
    token: str | None = None,
    body: dict[str, object] | None = None,
) -> dict[str, object]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = Request(url, data=data, method=method)
    if token is not None:
        request.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    with urlopen(request, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def _http_text(url: str, *, token: str) -> str:
    request = Request(url)
    request.add_header("Authorization", f"Bearer {token}")
    with urlopen(request, timeout=5) as response:
        return response.read().decode("utf-8")


def _png_bytes(*, width: int, height: int) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + (b"\x00\x00\x00" * width) for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )
