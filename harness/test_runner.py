from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from harness.config import HarnessConfig
from harness.memory.sqlite_store import SQLiteStore
from harness.sandbox import (
    CommandValidationError,
    DockerImageMissingError,
    DockerRunResult,
    DockerSandboxConfig,
    DockerSandboxRunner,
    DockerUnavailableError,
    validate_test_command,
)
from harness.security import sanitize_for_logging


@dataclass
class TestRunDecision:
    decision: str
    reason: str | None = None

    @property
    def approved(self) -> bool:
        return self.decision == "approved"


class TestExecutionApprovalProvider(Protocol):
    def decide(self, details: str) -> TestRunDecision:
        ...


class DockerTestRunner:
    def __init__(
        self,
        project_root: Path,
        config: HarnessConfig,
        store: SQLiteStore,
        approval_provider: TestExecutionApprovalProvider,
        docker_runner: DockerSandboxRunner | None = None,
    ) -> None:
        self.project_root = project_root.resolve()
        self.config = config
        self.store = store
        self.approval_provider = approval_provider
        self.sandbox_config = DockerSandboxConfig.model_validate(config.sandbox.model_dump())
        self.docker_runner = docker_runner or DockerSandboxRunner(self.sandbox_config)

    def run(self, command: list[str]) -> dict[str, object]:
        validate_test_command(command)
        run = self.store.create_run(
            goal=" ".join(command),
            task_type="docker_run_tests",
            status="running",
        )
        paths = self.store.initialize_run_artifacts(run.id)
        for kind, path in paths.items():
            self.store.register_artifact(run.id, kind=kind, path=path)
        run_dir = self.store.runs_dir / run.id
        stdout_path = run_dir / "test_stdout.txt"
        stderr_path = run_dir / "test_stderr.txt"
        result_path = run_dir / "test_result.json"
        for kind, path in {
            "test_stdout": stdout_path,
            "test_stderr": stderr_path,
            "test_result": result_path,
        }.items():
            path.touch(exist_ok=True)
            self.store.register_artifact(run.id, kind=kind, path=path)
        workspace = None
        approval = TestRunDecision(decision="not_requested")
        docker_result: DockerRunResult | None = None
        cleanup_status = "not_created"
        status = "sandbox_error"
        error = ""
        try:
            self.docker_runner.preflight()
            workspace = self.docker_runner.create_workspace(self.project_root)
            details = self._approval_details(command, workspace.path)
            approval = self.approval_provider.decide(details)
            self.store.append_event(
                run.id,
                "info",
                "test_execution_decision",
                "Persisted per-run test execution decision.",
                {"decision": approval.decision, "reason": approval.reason},
            )
            if not approval.approved:
                status = "execution_denied"
                return self._finish(
                    run.id,
                    status,
                    command,
                    approval,
                    docker_result,
                    stdout_path,
                    stderr_path,
                    result_path,
                    cleanup_status,
                    error,
                    workspace,
                )
            docker_result = self.docker_runner.run(workspace, command)
            stdout_path.write_text(str(sanitize_for_logging(docker_result.stdout)), encoding="utf-8")
            stderr_path.write_text(str(sanitize_for_logging(docker_result.stderr)), encoding="utf-8")
            if docker_result.timed_out:
                status = "tests_timed_out"
            elif docker_result.exit_code == 0:
                status = "tests_passed"
            else:
                status = "tests_failed"
        except DockerUnavailableError as exc:
            status = "docker_unavailable"
            error = str(exc)
        except DockerImageMissingError as exc:
            status = "docker_image_missing"
            error = str(exc)
        except (CommandValidationError, OSError, RuntimeError) as exc:
            status = "sandbox_error"
            error = str(sanitize_for_logging(str(exc)))
        return self._finish(
            run.id,
            status,
            command,
            approval,
            docker_result,
            stdout_path,
            stderr_path,
            result_path,
            cleanup_status,
            error,
            workspace,
        )

    def _finish(
        self,
        run_id: str,
        status: str,
        command: list[str],
        approval: TestRunDecision,
        docker_result: DockerRunResult | None,
        stdout_path: Path,
        stderr_path: Path,
        result_path: Path,
        cleanup_status: str,
        error: str,
        workspace,
    ) -> dict[str, object]:
        temp_workspace = str(workspace.path) if workspace is not None else ""
        if workspace is not None and workspace.cleanup_status == "not_cleaned":
            workspace.cleanup()
            cleanup_status = workspace.cleanup_status
        result_payload = {
            "status": status,
            "command": command,
            "image": self.sandbox_config.image,
            "network": self.sandbox_config.network,
            "timeout_seconds": self.sandbox_config.timeout_seconds,
            "memory_limit": self.sandbox_config.memory_limit,
            "cpu_limit": self.sandbox_config.cpu_limit,
            "workdir": self.sandbox_config.workdir,
            "install_project": self.sandbox_config.install_project,
            "temp_workspace": temp_workspace,
            "cleanup_status": cleanup_status,
            "exit_code": docker_result.exit_code if docker_result else None,
            "duration_seconds": docker_result.duration_seconds if docker_result else 0.0,
            "timed_out": docker_result.timed_out if docker_result else False,
            "approval_decision": approval.decision,
            "approval_reason": approval.reason,
            "error": error,
            "stdout_summary": _summarize_text(docker_result.stdout if docker_result else ""),
            "stderr_summary": _summarize_text(docker_result.stderr if docker_result else error),
            "failure_hint": _failure_hint(status, docker_result.stdout if docker_result else "", docker_result.stderr if docker_result else error),
            "stdout_artifact": str(stdout_path),
            "stderr_artifact": str(stderr_path),
        }
        result_path.write_text(json.dumps(sanitize_for_logging(result_payload), indent=2, sort_keys=True), encoding="utf-8")
        self.store.append_event(
            run_id,
            "info" if status in {"tests_passed", "tests_failed", "execution_denied"} else "warning",
            "docker_test_run_completed",
            "Docker test run completed.",
            result_payload,
        )
        self.store.update_run_status(run_id, status)
        self._write_report(
            run_id=run_id,
            status=status,
            payload=result_payload,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            result_path=result_path,
        )
        return {
            "run_id": run_id,
            "status": status,
            "approval_decision": approval.decision,
            "artifacts": {
                "test_stdout": str(stdout_path),
                "test_stderr": str(stderr_path),
                "test_result": str(result_path),
                "final_report": str(self.store.runs_dir / run_id / "final_report.md"),
            },
            **result_payload,
        }

    def _approval_details(self, command: list[str], temp_workspace: Path) -> str:
        return "\n".join(
            [
                "Test execution approval required:",
                f"Command: {' '.join(command)}",
                f"Docker image: {self.sandbox_config.image}",
                f"Working directory: {self.sandbox_config.workdir}",
                f"Network: {'enabled' if self.sandbox_config.network else 'disabled'}",
                f"Timeout: {self.sandbox_config.timeout_seconds} seconds",
                "Mounts:",
                f"- {temp_workspace} -> {self.sandbox_config.workdir}",
                "Source:",
                f"- {self.project_root} -> sanitized temporary workspace",
            ]
        )

    def _write_report(self, run_id: str, status: str, payload: dict[str, object], stdout_path: Path, stderr_path: Path, result_path: Path) -> None:
        report_path = self.store.runs_dir / run_id / "final_report.md"
        outcome = {
            "docker_unavailable": "Docker unavailable.",
            "docker_image_missing": "Docker image missing.",
            "execution_denied": "Test execution was denied.",
            "sandbox_error": "Sandbox execution error.",
            "tests_failed": "Tests exited nonzero.",
            "tests_timed_out": "Tests timed out.",
            "tests_passed": "Tests passed.",
        }.get(status, status)
        lines = [
            f"# Run {run_id}",
            "",
            f"- Status: {status}",
            f"- Outcome: {outcome}",
            f"- Command: {' '.join(payload['command'])}",
            f"- Docker image: {payload['image']}",
            f"- Network: {payload['network']}",
            f"- Timeout seconds: {payload['timeout_seconds']}",
            f"- Memory limit: {payload['memory_limit']}",
            f"- CPU limit: {payload['cpu_limit']}",
            f"- Working directory: {payload['workdir']}",
            f"- Install project: {payload['install_project']}",
            f"- Temp workspace cleanup status: {payload['cleanup_status']}",
            f"- Exit code: {payload['exit_code']}",
            f"- Duration seconds: {payload['duration_seconds']}",
            f"- Timed out: {payload['timed_out']}",
            f"- Approval decision: {payload['approval_decision']}",
            f"- Failure hint: {payload['failure_hint']}",
            f"- Error: {sanitize_for_logging(str(payload['error']))}",
            "",
            "## Output Summary",
            "",
            "### stdout",
            "```",
            str(sanitize_for_logging(payload["stdout_summary"])),
            "```",
            "### stderr",
            "```",
            str(sanitize_for_logging(payload["stderr_summary"])),
            "```",
            "",
            "## Artifacts",
            "",
            f"- test_stdout: {stdout_path}",
            f"- test_stderr: {stderr_path}",
            f"- test_result: {result_path}",
            f"- final_report: {report_path}",
        ]
        report_path.write_text("\n".join(lines), encoding="utf-8")


def _summarize_text(text: str, limit: int = 1200) -> str:
    sanitized = str(sanitize_for_logging(text or "")).strip()
    if len(sanitized) <= limit:
        return sanitized
    head = sanitized[: limit // 2].rstrip()
    tail = sanitized[-limit // 2 :].lstrip()
    return f"{head}\n...[truncated]...\n{tail}"


def _failure_hint(status: str, stdout: str, stderr: str) -> str:
    if status != "tests_failed":
        return ""
    combined = f"{stdout}\n{stderr}"
    lowered = combined.lower()
    if "harness_install_failed" in lowered:
        return "install_failed"
    if "no module named pytest" in lowered:
        return "pytest_missing"
    if "no module named 'harness'" in lowered or 'no module named "harness"' in lowered:
        return "package_import_failed"
    if "modulenotfounderror" in lowered or "importerror" in lowered:
        return "dependency_missing"
    if "== failures ==" in lowered or "short test summary info" in lowered or " failed" in lowered:
        return "pytest_failures"
    return "unknown_test_failure"
