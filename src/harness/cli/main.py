from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sqlite3
import socket
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Annotated

import click
import typer
from typer.core import TyperGroup

from harness.agent_authoring import (
    AgentBundleError,
    load_agent_bundle,
    merge_agent_bundle_with_builtins,
    preview_agent_bundle,
    scaffold_agent_bundle,
    validate_agent_bundle,
)
from harness import __version__
from harness.approvals import ApprovalProfile, ApprovalStore
from harness.autonomy import builtin_autonomy_policies, get_builtin_autonomy_policy
from harness.backends.codex_cli import (
    AUTH_ERROR,
    CodexCliBackend,
    CodexDangerousFlagError,
    CodexEditCommandUnavailable,
    CodexSandboxUnavailable,
    CodexUnavailable,
)
from harness.backends.local_openai import LocalEndpointUnavailable, LocalOpenAICompatibleBackend
from harness.capabilities import build_capability_catalog, get_capability
from harness.chat import chat_context, run_autonomous_read_loop, run_chat_loop
from harness.config import HARNESS_DIR, default_config, load_config, write_default_config
from harness.codex_runner import (
    CodexReadOnlyRepoSummaryRunner,
    CodexRepoPlanningRunner,
    HostedBoundaryApprovalRequired,
    HostedSecretBlocked,
)
from harness.codex_direct_runner import CodexDirectAgentRunner, DirtyWorkspaceError
from harness.codex_edit_runner import ActiveProjectModifiedError, ApplyBackDecision, CodexCodeEditRunner
from harness.daemon_adapters import execute_read_only_summary_lease
from harness.execution import execute_lease, list_execution_adapter_descriptors
from harness.edit_runner import NativeEditRunner, PatchApprovalDecision
from harness.evals import run_safety_smoke, run_security_check, run_security_layer_audit
from harness.integrity import run_integrity_check
from harness.isolation import ActiveRepoDirtyError
from harness.memory.sqlite_store import SQLiteStore
from harness.models import KillSwitchTargetKind, MemoryScopeType, MemorySourceKind, TaskStatus
from harness.objective_runner import run_next_active_objective_autonomously, run_objective_autonomously
from harness.paths import resolve_project_root
from harness.policy import (
    backend_descriptor_sha256,
    effective_policy_sha256,
    resolve_agent_effective_policy,
    resolve_backend_effective_policy,
    resolve_task_effective_policy,
    resolve_workbench_effective_policy,
)
from harness.progress import build_orchestration_progress
from harness.registry import builtin_spec_registry
from harness.sandbox import CommandValidationError, DockerImageManager
from harness.sandbox_profiles import build_sandbox_profile_catalog, get_sandbox_profile
from harness.security_explanations import render_blocked_state
from harness.spec_loader import (
    SpecBundleError,
    diff_builtin_to_custom_spec_registry,
    effective_policy_preview,
    export_builtin_spec_registry,
    export_custom_spec_registry,
    load_spec_registry,
    resolve_spec_bundle_path,
    validate_spec_bundle,
)
from harness.test_runner import DockerTestRunner, RunTestsDecision
from harness.tool_capabilities import get_tool_capability, list_tool_capabilities
from harness.traces import export_run_trace, to_otel_json
from harness.tui_assets import TUI_HOME_IMAGE_SCHEMA_VERSION, TuiHomeImageError, set_tui_home_image

class HarnessRootGroup(TyperGroup):
    def resolve_command(self, ctx, args):
        try:
            return super().resolve_command(ctx, args)
        except click.UsageError:
            if args:
                command = self.get_command(ctx, "__prompt")
                if command is not None:
                    return "__prompt", command, args
            raise


app = typer.Typer(help="Local-first agent harness.", invoke_without_command=True, cls=HarnessRootGroup)
dev_app = typer.Typer(help="Phase 1A development diagnostics.")
backends_app = typer.Typer(help="Configured backend metadata and preflight checks.", invoke_without_command=True)
approvals_app = typer.Typer(help="Hosted data-boundary approval profiles.", invoke_without_command=True)
tests_app = typer.Typer(help="Docker-sandboxed test execution.")
tests_image_app = typer.Typer(help="Managed Docker test image helpers.")
specs_app = typer.Typer(help="Read-only built-in v0.2 spec inspection.", invoke_without_command=True)
specs_preview_app = typer.Typer(help="Read-only effective v0.2 spec policy previews.")
policy_app = typer.Typer(help="Runtime effective policy evidence.")
artifacts_app = typer.Typer(help="Run artifact evidence inspection.")
tools_app = typer.Typer(help="Harness tool capability descriptors.")
capabilities_app = typer.Typer(help="Read-only Harness capability catalog.")
sandbox_app = typer.Typer(help="Read-only sandbox profile descriptors.")
controls_app = typer.Typer(help="Local runtime execution controls.")
memory_app = typer.Typer(help="Explicit local memory records.")
baseline_app = typer.Typer(help="Local run evidence baselines.")
evals_app = typer.Typer(help="Local evidence-only eval suites.")
security_app = typer.Typer(help="Local metadata-only security detections.")
integrity_app = typer.Typer(help="Local package and evidence integrity checks.")
traces_app = typer.Typer(help="Local run trace export.")
autonomy_app = typer.Typer(help="Autonomy policy profiles and decisions.")
autonomy_policy_app = typer.Typer(help="Autonomy policy profile inspection.")
daemon_app = typer.Typer(help="Local daemon control-plane scheduler.")
objectives_app = typer.Typer(help="Manual persistent objective records.")
tasks_app = typer.Typer(help="Manual persistent task queue.")
agents_app = typer.Typer(help="Declarative custom agent authoring.")
quickstart_app = typer.Typer(help="Guided command composition without hidden execution.")
tui_home_app = typer.Typer(help="TUI homepage visual customization.")
app.add_typer(dev_app, name="dev")
app.add_typer(backends_app, name="backends")
app.add_typer(approvals_app, name="approvals")
app.add_typer(tests_app, name="tests")
app.add_typer(specs_app, name="specs")
app.add_typer(policy_app, name="policy")
app.add_typer(artifacts_app, name="artifacts")
app.add_typer(tools_app, name="tools")
app.add_typer(capabilities_app, name="capabilities")
app.add_typer(sandbox_app, name="sandbox")
app.add_typer(controls_app, name="controls")
app.add_typer(memory_app, name="memory")
app.add_typer(baseline_app, name="baseline")
app.add_typer(evals_app, name="evals")
app.add_typer(security_app, name="security")
app.add_typer(integrity_app, name="integrity")
app.add_typer(traces_app, name="traces")
app.add_typer(autonomy_app, name="autonomy")
app.add_typer(daemon_app, name="daemon")
app.add_typer(objectives_app, name="objectives")
app.add_typer(tasks_app, name="tasks")
app.add_typer(agents_app, name="agents")
app.add_typer(quickstart_app, name="quickstart")
app.add_typer(tui_home_app, name="tui-home")
tests_app.add_typer(tests_image_app, name="image")
specs_app.add_typer(specs_preview_app, name="preview")
autonomy_app.add_typer(autonomy_policy_app, name="policy")

ProjectOption = Annotated[Path, typer.Option("--project", help="Project root path.")]
TaskStatusArg = Annotated[TaskStatus, typer.Argument(help="Task status.")]


class OutputFormat(str, Enum):
    TEXT = "text"
    JSON = "json"


OutputOption = Annotated[OutputFormat, typer.Option("--output", help="Output format.")]
SpecSourceOption = Annotated[str, typer.Option("--source", help="Spec source: builtin or explicit bundle path.")]
PolicySubjectKindOption = Annotated[
    str,
    typer.Option("--subject-kind", help="Policy subject kind: run, task, agent, workbench, or backend."),
]
PolicySubjectIdOption = Annotated[str, typer.Option("--subject-id", help="Policy subject id.")]
TraceFormatOption = Annotated[str, typer.Option("--format", help="Trace format. Only otel-json is supported.")]

TUI_SCHEMA_VERSION = "harness.tui/v1"
TUI_INSTALL_HINT = "Install Harness with its default dependencies, including Textual."

GITIGNORE_SECTION = """# Harness local artifacts
.harness/runs/
.harness/harness.sqlite
.harness/approvals.yaml
.harness/tmp/
*.egg-info/
"""


@app.callback()
def main(
    ctx: typer.Context,
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
    plain: Annotated[bool, typer.Option("--plain", help="Run the line-oriented chat fallback.")] = False,
    codex_like: Annotated[
        bool,
        typer.Option("--codex-like", help="Start the app in foreground Codex-like action mode."),
    ] = False,
    autonomous: Annotated[
        bool,
        typer.Option("--autonomous", help="Use the safe-local autonomy profile for eligible action contracts."),
    ] = False,
    autonomy: Annotated[str, typer.Option("--autonomy", help="Autonomy profile id for chat action contracts.")] = "manual",
) -> None:
    """Launch the unified Harness app when no subcommand is provided."""

    if ctx.invoked_subcommand is not None:
        return
    project_root = resolve_project_root(project)
    if output == OutputFormat.JSON:
        _emit_json(chat_context(project_root))
        raise typer.Exit()
    autonomy_profile = _resolve_autonomy_profile_option(autonomous, autonomy)
    if plain:
        raise typer.Exit(
            code=run_chat_loop(
                project_root,
                sys.stdin,
                sys.stdout,
                codex_like=codex_like,
                autonomy_profile_id=autonomy_profile,
            )
        )
    if codex_like:
        _run_unified_app(project_root, codex_like=True)
    else:
        _run_unified_app(project_root)
    raise typer.Exit()


@app.command("__prompt", hidden=True)
def foreground_prompt(
    prompt: Annotated[str, typer.Argument(help="Foreground Codex-style coding prompt.")],
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
    model: Annotated[str | None, typer.Option("--model", help="Codex model override for foreground prompt runs.")] = None,
    reasoning_effort: Annotated[
        str | None,
        typer.Option("--reasoning-effort", help="Codex reasoning effort override for foreground prompt runs."),
    ] = None,
    no_stream: Annotated[bool, typer.Option("--no-stream", help="Disable live Codex event summaries.")] = False,
    fail_on_dirty: Annotated[
        bool,
        typer.Option("--fail-on-dirty", help="Refuse foreground prompt runs when git status is dirty."),
    ] = False,
) -> None:
    project_root = resolve_project_root(project)
    result = _run_codex_direct_agent_cli(
        prompt,
        project_root,
        output=output,
        model=model,
        reasoning_effort=reasoning_effort,
        stream=not no_stream,
        fail_on_dirty=fail_on_dirty,
    )
    raise typer.Exit(code=0 if result.get("status") == "completed" else 1)


@app.command("tui", hidden=True)
def tui(project: ProjectOption = Path("."), output: OutputOption = OutputFormat.TEXT) -> None:
    project_root = resolve_project_root(project)
    if output == OutputFormat.JSON:
        payload = chat_context(project_root)
        payload["schema_version"] = TUI_SCHEMA_VERSION
        payload["mode"] = "unified_app"
        payload["launched"] = False
        _emit_json(payload)
        return
    _run_unified_app(project_root)


@app.command("chat", hidden=True)
def chat(
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
    plain: Annotated[bool, typer.Option("--plain", help="Run the line-oriented chat fallback.")] = False,
    codex_like: Annotated[
        bool,
        typer.Option("--codex-like", help="Start the alias in foreground Codex-like action mode."),
    ] = False,
    autonomous: Annotated[
        bool,
        typer.Option("--autonomous", help="Use the safe-local autonomy profile for eligible action contracts."),
    ] = False,
    autonomy: Annotated[str, typer.Option("--autonomy", help="Autonomy profile id for chat action contracts.")] = "manual",
) -> None:
    """Compatibility alias for the unified Harness app."""

    project_root = resolve_project_root(project)
    if output == OutputFormat.JSON:
        _emit_json(chat_context(project_root))
        return
    autonomy_profile = _resolve_autonomy_profile_option(autonomous, autonomy)
    if not plain:
        if codex_like:
            _run_unified_app(project_root, codex_like=True)
        else:
            _run_unified_app(project_root)
        return
    try:
        raise typer.Exit(
            code=run_chat_loop(
                project_root,
                sys.stdin,
                sys.stdout,
                codex_like=codex_like,
                autonomy_profile_id=autonomy_profile,
            )
        )
    except ValueError as exc:
        typer.echo(f"Chat command failed: {exc}")
        raise typer.Exit(code=1) from exc


@tui_home_app.command("set-image")
def tui_home_set_image(
    image_path: Path,
    width: Annotated[int, typer.Option("--width", help="Generated terminal art width in cells.")] = 80,
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    """Import a local image as the static TUI homepage pixel art."""

    try:
        result = set_tui_home_image(image_path, width=width)
    except TuiHomeImageError as exc:
        _emit_tui_home_error(str(exc), output)
        raise typer.Exit(code=1) from exc
    if output == OutputFormat.JSON:
        _emit_json(result)
        return
    typer.echo("TUI home image updated.")
    typer.echo(f"Source image: {result['source_image']}")
    typer.echo(f"Stored source: {result['stored_source']}")
    typer.echo(f"Generated module: {result['generated_module']}")
    typer.echo(f"Terminal size: {result['width']}x{result['terminal_rows']}")


@app.command()
def init(project: ProjectOption = Path(".")) -> None:
    project_root = resolve_project_root(project)
    project_root.mkdir(parents=True, exist_ok=True)
    harness_dir = project_root / HARNESS_DIR
    harness_dir.mkdir(parents=True, exist_ok=True)
    config_path = write_default_config(project_root)
    store = SQLiteStore(project_root)
    store.initialize()
    _update_gitignore(project_root)
    typer.echo(f"Initialized harness at {harness_dir}")
    typer.echo(f"Config: {config_path}")
    typer.echo("Updated .gitignore with Harness local artifacts section if needed.")


@app.command()
def runs(project: ProjectOption = Path("."), output: OutputOption = OutputFormat.TEXT) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    store = SQLiteStore(project_root)
    records = store.list_runs()
    if output == OutputFormat.JSON:
        _emit_json(
            {
                "schema_version": "harness.runs/v1",
                "runs": [record.model_dump(mode="json") for record in records],
            }
        )
        return
    if not records:
        typer.echo("No runs found.")
        return
    _print_tsv(["run_id", "status", "created_at", "task_type", "goal", "backend"])
    for record in records:
        _print_tsv_row(
            [
                record.id,
                record.status,
                record.created_at.isoformat(),
                record.task_type or "",
                record.goal or "",
                record.backend_name or "none",
            ]
        )


@app.command()
def home(project: ProjectOption = Path("."), output: OutputOption = OutputFormat.TEXT) -> None:
    """Show a read-only operator dashboard.

    Examples:
      harness home --project .
      harness home --project . --output json
    """
    project_root = resolve_project_root(project)
    result = _home_result(project_root)
    if output == OutputFormat.JSON:
        _emit_json(result)
        return
    typer.echo("Harness Home")
    _print_section("Project")
    _print_kv("Root", result["project_root"])
    _print_kv("Initialized", result["initialized"])
    _print_kv("Version", result["version"])
    if not result["initialized"]:
        _print_section("Next Actions")
        if result.get("state_error"):
            _print_kv("State error", f"{result['state_error']['type']}: {result['state_error']['message']}")
        for action in result["recommended_actions"]:
            _print_kv(action["description"], action["command"])
        _print_section("Safety")
        typer.echo("  Local-first control plane; no hidden execution.")
        return
    summary = result["summary"]
    _print_section("Summary")
    _print_kv("Imported agents", summary["imported_agents"])
    _print_kv("Objectives", summary["objectives"])
    _print_kv("Tasks", summary["tasks_total"])
    _print_kv("Active leases", summary["active_leases"])
    _print_kv("Active daemons", summary["active_daemons"])
    _print_kv("Recent runs", summary["recent_runs"])
    task_counts = result["task_status_counts"]
    _print_section("Task States")
    _print_tsv(["state", "count"])
    for state in ("ready", "blocked", "waiting_approval", "leased", "running"):
        _print_tsv_row([state, task_counts.get(state, 0)])
    if result["daemon"]["paused_tasks"]:
        _print_section("Daemon")
        _print_kv("Paused tasks", len(result["daemon"]["paused_tasks"]))
    if result["recent_runs"]:
        _print_section("Recent Runs")
        _print_tsv(["run_id", "status", "task_type"])
        for run in result["recent_runs"]:
            _print_tsv_row([run["id"], run["status"], run.get("task_type") or ""])
    if result["recommended_actions"]:
        _print_section("Next Actions")
        for action in result["recommended_actions"]:
            _print_kv(action["description"], action["command"])
    _print_section("Safety")
    typer.echo("  Local-first control plane; no hosted fallback, paid fallback, or OpenAI API usage.")


@quickstart_app.command("agent")
def quickstart_agent(project: ProjectOption = Path("."), output: OutputOption = OutputFormat.TEXT) -> None:
    """Print the explicit MVP agent command sequence without running it.

    Examples:
      harness quickstart agent --project .
      harness quickstart agent --project . --output json
    """
    project_root = resolve_project_root(project)
    result = _quickstart_agent_result(project_root)
    if output == OutputFormat.JSON:
        _emit_json(result)
        return
    typer.echo("Agent Quickstart")
    _print_section("Project")
    _print_kv("Root", result["project_root"])
    _print_kv("Initialized", result["initialized"])
    _print_section("Steps")
    for index, step in enumerate(result["steps"], start=1):
        typer.echo(f"{index}. {step['title']}")
        typer.echo(f"   {step['command']}")
    _print_section("Safety")
    typer.echo("  This command only prints commands; it does not run them.")


@app.command()
def show(run_id: str, project: ProjectOption = Path("."), output: OutputOption = OutputFormat.TEXT) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    store = SQLiteStore(project_root)
    try:
        record = store.get_run(run_id)
    except KeyError as exc:
        raise typer.BadParameter(str(exc)) from exc
    run_dir = store.runs_dir / run_id
    if output == OutputFormat.JSON:
        manifest_path = run_dir / "manifest.json"
        if manifest_path.exists():
            _emit_json(json.loads(manifest_path.read_text(encoding="utf-8")))
        else:
            _emit_json(store.build_run_manifest(run_id).model_dump(mode="json"))
        return
    typer.echo(f"Run: {record.id}")
    typer.echo(f"Status: {record.status}")
    typer.echo(f"Goal: {record.goal or ''}")
    typer.echo(f"Task type: {record.task_type or ''}")
    typer.echo(f"Project root: {record.project_root}")
    typer.echo(f"Created: {record.created_at.isoformat()}")
    typer.echo(f"Updated: {record.updated_at.isoformat()}")
    typer.echo(f"Backend: {record.backend_name or 'none'}")
    typer.echo(f"Backend kind: {record.backend_kind.value if record.backend_kind else 'none'}")
    typer.echo(f"Billing mode: {record.billing_mode.value if record.billing_mode else 'none'}")
    typer.echo(f"Execution location: {record.execution_location.value if record.execution_location else 'none'}")
    typer.echo(f"Data boundary: {record.data_boundary.value if record.data_boundary else 'none'}")
    typer.echo("Artifacts:")
    typer.echo(f"  events: {run_dir / 'events.jsonl'}")
    typer.echo(f"  transcript: {run_dir / 'transcript.jsonl'}")
    typer.echo(f"  final_report: {run_dir / 'final_report.md'}")


@app.command()
def compare(
    run_a: str,
    run_b: str,
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    store = SQLiteStore(project_root)
    try:
        result = store.compare_runs(run_a, run_b)
    except KeyError as exc:
        _emit_compare_error("harness.compare/v1", str(exc).strip("'"), output)
        raise typer.Exit(code=1) from exc
    if output == OutputFormat.JSON:
        _emit_json(result.model_dump(mode="json"))
        return
    _print_compare_result(result.model_dump(mode="json"))


@app.command()
def doctor(
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
    release: Annotated[
        bool,
        typer.Option("--release", help="Run release-readiness checks without backend/provider preflight."),
    ] = False,
) -> None:
    project_root = resolve_project_root(project)
    result = _doctor_result(project_root, release=release)
    if output == OutputFormat.JSON:
        _emit_json(result)
    else:
        typer.echo(f"Project: {result['project_root']}")
        typer.echo(f"Mode: {result['mode']}")
        typer.echo(f"Overall: {'pass' if result['ok'] else 'fail'}")
        for check in result["checks"]:
            typer.echo(f"{check['status']}\t{check['id']}\t{check['message']}")
    if not result["ok"]:
        raise typer.Exit(code=1)


@app.command("act")
def act(
    request: Annotated[str, typer.Argument(help="Autonomous request.")],
    project: ProjectOption = Path("."),
    autonomy: Annotated[str, typer.Option("--autonomy", help="Autonomy profile id.")] = "safe-local",
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    """Run a bounded autonomous chat loop."""

    project_root = resolve_project_root(project)
    try:
        get_builtin_autonomy_policy(autonomy)
    except KeyError as exc:
        raise typer.BadParameter(str(exc).strip("'"), param_hint="--autonomy") from exc
    result = run_autonomous_read_loop(
        request,
        project_root,
        autonomy_profile_id=autonomy,
        allow_action_contracts=True,
        auto_run_created_objective=True,
    )
    if output == OutputFormat.JSON:
        _emit_json(result)
        return
    typer.echo("Autonomous Act Loop")
    _print_kv("Profile", result["autonomy_profile_id"])
    _print_kv("Stop reason", result["stop_reason"])
    _print_kv("Model turns", result["model_turns"])
    _print_kv("Tool calls", result["tool_calls"])
    _print_kv("Evidence", result["evidence_path"])
    _print_section("Answer")
    for line in result["lines"]:
        typer.echo(line)
    if not result["ok"]:
        raise typer.Exit(code=1)


@objectives_app.command("add")
def objectives_add(
    title: Annotated[str, typer.Option("--title", help="Objective title.")],
    description: Annotated[str, typer.Option("--description", help="Objective description.")] = "",
    workbench: Annotated[str | None, typer.Option("--workbench", help="Built-in workbench id.")] = None,
    priority: Annotated[int, typer.Option("--priority", help="Higher priority objectives list first.")] = 0,
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    try:
        _validate_objective_refs(workbench)
        objective = SQLiteStore(project_root).create_objective(
            title=title,
            description=description,
            priority=priority,
            workbench_id=workbench,
            metadata={},
        )
    except (KeyError, ValueError) as exc:
        _emit_objective_error("harness.objective/v1", str(exc).strip("'"), output)
        raise typer.Exit(code=1) from exc
    if output == OutputFormat.JSON:
        _emit_json(
            {
                "schema_version": "harness.objective/v1",
                "ok": True,
                "objective": objective.model_dump(mode="json"),
            }
        )
        return
    typer.echo(f"Created objective {objective.id}")


@objectives_app.command("list")
def objectives_list(project: ProjectOption = Path("."), output: OutputOption = OutputFormat.TEXT) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    objectives = SQLiteStore(project_root).list_objectives()
    if output == OutputFormat.JSON:
        _emit_json(
            {
                "schema_version": "harness.objectives/v1",
                "ok": True,
                "objectives": [objective.model_dump(mode="json") for objective in objectives],
            }
        )
        return
    if not objectives:
        typer.echo("No objectives found.")
        return
    for objective in objectives:
        typer.echo(f"{objective.id}\t{objective.status.value}\t{objective.priority}\t{objective.title}")


@objectives_app.command("inspect")
def objectives_inspect(
    objective_id: str,
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    try:
        objective = SQLiteStore(project_root).get_objective(objective_id)
    except KeyError as exc:
        _emit_objective_error("harness.objective/v1", str(exc).strip("'"), output)
        raise typer.Exit(code=1) from exc
    if output == OutputFormat.JSON:
        _emit_json(
            {
                "schema_version": "harness.objective/v1",
                "ok": True,
                "objective": objective.model_dump(mode="json"),
            }
        )
        return
    _print_objective(objective)


@objectives_app.command("run")
def objectives_run(
    objective_id: str,
    project: ProjectOption = Path("."),
    autonomy: Annotated[str, typer.Option("--autonomy", help="Autonomy profile id.")] = "safe-local",
    max_steps: Annotated[int | None, typer.Option("--max-steps", help="Maximum adapter dispatches for this run.")] = None,
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    try:
        result = run_objective_autonomously(
            project_root,
            objective_id,
            autonomy_profile_id=autonomy,
            max_steps=max_steps,
        )
    except (KeyError, ValueError) as exc:
        _emit_objective_error("harness.autonomous_objective_run/v1", str(exc).strip("'"), output)
        raise typer.Exit(code=1) from exc
    if output == OutputFormat.JSON:
        _emit_json(result.model_dump(mode="json"))
        return
    typer.echo("Autonomous Objective Run")
    _print_kv("Objective", result.objective_id)
    _print_kv("Profile", result.autonomy_profile_id)
    _print_kv("Stop reason", result.stop_reason)
    _print_kv("Adapter dispatches", result.adapter_dispatches)
    _print_kv("Evidence", str(result.evidence_path))
    for step in result.step_results:
        typer.echo(
            f"Step {step.step}: task={step.task_id or 'none'} "
            f"adapter={step.adapter_id or 'none'} decision={step.decision_status or step.execution_decision or 'none'}"
        )


@tasks_app.command("add")
def tasks_add(
    title: Annotated[str, typer.Option("--title", help="Task title.")],
    description: Annotated[str, typer.Option("--description", help="Task description.")] = "",
    objective: Annotated[str | None, typer.Option("--objective", help="Objective id to attach.")] = None,
    depends_on: Annotated[
        list[str] | None,
        typer.Option("--depends-on", help="Task id this task depends on."),
    ] = None,
    requires_approval: Annotated[
        list[str] | None,
        typer.Option("--requires-approval", help="Approval key required before task selection."),
    ] = None,
    workbench: Annotated[str | None, typer.Option("--workbench", help="Built-in workbench id.")] = None,
    agent: Annotated[str | None, typer.Option("--agent", help="Built-in agent id.")] = None,
    execution_adapter: Annotated[
        str | None,
        typer.Option("--execution-adapter", help="Execution adapter metadata."),
    ] = None,
    task_type: Annotated[
        str | None,
        typer.Option("--task-type", help="Execution task type metadata."),
    ] = None,
    priority: Annotated[int, typer.Option("--priority", help="Higher priority tasks run first.")] = 0,
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    try:
        spec_source_kind, spec_source_path = _validate_task_spec_refs(project_root, workbench, agent)
        metadata = _execution_task_metadata(execution_adapter, task_type)
        task = SQLiteStore(project_root).create_task(
            title=title,
            description=description,
            priority=priority,
            objective_id=objective,
            workbench_id=workbench,
            agent_id=agent,
            spec_source_kind=spec_source_kind,
            spec_source_path=spec_source_path,
            depends_on=depends_on,
            required_approvals=requires_approval,
            metadata=metadata,
        )
    except (KeyError, ValueError) as exc:
        _emit_task_error("harness.task/v1", str(exc).strip("'"), output)
        raise typer.Exit(code=1) from exc
    if output == OutputFormat.JSON:
        _emit_json({"schema_version": "harness.task/v1", "ok": True, "task": task.model_dump(mode="json")})
        return
    typer.echo(f"Created task {task.id}")


@tasks_app.command("list")
def tasks_list(
    status: Annotated[TaskStatus | None, typer.Option("--status", help="Filter by task status.")] = None,
    objective: Annotated[str | None, typer.Option("--objective", help="Filter by objective id.")] = None,
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    try:
        tasks = SQLiteStore(project_root).list_tasks(
            status.value if status is not None else None,
            objective_id=objective,
        )
    except KeyError as exc:
        _emit_task_error("harness.tasks/v1", str(exc).strip("'"), output)
        raise typer.Exit(code=1) from exc
    if output == OutputFormat.JSON:
        _emit_json(
            {
                "schema_version": "harness.tasks/v1",
                "ok": True,
                "tasks": [task.model_dump(mode="json") for task in tasks],
            }
        )
        return
    if not tasks:
        typer.echo("No tasks found.")
        return
    _print_tsv(["task_id", "status", "priority", "title"])
    for task in tasks:
        _print_tsv_row([task.id, task.status.value, task.priority, task.title])


@tasks_app.command("graph")
def tasks_graph(
    objective: Annotated[str | None, typer.Option("--objective", help="Filter by objective id.")] = None,
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.JSON,
) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    try:
        graph = SQLiteStore(project_root).build_task_graph(objective_id=objective)
    except KeyError as exc:
        _emit_task_error("harness.task_graph/v1", str(exc).strip("'"), output)
        raise typer.Exit(code=1) from exc
    payload = {"schema_version": "harness.task_graph/v1", "ok": True, **graph}
    if output == OutputFormat.JSON:
        _emit_json(payload)
        return
    typer.echo(
        f"Task graph: {len(graph['objectives'])} objectives, "
        f"{len(graph['tasks'])} tasks, {len(graph['dependencies'])} dependencies"
    )


@tasks_app.command("inspect")
def tasks_inspect(task_id: str, project: ProjectOption = Path("."), output: OutputOption = OutputFormat.TEXT) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    try:
        task = SQLiteStore(project_root).get_task(task_id)
    except KeyError as exc:
        _emit_task_error("harness.task/v1", str(exc).strip("'"), output)
        raise typer.Exit(code=1) from exc
    if output == OutputFormat.JSON:
        _emit_json({"schema_version": "harness.task/v1", "ok": True, "task": task.model_dump(mode="json")})
        return
    _print_task(task)


@tasks_app.command("status")
def tasks_status(
    task_id: str,
    status: TaskStatusArg,
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    try:
        task = SQLiteStore(project_root).update_task_status(task_id, status)
    except (KeyError, ValueError) as exc:
        _emit_task_error("harness.task/v1", str(exc).strip("'"), output)
        raise typer.Exit(code=1) from exc
    if output == OutputFormat.JSON:
        _emit_json({"schema_version": "harness.task/v1", "ok": True, "task": task.model_dump(mode="json")})
        return
    typer.echo(f"Task {task.id}: {task.status.value}")


@tasks_app.command("cancel")
def tasks_cancel(task_id: str, project: ProjectOption = Path("."), output: OutputOption = OutputFormat.TEXT) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    try:
        task = SQLiteStore(project_root).cancel_task(task_id)
    except (KeyError, ValueError) as exc:
        _emit_task_error("harness.task/v1", str(exc).strip("'"), output)
        raise typer.Exit(code=1) from exc
    if output == OutputFormat.JSON:
        _emit_json({"schema_version": "harness.task/v1", "ok": True, "task": task.model_dump(mode="json")})
        return
    typer.echo(f"Task {task.id}: {task.status.value}")


@tasks_app.command("retry")
def tasks_retry(task_id: str, project: ProjectOption = Path("."), output: OutputOption = OutputFormat.TEXT) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    try:
        task = SQLiteStore(project_root).retry_task(task_id)
    except (KeyError, ValueError) as exc:
        _emit_task_error("harness.task/v1", str(exc).strip("'"), output)
        raise typer.Exit(code=1) from exc
    if output == OutputFormat.JSON:
        _emit_json({"schema_version": "harness.task/v1", "ok": True, "task": task.model_dump(mode="json")})
        return
    typer.echo(f"Task {task.id}: {task.status.value}")


@tasks_app.command("run-next")
def tasks_run_next(project: ProjectOption = Path("."), output: OutputOption = OutputFormat.TEXT) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    selection = SQLiteStore(project_root).select_next_task_for_lease()
    task = selection["task"] if selection is not None else None
    attempt = selection["attempt"] if selection is not None else None
    lease = selection["lease"] if selection is not None else None
    if output == OutputFormat.JSON:
        _emit_json(
            {
                "schema_version": "harness.task_run_next/v1",
                "ok": True,
                "selected_task": task.model_dump(mode="json") if task is not None else None,
                "attempt": attempt.model_dump(mode="json") if attempt is not None else None,
                "lease": lease.model_dump(mode="json") if lease is not None else None,
            }
        )
        return
    if task is None:
        typer.echo("No runnable ready task.")
    else:
        typer.echo(f"Leased task {task.id}")


@specs_app.callback()
def specs_callback(ctx: typer.Context, output: OutputOption = OutputFormat.TEXT) -> None:
    if ctx.invoked_subcommand is not None:
        return
    registry = builtin_spec_registry()
    if output == OutputFormat.JSON:
        _emit_json(
            {
                "schema_version": "harness.spec_registry/v1",
                "model_profiles": _dump_spec_mapping(registry.model_profiles),
                "tool_policies": _dump_spec_mapping(registry.tool_policies),
                "memory_scopes": _dump_spec_mapping(registry.memory_scopes),
                "agents": _dump_spec_mapping(registry.agents),
                "agent_profiles": _dump_spec_mapping(registry.agent_profiles),
                "workbenches": _dump_spec_mapping(registry.workbenches),
            }
        )
        return
    _print_spec_registry(registry)


@specs_app.command("agent")
def specs_agent(agent_id: str, output: OutputOption = OutputFormat.TEXT) -> None:
    registry = builtin_spec_registry()
    try:
        agent = registry.get_agent(agent_id)
    except KeyError as exc:
        raise typer.BadParameter(str(exc).strip("'")) from exc
    if output == OutputFormat.JSON:
        _emit_json(
            {
                "schema_version": "harness.agent_spec/v1",
                "agent": agent.model_dump(mode="json"),
            }
        )
        return
    _print_agent_spec(agent)


@specs_app.command("workbench")
def specs_workbench(workbench_id: str, output: OutputOption = OutputFormat.TEXT) -> None:
    registry = builtin_spec_registry()
    try:
        workbench = registry.get_workbench(workbench_id)
    except KeyError as exc:
        raise typer.BadParameter(str(exc).strip("'")) from exc
    if output == OutputFormat.JSON:
        _emit_json(
            {
                "schema_version": "harness.workbench_spec/v1",
                "workbench": workbench.model_dump(mode="json"),
            }
        )
        return
    _print_workbench_spec(workbench)


@specs_app.command("validate")
def specs_validate(path: Path, output: OutputOption = OutputFormat.TEXT) -> None:
    result = validate_spec_bundle(path)
    if output == OutputFormat.JSON:
        _emit_json(result)
    else:
        if result["ok"]:
            typer.echo(f"Spec bundle valid: {result['path']}")
        else:
            typer.echo(f"Spec bundle invalid: {result['path']}")
            for error in result["errors"]:
                typer.echo(f"  - {error}")
    if not result["ok"]:
        raise typer.Exit(code=1)


@specs_app.command("export")
def specs_export(
    source: Annotated[str, typer.Option("--source", help="Spec source: builtin or explicit bundle path.")],
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    try:
        if source == "builtin":
            result = export_builtin_spec_registry(builtin_spec_registry())
        else:
            result = export_custom_spec_registry(Path(source))
    except SpecBundleError as exc:
        if output == OutputFormat.JSON:
            _emit_json(
                {
                    "schema_version": "harness.spec_export/v1",
                    "ok": False,
                    "source": {"kind": "custom", "path": str(Path(source).expanduser().resolve())},
                    "errors": [str(exc)],
                }
            )
        else:
            typer.echo(f"Spec export invalid: {Path(source).expanduser().resolve()}")
            typer.echo(f"  - {exc}")
        raise typer.Exit(code=1) from exc
    if output == OutputFormat.JSON:
        _emit_json(result)
        return
    typer.echo(f"Spec export: {result['source']['kind']}")
    if result["source"]["path"] is not None:
        typer.echo(f"Path: {result['source']['path']}")
    typer.echo(
        "Sections: "
        + ", ".join(
            f"{section}={len(values)}"
            for section, values in result["registry"].items()
        )
    )


@specs_app.command("diff")
def specs_diff(
    source: Annotated[
        str,
        typer.Option("--source", help="Explicit custom bundle path to compare with built-in specs."),
    ],
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    try:
        result = diff_builtin_to_custom_spec_registry(builtin_spec_registry(), Path(source))
    except SpecBundleError as exc:
        if output == OutputFormat.JSON:
            _emit_json(
                {
                    "schema_version": "harness.spec_diff/v1",
                    "ok": False,
                    "source": {
                        "base": {"kind": "builtin", "path": None},
                        "compare": {"kind": "custom", "path": str(Path(source).expanduser().resolve())},
                    },
                    "errors": [str(exc)],
                }
            )
        else:
            typer.echo(f"Spec diff invalid: {Path(source).expanduser().resolve()}")
            typer.echo(f"  - {exc}")
        raise typer.Exit(code=1) from exc
    if output == OutputFormat.JSON:
        _emit_json(result)
        return
    typer.echo("Spec diff: builtin -> custom")
    typer.echo(f"Path: {result['source']['compare']['path']}")
    for section, section_diff in result["diff"].items():
        typer.echo(
            f"{section}: "
            f"added={len(section_diff['added'])}, "
            f"removed={len(section_diff['removed'])}, "
            f"changed={len(section_diff['changed'])}, "
            f"unchanged={len(section_diff['unchanged'])}"
        )


@specs_preview_app.command("agent")
def specs_preview_agent(
    agent_id: str,
    source: SpecSourceOption = "builtin",
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    _emit_effective_preview(target_kind="agent", target_id=agent_id, source=source, output=output)


@specs_preview_app.command("workbench")
def specs_preview_workbench(
    workbench_id: str,
    source: SpecSourceOption = "builtin",
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    _emit_effective_preview(target_kind="workbench", target_id=workbench_id, source=source, output=output)


def _emit_effective_preview(*, target_kind: str, target_id: str, source: str, output: OutputFormat) -> None:
    try:
        registry, source_info = _load_specs_preview_source(source)
        result = effective_policy_preview(
            registry,
            target_kind=target_kind,
            target_id=target_id,
            source_kind=source_info["kind"],
            source_path=Path(source_info["path"]) if source_info["path"] is not None else None,
        )
    except (KeyError, SpecBundleError) as exc:
        source_info = _specs_preview_error_source(source)
        if output == OutputFormat.JSON:
            _emit_json(
                {
                    "schema_version": "harness.spec_effective_preview/v1",
                    "ok": False,
                    "source": source_info,
                    "target": {"kind": target_kind, "id": target_id},
                    "errors": [str(exc).strip("'")],
                }
            )
        else:
            typer.echo(f"Spec preview invalid: {target_kind} {target_id}")
            typer.echo(f"  - {str(exc).strip("'")}")
        raise typer.Exit(code=1) from exc
    if output == OutputFormat.JSON:
        _emit_json(result)
        return
    typer.echo(f"Spec preview: {target_kind} {target_id}")
    typer.echo(f"Source: {result['source']['kind']}")
    if result["source"]["path"] is not None:
        typer.echo(f"Path: {result['source']['path']}")


def _load_specs_preview_source(source: str):
    if source == "builtin":
        return builtin_spec_registry(), {"kind": "builtin", "path": None}
    spec_path = resolve_spec_bundle_path(Path(source))
    return load_spec_registry(spec_path), {"kind": "custom", "path": str(spec_path)}


def _specs_preview_error_source(source: str) -> dict:
    if source == "builtin":
        return {"kind": "builtin", "path": None}
    return {"kind": "custom", "path": str(Path(source).expanduser().resolve())}


@agents_app.command("scaffold")
def agents_scaffold(
    agent_id: str,
    workbench: Annotated[str, typer.Option("--workbench", help="Built-in workbench id.")],
    kind: Annotated[str, typer.Option("--kind", help="Agent kind.")],
    model_profile: Annotated[str, typer.Option("--model-profile", help="Built-in model profile id.")],
    tool_policy: Annotated[str, typer.Option("--tool-policy", help="Built-in tool policy id.")],
    memory_scope: Annotated[str, typer.Option("--memory-scope", help="Built-in memory scope id.")],
    output_path: Annotated[Path, typer.Option("--output", help="Destination agent bundle directory.")],
    parent: Annotated[str | None, typer.Option("--parent", help="Optional built-in group parent id.")] = None,
    role: Annotated[str, typer.Option("--role", help="Agent role text.")] = "Custom declarative agent.",
    output_format: Annotated[OutputFormat, typer.Option("--output-format", help="Output format.")] = OutputFormat.TEXT,
) -> None:
    try:
        result = scaffold_agent_bundle(
            agent_id=agent_id,
            workbench_id=workbench,
            kind=kind,
            parent=parent,
            model_profile=model_profile,
            tool_policy=tool_policy,
            memory_scope=memory_scope,
            output_path=output_path,
            role=role,
        )
    except AgentBundleError as exc:
        _emit_agent_authoring_error(
            "harness.agent_scaffold/v1",
            str(exc).strip("'"),
            output_format,
            source_path=str(output_path.expanduser().resolve()),
        )
        raise typer.Exit(code=1) from exc
    if output_format == OutputFormat.JSON:
        _emit_json(result)
        return
    typer.echo(f"Agent bundle scaffolded: {result['source_path']}")
    typer.echo(f"Agent: {result['agent_id']}")
    typer.echo(f"Workbench: {result['workbench_id']}")


@agents_app.command("validate")
def agents_validate(bundle_path: Path, output: OutputOption = OutputFormat.TEXT) -> None:
    result = validate_agent_bundle(bundle_path)
    if output == OutputFormat.JSON:
        _emit_json(result)
    elif result["ok"]:
        typer.echo(f"Agent bundle valid: {result['source_path']}")
        typer.echo(f"Agent: {result['agent_id']}")
        typer.echo(f"Profiles: {len(result['profiles'])}")
    else:
        typer.echo(f"Agent bundle invalid: {result['source_path']}")
        for error in result["errors"]:
            typer.echo(f"  - {error}")
    if not result["ok"]:
        raise typer.Exit(code=1)


@agents_app.command("preview")
def agents_preview(bundle_path: Path, output: OutputOption = OutputFormat.TEXT) -> None:
    result = preview_agent_bundle(bundle_path)
    if output == OutputFormat.JSON:
        _emit_json(result)
    elif result["ok"]:
        typer.echo(f"Agent bundle preview: {result['source_path']}")
        typer.echo(f"Agent: {result['agent']['id']}")
        typer.echo(f"Workbench: {result['workbench']['id']}")
        typer.echo(
            "Parent chain: "
            f"{', '.join(parent['id'] for parent in result['parent_chain']) if result['parent_chain'] else 'none'}"
        )
    else:
        typer.echo(f"Agent bundle preview invalid: {result['source_path']}")
        for error in result["errors"]:
            typer.echo(f"  - {error}")
    if not result["ok"]:
        raise typer.Exit(code=1)


@agents_app.command("import")
def agents_import(
    bundle_path: Path,
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    try:
        loaded = load_agent_bundle(bundle_path)
        merge_agent_bundle_with_builtins(loaded)
        record = SQLiteStore(project_root).import_project_agent(loaded)
    except (AgentBundleError, ValueError) as exc:
        _emit_agent_authoring_error("harness.project_agent/v1", str(exc).strip("'"), output)
        raise typer.Exit(code=1) from exc
    if output == OutputFormat.JSON:
        payload = record.model_dump(mode="json")
        payload.update({"ok": True})
        _emit_json(payload)
        return
    typer.echo(f"Imported agent {record.agent_id}")
    typer.echo(f"Workbench: {record.workbench_id}")
    typer.echo(f"Source: {record.source_path}")


@agents_app.command("list")
def agents_list(project: ProjectOption = Path("."), output: OutputOption = OutputFormat.TEXT) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    records = SQLiteStore(project_root).list_project_agents()
    if output == OutputFormat.JSON:
        _emit_json(
            {
                "schema_version": "harness.project_agents/v1",
                "ok": True,
                "agents": [record.model_dump(mode="json") for record in records],
            }
        )
        return
    if not records:
        typer.echo("No project agents imported.")
        return
    _print_tsv(["agent_id", "workbench", "content_sha256", "source_path"])
    for record in records:
        _print_tsv_row([record.agent_id, record.workbench_id, record.content_sha256, record.source_path])


@agents_app.command("inspect")
def agents_inspect(
    agent_id: str,
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    try:
        record = SQLiteStore(project_root).get_project_agent(agent_id)
    except KeyError as exc:
        _emit_agent_authoring_error("harness.project_agent/v1", str(exc).strip("'"), output)
        raise typer.Exit(code=1) from exc
    if output == OutputFormat.JSON:
        payload = record.model_dump(mode="json")
        payload.update({"ok": True})
        _emit_json(payload)
        return
    _print_section("Agent")
    _print_kv("Agent id", record.agent_id)
    _print_kv("Workbench", record.workbench_id)
    _print_kv("Profiles", len(record.profiles))
    _print_section("Source")
    _print_kv("Path", record.source_path)
    _print_kv("Content SHA256", record.content_sha256)


@agents_app.command("preview-imported")
def agents_preview_imported(
    agent_id: str,
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    try:
        result = SQLiteStore(project_root).preview_project_agent(agent_id)
    except KeyError as exc:
        _emit_agent_authoring_error("harness.project_agent_preview/v1", str(exc).strip("'"), output)
        raise typer.Exit(code=1) from exc
    if output == OutputFormat.JSON:
        _emit_json(result)
        return
    typer.echo(f"Project agent preview: {result['agent_id']}")
    typer.echo(f"Workbench: {result['workbench_id']}")
    typer.echo(f"Drift: {result['drift']['status']}")
    typer.echo(
        "Parent chain: "
        f"{', '.join(parent['id'] for parent in result['parent_chain']) if result['parent_chain'] else 'none'}"
    )


@agents_app.command("remove")
def agents_remove(
    agent_id: str,
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    try:
        record = SQLiteStore(project_root).remove_project_agent(agent_id)
    except (KeyError, ValueError) as exc:
        _emit_agent_authoring_error("harness.project_agent/v1", str(exc).strip("'"), output)
        raise typer.Exit(code=1) from exc
    if output == OutputFormat.JSON:
        _emit_json(
            {
                "schema_version": "harness.project_agent/v1",
                "ok": True,
                "removed": True,
                "agent": record.model_dump(mode="json"),
            }
        )
        return
    typer.echo(f"Removed project agent {record.agent_id}")


@policy_app.command("explain")
def policy_explain(
    subject_kind: PolicySubjectKindOption,
    subject_id: PolicySubjectIdOption,
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    try:
        policy, extra = _resolve_policy_explain(project_root, subject_kind, subject_id)
    except (KeyError, ValueError) as exc:
        _emit_policy_error(str(exc).strip("'"), output)
        raise typer.Exit(code=1) from exc
    policy_hash = effective_policy_sha256(policy)
    if output == OutputFormat.JSON:
        payload = policy.model_dump(mode="json")
        payload.update({"ok": True, "policy_sha256": policy_hash, **extra})
        _emit_json(payload)
        return
    _print_section("Policy")
    _print_kv("Subject", f"{policy.subject_kind}/{policy.subject_id}")
    _print_kv("Policy SHA256", policy_hash)
    if extra.get("backend_descriptor_sha256"):
        _print_kv("Backend descriptor SHA256", extra["backend_descriptor_sha256"])
    _print_section("Levels")
    _print_tsv(["key", "level"])
    for key, level in policy.levels.items():
        _print_tsv_row([key, level.value])
    _print_section("Approvals")
    _print_kv("Required approvals", ", ".join(policy.required_approvals) if policy.required_approvals else "none")
    _print_section("Forbidden")
    _print_kv("Reasons", "; ".join(policy.forbidden_reasons) if policy.forbidden_reasons else "none")


@autonomy_policy_app.command("inspect")
def autonomy_policy_inspect(
    profile: Annotated[str, typer.Option("--profile", help="Built-in autonomy profile id.")] = "manual",
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    project_root = resolve_project_root(project)
    try:
        policy = get_builtin_autonomy_policy(profile)
    except KeyError as exc:
        _emit_autonomy_error(str(exc).strip("'"), output)
        raise typer.Exit(code=1) from exc
    payload = {
        "schema_version": "harness.autonomy_policy_inspect/v1",
        "ok": True,
        "project_root": str(project_root),
        "available_profiles": sorted(builtin_autonomy_policies()),
        "policy": policy.model_dump(mode="json"),
    }
    if output == OutputFormat.JSON:
        _emit_json(payload)
        return
    typer.echo(f"Profile: {policy.id}")
    typer.echo(f"Scope: {policy.scope.value}")
    _print_section("Allowed")
    _print_kv("Tools", ", ".join(policy.allowed_tools) if policy.allowed_tools else "none")
    _print_kv("Adapters", ", ".join(policy.allowed_adapters) if policy.allowed_adapters else "none")
    _print_kv("Task types", ", ".join(policy.allowed_task_types) if policy.allowed_task_types else "none")
    _print_kv("Boundaries", ", ".join(policy.allowed_boundaries) if policy.allowed_boundaries else "none")
    _print_section("Risks")
    _print_kv("Auto-confirm", ", ".join(policy.auto_confirm_risks) if policy.auto_confirm_risks else "none")
    _print_kv("Pause", ", ".join(policy.pause_on_risks) if policy.pause_on_risks else "none")
    _print_kv("Forbidden", ", ".join(policy.forbidden_risks) if policy.forbidden_risks else "none")
    _print_section("Budgets")
    for key, value in policy.budget.model_dump(mode="json").items():
        _print_kv(key, value if value is not None else "none")


def _resolve_policy_explain(project_root: Path, subject_kind: str, subject_id: str):
    normalized_kind = subject_kind.strip().lower()
    if normalized_kind not in {"run", "task", "agent", "workbench", "backend"}:
        raise ValueError(f"Unsupported policy subject kind: {subject_kind}")
    store = SQLiteStore(project_root)
    if normalized_kind == "run":
        manifest = store.build_run_manifest(subject_id)
        if manifest.effective_policy is None:
            raise KeyError(f"Effective policy not found for run: {subject_id}")
        return manifest.effective_policy, {"backend_descriptor_sha256": manifest.backend_descriptor_sha256}
    if normalized_kind == "task":
        return resolve_task_effective_policy(store.get_task(subject_id)), {}
    registry = builtin_spec_registry()
    if normalized_kind == "agent":
        return resolve_agent_effective_policy(registry, subject_id), {}
    if normalized_kind == "workbench":
        return resolve_workbench_effective_policy(registry, subject_id), {}
    cfg = load_config(project_root)
    try:
        backend = cfg.backends[subject_id]
    except KeyError as exc:
        raise KeyError(f"Backend not found: {subject_id}") from exc
    descriptor = backend.to_descriptor()
    return resolve_backend_effective_policy(descriptor), {
        "backend_descriptor_sha256": backend_descriptor_sha256(descriptor)
    }


@artifacts_app.command("list")
def artifacts_list(
    run_id: str,
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    store = SQLiteStore(project_root)
    try:
        artifacts = store.verify_artifacts(run_id)
    except KeyError as exc:
        _emit_artifact_error("harness.artifacts/v1", str(exc).strip("'"), output)
        raise typer.Exit(code=1) from exc
    if output == OutputFormat.JSON:
        _emit_json(
            {
                "schema_version": "harness.artifacts/v1",
                "ok": True,
                "run_id": run_id,
                "artifacts": [artifact.model_dump(mode="json") for artifact in artifacts],
            }
        )
        return
    if not artifacts:
        typer.echo("No artifacts found.")
        return
    _print_tsv(["artifact_id", "kind", "status", "sha256", "size_bytes"])
    for artifact in artifacts:
        _print_tsv_row(
            [
                artifact.id,
                artifact.kind,
                artifact.evidence_status,
                artifact.sha256 or "none",
                artifact.size_bytes if artifact.size_bytes is not None else "unknown",
            ]
        )


@artifacts_app.command("inspect")
def artifacts_inspect(
    artifact_id: str,
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    store = SQLiteStore(project_root)
    try:
        artifact = store.verify_artifact(artifact_id)
    except KeyError as exc:
        _emit_artifact_error("harness.artifact/v1", str(exc).strip("'"), output)
        raise typer.Exit(code=1) from exc
    if output == OutputFormat.JSON:
        payload = artifact.model_dump(mode="json")
        payload.update({"ok": True})
        _emit_json(payload)
        return
    _print_section("Artifact")
    _print_kv("Artifact id", artifact.id)
    _print_kv("Run", artifact.run_id)
    _print_kv("Kind", artifact.kind)
    _print_kv("Status", artifact.evidence_status)
    _print_section("Evidence")
    _print_kv("SHA256", artifact.sha256 or "none")
    _print_kv("Size bytes", artifact.size_bytes if artifact.size_bytes is not None else "unknown")
    _print_kv("Path", artifact.path)


@tools_app.command("list")
def tools_list(project: ProjectOption = Path("."), output: OutputOption = OutputFormat.TEXT) -> None:
    resolve_project_root(project)
    descriptors = list_tool_capabilities()
    if output == OutputFormat.JSON:
        _emit_json(
            {
                "schema_version": "harness.tool_capabilities/v1",
                "ok": True,
                "tools": [descriptor.model_dump(mode="json") for descriptor in descriptors],
            }
        )
        return
    for descriptor in descriptors:
        typer.echo(
            f"{descriptor.id}\t{descriptor.side_effect_level.value}\t"
            f"approvals={','.join(descriptor.approval_required) if descriptor.approval_required else 'none'}\t"
            f"sandbox={descriptor.sandbox_required}\t"
            f"replay={descriptor.replay_policy.value}"
        )


@tools_app.command("inspect")
def tools_inspect(
    tool_id: str,
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    resolve_project_root(project)
    try:
        descriptor = get_tool_capability(tool_id)
    except KeyError as exc:
        _emit_tool_error(str(exc).strip("'"), output)
        raise typer.Exit(code=1) from exc
    if output == OutputFormat.JSON:
        payload = descriptor.model_dump(mode="json")
        payload.update({"ok": True})
        _emit_json(payload)
        return
    typer.echo(f"Tool: {descriptor.id}")
    typer.echo(f"Side effect: {descriptor.side_effect_level.value}")
    typer.echo(
        "Approvals: "
        f"{', '.join(descriptor.approval_required) if descriptor.approval_required else 'none'}"
    )
    typer.echo(f"Sandbox required: {descriptor.sandbox_required}")
    typer.echo(f"Replay policy: {descriptor.replay_policy.value}")


@capabilities_app.command("list")
def capabilities_list(project: ProjectOption = Path("."), output: OutputOption = OutputFormat.TEXT) -> None:
    project_root = resolve_project_root(project)
    catalog = build_capability_catalog(project_root)
    if output == OutputFormat.JSON:
        _emit_json(catalog.model_dump(mode="json"))
        return
    _print_tsv(["capability_id", "task_types", "readiness", "approvals"])
    for capability in catalog.capabilities:
        _print_tsv_row(
            [
                capability.id,
                ", ".join(capability.supported_task_types),
                capability.readiness,
                ", ".join(capability.required_approvals) if capability.required_approvals else "none",
            ]
        )


@capabilities_app.command("inspect")
def capabilities_inspect(
    capability_id: str,
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    project_root = resolve_project_root(project)
    try:
        capability = get_capability(project_root, capability_id)
    except KeyError as exc:
        _emit_capability_error(str(exc).strip("'"), project_root, output)
        raise typer.Exit(code=1) from exc
    if output == OutputFormat.JSON:
        payload = capability.model_dump(mode="json")
        payload.update({"ok": True, "project_root": str(project_root)})
        _emit_json(payload)
        return
    _print_section("Capability")
    _print_kv("Capability id", capability.id)
    _print_kv("Title", capability.title)
    _print_kv("Adapter", capability.execution_adapter)
    _print_kv("Readiness", capability.readiness)
    for explanation in capability.blocked_state_explanations:
        _print_kv("Blocked state", render_blocked_state(explanation))
    _print_kv("Task types", ", ".join(capability.supported_task_types) if capability.supported_task_types else "none")
    _print_kv(
        "Required approvals",
        ", ".join(capability.required_approvals) if capability.required_approvals else "none",
    )
    _print_section("Safety")
    _print_kv("Side effects", capability.side_effect_summary)
    if capability.sandbox_profile:
        _print_kv("Sandbox profile", capability.sandbox_profile.get("id"))
    _print_kv("Replay policy", capability.replay_policy.value)
    for note in capability.safety_notes:
        typer.echo(f"- {note}")
    _print_section("Equivalent Commands")
    for command in capability.equivalent_commands:
        typer.echo(command)


@controls_app.command("list")
def controls_list(project: ProjectOption = Path("."), output: OutputOption = OutputFormat.TEXT) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    store = SQLiteStore(project_root)
    store.initialize()
    controls = store.list_execution_controls()
    breakers = store.list_adapter_breaker_states([descriptor.id for descriptor in list_execution_adapter_descriptors()])
    payload = {
        "schema_version": "harness.execution_controls/v1",
        "ok": True,
        "project_root": str(project_root),
        "controls": [control.model_dump(mode="json") for control in controls],
        "breakers": [breaker.model_dump(mode="json") for breaker in breakers],
    }
    if output == OutputFormat.JSON:
        _emit_json(payload)
        return
    _print_tsv(["target_kind", "target_id", "disabled", "reason"])
    for control in controls:
        _print_tsv_row([control.target_kind.value, control.target_id, str(control.disabled), control.reason])


@controls_app.command("disable")
def controls_disable(
    target_kind: Annotated[KillSwitchTargetKind, typer.Option("--target-kind", help="Control target kind.")],
    target_id: Annotated[str, typer.Option("--target-id", help="Control target id, or * for global controls.")],
    reason: Annotated[str, typer.Option("--reason", help="Reason for disabling this control.")],
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    try:
        _validate_control_target(target_kind, target_id)
    except ValueError as exc:
        _emit_controls_error(str(exc), project_root, output)
        raise typer.Exit(code=1) from exc
    store = SQLiteStore(project_root)
    store.initialize()
    control = store.disable_execution_control(
        target_kind,
        target_id,
        reason=reason,
        actor=_daemon_owner(),
    )
    payload = {
        "schema_version": "harness.execution_control/v1",
        "ok": True,
        "project_root": str(project_root),
        "control": control.model_dump(mode="json"),
    }
    if output == OutputFormat.JSON:
        _emit_json(payload)
        return
    typer.echo(f"Disabled {control.target_kind.value}:{control.target_id}")


@controls_app.command("enable")
def controls_enable(
    target_kind: Annotated[KillSwitchTargetKind, typer.Option("--target-kind", help="Control target kind.")],
    target_id: Annotated[str, typer.Option("--target-id", help="Control target id, or * for global controls.")],
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    try:
        _validate_control_target(target_kind, target_id)
    except ValueError as exc:
        _emit_controls_error(str(exc), project_root, output)
        raise typer.Exit(code=1) from exc
    store = SQLiteStore(project_root)
    store.initialize()
    control = store.enable_execution_control(
        target_kind,
        target_id,
        reason="Control enabled.",
        actor=_daemon_owner(),
    )
    payload = {
        "schema_version": "harness.execution_control/v1",
        "ok": True,
        "project_root": str(project_root),
        "control": control.model_dump(mode="json"),
    }
    if output == OutputFormat.JSON:
        _emit_json(payload)
        return
    typer.echo(f"Enabled {control.target_kind.value}:{control.target_id}")


@controls_app.command("breaker-status")
def controls_breaker_status(project: ProjectOption = Path("."), output: OutputOption = OutputFormat.TEXT) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    store = SQLiteStore(project_root)
    store.initialize()
    breakers = store.list_adapter_breaker_states([descriptor.id for descriptor in list_execution_adapter_descriptors()])
    payload = {
        "schema_version": "harness.adapter_breakers/v1",
        "ok": True,
        "project_root": str(project_root),
        "breakers": [breaker.model_dump(mode="json") for breaker in breakers],
    }
    if output == OutputFormat.JSON:
        _emit_json(payload)
        return
    _print_tsv(["adapter_id", "status", "failures", "threshold"])
    for breaker in breakers:
        _print_tsv_row(
            [breaker.adapter_id, breaker.status.value, str(breaker.failure_count), str(breaker.threshold)]
        )


@controls_app.command("breaker-reset")
def controls_breaker_reset(
    adapter_id: str,
    reason: Annotated[str, typer.Option("--reason", help="Reason for resetting this breaker.")],
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    try:
        _validate_adapter_id(adapter_id)
    except ValueError as exc:
        _emit_controls_error(str(exc), project_root, output)
        raise typer.Exit(code=1) from exc
    store = SQLiteStore(project_root)
    store.initialize()
    breaker = store.reset_adapter_breaker(adapter_id, reason=reason, actor=_daemon_owner())
    payload = {
        "schema_version": "harness.adapter_breaker/v1",
        "ok": True,
        "project_root": str(project_root),
        "breaker": breaker.model_dump(mode="json"),
    }
    if output == OutputFormat.JSON:
        _emit_json(payload)
        return
    typer.echo(f"Reset breaker {breaker.adapter_id}: {breaker.status.value}")


def _validate_control_target(target_kind: KillSwitchTargetKind, target_id: str) -> None:
    if target_kind in {
        KillSwitchTargetKind.HOSTED_BOUNDARY,
        KillSwitchTargetKind.DOCKER_EXECUTION,
        KillSwitchTargetKind.ACTIVE_REPO_APPLY_BACK,
    }:
        if target_id != "*":
            raise ValueError(f"{target_kind.value} controls require --target-id *")
        return
    if target_kind == KillSwitchTargetKind.ADAPTER:
        _validate_adapter_id(target_id)
    elif target_kind == KillSwitchTargetKind.TASK_TYPE:
        known = {task_type for descriptor in list_execution_adapter_descriptors() for task_type in descriptor.supported_task_types}
        if target_id not in known:
            raise ValueError(f"Unknown registered task type: {target_id}")
    elif target_kind == KillSwitchTargetKind.BACKEND and target_id != "codex_cli":
        raise ValueError(f"Unknown registered backend target: {target_id}")


def _validate_adapter_id(adapter_id: str) -> None:
    known = {descriptor.id for descriptor in list_execution_adapter_descriptors()}
    if adapter_id not in known:
        raise ValueError(f"Unknown registered adapter: {adapter_id}")


def _emit_controls_error(message: str, project_root: Path, output: OutputFormat) -> None:
    payload = {
        "schema_version": "harness.execution_controls/v1",
        "ok": False,
        "project_root": str(project_root),
        "errors": [message],
    }
    if output == OutputFormat.JSON:
        _emit_json(payload)
    else:
        typer.echo(f"Error: {message}")


@memory_app.command("save-note")
def memory_save_note(
    scope: Annotated[MemoryScopeType, typer.Option("--scope", help="Memory scope type.")],
    summary: Annotated[str, typer.Option("--summary", help="Operator note summary.")],
    scope_id: Annotated[str | None, typer.Option("--scope-id", help="Scope id for workbench, agent, or objective.")] = None,
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    store = SQLiteStore(project_root)
    try:
        store.initialize()
        resolved_scope_id = _resolve_memory_scope_id(store, project_root, scope, scope_id)
        record = store.save_memory_note(scope, resolved_scope_id, summary)
    except (KeyError, ValueError) as exc:
        _emit_memory_error(str(exc).strip("'"), output)
        raise typer.Exit(code=1) from exc
    if output == OutputFormat.JSON:
        _emit_json({"schema_version": "harness.memory_record/v1", "ok": True, "memory": record.model_dump(mode="json")})
        return
    typer.echo(f"Saved memory {record.id}")
    typer.echo(f"Scope: {record.scope_type.value}:{record.scope_id}")
    typer.echo(f"Redaction: {record.redaction_state.value}")


@memory_app.command("save-derived")
def memory_save_derived(
    scope: Annotated[MemoryScopeType, typer.Option("--scope", help="Memory scope type.")],
    source_kind: Annotated[MemorySourceKind, typer.Option("--source-kind", help="Derived memory source kind.")],
    source_id: Annotated[str, typer.Option("--source-id", help="Source run, task, objective, or attempt id.")],
    summary: Annotated[str, typer.Option("--summary", help="Derived memory summary.")],
    scope_id: Annotated[str | None, typer.Option("--scope-id", help="Scope id for workbench, agent, objective, or task.")] = None,
    source_artifact_id: Annotated[str | None, typer.Option("--source-artifact-id", help="Source artifact id for artifact summaries.")] = None,
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    store = SQLiteStore(project_root)
    try:
        resolved_scope_id = _resolve_memory_scope_id(store, project_root, scope, scope_id)
        record = store.save_derived_memory(
            scope,
            resolved_scope_id,
            source_kind,
            summary,
            source_id=source_id,
            source_artifact_id=source_artifact_id,
        )
    except (KeyError, ValueError) as exc:
        _emit_memory_error(str(exc).strip("'"), output)
        raise typer.Exit(code=1) from exc
    if output == OutputFormat.JSON:
        _emit_json({"schema_version": "harness.memory_record/v1", "ok": True, "memory": record.model_dump(mode="json")})
        return
    typer.echo(f"Saved memory {record.id}")
    typer.echo(f"Scope: {record.scope_type.value}:{record.scope_id}")
    typer.echo(f"Source: {record.source_kind.value}:{record.source_id}")
    typer.echo(f"Redaction: {record.redaction_state.value}")


@memory_app.command("list")
def memory_list(
    scope: Annotated[MemoryScopeType | None, typer.Option("--scope", help="Filter by memory scope type.")] = None,
    scope_id: Annotated[str | None, typer.Option("--scope-id", help="Filter by scope id.")] = None,
    include_forgotten: Annotated[bool, typer.Option("--include-forgotten", help="Include forgotten memory records.")] = False,
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    try:
        records = SQLiteStore(project_root).list_memory_records(
            scope,
            scope_id,
            include_forgotten=include_forgotten,
        )
    except ValueError as exc:
        _emit_memory_error(str(exc).strip("'"), output)
        raise typer.Exit(code=1) from exc
    if output == OutputFormat.JSON:
        _emit_json(
            {
                "schema_version": "harness.memory_records/v1",
                "ok": True,
                "project_root": str(project_root),
                "memory": [record.model_dump(mode="json") for record in records],
            }
        )
        return
    if not records:
        typer.echo("No memory records found.")
        return
    _print_tsv(["memory_id", "scope", "redaction", "summary"])
    for record in records:
        _print_tsv_row(
            [
                record.id,
                f"{record.scope_type.value}:{record.scope_id}",
                record.redaction_state.value,
                record.summary,
            ]
        )


@memory_app.command("inspect")
def memory_inspect(
    memory_id: str,
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    try:
        record = SQLiteStore(project_root).get_memory_record(memory_id)
    except KeyError as exc:
        _emit_memory_error(str(exc).strip("'"), output)
        raise typer.Exit(code=1) from exc
    if output == OutputFormat.JSON:
        payload = record.model_dump(mode="json")
        payload.update({"ok": True})
        _emit_json(payload)
        return
    _print_section("Memory")
    _print_kv("Memory id", record.id)
    _print_kv("Scope", f"{record.scope_type.value}:{record.scope_id}")
    _print_kv("Source", record.source_kind.value)
    _print_kv("Redaction", record.redaction_state.value)
    _print_kv("Summary", record.summary)
    _print_section("Evidence")
    _print_kv("SHA256", record.sha256)
    _print_kv("Size bytes", record.size_bytes)


@memory_app.command("forget")
def memory_forget(
    memory_id: str,
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    try:
        record = SQLiteStore(project_root).forget_memory_record(memory_id)
    except KeyError as exc:
        _emit_memory_error(str(exc).strip("'"), output)
        raise typer.Exit(code=1) from exc
    if output == OutputFormat.JSON:
        _emit_json({"schema_version": "harness.memory_record/v1", "ok": True, "memory": record.model_dump(mode="json")})
        return
    typer.echo(f"Forgot memory {record.id}")


@app.command("progress")
def progress(
    objective: Annotated[str, typer.Option("--objective", help="Objective id to inspect.")],
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    try:
        payload = build_orchestration_progress(project_root, objective)
    except KeyError as exc:
        _emit_progress_error(str(exc).strip("'"), project_root, objective, output)
        raise typer.Exit(code=1) from exc
    if output == OutputFormat.JSON:
        _emit_json(payload.model_dump(mode="json"))
        return
    _print_section("Orchestration Progress")
    _print_kv("Objective", f"{payload.objective_id} | {payload.objective_title}")
    _print_kv("Mode", payload.mode.value)
    _print_kv("Next action", payload.next_action or "none")
    if payload.active_lease_ids:
        _print_kv("Active leases", ", ".join(payload.active_lease_ids))
    if payload.active_run_ids:
        _print_kv("Active runs", ", ".join(payload.active_run_ids))
    if payload.blocked_reasons:
        _print_kv("Blocked reasons", "; ".join(payload.blocked_reasons))
    if payload.untrusted_context_warnings:
        _print_kv("Context warnings", ", ".join(payload.untrusted_context_warnings))
    _print_section("Tasks")
    if not payload.tasks:
        typer.echo("No tasks for this objective.")
        return
    _print_tsv(["task_id", "status", "adapter", "task_type", "lease", "run", "next"])
    for task in payload.tasks:
        _print_tsv_row(
            [
                task.task_id,
                task.status.value,
                task.execution_adapter or "",
                task.task_type or "",
                task.lease_id or "",
                task.run_id or "",
                task.next_action or "",
            ]
        )


@sandbox_app.command("profiles")
def sandbox_profiles(project: ProjectOption = Path("."), output: OutputOption = OutputFormat.TEXT) -> None:
    project_root = resolve_project_root(project)
    catalog = build_sandbox_profile_catalog(project_root)
    if output == OutputFormat.JSON:
        _emit_json(catalog.model_dump(mode="json"))
        return
    _print_tsv(["profile_id", "tier", "network", "active_repo_write", "host_filesystem"])
    for profile in catalog.profiles:
        _print_tsv_row(
            [
                profile.id,
                profile.tier.value,
                profile.network.value,
                profile.active_repo_write.value,
                profile.host_filesystem.value,
            ]
        )


@sandbox_app.command("inspect")
def sandbox_inspect(
    profile_id: str,
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    project_root = resolve_project_root(project)
    try:
        profile = get_sandbox_profile(profile_id)
    except KeyError as exc:
        _emit_sandbox_profile_error(str(exc).strip("'"), project_root, output)
        raise typer.Exit(code=1) from exc
    if output == OutputFormat.JSON:
        payload = profile.model_dump(mode="json")
        payload.update({"ok": True, "project_root": str(project_root)})
        _emit_json(payload)
        return
    _print_section("Sandbox Profile")
    _print_kv("Profile id", profile.id)
    _print_kv("Tier", profile.tier.value)
    _print_kv("Network", profile.network.value)
    _print_kv("Active repo write", profile.active_repo_write.value)
    _print_kv("Host filesystem", profile.host_filesystem.value)
    _print_kv("Secret path policy", profile.secret_path_policy)


@baseline_app.command("set")
def baseline_set(
    run_id: str,
    name: Annotated[str, typer.Option("--name", help="Baseline name.")],
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    store = SQLiteStore(project_root)
    try:
        baseline = store.set_run_baseline(name, run_id)
    except (KeyError, ValueError) as exc:
        _emit_compare_error("harness.baseline/v1", str(exc).strip("'"), output)
        raise typer.Exit(code=1) from exc
    if output == OutputFormat.JSON:
        payload = baseline.model_dump(mode="json")
        payload.update({"ok": True})
        _emit_json(payload)
        return
    typer.echo(f"Baseline: {baseline.name}")
    typer.echo(f"Run: {baseline.run_id}")
    typer.echo(f"Evidence: {baseline.evidence_sha256}")


@baseline_app.command("compare")
def baseline_compare(
    run_id: str,
    baseline: Annotated[str, typer.Option("--baseline", help="Baseline name.")],
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    store = SQLiteStore(project_root)
    try:
        result = store.compare_run_to_baseline(run_id, baseline)
    except KeyError as exc:
        _emit_compare_error("harness.baseline_compare/v1", str(exc).strip("'"), output)
        raise typer.Exit(code=1) from exc
    if output == OutputFormat.JSON:
        _emit_json(result)
        return
    typer.echo(f"Baseline: {result['baseline']['name']}")
    _print_compare_result(result["comparison"])


@evals_app.command("run")
def evals_run(
    suite: Annotated[str, typer.Option("--suite", help="Eval suite id.")] = "safety-smoke",
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    project_root = resolve_project_root(project)
    if suite == "integrity":
        result = run_integrity_check(project_root)
        _emit_integrity_check_result(result, output)
        if not result.ok:
            raise typer.Exit(code=1)
        return
    if suite == "security-layer":
        result = run_security_layer_audit(project_root)
        _emit_security_layer_audit_result(result, output)
        if not result.ok:
            raise typer.Exit(code=1)
        return
    _require_initialized(project_root)
    if suite == "safety-smoke":
        result = run_safety_smoke(project_root, load_config(project_root), SQLiteStore(project_root))
        if output == OutputFormat.JSON:
            _emit_json(result.model_dump(mode="json"))
        else:
            typer.echo(f"Suite: {result.suite}")
            typer.echo(f"Overall: {'pass' if result.ok else 'fail'}")
            for check in result.checks:
                typer.echo(f"{check.status}\t{check.id}\t{check.message}")
        if not result.ok:
            raise typer.Exit(code=1)
        return
    if suite == "security":
        result = run_security_check(project_root, SQLiteStore(project_root))
        _emit_security_check_result(result, output)
        if not result.ok:
            raise typer.Exit(code=1)
        return
    else:
        _emit_eval_error(f"Unsupported eval suite: {suite}", output)
        raise typer.Exit(code=1)


@security_app.command("check")
def security_check(project: ProjectOption = Path("."), output: OutputOption = OutputFormat.TEXT) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    result = run_security_check(project_root, SQLiteStore(project_root))
    _emit_security_check_result(result, output)
    if not result.ok:
        raise typer.Exit(code=1)


def _emit_security_check_result(result, output: OutputFormat) -> None:
    if output == OutputFormat.JSON:
        _emit_json(result.model_dump(mode="json"))
        return
    typer.echo("Suite: security")
    typer.echo(f"Overall: {'pass' if result.ok else 'fail'}")
    if not result.findings:
        typer.echo("pass\tinfo\tsecurity\tNo security detections found.")
    for finding in result.findings:
        typer.echo(f"{finding.status.value}\t{finding.severity.value}\t{finding.check_id}\t{finding.message}")


@security_app.command("audit")
def security_audit(project: ProjectOption = Path("."), output: OutputOption = OutputFormat.TEXT) -> None:
    project_root = resolve_project_root(project)
    result = run_security_layer_audit(project_root)
    _emit_security_layer_audit_result(result, output)
    if not result.ok:
        raise typer.Exit(code=1)


def _emit_security_layer_audit_result(result, output: OutputFormat) -> None:
    if output == OutputFormat.JSON:
        _emit_json(result.model_dump(mode="json"))
        return
    typer.echo("Suite: security-layer")
    typer.echo(f"Overall: {'pass' if result.ok else 'fail'}")
    for check in result.checks:
        typer.echo(f"{check.status}\t{check.id}\t{check.message}")


@integrity_app.command("check")
def integrity_check(project: ProjectOption = Path("."), output: OutputOption = OutputFormat.TEXT) -> None:
    project_root = resolve_project_root(project)
    result = run_integrity_check(project_root)
    _emit_integrity_check_result(result, output)
    if not result.ok:
        raise typer.Exit(code=1)


def _emit_integrity_check_result(result, output: OutputFormat) -> None:
    if output == OutputFormat.JSON:
        _emit_json(result.model_dump(mode="json"))
        return
    typer.echo("Suite: integrity")
    typer.echo(f"Overall: {'pass' if result.ok else 'fail'}")
    for check in result.checks:
        typer.echo(f"{check.status.value}\t{check.subject_kind.value}\t{check.subject_id}\t{check.message}")


@traces_app.command("export")
def traces_export(
    run_id: str,
    format: TraceFormatOption = "otel-json",
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    if format != "otel-json":
        _emit_trace_error(f"Unsupported trace format: {format}", output)
        raise typer.Exit(code=1)
    try:
        payload = to_otel_json(export_run_trace(project_root, SQLiteStore(project_root), run_id))
    except KeyError as exc:
        _emit_trace_error(str(exc).strip("'"), output)
        raise typer.Exit(code=1) from exc
    if output == OutputFormat.JSON:
        _emit_json(payload)
        return
    span_count = sum(len(scope["spans"]) for resource in payload["resourceSpans"] for scope in resource["scopeSpans"])
    typer.echo(f"Trace: {payload['trace_id']}")
    typer.echo(f"Run: {payload['run_id']}")
    typer.echo(f"Format: {payload['format']}")
    typer.echo(f"Spans: {span_count}")


@daemon_app.command("run-once")
def daemon_run_once(project: ProjectOption = Path("."), output: OutputOption = OutputFormat.TEXT) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    owner = _daemon_owner()
    try:
        result = SQLiteStore(project_root).daemon_run_once(owner=owner, pid=os.getpid())
    except (KeyError, ValueError) as exc:
        _emit_daemon_error("harness.daemon_tick/v1", str(exc).strip("'"), output)
        raise typer.Exit(code=1) from exc
    if output == OutputFormat.JSON:
        _emit_json(result.model_dump(mode="json"))
        return
    typer.echo(f"Daemon: {result.daemon_id}")
    typer.echo(f"Decision: {result.decision}")
    if result.selected_task is not None:
        typer.echo(f"Leased task: {result.selected_task.id}")
    for reason in result.pause_reasons:
        typer.echo(f"Paused task: {reason['task_id']}\t{reason['decision']}")


@daemon_app.command("run-autonomous")
def daemon_run_autonomous(
    project: ProjectOption = Path("."),
    autonomy: Annotated[str, typer.Option("--autonomy", help="Autonomy profile id.")] = "daemon-safe",
    max_steps: Annotated[int | None, typer.Option("--max-steps", help="Maximum adapter dispatches for this run.")] = None,
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    try:
        result = run_next_active_objective_autonomously(
            project_root,
            autonomy_profile_id=autonomy,
            max_steps=max_steps,
            owner=_daemon_owner(),
        )
    except (KeyError, ValueError) as exc:
        _emit_daemon_error("harness.autonomous_objective_run/v1", str(exc).strip("'"), output)
        raise typer.Exit(code=1) from exc
    if result is None:
        payload = {
            "schema_version": "harness.autonomous_objective_run/v1",
            "ok": True,
            "project_root": str(project_root),
            "autonomy_profile_id": autonomy,
            "stop_reason": "no_active_objective",
            "result": None,
        }
        if output == OutputFormat.JSON:
            _emit_json(payload)
            return
        typer.echo("No active objective with runnable work.")
        return
    if output == OutputFormat.JSON:
        _emit_json(result.model_dump(mode="json"))
        return
    typer.echo("Autonomous Daemon Objective Run")
    _print_kv("Objective", result.objective_id)
    _print_kv("Profile", result.autonomy_profile_id)
    _print_kv("Stop reason", result.stop_reason)
    _print_kv("Adapter dispatches", result.adapter_dispatches)
    _print_kv("Evidence", str(result.evidence_path))


@daemon_app.command("status")
def daemon_status(project: ProjectOption = Path("."), output: OutputOption = OutputFormat.TEXT) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    result = SQLiteStore(project_root).daemon_status()
    if output == OutputFormat.JSON:
        _emit_json(result.model_dump(mode="json"))
        return
    typer.echo(f"Project: {result.project_root}")
    typer.echo(f"Active daemons: {len(result.active_daemons)}")
    typer.echo(f"Paused tasks: {len(result.paused_tasks)}")
    if result.active_daemons:
        _print_tsv(["daemon_id", "status", "owner", "heartbeat_at"])
        for daemon in result.active_daemons:
            _print_tsv_row([daemon.id, daemon.status.value, daemon.owner, daemon.heartbeat_at.isoformat()])


@daemon_app.command("stop")
def daemon_stop(project: ProjectOption = Path("."), output: OutputOption = OutputFormat.TEXT) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    store = SQLiteStore(project_root)
    stopped = store.stop_daemons()
    status = store.daemon_status()
    if output == OutputFormat.JSON:
        _emit_json(
            {
                "schema_version": "harness.daemon_status/v1",
                "ok": True,
                "project_root": str(project_root),
                "stopped_daemons": [daemon.model_dump(mode="json") for daemon in stopped],
                "active_daemons": [daemon.model_dump(mode="json") for daemon in status.active_daemons],
                "latest_events": [event.model_dump(mode="json") for event in status.latest_events],
                "paused_tasks": status.paused_tasks,
                "stale_after_seconds": status.stale_after_seconds,
            }
        )
        return
    typer.echo(f"Stopped daemon records: {len(stopped)}")


@daemon_app.command("recover")
def daemon_recover(project: ProjectOption = Path("."), output: OutputOption = OutputFormat.TEXT) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    owner = _daemon_owner()
    try:
        result = SQLiteStore(project_root).recover_daemon_leases(owner=owner, pid=os.getpid())
    except (KeyError, ValueError) as exc:
        _emit_daemon_error("harness.daemon_recovery/v1", str(exc).strip("'"), output)
        raise typer.Exit(code=1) from exc
    if output == OutputFormat.JSON:
        _emit_json(result.model_dump(mode="json"))
        return
    typer.echo(f"Daemon: {result.daemon_id}")
    typer.echo(f"Expired leases: {len(result.expired_leases)}")
    typer.echo(f"Recovered tasks: {len(result.recovered_tasks)}")


@daemon_app.command("execute-dry-run")
def daemon_execute_dry_run(
    lease_id: str,
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    owner = _daemon_owner()
    try:
        result = SQLiteStore(project_root).execute_dry_run_lease(lease_id, owner=owner)
    except (KeyError, ValueError) as exc:
        _emit_daemon_error("harness.daemon_execute_dry_run/v1", str(exc).strip("'"), output)
        raise typer.Exit(code=1) from exc
    if output == OutputFormat.JSON:
        _emit_json(result.model_dump(mode="json"))
        return
    typer.echo(f"Decision: {result.decision}")
    typer.echo(f"Task: {result.task.id}")
    typer.echo(f"Run: {result.run.id}")
    typer.echo(f"Lease: {result.lease.id}")


@daemon_app.command("execute-read-only")
def daemon_execute_read_only(
    lease_id: str,
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    owner = _daemon_owner()
    try:
        result = execute_read_only_summary_lease(project_root, lease_id, owner=owner)
    except (KeyError, ValueError, LocalEndpointUnavailable, CodexUnavailable, CodexSandboxUnavailable) as exc:
        _emit_daemon_error("harness.daemon_execute_read_only/v1", str(exc).strip("'"), output)
        raise typer.Exit(code=1) from exc
    if output == OutputFormat.JSON:
        _emit_json(result.model_dump(mode="json"))
        return
    typer.echo(f"Decision: {result.decision}")
    typer.echo(f"Task: {result.task.id}")
    typer.echo(f"Attempt: {result.attempt.id}")
    typer.echo(f"Run: {result.run.id}")
    typer.echo(f"Lease: {result.lease.id}")


@daemon_app.command("execute")
def daemon_execute(
    lease_id: str,
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    owner = _daemon_owner()
    try:
        result = execute_lease(project_root, lease_id, owner=owner)
    except (KeyError, ValueError) as exc:
        _emit_daemon_error("harness.daemon_execute/v1", str(exc).strip("'"), output)
        raise typer.Exit(code=1) from exc
    if output == OutputFormat.JSON:
        _emit_json(result.model_dump(mode="json"))
        if not result.ok:
            raise typer.Exit(code=1)
        return
    typer.echo("Registered-adapter dispatch")
    typer.echo(f"Decision: {result.decision}")
    if result.security_decision is not None:
        typer.echo(f"Security decision: {result.security_decision.decision.value}")
    for explanation in result.blocked_state_explanations:
        typer.echo(f"Blocked state: {render_blocked_state(explanation)}")
    if result.untrusted_context_warnings:
        typer.echo(f"Context warnings: {', '.join(result.untrusted_context_warnings)}")
    typer.echo(f"Adapter: {result.adapter_id or 'none'}")
    if result.task is not None:
        typer.echo(f"Task: {result.task.id}")
    if result.attempt is not None:
        typer.echo(f"Attempt: {result.attempt.id}")
    if result.run is not None:
        typer.echo(f"Run: {result.run.id}")
    if result.lease is not None:
        typer.echo(f"Lease: {result.lease.id}")
    if result.rejection_reasons:
        typer.echo(f"Rejected: {'; '.join(result.rejection_reasons)}")
    if not result.ok:
        raise typer.Exit(code=1)


@daemon_app.command("adapters")
def daemon_adapters(project: ProjectOption = Path("."), output: OutputOption = OutputFormat.TEXT) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    descriptors = list_execution_adapter_descriptors()
    payload = {
        "schema_version": "harness.execution_adapters/v1",
        "ok": True,
        "project_root": str(project_root),
        "adapters": [descriptor.model_dump(mode="json") for descriptor in descriptors],
    }
    if output == OutputFormat.JSON:
        _emit_json(payload)
        return
    _print_tsv(["adapter_id", "task_types", "side_effects"])
    for descriptor in descriptors:
        _print_tsv_row(
            [
                descriptor.id,
                ", ".join(descriptor.supported_task_types),
                descriptor.side_effect_summary,
            ]
        )


@daemon_app.command("inspect-lease")
def daemon_inspect_lease(
    lease_id: str,
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    try:
        result = SQLiteStore(project_root).inspect_task_lease(lease_id)
    except (KeyError, ValueError) as exc:
        _emit_daemon_error("harness.daemon_lease/v1", str(exc).strip("'"), output)
        raise typer.Exit(code=1) from exc
    if output == OutputFormat.JSON:
        _emit_json(result.model_dump(mode="json"))
        return
    _print_section("Lease")
    _print_kv("Lease id", result.lease.id)
    _print_kv("Status", result.lease.status.value)
    _print_section("Links")
    _print_kv("Task", result.task.id if result.task else "missing")
    _print_kv("Attempt", result.attempt.id if result.attempt else "missing")
    _print_kv("Run", result.run.id if result.run else "none")
    _print_section("Eligibility")
    _print_kv("Dry-run eligible", result.dry_run_eligibility.get("eligible"))
    _print_kv("Read-only eligible", result.read_only_eligibility.get("eligible"))
    _print_kv("Registered adapter eligible", result.execution_eligibility.get("eligible"))
    _print_kv("Registered adapter", result.execution_eligibility.get("adapter_id") or "none")
    if result.security_decision is not None:
        _print_kv("Security decision", result.security_decision.decision.value)
        _print_kv("Security reason", "; ".join(result.security_decision.reasons))
    for explanation in result.blocked_state_explanations:
        _print_kv("Blocked state", render_blocked_state(explanation))
    if result.untrusted_context_warnings:
        _print_kv("Context warnings", ", ".join(result.untrusted_context_warnings))
    _print_section("Recovery")
    _print_kv("Action", result.recovery_recommendation.get("action"))


@backends_app.callback()
def backends_callback(
    ctx: typer.Context,
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    if ctx.invoked_subcommand is not None:
        return
    project_root = resolve_project_root(project)
    cfg = load_config(project_root)
    if output == OutputFormat.JSON:
        _emit_json(
            {
                "schema_version": "harness.backends/v1",
                "backends": [backend.to_descriptor().model_dump(mode="json") for backend in cfg.backends.values()],
            }
        )
        return
    _print_backends(cfg)


@backends_app.command("preflight")
def backends_preflight(project: ProjectOption = Path("."), output: OutputOption = OutputFormat.TEXT) -> None:
    project_root = resolve_project_root(project)
    cfg = load_config(project_root)
    results = []
    for name, backend in cfg.backends.items():
        if name == "codex_cli":
            status = CodexCliBackend(backend).preflight()
        elif name == "local_openai_compatible":
            status = LocalOpenAICompatibleBackend(backend).preflight()
        else:
            status = None
        reason = status.reason if status else "Paid backend preflight skipped; disabled by default."
        capabilities = status.capabilities if status else backend.capabilities
        results.append(
            {
                "name": name,
                "kind": backend.kind.value,
                "metadata": backend.metadata.model_dump(mode="json"),
                "available": status.available if status else False,
                "reason": reason,
                "detected_capabilities": capabilities.model_dump(mode="json"),
            }
        )
        if output == OutputFormat.JSON:
            continue
        typer.echo(f"{name}:")
        typer.echo(f"  kind: {backend.kind.value}")
        typer.echo(f"  billing_mode: {backend.metadata.billing_mode.value}")
        typer.echo(f"  execution_location: {backend.metadata.execution_location.value}")
        typer.echo(f"  data_boundary: {backend.metadata.data_boundary.value}")
        typer.echo(f"  allow_network: {backend.metadata.allow_network}")
        typer.echo(f"  available: {status.available if status else False}")
        typer.echo(f"  reason: {reason}")
        typer.echo("  detected_capabilities:")
        for key, value in capabilities.model_dump().items():
            typer.echo(f"    {key}: {value}")
    if output == OutputFormat.JSON:
        _emit_json({"schema_version": "harness.backend_preflight/v1", "backends": results})


@app.command()
def run(
    goal: str,
    task_type: Annotated[str, typer.Option("--task-type", help="Task type route.")] = "codex_direct_agent",
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
    approve_hosted_boundary: Annotated[
        bool,
        typer.Option("--approve-hosted-boundary", help="Approve one-time hosted Codex data boundary."),
    ] = False,
    approve_secret_context: Annotated[
        bool,
        typer.Option("--approve-secret-context", help="Approve sending redacted secret-flagged context to Codex."),
    ] = False,
    keep_isolation: Annotated[
        bool,
        typer.Option("--keep-isolation", help="Preserve isolated workspace for codex_code_edit runs."),
    ] = False,
    model: Annotated[str | None, typer.Option("--model", help="Codex model override for direct agent runs.")] = None,
    reasoning_effort: Annotated[
        str | None,
        typer.Option("--reasoning-effort", help="Codex reasoning effort override for direct agent runs."),
    ] = None,
    no_stream: Annotated[bool, typer.Option("--no-stream", help="Disable live Codex event summaries.")] = False,
    fail_on_dirty: Annotated[
        bool,
        typer.Option("--fail-on-dirty", help="Refuse direct agent runs when git status is dirty."),
    ] = False,
) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    cfg = load_config(project_root)
    store = SQLiteStore(project_root)
    if task_type == "codex_direct_agent":
        result = _run_codex_direct_agent_cli(
            goal,
            project_root,
            output=output,
            model=model,
            reasoning_effort=reasoning_effort,
            stream=not no_stream,
            fail_on_dirty=fail_on_dirty,
            cfg=cfg,
            store=store,
        )
        raise typer.Exit(code=0 if result.get("status") == "completed" else 1)
    if task_type == "read_only_repo_summary":
        backend_config = cfg.backends["codex_cli"]
        backend = CodexCliBackend(backend_config)
        approvals = ApprovalStore(project_root)
        approval = approvals.find_valid("codex_cli", "hosted_provider", task_type)
        if approval is None:
            approval = _obtain_hosted_boundary_approval(
                project_root=project_root,
                task_type=task_type,
                approve_flag=approve_hosted_boundary,
            )
        runner = CodexReadOnlyRepoSummaryRunner(project_root, store, backend, approvals)
        try:
            result = runner.run(
                goal=goal,
                task_type=task_type,
                approval=approval,
                approve_secret_context=approve_secret_context,
            )
        except (CodexUnavailable, CodexSandboxUnavailable, HostedBoundaryApprovalRequired, HostedSecretBlocked) as exc:
            raise typer.BadParameter(str(exc)) from exc
        typer.echo(f"Created run {result['run_id']}")
        typer.echo(result["final_summary"])
        typer.echo("Artifacts:")
        for kind, path in result["artifacts"].items():
            typer.echo(f"  {kind}: {path}")
        return
    if task_type == "codex_code_edit":
        backend_config = cfg.backends["codex_cli"]
        backend = CodexCliBackend(backend_config)
        approvals = ApprovalStore(project_root)
        approval = approvals.find_valid("codex_cli", "hosted_provider", task_type)
        runner = CodexCodeEditRunner(
            project_root,
            store,
            backend,
            approvals,
            apply_back_approval_provider=CliApplyBackApprovalProvider(),
        )
        try:
            result = runner.run(
                goal=goal,
                task_type=task_type,
                approval=approval,
                keep_isolation=keep_isolation,
            )
        except (
            CodexUnavailable,
            CodexSandboxUnavailable,
            CodexEditCommandUnavailable,
            CodexDangerousFlagError,
            HostedBoundaryApprovalRequired,
            ActiveProjectModifiedError,
            ActiveRepoDirtyError,
        ) as exc:
            raise typer.BadParameter(str(exc)) from exc
        typer.echo(f"Created run {result['run_id']}")
        typer.echo(f"Status: {result['status']}")
        typer.echo(f"Approval id: {result['approval_id']}")
        typer.echo(f"Isolation strategy: {result['isolation_strategy']}")
        typer.echo(f"Isolation cleanup status: {result['isolation_cleanup_status']}")
        if keep_isolation:
            typer.echo(f"Isolated workspace: {result['isolated_workspace']}")
        typer.echo(
            f"Changed files: {', '.join(result['changed_files']) if result['changed_files'] else 'none'}"
        )
        typer.echo(f"Apply-back decision: {result['apply_back_decision']}")
        if result["applied_files"]:
            typer.echo(f"Applied files: {', '.join(result['applied_files'])}")
        if result["apply_back_failure"]:
            typer.echo(f"Apply-back failure: {result['apply_back_failure']}")
        typer.echo("Artifacts:")
        for kind, path in result["artifacts"].items():
            typer.echo(f"  {kind}: {path}")
        return
    if task_type == "repo_planning":
        backend_config = cfg.backends["codex_cli"]
        backend = CodexCliBackend(backend_config)
        approvals = ApprovalStore(project_root)
        approval = approvals.find_valid("codex_cli", "hosted_provider", task_type)
        if approval is None:
            approval = _obtain_hosted_boundary_approval(
                project_root=project_root,
                task_type=task_type,
                approve_flag=approve_hosted_boundary,
            )
        runner = CodexRepoPlanningRunner(project_root, store, backend, approvals)
        try:
            result = runner.run(
                goal=goal,
                task_type=task_type,
                approval=approval,
                approve_secret_context=approve_secret_context,
            )
        except (CodexUnavailable, CodexSandboxUnavailable, HostedBoundaryApprovalRequired, HostedSecretBlocked) as exc:
            raise typer.BadParameter(str(exc)) from exc
        typer.echo(f"Created run {result['run_id']}")
        typer.echo(f"Status: {result['status']}")
        typer.echo(f"Approval id: {result['approval_id']}")
        typer.echo("Artifacts:")
        for kind, path in result["artifacts"].items():
            typer.echo(f"  {kind}: {path}")
        return
    if task_type == "simple_code_edit":
        backend_config = cfg.backends["local_openai_compatible"]
        backend = LocalOpenAICompatibleBackend(backend_config)
        approval_provider = CliPatchApprovalProvider()
        runner = NativeEditRunner(
            project_root,
            cfg,
            store,
            backend,
            approval_provider,
            test_approval_provider=CliTestExecutionApprovalProvider(),
        )
        try:
            result = runner.run(goal=goal, task_type=task_type)
        except LocalEndpointUnavailable as exc:
            raise typer.BadParameter(str(exc)) from exc
        typer.echo(f"Created run {result['run_id']}")
        typer.echo(result["final_answer"])
        typer.echo(f"Changed files: {', '.join(result['changed_files']) if result['changed_files'] else 'none'}")
        typer.echo("Artifacts:")
        for kind, path in result["artifacts"].items():
            typer.echo(f"  {kind}: {path}")
        return
    raise typer.BadParameter(
        "Supported task types are codex_direct_agent, read_only_repo_summary, repo_planning, simple_code_edit, and codex_code_edit."
    )


@approvals_app.callback()
def approvals_callback(
    ctx: typer.Context,
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    if ctx.invoked_subcommand is not None:
        return
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    store = ApprovalStore(project_root)
    approvals = store.list()
    if output == OutputFormat.JSON:
        _emit_json(
            {
                "schema_version": "harness.approvals/v1",
                "approvals": [approval.model_dump(mode="json") for approval in approvals],
            }
        )
        return
    if not approvals:
        typer.echo("No approvals found.")
        return
    for approval in approvals:
        typer.echo(
            f"{approval.id}\t{approval.backend}\t{approval.data_boundary}\t"
            f"{','.join(approval.task_types)}\t{approval.expires_at.isoformat()}\t"
            f"revoked={approval.revoked}"
        )


@approvals_app.command("add")
def approvals_add(
    backend: Annotated[str, typer.Option("--backend")],
    data_boundary: Annotated[str, typer.Option("--data-boundary")],
    task_types: Annotated[str, typer.Option("--task-types", help="Comma-separated task types.")],
    duration_days: Annotated[int | None, typer.Option("--duration-days")] = None,
    duration_hours: Annotated[int | None, typer.Option("--duration-hours")] = None,
    reason: Annotated[str | None, typer.Option("--reason")] = None,
    autonomy_scope: Annotated[str | None, typer.Option("--autonomy-scope")] = None,
    allowed_adapters: Annotated[str | None, typer.Option("--allowed-adapters", help="Comma-separated adapter ids.")] = None,
    allowed_workbenches: Annotated[str | None, typer.Option("--allowed-workbenches", help="Comma-separated workbench ids.")] = None,
    allowed_objectives: Annotated[str | None, typer.Option("--allowed-objectives", help="Comma-separated objective ids.")] = None,
    max_runs: Annotated[int | None, typer.Option("--max-runs")] = None,
    max_total_runtime_seconds: Annotated[int | None, typer.Option("--max-total-runtime-seconds")] = None,
    max_context_bytes: Annotated[int | None, typer.Option("--max-context-bytes")] = None,
    project: ProjectOption = Path("."),
) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    if duration_days is None and duration_hours is None:
        raise typer.BadParameter("Specify --duration-days or --duration-hours.")
    approval = ApprovalStore(project_root).add(
        backend=backend,
        data_boundary=data_boundary,
        task_types=_split_csv(task_types),
        duration_days=duration_days or 0,
        duration_hours=duration_hours,
        reason=reason,
        allowed_adapters=_split_csv(allowed_adapters),
        allowed_workbenches=_split_csv(allowed_workbenches),
        allowed_objective_ids=_split_csv(allowed_objectives),
        max_runs=max_runs,
        max_total_runtime_seconds=max_total_runtime_seconds,
        max_context_bytes=max_context_bytes,
        autonomy_scope=autonomy_scope,
    )
    typer.echo(f"Created approval {approval.id}")


@approvals_app.command("revoke")
def approvals_revoke(approval_id: str, project: ProjectOption = Path(".")) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    if not ApprovalStore(project_root).revoke(approval_id):
        raise typer.BadParameter(f"Approval not found: {approval_id}")
    typer.echo(f"Revoked approval {approval_id}")


@tests_app.command(
    "run",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def tests_run(ctx: typer.Context, project: ProjectOption = Path(".")) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    command = list(ctx.args)
    cfg = load_config(project_root)
    store = SQLiteStore(project_root)
    runner = DockerTestRunner(project_root, cfg, store, CliTestExecutionApprovalProvider())
    try:
        result = runner.run(command)
    except CommandValidationError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(f"Created run {result['run_id']}")
    typer.echo(f"Status: {result['status']}")
    typer.echo(f"Approval decision: {result['approval_decision']}")
    typer.echo("Artifacts:")
    for kind, path in result["artifacts"].items():
        typer.echo(f"  {kind}: {path}")


@tests_image_app.command("validate")
def tests_image_validate(project: ProjectOption = Path(".")) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    cfg = load_config(project_root)
    try:
        result = DockerImageManager(project_root, cfg).validate_dockerfile()
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(f"Dockerfile: {result.dockerfile}")
    typer.echo(f"Image: {result.image}")
    typer.echo(f"Valid: {result.ok}")
    if result.issues:
        typer.echo("Issues:")
        for issue in result.issues:
            typer.echo(f"  - {issue}")
    if result.warnings:
        typer.echo("Warnings:")
        for warning in result.warnings:
            typer.echo(f"  - {warning}")
    if not result.ok:
        raise typer.Exit(code=1)


@tests_image_app.command("generate")
def tests_image_generate(
    project: ProjectOption = Path("."),
    force: Annotated[bool, typer.Option("--force", help="Overwrite the configured Dockerfile if it exists.")] = False,
) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    cfg = load_config(project_root)
    try:
        path = DockerImageManager(project_root, cfg).generate_dockerfile(force=force)
    except (ValueError, FileExistsError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(f"Generated managed Docker test image file: {path}")


@tests_image_app.command("build")
def tests_image_build(project: ProjectOption = Path(".")) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    cfg = load_config(project_root)
    try:
        result = DockerImageManager(project_root, cfg).build_image()
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(f"Image: {result.image}")
    typer.echo(f"Dockerfile: {result.dockerfile}")
    typer.echo(f"Command: {' '.join(result.command)}")
    typer.echo(f"Built: {result.ok}")
    if result.stdout:
        typer.echo("stdout:")
        typer.echo(result.stdout)
    if result.stderr:
        typer.echo("stderr:")
        typer.echo(result.stderr)
    if result.guidance:
        typer.echo(f"Guidance: {result.guidance}")
    if not result.ok:
        raise typer.Exit(code=1)


@dev_app.command("create-run")
def dev_create_run(
    goal: Annotated[str, typer.Option("--goal")],
    task_type: Annotated[str, typer.Option("--task-type")],
    project: ProjectOption = Path("."),
) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    store = SQLiteStore(project_root)
    run = store.create_run(goal=goal, task_type=task_type, status="completed")
    paths = store.initialize_run_artifacts(run.id)
    store.append_event(
        run.id,
        level="info",
        event_type="dev_create_run",
        message="Created Phase 1A diagnostic run.",
        payload={"goal": goal, "task_type": task_type},
    )
    for kind, path in paths.items():
        store.register_artifact(run.id, kind=kind, path=path)
    report_path = store.generate_final_report(run.id)
    typer.echo(f"Created run {run.id}")
    typer.echo(f"Final report: {report_path}")


def _require_initialized(project_root: Path) -> None:
    if not (project_root / HARNESS_DIR / "harness.sqlite").exists():
        raise typer.BadParameter(f"Project is not initialized. Run 'harness init --project {project_root}'.")


def _split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _update_gitignore(project_root: Path) -> None:
    path = project_root / ".gitignore"
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    required_entries = [
        ".harness/runs/",
        ".harness/harness.sqlite",
        ".harness/approvals.yaml",
        ".harness/tmp/",
        "*.egg-info/",
    ]
    existing_lines = existing.splitlines()
    if all(entry in existing_lines for entry in required_entries):
        return
    content = existing.rstrip()
    if content:
        content += "\n\n"
    if "# Harness local artifacts" not in existing_lines:
        content += "# Harness local artifacts\n"
    for entry in required_entries:
        if entry not in existing_lines:
            content += f"{entry}\n"
    path.write_text(content, encoding="utf-8")


def _print_backends(cfg) -> None:
    for name, backend in cfg.backends.items():
        typer.echo(f"{name}:")
        typer.echo(f"  kind: {backend.kind.value}")
        typer.echo(f"  billing_mode: {backend.metadata.billing_mode.value}")
        typer.echo(f"  execution_location: {backend.metadata.execution_location.value}")
        typer.echo(f"  data_boundary: {backend.metadata.data_boundary.value}")
        typer.echo(f"  allow_network: {backend.metadata.allow_network}")
        typer.echo("  capabilities:")
        for key, value in backend.capabilities.model_dump().items():
            typer.echo(f"    {key}: {value}")


def _dump_spec_mapping(mapping: dict) -> dict:
    return {key: value.model_dump(mode="json") for key, value in mapping.items()}


def _print_spec_registry(registry) -> None:
    typer.echo("Built-in specs:")
    typer.echo(f"  model_profiles: {', '.join(registry.model_profiles)}")
    typer.echo(f"  tool_policies: {', '.join(registry.tool_policies)}")
    typer.echo(f"  memory_scopes: {', '.join(registry.memory_scopes)}")
    typer.echo(f"  agents: {', '.join(registry.agents)}")
    typer.echo(f"  workbenches: {', '.join(registry.workbenches)}")


def _print_agent_spec(agent) -> None:
    typer.echo(f"Agent: {agent.id}")
    typer.echo(f"Kind: {agent.kind.value}")
    typer.echo(f"Role: {agent.role}")
    typer.echo(f"Model profile: {agent.model_profile}")
    typer.echo(f"Tool policy: {agent.tool_policy}")
    typer.echo(f"Memory scope: {agent.memory_scope}")
    typer.echo(f"Parent: {agent.parent or 'none'}")
    typer.echo(f"Outputs: {', '.join(agent.outputs) if agent.outputs else 'none'}")
    typer.echo(f"Tags: {', '.join(agent.tags) if agent.tags else 'none'}")


def _print_workbench_spec(workbench) -> None:
    typer.echo(f"Workbench: {workbench.id}")
    typer.echo(f"Description: {workbench.description}")
    typer.echo(f"Default model profile: {workbench.default_model_profile}")
    typer.echo(f"Allowed agents: {', '.join(workbench.allowed_agents) if workbench.allowed_agents else 'none'}")
    typer.echo(
        "Approval policy: "
        f"{', '.join(f'{key}={value.value}' for key, value in workbench.approval_policy.items()) if workbench.approval_policy else 'none'}"
    )
    typer.echo(f"Forbidden actions: {', '.join(workbench.forbidden_actions) if workbench.forbidden_actions else 'none'}")


def _print_task(task) -> None:
    _print_section("Task")
    _print_kv("Task id", task.id)
    _print_kv("Title", task.title)
    _print_kv("Description", task.description)
    _print_kv("Status", task.status.value)
    _print_kv("Priority", task.priority)
    _print_section("Scope")
    _print_kv("Objective", task.objective_id or "none")
    _print_kv("Workbench", task.workbench_id or "none")
    _print_kv("Agent", task.agent_id or "none")
    _print_section("Gates")
    _print_kv("Depends on", ", ".join(task.depends_on) if task.depends_on else "none")
    _print_kv(
        "Required approvals",
        ", ".join(task.required_approvals) if task.required_approvals else "none",
    )
    _print_section("Execution")
    _print_kv("Run", task.run_id or "none")


def _print_objective(objective) -> None:
    typer.echo(f"Objective: {objective.id}")
    typer.echo(f"Title: {objective.title}")
    typer.echo(f"Description: {objective.description}")
    typer.echo(f"Status: {objective.status.value}")
    typer.echo(f"Priority: {objective.priority}")
    typer.echo(f"Workbench: {objective.workbench_id or 'none'}")


def _validate_objective_refs(workbench_id: str | None) -> None:
    registry = builtin_spec_registry()
    if workbench_id is not None:
        registry.get_workbench(workbench_id)


def _validate_task_spec_refs(project_root: Path, workbench_id: str | None, agent_id: str | None) -> tuple[str | None, Path | None]:
    registry = builtin_spec_registry()
    source_kind = "builtin" if workbench_id is not None else None
    source_path = None
    if workbench_id is not None:
        registry.get_workbench(workbench_id)
    if agent_id is not None:
        try:
            registry.get_agent(agent_id)
            source_kind = "builtin"
        except KeyError:
            try:
                project_agent = SQLiteStore(project_root).get_project_agent(agent_id)
            except KeyError as exc:
                raise KeyError(f"Agent not found: {agent_id}") from exc
            if workbench_id is not None and project_agent.workbench_id != workbench_id:
                raise ValueError(
                    f"Project agent {agent_id} belongs to workbench {project_agent.workbench_id}, "
                    f"not {workbench_id}"
                )
            source_kind = "project"
            source_path = project_agent.source_path
    return source_kind, source_path


def _resolve_memory_scope_id(
    store: SQLiteStore,
    project_root: Path,
    scope: MemoryScopeType,
    scope_id: str | None,
) -> str:
    registry = builtin_spec_registry()
    if scope == MemoryScopeType.PROJECT:
        return scope_id or str(project_root)
    if scope_id is None or not scope_id.strip():
        raise ValueError(f"--scope-id is required for {scope.value} memory scope.")
    if scope == MemoryScopeType.WORKBENCH:
        registry.get_workbench(scope_id)
        return scope_id
    if scope == MemoryScopeType.AGENT:
        try:
            registry.get_agent(scope_id)
        except KeyError:
            store.get_project_agent(scope_id)
        return scope_id
    if scope == MemoryScopeType.OBJECTIVE:
        store.get_objective(scope_id)
        return scope_id
    if scope == MemoryScopeType.TASK:
        store.get_task(scope_id)
        return scope_id
    raise ValueError(f"Unsupported memory scope: {scope.value}")


def _execution_task_metadata(execution_adapter: str | None, task_type: str | None) -> dict[str, str]:
    if execution_adapter is None and task_type is None:
        return {}
    supported = {
        ("dry_run", "phase_1a_test"),
        ("read_only_summary", "read_only_repo_summary"),
        ("codex_isolated_edit", "codex_code_edit"),
        ("repo_planning", "repo_planning"),
    }
    if (execution_adapter, task_type) not in supported:
        raise ValueError(
            "Unsupported execution metadata: supported pairs are "
            "dry_run/phase_1a_test, read_only_summary/read_only_repo_summary, "
            "codex_isolated_edit/codex_code_edit, and repo_planning/repo_planning"
        )
    return {"execution_adapter": execution_adapter or "", "task_type": task_type or ""}


def _emit_objective_error(schema_version: str, message: str, output: OutputFormat) -> None:
    if output == OutputFormat.JSON:
        _emit_json({"schema_version": schema_version, "ok": False, "errors": [message]})
    else:
        typer.echo(f"Objective command failed: {message}")


def _emit_task_error(schema_version: str, message: str, output: OutputFormat) -> None:
    if output == OutputFormat.JSON:
        _emit_json({"schema_version": schema_version, "ok": False, "errors": [message]})
    else:
        typer.echo(f"Task command failed: {message}")


def _emit_agent_authoring_error(
    schema_version: str,
    message: str,
    output: OutputFormat,
    *,
    source_path: str | None = None,
) -> None:
    if output == OutputFormat.JSON:
        payload = {"schema_version": schema_version, "ok": False, "errors": [message]}
        if source_path is not None:
            payload["source_path"] = source_path
        _emit_json(payload)
    else:
        typer.echo(f"Agent authoring command failed: {message}")


def _emit_policy_error(message: str, output: OutputFormat) -> None:
    if output == OutputFormat.JSON:
        _emit_json({"schema_version": "harness.effective_policy/v1", "ok": False, "errors": [message]})
    else:
        typer.echo(f"Policy command failed: {message}")


def _resolve_autonomy_profile_option(autonomous: bool, autonomy: str) -> str:
    profile = "safe-local" if autonomous and autonomy == "manual" else autonomy
    try:
        get_builtin_autonomy_policy(profile)
    except KeyError as exc:
        raise typer.BadParameter(str(exc).strip("'"), param_hint="--autonomy") from exc
    return profile


def _emit_autonomy_error(message: str, output: OutputFormat) -> None:
    if output == OutputFormat.JSON:
        _emit_json({"schema_version": "harness.autonomy_policy_inspect/v1", "ok": False, "errors": [message]})
    else:
        typer.echo(f"Autonomy command failed: {message}")


def _emit_artifact_error(schema_version: str, message: str, output: OutputFormat) -> None:
    if output == OutputFormat.JSON:
        _emit_json({"schema_version": schema_version, "ok": False, "errors": [message]})
    else:
        typer.echo(f"Artifact command failed: {message}")


def _emit_tool_error(message: str, output: OutputFormat) -> None:
    if output == OutputFormat.JSON:
        _emit_json({"schema_version": "harness.tool_capability/v1", "ok": False, "errors": [message]})
    else:
        typer.echo(f"Tool command failed: {message}")


def _emit_capability_error(message: str, project_root: Path, output: OutputFormat) -> None:
    if output == OutputFormat.JSON:
        _emit_json(
            {
                "schema_version": "harness.capability_catalog/v1",
                "ok": False,
                "project_root": str(project_root),
                "errors": [message],
            }
        )
    else:
        typer.echo(f"Capability command failed: {message}")


def _emit_sandbox_profile_error(message: str, project_root: Path, output: OutputFormat) -> None:
    if output == OutputFormat.JSON:
        _emit_json(
            {
                "schema_version": "harness.sandbox_profile/v1",
                "ok": False,
                "project_root": str(project_root),
                "errors": [message],
            }
        )
    else:
        typer.echo(f"Sandbox profile command failed: {message}")


def _emit_memory_error(message: str, output: OutputFormat) -> None:
    if output == OutputFormat.JSON:
        _emit_json({"schema_version": "harness.memory_record/v1", "ok": False, "errors": [message]})
    else:
        typer.echo(f"Memory command failed: {message}")


def _emit_progress_error(message: str, project_root: Path, objective_id: str, output: OutputFormat) -> None:
    if output == OutputFormat.JSON:
        _emit_json(
            {
                "schema_version": "harness.orchestration_progress/v1",
                "ok": False,
                "project_root": str(project_root),
                "objective_id": objective_id,
                "errors": [message],
            }
        )
    else:
        typer.echo(f"Progress command failed: {message}")


def _emit_compare_error(schema_version: str, message: str, output: OutputFormat) -> None:
    if output == OutputFormat.JSON:
        _emit_json({"schema_version": schema_version, "ok": False, "errors": [message]})
    else:
        typer.echo(f"Compare command failed: {message}")


def _emit_eval_error(message: str, output: OutputFormat) -> None:
    if output == OutputFormat.JSON:
        _emit_json({"schema_version": "harness.evals.safety_smoke/v1", "ok": False, "errors": [message]})
    else:
        typer.echo(f"Eval command failed: {message}")


def _emit_trace_error(message: str, output: OutputFormat) -> None:
    if output == OutputFormat.JSON:
        _emit_json({"schema_version": "harness.trace_export/v1", "ok": False, "errors": [message]})
    else:
        typer.echo(f"Trace command failed: {message}")


def _emit_daemon_error(schema_version: str, message: str, output: OutputFormat) -> None:
    if output == OutputFormat.JSON:
        _emit_json({"schema_version": schema_version, "ok": False, "errors": [message]})
    else:
        typer.echo(f"Daemon command failed: {message}")


def _emit_tui_home_error(message: str, output: OutputFormat) -> None:
    if output == OutputFormat.JSON:
        _emit_json({"schema_version": TUI_HOME_IMAGE_SCHEMA_VERSION, "ok": False, "errors": [message]})
    else:
        typer.echo(f"TUI home image command failed: {message}")


def _daemon_owner() -> str:
    return f"local_daemon:{socket.gethostname()}:{os.getpid()}"


def _print_compare_result(result: dict) -> None:
    typer.echo(f"Run A: {result['run_a']}")
    typer.echo(f"Run B: {result['run_b']}")
    typer.echo(f"Matches: {result['matches']}")
    changed = result.get("changed_sections", [])
    typer.echo(f"Changed sections: {', '.join(changed) if changed else 'none'}")


def _emit_json(payload) -> None:
    typer.echo(json.dumps(payload, indent=2, sort_keys=True))


def _print_section(title: str) -> None:
    typer.echo(f"\n{title}")


def _print_kv(label: str, value: object) -> None:
    typer.echo(f"  {label}: {value}")


def _print_tsv(headers: list[str]) -> None:
    _print_tsv_row(headers)


def _print_tsv_row(values: list[object]) -> None:
    typer.echo("\t".join(str(value) for value in values))


def _has_textual() -> bool:
    return importlib.util.find_spec("textual") is not None


def _run_unified_app(project_root: Path, *, codex_like: bool = False) -> None:
    if not _has_textual():
        typer.echo("Textual is not installed.")
        typer.echo(TUI_INSTALL_HINT)
        raise typer.Exit(code=1)
    from harness.tui import run_harness_app

    run_harness_app(project_root, codex_like=codex_like)


def _home_result(project_root: Path) -> dict:
    initialized = (project_root / HARNESS_DIR / "harness.sqlite").exists()
    result = {
        "schema_version": "harness.home/v1",
        "ok": True,
        "project_root": str(project_root),
        "initialized": initialized,
        "version": __version__,
        "summary": {
            "imported_agents": 0,
            "objectives": 0,
            "tasks_total": 0,
            "active_leases": 0,
            "active_daemons": 0,
            "recent_runs": 0,
        },
        "task_status_counts": {status.value: 0 for status in TaskStatus},
        "daemon": {
            "active_daemons": [],
            "paused_tasks": [],
            "latest_events": [],
        },
        "recent_runs": [],
        "safety_boundaries": [
            "local_first",
            "no_hosted_fallback",
            "no_paid_fallback",
            "no_openai_api_usage",
            "no_secret_exposure",
            "no_hidden_execution",
        ],
        "recommended_actions": [],
    }
    if not initialized:
        result["recommended_actions"] = [
            {
                "id": "initialize_project",
                "command": f"harness init --project {project_root}",
                "description": "Initialize local harness persistence for this project.",
            }
        ]
        return result

    store = SQLiteStore(project_root)
    try:
        agents = store.list_project_agents()
        objectives = store.list_objectives()
        tasks = store.list_tasks()
        leases = store.list_task_leases()
        runs = store.list_runs()[:5]
        daemon_status = store.daemon_status()
    except sqlite3.Error as exc:
        result["initialized"] = False
        result["state_error"] = {
            "type": exc.__class__.__name__,
            "message": str(exc),
        }
        result["recommended_actions"] = [
            {
                "id": "repair_project_state",
                "command": f"harness init --project {project_root}",
                "description": "Repair or migrate local harness persistence for this project.",
            }
        ]
        return result
    task_status_counts = {status.value: 0 for status in TaskStatus}
    for task in tasks:
        task_status_counts[task.status.value] = task_status_counts.get(task.status.value, 0) + 1
    active_leases = [lease for lease in leases if lease.status.value == "active"]
    result["summary"] = {
        "imported_agents": len(agents),
        "objectives": len(objectives),
        "tasks_total": len(tasks),
        "active_leases": len(active_leases),
        "active_daemons": len(daemon_status.active_daemons),
        "recent_runs": len(runs),
    }
    result["task_status_counts"] = task_status_counts
    result["daemon"] = {
        "active_daemons": [daemon.model_dump(mode="json") for daemon in daemon_status.active_daemons],
        "paused_tasks": daemon_status.paused_tasks,
        "latest_events": [event.model_dump(mode="json") for event in daemon_status.latest_events[:5]],
    }
    result["recent_runs"] = [run.model_dump(mode="json") for run in runs]
    if task_status_counts.get("ready", 0) > 0 and not active_leases:
        result["recommended_actions"].append(
            {
                "id": "lease_ready_task",
                "command": f"harness daemon run-once --project {project_root}",
                "description": "Lease the highest-priority eligible task without executing it.",
            }
        )
    if not agents:
        result["recommended_actions"].append(
            {
                "id": "author_agent",
                "command": "harness agents scaffold my_agent --workbench quant --kind specialist "
                "--parent quant_research --model-profile local_reasoning --tool-policy read_only "
                "--memory-scope quant --output agents/my_agent",
                "description": "Scaffold a declarative custom agent bundle.",
            }
        )
    return result


def _quickstart_agent_result(project_root: Path) -> dict:
    initialized = (project_root / HARNESS_DIR / "harness.sqlite").exists()
    agent_id = "my_agent"
    bundle_path = "agents/my_agent"
    project_arg = str(project_root)
    commands = [
        {
            "id": "scaffold_agent",
            "title": "Scaffold a declarative agent bundle",
            "command": "harness agents scaffold my_agent --workbench quant --kind specialist "
            "--parent quant_research --model-profile local_reasoning --tool-policy read_only "
            "--memory-scope quant --output agents/my_agent --output-format json",
            "description": "Creates agent.yaml and profiles/default.yaml at the explicit output path.",
            "mutates_when_run": True,
        },
        {
            "id": "validate_agent",
            "title": "Validate the bundle",
            "command": f"harness agents validate {bundle_path} --output json",
            "description": "Validates the explicit-path bundle against packaged built-ins.",
            "mutates_when_run": False,
        },
        {
            "id": "preview_agent",
            "title": "Preview effective agent metadata",
            "command": f"harness agents preview {bundle_path} --output json",
            "description": "Shows profiles, parent chain, and effective read-only metadata.",
            "mutates_when_run": False,
        },
        {
            "id": "init_project",
            "title": "Initialize harness persistence if needed",
            "command": f"harness init --project {project_arg}",
            "description": "Creates local harness project persistence.",
            "mutates_when_run": True,
            "skip_if_initialized": True,
        },
        {
            "id": "import_agent",
            "title": "Import the validated agent into this project",
            "command": f"harness agents import {bundle_path} --project {project_arg} --output json",
            "description": "Persists validated agent metadata into initialized harness persistence.",
            "mutates_when_run": True,
        },
        {
            "id": "inspect_agent",
            "title": "Inspect the imported agent",
            "command": f"harness agents inspect {agent_id} --project {project_arg} --output json",
            "description": "Reads imported agent metadata without execution.",
            "mutates_when_run": False,
        },
        {
            "id": "create_read_only_task",
            "title": "Create a read-only task for the imported agent",
            "command": 'harness tasks add --title "Read-only summary" '
            f"--agent {agent_id} --workbench quant --execution-adapter read_only_summary "
            f"--task-type read_only_repo_summary --project {project_arg} --output json",
            "description": "Creates a manual queue task using the bounded read-only adapter metadata.",
            "mutates_when_run": True,
        },
        {
            "id": "lease_task",
            "title": "Lease the next eligible task",
            "command": f"harness daemon run-once --project {project_arg} --output json",
            "description": "Selects and leases work; it does not execute the task.",
            "mutates_when_run": True,
        },
        {
            "id": "inspect_lease",
            "title": "Inspect the lease before execution",
            "command": f"harness daemon inspect-lease task_lease_... --project {project_arg} --output json",
            "description": "Replace task_lease_... with the lease id returned by daemon run-once.",
            "mutates_when_run": False,
        },
        {
            "id": "execute_read_only",
            "title": "Execute the bounded read-only adapter",
            "command": f"harness daemon execute-read-only task_lease_... --project {project_arg} --output json",
            "description": "Runs only the allowlisted read_only_summary/read_only_repo_summary adapter.",
            "mutates_when_run": True,
        },
    ]
    return {
        "schema_version": "harness.quickstart_agent/v1",
        "ok": True,
        "project_root": str(project_root),
        "initialized": initialized,
        "agent_id": agent_id,
        "bundle_path": bundle_path,
        "steps": commands,
        "safety_boundaries": [
            "quickstart_prints_only",
            "no_hidden_execution",
            "no_backend_preflight",
            "no_docker",
            "no_shell",
            "no_hosted_fallback",
            "no_paid_fallback",
            "no_openai_api_usage",
            "no_secret_exposure",
        ],
    }


def _doctor_result(project_root: Path, *, release: bool = False) -> dict:
    checks: list[dict] = []
    harness_dir = project_root / HARNESS_DIR
    config = None

    _add_check(
        checks,
        "initialized",
        "pass" if (harness_dir / "harness.sqlite").exists() else ("warn" if release else "fail"),
        "Harness SQLite state exists." if (harness_dir / "harness.sqlite").exists() else "Project is not initialized.",
        {"path": str(harness_dir / "harness.sqlite")},
    )

    try:
        config = load_config(project_root)
    except Exception as exc:
        _add_check(
            checks,
            "config_loadable",
            "warn" if release else "fail",
            "Harness config could not be loaded.",
            {"path": str(harness_dir / "config.yaml"), "error": str(exc)},
        )
    else:
        _add_check(
            checks,
            "config_loadable",
            "pass",
            "Harness config loaded.",
            {"path": str(harness_dir / "config.yaml")},
        )

    required_ignores = [".harness/runs/", ".harness/harness.sqlite", ".harness/approvals.yaml", ".harness/tmp/"]
    gitignore_path = project_root / ".gitignore"
    existing_ignores = gitignore_path.read_text(encoding="utf-8").splitlines() if gitignore_path.exists() else []
    missing_ignores = [entry for entry in required_ignores if entry not in existing_ignores]
    _add_check(
        checks,
        "local_artifact_ignores",
        "pass" if not missing_ignores else "warn",
        "Harness local artifact ignores are present." if not missing_ignores else "Some Harness local artifact ignores are missing.",
        {"path": str(gitignore_path), "missing": missing_ignores},
    )

    if release:
        if config is not None:
            _doctor_backend_descriptors(checks, config)
            _doctor_sandbox_safety(checks, config)
        _doctor_release_cli_metadata(checks)
        _doctor_release_runtime_state(checks, project_root)
    elif config is not None:
        _doctor_backend_descriptors(checks, config)
        if not release:
            _doctor_backend_preflight(checks, config)
            _doctor_docker_binary(checks)
            _doctor_dockerfile_validation(checks, project_root, config)
        _doctor_sandbox_safety(checks, config)

    return {
        "schema_version": "harness.doctor/v1",
        "project_root": str(project_root),
        "mode": "release" if release else "standard",
        "version": __version__,
        "ok": all(check["status"] != "fail" for check in checks),
        "checks": checks,
    }


def _add_check(checks: list[dict], check_id: str, status: str, message: str, details: dict | None = None) -> None:
    checks.append({"id": check_id, "status": status, "message": message, "details": details or {}})


def _doctor_backend_descriptors(checks: list[dict], config) -> None:
    try:
        descriptors = [backend.to_descriptor().model_dump(mode="json") for backend in config.backends.values()]
    except Exception as exc:
        _add_check(
            checks,
            "backend_descriptors",
            "fail",
            "Backend descriptors could not be built.",
            {"error": str(exc)},
        )
        return
    _add_check(
        checks,
        "backend_descriptors",
        "pass",
        "Backend descriptors are available.",
        {"backends": descriptors},
    )


def _doctor_backend_preflight(checks: list[dict], config) -> None:
    results = []
    for name, backend in config.backends.items():
        if name == "codex_cli":
            status = CodexCliBackend(backend).preflight()
            results.append(
                {
                    "name": name,
                    "status": "pass" if status.available else "warn",
                    "available": status.available,
                    "reason": status.reason,
                    "metadata": status.metadata.model_dump(mode="json"),
                    "detected_capabilities": status.capabilities.model_dump(mode="json"),
                }
            )
        elif name == "local_openai_compatible":
            status = LocalOpenAICompatibleBackend(backend).preflight()
            results.append(
                {
                    "name": name,
                    "status": "pass" if status.available else "warn",
                    "available": status.available,
                    "reason": status.reason,
                    "metadata": status.metadata.model_dump(mode="json"),
                    "detected_capabilities": status.capabilities.model_dump(mode="json"),
                }
            )
        else:
            results.append(
                {
                    "name": name,
                    "status": "warn",
                    "available": False,
                    "reason": "Paid backend preflight skipped; disabled by default.",
                    "metadata": backend.metadata.model_dump(mode="json"),
                    "detected_capabilities": backend.capabilities.model_dump(mode="json"),
                }
            )
    _add_check(
        checks,
        "backend_preflight",
        "pass" if all(item["status"] == "pass" for item in results) else "warn",
        "Backend preflight completed.",
        {"backends": results},
    )


def _doctor_release_cli_metadata(checks: list[dict]) -> None:
    codex_binary = shutil.which("codex")
    _add_check(
        checks,
        "codex_cli_metadata",
        "pass" if codex_binary else "warn",
        "Codex CLI binary is present." if codex_binary else "Codex CLI binary was not found on PATH.",
        {"binary": codex_binary, "preflight_performed": False},
    )
    docker_binary = shutil.which("docker")
    _add_check(
        checks,
        "docker_metadata",
        "pass" if docker_binary else "warn",
        "Docker binary is present for sandboxed test flows." if docker_binary else "Docker binary was not found on PATH.",
        {"binary": docker_binary, "version_check_performed": False},
    )
    descriptors = [descriptor.model_dump(mode="json") for descriptor in list_execution_adapter_descriptors()]
    _add_check(
        checks,
        "registered_adapters",
        "pass" if descriptors else "fail",
        "Registered execution adapters are available." if descriptors else "No registered execution adapters are available.",
        {"adapters": descriptors},
    )


def _doctor_release_runtime_state(checks: list[dict], project_root: Path) -> None:
    if not (project_root / HARNESS_DIR / "harness.sqlite").exists():
        _add_check(
            checks,
            "release_runtime_state",
            "warn",
            "Project is not initialized; runtime queue checks were skipped.",
            {"initialized": False},
        )
        return
    try:
        store = SQLiteStore(project_root)
        tasks = store.list_tasks()
        leases = store.list_task_leases()
        runs = store.list_runs()
    except sqlite3.Error as exc:
        _add_check(
            checks,
            "release_runtime_state",
            "fail",
            "Harness runtime state could not be inspected.",
            {"error": str(exc)},
        )
        return
    active_leases = [lease for lease in leases if lease.status.value == "active"]
    blocked_tasks = [task for task in tasks if task.status.value in {"blocked", "waiting_approval"}]
    failed_runs = [run for run in runs if run.status == "failed"]
    _add_check(
        checks,
        "release_runtime_state",
        "pass",
        "Harness runtime state inspected.",
        {
            "initialized": True,
            "tasks_total": len(tasks),
            "active_leases": [lease.model_dump(mode="json") for lease in active_leases[:5]],
            "blocked_or_waiting_tasks": [task.model_dump(mode="json") for task in blocked_tasks[:5]],
            "latest_failed_run": failed_runs[0].model_dump(mode="json") if failed_runs else None,
        },
    )


def _doctor_docker_binary(checks: list[dict]) -> None:
    docker_binary = shutil.which("docker")
    if docker_binary is None:
        _add_check(checks, "docker_binary", "warn", "Docker is not installed or not on PATH.", {})
        return
    try:
        version = subprocess.run([docker_binary, "--version"], text=True, capture_output=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        _add_check(checks, "docker_binary", "warn", "Docker version check failed.", {"error": str(exc)})
        return
    if version.returncode == 0:
        _add_check(
            checks,
            "docker_binary",
            "pass",
            "Docker is available.",
            {"binary": docker_binary, "version": version.stdout.strip()},
        )
    else:
        _add_check(
            checks,
            "docker_binary",
            "warn",
            "Docker is installed but unavailable.",
            {"binary": docker_binary, "stdout": version.stdout, "stderr": version.stderr},
        )


def _doctor_dockerfile_validation(checks: list[dict], project_root: Path, config) -> None:
    try:
        validation = DockerImageManager(project_root, config).validate_dockerfile()
    except ValueError as exc:
        _add_check(checks, "dockerfile_validation", "fail", "Dockerfile configuration is invalid.", {"error": str(exc)})
        return
    status = "pass" if validation.ok else "warn"
    _add_check(
        checks,
        "dockerfile_validation",
        status,
        "Dockerfile validation completed." if validation.ok else "Dockerfile validation reported issues.",
        validation.model_dump(mode="json"),
    )


def _doctor_sandbox_safety(checks: list[dict], config) -> None:
    issues = []
    if config.sandbox.network:
        issues.append("sandbox.network should be false by default.")
    _add_check(
        checks,
        "sandbox_safety",
        "pass" if not issues else "fail",
        "Sandbox safety settings are conservative." if not issues else "Sandbox safety settings need review.",
        {"issues": issues, "network": config.sandbox.network},
    )


def _obtain_hosted_boundary_approval(
    project_root: Path,
    task_type: str,
    approve_flag: bool,
) -> ApprovalProfile:
    disclosure = (
        "Hosted data-boundary approval required:\n"
        "  backend: codex_cli\n"
        "  billing mode: subscription\n"
        "  execution location: mixed\n"
        "  data boundary: hosted_provider\n"
        f"  task type: {task_type}\n"
        f"  project root: {project_root}\n"
        "  data that may be sent: task goal, project root, task type, and planning prompt context\n"
    )
    typer.echo(disclosure)
    if not approve_flag:
        approved = typer.confirm("Approve sending this planning context to Codex for this run?", default=False)
        if not approved:
            typer.echo("Hosted data-boundary approval denied.")
            raise typer.Exit(1)
    now = datetime.now(timezone.utc)
    return ApprovalProfile(
        id=f"one_time_codex_{int(now.timestamp())}",
        backend="codex_cli",
        project_root=str(project_root),
        data_boundary="hosted_provider",
        task_types=[task_type],
        expires_at=now + timedelta(minutes=30),
        created_at=now,
        reason="One-time CLI approval.",
    )


def _run_codex_direct_agent_cli(
    goal: str,
    project_root: Path,
    *,
    output: OutputFormat,
    model: str | None = None,
    reasoning_effort: str | None = None,
    stream: bool = True,
    fail_on_dirty: bool = False,
    cfg=None,
    store: SQLiteStore | None = None,
) -> dict:
    _require_initialized(project_root)
    cfg = cfg or load_config(project_root)
    store = store or SQLiteStore(project_root)
    backend = CodexCliBackend(cfg.backends["codex_cli"])
    runner = CodexDirectAgentRunner(project_root, store, backend)
    if output != OutputFormat.JSON:
        typer.echo("Codex foreground agent")
        _print_kv("Backend", backend.name)
        _print_kv("Model", model or backend.config.settings.get("model", ""))
        _print_kv("Sandbox", "workspace-write")
    try:
        result = runner.run(
            goal,
            model=model,
            reasoning_effort=reasoning_effort,
            stream=stream and output != OutputFormat.JSON,
            fail_on_dirty=fail_on_dirty,
            progress_callback=_print_codex_direct_progress if output != OutputFormat.JSON else None,
        )
    except (
        CodexUnavailable,
        CodexSandboxUnavailable,
        CodexEditCommandUnavailable,
        CodexDangerousFlagError,
        DirtyWorkspaceError,
    ) as exc:
        raise typer.BadParameter(str(exc)) from exc
    if output == OutputFormat.JSON:
        _emit_json({"schema_version": "harness.codex_direct_agent/v1", **result})
        return result
    typer.echo(f"Created run {result['run_id']}")
    typer.echo(f"Status: {result['status']}")
    typer.echo(f"Exit status: {result['exit_status']}")
    typer.echo(f"Approval mode: {result['approval_mode']}")
    typer.echo(f"Changed files: {', '.join(result['changed_files']) if result['changed_files'] else 'none'}")
    if result["diff_stat"].strip():
        _print_section("Diff Stat")
        typer.echo(result["diff_stat"].rstrip())
    _print_section("Final Codex Message")
    typer.echo(result["final_summary"])
    _print_section("Artifacts")
    for kind, path in result["artifacts"].items():
        typer.echo(f"  {kind}: {path}")
    _print_section("Next")
    typer.echo(f"  harness show {result['run_id']} --project {project_root}")
    return result


def _print_codex_direct_progress(event: dict) -> None:
    if event.get("type") == "event":
        summary = _codex_event_summary(event.get("event") or {})
        if summary:
            typer.echo(f"codex: {summary}")
    elif event.get("type") == "stdout":
        line = str(event.get("line") or "").strip()
        if line:
            typer.echo(f"codex: {line[:240]}")


def _codex_event_summary(event: dict) -> str:
    event_type = str(event.get("type") or event.get("event") or event.get("kind") or "").strip()
    text = _first_nested_text(event, keys={"summary", "text", "content", "message", "name", "tool", "command"})
    if event_type and text:
        return f"{event_type}: {text[:240]}"
    if text:
        return text[:240]
    if event_type:
        return event_type[:240]
    return ""


def _first_nested_text(value, *, keys: set[str]) -> str | None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in keys and isinstance(item, str) and item.strip():
                return item.strip()
        for item in value.values():
            found = _first_nested_text(item, keys=keys)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _first_nested_text(item, keys=keys)
            if found:
                return found
    return None


class CliPatchApprovalProvider:
    def decide(self, patch: str, summary: str) -> PatchApprovalDecision:
        typer.echo("Patch approval required:")
        typer.echo(summary)
        while True:
            choice = typer.prompt("Choose [a] approve once, [d] deny, [v] view full patch", default="d")
            normalized = choice.strip().lower()
            if normalized in {"a", "approve", "approve once"}:
                return PatchApprovalDecision(decision="approved")
            if normalized in {"d", "deny"}:
                return PatchApprovalDecision(decision="denied")
            if normalized in {"v", "view", "view full patch"}:
                typer.echo(patch)
                continue
            typer.echo("Invalid choice. Use a, d, or v.")


class CliApplyBackApprovalProvider:
    def decide(self, diff_summary: str, full_diff: str, diff_artifact: Path) -> ApplyBackDecision:
        typer.echo("Apply-back approval required:")
        typer.echo(diff_summary)
        while True:
            choice = typer.prompt("Choose [a] approve all changes, [d] deny all changes, [v] view full diff", default="d")
            normalized = choice.strip().lower()
            if normalized in {"a", "approve", "approve all", "approve all changes"}:
                return ApplyBackDecision(decision="approved")
            if normalized in {"d", "deny", "deny all", "deny all changes"}:
                return ApplyBackDecision(decision="denied")
            if normalized in {"v", "view", "view full diff"}:
                typer.echo(diff_artifact.read_text(encoding="utf-8") if diff_artifact.exists() else full_diff)
                continue
            typer.echo("Invalid choice. Use a, d, or v.")


class CliTestExecutionApprovalProvider:
    def decide(self, details: str) -> RunTestsDecision:
        typer.echo(details)
        while True:
            choice = typer.prompt("Choose [a] approve, [d] deny, [v] view details", default="d")
            normalized = choice.strip().lower()
            if normalized in {"a", "approve"}:
                return RunTestsDecision(decision="approved")
            if normalized in {"d", "deny"}:
                return RunTestsDecision(decision="denied")
            if normalized in {"v", "view", "view details"}:
                typer.echo(details)
                continue
            typer.echo("Invalid choice. Use a, d, or v.")


if __name__ == "__main__":
    app()
