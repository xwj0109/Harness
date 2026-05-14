from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Callable

from harness.backends.codex_cli import (
    CodexCliBackend,
    CodexRunResult,
    CodexUnavailable,
    write_codex_artifacts,
)
from harness.events import append_jsonl
from harness.memory.sqlite_store import SQLiteStore
from harness.models import BackendCapabilities
from harness.security import sanitize_for_logging


class DirtyWorkspaceError(RuntimeError):
    pass


ProgressCallback = Callable[[dict[str, Any]], None]


class CodexDirectAgentRunner:
    def __init__(
        self,
        project_root: Path,
        store: SQLiteStore,
        backend: CodexCliBackend,
    ) -> None:
        self.project_root = project_root.resolve()
        self.store = store
        self.backend = backend

    def run(
        self,
        goal: str,
        *,
        task_type: str = "codex_direct_agent",
        model: str | None = None,
        reasoning_effort: str | None = None,
        stream: bool = True,
        fail_on_dirty: bool = False,
        progress_callback: ProgressCallback | None = None,
    ) -> dict[str, Any]:
        pre_status = self._git_status_porcelain()
        if fail_on_dirty and pre_status.strip():
            raise DirtyWorkspaceError("Project has uncommitted changes; refusing to start because --fail-on-dirty was set.")
        status = self.backend.preflight()
        if not status.available:
            raise CodexUnavailable(status.reason or "Codex CLI is unavailable.")
        run = self.store.create_run(
            goal=goal,
            task_type=task_type,
            status="running",
            backend=self.backend.config.model_copy(update={"capabilities": status.capabilities}),
        )
        paths = self.store.initialize_run_artifacts(run.id)
        for kind, path in paths.items():
            self.store.register_artifact(run.id, kind=kind, path=path)
        run_dir = self.store.runs_dir / run.id
        final_message_path = run_dir / "codex_final_message.md"
        append_jsonl(paths["transcript"], {"role": "user", "content": sanitize_for_logging(goal)})
        self.store.append_event(
            run.id,
            "info",
            "pre_git_status",
            "Recorded pre-run git status.",
            {"status": pre_status},
        )
        result: CodexRunResult
        capabilities: BackendCapabilities
        network_status: str
        if stream:
            result = None  # type: ignore[assignment]
            capabilities = status.capabilities
            network_status = ""
            for event in self.backend.stream_direct_agent(
                self.project_root,
                goal,
                final_message_path,
                model=model,
                reasoning_effort=reasoning_effort,
            ):
                if event.get("type") == "completed":
                    result = event["result"]
                    capabilities = event.get("capabilities") or capabilities
                    network_status = str(event.get("network_status") or "")
                elif progress_callback is not None:
                    progress_callback(sanitize_for_logging(event))
            if result is None:
                raise CodexUnavailable("Codex direct agent stream ended without a completion event.")
        else:
            result, capabilities, network_status = self.backend.run_direct_agent(
                self.project_root,
                goal,
                final_message_path,
                model=model,
                reasoning_effort=reasoning_effort,
            )
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
        diff_stat = self._git_diff_stat()
        changed_files = self._changed_files(pre_status, post_status)
        self.store.append_event(
            run.id,
            "info",
            "codex_direct_agent_completed",
            "Codex direct foreground agent process completed.",
            {
                "exit_status": result.exit_status,
                "command": _redacted_command(result.command),
                "json_event_count": len(result.json_events),
                "capabilities": capabilities.model_dump(mode="json"),
                "network_status": network_status,
            },
        )
        self.store.append_event(
            run.id,
            "info",
            "post_git_status",
            "Recorded post-run git status.",
            {"status": post_status},
        )
        self.store.append_event(
            run.id,
            "info",
            "final_git_diff_stat",
            "Recorded final git diff stat.",
            {"diff_stat": diff_stat},
        )
        status_value = "completed" if result.exit_status == 0 else "failed"
        self.store.update_run_status(run.id, status_value)
        final_summary = result.final_message or _fallback_final_summary(result)
        artifact_paths = {**paths, **codex_artifacts, "codex_final_message": final_message_path}
        self._write_report(
            run_id=run.id,
            status_value=status_value,
            goal=goal,
            task_type=task_type,
            result=result,
            network_status=network_status,
            changed_files=changed_files,
            diff_stat=diff_stat,
            pre_status=pre_status,
            post_status=post_status,
            final_summary=final_summary,
            artifact_paths=artifact_paths,
        )
        return {
            "run_id": run.id,
            "status": status_value,
            "exit_status": result.exit_status,
            "final_summary": str(sanitize_for_logging(final_summary)),
            "changed_files": changed_files,
            "diff_stat": str(sanitize_for_logging(diff_stat)),
            "artifacts": {key: str(path) for key, path in artifact_paths.items()},
            "backend": self.backend.name,
            "model": model or self.backend.config.settings.get("model"),
            "sandbox_mode": self.backend.config.settings.get("last_codex_sandbox_mode", "workspace-write"),
            "approval_mode": self.backend.config.settings.get("last_codex_approval_mode", "unknown"),
        }

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

    def _git_diff_stat(self) -> str:
        result = subprocess.run(
            ["git", "diff", "--stat"],
            cwd=self.project_root,
            text=True,
            capture_output=True,
            timeout=30,
        )
        return result.stdout if result.returncode == 0 else ""

    def _changed_files(self, pre_status: str, post_status: str) -> list[str]:
        names: set[str] = set()
        pre_map = _parse_git_status_porcelain(pre_status)
        post_map = _parse_git_status_porcelain(post_status)
        for path, status in post_map.items():
            if pre_map.get(path) != status and _is_reportable_changed_path(path):
                names.add(path)
        diff = subprocess.run(
            ["git", "diff", "--name-only"],
            cwd=self.project_root,
            text=True,
            capture_output=True,
            timeout=30,
        )
        if diff.returncode == 0:
            names.update(line for line in diff.stdout.splitlines() if line.strip() and _is_reportable_changed_path(line))
        return sorted(names)

    def _write_report(
        self,
        run_id: str,
        status_value: str,
        goal: str,
        task_type: str,
        result: CodexRunResult,
        network_status: str,
        changed_files: list[str],
        diff_stat: str,
        pre_status: str,
        post_status: str,
        final_summary: str,
        artifact_paths: dict[str, Path],
    ) -> None:
        metadata = self.backend.config.metadata
        lines = [
            f"# Run {run_id}",
            "",
            f"- Goal: {sanitize_for_logging(goal)}",
            f"- Task type: {task_type}",
            f"- Status: {status_value}",
            f"- Backend name: {self.backend.name}",
            f"- Backend kind: {self.backend.config.kind.value}",
            f"- Billing mode: {metadata.billing_mode.value}",
            f"- Execution location: {metadata.execution_location.value}",
            f"- Data boundary: {metadata.data_boundary.value}",
            f"- Direct workspace edits: true",
            f"- Codex exit status: {result.exit_status}",
            f"- Codex network status: {network_status}",
            f"- Codex approval mode: {self.backend.config.settings.get('last_codex_approval_mode', 'unknown')}",
            f"- Codex command sandbox mode: {self.backend.config.settings.get('last_codex_sandbox_mode', 'workspace-write')}",
            "- Codex internal command approval enforceable: "
            f"{self.backend.config.settings.get('last_codex_internal_command_approval_enforceable', 'unknown')}",
            f"- Apply-back approval required: {self.backend.config.settings.get('last_apply_back_approval_required', False)}",
            f"- Changed files: {changed_files}",
            "",
            "## Final Git Diff Summary",
            "",
            "```",
            str(sanitize_for_logging(diff_stat)),
            "```",
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
            "## Final Codex Message",
            "",
            str(sanitize_for_logging(final_summary)),
            "",
            "## Artifacts",
            "",
        ]
        lines.extend(f"- {kind}: {path}" for kind, path in artifact_paths.items())
        artifact_paths["final_report"].write_text("\n".join(lines), encoding="utf-8")


def _fallback_final_summary(result: CodexRunResult) -> str:
    if result.exit_status == 0:
        return "Codex direct agent completed without a final message."
    stderr = str(sanitize_for_logging(result.stderr)).strip()
    if stderr:
        return f"Codex direct agent failed with exit status {result.exit_status}: {stderr[:1000]}"
    return f"Codex direct agent failed with exit status {result.exit_status}."


def _parse_git_status_porcelain(status: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    if status.startswith("GIT_STATUS_UNAVAILABLE:"):
        return parsed
    for line in status.splitlines():
        if not line.strip() or len(line) < 4:
            continue
        path = line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1].strip()
        if path:
            parsed[path] = line[:2]
    return parsed


def _is_reportable_changed_path(path: str) -> bool:
    blocked_prefixes = (
        ".harness/",
        ".git/",
        ".venv/",
        "node_modules/",
        "__pycache__/",
        ".pytest_cache/",
        ".mypy_cache/",
        "dist/",
        "build/",
    )
    return path not in {".harness", ".git"} and not path.startswith(blocked_prefixes)


def _redacted_command(command: list[str]) -> list[str]:
    if not command:
        return []
    redacted = list(command)
    redacted[-1] = "[PROMPT_REDACTED]"
    return redacted
