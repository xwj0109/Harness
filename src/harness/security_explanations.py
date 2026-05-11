from __future__ import annotations

from typing import Any

from harness.models import BlockedStateCode, BlockedStateExplanation, SecurityDecision, SecurityDecisionStatus
from harness.security import sanitize_for_logging


def explanations_from_security_decision(
    decision: SecurityDecision | None,
    *,
    lease_id: str | None = None,
    project_root: str | None = None,
) -> list[BlockedStateExplanation]:
    if decision is None or decision.decision == SecurityDecisionStatus.ALLOW:
        return []
    details = [str(sanitize_for_logging(reason)) for reason in decision.reasons]
    if decision.missing_approvals:
        details.append("missing approvals: " + ", ".join(decision.missing_approvals))
    return [
        _explanation(
            _code_for_reason(decision.reason_code, details),
            _message_for_code(_code_for_reason(decision.reason_code, details)),
            details=details,
            inspect_command=_inspect_command(lease_id, project_root),
        )
    ]


def explanations_from_eligibility(
    eligibility: dict[str, Any] | None,
    *,
    lease_id: str | None = None,
    project_root: str | None = None,
) -> list[BlockedStateExplanation]:
    if not eligibility or eligibility.get("eligible"):
        return []
    reason_code = str(eligibility.get("reason_code") or "")
    reasons = [str(sanitize_for_logging(reason)) for reason in eligibility.get("rejection_reasons", [])]
    return [
        _explanation(
            _code_for_reason(reason_code, reasons),
            _message_for_code(_code_for_reason(reason_code, reasons)),
            details=reasons,
            inspect_command=_inspect_command(lease_id, project_root),
        )
    ]


def explanations_from_reasons(
    reasons: list[Any],
    *,
    inspect_command: str | None = None,
) -> list[BlockedStateExplanation]:
    clean = [str(sanitize_for_logging(str(reason))) for reason in reasons if str(reason).strip()]
    if not clean:
        return []
    return [_explanation(_code_for_reason("", clean), _message_for_code(_code_for_reason("", clean)), details=clean, inspect_command=inspect_command)]


def dedupe_explanations(explanations: list[BlockedStateExplanation]) -> list[BlockedStateExplanation]:
    seen: set[tuple[str, str]] = set()
    deduped: list[BlockedStateExplanation] = []
    for explanation in explanations:
        key = (explanation.code.value, explanation.message)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(explanation)
    return deduped


def render_blocked_state(explanation: BlockedStateExplanation) -> str:
    parts = [explanation.code.value, explanation.message]
    if explanation.inspect_command:
        parts.append(explanation.inspect_command)
    return " | ".join(parts)


def _code_for_reason(reason_code: str, details: list[str]) -> BlockedStateCode:
    joined = f"{reason_code} {' '.join(details)}".casefold()
    if reason_code in {"missing_required_approval", "unresolved_task_approvals"} or "approval" in joined:
        return BlockedStateCode.MISSING_APPROVAL
    if reason_code == "control_disabled" or "control_disabled" in joined:
        return BlockedStateCode.DISABLED_ADAPTER
    if reason_code == "breaker_open" or "breaker_open" in joined:
        return BlockedStateCode.BREAKER_OPEN
    if reason_code == "unsafe_metadata" or "unsafe metadata" in joined:
        return BlockedStateCode.UNSAFE_METADATA
    if reason_code == "unknown_adapter" or "unknown execution adapter" in joined:
        return BlockedStateCode.UNKNOWN_ADAPTER
    if "sandbox" in joined and ("missing" in joined or "mismatch" in joined or "invalid" in joined):
        return BlockedStateCode.SANDBOX_PROFILE_MISMATCH
    if any(term in joined for term in ("secret", ".env", ".pem", ".key", ".sqlite", ".harness", ".git", "forbidden path")):
        return BlockedStateCode.FORBIDDEN_PATH_OR_SECRET_LIKE_CONTENT
    return BlockedStateCode.BLOCKED_BY_POLICY


def _message_for_code(code: BlockedStateCode) -> str:
    return {
        BlockedStateCode.MISSING_APPROVAL: "An explicit approval is required before this action can run.",
        BlockedStateCode.DISABLED_ADAPTER: "A local runtime control is disabling this execution path.",
        BlockedStateCode.UNSAFE_METADATA: "Task metadata does not match the registered adapter contract.",
        BlockedStateCode.UNKNOWN_ADAPTER: "The task references an adapter that is not registered.",
        BlockedStateCode.SANDBOX_PROFILE_MISMATCH: "Sandbox profile evidence is missing or does not match expectations.",
        BlockedStateCode.BREAKER_OPEN: "The adapter breaker is open after repeated execution failures.",
        BlockedStateCode.FORBIDDEN_PATH_OR_SECRET_LIKE_CONTENT: "Forbidden path or secret-like evidence blocked this action.",
        BlockedStateCode.BLOCKED_BY_POLICY: "Local policy or eligibility checks blocked this action.",
    }[code]


def _explanation(
    code: BlockedStateCode,
    message: str,
    *,
    details: list[str],
    inspect_command: str | None,
) -> BlockedStateExplanation:
    return BlockedStateExplanation(
        code=code,
        message=str(sanitize_for_logging(message)),
        details=[str(sanitize_for_logging(detail)) for detail in details],
        inspect_command=str(sanitize_for_logging(inspect_command)) if inspect_command else None,
    )


def _inspect_command(lease_id: str | None, project_root: str | None) -> str | None:
    if not lease_id:
        return None
    project = project_root or "."
    return f"harness daemon inspect-lease {lease_id} --project {project} --output json"
