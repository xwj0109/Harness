import pytest
from pydantic import ValidationError

from harness.registry import SpecRegistry, builtin_spec_registry
from harness.specs import (
    AgentKind,
    AgentSpec,
    MemoryScope,
    ModelProfile,
    ModelProfileKind,
    ToolPermission,
    ToolPolicy,
    WorkbenchSpec,
)


def test_builtin_spec_registry_contains_starter_specs() -> None:
    registry = builtin_spec_registry()

    assert {"local_reasoning", "codex_supervised"} <= set(registry.model_profiles)
    assert {"repo_inspector", "code_editor", "test_runner", "quant_researcher", "job_researcher"} <= set(
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


def test_builtin_registry_preserves_safety_defaults() -> None:
    registry = builtin_spec_registry()

    for policy in registry.tool_policies.values():
        assert policy.network in {ToolPermission.FORBIDDEN, ToolPermission.APPROVAL_REQUIRED}
        assert policy.active_repo_write != ToolPermission.ALLOWED
    for scope in registry.memory_scopes.values():
        assert {".harness/", ".git/", ".env*", "*.pem", "*.key", "*.sqlite", "secrets/"} <= set(
            scope.forbidden_paths
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
