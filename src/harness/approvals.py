from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3

import yaml
from pydantic import BaseModel, Field, model_validator


class ApprovalProfile(BaseModel):
    id: str
    backend: str
    project_root: str
    data_boundary: str
    task_types: list[str]
    allowed_task_types: list[str] = Field(default_factory=list)
    allowed_adapters: list[str] = Field(default_factory=list)
    allowed_workbenches: list[str] = Field(default_factory=list)
    allowed_objective_ids: list[str] = Field(default_factory=list)
    expires_at: datetime
    created_at: datetime
    reason: str | None = None
    revoked: bool = False
    revoked_at: datetime | None = None
    max_runs: int | None = None
    max_total_runtime_seconds: int | None = None
    max_context_bytes: int | None = None
    autonomy_scope: str | None = None

    @model_validator(mode="after")
    def _sync_allowed_task_types(self) -> "ApprovalProfile":
        if not self.allowed_task_types:
            self.allowed_task_types = list(self.task_types)
        if not self.task_types:
            self.task_types = list(self.allowed_task_types)
        return self

    def is_valid_for(
        self,
        backend: str,
        project_root: Path,
        data_boundary: str,
        task_type: str,
        *,
        adapter_id: str | None = None,
        workbench_id: str | None = None,
        objective_id: str | None = None,
        autonomy_scope: str | None = None,
        context_bytes: int | None = None,
        run_count: int = 0,
        total_runtime_seconds: int = 0,
        strict_scope: bool = False,
    ) -> bool:
        now = datetime.now(timezone.utc)
        base_valid = (
            not self.revoked
            and self.revoked_at is None
            and self.backend == backend
            and Path(self.project_root).resolve() == project_root.resolve()
            and self.data_boundary == data_boundary
            and task_type in self.allowed_task_types
            and self.expires_at > now
        )
        if not base_valid:
            return False
        if strict_scope and self.autonomy_scope != autonomy_scope:
            return False
        if self.allowed_adapters and adapter_id not in self.allowed_adapters:
            return False
        if self.allowed_workbenches and workbench_id not in self.allowed_workbenches:
            return False
        if self.allowed_objective_ids and objective_id not in self.allowed_objective_ids:
            return False
        if self.max_runs is not None and run_count >= self.max_runs:
            return False
        if self.max_total_runtime_seconds is not None and total_runtime_seconds >= self.max_total_runtime_seconds:
            return False
        if self.max_context_bytes is not None and context_bytes is not None and context_bytes > self.max_context_bytes:
            return False
        return True


class ApprovalStore:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root.resolve()
        self.path = self.project_root / ".harness" / "approvals.yaml"

    def list(self) -> list[ApprovalProfile]:
        if not self.path.exists():
            return []
        data = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        return [ApprovalProfile.model_validate(item) for item in data.get("approvals", [])]

    def save_all(self, approvals: list[ApprovalProfile]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"approvals": [approval.model_dump(mode="json") for approval in approvals]}
        self.path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    def add(
        self,
        backend: str,
        data_boundary: str,
        task_types: list[str],
        duration_days: int = 0,
        duration_hours: int | None = None,
        reason: str | None = None,
        allowed_adapters: list[str] | None = None,
        allowed_workbenches: list[str] | None = None,
        allowed_objective_ids: list[str] | None = None,
        max_runs: int | None = None,
        max_total_runtime_seconds: int | None = None,
        max_context_bytes: int | None = None,
        autonomy_scope: str | None = None,
    ) -> ApprovalProfile:
        now = datetime.now(timezone.utc)
        duration = timedelta(hours=duration_hours) if duration_hours is not None else timedelta(days=duration_days)
        approval = ApprovalProfile(
            id=f"appr_{uuid.uuid4().hex[:12]}",
            backend=backend,
            project_root=str(self.project_root),
            data_boundary=data_boundary,
            task_types=task_types,
            allowed_task_types=task_types,
            allowed_adapters=allowed_adapters or [],
            allowed_workbenches=allowed_workbenches or [],
            allowed_objective_ids=allowed_objective_ids or [],
            expires_at=now + duration,
            created_at=now,
            reason=reason,
            max_runs=max_runs,
            max_total_runtime_seconds=max_total_runtime_seconds,
            max_context_bytes=max_context_bytes,
            autonomy_scope=autonomy_scope,
        )
        approvals = self.list()
        approvals.append(approval)
        self.save_all(approvals)
        return approval

    def revoke(self, approval_id: str) -> bool:
        approvals = self.list()
        found = False
        for approval in approvals:
            if approval.id == approval_id:
                approval.revoked = True
                approval.revoked_at = datetime.now(timezone.utc)
                found = True
        self.save_all(approvals)
        return found

    def find_valid(
        self,
        backend: str,
        data_boundary: str,
        task_type: str,
        *,
        adapter_id: str | None = None,
        workbench_id: str | None = None,
        objective_id: str | None = None,
        autonomy_scope: str | None = None,
        context_bytes: int | None = None,
        strict_scope: bool = False,
    ) -> ApprovalProfile | None:
        for approval in self.list():
            usage = self._approval_usage(approval.id)
            if approval.is_valid_for(
                backend,
                self.project_root,
                data_boundary,
                task_type,
                adapter_id=adapter_id,
                workbench_id=workbench_id,
                objective_id=objective_id,
                autonomy_scope=autonomy_scope,
                context_bytes=context_bytes,
                run_count=usage["run_count"],
                total_runtime_seconds=usage["total_runtime_seconds"],
                strict_scope=strict_scope,
            ):
                return approval
        return None

    def _approval_usage(self, approval_id: str) -> dict[str, int]:
        db_path = self.project_root / ".harness" / "harness.sqlite"
        if not db_path.exists():
            return {"run_count": 0, "total_runtime_seconds": 0}
        try:
            with sqlite3.connect(db_path) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT created_at, updated_at FROM runs WHERE approval_id = ?",
                    (approval_id,),
                ).fetchall()
        except sqlite3.Error:
            return {"run_count": 0, "total_runtime_seconds": 0}
        total_runtime = 0
        for row in rows:
            try:
                created = datetime.fromisoformat(str(row["created_at"]))
                updated = datetime.fromisoformat(str(row["updated_at"]))
            except ValueError:
                continue
            total_runtime += max(0, int((updated - created).total_seconds()))
        return {"run_count": len(rows), "total_runtime_seconds": total_runtime}
