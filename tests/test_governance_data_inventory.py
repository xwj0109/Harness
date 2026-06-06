from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from typer.testing import CliRunner

from harness.cli.main import app
from harness.governance.data_inventory import build_data_inventory, classify_generated_path
from harness.governance.reference_repositories import (
    CURATED_REFERENCE_REPOSITORIES,
    REQUIRED_REFERENCE_PATTERNS,
    build_reference_repositories_audit,
)


NOW = datetime(2026, 5, 12, tzinfo=timezone.utc)
runner = CliRunner()


def _write(root: Path, rel: str, text: str = "{}\n", *, age_days: int = 0) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    timestamp = NOW.timestamp() - age_days * 86400
    os.utime(path, (timestamp, timestamp))
    return path


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _init_git_repo(root: Path, name: str) -> Path:
    repo = root / name
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True, text=True)
    (repo / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "-c", "user.name=Harness Test", "-c", "user.email=harness@example.invalid", "commit", "-m", "init")
    return repo


def test_classifies_representative_harness_generated_paths(tmp_path: Path) -> None:
    assert classify_generated_path(tmp_path, ".harness/harness.sqlite")["data_class"] == "compact_receipt"
    assert classify_generated_path(tmp_path, ".harness/sessions/sess_1/transcript.jsonl")["data_class"] == "raw_execution_log"
    assert classify_generated_path(tmp_path, ".harness/runs/run_1/manifest.json")["data_class"] == "compact_receipt"
    assert classify_generated_path(tmp_path, ".harness/runs/run_1/stdout.log")["data_class"] == "raw_execution_log"
    assert classify_generated_path(tmp_path, ".harness/governance/merge-check/run_1/diff.patch")["data_class"] == "raw_execution_log"
    assert classify_generated_path(tmp_path, ".harness/governance/merge-check/run_1/verdict.json")["data_class"] == "compact_receipt"
    assert classify_generated_path(tmp_path, ".harness/governance/tests/run_1/test-run.json")["data_class"] == "compact_receipt"
    assert classify_generated_path(tmp_path, ".harness/tmp/isolation.manifest.json")["data_class"] == "temp_isolation_manifest"
    assert classify_generated_path(tmp_path, ".harness/reference-code/example/file.py")["data_class"] == "unknown_generated_data"


def test_data_inventory_reports_retention_but_never_cleanup_for_protected_paths(tmp_path: Path) -> None:
    _write(tmp_path, ".harness/runs/run_old/stdout.log", "ok\n", age_days=20)
    _write(tmp_path, ".harness/tmp/isolation.manifest.json", "{}\n", age_days=8)

    payload = build_data_inventory(tmp_path, now=NOW).to_dict()
    items = {item["path"]: item for item in payload["items"]}

    assert items[".harness/runs/run_old/stdout.log"]["expired"] is True
    assert items[".harness/runs/run_old/stdout.log"]["cleanup_candidate"] is False
    assert "protected_path" in items[".harness/runs/run_old/stdout.log"]["blockers"]
    assert items[".harness/tmp/isolation.manifest.json"]["expired"] is True
    assert items[".harness/tmp/isolation.manifest.json"]["cleanup_candidate"] is False
    assert payload["cleanup_proposal"]["mutation_allowed"] is False
    assert payload["summary"]["cleanup_candidate_count"] == 0


def test_secret_and_private_references_are_blockers_not_candidates(tmp_path: Path) -> None:
    _write(tmp_path, ".harness/runs/run_secret/stdout.log", "API_TOKEN=secretvalue\n", age_days=20)
    _write(tmp_path, ".harness/runs/run_private/stderr.log", "read inbox/new/drop.md\n", age_days=20)

    payload = build_data_inventory(tmp_path, now=NOW).to_dict()
    items = {item["path"]: item for item in payload["items"]}

    assert "secret_pattern" in items[".harness/runs/run_secret/stdout.log"]["blockers"]
    assert "private_reference" in items[".harness/runs/run_private/stderr.log"]["blockers"]
    assert not items[".harness/runs/run_secret/stdout.log"]["cleanup_candidate"]
    assert not items[".harness/runs/run_private/stderr.log"]["cleanup_candidate"]
    blocked = payload["cleanup_proposal"]["summary"]["blocked_items"]
    assert blocked["count"] == 2


def test_failed_run_logs_get_longer_retention_window(tmp_path: Path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    from harness.memory.sqlite_store import SQLiteStore

    store = SQLiteStore(tmp_path)
    run = store.create_run("Failure", "dev", status="failed")
    log_path = _write(tmp_path, f".harness/runs/{run.id}/stderr.log", "error\n", age_days=20)
    store.register_artifact(run.id, "stderr_log", log_path)

    payload = build_data_inventory(tmp_path, now=NOW).to_dict()
    item = next(item for item in payload["items"] if item["path"] == f".harness/runs/{run.id}/stderr.log")

    assert item["data_class"] == "raw_execution_log"
    assert item["retention_days"] == 30
    assert item["expired"] is False
    assert item["cleanup_candidate"] is False


def test_data_audit_cli_emits_json_and_does_not_mutate(tmp_path: Path) -> None:
    tracked = _write(tmp_path, ".harness/runs/run_old/stdout.log", "ok\n", age_days=20)
    before = {path.relative_to(tmp_path).as_posix(): path.stat().st_mtime_ns for path in tmp_path.rglob("*") if path.is_file()}

    result = runner.invoke(app, ["governance", "data-audit", "--project", str(tmp_path), "--output", "json"])
    after = {path.relative_to(tmp_path).as_posix(): path.stat().st_mtime_ns for path in tmp_path.rglob("*") if path.is_file()}

    assert result.exit_code == 0, result.output
    assert tracked.exists()
    assert before == after
    payload = json.loads(result.output)
    assert payload["schema_version"] == "harness.data_inventory/v1"
    assert payload["cleanup_proposal"]["schema_version"] == "harness.data_cleanup_proposal/v1"
    assert payload["cleanup_proposal"]["mode"] == "read_only_proposal"
    assert payload["cleanup_proposal"]["mutation_allowed"] is False


def test_data_audit_cli_text_output_is_compact_and_read_only(tmp_path: Path) -> None:
    _write(tmp_path, ".harness/runs/run_old/stdout.log", "ok\n", age_days=20)

    result = runner.invoke(app, ["governance", "data-audit", "--project", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "Harness data audit: read-only" in result.output
    assert "Mutation allowed: False" in result.output


def test_reference_repositories_audit_reports_metadata_without_source_bodies(tmp_path: Path) -> None:
    refs_root = tmp_path / "harness-references"
    clean_repo = _init_git_repo(refs_root, "clean")
    dirty_repo = _init_git_repo(refs_root, "dirty")
    (refs_root / "notes").mkdir()
    (dirty_repo / "scratch.txt").write_text("SHOULD_NOT_APPEAR_TOKEN=super-secret-value\n", encoding="utf-8")
    _git(clean_repo, "remote", "add", "origin", "https://token:secret@github.com/example/clean.git")

    payload = build_reference_repositories_audit(
        tmp_path / "project",
        reference_root=refs_root,
        now=NOW,
        expected_repository_names=("clean", "dirty"),
    ).to_dict()
    serialized = json.dumps(payload, sort_keys=True)
    repos = {repo["name"]: repo for repo in payload["repositories"]}

    assert payload["schema_version"] == "harness.reference_repositories_audit/v1"
    assert payload["authority"]["read_only"] is True
    assert payload["authority"]["contents_included"] is False
    assert payload["authority"]["execution_allowed"] is False
    assert payload["authority"]["model_context_allowed"] is False
    assert payload["authority"]["network_required"] is False
    assert payload["authority"]["mutation_allowed"] is False
    assert payload["summary"]["repository_count"] == 2
    assert payload["summary"]["expected_repository_count"] == 2
    assert payload["summary"]["missing_expected_repository_count"] == 0
    assert payload["summary"]["extra_repository_count"] == 0
    assert payload["summary"]["missing_git_count"] == 1
    assert payload["summary"]["dirty_repository_count"] == 1
    assert payload["summary"]["manual_review_required_count"] == 2
    assert payload["summary"]["profiled_repository_count"] == 0
    assert payload["summary"]["unprofiled_repository_count"] == 2
    assert payload["summary"]["missing_required_reference_pattern_count"] == len(REQUIRED_REFERENCE_PATTERNS)
    assert repos["clean"]["head_sha"] and len(repos["clean"]["head_sha"]) == 40
    assert repos["clean"]["remote_origin_url"] == "https://github.com/example/clean.git"
    assert repos["clean"]["curated_expected"] is True
    assert repos["clean"]["profile_present"] is False
    assert repos["dirty"]["dirty"] is True
    assert repos["dirty"]["dirty_count"] == 1
    assert repos["dirty"]["manual_review_required"] is True
    assert repos["dirty"]["contents_included"] is False
    assert "SHOULD_NOT_APPEAR_TOKEN" not in serialized
    assert "super-secret-value" not in serialized
    assert "token:secret" not in serialized


def test_reference_repositories_audit_tracks_lfs_materialization_with_git_metadata_only(tmp_path: Path) -> None:
    refs_root = tmp_path / "harness-references"
    repo = refs_root / "microsoft-agent-framework"
    (repo / ".git").mkdir(parents=True)

    def fake_git(_repo: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
        if args == ["rev-parse", "--verify", "HEAD"]:
            return subprocess.CompletedProcess(args, 0, "a" * 40 + "\n", "")
        if args == ["branch", "--show-current"]:
            return subprocess.CompletedProcess(args, 0, "main\n", "")
        if args == ["remote", "get-url", "origin"]:
            return subprocess.CompletedProcess(args, 0, "https://github.com/microsoft/agent-framework.git\n", "")
        if args == ["status", "--porcelain=v1", "--untracked-files=normal"]:
            return subprocess.CompletedProcess(args, 0, "", "")
        if args == ["lfs", "version"]:
            return subprocess.CompletedProcess(args, 0, "git-lfs/3.0.0\n", "")
        if args == ["lfs", "ls-files"]:
            return subprocess.CompletedProcess(
                args,
                0,
                "ab35a2bd18 * python/packages/lab/lightning/assets/train_math_agent.png\n"
                "8fc96ee605 - python/packages/lab/lightning/assets/train_tau2_agent.png\n",
                "",
            )
        return subprocess.CompletedProcess(args, 1, "", "unexpected command")

    payload = build_reference_repositories_audit(
        tmp_path / "project",
        reference_root=refs_root,
        now=NOW,
        runner=fake_git,
        expected_repository_names=("microsoft-agent-framework",),
    ).to_dict()

    repo_payload = payload["repositories"][0]
    assert payload["summary"]["lfs_file_count"] == 2
    assert payload["summary"]["lfs_materialized_file_count"] == 1
    assert payload["summary"]["lfs_unmaterialized_file_count"] == 1
    assert "git_lfs_files_unmaterialized" in payload["warnings"]
    assert repo_payload["profile_present"] is True
    assert repo_payload["upstream"] == "microsoft/agent-framework"
    assert "tool_contracts" in repo_payload["reference_patterns"]
    assert repo_payload["integration_role"].startswith("Multi-agent runtime reference")
    assert repo_payload["license_review_required"] is True
    assert repo_payload["lfs_materialized_file_count"] == 1
    assert repo_payload["lfs_unmaterialized_file_count"] == 1
    assert repo_payload["contents_included"] is False


def test_reference_repositories_audit_reports_curated_pattern_coverage(tmp_path: Path) -> None:
    refs_root = tmp_path / "harness-references"
    for name in CURATED_REFERENCE_REPOSITORIES:
        (refs_root / name / ".git").mkdir(parents=True)

    def fake_git(repo: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
        if args == ["rev-parse", "--verify", "HEAD"]:
            return subprocess.CompletedProcess(args, 0, ("a" * 39) + repo.name[0] + "\n", "")
        if args == ["branch", "--show-current"]:
            return subprocess.CompletedProcess(args, 0, "main\n", "")
        if args == ["remote", "get-url", "origin"]:
            return subprocess.CompletedProcess(args, 0, f"https://github.com/example/{repo.name}.git\n", "")
        if args == ["status", "--porcelain=v1", "--untracked-files=normal"]:
            return subprocess.CompletedProcess(args, 0, "", "")
        if args == ["lfs", "version"]:
            return subprocess.CompletedProcess(args, 1, "", "git-lfs unavailable")
        return subprocess.CompletedProcess(args, 1, "", "unexpected command")

    payload = build_reference_repositories_audit(
        tmp_path / "project",
        reference_root=refs_root,
        now=NOW,
        runner=fake_git,
    ).to_dict()

    assert payload["summary"]["repository_count"] == len(CURATED_REFERENCE_REPOSITORIES)
    assert payload["summary"]["profiled_repository_count"] == len(CURATED_REFERENCE_REPOSITORIES)
    assert payload["summary"]["missing_required_reference_pattern_count"] == 0
    assert payload["missing_required_reference_patterns"] == []
    assert set(payload["required_reference_patterns"]) == set(REQUIRED_REFERENCE_PATTERNS)
    assert set(REQUIRED_REFERENCE_PATTERNS).issubset(set(payload["covered_reference_patterns"]))
    assert "microsoft-agent-framework" in payload["reference_pattern_coverage"]["agent_runtime"]
    assert "temporal-sdk-python" in payload["reference_pattern_coverage"]["durable_workflow"]
    assert "opentelemetry-semantic-conventions" in payload["reference_pattern_coverage"]["observability"]
    assert "firecracker" in payload["reference_pattern_coverage"]["low_level_isolation"]
    assert payload["summary"]["contents_included"] is False
    assert payload["authority"]["model_context_allowed"] is False


def test_reference_repositories_audit_cli_emits_json_and_remains_read_only(tmp_path: Path) -> None:
    refs_root = tmp_path / "harness-references"
    repo = _init_git_repo(refs_root, "microsoft-agent-framework")
    tracked = repo / "tracked.txt"
    before = tracked.stat().st_mtime_ns

    result = runner.invoke(
        app,
        ["governance", "references-audit", "--root", str(refs_root), "--project", str(tmp_path), "--output", "json"],
    )

    assert result.exit_code == 0, result.output
    assert tracked.stat().st_mtime_ns == before
    payload = json.loads(result.output)
    assert payload["schema_version"] == "harness.reference_repositories_audit/v1"
    assert payload["summary"]["repository_count"] == 1
    assert payload["summary"]["expected_repository_count"] == len(CURATED_REFERENCE_REPOSITORIES)
    assert payload["summary"]["missing_expected_repository_count"] == len(CURATED_REFERENCE_REPOSITORIES) - 1
    assert payload["summary"]["missing_required_reference_pattern_count"] > 0
    assert payload["summary"]["contents_included"] is False
    assert payload["authority"]["mutation_allowed"] is False
    assert payload["repositories"][0]["name"] == "microsoft-agent-framework"


def test_reference_repositories_audit_cli_text_is_compact_and_non_authoritative(tmp_path: Path) -> None:
    refs_root = tmp_path / "harness-references"
    _init_git_repo(refs_root, "microsoft-agent-framework")

    result = runner.invoke(
        app,
        ["governance", "references-audit", "--root", str(refs_root), "--project", str(tmp_path)],
    )

    assert result.exit_code == 0, result.output
    assert "Harness reference repositories audit: read-only" in result.output
    assert f"Expected curated repositories: {len(CURATED_REFERENCE_REPOSITORIES)}" in result.output
    assert f"Missing expected repositories: {len(CURATED_REFERENCE_REPOSITORIES) - 1}" in result.output
    assert f"Required reference patterns: {len(REQUIRED_REFERENCE_PATTERNS)}" in result.output
    assert "Missing required patterns:" in result.output
    assert "Contents included: False" in result.output
    assert "Model context allowed: False" in result.output
    assert "Execution allowed: False" in result.output
    assert "Mutation allowed: False" in result.output
