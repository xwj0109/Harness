from harness.backends.streaming import BackendStreamEvent, MockStreamingBackend, classify_codex_stream_item


def test_backend_stream_event_contract_and_mock_backend() -> None:
    events = list(MockStreamingBackend().stream())

    assert [event.type for event in events] == [
        "status",
        "message_delta",
        "tool_call",
        "tool_result",
        "token_usage",
        "status",
    ]
    assert all(isinstance(event, BackendStreamEvent) for event in events)
    assert events[1].text == "I will inspect the failing tests first."
    assert events[4].payload["total_tokens"] == 22


def test_codex_stream_item_classifier_maps_structured_events() -> None:
    message = classify_codex_stream_item({"type": "event", "event": {"type": "message", "delta": "hello"}})
    reasoning = classify_codex_stream_item(
        {"type": "event", "event": {"type": "reasoning_summary", "text": "safe summary"}}
    )
    tool_call = classify_codex_stream_item({"type": "event", "event": {"type": "tool_call", "tool": "repo_read"}})
    tool_result = classify_codex_stream_item(
        {"type": "event", "event": {"type": "tool_result", "text": "read src/parser.py"}}
    )
    usage = classify_codex_stream_item({"type": "event", "event": {"type": "token_usage", "total_tokens": 12}})
    stdout = classify_codex_stream_item({"type": "stdout", "line": "plain output\n"})

    assert message.type == "message_delta"
    assert message.text == "hello"
    assert reasoning.type == "reasoning_summary_delta"
    assert tool_call.type == "tool_call"
    assert tool_result.type == "tool_result"
    assert usage.type == "token_usage"
    assert stdout.type == "message_delta"
