from __future__ import annotations

import json

import pytest

from harness import model_registry
from harness.config import default_config
from harness.model_registry import (
    ModelResolutionError,
    ProviderCredentialStatus,
    build_model_descriptors,
    build_provider_descriptors,
    load_builtin_model_aliases,
    load_builtin_model_metadata,
    load_builtin_provider_metadata,
    load_generated_static_model_catalog,
    parse_model_ref,
    resolve_model_selection,
)


def test_provider_descriptors_are_metadata_only_and_redact_credentials() -> None:
    cfg = default_config()
    providers = build_provider_descriptors(cfg)
    by_id = {provider.provider_id: provider for provider in providers}

    assert set(by_id) == {"anthropic", "bedrock", "codex_cli", "google", "local_openai_compatible", "paid_openai_compatible"}
    assert by_id["codex_cli"].schema_version == "harness.provider_descriptor/v1"
    assert by_id["codex_cli"].display_name == "Codex CLI"
    assert by_id["codex_cli"].backend_id == "codex_cli"
    assert by_id["codex_cli"].metadata_source == "builtin_metadata"
    assert by_id["codex_cli"].enabled is True
    assert by_id["codex_cli"].credential is not None
    assert by_id["codex_cli"].credential.kind == "codex_login"
    assert by_id["codex_cli"].credential.status == ProviderCredentialStatus.CONFIGURED
    assert by_id["local_openai_compatible"].endpoint == "http://localhost:11434/v1"
    assert by_id["local_openai_compatible"].display_name == "Local OpenAI-Compatible"
    assert by_id["local_openai_compatible"].credential is not None
    assert by_id["local_openai_compatible"].credential.kind == "static_local"
    assert by_id["paid_openai_compatible"].enabled is False
    assert by_id["paid_openai_compatible"].display_name == "Paid OpenAI-Compatible"
    assert by_id["paid_openai_compatible"].credential is not None
    assert by_id["paid_openai_compatible"].credential.kind == "env"
    assert by_id["paid_openai_compatible"].credential.env_var == "OPENAI_API_KEY"
    assert by_id["paid_openai_compatible"].credential.status == ProviderCredentialStatus.MISSING
    assert "disabled_by_config" in by_id["paid_openai_compatible"].constraints
    assert by_id["anthropic"].enabled is False
    assert by_id["anthropic"].display_name == "Anthropic"
    assert by_id["anthropic"].credential is not None
    assert by_id["anthropic"].credential.kind == "env"
    assert by_id["anthropic"].credential.env_var == "ANTHROPIC_API_KEY"
    assert by_id["anthropic"].credential.status == ProviderCredentialStatus.MISSING
    assert "disabled_by_config" in by_id["anthropic"].constraints
    assert by_id["google"].enabled is False
    assert by_id["google"].display_name == "Google Generative AI"
    assert by_id["google"].credential is not None
    assert by_id["google"].credential.kind == "env"
    assert by_id["google"].credential.env_var == "GOOGLE_API_KEY"
    assert by_id["google"].credential.status == ProviderCredentialStatus.MISSING
    assert "disabled_by_config" in by_id["google"].constraints
    assert by_id["bedrock"].enabled is False
    assert by_id["bedrock"].display_name == "Amazon Bedrock"
    assert by_id["bedrock"].credential is not None
    assert by_id["bedrock"].credential.kind == "aws_profile"
    assert by_id["bedrock"].credential.env_var == "AWS_PROFILE"
    assert by_id["bedrock"].credential.status == ProviderCredentialStatus.MISSING
    assert "disabled_by_config" in by_id["bedrock"].constraints

    for provider in providers:
        assert provider.metadata_only is True
        assert provider.provider_execution_started is False
        assert provider.model_execution_started is False
        assert provider.network_accessed is False
        assert provider.credentials_included is False
        assert provider.hidden_provider_fallback is False
        assert provider.hidden_model_fallback is False
        assert provider.no_hidden_fallback is True
        assert provider.policy_boundary == {
            "kind": "provider_descriptor",
            "source": "provider_model_registry",
            "metadata_only": True,
        }

    serialized = json.dumps([provider.model_dump(mode="json") for provider in providers])
    assert "OPENAI_API_KEY" in serialized
    assert "ollama" not in serialized
    assert "api_key" not in serialized
    assert "auth_mode" not in serialized


def test_model_descriptors_include_current_backend_and_profile_models() -> None:
    cfg = default_config()
    models = build_model_descriptors(cfg)
    refs = {(model.provider_id, model.model_profile_id, model.raw_model_ref, model.protocol) for model in models}

    assert ("codex_cli", None, "codex_cli/gpt-5.5", "codex_cli") in refs
    assert ("local_openai_compatible", None, "local_openai_compatible/qwen3-coder:30b", "openai_chat") in refs
    assert ("paid_openai_compatible", None, "paid_openai_compatible/gpt-5.3-codex", "openai_codex_responses") in refs
    assert ("anthropic", None, "anthropic/claude-sonnet-4-20250514", "anthropic_messages") in refs
    assert ("google", None, "google/gemini-2.5-flash", "google_generative") in refs
    assert ("bedrock", None, "bedrock/anthropic.claude-3-5-sonnet-20241022-v2:0", "bedrock_converse") in refs
    assert ("codex_cli", "codex_supervised", "codex_cli/gpt-5.5", "codex_cli") in refs
    assert (
        "local_openai_compatible",
        "local_reasoning",
        "local_openai_compatible/qwen3-coder:30b",
        "openai_chat",
    ) in refs
    assert ("codex", None, "codex/gpt-5.5", "codex_cli") in refs
    assert ("local", None, "local/qwen3-coder", "openai_chat") in refs
    assert ("openai", None, "openai/gpt-5.3-codex", "openai_codex_responses") in refs

    by_ref = {model.raw_model_ref: model for model in models if model.source == "backend_config"}
    aliases_by_ref = {model.raw_model_ref: model for model in models if model.source == "alias"}
    assert by_ref["codex_cli/gpt-5.5"].api_id == "gpt-5.5"
    assert by_ref["codex_cli/gpt-5.5"].canonical_model_ref == "codex_cli/gpt-5.5"
    assert by_ref["codex_cli/gpt-5.5"].alias_of is None
    assert by_ref["codex_cli/gpt-5.5"].metadata_source == "builtin_metadata"
    assert by_ref["codex_cli/gpt-5.5"].reasoning_support == "effort"
    assert by_ref["codex_cli/gpt-5.5"].reasoning_effort_map == {
        "low": "low",
        "medium": "medium",
        "high": "high",
        "xhigh": "xhigh",
    }
    assert set(by_ref["codex_cli/gpt-5.5"].variants) == {"low", "medium", "high", "xhigh"}
    assert by_ref["codex_cli/gpt-5.5"].variants["high"].model_options == {"model_reasoning_effort": "high"}
    assert by_ref["codex_cli/gpt-5.5"].input_modalities == ["text"]
    assert by_ref["codex_cli/gpt-5.5"].output_modalities == ["text"]
    assert by_ref["local_openai_compatible/qwen3-coder:30b"].endpoint == "http://localhost:11434/v1"
    assert by_ref["local_openai_compatible/qwen3-coder:30b"].model_id == "qwen3-coder:30b"
    assert by_ref["local_openai_compatible/qwen3-coder:30b"].metadata_source == "builtin_metadata"
    assert by_ref["local_openai_compatible/qwen3-coder:30b"].reasoning_support == "unknown"
    assert by_ref["local_openai_compatible/qwen3-coder:30b"].variants["deterministic"].model_options == {
        "temperature": 0.0,
        "max_tokens": 2048,
    }
    assert by_ref["paid_openai_compatible/gpt-5.3-codex"].status == "disabled"
    assert by_ref["paid_openai_compatible/gpt-5.3-codex"].metadata_source == "builtin_metadata"
    assert by_ref["paid_openai_compatible/gpt-5.3-codex"].tool_support is True
    assert by_ref["anthropic/claude-sonnet-4-20250514"].status == "disabled"
    assert by_ref["anthropic/claude-sonnet-4-20250514"].metadata_source == "builtin_metadata"
    assert by_ref["anthropic/claude-sonnet-4-20250514"].protocol == "anthropic_messages"
    assert by_ref["anthropic/claude-sonnet-4-20250514"].reasoning_support == "tokens"
    assert by_ref["anthropic/claude-sonnet-4-20250514"].tool_support is True
    assert by_ref["google/gemini-2.5-flash"].status == "disabled"
    assert by_ref["google/gemini-2.5-flash"].metadata_source == "builtin_metadata"
    assert by_ref["google/gemini-2.5-flash"].protocol == "google_generative"
    assert by_ref["google/gemini-2.5-flash"].reasoning_support == "tokens"
    assert by_ref["google/gemini-2.5-flash"].input_modalities == ["text", "image"]
    assert by_ref["google/gemini-2.5-flash"].tool_support is True
    assert by_ref["bedrock/anthropic.claude-3-5-sonnet-20241022-v2:0"].status == "disabled"
    assert by_ref["bedrock/anthropic.claude-3-5-sonnet-20241022-v2:0"].metadata_source == "builtin_metadata"
    assert by_ref["bedrock/anthropic.claude-3-5-sonnet-20241022-v2:0"].protocol == "bedrock_converse"
    assert by_ref["bedrock/anthropic.claude-3-5-sonnet-20241022-v2:0"].input_modalities == ["text", "image"]
    assert by_ref["bedrock/anthropic.claude-3-5-sonnet-20241022-v2:0"].tool_support is True
    assert aliases_by_ref["codex/gpt-5.5"].canonical_model_ref == "codex_cli/gpt-5.5"
    assert aliases_by_ref["codex/gpt-5.5"].alias_of == "codex_cli/gpt-5.5"
    assert aliases_by_ref["codex/gpt-5.5"].backend_id == "codex_cli"
    assert aliases_by_ref["codex/gpt-5.5"].metadata_source == "builtin_alias"
    assert aliases_by_ref["local/qwen3-coder"].canonical_model_ref == "local_openai_compatible/qwen3-coder:30b"
    assert aliases_by_ref["local/qwen3-coder"].backend_id == "local_openai_compatible"
    assert aliases_by_ref["openai/gpt-5.3-codex"].canonical_model_ref == "paid_openai_compatible/gpt-5.3-codex"

    for model in models:
        assert model.schema_version == "harness.model_descriptor/v1"
        assert model.metadata_only is True
        assert model.provider_execution_started is False
        assert model.model_execution_started is False
        assert model.network_accessed is False
        assert model.credentials_included is False
        assert model.hidden_provider_fallback is False
        assert model.hidden_model_fallback is False
        assert model.no_hidden_fallback is True
        assert "fallback" in " ".join(model.safety_notes)


def test_builtin_provider_and_model_metadata_loads_deterministically() -> None:
    providers = load_builtin_provider_metadata()
    models = load_builtin_model_metadata()
    static_models = load_generated_static_model_catalog()
    aliases = load_builtin_model_aliases()

    assert providers["codex_cli"]["display_name"] == "Codex CLI"
    assert providers["local_openai_compatible"]["source"] == "builtin_metadata"
    assert models["codex_cli/gpt-5.5"]["protocol"] == "codex_cli"
    assert models["local_openai_compatible/qwen3-coder:30b"]["protocol"] == "openai_chat"
    assert models["paid_openai_compatible/gpt-5.3-codex"]["tool_support"] is True
    assert models["anthropic/claude-sonnet-4-20250514"]["protocol"] == "anthropic_messages"
    assert models["google/gemini-2.5-flash"]["protocol"] == "google_generative"
    assert models["bedrock/anthropic.claude-3-5-sonnet-20241022-v2:0"]["protocol"] == "bedrock_converse"
    assert static_models["google/gemini-2.5-pro"]["source"] == "generated_static_catalog"
    assert static_models["bedrock/anthropic.claude-3-7-sonnet-20250219-v1:0"]["protocol"] == "bedrock_converse"
    assert aliases["codex/gpt-5.5"].target == "codex_cli/gpt-5.5"
    assert aliases["local/qwen3-coder"].target == "local_openai_compatible/qwen3-coder:30b"
    assert aliases["openai/gpt-5.3-codex"].target == "paid_openai_compatible/gpt-5.3-codex"


def test_generated_static_model_catalog_enriches_descriptors_without_network_or_credentials() -> None:
    cfg = default_config()
    models = {model.raw_model_ref: model for model in build_model_descriptors(cfg)}

    google = models["google/gemini-2.5-pro"]
    bedrock = models["bedrock/anthropic.claude-3-7-sonnet-20250219-v1:0"]

    assert google.source == "static_catalog"
    assert google.metadata_source == "generated_static_catalog"
    assert google.protocol == "google_generative"
    assert google.context_limit == 1048576
    assert google.max_output_tokens == 8192
    assert google.input_modalities == ["text", "image"]
    assert google.reasoning_support == "tokens"
    assert google.tool_support is True
    assert google.release_date == "2025-06-17"
    assert google.family == "gemini-2.5"
    assert google.metadata_only is True
    assert google.network_accessed is False
    assert google.credentials_included is False
    assert google.provider_execution_started is False
    assert google.model_execution_started is False
    assert bedrock.source == "static_catalog"
    assert bedrock.protocol == "bedrock_converse"
    assert bedrock.release_date == "2025-02-19"
    assert bedrock.family == "claude-3.7"


def test_parse_model_ref_preserves_ollama_colon_model_ids_and_parses_variants() -> None:
    local = parse_model_ref("local_openai_compatible/qwen3-coder:30b")
    at_variant = parse_model_ref("codex_cli/gpt-5.5@high")
    colon_variant = parse_model_ref("codex_cli/gpt-5.5:high")
    missing = parse_model_ref(None)

    assert local.provider_id == "local_openai_compatible"
    assert local.model_id == "qwen3-coder:30b"
    assert local.variant is None
    assert at_variant.provider_id == "codex_cli"
    assert at_variant.model_id == "gpt-5.5"
    assert at_variant.variant == "high"
    assert colon_variant.provider_id == "codex_cli"
    assert colon_variant.model_id == "gpt-5.5"
    assert colon_variant.variant == "high"
    assert missing.provider_id is None
    assert missing.model_id is None
    assert missing.variant is None


def test_resolve_model_selection_returns_executable_descriptor_without_provider_calls() -> None:
    cfg = default_config()

    codex = resolve_model_selection(cfg, "codex_cli/gpt-5.5")
    local = resolve_model_selection(cfg, "local_openai_compatible/qwen3-coder:30b")

    assert codex.schema_version == "harness.resolved_model_selection/v1"
    assert codex.raw_model_ref == "codex_cli/gpt-5.5"
    assert codex.canonical_model_ref == "codex_cli/gpt-5.5"
    assert codex.provider_id == "codex_cli"
    assert codex.model_id == "gpt-5.5"
    assert codex.model.protocol == "codex_cli"
    assert codex.model.source == "backend_config"
    assert codex.provider.provider_id == "codex_cli"
    assert codex.resolved_endpoint is None
    assert codex.resolved_provider_options["command"] == "codex"
    assert codex.resolved_provider_options["model_reasoning_effort"] == "low"
    assert codex.resolved_model_options == {"model_reasoning_effort": "low"}
    assert codex.requested_reasoning_effort == "low"
    assert codex.resolved_reasoning_effort == "low"
    assert codex.reasoning_resolution == "exact"
    assert codex.metadata_only is True
    assert codex.provider_execution_started is False
    assert codex.model_execution_started is False
    assert codex.network_accessed is False
    assert codex.credentials_included is False
    assert codex.hidden_provider_fallback is False
    assert codex.hidden_model_fallback is False
    assert codex.no_hidden_fallback is True

    assert local.canonical_model_ref == "local_openai_compatible/qwen3-coder:30b"
    assert local.model_id == "qwen3-coder:30b"
    assert local.model.protocol == "openai_chat"
    assert local.resolved_endpoint == "http://localhost:11434/v1"
    assert local.resolved_provider_options["temperature"] == 0.2
    assert local.resolved_provider_options["max_tokens"] == 4096
    assert local.resolved_model_options == {}
    assert local.reasoning_resolution == "not_requested"


def test_resolve_model_selection_merges_model_variant_and_request_options_in_order() -> None:
    cfg = default_config()

    high = resolve_model_selection(cfg, "codex_cli/gpt-5.5@high")
    local = resolve_model_selection(cfg, "local_openai_compatible/qwen3-coder:30b@deterministic")
    request_override = resolve_model_selection(
        cfg,
        "codex_cli/gpt-5.5",
        request_options={"model_reasoning_effort": "medium", "timeout_seconds": 60},
    )

    assert high.variant == "high"
    assert high.resolved_provider_options["model_reasoning_effort"] == "low"
    assert high.resolved_model_options["model_reasoning_effort"] == "high"
    assert high.requested_reasoning_effort == "high"
    assert high.resolved_reasoning_effort == "high"
    assert high.reasoning_resolution == "exact"
    assert local.variant == "deterministic"
    assert local.resolved_provider_options["temperature"] == 0.2
    assert local.resolved_provider_options["max_tokens"] == 4096
    assert local.resolved_model_options["temperature"] == 0.0
    assert local.resolved_model_options["max_tokens"] == 2048
    assert request_override.resolved_provider_options["timeout_seconds"] == 60
    assert request_override.resolved_model_options["model_reasoning_effort"] == "medium"
    assert request_override.resolved_reasoning_effort == "medium"


def test_resolve_model_selection_rejects_disallowed_options_and_unsupported_reasoning() -> None:
    cfg = default_config()

    with pytest.raises(ModelResolutionError) as disallowed:
        resolve_model_selection(cfg, "codex_cli/gpt-5.5", request_options={"api_key": "secret"})
    with pytest.raises(ModelResolutionError) as unsupported:
        resolve_model_selection(
            cfg,
            "local_openai_compatible/qwen3-coder:30b",
            request_options={"model_reasoning_effort": "high"},
        )

    assert disallowed.value.blocked_reasons == ["option_key_disallowed"]
    assert unsupported.value.blocked_reasons == ["reasoning_effort_unsupported"]


@pytest.mark.parametrize(
    ("raw_model_ref", "request_options", "blocked_reasons"),
    [
        ("google/gemini-2.5-flash", {"context_tokens": 1048577}, ["context_limit_exceeded"]),
        ("google/gemini-2.5-flash", {"max_output_tokens": 8193}, ["output_limit_exceeded"]),
        ("local_openai_compatible/qwen3-coder:30b", {"input_modalities": ["image"]}, ["input_modality_unsupported"]),
        ("local_openai_compatible/qwen3-coder:30b", {"requires_tools": True}, ["tool_support_unsupported"]),
    ],
)
def test_resolve_model_selection_rejects_unsupported_runtime_capability_requests(
    raw_model_ref: str,
    request_options: dict[str, object],
    blocked_reasons: list[str],
) -> None:
    with pytest.raises(ModelResolutionError) as exc:
        resolve_model_selection(default_config(), raw_model_ref, request_options=request_options)

    assert exc.value.blocked_reasons == blocked_reasons


def test_resolve_model_selection_resolves_aliases_to_canonical_descriptors_without_fallback() -> None:
    cfg = default_config()

    codex = resolve_model_selection(cfg, "codex/gpt-5.5")
    local = resolve_model_selection(cfg, "local/qwen3-coder")
    hosted_disabled = resolve_model_selection(cfg, "openai/gpt-5.3-codex")

    assert codex.raw_model_ref == "codex/gpt-5.5"
    assert codex.canonical_model_ref == "codex_cli/gpt-5.5"
    assert codex.alias_used == "codex/gpt-5.5"
    assert codex.provider_id == "codex_cli"
    assert codex.model_id == "gpt-5.5"
    assert codex.provider.provider_id == "codex_cli"
    assert codex.model.raw_model_ref == "codex_cli/gpt-5.5"
    assert codex.model.alias_of is None
    assert local.canonical_model_ref == "local_openai_compatible/qwen3-coder:30b"
    assert local.alias_used == "local/qwen3-coder"
    assert local.provider_id == "local_openai_compatible"
    assert local.model_id == "qwen3-coder:30b"
    assert local.resolved_endpoint == "http://localhost:11434/v1"
    assert hosted_disabled.canonical_model_ref == "paid_openai_compatible/gpt-5.3-codex"
    assert hosted_disabled.alias_used == "openai/gpt-5.3-codex"
    assert hosted_disabled.provider_id == "paid_openai_compatible"


def test_resolve_model_selection_fails_closed_when_alias_target_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        model_registry,
        "load_builtin_model_aliases",
        lambda root=model_registry.BUILTIN_SPECS_DIR: {
            "alias/missing": model_registry.ModelAliasDescriptor(
                alias="alias/missing",
                target="codex_cli/not-real",
            )
        },
    )

    with pytest.raises(ModelResolutionError) as exc:
        resolve_model_selection(default_config(), "alias/missing")

    assert exc.value.raw_model_ref == "alias/missing"
    assert exc.value.blocked_reasons == ["alias_target_unknown"]


@pytest.mark.parametrize(
    ("raw_model_ref", "blocked_reasons"),
    [
        ("", ["model_ref_missing", "provider_not_specified", "model_unknown"]),
        ("gpt-5.5", ["provider_not_specified", "model_unknown"]),
        ("missing/gpt-5.5", ["provider_unknown", "model_unknown"]),
        ("codex_cli/not-a-real-model", ["model_unknown"]),
        ("missing-alias/gpt-5.5", ["provider_unknown", "model_unknown"]),
        ("codex_cli/gpt-5.5@ultra", ["variant_unknown"]),
    ],
)
def test_resolve_model_selection_reports_blocked_reasons(raw_model_ref: str, blocked_reasons: list[str]) -> None:
    with pytest.raises(ModelResolutionError) as exc:
        resolve_model_selection(default_config(), raw_model_ref)

    assert exc.value.raw_model_ref == (raw_model_ref.strip() or None)
    assert exc.value.blocked_reasons == blocked_reasons
