from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from harness.governance.gate_registry import GATES_BY_ID
from harness.governance.models import GovernanceTestPlanResult, GovernanceTestRunResult
from harness.governance.paths import governance_evidence_dir, governance_run_id
from harness.governance.tasks import load_governance_task, update_governance_task_test_run_path
from harness.policy import stable_json_sha256
from harness.security import sanitize_for_logging


TestRunner = Callable[[list[str], Path], subprocess.CompletedProcess[str]]

LOG_TAIL_CHARS = 4_000


def plan_governance_tests(
    project_root: Path,
    task_id: str,
    *,
    runner: TestRunner | None = None,
) -> GovernanceTestPlanResult:
    root = Path(project_root).resolve()
    task_result = load_governance_task(root, task_id)
    governance = task_result.governance
    changed_paths = _changed_paths(root, governance.base, governance.branch, runner or _run_command)
    task_type = _task_type(governance.goal, governance.allowed_paths, changed_paths)
    tests = _tests_for(task_type, changed_paths)
    gate_ids = _gate_ids_for(task_type)
    payload = {
        "schema_version": "harness.governance_test_plan/v1",
        "generated_at": _now(),
        "task_id": task_id,
        "task_type": task_type,
        "base": governance.base,
        "base_sha": governance.base_sha,
        "branch": governance.branch,
        "changed_paths": changed_paths,
        "policy": "Task-level tests are scoped evidence; merge-check remains the hard integration gate.",
        "gate_ids": gate_ids,
        "tests": tests,
        "merge_hard_gate": {
            "category": "merge",
            "name": "governance-merge-check",
            "command": ["harness", "governance", "merge-check", governance.branch, "--base", governance.base, "--project", "."],
            "required": True,
            "deferred_until_slice": 5,
        },
        "side_effects": {
            "provider_called": False,
            "network_called": False,
            "repo_files_modified": False,
            "evidence_written": False,
        },
    }
    clean = sanitize_for_logging(payload)
    policy_hash = stable_json_sha256(_policy_hash_payload(clean))
    if isinstance(clean, dict):
        clean["policy_hash"] = policy_hash
    return GovernanceTestPlanResult(task_id=task_id, task_type=task_type, policy_hash=policy_hash, payload=clean)


def run_governance_tests(
    project_root: Path,
    task_id: str,
    *,
    runner: TestRunner | None = None,
) -> GovernanceTestRunResult:
    root = Path(project_root).resolve()
    active_runner = runner or _run_command
    plan = plan_governance_tests(root, task_id, runner=active_runner)
    run_id = governance_run_id("test-run", task_id)
    evidence_dir = governance_evidence_dir(root, "tests", run_id)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    (evidence_dir / "test-plan.json").write_text(
        json.dumps(plan.payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    results: list[dict[str, object]] = []
    for index, test in enumerate(plan.payload.get("tests", []), start=1):
        if not isinstance(test, dict):
            continue
        command = test.get("command")
        if not isinstance(command, list) or not all(isinstance(item, str) for item in command):
            continue
        completed = active_runner(command, root)
        log_prefix = f"{index:02d}-{_safe_name(str(test.get('name') or 'test'))}"
        stdout_path = evidence_dir / f"{log_prefix}.stdout.log"
        stderr_path = evidence_dir / f"{log_prefix}.stderr.log"
        stdout = sanitize_for_logging(completed.stdout or "")
        stderr = sanitize_for_logging(completed.stderr or "")
        stdout_path.write_text(str(stdout), encoding="utf-8")
        stderr_path.write_text(str(stderr), encoding="utf-8")
        results.append(
            {
                "category": test.get("category"),
                "name": test.get("name"),
                "command": command,
                "required": bool(test.get("required", True)),
                "status": "pass" if completed.returncode == 0 else "fail",
                "returncode": completed.returncode,
                "stdout_log": _rel(root, stdout_path),
                "stderr_log": _rel(root, stderr_path),
                "stdout_tail": _tail(str(stdout)),
                "stderr_tail": _tail(str(stderr)),
            }
        )
    status = "pass" if results and all(item["status"] == "pass" for item in results) else "fail"
    payload = {
        "schema_version": "harness.governance_test_run/v1",
        "generated_at": _now(),
        "task_id": task_id,
        "run_id": run_id,
        "status": status,
        "plan": plan.payload,
        "results": results,
        "evidence_dir": _rel(root, evidence_dir),
        "links": {
            "base_sha": plan.payload.get("base_sha"),
            "branch": plan.payload.get("branch"),
            "policy_hash": plan.policy_hash,
            "gate_ids": plan.payload.get("gate_ids", []),
        },
        "side_effects": {
            "provider_called": False,
            "network_called": False,
            "repo_files_modified": False,
            "evidence_written": True,
            "task_metadata_updated": True,
        },
    }
    clean = sanitize_for_logging(payload)
    path = evidence_dir / "test-run.json"
    path.write_text(json.dumps(clean, indent=2, sort_keys=True), encoding="utf-8")
    update_governance_task_test_run_path(root, task_id, _rel(root, path))
    return GovernanceTestRunResult(
        ok=status == "pass",
        task_id=task_id,
        run_id=run_id,
        status=status,
        path=str(path),
        policy_hash=plan.policy_hash,
        payload=clean if isinstance(clean, dict) else {"value": clean},
    )


def _task_type(goal: str, allowed_paths: list[str], changed_paths: list[str]) -> str:
    haystack = " ".join([goal, *allowed_paths, *changed_paths]).lower()
    if any(token in haystack for token in ("governance", "security", "policy", "approval", "protected")):
        return "governance_security"
    if any(token in haystack for token in ("cli", "command", "typer", "src/harness/cli")):
        return "cli"
    if any(token in haystack for token in ("session_tools", "session tool", "permission", "src/harness/session_tools")):
        return "session_tool_permission"
    if any(token in haystack for token in ("adapter", "runtime", "core_service", "daemon", "provider")):
        return "adapter_runtime"
    if changed_paths and all(path.startswith("docs/") for path in changed_paths):
        return "docs_only"
    return "general"


def _tests_for(task_type: str, changed_paths: list[str]) -> list[dict[str, object]]:
    tests = [
        _test("unit", "governance-core", ["python3", "-m", "pytest", "tests/test_governance_gate_registry.py", "tests/test_governance_protected_paths.py", "-q"]),
    ]
    if task_type == "governance_security":
        tests.extend(
            [
                _test("security_gate", "governance-records", ["python3", "-m", "pytest", "tests/test_governance_tasks.py", "tests/test_governance_context.py", "tests/test_governance_cli.py", "-q"]),
                _test("security_gate", "governance-data-inventory", ["python3", "-m", "pytest", "tests/test_governance_data_inventory.py", "-q"]),
                _test("security_gate", "governance-network", ["python3", "-m", "pytest", "tests/test_governance_network.py", "-q"]),
                _test("security_gate", "governance-applyback", ["python3", "-m", "pytest", "tests/test_governance_applyback_integration.py", "-q"]),
                _test("security_gate", "path-security", ["python3", "-m", "pytest", "tests/test_paths_security.py", "-q"]),
                _test("integrity", "integrity-check", ["python3", "-m", "harness.cli.main", "integrity", "check", "--project", ".", "--output", "json"]),
                _test("security", "security-check", ["python3", "-m", "harness.cli.main", "security", "check", "--project", ".", "--output", "json"]),
            ]
        )
    elif task_type == "cli":
        tests.extend(
            [
                _test("cli_contract", "governance-cli", ["python3", "-m", "pytest", "tests/test_governance_cli.py", "-q"]),
                _test("cli_contract", "cli-smoke", ["python3", "-m", "pytest", "tests/test_cli_smoke.py", "-q"]),
            ]
        )
    elif task_type == "session_tool_permission":
        tests.extend(
            [
                _test("permission", "session-tools", ["python3", "-m", "pytest", "tests/test_session_tools.py", "tests/test_session_tool_catalog_contract.py", "-q"]),
                _test("permission", "local-server", ["python3", "-m", "pytest", "tests/test_local_server.py", "-q"]),
            ]
        )
    elif task_type == "adapter_runtime":
        tests.extend(
            [
                _test("runtime", "core-runtime", ["python3", "-m", "pytest", "tests/test_core_service.py", "tests/test_event_broker.py", "tests/test_session_runtime.py", "-q"]),
                _test("runtime", "adapters", ["python3", "-m", "pytest", "tests/test_process_supervisor.py", "tests/test_provider_adapters.py", "-q"]),
            ]
        )
    elif task_type == "docs_only":
        tests.append(_test("docs", "docs-contract", ["python3", "-m", "pytest", "tests/test_docs_phase_3d.py", "-q"]))
    else:
        tests.append(_test("general", "governance-cli", ["python3", "-m", "pytest", "tests/test_governance_cli.py", "-q"]))
    if any(path.startswith("src/harness/context") for path in changed_paths):
        tests.append(_test("context", "context-pack", ["python3", "-m", "pytest", "tests/test_context_pack.py", "tests/test_context_cli.py", "-q"]))
    return tests


def _test(category: str, name: str, command: list[str]) -> dict[str, object]:
    return {"category": category, "name": name, "command": command, "required": True}


def _gate_ids_for(task_type: str) -> list[str]:
    common = ["input_task_scope_declared", "test_evidence_fresh"]
    if task_type == "governance_security":
        return [
            *common,
            "applyback_bound_to_segment",
            "segment_context_pack_present",
            "allowed_paths_respected",
            "promotion_not_quarantined",
            "promotion_tests_current",
            "promotion_segment_bound",
            "promotion_network_policy_valid",
            "no_protected_writes",
            "no_secret_in_diff",
            "no_provider_permission_widening",
            "no_unsafe_sandbox_network_change",
        ]
    if task_type == "cli":
        return [*common, "no_protected_writes", "diff_size_bounded"]
    if task_type == "session_tool_permission":
        return [*common, "no_protected_writes", "allowed_paths_respected"]
    if task_type == "adapter_runtime":
        return [*common, "sandbox_capabilities_declared", "no_unsafe_sandbox_network_change"]
    return [gate for gate in common if gate in GATES_BY_ID]


def _changed_paths(root: Path, base: str, branch: str, runner: TestRunner) -> list[str]:
    result = runner(["git", "diff", "--name-only", f"{base}...{branch}"], root)
    if result.returncode != 0:
        return []
    return [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]


def _policy_hash_payload(payload: object) -> object:
    if isinstance(payload, dict):
        return {
            key: _policy_hash_payload(value)
            for key, value in payload.items()
            if key not in {"generated_at", "policy_hash"}
        }
    if isinstance(payload, list):
        return [_policy_hash_payload(value) for value in payload]
    return payload


def _tail(text: str, *, max_chars: int = LOG_TAIL_CHARS) -> str:
    return text if len(text) <= max_chars else text[-max_chars:]


def _rel(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _safe_name(value: str) -> str:
    return "".join(char if char.isalnum() or char in {"-", "_"} else "-" for char in value).strip("-")[:80] or "test"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _run_command(command: list[str], root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=root, text=True, capture_output=True, timeout=900)
