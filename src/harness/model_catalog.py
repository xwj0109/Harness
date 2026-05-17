from __future__ import annotations

import os
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from harness.config import HarnessConfig
from harness.models import BackendCapabilities, BackendKind, BackendMetadata
from harness.registry import SpecRegistry, builtin_spec_registry


class ProviderCredentialStatus(str, Enum):
    NOT_REQUIRED = "not_required"
    CONFIGURED = "configured"
    MISSING = "missing"
    UNKNOWN = "unknown"


class ProviderCatalogEntry(BaseModel):
    schema_version: str = "harness.provider_catalog_entry/v1"
    provider_id: str
    backend_id: str
    kind: BackendKind
    enabled: bool
    credential_status: ProviderCredentialStatus
    metadata: BackendMetadata
    capabilities: BackendCapabilities
    settings_preview: dict[str, Any] = Field(default_factory=dict)
    constraints: list[str] = Field(default_factory=list)
    policy_boundary: dict[str, Any] = Field(default_factory=dict)
    metadata_only: bool = True
    provider_execution_started: bool = False
    model_execution_started: bool = False
    network_accessed: bool = False
    credentials_included: bool = False
    credential_write_supported: bool = False
    credential_written: bool = False
    refresh_supported: bool = False
    hidden_provider_fallback: bool = False
    hidden_model_fallback: bool = False
    no_hidden_fallback: bool = True
    permission_granting: bool = False
    authority_granting: bool = False
    safety_notes: list[str] = Field(default_factory=list)


class ModelCatalogEntry(BaseModel):
    schema_version: str = "harness.model_catalog_entry/v1"
    provider_id: str
    backend_id: str
    model_id: str
    raw_model_ref: str
    variant: str | None = None
    model_profile_id: str | None = None
    source: str
    capabilities: BackendCapabilities
    context_limit: int | None = None
    cost: dict[str, Any] | None = None
    modalities: list[str] = Field(default_factory=lambda: ["text"])
    reasoning_support: str = "unknown"
    tool_support: bool = False
    policy_boundary: dict[str, Any] = Field(default_factory=dict)
    metadata_only: bool = True
    provider_execution_started: bool = False
    model_execution_started: bool = False
    network_accessed: bool = False
    credentials_included: bool = False
    refresh_supported: bool = False
    hidden_provider_fallback: bool = False
    hidden_model_fallback: bool = False
    no_hidden_fallback: bool = True
    permission_granting: bool = False
    authority_granting: bool = False
    safety_notes: list[str] = Field(default_factory=list)


class ModelSelectionValidation(BaseModel):
    schema_version: str = "harness.model_selection_validation/v1"
    raw_model_ref: str | None
    provider_id: str | None = None
    model_id: str | None = None
    variant: str | None = None
    known_catalog_entry: bool = False
    provider_known: bool = False
    provider_enabled: bool = False
    executable: bool = False
    matched_model: ModelCatalogEntry | None = None
    reasons: list[str] = Field(default_factory=list)
    blocked_reasons: list[str] = Field(default_factory=list)
    policy_boundary: dict[str, Any] = Field(default_factory=lambda: _catalog_policy_boundary("model_selection_validation"))
    metadata_only: bool = True
    provider_execution_started: bool = False
    model_execution_started: bool = False
    network_accessed: bool = False
    credentials_included: bool = False
    refresh_supported: bool = False
    hidden_provider_fallback: bool = False
    hidden_model_fallback: bool = False
    no_hidden_fallback: bool = True
    permission_granting: bool = False
    authority_granting: bool = False


def list_provider_catalog(config: HarnessConfig) -> list[ProviderCatalogEntry]:
    providers: list[ProviderCatalogEntry] = []
    for backend_id, backend in sorted(config.backends.items()):
        enabled = bool(backend.settings.get("enabled", True))
        constraints = list(backend.to_descriptor().constraints)
        if not enabled and "disabled_by_config" not in constraints:
            constraints.append("disabled_by_config")
        providers.append(
            ProviderCatalogEntry(
                provider_id=backend_id,
                backend_id=backend_id,
                kind=backend.kind,
                enabled=enabled,
                credential_status=_credential_status(backend.settings),
                metadata=backend.metadata,
                capabilities=backend.capabilities,
                settings_preview=_settings_preview(backend.settings),
                constraints=constraints,
                policy_boundary=_catalog_policy_boundary("provider_catalog_metadata"),
                safety_notes=[
                    "Provider catalog entries are metadata only and do not trigger login, refresh, network, or fallback.",
                    "Credential-bearing settings are never printed.",
                    "Unavailable or disabled providers must fail visibly at execution time instead of silently falling back.",
                ],
            )
        )
    return providers


def list_model_catalog(
    config: HarnessConfig,
    registry: SpecRegistry | None = None,
    *,
    provider_id: str | None = None,
) -> list[ModelCatalogEntry]:
    registry = registry or builtin_spec_registry()
    entries: list[ModelCatalogEntry] = []
    for backend_id, backend in sorted(config.backends.items()):
        if provider_id is not None and backend_id != provider_id:
            continue
        model = backend.settings.get("model")
        if isinstance(model, str) and model.strip():
            entries.append(
                ModelCatalogEntry(
                    provider_id=backend_id,
                    backend_id=backend_id,
                    model_id=model.strip(),
                    raw_model_ref=f"{backend_id}/{model.strip()}",
                    source="backend_config",
                    capabilities=backend.capabilities,
                    context_limit=backend.capabilities.max_context_tokens,
                    tool_support=backend.capabilities.tool_calling,
                    policy_boundary=_catalog_policy_boundary("model_catalog_metadata"),
                    safety_notes=_model_safety_notes(backend_id),
                )
            )
    for profile_id, profile in sorted(registry.model_profiles.items()):
        backend = config.backends.get(profile.backend)
        if backend is None:
            continue
        if provider_id is not None and profile.backend != provider_id:
            continue
        model = backend.settings.get("model")
        model_id = model.strip() if isinstance(model, str) and model.strip() else profile.id
        entries.append(
            ModelCatalogEntry(
                provider_id=profile.backend,
                backend_id=profile.backend,
                model_id=model_id,
                raw_model_ref=f"{profile.backend}/{model_id}",
                model_profile_id=profile_id,
                source="model_profile",
            capabilities=backend.capabilities,
            context_limit=backend.capabilities.max_context_tokens,
            tool_support=backend.capabilities.tool_calling,
            policy_boundary=_catalog_policy_boundary("model_catalog_metadata"),
            safety_notes=_model_safety_notes(profile.backend)
            + [f"Model profile {profile_id} resolves through backend {profile.backend}."],
        )
        )
    return sorted(entries, key=lambda item: (item.provider_id, item.source, item.model_profile_id or "", item.model_id))


def validate_model_selection(
    config: HarnessConfig,
    raw_model_ref: str | None,
    registry: SpecRegistry | None = None,
) -> ModelSelectionValidation:
    parsed = parse_model_ref(raw_model_ref)
    providers = {provider.provider_id: provider for provider in list_provider_catalog(config)}
    models = list_model_catalog(config, registry)
    raw = raw_model_ref.strip() if isinstance(raw_model_ref, str) else None
    matched = _match_model(models, raw, parsed)
    provider_id = parsed["provider_id"] or (matched.provider_id if matched is not None else None)
    provider = providers.get(provider_id) if provider_id is not None else None
    blocked: list[str] = []
    reasons: list[str] = []
    if not raw:
        blocked.append("model_ref_missing")
        reasons.append("No model ref was supplied; execution must ask for an explicit model instead of falling back.")
    if provider_id is None:
        blocked.append("provider_not_specified")
        reasons.append("Model ref does not specify a provider; Harness will not infer one through fallback.")
    elif provider is None:
        blocked.append("provider_unknown")
        reasons.append(f"Provider is not configured: {provider_id}")
    elif not provider.enabled:
        blocked.append("provider_disabled")
        reasons.append(f"Provider is disabled by configuration: {provider_id}")
    if raw and matched is None:
        blocked.append("model_unknown")
        reasons.append(f"Model ref is not present in the local catalog: {raw}")
    if matched is not None and provider is not None and provider.enabled:
        reasons.append("Model ref matches the local catalog and provider is enabled.")
    executable = bool(matched is not None and provider is not None and provider.enabled and not blocked)
    return ModelSelectionValidation(
        raw_model_ref=raw,
        provider_id=provider_id,
        model_id=parsed["model_id"],
        variant=parsed["variant"],
        known_catalog_entry=matched is not None,
        provider_known=provider is not None,
        provider_enabled=bool(provider.enabled) if provider is not None else False,
        executable=executable,
        matched_model=matched,
        reasons=reasons,
        blocked_reasons=blocked,
    )


def parse_model_ref(raw_model_ref: str | None) -> dict[str, str | None]:
    if not raw_model_ref:
        return {"provider_id": None, "model_id": None, "variant": None}
    raw = raw_model_ref.strip()
    provider_id = None
    model_id = raw
    variant = None
    if "/" in raw:
        provider_id, model_id = raw.split("/", 1)
    if "@" in model_id:
        model_id, variant = model_id.rsplit("@", 1)
    elif ":" in model_id:
        model_id, variant = model_id.rsplit(":", 1)
    return {"provider_id": provider_id or None, "model_id": model_id or None, "variant": variant or None}


def _match_model(
    models: list[ModelCatalogEntry],
    raw_model_ref: str | None,
    parsed: dict[str, str | None],
) -> ModelCatalogEntry | None:
    if not raw_model_ref:
        return None
    for model in models:
        if model.raw_model_ref == raw_model_ref:
            return model
    provider_id = parsed["provider_id"]
    model_id = parsed["model_id"]
    if provider_id is None or model_id is None:
        return None
    for model in models:
        if model.provider_id == provider_id and model.model_id == model_id:
            return model
    return None


def _credential_status(settings: dict[str, Any]) -> ProviderCredentialStatus:
    api_key_env = settings.get("api_key_env")
    if isinstance(api_key_env, str) and api_key_env.strip():
        return ProviderCredentialStatus.CONFIGURED if os.environ.get(api_key_env) else ProviderCredentialStatus.MISSING
    if "api_key" in settings or "auth_mode" in settings:
        return ProviderCredentialStatus.CONFIGURED
    return ProviderCredentialStatus.NOT_REQUIRED


def _settings_preview(settings: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "base_url",
        "model",
        "model_reasoning_effort",
        "temperature",
        "max_tokens",
        "timeout_seconds",
        "enabled",
        "use_subscription_credits",
    }
    preview: dict[str, Any] = {}
    for key in sorted(allowed):
        if key in settings:
            preview[key] = settings[key]
    if "api_key_env" in settings:
        preview["credential_env"] = settings["api_key_env"]
    return preview


def _model_safety_notes(provider_id: str) -> list[str]:
    return [
        f"Model selection for {provider_id} is explicit metadata; unknown refs may persist but must not trigger fallback.",
        "Execution may only use adapters that support the requested model override.",
    ]


def _catalog_policy_boundary(kind: str) -> dict[str, Any]:
    return {
        "kind": kind,
        "source": "provider_model_catalog",
        "metadata_only": True,
    }


def catalog_projection_evidence(kind: str) -> dict[str, Any]:
    return {
        "policy_boundary": _catalog_policy_boundary(kind),
        "metadata_only": True,
        "provider_execution_started": False,
        "model_execution_started": False,
        "network_accessed": False,
        "credentials_included": False,
        "credential_write_supported": False,
        "credential_written": False,
        "refresh_supported": False,
        "hidden_provider_fallback": False,
        "hidden_model_fallback": False,
        "no_hidden_fallback": True,
        "permission_granting": False,
        "authority_granting": False,
    }
