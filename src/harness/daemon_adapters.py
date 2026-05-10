from __future__ import annotations

from pathlib import Path

from harness.approvals import ApprovalStore
from harness.backends.codex_cli import CodexCliBackend, CodexSandboxUnavailable, CodexUnavailable
from harness.config import load_config
from harness.codex_runner import CodexReadOnlyRepoSummaryRunner, HostedBoundaryApprovalRequired, HostedSecretBlocked
from harness.memory.sqlite_store import DEFAULT_TASK_LEASE_OWNER, SQLiteStore
from harness.models import (
    BillingMode,
    DaemonReadOnlyResult,
    DataBoundary,
    ExecutionLocation,
)
from harness.policy import effective_policy_sha256, resolve_task_effective_policy
from harness.security import sanitize_for_logging


def execute_read_only_summary_lease(
    project_root: Path,
    lease_id: str,
    owner: str = DEFAULT_TASK_LEASE_OWNER,
) -> DaemonReadOnlyResult:
    store = SQLiteStore(project_root)
    lease, attempt, task = store.validate_read_only_lease_for_execution(lease_id)
    task_policy = resolve_task_effective_policy(task)
    policy_hash = effective_policy_sha256(task_policy)
    cfg = load_config(project_root)
    backend_config = cfg.backends.get("codex_cli")
    if backend_config is None:
        _record_read_only_rejection(store, lease, attempt, task, "missing_backend", ["Read-only execution requires configured codex_cli backend"])
        raise ValueError("Read-only execution requires configured codex_cli backend")
    if backend_config.metadata.billing_mode != BillingMode.SUBSCRIPTION:
        _record_read_only_rejection(store, lease, attempt, task, "unsafe_backend", ["Read-only execution requires subscription codex_cli backend"])
        raise ValueError("Read-only execution requires subscription codex_cli backend")
    if backend_config.metadata.execution_location not in {ExecutionLocation.MIXED, ExecutionLocation.HOSTED}:
        _record_read_only_rejection(store, lease, attempt, task, "unsafe_backend", ["Read-only execution requires hosted or mixed codex_cli backend"])
        raise ValueError("Read-only execution requires hosted or mixed codex_cli backend")
    if backend_config.metadata.data_boundary != DataBoundary.HOSTED_PROVIDER:
        _record_read_only_rejection(store, lease, attempt, task, "unsafe_backend", ["Read-only execution requires hosted_provider backend"])
        raise ValueError("Read-only execution requires hosted_provider backend")
    if backend_config.metadata.allow_network:
        _record_read_only_rejection(store, lease, attempt, task, "unsafe_backend", ["Read-only execution requires backend allow_network=false"])
        raise ValueError("Read-only execution requires backend allow_network=false")
    approval = ApprovalStore(project_root).find_valid("codex_cli", "hosted_provider", "read_only_repo_summary")
    if approval is None:
        _record_read_only_rejection(
            store,
            lease,
            attempt,
            task,
            "missing_hosted_approval",
            ["Missing valid hosted-provider Codex approval for read_only_repo_summary."],
        )
        raise ValueError("Missing valid hosted-provider Codex approval for read_only_repo_summary.")

    backend = CodexCliBackend(backend_config)
    backend_status = backend.preflight()
    if not backend_status.available:
        _record_read_only_rejection(
            store,
            lease,
            attempt,
            task,
            "backend_unavailable",
            [backend_status.reason or "Codex CLI is unavailable."],
        )
        raise CodexUnavailable(backend_status.reason or "Codex CLI is unavailable.")
    if not backend_status.capabilities.supports_read_only_sandbox:
        _record_read_only_rejection(
            store,
            lease,
            attempt,
            task,
            "backend_unavailable",
            ["Codex read-only sandbox is unavailable; refusing to run Codex."],
        )
        raise CodexSandboxUnavailable("Codex read-only sandbox is unavailable; refusing to run Codex.")

    backend_with_capabilities = backend_config.model_copy(update={"capabilities": backend_status.capabilities})
    run = store.start_read_only_lease_run(lease.id, backend=backend_with_capabilities, approval_id=approval.id, owner=owner)
    goal = task.title if not task.description else f"{task.title}\n\n{task.description}"
    runner = CodexReadOnlyRepoSummaryRunner(
        project_root,
        store,
        CodexCliBackend(backend_with_capabilities),
        ApprovalStore(project_root),
    )
    success = False
    failure_message: str | None = None
    try:
        runner.run_existing(run.id, goal=goal, task_type="read_only_repo_summary", approval=approval)
        run = store.get_run(run.id)
        success = run.status == "completed"
        if not success:
            failure_message = f"Read-only runner finished with run status: {run.status}"
    except (CodexUnavailable, CodexSandboxUnavailable, HostedBoundaryApprovalRequired, HostedSecretBlocked, Exception) as exc:
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
            "approval_id": approval.id,
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


def _record_read_only_rejection(
    store: SQLiteStore,
    lease,
    attempt,
    task,
    reason_code: str,
    rejection_reasons: list[str],
) -> None:
    daemon = store.ensure_daemon(owner=lease.owner)
    store.record_daemon_event(
        daemon.id,
        event_type="execution_adapter_rejected",
        message="Read-only summary adapter execution was rejected before run creation.",
        metadata={
            "lease_id": lease.id,
            "task_id": task.id,
            "attempt_id": attempt.id,
            "adapter_id": "read_only_summary",
            "reason_code": reason_code,
            "rejection_reasons": sanitize_for_logging(rejection_reasons),
        },
    )
