from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from harness.cli.main import app
from harness.governance.network import (
    GovernanceNetworkPolicy,
    evaluate_network_request,
    validate_network_policy,
    write_download_quarantine_record,
    write_network_policy_check,
)


runner = CliRunner()


def _policy(**overrides) -> GovernanceNetworkPolicy:
    payload = {
        "policy_id": "netpol_demo",
        "task_id": "task_123",
        "allowed_hosts": ["docs.example.com"],
        "denied_hosts": ["169.254.169.254"],
        "allowed_protocols": ["https"],
        "allowed_methods": ["GET"],
        "proxy_endpoint": "harness-local-mediator://demo",
        "request_log_path": ".harness/governance/network/netpol_demo/network-request-log.json",
        "download_quarantine_path": ".harness/governance/network/netpol_demo/downloads",
        "approval_id": "appr_123",
        "expires_at": "2099-01-01T00:00:00Z",
        "allow_downloads": True,
        "download_quarantine": True,
        "log_requests": True,
    }
    payload.update(overrides)
    return GovernanceNetworkPolicy.model_validate(payload)


def test_network_policy_requires_approval_logging_quarantine_and_scope() -> None:
    valid = validate_network_policy(_policy())
    invalid = validate_network_policy(
        _policy(
            approval_id="",
            request_log_path="",
            download_quarantine=False,
            allowed_hosts=["169.254.169.254"],
        )
    )

    assert valid.ok is True
    assert invalid.ok is False
    assert "approval id is required" in invalid.errors
    assert "request log path is required" in invalid.errors
    assert "download quarantine is required" in invalid.errors
    assert "metadata service hosts cannot be allowlisted" in invalid.errors


def test_network_request_evaluation_enforces_allowlist_denies_credentials_and_denied_hosts() -> None:
    policy = _policy()

    allowed = evaluate_network_request(policy, "https://docs.example.com/page")
    denied_host = evaluate_network_request(policy, "https://evil.example.com/page")
    denied_credentials = evaluate_network_request(policy, "https://token@docs.example.com/page")
    denied_metadata = evaluate_network_request(_policy(allowed_hosts=["docs.example.com"], denied_hosts=["docs.example.com"]), "https://docs.example.com/page")

    assert allowed["allowed"] is True
    assert denied_host["allowed"] is False
    assert denied_host["reason"] == "host is not allowlisted"
    assert denied_credentials["allowed"] is False
    assert denied_credentials["reason"] == "URL credentials are forbidden"
    assert denied_metadata["allowed"] is False
    assert denied_metadata["reason"] == "host is explicitly denied"


def test_policy_check_and_quarantine_write_evidence_without_promotion(tmp_path: Path) -> None:
    policy = _policy()

    check = write_network_policy_check(tmp_path, policy)
    record = write_download_quarantine_record(
        tmp_path,
        policy,
        source_url="https://docs.example.com/report.pdf",
        artifact_path=".harness/runs/run_1/report.pdf",
        sha256="abc123",
    )

    assert check.ok is True
    assert check.path is not None and check.path.is_file()
    assert record["schema_version"] == "harness.governance_download_quarantine/v1"
    assert record["approved_for_promotion"] is False
    assert (tmp_path / ".harness/governance/network/netpol_demo/download-quarantine.json").is_file()


def test_network_validate_cli_writes_json_evidence(tmp_path: Path) -> None:
    policy_path = tmp_path / "network-policy.json"
    policy_path.write_text(json.dumps(_policy().model_dump(mode="json")), encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "governance",
            "network",
            "validate",
            "--policy",
            str(policy_path),
            "--project",
            str(tmp_path),
            "--output",
            "json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema_version"] == "harness.governance_network_policy_check/v1"
    assert payload["ok"] is True
    assert payload["policy"]["policy_id"] == "netpol_demo"
    assert Path(payload["path"]).is_file()


def test_network_check_url_cli_fails_closed_for_unlisted_host(tmp_path: Path) -> None:
    policy_path = tmp_path / "network-policy.json"
    policy_path.write_text(json.dumps(_policy().model_dump(mode="json")), encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "governance",
            "network",
            "check-url",
            "https://evil.example.com/page",
            "--policy",
            str(policy_path),
            "--project",
            str(tmp_path),
            "--output",
            "json",
        ],
    )

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["decision"]["allowed"] is False
    assert payload["decision"]["reason"] == "host is not allowlisted"


def test_network_quarantine_cli_records_unpromoted_download(tmp_path: Path) -> None:
    policy_path = tmp_path / "network-policy.json"
    policy_path.write_text(json.dumps(_policy().model_dump(mode="json")), encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "governance",
            "network",
            "quarantine",
            "https://docs.example.com/report.pdf",
            "--policy",
            str(policy_path),
            "--artifact-path",
            ".harness/runs/run_1/report.pdf",
            "--sha256",
            "abc123",
            "--project",
            str(tmp_path),
            "--output",
            "json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema_version"] == "harness.governance_download_quarantine/v1"
    assert payload["approved_for_promotion"] is False
    assert payload["policy_id"] == "netpol_demo"
