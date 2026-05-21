from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

from harness.governance.gate_registry import require_known_gate
from harness.governance.paths import governance_evidence_dir, governance_run_id
from harness.governance.protected_paths import ProtectedPathMatch, protected_apply_path_match
from harness.policy import stable_json_sha256
from harness.security import sanitize_for_logging


SCHEMA_VERSION = "harness.governance_applyback_verdict/v1"
PASSING_TEST_STATUSES = frozenset({"pass", "passed"})
DEFAULT_MAX_TEST_AGE_HOURS = 24


@dataclass(frozen=True)
class GovernanceApplybackResult:
    payload: dict[str, Any]
    path: Path | None = None

    @property
    def ok(self) -> bool:
        return bool(self.payload.get("ok"))

    @property
    def verdict(self) -> str:
        return str(self.payload.get("verdict") or "reject")

    @property
    def policy_hash(self) -> str:
        return str(self.payload.get("policy_hash") or "")


def validate_applyback_promotion(
    payload: Mapping[str, Any],
    *,
    now: datetime | None = None,
    max_test_age_hours: int = DEFAULT_MAX_TEST_AGE_HOURS,
) -> GovernanceApplybackResult:
    checked_at = _coerce_aware(now) or datetime.now(timezone.utc)
    task_id = _string(payload.get("task_id"))
    segment_id = _string(payload.get("segment_id"))
    objective_id = _string(payload.get("objective_id"))
    context_pack_hash = _string(payload.get("context_pack_hash"))
    approval_id = _string(payload.get("approval_id"))
    changed_files = _strings(payload.get("changed_files") or payload.get("changed_paths"))
    allowed_paths = _strings(payload.get("allowed_paths"))
    test_evidence = _mapping(payload.get("test_evidence"))
    artifacts = _mappings(payload.get("artifacts"))
    network_policy = _mapping(payload.get("network_policy"))
    protected_exceptions = _mappings(payload.get("protected_path_exceptions"))
    diff_summary = _diff_summary(payload.get("diff_summary"), changed_files)

    gate_results: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []

    segment_ok = bool(task_id and (segment_id or objective_id))
    segment_reasons: list[str] = []
    if not task_id:
        segment_reasons.append("task_id is required")
    if not (segment_id or objective_id):
        segment_reasons.append("segment_id or objective_id is required")
    gate_results.append(_gate("applyback_bound_to_segment", segment_ok, "; ".join(segment_reasons) or "task and segment binding present"))
    for reason in segment_reasons:
        findings.append(_finding("applyback_segment_binding_missing", reason))

    context_ok = bool(context_pack_hash)
    gate_results.append(
        _gate(
            "segment_context_pack_present",
            context_ok,
            f"context_pack_hash={context_pack_hash or 'missing'}",
        )
    )
    if not context_ok:
        findings.append(_finding("applyback_context_pack_missing", "context_pack_hash is required"))

    outside_scope = [path for path in changed_files if not _path_allowed(path, allowed_paths)]
    paths_ok = bool(changed_files) and not outside_scope
    gate_results.append(
        _gate(
            "allowed_paths_respected",
            paths_ok,
            f"changed={len(changed_files)} outside_scope={len(outside_scope)}",
            {"changed_files": changed_files, "outside_scope": outside_scope, "allowed_paths": allowed_paths},
        )
    )
    if not changed_files:
        findings.append(_finding("applyback_changed_files_missing", "changed_files is required"))
    for path in outside_scope:
        findings.append(_finding("applyback_path_outside_scope", f"changed file is outside allowed paths: {path}", path=path))

    protected_hits = _protected_hits(changed_files)
    missing_exceptions = [
        hit for hit in protected_hits if not _protected_exception_present(hit, protected_exceptions, approval_id=approval_id)
    ]
    protected_ok = not missing_exceptions
    gate_results.append(
        _gate(
            "no_protected_writes",
            protected_ok,
            f"protected_hits={len(protected_hits)} missing_exceptions={len(missing_exceptions)}",
            {
                "protected_hits": [hit.to_dict() for hit in protected_hits],
                "missing_exception_hits": [hit.to_dict() for hit in missing_exceptions],
                "exceptions": protected_exceptions,
            },
        )
    )
    for hit in missing_exceptions:
        findings.append(
            _finding(
                "applyback_protected_path_requires_exception",
                f"protected path requires explicit exception evidence: {hit.path}",
                path=hit.path,
                details={"pattern": hit.pattern},
            )
        )

    tests_ok, test_reasons = _test_evidence_valid(
        task_id=task_id,
        segment_id=segment_id,
        context_pack_hash=context_pack_hash,
        evidence=test_evidence,
        now=checked_at,
        max_age=timedelta(hours=max_test_age_hours),
    )
    gate_results.append(
        _gate(
            "test_evidence_fresh",
            tests_ok,
            "; ".join(test_reasons) or "fresh passing test evidence is bound to the task",
            {"test_evidence": _summarize_test_evidence(test_evidence), "reasons": test_reasons},
        )
    )
    gate_results.append(
        _gate(
            "promotion_tests_current",
            tests_ok,
            "; ".join(test_reasons) or "promotion tests are current",
            {"test_evidence": _summarize_test_evidence(test_evidence), "reasons": test_reasons},
        )
    )
    for reason in test_reasons:
        findings.append(_finding("applyback_test_evidence_invalid", reason))

    promotion_segment_ok, promotion_segment_reasons = _promotion_segment_valid(
        task_id=task_id,
        segment_id=segment_id,
        objective_id=objective_id,
        test_evidence=test_evidence,
        network_policy=network_policy,
    )
    gate_results.append(
        _gate(
            "promotion_segment_bound",
            promotion_segment_ok,
            "; ".join(promotion_segment_reasons) or "promotion evidence is segment-bound",
            {"task_id": task_id, "segment_id": segment_id or None, "objective_id": objective_id or None},
        )
    )
    for reason in promotion_segment_reasons:
        findings.append(_finding("applyback_promotion_segment_invalid", reason))

    quarantined = _quarantined_artifacts(artifacts)
    quarantine_ok = not quarantined
    gate_results.append(
        _gate(
            "promotion_not_quarantined",
            quarantine_ok,
            f"quarantined_artifacts={len(quarantined)}",
            {"quarantined_artifacts": quarantined},
        )
    )
    for artifact in quarantined:
        findings.append(
            _finding(
                "applyback_quarantined_artifact",
                "quarantined artifact cannot be promoted without review promotion evidence",
                path=_string(artifact.get("path")) or None,
                details={"artifact_id": artifact.get("id")},
            )
        )

    network_ok, network_reasons = _network_policy_valid(network_policy)
    gate_results.append(
        _gate(
            "promotion_network_policy_valid",
            network_ok,
            "; ".join(network_reasons) or "network policy is absent or explicitly disabled for promotion",
            {"network_policy": network_policy},
        )
    )
    for reason in network_reasons:
        findings.append(_finding("applyback_network_policy_invalid", reason))

    gate_ids = [gate["id"] for gate in gate_results]
    hard_gate_passed = all(bool(gate["passed"]) for gate in gate_results)
    policy = {
        "task_id": task_id,
        "segment_id": segment_id or None,
        "objective_id": objective_id or None,
        "context_pack_hash": context_pack_hash or None,
        "approval_id": approval_id or None,
        "allowed_paths": allowed_paths,
        "changed_files": changed_files,
        "diff_summary": diff_summary,
        "protected_path_exceptions": protected_exceptions,
        "gate_ids": gate_ids,
        "max_test_age_hours": max_test_age_hours,
    }
    policy_hash = stable_json_sha256(policy)
    result = {
        "schema_version": SCHEMA_VERSION,
        "ok": hard_gate_passed,
        "verdict": "approve" if hard_gate_passed else "reject",
        "generated_at": checked_at.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "policy_hash": policy_hash,
        "task_id": task_id or None,
        "segment_id": segment_id or None,
        "objective_id": objective_id or None,
        "context_pack_hash": context_pack_hash or None,
        "approval_id": approval_id or None,
        "changed_files": changed_files,
        "diff_summary": diff_summary,
        "allowed_paths": allowed_paths,
        "gate_ids": gate_ids,
        "hard_gates": gate_results,
        "findings": findings,
        "policy": policy,
        "operator_authority": {
            "permission_granted": False,
            "future_authority_granted": False,
            "active_repo_mutation_performed": False,
            "durable_evidence_only": True,
        },
    }
    clean = sanitize_for_logging(result)
    return GovernanceApplybackResult(payload=clean if isinstance(clean, dict) else result)


def write_applyback_evidence(
    project_root: Path,
    payload: Mapping[str, Any],
    *,
    now: datetime | None = None,
    max_test_age_hours: int = DEFAULT_MAX_TEST_AGE_HOURS,
) -> GovernanceApplybackResult:
    result = validate_applyback_promotion(payload, now=now, max_test_age_hours=max_test_age_hours)
    task_id = _string(payload.get("task_id")) or "unbound"
    run_id = governance_run_id("applyback", task_id)
    evidence_dir = governance_evidence_dir(Path(project_root).resolve(), "applyback", run_id)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    path = evidence_dir / "applyback-verdict.json"
    path.write_text(json.dumps(result.payload, indent=2, sort_keys=True), encoding="utf-8")
    evidence_payload = dict(result.payload)
    evidence_payload["path"] = str(path)
    return GovernanceApplybackResult(payload=evidence_payload, path=path)


def load_applyback_request(path: Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("apply-back request must be a JSON object")
    return payload


def deferred_applyback_evidence(
    *,
    changed_files: Sequence[str],
    diff_summary: Mapping[str, Any],
    reason: str,
    approval_id: str | None = None,
    allowed_paths: Sequence[str] | None = None,
) -> dict[str, Any]:
    payload = {
        "task_id": None,
        "segment_id": None,
        "context_pack_hash": None,
        "approval_id": approval_id,
        "allowed_paths": list(allowed_paths or changed_files),
        "changed_files": list(changed_files),
        "diff_summary": dict(diff_summary),
        "test_evidence": None,
    }
    result = validate_applyback_promotion(payload)
    return {
        "schema_version": "harness.governance_applyback_preflight/v1",
        "ready": False,
        "reason": reason,
        "policy_hash": result.policy_hash,
        "approval_id": approval_id,
        "changed_files": list(changed_files),
        "diff_summary": dict(diff_summary),
        "gate_ids": result.payload.get("gate_ids", []),
        "hard_gates": result.payload.get("hard_gates", []),
        "verdict": result.verdict,
        "operator_authority": result.payload.get("operator_authority", {}),
    }


def _gate(gate_id: str, passed: bool, evidence: str, details: Mapping[str, Any] | None = None) -> dict[str, Any]:
    spec = require_known_gate(gate_id)
    return {
        "id": spec.id,
        "description": spec.description,
        "layer": spec.layer,
        "severity_on_fail": spec.severity_on_fail,
        "passed": passed,
        "evidence": evidence,
        "details": dict(details or {}),
    }


def _finding(finding_id: str, summary: str, *, path: str | None = None, details: Mapping[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": finding_id,
        "severity": "critical",
        "summary": summary,
        "details": {"merge_impact": "critical blockers", **dict(details or {})},
    }
    if path:
        payload["path"] = path
    return payload


def _diff_summary(value: Any, changed_files: list[str]) -> dict[str, Any]:
    if isinstance(value, Mapping):
        summary = dict(value)
    else:
        summary = {}
    files = _strings(summary.get("files")) or changed_files
    summary["files"] = files
    summary["file_count"] = int(summary.get("file_count") or len(files))
    summary.setdefault("added_lines", 0)
    summary.setdefault("removed_lines", 0)
    return summary


def _protected_hits(paths: Sequence[str]) -> list[ProtectedPathMatch]:
    hits: list[ProtectedPathMatch] = []
    for path in paths:
        match = protected_apply_path_match(path)
        if match is not None:
            hits.append(match)
    return hits


def _protected_exception_present(hit: ProtectedPathMatch, exceptions: Sequence[Mapping[str, Any]], *, approval_id: str) -> bool:
    for exception in exceptions:
        exception_path = _string(exception.get("path"))
        exception_pattern = _string(exception.get("pattern"))
        exception_approval = _string(exception.get("approval_id"))
        exception_evidence = _string(exception.get("evidence_id") or exception.get("evidence_path"))
        path_matches = exception_path == hit.path or exception_pattern == hit.pattern
        approval_matches = bool(exception_approval) and (not approval_id or exception_approval == approval_id)
        if path_matches and approval_matches and exception_evidence:
            return True
    return False


def _test_evidence_valid(
    *,
    task_id: str,
    segment_id: str,
    context_pack_hash: str,
    evidence: Mapping[str, Any],
    now: datetime,
    max_age: timedelta,
) -> tuple[bool, list[str]]:
    if not evidence:
        return False, ["missing test evidence"]
    reasons: list[str] = []
    evidence_task_id = _string(evidence.get("task_id"))
    if task_id and evidence_task_id != task_id:
        reasons.append(f"test evidence belongs to {evidence_task_id or 'missing'}, expected {task_id}")
    evidence_segment = _string(evidence.get("segment_id"))
    if segment_id and evidence_segment and evidence_segment != segment_id:
        reasons.append(f"test evidence segment {evidence_segment} does not match {segment_id}")
    evidence_context_hash = _string(evidence.get("context_pack_hash"))
    if context_pack_hash and evidence_context_hash and evidence_context_hash != context_pack_hash:
        reasons.append("test evidence context_pack_hash does not match apply-back context pack")
    status = _string(evidence.get("status")).lower()
    if status not in PASSING_TEST_STATUSES:
        reasons.append(f"test evidence status is not passing: {status or 'missing'}")
    generated_at = _parse_timestamp(evidence.get("generated_at"))
    if generated_at is None:
        reasons.append("test evidence is missing generated_at")
    elif now - generated_at > max_age:
        reasons.append("test evidence is stale")
    return not reasons, reasons


def _promotion_segment_valid(
    *,
    task_id: str,
    segment_id: str,
    objective_id: str,
    test_evidence: Mapping[str, Any],
    network_policy: Mapping[str, Any],
) -> tuple[bool, list[str]]:
    if not task_id or not (segment_id or objective_id):
        return False, ["promotion is missing task or segment binding"]
    reasons: list[str] = []
    expected = segment_id or objective_id
    for label, item in (("test evidence", test_evidence), ("network policy", network_policy)):
        if not item:
            continue
        item_segment = _string(item.get("segment_id") or item.get("objective_id"))
        if item_segment and item_segment != expected:
            reasons.append(f"{label} segment {item_segment} does not match {expected}")
        item_task = _string(item.get("task_id"))
        if item_task and item_task != task_id:
            reasons.append(f"{label} task {item_task} does not match {task_id}")
    return not reasons, reasons


def _quarantined_artifacts(artifacts: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    blocked: list[dict[str, Any]] = []
    for artifact in artifacts:
        metadata = _mapping(artifact.get("metadata"))
        combined = {**dict(metadata), **dict(artifact)}
        if not bool(combined.get("quarantined")):
            continue
        if _artifact_promoted_by_review(combined):
            continue
        blocked.append(dict(combined))
    return blocked


def _artifact_promoted_by_review(artifact: Mapping[str, Any]) -> bool:
    if bool(artifact.get("approved_for_promotion")):
        return True
    if _string(artifact.get("promotion_status")).lower() in {"promoted", "approved"}:
        return True
    for review in _mappings(artifact.get("review_evidence")):
        kind = _string(review.get("kind")).lower()
        verdict = _string(review.get("verdict")).lower()
        if kind in {"visual", "security", "quality"} and verdict in {"promote", "approved", "pass", "passed"}:
            return True
    return False


def _network_policy_valid(policy: Mapping[str, Any]) -> tuple[bool, list[str]]:
    if not policy:
        return True, []
    if bool(policy.get("network_disabled")) or _string(policy.get("mode")).lower() in {"none", "no_network", "disabled"}:
        return True, []
    if bool(policy.get("allow_downloads")) or _strings(policy.get("allowed_hosts")) or _strings(policy.get("allowed_domains")):
        return False, ["promotion evidence cannot carry enabled network policy authority"]
    return True, []


def _path_allowed(path: str, allowed_patterns: Sequence[str]) -> bool:
    normalized = _normalize_path(path)
    if not normalized or normalized.startswith("/") or normalized == ".." or normalized.startswith("../"):
        return False
    if ".." in PurePosixPath(normalized).parts:
        return False
    if not allowed_patterns:
        return False
    return any(_pattern_matches(normalized, pattern) for pattern in allowed_patterns)


def _pattern_matches(path: str, pattern: str) -> bool:
    normalized_pattern = _normalize_path(pattern)
    if normalized_pattern == "**":
        return True
    if normalized_pattern.endswith("/**"):
        prefix = normalized_pattern[:-3].rstrip("/")
        return path == prefix or path.startswith(prefix + "/")
    if normalized_pattern.endswith("/"):
        return path.startswith(normalized_pattern)
    return fnmatch.fnmatchcase(path, normalized_pattern)


def _normalize_path(path: str) -> str:
    return str(PurePosixPath(str(path).replace("\\", "/")))


def _summarize_test_evidence(evidence: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "task_id": evidence.get("task_id"),
        "segment_id": evidence.get("segment_id"),
        "status": evidence.get("status"),
        "generated_at": evidence.get("generated_at"),
        "context_pack_hash": evidence.get("context_pack_hash"),
    }


def _parse_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    raw = str(value)
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return _coerce_aware(parsed)


def _coerce_aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _string(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if not isinstance(value, Sequence):
        return []
    return [_string(item) for item in value if _string(item)]


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _mappings(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]
