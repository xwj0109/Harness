from __future__ import annotations

import sqlite3
from pathlib import Path

from pydantic import BaseModel, Field

from harness.memory.sqlite_store import SQLiteStore
from harness.models import (
    ArtifactRecord,
    EventRecord,
    EventStreamType,
    ObjectiveRecord,
    RunRecord,
    StoredEventRecord,
    TaskAttempt,
    TaskDependency,
    TaskLease,
    TaskRecord,
)
from harness.paths import resolve_project_root


class OrchestrationStateSnapshot(BaseModel):
    schema_version: str = "harness.orchestration_state_snapshot/v1"
    ok: bool = True
    project_root: Path
    initialized: bool = False
    objectives: list[ObjectiveRecord] = Field(default_factory=list)
    tasks: list[TaskRecord] = Field(default_factory=list)
    dependencies: list[TaskDependency] = Field(default_factory=list)
    attempts: list[TaskAttempt] = Field(default_factory=list)
    leases: list[TaskLease] = Field(default_factory=list)
    runs: list[RunRecord] = Field(default_factory=list)
    artifacts_by_run: dict[str, list[ArtifactRecord]] = Field(default_factory=dict)
    run_events_by_run: dict[str, list[EventRecord]] = Field(default_factory=dict)
    orchestration_events_by_objective: dict[str, list[StoredEventRecord]] = Field(default_factory=dict)
    error: str | None = None


def load_orchestration_state(project_root: Path) -> OrchestrationStateSnapshot:
    project_root = resolve_project_root(project_root)
    store = SQLiteStore(project_root)
    if not store.db_path.exists():
        return OrchestrationStateSnapshot(project_root=project_root, initialized=False)
    try:
        objectives = store.list_objectives()
        tasks = store.list_tasks()
        runs = store.list_runs()
        artifacts_by_run = {run.id: store.list_artifacts(run.id) for run in runs}
        run_events_by_run = {run.id: store.list_events(run.id) for run in runs}
        orchestration_events_by_objective = {
            objective.id: store.list_store_events(EventStreamType.ORCHESTRATION, objective.id)
            for objective in objectives
        }
        return OrchestrationStateSnapshot(
            project_root=project_root,
            initialized=True,
            objectives=objectives,
            tasks=tasks,
            dependencies=store.list_task_dependencies(),
            attempts=store.list_task_attempts(),
            leases=store.list_task_leases(),
            runs=runs,
            artifacts_by_run=artifacts_by_run,
            run_events_by_run=run_events_by_run,
            orchestration_events_by_objective=orchestration_events_by_objective,
        )
    except sqlite3.Error as exc:
        return OrchestrationStateSnapshot(
            project_root=project_root,
            initialized=False,
            ok=False,
            error=f"{exc.__class__.__name__}: {exc}",
        )
