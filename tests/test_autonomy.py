import json

from typer.testing import CliRunner

from harness.autonomy import (
    AutonomyDecisionStatus,
    AutonomyEvaluationInput,
    builtin_autonomy_policies,
    evaluate_autonomy,
    get_builtin_autonomy_policy,
)
from harness.cli.main import app
from harness.models import PolicyLevel


runner = CliRunner()


def test_builtin_autonomy_profiles_exist() -> None:
    profiles = builtin_autonomy_policies()

    assert sorted(profiles) == ["daemon-safe", "manual", "safe-local", "supervised-codex"]
    assert profiles["manual"].schema_version == "harness.autonomy_policy/v1"


def test_manual_profile_requires_approval_for_side_effect() -> None:
    policy = get_builtin_autonomy_policy("manual")

    decision = evaluate_autonomy(
        policy,
        AutonomyEvaluationInput(
            tool_name="create_task",
            risk="control_plane_write",
            boundary="local_control_plane",
            idempotency_key="task:create:1",
            evidence_contract="sqlite_record",
        ),
    )

    assert decision.status == AutonomyDecisionStatus.APPROVAL_REQUIRED
    assert decision.requires_human is True


def test_read_tool_auto_allowed_under_safe_local() -> None:
    policy = get_builtin_autonomy_policy("safe-local")

    decision = evaluate_autonomy(
        policy,
        AutonomyEvaluationInput(tool_name="read_file", risk="read", boundary="local_read"),
    )

    assert decision.status == AutonomyDecisionStatus.AUTO_ALLOWED
    assert decision.requires_human is False


def test_control_plane_task_creation_auto_allowed_under_safe_local() -> None:
    policy = get_builtin_autonomy_policy("safe-local")

    decision = evaluate_autonomy(
        policy,
        AutonomyEvaluationInput(
            tool_name="create_task",
            risk="control_plane_write",
            boundary="local_control_plane",
            task_type="phase_1a_test",
            idempotency_key="task:create:phase_1a_test",
            evidence_contract="sqlite_record",
        ),
    )

    assert decision.status == AutonomyDecisionStatus.AUTO_ALLOWED
    assert "task type is allowed" in " ".join(decision.reasons)


def test_dry_run_adapter_auto_allowed_under_safe_local() -> None:
    policy = get_builtin_autonomy_policy("safe-local")

    decision = evaluate_autonomy(
        policy,
        AutonomyEvaluationInput(
            tool_name="dispatch_registered_adapter",
            risk="sandboxed_execution",
            boundary="local_artifact",
            adapter_id="dry_run",
            task_type="phase_1a_test",
            idempotency_key="adapter:dry-run:1",
            evidence_contract="run_manifest",
        ),
    )

    assert decision.status == AutonomyDecisionStatus.AUTO_ALLOWED
    assert "adapter is allowed" in " ".join(decision.reasons)


def test_codex_adapter_pauses_without_hosted_approval() -> None:
    policy = get_builtin_autonomy_policy("supervised-codex")

    decision = evaluate_autonomy(
        policy,
        AutonomyEvaluationInput(
            tool_name="edit_isolated",
            risk="repo_mutation",
            boundary="hosted_provider_codex",
            adapter_id="codex_isolated_edit",
            task_type="codex_code_edit",
            requires_paid_or_hosted_boundary=True,
            requires_sandbox=True,
            sandbox_enforced=True,
            idempotency_key="codex:edit:1",
            evidence_contract="run_manifest",
        ),
    )

    assert decision.status == AutonomyDecisionStatus.APPROVAL_REQUIRED
    assert decision.requires_human is True


def test_codex_adapter_auto_allowed_with_scoped_hosted_approval() -> None:
    policy = get_builtin_autonomy_policy("supervised-codex")

    decision = evaluate_autonomy(
        policy,
        AutonomyEvaluationInput(
            tool_name="edit_isolated",
            risk="repo_mutation",
            boundary="hosted_provider_codex",
            adapter_id="codex_isolated_edit",
            task_type="codex_code_edit",
            has_scoped_approval=True,
            requires_paid_or_hosted_boundary=True,
            requires_sandbox=True,
            sandbox_enforced=True,
            idempotency_key="codex:edit:1",
            evidence_contract="run_manifest",
        ),
    )

    assert decision.status == AutonomyDecisionStatus.AUTO_ALLOWED


def test_active_repo_apply_back_not_auto_allowed() -> None:
    policy = get_builtin_autonomy_policy("supervised-codex")

    decision = evaluate_autonomy(
        policy,
        AutonomyEvaluationInput(
            tool_name="edit_isolated",
            risk="repo_mutation",
            boundary="active_repo_apply_back",
            would_mutate_active_repo=True,
            has_scoped_approval=True,
            idempotency_key="apply-back:1",
            evidence_contract="apply_back_decision",
        ),
    )

    assert decision.status == AutonomyDecisionStatus.DENIED
    assert "active repo mutation" in " ".join(decision.reasons)


def test_autonomy_cannot_broaden_effective_policy() -> None:
    policy = get_builtin_autonomy_policy("supervised-codex").model_copy(
        update={
            "allowed_tools": ["apply_back"],
            "allowed_boundaries": ["active_repo_apply_back"],
            "auto_confirm_risks": ["repo_mutation"],
            "allow_active_repo_mutation": True,
        }
    )

    decision = evaluate_autonomy(
        policy,
        AutonomyEvaluationInput(
            tool_name="apply_back",
            risk="repo_mutation",
            boundary="active_repo_apply_back",
            would_mutate_active_repo=True,
            has_scoped_approval=True,
            idempotency_key="apply-back:1",
            evidence_contract="apply_back_decision",
            effective_policy_levels={"active_repo_write": PolicyLevel.FORBIDDEN},
        ),
    )

    assert decision.status == AutonomyDecisionStatus.POLICY_MISMATCH
    assert "effective policy forbids" in " ".join(decision.reasons)


def test_autonomy_denies_unknown_adapter() -> None:
    policy = get_builtin_autonomy_policy("supervised-codex")

    decision = evaluate_autonomy(
        policy,
        AutonomyEvaluationInput(
            tool_name="dispatch_registered_adapter",
            risk="sandboxed_execution",
            boundary="local_artifact",
            adapter_id="unknown_adapter",
            task_type="phase_1a_test",
            requires_sandbox=True,
            sandbox_enforced=True,
            idempotency_key="adapter:unknown:1",
            evidence_contract="run_manifest",
        ),
    )

    assert decision.status == AutonomyDecisionStatus.DENIED
    assert "adapter is not allowed" in " ".join(decision.reasons)


def test_autonomy_respects_kill_switch() -> None:
    policy = get_builtin_autonomy_policy("safe-local")

    decision = evaluate_autonomy(
        policy,
        AutonomyEvaluationInput(
            tool_name="read_file",
            risk="read",
            boundary="local_read",
            kill_switch_active=True,
        ),
    )

    assert decision.status == AutonomyDecisionStatus.DENIED
    assert "kill switch" in " ".join(decision.reasons)


def test_autonomy_respects_adapter_breaker() -> None:
    policy = get_builtin_autonomy_policy("supervised-codex")

    decision = evaluate_autonomy(
        policy,
        AutonomyEvaluationInput(
            tool_name="dispatch_registered_adapter",
            risk="sandboxed_execution",
            boundary="local_artifact",
            adapter_id="dry_run",
            task_type="phase_1a_test",
            idempotency_key="adapter:dry-run:1",
            evidence_contract="run_manifest",
            adapter_breaker_open=True,
        ),
    )

    assert decision.status == AutonomyDecisionStatus.DENIED
    assert "breaker" in " ".join(decision.reasons)


def test_autonomy_budget_exhaustion_stops_loop() -> None:
    policy = get_builtin_autonomy_policy("safe-local")

    decision = evaluate_autonomy(
        policy,
        AutonomyEvaluationInput(
            tool_name="read_file",
            risk="read",
            boundary="local_read",
            tool_calls_used=policy.budget.max_tool_calls,
        ),
    )

    assert decision.status == AutonomyDecisionStatus.BUDGET_EXCEEDED
    assert "budget exhausted" in " ".join(decision.reasons)


def test_autonomy_requires_idempotency_for_side_effects() -> None:
    policy = get_builtin_autonomy_policy("safe-local")

    decision = evaluate_autonomy(
        policy,
        AutonomyEvaluationInput(
            tool_name="create_task",
            risk="control_plane_write",
            boundary="local_control_plane",
            task_type="phase_1a_test",
            evidence_contract="sqlite_record",
        ),
    )

    assert decision.status == AutonomyDecisionStatus.APPROVAL_REQUIRED
    assert "idempotency key" in " ".join(decision.reasons)


def test_autonomy_requires_evidence_when_policy_requires_evidence() -> None:
    policy = get_builtin_autonomy_policy("safe-local")

    decision = evaluate_autonomy(
        policy,
        AutonomyEvaluationInput(
            tool_name="create_task",
            risk="control_plane_write",
            boundary="local_control_plane",
            task_type="phase_1a_test",
            idempotency_key="task:create:phase_1a_test",
        ),
    )

    assert decision.status == AutonomyDecisionStatus.APPROVAL_REQUIRED
    assert "evidence contract" in " ".join(decision.reasons)


def test_autonomy_policy_inspect_json_cli(tmp_path) -> None:
    result = runner.invoke(
        app,
        ["autonomy", "policy", "inspect", "--project", str(tmp_path), "--profile", "safe-local", "--output", "json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema_version"] == "harness.autonomy_policy_inspect/v1"
    assert payload["ok"] is True
    assert payload["policy"]["id"] == "safe-local"
    assert "manual" in payload["available_profiles"]
    assert not (tmp_path / ".harness").exists()
