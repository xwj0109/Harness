from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath


PROTECTED_APPLY_PATTERNS: tuple[str, ...] = (
    ".git/**",
    ".harness/**",
    ".agents/**",
    ".codex/**",
    ".github/**",
    "pyproject.toml",
    "uv.lock",
    "poetry.lock",
    "requirements*.txt",
    "src/harness/action_policy.py",
    "src/harness/approvals.py",
    "src/harness/backends/**",
    "src/harness/builtin_specs/**",
    "src/harness/core_service.py",
    "src/harness/daemon_adapters.py",
    "src/harness/execution.py",
    "src/harness/governance/**",
    "src/harness/policy.py",
    "src/harness/sandbox/**",
    "src/harness/security.py",
    "src/harness/security_explanations.py",
    "src/harness/session_tools.py",
    "docs/command_catalog.md",
    "docs/operator_guide.md",
    "docs/plans/**",
    "docs/session_tool_catalog.md",
    "docs/smoke_checklist.md",
)


@dataclass(frozen=True)
class ProtectedPathMatch:
    path: str
    pattern: str
    reason: str = "protected_apply_path"

    def to_dict(self) -> dict[str, str]:
        return {"path": self.path, "pattern": self.pattern, "reason": self.reason}


def is_protected_apply_path(path: str | Path) -> bool:
    return protected_apply_path_match(path) is not None


def protected_apply_path_match(path: str | Path) -> ProtectedPathMatch | None:
    normalized = _normalize_project_relative_path(path)
    if not normalized:
        return None
    candidate = PurePosixPath(normalized)
    for pattern in PROTECTED_APPLY_PATTERNS:
        if _pattern_matches(candidate, normalized, pattern):
            return ProtectedPathMatch(path=normalized, pattern=pattern)
    return None


def _pattern_matches(candidate: PurePosixPath, normalized: str, pattern: str) -> bool:
    if pattern.endswith("/**"):
        prefix = pattern[:-3].rstrip("/")
        return normalized == prefix or normalized.startswith(f"{prefix}/")
    if any(char in pattern for char in "*?[]"):
        return candidate.match(pattern)
    return normalized == pattern


def _normalize_project_relative_path(path: str | Path) -> str:
    raw = path.as_posix() if isinstance(path, Path) else str(path)
    normalized = raw.strip().replace("\\", "/")
    if normalized.startswith("a/") or normalized.startswith("b/"):
        normalized = normalized[2:]
    while normalized.startswith("./"):
        normalized = normalized[2:]
    normalized = "/".join(part for part in normalized.split("/") if part and part != ".")
    return normalized
