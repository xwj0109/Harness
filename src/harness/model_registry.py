from __future__ import annotations

import os
from enum import Enum
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

from harness.config import HarnessConfig
from harness.models import BackendCapabilities, BackendKind, BackendMetadata
from harness.provider_auth import active_account_by_provider
from harness.registry import BUILTIN_SPECS_DIR, SpecRegistry, builtin_spec_registry


class ProviderCredentialStatus(str, Enum):
    NOT_REQUIRED = "not_required"
    CONFIGURED = "configured"
    MISSING = "missing"
    EXPIRED = "expired"
    REFRESH_REQUIRED = "refresh_required"
    UNKNOWN = "unknown"


CredentialKind = Literal["none", "env", "static_local", "codex_login", "api_key", "oauth", "aws_env", "aws_profile"]
ModelProtocol = Literal[
    "codex_cli",
    "openai_chat",
    "openai_responses",
    "openai_codex_responses",
    "anthropic_messages",
    "bedrock_converse",
    "google_generative",
]
ReasoningSupport = Literal["none", "effort", "tokens", "native", "unknown"]
ModelStatus = Literal["active", "beta", "deprecated", "disabled"]
ReasoningResolution = Literal["not_requested", "exact", "mapped"]


class ModelSelectionSource(str, Enum):
    COMMAND_ARG = "command_arg"
    SESSION_OVERRIDE = "session_override"
    SESSION_DEFAULT = "session_default"
    WORKSPACE_DEFAULT = "workspace_default"
    OPERATOR_PREFERENCE = "operator_preference"
    WORKBENCH_DEFAULT = "workbench_default"


PROVIDER_OPTION_KEYS = frozenset({"command", "timeout_seconds", "use_subscription_credits", "aws_region", "aws_profile"})
MODEL_OPTION_KEYS = frozenset(
    {
        "temperature",
        "max_tokens",
        "max_completion_tokens",
        "max_output_tokens",
        "context_tokens",
        "input_modalities",
        "requires_tools",
        "cache_retention",
        "cache_control",
        "thinking_budget_tokens",
        "anthropic_beta",
        "anthropic_betas",
        "anthropic_beta_headers",
        "model_reasoning_effort",
    }
)
REQUEST_OPTION_KEYS = PROVIDER_OPTION_KEYS | MODEL_OPTION_KEYS
CREDENTIAL_OPTION_KEYS = frozenset({"api_key", "api_key_env", "auth_mode", "credential_env", "authorization", "headers"})


class CredentialDescriptor(BaseModel):
    schema_version: str = "harness.credential_descriptor/v1"
    kind: CredentialKind
    env_var: str | None = None
    account_id: str | None = None
    account_description: str | None = None
    source: str = "config"
    status: ProviderCredentialStatus = ProviderCredentialStatus.UNKNOWN
    credential_write_supported: bool = False
    credential_written: bool = False


class ProviderDescriptor(BaseModel):
    schema_version: str = "harness.provider_descriptor/v1"
    provider_id: str
    display_name: str
    backend_id: str | None = None
    enabled: bool = True
    endpoint: str | None = None
    protocol_defaults: dict[str, Any] = Field(default_factory=dict)
    credential: CredentialDescriptor | None = None
    metadata: BackendMetadata
    capabilities: BackendCapabilities
    constraints: list[str] = Field(default_factory=list)
    policy_boundary: dict[str, Any] = Field(default_factory=lambda: _registry_policy_boundary("provider_descriptor"))
    source: str = "config"
    metadata_source: str | None = None
    metadata_only: bool = True
    provider_execution_started: bool = False
    model_execution_started: bool = False
    network_accessed: bool = False
    credentials_included: bool = False
    hidden_provider_fallback: bool = False
    hidden_model_fallback: bool = False
    no_hidden_fallback: bool = True


class ModelVariantDescriptor(BaseModel):
    schema_version: str = "harness.model_variant_descriptor/v1"
    variant_id: str
    display_name: str | None = None
    provider_options: dict[str, Any] = Field(default_factory=dict)
    model_options: dict[str, Any] = Field(default_factory=dict)
    reasoning_effort_map: dict[str, str | None] = Field(default_factory=dict)
    safety_notes: list[str] = Field(default_factory=list)


class ModelAliasDescriptor(BaseModel):
    schema_version: str = "harness.model_alias_descriptor/v1"
    alias: str
    target: str
    source: str = "builtin_alias"
    safety_notes: list[str] = Field(default_factory=list)


class ModelDescriptor(BaseModel):
    schema_version: str = "harness.model_descriptor/v1"
    provider_id: str
    model_id: str
    raw_model_ref: str
    canonical_model_ref: str | None = None
    alias_of: str | None = None
    api_id: str | None = None
    protocol: ModelProtocol
    backend_id: str | None = None
    model_profile_id: str | None = None
    endpoint: str | None = None
    provider_options: dict[str, Any] = Field(default_factory=dict)
    model_options: dict[str, Any] = Field(default_factory=dict)
    variants: dict[str, ModelVariantDescriptor] = Field(default_factory=dict)
    context_limit: int | None = None
    max_output_tokens: int | None = None
    input_modalities: list[str] = Field(default_factory=lambda: ["text"])
    output_modalities: list[str] = Field(default_factory=lambda: ["text"])
    tool_support: bool = False
    reasoning_support: ReasoningSupport = "unknown"
    reasoning_effort_map: dict[str, str | None] = Field(default_factory=dict)
    cost: dict[str, Any] | None = None
    status: ModelStatus = "active"
    release_date: str | None = None
    family: str | None = None
    source: str = "config"
    metadata_source: str | None = None
    policy_boundary: dict[str, Any] = Field(default_factory=lambda: _registry_policy_boundary("model_descriptor"))
    safety_notes: list[str] = Field(default_factory=list)
    metadata_only: bool = True
    provider_execution_started: bool = False
    model_execution_started: bool = False
    network_accessed: bool = False
    credentials_included: bool = False
    hidden_provider_fallback: bool = False
    hidden_model_fallback: bool = False
    no_hidden_fallback: bool = True


class ParsedModelRef(BaseModel):
    schema_version: str = "harness.parsed_model_ref/v1"
    raw_model_ref: str | None
    provider_id: str | None = None
    model_id: str | None = None
    variant: str | None = None


class ResolvedModelSelection(BaseModel):
    schema_version: str = "harness.resolved_model_selection/v1"
    raw_model_ref: str
    canonical_model_ref: str
    provider_id: str
    model_id: str
    variant: str | None = None
    alias_used: str | None = None
    provider: ProviderDescriptor
    model: ModelDescriptor
    resolved_endpoint: str | None = None
    resolved_provider_options: dict[str, Any] = Field(default_factory=dict)
    resolved_model_options: dict[str, Any] = Field(default_factory=dict)
    requested_reasoning_effort: str | None = None
    resolved_reasoning_effort: str | None = None
    reasoning_resolution: ReasoningResolution = "not_requested"
    policy_boundary: dict[str, Any] = Field(default_factory=lambda: _registry_policy_boundary("resolved_model_selection"))
    metadata_only: bool = True
    provider_execution_started: bool = False
    model_execution_started: bool = False
    network_accessed: bool = False
    credentials_included: bool = False
    hidden_provider_fallback: bool = False
    hidden_model_fallback: bool = False
    no_hidden_fallback: bool = True


class SessionModelResolution(BaseModel):
    schema_version: str = "harness.session_model_resolution/v1"
    session_id: str
    source: ModelSelectionSource | None = None
    raw_model_ref: str | None = None
    canonical_model_ref: str | None = None
    provider_id: str | None = None
    model_id: str | None = None
    variant: str | None = None
    alias_used: str | None = None
    resolved_model_selection: ResolvedModelSelection | None = None
    blocked_reasons: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    policy_boundary: dict[str, Any] = Field(default_factory=lambda: _registry_policy_boundary("session_model_resolution"))
    metadata_only: bool = True
    provider_execution_started: bool = False
    model_execution_started: bool = False
    network_accessed: bool = False
    credentials_included: bool = False
    hidden_provider_fallback: bool = False
    hidden_model_fallback: bool = False
    no_hidden_fallback: bool = True
    permission_granting: bool = False
    authority_granting: bool = False


class ModelResolutionError(ValueError):
    def __init__(self, raw_model_ref: str | None, blocked_reasons: list[str]) -> None:
        self.raw_model_ref = raw_model_ref
        self.blocked_reasons = blocked_reasons
        super().__init__(", ".join(blocked_reasons))


def build_provider_descriptors(
    config: HarnessConfig,
    *,
    provider_accounts: list[dict[str, Any]] | None = None,
) -> list[ProviderDescriptor]:
    metadata = load_builtin_provider_metadata()
    providers: list[ProviderDescriptor] = []
    accounts_by_provider = active_account_by_provider(provider_accounts or [])
    for backend_id, backend in sorted(config.backends.items()):
        item_metadata = metadata.get(backend_id, {})
        custom_provider = backend.settings.get("_custom_provider") if isinstance(backend.settings.get("_custom_provider"), dict) else {}
        enabled = bool(backend.settings.get("enabled", True))
        constraints = list(backend.to_descriptor().constraints)
        if not enabled and "disabled_by_config" not in constraints:
            constraints.append("disabled_by_config")
        providers.append(
            ProviderDescriptor(
                provider_id=backend_id,
                display_name=str(custom_provider.get("display_name") or item_metadata.get("display_name") or backend.name),
                backend_id=backend_id,
                enabled=enabled,
                endpoint=_backend_endpoint(backend.settings),
                protocol_defaults=_protocol_defaults(backend.kind, backend.settings),
                credential=_credential_descriptor(backend.settings, provider_account=accounts_by_provider.get(backend_id)),
                metadata=backend.metadata,
                capabilities=backend.capabilities,
                constraints=constraints,
                source=str(custom_provider.get("source") or "backend_config"),
                metadata_source=str(custom_provider.get("path") or item_metadata.get("source") or "") or None,
            )
        )
    return providers


def build_model_descriptors(
    config: HarnessConfig,
    registry: SpecRegistry | None = None,
) -> list[ModelDescriptor]:
    registry = registry or builtin_spec_registry()
    metadata = load_builtin_model_metadata()
    static_catalog = load_generated_static_model_catalog()
    descriptors: list[ModelDescriptor] = []
    for backend_id, backend in sorted(config.backends.items()):
        for static_model_id, static_model_metadata in sorted(_static_catalog_model_metadata(static_catalog, backend_id).items()):
            descriptors.append(
                _model_descriptor_for_backend(
                    backend_id,
                    backend,
                    static_model_id,
                    source="static_catalog",
                    metadata={f"{backend_id}/{static_model_id}": static_model_metadata},
                )
            )
        model_id = _backend_model_id(backend.settings)
        if model_id is not None:
            descriptors.append(_model_descriptor_for_backend(backend_id, backend, model_id, source="backend_config", metadata=metadata))
        for custom_model_id, custom_model_metadata in sorted(_custom_model_metadata(backend).items()):
            descriptors.append(
                _model_descriptor_for_backend(
                    backend_id,
                    backend,
                    custom_model_id,
                    source="custom_config",
                    metadata={f"{backend_id}/{custom_model_id}": custom_model_metadata},
                )
            )
    for profile_id, profile in sorted(registry.model_profiles.items()):
        backend = config.backends.get(profile.backend)
        if backend is None:
            continue
        model_id = _backend_model_id(backend.settings) or profile.id
        descriptors.append(
            _model_descriptor_for_backend(
                profile.backend,
                backend,
                model_id,
                source="model_profile",
                model_profile_id=profile_id,
                metadata=metadata,
            )
        )
    descriptors = _dedupe_model_descriptors(descriptors)
    descriptors.extend(_model_alias_descriptors(descriptors, load_builtin_model_aliases()))
    return sorted(
        descriptors,
        key=lambda item: (
            item.provider_id,
            item.source,
            item.model_profile_id or "",
            item.model_id,
            item.raw_model_ref,
        ),
    )


def parse_model_ref(raw_model_ref: str | None) -> ParsedModelRef:
    if raw_model_ref is None:
        return ParsedModelRef(raw_model_ref=None)
    raw = raw_model_ref.strip()
    if not raw:
        return ParsedModelRef(raw_model_ref=raw)
    provider_id: str | None = None
    model_id = raw
    variant: str | None = None
    if "/" in raw:
        provider_id, model_id = raw.split("/", 1)
    if "@" in model_id:
        model_id, variant = model_id.rsplit("@", 1)
    elif ":" in model_id and _looks_like_variant_suffix(model_id):
        model_id, variant = model_id.rsplit(":", 1)
    return ParsedModelRef(
        raw_model_ref=raw,
        provider_id=provider_id or None,
        model_id=model_id or None,
        variant=variant or None,
    )


def resolve_model_selection(
    config: HarnessConfig,
    raw_model_ref: str,
    registry: SpecRegistry | None = None,
    *,
    request_options: dict[str, Any] | None = None,
) -> ResolvedModelSelection:
    raw = raw_model_ref.strip()
    parsed = parse_model_ref(raw)
    providers = {provider.provider_id: provider for provider in build_provider_descriptors(config)}
    models = build_model_descriptors(config, registry)
    alias, alias_target_ref = _resolve_alias_target(raw, parsed, load_builtin_model_aliases())
    model = None
    alias_used: str | None = None
    if alias is not None:
        alias_used = raw
        model = _match_model_descriptor(models, alias_target_ref, parse_model_ref(alias_target_ref), include_aliases=False)
        if model is None:
            raise ModelResolutionError(raw, ["alias_target_unknown"])
    else:
        model = _match_model_descriptor(models, raw, parsed)
    if model is None:
        reasons = ["model_ref_missing"] if not raw else []
        if parsed.provider_id is None:
            reasons.append("provider_not_specified")
        elif parsed.provider_id not in providers:
            reasons.append("provider_unknown")
        reasons.append("model_unknown")
        raise ModelResolutionError(raw or None, reasons)
    provider = providers.get(model.provider_id)
    if provider is None:
        raise ModelResolutionError(raw, ["provider_unknown"])
    variant = parsed.variant if alias is None else parse_model_ref(alias_target_ref).variant
    if variant is not None and variant not in model.variants:
        raise ModelResolutionError(raw, ["variant_unknown"])
    provider_options, model_options = _resolve_options(raw, provider, model, variant, request_options)
    requested_reasoning, resolved_reasoning, reasoning_resolution = _resolve_reasoning_effort(
        raw,
        model,
        variant,
        provider_options,
        model_options,
    )
    _validate_model_request_capabilities(raw, model, model_options)
    return ResolvedModelSelection(
        raw_model_ref=raw,
        canonical_model_ref=model.canonical_model_ref or model.raw_model_ref,
        provider_id=model.provider_id,
        model_id=model.model_id,
        variant=variant,
        alias_used=alias_used,
        provider=provider,
        model=model,
        resolved_endpoint=model.endpoint or provider.endpoint,
        resolved_provider_options=provider_options,
        resolved_model_options=model_options,
        requested_reasoning_effort=requested_reasoning,
        resolved_reasoning_effort=resolved_reasoning,
        reasoning_resolution=reasoning_resolution,
    )


def resolve_model_for_session(
    config: HarnessConfig,
    store: Any,
    session_id: str,
    requested_ref: str | None = None,
    registry: SpecRegistry | None = None,
    *,
    request_options: dict[str, Any] | None = None,
) -> SessionModelResolution:
    registry = registry or builtin_spec_registry()
    session = store.get_session(session_id)
    candidate = _session_model_resolution_candidate(config, store, session, requested_ref, registry)
    if candidate is None:
        return SessionModelResolution(
            session_id=session_id,
            blocked_reasons=["model_ref_missing"],
            reasons=["No explicit, session, workspace, operator, or workbench model default resolved to a concrete catalog ref."],
        )
    source, raw_ref, reason = candidate
    try:
        resolved = resolve_model_selection(config, raw_ref, registry, request_options=request_options)
    except ModelResolutionError as exc:
        return SessionModelResolution(
            session_id=session_id,
            source=source,
            raw_model_ref=raw_ref,
            blocked_reasons=exc.blocked_reasons,
            reasons=[reason, f"Model resolution failed: {', '.join(exc.blocked_reasons)}"],
        )
    return SessionModelResolution(
        session_id=session_id,
        source=source,
        raw_model_ref=raw_ref,
        canonical_model_ref=resolved.canonical_model_ref,
        provider_id=resolved.provider_id,
        model_id=resolved.model_id,
        variant=resolved.variant,
        alias_used=resolved.alias_used,
        resolved_model_selection=resolved,
        reasons=[reason],
    )


def _session_model_resolution_candidate(
    config: HarnessConfig,
    store: Any,
    session: Any,
    requested_ref: str | None,
    registry: SpecRegistry,
) -> tuple[ModelSelectionSource, str, str] | None:
    requested = _clean_model_ref(requested_ref)
    if requested:
        return (ModelSelectionSource.COMMAND_ARG, requested, "Using explicit requested model ref.")
    session_ref = _clean_model_ref(getattr(session, "raw_model_ref", None))
    if session_ref:
        return (ModelSelectionSource.SESSION_OVERRIDE, session_ref, "Using session selected model ref.")
    session_default = _session_default_model_ref(getattr(session, "metadata", {}) or {})
    if session_default:
        return (ModelSelectionSource.SESSION_DEFAULT, session_default, "Using session default model ref.")
    workspace_default = _workspace_default_model_ref(config)
    if workspace_default:
        return (ModelSelectionSource.WORKSPACE_DEFAULT, workspace_default, "Using workspace configured default model ref.")
    preference = _default_model_preference(store)
    preference_ref = _clean_model_ref(preference.get("raw_model_ref") if preference else None)
    if preference_ref:
        return (ModelSelectionSource.OPERATOR_PREFERENCE, preference_ref, "Using operator default model preference.")
    workbench_ref = _workbench_default_model_ref(config, registry, getattr(session, "workbench_id", None))
    if workbench_ref:
        return (ModelSelectionSource.WORKBENCH_DEFAULT, workbench_ref, "Using workbench default model profile resolved to a catalog ref.")
    return None


def _clean_model_ref(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    clean = value.strip()
    return clean or None


def _session_default_model_ref(metadata: dict[str, Any]) -> str | None:
    for key in ("session_default_model_ref", "default_model_ref", "default_raw_model_ref"):
        value = _clean_model_ref(metadata.get(key))
        if value:
            return value
    return None


def _workspace_default_model_ref(config: HarnessConfig) -> str | None:
    chat = getattr(config, "chat", None)
    explicit = _clean_model_ref(getattr(chat, "default_model_ref", None))
    if explicit:
        return explicit
    profile_or_ref = _clean_model_ref(getattr(chat, "default_model_profile", None))
    if profile_or_ref and "/" in profile_or_ref:
        return profile_or_ref
    return None


def _default_model_preference(store: Any) -> dict[str, Any] | None:
    if not hasattr(store, "get_default_model_preference"):
        return None
    try:
        preference = store.get_default_model_preference()
    except Exception:
        return None
    return preference if isinstance(preference, dict) else None


def _workbench_default_model_ref(config: HarnessConfig, registry: SpecRegistry, workbench_id: str | None) -> str | None:
    clean_workbench_id = _clean_model_ref(workbench_id)
    if not clean_workbench_id:
        return None
    workbench = registry.workbenches.get(clean_workbench_id)
    if workbench is None:
        return None
    return _model_profile_concrete_ref(config, registry, workbench.default_model_profile)


def _model_profile_concrete_ref(config: HarnessConfig, registry: SpecRegistry, profile_id: str | None) -> str | None:
    clean_profile_id = _clean_model_ref(profile_id)
    if not clean_profile_id:
        return None
    descriptors = [
        descriptor
        for descriptor in build_model_descriptors(config, registry)
        if descriptor.model_profile_id == clean_profile_id and descriptor.alias_of is None
    ]
    if not descriptors:
        return None
    return _prefer_backend_config(descriptors).raw_model_ref


def _model_descriptor_for_backend(
    backend_id: str,
    backend,
    model_id: str,
    *,
    source: str,
    model_profile_id: str | None = None,
    metadata: dict[str, dict[str, Any]] | None = None,
) -> ModelDescriptor:
    raw_model_ref = f"{backend_id}/{model_id}"
    model_metadata = (metadata or {}).get(raw_model_ref, {})
    api_id = str(model_metadata.get("api_id") or backend.settings.get("api_id") or model_id)
    metadata_status = model_metadata.get("status")
    status = "disabled" if backend.settings.get("enabled") is False else str(metadata_status or "active")
    return ModelDescriptor(
        provider_id=backend_id,
        model_id=model_id,
        raw_model_ref=raw_model_ref,
        canonical_model_ref=raw_model_ref,
        api_id=api_id,
        protocol=_model_protocol(backend.kind, backend.settings, backend_id, model_metadata),
        backend_id=backend_id,
        model_profile_id=model_profile_id,
        endpoint=_backend_endpoint(backend.settings),
        provider_options=_safe_options(model_metadata.get("provider_options")),
        model_options=_safe_options(model_metadata.get("model_options")),
        variants=_model_variants(model_metadata.get("variants")),
        context_limit=_optional_int(model_metadata.get("context_limit")) or backend.capabilities.max_context_tokens,
        max_output_tokens=_optional_int(model_metadata.get("max_output_tokens")),
        input_modalities=_string_list(model_metadata.get("input_modalities")) or ["text"],
        output_modalities=_string_list(model_metadata.get("output_modalities")) or ["text"],
        tool_support=bool(model_metadata.get("tool_support", backend.capabilities.tool_calling)),
        reasoning_support=_reasoning_support(backend.settings, model_metadata),
        reasoning_effort_map=_reasoning_effort_map(backend.settings, model_metadata),
        cost=model_metadata.get("cost") if isinstance(model_metadata.get("cost"), dict) else None,
        status=status,  # type: ignore[arg-type]
        release_date=str(model_metadata.get("release_date") or "") or None,
        family=str(model_metadata.get("family") or "") or None,
        source=source,
        metadata_source=str(model_metadata.get("source") or "") or None,
        safety_notes=[
            f"Model descriptor {raw_model_ref} is metadata only and must be validated before execution.",
            "Unknown refs must fail visibly instead of using hidden fallback.",
            *_string_list(model_metadata.get("safety_notes")),
        ],
    )


def _backend_model_id(settings: dict[str, Any]) -> str | None:
    model = settings.get("model")
    if isinstance(model, str) and model.strip():
        return model.strip()
    return None


def _backend_endpoint(settings: dict[str, Any]) -> str | None:
    base_url = settings.get("base_url")
    if isinstance(base_url, str) and base_url.strip():
        return base_url.strip().rstrip("/")
    return None


def _model_protocol(
    kind: BackendKind,
    settings: dict[str, Any],
    backend_id: str,
    metadata: dict[str, Any] | None = None,
) -> ModelProtocol:
    protocol = settings.get("protocol")
    if isinstance(protocol, str) and protocol.strip():
        return protocol.strip()  # type: ignore[return-value]
    metadata_protocol = (metadata or {}).get("protocol")
    if isinstance(metadata_protocol, str) and metadata_protocol.strip():
        return metadata_protocol.strip()  # type: ignore[return-value]
    if backend_id == "codex_cli" or kind == BackendKind.EXTERNAL_AGENT:
        return "codex_cli"
    return "openai_chat"


def _protocol_defaults(kind: BackendKind, settings: dict[str, Any]) -> dict[str, Any]:
    if kind == BackendKind.EXTERNAL_AGENT:
        return {
            "protocol": "codex_cli",
            "command": settings.get("command", "codex"),
            "model_reasoning_effort": settings.get("model_reasoning_effort"),
            "use_subscription_credits": settings.get("use_subscription_credits"),
        }
    defaults: dict[str, Any] = {"protocol": settings.get("protocol")}
    custom_provider = settings.get("_custom_provider") if isinstance(settings.get("_custom_provider"), dict) else {}
    compatibility = custom_provider.get("compatibility") if isinstance(custom_provider.get("compatibility"), dict) else {}
    if compatibility:
        defaults["compatibility"] = dict(compatibility)
    for key in ("temperature", "max_tokens", "timeout_seconds", "aws_region", "aws_profile"):
        if key in settings:
            defaults[key] = settings[key]
    return {key: value for key, value in defaults.items() if value is not None}


def _credential_descriptor(
    settings: dict[str, Any],
    *,
    provider_account: dict[str, Any] | None = None,
) -> CredentialDescriptor:
    if provider_account is not None:
        account_metadata = provider_account.get("metadata") if isinstance(provider_account.get("metadata"), dict) else {}
        return CredentialDescriptor(
            kind=_credential_kind(str(provider_account.get("credential_kind") or "api_key")),
            env_var=str(account_metadata.get("env_var") or "") or None,
            account_id=str(provider_account.get("account_id") or "") or None,
            account_description=str(provider_account.get("description") or "") or None,
            source="provider_account",
            status=_credential_status(str(provider_account.get("status") or "unknown")),
            credential_write_supported=True,
            credential_written=False,
        )
    api_key_env = settings.get("api_key_env")
    if isinstance(api_key_env, str) and api_key_env.strip():
        return CredentialDescriptor(
            kind="env",
            env_var=api_key_env.strip(),
            source="env",
            status=ProviderCredentialStatus.CONFIGURED
            if os.environ.get(api_key_env.strip())
            else ProviderCredentialStatus.MISSING,
        )
    if "auth_mode" in settings:
        return CredentialDescriptor(kind="codex_login", source="config", status=ProviderCredentialStatus.CONFIGURED)
    if "api_key" in settings:
        return CredentialDescriptor(kind="static_local", source="static_local", status=ProviderCredentialStatus.CONFIGURED)
    configured_kind = settings.get("credential_kind")
    if isinstance(configured_kind, str) and configured_kind.strip():
        kind = _credential_kind(configured_kind.strip())
        if kind == "aws_profile":
            profile_env = str(settings.get("aws_profile_env") or "AWS_PROFILE")
            return CredentialDescriptor(
                kind=kind,
                env_var=profile_env,
                source="aws_profile",
                status=ProviderCredentialStatus.CONFIGURED if os.environ.get(profile_env) else ProviderCredentialStatus.MISSING,
            )
        if kind == "aws_env":
            return CredentialDescriptor(
                kind=kind,
                env_var="AWS_ACCESS_KEY_ID",
                source="aws_env",
                status=ProviderCredentialStatus.CONFIGURED if os.environ.get("AWS_ACCESS_KEY_ID") else ProviderCredentialStatus.MISSING,
            )
        status = ProviderCredentialStatus.NOT_REQUIRED if kind == "none" else ProviderCredentialStatus.MISSING
        return CredentialDescriptor(kind=kind, source="custom_config", status=status)
    return CredentialDescriptor(kind="none", source="config", status=ProviderCredentialStatus.NOT_REQUIRED)


def _credential_kind(value: str) -> CredentialKind:
    if value in {"none", "env", "static_local", "codex_login", "api_key", "oauth", "aws_env", "aws_profile"}:
        return value  # type: ignore[return-value]
    return "api_key"


def _credential_status(value: str) -> ProviderCredentialStatus:
    try:
        return ProviderCredentialStatus(value)
    except ValueError:
        return ProviderCredentialStatus.UNKNOWN


def _reasoning_support(settings: dict[str, Any], metadata: dict[str, Any] | None = None) -> ReasoningSupport:
    metadata_reasoning = (metadata or {}).get("reasoning_support")
    if isinstance(metadata_reasoning, str) and metadata_reasoning.strip():
        return metadata_reasoning.strip()  # type: ignore[return-value]
    if "model_reasoning_effort" in settings:
        return "effort"
    return "unknown"


def _reasoning_effort_map(settings: dict[str, Any], metadata: dict[str, Any] | None = None) -> dict[str, str | None]:
    metadata_map = (metadata or {}).get("reasoning_effort_map")
    if isinstance(metadata_map, dict):
        return {str(key): (str(value) if value is not None else None) for key, value in metadata_map.items()}
    effort = settings.get("model_reasoning_effort")
    if not isinstance(effort, str) or not effort.strip():
        return {}
    return {effort.strip(): effort.strip()}


def _resolve_alias_target(
    raw: str,
    parsed: ParsedModelRef,
    aliases: dict[str, ModelAliasDescriptor],
) -> tuple[ModelAliasDescriptor | None, str]:
    alias = aliases.get(raw)
    if alias is not None:
        return alias, alias.target
    if parsed.provider_id is None or parsed.model_id is None or parsed.variant is None:
        return None, raw
    base_ref = f"{parsed.provider_id}/{parsed.model_id}"
    alias = aliases.get(base_ref)
    if alias is None:
        return None, raw
    target = alias.target
    if parse_model_ref(target).variant is None:
        target = f"{target}@{parsed.variant}"
    return alias, target


def _resolve_options(
    raw_model_ref: str,
    provider: ProviderDescriptor,
    model: ModelDescriptor,
    variant: str | None,
    request_options: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    provider_options = _filter_options(provider.protocol_defaults, PROVIDER_OPTION_KEYS | MODEL_OPTION_KEYS)
    provider_options.update(_filter_options(model.provider_options, PROVIDER_OPTION_KEYS))
    model_options = _filter_options(model.model_options, MODEL_OPTION_KEYS)
    if variant is not None:
        variant_descriptor = model.variants[variant]
        provider_options.update(_filter_options(variant_descriptor.provider_options, PROVIDER_OPTION_KEYS))
        model_options.update(_filter_options(variant_descriptor.model_options, MODEL_OPTION_KEYS))
    if request_options:
        disallowed = [
            key
            for key in request_options
            if key in CREDENTIAL_OPTION_KEYS or key not in REQUEST_OPTION_KEYS
        ]
        if disallowed:
            raise ModelResolutionError(raw_model_ref, ["option_key_disallowed"])
        provider_options.update(
            {key: value for key, value in request_options.items() if key in PROVIDER_OPTION_KEYS and value is not None}
        )
        model_options.update(
            {key: value for key, value in request_options.items() if key in MODEL_OPTION_KEYS and value is not None}
        )
    return provider_options, model_options


def _resolve_reasoning_effort(
    raw_model_ref: str,
    model: ModelDescriptor,
    variant: str | None,
    provider_options: dict[str, Any],
    model_options: dict[str, Any],
) -> tuple[str | None, str | None, ReasoningResolution]:
    requested = model_options.get("model_reasoning_effort", provider_options.get("model_reasoning_effort"))
    if requested is None:
        return None, None, "not_requested"
    requested_effort = str(requested).strip()
    if not requested_effort:
        return None, None, "not_requested"
    if model.reasoning_support == "none":
        raise ModelResolutionError(raw_model_ref, ["reasoning_effort_unsupported"])
    effort_map = dict(model.reasoning_effort_map)
    if variant is not None:
        effort_map.update(model.variants[variant].reasoning_effort_map)
    if model.reasoning_support == "native" and not effort_map:
        model_options["model_reasoning_effort"] = requested_effort
        return requested_effort, requested_effort, "exact"
    if requested_effort not in effort_map:
        raise ModelResolutionError(raw_model_ref, ["reasoning_effort_unsupported"])
    resolved = effort_map[requested_effort]
    resolution: ReasoningResolution = "exact" if resolved == requested_effort else "mapped"
    if resolved is None:
        model_options.pop("model_reasoning_effort", None)
        return requested_effort, None, "mapped"
    model_options["model_reasoning_effort"] = resolved
    return requested_effort, resolved, resolution


def _validate_model_request_capabilities(
    raw_model_ref: str,
    model: ModelDescriptor,
    model_options: dict[str, Any],
) -> None:
    blocked: list[str] = []
    context_tokens = _optional_int(model_options.get("context_tokens"))
    if context_tokens is not None and model.context_limit is not None and context_tokens > model.context_limit:
        blocked.append("context_limit_exceeded")
    requested_output = _optional_int(model_options.get("max_output_tokens")) or _optional_int(model_options.get("max_tokens"))
    if requested_output is not None and model.max_output_tokens is not None and requested_output > model.max_output_tokens:
        blocked.append("output_limit_exceeded")
    requested_modalities = _string_list(model_options.get("input_modalities"))
    if requested_modalities:
        supported = {modality.lower() for modality in model.input_modalities}
        unsupported = sorted({modality.lower() for modality in requested_modalities if modality.lower() not in supported})
        if unsupported:
            blocked.append("input_modality_unsupported")
    if _truthy(model_options.get("requires_tools")) and not model.tool_support:
        blocked.append("tool_support_unsupported")
    if blocked:
        raise ModelResolutionError(raw_model_ref, blocked)


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _filter_options(options: dict[str, Any], allowed_keys: frozenset[str]) -> dict[str, Any]:
    return {
        key: value
        for key, value in options.items()
        if key in allowed_keys and key not in CREDENTIAL_OPTION_KEYS and value is not None
    }


def _match_model_descriptor(
    models: list[ModelDescriptor],
    raw_model_ref: str | None,
    parsed: ParsedModelRef,
    *,
    include_aliases: bool = True,
) -> ModelDescriptor | None:
    if not raw_model_ref:
        return None
    candidates_to_search = models if include_aliases else [model for model in models if model.alias_of is None]
    exact = [model for model in candidates_to_search if model.raw_model_ref == raw_model_ref]
    if exact:
        return _prefer_backend_config(exact)
    if parsed.provider_id is None or parsed.model_id is None:
        return None
    candidates = [
        model
        for model in candidates_to_search
        if model.provider_id == parsed.provider_id and model.model_id == parsed.model_id
    ]
    return _prefer_backend_config(candidates) if candidates else None


def _prefer_backend_config(models: list[ModelDescriptor]) -> ModelDescriptor:
    for model in models:
        if model.source == "backend_config":
            return model
    return models[0]


def _looks_like_variant_suffix(model_id: str) -> bool:
    suffix = model_id.rsplit(":", 1)[-1]
    return suffix in {"low", "medium", "high", "xhigh", "minimal", "fast", "smart"}


def _model_variants(value: Any) -> dict[str, ModelVariantDescriptor]:
    if not isinstance(value, dict):
        return {}
    variants: dict[str, ModelVariantDescriptor] = {}
    for key, raw_variant in value.items():
        if not isinstance(key, str) or not key.strip() or not isinstance(raw_variant, dict):
            continue
        variants[key] = ModelVariantDescriptor(
            variant_id=key,
            display_name=str(raw_variant.get("display_name")) if raw_variant.get("display_name") is not None else None,
            provider_options=_safe_options(raw_variant.get("provider_options")),
            model_options=_safe_options(raw_variant.get("model_options")),
            reasoning_effort_map=_reasoning_effort_map({}, raw_variant),
            safety_notes=_string_list(raw_variant.get("safety_notes")),
        )
    return variants


def _custom_model_metadata(backend) -> dict[str, dict[str, Any]]:
    values = backend.settings.get("_custom_models")
    if not isinstance(values, dict):
        return {}
    custom_provider = backend.settings.get("_custom_provider") if isinstance(backend.settings.get("_custom_provider"), dict) else {}
    allowlist = set(_string_list(custom_provider.get("model_allowlist")))
    blocklist = set(_string_list(custom_provider.get("model_blocklist")))
    disabled_models = set(_string_list(custom_provider.get("disabled_models")))
    result: dict[str, dict[str, Any]] = {}
    for model_id, metadata in values.items():
        if not isinstance(model_id, str) or not model_id.strip() or not isinstance(metadata, dict):
            continue
        clean_model_id = model_id.strip()
        if allowlist and clean_model_id not in allowlist:
            continue
        if clean_model_id in blocklist:
            continue
        item = dict(metadata)
        if "context_window" in item and "context_limit" not in item:
            item["context_limit"] = item["context_window"]
        if "id" in item and "model_id" not in item:
            item["model_id"] = item["id"]
        if clean_model_id in disabled_models:
            item["status"] = "disabled"
        result[clean_model_id] = item
    return result


def _dedupe_model_descriptors(descriptors: list[ModelDescriptor]) -> list[ModelDescriptor]:
    result: list[ModelDescriptor] = []
    for descriptor in descriptors:
        if descriptor.source not in {"custom_config", "backend_config"}:
            result.append(descriptor)
            continue
        replaced = False
        for index, existing in enumerate(result):
            if existing.raw_model_ref == descriptor.raw_model_ref and existing.source in {"backend_config", "custom_config", "static_catalog"}:
                result[index] = descriptor
                replaced = True
                break
        if not replaced:
            result.append(descriptor)
    return result


def _safe_options(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {
        str(key): item
        for key, item in value.items()
        if isinstance(key, str) and key not in CREDENTIAL_OPTION_KEYS and item is not None
    }


def load_builtin_provider_metadata(root: Path = BUILTIN_SPECS_DIR) -> dict[str, dict[str, Any]]:
    return _load_builtin_metadata_file(root / "providers.yaml", "providers")


def load_builtin_model_metadata(root: Path = BUILTIN_SPECS_DIR) -> dict[str, dict[str, Any]]:
    return _load_builtin_metadata_file(root / "models.yaml", "models")


def load_generated_static_model_catalog(root: Path = BUILTIN_SPECS_DIR) -> dict[str, dict[str, Any]]:
    path = root / "generated" / "static_model_catalog.yaml"
    if not path.exists():
        return {}
    return _load_builtin_metadata_file(path, "models")


def load_builtin_model_aliases(root: Path = BUILTIN_SPECS_DIR) -> dict[str, ModelAliasDescriptor]:
    metadata = _load_builtin_metadata_file(root / "model_aliases.yaml", "aliases")
    aliases: dict[str, ModelAliasDescriptor] = {}
    for alias, value in metadata.items():
        target = value.get("target")
        if not isinstance(target, str) or not target.strip():
            raise ValueError(f"Built-in model alias target must be non-empty: {alias}")
        aliases[alias] = ModelAliasDescriptor(
            alias=alias,
            target=target.strip(),
            source=str(value.get("source") or "builtin_alias"),
            safety_notes=_string_list(value.get("safety_notes")),
        )
    return aliases


def _model_alias_descriptors(
    descriptors: list[ModelDescriptor],
    aliases: dict[str, ModelAliasDescriptor],
) -> list[ModelDescriptor]:
    alias_descriptors: list[ModelDescriptor] = []
    canonical_by_ref = {
        descriptor.raw_model_ref: descriptor
        for descriptor in descriptors
        if descriptor.alias_of is None and descriptor.source == "backend_config"
    }
    for alias_ref, alias in sorted(aliases.items()):
        canonical = canonical_by_ref.get(alias.target)
        if canonical is None:
            continue
        parsed = parse_model_ref(alias_ref)
        alias_descriptors.append(
            canonical.model_copy(
                deep=True,
                update={
                    "provider_id": parsed.provider_id or canonical.provider_id,
                    "model_id": parsed.model_id or canonical.model_id,
                    "raw_model_ref": alias.alias,
                    "canonical_model_ref": canonical.raw_model_ref,
                    "alias_of": canonical.raw_model_ref,
                    "model_profile_id": None,
                    "source": "alias",
                    "metadata_source": alias.source,
                    "safety_notes": [
                        f"Model alias {alias.alias} resolves to canonical ref {canonical.raw_model_ref}.",
                        "Alias resolution must validate the canonical provider and model before execution.",
                        "Unknown or disabled alias targets must fail visibly instead of using hidden fallback.",
                        *alias.safety_notes,
                    ],
                },
            )
        )
    return alias_descriptors


def _static_catalog_model_metadata(static_catalog: dict[str, dict[str, Any]], backend_id: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for raw_model_ref, metadata in static_catalog.items():
        if not isinstance(metadata, dict):
            continue
        parsed = parse_model_ref(raw_model_ref)
        provider_id = str(metadata.get("provider_id") or parsed.provider_id or "")
        model_id = str(metadata.get("model_id") or parsed.model_id or "")
        if provider_id != backend_id or not model_id.strip():
            continue
        item = dict(metadata)
        item.setdefault("source", "generated_static_catalog")
        result[model_id.strip()] = item
    return result


def _load_builtin_metadata_file(path: Path, section: str) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    values = data.get(section, data)
    if not isinstance(values, dict):
        raise ValueError(f"Built-in {section} metadata must be a mapping.")
    result: dict[str, dict[str, Any]] = {}
    for key, value in values.items():
        if not isinstance(key, str) or not key.strip():
            raise ValueError(f"Built-in {section} metadata key must be non-empty.")
        if not isinstance(value, dict):
            raise ValueError(f"Built-in {section} metadata value must be a mapping: {key}")
        result[key] = dict(value)
    return result


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, str) and item.strip()]


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _registry_policy_boundary(kind: str) -> dict[str, Any]:
    return {
        "kind": kind,
        "source": "provider_model_registry",
        "metadata_only": True,
    }
