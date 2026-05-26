from __future__ import annotations

import json
from pathlib import Path
from typing import Any

PLUGIN_PROVIDER_HOOK_SCHEMA_VERSION = "harness.plugin_provider_hook/v1"


def plugin_provider_hook_policy_boundary(scope: str) -> dict[str, Any]:
    return {
        "kind": "plugin_provider_hook_metadata",
        "scope": scope,
        "manifest_metadata_read_allowed": True,
        "runtime_load_allowed": False,
        "provider_registration_allowed": False,
        "provider_execution_allowed": False,
        "model_discovery_allowed": False,
        "network_fetch_allowed": False,
        "credential_resolution_allowed": False,
        "origin_review_required": True,
    }


def read_plugin_provider_hooks_from_manifest(
    *,
    plugin_name: str,
    scope: str,
    manifest_path: Path | None,
    manifest_ref: str | None,
) -> dict[str, Any]:
    if manifest_path is None or not manifest_path.exists() or not manifest_path.is_file():
        return _plugin_provider_hooks_payload(
            plugin_name=plugin_name,
            scope=scope,
            manifest_ref=manifest_ref,
            hooks=[],
            validation_errors=[],
            manifest_read=False,
            manifest_parse_error=None,
        )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        return _plugin_provider_hooks_payload(
            plugin_name=plugin_name,
            scope=scope,
            manifest_ref=manifest_ref,
            hooks=[],
            validation_errors=[f"provider_hook_manifest_unreadable:{plugin_name}"],
            manifest_read=True,
            manifest_parse_error=exc.__class__.__name__,
        )
    hooks, errors = _provider_hooks_from_manifest(plugin_name, manifest)
    return _plugin_provider_hooks_payload(
        plugin_name=plugin_name,
        scope=scope,
        manifest_ref=manifest_ref,
        hooks=hooks,
        validation_errors=errors,
        manifest_read=True,
        manifest_parse_error=None,
    )


def _plugin_provider_hooks_payload(
    *,
    plugin_name: str,
    scope: str,
    manifest_ref: str | None,
    hooks: list[dict[str, Any]],
    validation_errors: list[str],
    manifest_read: bool,
    manifest_parse_error: str | None,
) -> dict[str, Any]:
    blocked_reasons = ["plugin_origin_review_required", "plugin_runtime_load_disabled"]
    if validation_errors:
        blocked_reasons.append("plugin_provider_hook_invalid")
    if not hooks:
        blocked_reasons.append("plugin_provider_hook_not_declared")
    return {
        "schema_version": "harness.plugin_provider_hooks/v1",
        "plugin": plugin_name,
        "scope": scope,
        "manifest_path": manifest_ref,
        "manifest_read": manifest_read,
        "manifest_parse_error": manifest_parse_error,
        "provider_hooks": hooks,
        "provider_hook_count": len(hooks),
        "validation_errors": validation_errors,
        "metadata_only": True,
        "runtime_loaded": False,
        "provider_registered": False,
        "provider_execution_started": False,
        "model_discovery_started": False,
        "network_called": False,
        "credentials_included": False,
        "permission_granting": False,
        "policy_boundary": plugin_provider_hook_policy_boundary(scope),
        "blocked_reasons": blocked_reasons,
    }


def _provider_hooks_from_manifest(plugin_name: str, manifest: Any) -> tuple[list[dict[str, Any]], list[str]]:
    if not isinstance(manifest, dict):
        return [], [f"provider_hook_manifest_must_be_mapping:{plugin_name}"]
    raw_hooks = _manifest_provider_hooks(manifest)
    if raw_hooks is None:
        return [], []
    normalized_items = _normalize_provider_hook_items(raw_hooks)
    hooks: list[dict[str, Any]] = []
    errors: list[str] = []
    if normalized_items is None:
        return [], [f"provider_hooks_must_be_list_or_mapping:{plugin_name}"]
    for fallback_id, raw_hook in normalized_items:
        if not isinstance(raw_hook, dict):
            errors.append(f"provider_hook_must_be_mapping:{plugin_name}:{fallback_id or '<hook>'}")
            continue
        hook, hook_errors = _provider_hook_from_spec(plugin_name, fallback_id, raw_hook)
        hooks.append(hook)
        errors.extend(hook_errors)
    return hooks, errors


def _manifest_provider_hooks(manifest: dict[str, Any]) -> Any:
    harness = manifest.get("harness")
    if isinstance(harness, dict):
        for key in ("provider_hooks", "providers"):
            if key in harness:
                return harness.get(key)
    for key in ("provider_hooks", "providers"):
        if key in manifest:
            return manifest.get(key)
    return None


def _normalize_provider_hook_items(value: Any) -> list[tuple[str | None, Any]] | None:
    if isinstance(value, list):
        return [(None, item) for item in value]
    if isinstance(value, dict):
        return [(str(key), item) for key, item in value.items()]
    return None


def _provider_hook_from_spec(plugin_name: str, fallback_id: str | None, spec: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    provider_id = _clean_string(spec.get("provider_id") or spec.get("id") or fallback_id)
    hook_id = _clean_string(spec.get("hook_id") or provider_id or fallback_id)
    protocol = _clean_string(spec.get("protocol"))
    data_boundary = _clean_string(spec.get("data_boundary"))
    endpoint = _clean_string(spec.get("endpoint") or spec.get("base_url"))
    raw_credential = spec.get("credential")
    credential = raw_credential if isinstance(raw_credential, dict) else {}
    credential_kind = _clean_string(credential.get("kind") if isinstance(credential, dict) else None)
    headers = spec.get("headers", {})
    raw_models = spec.get("models")
    models = _hook_models(raw_models)
    safe_metadata_only = bool(spec.get("safe_metadata_only", False))
    safety_notes = _string_list(spec.get("safety_notes"))
    validation_errors: list[str] = []
    if not provider_id:
        validation_errors.append(f"provider_hook_provider_id_missing:{plugin_name}:{hook_id or '<hook>'}")
    if not protocol:
        validation_errors.append(f"provider_hook_protocol_missing:{plugin_name}:{provider_id or hook_id or '<hook>'}")
    if not data_boundary:
        validation_errors.append(f"provider_hook_data_boundary_missing:{plugin_name}:{provider_id or hook_id or '<hook>'}")
    if not endpoint:
        validation_errors.append(f"provider_hook_endpoint_missing:{plugin_name}:{provider_id or hook_id or '<hook>'}")
    if "credential" not in spec:
        validation_errors.append(f"provider_hook_credential_missing:{plugin_name}:{provider_id or hook_id or '<hook>'}")
    elif not isinstance(raw_credential, dict):
        validation_errors.append(f"provider_hook_credential_must_be_mapping:{plugin_name}:{provider_id or hook_id or '<hook>'}")
    elif not credential_kind:
        validation_errors.append(f"provider_hook_credential_kind_missing:{plugin_name}:{provider_id or hook_id or '<hook>'}")
    elif credential_kind == "env" and not _clean_string(credential.get("env_var")):
        validation_errors.append(f"provider_hook_credential_env_var_missing:{plugin_name}:{provider_id or hook_id or '<hook>'}")
    if isinstance(raw_credential, dict):
        for secret_key in ("value", "api_key", "token", "secret", "authorization"):
            if secret_key in raw_credential:
                validation_errors.append(
                    f"provider_hook_credential_value_not_allowed:{plugin_name}:{provider_id or hook_id or '<hook>'}:{secret_key}"
                )
    if headers is not None and not isinstance(headers, dict):
        validation_errors.append(f"provider_hook_headers_must_be_mapping:{plugin_name}:{provider_id or hook_id or '<hook>'}")
    elif isinstance(headers, dict):
        for header, value in headers.items():
            if not isinstance(value, dict) or _clean_string(value.get("kind")) != "env" or not _clean_string(value.get("env_var")):
                validation_errors.append(
                    f"provider_hook_header_must_use_env_ref:{plugin_name}:{provider_id or hook_id or '<hook>'}:{header}"
                )
    if raw_models is None:
        validation_errors.append(f"provider_hook_models_missing:{plugin_name}:{provider_id or hook_id or '<hook>'}")
    if raw_models is not None and models is None:
        validation_errors.append(f"provider_hook_models_must_be_list_or_mapping:{plugin_name}:{provider_id or hook_id or '<hook>'}")
        models = []
    models = models or []
    if raw_models is not None and not models:
        validation_errors.append(f"provider_hook_model_list_empty:{plugin_name}:{provider_id or hook_id or '<hook>'}")
    if not safety_notes:
        validation_errors.append(f"provider_hook_safety_notes_missing:{plugin_name}:{provider_id or hook_id or '<hook>'}")
    hook = {
        "schema_version": PLUGIN_PROVIDER_HOOK_SCHEMA_VERSION,
        "plugin": plugin_name,
        "hook_id": hook_id,
        "provider_id": provider_id,
        "display_name": _clean_string(spec.get("display_name") or spec.get("name")),
        "protocol": protocol,
        "data_boundary": data_boundary,
        "endpoint_configured": bool(endpoint),
        "credential_kind": credential_kind or "none",
        "model_count": len(models),
        "models": models,
        "safe_metadata_only": safe_metadata_only,
        "safety_notes": safety_notes,
        "metadata_only": True,
        "runtime_load_required": True,
        "runtime_loaded": False,
        "provider_registered": False,
        "provider_execution_started": False,
        "model_discovery_started": False,
        "network_called": False,
        "credentials_included": False,
        "permission_granting": False,
        "blocked_reasons": ["plugin_origin_review_required", "plugin_runtime_load_disabled"],
    }
    if validation_errors:
        hook["blocked_reasons"] = [*hook["blocked_reasons"], "plugin_provider_hook_invalid"]
    return hook, validation_errors


def _hook_models(value: Any) -> list[dict[str, Any]] | None:
    if value is None:
        return []
    if isinstance(value, list):
        result: list[dict[str, Any]] = []
        for item in value:
            if isinstance(item, str) and item.strip():
                result.append({"model_id": item.strip()})
            elif isinstance(item, dict):
                model_id = _clean_string(item.get("model_id") or item.get("id"))
                if model_id:
                    result.append(
                        {
                            "model_id": model_id,
                            "display_name": _clean_string(item.get("display_name") or item.get("name")),
                            "api_id_configured": bool(_clean_string(item.get("api_id"))),
                        }
                    )
        return result
    if isinstance(value, dict):
        return [
            {
                "model_id": str(model_id),
                "display_name": _clean_string(spec.get("display_name") or spec.get("name")) if isinstance(spec, dict) else None,
                "api_id_configured": bool(_clean_string(spec.get("api_id"))) if isinstance(spec, dict) else False,
            }
            for model_id, spec in value.items()
            if isinstance(model_id, str) and model_id.strip()
        ]
    return None


def _clean_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]
