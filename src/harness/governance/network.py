from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from harness.governance.paths import governance_evidence_dir, governance_run_id
from harness.security import sanitize_for_logging, scan_text_for_secrets


NETWORK_POLICY_SCHEMA_VERSION = "harness.governance_network_policy/v1"
NETWORK_POLICY_CHECK_SCHEMA_VERSION = "harness.governance_network_policy_check/v1"
NETWORK_REQUEST_LOG_SCHEMA_VERSION = "harness.governance_network_request_log/v1"
DOWNLOAD_QUARANTINE_SCHEMA_VERSION = "harness.governance_download_quarantine/v1"
METADATA_SERVICE_HOSTS = {"169.254.169.254", "metadata.google.internal"}


class GovernanceNetworkPolicy(BaseModel):
    schema_version: str = NETWORK_POLICY_SCHEMA_VERSION
    policy_id: str
    task_id: str
    allowed_hosts: list[str] = Field(default_factory=list)
    denied_hosts: list[str] = Field(default_factory=list)
    allowed_protocols: list[str] = Field(default_factory=lambda: ["https"])
    allowed_methods: list[str] = Field(default_factory=lambda: ["GET"])
    proxy_endpoint: str | None = None
    request_log_path: str
    download_quarantine_path: str
    approval_id: str
    expires_at: str
    allow_downloads: bool = False
    download_quarantine: bool = True
    log_requests: bool = True
    block_metadata_services: bool = True
    source: str = "governance_network_policy"

    def to_sanitized_dict(self) -> dict[str, Any]:
        return sanitize_for_logging(self.model_dump(mode="json"))


@dataclass(frozen=True)
class GovernanceNetworkPolicyCheck:
    ok: bool
    policy: GovernanceNetworkPolicy | None
    errors: tuple[str, ...]
    gates: tuple[dict[str, object], ...]
    path: Path | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": NETWORK_POLICY_CHECK_SCHEMA_VERSION,
            "ok": self.ok,
            "policy": self.policy.to_sanitized_dict() if self.policy else None,
            "errors": list(self.errors),
            "gates": list(self.gates),
            "path": str(self.path) if self.path else None,
        }


def network_policy_from_mapping(payload: dict[str, Any]) -> GovernanceNetworkPolicy:
    policy_payload = dict(payload.get("policy") if isinstance(payload.get("policy"), dict) else payload)
    if "allowed_domains" in policy_payload and "allowed_hosts" not in policy_payload:
        policy_payload["allowed_hosts"] = policy_payload["allowed_domains"]
    if "denied_domains" in policy_payload and "denied_hosts" not in policy_payload:
        policy_payload["denied_hosts"] = policy_payload["denied_domains"]
    return GovernanceNetworkPolicy.model_validate(policy_payload)


def load_network_policy(path: Path) -> GovernanceNetworkPolicy:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("network policy file must contain a JSON object")
    return network_policy_from_mapping(payload)


def build_session_tool_network_policy(
    project_root: Path,
    *,
    session_id: str,
    task_id: str | None,
    tool_id: str,
    target: str,
    approval_id: str,
    expires_at: str,
    allowed_hosts: list[str],
    denied_hosts: list[str] | None = None,
    allowed_protocols: list[str] | None = None,
    allowed_methods: list[str] | None = None,
    proxy_endpoint: str | None = None,
    allow_downloads: bool = False,
) -> GovernanceNetworkPolicy:
    root = Path(project_root).resolve()
    stable = hashlib.sha256(
        json.dumps(
            {
                "session_id": session_id,
                "task_id": task_id,
                "tool_id": tool_id,
                "target": target,
                "approval_id": approval_id,
            },
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:16]
    policy_id = f"netpol_{stable}"
    policy_root = root / ".harness" / "governance" / "network" / policy_id
    return GovernanceNetworkPolicy(
        policy_id=policy_id,
        task_id=task_id or f"session:{session_id}",
        allowed_hosts=sorted({host.lower() for host in allowed_hosts if host}),
        denied_hosts=sorted({host.lower() for host in (denied_hosts or []) if host}),
        allowed_protocols=sorted({item.lower() for item in (allowed_protocols or ["https"]) if item}),
        allowed_methods=sorted({item.upper() for item in (allowed_methods or ["GET"]) if item}),
        proxy_endpoint=proxy_endpoint,
        request_log_path=_rel(root, policy_root / "network-request-log.json"),
        download_quarantine_path=_rel(root, policy_root / "downloads"),
        approval_id=approval_id,
        expires_at=expires_at,
        allow_downloads=allow_downloads,
        download_quarantine=True,
        log_requests=True,
        source=f"session_tool:{tool_id}",
    )


def validate_network_policy(policy: GovernanceNetworkPolicy | None, *, requires_network: bool = True) -> GovernanceNetworkPolicyCheck:
    if not requires_network:
        return GovernanceNetworkPolicyCheck(
            ok=True,
            policy=policy,
            errors=(),
            gates=(_gate("network_policy_valid", True, "network not required"),),
        )
    if policy is None:
        return GovernanceNetworkPolicyCheck(
            ok=False,
            policy=None,
            errors=("network policy evidence is required for network-enabled execution",),
            gates=(_gate("network_policy_valid", False, "missing policy"),),
        )
    errors: list[str] = []
    if not policy.policy_id.strip():
        errors.append("policy id is required")
    if not policy.task_id.strip():
        errors.append("mission/task id is required")
    if not policy.allowed_hosts:
        errors.append("at least one allowed host/domain is required")
    if not policy.request_log_path.strip():
        errors.append("request log path is required")
    if not policy.download_quarantine_path.strip():
        errors.append("download quarantine path is required")
    if not policy.approval_id.strip():
        errors.append("approval id is required")
    if not policy.log_requests:
        errors.append("network request logging is required")
    if not policy.download_quarantine:
        errors.append("download quarantine is required")
    if policy.block_metadata_services and any(host in METADATA_SERVICE_HOSTS for host in policy.allowed_hosts):
        errors.append("metadata service hosts cannot be allowlisted")
    overlap = set(policy.allowed_hosts).intersection(set(policy.denied_hosts))
    if overlap:
        errors.append(f"allowed hosts overlap denied hosts: {', '.join(sorted(overlap))}")
    if _expired(policy.expires_at):
        errors.append("network policy is expired")
    return GovernanceNetworkPolicyCheck(
        ok=not errors,
        policy=policy,
        errors=tuple(errors),
        gates=(
            _gate("network_policy_valid", not errors, "; ".join(errors) if errors else "policy has required evidence"),
            _gate("artifact_quarantined", policy.download_quarantine, f"download_quarantine={policy.download_quarantine}"),
        ),
    )


def evaluate_network_request(policy: GovernanceNetworkPolicy, url: str, *, method: str = "GET") -> dict[str, Any]:
    parsed = urlparse(url)
    host = "local/file" if parsed.scheme.lower() == "file" else (parsed.hostname or "").lower()
    protocol = parsed.scheme.lower()
    normalized_method = method.upper()
    reason = "allowed by governance network policy"
    allowed = True
    if parsed.username or parsed.password:
        allowed = False
        reason = "URL credentials are forbidden"
    elif not host:
        allowed = False
        reason = "URL host is required"
    elif host in {item.lower() for item in policy.denied_hosts}:
        allowed = False
        reason = "host is explicitly denied"
    elif host not in {item.lower() for item in policy.allowed_hosts}:
        allowed = False
        reason = "host is not allowlisted"
    elif protocol not in {item.lower() for item in policy.allowed_protocols}:
        allowed = False
        reason = "protocol is not allowlisted"
    elif normalized_method not in {item.upper() for item in policy.allowed_methods}:
        allowed = False
        reason = "method is not allowlisted"
    elif policy.block_metadata_services and host in METADATA_SERVICE_HOSTS:
        allowed = False
        reason = "metadata service host is blocked"
    return {
        "schema_version": "harness.governance_network_decision/v1",
        "allowed": allowed,
        "reason": reason,
        "request": {"url": url, "method": normalized_method, "protocol": protocol, "host": host},
        "policy_id": policy.policy_id,
    }


def write_network_policy_check(project_root: Path, policy: GovernanceNetworkPolicy) -> GovernanceNetworkPolicyCheck:
    root = Path(project_root).resolve()
    check = validate_network_policy(policy)
    run_id = governance_run_id("network", policy.policy_id)
    evidence_dir = governance_evidence_dir(root, "network", run_id)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    path = evidence_dir / "policy-check.json"
    payload = {**check.to_dict(), "path": _rel(root, path)}
    path.write_text(json.dumps(sanitize_for_logging(payload), indent=2, sort_keys=True), encoding="utf-8")
    return GovernanceNetworkPolicyCheck(check.ok, check.policy, check.errors, check.gates, path)


def write_network_request_log(
    project_root: Path,
    policy: GovernanceNetworkPolicy,
    decisions: list[dict[str, Any]],
) -> Path:
    root = Path(project_root).resolve()
    path = root / policy.request_log_path
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": NETWORK_REQUEST_LOG_SCHEMA_VERSION,
        "log_id": "network-log-" + uuid.uuid4().hex[:8],
        "policy_id": policy.policy_id,
        "task_id": policy.task_id,
        "created_at": _now(),
        "decisions": decisions,
    }
    path.write_text(json.dumps(sanitize_for_logging(payload), indent=2, sort_keys=True), encoding="utf-8")
    return path


def write_download_quarantine_record(
    project_root: Path,
    policy: GovernanceNetworkPolicy,
    *,
    source_url: str,
    artifact_path: str | None = None,
    sha256: str | None = None,
    approved_for_promotion: bool = False,
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    quarantine_root = root / policy.download_quarantine_path
    quarantine_root.mkdir(parents=True, exist_ok=True)
    quarantine_path = quarantine_root / _safe_download_name(source_url)
    record = {
        "schema_version": DOWNLOAD_QUARANTINE_SCHEMA_VERSION,
        "source_url": source_url,
        "quarantine_path": _rel(root, quarantine_path),
        "artifact_path": artifact_path,
        "sha256": sha256,
        "approved_for_promotion": approved_for_promotion,
        "policy_id": policy.policy_id,
        "approval_id": policy.approval_id,
        "created_at": _now(),
    }
    record_path = quarantine_root.parent / "download-quarantine.json"
    record_path.write_text(json.dumps(sanitize_for_logging(record), indent=2, sort_keys=True), encoding="utf-8")
    return record


def no_secret_network_evidence(payload: dict[str, Any]) -> bool:
    return not scan_text_for_secrets(json.dumps(sanitize_for_logging(payload), sort_keys=True, default=str))


def _safe_download_name(source_url: str) -> str:
    parsed = urlparse(source_url)
    name = Path(parsed.path).name or parsed.hostname or "download"
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip(".-")
    return safe or "download"


def _expired(expires_at: str) -> bool:
    try:
        parsed = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except ValueError:
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed <= datetime.now(timezone.utc)


def _gate(gate_id: str, passed: bool, evidence: str) -> dict[str, object]:
    return {"id": gate_id, "passed": passed, "evidence": evidence}


def _rel(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
