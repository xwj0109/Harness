from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from harness.paths import relative_to_project, resolve_under_project
from harness.security import sanitize_for_logging


COMMAND_CATALOG_SCHEMA_VERSION = "harness.commands/v1"
COMMAND_ACTION_SCHEMA_VERSION = "harness.command_action/v1"

_COMMAND_DIRS = (".harness/commands", ".opencode/command")
_COMMAND_SUFFIXES = {".md", ".txt", ".yaml", ".yml"}


def build_command_catalog(project_root: Path) -> dict[str, Any]:
    commands = []
    for base in _COMMAND_DIRS:
        directory = project_root / base
        if not directory.exists() or not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in _COMMAND_SUFFIXES:
                continue
            commands.append(_command_from_file(project_root, path, base))
    return {
        "schema_version": COMMAND_CATALOG_SCHEMA_VERSION,
        "ok": True,
        "commands": commands,
        "directories": list(_COMMAND_DIRS),
        "contents_included": False,
        "execution_supported": False,
        "filesystem_modified": False,
        "process_started": False,
        "permission_granting": False,
    }


def command_action_unsupported(action: str, command_id: str | None, body: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "schema_version": COMMAND_ACTION_SCHEMA_VERSION,
        "ok": False,
        "action": action,
        "command_id": command_id,
        "requested": sanitize_for_logging(body or {}),
        "error": "User-defined command execution is not implemented yet; refusing to start providers, shell, tools, or adapters.",
        "execution_started": False,
        "process_started": False,
        "network_called": False,
        "filesystem_modified": False,
        "permission_granting": False,
    }


def _command_from_file(project_root: Path, path: Path, base: str) -> dict[str, Any]:
    rel = relative_to_project(project_root, resolve_under_project(project_root, path))
    text = path.read_text(encoding="utf-8", errors="replace")
    metadata, body = _split_frontmatter(text)
    stem = path.stem.strip()
    raw_name = str(metadata.get("name") or stem)
    name = _normalize_name(raw_name)
    title = str(metadata.get("title") or raw_name).strip() or name
    description = str(metadata.get("description") or _first_nonempty_line(body) or title).strip()
    variables = metadata.get("variables") or _extract_template_variables(body)
    if isinstance(variables, dict):
        variable_names = sorted(str(key) for key in variables)
    elif isinstance(variables, list):
        variable_names = sorted(str(item) for item in variables)
    else:
        variable_names = _extract_template_variables(body)
    return {
        "id": f"project:{name}",
        "name": name,
        "slash": f"/{name}",
        "title": sanitize_for_logging(title),
        "description": sanitize_for_logging(description)[:512],
        "path": rel,
        "scope": "project",
        "origin": "opencode" if base == ".opencode/command" else "harness",
        "template_variables": variable_names,
        "body_preview": sanitize_for_logging(body.strip())[:512],
        "contents_included": False,
        "execution_supported": False,
        "mutates_when_run": None,
        "safety_note": "Project command template only; Harness does not execute user-defined commands in this phase.",
    }


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return {}, text
    raw = text[4:end]
    body = text[end + 5 :]
    loaded = yaml.safe_load(raw) or {}
    return (loaded if isinstance(loaded, dict) else {}), body


def _normalize_name(value: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9_-]+", "-", value.strip().lower()).strip("-")
    return text or "command"


def _first_nonempty_line(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip().lstrip("#").strip()
        if stripped:
            return stripped
    return ""


def _extract_template_variables(text: str) -> list[str]:
    names = set(re.findall(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_-]*)\s*\}\}", text))
    return sorted(names)
