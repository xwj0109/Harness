from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harness.governance.protected_paths import is_protected_apply_path
from harness.paths import PathSecurityError, is_excluded_relative, relative_to_project, resolve_under_project
from harness.security import SecretBlockedError, assert_not_secret_path
from harness.tools.base import ToolContext, ToolResult, ToolSpec


class PatchValidationError(ValueError):
    pass


@dataclass
class FilePatch:
    old_path: str
    new_path: str
    hunks: list[str]


@dataclass
class PatchSummary:
    files: list[str]
    added_lines: int
    removed_lines: int

    def render(self) -> str:
        return (
            f"Files: {', '.join(self.files) if self.files else 'none'}\n"
            f"Added lines: {self.added_lines}\n"
            f"Removed lines: {self.removed_lines}"
        )


@dataclass
class PlannedFileUpdate:
    relative_path: str
    path: Path
    content: str


class ApplyPatchTool:
    spec = ToolSpec(
        name="apply_patch",
        description="Apply a validated unified diff patch inside the project root.",
        input_schema={"type": "object", "properties": {"patch": {"type": "string"}}, "required": ["patch"]},
        risk_level="write",
    )

    def validate(self, patch: str, context: ToolContext) -> PatchSummary:
        summary, _updates = plan_unified_diff(patch, context.project_root, context.context_excludes)
        return summary

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        patch = str(arguments.get("patch", ""))
        try:
            summary, updates = plan_unified_diff(patch, context.project_root, context.context_excludes)
            apply_planned_updates(updates)
            return ToolResult(
                name=self.spec.name,
                ok=True,
                output="Patch applied.",
                data={"summary": summary.render(), "files": summary.files},
            )
        except (PatchValidationError, PathSecurityError, SecretBlockedError) as exc:
            return ToolResult(name=self.spec.name, ok=False, output=str(exc), error_type="patch_validation")


def parse_unified_diff(patch: str) -> list[FilePatch]:
    if "GIT binary patch" in patch or "Binary files " in patch:
        raise PatchValidationError("Binary patches are not allowed.")
    lines = patch.splitlines()
    patches: list[FilePatch] = []
    i = 0
    while i < len(lines):
        if not lines[i].startswith("--- "):
            i += 1
            continue
        old_path = lines[i][4:].split("\t", 1)[0].strip()
        i += 1
        if i >= len(lines) or not lines[i].startswith("+++ "):
            raise PatchValidationError("Malformed unified diff: missing +++ path.")
        new_path = lines[i][4:].split("\t", 1)[0].strip()
        i += 1
        hunks: list[str] = []
        while i < len(lines) and not lines[i].startswith("--- "):
            hunks.append(lines[i])
            i += 1
        if not any(line.startswith("@@ ") for line in hunks):
            raise PatchValidationError("Malformed unified diff: missing hunk header.")
        patches.append(FilePatch(old_path=old_path, new_path=new_path, hunks=hunks))
    if not patches:
        raise PatchValidationError("Malformed patch: no unified diff file headers found.")
    return patches


def plan_unified_diff(
    patch: str,
    project_root: Path,
    context_excludes: list[str] | None = None,
) -> tuple[PatchSummary, list[PlannedFileUpdate]]:
    files: list[str] = []
    added = 0
    removed = 0
    planned_by_path: dict[Path, PlannedFileUpdate] = {}
    for file_patch in parse_unified_diff(patch):
        path, relative_path = validate_file_patch_target(file_patch, project_root, context_excludes or [])
        original_text = planned_by_path[path].content if path in planned_by_path else read_patch_target(path)
        original = split_preserving_lines(original_text)
        updated = apply_hunks(original, file_patch.hunks)
        planned_by_path[path] = PlannedFileUpdate(relative_path=relative_path, path=path, content="".join(updated))
        if relative_path not in files:
            files.append(relative_path)
        for line in file_patch.hunks:
            if line.startswith("+") and not line.startswith("+++"):
                added += 1
            elif line.startswith("-") and not line.startswith("---"):
                removed += 1
    return PatchSummary(files=files, added_lines=added, removed_lines=removed), list(planned_by_path.values())


def validate_file_patch_target(
    file_patch: FilePatch,
    project_root: Path,
    context_excludes: list[str],
) -> tuple[Path, str]:
    target = normalized_patch_path(file_patch.new_path)
    old = normalized_patch_path(file_patch.old_path)
    if file_patch.new_path == "/dev/null" or file_patch.old_path == "/dev/null":
        raise PatchValidationError("Creating or deleting files is not allowed in Phase 2A.")
    if target != old:
        raise PatchValidationError("Renames are not allowed in Phase 2A.")
    resolved = resolve_under_project(project_root, target)
    assert_not_secret_path(resolved)
    if _is_blocked_edit_path(target):
        raise PatchValidationError(f"Blocked edit path: {target}")
    if not resolved.exists():
        raise PatchValidationError(f"Patch target does not exist: {target}")
    if not resolved.is_file():
        raise PatchValidationError(f"Patch target is not a file: {target}")
    relative_path = relative_to_project(project_root, resolved)
    if is_excluded_relative(relative_path, context_excludes):
        raise PatchValidationError(f"Patch target is excluded from model context: {relative_path}")
    return resolved, relative_path


def apply_planned_updates(updates: list[PlannedFileUpdate]) -> None:
    originals = {update.path: update.path.read_bytes() for update in updates}
    written: list[Path] = []
    try:
        for update in updates:
            written.append(update.path)
            update.path.write_bytes(update.content.encode("utf-8"))
    except Exception:
        for path in originals:
            path.write_bytes(originals[path])
        raise


def read_patch_target(path: Path) -> str:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return handle.read()


def apply_unified_diff(patch: str, project_root: Path) -> None:
    _summary, updates = plan_unified_diff(patch, project_root)
    apply_planned_updates(updates)


def apply_hunks(original: list[str], hunk_lines: list[str]) -> list[str]:
    output: list[str] = []
    original_index = 0
    i = 0
    while i < len(hunk_lines):
        header = hunk_lines[i]
        if not header.startswith("@@ "):
            i += 1
            continue
        match = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", header)
        if not match:
            raise PatchValidationError(f"Malformed hunk header: {header}")
        old_start = int(match.group(1)) - 1
        if old_start < original_index:
            raise PatchValidationError("Overlapping hunks are not allowed.")
        output.extend(original[original_index:old_start])
        original_index = old_start
        i += 1
        while i < len(hunk_lines) and not hunk_lines[i].startswith("@@ "):
            line = hunk_lines[i]
            if line.startswith(" "):
                expected = line[1:]
                if original_index >= len(original) or line_body(original[original_index]) != expected:
                    raise PatchValidationError("Patch context does not match target file.")
                output.append(original[original_index])
                original_index += 1
            elif line.startswith("-"):
                expected = line[1:]
                if original_index >= len(original) or line_body(original[original_index]) != expected:
                    raise PatchValidationError("Patch removal does not match target file.")
                original_index += 1
            elif line.startswith("+"):
                added_line = line[1:] + default_eol(original)
                if i + 1 < len(hunk_lines) and hunk_lines[i + 1] == r"\ No newline at end of file":
                    added_line = line[1:]
                    i += 1
                output.append(added_line)
            elif line == r"\ No newline at end of file":
                pass
            else:
                raise PatchValidationError(f"Malformed hunk line: {line}")
            i += 1
    output.extend(original[original_index:])
    return output


def split_preserving_lines(text: str) -> list[str]:
    if text == "":
        return []
    return text.splitlines(keepends=True)


def line_body(line: str) -> str:
    return line.removesuffix("\n").removesuffix("\r")


def default_eol(lines: list[str]) -> str:
    crlf = sum(1 for line in lines if line.endswith("\r\n"))
    lf = sum(1 for line in lines if line.endswith("\n") and not line.endswith("\r\n"))
    return "\r\n" if crlf > lf else "\n"


def normalized_patch_path(path: str) -> str:
    if path == "/dev/null":
        return path
    if path.startswith("a/") or path.startswith("b/"):
        return path[2:]
    return path


def _is_blocked_edit_path(relative_path: str) -> bool:
    return is_protected_apply_path(relative_path)
