from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from harness.approvals import ApprovalStore
from harness.capabilities import build_capability_catalog
from harness.governance.gate_registry import gate_registry_payload
from harness.governance.models import GovernanceContextPackResult
from harness.governance.paths import governance_root
from harness.governance.test_plan import plan_governance_tests
from harness.governance.tasks import load_governance_task, update_governance_task_context_hash
from harness.memory.sqlite_store import SQLiteStore
from harness.models import EventStreamType
from harness.sandbox_profiles import build_sandbox_profile_catalog
from harness.security import sanitize_for_logging


ContextRunner = Callable[[list[str], Path], subprocess.CompletedProcess[str]]

MAX_TEXT_CHARS = 12_000
MAX_DIFF_CHARS = 20_000
MAX_EVENTS = 20


def build_governance_context_pack(
    project_root: Path,
    task_id: str,
    *,
    runner: ContextRunner | None = None,
) -> GovernanceContextPackResult:
    root = Path(project_root).resolve()
    active_runner = runner or _run_command
    task_result = load_governance_task(root, task_id)
    governance = task_result.governance
    store = SQLiteStore(root)
    store.initialize()
    payload = {
        "schema_version": "harness.governance_context_pack/v1",
        "generated_at": _now(),
        "task": sanitize_for_logging(task_result.task.model_dump(mode="json")),
        "governance_task": sanitize_for_logging(governance.model_dump(mode="json")),
        "operator_docs": _operator_docs(root),
        "branch": {
            "base": governance.base,
            "name": governance.branch,
            "base_sha": governance.base_sha,
            "diff": _git_stdout(
                active_runner,
                root,
                ["git", "diff", f"{governance.base}...{governance.branch}"],
                limit=MAX_DIFF_CHARS,
            ),
        },
        "latest_evidence": {
            "recent_governance_artifacts": _recent_governance_artifacts(root),
            "recent_session_events": _recent_session_events(store, governance.session_id),
        },
        "active_permissions": [
            item.model_dump(mode="json")
            for item in store.list_session_permissions(governance.session_id)
        ],
        "approval_profiles": [
            approval.model_dump(mode="json")
            for approval in ApprovalStore(root).list()
            if not approval.revoked
        ],
        "sandbox_constraints": build_sandbox_profile_catalog(root).model_dump(mode="json"),
        "capability_catalog": build_capability_catalog(root).model_dump(mode="json"),
        "governance": {
            "gate_registry": gate_registry_payload(),
            "provider_independence": (
                "Provider CLIs and adapters execute work; Harness owns permissions, sessions, state, "
                "budgets, traces, apply-back, test evidence, and merge decisions."
            ),
        },
        "required_test_plan": plan_governance_tests(root, task_id, runner=active_runner).payload,
        "exclusions": [
            "secrets",
            "raw provider logs",
            "unrelated inbox material",
            "unapproved protected-infrastructure rewrites",
        ],
        "side_effects": {
            "provider_called": False,
            "network_called": False,
            "repo_files_modified": False,
            "evidence_written": True,
            "task_metadata_updated": True,
        },
    }
    clean = sanitize_for_logging(payload)
    digest = _digest_for_context_pack(clean)
    if isinstance(clean, dict):
        clean["sha256"] = digest
    path = governance_root(root) / "context-packs" / f"{task_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(clean, indent=2, sort_keys=True), encoding="utf-8")
    update_governance_task_context_hash(root, task_id, digest)
    return GovernanceContextPackResult(task_id=task_id, path=str(path), sha256=digest, payload=clean)


def _operator_docs(root: Path) -> dict[str, str]:
    docs = {
        "governance_plan": root / "docs" / "plans" / "toloclaw_governance_parity_plan.md",
        "command_catalog": root / "docs" / "command_catalog.md",
        "session_tool_catalog": root / "docs" / "session_tool_catalog.md",
        "operator_guide": root / "docs" / "operator_guide.md",
    }
    return {key: _read_excerpt(path) for key, path in docs.items()}


def _recent_governance_artifacts(root: Path) -> list[dict[str, object]]:
    gov_root = governance_root(root)
    if not gov_root.is_dir():
        return []
    results: list[dict[str, object]] = []
    for path in sorted(gov_root.rglob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True):
        if "context-packs" in path.relative_to(gov_root).parts:
            continue
        if len(results) >= 10:
            break
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            rel = str(path)
        results.append({"path": rel, "bytes": path.stat().st_size})
    return results


def _recent_session_events(store: SQLiteStore, session_id: str) -> list[dict[str, object]]:
    try:
        events = store.list_store_events(EventStreamType.SESSION, session_id)
    except Exception:
        return []
    return [
        {
            "kind": event.kind,
            "created_at": event.created_at.isoformat(),
            "payload": sanitize_for_logging(event.payload),
        }
        for event in events[-MAX_EVENTS:]
    ]


def _read_excerpt(path: Path, *, max_chars: int = MAX_TEXT_CHARS) -> str:
    if not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n[truncated]"


def _git_stdout(runner: ContextRunner, root: Path, command: list[str], *, limit: int) -> str:
    result = runner(command, root)
    if result.returncode != 0:
        return ""
    text = result.stdout or ""
    if len(text) <= limit:
        return str(sanitize_for_logging(text))
    return str(sanitize_for_logging(text[:limit] + "\n[truncated]"))


def _digest_for_context_pack(payload: object) -> str:
    stable = _without_generated_fields(payload)
    raw = json.dumps(stable, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _without_generated_fields(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: _without_generated_fields(item)
            for key, item in value.items()
            if key not in {"generated_at", "sha256", "context_pack_hash", "updated_at"}
        }
    if isinstance(value, list):
        return [_without_generated_fields(item) for item in value]
    return value


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _run_command(command: list[str], root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=root, text=True, capture_output=True, timeout=120)
