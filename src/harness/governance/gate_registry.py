from __future__ import annotations

from dataclasses import dataclass

from harness.governance.protected_paths import PROTECTED_APPLY_PATTERNS


SCHEMA_VERSION = "harness.governance.gate_registry/v1"


@dataclass(frozen=True)
class GateSpec:
    id: str
    description: str
    layer: str
    severity_on_fail: str = "critical"
    source: str = "toloclaw_governance_parity"

    def to_dict(self) -> dict[str, str]:
        return {
            "id": self.id,
            "description": self.description,
            "layer": self.layer,
            "severity_on_fail": self.severity_on_fail,
            "source": self.source,
        }


GATE_SPECS: tuple[GateSpec, ...] = (
    GateSpec("input_task_scope_declared", "Task declares goal, owner, permissions, sandbox, and expected artifacts.", "input"),
    GateSpec("sandbox_capabilities_declared", "Sandbox/tool capabilities are explicit and auditable.", "sandbox"),
    GateSpec("no_protected_writes", "Protected infrastructure paths are not modified without approval.", "applyback"),
    GateSpec("no_secret_in_diff", "Added diff lines contain no secret-like values.", "merge"),
    GateSpec("no_dangerous_subprocess_strings", "Added diff lines contain no dangerous execution strings.", "merge"),
    GateSpec("tests_pass", "Required Harness tests pass before merge.", "merge"),
    GateSpec("merge_base_resolves", "Base and branch resolve to a common merge base.", "merge"),
    GateSpec("branch_contains_current_base", "Branch is not behind the current integration base.", "merge"),
    GateSpec("no_mass_deletion_shape", "Branch diff is not dominated by deletions.", "merge"),
    GateSpec("no_core_workspace_deletions", "Branch does not delete core workspace files.", "merge"),
    GateSpec("diff_size_bounded", "Branch diff is small enough for reliable local review.", "merge"),
    GateSpec("no_vendored_third_party_diff", "Branch does not mix vendored third-party material into governance merge scope.", "merge"),
    GateSpec("context_retrieval_uses_compiler", "New workspace-context retrieval goes through Harness context retrieval or declares a justified exception.", "merge"),
    GateSpec("context_budget_enforced", "New prompt assembly uses Harness context budget enforcement or declares a justified exception.", "merge"),
    GateSpec("no_workspace_authority_drift", "Workspace authority files and policy settings do not drift without explicit review.", "merge"),
    GateSpec("no_provider_permission_widening", "Provider and backend configs do not widen authority.", "merge"),
    GateSpec("no_unsafe_sandbox_network_change", "Sandbox profiles do not enable broader network access.", "merge"),
    GateSpec("allowed_paths_respected", "Segment changes stay within declared allowed paths.", "promotion"),
    GateSpec("segment_context_pack_present", "Segment has a context pack or equivalent mission brief evidence.", "promotion"),
    GateSpec("test_evidence_fresh", "Segment has fresh passing test evidence.", "promotion"),
    GateSpec("applyback_bound_to_segment", "Apply-back or promotion evidence is bound to a governed segment.", "promotion"),
    GateSpec("checkpoint_approved", "Required mission checkpoint has approval or passing deterministic verdict.", "promotion"),
    GateSpec("isolation_transition_approved", "Isolation escalation has recorded reason and approval evidence.", "sandbox"),
    GateSpec("network_policy_valid", "Network-enabled isolation has a scoped allowlist, logging, quarantine, and approval evidence.", "sandbox"),
    GateSpec("artifact_quarantined", "Artifacts from elevated isolation are quarantined before promotion.", "promotion"),
    GateSpec("promotion_paths_within_scope", "Promoted changes stay within the task's allowed path scope.", "promotion"),
    GateSpec("promotion_not_quarantined", "Quarantined artifacts are not promoted into trusted workspace state.", "promotion"),
    GateSpec("promotion_tests_current", "Promotion has passing, fresh test evidence bound to the task.", "promotion"),
    GateSpec("promotion_segment_bound", "Promotion evidence is bound to the expected task segment.", "promotion"),
    GateSpec("promotion_network_policy_valid", "Promotion uses a valid no-network or explicitly disabled network policy.", "promotion"),
    GateSpec("post_merge_audit_recorded", "Merged work has durable provenance evidence.", "post_merge", severity_on_fail="high"),
)


GATES_BY_ID = {gate.id: gate for gate in GATE_SPECS}


def gate_registry_payload() -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "protected_apply_patterns_source": "harness.governance.protected_paths.PROTECTED_APPLY_PATTERNS",
        "protected_apply_patterns": list(PROTECTED_APPLY_PATTERNS),
        "gates": [gate.to_dict() for gate in GATE_SPECS],
    }


def require_known_gate(gate_id: str) -> GateSpec:
    if gate_id not in GATES_BY_ID:
        raise KeyError(f"unknown governance gate: {gate_id}")
    return GATES_BY_ID[gate_id]
