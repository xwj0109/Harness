from __future__ import annotations

from pathlib import Path
from typing import Any

from harness.backends.local_openai import LocalEndpointUnavailable, LocalOpenAICompatibleBackend
from harness.config import HarnessConfig
from harness.events import append_jsonl
from harness.memory.sqlite_store import SQLiteStore
from harness.protocol import CommandValidationError, ModelCommand, parse_model_command
from harness.security import sanitize_for_logging
from harness.tools.base import ToolContext, ToolResult
from harness.tools.readonly import GitDiffTool, GitStatusTool, ListFilesTool, ReadFileTool


class ReadOnlyRepoSummaryRunner:
    def __init__(
        self,
        project_root: Path,
        config: HarnessConfig,
        store: SQLiteStore,
        backend: LocalOpenAICompatibleBackend,
        max_steps: int = 12,
        max_invalid_retries: int = 2,
    ) -> None:
        self.project_root = project_root.resolve()
        self.config = config
        self.store = store
        self.backend = backend
        self.max_steps = max_steps
        self.max_invalid_retries = max_invalid_retries
        self.tools = {
            "list_files": ListFilesTool(),
            "read_file": ReadFileTool(),
            "git_status": GitStatusTool(),
            "git_diff": GitDiffTool(),
        }

    def run(self, goal: str, task_type: str) -> dict[str, Any]:
        backend_status = self.backend.preflight()
        if not backend_status.available:
            raise LocalEndpointUnavailable(backend_status.reason or "Local backend unavailable.")
        run = self.store.create_run(goal=goal, task_type=task_type, status="running", backend=self.backend.config)
        paths = self.store.initialize_run_artifacts(run.id)
        for kind, path in paths.items():
            self.store.register_artifact(run.id, kind=kind, path=path)
        self.store.append_event(
            run.id,
            "info",
            "run_started",
            "Started read-only repo summary run.",
            {"backend": self.backend.name, "task_type": task_type},
        )
        transcript_path = paths["transcript"]
        messages = self._initial_messages(goal, task_type)
        tool_results: list[dict[str, Any]] = []
        tools_executed: list[str] = []
        invalid_count = 0
        final_summary = ""
        stopped_for_invalid_output = False
        for step in range(self.max_steps):
            raw = self.backend.complete(messages)
            append_jsonl(
                transcript_path,
                sanitize_for_logging({"role": "assistant", "content": raw, "step": step}),
            )
            try:
                command = parse_model_command(raw)
            except CommandValidationError as exc:
                invalid_count += 1
                self.store.append_event(
                    run.id,
                    "warning",
                    "invalid_model_command",
                    "Rejected invalid model command.",
                    {"error": str(exc), "raw": raw, "invalid_count": invalid_count},
                )
                if invalid_count > self.max_invalid_retries:
                    final_summary = "Run stopped because the local model returned too many invalid commands."
                    stopped_for_invalid_output = True
                    break
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Your previous response was invalid. Return only a JSON object with "
                            "command and arguments. Do not include prose or markdown."
                        ),
                    }
                )
                continue
            if command.command == "final_answer":
                final_summary = str(sanitize_for_logging(command.final_text))
                self.store.append_event(
                    run.id,
                    "info",
                    "final_answer",
                    "Model returned final answer.",
                    {"summary": final_summary},
                )
                break
            result = self._execute_command(command)
            tools_executed.append(command.command)
            tool_payload = {
                "command": command.command,
                "arguments": command.arguments,
                "ok": result.ok,
                "output": result.output,
                "error_type": result.error_type,
                "data": result.data,
            }
            tool_results.append(tool_payload)
            self.store.append_event(run.id, "info", "tool_executed", "Executed read-only tool.", tool_payload)
            append_jsonl(transcript_path, sanitize_for_logging({"role": "tool", **tool_payload}))
            messages.append(
                {
                    "role": "user",
                    "content": self._tool_result_message(tool_payload, tool_results),
                }
            )
        if not final_summary:
            final_summary = "Run ended without a final model summary."
        self._write_phase_1b_report(
            run_id=run.id,
            goal=goal,
            task_type=task_type,
            tools_executed=tools_executed,
            invalid_count=invalid_count,
            final_summary=final_summary,
            artifact_paths=paths,
        )
        self.store.append_event(
            run.id,
            "info",
            "run_completed",
            "Completed read-only repo summary run.",
            {"invalid_model_command_count": invalid_count, "tools_executed": tools_executed},
        )
        self.store.update_run_status(run.id, "failed" if stopped_for_invalid_output else "completed")
        return {
            "run_id": run.id,
            "final_summary": final_summary,
            "invalid_model_command_count": invalid_count,
            "tools_executed": tools_executed,
            "artifacts": {key: str(path) for key, path in paths.items()},
        }

    def _initial_messages(self, goal: str, task_type: str) -> list[dict[str, str]]:
        allowed = ", ".join(["list_files", "read_file", "git_status", "git_diff", "final_answer"])
        return [
            {
                "role": "system",
                "content": (
                    "You are running inside a local-only read-only harness. Return exactly one JSON "
                    "object per response. Allowed commands are: "
                    f"{allowed}. Never request writes, shell execution, network access, secrets, "
                    "excluded directories, or paths outside the project. Use read_file only for "
                    "specific non-secret files discovered by list_files. Finish with final_answer."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Goal: {goal}\n"
                    f"Project root: {self.project_root}\n"
                    f"Task type: {task_type}\n"
                    f"Allowed tools: {allowed}\n"
                    "Excluded paths are not available in context. Start by inspecting file names or git status."
                ),
            },
        ]

    def _execute_command(self, command: ModelCommand) -> ToolResult:
        tool = self.tools[command.command]
        context = ToolContext(project_root=self.project_root, context_excludes=self.config.context_excludes)
        return tool.run(command.arguments, context)

    def _tool_result_message(self, payload: dict[str, Any], recent_results: list[dict[str, Any]]) -> str:
        recent = recent_results[-4:]
        return (
            "Tool result follows. Continue with another JSON command or final_answer.\n"
            f"Recent results: {recent}"
        )

    def _write_phase_1b_report(
        self,
        run_id: str,
        goal: str,
        task_type: str,
        tools_executed: list[str],
        invalid_count: int,
        final_summary: str,
        artifact_paths: dict[str, Path],
    ) -> None:
        report_path = artifact_paths["final_report"]
        metadata = self.backend.config.metadata
        capabilities = self.backend.config.capabilities
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
            f"- Allow network: {metadata.allow_network}",
            f"- Local endpoint: {self.backend.base_url}",
            f"- Capabilities: {capabilities.model_dump(mode='json')}",
            f"- Tools executed: {tools_executed}",
            f"- Invalid/blocked model commands: {invalid_count}",
            "",
            "## Final Model Summary",
            "",
            str(sanitize_for_logging(final_summary)),
            "",
            "## Artifacts",
            "",
            f"- events: {artifact_paths['events']}",
            f"- transcript: {artifact_paths['transcript']}",
            f"- final_report: {artifact_paths['final_report']}",
            "",
        ]
        report_path.write_text("\n".join(lines), encoding="utf-8")
