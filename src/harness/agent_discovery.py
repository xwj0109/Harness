from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from harness.agent_contracts import AgentContract, build_agent_contract
from harness.memory.sqlite_store import SQLiteStore
from harness.paths import resolve_project_root
from harness.registry import builtin_spec_registry
from harness.security import sanitize_for_logging
from harness.specs import AgentKind


AGENT_DISCOVERY_CATALOG_SCHEMA_VERSION = "harness.agent_discovery_catalog/v1"
AGENT_DISCOVERY_CARD_SCHEMA_VERSION = "harness.agent_discovery_card/v1"
DELEGATE_TASK_ANNOUNCEMENT_SCHEMA_VERSION = "harness.delegate_task_announcement/v1"
DELEGATE_BID_SCHEMA_VERSION = "harness.delegate_bid/v1"
DELEGATE_ALLOCATION_SCHEMA_VERSION = "harness.delegate_allocation/v1"
AGENT_DISCOVERY_SUMMARY_SCHEMA_VERSION = "harness.agent_discovery_summary/v1"

AgentDiscoveryStatus = Literal["discoverable", "invalid", "not_allowed"]
AgentDiscoverySourceKind = Literal["builtin_registry", "project_metadata"]
DelegateBidStatus = Literal["eligible", "ineligible"]


class AgentDiscoveryCard(BaseModel):
    schema_version: str = AGENT_DISCOVERY_CARD_SCHEMA_VERSION
    card_id: str
    card_sha256: str
    agent_id: str
    source_kind: AgentDiscoverySourceKind
    status: AgentDiscoveryStatus
    workbench_ids: list[str] = Field(default_factory=list)
    requested_workbench_id: str | None = None
    allowed_in_requested_workbench: bool = True
    kind: str | None = None
    role: str | None = None
    parent_chain: list[str] = Field(default_factory=list)
    model_profile: str | None = None
    backend_id: str | None = None
    tool_policy_id: str | None = None
    memory_scope: str | None = None
    output_contracts: list[str] = Field(default_factory=list)
    declared_outputs: list[str] = Field(default_factory=list)
    preferred_outputs: list[str] = Field(default_factory=list)
    review_responsibilities: list[str] = Field(default_factory=list)
    knowledge_domains: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    forbidden_actions: list[str] = Field(default_factory=list)
    contract_id: str | None = None
    contract_sha256: str | None = None
    authority: dict[str, Any] = Field(default_factory=dict)
    discovery: dict[str, Any] = Field(default_factory=dict)
    safety: dict[str, bool] = Field(default_factory=dict)
    validation_errors: list[str] = Field(default_factory=list)


class DelegateTaskAnnouncement(BaseModel):
    schema_version: str = DELEGATE_TASK_ANNOUNCEMENT_SCHEMA_VERSION
    announcement_id: str
    project_root: Path
    workbench_id: str | None = None
    task_type: str | None = None
    required_kind: str | None = None
    required_tool_policy_id: str | None = None
    required_outputs: list[str] = Field(default_factory=list)
    required_tags: list[str] = Field(default_factory=list)
    required_knowledge_domains: list[str] = Field(default_factory=list)
    required_review_responsibilities: list[str] = Field(default_factory=list)
    required_forbidden_actions: list[str] = Field(default_factory=list)
    excluded_agent_ids: list[str] = Field(default_factory=list)
    max_candidates: int = 3
    prefer_read_only: bool = True
    contents_included: bool = False
    authority: dict[str, bool] = Field(default_factory=dict)


class DelegateBid(BaseModel):
    schema_version: str = DELEGATE_BID_SCHEMA_VERSION
    bid_id: str
    announcement_id: str
    agent_id: str
    status: DelegateBidStatus
    score: int
    matched: dict[str, list[str]] = Field(default_factory=dict)
    rejected_reasons: list[str] = Field(default_factory=list)
    bid_terms: dict[str, Any] = Field(default_factory=dict)
    safety: dict[str, bool] = Field(default_factory=dict)


class DelegateAllocation(BaseModel):
    schema_version: str = DELEGATE_ALLOCATION_SCHEMA_VERSION
    ok: bool
    project_root: Path
    announcement: DelegateTaskAnnouncement
    selected_agent_ids: list[str] = Field(default_factory=list)
    selected_bid_ids: list[str] = Field(default_factory=list)
    bids: list[DelegateBid] = Field(default_factory=list)
    summary: dict[str, int] = Field(default_factory=dict)
    safety: dict[str, bool] = Field(default_factory=dict)
    next_actions: list[str] = Field(default_factory=list)


class AgentDiscoveryCatalog(BaseModel):
    schema_version: str = AGENT_DISCOVERY_CATALOG_SCHEMA_VERSION
    ok: bool
    project_root: Path
    initialized: bool
    workbench_id: str | None = None
    cards: list[AgentDiscoveryCard] = Field(default_factory=list)
    sample_allocation: DelegateAllocation | None = None
    summary: dict[str, int] = Field(default_factory=dict)
    safety: dict[str, bool] = Field(default_factory=dict)
    validation_errors: list[str] = Field(default_factory=list)


def build_agent_discovery_catalog(
    project_root: Path,
    *,
    workbench_id: str | None = None,
    include_project_agents: bool = True,
    include_sample_allocation: bool = True,
) -> AgentDiscoveryCatalog:
    root = resolve_project_root(project_root)
    registry = builtin_spec_registry()
    initialized = (root / ".harness" / "harness.sqlite").exists()
    validation_errors: list[str] = []
    workbench = registry.workbenches.get(workbench_id) if workbench_id else None
    if workbench_id and workbench is None:
        validation_errors.append(f"Workbench not found: {workbench_id}")

    builtin_agent_ids = sorted(registry.agents)
    if workbench is not None:
        builtin_agent_ids = [agent_id for agent_id in builtin_agent_ids if agent_id in set(workbench.allowed_agents)]

    cards = [
        _card_from_contract(
            root,
            build_agent_contract(root, agent_id, workbench_id=workbench_id),
            source_kind="builtin_registry",
            requested_workbench_id=workbench_id,
            workbench_ids=_workbench_ids_for_builtin_agent(registry, agent_id),
        )
        for agent_id in builtin_agent_ids
    ]
    if include_project_agents and initialized:
        cards.extend(_project_agent_cards(root, requested_workbench_id=workbench_id))

    cards.sort(key=lambda card: (card.source_kind, card.agent_id))
    sample_allocation = None
    if include_sample_allocation and not validation_errors:
        sample_allocation = evaluate_delegate_allocation(
            root,
            workbench_id=workbench_id or "coding",
            required_kind="reviewer",
            required_tags=["security"] if (workbench_id or "coding") == "coding" else [],
            required_review_responsibilities=["risk"] if (workbench_id or "coding") == "quant" else [],
            max_candidates=1,
            cards=cards if workbench_id else None,
        )

    safety = _safety_flags()
    invalid_cards = [card.agent_id for card in cards if card.status != "discoverable"]
    ok = not validation_errors and not invalid_cards and _safety_is_passive(safety)
    return AgentDiscoveryCatalog(
        ok=ok,
        project_root=root,
        initialized=initialized,
        workbench_id=workbench_id,
        cards=cards,
        sample_allocation=sample_allocation,
        summary={
            "card_count": len(cards),
            "discoverable_count": sum(1 for card in cards if card.status == "discoverable"),
            "invalid_count": sum(1 for card in cards if card.status == "invalid"),
            "not_allowed_count": sum(1 for card in cards if card.status == "not_allowed"),
            "builtin_count": sum(1 for card in cards if card.source_kind == "builtin_registry"),
            "project_count": sum(1 for card in cards if card.source_kind == "project_metadata"),
            "sample_selected_count": len(sample_allocation.selected_agent_ids) if sample_allocation is not None else 0,
        },
        safety=safety,
        validation_errors=validation_errors,
    )


def evaluate_delegate_allocation(
    project_root: Path,
    *,
    workbench_id: str | None = None,
    task_type: str | None = None,
    required_kind: Literal["orchestrator", "group", "specialist", "reviewer"] | None = None,
    required_tool_policy_id: str | None = None,
    required_outputs: list[str] | None = None,
    required_tags: list[str] | None = None,
    required_knowledge_domains: list[str] | None = None,
    required_review_responsibilities: list[str] | None = None,
    required_forbidden_actions: list[str] | None = None,
    excluded_agent_ids: list[str] | None = None,
    max_candidates: int = 3,
    prefer_read_only: bool = True,
    cards: list[AgentDiscoveryCard] | None = None,
) -> DelegateAllocation:
    root = resolve_project_root(project_root)
    announcement = _announcement(
        root,
        workbench_id=workbench_id,
        task_type=task_type,
        required_kind=required_kind,
        required_tool_policy_id=required_tool_policy_id,
        required_outputs=_normalized_list(required_outputs),
        required_tags=_normalized_list(required_tags),
        required_knowledge_domains=_normalized_list(required_knowledge_domains),
        required_review_responsibilities=_normalized_list(required_review_responsibilities),
        required_forbidden_actions=_normalized_list(required_forbidden_actions),
        excluded_agent_ids=_normalized_list(excluded_agent_ids),
        max_candidates=max(1, int(max_candidates or 1)),
        prefer_read_only=prefer_read_only,
    )
    candidate_cards = cards
    if candidate_cards is None:
        candidate_cards = build_agent_discovery_catalog(
            root,
            workbench_id=workbench_id,
            include_sample_allocation=False,
        ).cards
    bids = [_bid_for_card(announcement, card) for card in candidate_cards]
    eligible = sorted(
        [bid for bid in bids if bid.status == "eligible"],
        key=lambda bid: (-bid.score, bid.agent_id),
    )
    selected = eligible[: announcement.max_candidates]
    safety = _safety_flags()
    return DelegateAllocation(
        ok=bool(selected) and _safety_is_passive(safety),
        project_root=root,
        announcement=announcement,
        selected_agent_ids=[bid.agent_id for bid in selected],
        selected_bid_ids=[bid.bid_id for bid in selected],
        bids=sorted(bids, key=lambda bid: (-bid.score, bid.agent_id)),
        summary={
            "bid_count": len(bids),
            "eligible_count": len(eligible),
            "ineligible_count": len(bids) - len(eligible),
            "selected_count": len(selected),
            "max_candidates": announcement.max_candidates,
        },
        safety=safety,
        next_actions=[] if selected else ["Adjust task requirements or import a matching project agent before delegating."],
    )


def summarize_agent_discovery(catalog: AgentDiscoveryCatalog) -> dict[str, Any]:
    return {
        "schema_version": AGENT_DISCOVERY_SUMMARY_SCHEMA_VERSION,
        "ok": catalog.ok,
        "status": "pass" if catalog.ok else "fail",
        "initialized": catalog.initialized,
        "workbench_id": catalog.workbench_id,
        "summary": dict(catalog.summary),
        "agent_ids": [card.agent_id for card in catalog.cards],
        "selected_sample_agent_ids": []
        if catalog.sample_allocation is None
        else list(catalog.sample_allocation.selected_agent_ids),
        "validation_errors": list(catalog.validation_errors),
        "safety": dict(catalog.safety),
        "command": f"harness agents discover --project {catalog.project_root} --output json",
    }


def _project_agent_cards(root: Path, *, requested_workbench_id: str | None) -> list[AgentDiscoveryCard]:
    cards: list[AgentDiscoveryCard] = []
    try:
        records = SQLiteStore(root).list_project_agents()
    except Exception:
        return cards
    for record in records:
        if requested_workbench_id and record.workbench_id != requested_workbench_id:
            continue
        contract = build_agent_contract(root, record.agent_id, workbench_id=record.workbench_id)
        cards.append(
            _card_from_contract(
                root,
                contract,
                source_kind="project_metadata",
                requested_workbench_id=requested_workbench_id,
                workbench_ids=[record.workbench_id],
            )
        )
    return cards


def _card_from_contract(
    root: Path,
    contract: AgentContract,
    *,
    source_kind: AgentDiscoverySourceKind,
    requested_workbench_id: str | None,
    workbench_ids: list[str],
) -> AgentDiscoveryCard:
    allowed = requested_workbench_id is None or requested_workbench_id in set(workbench_ids)
    status: AgentDiscoveryStatus = "discoverable" if contract.ok and allowed else "invalid" if not contract.ok else "not_allowed"
    payload = {
        "agent_id": contract.agent_id,
        "source_kind": source_kind,
        "status": status,
        "workbench_ids": workbench_ids,
        "requested_workbench_id": requested_workbench_id,
        "contract_id": contract.contract_id,
        "contract_sha256": contract.contract_sha256,
    }
    card_sha = _stable_json_sha256(payload)
    return AgentDiscoveryCard(
        card_id="agent_card_" + card_sha[:16],
        card_sha256=card_sha,
        agent_id=contract.agent_id,
        source_kind=source_kind,
        status=status,
        workbench_ids=workbench_ids,
        requested_workbench_id=requested_workbench_id,
        allowed_in_requested_workbench=allowed,
        kind=contract.kind,
        role=contract.role,
        parent_chain=list(contract.parent_chain),
        model_profile=contract.model_profile,
        backend_id=contract.backend_id,
        tool_policy_id=contract.tool_policy_id,
        memory_scope=contract.memory_scope,
        output_contracts=list(contract.output_contracts),
        declared_outputs=list(contract.declared_outputs),
        preferred_outputs=list(contract.preferred_outputs),
        review_responsibilities=list(contract.review_responsibilities),
        knowledge_domains=list(contract.knowledge_domains),
        tags=list(contract.tags),
        forbidden_actions=list(contract.forbidden_actions),
        contract_id=contract.contract_id,
        contract_sha256=contract.contract_sha256,
        authority=contract.authority.model_dump(mode="json"),
        discovery={
            "mode": "static_registry" if source_kind == "builtin_registry" else "project_metadata",
            "transport": "local_metadata",
            "remote_discovery_enabled": False,
            "remote_agent_execution_enabled": False,
            "agent_card_pattern": "a2a_inspired_local_card",
        },
        safety={
            **_safety_flags(),
            "source_body_loaded": bool(contract.safety.get("source_body_loaded")),
        },
        validation_errors=list(contract.validation_errors),
    )


def _announcement(
    project_root: Path,
    *,
    workbench_id: str | None,
    task_type: str | None,
    required_kind: str | None,
    required_tool_policy_id: str | None,
    required_outputs: list[str],
    required_tags: list[str],
    required_knowledge_domains: list[str],
    required_review_responsibilities: list[str],
    required_forbidden_actions: list[str],
    excluded_agent_ids: list[str],
    max_candidates: int,
    prefer_read_only: bool,
) -> DelegateTaskAnnouncement:
    payload = {
        "project_root": str(project_root),
        "workbench_id": workbench_id,
        "task_type": task_type,
        "required_kind": required_kind,
        "required_tool_policy_id": required_tool_policy_id,
        "required_outputs": required_outputs,
        "required_tags": required_tags,
        "required_knowledge_domains": required_knowledge_domains,
        "required_review_responsibilities": required_review_responsibilities,
        "required_forbidden_actions": required_forbidden_actions,
        "excluded_agent_ids": excluded_agent_ids,
        "max_candidates": max_candidates,
        "prefer_read_only": prefer_read_only,
    }
    return DelegateTaskAnnouncement(
        announcement_id="delegate_announce_" + _stable_json_sha256(payload)[:16],
        project_root=project_root,
        workbench_id=workbench_id,
        task_type=task_type,
        required_kind=required_kind,
        required_tool_policy_id=required_tool_policy_id,
        required_outputs=required_outputs,
        required_tags=required_tags,
        required_knowledge_domains=required_knowledge_domains,
        required_review_responsibilities=required_review_responsibilities,
        required_forbidden_actions=required_forbidden_actions,
        excluded_agent_ids=excluded_agent_ids,
        max_candidates=max_candidates,
        prefer_read_only=prefer_read_only,
        authority={
            "task_record_creation_allowed": False,
            "agent_execution_allowed": False,
            "tool_execution_allowed": False,
            "permission_granting": False,
            "budget_granting": False,
        },
    )


def _bid_for_card(announcement: DelegateTaskAnnouncement, card: AgentDiscoveryCard) -> DelegateBid:
    score = 0
    rejected: list[str] = []
    matched: dict[str, list[str]] = {
        "outputs": _matches(announcement.required_outputs, [*card.output_contracts, *card.declared_outputs, *card.preferred_outputs]),
        "tags": _matches(announcement.required_tags, card.tags),
        "knowledge_domains": _matches(announcement.required_knowledge_domains, card.knowledge_domains),
        "review_responsibilities": _matches(
            announcement.required_review_responsibilities,
            [*card.review_responsibilities, *card.tags, *(card.role or "").split()],
        ),
        "forbidden_actions": _matches(announcement.required_forbidden_actions, card.forbidden_actions),
    }
    if card.status != "discoverable":
        rejected.append(f"card_status_{card.status}")
    if card.agent_id in set(announcement.excluded_agent_ids):
        rejected.append("agent_excluded")
    if announcement.workbench_id and announcement.workbench_id not in set(card.workbench_ids):
        rejected.append("workbench_not_allowed")
    if announcement.required_kind and card.kind != announcement.required_kind:
        rejected.append(f"kind_mismatch:{card.kind}")
    if announcement.required_tool_policy_id and card.tool_policy_id != announcement.required_tool_policy_id:
        rejected.append(f"tool_policy_mismatch:{card.tool_policy_id}")
    for key, required_values in (
        ("outputs", announcement.required_outputs),
        ("tags", announcement.required_tags),
        ("knowledge_domains", announcement.required_knowledge_domains),
        ("review_responsibilities", announcement.required_review_responsibilities),
        ("forbidden_actions", announcement.required_forbidden_actions),
    ):
        missing = sorted(set(_casefold_items(required_values)) - set(_casefold_items(matched[key])))
        if missing:
            rejected.append(f"missing_{key}:{','.join(missing)}")

    if not rejected:
        score += 20
        if announcement.required_kind and card.kind == announcement.required_kind:
            score += 40
        score += 10 * len(matched["tags"])
        score += 10 * len(matched["outputs"])
        score += 8 * len(matched["knowledge_domains"])
        score += 8 * len(matched["review_responsibilities"])
        score += 6 * len(matched["forbidden_actions"])
        if announcement.prefer_read_only and _card_is_read_only(card):
            score += 10
        if announcement.task_type and card.kind == _preferred_kind_for_task_type(announcement.task_type):
            score += 5
    bid_payload = {
        "announcement_id": announcement.announcement_id,
        "agent_id": card.agent_id,
        "score": score,
        "rejected": rejected,
        "card_sha256": card.card_sha256,
    }
    status: DelegateBidStatus = "eligible" if not rejected else "ineligible"
    return DelegateBid(
        bid_id="delegate_bid_" + _stable_json_sha256(bid_payload)[:16],
        announcement_id=announcement.announcement_id,
        agent_id=card.agent_id,
        status=status,
        score=score,
        matched=matched,
        rejected_reasons=rejected,
        bid_terms={
            "contract_id": card.contract_id,
            "contract_sha256": card.contract_sha256,
            "tool_policy_id": card.tool_policy_id,
            "delegate_budget_required": True,
            "trace_required": True,
            "runtime_authority_granted": False,
            "permission_granting": False,
        },
        safety=_safety_flags(),
    )


def _workbench_ids_for_builtin_agent(registry, agent_id: str) -> list[str]:
    return sorted(
        workbench_id for workbench_id, workbench in registry.workbenches.items() if agent_id in set(workbench.allowed_agents)
    )


def _card_is_read_only(card: AgentDiscoveryCard) -> bool:
    policy = card.tool_policy_id or ""
    authority = card.authority
    return (
        policy == "read_only"
        and authority.get("network_allowed") is False
        and authority.get("filesystem_mutation_allowed") is False
        and authority.get("tool_execution_allowed") is False
    )


def _preferred_kind_for_task_type(task_type: str) -> str | None:
    normalized = task_type.casefold()
    if "review" in normalized:
        return AgentKind.REVIEWER.value
    if "orchestr" in normalized:
        return AgentKind.ORCHESTRATOR.value
    if "summary" in normalized or "plan" in normalized or "edit" in normalized:
        return AgentKind.SPECIALIST.value
    return None


def _matches(required: list[str], available: list[str]) -> list[str]:
    available_by_fold = {item.casefold(): item for item in available if item}
    return [available_by_fold[item.casefold()] for item in required if item.casefold() in available_by_fold]


def _casefold_items(values: list[str]) -> list[str]:
    return [value.casefold() for value in values if value]


def _normalized_list(values: list[str] | None) -> list[str]:
    result: list[str] = []
    for value in values or []:
        for item in str(value).split(","):
            normalized = item.strip()
            if normalized and normalized not in result:
                result.append(normalized)
    return result


def _safety_flags() -> dict[str, bool]:
    return {
        "read_only": True,
        "metadata_only": True,
        "source_body_loaded": False,
        "provider_called": False,
        "network_called": False,
        "agent_execution_started": False,
        "tool_execution_started": False,
        "adapter_execution_started": False,
        "process_started": False,
        "filesystem_modified": False,
        "credential_accessed": False,
        "permission_granting": False,
        "budget_granting": False,
        "model_context_allowed": False,
    }


def _safety_is_passive(safety: dict[str, bool]) -> bool:
    return safety.get("read_only") is True and all(
        safety.get(key) is False
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
    )


def _stable_json_sha256(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(sanitize_for_logging(payload), sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()
