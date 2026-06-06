from __future__ import annotations

import os
import fnmatch
from pathlib import Path


class PathSecurityError(ValueError):
    pass


def resolve_project_root(project: str | Path) -> Path:
    return Path(project).expanduser().resolve()


def resolve_under_project(project_root: Path, candidate: str | Path) -> Path:
    raw = Path(candidate)
    target = raw.expanduser() if raw.is_absolute() else project_root / raw
    resolved = target.resolve()
    root = project_root.resolve()
    if resolved != root and root not in resolved.parents:
        raise PathSecurityError(f"Path escapes project root: {candidate}")
    return resolved


def reject_symlink_components_under_project(project_root: Path, candidate: str | Path) -> None:
    """Reject configured project paths that traverse symlink components.

    `resolve_under_project` ensures the final resolved path stays under the
    project root. Extension-like sources also need a stronger invariant: the
    configured path itself must not pass through a symlink, even when that
    symlink eventually resolves back inside the project. This keeps file-backed
    skill/resource loads auditable to the literal configured project path.
    """
    root = project_root.resolve()
    raw = Path(candidate).expanduser()
    target = raw if raw.is_absolute() else root / raw
    lexical_target = Path(os.path.abspath(os.fspath(target)))
    try:
        relative = lexical_target.relative_to(root)
    except ValueError as exc:
        raise PathSecurityError(f"Path escapes project root: {candidate}") from exc
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            rel = current.relative_to(root).as_posix()
            raise PathSecurityError(f"Path contains symlink component: {rel}")


def relative_to_project(project_root: Path, path: Path) -> str:
    return path.resolve().relative_to(project_root.resolve()).as_posix()


def is_excluded_relative(relative_path: str, patterns: list[str]) -> bool:
    rel = relative_path.strip("/")
    for pattern in patterns:
        pat = pattern.strip()
        if not pat:
            continue
        if pat.endswith("/"):
            prefix = pat.strip("/")
            parts = Path(rel).parts
            parent_dirs = parts[:-1]
            if (
                rel == prefix
                or rel.startswith(prefix + "/")
                or any(fnmatch.fnmatch(part, prefix) for part in parent_dirs)
            ):
                return True
        elif _simple_match(rel, pat):
            return True
    return False


def _simple_match(relative_path: str, pattern: str) -> bool:
    return Path(relative_path).match(pattern) or relative_path == pattern.strip("/")
