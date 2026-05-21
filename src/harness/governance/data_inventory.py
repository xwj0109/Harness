from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from harness.governance.protected_paths import protected_apply_path_match
from harness.governance.tasks import list_governance_tasks
from harness.security import is_secret_path, scan_text_for_secrets


SCHEMA_VERSION = "harness.data_inventory/v1"
CLEANUP_PROPOSAL_SCHEMA_VERSION = "harness.data_cleanup_proposal/v1"
DEFAULT_POLICY_SOURCE = "builtin:harness-governance-data-policy/v1"
TEXT_SCAN_SUFFIXES = {".json", ".jsonl", ".log", ".md", ".txt", ".yaml", ".yml", ".patch"}

RETENTION_CLASSES: dict[str, dict[str, Any]] = {
    "canonical_decision": {"retention_days": None, "action_after_retention": "keep"},
    "compact_receipt": {"retention_days": 365, "action_after_retention": "propose_archive_or_delete"},
    "raw_execution_log": {
        "retention_days": 14,
        "failed_run_retention_days": 30,
        "action_after_retention": "propose_compress_or_delete",
    },
    "replay_debug_bundle": {"retention_days": 30, "action_after_retention": "propose_archive_or_delete"},
    "temp_isolation_manifest": {"retention_days": 7, "action_after_retention": "propose_delete"},
    "generated_preview_artifact": {
        "retention_days": 30,
        "promoted_retention_days": None,
        "action_after_retention": "propose_archive_or_delete",
    },
    "unknown_generated_data": {"retention_days": None, "action_after_retention": "review"},
}


@dataclass(frozen=True)
class DataInventoryItem:
    path: str
    data_class: str
    owner: str
    size_bytes: int
    modified_at: str
    age_days: int
    retention_days: int | None
    expired: bool
    cleanup_candidate: bool
    safety: str
    reason: str
    action_after_retention: str
    blockers: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["blockers"] = list(self.blockers)
        return payload


@dataclass(frozen=True)
class DataInventoryReport:
    generated_at: str
    policy_source: str
    items: tuple[DataInventoryItem, ...]
    summary: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "generated_at": self.generated_at,
            "policy_source": self.policy_source,
            "retention_classes": RETENTION_CLASSES,
            "summary": self.summary,
            "cleanup_proposal": build_cleanup_proposal(self.items),
            "items": [item.to_dict() for item in self.items],
        }


def build_data_inventory(project_root: Path, *, now: datetime | None = None) -> DataInventoryReport:
    root = Path(project_root).resolve()
    generated_at = now or datetime.now(timezone.utc)
    artifact_index = _registered_artifact_index(root)
    run_statuses = _run_statuses(root)
    active_worktree_roots = _active_governance_worktree_roots(root)
    items: list[DataInventoryItem] = []
    for rel_path in _iter_inventory_paths(root):
        path = root / rel_path
        if not path.is_file():
            continue
        classification = classify_generated_path(
            root,
            rel_path,
            artifact_index=artifact_index,
            run_statuses=run_statuses,
            active_worktree_roots=active_worktree_roots,
        )
        stat = path.stat()
        modified_at = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
        age_days = _age_days(modified_at, generated_at)
        retention_days = classification["retention_days"]
        expired = retention_days is not None and age_days >= retention_days
        blockers = _safety_blockers(root, rel_path)
        safety = "blocked" if blockers else "eligible"
        cleanup_candidate = bool(
            expired
            and not blockers
            and safety == "eligible"
            and classification["action_after_retention"] != "keep"
        )
        items.append(
            DataInventoryItem(
                path=rel_path.as_posix(),
                data_class=classification["data_class"],
                owner=classification["owner"],
                size_bytes=stat.st_size,
                modified_at=modified_at.isoformat(timespec="seconds").replace("+00:00", "Z"),
                age_days=age_days,
                retention_days=retention_days,
                expired=expired,
                cleanup_candidate=cleanup_candidate,
                safety=safety,
                reason=classification["reason"],
                action_after_retention=classification["action_after_retention"],
                blockers=tuple(blockers),
            )
        )
    items_tuple = tuple(sorted(items, key=lambda item: item.path))
    return DataInventoryReport(
        generated_at=generated_at.isoformat(timespec="seconds").replace("+00:00", "Z"),
        policy_source=DEFAULT_POLICY_SOURCE,
        items=items_tuple,
        summary=_summarize(items_tuple),
    )


def classify_generated_path(
    project_root: Path,
    rel_path: str | Path,
    *,
    artifact_index: dict[str, dict[str, Any]] | None = None,
    run_statuses: dict[str, str] | None = None,
    active_worktree_roots: set[str] | None = None,
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    rel_path_obj = _normalize_rel(rel_path)
    rel = rel_path_obj.as_posix()
    artifact = (artifact_index or _registered_artifact_index(root)).get(rel)
    run_status_map = run_statuses or _run_statuses(root)
    active_worktrees = active_worktree_roots if active_worktree_roots is not None else _active_governance_worktree_roots(root)

    data_class = "unknown_generated_data"
    owner = "harness_generated"
    reason = "generated Harness path has no specific retention rule"
    retention_override: int | None = None
    has_retention_override = False

    if rel == ".harness/harness.sqlite":
        data_class = "compact_receipt"
        owner = "harness_state"
        reason = "Harness control-plane SQLite state"
    elif rel in {".harness/approvals.yaml", ".harness/.gitignore", ".harness/.DS_Store", ".harness/tmp/.DS_Store"}:
        data_class = "compact_receipt"
        owner = "harness_state"
        reason = "Harness local control-plane metadata"
    elif rel.startswith(".harness/sessions/"):
        data_class = "raw_execution_log" if rel.endswith(".jsonl") else "compact_receipt"
        owner = "harness_sessions"
        reason = "Harness session transcript or receipt"
    elif rel.startswith(".harness/runs/"):
        owner = "harness_runs"
        run_id = _run_id_from_rel(rel)
        if rel.endswith((".log", ".jsonl", ".txt")) and "manifest.json" not in rel:
            data_class = "raw_execution_log"
            reason = "run stdout/stderr, event stream, or provider output"
            if run_id and run_status_map.get(run_id) in {"failed", "error", "cancelled"}:
                retention_override = int(RETENTION_CLASSES[data_class]["failed_run_retention_days"])
                has_retention_override = True
                reason = "failed run log receives extended failed-run retention"
        elif artifact is not None:
            data_class = "generated_preview_artifact"
            reason = f"registered run artifact ({artifact.get('kind') or 'unknown'})"
            if _artifact_is_promoted(artifact):
                retention_override = RETENTION_CLASSES[data_class]["promoted_retention_days"]
                has_retention_override = True
                reason = "promoted generated artifact"
        else:
            data_class = "compact_receipt"
            reason = "run manifest, receipt, or artifact metadata"
    elif rel.startswith(".harness/governance/worktrees/"):
        data_class = "temp_isolation_manifest"
        owner = "harness_governance"
        reason = "governed task worktree or isolation payload"
        if any(rel == root_rel or rel.startswith(f"{root_rel}/") for root_rel in active_worktrees):
            retention_override = None
            has_retention_override = True
            reason = "active governed task worktree remains pinned"
    elif rel.startswith(".harness/governance/context-packs/"):
        data_class = "compact_receipt"
        owner = "harness_governance"
        reason = "governance context pack receipt"
    elif rel.startswith(".harness/governance/merge-check/"):
        owner = "harness_governance"
        if rel.endswith((".log", ".patch", ".txt")):
            data_class = "raw_execution_log"
            reason = "merge-check raw evidence log or diff"
        else:
            data_class = "compact_receipt"
            reason = "merge-check verdict or structured evidence"
    elif rel.startswith(".harness/governance/tests/"):
        owner = "harness_governance"
        if rel.endswith(".log"):
            data_class = "raw_execution_log"
            reason = "governance test stdout/stderr log"
        else:
            data_class = "compact_receipt"
            reason = "governance test plan or run receipt"
    elif rel.startswith(".harness/governance/"):
        data_class = "compact_receipt"
        owner = "harness_governance"
        reason = "governance evidence receipt"
    elif rel.startswith(".harness/tmp/"):
        data_class = "temp_isolation_manifest"
        owner = "harness_tmp"
        reason = "temporary isolation or runtime payload"
    elif rel.startswith(".harness/autonomy/"):
        data_class = "compact_receipt" if not rel.endswith((".log", ".jsonl")) else "raw_execution_log"
        owner = "harness_autonomy"
        reason = "autonomy loop evidence or checkpoint"
    elif rel.startswith(".harness/reference-code/") or rel.startswith(".harness/reference-repos/"):
        data_class = "unknown_generated_data"
        owner = "harness_reference_code"
        reason = "reference code snapshot requires manual retention review"

    if artifact is not None and data_class == "unknown_generated_data":
        data_class = "generated_preview_artifact"
        owner = "harness_artifacts"
        reason = f"registered generated artifact ({artifact.get('kind') or 'unknown'})"
        if _artifact_is_promoted(artifact):
            retention_override = RETENTION_CLASSES[data_class]["promoted_retention_days"]
            has_retention_override = True
            reason = "promoted generated artifact"

    class_policy = RETENTION_CLASSES[data_class]
    retention_days = retention_override if has_retention_override else class_policy.get("retention_days")
    if retention_days is not None:
        retention_days = int(retention_days)
    return {
        "data_class": data_class,
        "owner": owner,
        "retention_days": retention_days,
        "reason": reason,
        "action_after_retention": str(class_policy.get("action_after_retention", "review")),
    }


def build_cleanup_proposal(items: tuple[DataInventoryItem, ...]) -> dict[str, Any]:
    eligible = tuple(item for item in items if item.cleanup_candidate)
    blocked = tuple(item for item in items if item.blockers)
    retain = tuple(item for item in items if _retain_as_evidence(item))
    needs_policy = tuple(item for item in items if item.data_class == "unknown_generated_data" and not item.blockers)
    return {
        "schema_version": CLEANUP_PROPOSAL_SCHEMA_VERSION,
        "mode": "read_only_proposal",
        "mutation_allowed": False,
        "approval_required": True,
        "summary": {
            "eligible_cleanup_candidates": _proposal_bucket_summary(eligible),
            "blocked_items": _proposal_bucket_summary(blocked),
            "retain_as_evidence": _proposal_bucket_summary(retain),
            "needs_policy_classification": _proposal_bucket_summary(needs_policy),
        },
        "top_cleanup_directories": _top_directories(eligible),
        "top_blocked_directories": _top_directories(blocked),
        "top_needs_policy_classification": _top_directories(needs_policy),
        "eligible_cleanup_candidates": _sample_items(eligible),
        "blocked_items": _sample_items(blocked),
        "retain_as_evidence": _sample_items(retain),
        "needs_policy_classification": _sample_items(needs_policy),
        "non_deletable_rules": [
            "secret-pattern hits are blockers",
            "private references are blockers",
            "protected paths are blockers",
            "governance receipts are retained as evidence until their retention window expires",
            "recent failed-run logs are retained for the failed-run retention window",
            "v1 audit is read-only and never deletes, moves, truncates, or compresses files",
        ],
    }


def _iter_inventory_paths(root: Path) -> tuple[Path, ...]:
    harness_dir = root / ".harness"
    if not harness_dir.exists():
        return ()
    if harness_dir.is_file():
        return (Path(".harness"),)
    paths = [path.relative_to(root) for path in harness_dir.rglob("*") if path.is_file()]
    return tuple(paths)


def _registered_artifact_index(root: Path) -> dict[str, dict[str, Any]]:
    db_path = root / ".harness" / "harness.sqlite"
    if not db_path.is_file():
        return {}
    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT run_id, kind, path, metadata_json, evidence_status FROM artifacts").fetchall()
    except sqlite3.Error:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        path = Path(str(row["path"]))
        rel = _rel(root, path)
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except json.JSONDecodeError:
            metadata = {}
        out[rel] = {
            "run_id": row["run_id"],
            "kind": row["kind"],
            "metadata": metadata,
            "evidence_status": row["evidence_status"],
        }
    return out


def _run_statuses(root: Path) -> dict[str, str]:
    db_path = root / ".harness" / "harness.sqlite"
    if not db_path.is_file():
        return {}
    try:
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute("SELECT id, status FROM runs").fetchall()
    except sqlite3.Error:
        return {}
    return {str(row[0]): str(row[1]) for row in rows}


def _active_governance_worktree_roots(root: Path) -> set[str]:
    if not (root / ".harness" / "harness.sqlite").is_file():
        return set()
    active: set[str] = set()
    try:
        tasks = list_governance_tasks(root)
    except Exception:
        return active
    for result in tasks:
        if result.governance.status == "closed":
            continue
        worktree_path = Path(result.governance.worktree_path)
        active.add(_rel(root, worktree_path))
    return active


def _safety_blockers(root: Path, rel_path: Path) -> list[str]:
    path = root / rel_path
    blockers: list[str] = []
    if protected_apply_path_match(rel_path) is not None:
        blockers.append("protected_path")
    if is_secret_path(path):
        blockers.append("secret_path")
    rel_text = rel_path.as_posix()
    if _contains_private_reference(rel_text):
        blockers.append("private_reference")
    if path.suffix not in TEXT_SCAN_SUFFIXES:
        return blockers
    try:
        if path.stat().st_size > 262_144:
            return blockers
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [*blockers, "unreadable"]
    if scan_text_for_secrets(text):
        blockers.append("secret_pattern")
    if _contains_private_reference(text):
        blockers.append("private_reference")
    return _dedupe(blockers)


def _summarize(items: tuple[DataInventoryItem, ...]) -> dict[str, Any]:
    by_class: dict[str, dict[str, int]] = {}
    by_owner: dict[str, int] = {}
    expired_by_class: dict[str, int] = {}
    blocker_items = []
    cleanup_candidates = []
    for item in items:
        class_summary = by_class.setdefault(item.data_class, {"count": 0, "bytes": 0, "expired_count": 0, "expired_bytes": 0})
        class_summary["count"] += 1
        class_summary["bytes"] += item.size_bytes
        by_owner[item.owner] = by_owner.get(item.owner, 0) + item.size_bytes
        if item.expired:
            class_summary["expired_count"] += 1
            class_summary["expired_bytes"] += item.size_bytes
            expired_by_class[item.data_class] = expired_by_class.get(item.data_class, 0) + item.size_bytes
        if item.blockers:
            blocker_items.append(item)
        if item.cleanup_candidate:
            cleanup_candidates.append(item)
    top_growth_directories: dict[str, int] = {}
    for item in items:
        directory = str(Path(item.path).parent)
        top_growth_directories[directory] = top_growth_directories.get(directory, 0) + item.size_bytes
    return {
        "item_count": len(items),
        "total_bytes": sum(item.size_bytes for item in items),
        "expired_count": sum(1 for item in items if item.expired),
        "expired_bytes": sum(item.size_bytes for item in items if item.expired),
        "cleanup_candidate_count": len(cleanup_candidates),
        "cleanup_candidate_bytes": sum(item.size_bytes for item in cleanup_candidates),
        "blocker_count": len(blocker_items),
        "by_class": by_class,
        "bytes_by_owner": dict(sorted(by_owner.items())),
        "expired_bytes_by_class": dict(sorted(expired_by_class.items())),
        "top_growth_directories": [
            {"path": path, "bytes": size}
            for path, size in sorted(top_growth_directories.items(), key=lambda pair: pair[1], reverse=True)[:10]
        ],
        "cleanup_requires_approval": True,
        "mutation_allowed": False,
    }


def _proposal_bucket_summary(items: tuple[DataInventoryItem, ...]) -> dict[str, Any]:
    return {
        "count": len(items),
        "bytes": sum(item.size_bytes for item in items),
        "by_class": _count_bytes_by(items, "data_class"),
        "by_owner": _count_bytes_by(items, "owner"),
    }


def _count_bytes_by(items: tuple[DataInventoryItem, ...], attr: str) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for item in items:
        key = str(getattr(item, attr))
        bucket = out.setdefault(key, {"count": 0, "bytes": 0})
        bucket["count"] += 1
        bucket["bytes"] += item.size_bytes
    return dict(sorted(out.items()))


def _top_directories(items: tuple[DataInventoryItem, ...], *, limit: int = 10) -> list[dict[str, Any]]:
    totals: dict[str, dict[str, int]] = {}
    for item in items:
        directory = _proposal_group_path(item.path)
        bucket = totals.setdefault(directory, {"count": 0, "bytes": 0})
        bucket["count"] += 1
        bucket["bytes"] += item.size_bytes
    return [
        {"path": path, "count": bucket["count"], "bytes": bucket["bytes"]}
        for path, bucket in sorted(totals.items(), key=lambda pair: pair[1]["bytes"], reverse=True)[:limit]
    ]


def _sample_items(items: tuple[DataInventoryItem, ...], *, limit: int = 25) -> list[dict[str, Any]]:
    sorted_items = sorted(items, key=lambda item: item.size_bytes, reverse=True)
    return [
        {
            "path": item.path,
            "data_class": item.data_class,
            "owner": item.owner,
            "size_bytes": item.size_bytes,
            "age_days": item.age_days,
            "expired": item.expired,
            "safety": item.safety,
            "blockers": list(item.blockers),
            "reason": item.reason,
            "action_after_retention": item.action_after_retention,
        }
        for item in sorted_items[:limit]
    ]


def _retain_as_evidence(item: DataInventoryItem) -> bool:
    if item.cleanup_candidate or item.blockers:
        return False
    if item.owner in {"harness_governance", "harness_state"}:
        return True
    if item.data_class == "canonical_decision":
        return True
    if item.data_class == "raw_execution_log" and item.reason.startswith("failed run") and not item.expired:
        return True
    return False


def _proposal_group_path(path: str) -> str:
    parts = Path(path).parts
    if len(parts) >= 4 and parts[0] == ".harness" and parts[1] == "governance" and parts[2] == "worktrees":
        return str(Path(parts[0]) / parts[1] / parts[2] / parts[3])
    if len(parts) >= 3 and parts[0] == ".harness" and parts[1] == "runs":
        return str(Path(parts[0]) / parts[1] / parts[2])
    if len(parts) >= 3 and parts[0] == ".harness" and parts[1] == "sessions":
        return str(Path(parts[0]) / parts[1] / parts[2])
    return str(Path(path).parent)


def _artifact_is_promoted(artifact: dict[str, Any]) -> bool:
    metadata = artifact.get("metadata")
    if not isinstance(metadata, dict):
        return False
    return bool(metadata.get("promoted")) or str(metadata.get("promotion_status", "")).lower() == "promoted"


def _run_id_from_rel(rel: str) -> str | None:
    parts = Path(rel).parts
    if len(parts) >= 3 and parts[0] == ".harness" and parts[1] == "runs":
        return parts[2]
    return None


def _contains_private_reference(text: str) -> bool:
    lowered = text.lower()
    return "inbox/new" in lowered or "private_inbox" in lowered


def _rel(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root).as_posix()
    except ValueError:
        raw = path.as_posix()
        if raw.startswith("./"):
            return raw[2:]
        return raw


def _normalize_rel(path: str | Path) -> Path:
    raw = Path(path)
    if raw.is_absolute():
        raise ValueError(f"expected workspace-relative path: {path}")
    return Path(str(raw).strip("/"))


def _age_days(modified_at: datetime, now: datetime) -> int:
    if modified_at.tzinfo is None:
        modified_at = modified_at.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return max(0, int((now - modified_at).total_seconds() // 86400))


def _dedupe(values: list[str]) -> list[str]:
    out: list[str] = []
    for value in values:
        if value not in out:
            out.append(value)
    return out
