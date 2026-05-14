from __future__ import annotations

from datetime import datetime, timedelta, timezone

from typer.testing import CliRunner

from harness.approvals import ApprovalProfile, ApprovalStore
from harness.cli.main import app
from harness.memory.sqlite_store import SQLiteStore


runner = CliRunner()


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
    assert store.list()[0].revoked_at is not None


def test_scoped_hosted_approval_allows_repo_planning_under_supervised_codex(tmp_path) -> None:
    store = ApprovalStore(tmp_path)
    approval = store.add(
        backend="codex_cli",
        data_boundary="hosted_provider",
        task_types=["repo_planning"],
        duration_hours=8,
        allowed_adapters=["repo_planning"],
        allowed_objective_ids=["obj_123"],
        max_runs=3,
        autonomy_scope="supervised-codex",
    )

    found = store.find_valid(
        "codex_cli",
        "hosted_provider",
        "repo_planning",
        adapter_id="repo_planning",
        objective_id="obj_123",
        autonomy_scope="supervised-codex",
        strict_scope=True,
    )

    assert found is not None
    assert found.id == approval.id
    assert found.allowed_task_types == ["repo_planning"]
    assert found.allowed_adapters == ["repo_planning"]


def test_scoped_hosted_approval_allows_codex_isolated_edit_exact_scope(tmp_path) -> None:
    store = ApprovalStore(tmp_path)
    store.add(
        backend="codex_cli",
        data_boundary="hosted_provider",
        task_types=["codex_code_edit"],
        duration_hours=8,
        allowed_adapters=["codex_isolated_edit"],
        autonomy_scope="supervised-codex",
    )

    assert (
        store.find_valid(
            "codex_cli",
            "hosted_provider",
            "codex_code_edit",
            adapter_id="codex_isolated_edit",
            autonomy_scope="supervised-codex",
            strict_scope=True,
        )
        is not None
    )


def test_approval_scope_cannot_be_broadened_by_model(tmp_path) -> None:
    store = ApprovalStore(tmp_path)
    store.add(
        backend="codex_cli",
        data_boundary="hosted_provider",
        task_types=["repo_planning"],
        duration_hours=8,
        allowed_adapters=["repo_planning"],
        allowed_objective_ids=["obj_allowed"],
        autonomy_scope="supervised-codex",
    )

    assert (
        store.find_valid(
            "codex_cli",
            "hosted_provider",
            "codex_code_edit",
            adapter_id="codex_isolated_edit",
            objective_id="obj_allowed",
            autonomy_scope="supervised-codex",
            strict_scope=True,
        )
        is None
    )
    assert (
        store.find_valid(
            "codex_cli",
            "hosted_provider",
            "repo_planning",
            adapter_id="repo_planning",
            objective_id="obj_other",
            autonomy_scope="supervised-codex",
            strict_scope=True,
        )
        is None
    )
    assert (
        store.find_valid(
            "codex_cli",
            "hosted_provider",
            "repo_planning",
            adapter_id="repo_planning",
            objective_id="obj_allowed",
            autonomy_scope="safe-local",
            strict_scope=True,
        )
        is None
    )


def test_strict_scope_rejects_legacy_approval_without_autonomy_scope(tmp_path) -> None:
    store = ApprovalStore(tmp_path)
    store.add(
        backend="codex_cli",
        data_boundary="hosted_provider",
        task_types=["repo_planning"],
        duration_hours=8,
    )

    assert (
        store.find_valid(
            "codex_cli",
            "hosted_provider",
            "repo_planning",
            adapter_id="repo_planning",
            autonomy_scope="supervised-codex",
            strict_scope=True,
        )
        is None
    )
    assert store.find_valid("codex_cli", "hosted_provider", "repo_planning") is not None


def test_approval_max_runs_enforced(tmp_path) -> None:
    SQLiteStore(tmp_path).initialize()
    store = ApprovalStore(tmp_path)
    approval = store.add(
        backend="codex_cli",
        data_boundary="hosted_provider",
        task_types=["repo_planning"],
        duration_hours=8,
        allowed_adapters=["repo_planning"],
        max_runs=1,
        autonomy_scope="supervised-codex",
    )
    SQLiteStore(tmp_path).create_run(
        goal="already used",
        task_type="repo_planning",
        status="completed",
        approval_id=approval.id,
    )

    assert (
        store.find_valid(
            "codex_cli",
            "hosted_provider",
            "repo_planning",
            adapter_id="repo_planning",
            autonomy_scope="supervised-codex",
            strict_scope=True,
        )
        is None
    )


def test_approval_max_total_runtime_enforced(tmp_path) -> None:
    SQLiteStore(tmp_path).initialize()
    store = ApprovalStore(tmp_path)
    approval = store.add(
        backend="codex_cli",
        data_boundary="hosted_provider",
        task_types=["repo_planning"],
        duration_hours=8,
        max_total_runtime_seconds=0,
        autonomy_scope="supervised-codex",
    )
    SQLiteStore(tmp_path).create_run(
        goal="already used",
        task_type="repo_planning",
        status="completed",
        approval_id=approval.id,
    )

    assert (
        store.find_valid(
            "codex_cli",
            "hosted_provider",
            "repo_planning",
            adapter_id="repo_planning",
            autonomy_scope="supervised-codex",
            strict_scope=True,
        )
        is None
    )


def test_approvals_add_accepts_duration_hours_and_scope_fields(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0

    added = runner.invoke(
        app,
        [
            "approvals",
            "add",
            "--backend",
            "codex_cli",
            "--data-boundary",
            "hosted_provider",
            "--task-types",
            "repo_planning,codex_code_edit",
            "--duration-hours",
            "8",
            "--autonomy-scope",
            "supervised-codex",
            "--allowed-adapters",
            "repo_planning,codex_isolated_edit",
            "--allowed-objectives",
            "obj_123",
            "--max-runs",
            "2",
            "--project",
            str(tmp_path),
        ],
    )

    assert added.exit_code == 0
    approvals = ApprovalStore(tmp_path).list()
    assert len(approvals) == 1
    assert approvals[0].allowed_task_types == ["repo_planning", "codex_code_edit"]
    assert approvals[0].allowed_adapters == ["repo_planning", "codex_isolated_edit"]
    assert approvals[0].allowed_objective_ids == ["obj_123"]
    assert approvals[0].autonomy_scope == "supervised-codex"
    assert approvals[0].max_runs == 2
