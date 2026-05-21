from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from harness.governance.tasks import (
    close_governance_task,
    create_governance_task,
    list_governance_tasks,
    load_governance_task,
)
from harness.memory.sqlite_store import SQLiteStore
from harness.models import TaskStatus


def _runner(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    if command[:3] == ["git", "status", "--porcelain"]:
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
    if command[:3] == ["git", "rev-parse", "--verify"]:
        return subprocess.CompletedProcess(command, 0, stdout="abc123\n", stderr="")
    if command[:3] == ["git", "worktree", "add"]:
        Path(command[-2]).mkdir(parents=True, exist_ok=True)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
    return subprocess.CompletedProcess(command, 1, stdout="", stderr=f"unexpected command: {command}")


def test_create_governance_task_records_contract_and_session(tmp_path: Path) -> None:
    SQLiteStore(tmp_path).initialize()

    result = create_governance_task(
        tmp_path,
        slug="Governance Slice",
        agent_id="code_editor",
        goal="Implement governed task records",
        base="main",
        runner=_runner,
    )

    governance = result.governance
    assert governance.schema_version == "harness.governance_task/v1"
    assert governance.task_id == result.task.id
    assert governance.slug == "governance-slice"
    assert governance.branch.startswith("harness/task/governance-slice-")
    assert governance.base == "main"
    assert governance.base_sha == "abc123"
    assert governance.agent == "code_editor"
    assert governance.model_profile == "codex_supervised"
    assert governance.permission_profile == "isolated_code_edit"
    assert governance.sandbox_profile == "isolated_workspace"
    assert governance.allowed_paths == ["src/**", "tests/**", "docs/**"]
    assert governance.expected_artifacts
    assert governance.status == "active"

    store = SQLiteStore(tmp_path)
    task = store.get_task(result.task.id)
    assert task.metadata["governance"]["task_id"] == task.id
    assert task.metadata["governance_status"] == "active"
    assert task.session_id == governance.session_id
    session = store.get_session(governance.session_id)
    assert session.metadata["surface"] == "governance"
    assert session.metadata["governance_task_id"] == task.id
    assert session.active_task_id == task.id


def test_governance_task_list_show_and_close(tmp_path: Path) -> None:
    SQLiteStore(tmp_path).initialize()
    created = create_governance_task(
        tmp_path,
        slug="Closable",
        agent_id="code_editor",
        goal="Close this task",
        runner=_runner,
    )

    listed = list_governance_tasks(tmp_path)
    shown = load_governance_task(tmp_path, created.task.id)
    closed = close_governance_task(tmp_path, created.task.id)

    assert [item.task.id for item in listed] == [created.task.id]
    assert shown.governance.slug == "closable"
    assert closed.governance.status == "closed"
    assert closed.governance.closed_at is not None
    assert closed.task.status == TaskStatus.CANCELLED


def test_create_governance_task_refuses_unknown_agent(tmp_path: Path) -> None:
    SQLiteStore(tmp_path).initialize()

    with pytest.raises(ValueError, match="Agent not found"):
        create_governance_task(
            tmp_path,
            slug="Bad Agent",
            agent_id="missing_agent",
            goal="fail",
            runner=_runner,
        )


def test_create_governance_task_refuses_dirty_worktree(tmp_path: Path) -> None:
    SQLiteStore(tmp_path).initialize()

    def dirty_runner(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        if command[:3] == ["git", "status", "--porcelain"]:
            return subprocess.CompletedProcess(command, 0, stdout=" M README.md\n", stderr="")
        return _runner(command, cwd)

    with pytest.raises(ValueError, match="Working tree is dirty"):
        create_governance_task(
            tmp_path,
            slug="Dirty",
            agent_id="code_editor",
            goal="fail",
            runner=dirty_runner,
        )


def test_create_governance_task_refuses_missing_base(tmp_path: Path) -> None:
    SQLiteStore(tmp_path).initialize()

    def missing_base_runner(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        if command[:3] == ["git", "rev-parse", "--verify"]:
            return subprocess.CompletedProcess(command, 128, stdout="", stderr="fatal: Needed a single revision")
        return _runner(command, cwd)

    with pytest.raises(ValueError, match="Needed a single revision"):
        create_governance_task(
            tmp_path,
            slug="Missing Base",
            agent_id="code_editor",
            goal="fail",
            runner=missing_base_runner,
        )
