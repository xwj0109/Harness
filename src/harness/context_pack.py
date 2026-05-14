from __future__ import annotations

import json
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harness.config import DEFAULT_CONTEXT_EXCLUDES, HARNESS_DIR, load_config
from harness.memory.sqlite_store import SQLiteStore
from harness.operator_context import build_operator_context
from harness.paths import is_excluded_relative, relative_to_project, resolve_project_root
from harness.registry import builtin_spec_registry
from harness.sandbox_profiles import list_sandbox_profiles
from harness.security import assert_not_secret_path, sanitize_for_logging, scan_text_for_secrets


DEFAULT_CONTEXT_BUDGET_CHARS = 32_000
MAX_TREE_FILES = 200
MAX_FILE_CHARS = 8_000
MAX_DIFF_CHARS = 8_000


@dataclass(frozen=True)
class ContextBlock:
    kind: str
    title: str
    content: str
    source: str | None = None
    token_estimate: int = 0
    truncated: bool = False

    def to_payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "title": self.title,
            "content": self.content,
            "source": self.source,
            "token_estimate": self.token_estimate,
            "truncated": self.truncated,
        }


@dataclass(frozen=True)
class ContextManifest:
    project_root: str
    blocks: list[ContextBlock]
    excluded_patterns: list[str]
    blocked_paths: list[str]
    warnings: list[str]

    def to_payload(self) -> dict[str, Any]:
        return {
            "project_root": self.project_root,
            "blocks": [block.to_payload() for block in self.blocks],
            "excluded_patterns": self.excluded_patterns,
            "blocked_paths": self.blocked_paths,
            "warnings": self.warnings,
        }


def pack_chat_context(project_root: Path, *, budget_chars: int = DEFAULT_CONTEXT_BUDGET_CHARS) -> ContextManifest:
    project_root = resolve_project_root(project_root)
    excluded_patterns = _context_excludes(project_root)
    blocked_paths: list[str] = []
    warnings: list[str] = []
    blocks: list[ContextBlock] = []

    candidates = [
        _harness_vocabulary_block(),
        _builtin_registry_block(warnings),
        _security_policy_block(),
        _sandbox_profiles_block(warnings),
        _repo_tree_block(project_root, excluded_patterns, blocked_paths),
        _read_named_file_block(project_root, "README.md", excluded_patterns, blocked_paths, warnings),
        _read_named_file_block(project_root, "AGENTS.md", excluded_patterns, blocked_paths, warnings),
        _git_block(project_root, ["git", "status", "--short", "--branch"], "git_status", "Git status"),
        _git_block(project_root, ["git", "diff", "--stat"], "git_diff_stat", "Git diff stat"),
        _git_block(project_root, ["git", "diff", "--"], "git_diff", "Git diff", limit=MAX_DIFF_CHARS),
        _recent_artifacts_block(project_root, warnings),
        _operator_context_block(project_root, warnings),
    ]

    used = 0
    for block in candidates:
        if block is None:
            continue
        remaining = budget_chars - used
        if remaining <= 0:
            warnings.append("context_budget_exhausted")
            break
        fitted = _fit_block(block, remaining)
        blocks.append(fitted)
        used += len(fitted.content)
        if fitted.truncated:
            warnings.append(f"context_block_truncated:{fitted.kind}")
            break

    return ContextManifest(
        project_root=str(project_root),
        blocks=blocks,
        excluded_patterns=excluded_patterns,
        blocked_paths=blocked_paths,
        warnings=warnings,
    )


def _context_excludes(project_root: Path) -> list[str]:
    try:
        return list(load_config(project_root).context_excludes)
    except FileNotFoundError:
        return list(DEFAULT_CONTEXT_EXCLUDES)


def _block(kind: str, title: str, content: str, *, source: str | None = None, truncated: bool = False) -> ContextBlock:
    sanitized = str(sanitize_for_logging(content))
    return ContextBlock(
        kind=kind,
        title=title,
        content=sanitized,
        source=source,
        token_estimate=max(1, len(sanitized) // 4),
        truncated=truncated,
    )


def _fit_block(block: ContextBlock, remaining: int) -> ContextBlock:
    if len(block.content) <= remaining:
        return block
    marker = "\n[TRUNCATED: context budget]\n"
    content = block.content[: max(0, remaining - len(marker))] + marker
    return _block(block.kind, block.title, content, source=block.source, truncated=True)


def _harness_vocabulary_block() -> ContextBlock:
    return _block(
        "harness_vocabulary",
        "Harness vocabulary and authority boundaries",
        "\n".join(
            [
                "Harness is the authority layer underneath the LLM chat.",
                "Objectives group task graphs. Tasks carry adapter metadata. Leases reserve tasks. Runs produce artifacts and manifests.",
                "Registered adapters execute bounded work. Codex edits happen in isolated workspaces. Apply-back copies reviewed diffs into the active repo only after approval.",
                "Security, policy, approvals, sandbox profiles, runtime controls, and blocked-state explanations determine what can run.",
                "The LLM may discuss and request Harness actions, but side effects require Harness validation and confirmation.",
            ]
        ),
    )


def _repo_tree_block(project_root: Path, excludes: list[str], blocked_paths: list[str]) -> ContextBlock:
    files: list[str] = []
    for path in sorted(project_root.rglob("*")):
        if not path.is_file():
            continue
        rel = relative_to_project(project_root, path)
        if is_excluded_relative(rel, excludes):
            continue
        try:
            assert_not_secret_path(path)
        except ValueError:
            blocked_paths.append(rel)
            continue
        files.append(rel)
        if len(files) >= MAX_TREE_FILES:
            break
    suffix = "\n[TRUNCATED: file listing]" if len(files) >= MAX_TREE_FILES else ""
    return _block("repo_tree", "Repository tree summary", "\n".join(files) + suffix)


def _read_named_file_block(
    project_root: Path,
    name: str,
    excludes: list[str],
    blocked_paths: list[str],
    warnings: list[str],
) -> ContextBlock | None:
    path = project_root / name
    if not path.exists() or not path.is_file():
        return None
    rel = relative_to_project(project_root, path)
    if is_excluded_relative(rel, excludes):
        return None
    try:
        assert_not_secret_path(path)
        raw = path.read_bytes()
    except ValueError:
        blocked_paths.append(rel)
        return None
    if b"\x00" in raw:
        warnings.append(f"context_file_binary:{rel}")
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        warnings.append(f"context_file_encoding:{rel}")
        return None
    findings = scan_text_for_secrets(text)
    if findings:
        blocked_paths.append(rel)
        warnings.append(f"context_file_secret_findings:{rel}")
        return None
    truncated = len(text) > MAX_FILE_CHARS
    content = text[:MAX_FILE_CHARS] + ("\n[TRUNCATED: file too large]\n" if truncated else "")
    return _block("project_file", name, content, source=rel, truncated=truncated)


def _git_block(project_root: Path, command: list[str], kind: str, title: str, *, limit: int = 4_000) -> ContextBlock | None:
    try:
        result = subprocess.run(command, cwd=project_root, text=True, capture_output=True, timeout=10)
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    content = result.stdout.strip()
    if not content:
        return None
    truncated = len(content) > limit
    return _block(kind, title, content[:limit], truncated=truncated)


def _operator_context_block(project_root: Path, warnings: list[str]) -> ContextBlock | None:
    try:
        context = build_operator_context(project_root)
    except (sqlite3.Error, OSError, ValueError) as exc:
        warnings.append(f"operator_context_unavailable:{exc.__class__.__name__}")
        return None
    compact = {
        "initialized": context.get("initialized"),
        "branch": context.get("branch"),
        "summary": context.get("summary"),
        "task_status_counts": context.get("task_status_counts"),
        "agents": context.get("agents", [])[:10],
        "tasks": context.get("tasks", [])[:10],
        "active_leases": context.get("active_leases", [])[:10],
        "recent_runs": context.get("recent_runs", [])[:5],
        "memory": context.get("memory"),
        "progress": context.get("progress"),
        "registered_adapters": context.get("registered_adapters", []),
        "capabilities": context.get("capabilities"),
        "runtime_controls": context.get("runtime_controls"),
    }
    return _json_block("harness_state", "Harness state summary", compact)


def _builtin_registry_block(warnings: list[str]) -> ContextBlock | None:
    try:
        registry = builtin_spec_registry()
    except ValueError as exc:
        warnings.append(f"builtin_registry_unavailable:{exc.__class__.__name__}")
        return None
    payload = {
        "workbenches": {
            key: {
                "default_model_profile": value.default_model_profile,
                "allowed_agents": value.allowed_agents,
            }
            for key, value in registry.workbenches.items()
        },
        "agents": {
            key: {
                "kind": value.kind.value,
                "parent": value.parent,
                "model_profile": value.model_profile,
                "tool_policy": value.tool_policy,
                "memory_scope": value.memory_scope,
                "tags": value.tags,
            }
            for key, value in registry.agents.items()
        },
        "model_profiles": {
            key: {
                "kind": value.kind.value,
                "backend": value.backend,
                "default": value.default,
                "constraints": value.constraints,
            }
            for key, value in registry.model_profiles.items()
        },
        "tool_policies": {
            key: {
                "network": value.network.value,
                "active_repo_write": value.active_repo_write.value,
                "hosted_boundary": value.hosted_boundary.value,
                "tools": {tool: permission.value for tool, permission in value.tools.items()},
            }
            for key, value in registry.tool_policies.items()
        },
        "memory_scopes": {
            key: {
                "allowed_paths": value.allowed_paths,
                "forbidden_paths": value.forbidden_paths,
            }
            for key, value in registry.memory_scopes.items()
        },
    }
    return _json_block("builtin_harness_domain", "Built-in agents, workbenches, profiles, policies, and memory scopes", payload)


def _security_policy_block() -> ContextBlock:
    payload = {
        "security_layer": [
            "path guards block project escapes and secret-like paths",
            "context excludes prevent hidden state and build/cache folders from entering model context",
            "hosted/data-boundary work requires explicit approval",
            "registered adapters fail closed on unknown adapters, unsafe metadata, missing approvals, and breaker controls",
            "apply-back requires separate approval after isolated edit review",
        ],
        "blocked_state_codes": [
            "missing_approval",
            "disabled_adapter",
            "unsafe_metadata",
            "unknown_adapter",
            "sandbox_profile_mismatch",
            "breaker_open",
            "forbidden_path_or_secret_like_content",
        ],
    }
    return _json_block("security_policy_summary", "Security and policy summary", payload)


def _sandbox_profiles_block(warnings: list[str]) -> ContextBlock | None:
    try:
        payload = [profile.model_dump(mode="json") for profile in list_sandbox_profiles()]
    except ValueError as exc:
        warnings.append(f"sandbox_profiles_unavailable:{exc.__class__.__name__}")
        return None
    return _json_block("sandbox_profiles", "Sandbox profile summary", payload)


def _recent_artifacts_block(project_root: Path, warnings: list[str]) -> ContextBlock | None:
    if not (project_root / HARNESS_DIR / "harness.sqlite").exists():
        return None
    try:
        store = SQLiteStore(project_root)
        runs = store.list_runs()[:5]
        payload = []
        for run in runs:
            artifacts = store.list_artifacts(run.id)
            payload.append(
                {
                    "run": run.model_dump(mode="json"),
                    "artifacts": [
                        {
                            "id": artifact.id,
                            "kind": artifact.kind,
                            "path": str(artifact.path),
                            "size_bytes": artifact.size_bytes,
                            "sha256": artifact.sha256,
                            "producer": artifact.producer,
                            "redaction_state": artifact.redaction_state,
                            "evidence_status": artifact.evidence_status,
                        }
                        for artifact in artifacts[:10]
                    ],
                }
            )
    except (sqlite3.Error, KeyError, ValueError) as exc:
        warnings.append(f"recent_artifacts_unavailable:{exc.__class__.__name__}")
        return None
    if not payload:
        return None
    return _json_block("recent_artifacts", "Recent run artifact metadata", payload)


def _json_block(kind: str, title: str, payload: Any) -> ContextBlock:
    return _block(kind, title, json.dumps(sanitize_for_logging(payload), sort_keys=True, indent=2))
