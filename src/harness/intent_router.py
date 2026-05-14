from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field

from harness.models import RunMode
from harness.security import sanitize_for_logging


INTENT_ROUTE_SCHEMA_VERSION = "harness.intent_route/v1"


class IntentRoute(BaseModel):
    schema_version: str = INTENT_ROUTE_SCHEMA_VERSION
    intent: str
    confidence: Literal["exact", "pattern", "fallback"]
    workbench_id: str
    agent_id: str
    mode: RunMode
    execution_adapter: str
    task_type: str
    default_backend: str
    escalation_backend: str | None = None
    required_approvals: list[str] = Field(default_factory=list)
    expected_outputs: list[str] = Field(default_factory=list)
    policy_notes: list[str] = Field(default_factory=list)
    equivalent_commands: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class _RouteTemplate:
    intent: str
    patterns: tuple[str, ...]
    workbench_id: str
    agent_id: str
    mode: RunMode
    execution_adapter: str
    task_type: str
    default_backend: str
    required_approvals: tuple[str, ...]
    expected_outputs: tuple[str, ...]
    policy_notes: tuple[str, ...]


ROUTE_TEMPLATES: tuple[_RouteTemplate, ...] = (
    _RouteTemplate(
        intent="fix_tests",
        patterns=("fix failing tests", "fix the failing tests", "make tests pass", "repair test failure", "fix the tests"),
        workbench_id="coding",
        agent_id="code_editor",
        mode=RunMode.CODEX_EDIT,
        execution_adapter="codex_isolated_edit",
        task_type="codex_code_edit",
        default_backend="codex_cli",
        required_approvals=("hosted_provider_codex", "isolated_workspace", "apply_back_separate"),
        expected_outputs=("diff.patch", "test_result.json", "final_report.md", "manifest.json", "transcript.jsonl"),
        policy_notes=(
            "Hosted execution is approval-gated.",
            "Edits run in an isolated workspace.",
            "Active repo apply-back is a separate explicit decision.",
        ),
    ),
    _RouteTemplate(
        intent="explain_repo",
        patterns=("explain this repo", "summarize the codebase", "summarize this repo", "inspect this repo"),
        workbench_id="coding",
        agent_id="repo_inspector",
        mode=RunMode.READ_ONLY,
        execution_adapter="read_only_summary",
        task_type="read_only_repo_summary",
        default_backend="codex_cli",
        required_approvals=("hosted_provider_codex",),
        expected_outputs=("repo_summary.md", "final_report.md", "manifest.json", "transcript.jsonl"),
        policy_notes=("Read-only repository inspection must not mutate files.",),
    ),
    _RouteTemplate(
        intent="plan_change",
        patterns=("plan", "implementation plan", "how should we build"),
        workbench_id="coding",
        agent_id="repo_inspector",
        mode=RunMode.PLANNING,
        execution_adapter="repo_planning",
        task_type="repo_planning",
        default_backend="codex_cli",
        required_approvals=("hosted_provider_codex",),
        expected_outputs=("final_report.md", "manifest.json", "transcript.jsonl"),
        policy_notes=("Planning routes are read-only and must not modify files.",),
    ),
)


def route_instruction(instruction: str) -> IntentRoute:
    normalized = _normalize(instruction)
    for template in ROUTE_TEMPLATES:
        if normalized in template.patterns:
            return _route_from_template(template, "exact", instruction)
    for template in ROUTE_TEMPLATES:
        if any(_pattern_matches(normalized, pattern) for pattern in template.patterns):
            return _route_from_template(template, "pattern", instruction)
    return IntentRoute(
        intent="unsupported",
        confidence="fallback",
        workbench_id="coding",
        agent_id="repo_inspector",
        mode=RunMode.READ_ONLY,
        execution_adapter="none",
        task_type="unsupported",
        default_backend="none",
        required_approvals=[],
        expected_outputs=("transcript.jsonl",),
        policy_notes=[
            "No automatic execution route matched this instruction.",
            "Harness will not execute side effects for unsupported instructions.",
        ],
        equivalent_commands=[f'harness chat # then clarify: "{sanitize_for_logging(instruction)}"'],
    )


def _route_from_template(template: _RouteTemplate, confidence: Literal["exact", "pattern"], instruction: str) -> IntentRoute:
    return IntentRoute(
        intent=template.intent,
        confidence=confidence,
        workbench_id=template.workbench_id,
        agent_id=template.agent_id,
        mode=template.mode,
        execution_adapter=template.execution_adapter,
        task_type=template.task_type,
        default_backend=template.default_backend,
        required_approvals=list(template.required_approvals),
        expected_outputs=list(template.expected_outputs),
        policy_notes=list(template.policy_notes),
        equivalent_commands=_equivalent_commands(template, instruction),
    )


def _equivalent_commands(template: _RouteTemplate, instruction: str) -> list[str]:
    safe_instruction = str(sanitize_for_logging(instruction)).replace('"', '\\"')
    return [
        f'harness route "{safe_instruction}" --output json',
        (
            f'harness tasks add --title "{template.intent}" --workbench {template.workbench_id} '
            f"--agent {template.agent_id} --execution-adapter {template.execution_adapter} "
            f"--task-type {template.task_type}"
        ),
        "harness daemon run-once",
        "harness daemon execute <lease_id>",
    ]


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower()).strip(" .?!")


def _pattern_matches(normalized: str, pattern: str) -> bool:
    if pattern == "plan":
        return normalized.startswith("plan ") or " implementation plan" in normalized
    return pattern in normalized
