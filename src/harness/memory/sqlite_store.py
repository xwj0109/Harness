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
    RunManifest,
    RunRecord,
    run_mode_for_task_type,
)
from harness.security import sanitize_for_logging


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


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

    def _ensure_column(self, conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

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
