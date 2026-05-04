from __future__ import annotations

from pathlib import Path
from typing import Any

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


class ActiveProjectModifiedError(RuntimeError):
    pass


class CodexCodeEditRunner:
    def __init__(
        self,
        project_root: Path,
        store: SQLiteStore,
        backend: CodexCliBackend,
        approval_store: ApprovalStore,
        isolation_manager: IsolationManager | None = None,
    ) -> None:
        self.project_root = project_root.resolve()
        self.store = store
        self.backend = backend
        self.approval_store = approval_store
        self.isolation_manager = isolation_manager or IsolationManager()

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
            invariant_ok = self._active_baseline_hashes_unchanged(workspace.baseline_manifest, active_hashes)
            if not invariant_ok:
                raise ActiveProjectModifiedError("Active project files changed during isolated Codex execution.")
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
            status_value = "completed" if result.exit_status == 0 and diff_result.valid else "policy_violation"
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
            "artifacts": {key: str(path) for key, path in artifact_paths.items()},
        }

    def _build_payload(self, goal: str, task_type: str) -> str:
        return (
            "You are Codex running under a supervised harness for isolated code editing.\n"
            "Edit only within the workspace supplied by --cd. Do not access secrets. Do not use network unless the CLI sandbox allows it.\n"
            "The harness will inspect the isolated diff after you exit. Apply-back is not implemented in C2.\n"
            f"Task type: {task_type}\n"
            f"Goal: {goal}\n"
        )

    def _active_baseline_hashes_unchanged(self, manifest: Any, expected_hashes: dict[str, str]) -> bool:
        for relative_path, expected_hash in expected_hashes.items():
            path = self.project_root / relative_path
            if not path.exists() or not path.is_file() or path.is_symlink():
                return False
            import hashlib

            if hashlib.sha256(path.read_bytes()).hexdigest() != expected_hash:
                return False
        return True

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
            "- Apply-back: Apply-back is not implemented in C2; active project was not modified.",
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


def _redacted_command(command: list[str]) -> list[str]:
    return [str(sanitize_for_logging(part)) for part in command]
