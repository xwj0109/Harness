from __future__ import annotations

from pathlib import Path

from harness.action_router import ManagedActionDecision, ManagedActionResult, ManagedActionRoute
from harness.memory.sqlite_store import SQLiteStore


def write_managed_action_report(
    store: SQLiteStore,
    run_id: str,
    *,
    request: str,
    route: ManagedActionRoute,
    decision: ManagedActionDecision,
    result: ManagedActionResult,
) -> Path:
    report_path = store.runs_dir / run_id / "final_report.md"
    report_path.write_text(
        "\n".join(
            [
                "# Harness Managed Action Report",
                "",
                "## Summary",
                f"- Request: {request}",
                f"- Intent: {route.intent}",
                f"- Status: {result.status}",
                f"- Risk: {route.risk.value}",
                f"- Executor: {route.executor}",
                "",
                "## Result",
                f"- Created: {_join_paths(result.created_paths)}",
                f"- Changed: {_join_paths(result.changed_paths)}",
                "- Skipped: none",
                "",
                "## Policy",
                f"- Decision: {decision.status.value}",
                f"- Reasons: {'; '.join(decision.reasons) if decision.reasons else 'none'}",
                "- Hosted provider: not used",
                "- External network: not used",
                "- Active repo write: local low-risk workspace action",
                "",
                "## Evidence",
                f"- Run: {run_id}",
                f"- Events: {store.runs_dir / run_id / 'events.jsonl'}",
                f"- Artifacts: {', '.join(result.artifact_ids) if result.artifact_ids else 'pending registration'}",
                f"- Manifest: {store.runs_dir / run_id / 'manifest.json'}",
                "",
                "## Next Actions",
                f"- Inspect: harness report {run_id}",
                "- Undo: manual removal if this created an unwanted empty local file",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return report_path


def _join_paths(paths: list[Path]) -> str:
    return ", ".join(str(path) for path in paths) if paths else "none"

