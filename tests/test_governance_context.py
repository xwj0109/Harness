from __future__ import annotations

import json
import subprocess
from pathlib import Path

from harness.governance.context import build_governance_context_pack
from harness.governance.tasks import create_governance_task, load_governance_task
from harness.memory.sqlite_store import SQLiteStore


def _task_runner(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    if command[:3] == ["git", "status", "--porcelain"]:
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
    if command[:3] == ["git", "rev-parse", "--verify"]:
        return subprocess.CompletedProcess(command, 0, stdout="abc123\n", stderr="")
    if command[:3] == ["git", "worktree", "add"]:
        Path(command[-2]).mkdir(parents=True, exist_ok=True)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
    return subprocess.CompletedProcess(command, 1, stdout="", stderr=f"unexpected command: {command}")


def _context_runner(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    if command[:2] == ["git", "diff"]:
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="+OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz\n+safe change\n",
            stderr="",
        )
    return subprocess.CompletedProcess(command, 1, stdout="", stderr=f"unexpected command: {command}")


def test_build_governance_context_pack_redacts_hashes_and_updates_task(tmp_path: Path) -> None:
    SQLiteStore(tmp_path).initialize()
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / "docs" / "plans" / "toloclaw_governance_parity_plan.md").write_text(
        "Plan TOKEN=secretvalue\n",
        encoding="utf-8",
    )
    created = create_governance_task(
        tmp_path,
        slug="Context Pack",
        agent_id="code_editor",
        goal="Build context packs",
        runner=_task_runner,
    )

    result = build_governance_context_pack(tmp_path, created.task.id, runner=_context_runner)

    assert result.schema_version == "harness.governance_context_pack/v1"
    assert result.ok is True
    assert result.sha256
    path = Path(result.path)
    assert path.exists()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["sha256"] == result.sha256
    assert payload["task"]["id"] == created.task.id
    assert payload["governance_task"]["context_pack_hash"] is None
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in path.read_text(encoding="utf-8")
    assert "secretvalue" not in path.read_text(encoding="utf-8")
    assert payload["side_effects"]["provider_called"] is False
    assert payload["side_effects"]["network_called"] is False
    assert payload["side_effects"]["repo_files_modified"] is False
    assert payload["governance"]["gate_registry"]["schema_version"] == "harness.governance.gate_registry/v1"
    assert payload["required_test_plan"]["schema_version"] == "harness.governance_test_plan/v1"
    assert payload["required_test_plan"]["tests"]

    refreshed = load_governance_task(tmp_path, created.task.id)
    assert refreshed.governance.context_pack_hash == result.sha256


def test_build_governance_context_pack_hash_is_stable_for_same_content(tmp_path: Path) -> None:
    SQLiteStore(tmp_path).initialize()
    created = create_governance_task(
        tmp_path,
        slug="Stable",
        agent_id="code_editor",
        goal="Stable context",
        runner=_task_runner,
    )

    first = build_governance_context_pack(tmp_path, created.task.id, runner=_context_runner)
    second = build_governance_context_pack(tmp_path, created.task.id, runner=_context_runner)

    assert first.sha256 == second.sha256
