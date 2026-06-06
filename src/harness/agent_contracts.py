from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from harness.memory.sqlite_store import SQLiteStore
from harness.models import ProjectAgentRecord
from harness.paths import resolve_project_root
from harness.registry import SpecRegistry, builtin_spec_registry
from harness.security import sanitize_for_logging
from harness.specs import AgentProfileSpec, AgentSpec, ToolPermission, ToolPolicy


AGENT_CONTRACT_SCHEMA_VERSION = "harness.agent_contract/v1"
AGENT_CONTRACT_AUTHORITY_SCHEMA_VERSION = "harness.agent_contract_authority/v1"
AGENT_CONTRACT_TOOL_POLICY_SCHEMA_VERSION = "harness.agent_contract_tool_policy/v1"
AGENT_CONTRACT_BUDGET_POLICY_SCHEMA_VERSION = "harness.agent_contract_budget_policy/v1"
AGENT_CONTRACT_TRACE_POLICY_SCHEMA_VERSION = "harness.agent_contract_trace_policy/v1"
AGENT_TASK_INPUT_CONTRACT_SCHEMA_VERSION = "harness.agent_task_request/v1"

AgentContractSourceKind = Literal["builtin", "project", "unknown"]


class AgentContractAuthority(BaseModel):
    schema_version: str = AGENT_CONTRACT_AUTHORITY_SCHEMA_VERSION
    read_only_projection: bool = True
    identity_authority: bool = False
    orchestration_policy_authority: bool = False
    budget_authority: bool = False
    adapter_execution_allowed: bool = False
    agent_execution_allowed: bool = False
    model_execution_allowed: bool = False
    tool_execution_allowed: bool = False
    process_start_allowed: bool = False
    network_allowed: bool = False
    filesystem_mutation_allowed: bool = False
    credential_access_allowed: bool = False
    permission_granting: bool = False
    model_context_allowed: bool = False


class AgentContractToolPolicy(BaseModel):
    schema_version: str = AGENT_CONTRACT_TOOL_POLICY_SCHEMA_VERSION
    tool_policy_id: str | None = None
    permissions: dict[str, str] = Field(default_factory=dict)
    allowed_tool_ids: list[str] = Field(default_factory=list)
    approval_required_tool_ids: list[str] = Field(default_factory=list)
    forbidden_tool_ids: list[str] = Field(default_factory=list)
    network: str | None = None
    active_repo_write: str | None = None
    hosted_boundary: str | None = None


class AgentContractBudgetPolicy(BaseModel):
    schema_version: str = AGENT_CONTRACT_BUDGET_POLICY_SCHEMA_VERSION
    budget_source: str = "execution_adapter_or_handoff"
    per_handoff_budget_required: bool = True
    agent_may_increase_budget: bool = False
    agent_may_grant_tools: bool = False
    agent_may_grant_network: bool = False
    agent_may_grant_credentials: bool = False
    max_autonomous_retries: int = 0
    notes: list[str] = Field(
        default_factory=lambda: [
            "Agent identity is metadata only; delegate budgets are supplied by execution adapters and handoff envelopes.",
            "The agent contract cannot widen tool, model, network, filesystem, credential, or retry authority.",
        ]
    )


class AgentContractTracePolicy(BaseModel):
    schema_version: str = AGENT_CONTRACT_TRACE_POLICY_SCHEMA_VERSION
    trace_required: bool = True
    w3c_traceparent_required: bool = True
    correlation_id_required: bool = True
    handoff_envelope_schema_version: str = "harness.agent_handoff_envelope/v1"
    span_payload_hash_required: bool = True


class AgentContract(BaseModel):
    schema_version: str = AGENT_CONTRACT_SCHEMA_VERSION
    ok: bool
    contract_id: str
    contract_sha256: str
    agent_id: str
    source_kind: AgentContractSourceKind
    project_root: Path | None = None
    source_path: Path | None = None
    source_content_sha256: str | None = None
    workbench_id: str | None = None
    kind: str | None = None
    role: str | None = None
    parent_chain: list[str] = Field(default_factory=list)
    model_profile: str | None = None
    model_profile_kind: str | None = None
    backend_id: str | None = None
    tool_policy_id: str | None = None
    memory_scope: str | None = None
    input_contracts: list[str] = Field(default_factory=lambda: [AGENT_TASK_INPUT_CONTRACT_SCHEMA_VERSION])
    output_contracts: list[str] = Field(default_factory=list)
    declared_outputs: list[str] = Field(default_factory=list)
    profile_ids: list[str] = Field(default_factory=list)
    preferred_outputs: list[str] = Field(default_factory=list)
    review_responsibilities: list[str] = Field(default_factory=list)
    knowledge_domains: list[str] = Field(default_factory=list)
    forbidden_actions: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    tool_policy: AgentContractToolPolicy = Field(default_factory=AgentContractToolPolicy)
    budget_policy: AgentContractBudgetPolicy = Field(default_factory=AgentContractBudgetPolicy)
    trace_policy: AgentContractTracePolicy = Field(default_factory=AgentContractTracePolicy)
    authority: AgentContractAuthority = Field(default_factory=AgentContractAuthority)
    safety: dict[str, bool] = Field(default_factory=dict)
    validation_errors: list[str] = Field(default_factory=list)
    next_actions: list[str] = Field(default_factory=list)


def build_agent_contract(
    project_root: Path,
    agent_id: str | None,
    *,
    workbench_id: str | None = None,
    allow_project_agents: bool = True,
) -> AgentContract:
    """Return a passive, canonical identity contract for a built-in or imported agent."""

    root = resolve_project_root(project_root)
    normalized_agent_id = str(agent_id or "").strip()
    if not normalized_agent_id:
        return _unknown_agent_contract(root, "unconfigured", ["agent_id is required"])

    registry = builtin_spec_registry()
    if normalized_agent_id in registry.agents:
        return _contract_from_registry(
            root,
            registry,
            normalized_agent_id,
            source_kind="builtin",
            workbench_id=workbench_id,
        )

    if allow_project_agents:
        store_path = root / ".harness" / "harness.sqlite"
        if store_path.exists():
            try:
                record = SQLiteStore(root).get_project_agent(normalized_agent_id)
            except KeyError:
                pass
            else:
                return _contract_from_project_record(root, record, workbench_id=workbench_id)

    return _unknown_agent_contract(root, normalized_agent_id, [f"Agent not found: {normalized_agent_id}"])


def build_builtin_agent_contract(agent_id: str) -> AgentContract:
    registry = builtin_spec_registry()
    if agent_id not in registry.agents:
        return _unknown_agent_contract(None, agent_id, [f"Built-in agent not found: {agent_id}"])
    return _contract_from_registry(None, registry, agent_id, source_kind="builtin", workbench_id=None)


def _contract_from_project_record(
    project_root: Path,
    record: ProjectAgentRecord,
    *,
    workbench_id: str | None,
) -> AgentContract:
    builtin = builtin_spec_registry()
    agent = AgentSpec.model_validate(record.agent)
    profiles = [AgentProfileSpec.model_validate(profile) for profile in record.profiles]
    registry = SpecRegistry(
        model_profiles=dict(builtin.model_profiles),
        tool_policies=dict(builtin.tool_policies),
        memory_scopes=dict(builtin.memory_scopes),
        agents={**builtin.agents, record.agent_id: agent},
        agent_profiles={**builtin.agent_profiles, **{profile.id: profile for profile in profiles}},
        workbenches=dict(builtin.workbenches),
    )
    return _contract_from_registry(
        project_root,
        registry,
        record.agent_id,
        source_kind="project",
        workbench_id=workbench_id or record.workbench_id,
        source_path=record.source_path,
        source_content_sha256=record.content_sha256,
    )


def _contract_from_registry(
    project_root: Path | None,
    registry: SpecRegistry,
    agent_id: str,
    *,
    source_kind: AgentContractSourceKind,
    workbench_id: str | None,
    source_path: Path | None = None,
    source_content_sha256: str | None = None,
) -> AgentContract:
    errors: list[str] = []
    agent = registry.agents.get(agent_id)
    if agent is None:
        return _unknown_agent_contract(project_root, agent_id, [f"Agent not found: {agent_id}"])
    effective = registry.resolve_agent_effective_spec(agent_id)
    profiles = registry.list_agent_profiles(agent_id)
    model_profile = registry.model_profiles.get(agent.model_profile)
    tool_policy = registry.tool_policies.get(agent.tool_policy)
    if model_profile is None:
        errors.append(f"model_profile not found: {agent.model_profile}")
    if tool_policy is None:
        errors.append(f"tool_policy not found: {agent.tool_policy}")
    if agent.memory_scope not in registry.memory_scopes:
        errors.append(f"memory_scope not found: {agent.memory_scope}")
    if workbench_id:
        workbench = registry.workbenches.get(workbench_id)
        if workbench is None:
            errors.append(f"workbench not found: {workbench_id}")
        elif agent_id not in workbench.allowed_agents and source_kind != "project":
            errors.append(f"agent {agent_id} is not allowed in workbench {workbench_id}")

    profile_ids = [profile.id for profile in profiles]
    preferred_outputs = _merge_unique(item for profile in profiles for item in profile.preferred_outputs)
    review_responsibilities = _merge_unique(item for profile in profiles for item in profile.review_responsibilities)
    knowledge_domains = _merge_unique(item for profile in profiles for item in profile.knowledge_domains)
    forbidden_actions = _merge_unique(item for profile in profiles for item in profile.forbidden_actions)
    tags = _merge_unique([*effective.get("tags", []), *(item for profile in profiles for item in profile.tags)])
    declared_outputs = _merge_unique(effective.get("outputs", []))
    output_contracts = _merge_unique([*declared_outputs, *preferred_outputs])
    tool_projection = _tool_policy_projection(agent.tool_policy, tool_policy)
    contract_payload = {
        "agent_id": agent_id,
        "source_kind": source_kind,
        "workbench_id": workbench_id,
        "kind": agent.kind.value,
        "role": agent.role,
        "parent_chain": effective.get("parent_chain", []),
        "model_profile": agent.model_profile,
        "model_profile_kind": model_profile.kind.value if model_profile is not None else None,
        "backend_id": model_profile.backend if model_profile is not None else None,
        "tool_policy_id": agent.tool_policy,
        "memory_scope": agent.memory_scope,
        "input_contracts": [AGENT_TASK_INPUT_CONTRACT_SCHEMA_VERSION],
        "output_contracts": output_contracts,
        "declared_outputs": declared_outputs,
        "profile_ids": profile_ids,
        "preferred_outputs": preferred_outputs,
        "review_responsibilities": review_responsibilities,
        "knowledge_domains": knowledge_domains,
        "forbidden_actions": forbidden_actions,
        "tags": tags,
        "tool_policy": tool_projection.model_dump(mode="json"),
        "budget_policy": AgentContractBudgetPolicy().model_dump(mode="json"),
        "trace_policy": AgentContractTracePolicy().model_dump(mode="json"),
        "source_content_sha256": source_content_sha256,
    }
    contract_sha = _stable_json_sha256(contract_payload)
    return AgentContract(
        ok=not errors,
        contract_id="agent_contract_" + contract_sha[:16],
        contract_sha256=contract_sha,
        agent_id=agent_id,
        source_kind=source_kind,
        project_root=project_root,
        source_path=source_path,
        source_content_sha256=source_content_sha256,
        workbench_id=workbench_id,
        kind=agent.kind.value,
        role=agent.role,
        parent_chain=list(effective.get("parent_chain", [])),
        model_profile=agent.model_profile,
        model_profile_kind=model_profile.kind.value if model_profile is not None else None,
        backend_id=model_profile.backend if model_profile is not None else None,
        tool_policy_id=agent.tool_policy,
        memory_scope=agent.memory_scope,
        output_contracts=output_contracts,
        declared_outputs=declared_outputs,
        profile_ids=profile_ids,
        preferred_outputs=preferred_outputs,
        review_responsibilities=review_responsibilities,
        knowledge_domains=knowledge_domains,
        forbidden_actions=forbidden_actions,
        tags=tags,
        tool_policy=tool_projection,
        safety=_contract_safety(),
        validation_errors=errors,
        next_actions=[] if not errors else ["Fix the agent spec registry before using this agent in delegated work."],
    )


def _unknown_agent_contract(
    project_root: Path | None,
    agent_id: str,
    errors: list[str],
) -> AgentContract:
    payload = {
        "agent_id": agent_id,
        "source_kind": "unknown",
        "validation_errors": errors,
    }
    contract_sha = _stable_json_sha256(payload)
    return AgentContract(
        ok=False,
        contract_id="agent_contract_" + contract_sha[:16],
        contract_sha256=contract_sha,
        agent_id=agent_id,
        source_kind="unknown",
        project_root=project_root,
        safety=_contract_safety(),
        validation_errors=errors,
        next_actions=["Import or define the agent before using it in delegated work."],
    )


def _tool_policy_projection(policy_id: str | None, policy: ToolPolicy | None) -> AgentContractToolPolicy:
    if policy is None:
        return AgentContractToolPolicy(tool_policy_id=policy_id)
    permissions = {tool_id: permission.value for tool_id, permission in sorted(policy.tools.items())}
    return AgentContractToolPolicy(
        tool_policy_id=policy_id,
        permissions=permissions,
        allowed_tool_ids=[tool_id for tool_id, permission in sorted(policy.tools.items()) if permission == ToolPermission.ALLOWED],
        approval_required_tool_ids=[
            tool_id for tool_id, permission in sorted(policy.tools.items()) if permission == ToolPermission.APPROVAL_REQUIRED
        ],
        forbidden_tool_ids=[tool_id for tool_id, permission in sorted(policy.tools.items()) if permission == ToolPermission.FORBIDDEN],
        network=policy.network.value,
        active_repo_write=policy.active_repo_write.value,
        hosted_boundary=policy.hosted_boundary.value,
    )


def _contract_safety() -> dict[str, bool]:
    return {
        "read_only": True,
        "filesystem_modified": False,
        "network_called": False,
        "provider_called": False,
        "model_execution_started": False,
        "adapter_execution_started": False,
        "tool_execution_started": False,
        "agent_execution_started": False,
        "credential_accessed": False,
        "permission_granting": False,
        "source_body_loaded": False,
    }


def _merge_unique(values) -> list[str]:
    merged: list[str] = []
    for value in values:
        text = str(value).strip()
        if text and text not in merged:
            merged.append(text)
    return merged


def _stable_json_sha256(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(sanitize_for_logging(payload), sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
