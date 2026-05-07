from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from harness.events import append_jsonl
from harness.models import (
    ArtifactRecord,
    BackendCapabilities,
    BackendConfig,
    BackendDescriptor,
    BackendKind,
    BackendMetadata,
    EventRecord,
    ManifestArtifact,
    ObjectiveRecord,
    ObjectiveStatus,
    RunManifest,
    RunRecord,
    TaskDependency,
    TaskDependencyType,
    TaskRecord,
    TaskStatus,
    TaskTransitionRecord,
    run_mode_for_task_type,
)
from harness.security import sanitize_for_logging

LEGACY_TASK_STATUS_VALUES = {
    "queued": TaskStatus.READY,
    "completed": TaskStatus.SUCCEEDED,
    "canceled": TaskStatus.CANCELLED,
}

TASK_STATUS_QUERY_ALIASES = {
    TaskStatus.READY: ("ready", "queued"),
    TaskStatus.SUCCEEDED: ("succeeded", "completed"),
    TaskStatus.CANCELLED: ("cancelled", "canceled"),
}

ALLOWED_TASK_TRANSITIONS = {
    TaskStatus.CREATED: {TaskStatus.READY, TaskStatus.BLOCKED, TaskStatus.WAITING_APPROVAL},
    TaskStatus.READY: {
        TaskStatus.BLOCKED,
        TaskStatus.WAITING_APPROVAL,
        TaskStatus.LEASED,
        TaskStatus.RUNNING,
        TaskStatus.SUCCEEDED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
        TaskStatus.SKIPPED,
    },
    TaskStatus.BLOCKED: {TaskStatus.READY, TaskStatus.CANCELLED, TaskStatus.SKIPPED},
    TaskStatus.WAITING_APPROVAL: {TaskStatus.READY, TaskStatus.CANCELLED, TaskStatus.SKIPPED},
    TaskStatus.LEASED: {TaskStatus.RUNNING, TaskStatus.READY, TaskStatus.FAILED, TaskStatus.CANCELLED},
    TaskStatus.RUNNING: {
        TaskStatus.SUCCEEDED,
        TaskStatus.FAILED,
        TaskStatus.WAITING_APPROVAL,
        TaskStatus.CANCELLED,
    },
    TaskStatus.FAILED: {TaskStatus.READY, TaskStatus.CANCELLED},
    TaskStatus.SUCCEEDED: set(),
    TaskStatus.CANCELLED: set(),
    TaskStatus.SKIPPED: set(),
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def normalize_task_status(status: str | TaskStatus) -> TaskStatus:
    return TaskStatus(status.value if isinstance(status, TaskStatus) else status)


def validate_task_transition(from_status: str | TaskStatus, to_status: str | TaskStatus) -> None:
    current = normalize_task_status(from_status)
    next_status = normalize_task_status(to_status)
    if current == next_status:
        return
    if next_status not in ALLOWED_TASK_TRANSITIONS[current]:
        raise ValueError(f"Invalid task transition: {current.value} -> {next_status.value}")


class SQLiteStore:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root.resolve()
        self.harness_dir = self.project_root / ".harness"
        self.db_path = self.harness_dir / "harness.sqlite"
        self.runs_dir = self.harness_dir / "runs"

    def initialize(self) -> None:
        self.harness_dir.mkdir(parents=True, exist_ok=True)
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        (self.harness_dir / "tmp").mkdir(parents=True, exist_ok=True)
        approvals = self.harness_dir / "approvals.yaml"
        if not approvals.exists():
            approvals.write_text("approvals: []\n", encoding="utf-8")
        schema = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
        with self.connect() as conn:
            conn.executescript(schema)
            self._ensure_column(conn, "runs", "approval_id", "TEXT")
            self._ensure_column(conn, "tasks", "objective_id", "TEXT")
            self._ensure_column(conn, "tasks", "idempotency_key", "TEXT")
            self._ensure_column(conn, "tasks", "required_approvals_json", "TEXT")
            self._ensure_column(conn, "tasks", "approval_state", "TEXT")
            self._migrate_task_rows(conn)

    def _ensure_column(self, conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    def _migrate_task_rows(self, conn: sqlite3.Connection) -> None:
        timestamp = now_iso()
        for legacy, canonical in LEGACY_TASK_STATUS_VALUES.items():
            conn.execute("UPDATE tasks SET status = ? WHERE status = ?", (canonical.value, legacy))
        conn.execute(
            """
            UPDATE tasks
            SET idempotency_key = 'task_idem_' || lower(hex(randomblob(8)))
            WHERE idempotency_key IS NULL OR idempotency_key = ''
            """
        )
        conn.execute(
            """
            UPDATE tasks
            SET required_approvals_json = '[]'
            WHERE required_approvals_json IS NULL OR required_approvals_json = ''
            """
        )
        conn.execute(
            """
            UPDATE tasks
            SET updated_at = ?
            WHERE updated_at IS NULL OR updated_at = ''
            """,
            (timestamp,),
        )

    def connect(self) -> sqlite3.Connection:
        self.harness_dir.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def create_run(
        self,
        goal: str | None,
        task_type: str | None,
        status: str = "created",
        backend: BackendConfig | None = None,
        approval_id: str | None = None,
    ) -> RunRecord:
        run_id = f"run_{uuid.uuid4().hex[:12]}"
        timestamp = now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO runs (
                  id, goal, task_type, status, project_root, created_at, updated_at,
                  backend_name, backend_kind, billing_mode, execution_location,
                  data_boundary, allow_network, approval_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    goal,
                    task_type,
                    status,
                    str(self.project_root),
                    timestamp,
                    timestamp,
                    backend.name if backend else None,
                    backend.kind.value if backend else None,
                    backend.metadata.billing_mode.value if backend else None,
                    backend.metadata.execution_location.value if backend else None,
                    backend.metadata.data_boundary.value if backend else None,
                    int(backend.metadata.allow_network) if backend else None,
                    approval_id,
                ),
            )
        self.initialize_run_artifacts(run_id)
        if backend:
            self.persist_backend_snapshot(run_id, backend)
        self.write_run_manifest(run_id)
        return self.get_run(run_id)

    def initialize_run_artifacts(self, run_id: str) -> dict[str, Path]:
        run_dir = self.runs_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        paths = {
            "events": run_dir / "events.jsonl",
            "transcript": run_dir / "transcript.jsonl",
            "final_report": run_dir / "final_report.md",
            "manifest": run_dir / "manifest.json",
        }
        for path in paths.values():
            path.touch(exist_ok=True)
        return paths

    def list_runs(self) -> list[RunRecord]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM runs ORDER BY created_at DESC").fetchall()
        return [self._row_to_run(row) for row in rows]

    def get_run(self, run_id: str) -> RunRecord:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"Run not found: {run_id}")
        return self._row_to_run(row)

    def update_run_status(self, run_id: str, status: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE runs SET status = ?, updated_at = ? WHERE id = ?",
                (status, now_iso(), run_id),
            )
        self.write_run_manifest(run_id)

    def create_objective(
        self,
        title: str,
        description: str = "",
        priority: int = 0,
        workbench_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ObjectiveRecord:
        objective_id = f"obj_{uuid.uuid4().hex[:12]}"
        timestamp = now_iso()
        metadata = metadata or {}
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO objectives (
                  id, title, description, status, project_root, created_at, updated_at,
                  priority, workbench_id, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    objective_id,
                    title,
                    description,
                    ObjectiveStatus.ACTIVE.value,
                    str(self.project_root),
                    timestamp,
                    timestamp,
                    priority,
                    workbench_id,
                    json.dumps(sanitize_for_logging(metadata), sort_keys=True, default=str),
                ),
            )
        return self.get_objective(objective_id)

    def list_objectives(self) -> list[ObjectiveRecord]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM objectives ORDER BY priority DESC, created_at ASC"
            ).fetchall()
        return [self._row_to_objective(row) for row in rows]

    def get_objective(self, objective_id: str) -> ObjectiveRecord:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM objectives WHERE id = ?", (objective_id,)).fetchone()
        if row is None:
            raise KeyError(f"Objective not found: {objective_id}")
        return self._row_to_objective(row)

    def create_task(
        self,
        title: str,
        description: str = "",
        priority: int = 0,
        objective_id: str | None = None,
        workbench_id: str | None = None,
        agent_id: str | None = None,
        spec_source_kind: str | None = None,
        spec_source_path: Path | None = None,
        depends_on: list[str] | None = None,
        required_approvals: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> TaskRecord:
        task_id = f"task_{uuid.uuid4().hex[:12]}"
        idempotency_key = f"task_idem_{uuid.uuid4().hex[:16]}"
        timestamp = now_iso()
        depends_on = depends_on or []
        required_approvals = required_approvals or []
        metadata = metadata or {}
        with self.connect() as conn:
            if objective_id is not None:
                self._require_objective(conn, objective_id)
            for dependency_id in depends_on:
                self._require_task(conn, dependency_id)
            dependencies_satisfied = self._dependency_ids_completed(conn, depends_on)
            initial_status = (
                TaskStatus.WAITING_APPROVAL
                if required_approvals
                else TaskStatus.BLOCKED
                if not dependencies_satisfied
                else TaskStatus.READY
            )
            conn.execute(
                """
                INSERT INTO tasks (
                  id, title, description, status, project_root, created_at, updated_at,
                  priority, objective_id, workbench_id, agent_id, spec_source_kind, spec_source_path,
                  depends_on_json, run_id, metadata_json, idempotency_key,
                  required_approvals_json, approval_state
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    title,
                    description,
                    initial_status.value,
                    str(self.project_root),
                    timestamp,
                    timestamp,
                    priority,
                    objective_id,
                    workbench_id,
                    agent_id,
                    spec_source_kind,
                    str(spec_source_path) if spec_source_path is not None else None,
                    json.dumps(depends_on, sort_keys=True),
                    None,
                    json.dumps(sanitize_for_logging(metadata), sort_keys=True, default=str),
                    idempotency_key,
                    json.dumps(sanitize_for_logging(required_approvals), sort_keys=True, default=str),
                    "required" if required_approvals else None,
                ),
            )
            for dependency_id in depends_on:
                self._create_task_dependency(
                    conn,
                    upstream_task_id=dependency_id,
                    downstream_task_id=task_id,
                    dependency_type=TaskDependencyType.SUCCESS,
                    required_artifact_kind=None,
                    created_at=timestamp,
                )
            self._record_task_transition(
                conn,
                task_id=task_id,
                from_status=None,
                to_status=initial_status,
                reason="task_created",
                actor="system",
                metadata={},
                created_at=timestamp,
            )
        return self.get_task(task_id)

    def list_tasks(self, status: str | None = None, objective_id: str | None = None) -> list[TaskRecord]:
        with self.connect() as conn:
            if objective_id is not None:
                self._require_objective(conn, objective_id)
            if status is None:
                if objective_id is None:
                    rows = conn.execute(
                        "SELECT * FROM tasks ORDER BY priority DESC, created_at ASC"
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """
                        SELECT * FROM tasks
                        WHERE objective_id = ?
                        ORDER BY priority DESC, created_at ASC
                        """,
                        (objective_id,),
                    ).fetchall()
            else:
                query_status = normalize_task_status(status)
                status_values = TASK_STATUS_QUERY_ALIASES.get(query_status, (query_status.value,))
                placeholders = ", ".join("?" for _ in status_values)
                if objective_id is None:
                    rows = conn.execute(
                        f"""
                        SELECT * FROM tasks
                        WHERE status IN ({placeholders})
                        ORDER BY priority DESC, created_at ASC
                        """,
                        status_values,
                    ).fetchall()
                else:
                    rows = conn.execute(
                        f"""
                        SELECT * FROM tasks
                        WHERE status IN ({placeholders}) AND objective_id = ?
                        ORDER BY priority DESC, created_at ASC
                        """,
                        (*status_values, objective_id),
                    ).fetchall()
        return [self._row_to_task(row) for row in rows]

    def get_task(self, task_id: str) -> TaskRecord:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(f"Task not found: {task_id}")
        return self._row_to_task(row)

    def update_task_status(
        self,
        task_id: str,
        status: str | TaskStatus,
        *,
        run_id: str | None = None,
    ) -> TaskRecord:
        next_status = normalize_task_status(status)
        if run_id is not None:
            self.get_run(run_id)
        timestamp = now_iso()
        with self.connect() as conn:
            row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(f"Task not found: {task_id}")
            current_status = normalize_task_status(row["status"])
            validate_task_transition(current_status, next_status)
            result = conn.execute(
                "UPDATE tasks SET status = ?, updated_at = ?, run_id = COALESCE(?, run_id) WHERE id = ?",
                (next_status.value, timestamp, run_id, task_id),
            )
            if current_status != next_status:
                self._record_task_transition(
                    conn,
                    task_id=task_id,
                    from_status=current_status,
                    to_status=next_status,
                    reason="status_updated",
                    actor="operator",
                    metadata={"run_id": run_id} if run_id is not None else {},
                    created_at=timestamp,
                )
        if result.rowcount == 0:
            raise KeyError(f"Task not found: {task_id}")
        return self.get_task(task_id)

    def select_next_task(self) -> TaskRecord | None:
        candidates = [
            task
            for task in self.list_tasks()
            if task.status in {TaskStatus.READY, TaskStatus.BLOCKED}
        ]
        for task in candidates:
            if not self._task_dependencies_completed(task):
                continue
            if task.required_approvals:
                continue
            if task.status == TaskStatus.BLOCKED:
                task = self.update_task_status(task.id, TaskStatus.READY)
            return self.update_task_status(task.id, TaskStatus.RUNNING)
        return None

    def _task_dependencies_completed(self, task: TaskRecord) -> bool:
        for dependency_id in task.depends_on:
            try:
                dependency = self.get_task(dependency_id)
            except KeyError:
                return False
            if dependency.status != TaskStatus.SUCCEEDED:
                return False
        return True

    def _dependency_ids_completed(self, conn: sqlite3.Connection, dependency_ids: list[str]) -> bool:
        for dependency_id in dependency_ids:
            row = conn.execute("SELECT status FROM tasks WHERE id = ?", (dependency_id,)).fetchone()
            if row is None or normalize_task_status(row["status"]) != TaskStatus.SUCCEEDED:
                return False
        return True

    def create_task_dependency(
        self,
        upstream_task_id: str,
        downstream_task_id: str,
        dependency_type: TaskDependencyType = TaskDependencyType.SUCCESS,
        required_artifact_kind: str | None = None,
    ) -> TaskDependency:
        timestamp = now_iso()
        with self.connect() as conn:
            self._require_task(conn, upstream_task_id)
            self._require_task(conn, downstream_task_id)
            return self._create_task_dependency(
                conn,
                upstream_task_id=upstream_task_id,
                downstream_task_id=downstream_task_id,
                dependency_type=dependency_type,
                required_artifact_kind=required_artifact_kind,
                created_at=timestamp,
            )

    def list_task_dependencies(self, task_id: str | None = None) -> list[TaskDependency]:
        with self.connect() as conn:
            if task_id is None:
                rows = conn.execute(
                    "SELECT * FROM task_dependencies ORDER BY created_at ASC, id ASC"
                ).fetchall()
            else:
                self._require_task(conn, task_id)
                rows = conn.execute(
                    """
                    SELECT * FROM task_dependencies
                    WHERE upstream_task_id = ? OR downstream_task_id = ?
                    ORDER BY created_at ASC, id ASC
                    """,
                    (task_id, task_id),
                ).fetchall()
        return [self._row_to_task_dependency(row) for row in rows]

    def build_task_graph(self, objective_id: str | None = None) -> dict[str, Any]:
        tasks = self.list_tasks(objective_id=objective_id)
        task_ids = {task.id for task in tasks}
        objectives = self.list_objectives()
        if objective_id is not None:
            objectives = [objective for objective in objectives if objective.id == objective_id]
        dependencies = [
            dependency
            for dependency in self.list_task_dependencies()
            if dependency.upstream_task_id in task_ids or dependency.downstream_task_id in task_ids
        ]
        blocked_reasons = {task.id: self._blocked_reasons(task) for task in tasks}
        return {
            "objectives": [objective.model_dump(mode="json") for objective in objectives],
            "tasks": [task.model_dump(mode="json") for task in tasks],
            "dependencies": [dependency.model_dump(mode="json") for dependency in dependencies],
            "blocked_reasons": blocked_reasons,
        }

    def _blocked_reasons(self, task: TaskRecord) -> list[dict[str, Any]]:
        reasons: list[dict[str, Any]] = []
        for dependency_id in task.depends_on:
            try:
                dependency = self.get_task(dependency_id)
            except KeyError:
                reasons.append({"kind": "missing_dependency", "task_id": dependency_id})
                continue
            if dependency.status != TaskStatus.SUCCEEDED:
                reasons.append(
                    {
                        "kind": "unsatisfied_dependency",
                        "task_id": dependency_id,
                        "status": dependency.status.value,
                    }
                )
        if task.required_approvals:
            reasons.append(
                {
                    "kind": "unresolved_required_approvals",
                    "required_approvals": task.required_approvals,
                    "approval_state": task.approval_state,
                }
            )
        return reasons

    def _create_task_dependency(
        self,
        conn: sqlite3.Connection,
        *,
        upstream_task_id: str,
        downstream_task_id: str,
        dependency_type: TaskDependencyType,
        required_artifact_kind: str | None,
        created_at: str,
    ) -> TaskDependency:
        if upstream_task_id == downstream_task_id:
            raise ValueError("Task cannot depend on itself")
        if self._dependency_path_exists(conn, downstream_task_id, upstream_task_id):
            raise ValueError(
                f"Task dependency cycle detected: {upstream_task_id} -> {downstream_task_id}"
            )
        dependency_id = f"task_dep_{uuid.uuid4().hex[:12]}"
        conn.execute(
            """
            INSERT INTO task_dependencies (
              id, upstream_task_id, downstream_task_id, dependency_type,
              required_artifact_kind, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                dependency_id,
                upstream_task_id,
                downstream_task_id,
                dependency_type.value,
                required_artifact_kind,
                created_at,
            ),
        )
        return TaskDependency(
            id=dependency_id,
            upstream_task_id=upstream_task_id,
            downstream_task_id=downstream_task_id,
            dependency_type=dependency_type,
            required_artifact_kind=required_artifact_kind,
            created_at=parse_dt(created_at),
        )

    def _dependency_path_exists(
        self,
        conn: sqlite3.Connection,
        start_task_id: str,
        target_task_id: str,
    ) -> bool:
        seen: set[str] = set()
        stack = [start_task_id]
        while stack:
            current = stack.pop()
            if current == target_task_id:
                return True
            if current in seen:
                continue
            seen.add(current)
            rows = conn.execute(
                "SELECT downstream_task_id FROM task_dependencies WHERE upstream_task_id = ?",
                (current,),
            ).fetchall()
            stack.extend(row["downstream_task_id"] for row in rows)
        return False

    def _require_task(self, conn: sqlite3.Connection, task_id: str) -> None:
        row = conn.execute("SELECT id FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(f"Task not found: {task_id}")

    def _require_objective(self, conn: sqlite3.Connection, objective_id: str) -> None:
        row = conn.execute("SELECT id FROM objectives WHERE id = ?", (objective_id,)).fetchone()
        if row is None:
            raise KeyError(f"Objective not found: {objective_id}")

    def list_task_transitions(self, task_id: str) -> list[TaskTransitionRecord]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM task_transitions
                WHERE task_id = ?
                ORDER BY created_at ASC, id ASC
                """,
                (task_id,),
            ).fetchall()
        return [self._row_to_task_transition(row) for row in rows]

    def _record_task_transition(
        self,
        conn: sqlite3.Connection,
        *,
        task_id: str,
        from_status: TaskStatus | None,
        to_status: TaskStatus,
        reason: str,
        actor: str,
        metadata: dict[str, Any],
        created_at: str,
    ) -> None:
        transition_id = f"task_transition_{uuid.uuid4().hex[:12]}"
        conn.execute(
            """
            INSERT INTO task_transitions (
              id, task_id, from_status, to_status, reason, actor, created_at, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                transition_id,
                task_id,
                from_status.value if from_status is not None else None,
                to_status.value,
                reason,
                actor,
                created_at,
                json.dumps(sanitize_for_logging(metadata), sort_keys=True, default=str),
            ),
        )

    def append_event(
        self,
        run_id: str,
        level: str,
        event_type: str,
        message: str,
        payload: dict[str, Any] | None = None,
    ) -> EventRecord:
        event_id = f"evt_{uuid.uuid4().hex[:12]}"
        timestamp = now_iso()
        payload = sanitize_for_logging(payload or {})
        payload_json = json.dumps(payload, sort_keys=True, default=str)
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO events (id, run_id, created_at, level, event_type, message, payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (event_id, run_id, timestamp, level, event_type, message, payload_json),
            )
        record = EventRecord(
            id=event_id,
            run_id=run_id,
            created_at=parse_dt(timestamp),
            level=level,
            event_type=event_type,
            message=message,
            payload=payload,
        )
        append_jsonl(self.runs_dir / run_id / "events.jsonl", record.model_dump(mode="json"))
        return record

    def list_events(self, run_id: str) -> list[EventRecord]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM events WHERE run_id = ? ORDER BY created_at ASC", (run_id,)
            ).fetchall()
        return [
            EventRecord(
                id=row["id"],
                run_id=row["run_id"],
                created_at=parse_dt(row["created_at"]),
                level=row["level"],
                event_type=row["event_type"],
                message=row["message"],
                payload=json.loads(row["payload_json"]),
            )
            for row in rows
        ]

    def register_artifact(
        self,
        run_id: str,
        kind: str,
        path: Path,
        metadata: dict[str, Any] | None = None,
    ) -> ArtifactRecord:
        artifact_id = f"art_{uuid.uuid4().hex[:12]}"
        timestamp = now_iso()
        metadata = metadata or {}
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO artifacts (id, run_id, kind, path, created_at, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact_id,
                    run_id,
                    kind,
                    str(path),
                    timestamp,
                    json.dumps(metadata, sort_keys=True, default=str),
                ),
            )
        record = ArtifactRecord(
            id=artifact_id,
            run_id=run_id,
            kind=kind,
            path=path,
            created_at=parse_dt(timestamp),
            metadata=metadata,
        )
        self.write_run_manifest(run_id)
        return record

    def list_artifacts(self, run_id: str) -> list[ArtifactRecord]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM artifacts WHERE run_id = ? ORDER BY created_at ASC", (run_id,)
            ).fetchall()
        return [
            ArtifactRecord(
                id=row["id"],
                run_id=row["run_id"],
                kind=row["kind"],
                path=Path(row["path"]),
                created_at=parse_dt(row["created_at"]),
                metadata=json.loads(row["metadata_json"]),
            )
            for row in rows
        ]

    def persist_backend_snapshot(self, run_id: str, backend: BackendConfig) -> None:
        snapshot_id = f"backend_{uuid.uuid4().hex[:12]}"
        timestamp = now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO backend_snapshots (
                  id, run_id, backend_name, backend_kind, metadata_json,
                  capabilities_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_id,
                    run_id,
                    backend.name,
                    backend.kind.value,
                    backend.metadata.model_dump_json(),
                    backend.capabilities.model_dump_json(),
                    timestamp,
                ),
            )

    def generate_final_report(self, run_id: str) -> Path:
        run = self.get_run(run_id)
        artifacts = self.list_artifacts(run_id)
        events = self.list_events(run_id)
        report_path = self.runs_dir / run_id / "final_report.md"
        lines = [
            f"# Run {run.id}",
            "",
            f"- Status: {run.status}",
            f"- Goal: {run.goal or ''}",
            f"- Task type: {run.task_type or ''}",
            f"- Project root: {run.project_root}",
            f"- Created: {run.created_at.isoformat()}",
            f"- Updated: {run.updated_at.isoformat()}",
            f"- Backend: {run.backend_name or 'none'}",
            f"- Backend kind: {run.backend_kind.value if run.backend_kind else 'none'}",
            f"- Billing mode: {run.billing_mode.value if run.billing_mode else 'none'}",
            f"- Execution location: {run.execution_location.value if run.execution_location else 'none'}",
            f"- Data boundary: {run.data_boundary.value if run.data_boundary else 'none'}",
            f"- Allow network: {run.allow_network if run.allow_network is not None else 'none'}",
            "",
            "## Artifacts",
            "",
        ]
        if artifacts:
            lines.extend([f"- {artifact.kind}: {artifact.path}" for artifact in artifacts])
        else:
            lines.append("- none")
        lines.extend(["", "## Events", "", f"- Event count: {len(events)}", ""])
        report_path.write_text("\n".join(lines), encoding="utf-8")
        self.write_run_manifest(run_id)
        return report_path

    def write_run_manifest(self, run_id: str) -> Path:
        manifest = self.build_run_manifest(run_id)
        path = self.runs_dir / run_id / "manifest.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(manifest.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return path

    def build_run_manifest(self, run_id: str) -> RunManifest:
        run = self.get_run(run_id)
        artifacts = [
            ManifestArtifact(
                kind=artifact.kind,
                path=artifact.path,
                created_at=artifact.created_at,
                metadata=artifact.metadata,
            )
            for artifact in self.list_artifacts(run_id)
        ]
        return RunManifest(
            run_id=run.id,
            goal=run.goal,
            task_type=run.task_type,
            run_mode=run_mode_for_task_type(run.task_type),
            status=run.status,
            project_root=run.project_root,
            created_at=run.created_at,
            updated_at=run.updated_at,
            approval_id=run.approval_id,
            backend_descriptor=self._latest_backend_descriptor(run_id),
            artifacts=artifacts,
        )

    def _latest_backend_descriptor(self, run_id: str) -> BackendDescriptor | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT backend_name, backend_kind, metadata_json, capabilities_json
                FROM backend_snapshots
                WHERE run_id = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        return BackendDescriptor(
            name=row["backend_name"],
            kind=BackendKind(row["backend_kind"]),
            metadata=BackendMetadata.model_validate_json(row["metadata_json"]),
            capabilities=BackendCapabilities.model_validate_json(row["capabilities_json"]),
        )

    def _row_to_run(self, row: sqlite3.Row) -> RunRecord:
        return RunRecord(
            id=row["id"],
            goal=row["goal"],
            task_type=row["task_type"],
            status=row["status"],
            project_root=Path(row["project_root"]),
            created_at=parse_dt(row["created_at"]),
            updated_at=parse_dt(row["updated_at"]),
            backend_name=row["backend_name"],
            backend_kind=row["backend_kind"],
            billing_mode=row["billing_mode"],
            execution_location=row["execution_location"],
            data_boundary=row["data_boundary"],
            allow_network=bool(row["allow_network"]) if row["allow_network"] is not None else None,
            approval_id=row["approval_id"] if "approval_id" in row.keys() else None,
        )

    def _row_to_task(self, row: sqlite3.Row) -> TaskRecord:
        depends_on = set(json.loads(row["depends_on_json"]))
        with self.connect() as conn:
            dependency_rows = conn.execute(
                """
                SELECT upstream_task_id
                FROM task_dependencies
                WHERE downstream_task_id = ?
                ORDER BY created_at ASC, id ASC
                """,
                (row["id"],),
            ).fetchall()
        depends_on.update(dependency["upstream_task_id"] for dependency in dependency_rows)
        return TaskRecord(
            id=row["id"],
            title=row["title"],
            description=row["description"],
            status=normalize_task_status(row["status"]),
            project_root=Path(row["project_root"]),
            created_at=parse_dt(row["created_at"]),
            updated_at=parse_dt(row["updated_at"]),
            priority=row["priority"],
            objective_id=row["objective_id"] if "objective_id" in row.keys() else None,
            workbench_id=row["workbench_id"],
            agent_id=row["agent_id"],
            spec_source_kind=row["spec_source_kind"],
            spec_source_path=Path(row["spec_source_path"]) if row["spec_source_path"] else None,
            depends_on=sorted(depends_on),
            idempotency_key=row["idempotency_key"] if "idempotency_key" in row.keys() else None,
            required_approvals=json.loads(row["required_approvals_json"])
            if "required_approvals_json" in row.keys() and row["required_approvals_json"]
            else [],
            approval_state=row["approval_state"] if "approval_state" in row.keys() else None,
            run_id=row["run_id"],
            metadata=json.loads(row["metadata_json"]),
        )

    def _row_to_task_dependency(self, row: sqlite3.Row) -> TaskDependency:
        return TaskDependency(
            id=row["id"],
            upstream_task_id=row["upstream_task_id"],
            downstream_task_id=row["downstream_task_id"],
            dependency_type=TaskDependencyType(row["dependency_type"]),
            required_artifact_kind=row["required_artifact_kind"],
            created_at=parse_dt(row["created_at"]),
        )

    def _row_to_objective(self, row: sqlite3.Row) -> ObjectiveRecord:
        return ObjectiveRecord(
            id=row["id"],
            title=row["title"],
            description=row["description"],
            status=ObjectiveStatus(row["status"]),
            project_root=Path(row["project_root"]),
            created_at=parse_dt(row["created_at"]),
            updated_at=parse_dt(row["updated_at"]),
            priority=row["priority"],
            workbench_id=row["workbench_id"],
            metadata=json.loads(row["metadata_json"]),
        )

    def _row_to_task_transition(self, row: sqlite3.Row) -> TaskTransitionRecord:
        return TaskTransitionRecord(
            id=row["id"],
            task_id=row["task_id"],
            from_status=normalize_task_status(row["from_status"]) if row["from_status"] else None,
            to_status=normalize_task_status(row["to_status"]),
            reason=row["reason"],
            actor=row["actor"],
            created_at=parse_dt(row["created_at"]),
            metadata=json.loads(row["metadata_json"]),
        )
