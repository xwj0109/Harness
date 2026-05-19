from __future__ import annotations

from dataclasses import replace

from harness.context_budget import HeuristicTokenBudgeter
from harness.context_chunks import (
    ARTIFACT_METADATA_CHUNK_SCHEME,
    MEMORY_NOT_AUTHORITY_WARNING,
    ContextChunk,
    rebuild_artifact_metadata_context_chunks,
    rebuild_memory_context_chunks,
    rebuild_repo_file_context_chunks,
)
from harness.context_pack import pack_chat_context
from harness.context_retrieval import LexicalContextRetriever
from harness.context_vector import HybridContextRetriever, rebuild_context_vector_index
from harness.memory.sqlite_store import SQLiteStore
from harness.models import ContextSourceKind, ContextTrustLevel


def test_filename_query_ranks_matching_file_above_unrelated_docs(tmp_path) -> None:
    (tmp_path / "README.md").write_text("general project notes\n", encoding="utf-8")
    (tmp_path / "payment_router.py").write_text("def charge_card():\n    return 'ok'\n", encoding="utf-8")
    store = SQLiteStore(tmp_path)
    store.initialize()
    rebuild_repo_file_context_chunks(tmp_path, store=store, budgeter=HeuristicTokenBudgeter())

    results = LexicalContextRetriever(tmp_path, store=store).retrieve("payment_router.py", limit=3)

    assert results
    assert results[0].chunk.path == "payment_router.py"
    assert results[0].score == 1.0


def test_symbol_query_ranks_chunk_containing_symbol(tmp_path) -> None:
    (tmp_path / "router.py").write_text("def reconcile_invoice_total():\n    return 1\n", encoding="utf-8")
    (tmp_path / "notes.md").write_text("invoice notes without the function name\n", encoding="utf-8")
    store = SQLiteStore(tmp_path)
    store.initialize()
    rebuild_repo_file_context_chunks(tmp_path, store=store, budgeter=HeuristicTokenBudgeter())

    results = LexicalContextRetriever(tmp_path, store=store).retrieve("reconcile_invoice_total", limit=3)

    assert results[0].chunk.path == "router.py"
    assert "reconcile_invoice_total" in results[0].matched_terms


def test_exact_phrase_scores_above_loose_token_match(tmp_path) -> None:
    (tmp_path / "exact.md").write_text("alpha beta gamma is the phrase\n", encoding="utf-8")
    (tmp_path / "loose.md").write_text("alpha appears here and gamma appears later\n", encoding="utf-8")
    store = SQLiteStore(tmp_path)
    store.initialize()
    rebuild_repo_file_context_chunks(tmp_path, store=store, budgeter=HeuristicTokenBudgeter())

    results = LexicalContextRetriever(tmp_path, store=store).retrieve("alpha beta gamma", limit=2)

    assert [item.chunk.path for item in results] == ["exact.md", "loose.md"]
    assert results[0].score > results[1].score


def test_retrieval_respects_excludes_secret_paths_and_secret_bearing_previews(tmp_path) -> None:
    (tmp_path / "safe.txt").write_text("PUBLIC_MARKER safe content\n", encoding="utf-8")
    (tmp_path / ".env").write_text("PUBLIC_MARKER=hidden\n", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz", encoding="utf-8")
    store = SQLiteStore(tmp_path)
    store.initialize()
    rebuild_repo_file_context_chunks(tmp_path, store=store, budgeter=HeuristicTokenBudgeter())
    leaked = replace(
        store.list_context_chunks(path="safe.txt")[0],
        id="manual_secret_preview",
        path="manual_secret_preview.txt",
        text_preview="OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz",
        sha256="manual-secret-sha",
    )
    store.upsert_context_chunk(leaked)

    results = LexicalContextRetriever(tmp_path, store=store).retrieve("PUBLIC_MARKER OPENAI_API_KEY", limit=10)
    paths = {item.chunk.path for item in results}

    assert paths == {"safe.txt"}


def test_duplicate_chunks_are_deduplicated_by_source_line_and_hash(tmp_path) -> None:
    (tmp_path / "dupe.py").write_text("def important_symbol():\n    pass\n", encoding="utf-8")
    store = SQLiteStore(tmp_path)
    store.initialize()
    rebuild_repo_file_context_chunks(tmp_path, store=store, budgeter=HeuristicTokenBudgeter())
    original = store.list_context_chunks(path="dupe.py")[0]
    store.upsert_context_chunk(replace(original, id="manual_duplicate_chunk"))

    results = LexicalContextRetriever(tmp_path, store=store).retrieve("important_symbol", limit=10)

    assert [item.chunk.path for item in results] == ["dupe.py"]


def test_memory_chunks_keep_non_authority_warning_and_are_penalized_without_direct_relevance(tmp_path) -> None:
    (tmp_path / "local.py").write_text("def local_only_target():\n    return True\n", encoding="utf-8")
    store = SQLiteStore(tmp_path)
    store.initialize()
    memory = store.save_memory_note("project", str(tmp_path), "Remember deployment password rotation.")
    rebuild_repo_file_context_chunks(tmp_path, store=store, budgeter=HeuristicTokenBudgeter())
    rebuild_memory_context_chunks(tmp_path, store=store, budgeter=HeuristicTokenBudgeter())

    repo_results = LexicalContextRetriever(tmp_path, store=store).retrieve("local_only_target", limit=5)
    memory_results = LexicalContextRetriever(tmp_path, store=store).retrieve("deployment password rotation", limit=5)

    assert repo_results[0].chunk.source_kind == ContextSourceKind.REPO_FILE
    memory_hit = next(item for item in memory_results if item.chunk.memory_id == memory.id)
    assert MEMORY_NOT_AUTHORITY_WARNING in memory_hit.chunk.warnings
    assert memory_hit.chunk.trust_level == ContextTrustLevel.MEMORY


def test_pack_chat_context_query_includes_retrieved_blocks_and_preserves_pinned(tmp_path) -> None:
    (tmp_path / "target.py").write_text("def queried_symbol():\n    return 'selected'\n", encoding="utf-8")
    store = SQLiteStore(tmp_path)
    store.initialize()
    rebuild_repo_file_context_chunks(tmp_path, store=store, budgeter=HeuristicTokenBudgeter())

    manifest = pack_chat_context(tmp_path, query="queried_symbol", budgeter=HeuristicTokenBudgeter())
    payload = manifest.to_payload()

    assert any(block.kind == "harness_vocabulary" and block.role == "pinned" for block in manifest.blocks)
    retrieved = [block for block in manifest.blocks if block.kind == "retrieved_context_chunk"]
    assert retrieved
    assert retrieved[0].role == "retrieved"
    assert retrieved[0].source == "target.py"
    assert retrieved[0].score == 1.0
    assert retrieved[0].chunk_ids
    assert payload["retriever"] == "lexical_context_chunks"
    selected = payload["selected_chunks"][0]
    assert selected["path"] == "target.py"
    assert selected["source_kind"] == "repo_file"
    assert selected["trust_level"] == "untrusted_repo"
    assert selected["start_line"] == 1
    assert selected["end_line"] == 2
    assert selected["sha256"]
    assert selected["retriever"] == "lexical_context_chunks"
    assert selected["score"] == 1.0
    assert selected["compressed"] is False
    assert selected["chunk_scheme"]
    assert selected["tokenizer"]
    assert selected["provenance_id"]
    provenance = payload["context_provenance"]
    repo_provenance = next(record for record in provenance if record["id"] == selected["provenance_id"])
    assert repo_provenance["source_kind"] == "repo_file"
    assert repo_provenance["trust_level"] == "untrusted_repo"
    assert repo_provenance["path"] == "target.py"
    assert repo_provenance["sha256"] == selected["sha256"]
    assert repo_provenance["lineage"]["permission_granting"] is False
    assert repo_provenance["lineage"]["policy_authority"] is False
    assert repo_provenance["lineage"]["approval_authority"] is False
    assert "def queried_symbol" not in str(repo_provenance)
    assert payload["budget_report"]["schema_version"] == "harness.context_budget_report/v1"
    assert payload["role_summary"]["pinned"] >= 4
    assert payload["role_summary"]["retrieved"] >= 1
    assert "untrusted_context_warnings" in payload


def test_pack_chat_context_without_query_keeps_static_dynamic_context(tmp_path) -> None:
    (tmp_path / "README.md").write_text("# Demo\n", encoding="utf-8")
    store = SQLiteStore(tmp_path)
    store.initialize()
    rebuild_repo_file_context_chunks(tmp_path, store=store, budgeter=HeuristicTokenBudgeter())

    manifest = pack_chat_context(tmp_path, budgeter=HeuristicTokenBudgeter())
    payload = manifest.to_payload()
    kinds = {block.kind for block in manifest.blocks}

    assert "repo_tree" in kinds
    assert "retrieved_context_chunk" not in kinds
    assert "retriever" not in payload
    assert "selected_chunks" not in payload


def test_pack_chat_context_query_without_cache_does_not_initialize_sqlite(tmp_path) -> None:
    (tmp_path / "README.md").write_text("# Demo\n", encoding="utf-8")

    manifest = pack_chat_context(tmp_path, query="Demo", budgeter=HeuristicTokenBudgeter())
    payload = manifest.to_payload()

    assert "repo_tree" in {block.kind for block in manifest.blocks}
    assert "retriever" not in payload
    assert "selected_chunks" not in payload
    assert not (tmp_path / ".harness").exists()


def test_pack_chat_context_preserves_memory_warning_in_selected_chunk_provenance(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    memory = store.save_memory_note("project", str(tmp_path), "Remember MEMORY_NEEDLE is non-authority.")
    rebuild_memory_context_chunks(tmp_path, store=store, budgeter=HeuristicTokenBudgeter())

    manifest = pack_chat_context(tmp_path, query="MEMORY_NEEDLE", budgeter=HeuristicTokenBudgeter())
    payload = manifest.to_payload()

    selected = next(chunk for chunk in payload["selected_chunks"] if chunk["memory_id"] == memory.id)
    assert MEMORY_NOT_AUTHORITY_WARNING in selected["warnings"]
    assert MEMORY_NOT_AUTHORITY_WARNING in payload["untrusted_context_warnings"]
    provenance = next(record for record in payload["context_provenance"] if record["id"] == selected["provenance_id"])
    assert provenance["source_kind"] == "memory_record"
    assert provenance["trust_level"] == "memory"
    assert provenance["memory_id"] == memory.id
    assert MEMORY_NOT_AUTHORITY_WARNING in provenance["warnings"]
    assert provenance["lineage"]["permission_granting"] is False
    assert provenance["lineage"]["approval_authority"] is False


def test_context_provenance_preserves_distinct_chunk_trust_levels(tmp_path) -> None:
    (tmp_path / "repo.txt").write_text("SHARED_NEEDLE repo file\n", encoding="utf-8")
    store = SQLiteStore(tmp_path)
    store.initialize()
    memory = store.save_memory_note("project", str(tmp_path), "SHARED_NEEDLE memory")
    run = store.create_run(goal="artifact run", task_type="phase_1a_test")
    artifact_path = tmp_path / ".harness" / "runs" / run.id / "metadata.txt"
    artifact_path.write_text("artifact body must stay out", encoding="utf-8")
    artifact = store.register_artifact(
        run.id,
        "final_report",
        artifact_path,
        metadata={"summary": "SHARED_NEEDLE artifact metadata"},
        producer="test",
    )
    budgeter = HeuristicTokenBudgeter()
    rebuild_repo_file_context_chunks(tmp_path, store=store, budgeter=budgeter)
    rebuild_memory_context_chunks(tmp_path, store=store, budgeter=budgeter)
    rebuild_artifact_metadata_context_chunks(tmp_path, run.id, store=store, budgeter=budgeter)

    manifest = pack_chat_context(tmp_path, query="SHARED_NEEDLE", budgeter=budgeter)
    payload = manifest.to_payload()
    selected_by_kind = {chunk["source_kind"]: chunk for chunk in payload["selected_chunks"]}
    provenance_by_id = {record["id"]: record for record in payload["context_provenance"]}

    assert selected_by_kind["repo_file"]["trust_level"] == "untrusted_repo"
    assert selected_by_kind["memory_record"]["memory_id"] == memory.id
    assert selected_by_kind["memory_record"]["trust_level"] == "memory"
    assert selected_by_kind["artifact"]["artifact_id"] == artifact.id
    assert selected_by_kind["artifact"]["trust_level"] == "artifact"
    for selected in selected_by_kind.values():
        provenance = provenance_by_id[selected["provenance_id"]]
        assert provenance["source_kind"] == selected["source_kind"]
        assert provenance["trust_level"] == selected["trust_level"]
        assert provenance["lineage"]["compressed"] is False
        assert provenance["lineage"]["permission_granting"] is False
    assert selected_by_kind["artifact"]["chunk_scheme"] == ARTIFACT_METADATA_CHUNK_SCHEME
    assert "artifact body must stay out" not in str(payload["selected_chunks"])
    assert "artifact body must stay out" not in str(payload["context_provenance"])


def test_generated_chunk_provenance_keeps_generated_trust_level(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    store.upsert_context_chunk(
        ContextChunk(
            id="generated_chunk",
            source_kind=ContextSourceKind.GENERATED_PLAN,
            trust_level=ContextTrustLevel.GENERATED,
            source_id="generated_plan_1",
            sha256="generated-sha",
            size_bytes=32,
            token_count=8,
            tokenizer="heuristic_chars_per_token",
            chunk_scheme="generated-test-v1",
            text_preview="GENERATED_NEEDLE proposed plan",
            redaction_state="not_required",
            warnings=["generated_text_not_authority"],
            metadata={"permission_granting": False},
        )
    )

    payload = pack_chat_context(tmp_path, query="GENERATED_NEEDLE", budgeter=HeuristicTokenBudgeter()).to_payload()

    selected = next(chunk for chunk in payload["selected_chunks"] if chunk["source_kind"] == "generated_plan")
    provenance = next(record for record in payload["context_provenance"] if record["id"] == selected["provenance_id"])
    assert selected["trust_level"] == "generated"
    assert provenance["trust_level"] == "generated"
    assert "generated_text_not_authority" in payload["untrusted_context_warnings"]
    assert provenance["lineage"]["permission_granting"] is False


def test_hybrid_retrieval_is_opt_in_and_lexical_remains_default(tmp_path) -> None:
    (tmp_path / "symbol_file.py").write_text("def exact_symbol_name():\n    return True\n", encoding="utf-8")
    store = SQLiteStore(tmp_path)
    store.initialize()
    rebuild_repo_file_context_chunks(tmp_path, store=store, budgeter=HeuristicTokenBudgeter())
    rebuild_context_vector_index(tmp_path, store=store)

    lexical = LexicalContextRetriever(tmp_path, store=store).retrieve("exact_symbol_name", limit=3)
    hybrid_disabled = HybridContextRetriever(tmp_path, store=store, enable_dense=False).retrieve("exact_symbol_name", limit=3)
    hybrid_enabled = HybridContextRetriever(tmp_path, store=store, enable_dense=True).retrieve("exact_symbol_name", limit=3)

    assert lexical[0].chunk.path == "symbol_file.py"
    assert hybrid_disabled[0].retriever == "lexical_context_chunks"
    assert hybrid_disabled[0].chunk.path == lexical[0].chunk.path
    assert hybrid_enabled[0].chunk.path == "symbol_file.py"
