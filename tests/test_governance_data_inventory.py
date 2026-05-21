from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from typer.testing import CliRunner

from harness.cli.main import app
from harness.governance.data_inventory import build_data_inventory, classify_generated_path


NOW = datetime(2026, 5, 12, tzinfo=timezone.utc)
runner = CliRunner()


def _write(root: Path, rel: str, text: str = "{}\n", *, age_days: int = 0) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    timestamp = NOW.timestamp() - age_days * 86400
    os.utime(path, (timestamp, timestamp))
    return path


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
