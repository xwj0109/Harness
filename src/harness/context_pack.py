from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from harness.config import DEFAULT_CONTEXT_EXCLUDES, HARNESS_DIR, load_config
from harness.context_budget import (
    APPROXIMATE_TOKEN_BUDGET_WARNING,
    ContextBudgetReport,
    TokenBudgeter,
    budget_report,
    budgeter_for_project,
    legacy_char_budget_to_tokens,
    model_profile_for_project,
)
from harness.context_compression import ExtractiveContextCompressor
from harness.context_retrieval import LexicalContextRetriever, RetrievedContextChunk
from harness.memory.sqlite_store import SQLiteStore
from harness.models import ContextProvenanceRecord, ContextSourceKind, ContextTrustLevel
from harness.operator_context import build_operator_context
from harness.paths import is_excluded_relative, relative_to_project, resolve_project_root
from harness.registry import builtin_spec_registry
from harness.sandbox_profiles import list_sandbox_profiles
from harness.security import assert_not_secret_path, sanitize_for_logging, scan_text_for_secrets


DEFAULT_CONTEXT_BUDGET_CHARS = 32_000
MAX_TREE_FILES = 200
MAX_FILE_CHARS = 8_000
MAX_DIFF_CHARS = 8_000
ContextBlockRole = Literal["pinned", "retrieved", "derived"]


@dataclass(frozen=True)
class ContextRequest:
    project_root: Path
    query: str = ""
    mode: str | None = None
    model_profile: str | None = None
    session_id: str | None = None
    objective_id: str | None = None
    task_id: str | None = None
    run_id: str | None = None
    safety_boundaries: list[str] | None = None


@dataclass(frozen=True)
class ContextBlock:
    kind: str
    title: str
    content: str
    source: str | None = None
    token_estimate: int = 0
    truncated: bool = False
    role: ContextBlockRole = "retrieved"
    score: float | None = None
    chunk_ids: list[str] | None = None
    retrieval: dict[str, Any] | None = None
    provenance_id: str | None = None

    def to_payload(self) -> dict[str, Any]:
        payload = {
            "kind": self.kind,
            "title": self.title,
            "content": self.content,
            "source": self.source,
            "token_estimate": self.token_estimate,
            "truncated": self.truncated,
            "role": self.role,
        }
        if self.score is not None:
            payload["score"] = self.score
        if self.chunk_ids:
            payload["chunk_ids"] = list(self.chunk_ids)
        if self.retrieval:
            payload["retrieval"] = dict(self.retrieval)
        if self.provenance_id:
            payload["provenance_id"] = self.provenance_id
        return payload


@dataclass(frozen=True)
class ContextManifest:
    project_root: str
    blocks: list[ContextBlock]
    excluded_patterns: list[str]
    blocked_paths: list[str]
    warnings: list[str]
    budget_report: ContextBudgetReport
    role_summary: dict[str, int]
    retriever: str | None = None
    selected_chunks: list[dict[str, Any]] | None = None
    context_provenance: list[ContextProvenanceRecord] | None = None
    untrusted_context_warnings: list[str] | None = None

    def to_payload(self) -> dict[str, Any]:
        payload = {
            "project_root": self.project_root,
            "blocks": [block.to_payload() for block in self.blocks],
            "excluded_patterns": self.excluded_patterns,
            "blocked_paths": self.blocked_paths,
            "warnings": self.warnings,
            "budget_report": self.budget_report.to_payload(),
            "role_summary": dict(self.role_summary),
        }
        if self.retriever:
            payload["retriever"] = self.retriever
        if self.selected_chunks is not None:
            payload["selected_chunks"] = list(self.selected_chunks)
        if self.context_provenance is not None:
            payload["context_provenance"] = [
                record.model_dump(mode="json") for record in self.context_provenance
            ]
        if self.untrusted_context_warnings is not None:
            payload["untrusted_context_warnings"] = list(self.untrusted_context_warnings)
        return payload


def pack_chat_context(
    project_root: Path,
    *,
    budget_chars: int = DEFAULT_CONTEXT_BUDGET_CHARS,
    budgeter: TokenBudgeter | None = None,
    model_profile: str | None = None,
    query: str = "",
    mode: str | None = None,
    session_id: str | None = None,
    objective_id: str | None = None,
    task_id: str | None = None,
    run_id: str | None = None,
    safety_boundaries: list[str] | None = None,
    enable_compression: bool = False,
) -> ContextManifest:
    project_root = resolve_project_root(project_root)
    excluded_patterns = _context_excludes(project_root)
    blocked_paths: list[str] = []
    warnings: list[str] = []
    blocks: list[ContextBlock] = []
    selected_model_profile = model_profile_for_project(project_root, model_profile=model_profile)
    selected_budgeter = budgeter or budgeter_for_project(project_root, model_profile=selected_model_profile)
    if selected_budgeter.approximate:
        warnings.append(APPROXIMATE_TOKEN_BUDGET_WARNING)
    max_input_tokens = legacy_char_budget_to_tokens(budget_chars)
    request = ContextRequest(
        project_root=project_root,
        query=query,
        mode=mode,
        model_profile=selected_model_profile,
        session_id=session_id,
        objective_id=objective_id,
        task_id=task_id,
        run_id=run_id,
        safety_boundaries=list(safety_boundaries or []),
    )
    pinned_candidates = pack_pinned_context(project_root, request, warnings=warnings)
    retriever_name: str | None = None
    dynamic_candidates: list[ContextBlock | None] = []
    if query.strip():
        retrieved_chunks = _retrieve_dynamic_chunks(project_root, query, warnings)
        if retrieved_chunks:
            retriever_name = retrieved_chunks[0].retriever
            dynamic_candidates = [_retrieved_chunk_block(item) for item in retrieved_chunks]
    if not dynamic_candidates:
        dynamic_candidates = pack_static_dynamic_context(
            project_root,
            request,
            excluded_patterns=excluded_patterns,
            blocked_paths=blocked_paths,
            warnings=warnings,
        )
    candidates = [*pinned_candidates, *dynamic_candidates]
    compressor = ExtractiveContextCompressor() if enable_compression else None

    used = 0
    for block in candidates:
        if block is None:
            continue
        remaining = max_input_tokens - used
        if remaining <= 0:
            warnings.append("context_budget_exhausted")
            break
        candidate = block
        if compressor is not None and compressor.is_eligible(block) and selected_budgeter.count(block.content) > remaining:
            compressed = compressor.compress(
                block,
                query=query,
                target_tokens=remaining,
                budgeter=selected_budgeter,
            )
            if compressed.compressed:
                candidate = _compressed_block(block, compressed)
                warnings.append(f"context_block_compressed:{block.kind}")
        fitted = _fit_block(candidate, remaining, selected_budgeter)
        blocks.append(fitted)
        used += fitted.token_estimate
        if fitted.truncated:
            warnings.append(f"context_block_truncated:{fitted.kind}")
            break
    role_summary = _role_summary(blocks)
    selected_chunks = [dict(block.retrieval) for block in blocks if block.retrieval]
    context_provenance = _context_provenance(blocks)
    untrusted_context_warnings = _untrusted_context_warnings(context_provenance)

    return ContextManifest(
        project_root=str(project_root),
        blocks=blocks,
        excluded_patterns=excluded_patterns,
        blocked_paths=blocked_paths,
        warnings=warnings,
        budget_report=budget_report(
            selected_budgeter,
            model_profile=selected_model_profile,
            max_input_tokens=max_input_tokens,
            used_input_tokens=used,
        ),
        role_summary=role_summary,
        retriever=retriever_name,
        selected_chunks=selected_chunks if retriever_name else None,
        context_provenance=context_provenance,
        untrusted_context_warnings=untrusted_context_warnings,
    )


def pack_pinned_context(
    project_root: Path,
    request: ContextRequest | None = None,
    *,
    warnings: list[str] | None = None,
) -> list[ContextBlock | None]:
    project_root = resolve_project_root(project_root)
    active_request = request or ContextRequest(project_root=project_root)
    active_warnings = warnings if warnings is not None else []
    return [
        _harness_vocabulary_block(),
        _builtin_registry_block(active_warnings),
        _security_policy_block(),
        _sandbox_profiles_block(active_warnings),
        _request_context_block(active_request),
        _memory_summary_block(project_root, active_warnings),
    ]


def pack_static_dynamic_context(
    project_root: Path,
    request: ContextRequest | None = None,
    *,
    excluded_patterns: list[str] | None = None,
    blocked_paths: list[str] | None = None,
    warnings: list[str] | None = None,
) -> list[ContextBlock | None]:
    del request
    project_root = resolve_project_root(project_root)
    active_excludes = excluded_patterns if excluded_patterns is not None else _context_excludes(project_root)
    active_blocked_paths = blocked_paths if blocked_paths is not None else []
    active_warnings = warnings if warnings is not None else []
    return [
        _repo_tree_block(project_root, active_excludes, active_blocked_paths),
        _read_named_file_block(project_root, "README.md", active_excludes, active_blocked_paths, active_warnings),
        _read_named_file_block(project_root, "AGENTS.md", active_excludes, active_blocked_paths, active_warnings),
        _git_block(project_root, ["git", "status", "--short", "--branch"], "git_status", "Git status"),
        _git_block(project_root, ["git", "diff", "--stat"], "git_diff_stat", "Git diff stat"),
        _git_block(project_root, ["git", "diff", "--"], "git_diff", "Git diff", limit=MAX_DIFF_CHARS),
        _recent_artifacts_block(project_root, active_warnings),
        _operator_context_block(project_root, active_warnings),
    ]


def _context_excludes(project_root: Path) -> list[str]:
    try:
        return list(load_config(project_root).context_excludes)
    except FileNotFoundError:
        return list(DEFAULT_CONTEXT_EXCLUDES)


def _block(
    kind: str,
    title: str,
    content: str,
    *,
    source: str | None = None,
    truncated: bool = False,
    role: ContextBlockRole = "retrieved",
) -> ContextBlock:
    sanitized = str(sanitize_for_logging(content))
    return ContextBlock(
        kind=kind,
        title=title,
        content=sanitized,
        source=source,
        token_estimate=max(1, len(sanitized) // 4),
        truncated=truncated,
        role=role,
    )


def _fit_block(block: ContextBlock, remaining_tokens: int, budgeter: TokenBudgeter) -> ContextBlock:
    token_count = budgeter.count(block.content)
    if token_count <= remaining_tokens:
        return ContextBlock(
            kind=block.kind,
            title=block.title,
            content=block.content,
            source=block.source,
            token_estimate=token_count,
            truncated=block.truncated,
            role=block.role,
            score=block.score,
            chunk_ids=list(block.chunk_ids) if block.chunk_ids else None,
            retrieval=dict(block.retrieval) if block.retrieval else None,
            provenance_id=block.provenance_id,
        )
    fit = budgeter.fit(block.content, remaining_tokens)
    return ContextBlock(
        kind=block.kind,
        title=block.title,
        content=fit.text,
        source=block.source,
        token_estimate=fit.token_count,
        truncated=True,
        role=block.role,
        score=block.score,
        chunk_ids=list(block.chunk_ids) if block.chunk_ids else None,
        retrieval=dict(block.retrieval) if block.retrieval else None,
        provenance_id=block.provenance_id,
    )


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
        role="pinned",
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
    return _json_block("harness_state", "Harness state summary", compact, role="retrieved")


def _request_context_block(request: ContextRequest) -> ContextBlock | None:
    payload: dict[str, Any] = {}
    if request.mode:
        payload["mode"] = request.mode
    if request.model_profile:
        payload["model_profile"] = request.model_profile
    if request.session_id:
        payload["session_id"] = request.session_id
    if request.objective_id:
        payload["objective_id"] = request.objective_id
    if request.task_id:
        payload["task_id"] = request.task_id
    if request.run_id:
        payload["run_id"] = request.run_id
    if request.safety_boundaries:
        payload["safety_boundaries"] = list(request.safety_boundaries)
    if not payload:
        return None
    payload["permission_granting"] = False
    return _json_block("request_context", "Current request context", payload, role="pinned")


def _memory_summary_block(project_root: Path, warnings: list[str]) -> ContextBlock | None:
    if not (project_root / HARNESS_DIR / "harness.sqlite").exists():
        return None
    try:
        store = SQLiteStore(project_root)
        memory_records = store.list_memory_records()[:5]
        total = len(store.list_memory_records())
    except (sqlite3.Error, OSError, ValueError) as exc:
        warnings.append(f"memory_context_unavailable:{exc.__class__.__name__}")
        return None
    if not memory_records:
        return None
    if "memory_not_authority" not in warnings:
        warnings.append("memory_not_authority")
    memory = {
        "schema_version": "harness.memory_summary/v1",
        "total": total,
        "warnings": ["memory_not_authority"],
        "recent": [
            {
                "id": record.id,
                "scope_type": record.scope_type.value,
                "scope_id": record.scope_id,
                "summary": sanitize_for_logging(record.summary),
                "redaction_state": record.redaction_state.value,
                "lineage": sanitize_for_logging(record.lineage),
                "created_at": record.created_at.isoformat(),
            }
            for record in memory_records
        ],
    }
    return _json_block("memory_summary", "Memory summary and authority warning", memory, role="pinned")


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
    return _json_block(
        "builtin_harness_domain",
        "Built-in agents, workbenches, profiles, policies, and memory scopes",
        payload,
        role="pinned",
    )


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
    return _json_block("security_policy_summary", "Security and policy summary", payload, role="pinned")


def _sandbox_profiles_block(warnings: list[str]) -> ContextBlock | None:
    try:
        payload = [profile.model_dump(mode="json") for profile in list_sandbox_profiles()]
    except ValueError as exc:
        warnings.append(f"sandbox_profiles_unavailable:{exc.__class__.__name__}")
        return None
    return _json_block("sandbox_profiles", "Sandbox profile summary", payload, role="pinned")


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
    return _json_block("recent_artifacts", "Recent run artifact metadata", payload, role="retrieved")


def _json_block(kind: str, title: str, payload: Any, *, role: ContextBlockRole = "retrieved") -> ContextBlock:
    return _block(kind, title, json.dumps(sanitize_for_logging(payload), sort_keys=True, indent=2), role=role)


def _retrieve_dynamic_chunks(project_root: Path, query: str, warnings: list[str]) -> list[RetrievedContextChunk]:
    try:
        return LexicalContextRetriever(project_root).retrieve(query, limit=8)
    except (sqlite3.Error, OSError, ValueError) as exc:
        warnings.append(f"context_retrieval_unavailable:{exc.__class__.__name__}")
        return []


def _retrieved_chunk_block(item: RetrievedContextChunk) -> ContextBlock:
    chunk = item.chunk
    source = chunk.path or chunk.source_id or chunk.memory_id or chunk.artifact_id
    label = chunk.source_kind.value
    if chunk.path and chunk.start_line is not None and chunk.end_line is not None:
        label = f"{label}: {chunk.path}:{chunk.start_line}-{chunk.end_line}"
    elif source:
        label = f"{label}: {source}"
    metadata = item.to_manifest_ref()
    provenance_id = _provenance_id("chunk", chunk.id)
    metadata["provenance_id"] = provenance_id
    block = _block(
        "retrieved_context_chunk",
        f"Retrieved context chunk {item.rank}: {label}",
        chunk.text_preview,
        source=source,
        role="retrieved",
    )
    return ContextBlock(
        kind=block.kind,
        title=block.title,
        content=block.content,
        source=block.source,
        token_estimate=block.token_estimate,
        truncated=block.truncated,
        role=block.role,
        score=item.score,
        chunk_ids=[chunk.id],
        retrieval=metadata,
        provenance_id=provenance_id,
    )


def _compressed_block(block: ContextBlock, compressed: Any) -> ContextBlock:
    retrieval = dict(block.retrieval or {})
    lineage = dict(compressed.lineage)
    retrieval["compressed"] = True
    retrieval["compression"] = lineage
    retrieval["original_chunk_ids"] = list(lineage.get("original_chunk_ids") or block.chunk_ids or [])
    return ContextBlock(
        kind=block.kind,
        title=block.title,
        content=compressed.content,
        source=block.source,
        token_estimate=compressed.token_count,
        truncated=True,
        role=block.role,
        score=block.score,
        chunk_ids=list(block.chunk_ids) if block.chunk_ids else None,
        retrieval=retrieval,
        provenance_id=block.provenance_id,
    )


def _context_provenance(blocks: list[ContextBlock]) -> list[ContextProvenanceRecord]:
    records: list[ContextProvenanceRecord] = []
    seen: set[str] = set()
    for block in blocks:
        if block.retrieval:
            record = _retrieved_block_provenance(block)
        else:
            record = _static_block_provenance(block)
        if record is None or record.id in seen:
            continue
        seen.add(record.id)
        records.append(record)
    return records


def _retrieved_block_provenance(block: ContextBlock) -> ContextProvenanceRecord | None:
    metadata = block.retrieval or {}
    provenance_id = str(metadata.get("provenance_id") or block.provenance_id or "")
    if not provenance_id:
        return None
    source_kind = ContextSourceKind(str(metadata.get("source_kind") or ContextSourceKind.GENERATED_PLAN.value))
    trust_level = ContextTrustLevel(str(metadata.get("trust_level") or ContextTrustLevel.GENERATED.value))
    label = block.title
    path_value = metadata.get("path")
    lineage = {
        "chunk_id": metadata.get("chunk_id"),
        "original_chunk_ids": list(metadata.get("original_chunk_ids") or [metadata.get("chunk_id")]),
        "chunk_scheme": metadata.get("chunk_scheme"),
        "tokenizer": metadata.get("tokenizer"),
        "retriever": metadata.get("retriever"),
        "score": metadata.get("score"),
        "rank": metadata.get("rank"),
        "compressed": bool(metadata.get("compressed", False)),
        "compression": metadata.get("compression"),
        "start_line": metadata.get("start_line"),
        "end_line": metadata.get("end_line"),
        "permission_granting": False,
        "policy_authority": False,
        "approval_authority": False,
    }
    return ContextProvenanceRecord(
        id=provenance_id,
        source_kind=source_kind,
        trust_level=trust_level,
        label=str(sanitize_for_logging(label)),
        source_id=_optional_str(metadata.get("source_id")),
        artifact_id=_optional_str(metadata.get("artifact_id")),
        memory_id=_optional_str(metadata.get("memory_id")),
        path=Path(str(path_value)) if path_value else None,
        sha256=_optional_str(metadata.get("sha256")),
        redaction_state=None,
        lineage=sanitize_for_logging(lineage),
        warnings=[str(warning) for warning in metadata.get("warnings") or []],
    )


def _static_block_provenance(block: ContextBlock) -> ContextProvenanceRecord | None:
    if block.kind in {"harness_vocabulary", "builtin_harness_domain", "security_policy_summary", "sandbox_profiles", "request_context"}:
        source_kind = ContextSourceKind.GENERATED_PLAN
        trust_level = ContextTrustLevel.TRUSTED_OPERATOR
        warnings: list[str] = []
    elif block.kind == "memory_summary":
        source_kind = ContextSourceKind.MEMORY_RECORD
        trust_level = ContextTrustLevel.MEMORY
        warnings = ["memory_not_authority"]
    elif block.kind in {"repo_tree", "project_file", "git_status", "git_diff_stat", "git_diff"}:
        source_kind = ContextSourceKind.REPO_FILE
        trust_level = ContextTrustLevel.UNTRUSTED_REPO
        warnings = ["untrusted_repo_context"]
    elif block.kind in {"recent_artifacts"}:
        source_kind = ContextSourceKind.ARTIFACT
        trust_level = ContextTrustLevel.ARTIFACT
        warnings = ["artifact_content_not_authority"]
    elif block.kind in {"harness_state"}:
        source_kind = ContextSourceKind.TASK_METADATA
        trust_level = ContextTrustLevel.TRUSTED_OPERATOR
        warnings = []
    else:
        return None
    provenance_id = _provenance_id("block", f"{block.kind}:{block.source or block.title}:{block.token_estimate}")
    return ContextProvenanceRecord(
        id=provenance_id,
        source_kind=source_kind,
        trust_level=trust_level,
        label=str(sanitize_for_logging(block.title)),
        source_id=block.source,
        path=Path(block.source) if block.source else None,
        sha256=hashlib.sha256(block.content.encode("utf-8")).hexdigest(),
        redaction_state="sanitized",
        lineage={
            "block_kind": block.kind,
            "role": block.role,
            "permission_granting": False,
            "policy_authority": False,
            "approval_authority": False,
        },
        warnings=warnings,
    )


def _untrusted_context_warnings(records: list[ContextProvenanceRecord]) -> list[str]:
    warnings: list[str] = []
    for record in records:
        for warning in record.warnings:
            if warning not in warnings:
                warnings.append(warning)
    return warnings


def _provenance_id(kind: str, value: str) -> str:
    digest = hashlib.sha256(f"{kind}:{value}".encode("utf-8")).hexdigest()[:16]
    return f"ctx_{digest}"


def _optional_str(value: Any) -> str | None:
    return str(value) if value is not None else None


def _role_summary(blocks: list[ContextBlock]) -> dict[str, int]:
    summary = {"pinned": 0, "retrieved": 0, "derived": 0}
    for block in blocks:
        summary[block.role] = summary.get(block.role, 0) + 1
    return summary
