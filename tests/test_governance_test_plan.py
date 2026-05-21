from __future__ import annotations

import json
import subprocess
from pathlib import Path

from harness.governance.tasks import create_governance_task, load_governance_task
from harness.governance.test_plan import plan_governance_tests, run_governance_tests
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


def _plan_runner(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    if command[:3] == ["git", "diff", "--name-only"]:
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="src/harness/governance/tasks.py\ntests/test_governance_tasks.py\n",
            stderr="",
        )
    return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")


def test_governance_test_plan_maps_scope_and_links_policy(tmp_path: Path) -> None:
    SQLiteStore(tmp_path).initialize()
    created = create_governance_task(
        tmp_path,
        slug="Governance Tests",
        agent_id="code_editor",
        goal="Implement governance security tests",
        runner=_task_runner,
    )

    plan = plan_governance_tests(tmp_path, created.task.id, runner=_plan_runner)

    assert plan.schema_version == "harness.governance_test_plan/v1"
    assert plan.task_type == "governance_security"
    assert plan.policy_hash
    assert plan.payload["base_sha"] == "abc123"
    assert plan.payload["branch"].startswith("harness/task/governance-tests-")
    assert "no_protected_writes" in plan.payload["gate_ids"]
    assert any(test["name"] == "governance-records" for test in plan.payload["tests"])
    assert plan.payload["side_effects"]["evidence_written"] is False


def test_governance_test_run_writes_evidence_and_updates_task(tmp_path: Path) -> None:
    SQLiteStore(tmp_path).initialize()
    created = create_governance_task(
        tmp_path,
        slug="Run Tests",
        agent_id="code_editor",
        goal="Implement governance security tests",
        runner=_task_runner,
    )
    calls: list[list[str]] = []

    def run_runner(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        if command[:3] == ["git", "diff", "--name-only"]:
            return _plan_runner(command, cwd)
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz\nok\n", stderr="")

    result = run_governance_tests(tmp_path, created.task.id, runner=run_runner)

    assert result.schema_version == "harness.governance_test_run/v1"
    assert result.ok is True
    assert result.status == "pass"
    assert calls
    evidence_path = Path(result.path)
    assert evidence_path.exists()
    payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert payload["links"]["base_sha"] == "abc123"
    assert payload["links"]["policy_hash"] == result.policy_hash
    assert "no_protected_writes" in payload["links"]["gate_ids"]
    assert payload["side_effects"]["provider_called"] is False
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in evidence_path.read_text(encoding="utf-8")
    assert (evidence_path.parent / "test-plan.json").exists()
    assert any(path.name.endswith(".stdout.log") for path in evidence_path.parent.iterdir())

    refreshed = load_governance_task(tmp_path, created.task.id)
    assert refreshed.governance.latest_test_run_path == evidence_path.relative_to(tmp_path).as_posix()


def test_governance_test_run_records_failure(tmp_path: Path) -> None:
    SQLiteStore(tmp_path).initialize()
    created = create_governance_task(
        tmp_path,
        slug="Fail Tests",
        agent_id="code_editor",
        goal="Implement governance security tests",
        runner=_task_runner,
    )

    def fail_runner(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        if command[:3] == ["git", "diff", "--name-only"]:
            return _plan_runner(command, cwd)
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="failed\n")

    result = run_governance_tests(tmp_path, created.task.id, runner=fail_runner)

    assert result.ok is False
    assert result.status == "fail"
    assert Path(result.path).exists()
