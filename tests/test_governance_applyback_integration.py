from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from typer.testing import CliRunner

from harness.cli.main import app
from harness.governance.applyback import validate_applyback_promotion, write_applyback_evidence


runner = CliRunner()


def _request(**overrides):
    payload = {
        "task_id": "task_123",
        "segment_id": "seg_1",
        "objective_id": "obj_1",
        "context_pack_hash": "ctxabc123",
        "approval_id": "appr_123",
        "allowed_paths": ["src/product/**"],
        "changed_files": ["src/product/feature.py"],
        "diff_summary": {
            "files": ["src/product/feature.py"],
            "file_count": 1,
            "added_lines": 3,
            "removed_lines": 1,
        },
        "test_evidence": {
            "task_id": "task_123",
            "segment_id": "seg_1",
            "context_pack_hash": "ctxabc123",
            "status": "pass",
            "generated_at": "2026-05-21T10:00:00Z",
        },
        "artifacts": [
            {
                "id": "artifact_123",
                "path": ".harness/runs/run_1/feature.diff",
                "metadata": {"quarantined": False},
            }
        ],
        "network_policy": {"mode": "disabled", "task_id": "task_123", "segment_id": "seg_1"},
    }
    payload.update(overrides)
    return payload


def test_applyback_allows_scoped_paths_with_fresh_bound_evidence() -> None:
    result = validate_applyback_promotion(_request(), now=datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc))

    assert result.ok is True
    assert result.verdict == "approve"
    assert result.payload["schema_version"] == "harness.governance_applyback_verdict/v1"
    assert result.payload["policy_hash"]
    assert result.payload["approval_id"] == "appr_123"
    assert result.payload["changed_files"] == ["src/product/feature.py"]
    assert result.payload["diff_summary"]["added_lines"] == 3
    assert "applyback_bound_to_segment" in result.payload["gate_ids"]
    assert "allowed_paths_respected" in result.payload["gate_ids"]
    assert "promotion_not_quarantined" in result.payload["gate_ids"]
    assert result.payload["operator_authority"]["future_authority_granted"] is False


def test_applyback_blocks_protected_paths_without_exception_evidence() -> None:
    result = validate_applyback_promotion(
        _request(
            allowed_paths=["src/harness/governance/**"],
            changed_files=["src/harness/governance/applyback.py"],
            diff_summary={"files": ["src/harness/governance/applyback.py"]},
        ),
        now=datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc),
    )

    assert result.ok is False
    assert result.verdict == "reject"
    no_protected = next(gate for gate in result.payload["hard_gates"] if gate["id"] == "no_protected_writes")
    assert no_protected["passed"] is False
    assert no_protected["details"]["missing_exception_hits"][0]["pattern"] == "src/harness/governance/**"


def test_applyback_allows_protected_path_only_with_matching_exception_evidence() -> None:
    result = validate_applyback_promotion(
        _request(
            allowed_paths=["src/harness/governance/**"],
            changed_files=["src/harness/governance/applyback.py"],
            diff_summary={"files": ["src/harness/governance/applyback.py"]},
            protected_path_exceptions=[
                {
                    "path": "src/harness/governance/applyback.py",
                    "approval_id": "appr_123",
                    "evidence_id": "review_456",
                }
            ],
        ),
        now=datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc),
    )

    assert result.ok is True
    no_protected = next(gate for gate in result.payload["hard_gates"] if gate["id"] == "no_protected_writes")
    assert no_protected["passed"] is True


def test_applyback_blocks_stale_test_evidence() -> None:
    result = validate_applyback_promotion(
        _request(
            test_evidence={
                "task_id": "task_123",
                "segment_id": "seg_1",
                "context_pack_hash": "ctxabc123",
                "status": "pass",
                "generated_at": (datetime(2026, 5, 19, 9, 0, tzinfo=timezone.utc)).isoformat().replace("+00:00", "Z"),
            }
        ),
        now=datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc),
    )

    assert result.ok is False
    gate = next(gate for gate in result.payload["hard_gates"] if gate["id"] == "test_evidence_fresh")
    assert gate["passed"] is False
    assert "test evidence is stale" in gate["details"]["reasons"]


def test_applyback_blocks_quarantined_artifacts_until_review_promotion() -> None:
    result = validate_applyback_promotion(
        _request(
            artifacts=[
                {
                    "id": "download_123",
                    "path": ".harness/governance/network/netpol/downloads/report.pdf",
                    "metadata": {"quarantined": True, "approved_for_promotion": False},
                }
            ]
        ),
        now=datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc),
    )

    assert result.ok is False
    gate = next(gate for gate in result.payload["hard_gates"] if gate["id"] == "promotion_not_quarantined")
    assert gate["passed"] is False
    assert gate["details"]["quarantined_artifacts"][0]["id"] == "download_123"


def test_applyback_cli_writes_durable_evidence_without_granting_authority(tmp_path: Path) -> None:
    input_path = tmp_path / "applyback-request.json"
    input_path.write_text(json.dumps(_request()), encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "governance",
            "applyback",
            "validate",
            "--input",
            str(input_path),
            "--project",
            str(tmp_path),
            "--output",
            "json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["path"]
    assert Path(payload["path"]).is_file()
    assert payload["policy_hash"]
    assert payload["operator_authority"]["permission_granted"] is False
    assert payload["operator_authority"]["active_repo_mutation_performed"] is False


def test_applyback_evidence_writer_records_rejection(tmp_path: Path) -> None:
    stale = datetime.now(timezone.utc) - timedelta(days=3)
    result = write_applyback_evidence(
        tmp_path,
        _request(
            test_evidence={
                "task_id": "task_123",
                "segment_id": "seg_1",
                "context_pack_hash": "ctxabc123",
                "status": "pass",
                "generated_at": stale.isoformat().replace("+00:00", "Z"),
            }
        ),
    )

    assert result.ok is False
    assert result.path is not None and result.path.is_file()
    persisted = json.loads(result.path.read_text(encoding="utf-8"))
    assert persisted["verdict"] == "reject"
