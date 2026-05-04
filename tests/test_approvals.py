from __future__ import annotations

from datetime import datetime, timedelta, timezone

from harness.approvals import ApprovalProfile, ApprovalStore


def test_valid_approval_profile_allows_scoped_codex_use(tmp_path) -> None:
    store = ApprovalStore(tmp_path)
    approval = store.add(
        backend="codex_cli",
        data_boundary="hosted_provider",
        task_types=["repo_planning"],
        duration_days=30,
        reason="test",
    )
    found = store.find_valid("codex_cli", "hosted_provider", "repo_planning")
    assert found is not None
    assert found.id == approval.id


def test_expired_approval_is_rejected(tmp_path) -> None:
    store = ApprovalStore(tmp_path)
    expired = ApprovalProfile(
        id="expired",
        backend="codex_cli",
        project_root=str(tmp_path),
        data_boundary="hosted_provider",
        task_types=["repo_planning"],
        expires_at=datetime.now(timezone.utc) - timedelta(days=1),
        created_at=datetime.now(timezone.utc) - timedelta(days=2),
    )
    store.save_all([expired])
    assert store.find_valid("codex_cli", "hosted_provider", "repo_planning") is None


def test_revoke_approval(tmp_path) -> None:
    store = ApprovalStore(tmp_path)
    approval = store.add("codex_cli", "hosted_provider", ["repo_planning"], 30)
    assert store.revoke(approval.id)
    assert store.find_valid("codex_cli", "hosted_provider", "repo_planning") is None

