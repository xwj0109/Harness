from __future__ import annotations

from pathlib import Path

from harness.action_router import (
    ManagedActionDecision,
    ManagedActionDecisionStatus,
    ManagedActionRisk,
    ManagedActionRoute,
)
from harness.security import is_secret_path


AUTO_ALLOWED_RISKS = {ManagedActionRisk.READ_ONLY, ManagedActionRisk.LOCAL_WORKSPACE_WRITE_LOW}
APPROVAL_REQUIRED_RISKS = {
    ManagedActionRisk.LOCAL_WORKSPACE_WRITE_MEDIUM,
    ManagedActionRisk.SANDBOXED_EXECUTION,
    ManagedActionRisk.HOSTED_PROVIDER,
    ManagedActionRisk.ACTIVE_REPO_APPLY_BACK,
}
DENIED_RISKS = {ManagedActionRisk.DESTRUCTIVE, ManagedActionRisk.EXTERNAL_NETWORK}


def decide_managed_action(route: ManagedActionRoute, project_root: Path) -> ManagedActionDecision:
    if route.intent == "unsupported":
        return ManagedActionDecision(
            status=ManagedActionDecisionStatus.UNSUPPORTED,
            route=route,
            reasons=["No managed local action route matched."],
        )
    path_reasons = _path_policy_reasons(route, project_root)
    if path_reasons:
        return ManagedActionDecision(
            status=ManagedActionDecisionStatus.DENIED,
            route=route,
            reasons=path_reasons,
        )
    if route.risk in DENIED_RISKS:
        return ManagedActionDecision(
            status=ManagedActionDecisionStatus.DENIED,
            route=route,
            reasons=[f"Risk is denied by policy: {route.risk.value}"],
        )
    if route.risk in APPROVAL_REQUIRED_RISKS or route.required_approvals:
        return ManagedActionDecision(
            status=ManagedActionDecisionStatus.APPROVAL_REQUIRED,
            route=route,
            reasons=[f"Risk requires approval: {route.risk.value}", *route.required_approvals],
            requires_human=True,
        )
    if route.risk in AUTO_ALLOWED_RISKS:
        return ManagedActionDecision(
            status=ManagedActionDecisionStatus.AUTO_ALLOWED,
            route=route,
            reasons=[f"Risk is auto-allowed by local policy: {route.risk.value}"],
        )
    return ManagedActionDecision(
        status=ManagedActionDecisionStatus.DENIED,
        route=route,
        reasons=[f"Risk is not recognized by policy: {route.risk.value}"],
    )


def _path_policy_reasons(route: ManagedActionRoute, project_root: Path) -> list[str]:
    reasons: list[str] = []
    for key in ("filename", "dirname"):
        value = route.normalized_arguments.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        candidate = Path(value)
        if candidate.is_absolute():
            reasons.append(f"{key} must be project-relative.")
            continue
        if any(part == ".." for part in candidate.parts):
            reasons.append(f"{key} must not traverse outside the project.")
        if candidate.parts and candidate.parts[0] in {".git", ".harness"}:
            reasons.append(f"{key} must not target {candidate.parts[0]}.")
        if is_secret_path(candidate):
            reasons.append(f"{key} is secret-like and cannot be managed automatically.")
        resolved = (project_root / candidate).resolve()
        try:
            resolved.relative_to(project_root.resolve())
        except ValueError:
            reasons.append(f"{key} resolves outside the project.")
    allowed_extensions = route.normalized_arguments.get("allowed_extensions")
    filename = route.normalized_arguments.get("filename")
    if isinstance(filename, str) and isinstance(allowed_extensions, list):
        if Path(filename).suffix not in {str(item) for item in allowed_extensions}:
            reasons.append(f"File extension is not allowed for this action: {Path(filename).suffix}")
    return reasons

