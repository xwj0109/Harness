from __future__ import annotations

import re
from enum import Enum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from harness.security import sanitize_for_logging


class ManagedActionRisk(str, Enum):
    READ_ONLY = "read_only"
    LOCAL_WORKSPACE_WRITE_LOW = "local_workspace_write_low"
    LOCAL_WORKSPACE_WRITE_MEDIUM = "local_workspace_write_medium"
    SANDBOXED_EXECUTION = "sandboxed_execution"
    HOSTED_PROVIDER = "hosted_provider"
    ACTIVE_REPO_APPLY_BACK = "active_repo_apply_back"
    DESTRUCTIVE = "destructive"
    EXTERNAL_NETWORK = "external_network"


class ManagedActionDecisionStatus(str, Enum):
    AUTO_ALLOWED = "auto_allowed"
    APPROVAL_REQUIRED = "approval_required"
    DENIED = "denied"
    UNSUPPORTED = "unsupported"


class ManagedActionSandboxStatus(str, Enum):
    NOT_RUN = "not_run"
    SAFE = "safe"
    DANGEROUS = "dangerous"


class ManagedActionSandboxAssessment(BaseModel):
    schema_version: str = "harness.managed_action_sandbox_assessment/v1"
    status: ManagedActionSandboxStatus
    sandbox_profile: str = "managed_action_preflight"
    executor: str
    dangerous: bool = False
    reasons: list[str] = Field(default_factory=list)
    expected_paths: list[str] = Field(default_factory=list)


class ManagedActionRoute(BaseModel):
    schema_version: str = "harness.managed_action_route/v1"
    intent: str
    confidence: Literal["exact", "pattern", "fallback"]
    risk: ManagedActionRisk
    executor: str
    normalized_arguments: dict[str, Any] = Field(default_factory=dict)
    required_approvals: list[str] = Field(default_factory=list)
    expected_outputs: list[str] = Field(default_factory=list)
    policy_notes: list[str] = Field(default_factory=list)


class ManagedActionDecision(BaseModel):
    schema_version: str = "harness.managed_action_decision/v1"
    status: ManagedActionDecisionStatus
    route: ManagedActionRoute
    reasons: list[str] = Field(default_factory=list)
    requires_human: bool = False
    sandbox_assessment: ManagedActionSandboxAssessment | None = None


class ManagedActionResult(BaseModel):
    schema_version: str = "harness.managed_action_result/v1"
    ok: bool
    status: str
    intent: str
    run_id: str | None = None
    created_paths: list[Path] = Field(default_factory=list)
    changed_paths: list[Path] = Field(default_factory=list)
    artifact_ids: list[str] = Field(default_factory=list)
    report_path: Path | None = None
    manifest_path: Path | None = None
    message: str
    next_actions: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


def route_managed_action(instruction: str, project_root: Path | None = None) -> ManagedActionRoute:
    normalized = _normalize(instruction)
    markdown_filename = _filename_from_text(instruction, {".md"})
    text_filename = _filename_from_text(instruction, {".txt"})
    writable_filename = markdown_filename or text_filename
    file_write_text = _file_write_text_from_text(instruction, writable_filename)
    directory_name = _directory_name_from_text(instruction)
    note_text = _note_text_from_text(instruction)

    if _looks_like_run_tests_request(normalized):
        return ManagedActionRoute(
            intent="run_tests",
            confidence="exact" if normalized in {"test", "run tests", "run the tests"} else "pattern",
            risk=ManagedActionRisk.SANDBOXED_EXECUTION,
            executor="run_tests",
            normalized_arguments={
                "suggested_command": "pytest -q",
                "scope": "managed_action",
                "request": sanitize_for_logging(instruction),
            },
            required_approvals=["docker_execution"],
            expected_outputs=["test_result.json", "final_report.md", "manifest.json"],
            policy_notes=["Sandboxed test execution requires approval before execution."],
        )
    if writable_filename and file_write_text is not None and _looks_like_file_write_request(normalized):
        return ManagedActionRoute(
            intent="write_file",
            confidence="exact",
            risk=ManagedActionRisk.LOCAL_WORKSPACE_WRITE_LOW,
            executor="write_file",
            normalized_arguments={
                "filename": writable_filename,
                "text": file_write_text,
                "allowed_extensions": [Path(writable_filename).suffix],
                "overwrite_policy": "append_or_create",
                "request": sanitize_for_logging(instruction),
            },
            expected_outputs=["changed_file", "final_report.md", "manifest.json"],
            policy_notes=["Local low-risk file content write."],
        )
    if _looks_like_empty_markdown_request(normalized, markdown_filename):
        return ManagedActionRoute(
            intent="create_empty_markdown_file",
            confidence="exact" if markdown_filename else "pattern",
            risk=ManagedActionRisk.LOCAL_WORKSPACE_WRITE_LOW,
            executor="create_empty_file",
            normalized_arguments={
                "filename": markdown_filename or "scratch.md",
                "default_filename": "scratch.md",
                "allowed_extensions": [".md"],
                "overwrite_policy": "never",
                "request": sanitize_for_logging(instruction),
            },
            expected_outputs=["created_file", "final_report.md", "manifest.json"],
            policy_notes=["Local low-risk empty Markdown file creation."],
        )
    if _looks_like_empty_text_request(normalized, text_filename):
        return ManagedActionRoute(
            intent="create_empty_text_file",
            confidence="exact" if text_filename else "pattern",
            risk=ManagedActionRisk.LOCAL_WORKSPACE_WRITE_LOW,
            executor="create_empty_file",
            normalized_arguments={
                "filename": text_filename or "scratch.txt",
                "default_filename": "scratch.txt",
                "allowed_extensions": [".txt"],
                "overwrite_policy": "never",
                "request": sanitize_for_logging(instruction),
            },
            expected_outputs=["created_file", "final_report.md", "manifest.json"],
            policy_notes=["Local low-risk empty text file creation."],
        )
    if _looks_like_directory_request(normalized, directory_name):
        return ManagedActionRoute(
            intent="create_directory",
            confidence="exact" if directory_name else "pattern",
            risk=ManagedActionRisk.LOCAL_WORKSPACE_WRITE_LOW,
            executor="create_directory",
            normalized_arguments={
                "dirname": directory_name or "new-folder",
                "overwrite_policy": "no_op_if_exists",
                "request": sanitize_for_logging(instruction),
            },
            expected_outputs=["final_report.md", "manifest.json"],
            policy_notes=["Local low-risk directory creation."],
        )
    if _looks_like_note_request(normalized, note_text):
        return ManagedActionRoute(
            intent="local_note",
            confidence="pattern",
            risk=ManagedActionRisk.LOCAL_WORKSPACE_WRITE_LOW,
            executor="write_note_file",
            normalized_arguments={
                "filename": "notes.md",
                "text": note_text or instruction,
                "overwrite_policy": "append",
                "request": sanitize_for_logging(instruction),
            },
            expected_outputs=["changed_file", "final_report.md", "manifest.json"],
            policy_notes=["Local low-risk note append."],
        )
    return ManagedActionRoute(
        intent="unsupported",
        confidence="fallback",
        risk=ManagedActionRisk.READ_ONLY,
        executor="none",
        normalized_arguments={"request": sanitize_for_logging(instruction)},
        expected_outputs=[],
        policy_notes=["No managed local action route matched."],
    )


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower()).strip(" .?!")


def _filename_from_text(text: str, allowed_suffixes: set[str]) -> str | None:
    suffix_pattern = "|".join(re.escape(suffix.lstrip(".")) for suffix in sorted(allowed_suffixes))
    match = re.search(rf"(?<!\S)([A-Za-z0-9_./\\-]+\.({suffix_pattern}))(?!\S)", text)
    if not match:
        return None
    filename = match.group(1)
    if Path(filename).suffix not in allowed_suffixes:
        return None
    return filename


def _directory_name_from_text(text: str) -> str | None:
    match = re.search(r"(?:folder|directory|dir)\s+(?:called|named)?\s*([A-Za-z0-9][A-Za-z0-9_.-]*)", text, re.I)
    if not match:
        return None
    dirname = match.group(1).strip(".")
    if "/" in dirname or "\\" in dirname or not dirname:
        return None
    return dirname


def _note_text_from_text(text: str) -> str | None:
    match = re.search(r"(?:write|add|save)\s+(?:a\s+)?note(?:\s+that|\s*:)?\s+(.+)", text, re.I)
    return match.group(1).strip() if match else None


def _file_write_text_from_text(text: str, filename: str | None) -> str | None:
    if not filename:
        return None
    quoted = re.search(r"['\"]([^'\"]*)['\"]", text)
    if quoted:
        return quoted.group(1)
    after_filename = re.search(rf"{re.escape(filename)}\s+(?:with|containing|content|text)?\s*:?\s*(.+)", text, re.I)
    if after_filename:
        candidate = after_filename.group(1).strip()
        return candidate or None
    return None


def _looks_like_empty_markdown_request(normalized: str, filename: str | None) -> bool:
    words = set(normalized.split())
    if not {"create", "make", "add", "do"}.intersection(words):
        return False
    if "write" in words and "empty" not in words and "blank" not in words:
        return False
    if filename and filename.endswith(".md"):
        return True
    if ".md" not in normalized and "markdown" not in normalized:
        return False
    return any(marker in normalized for marker in {"empty", "blank", ".md", "markdown"})


def _looks_like_empty_text_request(normalized: str, filename: str | None) -> bool:
    words = set(normalized.split())
    if not {"create", "make", "add", "do"}.intersection(words):
        return False
    if "write" in words and "empty" not in words and "blank" not in words:
        return False
    if filename and filename.endswith(".txt"):
        return True
    if ".txt" not in normalized and "text file" not in normalized:
        return False
    return any(marker in normalized for marker in {"empty", "blank", ".txt", "text file"})


def _looks_like_directory_request(normalized: str, dirname: str | None) -> bool:
    words = set(normalized.split())
    return bool({"create", "make", "add"}.intersection(words)) and (
        dirname is not None or " folder" in normalized or " directory" in normalized
    )


def _looks_like_note_request(normalized: str, note_text: str | None) -> bool:
    words = set(normalized.split())
    return bool({"write", "add", "save"}.intersection(words)) and ("note" in words or note_text is not None)


def _looks_like_file_write_request(normalized: str) -> bool:
    words = set(normalized.split())
    return bool({"write", "add", "append", "put", "save"}.intersection(words))


def _looks_like_run_tests_request(normalized: str) -> bool:
    return normalized in {"test", "run tests", "run the tests"}
