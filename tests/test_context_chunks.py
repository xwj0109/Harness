from __future__ import annotations

from harness.context_budget import HeuristicTokenBudgeter
from harness.context_chunks import (
    ARTIFACT_METADATA_CHUNK_SCHEME,
    DEFAULT_REPO_CHUNK_SCHEME,
    MEMORY_CHUNK_SCHEME,
    MEMORY_NOT_AUTHORITY_WARNING,
    rebuild_artifact_metadata_context_chunks,
    rebuild_memory_context_chunks,
    rebuild_repo_file_context_chunks,
)
from harness.memory.sqlite_store import SQLiteStore
from harness.models import ContextSourceKind


def test_context_chunks_schema_and_store_round_trip(tmp_path) -> None:
    (tmp_path / "README.md").write_text("one\ntwo\n", encoding="utf-8")
    store = SQLiteStore(tmp_path)
    store.initialize()

    written = rebuild_repo_file_context_chunks(tmp_path, store=store, budgeter=HeuristicTokenBudgeter())
    rows = store.list_context_chunks(source_kind=ContextSourceKind.REPO_FILE.value, path="README.md")

    assert len(written) == 1
    assert len(rows) == 1
    assert rows[0].id == written[0].id
    assert rows[0].schema_version == "harness.context_chunk/v1"
    assert rows[0].source_kind == ContextSourceKind.REPO_FILE
    assert rows[0].path == "README.md"
    assert rows[0].start_line == 1
    assert rows[0].end_line == 2
    assert rows[0].chunk_scheme == DEFAULT_REPO_CHUNK_SCHEME
    assert rows[0].tokenizer == "heuristic_chars_per_token"


def test_repo_file_chunking_skips_excluded_secret_and_binary_paths(tmp_path) -> None:
    (tmp_path / "README.md").write_text("safe repo notes\n", encoding="utf-8")
    (tmp_path / ".env").write_text("API_KEY=abcdef123456", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("ignored", encoding="utf-8")
    (tmp_path / ".harness").mkdir()
    (tmp_path / ".harness" / "private.txt").write_text("ignored", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "pkg.js").write_text("ignored", encoding="utf-8")
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "bundle.js").write_text("ignored", encoding="utf-8")
    (tmp_path / "binary.bin").write_bytes(b"\x00\x01")
    (tmp_path / "secret.txt").write_text("OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz", encoding="utf-8")
    store = SQLiteStore(tmp_path)
    store.initialize()

    rebuild_repo_file_context_chunks(tmp_path, store=store, budgeter=HeuristicTokenBudgeter())
    paths = {chunk.path for chunk in store.list_context_chunks(source_kind=ContextSourceKind.REPO_FILE.value)}

    assert paths == {"README.md"}


def test_rebuilding_repo_chunks_is_idempotent_and_replaces_changed_file_chunks(tmp_path) -> None:
    readme = tmp_path / "README.md"
    readme.write_text("old line\n", encoding="utf-8")
    store = SQLiteStore(tmp_path)
    store.initialize()
    budgeter = HeuristicTokenBudgeter()

    first = rebuild_repo_file_context_chunks(tmp_path, store=store, budgeter=budgeter)
    second = rebuild_repo_file_context_chunks(tmp_path, store=store, budgeter=budgeter)

    assert [chunk.id for chunk in second] == [chunk.id for chunk in first]
    assert len(store.list_context_chunks(source_kind=ContextSourceKind.REPO_FILE.value, path="README.md")) == 1

    readme.write_text("new line\n", encoding="utf-8")
    third = rebuild_repo_file_context_chunks(tmp_path, store=store, budgeter=budgeter)
    rows = store.list_context_chunks(source_kind=ContextSourceKind.REPO_FILE.value, path="README.md")

    assert len(rows) == 1
    assert rows[0].id == third[0].id
    assert rows[0].id != first[0].id
    assert rows[0].text_preview == "new line"
    assert store.stale_context_chunks(
        source_kind=ContextSourceKind.REPO_FILE.value,
        path="README.md",
        sha256_values={rows[0].sha256},
        chunk_scheme=DEFAULT_REPO_CHUNK_SCHEME,
        tokenizer=budgeter.name,
    ) == []


def test_rebuilding_repo_chunks_deletes_chunks_when_file_becomes_unindexable(tmp_path) -> None:
    notes = tmp_path / "notes.txt"
    notes.write_text("safe line\n", encoding="utf-8")
    store = SQLiteStore(tmp_path)
    store.initialize()
    budgeter = HeuristicTokenBudgeter()

    rebuild_repo_file_context_chunks(tmp_path, store=store, budgeter=budgeter)
    assert len(store.list_context_chunks(source_kind=ContextSourceKind.REPO_FILE.value, path="notes.txt")) == 1

    notes.write_text("OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz", encoding="utf-8")
    rebuild_repo_file_context_chunks(tmp_path, store=store, budgeter=budgeter)

    assert store.list_context_chunks(source_kind=ContextSourceKind.REPO_FILE.value, path="notes.txt") == []


def test_memory_chunks_include_non_authority_warning_and_forget_invalidates(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    memory = store.save_memory_note("project", str(tmp_path), "Remember local-only context.")

    chunks = rebuild_memory_context_chunks(tmp_path, store=store, budgeter=HeuristicTokenBudgeter())
    rows = store.list_context_chunks(memory_id=memory.id)

    assert len(chunks) == 1
    assert len(rows) == 1
    assert rows[0].memory_id == memory.id
    assert rows[0].chunk_scheme == MEMORY_CHUNK_SCHEME
    assert rows[0].redaction_state == "not_required"
    assert MEMORY_NOT_AUTHORITY_WARNING in rows[0].warnings
    assert rows[0].metadata["permission_granting"] is False

    store.forget_memory_record(memory.id)

    assert store.list_context_chunks(memory_id=memory.id) == []


def test_rebuilding_memory_chunks_replaces_changed_memory_summary(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    memory = store.save_memory_note("project", str(tmp_path), "first summary")
    budgeter = HeuristicTokenBudgeter()

    first = rebuild_memory_context_chunks(tmp_path, store=store, budgeter=budgeter)[0]
    with store.connect() as conn:
        conn.execute(
            "UPDATE memory_records SET summary = ?, sha256 = ?, size_bytes = ? WHERE id = ?",
            ("second summary", "manual-test-sha", len("second summary".encode("utf-8")), memory.id),
        )
    second = rebuild_memory_context_chunks(tmp_path, store=store, budgeter=budgeter)[0]
    rows = store.list_context_chunks(memory_id=memory.id)

    assert first.id != second.id
    assert len(rows) == 1
    assert rows[0].id == second.id
    assert rows[0].text_preview == "second summary"


def test_artifact_metadata_chunks_do_not_include_artifact_body(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    run = store.create_run(goal="demo", task_type="phase_1a_test")
    artifact_path = tmp_path / ".harness" / "runs" / run.id / "final_report.md"
    artifact_path.write_text("artifact body should not enter chunk cache", encoding="utf-8")
    artifact = store.register_artifact(
        run.id,
        "final_report",
        artifact_path,
        metadata={"summary": "metadata only"},
        producer="test",
        redaction_state="not_required",
    )

    chunks = rebuild_artifact_metadata_context_chunks(tmp_path, run.id, store=store, budgeter=HeuristicTokenBudgeter())
    rows = store.list_context_chunks(artifact_id=artifact.id)

    assert len(chunks) == 1
    assert len(rows) == 1
    assert rows[0].chunk_scheme == ARTIFACT_METADATA_CHUNK_SCHEME
    assert rows[0].artifact_id == artifact.id
    assert rows[0].metadata["contents_included"] is False
    assert "artifact_body_not_indexed" in rows[0].warnings
    assert "metadata only" in rows[0].text_preview
    assert "artifact body should not enter chunk cache" not in rows[0].text_preview
