from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from harness.config import HarnessConfig
from harness.memory.sqlite_store import SQLiteStore
from harness.models import RedactionState, RunEventType
from harness.paths import PathSecurityError, relative_to_project, resolve_under_project
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
class RunTestsDecision:
    decision: str
    reason: str | None = None

    @property
    def approved(self) -> bool:
        return self.decision == "approved"


class TestExecutionApprovalProvider(Protocol):
    def decide(self, details: str) -> RunTestsDecision:
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

    def run(self, command: list[str], cwd: str | None = None) -> dict[str, object]:
        validate_test_command(command)
        container_workdir = self._container_workdir(cwd)
        run = self.store.create_run(
            goal=" ".join(command),
            task_type="docker_run_tests",
            status="running",
        )
        paths = self.store.initialize_run_artifacts(run.id)
        for kind, path in paths.items():
            self.store.register_artifact(run.id, kind=kind, path=path)
        return self._execute_in_run(
            run_id=run.id,
            command=command,
            cwd=cwd,
            container_workdir=container_workdir,
            artifact_index=1,
            approval_provider=self.approval_provider,
            update_run_status=True,
            write_final_report=True,
        )

    def run_in_existing_run(
        self,
        run_id: str,
        command: list[str],
        cwd: str | None = None,
        artifact_index: int = 1,
        approval_provider: TestExecutionApprovalProvider | None = None,
    ) -> dict[str, object]:
        validate_test_command(command)
        container_workdir = self._container_workdir(cwd)
        return self._execute_in_run(
            run_id=run_id,
            command=command,
            cwd=cwd,
            container_workdir=container_workdir,
            artifact_index=artifact_index,
            approval_provider=approval_provider or self.approval_provider,
            update_run_status=False,
            write_final_report=False,
        )

    def _execute_in_run(
        self,
        run_id: str,
        command: list[str],
        cwd: str | None,
        container_workdir: str,
        artifact_index: int,
        approval_provider: TestExecutionApprovalProvider,
        update_run_status: bool,
        write_final_report: bool,
    ) -> dict[str, object]:
        run_dir = self.store.runs_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        suffix = "" if artifact_index == 1 else f"_{artifact_index}"
        stdout_path = run_dir / f"test_stdout{suffix}.txt"
        stderr_path = run_dir / f"test_stderr{suffix}.txt"
        result_path = run_dir / f"test_result{suffix}.json"
        for kind, path in {
            f"test_stdout{suffix}": stdout_path,
            f"test_stderr{suffix}": stderr_path,
            f"test_result{suffix}": result_path,
        }.items():
            path.touch(exist_ok=True)
            self.store.register_artifact(run_id, kind=kind, path=path)
        workspace = None
        approval = RunTestsDecision(decision="not_requested")
        docker_result: DockerRunResult | None = None
        cleanup_status = "not_created"
        status = "sandbox_error"
        error = ""
        try:
            self.docker_runner.preflight()
            workspace = self.docker_runner.create_workspace(self.project_root)
            details = self._approval_details(command, workspace.path, container_workdir)
            self.store.append_run_event(
                run_id,
                RunEventType.APPROVAL_REQUIRED,
                {
                    "approval_kind": "docker_execution",
                    "reason": "Docker test execution requires explicit approval.",
                    "command": command,
                    "cwd": cwd,
                    "image": self.sandbox_config.image,
                    "network": self.sandbox_config.network,
                    "workdir": container_workdir,
                },
                message="Docker execution approval required.",
                redaction_state=RedactionState.REDACTED,
            )
            if update_run_status:
                self.store.update_run_status(run_id, "waiting_approval")
            approval = approval_provider.decide(details)
            self.store.append_event(
                run_id,
                "info",
                "test_execution_decision",
                "Persisted per-run test execution decision.",
                {"decision": approval.decision, "reason": approval.reason, "command": command, "cwd": cwd},
            )
            if not approval.approved:
                status = "execution_denied"
                return self._finish(
                    run_id,
                    status,
                    command,
                    cwd,
                    container_workdir,
                    approval,
                    docker_result,
                    stdout_path,
                    stderr_path,
                    result_path,
                    cleanup_status,
                    error,
                    workspace,
                    update_run_status,
                    write_final_report,
                )
            self.store.append_run_event(
                run_id,
                RunEventType.TEST_STARTED,
                {"command": command, "cwd": cwd, "workdir": container_workdir},
                message="Started Docker test execution.",
                redaction_state=RedactionState.REDACTED,
            )
            docker_result = self.docker_runner.run(workspace, command, workdir=container_workdir)
            stdout_path.write_text(str(sanitize_for_logging(docker_result.stdout)), encoding="utf-8")
            stderr_path.write_text(str(sanitize_for_logging(docker_result.stderr)), encoding="utf-8")
            if docker_result.stdout:
                self.store.append_run_event(
                    run_id,
                    RunEventType.TEST_OUTPUT,
                    {"stream": "stdout", "text": sanitize_for_logging(docker_result.stdout)},
                    message="Captured Docker test stdout.",
                    redaction_state=RedactionState.REDACTED,
                )
            if docker_result.stderr:
                self.store.append_run_event(
                    run_id,
                    RunEventType.TEST_OUTPUT,
                    {"stream": "stderr", "text": sanitize_for_logging(docker_result.stderr)},
                    message="Captured Docker test stderr.",
                    redaction_state=RedactionState.REDACTED,
                    level="warning",
                )
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
            run_id,
            status,
            command,
            cwd,
            container_workdir,
            approval,
            docker_result,
            stdout_path,
            stderr_path,
            result_path,
            cleanup_status,
            error,
            workspace,
            update_run_status,
            write_final_report,
        )

    def _finish(
        self,
        run_id: str,
        status: str,
        command: list[str],
        cwd: str | None,
        container_workdir: str,
        approval: RunTestsDecision,
        docker_result: DockerRunResult | None,
        stdout_path: Path,
        stderr_path: Path,
        result_path: Path,
        cleanup_status: str,
        error: str,
        workspace,
        update_run_status: bool,
        write_final_report: bool,
    ) -> dict[str, object]:
        temp_workspace = str(workspace.path) if workspace is not None else ""
        if workspace is not None and workspace.cleanup_status == "not_cleaned":
            workspace.cleanup()
            cleanup_status = workspace.cleanup_status
        stdout_summary, stderr_summary = summarize_test_output_for_model(
            docker_result.stdout if docker_result else "",
            docker_result.stderr if docker_result else error,
        )
        failure_hint = _failure_hint(status, docker_result.stdout if docker_result else "", docker_result.stderr if docker_result else error)
        result_payload = {
            "status": status,
            "command": command,
            "image": self.sandbox_config.image,
            "network": self.sandbox_config.network,
            "timeout_seconds": self.sandbox_config.timeout_seconds,
            "memory_limit": self.sandbox_config.memory_limit,
            "cpu_limit": self.sandbox_config.cpu_limit,
            "workdir": container_workdir,
            "cwd": cwd,
            "install_project": self.sandbox_config.install_project,
            "temp_workspace": temp_workspace,
            "cleanup_status": cleanup_status,
            "exit_code": docker_result.exit_code if docker_result else None,
            "duration_seconds": docker_result.duration_seconds if docker_result else 0.0,
            "timed_out": docker_result.timed_out if docker_result else False,
            "approval_decision": approval.decision,
            "approval_reason": approval.reason,
            "error": error,
            "stdout_summary": stdout_summary,
            "stderr_summary": stderr_summary,
            "failure_hint": failure_hint,
            "failure_guidance": _failure_guidance(failure_hint, self.sandbox_config.image),
            "stdout_artifact": str(stdout_path),
            "stderr_artifact": str(stderr_path),
            "result_artifact": str(result_path),
        }
        result_path.write_text(json.dumps(sanitize_for_logging(result_payload), indent=2, sort_keys=True), encoding="utf-8")
        self.store.append_event(
            run_id,
            "info" if status in {"tests_passed", "tests_failed", "execution_denied"} else "warning",
            "docker_test_run_completed",
            "Docker test run completed.",
            result_payload,
        )
        self.store.append_run_event(
            run_id,
            RunEventType.TEST_FINISHED,
            {
                "status": status,
                "exit_code": result_payload["exit_code"],
                "duration_seconds": result_payload["duration_seconds"],
                "approval_decision": approval.decision,
            },
            message=f"Docker test execution finished with status {status}.",
            redaction_state=RedactionState.NOT_REQUIRED if status == "execution_denied" else RedactionState.REDACTED,
        )
        if update_run_status:
            self.store.update_run_status(run_id, status)
        if write_final_report:
            self._write_report(
                run_id=run_id,
                status=status,
                payload=result_payload,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                result_path=result_path,
            )
            self.store.append_run_event(
                run_id,
                RunEventType.RUN_SUMMARY_CREATED,
                {"path": str(self.store.runs_dir / run_id / "final_report.md")},
                message="Created Docker test run summary.",
                redaction_state=RedactionState.REDACTED,
            )
            self.store.append_run_event(
                run_id,
                RunEventType.RUN_FINISHED if status not in {"sandbox_error", "docker_unavailable", "docker_image_missing"} else RunEventType.RUN_FAILED,
                {"status": status},
                message=f"Run finished with status {status}.",
                redaction_state=RedactionState.NOT_REQUIRED,
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

    def _approval_details(self, command: list[str], temp_workspace: Path, container_workdir: str) -> str:
        return "\n".join(
            [
                "Test execution approval required:",
                f"Command: {' '.join(command)}",
                f"Docker image: {self.sandbox_config.image}",
                f"Working directory: {container_workdir}",
                f"Network: {'enabled' if self.sandbox_config.network else 'disabled'}",
                f"Timeout: {self.sandbox_config.timeout_seconds} seconds",
                "Mounts:",
                f"- {temp_workspace} -> {self.sandbox_config.workdir}",
                "Source:",
                f"- {self.project_root} -> sanitized temporary workspace",
            ]
        )

    def _container_workdir(self, cwd: str | None) -> str:
        if cwd is None:
            return self.sandbox_config.workdir
        if not isinstance(cwd, str) or not cwd.strip():
            raise CommandValidationError("Test cwd must be a non-empty project-relative string.")
        raw = Path(cwd)
        if raw.is_absolute():
            raise CommandValidationError("Test cwd must be project-relative.")
        try:
            resolved = resolve_under_project(self.project_root, raw)
        except PathSecurityError as exc:
            raise CommandValidationError(str(exc)) from exc
        if not resolved.exists():
            raise CommandValidationError(f"Test cwd does not exist: {cwd}")
        if not resolved.is_dir():
            raise CommandValidationError(f"Test cwd is not a directory: {cwd}")
        if resolved == self.project_root:
            return self.sandbox_config.workdir
        return f"{self.sandbox_config.workdir.rstrip('/')}/{relative_to_project(self.project_root, resolved)}"

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
            f"- Failure guidance: {payload['failure_guidance']}",
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


def summarize_test_output_for_model(stdout: str, stderr: str, limit: int = 1200) -> tuple[str, str]:
    return _summarize_text(stdout, limit=limit), _summarize_text(stderr, limit=limit)


def _summarize_text(text: str, limit: int = 1200) -> str:
    sanitized = str(sanitize_for_logging(text or "")).strip()
    if len(sanitized) <= limit:
        return sanitized
    focused = _focused_failure_summary(sanitized, limit=limit)
    if focused:
        return focused
    head = sanitized[: limit // 2].rstrip()
    tail = sanitized[-limit // 2 :].lstrip()
    return f"{head}\n...[truncated]...\n{tail}"


def _focused_failure_summary(text: str, limit: int) -> str:
    lines = text.splitlines()
    selected: set[int] = set()
    markers = (
        "failures",
        "errors",
        "short test summary info",
        "traceback (most recent call last)",
        "assert ",
        "assertionerror",
        "modulenotfounderror",
        "importerror",
        "no module named",
    )
    for index, line in enumerate(lines):
        stripped = line.strip()
        lowered = stripped.lower()
        if (
            any(marker in lowered for marker in markers)
            or stripped.startswith(("FAILED ", "ERROR ", "E   ", "> "))
            or "::" in stripped and ("test" in lowered or "failed" in lowered or "error" in lowered)
        ):
            for offset in range(-3, 5):
                candidate = index + offset
                if 0 <= candidate < len(lines):
                    selected.add(candidate)
    if not selected:
        return ""
    pieces: list[str] = []
    last_index: int | None = None
    for index in sorted(selected):
        if last_index is not None and index > last_index + 1:
            pieces.append("...[omitted]...")
        pieces.append(lines[index])
        last_index = index
    summary = "\n".join(pieces).strip()
    if len(summary) <= limit:
        return summary
    head = summary[: limit // 2].rstrip()
    tail = summary[-limit // 2 :].lstrip()
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


def _failure_guidance(failure_hint: str, image: str) -> str:
    guidance = {
        "install_failed": (
            "Editable install failed inside the container. Ensure the configured image has build tooling and set "
            "sandbox.install_project_no_build_isolation: true when dependencies are already installed in the image."
        ),
        "pytest_missing": (
            f"The configured image `{image}` does not include pytest. Build a managed test image with "
            "`harness tests image build --project .` or configure sandbox.image to an image that contains pytest."
        ),
        "package_import_failed": (
            "The local package could not be imported. Set sandbox.install_project: true or run tests with a cwd/command "
            "that exposes the package in the sanitized workspace."
        ),
        "dependency_missing": (
            f"A Python dependency is missing from `{image}`. Add the dependency to the configured test image and rebuild "
            "with `harness tests image build --project .`; Phase 4 does not install dependencies during test execution."
        ),
        "pytest_failures": "Pytest ran and reported test failures. Inspect the summarized failure output and full artifacts.",
        "unknown_test_failure": "The test command exited nonzero. Inspect stdout/stderr artifacts for details.",
    }
    return guidance.get(failure_hint, "")
