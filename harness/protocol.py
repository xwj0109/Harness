from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, model_validator


ALLOWED_COMMANDS = {"list_files", "read_file", "git_status", "git_diff", "apply_patch", "final_answer"}


class CommandValidationError(ValueError):
    pass


class ModelCommand(BaseModel):
    command: Literal["list_files", "read_file", "git_status", "git_diff", "apply_patch", "final_answer"]
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
        elif self.command == "final_answer":
            if not keys <= {"answer", "summary"}:
                raise ValueError("final_answer accepts answer or summary.")
            if not any(isinstance(self.arguments.get(key), str) for key in ("answer", "summary")):
                raise ValueError("final_answer requires an answer or summary string.")
        return self

    @property
    def final_text(self) -> str:
        return str(self.arguments.get("answer") or self.arguments.get("summary") or "")


def parse_model_command(raw: str, allow_apply_patch: bool = False) -> ModelCommand:
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
    return command


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
