import asyncio
import json
import sqlite3

from rich.console import Console

from harness.memory.sqlite_store import SQLiteStore
from harness.models import RunEventType, SessionPermissionBoundaryKind, SessionPermissionScope, TokenUsageSnapshot
from harness.tui import (
    _append_streaming_content,
    _chat_response_to_tui_message,
    _merge_codex_stream_and_final_lines,
    _update_markup_static,
    _render_composer_status,
    build_tui_dashboard,
    build_codex_mode_model,
    create_harness_app,
    render_codex_like_transcript,
    render_codex_mode,
)


def test_codex_mode_model_renders_four_event_backed_panes(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    run = store.create_run(goal="fix parser", task_type="codex_code_edit", status="completed", task_id=None)
    store.append_run_event(run.id, RunEventType.RUN_STARTED, {"agent": "code_editor"})
    store.append_run_event(run.id, RunEventType.POLICY_RESOLVED, {"active_repo": "approval_required"})
    store.append_run_event(run.id, RunEventType.BACKEND_STARTED, {"streaming": True})
    store.append_run_event(
        run.id,
        RunEventType.REASONING_SUMMARY_DELTA,
        {"delta": "Inspect tests, patch parser fallback."},
    )
    store.append_run_event(run.id, RunEventType.MODEL_MESSAGE_DELTA, {"delta": "I will inspect the tests first."})
    store.append_run_event(run.id, RunEventType.TOOL_CALL_STARTED, {"tool": "repo_read"})
    store.append_run_event(run.id, RunEventType.TOOL_CALL_FINISHED, {"tool": "repo_read"})
    store.append_run_event(run.id, RunEventType.FILE_WRITE, {"path": "src/parser.py"})
    store.append_run_event(run.id, RunEventType.TEST_STARTED, {"command": "pytest -q"})
    store.append_run_event(run.id, RunEventType.TEST_FINISHED, {"status": "passed"})
    store.append_token_usage_event(run.id, TokenUsageSnapshot(input_tokens=10, output_tokens=5, reasoning_tokens=3, total_tokens=18))
    store.append_run_event(run.id, RunEventType.RUN_FINISHED, {"status": "completed"})

    model = build_codex_mode_model(tmp_path, run.id)

    assert model["schema_version"] == "harness.tui_codex_mode/v1"
    assert model["run_id"] == run.id
    assert model["state"] == "Succeeded"
    assert [pane["id"] for pane in model["panes"]] == [
        "live_procedure",
        "model_output",
        "artifacts",
        "controls",
    ]
    assert any("● Tool call: repo_read" in line for line in model["panes"][0]["lines"])
    assert any("thinking summary: Inspect tests" in line for line in model["panes"][1]["lines"])
    assert "reasoning_count=3" in model["header"][-1]
    rendered = render_codex_mode(model)
    assert "Codex Mode" in rendered
    assert "Live Procedure" in rendered
    assert "Model Output" in rendered
    assert "Artifacts" in rendered
    assert "Controls" in rendered


def test_codex_mode_model_handles_no_runs(tmp_path) -> None:
    SQLiteStore(tmp_path).initialize()

    model = build_codex_mode_model(tmp_path)

    assert model["ok"] is True
    assert model["run_id"] is None
    assert model["state"] == "Queued"
    assert "No persisted run events yet." in model["panes"][0]["lines"]


def test_tui_dashboard_and_composer_include_operator_phase_cwd_and_approval(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    session = store.create_session(title="Operator status", metadata={"cwd": "src"})
    permission = store.request_session_permission(
        session.id,
        tool_id="shell",
        normalized_action="run",
        normalized_target_pattern=json.dumps(
            {
                "normalized_cwd": "src",
                "command": "pytest -q",
                "normalized_command": "pytest -q",
                "timeout_seconds": 120,
                "sandbox_profile": "session_tool_shell_exact",
                "network_policy": "host_network_available",
            },
            sort_keys=True,
        ),
        boundary_kind=SessionPermissionBoundaryKind.SHELL,
        risk="medium",
        scope=SessionPermissionScope.ONCE,
    )

    dashboard = build_tui_dashboard(tmp_path)
    operator = dashboard["active_session"]["operator"]
    composer = _render_composer_status(dashboard)

    assert operator["phase"] == "waiting_approval"
    assert operator["cwd"] == "src"
    assert operator["waiting_approval_id"] == permission.id
    assert "cwd=src" in composer
    assert "Operator: waiting_approval" in composer
    assert permission.id in composer
    assert "command=pytest -q" in composer
    assert operator["approval_card"]["command"] == "pytest -q"


def test_codex_mode_model_repairs_missing_session_schema_without_raw_sqlite(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("DROP TABLE IF EXISTS sessions")

    model = build_codex_mode_model(tmp_path)

    assert model["ok"] is True
    assert "no such table" not in json.dumps(model).lower()
    assert SQLiteStore(tmp_path).inspect_required_session_schema()["ok"] is True


def test_tui_chat_response_renderer_hides_raw_payloads_unless_debug() -> None:
    response = {
        "title": "Assistant",
        "lines": ["Done."],
        "tool_results": [{"tool": "grep", "ok": True, "content": "{\"type\":\"harness.tool_result/v1\"}"}],
        "event_sequence": ["operator.turn.started", "tool_call.output"],
        "debug": {"exception_type": "ValueError", "traceback": "Traceback (most recent call last): ..."},
    }

    normal = _chat_response_to_tui_message(response)
    debug = _chat_response_to_tui_message(response, debug=True)

    assert "harness.tool_result/v1" not in json.dumps(normal)
    assert "Traceback" not in json.dumps(normal)
    debug_json = json.dumps(debug)
    assert "operator.turn.started" in debug_json
    assert "exception: ValueError" in debug_json
    assert "traceback captured" in debug_json

    approval = _chat_response_to_tui_message(
        {
            "kind": "session_tool_permission_required",
            "title": "Permission Required",
            "lines": [],
            "approval_card": {
                "approval_id": "perm_123",
                "tool_id": "shell",
                "cwd": ".",
                "command": "pytest -q",
            },
        }
    )
    assert "approval: perm_123" in approval["lines"]
    assert "command: pytest -q" in approval["lines"]


def test_codex_like_transcript_matches_prompt_entered_layout() -> None:
    rendered = render_codex_like_transcript(
        [
            {"role": "assistant", "title": "Harness chat", "lines": ["Project: /tmp/project"]},
            {"role": "user", "title": "first arbitrary prompt", "lines": []},
            {"role": "assistant", "title": "Assistant", "lines": ["First arbitrary response."]},
            {"role": "user", "title": "arbitrary request", "lines": []},
            {
                "role": "assistant",
                "title": "Assistant Streaming",
                "lines": [
                    "Turn started",
                    "Ran intent routing",
                    "- intent: unsupported",
                    "Explored",
                    "- Context: 2 selected blocks, 1 pinned, 1 retrieved",
                    "- Sources: harness policy, repo tree",
                    "- Budget: 120 / 1,000 tokens",
                    "- Warnings: approximate_token_budget_only",
                ],
            },
        ],
        working_seconds=3,
    )

    assert "[bold]Tip:[/bold] GPT-5.5 is now available in Codex." in rendered
    assert "[on #eeeeee][dim]›[/dim] first arbitrary prompt[/]" in rendered
    assert "\nFirst arbitrary response.\n" in rendered
    assert "[on #eeeeee][dim]›[/dim] arbitrary request[/]" in rendered
    assert "[green]●[/green] [bold]Ran[/bold] [dim]intent[/dim] [dim]routing[/dim]" in rendered
    assert "[dim]•[/dim] [bold]Explored[/bold]" in rendered
    assert "[dark_cyan]Context:[/dark_cyan] [dim]2 selected blocks, 1 pinned, 1 retrieved[/dim]" in rendered
    assert "[dark_cyan]Sources:[/dark_cyan] [dim]harness policy, repo tree[/dim]" in rendered
    assert "[dark_cyan]Budget:[/dark_cyan] [dim]120 / 1,000 tokens[/dim]" in rendered
    assert "[dark_cyan]Warnings:[/dark_cyan] [dim]approximate_token_budget_only[/dim]" in rendered
    assert "[dim]○[/dim] [dim]Working (3s • esc to interrupt)[/dim]" in rendered
    assert "Harness chat" not in rendered
    assert "[cyan]" not in rendered


def test_codex_like_streaming_content_accumulates_like_codex_prose() -> None:
    lines: list[str] = []
    lines = _append_streaming_content(lines, "First")
    lines = _append_streaming_content(lines, " streamed")
    lines = _append_streaming_content(lines, " sentence.\nSecond sentence.")
    lines.append("Ran model turn")
    lines = _append_streaming_content(lines, "Final")
    lines = _append_streaming_content(lines, " streamed answer.")

    rendered = render_codex_like_transcript(
        [
            {"role": "user", "title": "arbitrary request", "lines": []},
            {"role": "assistant", "title": "Assistant Streaming", "lines": lines},
        ],
        working_seconds=9,
    )

    assert "\nFirst streamed sentence.\n" in rendered
    assert "\nSecond sentence.\n" in rendered
    assert "[green]●[/green] [bold]Ran[/bold] [dim]model[/dim] [dim]turn[/dim]" in rendered
    assert "[dim]────────────────" in rendered
    assert "\nFinal streamed answer." in rendered
    assert "First\n•  streamed" not in rendered


def test_codex_like_streaming_content_does_not_append_after_turn_completed() -> None:
    lines = ["Turn completed"]
    lines = _append_streaming_content(lines, "This repo is Agent Harness.")

    rendered = render_codex_like_transcript(
        [{"role": "assistant", "title": "Assistant Streaming", "lines": lines}]
    )

    assert lines == ["Turn completed", "This repo is Agent Harness."]
    assert "Turn completedThis repo" not in rendered
    assert "Turn completed\n\nThis repo is [bold]Agent Harness[/bold]." in rendered


def test_codex_like_transcript_escapes_literal_rich_closing_tags() -> None:
    rendered = render_codex_like_transcript(
        [
            {"role": "user", "title": "trigger markup error", "lines": []},
            {
                "role": "assistant",
                "title": "Chat Error",
                "lines": [
                    "closing tag '[/cyan]' does not match any open tag",
                    "closing tag '[/bold]' does not match any open tag",
                ],
            },
        ]
    )

    Console().render_str(rendered)
    assert r"\[/cyan]" in rendered
    assert r"\[/bold]" in rendered


def test_markup_static_update_falls_back_to_plain_text_on_bad_markup() -> None:
    from textual.widgets import Static

    widget = Static("")

    _update_markup_static(widget, "closing tag [/bold] does not match any open tag")

    assert r"\[/bold]" in str(widget.content)


def test_codex_like_finish_keeps_live_steps_before_final_answer() -> None:
    merged = _merge_codex_stream_and_final_lines(
        [
            "Turn started",
            "Ran intent routing",
            "- intent: unsupported",
            "Explored",
            "- Context: 2 selected blocks, 1 pinned, 1 retrieved",
        ],
        [
            "Final answer line one.",
            "Final answer line two.",
        ],
    )
    rendered = render_codex_like_transcript(
        [
            {"role": "user", "title": "arbitrary request", "lines": []},
            {"role": "assistant", "title": "Assistant Streaming", "lines": merged},
        ]
    )

    assert "\nTurn started\n" in rendered
    assert "[green]●[/green] [bold]Ran[/bold] [dim]intent[/dim] [dim]routing[/dim]" in rendered
    assert "[dim]•[/dim] [bold]Explored[/bold]" in rendered
    assert "[dim]────────────────" in rendered
    assert "\nFinal answer line one.\n" in rendered


def test_codex_like_transcript_uses_connectors_only_for_procedure_children() -> None:
    rendered = render_codex_like_transcript(
        [
            {
                "role": "assistant",
                "title": "Assistant Streaming",
                "lines": [
                    "Ran pwd",
                    "- /Users/oscarxue/Documents/harness",
                    "Explored",
                    "- Read README.md",
                    "This repo is Agent Harness.",
                    "- CLI/TUI surface",
                ],
            },
        ],
        separator_width=24,
    )

    assert "[green]●[/green] [bold]Ran[/bold] [dim]pwd[/dim]" in rendered
    assert "[dim]└[/dim] [dim][dark_cyan]/Users/oscarxue/Documents/harness[/dark_cyan][/dim]" in rendered
    assert "[dim]└[/dim] [dark_cyan]Read[/dark_cyan] [bold]README[/bold].md" in rendered
    assert "[dim]────────────────────────[/dim]" in rendered
    assert "\nThis repo is [bold]Agent Harness[/bold].\n" in rendered
    assert "\nThis repo is [bold]Agent Harness[/bold].\n\n[dim]•[/dim]" in rendered
    assert "[dim]•[/dim] CLI[dark_cyan]/TUI[/dark_cyan] surface" in rendered
    assert "[dim]└[/dim] [dark_cyan]CLI" not in rendered


def test_codex_like_transcript_renders_reasoning_between_tool_calls() -> None:
    rendered = render_codex_like_transcript(
        [
            {
                "role": "assistant",
                "title": "Assistant Streaming",
                "lines": [
                    "Ran model turn",
                    "Reasoning: I need to inspect the README before answering.",
                    "Ran read_file",
                    "- read_file: ok",
                    "Ran model turn",
                    "Reasoning: The README gives enough project context.",
                    "Final answer.",
                ],
            },
        ],
        separator_width=24,
    )

    assert "[green]●[/green] [bold]Ran[/bold] [dim]model[/dim] [dim]turn[/dim]" in rendered
    assert "[dim]•[/dim] [dim]I need to inspect the [bold]README[/bold] before answering.[/dim]" in rendered
    assert "[green]●[/green] [bold]Ran[/bold] [dim]read_file[/dim]" in rendered
    assert "[dim]└[/dim] [dark_cyan]read_file:[/dark_cyan] [dim]ok[/dim]" in rendered
    assert "[dim]•[/dim] [dim]The [bold]README[/bold] gives enough project context.[/dim]" in rendered
    assert "[dim]────────────────────────[/dim]\nFinal answer." in rendered


def test_codex_like_transcript_spaces_and_emphasizes_prose_paragraphs() -> None:
    rendered = render_codex_like_transcript(
        [
            {
                "role": "assistant",
                "title": "Assistant",
                "lines": [
                    "This repo is Agent Harness.",
                    "In plain terms: it provides a supervised control plane.",
                    "Key files:",
                    "- README overview",
                    "- CLI entrypoint",
                ],
            },
        ]
    )

    assert "This repo is [bold]Agent Harness[/bold].\n\n[bold]In plain terms:[/bold]" in rendered
    assert "[bold]supervised control plane[/bold]" in rendered
    assert "[bold]Key files:[/bold]\n[dim]•[/dim] [bold]README[/bold] overview" in rendered
    assert "[bold]README[/bold] overview\n[dim]•[/dim] [bold]CLI[/bold] entrypoint" in rendered


def test_codex_like_app_keeps_transcript_and_context_together(tmp_path) -> None:
    async def run_pilot() -> None:
        app = create_harness_app(tmp_path, codex_like=True)
        async with app.run_test(size=(120, 40)):
            chat = app.query_one("#chat-content")
            side = app.query_one("#pane-container")
            status = app.query_one("#search-status")

            assert "[bold]Tip:[/bold] GPT-5.5 is now available in Codex." in str(chat.content)
            assert "Harness chat" not in str(chat.content)
            assert "Assistant" in str(side.content)
            assert "Mode: live" in str(side.content)
            assert "Harness" in str(status.content)
            assert "Q 0R/0A/0B" in str(status.content)

    asyncio.run(run_pilot())
