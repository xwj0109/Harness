from __future__ import annotations

import re
import shlex
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from harness.config import DEFAULT_CONTEXT_EXCLUDES
from harness.session_cwd import CwdResolutionError, CwdResolver


NATURAL_LANGUAGE_ROUTE_SCHEMA_VERSION = "harness.natural_language_route/v1"


class NaturalLanguageIntent(str, Enum):
    DIRECT_SLASH_COMMAND = "direct_slash_command"
    PROJECT_SWITCH = "project_switch"
    WORKSPACE_SWITCH = "workspace_switch"
    CWD_CHANGE = "cwd_change"
    PWD = "pwd"
    REPO_ROOT = "repo_root"
    GIT_DIFF = "git_diff"
    READ_FILE = "read_file"
    SEARCH_TEXT = "search_text"
    RUN_TESTS = "run_tests"
    UNSUPPORTED = "unsupported"


class NaturalLanguageRoute(BaseModel):
    schema_version: str = NATURAL_LANGUAGE_ROUTE_SCHEMA_VERSION
    ok: bool = True
    input: str
    intent: NaturalLanguageIntent
    tool_id: str | None = None
    tool_arguments: dict[str, Any] = Field(default_factory=dict)
    target_path: str | None = None
    target_project_root: str | None = None
    message: str | None = None
    blocked_reason: str | None = None
    command: str | None = None
    proposed_command: str | None = None


def route_natural_language(
    text: str,
    project_root: Path,
    *,
    session_cwd: str = ".",
    context_excludes: list[str] | None = None,
) -> NaturalLanguageRoute:
    raw = text.strip()
    normalized = _normalize(raw)
    root = project_root.expanduser().resolve()
    excludes = list(context_excludes or DEFAULT_CONTEXT_EXCLUDES)

    if not raw:
        return _unsupported(raw)
    if raw.startswith("/"):
        return NaturalLanguageRoute(
            input=raw,
            intent=NaturalLanguageIntent.DIRECT_SLASH_COMMAND,
            command=raw.split()[0],
        )
    if normalized == "pwd":
        return NaturalLanguageRoute(input=raw, intent=NaturalLanguageIntent.PWD, tool_id="pwd")
    if normalized in {"repo root", "go back to repo root", "go to repo root", "back to repo root"}:
        return NaturalLanguageRoute(
            input=raw,
            intent=NaturalLanguageIntent.REPO_ROOT,
            tool_id="cd",
            tool_arguments={"path": str(root), "actor": "operator"},
            target_path=".",
            command="cd",
        )
    if _is_git_diff_request(normalized):
        return NaturalLanguageRoute(input=raw, intent=NaturalLanguageIntent.GIT_DIFF, tool_id="git-diff")

    nav = _parse_navigation(raw)
    if nav is not None:
        command, target = nav
        return _route_path_navigation(
            raw,
            root,
            session_cwd=session_cwd,
            context_excludes=excludes,
            command=command,
            target=target,
        )

    run_tests = _parse_run_tests(normalized)
    if run_tests is not None:
        command = _test_command_for(run_tests, root)
        return NaturalLanguageRoute(
            input=raw,
            intent=NaturalLanguageIntent.RUN_TESTS,
            tool_id="shell",
            tool_arguments={"command": command, "timeout_seconds": 120},
            message="Shell approval is required before tests can run.",
            proposed_command=command,
        )

    search = _parse_search(raw)
    if search is not None:
        pattern, regex = search
        return NaturalLanguageRoute(
            input=raw,
            intent=NaturalLanguageIntent.SEARCH_TEXT,
            tool_id="grep",
            tool_arguments={"pattern": pattern, "path": ".", "regex": regex, "limit": 80},
        )

    read_target = _parse_read(raw, root, session_cwd=session_cwd, context_excludes=excludes)
    if read_target is not None:
        return read_target

    return _unsupported(raw)


def _route_path_navigation(
    raw: str,
    project_root: Path,
    *,
    session_cwd: str,
    context_excludes: list[str],
    command: str,
    target: str,
) -> NaturalLanguageRoute:
    target_abs = _candidate_path(project_root, target)
    project_candidate = _detect_project_root(target_abs)
    if project_candidate is not None:
        intent = NaturalLanguageIntent.WORKSPACE_SWITCH if command == "workspace" else NaturalLanguageIntent.PROJECT_SWITCH
        return NaturalLanguageRoute(
            input=raw,
            intent=intent,
            target_path=str(target_abs),
            target_project_root=str(project_candidate),
            command=command,
            proposed_command=f"/{command} {project_candidate}",
        )

    resolver = CwdResolver(project_root=project_root, context_excludes=context_excludes)
    try:
        resolved = resolver.resolve_cd(session_cwd=session_cwd, requested_path=target)
    except CwdResolutionError as exc:
        if exc.error_type == "path_security":
            return NaturalLanguageRoute(
                ok=False,
                input=raw,
                intent=NaturalLanguageIntent.PROJECT_SWITCH,
                target_path=str(target_abs),
                message=(
                    "That path is outside the active project and is not an initialized Harness project. "
                    "Initialize it or use an explicit project switch before running session tools there."
                ),
                blocked_reason="outside_project",
                command=command,
                proposed_command=f"/project {target_abs}",
            )
        return NaturalLanguageRoute(
            ok=False,
            input=raw,
            intent=NaturalLanguageIntent.CWD_CHANGE,
            target_path=target,
            message=exc.message,
            blocked_reason=exc.error_type,
            command=command,
        )

    return NaturalLanguageRoute(
        input=raw,
        intent=NaturalLanguageIntent.CWD_CHANGE,
        tool_id="cd",
        tool_arguments={"path": target, "actor": "operator"},
        target_path=resolved.normalized_project_relative_cwd,
        command=command,
        proposed_command=f"/cd {resolved.normalized_project_relative_cwd}",
    )


def _parse_navigation(raw: str) -> tuple[str, str] | None:
    patterns = [
        (r"^open\s+project\s+(.+)$", "project"),
        (r"^use\s+workspace\s+(.+)$", "workspace"),
        (r"^switch\s+to\s+(.+)$", "project"),
        (r"^move\s+to\s+(.+)$", "cd"),
        (r"^go\s+to\s+(.+)$", "cd"),
        (r"^cd(?:\s+(.+))?$", "cd"),
    ]
    for pattern, command in patterns:
        match = re.match(pattern, raw.strip(), flags=re.IGNORECASE)
        if match:
            target = _strip_quotes((match.group(1) or ".").strip())
            return command, target
    return None


def _parse_run_tests(normalized: str) -> str | None:
    match = re.match(r"^run(?:\s+the)?(?:\s+(.+?))?\s+tests?$", normalized)
    if not match:
        return None
    name = (match.group(1) or "").strip()
    return "" if name in {"", "the"} else name


def _test_command_for(name: str | None, project_root: Path) -> str:
    normalized = _normalize(name or "")
    known = {
        "session tool": "tests/test_session_tools.py",
        "session tools": "tests/test_session_tools.py",
        "session_tool": "tests/test_session_tools.py",
        "local server": "tests/test_local_server.py",
        "chat model": "tests/test_chat_model.py",
        "migration": "tests/test_migrations_runner.py",
        "migrations": "tests/test_migrations_runner.py",
    }
    target = known.get(normalized)
    if target is not None:
        return f"python3 -m pytest {target} -q"
    if not normalized:
        return "python3 -m pytest -q"
    slug = re.sub(r"[^a-z0-9]+", "_", normalized).strip("_")
    for candidate in (project_root / "tests" / f"test_{slug}.py", project_root / "tests" / f"{slug}.py"):
        if candidate.exists():
            return f"python3 -m pytest {candidate.relative_to(project_root).as_posix()} -q"
    return f"python3 -m pytest -q -k {shlex.quote(normalized)}"


def _parse_search(raw: str) -> tuple[str, bool] | None:
    stripped = raw.strip()
    match = re.match(r"^search\s+for\s+(.+)$", stripped, flags=re.IGNORECASE)
    if match:
        return _strip_quotes(match.group(1).strip()), False
    match = re.match(r"^find\s+where\s+(.+)$", stripped, flags=re.IGNORECASE)
    if not match:
        return None
    term = match.group(1).strip()
    term = re.sub(r"\s+(is|are)\s+(implemented|handled|defined|created|called|wired)\??$", "", term, flags=re.IGNORECASE)
    words = [word for word in re.findall(r"[A-Za-z0-9_./-]+", term) if len(word) > 1]
    if not words:
        return _strip_quotes(term), False
    if len(words) == 1:
        return words[0], False
    return "|".join(re.escape(word) for word in words[:6]), True


def _parse_read(
    raw: str,
    project_root: Path,
    *,
    session_cwd: str,
    context_excludes: list[str],
) -> NaturalLanguageRoute | None:
    match = re.match(r"^read\s+(.+)$", raw.strip(), flags=re.IGNORECASE)
    if not match:
        return None
    target = _strip_quotes(match.group(1).strip())
    if not _looks_like_path(target, project_root, session_cwd=session_cwd):
        return None
    resolver = CwdResolver(project_root=project_root, context_excludes=context_excludes)
    try:
        resolver.resolve_tool_path(session_cwd=session_cwd, call_cwd=None, requested_path=target, action="read")
    except CwdResolutionError as exc:
        return NaturalLanguageRoute(
            ok=False,
            input=raw,
            intent=NaturalLanguageIntent.READ_FILE,
            tool_id="read",
            tool_arguments={"path": target},
            target_path=target,
            message=exc.message,
            blocked_reason=exc.error_type,
        )
    return NaturalLanguageRoute(
        input=raw,
        intent=NaturalLanguageIntent.READ_FILE,
        tool_id="read",
        tool_arguments={"path": target},
        target_path=target,
    )


def _looks_like_path(target: str, project_root: Path, *, session_cwd: str) -> bool:
    if target.startswith(("/", "./", "../", "~")) or "/" in target or "." in Path(target).name:
        return True
    candidate = _candidate_path(project_root / session_cwd, target)
    return candidate.exists()


def _is_git_diff_request(normalized: str) -> bool:
    return normalized in {
        "show diff",
        "show the diff",
        "show me the diff",
        "what changed",
        "what changed?",
        "show me what changed",
        "show me what changed?",
    }


def _candidate_path(base: Path, target: str) -> Path:
    raw = Path(target).expanduser()
    return raw.resolve() if raw.is_absolute() else (base / raw).resolve()


def _detect_project_root(path: Path) -> Path | None:
    if path.name == ".harness" and path.is_dir():
        return path.parent.resolve()
    if path.is_dir() and (path / ".harness").exists():
        return path.resolve()
    return None


def _strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _normalize(value: str) -> str:
    return " ".join(value.strip().lower().split())


def _unsupported(raw: str) -> NaturalLanguageRoute:
    return NaturalLanguageRoute(input=raw, intent=NaturalLanguageIntent.UNSUPPORTED, ok=False)
