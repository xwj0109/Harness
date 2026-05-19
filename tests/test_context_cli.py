import json

from typer.testing import CliRunner

from harness.cli.main import app
from harness.context_budget import HeuristicTokenBudgeter
from harness.context_chunks import rebuild_repo_file_context_chunks
from harness.memory.sqlite_store import SQLiteStore


runner = CliRunner()


def test_context_inspect_is_passive_without_initialized_project(tmp_path) -> None:
    result = runner.invoke(app, ["context", "inspect", "--project", str(tmp_path), "--output", "json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["blocks"]
    assert payload["role_summary"]["pinned"] >= 1
    assert payload["inspection"]["permission_granting"] is False
    assert payload["inspection"]["process_started"] is False
    assert payload["inspection"]["filesystem_modified"] is False
    assert payload["inspection"]["provider_call_allowed"] is False
    assert not (tmp_path / ".harness").exists()


def test_context_chunks_and_search_are_read_only_when_cache_is_missing(tmp_path) -> None:
    chunks = runner.invoke(app, ["context", "chunks", "--project", str(tmp_path), "--output", "json"])
    search = runner.invoke(app, ["context", "search", "needle", "--project", str(tmp_path), "--output", "json"])

    assert chunks.exit_code == 0, chunks.output
    assert search.exit_code == 0, search.output
    assert json.loads(chunks.output)["count"] == 0
    assert json.loads(search.output)["count"] == 0
    assert not (tmp_path / ".harness").exists()


def test_context_search_reports_cached_chunk_refs_without_raw_text(tmp_path) -> None:
    (tmp_path / "README.md").write_text("needle_symbol appears here\n", encoding="utf-8")
    store = SQLiteStore(tmp_path)
    store.initialize()
    rebuild_repo_file_context_chunks(tmp_path, store=store, budgeter=HeuristicTokenBudgeter())

    result = runner.invoke(app, ["context", "search", "needle_symbol", "--project", str(tmp_path), "--output", "json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["count"] == 1
    assert payload["results"][0]["path"] == "README.md"
    assert "text_preview" not in payload["results"][0]
    assert payload["inspection"]["filesystem_modified"] is False


def test_context_rebuild_commands_are_explicitly_marked_as_filesystem_mutating(tmp_path) -> None:
    init_result = runner.invoke(app, ["init", "--project", str(tmp_path)])
    assert init_result.exit_code == 0, init_result.output
    (tmp_path / "README.md").write_text("indexable context\n", encoding="utf-8")

    chunks = runner.invoke(app, ["context", "rebuild-chunks", "--project", str(tmp_path), "--output", "json"])
    index = runner.invoke(app, ["context", "rebuild-index", "--project", str(tmp_path), "--output", "json"])

    assert chunks.exit_code == 0, chunks.output
    assert index.exit_code == 0, index.output
    assert json.loads(chunks.output)["inspection"]["filesystem_modified"] is True
    assert json.loads(index.output)["inspection"]["filesystem_modified"] is True
    assert json.loads(index.output)["inspection"]["permission_granting"] is False


def test_context_policy_cli_fails_closed_for_hosted_destinations() -> None:
    result = runner.invoke(app, ["context", "policy", "hosted_embedding", "--output", "json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["allowed"] is False
    assert payload["code"] == "context_hosted_transmission_denied"
    assert payload["permission_granting"] is False
    assert payload["provider_call_allowed"] is False
