from harness.config import default_config
from harness.memory.sqlite_store import SQLiteStore
from harness.models import PolicyLevel
from harness.policy import (
    effective_policy_sha256,
    resolve_agent_effective_policy,
    resolve_backend_effective_policy,
    resolve_run_effective_policy,
    resolve_task_effective_policy,
    resolve_workbench_effective_policy,
    stricter_policy_level,
)
from harness.registry import builtin_spec_registry


def test_policy_level_strictness_is_monotonic() -> None:
    assert stricter_policy_level(PolicyLevel.ALLOWED, PolicyLevel.APPROVAL_REQUIRED) == PolicyLevel.APPROVAL_REQUIRED
    assert stricter_policy_level(PolicyLevel.APPROVAL_REQUIRED, PolicyLevel.FORBIDDEN) == PolicyLevel.FORBIDDEN
    assert stricter_policy_level(PolicyLevel.FORBIDDEN, PolicyLevel.ALLOWED) == PolicyLevel.FORBIDDEN


def test_run_effective_policy_is_deterministic_and_reflects_backend_boundary(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    backend = default_config().backends["codex_cli"]
    run = store.create_run(
        goal="edit",
        task_type="codex_code_edit",
        backend=backend,
        approval_id="approval_123",
    )
    manifest = store.build_run_manifest(run.id)

    assert manifest.effective_policy is not None
    policy = manifest.effective_policy
    assert policy.levels["hosted_boundary"] == PolicyLevel.APPROVAL_REQUIRED
    assert policy.levels["paid_provider"] == PolicyLevel.FORBIDDEN
    assert "hosted_provider" in policy.required_approvals
    assert effective_policy_sha256(policy) == effective_policy_sha256(
        resolve_run_effective_policy(store.get_run(run.id), manifest.backend_descriptor)
    )


def test_task_effective_policy_records_required_approvals_without_execution(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    task = store.create_task(
        title="Approval task",
        required_approvals=["hosted_provider"],
        agent_id="code_editor",
        workbench_id="coding",
    )

    policy = resolve_task_effective_policy(task)

    assert policy.subject_kind == "task"
    assert policy.subject_id == task.id
    assert policy.levels["task_queue_execution"] == PolicyLevel.FORBIDDEN
    assert policy.levels["background_scheduling"] == PolicyLevel.FORBIDDEN
    assert policy.levels["hosted_boundary"] == PolicyLevel.APPROVAL_REQUIRED
    assert policy.required_approvals == ["hosted_provider"]


def test_builtin_agent_workbench_and_backend_policy_resolution() -> None:
    registry = builtin_spec_registry()
    agent_policy = resolve_agent_effective_policy(registry, "repo_inspector")
    workbench_policy = resolve_workbench_effective_policy(registry, "coding")
    backend_policy = resolve_backend_effective_policy(default_config().backends["paid_openai_compatible"].to_descriptor())

    assert agent_policy.levels["active_repo_write"] == PolicyLevel.FORBIDDEN
    assert workbench_policy.levels["hosted_boundary"] == PolicyLevel.APPROVAL_REQUIRED
    assert backend_policy.levels["paid_provider"] == PolicyLevel.FORBIDDEN
    assert backend_policy.levels["external_network"] == PolicyLevel.FORBIDDEN
