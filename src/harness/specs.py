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


HARD_FORBIDDEN_PATHS = frozenset({".harness/", ".git/", ".env*", "*.pem", "*.key", "*.sqlite", "secrets/"})
HARD_FORBIDDEN_PATH_DEFAULTS = (".harness/", ".git/", ".env*", "*.pem", "*.key", "*.sqlite", "secrets/")
LOCAL_COMPATIBLE_BACKENDS = frozenset({"local_openai_compatible"})
SUPERVISED_EXTERNAL_AGENT_BACKENDS = frozenset({"codex_cli"})
FORBIDDEN_MODEL_BACKEND_TERMS = ("openai", "api", "paid", "hosted")
FORBIDDEN_MODEL_CONSTRAINT_TERMS = ("openai_api", "paid", "hosted", "fallback", "raw_model_provider")
REQUIRED_WORKBENCH_FORBIDDEN_ACTIONS = {
    "coding": frozenset({"paid_api_fallback", "hosted_fallback"}),
    "quant": frozenset(
        {
            "live_trading",
            "broker_action",
            "capital_allocation",
            "order_placement",
            "paid_api_fallback",
            "hosted_fallback",
        }
    ),
    "personal": frozenset({"email_send", "application_submit", "external_message_send"}),
}


def _is_hard_forbidden_path(path: str) -> bool:
    normalized = path.strip().replace("\\", "/").rstrip("/")
    name = normalized.rsplit("/", 1)[-1]
    return (
        normalized == ".harness"
        or normalized.startswith(".harness/")
        or normalized == ".git"
        or normalized.startswith(".git/")
        or name.startswith(".env")
        or normalized.endswith(".pem")
        or normalized.endswith(".key")
        or normalized.endswith(".sqlite")
        or normalized == "secrets"
        or normalized.startswith("secrets/")
    )


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

    @model_validator(mode="after")
    def validate_backend_compatibility(self) -> ModelProfile:
        backend = self.backend.strip()
        if self.kind == ModelProfileKind.LOCAL:
            if backend not in LOCAL_COMPATIBLE_BACKENDS:
                raise ValueError(f"Local model profile backend is not local-compatible: {backend}")
        elif self.kind == ModelProfileKind.EXTERNAL_AGENT:
            if backend not in SUPERVISED_EXTERNAL_AGENT_BACKENDS:
                raise ValueError(f"External agent model profile backend is not supervised external agent: {backend}")
        if backend != "local_openai_compatible" and any(term in backend for term in FORBIDDEN_MODEL_BACKEND_TERMS):
            raise ValueError(f"Model profile backend is forbidden by harness safety policy: {backend}")
        for constraint in self.constraints:
            if any(term in constraint for term in FORBIDDEN_MODEL_CONSTRAINT_TERMS):
                raise ValueError(f"Model profile constraint is forbidden by harness safety policy: {constraint}")
        return self


class ToolPolicy(BaseModel):
    tools: dict[str, ToolPermission] = Field(default_factory=dict)
    network: ToolPermission = ToolPermission.FORBIDDEN
    active_repo_write: ToolPermission = ToolPermission.APPROVAL_REQUIRED
    hosted_boundary: ToolPermission = ToolPermission.APPROVAL_REQUIRED

    @model_validator(mode="after")
    def validate_broad_permissions(self) -> ToolPolicy:
        if self.network == ToolPermission.ALLOWED:
            raise ValueError("ToolPolicy network cannot be allowed.")
        if self.active_repo_write == ToolPermission.ALLOWED:
            raise ValueError("ToolPolicy active_repo_write cannot be allowed.")
        if self.hosted_boundary == ToolPermission.ALLOWED:
            raise ValueError("ToolPolicy hosted_boundary cannot be allowed.")
        return self


class MemoryScope(BaseModel):
    id: str
    description: str = ""
    allowed_paths: list[str] = Field(default_factory=list)
    forbidden_paths: list[str] = Field(default_factory=lambda: list(HARD_FORBIDDEN_PATH_DEFAULTS))

    @field_validator("id")
    @classmethod
    def id_non_empty(cls, value: str) -> str:
        return _non_empty(value, "id")

    @model_validator(mode="after")
    def validate_memory_boundaries(self) -> MemoryScope:
        if not HARD_FORBIDDEN_PATHS <= set(self.forbidden_paths):
            raise ValueError("MemoryScope forbidden_paths must include repository hard-forbidden paths.")
        for path in self.allowed_paths:
            if _is_hard_forbidden_path(path):
                raise ValueError(f"MemoryScope allowed_paths cannot include repository hard-forbidden path: {path}")
        return self


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
    def validate_workbench_boundaries(self) -> WorkbenchSpec:
        if self.model_profiles and self.default_model_profile not in self.model_profiles:
            raise ValueError("default_model_profile must exist in model_profiles.")
        required_actions = REQUIRED_WORKBENCH_FORBIDDEN_ACTIONS.get(self.id)
        if required_actions is not None and not required_actions <= set(self.forbidden_actions):
            missing = ", ".join(sorted(required_actions - set(self.forbidden_actions)))
            raise ValueError(f"Workbench {self.id} forbidden_actions missing required actions: {missing}")
        return self
