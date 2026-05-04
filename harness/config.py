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
                "timeout_seconds": 900,
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
