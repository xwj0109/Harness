from __future__ import annotations

import inspect
import shlex
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from harness.agent_contracts import AGENT_CONTRACT_SCHEMA_VERSION
from harness.agent_discovery import (
    AGENT_DISCOVERY_CATALOG_SCHEMA_VERSION,
    DELEGATE_ALLOCATION_SCHEMA_VERSION,
    build_agent_discovery_catalog,
    evaluate_delegate_allocation,
)
from harness.agent_handoff import AGENT_HANDOFF_ENVELOPE_SCHEMA_VERSION, build_agent_handoff_envelope
from harness.config import HARNESS_DIR
from harness.context_policy import decide_context_transmission
from harness.delegate_budgets import adapter_delegate_budget_projection
from harness.execution import (
    SESSION_CHILD_TASK_EXECUTION_ADAPTER,
    SESSION_DELEGATE_TASK_TYPE,
    list_execution_adapter_descriptors,
    runtime_control_matches_descriptor,
    validate_execution_task_payload,
)
from harness.external_protocols import build_external_protocol_catalog
from harness.governance.gate_registry import gate_registry_payload
from harness.governance.reference_repositories import build_reference_repositories_audit
from harness.memory.sqlite_store import SQLiteStore, TASK_REPLAY_RECEIPT_SCHEMA_VERSION
from harness.models import (
    TRACE_CONTEXT_PROPAGATION,
    TRACE_SEMANTIC_CONVENTIONS,
    ExecutionAdapterDescriptor,
    KillSwitchRecord,
    ObjectiveStatus,
    TaskRecord,
    TaskStatus,
    utc_now,
)
from harness.objective_batch_plan import OBJECTIVE_BATCH_PLAN_SCHEMA_VERSION
from harness.objective_checkpoints import (
    evaluate_objective_checkpoint_gate,
    list_objective_checkpoints,
    verify_objective_checkpoint_evidence,
)
from harness.objective_evidence import verify_objective_evidence
from harness.objective_runner import run_objective_autonomously, run_objective_parallel
from harness.orchestration_replay import run_orchestration_replay_audit, summarize_orchestration_replay
from harness.orchestration_scenarios import build_orchestration_scenario_catalog, compact_scenario_rows
from harness.orchestration_state import OrchestrationStateSnapshot, load_orchestration_state
from harness.orchestration_workflows import build_workflow_coordination_catalog
from harness.paths import resolve_project_root
from harness.pending_chat_actions import (
    PENDING_CHAT_ACTION_METADATA_KEY,
    PENDING_CHAT_ACTION_SCHEMA_VERSION,
    clear_pending_chat_action_metadata,
    pending_chat_action_audit,
)
from harness.progress import build_orchestration_progress
from harness.sandbox_profiles import get_sandbox_profile
from harness.schema_contracts import build_schema_contract_catalog
from harness.security import sanitize_for_logging
from harness.session_tools import model_visible_session_tool_ids, session_tool_catalog_projection
from harness.task_operator_bridge import (
    SESSION_OPERATOR_DEFAULT_ALLOWED_TOOLS,
    default_session_operator_tool_ids,
)
from harness.traces import export_objective_trace, export_run_trace, to_otel_json


ORCHESTRATION_READINESS_AUDIT_SCHEMA_VERSION = "harness.orchestration_readiness_audit/v1"
ORCHESTRATION_READINESS_CHECK_SCHEMA_VERSION = "harness.orchestration_readiness_check/v1"
ORCHESTRATION_READINESS_SUMMARY_SCHEMA_VERSION = "harness.orchestration_readiness_summary/v1"

ReadinessStatus = Literal["pass", "warning", "fail", "skipped"]

REFERENCE_SYSTEMS = [
    "microsoft_agent_framework",
    "langgraph",
    "temporal",
    "dapr",
    "openai_agents",
    "google_adk",
    "opentelemetry",
]

REFERENCE_PATTERNS_BY_CHECK = {
    "agentic_security_controls": [
        "microsoft_agent_framework",
        "modelcontextprotocol",
        "A2A",
        "temporal",
        "dapr",
        "opentelemetry",
    ],
    "agent_discovery_and_allocation": ["contract_net", "A2A", "microsoft_agent_framework", "google_adk"],
    "append_only_objective_evidence": ["temporal", "langgraph", "opentelemetry"],
    "applyback_governance": ["temporal", "microsoft_agent_framework"],
    "bounded_parallel_scheduler": ["temporal", "langgraph", "dapr"],
    "budget_limited_delegation": ["microsoft_agent_framework", "google_adk", "openai_agents"],
    "durable_supervisor_state": ["temporal", "langgraph", "microsoft_agent_framework"],
    "objective_lifecycle_controls": ["microsoft_agent_framework", "temporal", "langgraph"],
    "orchestration_scenario_conformance": [
        "microsoft_agent_framework",
        "temporal",
        "langgraph",
        "opentelemetry",
        "owasp_agentic",
    ],
    "otel_trace_export": ["opentelemetry", "temporal"],
    "pending_chat_action_recovery": ["microsoft_agent_framework", "temporal", "openai_agents"],
    "progress_observability": ["microsoft_agent_framework", "openai_agents", "opentelemetry"],
    "external_protocol_compatibility": ["modelcontextprotocol", "A2A", "microsoft_agent_framework"],
    "protocol_and_tool_exposure": ["openai_agents", "google_adk", "microsoft_agent_framework"],
    "reference_repository_hygiene": ["microsoft_agent_framework", "langgraph", "google_adk"],
    "replay_drift_detection": ["temporal", "dapr", "langgraph", "microsoft_agent_framework"],
    "runtime_controls_and_breakers": ["dapr", "temporal", "microsoft_agent_framework"],
    "schema_compatibility_contracts": ["microsoft_agent_framework", "temporal", "langgraph", "opentelemetry"],
    "sandboxed_registered_adapters": ["openai_agents", "microsoft_agent_framework"],
    "supervisor_checkpoints": ["microsoft_agent_framework", "temporal", "langgraph"],
    "typed_task_delegation": ["microsoft_agent_framework", "google_adk", "openai_agents"],
    "workflow_coordination_contracts": ["microsoft_agent_framework", "temporal", "langgraph", "google_adk"],
}


class OrchestrationReadinessCheck(BaseModel):
    schema_version: str = ORCHESTRATION_READINESS_CHECK_SCHEMA_VERSION
    id: str
    status: ReadinessStatus
    message: str
    reference_patterns: list[str] = Field(default_factory=list)
    evidence: dict[str, Any] = Field(default_factory=dict)
    gaps: list[str] = Field(default_factory=list)
    next_actions: list[str] = Field(default_factory=list)


class OrchestrationReadinessAudit(BaseModel):
    schema_version: str = ORCHESTRATION_READINESS_AUDIT_SCHEMA_VERSION
    ok: bool
    project_root: Path
    initialized: bool
    reference_root: Path | None = None
    reference_systems: list[str] = Field(default_factory=lambda: list(REFERENCE_SYSTEMS))
    safety: dict[str, bool] = Field(default_factory=dict)
    summary: dict[str, int] = Field(default_factory=dict)
    checks: list[OrchestrationReadinessCheck] = Field(default_factory=list)


def summarize_orchestration_readiness(audit: OrchestrationReadinessAudit) -> dict[str, Any]:
    failing = [check.id for check in audit.checks if check.status == "fail"]
    warnings = [check.id for check in audit.checks if check.status == "warning"]
    skipped = [check.id for check in audit.checks if check.status == "skipped"]
    status = "fail" if failing else "warning" if warnings else "pass"
    next_action = (
        f"harness orchestration audit --project {audit.project_root} --output json"
        if status == "pass"
        else f"harness orchestration audit --project {audit.project_root} --output json"
    )
    return {
        "schema_version": ORCHESTRATION_READINESS_SUMMARY_SCHEMA_VERSION,
        "ok": audit.ok,
        "status": status,
        "initialized": audit.initialized,
        "summary": dict(audit.summary),
        "failing_check_ids": failing,
        "warning_check_ids": warnings,
        "skipped_check_ids": skipped,
        "reference_root": str(audit.reference_root) if audit.reference_root is not None else None,
        "reference_systems": list(audit.reference_systems),
        "safety": dict(audit.safety),
        "next_action": next_action,
        "command": f"harness orchestration audit --project {audit.project_root} --output json",
    }


def run_orchestration_readiness_audit(
    project_root: Path,
    *,
    reference_root: Path | None = None,
    include_references: bool = True,
) -> OrchestrationReadinessAudit:
    """Evaluate Harness orchestration architecture without granting or executing authority."""

    project_root = resolve_project_root(project_root)
    snapshot = load_orchestration_state(project_root)
    typed_task_delegation = _typed_task_delegation_check(project_root)
    bounded_parallel_scheduler = _bounded_parallel_scheduler_check()
    replay_audit = run_orchestration_replay_audit(project_root)
    replay_drift_detection = _replay_drift_detection_check(project_root, replay_audit)
    runtime_controls_and_breakers = _runtime_controls_and_breakers_check(project_root, snapshot)
    external_protocol_compatibility = _external_protocol_compatibility_check(project_root)
    schema_compatibility_contracts = _schema_compatibility_contracts_check(project_root)
    protocol_and_tool_exposure = _protocol_and_tool_exposure_check(project_root)
    applyback_governance = _applyback_governance_check()
    workflow_coordination_contracts = _workflow_coordination_contracts_check(project_root)
    orchestration_scenario_conformance = _orchestration_scenario_conformance_check(project_root)
    checks = [
        _durable_supervisor_state_check(snapshot),
        _objective_lifecycle_controls_check(project_root),
        typed_task_delegation,
        _agent_discovery_and_allocation_check(project_root),
        _supervisor_checkpoints_check(project_root, snapshot),
        bounded_parallel_scheduler,
        workflow_coordination_contracts,
        _append_only_objective_evidence_check(project_root, snapshot),
        _otel_trace_export_check(project_root, snapshot),
        replay_drift_detection,
        orchestration_scenario_conformance,
        _pending_chat_action_recovery_check(project_root, snapshot),
        _budget_limited_delegation_check(),
        _sandboxed_registered_adapters_check(),
        runtime_controls_and_breakers,
        _progress_observability_check(project_root, snapshot),
        external_protocol_compatibility,
        schema_compatibility_contracts,
        protocol_and_tool_exposure,
        applyback_governance,
        _agentic_security_controls_check(
            typed_task_delegation=typed_task_delegation,
            bounded_parallel_scheduler=bounded_parallel_scheduler,
            replay_drift_detection=replay_drift_detection,
            runtime_controls_and_breakers=runtime_controls_and_breakers,
            external_protocol_compatibility=external_protocol_compatibility,
            schema_compatibility_contracts=schema_compatibility_contracts,
            protocol_and_tool_exposure=protocol_and_tool_exposure,
            applyback_governance=applyback_governance,
        ),
    ]
    checks.append(_reference_repository_hygiene_check(project_root, reference_root, include_references))
    checks.sort(key=lambda item: item.id)
    summary = _summary(checks)
    resolved_reference_root = None
    if include_references:
        resolved_reference_root = (
            project_root.with_name(f"{project_root.name}-references")
            if reference_root is None
            else Path(reference_root).expanduser().resolve()
        )
    return OrchestrationReadinessAudit(
        ok=summary["fail"] == 0,
        project_root=project_root,
        initialized=snapshot.initialized,
        reference_root=resolved_reference_root,
        safety=_safety_flags(),
        summary=summary,
        checks=checks,
    )


def _durable_supervisor_state_check(snapshot: OrchestrationStateSnapshot) -> OrchestrationReadinessCheck:
    evidence = _snapshot_counts(snapshot)
    if not snapshot.ok:
        return _check(
            "durable_supervisor_state",
            "fail",
            "Durable orchestration state could not be loaded.",
            evidence={**evidence, "error": snapshot.error},
            gaps=["SQLite-backed objective/task state is not currently readable."],
            next_actions=["Run `harness integrity check --output json` and inspect the local SQLite error."],
        )
    return _check(
        "durable_supervisor_state",
        "pass",
        "Durable objective, task, dependency, lease, run, and event state is inspectable.",
        evidence=evidence,
    )


def _objective_lifecycle_controls_check(project_root: Path) -> OrchestrationReadinessCheck:
    store_source = inspect.getsource(SQLiteStore.update_objective_status)
    create_source = inspect.getsource(SQLiteStore.create_objective)
    runner_source = inspect.getsource(run_objective_autonomously) + inspect.getsource(run_objective_parallel)
    progress_module = __import__("harness.progress").progress
    progress_source = "".join(
        inspect.getsource(member)
        for member in (
            build_orchestration_progress,
            progress_module._task_progress,
            progress_module._mode_for_progress,
            progress_module._next_action_for_progress,
            progress_module._equivalent_commands,
        )
    )
    store_source_path = inspect.getsourcefile(SQLiteStore)
    store_module_source = Path(store_source_path).read_text(encoding="utf-8") if store_source_path is not None else store_source
    cli_path = (
        Path(store_source_path).resolve().parents[1] / "cli" / "main.py"
        if store_source_path is not None
        else project_root / "src" / "harness" / "cli" / "main.py"
    )
    cli_source = cli_path.read_text(encoding="utf-8") if cli_path.exists() else ""
    evidence = {
        "store_update_method": "update_objective_status",
        "store_create_draft_objective": "status: str | ObjectiveStatus" in create_source
        and "ObjectiveStatus.CREATED" in create_source,
        "stores_lifecycle_events": "lifecycle_events" in store_source,
        "validates_transitions": "validate_objective_transition" in store_source,
        "waiting_approval_status": "ObjectiveStatus.WAITING_APPROVAL" in store_module_source
        and "waiting_approval" in inspect.getsource(ObjectiveStatus),
        "retrying_status": "ObjectiveStatus.RETRYING" in store_module_source
        and "retrying" in inspect.getsource(ObjectiveStatus),
        "store_retry_method": "def retry_objective" in store_module_source
        and "retry_task" in store_module_source,
        "runner_blocks_inactive_objectives": "objective.status != ObjectiveStatus.ACTIVE" in runner_source
        and "objective_inactive" in runner_source,
        "runner_marks_checkpoint_waiting_approval": "_mark_objective_waiting_approval" in runner_source
        and "checkpoint_blocked" in runner_source,
        "progress_terminalizes_inactive_objectives": "TERMINAL_OBJECTIVE_STATUSES" in progress_source,
        "progress_blocks_created_objectives": "ObjectiveStatus.CREATED" in progress_source
        and "CREATED_OBJECTIVE_REASON" in progress_source
        and "objectives start" in progress_source,
        "progress_blocks_suspended_objectives": "ObjectiveStatus.SUSPENDED" in progress_source
        and "objectives resume" in progress_source,
        "cli_add_draft_option": "--draft" in cli_source
        and "ObjectiveStatus.CREATED if draft else ObjectiveStatus.ACTIVE" in cli_source,
        "cli_start_command": "def objectives_start" in cli_source,
        "progress_blocks_waiting_approval_objectives": "ObjectiveStatus.WAITING_APPROVAL" in progress_source
        and "objectives checkpoints gate" in progress_source,
        "progress_blocks_retrying_objectives": "ObjectiveStatus.RETRYING" in progress_source
        and "RETRYING_OBJECTIVE_REASON" in progress_source
        and "objectives resume" in progress_source,
        "cli_cancel_command": "def objectives_cancel" in cli_source,
        "cli_complete_command": "def objectives_complete" in cli_source,
        "cli_suspend_command": "def objectives_suspend" in cli_source,
        "cli_resume_command": "def objectives_resume" in cli_source,
        "cli_timeout_command": "def objectives_timeout" in cli_source,
        "cli_retry_command": "def objectives_retry" in cli_source,
        "commands": [
            f"harness objectives add --title <title> --draft --project {project_root} --output json",
            f"harness objectives start <objective_id> --project {project_root} --output json",
            f"harness objectives suspend <objective_id> --project {project_root} --output json",
            f"harness objectives resume <objective_id> --project {project_root} --output json",
            f"harness objectives cancel <objective_id> --project {project_root} --output json",
            f"harness objectives complete <objective_id> --project {project_root} --output json",
            f"harness objectives timeout <objective_id> --project {project_root} --output json",
            f"harness objectives retry <objective_id> --project {project_root} --output json",
            f"harness progress --objective <objective_id> --project {project_root} --output json",
        ],
    }
    gaps: list[str] = []
    if not evidence["stores_lifecycle_events"]:
        gaps.append("Objective status changes are not persisted with lifecycle metadata.")
    if not evidence["store_create_draft_objective"]:
        gaps.append("Objectives cannot be created in a non-dispatchable draft/created state.")
    if not evidence["validates_transitions"]:
        gaps.append("Objective status changes are not transition-validated.")
    if not evidence["waiting_approval_status"]:
        gaps.append("Objective waiting-approval state is not represented in the lifecycle enum and transition table.")
    if not evidence["retrying_status"] or not evidence["store_retry_method"]:
        gaps.append("Objective retry is not represented as a durable lifecycle action.")
    if not evidence["runner_blocks_inactive_objectives"]:
        gaps.append("Objective runners can dispatch inactive objectives.")
    if not evidence["runner_marks_checkpoint_waiting_approval"]:
        gaps.append("Objective runners do not serialize checkpoint approval waits in objective lifecycle state.")
    if not evidence["progress_terminalizes_inactive_objectives"]:
        gaps.append("Progress can advertise dispatch for inactive objectives.")
    if not evidence["progress_blocks_created_objectives"]:
        gaps.append("Progress does not block created objectives with a start path.")
    if not evidence["cli_add_draft_option"]:
        gaps.append("Objective draft creation is missing from the CLI.")
    if not evidence["progress_blocks_suspended_objectives"]:
        gaps.append("Progress does not block suspended objectives with a resume path.")
    if not evidence["progress_blocks_waiting_approval_objectives"]:
        gaps.append("Progress does not block waiting-approval objectives with checkpoint actions.")
    if not evidence["progress_blocks_retrying_objectives"]:
        gaps.append("Progress does not block retrying objectives before dispatch.")
    if not evidence["cli_start_command"]:
        gaps.append("Objective start command is missing from the CLI.")
    if not evidence["cli_cancel_command"] or not evidence["cli_complete_command"]:
        gaps.append("Objective lifecycle commands are missing from the CLI.")
    if not evidence["cli_suspend_command"] or not evidence["cli_resume_command"]:
        gaps.append("Objective suspend/resume commands are missing from the CLI.")
    if not evidence["cli_timeout_command"]:
        gaps.append("Objective timeout command is missing from the CLI.")
    if not evidence["cli_retry_command"]:
        gaps.append("Objective retry command is missing from the CLI.")
    return _check(
        "objective_lifecycle_controls",
        "pass" if not gaps else "fail",
        "Objective lifecycle controls are explicit, durable, and enforced before dispatch.",
        evidence=evidence,
        gaps=gaps,
        next_actions=[] if not gaps else ["Restore objective lifecycle store, CLI, progress, and runner guards."],
    )


def _typed_task_delegation_check(project_root: Path) -> OrchestrationReadinessCheck:
    descriptors = {descriptor.id: descriptor for descriptor in list_execution_adapter_descriptors()}
    descriptor = descriptors.get(SESSION_CHILD_TASK_EXECUTION_ADAPTER)
    validation_errors = validate_execution_task_payload(
        execution_adapter=SESSION_CHILD_TASK_EXECUTION_ADAPTER,
        task_type=SESSION_DELEGATE_TASK_TYPE,
        metadata={
            "execution_adapter": SESSION_CHILD_TASK_EXECUTION_ADAPTER,
            "task_type": SESSION_DELEGATE_TASK_TYPE,
        },
    )
    if descriptor is None:
        return _check(
            "typed_task_delegation",
            "fail",
            "The session child-task delegation adapter is not registered.",
            evidence={"registered_adapter_ids": sorted(descriptors)},
            gaps=["Delegated tasks do not have a typed record-only adapter contract."],
            next_actions=["Register the session_child_task adapter before enabling child-task orchestration."],
        )
    synthetic_task = TaskRecord(
        id="task_readiness_handoff",
        title="Readiness handoff",
        description="Validate the typed handoff envelope.",
        status=TaskStatus.READY,
        project_root=project_root,
        created_at=utc_now(),
        updated_at=utc_now(),
        agent_id="repo_inspector",
        workbench_id="coding",
        idempotency_key="task_idem_readiness_handoff",
        session_id="sess_child_readiness",
        metadata={
            "schema_version": "harness.session_tool_task_metadata/v1",
            "task_type": SESSION_DELEGATE_TASK_TYPE,
            "execution_adapter": SESSION_CHILD_TASK_EXECUTION_ADAPTER,
            "execution_started": False,
            "hidden_process_started": False,
            "parent_session_id": "sess_parent_readiness",
            "child_session_id": "sess_child_readiness",
            "source_tool_run_id": "run_readiness_handoff",
            "allowed_tools": ["read", "glob", "grep"],
            "boundary": "read_only_project",
            "output_expectation": "Readiness envelope only.",
        },
    )
    handoff = build_agent_handoff_envelope(project_root, synthetic_task)
    handoff_authority = handoff.authority.model_dump(mode="json")
    unsafe_handoff_authority = sorted(
        key
        for key in (
            "adapter_execution_allowed",
            "process_start_allowed",
            "network_allowed",
            "tool_execution_allowed",
            "agent_execution_allowed",
            "filesystem_mutation_allowed",
            "model_context_allowed",
            "credential_access_allowed",
            "permission_granting",
        )
        if handoff_authority.get(key) is not False
    )
    evidence = {
        "adapter_id": descriptor.id,
        "supported_task_types": descriptor.supported_task_types,
        "required_task_metadata": descriptor.required_task_metadata,
        "autonomy_default": descriptor.autonomy_default,
        "output_contracts": descriptor.output_contracts,
        "validation_errors": validation_errors,
        "handoff_schema_version": handoff.schema_version,
        "handoff_expected_schema_version": AGENT_HANDOFF_ENVELOPE_SCHEMA_VERSION,
        "handoff_ok": handoff.ok,
        "handoff_validation_errors": handoff.validation_errors,
        "handoff_traceparent": handoff.trace_context.traceparent,
        "handoff_payload_sha256": handoff.integrity.payload_sha256,
        "handoff_authority": handoff_authority,
        "unsafe_handoff_authority": unsafe_handoff_authority,
        "agent_contract_schema_version": handoff.agent_contract.schema_version,
        "agent_contract_expected_schema_version": AGENT_CONTRACT_SCHEMA_VERSION,
        "agent_contract_ok": handoff.agent_contract.ok,
        "agent_contract_id": handoff.agent_contract.contract_id,
        "agent_contract_sha256": handoff.agent_contract.contract_sha256,
        "agent_contract_source_kind": handoff.agent_contract.source_kind,
        "agent_contract_tool_policy": handoff.agent_contract.tool_policy.model_dump(mode="json"),
        "agent_contract_budget_policy": handoff.agent_contract.budget_policy.model_dump(mode="json"),
        "agent_contract_trace_policy": handoff.agent_contract.trace_policy.model_dump(mode="json"),
        "agent_contract_validation_errors": handoff.agent_contract.validation_errors,
    }
    ok = (
        not validation_errors
        and descriptor.autonomy_default == "forbidden"
        and AGENT_HANDOFF_ENVELOPE_SCHEMA_VERSION in descriptor.output_contracts
        and handoff.ok
        and handoff.agent_contract.ok
        and handoff.agent_contract.schema_version == AGENT_CONTRACT_SCHEMA_VERSION
        and not unsafe_handoff_authority
    )
    return _check(
        "typed_task_delegation",
        "pass" if ok else "fail",
        "Delegated child tasks use a typed, record-only adapter, handoff envelope, and agent identity contract."
        if ok
        else "Delegated child-task metadata validation is incomplete.",
        evidence=evidence,
        gaps=[] if ok else ["Child-task delegation may be dispatchable, schema-loose, or identity-loose."],
        next_actions=[]
        if ok
        else ["Keep session_child_task record-only and validate handoff and agent contracts before task creation."],
    )


def _agent_discovery_and_allocation_check(project_root: Path) -> OrchestrationReadinessCheck:
    catalog = build_agent_discovery_catalog(project_root, workbench_id="coding")
    allocation = evaluate_delegate_allocation(
        project_root,
        workbench_id="coding",
        task_type="security_review",
        required_kind="reviewer",
        required_tags=["security"],
        required_tool_policy_id="read_only",
        max_candidates=1,
        cards=catalog.cards,
    )
    cards_by_id = {card.agent_id: card for card in catalog.cards}
    selected_ids = list(allocation.selected_agent_ids)
    selected_bids = [bid for bid in allocation.bids if bid.bid_id in set(allocation.selected_bid_ids)]
    catalog_safety_issues = _passive_metadata_safety_issues(catalog.safety)
    allocation_safety_issues = _passive_metadata_safety_issues(allocation.safety)
    card_safety_issues = sorted(
        f"{card.agent_id}:{issue}"
        for card in catalog.cards
        for issue in _passive_metadata_safety_issues(card.safety)
    )
    bid_safety_issues = sorted(
        f"{bid.agent_id}:{issue}"
        for bid in allocation.bids
        for issue in _passive_metadata_safety_issues(bid.safety)
    )
    announcement_authority = allocation.announcement.authority
    unsafe_announcement_authority = sorted(
        key
        for key in (
            "task_record_creation_allowed",
            "agent_execution_allowed",
            "tool_execution_allowed",
            "permission_granting",
            "budget_granting",
        )
        if announcement_authority.get(key) is not False
    )
    unsafe_card_authority = sorted(
        f"{card.agent_id}:{key}"
        for card in catalog.cards
        for key in (
            "identity_authority",
            "orchestration_policy_authority",
            "budget_authority",
            "adapter_execution_allowed",
            "agent_execution_allowed",
            "model_execution_allowed",
            "tool_execution_allowed",
            "process_start_allowed",
            "network_allowed",
            "filesystem_mutation_allowed",
            "credential_access_allowed",
            "permission_granting",
            "model_context_allowed",
        )
        if card.authority.get(key) is not False
    )
    selected_security_reviewer = cards_by_id.get("security_reviewer")
    gaps: list[str] = []
    if catalog.schema_version != AGENT_DISCOVERY_CATALOG_SCHEMA_VERSION:
        gaps.append("Agent discovery catalog schema version drifted.")
    if allocation.schema_version != DELEGATE_ALLOCATION_SCHEMA_VERSION:
        gaps.append("Delegate allocation schema version drifted.")
    if not catalog.ok:
        gaps.append("Agent discovery catalog did not validate.")
    if not allocation.ok:
        gaps.append("Delegate allocation did not select an eligible agent.")
    if selected_ids != ["security_reviewer"]:
        gaps.append("Security review delegation no longer selects the security_reviewer profile deterministically.")
    if selected_security_reviewer is None:
        gaps.append("The coding workbench discovery catalog is missing security_reviewer.")
    elif selected_security_reviewer.kind != "reviewer" or selected_security_reviewer.tool_policy_id != "read_only":
        gaps.append("The selected security reviewer is not a read-only reviewer profile.")
    if catalog_safety_issues or allocation_safety_issues or card_safety_issues or bid_safety_issues:
        gaps.append("Agent discovery or allocation projections are no longer passive metadata.")
    if unsafe_announcement_authority or unsafe_card_authority:
        gaps.append("Agent discovery or allocation metadata grants runtime authority.")
    evidence = {
        "catalog_schema_version": catalog.schema_version,
        "allocation_schema_version": allocation.schema_version,
        "catalog_ok": catalog.ok,
        "allocation_ok": allocation.ok,
        "workbench_id": catalog.workbench_id,
        "card_count": len(catalog.cards),
        "discoverable_count": catalog.summary.get("discoverable_count"),
        "agent_ids": sorted(cards_by_id),
        "selected_agent_ids": selected_ids,
        "eligible_count": allocation.summary.get("eligible_count"),
        "sample_allocation_selected_agent_ids": []
        if catalog.sample_allocation is None
        else list(catalog.sample_allocation.selected_agent_ids),
        "announcement": allocation.announcement.model_dump(mode="json"),
        "selected_bids": [bid.model_dump(mode="json") for bid in selected_bids],
        "catalog_safety_issues": catalog_safety_issues,
        "allocation_safety_issues": allocation_safety_issues,
        "card_safety_issues": card_safety_issues,
        "bid_safety_issues": bid_safety_issues,
        "unsafe_announcement_authority": unsafe_announcement_authority,
        "unsafe_card_authority": unsafe_card_authority,
        "catalog_safety": dict(catalog.safety),
        "allocation_safety": dict(allocation.safety),
        "commands": [
            f"harness agents discover --project {project_root} --workbench coding --output json",
            (
                f"harness agents allocate --project {project_root} --workbench coding "
                "--task-type security_review --required-kind reviewer --required-tag security "
                "--required-tool-policy read_only --max-candidates 1 --output json"
            ),
        ],
    }
    ok = not gaps
    return _check(
        "agent_discovery_and_allocation",
        "pass" if ok else "fail",
        "Agent discovery exposes passive local cards and deterministic read-only delegate allocation."
        if ok
        else "Agent discovery or delegate allocation is missing, non-deterministic, or authority-bearing.",
        evidence=evidence,
        gaps=gaps,
        next_actions=[]
        if ok
        else ["Restore metadata-only discovery cards and deterministic Contract-Net-style bid selection."],
    )


def _supervisor_checkpoints_check(
    project_root: Path,
    snapshot: OrchestrationStateSnapshot,
) -> OrchestrationReadinessCheck:
    gate_ids = {str(gate.get("id")) for gate in gate_registry_payload().get("gates", []) if isinstance(gate, dict)}
    if "checkpoint_approved" not in gate_ids:
        return _check(
            "supervisor_checkpoints",
            "fail",
            "The checkpoint_approved governance gate is missing.",
            evidence={"gate_ids": sorted(gate_ids)},
            gaps=["Supervisor checkpoints are not represented in governance gate metadata."],
            next_actions=["Add checkpoint_approved to the governance gate registry."],
        )
    if not snapshot.initialized:
        return _check(
            "supervisor_checkpoints",
            "pass",
            "Supervisor checkpoint gate support is registered; no runtime state was inspected.",
            evidence={"initialized": False, "gate_id": "checkpoint_approved"},
        )
    try:
        gates = [evaluate_objective_checkpoint_gate(project_root, objective.id) for objective in snapshot.objectives]
        verifications = [verify_objective_checkpoint_evidence(project_root, objective.id) for objective in snapshot.objectives]
    except Exception as exc:
        return _check(
            "supervisor_checkpoints",
            "fail",
            "Supervisor checkpoint gates or evidence could not be evaluated.",
            evidence={"error": f"{exc.__class__.__name__}: {exc}"},
            gaps=["Checkpoint gate projection is not reliable for existing objectives."],
            next_actions=["Run `harness objectives checkpoints gate <objective_id> --output json` for the failing objective."],
        )
    failed_verifications = [verification for verification in verifications if not verification.ok]
    if failed_verifications:
        failed_objective_ids = [verification.objective_id for verification in failed_verifications]
        return _check(
            "supervisor_checkpoints",
            "fail",
            "One or more objective checkpoint evidence chains failed verification.",
            evidence={
                "objective_count": len(snapshot.objectives),
                "gate_id": "checkpoint_approved",
                "required_checkpoint_count": sum(gate.required_checkpoint_count for gate in gates),
                "blocked_objective_count": sum(1 for gate in gates if not gate.ok),
                "pending_checkpoint_count": sum(len(gate.pending_checkpoint_ids) for gate in gates),
                "rejected_checkpoint_count": sum(len(gate.rejected_checkpoint_ids) for gate in gates),
                "checkpoint_evidence_verified": [
                    {
                        "objective_id": verification.objective_id,
                        "ok": verification.ok,
                        "summary": verification.summary,
                        "failed_check_ids": [check.id for check in verification.checks if check.status == "fail"],
                    }
                    for verification in verifications
                ],
            },
            gaps=[f"Checkpoint evidence failed verification: {', '.join(failed_objective_ids)}"],
            next_actions=[
                "Run `harness objectives checkpoints verify <objective_id> --output json` for each failing objective."
            ],
        )
    return _check(
        "supervisor_checkpoints",
        "pass",
        "Supervisor checkpoint gates are registered and evaluable.",
        evidence={
            "objective_count": len(snapshot.objectives),
            "gate_id": "checkpoint_approved",
            "required_checkpoint_count": sum(gate.required_checkpoint_count for gate in gates),
            "blocked_objective_count": sum(1 for gate in gates if not gate.ok),
            "pending_checkpoint_count": sum(len(gate.pending_checkpoint_ids) for gate in gates),
            "rejected_checkpoint_count": sum(len(gate.rejected_checkpoint_ids) for gate in gates),
            "checkpoint_evidence_verified_count": len(verifications),
            "checkpoint_evidence_failed_count": 0,
        },
    )


def _bounded_parallel_scheduler_check() -> OrchestrationReadinessCheck:
    signature = inspect.signature(run_objective_parallel)
    parameters = signature.parameters
    max_parallel = parameters.get("max_parallel")
    has_parallel_bound = max_parallel is not None and max_parallel.default is not inspect.Signature.empty
    evidence = {
        "callable": callable(run_objective_parallel),
        "has_max_parallel_bound": has_parallel_bound,
        "max_parallel_default": max_parallel.default if max_parallel is not None else None,
        "event_contracts": ["batch_planned", "batch_started", "batch_completed"],
    }
    return _check(
        "bounded_parallel_scheduler",
        "pass" if has_parallel_bound else "fail",
        "The objective scheduler exposes bounded parallel execution and batch evidence contracts."
        if has_parallel_bound
        else "The objective scheduler does not expose a bounded max_parallel control.",
        evidence=evidence,
        gaps=[] if has_parallel_bound else ["Parallel dispatch cannot be deterministically bounded."],
        next_actions=[] if has_parallel_bound else ["Add an explicit max_parallel bound to the objective scheduler."],
    )


def _append_only_objective_evidence_check(
    project_root: Path,
    snapshot: OrchestrationStateSnapshot,
) -> OrchestrationReadinessCheck:
    if not snapshot.initialized:
        return _check(
            "append_only_objective_evidence",
            "pass",
            "Objective evidence verification is available; no runtime state was inspected.",
            evidence={"initialized": False},
        )
    verified: list[dict[str, Any]] = []
    failures: list[str] = []
    objectives_with_run_evidence = {
        run.objective_id
        for run in snapshot.runs
        if isinstance(run.objective_id, str) and run.objective_id
    }
    objectives_missing_evidence: list[str] = []
    for objective in snapshot.objectives:
        evidence_path = _objective_evidence_path(project_root, objective.id)
        if not evidence_path.exists():
            if objective.id in objectives_with_run_evidence:
                objectives_missing_evidence.append(objective.id)
            continue
        verification = verify_objective_evidence(project_root, objective.id, evidence_path=evidence_path)
        verified.append(
            {
                "objective_id": objective.id,
                "ok": verification.ok,
                "summary": verification.summary,
            }
        )
        if not verification.ok:
            failures.append(objective.id)
    status: ReadinessStatus = "pass"
    message = "Objective JSONL evidence is append-only and verifiable."
    gaps: list[str] = []
    next_actions: list[str] = []
    reconciliation_dry_run_commands = [
        "harness objectives reconcile-evidence "
        f"{objective_id} --project {shlex.quote(str(project_root))} --dry-run --output json"
        for objective_id in objectives_missing_evidence
    ]
    if failures:
        status = "fail"
        message = "One or more objective evidence chains failed verification."
        gaps.append(f"Objective evidence failed verification: {', '.join(failures)}")
        next_actions.append("Run `harness objectives verify-evidence <objective_id> --output json` for each failing objective.")
    elif objectives_missing_evidence:
        status = "warning"
        message = "Some objectives have run evidence but no objective JSONL evidence chain."
        gaps.append(
            "Run-linked objectives missing objective evidence: "
            + ", ".join(objectives_missing_evidence[:5])
            + (f" (+{len(objectives_missing_evidence) - 5} more)" if len(objectives_missing_evidence) > 5 else "")
        )
        next_actions.append("Run or reconcile those objectives through the objective runner before treating objective evidence as complete.")
        next_actions.extend(reconciliation_dry_run_commands[:5])
    return _check(
        "append_only_objective_evidence",
        status,
        message,
        evidence={
            "objective_count": len(snapshot.objectives),
            "objectives_with_run_evidence": len(objectives_with_run_evidence),
            "objectives_with_evidence": len(verified),
            "objectives_missing_evidence": objectives_missing_evidence,
            "objectives_missing_evidence_count": len(objectives_missing_evidence),
            "reconciliation_dry_run_commands": reconciliation_dry_run_commands,
            "verified": verified,
        },
        gaps=gaps,
        next_actions=next_actions,
    )


def _otel_trace_export_check(
    project_root: Path,
    snapshot: OrchestrationStateSnapshot,
) -> OrchestrationReadinessCheck:
    evidence: dict[str, Any] = {
        "run_trace_export_callable": callable(export_run_trace),
        "objective_trace_export_callable": callable(export_objective_trace),
        "required_semantic_conventions": list(TRACE_SEMANTIC_CONVENTIONS),
        "required_trace_context": dict(TRACE_CONTEXT_PROPAGATION),
        "run_trace_checked": False,
        "objective_trace_checked": False,
    }
    failures: list[str] = []
    if snapshot.initialized:
        store = SQLiteStore(project_root)
        if snapshot.runs:
            run = snapshot.runs[0]
            try:
                export = export_run_trace(project_root, store, run.id)
                contract = _trace_export_semantic_contract(export)
                evidence["run_trace_checked"] = True
                evidence["run_trace"] = {
                    "run_id": run.id,
                    "span_count": len(export.spans),
                    **contract,
                }
                if contract["gaps"]:
                    failures.append(f"run:{run.id}:semantic_contract")
            except Exception as exc:
                failures.append(f"run:{run.id}")
                evidence["run_trace_error"] = f"{exc.__class__.__name__}: {exc}"
        objective_with_evidence = next(
            (objective for objective in snapshot.objectives if _objective_evidence_path(project_root, objective.id).exists()),
            None,
        )
        if objective_with_evidence is not None:
            try:
                export = export_objective_trace(project_root, store, objective_with_evidence.id)
                contract = _trace_export_semantic_contract(export)
                evidence["objective_trace_checked"] = True
                evidence["objective_trace"] = {
                    "objective_id": objective_with_evidence.id,
                    "span_count": len(export.spans),
                    "ok": export.ok,
                    **contract,
                }
                if not export.ok:
                    failures.append(f"objective:{objective_with_evidence.id}")
                if contract["gaps"]:
                    failures.append(f"objective:{objective_with_evidence.id}:semantic_contract")
            except Exception as exc:
                failures.append(f"objective:{objective_with_evidence.id}")
                evidence["objective_trace_error"] = f"{exc.__class__.__name__}: {exc}"
    return _check(
        "otel_trace_export",
        "pass" if not failures else "fail",
        "Run and objective trace export surfaces are available and valid for inspected evidence."
        if not failures
        else "One or more trace export surfaces failed against existing evidence.",
        evidence=evidence,
        gaps=[] if not failures else [f"Trace export failed: {', '.join(failures)}"],
        next_actions=[] if not failures else ["Run the failing `harness traces export* --output json` command directly."],
    )


def _trace_export_semantic_contract(export: Any) -> dict[str, Any]:
    payload = to_otel_json(export)
    missing_conventions = sorted(set(TRACE_SEMANTIC_CONVENTIONS) - set(payload.get("semantic_conventions") or []))
    trace_context = payload.get("trace_context") if isinstance(payload.get("trace_context"), dict) else {}
    missing_trace_context = sorted(
        key
        for key, value in TRACE_CONTEXT_PROPAGATION.items()
        if trace_context.get(key) != value
    )
    spans = _otel_span_payloads(payload)
    span_attribute_gaps: list[dict[str, Any]] = []
    for span in spans:
        attrs = _otel_attributes(span)
        missing = [
            key
            for key in (
                "harness.trace.semantic_conventions",
                "harness.trace.w3c_trace_context",
                "harness.trace.external_protocol_propagation_required",
            )
            if key not in attrs
        ]
        if missing:
            span_attribute_gaps.append({"span": span.get("name"), "missing": missing})
    root_span_name = "harness.run" if getattr(export, "run_id", None) else "harness.objective"
    root_attrs = next((_otel_attributes(span) for span in spans if span.get("name") == root_span_name), {})
    root_operation_ok = root_attrs.get("gen_ai.operation.name") == "invoke_agent"
    root_agent_ok = "gen_ai.agent.id" in root_attrs and "gen_ai.agent.name" in root_attrs
    objective_workflow_ok = True
    if getattr(export, "objective_id", None):
        objective_workflow_ok = root_attrs.get("workflow.id") == getattr(export, "objective_id", None)
    gaps: list[str] = []
    if missing_conventions:
        gaps.append("missing_trace_semantic_conventions")
    if missing_trace_context:
        gaps.append("missing_trace_context_contract")
    if span_attribute_gaps:
        gaps.append("span_semantic_attributes_missing")
    if not root_operation_ok:
        gaps.append("root_genai_operation_missing")
    if not root_agent_ok:
        gaps.append("root_genai_agent_identity_missing")
    if not objective_workflow_ok:
        gaps.append("objective_workflow_identity_missing")
    return {
        "semantic_conventions": list(payload.get("semantic_conventions") or []),
        "missing_semantic_conventions": missing_conventions,
        "trace_context": trace_context,
        "missing_trace_context": missing_trace_context,
        "root_span_name": root_span_name,
        "root_genai_operation_ok": root_operation_ok,
        "root_genai_agent_identity_ok": root_agent_ok,
        "objective_workflow_identity_ok": objective_workflow_ok,
        "span_semantic_attribute_gap_count": len(span_attribute_gaps),
        "span_semantic_attribute_gaps": span_attribute_gaps[:5],
        "gaps": gaps,
    }


def _otel_span_payloads(payload: dict[str, Any]) -> list[dict[str, Any]]:
    spans: list[dict[str, Any]] = []
    for resource_span in payload.get("resourceSpans") or []:
        if not isinstance(resource_span, dict):
            continue
        for scope_span in resource_span.get("scopeSpans") or []:
            if not isinstance(scope_span, dict):
                continue
            for span in scope_span.get("spans") or []:
                if isinstance(span, dict):
                    spans.append(span)
    return spans


def _otel_attributes(span: dict[str, Any]) -> dict[str, Any]:
    attrs: dict[str, Any] = {}
    for item in span.get("attributes") or []:
        if isinstance(item, dict) and isinstance(item.get("key"), str):
            attrs[item["key"]] = item.get("value")
    return attrs


def _replay_drift_detection_check(
    project_root: Path,
    audit: Any | None = None,
) -> OrchestrationReadinessCheck:
    audit = audit or run_orchestration_replay_audit(project_root)
    summary = summarize_orchestration_replay(audit)
    failing = [case.id for case in audit.cases if case.status == "fail"]
    evidence = {
        "schema_version": audit.schema_version,
        "summary": audit.summary,
        "status": summary["status"],
        "case_ids": [case.id for case in audit.cases],
        "failing_case_ids": failing,
        "skipped_case_ids": [case.id for case in audit.cases if case.status == "skipped"],
        "synthetic_case_ids": [case.id for case in audit.cases if case.source_kind == "synthetic"],
        "captured_case_ids": [case.id for case in audit.cases if case.source_kind == "objective_evidence"],
        "detected_issue_codes": sorted(
            {
                code
                for case in audit.cases
                for code in case.detected_issue_codes
                if case.source_kind == "synthetic"
            }
        ),
        "captured_event_count": sum(case.event_count for case in audit.cases if case.source_kind == "objective_evidence"),
        "safety": audit.safety,
        "commands": [
            f"harness evals run --suite orchestration-replay --project {project_root} --output json",
            f"harness orchestration replay --project {project_root} --output json",
        ],
    }
    safety_issues = sorted(
        key
        for key in (
            "provider_called",
            "network_called",
            "adapter_execution_started",
            "filesystem_modified",
            "permission_granting",
            "artifact_bodies_read",
            "model_context_allowed",
        )
        if audit.safety.get(key) is not False
    )
    if audit.safety.get("read_only") is not True:
        safety_issues.append("read_only")
    if safety_issues:
        failing.append("safety")
        evidence["safety_issues"] = safety_issues
    return _check(
        "replay_drift_detection",
        "pass" if audit.ok and not safety_issues else "fail",
        "Synthetic and captured orchestration event logs replay without semantic drift."
        if audit.ok and not safety_issues
        else "Orchestration replay drift detection found semantic or safety issues.",
        evidence=evidence,
        gaps=[]
        if audit.ok and not safety_issues
        else ["Replay drift detection failed for one or more synthetic or captured event-log cases."],
        next_actions=[]
        if audit.ok and not safety_issues
        else ["Run `harness evals run --suite orchestration-replay --output json` and inspect failed cases."],
    )


def _pending_chat_action_recovery_check(
    project_root: Path,
    snapshot: OrchestrationStateSnapshot,
) -> OrchestrationReadinessCheck:
    valid_metadata = {
        PENDING_CHAT_ACTION_METADATA_KEY: {
            "schema_version": PENDING_CHAT_ACTION_SCHEMA_VERSION,
            "kind": "task_draft",
            "draft": {
                "title": "Readiness pending task",
                "description": "Synthetic readiness envelope; not persisted.",
                "execution_adapter": "dry_run",
                "task_type": "phase_1a_test",
            },
        }
    }
    invalid_metadata = {
        PENDING_CHAT_ACTION_METADATA_KEY: {
            "schema_version": PENDING_CHAT_ACTION_SCHEMA_VERSION,
            "kind": "task_draft",
        }
    }
    stale_metadata = {
        PENDING_CHAT_ACTION_METADATA_KEY: {
            "schema_version": PENDING_CHAT_ACTION_SCHEMA_VERSION,
            "kind": "execute_lease",
            "lease_id": "task_lease_readiness_missing",
        }
    }
    valid_audit = pending_chat_action_audit(valid_metadata, session_id="sess_readiness")
    invalid_audit = pending_chat_action_audit(invalid_metadata, session_id="sess_readiness")
    stale_audit = pending_chat_action_audit(
        stale_metadata,
        session_id="sess_readiness",
        lease_status="missing",
    )
    failures: list[str] = []
    if not valid_audit.get("recoverable") or valid_audit.get("pending_action") is None:
        failures.append("recoverable_projection_missing")
    elif valid_audit["pending_action"].get("next_commands") != ["/confirm", "/decline"]:
        failures.append("recoverable_next_commands_changed")
    if invalid_audit.get("status") != "invalid" or invalid_audit.get("pending_action") is not None:
        failures.append("invalid_metadata_is_confirmable")
    if stale_audit.get("status") != "stale" or stale_audit.get("pending_action") is not None:
        failures.append("stale_metadata_is_confirmable")
    if not callable(clear_pending_chat_action_metadata):
        failures.append("cleanup_helper_missing")
    for label, audit in (("recoverable", valid_audit), ("invalid", invalid_audit), ("stale", stale_audit)):
        if audit.get("raw_metadata_exposed") is not False:
            failures.append(f"{label}_raw_metadata_exposed")
        for key in (
            "process_started",
            "filesystem_modified",
            "active_repo_modified",
            "adapter_dispatch_started",
            "provider_called",
            "model_context_sent",
            "network_called",
            "permission_granting",
            "authority_granting",
        ):
            if audit.get(key) is not False:
                failures.append(f"{label}_{key}")
    current: dict[str, Any] = {"initialized": snapshot.initialized}
    warning_ids: list[str] = []
    if snapshot.initialized:
        try:
            store = SQLiteStore(project_root)
            sessions = store.list_sessions()
            audits = [_pending_chat_action_audit_for_readiness(store, session) for session in sessions]
            warning_ids = [
                str(audit.get("session_id"))
                for audit in audits
                if audit.get("status") in {"invalid", "stale"} and audit.get("session_id")
            ]
            current.update(
                {
                    "session_count": len(sessions),
                    "present_count": sum(1 for audit in audits if audit.get("present")),
                    "recoverable_count": sum(1 for audit in audits if audit.get("recoverable")),
                    "invalid_count": sum(1 for audit in audits if audit.get("status") == "invalid"),
                    "stale_count": sum(1 for audit in audits if audit.get("status") == "stale"),
                    "cleanup_supported_count": sum(1 for audit in audits if audit.get("cleanup_supported")),
                    "warning_session_ids": warning_ids,
                }
            )
        except Exception as exc:
            failures.append("runtime_session_audit_failed")
            current["error"] = f"{exc.__class__.__name__}: {exc}"
    evidence = {
        "synthetic": {
            "recoverable_status": valid_audit.get("status"),
            "recoverable_next_commands": (valid_audit.get("pending_action") or {}).get("next_commands"),
            "invalid_status": invalid_audit.get("status"),
            "invalid_issue_codes": [issue.get("code") for issue in invalid_audit.get("issues") or []],
            "stale_status": stale_audit.get("status"),
            "stale_issue_codes": [issue.get("code") for issue in stale_audit.get("issues") or []],
            "cleanup_callable": callable(clear_pending_chat_action_metadata),
            "cleanup_command": invalid_audit.get("cleanup_command"),
            "cleanup_route": invalid_audit.get("cleanup_route"),
            "raw_metadata_exposed": any(
                audit.get("raw_metadata_exposed") for audit in (valid_audit, invalid_audit, stale_audit)
            ),
        },
        "current_sessions": current,
        "cleanup_mutation_scope": "session_metadata_only",
        "execution_started": False,
        "provider_called": False,
        "network_called": False,
        "filesystem_modified": False,
        "permission_granting": False,
    }
    if failures:
        return _check(
            "pending_chat_action_recovery",
            "fail",
            "Pending chat action recovery and cleanup projections are not reliable.",
            evidence=evidence,
            gaps=failures,
            next_actions=[
                "Run `harness sessions pending-action <session_id> --output json` and verify invalid metadata is not confirmable."
            ],
        )
    if warning_ids:
        return _check(
            "pending_chat_action_recovery",
            "warning",
            "Pending chat action recovery works, but one or more sessions need metadata cleanup.",
            evidence=evidence,
            gaps=[f"Invalid or stale pending action metadata: {', '.join(warning_ids)}"],
            next_actions=[
                "Run `harness sessions pending-action <session_id> --output json` for each warning session.",
                "Run `harness sessions clear-pending-action <session_id>` to clear only stale proposal metadata.",
            ],
        )
    return _check(
        "pending_chat_action_recovery",
        "pass",
        "Pending chat action recovery, invalid/stale auditing, and metadata-only cleanup are inspectable.",
        evidence=evidence,
    )


def _budget_limited_delegation_check() -> OrchestrationReadinessCheck:
    adapters = [adapter_delegate_budget_projection(descriptor) for descriptor in list_execution_adapter_descriptors()]
    gaps = [
        f"{adapter['adapter_id']}: {gap}"
        for adapter in adapters
        for gap in adapter.get("gaps", [])
    ]
    ok = not gaps
    return _check(
        "budget_limited_delegation",
        "pass" if ok else "fail",
        "Every registered adapter declares a serialized delegate budget aligned with sandbox and cost policy."
        if ok
        else "One or more registered adapters can delegate without a complete budget boundary.",
        evidence={
            "adapter_count": len(adapters),
            "budget_schema_version": "harness.delegate_budget/v1",
            "adapters": adapters,
            "provider_called": False,
            "network_called": False,
            "adapter_execution_started": False,
            "permission_granting": False,
        },
        gaps=gaps,
        next_actions=[]
        if ok
        else ["Add delegate_budget metadata or align it with the adapter sandbox profile before enabling delegation."],
    )


def _sandboxed_registered_adapters_check() -> OrchestrationReadinessCheck:
    failures: list[str] = []
    adapters: list[dict[str, Any]] = []
    for descriptor in list_execution_adapter_descriptors():
        profile = get_sandbox_profile(descriptor.sandbox_profile_id) if descriptor.sandbox_profile_id else None
        adapters.append(
            {
                "adapter_id": descriptor.id,
                "sandbox_profile_id": descriptor.sandbox_profile_id,
                "required_approvals": descriptor.required_approvals,
                "autonomy_default": descriptor.autonomy_default,
            }
        )
        if profile is None:
            failures.append(descriptor.id)
    return _check(
        "sandboxed_registered_adapters",
        "pass" if not failures else "fail",
        "Every registered adapter declares a known sandbox profile."
        if not failures
        else "One or more registered adapters are missing sandbox profile metadata.",
        evidence={"adapters": adapters},
        gaps=[] if not failures else [f"Missing sandbox profiles: {', '.join(sorted(failures))}"],
        next_actions=[] if not failures else ["Add a sandbox_profile_id for each registered adapter descriptor."],
    )


def _runtime_controls_and_breakers_check(
    project_root: Path,
    snapshot: OrchestrationStateSnapshot,
) -> OrchestrationReadinessCheck:
    if not snapshot.initialized:
        return _check(
            "runtime_controls_and_breakers",
            "skipped",
            "Project runtime state is not initialized; controls and breaker state were not inspected.",
            evidence={"initialized": False},
            next_actions=["Run this audit after `harness init` to inspect persisted controls."],
        )
    try:
        store = SQLiteStore(project_root)
        descriptors = list_execution_adapter_descriptors()
        adapter_ids = [descriptor.id for descriptor in descriptors]
        controls = store.list_execution_controls()
        breakers = store.list_adapter_breaker_states(adapter_ids)
    except Exception as exc:
        return _check(
            "runtime_controls_and_breakers",
            "fail",
            "Runtime controls and adapter breakers could not be inspected.",
            evidence={"error": f"{exc.__class__.__name__}: {exc}"},
            gaps=["Kill-switch and breaker state is not readable."],
            next_actions=["Run `harness controls list --output json` and inspect the failing local state."],
        )
    active_controls = [control for control in controls if control.disabled]
    unmatched_controls = [
        control
        for control in active_controls
        if _runtime_control_requires_descriptor_match(control.target_kind.value)
        and not _runtime_control_matches_any_descriptor(control, descriptors)
    ]
    open_breakers = [breaker for breaker in breakers if breaker.status.value == "open"]
    ok = not unmatched_controls
    return _check(
        "runtime_controls_and_breakers",
        "pass" if ok else "fail",
        "Runtime controls and adapter circuit-breaker state are inspectable.",
        evidence={
            "controls": len(controls),
            "active_controls": len(active_controls),
            "active_control_targets": [
                {
                    "target_kind": control.target_kind.value,
                    "target_id": control.target_id,
                    "reason": control.reason,
                }
                for control in active_controls
            ],
            "unmatched_active_controls": [
                {
                    "target_kind": control.target_kind.value,
                    "target_id": control.target_id,
                    "reason": control.reason,
                }
                for control in unmatched_controls
            ],
            "adapter_breakers": len(breakers),
            "open_breakers": len(open_breakers),
            "open_breaker_adapter_ids": [breaker.adapter_id for breaker in open_breakers],
            "registered_adapters": len(adapter_ids),
        },
        gaps=[]
        if ok
        else [
            "One or more active runtime controls no longer match a registered execution adapter descriptor.",
        ],
        next_actions=[]
        if ok
        else [
            "Run `harness controls list --output json` and either re-enable stale controls or restore the missing adapter descriptor."
        ],
    )


def _runtime_control_requires_descriptor_match(target_kind: str) -> bool:
    return target_kind in {"adapter", "task_type", "backend", "hosted_boundary"}


def _runtime_control_matches_any_descriptor(
    control: KillSwitchRecord,
    descriptors: list[ExecutionAdapterDescriptor],
) -> bool:
    for descriptor in descriptors:
        task_types = descriptor.supported_task_types or [None]
        for task_type in task_types:
            if runtime_control_matches_descriptor(control, descriptor, task_type):
                return True
    return False


def _progress_observability_check(
    project_root: Path,
    snapshot: OrchestrationStateSnapshot,
) -> OrchestrationReadinessCheck:
    if not snapshot.initialized or not snapshot.objectives:
        return _check(
            "progress_observability",
            "skipped",
            "No objective state is present; progress projection was not inspected.",
            evidence={"initialized": snapshot.initialized, "objective_count": len(snapshot.objectives)},
            next_actions=["Create an objective, then run `harness progress --objective <id> --output json`."],
        )
    checked: list[dict[str, Any]] = []
    failures: list[str] = []
    for objective in snapshot.objectives:
        try:
            progress = build_orchestration_progress(project_root, objective.id)
            checked.append(
                {
                    "objective_id": objective.id,
                    "mode": progress.mode.value,
                    "objective_status": progress.objective_status.value,
                    "blocked_reasons": len(progress.blocked_reasons),
                    "has_checkpoints": progress.checkpoints is not None,
                    "has_next_action": bool(progress.next_action),
                }
            )
        except Exception as exc:
            failures.append(objective.id)
            checked.append({"objective_id": objective.id, "error": f"{exc.__class__.__name__}: {exc}"})
    return _check(
        "progress_observability",
        "pass" if not failures else "fail",
        "Objective progress projections are readable for existing objectives."
        if not failures
        else "One or more objective progress projections failed.",
        evidence={"checked": checked},
        gaps=[] if not failures else [f"Progress projection failed: {', '.join(failures)}"],
        next_actions=[] if not failures else ["Run `harness progress --objective <objective_id> --output json`."],
    )


def _protocol_and_tool_exposure_check(project_root: Path) -> OrchestrationReadinessCheck:
    catalog = session_tool_catalog_projection(project_root=project_root)
    tools = catalog.get("tools", [])
    visible = set(model_visible_session_tool_ids(project_root=project_root))
    session_read_tools_default_tool_ids = default_session_operator_tool_ids(project_root)
    session_read_tools_expected_default_tool_ids = sorted(SESSION_OPERATOR_DEFAULT_ALLOWED_TOOLS)
    missing_exposure: list[str] = []
    exposed_boundary_tools: list[str] = []
    loose_model_visible_schemas: list[str] = []
    session_read_tools_default_extras = sorted(
        set(session_read_tools_default_tool_ids) - set(session_read_tools_expected_default_tool_ids)
    )
    session_read_tools_default_missing = sorted(
        set(session_read_tools_expected_default_tool_ids) - set(session_read_tools_default_tool_ids)
    )
    session_read_tools_default_not_model_visible = sorted(
        set(session_read_tools_default_tool_ids) - visible
    )
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        tool_id = str(tool.get("id") or "")
        policy = tool.get("policy")
        if not isinstance(policy, dict) or not isinstance(policy.get("exposure"), dict):
            missing_exposure.append(tool_id)
            continue
        side_effect = str(tool.get("side_effect") or "")
        boundary_kind = str(tool.get("boundary_kind") or "")
        permission_required = bool(tool.get("permission_required"))
        if tool_id in visible:
            input_schema = tool.get("input_schema")
            if (
                isinstance(input_schema, dict)
                and input_schema.get("type") == "object"
                and input_schema.get("additionalProperties") is not False
            ):
                loose_model_visible_schemas.append(tool_id)
        if (
            tool_id in visible
            and (permission_required or side_effect in {"mutation", "execution", "network"} or boundary_kind != "local_only")
        ):
            exposed_boundary_tools.append(tool_id)
    ok = (
        not missing_exposure
        and not exposed_boundary_tools
        and not loose_model_visible_schemas
        and not session_read_tools_default_extras
        and not session_read_tools_default_missing
        and not session_read_tools_default_not_model_visible
        and "invalid" not in session_read_tools_default_tool_ids
        and catalog.get("permission_granting") is False
    )
    evidence = {
        "tool_count": len(tools),
        "model_visible_tool_ids": sorted(visible),
        "missing_exposure": sorted(missing_exposure),
        "exposed_boundary_tools": sorted(exposed_boundary_tools),
        "loose_model_visible_schemas": sorted(loose_model_visible_schemas),
        "session_read_tools_default_tool_ids": session_read_tools_default_tool_ids,
        "session_read_tools_expected_default_tool_ids": session_read_tools_expected_default_tool_ids,
        "session_read_tools_default_extras": session_read_tools_default_extras,
        "session_read_tools_default_missing": session_read_tools_default_missing,
        "session_read_tools_default_not_model_visible": session_read_tools_default_not_model_visible,
        "permission_granting": catalog.get("permission_granting"),
    }
    gaps: list[str] = []
    next_actions: list[str] = []
    if missing_exposure:
        gaps.append("One or more session tools lack explicit exposure policy projection.")
        next_actions.append("Add policy.exposure to every session tool catalog entry before enabling orchestration.")
    if exposed_boundary_tools:
        gaps.append("Default tool exposure widens execution, mutation, network, or extension authority.")
        next_actions.append("Tighten session tool exposure policy before enabling agent-to-tool orchestration.")
    if loose_model_visible_schemas:
        gaps.append("Default model-visible tool schemas allow unspecified top-level arguments.")
        next_actions.append("Set additionalProperties=false on every default model-visible object input schema.")
    if session_read_tools_default_extras or session_read_tools_default_missing:
        gaps.append("The session_read_tools default native schema set drifted from the explicit read-inspection contract.")
        next_actions.append("Restore the default session_read_tools schema set to read, glob, grep, and artifact-read.")
    if session_read_tools_default_not_model_visible:
        gaps.append("The session_read_tools default includes tools that are not model-visible by central policy.")
        next_actions.append("Filter session_read_tools defaults through policy.exposure.model_visible.")
    if "invalid" in session_read_tools_default_tool_ids:
        gaps.append("The session_read_tools default advertises the internal invalid-call recovery tool.")
        next_actions.append("Keep invalid-call recovery internal and remove it from native schema defaults.")
    if catalog.get("permission_granting") is not False:
        gaps.append("Session tool catalog projection is not explicitly non-permission-granting.")
        next_actions.append("Keep catalog and exposure projection read-only and permission_granting=false.")
    return _check(
        "protocol_and_tool_exposure",
        "pass" if ok else "fail",
        "Model-visible tools are limited to low-risk local/session surfaces with explicit exposure policy."
        if ok
        else "Tool exposure policy allows risky, unclassified, or loosely typed model-visible surfaces.",
        evidence=evidence,
        gaps=gaps,
        next_actions=next_actions,
    )


def _external_protocol_compatibility_check(project_root: Path) -> OrchestrationReadinessCheck:
    catalog = build_external_protocol_catalog(project_root)
    protocols = {descriptor.id: descriptor for descriptor in catalog.protocols}
    expected_ids = {
        "model_provider_protocols",
        "local_server_openapi",
        "local_session_tools",
        "mcp_tool",
        "mcp_cached_resource",
        "external_openapi_tool",
        "a2a_remote_agent",
        "grpc_remote_tool",
    }
    expected_model_protocols = {
        "anthropic_messages",
        "bedrock_converse",
        "codex_cli",
        "google_generative",
        "openai_chat",
        "openai_codex_responses",
        "openai_responses",
    }
    missing_ids = sorted(expected_ids - set(protocols))
    missing_model_protocols = sorted(expected_model_protocols - set(catalog.registered_model_protocols))
    risky_visible = sorted(
        protocol_id
        for protocol_id in ("mcp_tool", "mcp_cached_resource", "external_openapi_tool", "a2a_remote_agent", "grpc_remote_tool")
        if protocols.get(protocol_id) is not None and protocols[protocol_id].default_model_visible
    )
    unsafe_runtime = sorted(
        protocol_id
        for protocol_id in ("mcp_tool", "external_openapi_tool", "a2a_remote_agent", "grpc_remote_tool")
        if protocols.get(protocol_id) is not None and protocols[protocol_id].runtime_enabled
    )
    unsafe_authority = sorted(
        protocol.id
        for protocol in catalog.protocols
        if protocol.id != "model_provider_protocols"
        and (
            protocol.authority.process_start_allowed
            or protocol.authority.network_allowed
            or protocol.authority.agent_execution_allowed
            or protocol.authority.filesystem_mutation_allowed
            or protocol.authority.credential_access_allowed
            or protocol.authority.permission_granting
        )
    )
    expected_statuses = {
        "local_server_openapi": "metadata_only",
        "mcp_tool": "fail_closed",
        "mcp_cached_resource": "cached_resource_only",
        "external_openapi_tool": "fail_closed",
        "a2a_remote_agent": "fail_closed",
        "grpc_remote_tool": "fail_closed",
    }
    status_mismatches = sorted(
        f"{protocol_id}:{protocols[protocol_id].status}!={status}"
        for protocol_id, status in expected_statuses.items()
        if protocol_id in protocols and protocols[protocol_id].status != status
    )
    required_telemetry_contracts = {
        "model_provider_protocols": {"opentelemetry.semconv.gen_ai", "w3c_trace_context"},
        "mcp_tool": {"opentelemetry.semconv.gen_ai.mcp", "w3c_trace_context"},
        "mcp_cached_resource": {"opentelemetry.semconv.gen_ai.mcp", "w3c_trace_context"},
        "external_openapi_tool": {"opentelemetry.semconv.gen_ai", "w3c_trace_context"},
        "a2a_remote_agent": {"opentelemetry.semconv.gen_ai.agent", "w3c_trace_context"},
        "grpc_remote_tool": {"opentelemetry.trace", "w3c_trace_context"},
    }
    telemetry_contract_gaps = sorted(
        f"{protocol_id}:{contract}"
        for protocol_id, contracts in required_telemetry_contracts.items()
        if protocol_id in protocols
        for contract in sorted(contracts - set(protocols[protocol_id].telemetry_contracts))
    )
    safety = catalog.safety
    safety_issues = sorted(
        key
        for key in (
            "process_started",
            "network_called",
            "tool_execution_started",
            "agent_execution_started",
            "filesystem_modified",
            "credential_accessed",
            "permission_granting",
            "model_context_allowed",
        )
        if safety.get(key) is not False
    )
    evidence = {
        "schema_version": catalog.schema_version,
        "protocol_ids": sorted(protocols),
        "summary": catalog.summary,
        "registered_model_protocols": catalog.registered_model_protocols,
        "model_visible_tool_ids": catalog.model_visible_tool_ids,
        "missing_protocol_ids": missing_ids,
        "missing_model_protocols": missing_model_protocols,
        "risky_default_model_visible_protocols": risky_visible,
        "unsafe_runtime_enabled_protocols": unsafe_runtime,
        "unsafe_authority_protocols": unsafe_authority,
        "status_mismatches": status_mismatches,
        "required_telemetry_contracts": {
            protocol_id: sorted(contracts) for protocol_id, contracts in sorted(required_telemetry_contracts.items())
        },
        "telemetry_contracts": {
            protocol_id: list(protocols[protocol_id].telemetry_contracts) for protocol_id in sorted(protocols)
        },
        "telemetry_contract_gaps": telemetry_contract_gaps,
        "safety_issues": safety_issues,
        "safety": safety,
        "commands": [
            f"harness protocols list --project {project_root} --output json",
            f"harness protocols inspect mcp_tool --project {project_root} --output json",
            f"harness protocols inspect a2a_remote_agent --project {project_root} --output json",
            f"harness models protocols --project {project_root} --output json",
        ],
    }
    gaps: list[str] = []
    if missing_ids:
        gaps.append("External protocol compatibility descriptors are missing.")
    if missing_model_protocols:
        gaps.append("Registered model-provider protocol adapters are incomplete.")
    if risky_visible:
        gaps.append("Risky extension or remote-agent protocol surfaces are model-visible by default.")
    if unsafe_runtime:
        gaps.append("Fail-closed external protocol surfaces are runtime-enabled.")
    if unsafe_authority:
        gaps.append("External protocol metadata grants process, network, agent, filesystem, credential, or permission authority.")
    if status_mismatches:
        gaps.append("External protocol status no longer matches the explicit incremental-adoption posture.")
    if telemetry_contract_gaps:
        gaps.append("External protocol descriptors are missing trace propagation or semantic span contracts.")
    if safety_issues:
        gaps.append("External protocol catalog projection is not passive.")
    return _check(
        "external_protocol_compatibility",
        "pass" if not gaps else "fail",
        "External protocol compatibility is explicit, metadata-first, and fail-closed for remote execution."
        if not gaps
        else "External protocol compatibility exposes missing or unsafe protocol surfaces.",
        evidence=evidence,
        gaps=gaps,
        next_actions=[] if not gaps else ["Restore the external protocol catalog to metadata-only/fail-closed defaults."],
    )


def _schema_compatibility_contracts_check(project_root: Path) -> OrchestrationReadinessCheck:
    catalog = build_schema_contract_catalog(project_root)
    schemas = {descriptor.id: descriptor for descriptor in catalog.schemas}
    ids = [descriptor.id for descriptor in catalog.schemas]
    duplicate_ids = sorted({schema_id for schema_id in ids if ids.count(schema_id) > 1})
    critical_ids = set(catalog.critical_schema_ids)
    missing_critical_ids = sorted(critical_ids - set(schemas))
    unversioned_schema_ids = sorted(
        descriptor.id
        for descriptor in catalog.schemas
        if not descriptor.current_schema_version.startswith("harness.") or "/v" not in descriptor.current_schema_version
    )
    unsafe_authority_schema_ids = sorted(
        descriptor.id
        for descriptor in catalog.schemas
        if not _schema_contract_authority_is_passive(descriptor.authority.model_dump(mode="json"))
    )
    incomplete_contract_ids = sorted(
        descriptor.id
        for descriptor in catalog.schemas
        if not descriptor.produced_by
        or not descriptor.consumed_by
        or not descriptor.validation_surfaces
        or not descriptor.upgrade_notes
    )
    expected_policies = {
        "agent_contract": "additive_only",
        "agent_discovery_catalog": "additive_only",
        "agent_handoff_envelope": "breaking_requires_new_version",
        "delegate_budget": "additive_only",
        "task_replay_receipt": "additive_only",
        "external_protocol_catalog": "metadata_projection_only",
        "orchestration_readiness_audit": "additive_only",
        "orchestration_efficiency_audit": "additive_only",
        "orchestration_replay_audit": "additive_only",
        "orchestration_synthesis_report": "additive_only",
        "orchestration_scenario_catalog": "additive_only",
        "workflow_template": "additive_only",
        "workflow_agent_selection": "additive_only",
        "workflow_coordination_catalog": "additive_only",
        "objective_batch_plan": "additive_only",
        "objective_evidence_chain": "append_only_hash_chained",
        "objective_checkpoint_chain": "append_only_hash_chained",
        "trace_export": "additive_only",
        "sandbox_profile_catalog": "additive_only",
        "sandbox_profile": "additive_only",
        "session_tool_policy_projection": "additive_only",
        "local_server_openapi": "metadata_projection_only",
    }
    policy_mismatches = sorted(
        f"{schema_id}:{schemas[schema_id].compatibility_policy}!={policy}"
        for schema_id, policy in expected_policies.items()
        if schema_id in schemas and schemas[schema_id].compatibility_policy != policy
    )
    expected_versions = {
        "agent_contract": AGENT_CONTRACT_SCHEMA_VERSION,
        "agent_discovery_catalog": "harness.agent_discovery_catalog/v1",
        "agent_handoff_envelope": AGENT_HANDOFF_ENVELOPE_SCHEMA_VERSION,
        "delegate_budget": "harness.delegate_budget/v1",
        "task_replay_receipt": TASK_REPLAY_RECEIPT_SCHEMA_VERSION,
        "external_protocol_catalog": "harness.external_protocol_catalog/v1",
        "orchestration_readiness_audit": ORCHESTRATION_READINESS_AUDIT_SCHEMA_VERSION,
        "orchestration_efficiency_audit": "harness.orchestration_efficiency/v1",
        "orchestration_replay_audit": "harness.orchestration_replay_audit/v1",
        "orchestration_synthesis_report": "harness.orchestration_synthesis/v1",
        "orchestration_scenario_catalog": "harness.orchestration_scenario_catalog/v1",
        "workflow_template": "harness.workflow_template/v1",
        "workflow_agent_selection": "harness.workflow_agent_selection/v1",
        "workflow_coordination_catalog": "harness.workflow_coordination_catalog/v1",
        "objective_batch_plan": OBJECTIVE_BATCH_PLAN_SCHEMA_VERSION,
        "objective_evidence_chain": "harness.autonomous_objective_event/v1",
        "objective_checkpoint_chain": "harness.objective_checkpoint_event/v1",
        "trace_export": "harness.trace_export/v1",
        "sandbox_profile_catalog": "harness.sandbox_profiles/v1",
        "sandbox_profile": "harness.sandbox_profile/v1",
        "session_tool_policy_projection": "harness.session_tool_policy_projection/v1",
        "local_server_openapi": "harness.local_server.openapi/v1",
    }
    version_mismatches = sorted(
        f"{schema_id}:{schemas[schema_id].current_schema_version}!={version}"
        for schema_id, version in expected_versions.items()
        if schema_id in schemas and schemas[schema_id].current_schema_version != version
    )
    safety = catalog.safety
    safety_issues = sorted(
        key
        for key in (
            "process_started",
            "network_called",
            "tool_execution_started",
            "agent_execution_started",
            "filesystem_modified",
            "credential_accessed",
            "permission_granting",
            "model_context_allowed",
            "artifact_bodies_read",
        )
        if safety.get(key) is not False
    )
    if safety.get("read_only") is not True:
        safety_issues.append("read_only")
    if safety.get("schema_validation_only") is not True:
        safety_issues.append("schema_validation_only")
    evidence = {
        "schema_version": catalog.schema_version,
        "schema_ids": sorted(schemas),
        "critical_schema_ids": catalog.critical_schema_ids,
        "summary": catalog.summary,
        "missing_critical_schema_ids": missing_critical_ids,
        "duplicate_schema_ids": duplicate_ids,
        "unversioned_schema_ids": unversioned_schema_ids,
        "unsafe_authority_schema_ids": unsafe_authority_schema_ids,
        "incomplete_contract_ids": incomplete_contract_ids,
        "policy_mismatches": policy_mismatches,
        "version_mismatches": version_mismatches,
        "safety_issues": sorted(set(safety_issues)),
        "safety": safety,
        "commands": [
            f"harness schemas list --project {project_root} --output json",
            f"harness schemas inspect agent_handoff_envelope --project {project_root} --output json",
            f"harness schemas inspect workflow_agent_selection --project {project_root} --output json",
            f"harness schemas inspect task_replay_receipt --project {project_root} --output json",
            f"harness schemas inspect sandbox_profile_catalog --project {project_root} --output json",
            f"harness schemas inspect objective_evidence_chain --project {project_root} --output json",
        ],
    }
    gaps: list[str] = []
    if missing_critical_ids:
        gaps.append("Critical orchestration schemas are missing from the compatibility registry.")
    if duplicate_ids:
        gaps.append("Schema compatibility registry contains duplicate ids.")
    if unversioned_schema_ids:
        gaps.append("One or more schema contracts do not use explicit Harness schema versions.")
    if unsafe_authority_schema_ids:
        gaps.append("Schema compatibility metadata grants runtime authority.")
    if incomplete_contract_ids:
        gaps.append("One or more schema contracts lack producer, consumer, validation, or upgrade metadata.")
    if policy_mismatches:
        gaps.append("Schema compatibility policies drifted from the expected migration posture.")
    if version_mismatches:
        gaps.append("Registered schema versions drifted from current produced payload versions.")
    if safety_issues:
        gaps.append("Schema compatibility catalog projection is not passive.")
    return _check(
        "schema_compatibility_contracts",
        "pass" if not gaps else "fail",
        "Critical orchestration schemas are registered with explicit compatibility, ownership, and passive authority contracts."
        if not gaps
        else "Schema compatibility contracts are missing, unversioned, authority-bearing, or stale.",
        evidence=evidence,
        gaps=gaps,
        next_actions=[]
        if not gaps
        else ["Restore the schema compatibility catalog before relying on rolling-upgrade or migration evidence."],
    )


def _schema_contract_authority_is_passive(authority: dict[str, Any]) -> bool:
    return (
        authority.get("read_only_projection") is True
        and authority.get("validation_only") is True
        and all(
            authority.get(key) is False
            for key in (
                "execution_authority",
                "process_start_allowed",
                "network_allowed",
                "tool_execution_allowed",
                "agent_execution_allowed",
                "filesystem_mutation_allowed",
                "credential_access_allowed",
                "permission_granting",
                "model_context_allowed",
            )
        )
    )


def _passive_metadata_safety_issues(safety: dict[str, Any]) -> list[str]:
    issues = sorted(
        key
        for key in (
            "source_body_loaded",
            "provider_called",
            "network_called",
            "agent_execution_started",
            "tool_execution_started",
            "adapter_execution_started",
            "process_started",
            "filesystem_modified",
            "credential_accessed",
            "permission_granting",
            "budget_granting",
            "model_context_allowed",
        )
        if safety.get(key) is not False
    )
    if safety.get("read_only") is not True:
        issues.append("read_only")
    if safety.get("metadata_only") is not True:
        issues.append("metadata_only")
    return sorted(set(issues))


def _orchestration_scenario_conformance_check(project_root: Path) -> OrchestrationReadinessCheck:
    catalog = build_orchestration_scenario_catalog(project_root)
    payload = catalog.model_dump(mode="json")
    case_ids = [str(case.get("id")) for case in payload.get("cases", []) if isinstance(case, dict)]
    layer_ids = sorted(
        {str(case.get("layer")) for case in payload.get("cases", []) if isinstance(case, dict) and case.get("layer")}
    )
    missing_cases = [case_id for case_id in payload.get("required_case_ids", []) if case_id not in set(case_ids)]
    missing_layers = [layer for layer in payload.get("required_layers", []) if layer not in set(layer_ids)]
    failed_cases = [
        str(case.get("id"))
        for case in payload.get("cases", [])
        if isinstance(case, dict) and case.get("status") == "fail"
    ]
    safety = payload.get("safety", {})
    safety_issues = sorted(
        key
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
            "live_benchmark_execution_allowed",
            "approval_store_instantiated",
        )
        if isinstance(safety, dict) and safety.get(key) is not False
    )
    if not isinstance(safety, dict) or safety.get("read_only") is not True:
        safety_issues.append("read_only")
    ok = catalog.ok and not missing_cases and not missing_layers and not failed_cases and not safety_issues
    evidence = {
        "schema_version": catalog.schema_version,
        "summary": payload.get("summary", {}),
        "case_ids": case_ids,
        "layer_ids": layer_ids,
        "missing_required_case_ids": missing_cases,
        "missing_required_layers": missing_layers,
        "failed_case_ids": failed_cases,
        "safety_issues": safety_issues,
        "safety": safety if isinstance(safety, dict) else {},
        "cases": compact_scenario_rows(catalog),
    }
    gaps = (
        [f"missing scenario case ids: {', '.join(missing_cases)}"] if missing_cases else []
    ) + ([f"missing scenario layers: {', '.join(missing_layers)}"] if missing_layers else [])
    gaps.extend(f"{case_id}: scenario conformance failed" for case_id in failed_cases)
    gaps.extend(f"safety issue: {issue}" for issue in safety_issues)
    return _check(
        "orchestration_scenario_conformance",
        "pass" if ok else "fail",
        "Layered orchestration scenario probes cover replay, scenario, contract, security, unit, and benchmark failure modes."
        if ok
        else "Layered orchestration scenario coverage is incomplete or unsafe.",
        evidence=evidence,
        gaps=gaps,
        next_actions=[]
        if ok
        else ["Run `harness orchestration scenarios --output json` and restore missing or failed scenario probes."],
    )


def _workflow_coordination_contracts_check(project_root: Path) -> OrchestrationReadinessCheck:
    catalog = build_workflow_coordination_catalog(project_root)
    payload = catalog.model_dump(mode="json")
    pattern_ids = [str(pattern.get("id")) for pattern in payload.get("patterns", []) if isinstance(pattern, dict)]
    state_class_ids = [
        str(state_class.get("id")) for state_class in payload.get("state_classes", []) if isinstance(state_class, dict)
    ]
    missing_patterns = [
        pattern_id for pattern_id in payload.get("required_pattern_ids", []) if pattern_id not in set(pattern_ids)
    ]
    missing_state_classes = [
        state_id for state_id in payload.get("required_state_class_ids", []) if state_id not in set(state_class_ids)
    ]
    failed_patterns = [
        str(pattern.get("id"))
        for pattern in payload.get("patterns", [])
        if isinstance(pattern, dict) and pattern.get("status") == "fail"
    ]
    safety = payload.get("safety", {})
    safety_issues = sorted(
        key
        for key in (
            "reference_code_imported",
            "reference_contents_included",
            "provider_called",
            "network_called",
            "adapter_execution_started",
            "tool_execution_started",
            "agent_execution_started",
            "filesystem_modified",
            "permission_granting",
            "artifact_bodies_read",
            "model_context_allowed",
        )
        if isinstance(safety, dict) and safety.get(key) is not False
    )
    if not isinstance(safety, dict) or safety.get("read_only") is not True:
        safety_issues.append("read_only")
    ok = catalog.ok and not missing_patterns and not missing_state_classes and not failed_patterns and not safety_issues
    evidence = {
        "schema_version": catalog.schema_version,
        "summary": payload.get("summary", {}),
        "pattern_ids": pattern_ids,
        "state_class_ids": state_class_ids,
        "missing_required_pattern_ids": missing_patterns,
        "missing_required_state_class_ids": missing_state_classes,
        "failed_pattern_ids": failed_patterns,
        "safety_issues": safety_issues,
        "safety": safety if isinstance(safety, dict) else {},
        "patterns": [
            {
                "id": pattern.get("id"),
                "status": pattern.get("status"),
                "execution_mode": pattern.get("execution_mode"),
                "state_classes": pattern.get("state_classes", []),
                "reference_patterns": pattern.get("reference_patterns", []),
                "invariants": pattern.get("invariants", []),
            }
            for pattern in payload.get("patterns", [])
            if isinstance(pattern, dict)
        ],
        "state_classes": [
            {
                "id": state_class.get("id"),
                "durability": state_class.get("durability"),
                "mutation_owner": state_class.get("mutation_owner"),
                "model_context_allowed_by_default": state_class.get("model_context_allowed_by_default"),
            }
            for state_class in payload.get("state_classes", [])
            if isinstance(state_class, dict)
        ],
    }
    gaps: list[str] = []
    if missing_patterns:
        gaps.append(f"Missing workflow coordination patterns: {', '.join(missing_patterns)}")
    if missing_state_classes:
        gaps.append(f"Missing workflow state classes: {', '.join(missing_state_classes)}")
    if failed_patterns:
        gaps.append(f"Workflow coordination patterns failed: {', '.join(failed_patterns)}")
    if safety_issues:
        gaps.append(f"Workflow coordination catalog safety flags are not passive: {', '.join(safety_issues)}")
    return _check(
        "workflow_coordination_contracts",
        "pass" if ok else "fail",
        "Workflow coordination patterns and state-class boundaries are explicit, versioned, and passive."
        if ok
        else "Workflow coordination contracts are missing, failing, or no longer passive.",
        evidence=evidence,
        gaps=gaps,
        next_actions=[]
        if ok
        else ["Run `harness orchestration workflows --output json` and restore required pattern/state-class contracts."],
    )


def _agentic_security_controls_check(
    *,
    typed_task_delegation: OrchestrationReadinessCheck,
    bounded_parallel_scheduler: OrchestrationReadinessCheck,
    replay_drift_detection: OrchestrationReadinessCheck,
    runtime_controls_and_breakers: OrchestrationReadinessCheck,
    external_protocol_compatibility: OrchestrationReadinessCheck,
    schema_compatibility_contracts: OrchestrationReadinessCheck,
    protocol_and_tool_exposure: OrchestrationReadinessCheck,
    applyback_governance: OrchestrationReadinessCheck,
) -> OrchestrationReadinessCheck:
    local_memory = decide_context_transmission(
        "local_sqlite",
        source_kind="memory_record",
        trust_level="memory",
    )
    hosted_memory = decide_context_transmission(
        "hosted_model",
        source_kind="memory_record",
        trust_level="memory",
    )
    remote_memory = decide_context_transmission(
        "remote_vector_store",
        source_kind="memory_record",
        trust_level="memory",
    )
    secret_context = decide_context_transmission(
        "local_sqlite",
        source_kind="artifact",
        trust_level="untrusted_tool_output",
        path=".env",
        warnings=["secret_path"],
    )
    memory_poisoning_ok = (
        local_memory.allowed
        and "memory_not_authority" in local_memory.warnings
        and not hosted_memory.allowed
        and "memory_not_authority" in hosted_memory.warnings
        and not remote_memory.allowed
        and not secret_context.allowed
        and all(
            _context_policy_decision_is_non_authority(decision)
            for decision in (local_memory, hosted_memory, remote_memory, secret_context)
        )
    )

    handoff_evidence = typed_task_delegation.evidence
    handoff_authority = handoff_evidence.get("handoff_authority") if isinstance(handoff_evidence, dict) else {}
    unsafe_handoff_authority = (
        list(handoff_evidence.get("unsafe_handoff_authority") or [])
        if isinstance(handoff_evidence, dict)
        else []
    )
    external_evidence = external_protocol_compatibility.evidence
    risky_protocols = sorted(
        set(external_evidence.get("risky_default_model_visible_protocols") or [])
        | set(external_evidence.get("unsafe_runtime_enabled_protocols") or [])
        | set(external_evidence.get("unsafe_authority_protocols") or [])
    )
    inter_agent_communication_ok = (
        typed_task_delegation.status == "pass"
        and external_protocol_compatibility.status == "pass"
        and not unsafe_handoff_authority
        and not risky_protocols
        and bool(handoff_evidence.get("handoff_traceparent"))
        and bool(handoff_evidence.get("handoff_payload_sha256"))
        and isinstance(handoff_authority, dict)
        and handoff_authority.get("read_only_projection") is True
        and handoff_authority.get("permission_granting") is False
        and handoff_authority.get("credential_access_allowed") is False
        and handoff_authority.get("model_context_allowed") is False
    )

    replay_evidence = replay_drift_detection.evidence
    detected_issue_codes = set(replay_evidence.get("detected_issue_codes") or [])
    required_replay_detections = {
        "dispatch_after_blocking_event",
        "duplicate_side_effect_dispatch",
        "missing_stopped_event",
    }
    descriptors = list_execution_adapter_descriptors()
    safe_auto_replay_policies = {"safe", "idempotent_with_key"}
    auto_allowed_unsafe_replay = sorted(
        descriptor.id
        for descriptor in descriptors
        if descriptor.autonomy_default == "auto_allowed"
        and getattr(descriptor.replay_policy, "value", str(descriptor.replay_policy)) not in safe_auto_replay_policies
    )
    cascading_failure_ok = (
        bounded_parallel_scheduler.status == "pass"
        and replay_drift_detection.status == "pass"
        and required_replay_detections.issubset(detected_issue_codes)
        and runtime_controls_and_breakers.status != "fail"
        and not auto_allowed_unsafe_replay
    )

    dependent_statuses = {
        "typed_task_delegation": typed_task_delegation.status,
        "bounded_parallel_scheduler": bounded_parallel_scheduler.status,
        "replay_drift_detection": replay_drift_detection.status,
        "runtime_controls_and_breakers": runtime_controls_and_breakers.status,
        "external_protocol_compatibility": external_protocol_compatibility.status,
        "schema_compatibility_contracts": schema_compatibility_contracts.status,
        "protocol_and_tool_exposure": protocol_and_tool_exposure.status,
        "applyback_governance": applyback_governance.status,
    }
    failed_dependencies = sorted(check_id for check_id, status in dependent_statuses.items() if status == "fail")
    risk_controls = [
        {
            "risk_id": "memory_poisoning",
            "status": "pass" if memory_poisoning_ok else "fail",
            "controls": [
                "memory_not_authority_warning",
                "hosted_context_transmission_denied_by_default",
                "remote_vector_store_denied_by_default",
                "secret_or_excluded_context_denied",
            ],
        },
        {
            "risk_id": "insecure_inter_agent_communication",
            "status": "pass" if inter_agent_communication_ok else "fail",
            "controls": [
                "typed_handoff_envelope",
                "readonly_handoff_authority",
                "traceparent_and_payload_hash_required",
                "remote_agent_protocols_fail_closed",
            ],
        },
        {
            "risk_id": "cascading_failures",
            "status": "pass" if cascading_failure_ok else "fail",
            "controls": [
                "bounded_parallel_scheduler",
                "adapter_breaker_projection",
                "auto_allowed_replay_policy_guard",
                "replay_detects_duplicate_and_blocked_dispatch",
            ],
        },
    ]
    failing_risks = [row["risk_id"] for row in risk_controls if row["status"] != "pass"]
    gaps: list[str] = []
    if failing_risks:
        gaps.append("Agentic security risk controls failed: " + ", ".join(failing_risks))
    if failed_dependencies:
        gaps.append("Dependent readiness controls failed: " + ", ".join(failed_dependencies))
    evidence = {
        "risk_controls": risk_controls,
        "risk_count": len(risk_controls),
        "passed_risk_count": sum(1 for row in risk_controls if row["status"] == "pass"),
        "dependent_check_statuses": dependent_statuses,
        "failed_dependency_check_ids": failed_dependencies,
        "context_policy_decisions": {
            "local_memory": local_memory.to_payload(),
            "hosted_memory": hosted_memory.to_payload(),
            "remote_memory": remote_memory.to_payload(),
            "secret_context": secret_context.to_payload(),
        },
        "handoff": {
            "schema_version": handoff_evidence.get("handoff_schema_version"),
            "traceparent_present": bool(handoff_evidence.get("handoff_traceparent")),
            "payload_sha256_present": bool(handoff_evidence.get("handoff_payload_sha256")),
            "unsafe_handoff_authority": unsafe_handoff_authority,
            "authority": handoff_authority if isinstance(handoff_authority, dict) else {},
        },
        "protocols": {
            "risky_protocols": risky_protocols,
            "fail_closed_protocol_count": (external_evidence.get("summary") or {}).get("fail_closed_count"),
        },
        "cascading_failure": {
            "required_replay_detections": sorted(required_replay_detections),
            "detected_replay_issue_codes": sorted(detected_issue_codes),
            "auto_allowed_unsafe_replay_adapter_ids": auto_allowed_unsafe_replay,
            "adapter_count": len(descriptors),
            "runtime_controls_status": runtime_controls_and_breakers.status,
        },
        "security_regression_surfaces": [
            "tests/test_security_regression_matrix.py",
            "tests/test_context_pack.py",
            "tests/test_memory_v1_8.py",
            "tests/test_orchestration_readiness.py",
        ],
        "safety": {
            "read_only": True,
            "provider_called": False,
            "network_called": False,
            "adapter_execution_started": False,
            "filesystem_modified": False,
            "permission_granting": False,
            "model_context_allowed": False,
            "credential_accessed": False,
        },
    }
    ok = not gaps
    return _check(
        "agentic_security_controls",
        "pass" if ok else "fail",
        "Agentic security controls cover memory poisoning, insecure inter-agent communication, and cascading failures."
        if ok
        else "Agentic security controls are missing or depend on failing readiness checks.",
        evidence=evidence,
        gaps=gaps,
        next_actions=[]
        if ok
        else ["Restore context-policy, handoff, protocol, replay, runtime-control, and apply-back readiness checks."],
    )


def _context_policy_decision_is_non_authority(decision: Any) -> bool:
    return all(
        getattr(decision, field_name) is False
        for field_name in (
            "permission_granting",
            "policy_authority",
            "approval_authority",
            "process_started",
            "filesystem_modified",
            "provider_call_allowed",
            "docker_allowed",
            "adapter_dispatch_allowed",
            "active_repo_mutation_allowed",
        )
    )


def _applyback_governance_check() -> OrchestrationReadinessCheck:
    payload = gate_registry_payload()
    gate_ids = {str(gate.get("id")) for gate in payload.get("gates", []) if isinstance(gate, dict)}
    required = {
        "allowed_paths_respected",
        "applyback_bound_to_segment",
        "checkpoint_approved",
        "no_secret_in_diff",
        "no_vendored_third_party_diff",
        "promotion_tests_current",
    }
    missing = sorted(required - gate_ids)
    return _check(
        "applyback_governance",
        "pass" if not missing else "fail",
        "Apply-back and promotion gates cover path scope, checkpoints, tests, secrets, and vendored-code drift."
        if not missing
        else "Apply-back governance gate registry is missing required orchestration gates.",
        evidence={
            "required_gate_ids": sorted(required),
            "missing_gate_ids": missing,
            "protected_apply_patterns": len(payload.get("protected_apply_patterns", [])),
        },
        gaps=[] if not missing else [f"Missing governance gates: {', '.join(missing)}"],
        next_actions=[] if not missing else ["Restore the missing gate ids in governance.gate_registry."],
    )


def _reference_repository_hygiene_check(
    project_root: Path,
    reference_root: Path | None,
    include_references: bool,
) -> OrchestrationReadinessCheck:
    if not include_references:
        return _check(
            "reference_repository_hygiene",
            "skipped",
            "Reference repository hygiene inspection was explicitly disabled.",
            evidence={"include_references": False},
        )
    audit = build_reference_repositories_audit(project_root, reference_root=reference_root)
    payload = audit.to_dict()
    summary = payload["summary"]
    status: ReadinessStatus = "pass"
    gaps: list[str] = []
    next_actions: list[str] = []
    if not payload["root_exists"]:
        status = "warning"
        gaps.append("No local reference repository root was found.")
        next_actions.append("Pull useful references under the sibling harness-references directory or pass --reference-root.")
    elif not payload["root_is_directory"]:
        status = "fail"
        gaps.append("Reference root exists but is not a directory.")
        next_actions.append("Move the file aside or pass a valid --reference-root directory.")
    else:
        if summary["repository_count"] == 0:
            status = "warning"
            gaps.append("Reference root exists but contains no Git repositories.")
            next_actions.append("Clone or pull curated reference repositories before using this audit as reference evidence.")
        if summary.get("missing_expected_repository_count", 0) > 0:
            status = "warning"
            gaps.append("One or more curated reference repositories are missing.")
            next_actions.append(
                "Run `harness governance references-audit --output json` and pull the missing curated references."
            )
        if summary.get("missing_required_reference_pattern_count", 0) > 0:
            status = "warning"
            missing_patterns = payload.get("missing_required_reference_patterns") or summary.get(
                "missing_required_reference_patterns", []
            )
            gaps.append(
                "Reference repository set does not cover required implementation patterns: "
                + ", ".join(str(pattern) for pattern in missing_patterns)
            )
            next_actions.append(
                "Use the reference pattern coverage matrix before translating third-party designs into Harness."
            )
        if summary["dirty_repository_count"] > 0:
            status = "warning"
            gaps.append("One or more reference repositories have uncommitted local changes.")
            next_actions.append("Review dirty reference repositories before translating patterns into Harness.")
        if summary.get("lfs_unmaterialized_file_count", 0) > 0:
            status = "warning"
            gaps.append("One or more Git LFS reference files are not materialized locally.")
            next_actions.append("Run `git lfs pull` in the affected reference repositories before using their assets.")
    evidence = {
        "reference_root": payload["reference_root"],
        "summary": summary,
        "authority": payload["authority"],
        "expected_repository_names": payload["expected_repository_names"],
        "required_reference_patterns": payload.get("required_reference_patterns", []),
        "covered_reference_patterns": payload.get("covered_reference_patterns", []),
        "missing_required_reference_patterns": payload.get("missing_required_reference_patterns", []),
        "reference_pattern_coverage": payload.get("reference_pattern_coverage", {}),
        "missing_expected_repository_names": payload["missing_expected_repository_names"],
        "extra_repository_names": payload["extra_repository_names"],
        "repository_names": [repo["name"] for repo in payload["repositories"]],
    }
    return _check(
        "reference_repository_hygiene",
        status,
        "Reference repositories are inventoried as non-authoritative, metadata-only inputs."
        if status == "pass"
        else "Reference repository metadata is available but needs operator review.",
        evidence=evidence,
        gaps=gaps,
        next_actions=next_actions,
    )


def _pending_chat_action_audit_for_readiness(store: SQLiteStore, session: Any) -> dict[str, Any]:
    return pending_chat_action_audit(
        session.metadata,
        session_id=session.id,
        lease_status=_pending_chat_action_lease_status_for_readiness(store, session.metadata),
    )


def _pending_chat_action_lease_status_for_readiness(store: SQLiteStore, metadata: dict[str, Any] | None) -> str | None:
    raw = (metadata or {}).get(PENDING_CHAT_ACTION_METADATA_KEY)
    if not isinstance(raw, dict) or raw.get("kind") != "execute_lease":
        return None
    lease_id = raw.get("lease_id")
    if not isinstance(lease_id, str) or not lease_id.strip():
        return None
    try:
        return store.get_task_lease(lease_id).status.value
    except KeyError:
        return "missing"


def _objective_evidence_path(project_root: Path, objective_id: str) -> Path:
    return project_root / HARNESS_DIR / "autonomy" / "objectives" / f"{objective_id}.jsonl"


def _snapshot_counts(snapshot: OrchestrationStateSnapshot) -> dict[str, Any]:
    return {
        "initialized": snapshot.initialized,
        "ok": snapshot.ok,
        "objectives": len(snapshot.objectives),
        "tasks": len(snapshot.tasks),
        "dependencies": len(snapshot.dependencies),
        "attempts": len(snapshot.attempts),
        "leases": len(snapshot.leases),
        "runs": len(snapshot.runs),
        "artifact_runs": len(snapshot.artifacts_by_run),
        "run_event_streams": len(snapshot.run_events_by_run),
        "orchestration_event_streams": len(snapshot.orchestration_events_by_objective),
    }


def _check(
    check_id: str,
    status: ReadinessStatus,
    message: str,
    *,
    evidence: dict[str, Any] | None = None,
    gaps: list[str] | None = None,
    next_actions: list[str] | None = None,
) -> OrchestrationReadinessCheck:
    return OrchestrationReadinessCheck(
        id=check_id,
        status=status,
        message=message,
        reference_patterns=list(REFERENCE_PATTERNS_BY_CHECK.get(check_id, [])),
        evidence=sanitize_for_logging(evidence or {}),
        gaps=list(gaps or []),
        next_actions=list(next_actions or []),
    )


def _summary(checks: list[OrchestrationReadinessCheck]) -> dict[str, int]:
    return {
        "total": len(checks),
        "pass": sum(1 for check in checks if check.status == "pass"),
        "warning": sum(1 for check in checks if check.status == "warning"),
        "fail": sum(1 for check in checks if check.status == "fail"),
        "skipped": sum(1 for check in checks if check.status == "skipped"),
    }


def _safety_flags() -> dict[str, bool]:
    return {
        "read_only": True,
        "reference_code_imported": False,
        "reference_contents_included": False,
        "reference_execution_allowed": False,
        "provider_called": False,
        "network_called": False,
        "adapter_execution_started": False,
        "filesystem_modified": False,
        "filesystem_mutation_allowed": False,
        "permission_granting": False,
    }
