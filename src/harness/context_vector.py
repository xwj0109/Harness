from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from harness.context_chunks import ContextChunk, MEMORY_NOT_AUTHORITY_WARNING
from harness.context_retrieval import LexicalContextRetriever, RetrievedContextChunk
from harness.memory.sqlite_store import SQLiteStore
from harness.models import ContextSourceKind
from harness.paths import is_excluded_relative, resolve_project_root
from harness.security import assert_not_secret_path, scan_text_for_secrets


LOCAL_HASH_EMBEDDER_ID = "local_hash_bow_v1"
LOCAL_HASH_EMBEDDER_DIMENSION = 64
VECTOR_SCHEMA_VERSION = "harness.context_vector/v1"
_TOKEN_RE = re.compile(r"[A-Za-z0-9_./:-]+")


@dataclass(frozen=True)
class ContextVectorMetadata:
    embedding_provider_id: str
    dimension: int
    quantization: str = "float32-json"


@dataclass(frozen=True)
class VectorRecord:
    id: str
    chunk_id: str
    embedding_provider_id: str
    dimension: int
    quantization: str
    source_sha256: str
    vector: list[float]
    metadata: dict[str, Any] = field(default_factory=dict)
    schema_version: str = VECTOR_SCHEMA_VERSION

    def to_row(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "schema_version": self.schema_version,
            "chunk_id": self.chunk_id,
            "embedding_provider_id": self.embedding_provider_id,
            "dimension": self.dimension,
            "quantization": self.quantization,
            "source_sha256": self.source_sha256,
            "vector_json": json.dumps(list(self.vector), separators=(",", ":")),
            "metadata_json": json.dumps(self.metadata, sort_keys=True, default=str),
        }


@dataclass(frozen=True)
class ContextIndexHealth:
    schema_version: str
    embedding_provider_id: str
    chunk_count: int
    vector_count: int
    missing_chunk_ids: list[str]
    stale_vector_ids: list[str]
    orphan_vector_ids: list[str]

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "embedding_provider_id": self.embedding_provider_id,
            "chunk_count": self.chunk_count,
            "vector_count": self.vector_count,
            "missing_count": len(self.missing_chunk_ids),
            "stale_count": len(self.stale_vector_ids),
            "orphan_count": len(self.orphan_vector_ids),
            "missing_chunk_ids": list(self.missing_chunk_ids),
            "stale_vector_ids": list(self.stale_vector_ids),
            "orphan_vector_ids": list(self.orphan_vector_ids),
        }


class EmbeddingProvider(Protocol):
    metadata: ContextVectorMetadata

    def embed(self, text: str) -> list[float]:
        ...


class VectorIndex(Protocol):
    def search(self, query: str, *, limit: int = 8) -> list[RetrievedContextChunk]:
        ...


class LocalHashEmbeddingProvider:
    def __init__(self, *, dimension: int = LOCAL_HASH_EMBEDDER_DIMENSION) -> None:
        self.metadata = ContextVectorMetadata(
            embedding_provider_id=LOCAL_HASH_EMBEDDER_ID,
            dimension=dimension,
        )

    def embed(self, text: str) -> list[float]:
        vector = [0.0] * self.metadata.dimension
        for token in _terms(text):
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            bucket = int.from_bytes(digest[:4], "big") % self.metadata.dimension
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vector[bucket] += sign
        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0:
            return vector
        return [round(value / norm, 8) for value in vector]


class LocalVectorIndex:
    def __init__(
        self,
        project_root: Path,
        *,
        store: SQLiteStore | None = None,
        embedder: EmbeddingProvider | None = None,
    ) -> None:
        self.project_root = resolve_project_root(project_root)
        self.store = store or SQLiteStore(self.project_root)
        self.embedder = embedder or LocalHashEmbeddingProvider()

    def rebuild(self) -> list[VectorRecord]:
        chunks = [chunk for chunk in self.store.list_context_chunks() if self._chunk_is_indexable(chunk)]
        chunk_ids = {chunk.id for chunk in chunks}
        self.store.delete_context_vectors_not_in(chunk_ids, embedding_provider_id=self.embedder.metadata.embedding_provider_id)
        written: list[VectorRecord] = []
        for chunk in chunks:
            record = vector_record_for_chunk(chunk, self.embedder)
            self.store.upsert_context_vector(record)
            written.append(record)
        return written

    def health(self) -> ContextIndexHealth:
        chunks = [chunk for chunk in self.store.list_context_chunks() if self._chunk_is_indexable(chunk)]
        vectors = self.store.list_context_vectors(embedding_provider_id=self.embedder.metadata.embedding_provider_id)
        chunks_by_id = {chunk.id: chunk for chunk in chunks}
        vectors_by_chunk = {vector.chunk_id: vector for vector in vectors}
        missing = sorted(chunk_id for chunk_id in chunks_by_id if chunk_id not in vectors_by_chunk)
        stale = sorted(
            vector.id
            for vector in vectors
            if vector.chunk_id in chunks_by_id and vector.source_sha256 != chunks_by_id[vector.chunk_id].sha256
        )
        orphan = sorted(vector.id for vector in vectors if vector.chunk_id not in chunks_by_id)
        return ContextIndexHealth(
            schema_version="harness.context_index_health/v1",
            embedding_provider_id=self.embedder.metadata.embedding_provider_id,
            chunk_count=len(chunks),
            vector_count=len(vectors),
            missing_chunk_ids=missing,
            stale_vector_ids=stale,
            orphan_vector_ids=orphan,
        )

    def search(self, query: str, *, limit: int = 8) -> list[RetrievedContextChunk]:
        if not query.strip() or limit <= 0 or not self.store.db_path.exists():
            return []
        query_vector = self.embedder.embed(query)
        if not any(query_vector):
            return []
        chunks = {chunk.id: chunk for chunk in self.store.list_context_chunks() if self._chunk_is_indexable(chunk)}
        scores: list[tuple[float, ContextChunk]] = []
        for vector in self.store.list_context_vectors(embedding_provider_id=self.embedder.metadata.embedding_provider_id):
            chunk = chunks.get(vector.chunk_id)
            if chunk is None or vector.source_sha256 != chunk.sha256:
                continue
            score = _cosine(query_vector, vector.vector)
            if score > 0:
                scores.append((score, chunk))
        ordered = sorted(
            scores,
            key=lambda item: (-item[0], item[1].source_kind.value, item[1].path or "", item[1].start_line or 0, item[1].id),
        )
        if not ordered:
            return []
        max_score = ordered[0][0]
        results: list[RetrievedContextChunk] = []
        for rank, (score, chunk) in enumerate(ordered[:limit], start=1):
            results.append(
                RetrievedContextChunk(
                    chunk=chunk,
                    score=round(score / max_score, 6) if max_score else 0.0,
                    rank=rank,
                    retriever="local_dense_hash",
                    matched_terms=[],
                )
            )
        return results

    def _chunk_is_indexable(self, chunk: ContextChunk) -> bool:
        if chunk.source_kind == ContextSourceKind.REPO_FILE:
            if not chunk.path:
                return False
            path = self.project_root / chunk.path
            if not path.exists() or not path.is_file():
                return False
            try:
                assert_not_secret_path(path)
            except ValueError:
                return False
        if chunk.source_kind == ContextSourceKind.MEMORY_RECORD and MEMORY_NOT_AUTHORITY_WARNING not in chunk.warnings:
            return False
        if scan_text_for_secrets(chunk.text_preview):
            return False
        try:
            from harness.config import DEFAULT_CONTEXT_EXCLUDES, load_config

            try:
                excludes = list(load_config(self.project_root).context_excludes)
            except FileNotFoundError:
                excludes = list(DEFAULT_CONTEXT_EXCLUDES)
            if chunk.path and is_excluded_relative(chunk.path, excludes):
                return False
        except ValueError:
            return False
        return True


class HybridContextRetriever:
    def __init__(
        self,
        project_root: Path,
        *,
        store: SQLiteStore | None = None,
        enable_dense: bool = False,
        embedder: EmbeddingProvider | None = None,
    ) -> None:
        self.project_root = resolve_project_root(project_root)
        self.store = store or SQLiteStore(self.project_root)
        self.lexical = LexicalContextRetriever(self.project_root, store=self.store)
        self.dense = LocalVectorIndex(self.project_root, store=self.store, embedder=embedder)
        self.enable_dense = enable_dense

    def retrieve(self, query: str, *, limit: int = 8) -> list[RetrievedContextChunk]:
        lexical = self.lexical.retrieve(query, limit=limit)
        if not self.enable_dense:
            return lexical
        try:
            dense = self.dense.search(query, limit=limit)
        except Exception:
            return lexical
        if not dense:
            return lexical
        return _rrf_fuse(lexical, dense, limit=limit)


def vector_record_for_chunk(chunk: ContextChunk, embedder: EmbeddingProvider) -> VectorRecord:
    vector = embedder.embed(chunk.text_preview)
    record_id = _vector_id(chunk.id, embedder.metadata.embedding_provider_id)
    return VectorRecord(
        id=record_id,
        chunk_id=chunk.id,
        embedding_provider_id=embedder.metadata.embedding_provider_id,
        dimension=embedder.metadata.dimension,
        quantization=embedder.metadata.quantization,
        source_sha256=chunk.sha256,
        vector=vector,
        metadata={
            "source_kind": chunk.source_kind.value,
            "trust_level": chunk.trust_level.value,
            "path": chunk.path,
            "memory_id": chunk.memory_id,
            "artifact_id": chunk.artifact_id,
            "permission_granting": False,
            "policy_authority": False,
            "approval_authority": False,
        },
    )


def rebuild_context_vector_index(project_root: Path, *, store: SQLiteStore | None = None) -> list[VectorRecord]:
    return LocalVectorIndex(project_root, store=store).rebuild()


def context_vector_index_health(project_root: Path, *, store: SQLiteStore | None = None) -> ContextIndexHealth:
    return LocalVectorIndex(project_root, store=store).health()


def deny_remote_vector_configuration(config: dict[str, Any]) -> tuple[bool, str]:
    kind = str(config.get("kind") or config.get("provider") or config.get("vector_store") or "").casefold()
    if kind in {"", "local", "local_hash", LOCAL_HASH_EMBEDDER_ID}:
        return False, ""
    return True, "remote_or_hosted_vector_indexing_unsupported_without_explicit_future_policy"


def _rrf_fuse(
    lexical: list[RetrievedContextChunk],
    dense: list[RetrievedContextChunk],
    *,
    limit: int,
    k: int = 60,
) -> list[RetrievedContextChunk]:
    by_key: dict[tuple[object, ...], RetrievedContextChunk] = {}
    scores: dict[tuple[object, ...], float] = {}
    retrievers: dict[tuple[object, ...], list[str]] = {}
    for results in (lexical, dense):
        for rank, item in enumerate(results, start=1):
            key = _chunk_key(item.chunk)
            by_key.setdefault(key, item)
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
            if item.retriever not in retrievers.setdefault(key, []):
                retrievers[key].append(item.retriever)
    ordered = sorted(
        scores,
        key=lambda key: (-scores[key], by_key[key].chunk.source_kind.value, by_key[key].chunk.path or "", by_key[key].chunk.id),
    )
    if not ordered:
        return []
    max_score = scores[ordered[0]]
    fused: list[RetrievedContextChunk] = []
    for rank, key in enumerate(ordered[:limit], start=1):
        item = by_key[key]
        fused.append(
            RetrievedContextChunk(
                chunk=item.chunk,
                score=round(scores[key] / max_score, 6) if max_score else 0.0,
                rank=rank,
                retriever="hybrid_rrf",
                matched_terms=[f"retrievers:{','.join(retrievers[key])}"],
            )
        )
    return fused


def _chunk_key(chunk: ContextChunk) -> tuple[object, ...]:
    return (chunk.source_kind.value, chunk.path, chunk.start_line, chunk.end_line, chunk.sha256)


def _terms(text: str) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()
    for token in _TOKEN_RE.findall(text.lower()):
        for part in re.split(r"[/.\-:]+", token):
            if part and part not in seen:
                seen.add(part)
                terms.append(part)
        if token and token not in seen:
            seen.add(token)
            terms.append(token)
    return terms


def _cosine(left: list[float], right: list[float]) -> float:
    if len(left) != len(right):
        return 0.0
    return sum(a * b for a, b in zip(left, right, strict=True))


def _vector_id(chunk_id: str, provider_id: str) -> str:
    digest = hashlib.sha256(f"{provider_id}:{chunk_id}".encode("utf-8")).hexdigest()[:24]
    return f"vec_{digest}"
