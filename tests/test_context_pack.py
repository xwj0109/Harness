from __future__ import annotations

import json

from harness.context_pack import pack_chat_context
from harness.memory.sqlite_store import SQLiteStore


def _blocks_by_kind(manifest):
    return {block.kind: block for block in manifest.blocks}


def test_context_pack_includes_readme_tree_diff_without_initializing(tmp_path) -> None:
    (tmp_path / "README.md").write_text("# Demo\n\nHarness repo notes.", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('hello')\n", encoding="utf-8")

    manifest = pack_chat_context(tmp_path)
    blocks = _blocks_by_kind(manifest)

    assert blocks["repo_tree"].content
    assert "README.md" in blocks["repo_tree"].content
    assert "src/app.py" in blocks["repo_tree"].content
    assert blocks["project_file"].source == "README.md"
    assert "Harness repo notes" in blocks["project_file"].content
    assert "harness_state" in blocks
    assert not (tmp_path / ".harness").exists()


def test_context_pack_includes_harness_domain_summary(tmp_path) -> None:
    manifest = pack_chat_context(tmp_path)
    blocks = _blocks_by_kind(manifest)

    assert "harness_vocabulary" in blocks
    vocabulary = blocks["harness_vocabulary"].content.lower()
    assert "objectives" in vocabulary
    assert "leases" in vocabulary
    assert "apply-back" in vocabulary
    assert "builtin_harness_domain" in blocks
    domain = json.loads(blocks["builtin_harness_domain"].content)
    assert "agents" in domain
    assert "workbenches" in domain
    assert "model_profiles" in domain
    assert "tool_policies" in domain
    assert "memory_scopes" in domain
    assert "security_policy_summary" in blocks
    assert "sandbox_profiles" in blocks


def test_context_pack_obeys_excludes_and_blocks_secret_paths(tmp_path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("secret-ish git internals", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "pkg.js").write_text("ignored", encoding="utf-8")
    (tmp_path / ".env").write_text("API_KEY=abcdef123456", encoding="utf-8")
    (tmp_path / "safe.txt").write_text("safe", encoding="utf-8")

    manifest = pack_chat_context(tmp_path)
    tree = _blocks_by_kind(manifest)["repo_tree"].content

    assert "safe.txt" in tree
    assert ".git/config" not in tree
    assert "node_modules/pkg.js" not in tree
    assert ".env" not in tree
    assert ".env" in manifest.blocked_paths


def test_context_pack_includes_artifact_metadata_without_artifact_body(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    run = store.create_run(goal="demo run", task_type="phase_1a_test")
    artifact_path = tmp_path / ".harness" / "runs" / run.id / "secret_report.md"
    artifact_path.write_text("artifact body should not enter chat context", encoding="utf-8")
    artifact = store.register_artifact(
        run.id,
        "final_report",
        artifact_path,
        producer="test",
        redaction_state="redacted",
    )

    manifest = pack_chat_context(tmp_path)
    blocks = _blocks_by_kind(manifest)

    assert "recent_artifacts" in blocks
    payload = json.loads(blocks["recent_artifacts"].content)
    assert payload[0]["run"]["id"] == run.id
    assert payload[0]["artifacts"][0]["id"] == artifact.id
    assert payload[0]["artifacts"][0]["kind"] == "final_report"
    assert "artifact body should not enter chat context" not in blocks["recent_artifacts"].content
