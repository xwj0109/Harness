from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


CONTEXT_POLICY_SCHEMA_VERSION = "harness.context_policy/v1"
CONTEXT_POLICY_LOCAL_ALLOWED = "context_local_allowed"
CONTEXT_POLICY_HOSTED_DENIED = "context_hosted_transmission_denied"
CONTEXT_POLICY_REMOTE_VECTOR_DENIED = "context_remote_vector_store_denied"
CONTEXT_POLICY_SECRET_DENIED = "context_secret_or_excluded_denied"

LOCAL_DESTINATIONS = {"local_process", "local_sqlite", "local_vector_index"}
HOSTED_DESTINATIONS = {"hosted_embedding", "hosted_reranker", "hosted_model", "hosted_compression"}
REMOTE_VECTOR_DESTINATIONS = {"remote_vector_store", "pgvector", "qdrant", "weaviate", "milvus"}


@dataclass(frozen=True)
class ContextPolicyDecision:
    destination: str
    allowed: bool
    code: str
    reason: str
    warnings: list[str] = field(default_factory=list)
    permission_granting: bool = False
    policy_authority: bool = False
    approval_authority: bool = False
    process_started: bool = False
    filesystem_modified: bool = False
    provider_call_allowed: bool = False
    docker_allowed: bool = False
    adapter_dispatch_allowed: bool = False
    active_repo_mutation_allowed: bool = False

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": CONTEXT_POLICY_SCHEMA_VERSION,
            "destination": self.destination,
            "allowed": self.allowed,
            "code": self.code,
            "reason": self.reason,
            "warnings": list(self.warnings),
            "permission_granting": self.permission_granting,
            "policy_authority": self.policy_authority,
            "approval_authority": self.approval_authority,
            "process_started": self.process_started,
            "filesystem_modified": self.filesystem_modified,
            "provider_call_allowed": self.provider_call_allowed,
            "docker_allowed": self.docker_allowed,
            "adapter_dispatch_allowed": self.adapter_dispatch_allowed,
            "active_repo_mutation_allowed": self.active_repo_mutation_allowed,
        }


def decide_context_transmission(
    destination: str,
    *,
    source_kind: str | None = None,
    trust_level: str | None = None,
    redaction_state: str | None = None,
    path: str | None = None,
    warnings: list[str] | None = None,
) -> ContextPolicyDecision:
    normalized = destination.strip().casefold()
    active_warnings = list(warnings or [])
    if _is_secret_or_excluded(path, active_warnings) or redaction_state == "forgotten":
        return ContextPolicyDecision(
            destination=destination,
            allowed=False,
            code=CONTEXT_POLICY_SECRET_DENIED,
            reason="Secret-like, context-excluded, or forgotten context cannot be transmitted or indexed.",
            warnings=active_warnings,
        )
    if normalized in LOCAL_DESTINATIONS:
        return ContextPolicyDecision(
            destination=destination,
            allowed=True,
            code=CONTEXT_POLICY_LOCAL_ALLOWED,
            reason="Local context inspection/indexing is passive and does not grant execution authority.",
            warnings=_context_warnings(source_kind, trust_level, active_warnings),
        )
    if normalized in REMOTE_VECTOR_DESTINATIONS:
        return ContextPolicyDecision(
            destination=destination,
            allowed=False,
            code=CONTEXT_POLICY_REMOTE_VECTOR_DENIED,
            reason="Remote vector stores are unsupported in this release and fail closed.",
            warnings=_context_warnings(source_kind, trust_level, active_warnings),
        )
    if normalized in HOSTED_DESTINATIONS:
        return ContextPolicyDecision(
            destination=destination,
            allowed=False,
            code=CONTEXT_POLICY_HOSTED_DENIED,
            reason="Hosted context transmission requires a future explicit approval path and is denied by default.",
            warnings=_context_warnings(source_kind, trust_level, active_warnings),
        )
    return ContextPolicyDecision(
        destination=destination,
        allowed=False,
        code=CONTEXT_POLICY_HOSTED_DENIED,
        reason="Unknown context destination is denied by default.",
        warnings=_context_warnings(source_kind, trust_level, active_warnings),
    )


def context_policy_manifest_warnings(decisions: list[ContextPolicyDecision]) -> list[str]:
    warnings: list[str] = []
    for decision in decisions:
        if not decision.allowed and decision.code not in warnings:
            warnings.append(decision.code)
        for warning in decision.warnings:
            if warning not in warnings:
                warnings.append(warning)
    return warnings


def _context_warnings(source_kind: str | None, trust_level: str | None, warnings: list[str]) -> list[str]:
    result = list(warnings)
    if source_kind == "memory_record" and "memory_not_authority" not in result:
        result.append("memory_not_authority")
    if trust_level in {"untrusted_repo", "untrusted_tool_output"} and "untrusted_context" not in result:
        result.append("untrusted_context")
    return result


def _is_secret_or_excluded(path: str | None, warnings: list[str]) -> bool:
    if any("secret" in warning or "excluded" in warning for warning in warnings):
        return True
    if not path:
        return False
    lowered = path.casefold()
    return any(
        marker in lowered
        for marker in (
            ".env",
            "secret",
            "secrets/",
            ".pem",
            ".key",
            ".harness/",
            ".git/",
        )
    )
