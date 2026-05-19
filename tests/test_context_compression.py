from __future__ import annotations

from harness.context_budget import HeuristicTokenBudgeter
from harness.context_compression import ExtractiveContextCompressor
from harness.context_pack import ContextBlock, pack_chat_context
from harness.context_chunks import rebuild_memory_context_chunks, rebuild_repo_file_context_chunks
from harness.memory.sqlite_store import SQLiteStore


def _retrieved_block(content: str) -> ContextBlock:
    return ContextBlock(
        kind="retrieved_context_chunk",
        title="Retrieved repo chunk",
        content=content,
        source="src/example.py",
        role="retrieved",
        chunk_ids=["ctx_original"],
        provenance_id="ctx_prov",
        retrieval={
            "chunk_id": "ctx_original",
            "source_kind": "repo_file",
            "trust_level": "untrusted_repo",
            "path": "src/example.py",
            "start_line": 1,
            "end_line": 200,
            "sha256": "sha",
            "retriever": "lexical_context_chunks",
            "score": 1.0,
            "compressed": False,
            "warnings": [],
        },
    )


def test_pinned_safety_policy_blocks_are_never_compressed() -> None:
    compressor = ExtractiveContextCompressor()
    pinned = ContextBlock(
        kind="security_policy_summary",
        title="Security",
        content="hosted/data-boundary work requires explicit approval",
        role="pinned",
    )

    result = compressor.compress(
        pinned,
        query="approval",
        target_tokens=2,
        budgeter=HeuristicTokenBudgeter(),
    )

    assert compressor.is_eligible(pinned) is False
    assert result.compressed is False
    assert result.content == pinned.content


def test_diffs_and_memory_warning_blocks_are_never_compressed() -> None:
    compressor = ExtractiveContextCompressor()
    diff = ContextBlock(kind="git_diff", title="Diff", content="+ changed\n" * 50, role="retrieved")
    memory = _retrieved_block("memory_not_authority\nRemember warning text.")
    memory = ContextBlock(
        **{
            **memory.__dict__,
            "retrieval": {
                **(memory.retrieval or {}),
                "source_kind": "memory_record",
                "trust_level": "memory",
                "warnings": ["memory_not_authority"],
            },
        }
    )

    assert compressor.is_eligible(diff) is False
    assert compressor.is_eligible(memory) is False


def test_retrieved_code_block_is_deterministically_extractively_compressed() -> None:
    content = "\n".join(
        [
            "copyright example",
            "def unrelated():",
            "    return 1",
            "",
            "def target_symbol():",
            "    value = 'needle'",
            "    return value",
            "",
            "def repeated():",
            "    return value",
            "def repeated():",
            "    return value",
        ]
        + [f"filler_{index} = {index}" for index in range(80)]
    )
    block = _retrieved_block(content)
    budgeter = HeuristicTokenBudgeter()

    first = ExtractiveContextCompressor().compress(block, query="target_symbol", target_tokens=35, budgeter=budgeter)
    second = ExtractiveContextCompressor().compress(block, query="target_symbol", target_tokens=35, budgeter=budgeter)

    assert first == second
    assert first.compressed is True
    assert budgeter.count(first.content) <= 35
    assert "target_symbol" in first.content
    assert "copyright example" not in first.content
    assert first.lineage["method"] == "extractive_line_window"
    assert first.lineage["original_chunk_ids"] == ["ctx_original"]
    assert first.lineage["provenance_ids"] == ["ctx_prov"]
    assert first.lineage["permission_granting"] is False
    assert first.lineage["policy_authority"] is False
    assert first.lineage["approval_authority"] is False


def test_pack_chat_context_compression_is_disabled_by_default_and_enableable(tmp_path) -> None:
    source = "\n".join(
        ["def target_symbol():", "    return 'needle'"] + [f"line_{i} = '{'x' * 120}'" for i in range(220)]
    )
    (tmp_path / "target.py").write_text(source, encoding="utf-8")
    store = SQLiteStore(tmp_path)
    store.initialize()
    budgeter = HeuristicTokenBudgeter()
    rebuild_repo_file_context_chunks(tmp_path, store=store, budgeter=budgeter)

    uncompressed = pack_chat_context(tmp_path, query="target_symbol", budgeter=budgeter, budget_chars=14_000)
    compressed = pack_chat_context(
        tmp_path,
        query="target_symbol",
        budgeter=budgeter,
        budget_chars=14_000,
        enable_compression=True,
    )

    assert "selected_chunks" not in uncompressed.to_payload() or all(
        chunk["compressed"] is False for chunk in uncompressed.to_payload()["selected_chunks"]
    )
    compressed_payload = compressed.to_payload()
    selected = compressed_payload["selected_chunks"][0]
    provenance = next(record for record in compressed_payload["context_provenance"] if record["id"] == selected["provenance_id"])
    assert selected["compressed"] is True
    assert selected["compression"]["method"] == "extractive_line_window"
    assert selected["original_chunk_ids"]
    assert compressed_payload["context_summary"]["selected_chunk_count"] >= 1
    assert compressed_payload["context_summary"]["compressed_block_ids"]
    assert "retrieved chunks" in compressed_payload["context_summary"]["source_categories"]
    assert provenance["lineage"]["compressed"] is True
    assert provenance["lineage"]["compression"]["original_chunk_ids"] == selected["original_chunk_ids"]
    assert provenance["lineage"]["permission_granting"] is False
    assert compressed_payload["budget_report"]["schema_version"] == "harness.context_budget_report/v1"
    assert compressed_payload["role_summary"]["pinned"] >= 4


def test_memory_warning_survives_when_compression_enabled(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    memory = store.save_memory_note("project", str(tmp_path), "Remember MEMORY_NEEDLE with memory_not_authority.")
    budgeter = HeuristicTokenBudgeter()
    rebuild_memory_context_chunks(tmp_path, store=store, budgeter=budgeter)

    manifest = pack_chat_context(
        tmp_path,
        query="MEMORY_NEEDLE",
        budgeter=budgeter,
        enable_compression=True,
    )
    payload = manifest.to_payload()
    selected = next(chunk for chunk in payload["selected_chunks"] if chunk["memory_id"] == memory.id)

    assert selected["compressed"] is False
    assert "memory_not_authority" in selected["warnings"]
    assert "memory_not_authority" in payload["untrusted_context_warnings"]


def test_pack_chat_context_query_without_cache_still_falls_back_with_compression_enabled(tmp_path) -> None:
    (tmp_path / "README.md").write_text("# Demo\n", encoding="utf-8")

    manifest = pack_chat_context(
        tmp_path,
        query="Demo",
        budgeter=HeuristicTokenBudgeter(),
        enable_compression=True,
    )
    payload = manifest.to_payload()

    assert "repo_tree" in {block.kind for block in manifest.blocks}
    assert "selected_chunks" not in payload
    assert not (tmp_path / ".harness").exists()
