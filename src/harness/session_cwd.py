from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from harness.paths import is_excluded_relative
from harness.security import is_secret_path


DEFAULT_SESSION_CWD = "."


class CwdResolutionError(ValueError):
    def __init__(self, message: str, *, action: str, target: str, error_type: str) -> None:
        super().__init__(message)
        self.message = message
        self.action = action
        self.target = target
        self.error_type = error_type


class SessionCwd(BaseModel):
    schema_version: str = "harness.session_cwd/v1"
    cwd: str = DEFAULT_SESSION_CWD
    resolved_abs_path: str


class CwdResolution(BaseModel):
    schema_version: str = "harness.cwd_resolution/v1"
    normalized_project_relative_cwd: str
    resolved_abs_path: str
    permission_status: str = "allow"
    blocked_reason: str | None = None


@dataclass(frozen=True)
class CwdResolver:
    project_root: Path
    context_excludes: list[str]

    @property
    def root(self) -> Path:
        return self.project_root.expanduser().resolve()

    def current(self, session_cwd: str | None, *, allow_excluded: bool = False) -> CwdResolution:
        return self.resolve_cwd(session_cwd=session_cwd, call_cwd=None, allow_excluded=allow_excluded)

    def resolve_cwd(
        self,
        *,
        session_cwd: str | None,
        call_cwd: str | None,
        allow_excluded: bool = False,
        action: str = "cwd",
    ) -> CwdResolution:
        requested = _clean_cwd(call_cwd if call_cwd not in {None, ""} else session_cwd)
        return self._resolve_directory(requested, base_cwd=DEFAULT_SESSION_CWD, allow_excluded=allow_excluded, action=action)

    def resolve_cd(
        self,
        *,
        session_cwd: str | None,
        requested_path: str,
        allow_excluded: bool = False,
    ) -> CwdResolution:
        base = _clean_cwd(session_cwd)
        return self._resolve_directory(str(requested_path or "."), base_cwd=base, allow_excluded=allow_excluded, action="cd")

    def resolve_tool_path(
        self,
        *,
        session_cwd: str | None,
        call_cwd: str | None,
        requested_path: str,
        action: str,
        allow_excluded: bool = False,
    ) -> Path:
        cwd = self.resolve_cwd(
            session_cwd=session_cwd,
            call_cwd=call_cwd,
            allow_excluded=allow_excluded,
            action=action,
        )
        path = self._candidate_from(cwd_abs=Path(cwd.resolved_abs_path), requested=str(requested_path or "."))
        resolved = path.expanduser().resolve()
        rel = self._relative_or_raise(resolved, requested=str(requested_path or "."), action=action)
        self._check_path_policy(resolved, rel, action=action, allow_excluded=allow_excluded)
        return resolved

    def _resolve_directory(
        self,
        requested: str,
        *,
        base_cwd: str,
        allow_excluded: bool,
        action: str,
    ) -> CwdResolution:
        base = self._candidate_from(cwd_abs=self.root, requested=base_cwd)
        base_abs = base.expanduser().resolve()
        base_rel = self._relative_or_raise(base_abs, requested=base_cwd, action=action)
        self._check_path_policy(base_abs, base_rel, action=action, allow_excluded=allow_excluded)
        target = self._candidate_from(cwd_abs=base_abs, requested=requested)
        resolved = target.expanduser().resolve()
        rel = self._relative_or_raise(resolved, requested=requested, action=action)
        self._check_path_policy(resolved, rel, action=action, allow_excluded=allow_excluded)
        if not resolved.exists():
            raise CwdResolutionError(
                f"Directory does not exist: {requested}",
                action=action,
                target=rel,
                error_type="missing",
            )
        if not resolved.is_dir():
            raise CwdResolutionError(
                f"Path is not a directory: {requested}",
                action=action,
                target=rel,
                error_type="not_directory",
            )
        return CwdResolution(
            normalized_project_relative_cwd=rel or DEFAULT_SESSION_CWD,
            resolved_abs_path=str(resolved),
        )

    def _candidate_from(self, *, cwd_abs: Path, requested: str) -> Path:
        raw = Path(requested).expanduser()
        return raw if raw.is_absolute() else cwd_abs / raw

    def _relative_or_raise(self, path: Path, *, requested: str, action: str) -> str:
        root = self.root
        try:
            rel_path = path.relative_to(root)
        except ValueError as exc:
            raise CwdResolutionError(
                f"Path escapes project root: {requested}",
                action=action,
                target=requested,
                error_type="path_security",
            ) from exc
        rel = rel_path.as_posix()
        return rel or DEFAULT_SESSION_CWD

    def _check_path_policy(self, path: Path, rel: str, *, action: str, allow_excluded: bool) -> None:
        if is_secret_path(path):
            raise CwdResolutionError(
                f"Blocked secret-like path: {path.name}",
                action=action,
                target=rel,
                error_type="secret_path",
            )
        if is_excluded_relative(rel, self.context_excludes) and not allow_excluded:
            raise CwdResolutionError(
                f"Path is excluded from context: {rel}",
                action=action,
                target=rel,
                error_type="context_excluded",
            )


def session_cwd_from_metadata(metadata: dict[str, Any] | None) -> str:
    value = (metadata or {}).get("cwd")
    if not isinstance(value, str) or not value.strip():
        return DEFAULT_SESSION_CWD
    return _normalize_relative_cwd(value)


def session_cwd_payload(project_root: Path, metadata: dict[str, Any] | None, context_excludes: list[str]) -> dict[str, Any]:
    cwd = session_cwd_from_metadata(metadata)
    resolver = CwdResolver(project_root=project_root, context_excludes=context_excludes)
    resolved = resolver.current(cwd, allow_excluded=True)
    return SessionCwd(cwd=resolved.normalized_project_relative_cwd, resolved_abs_path=resolved.resolved_abs_path).model_dump(mode="json")


def _clean_cwd(value: str | None) -> str:
    if not isinstance(value, str) or not value.strip():
        return DEFAULT_SESSION_CWD
    return _normalize_relative_cwd(value)


def _normalize_relative_cwd(value: str) -> str:
    stripped = value.strip()
    return DEFAULT_SESSION_CWD if stripped in {"", "./"} else stripped


def relative_tool_path(cwd: str, path: str) -> str:
    raw = Path(path)
    if raw.is_absolute():
        return path
    base = Path(_clean_cwd(cwd))
    joined = base / raw
    normalized = joined.as_posix()
    return DEFAULT_SESSION_CWD if normalized in {"", "."} else normalized
