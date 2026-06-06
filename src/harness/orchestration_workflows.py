from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from harness.agent_handoff import build_agent_handoff_envelope
from harness.config import HARNESS_DIR
from harness.context_policy import decide_context_transmission
from harness.external_protocols import build_external_protocol_catalog
from harness.objective_checkpoints import (
    create_objective_checkpoint,
    evaluate_objective_checkpoint_gate,
    resolve_objective_checkpoint,
)
from harness.objective_evidence import verify_objective_evidence
from harness.objective_runner import run_objective_autonomously, run_objective_parallel
from harness.orchestration_replay import replay_objective_event_log, run_orchestration_replay_audit
from harness.paths import resolve_project_root
from harness.traces import export_objective_trace, export_run_trace


WORKFLOW_COORDINATION_CATALOG_SCHEMA_VERSION = "harness.workflow_coordination_catalog/v1"
WORKFLOW_PATTERN_CONTRACT_SCHEMA_VERSION = "harness.workflow_pattern_contract/v1"
WORKFLOW_STATE_CLASS_SCHEMA_VERSION = "harness.workflow_state_class/v1"
WORKFLOW_COORDINATION_SUMMARY_SCHEMA_VERSION = "harness.workflow_coordination_summary/v1"

WorkflowContractStatus = Literal["pass", "warning", "fail"]

REQUIRED_WORKFLOW_PATTERN_IDS: tuple[str, ...] = (
    "durable_supervisor",
    "sequential_steps",
    "bounded_parallel_fanout",
    "typed_agent_handoff",
    "human_approval_pause",
    "append_only_replay",
    "external_protocol_boundary",
)

REQUIRED_STATE_CLASS_IDS: tuple[str, ...] = (
    "session_state",
    "workflow_state",
    "memory_state",
    "artifact_state",
)


class WorkflowStateClass(BaseModel):
    schema_version: str = WORKFLOW_STATE_CLASS_SCHEMA_VERSION
    id: str
    title: str
    owner_surface: str
    durability: str
    mutation_owner: str
    replay_source: str | None = None
    model_context_allowed_by_default: bool = False
    authority_notes: list[str] = Field(default_factory=list)


class WorkflowPatternContract(BaseModel):
    schema_version: str = WORKFLOW_PATTERN_CONTRACT_SCHEMA_VERSION
    id: str
    title: str
    status: WorkflowContractStatus
    execution_mode: str
    reference_patterns: list[str] = Field(default_factory=list)
    harness_surfaces: list[str] = Field(default_factory=list)
    state_classes: list[str] = Field(default_factory=list)
    invariants: list[str] = Field(default_factory=list)
    evidence: dict[str, Any] = Field(default_factory=dict)
    gaps: list[str] = Field(default_factory=list)
    next_actions: list[str] = Field(default_factory=list)


class WorkflowCoordinationCatalog(BaseModel):
    schema_version: str = WORKFLOW_COORDINATION_CATALOG_SCHEMA_VERSION
    ok: bool
    project_root: Path
    initialized: bool
    patterns: list[WorkflowPatternContract]
    state_classes: list[WorkflowStateClass]
    required_pattern_ids: list[str]
    required_state_class_ids: list[str]
    summary: dict[str, int]
    safety: dict[str, bool]


def build_workflow_coordination_catalog(project_root: Path) -> WorkflowCoordinationCatalog:
    """Describe the supported orchestration coordination contracts without executing work."""

    root = resolve_project_root(project_root)
    initialized = (root / HARNESS_DIR / "harness.sqlite").exists()
    patterns = _workflow_pattern_contracts(root)
    state_classes = _state_classes()
    pattern_ids = {pattern.id for pattern in patterns}
    state_class_ids = {state_class.id for state_class in state_classes}
    missing_patterns = [pattern_id for pattern_id in REQUIRED_WORKFLOW_PATTERN_IDS if pattern_id not in pattern_ids]
    missing_state_classes = [state_id for state_id in REQUIRED_STATE_CLASS_IDS if state_id not in state_class_ids]
    failing_patterns = [pattern.id for pattern in patterns if pattern.status == "fail"]
    safety = _safety_flags()
    ok = not missing_patterns and not missing_state_classes and not failing_patterns and _safety_is_passive(safety)
    return WorkflowCoordinationCatalog(
        ok=ok,
        project_root=root,
        initialized=initialized,
        patterns=patterns,
        state_classes=state_classes,
        required_pattern_ids=list(REQUIRED_WORKFLOW_PATTERN_IDS),
        required_state_class_ids=list(REQUIRED_STATE_CLASS_IDS),
        summary={
            "pattern_count": len(patterns),
            "required_pattern_count": len(REQUIRED_WORKFLOW_PATTERN_IDS),
            "required_pattern_present_count": len(REQUIRED_WORKFLOW_PATTERN_IDS) - len(missing_patterns),
            "state_class_count": len(state_classes),
            "required_state_class_count": len(REQUIRED_STATE_CLASS_IDS),
            "required_state_class_present_count": len(REQUIRED_STATE_CLASS_IDS) - len(missing_state_classes),
            "pass": sum(1 for pattern in patterns if pattern.status == "pass"),
            "warning": sum(1 for pattern in patterns if pattern.status == "warning"),
            "fail": sum(1 for pattern in patterns if pattern.status == "fail"),
            "missing_required_pattern_count": len(missing_patterns),
            "missing_required_state_class_count": len(missing_state_classes),
        },
        safety=safety,
    )


def summarize_workflow_coordination(catalog: WorkflowCoordinationCatalog) -> dict[str, Any]:
    failing = [pattern.id for pattern in catalog.patterns if pattern.status == "fail"]
    warnings = [pattern.id for pattern in catalog.patterns if pattern.status == "warning"]
    status = "fail" if failing else "warning" if warnings else "pass"
    return {
        "schema_version": WORKFLOW_COORDINATION_SUMMARY_SCHEMA_VERSION,
        "ok": catalog.ok,
        "status": status,
        "initialized": catalog.initialized,
        "summary": dict(catalog.summary),
        "failing_pattern_ids": failing,
        "warning_pattern_ids": warnings,
        "pattern_ids": [pattern.id for pattern in catalog.patterns],
        "state_class_ids": [state_class.id for state_class in catalog.state_classes],
        "safety": dict(catalog.safety),
        "command": f"harness orchestration workflows --project {catalog.project_root} --output json",
    }


def _workflow_pattern_contracts(project_root: Path) -> list[WorkflowPatternContract]:
    parallel_signature = inspect.signature(run_objective_parallel)
    max_parallel = parallel_signature.parameters.get("max_parallel")
    has_parallel_bound = max_parallel is not None and max_parallel.default is not inspect.Signature.empty
    replay_audit = run_orchestration_replay_audit(project_root)
    replay_issue_codes = sorted(
        {
            code
            for case in replay_audit.cases
            if case.source_kind == "synthetic"
            for code in case.detected_issue_codes
        }
    )
    protocol_catalog = build_external_protocol_catalog(project_root)
    risky_protocols = [
        descriptor.id
        for descriptor in protocol_catalog.protocols
        if descriptor.category in {"extension", "agent_to_agent", "rpc"}
        and (
            descriptor.default_model_visible
            or descriptor.authority.process_start_allowed
            or descriptor.authority.network_allowed
            or descriptor.authority.agent_execution_allowed
            or (
                descriptor.status == "fail_closed"
                and (descriptor.runtime_enabled or descriptor.authority.tool_execution_allowed)
            )
        )
    ]
    memory_decision = decide_context_transmission(
        "hosted_model",
        source_kind="memory_record",
        trust_level="memory",
    )
    contracts = [
        WorkflowPatternContract(
            id="durable_supervisor",
            title="Durable supervisor with side-effect activities",
            status="pass" if callable(run_objective_autonomously) else "fail",
            execution_mode="local_control_plane",
            reference_patterns=["temporal", "dapr", "microsoft_agent_framework", "openai_agents"],
            harness_surfaces=[
                "objectives",
                "tasks",
                "task dependencies",
                "leases",
                "runs",
                "objective evidence",
            ],
            state_classes=["workflow_state", "artifact_state"],
            invariants=[
                "orchestrator state is persisted separately from adapter execution",
                "registered adapters own side effects behind leases and approvals",
                "operator progress is reconstructed from durable metadata",
            ],
            evidence={
                "runner_callable": callable(run_objective_autonomously),
                "checkpoint_evidence_callable": callable(verify_objective_evidence),
            },
            gaps=[] if callable(run_objective_autonomously) else ["Objective supervisor runner is unavailable."],
        ),
        WorkflowPatternContract(
            id="sequential_steps",
            title="Sequential objective step execution",
            status="pass" if callable(run_objective_autonomously) else "fail",
            execution_mode="deterministic_scheduler",
            reference_patterns=["google_adk", "microsoft_agent_framework", "langgraph"],
            harness_surfaces=["objective runner", "task dependencies", "objective lifecycle controls"],
            state_classes=["workflow_state"],
            invariants=[
                "created, paused, suspended, waiting-approval, and terminal objective states are explicit",
                "task dependency order is persisted before dispatch",
            ],
            evidence={"runner_callable": callable(run_objective_autonomously)},
        ),
        WorkflowPatternContract(
            id="bounded_parallel_fanout",
            title="Bounded fan-out/fan-in with batch barriers",
            status="pass" if has_parallel_bound else "fail",
            execution_mode="bounded_batch_scheduler",
            reference_patterns=["microsoft_agent_framework", "langgraph", "google_adk", "kairos", "lamas"],
            harness_surfaces=["run_objective_parallel", "objective batch plans", "orchestration replay"],
            state_classes=["workflow_state", "artifact_state"],
            invariants=[
                "parallel branches require an explicit max_parallel bound",
                "batch_planned, batch_started, and batch_completed evidence form the barrier contract",
                "slow branches must not let aggregation mark a batch complete without terminal task evidence",
            ],
            evidence={
                "has_max_parallel_bound": has_parallel_bound,
                "max_parallel_default": max_parallel.default if max_parallel is not None else None,
                "barrier_event_contracts": ["batch_planned", "batch_started", "batch_completed"],
                "slow_branch_replay_detected": "batch_completed_missing_terminal_task" in replay_issue_codes,
                "detected_replay_issue_codes": replay_issue_codes,
            },
            gaps=[] if has_parallel_bound else ["Parallel execution is missing an explicit max_parallel bound."],
        ),
        WorkflowPatternContract(
            id="typed_agent_handoff",
            title="Typed agent handoff envelope",
            status="pass" if callable(build_agent_handoff_envelope) else "fail",
            execution_mode="record_only_handoff",
            reference_patterns=["microsoft_agent_framework", "openai_agents", "google_adk", "a2a"],
            harness_surfaces=["session_child_task", "agent contracts", "handoff inspect command"],
            state_classes=["session_state", "workflow_state"],
            invariants=[
                "handoffs carry trace context and payload hashes",
                "handoffs do not grant adapter, tool, network, credential, model-context, or permission authority",
            ],
            evidence={"handoff_builder_callable": callable(build_agent_handoff_envelope)},
        ),
        WorkflowPatternContract(
            id="human_approval_pause",
            title="Human approval pause and resume gates",
            status="pass"
            if callable(create_objective_checkpoint)
            and callable(resolve_objective_checkpoint)
            and callable(evaluate_objective_checkpoint_gate)
            else "fail",
            execution_mode="durable_hitl_gate",
            reference_patterns=["microsoft_agent_framework", "langgraph", "openai_agents", "temporal"],
            harness_surfaces=["objective checkpoints", "approval records", "pending chat actions"],
            state_classes=["workflow_state", "artifact_state"],
            invariants=[
                "approval pauses are represented as persisted checkpoint evidence",
                "approval evidence narrows authority and never acts as provider or apply-back authority by itself",
            ],
            evidence={
                "create_checkpoint_callable": callable(create_objective_checkpoint),
                "resolve_checkpoint_callable": callable(resolve_objective_checkpoint),
                "evaluate_gate_callable": callable(evaluate_objective_checkpoint_gate),
            },
        ),
        WorkflowPatternContract(
            id="append_only_replay",
            title="Append-only replay and drift detection",
            status="pass" if replay_audit.ok and callable(replay_objective_event_log) else "fail",
            execution_mode="passive_event_reducer",
            reference_patterns=["temporal", "dapr", "langgraph", "microsoft_agent_framework", "opentelemetry"],
            harness_surfaces=["objective JSONL evidence", "orchestration replay audit", "trace export"],
            state_classes=["workflow_state", "artifact_state"],
            invariants=[
                "replay reduces event logs without re-executing side effects",
                "duplicate dispatch and incomplete barrier cases are detected as semantic drift",
            ],
            evidence={
                "replay_ok": replay_audit.ok,
                "synthetic_case_count": sum(1 for case in replay_audit.cases if case.source_kind == "synthetic"),
                "replay_issue_codes": replay_issue_codes,
                "trace_export_callables": {
                    "run": callable(export_run_trace),
                    "objective": callable(export_objective_trace),
                },
            },
            gaps=[] if replay_audit.ok else ["Synthetic orchestration replay cases are failing."],
        ),
        WorkflowPatternContract(
            id="external_protocol_boundary",
            title="External protocol adapter boundary",
            status="pass" if protocol_catalog.ok and not risky_protocols else "fail",
            execution_mode="compatibility_catalog_fail_closed",
            reference_patterns=["modelcontextprotocol", "a2a", "openapi", "grpc", "microsoft_agent_framework"],
            harness_surfaces=["external protocol catalog", "model-visible tool exposure policy", "schema contracts"],
            state_classes=["workflow_state", "artifact_state"],
            invariants=[
                "remote agent and tool protocols are compatibility metadata until authority is explicitly implemented",
                "protocol descriptors must not be model-visible or execution-enabled by default",
            ],
            evidence={
                "catalog_ok": protocol_catalog.ok,
                "protocol_ids": [descriptor.id for descriptor in protocol_catalog.protocols],
                "risky_protocol_ids": risky_protocols,
            },
            gaps=[] if protocol_catalog.ok and not risky_protocols else ["One or more external protocols are risky by default."],
        ),
        WorkflowPatternContract(
            id="memory_context_boundary",
            title="Memory context boundary",
            status="pass" if memory_decision.allowed is False and "memory_not_authority" in memory_decision.warnings else "fail",
            execution_mode="local_non_authority_context",
            reference_patterns=["owasp_agentic", "openai_agents", "microsoft_agent_framework"],
            harness_surfaces=["context policy", "local memory store", "agentic security controls"],
            state_classes=["memory_state", "session_state"],
            invariants=[
                "memory can inform local context but cannot grant authority",
                "hosted-model memory propagation remains denied unless a future governed path is implemented",
            ],
            evidence={
                "hosted_memory_allowed": memory_decision.allowed,
                "code": memory_decision.code,
                "reason": memory_decision.reason,
                "warnings": list(memory_decision.warnings),
            },
            gaps=[]
            if memory_decision.allowed is False and "memory_not_authority" in memory_decision.warnings
            else ["Memory context policy does not fail closed for hosted model transmission."],
        ),
    ]
    return contracts


def _state_classes() -> list[WorkflowStateClass]:
    return [
        WorkflowStateClass(
            id="session_state",
            title="Session state",
            owner_surface="sessions and session tools",
            durability="SQLite rows plus append-only session events",
            mutation_owner="session gateway",
            replay_source="session events and transcript health projections",
            authority_notes=[
                "Session messages are not tool, provider, filesystem, network, or permission authority.",
                "Model-visible tools are derived from policy projections, not raw session text.",
            ],
        ),
        WorkflowStateClass(
            id="workflow_state",
            title="Workflow state",
            owner_surface="objectives, tasks, leases, runs, and checkpoints",
            durability="SQLite control-plane rows plus objective JSONL evidence",
            mutation_owner="objective runner and daemon control plane",
            replay_source="objective evidence and orchestration replay audit",
            authority_notes=[
                "Workflow state records lifecycle and dispatch evidence; side effects remain behind adapters.",
                "Checkpoint approvals narrow the next step instead of granting broad future authority.",
            ],
        ),
        WorkflowStateClass(
            id="memory_state",
            title="Long-term memory state",
            owner_surface="explicit local memory",
            durability="local SQLite memory records",
            mutation_owner="memory commands and governed memory tools",
            replay_source=None,
            model_context_allowed_by_default=False,
            authority_notes=[
                "Memory is treated as untrusted context and never as approval, permission, provider, or tool authority.",
                "Hosted memory transmission is denied by default by the context policy.",
            ],
        ),
        WorkflowStateClass(
            id="artifact_state",
            title="Artifact and evidence state",
            owner_surface="artifacts, traces, governance evidence, reference metadata",
            durability="project-local evidence files and metadata projections",
            mutation_owner="registered evidence writers and governance commands",
            replay_source="objective evidence, checkpoint evidence, trace exports, and governance evidence",
            authority_notes=[
                "Artifact bodies are not read by default readiness projections.",
                "Reference repositories remain design evidence and are not imported or executed.",
            ],
        ),
    ]


def _safety_flags() -> dict[str, bool]:
    return {
        "read_only": True,
        "metadata_only": True,
        "reference_code_imported": False,
        "reference_contents_included": False,
        "provider_called": False,
        "network_called": False,
        "adapter_execution_started": False,
        "tool_execution_started": False,
        "agent_execution_started": False,
        "process_started": False,
        "filesystem_modified": False,
        "permission_granting": False,
        "credential_accessed": False,
        "artifact_bodies_read": False,
        "model_context_allowed": False,
    }


def _safety_is_passive(safety: dict[str, bool]) -> bool:
    return safety.get("read_only") is True and all(
        safety.get(key) is False
        for key in (
            "reference_code_imported",
            "reference_contents_included",
            "provider_called",
            "network_called",
            "adapter_execution_started",
            "tool_execution_started",
            "agent_execution_started",
            "process_started",
            "filesystem_modified",
            "permission_granting",
            "credential_accessed",
            "artifact_bodies_read",
            "model_context_allowed",
        )
    )
