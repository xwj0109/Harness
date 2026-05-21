from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path


GOVERNANCE_DIR = ".harness/governance"


def governance_root(project_root: Path) -> Path:
    return Path(project_root).resolve() / GOVERNANCE_DIR


def governance_evidence_dir(project_root: Path, category: str, run_id: str) -> Path:
    return governance_root(project_root) / category / run_id


def governance_run_id(category: str, subject: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", subject.strip()).strip("-._").lower() or "unknown"
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{category}-{slug[:80]}"
