from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

from harness.action_router import (
    ManagedActionDecision,
    ManagedActionDecisionStatus,
    ManagedActionRisk,
    ManagedActionRoute,
    ManagedActionSandboxAssessment,
    ManagedActionSandboxStatus,
)
from harness.governance.protected_paths import protected_apply_path_match
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
            sandbox_assessment=_not_run_sandbox_assessment(route, "approval_required_actions_are_not_preflighted"),
        )
    if route.risk in AUTO_ALLOWED_RISKS:
        sandbox_assessment = assess_managed_action_in_sandbox(route, project_root)
        if sandbox_assessment.dangerous:
            return ManagedActionDecision(
                status=ManagedActionDecisionStatus.DENIED,
                route=route,
                reasons=["Sandbox preflight classified the action as dangerous.", *sandbox_assessment.reasons],
                sandbox_assessment=sandbox_assessment,
            )
        return ManagedActionDecision(
            status=ManagedActionDecisionStatus.AUTO_ALLOWED,
            route=route,
            reasons=[f"Risk is auto-allowed by local policy: {route.risk.value}"],
            sandbox_assessment=sandbox_assessment,
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
        if "\\" in value:
            reasons.append(f"{key} must use forward-slash project-relative paths.")
        candidate = Path(value)
        if candidate.is_absolute():
            reasons.append(f"{key} must be project-relative.")
            continue
        if any(part == ".." for part in candidate.parts):
            reasons.append(f"{key} must not traverse outside the project.")
        protected_match = protected_apply_path_match(candidate)
        if protected_match is not None:
            reasons.append(f"{key} targets protected Harness governance path: {protected_match.path}")
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


def assess_managed_action_in_sandbox(route: ManagedActionRoute, project_root: Path) -> ManagedActionSandboxAssessment:
    """Classify the real executor target using an isolated filesystem preflight."""
    reasons: list[str] = []
    expected_paths: list[str] = []
    with TemporaryDirectory(prefix="harness-managed-action-") as sandbox_dir:
        sandbox_root = Path(sandbox_dir)
        try:
            expected_paths = _simulate_managed_action(route, project_root.resolve(), sandbox_root)
        except (OSError, ValueError) as exc:
            reasons.append(str(exc))
    if reasons:
        return ManagedActionSandboxAssessment(
            status=ManagedActionSandboxStatus.DANGEROUS,
            executor=route.executor,
            dangerous=True,
            reasons=reasons,
            expected_paths=expected_paths,
        )
    return ManagedActionSandboxAssessment(
        status=ManagedActionSandboxStatus.SAFE,
        executor=route.executor,
        dangerous=False,
        reasons=["Sandbox preflight completed without dangerous effects."],
        expected_paths=expected_paths,
    )


def _not_run_sandbox_assessment(route: ManagedActionRoute, reason: str) -> ManagedActionSandboxAssessment:
    return ManagedActionSandboxAssessment(
        status=ManagedActionSandboxStatus.NOT_RUN,
        executor=route.executor,
        dangerous=False,
        reasons=[reason],
    )


def _simulate_managed_action(route: ManagedActionRoute, project_root: Path, sandbox_root: Path) -> list[str]:
    if route.executor in {"create_empty_file", "create_file_with_content"}:
        requested = str(
            route.normalized_arguments.get("filename")
            or route.normalized_arguments.get("default_filename")
            or "scratch.txt"
        )
        target = _sandbox_target(project_root, sandbox_root, requested)
        _ensure_sandbox_parent_directory(target, requested)
        _mirror_existing_regular_file(project_root / requested, target)
        candidate = _next_available_path(target)
        text = str(route.normalized_arguments.get("text") or "") if route.executor == "create_file_with_content" else ""
        candidate.write_text(text if not text or text.endswith("\n") else f"{text}\n", encoding="utf-8")
        return [str(_project_relative_from_sandbox(sandbox_root, candidate))]
    if route.executor == "create_directory":
        dirname = str(route.normalized_arguments.get("dirname") or "new-folder")
        target = _sandbox_target(project_root, sandbox_root, dirname)
        _mirror_existing_regular_file(project_root / dirname, target)
        if target.exists() and not target.is_dir():
            raise ValueError(f"Sandbox preflight found existing non-directory target: {dirname}")
        target.mkdir(parents=False, exist_ok=True)
        return [str(_project_relative_from_sandbox(sandbox_root, target))]
    if route.executor in {"write_file", "write_note_file"}:
        filename = str(route.normalized_arguments.get("filename") or ("notes.md" if route.executor == "write_note_file" else "scratch.md"))
        target = _sandbox_target(project_root, sandbox_root, filename)
        _ensure_sandbox_parent_directory(target, filename)
        _mirror_existing_writable_file(project_root / filename, target, filename)
        existing = target.read_text(encoding="utf-8") if target.exists() else ""
        text = str(route.normalized_arguments.get("text") or "")
        separator = "" if not existing or existing.endswith("\n") else "\n"
        target.write_text(f"{existing}{separator}{text}\n", encoding="utf-8")
        return [str(_project_relative_from_sandbox(sandbox_root, target))]
    raise ValueError(f"Sandbox preflight does not support executor: {route.executor}")


def _sandbox_target(project_root: Path, sandbox_root: Path, relative_path: str) -> Path:
    candidate = Path(relative_path)
    if candidate.is_absolute() or any(part == ".." for part in candidate.parts):
        raise ValueError(f"Sandbox preflight refused unsafe relative path: {relative_path}")
    real_target = (project_root / candidate).resolve()
    try:
        real_target.relative_to(project_root)
    except ValueError as exc:
        raise ValueError(f"Sandbox preflight target resolves outside the project: {relative_path}") from exc
    _ensure_project_parent_directories(project_root, real_target, relative_path)
    return sandbox_root / candidate


def _ensure_project_parent_directories(project_root: Path, real_target: Path, relative_path: str) -> None:
    try:
        parent_relative = real_target.parent.relative_to(project_root)
    except ValueError as exc:
        raise ValueError(f"Sandbox preflight parent resolves outside the project: {relative_path}") from exc
    current = project_root
    for part in parent_relative.parts:
        current = current / part
        if current.exists() and not current.is_dir():
            raise ValueError(f"Sandbox preflight found non-directory parent for target: {relative_path}")


def _ensure_sandbox_parent_directory(path: Path, relative_path: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except FileExistsError as exc:
        raise ValueError(f"Sandbox preflight found non-directory parent for target: {relative_path}") from exc


def _mirror_existing_regular_file(real_path: Path, sandbox_path: Path) -> None:
    if not real_path.exists():
        return
    if real_path.is_dir():
        sandbox_path.mkdir(parents=True, exist_ok=True)
        return
    if not real_path.is_file():
        raise ValueError(f"Sandbox preflight found unsupported filesystem target: {real_path.name}")
    sandbox_path.parent.mkdir(parents=True, exist_ok=True)
    sandbox_path.write_bytes(real_path.read_bytes())


def _mirror_existing_writable_file(real_path: Path, sandbox_path: Path, relative_path: str) -> None:
    if not real_path.exists():
        return
    if real_path.is_dir():
        raise ValueError(f"Sandbox preflight found existing directory where file content would be written: {relative_path}")
    if not real_path.is_file():
        raise ValueError(f"Sandbox preflight found unsupported filesystem target: {real_path.name}")
    sandbox_path.parent.mkdir(parents=True, exist_ok=True)
    sandbox_path.write_bytes(real_path.read_bytes())


def _next_available_path(path: Path) -> Path:
    candidate = path
    base = path.stem or "scratch"
    suffix = path.suffix
    index = 2
    while candidate.exists():
        candidate = path.parent / f"{base}-{index}{suffix}"
        index += 1
    return candidate


def _project_relative_from_sandbox(sandbox_root: Path, path: Path) -> Path:
    return path.resolve().relative_to(sandbox_root.resolve())
