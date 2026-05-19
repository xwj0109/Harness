from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from harness.config import DEFAULT_CONTEXT_EXCLUDES, load_config
from harness.context_budget import TokenBudgeter, budgeter_for_project
from harness.models import ArtifactRecord, ContextSourceKind, ContextTrustLevel, MemoryRecord
from harness.paths import is_excluded_relative, relative_to_project, resolve_project_root
from harness.security import assert_not_secret_path, sanitize_for_logging, scan_text_for_secrets


CONTEXT_CHUNK_SCHEMA_VERSION = "harness.context_chunk/v1"
DEFAULT_REPO_CHUNK_SCHEME = "line-v1:80"
MEMORY_CHUNK_SCHEME = "memory-summary-v1"
ARTIFACT_METADATA_CHUNK_SCHEME = "artifact-metadata-v1"
MAX_REPO_CHUNK_LINES = 80
MAX_TEXT_PREVIEW_CHARS = 1200
MEMORY_NOT_AUTHORITY_WARNING = "memory_not_authority"


@dataclass(frozen=True)
class ContextChunk:
    id: str
    source_kind: ContextSourceKind
    trust_level: ContextTrustLevel
    sha256: str
    size_bytes: int
    chunk_scheme: str
    text_preview: str
    schema_version: str = CONTEXT_CHUNK_SCHEMA_VERSION
    path: str | None = None
    source_id: str | None = None
    artifact_id: str | None = None
    memory_id: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    token_count: int | None = None
    tokenizer: str | None = None
    redaction_state: str | None = None
    warnings: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_row(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "schema_version": self.schema_version,
            "source_kind": self.source_kind.value,
            "trust_level": self.trust_level.value,
            "path": self.path,
            "source_id": self.source_id,
            "artifact_id": self.artifact_id,
            "memory_id": self.memory_id,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "token_count": self.token_count,
            "tokenizer": self.tokenizer,
            "chunk_scheme": self.chunk_scheme,
            "text_preview": self.text_preview,
            "redaction_state": self.redaction_state,
            "warnings_json": json.dumps(list(self.warnings), sort_keys=True, default=str),
            "metadata_json": json.dumps(sanitize_for_logging(self.metadata), sort_keys=True, default=str),
        }


def chunk_repo_file(
    project_root: Path,
    path: Path,
    *,
    budgeter: TokenBudgeter | None = None,
    chunk_scheme: str = DEFAULT_REPO_CHUNK_SCHEME,
    max_lines: int = MAX_REPO_CHUNK_LINES,
) -> list[ContextChunk]:
    project_root = resolve_project_root(project_root)
    path = path.resolve()
    rel = relative_to_project(project_root, path)
    if is_excluded_relative(rel, _context_excludes(project_root)):
        return []
    try:
        assert_not_secret_path(path)
    except ValueError:
        return []
    budgeter = budgeter or budgeter_for_project(project_root)
    raw = path.read_bytes()
    if b"\x00" in raw:
        return []
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return []
    if scan_text_for_secrets(text):
        return []
    lines = text.splitlines()
    if not lines:
        lines = [""]
    chunks: list[ContextChunk] = []
    for start_index in range(0, len(lines), max_lines):
        end_index = min(start_index + max_lines, len(lines))
        chunk_text = "\n".join(lines[start_index:end_index])
        encoded = chunk_text.encode("utf-8")
        sha256 = hashlib.sha256(encoded).hexdigest()
        start_line = start_index + 1
        end_line = end_index
        chunk_id = _chunk_id(
            "repo_file",
            rel,
            sha256,
            str(start_line),
            str(end_line),
            chunk_scheme,
            budgeter.name,
        )
        chunks.append(
            ContextChunk(
                id=chunk_id,
                source_kind=ContextSourceKind.REPO_FILE,
                trust_level=ContextTrustLevel.UNTRUSTED_REPO,
                path=rel,
                start_line=start_line,
                end_line=end_line,
                sha256=sha256,
                size_bytes=len(encoded),
                token_count=budgeter.count(chunk_text),
                tokenizer=budgeter.name,
                chunk_scheme=chunk_scheme,
                text_preview=_preview(chunk_text),
                redaction_state="not_required",
                metadata={"project_root": str(project_root), "permission_granting": False},
            )
        )
    return chunks


def rebuild_repo_file_context_chunks(
    project_root: Path,
    *,
    store: Any | None = None,
    budgeter: TokenBudgeter | None = None,
    chunk_scheme: str = DEFAULT_REPO_CHUNK_SCHEME,
) -> list[ContextChunk]:
    from harness.memory.sqlite_store import SQLiteStore

    project_root = resolve_project_root(project_root)
    store = store or SQLiteStore(project_root)
    excludes = _context_excludes(project_root)
    budgeter = budgeter or budgeter_for_project(project_root)
    written: list[ContextChunk] = []
    _delete_invalid_repo_file_chunks(project_root, store, excludes)
    for path in sorted(project_root.rglob("*")):
        if not path.is_file():
            continue
        rel = relative_to_project(project_root, path)
        if is_excluded_relative(rel, excludes):
            continue
        try:
            assert_not_secret_path(path)
        except ValueError:
            continue
        chunks = chunk_repo_file(project_root, path, budgeter=budgeter, chunk_scheme=chunk_scheme)
        if not chunks:
            store.delete_context_chunks_for_source_path(
                ContextSourceKind.REPO_FILE.value,
                rel,
                chunk_scheme=chunk_scheme,
                tokenizer=budgeter.name,
            )
            continue
        expected_ids = {chunk.id for chunk in chunks}
        store.delete_context_chunks_for_source_path(
            ContextSourceKind.REPO_FILE.value,
            rel,
            keep_ids=expected_ids,
            chunk_scheme=chunk_scheme,
            tokenizer=budgeter.name,
        )
        for chunk in chunks:
            store.upsert_context_chunk(chunk)
        written.extend(chunks)
    return written


def _delete_invalid_repo_file_chunks(project_root: Path, store: Any, excludes: list[str]) -> None:
    for chunk in store.list_context_chunks(source_kind=ContextSourceKind.REPO_FILE.value):
        if not chunk.path:
            continue
        path = project_root / chunk.path
        delete = False
        if is_excluded_relative(chunk.path, excludes):
            delete = True
        elif not path.exists() or not path.is_file():
            delete = True
        else:
            try:
                assert_not_secret_path(path)
            except ValueError:
                delete = True
        if delete:
            store.delete_context_chunks_for_source_path(ContextSourceKind.REPO_FILE.value, chunk.path)


def chunk_memory_record(memory: MemoryRecord, *, budgeter: TokenBudgeter | None = None) -> ContextChunk:
    text = str(sanitize_for_logging(memory.summary))
    encoded = text.encode("utf-8")
    sha256 = hashlib.sha256(encoded).hexdigest()
    tokenizer = budgeter.name if budgeter is not None else None
    chunk_id = _chunk_id("memory_record", memory.id, sha256, MEMORY_CHUNK_SCHEME, tokenizer or "")
    return ContextChunk(
        id=chunk_id,
        source_kind=ContextSourceKind.MEMORY_RECORD,
        trust_level=ContextTrustLevel.MEMORY,
        source_id=memory.source_id,
        memory_id=memory.id,
        sha256=sha256,
        size_bytes=len(encoded),
        token_count=budgeter.count(text) if budgeter is not None else None,
        tokenizer=tokenizer,
        chunk_scheme=MEMORY_CHUNK_SCHEME,
        text_preview=_preview(text),
        redaction_state=memory.redaction_state.value,
        warnings=[MEMORY_NOT_AUTHORITY_WARNING],
        metadata={
            "scope_type": memory.scope_type.value,
            "scope_id": memory.scope_id,
            "source_kind": memory.source_kind.value,
            "source_artifact_id": memory.source_artifact_id,
            "lineage": sanitize_for_logging(memory.lineage),
            "permission_granting": False,
            "policy_authority": False,
            "approval_authority": False,
        },
    )


def rebuild_memory_context_chunks(
    project_root: Path,
    *,
    store: Any | None = None,
    budgeter: TokenBudgeter | None = None,
) -> list[ContextChunk]:
    from harness.memory.sqlite_store import SQLiteStore

    project_root = resolve_project_root(project_root)
    store = store or SQLiteStore(project_root)
    budgeter = budgeter or budgeter_for_project(project_root)
    chunks: list[ContextChunk] = []
    for memory in store.list_memory_records():
        chunk = chunk_memory_record(memory, budgeter=budgeter)
        store.delete_context_chunks_for_memory(memory.id)
        store.upsert_context_chunk(chunk)
        chunks.append(chunk)
    return chunks


def chunk_artifact_metadata(artifact: ArtifactRecord, *, budgeter: TokenBudgeter | None = None) -> ContextChunk:
    payload = {
        "id": artifact.id,
        "run_id": artifact.run_id,
        "session_id": artifact.session_id,
        "kind": artifact.kind,
        "path": str(artifact.path),
        "sha256": artifact.sha256,
        "size_bytes": artifact.size_bytes,
        "producer": artifact.producer,
        "redaction_state": artifact.redaction_state,
        "evidence_status": artifact.evidence_status,
        "metadata": sanitize_for_logging(artifact.metadata),
        "contents_included": False,
        "permission_granting": False,
    }
    text = json.dumps(payload, sort_keys=True, default=str)
    encoded = text.encode("utf-8")
    sha256 = hashlib.sha256(encoded).hexdigest()
    tokenizer = budgeter.name if budgeter is not None else None
    chunk_id = _chunk_id("artifact", artifact.id, sha256, ARTIFACT_METADATA_CHUNK_SCHEME, tokenizer or "")
    return ContextChunk(
        id=chunk_id,
        source_kind=ContextSourceKind.ARTIFACT,
        trust_level=ContextTrustLevel.ARTIFACT,
        path=str(artifact.path),
        source_id=artifact.run_id,
        artifact_id=artifact.id,
        sha256=sha256,
        size_bytes=len(encoded),
        token_count=budgeter.count(text) if budgeter is not None else None,
        tokenizer=tokenizer,
        chunk_scheme=ARTIFACT_METADATA_CHUNK_SCHEME,
        text_preview=_preview(text),
        redaction_state=artifact.redaction_state,
        warnings=["artifact_body_not_indexed"],
        metadata={"artifact_kind": artifact.kind, "contents_included": False, "permission_granting": False},
    )


def rebuild_artifact_metadata_context_chunks(
    project_root: Path,
    run_id: str,
    *,
    store: Any | None = None,
    budgeter: TokenBudgeter | None = None,
) -> list[ContextChunk]:
    from harness.memory.sqlite_store import SQLiteStore

    project_root = resolve_project_root(project_root)
    store = store or SQLiteStore(project_root)
    budgeter = budgeter or budgeter_for_project(project_root)
    chunks: list[ContextChunk] = []
    for artifact in store.list_artifacts(run_id):
        chunk = chunk_artifact_metadata(artifact, budgeter=budgeter)
        store.upsert_context_chunk(chunk)
        chunks.append(chunk)
    return chunks


def _context_excludes(project_root: Path) -> list[str]:
    try:
        return list(load_config(project_root).context_excludes)
    except FileNotFoundError:
        return list(DEFAULT_CONTEXT_EXCLUDES)


def _preview(text: str) -> str:
    sanitized = str(sanitize_for_logging(text))
    if len(sanitized) <= MAX_TEXT_PREVIEW_CHARS:
        return sanitized
    return sanitized[:MAX_TEXT_PREVIEW_CHARS] + "\n[TRUNCATED: context chunk preview]\n"


def _chunk_id(*parts: str) -> str:
    stable = "\0".join(parts)
    return f"ctx_{hashlib.sha256(stable.encode('utf-8')).hexdigest()[:24]}"
