from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from harness.governance.models import GOVERNANCE_TASK_SCHEMA_VERSION, GovernanceTaskMetadata
from harness.memory.sqlite_store import SQLiteStore
from harness.models import TaskRecord, TaskStatus
from harness.registry import builtin_spec_registry
from harness.security import sanitize_for_logging


TaskRunner = Callable[[list[str], Path], subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class GovernanceTaskResult:
    task: TaskRecord
    governance: GovernanceTaskMetadata

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": GOVERNANCE_TASK_SCHEMA_VERSION,
            "ok": True,
            "task": sanitize_for_logging(self.task.model_dump(mode="json")),
            "governance": sanitize_for_logging(self.governance.model_dump(mode="json")),
        }


def create_governance_task(
    project_root: Path,
    *,
    slug: str,
    agent_id: str,
    goal: str,
    base: str = "main",
    runner: TaskRunner | None = None,
) -> GovernanceTaskResult:
    root = Path(project_root).resolve()
    active_runner = runner or _run_command
    clean_slug = _slugify(slug)
    if not clean_slug:
        raise ValueError("task slug must contain at least one alphanumeric character")
    if not goal.strip():
        raise ValueError("goal must be non-empty")

    registry = builtin_spec_registry()
    try:
        agent = registry.get_agent(agent_id)
    except KeyError as exc:
        raise ValueError(str(exc).strip("'")) from exc

    _ensure_git_worktree_clean(root, active_runner)
    base_sha = _git_stdout(active_runner, root, ["git", "rev-parse", "--verify", base])
    created_at = _now()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    branch = f"harness/task/{clean_slug}-{stamp.lower()}"
    worktree_path = (root / ".harness" / "governance" / "worktrees" / f"{stamp}-{clean_slug}").resolve()
    _run_checked(active_runner, root, ["git", "worktree", "add", "-b", branch, str(worktree_path), base])

    store = SQLiteStore(root)
    store.initialize()
    session = store.create_session(
        title=f"governance:{clean_slug}",
        agent_id=agent.id,
        intent="governance_task",
        metadata={
            "surface": "governance",
            "schema_version": "harness.governance_session/v1",
            "governance_slug": clean_slug,
            "governance_branch": branch,
            "permission_granting": False,
            "authority_granting": False,
        },
    )
    task = store.create_task(
        title=f"Governance: {clean_slug}",
        description=str(sanitize_for_logging(goal.strip())),
        agent_id=agent.id,
        session_id=session.id,
        metadata={
            "governance_schema_version": GOVERNANCE_TASK_SCHEMA_VERSION,
            "governance_status": "creating",
        },
    )
    metadata = GovernanceTaskMetadata(
        task_id=task.id,
        slug=clean_slug,
        branch=branch,
        base=base,
        base_sha=base_sha,
        worktree_path=str(worktree_path),
        session_id=session.id,
        agent=agent.id,
        model_profile=agent.model_profile,
        permission_profile=agent.tool_policy,
        sandbox_profile=_sandbox_profile_for_tool_policy(agent.tool_policy),
        goal=goal.strip(),
        allowed_paths=list(_default_allowed_paths(agent.tool_policy)),
        expected_artifacts=list(agent.outputs or _default_expected_artifacts(agent.tool_policy)),
        created_at=created_at,
    )
    task = _replace_task_governance_metadata(store, task.id, metadata)
    store.update_session(
        session.id,
        active_task_id=task.id,
        agent_id=agent.id,
        metadata={
            **session.metadata,
            "governance_task_id": task.id,
            "governance_status": metadata.status,
        },
    )
    return GovernanceTaskResult(task=task, governance=metadata)


def list_governance_tasks(project_root: Path) -> list[GovernanceTaskResult]:
    store = SQLiteStore(Path(project_root).resolve())
    store.initialize()
    results: list[GovernanceTaskResult] = []
    for task in store.list_tasks():
        metadata = governance_metadata_from_task(task)
        if metadata is not None:
            results.append(GovernanceTaskResult(task=task, governance=metadata))
    return results


def load_governance_task(project_root: Path, task_id: str) -> GovernanceTaskResult:
    store = SQLiteStore(Path(project_root).resolve())
    store.initialize()
    task = store.get_task(task_id)
    metadata = governance_metadata_from_task(task)
    if metadata is None:
        raise KeyError(f"Governance task not found: {task_id}")
    return GovernanceTaskResult(task=task, governance=metadata)


def close_governance_task(project_root: Path, task_id: str) -> GovernanceTaskResult:
    root = Path(project_root).resolve()
    store = SQLiteStore(root)
    store.initialize()
    task = store.get_task(task_id)
    metadata = governance_metadata_from_task(task)
    if metadata is None:
        raise KeyError(f"Governance task not found: {task_id}")
    if task.status not in {TaskStatus.CANCELLED, TaskStatus.SUCCEEDED, TaskStatus.SKIPPED}:
        task = store.cancel_task(task.id)
    closed = metadata.model_copy(update={"status": "closed", "closed_at": _now()})
    task = _replace_task_governance_metadata(store, task.id, closed)
    if task.session_id:
        session = store.get_session(task.session_id)
        store.update_session(
            task.session_id,
            metadata={**session.metadata, "governance_status": "closed", "governance_task_id": task.id},
        )
    return GovernanceTaskResult(task=task, governance=closed)


def update_governance_task_context_hash(project_root: Path, task_id: str, context_pack_hash: str) -> GovernanceTaskResult:
    root = Path(project_root).resolve()
    store = SQLiteStore(root)
    store.initialize()
    task = store.get_task(task_id)
    metadata = governance_metadata_from_task(task)
    if metadata is None:
        raise KeyError(f"Governance task not found: {task_id}")
    updated = metadata.model_copy(update={"context_pack_hash": context_pack_hash})
    task = _replace_task_governance_metadata(store, task.id, updated)
    return GovernanceTaskResult(task=task, governance=updated)


def update_governance_task_test_run_path(project_root: Path, task_id: str, test_run_path: str) -> GovernanceTaskResult:
    root = Path(project_root).resolve()
    store = SQLiteStore(root)
    store.initialize()
    task = store.get_task(task_id)
    metadata = governance_metadata_from_task(task)
    if metadata is None:
        raise KeyError(f"Governance task not found: {task_id}")
    updated = metadata.model_copy(update={"latest_test_run_path": test_run_path})
    task = _replace_task_governance_metadata(store, task.id, updated)
    return GovernanceTaskResult(task=task, governance=updated)


def update_governance_task_merge_check_verdict(project_root: Path, task_id: str, verdict: str) -> GovernanceTaskResult:
    root = Path(project_root).resolve()
    store = SQLiteStore(root)
    store.initialize()
    task = store.get_task(task_id)
    metadata = governance_metadata_from_task(task)
    if metadata is None:
        raise KeyError(f"Governance task not found: {task_id}")
    updated = metadata.model_copy(update={"latest_merge_check_verdict": verdict})
    task = _replace_task_governance_metadata(store, task.id, updated)
    return GovernanceTaskResult(task=task, governance=updated)


def governance_metadata_from_task(task: TaskRecord) -> GovernanceTaskMetadata | None:
    payload = task.metadata.get("governance")
    if not isinstance(payload, dict):
        return None
    if payload.get("schema_version") != GOVERNANCE_TASK_SCHEMA_VERSION:
        return None
    return GovernanceTaskMetadata.model_validate(payload)


def _replace_task_governance_metadata(
    store: SQLiteStore,
    task_id: str,
    metadata: GovernanceTaskMetadata,
) -> TaskRecord:
    task = store.get_task(task_id)
    next_metadata = dict(task.metadata)
    next_metadata["governance"] = metadata.model_dump(mode="json")
    next_metadata["governance_schema_version"] = metadata.schema_version
    next_metadata["governance_status"] = metadata.status
    timestamp = _now()
    with store.connect() as conn:
        conn.execute(
            "UPDATE tasks SET metadata_json = ?, updated_at = ? WHERE id = ?",
            (json.dumps(sanitize_for_logging(next_metadata), sort_keys=True, default=str), timestamp, task_id),
        )
    return store.get_task(task_id)


def _ensure_git_worktree_clean(root: Path, runner: TaskRunner) -> None:
    status = _git_stdout(runner, root, ["git", "status", "--porcelain"])
    if status.strip():
        raise ValueError("Working tree is dirty; commit, stash, or remove changes before creating a governed task worktree.")


def _git_stdout(runner: TaskRunner, root: Path, command: list[str]) -> str:
    result = _run_checked(runner, root, command)
    return (result.stdout or "").strip()


def _run_checked(runner: TaskRunner, root: Path, command: list[str]) -> subprocess.CompletedProcess[str]:
    result = runner(command, root)
    if result.returncode != 0:
        reason = (result.stderr or result.stdout or f"command failed: {' '.join(command)}").strip()
        raise ValueError(str(sanitize_for_logging(reason)))
    return result


def _run_command(command: list[str], root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=root, text=True, capture_output=True, timeout=120)


def _slugify(value: str) -> str:
    lowered = value.strip().lower()
    slug = re.sub(r"[^a-z0-9._-]+", "-", lowered).strip("-._")
    return slug[:48]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _sandbox_profile_for_tool_policy(tool_policy: str) -> str:
    if tool_policy == "isolated_code_edit":
        return "isolated_workspace"
    if tool_policy == "docker_test":
        return "docker_sandbox"
    return "read_only"


def _default_allowed_paths(tool_policy: str) -> tuple[str, ...]:
    if tool_policy == "isolated_code_edit":
        return ("src/**", "tests/**", "docs/**")
    if tool_policy == "docker_test":
        return ("tests/**", ".harness/governance/**")
    return ()


def _default_expected_artifacts(tool_policy: str) -> tuple[str, ...]:
    if tool_policy == "isolated_code_edit":
        return ("patch_summary", "test_plan", "branch_diff")
    if tool_policy == "docker_test":
        return ("test_report",)
    return ("workspace_summary",)
