from __future__ import annotations

import json

from harness.memory.sqlite_store import SQLiteStore
from harness.models import EventStreamType, RunEventType, SessionMessageRole, SessionPartKind
from harness.session_timeline import (
    list_session_timeline,
    list_session_transcript,
    render_timeline_event,
    render_transcript_entry,
    timeline_event_jsonl,
    transcript_entry_jsonl,
)


def test_session_timeline_replays_after_restart_with_redaction(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    raw_secret = "sk-abcdefghijklmnopqrstuvwxyz"
    session = store.create_session(title="Replay")
    message = store.append_session_message(
        session.id,
        SessionMessageRole.USER,
        f"Use token {raw_secret}",
    )
    store.append_session_part(
        session.id,
        message.id,
        SessionPartKind.TEXT,
        text=f"Inspect without leaking Bearer abcdefghijklmnop",
    )
    run = store.create_run("Replay run", "codex_direct", session_id=session.id)
    store.append_run_event(
        run.id,
        RunEventType.RUN_STARTED,
        {"summary": f"started with {raw_secret}"},
        message=f"started with {raw_secret}",
    )
    store.append_store_event(
        EventStreamType.SESSION,
        session.id,
        "model.message_delta",
        {"summary": f"delta {raw_secret}"},
        session_id=session.id,
        run_id=run.id,
    )

    restarted = SQLiteStore(tmp_path)
    restarted.initialize()

    rendered_timeline = "\n".join(render_timeline_event(event) for event in list_session_timeline(restarted, session.id))
    timeline_jsonl = "\n".join(timeline_event_jsonl(event) for event in list_session_timeline(restarted, session.id))
    rendered_transcript = "\n\n".join(render_transcript_entry(entry) for entry in list_session_transcript(restarted, session.id))
    transcript_jsonl = "\n".join(transcript_entry_jsonl(entry) for entry in list_session_transcript(restarted, session.id))

    combined = "\n".join([rendered_timeline, timeline_jsonl, rendered_transcript, transcript_jsonl])
    assert raw_secret not in combined
    assert "Bearer abcdefghijklmnop" not in combined
    assert "[REDACTED_SECRET]" in combined
    assert "Run started" in rendered_timeline
    assert "Model update" in rendered_timeline
    assert f"user {message.id}" in rendered_transcript
    assert "\x1b[" not in timeline_jsonl
    assert "\x1b[" not in transcript_jsonl


def test_session_event_sequence_is_monotonic_per_stream_and_filterable(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    session = store.create_session(title="Ordering")

    appended = [
        store.append_store_event(EventStreamType.SESSION, session.id, f"custom.{index}", {}, session_id=session.id)
        for index in range(3)
    ]

    session_stream = store.list_store_events(EventStreamType.SESSION, session.id)
    assert [event.seq for event in session_stream] == list(range(1, len(session_stream) + 1))
    assert [event.seq for event in appended] == [2, 3, 4]

    after_two = store.list_store_events(EventStreamType.SESSION, session.id, after_seq=2)
    assert [event.kind for event in after_two] == ["custom.1", "custom.2"]

    run = store.create_run("Ordering run", "codex_direct", session_id=session.id)
    first_run_event = store.append_run_event(run.id, RunEventType.RUN_STARTED, {"summary": "started"})
    second_run_event = store.append_run_event(run.id, RunEventType.RUN_FINISHED, {"summary": "finished"})
    run_stream = store.list_store_events(EventStreamType.RUN, run.id)
    assert [event.seq for event in run_stream] == [1, 2]
    assert [first_run_event.seq, second_run_event.seq] == [1, 2]

    linked_kinds = [event.kind for event in list_session_timeline(store, session.id)]
    assert ["run.started", "run.finished"] == [kind for kind in linked_kinds if kind.startswith("run.")]


def test_session_timeline_renders_usage_and_cost_evidence(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    session = store.create_session(title="Usage evidence")
    store.append_store_event(
        EventStreamType.SESSION,
        session.id,
        "token_usage.updated",
        {
            "normalized_usage": {
                "input_tokens": 3,
                "output_tokens": 4,
                "reasoning_tokens": 1,
                "cache_read_tokens": 2,
                "cache_write_tokens": 1,
                "total_tokens": 8,
            },
            "estimated_cost": {
                "currency": "USD",
                "input": 0.000003,
                "output": 0.000008,
                "total": 0.000011,
                "estimated": True,
                "source": "model_descriptor_pricing",
            },
            "estimated_cost_usd": 0.000011,
            "provider_reported_cost": {
                "currency": "USD",
                "total": 0.006,
                "source": "provider_usage_cost",
            },
        },
        session_id=session.id,
    )

    rendered = "\n".join(render_timeline_event(event) for event in list_session_timeline(store, session.id))
    timeline_jsonl = "\n".join(timeline_event_jsonl(event) for event in list_session_timeline(store, session.id))

    assert "Token usage updated" in rendered
    assert "input=3 output=4 reasoning=1 cache_read=2 cache_write=1 total=8" in rendered
    assert "estimated_cost_usd=1.1e-05 estimated=True source=model_descriptor_pricing" in rendered
    assert "provider_reported_cost=0.006 USD source=provider_usage_cost" in rendered
    assert '"estimated": true' in timeline_jsonl
    assert '"provider_reported_cost"' in timeline_jsonl


def test_session_transcript_reconstruction_keeps_part_order_and_json_schema(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    session = store.create_session(title="Transcript")
    message = store.append_session_message(session.id, SessionMessageRole.USER, "Use these inputs")
    store.append_session_part(session.id, message.id, SessionPartKind.TEXT, text="First")
    store.append_session_part(
        session.id,
        message.id,
        SessionPartKind.ARTIFACT_REF,
        metadata={"attachment_kind": "file_ref", "path": "src/app.py"},
    )
    store.append_session_part(session.id, message.id, SessionPartKind.TEXT, text="Second")

    entries = list_session_transcript(store, session.id)
    assert len(entries) == 1
    assert [part.ordinal for part in entries[0].parts] == [1, 2, 3]

    rendered = render_transcript_entry(entries[0])
    assert rendered.index("First") < rendered.index("[file] src/app.py") < rendered.index("Second")

    envelope = json.loads(transcript_entry_jsonl(entries[0]))
    assert envelope["schema_version"] == "harness.session_transcript_entry/v1"
    assert [part["ordinal"] for part in envelope["parts"]] == [1, 2, 3]


def test_session_snapshot_refs_are_append_only_and_not_revert_grants(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    session = store.create_session(title="Snapshot")
    run = store.create_run("Snapshot run", "codex_isolated_edit", session_id=session.id)
    artifact_path = store.initialize_run_artifacts(run.id)["final_report"]
    artifact_path.write_text("snapshot evidence\n", encoding="utf-8")
    artifact = store.register_artifact(run.id, "snapshot_manifest", artifact_path, session_id=session.id)
    message = store.append_session_message(
        session.id,
        SessionMessageRole.ASSISTANT,
        "Recorded an isolated snapshot",
        run_id=run.id,
    )

    part = store.append_session_snapshot_ref(
        session.id,
        message.id,
        "snap_123",
        snapshot_kind="isolated_workspace",
        artifact_id=artifact.id,
        run_id=run.id,
        reversible=True,
        metadata={"files": ["app.py"]},
    )

    assert part.kind == SessionPartKind.SNAPSHOT_REF
    assert part.metadata["snapshot_id"] == "snap_123"
    assert part.metadata["reversible"] is True
    assert part.metadata["revert_supported"] is False

    rendered_timeline = "\n".join(render_timeline_event(event) for event in list_session_timeline(store, session.id))
    rendered_transcript = "\n\n".join(render_transcript_entry(entry) for entry in list_session_transcript(store, session.id))
    events = store.list_session_store_events(session.id)

    assert "Snapshot recorded" in rendered_timeline
    assert "isolated_workspace snap_123 reversible=True" in rendered_timeline
    assert "[snapshot] snap_123 reversible=True" in rendered_transcript
    snapshot_events = [event for event in events if event.kind == "session.snapshot.recorded"]
    assert len(snapshot_events) == 1
    assert snapshot_events[0].payload["revert_supported"] is False
    assert snapshot_events[0].payload["permission_granting"] is False


def test_session_timeline_renders_persisted_tui_activation_as_ui_only_evidence(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    session = store.create_session(title="UI activation")
    store.append_store_event(
        EventStreamType.SESSION,
        session.id,
        "tui.ui_activation.applied",
        {
            "source": "slash",
            "entry_id": "ui_controls.settings",
            "activation_kind": "ui_action",
            "action": {"type": "focus_section", "section_id": "settings"},
            "ui_action_applied": True,
            "command_started": False,
            "process_started": False,
            "filesystem_modified": False,
            "permission_granting": False,
            "authority_granting": False,
            "evidence_status": "ui_only_persisted",
            "policy_boundary": {
                "kind": "safe_ui_activation",
                "ui_state_only": True,
                "command_execution_allowed": False,
                "process_start_allowed": False,
                "filesystem_mutation_allowed": False,
                "permission_grant_allowed": False,
                "authority_grant_allowed": False,
            },
            "blocked_reasons": [],
        },
        session_id=session.id,
    )

    rendered_timeline = "\n".join(render_timeline_event(event) for event in list_session_timeline(store, session.id))
    timeline_jsonl = "\n".join(timeline_event_jsonl(event) for event in list_session_timeline(store, session.id))

    assert "UI action applied" in rendered_timeline
    assert "ui_controls.settings action=focus_section source=slash" in rendered_timeline
    assert "command_started=False" in rendered_timeline
    assert "process_started=False" in rendered_timeline
    assert "filesystem_modified=False" in rendered_timeline
    assert "permission_granting=False" in rendered_timeline
    assert "authority_granting=False" in rendered_timeline
    assert '"kind": "tui.ui_activation.applied"' in timeline_jsonl
