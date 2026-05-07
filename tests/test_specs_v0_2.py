import pytest
from pydantic import ValidationError

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


def test_spec_enums_serialize_as_stable_strings() -> None:
    profile = ModelProfile(id="local_reasoning", kind=ModelProfileKind.LOCAL, backend="local_openai_compatible")
    policy = ToolPolicy(tools={"repo_read": ToolPermission.ALLOWED})
    agent = AgentSpec(
        id="repo_inspector",
        kind=AgentKind.SPECIALIST,
        role="Inspect local repositories without mutation.",
        model_profile="local_reasoning",
        tool_policy="read_only",
        memory_scope="project",
    )

    assert profile.model_dump(mode="json")["kind"] == "local"
    assert policy.model_dump(mode="json")["tools"]["repo_read"] == "allowed"
    assert policy.model_dump(mode="json")["network"] == "forbidden"
    assert agent.model_dump(mode="json")["kind"] == "specialist"


def test_spec_defaults_preserve_safety_boundaries() -> None:
    policy = ToolPolicy()
    scope = MemoryScope(id="project")

    assert policy.network == ToolPermission.FORBIDDEN
    assert policy.hosted_boundary == ToolPermission.APPROVAL_REQUIRED
    assert policy.active_repo_write == ToolPermission.APPROVAL_REQUIRED
    assert {".harness/", ".git/", ".env*", "*.pem", "*.key", "*.sqlite", "secrets/"} <= set(
        scope.forbidden_paths
    )


def test_model_profiles_describe_local_and_supervised_external_backends() -> None:
    local = ModelProfile(id="local_reasoning", kind=ModelProfileKind.LOCAL, backend="local_openai_compatible")
    codex = ModelProfile(
        id="codex_supervised",
        kind=ModelProfileKind.EXTERNAL_AGENT,
        backend="codex_cli",
        constraints=["supervised_external_agent"],
    )

    assert local.kind == ModelProfileKind.LOCAL
    assert local.backend == "local_openai_compatible"
    assert codex.kind == ModelProfileKind.EXTERNAL_AGENT
    assert codex.backend == "codex_cli"
    assert codex.constraints == ["supervised_external_agent"]


def test_coding_workbench_with_repo_inspector_validates() -> None:
    workbench = WorkbenchSpec(
        id="coding",
        description="Local-first coding workbench.",
        allowed_agents=["repo_inspector"],
        default_model_profile="local_reasoning",
        model_profiles={
            "local_reasoning": ModelProfile(
                id="local_reasoning",
                kind=ModelProfileKind.LOCAL,
                backend="local_openai_compatible",
            )
        },
        tool_policies={"read_only": ToolPolicy(tools={"repo_read": ToolPermission.ALLOWED})},
        memory_scopes={"project": MemoryScope(id="project")},
        approval_policy={"active_repo_apply": ToolPermission.APPROVAL_REQUIRED},
        forbidden_actions=["paid_api_fallback", "hosted_fallback"],
    )
    agent = AgentSpec(
        id="repo_inspector",
        kind=AgentKind.SPECIALIST,
        role="Inspect repository structure and summarize local evidence.",
        model_profile="local_reasoning",
        tool_policy="read_only",
        memory_scope="project",
        outputs=["repo_summary.md"],
        tags=["starter"],
    )

    assert workbench.id == "coding"
    assert workbench.default_model_profile == "local_reasoning"
    assert agent.id in workbench.allowed_agents
    assert agent.parent is None


@pytest.mark.parametrize(
    ("model", "kwargs"),
    [
        (ModelProfile, {"id": "", "kind": ModelProfileKind.LOCAL, "backend": "local_openai_compatible"}),
        (ModelProfile, {"id": "local_reasoning", "kind": ModelProfileKind.LOCAL, "backend": ""}),
        (MemoryScope, {"id": ""}),
        (
            AgentSpec,
            {
                "id": "",
                "kind": AgentKind.SPECIALIST,
                "role": "Inspect repositories.",
                "model_profile": "local_reasoning",
                "tool_policy": "read_only",
                "memory_scope": "project",
            },
        ),
        (
            AgentSpec,
            {
                "id": "repo_inspector",
                "kind": AgentKind.SPECIALIST,
                "role": "",
                "model_profile": "local_reasoning",
                "tool_policy": "read_only",
                "memory_scope": "project",
            },
        ),
        (
            WorkbenchSpec,
            {"id": "", "description": "Coding workbench.", "allowed_agents": [], "default_model_profile": "local"},
        ),
        (
            WorkbenchSpec,
            {"id": "coding", "description": "", "allowed_agents": [], "default_model_profile": "local"},
        ),
        (
            WorkbenchSpec,
            {"id": "coding", "description": "Coding workbench.", "allowed_agents": [], "default_model_profile": ""},
        ),
    ],
)
def test_specs_reject_empty_required_fields(model, kwargs) -> None:
    with pytest.raises(ValidationError):
        model(**kwargs)


def test_workbench_rejects_missing_default_model_profile_reference() -> None:
    with pytest.raises(ValidationError, match="default_model_profile must exist"):
        WorkbenchSpec(
            id="coding",
            description="Local-first coding workbench.",
            allowed_agents=["repo_inspector"],
            default_model_profile="missing_profile",
            model_profiles={
                "local_reasoning": ModelProfile(
                    id="local_reasoning",
                    kind=ModelProfileKind.LOCAL,
                    backend="local_openai_compatible",
                )
            },
        )
