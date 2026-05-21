from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from harness.governance.gate_registry import require_known_gate
from harness.governance.paths import governance_evidence_dir, governance_run_id
from harness.governance.protected_paths import protected_apply_path_match
from harness.governance.tasks import list_governance_tasks, update_governance_task_merge_check_verdict
from harness.security import sanitize_for_logging, scan_text_for_secrets


MergeRunner = Callable[[list[str], Path], subprocess.CompletedProcess[str]]

SCHEMA_VERSION = "harness.governance.merge_check/v1"
DEFAULT_TEST_COMMAND = [
    "python3",
    "-m",
    "pytest",
    "tests/test_governance_gate_registry.py",
    "tests/test_governance_protected_paths.py",
    "tests/test_governance_tasks.py",
    "tests/test_governance_context.py",
    "tests/test_governance_test_plan.py",
    "tests/test_governance_merge_check.py",
    "tests/test_governance_data_inventory.py",
    "tests/test_governance_network.py",
    "tests/test_governance_applyback_integration.py",
    "tests/test_governance_cli.py",
    "-q",
]
DANGEROUS_SUBPROCESS_STRINGS = (
    "--privileged",
    "--network=host",
    "--net=host",
    "docker.sock",
    "rm -rf /",
    "git push --force",
    "--no-verify",
    "--no-gpg-sign",
)
AUTHORITY_DRIFT_PATTERNS = (
    "active_repo_write: allowed",
    "hosted_boundary: allowed",
    "external_network: allowed",
    "paid_provider: allowed",
    "docker_execution: allowed",
)
SANDBOX_NETWORK_PATTERNS = (
    "network: allowed",
    "network=host",
    "host_network_available",
    "external_network: allowed",
)
PROVIDER_PERMISSION_PATTERNS = (
    "allowed_adapters:",
    "allowed_task_types:",
    "hosted_boundary: allowed",
    "paid_provider: allowed",
)
CORE_DELETE_PREFIXES = ("src/harness/", "tests/", "docs/")
VENDORED_PREFIXES = ("vendor/", "third_party/", "node_modules/")
HIGH_DELETION_RATIO = 0.25
HIGH_DELETION_MINIMUM = 5
LARGE_DIFF_CHANGED_LIMIT = 250
NEAR_LARGE_DIFF_CHANGED_LIMIT = 100


@dataclass(frozen=True)
class GovernanceMergeCheckResult:
    payload: dict[str, object]
    path: Path
    exit_code: int


def run_governance_merge_check(
    project_root: Path,
    *,
    branch: str,
    base: str = "main",
    strict: bool = False,
    runner: MergeRunner | None = None,
    test_command: list[str] | None = None,
) -> GovernanceMergeCheckResult:
    root = Path(project_root).resolve()
    active_runner = runner or _run_command
    run_id = governance_run_id("merge-check", branch)
    evidence_dir = governance_evidence_dir(root, "merge-check", run_id)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    generated_at = _now()

    try:
        dirty_status = _git_stdout(active_runner, root, ["git", "status", "--porcelain"], allow_failure=False)
        if dirty_status.strip():
            return _operational_error(
                root=root,
                evidence_dir=evidence_dir,
                run_id=run_id,
                generated_at=generated_at,
                branch=branch,
                base=base,
                reason="Working tree is dirty; merge-check requires committed or stashed changes.",
            )
        base_sha = _git_stdout(active_runner, root, ["git", "rev-parse", "--verify", base], allow_failure=False)
        head_sha = _git_stdout(active_runner, root, ["git", "rev-parse", "--verify", branch], allow_failure=False)
    except ValueError as exc:
        return _operational_error(
            root=root,
            evidence_dir=evidence_dir,
            run_id=run_id,
            generated_at=generated_at,
            branch=branch,
            base=base,
            reason=str(exc),
        )

    merge_base_result = active_runner(["git", "merge-base", base, branch], root)
    merge_base = (merge_base_result.stdout or "").strip() if merge_base_result.returncode == 0 else None
    ahead_behind = _ahead_behind(active_runner, root, base, branch) if merge_base else (0, 0)
    behind, ahead = ahead_behind
    name_status = _name_status(active_runner, root, base, branch) if merge_base else []
    files_touched = [path for _state, path in name_status]
    deleted_files = [path for state, path in name_status if state.startswith("D")]
    diff_text = _git_stdout(active_runner, root, ["git", "diff", f"{base}...{branch}"], allow_failure=True)
    numstat = _numstat(active_runner, root, base, branch) if merge_base else {"files": len(files_touched), "insertions": 0, "deletions": 0}
    commits = _commits(active_runner, root, base, branch) if merge_base else []

    (evidence_dir / "diff.patch").write_text(str(sanitize_for_logging(diff_text)), encoding="utf-8")
    (evidence_dir / "diff_files.txt").write_text("\n".join(files_touched), encoding="utf-8")
    (evidence_dir / "commits.json").write_text(json.dumps(commits, indent=2, sort_keys=True), encoding="utf-8")

    added_text = "\n".join(_added_lines(diff_text))
    secret_findings = [finding.to_dict() for finding in scan_text_for_secrets(added_text)]
    dangerous_findings = _pattern_findings(added_text, DANGEROUS_SUBPROCESS_STRINGS, "dangerous_execution_string")
    protected_hits = [
        match.to_dict()
        for path in files_touched
        for match in [protected_apply_path_match(path)]
        if match is not None
    ]
    drift_findings = _workspace_authority_drift_findings(added_text, files_touched)
    provider_findings = _pattern_findings(added_text, PROVIDER_PERMISSION_PATTERNS, "provider_permission_widening")
    sandbox_findings = _pattern_findings(added_text, SANDBOX_NETWORK_PATTERNS, "sandbox_network_widening")
    core_deletions = [path for path in deleted_files if _is_core_path(path)]
    vendored_paths = [path for path in files_touched if _is_vendored_path(path)]
    high_deletion_ratio = (
        len(deleted_files) >= HIGH_DELETION_MINIMUM
        and bool(files_touched)
        and (len(deleted_files) / len(files_touched)) >= HIGH_DELETION_RATIO
    )

    test_result = active_runner(test_command or DEFAULT_TEST_COMMAND, root)
    pytest_log = "\n".join(
        [
            "$ " + " ".join(test_command or DEFAULT_TEST_COMMAND),
            "",
            "STDOUT:",
            str(sanitize_for_logging(test_result.stdout or "")),
            "",
            "STDERR:",
            str(sanitize_for_logging(test_result.stderr or "")),
        ]
    )
    pytest_log_path = evidence_dir / "pytest.log"
    pytest_log_path.write_text(pytest_log, encoding="utf-8")

    secret_scan = {"scanned_files": len(files_touched), "findings": secret_findings}
    (evidence_dir / "secret_scan.json").write_text(json.dumps(secret_scan, indent=2, sort_keys=True), encoding="utf-8")
    drift_payload = {"findings": drift_findings, "provider_findings": provider_findings, "sandbox_findings": sandbox_findings}
    (evidence_dir / "drift.json").write_text(json.dumps(sanitize_for_logging(drift_payload), indent=2, sort_keys=True), encoding="utf-8")

    hard_gates = [
        _gate("merge_base_resolves", merge_base is not None, f"merge_base={merge_base or 'unresolved'}"),
        _gate("branch_contains_current_base", behind == 0, f"behind={behind} ahead={ahead}"),
        _gate("no_protected_writes", not protected_hits, f"protected_hits={len(protected_hits)}"),
        _gate("no_secret_in_diff", not secret_findings, f"secret_findings={len(secret_findings)}"),
        _gate("no_dangerous_subprocess_strings", not dangerous_findings, f"dangerous_findings={len(dangerous_findings)}"),
        _gate("tests_pass", test_result.returncode == 0, f"exit_code={test_result.returncode}"),
        _gate("no_workspace_authority_drift", not drift_findings, f"drift_findings={len(drift_findings)}"),
        _gate("no_provider_permission_widening", not provider_findings, f"provider_findings={len(provider_findings)}"),
        _gate("no_unsafe_sandbox_network_change", not sandbox_findings, f"sandbox_findings={len(sandbox_findings)}"),
        _gate("no_mass_deletion_shape", not high_deletion_ratio, f"deleted={len(deleted_files)} changed={len(files_touched)}"),
        _gate("no_core_workspace_deletions", not core_deletions, f"core_deletions={len(core_deletions)}"),
        _gate("diff_size_bounded", len(files_touched) <= LARGE_DIFF_CHANGED_LIMIT, f"changed={len(files_touched)} limit={LARGE_DIFF_CHANGED_LIMIT}"),
        _gate("no_vendored_third_party_diff", not vendored_paths, f"vendored_paths={len(vendored_paths)}"),
    ]
    soft_findings = _soft_findings(files_touched, name_status, commits, numstat)
    if strict:
        soft_findings = [
            {**finding, "strict_escalated": True}
            if finding.get("severity") == "warning"
            else finding
            for finding in soft_findings
        ]
    verdict = _verdict_for(hard_gates, soft_findings, strict=strict)
    reason = _reason_for(verdict, hard_gates, soft_findings)
    evidence = {
        "diff_stat": numstat,
        "files_touched": files_touched,
        "tests_run": {
            "command": " ".join(test_command or DEFAULT_TEST_COMMAND),
            "exit_code": test_result.returncode,
            "passed": _pytest_count(test_result.stdout or "", "passed"),
            "failed": _pytest_count(test_result.stdout or "", "failed"),
            "log_path": _rel(root, pytest_log_path),
        },
        "drift_findings": drift_findings,
        "provider_permission_findings": provider_findings,
        "sandbox_network_findings": sandbox_findings,
        "dangerous_findings": dangerous_findings,
        "applyback_protected_hits": protected_hits,
        "secret_scan": secret_scan,
        "commits": commits,
        "readiness": {
            "merge_base": merge_base,
            "behind": behind,
            "ahead": ahead,
            "changed_count": len(files_touched),
            "deleted_count": len(deleted_files),
            "high_deletion_ratio": high_deletion_ratio,
        },
    }
    payload: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "generated_at": generated_at,
        "branch": branch,
        "base": base,
        "head_sha": head_sha,
        "base_sha": base_sha,
        "verdict": verdict,
        "reason": reason,
        "summary": f"Merge-check for {branch} against {base}: {verdict}. {reason}",
        "hard_gates": hard_gates,
        "soft_findings": soft_findings,
        "evidence": evidence,
        "remediations": _remediations(hard_gates, soft_findings),
        "operator_authority": {
            "merge_performed": False,
            "push_performed": False,
            "provider_called": False,
            "comments_posted": False,
            "needs_human_approval": verdict != "approve",
        },
        "report_links": {
            "evidence_dir": _rel(root, evidence_dir),
            "session_id": None,
        },
    }
    clean = sanitize_for_logging(payload)
    verdict_path = evidence_dir / "verdict.json"
    verdict_path.write_text(json.dumps(clean, indent=2, sort_keys=True), encoding="utf-8")
    _update_matching_governance_task(root, branch, verdict)
    return GovernanceMergeCheckResult(payload=clean if isinstance(clean, dict) else payload, path=verdict_path, exit_code=_exit_code(verdict))


def _operational_error(
    *,
    root: Path,
    evidence_dir: Path,
    run_id: str,
    generated_at: str,
    branch: str,
    base: str,
    reason: str,
) -> GovernanceMergeCheckResult:
    payload: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "generated_at": generated_at,
        "branch": branch,
        "base": base,
        "head_sha": None,
        "base_sha": None,
        "verdict": "error",
        "reason": str(sanitize_for_logging(reason)),
        "summary": f"Merge-check could not run for {branch} against {base}: {sanitize_for_logging(reason)}",
        "hard_gates": [],
        "soft_findings": [],
        "evidence": {},
        "remediations": [],
        "operator_authority": {
            "merge_performed": False,
            "push_performed": False,
            "provider_called": False,
            "comments_posted": False,
            "needs_human_approval": True,
        },
        "report_links": {"evidence_dir": _rel(root, evidence_dir), "session_id": None},
    }
    path = evidence_dir / "verdict.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    for name in ("pytest.log", "diff.patch", "diff_files.txt"):
        (evidence_dir / name).touch(exist_ok=True)
    (evidence_dir / "drift.json").write_text(json.dumps({"findings": []}, indent=2), encoding="utf-8")
    (evidence_dir / "secret_scan.json").write_text(json.dumps({"findings": []}, indent=2), encoding="utf-8")
    (evidence_dir / "commits.json").write_text("[]\n", encoding="utf-8")
    return GovernanceMergeCheckResult(payload=payload, path=path, exit_code=1)


def _gate(gate_id: str, passed: bool, evidence: str) -> dict[str, object]:
    spec = require_known_gate(gate_id)
    return {"id": spec.id, "passed": passed, "evidence": evidence, "description": spec.description}


def _verdict_for(hard_gates: list[dict[str, object]], soft_findings: list[dict[str, object]], *, strict: bool) -> str:
    if any(not gate.get("passed") for gate in hard_gates):
        return "reject"
    if any(finding.get("severity") == "error" for finding in soft_findings):
        return "request_changes"
    if strict and any(finding.get("severity") == "warning" for finding in soft_findings):
        return "request_changes"
    if soft_findings:
        return "request_changes"
    return "approve"


def _reason_for(verdict: str, hard_gates: list[dict[str, object]], soft_findings: list[dict[str, object]]) -> str:
    failed = [str(gate["id"]) for gate in hard_gates if not gate.get("passed")]
    if failed:
        return f"Failed hard gates: {', '.join(failed)}."
    if soft_findings:
        return f"Soft findings require review: {len(soft_findings)} finding(s)."
    if verdict == "approve":
        return "All hard gates passed and no soft findings were recorded."
    return "Merge-check completed."


def _exit_code(verdict: str) -> int:
    return {"approve": 0, "request_changes": 2, "reject": 3, "error": 1}.get(verdict, 1)


def _remediations(hard_gates: list[dict[str, object]], soft_findings: list[dict[str, object]]) -> list[dict[str, object]]:
    remediations: list[dict[str, object]] = []
    rank = 1
    for gate in hard_gates:
        if gate.get("passed"):
            continue
        remediations.append(
            {
                "rank": rank,
                "action": f"Resolve failed hard gate {gate['id']}.",
                "reason": gate.get("evidence") or gate.get("description"),
                "estimated_impact": "required before merge",
                "risk": "depends on remediation",
            }
        )
        rank += 1
    for finding in soft_findings:
        remediations.append(
            {
                "rank": rank,
                "action": f"Review soft finding {finding['id']}.",
                "reason": finding.get("message", ""),
                "estimated_impact": "may unblock request_changes verdict",
                "risk": "low if reviewed locally",
            }
        )
        rank += 1
    return remediations


def _git_stdout(runner: MergeRunner, root: Path, command: list[str], *, allow_failure: bool) -> str:
    result = runner(command, root)
    if result.returncode != 0:
        if allow_failure:
            return ""
        reason = (result.stderr or result.stdout or f"command failed: {' '.join(command)}").strip()
        raise ValueError(str(sanitize_for_logging(reason)))
    return (result.stdout or "").strip()


def _ahead_behind(runner: MergeRunner, root: Path, base: str, branch: str) -> tuple[int, int]:
    result = runner(["git", "rev-list", "--left-right", "--count", f"{base}...{branch}"], root)
    if result.returncode != 0:
        return 0, 0
    parts = (result.stdout or "").split()
    if len(parts) != 2:
        return 0, 0
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return 0, 0


def _name_status(runner: MergeRunner, root: Path, base: str, branch: str) -> list[tuple[str, str]]:
    result = runner(["git", "diff", "--name-status", f"{base}...{branch}"], root)
    if result.returncode != 0:
        return []
    parsed: list[tuple[str, str]] = []
    for line in (result.stdout or "").splitlines():
        fields = line.split("\t")
        if len(fields) >= 2:
            parsed.append((fields[0], fields[-1]))
    return parsed


def _numstat(runner: MergeRunner, root: Path, base: str, branch: str) -> dict[str, int]:
    result = runner(["git", "diff", "--numstat", f"{base}...{branch}"], root)
    files = insertions = deletions = 0
    if result.returncode != 0:
        return {"files": 0, "insertions": 0, "deletions": 0}
    for line in (result.stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        files += 1
        insertions += _int_stat(parts[0])
        deletions += _int_stat(parts[1])
    return {"files": files, "insertions": insertions, "deletions": deletions}


def _int_stat(value: str) -> int:
    try:
        return int(value)
    except ValueError:
        return 0


def _commits(runner: MergeRunner, root: Path, base: str, branch: str) -> list[dict[str, str]]:
    result = runner(["git", "log", "--format=%H%x09%s%x09%an <%ae>", f"{base}...{branch}"], root)
    if result.returncode != 0:
        return []
    commits: list[dict[str, str]] = []
    for line in (result.stdout or "").splitlines():
        sha, subject, author = (line.split("\t") + ["", "", ""])[:3]
        commits.append({"sha": sha, "subject": subject, "author": author})
    return commits


def _added_lines(diff_text: str) -> list[str]:
    lines: list[str] = []
    for line in diff_text.splitlines():
        if line.startswith("+") and not line.startswith("+++") and not line.startswith("+@@"):
            lines.append(line[1:])
    return lines


def _pattern_findings(text: str, patterns: tuple[str, ...], finding_id: str) -> list[dict[str, object]]:
    findings: list[dict[str, object]] = []
    lowered = text.lower()
    for pattern in patterns:
        if pattern.lower() in lowered:
            findings.append({"id": finding_id, "pattern": pattern, "severity": "critical"})
    return findings


def _workspace_authority_drift_findings(added_text: str, files_touched: list[str]) -> list[dict[str, object]]:
    findings = _pattern_findings(added_text, AUTHORITY_DRIFT_PATTERNS, "workspace_authority_drift")
    for path in files_touched:
        if path in {"pyproject.toml", "src/harness/policy.py", "src/harness/approvals.py"}:
            findings.append({"id": "workspace_authority_drift", "path": path, "severity": "critical"})
    return findings


def _soft_findings(
    files_touched: list[str],
    name_status: list[tuple[str, str]],
    commits: list[dict[str, str]],
    diff_stat: dict[str, int],
) -> list[dict[str, object]]:
    findings: list[dict[str, object]] = []
    if len(files_touched) >= NEAR_LARGE_DIFF_CHANGED_LIMIT:
        findings.append({"id": "diff_size_near_limit", "severity": "warning", "message": f"Branch touches {len(files_touched)} files."})
    added_modules = [path for state, path in name_status if state.startswith("A") and path.startswith("src/harness/") and path.endswith(".py")]
    test_paths = {path for path in files_touched if path.startswith("tests/")}
    for module in added_modules:
        stem = Path(module).stem
        if not any(stem in Path(test).stem for test in test_paths):
            findings.append({"id": "new_module_without_test", "severity": "warning", "message": f"New module lacks an obvious matching test: {module}", "path": module})
    has_docs = any(path.startswith("docs/") for path in files_touched)
    has_code = any(path.startswith("src/") for path in files_touched)
    if has_docs and not has_code:
        findings.append({"id": "doc_code_drift", "severity": "info", "message": "Documentation changed without a matching code change."})
    if "pyproject.toml" in files_touched:
        findings.append({"id": "dependency_manifest_changed", "severity": "warning", "message": "pyproject.toml changed; dependency impact requires review.", "path": "pyproject.toml"})
    if diff_stat.get("insertions", 0) + diff_stat.get("deletions", 0) > 200:
        subject_only = [commit for commit in commits if commit.get("subject") and "\n" not in commit.get("subject", "")]
        if subject_only:
            findings.append({"id": "large_diff_subject_only_commits", "severity": "warning", "message": "Large diff has commits with subject-only metadata."})
    return findings


def _pytest_count(text: str, label: str) -> int:
    match = re.search(rf"(\d+)\s+{re.escape(label)}", text)
    return int(match.group(1)) if match else 0


def _is_core_path(path: str) -> bool:
    return any(path.startswith(prefix) for prefix in CORE_DELETE_PREFIXES)


def _is_vendored_path(path: str) -> bool:
    return any(path.startswith(prefix) for prefix in VENDORED_PREFIXES)


def _update_matching_governance_task(root: Path, branch: str, verdict: str) -> None:
    if not (root / ".harness" / "harness.sqlite").exists():
        return
    try:
        for result in list_governance_tasks(root):
            if result.governance.branch == branch:
                update_governance_task_merge_check_verdict(root, result.task.id, verdict)
                return
    except Exception:
        return


def _rel(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _run_command(command: list[str], root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=root, text=True, capture_output=True, timeout=900)
