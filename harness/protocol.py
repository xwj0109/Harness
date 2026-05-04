from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, model_validator

from harness.paths import PathSecurityError, relative_to_project, resolve_under_project
from harness.sandbox import CommandValidationError as TestCommandValidationError
from harness.sandbox import validate_test_command

ALLOWED_COMMANDS = {"list_files", "read_file", "git_status", "git_diff", "apply_patch", "run_tests", "final_answer"}


class CommandValidationError(ValueError):
    pass


class ModelCommand(BaseModel):
    command: Literal["list_files", "read_file", "git_status", "git_diff", "apply_patch", "run_tests", "final_answer"]
    arguments: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_arguments(self) -> "ModelCommand":
        keys = set(self.arguments)
        if self.command == "list_files":
            if not keys <= {"path"}:
                raise ValueError("list_files only accepts optional path.")
            if "path" in self.arguments and not isinstance(self.arguments["path"], str):
                raise ValueError("list_files path must be a string.")
        elif self.command == "read_file":
            if keys != {"path"}:
                raise ValueError("read_file requires only path.")
            if not isinstance(self.arguments["path"], str):
                raise ValueError("read_file path must be a string.")
        elif self.command in {"git_status", "git_diff"}:
            if keys:
                raise ValueError(f"{self.command} accepts no arguments.")
        elif self.command == "apply_patch":
            if keys != {"patch"}:
                raise ValueError("apply_patch requires only patch.")
            if not isinstance(self.arguments["patch"], str) or not self.arguments["patch"].strip():
                raise ValueError("apply_patch patch must be a non-empty string.")
        elif self.command == "run_tests":
            if not keys <= {"command", "cwd"} or "command" not in self.arguments:
                raise ValueError("run_tests requires command and optional cwd.")
            try:
                validate_test_command(self.arguments["command"])
            except TestCommandValidationError as exc:
                raise ValueError(str(exc)) from exc
            if "cwd" in self.arguments and not isinstance(self.arguments["cwd"], str):
                raise ValueError("run_tests cwd must be a string.")
        elif self.command == "final_answer":
            if not keys <= {"answer", "summary"}:
                raise ValueError("final_answer accepts answer or summary.")
            if not any(isinstance(self.arguments.get(key), str) for key in ("answer", "summary")):
                raise ValueError("final_answer requires an answer or summary string.")
        return self

    @property
    def final_text(self) -> str:
        return str(self.arguments.get("answer") or self.arguments.get("summary") or "")


def parse_model_command(
    raw: str,
    allow_apply_patch: bool = False,
    allow_run_tests: bool = False,
    project_root: Path | None = None,
) -> ModelCommand:
    try:
        data = json.loads(_extract_json(raw))
    except json.JSONDecodeError as exc:
        raise CommandValidationError(f"Malformed JSON command: {exc}") from exc
    data = _normalize_command_shorthand(data)
    try:
        command = ModelCommand.model_validate(data)
    except ValidationError as exc:
        raise CommandValidationError(f"Invalid command schema: {exc}") from exc
    if command.command == "apply_patch" and not allow_apply_patch:
        raise CommandValidationError("apply_patch is not allowed for this task type.")
    if command.command == "run_tests":
        if not allow_run_tests:
            raise CommandValidationError("run_tests is not allowed for this task type.")
        if project_root is not None and "cwd" in command.arguments:
            command.arguments["cwd"] = _validate_run_tests_cwd(project_root, command.arguments["cwd"])
    return command


def _validate_run_tests_cwd(project_root: Path, cwd: str) -> str:
    if not cwd.strip():
        raise CommandValidationError("run_tests cwd must be a non-empty project-relative path.")
    raw = Path(cwd)
    if raw.is_absolute():
        raise CommandValidationError("run_tests cwd must be project-relative.")
    try:
        resolved = resolve_under_project(project_root, raw)
    except PathSecurityError as exc:
        raise CommandValidationError(str(exc)) from exc
    if not resolved.exists():
        raise CommandValidationError(f"run_tests cwd does not exist: {cwd}")
    if not resolved.is_dir():
        raise CommandValidationError(f"run_tests cwd is not a directory: {cwd}")
    return "." if resolved == project_root.resolve() else relative_to_project(project_root, resolved)


def _normalize_command_shorthand(data: Any) -> Any:
    if not isinstance(data, dict):
        return data
    command = data.get("command")
    arguments = data.get("arguments")
    normalized = dict(data)
    if command == "list_files" and isinstance(arguments, str):
        normalized["arguments"] = {"path": arguments or "."}
    elif command == "read_file" and isinstance(arguments, str):
        normalized["arguments"] = {"path": arguments}
    elif command == "read_file" and isinstance(arguments, dict) and set(arguments) == {"file_path"}:
        normalized["arguments"] = {"path": arguments["file_path"]}
    elif command in {"git_status", "git_diff"} and arguments == "":
        normalized["arguments"] = {}
    elif command == "final_answer" and isinstance(arguments, str):
        normalized["arguments"] = {"answer": arguments}
    return normalized


def _extract_json(raw: str) -> str:
    stripped = raw.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        return "\n".join(lines).strip()
    return stripped
