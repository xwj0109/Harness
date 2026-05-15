from harness.procedure_renderer import render_procedure_event


def test_procedure_renderer_covers_codex_like_live_events() -> None:
    cases = [
        ({"seq": 1, "type": "run.started", "payload": {}}, "1. ● Run started"),
        ({"seq": 2, "type": "policy.resolved", "payload": {}}, "2. ● Resolving policy"),
        ({"seq": 3, "type": "approval.required", "payload": {}}, "3. ! Approval required"),
        ({"seq": 4, "type": "workspace.prepared", "payload": {}}, "4. ● Preparing workspace"),
        ({"seq": 5, "type": "backend.started", "payload": {}}, "5. ● Model started"),
        (
            {"seq": 6, "type": "reasoning.summary_delta", "payload": {"delta": "safe summary"}},
            "6. thinking summary: safe summary",
        ),
        (
            {"seq": 7, "type": "tool_call.started", "payload": {"tool": "repo_read"}},
            "7. ● Tool call: repo_read",
        ),
        ({"seq": 8, "type": "file.read", "payload": {"path": "src/parser.py"}}, "8. ● File read: src/parser.py"),
        ({"seq": 9, "type": "file.write", "payload": {"path": "src/parser.py"}}, "9. ● Editing: src/parser.py"),
        ({"seq": 10, "type": "diff.updated", "payload": {"added": 12, "removed": 4}}, "10. ● Diff ready (+12 -4 lines)"),
        ({"seq": 11, "type": "test.started", "payload": {"command": "pytest -q"}}, "11. ● Running tests: pytest -q"),
        ({"seq": 12, "type": "test.finished", "payload": {"status": "passed"}}, "12. ● Tests finished: passed"),
        (
            {"seq": 13, "type": "token_usage.updated", "payload": {"total_tokens": 42}},
            "13. ● Token usage updated: 42 total",
        ),
        ({"seq": 14, "type": "run.summary_created", "payload": {}}, "14. ● Final summary"),
        ({"seq": 15, "type": "run.finished", "payload": {}}, "15. ● Run finished"),
    ]

    for event, expected in cases:
        assert render_procedure_event(event) == expected
