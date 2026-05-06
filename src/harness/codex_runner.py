from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from harness.approvals import ApprovalProfile, ApprovalStore
from harness.backends.codex_cli import (
    CodexCliBackend,
    CodexSandboxUnavailable,
    CodexUnavailable,
    write_codex_artifacts,
)
from harness.events import append_jsonl
from harness.memory.sqlite_store import SQLiteStore
from harness.security import scan_text_for_secrets, sanitize_for_logging


class HostedBoundaryApprovalRequired(RuntimeError):
    pass


class HostedSecretBlocked(RuntimeError):
    pass


class CodexRepoPlanningRunner:
    def __init__(
        self,
        project_root: Path,
        store: SQLiteStore,
        backend: CodexCliBackend,
        approval_store: ApprovalStore,
    ) -> None:
        self.project_root = project_root.resolve()
        self.store = store
        self.backend = backend
        self.approval_store = approval_store

    def run(
        self,
        goal: str,
        task_type: str,
        approval: ApprovalProfile | None,
        approve_secret_context: bool = False,
    ) -> dict[str, Any]:
        status = self.backend.preflight()
        if not status.available:
            raise CodexUnavailable(status.reason or "Codex CLI is unavailable.")
        if not status.capabilities.supports_read_only_sandbox:
            raise CodexSandboxUnavailable("Codex read-only sandbox is unavailable; refusing to run Codex.")
        if approval is None:
            raise HostedBoundaryApprovalRequired("Hosted data-boundary approval is required for codex_cli.")
        payload = self._build_payload(goal, task_type)
        findings = scan_text_for_secrets(payload)
        if findings and not approve_secret_context:
            raise HostedSecretBlocked(
                "Hosted Codex payload appears to contain secret-like content. Refusing to send it."
            )
        backend_config = self.backend.config.model_copy(update={"capabilities": status.capabilities})
        run = self.store.create_run(
            goal=goal,
            task_type=task_type,
            status="running",
            backend=backend_config,
            approval_id=approval.id,
        )
        paths = self.store.initialize_run_artifacts(run.id)
        for kind, path in paths.items():
            self.store.register_artifact(run.id, kind=kind, path=path)
        run_dir = self.store.runs_dir / run.id
        final_message_path = run_dir / "codex_final_message.md"
        pre_status = self._git_status_porcelain()
        self.store.append_event(
            run.id,
            "info",
            "hosted_boundary_approval_used",
            "Using hosted data-boundary approval for Codex.",
            {"approval_id": approval.id, "backend": self.backend.name, "task_type": task_type},
        )
        if findings:
            self.store.append_event(
                run.id,
                "warning",
                "hosted_payload_secret_findings",
                "Secret-like findings were approved for hosted Codex payload.",
                {"findings": [finding.to_dict() for finding in findings]},
            )
        self.store.append_event(
            run.id,
            "info",
            "pre_git_status",
            "Recorded pre-run git status.",
            {"status": pre_status},
        )
        append_jsonl(paths["transcript"], {"role": "user", "content": sanitize_for_logging(payload)})
        result = self.backend.run_read_only(self.project_root, payload, final_message_path)
        codex_artifacts = write_codex_artifacts(run_dir, result)
        for kind, path in codex_artifacts.items():
            self.store.register_artifact(run.id, kind=kind, path=path)
        if final_message_path.exists():
            final_message_path.write_text(
                str(sanitize_for_logging(final_message_path.read_text(encoding="utf-8", errors="replace"))),
                encoding="utf-8",
            )
            self.store.register_artifact(run.id, "codex_final_message", final_message_path)
        post_status = self._git_status_porcelain()
        policy_violation = pre_status != post_status
        self.store.append_event(
            run.id,
            "info",
            "codex_completed",
            "Codex process completed.",
            {
                "exit_status": result.exit_status,
                "command": _redacted_command(result.command),
                "json_event_count": len(result.json_events),
            },
        )
        self.store.append_event(
            run.id,
            "info",
            "post_git_status",
            "Recorded post-run git status.",
            {"status": post_status},
        )
        if policy_violation:
            self.store.append_event(
                run.id,
                "error",
                "policy_violation",
                "Read-only Codex run changed project git status.",
                {"pre_status": pre_status, "post_status": post_status},
            )
        status_value = "policy_violation" if policy_violation else ("completed" if result.exit_status == 0 else "failed")
        self.store.update_run_status(run.id, status_value)
        self._write_report(
            run_id=run.id,
            goal=goal,
            task_type=task_type,
            approval_id=approval.id,
            capabilities=status.capabilities.model_dump(mode="json"),
            pre_status=pre_status,
            post_status=post_status,
            policy_violation=policy_violation,
            result=result,
            artifact_paths={**paths, **codex_artifacts, "codex_final_message": final_message_path},
        )
        return {
            "run_id": run.id,
            "status": status_value,
            "approval_id": approval.id,
            "policy_violation": policy_violation,
            "artifacts": {key: str(path) for key, path in {**paths, **codex_artifacts}.items()},
        }

    def _build_payload(self, goal: str, task_type: str) -> str:
        return (
            "You are Codex running under a supervised harness for read-only planning.\n"
            "Do not edit files. Do not run write commands. Do not access secrets.\n"
            f"Task type: {task_type}\n"
            f"Project root: {self.project_root}\n"
            f"Goal: {goal}\n"
            "Return a concise implementation plan and risks. The harness will inspect git status after the run."
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

    def _write_report(
        self,
        run_id: str,
        goal: str,
        task_type: str,
        approval_id: str,
        capabilities: dict[str, Any],
        pre_status: str,
        post_status: str,
        policy_violation: bool,
        result: Any,
        artifact_paths: dict[str, Path],
    ) -> None:
        metadata = self.backend.config.metadata
        final = result.final_message or ""
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
            f"- Approval id: {approval_id}",
            f"- Capabilities: {capabilities}",
            f"- Exit status: {result.exit_status}",
            f"- Policy violation: {policy_violation}",
            "",
            "## Policy Status",
            "",
            (
                "POLICY VIOLATION: read-only Codex planning changed git status."
                if policy_violation
                else "No read-only policy violation detected."
            ),
            "",
            "## Git Status",
            "",
            "### Pre-run",
            "```",
            str(sanitize_for_logging(pre_status)),
            "```",
            "### Post-run",
            "```",
            str(sanitize_for_logging(post_status)),
            "```",
            "",
            "## Codex Final Message",
            "",
            str(sanitize_for_logging(final)),
            "",
            "## Artifacts",
            "",
        ]
        lines.extend(f"- {kind}: {path}" for kind, path in artifact_paths.items())
        (self.store.runs_dir / run_id / "final_report.md").write_text("\n".join(lines), encoding="utf-8")


def _redacted_command(command: list[str]) -> list[str]:
    return [str(sanitize_for_logging(part)) for part in command]
