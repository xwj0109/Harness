from __future__ import annotations

import json
import os
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from harness.events import append_jsonl
from harness.backends.streaming import BackendStreamEvent, classify_codex_stream_item
from harness.models import BackendCapabilities, BackendConfig, BackendStatus
from harness.security import sanitize_for_logging


AUTH_ERROR = "Codex is not authenticated. Run codex login, then retry."


class CodexUnavailable(RuntimeError):
    pass


class CodexSandboxUnavailable(RuntimeError):
    pass


class CodexEditCommandUnavailable(RuntimeError):
    pass


class CodexDangerousFlagError(ValueError):
    pass


NETWORK_NOT_ENFORCEABLE = "network isolation is not enforceable by the harness for Codex subprocesses"
DANGEROUS_CODEX_FLAGS = {"--dangerously-bypass-approvals-and-sandbox", "--yolo"}
SANDBOX_FLAG = "--sandbox"
DANGER_FULL_ACCESS = "danger-full-access"
ALLOWED_REASONING_EFFORTS = {"none", "low", "medium", "high", "xhigh"}


@dataclass
class CodexRunResult:
    command: list[str]
    stdout: str
    stderr: str
    exit_status: int
    json_events: list[dict[str, Any]]
    final_message: str | None


class CodexCliBackend:
    def __init__(self, config: BackendConfig) -> None:
        self.config = config

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def command_name(self) -> str:
        return str(self.config.settings.get("command", "codex"))

    def detect_capabilities(self) -> BackendCapabilities:
        top = self._run_help([self.command_name, "--help"])
        exec_help = self._run_help([self.command_name, "exec", "--help"])
        login_help = self._run_help([self.command_name, "login", "--help"])
        exec_available = top.returncode == 0 and "exec" in top.stdout
        return BackendCapabilities(
            structured_output="--output-schema" in exec_help.stdout,
            tool_calling=False,
            json_mode="--json" in exec_help.stdout,
            max_context_tokens=None,
            supports_exec=exec_available,
            supports_read_only_sandbox="--sandbox" in exec_help.stdout and "read-only" in exec_help.stdout,
            supports_json_events="--json" in exec_help.stdout,
            supports_cd="--cd" in exec_help.stdout,
            supports_model_arg="--model" in exec_help.stdout,
            supports_output_last_message="--output-last-message" in exec_help.stdout,
            supports_output_schema="--output-schema" in exec_help.stdout,
            supports_login_status="status" in login_help.stdout,
            supports_workspace_write_sandbox="--sandbox" in exec_help.stdout and "workspace-write" in exec_help.stdout,
            supports_ask_for_approval="--ask-for-approval" in exec_help.stdout,
            supports_network_control=_detect_network_control(exec_help.stdout),
            supports_full_auto="--full-auto" in exec_help.stdout,
            supports_full_auto_workspace_write_on_request=_full_auto_documents_workspace_write_on_request(exec_help.stdout),
            supports_skip_git_repo_check="--skip-git-repo-check" in exec_help.stdout,
        )

    def preflight(self) -> BackendStatus:
        try:
            top = self._run_help([self.command_name, "--help"])
        except FileNotFoundError:
            return BackendStatus(
                available=False,
                reason="Codex CLI is not installed or not on PATH.",
                metadata=self.config.metadata,
                capabilities=self.config.capabilities,
            )
        capabilities = self.detect_capabilities()
        if top.returncode != 0 or "exec" not in top.stdout:
            return BackendStatus(
                available=False,
                reason="Codex CLI does not expose `codex exec`.",
                metadata=self.config.metadata,
                capabilities=capabilities,
            )
        auth = subprocess.run(
            [self.command_name, "login", "status"],
            text=True,
            capture_output=True,
            timeout=15,
            env=_codex_env(),
        )
        if auth.returncode != 0:
            return BackendStatus(
                available=False,
                reason=AUTH_ERROR,
                metadata=self.config.metadata,
                capabilities=capabilities,
            )
        return BackendStatus(
            available=True,
            reason=None,
            metadata=self.config.metadata,
            capabilities=capabilities,
        )

    def build_read_only_command(self, project_root: Path, prompt: str, final_message_path: Path | None) -> list[str]:
        capabilities = self.detect_capabilities()
        if not capabilities.supports_read_only_sandbox:
            raise CodexSandboxUnavailable("Codex read-only sandbox is unavailable; refusing to run Codex.")
        command = [self.command_name, "exec"]
        if capabilities.supports_json_events:
            command.append("--json")
        if capabilities.supports_cd:
            command.extend(["--cd", str(project_root)])
        model = self.config.settings.get("model")
        if model and capabilities.supports_model_arg:
            command.extend(["--model", str(model)])
        command.extend(self._reasoning_effort_args())
        if self._should_skip_git_repo_check(capabilities):
            command.append("--skip-git-repo-check")
        command.extend(["--sandbox", "read-only"])
        if final_message_path and capabilities.supports_output_last_message:
            command.extend(["--output-last-message", str(final_message_path)])
        command.append(prompt)
        validate_no_dangerous_codex_flags(command)
        return command

    def build_edit_command(
        self,
        isolated_workspace: Path,
        prompt: str,
        final_message_path: Path | None,
    ) -> tuple[list[str], BackendCapabilities, str]:
        capabilities = self.detect_capabilities()
        if not capabilities.supports_cd:
            raise CodexEditCommandUnavailable(
                "Codex edit execution requires `--cd`; refusing to run. "
                + _edit_capability_diagnostics(capabilities)
            )
        if not capabilities.supports_workspace_write_sandbox:
            raise CodexEditCommandUnavailable(
                "Codex edit execution requires an edit-capable workspace-write sandbox; refusing to run. "
                + _edit_capability_diagnostics(capabilities)
            )
        can_use_direct_approval = capabilities.supports_ask_for_approval
        can_use_full_auto = (
            capabilities.supports_full_auto
            and capabilities.supports_full_auto_workspace_write_on_request
        )
        can_use_workspace_write_without_internal_approval = capabilities.supports_workspace_write_sandbox
        command = [self.command_name, "exec"]
        if can_use_direct_approval:
            command.extend(["--sandbox", "workspace-write"])
            approval_policy = str(self.config.settings.get("ask_for_approval", "on-request"))
            if approval_policy != "on-request":
                validate_no_dangerous_codex_flags([approval_policy])
                raise CodexEditCommandUnavailable("Codex edit execution requires on-request approval policy.")
            command.extend(["--ask-for-approval", "on-request"])
            approval_mode = "on-request via --ask-for-approval"
        else:
            if can_use_full_auto:
                command.append("--full-auto")
                approval_mode = "on-request via --full-auto"
                internal_approval_enforceable = True
            elif can_use_workspace_write_without_internal_approval:
                command.extend(["--sandbox", "workspace-write"])
                approval_mode = "not available in codex exec; harness apply-back approval required"
                internal_approval_enforceable = False
            else:
                raise CodexEditCommandUnavailable(
                    "Codex edit execution requires workspace-write mode; refusing to run. "
                    + _edit_capability_diagnostics(capabilities)
                )
        if can_use_direct_approval:
            internal_approval_enforceable = True
        command.extend(["--cd", str(isolated_workspace)])
        model = self.config.settings.get("model")
        if model and capabilities.supports_model_arg:
            command.extend(["--model", str(model)])
        command.extend(self._reasoning_effort_args())
        if self._should_skip_git_repo_check(capabilities):
            command.append("--skip-git-repo-check")
        if capabilities.supports_json_events:
            command.append("--json")
        if final_message_path and capabilities.supports_output_last_message:
            command.extend(["--output-last-message", str(final_message_path)])
        command.append(prompt)
        validate_no_dangerous_codex_flags(command)
        network_status = (
            "Codex CLI exposes network-control capability; selected most restrictive supported configuration."
            if capabilities.supports_network_control
            else NETWORK_NOT_ENFORCEABLE
        )
        self.config.settings["last_codex_approval_mode"] = approval_mode
        self.config.settings["last_codex_sandbox_mode"] = "workspace-write"
        self.config.settings["last_codex_internal_command_approval_enforceable"] = internal_approval_enforceable
        self.config.settings["last_apply_back_approval_required"] = True
        return command, capabilities, network_status

    def _should_skip_git_repo_check(self, capabilities: BackendCapabilities) -> bool:
        return bool(self.config.settings.get("skip_git_repo_check", True)) and capabilities.supports_skip_git_repo_check

    def run_read_only(self, project_root: Path, prompt: str, final_message_path: Path | None) -> CodexRunResult:
        command = self.build_read_only_command(project_root, prompt, final_message_path)
        result = subprocess.run(
            command,
            cwd=project_root,
            text=True,
            capture_output=True,
            timeout=float(self.config.settings.get("timeout_seconds", 900)),
            env=_codex_env(),
        )
        final_message = None
        if final_message_path and final_message_path.exists():
            final_message = str(
                sanitize_for_logging(final_message_path.read_text(encoding="utf-8", errors="replace"))
            )
            final_message_path.write_text(final_message, encoding="utf-8")
        events = _parse_jsonl_events(result.stdout)
        return CodexRunResult(
            command=command,
            stdout=result.stdout,
            stderr=result.stderr,
            exit_status=result.returncode,
            json_events=events,
            final_message=final_message,
        )

    def stream_read_only(
        self,
        project_root: Path,
        prompt: str,
        final_message_path: Path | None,
    ) -> Iterator[dict[str, Any]]:
        command = self.build_read_only_command(project_root, prompt, final_message_path)
        process = subprocess.Popen(
            command,
            cwd=project_root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_codex_env(),
        )
        stdout_lines: list[str] = []
        stderr_lines: list[str] = []
        events: list[dict[str, Any]] = []
        stderr_thread = None
        if process.stderr is not None:
            stderr_thread = threading.Thread(target=_collect_pipe_lines, args=(process.stderr, stderr_lines), daemon=True)
            stderr_thread.start()
        assert process.stdout is not None
        for line in process.stdout:
            stdout_lines.append(line)
            parsed = _parse_jsonl_event(line)
            if parsed is not None:
                events.append(parsed)
                yield {"type": "event", "event": parsed, "line": line}
            else:
                yield {"type": "stdout", "line": line}
        exit_status = process.wait(timeout=float(self.config.settings.get("timeout_seconds", 900)))
        if stderr_thread is not None:
            stderr_thread.join(timeout=1)
        stderr = "".join(stderr_lines)
        final_message = None
        if final_message_path and final_message_path.exists():
            final_message = str(
                sanitize_for_logging(final_message_path.read_text(encoding="utf-8", errors="replace"))
            )
            final_message_path.write_text(final_message, encoding="utf-8")
        yield {
            "type": "completed",
            "result": CodexRunResult(
                command=command,
                stdout="".join(stdout_lines),
                stderr=stderr,
                exit_status=exit_status,
                json_events=events,
                final_message=final_message,
            ),
        }

    def stream_read_only_backend_events(
        self,
        project_root: Path,
        prompt: str,
        final_message_path: Path | None,
    ) -> Iterator[BackendStreamEvent]:
        for item in self.stream_read_only(project_root, prompt, final_message_path):
            yield classify_codex_stream_item(item)

    def run_edit(self, isolated_workspace: Path, prompt: str, final_message_path: Path | None) -> tuple[CodexRunResult, BackendCapabilities, str]:
        command, capabilities, network_status = self.build_edit_command(isolated_workspace, prompt, final_message_path)
        result = subprocess.run(
            command,
            cwd=isolated_workspace,
            text=True,
            capture_output=True,
            timeout=float(self.config.settings.get("timeout_seconds", 900)),
            env=_codex_env(),
        )
        final_message = None
        if final_message_path and final_message_path.exists():
            final_message = str(
                sanitize_for_logging(final_message_path.read_text(encoding="utf-8", errors="replace"))
            )
            final_message_path.write_text(final_message, encoding="utf-8")
        events = _parse_jsonl_events(result.stdout)
        return (
            CodexRunResult(
                command=command,
                stdout=result.stdout,
                stderr=result.stderr,
                exit_status=result.returncode,
                json_events=events,
                final_message=final_message,
            ),
            capabilities,
            network_status,
        )

    def build_direct_agent_command(
        self,
        project_root: Path,
        prompt: str,
        final_message_path: Path | None,
        *,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> tuple[list[str], BackendCapabilities, str]:
        capabilities = self.detect_capabilities()
        if not capabilities.supports_cd:
            raise CodexEditCommandUnavailable(
                "Codex direct agent execution requires `--cd`; refusing to run. "
                + _edit_capability_diagnostics(capabilities)
            )
        if not capabilities.supports_workspace_write_sandbox:
            raise CodexEditCommandUnavailable(
                "Codex direct agent execution requires workspace-write sandbox support; refusing to run. "
                + _edit_capability_diagnostics(capabilities)
            )
        command = [self.command_name, "exec"]
        if capabilities.supports_ask_for_approval:
            command.extend(["--sandbox", "workspace-write", "--ask-for-approval", "on-request"])
            approval_mode = "on-request via --ask-for-approval"
            internal_approval_enforceable = True
        elif capabilities.supports_full_auto and capabilities.supports_full_auto_workspace_write_on_request:
            command.append("--full-auto")
            approval_mode = "on-request via --full-auto"
            internal_approval_enforceable = True
        else:
            command.extend(["--sandbox", "workspace-write"])
            approval_mode = "workspace-write without explicit Codex approval flag"
            internal_approval_enforceable = False
        command.extend(["--cd", str(project_root)])
        selected_model = model or self.config.settings.get("model")
        if selected_model and capabilities.supports_model_arg:
            command.extend(["--model", str(selected_model)])
        command.extend(_reasoning_effort_args(reasoning_effort or self.config.settings.get("model_reasoning_effort")))
        if self._should_skip_git_repo_check(capabilities):
            command.append("--skip-git-repo-check")
        if capabilities.supports_json_events:
            command.append("--json")
        if final_message_path and capabilities.supports_output_last_message:
            command.extend(["--output-last-message", str(final_message_path)])
        command.append(prompt)
        validate_no_dangerous_codex_flags(command)
        network_status = (
            "Codex CLI exposes network-control capability; selected most restrictive supported configuration."
            if capabilities.supports_network_control
            else NETWORK_NOT_ENFORCEABLE
        )
        self.config.settings["last_codex_approval_mode"] = approval_mode
        self.config.settings["last_codex_sandbox_mode"] = "workspace-write"
        self.config.settings["last_codex_internal_command_approval_enforceable"] = internal_approval_enforceable
        self.config.settings["last_apply_back_approval_required"] = False
        return command, capabilities, network_status

    def run_direct_agent(
        self,
        project_root: Path,
        prompt: str,
        final_message_path: Path | None,
        *,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> tuple[CodexRunResult, BackendCapabilities, str]:
        command, capabilities, network_status = self.build_direct_agent_command(
            project_root,
            prompt,
            final_message_path,
            model=model,
            reasoning_effort=reasoning_effort,
        )
        result = subprocess.run(
            command,
            cwd=project_root,
            text=True,
            capture_output=True,
            timeout=float(self.config.settings.get("timeout_seconds", 900)),
            env=_codex_env(),
        )
        final_message = None
        if final_message_path and final_message_path.exists():
            final_message = str(
                sanitize_for_logging(final_message_path.read_text(encoding="utf-8", errors="replace"))
            )
            final_message_path.write_text(final_message, encoding="utf-8")
        events = _parse_jsonl_events(result.stdout)
        return (
            CodexRunResult(
                command=command,
                stdout=result.stdout,
                stderr=result.stderr,
                exit_status=result.returncode,
                json_events=events,
                final_message=final_message,
            ),
            capabilities,
            network_status,
        )

    def stream_direct_agent(
        self,
        project_root: Path,
        prompt: str,
        final_message_path: Path | None,
        *,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        command, capabilities, network_status = self.build_direct_agent_command(
            project_root,
            prompt,
            final_message_path,
            model=model,
            reasoning_effort=reasoning_effort,
        )
        process = subprocess.Popen(
            command,
            cwd=project_root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_codex_env(),
        )
        stdout_lines: list[str] = []
        stderr_lines: list[str] = []
        events: list[dict[str, Any]] = []
        stderr_thread = None
        if process.stderr is not None:
            stderr_thread = threading.Thread(target=_collect_pipe_lines, args=(process.stderr, stderr_lines), daemon=True)
            stderr_thread.start()
        assert process.stdout is not None
        for line in process.stdout:
            stdout_lines.append(line)
            parsed = _parse_jsonl_event(line)
            if parsed is not None:
                events.append(parsed)
                yield {"type": "event", "event": parsed, "line": line}
            else:
                yield {"type": "stdout", "line": line}
        exit_status = process.wait(timeout=float(self.config.settings.get("timeout_seconds", 900)))
        if stderr_thread is not None:
            stderr_thread.join(timeout=1)
        final_message = None
        if final_message_path and final_message_path.exists():
            final_message = str(
                sanitize_for_logging(final_message_path.read_text(encoding="utf-8", errors="replace"))
            )
            final_message_path.write_text(final_message, encoding="utf-8")
        yield {
            "type": "completed",
            "capabilities": capabilities,
            "network_status": network_status,
            "result": CodexRunResult(
                command=command,
                stdout="".join(stdout_lines),
                stderr="".join(stderr_lines),
                exit_status=exit_status,
                json_events=events,
                final_message=final_message,
            ),
        }

    def stream_direct_agent_backend_events(
        self,
        project_root: Path,
        prompt: str,
        final_message_path: Path | None,
        *,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> Iterator[BackendStreamEvent]:
        for item in self.stream_direct_agent(
            project_root,
            prompt,
            final_message_path,
            model=model,
            reasoning_effort=reasoning_effort,
        ):
            yield classify_codex_stream_item(item)

    def _run_help(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(args, text=True, capture_output=True, timeout=15, env=_codex_env())

    def _reasoning_effort_args(self) -> list[str]:
        return _reasoning_effort_args(self.config.settings.get("model_reasoning_effort"))


def _reasoning_effort_args(effort: Any) -> list[str]:
    if effort is None:
        return []
    effort_value = str(effort)
    if effort_value not in ALLOWED_REASONING_EFFORTS:
        raise CodexDangerousFlagError(f"Unsupported Codex reasoning effort: {effort_value}")
    return ["-c", f'model_reasoning_effort="{effort_value}"']


def _parse_jsonl_events(stdout: str) -> list[dict[str, Any]]:
    events = []
    for line in stdout.splitlines():
        parsed = _parse_jsonl_event(line)
        if parsed is not None:
            events.append(parsed)
    return events


def _parse_jsonl_event(line: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(line)
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, dict):
        return sanitize_for_logging(parsed)
    return None


def _collect_pipe_lines(pipe: Any, lines: list[str]) -> None:
    for line in pipe:
        lines.append(line)


def _codex_env() -> dict[str, str]:
    env = os.environ.copy()
    env.pop("OPENAI_API_KEY", None)
    return env


def validate_no_dangerous_codex_flags(command: list[str]) -> None:
    present = sorted(DANGEROUS_CODEX_FLAGS.intersection(command))
    if present:
        raise CodexDangerousFlagError(f"Dangerous Codex flags are not allowed: {', '.join(present)}")
    for index, part in enumerate(command[:-1]):
        if part == SANDBOX_FLAG and command[index + 1] == DANGER_FULL_ACCESS:
            raise CodexDangerousFlagError("Dangerous Codex sandbox mode is not allowed: danger-full-access")


def _detect_network_control(help_text: str) -> bool:
    lowered = help_text.lower()
    return "--network" in lowered or "network" in lowered and ("disable" in lowered or "off" in lowered)


def _full_auto_documents_workspace_write_on_request(help_text: str) -> bool:
    lowered = " ".join(help_text.lower().replace(",", " ").split())
    if "--full-auto" not in lowered:
        return False
    return (
        "-a on-request" in lowered
        and "--sandbox workspace-write" in lowered
    )


def _edit_capability_diagnostics(capabilities: BackendCapabilities) -> str:
    return (
        "Detected edit capabilities: "
        f"supports_full_auto={capabilities.supports_full_auto}, "
        f"supports_workspace_write_sandbox={capabilities.supports_workspace_write_sandbox}, "
        f"supports_ask_for_approval={capabilities.supports_ask_for_approval}, "
        f"supports_cd={capabilities.supports_cd}, "
        "full_auto_documents_workspace_write_on_request="
        f"{capabilities.supports_full_auto_workspace_write_on_request}."
    )


def write_codex_artifacts(run_dir: Path, result: CodexRunResult) -> dict[str, Path]:
    stdout_path = run_dir / "codex_stdout.txt"
    stderr_path = run_dir / "codex_stderr.txt"
    events_path = run_dir / "codex_events.jsonl"
    stdout_path.write_text(str(sanitize_for_logging(result.stdout)), encoding="utf-8")
    stderr_path.write_text(str(sanitize_for_logging(result.stderr)), encoding="utf-8")
    for event in result.json_events:
        append_jsonl(events_path, sanitize_for_logging(event))
    events_path.touch(exist_ok=True)
    return {"codex_stdout": stdout_path, "codex_stderr": stderr_path, "codex_events": events_path}
