from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from harness.memory.sqlite_store import TASK_REPLAY_RECEIPT_SCHEMA_VERSION
from harness.objective_batch_plan import OBJECTIVE_BATCH_PLAN_SCHEMA_VERSION
from harness.paths import resolve_project_root
from harness.workflow_templates import WORKFLOW_AGENT_SELECTION_SCHEMA_VERSION, WORKFLOW_TEMPLATE_SCHEMA_VERSION


SCHEMA_CONTRACT_CATALOG_SCHEMA_VERSION = "harness.schema_contract_catalog/v1"
SCHEMA_CONTRACT_DESCRIPTOR_SCHEMA_VERSION = "harness.schema_contract_descriptor/v1"
SCHEMA_CONTRACT_AUTHORITY_SCHEMA_VERSION = "harness.schema_contract_authority/v1"

SchemaContractSurface = Literal[
    "agent_identity",
    "agent_discovery",
    "task_handoff",
    "task_queue",
    "delegation_budget",
    "external_protocol",
    "orchestration_readiness",
    "orchestration_efficiency",
    "orchestration_replay",
    "orchestration_scenarios",
    "orchestration_synthesis",
    "workflow_template",
    "workflow_coordination",
    "objective_evidence",
    "objective_checkpoint",
    "trace_export",
    "sandbox_profile",
    "session_tooling",
    "local_server",
]
SchemaContractStability = Literal["stable", "beta", "internal"]
SchemaCompatibilityPolicy = Literal[
    "additive_only",
    "breaking_requires_new_version",
    "metadata_projection_only",
    "append_only_hash_chained",
]


class SchemaContractAuthority(BaseModel):
    schema_version: str = SCHEMA_CONTRACT_AUTHORITY_SCHEMA_VERSION
    read_only_projection: bool = True
    validation_only: bool = True
    execution_authority: bool = False
    process_start_allowed: bool = False
    network_allowed: bool = False
    tool_execution_allowed: bool = False
    agent_execution_allowed: bool = False
    filesystem_mutation_allowed: bool = False
    credential_access_allowed: bool = False
    permission_granting: bool = False
    model_context_allowed: bool = False


class SchemaContractDescriptor(BaseModel):
    schema_version: str = SCHEMA_CONTRACT_DESCRIPTOR_SCHEMA_VERSION
    id: str
    title: str
    surface: SchemaContractSurface
    stability: SchemaContractStability
    compatibility_policy: SchemaCompatibilityPolicy
    current_schema_version: str
    owner: str
    produced_by: list[str] = Field(default_factory=list)
    consumed_by: list[str] = Field(default_factory=list)
    validation_surfaces: list[str] = Field(default_factory=list)
    upgrade_notes: list[str] = Field(default_factory=list)
    reference_patterns: list[str] = Field(default_factory=list)
    authority: SchemaContractAuthority = Field(default_factory=SchemaContractAuthority)


class SchemaContractCatalog(BaseModel):
    schema_version: str = SCHEMA_CONTRACT_CATALOG_SCHEMA_VERSION
    ok: bool = True
    project_root: Path
    initialized: bool
    schemas: list[SchemaContractDescriptor]
    critical_schema_ids: list[str]
    safety: dict[str, bool]
    summary: dict[str, int]


CRITICAL_SCHEMA_IDS = [
    "agent_contract",
    "agent_discovery_catalog",
    "agent_handoff_envelope",
    "delegate_budget",
    "task_replay_receipt",
    "external_protocol_catalog",
    "orchestration_readiness_audit",
    "orchestration_efficiency_audit",
    "orchestration_replay_audit",
    "orchestration_scenario_catalog",
    "orchestration_synthesis_report",
    "workflow_template",
    "workflow_agent_selection",
    "workflow_coordination_catalog",
    "objective_batch_plan",
    "objective_evidence_chain",
    "objective_checkpoint_chain",
    "trace_export",
    "sandbox_profile_catalog",
    "sandbox_profile",
    "session_tool_policy_projection",
    "local_server_openapi",
]


def build_schema_contract_catalog(project_root: Path) -> SchemaContractCatalog:
    """Return the passive compatibility registry for orchestration schema surfaces."""

    root = resolve_project_root(project_root)
    initialized = (root / ".harness" / "harness.sqlite").exists()
    schemas = _schema_descriptors()
    ids = [descriptor.id for descriptor in schemas]
    duplicate_count = len(ids) - len(set(ids))
    versioned_count = sum(1 for descriptor in schemas if _is_harness_v1_schema(descriptor.current_schema_version))
    stable_count = sum(1 for descriptor in schemas if descriptor.stability == "stable")
    internal_count = sum(1 for descriptor in schemas if descriptor.stability == "internal")
    critical_present_count = sum(1 for schema_id in CRITICAL_SCHEMA_IDS if schema_id in set(ids))
    authority_safe_count = sum(1 for descriptor in schemas if _authority_is_passive(descriptor.authority))
    ok = (
        duplicate_count == 0
        and critical_present_count == len(CRITICAL_SCHEMA_IDS)
        and versioned_count == len(schemas)
        and authority_safe_count == len(schemas)
    )
    return SchemaContractCatalog(
        ok=ok,
        project_root=root,
        initialized=initialized,
        schemas=schemas,
        critical_schema_ids=list(CRITICAL_SCHEMA_IDS),
        safety={
            "read_only": True,
            "schema_validation_only": True,
            "process_started": False,
            "network_called": False,
            "tool_execution_started": False,
            "agent_execution_started": False,
            "filesystem_modified": False,
            "credential_accessed": False,
            "permission_granting": False,
            "model_context_allowed": False,
            "artifact_bodies_read": False,
        },
        summary={
            "schema_count": len(schemas),
            "critical_schema_count": len(CRITICAL_SCHEMA_IDS),
            "critical_present_count": critical_present_count,
            "duplicate_schema_id_count": duplicate_count,
            "versioned_schema_count": versioned_count,
            "stable_schema_count": stable_count,
            "internal_schema_count": internal_count,
            "authority_safe_schema_count": authority_safe_count,
        },
    )


def get_schema_contract_descriptor(project_root: Path, schema_id: str) -> SchemaContractDescriptor:
    for descriptor in build_schema_contract_catalog(project_root).schemas:
        if descriptor.id == schema_id:
            return descriptor
    raise KeyError(f"Schema contract not found: {schema_id}")


def _schema_descriptors() -> list[SchemaContractDescriptor]:
    return [
        SchemaContractDescriptor(
            id="agent_contract",
            title="Canonical agent identity contract",
            surface="agent_identity",
            stability="stable",
            compatibility_policy="additive_only",
            current_schema_version="harness.agent_contract/v1",
            owner="agent_contracts",
            produced_by=["build_agent_contract", "harness agents contract"],
            consumed_by=["agent handoff envelopes", "typed_task_delegation readiness"],
            validation_surfaces=[
                "tests/test_agent_contracts.py",
                "tests/test_agent_handoff.py",
                "harness agents contract <agent_id> --output json",
            ],
            upgrade_notes=[
                "Add fields compatibly within v1; use v2 before changing identity, policy, or authority semantics.",
                "Agent contracts are metadata projections and never grant runtime, model, tool, network, or filesystem authority.",
            ],
            reference_patterns=["microsoft_agent_framework", "google_adk", "openai_agents"],
        ),
        SchemaContractDescriptor(
            id="agent_discovery_catalog",
            title="Agent discovery and delegate allocation catalog",
            surface="agent_discovery",
            stability="stable",
            compatibility_policy="additive_only",
            current_schema_version="harness.agent_discovery_catalog/v1",
            owner="agent_discovery",
            produced_by=["build_agent_discovery_catalog", "harness agents discover"],
            consumed_by=[
                "readiness agent_discovery_and_allocation",
                "local server /agents/discovery",
                "delegate allocation previews",
            ],
            validation_surfaces=[
                "tests/test_agent_discovery.py",
                "harness agents discover --output json",
                "harness agents allocate --output json",
            ],
            upgrade_notes=[
                "Discovery cards and bid terms can grow additively; changing selection semantics or authority flags requires a new version.",
                "The catalog is local metadata only and never starts agents, calls providers, creates tasks, grants budgets, or grants permission.",
            ],
            reference_patterns=["contract_net", "a2a_agent_card", "microsoft_agent_framework", "google_adk"],
        ),
        SchemaContractDescriptor(
            id="agent_handoff_envelope",
            title="Typed agent handoff envelope",
            surface="task_handoff",
            stability="stable",
            compatibility_policy="breaking_requires_new_version",
            current_schema_version="harness.agent_handoff_envelope/v1",
            owner="agent_handoff",
            produced_by=["build_agent_handoff_envelope", "harness handoffs inspect-task"],
            consumed_by=["session child-task records", "task-status", "readiness typed_task_delegation"],
            validation_surfaces=[
                "tests/test_agent_handoff.py",
                "harness handoffs inspect-task <task_id> --output json",
            ],
            upgrade_notes=[
                "Handoff payload hashes include nested contracts; semantic field removal or authority broadening requires a new version.",
                "The envelope remains record-only until an explicit execution adapter consumes it under separate approval evidence.",
            ],
            reference_patterns=["microsoft_agent_framework", "google_adk", "openai_agents", "opentelemetry"],
        ),
        SchemaContractDescriptor(
            id="delegate_budget",
            title="Delegate budget contract",
            surface="delegation_budget",
            stability="stable",
            compatibility_policy="additive_only",
            current_schema_version="harness.delegate_budget/v1",
            owner="delegate_budgets",
            produced_by=["ExecutionAdapterDescriptor.delegate_budget", "adapter_delegate_budget_projection"],
            consumed_by=["task metadata validation", "orchestration efficiency audit", "readiness budget_limited_delegation"],
            validation_surfaces=[
                "tests/test_orchestration_efficiency.py",
                "harness evals run --suite orchestration-efficiency --output json",
            ],
            upgrade_notes=[
                "Budget fields may be added with explicit safe defaults; widening runtime, tool, model, or network authority must be modeled as policy and test changes.",
            ],
            reference_patterns=["microsoft_agent_framework", "temporal", "openai_agents"],
        ),
        SchemaContractDescriptor(
            id="task_replay_receipt",
            title="Task retry and attempt replay receipt",
            surface="task_queue",
            stability="stable",
            compatibility_policy="additive_only",
            current_schema_version=TASK_REPLAY_RECEIPT_SCHEMA_VERSION,
            owner="sqlite_store",
            produced_by=[
                "SQLiteStore.retry_task",
                "SQLiteStore.select_next_task_with_guarded_lease",
                "tasks retry",
                "tasks run-next",
            ],
            consumed_by=[
                "task transition audit",
                "attempt metadata inspection",
                "orchestration efficiency replay/idempotency check",
            ],
            validation_surfaces=[
                "tests/test_sqlite_store.py",
                "tests/test_orchestration_efficiency.py",
                "harness evals run --suite orchestration-efficiency --output json",
            ],
            upgrade_notes=[
                "Receipt fields can grow additively; changing retry authorization, idempotency-key, attempt-number, approval-revalidation, or active-lease duplicate-guard semantics requires a new version.",
                "Replay receipts are evidence only and never grant retry, lease, adapter, provider, network, filesystem, or permission authority.",
            ],
            reference_patterns=["temporal", "dapr", "containerd", "microsoft_agent_framework"],
        ),
        SchemaContractDescriptor(
            id="external_protocol_catalog",
            title="External protocol compatibility catalog",
            surface="external_protocol",
            stability="stable",
            compatibility_policy="metadata_projection_only",
            current_schema_version="harness.external_protocol_catalog/v1",
            owner="external_protocols",
            produced_by=["build_external_protocol_catalog", "harness protocols list"],
            consumed_by=["readiness external_protocol_compatibility", "orchestration synthesis"],
            validation_surfaces=[
                "tests/test_external_protocols.py",
                "harness protocols list --output json",
            ],
            upgrade_notes=[
                "Adding protocol descriptors or telemetry contracts is compatible; enabling execution authority requires separate fail-closed policy, replay, identity, trace propagation, and approval evidence.",
            ],
            reference_patterns=["modelcontextprotocol", "A2A", "openapi", "grpc", "opentelemetry"],
        ),
        SchemaContractDescriptor(
            id="orchestration_readiness_audit",
            title="Orchestration readiness audit",
            surface="orchestration_readiness",
            stability="stable",
            compatibility_policy="additive_only",
            current_schema_version="harness.orchestration_readiness_audit/v1",
            owner="orchestration_readiness",
            produced_by=["run_orchestration_readiness_audit", "harness orchestration audit"],
            consumed_by=["doctor --release", "TUI dashboard", "local server /orchestration/readiness", "orchestration synthesis"],
            validation_surfaces=[
                "tests/test_orchestration_readiness.py",
                "harness orchestration audit --output json",
            ],
            upgrade_notes=[
                "Readiness checks may be added; removing or renaming existing check ids requires a new audit schema version.",
            ],
            reference_patterns=["microsoft_agent_framework", "temporal", "langgraph", "opentelemetry"],
        ),
        SchemaContractDescriptor(
            id="orchestration_efficiency_audit",
            title="Orchestration efficiency audit",
            surface="orchestration_efficiency",
            stability="stable",
            compatibility_policy="additive_only",
            current_schema_version="harness.orchestration_efficiency/v1",
            owner="orchestration_efficiency",
            produced_by=["run_orchestration_efficiency_audit", "harness evals run --suite orchestration-efficiency"],
            consumed_by=["doctor --release", "orchestration synthesis"],
            validation_surfaces=[
                "tests/test_orchestration_efficiency.py",
                "harness evals run --suite orchestration-efficiency --output json",
            ],
            upgrade_notes=[
                "New measurements are compatible when safety flags remain passive and release gates retain deterministic semantics.",
            ],
            reference_patterns=["temporal", "langgraph", "containerd", "gvisor", "firecracker"],
        ),
        SchemaContractDescriptor(
            id="orchestration_synthesis_report",
            title="Orchestration synthesis report",
            surface="orchestration_synthesis",
            stability="stable",
            compatibility_policy="additive_only",
            current_schema_version="harness.orchestration_synthesis/v1",
            owner="orchestration_synthesis",
            produced_by=["run_orchestration_synthesis", "harness orchestration synthesis"],
            consumed_by=["doctor --release", "TUI cockpit evidence", "local server /orchestration/synthesis"],
            validation_surfaces=[
                "tests/test_orchestration_efficiency.py",
                "harness evals run --suite orchestration-synthesis --output json",
            ],
            upgrade_notes=[
                "Synthesis source reports and adoption rows can grow additively; changing source-report status semantics requires a new version.",
            ],
            reference_patterns=["microsoft_agent_framework", "temporal", "langgraph", "modelcontextprotocol"],
        ),
        SchemaContractDescriptor(
            id="orchestration_replay_audit",
            title="Orchestration replay drift audit",
            surface="orchestration_replay",
            stability="stable",
            compatibility_policy="additive_only",
            current_schema_version="harness.orchestration_replay_audit/v1",
            owner="orchestration_replay",
            produced_by=["run_orchestration_replay_audit", "harness evals run --suite orchestration-replay"],
            consumed_by=["readiness replay_drift_detection", "orchestration synthesis", "release-gate evidence"],
            validation_surfaces=[
                "tests/test_orchestration_replay.py",
                "harness evals run --suite orchestration-replay --output json",
            ],
            upgrade_notes=[
                "Replay cases and reducer summaries can grow additively; changing issue-code semantics requires test and docs updates.",
                "Replay is a read-only semantic drift detector and never re-executes adapters, providers, tools, or objective actions.",
            ],
            reference_patterns=["temporal", "dapr", "langgraph", "microsoft_agent_framework"],
        ),
        SchemaContractDescriptor(
            id="orchestration_scenario_catalog",
            title="Orchestration scenario conformance catalog",
            surface="orchestration_scenarios",
            stability="stable",
            compatibility_policy="additive_only",
            current_schema_version="harness.orchestration_scenario_catalog/v1",
            owner="orchestration_scenarios",
            produced_by=["build_orchestration_scenario_catalog", "harness orchestration scenarios"],
            consumed_by=[
                "readiness orchestration_scenario_conformance",
                "orchestration synthesis",
                "local server /orchestration/scenarios",
            ],
            validation_surfaces=[
                "tests/test_orchestration_scenarios.py",
                "harness orchestration scenarios --output json",
            ],
            upgrade_notes=[
                "Scenario rows and detected signals can grow additively; changing required case ids or signal meaning requires a new catalog version.",
                "Scenario conformance remains passive and never runs adapters, tools, providers, live benchmarks, or reference code.",
            ],
            reference_patterns=["microsoft_agent_framework", "temporal", "langgraph", "opentelemetry", "owasp_agentic"],
        ),
        SchemaContractDescriptor(
            id="workflow_coordination_catalog",
            title="Workflow coordination pattern catalog",
            surface="workflow_coordination",
            stability="stable",
            compatibility_policy="additive_only",
            current_schema_version="harness.workflow_coordination_catalog/v1",
            owner="orchestration_workflows",
            produced_by=["build_workflow_coordination_catalog", "harness orchestration workflows"],
            consumed_by=[
                "readiness workflow_coordination_contracts",
                "orchestration synthesis",
                "local server /orchestration/workflows",
            ],
            validation_surfaces=[
                "tests/test_orchestration_workflows.py",
                "harness orchestration workflows --output json",
            ],
            upgrade_notes=[
                "Pattern and state-class rows can grow additively; changing required pattern semantics requires a new catalog version.",
                "The catalog is a passive contract surface and never imports or executes reference workflow code.",
            ],
            reference_patterns=["microsoft_agent_framework", "temporal", "langgraph", "google_adk"],
        ),
        SchemaContractDescriptor(
            id="workflow_template",
            title="Reviewed workflow template payload",
            surface="workflow_template",
            stability="stable",
            compatibility_policy="additive_only",
            current_schema_version=WORKFLOW_TEMPLATE_SCHEMA_VERSION,
            owner="workflow_templates",
            produced_by=["template_for_intent", "WorkflowTemplate.to_payload", "chat orchestration drafts"],
            consumed_by=[
                "chat draft-before-confirm action contracts",
                "pending chat action recovery",
                "reviewed objective/task graph creation",
            ],
            validation_surfaces=[
                "tests/test_reviewer_workflows.py",
                "tests/test_operator_chat_path.py",
                "harness --plain / chat orchestration draft payloads",
            ],
            upgrade_notes=[
                "Workflow template payloads can grow additively; removing task graph, checkpoint, approval, or safety-boundary fields requires a new version.",
                "The payload is a draft contract only and cannot create records, acquire leases, dispatch adapters, or grant hosted/apply-back authority until explicit confirmation and separate policy checks.",
            ],
            reference_patterns=["microsoft_agent_framework", "temporal", "langgraph", "openai_agents"],
        ),
        SchemaContractDescriptor(
            id="workflow_agent_selection",
            title="Workflow agent-selection requirements",
            surface="workflow_template",
            stability="stable",
            compatibility_policy="additive_only",
            current_schema_version=WORKFLOW_AGENT_SELECTION_SCHEMA_VERSION,
            owner="workflow_templates",
            produced_by=["WorkflowAgentSelection.to_payload", "template_for_intent"],
            consumed_by=[
                "chat delegate allocation requirements",
                "delegate_allocation receipts",
                "reviewed workflow task metadata",
            ],
            validation_surfaces=[
                "tests/test_reviewer_workflows.py",
                "tests/test_operator_chat_path.py",
                "harness agents allocate --output json",
            ],
            upgrade_notes=[
                "Requirement fields can grow additively; changing matching semantics for kind, tool policy, outputs, or tags requires a new version and allocation tests.",
                "Agent selection requirements remain metadata for deterministic bidding and never grant runtime, provider, network, tool, budget, filesystem, or permission authority.",
            ],
            reference_patterns=["contract_net", "a2a_agent_card", "microsoft_agent_framework", "google_adk"],
        ),
        SchemaContractDescriptor(
            id="objective_batch_plan",
            title="Objective batch plan event payload",
            surface="objective_evidence",
            stability="stable",
            compatibility_policy="additive_only",
            current_schema_version=OBJECTIVE_BATCH_PLAN_SCHEMA_VERSION,
            owner="objective_batch_plan",
            produced_by=["run_objective_parallel", "batch_planned objective evidence"],
            consumed_by=["objective evidence verifier", "objective trace export", "replay drift detection"],
            validation_surfaces=[
                "tests/test_objective_runner.py",
                "harness objectives verify-evidence <objective_id> --output json",
            ],
            upgrade_notes=[
                "Batch-plan fields can grow additively; scheduler-policy sort keys, selected pair/source, or dependency snapshot semantics require a new version.",
                "Batch plans are evidence records only and do not grant additional dispatch, lease, or parallel execution authority.",
            ],
            reference_patterns=["langgraph", "temporal", "microsoft_agent_framework"],
        ),
        SchemaContractDescriptor(
            id="objective_evidence_chain",
            title="Append-only objective evidence chain",
            surface="objective_evidence",
            stability="stable",
            compatibility_policy="append_only_hash_chained",
            current_schema_version="harness.autonomous_objective_event/v1",
            owner="objective_evidence",
            produced_by=["objective runner evidence writes", "reconcile objective evidence"],
            consumed_by=["verify objective evidence", "readiness append_only_objective_evidence", "trace export"],
            validation_surfaces=[
                "tests/test_orchestration_readiness.py",
                "harness objectives verify-evidence <objective_id> --output json",
            ],
            upgrade_notes=[
                "Evidence events are append-only and hash chained; changing event semantics requires migration evidence or a new chain version.",
            ],
            reference_patterns=["temporal", "langgraph", "opentelemetry"],
        ),
        SchemaContractDescriptor(
            id="objective_checkpoint_chain",
            title="Supervisor checkpoint evidence chain",
            surface="objective_checkpoint",
            stability="stable",
            compatibility_policy="append_only_hash_chained",
            current_schema_version="harness.objective_checkpoint_event/v1",
            owner="objective_checkpoints",
            produced_by=["create_objective_checkpoint", "resolve_objective_checkpoint"],
            consumed_by=["checkpoint gate evaluation", "readiness supervisor_checkpoints"],
            validation_surfaces=[
                "tests/test_orchestration_readiness.py",
                "harness objectives checkpoints verify <objective_id> --output json",
            ],
            upgrade_notes=[
                "Checkpoint evidence is append-only and approval-bound; changing verdict semantics requires a new version.",
            ],
            reference_patterns=["microsoft_agent_framework", "temporal", "langgraph"],
        ),
        SchemaContractDescriptor(
            id="trace_export",
            title="Trace export contract",
            surface="trace_export",
            stability="stable",
            compatibility_policy="additive_only",
            current_schema_version="harness.trace_export/v1",
            owner="traces",
            produced_by=["export_run_trace", "export_objective_trace", "harness traces export"],
            consumed_by=["readiness otel_trace_export", "orchestration synthesis", "operator observability"],
            validation_surfaces=[
                "tests/test_evals_traces_v0_3_5.py",
                "harness traces export --run <run_id> --output json",
            ],
            upgrade_notes=[
                "Trace attributes and semantic-convention metadata may be added; changing span identity, parentage, or redaction semantics requires a new version.",
                "External protocol adapters must preserve W3C trace context and use GenAI/MCP-compatible low-cardinality attributes before they can execute.",
            ],
            reference_patterns=["opentelemetry", "temporal"],
        ),
        SchemaContractDescriptor(
            id="sandbox_profile_catalog",
            title="Sandbox profile catalog",
            surface="sandbox_profile",
            stability="stable",
            compatibility_policy="additive_only",
            current_schema_version="harness.sandbox_profiles/v1",
            owner="sandbox_profiles",
            produced_by=["build_sandbox_profile_catalog", "harness sandbox profiles"],
            consumed_by=[
                "capability catalog sandbox projections",
                "adapter security-versus-complexity audit",
                "security-layer sandbox checks",
            ],
            validation_surfaces=[
                "tests/test_sandbox_profiles.py",
                "tests/test_orchestration_efficiency.py",
                "harness sandbox profiles --output json",
            ],
            upgrade_notes=[
                "Profile rows can be added additively; changing network, filesystem, active-repo-write, mount, or secret-path semantics requires a new version.",
                "The catalog is a control-plane contract only and never starts Docker, Codex, shells, providers, sandboxes, or low-level runtimes.",
            ],
            reference_patterns=["containerd", "runc", "gvisor", "firecracker", "openai_agents"],
        ),
        SchemaContractDescriptor(
            id="sandbox_profile",
            title="Sandbox profile descriptor",
            surface="sandbox_profile",
            stability="stable",
            compatibility_policy="additive_only",
            current_schema_version="harness.sandbox_profile/v1",
            owner="sandbox_profiles",
            produced_by=["get_sandbox_profile", "harness sandbox inspect"],
            consumed_by=[
                "execution adapter descriptors",
                "run manifests",
                "blocked-state explanations",
                "capability inspect",
            ],
            validation_surfaces=[
                "tests/test_sandbox_profiles.py",
                "tests/test_evals_traces_v0_3_5.py",
                "harness sandbox inspect <profile_id> --output json",
            ],
            upgrade_notes=[
                "Descriptor fields can grow additively; relaxing network, host filesystem, active-repo-write, forbidden-mount, or secret-path semantics requires explicit policy and test updates.",
                "Profiles describe required isolation posture; they are not permission grants and do not execute low-level isolation runtimes by themselves.",
            ],
            reference_patterns=["containerd", "runc", "gvisor", "firecracker", "bubblewrap", "nsjail"],
        ),
        SchemaContractDescriptor(
            id="session_tool_policy_projection",
            title="Session tool policy projection",
            surface="session_tooling",
            stability="stable",
            compatibility_policy="additive_only",
            current_schema_version="harness.session_tool_policy_projection/v1",
            owner="session_tools",
            produced_by=["session_tool_catalog_projection", "harness tools catalog"],
            consumed_by=["model-visible tool schema generation", "readiness protocol_and_tool_exposure"],
            validation_surfaces=[
                "tests/test_session_tool_catalog_contract.py",
                "harness tools catalog --output json",
            ],
            upgrade_notes=[
                "Tool metadata can grow additively; model-visible argument schema loosening requires explicit readiness and golden-test updates.",
            ],
            reference_patterns=["openai_agents", "microsoft_agent_framework", "modelcontextprotocol"],
        ),
        SchemaContractDescriptor(
            id="local_server_openapi",
            title="Local server OpenAPI metadata",
            surface="local_server",
            stability="stable",
            compatibility_policy="metadata_projection_only",
            current_schema_version="harness.local_server.openapi/v1",
            owner="local_server",
            produced_by=["build_openapi_spec", "harness serve --openapi"],
            consumed_by=["external protocol catalog", "attached clients"],
            validation_surfaces=[
                "tests/test_local_server.py",
                "harness serve --openapi --output json",
            ],
            upgrade_notes=[
                "OpenAPI metadata can be extended; route behavior remains governed by server auth, local-only binding, and per-route authority checks.",
            ],
            reference_patterns=["openapi", "modelcontextprotocol"],
        ),
    ]


def _is_harness_v1_schema(schema_version: str) -> bool:
    return schema_version.startswith("harness.") and "/v" in schema_version


def _authority_is_passive(authority: SchemaContractAuthority) -> bool:
    payload = authority.model_dump(mode="json")
    return (
        payload.get("read_only_projection") is True
        and payload.get("validation_only") is True
        and all(
            payload.get(key) is False
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
