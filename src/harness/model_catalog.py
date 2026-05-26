from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from harness.config import HarnessConfig
from harness.model_registry import (
    ModelDescriptor,
    ModelResolutionError,
    ProviderCredentialStatus,
    ProviderDescriptor,
    ResolvedModelSelection,
    build_model_descriptors,
    build_provider_descriptors,
    parse_model_ref as parse_descriptor_model_ref,
    resolve_model_selection,
)
from harness.models import BackendCapabilities, BackendKind, BackendMetadata
from harness.provider_auth import resolve_provider_credential
from harness.registry import SpecRegistry


class ProviderCatalogEntry(BaseModel):
    schema_version: str = "harness.provider_catalog_entry/v1"
    provider_id: str
    display_name: str | None = None
    backend_id: str
    kind: BackendKind
    enabled: bool
    connected: bool = False
    credential_status: ProviderCredentialStatus
    credential_source: str = "unknown"
    active_account_id: str | None = None
    metadata: BackendMetadata
    capabilities: BackendCapabilities
    source: str = "backend_config"
    settings_preview: dict[str, Any] = Field(default_factory=dict)
    constraints: list[str] = Field(default_factory=list)
    auth_methods: list[str] = Field(default_factory=list)
    model_count: int = 0
    available_model_count: int = 0
    default_model_candidate: str | None = None
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
    canonical_model_ref: str | None = None
    alias_of: str | None = None
    protocol: str | None = None
    status: str = "active"
    variant: str | None = None
    model_profile_id: str | None = None
    source: str
    known_catalog_model: bool = True
    available_model: bool = False
    executable_model: bool = False
    selected_model: bool = False
    availability: str = "blocked"
    blocked_reasons: list[str] = Field(default_factory=list)
    variant_list: list[str] = Field(default_factory=list)
    provider_enabled: bool = False
    provider_connected: bool = False
    provider_credential_status: str = "unknown"
    capabilities: BackendCapabilities
    context_limit: int | None = None
    max_output_tokens: int | None = None
    cost: dict[str, Any] | None = None
    modalities: list[str] = Field(default_factory=lambda: ["text"])
    reasoning_support: str = "unknown"
    tool_support: bool = False
    release_date: str | None = None
    family: str | None = None
    endpoint: str | None = None
    discovered_at: str | None = None
    last_refresh_at: str | None = None
    cache_refreshed_at: str | None = None
    cache_ttl_seconds: int | None = None
    cache_expires_at: str | None = None
    cache_status: str | None = None
    discovery_metadata: dict[str, Any] = Field(default_factory=dict)
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
    canonical_model_ref: str | None = None
    protocol: str | None = None
    alias_used: str | None = None
    known_catalog_entry: bool = False
    provider_known: bool = False
    provider_enabled: bool = False
    executable: bool = False
    matched_model: ModelCatalogEntry | None = None
    resolved_model_selection: ResolvedModelSelection | None = None
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


def list_provider_catalog(
    config: HarnessConfig,
    *,
    provider_accounts: list[dict[str, Any]] | None = None,
) -> list[ProviderCatalogEntry]:
    from harness.active_provider_registry import ActiveProviderRegistry

    active = ActiveProviderRegistry(config, provider_accounts=provider_accounts)
    active.list_all_models()
    states = {state.provider_id: state for state in active.list_all_providers()}
    return [
        _provider_catalog_entry_from_descriptor(config, provider, active_state=states.get(provider.provider_id))
        for provider in build_provider_descriptors(config, provider_accounts=provider_accounts)
    ]


def list_model_catalog(
    config: HarnessConfig,
    registry: SpecRegistry | None = None,
    *,
    provider_id: str | None = None,
    provider_accounts: list[dict[str, Any]] | None = None,
    model_overlays: list[ModelCatalogEntry] | None = None,
) -> list[ModelCatalogEntry]:
    from harness.active_provider_registry import ActiveProviderRegistry

    active = ActiveProviderRegistry(
        config,
        provider_accounts=provider_accounts,
        registry=registry,
        model_overlays=model_overlays or [],
    )
    states = {state.raw_model_ref: state for state in active.list_all_models()}
    entries = [
        _model_catalog_entry_from_descriptor(config, model, active_state=states.get(model.raw_model_ref))
        for model in build_model_descriptors(config, registry)
        if provider_id is None or model.provider_id == provider_id
    ]
    for overlay in model_overlays or []:
        if provider_id is not None and overlay.provider_id != provider_id:
            continue
        if overlay.raw_model_ref in {entry.raw_model_ref for entry in entries}:
            continue
        state = states.get(overlay.raw_model_ref)
        entries.append(
            overlay.model_copy(
                update=_model_state_update(state),
                deep=True,
            )
        )
    return sorted(
        entries,
        key=lambda item: (
            item.provider_id,
            item.source,
            item.model_profile_id or "",
            item.model_id,
            item.raw_model_ref,
        ),
    )


def validate_model_selection(
    config: HarnessConfig,
    raw_model_ref: str | None,
    registry: SpecRegistry | None = None,
    *,
    request_options: dict[str, Any] | None = None,
    model_overlays: list[ModelCatalogEntry] | None = None,
    provider_accounts: list[dict[str, Any]] | None = None,
) -> ModelSelectionValidation:
    from harness.active_provider_registry import ActiveProviderRegistry

    parsed = parse_model_ref(raw_model_ref)
    active = ActiveProviderRegistry(
        config,
        provider_accounts=provider_accounts,
        registry=registry,
        model_overlays=model_overlays or [],
    )
    active_providers = {provider.provider_id: provider for provider in active.list_all_providers()}
    providers = {provider.provider_id: provider for provider in list_provider_catalog(config, provider_accounts=provider_accounts)}
    models = _merge_model_entries(
        list_model_catalog(config, registry, provider_accounts=provider_accounts),
        model_overlays or [],
    )
    raw = raw_model_ref.strip() if isinstance(raw_model_ref, str) else None
    matched = _match_model(models, raw, parsed)
    resolved: ResolvedModelSelection | None = None
    resolution_error: ModelResolutionError | None = None
    if raw:
        try:
            resolved = resolve_model_selection(config, raw, registry, request_options=request_options)
        except ModelResolutionError as exc:
            resolution_error = exc
    if resolved is None and matched is not None and matched.source == "discovered":
        resolution_error = None
    provider_id = (
        resolved.provider_id
        if resolved is not None
        else parsed["provider_id"] or (matched.provider_id if matched is not None else None)
    )
    provider = providers.get(provider_id) if provider_id is not None else None
    active_provider = active_providers.get(provider_id) if provider_id is not None else None
    active_model = active.get_model(provider_id, matched.model_id) if provider_id is not None and matched is not None else None
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
    elif active_provider is not None and not active_provider.connected:
        for reason in active_provider.blocked_reasons:
            if reason not in blocked:
                blocked.append(reason)
        reasons.append(f"Provider is not connected: {provider_id}")
    if raw and matched is None:
        blocked.append("model_unknown")
        reasons.append(f"Model ref is not present in the local catalog: {raw}")
    if resolution_error is not None:
        for reason in resolution_error.blocked_reasons:
            if reason not in blocked:
                blocked.append(reason)
        reasons.append(f"Model descriptor resolution failed: {', '.join(resolution_error.blocked_reasons)}")
    if active_model is not None:
        for reason in active_model.blocked_reasons:
            if reason not in blocked:
                blocked.append(reason)
    if matched is not None and provider is not None and provider.enabled and not blocked:
        reasons.append("Model ref matches the local catalog and provider is enabled.")
    executable = bool(matched is not None and provider is not None and provider.enabled and not blocked)
    return ModelSelectionValidation(
        raw_model_ref=raw,
        provider_id=provider_id,
        model_id=resolved.model_id if resolved is not None else parsed["model_id"],
        variant=resolved.variant if resolved is not None else parsed["variant"],
        canonical_model_ref=resolved.canonical_model_ref if resolved is not None else None,
        protocol=resolved.model.protocol
        if resolved is not None
        else matched.protocol
        if matched is not None and matched.source == "discovered"
        else None,
        alias_used=resolved.alias_used if resolved is not None else None,
        known_catalog_entry=matched is not None,
        provider_known=provider is not None,
        provider_enabled=bool(provider.enabled) if provider is not None else False,
        executable=executable,
        matched_model=matched,
        resolved_model_selection=resolved,
        reasons=reasons,
        blocked_reasons=blocked,
    )


def parse_model_ref(raw_model_ref: str | None) -> dict[str, str | None]:
    parsed = parse_descriptor_model_ref(raw_model_ref)
    return {"provider_id": parsed.provider_id, "model_id": parsed.model_id, "variant": parsed.variant}


def build_model_provider_suggestions(
    config: HarnessConfig,
    raw_model_ref: str | None,
    *,
    provider_accounts: list[dict[str, Any]] | None = None,
    model_overlays: list[ModelCatalogEntry] | None = None,
    limit: int = 8,
) -> dict[str, Any]:
    from harness.active_provider_registry import ActiveProviderRegistry

    parsed = parse_model_ref(raw_model_ref)
    registry = ActiveProviderRegistry(
        config,
        provider_accounts=provider_accounts,
        model_overlays=model_overlays or [],
    )
    model_suggestions = _unique_suggestions(
        _model_suggestion_queries(raw_model_ref, parsed),
        suggest=lambda query: registry.suggest_models(query, limit=limit),
        key="raw_model_ref",
        limit=limit,
    )
    provider_suggestions = _unique_suggestions(
        _provider_suggestion_queries(raw_model_ref, parsed),
        suggest=lambda query: registry.suggest_providers(query, limit=limit),
        key="provider_id",
        limit=limit,
    )
    if not provider_suggestions and parsed["provider_id"]:
        provider_suggestions = registry.suggest_providers(None, limit=min(limit, 6))
    return {
        "schema_version": "harness.model_provider_suggestions/v1",
        "ok": True,
        "raw_model_ref": str(raw_model_ref or "").strip() or None,
        "model_suggestions": model_suggestions,
        "provider_suggestions": provider_suggestions,
        "suggestions": model_suggestions,
        "suggestion_only": True,
        "selected_model": False,
        "selected_provider": False,
        "provider_execution_started": False,
        "model_execution_started": False,
        "network_accessed": False,
        "credentials_included": False,
        "hidden_provider_fallback": False,
        "hidden_model_fallback": False,
        "no_hidden_fallback": True,
        "permission_granting": False,
        "authority_granting": False,
    }


def _model_suggestion_queries(raw_model_ref: str | None, parsed: dict[str, str | None]) -> list[str | None]:
    queries: list[str | None] = []
    raw = str(raw_model_ref or "").strip()
    provider_id = parsed["provider_id"]
    model_id = parsed["model_id"]
    if raw:
        queries.append(raw)
    if provider_id and model_id:
        queries.append(f"{provider_id} {model_id}")
    if model_id:
        queries.append(model_id)
        queries.append(model_id.replace("-", " ").replace("_", " "))
    if provider_id:
        queries.append(provider_id)
    if not queries:
        queries.append(None)
    return queries


def _provider_suggestion_queries(raw_model_ref: str | None, parsed: dict[str, str | None]) -> list[str | None]:
    queries: list[str | None] = []
    provider_id = parsed["provider_id"]
    raw = str(raw_model_ref or "").strip()
    if provider_id:
        queries.append(provider_id)
        queries.append(provider_id.replace("-", " ").replace("_", " "))
    elif raw and "/" not in raw:
        queries.append(raw)
    if not queries:
        queries.append(None)
    return queries


def _unique_suggestions(
    queries: list[str | None],
    *,
    suggest: Any,
    key: str,
    limit: int,
) -> list[dict[str, Any]]:
    seen: set[str] = set()
    suggestions: list[dict[str, Any]] = []
    for query in queries:
        for suggestion in suggest(query):
            identifier = str(suggestion.get(key) or "")
            if not identifier or identifier in seen:
                continue
            seen.add(identifier)
            suggestions.append(suggestion)
            if len(suggestions) >= limit:
                return suggestions
    return suggestions


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


def _merge_model_entries(base: list[ModelCatalogEntry], overlays: list[ModelCatalogEntry]) -> list[ModelCatalogEntry]:
    entries = list(base)
    existing_refs = {entry.raw_model_ref for entry in entries}
    for overlay in overlays:
        if overlay.raw_model_ref in existing_refs:
            continue
        entries.append(overlay)
        existing_refs.add(overlay.raw_model_ref)
    return entries


def _provider_catalog_entry_from_descriptor(
    config: HarnessConfig,
    provider: ProviderDescriptor,
    *,
    active_state: Any | None = None,
) -> ProviderCatalogEntry:
    backend = config.backends[provider.backend_id or provider.provider_id]
    if active_state is not None:
        credential_status = active_state.credential_status
        credential_source = active_state.credential_source
        connected = active_state.connected
        active_account_id = active_state.account_id
        model_count = active_state.model_count
        available_model_count = active_state.available_model_count
        default_model_candidate = active_state.default_model_candidate
    else:
        resolved_credential = resolve_provider_credential(config, provider, allow_secret_material=False)
        credential_status = resolved_credential.status
        credential_source = resolved_credential.source
        connected = credential_status in {"configured", "not_required"}
        active_account_id = resolved_credential.account_id
        model_count = 0
        available_model_count = 0
        default_model_candidate = None
    return ProviderCatalogEntry(
        provider_id=provider.provider_id,
        display_name=provider.display_name,
        backend_id=provider.backend_id or provider.provider_id,
        kind=backend.kind,
        enabled=provider.enabled,
        connected=connected,
        credential_status=credential_status,
        credential_source=credential_source,
        active_account_id=active_account_id,
        metadata=provider.metadata,
        capabilities=provider.capabilities,
        source=provider.source,
        settings_preview=_settings_preview(backend.settings),
        constraints=list(provider.constraints),
        auth_methods=_provider_auth_methods(provider.credential),
        model_count=model_count,
        available_model_count=available_model_count,
        default_model_candidate=default_model_candidate,
        policy_boundary=_catalog_policy_boundary("provider_catalog_metadata"),
        safety_notes=[
            "Provider catalog entries are metadata only and do not trigger login, refresh, network, or fallback.",
            "Credential-bearing settings are never printed.",
            "Unavailable or disabled providers must fail visibly at execution time instead of silently falling back.",
        ],
    )


def _model_catalog_entry_from_descriptor(
    config: HarnessConfig,
    model: ModelDescriptor,
    *,
    active_state: Any | None = None,
) -> ModelCatalogEntry:
    backend = config.backends[model.backend_id or model.provider_id]
    safety_notes = _model_safety_notes(model.provider_id)
    if model.model_profile_id is not None:
        safety_notes = safety_notes + [
            f"Model profile {model.model_profile_id} resolves through backend {model.backend_id or model.provider_id}."
        ]
    if model.alias_of is not None:
        safety_notes = safety_notes + [
            f"Model alias {model.raw_model_ref} resolves through canonical ref {model.alias_of}."
        ]
    return ModelCatalogEntry(
        provider_id=model.provider_id,
        backend_id=model.backend_id or model.provider_id,
        model_id=model.model_id,
        raw_model_ref=model.raw_model_ref,
        canonical_model_ref=model.canonical_model_ref,
        alias_of=model.alias_of,
        protocol=model.protocol,
        status=model.status,
        variant=None,
        model_profile_id=model.model_profile_id,
        source=model.source,
        **_model_state_update(active_state),
        capabilities=backend.capabilities,
        context_limit=model.context_limit,
        max_output_tokens=model.max_output_tokens,
        cost=model.cost,
        modalities=list(model.input_modalities),
        reasoning_support=model.reasoning_support,
        tool_support=model.tool_support,
        release_date=model.release_date,
        family=model.family,
        endpoint=model.endpoint,
        policy_boundary=_catalog_policy_boundary("model_catalog_metadata"),
        safety_notes=safety_notes,
    )


def _model_state_update(active_state: Any | None) -> dict[str, Any]:
    if active_state is None:
        return {}
    provider_state = active_state.provider_state or {}
    return {
        "known_catalog_model": bool(active_state.known_catalog_model),
        "available_model": bool(active_state.available_model),
        "executable_model": bool(active_state.executable_model),
        "selected_model": bool(active_state.selected_model),
        "availability": active_state.availability,
        "blocked_reasons": list(active_state.blocked_reasons),
        "variant_list": list(active_state.variant_list),
        "provider_enabled": bool(provider_state.get("enabled")),
        "provider_connected": bool(provider_state.get("connected")),
        "provider_credential_status": str(provider_state.get("credential_status") or "unknown"),
    }


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


def _provider_auth_methods(credential: Any | None) -> list[str]:
    if credential is None:
        return ["none"]
    kind = str(getattr(credential, "kind", None) or "unknown")
    env_var = getattr(credential, "env_var", None)
    if isinstance(env_var, str) and env_var.strip():
        return [f"{kind}:{env_var.strip()}"]
    account_id = getattr(credential, "account_id", None)
    if isinstance(account_id, str) and account_id.strip():
        return [f"{kind}:account"]
    return [kind]


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
