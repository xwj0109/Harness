from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from harness.events import append_jsonl
from harness.memory.sqlite_store import SQLiteStore
from harness.procedure_renderer import render_procedure_event
from harness.security import sanitize_for_logging


LIVE_ARTIFACT_KINDS = ("events", "transcript", "procedure", "final_report", "token_usage", "manifest")


def write_live_run_artifacts(store: SQLiteStore, run_id: str, *, reasoning_summary: str | None = None) -> dict[str, Path]:
    run = store.get_run(run_id)
    run_dir = store.runs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    events = store.list_events(run_id)
    event_envelopes = [event.jsonl_envelope() for event in events]
    latest_usage = _latest_token_usage(event_envelopes)
    reasoning_summary = reasoning_summary or _reasoning_summary(event_envelopes)

    paths = {
        "events": run_dir / "events.jsonl",
        "transcript": run_dir / "transcript.jsonl",
        "procedure": run_dir / "procedure.md",
        "final_report": run_dir / "final_report.md",
        "token_usage": run_dir / "token_usage.json",
        "manifest": run_dir / "manifest.json",
    }

    _write_transcript(paths["transcript"], event_envelopes)
    _write_procedure(paths["procedure"], event_envelopes)
    paths["token_usage"].write_text(json.dumps(latest_usage, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    existing = {artifact.kind for artifact in store.list_artifacts(run_id)}
    for kind in ("events", "transcript", "procedure", "token_usage"):
        if kind in existing:
            continue
        store.register_artifact(
            run_id,
            kind,
            paths[kind],
            producer="harness.live_run",
            redaction_state="redacted" if kind in {"events", "transcript", "procedure", "final_report"} else "not_required",
            metadata={"live_run_artifact": True},
        )
        existing.add(kind)
    paths["final_report"].write_text(
        _render_final_report(
            store=store,
            run_id=run_id,
            reasoning_summary=reasoning_summary,
            token_usage=latest_usage,
        ),
        encoding="utf-8",
    )
    for kind in ("final_report", "manifest"):
        if kind in existing:
            continue
        store.register_artifact(
            run_id,
            kind,
            paths[kind],
            producer="harness.live_run",
            redaction_state="redacted",
            metadata={"live_run_artifact": True},
        )
        existing.add(kind)
    store.write_run_manifest(run_id)
    return paths


def _write_transcript(path: Path, events: list[dict[str, Any]]) -> None:
    path.write_text("", encoding="utf-8")
    for event in events:
        if event.get("visibility") != "user_visible":
            continue
        append_jsonl(
            path,
            sanitize_for_logging(
                {
                    "schema_version": "harness.transcript/v1",
                    "run_id": event.get("run_id"),
                    "task_id": event.get("task_id"),
                    "seq": event.get("seq"),
                    "timestamp": event.get("timestamp"),
                    "type": event.get("type"),
                    "redaction_state": event.get("redaction_state"),
                    "text": render_procedure_event(event),
                    "payload": event.get("payload") or {},
                }
            ),
        )


def _write_procedure(path: Path, events: list[dict[str, Any]]) -> None:
    lines = ["# Live Procedure", ""]
    for event in events:
        if event.get("visibility") == "user_visible":
            lines.append(render_procedure_event(event))
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def _render_final_report(
    *,
    store: SQLiteStore,
    run_id: str,
    reasoning_summary: str,
    token_usage: dict[str, Any],
) -> str:
    run = store.get_run(run_id)
    artifacts = store.list_artifacts(run_id)
    events = store.list_events(run_id)
    changed_files = _event_payload_values(events, {"file.write"}, "path")
    tests = [event for event in events if event.event_type in {"test.started", "test.finished"}]
    approvals = [event for event in events if event.event_type == "approval.required"]
    risks = _remaining_risks(events)

    lines = [
        "# Run Summary",
        "",
        "## User request",
        str(sanitize_for_logging(run.goal or "")),
        "",
        "## What happened",
        _what_happened(run.status, events),
        "",
        "## Procedure taken",
    ]
    for index, event in enumerate([event for event in events if event.visibility.value == "user_visible"], start=1):
        lines.append(f"{index}. {render_procedure_event(event.jsonl_envelope()).split('. ', 1)[-1]}")
    if not events:
        lines.append("1. No live events were recorded.")
    lines.extend(
        [
            "",
            "## Reasoning summary",
            str(sanitize_for_logging(reasoning_summary or "No reasoning summary was provided.")),
            "",
            "## Files changed",
        ]
    )
    lines.extend([f"- {path}" for path in changed_files] or ["- none recorded"])
    lines.extend(["", "## Tests"])
    if tests:
        for event in tests:
            lines.append(f"- {event.event_type}: {event.payload}")
    else:
        lines.append("- none recorded")
    lines.extend(["", "## Token usage", "```json", json.dumps(token_usage, indent=2, sort_keys=True), "```"])
    lines.extend(["", "## Approvals"])
    if approvals:
        for event in approvals:
            lines.append(f"- {event.payload}")
    else:
        lines.append("- none recorded")
    lines.extend(["", "## Artifacts"])
    if artifacts:
        for artifact in artifacts:
            lines.append(f"- {artifact.kind}: {artifact.path} ({artifact.sha256 or 'no sha256'})")
    else:
        lines.append("- none registered yet")
    lines.extend(["", "## Remaining risks"])
    lines.extend([f"- {risk}" for risk in risks] or ["- none recorded"])
    lines.append("")
    return "\n".join(lines)


def _latest_token_usage(events: list[dict[str, Any]]) -> dict[str, Any]:
    usage: dict[str, Any] = {}
    for event in events:
        if event.get("type") == "token_usage.updated":
            usage = dict(event.get("payload") or {})
    return usage


def _reasoning_summary(events: list[dict[str, Any]]) -> str:
    parts = []
    for event in events:
        if event.get("type") == "reasoning.summary_delta":
            payload = event.get("payload") or {}
            text = payload.get("delta") or payload.get("text")
            if text:
                parts.append(str(text))
    return " ".join(parts)


def _event_payload_values(events, event_types: set[str], key: str) -> list[str]:
    values: list[str] = []
    for event in events:
        if event.event_type in event_types:
            value = event.payload.get(key)
            if value and str(value) not in values:
                values.append(str(value))
    return values


def _what_happened(status: str, events) -> str:
    if any(event.event_type == "run.failed" for event in events) or status == "failed":
        return "The run failed after emitting the recorded live procedure events."
    if any(event.event_type == "run.finished" for event in events) or status.startswith("completed"):
        return "The run completed and produced the recorded live procedure artifacts."
    return "The run produced the recorded live procedure events."


def _remaining_risks(events) -> list[str]:
    risks: list[str] = []
    if any(event.event_type == "approval.required" for event in events):
        risks.append("One or more approval gates were required during the run.")
    if not any(event.event_type == "test.finished" for event in events):
        risks.append("No completed test event was recorded.")
    return risks
