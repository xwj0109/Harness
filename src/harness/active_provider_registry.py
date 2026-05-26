from __future__ import annotations

from difflib import SequenceMatcher
from typing import Any

from pydantic import BaseModel, Field

from harness.config import HarnessConfig
from harness.model_registry import (
    ModelDescriptor,
    ProviderDescriptor,
    build_model_descriptors,
    build_provider_descriptors,
    parse_model_ref,
)
from harness.provider_auth import ProviderCredentialResolutionError, resolve_provider_credential
from harness.registry import SpecRegistry
from harness.security import sanitize_for_logging


ACTIVE_PROVIDER_REGISTRY_SCHEMA_VERSION = "harness.active_provider_registry/v1"


class ActiveProviderState(BaseModel):
    schema_version: str = "harness.active_provider_state/v1"
    provider_id: str
    provider_descriptor: dict[str, Any]
    display_name: str | None = None
    enabled: bool
    connected: bool
    credential_status: str
    credential_source: str
    credential_kind: str | None = None
    account_id: str | None = None
    catalog_source: str
    model_count: int = 0
    available_model_count: int = 0
    default_model_candidate: str | None = None
    constraints: list[str] = Field(default_factory=list)
    blocked_reasons: list[str] = Field(default_factory=list)
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


class ActiveModelState(BaseModel):
    schema_version: str = "harness.active_model_state/v1"
    provider_id: str
    model_id: str
    raw_model_ref: str
    canonical_model_ref: str | None = None
    model_descriptor: dict[str, Any]
    provider_state: dict[str, Any]
    known_catalog_model: bool = True
    available_model: bool
    executable_model: bool
    selected_model: bool = False
    availability: str
    blocked_reasons: list[str] = Field(default_factory=list)
    variant_list: list[str] = Field(default_factory=list)
    capabilities: dict[str, Any] = Field(default_factory=dict)
    cost: dict[str, Any] | None = None
    limits: dict[str, int | None] = Field(default_factory=dict)
    source: str
    catalog_source: str
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


class ActiveProviderRegistry:
    def __init__(
        self,
        config: HarnessConfig,
        *,
        store: Any | None = None,
        provider_accounts: list[dict[str, Any]] | None = None,
        registry: SpecRegistry | None = None,
        model_overlays: list[Any] | None = None,
        runtime_flags: dict[str, Any] | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.provider_accounts = provider_accounts if provider_accounts is not None else _store_provider_accounts(store)
        self.registry = registry
        self.model_overlays = model_overlays or []
        self.runtime_flags = runtime_flags or {}
        self.provider_descriptors = build_provider_descriptors(config, provider_accounts=self.provider_accounts)
        self.model_descriptors = build_model_descriptors(config, registry)
        self._providers_by_id: dict[str, ActiveProviderState] | None = None
        self._models_by_ref: dict[str, ActiveModelState] | None = None

    def list_all_providers(self) -> list[ActiveProviderState]:
        return list(self._provider_states().values())

    def list_connected_providers(self) -> list[ActiveProviderState]:
        return [provider for provider in self.list_all_providers() if provider.connected]

    def list_available_models(self) -> list[ActiveModelState]:
        return [model for model in self._model_states().values() if model.available_model]

    def list_all_models(self) -> list[ActiveModelState]:
        return list(self._model_states().values())

    def get_provider(self, provider_id: str) -> ActiveProviderState | None:
        return self._provider_states().get(str(provider_id or "").strip())

    def get_model(self, provider_id: str, model_id: str) -> ActiveModelState | None:
        provider = str(provider_id or "").strip()
        model = str(model_id or "").strip()
        if not provider or not model:
            return None
        states = self._model_states()
        return states.get(f"{provider}/{model}") or next(
            (candidate for candidate in states.values() if candidate.provider_id == provider and candidate.model_id == model),
            None,
        )

    def suggest_models(self, raw_query: str | None, *, limit: int = 8) -> list[dict[str, Any]]:
        query = str(raw_query or "").strip().casefold()
        terms = [term for term in query.replace("/", " ").replace("-", " ").split() if term]
        suggestions: list[tuple[tuple[int, str, str], ActiveModelState]] = []
        for model in self._model_states().values():
            haystack = " ".join(
                str(value or "")
                for value in (
                    model.raw_model_ref,
                    model.canonical_model_ref,
                    model.provider_id,
                    model.model_id,
                    model.source,
                )
            ).casefold()
            match_rank = _suggestion_match_rank(query, terms, haystack)
            if match_rank is None:
                continue
            rank = match_rank * 10 + (0 if model.available_model else 1)
            suggestions.append(((rank, model.provider_id, model.raw_model_ref), model))
        return [
            {
                "schema_version": "harness.model_suggestion/v1",
                "raw_model_ref": model.raw_model_ref,
                "canonical_model_ref": model.canonical_model_ref,
                "provider_id": model.provider_id,
                "model_id": model.model_id,
                "available_model": model.available_model,
                "executable_model": model.executable_model,
                "blocked_reasons": model.blocked_reasons,
                "suggestion_only": True,
                "selected_model": False,
                "provider_execution_started": False,
                "model_execution_started": False,
                "network_accessed": False,
                "credentials_included": False,
                "hidden_provider_fallback": False,
                "hidden_model_fallback": False,
                "no_hidden_fallback": True,
                "permission_granting": False,
            }
            for _, model in sorted(suggestions, key=lambda item: item[0])[:limit]
        ]

    def suggest_providers(self, raw_query: str | None, *, limit: int = 6) -> list[dict[str, Any]]:
        query = str(raw_query or "").strip().casefold()
        terms = [term for term in query.replace("/", " ").replace("-", " ").replace("_", " ").split() if term]
        suggestions: list[tuple[tuple[int, str], ActiveProviderState]] = []
        for provider in self._provider_states().values():
            haystack = " ".join(
                str(value or "")
                for value in (
                    provider.provider_id,
                    provider.display_name,
                    provider.catalog_source,
                    provider.credential_status,
                )
            ).casefold()
            match_rank = _suggestion_match_rank(query, terms, haystack)
            if match_rank is None:
                continue
            availability_rank = 0 if provider.connected else 1 if provider.enabled else 2
            suggestions.append(((match_rank * 10 + availability_rank, provider.provider_id), provider))
        return [_provider_suggestion_payload(provider) for _, provider in sorted(suggestions, key=lambda item: item[0])[:limit]]

    def resolve_session_default(self, session_id: str) -> ActiveModelState | None:
        if self.store is None or not hasattr(self.store, "get_session"):
            return None
        try:
            session = self.store.get_session(session_id)
        except Exception:
            return None
        raw_ref = str(getattr(session, "raw_model_ref", "") or "").strip()
        if not raw_ref:
            return None
        parsed = parse_model_ref(raw_ref)
        if parsed.provider_id and parsed.model_id:
            return self.get_model(parsed.provider_id, parsed.model_id)
        return self._model_states().get(raw_ref)

    def _provider_states(self) -> dict[str, ActiveProviderState]:
        if self._providers_by_id is not None:
            return self._providers_by_id
        model_counts = _model_counts(self.model_descriptors, self.model_overlays)
        states: dict[str, ActiveProviderState] = {}
        for provider in self.provider_descriptors:
            try:
                credential = resolve_provider_credential(self.config, provider, self.store, allow_secret_material=False)
                credential_status = credential.status
                credential_source = credential.source
                credential_kind = credential.credential_kind
                account_id = credential.account_id
            except ProviderCredentialResolutionError as exc:
                credential_status = exc.reason
                credential_source = exc.reason
                credential_kind = getattr(provider.credential, "kind", None)
                account_id = getattr(provider.credential, "account_id", None)
            blocked = _provider_blocked_reasons(provider, credential_status)
            connected = credential_status in {"configured", "not_required"} and not blocked
            states[provider.provider_id] = ActiveProviderState(
                provider_id=provider.provider_id,
                provider_descriptor=sanitize_for_logging(provider.model_dump(mode="json")),
                display_name=provider.display_name,
                enabled=provider.enabled,
                connected=connected,
                credential_status=credential_status,
                credential_source=credential_source,
                credential_kind=str(credential_kind) if credential_kind else None,
                account_id=str(account_id) if account_id else None,
                catalog_source=provider.source,
                model_count=model_counts.get(provider.provider_id, 0),
                default_model_candidate=_default_model_candidate(provider.provider_id, self.model_descriptors),
                constraints=list(provider.constraints),
                blocked_reasons=blocked,
            )
        self._providers_by_id = states
        return states

    def _model_states(self) -> dict[str, ActiveModelState]:
        if self._models_by_ref is not None:
            return self._models_by_ref
        providers = self._provider_states()
        states: dict[str, ActiveModelState] = {}
        for model in self.model_descriptors:
            state = _active_model_from_descriptor(model, providers.get(model.provider_id))
            states[state.raw_model_ref] = state
        for overlay in self.model_overlays:
            state = _active_model_from_overlay(overlay, providers.get(str(getattr(overlay, "provider_id", "") or "")))
            states.setdefault(state.raw_model_ref, state)
        self._models_by_ref = dict(sorted(states.items(), key=lambda item: _model_state_sort_key(item[1])))
        self._providers_by_id = _providers_with_available_counts(self._providers_by_id or {}, self._models_by_ref)
        return self._models_by_ref


def _suggestion_match_rank(query: str, terms: list[str], haystack: str) -> int | None:
    if not terms:
        return 2
    if query and query in haystack:
        return 0
    if all(term in haystack for term in terms):
        return 1
    if any(term in haystack for term in terms):
        return 2
    best_ratio = max((SequenceMatcher(None, term, token).ratio() for term in terms for token in haystack.split()), default=0.0)
    return 3 if best_ratio >= 0.72 else None


def _provider_suggestion_payload(provider: ActiveProviderState) -> dict[str, Any]:
    return {
        "schema_version": "harness.provider_suggestion/v1",
        "provider_id": provider.provider_id,
        "display_name": provider.display_name,
        "enabled": provider.enabled,
        "connected": provider.connected,
        "credential_status": provider.credential_status,
        "available_model_count": provider.available_model_count,
        "default_model_candidate": provider.default_model_candidate,
        "blocked_reasons": list(provider.blocked_reasons),
        "suggestion_only": True,
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


def _store_provider_accounts(store: Any | None) -> list[dict[str, Any]]:
    if store is None or not hasattr(store, "list_provider_accounts"):
        return []
    try:
        return [account for account in store.list_provider_accounts() if isinstance(account, dict)]
    except Exception:
        return []


def _model_counts(descriptors: list[ModelDescriptor], overlays: list[Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for model in descriptors:
        counts[model.provider_id] = counts.get(model.provider_id, 0) + 1
    for overlay in overlays:
        provider_id = str(getattr(overlay, "provider_id", "") or "")
        if provider_id:
            counts[provider_id] = counts.get(provider_id, 0) + 1
    return counts


def _provider_blocked_reasons(provider: ProviderDescriptor, credential_status: str) -> list[str]:
    if not provider.enabled:
        return ["provider_disabled"]
    blocked = [
        constraint
        for constraint in provider.constraints
        if constraint not in {"disabled_by_default", "no_automatic_fallback", "preflight_skipped"}
    ]
    credential_reason = {
        "missing": "credential_missing",
        "expired": "credential_expired",
        "refresh_required": "credential_refresh_required",
        "credential_refresh_required": "credential_refresh_required",
        "credential_missing": "credential_missing",
    }.get(credential_status)
    if credential_reason and credential_reason not in blocked:
        blocked.append(credential_reason)
    return blocked


def _default_model_candidate(provider_id: str, models: list[ModelDescriptor]) -> str | None:
    for model in models:
        if model.provider_id == provider_id and model.source == "backend_config":
            return model.raw_model_ref
    for model in models:
        if model.provider_id == provider_id:
            return model.raw_model_ref
    return None


def _active_model_from_descriptor(model: ModelDescriptor, provider: ActiveProviderState | None) -> ActiveModelState:
    blocked = _model_blocked_reasons(model.status, provider)
    available = bool(provider and provider.enabled and provider.connected and not blocked and model.status != "disabled")
    executable = available
    return ActiveModelState(
        provider_id=model.provider_id,
        model_id=model.model_id,
        raw_model_ref=model.raw_model_ref,
        canonical_model_ref=model.canonical_model_ref,
        model_descriptor=sanitize_for_logging(model.model_dump(mode="json")),
        provider_state=provider.model_dump(mode="json") if provider else {},
        available_model=available,
        executable_model=executable,
        availability="available" if available else "blocked",
        blocked_reasons=blocked,
        variant_list=sorted(model.variants),
        capabilities=(provider.provider_descriptor.get("capabilities") if provider else {}) or {},
        cost=sanitize_for_logging(model.cost),
        limits={"context": model.context_limit, "max_output": model.max_output_tokens},
        source=model.source,
        catalog_source=model.metadata_source or model.source,
    )


def _active_model_from_overlay(overlay: Any, provider: ActiveProviderState | None) -> ActiveModelState:
    raw_ref = str(getattr(overlay, "raw_model_ref", "") or "")
    model_id = str(getattr(overlay, "model_id", "") or raw_ref.split("/", 1)[-1])
    provider_id = str(getattr(overlay, "provider_id", "") or raw_ref.split("/", 1)[0])
    status = str(getattr(overlay, "status", "active") or "active")
    blocked = _model_blocked_reasons(status, provider)
    available = bool(provider and provider.enabled and provider.connected and not blocked and status != "disabled")
    descriptor = overlay.model_dump(mode="json") if hasattr(overlay, "model_dump") else dict(getattr(overlay, "__dict__", {}) or {})
    return ActiveModelState(
        provider_id=provider_id,
        model_id=model_id,
        raw_model_ref=raw_ref,
        canonical_model_ref=getattr(overlay, "canonical_model_ref", None),
        model_descriptor=sanitize_for_logging(descriptor),
        provider_state=provider.model_dump(mode="json") if provider else {},
        available_model=available,
        executable_model=available,
        availability="available" if available else "blocked",
        blocked_reasons=blocked,
        variant_list=[],
        capabilities=(provider.provider_descriptor.get("capabilities") if provider else {}) or {},
        cost=sanitize_for_logging(getattr(overlay, "cost", None)),
        limits={"context": getattr(overlay, "context_limit", None), "max_output": getattr(overlay, "max_output_tokens", None)},
        source=str(getattr(overlay, "source", "discovered") or "discovered"),
        catalog_source=str(getattr(overlay, "discovered_at", None) or getattr(overlay, "source", "discovered") or "discovered"),
    )


def _model_blocked_reasons(status: str, provider: ActiveProviderState | None) -> list[str]:
    blocked: list[str] = []
    if provider is None:
        blocked.append("provider_unknown")
    else:
        blocked.extend(provider.blocked_reasons)
        if not provider.enabled:
            return _dedupe(blocked)
    if status == "disabled" and "model_disabled" not in blocked:
        blocked.append("model_disabled")
    return _dedupe(blocked)


def _providers_with_available_counts(
    providers: dict[str, ActiveProviderState],
    models: dict[str, ActiveModelState],
) -> dict[str, ActiveProviderState]:
    counts: dict[str, int] = {}
    for model in models.values():
        if model.available_model:
            counts[model.provider_id] = counts.get(model.provider_id, 0) + 1
    return {
        provider_id: provider.model_copy(update={"available_model_count": counts.get(provider_id, 0)})
        for provider_id, provider in providers.items()
    }


def _model_state_sort_key(model: ActiveModelState) -> tuple[str, str, str, str]:
    return (model.provider_id, model.source, model.model_id, model.raw_model_ref)


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result
