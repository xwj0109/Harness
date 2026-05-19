from __future__ import annotations

from dataclasses import replace

from harness.context_budget import HeuristicTokenBudgeter
from harness.context_chunks import MEMORY_NOT_AUTHORITY_WARNING, rebuild_memory_context_chunks, rebuild_repo_file_context_chunks
from harness.context_vector import (
    HybridContextRetriever,
    LocalHashEmbeddingProvider,
    LocalVectorIndex,
    context_vector_index_health,
    deny_remote_vector_configuration,
    rebuild_context_vector_index,
)
from harness.memory.sqlite_store import SQLiteStore
from harness.models import ContextSourceKind


def test_local_hash_embedder_is_deterministic() -> None:
    embedder = LocalHashEmbeddingProvider(dimension=16)

    first = embedder.embed("alpha beta alpha")
    second = embedder.embed("alpha beta alpha")

    assert first == second
    assert len(first) == 16
    assert any(value for value in first)
    assert embedder.metadata.embedding_provider_id == "local_hash_bow_v1"


def test_rebuild_vector_index_from_context_chunks_is_idempotent(tmp_path) -> None:
    (tmp_path / "README.md").write_text("alpha beta gamma\n", encoding="utf-8")
    store = SQLiteStore(tmp_path)
    store.initialize()
    rebuild_repo_file_context_chunks(tmp_path, store=store, budgeter=HeuristicTokenBudgeter())

    first = rebuild_context_vector_index(tmp_path, store=store)
    second = rebuild_context_vector_index(tmp_path, store=store)
    rows = store.list_context_vectors()

    assert len(first) == 1
    assert [record.id for record in second] == [record.id for record in first]
    assert len(rows) == 1
    assert rows[0].chunk_id == first[0].chunk_id
    assert rows[0].embedding_provider_id == "local_hash_bow_v1"
    assert rows[0].dimension == 64
    assert rows[0].source_sha256 == first[0].source_sha256
    assert rows[0].metadata["permission_granting"] is False


def test_changed_chunks_replace_stale_vectors_and_health_reports_drift(tmp_path) -> None:
    path = tmp_path / "notes.txt"
    path.write_text("old target\n", encoding="utf-8")
    store = SQLiteStore(tmp_path)
    store.initialize()
    budgeter = HeuristicTokenBudgeter()
    rebuild_repo_file_context_chunks(tmp_path, store=store, budgeter=budgeter)
    first = rebuild_context_vector_index(tmp_path, store=store)[0]

    stale = replace(first, id="manual_stale_vector", chunk_id="manual_stale_chunk", source_sha256="stale-sha")
    store.upsert_context_vector(stale)
    health = context_vector_index_health(tmp_path, store=store).to_payload()
    assert health["orphan_count"] == 1
    assert "manual_stale_vector" in health["orphan_vector_ids"]

    path.write_text("new target\n", encoding="utf-8")
    rebuild_repo_file_context_chunks(tmp_path, store=store, budgeter=budgeter)
    rebuilt = rebuild_context_vector_index(tmp_path, store=store)

    assert len(rebuilt) == 1
    assert store.list_context_vectors()[0].source_sha256 == rebuilt[0].source_sha256
    assert context_vector_index_health(tmp_path, store=store).to_payload()["orphan_count"] == 0


def test_deleted_and_forgotten_chunks_are_removed_from_dense_search(tmp_path) -> None:
    (tmp_path / "repo.txt").write_text("repo target\n", encoding="utf-8")
    store = SQLiteStore(tmp_path)
    store.initialize()
    memory = store.save_memory_note("project", str(tmp_path), "memory target")
    budgeter = HeuristicTokenBudgeter()
    rebuild_repo_file_context_chunks(tmp_path, store=store, budgeter=budgeter)
    rebuild_memory_context_chunks(tmp_path, store=store, budgeter=budgeter)
    rebuild_context_vector_index(tmp_path, store=store)

    memory_chunks = store.list_context_chunks(memory_id=memory.id)
    assert memory_chunks
    assert store.list_context_vectors(chunk_id=memory_chunks[0].id)
    store.forget_memory_record(memory.id)
    assert store.list_context_vectors(chunk_id=memory_chunks[0].id) == []
    (tmp_path / "repo.txt").unlink()
    rebuild_repo_file_context_chunks(tmp_path, store=store, budgeter=budgeter)
    rebuild_context_vector_index(tmp_path, store=store)

    assert LocalVectorIndex(tmp_path, store=store).search("target") == []


def test_memory_vectors_keep_non_authority_warning(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    memory = store.save_memory_note("project", str(tmp_path), "Remember vector memory target.")
    rebuild_memory_context_chunks(tmp_path, store=store, budgeter=HeuristicTokenBudgeter())
    rebuild_context_vector_index(tmp_path, store=store)

    results = LocalVectorIndex(tmp_path, store=store).search("vector memory", limit=3)

    assert results
    assert results[0].chunk.memory_id == memory.id
    assert MEMORY_NOT_AUTHORITY_WARNING in results[0].chunk.warnings


def test_secret_like_and_excluded_chunks_are_not_indexed_or_returned(tmp_path) -> None:
    (tmp_path / "safe.txt").write_text("safe target\n", encoding="utf-8")
    (tmp_path / ".env").write_text("target=hidden\n", encoding="utf-8")
    store = SQLiteStore(tmp_path)
    store.initialize()
    rebuild_repo_file_context_chunks(tmp_path, store=store, budgeter=HeuristicTokenBudgeter())
    rebuild_context_vector_index(tmp_path, store=store)

    paths = {result.chunk.path for result in LocalVectorIndex(tmp_path, store=store).search("target", limit=10)}

    assert paths == {"safe.txt"}


def test_hybrid_retriever_lexical_fallback_and_exact_filename_ranking(tmp_path) -> None:
    (tmp_path / "payment_router.py").write_text("def charge_card():\n    return True\n", encoding="utf-8")
    (tmp_path / "notes.md").write_text("payment routing notes charge card\n", encoding="utf-8")
    store = SQLiteStore(tmp_path)
    store.initialize()
    rebuild_repo_file_context_chunks(tmp_path, store=store, budgeter=HeuristicTokenBudgeter())

    lexical_only = HybridContextRetriever(tmp_path, store=store, enable_dense=False).retrieve("payment_router.py", limit=3)
    assert lexical_only[0].chunk.path == "payment_router.py"

    rebuild_context_vector_index(tmp_path, store=store)
    hybrid = HybridContextRetriever(tmp_path, store=store, enable_dense=True).retrieve("payment_router.py", limit=3)
    assert hybrid[0].chunk.path == "payment_router.py"
    assert hybrid[0].retriever in {"hybrid_rrf", "lexical_context_chunks"}


def test_empty_or_missing_vector_index_falls_back_to_lexical_without_initializing(tmp_path) -> None:
    (tmp_path / "README.md").write_text("fallback target\n", encoding="utf-8")

    missing = HybridContextRetriever(tmp_path, enable_dense=True).retrieve("fallback", limit=3)
    assert missing == []
    assert not (tmp_path / ".harness").exists()

    store = SQLiteStore(tmp_path)
    store.initialize()
    rebuild_repo_file_context_chunks(tmp_path, store=store, budgeter=HeuristicTokenBudgeter())
    fallback = HybridContextRetriever(tmp_path, store=store, enable_dense=True).retrieve("fallback", limit=3)
    assert fallback[0].retriever == "lexical_context_chunks"


def test_remote_vector_configuration_fails_closed() -> None:
    denied, reason = deny_remote_vector_configuration({"kind": "qdrant"})

    assert denied is True
    assert reason == "remote_or_hosted_vector_indexing_unsupported_without_explicit_future_policy"
