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
    safety_notes: list[str] = Field(default_factory=list)


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
                safety_notes=_model_safety_notes(profile.backend)
                + [f"Model profile {profile_id} resolves through backend {profile.backend}."],
            )
        )
    return sorted(entries, key=lambda item: (item.provider_id, item.source, item.model_profile_id or "", item.model_id))


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
