from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from harness.backends.local_openai import LocalEndpointUnavailable, LocalOpenAICompatibleBackend
from harness.config import HarnessConfig
from harness.events import append_jsonl
from harness.memory.sqlite_store import SQLiteStore
from harness.protocol import CommandValidationError, ModelCommand, parse_model_command
from harness.security import sanitize_for_logging
from harness.tools.base import ToolContext, ToolResult
from harness.paths import PathSecurityError
from harness.security import SecretBlockedError
from harness.test_runner import DockerTestRunner, TestExecutionApprovalProvider
from harness.tools.patch import ApplyPatchTool, PatchValidationError
from harness.tools.readonly import GitDiffTool, GitStatusTool, ListFilesTool, ReadFileTool


class PatchApprovalProvider(Protocol):
    def decide(self, patch: str, summary: str) -> "PatchApprovalDecision":
        ...


@dataclass
class PatchApprovalDecision:
    decision: str
    reason: str | None = None

    @property
    def approved(self) -> bool:
        return self.decision == "approved"


class NativeEditRunner:
    def __init__(
        self,
        project_root: Path,
        config: HarnessConfig,
        store: SQLiteStore,
        backend: LocalOpenAICompatibleBackend,
        approval_provider: PatchApprovalProvider,
        test_approval_provider: TestExecutionApprovalProvider | None = None,
        docker_test_runner_factory: Callable[
            [Path, HarnessConfig, SQLiteStore, TestExecutionApprovalProvider],
            DockerTestRunner,
        ]
        | None = None,
        max_steps: int = 16,
        max_invalid_retries: int = 2,
    ) -> None:
        self.project_root = project_root.resolve()
        self.config = config
        self.store = store
        self.backend = backend
        self.approval_provider = approval_provider
        self.test_approval_provider = test_approval_provider
        self.docker_test_runner_factory = docker_test_runner_factory or DockerTestRunner
        self.max_steps = max_steps
        self.max_invalid_retries = max_invalid_retries
        self.tools = {
            "list_files": ListFilesTool(),
            "read_file": ReadFileTool(),
            "git_status": GitStatusTool(),
            "git_diff": GitDiffTool(),
        }
        self.patch_tool = ApplyPatchTool()

    def run(self, goal: str, task_type: str) -> dict[str, Any]:
        backend_status = self.backend.preflight()
        if not backend_status.available:
            raise LocalEndpointUnavailable(backend_status.reason or "Local backend unavailable.")
        run = self.store.create_run(goal=goal, task_type=task_type, status="running", backend=self.backend.config)
        paths = self.store.initialize_run_artifacts(run.id)
        for kind, path in paths.items():
            self.store.register_artifact(run.id, kind=kind, path=path)
        transcript_path = paths["transcript"]
        pre_status = self._git_status_porcelain()
        self.store.append_event(run.id, "info", "pre_git_status", "Recorded pre-run git status.", {"status": pre_status})
        messages = self._initial_messages(goal, task_type)
        tool_results: list[dict[str, Any]] = []
        tools_executed: list[str] = []
        patch_decisions: list[dict[str, Any]] = []
        test_runs: list[dict[str, Any]] = []
        applied_patch_files: list[str] = []
        denied_or_blocked = 0
        invalid_count = 0
        final_answer = ""
        failed = False
        for step in range(self.max_steps):
            raw = self.backend.complete(messages)
            append_jsonl(transcript_path, sanitize_for_logging({"role": "assistant", "content": raw, "step": step}))
            try:
                command = parse_model_command(
                    raw,
                    allow_apply_patch=True,
                    allow_run_tests=True,
                    project_root=self.project_root,
                )
            except CommandValidationError as exc:
                invalid_count += 1
                self.store.append_event(
                    run.id,
                    "warning",
                    "invalid_model_command",
                    "Rejected invalid model command.",
                    {"error": str(exc), "raw": raw, "invalid_count": invalid_count},
                )
                if invalid_count > self.max_invalid_retries:
                    final_answer = "Run stopped because the local model returned too many invalid commands."
                    failed = True
                    break
                messages.append({"role": "user", "content": "Invalid command. Return only valid JSON."})
                continue
            if command.command == "final_answer":
                final_answer = str(sanitize_for_logging(command.final_text))
                self.store.append_event(run.id, "info", "final_answer", "Model returned final answer.", {"summary": final_answer})
                break
            if command.command == "apply_patch":
                result, decision_payload = self._handle_patch(run.id, command)
                patch_decisions.append(decision_payload)
                if result.ok:
                    applied_patch_files.extend(str(path) for path in result.data.get("files", []))
                if not result.ok:
                    denied_or_blocked += 1
            elif command.command == "run_tests":
                result, test_record = self._handle_run_tests(run.id, command, len(test_runs) + 1)
                test_runs.append(test_record)
            else:
                result = self._execute_read_tool(command)
            tools_executed.append(command.command)
            tool_payload = {
                "command": command.command,
                "arguments": command.arguments if command.command != "apply_patch" else {"patch": "[PATCH_REDACTED_IN_EVENT]"},
                "ok": result.ok,
                "output": result.output,
                "error_type": result.error_type,
                "data": result.data,
            }
            tool_results.append(tool_payload)
            self.store.append_event(run.id, "info", "tool_executed", "Executed native tool.", tool_payload)
            append_jsonl(transcript_path, sanitize_for_logging({"role": "tool", **tool_payload}))
            messages.append({"role": "user", "content": self._tool_result_message(tool_payload, tool_results)})
        if not final_answer:
            final_answer = "Run ended without a final model answer."
        post_status = self._git_status_porcelain()
        diff_stat = self._git_diff_stat()
        changed_files = self._git_changed_files(pre_status, post_status, applied_patch_files)
        self.store.append_event(run.id, "info", "post_git_status", "Recorded post-run git status.", {"status": post_status})
        self.store.append_event(run.id, "info", "final_git_diff_stat", "Recorded final git diff stat.", {"diff_stat": diff_stat})
        self._write_report(
            run_id=run.id,
            goal=goal,
            task_type=task_type,
            tools_executed=tools_executed,
            patch_decisions=patch_decisions,
            test_runs=test_runs,
            changed_files=changed_files,
            diff_stat=diff_stat,
            denied_or_blocked=denied_or_blocked,
            final_answer=final_answer,
            artifact_paths=paths,
            pre_status=pre_status,
            post_status=post_status,
        )
        self.store.update_run_status(run.id, "failed" if failed else "completed")
        return {
            "run_id": run.id,
            "final_answer": final_answer,
            "tools_executed": tools_executed,
            "patch_decisions": patch_decisions,
            "test_runs": test_runs,
            "changed_files": changed_files,
            "artifacts": {key: str(path) for key, path in paths.items()},
        }

    def _handle_patch(self, run_id: str, command: ModelCommand) -> tuple[ToolResult, dict[str, Any]]:
        patch = command.arguments["patch"]
        context = ToolContext(project_root=self.project_root, context_excludes=self.config.context_excludes)
        try:
            summary = self.patch_tool.validate(patch, context).render()
        except (PatchValidationError, PathSecurityError, SecretBlockedError) as exc:
            decision = {"decision": "blocked", "reason": str(exc), "summary": ""}
            self.store.append_event(run_id, "warning", "patch_blocked", "Patch blocked before approval.", decision)
            return ToolResult(name="apply_patch", ok=False, output=str(exc), error_type="patch_validation"), decision
        decision = self.approval_provider.decide(patch, summary)
        decision_payload = {"decision": decision.decision, "reason": decision.reason, "summary": summary}
        if not decision.approved:
            self.store.append_event(run_id, "info", "patch_denied", "Patch denied by user.", decision_payload)
            return ToolResult(name="apply_patch", ok=False, output="Patch denied by user.", error_type="patch_denied"), decision_payload
        result = self.patch_tool.run({"patch": patch}, context)
        self.store.append_event(
            run_id,
            "info" if result.ok else "warning",
            "patch_approved" if result.ok else "patch_apply_failed",
            "Patch approval decision processed.",
            {**decision_payload, "ok": result.ok, "error_type": result.error_type},
        )
        return result, decision_payload

    def _handle_run_tests(self, run_id: str, command: ModelCommand, artifact_index: int) -> tuple[ToolResult, dict[str, Any]]:
        if self.test_approval_provider is None:
            record: dict[str, Any] = {
                "status": "execution_denied",
                "command": command.arguments["command"],
                "cwd": command.arguments.get("cwd"),
                "approval_decision": "denied",
                "approval_reason": "No test execution approval provider configured.",
                "exit_code": None,
                "duration_seconds": 0.0,
                "timed_out": False,
                "failure_hint": "",
                "stdout_summary": "",
                "stderr_summary": "No test execution approval provider configured.",
                "stdout_artifact": "",
                "stderr_artifact": "",
                "result_artifact": "",
            }
            self.store.append_event(
                run_id,
                "warning",
                "run_tests_unavailable",
                "run_tests denied because no approval provider is configured.",
                record,
            )
            return _run_tests_tool_result(record), record
        runner = self.docker_test_runner_factory(
            self.project_root,
            self.config,
            self.store,
            self.test_approval_provider,
        )
        record = runner.run_in_existing_run(
            run_id=run_id,
            command=command.arguments["command"],
            cwd=command.arguments.get("cwd"),
            artifact_index=artifact_index,
            approval_provider=self.test_approval_provider,
        )
        return _run_tests_tool_result(record), record

    def _execute_read_tool(self, command: ModelCommand) -> ToolResult:
        tool = self.tools[command.command]
        context = ToolContext(project_root=self.project_root, context_excludes=self.config.context_excludes)
        return tool.run(command.arguments, context)

    def _initial_messages(self, goal: str, task_type: str) -> list[dict[str, str]]:
        allowed = ", ".join(["list_files", "read_file", "git_status", "git_diff", "apply_patch", "run_tests", "final_answer"])
        return [
            {
                "role": "system",
                "content": (
                    "You are running inside a local-only harness-native edit flow. Return exactly one JSON object "
                    f"per response. Allowed commands are: {allowed}. Patches must be unified diffs. "
                    "The harness will preview and ask for approval before applying any patch. "
                    "Do not request shell execution, network access, secrets, or paths outside the project. "
                    "Use run_tests only with a JSON command token list; tests run only in a Docker sandbox."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Goal: {goal}\nProject root: {self.project_root}\nTask type: {task_type}\n"
                    f"Allowed tools: {allowed}\nStart by inspecting files or git status."
                ),
            },
        ]

    def _tool_result_message(self, payload: dict[str, Any], recent_results: list[dict[str, Any]]) -> str:
        return f"Tool result follows. Continue with another JSON command or final_answer.\nRecent results: {recent_results[-4:]}"

    def _git_status_porcelain(self) -> str:
        result = subprocess.run(["git", "status", "--porcelain"], cwd=self.project_root, text=True, capture_output=True, timeout=30)
        return result.stdout if result.returncode == 0 else f"GIT_STATUS_UNAVAILABLE: {(result.stderr or result.stdout).strip()}"

    def _git_diff_stat(self) -> str:
        result = subprocess.run(["git", "diff", "--stat"], cwd=self.project_root, text=True, capture_output=True, timeout=30)
        return result.stdout if result.returncode == 0 else ""

    def _git_changed_files(self, pre_status: str, post_status: str, applied_patch_files: list[str]) -> list[str]:
        names: set[str] = set()
        names.update(path for path in applied_patch_files if self._is_reportable_changed_path(path))
        pre_map = self._parse_git_status_porcelain(pre_status)
        post_map = self._parse_git_status_porcelain(post_status)
        for path, status in post_map.items():
            if pre_map.get(path) != status and self._is_reportable_changed_path(path):
                names.add(path)
        diff = subprocess.run(["git", "diff", "--name-only"], cwd=self.project_root, text=True, capture_output=True, timeout=30)
        if diff.returncode == 0:
            names.update(line for line in diff.stdout.splitlines() if line.strip() and self._is_reportable_changed_path(line))
        status = subprocess.run(["git", "status", "--porcelain"], cwd=self.project_root, text=True, capture_output=True, timeout=30)
        if status.returncode == 0:
            for line in status.stdout.splitlines():
                if not line.strip():
                    continue
                path = line[3:].strip()
                if " -> " in path:
                    path = path.split(" -> ", 1)[1].strip()
                if path and pre_map.get(path) != line[:2] and self._is_reportable_changed_path(path):
                    names.add(path)
        return sorted(names)

    def _parse_git_status_porcelain(self, status: str) -> dict[str, str]:
        parsed: dict[str, str] = {}
        if status.startswith("GIT_STATUS_UNAVAILABLE:"):
            return parsed
        for line in status.splitlines():
            if not line.strip() or len(line) < 4:
                continue
            path = line[3:].strip()
            if " -> " in path:
                path = path.split(" -> ", 1)[1].strip()
            if path:
                parsed[path] = line[:2]
        return parsed

    def _is_reportable_changed_path(self, path: str) -> bool:
        blocked_prefixes = (
            ".harness/",
            ".git/",
            ".venv/",
            "node_modules/",
            "__pycache__/",
            ".pytest_cache/",
            ".mypy_cache/",
            "dist/",
            "build/",
        )
        return path not in {".harness", ".git"} and not path.startswith(blocked_prefixes)

    def _write_report(
        self,
        run_id: str,
        goal: str,
        task_type: str,
        tools_executed: list[str],
        patch_decisions: list[dict[str, Any]],
        test_runs: list[dict[str, Any]],
        changed_files: list[str],
        diff_stat: str,
        denied_or_blocked: int,
        final_answer: str,
        artifact_paths: dict[str, Path],
        pre_status: str,
        post_status: str,
    ) -> None:
        metadata = self.backend.config.metadata
        lines = [
            f"# Run {run_id}",
            "",
            f"- Goal: {sanitize_for_logging(goal)}",
            f"- Task type: {task_type}",
            f"- Backend name: {self.backend.name}",
            f"- Backend kind: {self.backend.config.kind.value}",
            f"- Billing mode: {metadata.billing_mode.value}",
            f"- Execution location: {metadata.execution_location.value}",
            f"- Data boundary: {metadata.data_boundary.value}",
            f"- Tools used: {tools_executed}",
            f"- Denied/blocked patch attempts: {denied_or_blocked}",
            f"- Changed files: {changed_files}",
            "",
            "## Patch Approval Decisions",
            "",
            str(sanitize_for_logging(patch_decisions)),
            "",
        ]
        if test_runs:
            lines.extend(["## Test Executions", ""])
            for index, test_run in enumerate(test_runs, start=1):
                lines.extend(
                    [
                        f"### Test Execution {index}",
                        "",
                        f"- Command: {' '.join(str(part) for part in test_run.get('command', []))}",
                        f"- Docker image: {test_run.get('image', '')}",
                        f"- Network: {test_run.get('network', '')}",
                        f"- Timeout seconds: {test_run.get('timeout_seconds', '')}",
                        f"- Memory limit: {test_run.get('memory_limit', '')}",
                        f"- CPU limit: {test_run.get('cpu_limit', '')}",
                        f"- Working directory: {test_run.get('workdir', '')}",
                        f"- Approval decision: {test_run.get('approval_decision', '')}",
                        f"- Status: {test_run.get('status', '')}",
                        f"- Exit code: {test_run.get('exit_code', '')}",
                        f"- Duration seconds: {test_run.get('duration_seconds', '')}",
                        f"- Timed out: {test_run.get('timed_out', '')}",
                        f"- Failure hint: {test_run.get('failure_hint', '')}",
                        f"- stdout: {test_run.get('stdout_artifact', '')}",
                        f"- stderr: {test_run.get('stderr_artifact', '')}",
                        f"- test_result: {test_run.get('result_artifact', '')}",
                        "",
                    ]
                )
        lines.extend(
            [
            "## Final Git Diff Summary",
            "",
            "```",
            str(sanitize_for_logging(diff_stat)),
            "```",
            "",
            "## Git Status",
            "",
            "### Pre-run",
            "```",
            str(sanitize_for_logging(pre_status)),
            "```",
            "### Post-run",
            "```",
            str(sanitize_for_logging(post_status)),
            "```",
            "",
            "## Final Model Answer",
            "",
            str(sanitize_for_logging(final_answer)),
            "",
            "## Artifacts",
            "",
            ]
        )
        lines.extend(f"- {kind}: {path}" for kind, path in artifact_paths.items())
        artifact_paths["final_report"].write_text("\n".join(lines), encoding="utf-8")


def _run_tests_tool_result(record: dict[str, Any]) -> ToolResult:
    artifacts = {
        "stdout": record.get("stdout_artifact", ""),
        "stderr": record.get("stderr_artifact", ""),
        "result": record.get("result_artifact", ""),
    }
    observation = {
        "tool": "run_tests",
        "status": record.get("status"),
        "exit_code": record.get("exit_code"),
        "timed_out": record.get("timed_out"),
        "failure_hint": record.get("failure_hint", ""),
        "stdout_summary": record.get("stdout_summary", ""),
        "stderr_summary": record.get("stderr_summary", ""),
        "artifacts": artifacts,
    }
    status = str(record.get("status") or "sandbox_error")
    return ToolResult(
        name="run_tests",
        ok=status == "tests_passed",
        output=json.dumps(sanitize_for_logging(observation), sort_keys=True),
        data=observation,
        artifacts=[path for path in artifacts.values() if path],
        error_type=None if status == "tests_passed" else status,
    )
