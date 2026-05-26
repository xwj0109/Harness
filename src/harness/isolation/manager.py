from __future__ import annotations

import difflib
import fnmatch
import hashlib
import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from harness.config import DEFAULT_CONTEXT_EXCLUDES, DEFAULT_ISOLATION_COPY_EXCLUDES
from harness.security import is_secret_path, sanitize_for_logging


class ActiveRepoDirtyError(RuntimeError):
    pass


TEXT_CLASSIFICATION = "text"
BINARY_CLASSIFICATION = "binary"
GENERATED_ARTIFACT_DIR_NAMES = {
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    "dist",
    "build",
}


@dataclass(frozen=True)
class ManifestEntry:
    path: str
    sha256: str
    size: int
    text_or_binary: str
    file_type: str
    is_symlink: bool
    text: str | None = field(default=None, repr=False, compare=False)

    def to_record(self) -> dict[str, str | int | bool]:
        return {
            "path": self.path,
            "sha256": self.sha256,
            "size": self.size,
            "text_or_binary": self.text_or_binary,
            "file_type": self.file_type,
            "is_symlink": self.is_symlink,
        }


@dataclass
class BaselineManifest:
    project_root: Path
    entries: dict[str, ManifestEntry]
    excluded_patterns: list[str]

    def write_json(self, path: Path) -> None:
        payload = {
            "project_root": str(self.project_root),
            "excluded_patterns": self.excluded_patterns,
            "entries": [entry.to_record() for entry in self.entries.values()],
        }
        path.write_text(json.dumps(sanitize_for_logging(payload), indent=2, sort_keys=True), encoding="utf-8")


@dataclass(frozen=True)
class FileChangeViolation:
    path: str
    kind: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {"path": self.path, "kind": self.kind, "reason": self.reason}


@dataclass
class DiffInspectionResult:
    changed_files: list[str]
    allowed_changed_files: list[str]
    unified_diff: str
    diff_stat: str
    violations: list[FileChangeViolation]
    ignored_generated_artifacts: list[str] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not self.violations


@dataclass
class IsolationWorkspace:
    active_project: Path
    path: Path
    strategy: str
    baseline_manifest: BaselineManifest
    active_pre_isolation_git_status: str
    agents_md_exists: bool
    warnings: list[str]
    cleanup_commands: list[list[str]] = field(default_factory=list)
    cleanup_status: str = "not_cleaned"

    def cleanup(self) -> None:
        errors: list[str] = []
        for command in self.cleanup_commands:
            result = subprocess.run(command, text=True, capture_output=True, timeout=60)
            if result.returncode != 0:
                errors.append((result.stderr or result.stdout).strip())
        if self.path.exists():
            try:
                shutil.rmtree(self.path)
            except Exception as exc:  # pragma: no cover - defensive cleanup reporting
                errors.append(str(exc))
        self.cleanup_status = "failed: " + "; ".join(errors) if errors else "cleaned"


class IsolationManager:
    def __init__(
        self,
        context_excludes: list[str] | None = None,
        copy_excludes: list[str] | None = None,
    ) -> None:
        self.context_excludes = list(context_excludes or DEFAULT_CONTEXT_EXCLUDES)
        self.copy_excludes = list(copy_excludes or DEFAULT_ISOLATION_COPY_EXCLUDES)
        self.excluded_patterns = _combined_patterns(self.context_excludes, self.copy_excludes)

    def create(self, project_root: Path, allow_dirty: bool = False) -> IsolationWorkspace:
        active_project = project_root.expanduser().resolve()
        warnings: list[str] = []
        agents_md_exists = (active_project / "AGENTS.md").exists()
        if not agents_md_exists:
            warnings.append("AGENTS.md is missing; recommend creating one before Codex edit runs.")
        baseline = create_baseline_manifest(active_project, self.excluded_patterns)
        git_repo = _is_git_repo(active_project)
        pre_status = _git_status_porcelain(active_project) if git_repo else "NOT_A_GIT_REPO"
        if git_repo and pre_status.strip() and not allow_dirty:
            raise ActiveRepoDirtyError("Active Git repo has uncommitted changes; explicit approval is required.")
        dirty_repo_copy_required = bool(git_repo and pre_status.strip() and allow_dirty)
        if dirty_repo_copy_required:
            warnings.append(
                "Active Git repo is dirty; isolated copy includes current workspace state and apply-back still requires freshness checks."
            )
        if git_repo and _has_valid_head(active_project) and not dirty_repo_copy_required:
            temp_path = Path(tempfile.mkdtemp(prefix="harness-isolation-")).resolve()
            created = self._try_git_worktree(active_project, temp_path)
            if created:
                return IsolationWorkspace(
                    active_project=active_project,
                    path=temp_path,
                    strategy="git_worktree",
                    baseline_manifest=baseline,
                    active_pre_isolation_git_status=pre_status,
                    agents_md_exists=agents_md_exists,
                    warnings=warnings,
                    cleanup_commands=[["git", "worktree", "remove", "--force", str(temp_path)]],
                )
            if temp_path.exists():
                shutil.rmtree(temp_path)
        copy_path = Path(tempfile.mkdtemp(prefix="harness-isolation-copy-")).resolve()
        _copy_project(active_project, copy_path, self.excluded_patterns)
        return IsolationWorkspace(
            active_project=active_project,
            path=copy_path,
            strategy="isolated_copy",
            baseline_manifest=baseline,
            active_pre_isolation_git_status=pre_status,
            agents_md_exists=agents_md_exists,
            warnings=warnings,
        )

    def _try_git_worktree(self, active_project: Path, destination: Path) -> bool:
        result = subprocess.run(
            ["git", "worktree", "add", "--detach", str(destination), "HEAD"],
            cwd=active_project,
            text=True,
            capture_output=True,
            timeout=60,
        )
        return result.returncode == 0


def create_baseline_manifest(project_root: Path, excluded_patterns: list[str] | None = None) -> BaselineManifest:
    root = project_root.expanduser().resolve()
    patterns = list(excluded_patterns or _combined_patterns(DEFAULT_CONTEXT_EXCLUDES, DEFAULT_ISOLATION_COPY_EXCLUDES))
    entries: dict[str, ManifestEntry] = {}
    for path in sorted(root.rglob("*")):
        if path == root:
            continue
        rel = path.relative_to(root).as_posix()
        if _is_generated_artifact_path(rel) or _is_policy_blocked_path(rel, patterns):
            continue
        if path.is_symlink() or not path.is_file():
            continue
        data = path.read_bytes()
        text = _decode_text(data)
        if text is None:
            continue
        entries[rel] = ManifestEntry(
            path=rel,
            sha256=_sha256(data),
            size=len(data),
            text_or_binary=TEXT_CLASSIFICATION,
            file_type="file",
            is_symlink=False,
            text=text,
        )
    return BaselineManifest(project_root=root, entries=entries, excluded_patterns=patterns)


def inspect_isolated_diff(isolated_workspace: Path, baseline_manifest: BaselineManifest) -> DiffInspectionResult:
    root = isolated_workspace.expanduser().resolve()
    violations: list[FileChangeViolation] = []
    changed_files: set[str] = set()
    allowed_changed_files: list[str] = []
    ignored_generated_artifacts: set[str] = set()
    diff_parts: list[str] = []
    seen_paths = _walk_existing_paths(root)
    for rel in sorted(seen_paths - set(baseline_manifest.entries)):
        if rel == ".git" or rel.startswith(".git/"):
            continue
        if _is_generated_artifact_path(rel):
            ignored_generated_artifacts.add(rel)
            continue
        changed_files.add(rel)
        violations.append(_violation(rel, "creation", "File creation is not supported in this phase."))
    for rel, entry in sorted(baseline_manifest.entries.items()):
        if _is_generated_artifact_path(rel):
            ignored_generated_artifacts.add(rel)
            continue
        path = root / rel
        if not path.exists() and not path.is_symlink():
            changed_files.add(rel)
            violations.append(_violation(rel, "deletion", "File deletion is not supported in this phase."))
            continue
        if path.is_symlink():
            changed_files.add(rel)
            violations.append(_violation(rel, "symlink", "Symlink changes are not supported in this phase."))
            continue
        if not path.is_file():
            changed_files.add(rel)
            violations.append(_violation(rel, "file_type", "Target changed from a regular file."))
            continue
        data = path.read_bytes()
        current_hash = _sha256(data)
        if current_hash == entry.sha256:
            continue
        changed_files.add(rel)
        if _is_policy_blocked_path(rel, baseline_manifest.excluded_patterns):
            violations.append(_violation(rel, "blocked_path", "Blocked-path changes are not allowed."))
            continue
        current_text = _decode_text(data)
        if current_text is None:
            violations.append(_violation(rel, "binary", "Binary changes are not supported in this phase."))
            continue
        allowed_changed_files.append(rel)
        diff_parts.append(_unified_diff(rel, entry.text or "", current_text))
    unified_diff = "".join(diff_parts)
    return DiffInspectionResult(
        changed_files=sorted(changed_files),
        allowed_changed_files=allowed_changed_files,
        unified_diff=unified_diff,
        diff_stat=_diff_stat(unified_diff),
        violations=violations,
        ignored_generated_artifacts=sorted(ignored_generated_artifacts),
    )


def _copy_project(source: Path, destination: Path, excluded_patterns: list[str]) -> None:
    def ignore(directory: str, names: list[str]) -> set[str]:
        ignored: set[str] = set()
        base = Path(directory)
        for name in names:
            rel = (base / name).relative_to(source).as_posix()
            if _is_policy_blocked_path(rel, excluded_patterns):
                ignored.add(name)
        return ignored

    shutil.copytree(source, destination, ignore=ignore, symlinks=True, dirs_exist_ok=True)


def _walk_existing_paths(root: Path) -> set[str]:
    paths: set[str] = set()
    for path in root.rglob("*"):
        rel = path.relative_to(root).as_posix()
        if rel == ".git" or rel.startswith(".git/"):
            continue
        if path.is_file() or path.is_symlink():
            paths.add(rel)
    return paths


def _is_generated_artifact_path(relative_path: str) -> bool:
    rel = relative_path.strip("/")
    if not rel:
        return False
    path = Path(rel)
    parts = path.parts
    name = path.name
    if name == ".DS_Store":
        return True
    if name.endswith(".pyc"):
        return True
    if any(part in GENERATED_ARTIFACT_DIR_NAMES for part in parts):
        return True
    return any(part.endswith(".egg-info") for part in parts)


def _unified_diff(relative_path: str, old_text: str, new_text: str) -> str:
    return "".join(
        difflib.unified_diff(
            old_text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=f"a/{relative_path}",
            tofile=f"b/{relative_path}",
        )
    )


def _diff_stat(unified_diff: str) -> str:
    stats: dict[str, tuple[int, int]] = {}
    current: str | None = None
    for line in unified_diff.splitlines():
        if line.startswith("+++ b/"):
            current = line[6:]
            stats.setdefault(current, (0, 0))
            continue
        if current and line.startswith("+") and not line.startswith("+++"):
            added, removed = stats[current]
            stats[current] = (added + 1, removed)
        elif current and line.startswith("-") and not line.startswith("---"):
            added, removed = stats[current]
            stats[current] = (added, removed + 1)
    if not stats:
        return ""
    lines = []
    total_added = 0
    total_removed = 0
    for path, (added, removed) in stats.items():
        total_added += added
        total_removed += removed
        lines.append(f" {path} | {added + removed} {'+' * added}{'-' * removed}")
    files = len(stats)
    lines.append(f" {files} file{'s' if files != 1 else ''} changed, {total_added} insertion{'s' if total_added != 1 else ''}(+), {total_removed} deletion{'s' if total_removed != 1 else ''}(-)")
    return "\n".join(lines) + "\n"


def _is_git_repo(path: Path) -> bool:
    result = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=path,
        text=True,
        capture_output=True,
        timeout=30,
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


def _has_valid_head(path: Path) -> bool:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=path,
        text=True,
        capture_output=True,
        timeout=30,
    )
    return result.returncode == 0


def _git_status_porcelain(path: Path) -> str:
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=path,
        text=True,
        capture_output=True,
        timeout=30,
    )
    if result.returncode != 0:
        return f"GIT_STATUS_UNAVAILABLE: {(result.stderr or result.stdout).strip()}"
    return result.stdout


def _decode_text(data: bytes) -> str | None:
    if b"\x00" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _combined_patterns(*groups: Iterable[str]) -> list[str]:
    patterns: list[str] = []
    for group in groups:
        for pattern in group:
            if pattern not in patterns:
                patterns.append(pattern)
    for pattern in [".env*", "secrets/", ".git/", ".harness/", ".venv/", "node_modules/", "data/raw/"]:
        if pattern not in patterns:
            patterns.append(pattern)
    return patterns


def _is_policy_blocked_path(relative_path: str, patterns: list[str]) -> bool:
    rel = relative_path.strip("/")
    if not rel:
        return False
    if is_secret_path(Path(rel)):
        return True
    for pattern in patterns:
        pat = pattern.strip()
        if not pat:
            continue
        if pat.endswith("/"):
            prefix = pat.strip("/")
            if rel == prefix or rel.startswith(prefix + "/"):
                return True
        elif fnmatch.fnmatch(rel, pat) or Path(rel).match(pat):
            return True
    return False


def _violation(path: str, kind: str, reason: str) -> FileChangeViolation:
    if _is_policy_blocked_path(path, _combined_patterns(DEFAULT_CONTEXT_EXCLUDES, DEFAULT_ISOLATION_COPY_EXCLUDES)):
        return FileChangeViolation(path=path, kind="blocked_path", reason="Blocked-path changes are not allowed.")
    return FileChangeViolation(path=path, kind=kind, reason=reason)
