from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from harness.models import BackendCapabilities, BackendConfig, BackendKind, BackendMetadata


HARNESS_DIR = ".harness"
CONFIG_FILE = "config.yaml"

DEFAULT_CONTEXT_EXCLUDES = [
    ".harness/",
    ".git/",
    ".venv/",
    "node_modules/",
    "__pycache__/",
    ".pytest_cache/",
    ".mypy_cache/",
    "dist/",
    "build/",
    "*.egg-info/",
    ".DS_Store",
]

DEFAULT_ISOLATION_COPY_EXCLUDES = [
    ".git/",
    ".harness/",
    ".venv/",
    "node_modules/",
    "data/raw/",
    "secrets/",
    ".env",
    "*.pem",
    "*.key",
    "*.sqlite",
]


class SandboxConfig(BaseModel):
    image: str = "python:3.12-slim"
    image_build_file: str = "Dockerfile.harness-test"
    network: bool = False
    timeout_seconds: int = 120
    memory_limit: str = "2g"
    cpu_limit: float = 2.0
    workdir: str = "/workspace"
    install_project: bool = False
    install_project_no_build_isolation: bool = True


class ChatConfig(BaseModel):
    default_model_profile: str = "codex_cli"
    mode: str = "subscription"
    stream: bool = True
    allow_hosted_chat: bool = False
    allow_codex_subscription_chat: bool = True


class NamedReferenceConfig(BaseModel):
    kind: str = "local"
    path: str | None = None
    url: str | None = None
    description: str | None = None


class LspServerConfig(BaseModel):
    command: list[str] = Field(default_factory=list)
    file_extensions: list[str] = Field(default_factory=list)
    enabled: bool = False


class LspConfig(BaseModel):
    enabled: bool = False
    servers: dict[str, LspServerConfig] = Field(default_factory=dict)


class FormatterProfileConfig(BaseModel):
    command: list[str] = Field(default_factory=list)
    file_extensions: list[str] = Field(default_factory=list)
    enabled: bool = False
    format_on_accepted_edit: bool = False


class FormatterConfig(BaseModel):
    enabled: bool = False
    profiles: dict[str, FormatterProfileConfig] = Field(default_factory=dict)


class McpResourceConfig(BaseModel):
    uri: str
    path: str
    enabled: bool = True
    content_type: str | None = None
    description: str | None = None


class McpServerConfig(BaseModel):
    kind: str = "local"
    command: list[str] = Field(default_factory=list)
    url: str | None = None
    enabled: bool = False
    description: str | None = None
    resources: dict[str, McpResourceConfig] = Field(default_factory=dict)


class McpConfig(BaseModel):
    enabled: bool = False
    servers: dict[str, McpServerConfig] = Field(default_factory=dict)


class PluginConfig(BaseModel):
    path: str | None = None
    url: str | None = None
    spec: str | None = None
    entrypoint: str | None = None
    version: str | None = None
    enabled: bool = False
    description: str | None = None
    options: dict[str, Any] = Field(default_factory=dict)


class PluginsConfig(BaseModel):
    enabled: bool = False
    project: dict[str, PluginConfig] = Field(default_factory=dict)


class SkillConfig(BaseModel):
    path: str | None = None
    spec: str | None = None
    version: str | None = None
    enabled: bool = False
    description: str | None = None


class SkillsConfig(BaseModel):
    enabled: bool = False
    project: dict[str, SkillConfig] = Field(default_factory=dict)


class WebToolsConfig(BaseModel):
    enabled: bool = False
    fetch_enabled: bool = False
    search_enabled: bool = False
    approval_required: bool = True
    allowed_domains: list[str] = Field(default_factory=list)
    search_endpoint_url: str | None = None


class HarnessConfig(BaseModel):
    project_name: str = "agent-harness-project"
    context_excludes: list[str] = Field(default_factory=lambda: list(DEFAULT_CONTEXT_EXCLUDES))
    isolation_copy_excludes: list[str] = Field(
        default_factory=lambda: list(DEFAULT_ISOLATION_COPY_EXCLUDES)
    )
    backend_note: str = (
        "Future LocalOpenAICompatibleBackend may be treated as local_only only when "
        "base_url is localhost, 127.0.0.1, or an explicitly approved local/LAN endpoint."
    )
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    chat: ChatConfig = Field(default_factory=ChatConfig)
    references: dict[str, NamedReferenceConfig] = Field(default_factory=dict)
    lsp: LspConfig = Field(default_factory=LspConfig)
    formatter: FormatterConfig = Field(default_factory=FormatterConfig)
    mcp: McpConfig = Field(default_factory=McpConfig)
    plugins: PluginsConfig = Field(default_factory=PluginsConfig)
    skills: SkillsConfig = Field(default_factory=SkillsConfig)
    web_tools: WebToolsConfig = Field(default_factory=WebToolsConfig)
    backends: dict[str, BackendConfig]


def default_backend_configs() -> dict[str, BackendConfig]:
    return {
        "codex_cli": BackendConfig(
            name="codex_cli",
            kind=BackendKind.EXTERNAL_AGENT,
            metadata=BackendMetadata(
                billing_mode="subscription",
                execution_location="mixed",
                data_boundary="hosted_provider",
                allow_network=False,
            ),
            capabilities=BackendCapabilities(
                structured_output=True,
                tool_calling=False,
                json_mode=True,
                supports_exec=False,
            ),
            settings={
                "command": "codex",
                "auth_mode": "chatgpt",
                "model": "gpt-5.5",
                "model_reasoning_effort": "low",
                "timeout_seconds": 900,
                "skip_git_repo_check": True,
                "use_subscription_credits": True,
            },
        ),
        "local_openai_compatible": BackendConfig(
            name="local_openai_compatible",
            kind=BackendKind.NATIVE_MODEL,
            metadata=BackendMetadata(
                billing_mode="local_no_api_cost",
                execution_location="local_machine",
                data_boundary="local_only",
                allow_network=False,
            ),
            capabilities=BackendCapabilities(
                structured_output=True,
                tool_calling=False,
                json_mode=True,
            ),
            settings={
                "base_url": "http://localhost:11434/v1",
                "api_key": "ollama",
                "model": "qwen3-coder:30b",
                "temperature": 0.2,
                "max_tokens": 4096,
                "timeout_seconds": 300,
            },
        ),
        "paid_openai_compatible": BackendConfig(
            name="paid_openai_compatible",
            kind=BackendKind.NATIVE_MODEL,
            metadata=BackendMetadata(
                billing_mode="paid_api",
                execution_location="hosted",
                data_boundary="hosted_provider",
                allow_network=True,
            ),
            capabilities=BackendCapabilities(
                structured_output=True,
                tool_calling=True,
                json_mode=True,
            ),
            settings={
                "enabled": False,
                "base_url": "https://api.openai.com/v1",
                "api_key_env": "OPENAI_API_KEY",
                "model": "gpt-5.3-codex",
                "temperature": 0.2,
                "max_tokens": 4096,
                "billing_warning": "This uses API billing, not Codex subscription credits.",
            },
        ),
    }


def default_config() -> HarnessConfig:
    return HarnessConfig(backends=default_backend_configs())


def config_path(project_root: Path) -> Path:
    return project_root / HARNESS_DIR / CONFIG_FILE


def load_config(project_root: Path) -> HarnessConfig:
    path = config_path(project_root)
    if not path.exists():
        raise FileNotFoundError(f"Missing {path}. Run 'harness init --project {project_root}' first.")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return HarnessConfig.model_validate(data)


def write_default_config(project_root: Path) -> Path:
    cfg = default_config()
    path = config_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(yaml.safe_dump(_config_to_yaml_dict(cfg), sort_keys=False), encoding="utf-8")
    return path


def _config_to_yaml_dict(config: HarnessConfig) -> dict[str, Any]:
    return config.model_dump(mode="json")
