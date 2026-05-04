from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated

import typer

from harness.approvals import ApprovalProfile, ApprovalStore
from harness.backends.codex_cli import (
    AUTH_ERROR,
    CodexCliBackend,
    CodexDangerousFlagError,
    CodexEditCommandUnavailable,
    CodexSandboxUnavailable,
    CodexUnavailable,
)
from harness.backends.local_openai import LocalEndpointUnavailable, LocalOpenAICompatibleBackend
from harness.config import HARNESS_DIR, default_config, load_config, write_default_config
from harness.codex_runner import HostedBoundaryApprovalRequired, HostedSecretBlocked, CodexRepoPlanningRunner
from harness.codex_edit_runner import ActiveProjectModifiedError, ApplyBackDecision, CodexCodeEditRunner
from harness.edit_runner import NativeEditRunner, PatchApprovalDecision
from harness.isolation import ActiveRepoDirtyError
from harness.memory.sqlite_store import SQLiteStore
from harness.paths import resolve_project_root
from harness.runner import ReadOnlyRepoSummaryRunner

app = typer.Typer(help="Local-first agent harness.")
dev_app = typer.Typer(help="Phase 1A development diagnostics.")
backends_app = typer.Typer(help="Configured backend metadata and preflight checks.", invoke_without_command=True)
approvals_app = typer.Typer(help="Hosted data-boundary approval profiles.", invoke_without_command=True)
app.add_typer(dev_app, name="dev")
app.add_typer(backends_app, name="backends")
app.add_typer(approvals_app, name="approvals")

ProjectOption = Annotated[Path, typer.Option("--project", help="Project root path.")]

GITIGNORE_SECTION = """# Harness local artifacts
.harness/runs/
.harness/harness.sqlite
.harness/approvals.yaml
.harness/tmp/
*.egg-info/
"""


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
def runs(project: ProjectOption = Path(".")) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    store = SQLiteStore(project_root)
    records = store.list_runs()
    if not records:
        typer.echo("No runs found.")
        return
    for record in records:
        typer.echo(
            f"{record.id}\t{record.status}\t{record.created_at.isoformat()}\t"
            f"{record.task_type or ''}\t{record.goal or ''}\t{record.backend_name or 'none'}"
        )


@app.command()
def show(run_id: str, project: ProjectOption = Path(".")) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    store = SQLiteStore(project_root)
    try:
        record = store.get_run(run_id)
    except KeyError as exc:
        raise typer.BadParameter(str(exc)) from exc
    run_dir = store.runs_dir / run_id
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


@backends_app.callback()
def backends_callback(ctx: typer.Context, project: ProjectOption = Path(".")) -> None:
    if ctx.invoked_subcommand is not None:
        return
    project_root = resolve_project_root(project)
    cfg = load_config(project_root)
    _print_backends(cfg)


@backends_app.command("preflight")
def backends_preflight(project: ProjectOption = Path(".")) -> None:
    project_root = resolve_project_root(project)
    cfg = load_config(project_root)
    for name, backend in cfg.backends.items():
        typer.echo(f"{name}:")
        typer.echo(f"  kind: {backend.kind.value}")
        typer.echo(f"  billing_mode: {backend.metadata.billing_mode.value}")
        typer.echo(f"  execution_location: {backend.metadata.execution_location.value}")
        typer.echo(f"  data_boundary: {backend.metadata.data_boundary.value}")
        typer.echo(f"  allow_network: {backend.metadata.allow_network}")
        if name == "codex_cli":
            status = CodexCliBackend(backend).preflight()
        elif name == "local_openai_compatible":
            status = LocalOpenAICompatibleBackend(backend).preflight()
        else:
            status = None
        typer.echo(f"  available: {status.available if status else False}")
        typer.echo(f"  reason: {status.reason if status else 'Paid backend preflight skipped; disabled by default.'}")
        typer.echo("  detected_capabilities:")
        capabilities = status.capabilities if status else backend.capabilities
        for key, value in capabilities.model_dump().items():
            typer.echo(f"    {key}: {value}")


@app.command()
def run(
    goal: str,
    task_type: Annotated[str, typer.Option("--task-type", help="Task type route.")],
    project: ProjectOption = Path("."),
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
) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    cfg = load_config(project_root)
    store = SQLiteStore(project_root)
    if task_type == "read_only_repo_summary":
        backend_config = cfg.backends["local_openai_compatible"]
        backend = LocalOpenAICompatibleBackend(backend_config)
        runner = ReadOnlyRepoSummaryRunner(project_root, cfg, store, backend)
        try:
            result = runner.run(goal=goal, task_type=task_type)
        except LocalEndpointUnavailable as exc:
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
        runner = NativeEditRunner(project_root, cfg, store, backend, approval_provider)
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
        "Supported task types are read_only_repo_summary, repo_planning, simple_code_edit, and codex_code_edit."
    )


@approvals_app.callback()
def approvals_callback(ctx: typer.Context, project: ProjectOption = Path(".")) -> None:
    if ctx.invoked_subcommand is not None:
        return
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    store = ApprovalStore(project_root)
    approvals = store.list()
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
    duration_days: Annotated[int, typer.Option("--duration-days")],
    reason: Annotated[str | None, typer.Option("--reason")] = None,
    project: ProjectOption = Path("."),
) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    approval = ApprovalStore(project_root).add(
        backend=backend,
        data_boundary=data_boundary,
        task_types=[item.strip() for item in task_types.split(",") if item.strip()],
        duration_days=duration_days,
        reason=reason,
    )
    typer.echo(f"Created approval {approval.id}")


@approvals_app.command("revoke")
def approvals_revoke(approval_id: str, project: ProjectOption = Path(".")) -> None:
    project_root = resolve_project_root(project)
    _require_initialized(project_root)
    if not ApprovalStore(project_root).revoke(approval_id):
        raise typer.BadParameter(f"Approval not found: {approval_id}")
    typer.echo(f"Revoked approval {approval_id}")


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


if __name__ == "__main__":
    app()
