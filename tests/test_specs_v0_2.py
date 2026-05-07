import pytest
from pydantic import ValidationError

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
    assert HARD_FORBIDDEN_PATHS <= set(scope.forbidden_paths)


def test_tool_policy_allows_narrower_broad_permissions() -> None:
    policy = ToolPolicy(
        network=ToolPermission.FORBIDDEN,
        hosted_boundary=ToolPermission.FORBIDDEN,
        active_repo_write=ToolPermission.FORBIDDEN,
        tools={"repo_read": ToolPermission.ALLOWED},
    )

    assert policy.network == ToolPermission.FORBIDDEN
    assert policy.hosted_boundary == ToolPermission.FORBIDDEN
    assert policy.active_repo_write == ToolPermission.FORBIDDEN
    assert policy.tools["repo_read"] == ToolPermission.ALLOWED


@pytest.mark.parametrize(
    ("field_name", "message"),
    [
        ("network", "ToolPolicy network cannot be allowed."),
        ("hosted_boundary", "ToolPolicy hosted_boundary cannot be allowed."),
        ("active_repo_write", "ToolPolicy active_repo_write cannot be allowed."),
    ],
)
def test_tool_policy_rejects_unsafe_broad_allowed_permissions(field_name, message) -> None:
    with pytest.raises(ValidationError, match=message):
        ToolPolicy(**{field_name: ToolPermission.ALLOWED})


def test_memory_scope_allows_safe_custom_allowed_paths_with_hard_forbidden_paths() -> None:
    scope = MemoryScope(
        id="docs_scope",
        allowed_paths=["docs/"],
        forbidden_paths=[*HARD_FORBIDDEN_PATHS, "private/"],
    )

    assert scope.allowed_paths == ["docs/"]
    assert HARD_FORBIDDEN_PATHS <= set(scope.forbidden_paths)


def test_memory_scope_rejects_missing_hard_forbidden_paths() -> None:
    with pytest.raises(ValidationError, match="forbidden_paths must include repository hard-forbidden paths"):
        MemoryScope(id="project", forbidden_paths=[])


@pytest.mark.parametrize(
    "allowed_path",
    [
        ".harness",
        ".harness/runs",
        ".git",
        ".git/config",
        ".env",
        ".env.local",
        "private.pem",
        "private.key",
        "state.sqlite",
        "secrets",
        "secrets/token.txt",
    ],
)
def test_memory_scope_rejects_hard_forbidden_allowed_paths(allowed_path) -> None:
    with pytest.raises(ValidationError, match="allowed_paths cannot include repository hard-forbidden path"):
        MemoryScope(id="project", allowed_paths=[allowed_path])


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


def test_model_profile_rejects_local_profile_with_external_agent_backend() -> None:
    with pytest.raises(ValidationError, match="Local model profile backend is not local-compatible: codex_cli"):
        ModelProfile(id="invalid_local", kind=ModelProfileKind.LOCAL, backend="codex_cli")


def test_model_profile_rejects_external_agent_profile_with_local_backend() -> None:
    with pytest.raises(
        ValidationError,
        match="External agent model profile backend is not supervised external agent: local_openai_compatible",
    ):
        ModelProfile(
            id="invalid_external",
            kind=ModelProfileKind.EXTERNAL_AGENT,
            backend="local_openai_compatible",
        )


@pytest.mark.parametrize(
    "backend",
    ["openai", "openai_api", "paid_openai_compatible", "hosted_openai", "codex"],
)
def test_model_profile_rejects_forbidden_backend_declarations(backend) -> None:
    with pytest.raises(ValidationError):
        ModelProfile(id="unsafe", kind=ModelProfileKind.EXTERNAL_AGENT, backend=backend)


@pytest.mark.parametrize(
    "constraint",
    ["openai_api", "paid_fallback", "hosted_fallback", "raw_model_provider"],
)
def test_model_profile_rejects_forbidden_constraints(constraint) -> None:
    with pytest.raises(ValidationError, match="Model profile constraint is forbidden by harness safety policy"):
        ModelProfile(
            id="unsafe_codex",
            kind=ModelProfileKind.EXTERNAL_AGENT,
            backend="codex_cli",
            constraints=["supervised_external_agent", constraint],
        )


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


def test_workbench_allows_stricter_forbidden_actions() -> None:
    workbench = WorkbenchSpec(
        id="coding",
        description="Local-first coding workbench.",
        allowed_agents=["repo_inspector"],
        default_model_profile="local_reasoning",
        forbidden_actions=["paid_api_fallback", "hosted_fallback", "shell_exec"],
    )

    assert REQUIRED_WORKBENCH_FORBIDDEN_ACTIONS["coding"] <= set(workbench.forbidden_actions)
    assert "shell_exec" in workbench.forbidden_actions


@pytest.mark.parametrize(
    ("workbench_id", "description", "forbidden_actions", "missing"),
    [
        ("coding", "Coding workbench.", [], "hosted_fallback, paid_api_fallback"),
        ("quant", "Quant workbench.", ["live_trading", "capital_allocation"], "broker_action"),
        ("personal", "Personal workbench.", ["email_send", "application_submit"], "external_message_send"),
    ],
)
def test_workbench_rejects_missing_required_forbidden_actions(
    workbench_id, description, forbidden_actions, missing
) -> None:
    with pytest.raises(
        ValidationError,
        match=f"Workbench {workbench_id} forbidden_actions missing required actions: {missing}",
    ):
        WorkbenchSpec(
            id=workbench_id,
            description=description,
            allowed_agents=[],
            default_model_profile="local_reasoning",
            forbidden_actions=forbidden_actions,
        )


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
