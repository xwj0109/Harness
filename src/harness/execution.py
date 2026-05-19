from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel

from harness.approvals import ApprovalStore
from harness.backends.codex_cli import CodexCliBackend, CodexSandboxUnavailable, CodexUnavailable
from harness.backends.local_openai import LocalEndpointUnavailable
from harness.codex_edit_runner import CodexCodeEditRunner
from harness.codex_runner import CodexRepoPlanningRunner
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
    BreakerStatus,
    KillSwitchTargetKind,
    RunManifest,
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
        sandbox_profile_id=NONE_SANDBOX_PROFILE,
        side_effect_summary="Writes harness run/task/lease/artifact evidence only.",
        replay_policy=ToolReplayPolicy.IDEMPOTENT_WITH_KEY,
        safety_notes=[
            "Descriptors are documentation and validation metadata, not permission grants.",
            "Execution still requires an active lease and exact task metadata.",
        ],
        autonomy_default="auto_allowed",
        max_autonomous_retries=0,
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


def builtin_execution_adapters() -> dict[str, ExecutionAdapter]:
    adapters: list[ExecutionAdapter] = [
        DryRunExecutionAdapter(),
        ReadOnlySummaryExecutionAdapter(),
        SessionOperatorExecutionAdapter(),
        CodexIsolatedEditExecutionAdapter(),
        RepoPlanningExecutionAdapter(),
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
        context_provenance = store.build_context_provenance(task=task)
        _record_adapter_rejection(
            store,
            lease=lease,
            task=task,
            attempt=attempt,
            adapter_id=adapter_id,
            reason_code=reason_code,
            rejection_reasons=rejection_reasons,
            security_decision_id=security_decision.id,
            policy_sha256=security_decision.policy_sha256,
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
    except (KeyError, ValueError, LocalEndpointUnavailable, CodexUnavailable, CodexSandboxUnavailable) as exc:
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
        context_provenance = store.build_context_provenance(
            task=refreshed_task,
            run_id=run.id if run is not None else None,
        )
        _record_adapter_rejection(
            store,
            lease=refreshed_lease,
            task=refreshed_task,
            attempt=refreshed_attempt,
            adapter_id=adapter_id,
            reason_code="adapter_execution_failed",
            rejection_reasons=[sanitized],
            security_decision_id=security_decision.id,
            policy_sha256=security_decision.policy_sha256,
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
            if _control_matches_descriptor(control.target_kind, control.target_id, descriptor, task_type):
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


def _control_matches_descriptor(
    target_kind: KillSwitchTargetKind,
    target_id: str,
    descriptor: ExecutionAdapterDescriptor,
    task_type: str | None,
) -> bool:
    target = target_id or "*"
    if target_kind == KillSwitchTargetKind.ADAPTER:
        return target in {"*", descriptor.id}
    if target_kind == KillSwitchTargetKind.TASK_TYPE:
        return target == "*" or target == task_type or target in descriptor.supported_task_types
    if target_kind == KillSwitchTargetKind.BACKEND:
        return target in {"*", "codex_cli"} and _descriptor_uses_codex_backend(descriptor)
    if target_kind == KillSwitchTargetKind.HOSTED_BOUNDARY:
        return target == "*" and _descriptor_crosses_hosted_boundary(descriptor)
    if target_kind == KillSwitchTargetKind.DOCKER_EXECUTION:
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
    security_decision_id: str | None = None,
    policy_sha256: str | None = None,
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
                "security_decision_id": security_decision_id,
                "policy_sha256": policy_sha256,
            }
        ),
    )
