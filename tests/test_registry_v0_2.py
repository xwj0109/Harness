import shutil

import pytest
from pydantic import ValidationError

from harness.registry import BUILTIN_SPECS_DIR, SpecRegistry, builtin_spec_registry, load_packaged_spec_registry
from harness.specs import (
    AgentKind,
    AgentSpec,
    HARD_FORBIDDEN_PATHS,
    MemoryScope,
    ModelProfile,
    ModelProfileKind,
    REQUIRED_WORKBENCH_FORBIDDEN_ACTIONS,
    ToolPermission,
    ToolPolicy,
    WorkbenchSpec,
)

QUANT_AGENT_IDS = {
    "quant_orchestrator",
    "quant_researcher",
    "commodities_researcher",
    "equities_researcher",
    "volatility_researcher",
    "data_engineer",
    "backtest_engineer",
    "low_level_optimizer",
    "risk_reviewer",
    "leakage_reviewer",
    "statistical_validity_reviewer",
}

QUANT_FORBIDDEN_ACTIONS = {
    "live_trading",
    "broker_action",
    "capital_allocation",
    "order_placement",
    "paid_api_fallback",
    "hosted_fallback",
}

QUANT_GROUP_IDS = {"quant_research", "quant_development", "trading_analysis", "review"}


def test_builtin_spec_registry_contains_starter_specs() -> None:
    registry = builtin_spec_registry()

    assert {"local_reasoning", "codex_supervised"} <= set(registry.model_profiles)
    assert ({"repo_inspector", "code_editor", "test_runner", "job_researcher"} | QUANT_AGENT_IDS) <= set(
        registry.agents
    )
    assert {"coding", "quant", "personal"} <= set(registry.workbenches)


def test_builtin_agent_references_resolve() -> None:
    registry = builtin_spec_registry()

    for agent in registry.agents.values():
        assert agent.model_profile in registry.model_profiles
        assert agent.tool_policy in registry.tool_policies
        assert agent.memory_scope in registry.memory_scopes
        if agent.parent is not None:
            assert agent.parent in registry.agents


def test_builtin_workbench_references_resolve() -> None:
    registry = builtin_spec_registry()

    for workbench in registry.workbenches.values():
        assert workbench.default_model_profile in registry.model_profiles
        for agent_id in workbench.allowed_agents:
            assert agent_id in registry.agents
        assert REQUIRED_WORKBENCH_FORBIDDEN_ACTIONS[workbench.id] <= set(workbench.forbidden_actions)


def test_builtin_registry_preserves_safety_defaults() -> None:
    registry = builtin_spec_registry()

    for policy in registry.tool_policies.values():
        assert policy.network == ToolPermission.FORBIDDEN
        assert policy.hosted_boundary != ToolPermission.ALLOWED
        assert policy.active_repo_write != ToolPermission.ALLOWED
    assert registry.tool_policies["read_only"].active_repo_write == ToolPermission.FORBIDDEN
    for scope in registry.memory_scopes.values():
        assert HARD_FORBIDDEN_PATHS <= set(scope.forbidden_paths)


def test_builtin_quant_workbench_contains_v0_6_agent_set_with_safety_boundaries() -> None:
    registry = builtin_spec_registry()
    workbench = registry.get_workbench("quant")

    assert QUANT_AGENT_IDS <= set(workbench.allowed_agents)
    assert QUANT_FORBIDDEN_ACTIONS <= set(workbench.forbidden_actions)
    for agent_id in QUANT_AGENT_IDS:
        agent = registry.get_agent(agent_id)
        policy = registry.tool_policies[agent.tool_policy]
        assert agent.model_profile == "codex_supervised"
        assert agent.memory_scope == "quant"
        assert "quant" in agent.tags
        assert policy.network == ToolPermission.FORBIDDEN
        assert policy.active_repo_write != ToolPermission.ALLOWED
        assert policy.hosted_boundary != ToolPermission.ALLOWED


def test_builtin_quant_groups_and_parent_chains_are_resolved() -> None:
    registry = builtin_spec_registry()

    for group_id in QUANT_GROUP_IDS:
        group = registry.get_agent(group_id)
        assert group.kind == AgentKind.GROUP
        assert group.tool_policy == "read_only"
        assert group.memory_scope == "quant"

    assert registry.get_agent("commodities_researcher").parent == "quant_research"
    assert registry.get_agent("backtest_engineer").parent == "quant_development"
    assert registry.get_agent("risk_reviewer").parent == "trading_analysis"
    assert registry.get_agent("leakage_reviewer").parent == "review"

    resolved = registry.resolve_agent_effective_spec("commodities_researcher")
    assert resolved["parent_chain"] == ["quant_research"]
    assert resolved["model_profile"] == "codex_supervised"
    assert resolved["tool_policy"] == "read_only"
    assert resolved["memory_scope"] == "quant"
    assert resolved["tags"] == ["starter", "quant", "group", "research", "commodities"]
    assert resolved["outputs"] == ["research_brief.md", "data_requirements.md", "commodities_research_note.md"]


def test_builtin_agent_profiles_load_and_attach_to_agents() -> None:
    registry = builtin_spec_registry()

    assert {"commodities_researcher.default", "risk_reviewer.default", "job_researcher.default"} <= set(
        registry.agent_profiles
    )
    commodities_profiles = registry.list_agent_profiles("commodities_researcher")
    assert [profile.id for profile in commodities_profiles] == ["commodities_researcher.default"]
    profile = commodities_profiles[0]
    assert profile.agent_id == "commodities_researcher"
    assert "commodities" in profile.knowledge_domains
    assert "commodities_research_note.md" in profile.preferred_outputs
    assert "broker_action" in profile.forbidden_actions


def test_builtin_registry_loads_from_packaged_hierarchical_yaml() -> None:
    registry = load_packaged_spec_registry()

    assert registry.get_agent("quant_orchestrator").memory_scope == "quant"
    assert registry.get_agent("statistical_validity_reviewer").kind == AgentKind.REVIEWER
    assert QUANT_AGENT_IDS <= set(registry.get_workbench("quant").allowed_agents)


def test_packaged_registry_rejects_duplicate_agent_ids(tmp_path) -> None:
    root = tmp_path / "builtin_specs"
    shutil.copytree(BUILTIN_SPECS_DIR, root)
    duplicate = root / "agents" / "quant" / "duplicate_quant_researcher.yaml"
    duplicate.write_text(
        """
id: quant_researcher
kind: specialist
role: Duplicate quant researcher.
model_profile: local_reasoning
tool_policy: read_only
memory_scope: quant
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Duplicate packaged built-in agents id: quant_researcher"):
        load_packaged_spec_registry(root)


def test_packaged_registry_rejects_malformed_agent_specs(tmp_path) -> None:
    root = tmp_path / "builtin_specs"
    shutil.copytree(BUILTIN_SPECS_DIR, root)
    malformed = root / "agents" / "quant" / "malformed.yaml"
    malformed.write_text(
        """
id: malformed_quant_agent
kind: specialist
role: Missing references.
model_profile: missing_profile
tool_policy: read_only
memory_scope: quant
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Packaged built-in specs are invalid"):
        load_packaged_spec_registry(root)


def test_packaged_registry_rejects_duplicate_agent_profile_ids(tmp_path) -> None:
    root = tmp_path / "builtin_specs"
    shutil.copytree(BUILTIN_SPECS_DIR, root)
    duplicate_dir = root / "agents" / "quant" / "profiles_extra" / "profiles"
    duplicate_dir.mkdir(parents=True)
    (duplicate_dir / "duplicate.yaml").write_text(
        """
id: commodities_researcher.default
agent_id: commodities_researcher
description: Duplicate profile.
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Duplicate packaged built-in agent_profiles id"):
        load_packaged_spec_registry(root)


def test_registry_rejects_agent_profile_missing_agent_reference() -> None:
    with pytest.raises(ValidationError, match="references missing agent"):
        SpecRegistry(
            agent_profiles={
                "missing_agent.default": {
                    "id": "missing_agent.default",
                    "agent_id": "missing_agent",
                    "description": "Missing agent profile.",
                }
            }
        )


def test_registry_rejects_agent_profile_forbidden_output_paths() -> None:
    with pytest.raises(ValidationError, match="preferred_outputs cannot include forbidden path"):
        SpecRegistry(
            agents={
                "repo_inspector": AgentSpec(
                    id="repo_inspector",
                    kind=AgentKind.SPECIALIST,
                    role="Inspect repositories.",
                    model_profile="local_reasoning",
                    tool_policy="read_only",
                    memory_scope="project",
                )
            },
            agent_profiles={
                "repo_inspector.default": {
                    "id": "repo_inspector.default",
                    "agent_id": "repo_inspector",
                    "description": "Unsafe profile.",
                    "preferred_outputs": [".env"],
                }
            },
        )


def test_registry_rejects_parent_cycles() -> None:
    with pytest.raises(ValidationError, match="Agent parent cycle detected"):
        SpecRegistry(
            model_profiles={
                "local_reasoning": ModelProfile(
                    id="local_reasoning",
                    kind=ModelProfileKind.LOCAL,
                    backend="local_openai_compatible",
                )
            },
            tool_policies={"read_only": ToolPolicy(active_repo_write=ToolPermission.FORBIDDEN)},
            memory_scopes={"project": MemoryScope(id="project")},
            agents={
                "group_a": AgentSpec(
                    id="group_a",
                    kind=AgentKind.GROUP,
                    role="Group A.",
                    model_profile="local_reasoning",
                    tool_policy="read_only",
                    memory_scope="project",
                    parent="group_b",
                ),
                "group_b": AgentSpec(
                    id="group_b",
                    kind=AgentKind.GROUP,
                    role="Group B.",
                    model_profile="local_reasoning",
                    tool_policy="read_only",
                    memory_scope="project",
                    parent="group_a",
                ),
            },
        )


def test_registry_rejects_non_group_parent() -> None:
    with pytest.raises(ValidationError, match="parent is not a group"):
        SpecRegistry(
            model_profiles={
                "local_reasoning": ModelProfile(
                    id="local_reasoning",
                    kind=ModelProfileKind.LOCAL,
                    backend="local_openai_compatible",
                )
            },
            tool_policies={"read_only": ToolPolicy(active_repo_write=ToolPermission.FORBIDDEN)},
            memory_scopes={"project": MemoryScope(id="project")},
            agents={
                "parent_specialist": AgentSpec(
                    id="parent_specialist",
                    kind=AgentKind.SPECIALIST,
                    role="Not a group.",
                    model_profile="local_reasoning",
                    tool_policy="read_only",
                    memory_scope="project",
                ),
                "child": AgentSpec(
                    id="child",
                    kind=AgentKind.SPECIALIST,
                    role="Child.",
                    model_profile="local_reasoning",
                    tool_policy="read_only",
                    memory_scope="project",
                    parent="parent_specialist",
                ),
            },
        )


def test_registry_rejects_child_policy_that_broadens_parent_policy() -> None:
    with pytest.raises(ValidationError, match="broadens parent parent_group active_repo_write"):
        SpecRegistry(
            model_profiles={
                "local_reasoning": ModelProfile(
                    id="local_reasoning",
                    kind=ModelProfileKind.LOCAL,
                    backend="local_openai_compatible",
                )
            },
            tool_policies={
                "parent_policy": ToolPolicy(active_repo_write=ToolPermission.FORBIDDEN),
                "child_policy": ToolPolicy(active_repo_write=ToolPermission.APPROVAL_REQUIRED),
            },
            memory_scopes={"project": MemoryScope(id="project")},
            agents={
                "parent_group": AgentSpec(
                    id="parent_group",
                    kind=AgentKind.GROUP,
                    role="Parent group.",
                    model_profile="local_reasoning",
                    tool_policy="parent_policy",
                    memory_scope="project",
                ),
                "child": AgentSpec(
                    id="child",
                    kind=AgentKind.SPECIALIST,
                    role="Child.",
                    model_profile="local_reasoning",
                    tool_policy="child_policy",
                    memory_scope="project",
                    parent="parent_group",
                ),
            },
        )


def test_builtin_lookup_helpers_return_specs() -> None:
    registry = builtin_spec_registry()

    assert registry.get_agent("repo_inspector").id == "repo_inspector"
    assert registry.get_workbench("coding").id == "coding"


def test_lookup_helpers_raise_clear_key_errors() -> None:
    registry = builtin_spec_registry()

    with pytest.raises(KeyError, match="Agent not found: missing"):
        registry.get_agent("missing")
    with pytest.raises(KeyError, match="Workbench not found: missing"):
        registry.get_workbench("missing")


def test_registry_rejects_agent_missing_model_profile() -> None:
    with pytest.raises(ValidationError, match="missing model_profile"):
        SpecRegistry(
            tool_policies={"read_only": ToolPolicy()},
            memory_scopes={"project": MemoryScope(id="project")},
            agents={
                "repo_inspector": AgentSpec(
                    id="repo_inspector",
                    kind=AgentKind.SPECIALIST,
                    role="Inspect repositories.",
                    model_profile="missing_profile",
                    tool_policy="read_only",
                    memory_scope="project",
                )
            },
        )


def test_registry_rejects_workbench_missing_allowed_agent() -> None:
    with pytest.raises(ValidationError, match="missing allowed agent"):
        SpecRegistry(
            model_profiles={
                "local_reasoning": ModelProfile(
                    id="local_reasoning",
                    kind=ModelProfileKind.LOCAL,
                    backend="local_openai_compatible",
                )
            },
            workbenches={
                "coding": WorkbenchSpec(
                    id="coding",
                    description="Coding workbench.",
                    allowed_agents=["missing_agent"],
                    default_model_profile="local_reasoning",
                    forbidden_actions=["paid_api_fallback", "hosted_fallback"],
                )
            },
        )


def test_registry_rejects_agent_missing_parent() -> None:
    with pytest.raises(ValidationError, match="missing parent"):
        SpecRegistry(
            model_profiles={
                "local_reasoning": ModelProfile(
                    id="local_reasoning",
                    kind=ModelProfileKind.LOCAL,
                    backend="local_openai_compatible",
                )
            },
            tool_policies={"read_only": ToolPolicy()},
            memory_scopes={"project": MemoryScope(id="project")},
            agents={
                "repo_inspector": AgentSpec(
                    id="repo_inspector",
                    kind=AgentKind.SPECIALIST,
                    role="Inspect repositories.",
                    model_profile="local_reasoning",
                    tool_policy="read_only",
                    memory_scope="project",
                    parent="missing_parent",
                )
            },
        )


def test_registry_rejects_model_profile_mapping_key_mismatch() -> None:
    with pytest.raises(ValidationError, match="model_profile mapping key must match contained id"):
        SpecRegistry(
            model_profiles={
                "local": ModelProfile(
                    id="local_reasoning",
                    kind=ModelProfileKind.LOCAL,
                    backend="local_openai_compatible",
                )
            }
        )


def test_registry_rejects_memory_scope_mapping_key_mismatch() -> None:
    with pytest.raises(ValidationError, match="memory_scope mapping key must match contained id"):
        SpecRegistry(memory_scopes={"project_scope": MemoryScope(id="project")})


def test_registry_rejects_agent_mapping_key_mismatch() -> None:
    with pytest.raises(ValidationError, match="agent mapping key must match contained id"):
        SpecRegistry(
            agents={
                "inspector": AgentSpec(
                    id="repo_inspector",
                    kind=AgentKind.SPECIALIST,
                    role="Inspect repositories.",
                    model_profile="local_reasoning",
                    tool_policy="read_only",
                    memory_scope="project",
                )
            }
        )


def test_registry_rejects_workbench_mapping_key_mismatch() -> None:
    with pytest.raises(ValidationError, match="workbench mapping key must match contained id"):
        SpecRegistry(
            workbenches={
                "code": WorkbenchSpec(
                    id="coding",
                    description="Coding workbench.",
                    allowed_agents=[],
                    default_model_profile="local_reasoning",
                    forbidden_actions=["paid_api_fallback", "hosted_fallback"],
                )
            }
        )


def test_registry_rejects_empty_tool_policy_mapping_key() -> None:
    with pytest.raises(ValidationError, match="Tool policy id must be non-empty"):
        SpecRegistry(tool_policies={"": ToolPolicy()})
