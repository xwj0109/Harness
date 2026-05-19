from __future__ import annotations

import json

from harness.context_budget import APPROXIMATE_TOKEN_BUDGET_WARNING, HeuristicTokenBudgeter
from harness.context_pack import pack_chat_context, pack_pinned_context, pack_static_dynamic_context
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
    assert manifest.budget_report.schema_version == "harness.context_budget_report/v1"
    assert manifest.budget_report.model_profile == "codex_cli"
    assert manifest.to_payload()["budget_report"]["used_input_tokens"] == manifest.budget_report.used_input_tokens
    assert blocks["repo_tree"].role == "retrieved"
    assert blocks["project_file"].role == "retrieved"
    assert blocks["harness_state"].role == "retrieved"
    payload = manifest.to_payload()
    assert payload["role_summary"]["pinned"] >= 4
    assert payload["role_summary"]["retrieved"] >= 3
    summary = payload["context_summary"]
    assert payload["context_snapshot"] == summary
    assert summary["selected_block_count"] == len(manifest.blocks)
    assert summary["role_counts"] == payload["role_summary"]
    assert summary["token_budget"]["used_input_tokens"] == manifest.budget_report.used_input_tokens
    assert summary["token_budget"]["max_input_tokens"] == manifest.budget_report.max_input_tokens
    assert summary["blocked_path_count"] == len(manifest.blocked_paths)
    assert summary["provenance_count"] == len(payload["context_provenance"])
    assert {"harness policy", "registry", "repo tree", "repo file"} <= set(summary["source_categories"])
    assert len(summary["selected_sources"]) == len(manifest.blocks)
    assert {block["role"] for block in payload["blocks"]} >= {"pinned", "retrieved"}
    assert payload["context_provenance"]
    assert payload["untrusted_context_warnings"]
    assert "budget_report" in payload
    assert "role_summary" in payload
    repo_record = next(record for record in payload["context_provenance"] if record["source_kind"] == "repo_file")
    assert repo_record["trust_level"] == "untrusted_repo"
    assert repo_record["lineage"]["permission_granting"] is False
    assert repo_record["lineage"]["policy_authority"] is False
    assert repo_record["lineage"]["approval_authority"] is False


def test_context_pack_includes_harness_domain_summary(tmp_path) -> None:
    manifest = pack_chat_context(tmp_path)
    blocks = _blocks_by_kind(manifest)

    assert "harness_vocabulary" in blocks
    vocabulary = blocks["harness_vocabulary"].content.lower()
    assert "objectives" in vocabulary
    assert "leases" in vocabulary
    assert "apply-back" in vocabulary
    assert "builtin_harness_domain" in blocks
    assert blocks["harness_vocabulary"].role == "pinned"
    assert blocks["builtin_harness_domain"].role == "pinned"
    domain = json.loads(blocks["builtin_harness_domain"].content)
    assert "agents" in domain
    assert "workbenches" in domain
    assert "model_profiles" in domain
    assert "tool_policies" in domain
    assert "memory_scopes" in domain
    assert "security_policy_summary" in blocks
    assert "sandbox_profiles" in blocks
    assert blocks["security_policy_summary"].role == "pinned"
    assert blocks["sandbox_profiles"].role == "pinned"
    policy = json.loads(blocks["security_policy_summary"].content)
    assert policy["schema_version"] == "harness.security_policy_summary/v1"
    assert "active_repo_write" in policy["policy_keys"]
    assert "missing_approval" in policy["blocked_state_codes"]


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


def test_context_pack_pins_memory_summary_as_non_authoritative(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    record = store.save_memory_note("project", str(tmp_path), "Remember local-only context.")

    manifest = pack_chat_context(tmp_path)
    blocks = _blocks_by_kind(manifest)

    assert "memory_summary" in blocks
    assert blocks["memory_summary"].role == "pinned"
    assert "memory_not_authority" in manifest.warnings
    payload = json.loads(blocks["memory_summary"].content)
    assert payload["warnings"] == ["memory_not_authority"]
    assert payload["recent"][0]["id"] == record.id
    assert payload["recent"][0]["summary"] == "Remember local-only context."
    manifest_payload = manifest.to_payload()
    memory_provenance = [
        item for item in manifest_payload["context_provenance"] if item["source_kind"] == "memory_record"
    ]
    assert memory_provenance
    assert "memory_not_authority" in manifest_payload["untrusted_context_warnings"]


def test_context_pack_warns_when_heuristic_budgeter_is_used(tmp_path) -> None:
    manifest = pack_chat_context(tmp_path, budgeter=HeuristicTokenBudgeter())

    assert APPROXIMATE_TOKEN_BUDGET_WARNING in manifest.warnings
    assert manifest.budget_report.approximate is True
    assert APPROXIMATE_TOKEN_BUDGET_WARNING in manifest.budget_report.warnings


def test_context_pack_truncates_oversized_block_by_token_budget(tmp_path) -> None:
    manifest = pack_chat_context(tmp_path, budget_chars=80, budgeter=HeuristicTokenBudgeter())
    first = manifest.blocks[0]

    assert first.kind == "harness_vocabulary"
    assert first.truncated is True
    assert first.token_estimate <= manifest.budget_report.max_input_tokens
    assert "[TRUNCATED: context budget]" in first.content
    assert "context_block_truncated:harness_vocabulary" in manifest.warnings
    summary = manifest.to_payload()["context_summary"]
    assert summary["selected_block_count"] == 1
    assert summary["truncated_block_ids"] == ["harness_vocabulary"]
    assert summary["warning_codes"] == manifest.warnings


def test_context_pack_adds_pinned_request_context_when_supplied(tmp_path) -> None:
    manifest = pack_chat_context(
        tmp_path,
        mode="plan",
        model_profile="codex_cli",
        session_id="sess_1",
        safety_boundaries=["No provider call from context packing."],
    )
    blocks = _blocks_by_kind(manifest)

    assert blocks["request_context"].role == "pinned"
    payload = json.loads(blocks["request_context"].content)
    assert payload["mode"] == "plan"
    assert payload["model_profile"] == "codex_cli"
    assert payload["session_id"] == "sess_1"
    assert payload["permission_granting"] is False


def test_context_pack_splits_pinned_and_static_dynamic_builders(tmp_path) -> None:
    (tmp_path / "README.md").write_text("repo notes\n", encoding="utf-8")

    pinned = [block for block in pack_pinned_context(tmp_path) if block is not None]
    dynamic = [block for block in pack_static_dynamic_context(tmp_path) if block is not None]

    assert {block.kind for block in pinned} >= {
        "harness_vocabulary",
        "builtin_harness_domain",
        "security_policy_summary",
        "sandbox_profiles",
    }
    assert {block.role for block in pinned} == {"pinned"}
    dynamic_by_kind = {block.kind: block for block in dynamic}
    assert dynamic_by_kind["repo_tree"].role == "retrieved"
    assert dynamic_by_kind["project_file"].role == "retrieved"
    assert dynamic_by_kind["harness_state"].role == "retrieved"
