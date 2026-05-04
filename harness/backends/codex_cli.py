from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harness.events import append_jsonl
from harness.models import BackendCapabilities, BackendConfig, BackendStatus
from harness.security import sanitize_for_logging


AUTH_ERROR = "Codex is not authenticated. Run codex login, then retry."


class CodexUnavailable(RuntimeError):
    pass


class CodexSandboxUnavailable(RuntimeError):
    pass


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
        command.extend(["--sandbox", "read-only"])
        if final_message_path and capabilities.supports_output_last_message:
            command.extend(["--output-last-message", str(final_message_path)])
        command.append(prompt)
        return command

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

    def _run_help(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(args, text=True, capture_output=True, timeout=15, env=_codex_env())


def _parse_jsonl_events(stdout: str) -> list[dict[str, Any]]:
    events = []
    for line in stdout.splitlines():
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            events.append(sanitize_for_logging(parsed))
    return events


def _codex_env() -> dict[str, str]:
    env = os.environ.copy()
    env.pop("OPENAI_API_KEY", None)
    return env


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
