from __future__ import annotations

import pytest

from harness.governance.gate_registry import GATES_BY_ID, gate_registry_payload, require_known_gate
from harness.governance.protected_paths import PROTECTED_APPLY_PATTERNS


def test_gate_registry_payload_is_canonical_and_includes_protected_paths() -> None:
    payload = gate_registry_payload()

    assert payload["schema_version"] == "harness.governance.gate_registry/v1"
    assert payload["protected_apply_patterns_source"] == "harness.governance.protected_paths.PROTECTED_APPLY_PATTERNS"
    assert payload["protected_apply_patterns"] == list(PROTECTED_APPLY_PATTERNS)
    assert "no_protected_writes" in GATES_BY_ID
    assert "no_unsafe_sandbox_network_change" in GATES_BY_ID
    assert require_known_gate("no_provider_permission_widening").layer == "merge"


def test_gate_registry_fails_closed_for_unknown_gate() -> None:
    with pytest.raises(KeyError):
        require_known_gate("unknown_gate")
