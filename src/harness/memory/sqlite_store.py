from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
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
    DaemonEvent,
    DaemonRecord,
    DaemonRecoveryResult,
    DaemonStatus,
    DaemonStatusResult,
    DaemonTickResult,
    EventRecord,
    ManifestArtifact,
    ObjectiveRecord,
    ObjectiveStatus,
    RunBaselineRecord,
    RunCompareResult,
    RunManifest,
    RunRecord,
    TaskAttempt,
    TaskDependency,
    TaskDependencyType,
    TaskLease,
    TaskLeaseStatus,
    TaskRecord,
    TaskStatus,
    TaskTransitionRecord,
    PolicyLevel,
    run_mode_for_task_type,
)
from harness.policy import (
    backend_descriptor_sha256,
    effective_policy_sha256,
    resolve_run_effective_policy,
    resolve_task_effective_policy,
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

DEFAULT_TASK_LEASE_MINUTES = 30
DEFAULT_TASK_LEASE_OWNER = "manual_cli"
DEFAULT_DAEMON_STALE_AFTER_SECONDS = 120
DAEMON_POLICY_FORBIDDEN_METADATA_KEYS = {
    "daemon_policy_forbidden",
    "requires_active_repo_write",
    "requires_external_network",
    "requires_docker",
    "requires_paid_provider",
    "requires_hosted_boundary",
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
    TaskStatus.LEASED: {
        TaskStatus.RUNNING,
        TaskStatus.READY,
        TaskStatus.BLOCKED,
        TaskStatus.WAITING_APPROVAL,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    },
    TaskStatus.RUNNING: {
        TaskStatus.READY,
        TaskStatus.BLOCKED,
        TaskStatus.SUCCEEDED,
        TaskStatus.FAILED,
        TaskStatus.WAITING_APPROVAL,
        TaskStatus.CANCELLED,
    },
    TaskStatus.FAILED: {
        TaskStatus.READY,
        TaskStatus.BLOCKED,
        TaskStatus.WAITING_APPROVAL,
        TaskStatus.CANCELLED,
    },
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
            self._ensure_column(conn, "artifacts", "schema_version", "TEXT")
            self._ensure_column(conn, "artifacts", "sha256", "TEXT")
            self._ensure_column(conn, "artifacts", "size_bytes", "INTEGER")
            self._ensure_column(conn, "artifacts", "producer", "TEXT")
            self._ensure_column(conn, "artifacts", "redaction_state", "TEXT")
            self._ensure_column(conn, "artifacts", "evidence_status", "TEXT")
            self._ensure_column(conn, "tasks", "objective_id", "TEXT")
            self._ensure_column(conn, "tasks", "idempotency_key", "TEXT")
            self._ensure_column(conn, "tasks", "required_approvals_json", "TEXT")
            self._ensure_column(conn, "tasks", "approval_state", "TEXT")
            self._migrate_task_rows(conn)
            self._migrate_artifact_rows(conn)

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

    def _migrate_artifact_rows(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            UPDATE artifacts
            SET schema_version = 'harness.artifact/v1'
            WHERE schema_version IS NULL OR schema_version = ''
            """
        )
        conn.execute(
            """
            UPDATE artifacts
            SET redaction_state = 'unknown'
            WHERE redaction_state IS NULL OR redaction_state = ''
            """
        )
        conn.execute(
            """
            UPDATE artifacts
            SET evidence_status = 'unknown'
            WHERE evidence_status IS NULL OR evidence_status = ''
            """
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

    def cancel_task(self, task_id: str) -> TaskRecord:
        task = self.get_task(task_id)
        if task.status == TaskStatus.CANCELLED:
            raise ValueError("Invalid task transition: cancelled -> cancelled")
        return self.update_task_status(task.id, TaskStatus.CANCELLED)

    def retry_task(self, task_id: str) -> TaskRecord:
        task = self.get_task(task_id)
        if task.status != TaskStatus.FAILED:
            raise ValueError(f"Task retry requires failed status: {task.status.value}")
        if task.required_approvals:
            return self.update_task_status(task.id, TaskStatus.WAITING_APPROVAL)
        if not self._task_dependencies_completed(task):
            return self.update_task_status(task.id, TaskStatus.BLOCKED)
        return self.update_task_status(task.id, TaskStatus.READY)

    def select_next_task(self) -> TaskRecord | None:
        selection = self.select_next_task_for_lease()
        return selection["task"] if selection is not None else None

    def select_next_task_for_lease(
        self,
        owner: str = DEFAULT_TASK_LEASE_OWNER,
        lease_duration_minutes: int = DEFAULT_TASK_LEASE_MINUTES,
    ) -> dict[str, TaskAttempt | TaskLease | TaskRecord] | None:
        timestamp = now_iso()
        expires_at = (parse_dt(timestamp) + timedelta(minutes=lease_duration_minutes)).isoformat()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """
                SELECT * FROM tasks
                WHERE status IN (?, ?)
                ORDER BY priority DESC, created_at ASC
                """,
                (TaskStatus.READY.value, TaskStatus.BLOCKED.value),
            ).fetchall()
            for row in rows:
                task = self._row_to_task(row)
                if self._task_has_active_lease(conn, task.id):
                    continue
                if task.required_approvals:
                    continue
                if not self._task_dependencies_completed(task):
                    continue
                return self._lease_task_in_conn(
                    conn,
                    task=task,
                    owner=owner,
                    timestamp=timestamp,
                    expires_at=expires_at,
                )
        return None

    def select_next_daemon_task_for_lease(
        self,
        owner: str,
        lease_duration_minutes: int = DEFAULT_TASK_LEASE_MINUTES,
    ) -> tuple[dict[str, TaskAttempt | TaskLease | TaskRecord] | None, list[dict[str, Any]]]:
        timestamp = now_iso()
        expires_at = (parse_dt(timestamp) + timedelta(minutes=lease_duration_minutes)).isoformat()
        pause_reasons: list[dict[str, Any]] = []
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """
                SELECT * FROM tasks
                WHERE status IN (?, ?, ?)
                ORDER BY priority DESC, created_at ASC
                """,
                (TaskStatus.READY.value, TaskStatus.BLOCKED.value, TaskStatus.WAITING_APPROVAL.value),
            ).fetchall()
            for row in rows:
                task = self._row_to_task(row)
                eligibility = self.daemon_task_eligibility(task, conn=conn)
                if eligibility["decision"] == "eligible":
                    return (
                        self._lease_task_in_conn(
                            conn,
                            task=task,
                            owner=owner,
                            timestamp=timestamp,
                            expires_at=expires_at,
                        ),
                        pause_reasons,
                    )
                if eligibility["decision"] in {
                    "blocked_dependency",
                    "waiting_approval",
                    "policy_forbidden",
                    "active_lease",
                }:
                    pause_reasons.append(eligibility)
        return None, pause_reasons

    def daemon_task_eligibility(
        self,
        task: TaskRecord,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        policy = resolve_task_effective_policy(task)
        policy_hash = effective_policy_sha256(policy)
        base = {
            "task_id": task.id,
            "status": task.status.value,
            "required_approvals": sorted(set(task.required_approvals)),
            "effective_policy_sha256": policy_hash,
        }
        if task.status in {
            TaskStatus.LEASED,
            TaskStatus.RUNNING,
            TaskStatus.SUCCEEDED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
            TaskStatus.SKIPPED,
        }:
            return {
                **base,
                "decision": "skipped_status",
                "reason": f"Task status is not daemon-selectable: {task.status.value}",
            }
        if conn is not None and self._task_has_active_lease(conn, task.id):
            return {
                **base,
                "decision": "active_lease",
                "reason": "Task already has an active lease.",
            }
        if task.required_approvals:
            return {
                **base,
                "decision": "waiting_approval",
                "reason": "Task has unresolved required approvals.",
            }
        forbidden_keys = self._daemon_policy_forbidden_keys(task, policy)
        if forbidden_keys:
            return {
                **base,
                "decision": "policy_forbidden",
                "reason": "Task metadata requests daemon-forbidden capability.",
                "forbidden_policy_keys": forbidden_keys,
            }
        if not self._task_dependencies_completed(task):
            return {
                **base,
                "decision": "blocked_dependency",
                "reason": "Task dependencies are not satisfied.",
                "blocked_dependency_ids": self._task_blocked_dependency_ids(task),
            }
        return {
            **base,
            "decision": "eligible",
            "reason": "Task is ready for daemon lease acquisition.",
        }

    def daemon_paused_tasks(self) -> list[dict[str, Any]]:
        rows: list[sqlite3.Row]
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM tasks
                WHERE status IN (?, ?, ?)
                ORDER BY priority DESC, created_at ASC
                """,
                (TaskStatus.READY.value, TaskStatus.BLOCKED.value, TaskStatus.WAITING_APPROVAL.value),
            ).fetchall()
            paused = [
                self.daemon_task_eligibility(self._row_to_task(row), conn=conn)
                for row in rows
            ]
        return [
            item
            for item in paused
            if item["decision"] in {
                "blocked_dependency",
                "waiting_approval",
                "policy_forbidden",
                "active_lease",
            }
        ]

    def _daemon_policy_forbidden_keys(
        self,
        task: TaskRecord,
        policy: Any,
    ) -> list[str]:
        requested = [
            key
            for key in sorted(DAEMON_POLICY_FORBIDDEN_METADATA_KEYS)
            if bool(task.metadata.get(key))
        ]
        metadata_to_policy_key = {
            "daemon_policy_forbidden": "task_queue_execution",
            "requires_active_repo_write": "active_repo_write",
            "requires_external_network": "external_network",
            "requires_docker": "docker_execution",
            "requires_paid_provider": "paid_provider",
            "requires_hosted_boundary": "hosted_boundary",
        }
        forbidden: list[str] = []
        for metadata_key in requested:
            policy_key = metadata_to_policy_key[metadata_key]
            if policy.levels.get(policy_key) in {PolicyLevel.FORBIDDEN, PolicyLevel.APPROVAL_REQUIRED}:
                forbidden.append(policy_key)
        return sorted(set(forbidden))

    def _task_blocked_dependency_ids(self, task: TaskRecord) -> list[str]:
        blocked: list[str] = []
        for dependency_id in task.depends_on:
            try:
                dependency = self.get_task(dependency_id)
            except KeyError:
                blocked.append(dependency_id)
                continue
            if dependency.status != TaskStatus.SUCCEEDED:
                blocked.append(dependency_id)
        return sorted(set(blocked))

    def _lease_task_in_conn(
        self,
        conn: sqlite3.Connection,
        *,
        task: TaskRecord,
        owner: str,
        timestamp: str,
        expires_at: str,
    ) -> dict[str, TaskAttempt | TaskLease | TaskRecord]:
        current_status = task.status
        if current_status == TaskStatus.BLOCKED:
            validate_task_transition(TaskStatus.BLOCKED, TaskStatus.READY)
            conn.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                (TaskStatus.READY.value, timestamp, task.id),
            )
            self._record_task_transition(
                conn,
                task_id=task.id,
                from_status=TaskStatus.BLOCKED,
                to_status=TaskStatus.READY,
                reason="dependencies_satisfied",
                actor="system",
                metadata={},
                created_at=timestamp,
            )
            current_status = TaskStatus.READY
        validate_task_transition(current_status, TaskStatus.LEASED)
        attempt_number = self._next_attempt_number(conn, task.id)
        attempt_id = f"task_attempt_{uuid.uuid4().hex[:12]}"
        lease_id = f"task_lease_{uuid.uuid4().hex[:12]}"
        conn.execute(
            """
            INSERT INTO task_attempts (
              id, task_id, attempt_number, status, lease_id, run_id,
              created_at, started_at, finished_at, failure_code, failure_message,
              metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                attempt_id,
                task.id,
                attempt_number,
                TaskStatus.LEASED.value,
                lease_id,
                None,
                timestamp,
                None,
                None,
                None,
                None,
                "{}",
            ),
        )
        conn.execute(
            """
            INSERT INTO task_leases (
              id, task_id, attempt_id, owner, status, acquired_at, expires_at,
              heartbeat_at, released_at, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                lease_id,
                task.id,
                attempt_id,
                owner,
                TaskLeaseStatus.ACTIVE.value,
                timestamp,
                expires_at,
                None,
                None,
                "{}",
            ),
        )
        conn.execute(
            "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
            (TaskStatus.LEASED.value, timestamp, task.id),
        )
        self._record_task_transition(
            conn,
            task_id=task.id,
            from_status=current_status,
            to_status=TaskStatus.LEASED,
            reason="task_leased",
            actor=owner,
            metadata={"attempt_id": attempt_id, "lease_id": lease_id},
            created_at=timestamp,
        )
        return {
            "task": self._row_to_task(
                conn.execute("SELECT * FROM tasks WHERE id = ?", (task.id,)).fetchone()
            ),
            "attempt": self._row_to_task_attempt(
                conn.execute("SELECT * FROM task_attempts WHERE id = ?", (attempt_id,)).fetchone()
            ),
            "lease": self._row_to_task_lease(
                conn.execute("SELECT * FROM task_leases WHERE id = ?", (lease_id,)).fetchone()
            ),
        }

    def daemon_run_once(self, owner: str, pid: int | None = None) -> DaemonTickResult:
        daemon = self.ensure_daemon(owner=owner, pid=pid)
        tick_id = f"daemon_tick_{uuid.uuid4().hex[:12]}"
        renewed_leases = self.renew_daemon_leases(owner=owner)
        if renewed_leases:
            self.record_daemon_event(
                daemon.id,
                event_type="tick",
                message="Daemon scheduler tick renewed active lease.",
                metadata={
                    "tick_id": tick_id,
                    "decision": "renewed_lease",
                    "lease_ids": [lease.id for lease in renewed_leases],
                },
            )
            return DaemonTickResult(
                daemon_id=daemon.id,
                owner=daemon.owner,
                project_root=self.project_root,
                tick_id=tick_id,
                decision="renewed_lease",
                selected_task=None,
                attempt=None,
                lease=renewed_leases[0],
                pause_reasons=[],
            )
        selection, pause_reasons = self.select_next_daemon_task_for_lease(owner=owner)
        decision = "leased_task" if selection is not None else "paused" if pause_reasons else "no_eligible_task"
        metadata = {
            "tick_id": tick_id,
            "decision": decision,
            "task_id": selection["task"].id if selection is not None else None,
            "attempt_id": selection["attempt"].id if selection is not None else None,
            "lease_id": selection["lease"].id if selection is not None else None,
            "pause_reasons": pause_reasons,
        }
        self.record_daemon_event(
            daemon.id,
            event_type="tick",
            message="Daemon scheduler tick completed.",
            metadata=metadata,
        )
        return DaemonTickResult(
            daemon_id=daemon.id,
            owner=daemon.owner,
            project_root=self.project_root,
            tick_id=tick_id,
            decision=decision,
            selected_task=selection["task"] if selection is not None else None,
            attempt=selection["attempt"] if selection is not None else None,
            lease=selection["lease"] if selection is not None else None,
            pause_reasons=pause_reasons,
        )

    def renew_daemon_leases(
        self,
        owner: str,
        lease_duration_minutes: int = DEFAULT_TASK_LEASE_MINUTES,
    ) -> list[TaskLease]:
        timestamp = now_iso()
        expires_at = (parse_dt(timestamp) + timedelta(minutes=lease_duration_minutes)).isoformat()
        renewed_ids: list[str] = []
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM task_leases
                WHERE owner = ? AND status = ? AND expires_at > ?
                ORDER BY acquired_at ASC, id ASC
                """,
                (owner, TaskLeaseStatus.ACTIVE.value, timestamp),
            ).fetchall()
            for row in rows:
                conn.execute(
                    "UPDATE task_leases SET heartbeat_at = ?, expires_at = ? WHERE id = ?",
                    (timestamp, expires_at, row["id"]),
                )
                renewed_ids.append(row["id"])
        return [lease for lease in self.list_task_leases() if lease.id in set(renewed_ids)]

    def recover_daemon_leases(self, owner: str, pid: int | None = None) -> DaemonRecoveryResult:
        daemon = self.ensure_daemon(owner=owner, pid=pid)
        timestamp = now_iso()
        expired_ids: list[str] = []
        recovered_task_ids: list[str] = []
        event_ids: list[str] = []
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM task_leases
                WHERE status = ? AND expires_at <= ?
                ORDER BY expires_at ASC, id ASC
                """,
                (TaskLeaseStatus.ACTIVE.value, timestamp),
            ).fetchall()
            for row in rows:
                lease = self._row_to_task_lease(row)
                task_row = conn.execute(
                    "SELECT * FROM tasks WHERE id = ?", (lease.task_id,)
                ).fetchone()
                if task_row is None:
                    continue
                task = self._row_to_task(task_row)
                if task.status not in {TaskStatus.LEASED, TaskStatus.RUNNING}:
                    continue
                next_status = self._task_requeue_status(task)
                validate_task_transition(task.status, next_status)
                conn.execute(
                    """
                    UPDATE task_leases
                    SET status = ?, released_at = ?, heartbeat_at = COALESCE(heartbeat_at, ?)
                    WHERE id = ?
                    """,
                    (TaskLeaseStatus.EXPIRED.value, timestamp, timestamp, lease.id),
                )
                if lease.attempt_id is not None:
                    conn.execute(
                        """
                        UPDATE task_attempts
                        SET status = ?, finished_at = ?, failure_code = ?, failure_message = ?
                        WHERE id = ? AND run_id IS NULL
                        """,
                        (
                            TaskStatus.FAILED.value,
                            timestamp,
                            "lease_expired",
                            "Daemon recovery expired an active lease before execution.",
                            lease.attempt_id,
                        ),
                    )
                conn.execute(
                    "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                    (next_status.value, timestamp, task.id),
                )
                self._record_task_transition(
                    conn,
                    task_id=task.id,
                    from_status=task.status,
                    to_status=next_status,
                    reason="lease_expired",
                    actor=owner,
                    metadata={"lease_id": lease.id, "attempt_id": lease.attempt_id},
                    created_at=timestamp,
                )
                event_ids.append(
                    self._record_daemon_event(
                        conn,
                        daemon_id=daemon.id,
                        event_type="recover_lease",
                        message="Expired active lease and returned task to queue.",
                        metadata={
                            "lease_id": lease.id,
                            "task_id": task.id,
                            "attempt_id": lease.attempt_id,
                            "next_status": next_status.value,
                        },
                        created_at=timestamp,
                    )
                )
                expired_ids.append(lease.id)
                recovered_task_ids.append(task.id)
        return DaemonRecoveryResult(
            daemon_id=daemon.id,
            owner=daemon.owner,
            project_root=self.project_root,
            renewed_leases=[],
            expired_leases=[lease for lease in self.list_task_leases() if lease.id in set(expired_ids)],
            recovered_tasks=[self.get_task(task_id) for task_id in recovered_task_ids],
            events=[self.get_daemon_event(event_id) for event_id in event_ids],
        )

    def _task_requeue_status(self, task: TaskRecord) -> TaskStatus:
        if task.required_approvals:
            return TaskStatus.WAITING_APPROVAL
        if not self._task_dependencies_completed(task):
            return TaskStatus.BLOCKED
        return TaskStatus.READY

    def ensure_daemon(self, owner: str, pid: int | None = None) -> DaemonRecord:
        timestamp = now_iso()
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM daemon_records
                WHERE owner = ? AND status = ?
                ORDER BY heartbeat_at DESC
                LIMIT 1
                """,
                (owner, DaemonStatus.RUNNING.value),
            ).fetchone()
            if row is not None:
                conn.execute(
                    "UPDATE daemon_records SET heartbeat_at = ?, pid = COALESCE(?, pid) WHERE id = ?",
                    (timestamp, pid, row["id"]),
                )
                daemon_id = row["id"]
                self._record_daemon_event(
                    conn,
                    daemon_id=daemon_id,
                    event_type="heartbeat",
                    message="Daemon heartbeat recorded.",
                    metadata={},
                    created_at=timestamp,
                )
                updated = conn.execute(
                    "SELECT * FROM daemon_records WHERE id = ?", (daemon_id,)
                ).fetchone()
                return self._row_to_daemon(updated)
            daemon_id = f"daemon_{uuid.uuid4().hex[:12]}"
            conn.execute(
                """
                INSERT INTO daemon_records (
                  id, owner, status, pid, project_root, started_at, heartbeat_at,
                  stopped_at, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    daemon_id,
                    owner,
                    DaemonStatus.RUNNING.value,
                    pid,
                    str(self.project_root),
                    timestamp,
                    timestamp,
                    None,
                    "{}",
                ),
            )
            self._record_daemon_event(
                conn,
                daemon_id=daemon_id,
                event_type="start",
                message="Daemon record started.",
                metadata={},
                created_at=timestamp,
            )
        return self.get_daemon(daemon_id)

    def stop_daemons(self, owner: str | None = None) -> list[DaemonRecord]:
        timestamp = now_iso()
        stopped_ids: list[str] = []
        with self.connect() as conn:
            if owner is None:
                rows = conn.execute(
                    "SELECT * FROM daemon_records WHERE status = ? ORDER BY heartbeat_at DESC",
                    (DaemonStatus.RUNNING.value,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM daemon_records
                    WHERE status = ? AND owner = ?
                    ORDER BY heartbeat_at DESC
                    """,
                    (DaemonStatus.RUNNING.value, owner),
                ).fetchall()
            for row in rows:
                stopped_ids.append(row["id"])
                conn.execute(
                    "UPDATE daemon_records SET status = ?, stopped_at = ?, heartbeat_at = ? WHERE id = ?",
                    (DaemonStatus.STOPPED.value, timestamp, timestamp, row["id"]),
                )
                self._record_daemon_event(
                    conn,
                    daemon_id=row["id"],
                    event_type="stop",
                    message="Daemon record stopped.",
                    metadata={},
                    created_at=timestamp,
                )
        return [self.get_daemon(daemon_id) for daemon_id in stopped_ids]

    def daemon_status(
        self,
        *,
        stale_after_seconds: int = DEFAULT_DAEMON_STALE_AFTER_SECONDS,
    ) -> DaemonStatusResult:
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=stale_after_seconds)
        active_daemons = [
            daemon.model_copy(update={"status": DaemonStatus.STALE})
            if daemon.heartbeat_at < cutoff
            else daemon
            for daemon in self.list_daemons(include_stopped=False)
        ]
        return DaemonStatusResult(
            project_root=self.project_root,
            active_daemons=active_daemons,
            latest_events=self.list_daemon_events(limit=20),
            paused_tasks=self.daemon_paused_tasks(),
            stale_after_seconds=stale_after_seconds,
        )

    def get_daemon(self, daemon_id: str) -> DaemonRecord:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM daemon_records WHERE id = ?", (daemon_id,)).fetchone()
        if row is None:
            raise KeyError(f"Daemon not found: {daemon_id}")
        return self._row_to_daemon(row)

    def list_daemons(self, include_stopped: bool = False) -> list[DaemonRecord]:
        with self.connect() as conn:
            if include_stopped:
                rows = conn.execute(
                    "SELECT * FROM daemon_records ORDER BY heartbeat_at DESC, id ASC"
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM daemon_records
                    WHERE status = ?
                    ORDER BY heartbeat_at DESC, id ASC
                    """,
                    (DaemonStatus.RUNNING.value,),
                ).fetchall()
        return [self._row_to_daemon(row) for row in rows]

    def record_daemon_event(
        self,
        daemon_id: str,
        event_type: str,
        message: str,
        metadata: dict[str, Any] | None = None,
    ) -> DaemonEvent:
        self.get_daemon(daemon_id)
        timestamp = now_iso()
        with self.connect() as conn:
            event_id = self._record_daemon_event(
                conn,
                daemon_id=daemon_id,
                event_type=event_type,
                message=message,
                metadata=metadata or {},
                created_at=timestamp,
            )
        return self.get_daemon_event(event_id)

    def get_daemon_event(self, event_id: str) -> DaemonEvent:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM daemon_events WHERE id = ?", (event_id,)).fetchone()
        if row is None:
            raise KeyError(f"Daemon event not found: {event_id}")
        return self._row_to_daemon_event(row)

    def list_daemon_events(self, daemon_id: str | None = None, limit: int = 50) -> list[DaemonEvent]:
        with self.connect() as conn:
            if daemon_id is None:
                rows = conn.execute(
                    """
                    SELECT * FROM daemon_events
                    ORDER BY created_at DESC, id ASC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            else:
                self.get_daemon(daemon_id)
                rows = conn.execute(
                    """
                    SELECT * FROM daemon_events
                    WHERE daemon_id = ?
                    ORDER BY created_at DESC, id ASC
                    LIMIT ?
                    """,
                    (daemon_id, limit),
                ).fetchall()
        return [self._row_to_daemon_event(row) for row in rows]

    def _record_daemon_event(
        self,
        conn: sqlite3.Connection,
        *,
        daemon_id: str,
        event_type: str,
        message: str,
        metadata: dict[str, Any],
        created_at: str,
    ) -> str:
        event_id = f"daemon_evt_{uuid.uuid4().hex[:12]}"
        conn.execute(
            """
            INSERT INTO daemon_events (
              id, daemon_id, event_type, message, created_at, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                daemon_id,
                event_type,
                message,
                created_at,
                json.dumps(sanitize_for_logging(metadata), sort_keys=True, default=str),
            ),
        )
        return event_id

    def select_next_task_legacy(self) -> TaskRecord | None:
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

    def list_task_attempts(self, task_id: str | None = None) -> list[TaskAttempt]:
        with self.connect() as conn:
            if task_id is None:
                rows = conn.execute(
                    "SELECT * FROM task_attempts ORDER BY created_at ASC, id ASC"
                ).fetchall()
            else:
                self._require_task(conn, task_id)
                rows = conn.execute(
                    """
                    SELECT * FROM task_attempts
                    WHERE task_id = ?
                    ORDER BY attempt_number ASC, created_at ASC, id ASC
                    """,
                    (task_id,),
                ).fetchall()
        return [self._row_to_task_attempt(row) for row in rows]

    def list_task_leases(self, task_id: str | None = None) -> list[TaskLease]:
        with self.connect() as conn:
            if task_id is None:
                rows = conn.execute(
                    "SELECT * FROM task_leases ORDER BY acquired_at ASC, id ASC"
                ).fetchall()
            else:
                self._require_task(conn, task_id)
                rows = conn.execute(
                    """
                    SELECT * FROM task_leases
                    WHERE task_id = ?
                    ORDER BY acquired_at ASC, id ASC
                    """,
                    (task_id,),
                ).fetchall()
        return [self._row_to_task_lease(row) for row in rows]

    def _task_has_active_lease(self, conn: sqlite3.Connection, task_id: str) -> bool:
        row = conn.execute(
            "SELECT id FROM task_leases WHERE task_id = ? AND status = ? LIMIT 1",
            (task_id, TaskLeaseStatus.ACTIVE.value),
        ).fetchone()
        return row is not None

    def _next_attempt_number(self, conn: sqlite3.Connection, task_id: str) -> int:
        row = conn.execute(
            "SELECT COALESCE(MAX(attempt_number), 0) AS max_attempt FROM task_attempts WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        return int(row["max_attempt"]) + 1

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
        producer: str | None = None,
        redaction_state: str = "unknown",
    ) -> ArtifactRecord:
        self.get_run(run_id)
        if not path.exists():
            raise FileNotFoundError(f"Artifact path not found: {path}")
        artifact_id = f"art_{uuid.uuid4().hex[:12]}"
        timestamp = now_iso()
        metadata = metadata or {}
        sha256, size_bytes = self._artifact_file_evidence(path)
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO artifacts (
                  id, run_id, kind, path, created_at, schema_version, sha256,
                  size_bytes, producer, redaction_state, evidence_status, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact_id,
                    run_id,
                    kind,
                    str(path),
                    timestamp,
                    "harness.artifact/v1",
                    sha256,
                    size_bytes,
                    producer,
                    redaction_state,
                    "verified",
                    json.dumps(metadata, sort_keys=True, default=str),
                ),
            )
        record = ArtifactRecord(
            id=artifact_id,
            run_id=run_id,
            kind=kind,
            path=path,
            created_at=parse_dt(timestamp),
            sha256=sha256,
            size_bytes=size_bytes,
            producer=producer,
            redaction_state=redaction_state,
            evidence_status="verified",
            metadata=metadata,
        )
        self.write_run_manifest(run_id)
        return record

    def get_artifact(self, artifact_id: str) -> ArtifactRecord:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM artifacts WHERE id = ?", (artifact_id,)).fetchone()
        if row is None:
            raise KeyError(f"Artifact not found: {artifact_id}")
        return self._row_to_artifact(row)

    def list_artifacts(self, run_id: str) -> list[ArtifactRecord]:
        self.get_run(run_id)
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM artifacts WHERE run_id = ? ORDER BY created_at ASC", (run_id,)
            ).fetchall()
        return [self._row_to_artifact(row) for row in rows]

    def verify_artifact(self, artifact_id: str) -> ArtifactRecord:
        artifact = self.get_artifact(artifact_id)
        status = self._artifact_evidence_status(artifact)
        return artifact.model_copy(update={"evidence_status": status})

    def verify_artifacts(self, run_id: str) -> list[ArtifactRecord]:
        return [self.verify_artifact(artifact.id) for artifact in self.list_artifacts(run_id)]

    def _artifact_file_evidence(self, path: Path) -> tuple[str, int]:
        digest = hashlib.sha256()
        size_bytes = 0
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                size_bytes += len(chunk)
                digest.update(chunk)
        return digest.hexdigest(), size_bytes

    def _artifact_evidence_status(self, artifact: ArtifactRecord) -> str:
        if not artifact.path.exists():
            return "missing"
        if artifact.sha256 is None or artifact.size_bytes is None:
            return "unknown"
        sha256, size_bytes = self._artifact_file_evidence(artifact.path)
        if sha256 == artifact.sha256 and size_bytes == artifact.size_bytes:
            return "verified"
        return "mismatch"

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
        backend_descriptor = self._latest_backend_descriptor(run_id)
        effective_policy = resolve_run_effective_policy(run, backend_descriptor)
        artifacts = [
            ManifestArtifact(
                id=artifact.id,
                run_id=artifact.run_id,
                kind=artifact.kind,
                path=artifact.path,
                created_at=artifact.created_at,
                sha256=artifact.sha256,
                size_bytes=artifact.size_bytes,
                producer=artifact.producer,
                redaction_state=artifact.redaction_state,
                evidence_status=self._artifact_evidence_status(artifact),
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
            backend_descriptor=backend_descriptor,
            artifacts=artifacts,
            effective_policy=effective_policy,
            effective_policy_sha256=effective_policy_sha256(effective_policy),
            backend_descriptor_sha256=backend_descriptor_sha256(backend_descriptor),
        )

    def build_run_evidence_snapshot(self, run_id: str) -> dict[str, Any]:
        manifest = self.build_run_manifest(run_id).model_dump(mode="json")
        return sanitize_for_logging(
            {
                "run_id": manifest["run_id"],
                "run_status": {"status": manifest["status"]},
                "effective_policy_sha256": manifest.get("effective_policy_sha256"),
                "backend_descriptor_sha256": manifest.get("backend_descriptor_sha256"),
                "sandbox_profile": manifest.get("sandbox_profile"),
                "approvals": {
                    "approval_id": manifest.get("approval_id"),
                    "required_approvals": (
                        manifest.get("effective_policy", {}).get("required_approvals", [])
                        if manifest.get("effective_policy")
                        else []
                    ),
                },
                "task_objective_linkage": {
                    "task_id": manifest.get("task_id"),
                    "objective_id": manifest.get("objective_id"),
                    "trace_id": manifest.get("trace_id"),
                },
                "artifacts": [
                    {
                        "id": artifact.get("id"),
                        "kind": artifact.get("kind"),
                        "sha256": artifact.get("sha256"),
                        "size_bytes": artifact.get("size_bytes"),
                        "producer": artifact.get("producer"),
                        "redaction_state": artifact.get("redaction_state"),
                        "evidence_status": artifact.get("evidence_status"),
                        "metadata": artifact.get("metadata", {}),
                    }
                    for artifact in sorted(
                        manifest.get("artifacts", []),
                        key=lambda item: (item.get("kind") or "", item.get("id") or ""),
                    )
                ],
                "test_result_evidence": {
                    "validation_results": manifest.get("validation_results"),
                    "test_artifacts": [
                        {
                            "id": artifact.get("id"),
                            "kind": artifact.get("kind"),
                            "sha256": artifact.get("sha256"),
                            "size_bytes": artifact.get("size_bytes"),
                            "evidence_status": artifact.get("evidence_status"),
                        }
                        for artifact in sorted(
                            manifest.get("artifacts", []),
                            key=lambda item: (item.get("kind") or "", item.get("id") or ""),
                        )
                        if "test" in (artifact.get("kind") or "")
                        or "pytest" in (artifact.get("kind") or "")
                    ],
                },
            }
        )

    def set_run_baseline(self, name: str, run_id: str) -> RunBaselineRecord:
        if not name.strip():
            raise ValueError("Baseline name is required")
        snapshot = self.build_run_evidence_snapshot(run_id)
        evidence_sha256 = self._stable_json_sha256(snapshot)
        timestamp = now_iso()
        snapshot_json = json.dumps(snapshot, sort_keys=True, default=str)
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO run_baselines (name, run_id, created_at, evidence_sha256, snapshot_json)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                  run_id = excluded.run_id,
                  created_at = excluded.created_at,
                  evidence_sha256 = excluded.evidence_sha256,
                  snapshot_json = excluded.snapshot_json
                """,
                (name, run_id, timestamp, evidence_sha256, snapshot_json),
            )
        return self.get_run_baseline(name)

    def get_run_baseline(self, name: str) -> RunBaselineRecord:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM run_baselines WHERE name = ?", (name,)).fetchone()
        if row is None:
            raise KeyError(f"Baseline not found: {name}")
        return self._row_to_run_baseline(row)

    def compare_runs(self, run_a: str, run_b: str) -> RunCompareResult:
        return self._compare_snapshots(
            run_a=run_a,
            run_b=run_b,
            snapshot_a=self.build_run_evidence_snapshot(run_a),
            snapshot_b=self.build_run_evidence_snapshot(run_b),
        )

    def compare_run_to_baseline(self, run_id: str, baseline_name: str) -> dict[str, Any]:
        baseline = self.get_run_baseline(baseline_name)
        comparison = self._compare_snapshots(
            run_a=baseline.run_id,
            run_b=run_id,
            snapshot_a=baseline.snapshot,
            snapshot_b=self.build_run_evidence_snapshot(run_id),
        )
        return {
            "schema_version": "harness.baseline_compare/v1",
            "ok": True,
            "baseline": baseline.model_dump(mode="json"),
            "comparison": comparison.model_dump(mode="json"),
        }

    def _compare_snapshots(
        self,
        *,
        run_a: str,
        run_b: str,
        snapshot_a: dict[str, Any],
        snapshot_b: dict[str, Any],
    ) -> RunCompareResult:
        section_names = [
            "run_status",
            "effective_policy_sha256",
            "backend_descriptor_sha256",
            "sandbox_profile",
            "approvals",
            "task_objective_linkage",
            "artifacts",
            "test_result_evidence",
        ]
        sections = {
            section: {
                "matches": snapshot_a.get(section) == snapshot_b.get(section),
                "run_a": snapshot_a.get(section),
                "run_b": snapshot_b.get(section),
            }
            for section in section_names
        }
        changed_sections = [section for section, value in sections.items() if not value["matches"]]
        return RunCompareResult(
            run_a=run_a,
            run_b=run_b,
            matches=not changed_sections,
            changed_sections=changed_sections,
            sections=sections,
        )

    def _stable_json_sha256(self, value: Any) -> str:
        payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

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

    def _row_to_artifact(self, row: sqlite3.Row) -> ArtifactRecord:
        return ArtifactRecord(
            schema_version=row["schema_version"] or "harness.artifact/v1",
            id=row["id"],
            run_id=row["run_id"],
            kind=row["kind"],
            path=Path(row["path"]),
            created_at=parse_dt(row["created_at"]),
            sha256=row["sha256"],
            size_bytes=row["size_bytes"],
            producer=row["producer"],
            redaction_state=row["redaction_state"] or "unknown",
            evidence_status=row["evidence_status"] or "unknown",
            metadata=json.loads(row["metadata_json"]),
        )

    def _row_to_run_baseline(self, row: sqlite3.Row) -> RunBaselineRecord:
        return RunBaselineRecord(
            name=row["name"],
            run_id=row["run_id"],
            created_at=parse_dt(row["created_at"]),
            evidence_sha256=row["evidence_sha256"],
            snapshot=json.loads(row["snapshot_json"]),
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

    def _row_to_task_attempt(self, row: sqlite3.Row) -> TaskAttempt:
        return TaskAttempt(
            id=row["id"],
            task_id=row["task_id"],
            attempt_number=row["attempt_number"],
            status=normalize_task_status(row["status"]),
            lease_id=row["lease_id"],
            run_id=row["run_id"],
            created_at=parse_dt(row["created_at"]),
            started_at=parse_dt(row["started_at"]) if row["started_at"] else None,
            finished_at=parse_dt(row["finished_at"]) if row["finished_at"] else None,
            failure_code=row["failure_code"],
            failure_message=row["failure_message"],
            metadata=json.loads(row["metadata_json"]),
        )

    def _row_to_task_lease(self, row: sqlite3.Row) -> TaskLease:
        return TaskLease(
            id=row["id"],
            task_id=row["task_id"],
            attempt_id=row["attempt_id"],
            owner=row["owner"],
            status=TaskLeaseStatus(row["status"]),
            acquired_at=parse_dt(row["acquired_at"]),
            expires_at=parse_dt(row["expires_at"]),
            heartbeat_at=parse_dt(row["heartbeat_at"]) if row["heartbeat_at"] else None,
            released_at=parse_dt(row["released_at"]) if row["released_at"] else None,
            metadata=json.loads(row["metadata_json"]),
        )

    def _row_to_daemon(self, row: sqlite3.Row) -> DaemonRecord:
        return DaemonRecord(
            id=row["id"],
            owner=row["owner"],
            status=DaemonStatus(row["status"]),
            pid=row["pid"],
            project_root=Path(row["project_root"]),
            started_at=parse_dt(row["started_at"]),
            heartbeat_at=parse_dt(row["heartbeat_at"]),
            stopped_at=parse_dt(row["stopped_at"]) if row["stopped_at"] else None,
            metadata=json.loads(row["metadata_json"]),
        )

    def _row_to_daemon_event(self, row: sqlite3.Row) -> DaemonEvent:
        return DaemonEvent(
            id=row["id"],
            daemon_id=row["daemon_id"],
            event_type=row["event_type"],
            message=row["message"],
            created_at=parse_dt(row["created_at"]),
            metadata=json.loads(row["metadata_json"]),
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
