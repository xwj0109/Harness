from __future__ import annotations

from pathlib import Path

from harness.backends.local_openai import LocalEndpointUnavailable, LocalOpenAICompatibleBackend
from harness.config import load_config
from harness.memory.sqlite_store import DEFAULT_TASK_LEASE_OWNER, SQLiteStore
from harness.models import (
    BillingMode,
    DaemonReadOnlyResult,
    DataBoundary,
    ExecutionLocation,
)
from harness.policy import effective_policy_sha256, resolve_task_effective_policy
from harness.runner import ReadOnlyRepoSummaryRunner
from harness.security import sanitize_for_logging


def execute_read_only_summary_lease(
    project_root: Path,
    lease_id: str,
    owner: str = DEFAULT_TASK_LEASE_OWNER,
) -> DaemonReadOnlyResult:
    store = SQLiteStore(project_root)
    lease, attempt, task = store.validate_read_only_lease_for_execution(lease_id)
    task_policy = resolve_task_effective_policy(task)
    if task_policy.required_approvals:
        raise ValueError(
            "Read-only execution rejected: policy requires approvals "
            f"{', '.join(task_policy.required_approvals)}"
        )
    policy_hash = effective_policy_sha256(task_policy)
    cfg = load_config(project_root)
    backend_config = cfg.backends.get("local_openai_compatible")
    if backend_config is None:
        raise ValueError("Read-only execution requires configured local_openai_compatible backend")
    if backend_config.metadata.billing_mode != BillingMode.LOCAL_NO_API_COST:
        raise ValueError("Read-only execution requires local_no_api_cost backend")
    if backend_config.metadata.execution_location != ExecutionLocation.LOCAL_MACHINE:
        raise ValueError("Read-only execution requires local_machine backend")
    if backend_config.metadata.data_boundary != DataBoundary.LOCAL_ONLY:
        raise ValueError("Read-only execution requires local_only backend")
    if backend_config.metadata.allow_network:
        raise ValueError("Read-only execution requires backend allow_network=false")

    backend = LocalOpenAICompatibleBackend(backend_config)
    backend_status = backend.preflight()
    if not backend_status.available:
        raise LocalEndpointUnavailable(backend_status.reason or "Local backend unavailable.")

    run = store.start_read_only_lease_run(lease.id, backend=backend_config, owner=owner)
    goal = task.title if not task.description else f"{task.title}\n\n{task.description}"
    runner = ReadOnlyRepoSummaryRunner(project_root, cfg, store, backend)
    success = False
    failure_message: str | None = None
    try:
        runner.run_existing(run.id, goal=goal, task_type="read_only_repo_summary")
        run = store.get_run(run.id)
        success = run.status == "completed"
        if not success:
            failure_message = f"Read-only runner finished with run status: {run.status}"
    except Exception as exc:
        failure_message = str(sanitize_for_logging(str(exc)))
        store.update_run_status(run.id, "failed")
    store.finish_read_only_lease_run(
        lease.id,
        run_id=run.id,
        owner=owner,
        success=success,
        failure_code=None if success else "read_only_execution_failed",
        failure_message=failure_message,
    )
    daemon = store.ensure_daemon(owner=lease.owner)
    decision = "read_only_summary_completed" if success else "read_only_summary_failed"
    store.record_daemon_event(
        daemon.id,
        event_type="execute_read_only",
        message="Read-only summary execution linked lease to run evidence.",
        metadata={
            "lease_id": lease.id,
            "attempt_id": attempt.id,
            "task_id": task.id,
            "run_id": run.id,
            "decision": decision,
            "policy_sha256": policy_hash,
        },
    )
    store.write_run_manifest(run.id)
    result = DaemonReadOnlyResult(
        decision=decision,
        project_root=store.project_root,
        task=store.get_task(task.id),
        attempt=store.get_task_attempt(attempt.id),
        lease=store.get_task_lease(lease.id),
        run=store.get_run(run.id),
        manifest=store.build_run_manifest(run.id),
        policy_sha256=policy_hash,
        errors=[] if success else [failure_message or "Read-only execution failed."],
    )
    if not success:
        raise ValueError(result.errors[0])
    return result
