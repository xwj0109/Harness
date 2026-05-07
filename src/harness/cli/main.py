from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from enum import Enum
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
from harness.sandbox import CommandValidationError, DockerImageManager
from harness.test_runner import DockerTestRunner, RunTestsDecision

app = typer.Typer(help="Local-first agent harness.")
dev_app = typer.Typer(help="Phase 1A development diagnostics.")
backends_app = typer.Typer(help="Configured backend metadata and preflight checks.", invoke_without_command=True)
approvals_app = typer.Typer(help="Hosted data-boundary approval profiles.", invoke_without_command=True)
tests_app = typer.Typer(help="Docker-sandboxed test execution.")
tests_image_app = typer.Typer(help="Managed Docker test image helpers.")
app.add_typer(dev_app, name="dev")
app.add_typer(backends_app, name="backends")
app.add_typer(approvals_app, name="approvals")
app.add_typer(tests_app, name="tests")
tests_app.add_typer(tests_image_app, name="image")

ProjectOption = Annotated[Path, typer.Option("--project", help="Project root path.")]


class OutputFormat(str, Enum):
    TEXT = "text"
    JSON = "json"


OutputOption = Annotated[OutputFormat, typer.Option("--output", help="Output format.")]

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
    for record in records:
        typer.echo(
            f"{record.id}\t{record.status}\t{record.created_at.isoformat()}\t"
            f"{record.task_type or ''}\t{record.goal or ''}\t{record.backend_name or 'none'}"
        )


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
def doctor(project: ProjectOption = Path("."), output: OutputOption = OutputFormat.TEXT) -> None:
    project_root = resolve_project_root(project)
    result = _doctor_result(project_root)
    if output == OutputFormat.JSON:
        _emit_json(result)
    else:
        typer.echo(f"Project: {result['project_root']}")
        typer.echo(f"Overall: {'pass' if result['ok'] else 'fail'}")
        for check in result["checks"]:
            typer.echo(f"{check['status']}\t{check['id']}\t{check['message']}")
    if not result["ok"]:
        raise typer.Exit(code=1)


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
        "Supported task types are read_only_repo_summary, repo_planning, simple_code_edit, and codex_code_edit."
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


def _emit_json(payload) -> None:
    typer.echo(json.dumps(payload, indent=2, sort_keys=True))


def _doctor_result(project_root: Path) -> dict:
    checks: list[dict] = []
    harness_dir = project_root / HARNESS_DIR
    config = None

    _add_check(
        checks,
        "initialized",
        "pass" if (harness_dir / "harness.sqlite").exists() else "fail",
        "Harness SQLite state exists." if (harness_dir / "harness.sqlite").exists() else "Project is not initialized.",
        {"path": str(harness_dir / "harness.sqlite")},
    )

    try:
        config = load_config(project_root)
    except Exception as exc:
        _add_check(
            checks,
            "config_loadable",
            "fail",
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

    if config is not None:
        _doctor_backend_descriptors(checks, config)
        _doctor_backend_preflight(checks, config)
        _doctor_docker_binary(checks)
        _doctor_dockerfile_validation(checks, project_root, config)
        _doctor_sandbox_safety(checks, config)

    return {
        "schema_version": "harness.doctor/v1",
        "project_root": str(project_root),
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
