from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml
from pydantic import BaseModel


class ApprovalProfile(BaseModel):
    id: str
    backend: str
    project_root: str
    data_boundary: str
    task_types: list[str]
    expires_at: datetime
    created_at: datetime
    reason: str | None = None
    revoked: bool = False

    def is_valid_for(self, backend: str, project_root: Path, data_boundary: str, task_type: str) -> bool:
        now = datetime.now(timezone.utc)
        return (
            not self.revoked
            and self.backend == backend
            and Path(self.project_root).resolve() == project_root.resolve()
            and self.data_boundary == data_boundary
            and task_type in self.task_types
            and self.expires_at > now
        )


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
        duration_days: int,
        reason: str | None = None,
    ) -> ApprovalProfile:
        now = datetime.now(timezone.utc)
        approval = ApprovalProfile(
            id=f"appr_{uuid.uuid4().hex[:12]}",
            backend=backend,
            project_root=str(self.project_root),
            data_boundary=data_boundary,
            task_types=task_types,
            expires_at=now + timedelta(days=duration_days),
            created_at=now,
            reason=reason,
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
                found = True
        self.save_all(approvals)
        return found

    def find_valid(
        self,
        backend: str,
        data_boundary: str,
        task_type: str,
    ) -> ApprovalProfile | None:
        for approval in self.list():
            if approval.is_valid_for(backend, self.project_root, data_boundary, task_type):
                return approval
        return None

