from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from harness.paths import (
    PathSecurityError,
    is_excluded_relative,
    relative_to_project,
    resolve_under_project,
)
from harness.security import SecretBlockedError, assert_not_secret_path, is_secret_path
from harness.tools.base import ToolContext, ToolResult, ToolSpec

MAX_READ_BYTES = 1024 * 1024


class ListFilesTool:
    spec = ToolSpec(
        name="list_files",
        description="List non-secret files under the project root.",
        input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
        risk_level="read",
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        requested = arguments.get("path", ".")
        try:
            root = context.project_root.resolve()
            start = resolve_under_project(root, requested)
            if not start.exists():
                return ToolResult(name=self.spec.name, ok=False, output="Path does not exist.", error_type="missing")
            if start.is_file():
                candidates = [start]
            else:
                candidates = [p for p in start.rglob("*") if p.is_file()]
            files: list[str] = []
            blocked: list[str] = []
            for path in sorted(candidates):
                rel = relative_to_project(root, path)
                if is_excluded_relative(rel, context.context_excludes):
                    continue
                if is_secret_path(path):
                    blocked.append(rel)
                    continue
                files.append(rel)
            return ToolResult(
                name=self.spec.name,
                ok=True,
                output="\n".join(files),
                data={"files": files, "blocked_secret_paths": blocked},
            )
        except PathSecurityError as exc:
            return ToolResult(name=self.spec.name, ok=False, output=str(exc), error_type="path_security")


class ReadFileTool:
    spec = ToolSpec(
        name="read_file",
        description="Read a non-secret text file under the project root.",
        input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
        risk_level="read",
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        requested = arguments.get("path")
        if not requested:
            return ToolResult(name=self.spec.name, ok=False, output="Missing path.", error_type="validation")
        try:
            root = context.project_root.resolve()
            path = resolve_under_project(root, requested)
            assert_not_secret_path(path)
            if not path.exists():
                return ToolResult(name=self.spec.name, ok=False, output="File does not exist.", error_type="missing")
            if not path.is_file():
                return ToolResult(name=self.spec.name, ok=False, output="Path is not a file.", error_type="not_file")
            size = path.stat().st_size
            if size > MAX_READ_BYTES:
                return ToolResult(name=self.spec.name, ok=False, output="File is too large.", error_type="too_large")
            raw = path.read_bytes()
            if b"\x00" in raw:
                return ToolResult(name=self.spec.name, ok=False, output="File appears to be binary.", error_type="binary")
            text = raw.decode("utf-8")
            return ToolResult(
                name=self.spec.name,
                ok=True,
                output=text,
                data={"path": relative_to_project(root, path), "bytes": size},
            )
        except UnicodeDecodeError:
            return ToolResult(name=self.spec.name, ok=False, output="File is not valid UTF-8 text.", error_type="encoding")
        except PathSecurityError as exc:
            return ToolResult(name=self.spec.name, ok=False, output=str(exc), error_type="path_security")
        except SecretBlockedError as exc:
            return ToolResult(name=self.spec.name, ok=False, output=str(exc), error_type="secret_path")


class GitStatusTool:
    spec = ToolSpec(
        name="git_status",
        description="Show git status for the project.",
        input_schema={"type": "object", "properties": {}},
        risk_level="read",
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        return _run_git(["git", "status", "--short", "--branch"], context.project_root, self.spec.name)


class GitDiffTool:
    spec = ToolSpec(
        name="git_diff",
        description="Show git diff stat and patch for the project.",
        input_schema={"type": "object", "properties": {}},
        risk_level="read",
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        stat = _run_git(["git", "diff", "--stat"], context.project_root, self.spec.name)
        diff = _run_git(["git", "diff", "--"], context.project_root, self.spec.name)
        if not stat.ok:
            return stat
        output = (stat.output + "\n" + diff.output).strip()
        return ToolResult(
            name=self.spec.name,
            ok=diff.ok,
            output=output,
            data={"stat": stat.output, "diff": diff.output},
            error_type=diff.error_type,
        )


def _run_git(command: list[str], cwd: Path, tool_name: str) -> ToolResult:
    try:
        result = subprocess.run(command, cwd=cwd, text=True, capture_output=True, timeout=30)
    except FileNotFoundError:
        return ToolResult(name=tool_name, ok=False, output="git is not installed.", error_type="missing_git")
    if result.returncode != 0:
        message = (result.stderr or result.stdout or "Not a Git repository.").strip()
        return ToolResult(name=tool_name, ok=False, output=message, error_type="git_error")
    return ToolResult(name=tool_name, ok=True, output=result.stdout.strip())

