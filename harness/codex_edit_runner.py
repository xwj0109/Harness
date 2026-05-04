from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from harness.approvals import ApprovalProfile, ApprovalStore
from harness.backends.codex_cli import (
    CodexCliBackend,
    CodexEditCommandUnavailable,
    CodexRunResult,
    CodexSandboxUnavailable,
    CodexUnavailable,
    write_codex_artifacts,
)
from harness.codex_runner import HostedBoundaryApprovalRequired
from harness.events import append_jsonl
from harness.isolation import ActiveRepoDirtyError, IsolationManager, inspect_isolated_diff
from harness.memory.sqlite_store import SQLiteStore
from harness.security import sanitize_for_logging
from harness.tools.patch import PatchValidationError, apply_planned_updates, plan_unified_diff


class ActiveProjectModifiedError(RuntimeError):
    pass


@dataclass
class ApplyBackDecision:
    decision: str
    reason: str | None = None

    @property
    def approved(self) -> bool:
        return self.decision == "approved"


class ApplyBackApprovalProvider(Protocol):
    def decide(self, diff_summary: str, full_diff: str, diff_artifact: Path) -> ApplyBackDecision:
        ...


class DenyByDefaultApplyBackApproval:
    def decide(self, diff_summary: str, full_diff: str, diff_artifact: Path) -> ApplyBackDecision:
        return ApplyBackDecision(
            decision="denied",
            reason="No apply-back approval provider configured.",
        )


@dataclass
class FreshnessCheckResult:
    ok: bool
    active_pre_apply_status: str
    active_post_apply_status: str = ""
    target_hash_checks: list[dict[str, str | bool]] | None = None
    reason: str | None = None


class CodexCodeEditRunner:
    def __init__(
        self,
        project_root: Path,
        store: SQLiteStore,
        backend: CodexCliBackend,
        approval_store: ApprovalStore,
        isolation_manager: IsolationManager | None = None,
        apply_back_approval_provider: ApplyBackApprovalProvider | None = None,
    ) -> None:
        self.project_root = project_root.resolve()
        self.store = store
        self.backend = backend
        self.approval_store = approval_store
        self.isolation_manager = isolation_manager or IsolationManager()
        self.apply_back_approval_provider = apply_back_approval_provider or DenyByDefaultApplyBackApproval()

    def run(
        self,
        goal: str,
        task_type: str,
        approval: ApprovalProfile | None,
        keep_isolation: bool = False,
    ) -> dict[str, Any]:
        if approval is None:
            raise HostedBoundaryApprovalRequired(
                "A valid hosted data-boundary project approval profile is required for codex_code_edit. "
                "Create one with: harness approvals add --backend codex_cli --data-boundary hosted_provider "
                "--task-types codex_code_edit --duration-days <days>"
            )
        status = self.backend.preflight()
        if not status.available:
            raise CodexUnavailable(status.reason or "Codex CLI is unavailable.")
        run = self.store.create_run(
            goal=goal,
            task_type=task_type,
            status="running",
            backend=self.backend.config.model_copy(update={"capabilities": status.capabilities}),
            approval_id=approval.id,
        )
        paths = self.store.initialize_run_artifacts(run.id)
        for kind, path in paths.items():
            self.store.register_artifact(run.id, kind=kind, path=path)
        run_dir = self.store.runs_dir / run.id
        workspace = None
        result: CodexRunResult | None = None
        diff_result = None
        network_status = ""
        apply_back_decision = ApplyBackDecision(decision="not_requested", reason=None)
        freshness_result = FreshnessCheckResult(ok=False, active_pre_apply_status="")
        applied_files: list[str] = []
        apply_back_failure: str | None = None
        final_message_path = run_dir / "codex_final_message.md"
        manifest_path = run_dir / "baseline_manifest.json"
        unified_diff_path = run_dir / "isolated_unified_diff.patch"
        diff_stat_path = run_dir / "isolated_diff_stat.txt"
        try:
            workspace = self.isolation_manager.create(self.project_root)
            workspace.baseline_manifest.write_json(manifest_path)
            for kind, path in {
                "baseline_manifest": manifest_path,
                "isolated_unified_diff": unified_diff_path,
                "isolated_diff_stat": diff_stat_path,
            }.items():
                self.store.register_artifact(run.id, kind=kind, path=path)
            self.store.append_event(
                run.id,
                "info",
                "isolation_created",
                "Created isolated workspace for Codex edit run.",
                {
                    "strategy": workspace.strategy,
                    "isolated_workspace": str(workspace.path),
                    "active_project": str(self.project_root),
                    "agents_md_exists": workspace.agents_md_exists,
                    "warnings": workspace.warnings,
                    "active_pre_isolation_git_status": workspace.active_pre_isolation_git_status,
                },
            )
            active_hashes = {path: entry.sha256 for path, entry in workspace.baseline_manifest.entries.items()}
            prompt = self._build_payload(goal, task_type)
            append_jsonl(paths["transcript"], {"role": "user", "content": sanitize_for_logging(prompt)})
            result, detected_capabilities, network_status = self.backend.run_edit(workspace.path, prompt, final_message_path)
            codex_artifacts = write_codex_artifacts(run_dir, result)
            for kind, path in codex_artifacts.items():
                self.store.register_artifact(run.id, kind=kind, path=path)
            if final_message_path.exists():
                final_message_path.write_text(
                    str(sanitize_for_logging(final_message_path.read_text(encoding="utf-8", errors="replace"))),
                    encoding="utf-8",
                )
                self.store.register_artifact(run.id, "codex_final_message", final_message_path)
            diff_result = inspect_isolated_diff(workspace.path, workspace.baseline_manifest)
            unified_diff_path.write_text(str(sanitize_for_logging(diff_result.unified_diff)), encoding="utf-8")
            diff_stat_path.write_text(str(sanitize_for_logging(diff_result.diff_stat)), encoding="utf-8")
            self.store.append_event(
                run.id,
                "info",
                "codex_code_edit_completed",
                "Codex edit subprocess completed inside isolated workspace.",
                {
                    "exit_status": result.exit_status,
                    "command": _redacted_command(result.command),
                    "json_event_count": len(result.json_events),
                    "capabilities": detected_capabilities.model_dump(mode="json"),
                    "network_status": network_status,
                },
            )
            self.store.append_event(
                run.id,
                "info" if diff_result.valid else "warning",
                "isolated_diff_inspected",
                "Inspected isolated workspace diff. Apply-back is not implemented in C2.",
                {
                    "changed_files": diff_result.changed_files,
                    "allowed_changed_files": diff_result.allowed_changed_files,
                    "diff_stat": diff_result.diff_stat,
                    "violations": [violation.to_dict() for violation in diff_result.violations],
                },
            )
            if result.exit_status == 0 and diff_result.valid and diff_result.allowed_changed_files:
                apply_back_decision, freshness_result, applied_files, apply_back_failure = self._handle_apply_back(
                    run_id=run.id,
                    diff_result=diff_result,
                    diff_artifact=unified_diff_path,
                    baseline_hashes=active_hashes,
                    pre_isolation_status=workspace.active_pre_isolation_git_status,
                )
            elif result.exit_status == 0 and not diff_result.valid:
                apply_back_decision = ApplyBackDecision(decision="blocked", reason="Isolated diff failed policy validation.")
                self._persist_apply_back_decision(run.id, apply_back_decision)
            elif result.exit_status == 0:
                apply_back_decision = ApplyBackDecision(decision="not_requested", reason="No valid isolated changes to apply.")
                self._persist_apply_back_decision(run.id, apply_back_decision)
            status_value = self._status_value(result.exit_status, diff_result, apply_back_decision, apply_back_failure)
            if result.exit_status != 0:
                status_value = "failed"
            return_payload = self._complete_run(
                run_id=run.id,
                status_value=status_value,
                goal=goal,
                task_type=task_type,
                approval=approval,
                workspace=workspace,
                keep_isolation=keep_isolation,
                result=result,
                network_status=network_status,
                diff_result=diff_result,
                apply_back_decision=apply_back_decision,
                freshness_result=freshness_result,
                applied_files=applied_files,
                apply_back_failure=apply_back_failure,
                artifact_paths={
                    **paths,
                    "baseline_manifest": manifest_path,
                    "isolated_unified_diff": unified_diff_path,
                    "isolated_diff_stat": diff_stat_path,
                },
            )
            return return_payload
        except ActiveRepoDirtyError:
            self.store.update_run_status(run.id, "failed")
            raise
        except (CodexEditCommandUnavailable, ActiveProjectModifiedError):
            self.store.update_run_status(run.id, "failed")
            raise
        finally:
            if workspace is not None and not keep_isolation and workspace.cleanup_status == "not_cleaned":
                workspace.cleanup()

    def _complete_run(
        self,
        run_id: str,
        status_value: str,
        goal: str,
        task_type: str,
        approval: ApprovalProfile,
        workspace: Any,
        keep_isolation: bool,
        result: CodexRunResult,
        network_status: str,
        diff_result: Any,
        apply_back_decision: ApplyBackDecision,
        freshness_result: FreshnessCheckResult,
        applied_files: list[str],
        apply_back_failure: str | None,
        artifact_paths: dict[str, Path],
    ) -> dict[str, Any]:
        cleanup_status = "kept" if keep_isolation else "pending_cleanup"
        if not keep_isolation:
            workspace.cleanup()
            cleanup_status = workspace.cleanup_status
        self.store.update_run_status(run_id, status_value)
        self._write_report(
            run_id=run_id,
            goal=goal,
            task_type=task_type,
            approval_id=approval.id,
            workspace=workspace,
            cleanup_status=cleanup_status,
            result=result,
            network_status=network_status,
            diff_result=diff_result,
            apply_back_decision=apply_back_decision,
            freshness_result=freshness_result,
            applied_files=applied_files,
            apply_back_failure=apply_back_failure,
            artifact_paths=artifact_paths,
        )
        return {
            "run_id": run_id,
            "status": status_value,
            "approval_id": approval.id,
            "isolation_strategy": workspace.strategy,
            "isolated_workspace": str(workspace.path),
            "isolation_cleanup_status": cleanup_status,
            "changed_files": diff_result.changed_files,
            "policy_violations": [violation.to_dict() for violation in diff_result.violations],
            "apply_back_decision": apply_back_decision.decision,
            "freshness_result": {
                "ok": freshness_result.ok,
                "reason": freshness_result.reason,
                "target_hash_checks": freshness_result.target_hash_checks or [],
            },
            "applied_files": applied_files,
            "apply_back_failure": apply_back_failure,
            "artifacts": {key: str(path) for key, path in artifact_paths.items()},
        }

    def _build_payload(self, goal: str, task_type: str) -> str:
        return (
            "You are Codex running under a supervised harness for isolated code editing.\n"
            "Edit only within the workspace supplied by --cd. Do not access secrets. Do not use network unless the CLI sandbox allows it.\n"
            "The harness will inspect the isolated diff after you exit. Apply-back requires explicit user approval.\n"
            f"Task type: {task_type}\n"
            f"Goal: {goal}\n"
        )

    def _handle_apply_back(
        self,
        run_id: str,
        diff_result: Any,
        diff_artifact: Path,
        baseline_hashes: dict[str, str],
        pre_isolation_status: str,
    ) -> tuple[ApplyBackDecision, FreshnessCheckResult, list[str], str | None]:
        summary = self._apply_back_summary(diff_result)
        decision = self.apply_back_approval_provider.decide(summary, diff_result.unified_diff, diff_artifact)
        self._persist_apply_back_decision(run_id, decision)
        if not decision.approved:
            return decision, FreshnessCheckResult(ok=False, active_pre_apply_status="", reason="Apply-back denied."), [], None
        freshness = self._check_freshness(diff_result.allowed_changed_files, baseline_hashes, pre_isolation_status)
        if not freshness.ok:
            self.store.append_event(
                run_id,
                "warning",
                "apply_back_freshness_failed",
                "Apply-back failed closed because active project changed since isolation.",
                {
                    "reason": freshness.reason,
                    "active_pre_apply_status": freshness.active_pre_apply_status,
                    "target_hash_checks": freshness.target_hash_checks or [],
                },
            )
            return decision, freshness, [], freshness.reason or "Freshness check failed."
        try:
            summary_obj, updates = plan_unified_diff(
                diff_result.unified_diff,
                self.project_root,
                self.isolation_manager.excluded_patterns,
            )
            apply_planned_updates(updates)
        except Exception as exc:
            reason = str(sanitize_for_logging(str(exc)))
            self.store.append_event(
                run_id,
                "error",
                "apply_back_validation_or_apply_failed",
                "Apply-back failed validation or atomic application.",
                {"reason": reason},
            )
            return decision, freshness, [], reason
        post_status = self._git_status_porcelain()
        freshness.active_post_apply_status = post_status
        self.store.append_event(
            run_id,
            "info",
            "apply_back_applied",
            "Approved isolated diff was applied to the active project.",
            {"files": summary_obj.files, "active_post_apply_status": post_status},
        )
        return decision, freshness, summary_obj.files, None

    def _persist_apply_back_decision(self, run_id: str, decision: ApplyBackDecision) -> None:
        self.store.append_event(
            run_id,
            "info",
            "apply_back_decision",
            "Persisted per-run apply-back decision.",
            {"decision": decision.decision, "reason": decision.reason},
        )

    def _check_freshness(
        self,
        target_files: list[str],
        baseline_hashes: dict[str, str],
        pre_isolation_status: str,
    ) -> FreshnessCheckResult:
        active_status = self._git_status_porcelain()
        checks: list[dict[str, str | bool]] = []
        ok = True
        reason = None
        if active_status != pre_isolation_status:
            ok = False
            reason = "Active git status changed since isolation was created."
        for relative_path in target_files:
            expected = baseline_hashes.get(relative_path, "")
            path = self.project_root / relative_path
            exists = path.exists() and path.is_file() and not path.is_symlink()
            actual = _sha256_path(path) if exists else ""
            matches = bool(exists and expected and actual == expected)
            checks.append(
                {
                    "path": relative_path,
                    "expected_sha256": expected,
                    "actual_sha256": actual,
                    "matches": matches,
                }
            )
            if not matches:
                ok = False
                reason = reason or f"Target file changed since isolation was created: {relative_path}"
        return FreshnessCheckResult(
            ok=ok,
            active_pre_apply_status=active_status,
            target_hash_checks=checks,
            reason=reason,
        )

    def _git_status_porcelain(self) -> str:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=self.project_root,
            text=True,
            capture_output=True,
            timeout=30,
        )
        if result.returncode != 0:
            return f"GIT_STATUS_UNAVAILABLE: {(result.stderr or result.stdout).strip()}"
        return result.stdout

    def _apply_back_summary(self, diff_result: Any) -> str:
        return (
            f"Changed files: {', '.join(diff_result.allowed_changed_files) if diff_result.allowed_changed_files else 'none'}\n"
            f"Policy violations: {len(diff_result.violations)}\n"
            f"Diff stat:\n{diff_result.diff_stat}"
        )

    def _status_value(
        self,
        exit_status: int,
        diff_result: Any,
        decision: ApplyBackDecision,
        apply_back_failure: str | None,
    ) -> str:
        if exit_status != 0:
            return "failed"
        if not diff_result.valid:
            return "policy_violation"
        if decision.decision == "denied":
            return "completed_denied"
        if decision.approved and apply_back_failure:
            return "apply_back_failed"
        if decision.approved:
            return "completed_applied"
        return "completed"

    def _write_report(
        self,
        run_id: str,
        goal: str,
        task_type: str,
        approval_id: str,
        workspace: Any,
        cleanup_status: str,
        result: CodexRunResult,
        network_status: str,
        diff_result: Any,
        apply_back_decision: ApplyBackDecision,
        freshness_result: FreshnessCheckResult,
        applied_files: list[str],
        apply_back_failure: str | None,
        artifact_paths: dict[str, Path],
    ) -> None:
        metadata = self.backend.config.metadata
        lines = [
            f"# Run {run_id}",
            "",
            f"- Goal: {sanitize_for_logging(goal)}",
            f"- Task type: {task_type}",
            f"- Backend name: {self.backend.name}",
            f"- Backend kind: {self.backend.config.kind.value}",
            f"- Billing mode: {metadata.billing_mode.value}",
            f"- Execution location: {metadata.execution_location.value}",
            f"- Data boundary: {metadata.data_boundary.value}",
            f"- Hosted-boundary approval id: {approval_id}",
            f"- Isolation strategy: {workspace.strategy}",
            f"- Isolated workspace: {workspace.path}",
            f"- Isolation cleanup status: {cleanup_status}",
            f"- AGENTS.md exists: {workspace.agents_md_exists}",
            f"- Active pre-isolation git status: {workspace.active_pre_isolation_git_status!r}",
            f"- Codex exit status: {result.exit_status}",
            f"- Codex network status: {network_status}",
            f"- Changed files: {diff_result.changed_files}",
            f"- Allowed changed files: {diff_result.allowed_changed_files}",
            f"- Policy violations: {[violation.to_dict() for violation in diff_result.violations]}",
            f"- Apply-back approval decision: {apply_back_decision.decision}",
            f"- Apply-back decision reason: {apply_back_decision.reason or ''}",
            f"- Freshness result: {freshness_result.ok}",
            f"- Freshness failure reason: {freshness_result.reason or ''}",
            f"- Target file hash checks: {freshness_result.target_hash_checks or []}",
            f"- Active pre-apply git status: {freshness_result.active_pre_apply_status!r}",
            f"- Active post-apply git status: {freshness_result.active_post_apply_status!r}",
            f"- Applied files: {applied_files}",
            f"- Apply-back failure: {apply_back_failure or ''}",
            f"- Apply-back outcome: {self._apply_back_outcome(diff_result, apply_back_decision, apply_back_failure)}",
            "",
            "## Diff Stat",
            "",
            "```",
            str(sanitize_for_logging(diff_result.diff_stat)),
            "```",
            "",
            "## Codex Final Message Advisory Only",
            "",
            str(sanitize_for_logging(result.final_message or "")),
            "",
            "## Artifacts",
            "",
        ]
        lines.extend(f"- {kind}: {path}" for kind, path in artifact_paths.items())
        artifact_paths["final_report"].write_text("\n".join(lines), encoding="utf-8")

    def _apply_back_outcome(
        self,
        diff_result: Any,
        decision: ApplyBackDecision,
        apply_back_failure: str | None,
    ) -> str:
        if not diff_result.valid:
            return "Codex completed but changes were blocked by policy."
        if decision.decision == "denied":
            return "Codex completed but changes were denied."
        if decision.approved and apply_back_failure:
            if "git status changed" in apply_back_failure or "Target file changed" in apply_back_failure:
                return "Codex completed but apply-back failed freshness checks."
            return "Codex completed but apply-back failed validation."
        if decision.approved:
            return "Codex completed and approved changes were applied."
        return "Codex completed with no apply-back."


def _redacted_command(command: list[str]) -> list[str]:
    return [str(sanitize_for_logging(part)) for part in command]


def _sha256_path(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
