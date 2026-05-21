"""Governance contracts for Harness authority boundaries."""

from harness.governance.gate_registry import (
    GATES_BY_ID,
    GATE_SPECS,
    SCHEMA_VERSION as GATE_REGISTRY_SCHEMA_VERSION,
    GateSpec,
    gate_registry_payload,
    require_known_gate,
)
from harness.governance.protected_paths import (
    PROTECTED_APPLY_PATTERNS,
    ProtectedPathMatch,
    is_protected_apply_path,
    protected_apply_path_match,
)

__all__ = [
    "GATES_BY_ID",
    "GATE_REGISTRY_SCHEMA_VERSION",
    "GATE_SPECS",
    "GateSpec",
    "PROTECTED_APPLY_PATTERNS",
    "ProtectedPathMatch",
    "gate_registry_payload",
    "is_protected_apply_path",
    "protected_apply_path_match",
    "require_known_gate",
]
