from __future__ import annotations

import fcntl
import base64
import hashlib
import json
import os
import secrets
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

PROVIDER_AUTH_METHODS_SCHEMA_VERSION = "harness.provider_auth_methods/v1"
PROVIDER_AUTH_ACTION_SCHEMA_VERSION = "harness.provider_auth_action/v1"
PROVIDER_SECRET_STORE_SCHEMA_VERSION = "harness.provider_secret_store/v1"
PROVIDER_SECRET_STORE_FILE = "provider_secrets.json"
PROVIDER_SECRET_LOCK_FILE = "provider_secrets.lock"
PROVIDER_OAUTH_REFRESH_LOCK_FILE = "provider_oauth_refresh.lock"


class ResolvedProviderCredential(BaseModel):
    schema_version: str = "harness.resolved_provider_credential/v1"
    provider_id: str
    credential_kind: str
    status: str
    source: str
    env_var: str | None = None
    account_id: str | None = None
    expires_at: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    api_key: str | None = None
    redaction_state: str = "redacted"
    credential_value_included: bool = False
    credentials_included: bool = False
    network_accessed: bool = False
    credential_written: bool = False
    no_hidden_fallback: bool = True

    def redacted_evidence(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "provider_id": self.provider_id,
            "credential_kind": self.credential_kind,
            "status": self.status,
            "source": self.source,
            "env_var": self.env_var,
            "account_id": self.account_id,
            "expires_at": self.expires_at,
            "header_names": sorted(self.headers),
            "redaction_state": "redacted" if self.credentials_included else self.redaction_state,
            "credential_value_included": False,
            "credentials_included": False,
            "network_accessed": False,
            "credential_written": False,
            "no_hidden_fallback": True,
        }


class ProviderCredentialResolutionError(ValueError):
    VALID_REASONS = {
        "provider_unknown",
        "credential_missing",
        "credential_expired",
        "credential_refresh_failed",
        "credential_refresh_required",
        "credential_kind_unsupported",
        "credential_source_unavailable",
    }

    def __init__(self, provider_id: str | None, reason: str, message: str | None = None) -> None:
        self.provider_id = provider_id
        self.reason = reason if reason in self.VALID_REASONS else "credential_source_unavailable"
        self.blocked_reasons = [self.reason]
        super().__init__(message or self.reason)

    def to_provider_error_message(self) -> str:
        provider = self.provider_id or "unknown"
        return f"Provider credential resolution failed for {provider}: {self.reason}"


class ProviderAccountRecord(BaseModel):
    schema_version: str = "harness.provider_account/v1"
    account_id: str
    provider_id: str
    description: str = "default"
    credential_kind: str
    status: str
    active: bool = True
    expires_at: str | None = None
    created_at: str
    updated_at: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    credential_value_included: bool = False
    credentials_included: bool = False
    credential_written: bool = False
    provider_execution_started: bool = False
    model_execution_started: bool = False
    network_accessed: bool = False
    permission_granting: bool = False
    authority_granting: bool = False
    no_hidden_fallback: bool = True


class ProviderOAuthAccountRecord(BaseModel):
    schema_version: str = "harness.provider_oauth_account/v1"
    provider_id: str
    account_id: str
    refresh_token_secret_ref: str | None = None
    access_token_secret_ref: str | None = None
    expires_at: str | None = None
    scopes: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    credential_value_included: bool = False
    credentials_included: bool = False
    provider_execution_started: bool = False
    model_execution_started: bool = False
    network_accessed: bool = False
    permission_granting: bool = False
    authority_granting: bool = False
    no_hidden_fallback: bool = True


def active_account_by_provider(accounts: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for account in accounts:
        if not account.get("active"):
            continue
        provider_id = account.get("provider_id")
        if isinstance(provider_id, str) and provider_id and provider_id not in result:
            result[provider_id] = account
    return result


def provider_auth_methods_projection(config: Any, store: Any | None = None) -> dict[str, Any]:
    from harness.model_registry import build_provider_descriptors

    accounts = _provider_accounts(store)
    active_accounts = active_account_by_provider(accounts)
    providers = []
    for provider in build_provider_descriptors(config, provider_accounts=accounts):
        try:
            credential = resolve_provider_credential(config, provider, store, allow_secret_material=False)
            credential_status = credential.status
            credential_source = credential.source
        except ProviderCredentialResolutionError as exc:
            credential_status = "missing"
            credential_source = exc.reason
        active_account = active_accounts.get(provider.provider_id)
        methods = _supported_auth_methods(config, provider.provider_id)
        oauth_supported = any(method["method"] == "oauth" and method["supported"] for method in methods)
        providers.append(
            {
                "schema_version": "harness.provider_auth_methods_entry/v1",
                "provider_id": provider.provider_id,
                "display_name": provider.display_name,
                "backend_id": provider.backend_id or provider.provider_id,
                "enabled": provider.enabled,
                "credential_status": credential_status,
                "credential_source": credential_source,
                "configured": credential_status in {"configured", "not_required"},
                "active_account_id": active_account.get("account_id") if active_account else None,
                "active_credential_kind": active_account.get("credential_kind") if active_account else None,
                "account_count": len([account for account in accounts if account.get("provider_id") == provider.provider_id]),
                "auth_methods": [method["method"] for method in methods if method["supported"]],
                "methods": methods,
                "oauth_supported": oauth_supported,
                "credentials_included": False,
                "credential_value_included": False,
                "credential_write_supported": any(method["supported"] for method in methods),
                "credential_written": False,
                "provider_execution_started": False,
                "model_execution_started": False,
                "network_accessed": False,
                "permission_granting": False,
                "authority_granting": False,
                "no_hidden_fallback": True,
            }
        )
    auth_methods = sorted({method for provider in providers for method in provider["auth_methods"]})
    oauth_supported_providers = [provider["provider_id"] for provider in providers if provider["oauth_supported"]]
    credential_write_supported_providers = [
        provider["provider_id"] for provider in providers if provider["credential_write_supported"]
    ]
    return {
        "schema_version": PROVIDER_AUTH_METHODS_SCHEMA_VERSION,
        "ok": True,
        "providers": providers,
        "auth_methods": auth_methods,
        "methods_by_provider": {provider["provider_id"]: provider["auth_methods"] for provider in providers},
        "oauth_supported_providers": oauth_supported_providers,
        "oauth_unsupported_providers": [
            provider["provider_id"] for provider in providers if not provider["oauth_supported"]
        ],
        "oauth_support": {provider["provider_id"]: provider["oauth_supported"] for provider in providers},
        "credential_write_supported_providers": credential_write_supported_providers,
        "credential_write_support": {
            provider["provider_id"]: provider["credential_write_supported"] for provider in providers
        },
        "credentials_included": False,
        "credential_value_included": False,
        "provider_execution_started": False,
        "model_execution_started": False,
        "network_accessed": False,
        "permission_granting": False,
        "authority_granting": False,
        "no_hidden_fallback": True,
    }


def connect_provider_api_key(
    project_root: Path,
    store: Any,
    config: Any,
    provider_id: str,
    api_key: str,
    *,
    description: str = "default",
    active: bool = True,
) -> dict[str, Any]:
    clean_provider_id = _require_provider(config, provider_id)
    clean_api_key = str(api_key or "")
    if not clean_api_key:
        raise ProviderCredentialResolutionError(clean_provider_id, "credential_missing", "Missing API key.")
    account = store.create_provider_account(
        provider_id=clean_provider_id,
        credential_kind="api_key",
        status="configured",
        description=description or "default",
        active=active,
        metadata={"secret_store": PROVIDER_SECRET_STORE_FILE},
    )
    try:
        secret_write = write_provider_account_secret(Path(project_root), account, clean_api_key)
    except Exception:
        try:
            store.remove_provider_account(account["account_id"])
        except Exception:
            pass
        raise
    return _provider_auth_action_payload(
        clean_provider_id,
        "api_key",
        account=account,
        secret_write=secret_write,
        account_created=True,
        account_activated=active,
        credential_written=True,
    )


def connect_provider_env(
    store: Any,
    config: Any,
    provider_id: str,
    env_var: str,
    *,
    description: str = "default",
    active: bool = True,
) -> dict[str, Any]:
    clean_provider_id = _require_provider(config, provider_id)
    clean_env_var = str(env_var or "").strip()
    if not clean_env_var:
        raise ProviderCredentialResolutionError(clean_provider_id, "credential_missing", "Missing environment variable name.")
    account = store.create_provider_account(
        provider_id=clean_provider_id,
        credential_kind="env",
        status="configured" if os.environ.get(clean_env_var) else "missing",
        description=description or "default",
        active=active,
        metadata={"env_var": clean_env_var},
    )
    return _provider_auth_action_payload(
        clean_provider_id,
        "env",
        account=account,
        account_created=True,
        account_activated=active,
    )


def connect_provider_local_account(
    store: Any,
    config: Any,
    provider_id: str,
    credential_kind: str,
    *,
    description: str = "default",
    active: bool = True,
    env_var: str | None = None,
) -> dict[str, Any]:
    clean_provider_id = _require_provider(config, provider_id)
    kind = str(credential_kind or "").strip()
    supported_methods = {
        str(method.get("method") or "")
        for method in _supported_auth_methods(config, clean_provider_id)
        if method.get("supported")
    }
    if kind not in {"static_local", "codex_login", "aws_env", "aws_profile"} or kind not in supported_methods:
        raise ProviderCredentialResolutionError(clean_provider_id, "credential_kind_unsupported")
    backend = (getattr(config, "backends", {}) or {}).get(clean_provider_id)
    settings = dict(getattr(backend, "settings", {}) or {}) if backend is not None else {}
    metadata: dict[str, Any] = {}
    status = "configured"
    if kind == "aws_env":
        metadata["env_var"] = "AWS_ACCESS_KEY_ID"
        status = "configured" if os.environ.get("AWS_ACCESS_KEY_ID") else "missing"
    elif kind == "aws_profile":
        profile_env = str(env_var or settings.get("aws_profile_env") or "AWS_PROFILE").strip() or "AWS_PROFILE"
        metadata["env_var"] = profile_env
        if settings.get("aws_profile"):
            metadata["profile"] = str(settings.get("aws_profile"))
        status = "configured" if settings.get("aws_profile") or os.environ.get(profile_env) else "missing"
    account = store.create_provider_account(
        provider_id=clean_provider_id,
        credential_kind=kind,
        status=status,
        description=description or "default",
        active=active,
        metadata=metadata,
    )
    return _provider_auth_action_payload(
        clean_provider_id,
        kind,
        account=account,
        account_created=True,
        account_activated=active,
    )


def activate_provider_auth_account(store: Any, config: Any, provider_id: str, account_id: str) -> dict[str, Any]:
    clean_provider_id = _require_provider(config, provider_id)
    clean_account_id = str(account_id or "").strip()
    if not clean_account_id:
        raise ValueError("Missing provider account id.")
    account = store.activate_provider_account(clean_provider_id, clean_account_id)
    return _provider_auth_action_payload(
        clean_provider_id,
        "activate",
        account=account,
        account_activated=True,
    )


def disconnect_provider_auth(store: Any, config: Any, provider_id: str) -> dict[str, Any]:
    clean_provider_id = _require_provider(config, provider_id)
    removed = []
    for account in list(store.list_provider_accounts(clean_provider_id)):
        removed.append(store.remove_provider_account(account["account_id"]))
    return _provider_auth_action_payload(
        clean_provider_id,
        "delete",
        account=None,
        removed_accounts=removed,
        account_deleted=bool(removed),
        credential_removed=any(bool(account.get("credential_removed")) for account in removed),
    )


def provider_oauth_authorize(config: Any, provider_id: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    clean_provider_id = _require_provider(config, provider_id)
    if not _oauth_supported(config, clean_provider_id):
        return _provider_oauth_unsupported(clean_provider_id, "authorize", body or {})
    pkce = generate_pkce_pair()
    requested_scopes = _oauth_scopes(body or {})
    callback_route = f"/provider/{clean_provider_id}/oauth/callback"
    return {
        "schema_version": "harness.provider_oauth_action/v1",
        "ok": True,
        "provider_id": clean_provider_id,
        "action": "authorize",
        "oauth_supported": True,
        "method": "manual_code",
        "manual_code_required": True,
        "authorization_url": _oauth_authorization_url(config, clean_provider_id),
        "callback_route": callback_route,
        "callback_fields": ["access_token", "refresh_token", "expires_at", "scopes"],
        "scopes": requested_scopes,
        "pkce": {
            "schema_version": "harness.provider_oauth_pkce/v1",
            "code_challenge_method": "S256",
            "code_challenge": pkce["code_challenge"],
            "code_verifier_included": False,
            "code_verifier_secret_ref": "manual_entry_only_not_persisted",
            "state": pkce["state"],
        },
        "credentials_included": False,
        "credential_value_included": False,
        "browser_opened": False,
        "network_called": False,
        "credentials_stored": False,
        "filesystem_modified": False,
        "provider_execution_started": False,
        "model_execution_started": False,
        "permission_granting": False,
        "authority_granting": False,
        "no_hidden_fallback": True,
    }


def provider_oauth_callback(
    project_root: Path,
    store: Any,
    config: Any,
    provider_id: str,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = body or {}
    clean_provider_id = _require_provider(config, provider_id)
    if not _oauth_supported(config, clean_provider_id):
        return _provider_oauth_unsupported(clean_provider_id, "callback", payload)
    access_token = str(payload.get("access_token") or "").strip()
    refresh_token = str(payload.get("refresh_token") or "").strip()
    if not access_token and not refresh_token:
        raise ProviderCredentialResolutionError(clean_provider_id, "credential_missing", "OAuth callback requires access_token or refresh_token.")
    expires_at = _oauth_expires_at(payload)
    scopes = _oauth_scopes(payload)
    account = store.create_provider_account(
        provider_id=clean_provider_id,
        credential_kind="oauth",
        status="configured" if access_token else "refresh_required",
        description=str(payload.get("description") or "oauth_manual_code").strip() or "oauth_manual_code",
        active=bool(payload.get("active", True)),
        expires_at=expires_at,
        metadata={
            "oauth_method": "manual_code",
            "access_secret_ref": "provider_secret_store:access_token" if access_token else None,
            "refresh_secret_ref": "provider_secret_store:refresh_token" if refresh_token else None,
            "scopes": scopes,
            "token_type": str(payload.get("token_type") or "Bearer"),
        },
    )
    token_write = write_provider_oauth_tokens(
        Path(project_root),
        account,
        access_token=access_token or None,
        refresh_token=refresh_token or None,
    )
    return _provider_auth_action_payload(
        clean_provider_id,
        "oauth_callback",
        account=account,
        secret_write=token_write,
        account_created=True,
        account_activated=bool(payload.get("active", True)),
        credential_written=True,
    )


def generate_pkce_pair() -> dict[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).decode("ascii").rstrip("=")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return {
        "schema_version": "harness.provider_oauth_pkce_pair/v1",
        "code_verifier": verifier,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": secrets.token_urlsafe(24),
    }


def resolve_provider_credential(
    config: Any,
    provider: Any,
    store: Any | None = None,
    *,
    allow_secret_material: bool,
) -> ResolvedProviderCredential:
    provider_id = str(getattr(provider, "provider_id", "") or "").strip()
    backend_id = str(getattr(provider, "backend_id", "") or provider_id).strip()
    backends = getattr(config, "backends", {}) or {}
    backend = backends.get(backend_id) or backends.get(provider_id)
    if not provider_id or backend is None:
        raise ProviderCredentialResolutionError(provider_id or None, "provider_unknown")

    settings = dict(getattr(backend, "settings", {}) or {})
    account = _active_account(store, provider_id)
    descriptor = getattr(provider, "credential", None)
    if account is None and getattr(descriptor, "source", None) == "provider_account":
        account = _account_from_descriptor(descriptor, provider_id)

    if account is not None:
        credential = _credential_from_account(provider_id, account, allow_secret_material=allow_secret_material)
    else:
        credential = _credential_from_settings(provider_id, provider, settings, allow_secret_material=allow_secret_material)
    if allow_secret_material and credential.credential_kind == "static_local" and not credential.api_key:
        if _is_local_provider(provider):
            credential = credential.model_copy(update={"api_key": "local", "credential_value_included": True, "credentials_included": True, "redaction_state": "restricted"})
        else:
            raise ProviderCredentialResolutionError(provider_id, "credential_source_unavailable")

    header_refs = settings.get("header_env_refs") if isinstance(settings.get("header_env_refs"), dict) else {}
    resolved_headers = _resolve_header_env_refs(provider_id, header_refs, allow_secret_material=allow_secret_material)
    headers = {**credential.headers, **resolved_headers}
    status = credential.status
    if header_refs and len(resolved_headers) != len(header_refs):
        status = "missing"
    if allow_secret_material and status in {"missing", "expired", "refresh_required", "unsupported"}:
        reason = {
            "missing": "credential_missing",
            "expired": "credential_expired",
            "refresh_required": "credential_refresh_required",
            "unsupported": "credential_kind_unsupported",
        }[status]
        raise ProviderCredentialResolutionError(provider_id, reason)
    return credential.model_copy(update={"status": status, "headers": headers}, deep=True)


def provider_secret_store_path(project_root: Path) -> Path:
    return project_root.resolve() / ".harness" / PROVIDER_SECRET_STORE_FILE


def write_provider_account_secret(project_root: Path, account: dict[str, Any], secret_value: str) -> dict[str, Any]:
    value = str(secret_value or "")
    if not value:
        raise ProviderCredentialResolutionError(str(account.get("provider_id") or None), "credential_missing")
    account_id = str(account.get("account_id") or "").strip()
    provider_id = str(account.get("provider_id") or "").strip()
    if not account_id or not provider_id:
        raise ValueError("Provider account secret requires account_id and provider_id.")
    store_path = provider_secret_store_path(project_root)
    with _provider_secret_store_lock(project_root):
        payload = _read_provider_secret_store_unlocked(store_path)
        payload.setdefault("secrets", {})[account_id] = {
            "provider_id": provider_id,
            "credential_kind": str(account.get("credential_kind") or "api_key"),
            "value": value,
            "updated_at": _now_iso(),
        }
        _write_provider_secret_store_unlocked(store_path, payload)
    return {
        "schema_version": "harness.provider_secret_write/v1",
        "ok": True,
        "provider_id": provider_id,
        "account_id": account_id,
        "secret_store": str(store_path),
        "credential_value_included": False,
        "credentials_included": False,
        "credential_written": True,
        "network_accessed": False,
        "no_hidden_fallback": True,
    }


def write_provider_oauth_tokens(
    project_root: Path,
    account: dict[str, Any],
    *,
    access_token: str | None = None,
    refresh_token: str | None = None,
) -> dict[str, Any]:
    account_id = str(account.get("account_id") or "").strip()
    provider_id = str(account.get("provider_id") or "").strip()
    if not account_id or not provider_id:
        raise ValueError("Provider OAuth token write requires account_id and provider_id.")
    if not access_token and not refresh_token:
        raise ProviderCredentialResolutionError(provider_id, "credential_missing", "OAuth token write requires at least one token.")
    store_path = provider_secret_store_path(project_root)
    with _provider_secret_store_lock(project_root):
        payload = _read_provider_secret_store_unlocked(store_path)
        existing = payload.setdefault("secrets", {}).get(account_id)
        existing_tokens = existing.get("tokens") if isinstance(existing, dict) and isinstance(existing.get("tokens"), dict) else {}
        tokens = dict(existing_tokens)
        if access_token:
            tokens["access_token"] = access_token
        if refresh_token:
            tokens["refresh_token"] = refresh_token
        payload.setdefault("secrets", {})[account_id] = {
            "provider_id": provider_id,
            "credential_kind": "oauth",
            "tokens": tokens,
            "updated_at": _now_iso(),
        }
        _write_provider_secret_store_unlocked(store_path, payload)
    return {
        "schema_version": "harness.provider_oauth_token_write/v1",
        "ok": True,
        "provider_id": provider_id,
        "account_id": account_id,
        "secret_store": str(store_path),
        "access_token_written": bool(access_token),
        "refresh_token_written": bool(refresh_token),
        "credential_value_included": False,
        "credentials_included": False,
        "credential_written": True,
        "network_accessed": False,
        "no_hidden_fallback": True,
    }


def read_provider_account_secret(project_root: Path, account: dict[str, Any]) -> str | None:
    account_id = str(account.get("account_id") or "").strip()
    if not account_id:
        return None
    store_path = provider_secret_store_path(project_root)
    with _provider_secret_store_lock(project_root):
        payload = _read_provider_secret_store_unlocked(store_path)
    item = (payload.get("secrets") or {}).get(account_id)
    if not isinstance(item, dict):
        return None
    provider_id = str(account.get("provider_id") or "")
    if provider_id and str(item.get("provider_id") or "") != provider_id:
        return None
    value = item.get("value")
    return value if isinstance(value, str) and value else None


def read_provider_oauth_tokens(project_root: Path, account: dict[str, Any]) -> dict[str, str]:
    account_id = str(account.get("account_id") or "").strip()
    if not account_id:
        return {}
    store_path = provider_secret_store_path(project_root)
    with _provider_secret_store_lock(project_root):
        payload = _read_provider_secret_store_unlocked(store_path)
    item = (payload.get("secrets") or {}).get(account_id)
    if not isinstance(item, dict):
        return {}
    provider_id = str(account.get("provider_id") or "")
    if provider_id and str(item.get("provider_id") or "") != provider_id:
        return {}
    tokens = item.get("tokens")
    if not isinstance(tokens, dict):
        return {}
    return {key: value for key, value in tokens.items() if key in {"access_token", "refresh_token"} and isinstance(value, str) and value}


def delete_provider_account_secret(project_root: Path, account_id: str) -> bool:
    clean_id = str(account_id or "").strip()
    if not clean_id:
        return False
    store_path = provider_secret_store_path(project_root)
    with _provider_secret_store_lock(project_root):
        payload = _read_provider_secret_store_unlocked(store_path)
        secrets = payload.setdefault("secrets", {})
        removed = clean_id in secrets
        secrets.pop(clean_id, None)
        _write_provider_secret_store_unlocked(store_path, payload)
    return removed


def _provider_accounts(store: Any | None) -> list[dict[str, Any]]:
    if store is None or not hasattr(store, "list_provider_accounts"):
        return []
    try:
        accounts = store.list_provider_accounts()
    except Exception:
        return []
    return [account for account in accounts if isinstance(account, dict)]


def _require_provider(config: Any, provider_id: str) -> str:
    clean_provider_id = str(provider_id or "").strip()
    backends = getattr(config, "backends", {}) or {}
    if not clean_provider_id or clean_provider_id not in backends:
        raise ProviderCredentialResolutionError(clean_provider_id or None, "provider_unknown")
    return clean_provider_id


def _supported_auth_methods(config: Any, provider_id: str) -> list[dict[str, Any]]:
    backends = getattr(config, "backends", {}) or {}
    backend = backends.get(provider_id)
    settings = dict(getattr(backend, "settings", {}) or {}) if backend is not None else {}
    kind = str(settings.get("credential_kind") or "").strip()
    backend_kind = str(getattr(getattr(backend, "kind", None), "value", getattr(backend, "kind", "")) or "")
    is_external_agent = backend_kind == "external_agent" or provider_id == "codex_cli" or "auth_mode" in settings
    is_aws = provider_id == "bedrock" or kind in {"aws_profile", "aws_env"}
    oauth_supported = _oauth_supported(config, provider_id)
    has_env_default = bool(str(settings.get("api_key_env") or "").strip()) or kind == "env"
    has_static_local = "api_key" in settings
    native_key_provider = not is_external_agent and not is_aws
    methods = [
        _auth_method(
            "api_key",
            supported=native_key_provider,
            prompt_keys=["api_key"],
            secret_value_required=True,
            description="Store an API key in the local Harness provider secret store.",
        ),
        _auth_method(
            "env",
            supported=native_key_provider or has_env_default,
            prompt_keys=["env_var"],
            default_env_var=str(settings.get("api_key_env") or settings.get("credential_env") or "").strip() or None,
            description="Bind this provider to an environment variable resolved only at runtime.",
        ),
        _auth_method(
            "oauth",
            supported=oauth_supported,
            prompt_keys=["authorization_code", "access_token", "refresh_token", "expires_at", "scopes"],
            description="Use a manual-code OAuth callback that stores tokens in the local provider secret store.",
            blocked_reason=None if oauth_supported else "oauth_provider_not_supported",
        ),
        _auth_method(
            "aws_profile",
            supported=is_aws,
            prompt_keys=["profile_env", "profile"],
            default_env_var=str(settings.get("aws_profile_env") or "AWS_PROFILE") if is_aws else None,
            description="Use an AWS profile or AWS_PROFILE-style environment binding.",
        ),
        _auth_method(
            "aws_env",
            supported=is_aws,
            prompt_keys=["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"],
            default_env_var="AWS_ACCESS_KEY_ID" if is_aws else None,
            description="Use AWS credential environment variables resolved only at runtime.",
        ),
        _auth_method(
            "codex_login",
            supported=is_external_agent,
            prompt_keys=["codex_cli_session"],
            description="Use the local Codex CLI login/session boundary.",
        ),
        _auth_method(
            "static_local",
            supported=has_static_local and not is_external_agent,
            prompt_keys=[],
            description="Use a local-only configured placeholder credential.",
        ),
    ]
    return methods


def _auth_method(
    method: str,
    *,
    supported: bool,
    prompt_keys: list[str],
    description: str,
    secret_value_required: bool = False,
    default_env_var: str | None = None,
    blocked_reason: str | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": "harness.provider_auth_method/v1",
        "method": method,
        "supported": supported,
        "enabled": supported,
        "description": description,
        "prompt_keys": prompt_keys,
        "metadata_prompts": prompt_keys,
        "default_env_var": default_env_var,
        "secret_value_required": secret_value_required,
        "credential_value_included": False,
        "credentials_included": False,
        "oauth_supported": method == "oauth" and supported,
        "blocked_reason": blocked_reason,
        "provider_execution_started": False,
        "model_execution_started": False,
        "network_accessed": False,
        "permission_granting": False,
        "no_hidden_fallback": True,
    }


def _provider_auth_action_payload(
    provider_id: str,
    action: str,
    *,
    account: dict[str, Any] | None,
    secret_write: dict[str, Any] | None = None,
    removed_accounts: list[dict[str, Any]] | None = None,
    account_created: bool = False,
    account_activated: bool = False,
    account_deleted: bool = False,
    credential_written: bool = False,
    credential_removed: bool = False,
) -> dict[str, Any]:
    return {
        "schema_version": PROVIDER_AUTH_ACTION_SCHEMA_VERSION,
        "ok": True,
        "provider_id": provider_id,
        "action": action,
        "account": account,
        "account_id": account.get("account_id") if account else None,
        "removed_accounts": removed_accounts or [],
        "secret_write": secret_write,
        "account_created": account_created,
        "account_activated": account_activated,
        "account_deleted": account_deleted,
        "credential_source": _credential_source_for_action(action, account),
        "credential_kind": account.get("credential_kind") if account else action,
        "credential_written": credential_written,
        "credential_removed": credential_removed,
        "credential_value_included": False,
        "credentials_included": False,
        "provider_execution_started": False,
        "model_execution_started": False,
        "network_accessed": False,
        "browser_opened": False,
        "active_model_changed": False,
        "permission_granting": False,
        "authority_granting": False,
        "no_hidden_fallback": True,
    }


def _credential_source_for_action(action: str, account: dict[str, Any] | None) -> str:
    if action == "api_key":
        return "provider_account_secret_store"
    if action == "env":
        return "provider_account_env"
    if action == "oauth_callback":
        return "provider_account_oauth_secret_store"
    if account:
        return f"provider_account_{account.get('credential_kind') or 'unknown'}"
    return "provider_account"


def _active_account(store: Any | None, provider_id: str) -> dict[str, Any] | None:
    if store is None or not hasattr(store, "active_provider_account"):
        return None
    account = store.active_provider_account(provider_id)
    if not isinstance(account, dict):
        return None
    project_root = getattr(store, "project_root", None)
    if isinstance(project_root, Path):
        return {**account, "_secret_project_root": project_root, "_secret_store_obj": store}
    return account


def _account_from_descriptor(descriptor: Any, provider_id: str) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    env_var = getattr(descriptor, "env_var", None)
    if env_var:
        metadata["env_var"] = env_var
    status = getattr(descriptor, "status", "unknown")
    return {
        "provider_id": provider_id,
        "credential_kind": getattr(descriptor, "kind", "api_key"),
        "status": getattr(status, "value", status),
        "account_id": getattr(descriptor, "account_id", None),
        "expires_at": None,
        "metadata": metadata,
    }


def _credential_from_account(
    provider_id: str,
    account: dict[str, Any],
    *,
    allow_secret_material: bool,
) -> ResolvedProviderCredential:
    metadata = account.get("metadata") if isinstance(account.get("metadata"), dict) else {}
    kind = str(account.get("credential_kind") or "api_key")
    status = str(account.get("status") or "unknown")
    env_var = str(metadata.get("env_var") or "") or None
    if kind == "oauth":
        return _oauth_credential_from_account(provider_id, account, allow_secret_material=allow_secret_material)
    if status == "expired":
        if allow_secret_material:
            raise ProviderCredentialResolutionError(provider_id, "credential_expired")
        return _credential(provider_id, kind, "expired", "provider_account", env_var=env_var, account=account)
    if kind == "env" and env_var:
        if not allow_secret_material:
            return _credential(provider_id, kind, status, "provider_account", env_var=env_var, account=account)
        return _env_credential(provider_id, kind, env_var, source="provider_account", account=account, allow_secret_material=allow_secret_material)
    if kind == "api_key":
        if allow_secret_material:
            secret = _read_secret_for_account(provider_id, account)
            if secret:
                return _credential(
                    provider_id,
                    kind,
                    "configured",
                    "provider_account_secret_store",
                    account=account,
                    api_key=secret,
                    credentials_included=True,
                )
            raise ProviderCredentialResolutionError(provider_id, "credential_missing")
        return _credential(provider_id, kind, status, "provider_account", account=account)
    if kind in {"static_local", "codex_login", "aws_env", "aws_profile", "none"}:
        return _credential(provider_id, kind, status, "provider_account", env_var=env_var, account=account)
    if allow_secret_material:
        raise ProviderCredentialResolutionError(provider_id, "credential_kind_unsupported")
    return _credential(provider_id, kind, "unsupported", "provider_account", account=account)


def _credential_from_settings(
    provider_id: str,
    provider: Any,
    settings: dict[str, Any],
    *,
    allow_secret_material: bool,
) -> ResolvedProviderCredential:
    api_key_env = settings.get("api_key_env")
    if isinstance(api_key_env, str) and api_key_env.strip():
        return _env_credential(provider_id, "env", api_key_env.strip(), source="env", account=None, allow_secret_material=allow_secret_material)
    if "auth_mode" in settings:
        return _credential(provider_id, "codex_login", "configured", "config")
    if "api_key" in settings:
        value = str(settings.get("api_key") or "")
        include = allow_secret_material and _is_local_provider(provider)
        return _credential(
            provider_id,
            "static_local",
            "configured" if value else "missing",
            "static_local",
            api_key=value if include else None,
            credentials_included=include,
        )
    kind = str(settings.get("credential_kind") or "none")
    if kind == "aws_profile":
        profile_env = str(settings.get("aws_profile_env") or "AWS_PROFILE")
        configured = bool(settings.get("aws_profile") or os.environ.get(profile_env))
        return _credential(provider_id, kind, "configured" if configured else "missing", "aws_profile", env_var=profile_env)
    if kind == "aws_env":
        configured = bool(os.environ.get("AWS_ACCESS_KEY_ID"))
        return _credential(provider_id, kind, "configured" if configured else "missing", "aws_env", env_var="AWS_ACCESS_KEY_ID")
    if kind == "env":
        env_var = str(settings.get("credential_env") or "").strip()
        if not env_var:
            return _credential(provider_id, kind, "missing", "custom_config")
        return _env_credential(provider_id, kind, env_var, source="custom_config", account=None, allow_secret_material=allow_secret_material)
    if kind == "static_local":
        placeholder = "local" if _is_local_provider(provider) else ""
        return _credential(
            provider_id,
            kind,
            "configured" if placeholder else "missing",
            "static_local",
            api_key=placeholder if allow_secret_material and placeholder else None,
            credentials_included=allow_secret_material and bool(placeholder),
        )
    if kind == "none":
        return _credential(provider_id, kind, "not_required", "config", redaction_state="not_required")
    if kind == "oauth":
        if allow_secret_material:
            raise ProviderCredentialResolutionError(provider_id, "credential_refresh_required")
        return _credential(provider_id, kind, "refresh_required", "custom_config")
    if allow_secret_material:
        raise ProviderCredentialResolutionError(provider_id, "credential_kind_unsupported")
    return _credential(provider_id, kind, "unsupported", "custom_config")


def _env_credential(
    provider_id: str,
    kind: str,
    env_var: str,
    *,
    source: str,
    account: dict[str, Any] | None,
    allow_secret_material: bool,
) -> ResolvedProviderCredential:
    value = os.environ.get(env_var)
    if allow_secret_material and not value:
        raise ProviderCredentialResolutionError(provider_id, "credential_missing")
    return _credential(
        provider_id,
        kind,
        "configured" if value else "missing",
        source,
        env_var=env_var,
        account=account,
        api_key=value if allow_secret_material else None,
        credentials_included=allow_secret_material and bool(value),
    )


def _oauth_credential_from_account(
    provider_id: str,
    account: dict[str, Any],
    *,
    allow_secret_material: bool,
) -> ResolvedProviderCredential:
    account_status = str(account.get("status") or "unknown")
    expires_at = str(account.get("expires_at") or "") or None
    projected_status = "expired" if _oauth_token_expired(expires_at) else account_status
    if not allow_secret_material:
        return _credential(provider_id, "oauth", projected_status, "provider_account", account=account)
    project_root = _account_project_root(account)
    if project_root is None:
        raise ProviderCredentialResolutionError(provider_id, "credential_source_unavailable")
    tokens = read_provider_oauth_tokens(project_root, account)
    access_token = tokens.get("access_token")
    if access_token and not _oauth_token_expired(expires_at):
        return _credential(
            provider_id,
            "oauth",
            "configured",
            "provider_account_oauth_access_token",
            account=account,
            api_key=access_token,
            credentials_included=True,
        )
    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        _record_provider_oauth_refresh_event(account, ok=False, reason="refresh_token_missing", network_accessed=False)
        raise ProviderCredentialResolutionError(provider_id, "credential_refresh_required")
    with _provider_oauth_refresh_lock(project_root):
        try:
            refreshed = refresh_provider_oauth_account(project_root, provider_id, account, refresh_token)
        except ProviderCredentialResolutionError as exc:
            _record_provider_oauth_refresh_event(account, ok=False, reason=exc.reason, network_accessed=False)
            raise
        refreshed_access_token = str(refreshed.get("access_token") or "").strip()
        refreshed_refresh_token = str(refreshed.get("refresh_token") or refresh_token).strip()
        if not refreshed_access_token:
            _record_provider_oauth_refresh_event(account, ok=False, reason="access_token_missing", network_accessed=bool(refreshed.get("network_accessed")))
            raise ProviderCredentialResolutionError(provider_id, "credential_refresh_failed")
        write_provider_oauth_tokens(
            project_root,
            account,
            access_token=refreshed_access_token,
            refresh_token=refreshed_refresh_token,
        )
        _record_provider_oauth_refresh_event(account, ok=True, reason=None, network_accessed=bool(refreshed.get("network_accessed", True)))
        return _credential(
            provider_id,
            "oauth",
            "configured",
            "provider_account_oauth_refreshed",
            account={**account, "expires_at": refreshed.get("expires_at") or expires_at},
            api_key=refreshed_access_token,
            credentials_included=True,
        )


def refresh_provider_oauth_account(
    project_root: Path,
    provider_id: str,
    account: dict[str, Any],
    refresh_token: str,
) -> dict[str, Any]:
    raise ProviderCredentialResolutionError(
        provider_id,
        "credential_refresh_required",
        "OAuth refresh is not configured for this provider.",
    )


def _resolve_header_env_refs(
    provider_id: str,
    header_refs: dict[str, Any],
    *,
    allow_secret_material: bool,
) -> dict[str, str]:
    headers: dict[str, str] = {}
    for header, env_var in header_refs.items():
        header_name = str(header or "").strip()
        env_name = str(env_var or "").strip()
        if not header_name or not env_name:
            continue
        value = os.environ.get(env_name)
        if allow_secret_material and not value:
            raise ProviderCredentialResolutionError(provider_id, "credential_missing")
        if allow_secret_material and value:
            headers[header_name] = value
        elif value:
            headers[header_name] = "[redacted]"
    return headers


def _read_secret_for_account(provider_id: str, account: dict[str, Any]) -> str | None:
    store = account.get("_secret_project_root")
    if isinstance(store, Path):
        return read_provider_account_secret(store, account)
    project_root = account.get("project_root")
    if isinstance(project_root, str) and project_root.strip():
        return read_provider_account_secret(Path(project_root), account)
    return None


def _account_project_root(account: dict[str, Any]) -> Path | None:
    store = account.get("_secret_project_root")
    if isinstance(store, Path):
        return store
    project_root = account.get("project_root")
    if isinstance(project_root, str) and project_root.strip():
        return Path(project_root)
    return None


def _oauth_supported(config: Any, provider_id: str) -> bool:
    backends = getattr(config, "backends", {}) or {}
    backend = backends.get(provider_id)
    settings = dict(getattr(backend, "settings", {}) or {}) if backend is not None else {}
    return provider_id == "paid_openai_compatible" or bool(settings.get("oauth_authorization_url"))


def _oauth_authorization_url(config: Any, provider_id: str) -> str:
    backends = getattr(config, "backends", {}) or {}
    backend = backends.get(provider_id)
    settings = dict(getattr(backend, "settings", {}) or {}) if backend is not None else {}
    configured = str(settings.get("oauth_authorization_url") or "").strip()
    if configured:
        return configured
    return f"manual-code://{provider_id}/authorize"


def _oauth_scopes(body: dict[str, Any]) -> list[str]:
    raw = body.get("scopes") if "scopes" in body else body.get("scope")
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    if isinstance(raw, str):
        return [item.strip() for item in raw.replace(",", " ").split() if item.strip()]
    return []


def _oauth_expires_at(body: dict[str, Any]) -> str | None:
    expires_at = str(body.get("expires_at") or "").strip()
    if expires_at:
        return expires_at
    expires_in = body.get("expires_in")
    if expires_in in {None, ""}:
        return None
    try:
        seconds = int(expires_in)
    except (TypeError, ValueError) as exc:
        raise ValueError("OAuth expires_in must be an integer number of seconds.") from exc
    return (datetime.now(timezone.utc) + timedelta(seconds=max(0, seconds))).isoformat()


def _oauth_token_expired(expires_at: str | None) -> bool:
    if not expires_at:
        return False
    try:
        parsed = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
    except ValueError:
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed <= datetime.now(timezone.utc) + timedelta(seconds=30)


def _provider_oauth_unsupported(provider_id: str, action: str, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "harness.provider_oauth_action/v1",
        "ok": False,
        "provider_id": provider_id,
        "action": action,
        "requested": _redacted_oauth_request(body),
        "error": f"Provider OAuth {action} is not supported for {provider_id}; refusing to open browser, call network, or store credentials.",
        "oauth_supported": False,
        "browser_opened": False,
        "network_called": False,
        "credentials_stored": False,
        "credential_value_included": False,
        "credentials_included": False,
        "filesystem_modified": False,
        "provider_execution_started": False,
        "model_execution_started": False,
        "permission_granting": False,
        "authority_granting": False,
        "no_hidden_fallback": True,
    }


def _redacted_oauth_request(body: dict[str, Any]) -> dict[str, Any]:
    redacted: dict[str, Any] = {}
    for key, value in (body or {}).items():
        key_text = str(key)
        if any(word in key_text.lower() for word in ("token", "code", "secret", "verifier")):
            redacted[key_text] = "[redacted]"
        else:
            redacted[key_text] = value
    return redacted


def _record_provider_oauth_refresh_event(
    account: dict[str, Any],
    *,
    ok: bool,
    reason: str | None,
    network_accessed: bool,
) -> None:
    store = account.get("_secret_store_obj")
    if store is None or not hasattr(store, "append_store_event"):
        return
    try:
        store.append_store_event(
            "orchestration",
            "provider_accounts",
            "provider.oauth_token_refreshed" if ok else "provider.oauth_token_refresh_failed",
            {
                "schema_version": "harness.provider_oauth_refresh_event/v1",
                "provider_id": account.get("provider_id"),
                "account_id": account.get("account_id"),
                "ok": ok,
                "reason": reason,
                "credential_value_included": False,
                "credentials_included": False,
                "credential_written": ok,
                "network_accessed": network_accessed,
                "provider_execution_started": False,
                "model_execution_started": False,
                "permission_granting": False,
                "authority_granting": False,
                "no_hidden_fallback": True,
            },
            redaction_state="redacted",
        )
    except Exception:
        return


def _credential(
    provider_id: str,
    kind: str,
    status: str,
    source: str,
    *,
    env_var: str | None = None,
    account: dict[str, Any] | None = None,
    api_key: str | None = None,
    headers: dict[str, str] | None = None,
    credentials_included: bool = False,
    redaction_state: str = "redacted",
) -> ResolvedProviderCredential:
    return ResolvedProviderCredential(
        provider_id=provider_id,
        credential_kind=kind,
        status=status,
        source=source,
        env_var=env_var,
        account_id=str(account.get("account_id") or "") or None if account else None,
        expires_at=str(account.get("expires_at") or "") or None if account else None,
        headers=headers or {},
        api_key=api_key,
        redaction_state=redaction_state if not credentials_included else "restricted",
        credential_value_included=credentials_included,
        credentials_included=credentials_included,
    )


def _is_local_provider(provider: Any) -> bool:
    metadata = getattr(provider, "metadata", None)
    boundary = getattr(getattr(metadata, "data_boundary", None), "value", None) or getattr(metadata, "data_boundary", None)
    return str(boundary) == "local_only"


class _provider_secret_store_lock:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root.resolve()
        self.lock_path = self.project_root / ".harness" / PROVIDER_SECRET_LOCK_FILE
        self.handle: Any = None

    def __enter__(self) -> "_provider_secret_store_lock":
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.lock_path.open("a+", encoding="utf-8")
        os.chmod(self.lock_path, 0o600)
        fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self.handle is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()


class _provider_oauth_refresh_lock:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root.resolve()
        self.lock_path = self.project_root / ".harness" / PROVIDER_OAUTH_REFRESH_LOCK_FILE
        self.handle: Any = None

    def __enter__(self) -> "_provider_oauth_refresh_lock":
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.lock_path.open("a+", encoding="utf-8")
        os.chmod(self.lock_path, 0o600)
        fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self.handle is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()


def _read_provider_secret_store_unlocked(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema_version": PROVIDER_SECRET_STORE_SCHEMA_VERSION, "secrets": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"schema_version": PROVIDER_SECRET_STORE_SCHEMA_VERSION, "secrets": {}}
    if not isinstance(data, dict):
        return {"schema_version": PROVIDER_SECRET_STORE_SCHEMA_VERSION, "secrets": {}}
    secrets = data.get("secrets")
    if not isinstance(secrets, dict):
        secrets = {}
    return {"schema_version": PROVIDER_SECRET_STORE_SCHEMA_VERSION, "secrets": secrets}


def _write_provider_secret_store_unlocked(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": PROVIDER_SECRET_STORE_SCHEMA_VERSION,
        "secrets": payload.get("secrets") if isinstance(payload.get("secrets"), dict) else {},
    }
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent), text=True)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, path)
        os.chmod(path, 0o600)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
