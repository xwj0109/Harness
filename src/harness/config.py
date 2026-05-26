from __future__ import annotations

from pathlib import Path
from typing import Any
import urllib.parse

import yaml
from pydantic import BaseModel, Field

from harness.models import BackendCapabilities, BackendConfig, BackendKind, BackendMetadata
from harness.model_protocols import ALLOWED_MODEL_PROTOCOLS


HARNESS_DIR = ".harness"
CONFIG_FILE = "config.yaml"
CUSTOM_MODELS_FILE = "models.yaml"

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
    search_provider: str = "configured_http"
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


class CustomModelsConfigError(ValueError):
    def __init__(self, path: Path, errors: list[str]) -> None:
        self.path = path
        self.errors = errors
        self.issues = custom_models_validation_issues(errors)
        super().__init__("; ".join(errors))


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
        "anthropic": BackendConfig(
            name="anthropic",
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
                max_context_tokens=200000,
            ),
            settings={
                "enabled": False,
                "base_url": "https://api.anthropic.com/v1",
                "api_key_env": "ANTHROPIC_API_KEY",
                "protocol": "anthropic_messages",
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 8192,
                "timeout_seconds": 300,
                "billing_warning": "This uses Anthropic API billing and is disabled by default.",
            },
        ),
        "google": BackendConfig(
            name="google",
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
                max_context_tokens=1048576,
            ),
            settings={
                "enabled": False,
                "base_url": "https://generativelanguage.googleapis.com/v1beta",
                "api_key_env": "GOOGLE_API_KEY",
                "protocol": "google_generative",
                "model": "gemini-2.5-flash",
                "max_tokens": 8192,
                "timeout_seconds": 300,
                "billing_warning": "This uses Google Generative AI API billing and is disabled by default.",
            },
        ),
        "bedrock": BackendConfig(
            name="bedrock",
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
                max_context_tokens=200000,
            ),
            settings={
                "enabled": False,
                "base_url": "https://bedrock-runtime.us-east-1.amazonaws.com",
                "credential_kind": "aws_profile",
                "aws_profile_env": "AWS_PROFILE",
                "aws_region": "us-east-1",
                "protocol": "bedrock_converse",
                "model": "anthropic.claude-3-5-sonnet-20241022-v2:0",
                "max_tokens": 8192,
                "timeout_seconds": 300,
                "billing_warning": "This uses AWS Bedrock billing and is disabled by default.",
            },
        ),
    }


def default_config() -> HarnessConfig:
    return HarnessConfig(backends=default_backend_configs())


def config_path(project_root: Path) -> Path:
    return project_root / HARNESS_DIR / CONFIG_FILE


def custom_models_config_path(project_root: Path) -> Path:
    return project_root / HARNESS_DIR / CUSTOM_MODELS_FILE


def load_config(project_root: Path) -> HarnessConfig:
    path = config_path(project_root)
    if not path.exists():
        raise FileNotFoundError(f"Missing {path}. Run 'harness init --project {project_root}' first.")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    cfg = HarnessConfig.model_validate(data)
    return merge_custom_models_config(cfg, project_root)


def validate_custom_models_config(project_root: Path) -> dict[str, Any]:
    path = custom_models_config_path(project_root)
    config_data = yaml.safe_load(config_path(project_root).read_text(encoding="utf-8")) or {}
    cfg = HarnessConfig.model_validate(config_data)
    data = _read_custom_models_yaml(path)
    errors = _validate_custom_models_data(data, cfg)
    if errors:
        raise CustomModelsConfigError(path, errors)
    return {
        "schema_version": "harness.custom_models_config_validation/v1",
        "ok": True,
        "path": str(path),
        "exists": path.exists(),
        "provider_count": len(data.get("providers", {}) if isinstance(data.get("providers"), dict) else {}),
        "model_count": _custom_model_count(data),
        "validation_issues": [],
        "metadata_only": True,
        "provider_execution_started": False,
        "model_execution_started": False,
        "network_accessed": False,
        "credentials_included": False,
        "credential_written": False,
        "hidden_provider_fallback": False,
        "hidden_model_fallback": False,
        "no_hidden_fallback": True,
        "permission_granting": False,
        "authority_granting": False,
    }


def merge_custom_models_config(config: HarnessConfig, project_root: Path) -> HarnessConfig:
    path = custom_models_config_path(project_root)
    data = _read_custom_models_yaml(path)
    if not data:
        return config
    errors = _validate_custom_models_data(data, config)
    if errors:
        raise CustomModelsConfigError(path, errors)
    merged = config.model_copy(deep=True)
    for provider_id, provider_spec in sorted((data.get("providers") or {}).items()):
        merged.backends[provider_id] = _custom_provider_backend(provider_id, provider_spec, path)
    for raw_ref, model_spec in sorted((data.get("models") or {}).items()):
        provider_id, model_id = _split_custom_model_ref(raw_ref)
        backend = merged.backends.get(provider_id)
        if backend is None:
            continue
        custom_models = dict(backend.settings.get("_custom_models") or {})
        item = dict(model_spec)
        item.setdefault("id", model_id)
        item.setdefault("model_id", model_id)
        item.setdefault("provider_id", provider_id)
        item.setdefault("override", True)
        custom_models[model_id] = item
        backend.settings["_custom_models"] = custom_models
        merged.backends[provider_id] = backend
    return merged


def write_default_config(project_root: Path) -> Path:
    cfg = default_config()
    path = config_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(yaml.safe_dump(_config_to_yaml_dict(cfg), sort_keys=False), encoding="utf-8")
    return path


def _config_to_yaml_dict(config: HarnessConfig) -> dict[str, Any]:
    return config.model_dump(mode="json")


def _read_custom_models_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise CustomModelsConfigError(path, ["custom_models_config_not_mapping"])
    return data


def _validate_custom_models_data(data: dict[str, Any], base_config: HarnessConfig) -> list[str]:
    errors: list[str] = []
    providers = data.get("providers", {})
    models = data.get("models", {})
    if providers is None:
        providers = {}
    if models is None:
        models = {}
    if not isinstance(providers, dict):
        errors.append("providers_must_be_mapping")
        providers = {}
    if not isinstance(models, dict):
        errors.append("models_must_be_mapping")
        models = {}
    for provider_id, spec in providers.items():
        if not _valid_custom_id(provider_id):
            errors.append(f"provider_id_invalid:{provider_id}")
            continue
        if not isinstance(spec, dict):
            errors.append(f"provider_must_be_mapping:{provider_id}")
            continue
        if provider_id in base_config.backends and not bool(spec.get("override", False)):
            errors.append(f"provider_duplicate_requires_override:{provider_id}")
        boundary_value = spec.get("data_boundary")
        boundary = str(boundary_value or "")
        if not isinstance(boundary_value, str) or not boundary_value.strip():
            errors.append(f"provider_data_boundary_missing:{provider_id}")
            boundary = "local_only"
        elif boundary not in {"local_only", "hosted_provider", "external_router"}:
            errors.append(f"provider_data_boundary_invalid:{provider_id}")
        protocol = str(spec.get("protocol") or "openai_chat")
        if protocol not in _custom_protocols():
            errors.append(f"provider_protocol_invalid:{provider_id}")
        base_url = spec.get("base_url") or spec.get("endpoint")
        if not isinstance(base_url, str) or not base_url.strip():
            errors.append(f"provider_base_url_missing:{provider_id}")
        elif boundary == "local_only" and not _is_safe_local_url(base_url, _string_list(spec.get("approved_lan_endpoints"))):
            errors.append(f"provider_local_url_not_loopback_or_approved_lan:{provider_id}")
        if boundary != "local_only" and bool(spec.get("enabled", False)) and not bool(spec.get("approved", False)):
            errors.append(f"hosted_provider_enabled_requires_approved:true:{provider_id}")
        if "credential" not in spec:
            errors.append(f"credential_missing:{provider_id}")
            credential = {}
        else:
            credential = spec.get("credential")
        if not isinstance(credential, dict):
            errors.append(f"credential_must_be_mapping:{provider_id}")
        else:
            kind = str(credential.get("kind") or "none")
            if kind not in {"none", "env", "static_local", "api_key", "oauth", "codex_login", "aws_env", "aws_profile"}:
                errors.append(f"credential_kind_invalid:{provider_id}")
            if kind == "env" and not str(credential.get("env_var") or "").strip():
                errors.append(f"credential_env_var_missing:{provider_id}")
            for secret_key in ("value", "api_key", "token", "secret", "authorization"):
                if secret_key in credential:
                    errors.append(f"credential_value_not_allowed:{provider_id}:{secret_key}")
        headers = spec.get("headers", {})
        if headers is not None and not isinstance(headers, dict):
            errors.append(f"headers_must_be_mapping:{provider_id}")
        elif isinstance(headers, dict):
            for header, value in headers.items():
                if not isinstance(value, dict) or str(value.get("kind") or "") != "env" or not str(value.get("env_var") or "").strip():
                    errors.append(f"header_must_use_env_ref:{provider_id}:{header}")
        provider_models = spec.get("models", {})
        if provider_models is not None and not isinstance(provider_models, dict):
            errors.append(f"provider_models_must_be_mapping:{provider_id}")
        elif isinstance(provider_models, dict):
            for model_id, model_spec in provider_models.items():
                errors.extend(_validate_custom_model_spec(provider_id, model_id, model_spec))
        for list_field in ("model_allowlist", "model_whitelist", "model_blocklist", "model_blacklist", "disabled_models"):
            value = spec.get(list_field)
            if list_field in spec and (
                not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value)
            ):
                errors.append(f"provider_{list_field}_must_be_string_list:{provider_id}")
    known_providers = set(base_config.backends) | {key for key in providers if isinstance(key, str)}
    for raw_ref, model_spec in models.items():
        if not isinstance(raw_ref, str) or "/" not in raw_ref:
            errors.append(f"model_ref_invalid:{raw_ref}")
            continue
        provider_id, model_id = _split_custom_model_ref(raw_ref)
        if provider_id not in known_providers:
            errors.append(f"model_provider_unknown:{raw_ref}")
        if provider_id in base_config.backends and _backend_model_ref(base_config.backends[provider_id]) == raw_ref and not (
            isinstance(model_spec, dict) and bool(model_spec.get("override", False))
        ):
            errors.append(f"model_duplicate_requires_override:{raw_ref}")
        errors.extend(_validate_custom_model_spec(provider_id, model_id, model_spec))
    return errors


def _custom_provider_backend(provider_id: str, spec: dict[str, Any], path: Path) -> BackendConfig:
    boundary = str(spec.get("data_boundary") or "local_only")
    enabled = bool(spec.get("enabled", boundary == "local_only"))
    models = spec.get("models") if isinstance(spec.get("models"), dict) else {}
    first_model_id = next(iter(models), str(spec.get("model") or ""))
    credential = spec.get("credential") if isinstance(spec.get("credential"), dict) else {}
    settings: dict[str, Any] = {
        "enabled": enabled,
        "base_url": str(spec.get("base_url") or spec.get("endpoint")).strip().rstrip("/"),
        "protocol": str(spec.get("protocol") or "openai_chat"),
        "model": str(first_model_id),
        "timeout_seconds": int(spec.get("timeout_seconds") or 300),
        "_custom_provider": {
            "display_name": str(spec.get("display_name") or provider_id),
            "source": "custom_config",
            "path": str(path),
            "compatibility": dict(spec.get("compatibility") or {}) if isinstance(spec.get("compatibility"), dict) else {},
            "model_allowlist": _string_list(spec.get("model_allowlist")) or _string_list(spec.get("model_whitelist")),
            "model_blocklist": _string_list(spec.get("model_blocklist")) or _string_list(spec.get("model_blacklist")),
            "disabled_models": _string_list(spec.get("disabled_models")),
        },
        "_custom_models": {
            str(model_id): dict(model_spec)
            for model_id, model_spec in models.items()
            if isinstance(model_id, str) and isinstance(model_spec, dict)
        },
    }
    if spec.get("approved_lan_endpoints") is not None:
        settings["approved_lan_endpoints"] = _string_list(spec.get("approved_lan_endpoints"))
    kind = str(credential.get("kind") or "none")
    settings["credential_kind"] = kind
    if kind == "env":
        settings["api_key_env"] = str(credential.get("env_var")).strip()
    elif kind == "static_local":
        settings["api_key"] = "local"
    elif kind == "codex_login":
        settings["auth_mode"] = "custom_login"
    elif kind == "aws_profile":
        settings["aws_profile_env"] = str(credential.get("profile_env") or "AWS_PROFILE").strip()
        if credential.get("profile"):
            settings["aws_profile"] = str(credential.get("profile")).strip()
    elif kind == "aws_env":
        settings["aws_region"] = str(spec.get("aws_region") or credential.get("aws_region") or "").strip() or None
    if isinstance(spec.get("headers"), dict):
        settings["header_env_refs"] = {
            str(header): str(value.get("env_var")).strip()
            for header, value in spec["headers"].items()
            if isinstance(value, dict) and value.get("kind") == "env"
        }
    max_context = _max_custom_context(models)
    return BackendConfig(
        name=provider_id,
        kind=BackendKind.NATIVE_MODEL,
        metadata=BackendMetadata(
            billing_mode="local_no_api_cost" if boundary == "local_only" else "paid_api",
            execution_location="local_machine" if boundary == "local_only" else "hosted",
            data_boundary=boundary,
            allow_network=boundary != "local_only",
        ),
        capabilities=BackendCapabilities(
            structured_output=bool(spec.get("structured_output", True)),
            tool_calling=bool(spec.get("tool_support", False)),
            json_mode=bool(spec.get("json_mode", True)),
            max_context_tokens=max_context,
        ),
        settings=settings,
    )


def _validate_custom_model_spec(provider_id: str, model_id: Any, model_spec: Any) -> list[str]:
    if not _valid_custom_id(str(model_id)):
        return [f"model_id_invalid:{provider_id}/{model_id}"]
    if not isinstance(model_spec, dict):
        return [f"model_must_be_mapping:{provider_id}/{model_id}"]
    errors: list[str] = []
    for int_field in ("context_window", "context_limit", "max_output_tokens"):
        if int_field in model_spec and _optional_positive_int(model_spec.get(int_field)) is None:
            errors.append(f"model_{int_field}_invalid:{provider_id}/{model_id}")
    if "protocol" in model_spec and str(model_spec.get("protocol")) not in _custom_protocols():
        errors.append(f"model_protocol_invalid:{provider_id}/{model_id}")
    if "status" in model_spec and str(model_spec.get("status")) not in {"active", "beta", "deprecated", "disabled"}:
        errors.append(f"model_status_invalid:{provider_id}/{model_id}")
    return errors


def _split_custom_model_ref(raw_ref: str) -> tuple[str, str]:
    provider_id, model_id = raw_ref.split("/", 1)
    return provider_id.strip(), model_id.strip()


def _backend_model_ref(backend: BackendConfig) -> str | None:
    model = backend.settings.get("model")
    if isinstance(model, str) and model.strip():
        return f"{backend.name}/{model.strip()}"
    return None


def _valid_custom_id(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip()) and not any(char.isspace() for char in value)


def _is_safe_local_url(base_url: str, approved_lan_endpoints: list[str]) -> bool:
    parsed = urllib.parse.urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    if parsed.hostname in {"localhost", "127.0.0.1", "::1"}:
        return True
    return base_url.rstrip("/") in {item.rstrip("/") for item in approved_lan_endpoints}


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, str) and item.strip()]


def _optional_positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _max_custom_context(models: Any) -> int | None:
    if not isinstance(models, dict):
        return None
    values = [
        _optional_positive_int(model.get("context_window", model.get("context_limit")))
        for model in models.values()
        if isinstance(model, dict)
    ]
    return max([value for value in values if value is not None], default=None)


def _custom_model_count(data: dict[str, Any]) -> int:
    providers = data.get("providers") if isinstance(data.get("providers"), dict) else {}
    top_models = data.get("models") if isinstance(data.get("models"), dict) else {}
    provider_models = 0
    for spec in providers.values():
        if isinstance(spec, dict) and isinstance(spec.get("models"), dict):
            provider_models += len(spec["models"])
    return len(top_models) + provider_models


def custom_models_validation_issues(errors: list[str]) -> list[dict[str, str]]:
    return [_custom_models_validation_issue(error) for error in errors]


def _custom_models_validation_issue(error: str) -> dict[str, str]:
    parts = error.split(":")
    code = parts[0]
    provider_id = parts[1] if len(parts) > 1 else ""
    field = parts[2] if len(parts) > 2 else ""
    model_provider, model_id = _split_issue_model_ref(provider_id)
    path = ".harness/models.yaml"
    message = "Custom model config is invalid."
    fix = "Edit .harness/models.yaml and rerun `harness models config validate`."

    if code == "custom_models_config_not_mapping":
        message = "The custom model config root must be a YAML mapping."
        fix = "Use top-level `providers:` and/or `models:` mappings."
    elif code == "providers_must_be_mapping":
        path = "providers"
        message = "`providers` must be a mapping keyed by provider id."
        fix = "Change `providers` to a YAML mapping such as `providers: {my_provider: {...}}`."
    elif code == "models_must_be_mapping":
        path = "models"
        message = "`models` must be a mapping keyed by provider/model ref."
        fix = "Change `models` to a YAML mapping such as `models: {provider/model: {...}}`."
    elif code == "provider_id_invalid":
        path = f"providers.{provider_id or '<provider_id>'}"
        message = "Provider ids must be non-empty strings without whitespace."
        fix = "Rename the provider id using letters, numbers, underscores, hyphens, or dots."
    elif code == "provider_must_be_mapping":
        path = f"providers.{provider_id}"
        message = "Provider definitions must be YAML mappings."
        fix = "Replace the provider value with a mapping containing data_boundary, base_url, protocol, credential, and models."
    elif code == "provider_duplicate_requires_override":
        path = f"providers.{provider_id}"
        message = "This custom provider shadows a built-in provider."
        fix = "Rename the provider or set `override: true` if the override is intentional."
    elif code == "provider_data_boundary_missing":
        path = f"providers.{provider_id}.data_boundary"
        message = "Custom providers must explicitly declare their data boundary."
        fix = "Set `data_boundary` to `local_only`, `hosted_provider`, or `external_router`."
    elif code == "provider_data_boundary_invalid":
        path = f"providers.{provider_id}.data_boundary"
        message = "Custom provider data_boundary is unsupported."
        fix = "Use one of: `local_only`, `hosted_provider`, `external_router`."
    elif code == "provider_protocol_invalid":
        path = f"providers.{provider_id}.protocol"
        message = "Custom provider protocol is unsupported."
        fix = "Use a registered protocol such as `openai_chat`, `openai_responses`, `anthropic_messages`, `google_generative`, or `bedrock_converse`."
    elif code == "provider_base_url_missing":
        path = f"providers.{provider_id}.base_url"
        message = "Custom providers must declare an endpoint."
        fix = "Set `base_url` or `endpoint` to the provider API endpoint."
    elif code == "provider_local_url_not_loopback_or_approved_lan":
        path = f"providers.{provider_id}.base_url"
        message = "A `local_only` custom provider endpoint must be loopback or an approved LAN endpoint."
        fix = "Use localhost/127.0.0.1, change data_boundary, or add the exact LAN endpoint to `approved_lan_endpoints`."
    elif code == "hosted_provider_enabled_requires_approved":
        provider_id = parts[2] if len(parts) > 2 else provider_id
        path = f"providers.{provider_id}.approved"
        message = "Enabled hosted or external custom providers require explicit approval."
        fix = "Set `approved: true` only after confirming the endpoint and data boundary are acceptable."
    elif code == "credential_must_be_mapping":
        path = f"providers.{provider_id}.credential"
        message = "Provider credential policy must be a mapping."
        fix = "Use `credential: {kind: env, env_var: NAME}` or another supported credential kind."
    elif code == "credential_missing":
        path = f"providers.{provider_id}.credential"
        message = "Custom providers must explicitly declare credential behavior."
        fix = "Set `credential` to a supported policy such as `{kind: static_local}`, `{kind: env, env_var: NAME}`, or `{kind: none}`."
    elif code == "credential_kind_invalid":
        path = f"providers.{provider_id}.credential.kind"
        message = "Provider credential kind is unsupported."
        fix = "Use one of: `none`, `env`, `static_local`, `api_key`, `oauth`, `codex_login`, `aws_env`, `aws_profile`."
    elif code == "credential_env_var_missing":
        path = f"providers.{provider_id}.credential.env_var"
        message = "Env credentials must name the environment variable to read."
        fix = "Set `credential.env_var` to the environment variable name, not the secret value."
    elif code == "credential_value_not_allowed":
        path = f"providers.{provider_id}.credential.{field or '<secret>'}"
        message = "Credential secret values must not be embedded in .harness/models.yaml."
        fix = "Move the secret to an environment variable or provider account secret store and reference it by name."
    elif code == "headers_must_be_mapping":
        path = f"providers.{provider_id}.headers"
        message = "Provider headers must be a mapping."
        fix = "Use header names mapped to env refs, for example `X-API-Key: {kind: env, env_var: MY_HEADER}`."
    elif code == "header_must_use_env_ref":
        path = f"providers.{provider_id}.headers.{field or '<header>'}"
        message = "Custom provider headers must use environment variable references."
        fix = "Set the header to `{kind: env, env_var: HEADER_ENV_NAME}`; do not place header values in config."
    elif code == "provider_models_must_be_mapping":
        path = f"providers.{provider_id}.models"
        message = "Provider models must be a mapping keyed by model id."
        fix = "Change `models` to a YAML mapping such as `models: {model-id: {...}}`."
    elif code.startswith("provider_") and code.endswith("_must_be_string_list"):
        list_field = code.removeprefix("provider_").removesuffix("_must_be_string_list")
        path = f"providers.{provider_id}.{list_field}"
        message = f"`{list_field}` must be a list of non-empty strings."
        fix = f"Use YAML list syntax, for example `{list_field}: [model-a, model-b]`."
    elif code == "model_ref_invalid":
        path = f"models.{provider_id or '<model_ref>'}"
        message = "Top-level model overrides must use provider/model refs."
        fix = "Rename the key to `provider_id/model_id`."
    elif code == "model_provider_unknown":
        path = f"models.{provider_id}"
        message = "Top-level model override references an unknown provider."
        fix = "Add a matching provider under `providers` or use an existing provider id."
    elif code == "model_duplicate_requires_override":
        path = f"models.{provider_id}"
        message = "This model override duplicates the provider's configured model."
        fix = "Set `override: true` on the model spec if the override is intentional."
    elif code == "model_id_invalid":
        path = f"providers.{model_provider}.models.{model_id or '<model_id>'}" if model_provider else "providers.<provider_id>.models"
        message = "Model ids must be non-empty strings without whitespace."
        fix = "Rename the model id using a provider-compatible id without spaces."
    elif code == "model_must_be_mapping":
        path = f"providers.{model_provider}.models.{model_id or '<model_id>'}" if model_provider else "providers.<provider_id>.models"
        message = "Model definitions must be YAML mappings."
        fix = "Replace the model value with a mapping containing context_window, max_output_tokens, status, or variants."
    elif code.startswith("model_") and code.endswith("_invalid"):
        raw_field = code.removeprefix("model_").removesuffix("_invalid")
        path = (
            f"providers.{model_provider}.models.{model_id or '<model_id>'}.{raw_field}"
            if model_provider
            else f"providers.<provider_id>.models.{field or '<model>'}.{raw_field}"
        )
        if raw_field in {"context_window", "context_limit", "max_output_tokens"}:
            message = f"`{raw_field}` must be a positive integer."
            fix = f"Set `{raw_field}` to a positive integer token limit."
        elif raw_field == "protocol":
            message = "Model protocol override is unsupported."
            fix = "Use one of the supported provider protocols or remove the model-level protocol override."
        elif raw_field == "status":
            message = "Model status is unsupported."
            fix = "Use one of: `active`, `beta`, `deprecated`, `disabled`."

    return {"code": code, "path": path, "message": message, "fix": fix}


def _split_issue_model_ref(value: str) -> tuple[str, str]:
    if "/" not in value:
        return "", ""
    provider_id, model_id = value.split("/", 1)
    return provider_id, model_id


def _custom_protocols() -> set[str]:
    return set(ALLOWED_MODEL_PROTOCOLS)
