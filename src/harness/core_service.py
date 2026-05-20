from __future__ import annotations

from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field

from harness.execution import (
    CODEX_CODE_EDIT_TASK_TYPE,
    CODEX_ISOLATED_EDIT_ADAPTER,
    REPO_PLANNING_EXECUTION_ADAPTER,
    REPO_PLANNING_TASK_TYPE,
    execute_lease,
)
from harness.memory.sqlite_store import DRY_RUN_EXECUTION_ADAPTER, DRY_RUN_TASK_TYPE, SQLiteStore
from harness.models import (
    EventRecord,
    ObjectiveRecord,
    RunRecord,
    SessionSpec,
    TaskLease,
    run_mode_for_task_type,
)
from harness.security import sanitize_for_logging


CORE_SCHEMA_VERSION = "harness.core_run/v1"
CORE_OWNER = "core_service"
SUPPORTED_CORE_MODES = {"dry_run", "repo_planning", "codex_isolated_edit"}


class CoreEventSummary(BaseModel):
    schema_version: str = "harness.core_event_summary/v1"
    event_id: str
    run_id: str
    task_id: str | None = None
    seq: int | None = None
    event_type: str
    level: str
    message: str
    visibility: str
    redaction_state: str
    created_at: datetime


class CoreRunSummary(BaseModel):
    schema_version: str = "harness.core_summary/v1"
    ok: bool
    mode: str
    decision: str
    status: str
    task_id: str | None = None
    lease_id: str | None = None
    run_id: str | None = None
    adapter_id: str | None = None
    manifest_path: Path | None = None
    event_count: int = 0
    artifact_kinds: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    summary_text: str


class CoreSessionStartResult(BaseModel):
    schema_version: str = "harness.core_session_start/v1"
    ok: bool
    mode: str
    project_root: Path
    goal: str
    session_id: str | None = None
    objective_id: str | None = None
    errors: list[str] = Field(default_factory=list)


class CoreTaskCreationResult(BaseModel):
    schema_version: str = "harness.core_task/v1"
    ok: bool
    mode: str
    project_root: Path
    task_id: str | None = None
    session_id: str | None = None
    objective_id: str | None = None
    adapter_id: str | None = None
    task_type: str | None = None
    status: str | None = None
    errors: list[str] = Field(default_factory=list)


class CoreRunExecutionResult(BaseModel):
    schema_version: str = CORE_SCHEMA_VERSION
    ok: bool
    mode: str
    decision: str
    project_root: Path
    session_id: str | None = None
    objective_id: str | None = None
    task_id: str | None = None
    lease_id: str | None = None
    run_id: str | None = None
    adapter_id: str | None = None
    manifest: Path | None = None
    errors: list[str] = Field(default_factory=list)
    next_commands: list[str] = Field(default_factory=list)
    summary: CoreRunSummary | None = None
    task: CoreTaskCreationResult | None = None


class HarnessCoreService:
    """Headless backend entrypoint for one governed task execution slice."""

    def start_goal(
        self,
        goal: str,
        mode: str,
        project_root: Path,
        output_format: str = "json",
    ) -> CoreRunExecutionResult:
        normalized_mode = self._normalize_mode(mode)
        root = Path(project_root).resolve()
        if normalized_mode not in SUPPORTED_CORE_MODES:
            reason = (
                f"Unsupported core mode: {mode}. "
                f"Supported modes are: {', '.join(sorted(SUPPORTED_CORE_MODES))}."
            )
            return self._closed_result(
                mode=normalized_mode or mode,
                project_root=root,
                decision="unsupported_mode",
                errors=[reason],
            )

        store = SQLiteStore(root)
        store.initialize()
        try:
            session_start = self._start_session_for_goal(store, goal, normalized_mode)
            task_result = self.create_task_for_goal(
                goal=goal,
                mode=normalized_mode,
                project_root=root,
                session_id=session_start.session_id,
                objective_id=session_start.objective_id,
                output_format=output_format,
            )
            if not task_result.ok or task_result.task_id is None:
                return self._closed_result(
                    mode=normalized_mode,
                    project_root=root,
                    decision="task_creation_failed",
                    session_id=session_start.session_id,
                    objective_id=session_start.objective_id,
                    task=task_result,
                    errors=task_result.errors or ["Task creation failed."],
                )
            return self.run_task(
                task_id=task_result.task_id,
                mode=normalized_mode,
                project_root=root,
                session_id=session_start.session_id,
                objective_id=session_start.objective_id,
                task=task_result,
            )
        except Exception as exc:
            reason = str(sanitize_for_logging(str(exc)))
            return self._closed_result(
                mode=normalized_mode,
                project_root=root,
                decision="core_service_failed",
                errors=[reason],
            )

    def create_task_for_goal(
        self,
        *,
        goal: str,
        mode: str,
        project_root: Path,
        session_id: str | None = None,
        objective_id: str | None = None,
        output_format: str = "json",
    ) -> CoreTaskCreationResult:
        root = Path(project_root).resolve()
        normalized_mode = self._normalize_mode(mode)
        if normalized_mode not in SUPPORTED_CORE_MODES:
            return CoreTaskCreationResult(
                ok=False,
                mode=normalized_mode or mode,
                project_root=root,
                errors=[f"Unsupported core mode: {mode}."],
            )
        store = SQLiteStore(root)
        store.initialize()
        adapter_id, task_type = self._adapter_metadata(normalized_mode)
        task = store.create_task(
            title=str(sanitize_for_logging(goal)),
            description="",
            priority=0,
            objective_id=objective_id,
            session_id=session_id,
            metadata={
                "execution_adapter": adapter_id,
                "task_type": task_type,
                "core_service_mode": normalized_mode,
                "core_output_format": output_format,
            },
        )
        return CoreTaskCreationResult(
            ok=True,
            mode=normalized_mode,
            project_root=root,
            task_id=task.id,
            session_id=session_id,
            objective_id=objective_id,
            adapter_id=adapter_id,
            task_type=task_type,
            status=task.status.value,
        )

    def run_task(
        self,
        *,
        task_id: str,
        mode: str,
        project_root: Path,
        session_id: str | None = None,
        objective_id: str | None = None,
        task: CoreTaskCreationResult | None = None,
    ) -> CoreRunExecutionResult:
        root = Path(project_root).resolve()
        normalized_mode = self._normalize_mode(mode)
        store = SQLiteStore(root)
        store.initialize()
        task_record = store.get_task(task_id)
        selection = store.select_next_task_for_lease(owner=CORE_OWNER, objective_id=task_record.objective_id)
        if selection is None or selection["task"].id != task_id:
            return self._closed_result(
                mode=normalized_mode,
                project_root=root,
                decision="lease_unavailable",
                session_id=session_id or task_record.session_id,
                objective_id=objective_id or task_record.objective_id,
                task_id=task_id,
                adapter_id=str(task_record.metadata.get("execution_adapter") or ""),
                task=task,
                errors=[f"Unable to acquire a lease for task {task_id}."],
            )

        lease = selection["lease"]
        dispatch = execute_lease(root, lease.id, owner=CORE_OWNER)
        run_id = dispatch.run.id if dispatch.run is not None else None
        if session_id is not None and run_id is not None:
            store.attach_session_to_run(session_id, run_id)
            dispatch.run = store.get_run(run_id)
            dispatch.manifest = store.build_run_manifest(run_id)
        manifest_path = self._manifest_path(root, run_id) if run_id is not None else None
        errors = list(dispatch.errors or dispatch.rejection_reasons)
        summary = self._build_summary(
            store=store,
            mode=normalized_mode,
            ok=dispatch.ok,
            decision=dispatch.decision,
            task_id=dispatch.task.id if dispatch.task is not None else task_id,
            lease_id=dispatch.lease.id if dispatch.lease is not None else lease.id,
            run_id=run_id,
            adapter_id=dispatch.adapter_id,
            manifest_path=manifest_path,
            errors=errors,
        )
        return CoreRunExecutionResult(
            ok=dispatch.ok,
            mode=normalized_mode,
            decision=dispatch.decision,
            project_root=root,
            session_id=session_id or (dispatch.run.session_id if dispatch.run is not None else task_record.session_id),
            objective_id=objective_id or task_record.objective_id,
            task_id=dispatch.task.id if dispatch.task is not None else task_id,
            lease_id=dispatch.lease.id if dispatch.lease is not None else lease.id,
            run_id=run_id,
            adapter_id=dispatch.adapter_id,
            manifest=manifest_path,
            errors=errors,
            next_commands=self._next_commands(root, run_id, task_id, dispatch.lease.id if dispatch.lease else lease.id),
            summary=summary,
            task=task,
        )

    def get_run_summary(self, run_id: str, project_root: Path) -> CoreRunSummary:
        root = Path(project_root).resolve()
        store = SQLiteStore(root)
        store.initialize()
        run = store.get_run(run_id)
        task = store.get_task(run.task_id) if run.task_id is not None else None
        lease = self._latest_task_lease(store, task.id) if task is not None else None
        adapter_id = str(task.metadata.get("execution_adapter")) if task is not None else None
        mode = self._mode_from_adapter(adapter_id, run.task_type)
        decision = self._decision_from_run(run)
        errors = [] if run.status in {"completed", "completed_applied", "completed_denied", "completed_no_changes"} else [run.status]
        return self._build_summary(
            store=store,
            mode=mode,
            ok=not errors,
            decision=decision,
            task_id=run.task_id,
            lease_id=lease.id if lease is not None else None,
            run_id=run.id,
            adapter_id=adapter_id,
            manifest_path=self._manifest_path(root, run.id),
            errors=errors,
        )

    def list_run_events(self, run_id: str, project_root: Path) -> list[CoreEventSummary]:
        root = Path(project_root).resolve()
        store = SQLiteStore(root)
        store.initialize()
        return [self._event_summary(event) for event in store.list_events(run_id)]

    def _start_session_for_goal(self, store: SQLiteStore, goal: str, mode: str) -> CoreSessionStartResult:
        adapter_id, task_type = self._adapter_metadata(mode)
        session: SessionSpec | None = store.create_session(
            title=str(sanitize_for_logging(goal))[:120],
            mode=run_mode_for_task_type(task_type).value,
            intent="core_service_goal",
            metadata={
                "core_service": True,
                "core_mode": mode,
                "execution_adapter": adapter_id,
                "task_type": task_type,
            },
        )
        objective: ObjectiveRecord = store.create_objective(
            title=str(sanitize_for_logging(goal)),
            description="Headless core service objective for a single goal.",
            session_id=session.id if session is not None else None,
            metadata={"core_service": True, "core_mode": mode},
        )
        if session is not None:
            store.attach_session_to_objective(session.id, objective.id)
        return CoreSessionStartResult(
            ok=True,
            mode=mode,
            project_root=store.project_root,
            goal=str(sanitize_for_logging(goal)),
            session_id=session.id if session is not None else None,
            objective_id=objective.id,
        )

    def _build_summary(
        self,
        *,
        store: SQLiteStore,
        mode: str,
        ok: bool,
        decision: str,
        task_id: str | None,
        lease_id: str | None,
        run_id: str | None,
        adapter_id: str | None,
        manifest_path: Path | None,
        errors: list[str],
    ) -> CoreRunSummary:
        status = "blocked"
        event_count = 0
        artifact_kinds: list[str] = []
        if run_id is not None:
            try:
                run = store.get_run(run_id)
                status = run.status
                event_count = len(store.list_events(run_id))
                artifact_kinds = sorted({artifact.kind for artifact in store.list_artifacts(run_id)})
            except KeyError:
                status = "missing_run"
        elif ok:
            status = "completed"
        text = (
            f"Core run decision={decision}; status={status}; run_id={run_id or 'none'}; "
            f"task_id={task_id or 'none'}; lease_id={lease_id or 'none'}; "
            f"adapter_id={adapter_id or 'none'}; manifest={manifest_path or 'none'}; "
            f"errors={'; '.join(errors) if errors else 'none'}."
        )
        return CoreRunSummary(
            ok=ok,
            mode=mode,
            decision=decision,
            status=status,
            task_id=task_id,
            lease_id=lease_id,
            run_id=run_id,
            adapter_id=adapter_id,
            manifest_path=manifest_path,
            event_count=event_count,
            artifact_kinds=artifact_kinds,
            errors=errors,
            summary_text=text,
        )

    def _closed_result(
        self,
        *,
        mode: str,
        project_root: Path,
        decision: str,
        errors: list[str],
        session_id: str | None = None,
        objective_id: str | None = None,
        task_id: str | None = None,
        adapter_id: str | None = None,
        task: CoreTaskCreationResult | None = None,
    ) -> CoreRunExecutionResult:
        summary = CoreRunSummary(
            ok=False,
            mode=mode,
            decision=decision,
            status="blocked",
            task_id=task_id,
            adapter_id=adapter_id,
            errors=errors,
            summary_text=(
                f"Core run decision={decision}; status=blocked; run_id=none; "
                f"task_id={task_id or 'none'}; lease_id=none; adapter_id={adapter_id or 'none'}; "
                f"manifest=none; errors={'; '.join(errors) if errors else 'none'}."
            ),
        )
        return CoreRunExecutionResult(
            ok=False,
            mode=mode,
            decision=decision,
            project_root=Path(project_root).resolve(),
            session_id=session_id,
            objective_id=objective_id,
            task_id=task_id,
            adapter_id=adapter_id,
            errors=errors,
            next_commands=self._next_commands(Path(project_root).resolve(), None, task_id, None),
            summary=summary,
            task=task,
        )

    def _next_commands(self, project_root: Path, run_id: str | None, task_id: str | None, lease_id: str | None) -> list[str]:
        project = str(project_root)
        commands: list[str] = []
        if run_id is not None:
            commands.extend(
                [
                    f"harness show {run_id} --project {project} --output json",
                    f"harness core inspect-events {run_id} --project {project} --output json",
                    f"harness events {run_id} --project {project} --jsonl",
                    f"harness artifacts list {run_id} --project {project} --output json",
                ]
            )
        if task_id is not None:
            commands.append(f"harness core inspect-task {task_id} --project {project} --output json")
            commands.append(f"harness tasks inspect {task_id} --project {project} --output json")
        if lease_id is not None:
            commands.append(f"harness daemon inspect-lease {lease_id} --project {project} --output json")
        return commands

    def _adapter_metadata(self, mode: str) -> tuple[str, str]:
        if mode == "dry_run":
            return DRY_RUN_EXECUTION_ADAPTER, DRY_RUN_TASK_TYPE
        if mode == "repo_planning":
            return REPO_PLANNING_EXECUTION_ADAPTER, REPO_PLANNING_TASK_TYPE
        if mode == "codex_isolated_edit":
            return CODEX_ISOLATED_EDIT_ADAPTER, CODEX_CODE_EDIT_TASK_TYPE
        raise ValueError(f"Unsupported core mode: {mode}")

    def _mode_from_adapter(self, adapter_id: str | None, task_type: str | None) -> str:
        if adapter_id in SUPPORTED_CORE_MODES:
            return adapter_id
        if task_type == DRY_RUN_TASK_TYPE:
            return "dry_run"
        if task_type == REPO_PLANNING_TASK_TYPE:
            return "repo_planning"
        if task_type == CODEX_CODE_EDIT_TASK_TYPE:
            return "codex_isolated_edit"
        return "unknown"

    def _decision_from_run(self, run: RunRecord) -> str:
        if run.status in {"completed", "completed_applied", "completed_denied", "completed_no_changes"}:
            return f"{run.task_type or 'run'}_completed"
        return f"{run.task_type or 'run'}_{run.status}"

    def _latest_task_lease(self, store: SQLiteStore, task_id: str) -> TaskLease | None:
        leases = store.list_task_leases(task_id)
        return leases[-1] if leases else None

    def _event_summary(self, event: EventRecord) -> CoreEventSummary:
        return CoreEventSummary(
            event_id=event.id,
            run_id=event.run_id,
            task_id=event.task_id,
            seq=event.seq,
            event_type=event.event_type,
            level=event.level,
            message=event.message,
            visibility=event.visibility.value,
            redaction_state=event.redaction_state.value,
            created_at=event.created_at,
        )

    def _manifest_path(self, project_root: Path, run_id: str | None) -> Path | None:
        if run_id is None:
            return None
        path = Path(project_root).resolve() / ".harness" / "runs" / run_id / "manifest.json"
        return path if path.exists() else None

    def _normalize_mode(self, mode: str) -> str:
        return str(mode or "").strip().lower().replace("-", "_")
