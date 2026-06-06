from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from pydantic import BaseModel

from harness.approvals import ApprovalStore
from harness.backends.codex_cli import CodexCliBackend
from harness.codex_edit_runner import CodexCodeEditRunner
from harness.codex_runner import CodexRepoPlanningRunner
from harness.config import load_config
from harness.daemon_adapters import execute_read_only_summary_lease
from harness.delegate_budgets import task_delegate_budget_rejection_reasons, validate_adapter_delegate_budget
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
    DelegateBudgetPolicy,
    ExecutionLocation,
    ExecutionAdapterDescriptor,
    BreakerStatus,
    KillSwitchRecord,
    KillSwitchTargetKind,
    RunManifest,
    SandboxActiveRepoWritePolicy,
    SandboxNetworkPolicy,
    SecurityDecision,
    SecurityDecisionStatus,
    TaskAttempt,
    TaskLease,
    TaskLeaseStatus,
    TaskRecord,
    TaskStatus,
    ToolReplayPolicy,
)
from harness.policy import effective_policy_sha256, resolve_task_effective_policy
from harness.sandbox_profiles import (
    ISOLATED_WORKSPACE_CODEX_SANDBOX_PROFILE,
    NONE_SANDBOX_PROFILE,
    READ_ONLY_CODEX_SANDBOX_PROFILE,
    get_sandbox_profile,
)
from harness.security import sanitize_for_logging
from harness.security_explanations import explanations_from_reasons, explanations_from_security_decision
from harness.task_operator_bridge import (
    SESSION_OPERATOR_EXECUTION_ADAPTER,
    SESSION_OPERATOR_TASK_TYPES,
    execute_operator_task_lease,
)


EXECUTION_ADAPTER_REJECTED = "execution_adapter_rejected"
EXECUTION_DUPLICATE_REJECTED = "execution_duplicate_rejected"
CODEX_ISOLATED_EDIT_ADAPTER = "codex_isolated_edit"
CODEX_CODE_EDIT_TASK_TYPE = "codex_code_edit"
REPO_PLANNING_EXECUTION_ADAPTER = "repo_planning"
REPO_PLANNING_TASK_TYPE = "repo_planning"
REVIEW_GATE_EXECUTION_ADAPTER = "review_gate"
IMPLEMENTATION_REVIEW_TASK_TYPE = "implementation_review"
SECURITY_REVIEW_TASK_TYPE = "security_review"
FACTUALITY_REVIEW_TASK_TYPE = "factuality_review"
REVIEW_GATE_TASK_TYPES = {
    IMPLEMENTATION_REVIEW_TASK_TYPE,
    SECURITY_REVIEW_TASK_TYPE,
    FACTUALITY_REVIEW_TASK_TYPE,
}
SESSION_CHILD_TASK_EXECUTION_ADAPTER = "session_child_task"
SESSION_DELEGATE_TASK_TYPE = "session_delegate"
REVIEW_ROLE_BY_TASK_TYPE = {
    IMPLEMENTATION_REVIEW_TASK_TYPE: "implementation_reviewer",
    SECURITY_REVIEW_TASK_TYPE: "security_reviewer",
    FACTUALITY_REVIEW_TASK_TYPE: "factuality_reviewer",
}


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


def _delegate_budget(
    *,
    timeout_seconds: int,
    max_runtime_invocations: int,
    max_model_calls: int,
    max_tool_calls: int,
    cost_policy: str,
    filesystem_scope: str,
    tool_allowlist: Sequence[str] = (),
    max_input_tokens: int | None = None,
    max_output_tokens: int | None = None,
    max_cost_usd: str | None = None,
    max_cpu_seconds: int | None = None,
    max_memory_mb: int | None = None,
    active_repo_write: SandboxActiveRepoWritePolicy = SandboxActiveRepoWritePolicy.FORBIDDEN,
    notes: Sequence[str] = (),
) -> DelegateBudgetPolicy:
    return DelegateBudgetPolicy(
        timeout_seconds=timeout_seconds,
        max_runtime_invocations=max_runtime_invocations,
        max_model_calls=max_model_calls,
        max_tool_calls=max_tool_calls,
        max_parallel_branches=1,
        max_input_tokens=max_input_tokens,
        max_output_tokens=max_output_tokens,
        max_cost_usd=max_cost_usd,
        max_cpu_seconds=max_cpu_seconds,
        max_memory_mb=max_memory_mb,
        cost_policy=cost_policy,
        network_policy=SandboxNetworkPolicy.FORBIDDEN,
        active_repo_write=active_repo_write,
        filesystem_scope=filesystem_scope,
        tool_allowlist=list(tool_allowlist),
        notes=list(notes),
    )


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
        sandbox_profile_id=NONE_SANDBOX_PROFILE,
        side_effect_summary="Writes harness run/task/lease/artifact evidence only.",
        replay_policy=ToolReplayPolicy.IDEMPOTENT_WITH_KEY,
        safety_notes=[
            "Descriptors are documentation and validation metadata, not permission grants.",
            "Execution still requires an active lease and exact task metadata.",
        ],
        autonomy_default="auto_allowed",
        max_autonomous_retries=0,
        delegate_budget=_delegate_budget(
            timeout_seconds=30,
            max_runtime_invocations=0,
            max_model_calls=0,
            max_tool_calls=0,
            cost_policy="local_no_api_cost",
            filesystem_scope="harness_artifacts",
            max_cpu_seconds=0,
            max_memory_mb=0,
            notes=["Creates local Harness evidence only; no delegate runtime is invoked."],
        ),
        required_autonomy_scopes=["safe-local", "daemon-safe", "supervised-codex"],
        output_contracts=["harness.daemon_execute/v1", "harness.manifest/v1.1"],
        terminal_evidence_required=["task", "lease", "run", "manifest", "policy_sha256"],
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
        description="Execute the bounded read-only repository summary adapter through the supervised Codex CLI subscription backend in read-only sandbox mode.",
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
        required_approvals=["hosted_provider_codex"],
        backend_requirements=[
            "codex_cli backend",
            "billing_mode=subscription",
            "execution_location=mixed or hosted",
            "data_boundary=hosted_provider",
            "allow_network=false",
        ],
        sandbox_requirements=["Codex CLI --cd support", "Codex CLI read-only sandbox support"],
        sandbox_profile_id=READ_ONLY_CODEX_SANDBOX_PROFILE,
        side_effect_summary="Writes harness run/task/lease/artifact evidence and runs Codex CLI in read-only sandbox mode.",
        replay_policy=ToolReplayPolicy.REQUIRES_FRESH_APPROVAL,
        safety_notes=[
            "Descriptors are documentation and validation metadata, not permission grants.",
            "Hosted-boundary approval is validated before run creation.",
            "Execution still performs backend, policy, lease, and exact metadata checks.",
        ],
        autonomy_default="approval_required",
        max_autonomous_retries=0,
        delegate_budget=_delegate_budget(
            timeout_seconds=900,
            max_runtime_invocations=1,
            max_model_calls=0,
            max_tool_calls=0,
            cost_policy="subscription_boundary",
            filesystem_scope="project_read_only",
            tool_allowlist=["codex_cli_read_only_sandbox"],
            max_output_tokens=8192,
            max_cpu_seconds=900,
            max_memory_mb=1024,
            notes=["A single supervised Codex CLI read-only sandbox invocation is allowed after hosted-boundary approval."],
        ),
        required_autonomy_scopes=["supervised-codex"],
        output_contracts=["harness.daemon_execute/v1", "harness.manifest/v1.1"],
        terminal_evidence_required=["task", "lease", "run", "manifest", "approval_id", "policy_sha256"],
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


class SessionOperatorExecutionAdapter:
    id = SESSION_OPERATOR_EXECUTION_ADAPTER
    descriptor = ExecutionAdapterDescriptor(
        id=SESSION_OPERATOR_EXECUTION_ADAPTER,
        description="Run one leased task through the Harness natural-language operator loop using typed session tools and exact approvals.",
        supported_task_types=sorted(SESSION_OPERATOR_TASK_TYPES),
        required_task_metadata={"execution_adapter": SESSION_OPERATOR_EXECUTION_ADAPTER},
        rejected_task_metadata=[
            "daemon_policy_forbidden",
            "requires_active_repo_write",
            "requires_external_network",
            "requires_docker",
            "requires_paid_provider",
            "requires_hosted_boundary",
        ],
        required_approvals=[],
        backend_requirements=["provider-native tool-capable chat backend"],
        sandbox_requirements=["Harness session-tool gateway", "exact approval resume for shell/test tools"],
        sandbox_profile_id=NONE_SANDBOX_PROFILE,
        side_effect_summary="Writes harness task/run/session evidence and executes only session tools allowed by Harness policy.",
        replay_policy=ToolReplayPolicy.IDEMPOTENT_WITH_KEY,
        safety_notes=[
            "Descriptors are documentation and validation metadata, not permission grants.",
            "Shell/test tools still require exact one-shot approvals through the session permission gate.",
            "Active repo mutation remains behind the separate apply-back boundary.",
        ],
        autonomy_default="auto_allowed",
        max_autonomous_retries=0,
        delegate_budget=_delegate_budget(
            timeout_seconds=900,
            max_runtime_invocations=1,
            max_model_calls=8,
            max_tool_calls=20,
            cost_policy="provider_policy_validated",
            filesystem_scope="session_policy",
            tool_allowlist=["artifact-read", "glob", "grep", "read"],
            max_input_tokens=128000,
            max_output_tokens=8192,
            max_cpu_seconds=900,
            max_memory_mb=512,
            notes=["Explicit task metadata may narrow or expand allowed session tools only through policy validation."],
        ),
        required_autonomy_scopes=["safe-local", "daemon-safe"],
        output_contracts=["harness.daemon_execute/v1", "harness.manifest/v1.1", "harness.operator_task_tool_results/v1"],
        terminal_evidence_required=["task", "attempt", "lease", "run", "turn", "manifest"],
    )

    def inspect_eligibility(
        self,
        project_root: Path,
        lease: TaskLease,
        task: TaskRecord | None,
        attempt: TaskAttempt | None,
    ) -> dict[str, Any]:
        eligibility = _base_adapter_eligibility(self.descriptor, lease, task, attempt)
        if task is not None and task.metadata.get("task_type") not in SESSION_OPERATOR_TASK_TYPES:
            return {
                **eligibility,
                "eligible": False,
                "reason_code": "unsupported_task_type",
                "reason": f"Unsupported task_type for {self.id}: {task.metadata.get('task_type')}.",
                "rejection_reasons": [f"Unsupported task_type for {self.id}: {task.metadata.get('task_type')}."],
            }
        return eligibility

    def execute(
        self,
        project_root: Path,
        lease_id: str,
        owner: str = DEFAULT_TASK_LEASE_OWNER,
    ) -> DaemonExecuteResult:
        return execute_operator_task_lease(project_root, lease_id, owner=owner)


class SessionChildTaskExecutionAdapter:
    id = SESSION_CHILD_TASK_EXECUTION_ADAPTER
    descriptor = ExecutionAdapterDescriptor(
        id=SESSION_CHILD_TASK_EXECUTION_ADAPTER,
        description="Record-only adapter contract for session-tool delegated child tasks. It creates linked session/task evidence but is not daemon-dispatchable.",
        supported_task_types=[SESSION_DELEGATE_TASK_TYPE],
        required_task_metadata={
            "execution_adapter": SESSION_CHILD_TASK_EXECUTION_ADAPTER,
            "task_type": SESSION_DELEGATE_TASK_TYPE,
        },
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
        sandbox_requirements=["Harness session-tool gateway"],
        sandbox_profile_id=NONE_SANDBOX_PROFILE,
        side_effect_summary="Creates linked parent/child session and task records only; daemon execution is denied.",
        replay_policy=ToolReplayPolicy.NOT_REPLAYABLE,
        safety_notes=[
            "Descriptors are documentation and validation metadata, not permission grants.",
            "This adapter validates delegated child-task records created by the session `task` tool.",
            "It is intentionally not daemon-dispatchable; execution remains with explicit session/tool paths.",
        ],
        autonomy_default="forbidden",
        max_autonomous_retries=0,
        delegate_budget=_delegate_budget(
            timeout_seconds=30,
            max_runtime_invocations=0,
            max_model_calls=0,
            max_tool_calls=0,
            cost_policy="record_only",
            filesystem_scope="harness_artifacts",
            max_cpu_seconds=0,
            max_memory_mb=0,
            notes=["Delegated child tasks are records only; daemon dispatch is denied."],
        ),
        required_autonomy_scopes=[],
        output_contracts=[
            "harness.session_tool_task/v1",
            "harness.session_tool_task_status/v1",
            "harness.agent_handoff_envelope/v1",
        ],
        terminal_evidence_required=["parent_session", "child_session", "task", "session_tool_artifact"],
    )

    def inspect_eligibility(
        self,
        project_root: Path,
        lease: TaskLease,
        task: TaskRecord | None,
        attempt: TaskAttempt | None,
    ) -> dict[str, Any]:
        return {
            **_base_adapter_eligibility(self.descriptor, lease, task, attempt),
            "eligible": False,
            "reason_code": "record_only_adapter",
            "reason": "session_child_task is a record-only session-tool contract and cannot be daemon-dispatched.",
            "rejection_reasons": [
                "session_child_task is a record-only session-tool contract and cannot be daemon-dispatched."
            ],
        }

    def execute(
        self,
        project_root: Path,
        lease_id: str,
        owner: str = DEFAULT_TASK_LEASE_OWNER,
    ) -> DaemonExecuteResult:
        store = SQLiteStore(project_root)
        lease, task, attempt = _load_lease_context(store, lease_id)
        reason = "session_child_task is a record-only session-tool contract and cannot be daemon-dispatched."
        lease, task, attempt = _record_adapter_rejection(
            store,
            lease=lease,
            task=task,
            attempt=attempt,
            adapter_id=self.id,
            reason_code="record_only_adapter",
            rejection_reasons=[reason],
        )
        return DaemonExecuteResult(
            ok=False,
            decision=EXECUTION_ADAPTER_REJECTED,
            adapter_id=self.id,
            project_root=store.project_root,
            task=task,
            attempt=attempt,
            lease=lease,
            rejection_reasons=[reason],
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
        sandbox_profile_id=ISOLATED_WORKSPACE_CODEX_SANDBOX_PROFILE,
        side_effect_summary="Writes harness evidence and isolated workspace files; active repo mutation only after separate apply-back approval.",
        replay_policy=ToolReplayPolicy.REQUIRES_FRESH_APPROVAL,
        safety_notes=[
            "Descriptors are documentation and validation metadata, not permission grants.",
            "Hosted-boundary approval is validated before run creation.",
            "A leased task is not apply-back approval; apply-back remains denied by default.",
        ],
        autonomy_default="approval_required",
        max_autonomous_retries=0,
        delegate_budget=_delegate_budget(
            timeout_seconds=1800,
            max_runtime_invocations=1,
            max_model_calls=0,
            max_tool_calls=0,
            cost_policy="subscription_boundary",
            filesystem_scope="isolated_workspace",
            tool_allowlist=["codex_cli_workspace_write_sandbox"],
            max_output_tokens=16384,
            max_cpu_seconds=1800,
            max_memory_mb=2048,
            active_repo_write=SandboxActiveRepoWritePolicy.APPROVAL_REQUIRED,
            notes=["Active repository apply-back remains outside this budget and requires a separate approval path."],
        ),
        required_autonomy_scopes=["supervised-codex"],
        output_contracts=["harness.daemon_execute/v1", "harness.manifest/v1.1", "isolated_diff_artifacts"],
        terminal_evidence_required=[
            "task",
            "lease",
            "run",
            "manifest",
            "approval_id",
            "diff_artifact",
            "policy_sha256",
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
        lease, attempt, task = store.validate_execution_lease_for_run(lease_id, owner=owner)
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

        approval = ApprovalStore(project_root).find_valid(
            "codex_cli",
            "hosted_provider",
            CODEX_CODE_EDIT_TASK_TYPE,
            adapter_id=self.id,
            workbench_id=task.workbench_id,
            objective_id=task.objective_id,
        )
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
            lease, task, attempt = _record_adapter_rejection(
                store,
                lease=lease,
                task=task,
                attempt=attempt,
                adapter_id=self.id,
                reason_code="backend_unavailable",
                rejection_reasons=[reason],
                decision="codex_isolated_edit_failed",
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
        allow_dirty_isolation = approval.autonomy_scope == "supervised-codex"
        try:
            adapter_payload = runner.run_existing(
                run_id=run.id,
                goal=goal,
                task_type=CODEX_CODE_EDIT_TASK_TYPE,
                approval=approval,
                keep_isolation=keep_isolation,
                allow_dirty_isolation=allow_dirty_isolation,
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


class RepoPlanningExecutionAdapter:
    id = REPO_PLANNING_EXECUTION_ADAPTER
    descriptor = ExecutionAdapterDescriptor(
        id=REPO_PLANNING_EXECUTION_ADAPTER,
        description="Run Codex as a supervised external agent in read-only sandbox mode to produce an implementation plan and evidence artifacts.",
        supported_task_types=[REPO_PLANNING_TASK_TYPE],
        required_task_metadata={
            "execution_adapter": REPO_PLANNING_EXECUTION_ADAPTER,
            "task_type": REPO_PLANNING_TASK_TYPE,
        },
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
        sandbox_requirements=["Codex CLI --cd support", "Codex CLI read-only sandbox support"],
        sandbox_profile_id=READ_ONLY_CODEX_SANDBOX_PROFILE,
        side_effect_summary="Writes harness run/task/lease/artifact evidence and runs Codex CLI in read-only sandbox mode.",
        replay_policy=ToolReplayPolicy.REQUIRES_FRESH_APPROVAL,
        safety_notes=[
            "Descriptors are documentation and validation metadata, not permission grants.",
            "Hosted-boundary approval is validated before run creation.",
            "Execution still performs backend, policy, lease, and exact metadata checks.",
        ],
        autonomy_default="approval_required",
        max_autonomous_retries=0,
        delegate_budget=_delegate_budget(
            timeout_seconds=900,
            max_runtime_invocations=1,
            max_model_calls=0,
            max_tool_calls=0,
            cost_policy="subscription_boundary",
            filesystem_scope="project_read_only",
            tool_allowlist=["codex_cli_read_only_sandbox"],
            max_output_tokens=8192,
            max_cpu_seconds=900,
            max_memory_mb=1024,
            notes=["A single supervised Codex CLI read-only planning invocation is allowed after hosted-boundary approval."],
        ),
        required_autonomy_scopes=["supervised-codex"],
        output_contracts=["harness.daemon_execute/v1", "harness.manifest/v1.1", "planning_artifacts"],
        terminal_evidence_required=["task", "lease", "run", "manifest", "approval_id", "policy_sha256"],
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
        lease, attempt, task = store.validate_execution_lease_for_run(lease_id, owner=owner)
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
            return _rejected_result(store, lease, task, attempt, self.id, "repo_planning_blocked_policy", [reason])

        approval = ApprovalStore(project_root).find_valid(
            "codex_cli",
            "hosted_provider",
            REPO_PLANNING_TASK_TYPE,
            adapter_id=self.id,
            workbench_id=task.workbench_id,
            objective_id=task.objective_id,
        )
        if approval is None:
            reason = "Missing valid hosted-provider Codex approval for repo_planning."
            _record_adapter_rejection(
                store,
                lease=lease,
                task=task,
                attempt=attempt,
                adapter_id=self.id,
                reason_code="missing_hosted_approval",
                rejection_reasons=[reason],
            )
            return _rejected_result(store, lease, task, attempt, self.id, "repo_planning_blocked_policy", [reason])

        cfg = load_config(project_root)
        backend_config = cfg.backends.get("codex_cli")
        backend_reasons = _codex_backend_rejection_reasons(backend_config, adapter_label="Repo planning")
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
            return _rejected_result(store, lease, task, attempt, self.id, "repo_planning_blocked_policy", backend_reasons)

        backend = CodexCliBackend(backend_config)
        status = backend.preflight()
        if not status.available:
            reason = status.reason or "Codex CLI is unavailable."
            lease, task, attempt = _record_adapter_rejection(
                store,
                lease=lease,
                task=task,
                attempt=attempt,
                adapter_id=self.id,
                reason_code="backend_unavailable",
                rejection_reasons=[reason],
                decision="repo_planning_failed",
            )
            return DaemonExecuteResult(
                ok=False,
                decision="repo_planning_failed",
                adapter_id=self.id,
                project_root=store.project_root,
                task=task,
                attempt=attempt,
                lease=lease,
                policy_sha256=effective_policy_sha256(resolve_task_effective_policy(task)),
                errors=[reason],
            )
        if not status.capabilities.supports_read_only_sandbox:
            reason = "Codex read-only sandbox is unavailable; refusing to run Codex."
            _record_adapter_rejection(
                store,
                lease=lease,
                task=task,
                attempt=attempt,
                adapter_id=self.id,
                reason_code="sandbox_unavailable",
                rejection_reasons=[reason],
            )
            return _rejected_result(store, lease, task, attempt, self.id, "repo_planning_blocked_policy", [reason])

        backend_with_capabilities = backend_config.model_copy(update={"capabilities": status.capabilities})
        run = store.start_attempt_run(
            lease.id,
            task_type=REPO_PLANNING_TASK_TYPE,
            backend=backend_with_capabilities,
            approval_id=approval.id,
            owner=owner,
        )
        runner = CodexRepoPlanningRunner(
            project_root,
            store,
            CodexCliBackend(backend_with_capabilities),
            ApprovalStore(project_root),
        )
        goal = task.title if not task.description else f"{task.title}\n\n{task.description}"
        try:
            adapter_payload = runner.run_existing(
                run_id=run.id,
                goal=goal,
                task_type=REPO_PLANNING_TASK_TYPE,
                approval=approval,
            )
            decision, success, run_status, failure_code = _repo_planning_decision_from_status(
                str(adapter_payload.get("status", ""))
            )
            store.finish_attempt_run(
                lease.id,
                run_id=run.id,
                owner=owner,
                success=success,
                decision=decision,
                run_status=run_status,
                failure_code=failure_code,
                failure_message=None if success else decision,
            )
            daemon = store.ensure_daemon(owner=lease.owner)
            store.record_daemon_event(
                daemon.id,
                event_type="execute_repo_planning",
                message="Repo planning adapter linked lease to run evidence.",
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
                decision="repo_planning_failed",
                run_status="failed",
                failure_code="repo_planning_failed",
                failure_message=reason,
            )
            daemon = store.ensure_daemon(owner=lease.owner)
            store.record_daemon_event(
                daemon.id,
                event_type="execute_repo_planning",
                message="Repo planning adapter failed after run creation.",
                metadata={
                    "lease_id": lease.id,
                    "attempt_id": attempt.id,
                    "task_id": task.id,
                    "run_id": run.id,
                    "decision": "repo_planning_failed",
                    "error": reason,
                },
            )
            store.write_run_manifest(run.id)
            return DaemonExecuteResult(
                ok=False,
                decision="repo_planning_failed",
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


class ReviewGateExecutionAdapter:
    id = REVIEW_GATE_EXECUTION_ADAPTER
    descriptor = ExecutionAdapterDescriptor(
        id=REVIEW_GATE_EXECUTION_ADAPTER,
        description="Create typed local reviewer-gate evidence from task, dependency, run, and artifact provenance without invoking tools, backends, shell, network, hosted providers, or paid providers.",
        supported_task_types=sorted(REVIEW_GATE_TASK_TYPES),
        required_task_metadata={"execution_adapter": REVIEW_GATE_EXECUTION_ADAPTER},
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
        sandbox_profile_id=NONE_SANDBOX_PROFILE,
        side_effect_summary="Writes harness run/task/lease/artifact evidence only.",
        replay_policy=ToolReplayPolicy.IDEMPOTENT_WITH_KEY,
        safety_notes=[
            "Descriptors are documentation and validation metadata, not permission grants.",
            "Review gates inspect provenance and dependency state; they do not execute tools or grant approvals.",
            "Security review evidence can block later apply-back paths but cannot approve apply-back.",
        ],
        autonomy_default="auto_allowed",
        max_autonomous_retries=0,
        delegate_budget=_delegate_budget(
            timeout_seconds=60,
            max_runtime_invocations=0,
            max_model_calls=0,
            max_tool_calls=0,
            cost_policy="local_no_api_cost",
            filesystem_scope="harness_artifacts",
            max_cpu_seconds=0,
            max_memory_mb=0,
            notes=["Review gates inspect local provenance metadata and write Harness evidence only."],
        ),
        required_autonomy_scopes=["safe-local", "daemon-safe", "supervised-codex"],
        output_contracts=["harness.daemon_execute/v1", "harness.manifest/v1.1", "harness.review_gate_report/v1"],
        terminal_evidence_required=["task", "attempt", "lease", "run", "manifest", "review_report", "policy_sha256"],
    )

    def inspect_eligibility(
        self,
        project_root: Path,
        lease: TaskLease,
        task: TaskRecord | None,
        attempt: TaskAttempt | None,
    ) -> dict[str, Any]:
        eligibility = _base_adapter_eligibility(self.descriptor, lease, task, attempt)
        if task is None or not eligibility.get("eligible"):
            return eligibility
        extra_reasons = _review_gate_rejection_reasons(task)
        if extra_reasons:
            return {
                **eligibility,
                "eligible": False,
                "reason_code": "review_gate_contract_mismatch",
                "reason": " ".join(extra_reasons),
                "rejection_reasons": [*list(eligibility.get("rejection_reasons", [])), *extra_reasons],
            }
        return eligibility

    def execute(
        self,
        project_root: Path,
        lease_id: str,
        owner: str = DEFAULT_TASK_LEASE_OWNER,
    ) -> DaemonExecuteResult:
        store = SQLiteStore(project_root)
        lease, attempt, task = store.validate_execution_lease_for_run(lease_id, owner=owner)
        try:
            _validate_task_against_descriptor(self.descriptor, task)
            review_reasons = _review_gate_rejection_reasons(task)
            if review_reasons:
                raise ValueError(" ".join(review_reasons))
        except ValueError as exc:
            reason = str(sanitize_for_logging(str(exc)))
            _record_adapter_rejection(
                store,
                lease=lease,
                task=task,
                attempt=attempt,
                adapter_id=self.id,
                reason_code="review_gate_contract_mismatch",
                rejection_reasons=[reason],
            )
            return _rejected_result(store, lease, task, attempt, self.id, "review_gate_blocked_policy", [reason])

        task_type = str(task.metadata.get("task_type"))
        run = store.start_attempt_run(
            lease.id,
            task_type=task_type,
            backend=None,
            approval_id=None,
            owner=owner,
        )
        paths = store.initialize_run_artifacts(run.id)
        policy_hash = effective_policy_sha256(resolve_task_effective_policy(task))
        report = _build_review_gate_report(store, task, run.id, policy_hash)
        report_path = store.runs_dir / run.id / "review_report.json"
        report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        append_payload = {
            "event": "review_gate_completed",
            "task_id": task.id,
            "attempt_id": attempt.id,
            "lease_id": lease.id,
            "run_id": run.id,
            "review_role": report["review_role"],
            "verdict": report["verdict"],
            "policy_sha256": policy_hash,
        }
        paths["transcript"].write_text(json.dumps(sanitize_for_logging(append_payload), sort_keys=True) + "\n", encoding="utf-8")
        paths["final_report"].write_text(_render_review_gate_markdown(report), encoding="utf-8")
        store.append_event(
            run.id,
            "info",
            "review_gate_completed",
            "Review gate evidence completed without tool execution.",
            append_payload,
        )
        for kind, path in (
            ("events", paths["events"]),
            ("transcript", paths["transcript"]),
            ("final_report", paths["final_report"]),
            ("review_report", report_path),
            ("manifest", paths["manifest"]),
        ):
            store.register_artifact(
                run.id,
                kind=kind,
                path=path,
                producer="review_gate_adapter",
                redaction_state="redacted",
                metadata={
                    "review_gate": True,
                    "review_role": report["review_role"],
                    "verdict": report["verdict"],
                    "policy_sha256": policy_hash,
                },
            )
        success = report["verdict"] == "passed"
        decision = "review_gate_passed" if success else "review_gate_failed"
        store.finish_attempt_run(
            lease.id,
            run_id=run.id,
            owner=owner,
            success=success,
            decision=decision,
            run_status="completed" if success else "failed",
            failure_code=None if success else "review_gate_failed",
            failure_message=None if success else "Review gate checks failed.",
        )
        daemon = store.ensure_daemon(owner=lease.owner)
        store.record_daemon_event(
            daemon.id,
            event_type="execute_review_gate",
            message="Review gate adapter linked lease to typed review evidence.",
            metadata={
                "lease_id": lease.id,
                "attempt_id": attempt.id,
                "task_id": task.id,
                "run_id": run.id,
                "decision": decision,
                "review_role": report["review_role"],
                "verdict": report["verdict"],
                "policy_sha256": policy_hash,
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
            policy_sha256=policy_hash,
            errors=[] if success else ["Review gate checks failed."],
            adapter_result={"report": sanitize_for_logging(report)},
        )


def builtin_execution_adapters() -> dict[str, ExecutionAdapter]:
    adapters: list[ExecutionAdapter] = [
        DryRunExecutionAdapter(),
        ReadOnlySummaryExecutionAdapter(),
        SessionOperatorExecutionAdapter(),
        SessionChildTaskExecutionAdapter(),
        CodexIsolatedEditExecutionAdapter(),
        RepoPlanningExecutionAdapter(),
        ReviewGateExecutionAdapter(),
    ]
    return {adapter.id: adapter for adapter in adapters}


def list_execution_adapter_descriptors() -> list[ExecutionAdapterDescriptor]:
    return [adapter.descriptor for adapter in builtin_execution_adapters().values()]


def get_execution_adapter_descriptor(adapter_id: str | None) -> ExecutionAdapterDescriptor | None:
    if adapter_id is None:
        return None
    adapter = builtin_execution_adapters().get(adapter_id)
    return adapter.descriptor if adapter is not None else None


def validate_execution_task_payload(
    *,
    execution_adapter: str,
    task_type: str,
    metadata: Mapping[str, Any] | None = None,
    agent_id: str | None = None,
    depends_on: Sequence[str] | None = None,
) -> list[str]:
    """Validate a task payload against the registered adapter contract.

    This is intentionally side-effect free so chat/action-contract and workflow
    template paths can reject invalid graphs before they create durable records.
    """
    adapters = builtin_execution_adapters()
    descriptor = adapters.get(execution_adapter).descriptor if execution_adapter in adapters else None
    if descriptor is None:
        return [f"Unknown execution adapter: {execution_adapter}."]

    task_metadata = dict(metadata or {})
    reasons: list[str] = []
    reasons.extend(_adapter_descriptor_contract_rejection_reasons(descriptor))
    for key, expected in (("execution_adapter", execution_adapter), ("task_type", task_type)):
        if key in task_metadata and task_metadata.get(key) != expected:
            reasons.append(f"Task metadata {key}={task_metadata.get(key)} conflicts with task payload {key}={expected}.")
    combined_metadata = {**task_metadata, "execution_adapter": execution_adapter, "task_type": task_type}
    for key, expected in descriptor.required_task_metadata.items():
        if combined_metadata.get(key) != expected:
            reasons.append(f"Execution requires {key}={expected}.")
    rejected = sorted(key for key in descriptor.rejected_task_metadata if bool(combined_metadata.get(key)))
    if rejected:
        reasons.append(f"Execution rejected by task metadata: {', '.join(rejected)}.")
    reasons.extend(task_delegate_budget_rejection_reasons(descriptor, combined_metadata))
    if task_type not in descriptor.supported_task_types:
        reasons.append(f"Unsupported task_type for {execution_adapter}: {task_type}.")
    if execution_adapter == REVIEW_GATE_EXECUTION_ADAPTER:
        reasons.extend(
            _review_gate_payload_rejection_reasons(
                task_type=task_type,
                metadata=combined_metadata,
                agent_id=agent_id,
                depends_on=list(depends_on or []),
            )
        )
    return reasons


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


def evaluate_registered_adapter_security_decision(
    project_root: Path,
    lease: TaskLease,
    task: TaskRecord | None,
    attempt: TaskAttempt | None,
    owner: str = DEFAULT_TASK_LEASE_OWNER,
) -> SecurityDecision:
    eligibility = inspect_execution_eligibility(project_root, lease, task, attempt)
    adapter_id = eligibility.get("adapter_id")
    adapters = builtin_execution_adapters()
    descriptor = adapters[str(adapter_id)].descriptor if isinstance(adapter_id, str) and adapter_id in adapters else None
    policy = resolve_task_effective_policy(task) if task is not None else None
    policy_sha = effective_policy_sha256(policy) if policy is not None else None
    task_type = task.metadata.get("task_type") if task is not None else None
    task_type_value = str(task_type) if isinstance(task_type, str) else None
    required_approvals = list(dict.fromkeys([str(item) for item in eligibility.get("descriptor_required_approvals", [])]))
    if policy is not None:
        for approval in policy.required_approvals:
            if approval not in required_approvals:
                required_approvals.append(approval)
    task_required = [str(item) for item in task.required_approvals] if task is not None else []
    descriptor_missing = _missing_descriptor_approvals(project_root, descriptor, task, task_type_value)
    missing_approvals = sorted(set(task_required + descriptor_missing))
    if eligibility.get("reason_code") == "unresolved_task_approvals":
        missing_approvals = sorted(set(missing_approvals + required_approvals))
    satisfied_approvals = sorted(set(required_approvals) - set(missing_approvals))
    reason_code = str(eligibility.get("reason_code") or "adapter_ineligible")
    reasons = [str(sanitize_for_logging(str(item))) for item in eligibility.get("rejection_reasons", [])]
    if eligibility.get("eligible"):
        decision = SecurityDecisionStatus.ALLOW
        reason_code = "allow"
        reasons = [str(sanitize_for_logging(str(eligibility.get("reason") or "Registered adapter execution is allowed.")))]
    if reason_code == "unresolved_task_approvals" or missing_approvals:
        decision = SecurityDecisionStatus.APPROVAL_REQUIRED
        reason_code = "missing_required_approval" if reason_code == "allow" else reason_code
        if not reasons or eligibility.get("eligible"):
            reasons = ["Task has unresolved required approvals."]
        if descriptor_missing:
            reasons = [*reasons, f"Missing required adapter approvals: {', '.join(descriptor_missing)}."]
    elif not eligibility.get("eligible"):
        decision = SecurityDecisionStatus.DENY
        if not reasons:
            reasons = [str(sanitize_for_logging(str(eligibility.get("reason") or "Registered adapter execution is denied.")))]
    if lease.owner != owner:
        decision = SecurityDecisionStatus.DENY
        reason_code = "lease_owner_mismatch"
        reasons = [
            str(
                sanitize_for_logging(
                    f"Lease owner mismatch: lease is owned by {lease.owner}, not {owner}."
                )
            )
        ]
    control_denial = _runtime_control_denial(project_root, descriptor, task_type_value, decision)
    if control_denial is not None:
        decision = SecurityDecisionStatus.DENY
        reason_code = control_denial["reason_code"]
        reasons = control_denial["reasons"]
    resource_id = lease.id
    payload = {
        "subject_kind": "daemon_owner",
        "subject_id": owner,
        "resource_kind": "task_lease",
        "resource_id": resource_id,
        "action": "registered_adapter_execute",
        "adapter_id": adapter_id,
        "task_id": task.id if task is not None else lease.task_id,
        "attempt_id": attempt.id if attempt is not None else lease.attempt_id,
        "task_type": task_type_value,
        "policy_sha256": policy_sha,
        "decision": decision.value,
        "reason_code": reason_code,
        "required_approvals": required_approvals,
        "missing_approvals": missing_approvals,
    }
    decision_id = "secdec_" + hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]
    return SecurityDecision(
        id=decision_id,
        created_at=lease.acquired_at,
        subject_kind="daemon_owner",
        subject_id=owner,
        resource_kind="task_lease",
        resource_id=resource_id,
        action="registered_adapter_execute",
        decision=decision,
        policy_sha256=policy_sha,
        required_approvals=required_approvals,
        satisfied_approvals=satisfied_approvals,
        missing_approvals=missing_approvals,
        adapter_id=str(adapter_id) if adapter_id is not None else None,
        task_type=task_type_value,
        data_boundary=None,
        side_effect_level=None,
        sandbox_profile_id=descriptor.sandbox_profile_id if descriptor is not None else None,
        replay_policy=descriptor.replay_policy if descriptor is not None else None,
        reason_code=reason_code,
        reasons=reasons,
    )


def execute_lease(
    project_root: Path,
    lease_id: str,
    owner: str = DEFAULT_TASK_LEASE_OWNER,
) -> DaemonExecuteResult:
    store = SQLiteStore(project_root)
    lease, task, attempt = _load_lease_context(store, lease_id)
    eligibility = inspect_execution_eligibility(project_root, lease, task, attempt)
    security_decision = evaluate_registered_adapter_security_decision(project_root, lease, task, attempt, owner=owner)
    adapter_id = eligibility.get("adapter_id")
    if security_decision.decision != SecurityDecisionStatus.ALLOW:
        reason_code = security_decision.reason_code
        rejection_reasons = list(security_decision.reasons)
        decision = EXECUTION_DUPLICATE_REJECTED if reason_code == "duplicate_run" else EXECUTION_ADAPTER_REJECTED
        lease, task, attempt = _record_adapter_rejection(
            store,
            lease=lease,
            task=task,
            attempt=attempt,
            adapter_id=adapter_id,
            reason_code=reason_code,
            rejection_reasons=rejection_reasons,
            decision=decision,
            security_decision_id=security_decision.id,
            policy_sha256=security_decision.policy_sha256,
        )
        context_provenance = store.build_context_provenance(task=task)
        return DaemonExecuteResult(
            ok=False,
            decision=decision,
            adapter_id=adapter_id,
            project_root=store.project_root,
            task=task,
            attempt=attempt,
            lease=lease,
            policy_sha256=eligibility.get("policy_sha256"),
            security_decision=security_decision,
            context_provenance=context_provenance,
            untrusted_context_warnings=_dedupe_context_warnings(context_provenance),
            blocked_state_explanations=explanations_from_security_decision(
                security_decision,
                lease_id=lease.id,
                project_root=str(store.project_root),
            ),
            rejection_reasons=rejection_reasons,
        )
    adapter = builtin_execution_adapters()[str(adapter_id)]
    try:
        result = adapter.execute(project_root, lease_id, owner=owner)
        result.security_decision = security_decision
        if result.manifest is not None:
            result.context_provenance = list(result.manifest.context_provenance)
            result.untrusted_context_warnings = list(result.manifest.untrusted_context_warnings)
        else:
            result.context_provenance = store.build_context_provenance(task=task)
            result.untrusted_context_warnings = _dedupe_context_warnings(result.context_provenance)
        result.blocked_state_explanations = explanations_from_reasons(
            list(result.rejection_reasons) + list(result.errors),
            inspect_command=f"harness daemon inspect-lease {lease_id} --project {project_root} --output json",
        )
        return result
    except Exception as exc:
        sanitized = str(sanitize_for_logging(str(exc)))
        refreshed_lease, refreshed_task, refreshed_attempt, run, manifest = _record_adapter_execution_failure(
            store,
            lease=lease,
            task=task,
            attempt=attempt,
            adapter_id=adapter_id,
            owner=owner,
            error=sanitized,
            security_decision=security_decision,
        )
        context_provenance = store.build_context_provenance(
            task=refreshed_task,
            run_id=run.id if run is not None else None,
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
            security_decision=security_decision,
            context_provenance=context_provenance,
            untrusted_context_warnings=_dedupe_context_warnings(context_provenance),
            blocked_state_explanations=explanations_from_reasons(
                [sanitized],
                inspect_command=f"harness daemon inspect-lease {lease_id} --project {store.project_root} --output json",
            ),
            rejection_reasons=[sanitized],
            errors=[sanitized],
        )


def _load_lease_context(store: SQLiteStore, lease_id: str) -> tuple[TaskLease, TaskRecord | None, TaskAttempt | None]:
    lease = store.get_task_lease(lease_id)
    task = _safe_get_task(store, lease.task_id)
    attempt = _safe_get_attempt(store, lease.attempt_id)
    return lease, task, attempt


def _dedupe_context_warnings(records: list[Any]) -> list[str]:
    warnings: list[str] = []
    for record in records:
        for warning in getattr(record, "warnings", []):
            if warning not in warnings:
                warnings.append(warning)
    return warnings


def _missing_descriptor_approvals(
    project_root: Path,
    descriptor: ExecutionAdapterDescriptor | None,
    task: TaskRecord | None,
    task_type: str | None,
) -> list[str]:
    if descriptor is None or task_type is None:
        return []
    missing: list[str] = []
    for approval in descriptor.required_approvals:
        if approval == "hosted_provider_codex":
            found = ApprovalStore(project_root).find_valid(
                "codex_cli",
                "hosted_provider",
                task_type,
                adapter_id=descriptor.id,
                workbench_id=task.workbench_id if task is not None else None,
                objective_id=task.objective_id if task is not None else None,
            )
            if found is None:
                missing.append(approval)
        else:
            missing.append(approval)
    return missing


def _runtime_control_denial(
    project_root: Path,
    descriptor: ExecutionAdapterDescriptor | None,
    task_type: str | None,
    decision: SecurityDecisionStatus,
) -> dict[str, Any] | None:
    if descriptor is None:
        return None
    try:
        store = SQLiteStore(project_root)
        controls = store.active_execution_controls()
        for control in controls:
            if runtime_control_matches_descriptor(control, descriptor, task_type):
                return {
                    "reason_code": "control_disabled",
                    "reasons": [
                        str(
                            sanitize_for_logging(
                                f"Execution control disabled {control.target_kind.value}:{control.target_id}. {control.reason}"
                            )
                        )
                    ],
                }
        if decision == SecurityDecisionStatus.ALLOW:
            breaker = store.adapter_breaker_state(descriptor.id)
            if breaker.status == BreakerStatus.OPEN:
                return {
                    "reason_code": "breaker_open",
                    "reasons": [
                        str(
                            sanitize_for_logging(
                                f"Adapter breaker is open for {descriptor.id}: "
                                f"{breaker.failure_count}/{breaker.threshold} failures in {breaker.window_seconds} seconds."
                            )
                        )
                    ],
                }
    except Exception as exc:
        if _descriptor_is_high_risk(descriptor, task_type):
            return {
                "reason_code": "control_state_unavailable",
                "reasons": [str(sanitize_for_logging(f"Runtime control state unavailable: {exc}"))],
            }
    return None


def runtime_control_matches_descriptor(
    control: KillSwitchRecord,
    descriptor: ExecutionAdapterDescriptor,
    task_type: str | None,
) -> bool:
    target = control.target_id or "*"
    if control.target_kind == KillSwitchTargetKind.ADAPTER:
        return target in {"*", descriptor.id}
    if control.target_kind == KillSwitchTargetKind.TASK_TYPE:
        return target == "*" or target == task_type or target in descriptor.supported_task_types
    if control.target_kind == KillSwitchTargetKind.BACKEND:
        return target in {"*", "codex_cli"} and _descriptor_uses_codex_backend(descriptor)
    if control.target_kind == KillSwitchTargetKind.HOSTED_BOUNDARY:
        return target == "*" and _descriptor_crosses_hosted_boundary(descriptor)
    if control.target_kind == KillSwitchTargetKind.DOCKER_EXECUTION:
        return target == "*" and (task_type == "docker_run_tests" or "docker" in descriptor.id)
    return False


def _descriptor_uses_codex_backend(descriptor: ExecutionAdapterDescriptor) -> bool:
    return any("codex_cli" in requirement for requirement in descriptor.backend_requirements)


def _descriptor_crosses_hosted_boundary(descriptor: ExecutionAdapterDescriptor) -> bool:
    return "hosted_provider_codex" in descriptor.required_approvals or any(
        "data_boundary=hosted_provider" in requirement for requirement in descriptor.backend_requirements
    )


def _descriptor_is_high_risk(descriptor: ExecutionAdapterDescriptor, task_type: str | None) -> bool:
    return (
        _descriptor_crosses_hosted_boundary(descriptor)
        or task_type == "docker_run_tests"
        or "docker" in descriptor.id
    )


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
    sandbox_reasons = _sandbox_profile_rejection_reasons(descriptor)
    delegate_budget_reasons = _delegate_budget_descriptor_rejection_reasons(descriptor)
    if task is None:
        reason_code = "missing_task"
        reasons.append("Task not found.")
    elif attempt is None:
        reason_code = "missing_attempt"
        reasons.append("Task attempt not found.")
    elif sandbox_reasons:
        reason_code = "sandbox_profile_mismatch"
        reasons.extend(sandbox_reasons)
    elif delegate_budget_reasons:
        reason_code = "delegate_budget_mismatch"
        reasons.extend(delegate_budget_reasons)
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
        budget_rejections = task_delegate_budget_rejection_reasons(descriptor, task.metadata)
        if budget_rejections:
            reason_code = "unsafe_metadata"
            reasons.extend(budget_rejections)
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


def _sandbox_profile_rejection_reasons(descriptor: ExecutionAdapterDescriptor) -> list[str]:
    profile_id = descriptor.sandbox_profile_id
    if not profile_id:
        return [f"{descriptor.id} adapter does not declare a sandbox_profile_id."]
    try:
        profile = get_sandbox_profile(profile_id)
    except KeyError:
        return [f"{descriptor.id} adapter references unknown sandbox_profile_id={profile_id}."]
    if profile.schema_version != "harness.sandbox_profile/v1":
        return [
            f"{descriptor.id} adapter sandbox_profile_id={profile_id} has unsupported "
            f"schema_version={profile.schema_version}."
        ]
    return []


def _adapter_descriptor_contract_rejection_reasons(descriptor: ExecutionAdapterDescriptor) -> list[str]:
    return [
        *_sandbox_profile_rejection_reasons(descriptor),
        *_delegate_budget_descriptor_rejection_reasons(descriptor),
    ]


def _delegate_budget_descriptor_rejection_reasons(descriptor: ExecutionAdapterDescriptor) -> list[str]:
    try:
        profile = get_sandbox_profile(descriptor.sandbox_profile_id) if descriptor.sandbox_profile_id else None
    except KeyError:
        profile = None
    return [
        f"{descriptor.id} adapter delegate_budget is invalid: {reason}."
        for reason in validate_adapter_delegate_budget(descriptor, profile=profile)
    ]


def _validate_task_against_descriptor(descriptor: ExecutionAdapterDescriptor, task: TaskRecord) -> None:
    reasons: list[str] = []
    for key, expected in descriptor.required_task_metadata.items():
        if task.metadata.get(key) != expected:
            reasons.append(f"Execution requires {key}={expected}.")
    rejected = sorted(key for key in descriptor.rejected_task_metadata if bool(task.metadata.get(key)))
    if rejected:
        reasons.append(f"Execution rejected by task metadata: {', '.join(rejected)}.")
    reasons.extend(task_delegate_budget_rejection_reasons(descriptor, task.metadata))
    task_type = task.metadata.get("task_type")
    if task_type not in descriptor.supported_task_types:
        reasons.append(f"Unsupported task_type for {descriptor.id}: {task_type}.")
    if reasons:
        raise ValueError(" ".join(reasons))


def _codex_backend_rejection_reasons(backend_config: Any, *, adapter_label: str = "Codex isolated edit") -> list[str]:
    if backend_config is None:
        return [f"{adapter_label} requires configured codex_cli backend."]
    reasons: list[str] = []
    if backend_config.name != "codex_cli":
        reasons.append(f"{adapter_label} requires backend name codex_cli.")
    if backend_config.kind != BackendKind.EXTERNAL_AGENT:
        reasons.append(f"{adapter_label} requires external_agent backend.")
    if backend_config.metadata.billing_mode == BillingMode.PAID_API:
        reasons.append(f"{adapter_label} must not use paid API billing.")
    if backend_config.metadata.data_boundary != DataBoundary.HOSTED_PROVIDER:
        reasons.append(f"{adapter_label} requires hosted_provider data boundary.")
    if backend_config.metadata.execution_location not in {ExecutionLocation.MIXED, ExecutionLocation.HOSTED}:
        reasons.append(f"{adapter_label} requires hosted or mixed execution location.")
    if backend_config.metadata.allow_network:
        reasons.append(f"{adapter_label} requires backend allow_network=false.")
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


def _repo_planning_decision_from_status(status: str) -> tuple[str, bool, str, str | None]:
    if status == "completed":
        return "repo_planning_completed", True, "completed", None
    if status == "policy_violation":
        return "repo_planning_blocked_policy", False, "policy_violation", "repo_planning_policy_violation"
    return "repo_planning_failed", False, "failed", "repo_planning_failed"


def _review_gate_rejection_reasons(task: TaskRecord) -> list[str]:
    return _review_gate_payload_rejection_reasons(
        task_type=task.metadata.get("task_type"),
        metadata=task.metadata,
        agent_id=task.agent_id,
        depends_on=task.depends_on,
    )


def _review_gate_payload_rejection_reasons(
    *,
    task_type: Any,
    metadata: Mapping[str, Any],
    agent_id: str | None,
    depends_on: Sequence[str],
) -> list[str]:
    expected_role = REVIEW_ROLE_BY_TASK_TYPE.get(str(task_type))
    reasons: list[str] = []
    if metadata.get("review_gate") is not True:
        reasons.append("Review gate execution requires review_gate=true.")
    if metadata.get("completion_gate") is not True:
        reasons.append("Review gate execution requires completion_gate=true.")
    if expected_role is None:
        reasons.append(f"Unsupported review task type: {task_type}.")
    else:
        if agent_id != expected_role:
            reasons.append(f"Review gate execution requires agent_id={expected_role}.")
        if metadata.get("review_role") != expected_role:
            reasons.append(f"Review gate execution requires review_role={expected_role}.")
    if metadata.get("workflow_stage") != task_type:
        reasons.append(f"Review gate execution requires workflow_stage={task_type}.")
    if not depends_on:
        reasons.append("Review gate execution requires at least one upstream dependency.")
    review_target_stage = metadata.get("review_target_stage")
    if not isinstance(review_target_stage, str) or not review_target_stage.strip():
        reasons.append("Review gate execution requires review_target_stage metadata.")
    if task_type == SECURITY_REVIEW_TASK_TYPE and metadata.get("blocks_apply_back") is not True:
        reasons.append("Security review gate requires blocks_apply_back=true.")
    return reasons


def _build_review_gate_report(
    store: SQLiteStore,
    task: TaskRecord,
    run_id: str,
    policy_sha256: str,
) -> dict[str, Any]:
    task_type = str(task.metadata.get("task_type") or "")
    expected_role = REVIEW_ROLE_BY_TASK_TYPE.get(task_type)
    review_role = str(task.metadata.get("review_role") or expected_role or "unknown")
    workflow_stage = str(task.metadata.get("workflow_stage") or task_type or "unknown")
    contract_reasons = _review_gate_rejection_reasons(task)
    upstream_tasks: list[dict[str, Any]] = []
    dependency_reasons: list[str] = []
    for dependency_id in task.depends_on:
        upstream_tasks.append(_review_gate_upstream_task_report(store, dependency_id, dependency_reasons))
    target_stage_tasks, target_stage_reasons = _review_gate_target_stage_evidence(store, task)

    checks: list[dict[str, Any]] = [
        {
            "id": "review_gate_contract",
            "status": "failed" if contract_reasons else "passed",
            "summary": "Reviewer task metadata matches the registered adapter contract."
            if not contract_reasons
            else "Reviewer task metadata does not match the registered adapter contract.",
            "reasons": contract_reasons,
        },
        {
            "id": "dependency_evidence",
            "status": "failed" if dependency_reasons else "passed",
            "summary": "Every upstream dependency has succeeded run and artifact evidence."
            if not dependency_reasons
            else "One or more upstream dependencies are missing succeeded run or artifact evidence.",
            "reasons": dependency_reasons,
        },
        {
            "id": "review_target_stage_evidence",
            "status": "failed" if target_stage_reasons else "passed",
            "summary": "The declared review target stage is present in the upstream dependency graph with run and artifact evidence."
            if not target_stage_reasons
            else "The declared review target stage is missing or incomplete in the upstream dependency graph.",
            "reasons": target_stage_reasons,
        },
        {
            "id": "no_tool_execution",
            "status": "passed",
            "summary": "The review gate wrote local Harness evidence only and did not invoke tools, shell, network, Docker, hosted providers, or paid providers.",
            "reasons": [],
        },
    ]
    if task_type == SECURITY_REVIEW_TASK_TYPE:
        apply_back_reasons = (
            []
            if task.metadata.get("blocks_apply_back") is True
            else ["Security review gate must block apply-back by default."]
        )
        checks.append(
            {
                "id": "apply_back_gate",
                "status": "failed" if apply_back_reasons else "passed",
                "summary": "Security review blocks apply-back unless a separate approval path is used."
                if not apply_back_reasons
                else "Security review is not configured to block apply-back.",
                "reasons": apply_back_reasons,
            }
        )

    passed = all(check["status"] == "passed" for check in checks)
    return sanitize_for_logging(
        {
            "schema_version": "harness.review_gate_report/v1",
            "adapter_id": REVIEW_GATE_EXECUTION_ADAPTER,
            "decision": "review_gate_passed" if passed else "review_gate_failed",
            "verdict": "passed" if passed else "failed",
            "task_id": task.id,
            "objective_id": task.objective_id,
            "run_id": run_id,
            "task_type": task_type,
            "workflow_stage": workflow_stage,
            "review_role": review_role,
            "review_target_stage": task.metadata.get("review_target_stage"),
            "blocks_apply_back": bool(task.metadata.get("blocks_apply_back")),
            "policy_sha256": policy_sha256,
            "checks": checks,
            "upstream_tasks": upstream_tasks,
            "target_stage_tasks": target_stage_tasks,
            "side_effects": ["local_harness_evidence_only"],
            "summary": (
                f"{review_role} review gate passed with {len(upstream_tasks)} upstream evidence record(s)."
                if passed
                else f"{review_role} review gate failed; inspect checks for missing contract or evidence."
            ),
        }
    )


def _review_gate_target_stage_evidence(
    store: SQLiteStore,
    task: TaskRecord,
) -> tuple[list[dict[str, Any]], list[str]]:
    target_stage = task.metadata.get("review_target_stage")
    if not isinstance(target_stage, str) or not target_stage.strip():
        return [], []
    upstream_tasks, traversal_reasons = _review_gate_upstream_task_graph(store, task)
    target_tasks = [
        _review_gate_task_evidence_summary(store, upstream_task)
        for upstream_task in upstream_tasks
        if upstream_task.metadata.get("workflow_stage") == target_stage
    ]
    reasons = list(traversal_reasons)
    if not target_tasks:
        reasons.append(f"Review target stage has no upstream evidence: {target_stage}.")
    for target_task in target_tasks:
        task_id = target_task["task_id"]
        if target_task["status"] != TaskStatus.SUCCEEDED.value:
            reasons.append(f"Review target stage task has not succeeded: {task_id} is {target_task['status']}.")
        if target_task["run_id"] is None:
            reasons.append(f"Review target stage task has no run evidence: {task_id}.")
        if not target_task["artifact_kinds"]:
            reasons.append(f"Review target stage task has no artifact evidence: {task_id}.")
    return target_tasks, reasons


def _review_gate_upstream_task_graph(
    store: SQLiteStore,
    task: TaskRecord,
) -> tuple[list[TaskRecord], list[str]]:
    tasks: list[TaskRecord] = []
    reasons: list[str] = []
    visited: set[str] = set()
    queue = list(task.depends_on)
    while queue:
        dependency_id = queue.pop(0)
        if dependency_id in visited:
            continue
        visited.add(dependency_id)
        try:
            dependency = store.get_task(dependency_id)
        except KeyError:
            reasons.append(f"Upstream dependency graph is missing task: {dependency_id}.")
            continue
        tasks.append(dependency)
        queue.extend(dependency.depends_on)
    return tasks, reasons


def _review_gate_task_evidence_summary(store: SQLiteStore, task: TaskRecord) -> dict[str, Any]:
    artifacts: list[dict[str, Any]] = []
    run_status: str | None = None
    if task.run_id is not None:
        try:
            run = store.get_run(task.run_id)
            run_status = run.status
            artifacts = [
                {
                    "artifact_id": artifact.id,
                    "kind": artifact.kind,
                    "sha256": artifact.sha256,
                    "evidence_status": artifact.evidence_status,
                    "producer": artifact.producer,
                }
                for artifact in store.list_artifacts(run.id)
            ]
        except KeyError:
            run_status = "missing"
    return {
        "task_id": task.id,
        "title": task.title,
        "status": task.status.value,
        "run_id": task.run_id,
        "run_status": run_status,
        "agent_id": task.agent_id,
        "workflow_stage": task.metadata.get("workflow_stage"),
        "execution_adapter": task.metadata.get("execution_adapter"),
        "task_type": task.metadata.get("task_type"),
        "artifact_kinds": sorted({str(artifact["kind"]) for artifact in artifacts}),
        "artifacts": artifacts,
    }


def _review_gate_upstream_task_report(
    store: SQLiteStore,
    dependency_id: str,
    dependency_reasons: list[str],
) -> dict[str, Any]:
    try:
        dependency = store.get_task(dependency_id)
    except KeyError:
        dependency_reasons.append(f"Upstream dependency is missing: {dependency_id}.")
        return {
            "task_id": dependency_id,
            "status": "missing",
            "run_id": None,
            "artifact_kinds": [],
            "artifacts": [],
        }

    artifacts: list[dict[str, Any]] = []
    run_status: str | None = None
    if dependency.run_id is None:
        dependency_reasons.append(f"Upstream dependency has no run evidence: {dependency.id}.")
    else:
        try:
            run = store.get_run(dependency.run_id)
            run_status = run.status
            artifacts = [
                {
                    "artifact_id": artifact.id,
                    "kind": artifact.kind,
                    "path": str(artifact.path),
                    "sha256": artifact.sha256,
                    "evidence_status": artifact.evidence_status,
                    "producer": artifact.producer,
                }
                for artifact in store.list_artifacts(run.id)
            ]
            if not artifacts:
                dependency_reasons.append(f"Upstream dependency has no artifact evidence: {dependency.id}.")
        except KeyError:
            dependency_reasons.append(f"Upstream dependency run evidence is missing: {dependency.id}.")

    if dependency.status != TaskStatus.SUCCEEDED:
        dependency_reasons.append(f"Upstream dependency has not succeeded: {dependency.id} is {dependency.status.value}.")
    return {
        "task_id": dependency.id,
        "title": dependency.title,
        "status": dependency.status.value,
        "run_id": dependency.run_id,
        "run_status": run_status,
        "agent_id": dependency.agent_id,
        "workflow_stage": dependency.metadata.get("workflow_stage"),
        "execution_adapter": dependency.metadata.get("execution_adapter"),
        "task_type": dependency.metadata.get("task_type"),
        "artifact_kinds": sorted({str(artifact["kind"]) for artifact in artifacts}),
        "artifacts": artifacts,
    }


def _render_review_gate_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Review Gate Report",
        "",
        f"- Verdict: {report.get('verdict')}",
        f"- Decision: {report.get('decision')}",
        f"- Review role: {report.get('review_role')}",
        f"- Workflow stage: {report.get('workflow_stage')}",
        f"- Review target stage: {report.get('review_target_stage')}",
        f"- Task id: {report.get('task_id')}",
        f"- Objective id: {report.get('objective_id') or 'none'}",
        f"- Run id: {report.get('run_id')}",
        f"- Policy sha256: {report.get('policy_sha256')}",
        f"- Blocks apply-back: {str(bool(report.get('blocks_apply_back'))).lower()}",
        "",
        "## Checks",
        "",
    ]
    for check in report.get("checks", []):
        reasons = check.get("reasons") or []
        suffix = "" if not reasons else f" Reasons: {'; '.join(str(reason) for reason in reasons)}"
        lines.append(f"- {check.get('id')}: {check.get('status')} - {check.get('summary')}{suffix}")
    lines.extend(["", "## Upstream Evidence", ""])
    upstream_tasks = report.get("upstream_tasks") or []
    if not upstream_tasks:
        lines.append("- none")
    for upstream in upstream_tasks:
        artifact_kinds = ", ".join(str(kind) for kind in upstream.get("artifact_kinds", [])) or "none"
        lines.append(
            "- "
            f"{upstream.get('task_id')}: {upstream.get('status')} "
            f"run={upstream.get('run_id') or 'none'} artifacts={artifact_kinds}"
        )
    lines.extend(["", "## Target Stage Evidence", ""])
    target_stage_tasks = report.get("target_stage_tasks") or []
    if not target_stage_tasks:
        lines.append("- none")
    for target in target_stage_tasks:
        artifact_kinds = ", ".join(str(kind) for kind in target.get("artifact_kinds", [])) or "none"
        lines.append(
            "- "
            f"{target.get('task_id')}: stage={target.get('workflow_stage') or 'none'} "
            f"status={target.get('status')} run={target.get('run_id') or 'none'} artifacts={artifact_kinds}"
        )
    lines.extend(["", "## Side Effects", "", "- local_harness_evidence_only", ""])
    return "\n".join(lines)


def _rejected_result(
    store: SQLiteStore,
    lease: TaskLease,
    task: TaskRecord | None,
    attempt: TaskAttempt | None,
    adapter_id: str,
    decision: str,
    rejection_reasons: list[str],
) -> DaemonExecuteResult:
    try:
        refreshed_lease = store.get_task_lease(lease.id)
    except KeyError:
        refreshed_lease = lease
    refreshed_task = _safe_get_task(store, refreshed_lease.task_id) or task
    refreshed_attempt = _safe_get_attempt(store, refreshed_lease.attempt_id) or attempt
    policy_sha = (
        effective_policy_sha256(resolve_task_effective_policy(refreshed_task))
        if refreshed_task is not None
        else None
    )
    return DaemonExecuteResult(
        ok=False,
        decision=decision,
        adapter_id=adapter_id,
        project_root=store.project_root,
        task=refreshed_task,
        attempt=refreshed_attempt,
        lease=refreshed_lease,
        policy_sha256=policy_sha,
        rejection_reasons=rejection_reasons,
    )


def _record_adapter_execution_failure(
    store: SQLiteStore,
    *,
    lease: TaskLease,
    task: TaskRecord | None,
    attempt: TaskAttempt | None,
    adapter_id: str | None,
    owner: str,
    error: str,
    security_decision: SecurityDecision,
) -> tuple[TaskLease, TaskRecord | None, TaskAttempt | None, Any | None, RunManifest | None]:
    refreshed_lease = store.get_task_lease(lease.id)
    refreshed_attempt = _safe_get_attempt(store, refreshed_lease.attempt_id) or attempt
    refreshed_task = _safe_get_task(store, refreshed_lease.task_id) or task
    run = None
    manifest = None
    run_id = refreshed_attempt.run_id if refreshed_attempt is not None else None
    if run_id is None and refreshed_task is not None:
        run_id = refreshed_task.run_id
    if run_id is not None:
        try:
            store.finish_attempt_run(
                refreshed_lease.id,
                run_id=run_id,
                owner=owner,
                success=False,
                decision=EXECUTION_ADAPTER_REJECTED,
                run_status="failed",
                failure_code="adapter_execution_failed",
                failure_message=error,
            )
        except Exception:
            pass

    refreshed_lease, refreshed_task, refreshed_attempt = _record_adapter_rejection(
        store,
        lease=refreshed_lease,
        task=refreshed_task,
        attempt=refreshed_attempt,
        adapter_id=adapter_id,
        reason_code="adapter_execution_failed",
        rejection_reasons=[error],
        decision=EXECUTION_ADAPTER_REJECTED,
        security_decision_id=security_decision.id,
        policy_sha256=security_decision.policy_sha256,
    )
    if run_id is not None:
        try:
            run = store.get_run(run_id)
            manifest = store.build_run_manifest(run.id)
        except KeyError:
            run = None
            manifest = None
    return refreshed_lease, refreshed_task, refreshed_attempt, run, manifest


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
    decision: str = EXECUTION_ADAPTER_REJECTED,
    security_decision_id: str | None = None,
    policy_sha256: str | None = None,
) -> tuple[TaskLease, TaskRecord | None, TaskAttempt | None]:
    refreshed_lease, refreshed_attempt, refreshed_task = store.finalize_rejected_task_lease(
        lease.id,
        reason_code=reason_code,
        rejection_reasons=rejection_reasons,
        decision=decision,
        adapter_id=adapter_id,
        security_decision_id=security_decision_id,
        policy_sha256=policy_sha256,
        owner=lease.owner,
    )
    daemon = store.ensure_daemon(owner=refreshed_lease.owner)
    store.record_daemon_event(
        daemon.id,
        event_type=EXECUTION_ADAPTER_REJECTED,
        message="Execution adapter dispatch or execution was rejected.",
        metadata=sanitize_for_logging(
            {
                "lease_id": refreshed_lease.id,
                "task_id": refreshed_task.id if refreshed_task is not None else refreshed_lease.task_id,
                "attempt_id": refreshed_attempt.id if refreshed_attempt is not None else refreshed_lease.attempt_id,
                "adapter_id": adapter_id,
                "decision": decision,
                "reason_code": reason_code,
                "error": rejection_reasons[0]
                if reason_code == "adapter_execution_failed" and rejection_reasons
                else None,
                "rejection_reasons": rejection_reasons,
                "lease_status": refreshed_lease.status.value,
                "task_status": refreshed_task.status.value if refreshed_task is not None else None,
                "attempt_status": refreshed_attempt.status.value if refreshed_attempt is not None else None,
                "security_decision_id": security_decision_id,
                "policy_sha256": policy_sha256,
            }
        ),
    )
    return refreshed_lease, refreshed_task, refreshed_attempt
