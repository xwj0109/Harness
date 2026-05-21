from __future__ import annotations

import json
import subprocess
from pathlib import Path

from harness.governance.merge_check import run_governance_merge_check
from harness.governance.tasks import create_governance_task, load_governance_task
from harness.memory.sqlite_store import SQLiteStore


def _completed(command: list[str], returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, returncode, stdout=stdout, stderr=stderr)


class MergeRunner:
    def __init__(
        self,
        *,
        status: str = "",
        name_status: str = "M\tsrc/harness/feature.py\nM\ttests/test_feature.py\n",
        diff: str = "+safe change\n",
        numstat: str = "10\t1\tsrc/harness/feature.py\n2\t0\ttests/test_feature.py\n",
        rev_list: str = "0\t1\n",
        test_returncode: int = 0,
        test_stdout: str = "7 passed in 0.01s\n",
    ) -> None:
        self.status = status
        self.name_status = name_status
        self.diff = diff
        self.numstat = numstat
        self.rev_list = rev_list
        self.test_returncode = test_returncode
        self.test_stdout = test_stdout
        self.commands: list[list[str]] = []

    def __call__(self, command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        if command[:3] == ["git", "status", "--porcelain"]:
            return _completed(command, stdout=self.status)
        if command[:3] == ["git", "rev-parse", "--verify"]:
            rev = command[-1]
            return _completed(command, stdout=f"{rev}-sha\n")
        if command[:3] == ["git", "merge-base", "main"]:
            return _completed(command, stdout="merge-base-sha\n")
        if command[:3] == ["git", "rev-list", "--left-right"]:
            return _completed(command, stdout=self.rev_list)
        if command[:3] == ["git", "diff", "--name-status"]:
            return _completed(command, stdout=self.name_status)
        if command[:3] == ["git", "diff", "--numstat"]:
            return _completed(command, stdout=self.numstat)
        if command[:2] == ["git", "diff"]:
            return _completed(command, stdout=self.diff)
        if command[:2] == ["git", "log"]:
            return _completed(command, stdout="abc\tImplement feature\tUser <u@example.com>\n")
        if command[:3] == ["git", "worktree", "add"]:
            Path(command[-2]).mkdir(parents=True, exist_ok=True)
            return _completed(command)
        if command[:3] == ["python3", "-m", "pytest"]:
            return _completed(command, returncode=self.test_returncode, stdout=self.test_stdout, stderr="")
        return _completed(command, returncode=1, stderr=f"unexpected command: {command}")


def _task_runner(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    if command[:3] == ["git", "status", "--porcelain"]:
        return _completed(command)
    if command[:3] == ["git", "rev-parse", "--verify"]:
        return _completed(command, stdout="base-sha\n")
    if command[:3] == ["git", "worktree", "add"]:
        Path(command[-2]).mkdir(parents=True, exist_ok=True)
        return _completed(command)
    return _completed(command, returncode=1, stderr=f"unexpected command: {command}")


def test_merge_check_approves_clean_branch_and_writes_evidence(tmp_path: Path) -> None:
    runner = MergeRunner()

    result = run_governance_merge_check(tmp_path, branch="feature", base="main", runner=runner)

    assert result.exit_code == 0
    assert result.payload["schema_version"] == "harness.governance.merge_check/v1"
    assert result.payload["verdict"] == "approve"
    assert result.payload["operator_authority"]["merge_performed"] is False
    assert result.payload["operator_authority"]["provider_called"] is False
    evidence_dir = result.path.parent
    for name in ("verdict.json", "pytest.log", "diff.patch", "diff_files.txt", "drift.json", "secret_scan.json", "commits.json"):
        assert (evidence_dir / name).exists()
    assert json.loads(result.path.read_text(encoding="utf-8"))["verdict"] == "approve"


def test_merge_check_rejects_secret_in_added_lines(tmp_path: Path) -> None:
    runner = MergeRunner(diff="+OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz\n")

    result = run_governance_merge_check(tmp_path, branch="feature", base="main", runner=runner)

    assert result.exit_code == 3
    assert result.payload["verdict"] == "reject"
    gates = {gate["id"]: gate for gate in result.payload["hard_gates"]}
    assert gates["no_secret_in_diff"]["passed"] is False
    assert result.payload["evidence"]["secret_scan"]["findings"]


def test_merge_check_rejects_protected_path_and_dangerous_string(tmp_path: Path) -> None:
    runner = MergeRunner(
        name_status="M\tsrc/harness/session_tools.py\n",
        diff="+subprocess.run(['docker', '--privileged'])\n",
        numstat="1\t0\tsrc/harness/session_tools.py\n",
    )

    result = run_governance_merge_check(tmp_path, branch="feature", base="main", runner=runner)

    gates = {gate["id"]: gate for gate in result.payload["hard_gates"]}
    assert result.payload["verdict"] == "reject"
    assert gates["no_protected_writes"]["passed"] is False
    assert gates["no_dangerous_subprocess_strings"]["passed"] is False


def test_merge_check_request_changes_for_soft_findings(tmp_path: Path) -> None:
    runner = MergeRunner(
        name_status="A\tsrc/harness/new_module.py\n",
        numstat="5\t0\tsrc/harness/new_module.py\n",
    )

    result = run_governance_merge_check(tmp_path, branch="feature", base="main", runner=runner, strict=True)

    assert result.exit_code == 2
    assert result.payload["verdict"] == "request_changes"
    assert any(finding["id"] == "new_module_without_test" for finding in result.payload["soft_findings"])


def test_merge_check_dirty_worktree_is_operational_error_with_evidence(tmp_path: Path) -> None:
    runner = MergeRunner(status=" M README.md\n")

    result = run_governance_merge_check(tmp_path, branch="feature", base="main", runner=runner)

    assert result.exit_code == 1
    assert result.payload["verdict"] == "error"
    assert "dirty" in result.payload["reason"]
    assert result.path.exists()
    assert (result.path.parent / "pytest.log").exists()


def test_merge_check_updates_matching_governance_task_verdict(tmp_path: Path) -> None:
    SQLiteStore(tmp_path).initialize()
    created = create_governance_task(
        tmp_path,
        slug="Merge Verdict",
        agent_id="code_editor",
        goal="Track merge verdict",
        runner=_task_runner,
    )
    runner = MergeRunner()

    result = run_governance_merge_check(tmp_path, branch=created.governance.branch, base="main", runner=runner)

    assert result.payload["verdict"] == "approve"
    refreshed = load_governance_task(tmp_path, created.task.id)
    assert refreshed.governance.latest_merge_check_verdict == "approve"
