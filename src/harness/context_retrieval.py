from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from harness.config import DEFAULT_CONTEXT_EXCLUDES, load_config
from harness.context_chunks import ContextChunk, MEMORY_NOT_AUTHORITY_WARNING
from harness.memory.sqlite_store import SQLiteStore
from harness.models import ContextSourceKind
from harness.paths import is_excluded_relative, resolve_project_root
from harness.security import assert_not_secret_path, scan_text_for_secrets


LEXICAL_RETRIEVER_NAME = "lexical_context_chunks"
_QUERY_TOKEN_RE = re.compile(r"[A-Za-z0-9_./:-]+")
_SYMBOL_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\b")
_GENERATED_SOURCE_KINDS = {ContextSourceKind.GENERATED_PLAN, ContextSourceKind.ARTIFACT}


@dataclass(frozen=True)
class RetrievedContextChunk:
    chunk: ContextChunk
    score: float
    rank: int
    retriever: str = LEXICAL_RETRIEVER_NAME
    matched_terms: list[str] = field(default_factory=list)

    def to_manifest_ref(self) -> dict[str, object]:
        return {
            "chunk_id": self.chunk.id,
            "retriever": self.retriever,
            "score": self.score,
            "rank": self.rank,
            "compressed": False,
            "source_kind": self.chunk.source_kind.value,
            "trust_level": self.chunk.trust_level.value,
            "path": self.chunk.path,
            "source_id": self.chunk.source_id,
            "artifact_id": self.chunk.artifact_id,
            "memory_id": self.chunk.memory_id,
            "start_line": self.chunk.start_line,
            "end_line": self.chunk.end_line,
            "sha256": self.chunk.sha256,
            "chunk_scheme": self.chunk.chunk_scheme,
            "token_count": self.chunk.token_count,
            "tokenizer": self.chunk.tokenizer,
            "warnings": list(self.chunk.warnings),
            "matched_terms": list(self.matched_terms),
        }


class ContextRetriever(Protocol):
    def retrieve(self, query: str, *, limit: int = 8) -> list[RetrievedContextChunk]:
        ...


@dataclass(frozen=True)
class _ScoredChunk:
    chunk: ContextChunk
    raw_score: float
    matched_terms: tuple[str, ...]


class LexicalContextRetriever:
    def __init__(self, project_root: Path, *, store: SQLiteStore | None = None) -> None:
        self.project_root = resolve_project_root(project_root)
        self.store = store or SQLiteStore(self.project_root)
        self.excluded_patterns = _context_excludes(self.project_root)

    def retrieve(self, query: str, *, limit: int = 8) -> list[RetrievedContextChunk]:
        query = query.strip()
        if not query or limit <= 0:
            return []
        if not self.store.db_path.exists():
            return []
        try:
            chunks = self.store.list_context_chunks()
        except sqlite3.Error:
            return []
        query_terms = _query_terms(query)
        if not query_terms:
            return []
        scored = [
            scored
            for chunk in chunks
            if self._chunk_is_retrievable(chunk)
            for scored in [self._score_chunk(chunk, query, query_terms)]
            if scored.raw_score > 0
        ]
        deduped = _dedupe_scored_chunks(scored)
        if not deduped:
            return []
        max_score = max(item.raw_score for item in deduped)
        ordered = sorted(
            deduped,
            key=lambda item: (
                -item.raw_score,
                item.chunk.source_kind.value,
                item.chunk.path or "",
                item.chunk.start_line or 0,
                item.chunk.end_line or 0,
                item.chunk.id,
            ),
        )
        selected: list[RetrievedContextChunk] = []
        for rank, item in enumerate(ordered[:limit], start=1):
            normalized = round(item.raw_score / max_score, 6) if max_score else 0.0
            selected.append(
                RetrievedContextChunk(
                    chunk=item.chunk,
                    score=normalized,
                    rank=rank,
                    matched_terms=list(item.matched_terms),
                )
            )
        return selected

    def _chunk_is_retrievable(self, chunk: ContextChunk) -> bool:
        if chunk.source_kind == ContextSourceKind.REPO_FILE:
            if not chunk.path:
                return False
            path = self.project_root / chunk.path
            if not path.exists() or not path.is_file():
                return False
            if is_excluded_relative(chunk.path, self.excluded_patterns):
                return False
            try:
                assert_not_secret_path(path)
            except ValueError:
                return False
        if scan_text_for_secrets(chunk.text_preview):
            return False
        if chunk.source_kind == ContextSourceKind.MEMORY_RECORD and MEMORY_NOT_AUTHORITY_WARNING not in chunk.warnings:
            return False
        return True

    def _score_chunk(self, chunk: ContextChunk, query: str, query_terms: set[str]) -> _ScoredChunk:
        haystack = chunk.text_preview.lower()
        path = (chunk.path or "").lower()
        filename = Path(chunk.path).name.lower() if chunk.path else ""
        extension = Path(chunk.path).suffix.lower().lstrip(".") if chunk.path else ""
        query_lower = query.lower()
        score = 0.0
        matched: set[str] = set()

        if query_lower and query_lower in haystack:
            score += 8.0
            matched.add(query_lower)
        if query_lower and path and query_lower in path:
            score += 5.0
            matched.add(query_lower)

        preview_terms = set(_query_terms(chunk.text_preview))
        path_terms = set(_query_terms(path))
        filename_terms = set(_query_terms(filename))
        preview_symbols = set(_symbol_terms(chunk.text_preview))

        for term in sorted(query_terms):
            if term in preview_terms:
                score += 1.25
                matched.add(term)
            if term in preview_symbols:
                score += 1.75
                matched.add(term)
            if term in path_terms:
                score += 2.5
                matched.add(term)
            if term in filename_terms:
                score += 3.5
                matched.add(term)
            if extension and term == extension:
                score += 0.75
                matched.add(term)

        if chunk.source_kind == ContextSourceKind.MEMORY_RECORD and not matched:
            score *= 0.35
        elif chunk.source_kind == ContextSourceKind.MEMORY_RECORD:
            score *= 0.85
        elif chunk.source_kind in _GENERATED_SOURCE_KINDS:
            score *= 0.75

        return _ScoredChunk(chunk=chunk, raw_score=score, matched_terms=tuple(sorted(matched)))


def _query_terms(text: str) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()
    for token in _QUERY_TOKEN_RE.findall(text.lower()):
        for part in re.split(r"[/.\-:]+", token):
            if part and part not in seen:
                seen.add(part)
                terms.append(part)
        if token and token not in seen:
            seen.add(token)
            terms.append(token)
    return terms


def _symbol_terms(text: str) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()
    for token in _SYMBOL_RE.findall(text):
        lowered = token.lower()
        if lowered not in seen:
            seen.add(lowered)
            terms.append(lowered)
    return terms


def _dedupe_scored_chunks(chunks: list[_ScoredChunk]) -> list[_ScoredChunk]:
    selected: dict[tuple[object, ...], _ScoredChunk] = {}
    for item in chunks:
        key = (
            item.chunk.source_kind.value,
            item.chunk.path,
            item.chunk.start_line,
            item.chunk.end_line,
            item.chunk.sha256,
        )
        existing = selected.get(key)
        if existing is None or _dedupe_sort_key(item) < _dedupe_sort_key(existing):
            selected[key] = item
    return list(selected.values())


def _dedupe_sort_key(item: _ScoredChunk) -> tuple[float, str]:
    return (-item.raw_score, item.chunk.id)


def _context_excludes(project_root: Path) -> list[str]:
    try:
        return list(load_config(project_root).context_excludes)
    except FileNotFoundError:
        return list(DEFAULT_CONTEXT_EXCLUDES)
