from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field, field_validator, model_validator


class ModelProfileKind(str, Enum):
    LOCAL = "local"
    EXTERNAL_AGENT = "external_agent"


class ToolPermission(str, Enum):
    ALLOWED = "allowed"
    APPROVAL_REQUIRED = "approval_required"
    FORBIDDEN = "forbidden"


class AgentKind(str, Enum):
    ORCHESTRATOR = "orchestrator"
    GROUP = "group"
    SPECIALIST = "specialist"
    REVIEWER = "reviewer"


def _non_empty(value: str, field_name: str) -> str:
    if not value.strip():
        raise ValueError(f"{field_name} must be non-empty.")
    return value


class ModelProfile(BaseModel):
    id: str
    kind: ModelProfileKind
    backend: str
    description: str = ""
    default: bool = False
    constraints: list[str] = Field(default_factory=list)

    @field_validator("id", "backend")
    @classmethod
    def required_non_empty(cls, value: str, info) -> str:
        return _non_empty(value, info.field_name)


class ToolPolicy(BaseModel):
    tools: dict[str, ToolPermission] = Field(default_factory=dict)
    network: ToolPermission = ToolPermission.FORBIDDEN
    active_repo_write: ToolPermission = ToolPermission.APPROVAL_REQUIRED
    hosted_boundary: ToolPermission = ToolPermission.APPROVAL_REQUIRED


class MemoryScope(BaseModel):
    id: str
    description: str = ""
    allowed_paths: list[str] = Field(default_factory=list)
    forbidden_paths: list[str] = Field(
        default_factory=lambda: [".harness/", ".git/", ".env*", "*.pem", "*.key", "*.sqlite", "secrets/"]
    )

    @field_validator("id")
    @classmethod
    def id_non_empty(cls, value: str) -> str:
        return _non_empty(value, "id")


class AgentSpec(BaseModel):
    id: str
    kind: AgentKind
    role: str
    model_profile: str
    tool_policy: str
    memory_scope: str
    parent: str | None = None
    outputs: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)

    @field_validator("id", "role", "model_profile", "tool_policy", "memory_scope")
    @classmethod
    def required_non_empty(cls, value: str, info) -> str:
        return _non_empty(value, info.field_name)


class WorkbenchSpec(BaseModel):
    id: str
    description: str
    allowed_agents: list[str]
    default_model_profile: str
    tool_policies: dict[str, ToolPolicy] = Field(default_factory=dict)
    memory_scopes: dict[str, MemoryScope] = Field(default_factory=dict)
    model_profiles: dict[str, ModelProfile] = Field(default_factory=dict)
    approval_policy: dict[str, ToolPermission] = Field(default_factory=dict)
    forbidden_actions: list[str] = Field(default_factory=list)

    @field_validator("id", "description", "default_model_profile")
    @classmethod
    def required_non_empty(cls, value: str, info) -> str:
        return _non_empty(value, info.field_name)

    @model_validator(mode="after")
    def default_model_profile_exists(self) -> WorkbenchSpec:
        if self.model_profiles and self.default_model_profile not in self.model_profiles:
            raise ValueError("default_model_profile must exist in model_profiles.")
        return self
