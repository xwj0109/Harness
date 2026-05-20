from __future__ import annotations

import importlib.util
import hashlib
import json
import os
import re
import shutil
import sqlite3
import socket
import stat
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Annotated
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import click
import typer
from typer.core import TyperGroup

from harness.action_executors import execute_managed_action
from harness.action_policy import decide_managed_action
from harness.action_router import ManagedActionDecisionStatus, route_managed_action
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
from harness.command_catalog import build_command_catalog, command_action_unsupported
from harness.config import HARNESS_DIR, default_config, load_config, write_default_config
from harness.context_chunks import (
    rebuild_artifact_metadata_context_chunks,
    rebuild_memory_context_chunks,
    rebuild_repo_file_context_chunks,
)
from harness.core_service import HarnessCoreService
from harness.context_pack import pack_chat_context
from harness.context_policy import decide_context_transmission
from harness.context_retrieval import LexicalContextRetriever
from harness.context_vector import context_vector_index_health, rebuild_context_vector_index
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
from harness.intent_router import IntentRoute, route_instruction
from harness.isolation import ActiveRepoDirtyError
from harness.live_artifacts import write_live_run_artifacts
from harness.local_server import (
    _distribution_action_unsupported,
    _distribution_status_projection,
    _extensibility_status_projection,
    _dev_loop_status_projection,
    _desktop_action_unsupported,
    _desktop_status_projection,
    _mcp_resources_projection,
    _mcp_status_projection,
    _packaging_smoke_action_unsupported,
    _packaging_smoke_projection,
    _plugin_catalog,
    _pty_action_unsupported,
    _pty_restoration_readiness_projection,
    _pty_session_projection,
    _pty_shell_projection,
    _pty_terminal_tabs_projection,
    _pr_action_unsupported,
    _route_post,
    _server_dispose_unsupported,
    _server_lifecycle_projection,
    _server_mdns_projection,
    _skill_catalog,
    _session_changed_files_projection,
    _session_diff_projection,
    _session_revert_readiness_projection,
    _session_snapshots_projection,
    _version_check_projection,
    _web_client_projection,
    _web_open_unsupported,
    _worktree_action_unsupported,
    _web_tool_policy_projection,
    _worktree_projection,
    build_openapi_spec,
    generate_server_token,
    serve_local_http,
)
from harness.memory.sqlite_store import (
    REQUIRED_SESSION_SCHEMA_TABLES,
    SCHEMA_MIGRATIONS,
    SESSION_SCHEMA_REPAIR_MESSAGE,
    SQLiteStore,
    is_missing_session_schema_error,
)
from harness.model_catalog import catalog_projection_evidence, list_model_catalog, list_provider_catalog, validate_model_selection
from harness.models import (
    EventStreamType,
    KillSwitchTargetKind,
    MemoryScopeType,
    MemorySourceKind,
    RedactionState,
    RunEventType,
    SessionPermissionBoundaryKind,
    SessionPermissionScope,
    SessionPermissionSource,
    SessionPermissionStatus,
    SessionMessageRole,
    SessionMutationReversibility,
    SessionPartKind,
    SessionStatus,
    TaskStatus,
)
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
from harness.procedure_renderer import render_procedure_event
from harness.progress import build_orchestration_progress
from harness.registry import builtin_spec_registry
from harness.sandbox import CommandValidationError, DockerImageManager
from harness.sandbox_profiles import build_sandbox_profile_catalog, get_sandbox_profile
from harness.security_explanations import render_blocked_state
from harness.session_events import (
    SessionEventKind,
    append_session_event,
    read_session_events,
    render_session_event,
    session_transcript_path,
)
from harness.session_timeline import (
    list_session_timeline,
    list_session_transcript,
    render_timeline_event,
    render_transcript_entry,
    timeline_event_jsonl,
    transcript_entry_jsonl,
)
from harness.session_replay import build_session_replay_projection
from harness.session_share import build_local_session_share_snapshot, hosted_share_unsupported
from harness.session_cwd import CwdResolutionError, CwdResolver, cwd_recovery_message, session_cwd_from_metadata
from harness.session_tools import default_session_tool_descriptors, execute_session_tool, get_session_tool_descriptor
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
from harness.tui import build_tui_settings_catalog, normalize_tui_preferences
from harness.workspace_catalog import build_workspace_catalog, build_workspace_clients_projection, workspace_action_unsupported

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
providers_app = typer.Typer(help="Provider catalog metadata without credential disclosure.")
models_app = typer.Typer(help="Model catalog metadata without provider fallback.")
mcp_app = typer.Typer(help="MCP configuration diagnostics without connecting to servers.")
plugins_app = typer.Typer(help="Plugin discovery diagnostics without loading plugins.")
skills_app = typer.Typer(help="Skill discovery diagnostics without loading skill bodies.")
web_app = typer.Typer(
    help="Web fetch/search policy diagnostics and web-client launcher boundary.",
    invoke_without_command=True,
)
web_client_app = typer.Typer(help="Web client diagnostics without opening a browser.")
extensions_app = typer.Typer(help="Extensibility policy diagnostics without loading or connecting extensions.")
worktrees_app = typer.Typer(help="Git worktree diagnostics without creating, removing, or resetting worktrees.")
pty_app = typer.Typer(help="Managed PTY diagnostics without starting terminal processes.")
dev_loop_app = typer.Typer(help="Interactive development-loop diagnostics without terminal or workspace mutation.")
pr_app = typer.Typer(help="Pull request checkout/run helpers without network or git mutation.")
distribution_app = typer.Typer(help="Distribution and update diagnostics without modifying installation.")
settings_app = typer.Typer(help="Operator settings diagnostics and preference projections.")
commands_app = typer.Typer(help="Project command template discovery without execution.")
context_app = typer.Typer(help="Passive context inspection, chunk cache, and local index diagnostics.")
workspaces_app = typer.Typer(help="Workspace registry diagnostics without attach or sync.")
server_app = typer.Typer(help="Local server lifecycle diagnostics without process mutation.")
approvals_app = typer.Typer(help="Hosted data-boundary approval profiles.", invoke_without_command=True)
tests_app = typer.Typer(help="Docker-sandboxed test execution.")
tests_image_app = typer.Typer(help="Managed Docker test image helpers.")
specs_app = typer.Typer(help="Read-only built-in v0.2 spec inspection.", invoke_without_command=True)
specs_preview_app = typer.Typer(help="Read-only effective v0.2 spec policy previews.")
policy_app = typer.Typer(help="Runtime effective policy evidence.")
artifacts_app = typer.Typer(help="Run artifact evidence inspection.")
sessions_app = typer.Typer(help="Interactive session records.")
actions_app = typer.Typer(help="Self-managed local action routing and execution.")
runs_app = typer.Typer(help="Run listing and live event tailing.", invoke_without_command=True)
tools_app = typer.Typer(help="Harness tool capability descriptors.")
capabilities_app = typer.Typer(help="Read-only Harness capability catalog.")
sandbox_app = typer.Typer(help="Read-only sandbox profile descriptors.")
controls_app = typer.Typer(help="Local runtime execution controls.")
core_app = typer.Typer(help="Minimal headless core loop.")
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
app.add_typer(providers_app, name="providers")
app.add_typer(models_app, name="models")
app.add_typer(mcp_app, name="mcp")
app.add_typer(plugins_app, name="plugins")
app.add_typer(skills_app, name="skills")
app.add_typer(web_app, name="web")
app.add_typer(extensions_app, name="extensions")
app.add_typer(worktrees_app, name="worktrees")
app.add_typer(pty_app, name="pty")
app.add_typer(dev_loop_app, name="dev-loop")
app.add_typer(pr_app, name="pr")
app.add_typer(distribution_app, name="distribution")
app.add_typer(settings_app, name="settings")
app.add_typer(commands_app, name="commands")
app.add_typer(context_app, name="context")
app.add_typer(workspaces_app, name="workspaces")
app.add_typer(server_app, name="server")
app.add_typer(approvals_app, name="approvals")
app.add_typer(tests_app, name="tests")
app.add_typer(specs_app, name="specs")
app.add_typer(policy_app, name="policy")
app.add_typer(artifacts_app, name="artifacts")
app.add_typer(sessions_app, name="session")
app.add_typer(sessions_app, name="sessions")
app.add_typer(actions_app, name="actions")
app.add_typer(runs_app, name="runs")
app.add_typer(tools_app, name="tools")
app.add_typer(capabilities_app, name="capabilities")
app.add_typer(sandbox_app, name="sandbox")
app.add_typer(controls_app, name="controls")
app.add_typer(core_app, name="core")
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
web_app.add_typer(web_client_app, name="client")
specs_app.add_typer(specs_preview_app, name="preview")
autonomy_app.add_typer(autonomy_policy_app, name="policy")

ProjectOption = Annotated[Path, typer.Option("--project", help="Project root path.")]
TaskStatusArg = Annotated[TaskStatus, typer.Argument(help="Task status.")]


class OutputFormat(str, Enum):
    TEXT = "text"
    JSON = "json"


class ForegroundMode(str, Enum):
    AUTO = "auto"
    DIRECT = "direct"


class StreamFormat(str, Enum):
    HUMAN = "human"
    JSONL = "jsonl"
    NONE = "none"


OutputOption = Annotated[OutputFormat, typer.Option("--output", help="Output format.")]
SpecSourceOption = Annotated[str, typer.Option("--source", help="Spec source: builtin or explicit bundle path.")]
PolicySubjectKindOption = Annotated[
    str,
    typer.Option("--subject-kind", help="Policy subject kind: run, task, agent, workbench, or backend."),
]
PolicySubjectIdOption = Annotated[str, typer.Option("--subject-id", help="Policy subject id.")]
TraceFormatOption = Annotated[str, typer.Option("--format", help="Trace format. Only otel-json is supported.")]
TranscriptFormatOption = Annotated[str, typer.Option("--format", help="Transcript format: markdown or jsonl.")]

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
    mode: Annotated[
        ForegroundMode,
        typer.Option("--mode", help="Foreground execution mode. Use direct for active-workspace Codex."),
    ] = ForegroundMode.AUTO,
    fail_on_dirty: Annotated[
        bool,
        typer.Option("--fail-on-dirty", help="Refuse foreground prompt runs when git status is dirty."),
    ] = False,
    continue_session: Annotated[
        bool,
        typer.Option("--continue", help="Append this prompt to the most recently updated non-archived session."),
    ] = False,
    session_id: Annotated[str | None, typer.Option("--session", help="Append this prompt to an existing session id.")] = None,
    fork_session: Annotated[bool, typer.Option("--fork", help="Fork the selected session before running.")] = False,
    title: Annotated[str | None, typer.Option("--title", help="Title for a new or forked session.")] = None,
    agent_id: Annotated[str | None, typer.Option("--agent", help="Persist the selected agent id for this session.")] = None,
    files: Annotated[list[Path] | None, typer.Option("--file", help="Record a file attachment reference.")] = None,
    no_session: Annotated[
        bool,
        typer.Option("--no-session", help="Temporary compatibility path: run without session persistence."),
    ] = False,
) -> None:
    project_root = resolve_project_root(project)
    resolved_prompt, resolved_agent = _resolve_foreground_agent_selection(prompt, agent_id)
    if mode == ForegroundMode.AUTO and resolved_agent in _NATIVE_AGENT_ALIASES and not no_session:
        result = _run_native_agent_alias_session(
            resolved_prompt,
            project_root,
            agent_id=resolved_agent,
            output=output,
            model=model,
            session_id=session_id,
            continue_session=continue_session,
            fork_session=fork_session,
            title=title,
            file_refs=files or [],
        )
        raise typer.Exit(code=0 if result.get("ok") else 1)
    result = _run_codex_direct_agent_cli(
        resolved_prompt,
        project_root,
        output=output,
        model=model,
        reasoning_effort=reasoning_effort,
        stream=not no_stream,
        fail_on_dirty=fail_on_dirty,
        session_id=session_id,
        continue_session=continue_session,
        fork_session=fork_session,
        title=title,
        agent_id=resolved_agent,
        file_refs=files or [],
        no_session=no_session,
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
    if (project_root / HARNESS_DIR / "harness.sqlite").exists():
        store = SQLiteStore.open_initialized(project_root)
        session = store.create_session(metadata={"entrypoint": "chat"})
        append_session_event(
            project_root,
            session_id=session.id,
            event_type=SessionEventKind.SESSION_STARTED,
            message="Chat session started",
            payload={"entrypoint": "harness chat"},
        )
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


@context_app.command("inspect")
def context_inspect(
    query: Annotated[str | None, typer.Option("--query", help="Optional user turn for retrieval-aware context packing.")] = None,
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    """Inspect the packed chat context manifest without executing providers or tools."""

    project_root = resolve_project_root(project)
    manifest = pack_chat_context(project_root, query=query or "")
    payload = manifest.to_payload()
    payload["inspection"] = _context_cli_safety_payload(filesystem_modified=False)
    if output == OutputFormat.JSON:
        _emit_json(payload)
        return
    summary = payload.get("context_summary", {})
    role_counts = summary.get("role_counts") or payload.get("role_summary") or {}
    selected_count = int(summary.get("selected_block_count") or len(payload["blocks"]))
    role_parts = [f"{int(role_counts.get(role) or 0)} {role}" for role in ("pinned", "retrieved", "derived") if int(role_counts.get(role) or 0)]
    context_line = f"Context: {selected_count} selected {'block' if selected_count == 1 else 'blocks'}"
    if role_parts:
        context_line += ", " + ", ".join(role_parts)
    typer.echo(context_line)
    if summary.get("source_categories"):
        typer.echo(f"Sources: {', '.join(summary['source_categories'])}")
    token_budget = summary.get("token_budget") or payload.get("budget_report", {})
    if token_budget.get("used_input_tokens") is not None and token_budget.get("max_input_tokens") is not None:
        typer.echo(f"Budget: {int(token_budget['used_input_tokens']):,} / {int(token_budget['max_input_tokens']):,} tokens")
    if summary.get("retriever") or summary.get("selected_chunk_count"):
        typer.echo(f"Retrieval: {summary.get('retriever') or 'none'}, {int(summary.get('selected_chunk_count') or 0)} selected chunks")
    if summary.get("blocked_path_count"):
        typer.echo(f"Blocked paths: {summary['blocked_path_count']}")
    warnings = summary.get("warning_codes") or payload.get("warnings") or []
    if warnings:
        typer.echo(f"Warnings: {', '.join(warnings)}")
    selected_sources = list(summary.get("selected_sources") or [])
    if selected_sources:
        typer.echo("Selected sources:")
        for item in selected_sources[:12]:
            status = ",".join(item.get("status") or []) or "selected"
            typer.echo(
                f"- {item.get('kind')} {item.get('source')} "
                f"role={item.get('role')} tokens={item.get('token_estimate')} status={status}"
            )


@context_app.command("estimate")
def context_estimate(
    query: Annotated[str, typer.Argument(help="User turn to estimate against the current context pipeline.")] = "",
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    """Estimate packed context budget use without executing providers or mutating state."""

    project_root = resolve_project_root(project)
    manifest = pack_chat_context(project_root, query=query)
    report = manifest.budget_report.to_payload()
    payload = {
        "schema_version": "harness.context_estimate/v1",
        "project_root": str(project_root),
        "query_present": bool(query.strip()),
        "budget_report": report,
        "role_summary": dict(manifest.role_summary),
        "warnings": list(manifest.warnings),
        "inspection": _context_cli_safety_payload(filesystem_modified=False),
    }
    if output == OutputFormat.JSON:
        _emit_json(payload)
        return
    typer.echo(f"Tokenizer: {report['tokenizer']}")
    typer.echo(f"Used input tokens: {report['used_input_tokens']}")
    typer.echo(f"Approximate: {report['approximate']}")


@context_app.command("chunks")
def context_chunks(
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    """List cached context chunks without rebuilding or indexing."""

    project_root = resolve_project_root(project)
    db_path = project_root / HARNESS_DIR / "harness.sqlite"
    chunks = []
    if db_path.exists():
        chunks = SQLiteStore(project_root).list_context_chunks()
    payload = {
        "schema_version": "harness.context_chunks_list/v1",
        "project_root": str(project_root),
        "count": len(chunks),
        "chunks": [_context_chunk_payload(chunk) for chunk in chunks],
        "inspection": _context_cli_safety_payload(filesystem_modified=False),
    }
    if output == OutputFormat.JSON:
        _emit_json(payload)
        return
    typer.echo(f"Context chunks: {len(chunks)}")
    for chunk in chunks[:20]:
        label = chunk.path or chunk.memory_id or chunk.artifact_id or chunk.source_id or chunk.id
        typer.echo(f"- {chunk.source_kind.value}\t{label}\t{chunk.start_line or ''}-{chunk.end_line or ''}")


@context_app.command("search")
def context_search(
    query: Annotated[str, typer.Argument(help="Local lexical query over cached context chunks.")],
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
    limit: Annotated[int, typer.Option("--limit", help="Maximum retrieved chunks.")] = 8,
) -> None:
    """Search cached chunks locally without rebuilding caches or executing tools."""

    project_root = resolve_project_root(project)
    results = LexicalContextRetriever(project_root).retrieve(query, limit=limit)
    payload = {
        "schema_version": "harness.context_search/v1",
        "project_root": str(project_root),
        "query": query,
        "retriever": "lexical_context_chunks",
        "count": len(results),
        "results": [result.to_manifest_ref() for result in results],
        "inspection": _context_cli_safety_payload(filesystem_modified=False),
    }
    if output == OutputFormat.JSON:
        _emit_json(payload)
        return
    typer.echo(f"Results: {len(results)}")
    for result in results:
        chunk = result.chunk
        label = chunk.path or chunk.memory_id or chunk.artifact_id or chunk.id
        typer.echo(f"{result.rank}\t{result.score:.3f}\t{label}")


@context_app.command("policy")
def context_policy(
    destination: Annotated[str, typer.Argument(help="Destination such as local_sqlite, hosted_embedding, or qdrant.")],
    source_kind: Annotated[str | None, typer.Option("--source-kind", help="Optional context source kind.")] = None,
    trust_level: Annotated[str | None, typer.Option("--trust-level", help="Optional context trust level.")] = None,
    path: Annotated[str | None, typer.Option("--path", help="Optional context path.")] = None,
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    """Explain fail-closed context transmission policy for a destination."""

    decision = decide_context_transmission(destination, source_kind=source_kind, trust_level=trust_level, path=path)
    payload = decision.to_payload()
    payload["inspection"] = _context_cli_safety_payload(filesystem_modified=False)
    if output == OutputFormat.JSON:
        _emit_json(payload)
        return
    typer.echo(f"Destination: {destination}")
    typer.echo(f"Allowed: {decision.allowed}")
    typer.echo(f"Code: {decision.code}")
    typer.echo(f"Reason: {decision.reason}")


@context_app.command("rebuild-chunks")
def context_rebuild_chunks(
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    """Explicitly rebuild local context chunks from repo files, memory summaries, and artifact metadata."""

    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    store = SQLiteStore(project_root)
    repo_chunks = rebuild_repo_file_context_chunks(project_root, store=store)
    memory_chunks = rebuild_memory_context_chunks(project_root, store=store)
    artifact_chunks = []
    for run in store.list_runs()[:50]:
        artifact_chunks.extend(rebuild_artifact_metadata_context_chunks(project_root, run.id, store=store))
    payload = {
        "schema_version": "harness.context_rebuild_chunks/v1",
        "project_root": str(project_root),
        "repo_chunks": len(repo_chunks),
        "memory_chunks": len(memory_chunks),
        "artifact_metadata_chunks": len(artifact_chunks),
        "inspection": _context_cli_safety_payload(filesystem_modified=True),
    }
    if output == OutputFormat.JSON:
        _emit_json(payload)
        return
    typer.echo(f"Repo chunks: {len(repo_chunks)}")
    typer.echo(f"Memory chunks: {len(memory_chunks)}")
    typer.echo(f"Artifact metadata chunks: {len(artifact_chunks)}")


@context_app.command("rebuild-index")
def context_rebuild_index(project: ProjectOption = Path("."), output: OutputOption = OutputFormat.TEXT) -> None:
    """Explicitly rebuild the derived local vector index from cached context chunks."""

    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    store = SQLiteStore(project_root)
    records = rebuild_context_vector_index(project_root, store=store)
    health = context_vector_index_health(project_root, store=store).to_payload()
    payload = {
        "schema_version": "harness.context_rebuild_index/v1",
        "project_root": str(project_root),
        "records": len(records),
        "health": health,
        "inspection": _context_cli_safety_payload(filesystem_modified=True),
    }
    if output == OutputFormat.JSON:
        _emit_json(payload)
        return
    typer.echo(f"Vector records: {len(records)}")
    typer.echo(f"Missing: {health['missing_count']} stale: {health['stale_count']} orphan: {health['orphan_count']}")


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


@runs_app.callback(invoke_without_command=True)
def runs(
    ctx: typer.Context,
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    if ctx.invoked_subcommand is not None:
        return
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


@runs_app.command("tail")
def runs_tail(
    run_id: str,
    project: ProjectOption = Path("."),
    jsonl: Annotated[bool, typer.Option("--jsonl", help="Emit raw JSONL events.")] = False,
    follow: Annotated[bool, typer.Option("--follow", "-f", help="Follow until the run reaches a terminal state.")] = False,
) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    store = SQLiteStore(project_root)
    _tail_run_events(store, run_id, jsonl=jsonl, follow=follow)


@app.command("events")
def events_command(
    run_or_session_id: str,
    project: ProjectOption = Path("."),
    jsonl: Annotated[bool, typer.Option("--jsonl", help="Emit raw JSONL events.")] = False,
    follow: Annotated[bool, typer.Option("--follow", "-f", help="Follow until the run or session reaches a terminal state.")] = False,
) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    store = SQLiteStore(project_root)
    try:
        store.get_session(run_or_session_id)
    except KeyError:
        _tail_run_events(store, run_or_session_id, jsonl=jsonl, follow=follow)
        return
    _tail_session_events(store, run_or_session_id, jsonl=jsonl, follow=follow, limit=None)


@app.command("transcript")
def transcript_command(
    run_id: str,
    project: ProjectOption = Path("."),
    format: TranscriptFormatOption = "markdown",
) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    store = SQLiteStore(project_root)
    paths = write_live_run_artifacts(store, run_id)
    if format == "jsonl":
        typer.echo(paths["transcript"].read_text(encoding="utf-8"), nl=False)
        return
    if format != "markdown":
        raise typer.BadParameter("Transcript format must be markdown or jsonl.")
    typer.echo(paths["procedure"].read_text(encoding="utf-8"), nl=False)


@app.command("summary")
def summary_command(run_id: str, project: ProjectOption = Path(".")) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    store = SQLiteStore(project_root)
    paths = write_live_run_artifacts(store, run_id)
    typer.echo(paths["final_report"].read_text(encoding="utf-8"), nl=False)


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
    repair: Annotated[
        bool,
        typer.Option("--repair", help="Run SQLite migrations and additive schema repair before checking state."),
    ] = False,
) -> None:
    project_root = resolve_project_root(project)
    result = _doctor_result(project_root, release=release, repair=repair)
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


@tasks_app.command("run")
def tasks_run(
    task_id: str,
    live: Annotated[bool, typer.Option("--live", help="Create a live run for this task.")] = False,
    project: ProjectOption = Path("."),
    stream: Annotated[StreamFormat, typer.Option("--stream", help="Live stream format.")] = StreamFormat.HUMAN,
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    store = SQLiteStore(project_root)
    try:
        task = store.get_task(task_id)
    except KeyError as exc:
        _emit_task_error("harness.task_run/v1", str(exc).strip("'"), output)
        raise typer.Exit(code=1) from exc
    if not live:
        _emit_task_error("harness.task_run/v1", "Only --live task runs are supported by this command.", output)
        raise typer.Exit(code=1)
    task_type = str(task.metadata.get("task_type") or "codex_code_edit")
    goal = task.title if not task.description else f"{task.title}\n\n{task.description}"
    result = _create_policy_first_live_run(
        project_root=project_root,
        goal=goal,
        task_type=task_type,
        agent=task.agent_id or "code_editor",
        task_id=task.id,
        task_file=None,
    )
    if output == OutputFormat.JSON:
        _emit_json({"schema_version": "harness.task_run/v1", "ok": True, **result})
        return
    _emit_live_stream(project_root, result["run_id"], stream)
    if stream != StreamFormat.JSONL:
        typer.echo(f"Run: {result['run_id']}")
        typer.echo(f"Status: {result['status']}")


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


@agents_app.command("generate")
def agents_generate(
    agent_id: str,
    description: Annotated[str, typer.Option("--description", help="Natural-language agent description.")],
    output_path: Annotated[Path, typer.Option("--output", help="Destination agent bundle directory.")],
    workbench: Annotated[str, typer.Option("--workbench", help="Built-in workbench id.")] = "coding",
    kind: Annotated[str, typer.Option("--kind", help="Agent kind.")] = "specialist",
    model_profile: Annotated[str, typer.Option("--model-profile", help="Built-in model profile id.")] = "codex_supervised",
    tool_policy: Annotated[str, typer.Option("--tool-policy", help="Built-in tool policy id.")] = "read_only",
    memory_scope: Annotated[str, typer.Option("--memory-scope", help="Built-in memory scope id.")] = "project",
    parent: Annotated[str | None, typer.Option("--parent", help="Optional built-in group parent id.")] = None,
    output_format: Annotated[OutputFormat, typer.Option("--output-format", help="Output format.")] = OutputFormat.TEXT,
) -> None:
    role = description.strip()
    if not role:
        raise typer.BadParameter("--description must not be empty.")
    try:
        scaffold = scaffold_agent_bundle(
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
            "harness.agent_generate/v1",
            str(exc).strip("'"),
            output_format,
            source_path=str(output_path.expanduser().resolve()),
        )
        raise typer.Exit(code=1) from exc
    result = {
        "schema_version": "harness.agent_generate/v1",
        "ok": True,
        "agent_id": agent_id,
        "source_path": scaffold["source_path"],
        "workbench_id": scaffold["workbench_id"],
        "generated_from_description": True,
        "description": role,
        "defaults": {
            "kind": kind,
            "model_profile": model_profile,
            "tool_policy": tool_policy,
            "memory_scope": memory_scope,
            "parent": parent,
        },
        "scaffold": scaffold,
        "provider_execution_started": False,
        "model_execution_started": False,
        "hidden_provider_fallback": False,
        "permission_granting": False,
        "authority_granting": False,
    }
    if output_format == OutputFormat.JSON:
        _emit_json(result)
        return
    typer.echo(f"Agent bundle generated: {result['source_path']}")
    typer.echo(f"Agent: {result['agent_id']}")
    typer.echo(f"Workbench: {result['workbench_id']}")
    typer.echo("Defaults:")
    for key, value in result["defaults"].items():
        typer.echo(f"  {key}: {value or 'none'}")


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


@app.command("route")
def route(
    instruction: Annotated[str, typer.Argument(help="Natural-language instruction to route.")],
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    """Inspect the deterministic product route for an instruction."""

    resolve_project_root(project)
    resolved = route_instruction(instruction)
    payload = {
        "schema_version": "harness.route/v1",
        "ok": resolved.intent != "unsupported",
        "route": resolved.model_dump(mode="json"),
    }
    if output == OutputFormat.JSON:
        _emit_json(payload)
        return
    typer.echo(f"Intent: {resolved.intent}")
    typer.echo(f"Confidence: {resolved.confidence}")
    typer.echo(f"Workbench: {resolved.workbench_id}")
    typer.echo(f"Agent: {resolved.agent_id}")
    typer.echo(f"Mode: {resolved.mode.value}")
    typer.echo(f"Task type: {resolved.task_type}")
    typer.echo(f"Backend: {resolved.default_backend}")
    typer.echo(
        "Approvals: "
        f"{', '.join(resolved.required_approvals) if resolved.required_approvals else 'none'}"
    )
    typer.echo(
        "Expected outputs: "
        f"{', '.join(resolved.expected_outputs) if resolved.expected_outputs else 'none'}"
    )


@sessions_app.command("list")
def sessions_list(
    status: Annotated[SessionStatus | None, typer.Option("--status", help="Filter by session status.")] = None,
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    sessions = SQLiteStore(project_root).list_sessions(status.value if status is not None else None)
    if output == OutputFormat.JSON:
        _emit_json(
            {
                "schema_version": "harness.sessions/v1",
                "ok": True,
                "sessions": [session.model_dump(mode="json") for session in sessions],
            }
        )
        return
    if not sessions:
        typer.echo("No sessions found.")
        return
    _print_tsv(["session_id", "status", "intent", "run", "task", "updated_at"])
    for session in sessions:
        _print_tsv_row(
            [
                session.id,
                session.status.value,
                session.intent or "none",
                session.active_run_id or "none",
                session.active_task_id or "none",
                session.updated_at.isoformat(),
            ]
        )


@sessions_app.command("inspect")
def sessions_inspect(
    session_id: str,
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    store = SQLiteStore(project_root)
    try:
        session = store.get_session(session_id)
    except KeyError as exc:
        _emit_session_error(str(exc).strip("'"), output)
        raise typer.Exit(code=1) from exc
    transcript = session_transcript_path(project_root, session.id)
    events = read_session_events(project_root, session.id)
    latest_ui_activation = _latest_session_ui_activation(store, session.id)
    model_validation = _session_model_validation(load_config(project_root), session)
    payload = {
        "schema_version": "harness.session/v1",
        "ok": True,
        "session": session.model_dump(mode="json"),
        "transcript_path": str(transcript),
        "event_count": len(events),
        "latest_ui_activation": latest_ui_activation,
        "model_validation": model_validation,
        "next_actions": _session_next_actions(session),
    }
    if output == OutputFormat.JSON:
        _emit_json(payload)
        return
    _print_section("Session")
    _print_kv("Session id", session.id)
    _print_kv("Status", session.status.value)
    _print_kv("Intent", session.intent or "none")
    _print_kv("Objective", session.objective_id or "none")
    _print_kv("Task", session.active_task_id or "none")
    _print_kv("Run", session.active_run_id or "none")
    _print_kv("Transcript", transcript)
    if model_validation:
        _print_kv("Model", model_validation["raw_model_ref"] or "default")
        _print_kv("Model executable", model_validation["executable"])
        if model_validation["blocked_reasons"]:
            _print_kv("Model blocked", ", ".join(model_validation["blocked_reasons"]))
        _print_kv("No hidden fallback", model_validation["no_hidden_fallback"])
    if latest_ui_activation:
        _print_kv(
            "Latest UI action",
            f"{latest_ui_activation['entry_id']} action={latest_ui_activation['action_type']} source={latest_ui_activation['source']}",
        )
        _print_kv(
            "UI action flags",
            (
                f"command={latest_ui_activation['command_started']} "
                f"process={latest_ui_activation['process_started']} "
                f"filesystem={latest_ui_activation['filesystem_modified']} "
                f"permission={latest_ui_activation['permission_granting']} "
                f"authority={latest_ui_activation['authority_granting']}"
            ),
        )
    _print_section("Next")
    for action in payload["next_actions"]:
        typer.echo(action)


@sessions_app.command("get")
def sessions_get(
    session_id: str,
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    sessions_inspect(session_id=session_id, project=project, output=output)


@sessions_app.command("status")
def sessions_status(
    session_id: str,
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    store = SQLiteStore(project_root)
    try:
        session = store.get_session(session_id)
    except KeyError as exc:
        _emit_session_error(str(exc).strip("'"), output)
        raise typer.Exit(code=1) from exc
    events = store.list_session_store_events(session.id)
    messages = store.list_session_messages(session.id)
    children = store.list_child_sessions(session.id)
    latest_ui_activation = _latest_session_ui_activation(store, session.id)
    model_validation = _session_model_validation(load_config(project_root), session)
    payload = {
        "schema_version": "harness.session_status/v1",
        "ok": True,
        "session_id": session.id,
        "status": session.status.value,
        "title": session.title,
        "active_run_id": session.active_run_id,
        "active_task_id": session.active_task_id,
        "objective_id": session.objective_id,
        "summary": session.summary,
        "token_input": session.token_input,
        "token_output": session.token_output,
        "token_reasoning": session.token_reasoning,
        "token_cache_read": session.token_cache_read,
        "token_cache_write": session.token_cache_write,
        "estimated_cost_usd": str(session.estimated_cost_usd) if session.estimated_cost_usd is not None else None,
        "message_count": len(messages),
        "event_count": len(events),
        "latest_ui_activation": latest_ui_activation,
        "model_validation": model_validation,
        "child_session_ids": [child.id for child in children],
        "terminal": session.status
        in {SessionStatus.COMPLETED, SessionStatus.FAILED, SessionStatus.CANCELLED, SessionStatus.ARCHIVED},
        "process_running": False,
        "permission_granting": False,
    }
    if output == OutputFormat.JSON:
        _emit_json(payload)
        return
    _print_kv("Session id", payload["session_id"])
    _print_kv("Status", payload["status"])
    _print_kv("Summary", payload["summary"] or "none")
    _print_kv("Messages", payload["message_count"])
    _print_kv("Events", payload["event_count"])
    if model_validation:
        _print_kv("Model", model_validation["raw_model_ref"] or "default")
        _print_kv("Model executable", model_validation["executable"])
        if model_validation["blocked_reasons"]:
            _print_kv("Model blocked", ", ".join(model_validation["blocked_reasons"]))
        _print_kv("No hidden fallback", model_validation["no_hidden_fallback"])
    if latest_ui_activation:
        _print_kv(
            "Latest UI action",
            f"{latest_ui_activation['entry_id']} action={latest_ui_activation['action_type']} source={latest_ui_activation['source']}",
        )
        _print_kv(
            "UI action flags",
            (
                f"command={latest_ui_activation['command_started']} "
                f"process={latest_ui_activation['process_started']} "
                f"filesystem={latest_ui_activation['filesystem_modified']} "
                f"permission={latest_ui_activation['permission_granting']} "
                f"authority={latest_ui_activation['authority_granting']}"
            ),
        )
    _print_kv("Children", ",".join(payload["child_session_ids"]) if payload["child_session_ids"] else "none")


@sessions_app.command("model")
def sessions_model(
    session_id: str,
    raw_model_ref: Annotated[str, typer.Argument(help="Explicit provider/model ref, for example codex_cli/gpt-5.5.")],
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    cfg = load_config(project_root)
    store = SQLiteStore(project_root)
    try:
        store.get_session(session_id)
    except KeyError as exc:
        _emit_session_error(str(exc).strip("'"), output)
        raise typer.Exit(code=1) from exc
    parsed_model = _parse_model_ref(raw_model_ref)
    session = store.update_session_model(
        session_id,
        raw_model_ref=raw_model_ref,
        provider_id=parsed_model["provider_id"],
        model_id=parsed_model["model_id"],
        model_variant=parsed_model["model_variant"],
    )
    validation = validate_model_selection(cfg, raw_model_ref)
    validation_payload = validation.model_dump(mode="json")
    store.append_store_event(
        EventStreamType.SESSION,
        session.id,
        "session.model_validation",
        {
            **validation_payload,
            "source": "session_model_command",
            "summary": "Model selection validated." if validation.executable else "Model selection blocked before execution.",
            "provider_execution_started": False,
            "model_execution_started": False,
            "hidden_provider_fallback": False,
            "hidden_model_fallback": False,
            "no_hidden_fallback": True,
            "permission_granting": False,
            "authority_granting": False,
        },
        session_id=session.id,
        redaction_state=RedactionState.NOT_REQUIRED,
    )
    payload = {
        "schema_version": "harness.session_model_selection/v1",
        "ok": validation.executable,
        "session": store.get_session(session.id).model_dump(mode="json"),
        "model_validation": validation_payload,
        "provider_execution_started": False,
        "model_execution_started": False,
        "hidden_provider_fallback": False,
        "hidden_model_fallback": False,
        "no_hidden_fallback": True,
        "permission_granting": False,
        "authority_granting": False,
    }
    if output == OutputFormat.JSON:
        _emit_json(payload)
        if not validation.executable:
            raise typer.Exit(code=1)
        return
    typer.echo(f"Session: {session.id}")
    typer.echo(f"Model: {raw_model_ref}")
    typer.echo(f"Executable: {validation.executable}")
    if validation.blocked_reasons:
        typer.echo(f"Blocked: {', '.join(validation.blocked_reasons)}")
    typer.echo("Model selection was persisted as metadata only; no provider call or fallback was performed.")
    if not validation.executable:
        raise typer.Exit(code=1)


@sessions_app.command("children")
def sessions_children(
    session_id: str,
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    store = SQLiteStore(project_root)
    try:
        parent = store.get_session(session_id)
        children = store.list_child_sessions(session_id)
    except KeyError as exc:
        _emit_session_error(str(exc).strip("'"), output)
        raise typer.Exit(code=1) from exc
    payload = {
        "schema_version": "harness.session_children/v1",
        "ok": True,
        "session_id": parent.id,
        "children": [child.model_dump(mode="json") for child in children],
        "child_session_ids": [child.id for child in children],
        "execution_started": False,
        "permission_granting": False,
    }
    if output == OutputFormat.JSON:
        _emit_json(payload)
        return
    if not children:
        typer.echo("No child sessions found.")
        return
    _print_tsv(["session_id", "status", "title", "forked_from_message", "updated_at"])
    for child in children:
        _print_tsv_row(
            [
                child.id,
                child.status.value,
                child.title or "",
                child.forked_from_message_id or "",
                child.updated_at.isoformat(),
            ]
        )


@sessions_app.command("summarize")
def sessions_summarize(
    session_id: str,
    summary: Annotated[str | None, typer.Option("--summary", help="Persist an operator/model-visible session summary.")] = None,
    token_input: Annotated[int | None, typer.Option("--input-tokens", help="Input token rollup.")] = None,
    token_output: Annotated[int | None, typer.Option("--output-tokens", help="Output token rollup.")] = None,
    token_reasoning: Annotated[int | None, typer.Option("--reasoning-tokens", help="Reasoning token count rollup.")] = None,
    token_cache_read: Annotated[int | None, typer.Option("--cache-read-tokens", help="Cache read token count rollup.")] = None,
    token_cache_write: Annotated[int | None, typer.Option("--cache-write-tokens", help="Cache write token count rollup.")] = None,
    estimated_cost_usd: Annotated[str | None, typer.Option("--estimated-cost-usd", help="Estimated cost rollup.")] = None,
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    store = SQLiteStore(project_root)
    try:
        session = store.update_session_summary(
            session_id,
            summary=summary,
            token_input=token_input,
            token_output=token_output,
            token_reasoning=token_reasoning,
            token_cache_read=token_cache_read,
            token_cache_write=token_cache_write,
            estimated_cost_usd=estimated_cost_usd,
        )
    except (KeyError, ValueError) as exc:
        _emit_session_error(str(exc).strip("'"), output)
        raise typer.Exit(code=1) from exc
    payload = {
        "schema_version": "harness.session_summary/v1",
        "ok": True,
        "session": session.model_dump(mode="json"),
        "mutable_projection": True,
        "permission_granting": False,
    }
    if output == OutputFormat.JSON:
        _emit_json(payload)
        return
    _print_kv("Session id", session.id)
    _print_kv("Summary", session.summary or "none")
    _print_kv("Input tokens", session.token_input)
    _print_kv("Output tokens", session.token_output)
    _print_kv("Estimated cost USD", session.estimated_cost_usd or "none")


@sessions_app.command("abort")
def sessions_abort(
    session_id: str,
    reason: Annotated[str | None, typer.Option("--reason", help="Operator-visible abort reason.")] = None,
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    store = SQLiteStore(project_root)
    try:
        session = store.cancel_session(session_id, reason=reason)
    except KeyError as exc:
        _emit_session_error(str(exc).strip("'"), output)
        raise typer.Exit(code=1) from exc
    payload = {
        "schema_version": "harness.session_abort/v1",
        "ok": True,
        "session": session.model_dump(mode="json"),
        "process_stopped": False,
        "run_cancelled": False,
        "task_cancelled": False,
        "permission_granting": False,
    }
    if output == OutputFormat.JSON:
        _emit_json(payload)
        return
    typer.echo(f"Cancelled session {session.id}. No process was stopped by this metadata-only abort.")


@sessions_app.command("archive")
def sessions_archive(
    session_id: str,
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    store = SQLiteStore(project_root)
    try:
        session = store.archive_session(session_id)
    except KeyError as exc:
        _emit_session_error(str(exc).strip("'"), output)
        raise typer.Exit(code=1) from exc
    payload = {"schema_version": "harness.session_archive/v1", "ok": True, "session": session.model_dump(mode="json")}
    if output == OutputFormat.JSON:
        _emit_json(payload)
        return
    typer.echo(f"Archived session {session.id}.")


@sessions_app.command("restore")
def sessions_restore(
    session_id: str,
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    store = SQLiteStore(project_root)
    try:
        session = store.restore_session(session_id)
    except KeyError as exc:
        _emit_session_error(str(exc).strip("'"), output)
        raise typer.Exit(code=1) from exc
    payload = {"schema_version": "harness.session_restore/v1", "ok": True, "session": session.model_dump(mode="json")}
    if output == OutputFormat.JSON:
        _emit_json(payload)
        return
    typer.echo(f"Restored session {session.id}.")


@sessions_app.command("delete")
def sessions_delete(
    session_id: str,
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    store = SQLiteStore(project_root)
    try:
        session = store.delete_session(session_id)
    except KeyError as exc:
        _emit_session_error(str(exc).strip("'"), output)
        raise typer.Exit(code=1) from exc
    payload = {
        "schema_version": "harness.session_delete/v1",
        "ok": True,
        "destructive": False,
        "behavior": "archive",
        "session": session.model_dump(mode="json"),
    }
    if output == OutputFormat.JSON:
        _emit_json(payload)
        return
    typer.echo(f"Archived session {session.id}. Use `harness sessions purge {session.id} --confirm {session.id}` for session-only hard delete.")


@sessions_app.command("purge")
def sessions_purge(
    session_id: str,
    confirm: Annotated[str | None, typer.Option("--confirm", help="Required exact session id confirmation.")] = None,
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    if confirm != session_id:
        payload = {
            "schema_version": "harness.session_purge/v1",
            "ok": False,
            "hard_deleted": False,
            "error": "Hard delete requires --confirm <session_id>.",
            "session_id": session_id,
            "process_started": False,
            "permission_granting": False,
        }
        if output == OutputFormat.JSON:
            _emit_json(payload)
        else:
            typer.echo(payload["error"])
        raise typer.Exit(code=1)
    store = SQLiteStore(project_root)
    try:
        counts = store.hard_delete_session(session_id)
    except KeyError as exc:
        _emit_session_error(str(exc).strip("'"), output)
        raise typer.Exit(code=1) from exc
    payload = {
        "schema_version": "harness.session_purge/v1",
        "ok": True,
        "hard_deleted": True,
        "session_id": session_id,
        "behavior": "session_only_hard_delete",
        "deletion_counts": counts,
        "runs_deleted": 0,
        "tasks_deleted": 0,
        "artifacts_deleted": 0,
        "process_started": False,
        "permission_granting": False,
    }
    if output == OutputFormat.JSON:
        _emit_json(payload)
        return
    typer.echo(f"Hard deleted session {session_id}. Linked runs, tasks, and artifacts were retained.")


@sessions_app.command("fork")
def sessions_fork(
    session_id: str,
    message_id: Annotated[str | None, typer.Option("--message", help="Message id to fork from.")] = None,
    title: Annotated[str | None, typer.Option("--title", help="Title for the child session.")] = None,
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    store = SQLiteStore(project_root)
    try:
        session = store.fork_session(session_id, message_id=message_id, title=title)
    except KeyError as exc:
        _emit_session_error(str(exc).strip("'"), output)
        raise typer.Exit(code=1) from exc
    payload = {"schema_version": "harness.session_fork/v1", "ok": True, "session": session.model_dump(mode="json")}
    if output == OutputFormat.JSON:
        _emit_json(payload)
        return
    typer.echo(f"Forked session {session.id}.")


@sessions_app.command("export")
def sessions_export(
    session_id: str,
    metadata_only: Annotated[
        bool,
        typer.Option("--metadata-only", help="Export metadata, transcript, events, and artifact references only."),
    ] = True,
    sanitize: Annotated[bool, typer.Option("--sanitize/--no-sanitize", help="Sanitize exported text fields.")] = True,
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.JSON,
) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    store = SQLiteStore(project_root)
    try:
        session = store.get_session(session_id)
    except KeyError as exc:
        _emit_session_error(str(exc).strip("'"), output)
        raise typer.Exit(code=1) from exc
    messages = store.list_session_messages(session.id)
    parts = store.list_session_parts(session.id)
    events = store.list_session_store_events(session.id)
    payload = {
        "schema_version": "harness.session_export/v1",
        "ok": True,
        "metadata_only": metadata_only,
        "sanitize": sanitize,
        "include_artifacts": False,
        "session": session.model_dump(mode="json"),
        "messages": [message.model_dump(mode="json") for message in messages],
        "parts": [part.model_dump(mode="json") for part in parts],
        "events": [event.model_dump(mode="json") for event in events],
    }
    if output == OutputFormat.JSON:
        _emit_json(payload)
        return
    _print_section("Session export")
    _print_kv("Session id", session.id)
    _print_kv("Messages", len(messages))
    _print_kv("Parts", len(parts))
    _print_kv("Events", len(events))
    _print_kv("Artifacts included", "no")


@sessions_app.command("share")
def sessions_share(
    session_id: str,
    hosted: Annotated[bool, typer.Option("--hosted", help="Request hosted sharing when implemented.")] = False,
    sanitize: Annotated[bool, typer.Option("--sanitize/--no-sanitize", help="Sanitize shared text fields.")] = True,
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.JSON,
) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    store = SQLiteStore(project_root)
    try:
        store.get_session(session_id)
    except KeyError as exc:
        _emit_session_error(str(exc).strip("'"), output)
        raise typer.Exit(code=1) from exc
    if hosted:
        payload = hosted_share_unsupported(session_id, {"sanitize": sanitize})
        if output == OutputFormat.JSON:
            _emit_json(payload)
            raise typer.Exit(code=1)
        typer.echo(payload["error"])
        raise typer.Exit(code=1)
    payload = build_local_session_share_snapshot(store, session_id, sanitize=sanitize)
    if output == OutputFormat.JSON:
        _emit_json(payload)
        return
    _print_section("Local session share snapshot")
    _print_kv("Session id", session_id)
    _print_kv("Snapshot sha256", payload["snapshot_sha256"])
    _print_kv("Hosted URL", "not supported")
    _print_kv("Artifact files included", "no")


@sessions_app.command("tail")
def sessions_tail(
    session_id: str,
    project: ProjectOption = Path("."),
    jsonl: Annotated[bool, typer.Option("--jsonl", help="Emit append-only session events as JSONL.")] = False,
    follow: Annotated[bool, typer.Option("--follow", "-f", help="Follow until the session reaches a terminal state.")] = False,
    limit: Annotated[int, typer.Option("--limit", help="Maximum number of recent events to render before following.")] = 50,
) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    store = SQLiteStore(project_root)
    try:
        _tail_session_events(store, session_id, jsonl=jsonl, follow=follow, limit=limit)
    except typer.BadParameter as exc:
        _emit_session_error(str(exc).strip("'"), OutputFormat.TEXT)
        raise typer.Exit(code=1) from exc


@sessions_app.command("replay")
def sessions_replay(
    session_id: str,
    after_seq: Annotated[int | None, typer.Option("--after-seq", help="Return events after this event-store sequence.")] = None,
    limit: Annotated[int | None, typer.Option("--limit", help="Maximum events to return.")] = None,
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.JSON,
) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    store = SQLiteStore(project_root)
    try:
        payload = build_session_replay_projection(store, session_id, after_seq=after_seq, limit=limit)
    except KeyError as exc:
        _emit_session_error(str(exc).strip("'"), output)
        raise typer.Exit(code=1) from exc
    if output == OutputFormat.JSON:
        _emit_json(payload)
        return
    for event in payload["events"]:
        typer.echo(f"{event['seq']:04d} {event['kind']}")
    typer.echo(f"Next cursor: {payload['next_after_seq']}")


@sessions_app.command("transcript")
def sessions_transcript(
    session_id: str,
    project: ProjectOption = Path("."),
    format: TranscriptFormatOption = "markdown",
) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    store = SQLiteStore(project_root)
    try:
        store.get_session(session_id)
    except KeyError as exc:
        _emit_session_error(str(exc).strip("'"), OutputFormat.TEXT)
        raise typer.Exit(code=1) from exc
    entries = list_session_transcript(store, session_id)
    if format == "jsonl":
        for entry in entries:
            typer.echo(transcript_entry_jsonl(entry))
        return
    if format != "markdown":
        raise typer.BadParameter("Transcript format must be markdown or jsonl.")
    for index, entry in enumerate(entries):
        if index:
            typer.echo("")
        typer.echo(render_transcript_entry(entry))


@sessions_app.command("retract-message")
def sessions_retract_message(
    session_id: str,
    message_id: str,
    reason: Annotated[str | None, typer.Option("--reason", help="Reason recorded in the append-only event.")] = None,
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    store = SQLiteStore(project_root)
    try:
        event = store.record_session_message_retraction(session_id, message_id, reason=reason)
    except KeyError as exc:
        _emit_session_error(str(exc).strip("'"), output)
        raise typer.Exit(code=1) from exc
    payload = {
        "schema_version": "harness.session_message_retraction/v1",
        "ok": True,
        "session_id": session_id,
        "message_id": message_id,
        "event": event.model_dump(mode="json"),
        "message_mutated": False,
        "parts_mutated": False,
        "permission_granting": False,
    }
    if output == OutputFormat.JSON:
        _emit_json(payload)
        return
    typer.echo(f"Recorded retraction event for message {message_id}.")


@sessions_app.command("correct-part")
def sessions_correct_part(
    session_id: str,
    part_id: str,
    corrected_text: Annotated[str, typer.Option("--text", help="Corrected text recorded as a new event.")],
    reason: Annotated[str | None, typer.Option("--reason", help="Reason recorded in the append-only event.")] = None,
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    store = SQLiteStore(project_root)
    try:
        event = store.record_session_part_correction(session_id, part_id, corrected_text=corrected_text, reason=reason)
    except KeyError as exc:
        _emit_session_error(str(exc).strip("'"), output)
        raise typer.Exit(code=1) from exc
    payload = {
        "schema_version": "harness.session_part_correction/v1",
        "ok": True,
        "session_id": session_id,
        "part_id": part_id,
        "event": event.model_dump(mode="json"),
        "part_mutated": False,
        "message_mutated": False,
        "permission_granting": False,
    }
    if output == OutputFormat.JSON:
        _emit_json(payload)
        return
    typer.echo(f"Recorded correction event for part {part_id}.")


@sessions_app.command("diff")
def sessions_diff(
    session_id: str,
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    store = SQLiteStore(project_root)
    try:
        payload = _session_diff_projection(store, session_id)
    except KeyError as exc:
        _emit_session_error(str(exc).strip("'"), output)
        raise typer.Exit(code=1) from exc
    if output == OutputFormat.JSON:
        _emit_json(payload)
        return
    if not payload["diffs"]:
        typer.echo("No session diff artifacts found.")
        return
    for index, item in enumerate(payload["diffs"]):
        if index:
            typer.echo("")
        typer.echo(f"Diff artifact: {item['id']} kind={item['kind']} run={item['run_id']}")
        if item.get("preview"):
            typer.echo(item["preview"])
        if item.get("preview_truncated"):
            typer.echo("[diff preview truncated]")
    typer.echo("Revert, unrevert, and selected hunk apply are not enabled for session diffs yet.")


@sessions_app.command("changed-files")
def sessions_changed_files(
    session_id: str,
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    store = SQLiteStore(project_root)
    cfg = load_config(project_root)
    try:
        payload = _session_changed_files_projection(store, session_id, project_root, cfg.context_excludes)
    except KeyError as exc:
        _emit_session_error(str(exc).strip("'"), output)
        raise typer.Exit(code=1) from exc
    if output == OutputFormat.JSON:
        _emit_json(payload)
        return
    if not payload["files"]:
        typer.echo("No session changed files found.")
        return
    _print_tsv(["path", "sources", "diff_artifacts", "active_status"])
    for item in payload["files"]:
        active = item.get("active_repo_status") or {}
        active_status = f"{active.get('index_status') or ''}{active.get('worktree_status') or ''}".strip() or "none"
        _print_tsv_row(
            [
                item["path"],
                ",".join(item["sources"]),
                ",".join(item["diff_artifact_ids"]),
                active_status,
            ]
        )


@sessions_app.command("snapshots")
def sessions_snapshots(
    session_id: str,
    message_id: Annotated[str | None, typer.Option("--message", help="Limit snapshot metadata to one message id.")] = None,
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    store = SQLiteStore(project_root)
    cfg = load_config(project_root)
    try:
        payload = _session_snapshots_projection(
            store,
            session_id,
            project_root,
            cfg.context_excludes,
            message_id=message_id,
        )
    except KeyError as exc:
        _emit_session_error(str(exc).strip("'"), output)
        raise typer.Exit(code=1) from exc
    if output == OutputFormat.JSON:
        _emit_json(payload)
        return
    if not payload["snapshots"]:
        typer.echo("No session snapshots found.")
        return
    _print_tsv(["snapshot", "source", "message", "runs", "diff_artifacts", "changed_files", "revert_supported"])
    for snapshot in payload["snapshots"]:
        _print_tsv_row(
            [
                snapshot["snapshot_id"],
                snapshot["source"],
                snapshot["message_id"],
                ",".join(snapshot["run_ids"]),
                ",".join(artifact["id"] for artifact in snapshot["diff_artifacts"]),
                ",".join(snapshot["changed_paths"]),
                snapshot["revert_supported"],
            ]
        )
    typer.echo("Snapshot metadata is read-only; revert, unrevert, and selected hunk apply are not enabled yet.")


@sessions_app.command("revert-readiness")
def sessions_revert_readiness(
    session_id: str,
    message_id: Annotated[str | None, typer.Option("--message", help="Limit revert readiness to one message id.")] = None,
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    store = SQLiteStore(project_root)
    cfg = load_config(project_root)
    try:
        payload = _session_revert_readiness_projection(
            store,
            session_id,
            project_root,
            cfg.context_excludes,
            message_id=message_id,
        )
    except KeyError as exc:
        _emit_session_error(str(exc).strip("'"), output)
        raise typer.Exit(code=1) from exc
    if output == OutputFormat.JSON:
        _emit_json(payload)
        return
    _print_tsv(["field", "value"])
    _print_tsv_row(["ready", payload["ready"]])
    _print_tsv_row(["snapshots", payload["snapshot_count"]])
    _print_tsv_row(["diff_artifacts", payload["diff_artifact_count"]])
    _print_tsv_row(["changed_files", payload["changed_file_count"]])
    _print_tsv_row(["active_conflicts", payload["active_conflict_count"]])
    _print_tsv_row(["reversibility", payload["mutation_reversibility"]])
    _print_tsv_row(["policy_boundary", payload["policy_boundary"]["kind"]])
    if payload["changed_paths"]:
        _print_tsv_row(["changed_paths", ",".join(payload["changed_paths"])])
    typer.echo("Blockers:")
    for blocker in payload["blockers"]:
        typer.echo(f"- {blocker['code']}: {blocker['message']}")
    typer.echo("Revert readiness is diagnostic only; no revert, unrevert, selected hunk apply, or filesystem mutation was started.")


@sessions_app.command("revert")
def sessions_revert(
    session_id: str,
    message_id: Annotated[str | None, typer.Option("--message", help="Message id whose effects would be reverted.")] = None,
    artifact_id: Annotated[str | None, typer.Option("--artifact", help="Diff/snapshot artifact id to target.")] = None,
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    _emit_session_mutation_unsupported(
        "revert",
        session_id,
        output,
        project=project,
        message_id=message_id,
        artifact_id=artifact_id,
    )


@sessions_app.command("unrevert")
def sessions_unrevert(
    session_id: str,
    message_id: Annotated[str | None, typer.Option("--message", help="Message id whose effects would be restored.")] = None,
    artifact_id: Annotated[str | None, typer.Option("--artifact", help="Diff/snapshot artifact id to target.")] = None,
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    _emit_session_mutation_unsupported(
        "unrevert",
        session_id,
        output,
        project=project,
        message_id=message_id,
        artifact_id=artifact_id,
    )


@sessions_app.command("apply-hunk")
def sessions_apply_hunk(
    session_id: str,
    hunk_id: Annotated[str, typer.Option("--hunk", help="Selected hunk id to apply later.")] = "",
    artifact_id: Annotated[str | None, typer.Option("--artifact", help="Diff artifact id containing the hunk.")] = None,
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    _emit_session_mutation_unsupported(
        "apply-hunk",
        session_id,
        output,
        project=project,
        artifact_id=artifact_id,
        hunk_id=hunk_id,
    )


@sessions_app.command("tools")
def sessions_tools(
    tool_id: Annotated[str | None, typer.Option("--tool", help="Inspect one session tool descriptor.")] = None,
    plan_only: Annotated[bool, typer.Option("--plan-only", help="Show only tools allowed for the plan agent.")] = False,
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    try:
        descriptors = [get_session_tool_descriptor(tool_id)] if tool_id else default_session_tool_descriptors()
    except KeyError as exc:
        _emit_session_error(str(exc).strip("'"), output)
        raise typer.Exit(code=1) from exc
    if plan_only:
        descriptors = [descriptor for descriptor in descriptors if descriptor.allowed_in_plan_agent]
    payload = {
        "schema_version": "harness.session_tools/v1",
        "ok": True,
        "permission_granting": False,
        "tools": [descriptor.model_dump(mode="json") for descriptor in descriptors],
    }
    if output == OutputFormat.JSON:
        _emit_json(payload)
        return
    _print_section("Session tools")
    typer.echo("Descriptors are documentation and validation metadata, not permission grants.")
    if not descriptors:
        typer.echo("No session tools matched.")
        return
    _print_tsv(["tool", "side_effect", "boundary", "permission", "plan"])
    for descriptor in descriptors:
        _print_tsv_row(
            [
                descriptor.id,
                descriptor.side_effect.value,
                descriptor.boundary_kind.value,
                descriptor.permission_key,
                "yes" if descriptor.allowed_in_plan_agent else "no",
            ]
        )


@sessions_app.command("tool")
def sessions_tool(
    session_id: str,
    tool_id: str,
    input_json: Annotated[
        str,
        typer.Option("--input-json", help="JSON object arguments for the session tool."),
    ] = "{}",
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    try:
        arguments = json.loads(input_json)
        if not isinstance(arguments, dict):
            raise ValueError("--input-json must decode to a JSON object.")
        result = execute_session_tool(
            SQLiteStore.open_initialized(project_root),
            project_root,
            session_id,
            tool_id,
            arguments,
        )
    except CwdResolutionError as exc:
        _emit_session_error(cwd_recovery_message(exc), output)
        raise typer.Exit(code=1) from exc
    except (KeyError, ValueError, json.JSONDecodeError) as exc:
        _emit_session_error(str(exc).strip("'"), output)
        raise typer.Exit(code=1) from exc
    except sqlite3.Error as exc:
        message = SESSION_SCHEMA_REPAIR_MESSAGE if is_missing_session_schema_error(exc) else str(exc)
        _emit_session_error(message, output)
        raise typer.Exit(code=1) from exc
    payload = {
        "schema_version": "harness.session_tool_execution/v1",
        "ok": result.ok,
        "result": result.model_dump(mode="json"),
    }
    if output == OutputFormat.JSON:
        _emit_json(payload)
        return
    typer.echo(f"Tool: {result.tool_id}")
    typer.echo(f"Status: {'ok' if result.ok else 'failed'}")
    typer.echo(f"Run: {result.run_id}")
    if result.permission_id:
        typer.echo(f"Permission: {result.permission_id}")
    if result.artifact_id:
        typer.echo(f"Artifact: {result.artifact_id}")
    typer.echo(result.preview)


@sessions_app.command("todo")
def sessions_todo(
    session_id: str,
    content: Annotated[str | None, typer.Option("--content", help="Todo content to append.")] = None,
    status: Annotated[str | None, typer.Option("--status", help="Filter/list or append with this todo status.")] = None,
    priority: Annotated[int, typer.Option("--priority", help="Todo priority when appending.")] = 0,
    list_only: Annotated[bool, typer.Option("--list", help="List existing session todos.")] = False,
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    store = SQLiteStore(project_root)
    try:
        store.get_session(session_id)
        if list_only or content is None:
            todos = store.list_session_todos(session_id, status=status)
            payload = {
                "schema_version": "harness.session_todos/v1",
                "ok": True,
                "session_id": session_id,
                "todos": [todo.model_dump(mode="json") for todo in todos],
            }
            if output == OutputFormat.JSON:
                _emit_json(payload)
                return
            if not todos:
                typer.echo("No session todos found.")
                return
            _print_tsv(["todo_id", "status", "priority", "content"])
            for todo in todos:
                _print_tsv_row([todo.id, todo.status, str(todo.priority), todo.content])
            return
        todo = store.append_session_todo(session_id, content, status=status or "pending", priority=priority)
    except (KeyError, ValueError) as exc:
        _emit_session_error(str(exc).strip("'"), output)
        raise typer.Exit(code=1) from exc
    payload = {"schema_version": "harness.session_todo/v1", "ok": True, "todo": todo.model_dump(mode="json")}
    if output == OutputFormat.JSON:
        _emit_json(payload)
        return
    typer.echo(f"Added todo {todo.id}.")


@sessions_app.command("question")
def sessions_question(
    session_id: str,
    question: Annotated[str, typer.Option("--question", help="Question to persist for the operator.")],
    choice: Annotated[list[str] | None, typer.Option("--choice", help="Optional answer choice.")] = None,
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    store = SQLiteStore(project_root)
    try:
        store.get_session(session_id)
        part = store.append_session_question(session_id, question, choices=choice or [])
    except KeyError as exc:
        _emit_session_error(str(exc).strip("'"), output)
        raise typer.Exit(code=1) from exc
    payload = {"schema_version": "harness.session_question/v1", "ok": True, "part": part.model_dump(mode="json")}
    if output == OutputFormat.JSON:
        _emit_json(payload)
        return
    typer.echo(f"Recorded question part {part.id}.")


def _session_permission_reply_status(reply: str | None) -> SessionPermissionStatus:
    if not reply:
        raise typer.BadParameter("Missing permission reply.")
    normalized = reply.strip().lower()
    if normalized in {"once", "always", "allow", "allowed"}:
        return SessionPermissionStatus.ALLOWED
    if normalized in {"reject", "deny", "denied"}:
        return SessionPermissionStatus.DENIED
    if normalized in {"cancel", "cancelled", "canceled"}:
        return SessionPermissionStatus.CANCELLED
    raise typer.BadParameter("--reply must be one of: once, always, reject, cancel.")


@sessions_app.command("permission")
def sessions_permission(
    session_id: str,
    request: Annotated[bool, typer.Option("--request", help="Create a pending permission request.")] = False,
    resolve: Annotated[str | None, typer.Option("--resolve", help="Resolve an existing permission id.")] = None,
    reply: Annotated[
        str | None,
        typer.Option("--reply", help="opencode-style reply for --resolve: once, always, reject, or cancel."),
    ] = None,
    decision: Annotated[
        SessionPermissionStatus | None,
        typer.Option("--decision", help="Resolution decision: allowed, denied, or cancelled."),
    ] = None,
    tool: Annotated[str | None, typer.Option("--tool", help="Tool id for a new request.")] = None,
    action: Annotated[str | None, typer.Option("--action", help="Normalized action for a new request.")] = None,
    target: Annotated[
        str | None,
        typer.Option("--target", help="Normalized target pattern for a new request."),
    ] = None,
    boundary: Annotated[
        SessionPermissionBoundaryKind,
        typer.Option("--boundary", help="Permission boundary kind."),
    ] = SessionPermissionBoundaryKind.LOCAL_ONLY,
    risk: Annotated[str, typer.Option("--risk", help="Risk label for a new request.")] = "low",
    scope: Annotated[
        SessionPermissionScope,
        typer.Option("--scope", help="Grant scope for a new request."),
    ] = SessionPermissionScope.ONCE,
    reason: Annotated[list[str] | None, typer.Option("--reason", help="Policy reason or resolution reason.")] = None,
    status: Annotated[SessionPermissionStatus | None, typer.Option("--status", help="Filter listed permissions.")] = None,
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    store = SQLiteStore(project_root)
    try:
        store.get_session(session_id)
        if request:
            if not tool or not action or not target:
                raise typer.BadParameter("--request requires --tool, --action, and --target.")
            permission = store.request_session_permission(
                session_id,
                tool_id=tool,
                normalized_action=action,
                normalized_target_pattern=target,
                boundary_kind=boundary,
                risk=risk,
                scope=scope,
                source=SessionPermissionSource.POLICY,
                policy_reasons=reason or [],
            )
            payload = {
                "schema_version": "harness.session_permission/v1",
                "ok": True,
                "permission": permission.model_dump(mode="json"),
            }
        elif resolve is not None:
            if decision is None and reply is None:
                raise typer.BadParameter("--resolve requires --decision or --reply.")
            existing = store.get_session_permission(resolve)
            if existing.session_id != session_id:
                raise typer.BadParameter(f"Permission {resolve} does not belong to session {session_id}.")
            resolved_status = decision or _session_permission_reply_status(reply)
            permission = store.resolve_session_permission(
                resolve,
                resolved_status,
                source=SessionPermissionSource.USER,
                reason="; ".join(reason or []) if reason else None,
            )
            permissions = store.list_session_permissions(session_id)
            pending_ids = [permission.id for permission in permissions if permission.status == SessionPermissionStatus.PENDING]
            payload = {
                "schema_version": "harness.session_permission_reply/v1" if reply else "harness.session_permission/v1",
                "ok": True,
                "reply": reply,
                "decision": resolved_status.value,
                "permission": permission.model_dump(mode="json"),
                "pending_permission_ids": pending_ids,
                "pending_count": len(pending_ids),
                "execution_started": False,
                "scope_broadened": False,
                "permission_granting": resolved_status == SessionPermissionStatus.ALLOWED,
            }
        else:
            permissions = store.list_session_permissions(session_id, status=status)
            payload = {
                "schema_version": "harness.session_permissions/v1",
                "ok": True,
                "session_id": session_id,
                "permissions": [permission.model_dump(mode="json") for permission in permissions],
            }
    except (KeyError, ValueError) as exc:
        _emit_session_error(str(exc).strip("'"), output)
        raise typer.Exit(code=1) from exc
    if output == OutputFormat.JSON:
        _emit_json(payload)
        return
    if "permissions" in payload:
        permissions = payload["permissions"]
        if not permissions:
            typer.echo("No session permissions found.")
            return
        _print_tsv(["permission_id", "status", "scope", "tool", "target", "expires_at"])
        for permission in permissions:
            _print_tsv_row(
                [
                    permission["id"],
                    permission["status"],
                    permission["scope"],
                    permission["tool_id"],
                    permission["normalized_target_pattern"],
                    permission["expires_at"],
                ]
            )
        return
    permission = payload["permission"]
    typer.echo(f"Permission {permission['id']}: {permission['status']}")


@app.command("resume")
def resume(session_id: str, project: ProjectOption = Path("."), output: OutputOption = OutputFormat.TEXT) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    try:
        session = SQLiteStore(project_root).get_session(session_id)
    except KeyError as exc:
        _emit_session_error(str(exc).strip("'"), output)
        raise typer.Exit(code=1) from exc
    events = read_session_events(project_root, session.id)
    payload = {
        "schema_version": "harness.session_resume/v1",
        "ok": True,
        "session": session.model_dump(mode="json"),
        "events": [event.model_dump(mode="json") for event in events],
        "next_actions": _session_next_actions(session),
    }
    if output == OutputFormat.JSON:
        _emit_json(payload)
        return
    typer.echo(f"Resumed session {session.id}")
    for event in events[-20:]:
        typer.echo(render_session_event(event))
    typer.echo("Next:")
    for action in payload["next_actions"]:
        typer.echo(f"  {action}")


@actions_app.command("route")
def actions_route(
    instruction: Annotated[str, typer.Argument(help="Natural-language local action instruction.")],
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    project_root = resolve_project_root(project)
    route = route_managed_action(instruction, project_root)
    decision = decide_managed_action(route, project_root)
    payload = {
        "schema_version": "harness.managed_action_route_preview/v1",
        "ok": decision.status != ManagedActionDecisionStatus.DENIED,
        "project_root": str(project_root),
        "instruction": instruction,
        "route": route.model_dump(mode="json"),
        "decision": decision.model_dump(mode="json"),
    }
    if output == OutputFormat.JSON:
        _emit_json(payload)
        return
    _print_section("Managed Action Route")
    _print_kv("Intent", route.intent)
    _print_kv("Confidence", route.confidence)
    _print_kv("Risk", route.risk.value)
    _print_kv("Executor", route.executor)
    _print_kv("Decision", decision.status.value)
    if decision.reasons:
        _print_section("Reasons")
        for reason in decision.reasons:
            typer.echo(reason)


@actions_app.command("run")
def actions_run(
    instruction: Annotated[str, typer.Argument(help="Natural-language local action instruction.")],
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    store = SQLiteStore(project_root)
    route = route_managed_action(instruction, project_root)
    decision = decide_managed_action(route, project_root)
    if decision.status != ManagedActionDecisionStatus.AUTO_ALLOWED:
        payload = {
            "schema_version": "harness.managed_action_run/v1",
            "ok": False,
            "project_root": str(project_root),
            "instruction": instruction,
            "route": route.model_dump(mode="json"),
            "decision": decision.model_dump(mode="json"),
            "result": None,
        }
        if output == OutputFormat.JSON:
            _emit_json(payload)
            raise typer.Exit(code=1)
        typer.echo(f"Action not executed: {decision.status.value}")
        for reason in decision.reasons:
            typer.echo(reason)
        raise typer.Exit(code=1)
    try:
        result = execute_managed_action(project_root, route, decision, store)
    except ValueError as exc:
        payload = {
            "schema_version": "harness.managed_action_run/v1",
            "ok": False,
            "project_root": str(project_root),
            "instruction": instruction,
            "route": route.model_dump(mode="json"),
            "decision": decision.model_dump(mode="json"),
            "errors": [str(exc)],
        }
        if output == OutputFormat.JSON:
            _emit_json(payload)
            return
        typer.echo(str(exc))
        raise typer.Exit(code=1) from exc
    payload = {
        "schema_version": "harness.managed_action_run/v1",
        "ok": result.ok,
        "project_root": str(project_root),
        "instruction": instruction,
        "route": route.model_dump(mode="json"),
        "decision": decision.model_dump(mode="json"),
        "result": result.model_dump(mode="json"),
    }
    if output == OutputFormat.JSON:
        _emit_json(payload)
        return
    typer.echo(result.message)
    if result.run_id:
        typer.echo(f"Run: {result.run_id}")
    if result.report_path:
        typer.echo(f"Report: {result.report_path}")
    if result.manifest_path:
        typer.echo(f"Manifest: {result.manifest_path}")
    if not result.ok:
        raise typer.Exit(code=1)


@actions_app.command("report")
def actions_report(run_id: str, project: ProjectOption = Path("."), output: OutputOption = OutputFormat.TEXT) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    store = SQLiteStore(project_root)
    try:
        run = store.get_run(run_id)
    except KeyError as exc:
        _emit_managed_action_error("harness.managed_action_report/v1", str(exc).strip("'"), output)
        raise typer.Exit(code=1) from exc
    report_path = store.runs_dir / run.id / "final_report.md"
    if not report_path.exists() or not report_path.read_text(encoding="utf-8").strip():
        _emit_managed_action_error("harness.managed_action_report/v1", f"Managed action report not found: {run_id}", output)
        raise typer.Exit(code=1)
    payload = {
        "schema_version": "harness.managed_action_report/v1",
        "ok": True,
        "run_id": run.id,
        "path": str(report_path),
        "content": report_path.read_text(encoding="utf-8"),
    }
    if output == OutputFormat.JSON:
        _emit_json(payload)
        return
    typer.echo(payload["content"])


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


@artifacts_app.command("open")
def artifacts_open(
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
        _emit_artifact_error("harness.artifacts_open/v1", str(exc).strip("'"), output)
        raise typer.Exit(code=1) from exc
    run_dir = project_root / HARNESS_DIR / "runs" / run_id
    payload = {
        "schema_version": "harness.artifacts_open/v1",
        "ok": True,
        "run_id": run_id,
        "run_dir": str(run_dir),
        "artifacts": [artifact.model_dump(mode="json") for artifact in artifacts],
    }
    if output == OutputFormat.JSON:
        _emit_json(payload)
        return
    typer.echo(f"Run artifacts: {run_dir}")
    for artifact in artifacts:
        typer.echo(f"{artifact.kind}: {artifact.path}")


@app.command("report")
def report(run_id: str, project: ProjectOption = Path("."), output: OutputOption = OutputFormat.TEXT) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    store = SQLiteStore(project_root)
    try:
        report_path = _ensure_product_report(store, run_id)
    except KeyError as exc:
        _emit_artifact_error("harness.report/v1", str(exc).strip("'"), output)
        raise typer.Exit(code=1) from exc
    payload = {
        "schema_version": "harness.report/v1",
        "ok": True,
        "run_id": run_id,
        "path": str(report_path),
        "content": report_path.read_text(encoding="utf-8"),
    }
    if output == OutputFormat.JSON:
        _emit_json(payload)
        return
    typer.echo(payload["content"])


@app.command("diff")
def diff(run_id: str, project: ProjectOption = Path("."), output: OutputOption = OutputFormat.TEXT) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    store = SQLiteStore(project_root)
    try:
        diff_artifact = _find_artifact_by_kind(store, run_id, {"isolated_diff", "diff", "patch", "diff.patch"})
    except KeyError as exc:
        _emit_artifact_error("harness.diff/v1", str(exc).strip("'"), output)
        raise typer.Exit(code=1) from exc
    if diff_artifact is None:
        payload = {"schema_version": "harness.diff/v1", "ok": False, "run_id": run_id, "error": "No diff artifact found."}
        if output == OutputFormat.JSON:
            _emit_json(payload)
            return
        typer.echo("No diff artifact found for this run.")
        raise typer.Exit(code=1)
    content = diff_artifact.path.read_text(encoding="utf-8")
    payload = {
        "schema_version": "harness.diff/v1",
        "ok": True,
        "run_id": run_id,
        "artifact": diff_artifact.model_dump(mode="json"),
        "content": content,
    }
    if output == OutputFormat.JSON:
        _emit_json(payload)
        return
    typer.echo(content)


@app.command("reject")
def reject(run_id: str, project: ProjectOption = Path("."), output: OutputOption = OutputFormat.TEXT) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    try:
        payload = _record_apply_decision(project_root, run_id, "rejected")
    except KeyError as exc:
        _emit_artifact_error("harness.apply_decision/v1", str(exc).strip("'"), output)
        raise typer.Exit(code=1) from exc
    if output == OutputFormat.JSON:
        _emit_json(payload)
        return
    typer.echo(f"Rejected apply-back for {run_id}")
    typer.echo(f"Decision artifact: {payload['artifact']['path']}")


@app.command("apply")
def apply(run_id: str, project: ProjectOption = Path("."), output: OutputOption = OutputFormat.TEXT) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    try:
        payload = _record_apply_decision(project_root, run_id, "apply_requested", ok=False)
    except KeyError as exc:
        _emit_artifact_error("harness.apply_decision/v1", str(exc).strip("'"), output)
        raise typer.Exit(code=1) from exc
    payload["error"] = (
        "Direct product-spine apply is not enabled yet; use the existing codex_code_edit apply-back "
        "approval path or inspect the diff artifact first."
    )
    if output == OutputFormat.JSON:
        _emit_json(payload)
        return
    typer.echo(payload["error"])
    typer.echo(f"Decision artifact: {payload['artifact']['path']}")
    raise typer.Exit(code=1)


@app.command("undo")
def undo(run_id: str, project: ProjectOption = Path("."), output: OutputOption = OutputFormat.TEXT) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    try:
        store = SQLiteStore(project_root)
        store.get_run(run_id)
    except KeyError as exc:
        _emit_artifact_error("harness.undo/v1", str(exc).strip("'"), output)
        raise typer.Exit(code=1) from exc
    undo_path = project_root / HARNESS_DIR / "runs" / run_id / "undo.json"
    payload = {
        "schema_version": "harness.undo/v1",
        "ok": False,
        "run_id": run_id,
        "error": "Undo metadata is not available for this run.",
        "undo_path": str(undo_path),
    }
    if output == OutputFormat.JSON:
        _emit_json(payload)
        return
    typer.echo(payload["error"])
    typer.echo("Manual recovery: inspect the run report, manifest, and diff artifact.")
    raise typer.Exit(code=1)


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


@core_app.command("run")
def core_run(
    goal: Annotated[str, typer.Argument(help="Operator goal to run through the headless core loop.")],
    mode: Annotated[str, typer.Option("--mode", help="Core mode: dry_run, repo_planning, or codex_isolated_edit.")] = "dry_run",
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.JSON,
) -> None:
    project_root = resolve_project_root(project)
    result = HarnessCoreService().start_goal(
        goal,
        mode=mode,
        project_root=project_root,
        output_format=output.value,
    )
    if output == OutputFormat.JSON:
        _emit_json(result.model_dump(mode="json"))
        if not result.ok:
            raise typer.Exit(code=1)
        return
    typer.echo(result.summary.summary_text if result.summary is not None else f"Decision: {result.decision}")
    if not result.ok:
        raise typer.Exit(code=1)


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


@providers_app.command("list")
def providers_list(project: ProjectOption = Path("."), output: OutputOption = OutputFormat.TEXT) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    cfg = load_config(project_root)
    providers = list_provider_catalog(cfg)
    models = list_model_catalog(cfg)
    cache = SQLiteStore(project_root).replace_provider_model_catalog_cache(providers, models)
    payload = {
        "schema_version": "harness.providers/v1",
        "ok": True,
        "cache": cache,
        "providers": [provider.model_dump(mode="json") for provider in providers],
    }
    payload.update(catalog_projection_evidence("providers_catalog_projection"))
    if output == OutputFormat.JSON:
        _emit_json(payload)
        return
    _print_tsv(["provider", "kind", "enabled", "credentials", "boundary", "model"])
    for provider in providers:
        _print_tsv_row(
            [
                provider.provider_id,
                provider.kind.value,
                str(provider.enabled),
                provider.credential_status.value,
                provider.metadata.data_boundary.value,
                str(provider.settings_preview.get("model") or ""),
            ]
        )
    typer.echo("Provider catalog entries are metadata only; credentials are not printed and no fallback is granted.")


@providers_app.command("status")
def providers_status(project: ProjectOption = Path("."), output: OutputOption = OutputFormat.TEXT) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    cfg = load_config(project_root)
    providers = list_provider_catalog(cfg)
    models = list_model_catalog(cfg)
    cache = SQLiteStore(project_root).replace_provider_model_catalog_cache(providers, models)
    payload = {
        "schema_version": "harness.providers_status/v1",
        "ok": True,
        "cache": cache,
        "providers": [provider.model_dump(mode="json") for provider in providers],
    }
    payload.update(catalog_projection_evidence("providers_status_projection"))
    if output == OutputFormat.JSON:
        _emit_json(payload)
        return
    for provider in providers:
        typer.echo(f"{provider.provider_id}:")
        typer.echo(f"  enabled: {provider.enabled}")
        typer.echo(f"  credential_status: {provider.credential_status.value}")
        typer.echo(f"  data_boundary: {provider.metadata.data_boundary.value}")
        if provider.constraints:
            typer.echo(f"  constraints: {', '.join(provider.constraints)}")


@providers_app.command("login")
def providers_login(
    provider_id: str,
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    cfg = load_config(project_root)
    if provider_id not in cfg.backends:
        _emit_session_error(f"Provider not found: {provider_id}", output)
        raise typer.Exit(code=1)
    payload = {
        "schema_version": "harness.provider_auth/v1",
        "ok": False,
        "provider_id": provider_id,
        "action": "login",
        "error": "Provider login is not implemented yet; refusing to write credentials or call providers implicitly.",
        "permission_granting": False,
        "no_hidden_fallback": True,
    }
    if output == OutputFormat.JSON:
        _emit_json(payload)
        raise typer.Exit(code=1)
    typer.echo(payload["error"])
    raise typer.Exit(code=1)


@providers_app.command("logout")
def providers_logout(
    provider_id: str,
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    cfg = load_config(project_root)
    if provider_id not in cfg.backends:
        _emit_session_error(f"Provider not found: {provider_id}", output)
        raise typer.Exit(code=1)
    payload = {
        "schema_version": "harness.provider_auth/v1",
        "ok": False,
        "provider_id": provider_id,
        "action": "logout",
        "error": "Provider logout is not implemented yet; refusing to remove credentials or call providers implicitly.",
        "permission_granting": False,
        "no_hidden_fallback": True,
    }
    if output == OutputFormat.JSON:
        _emit_json(payload)
        raise typer.Exit(code=1)
    typer.echo(payload["error"])
    raise typer.Exit(code=1)


@models_app.command("list")
def models_list(
    provider: Annotated[str | None, typer.Option("--provider", help="Filter by provider/backend id.")] = None,
    verbose: Annotated[bool, typer.Option("--verbose", help="Show capability and source details.")] = False,
    refresh: Annotated[
        bool,
        typer.Option("--refresh", help="Reserved for a later explicit provider refresh; currently fails closed."),
    ] = False,
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    if refresh:
        _emit_session_error("Model refresh is not implemented yet; refusing to call providers implicitly.", output)
        raise typer.Exit(code=1)
    cfg = load_config(project_root)
    if provider is not None and provider not in cfg.backends:
        _emit_session_error(f"Provider not found: {provider}", output)
        raise typer.Exit(code=1)
    providers = list_provider_catalog(cfg)
    all_models = list_model_catalog(cfg)
    cache = SQLiteStore(project_root).replace_provider_model_catalog_cache(providers, all_models)
    models = list_model_catalog(cfg, provider_id=provider)
    payload = {
        "schema_version": "harness.models/v1",
        "ok": True,
        "cache": cache,
        "models": [model.model_dump(mode="json") for model in models],
    }
    payload.update(catalog_projection_evidence("models_catalog_projection"))
    if output == OutputFormat.JSON:
        _emit_json(payload)
        return
    if verbose:
        _print_tsv(["provider", "model", "profile", "source", "tools", "reasoning", "modalities", "context", "cost", "raw_ref"])
        for model in models:
            _print_tsv_row(
                [
                    model.provider_id,
                    model.model_id,
                    model.model_profile_id or "",
                    model.source,
                    str(model.tool_support),
                    model.reasoning_support,
                    ",".join(model.modalities),
                    str(model.context_limit or ""),
                    json.dumps(model.cost, sort_keys=True) if model.cost is not None else "",
                    model.raw_model_ref,
                ]
            )
    else:
        _print_tsv(["provider", "model", "profile", "raw_ref"])
        for model in models:
            _print_tsv_row([model.provider_id, model.model_id, model.model_profile_id or "", model.raw_model_ref])
    typer.echo("Model refs are explicit metadata; unavailable selections must fail visibly rather than fall back.")


@models_app.command("validate")
def models_validate(
    raw_model_ref: Annotated[str, typer.Argument(help="Raw provider/model reference to validate.")],
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    cfg = load_config(project_root)
    result = validate_model_selection(cfg, raw_model_ref)
    payload = {
        "schema_version": "harness.model_selection_validation_result/v1",
        "ok": result.executable,
        "validation": result.model_dump(mode="json"),
    }
    if output == OutputFormat.JSON:
        _emit_json(payload)
        if not result.executable:
            raise typer.Exit(code=1)
        return
    typer.echo(f"Model: {raw_model_ref}")
    typer.echo(f"Known: {result.known_catalog_entry}")
    typer.echo(f"Provider enabled: {result.provider_enabled}")
    typer.echo(f"Executable: {result.executable}")
    if result.blocked_reasons:
        typer.echo(f"Blocked: {', '.join(result.blocked_reasons)}")
    typer.echo("Validation is metadata-only; no provider call, refresh, credential read, or fallback is performed.")
    if not result.executable:
        raise typer.Exit(code=1)


@mcp_app.command("status")
@mcp_app.command("list")
def mcp_list(project: ProjectOption = Path("."), output: OutputOption = OutputFormat.TEXT) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    cfg = load_config(project_root)
    payload = _mcp_status_projection(cfg)
    if output == OutputFormat.JSON:
        _emit_json(payload)
        return
    _print_tsv(["name", "kind", "enabled", "requires_network", "connected", "process_started", "network_called", "tools_registered"])
    for server in payload["servers"]:
        _print_tsv_row(
            [
                server["name"],
                server["kind"],
                server["enabled"],
                server["requires_network"],
                server["connected"],
                server["process_started"],
                server["network_called"],
                server["tool_registration_enabled"],
            ]
        )
    typer.echo("MCP diagnostics are metadata only; no process or network connection was started.")


@mcp_app.command("resources")
def mcp_resources(project: ProjectOption = Path("."), output: OutputOption = OutputFormat.TEXT) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    cfg = load_config(project_root)
    payload = _mcp_resources_projection(cfg)
    if output == OutputFormat.JSON:
        _emit_json(payload)
        return
    _print_tsv(["resource", "server", "enabled", "cached", "path", "content_type", "connected", "process_started", "network_called"])
    for resource in payload["resources"]:
        _print_tsv_row(
            [
                resource.get("uri") or resource.get("name") or "",
                resource.get("server") or "",
                resource.get("enabled", False),
                resource.get("cached", False),
                resource.get("path") or "",
                resource.get("content_type") or "",
                resource.get("connected", False),
                resource.get("process_started", False),
                resource.get("network_called", False),
            ]
        )
    if not payload["resources"]:
        _print_tsv_row(["none", "", False, payload["cached_only"], "", "", False, False, False])
    typer.echo("MCP resources are cached-only in this phase; no connection was attempted.")


@mcp_app.command("add")
def mcp_add(
    name: str,
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    _emit_mcp_unsupported("add", output, server_name=name, project=project)


@mcp_app.command("auth")
def mcp_auth(
    name: str,
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    _emit_mcp_unsupported("auth", output, server_name=name, project=project)


@mcp_app.command("logout")
def mcp_logout(
    name: str,
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    _emit_mcp_unsupported("logout", output, server_name=name, project=project)


@mcp_app.command("connect")
def mcp_connect(
    name: str,
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    _emit_mcp_unsupported("connect", output, server_name=name, project=project)


@mcp_app.command("disconnect")
def mcp_disconnect(
    name: str,
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    _emit_mcp_unsupported("disconnect", output, server_name=name, project=project)


@plugins_app.command("list")
def plugins_list(project: ProjectOption = Path("."), output: OutputOption = OutputFormat.TEXT) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    cfg = load_config(project_root)
    payload = _plugin_catalog(project_root, cfg)
    if output == OutputFormat.JSON:
        _emit_json(payload)
        return
    _print_tsv(["name", "scope", "enabled", "origin", "source_kind", "spec", "path", "manifest", "loaded", "tools_registered"])
    for plugin in payload["plugins"]:
        _print_tsv_row(
            [
                plugin["name"],
                plugin["scope"],
                plugin["enabled"],
                plugin["origin"],
                plugin.get("source_kind") or "",
                plugin.get("spec") or "",
                plugin.get("path") or "",
                plugin.get("manifest_path") or "",
                plugin["runtime_loaded"],
                plugin["tools_registered"],
            ]
        )
    typer.echo("Plugin diagnostics are metadata only; no plugin code was loaded and no tools were registered.")


@plugins_app.command("install")
def plugins_install(
    name: str,
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    _emit_plugin_unsupported("install", output, plugin_name=name, project=project)


@plugins_app.command("update")
def plugins_update(
    name: str,
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    _emit_plugin_unsupported("update", output, plugin_name=name, project=project)


@plugins_app.command("remove")
def plugins_remove(
    name: str,
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    _emit_plugin_unsupported("remove", output, plugin_name=name, project=project)


@skills_app.command("list")
def skills_list(project: ProjectOption = Path("."), output: OutputOption = OutputFormat.TEXT) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    cfg = load_config(project_root)
    payload = _skill_catalog(project_root, cfg)
    if output == OutputFormat.JSON:
        _emit_json(payload)
        return
    _print_tsv(["name", "scope", "enabled", "origin", "source_kind", "spec", "skill_file", "skill_file_exists", "loaded", "body_loaded", "tool_registered"])
    for skill in payload["skills"]:
        _print_tsv_row(
            [
                skill["name"],
                skill["scope"],
                skill["enabled"],
                skill["origin"],
                skill.get("source_kind") or "",
                skill.get("spec") or "",
                skill.get("skill_file_path") or skill.get("skill_file") or "",
                skill.get("skill_file_exists", False),
                skill["runtime_loaded"],
                skill["skill_body_loaded"],
                skill["tool_registered"],
            ]
        )
    typer.echo("Skill diagnostics are metadata only; skill bodies are not loaded in this phase.")


@skills_app.command("load")
def skills_load(
    name: str,
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    _emit_skill_unsupported("load", output, skill_name=name, project=project)


@web_app.callback()
def web(
    ctx: typer.Context,
    host: Annotated[str, typer.Option("--host", help="Local server host to describe.")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", help="Local server port to describe.")] = 8765,
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    """Top-level web client launcher boundary."""
    if ctx.invoked_subcommand is not None:
        return
    payload = _web_open_unsupported({}, host=host, port=port)
    if output == OutputFormat.JSON:
        _emit_json(payload)
        raise typer.Exit(code=1)
    typer.echo(payload["error"])
    raise typer.Exit(code=1)


@web_app.command("tools")
def web_tools(project: ProjectOption = Path("."), output: OutputOption = OutputFormat.TEXT) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    cfg = load_config(project_root)
    payload = _web_tool_policy_projection(cfg)
    if output == OutputFormat.JSON:
        _emit_json(payload)
        return
    _print_tsv(["tool", "enabled", "decision", "approval_required", "network_called"])
    for tool in payload["tools"]:
        _print_tsv_row(
            [
                tool["id"],
                tool["enabled"],
                tool["decision"],
                tool["approval_required"],
                tool["network_called"],
            ]
        )
    typer.echo("Web tool diagnostics are policy-only; no fetch or search request was made.")


@web_app.command("fetch")
def web_fetch(
    url: str,
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    _emit_web_unsupported("web-fetch", "fetch", output, target=url, project=project)


@web_app.command("search")
def web_search(
    query: str,
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    _emit_web_unsupported("web-search", "search", output, target=query, project=project)


@extensions_app.command("status")
def extensions_status(project: ProjectOption = Path("."), output: OutputOption = OutputFormat.TEXT) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    cfg = load_config(project_root)
    payload = _extensibility_status_projection(project_root, cfg)
    if output == OutputFormat.JSON:
        _emit_json(payload)
        return
    _print_tsv(["surface", "enabled", "count", "loaded_or_connected", "network_called", "filesystem_modified"])
    _print_tsv_row(
        [
            "mcp",
            payload["mcp"]["enabled"],
            f'servers={payload["mcp"]["server_count"]},resources={payload["mcp"]["resource_count"]}',
            payload["mcp"]["connected"],
            payload["mcp"]["network_called"],
            False,
        ]
    )
    _print_tsv_row(
        [
            "plugins",
            payload["plugins"]["enabled"],
            payload["plugins"]["plugin_count"],
            payload["plugins"]["runtime_loaded"],
            payload["plugins"]["network_called"],
            payload["plugins"]["filesystem_modified"],
        ]
    )
    _print_tsv_row(
        [
            "skills",
            payload["skills"]["enabled"],
            payload["skills"]["skill_count"],
            payload["skills"]["skill_body_loaded"],
            payload["skills"]["network_called"],
            payload["skills"]["filesystem_modified"],
        ]
    )
    _print_tsv_row(
        [
            "web-tools",
            payload["web_tools"]["enabled"],
            ",".join(f"{key}:{value}" for key, value in sorted(payload["web_tools"]["decisions"].items())),
            False,
            payload["web_tools"]["network_called"],
            False,
        ]
    )
    typer.echo("Extensibility diagnostics are metadata-only; no MCP connection, plugin load, skill body load, or web request was started.")


@web_client_app.command("status")
def web_client_status(
    host: Annotated[str, typer.Option("--host", help="Local server host to describe.")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", help="Local server port to describe.")] = 8765,
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    payload = _web_client_projection(host=host, port=port)
    if output == OutputFormat.JSON:
        _emit_json(payload)
        return
    _print_kv("Client URL", payload["client_url"])
    _print_kv("Client available", payload["client_available"])
    _print_kv("Static assets served", payload["static_assets_served"])
    _print_kv("Open supported", payload["open_supported"])


@web_client_app.command("open")
def web_client_open(
    host: Annotated[str, typer.Option("--host", help="Local server host to describe.")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", help="Local server port to describe.")] = 8765,
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    payload = _web_open_unsupported({}, host=host, port=port)
    if output == OutputFormat.JSON:
        _emit_json(payload)
        raise typer.Exit(code=1)
    typer.echo(payload["error"])
    raise typer.Exit(code=1)


@worktrees_app.command("list")
def worktrees_list(project: ProjectOption = Path("."), output: OutputOption = OutputFormat.TEXT) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    payload = _worktree_projection(project_root)
    if output == OutputFormat.JSON:
        _emit_json(payload)
        return
    if not payload["available"]:
        typer.echo(payload["reason"])
        return
    _print_tsv(["path", "current", "branch", "head", "mutation_supported"])
    for worktree in payload["worktrees"]:
        _print_tsv_row(
            [
                worktree["path"],
                worktree["is_current"],
                worktree.get("branch") or "",
                worktree.get("head") or "",
                worktree["mutation_supported"],
            ]
        )
    typer.echo("Worktree diagnostics are metadata only; create/remove/reset are not enabled in this phase.")


@worktrees_app.command("create")
def worktrees_create(
    path: str,
    branch: Annotated[str, typer.Option("--branch", help="Branch/ref to use when worktree creation is enabled later.")] = "HEAD",
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    _emit_worktree_unsupported("create", output, target=path, project=project, requested={"path": path, "branch": branch})


@worktrees_app.command("remove")
def worktrees_remove(
    path: str,
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    _emit_worktree_unsupported("remove", output, target=path, project=project, requested={"path": path})


@worktrees_app.command("reset")
def worktrees_reset(
    path: str,
    branch: Annotated[str, typer.Option("--branch", help="Default branch/ref to reset from later.")] = "main",
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    _emit_worktree_unsupported("reset", output, target=path, project=project, requested={"path": path, "branch": branch})


@pty_app.command("list")
def pty_list(project: ProjectOption = Path("."), output: OutputOption = OutputFormat.TEXT) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    payload = _pty_session_projection()
    if output == OutputFormat.JSON:
        _emit_json(payload)
        return
    typer.echo(f"Policy boundary: {payload['policy_boundary']['kind']}")
    typer.echo(f"Blocked reasons: {','.join(payload['blocked_reasons'])}")
    _print_tsv(["pty_id", "status", "shell", "process_started"])
    for session in payload["sessions"]:
        _print_tsv_row(
            [
                session.get("id", ""),
                session.get("status", ""),
                session.get("shell", ""),
                payload["process_started"],
            ]
        )
    if not payload["sessions"]:
        _print_tsv_row(["none", "", "", payload["process_started"]])
    typer.echo("PTY diagnostics are metadata only; no terminal process was started.")


@pty_app.command("shells")
def pty_shells(project: ProjectOption = Path("."), output: OutputOption = OutputFormat.TEXT) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    payload = _pty_shell_projection()
    if output == OutputFormat.JSON:
        _emit_json(payload)
        return
    typer.echo(f"Policy boundary: {payload['policy_boundary']['kind']}")
    typer.echo(f"Blocked reasons: {','.join(payload['blocked_reasons'])}")
    _print_tsv(["path", "exists", "acceptable", "probed"])
    for shell in payload["shells"]:
        _print_tsv_row([shell["path"], shell["exists"], shell["acceptable"], payload["probed"]])
    typer.echo("Shell candidates are not probed or accepted until PTY policy gates are implemented.")


@pty_app.command("restoration")
def pty_restoration(
    pty_id: Annotated[str | None, typer.Option("--pty", help="Optional PTY id to inspect for restoration readiness.")] = None,
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    store = SQLiteStore(project_root)
    payload = _pty_restoration_readiness_projection(store, pty_id=pty_id)
    if output == OutputFormat.JSON:
        _emit_json(payload)
        return
    _print_tsv(["field", "value"])
    _print_tsv_row(["ready", payload["ready"]])
    _print_tsv_row(["pty_id", payload.get("pty_id") or ""])
    _print_tsv_row(["events", payload["event_count"]])
    _print_tsv_row(["output_events", payload["output_event_count"]])
    _print_tsv_row(["artifact_refs", payload["artifact_ref_count"]])
    _print_tsv_row(["policy_boundary", payload["policy_boundary"]["kind"]])
    _print_tsv_row(["blocked_reasons", ",".join(payload["blocked_reasons"])])
    if payload["missing_events"]:
        _print_tsv_row(["missing_events", ",".join(payload["missing_events"])])
    typer.echo("Blockers:")
    for blocker in payload["blockers"]:
        typer.echo(f"- {blocker['code']}: {blocker['message']}")
    typer.echo("PTY restoration readiness is diagnostic only; no terminal process, live stream, or artifact content read was started.")


@pty_app.command("tabs")
def pty_tabs(
    pty_id: Annotated[str | None, typer.Option("--pty", help="Optional PTY id to project as a terminal tab.")] = None,
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    store = SQLiteStore(project_root)
    payload = _pty_terminal_tabs_projection(store, pty_id=pty_id)
    if output == OutputFormat.JSON:
        _emit_json(payload)
        return
    typer.echo(f"Policy boundary: {payload['policy_boundary']['kind']}")
    typer.echo(f"Blocked reasons: {','.join(payload['blocked_reasons'])}")
    if not payload["tabs"]:
        typer.echo("No terminal tabs found.")
        typer.echo("Terminal tab projection is diagnostic only; no PTY process or live stream was started.")
        return
    _print_tsv(["pty_id", "title", "status", "events", "output_events", "restoration_ready"])
    for tab in payload["tabs"]:
        _print_tsv_row(
            [
                tab["id"],
                tab["title"],
                tab["status"],
                tab["event_count"],
                tab["output_event_count"],
                tab["restoration_ready"],
            ]
        )
    typer.echo("Terminal tab projection is diagnostic only; no PTY process, live stream, or artifact content read was started.")


@pty_app.command("create")
def pty_create(
    command: Annotated[str | None, typer.Option("--command", help="Requested terminal command.")] = None,
    shell: Annotated[str | None, typer.Option("--shell", help="Requested shell path.")] = None,
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    _emit_pty_unsupported("create", output, project=project, requested={"command": command, "shell": shell})


@pty_app.command("write")
def pty_write(
    pty_id: str,
    data: Annotated[str, typer.Option("--data", help="Input data to write later.")] = "",
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    _emit_pty_unsupported("write", output, project=project, pty_id=pty_id, requested={"data": data})


@pty_app.command("resize")
def pty_resize(
    pty_id: str,
    cols: Annotated[int, typer.Option("--cols", help="Requested terminal columns.")] = 80,
    rows: Annotated[int, typer.Option("--rows", help="Requested terminal rows.")] = 24,
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    _emit_pty_unsupported("resize", output, project=project, pty_id=pty_id, requested={"cols": cols, "rows": rows})


@pty_app.command("close")
def pty_close(
    pty_id: str,
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    _emit_pty_unsupported("close", output, project=project, pty_id=pty_id)


@dev_loop_app.command("status")
def dev_loop_status(
    session_id: Annotated[str | None, typer.Option("--session", help="Optional session id for diff/snapshot/revert readiness.")] = None,
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    store = SQLiteStore(project_root)
    cfg = load_config(project_root)
    try:
        payload = _dev_loop_status_projection(store, project_root, cfg, session_id=session_id)
    except KeyError as exc:
        _emit_session_error(str(exc).strip("'"), output)
        raise typer.Exit(code=1) from exc
    if output == OutputFormat.JSON:
        _emit_json(payload)
        return
    _print_tsv(["field", "value"])
    _print_tsv_row(["policy_boundary", payload["policy_boundary"]["kind"]])
    _print_tsv_row(["blocked_reasons", ",".join(payload["blocked_reasons"])])
    _print_tsv(["surface", "available_or_count", "mutation_supported", "process_started", "filesystem_modified"])
    _print_tsv_row(
        [
            "pty",
            payload["pty"]["session_count"],
            payload["pty"]["managed_pty_supported"],
            payload["pty"]["process_started"],
            False,
        ]
    )
    terminal_tabs = payload.get("terminal_tabs") or {}
    _print_tsv_row(
        [
            "terminal_tabs",
            (
                f'tabs={terminal_tabs.get("tab_count", 0)},'
                f'output={terminal_tabs.get("output_event_count", 0)},'
                f'artifacts={terminal_tabs.get("artifact_ref_count", 0)}'
            ),
            terminal_tabs.get("terminal_tabs_supported", False),
            terminal_tabs.get("process_started", False),
            False,
        ]
    )
    _print_tsv_row(
        [
            "terminal_policy",
            (terminal_tabs.get("policy_boundary") or {}).get("kind") or "unknown",
            terminal_tabs.get("terminal_control_supported", False),
            terminal_tabs.get("websocket_opened", False),
            False,
        ]
    )
    _print_tsv_row(
        [
            "terminal_blockers",
            ",".join(terminal_tabs.get("blocked_reasons") or ["none"]),
            False,
            False,
            False,
        ]
    )
    _print_tsv_row(
        [
            "worktrees",
            payload["worktrees"]["worktree_count"] if payload["worktrees"]["available"] else "unavailable",
            payload["worktrees"]["mutation_supported"],
            payload["worktrees"]["process_started"],
            False,
        ]
    )
    session = payload.get("session")
    if session:
        _print_tsv_row(
            [
                "session",
                f'diffs={session["diff_artifact_count"]},files={session["changed_file_count"]}',
                session["revert_supported"],
                False,
                session["filesystem_modified"],
            ]
        )
    typer.echo("Dev-loop diagnostics are metadata-only; no PTY, worktree mutation, revert, unrevert, or hunk apply was started.")


@pr_app.command("checkout")
def pr_checkout(
    pr: str,
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    _emit_pr_unsupported("checkout", output, project=project, requested={"pr": pr})


@pr_app.command("run")
def pr_run(
    pr: str,
    adapter: Annotated[str | None, typer.Option("--adapter", help="Adapter to run after checkout later.")] = None,
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    _emit_pr_unsupported("run", output, project=project, requested={"pr": pr, "adapter": adapter})


@distribution_app.command("status")
def distribution_status(project: ProjectOption = Path("."), output: OutputOption = OutputFormat.TEXT) -> None:
    project_root = resolve_project_root(project)
    payload = _distribution_status_projection(project_root)
    if output == OutputFormat.JSON:
        _emit_json(payload)
        return
    _print_tsv(["field", "value"])
    _print_tsv_row(["version", payload["version"]])
    _print_tsv_row(["packaging_path", payload["packaging_path"]])
    _print_tsv_row(["python_executable", payload["python_executable"]])
    _print_tsv_row(["local_development_install_supported", payload["local_development_install_supported"]])
    typer.echo("Distribution status is diagnostic only; no files were modified.")


@distribution_app.command("version-check")
def distribution_version_check(output: OutputOption = OutputFormat.TEXT) -> None:
    payload = _version_check_projection()
    if output == OutputFormat.JSON:
        _emit_json(payload)
        return
    _print_tsv(["field", "value"])
    _print_tsv_row(["current_version", payload["current_version"]])
    _print_tsv_row(["latest_version", payload["latest_version"] or "unknown"])
    _print_tsv_row(["update_available", payload["update_available"]])
    typer.echo(payload["reason"])


@distribution_app.command("install")
def distribution_install(
    target: Annotated[str | None, typer.Option("--target", help="Requested install target for future implementations.")] = None,
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    _emit_distribution_unsupported("install", output, requested={"target": target})


@distribution_app.command("upgrade")
def distribution_upgrade(
    version: Annotated[str | None, typer.Option("--version", help="Requested version for future implementations.")] = None,
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    _emit_distribution_unsupported("upgrade", output, requested={"version": version})


@distribution_app.command("uninstall")
def distribution_uninstall(
    confirm: Annotated[str | None, typer.Option("--confirm", help="Future destructive confirmation token.")] = None,
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    _emit_distribution_unsupported("uninstall", output, requested={"confirm": confirm})


@distribution_app.command("desktop-status")
def distribution_desktop_status(output: OutputOption = OutputFormat.TEXT) -> None:
    payload = _desktop_status_projection()
    if output == OutputFormat.JSON:
        _emit_json(payload)
        return
    _print_kv("Packaging decision", payload["packaging_decision"])
    _print_kv("Desktop wrapper supported", payload["desktop_wrapper_supported"])
    _print_kv("Launch supported", payload["launch_supported"])
    _print_kv("Requires local server", payload["requires_local_server"])


@distribution_app.command("desktop-launch")
def distribution_desktop_launch(output: OutputOption = OutputFormat.TEXT) -> None:
    payload = _desktop_action_unsupported("launch", {})
    if output == OutputFormat.JSON:
        _emit_json(payload)
        raise typer.Exit(code=1)
    typer.echo(payload["error"])
    raise typer.Exit(code=1)


@distribution_app.command("packaging-smoke")
def distribution_packaging_smoke(
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    project_root = resolve_project_root(project)
    payload = _packaging_smoke_projection(project_root)
    if output == OutputFormat.JSON:
        _emit_json(payload)
        return
    _print_kv("Packaging path", payload["packaging_path"])
    _print_kv("Wheel smoke supported", payload["wheel_smoke_supported"])
    _print_kv("Execution supported", payload["execution_supported"])
    for command in payload["commands"]:
        typer.echo(command)


@distribution_app.command("packaging-smoke-run")
def distribution_packaging_smoke_run(
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    project_root = resolve_project_root(project)
    payload = _packaging_smoke_action_unsupported({"project_root": str(project_root)})
    if output == OutputFormat.JSON:
        _emit_json(payload)
        raise typer.Exit(code=1)
    typer.echo(payload["error"])
    raise typer.Exit(code=1)


@settings_app.command("tui")
def settings_tui(output: OutputOption = OutputFormat.TEXT) -> None:
    payload = build_tui_settings_catalog()
    if output == OutputFormat.JSON:
        _emit_json(payload)
        return
    _print_tsv(["key", "default", "choices"])
    for setting in payload["settings"]:
        _print_tsv_row([setting["key"], setting["default"], ",".join(str(choice) for choice in setting.get("choices", []))])
    typer.echo("TUI settings catalog is metadata only; use session preferences to persist per-session values.")


@sessions_app.command("preferences")
def sessions_preferences(
    session_id: str,
    set_values: Annotated[
        list[str] | None,
        typer.Option("--set", help="Persist a supported TUI preference as key=value. Repeatable."),
    ] = None,
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    store = SQLiteStore(project_root)
    try:
        session = store.get_session(session_id)
        requested = _parse_key_value_options(set_values or [])
        if requested:
            normalized = normalize_tui_preferences({**session.ui_preferences, **requested})
            session = store.update_session_ui_preferences(session_id, normalized)
    except KeyError as exc:
        _emit_session_error(str(exc).strip("'"), output)
        raise typer.Exit(code=1) from exc
    except ValueError as exc:
        _emit_session_error(str(exc), output)
        raise typer.Exit(code=1) from exc
    payload = {
        "schema_version": "harness.session_preferences/v1",
        "ok": True,
        "session_id": session.id,
        "preferences": normalize_tui_preferences(session.ui_preferences),
        "settings": build_tui_settings_catalog(session.ui_preferences, source="active_session", session_id=session.id),
        "updated": bool(set_values),
        "permission_granting": False,
    }
    if output == OutputFormat.JSON:
        _emit_json(payload)
        return
    _print_tsv(["key", "value"])
    for key, value in payload["preferences"].items():
        _print_tsv_row([key, value])


@sessions_app.command("mentions")
def sessions_mentions(
    session_id: str,
    prompt: Annotated[str, typer.Argument(help="Prompt text containing @file, @directory, @reference, or @session mentions.")],
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    store = SQLiteStore(project_root)
    cfg = load_config(project_root)
    try:
        payload = _route_post(
            f"/sessions/{session_id}/mentions/resolve",
            body={"prompt": prompt},
            project_root=project_root,
            store=store,
            cfg=cfg,
            host="127.0.0.1",
            port=8765,
        )
    except (KeyError, ValueError) as exc:
        _emit_session_error(str(exc).strip("'"), output)
        raise typer.Exit(code=1) from exc
    if output == OutputFormat.JSON:
        _emit_json(payload)
        return
    _print_tsv(["kind", "target", "resolved", "bytes", "tokens"])
    for mention in payload["mentions"]:
        _print_tsv_row(
            [
                mention["kind"],
                mention["target"],
                mention["resolved"],
                mention.get("size_bytes", ""),
                mention.get("estimated_tokens", ""),
            ]
        )
    typer.echo("Mention resolution is persisted as a session event; contents are not included.")


@sessions_app.command("attachments")
def sessions_attachments(
    session_id: str,
    paths: Annotated[
        list[Path] | None,
        typer.Option("--file", help="Prepare a file attachment reference for the session. Repeatable."),
    ] = None,
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    store = SQLiteStore(project_root)
    cfg = load_config(project_root)
    try:
        payload = _route_post(
            f"/sessions/{session_id}/attachments",
            body={"paths": [str(path) for path in paths or []]},
            project_root=project_root,
            store=store,
            cfg=cfg,
            host="127.0.0.1",
            port=8765,
        )
    except (KeyError, ValueError) as exc:
        _emit_session_error(str(exc).strip("'"), output)
        raise typer.Exit(code=1) from exc
    if output == OutputFormat.JSON:
        _emit_json(payload)
        return
    _print_tsv(["path", "content_type", "bytes", "tokens", "accepted", "overflow"])
    for attachment in payload["attachments"]:
        _print_tsv_row(
            [
                attachment["path"],
                attachment["content_type"],
                attachment["size_bytes"],
                attachment["estimated_tokens"],
                attachment["accepted"],
                attachment["requires_artifact_overflow"],
            ]
        )
    typer.echo("Attachment preparation records metadata only; contents are not included.")


@sessions_app.command("context-estimate")
def sessions_context_estimate(
    session_id: str,
    prompt: Annotated[str, typer.Argument(help="Prompt text to estimate with mentions and attachments.")],
    files: Annotated[
        list[Path] | None,
        typer.Option("--file", help="Include a file attachment in the estimate. Repeatable."),
    ] = None,
    include_instructions: Annotated[
        bool,
        typer.Option("--include-instructions", help="Include discovered instruction-file metadata in the estimate."),
    ] = False,
    budget_tokens: Annotated[int | None, typer.Option("--budget-tokens", help="Optional token budget for within-budget reporting.")] = None,
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    store = SQLiteStore(project_root)
    cfg = load_config(project_root)
    body = {
        "prompt": prompt,
        "attachment_paths": [str(path) for path in files or []],
        "include_instructions": include_instructions,
        "budget_tokens": budget_tokens,
    }
    try:
        payload = _route_post(
            f"/sessions/{session_id}/context/estimate",
            body=body,
            project_root=project_root,
            store=store,
            cfg=cfg,
            host="127.0.0.1",
            port=8765,
        )
    except (KeyError, ValueError) as exc:
        _emit_session_error(str(exc).strip("'"), output)
        raise typer.Exit(code=1) from exc
    if output == OutputFormat.JSON:
        _emit_json(payload)
        return
    _print_kv("Total bytes", payload["total_bytes"])
    _print_kv("Estimated tokens", payload["total_estimated_tokens"])
    _print_kv("Budget tokens", payload["budget_tokens"] if payload["budget_tokens"] is not None else "none")
    _print_kv("Within budget", payload["within_budget"] if payload["within_budget"] is not None else "unknown")
    _print_tsv(["kind", "bytes", "tokens", "contents"])
    for item in payload["items"]:
        _print_tsv_row(
            [
                item["kind"],
                item.get("size_bytes", ""),
                item.get("estimated_tokens", ""),
                item.get("contents_included", False),
            ]
        )
    typer.echo("Context estimates are metadata-only and persisted as session events.")


@commands_app.command("list")
def commands_list(project: ProjectOption = Path("."), output: OutputOption = OutputFormat.TEXT) -> None:
    project_root = resolve_project_root(project)
    payload = build_command_catalog(project_root)
    if output == OutputFormat.JSON:
        _emit_json(payload)
        return
    _print_tsv(["slash", "title", "path", "execution_supported"])
    for command in payload["commands"]:
        _print_tsv_row([command["slash"], command["title"], command["path"], command["execution_supported"]])
    if not payload["commands"]:
        typer.echo("No project command templates found.")


@commands_app.command("run")
def commands_run(
    name: str,
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    project_root = resolve_project_root(project)
    payload = build_command_catalog(project_root)
    command = next((item for item in payload["commands"] if item["name"] == name or item["id"] == name), None)
    action = command_action_unsupported("run", command["id"] if command else name, {"name": name})
    if output == OutputFormat.JSON:
        _emit_json(action)
        raise typer.Exit(code=1)
    typer.echo(action["error"])
    raise typer.Exit(code=1)


@workspaces_app.command("list")
def workspaces_list(project: ProjectOption = Path("."), output: OutputOption = OutputFormat.TEXT) -> None:
    project_root = resolve_project_root(project)
    payload = build_workspace_catalog(project_root)
    if output == OutputFormat.JSON:
        _emit_json(payload)
        return
    _print_tsv(["id", "current", "initialized", "path"])
    for workspace in payload["workspaces"]:
        _print_tsv_row([workspace["id"], workspace["current"], workspace["initialized"], workspace["path"]])


@workspaces_app.command("current")
def workspaces_current(project: ProjectOption = Path("."), output: OutputOption = OutputFormat.TEXT) -> None:
    project_root = resolve_project_root(project)
    payload = build_workspace_catalog(project_root)
    current = payload["workspaces"][0]
    if output == OutputFormat.JSON:
        _emit_json({"schema_version": "harness.workspace/v1", "ok": True, "workspace": current})
        return
    _print_kv("Workspace id", current["id"])
    _print_kv("Path", current["path"])
    _print_kv("Initialized", current["initialized"])


@workspaces_app.command("clients")
def workspaces_clients(project: ProjectOption = Path("."), output: OutputOption = OutputFormat.TEXT) -> None:
    project_root = resolve_project_root(project)
    payload = build_workspace_clients_projection(project_root)
    if output == OutputFormat.JSON:
        _emit_json(payload)
        return
    _print_tsv(["client_id", "kind", "active"])
    if not payload["clients"]:
        _print_tsv_row(["none", "", False])


@workspaces_app.command("attach")
def workspaces_attach(
    workspace_id: str,
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    resolve_project_root(project)
    payload = workspace_action_unsupported("attach", workspace_id, {"workspace_id": workspace_id})
    if output == OutputFormat.JSON:
        _emit_json(payload)
        raise typer.Exit(code=1)
    typer.echo(payload["error"])
    raise typer.Exit(code=1)


@workspaces_app.command("steal")
def workspaces_steal(
    workspace_id: str,
    client_id: Annotated[str | None, typer.Option("--client", help="Client id to steal from when implemented.")] = None,
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    resolve_project_root(project)
    payload = workspace_action_unsupported("steal", workspace_id, {"workspace_id": workspace_id, "client_id": client_id})
    if output == OutputFormat.JSON:
        _emit_json(payload)
        raise typer.Exit(code=1)
    typer.echo(payload["error"])
    raise typer.Exit(code=1)


@workspaces_app.command("dispose")
def workspaces_dispose(
    workspace_id: str,
    client_id: Annotated[str | None, typer.Option("--client", help="Client id to dispose when implemented.")] = None,
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    resolve_project_root(project)
    payload = workspace_action_unsupported("dispose", workspace_id, {"workspace_id": workspace_id, "client_id": client_id})
    if output == OutputFormat.JSON:
        _emit_json(payload)
        raise typer.Exit(code=1)
    typer.echo(payload["error"])
    raise typer.Exit(code=1)


@workspaces_app.command("sync")
def workspaces_sync(
    workspace_id: str,
    project: ProjectOption = Path("."),
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    resolve_project_root(project)
    payload = workspace_action_unsupported("sync", workspace_id, {"workspace_id": workspace_id})
    if output == OutputFormat.JSON:
        _emit_json(payload)
        raise typer.Exit(code=1)
    typer.echo(payload["error"])
    raise typer.Exit(code=1)


@server_app.command("lifecycle")
def server_lifecycle(
    project: ProjectOption = Path("."),
    host: Annotated[str, typer.Option("--host", help="Local server host to describe.")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", help="Local server port to describe.")] = 8765,
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    project_root = resolve_project_root(project)
    payload = _server_lifecycle_projection(project_root, host=host, port=port)
    if output == OutputFormat.JSON:
        _emit_json(payload)
        return
    _print_kv("Server URL", payload["server_url"])
    _print_kv("Dispose supported", payload["dispose_supported"])
    _print_kv("mDNS supported", payload["mdns_supported"])
    _print_kv("WebSocket supported", payload["websocket_supported"])


@server_app.command("mdns")
def server_mdns(
    host: Annotated[str, typer.Option("--host", help="Local server host to describe.")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", help="Local server port to describe.")] = 8765,
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    payload = _server_mdns_projection(host=host, port=port)
    if output == OutputFormat.JSON:
        _emit_json(payload)
        return
    _print_kv("Advertised", payload["advertised"])
    _print_kv("Service type", payload["service_type"])
    _print_kv("LAN discovery supported", payload["lan_discovery_supported"])


@server_app.command("dispose")
def server_dispose(output: OutputOption = OutputFormat.TEXT) -> None:
    payload = _server_dispose_unsupported({})
    if output == OutputFormat.JSON:
        _emit_json(payload)
        raise typer.Exit(code=1)
    typer.echo(payload["error"])
    raise typer.Exit(code=1)


@app.command()
def serve(
    project: ProjectOption = Path("."),
    host: Annotated[str, typer.Option("--host", help="Local interface to bind.")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", help="Local port to bind.")] = 8765,
    token: Annotated[str | None, typer.Option("--token", help="Bearer token for local API clients.")] = None,
    openapi: Annotated[bool, typer.Option("--openapi", help="Print the OpenAPI document and exit.")] = False,
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    server_url = f"http://{host}:{port}"
    if openapi:
        spec = build_openapi_spec(server_url=server_url)
        if output == OutputFormat.JSON:
            _emit_json(spec)
            return
        typer.echo(json.dumps(spec, indent=2, sort_keys=True))
        return
    resolved_token = token or os.environ.get("HARNESS_SERVER_TOKEN") or generate_server_token()
    if output == OutputFormat.JSON:
        _emit_json(
            {
                "schema_version": "harness.local_server_start/v1",
                "ok": True,
                "url": server_url,
                "auth": "bearer",
                "token_generated": token is None and not os.environ.get("HARNESS_SERVER_TOKEN"),
                "permission_granting": False,
            }
        )
    else:
        typer.echo(f"Harness local server: {server_url}")
        typer.echo("Auth: bearer token")
        if token is None and not os.environ.get("HARNESS_SERVER_TOKEN"):
            typer.echo(f"Generated token: {resolved_token}")
    serve_local_http(project_root, host=host, port=port, token=resolved_token)


@app.command()
def attach(
    server_url: Annotated[str, typer.Option("--server-url", help="Existing Harness local server URL.")] = "http://127.0.0.1:8765",
    token: Annotated[str | None, typer.Option("--token", help="Bearer token for the existing local server.")] = None,
    output: OutputOption = OutputFormat.TEXT,
) -> None:
    """Read-only attachment probe for an already-running Harness local server."""
    resolved_token = token or os.environ.get("HARNESS_SERVER_TOKEN")
    if not resolved_token:
        raise typer.BadParameter("Missing server token. Pass --token or set HARNESS_SERVER_TOKEN.")
    try:
        health = _local_server_get_json(server_url, "/health", resolved_token)
        sessions = _local_server_get_json(server_url, "/sessions", resolved_token)
        openapi = _local_server_get_json(server_url, "/openapi.json", resolved_token)
    except HTTPError as exc:
        raise typer.BadParameter(f"Server rejected attach request with HTTP {exc.code}.") from exc
    except URLError as exc:
        raise typer.BadParameter(f"Could not reach Harness server: {exc.reason}") from exc
    payload = {
        "schema_version": "harness.local_server_attach/v1",
        "ok": True,
        "server_url": server_url.rstrip("/"),
        "health": health,
        "session_count": len(sessions.get("sessions", [])),
        "sessions": sessions.get("sessions", []),
        "openapi_schema_version": openapi.get("info", {}).get("x-harness-schema-version"),
        "permission_granting": False,
    }
    if output == OutputFormat.JSON:
        _emit_json(payload)
        return
    typer.echo(f"Attached to Harness local server: {payload['server_url']}")
    typer.echo(f"Project: {health.get('project_root', '')}")
    typer.echo(f"Sessions: {payload['session_count']}")
    typer.echo(f"OpenAPI schema: {payload['openapi_schema_version']}")
    typer.echo("Mode: read-only projection; no permissions are granted by attach.")


def _local_server_get_json(server_url: str, path: str, token: str) -> dict[str, object]:
    request = Request(server_url.rstrip("/") + path)
    request.add_header("Authorization", f"Bearer {token}")
    with urlopen(request, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


@app.command()
def run(
    goal: str,
    task_type: Annotated[str, typer.Option("--task-type", help="Task type route. Use auto for product routing.")] = "auto",
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
    if task_type == "auto":
        if route_instruction(goal).intent != "unsupported":
            result = _run_product_session(goal, project_root, output=output)
            raise typer.Exit(code=0 if result.get("ok") or result.get("status") == "waiting_approval" else 1)
        task_type = "codex_direct_agent"
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


@app.command("run-live")
def run_live(
    task_file: Annotated[Path, typer.Option("--task-file", help="Task prompt file.")],
    agent: Annotated[str, typer.Option("--agent", help="Agent id for the live run.")] = "code_editor",
    project: ProjectOption = Path("."),
    stream: Annotated[StreamFormat, typer.Option("--stream", help="Stream format.")] = StreamFormat.HUMAN,
) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    path = task_file if task_file.is_absolute() else project_root / task_file
    if not path.exists() or not path.is_file():
        raise typer.BadParameter(f"Task file not found: {task_file}")
    result = _create_policy_first_live_run(
        project_root=project_root,
        goal=path.read_text(encoding="utf-8"),
        task_type="codex_code_edit",
        agent=agent,
        task_id=None,
        task_file=path,
    )
    _emit_live_stream(project_root, result["run_id"], stream)
    if stream != StreamFormat.JSONL:
        typer.echo(f"Run: {result['run_id']}")
        typer.echo(f"Status: {result['status']}")
        typer.echo(f"Artifacts: {result['run_dir']}")


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
    SQLiteStore(project_root).initialize()


_NATIVE_AGENT_ALIASES = {"build", "plan", "general", "explore"}


def _resolve_foreground_agent_selection(prompt: str, agent_id: str | None) -> tuple[str, str | None]:
    match = re.search(r"(?<!\w)@([A-Za-z][A-Za-z0-9_-]*)\b", prompt)
    mention_agent = match.group(1) if match else None
    if agent_id is not None and mention_agent is not None and agent_id != mention_agent:
        raise typer.BadParameter(f"--agent {agent_id} conflicts with prompt mention @{mention_agent}.")
    resolved_agent = agent_id or mention_agent
    if match is None:
        return prompt, resolved_agent
    cleaned = (prompt[: match.start()] + prompt[match.end() :]).strip()
    return cleaned or prompt, resolved_agent


def _run_native_agent_alias_session(
    goal: str,
    project_root: Path,
    *,
    agent_id: str,
    output: OutputFormat,
    model: str | None = None,
    session_id: str | None = None,
    continue_session: bool = False,
    fork_session: bool = False,
    title: str | None = None,
    file_refs: list[Path] | None = None,
) -> dict:
    _require_initialized(project_root)
    store = SQLiteStore(project_root)
    cfg = load_config(project_root)
    session = _resolve_prompt_session(
        store,
        session_id=session_id,
        continue_session=continue_session,
        fork_session=fork_session,
        title=title,
        agent_id=agent_id,
        raw_model_ref=model,
        goal=goal,
    )
    user_message = store.append_session_message(session.id, SessionMessageRole.USER, goal, agent_id=agent_id)
    store.append_session_part(session.id, user_message.id, SessionPartKind.TEXT, text=goal, redaction_state=RedactionState.REDACTED)
    for file_ref in file_refs or []:
        store.append_session_part(
            session.id,
            user_message.id,
            SessionPartKind.ARTIFACT_REF,
            metadata={
                "attachment_kind": "file_ref",
                "path": str(file_ref),
                "resolved_path": str((project_root / file_ref).resolve() if not file_ref.is_absolute() else file_ref.resolve()),
            },
            redaction_state=RedactionState.NOT_REQUIRED,
        )
    if model is not None:
        validation = validate_model_selection(cfg, model)
        validation_payload = validation.model_dump(mode="json")
        store.append_store_event(
            EventStreamType.SESSION,
            session.id,
            "session.model_validation",
            {
                **validation_payload,
                "summary": "Model selection validated." if validation.executable else "Model selection blocked before task creation.",
            },
            session_id=session.id,
            message_id=user_message.id,
            redaction_state=RedactionState.NOT_REQUIRED,
        )
        if not validation.executable:
            store.append_session_part(
                session.id,
                user_message.id,
                SessionPartKind.SUMMARY,
                text="Model selection blocked before task creation.",
                metadata={"status": "model_validation_failed", "validation": validation_payload},
                redaction_state=RedactionState.NOT_REQUIRED,
            )
            result = {
                "schema_version": "harness.native_agent_session/v1",
                "ok": False,
                "status": "model_validation_failed",
                "session": store.get_session(session.id).model_dump(mode="json"),
                "task": None,
                "agent": {"agent_id": agent_id},
                "model_validation": validation_payload,
                "no_hidden_fallback": True,
                "provider_execution_started": False,
                "model_execution_started": False,
                "hidden_provider_fallback": False,
                "hidden_model_fallback": False,
                "permission_granting": False,
                "authority_granting": False,
            }
            if output == OutputFormat.JSON:
                _emit_json(result)
                raise typer.Exit(code=1)
            typer.echo("Model selection blocked before task creation.")
            for reason in validation.blocked_reasons:
                typer.echo(f"  - {reason}")
            raise typer.Exit(code=1)

    alias_config = _native_agent_alias_config(agent_id)
    if agent_id in {"general", "explore"}:
        return _run_native_subagent_branch(
            store,
            parent_session=session,
            parent_message_id=user_message.id,
            goal=goal,
            agent_id=agent_id,
            alias_config=alias_config,
            output=output,
        )
    objective = store.create_objective(
        title=f"{agent_id} session",
        description=f"Session-requested {agent_id} workflow: {goal}",
        priority=1000,
        workbench_id="coding",
        metadata={"intent": alias_config["intent"], "agent_alias": agent_id},
        session_id=session.id,
    )
    task = store.create_task(
        title=f"{agent_id}: {goal[:80]}",
        description=goal,
        priority=1000,
        objective_id=objective.id,
        workbench_id="coding",
        agent_id=agent_id,
        metadata=alias_config["metadata"],
        required_approvals=alias_config["required_approvals"],
        session_id=session.id,
    )
    store.attach_session_to_objective(session.id, objective.id)
    store.attach_session_to_task(session.id, task.id)
    session_status = SessionStatus.WAITING_APPROVAL if task.status == TaskStatus.WAITING_APPROVAL else SessionStatus.IDLE
    session = store.update_session(
        session.id,
        status=session_status,
        objective_id=objective.id,
        active_task_id=task.id,
    )
    store.append_store_event(
        EventStreamType.SESSION,
        session.id,
        "agent.selected",
        {
            "agent_id": agent_id,
            "mode": alias_config["mode"],
            "execution_adapter": alias_config["metadata"]["execution_adapter"],
            "task_type": alias_config["metadata"]["task_type"],
            "summary": alias_config["summary"],
        },
        session_id=session.id,
        task_id=task.id,
        redaction_state=RedactionState.NOT_REQUIRED,
    )
    if task.status == TaskStatus.WAITING_APPROVAL:
        store.append_store_event(
            EventStreamType.SESSION,
            session.id,
            "run.blocked",
            {
                "summary": "Hosted-boundary approval is required before the isolated agent task can run.",
                "required_approvals": alias_config["required_approvals"],
            },
            session_id=session.id,
            task_id=task.id,
            redaction_state=RedactionState.NOT_REQUIRED,
        )

    result = {
        "schema_version": "harness.native_agent_session/v1",
        "ok": True,
        "status": session.status.value,
        "session": session.model_dump(mode="json"),
        "task": task.model_dump(mode="json"),
        "agent": alias_config,
        "next_actions": _native_agent_next_actions(session.id, task.id, task.status),
    }
    if output == OutputFormat.JSON:
        _emit_json(result)
        return result
    typer.echo(f"Session: {session.id}")
    typer.echo(f"Agent: {agent_id}")
    typer.echo(f"Task: {task.id}")
    typer.echo(f"Status: {session.status.value}")
    typer.echo(f"Execution adapter: {alias_config['metadata']['execution_adapter']}")
    typer.echo("Next:")
    for action in result["next_actions"]:
        typer.echo(f"  {action}")
    return result


def _run_native_subagent_branch(
    store: SQLiteStore,
    *,
    parent_session,
    parent_message_id: str,
    goal: str,
    agent_id: str,
    alias_config: dict,
    output: OutputFormat,
) -> dict:
    child = store.fork_session(
        parent_session.id,
        message_id=parent_message_id,
        title=f"{agent_id}: {goal[:80]}",
        metadata={
            "subagent": True,
            "subagent_id": agent_id,
            "parent_session_id": parent_session.id,
            "parent_message_id": parent_message_id,
            "parallelizable": True,
            "bounded_read_only": True,
        },
    )
    child = store.update_session(
        child.id,
        agent_id=agent_id,
        mode=alias_config["mode"],
        intent=alias_config["intent"],
        status=SessionStatus.RUNNING,
    )
    child_user_message = store.append_session_message(child.id, SessionMessageRole.USER, goal, agent_id=agent_id)
    store.append_session_part(
        child.id,
        child_user_message.id,
        SessionPartKind.TEXT,
        text=goal,
        redaction_state=RedactionState.REDACTED,
    )
    objective = store.create_objective(
        title=f"{agent_id} subagent",
        description=f"Bounded read-only subagent request: {goal}",
        priority=1000,
        workbench_id="coding",
        metadata={"intent": alias_config["intent"], "agent_alias": agent_id, "parent_session_id": parent_session.id},
        session_id=child.id,
    )
    task = store.create_task(
        title=f"{agent_id}: {goal[:80]}",
        description=goal,
        priority=1000,
        objective_id=objective.id,
        workbench_id="coding",
        agent_id=agent_id,
        metadata={**alias_config["metadata"], "parent_session_id": parent_session.id},
        required_approvals=[],
        session_id=child.id,
    )
    store.attach_session_to_objective(child.id, objective.id)
    store.attach_session_to_task(child.id, task.id)
    run = store.create_run(
        goal=goal,
        task_type=alias_config["metadata"]["task_type"],
        status="completed",
        task_id=task.id,
        objective_id=objective.id,
        session_id=child.id,
    )
    artifact_paths = store.initialize_run_artifacts(run.id)
    summary_path = artifact_paths["final_report"]
    summary_path.write_text(
        "\n".join(
            [
                f"# {agent_id} bounded research summary",
                "",
                f"Request: {goal}",
                "",
                "This Phase 3 native subagent branch is intentionally bounded to persisted session state, read/glob/grep/artifact-read tool policy, and artifact-backed evidence.",
                "No shell, network, provider execution, or active workspace edits were started.",
                "",
                "Next: run the linked task through a registered Harness read-only adapter when expanded execution is enabled.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    artifact = store.register_artifact(
        run.id,
        kind="subagent_summary",
        path=summary_path,
        metadata={
            "agent_id": agent_id,
            "parent_session_id": parent_session.id,
            "parent_message_id": parent_message_id,
            "allowed_tools": alias_config["metadata"]["allowed_tools"],
            "preview_max_bytes": 16 * 1024,
            "event_payload_max_bytes": 64 * 1024,
            "content_type": "text/markdown",
        },
        producer="harness_native_agent_alias",
        redaction_state=RedactionState.NOT_REQUIRED.value,
        session_id=child.id,
    )
    assistant_text = f"{agent_id} completed a bounded read-only branch. Summary artifact: {artifact.id}"
    assistant_message = store.append_session_message(
        child.id,
        SessionMessageRole.ASSISTANT,
        assistant_text,
        agent_id=agent_id,
        run_id=run.id,
    )
    store.append_session_part(
        child.id,
        assistant_message.id,
        SessionPartKind.SUMMARY,
        text=assistant_text,
        metadata={"artifact_id": artifact.id, "run_id": run.id},
        redaction_state=RedactionState.NOT_REQUIRED,
    )
    store.append_session_part(
        child.id,
        assistant_message.id,
        SessionPartKind.ARTIFACT_REF,
        metadata={"artifact_id": artifact.id, "kind": artifact.kind, "path": str(artifact.path), "run_id": run.id},
        redaction_state=RedactionState.NOT_REQUIRED,
    )
    child = store.update_session(
        child.id,
        status=SessionStatus.IDLE,
        objective_id=objective.id,
        active_task_id=task.id,
        active_run_id=run.id,
    )
    store.append_store_event(
        EventStreamType.SESSION,
        parent_session.id,
        "subagent.spawned",
        {
            "summary": f"{agent_id} subagent branch created.",
            "agent_id": agent_id,
            "child_session_id": child.id,
            "task_id": task.id,
            "run_id": run.id,
            "artifact_id": artifact.id,
            "parallelizable": True,
            "provider_execution_started": False,
            "shell_started": False,
            "network_started": False,
            "active_repo_write": "forbidden",
        },
        session_id=parent_session.id,
        message_id=parent_message_id,
        task_id=task.id,
        run_id=run.id,
        artifact_id=artifact.id,
        redaction_state=RedactionState.NOT_REQUIRED,
    )
    store.append_store_event(
        EventStreamType.SESSION,
        child.id,
        "agent.selected",
        {
            "agent_id": agent_id,
            "mode": alias_config["mode"],
            "execution_adapter": alias_config["metadata"]["execution_adapter"],
            "task_type": alias_config["metadata"]["task_type"],
            "summary": alias_config["summary"],
        },
        session_id=child.id,
        task_id=task.id,
        redaction_state=RedactionState.NOT_REQUIRED,
    )
    store.append_store_event(
        EventStreamType.SESSION,
        child.id,
        "subagent.completed",
        {
            "summary": assistant_text,
            "agent_id": agent_id,
            "parent_session_id": parent_session.id,
            "run_id": run.id,
            "artifact_id": artifact.id,
            "bounded_read_only": True,
            "provider_execution_started": False,
            "shell_started": False,
            "network_started": False,
            "active_repo_write": "forbidden",
        },
        session_id=child.id,
        message_id=assistant_message.id,
        task_id=task.id,
        run_id=run.id,
        artifact_id=artifact.id,
        redaction_state=RedactionState.NOT_REQUIRED,
    )
    result = {
        "schema_version": "harness.native_agent_session/v1",
        "ok": True,
        "status": child.status.value,
        "parent_session": store.get_session(parent_session.id).model_dump(mode="json"),
        "session": child.model_dump(mode="json"),
        "task": task.model_dump(mode="json"),
        "run": run.model_dump(mode="json"),
        "artifact": artifact.model_dump(mode="json"),
        "agent": alias_config,
        "subagent_branch": True,
        "provider_execution_started": False,
        "model_execution_started": False,
        "shell_started": False,
        "network_started": False,
        "active_repo_write": "forbidden",
        "next_actions": _native_agent_next_actions(child.id, task.id, task.status),
    }
    if output == OutputFormat.JSON:
        _emit_json(result)
        return result
    typer.echo(f"Parent session: {parent_session.id}")
    typer.echo(f"Subagent session: {child.id}")
    typer.echo(f"Agent: {agent_id}")
    typer.echo(f"Task: {task.id}")
    typer.echo(f"Run: {run.id}")
    typer.echo(f"Summary artifact: {artifact.path}")
    return result


def _native_agent_alias_config(agent_id: str) -> dict:
    if agent_id == "build":
        return {
            "id": "build",
            "intent": "build_change",
            "mode": "codex_edit",
            "summary": "Build uses isolated Codex edit by default; active-workspace direct runs require --mode direct.",
            "required_approvals": ["hosted_provider_codex"],
            "metadata": {
                "execution_adapter": "codex_isolated_edit",
                "task_type": "codex_code_edit",
                "agent_alias": "build",
                "mutation_boundary": "isolated_workspace_apply_back",
                "direct_active_workspace": False,
            },
        }
    if agent_id == "plan":
        return {
            "id": "plan",
            "intent": "plan_change",
            "mode": "planning",
            "summary": "Plan uses a read-only session-local tool loop placeholder and denies active edits.",
            "required_approvals": [],
            "metadata": {
                "execution_adapter": "session_read_tools",
                "task_type": "session_plan",
                "agent_alias": "plan",
                "allowed_tools": ["read", "glob", "grep", "artifact-read"],
                "active_repo_write": "forbidden",
                "external_network": "forbidden",
            },
        }
    return {
        "id": agent_id,
        "intent": f"{agent_id}_research",
        "mode": "read_only",
        "summary": f"{agent_id} is bounded to read-only artifact-backed work until subagent execution lands.",
        "required_approvals": [],
        "metadata": {
            "execution_adapter": "session_read_tools",
            "task_type": "session_read_only_research",
            "agent_alias": agent_id,
            "allowed_tools": ["read", "glob", "grep", "artifact-read"],
            "active_repo_write": "forbidden",
            "external_network": "forbidden",
            "subagent_placeholder": True,
        },
    }


def _native_agent_next_actions(session_id: str, task_id: str, status: TaskStatus) -> list[str]:
    actions = [
        f"inspect session with harness session get {session_id}",
        f"inspect task with harness tasks show {task_id}",
    ]
    if status == TaskStatus.WAITING_APPROVAL:
        actions.insert(
            0,
            "approve hosted boundary with harness approvals add --backend codex_cli --data-boundary hosted_provider --task-type codex_code_edit",
        )
    return actions


def _run_product_session(goal: str, project_root: Path, *, output: OutputFormat) -> dict:
    store = SQLiteStore(project_root)
    route = route_instruction(goal)
    session = store.create_session(
        workbench_id=route.workbench_id,
        agent_id=route.agent_id,
        mode=route.mode.value,
        intent=route.intent,
        metadata={"instruction": goal, "route": route.model_dump(mode="json")},
    )
    append_session_event(
        project_root,
        session_id=session.id,
        event_type=SessionEventKind.SESSION_STARTED,
        message="Session started",
        payload={"instruction": goal},
    )
    route_event = append_session_event(
        project_root,
        session_id=session.id,
        event_type=SessionEventKind.INTENT_ROUTED,
        message=f"Routed as {route.intent}",
        payload=route.model_dump(mode="json"),
    )
    if route.intent == "unsupported":
        session = store.update_session(session.id, status=SessionStatus.FAILED)
        event = append_session_event(
            project_root,
            session_id=session.id,
            event_type=SessionEventKind.SESSION_FAILED,
            message="No safe automatic route matched this instruction",
            level="warning",
            payload={"instruction": goal},
        )
        result = _product_session_payload(project_root, session.id, route, [route_event, event], ok=False)
        _emit_product_session_result(result, output)
        return result

    objective = store.create_objective(
        title=f"Session {route.intent}",
        description=f"Session-requested workflow: {goal}",
        priority=1000,
        workbench_id=route.workbench_id,
        metadata={"intent": route.intent},
        session_id=session.id,
    )
    task = store.create_task(
        title=f"Session task: {route.intent}",
        description=goal,
        priority=1000,
        objective_id=objective.id,
        workbench_id=route.workbench_id,
        agent_id=route.agent_id,
        metadata={"execution_adapter": route.execution_adapter, "task_type": route.task_type, "intent": route.intent},
        required_approvals=["hosted_provider_codex"] if "hosted_provider_codex" in route.required_approvals else [],
        session_id=session.id,
    )
    store.attach_session_to_objective(session.id, objective.id)
    store.attach_session_to_task(session.id, task.id)

    approval = None
    if "hosted_provider_codex" in route.required_approvals:
        approval = ApprovalStore(project_root).find_valid("codex_cli", "hosted_provider", route.task_type)
    if "hosted_provider_codex" in route.required_approvals and approval is None:
        session = store.update_session(
            session.id,
            status=SessionStatus.WAITING_APPROVAL,
            objective_id=objective.id,
            active_task_id=task.id,
        )
        event = append_session_event(
            project_root,
            session_id=session.id,
            objective_id=objective.id,
            task_id=task.id,
            event_type=SessionEventKind.APPROVAL_REQUIRED,
            message="Hosted-boundary approval is required before execution",
            level="warning",
            payload={
                "backend": "codex_cli",
                "data_boundary": "hosted_provider",
                "task_type": route.task_type,
                "command": (
                    "harness approvals add --backend codex_cli --data-boundary hosted_provider "
                    f"--task-type {route.task_type}"
                ),
            },
        )
        result = _product_session_payload(project_root, session.id, route, [route_event, event], ok=True)
        _emit_product_session_result(result, output)
        return result

    run = store.create_run(
        goal=goal,
        task_type=route.task_type,
        status="created",
        task_id=task.id,
        objective_id=objective.id,
        approval_id=approval.id if approval is not None else None,
        session_id=session.id,
    )
    store.attach_session_to_run(session.id, run.id)
    report_path = _write_product_report(store, run.id, instruction=goal, route=route, status="created")
    artifact = store.register_artifact(run.id, "final_report", report_path, producer="product_session")
    event = append_session_event(
        project_root,
        session_id=session.id,
        objective_id=objective.id,
        task_id=task.id,
        run_id=run.id,
        event_type=SessionEventKind.REPORT_READY,
        message="Report ready",
        payload={"report": str(report_path), "artifact_id": artifact.id},
    )
    session = store.update_session(session.id, status=SessionStatus.COMPLETED, active_run_id=run.id)
    complete_event = append_session_event(
        project_root,
        session_id=session.id,
        objective_id=objective.id,
        task_id=task.id,
        run_id=run.id,
        event_type=SessionEventKind.SESSION_COMPLETED,
        message="Session completed",
        payload={"run_id": run.id},
    )
    result = _product_session_payload(project_root, session.id, route, [route_event, event, complete_event], ok=True)
    _emit_product_session_result(result, output)
    return result


def _product_session_payload(project_root: Path, session_id: str, route: IntentRoute, events: list, *, ok: bool) -> dict:
    store = SQLiteStore(project_root)
    session = store.get_session(session_id)
    artifacts = []
    if session.active_run_id:
        artifacts = [artifact.model_dump(mode="json") for artifact in store.verify_artifacts(session.active_run_id)]
    return {
        "schema_version": "harness.product_session/v1",
        "ok": ok,
        "status": session.status.value,
        "session": session.model_dump(mode="json"),
        "route": route.model_dump(mode="json"),
        "transcript_path": str(session_transcript_path(project_root, session.id)),
        "events": [event.model_dump(mode="json") for event in events],
        "artifacts": artifacts,
        "next_actions": _session_next_actions(session),
    }


def _emit_product_session_result(result: dict, output: OutputFormat) -> None:
    if output == OutputFormat.JSON:
        _emit_json(result)
        return
    for event in result["events"]:
        typer.echo(render_session_event(event))
    typer.echo(f"Session: {result['session']['id']}")
    typer.echo(f"Status: {result['status']}")
    typer.echo("Next:")
    for action in result["next_actions"]:
        typer.echo(f"  {action}")


def _session_next_actions(session) -> list[str]:
    if session.status == SessionStatus.WAITING_APPROVAL:
        return [
            "approve hosted boundary with harness approvals add",
            f"resume with harness resume {session.id}",
            f"inspect with harness sessions inspect {session.id}",
        ]
    actions = [f"inspect with harness sessions inspect {session.id}"]
    if session.active_run_id:
        actions.extend(
            [
                f"report with harness report {session.active_run_id}",
                f"artifacts with harness artifacts open {session.active_run_id}",
                f"diff with harness diff {session.active_run_id}",
            ]
        )
    return actions


def _session_model_validation(cfg, session) -> dict | None:
    if not session.raw_model_ref:
        return None
    validation = validate_model_selection(cfg, session.raw_model_ref)
    payload = validation.model_dump(mode="json")
    payload["provider_execution_started"] = False
    payload["model_execution_started"] = False
    payload["hidden_provider_fallback"] = False
    payload["hidden_model_fallback"] = False
    payload["no_hidden_fallback"] = True
    payload["permission_granting"] = False
    payload["authority_granting"] = False
    return payload


def _latest_session_ui_activation(store: SQLiteStore, session_id: str) -> dict | None:
    events = store.list_session_store_events(session_id)
    event = next((item for item in reversed(events) if item.kind == "tui.ui_activation.applied"), None)
    if event is None:
        return None
    payload = event.payload or {}
    action = payload.get("action") or {}
    return {
        "seq": event.seq,
        "event_id": event.id,
        "entry_id": payload.get("entry_id"),
        "source": payload.get("source"),
        "activation_kind": payload.get("activation_kind"),
        "action_type": action.get("type"),
        "evidence_status": payload.get("evidence_status") or "ui_only_persisted",
        "policy_boundary": payload.get("policy_boundary") or {
            "kind": "safe_ui_activation",
            "ui_state_only": True,
            "command_execution_allowed": False,
            "process_start_allowed": False,
            "filesystem_mutation_allowed": False,
            "permission_grant_allowed": False,
            "authority_grant_allowed": False,
        },
        "blocked_reasons": payload.get("blocked_reasons") or [],
        "command_started": bool(payload.get("command_started")),
        "process_started": bool(payload.get("process_started")),
        "filesystem_modified": bool(payload.get("filesystem_modified")),
        "permission_granting": bool(payload.get("permission_granting")),
        "authority_granting": bool(payload.get("authority_granting")),
    }


def _write_product_report(
    store: SQLiteStore,
    run_id: str,
    *,
    instruction: str,
    route: IntentRoute | None,
    status: str,
) -> Path:
    run = store.get_run(run_id)
    report_path = store.runs_dir / run_id / "final_report.md"
    route_name = route.intent if route is not None else (run.task_type or "unknown")
    mode = route.mode.value if route is not None else "unknown"
    expected_outputs = route.expected_outputs if route is not None else []
    report_path.write_text(
        "\n".join(
            [
                "# Harness Run Report",
                "",
                "## Summary",
                f"- Instruction: {instruction}",
                f"- Intent: {route_name}",
                f"- Route: {mode}",
                f"- Status: {status}",
                "",
                "## Work Performed",
                "- Read: see transcript and run events",
                "- Edited: see diff artifact when present",
                "- Tested: see test_result.json when present",
                "- Reviewed: see policy and manifest evidence",
                "",
                "## Policy And Approvals",
                f"- Hosted boundary: {run.approval_id or 'not approved'}",
                "- Isolation: required for edit routes",
                "- Path policy: see manifest",
                "- Secret scan: see artifact redaction state",
                "- Apply-back: separate explicit decision",
                "",
                "## Artifacts",
                "- Transcript: session transcript when run is session-linked",
                "- Patch: diff artifact when present",
                "- Test result: test_result.json when present",
                "- Manifest: manifest.json",
                f"- Expected outputs: {', '.join(expected_outputs) if expected_outputs else 'none'}",
                "",
                "## Next Actions",
                f"- apply: harness apply {run_id}",
                f"- reject: harness reject {run_id}",
                f"- retry: harness run \"{instruction}\"",
                f"- inspect: harness artifacts open {run_id}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return report_path


def _ensure_product_report(store: SQLiteStore, run_id: str) -> Path:
    run = store.get_run(run_id)
    report_path = store.runs_dir / run_id / "final_report.md"
    if report_path.exists() and report_path.read_text(encoding="utf-8").strip():
        return report_path
    return _write_product_report(
        store,
        run_id,
        instruction=run.goal or "",
        route=None,
        status=run.status,
    )


def _find_artifact_by_kind(store: SQLiteStore, run_id: str, kinds: set[str]):
    artifacts = store.verify_artifacts(run_id)
    normalized = {kind.lower() for kind in kinds}
    for artifact in artifacts:
        if artifact.kind.lower() in normalized or artifact.path.name.lower() in normalized:
            return artifact
    return None


def _record_apply_decision(project_root: Path, run_id: str, decision: str, *, ok: bool = True) -> dict:
    store = SQLiteStore(project_root)
    run = store.get_run(run_id)
    run_dir = project_root / HARNESS_DIR / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    decision_path = run_dir / "apply_back.json"
    payload = {
        "schema_version": "harness.apply_back_decision/v1",
        "ok": ok,
        "run_id": run_id,
        "decision": decision,
        "session_id": run.session_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    decision_path.write_text(json.dumps(payload, sort_keys=True, indent=2), encoding="utf-8")
    artifact = store.register_artifact(run_id, "apply_back", decision_path, producer="product_session")
    store.append_event(run_id, "info", "apply_back_decision", f"Apply-back decision: {decision}", payload)
    if run.session_id:
        append_session_event(
            project_root,
            session_id=run.session_id,
            run_id=run_id,
            event_type=SessionEventKind.APPLY_DECIDED,
            message=f"Apply-back decision: {decision}",
            payload={"artifact_id": artifact.id, "decision": decision},
        )
    payload["artifact"] = artifact.model_dump(mode="json")
    return payload


def _emit_session_error(message: str, output: OutputFormat) -> None:
    payload = {"schema_version": "harness.session_error/v1", "ok": False, "error": message}
    if output == OutputFormat.JSON:
        _emit_json(payload)
    else:
        typer.echo(message)


def _emit_session_mutation_unsupported(
    action: str,
    session_id: str,
    output: OutputFormat,
    *,
    project: Path,
    message_id: str | None = None,
    artifact_id: str | None = None,
    hunk_id: str | None = None,
) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    store = SQLiteStore(project_root)
    try:
        store.get_session(session_id)
    except KeyError as exc:
        _emit_session_error(str(exc).strip("'"), output)
        raise typer.Exit(code=1) from exc
    payload = {
        "schema_version": "harness.session_mutation_action/v1",
        "ok": False,
        "session_id": session_id,
        "action": action,
        "message_id": message_id,
        "artifact_id": artifact_id,
        "hunk_id": hunk_id,
        "error": (
            f"Session {action} is not implemented yet; refusing to mutate active workspace files or git state."
        ),
        "mutation_started": False,
        "git_mutation_started": False,
        "filesystem_modified": False,
        "revert_supported": False,
        "unrevert_supported": False,
        "selected_hunk_apply_supported": False,
        "permission_granting": False,
    }
    if output == OutputFormat.JSON:
        _emit_json(payload)
        raise typer.Exit(code=1)
    typer.echo(payload["error"])
    raise typer.Exit(code=1)


def _emit_mcp_unsupported(
    action: str,
    output: OutputFormat,
    *,
    server_name: str | None = None,
    project: Path | None = None,
) -> None:
    if project is not None:
        project_root = resolve_project_root(project)
        _require_initialized(project_root)
    payload = {
        "schema_version": "harness.mcp_action/v1",
        "ok": False,
        "action": action,
        "server": server_name,
        "error": (
            f"MCP {action} is not implemented yet; refusing to start processes, call network, "
            "write credentials, or register tools implicitly."
        ),
        "policy_boundary": {
            "kind": "mcp_action",
            "process_launch_allowed": False,
            "network_connection_allowed": False,
            "oauth_allowed": False,
            "credentials_storage_allowed": False,
            "tool_registration_allowed": False,
            "tool_execution_allowed": False,
            "requires_explicit_mcp_policy": True,
        },
        "blocked_reasons": ["mcp_action_disabled", "mcp_process_launch_disabled", "mcp_network_connection_disabled"],
        "process_started": False,
        "network_called": False,
        "tool_registration_enabled": False,
        "tool_execution_started": False,
        "filesystem_modified": False,
        "permission_granting": False,
    }
    if output == OutputFormat.JSON:
        _emit_json(payload)
        raise typer.Exit(code=1)
    typer.echo(payload["error"])
    raise typer.Exit(code=1)


def _emit_plugin_unsupported(
    action: str,
    output: OutputFormat,
    *,
    plugin_name: str | None = None,
    project: Path | None = None,
) -> None:
    if project is not None:
        project_root = resolve_project_root(project)
        _require_initialized(project_root)
    payload = {
        "schema_version": "harness.plugin_action/v1",
        "ok": False,
        "action": action,
        "plugin": plugin_name,
        "error": (
            f"Plugin {action} is not implemented yet; refusing to fetch, modify plugin files, "
            "load plugin code, or register tools implicitly."
        ),
        "policy_boundary": {
            "kind": "plugin_action",
            "runtime_load_allowed": False,
            "tool_registration_allowed": False,
            "tool_execution_allowed": False,
            "filesystem_mutation_allowed": False,
            "network_fetch_allowed": False,
            "origin_review_required": True,
        },
        "blocked_reasons": ["plugin_action_disabled", "plugin_origin_review_required", "plugin_runtime_load_disabled"],
        "filesystem_modified": False,
        "network_called": False,
        "runtime_loaded": False,
        "tools_registered": False,
        "tool_execution_started": False,
        "install_supported": False,
        "update_supported": False,
        "remove_supported": False,
        "permission_granting": False,
    }
    if output == OutputFormat.JSON:
        _emit_json(payload)
        raise typer.Exit(code=1)
    typer.echo(payload["error"])
    raise typer.Exit(code=1)


def _emit_skill_unsupported(
    action: str,
    output: OutputFormat,
    *,
    skill_name: str | None = None,
    project: Path | None = None,
) -> None:
    if project is not None:
        project_root = resolve_project_root(project)
        _require_initialized(project_root)
    payload = {
        "schema_version": "harness.skill_action/v1",
        "ok": False,
        "action": action,
        "skill": skill_name,
        "error": (
            f"Skill {action} is not implemented yet; refusing to load skill bodies or register skill tools implicitly."
        ),
        "skill_body_loaded": False,
        "runtime_loaded": False,
        "tool_registered": False,
        "permission_granting": False,
    }
    if output == OutputFormat.JSON:
        _emit_json(payload)
        raise typer.Exit(code=1)
    typer.echo(payload["error"])
    raise typer.Exit(code=1)


def _emit_web_unsupported(
    tool_id: str,
    action: str,
    output: OutputFormat,
    *,
    target: str,
    project: Path,
) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    cfg = load_config(project_root)
    policy = _web_tool_policy_projection(cfg)
    tool_policy = next((tool for tool in policy["tools"] if tool["id"] == tool_id), None)
    payload = {
        "schema_version": "harness.web_tool_action/v1",
        "ok": False,
        "action": action,
        "tool": tool_id,
        "target": target,
        "decision": (tool_policy or {}).get("decision", "denied"),
        "approval_required": bool((tool_policy or {}).get("approval_required", False)),
        "allowed_domains": policy.get("allowed_domains", []),
        "error": (
            f"Web {action} execution is not implemented yet; refusing to call external network implicitly."
        ),
        "network_called": False,
        "execution_started": False,
        "permission_granting": False,
    }
    if output == OutputFormat.JSON:
        _emit_json(payload)
        raise typer.Exit(code=1)
    typer.echo(payload["error"])
    typer.echo(f"Decision: {payload['decision']}")
    typer.echo("Network called: false")
    raise typer.Exit(code=1)


def _emit_worktree_unsupported(
    action: str,
    output: OutputFormat,
    *,
    target: str,
    project: Path,
    requested: dict[str, object] | None = None,
) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    payload = _worktree_action_unsupported(action, requested or {"path": target}, project_root)
    if output == OutputFormat.JSON:
        _emit_json(payload)
        raise typer.Exit(code=1)
    plan = payload.get("plan") or {}
    if plan:
        _print_tsv(["field", "value"])
        _print_tsv_row(["action", action])
        _print_tsv_row(["target", plan.get("target") or ""])
        _print_tsv_row(["managed_path", plan.get("managed_path") or ""])
        _print_tsv_row(["branch", plan.get("branch") or ""])
        _print_tsv_row(["policy_boundary", (plan.get("policy_boundary") or {}).get("kind") or ""])
        _print_tsv_row(["approval_required", plan.get("approval_required", True)])
        typer.echo("Planned steps:")
        for step in plan.get("steps") or []:
            typer.echo(f"- {step.get('name')}: {' '.join(step.get('command') or [])} (executed=false)")
        typer.echo(
            "Safety: process_started=false filesystem_modified=false git_mutation_started=false permission_granting=false"
        )
    typer.echo(payload["error"])
    raise typer.Exit(code=1)


def _emit_pty_unsupported(
    action: str,
    output: OutputFormat,
    *,
    project: Path,
    pty_id: str | None = None,
    requested: dict[str, object] | None = None,
) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    payload = _pty_action_unsupported(action, requested or {}, pty_id=pty_id)
    if output == OutputFormat.JSON:
        _emit_json(payload)
        raise typer.Exit(code=1)
    plan = payload.get("plan") or {}
    if plan:
        _print_tsv(["field", "value"])
        _print_tsv_row(["action", action])
        _print_tsv_row(["pty_id", plan.get("pty_id") or ""])
        if plan.get("shell"):
            _print_tsv_row(["shell", plan["shell"]])
        if plan.get("command"):
            _print_tsv_row(["command", plan["command"]])
        if plan.get("cols") is not None:
            _print_tsv_row(["cols", plan["cols"]])
        if plan.get("rows") is not None:
            _print_tsv_row(["rows", plan["rows"]])
        _print_tsv_row(["policy_boundary", (plan.get("policy_boundary") or {}).get("kind") or ""])
        _print_tsv_row(["approval_required", plan.get("approval_required", True)])
        _print_tsv_row(["blocked_reasons", ",".join(plan.get("blocked_reasons") or [])])
        typer.echo("Planned steps:")
        for step in plan.get("steps") or []:
            detail = step.get("name") or ""
            if step.get("command"):
                detail += f" command={step['command']}"
            if step.get("data_preview"):
                detail += f" data_preview={step['data_preview']}"
            typer.echo(f"- {detail} (executed=false)")
        typer.echo(
            "Safety: process_started=false input_written=false terminal_resized=false terminal_closed=false websocket_token_issued=false live_stream_read=false"
        )
    typer.echo(payload["error"])
    raise typer.Exit(code=1)


def _emit_pr_unsupported(
    action: str,
    output: OutputFormat,
    *,
    project: Path,
    requested: dict[str, object],
) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    payload = _pr_action_unsupported(action, requested)
    if output == OutputFormat.JSON:
        _emit_json(payload)
        raise typer.Exit(code=1)
    plan = payload.get("plan") or {}
    if plan:
        _print_tsv(["field", "value"])
        _print_tsv_row(["pr", payload.get("pr") or ""])
        _print_tsv_row(["valid_pr_ref", plan.get("valid_pr_ref")])
        _print_tsv_row(["owner", plan.get("owner") or ""])
        _print_tsv_row(["repo", plan.get("repo") or ""])
        _print_tsv_row(["number", plan.get("number") or ""])
        _print_tsv_row(["branch", plan.get("branch") or ""])
        _print_tsv_row(["worktree_path", plan.get("worktree_path") or ""])
        _print_tsv_row(["fetch_ref", plan.get("fetch_ref") or ""])
        _print_tsv_row(["policy_boundary", (plan.get("policy_boundary") or {}).get("kind") or ""])
        _print_tsv_row(["approval_required", plan.get("approval_required", True)])
        _print_tsv_row(["blocked_reasons", ",".join(plan.get("blocked_reasons") or [])])
        if plan.get("adapter"):
            _print_tsv_row(["adapter", plan["adapter"]])
        typer.echo("Planned steps:")
        for step in plan.get("steps") or []:
            command = " ".join(step.get("command") or []) if step.get("command") else step.get("adapter") or ""
            typer.echo(f"- {step.get('name')}: {command} (executed=false)")
        typer.echo(
            "Safety: network_called=false process_started=false filesystem_modified=false git_mutation_started=false adapter_started=false permission_granting=false"
        )
    typer.echo(payload["error"])
    raise typer.Exit(code=1)


def _emit_distribution_unsupported(
    action: str,
    output: OutputFormat,
    *,
    requested: dict[str, object],
) -> None:
    payload = _distribution_action_unsupported(action, requested)
    if output == OutputFormat.JSON:
        _emit_json(payload)
        raise typer.Exit(code=1)
    typer.echo(payload["error"])
    raise typer.Exit(code=1)


def _emit_managed_action_error(schema_version: str, message: str, output: OutputFormat) -> None:
    if output == OutputFormat.JSON:
        _emit_json({"schema_version": schema_version, "ok": False, "errors": [message]})
    else:
        typer.echo(message)


def _split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _parse_key_value_options(values: list[str]) -> dict[str, str]:
    parsed: dict[str, str] = {}
    allowed = {setting["key"] for setting in build_tui_settings_catalog()["settings"]}
    for value in values:
        if "=" not in value:
            raise ValueError(f"Expected key=value preference: {value}")
        key, raw = value.split("=", 1)
        key = key.strip()
        if key not in allowed:
            raise ValueError(f"Unsupported preference key: {key}")
        parsed[key] = raw.strip()
    return parsed


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


SUPPORTED_EXECUTION_TASK_METADATA: tuple[tuple[str, str], ...] = (
    ("dry_run", "phase_1a_test"),
    ("read_only_summary", "read_only_repo_summary"),
    ("codex_isolated_edit", "codex_code_edit"),
    ("repo_planning", "repo_planning"),
    ("session_read_tools", "session_plan"),
    ("session_read_tools", "session_read_only_research"),
    ("session_read_tools", "session_operator"),
)


def _supported_execution_task_metadata_message() -> str:
    pairs = [f"{execution_adapter}/{task_type}" for execution_adapter, task_type in SUPPORTED_EXECUTION_TASK_METADATA]
    if len(pairs) == 1:
        return pairs[0]
    return f"{', '.join(pairs[:-1])}, and {pairs[-1]}"


def _execution_task_metadata(execution_adapter: str | None, task_type: str | None) -> dict[str, str]:
    if execution_adapter is None and task_type is None:
        return {}
    if (execution_adapter, task_type) not in SUPPORTED_EXECUTION_TASK_METADATA:
        raise ValueError(
            "Unsupported execution metadata: supported pairs are "
            f"{_supported_execution_task_metadata_message()}"
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


def _context_cli_safety_payload(*, filesystem_modified: bool) -> dict:
    return {
        "permission_granting": False,
        "policy_authority": False,
        "approval_authority": False,
        "process_started": False,
        "filesystem_modified": filesystem_modified,
        "provider_call_allowed": False,
        "provider_preflight_started": False,
        "docker_allowed": False,
        "adapter_dispatch_allowed": False,
        "active_repo_mutation_allowed": False,
    }


def _context_chunk_payload(chunk) -> dict:
    return {
        "chunk_id": chunk.id,
        "source_kind": chunk.source_kind.value,
        "trust_level": chunk.trust_level.value,
        "path": chunk.path,
        "source_id": chunk.source_id,
        "artifact_id": chunk.artifact_id,
        "memory_id": chunk.memory_id,
        "start_line": chunk.start_line,
        "end_line": chunk.end_line,
        "sha256": chunk.sha256,
        "size_bytes": chunk.size_bytes,
        "token_count": chunk.token_count,
        "tokenizer": chunk.tokenizer,
        "chunk_scheme": chunk.chunk_scheme,
        "redaction_state": chunk.redaction_state,
        "warnings": list(chunk.warnings),
        "metadata": dict(chunk.metadata),
        "permission_granting": False,
        "policy_authority": False,
        "approval_authority": False,
    }


def _tail_run_events(store: SQLiteStore, run_id: str, *, jsonl: bool, follow: bool) -> None:
    try:
        store.get_run(run_id)
    except KeyError as exc:
        raise typer.BadParameter(str(exc)) from exc
    path = store.runs_dir / run_id / "events.jsonl"
    path.touch(exist_ok=True)
    offset = 0
    while True:
        lines = path.read_text(encoding="utf-8").splitlines()
        for line in lines[offset:]:
            if not line.strip():
                continue
            if jsonl:
                typer.echo(line)
            else:
                typer.echo(render_procedure_event(json.loads(line)))
        offset = len(lines)
        if not follow:
            return
        run = store.get_run(run_id)
        if run.status in {"completed", "completed_applied", "completed_denied", "failed", "cancelled", "canceled"}:
            return
        time.sleep(0.25)


def _tail_session_events(store: SQLiteStore, session_id: str, *, jsonl: bool, follow: bool, limit: int | None) -> None:
    try:
        store.get_session(session_id)
    except KeyError as exc:
        raise typer.BadParameter(str(exc)) from exc
    seen_ids: set[str] = set()
    while True:
        events = list_session_timeline(store, session_id, limit=limit if not seen_ids else None)
        for event in events:
            if event.id in seen_ids:
                continue
            typer.echo(timeline_event_jsonl(event) if jsonl else render_timeline_event(event))
            seen_ids.add(event.id)
        if not follow:
            return
        session = store.get_session(session_id)
        if session.status in {
            SessionStatus.COMPLETED,
            SessionStatus.FAILED,
            SessionStatus.CANCELLED,
            SessionStatus.ARCHIVED,
        }:
            return
        time.sleep(0.25)


def _create_policy_first_live_run(
    *,
    project_root: Path,
    goal: str,
    task_type: str,
    agent: str,
    task_id: str | None,
    task_file: Path | None,
) -> dict:
    store = SQLiteStore(project_root)
    cfg = load_config(project_root)
    approval = ApprovalStore(project_root).find_valid("codex_cli", "hosted_provider", task_type)
    run = store.create_run(
        goal=goal,
        task_type=task_type,
        status="running",
        backend=cfg.backends.get("codex_cli"),
        approval_id=approval.id if approval else None,
        task_id=task_id,
    )
    paths = store.initialize_run_artifacts(run.id)
    for kind, path in paths.items():
        if kind not in {artifact.kind for artifact in store.list_artifacts(run.id)}:
            store.register_artifact(run.id, kind=kind, path=path, producer="harness.live_run")
    store.append_run_event(
        run.id,
        RunEventType.RUN_STARTED,
        {
            "agent": agent,
            "backend": "codex_cli",
            "mode": "edit-isolated",
            "task_file": str(task_file) if task_file else None,
        },
        message="Live run started.",
        redaction_state=RedactionState.REDACTED,
        task_id=task_id,
    )
    store.append_run_event(
        run.id,
        RunEventType.POLICY_RESOLVED,
        {
            "hosted_provider": "approved" if approval else "approval_required",
            "active_repo_write": "forbidden_until_apply_back_approval",
            "isolated_workspace": "required",
            "approval_id": approval.id if approval else None,
        },
        message="Resolved live run policy.",
        redaction_state=RedactionState.NOT_REQUIRED,
        task_id=task_id,
    )
    if approval is None:
        store.append_run_event(
            run.id,
            RunEventType.APPROVAL_REQUIRED,
            {
                "approval_kind": "hosted_provider_codex",
                "reason": "Codex hosted-boundary approval is required before backend execution.",
                "backend": "codex_cli",
                "task_type": task_type,
            },
            message="Hosted-boundary approval required.",
            redaction_state=RedactionState.NOT_REQUIRED,
            task_id=task_id,
        )
        store.update_run_status(run.id, "waiting_approval")
    else:
        store.append_run_event(
            run.id,
            RunEventType.BACKEND_STARTED,
            {"backend": "codex_cli", "streaming": False, "status": "not_dispatched_by_run_live"},
            message="Hosted approval is present; dispatch through the registered Codex runner.",
            redaction_state=RedactionState.NOT_REQUIRED,
            task_id=task_id,
        )
        store.update_run_status(run.id, "completed")
        store.append_run_event(
            run.id,
            RunEventType.RUN_FINISHED,
            {"status": "completed", "dispatch": "not_dispatched_by_run_live"},
            message="Live run setup completed.",
            redaction_state=RedactionState.NOT_REQUIRED,
            task_id=task_id,
        )
    artifact_paths = write_live_run_artifacts(store, run.id)
    return {
        "run_id": run.id,
        "status": store.get_run(run.id).status,
        "run_dir": str(store.runs_dir / run.id),
        "artifacts": {key: str(path) for key, path in artifact_paths.items()},
    }


def _emit_live_stream(project_root: Path, run_id: str, stream: StreamFormat) -> None:
    if stream == StreamFormat.NONE:
        return
    store = SQLiteStore(project_root)
    _tail_run_events(store, run_id, jsonl=stream == StreamFormat.JSONL, follow=False)


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


def _doctor_result(project_root: Path, *, release: bool = False, repair: bool = False) -> dict:
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
    _doctor_session_schema(checks, project_root, repair=repair)
    _doctor_schema_current(checks, project_root)
    _doctor_required_table_groups(checks, project_root)
    _doctor_artifact_directory(checks, project_root, repair=repair)
    _doctor_tool_registry(checks)
    _doctor_shell_config(checks)
    _doctor_session_permission_tables(checks, project_root)
    _doctor_session_status_projection(checks, project_root, repair=repair)

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

    if config is not None:
        _doctor_session_cwds(checks, project_root, config, repair=repair)

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

    if not release and not repair:
        checks = _filter_standard_doctor_pass_checks(checks)

    return {
        "schema_version": "harness.doctor/v1",
        "project_root": str(project_root),
        "mode": "release" if release else "standard",
        "repair": repair,
        "version": __version__,
        "ok": all(check["status"] != "fail" for check in checks),
        "checks": checks,
    }


def _add_check(checks: list[dict], check_id: str, status: str, message: str, details: dict | None = None) -> None:
    checks.append({"id": check_id, "status": status, "message": message, "details": details or {}})


def _filter_standard_doctor_pass_checks(checks: list[dict]) -> list[dict]:
    extended_ids = {
        "schema_current",
        "required_session_tables",
        "required_event_tables",
        "artifact_directory",
        "tool_registry",
        "shell_config",
        "session_permission_tables",
        "session_status_projection",
        "session_cwd",
    }
    return [check for check in checks if check["id"] not in extended_ids or check["status"] != "pass"]


def _doctor_session_schema(checks: list[dict], project_root: Path, *, repair: bool) -> None:
    store = SQLiteStore(project_root)
    if not store.db_path.exists():
        _add_check(
            checks,
            "session_schema",
            "warn",
            "Harness session database does not exist yet.",
            {"path": str(store.db_path), "repairable": False, "repair_attempted": False},
        )
        return

    try:
        before = store.inspect_required_session_schema()
    except sqlite3.Error as exc:
        message = (
            SESSION_SCHEMA_REPAIR_MESSAGE
            if is_missing_session_schema_error(exc)
            else "Harness session schema could not be inspected."
        )
        _add_check(
            checks,
            "session_schema",
            "fail",
            message,
            {"path": str(store.db_path), "repairable": True, "repair_attempted": False},
        )
        return

    repair_attempted = False
    repair_error: str | None = None
    if repair:
        repair_attempted = True
        try:
            store.initialize()
        except Exception as exc:
            repair_error = (
                SESSION_SCHEMA_REPAIR_MESSAGE
                if is_missing_session_schema_error(exc)
                else f"{type(exc).__name__}: {exc}"
            )

    try:
        after = store.inspect_required_session_schema()
    except sqlite3.Error as exc:
        message = (
            SESSION_SCHEMA_REPAIR_MESSAGE
            if is_missing_session_schema_error(exc)
            else "Harness session schema could not be inspected."
        )
        _add_check(
            checks,
            "session_schema",
            "fail",
            message,
            {
                "path": str(store.db_path),
                "repairable": True,
                "repair_attempted": repair_attempted,
                "missing_tables_before": before["missing_tables"],
            },
        )
        return

    details = {
        "path": str(store.db_path),
        "required_tables": after["required_tables"],
        "present_tables": after["present_tables"],
        "missing_tables": after["missing_tables"],
        "missing_tables_before": before["missing_tables"],
        "repairable": bool(after["missing_tables"]),
        "repair_attempted": repair_attempted,
    }
    if repair_error is not None:
        details["repair_error"] = repair_error
        _add_check(checks, "session_schema", "fail", "Harness session schema repair failed.", details)
        return
    if after["missing_tables"]:
        _add_check(checks, "session_schema", "fail", SESSION_SCHEMA_REPAIR_MESSAGE, details)
        return
    if repair_attempted and before["missing_tables"]:
        _add_check(checks, "session_schema", "pass", "Harness session schema repaired.", details)
        return
    _add_check(checks, "session_schema", "pass", "Harness session/event tables are present.", details)


def _doctor_schema_current(checks: list[dict], project_root: Path) -> None:
    store = SQLiteStore(project_root)
    state = _inspect_schema_migration_state(store.db_path)
    if not state["db_exists"]:
        _add_check(
            checks,
            "schema_current",
            "warn",
            "Harness database does not exist yet; schema migration check skipped.",
            state,
        )
        return
    if state["current"]:
        _add_check(checks, "schema_current", "pass", "Harness schema migrations are current.", state)
        return
    _add_check(
        checks,
        "schema_current",
        "fail",
        "Harness schema migrations are not current. Run: harness doctor --repair",
        state,
    )


def _inspect_schema_migration_state(db_path: Path) -> dict:
    expected = _expected_schema_migration_checksums()
    if not db_path.exists():
        return {
            "db_exists": False,
            "schema_migrations_table": False,
            "expected_migrations": list(expected),
            "applied_migrations": [],
            "missing_migrations": list(expected),
            "unknown_migrations": [],
            "checksum_mismatches": [],
            "current": False,
            "repairable": False,
        }
    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            has_table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
            ).fetchone() is not None
            if not has_table:
                return {
                    "db_exists": True,
                    "schema_migrations_table": False,
                    "expected_migrations": list(expected),
                    "applied_migrations": [],
                    "missing_migrations": list(expected),
                    "unknown_migrations": [],
                    "checksum_mismatches": [],
                    "current": False,
                    "repairable": True,
                }
            rows = conn.execute("SELECT id, checksum FROM schema_migrations ORDER BY id ASC").fetchall()
    except sqlite3.Error as exc:
        return {
            "db_exists": True,
            "schema_migrations_table": None,
            "expected_migrations": list(expected),
            "applied_migrations": [],
            "missing_migrations": list(expected),
            "unknown_migrations": [],
            "checksum_mismatches": [],
            "current": False,
            "repairable": True,
            "error": SESSION_SCHEMA_REPAIR_MESSAGE if is_missing_session_schema_error(exc) else str(exc),
        }
    applied = {str(row["id"]): str(row["checksum"]) for row in rows}
    missing = [migration_id for migration_id in expected if migration_id not in applied]
    unknown = sorted(migration_id for migration_id in applied if migration_id not in expected)
    mismatches = [
        {"id": migration_id, "expected": checksum, "actual": applied[migration_id]}
        for migration_id, checksum in expected.items()
        if migration_id in applied and applied[migration_id] != checksum
    ]
    return {
        "db_exists": True,
        "schema_migrations_table": True,
        "expected_migrations": list(expected),
        "applied_migrations": list(applied),
        "missing_migrations": missing,
        "unknown_migrations": unknown,
        "checksum_mismatches": mismatches,
        "current": not missing and not unknown and not mismatches,
        "repairable": bool(missing) and not unknown and not mismatches,
    }


def _expected_schema_migration_checksums() -> dict[str, str]:
    migrations_dir = Path(__file__).resolve().parents[1] / "memory" / "migrations"
    expected: dict[str, str] = {}
    for migration_id, filename, _description in SCHEMA_MIGRATIONS:
        text = (migrations_dir / filename).read_text(encoding="utf-8")
        expected[migration_id] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return expected


def _doctor_required_table_groups(checks: list[dict], project_root: Path) -> None:
    store = SQLiteStore(project_root)
    schema = store.inspect_required_session_schema()
    session_tables = ["sessions", "session_messages", "session_parts"]
    event_tables = ["events", "event_store", "artifacts"]
    _add_required_table_check(checks, "required_session_tables", "Required session tables are present.", schema, session_tables)
    _add_required_table_check(checks, "required_event_tables", "Required event/evidence tables are present.", schema, event_tables)


def _add_required_table_check(
    checks: list[dict],
    check_id: str,
    pass_message: str,
    schema: dict,
    required: list[str],
) -> None:
    missing = [table for table in required if table in schema.get("missing_tables", [])]
    status = "pass" if not missing and schema.get("db_exists") else "warn" if not schema.get("db_exists") else "fail"
    if status == "pass":
        message = pass_message
    elif status == "warn":
        message = "Harness database does not exist yet; table check skipped."
    else:
        message = f"Missing required table(s): {', '.join(missing)}. Run: harness doctor --repair"
    _add_check(
        checks,
        check_id,
        status,
        message,
        {
            "required_tables": required,
            "missing_tables": missing,
            "repairable": bool(missing),
        },
    )


def _doctor_session_cwds(checks: list[dict], project_root: Path, config, *, repair: bool) -> None:
    store = SQLiteStore(project_root)
    if not store.db_path.exists():
        _add_check(
            checks,
            "session_cwd",
            "warn",
            "Harness database does not exist yet; session cwd check skipped.",
            {"repair_attempted": False},
        )
        return
    schema = store.inspect_required_session_schema()
    if "sessions" in schema.get("missing_tables", []) or "event_store" in schema.get("missing_tables", []):
        _add_check(
            checks,
            "session_cwd",
            "warn",
            "Session cwd check skipped until session schema is repaired.",
            {"missing_tables": schema.get("missing_tables", []), "repair_attempted": False},
        )
        return
    try:
        sessions = store.list_sessions()
    except sqlite3.Error as exc:
        _add_check(
            checks,
            "session_cwd",
            "fail",
            "Session cwd state could not be inspected. Run: harness doctor --repair",
            {"error": SESSION_SCHEMA_REPAIR_MESSAGE if is_missing_session_schema_error(exc) else str(exc)},
        )
        return
    resolver = CwdResolver(project_root=project_root, context_excludes=config.context_excludes)
    invalid = []
    for session in sessions:
        cwd = session_cwd_from_metadata(session.metadata)
        try:
            resolver.current(cwd, allow_excluded=True)
        except CwdResolutionError as exc:
            invalid.append(
                {
                    "session_id": session.id,
                    "cwd": cwd,
                    "error_type": exc.error_type,
                    "message": cwd_recovery_message(exc),
                }
            )
    repaired: list[str] = []
    repair_errors: list[dict] = []
    if repair and invalid:
        for item in invalid:
            try:
                session = store.get_session(item["session_id"])
                store.update_session_cwd(
                    session.id,
                    project_root=str(project_root),
                    old_cwd=str(item["cwd"]),
                    new_cwd=".",
                    requested_path=".",
                    resolved_abs_path=str(project_root),
                    actor="doctor",
                    tool_call_id="doctor_repair",
                )
                repaired.append(session.id)
            except Exception as exc:
                repair_errors.append({"session_id": item["session_id"], "error": str(exc)})
    after_invalid = []
    if repair and invalid and not repair_errors:
        for session in store.list_sessions():
            cwd = session_cwd_from_metadata(session.metadata)
            try:
                resolver.current(cwd, allow_excluded=True)
            except CwdResolutionError as exc:
                after_invalid.append(
                    {
                        "session_id": session.id,
                        "cwd": cwd,
                        "error_type": exc.error_type,
                        "message": cwd_recovery_message(exc),
                    }
                )
    else:
        after_invalid = invalid
    details = {
        "session_count": len(sessions),
        "invalid": after_invalid,
        "invalid_before": invalid,
        "repaired_session_ids": repaired,
        "repair_attempted": bool(repair and invalid),
        "repair_errors": repair_errors,
        "repair_action": 'reset cwd to "."',
    }
    if repair_errors:
        _add_check(checks, "session_cwd", "fail", "Invalid session cwd repair failed.", details)
    elif after_invalid:
        _add_check(
            checks,
            "session_cwd",
            "fail",
            'One or more sessions have invalid cwd. Run: harness doctor --repair to reset them to "."',
            details,
        )
    elif repaired:
        _add_check(checks, "session_cwd", "pass", 'Invalid session cwd repaired to ".".', details)
    else:
        _add_check(checks, "session_cwd", "pass", "Persisted session cwd values are valid.", details)


def _doctor_tool_registry(checks: list[dict]) -> None:
    issues: list[str] = []
    try:
        descriptors = default_session_tool_descriptors()
    except Exception as exc:
        _add_check(checks, "tool_registry", "fail", "Session tool registry could not be loaded.", {"error": str(exc)})
        return
    ids = [descriptor.id for descriptor in descriptors]
    duplicates = sorted({tool_id for tool_id in ids if ids.count(tool_id) > 1})
    if duplicates:
        issues.append("duplicate tool ids: " + ", ".join(duplicates))
    for descriptor in descriptors:
        try:
            loaded = get_session_tool_descriptor(descriptor.id)
        except KeyError:
            issues.append(f"descriptor lookup failed: {descriptor.id}")
            continue
        if loaded.id != descriptor.id:
            issues.append(f"descriptor lookup mismatch: {descriptor.id}")
        if not isinstance(descriptor.input_schema, dict) or not isinstance(descriptor.output_schema, dict):
            issues.append(f"descriptor schema missing: {descriptor.id}")
    _add_check(
        checks,
        "tool_registry",
        "pass" if not issues else "fail",
        "Session tool registry is valid." if not issues else "Session tool registry has invalid descriptors.",
        {"tool_count": len(descriptors), "issues": issues},
    )


def _doctor_shell_config(checks: list[dict]) -> None:
    shell = Path("/bin/sh")
    ok = shell.exists() and os.access(shell, os.X_OK)
    _add_check(
        checks,
        "shell_config",
        "pass" if ok else "fail",
        "Default session shell is executable." if ok else "Default session shell is unavailable; configure a valid absolute shell path.",
        {"default_shell": str(shell), "exists": shell.exists(), "executable": os.access(shell, os.X_OK)},
    )


def _doctor_artifact_directory(checks: list[dict], project_root: Path, *, repair: bool) -> None:
    harness_dir = project_root / HARNESS_DIR
    db_path = harness_dir / "harness.sqlite"
    runs_dir = harness_dir / "runs"
    tmp_dir = harness_dir / "tmp"
    if not db_path.exists() and not harness_dir.exists():
        _add_check(
            checks,
            "artifact_directory",
            "warn",
            "Project is not initialized; artifact directory check skipped.",
            {"paths": [str(runs_dir), str(tmp_dir)], "repair_attempted": False},
        )
        return
    repaired: list[str] = []
    if repair and db_path.exists():
        for path in (runs_dir, tmp_dir):
            if not path.exists():
                path.mkdir(parents=True, exist_ok=True)
                repaired.append(str(path))
    issues = [_artifact_directory_issue(path) for path in (runs_dir, tmp_dir)]
    issues = [issue for issue in issues if issue is not None]
    details = {"paths": [str(runs_dir), str(tmp_dir)], "issues": issues, "repaired_paths": repaired, "repair_attempted": repair}
    if issues:
        _add_check(
            checks,
            "artifact_directory",
            "fail",
            "Harness artifact directory is not writable.",
            details,
        )
    elif repaired:
        _add_check(checks, "artifact_directory", "pass", "Harness artifact directories were recreated.", details)
    else:
        _add_check(checks, "artifact_directory", "pass", "Harness artifact directories are writable.", details)


def _artifact_directory_issue(path: Path) -> dict | None:
    if not path.exists():
        return {"path": str(path), "error": "missing", "repairable": True}
    if not path.is_dir():
        return {"path": str(path), "error": "not_directory", "repairable": False}
    mode = path.stat().st_mode
    has_write_bit = bool(mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
    if not has_write_bit or not os.access(path, os.W_OK):
        return {"path": str(path), "error": "not_writable", "repairable": False}
    return None


def _doctor_session_permission_tables(checks: list[dict], project_root: Path) -> None:
    store = SQLiteStore(project_root)
    schema = store.inspect_required_session_schema()
    if not schema.get("db_exists"):
        _add_check(
            checks,
            "session_permission_tables",
            "warn",
            "Harness database does not exist yet; permission table check skipped.",
            {"required_table": "session_permissions"},
        )
        return
    if "session_permissions" in schema.get("missing_tables", []):
        _add_check(
            checks,
            "session_permission_tables",
            "fail",
            "Session permission table is missing. Run: harness doctor --repair",
            {"required_table": "session_permissions", "missing_tables": ["session_permissions"], "repairable": True},
        )
        return
    required_columns = {
        "id",
        "session_id",
        "run_id",
        "tool_id",
        "normalized_action",
        "normalized_target_pattern",
        "boundary_kind",
        "risk",
        "status",
        "scope",
        "source",
        "policy_reasons_json",
        "requested_at",
        "resolved_at",
        "expires_at",
    }
    with sqlite3.connect(store.db_path) as conn:
        rows = conn.execute("PRAGMA table_info(session_permissions)").fetchall()
    columns = {str(row[1]) for row in rows}
    missing_columns = sorted(required_columns - columns)
    _add_check(
        checks,
        "session_permission_tables",
        "pass" if not missing_columns else "fail",
        "Session permission table is valid." if not missing_columns else "Session permission table is missing required columns. Run: harness doctor --repair",
        {
            "required_table": "session_permissions",
            "required_columns": sorted(required_columns),
            "missing_columns": missing_columns,
            "repairable": bool(missing_columns),
        },
    )


def _doctor_session_status_projection(checks: list[dict], project_root: Path, *, repair: bool) -> None:
    store = SQLiteStore(project_root)
    schema = store.inspect_required_session_schema()
    if not schema.get("db_exists") or "sessions" in schema.get("missing_tables", []) or "runs" in schema.get("missing_tables", []):
        _add_check(
            checks,
            "session_status_projection",
            "warn",
            "Session status projection check skipped until runtime tables exist.",
            {"materialized": False, "repair_attempted": False},
        )
        return
    try:
        sessions = store.list_sessions()
        runs = {run.id for run in store.list_runs()}
    except sqlite3.Error as exc:
        _add_check(
            checks,
            "session_status_projection",
            "fail",
            "Session status projection could not be inspected. Run: harness doctor --repair",
            {"error": SESSION_SCHEMA_REPAIR_MESSAGE if is_missing_session_schema_error(exc) else str(exc)},
        )
        return
    stale = [session.id for session in sessions if session.active_run_id and session.active_run_id not in runs]
    _add_check(
        checks,
        "session_status_projection",
        "pass" if not stale else "warn",
        "Session status projection is derived on read; no rebuild required." if not stale else "Some sessions reference missing active runs; derived status remains available.",
        {"materialized": False, "stale_active_run_session_ids": stale, "repair_attempted": repair, "manual_action_required": bool(stale)},
    )


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
    session_id: str | None = None,
    continue_session: bool = False,
    fork_session: bool = False,
    title: str | None = None,
    agent_id: str | None = None,
    file_refs: list[Path] | None = None,
    no_session: bool = False,
    cfg=None,
    store: SQLiteStore | None = None,
) -> dict:
    _require_initialized(project_root)
    cfg = cfg or load_config(project_root)
    store = store or SQLiteStore(project_root)
    session = None
    user_message = None
    if no_session and (session_id or continue_session or fork_session or title or agent_id or file_refs):
        raise typer.BadParameter("--no-session cannot be combined with session selection, fork, title, agent, or file options.")
    if not no_session:
        session = _resolve_prompt_session(
            store,
            session_id=session_id,
            continue_session=continue_session,
            fork_session=fork_session,
            title=title,
            agent_id=agent_id,
            raw_model_ref=model,
            goal=goal,
        )
        user_message = store.append_session_message(session.id, SessionMessageRole.USER, goal, agent_id=agent_id)
        store.append_session_part(session.id, user_message.id, SessionPartKind.TEXT, text=goal, redaction_state=RedactionState.REDACTED)
        for file_ref in file_refs or []:
            store.append_session_part(
                session.id,
                user_message.id,
                SessionPartKind.ARTIFACT_REF,
                metadata={
                    "attachment_kind": "file_ref",
                    "path": str(file_ref),
                    "resolved_path": str((project_root / file_ref).resolve() if not file_ref.is_absolute() else file_ref.resolve()),
                },
                redaction_state=RedactionState.NOT_REQUIRED,
            )
        if model is not None:
            validation = validate_model_selection(cfg, model)
            validation_payload = validation.model_dump(mode="json")
            store.append_store_event(
                EventStreamType.SESSION,
                session.id,
                "session.model_validation",
                {
                    **validation_payload,
                    "summary": "Model selection validated." if validation.executable else "Model selection blocked before provider execution.",
                },
                session_id=session.id,
                message_id=user_message.id,
                redaction_state=RedactionState.NOT_REQUIRED,
            )
            if not validation.executable:
                store.append_session_part(
                    session.id,
                    user_message.id,
                    SessionPartKind.SUMMARY,
                    text="Model selection blocked before provider execution.",
                    metadata={"status": "model_validation_failed", "validation": validation_payload},
                    redaction_state=RedactionState.NOT_REQUIRED,
                )
                result = {
                    "schema_version": "harness.codex_direct_agent/v1",
                    "ok": False,
                    "status": "model_validation_failed",
                    "session_id": session.id,
                    "run_id": None,
                    "model_validation": validation_payload,
                    "no_hidden_fallback": True,
                    "provider_execution_started": False,
                    "model_execution_started": False,
                    "hidden_provider_fallback": False,
                    "hidden_model_fallback": False,
                    "permission_granting": False,
                    "authority_granting": False,
                }
                if output == OutputFormat.JSON:
                    _emit_json(result)
                    raise typer.Exit(code=1)
                typer.echo("Model selection blocked before provider execution.")
                for reason in validation.blocked_reasons:
                    typer.echo(f"  - {reason}")
                raise typer.Exit(code=1)
    backend = CodexCliBackend(cfg.backends["codex_cli"])
    runner = CodexDirectAgentRunner(project_root, store, backend)
    progress_callback = None
    if output != OutputFormat.JSON:
        progress_callback = _codex_direct_session_progress_callback(store, session.id if session is not None else None)
    if output != OutputFormat.JSON:
        typer.echo("Codex foreground agent")
        if session is not None:
            _print_kv("Session", session.id)
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
            session_id=session.id if session is not None else None,
            progress_callback=progress_callback,
        )
    except (
        CodexUnavailable,
        CodexSandboxUnavailable,
        CodexEditCommandUnavailable,
        CodexDangerousFlagError,
        DirtyWorkspaceError,
    ) as exc:
        if session is not None and user_message is not None:
            store.append_session_part(
                session.id,
                user_message.id,
                SessionPartKind.SUMMARY,
                text=str(exc),
                metadata={"status": "failed_before_run"},
                redaction_state=RedactionState.REDACTED,
            )
        raise typer.BadParameter(str(exc)) from exc
    if session is not None:
        run_id = result.get("run_id")
        if run_id:
            store.attach_session_to_run(session.id, str(run_id))
        assistant_message = store.append_session_message(
            session.id,
            SessionMessageRole.ASSISTANT,
            result.get("final_summary", ""),
            run_id=str(run_id) if run_id else None,
            agent_id=agent_id,
            mutation_reversibility=SessionMutationReversibility.NOT_REVERSIBLE_ACTIVE_WORKSPACE,
        )
        store.append_session_part(
            session.id,
            assistant_message.id,
            SessionPartKind.SUMMARY,
            text=result.get("final_summary", ""),
            run_id=str(run_id) if run_id else None,
            redaction_state=RedactionState.REDACTED,
        )
        if run_id:
            for artifact in store.list_artifacts(str(run_id)):
                store.append_session_part(
                    session.id,
                    assistant_message.id,
                    SessionPartKind.ARTIFACT_REF,
                    artifact_id=artifact.id,
                    run_id=str(run_id),
                    metadata={
                        "kind": artifact.kind,
                        "path": str(artifact.path),
                        "sha256": artifact.sha256,
                        "size_bytes": artifact.size_bytes,
                        "redaction_state": artifact.redaction_state,
                    },
                    redaction_state=RedactionState.NOT_REQUIRED
                    if artifact.redaction_state == "not_required"
                    else RedactionState.REDACTED,
                )
        result["session_id"] = session.id
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


def _codex_direct_session_progress_callback(store: SQLiteStore, session_id: str | None):
    def callback(event: dict) -> None:
        if session_id is not None:
            summary = _codex_event_summary(event.get("event") or {}) if event.get("type") == "event" else ""
            if not summary and event.get("type") == "stdout":
                summary = str(event.get("line") or "").strip()[:240]
            kind = "model.message_delta" if event.get("type") in {"event", "stdout"} else "run.progress"
            store.append_store_event(
                EventStreamType.SESSION,
                session_id,
                kind,
                {
                    "summary": summary,
                    "stream_type": event.get("type"),
                    "event": event.get("event"),
                    "line": event.get("line"),
                },
                session_id=session_id,
                run_id=event.get("run_id"),
                redaction_state=RedactionState.REDACTED,
            )
        _print_codex_direct_progress(event)

    return callback


def _resolve_prompt_session(
    store: SQLiteStore,
    *,
    session_id: str | None,
    continue_session: bool,
    fork_session: bool,
    title: str | None,
    agent_id: str | None,
    raw_model_ref: str | None,
    goal: str,
):
    if session_id and continue_session:
        raise typer.BadParameter("--session and --continue are mutually exclusive.")
    if fork_session and not (session_id or continue_session):
        raise typer.BadParameter("--fork requires --session or --continue.")
    parsed_model = _parse_model_ref(raw_model_ref)
    if session_id is not None:
        session = store.get_session(session_id)
    elif continue_session:
        session = store.latest_session()
        if session is None:
            raise typer.BadParameter("No non-archived session exists to continue.")
    else:
        session = store.create_session(
            title=title,
            agent_id=agent_id,
            raw_model_ref=raw_model_ref,
            provider_id=parsed_model["provider_id"],
            model_id=parsed_model["model_id"],
            model_variant=parsed_model["model_variant"],
            intent="foreground_prompt",
            metadata={"created_by": "foreground_prompt", "initial_goal_preview": goal[:240]},
        )
    if fork_session:
        session = store.fork_session(session.id, title=title)
    updates = {}
    if agent_id is not None:
        updates["agent_id"] = agent_id
    if raw_model_ref is not None:
        updates.update(parsed_model)
        updates["raw_model_ref"] = raw_model_ref
    if title is not None and session.title != title:
        session = store.update_session_title(session.id, title)
    if updates:
        session = store.update_session(
            session.id,
            agent_id=updates.get("agent_id"),
        )
        if raw_model_ref is not None:
            store.update_session_model(
                session.id,
                raw_model_ref=raw_model_ref,
                provider_id=parsed_model["provider_id"],
                model_id=parsed_model["model_id"],
                model_variant=parsed_model["model_variant"],
            )
            session = store.get_session(session.id)
    return session


def _parse_model_ref(raw_model_ref: str | None) -> dict[str, str | None]:
    if not raw_model_ref:
        return {"provider_id": None, "model_id": None, "model_variant": None}
    provider_id = None
    model_id = raw_model_ref
    variant = None
    if "/" in raw_model_ref:
        provider_id, model_id = raw_model_ref.split("/", 1)
    if "@" in model_id:
        model_id, variant = model_id.rsplit("@", 1)
    elif ":" in model_id:
        model_id, variant = model_id.rsplit(":", 1)
    return {"provider_id": provider_id or None, "model_id": model_id or None, "model_variant": variant or None}


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
