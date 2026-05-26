from __future__ import annotations

import configparser
from datetime import datetime, timezone
import hmac
import os
import urllib.parse
import hashlib
import json
from typing import Any, Protocol

from pydantic import BaseModel, Field

from harness.backends.local_openai import OpenAICompatibleHttpClient, UrllibOpenAICompatibleHttpClient, validate_local_base_url
from harness.config import HarnessConfig
from harness.memory.sqlite_store import SQLiteStore, now_iso
from harness.model_catalog import ModelCatalogEntry, list_model_catalog, list_provider_catalog
from harness.model_registry import ProviderDescriptor, build_provider_descriptors, load_generated_static_model_catalog
from harness.models import BackendKind
from harness.provider_auth import ProviderCredentialResolutionError, ResolvedProviderCredential, resolve_provider_credential


DISCOVERY_CACHE_TTL_SECONDS = 24 * 60 * 60


class ModelDiscoveryError(ValueError):
    def __init__(self, provider_id: str, message: str, blocked_reasons: list[str]) -> None:
        self.provider_id = provider_id
        self.blocked_reasons = blocked_reasons
        super().__init__(message)


class ModelDiscoveryResult(BaseModel):
    schema_version: str = "harness.model_discovery_result/v1"
    ok: bool
    provider_id: str
    source: str = "discovered"
    discovered_at: str
    endpoint: str | None = None
    raw_provider_response_sha256: str | None = None
    approval_evidence: dict[str, Any] = Field(default_factory=dict)
    model_count: int = 0
    models: list[ModelCatalogEntry] = Field(default_factory=list)
    cache: dict[str, Any] | None = None
    network_accessed: bool = False
    credentials_included: bool = False
    credential_written: bool = False
    redaction_state: str = "not_required"
    provider_execution_started: bool = False
    model_execution_started: bool = False
    hidden_provider_fallback: bool = False
    hidden_model_fallback: bool = False
    no_hidden_fallback: bool = True
    permission_granting: bool = False
    authority_granting: bool = False


class ProviderDiscoveryPolicy(BaseModel):
    schema_version: str = "harness.provider_discovery_policy/v1"
    approve_hosted: bool = False
    metadata_only: bool = True
    with_credentials: bool = False
    timeout_seconds: float | None = None


class ProviderDiscoveryAdapter(Protocol):
    provider_id: str

    def supports(self, provider: ProviderDescriptor) -> bool:
        ...

    def discover(
        self,
        provider: ProviderDescriptor,
        credential: ResolvedProviderCredential | None,
        policy: ProviderDiscoveryPolicy,
    ) -> ModelDiscoveryResult:
        ...


class ProviderDiscoveryAdapterRegistry:
    def __init__(self, adapters: list[ProviderDiscoveryAdapter] | None = None) -> None:
        self._adapters: list[ProviderDiscoveryAdapter] = []
        for adapter in adapters or []:
            self.register(adapter)

    def register(self, adapter: ProviderDiscoveryAdapter) -> None:
        self._adapters.append(adapter)

    def list_adapters(self) -> list[ProviderDiscoveryAdapter]:
        return list(self._adapters)

    def supported_adapters(self, provider: ProviderDescriptor) -> list[ProviderDiscoveryAdapter]:
        return [adapter for adapter in self._adapters if adapter.supports(provider)]

    def get_for_provider(self, provider: ProviderDescriptor) -> ProviderDiscoveryAdapter:
        adapters = self.supported_adapters(provider)
        if not adapters:
            raise ModelDiscoveryError(provider.provider_id, "Model discovery is not implemented for this provider.", ["discovery_unsupported"])
        return adapters[0]


class OpenAICompatibleDiscoveryAdapter:
    provider_id = "openai_compatible_models"

    def __init__(self, *, http_client: OpenAICompatibleHttpClient | None = None) -> None:
        self.http_client = http_client

    def supports(self, provider: ProviderDescriptor) -> bool:
        protocol = str(provider.protocol_defaults.get("protocol") or "")
        if protocol in {"openai_chat", "openai_responses", "openai_codex_responses"}:
            return bool(provider.endpoint)
        return bool(provider.endpoint) and provider.provider_id in {"local_openai_compatible", "paid_openai_compatible"}

    def discover(
        self,
        provider: ProviderDescriptor,
        credential: ResolvedProviderCredential | None,
        policy: ProviderDiscoveryPolicy,
    ) -> ModelDiscoveryResult:
        endpoint = provider.endpoint
        if endpoint is None:
            raise ModelDiscoveryError(provider.provider_id, f"Provider has no base_url: {provider.provider_id}", ["endpoint_missing"])
        is_local = provider.metadata.data_boundary.value == "local_only"
        if is_local:
            validate_local_base_url(endpoint, list(provider.protocol_defaults.get("approved_lan_endpoints", [])))
        elif not policy.approve_hosted:
            raise ModelDiscoveryError(
                provider.provider_id,
                "Hosted model discovery requires explicit hosted-provider/network approval.",
                ["hosted_discovery_approval_required"],
            )
        if policy.with_credentials and (credential is None or not credential.credentials_included):
            raise ModelDiscoveryError(provider.provider_id, "Credential-backed discovery requested without runtime credentials.", ["credential_missing"])
        discovered_at = now_iso()
        client = self.http_client or UrllibOpenAICompatibleHttpClient()
        response = client.get_json(
            _join_url(endpoint, "/models"),
            headers=_discovery_headers(credential),
            timeout=float(policy.timeout_seconds or provider.protocol_defaults.get("timeout_seconds", 30)),
        )
        response_hash = _response_hash(response)
        approval_evidence = {
            "hosted_refresh_approved": bool(policy.approve_hosted and not is_local),
            "local_endpoint_validated": bool(is_local),
            "permission_granting": False,
        }
        discovered_models = _models_from_response(response)
        models = [
            _catalog_entry_from_openai_model(
                provider,
                model,
                endpoint=endpoint,
                discovered_at=discovered_at,
                response_hash=response_hash,
                approval_evidence=approval_evidence,
                credentials_included=bool(policy.with_credentials and credential is not None and credential.credentials_included),
            )
            for model in discovered_models
        ]
        return ModelDiscoveryResult(
            ok=True,
            provider_id=provider.provider_id,
            discovered_at=discovered_at,
            endpoint=endpoint,
            raw_provider_response_sha256=response_hash,
            approval_evidence=approval_evidence,
            model_count=len(models),
            models=models,
            network_accessed=True,
            credentials_included=bool(policy.with_credentials and credential is not None and credential.credentials_included),
            credential_written=False,
        )


class GoogleGenerativeDiscoveryAdapter:
    provider_id = "google_generative_models"

    def __init__(self, *, http_client: OpenAICompatibleHttpClient | None = None) -> None:
        self.http_client = http_client

    def supports(self, provider: ProviderDescriptor) -> bool:
        return bool(provider.endpoint) and provider.provider_id == "google"

    def discover(
        self,
        provider: ProviderDescriptor,
        credential: ResolvedProviderCredential | None,
        policy: ProviderDiscoveryPolicy,
    ) -> ModelDiscoveryResult:
        endpoint = provider.endpoint
        if endpoint is None:
            raise ModelDiscoveryError(provider.provider_id, f"Provider has no base_url: {provider.provider_id}", ["endpoint_missing"])
        if not policy.approve_hosted:
            raise ModelDiscoveryError(
                provider.provider_id,
                "Hosted model discovery requires explicit hosted-provider/network approval.",
                ["hosted_discovery_approval_required"],
            )
        if policy.with_credentials and (credential is None or not credential.credentials_included):
            raise ModelDiscoveryError(provider.provider_id, "Credential-backed discovery requested without runtime credentials.", ["credential_missing"])

        discovered_at = now_iso()
        client = self.http_client or UrllibOpenAICompatibleHttpClient()
        credentials_included = bool(policy.with_credentials and credential is not None and credential.credentials_included)
        response = client.get_json(
            _join_url(endpoint, "/models"),
            headers=_google_discovery_headers(credential),
            timeout=float(policy.timeout_seconds or provider.protocol_defaults.get("timeout_seconds", 30)),
        )
        response_hash = _response_hash(response)
        approval_evidence = {
            "hosted_refresh_approved": True,
            "local_endpoint_validated": False,
            "permission_granting": False,
        }
        discovered_models = _google_models_from_response(response)
        models = [
            _catalog_entry_from_google_model(
                provider,
                model,
                endpoint=endpoint,
                discovered_at=discovered_at,
                response_hash=response_hash,
                approval_evidence=approval_evidence,
                credentials_included=credentials_included,
            )
            for model in discovered_models
        ]
        return ModelDiscoveryResult(
            ok=True,
            provider_id=provider.provider_id,
            discovered_at=discovered_at,
            endpoint=endpoint,
            raw_provider_response_sha256=response_hash,
            approval_evidence=approval_evidence,
            model_count=len(models),
            models=models,
            network_accessed=True,
            credentials_included=credentials_included,
            credential_written=False,
        )


class BedrockFoundationModelsDiscoveryAdapter:
    provider_id = "bedrock_foundation_models"

    def __init__(self, *, http_client: OpenAICompatibleHttpClient | None = None) -> None:
        self.http_client = http_client

    def supports(self, provider: ProviderDescriptor) -> bool:
        return provider.provider_id == "bedrock" or provider.protocol_defaults.get("protocol") == "bedrock_converse"

    def discover(
        self,
        provider: ProviderDescriptor,
        credential: ResolvedProviderCredential | None,
        policy: ProviderDiscoveryPolicy,
    ) -> ModelDiscoveryResult:
        if not policy.approve_hosted:
            raise ModelDiscoveryError(
                provider.provider_id,
                "Hosted model discovery requires explicit hosted-provider/network approval.",
                ["hosted_discovery_approval_required"],
            )
        if not policy.with_credentials or credential is None or credential.status != "configured":
            raise ModelDiscoveryError(
                provider.provider_id,
                "Bedrock foundation model discovery requires explicit credential-backed refresh.",
                ["credential_missing"],
            )

        region = _bedrock_discovery_region(provider)
        endpoint = _bedrock_foundation_models_endpoint(region)
        auth = _bedrock_discovery_auth_material(provider, credential)
        discovered_at = now_iso()
        client = self.http_client or UrllibOpenAICompatibleHttpClient()
        headers = _bedrock_discovery_headers(provider, credential, endpoint, region=region, auth=auth)
        response = client.get_json(
            endpoint,
            headers=headers,
            timeout=float(policy.timeout_seconds or provider.protocol_defaults.get("timeout_seconds", 30)),
        )
        response_hash = _response_hash(response)
        approval_evidence = {
            "hosted_refresh_approved": True,
            "local_endpoint_validated": False,
            "permission_granting": False,
        }
        discovered_models = _bedrock_models_from_response(response)
        models = [
            _catalog_entry_from_bedrock_model(
                provider,
                model,
                endpoint=provider.endpoint or endpoint,
                discovery_endpoint=endpoint,
                region=region,
                discovered_at=discovered_at,
                response_hash=response_hash,
                approval_evidence=approval_evidence,
            )
            for model in discovered_models
        ]
        return ModelDiscoveryResult(
            ok=True,
            provider_id=provider.provider_id,
            discovered_at=discovered_at,
            endpoint=endpoint,
            raw_provider_response_sha256=response_hash,
            approval_evidence=approval_evidence,
            model_count=len(models),
            models=models,
            network_accessed=True,
            credentials_included=True,
            credential_written=False,
        )


class AnthropicStaticCatalogDiscoveryAdapter:
    provider_id = "anthropic_static_catalog"

    def supports(self, provider: ProviderDescriptor) -> bool:
        return provider.provider_id == "anthropic" and bool(_static_catalog_models_for_provider(provider.provider_id))

    def discover(
        self,
        provider: ProviderDescriptor,
        credential: ResolvedProviderCredential | None,
        policy: ProviderDiscoveryPolicy,
    ) -> ModelDiscoveryResult:
        discovered_at = now_iso()
        static_models = _static_catalog_models_for_provider(provider.provider_id)
        if not static_models:
            raise ModelDiscoveryError(provider.provider_id, "No static model catalog entries exist for this provider.", ["discovery_unsupported"])
        response_hash = _response_hash({"models": static_models})
        approval_evidence = {
            "static_catalog": True,
            "hosted_refresh_approved": False,
            "local_endpoint_validated": False,
            "permission_granting": False,
        }
        models = [
            _catalog_entry_from_static_model(
                provider,
                raw_model_ref,
                metadata,
                discovered_at=discovered_at,
                response_hash=response_hash,
                approval_evidence=approval_evidence,
            )
            for raw_model_ref, metadata in sorted(static_models.items())
        ]
        return ModelDiscoveryResult(
            ok=True,
            provider_id=provider.provider_id,
            source="static_catalog",
            discovered_at=discovered_at,
            endpoint=None,
            raw_provider_response_sha256=response_hash,
            approval_evidence=approval_evidence,
            model_count=len(models),
            models=models,
            network_accessed=False,
            credentials_included=False,
            credential_written=False,
        )


def build_default_provider_discovery_registry(
    *,
    http_client: OpenAICompatibleHttpClient | None = None,
) -> ProviderDiscoveryAdapterRegistry:
    return ProviderDiscoveryAdapterRegistry(
        [
            OpenAICompatibleDiscoveryAdapter(http_client=http_client),
            GoogleGenerativeDiscoveryAdapter(http_client=http_client),
            BedrockFoundationModelsDiscoveryAdapter(http_client=http_client),
            AnthropicStaticCatalogDiscoveryAdapter(),
        ]
    )


def refresh_model_discovery(
    config: HarnessConfig,
    provider_id: str,
    *,
    store: SQLiteStore | None = None,
    http_client: OpenAICompatibleHttpClient | None = None,
    approve_hosted: bool = False,
    metadata_only: bool = True,
    with_credentials: bool = False,
) -> ModelDiscoveryResult:
    provider_id = _canonical_discovery_provider_id(config, provider_id)
    backend = config.backends.get(provider_id)
    if backend is None:
        raise ModelDiscoveryError(provider_id, f"Provider not found: {provider_id}", ["provider_unknown"])
    if backend.kind != BackendKind.NATIVE_MODEL:
        raise ModelDiscoveryError(provider_id, "Model discovery is only implemented for native model providers.", ["discovery_unsupported"])
    if not metadata_only:
        raise ModelDiscoveryError(provider_id, "Model discovery refresh is metadata-only and cannot start provider execution.", ["metadata_only_required"])
    provider = _provider_descriptor(config, provider_id)
    registry = build_default_provider_discovery_registry(http_client=http_client)
    adapter = registry.get_for_provider(provider)
    credential = None
    if with_credentials and _adapter_uses_credentials(adapter):
        try:
            credential = resolve_provider_credential(config, provider, store, allow_secret_material=True)
        except ProviderCredentialResolutionError as exc:
            raise ModelDiscoveryError(provider_id, exc.to_provider_error_message(), exc.blocked_reasons) from exc
    result = adapter.discover(
        provider,
        credential,
        ProviderDiscoveryPolicy(
            approve_hosted=approve_hosted,
            metadata_only=metadata_only,
            with_credentials=with_credentials,
            timeout_seconds=float(backend.settings.get("timeout_seconds", 30)),
        ),
    )
    cache = None
    if store is not None:
        cache = store.replace_discovered_model_catalog_cache(
            provider_id,
            result.models,
            discovery_metadata={
                "schema_version": "harness.model_discovery_metadata/v1",
                "discovered_at": result.discovered_at,
                "endpoint": result.endpoint,
                "network_accessed": result.network_accessed,
                "credentials_included": result.credentials_included,
                "approval_evidence": result.approval_evidence,
                "raw_provider_response_sha256": result.raw_provider_response_sha256,
                "model_ids": [model.model_id for model in result.models],
                "cache_ttl_seconds": DISCOVERY_CACHE_TTL_SECONDS,
            },
        )
    return result.model_copy(update={"cache": cache}, deep=True)


def _adapter_uses_credentials(adapter: ProviderDiscoveryAdapter) -> bool:
    return adapter.provider_id != "anthropic_static_catalog"


def list_cached_discovered_models(
    config: HarnessConfig,
    store: SQLiteStore,
    *,
    provider_id: str | None = None,
) -> list[ModelCatalogEntry]:
    known_providers = set(config.backends)
    entries: list[ModelCatalogEntry] = []
    for row in store.list_provider_model_catalog_cache("model"):
        payload = _discovered_cache_payload_with_status(row)
        if payload.get("source") != "discovered":
            continue
        if payload.get("provider_id") not in known_providers:
            continue
        if provider_id is not None and payload.get("provider_id") != provider_id:
            continue
        entries.append(ModelCatalogEntry.model_validate(payload))
    return sorted(entries, key=lambda item: (item.provider_id, item.model_id, item.raw_model_ref))


def _discovered_cache_payload_with_status(row: dict[str, Any]) -> dict[str, Any]:
    payload = dict(row["payload"])
    metadata = dict(payload.get("discovery_metadata") if isinstance(payload.get("discovery_metadata"), dict) else {})
    refreshed_at = str(payload.get("cache_refreshed_at") or metadata.get("cache_refreshed_at") or row.get("refreshed_at") or "") or None
    expires_at = str(payload.get("cache_expires_at") or metadata.get("cache_expires_at") or "") or None
    ttl_seconds = _optional_int(payload.get("cache_ttl_seconds")) or _optional_int(metadata.get("cache_ttl_seconds"))
    cache_status = _cache_status(expires_at)
    cache_update = {
        "last_refresh_at": refreshed_at,
        "cache_refreshed_at": refreshed_at,
        "cache_ttl_seconds": ttl_seconds,
        "cache_expires_at": expires_at,
        "cache_status": cache_status,
    }
    payload.update(cache_update)
    metadata.update(cache_update)
    payload["discovery_metadata"] = metadata
    return payload


def _cache_status(expires_at: str | None) -> str:
    if not expires_at:
        return "unknown"
    try:
        parsed = datetime.fromisoformat(expires_at)
    except ValueError:
        return "unknown"
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return "stale" if parsed <= datetime.now(timezone.utc) else "fresh"


def _model_ids_from_response(response: dict[str, Any]) -> list[str]:
    data = response.get("data")
    if not isinstance(data, list):
        raise ModelDiscoveryError("unknown", "Provider /models response did not include a data list.", ["invalid_models_response"])
    model_ids: list[str] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        model_id = item.get("id")
        if isinstance(model_id, str) and model_id.strip():
            model_ids.append(model_id.strip())
    return sorted(set(model_ids))


def _models_from_response(response: dict[str, Any]) -> list[dict[str, Any]]:
    data = response.get("data")
    if not isinstance(data, list):
        raise ModelDiscoveryError("unknown", "Provider /models response did not include a data list.", ["invalid_models_response"])
    models: dict[str, dict[str, Any]] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        model_id = item.get("id")
        if not isinstance(model_id, str) or not model_id.strip():
            continue
        architecture = item.get("architecture") if isinstance(item.get("architecture"), dict) else {}
        top_provider = item.get("top_provider") if isinstance(item.get("top_provider"), dict) else {}
        models[model_id.strip()] = {
            "id": model_id.strip(),
            "owned_by": item.get("owned_by") if isinstance(item.get("owned_by"), str) else None,
            "created": item.get("created") if isinstance(item.get("created"), int) else None,
            "status": item.get("status") if isinstance(item.get("status"), str) else None,
            "release_date": item.get("release_date") if isinstance(item.get("release_date"), str) else None,
            "family": item.get("family") if isinstance(item.get("family"), str) else None,
            "name": item.get("name") if isinstance(item.get("name"), str) else None,
            "description": item.get("description") if isinstance(item.get("description"), str) else None,
            "context_window": _optional_int(item.get("context_window"))
            or _optional_int(item.get("context_length"))
            or _optional_int(top_provider.get("context_length")),
            "max_output_tokens": _optional_int(item.get("max_output_tokens"))
            or _optional_int(item.get("max_completion_tokens"))
            or _optional_int(top_provider.get("max_completion_tokens")),
            "input_modalities": _string_list(architecture.get("input_modalities")),
            "output_modalities": _string_list(architecture.get("output_modalities")),
            "supported_parameters": _string_list(item.get("supported_parameters")),
            "architecture": _json_dict(architecture),
            "top_provider": _json_dict(top_provider),
            "pricing": _json_dict(item.get("pricing")),
            "per_request_limits": _json_dict(item.get("per_request_limits")),
        }
    return [models[key] for key in sorted(models)]


def _catalog_entry_from_openai_model(
    provider: ProviderDescriptor,
    model: dict[str, Any],
    *,
    endpoint: str,
    discovered_at: str,
    response_hash: str,
    approval_evidence: dict[str, Any],
    credentials_included: bool,
) -> ModelCatalogEntry:
    model_id = str(model["id"])
    openrouter_compatible = _is_openrouter_compatible_provider(provider)
    supported_parameters = list(model.get("supported_parameters") or [])
    input_modalities = list(model.get("input_modalities") or [])
    output_modalities = list(model.get("output_modalities") or [])
    modalities = sorted({*input_modalities, *output_modalities}) or ["text"]
    context_limit = _optional_int(model.get("context_window")) or provider.capabilities.max_context_tokens
    max_output_tokens = _optional_int(model.get("max_output_tokens"))
    reasoning_support = "effort" if openrouter_compatible and _openrouter_supports_reasoning(supported_parameters) else "unknown"
    tool_support = provider.capabilities.tool_calling
    if openrouter_compatible and supported_parameters:
        tool_support = any(parameter in supported_parameters for parameter in ("tools", "tool_choice"))
    discovery_metadata = {
        "schema_version": "harness.model_discovery_metadata/v1",
        "discovered_at": discovered_at,
        "endpoint": endpoint,
        "network_accessed": True,
        "credentials_included": credentials_included,
        "approval_evidence": approval_evidence,
        "raw_provider_response_sha256": response_hash,
        "provider_model_id": model_id,
        "owner": model.get("owned_by"),
        "created": model.get("created"),
        "status": _model_status(model.get("status")),
        "release_date": model.get("release_date"),
        "family": model.get("family"),
    }
    if openrouter_compatible:
        discovery_metadata.update(
            {
                "compatibility": "openrouter",
                "openrouter": True,
                "display_name": model.get("name"),
                "description": model.get("description"),
                "architecture": model.get("architecture") or {},
                "top_provider": model.get("top_provider") or {},
                "supported_parameters": supported_parameters,
                "per_request_limits": model.get("per_request_limits") or {},
            }
        )
    return ModelCatalogEntry(
        provider_id=provider.provider_id,
        backend_id=provider.backend_id or provider.provider_id,
        model_id=model_id,
        raw_model_ref=f"{provider.provider_id}/{model_id}",
        canonical_model_ref=f"{provider.provider_id}/{model_id}",
        source="discovered",
        status=_model_status(model.get("status")),
        capabilities=provider.capabilities,
        context_limit=context_limit,
        max_output_tokens=max_output_tokens,
        cost=model.get("pricing") if openrouter_compatible and isinstance(model.get("pricing"), dict) else None,
        modalities=modalities,
        reasoning_support=reasoning_support,
        tool_support=tool_support,
        release_date=model.get("release_date") if isinstance(model.get("release_date"), str) else None,
        family=model.get("family") if isinstance(model.get("family"), str) else None,
        endpoint=endpoint,
        discovered_at=discovered_at,
        discovery_metadata=discovery_metadata,
        metadata_only=True,
        provider_execution_started=False,
        model_execution_started=False,
        network_accessed=True,
        credentials_included=credentials_included,
        refresh_supported=True,
        hidden_provider_fallback=False,
        hidden_model_fallback=False,
        no_hidden_fallback=True,
        permission_granting=False,
        authority_granting=False,
        safety_notes=[
            f"Model {model_id} was discovered from an explicit provider refresh.",
            *(
                ["OpenRouter-compatible metadata was preserved for operator inspection and request serialization."]
                if openrouter_compatible
                else []
            ),
            "Discovered models are metadata only and do not enable hidden provider fallback.",
        ],
    )


def _google_models_from_response(response: dict[str, Any]) -> list[dict[str, Any]]:
    data = response.get("models")
    if not isinstance(data, list):
        raise ModelDiscoveryError("google", "Google model list response did not include a models list.", ["invalid_models_response"])
    models: dict[str, dict[str, Any]] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        raw_name = item.get("name")
        if not isinstance(raw_name, str) or not raw_name.strip():
            continue
        model_id = _google_model_id(raw_name)
        if not model_id:
            continue
        models[model_id] = {
            "id": model_id,
            "name": raw_name.strip(),
            "display_name": item.get("displayName") if isinstance(item.get("displayName"), str) else None,
            "description": item.get("description") if isinstance(item.get("description"), str) else None,
            "input_token_limit": _optional_int(item.get("inputTokenLimit")),
            "output_token_limit": _optional_int(item.get("outputTokenLimit")),
            "supported_generation_methods": [
                str(method)
                for method in item.get("supportedGenerationMethods", [])
                if isinstance(method, str) and method.strip()
            ],
        }
    return [models[key] for key in sorted(models)]


def _catalog_entry_from_google_model(
    provider: ProviderDescriptor,
    model: dict[str, Any],
    *,
    endpoint: str,
    discovered_at: str,
    response_hash: str,
    approval_evidence: dict[str, Any],
    credentials_included: bool,
) -> ModelCatalogEntry:
    model_id = str(model["id"])
    supported_generation_methods = list(model.get("supported_generation_methods") or [])
    return ModelCatalogEntry(
        provider_id=provider.provider_id,
        backend_id=provider.backend_id or provider.provider_id,
        model_id=model_id,
        raw_model_ref=f"{provider.provider_id}/{model_id}",
        canonical_model_ref=f"{provider.provider_id}/{model_id}",
        source="discovered",
        status=_model_status(model.get("status")),
        capabilities=provider.capabilities,
        context_limit=model.get("input_token_limit") or provider.capabilities.max_context_tokens,
        max_output_tokens=model.get("output_token_limit"),
        modalities=["text", "image"],
        reasoning_support="tokens" if "2.5" in model_id else "unknown",
        tool_support=provider.capabilities.tool_calling,
        release_date=model.get("release_date") if isinstance(model.get("release_date"), str) else None,
        family=model.get("family") if isinstance(model.get("family"), str) else None,
        endpoint=endpoint,
        discovered_at=discovered_at,
        discovery_metadata={
            "schema_version": "harness.model_discovery_metadata/v1",
            "discovered_at": discovered_at,
            "endpoint": endpoint,
            "network_accessed": True,
            "credentials_included": credentials_included,
            "approval_evidence": approval_evidence,
            "raw_provider_response_sha256": response_hash,
            "provider_model_id": model_id,
            "provider_model_name": model.get("name"),
            "display_name": model.get("display_name"),
            "description": model.get("description"),
            "supported_generation_methods": supported_generation_methods,
        },
        metadata_only=True,
        provider_execution_started=False,
        model_execution_started=False,
        network_accessed=True,
        credentials_included=credentials_included,
        refresh_supported=True,
        hidden_provider_fallback=False,
        hidden_model_fallback=False,
        no_hidden_fallback=True,
        permission_granting=False,
        authority_granting=False,
        safety_notes=[
            f"Model {model_id} was discovered from an explicit Google model-list refresh.",
            "Discovered models are metadata only and do not enable hidden provider fallback.",
        ],
    )


def _bedrock_models_from_response(response: dict[str, Any]) -> list[dict[str, Any]]:
    data = response.get("modelSummaries")
    if not isinstance(data, list):
        raise ModelDiscoveryError("bedrock", "Bedrock foundation model response did not include a modelSummaries list.", ["invalid_models_response"])
    models: dict[str, dict[str, Any]] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        model_id = item.get("modelId")
        if not isinstance(model_id, str) or not model_id.strip():
            continue
        lifecycle = item.get("modelLifecycle") if isinstance(item.get("modelLifecycle"), dict) else {}
        models[model_id.strip()] = {
            "id": model_id.strip(),
            "arn": item.get("modelArn") if isinstance(item.get("modelArn"), str) else None,
            "name": item.get("modelName") if isinstance(item.get("modelName"), str) else None,
            "provider_name": item.get("providerName") if isinstance(item.get("providerName"), str) else None,
            "input_modalities": _lower_string_list(item.get("inputModalities")),
            "output_modalities": _lower_string_list(item.get("outputModalities")),
            "response_streaming_supported": item.get("responseStreamingSupported")
            if isinstance(item.get("responseStreamingSupported"), bool)
            else None,
            "customizations_supported": _lower_string_list(item.get("customizationsSupported")),
            "inference_types_supported": _lower_string_list(item.get("inferenceTypesSupported")),
            "model_lifecycle": _json_dict(lifecycle),
            "status": _bedrock_model_status(lifecycle),
        }
    return [models[key] for key in sorted(models)]


def _catalog_entry_from_bedrock_model(
    provider: ProviderDescriptor,
    model: dict[str, Any],
    *,
    endpoint: str,
    discovery_endpoint: str,
    region: str,
    discovered_at: str,
    response_hash: str,
    approval_evidence: dict[str, Any],
) -> ModelCatalogEntry:
    model_id = str(model["id"])
    input_modalities = list(model.get("input_modalities") or [])
    output_modalities = list(model.get("output_modalities") or [])
    modalities = sorted({*input_modalities, *output_modalities}) or ["text"]
    return ModelCatalogEntry(
        provider_id=provider.provider_id,
        backend_id=provider.backend_id or provider.provider_id,
        model_id=model_id,
        raw_model_ref=f"{provider.provider_id}/{model_id}",
        canonical_model_ref=f"{provider.provider_id}/{model_id}",
        source="discovered",
        status=_model_status(model.get("status")),
        capabilities=provider.capabilities,
        context_limit=provider.capabilities.max_context_tokens,
        modalities=modalities,
        reasoning_support="unknown",
        tool_support=provider.capabilities.tool_calling,
        release_date=model.get("release_date") if isinstance(model.get("release_date"), str) else None,
        family=model.get("family") if isinstance(model.get("family"), str) else None,
        endpoint=endpoint,
        discovered_at=discovered_at,
        discovery_metadata={
            "schema_version": "harness.model_discovery_metadata/v1",
            "discovered_at": discovered_at,
            "endpoint": discovery_endpoint,
            "runtime_endpoint": endpoint,
            "network_accessed": True,
            "credentials_included": True,
            "approval_evidence": approval_evidence,
            "raw_provider_response_sha256": response_hash,
            "provider_model_id": model_id,
            "model_arn": model.get("arn"),
            "model_name": model.get("name"),
            "provider_name": model.get("provider_name"),
            "aws_region": region,
            "input_modalities": input_modalities,
            "output_modalities": output_modalities,
            "response_streaming_supported": model.get("response_streaming_supported"),
            "customizations_supported": model.get("customizations_supported") or [],
            "inference_types_supported": model.get("inference_types_supported") or [],
            "model_lifecycle": model.get("model_lifecycle") or {},
        },
        metadata_only=True,
        provider_execution_started=False,
        model_execution_started=False,
        network_accessed=True,
        credentials_included=True,
        refresh_supported=True,
        hidden_provider_fallback=False,
        hidden_model_fallback=False,
        no_hidden_fallback=True,
        permission_granting=False,
        authority_granting=False,
        safety_notes=[
            f"Model {model_id} was discovered from an explicit Bedrock foundation-model refresh.",
            "Discovered models are metadata only and do not enable hidden provider fallback.",
        ],
    )


def _static_catalog_models_for_provider(provider_id: str) -> dict[str, dict[str, Any]]:
    catalog = load_generated_static_model_catalog()
    return {
        raw_model_ref: dict(metadata)
        for raw_model_ref, metadata in catalog.items()
        if isinstance(raw_model_ref, str)
        and isinstance(metadata, dict)
        and metadata.get("provider_id") == provider_id
    }


def _catalog_entry_from_static_model(
    provider: ProviderDescriptor,
    raw_model_ref: str,
    metadata: dict[str, Any],
    *,
    discovered_at: str,
    response_hash: str,
    approval_evidence: dict[str, Any],
) -> ModelCatalogEntry:
    model_id = str(metadata.get("model_id") or raw_model_ref.split("/", 1)[-1])
    release_date = metadata.get("release_date")
    family = metadata.get("family")
    return ModelCatalogEntry(
        provider_id=provider.provider_id,
        backend_id=provider.backend_id or provider.provider_id,
        model_id=model_id,
        raw_model_ref=raw_model_ref,
        canonical_model_ref=raw_model_ref,
        protocol=str(metadata.get("protocol") or provider.protocol_defaults.get("protocol") or ""),
        status=_model_status(metadata.get("status")),
        source="static_catalog",
        capabilities=provider.capabilities,
        context_limit=_optional_int(metadata.get("context_limit")) or provider.capabilities.max_context_tokens,
        max_output_tokens=_optional_int(metadata.get("max_output_tokens")),
        cost=metadata.get("cost") if isinstance(metadata.get("cost"), dict) else None,
        modalities=_modalities_from_metadata(metadata),
        reasoning_support=str(metadata.get("reasoning_support") or "unknown"),
        tool_support=bool(metadata.get("tool_support", provider.capabilities.tool_calling)),
        release_date=str(release_date or "") or None,
        family=str(family or "") or None,
        endpoint=provider.endpoint,
        discovered_at=discovered_at,
        discovery_metadata={
            "schema_version": "harness.model_discovery_metadata/v1",
            "discovered_at": discovered_at,
            "endpoint": None,
            "network_accessed": False,
            "credentials_included": False,
            "approval_evidence": approval_evidence,
            "raw_provider_response_sha256": response_hash,
            "provider_model_id": model_id,
            "static_catalog": True,
            "static_catalog_source": metadata.get("source"),
            "release_date": release_date,
            "family": family,
        },
        metadata_only=True,
        provider_execution_started=False,
        model_execution_started=False,
        network_accessed=False,
        credentials_included=False,
        refresh_supported=True,
        hidden_provider_fallback=False,
        hidden_model_fallback=False,
        no_hidden_fallback=True,
        permission_granting=False,
        authority_granting=False,
        safety_notes=[
            f"Model {model_id} was loaded from the generated static Anthropic catalog.",
            *_string_list(metadata.get("safety_notes")),
            "Static catalog models are metadata only and do not enable hidden provider fallback.",
        ],
    )


def _discovery_headers(credential: ResolvedProviderCredential | None) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if credential is None:
        return headers
    headers.update({str(key): str(value) for key, value in credential.headers.items() if value is not None})
    if credential.credentials_included and credential.api_key:
        headers.setdefault("Authorization", f"Bearer {credential.api_key}")
    return headers


def _google_discovery_headers(credential: ResolvedProviderCredential | None) -> dict[str, str]:
    headers = _discovery_headers(credential)
    if credential is not None and credential.credentials_included and credential.api_key:
        headers.setdefault("x-goog-api-key", credential.api_key)
    return headers


def _google_model_id(raw_name: str) -> str:
    name = raw_name.strip()
    if name.startswith("models/"):
        return name.removeprefix("models/").strip()
    return name


def _bedrock_discovery_region(provider: ProviderDescriptor) -> str:
    region = provider.protocol_defaults.get("aws_region") or _region_from_bedrock_endpoint(provider.endpoint) or "us-east-1"
    return str(region)


def _region_from_bedrock_endpoint(endpoint: str | None) -> str | None:
    if not endpoint:
        return None
    host = urllib.parse.urlparse(endpoint).netloc
    parts = host.split(".")
    if len(parts) >= 3 and parts[0] in {"bedrock", "bedrock-runtime"}:
        return parts[1]
    return None


def _bedrock_foundation_models_endpoint(region: str) -> str:
    return f"https://bedrock.{region}.amazonaws.com/foundation-models"


def _bedrock_discovery_auth_material(provider: ProviderDescriptor, credential: ResolvedProviderCredential) -> dict[str, str]:
    kind = str(credential.credential_kind or "").casefold()
    source = str(credential.source or "").casefold()
    if kind in {"oauth", "bearer", "api_key"} and credential.api_key:
        return {"bearer_token": credential.api_key}
    if kind == "aws_env" or source == "aws_env":
        return _bedrock_auth_material_from_env(provider.provider_id)
    if kind == "aws_profile" or source in {"aws_profile", "provider_account"} or provider.provider_id == "bedrock":
        profile = str(provider.protocol_defaults.get("aws_profile") or os.environ.get(str(credential.env_var or "AWS_PROFILE")) or "default")
        return _bedrock_auth_material_from_profile(provider.provider_id, profile)
    raise ModelDiscoveryError(provider.provider_id, "Bedrock discovery requires AWS credentials.", ["credential_missing"])


def _bedrock_auth_material_from_env(provider_id: str) -> dict[str, str]:
    access_key_id = os.environ.get("AWS_ACCESS_KEY_ID")
    secret_access_key = os.environ.get("AWS_SECRET_ACCESS_KEY")
    if not access_key_id or not secret_access_key:
        raise ModelDiscoveryError(provider_id, "Bedrock discovery requires AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY.", ["credential_missing"])
    material = {"access_key_id": access_key_id, "secret_access_key": secret_access_key}
    session_token = os.environ.get("AWS_SESSION_TOKEN")
    if session_token:
        material["session_token"] = session_token
    return material


def _bedrock_auth_material_from_profile(provider_id: str, profile: str) -> dict[str, str]:
    parser = configparser.ConfigParser()
    credentials_file = os.environ.get("AWS_SHARED_CREDENTIALS_FILE") or os.path.join(os.path.expanduser("~"), ".aws", "credentials")
    config_file = os.environ.get("AWS_CONFIG_FILE") or os.path.join(os.path.expanduser("~"), ".aws", "config")
    parser.read([credentials_file, config_file])
    section_names = [profile, f"profile {profile}"] if profile != "default" else ["default", "profile default"]
    for section in section_names:
        if not parser.has_section(section):
            continue
        access_key_id = parser.get(section, "aws_access_key_id", fallback="").strip()
        secret_access_key = parser.get(section, "aws_secret_access_key", fallback="").strip()
        if access_key_id and secret_access_key:
            material = {"access_key_id": access_key_id, "secret_access_key": secret_access_key}
            session_token = parser.get(section, "aws_session_token", fallback="").strip()
            if session_token:
                material["session_token"] = session_token
            return material
    raise ModelDiscoveryError(provider_id, "Bedrock discovery could not load AWS profile credentials.", ["credential_missing"])


def _bedrock_discovery_headers(
    provider: ProviderDescriptor,
    credential: ResolvedProviderCredential,
    url: str,
    *,
    region: str,
    auth: dict[str, str],
) -> dict[str, str]:
    base_headers = {
        **_discovery_headers(credential),
        "X-Harness-AWS-Credential-Source": credential.source or credential.credential_kind,
        "X-Harness-AWS-Region": region,
    }
    if auth.get("bearer_token"):
        return {**base_headers, "Authorization": f"Bearer {auth['bearer_token']}"}
    return _aws_sigv4_get_headers(base_headers, url=url, region=region, auth=auth, provider_id=provider.provider_id)


def _aws_sigv4_get_headers(
    headers: dict[str, str],
    *,
    url: str,
    region: str,
    auth: dict[str, str],
    provider_id: str,
) -> dict[str, str]:
    access_key_id = auth.get("access_key_id")
    secret_access_key = auth.get("secret_access_key")
    if not access_key_id or not secret_access_key:
        raise ModelDiscoveryError(provider_id, "Bedrock discovery requires AWS access key material.", ["credential_missing"])
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc
    now = datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    payload_hash = hashlib.sha256(b"").hexdigest()
    signed_header_values = {
        "content-type": headers.get("Content-Type", "application/json"),
        "host": host,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amz_date,
    }
    if auth.get("session_token"):
        signed_header_values["x-amz-security-token"] = auth["session_token"]
    canonical_headers = "".join(f"{key}:{signed_header_values[key]}\n" for key in sorted(signed_header_values))
    signed_headers = ";".join(sorted(signed_header_values))
    canonical_query = _canonical_query(parsed.query)
    canonical_request = "\n".join(
        [
            "GET",
            parsed.path or "/",
            canonical_query,
            canonical_headers,
            signed_headers,
            payload_hash,
        ]
    )
    credential_scope = f"{date_stamp}/{region}/bedrock/aws4_request"
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            credential_scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ]
    )
    signing_key = _aws_sigv4_signing_key(secret_access_key, date_stamp, region, "bedrock")
    signature = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    authorization = (
        "AWS4-HMAC-SHA256 "
        f"Credential={access_key_id}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, "
        f"Signature={signature}"
    )
    signed = {
        **headers,
        "Host": host,
        "X-Amz-Date": amz_date,
        "X-Amz-Content-Sha256": payload_hash,
        "Authorization": authorization,
    }
    if auth.get("session_token"):
        signed["X-Amz-Security-Token"] = auth["session_token"]
    return signed


def _canonical_query(query: str) -> str:
    pairs = urllib.parse.parse_qsl(query, keep_blank_values=True)
    return "&".join(
        f"{urllib.parse.quote(key, safe='-_.~')}={urllib.parse.quote(value, safe='-_.~')}"
        for key, value in sorted(pairs)
    )


def _aws_sigv4_signing_key(secret_access_key: str, date_stamp: str, region: str, service: str) -> bytes:
    date_key = hmac.new(("AWS4" + secret_access_key).encode("utf-8"), date_stamp.encode("utf-8"), hashlib.sha256).digest()
    region_key = hmac.new(date_key, region.encode("utf-8"), hashlib.sha256).digest()
    service_key = hmac.new(region_key, service.encode("utf-8"), hashlib.sha256).digest()
    return hmac.new(service_key, b"aws4_request", hashlib.sha256).digest()


def _bedrock_model_status(lifecycle: dict[str, Any]) -> str:
    status = str(lifecycle.get("status") or "active").strip().casefold()
    if status == "active":
        return "active"
    if status in {"legacy", "deprecated"}:
        return "deprecated"
    return "disabled" if status else "active"


def _model_status(value: Any) -> str:
    status = str(value or "active").strip().casefold()
    if status in {"active", "beta", "deprecated", "disabled"}:
        return status
    return "active"


def _modalities_from_metadata(metadata: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for item in [*_string_list(metadata.get("input_modalities")), *_string_list(metadata.get("output_modalities"))]:
        if item not in values:
            values.append(item)
    return values or ["text"]


def _is_openrouter_compatible_provider(provider: ProviderDescriptor) -> bool:
    compatibility = provider.protocol_defaults.get("compatibility")
    if isinstance(compatibility, dict):
        for key in ("provider", "kind", "api", "compatibility"):
            value = compatibility.get(key)
            if isinstance(value, str) and value.casefold() == "openrouter":
                return True
        if compatibility.get("openrouter") is True:
            return True
    endpoint = provider.endpoint or ""
    try:
        host = urllib.parse.urlparse(endpoint).netloc.casefold()
    except ValueError:
        return False
    return host == "openrouter.ai" or host.endswith(".openrouter.ai")


def _openrouter_supports_reasoning(supported_parameters: list[str]) -> bool:
    normalized = {parameter.casefold() for parameter in supported_parameters}
    return bool(normalized & {"reasoning", "reasoning_effort"})


def _json_dict(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in value.items() if item is not None}


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if isinstance(item, str) and item.strip()]


def _lower_string_list(value: Any) -> list[str]:
    return [item.casefold() for item in _string_list(value)]


def _response_hash(response: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(response, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _optional_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _join_url(base_url: str, path: str) -> str:
    return urllib.parse.urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))


def _provider_descriptor(config: HarnessConfig, provider_id: str) -> ProviderDescriptor:
    for provider in build_provider_descriptors(config):
        if provider.provider_id == provider_id:
            return provider
    raise ModelDiscoveryError(provider_id, f"Provider not found: {provider_id}", ["provider_unknown"])


def _canonical_discovery_provider_id(config: HarnessConfig, provider_id: str) -> str:
    if provider_id in config.backends:
        return provider_id
    if provider_id == "openai" and "paid_openai_compatible" in config.backends:
        return "paid_openai_compatible"
    return provider_id
