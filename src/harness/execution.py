from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel

from harness.approvals import ApprovalStore
from harness.backends.codex_cli import CodexCliBackend
from harness.backends.local_openai import LocalEndpointUnavailable
from harness.codex_edit_runner import CodexCodeEditRunner
from harness.config import load_config
from harness.daemon_adapters import execute_read_only_summary_lease
from harness.memory.sqlite_store import (
    DEFAULT_TASK_LEASE_OWNER,
    DRY_RUN_EXECUTION_ADAPTER,
    DRY_RUN_TASK_TYPE,
    READ_ONLY_EXECUTION_ADAPTER,
    READ_ONLY_TASK_TYPE,
    SQLiteStore,
)
from harness.models import (
    BackendKind,
    BillingMode,
    DataBoundary,
    DaemonExecuteResult,
    ExecutionLocation,
    ExecutionAdapterDescriptor,
    RunManifest,
    TaskAttempt,
    TaskLease,
    TaskLeaseStatus,
    TaskRecord,
    TaskStatus,
    ToolReplayPolicy,
)
from harness.policy import effective_policy_sha256, resolve_task_effective_policy
from harness.security import sanitize_for_logging


EXECUTION_ADAPTER_REJECTED = "execution_adapter_rejected"
EXECUTION_DUPLICATE_REJECTED = "execution_duplicate_rejected"
CODEX_ISOLATED_EDIT_ADAPTER = "codex_isolated_edit"
CODEX_CODE_EDIT_TASK_TYPE = "codex_code_edit"


class ExecutionAdapter(Protocol):
    id: str
    descriptor: ExecutionAdapterDescriptor

    def inspect_eligibility(
        self,
        project_root: Path,
        lease: TaskLease,
        task: TaskRecord | None,
        attempt: TaskAttempt | None,
    ) -> dict[str, Any]:
        ...

    def execute(
        self,
        project_root: Path,
        lease_id: str,
        owner: str = DEFAULT_TASK_LEASE_OWNER,
    ) -> DaemonExecuteResult:
        ...


class DryRunExecutionAdapter:
    id = DRY_RUN_EXECUTION_ADAPTER
    descriptor = ExecutionAdapterDescriptor(
        id=DRY_RUN_EXECUTION_ADAPTER,
        description="Create local dry-run daemon evidence without invoking tools, backends, Docker, shell, network, hosted providers, or paid providers.",
        supported_task_types=[DRY_RUN_TASK_TYPE],
        required_task_metadata={"execution_adapter": DRY_RUN_EXECUTION_ADAPTER, "task_type": DRY_RUN_TASK_TYPE},
        rejected_task_metadata=[
            "daemon_policy_forbidden",
            "requires_active_repo_write",
            "requires_external_network",
            "requires_docker",
            "requires_paid_provider",
            "requires_hosted_boundary",
        ],
        required_approvals=[],
        backend_requirements=[],
        sandbox_requirements=[],
        side_effect_summary="Writes harness run/task/lease/artifact evidence only.",
        replay_policy=ToolReplayPolicy.IDEMPOTENT_WITH_KEY,
        safety_notes=[
            "Descriptors are documentation and validation metadata, not permission grants.",
            "Execution still requires an active lease and exact task metadata.",
        ],
    )

    def inspect_eligibility(
        self,
        project_root: Path,
        lease: TaskLease,
        task: TaskRecord | None,
        attempt: TaskAttempt | None,
    ) -> dict[str, Any]:
        return _base_adapter_eligibility(self.descriptor, lease, task, attempt)

    def execute(
        self,
        project_root: Path,
        lease_id: str,
        owner: str = DEFAULT_TASK_LEASE_OWNER,
    ) -> DaemonExecuteResult:
        result = SQLiteStore(project_root).execute_dry_run_lease(lease_id, owner=owner)
        return _generic_result_from_adapter_result(
            adapter_id=self.id,
            decision=result.decision,
            project_root=project_root,
            result=result,
        )


class ReadOnlySummaryExecutionAdapter:
    id = READ_ONLY_EXECUTION_ADAPTER
    descriptor = ExecutionAdapterDescriptor(
        id=READ_ONLY_EXECUTION_ADAPTER,
        description="Execute the bounded read-only repository summary adapter through the configured local-only, no-cost backend and read-only tools.",
        supported_task_types=[READ_ONLY_TASK_TYPE],
        required_task_metadata={"execution_adapter": READ_ONLY_EXECUTION_ADAPTER, "task_type": READ_ONLY_TASK_TYPE},
        rejected_task_metadata=[
            "daemon_policy_forbidden",
            "requires_active_repo_write",
            "requires_external_network",
            "requires_docker",
            "requires_paid_provider",
            "requires_hosted_boundary",
        ],
        required_approvals=[],
        backend_requirements=[
            "local_openai_compatible backend",
            "billing_mode=local_no_api_cost",
            "execution_location=local_machine",
            "data_boundary=local_only",
            "allow_network=false",
        ],
        sandbox_requirements=[],
        side_effect_summary="Writes harness run/task/lease/artifact evidence and executes read-only repository tools.",
        replay_policy=ToolReplayPolicy.REQUIRES_FRESH_APPROVAL,
        safety_notes=[
            "Descriptors are documentation and validation metadata, not permission grants.",
            "Execution still performs backend, policy, lease, and exact metadata checks.",
        ],
    )

    def inspect_eligibility(
        self,
        project_root: Path,
        lease: TaskLease,
        task: TaskRecord | None,
        attempt: TaskAttempt | None,
    ) -> dict[str, Any]:
        return _base_adapter_eligibility(self.descriptor, lease, task, attempt)

    def execute(
        self,
        project_root: Path,
        lease_id: str,
        owner: str = DEFAULT_TASK_LEASE_OWNER,
    ) -> DaemonExecuteResult:
        result = execute_read_only_summary_lease(project_root, lease_id, owner=owner)
        return _generic_result_from_adapter_result(
            adapter_id=self.id,
            decision=result.decision,
            project_root=project_root,
            result=result,
        )


class CodexIsolatedEditExecutionAdapter:
    id = CODEX_ISOLATED_EDIT_ADAPTER
    descriptor = ExecutionAdapterDescriptor(
        id=CODEX_ISOLATED_EDIT_ADAPTER,
        description="Run Codex as a supervised external agent inside an isolated workspace, then inspect and optionally apply back validated changes.",
        supported_task_types=[CODEX_CODE_EDIT_TASK_TYPE],
        required_task_metadata={"execution_adapter": CODEX_ISOLATED_EDIT_ADAPTER, "task_type": CODEX_CODE_EDIT_TASK_TYPE},
        rejected_task_metadata=[
            "daemon_policy_forbidden",
            "requires_active_repo_write",
            "requires_external_network",
            "requires_docker",
            "requires_paid_provider",
            "requires_hosted_boundary",
        ],
        required_approvals=["hosted_provider_codex"],
        backend_requirements=[
            "codex_cli backend",
            "kind=external_agent",
            "data_boundary=hosted_provider",
            "billing_mode=subscription",
            "allow_network=false",
        ],
        sandbox_requirements=["Codex CLI --cd support", "Codex CLI workspace-write sandbox support"],
        side_effect_summary="Writes harness evidence and isolated workspace files; active repo mutation only after separate apply-back approval.",
        replay_policy=ToolReplayPolicy.REQUIRES_FRESH_APPROVAL,
        safety_notes=[
            "Descriptors are documentation and validation metadata, not permission grants.",
            "Hosted-boundary approval is validated before run creation.",
            "A leased task is not apply-back approval; apply-back remains denied by default.",
        ],
    )

    def inspect_eligibility(
        self,
        project_root: Path,
        lease: TaskLease,
        task: TaskRecord | None,
        attempt: TaskAttempt | None,
    ) -> dict[str, Any]:
        return _base_adapter_eligibility(self.descriptor, lease, task, attempt)

    def execute(
        self,
        project_root: Path,
        lease_id: str,
        owner: str = DEFAULT_TASK_LEASE_OWNER,
    ) -> DaemonExecuteResult:
        store = SQLiteStore(project_root)
        lease, attempt, task = store.validate_execution_lease_for_run(lease_id)
        try:
            _validate_task_against_descriptor(self.descriptor, task)
        except ValueError as exc:
            reason = str(sanitize_for_logging(str(exc)))
            _record_adapter_rejection(
                store,
                lease=lease,
                task=task,
                attempt=attempt,
                adapter_id=self.id,
                reason_code="unsafe_metadata",
                rejection_reasons=[reason],
            )
            return _rejected_result(store, lease, task, attempt, self.id, "codex_isolated_edit_blocked_policy", [reason])

        approval = ApprovalStore(project_root).find_valid("codex_cli", "hosted_provider", CODEX_CODE_EDIT_TASK_TYPE)
        if approval is None:
            reason = "Missing valid hosted-provider Codex approval for codex_code_edit."
            _record_adapter_rejection(
                store,
                lease=lease,
                task=task,
                attempt=attempt,
                adapter_id=self.id,
                reason_code="missing_hosted_approval",
                rejection_reasons=[reason],
            )
            return _rejected_result(store, lease, task, attempt, self.id, "codex_isolated_edit_blocked_policy", [reason])

        cfg = load_config(project_root)
        backend_config = cfg.backends.get("codex_cli")
        backend_reasons = _codex_backend_rejection_reasons(backend_config)
        if backend_reasons:
            _record_adapter_rejection(
                store,
                lease=lease,
                task=task,
                attempt=attempt,
                adapter_id=self.id,
                reason_code="unsafe_backend",
                rejection_reasons=backend_reasons,
            )
            return _rejected_result(store, lease, task, attempt, self.id, "codex_isolated_edit_blocked_policy", backend_reasons)

        backend = CodexCliBackend(backend_config)
        status = backend.preflight()
        if not status.available:
            reason = status.reason or "Codex CLI is unavailable."
            _record_adapter_rejection(
                store,
                lease=lease,
                task=task,
                attempt=attempt,
                adapter_id=self.id,
                reason_code="backend_unavailable",
                rejection_reasons=[reason],
            )
            return DaemonExecuteResult(
                ok=False,
                decision="codex_isolated_edit_failed",
                adapter_id=self.id,
                project_root=store.project_root,
                task=task,
                attempt=attempt,
                lease=lease,
                policy_sha256=effective_policy_sha256(resolve_task_effective_policy(task)),
                errors=[reason],
            )

        backend_with_capabilities = backend_config.model_copy(update={"capabilities": status.capabilities})
        run = store.start_attempt_run(
            lease.id,
            task_type=CODEX_CODE_EDIT_TASK_TYPE,
            backend=backend_with_capabilities,
            approval_id=approval.id,
            owner=owner,
        )
        runner = CodexCodeEditRunner(
            project_root,
            store,
            CodexCliBackend(backend_with_capabilities),
            ApprovalStore(project_root),
        )
        goal = task.title if not task.description else f"{task.title}\n\n{task.description}"
        keep_isolation = bool(task.metadata.get("keep_isolation", False))
        try:
            adapter_payload = runner.run_existing(
                run_id=run.id,
                goal=goal,
                task_type=CODEX_CODE_EDIT_TASK_TYPE,
                approval=approval,
                keep_isolation=keep_isolation,
            )
            decision, success, run_status = _codex_decision_from_status(str(adapter_payload.get("status", "")))
            store.finish_attempt_run(
                lease.id,
                run_id=run.id,
                owner=owner,
                success=success,
                decision=decision,
                run_status=run_status,
                failure_code=None if success else "codex_isolated_edit_failed",
                failure_message=None if success else decision,
            )
            daemon = store.ensure_daemon(owner=lease.owner)
            store.record_daemon_event(
                daemon.id,
                event_type="execute_codex_isolated_edit",
                message="Codex isolated edit adapter linked lease to run evidence.",
                metadata={
                    "lease_id": lease.id,
                    "attempt_id": attempt.id,
                    "task_id": task.id,
                    "run_id": run.id,
                    "decision": decision,
                    "approval_id": approval.id,
                },
            )
            store.write_run_manifest(run.id)
            return DaemonExecuteResult(
                ok=success,
                decision=decision,
                adapter_id=self.id,
                project_root=store.project_root,
                task=store.get_task(task.id),
                attempt=store.get_task_attempt(attempt.id),
                lease=store.get_task_lease(lease.id),
                run=store.get_run(run.id),
                manifest=store.build_run_manifest(run.id),
                policy_sha256=effective_policy_sha256(resolve_task_effective_policy(task)),
                approval_id=approval.id,
                errors=[] if success else [decision],
                adapter_result=sanitize_for_logging(adapter_payload),
            )
        except Exception as exc:
            reason = str(sanitize_for_logging(str(exc)))
            store.finish_attempt_run(
                lease.id,
                run_id=run.id,
                owner=owner,
                success=False,
                decision="codex_isolated_edit_failed",
                run_status="failed",
                failure_code="codex_isolated_edit_failed",
                failure_message=reason,
            )
            daemon = store.ensure_daemon(owner=lease.owner)
            store.record_daemon_event(
                daemon.id,
                event_type="execute_codex_isolated_edit",
                message="Codex isolated edit adapter failed after run creation.",
                metadata={
                    "lease_id": lease.id,
                    "attempt_id": attempt.id,
                    "task_id": task.id,
                    "run_id": run.id,
                    "decision": "codex_isolated_edit_failed",
                    "error": reason,
                },
            )
            store.write_run_manifest(run.id)
            return DaemonExecuteResult(
                ok=False,
                decision="codex_isolated_edit_failed",
                adapter_id=self.id,
                project_root=store.project_root,
                task=store.get_task(task.id),
                attempt=store.get_task_attempt(attempt.id),
                lease=store.get_task_lease(lease.id),
                run=store.get_run(run.id),
                manifest=store.build_run_manifest(run.id),
                policy_sha256=effective_policy_sha256(resolve_task_effective_policy(task)),
                approval_id=approval.id,
                errors=[reason],
            )


def builtin_execution_adapters() -> dict[str, ExecutionAdapter]:
    adapters: list[ExecutionAdapter] = [
        DryRunExecutionAdapter(),
        ReadOnlySummaryExecutionAdapter(),
        CodexIsolatedEditExecutionAdapter(),
    ]
    return {adapter.id: adapter for adapter in adapters}


def list_execution_adapter_descriptors() -> list[ExecutionAdapterDescriptor]:
    return [adapter.descriptor for adapter in builtin_execution_adapters().values()]


def inspect_execution_eligibility(
    project_root: Path,
    lease: TaskLease,
    task: TaskRecord | None,
    attempt: TaskAttempt | None,
) -> dict[str, Any]:
    adapter_id = _adapter_id_from_task(task)
    base = _eligibility_base(project_root, lease, task, attempt, adapter_id)
    if task is None:
        return {**base, "eligible": False, "reason_code": "missing_task", "rejection_reasons": ["Task not found."]}
    if attempt is None:
        return {**base, "eligible": False, "reason_code": "missing_attempt", "rejection_reasons": ["Task attempt not found."]}
    if not adapter_id:
        return {
            **base,
            "eligible": False,
            "reason_code": "missing_adapter",
            "rejection_reasons": ["Task metadata is missing execution_adapter."],
        }
    adapters = builtin_execution_adapters()
    adapter = adapters.get(adapter_id)
    if adapter is None:
        return {
            **base,
            "eligible": False,
            "registered": False,
            "reason_code": "unknown_adapter",
            "rejection_reasons": [f"Unknown execution adapter: {adapter_id}."],
        }
    return adapter.inspect_eligibility(project_root, lease, task, attempt)


def execute_lease(
    project_root: Path,
    lease_id: str,
    owner: str = DEFAULT_TASK_LEASE_OWNER,
) -> DaemonExecuteResult:
    store = SQLiteStore(project_root)
    lease, task, attempt = _load_lease_context(store, lease_id)
    eligibility = inspect_execution_eligibility(project_root, lease, task, attempt)
    adapter_id = eligibility.get("adapter_id")
    if not eligibility.get("eligible"):
        reason_code = str(eligibility.get("reason_code") or "adapter_ineligible")
        rejection_reasons = [str(item) for item in eligibility.get("rejection_reasons", [])]
        decision = EXECUTION_DUPLICATE_REJECTED if reason_code == "duplicate_run" else EXECUTION_ADAPTER_REJECTED
        _record_adapter_rejection(
            store,
            lease=lease,
            task=task,
            attempt=attempt,
            adapter_id=adapter_id,
            reason_code=reason_code,
            rejection_reasons=rejection_reasons,
        )
        return DaemonExecuteResult(
            ok=False,
            decision=decision,
            adapter_id=adapter_id,
            project_root=store.project_root,
            task=task,
            attempt=attempt,
            lease=lease,
            policy_sha256=eligibility.get("policy_sha256"),
            rejection_reasons=rejection_reasons,
        )
    adapter = builtin_execution_adapters()[str(adapter_id)]
    try:
        return adapter.execute(project_root, lease_id, owner=owner)
    except (KeyError, ValueError, LocalEndpointUnavailable) as exc:
        sanitized = str(sanitize_for_logging(str(exc)))
        refreshed_lease = store.get_task_lease(lease.id)
        refreshed_attempt = _safe_get_attempt(store, refreshed_lease.attempt_id)
        refreshed_task = _safe_get_task(store, refreshed_lease.task_id)
        run = None
        manifest = None
        if refreshed_attempt is not None and refreshed_attempt.run_id is not None:
            try:
                run = store.get_run(refreshed_attempt.run_id)
                manifest = store.build_run_manifest(run.id)
            except KeyError:
                run = None
                manifest = None
        _record_adapter_rejection(
            store,
            lease=refreshed_lease,
            task=refreshed_task,
            attempt=refreshed_attempt,
            adapter_id=adapter_id,
            reason_code="adapter_execution_failed",
            rejection_reasons=[sanitized],
        )
        return DaemonExecuteResult(
            ok=False,
            decision=EXECUTION_ADAPTER_REJECTED,
            adapter_id=adapter_id,
            project_root=store.project_root,
            task=refreshed_task,
            attempt=refreshed_attempt,
            lease=refreshed_lease,
            run=run,
            manifest=manifest,
            policy_sha256=eligibility.get("policy_sha256"),
            rejection_reasons=[sanitized],
            errors=[sanitized],
        )


def _load_lease_context(store: SQLiteStore, lease_id: str) -> tuple[TaskLease, TaskRecord | None, TaskAttempt | None]:
    lease = store.get_task_lease(lease_id)
    task = _safe_get_task(store, lease.task_id)
    attempt = _safe_get_attempt(store, lease.attempt_id)
    return lease, task, attempt


def _safe_get_task(store: SQLiteStore, task_id: str | None) -> TaskRecord | None:
    if task_id is None:
        return None
    try:
        return store.get_task(task_id)
    except KeyError:
        return None


def _safe_get_attempt(store: SQLiteStore, attempt_id: str | None) -> TaskAttempt | None:
    if attempt_id is None:
        return None
    try:
        return store.get_task_attempt(attempt_id)
    except KeyError:
        return None


def _adapter_id_from_task(task: TaskRecord | None) -> str | None:
    if task is None:
        return None
    value = task.metadata.get("execution_adapter")
    return str(value) if isinstance(value, str) and value.strip() else None


def _eligibility_base(
    project_root: Path,
    lease: TaskLease,
    task: TaskRecord | None,
    attempt: TaskAttempt | None,
    adapter_id: str | None,
) -> dict[str, Any]:
    policy_sha = None
    if task is not None:
        policy_sha = effective_policy_sha256(resolve_task_effective_policy(task))
    adapters = builtin_execution_adapters()
    descriptor = adapters[adapter_id].descriptor if adapter_id in adapters else None
    return {
        "adapter_id": adapter_id,
        "registered": adapter_id in adapters if adapter_id else False,
        "lease_id": lease.id,
        "task_id": task.id if task is not None else lease.task_id,
        "attempt_id": attempt.id if attempt is not None else lease.attempt_id,
        "active_lease": lease.status == TaskLeaseStatus.ACTIVE,
        "duplicate_run": attempt.run_id is not None if attempt is not None else False,
        "policy_sha256": policy_sha,
        "descriptor_required_approvals": descriptor.required_approvals if descriptor is not None else [],
        "supported_task_types": descriptor.supported_task_types if descriptor is not None else [],
    }


def _base_adapter_eligibility(
    descriptor: ExecutionAdapterDescriptor,
    lease: TaskLease,
    task: TaskRecord | None,
    attempt: TaskAttempt | None,
) -> dict[str, Any]:
    base = _eligibility_base(Path("."), lease, task, attempt, descriptor.id)
    reasons: list[str] = []
    reason_code = ""
    if task is None:
        reason_code = "missing_task"
        reasons.append("Task not found.")
    elif attempt is None:
        reason_code = "missing_attempt"
        reasons.append("Task attempt not found.")
    elif lease.status != TaskLeaseStatus.ACTIVE:
        reason_code = "inactive_lease"
        reasons.append(f"Lease is not active: {lease.status.value}.")
    elif attempt.run_id is not None:
        reason_code = "duplicate_run"
        reasons.append("Task attempt is already linked to a run.")
    elif task.status != TaskStatus.LEASED:
        reason_code = "task_not_leased"
        reasons.append(f"Task status is not leased: {task.status.value}.")
    elif task.required_approvals:
        reason_code = "unresolved_task_approvals"
        reasons.append("Task has unresolved required approvals.")
    else:
        for key, expected in descriptor.required_task_metadata.items():
            if task.metadata.get(key) != expected:
                reason_code = "metadata_mismatch"
                reasons.append(f"Execution requires {key}={expected}.")
        rejected = sorted(key for key in descriptor.rejected_task_metadata if bool(task.metadata.get(key)))
        if rejected:
            reason_code = "unsafe_metadata"
            reasons.append(f"Execution rejected by task metadata: {', '.join(rejected)}.")
        task_type = task.metadata.get("task_type")
        if task_type not in descriptor.supported_task_types:
            reason_code = "unsupported_task_type"
            reasons.append(f"Unsupported task_type for {descriptor.id}: {task_type}.")
    eligible = not reasons
    return {
        **base,
        "adapter_id": descriptor.id,
        "registered": True,
        "eligible": eligible,
        "reason_code": "eligible" if eligible else reason_code or "adapter_ineligible",
        "reason": f"{descriptor.id} execution is available." if eligible else " ".join(reasons),
        "rejection_reasons": reasons,
    }


def _validate_task_against_descriptor(descriptor: ExecutionAdapterDescriptor, task: TaskRecord) -> None:
    reasons: list[str] = []
    for key, expected in descriptor.required_task_metadata.items():
        if task.metadata.get(key) != expected:
            reasons.append(f"Execution requires {key}={expected}.")
    rejected = sorted(key for key in descriptor.rejected_task_metadata if bool(task.metadata.get(key)))
    if rejected:
        reasons.append(f"Execution rejected by task metadata: {', '.join(rejected)}.")
    task_type = task.metadata.get("task_type")
    if task_type not in descriptor.supported_task_types:
        reasons.append(f"Unsupported task_type for {descriptor.id}: {task_type}.")
    if reasons:
        raise ValueError(" ".join(reasons))


def _codex_backend_rejection_reasons(backend_config: Any) -> list[str]:
    if backend_config is None:
        return ["Codex isolated edit requires configured codex_cli backend."]
    reasons: list[str] = []
    if backend_config.name != "codex_cli":
        reasons.append("Codex isolated edit requires backend name codex_cli.")
    if backend_config.kind != BackendKind.EXTERNAL_AGENT:
        reasons.append("Codex isolated edit requires external_agent backend.")
    if backend_config.metadata.billing_mode == BillingMode.PAID_API:
        reasons.append("Codex isolated edit must not use paid API billing.")
    if backend_config.metadata.data_boundary != DataBoundary.HOSTED_PROVIDER:
        reasons.append("Codex isolated edit requires hosted_provider data boundary.")
    if backend_config.metadata.execution_location not in {ExecutionLocation.MIXED, ExecutionLocation.HOSTED}:
        reasons.append("Codex isolated edit requires hosted or mixed execution location.")
    if backend_config.metadata.allow_network:
        reasons.append("Codex isolated edit requires backend allow_network=false.")
    return reasons


def _codex_decision_from_status(status: str) -> tuple[str, bool, str]:
    if status == "completed_applied":
        return "codex_isolated_edit_completed_applied", True, "completed_applied"
    if status == "completed_denied":
        return "codex_isolated_edit_completed_denied", True, "completed_denied"
    if status == "completed":
        return "codex_isolated_edit_completed_no_changes", True, "completed_no_changes"
    if status == "policy_violation":
        return "codex_isolated_edit_blocked_policy", False, "failed"
    if status == "apply_back_failed":
        return "codex_isolated_edit_failed", False, "failed"
    if status == "failed":
        return "codex_isolated_edit_failed", False, "failed"
    return "codex_isolated_edit_failed", False, "failed"


def _rejected_result(
    store: SQLiteStore,
    lease: TaskLease,
    task: TaskRecord | None,
    attempt: TaskAttempt | None,
    adapter_id: str,
    decision: str,
    rejection_reasons: list[str],
) -> DaemonExecuteResult:
    policy_sha = effective_policy_sha256(resolve_task_effective_policy(task)) if task is not None else None
    return DaemonExecuteResult(
        ok=False,
        decision=decision,
        adapter_id=adapter_id,
        project_root=store.project_root,
        task=task,
        attempt=attempt,
        lease=lease,
        policy_sha256=policy_sha,
        rejection_reasons=rejection_reasons,
    )


def _generic_result_from_adapter_result(
    *,
    adapter_id: str,
    decision: str,
    project_root: Path,
    result: BaseModel,
) -> DaemonExecuteResult:
    manifest: RunManifest | None = getattr(result, "manifest", None)
    return DaemonExecuteResult(
        ok=bool(getattr(result, "ok", True)),
        decision=decision,
        adapter_id=adapter_id,
        project_root=Path(project_root).resolve(),
        task=getattr(result, "task", None),
        attempt=getattr(result, "attempt", None),
        lease=getattr(result, "lease", None),
        run=getattr(result, "run", None),
        manifest=manifest,
        policy_sha256=getattr(result, "policy_sha256", None),
        errors=list(getattr(result, "errors", [])),
        adapter_result=result.model_dump(mode="json"),
    )


def _record_adapter_rejection(
    store: SQLiteStore,
    *,
    lease: TaskLease,
    task: TaskRecord | None,
    attempt: TaskAttempt | None,
    adapter_id: str | None,
    reason_code: str,
    rejection_reasons: list[str],
) -> None:
    daemon = store.ensure_daemon(owner=lease.owner)
    store.record_daemon_event(
        daemon.id,
        event_type=EXECUTION_ADAPTER_REJECTED,
        message="Execution adapter dispatch or execution was rejected.",
        metadata=sanitize_for_logging(
            {
                "lease_id": lease.id,
                "task_id": task.id if task is not None else lease.task_id,
                "attempt_id": attempt.id if attempt is not None else lease.attempt_id,
                "adapter_id": adapter_id,
                "reason_code": reason_code,
                "rejection_reasons": rejection_reasons,
            }
        ),
    )
